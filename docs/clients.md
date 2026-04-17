# Client setup

BeaconMCP exposes a single MCP endpoint (`https://<your-host>/mcp`) and three
auth paths the dashboard helps you drive:

- **OAuth 2.1 (pre-registered client)** — Claude only. See the main
  [README](../README.md#connecting-clients) for that flow.
- **OAuth + Dynamic Client Registration** — ChatGPT (Web / Mobile / Codex),
  Gemini CLI, OpenCode, Cursor, VS Code. Requires
  `server.allow_dynamic_registration: true` in `beaconmcp.yaml`.
- **Static bearer token** — Gemini (Web/CLI/Antigravity), Mistral, VS Code,
  Cursor, any HTTP-only MCP client.

> **Security note — always type the TOTP by hand from your phone.**
> The TOTP seed belongs in an authenticator app on a device you physically
> control. Do **not** generate codes programmatically with `oathtool` /
> `pyotp` / a shell alias, and do **not** store the raw seed in a `.env` or
> a secrets manager. Every flow below is designed so you read a 6-digit
> code off your phone. Unattended-service automation is covered separately
> in [totp-automation.md](totp-automation.md).

> **Trusted redirect URIs — hard-coded allowlist on the server.**
> Every `redirect_uri` reaching `/oauth/authorize` or `/oauth/register/c/<slug>`
> is checked against a fixed list in
> [`src/beaconmcp/auth.py`](../src/beaconmcp/auth.py) (constant
> `TRUSTED_REDIRECT_PREFIXES`). It covers every client documented here
> — consumer and enterprise web URLs (claude.ai, chatgpt.com,
> chat.mistral.ai, …), the OS URI schemes used by desktop clients
> (`vscode://`, `cursor://`), and HTTP loopback for CLI tools
> (`http://localhost:*`, `http://127.0.0.1:*`). If a new client shows
> "invalid_redirect_uri" during DCR or "redirect_uri origin not on the
> BeaconMCP trusted-origin allowlist" at `/oauth/authorize`, add its
> origin to that constant and restart. This check exists because DCR
> would otherwise let any caller register an attacker-controlled
> callback.

> **CORS allowlist — required for every web client.**
> Browser-based MCP clients (Claude Web, ChatGPT Web, Le Chat, Perplexity,
> Gemini Web) fire a CORS preflight before they can reach `/mcp`. If the
> request origin is missing from `server.allowed_origins` in
> `beaconmcp.yaml`, every call fails silently with a browser console
> error. Add each web client's origin explicitly:
>
> ```yaml
> server:
>   allowed_origins:
>     - https://claude.ai
>     - https://chatgpt.com
>     - https://chat.mistral.ai
>     - https://www.perplexity.ai
>     - https://gemini.google.com
> ```
>
> Desktop / CLI clients (Claude Desktop, Gemini CLI, Cursor, VS Code,
> Mistral Vibe, OpenCode) are not browser-based and don't need an entry.

The dashboard's [`/app/tokens`](../src/beaconmcp/dashboard/templates/tokens.html)
page presents the same information with copy-pasteable snippets per platform
— this document is the offline reference.

---

## ChatGPT (OAuth + DCR)

ChatGPT's Developer Mode connector only accepts **OAuth with Dynamic Client
Registration (RFC 7591)** — it will not take a pre-provisioned
`client_id` / `client_secret` nor a static bearer header. BeaconMCP supports
this by minting a one-off bootstrap URL from the dashboard: the URL lets
ChatGPT register a derived OAuth client tied to your account. 2FA is
preserved — at authorization time, you still type your own TOTP from your
phone; the derived client has no TOTP seed of its own.

**One-time setup:**

1. Enable the feature in `beaconmcp.yaml`:
   ```yaml
   server:
     allow_dynamic_registration: true
   ```
   Then restart `beaconmcp serve`.

**To add ChatGPT (from your phone, no laptop needed):**

1. In your mobile browser, open `https://<your-host>/app/connectors`, sign in with your TOTP from your authenticator app.
2. Enter a label (e.g. `ChatGPT iPhone`), type your current TOTP, submit. You get a one-off URL of the shape `https://<your-host>/mcp/c/<slug>`. The URL is **single-use** and expires in 15 min.
3. In the ChatGPT app: **Settings → Connectors → Add custom**.
   - **Name:** BeaconMCP
   - **URL:** paste the `/mcp/c/<slug>` URL.
   - **Authentication:** OAuth.
4. ChatGPT fetches the OAuth metadata, POSTs to the slug-gated `/oauth/register/c/<slug>` — BeaconMCP consumes the slug atomically and mints a derived client scoped to your account.
5. ChatGPT then redirects you to BeaconMCP's authorization page. Type your TOTP from your phone. Token lifetime: 24 h.
6. From now on, ChatGPT auto-refreshes via the authorization code flow. Every 24 h it re-prompts for your TOTP — no re-registration, no new slug.

**Revocation:** `https://<your-host>/app/connectors` lists every active derived client. Revoke one and ChatGPT loses access immediately. Revoking your human account cascades to every derived client automatically.

**Why not a static bearer?** ChatGPT's connector UI has no "Authorization header" field — only "No authentication" or "OAuth" — and the OAuth path strictly requires DCR. The slug-gated bootstrap is the narrow, audit-friendly way to let it in while keeping your TOTP on your phone.

---

## Perplexity (not supported)

> ⚠ Perplexity is deprecating MCP. In March 2026, Perplexity's CTO
> announced that the company is moving to direct REST APIs and a
> "Code Mode" execution model, citing OAuth / DCR friction and
> context-window waste from MCP tool schemas. No setup instructions
> here — there is no working integration to document.

## ChatGPT Codex (OAuth + DCR, terminal/IDE)

Codex is OpenAI's terminal/IDE MCP client. It speaks full OAuth 2.1 and
catches the redirect on an ephemeral local port.

1. Add BeaconMCP to Codex's `config.toml`:
   ```toml
   [mcp_servers.beaconmcp]
   url = "https://<your-host>/mcp/c/<slug>"
   ```
2. Run `codex mcp login beaconmcp`. Codex binds a loopback listener and
   opens your browser on BeaconMCP's authorization page.
3. Type your TOTP. Codex caches the token locally and refreshes on its
   own.

**Remote dev environments** (Codespaces, SSH container): set
`mcp_oauth_callback_url` in `config.toml` to your ingress URL so the
redirect hits the right host instead of localhost. A matching port can
be pinned via `mcp_oauth_callback_port`.

---

## OpenCode (OAuth + DCR)

OpenCode natively handles OAuth with Dynamic Client Registration. Point it
at a slug URL minted from `/app/connectors` and it auto-registers on first
use. Requires `server.allow_dynamic_registration: true`.

```json
// opencode.json (or ~/.config/opencode/opencode.json)
{
  "mcp": {
    "beaconmcp": {
      "type": "remote",
      "url": "https://<your-host>/mcp/c/<slug>",
      "enabled": true,
      "oauth": true
    }
  }
}
```

Auth tokens land in `~/.local/share/opencode/mcp-auth.json`. Connector
URLs are single-use and expire in 15 min — mint a fresh one per install.

---

## Gemini

### Gemini CLI (OAuth + DCR, recommended)

Gemini CLI speaks full OAuth 2.1 with Dynamic Client Registration — drop
a remote URL in `settings.json`, run `/mcp auth <name>`, and the CLI
opens a browser to BeaconMCP's authorization page (TOTP prompt).

```json
// ~/.gemini/settings.json
{
  "mcpServers": {
    "beaconmcp": {
      "httpUrl": "https://<your-host>/mcp/c/<slug>"
    }
  }
}
```

Then: `/mcp auth beaconmcp`. Mint the slug from `/app/connectors`.

Bearer header is also supported if you prefer it:

```bash
gemini mcp add beaconmcp \
  --url https://<your-host>/mcp \
  --header "Authorization: Bearer <token>"
```

### Gemini Web / Mobile / macOS native app (not supported yet)

Gemini's consumer web UI (gemini.google.com), the iOS / Android apps, and
the new macOS native app do **not** expose a custom-MCP connector today.
The only Gemini surfaces that can reach BeaconMCP are **Gemini CLI** and
**Antigravity**.

### Gemini API (google-genai SDK)

For programmatic Gemini API usage, BeaconMCP is passed as a remote MCP
tool. Obtain the bearer interactively from the dashboard rather than
letting the process derive TOTP codes on its own.

```python
import os
from google import genai

token = os.environ["BEACONMCP_TOKEN"]

client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="List the VMs on pve1",
    config={
        "tools": [
            {
                "mcp_servers": [
                    {
                        "url": "https://<your-host>/mcp",
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                ]
            }
        ]
    },
)
```

Long-running services should rotate tokens on a schedule (an operator
typing the TOTP) rather than embedding the seed.

### Google Antigravity

Antigravity reads MCP servers from `~/.gemini/antigravity/mcp_config.json`
(macOS / Linux) or `%USERPROFILE%\.gemini\antigravity\mcp_config.json`
(Windows). The top-level key is `mcpServers` and the HTTP URL field is
**`serverUrl`** (not `url`):

```json
{
  "mcpServers": {
    "beaconmcp": {
      "serverUrl": "https://<your-host>/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

If the native HTTP transport misbehaves, fall back to the `mcp-remote`
proxy with command-based config:

```json
{
  "mcpServers": {
    "beaconmcp": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://<your-host>/mcp",
        "--header", "Authorization: Bearer <token>"
      ]
    }
  }
}
```

---

## Mistral

### Le Chat (OAuth 2.1)

Le Chat speaks OAuth 2.1 natively. Same flow as Claude — point it at
the bare `/mcp` URL and it handles the rest.

1. In Le Chat: *Intelligence → Connecteurs → Ajouter un connecteur → Connecteur MCP personnalisé*.
2. Fill in:
   - **Name:** BeaconMCP
   - **Description:** (optional)
   - **MCP Server URL:** `https://<your-host>/mcp`
