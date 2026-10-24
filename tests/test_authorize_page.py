"""Remastered /oauth/authorize page (rendered directly)."""

from __future__ import annotations

from beaconmcp import oauth_ui


def _render(**kw) -> str:
    args = dict(
        host="beacon.homelab.fr", client_id="beaconmcp_7f3a", client_name="Claude Code",
        logo_url="/oauth/logo/si/claude",
        redirect_uri="http://localhost:53172/callback",
        kinds=oauth_ui.capability_kinds(proxmox=True, bmc=True, ssh=False),
        hidden_inputs="", banner="", locked=False, approved=False, passkeys_on=True,
        ticket="", access_expiry="&mdash;", ticket_expiry="&mdash;",
    )
    args.update(kw)
    return oauth_ui.render_authorize_page(**args)


def test_redirect_host_bold() -> None:
    page = _render()
    assert "http://<strong>localhost</strong>:53172/callback" in page
    assert "Redirects to" in page


def test_redirect_host_bold_with_userinfo_like_path() -> None:
    page = _render(redirect_uri="https://claude.ai/api/mcp/auth_callback?x=<y>")
    assert "https://<strong>claude.ai</strong>/api/mcp/auth_callback?x=&lt;y&gt;" in page


def test_no_session_line() -> None:
    assert "<span>Session</span>" not in _render()


def test_capability_kinds_only_no_counts() -> None:
    page = _render()
    assert "Proxmox · BMC" in page
    assert "nodes" not in page and "iLO" not in page


def test_power_copy_soft() -> None:
    page = _render()
    assert "Confirmed in your client when it supports it" in page
    assert "asks you to confirm first" not in page


def test_no_bmc_no_power_row() -> None:
    page = _render(kinds=oauth_ui.capability_kinds(proxmox=False, bmc=False, ssh=True))
    assert "Power off" not in page and "Run shell commands" in page


def test_known_client_logo_and_name() -> None:
    page = _render()
    assert 'src="/oauth/logo/si/claude"' in page and "Connect Claude Code" in page


def test_passkey_first_when_enabled() -> None:
    page = _render()
    assert page.index('id="passkey-btn"') < page.index('id="totp-inputs"')
    assert 'data-passkeys="true"' in page


def test_escapes_client_name() -> None:
    page = _render(client_name="<img src=x onerror=alert(1)>")
    assert "<img src=x onerror" not in page


def test_locked_disables_inputs() -> None:
    page = _render(locked=True)
    assert 'aria-label="Digit 1" disabled' in page


def test_approved_shows_done_step() -> None:
    page = _render(approved=True, ticket="t1")
    assert 'id="step-verify" hidden' in page and 'value="t1"' in page
