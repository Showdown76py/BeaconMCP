import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

# load_dotenv MUST run before importing server, because server.py
# triggers Config.from_env() at import time
load_dotenv()


def _configure_logging() -> None:
    """Wire root logging so ``logging.getLogger('beaconmcp.*')`` emits to stderr.

    Honours ``BEACONMCP_LOG_LEVEL`` (default ``INFO``). Runs once at CLI
    entry before any module-level ``_logger`` call can fire. Keeps the
    format compact so journalctl stays readable.
    """
    if not logging.getLogger().handlers:
        level_name = os.environ.get("BEACONMCP_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )


def _configure_audit_log(config_path: str | None = None) -> None:
    """Route the ``beaconmcp.audit`` logger to a dedicated JSON-lines file.

    Resolution order: ``BEACONMCP_AUDIT_LOG`` env var, then
    ``server.audit_log`` from the YAML (``config_path``), then
    ``/opt/beaconmcp/audit.log``. Each :func:`beaconmcp.audit.emit` call
    already produces one JSON object, so the handler uses a bare
    ``%(message)s`` formatter. ``propagate`` is left on so the same line
    still reaches journalctl; set the value to ``-`` to disable the file
    and keep stderr only.

    Called by the commands that emit audit events (``serve``, ``auth
    revoke``) rather than at import, so a plain ``--help`` or
    ``validate-config`` never touches /opt.
    """
    audit_logger = logging.getLogger("beaconmcp.audit")
    if any(isinstance(h, logging.FileHandler) for h in audit_logger.handlers):
        return  # already wired
    path = (
        os.environ.get("BEACONMCP_AUDIT_LOG")
        or config_path
        or "/opt/beaconmcp/audit.log"
    ).strip()
    if not path or path == "-":
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        # Client ids, source IPs and tool args land in here -- owner-only,
        # like clients.json (FileHandler creates it with the default umask).
        os.chmod(path, 0o600)
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(handler)
        audit_logger.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001 -- audit file is best-effort
        logging.getLogger("beaconmcp").warning(
            "could not open audit log at %s; auditing to stderr only", path,
        )


_configure_logging()


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
    logging.getLogger("beaconmcp").warning(
        "TARKAMCP_* environment variables are deprecated (found: %s). "
        "Rename to BEACONMCP_*; the legacy names will be removed in 2.1.",
        ", ".join(sorted(legacy)),
    )


_apply_legacy_env_shim()


_logger = logging.getLogger("beaconmcp")


# Edge header set by Cloudflare on every request it proxies. Its presence on a
# request that reached us *without* a usable Authorization header is the
# fingerprint of a Cloudflare WAF / Access / Bot-management rule stripping or
# blocking the header before it ever reached the app.
_CF_EDGE_HEADER = "cf-ray"
_CF_HINT = (
    "Request arrived via Cloudflare (cf-ray present) without an Authorization "
    "header -- a WAF/Access/Bot-Fight-Mode rule may be stripping or blocking it; "
    "see docs/cloudflare.md"
)

# A public /mcp endpoint gets scanned continuously, and every one of those
# unauthenticated hits carries a cf-ray. Logging each one hands any anonymous
# caller a journal-flooding lever, so the warning is throttled: it exists to
# tell an operator "your edge is eating the header", which one line per
# interval conveys just as well as ten thousand.
_CF_LOG_INTERVAL_SECONDS = 300.0
_cf_log_state = {"last": 0.0, "suppressed": 0}
_cf_log_lock = threading.Lock()

#: How long a stop waits for in-flight requests before cancelling them.
#: uvicorn waits forever by default, so one request that never finishes (a
#: stalled upload, a long tool call) held every restart until systemd's
#: 90 s stop timeout SIGKILLed the process, skipping the app's shutdown.
_SHUTDOWN_GRACE_SECONDS = 10


def _log_cloudflare_unauthorized(cf_ray: str) -> None:
    now = time.monotonic()
    with _cf_log_lock:
        elapsed = now - _cf_log_state["last"]
        if _cf_log_state["last"] and elapsed < _CF_LOG_INTERVAL_SECONDS:
            _cf_log_state["suppressed"] += 1
            return
        suppressed = _cf_log_state["suppressed"]
        _cf_log_state["last"] = now
        _cf_log_state["suppressed"] = 0

    _logger.warning(
        "Unauthorized MCP request proxied by Cloudflare (cf-ray=%s) with no "
        "valid Authorization header. A Cloudflare WAF/Access/Bot-Fight-Mode "
        "rule is likely stripping or blocking it. See docs/cloudflare.md.%s",
        cf_ray,
        f" ({suppressed} similar suppressed)" if suppressed else "",
    )


def _oauth_server_metadata(issuer: str) -> dict:
    """RFC 8414 authorization-server metadata for the main issuer."""
    # registration_endpoint is intentionally omitted: dynamic client
    # registration is disabled, clients must be provisioned via CLI.
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code", "refresh_token", "client_credentials",
        ],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


def _build_unauthorized_body(headers, *, error: str) -> dict:
    """Build the JSON body for a 401 on an MCP/OAuth-protected request.

    For a plain direct request (curl, a misconfigured client) the body stays
    minimal: ``{"error": "unauthorized"}``. When the request carries
    Cloudflare's ``cf-ray`` edge header but no usable bearer, that strongly
    signals an edge rule ate the ``Authorization`` header, so we enrich the
    body with a ``hint`` pointing at the Cloudflare guide and emit a throttled
    warning (the operator sees this in ``journalctl -u beaconmcp``). The 401
    status and ``WWW-Authenticate`` header are set by the caller and never
    change -- OAuth discovery depends on them.
    """
    body: dict[str, str] = {"error": error}
    cf_ray = headers.get(_CF_EDGE_HEADER)
    if cf_ray:
        body["hint"] = _CF_HINT
        _log_cloudflare_unauthorized(cf_ray)
    return body


