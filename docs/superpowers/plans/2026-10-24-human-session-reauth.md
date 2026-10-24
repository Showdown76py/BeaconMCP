# Human-session re-auth via MCP elicitation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** keep the 24 h human session for interactive OAuth clients, but turn its expiry into an in-band re-verification (URL or form elicitation) instead of a 401, remaster `/oauth/authorize`, and add best-effort form-elicitation confirmation for a few destructive tools.

**Architecture:** interactive bearers (authorization_code grant) become 30-day, hashed-on-disk tokens carrying a `verified_at` stamp. An ASGI wrapper around the MCP app records each session's client capabilities at `initialize` and answers 401 / JSON-RPC errors for lapsed sessions that can't elicit. Elicitation-capable sessions reach the tool wrapper, which holds the call on a pending re-verification until the new `/oauth/reverify` page completes it. New code lives in small modules; `__main__._run_http` only wires them.

**Tech Stack:** Python 3.11+, `mcp` 1.x FastMCP (streamable HTTP, stateful), Starlette, anyio, httpx, sqlite3, pytest + Starlette `TestClient`.

**Spec:** `docs/superpowers/specs/2026-10-24-human-session-reauth-design.md`

## Global Constraints

- `mcp` stays pinned `>=1.0,<2`. No new runtime dependency (httpx, anyio already present).
- Defaults: `human_session_ttl` 86400, `interactive_bearer_ttl` 2592000, `reverify_hold_timeout` 300, `reverify_link_ttl` 600 (seconds), all under `server:` in `beaconmcp.yaml`.
- Named tokens, `client_credentials` bearers and dashboard-internal bearers keep today's behaviour exactly.
- Interactive bearers are persisted **hashed** (SHA-256 hex); raw token never written to disk.
- `/oauth/reverify*` and `/oauth/logo/*` work with the dashboard disabled (same self-contained rule as `/oauth/authorize`).
- Client-supplied SVG is never served. Remote images: HTTPS only, no private/loopback/link-local targets, ≤ 256 KB, PNG/WebP/JPEG/ICO, cached on disk, never fetched on the render path.
- Client identity and logos are display-only; no security decision reads them.
- Code comments: English, short, lowercase, only for non-obvious choices (user rule).
- Run tests with the project venv: `python -m pytest -q`.

## Review Focus

- Server restart while a session is lapsed: bearer reloads from disk, MCP session is gone, client re-initializes, capabilities get recorded again, gate still works. → test in Task 3 (`test_capabilities_recorded_on_reinitialize`).
- Two tool calls racing during a lapse share one pending entry and both resume after one verification. → Task 4 (`test_concurrent_calls_share_pending`).
- Re-verification link opened after the hold timed out: page still completes, the next call runs without a new prompt. → Task 4 (`test_late_verify_then_retry_runs`).
- Revoking the bearer (security_end_session / "Revoke this access") while a call is held: the held call fails fast, the link stops working. → Task 5 (`test_revoke_from_page_kills_bearer_and_pending`).
- A passkey/TOTP of another client on someone's re-verify link is rejected. → Task 5 (`test_other_clients_passkey_rejected`).

---

### Task 1: Interactive bearers in `TokenStore`

**Files:**
- Modify: `src/beaconmcp/auth.py` (`AccessToken`, `TokenStore`)
- Modify: `src/beaconmcp/__main__.py` (authorization_code branch of `oauth_token`)
- Test: `tests/test_interactive_tokens.py`

**Interfaces:**
- Produces:
  - `AccessToken.interactive: bool = False`, `AccessToken.verified_at: float = 0.0`
  - `token_hash(token: str) -> str` (module-level, sha256 hex)
  - `TokenStore(db_path=None, *, named_token_ttl=None, interactive_token_ttl: int | None = None)`; `INTERACTIVE_TOKEN_TTL = 30 days`
  - `TokenStore.issue_interactive(client_id: str) -> tuple[str, int]` (sets `verified_at=now`, persists hashed)
  - `TokenStore.get_interactive(token: str) -> AccessToken | None` (None if unknown, expired or not interactive)
  - `TokenStore.mark_verified(thash: str, at: float | None = None) -> bool`
  - `TokenStore.revoke_hash(thash: str) -> bool`
  - `validate()` and `revoke()` cover interactive tokens too.

