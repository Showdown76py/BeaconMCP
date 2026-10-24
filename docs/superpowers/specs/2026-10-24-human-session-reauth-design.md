# Human-session re-auth via MCP elicitation — Design

**Date:** 2026-10-24
**Status:** Approved
**Scope:** OAuth/MCP auth layer, `/oauth/authorize` page, a new re-verification page, form-elicitation confirmation for a short list of destructive tools.
**Mockup:** [`2026-10-24-human-session-reauth-mockup.html`](2026-10-24-human-session-reauth-mockup.html) (open in a browser; three states side by side).

## 1. Why

BeaconMCP expires interactive access after 24 h on purpose: a human must prove again, with TOTP or a passkey, that they still control the client. That rule stays.

What hurts is how the expiry reaches the user. Today the bearer issued by the `authorization_code` grant *is* the 24 h session: it has no refresh token and it lives only in memory. When it expires (or the server restarts), the next MCP request gets a 401. Claude Code does not re-run the browser flow on its own in that case: it shows a notice and the user has to go to `/mcp` → *Re-authenticate*. Most users are on Claude Code CLI, Claude Code Desktop or Claude Cowork.

MCP URL-mode elicitation lets the server hand the client a link to open in the browser, inside a live MCP connection. This design uses it so the expiry turns into "open this link, verify, carry on" instead of a broken connection.

## 2. Goals, non-goals, anti-goals

**Goals**

1. Keep a 24 h human session for interactive OAuth clients.
2. When that session has lapsed, a tool call leads the user to a BeaconMCP re-verification page (passkey or TOTP), and the call then completes without the user having to reconnect the server.
3. Clients that can't do URL elicitation keep working exactly as today (401 → reconnect).
4. Remaster the `/oauth/authorize` page (mockup state 1) and add the re-verification page (states 2 and 3) in the same visual language.
5. A short list of destructive tools asks for an explicit human confirmation through form elicitation.

**Non-goals**

- Changing the 24 h duration or making it optional.
- Touching named tokens (dashboard "API tokens") or the `client_credentials` grant. Both keep today's behaviour.
- The dashboard's own login and TOTP-refresh pages (`/app/login`, `/app/refresh`). *(My call: the user asked to remaster "the login page"; the page they see from Claude Code is `/oauth/authorize`, so that is the one in scope.)*
- Using the re-verification page for destructive-action confirmation. The user chose a plain form elicitation for that.
- MCP Tasks and the migration to `mcp` 2.x / spec 2026-07-28 (see §9).

**Anti-goals** (would be a failure even if it works)

- A stolen bearer that keeps working past 24 h without a fresh passkey/TOTP.
- Automation breaking: elicitation needs a human, so it must never be required on a path without one (named tokens, `client_credentials`, clients without the capability).
- Confirmation prompts on routine actions. Elicitation is reserved for the few truly destructive tools (§6).
- Leaking a secret through the model: TOTP codes and passkey ceremonies only ever happen in the browser page.

## 3. Constraints

- `mcp` is pinned `>=1.0,<2` (installed: 1.27). It ships `ctx.elicit()` (form), `ctx.elicit_url()`, `ElicitRequestURLParams`, `ElicitationRequiredErrorData` and `ElicitCompleteNotification`.
- Client support (Claude Code docs, fetched 2026-10-24):
  - Form elicitation: supported (dialog).
  - URL elicitation: Claude Code asks whether to open the link in the browser. Whether it auto-retries a call after `-32042` / `notifications/elicitation/complete` is **not documented**. A June 2026 bug report said URL mode regressed to form-only around 2.1.108.
  - Cowork: connectors with OAuth work. Elicitation support is **unknown**.
  - Consequence: the design must not depend on the client retrying the call.
- Unnamed bearers are in-memory only (`TokenStore._tokens`); a restart drops them.
- `/oauth/authorize` is self-contained (works with the dashboard disabled): its CSS/JS are module-level strings in `__main__.py`, and its WebAuthn code is duplicated from `dashboard/static/webauthn.js`.
- Tools are wrapped by `server._metric_tool`, which already runs around every tool call and is the natural place to gate.

## 4. Behaviour

### 4.1 Bearer vs human session

For bearers issued by the **`authorization_code` grant** only:

- **Bearer**: authenticates the MCP connection. Lifetime 30 days, fixed from issue and not extended by re-verification *(my call: mirrors `NAMED_TOKEN_TTL`)*. Persisted so a restart doesn't drop it, **stored hashed** (SHA-256 of the token as the key) rather than in plain text like named tokens today *(my call)*. When a client completes a new `/oauth/authorize` flow, its previous interactive bearers are revoked *(my call)*.
- **Human session**: a timestamp `verified_at` stored with the bearer. Valid for 24 h after the last passkey/TOTP check (initial authorize, or a re-verification).