def main():
    parser = argparse.ArgumentParser(description="BeaconMCP - Proxmox MCP Server")
    sub = parser.add_subparsers(dest="command")

    # --- serve (default) ---
    doctor_parser = sub.add_parser("doctor", help="Preflight connectivity and config check")
    doctor_parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to beaconmcp.yaml (overrides BEACONMCP_CONFIG and the default search)",
    )

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

    # --- check-startup (self-update gate) ---
    check_parser = sub.add_parser(
        "check-startup",
        help="Load the config and build the whole server without serving it "
             "(the gate a self-update must pass before restarting)",
    )
    check_parser.add_argument(
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
    create_parser.add_argument("--name", required=True, help="Client name (e.g. 'Assistant Web', 'My iPhone')")
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
    elif args.command == "doctor":
        _cmd_doctor(args)
    elif args.command == "validate-config":
        _cmd_validate_config(args)
    elif args.command == "check-startup":
        _cmd_check_startup(args)
    elif args.command == "init":
        _cmd_init(args)


def _cmd_init(args):
    from .wizard import run_wizard

    yaml_path = args.config if args.config else Path(os.environ.get("BEACONMCP_CONFIG", "beaconmcp.yaml"))
    env_path = args.env
    sys.exit(run_wizard(yaml_path=yaml_path, env_path=env_path, start_blank=args.blank))



def _cmd_doctor(args):
    import asyncio
    from .config import Config
    from .proxmox.client import ProxmoxClient
    from .ssh.client import SSHClient
    from .bmc import build_registry

    print("BeaconMCP Doctor - Preflight Check\n")
    try:
        cfg = Config.load(config_path=getattr(args, "config", None))
        print("✓ Config loaded successfully.")
    except Exception as exc:
        print(f"✗ Config failed to load: {exc}")
        sys.exit(1)

    print(f"\nProxmox Nodes ({len(cfg.pve_nodes)}):")
    if cfg.pve_nodes:
        proxmox_client = ProxmoxClient(cfg)
        for node in cfg.pve_nodes:
            try:
                status = proxmox_client.get(node.name, "version")
                if isinstance(status, dict) and "version" in status:
                    print(f"  ✓ {node.name}: Reachable (PVE {status['version']})")
                else:
                    print(f"  ✗ {node.name}: Unexpected response {status}")
            except Exception as exc:
                print(f"  ✗ {node.name}: Unreachable ({exc})")
    else:
        print("  - No Proxmox nodes configured.")

    print(f"\nSSH Hosts ({len(cfg.ssh.hosts) if cfg.ssh else 0}):")
    if cfg.ssh and cfg.ssh.hosts:
        ssh_client = SSHClient(cfg)

        async def check_ssh() -> None:
            for host in cfg.ssh.hosts:
                try:
                    result = await ssh_client.exec_command(host.name, "echo OK", timeout=5)
                    if result.get("error"):
                        print(f"  ✗ {host.name}: Failed ({result['error']})")
                    elif result.get("exit_code") == 0:
                        print(f"  ✓ {host.name}: Reachable")
                    else:
                        print(f"  ✗ {host.name}: Failed ({result.get('stderr', '').strip() or result})")
                except Exception as exc:
                    print(f"  ✗ {host.name}: Unreachable ({exc})")

        asyncio.run(check_ssh())
    else:
        print("  - No explicit SSH hosts configured.")

    print(f"\nBMC Devices ({len(cfg.bmc_devices)}):")
    if cfg.bmc_devices:
        registry = build_registry(cfg)

        async def check_bmc() -> None:
            for device in cfg.bmc_devices:
                client = registry.get(device.id)
                if not client:
                    print(f"  ✗ {device.id}: Backend not initialized")
                    continue
                try:
                    health = await client.health()
                    summary = health.get("overall_health") or health.get("health") or "reachable"
                    print(f"  ✓ {device.id}: Reachable ({summary})")
                except Exception as exc:
                    print(f"  ✗ {device.id}: Unreachable ({exc})")

        asyncio.run(check_bmc())
    else:
        print("  - No BMC devices configured.")

    # Only the library half can be checked here: credential storage depends
    # on the running server's database, which doctor does not open.
    print("\nPasskeys (WebAuthn):")
    from .dashboard.passkeys import webauthn_installed

    if webauthn_installed():
        print("  ✓ 'webauthn' package importable.")
        print("    Browsers additionally require HTTPS or a loopback host.")
    else:
        print("  ✗ 'webauthn' package is missing - login pages offer TOTP only.")
        print("    Fix with: pip install 'webauthn>=2,<4'  (or reinstall BeaconMCP)")

    print("\nPreflight check complete.")


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


def _cmd_check_startup(args):
    """Import every module and build the HTTP app against this config.

    This is the gate a self-update must pass before it restarts the
    service: ``validate-config`` only imports ``config.py``, so a broken
    import anywhere else would sail through it and take the server down.
    Nothing is served and no live state is touched: the token store, the
    dashboard database and the audit log point at a scratch directory, and
    the clients file is a copy.
    """
    import shutil
    import tempfile
    import traceback

    from .config import Config, ConfigError

    if args.config:
        os.environ["BEACONMCP_CONFIG"] = str(args.config)
    try:
        cfg = Config.load(config_path=args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="beaconmcp-check-") as tmp:
        scratch = Path(tmp)
        env_cf = os.environ.get("BEACONMCP_CLIENTS_FILE")
        clients_src = Path(env_cf) if env_cf else cfg.server.clients_file
        clients_copy = scratch / "clients.json"
        if clients_src and Path(clients_src).is_file():
            shutil.copy2(clients_src, clients_copy)
        os.environ["BEACONMCP_CLIENTS_FILE"] = str(clients_copy)
        os.environ["BEACONMCP_TOKENS_DB"] = str(scratch / "tokens.db")
        os.environ["BEACONMCP_DASHBOARD_DB"] = str(scratch / "dashboard.db")
        os.environ["BEACONMCP_AUDIT_LOG"] = "-"
        try:
            import asyncio

            import httpx

            from .server import mcp

            app = _run_http(mcp, "127.0.0.1", 0, serve=False)

            async def _probe() -> httpx.Response:
                # Run the app's lifespan too: that is where the MCP session
                # manager starts, and a failure there is a failed start.
                async with app.router.lifespan_context(app):
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://127.0.0.1",
                    ) as http:
                        return await http.get("/health")

            response = asyncio.run(_probe())
            if response.status_code != 200:
                raise RuntimeError(f"/health answered HTTP {response.status_code}")
        except Exception:  # noqa: BLE001 -- report whatever broke, then fail
            traceback.print_exc()
            print("ERROR: the server does not start with this configuration.", file=sys.stderr)
            sys.exit(1)
    print("OK: configuration loads, every module imports and /health answers.")


