"""`beaconmcp serve` must bind where the operator asked.

Precedence: ``--host``/``--port`` on the command line, then
``BEACONMCP_HOST``/``BEACONMCP_PORT``, then ``server.host``/``server.port``
in beaconmcp.yaml, then the built-in 0.0.0.0:8420. The systemd unit runs
``python -m beaconmcp serve`` with no flags, so the YAML value is what an
operator relies on to keep the server off the LAN.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

ROOT = Path(__file__).parent.parent

# Same throwaway values as the `config` job in .github/workflows/ci.yml.
_CI_ENV = {
    "BEACONMCP_SESSION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "GEMINI_API_KEY": "ci-dummy",
    "PVE1_TOKEN_SECRET": "ci-dummy",
    "PVE2_TOKEN_SECRET": "ci-dummy",
    "RACK1_ILO_PASSWORD": "ci-dummy",
    "RACK2_IPMI_PASSWORD": "ci-dummy",
    "RACK3_IDRAC_PASSWORD": "ci-dummy",
    "VPS2_PW": "ci-dummy",
}


def _serve(monkeypatch: pytest.MonkeyPatch, argv: list[str], *, yaml_host: str,
           yaml_port: int, env: dict[str, str] | None = None) -> tuple[str, int]:
    """Run `main()` with *argv* and return the (host, port) handed to `_run_http`."""
    for key, value in _CI_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BEACONMCP_CONFIG", str(ROOT / "beaconmcp.yaml.example"))
    monkeypatch.delenv("BEACONMCP_HOST", raising=False)
    monkeypatch.delenv("BEACONMCP_PORT", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    from beaconmcp import __main__ as entry
    from beaconmcp import updates
    from beaconmcp.server import config

    monkeypatch.setattr(config.server, "host", yaml_host)
    monkeypatch.setattr(config.server, "port", yaml_port)
    monkeypatch.setattr(entry, "_configure_audit_log", lambda *_a, **_k: None)
    monkeypatch.setattr(updates, "configure", lambda *_a, **_k: None)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(entry, "_run_http",
                        lambda _mcp, host, port: captured.update(host=host, port=port))
    monkeypatch.setattr(sys, "argv", ["beaconmcp", *argv])

    entry.main()
    return captured["host"], captured["port"]


@pytest.mark.parametrize("argv", [["serve"], []])
def test_yaml_bind_address_is_used_when_nothing_overrides_it(monkeypatch, argv):
    assert _serve(monkeypatch, argv, yaml_host="127.0.0.1", yaml_port=9431) == (
        "127.0.0.1", 9431,
    )


def test_env_overrides_yaml(monkeypatch):
    got = _serve(monkeypatch, ["serve"], yaml_host="127.0.0.1", yaml_port=9431,
                 env={"BEACONMCP_HOST": "192.0.2.10", "BEACONMCP_PORT": "9432"})
    assert got == ("192.0.2.10", 9432)


def test_cli_overrides_env_and_yaml(monkeypatch):
    got = _serve(monkeypatch, ["serve", "--host", "192.0.2.20", "--port", "9433"],
                 yaml_host="127.0.0.1", yaml_port=9431,
                 env={"BEACONMCP_HOST": "192.0.2.10", "BEACONMCP_PORT": "9432"})
    assert got == ("192.0.2.20", 9433)


def test_builtin_default_when_yaml_leaves_it_unset(monkeypatch):
    # config.py defaults server.host/port to 0.0.0.0:8420 when the YAML is silent.
    assert _serve(monkeypatch, ["serve"], yaml_host="0.0.0.0", yaml_port=8420) == (
        "0.0.0.0", 8420,
    )
