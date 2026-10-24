"""Async tools must not call the blocking Proxmox client on the event loop.

``server._metric_tool`` pushes sync tool bodies onto a worker thread but
awaits ``async def`` tools directly on the event loop. A proxmoxer request
(5 s timeout, retried) made straight from an async tool therefore freezes
every other request, ``/health`` included, for as long as the node takes to
answer. Async tools have to route those calls through ``run_in_executor``.

This walks every ``async def`` under ``src/beaconmcp`` and fails on a direct
call to the client's HTTP methods or to one of the blocking helpers that
wrap them. Calls inside a nested ``def`` or ``lambda`` are fine: that is the
shape handed to ``run_in_executor``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "beaconmcp"

# Names the Proxmox client is bound to in tool modules.
CLIENT_NAMES = {"client", "proxmox_client"}
CLIENT_METHODS = {"get", "post", "put", "delete", "api_call"}
# Sync helpers that issue Proxmox requests themselves.
BLOCKING_HELPERS = {
    "_detect_vm_type",
    "_qemu_file_size",
    "_start_async_qemu",
    "_poll_session",
    "_recent_errors",
}


def _direct_calls(fn: ast.AsyncFunctionDef):
    """Yield calls in ``fn``'s own body, not in nested functions or lambdas."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _offenders() -> list[str]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in _direct_calls(fn):
                func = call.func
                blocking = (
                    isinstance(func, ast.Attribute)
                    and func.attr in CLIENT_METHODS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in CLIENT_NAMES
                ) or (isinstance(func, ast.Name) and func.id in BLOCKING_HELPERS)
                if blocking:
                    rel = path.relative_to(SRC.parent)
                    found.append(f"{rel}:{call.lineno} {fn.name}: {ast.unparse(func)}")
    return found


def test_no_blocking_proxmox_call_in_async_functions() -> None:
    offenders = _offenders()
    assert not offenders, (
        "async functions calling the blocking Proxmox client directly "
        "(wrap them in loop.run_in_executor):\n  " + "\n  ".join(offenders)
    )


def test_checker_flags_a_direct_call() -> None:
    # Guard against the walker silently matching nothing.
    tree = ast.parse(
        "async def t():\n"
        "    client.get('n', 'p')\n"
        "    _detect_vm_type(client, 'n', 1)\n"
        "    await loop.run_in_executor(None, lambda: client.get('n', 'p'))\n"
    )
    fn = tree.body[0]
    names = [ast.unparse(c.func) for c in _direct_calls(fn)]
    assert "client.get" in names
    assert "_detect_vm_type" in names
    # The call inside the lambda is not a direct call.
    assert names.count("client.get") == 1
