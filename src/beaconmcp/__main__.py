import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# load_dotenv MUST run before importing server, because server.py
# triggers Config.from_env() at import time
load_dotenv()


def _apply_legacy_env_shim() -> None:
    """Propagate deprecated TARKAMCP_* env vars to their BEACONMCP_* counterparts.

    Removed in the next minor release (2.1.0). Emits a one-line stderr warning
    the first time a legacy variable is seen so existing deployments keep
    working through one upgrade cycle.
    """
    legacy = [k for k in os.environ if k.startswith("TARKAMCP_")]
    if not legacy:
        return
    for key in legacy:
        new_key = "BEACONMCP_" + key[len("TARKAMCP_"):]
        if new_key not in os.environ:
            os.environ[new_key] = os.environ[key]
    print(
        f"DeprecationWarning: TARKAMCP_* environment variables are deprecated "
        f"(found: {', '.join(sorted(legacy))}). Rename to BEACONMCP_*; the "
        f"legacy names will be removed in 2.1.",
        file=sys.stderr,
    )


_apply_legacy_env_shim()


def main():
    parser = argparse.ArgumentParser(description="BeaconMCP - Proxmox MCP Server")
    sub = parser.add_subparsers(dest="command")

    # --- serve (default) ---
    serve_parser = sub.add_parser("serve", help="Start the MCP HTTP server")
    serve_parser.add_argument(
        "--port", type=int, default=int(os.environ.get("BEACONMCP_PORT", "8420")),
        help="HTTP port (default: 8420)",
    )
    serve_parser.add_argument(
        "--host", default=os.environ.get("BEACONMCP_HOST", "0.0.0.0"),
        help="HTTP bind address (default: 0.0.0.0)",
    )

    # --- validate-config ---
    validate_parser = sub.add_parser(
        "validate-config", help="Parse and print the resolved config (secrets redacted)"
    )
    validate_parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to beaconmcp.yaml (overrides BEACONMCP_CONFIG and the default search)",
    )

    # --- init (interactive TUI wizard) ---
    init_parser = sub.add_parser(
        "init",
        help="Interactive TUI to create or edit beaconmcp.yaml (needs 'beaconmcp[wizard]')",
    )
    init_parser.add_argument(
        "--config", type=Path, default=None,
        help="YAML path to create or edit (default: ./beaconmcp.yaml)",
    )
    init_parser.add_argument(
        "--env", type=Path, default=Path(".env"),
        help="Path to .env where referenced ${VAR} names are appended",
    )
    init_parser.add_argument(
        "--blank", action="store_true",
        help="Start from an empty draft even if the YAML already exists "
             "(the existing file is only overwritten when you save)",
    )

    # --- auth ---
    auth_parser = sub.add_parser("auth", help="Manage OAuth client credentials")
    auth_sub = auth_parser.add_subparsers(dest="auth_command")

    create_parser = auth_sub.add_parser("create", help="Create a new client")
    create_parser.add_argument("--name", required=True, help="Client name (e.g. 'Claude Web', 'My iPhone')")
    create_parser.add_argument("--clients-file", type=Path, default=None, help="Path to clients.json")

    list_parser = auth_sub.add_parser("list", help="List all clients")
    list_parser.add_argument("--clients-file", type=Path, default=None, help="Path to clients.json")

    revoke_parser = auth_sub.add_parser("revoke", help="Revoke a client")
    revoke_parser.add_argument("client_id", help="Client ID to revoke")
    revoke_parser.add_argument("--clients-file", type=Path, default=None, help="Path to clients.json")

    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        _cmd_serve(args)
    elif args.command == "auth":
        _cmd_auth(args)
    elif args.command == "validate-config":
        _cmd_validate_config(args)
    elif args.command == "init":
        _cmd_init(args)


def _cmd_init(args):
    from .wizard import run_wizard

    yaml_path = args.config if args.config else Path(os.environ.get("BEACONMCP_CONFIG", "beaconmcp.yaml"))
    env_path = args.env
    sys.exit(run_wizard(yaml_path=yaml_path, env_path=env_path, start_blank=args.blank))