3. Validate. Le Chat discovers the OAuth metadata and redirects you to
   BeaconMCP's authorization page — type your TOTP from your phone.
   Token lifetime: 24 h; Le Chat refreshes via the authorization code
   flow on its own.

Custom connectors are on Le Chat Pro / Enterprise; the free tier may
hide the panel.

**CORS:** add `https://chat.mistral.ai` to `server.allowed_origins`
(see the allowlist note at the top of this file).

### Mistral Vibe

> ⚠ Unverified — Vibe's bearer support hasn't been tested against a
> live BeaconMCP instance. If it doesn't work out of the box, check the
> latest Vibe docs (the schema has been iterating fast) and report back.

Vibe reads its config from `./.vibe/config.toml` (per-project) or
`~/.vibe/config.toml` (global). **TOML format**, not JSON:

```toml
[[mcp_servers]]
name = "beaconmcp"
transport = "http"
url = "https://<your-host>/mcp"
headers = { "Authorization" = "Bearer <token>" }
```

`transport` accepts `"http"`, `"streamable-http"`, or `"stdio"`. Each
server is its own `[[mcp_servers]]` array entry.

---

## VS Code (OAuth + DCR)

VS Code routes MCP authentication through its native Authentication
Provider system — the same flow used for your GitHub / Microsoft Entra
logins. It reads `WWW-Authenticate`, shows a toast to Allow, catches
the redirect on the `vscode://` (or `vscode-insiders://`) OS URI scheme,
and stores the resulting token in your OS keychain.

