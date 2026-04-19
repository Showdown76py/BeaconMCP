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
