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
    monkeypatch.setenv("PVE1_SSH_PW", "ssh-secret")
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
        ssh:
          hosts:
            - name: pve1-ssh
              host: pve1.example.com
              user: root
              password: ${PVE1_SSH_PW}
        bmc:
          devices:
            - id: rack1-ilo
              type: hp_ilo
              host: 10.0.0.10
              user: Administrator
              password: ${RACK1_ILO_PASSWORD}
              jump_host: pve1-ssh
        """,
    )

    cfg = Config.load(config_path=path)

    assert [n.name for n in cfg.pve_nodes] == ["pve1"]
    assert cfg.pve_nodes[0].token_secret == "secret1"
    assert len(cfg.bmc_devices) == 1
    assert cfg.bmc_devices[0].password == "ilopw"
    assert cfg.bmc_devices[0].jump_host == "pve1-ssh"
    assert cfg.ssh is not None and len(cfg.ssh.hosts) == 1
    assert cfg.ssh.hosts[0].password == "ssh-secret"
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


def test_at_least_one_capability_required(tmp_path: Path) -> None:
    """A config with no Proxmox, no SSH, no BMC should be refused."""
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes: []
        """,
    )
    with pytest.raises(ConfigError, match="at least one capability"):
        Config.load(config_path=path)


def test_ssh_only_minimal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A VPS-only user: no Proxmox, no BMC, just SSH hosts. Must load fine."""
    monkeypatch.setenv("VPS1_PW", "pw1")
    monkeypatch.setenv("VPS2_PW", "pw2")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        ssh:
          hosts:
            - name: vps1
              host: 198.51.100.10
              user: root
              password: ${VPS1_PW}
            - name: vps2
              host: 198.51.100.11
              port: 2222
              user: admin
              password: ${VPS2_PW}
        """,
    )
    cfg = Config.load(config_path=path)
    assert cfg.pve_nodes == []
    assert cfg.bmc_devices == []
    assert cfg.ssh is not None
    assert [h.name for h in cfg.ssh.hosts] == ["vps1", "vps2"]
    assert cfg.ssh.hosts[1].port == 2222


def test_proxmox_only_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxmox-only: no ssh: and no bmc: section. Must load."""
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
        """,
    )
    cfg = Config.load(config_path=path)
    assert len(cfg.pve_nodes) == 1
    assert cfg.ssh is None
    assert cfg.bmc_devices == []


def test_ssh_legacy_shape_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy ssh.user/ssh.password flat shape must produce an actionable error."""
    monkeypatch.setenv("SSH_PW", "x")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        ssh:
          user: root
          password: ${SSH_PW}
          vmid_to_ip: "192.168.1.{id}"
        """,
    )
    with pytest.raises(ConfigError, match="legacy shape"):
        Config.load(config_path=path)


def test_ssh_host_name_collides_with_proxmox_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an ssh.hosts[].name matches a proxmox.nodes[].name, refuse to start."""
    monkeypatch.setenv("PVE1_TOKEN_SECRET", "x")
    monkeypatch.setenv("SSH_PW", "y")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        proxmox:
          nodes:
            - name: serv1
              host: serv1.example.com
              token_id: root@pam!beaconmcp
              token_secret: ${PVE1_TOKEN_SECRET}
        ssh:
          hosts:
            - name: serv1
              host: serv1.example.com
              user: root
              password: ${SSH_PW}
        """,
    )
    with pytest.raises(ConfigError, match="declared both in"):
        Config.load(config_path=path)


def test_ssh_host_requires_one_auth_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each SSH host must provide exactly one of password or key_file."""
    # Case 1: neither provided
    path1 = _write(
        tmp_path / "neither.yaml",
        """
        version: 1
        ssh:
          hosts:
            - name: vps1
              host: 198.51.100.10
              user: root
        """,
    )
    with pytest.raises(ConfigError, match="neither"):
        Config.load(config_path=path1)

    # Case 2: both provided
    monkeypatch.setenv("VPS_PW", "x")
    path2 = _write(
        tmp_path / "both.yaml",
        """
        version: 1
        ssh:
          hosts:
            - name: vps1
              host: 198.51.100.10
              user: root
              password: ${VPS_PW}
              key_file: ~/.ssh/id_ed25519
        """,
    )
    with pytest.raises(ConfigError, match="both"):
        Config.load(config_path=path2)


def test_ssh_empty_hosts_list_rejected(tmp_path: Path) -> None:
    """An ssh: section with no hosts[] entries is a config mistake."""
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        ssh:
          hosts: []
        """,
    )
    with pytest.raises(ConfigError, match="at least one host entry"):
        Config.load(config_path=path)


def test_ssh_duplicate_host_names_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PW", "x")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        ssh:
          hosts:
            - name: dup
              host: 198.51.100.10
              user: root
              password: ${PW}
            - name: dup
              host: 198.51.100.11
              user: root
              password: ${PW}
        """,
    )
    with pytest.raises(ConfigError, match="duplicate name"):
        Config.load(config_path=path)


def test_bmc_jump_host_must_reference_ssh_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bmc.devices[].jump_host must match an ssh.hosts[].name — caught at load."""
    monkeypatch.setenv("ILO_PW", "x")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        bmc:
          devices:
            - id: ilo1
              type: hp_ilo
              host: 10.0.0.10
              user: Administrator
              password: ${ILO_PW}
              jump_host: nonexistent
        """,
    )
    with pytest.raises(ConfigError, match="jump_host"):
        Config.load(config_path=path)


def test_legacy_env_fallback_drops_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy env path still synthesizes Proxmox and BMC, but drops SSH with a warning."""
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
    # Deliberately don't set ILO_JUMP_HOST so BMC loads without needing
    # an SSH host (legacy path can no longer synthesize one).

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        cfg = Config.load()

    messages = [str(w.message) for w in captured]
    # Two deprecations: top-level legacy-env + SSH-drop.
    assert any("deprecated" in m.lower() for m in messages)
    assert any("SSH" in m and "no longer supported" in m for m in messages)
    assert len(cfg.pve_nodes) == 1
    assert cfg.pve_nodes[0].token_secret == "legacy-secret"
    assert len(cfg.bmc_devices) == 1
    assert cfg.bmc_devices[0].type == "hp_ilo"
    assert cfg.bmc_devices[0].jump_host is None  # jump not synthesized in legacy
    assert cfg.ssh is None


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
    monkeypatch.setenv("VPS_PW", "1234567890")
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
          hosts:
            - name: vps1
              host: 198.51.100.10
              user: root
              password: ${VPS_PW}
        """,
    )
    cfg = Config.load(config_path=path)
    redacted = cfg.redacted()
    s = str(redacted)
    assert "abcdefghij" not in s
    assert "1234567890" not in s
    assert "***" in redacted["proxmox"]["nodes"][0]["token_secret"]
    assert "***" in redacted["ssh"]["hosts"][0]["password"]


def test_get_ssh_host_accessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VPS_PW", "pw")
    path = _write(
        tmp_path / "beaconmcp.yaml",
        """
        version: 1
        ssh:
          hosts:
            - name: vps1
              host: 198.51.100.10
              user: root
              password: ${VPS_PW}
        """,
    )
    cfg = Config.load(config_path=path)
    h = cfg.get_ssh_host("vps1")
    assert h is not None
    assert h.host == "198.51.100.10"
    assert cfg.get_ssh_host("missing") is None
    assert cfg.get_ssh_host_by_address("198.51.100.10") is not None
    assert cfg.get_ssh_host_by_address("10.0.0.1") is None
