"""MCP tool annotations: every tool declares them, and they reach tools/list.

The table in ``beaconmcp.tool_annotations`` is the single source. These tests
read what a client actually receives from ``tools/list`` on the real /mcp
stack, so a tool added without an entry (or an entry left behind by a removed
tool) fails here instead of shipping with default hints.
"""

from __future__ import annotations

from typing import Any

# Reuse the real-stack harness; pytest puts tests/ on sys.path.
from test_mcp_http_stack import _body, _client, _handshake, _rpc
from test_mcp_http_stack import stack as stack  # fixture re-export


async def _listed_tools(stack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    app = stack["app"]
    async with app.router.lifespan_context(app), _client(app) as c:
        h = await _handshake(c, stack["token"])
        resp = await c.post("/mcp", json=_rpc("tools/list", 2), headers=h)
    assert resp.status_code == 200, resp.text
    return {t["name"]: t for t in _body(resp)["result"]["tools"]}


async def test_every_listed_tool_carries_annotations(stack) -> None:
    tools = await _listed_tools(stack)
    missing = sorted(n for n, t in tools.items() if not t.get("annotations"))
    assert not missing, f"tools without annotations on the wire: {missing}"
    for name, tool in tools.items():
        ann = tool["annotations"]
        assert isinstance(ann.get("readOnlyHint"), bool), name
        assert isinstance(ann.get("openWorldHint"), bool), name


async def test_table_and_registered_tools_match_exactly(stack) -> None:
    from beaconmcp.tool_annotations import TOOL_ANNOTATIONS

    registered = set(await _listed_tools(stack))
    table = set(TOOL_ANNOTATIONS)
    assert not registered - table, f"tools missing from the table: {sorted(registered - table)}"
    assert not table - registered, f"table entries without a tool: {sorted(table - registered)}"


async def test_wire_matches_table(stack) -> None:
    from beaconmcp.tool_annotations import TOOL_ANNOTATIONS

    tools = await _listed_tools(stack)
    for name, tool in tools.items():
        want = TOOL_ANNOTATIONS[name].model_dump(by_alias=True, exclude_none=True)
        assert tool["annotations"] == want, name


def test_read_only_entries_set_no_write_hints() -> None:
    from beaconmcp.tool_annotations import TOOL_ANNOTATIONS

    for name, ann in TOOL_ANNOTATIONS.items():
        if ann.read_only_hint:
            # destructive/idempotent are meaningless for a tool that changes nothing
            assert ann.destructive_hint is None, name
            assert ann.idempotent_hint is None, name
        else:
            assert isinstance(ann.destructive_hint, bool), name
            assert isinstance(ann.idempotent_hint, bool), name


def test_server_confirm_tools_are_destructive() -> None:
    from beaconmcp.confirm import DEFAULT_CONFIRM_TOOLS
    from beaconmcp.tool_annotations import TOOL_ANNOTATIONS

    for name in DEFAULT_CONFIRM_TOOLS:
        ann = TOOL_ANNOTATIONS[name]
        assert ann.read_only_hint is False and ann.destructive_hint is True, name


def test_known_classifications() -> None:
    from beaconmcp.tool_annotations import TOOL_ANNOTATIONS as T

    # Arbitrary execution and overwrites are destructive. upload_link only mints
    # a link, but whoever holds it can overwrite a file in the guest.
    for name in ("ssh_run", "proxmox_run", "proxmox_write_file", "proxmox_upload_file",
                 "proxmox_upload_link", "proxmox_vm_create", "vm_bulk_action",
                 "proxmox_vm_stop"):
        assert T[name].read_only_hint is False and T[name].destructive_hint is True, name
    # Modes: reads without `updates` but writes with it, so not read-only.
    assert T["proxmox_vm_config"].read_only_hint is False
    # Pure additions are not destructive.
    for name in ("proxmox_snapshot_create", "proxmox_vm_clone", "proxmox_vm_start",
                 "proxmox_backup_create"):
        assert T[name].destructive_hint is False, name
    # Ending a session changes state, so it is not read-only.
    assert T["security_end_session"].read_only_hint is False
    # Only tools that reach beyond the declared infrastructure are open-world.
    open_world = {n for n, a in T.items() if a.open_world_hint}
    assert open_world == {"proxmox_fetch_url", "beaconmcp_check_update", "beaconmcp_self_update"}
