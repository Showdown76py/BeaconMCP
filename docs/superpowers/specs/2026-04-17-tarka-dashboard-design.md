# TarkaMCP Dashboard — Design

**Date:** 2026-04-17
**Status:** Draft (awaiting review)
**Scope:** Web dashboard adjacent to the existing MCP server, served on the same origin (`mcp.tarkacore.dev`).

## 1. Goals

Add a web-accessible companion dashboard to TarkaMCP providing two surfaces:

- **Login panel** — collect `client_id` + `client_secret` + TOTP, exchange for an MCP bearer, and persist a browser session for 90 days so users (especially on mobile) no longer need to run `curl + oathtool` to obtain a token.
- **Chat panel** — multi-conversation chat interface powered by Gemini 3 Flash / 3.1 Pro, invoking TarkaMCP tools through the Gemini SDK's native MCP integration. Usable from phone or desktop.

### Success criteria

1. From a phone browser, an operator can log in, open a chat, ask "status de pve2", and see tool-call results without typing in a shell.
2. Session survives closing the browser and coming back within 90 days; only TOTP is re-prompted every 24h.
3. UI feels smooth (streaming responses, no full-page reloads, < 100 ms perceived click latency).
4. The dashboard is optional — if `GEMINI_API_KEY` is absent, the rest of TarkaMCP runs unchanged.

### Non-goals (explicit)

- No multi-user model — one `client_id` = one operator. Conversations are scoped to the client that owns them.
- No rate-limiting beyond the existing TOTP lockout (5 failures / 5 min).
- No conversation sharing, export, or public links.
- No voice / STT.
- No push notifications.
- No syntax-highlighting in code blocks (v1).
- No full-text search over history (v1).

## 2. Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Gemini API key location | Server-side env var `GEMINI_API_KEY`. Server proxies all Gemini calls. |
| Tool execution path | Approach **A** — Gemini SDK invoked with an `McpServer` tool pointing to `https://mcp.tarkacore.dev/mcp` + user bearer in headers. Google's backend calls TarkaMCP directly. |
| Session durability | 90-day HttpOnly server-side session cookie. TOTP re-prompt every 24 h to refresh MCP bearer. |
| Conversation model | Multiple conversations with sidebar, server-persisted in SQLite. |
| Thinking control | Gemini 3 effort presets: `minimal` / `low` / `medium` / `high`. Per-conversation setting. |
| Tool-call visualization | Inline collapsible cards showing `name · duration · status`; expand for args + result preview. |
| Frontend stack | Vanilla HTML + CSS custom properties + ES modules. Jinja2 server-rendered templates. Marked + DOMPurify vendored for markdown. No build step. |
| Chat history storage | SQLite at `/opt/tarkamcp/dashboard.db` (WAL, foreign keys). Single DB for sessions + conversations + messages. |
| Theme | Light / dark via `prefers-color-scheme`, slate-neutral palette + Proxmox orange `#e57000` accent. |
| Model selector | Default `gemini-3-flash`; dropdown to switch to `gemini-3.1-pro`. Choice persisted per conversation. |
| Iconography | Inline monochrome SVG icons (plus, chevron, arrow, ellipsis, status check/warn/spinner). **No Unicode emojis anywhere.** The "no AI slop" rule bans emojis 🎉✨🤖 etc., not vector icons. |

## 3. Module layout

```
src/tarkamcp/
  dashboard/                   NEW MODULE
    __init__.py                register_dashboard_routes(app, ...)
    app.py                     Starlette routes: login, refresh, logout, chat, api/*
    session.py                 SessionStore (SQLite + AES-GCM) and cookie helpers
    db.py                      Connection pooling, migrations (PRAGMA user_version)
    chat.py                    ChatEngine: Gemini SDK wrapper, SSE event generation
    csrf.py                    Double-submit cookie middleware
    templates/
      base.html                Layout, CSS vars, icon sprite
      login.html
      totp_refresh.html
      chat.html                Shell (sidebar + messages + composer)
    static/
      app.css
      chat.js                  SSE client, sidebar, markdown render, composer
      marked.min.js            Vendored 15 kb
      dompurify.min.js         Vendored 20 kb
      icons.svg                SVG sprite (6-7 icons)
  __main__.py                  + mount dashboard routes when enabled
```

The dashboard is mounted conditionally in `__main__.py::_run_http`, similar to how SSH / iLO modules register themselves. Requires `GEMINI_API_KEY` set. Can be disabled via `TARKAMCP_DASHBOARD_ENABLED=false`.