**Recommended — OAuth + DCR:**

```json
// .vscode/mcp.json  (or settings.json → "mcp.servers")
{
  "servers": {
    "beaconmcp": {
      "type": "http",
      "url": "https://<your-host>/mcp/c/<slug>"
    }
  }
}
```

Mint the slug from `/app/connectors`. VS Code prompts you to sign in the
first time the server is used; revoke anytime from the Accounts menu
(profile icon, bottom left).

**Alternative — Bearer:**

```json
{
  "servers": {
    "beaconmcp": {
      "type": "http",
      "url": "https://<your-host>/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Verify with *Command Palette → MCP: List Servers*.

---

## Cursor (OAuth + DCR)

Cursor is a first-class OAuth 2.1 client since v1.0. Drop a remote URL
in `mcp.json`, Cursor surfaces a blue *Connect* button in
*Settings → Tools & MCP* when it detects the 401, pops a browser for
consent (PKCE + DCR are automatic), and catches the redirect via the
`cursor://` OS scheme (or a loopback fallback).

**Recommended — OAuth + DCR:**

```json
// ~/.cursor/mcp.json (global) or .cursor/mcp.json (per project)
{
  "mcpServers": {
    "beaconmcp": {
      "url": "https://<your-host>/mcp/c/<slug>"
    }
  }
}
```

Mint the slug from `/app/connectors`. Click *Connect* in Cursor's
settings when it surfaces the "Needs authentication" state.

**Alternative — Bearer:**

```json
{
  "mcpServers": {
    "beaconmcp": {
      "url": "https://<your-host>/mcp",
      "headers": {
        "Authorization": "Bearer ${env:BEACONMCP_TOKEN}"
      }
    }
  }
}
```

Cursor expands `${env:VAR}` natively so the bearer can live in your
shell environment rather than in the repo.

---

## Other MCP-over-HTTP clients

Any client that can send a bearer on `https://<your-host>/mcp` works the
same way: create a token from `/app/tokens` after typing your TOTP,
configure the client to send `Authorization: Bearer <token>`, revoke from
the same page when you are done. If the client natively speaks OAuth 2.1
(like Claude) or OAuth + DCR (like ChatGPT / OpenCode), prefer those flows
— they keep the TOTP prompt at the authorization page instead of relying
on a stored bearer.