def _cmd_validate_config(args):
    from .config import Config, ConfigError

    import yaml

    try:
        cfg = Config.load(config_path=args.config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(yaml.safe_dump(cfg.redacted(), sort_keys=False, allow_unicode=True))
    print(
        f"OK: loaded {len(cfg.pve_nodes)} Proxmox node(s), "
        f"{len(cfg.bmc_devices)} BMC device(s), "
        f"SSH {'enabled' if cfg.ssh else 'disabled'}, "
        f"dashboard {'enabled' if cfg.features.dashboard.enabled else 'disabled'}.",
        file=sys.stderr,
    )


def _cmd_serve(args):
    from .server import config, mcp

    cli_host = getattr(args, "host", None)
    cli_port = getattr(args, "port", None)
    host = cli_host or os.environ.get("BEACONMCP_HOST") or config.server.host
    port_raw = cli_port or os.environ.get("BEACONMCP_PORT") or config.server.port
    _run_http(mcp, host, int(port_raw))


def _cmd_auth(args):
    from .auth import ClientStore

    store = ClientStore(getattr(args, "clients_file", None))

    if args.auth_command == "create":
        import pyotp
        import qrcode

        client_id, client_secret, totp_secret = store.create(args.name)
        provisioning_uri = pyotp.TOTP(totp_secret).provisioning_uri(
            name=client_id, issuer_name="BeaconMCP"
        )
        qr = qrcode.QRCode(border=1)
        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        print()
        print("  Client created.")
        print()
        print(f"  Name:          {args.name}")
        print(f"  Client ID:     {client_id}")
        print(f"  Client Secret: {client_secret}")
        print()
        print("  --- 2FA / Authenticator app ---")
        print("  Scan this QR code with your authenticator (Google Authenticator, Authy, 1Password, ...):")
        print()
        qr.print_ascii(invert=True)
        print()
        print(f"  Manual seed (if the scan fails) : {totp_secret}")
        print(f"  otpauth URI                     : {provisioning_uri}")
        print()
        print("  The Client Secret and the TOTP seed are NOT shown again.")
        print("  Save them now — otherwise you will have to recreate the client.")
        print()

    elif args.auth_command == "list":
        clients = store.list_clients()
        if not clients:
            print("No clients registered.")
            return
        print(f"\n{'Client ID':<30} {'Name':<25} {'Created'}")
        print("-" * 75)
        from datetime import datetime
        for c in clients:
            created = datetime.fromtimestamp(c["created_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"{c['client_id']:<30} {c['name']:<25} {created}")
        print()

    elif args.auth_command == "revoke":
        if store.revoke(args.client_id):
            print(f"Client {args.client_id} revoked.")
        else:
            print(f"Client {args.client_id} not found.")
            sys.exit(1)

    else:
        print("Usage: beaconmcp auth {create|list|revoke}")
        sys.exit(1)


def _run_http(mcp, host: str, port: int):
    """Run the MCP server over Streamable HTTP with OAuth client credentials."""
    import html
    import time
    import uvicorn
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Mount, Route

    from urllib.parse import urlencode, urlparse

    from . import auth
    from .auth import ClientStore, CodeStore, TokenStore, current_bearer_token
    from .server import config

    env_cf = os.environ.get("BEACONMCP_CLIENTS_FILE")
    clients_path = Path(env_cf) if env_cf else config.server.clients_file
    client_store = ClientStore(clients_path)
    token_store = TokenStore()
    code_store = CodeStore()
    # Share the TokenStore with MCP tools so security_end_session can revoke
    # the caller's bearer without an import cycle.
    auth.register_token_store(token_store)

    # In-memory TOTP bruteforce guard: per client_id, (failures, cooldown_until).
    # 5 failed attempts → 5-minute lockout. Reset on first success.
    totp_fail_max = 5
    totp_lockout_seconds = 300
    totp_failures: dict[str, tuple[int, float]] = {}

    def totp_locked(client_id: str) -> bool:
        entry = totp_failures.get(client_id)
        if not entry:
            return False
        count, until = entry
        if count < totp_fail_max:
            return False
        if time.time() >= until:
            totp_failures.pop(client_id, None)
            return False
        return True

    def totp_record_failure(client_id: str) -> None:
        count, _ = totp_failures.get(client_id, (0, 0.0))
        count += 1
        totp_failures[client_id] = (count, time.time() + totp_lockout_seconds)

    def totp_record_success(client_id: str) -> None:
        totp_failures.pop(client_id, None)

    def _issuer(request: Request) -> str:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host_header = request.headers.get(
            "x-forwarded-host", request.headers.get("host", "localhost")
        )
        return f"{scheme}://{host_header}"

    async def oauth_metadata(request: Request) -> Response:
        issuer = _issuer(request)
        # registration_endpoint is intentionally omitted: dynamic client
        # registration is disabled, clients must be provisioned via CLI.
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        })

    async def protected_resource_metadata(request: Request) -> Response:
        # RFC 9728 - required by the MCP 2025-06-18 spec so that clients
        # (Claude Web in particular) can discover which authorization server
        # protects the /mcp resource. We act as our own authorization server.
        issuer = _issuer(request)
        return JSONResponse({
            "resource": f"{issuer}/mcp",
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
        })

    def _validate_authorize_params(
        params: dict[str, str],
    ) -> tuple[dict[str, str], Response | None]:
        """Validate the standard OAuth2 authorize parameters.

        Returns ``(normalized, error_response)``. If ``error_response`` is not
        None it must be returned directly without redirecting, because until
        client_id and redirect_uri are trusted we cannot send the user back
        to an attacker-controlled URL (OAuth 2.1 §4.1.2.1).
        """
        response_type = params.get("response_type", "")
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")

        if response_type != "code":
            return {}, JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        if not client_id or not client_store.exists(client_id):
            return {}, JSONResponse({"error": "unauthorized_client"}, status_code=400)
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            return {}, JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri must be an absolute URL"},
                status_code=400,
            )
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            return {}, JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri must use https"},
                status_code=400,
            )
        # Reject any redirect_uri whose origin isn't on the trusted
        # allowlist. Prevents authorization-code exfiltration via a
        # typo-squat or attacker-controlled client that somehow got a
        # valid client_id.
        if not auth.is_trusted_redirect_uri(redirect_uri):
            return {}, JSONResponse(
                {"error": "invalid_request",
                 "error_description": (
                     "redirect_uri origin not on the BeaconMCP trusted-"
                     "origin allowlist; see auth.TRUSTED_REDIRECT_PREFIXES"
                 )},
                status_code=400,
            )
        if not code_challenge or code_challenge_method != "S256":
            return {}, JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE with S256 is required"},
                status_code=400,
            )

        return (
            {
                "response_type": response_type,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            },
            None,
        )

    def _render_authorize_form(
        normalized: dict[str, str], error: str | None = None, locked: bool = False
    ) -> HTMLResponse:
        client_name = client_store.get_name(normalized["client_id"]) or normalized["client_id"]
        hidden = "\n".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in normalized.items()
        )

        banner = ""
        if locked:
            banner = (
                '<div class="banner banner-error">Too many attempts. '
                "Try again in 5 minutes.</div>"
            )
        elif error:
            banner = f'<div class="banner banner-error">{html.escape(error)}</div>'

        disabled = "disabled" if locked else ""
        # Self-contained page: /oauth/authorize is reachable even when the
        # dashboard (and its /app/static bundle) is disabled. Styles/JS live
        # inline; same design tokens as the dashboard auth pages.
        page = f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BeaconMCP · Two-factor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --accent: oklch(0.68 0.17 48);
  --accent-soft: oklch(0.68 0.17 48 / 0.12);
  --accent-softer: oklch(0.68 0.17 48 / 0.06);
  --accent-border: oklch(0.68 0.17 48 / 0.35);
  --accent-hover: oklch(0.62 0.18 48);
  --accent-fg: #fff;
  --bg: oklch(0.99 0.004 70);
  --bg-soft: oklch(0.975 0.005 70);
  --bg-elev: #fff;
  --fg: oklch(0.22 0.01 70);
  --fg-mid: oklch(0.42 0.008 70);
  --fg-muted: oklch(0.55 0.008 70);
  --fg-faint: oklch(0.7 0.006 70);
  --border: oklch(0.92 0.006 70);
  --border-strong: oklch(0.86 0.008 70);
  --border-subtle: oklch(0.95 0.005 70);
  --danger: oklch(0.58 0.19 25);
  --danger-soft: oklch(0.58 0.19 25 / 0.1);
  --shadow: 0 1px 2px rgba(20,14,8,0.04), 0 4px 20px rgba(20,14,8,0.06);
  --font: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
}}
[data-theme="dark"] {{
  --bg: oklch(0.16 0.008 60);
  --bg-soft: oklch(0.19 0.008 60);
  --bg-elev: oklch(0.21 0.009 60);
  --fg: oklch(0.95 0.006 70);
  --fg-mid: oklch(0.78 0.008 70);
  --fg-muted: oklch(0.62 0.01 70);
  --fg-faint: oklch(0.45 0.008 70);
  --border: oklch(0.28 0.009 60);
  --border-strong: oklch(0.36 0.01 60);
  --border-subtle: oklch(0.24 0.008 60);
  --accent: oklch(0.75 0.17 50);
  --accent-soft: oklch(0.75 0.17 50 / 0.16);
  --accent-softer: oklch(0.75 0.17 50 / 0.08);
  --accent-border: oklch(0.75 0.17 50 / 0.4);
  --accent-hover: oklch(0.82 0.17 50);
  --accent-fg: oklch(0.12 0.008 60);
  --danger: oklch(0.68 0.19 25);
  --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 4px 20px rgba(0,0,0,0.4);
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0;
  font-family: var(--font);
  font-size: 15px;
  color: var(--fg);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}}