## 4. URL routing

| Path | Method | Auth | Role |
|---|---|---|---|
| `/` | GET | — | 302 to `/app/chat` if session, else `/app/login` |
| `/app/login` | GET | — | Render login form (or single-field refresh if cookie exists) |
| `/app/login` | POST | — | Validate creds + TOTP, create session, set cookie, 302 to `/app/chat` |
| `/app/refresh` | GET | cookie | Render TOTP-only form |
| `/app/refresh` | POST | cookie | Re-issue MCP bearer, update session |
| `/app/logout` | POST | cookie | Revoke bearer, delete session, clear cookie, 302 to `/app/login` |
| `/app/chat` | GET | cookie | Render chat shell HTML |
| `/app/api/conversations` | GET | cookie | JSON list (scoped to `client_id`) |
| `/app/api/conversations` | POST | cookie + CSRF | Create empty conversation |
| `/app/api/conversations/{id}` | GET | cookie | Fetch full conversation + messages |
| `/app/api/conversations/{id}` | PATCH | cookie + CSRF | Rename, change model/effort |
| `/app/api/conversations/{id}` | DELETE | cookie + CSRF | Delete conversation and messages |
| `/app/api/chat/stream` | POST | cookie + CSRF | SSE streaming turn |
| `/app/static/*` | GET | — | Static assets |
| `/mcp`, `/oauth/*`, `/.well-known/*`, `/health` | — | — | **Unchanged** |

All `/app/*` routes go through a dedicated `DashboardSessionMiddleware`, not the existing bearer middleware that protects `/mcp`.

## 5. Authentication & sessions

### Principle

Cookie holds only an opaque `session_id` (256-bit random). All sensitive material (client_secret, current MCP bearer) lives in SQLite, encrypted at rest.

### Session table

```sql
CREATE TABLE sessions (
  session_id             TEXT PRIMARY KEY,
  client_id              TEXT NOT NULL,
  client_secret_enc      BLOB NOT NULL,
  mcp_bearer             TEXT,
  mcp_bearer_expires_at  REAL,
  created_at             REAL NOT NULL,
  last_seen_at           REAL NOT NULL,
  expires_at             REAL NOT NULL,
  user_agent             TEXT
);
CREATE INDEX idx_sessions_client ON sessions(client_id);
```

### Client secret encryption

- Master key: env var `TARKAMCP_SESSION_KEY` (32 bytes, base64-encoded). Generated by `deploy/install.sh` if absent.
- Algorithm: AES-256-GCM via `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- Storage layout: `nonce(12 bytes) || ciphertext || tag`.
- Rotation: not addressed in v1. Compromise recovery = wipe `sessions` table, rotate key, users log in again.

### Cookie

```
Set-Cookie: tarkamcp_session=<session_id>;
  HttpOnly; Secure; SameSite=Strict;
  Path=/app;
  Max-Age=7776000