- [ ] **Step 1: Write failing tests** — `test_issue_interactive_validates_and_is_hashed_on_disk` (sqlite row has `token_hash == token_hash(tok)`, no raw token anywhere in the file bytes), `test_interactive_survives_restart` (new `TokenStore` on same db validates the token, keeps `verified_at`), `test_mark_verified_persists`, `test_interactive_ttl_default_30_days` (`expires_in == 2592000`), `test_revoke_interactive`, `test_get_interactive_ignores_named_and_plain`.
- [ ] **Step 2: Run, expect FAIL** — `python -m pytest tests/test_interactive_tokens.py -q`
- [ ] **Step 3: Implement** — new sqlite table `interactive_tokens(token_hash TEXT PRIMARY KEY, client_id TEXT, expires_at REAL, created_at REAL, verified_at REAL)`; in memory a second dict keyed by hash; `validate()` falls back to it. Switch the authorization_code grant to `issue_interactive`.
- [ ] **Step 4: Run, expect PASS** (whole suite stays green)
- [ ] **Step 5: Commit** `feat(auth): persistent hashed interactive bearers with verified_at`

### Task 2: Re-auth state (`reauth.py`)

**Files:**
- Create: `src/beaconmcp/reauth.py`
- Test: `tests/test_reauth_state.py`

**Interfaces:**
- Produces:
  - `@dataclass ClientSessionInfo(elicit_form: bool, elicit_url: bool, name: str = "", title: str = "", version: str = "", website_url: str = "", icons: list[str], user_agent: str = "", ip: str = "")` + `ClientSessionInfo.from_initialize(params: dict, user_agent: str, ip: str)` — empty `elicitation: {}` means form only (2025-06-18 rule).
  - `class ClientInfoRegistry`: `record(thash, info)`, `get(thash) -> ClientSessionInfo | None`
  - `def session_lapsed(at: AccessToken, ttl: int, now: float | None = None) -> bool`
  - `@dataclass PendingReverify(id, token_hash, client_id, tool, args: dict, created_at, expires_at, ip, user_agent, client_label, bearer_issued_at, last_verified_at)` with `done: bool`, `revoked: bool`
  - `class PendingReverifyStore(link_ttl: int = 600)`: `get_or_create(*, token_hash, client_id, tool, args, ip, user_agent, client_label, bearer_issued_at, last_verified_at) -> tuple[PendingReverify, bool]`, `get(pid) -> PendingReverify | None` (None once expired/done/revoked), `complete(pid) -> PendingReverify | None`, `revoke_token(thash) -> int`, `async wait(pid, timeout: float) -> bool`
  - `@dataclass ReauthRuntime(token_store, registry, pending, session_ttl, hold_timeout, issuer_for: Callable[[Request], str])`, `configure(rt | None)`, `runtime() -> ReauthRuntime | None`

- [ ] **Step 1: Failing tests** — `test_from_initialize_empty_elicitation_is_form_only`, `test_from_initialize_url_and_form`, `test_session_lapsed_only_for_interactive`, `test_get_or_create_shares_live_entry_per_token`, `test_pending_expires_after_link_ttl`, `test_complete_wakes_waiter` (anyio), `test_wait_times_out_false`, `test_revoke_token_wakes_waiters_with_false`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** — ids from `secrets.token_urlsafe(24)`; one `anyio.Event` per entry; expired entries pruned lazily.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat(reauth): pending re-verification and client session registry`

### Task 3: ASGI gate in front of the MCP app

**Files:**
- Modify: `src/beaconmcp/reauth.py` (add `McpGateApp`)
- Test: `tests/test_reauth_asgi.py`

**Interfaces:**
- Consumes: Task 1 `get_interactive`, Task 2 registry/pending/runtime.
- Produces: `McpGateApp(inner, *, resource_metadata_for: Callable[[Scope], str])` (reads `runtime()` per request; passthrough when None).

Behaviour per POST with a bearer (body buffered, ≤ 1 MiB, otherwise passthrough):
- `initialize` → `registry.record(hash, ClientSessionInfo.from_initialize(...))`.
- Lapsed interactive session and a message with method `tools/call`, `resources/read` or `prompts/get`:
  - no recorded info, or neither form nor url → `401` + `WWW-Authenticate: Bearer realm="beaconmcp", error="invalid_token", resource_metadata="…"` (same as today).
  - `resources/read` / `prompts/get` → HTTP 200 JSON-RPC error: url-capable → code `-32042` with `data.elicitations=[{mode:"url", elicitationId, url, message}]`; form-only → code `-32001` with the link in `message`.
  - `tools/call` → passthrough (Task 4).
- Body replayed to the inner app unchanged.

- [ ] **Step 1: Failing tests** (inner app = echo ASGI that returns the body) — `test_initialize_records_capabilities`, `test_lapsed_without_elicitation_gets_401_with_header`, `test_lapsed_tools_call_with_url_passes_through`, `test_lapsed_resources_read_url_gets_32042`, `test_lapsed_resources_read_form_only_gets_link_error`, `test_fresh_session_untouched`, `test_named_token_never_gated`, `test_capabilities_recorded_on_reinitialize`, `test_body_replayed_intact`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement `McpGateApp`**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat(reauth): asgi gate for lapsed human sessions`