def _local_health_url(host: str, port: int) -> str:
    """Where a process on this machine reaches our own /health."""
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/health"


def _cmd_serve(args):
    from . import updates
    from .server import config, mcp

    _configure_audit_log(config.server.audit_log)
    cli_host = getattr(args, "host", None)
    cli_port = getattr(args, "port", None)
    host = cli_host or os.environ.get("BEACONMCP_HOST") or config.server.host
    port_raw = cli_port or os.environ.get("BEACONMCP_PORT") or config.server.port
    # Lets a self-update hand its post-restart watchdog the URL to poll.
    updates.configure(
        config.features.updates, health_url=_local_health_url(host, int(port_raw)),
    )
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
        from . import audit

        # Best-effort: pick up server.audit_log when a YAML is reachable so
        # the revoke lands in the same file as the server's events. `auth`
        # must keep working without any config (legacy env deployments).
        try:
            from .config import Config

            _configure_audit_log(Config.load().server.audit_log)
        except Exception:  # noqa: BLE001
            _configure_audit_log()
        if store.revoke(args.client_id):
            audit.emit("auth.client.revoke", client_id=args.client_id, via="cli")
            print(f"Client {args.client_id} revoked.")
        else:
            print(f"Client {args.client_id} not found.")
            sys.exit(1)

    else:
        print("Usage: beaconmcp auth {create|list|revoke}")
        sys.exit(1)