```

`Path=/app` prevents the cookie from being sent on `/mcp`, `/oauth/*`, or Google's back-channel calls to the MCP endpoint.

### Initial login flow (`POST /app/login`)

```
1. ClientStore.verify(client_id, client_secret)       → 401 if fail
2. totp_locked(client_id)                             → 429 if locked
3. ClientStore.verify_totp(client_id, totp)           → 401 + increment fail count
4. token_store.issue(client_id)                       → (bearer, 86400)
5. Generate session_id = secrets.token_urlsafe(32)
6. AES-GCM encrypt client_secret with TARKAMCP_SESSION_KEY
7. INSERT INTO sessions (...)
8. Set cookie; 302 → /app/chat
```

### 24h bearer refresh flow

Middleware detects `mcp_bearer_expires_at < now()` when an `/app/api/chat/stream` request hits it:

```
1. Return SSE event {type: "session_expired"}
2. Client redirects to /app/refresh
3. /app/refresh GET: render "Code TOTP pour {client_name}" (1 field)
4. /app/refresh POST: verify_totp → token_store.issue → UPDATE sessions
5. 302 → /app/chat; UI auto-retries the last turn
```

If the session cookie is missing, unknown, or past `expires_at`: 302 → `/app/login` (full form).

### Logout

```
1. Load session
2. token_store.revoke(session.mcp_bearer)  (existing 8s grace)
3. DELETE FROM sessions WHERE session_id=?
4. Clear-Site-Data: "cookies" + Set-Cookie tarkamcp_session=; Max-Age=0
5. 302 → /app/login
```

### Multi-session and admin revocation

- Multiple sessions per `client_id` (phone + desktop) are allowed.
- `tarkamcp auth revoke <client_id>` cascades: deletes the client, deletes all its sessions, revokes all its bearers.
- New CLI subcommand `tarkamcp dashboard sessions [--client-id X]` lists sessions with last-seen timestamps and supports `--kill <session_id>`.

### Security

| Surface | Measure |
|---|---|
| Session cookie | HttpOnly, Secure, SameSite=Strict, Path=/app, Max-Age=7776000 |
| CSRF | Double-submit cookie `tarkamcp_csrf_token` (JS-readable, `SameSite=Strict`, `Path=/app`) + header `X-CSRF-Token` required on POST/PATCH/DELETE |
| Session fixation | Regenerate `session_id` at login |
| Secret at rest | AES-256-GCM with env-derived key |
| Secret in logs | Logging filter redacts `sk_*` and `tarkamcp_*` tokens |
| Login brute-force | Reuses existing 5-failure / 5-minute TOTP lockout |
| Clickjacking | `X-Frame-Options: DENY` on `/app/*` |
| MIME sniffing | `X-Content-Type-Options: nosniff` |
| Referrer | `Referrer-Policy: strict-origin-when-cross-origin` |
| CSP | `default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:` |
| DNS rebinding | Existing `TransportSecuritySettings` + `TARKAMCP_ALLOWED_HOSTS` |

## 6. Database schema

SQLite at `/opt/tarkamcp/dashboard.db` (overridable via `TARKAMCP_DASHBOARD_DB`). Mode WAL, `synchronous=NORMAL`, `foreign_keys=ON`. Versioned via `PRAGMA user_version`.

```sql
CREATE TABLE sessions (...);   -- see §5

CREATE TABLE conversations (
  id               TEXT PRIMARY KEY,             -- UUID v4
  client_id        TEXT NOT NULL,
  title            TEXT,
  model            TEXT NOT NULL DEFAULT 'gemini-3-flash',
  thinking_effort  TEXT NOT NULL DEFAULT 'low',  -- minimal|low|medium|high
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE INDEX idx_conv_client ON conversations(client_id, updated_at DESC);

CREATE TABLE messages (
  id               TEXT PRIMARY KEY,
  conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role             TEXT NOT NULL,                -- user|assistant
  content          TEXT,
  tool_calls       TEXT,                         -- JSON array
  thinking_summary TEXT,
  model            TEXT,
  effort           TEXT,
  created_at       REAL NOT NULL
);
CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at);
```

`tool_calls` is a JSON array: `[{id, name, args, result, status, duration_ms, preview}]`. JSON instead of a child table keeps reads to a single SELECT + parse.

## 7. Chat flow

### Turn end-to-end

```
POST /app/api/chat/stream { conversation_id, content, model, effort }
   ↓
1. DashboardSessionMiddleware validates cookie + CSRF, loads session
2. If bearer expired → SSE event "session_expired" and close
3. Load conversation history from SQLite (ordered by created_at)
4. INSERT message (role=user)
5. Open SSE response (Content-Type: text/event-stream, no-cache)
6. Call google-genai streaming API with:
     - model: the conversation's model
     - contents: conversation history + new user message
     - thinking config: effort level from the conversation
     - tools: MCP server pointed at https://mcp.tarkacore.dev/mcp
       with the session's bearer in the Authorization header
   (Exact SDK class names — Tool, McpServer, ThinkingConfig,
   GenerateContentConfig — are verified at implementation time against
   the installed google-genai version.)
7. For each chunk, emit matching SSE event (see table below)
8. On stream end, INSERT message (role=assistant, content, tool_calls, ...)
9. UPDATE conversations SET updated_at=now
10. If this was the first user turn in the conversation, fire auto-title:
    - Secondary short genai call (gemini-3-flash, effort=minimal, no tools)
    - UPDATE conversations SET title=?
    - SSE event "title_updated"
11. Emit "done" event and close stream
```

### SSE event vocabulary

| Event | Payload |
|---|---|
| `text_delta` | `{"text": "..."}` |
| `thinking_delta` | `{"summary": "..."}` |
| `tool_call` | `{"id": "...", "name": "...", "args": {...}}` |
| `tool_result` | `{"id": "...", "status": "ok"\|"error", "preview": "...", "duration_ms": 234}` |
| `error` | `{"code": "...", "message": "..."}` |
| `session_expired` | `{}` |
| `title_updated` | `{"conversation_id": "...", "title": "..."}` |
| `aborted` | `{}` |
| `done` | `{"message_id": "..."}` |

Server honors `Request.is_disconnected` — if the client aborts (user pressed "Arrêter"), the loop breaks, a partial assistant message is persisted with a marker, and `"aborted"` is emitted before close.

### Model and effort

Per-conversation columns. Default for new chat: `gemini-3-flash` + `low`. Changing model or effort applies to subsequent turns; existing messages are unaffected (each message records the `model` and `effort` used).

### Auto-title

Performed after the first *user* message gets its assistant response. Call is isolated (no tools, no streaming), prompt: "Donne un titre de 4 mots maximum, sans emoji, sans ponctuation finale, pour: {user_msg}". Persisted on the conversation. UI updates the sidebar on `title_updated`.

## 8. UI

### Login (`/app/login`)

Centered card layout, styled with the dashboard's light/dark palette (not coupled to the existing `/oauth/authorize` page, which is left untouched). First-time visit shows 3 fields (Client ID, Client Secret, TOTP) + "Rester connecté 90j" checkbox. On subsequent visits where a valid session cookie exists but its bearer is stale, `/app/refresh` renders a single TOTP field with the client name in evidence.

### Chat desktop layout

```
┌──────────────┬──────────────────────────────────────────────┐
│ TarkaMCP     │                                              │
│ + Nouveau    │   [user bubble, right-aligned]               │
│ ─────────    │                                              │
│ > pve2 down? │   Gemini 3 Flash · low                       │
│   LXC update │   [streaming text …]                         │
│   …          │                                              │
│              │   ▸ proxmox_list_vms  · 180 ms · ok          │
│              │                                              │
│ ─────────    │ ┌──────────────────────────────────────────┐ │
│ Modèle       │ │ Envoyer un message à TarkaMCP…           │ │
│  Flash ▼     │ └──────────────────────────────────────────┘ │
│ Effort: low  │  Flash ▼ · effort ▼              [Envoyer]  │
│ Déconnexion  │                                              │
└──────────────┴──────────────────────────────────────────────┘
```

- Sidebar 260 px, collapsible on narrow screens.
- Conversation list sorted by `updated_at DESC`, single-line truncation, hover reveals ⋯ menu (rename, delete).
- Active conversation highlighted with `--bg-soft` and a left accent bar.
- Model / effort controls live at the bottom of the sidebar AND in the composer row (both edit the same conversation settings).

### Chat mobile layout (< 768 px)

```
┌─────────────────────────────┐
│ ☰   pve2 down?        ⋯     │
├─────────────────────────────┤
│                             │
│                  msg user   │
│                             │
│  Gemini 3 Flash · low       │
│  streaming text …           │
│                             │
│  ▸ proxmox_list_vms · 180ms │
│                             │
├─────────────────────────────┤
│ [input message ... ]        │
│ Flash ▼  effort ▼   [↑]     │
└─────────────────────────────┘
```

- Hamburger icon opens sidebar as a left drawer with an overlay dim.
- Safe-area insets respected on iOS (`env(safe-area-inset-bottom)`).
- Touch targets ≥ 44 px.

### Messages zone

- Centered column, `max-width: 48rem`.
- **User** message: `--user-bubble` background, right-aligned, 80 % max-width, plain text (no markdown parsing).
- **Assistant** message: no bubble, plain flow on canvas, with a small header "`{model} · {effort}`" in `--fg-muted`. Markdown rendered via `marked` + sanitized with `DOMPurify` (strict allowlist, no raw HTML passes through).
- **Tool-call card** — collapsed:
  ```
  [chevron-right]  proxmox_list_vms   · 180 ms · ok
  ```
  Expanded:
  ```
  [chevron-down]  proxmox_list_vms   · 180 ms · ok
    args:   { "node": "pve1" }
    result: 12 VMs listées
      [vmid=101] web-prod · running
      ...
    [Copier le résultat]
  ```
  Border 1 px, rounded, `--bg-soft` background. Status icon (check / warn / spinner) is an inline SVG, no emoji.

### Composer

- Auto-growing `<textarea>` (max 10 lines), placeholder "Envoyer un message à TarkaMCP…"
- `Enter` sends, `Shift+Enter` inserts newline, `Ctrl/Cmd+Enter` also sends.
- During streaming the send button becomes **Arrêter** (aborts the fetch; server handles `is_disconnected`).
- After streaming, a small **Régénérer** button appears under the last assistant message (re-runs the same user turn).

### Theme

```css
:root {
  --bg: #ffffff;
  --bg-soft: #f5f5f7;
  --fg: #1a1a1a;
  --fg-muted: #6b6b76;
  --border: #e4e4e8;
  --accent: #e57000;
  --accent-fg: #ffffff;
  --user-bubble: #f0f0f2;
  --danger: #c23b3b;
  --radius: 10px;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f10;
    --bg-soft: #19191b;
    --fg: #ececec;
    --fg-muted: #9a9aa5;
    --border: #2a2a2e;
    --accent: #ff8a2a;
    --accent-fg: #0f0f10;
    --user-bubble: #23232a;
    --danger: #ff6b6b;
  }
}
```

Transitions: `150 ms ease` on hovers and dropdown open. Respect `prefers-reduced-motion` (disable animations).

### Iconography

- Inline SVG sprite (`/app/static/icons.svg`), 6-7 icons total:
  `plus`, `chevron-right`, `chevron-down`, `arrow-up-send`, `ellipsis`, `check`, `warn`, `spinner`.
- `currentColor` fills — icons inherit text color.
- No Unicode emoji anywhere: neither in static copy, nor in placeholders, nor in the system prompt sent to Gemini (to reduce the chance it mirrors an emoji style back at us).

### Accessibility

- Explicit `<label for>` or `aria-label` on every control.
- `aria-live="polite"` on the message stream region.
- AA contrast verified (`#ff8a2a` on `#0f0f10` passes).
- Visible focus ring on all interactives.
- Keyboard nav: `Tab` through sidebar → composer, arrow keys in the conversation list.

## 9. Error handling

| Case | Detection | UI reaction |
|---|---|---|
| Login client_id/secret wrong | `ClientStore.verify` false | Form re-rendered with banner "Identifiants invalides" |
| TOTP wrong | `verify_totp` false, failure recorded | Banner "Code incorrect" + clock-drift hint |
| TOTP lockout | `totp_locked` true | Banner "Trop de tentatives, réessaie dans 5 min" + disabled input |
| Bearer expired mid-chat | `/mcp` returns 401 through Gemini SDK | SSE `session_expired` → modal "Session expirée, code TOTP requis" |
| Session cookie missing/expired | `SessionStore.load` returns None | 302 `/app/login` |
| Gemini down / quota | `google.genai` exception | SSE `error` + toast "Gemini indisponible" + Régénérer button |
| MCP tool error | Gemini emits `tool_result` with failure | Card in red with preview; Gemini continues the turn normally |
| SQLite locked | `OperationalError` | Retry ×3 with backoff 50/150/500 ms; else 500 + toast |
| Network drop during SSE | `EventSource.onerror` | Toast "Connexion interrompue" + Reconnecter button (no resume, full retry) |
| User abort | `AbortController` client-side → `is_disconnected` server-side | Emit `aborted`, persist partial message |
| CSRF fail | Middleware | 403 `{error: "csrf"}` |

Principle: no stack traces in the UI. Short human message + actionable next step.

## 10. Configuration

New environment variables (`.env`):

```
GEMINI_API_KEY=...                                 # required to enable dashboard
TARKAMCP_SESSION_KEY=<base64 32 bytes>             # generated by install.sh
TARKAMCP_DASHBOARD_DB=/opt/tarkamcp/dashboard.db   # optional override
TARKAMCP_DASHBOARD_ENABLED=true                    # optional kill-switch
```

`deploy/install.sh` extended:
- Generates `TARKAMCP_SESSION_KEY` if missing: `openssl rand -base64 32 >> .env`
- Warns if `GEMINI_API_KEY` is missing: "Dashboard chat disabled, set GEMINI_API_KEY to enable"

`marked.min.js` and `dompurify.min.js` are **vendored into the repository** (`src/tarkamcp/dashboard/static/`) at pinned versions. No runtime download, no npm. To update, a maintainer runs a one-off script that fetches the versions from the upstream CDN, verifies their SHA-256 against known values, and commits the result.

## 11. Dependencies

New Python dependencies in `pyproject.toml`:

| Dep | Version | Use |
|---|---|---|
| `google-genai` | `>=1.0` | Gemini 3 SDK with MCP integration |
| `cryptography` | `>=42` | AES-GCM for session client-secret encryption |
| `jinja2` | `>=3` | Server-rendered templates |

Vendored JS assets (no npm):
- `marked` (~15 kb min.js) — Markdown parsing
- `DOMPurify` (~20 kb min.js) — XSS sanitization

## 12. Testing

### Unit (`tests/test_dashboard_unit.py`)

- `SessionStore`: create → load → expire → load returns None.
- AES-GCM round-trip; wrong key rejected.
- SSE event serialization: proper escaping of newlines and quotes.
- CSRF double-submit validation: missing header → 403, mismatched → 403, matching → ok.
- Markdown sanitization: `<script>` stripped, `<a href="javascript:…">` stripped, `<img src=x onerror=…>` stripped.

### Integration (`tests/test_dashboard_integration.py`)

- Full login flow: POST `/app/login` → cookie issued → GET `/app/chat` → 200.
- 24 h bearer refresh: forcibly set `mcp_bearer_expires_at=0` → POST `/app/refresh` with TOTP → new bearer; chat resumes.
- Conversation CRUD: POST → GET list → PATCH rename → DELETE → 404.
- Message persistence: chat stream completes → reload `/app/chat` → history visible.
- Logout: bearer revoked (cannot call `/mcp` any more), cookie cleared, `/app/chat` redirects to login.

### Gemini mocking

- `ChatEngine` accepts an injected `genai.Client`.
- Tests supply a `FakeGeminiClient` yielding a scripted sequence of chunks (text, one tool_call, one tool_result, done).
- CI does **not** hit Google.

### Extending `tests/test_integration.py`

Add a `--section dashboard` selector that:
- Requires `GEMINI_API_KEY` and `TARKAMCP_DASHBOARD_ENABLED=true`.
- Hits `/app/login`, `/app/chat`, and the streaming endpoint with the fake Gemini client.
- Verifies the dashboard routes are registered only when the env conditions are met.

### Manual test checklist (README)

- [ ] Login (client_id / secret / TOTP) on Chrome desktop.
- [ ] Reload: session persists.
- [ ] Force bearer expiry (`UPDATE sessions SET mcp_bearer_expires_at=0`) → re-prompt TOTP works.
- [ ] Safari iOS: login + chat + mobile layout.
- [ ] Chrome Android: hamburger sidebar, safe-area at bottom OK.
- [ ] Light ↔ dark: OS pref toggle reflected live.
- [ ] Logout: cookie cleared, back to `/app/login`.
- [ ] New conversation: "liste les VMs" → tool-call card appears and expands.
- [ ] Switch Flash → Pro → effort medium mid-conversation: subsequent messages use new mode; old ones unchanged.
- [ ] Visual audit: no Unicode emoji anywhere in the UI, only inline SVG icons.

## 13. Suggested delivery staging

Not binding — the implementation plan decides. But this design breaks cleanly into three shippable stages:

1. **Auth & sessions** — DB schema for `sessions`, `SessionStore`, `DashboardSessionMiddleware`, `/app/login`, `/app/refresh`, `/app/logout`. Dashboard enabled but `/app/chat` is a placeholder. Deliverable: "I can log in on my phone and get a cookie."
2. **Chat foundation** — `conversations` + `messages` tables, `/app/api/conversations`, `/app/api/chat/stream` with `FakeGeminiClient` wiring, basic chat UI (no tool cards, no mobile polish). Deliverable: "I can have a text-only conversation persisted in SQLite."
3. **Gemini + MCP + polish** — real `google-genai` integration, tool-call cards with expand/collapse, mobile layout, theme light/dark, auto-title, model/effort selectors. Deliverable: the spec'd UX end-to-end.

## 14. Open questions (non-blocking for v1)

- **SSE resume**: if a network hiccup cuts streaming mid-tool-call, do we attempt resume or always full-retry? v1 does full-retry; if noisy in practice, add a resume protocol on top of SSE `Last-Event-ID`.
- **Conversation export**: explicit non-goal for v1, but consider `GET /app/api/conversations/{id}/export.md` later.
- **Per-session pinning**: "stick to Pro for this device" might be handy later; v1 keeps model per-conversation.
- **Unification with `/oauth/authorize` styling**: the existing OAuth TOTP page uses hardcoded dark colors. Not unified now to keep scope tight, but a future pass could migrate it to the same CSS variables for consistency.

---

**End of spec.**
