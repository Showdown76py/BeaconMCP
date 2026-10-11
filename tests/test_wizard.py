"""Coverage for the init wizard YAML projection helpers."""

from __future__ import annotations

from pathlib import Path

from beaconmcp.wizard import ConfigDraft, load_yaml_into_draft, render_yaml


def test_render_yaml_emits_trusted_proxies() -> None:
    draft = ConfigDraft()
    draft.server.trusted_proxies = ["127.0.0.1", "::1", "cloudflare"]

    yaml_text = render_yaml(draft)

    assert "trusted_proxies:" in yaml_text
    assert "- 127.0.0.1" in yaml_text
    assert '- "::1"' in yaml_text
    assert "- cloudflare" in yaml_text


def test_load_yaml_into_draft_reads_trusted_proxies(tmp_path: Path) -> None:
    cfg = tmp_path / "beaconmcp.yaml"
    cfg.write_text(
        """
version: 1
server:
  trusted_proxies:
    - 127.0.0.1
    - ::1
    - cloudflare
""".lstrip(),
        encoding="utf-8",
    )

    draft = load_yaml_into_draft(cfg)

    assert draft.server.trusted_proxies == ["127.0.0.1", "::1", "cloudflare"]


def test_render_yaml_round_trips_host_key_and_server_paths(tmp_path: Path) -> None:
    from beaconmcp.wizard import SSHHostDraft

    draft = ConfigDraft()
    draft.server.tokens_db = "/var/lib/beaconmcp/tokens.db"
    draft.server.audit_log = "-"
    draft.ssh.known_hosts = "/etc/beaconmcp/known_hosts"
    draft.ssh.strict_host_key_checking = "true"
    draft.ssh.defaults.key_file = "~/.ssh/beaconmcp"
    draft.ssh.hosts.append(SSHHostDraft(
        name="vps",
        host="198.51.100.10",
        user="root",
        password_env="VPS_PW",
        known_hosts="/etc/beaconmcp/vps_known_hosts",
        strict_host_key_checking="false",
    ))

    yaml_text = render_yaml(draft)
    # A bare "-" must be quoted or it parses as a sequence entry.
    assert 'audit_log: "-"' in yaml_text

    cfg = tmp_path / "beaconmcp.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    reloaded = load_yaml_into_draft(cfg)

    assert reloaded.server.tokens_db == "/var/lib/beaconmcp/tokens.db"
    assert reloaded.server.audit_log == "-"
    assert reloaded.ssh.known_hosts == "/etc/beaconmcp/known_hosts"
    assert reloaded.ssh.strict_host_key_checking == "true"
    host = reloaded.ssh.hosts[0]
    assert host.known_hosts == "/etc/beaconmcp/vps_known_hosts"
    assert host.strict_host_key_checking == "false"
    # Stable round-trip: saving again must not drop or reorder anything.
    assert render_yaml(reloaded) == yaml_text


def test_load_yaml_defaults_host_key_fields_to_inherit(tmp_path: Path) -> None:
    cfg = tmp_path / "beaconmcp.yaml"
    cfg.write_text(
        """
version: 1
ssh:
  hosts:
    - name: vps1
      host: 198.51.100.10
      user: root
      key_file: ~/.ssh/id_ed25519
""".lstrip(),
        encoding="utf-8",
    )

    draft = load_yaml_into_draft(cfg)

    assert draft.ssh.known_hosts == ""
    assert draft.ssh.strict_host_key_checking == ""
    assert draft.ssh.hosts[0].known_hosts == ""
    assert draft.ssh.hosts[0].strict_host_key_checking == ""


def test_named_token_ttl_zero_round_trips(tmp_path: Path) -> None:
    cfg = tmp_path / "beaconmcp.yaml"
    cfg.write_text(
        """
version: 1
server:
  named_token_ttl: 0
""".lstrip(),
        encoding="utf-8",
    )
    draft = load_yaml_into_draft(cfg)
    # 0 means "never expires" -- it must not be dropped as falsy.
    assert draft.server.named_token_ttl == "0"
    assert "named_token_ttl: 0" in render_yaml(draft)


def test_render_yaml_round_trips_verify_ssl_ca_and_explicit_strict_false(
    tmp_path: Path,
) -> None:
    from beaconmcp.wizard import PVENodeDraft

    draft = ConfigDraft()
    draft.pve_nodes.append(PVENodeDraft(
        name="pve1", host="10.0.0.1", token_id="root@pam!t", token_secret_env="PVE1",
    ))
    draft.verify_ssl_ca = "/etc/beaconmcp/pve-root-ca.pem"
    # An explicit opt-out must survive a save: unset now means "pin on first use".
    draft.ssh.strict_host_key_checking = "false"
    yaml_text = render_yaml(draft)
    assert "verify_ssl: /etc/beaconmcp/pve-root-ca.pem" in yaml_text
    assert "strict_host_key_checking: false" in yaml_text

    cfg = tmp_path / "beaconmcp.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    reloaded = load_yaml_into_draft(cfg)
    assert reloaded.verify_ssl_ca == "/etc/beaconmcp/pve-root-ca.pem"
    assert reloaded.ssh.strict_host_key_checking == "false"
    assert render_yaml(reloaded) == yaml_text