### Task 4: Tool-call gate (hold until verified)

**Files:**
- Modify: `src/beaconmcp/reauth.py` (add `gate_tool_call`)
- Modify: `src/beaconmcp/server.py` (`_metric_tool` wrapper calls it first)
- Test: `tests/test_reauth_gate.py`

**Interfaces:**
- Produces: `async def gate_tool_call(ctx, tool: str, args: dict) -> None` — raises `mcp.server.fastmcp.exceptions.ToolError` with the link when not verified in time. No-op when `runtime()` is None, outside an HTTP request, for non-interactive bearers, or when the session is fresh.

Flow: bearer from `ctx.request_context.request.headers`; `get_or_create` pending; only the creator elicits — url-capable → `ctx.elicit_url(message, url, pid)`; form-only → `ctx.elicit(message_with_link, schema=_Verified)` (`_Verified(verified: bool)`); elicitation errors are swallowed (the wait still applies); then `await pending.wait(pid, hold_timeout)`; on success send `ctx.session.send_elicit_complete(pid)` (url case, errors swallowed) and return. Message: `"Your BeaconMCP session expired. Verify it's you to run {tool}."`. ToolError text: `"BeaconMCP session expired. Verify at {url} then retry."`.

- [ ] **Step 1: Failing tests** (fake ctx exposing `request_context.request.headers`, `request_context.session.client_params.capabilities`, `elicit_url`, `elicit`, `session.send_elicit_complete`) — `test_fresh_session_runs_without_elicitation`, `test_url_client_elicits_then_runs_after_complete`, `test_form_client_gets_link_in_form`, `test_hold_timeout_raises_tool_error_with_link`, `test_concurrent_calls_share_pending` (one elicitation, both resume), `test_late_verify_then_retry_runs`, `test_decline_still_waits_then_errors`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement; wire into `_metric_tool` via `mcp.get_context()`** (status `"reauth"` in metrics/audit when blocked)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat(reauth): hold gated tool calls on a pending re-verification`

### Task 5: Shared auth-page UI, client identity, `/oauth/reverify`

**Files:**
- Create: `src/beaconmcp/oauth_ui.py` (CSS, theme JS, WebAuthn JS helpers, `page()`, `server_bar()`, BeaconMCP logo SVG) — moved out of `__main__.py`
- Create: `src/beaconmcp/client_identity.py` (known-client table + `identify()`; logos come in Task 6)
- Create: `src/beaconmcp/reverify.py` (`ReverifyDeps`, `build_reverify_routes(deps) -> list[Route]`)
- Test: `tests/test_reverify_page.py`, `tests/test_client_identity.py`

**Interfaces:**
- `client_identity.ClientIdentity(key: str | None, name: str)`; `identify(*, client_info_name: str = "", redirect_uri: str = "", registered_name: str = "") -> ClientIdentity`. Known keys: `claude-code`, `claude`, `codex`, `chatgpt`. Order: clientInfo name → redirect host/path (`claude.ai/api/mcp/auth_callback` → claude, `chatgpt.com` → chatgpt; localhost never decides) → registered name → `ClientIdentity(None, registered_name or "Unknown client")`.
- `ReverifyDeps(client_store, token_store, pending, registry, totp_locked, totp_record_failure, totp_record_success, passkey_service, passkey_owner: Callable[[str], str], trusted_proxies: tuple[str, ...], login_limiter, logo_url_for: Callable[[ClientIdentity], str])`
- Routes: `GET /oauth/reverify?id=` (page, 410 page when id dead), `POST /oauth/reverify` (form or JSON `{id, totp}`), `POST /oauth/reverify/passkey/options` `{id}`, `POST /oauth/reverify/passkey/verify` `{id, state, credential}`, `POST /oauth/reverify/revoke` `{id}`.
- Success = `token_store.mark_verified(entry.token_hash)` + `pending.complete(id)` + audit `auth.reverify.ok`; failures audit `auth.reverify.fail`; GET audits nothing; creation audits `auth.reverify.requested` (in Task 4's gate).

- [ ] **Step 1: Failing tests** — identity: `test_identify_by_client_info`, `test_identify_by_redirect_claude_ai`, `test_localhost_redirect_falls_back_to_registered_name`, `test_unknown_client_generic`. Page (Starlette `TestClient` on `build_reverify_routes` with fakes like `tests/test_passkeys.py`): `test_get_shows_tool_args_origin_and_last_verified`, `test_get_dead_id_410`, `test_totp_success_marks_verified_and_completes`, `test_totp_wrong_counts_failure`, `test_locked_client_refused`, `test_single_use`, `test_other_clients_passkey_rejected`, `test_revoke_from_page_kills_bearer_and_pending`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** — page follows mockup state 2 then 3 (`docs/superpowers/specs/2026-10-24-human-session-reauth-mockup.html`): "Last verified …", "Verify it's still you", "<client> paused a request until you confirm.", waiting-request block (tool, args as sent), origin block (From IP, Device = clientInfo name/version or user-agent, Connected = bearer age), passkey primary when available else TOTP first, "Signed in as", "Not you? Revoke this access". Success state: "You're verified", "Return to <client>, your request is running now.", "Session valid until …", "You can close this tab." All values HTML-escaped.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat(reauth): re-verification page and client identity`

