"""``beaconmcp check-startup``: the gate a self-update must pass before restarting.

Runs the real CLI in a subprocess, the way ``apply_update`` does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
SESSION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _config(tmp_path: Path, *, dashboard: bool) -> Path:
    path = tmp_path / "beaconmcp.yaml"
    path.write_text(textwrap.dedent(f"""
        version: 1
        server:
          clients_file: {tmp_path}/state/clients.json
          tokens_db: {tmp_path}/state/tokens.db
          audit_log: {tmp_path}/state/audit.log
        proxmox:
          nodes:
            - name: pve1
              host: 127.0.0.1:1
              token_id: root@pam!beaconmcp
              token_secret: secret
        features:
          dashboard:
            enabled: {"true" if dashboard else "false"}
          updates:
            enabled: false
    """).lstrip())
    return path


def _cli(command: str, config: Path, **env: str) -> subprocess.CompletedProcess:
    full_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("BEACONMCP_")
    }
    full_env.update(PYTHONPATH=str(SRC), **env)
    full_env.setdefault("BEACONMCP_DASHBOARD_DB", str(config.parent / "state" / "dashboard.db"))
    return subprocess.run(
        [sys.executable, "-m", "beaconmcp", command, "--config", str(config)],
        capture_output=True, text=True, timeout=120, env=full_env, cwd=config.parent,
    )


def test_a_server_that_builds_passes(tmp_path):
    config = _config(tmp_path, dashboard=True)
    proc = _cli("check-startup", config, BEACONMCP_SESSION_KEY=SESSION_KEY)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "/health answers" in proc.stdout
    # Live state is never touched: everything went to a scratch directory.
    assert not (tmp_path / "state").exists()


def test_a_failure_validate_config_misses_is_caught(tmp_path):
    """The config parses, but the server cannot build (dashboard without its key)."""
    config = _config(tmp_path, dashboard=True)
    assert _cli("validate-config", config).returncode == 0
    proc = _cli("check-startup", config)
    assert proc.returncode == 1
    assert "BEACONMCP_SESSION_KEY is required" in proc.stderr
    assert "does not start with this configuration" in proc.stderr


def test_an_invalid_config_fails(tmp_path):
    config = _config(tmp_path, dashboard=False)
    config.write_text(config.read_text() + "    channel: nightly\n")
    proc = _cli("check-startup", config)
    assert proc.returncode == 1
    assert "features.updates.channel" in proc.stderr


def test_watchdog_polls_the_server_on_a_local_address():
    sys.path.insert(0, str(SRC))
    from beaconmcp.__main__ import _local_health_url

    assert _local_health_url("0.0.0.0", 8420) == "http://127.0.0.1:8420/health"
    assert _local_health_url("::", 8420) == "http://[::1]:8420/health"
    assert _local_health_url("10.0.0.5", 9000) == "http://10.0.0.5:9000/health"
    assert _local_health_url("fd00::5", 9000) == "http://[fd00::5]:9000/health"