All other bearers (named tokens, `client_credentials`, dashboard-internal bearers) have no human-session concept and are unaffected.

### 4.2 What the lapsed session blocks

With a valid bearer but `verified_at` older than 24 h:

- `tools/call`: gated (§4.3).
- `resources/read`, `prompts/get`: refused with an error carrying the re-verification link. They can't wait like a tool call, and returning data would defeat the point.
- `initialize`, `tools/list`, `resources/list`, `prompts/list`, ping: allowed, so the client stays connected and can show the tools.

### 4.3 Gated tool call

1. The gate (in `_metric_tool`'s wrapper, before running the tool) sees the lapsed session.
2. If the client declared `elicitation.url` at initialize:
   - Create a **pending re-verification** bound to: bearer, `client_id`, tool name and compacted arguments, creation time. TTL 10 minutes.
   - Send `elicitation/create` in URL mode with `url = {issuer}/oauth/reverify?id=<opaque id>` and a short message ("Your BeaconMCP session expired. Verify to run `proxmox_vm_start`.").
   - Hold the call, waiting for the pending entry to be completed, up to a hold timeout *(my call: 5 minutes, configurable)*.
   - On completion: set `verified_at = now` on the bearer, send `notifications/elicitation/complete`, run the tool, return its result normally.
   - If the user declines/cancels the elicitation, or the hold times out: return a tool error that includes the link and says to retry after verifying. The pending entry stays valid until its own TTL, so verifying late and retrying works.
3. If the client declared form elicitation but not `elicitation.url` (e.g. the Claude Code regression noted in §3): send a form elicitation whose message carries the same link, with a single boolean "I've verified", and hold the call exactly as in step 2. The hold ends when the pending entry is completed server-side, whatever the boolean says. *(My call: most users are on Claude Code, so this branch must exist.)*
4. If the client declared neither: answer **401 with the same `WWW-Authenticate` header as today**, so the client falls back to its normal re-auth (today's behaviour). *(Detail for the builder: the capability is only known per MCP session, so the gate needs to reach the HTTP layer; returning a tool error with the link is the acceptable fallback if forcing a 401 from inside a tool call isn't practical. Document what was chosen.)*

Concurrent calls during a lapse share one pending re-verification per bearer rather than opening one link each.

### 4.4 The re-verification page (`/oauth/reverify`)

Mockup state 2, then state 3.

- Lives next to `/oauth/authorize`, same self-contained approach (no dependency on the dashboard being enabled), same passkey + TOTP backends and the same lockout counters.
- Shows: server host, "Last verified <when>", the waiting request (tool name + compacted args exactly as sent, no extra lookups such as VM names *(my call)*, "From <client name> · <age>", "Link expires in <countdown>"), a primary "Verify with passkey" button when the client has a passkey, "Use a 6-digit code instead" as secondary. Without a passkey, TOTP boxes come first.
- Origin of the request (user's decision): the page also shows where the waiting request came from, so the owner can spot a link triggered by a stolen bearer. It shows the requesting IP (as seen by BeaconMCP, honouring the existing trusted-proxy handling), a short user-agent / client description, and when the bearer was issued ("Connected 3 days ago"). These are captured on the pending entry when the gate creates it. A "Not you? Revoke this access" action revokes the bearer and its pending entries.
- Binding: the opaque id resolves to the pending entry, and the passkey/TOTP must belong to that entry's `client_id`. That is what stops a link from being completed by someone else.
- Single use: completing, expiring or being superseded invalidates the id.
- On success: state 3, "You're verified. Return to Claude Code, your request is running now." plus "Session valid until …", "Verified with …", "You can close this tab." The client name comes from the client store, not hard-coded.
- CSRF and rate limiting as on `/oauth/authorize`.

### 4.5 `/oauth/authorize` remaster

Mockup state 1, applied to the existing page and flow (no protocol change):

- Server bar with the BeaconMCP logo (SVG from `assets/logo.webp`) and the host.
- Client icon ↔ BeaconMCP logo, then "Connect <client name>" (see §4.6). Known clients get their **official** logo (the user asked for the real Claude Code logo in production, not the mockup's hand-drawn star). Unknown clients get a neutral generic icon and their registered name.
- A short list of what access grants, by capability kind only (Proxmox, BMC, SSH), shown when that capability is configured. No counts, hostnames or device ids before sign-in *(my call: the page is reachable before 2FA)*.
- Passkey first when available, then "or 6-digit code" with the TOTP boxes.
- Footer: "Redirects to" with the redirect URI's **host in bold**, and the client id. No "Session" line.
- The post-2FA confirmation step and its no-JS form path stay.

### 4.6 Identifying the client (Claude Code, Codex, ChatGPT…)

The two pages don't have the same information, so detection uses what each one has:

| Signal | Where it's available | Example |
|---|---|---|
| MCP `initialize` → `clientInfo.name` / `version` | MCP session only: the gate, so the re-verify page | `claude-code` 2.1.x |
| `User-Agent` of the MCP HTTP requests | MCP session; captured on the pending entry | Claude Code / Codex user agents |
| `redirect_uri` of the OAuth request | `/oauth/authorize` | `http://localhost:<port>/callback`, `https://claude.ai/api/mcp/auth_callback`, `https://chatgpt.com/connector_platform_oauth_redirect` |
| Registered client name (`auth create --name`, or DCR `client_name`) | both | "Claude Code", "ChatGPT connector" |

- A small table of known clients maps these signals to a display name and an official logo: Claude Code, Claude (claude.ai / Desktop / Cowork connectors), Codex, ChatGPT. Matching order: `clientInfo.name` when present, then `redirect_uri` host/path, then the registered name. Unmatched → registered name + generic icon.
- On `/oauth/authorize` there is no MCP session yet, so a `localhost` redirect is ambiguous (Claude Code, Codex and other CLIs all use one). In that case the registered name decides, and if it doesn't match a known client the page shows the registered name with the generic icon rather than guessing.
- These signals are **self-declared and spoofable**. They are used for display only (icon, name, the "Device" line of §4.4), never for any security decision. *(My call.)*
- The `clientInfo` seen at `initialize` is remembered per bearer so the re-verify page and the audit log can show it.
- Logos ship as static assets with the server (no hot-linking), taken from each vendor's official brand resources.

## 5. Security notes

- A stolen bearer is useless after 24 h: every gated path needs a passkey/TOTP of the owning client.
- The re-verification link carries only an opaque random id, never the bearer or client secret.
- Revoking a bearer (`security_end_session`, dashboard) also kills its pending re-verifications.
- Known residual risk: an owner who ignores the origin details can still complete a link triggered by a stolen bearer. The origin block (§4.4) is the chosen mitigation.
- Audit events: `auth.reverify.requested`, `auth.reverify.ok`, `auth.reverify.fail`, `auth.reverify.expired`, each with `client_id` and the tool name.

## 6. Destructive-action confirmation (form elicitation)

Separate from re-auth, and deliberately narrow.

- Default list: `bmc_power_off`, `bmc_power_reset`, `proxmox_snapshot_rollback`, `proxmox_snapshot_delete`, `proxmox_backup_restore` when `force=True`, `beaconmcp_self_update`. Overridable in `beaconmcp.yaml` *(key name left to the builder, e.g. `security.confirm_tools`)*.
- When the client supports form elicitation: before running, send a form elicitation with a single boolean "Confirm" and a message naming the target ("Power off srv-02 via iLO?"). Run only on `accept` + confirmed. `dry_run=True` calls skip the prompt.
- For `beaconmcp_self_update`, a confirmed elicitation replaces `confirm=True` on capable clients *(my call)*.
- When the client doesn't support it: the tool still runs, with today's behaviour (the existing `confirm` / `force` / `dry_run` parameters where they exist). This is the user's decision, to keep automation working. It makes the confirmation best-effort, not a hard gate. `bmc_power_off` and `bmc_power_reset` therefore run unconfirmed on such clients, as they do today.
- Copy must not over-promise: the authorize page says the confirmation happens "when your client supports it".
- Audit: `tool.confirm.ok` / `tool.confirm.declined`.

## 7. Configuration

Under `server:` in `beaconmcp.yaml`, with defaults *(names are suggestions)*:

- `human_session_ttl`: 86400
- `interactive_bearer_ttl`: 2592000
- `reverify_hold_timeout`: 300
- `reverify_link_ttl`: 600

## 8. Testing

- Unit: `TokenStore` `verified_at` + persistence of interactive bearers, pending re-verification lifecycle (create, share, complete, expire, revoke).
- Gate: lapsed session with/without `elicitation.url`; completion within hold; decline; hold timeout then late verify and retry; named / `client_credentials` bearers never gated.
- Page: TOTP and passkey success, wrong client's passkey rejected, lockout, single use, expired id.
- Form confirmation: accept, decline, no capability, `dry_run`.
- Manual: Claude Code CLI and Desktop against a dev server with `human_session_ttl` lowered to ~60 s. Cowork if reachable. Record what each client does with the URL elicitation and whether it retries.

## 9. Later

- MCP 2026-07-28: the same flow maps to `InputRequiredResult`. Do it with the `mcp` 2.x migration.
- MCP Tasks for long Proxmox jobs (UPIDs) and async exec.

## 10. Left to the builder

- Where the pending re-verification store lives (in-memory is fine, it's short-lived).
- How the gate reaches the MCP session to read client capabilities and send elicitations from inside `_metric_tool`.
- Exact message wording, error shapes and config key names.
- Splitting `/oauth/authorize` CSS/JS so the remaster and the new page share them.
