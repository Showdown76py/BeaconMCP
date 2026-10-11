"""The shipped ``allowed_origins`` defaults must let the Claude web connector in.

``allowed_origins`` is both the CORS allowlist and the HTTPS ``redirect_uri``
allowlist for ``/oauth/authorize``, so a missing ``https://claude.ai`` makes
the Claude web/mobile connector fail with ``invalid_redirect_uri`` on a
default install. Every place that ships a default is checked here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from beaconmcp.auth import is_trusted_redirect_uri  # noqa: E402
from beaconmcp.wizard import ServerDraft  # noqa: E402

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _server_builtin_origins() -> list[str]:
    """Read the env-less fallback list in server.py without importing it.

    Importing ``beaconmcp.server`` loads the real config and builds the app,
    so the literal passed to ``_csv_env("BEACONMCP_ALLOWED_ORIGINS", [...])``
    is extracted from the source instead.
    """
    tree = ast.parse((ROOT / "src/beaconmcp/server.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_csv_env"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "BEACONMCP_ALLOWED_ORIGINS"
        ):
            return ast.literal_eval(node.args[1])
    raise AssertionError("BEACONMCP_ALLOWED_ORIGINS fallback not found in server.py")


def _example_yaml_origins() -> list[str]:
    data = yaml.safe_load((ROOT / "beaconmcp.yaml.example").read_text())
    return data["server"]["allowed_origins"]


def test_defaults_include_claude_and_not_assistant_ai() -> None:
    for source, origins in (
        ("server.py fallback", _server_builtin_origins()),
        ("beaconmcp.yaml.example", _example_yaml_origins()),
        ("wizard ServerDraft", ServerDraft().allowed_origins),
    ):
        assert "https://claude.ai" in origins, source
        assert "https://assistant.ai" not in origins, source


def test_claude_callback_trusted_with_each_default() -> None:
    for origins in (
        _server_builtin_origins(),
        _example_yaml_origins(),
        ServerDraft().allowed_origins,
    ):
        assert is_trusted_redirect_uri(CLAUDE_CALLBACK, origins)
