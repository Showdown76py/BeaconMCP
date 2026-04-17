"""Unit tests for the YAML-first config loader."""

from __future__ import annotations

import textwrap
import warnings
from pathlib import Path

import pytest

from beaconmcp.config import Config, ConfigError


def _write(path: Path, yaml_text: str) -> Path:
    path.write_text(textwrap.dedent(yaml_text).lstrip())
    return path


def test_yaml_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "secret1")
    monkeypatch.setenv("RACK1_ILO_PASSWORD", "ilopw")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          verify_ssl: false
          nodes:
            - name: pve1
              host: pve1.example.com
              token_id: root@pam!beaconmcp
              token_secret: ${PVE1_TOKEN_SECRET}
        bmc:
          devices:
            - id: rack1-ilo
              type: hp_ilo
              host: 10.0.0.10
              user: Administrator
              password: ${RACK1_ILO_PASSWORD}
              jump_host: pve1
        """,
    )

    cfg = Config.load(config_path=path)

    assert [n.name for n in cfg.pve_nodes] == ["pve1"]
    assert cfg.pve_nodes[0].token_secret == "secret1"
    assert len(cfg.bmc_devices) == 1
    assert cfg.bmc_devices[0].password == "ilopw"
    assert cfg.bmc_devices[0].jump_host == "pve1"
    assert cfg.verify_ssl is False


def test_missing_env_ref_raises_with_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PVE1_TOKEN_SECRET", raising=False)
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes:
            - name: pve1
              host: pve1.example.com
              token_id: root@pam!beaconmcp
              token_secret: ${PVE1_TOKEN_SECRET}
        """,
    )

    with pytest.raises(ConfigError) as exc:
        Config.load(config_path=path)
    message = str(exc.value)
    assert "PVE1_TOKEN_SECRET" in message
    assert "proxmox.nodes.[0].token_secret" in message


def test_duplicate_bmc_device_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "x")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes:
            - name: pve1
              host: pve1.example.com
              token_id: root@pam!beaconmcp
              token_secret: ${PVE1_TOKEN_SECRET}
        bmc:
          devices:
            - id: dup
              type: hp_ilo
              host: 10.0.0.10
              user: admin
              password: x
            - id: dup
              type: ipmi
              host: 10.0.0.11
              user: admin
              password: y
        """,
    )

    with pytest.raises(ConfigError, match="Duplicate BMC device id"):
        Config.load(config_path=path)


def test_no_proxmox_nodes_exits(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes: []
        """,
    )
    with pytest.raises(SystemExit):
        Config.load(config_path=path)


def test_legacy_env_fallback_synthesizes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the loader at an empty directory so no YAML is found.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACONMCP_CONFIG", raising=False)
    monkeypatch.setenv("PVE1_HOST", "pve1.example.com")
    monkeypatch.setenv("PVE1_TOKEN_ID", "root@pam!beaconmcp")
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "legacy-secret")
    monkeypatch.setenv("SSH_USER", "root")
    monkeypatch.setenv("SSH_PASSWORD", "legacy-ssh")
    monkeypatch.setenv("ILO_HOST", "10.0.0.10")
    monkeypatch.setenv("ILO_USER", "Administrator")
    monkeypatch.setenv("ILO_PASSWORD", "legacy-ilo")

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        cfg = Config.load()

    assert any("deprecated" in str(w.message).lower() for w in captured)
    assert len(cfg.pve_nodes) == 1
    assert cfg.pve_nodes[0].token_secret == "legacy-secret"
    assert len(cfg.bmc_devices) == 1
    assert cfg.bmc_devices[0].type == "hp_ilo"
    assert cfg.bmc_devices[0].jump_host == "pve1"
    assert cfg.ssh is not None
    assert cfg.ssh.password == "legacy-ssh"


def test_ssh_vmid_to_ip_defaults_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "x")
    monkeypatch.setenv("SSH_PASSWORD", "pw")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes:
            - name: pve1
              host: pve1.example.com
              token_id: root@pam!beaconmcp
              token_secret: ${PVE1_TOKEN_SECRET}
        ssh:
          user: root
          password: ${SSH_PASSWORD}
        """,
    )
    cfg = Config.load(config_path=path)
    assert cfg.ssh is not None
    assert cfg.ssh.vmid_to_ip is None


def test_get_node_host_strips_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proxmox.nodes[].host often carries the API port (e.g. :443 behind a reverse
    proxy). SSH and BMC-over-SSH-tunnel need the bare hostname."""
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "x")
    monkeypatch.setenv("PVE2_TOKEN_SECRET", "y")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes:
            - name: pve1
              host: pve1.example.com:443
              token_id: "root@pam!beaconmcp"
              token_secret: ${PVE1_TOKEN_SECRET}
            - name: pve6
              host: "[::1]:8006"
              token_id: "root@pam!beaconmcp"
              token_secret: ${PVE2_TOKEN_SECRET}
        """,
    )
    cfg = Config.load(config_path=path)
    assert cfg.get_node_host("pve1") == "pve1.example.com"
    assert cfg.get_node_host("pve6") == "[::1]"
    assert cfg.get_node_host("missing") is None
    # The raw .host value is preserved for proxmoxer which accepts host:port.
    assert cfg.pve_nodes[0].host == "pve1.example.com:443"


def test_redacted_masks_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "abcdefghij")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes:
            - name: pve1
              host: pve1.example.com
              token_id: root@pam!beaconmcp
              token_secret: ${PVE1_TOKEN_SECRET}
        """,
    )
    cfg = Config.load(config_path=path)
    redacted = cfg.redacted()
    assert "abcdefghij" not in str(redacted)
    assert "***" in redacted["proxmox"]["nodes"][0]["token_secret"]