def _run_http(mcp, host: str, port: int, *, serve: bool = True):
    """Run the MCP server over Streamable HTTP with OAuth client credentials.

    ``serve=False`` builds and returns the Starlette app without printing
    the banner or binding a port (``beaconmcp check-startup``).
    """
    import datetime as _dt
    import html
    import secrets
    import uvicorn
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Mount, Route

    from urllib.parse import urlencode, urlparse

    from . import audit
    from . import auth
    from . import oauth_ui
    from .client_identity import LogoResolver, identify
    from . import transfers
    from .auth import (
        TOTP_REPLAY_MESSAGE,
        ClientStore,
        CodeStore,
        RefreshTokenError,
        TokenStore,
        TotpResult,
        current_bearer_token,
    )
    from .ratelimit import RateLimiter, TotpLockout, client_ip, forwarded_host
    from .server import config, transport_security
    # The same list the SDK enforces for CORS, including its built-in default
    # when beaconmcp.yaml leaves server.allowed_origins empty: OAuth
    # redirects to claude.ai must keep working out of the box.
    redirect_origins = list(transport_security.allowed_origins or [])

    from .metrics import REGISTRY, auth_events, http_requests

    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path

            # Group dashboard and static paths to avoid cardinality explosion
            if path.startswith("/app/api/conversations"):
                path = "/app/api/conversations"
            elif path.startswith("/app/"):
                path = "/app"
            elif path.startswith("/static/"):
                path = "/static"
            elif path.startswith(transfers.UPLOAD_PATH_PREFIX):
                # The rest of the path is a live credential.
                path = transfers.UPLOAD_PATH_PREFIX.rstrip("/")

            try:
                response = await call_next(request)
                status = str(response.status_code)
                return response
            except Exception:
                status = "500"
                raise
            finally:
                http_requests.inc(path=path, status=status)

    # Per-IP rate limit for auth-adjacent endpoints. Numbers sized so a
    # human-driven login (with a few retries) always fits, while automated
    # brute-force dies fast. TOTP lockout already guards per-client; this
    # covers the "wrong client_id" probing the TOTP guard can't see.
    _token_limiter = RateLimiter(limit=30, window_seconds=60.0)
    _login_limiter = RateLimiter(limit=10, window_seconds=60.0)

    env_cf = os.environ.get("BEACONMCP_CLIENTS_FILE")
    clients_path = Path(env_cf) if env_cf else config.server.clients_file
    client_store = ClientStore(clients_path)
    # Persist named API tokens so a restart/redeploy no longer silently
    # invalidates tokens users pasted into external clients. Resolution:
    # BEACONMCP_TOKENS_DB env var > server.tokens_db in the YAML >
    # tokens.db next to clients.json.
    env_tokens_db = os.environ.get("BEACONMCP_TOKENS_DB")
    tokens_db = (
        Path(env_tokens_db)
        if env_tokens_db
        else (config.server.tokens_db or clients_path.parent / "tokens.db")
    )
    # Named-token lifetime: BEACONMCP_NAMED_TOKEN_TTL env (seconds) >
    # server.named_token_ttl in the YAML > TokenStore default (30 days).
    env_named_ttl = os.environ.get("BEACONMCP_NAMED_TOKEN_TTL")
    named_token_ttl = (
        int(env_named_ttl)
        if env_named_ttl and env_named_ttl.isdigit()
        else config.server.named_token_ttl
    )
    # Refresh-token idle lifetime: BEACONMCP_REFRESH_TOKEN_TTL env (seconds) >
    # server.refresh_token_ttl in the YAML > TokenStore default (24 h). The
    # env path skips the YAML loader, so it gets the same bounds check here:
    # no sign-in outlives 24 h, and "0 = never" is gone.
    env_refresh_ttl = os.environ.get("BEACONMCP_REFRESH_TOKEN_TTL")
    refresh_token_ttl = (
        int(env_refresh_ttl)
        if env_refresh_ttl and env_refresh_ttl.isdigit()
        else config.server.refresh_token_ttl
    )
    if refresh_token_ttl is not None and not (
        0 < refresh_token_ttl <= TokenStore.SESSION_LIFETIME
    ):
        print(
            "ERROR: BEACONMCP_REFRESH_TOKEN_TTL must be between 1 and "
            f"{TokenStore.SESSION_LIFETIME} seconds (24 h); a sign-in never "
            "outlives 24 h and 0 (never expires) is not supported.",
            file=sys.stderr,
        )
        sys.exit(1)
    token_store = TokenStore(
        db_path=tokens_db,
        named_token_ttl=named_token_ttl,
        refresh_token_ttl=refresh_token_ttl,
    )
    # OAuth tokens now survive restarts: drop those of clients deleted while
    # the server was down. Skipped when no client loaded at all, so an
    # unreadable clients.json can't wipe the db.
    known_clients = {c["client_id"] for c in client_store.list_clients()}
    if known_clients:
        token_store.prune_unknown_clients(known_clients)
    # ...and of clients deleted while it is up: ``beaconmcp auth revoke``
    # rewrites clients.json from another process, the store notices at the
    # next lookup and hands us the survivors.
    client_store.on_clients_removed = token_store.prune_unknown_clients
    code_store = CodeStore()
    logo_resolver = LogoResolver(clients_path.parent / "logo-cache")

    # Shared dashboard SQLite handle. Three features live in it -- sessions,
    # DCR bootstrap slugs and passkeys -- and /oauth/authorize wants the
    # passkey table even on deployments that keep the dashboard off, so the
    # handle is opened here rather than inside the dashboard builder.
    from . import dashboard as _dashboard_mod
    from .dashboard import passkeys as passkeys_mod
    from .dashboard.db import Database as _Database

    shared_database = None
    _database_required = (
        _dashboard_mod.is_enabled() or config.server.allow_dynamic_registration
    )
    try:
        shared_database = _Database()
    except Exception as exc:  # noqa: BLE001
        if _database_required:
            raise
        # Dashboard off and no writable database: passkeys are simply not
        # offered. Never a reason to refuse to boot the MCP server.
        logging.getLogger("beaconmcp").warning(
            "passkey storage unavailable (%s); passkeys disabled", exc,
        )

    passkey_service = passkeys_mod.PasskeyService(
        passkeys_mod.PasskeyStore(shared_database)
        if shared_database is not None
        else None
    )
    # Share the TokenStore with MCP tools so security_end_session can revoke
    # the caller's bearer without an import cycle.
    auth.register_token_store(token_store)

    # In-memory TOTP brute-force guard, keyed on (client_id, source IP): 5
    # wrong codes from one IP lock that IP out for 5 minutes, and 10 from any
    # mix of IPs lock the client's TOTP (passkeys stay usable). Keyed on
    # client_id alone, anyone who knew a client_id could lock its owner out
    # of every 2FA path.
    totp_lockout = TotpLockout(per_source=5, per_client=10, lockout_seconds=300)

    # A derived (DCR) client checks codes against its owner's seed, so the
    # lockout is keyed on the seed owner: otherwise every derived client_id
    # would get its own guess budget against the same seed.
    def _seed_owner(client_id: str) -> str:
        client = client_store.get(client_id)
        if client is not None and client.owner_client_id:
            return client.owner_client_id
        return client_id

    def totp_locked(client_id: str, ip: str | None = None) -> bool:
        return totp_lockout.locked(_seed_owner(client_id), ip)

    def totp_record_failure(client_id: str, ip: str | None = None) -> None:
        totp_lockout.record_failure(_seed_owner(client_id), ip)

    def totp_record_success(client_id: str, ip: str | None = None) -> None:
        totp_lockout.record_success(_seed_owner(client_id), ip)

    def _ip(request: Request) -> str:
        return client_ip(request, tuple(config.server.trusted_proxies))

    def _issuer(request: Request) -> str:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        # X-Forwarded-Host is only trusted from a declared proxy; otherwise the
        # request's own Host header wins (see ratelimit.forwarded_host).
        host_header = forwarded_host(
            request, tuple(config.server.trusted_proxies),
        )
        return f"{scheme}://{host_header}"

    async def oauth_metadata(request: Request) -> Response:
        return JSONResponse(_oauth_server_metadata(_issuer(request)))

    async def protected_resource_metadata(request: Request) -> Response:
        # RFC 9728 - required by the MCP 2025-06-18 spec so that clients
        # (Assistant Web in particular) can discover which authorization server
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
        if not auth.is_trusted_redirect_uri(redirect_uri, redirect_origins):
            return {}, JSONResponse(
                {"error": "invalid_request",
                 "error_description": (
                     "redirect_uri origin not on the trusted-origin allowlist; "
                     "add it to server.allowed_origins in beaconmcp.yaml"
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

    # Approvals that cleared 2FA but have not been turned into an
    # authorization code yet. The operator sits on the confirmation screen
    # (session lifetime, optional passkey enrolment) for as long as they
    # like; the *code* is only minted when they press "Finish", so its
    # 60 s OAuth 2.1 lifetime is never burned by a human reading a page.
    #
    # In-memory and single-use on purpose: a ticket that survives a restart
    # would be a standing authorization nobody asked for.
    pending_authorizations: dict[str, tuple[dict[str, str], float]] = {}
    PENDING_AUTHORIZATION_TTL = 600.0

    def _prune_pending() -> None:
        now = time.time()
        for key in [k for k, (_, exp) in pending_authorizations.items() if now > exp]:
            del pending_authorizations[key]

    def _issue_pending(normalized: dict[str, str]) -> tuple[str, float]:
        _prune_pending()
        ticket = secrets.token_urlsafe(32)
        expires_at = time.time() + PENDING_AUTHORIZATION_TTL
        pending_authorizations[ticket] = (dict(normalized), expires_at)
        return ticket, expires_at

    def _consume_pending(ticket: str) -> dict[str, str] | None:
        _prune_pending()
        entry = pending_authorizations.pop(ticket, None)
        if entry is None:
            return None
        params, expires_at = entry
        return params if time.time() <= expires_at else None

    def _peek_pending(ticket: str) -> dict[str, str] | None:
        """Read a ticket without spending it (passkey enrolment mid-screen)."""
        _prune_pending()
        entry = pending_authorizations.get(ticket)
        if entry is None:
            return None
        params, expires_at = entry
        return params if time.time() <= expires_at else None

    def _passkey_owner(client_id: str) -> str:
        """Client whose passkeys guard ``client_id``.

        Mirrors the TOTP delegation in :meth:`ClientStore.check_totp`: a
        dynamically-registered client (ChatGPT and friends) has no second
        factor of its own, so the human owner's passkeys authorize it.
        """
        client = client_store.get(client_id)
        if client is not None and client.owner_client_id:
            return client.owner_client_id
        return client_id

    def _authorize_passkeys_enabled(request: Request, client_id: str) -> bool:
        if passkey_service is None or not passkey_service.available:
            return False
        return passkeys_mod.is_secure_context(request)

    def _wants_json_authorize(request: Request) -> bool:
        return request.headers.get("x-beaconmcp-mode", "") == "json"

    def _authorize_page(
        normalized: dict[str, str],
        *,
        request: Request,
        error: str | None = None,
        locked: bool = False,
        ticket: str | None = None,
        access_expires_at: float | None = None,
        ticket_expires_at: float | None = None,
    ) -> HTMLResponse:
        """Render the two-panel authorize page.

        ``ticket`` switches it to the post-2FA panel. That panel is rendered
        server-side too, so the flow still completes with JavaScript off:
        the confirmation screen is a plain form posting the ticket back.
        """
        client_id = normalized["client_id"]
        client_name = client_store.get_name(client_id) or client_id
        hidden = "\n".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
            for k, v in normalized.items()
        )

        banner = ""
        if locked:
            banner = (
                '<div class="banner">Too many attempts. Try again in 5 minutes.</div>'
            )
        elif error:
            banner = f'<div class="banner">{html.escape(error)}</div>'

        approved = ticket is not None
        passkeys_on = _authorize_passkeys_enabled(request, client_id)

        def _fmt(ts: float | None) -> str:
            if not ts:
                return "&mdash;"
            return html.escape(
                _dt.datetime.fromtimestamp(ts).strftime("%d %b %Y at %H:%M")
            )

        redirect_uri = normalized.get("redirect_uri", "")
        client = client_store.get(client_id)
        identity = identify(redirect_uri=redirect_uri, registered_name=client_name)
        logo_url = logo_resolver.url_for(
            identity,
            logo_uri=(client.logo_uri or "") if client else "",
            website_url=(client.client_uri or "") if client else "",
        )
        page = oauth_ui.render_authorize_page(
            host=forwarded_host(request, tuple(config.server.trusted_proxies)),
            client_id=client_id,
            client_name=identity.name if identity.key else client_name,
            logo_url=logo_url,
            redirect_uri=redirect_uri,
            kinds=oauth_ui.capability_kinds(
                proxmox=bool(config.pve_nodes),
                bmc=bool(config.bmc_devices),
                ssh=bool(config.ssh and config.ssh.hosts),
            ),
            hidden_inputs=hidden,
            banner=banner,
            locked=locked,
            approved=approved,
            passkeys_on=passkeys_on,
            ticket=ticket or "",
            access_expiry=_fmt(access_expires_at),
            ticket_expiry=_fmt(ticket_expires_at),
        )
        return HTMLResponse(page, headers=oauth_ui.SECURITY_HEADERS)

    def _authorize_redirect(normalized: dict[str, str]) -> Response:
        """Mint the authorization code and bounce back to the OAuth client."""
        redirect_uri = normalized["redirect_uri"]
        code = code_store.issue(
            normalized["client_id"],
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

    def _authorize_approved(
        request: Request, normalized: dict[str, str], *, as_json: bool = False,
    ) -> Response:
        """Second factor cleared: hand the operator the confirmation screen.

        ``as_json`` forces the JSON shape for callers that are JSON-only by
        construction (the passkey endpoints), independently of whether the
        client remembered to send the mode header.
        """
        ticket, ticket_expires_at = _issue_pending(normalized)
        access_expires_at = time.time() + TokenStore.TOKEN_TTL
        if as_json or _wants_json_authorize(request):
            return JSONResponse({
                "ok": True,
                "ticket": ticket,
                "ticket_expires_at": ticket_expires_at,
                "access_expires_at": access_expires_at,
                "passkeys_enabled": _authorize_passkeys_enabled(
                    request, normalized["client_id"],
                ),
            })
        return _authorize_page(
            normalized,
            request=request,
            ticket=ticket,
            ticket_expires_at=ticket_expires_at,
            access_expires_at=access_expires_at,
        )

    def _authorize_failed(
        request: Request,
        normalized: dict[str, str],
        message: str,
        *,
        status: int = 401,
        locked: bool = False,
    ) -> Response:
        if _wants_json_authorize(request):
            return JSONResponse(
                {"ok": False, "error": message, "locked": locked},
                status_code=status,
            )
        return _authorize_page(
            normalized, request=request, error=message, locked=locked,
        )

    async def oauth_authorize_get(request: Request) -> Response:
        normalized, err = _validate_authorize_params(dict(request.query_params))
        if err is not None:
            return err
        return _authorize_page(
            normalized,
            request=request,
            locked=totp_locked(normalized["client_id"], _ip(request)),
        )

    async def oauth_authorize_post(request: Request) -> Response:
        form = await request.form()
        body = {k: v for k, v in form.items() if isinstance(v, str)}
        normalized, err = _validate_authorize_params(body)
        if err is not None:
            return err

        # This endpoint takes a TOTP guess with nothing but a client_id, so
        # it gets the same per-IP limit as /app/login.
        ip = _ip(request)
        if not _login_limiter.check(ip):
            retry = _login_limiter.retry_after(ip)
            response = _authorize_failed(
                request, normalized,
                f"Too many attempts. Retry in {retry}s.",
                status=429,
            )
            response.headers["Retry-After"] = str(retry)
            return response

        client_id = normalized["client_id"]
        if totp_locked(client_id, ip):
            return _authorize_failed(
                request, normalized,
                "Too many attempts. Try again in 5 minutes.",
                status=429, locked=True,
            )

        code_totp = body.get("totp", "")
        totp_result = client_store.check_totp(client_id, code_totp)
        if totp_result is not TotpResult.OK:
            # A replayed code is the operator re-submitting one they already
            # spent, not an attack -- it must not count towards the lockout.
            if totp_result is TotpResult.INVALID:
                totp_record_failure(client_id, ip)
            audit.emit(
                "auth.authorize.fail", client_id=client_id,
                reason=totp_result.value, ip=ip,
            )
            return _authorize_failed(
                request, normalized,
                (
                    TOTP_REPLAY_MESSAGE
                    if totp_result is TotpResult.REPLAY
                    else "Incorrect code. Check that your device clock is in sync."
                ),
                locked=totp_locked(client_id, ip),
            )
        totp_record_success(client_id, ip)
        audit.emit("auth.authorize.2fa", client_id=client_id, ip=ip)
        return _authorize_approved(request, normalized)

    async def oauth_authorize_finalize(request: Request) -> Response:
        """Turn an approved ticket into an authorization code and redirect."""
        form = await request.form()
        ticket_raw = form.get("ticket", "")
        ticket = ticket_raw.strip() if isinstance(ticket_raw, str) else ""
        normalized = _consume_pending(ticket) if ticket else None
        if normalized is None:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": (
                        "This approval expired or was already used. "
                        "Start the authorization again."
                    ),
                },
                status_code=400,
            )
        audit.emit(
            "auth.authorize.ok", client_id=normalized["client_id"],
            ip=client_ip(request, tuple(config.server.trusted_proxies)),
        )
        return _authorize_redirect(normalized)

    # --- passkeys on the authorize page ----------------------------------

    def _authorize_passkey_guard(request: Request) -> Response | None:
        """Rate-limit + availability gate shared by the passkey endpoints."""
        if passkey_service is None or not passkey_service.available:
            return JSONResponse(
                {"ok": False, "error": "Passkeys are not available here."},
                status_code=503,
            )
        ip = client_ip(request, tuple(config.server.trusted_proxies))
        if not _login_limiter.check(ip):
            retry = _login_limiter.retry_after(ip)
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Too many attempts. Retry in {retry}s.",
                },
                status_code=429,
            )
        return None

    async def _authorize_json_body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    async def oauth_passkey_options(request: Request) -> Response:
        blocked = _authorize_passkey_guard(request)
        if blocked is not None:
            return blocked
        body = await _authorize_json_body(request)
        client_id = str(body.get("client_id") or "").strip()
        if not client_id or not client_store.exists(client_id):
            return JSONResponse(
                {"ok": False, "error": "Unknown client."}, status_code=400,
            )
        # No TOTP-lockout check on the passkey path: a passkey cannot be
        # guessed, so it stays the owner's way in while someone burns the
        # client's TOTP budget. The per-IP guard above still applies.
        try:
            options, state = passkey_service.authentication_options(
                request, client_id=_passkey_owner(client_id),
            )
        except passkeys_mod.PasskeyError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=400,
            )
        return JSONResponse({"ok": True, "options": options, "state": state})

    async def oauth_passkey_verify(request: Request) -> Response:
        blocked = _authorize_passkey_guard(request)
        if blocked is not None:
            return blocked
        body = await _authorize_json_body(request)
        raw_params = body.get("params")
        if not isinstance(raw_params, dict):
            return JSONResponse(
                {"ok": False, "error": "invalid_request"}, status_code=400,
            )
        params = {k: v for k, v in raw_params.items() if isinstance(v, str)}
        normalized, err = _validate_authorize_params(params)
        if err is not None:
            return JSONResponse(
                {"ok": False, "error": "Invalid authorization request."},
                status_code=400,
            )
        client_id = normalized["client_id"]
        credential = body.get("credential")
        state = str(body.get("state") or "")
        if not isinstance(credential, dict) or not state:
            return JSONResponse(
                {"ok": False, "error": "Malformed passkey response."},
                status_code=400,
            )
        try:
            record = passkey_service.verify_authentication(
                request, state=state, credential=credential,
            )
        except passkeys_mod.PasskeyError as exc:
            audit.emit(
                "auth.authorize.fail", client_id=client_id,
                reason=f"passkey:{exc}",
                ip=client_ip(request, tuple(config.server.trusted_proxies)),
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
        # The challenge was minted for the seed owner; make sure the
        # credential that answered it really guards *this* client.
        if record.client_id != _passkey_owner(client_id):
            return JSONResponse(
                {"ok": False, "error": "This passkey belongs to another client."},
                status_code=401,
            )
        totp_record_success(client_id, _ip(request))
        audit.emit(
            "auth.authorize.2fa", client_id=client_id, via="passkey",
            ip=client_ip(request, tuple(config.server.trusted_proxies)),
        )
        return _authorize_approved(request, normalized, as_json=True)

    async def oauth_passkey_register_options(request: Request) -> Response:
        blocked = _authorize_passkey_guard(request)
        if blocked is not None:
            return blocked
        body = await _authorize_json_body(request)
        normalized = _peek_pending(str(body.get("ticket") or ""))
        if normalized is None:
            return JSONResponse(
                {"ok": False, "error": "This approval expired. Start again."},
                status_code=400,
            )
        owner = _passkey_owner(normalized["client_id"])
        try:
            options, state = passkey_service.registration_options(
                request,
                client_id=owner,
                client_name=client_store.get_name(owner) or owner,
            )
        except passkeys_mod.PasskeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "options": options, "state": state})

    async def oauth_passkey_register_verify(request: Request) -> Response:
        blocked = _authorize_passkey_guard(request)
        if blocked is not None:
            return blocked
        body = await _authorize_json_body(request)
        normalized = _peek_pending(str(body.get("ticket") or ""))
        if normalized is None:
            return JSONResponse(
                {"ok": False, "error": "This approval expired. Start again."},
                status_code=400,
            )
        credential = body.get("credential")
        state = str(body.get("state") or "")
        if not isinstance(credential, dict) or not state:
            return JSONResponse(
                {"ok": False, "error": "Malformed passkey response."},
                status_code=400,
            )
        try:
            record = passkey_service.verify_registration(
                request, state=state, credential=credential,
            )
        except passkeys_mod.PasskeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        audit.emit(
            "auth.passkey.register.ok",
            client_id=record.client_id, label=record.label,
        )
        return JSONResponse(
            {"ok": True, "passkey": record.to_json()}, status_code=201,
        )

    async def oauth_token(request: Request) -> Response:
        ip = client_ip(request, tuple(config.server.trusted_proxies))
        if not _token_limiter.check(ip):
            retry = _token_limiter.retry_after(ip)
            return JSONResponse(
                {"error": "rate_limited", "error_description": "too many requests"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
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
            auth_events.inc(kind="token", outcome="invalid_client")
            audit.emit("auth.token.fail", client_id=client_id, reason="invalid_client")
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        if grant_type == "client_credentials":
            # 2FA mandatory on every client_credentials exchange (design choice:
            # no non-interactive escape hatch, the operator must re-type a TOTP
            # code at every 24 h token refresh).
            if totp_locked(client_id, ip):
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "too many failed TOTP attempts, retry later",
                    },
                    status_code=400,
                )
            code_totp = body.get("totp", "")
            totp_result = client_store.check_totp(client_id, code_totp)
            if totp_result is not TotpResult.OK:
                # Replays don't count towards the lockout -- see /authorize.
                if totp_result is TotpResult.INVALID:
                    totp_record_failure(client_id, ip)
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": (
                            "totp code already used"
                            if totp_result is TotpResult.REPLAY
                            else "missing or invalid totp"
                        ),
                    },
                    status_code=400,
                )
            totp_record_success(client_id, ip)
            # No refresh token here: every client_credentials exchange must
            # carry a fresh TOTP code, a refresh token would bypass that.
            grant = token_store.issue_oauth(client_id)
            auth_events.inc(kind="token", outcome="ok")
            audit.emit("auth.token.issue", client_id=client_id, grant_type=grant_type)
            return JSONResponse({
                "access_token": grant.access_token,
                "token_type": "bearer",
                "expires_in": grant.expires_in,
            })

        if grant_type == "authorization_code":
            code = body.get("code", "")
            redirect_uri = body.get("redirect_uri", "")
            code_verifier = body.get("code_verifier", "")
            if not code_store.consume(code, client_id, redirect_uri, code_verifier):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            grant = token_store.issue_oauth(client_id, refresh=True)
            auth_events.inc(kind="token", outcome="ok")
            audit.emit("auth.token.issue", client_id=client_id, grant_type=grant_type)
            return JSONResponse({
                "access_token": grant.access_token,
                "token_type": "bearer",
                "expires_in": grant.expires_in,
                "refresh_token": grant.refresh_token,
            })

        if grant_type == "refresh_token":
            presented = body.get("refresh_token", "")
            family_id = token_store.family_of(presented) if presented else None
            try:
                if not presented:
                    raise RefreshTokenError("invalid")
                grant = token_store.refresh(presented, client_id)
            except RefreshTokenError as exc:
                auth_events.inc(kind="token", outcome=f"refresh_{exc.reason}")
                if exc.reason == "reused":
                    audit.emit(
                        "auth.token.refresh.reuse",
                        client_id=client_id, family_id=exc.family_id,
                    )
                else:
                    audit.emit(
                        "auth.token.fail", client_id=client_id,
                        reason=f"refresh_{exc.reason}", grant_type=grant_type,
                    )
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            auth_events.inc(kind="token", outcome="ok")
            audit.emit(
                "auth.token.refresh", client_id=client_id, family_id=family_id,
            )
            return JSONResponse({
                "access_token": grant.access_token,
                "token_type": "bearer",
                "expires_in": grant.expires_in,
                "refresh_token": grant.refresh_token,
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

    async def metrics(_request: Request) -> Response:
        # Prometheus text exposition format. Unauthenticated by design --
        # scrape access is usually controlled via network ACL / reverse
        # proxy rather than a bearer. No labels leak secrets; all values
        # are counters/histograms. If you need auth, front with nginx.
        return Response(
            REGISTRY.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path in (
            "/",
            "/health",
            "/metrics",
            "/oauth/token",
            "/oauth/authorize",
            "/oauth/authorize/finalize",
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

        # One-shot upload links: the token in the path is the credential,
        # minted by an authenticated proxmox_upload_link call. The handler
        # rejects unknown, expired and already used tokens.
        if path.startswith(transfers.UPLOAD_PATH_PREFIX):
            return await call_next(request)

        # Passkey ceremonies for /oauth/authorize. They are part of signing
        # in, so they cannot require a bearer -- there isn't one yet. Their
        # own guards are the WebAuthn signature, the TOTP lockout and the
        # per-IP login limiter; enrolment additionally demands an approval
        # ticket, which is only handed out after a second factor passed.
        if path.startswith("/oauth/passkey/"):
            return await call_next(request)

        # logos on the authorize page, public images
        if path.startswith("/oauth/logo/"):
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
                _build_unauthorized_body(request.headers, error="unauthorized"),
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="beaconmcp", resource_metadata="{resource_meta}"',
                },
            )

        bearer = authorization[7:]
        client_id = token_store.validate(bearer)
        # A bearer of a client revoked from the CLI dies with the client:
        # ``exists`` re-reads clients.json when it changed on disk, which
        # also prunes the client's tokens via ``on_clients_removed``.
        if client_id and not client_store.exists(client_id):
            client_id = None
        if not client_id:
            return JSONResponse(
                _build_unauthorized_body(request.headers, error="invalid_token"),
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer realm="beaconmcp", error="invalid_token", resource_metadata="{resource_meta}"',
                },
            )

        # Expose the bearer to downstream MCP tools via ContextVar so
        # security_end_session can revoke it after responding.
        token_var = current_bearer_token.set(bearer)
        # Lets proxmox_upload_link build an absolute URL when no public_url
        # is configured.
        base_var = transfers.current_public_base.set(issuer)
        try:
            return await call_next(request)
        finally:
            transfers.current_public_base.reset(base_var)
            current_bearer_token.reset(token_var)

    # OAuth Dynamic Client Registration plumbing. Only engaged when both
    # the feature flag is set AND the dashboard is enabled (the slug store
    # lives in the dashboard's SQLite db, opened above).
    dyn_reg_store = None
    if config.server.allow_dynamic_registration:
        if not _dashboard_mod.is_enabled():
            print(
                "ERROR: server.allow_dynamic_registration requires the dashboard "
                "(BEACONMCP_DASHBOARD_ENABLED=true). DCR state lives in the "
                "dashboard's database.",
                file=sys.stderr,
            )
            sys.exit(1)
        from .dashboard.dyn_reg import DynamicSlugStore as _DynamicSlugStore
        assert shared_database is not None  # required path: creation raised otherwise
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
            "grant_types_supported": ["authorization_code", "refresh_token"],
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
        # allowlist (server.allowed_origins + non-origin OAuth exceptions).
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
               if not auth.is_trusted_redirect_uri(u, redirect_origins)]
        if bad:
            return JSONResponse(
                {"error": "invalid_redirect_uri",
                 "error_description": (
                     "one or more redirect_uris are not on the trusted-origin "
                     "allowlist; add them to server.allowed_origins in beaconmcp.yaml"
                 ),
                 "rejected_redirect_uris": bad},
                status_code=400,
            )

        def _https_meta(key: str) -> str | None:
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048:
                return value
            return None

        try:
            new_client_id, new_client_secret = client_store.create_dynamic(
                owner_client_id=row.owner_client_id,
                name=f"{row.label} ({client_name})"[:120],
                registration_source=f"chatgpt:{slug}",
                logo_uri=_https_meta("logo_uri"),
                client_uri=_https_meta("client_uri"),
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
            "grant_types": ["authorization_code", "refresh_token"],
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

    mcp_app = _McpSlugRewriteApp(
        mcp.streamable_http_app(transport_security=transport_security)
    )

    # The MCP streamable-HTTP app starts its session manager in its own
    # lifespan. Mount does not run a child's lifespan, so the parent
    # Starlette's lifespan must start the StreamableHTTP session manager
    # itself; mcp 2.x prescribes ``session_manager.run()`` for that, otherwise
    # requests fail with "Task group is not initialized".
    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
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
        login_limiter=_login_limiter,
        trusted_proxies=tuple(config.server.trusted_proxies),
        passkey_service=passkey_service,
        updates=config.features.updates,
        config_path=config.source_path,
    )

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/metrics", metrics),
            Route("/.well-known/oauth-authorization-server", oauth_metadata),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata),
            Route("/oauth/authorize", oauth_authorize_get, methods=["GET"]),
            Route("/oauth/authorize", oauth_authorize_post, methods=["POST"]),
            Route(
                "/oauth/authorize/finalize",
                oauth_authorize_finalize, methods=["POST"],
            ),
            Route("/oauth/passkey/options", oauth_passkey_options, methods=["POST"]),
            Route("/oauth/passkey/verify", oauth_passkey_verify, methods=["POST"]),
            Route(
                "/oauth/passkey/register/options",
                oauth_passkey_register_options, methods=["POST"],
            ),
            Route(
                "/oauth/passkey/register/verify",
                oauth_passkey_register_verify, methods=["POST"],
            ),
            Route("/oauth/token", oauth_token, methods=["POST"]),
            Route("/oauth/register", oauth_register, methods=["POST"]),
            Route(
                transfers.UPLOAD_PATH_PREFIX + "{token}",
                transfers.make_upload_endpoint(tuple(config.server.trusted_proxies)),
                methods=["PUT", "POST"],
            ),
            *logo_resolver.routes(),
            *dcr_routes,
            *dashboard_routes,
            Mount("/", app=mcp_app),
        ],
        middleware=[
            Middleware(MetricsMiddleware),
            Middleware(BaseHTTPMiddleware, dispatch=auth_middleware),
        ],
        lifespan=lifespan,
    )
    if not serve:
        return app

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
    # The login pages hide their passkey buttons when this is off, with no
    # visible explanation -- so say it here, where the operator can see it.
    passkey_reason = passkey_service.unavailable_reason
    if passkey_reason is None:
        print("Passkeys:  enabled (also needs HTTPS, or a loopback host)")
    else:
        print(f"Passkeys:  disabled - {passkey_reason}")
    if n_clients == 0:
        print("\nNo clients registered. Create one with: beaconmcp auth create --name 'My Client'")
    # proxy_headers=False: the app owns its own forwarded-header trust model
    # end to end -- client_ip walks X-Forwarded-For against trusted_proxies and
    # forwarded_host does the same for X-Forwarded-Host, both keyed on the real
    # TCP peer. uvicorn's default ProxyHeadersMiddleware (proxy_headers=True,
    # forwarded_allow_ips="127.0.0.1") rewrites scope["client"] to the XFF
    # client before the app runs, which would hide the real peer from both
    # helpers -- their trusted-proxy branch could never open. Every scheme read
    # goes through the x-forwarded-proto header directly, so nothing else needs
    # uvicorn to interpret the forwarded headers for us.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=False,
        timeout_graceful_shutdown=_SHUTDOWN_GRACE_SECONDS,
    )


def _build_dashboard_routes(client_store, token_store, totp_locked,
                             totp_record_failure, totp_record_success,
                             *, dyn_reg=None, shared_database=None,
                             login_limiter=None, trusted_proxies=(),
                             passkey_service=None, updates=None,
                             config_path=None):
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
        login_limiter=login_limiter,
        trusted_proxies=trusted_proxies,
        passkeys=passkey_service,
        updates_enabled=updates.enabled if updates is not None else True,
        allow_self_update=(
            updates.allow_self_update if updates is not None else True
        ),
        config_path=config_path,
    )
    return build_dashboard_routes(deps)


if __name__ == "__main__":
    main()
