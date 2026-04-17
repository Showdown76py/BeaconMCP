# Client setup

BeaconMCP exposes a single MCP endpoint (`https://<your-host>/mcp`) and three
auth paths the dashboard helps you drive:

- **OAuth 2.1 (pre-registered client)** — Claude only. See the main
  [README](../README.md#connecting-clients) for that flow.
- **OAuth + Dynamic Client Registration** — ChatGPT, Perplexity, OpenCode.
  Requires `server.allow_dynamic_registration: true` in `beaconmcp.yaml`.
- **Static bearer token** — Gemini (Web/CLI/Antigravity), Mistral, VS Code,
  Cursor, any HTTP-only MCP client.

> **Security note — always type the TOTP by hand from your phone.**
> The TOTP seed belongs in an authenticator app on a device you physically
> control. Do **not** generate codes programmatically with `oathtool` /
> `pyotp` / a shell alias, and do **not** store the raw seed in a `.env` or
> a secrets manager. Every flow below is designed so you read a 6-digit
> code off your phone. Unattended-service automation is covered separately
> in [totp-automation.md](totp-automation.md).

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

## Perplexity (OAuth + DCR)

Perplexity's custom connector auto-discovers the auth method from the MCP
server's `.well-known` metadata. With
`server.allow_dynamic_registration: true`, point it at a slug URL and
Perplexity completes DCR + the OAuth consent flow automatically.

Requires Perplexity **Pro**, **Max**, or **Enterprise** — custom connectors
aren't on the free tier.

1. Mint a connector URL from `https://<your-host>/app/connectors` (single-use, 15 min TTL).
2. In Perplexity: **Settings → Connectors → Add custom**. Tick *"I understand custom connectors can introduce risks"*.
3. Fill in:
   - **Name:** BeaconMCP
   - **Description:** (optional)
   - **MCP Server URL:** paste the `/mcp/c/<slug>` URL.
4. Perplexity auto-detects OAuth, runs DCR against the slug-gated `/oauth/register/c/<slug>`, then redirects you to BeaconMCP's authorization page. Type your TOTP from your phone.
5. Bearer lifetime: 24 h. Perplexity refreshes via the authorization code flow on its own — you re-type the TOTP each rotation.

Revoke from `https://<your-host>/app/connectors` like any other DCR-derived client.

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

### Gemini CLI

Gemini CLI sends a static `Authorization` header with every call. Create a
token from `/app/tokens`, then:

```bash
gemini mcp add beaconmcp \
  --url https://<your-host>/mcp \
  --header "Authorization: Bearer <token>"
```

Replace the token via the dashboard flow when it expires — do not bake TOTP
generation into a shell alias or wrapper script.

### Gemini Web

In Gemini's custom-MCP panel (*Tools → Extensions → Custom MCP*), paste
`https://<your-host>/mcp` and set the Authorization header to
`Bearer <token>`.

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

### Le Chat

*Intelligence → Connecteurs → Ajouter un connecteur → Connecteur MCP
personnalisé*. Le Chat **auto-detects** the auth method from the server's
`.well-known` metadata. Two supported paths:

- **OAuth 2.1 (recommended)** — enable `allow_dynamic_registration: true`
  on the server and paste a slug URL minted from `/app/connectors`
  (`https://<your-host>/mcp/c/<slug>`). Le Chat runs DCR + the OAuth
  consent flow; you type your TOTP on the authorization page.
- **Bearer** — paste `https://<your-host>/mcp` and a token from
  `/app/tokens`.

Custom connectors are on Le Chat Pro / Enterprise. The free tier may hide
the panel entirely.

**CORS:** add `https://chat.mistral.ai` to `server.allowed_origins`
(see the allowlist note at the top of this file).

### Mistral Vibe

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

## VS Code

VS Code's built-in MCP client picks up servers from workspace or user
settings:

```json
// .vscode/mcp.json  (or settings.json → "mcp.servers")
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

Verify with *Command Palette → MCP: List Servers*. If you rely on GitHub
Copilot's MCP integration, field names may differ — check the extension's
readme.

---

## Cursor

Cursor reads MCP servers from `.cursor/mcp.json` (per-project) or
`~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "beaconmcp": {
      "url": "https://<your-host>/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Reload the Cursor window after editing; the server shows up under
*Settings → Cursor Settings → MCP Servers* with a live status indicator.

---

## Other MCP-over-HTTP clients

Any client that can send a bearer on `https://<your-host>/mcp` works the
same way: create a token from `/app/tokens` after typing your TOTP,
configure the client to send `Authorization: Bearer <token>`, revoke from
the same page when you are done. If the client natively speaks OAuth 2.1
(like Claude) or OAuth + DCR (like ChatGPT / OpenCode), prefer those flows
— they keep the TOTP prompt at the authorization page instead of relying
on a stored bearer.
