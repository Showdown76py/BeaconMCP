"""Regression tests for the security-audit hardening pass.

Each test pins one specific weakness that was found and fixed, so a future
refactor that reopens it fails here rather than in production.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp import audit
from beaconmcp.auth import is_trusted_redirect_uri
from beaconmcp.config import Config, ConfigError
from beaconmcp.dashboard.chat import _tool_call_requires_confirmation
from beaconmcp.dashboard.db import Database
from beaconmcp.proxmox.client import ProxmoxClient


# --- redirect_uri: loopback userinfo evasion --------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        # Real host is attacker.example; the string merely *starts with*
        # a trusted loopback prefix.
        "http://localhost:1@attacker.example/cb",
        "http://127.0.0.1:8080@evil.example/callback",
        "http://localhost@evil.example/cb",
        "http://[::1]:80@evil.example/cb",
        # Plain-HTTP host that is simply not loopback.
        "http://evil.example/cb",
        # An allowed HTTPS origin must not be reachable over plaintext http.
        "http://assistant.ai/cb",
    ],
)
def test_loopback_prefix_evasion_rejected(uri: str) -> None:
    assert not is_trusted_redirect_uri(uri, ["https://assistant.ai"]), uri


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:54321/callback",
        "http://localhost/callback",
        "http://127.0.0.1:3000/oauth/cb",
        "http://[::1]:8080/cb",
        "vscode://ms-vscode.remote/callback",
    ],
)
def test_genuine_loopback_and_scheme_callbacks_still_accepted(uri: str) -> None:
    assert is_trusted_redirect_uri(uri), uri


# --- Proxmox API path-segment injection -------------------------------------


class _StubConfig:
    pve_nodes: list = []
    verify_ssl = False

    def get_node(self, name):  # pragma: no cover - never reached
        raise AssertionError("connection must not be attempted")


@pytest.mark.parametrize(
    "path",
    [
        # A snapname of "../../../access/users" re-targets the request.
        "nodes/pve1/qemu/100/snapshot/../../../access/users/rollback",
        "nodes/pve1/storage/../../access/domains/content",
        "nodes/pve1/qemu/100/snapshot//rollback",
        "nodes/pve1/qemu/100/snapshot/_store/rollback",
    ],
)
def test_traversal_path_segments_rejected_before_any_request(path: str) -> None:
    client = ProxmoxClient(_StubConfig())  # type: ignore[arg-type]
    result = client.get("pve1", path)
    assert isinstance(result, dict)
    assert "illegal Proxmox API path segment" in result["error"]


def test_legitimate_paths_are_not_rejected() -> None:
    """The guard must not fire on the shapes real tools build."""
    from beaconmcp.proxmox.client import _split_api_path

    for path in (
        "nodes",
        "version",
        "nodes/pve-1/qemu/100/status/current",
        "nodes/pve1/lxc/200/snapshot/pre-upgrade_2024/rollback",
        "nodes/pve1/storage/local-lvm/content",
        "nodes/pve1/storage/pbs.backup/content",
        "nodes/pve1/qemu/100/agent/exec-status",
        "nodes/pve1/vzdump",
    ):
        assert _split_api_path(path)


# --- chat: dangerous-tool confirmation gate ---------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        # Writing a guest file is code execution in one hop
        # (~/.ssh/authorized_keys, /etc/cron.d/...).
        ("proxmox_write_file", {"node": "pve1", "vmid": 100, "path": "/x", "content": "y"}),
        ("proxmox_upload_file", {"source": "a", "dest": "/b"}),
        ("proxmox_run", {"node": "pve1", "vmid": 100, "command": "id"}),
        ("ssh_run", {"host": "pve1", "command": "id"}),
        # Destructive / irreversible.
        ("vm_bulk_action", {"vmids": [1, 2], "action": "stop"}),
        ("proxmox_vm_stop", {"node": "pve1", "vmid": 100}),
        ("proxmox_snapshot_rollback", {"node": "pve1", "vmid": 100, "snapname": "s"}),
        ("proxmox_snapshot_delete", {"node": "pve1", "vmid": 100, "snapname": "s"}),
        ("proxmox_backup_restore", {"node": "pve1", "vmid": 100, "archive": "a"}),
        ("bmc_power_off", {"device_id": "rack1"}),
        ("bmc_power_reset", {"device_id": "rack1"}),
        # Read-or-write tool, in its writing shape.
        ("proxmox_vm_config", {"node": "pve1", "vmid": 100, "updates": {"memory": 1}}),
    ],
)
def test_dangerous_tools_require_confirmation(name: str, args: dict) -> None:
    assert _tool_call_requires_confirmation(name, args), name


@pytest.mark.parametrize(
    "name,args",
    [
        # Pure reads.
        ("proxmox_list_vms", {}),
        ("proxmox_read_file", {"node": "pve1", "vmid": 100, "path": "/etc/hosts"}),
        ("cluster_overview", {}),
        ("bmc_power_status", {"device_id": "rack1"}),
        # Read shape of the read-or-write tool.
        ("proxmox_vm_config", {"node": "pve1", "vmid": 100}),
        ("proxmox_vm_config", {"node": "pve1", "vmid": 100, "updates": None}),
        # Polling an already-approved exec session is read-only.
        ("proxmox_run", {"exec_id": "abc123"}),
        ("ssh_run", {"exec_id": "abc123"}),
        # dry_run tools only describe what they would do.
        ("proxmox_snapshot_delete", {"node": "pve1", "vmid": 1, "snapname": "s", "dry_run": True}),
    ],
)
def test_read_only_calls_do_not_require_confirmation(name: str, args: dict) -> None:
    assert not _tool_call_requires_confirmation(name, args), name


# --- BMC verify_tls actually reaches the backend ----------------------------


def _bmc_yaml(tmp_path: Path, verify_tls: str) -> Path:
    path = tmp_path / "beaconmcp.yaml"
    path.write_text(
        "version: 1\n"
        "bmc:\n"
        "  devices:\n"
        "    - id: rack1\n"
        "      type: redfish\n"
        "      host: 10.0.0.5\n"
        "      user: root\n"
        "      password: pw\n"
        f"      verify_tls: {verify_tls}\n"
    )
    return path


def test_verify_tls_is_parsed_from_yaml(tmp_path: Path) -> None:
    cfg = Config.load(config_path=_bmc_yaml(tmp_path, "true"))
    assert cfg.bmc_devices[0].verify_tls is True

    cfg = Config.load(config_path=_bmc_yaml(tmp_path, "false"))
    assert cfg.bmc_devices[0].verify_tls is False


def test_verify_tls_reaches_the_redfish_backend(tmp_path: Path) -> None:
    from beaconmcp.bmc import build_registry

    cfg = Config.load(config_path=_bmc_yaml(tmp_path, "true"))
    backend = build_registry(cfg)["rack1"]
    assert backend._verify is True  # type: ignore[attr-defined]


def test_verify_tls_defaults_to_false_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "beaconmcp.yaml"
    path.write_text(
        "version: 1\n"
        "bmc:\n"
        "  devices:\n"
        "    - id: rack1\n"
        "      type: redfish\n"
        "      host: 10.0.0.5\n"
        "      user: root\n"
        "      password: pw\n"
    )
    cfg = Config.load(config_path=path)
    assert cfg.bmc_devices[0].verify_tls is False


# --- dashboard.db must not be world-readable --------------------------------


def test_dashboard_db_is_owner_only(tmp_path: Path) -> None:
    db_file = tmp_path / "dashboard.db"
    Database(db_file)
    mode = stat.S_IMODE(os.stat(db_file).st_mode)
    assert mode & 0o077 == 0, f"dashboard.db is group/world accessible: {mode:o}"
    for sidecar in (
        db_file.with_name(db_file.name + "-wal"),
        db_file.with_name(db_file.name + "-shm"),
    ):
        if sidecar.exists():
            side_mode = stat.S_IMODE(os.stat(sidecar).st_mode)
            assert side_mode & 0o077 == 0, f"{sidecar.name}: {side_mode:o}"


# --- audit redaction --------------------------------------------------------


def test_audit_redacts_session_and_oauth_material() -> None:
    redacted = audit._redact(
        {
            "session_id": "s3cr3t-session",
            "code_verifier": "verifier",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "nested": {"access_token": "tok", "client_id": "beaconmcp_1"},
            "client_id": "beaconmcp_1",
        }
    )
    assert redacted["session_id"] == "***"
    assert redacted["code_verifier"] == "***"
    assert redacted["totp_secret"] == "***"
    assert redacted["nested"]["access_token"] == "***"
    # Non-secret identifiers stay readable -- the log is useless otherwise.
    assert redacted["client_id"] == "beaconmcp_1"
    assert redacted["nested"]["client_id"] == "beaconmcp_1"


def test_config_error_is_importable() -> None:
    """Guard against the ConfigError import above going stale."""
    assert issubclass(ConfigError, Exception)