### Task 6: Client logos (`/oauth/logo/...`) and DCR metadata

**Files:**
- Modify: `src/beaconmcp/client_identity.py` (`LogoResolver`)
- Create: `src/beaconmcp/assets/client_logos/` (`generic.svg`, known-client SVGs, Simple Icons subset + `SOURCE.md` with pinned version and CC0 note)
- Modify: `src/beaconmcp/auth.py` (`Client.logo_uri`, `Client.client_uri`, persisted; `create_dynamic(..., logo_uri=None, client_uri=None)`)
- Modify: `src/beaconmcp/__main__.py` (`dcr_register` stores them)
- Test: `tests/test_client_logos.py`

**Interfaces:**
- `LogoResolver(cache_dir: Path, fetch: Callable[[str], Awaitable[tuple[bytes, str]]] | None = None)`:
  - `url_for(identity: ClientIdentity, *, declared_icons: list[str] = (), logo_uri: str = "", website_url: str = "") -> str` — synchronous, never fetches; returns `/oauth/logo/known/<key>`, `/oauth/logo/cached/<sha>`, `/oauth/logo/si/<slug>`, or `/oauth/logo/generic`; schedules background fetches for uncached remote candidates.
  - `routes() -> list[Route]` serving those paths with `X-Content-Type-Options: nosniff`, long cache headers.
  - `simple_icons_slug(name: str) -> str | None` — lowercase, strip non-alnum, drop trailing "connector"/parenthesised suffix, alias table, must exist in bundled set.
  - `safe_fetch(url) -> tuple[bytes, str]` — https only, resolve host and refuse private/loopback/link-local/reserved, no cross-host redirect, timeout 5 s, ≤ 262144 bytes, content-type in png/webp/jpeg/x-icon (sniffed magic bytes too).