body {{
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}}
.auth-card {{
  width: 100%; max-width: 380px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 32px;
  box-shadow: var(--shadow);
  animation: rise 400ms var(--ease-out) both;
}}
@keyframes rise {{
  from {{ opacity: 0; transform: translateY(6px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
.auth-brand {{ display: flex; align-items: center; margin-bottom: 26px; }}
.auth-brand .name {{ font-weight: 600; font-size: 15px; letter-spacing: -0.01em; }}
h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 600; letter-spacing: -0.015em; }}
.sub {{ margin: 0 0 18px; font-size: 13.5px; color: var(--fg-muted); }}
.sub strong {{ color: var(--fg); font-weight: 600; }}
.banner {{
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 13px;
  margin: 0 0 14px;
  background: var(--danger-soft);
  color: var(--danger);
  border: 1px solid color-mix(in oklab, var(--danger) 35%, var(--border));
}}
.toast-banner {{
  background: var(--accent-softer);
  border: 1px solid var(--accent-border);
  color: var(--fg);
  border-radius: 10px;
  padding: 9px 12px;
  font-size: 12.5px;
  margin-bottom: 16px;
  display: flex; align-items: center; gap: 8px;
}}
.toast-banner .dot {{
  width: 6px; height: 6px;
  border-radius: 50%; background: var(--accent);
  flex-shrink: 0;
}}
.toast-banner b {{ font-family: var(--font-mono); margin-left: 2px; }}
.totp-inputs {{
  display: flex; gap: 8px; justify-content: space-between;
  margin: 8px 0 18px;
}}
.totp-inputs input {{
  width: 100%; aspect-ratio: 1 / 1.15;
  text-align: center;
  font-size: 24px; font-weight: 600;
  font-family: var(--font-mono);
  background: var(--bg-soft);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  color: var(--fg);
  outline: none;
  transition: border-color 160ms var(--ease-out), box-shadow 160ms var(--ease-out);
}}
.totp-inputs input:focus {{
  border-color: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}}
.totp-inputs input.filled {{
  background: var(--accent-softer);
  border-color: var(--accent-border);
}}
.btn-primary {{
  width: 100%;
  padding: 12px 16px;
  border: 0; border-radius: 10px;
  background: var(--accent); color: var(--accent-fg);
  font-family: var(--font); font-weight: 600; font-size: 14.5px;
  cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  transition: background 180ms var(--ease-out), transform 100ms var(--ease-out);
  box-shadow: 0 4px 14px oklch(0.68 0.17 48 / 0.28), inset 0 1px 0 rgba(255,255,255,0.2);
}}
.btn-primary:hover:not(:disabled) {{ background: var(--accent-hover); }}
.btn-primary:active:not(:disabled) {{ transform: translateY(1px); }}
.btn-primary:disabled {{ opacity: 0.6; cursor: not-allowed; }}
</style>
<script>
(function() {{
  try {{
    var raw = localStorage.getItem("beaconmcp-ui-state");
    var s = raw ? JSON.parse(raw) : {{}};
    var t = s.theme || "auto";
    var dark = t === "dark" || (t === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  }} catch (e) {{}}
}})();
</script>
</head>
<body>
<div class="auth-card">
  <div class="auth-brand"><span class="name">BeaconMCP</span></div>
  <h1>Authorize access</h1>
  <p class="sub">Enter the 6-digit code from your authenticator to grant access to <strong>{html.escape(client_name)}</strong>.</p>
  {banner}
  <div class="toast-banner">
    <span class="dot"></span>
    Client: <b>{html.escape(normalized["client_id"])}</b>
  </div>
  <form method="POST" action="/oauth/authorize" id="authorize-form">
{hidden}
    <div class="totp-inputs" id="totp-inputs">
      <input maxlength="1" inputmode="numeric" aria-label="Digit 1" {disabled}>
      <input maxlength="1" inputmode="numeric" aria-label="Digit 2" {disabled}>
      <input maxlength="1" inputmode="numeric" aria-label="Digit 3" {disabled}>
      <input maxlength="1" inputmode="numeric" aria-label="Digit 4" {disabled}>
      <input maxlength="1" inputmode="numeric" aria-label="Digit 5" {disabled}>
      <input maxlength="1" inputmode="numeric" aria-label="Digit 6" {disabled}>
    </div>
    <input type="hidden" name="totp" id="totp" pattern="\\d{{6}}" required>
    <button type="submit" class="btn-primary" id="verify-btn" {disabled} disabled>
      Verify and authorize
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>
    </button>
  </form>
</div>
<script>
(function() {{
  var container = document.getElementById("totp-inputs");
  if (!container) return;
  var form = container.closest("form");
  var totpHidden = document.getElementById("totp");
  var verifyBtn = document.getElementById("verify-btn");
  var inputs = container.querySelectorAll("input");
  if (!form || !totpHidden || !verifyBtn || !inputs.length) return;

  function collectTotp() {{
    var s = "";
    inputs.forEach(function(i) {{ s += (i.value || "").replace(/\\D/g, ""); }});
    return s;
  }}
  function refresh() {{
    var v = collectTotp();
    totpHidden.value = v;
    verifyBtn.disabled = v.length !== 6;
  }}

  inputs.forEach(function(inp, i) {{
    inp.addEventListener("input", function(e) {{
      var v = (e.target.value || "").replace(/\\D/g, "");
      e.target.value = v.slice(0, 1);
      if (v) {{
        e.target.classList.add("filled");
        if (inputs[i + 1]) inputs[i + 1].focus();
      }} else {{
        e.target.classList.remove("filled");
      }}
      refresh();
    }});
    inp.addEventListener("keydown", function(e) {{
      if (e.key === "Backspace" && !e.target.value && inputs[i - 1]) {{
        inputs[i - 1].focus();
        inputs[i - 1].value = "";
        inputs[i - 1].classList.remove("filled");
        refresh();
      }}
    }});
    inp.addEventListener("paste", function(e) {{
      e.preventDefault();
      var src = e.clipboardData || window.clipboardData;
      var pasted = ((src && src.getData("text")) || "").replace(/\\D/g, "").slice(0, 6);
      pasted.split("").forEach(function(ch, k) {{
        if (inputs[k]) {{ inputs[k].value = ch; inputs[k].classList.add("filled"); }}
      }});
      if (inputs[Math.min(pasted.length, 5)]) inputs[Math.min(pasted.length, 5)].focus();
      refresh();
    }});
  }});

  if (inputs[0] && !inputs[0].disabled) inputs[0].focus();
}})();
</script>
</body>
</html>
"""
        return HTMLResponse(page)

    async def oauth_authorize_get(request: Request) -> Response:
        normalized, err = _validate_authorize_params(dict(request.query_params))
        if err is not None:
            return err
        return _render_authorize_form(
            normalized, locked=totp_locked(normalized["client_id"])
        )

    async def oauth_authorize_post(request: Request) -> Response:
        form = await request.form()
        body = {k: v for k, v in form.items() if isinstance(v, str)}
        normalized, err = _validate_authorize_params(body)
        if err is not None:
            return err

        client_id = normalized["client_id"]
        if totp_locked(client_id):
            return _render_authorize_form(normalized, locked=True)

        code_totp = body.get("totp", "")
        if not client_store.verify_totp(client_id, code_totp):
            totp_record_failure(client_id)
            return _render_authorize_form(
                normalized,
                error="Incorrect code. Check that your device clock is in sync.",
                locked=totp_locked(client_id),
            )
        totp_record_success(client_id)

        redirect_uri = normalized["redirect_uri"]
        code = code_store.issue(
            client_id,
            redirect_uri,
            normalized["code_challenge"],
            normalized["code_challenge_method"],
        )
        query = {"code": code}
        if normalized["state"]:
            query["state"] = normalized["state"]
        parsed = urlparse(redirect_uri)
        sep = "&" if parsed.query else "?"
        location = f"{redirect_uri}{sep}{urlencode(query)}"
        return Response(status_code=302, headers={"Location": location})

    async def oauth_token(request: Request) -> Response:
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                raw = await request.json()
                body: dict[str, str] = {k: v for k, v in raw.items() if isinstance(v, str)}
            else:
                form = await request.form()
                body = {k: v for k, v in form.items() if isinstance(v, str)}
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        grant_type = body.get("grant_type", "")
        client_id = body.get("client_id", "")
        client_secret = body.get("client_secret", "")

        if not client_store.verify(client_id, client_secret):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        if grant_type == "client_credentials":
            # 2FA mandatory on every client_credentials exchange (design choice:
            # no non-interactive escape hatch, the operator must re-type a TOTP
            # code at every 24 h token refresh).
            if totp_locked(client_id):
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "too many failed TOTP attempts, retry later",
                    },
                    status_code=400,
                )
            code_totp = body.get("totp", "")
            if not client_store.verify_totp(client_id, code_totp):
                totp_record_failure(client_id)
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "missing or invalid totp",
                    },
                    status_code=400,
                )
            totp_record_success(client_id)
            token, expires_in = token_store.issue(client_id)
            return JSONResponse({
                "access_token": token,
                "token_type": "bearer",
                "expires_in": expires_in,
            })

        if grant_type == "authorization_code":
            code = body.get("code", "")
            redirect_uri = body.get("redirect_uri", "")
            code_verifier = body.get("code_verifier", "")
            if not code_store.consume(code, client_id, redirect_uri, code_verifier):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            token, expires_in = token_store.issue(client_id)
            return JSONResponse({
                "access_token": token,
                "token_type": "bearer",
                "expires_in": expires_in,
            })

        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def oauth_register(_request: Request) -> Response:
        # Dynamic client registration is disabled by design. Respond explicitly
        # instead of letting the request fall through to a generic 404.
        return JSONResponse(
            {
                "error": "registration_not_supported",
                "error_description": "Dynamic client registration is disabled. Ask the administrator to provision a client via `beaconmcp auth create`.",
            },
            status_code=403,
        )

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "server": "beaconmcp"})

    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in (
            "/",
            "/health",
            "/oauth/token",
            "/oauth/authorize",
            "/oauth/register",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            return await call_next(request)

        # Slug-scoped DCR endpoints are public by design — the slug is the
        # capability. The downstream handler rejects unknown/expired slugs.
        if (
            path.startswith("/.well-known/oauth-protected-resource/mcp/c/")
            or path.startswith("/.well-known/oauth-authorization-server/as/")
            or path.startswith("/oauth/register/c/")
        ):
            return await call_next(request)

        # Dashboard routes have their own session-based auth.
        if path.startswith("/app/"):
            return await call_next(request)

        # MCP 2025-06-18 + RFC 9728: point unauth'd clients at the resource
        # metadata so they can discover the authorization server. For slug-
        # scoped URLs (/mcp/c/<slug>) we point at the matching slug-scoped
        # metadata so ChatGPT's DCR flow lands on /oauth/register/c/<slug>
        # instead of the disabled global /oauth/register.
        issuer = _issuer(request)
        if path.startswith("/mcp/c/") and dyn_reg_store is not None:
            resource_meta = (
                f"{issuer}/.well-known/oauth-protected-resource{path}"
            )
        else:
            resource_meta = f"{issuer}/.well-known/oauth-protected-resource"

        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="beaconmcp", resource_metadata="{resource_meta}"',
                },
            )

        bearer = authorization[7:]
        client_id = token_store.validate(bearer)
        if not client_id:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="beaconmcp", error="invalid_token", resource_metadata="{resource_meta}"',
                },
            )

        # Expose the bearer to downstream MCP tools via ContextVar so
        # security_end_session can revoke it after responding.
        token_var = current_bearer_token.set(bearer)
        try:
            return await call_next(request)
        finally:
            current_bearer_token.reset(token_var)

    # OAuth Dynamic Client Registration plumbing. Only engaged when both
    # the feature flag is set AND the dashboard is enabled (the slug store
    # lives in the dashboard's SQLite db).
    from . import dashboard as _dashboard_mod
    dyn_reg_store = None
    shared_database = None
    if config.server.allow_dynamic_registration:
        if not _dashboard_mod.is_enabled():
            print(
                "ERROR: server.allow_dynamic_registration requires the dashboard "
                "(BEACONMCP_DASHBOARD_ENABLED=true). DCR state lives in the "
                "dashboard's database.",
                file=sys.stderr,
            )
            sys.exit(1)
        from .dashboard.db import Database as _Database
        from .dashboard.dyn_reg import DynamicSlugStore as _DynamicSlugStore
        shared_database = _Database()
        dyn_reg_store = _DynamicSlugStore(shared_database)

    async def dcr_protected_resource_metadata(request: Request) -> Response:
        # RFC 9728 resource metadata served at the slug-scoped path so
        # ChatGPT (which uses the pasted URL as the resource) discovers
        # the right authorization server. The issuer URL is also slug-
        # scoped so the AS metadata can advertise a slug-specific
        # registration_endpoint.
        slug = request.path_params["slug"]
        issuer = _issuer(request)
        resource = f"{issuer}/mcp/c/{slug}"
        return JSONResponse({
            "resource": resource,
            "authorization_servers": [f"{issuer}/as/{slug}"],
            "bearer_methods_supported": ["header"],
        })

    async def dcr_oauth_metadata(request: Request) -> Response:
        slug = request.path_params["slug"]
        issuer = _issuer(request)
        return JSONResponse({
            "issuer": f"{issuer}/as/{slug}",
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register/c/{slug}",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        })

    async def dcr_register(request: Request) -> Response:
        if dyn_reg_store is None:
            return JSONResponse({"error": "registration_not_supported"}, status_code=403)
        slug = request.path_params["slug"]
        row = dyn_reg_store.load(slug)
        if row is None:
            return JSONResponse(
                {"error": "invalid_client_metadata",
                 "error_description": "unknown bootstrap slug"},
                status_code=404,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        client_name = "ChatGPT connector"
        if isinstance(body, dict):
            candidate = body.get("client_name")
            if isinstance(candidate, str) and candidate.strip():
                client_name = candidate.strip()[:60]

        # Validate redirect_uris BEFORE provisioning a client. The MCP spec
        # lets the caller propose arbitrary redirect URIs during DCR; if we
        # accepted them blindly, a rogue script could register itself with
        # redirect_uri=https://evil.example/cb and later phish an
        # authorization code out of us. Reject anything not on the trusted
        # allowlist (auth.TRUSTED_REDIRECT_PREFIXES).
        redirect_uris_raw = None
        if isinstance(body, dict):
            redirect_uris_raw = body.get("redirect_uris")
        if not isinstance(redirect_uris_raw, list) or not redirect_uris_raw:
            return JSONResponse(
                {"error": "invalid_redirect_uri",
                 "error_description": "redirect_uris is required"},
                status_code=400,
            )
        bad = [u for u in redirect_uris_raw
               if not auth.is_trusted_redirect_uri(u)]
        if bad:
            return JSONResponse(
                {"error": "invalid_redirect_uri",
                 "error_description": (
                     "one or more redirect_uris are not on the BeaconMCP "
                     "trusted-origin allowlist"
                 ),
                 "rejected_redirect_uris": bad},
                status_code=400,
            )

        try:
            new_client_id, new_client_secret = client_store.create_dynamic(
                owner_client_id=row.owner_client_id,
                name=f"{row.label} ({client_name})"[:120],
                registration_source=f"chatgpt:{slug}",
            )
        except ValueError:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

        try:
            dyn_reg_store.consume(slug, new_client_id)
        except Exception:
            # Lost the race or slug expired between load + consume. Roll
            # back the just-created client to keep state consistent.
            client_store.revoke(new_client_id)
            return JSONResponse(
                {"error": "invalid_client_metadata",
                 "error_description": "bootstrap slug already used"},
                status_code=409,
            )

        # RFC 7591 response. We advertise only the grant and methods we
        # actually honor; clients that expected client_credentials here
        # should not be using DCR.
        return JSONResponse({
            "client_id": new_client_id,
            "client_secret": new_client_secret,
            "client_id_issued_at": int(row.created_at),
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "redirect_uris": redirect_uris_raw,
        }, status_code=201)

    class _McpSlugRewriteApp:
        """ASGI shim that strips ``/mcp/c/<slug>`` down to ``/mcp`` before
        handing off to the real MCP app. The slug serves only as a URL
        alias for clients (ChatGPT) that pasted the bootstrap URL and
        have no reason to call a different path after DCR."""

        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                path = scope.get("path", "")
                if path.startswith("/mcp/c/"):
                    remainder = path[len("/mcp/c/"):]
                    # Drop the slug segment itself; keep whatever follows.
                    slash = remainder.find("/")
                    suffix = remainder[slash:] if slash >= 0 else ""
                    new_path = "/mcp" + suffix
                    scope = dict(scope)
                    scope["path"] = new_path
                    raw = scope.get("raw_path")
                    if isinstance(raw, bytes):
                        scope["raw_path"] = new_path.encode("ascii")
            await self._inner(scope, receive, send)

    mcp_app = _McpSlugRewriteApp(mcp.streamable_http_app())

    # The MCP streamable-HTTP app starts its session manager task group in its
    # own lifespan. When we Mount it under a parent Starlette, only the parent
    # app's lifespan runs — so we forward the child's lifespan explicitly,
    # otherwise requests fail with "Task group is not initialized".
    inner_mcp = mcp_app._inner
    @asynccontextmanager
    async def lifespan(_app):
        async with inner_mcp.router.lifespan_context(_app):
            yield

    dcr_routes: list = []
    if dyn_reg_store is not None:
        dcr_routes = [
            Route(
                "/.well-known/oauth-protected-resource/mcp/c/{slug}",
                dcr_protected_resource_metadata,
            ),
            Route(
                "/.well-known/oauth-authorization-server/as/{slug}",
                dcr_oauth_metadata,
            ),
            Route("/oauth/register/c/{slug}", dcr_register, methods=["POST"]),
        ]

    # Optional dashboard routes (login + chat panels at /app/*).
    dashboard_routes = _build_dashboard_routes(
        client_store, token_store, totp_locked,
        totp_record_failure, totp_record_success,
        dyn_reg=dyn_reg_store, shared_database=shared_database,
    )

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/.well-known/oauth-authorization-server", oauth_metadata),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata),
            Route("/oauth/authorize", oauth_authorize_get, methods=["GET"]),
            Route("/oauth/authorize", oauth_authorize_post, methods=["POST"]),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Route("/oauth/register", oauth_register, methods=["POST"]),
            *dcr_routes,
            *dashboard_routes,
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=auth_middleware)],
        lifespan=lifespan,
    )

    n_clients = len(client_store.list_clients())
    print(f"BeaconMCP starting on {host}:{port}")
    print(f"Clients: {n_clients}")
    print(f"MCP:       http://{host}:{port}/mcp")
    print(f"Authorize: http://{host}:{port}/oauth/authorize")
    print(f"Token:     http://{host}:{port}/oauth/token")
    print(f"Health:    http://{host}:{port}/health")
    if dashboard_routes:
        chat_status = "enabled" if os.environ.get("GEMINI_API_KEY") else "disabled, tokens only"
        print(f"Dashboard: http://{host}:{port}/app/login (chat: {chat_status})")
    else:
        print("Dashboard: disabled (BEACONMCP_DASHBOARD_ENABLED=false)")
    if n_clients == 0:
        print("\nNo clients registered. Create one with: beaconmcp auth create --name 'My Client'")
    uvicorn.run(app, host=host, port=port, log_level="info")


def _build_dashboard_routes(client_store, token_store, totp_locked,
                             totp_record_failure, totp_record_success,
                             *, dyn_reg=None, shared_database=None):
    """Build dashboard routes if enabled. Returns [] when disabled."""
    from . import dashboard
    if not dashboard.is_enabled():
        return []
    from .dashboard.app import DashboardDeps, build_dashboard_routes
    from .dashboard.chat import GeminiChatEngine
    from .dashboard.confirmations import ConfirmationStore
    from .dashboard.conversations import ConversationStore
    from .dashboard.db import Database
    from .dashboard.session import SessionStore
    from .dashboard.usage import Budget, UsageStore

    database = shared_database if shared_database is not None else Database()
    session_store = SessionStore(database)
    conversations = ConversationStore(database)
    confirmations = ConfirmationStore()

    # Usage accounting. Both caps are applied globally to every client.
    # Setting a cap to 0 (or leaving the var unset and letting the float
    # parse to 0) disables enforcement on that window. Defaults follow
    # the decision captured in docs/superpowers/specs: $2 / 5h, $10 / week.
    def _float_env(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            print(
                f"WARNING: {name}={raw!r} is not a valid float; "
                f"using default {default}.",
                file=sys.stderr,
            )
            return default

    budget = Budget(
        limit_5h_usd=_float_env("BEACONMCP_DASHBOARD_LIMIT_5H_USD", 2.0),
        limit_week_usd=_float_env("BEACONMCP_DASHBOARD_LIMIT_WEEK_USD", 10.0),
    )
    usage = UsageStore(database, budget)

    api_key = os.environ.get("GEMINI_API_KEY", "")
    engine = GeminiChatEngine(api_key=api_key) if api_key else None
    mcp_public_url = os.environ.get("BEACONMCP_DASHBOARD_PUBLIC_URL", "").strip() or None
    mcp_mode = os.environ.get("BEACONMCP_DASHBOARD_MCP_MODE", "local").strip().lower()
    if mcp_mode not in ("local", "remote"):
        mcp_mode = "local"
    if mcp_mode == "remote":
        print(
            "WARNING: BEACONMCP_DASHBOARD_MCP_MODE=remote is unsupported "
            "(caused 500 INTERNAL on Gemini 3). Chat turns will error out "
            "with a helpful message until you remove the variable.",
            file=sys.stderr,
        )

    deps = DashboardDeps(
        database=database,
        session_store=session_store,
        client_store=client_store,
        token_store=token_store,
        totp_locked=totp_locked,
        totp_record_failure=totp_record_failure,
        totp_record_success=totp_record_success,
        conversations=conversations,
        engine=engine,
        confirmations=confirmations,
        usage=usage,
        mcp_public_url=mcp_public_url,
        mcp_mode=mcp_mode,
        dyn_reg=dyn_reg,
    )
    return build_dashboard_routes(deps)


if __name__ == "__main__":
    main()