- [ ] **Step 1: Failing tests** — `test_known_client_uses_bundled_logo`, `test_declared_icon_cached_then_served`, `test_uncached_declared_icon_falls_through_without_blocking`, `test_simple_icons_slug_normalisation` (`"GitHub Copilot"` → `githubcopilot`, `"Foo connector"` → `foo` only if bundled), `test_safe_fetch_rejects_http_private_svg_and_oversize`, `test_favicon_from_website_url`, `test_generic_fallback`, `test_dcr_stores_logo_and_client_uri`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat(auth-ui): client logo resolution chain`

### Task 7: `/oauth/authorize` remaster

**Files:**
- Modify: `src/beaconmcp/__main__.py` (`_authorize_page`, drop the moved CSS/JS constants)
- Test: `tests/test_authorize_page.py`

**Interfaces:**
- Consumes: `oauth_ui`, `identify`, `LogoResolver.url_for`.
- Produces: `render_authorize_page(...)` in `oauth_ui.py` or kept in `__main__` — builder's choice; ids `step-verify`, `step-done`, `authorize-form`, `finalize-form`, `totp-inputs`, `passkey-btn` and the JSON/no-JS flows stay as they are.

- [ ] **Step 1: Failing tests** (render the page function directly) — `test_redirect_host_bold`, `test_no_session_line`, `test_capability_kinds_only_no_counts` (config with 3 nodes shows "Proxmox" but not "3"), `test_power_copy_soft` ("Confirmed in your client when it supports it"), `test_known_client_logo_and_name`, `test_passkey_first_when_enabled`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** — mockup state 1.
- [ ] **Step 4: Run, expect PASS** (existing authorize/passkey tests still green)
- [ ] **Step 5: Commit** `feat(auth-ui): remaster the authorize page`

### Task 8: Destructive-action confirmation

**Files:**
- Create: `src/beaconmcp/confirm.py`
- Modify: `src/beaconmcp/server.py` (wrapper calls it after the re-auth gate)
- Modify: `src/beaconmcp/config.py` (`server.confirm_tools: list[str] | None`)
- Test: `tests/test_confirm.py`

**Interfaces:**
- `DEFAULT_CONFIRM_TOOLS = ("bmc_power_off", "bmc_power_reset", "proxmox_snapshot_rollback", "proxmox_snapshot_delete", "proxmox_backup_restore", "beaconmcp_self_update")`
- `async def confirm_destructive(ctx, tool: str, kwargs: dict, tools: Collection[str]) -> dict` — returns possibly-updated kwargs (`confirm=True` injected for `beaconmcp_self_update` on accept); raises `ToolError("Cancelled by the user.")` on decline/cancel/unchecked. Skips: tool not listed, `dry_run=True`, `proxmox_backup_restore` without `force=True`, client without form elicitation, no ctx.
- Schema `_Confirm(confirm: bool)`; message e.g. `"Power off {device_id or 'the server'} via its BMC?"`, `"Roll back VM {vmid} on {node} to snapshot {snapname}?"`.
- Audit `tool.confirm.ok` / `tool.confirm.declined`.

- [ ] **Step 1: Failing tests** — `test_unlisted_tool_untouched`, `test_accept_runs`, `test_decline_raises`, `test_no_form_capability_runs_unconfirmed`, `test_dry_run_skips`, `test_backup_restore_only_when_force`, `test_self_update_confirm_injected`, `test_config_override_list`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat(tools): elicitation confirmation for destructive tools`

### Task 9: Config + wiring + docs

**Files:**
- Modify: `src/beaconmcp/config.py` (`ServerConfig.human_session_ttl`, `interactive_bearer_ttl`, `reverify_hold_timeout`, `reverify_link_ttl`; parse + validate > 0; masked dump)
- Modify: `src/beaconmcp/__main__.py` (build registry/pending/resolver, `reauth.configure`, wrap MCP app in `McpGateApp`, add reverify + logo routes, public paths in `auth_middleware`, `TokenStore(interactive_token_ttl=...)`)
- Modify: `src/beaconmcp/auth.py` (`revoke_current_token` also revokes pending entries via `reauth.runtime()`)
- Modify: `beaconmcp.yaml.example`, `CLAUDE.md` (project structure lines), spec §4.1 (drop "previous bearers revoked", see note below)
- Test: `tests/test_config_yaml.py` additions (`test_reauth_defaults`, `test_reauth_ttl_must_be_positive`)

Note: the spec's "previous interactive bearers are revoked on a new authorize" would log out a second device sharing the same client (CLI + Desktop). A lapsed bearer is already useless without a fresh passkey/TOTP, so this plan drops that rule and amends the spec.

- [ ] **Step 1: Failing config tests**
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement config + wiring + docs**
- [ ] **Step 4: Full suite PASS**
- [ ] **Step 5: Commit** `feat(reauth): wire human-session re-auth into the http server`

### Task 10: End-to-end check

- [ ] **Step 1:** Start a dev server (temp config, one SSH-less dummy capability as the test configs do, `human_session_ttl: 5`), create a client, get an interactive bearer through `/oauth/authorize` + `/oauth/token` with a computed TOTP.
- [ ] **Step 2:** With the Python SDK `ClientSession(elicitation_callback=...)`: call a read-only tool after the TTL, receive the URL elicitation, complete `/oauth/reverify` with TOTP from a second HTTP client, see the held call return. Then without elicitation callback: expect 401.
- [ ] **Step 3:** Screenshot `/oauth/authorize` and `/oauth/reverify` in the browser pane (light + dark).
- [ ] **Step 4:** Record results in the final summary (what was and wasn't verified, e.g. real Claude Code untested).
