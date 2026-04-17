<div align="center">

# BeaconMCP

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![MCP Protocol](https://img.shields.io/badge/MCP-Model_Context_Protocol-5A67D8)](https://modelcontextprotocol.io/)
[![Proxmox VE](https://img.shields.io/badge/Proxmox-VE_8.x-E57000?logo=proxmox&logoColor=white)](https://www.proxmox.com/)
[![HP iLO](https://img.shields.io/badge/HP-iLO_4%2F5-0096D6?logo=hp&logoColor=white)](https://www.hpe.com/us/en/servers/integrated-lights-out-ilo.html)
[![IPMI](https://img.shields.io/badge/IPMI-2.0-4E5D70)](https://en.wikipedia.org/wiki/Intelligent_Platform_Management_Interface)
[![ChatGPT](https://img.shields.io/badge/ChatGPT-Compatible-74AA9C?logo=openai&logoColor=white)](https://chatgpt.com/)
[![Gemini](https://img.shields.io/badge/Gemini-Compatible-4285F4?logo=google&logoColor=white)](https://gemini.google.com/)
[![License](https://img.shields.io/badge/license-Apache_2.0_%2B_Commons_Clause-red)](LICENSE)

**Remote MCP server for Proxmox VE clusters and BMC-managed hardware.**

Works with **Claude** (web, mobile, desktop) &bull; **ChatGPT** &bull; **Gemini** (CLI, API)

[Installation](#installation) &bull; [Connecting clients](#connecting-clients) &bull; [Tools](#available-tools) &bull; [Tests](#tests)

</div>

---

## Overview

BeaconMCP exposes a Proxmox VE cluster and the hardware underneath it (HP iLO, generic IPMI) as a single Streamable HTTP MCP server. Any MCP-capable client can diagnose a crash, power-cycle a frozen host, create or migrate VMs, and execute commands inside guests or on the bare-metal nodes — through a single OAuth 2.1 endpoint.

- **30 MCP tools** across four modules: Proxmox (monitoring, VM lifecycle, system), SSH fallback, and BMC hardware management.
- **N nodes, N BMC devices.** Declare as many Proxmox nodes as your cluster has, and as many BMC endpoints (HP iLO, IPMI) as you manage. No hard-coded node counts.
- **Backend-agnostic hardware layer.** HP iLO and generic IPMI ship out of the box; Dell iDRAC and Supermicro are pluggable stubs.
- **YAML-first configuration** with `${ENV}` references for secrets. Validation runs at startup.
- **OAuth 2.1 + TOTP.** Client credentials with mandatory second factor on every token issuance.
- **Optional web dashboard** — login, API-token management, and an (optional) integrated Gemini chat panel.

---

## Architecture

```
Clients (Claude, ChatGPT, Gemini)
        │
        │ HTTPS (reverse proxy / tunnel)
        ▼
┌──────────────────────────────────┐
│   BeaconMCP  (HTTP :8420)         │
│   ├── proxmox/   → Proxmox API   │
│   ├── ssh/       → SSH :22       │
│   ├── bmc/       → iLO / IPMI    │
│   └── dashboard/ → /app/*        │
└──────────────────────────────────┘
        │
        │ managed cluster
        ▼
  Proxmox nodes (N)  ·  BMC devices (N)
```

BeaconMCP runs on any host that can reach the Proxmox API of every declared node and the BMC management network. It speaks MCP over Streamable HTTP and is typically placed behind a reverse proxy with DNS-rebinding protection configured via `server.allowed_hosts` in the YAML.

**Recommended deployment:** run BeaconMCP **directly on one of your Proxmox nodes** (the primary one, conventionally `pve1`). That node becomes addressable as `host: localhost` in the YAML — both the Proxmox API (`:8006`) and SSH (`:22`) are reachable without a reverse proxy or tunnel, which also lets the `bmc_*` SSH-jump tunnel feature (HP iLO on a private management VLAN) work without extra configuration. Remote nodes in the cluster keep using their public FQDN.

---

## Requirements

- Python 3.11+
- Proxmox VE 8.x with API tokens provisioned on each node (Datacenter → Permissions → API Tokens)
- *(optional)* `ipmitool` binary on the BeaconMCP host if any IPMI BMC is configured
- *(optional)* reachable jump host (a Proxmox node) for HP iLO devices exposed only on a private management VLAN
- *(optional)* `GEMINI_API_KEY` to enable the integrated chat panel

---

## Installation

### 1. Install

SSH to the Proxmox node that will host BeaconMCP (we recommend your primary node — pve1 in typical setups), then:

```bash
git clone https://github.com/Showdown76py/BeaconMCP.git /opt/beaconmcp
cd /opt/beaconmcp
sudo bash deploy/install.sh
```

The install script creates a `beaconmcp` system user, installs the package in editable mode, registers a systemd unit, and creates `/opt/beaconmcp` for persistent state.

### 2. Configure

```bash
cp beaconmcp.yaml.example /opt/beaconmcp/beaconmcp.yaml
cp .env.example /opt/beaconmcp/.env
# Edit both: YAML defines the topology, .env holds the secrets.
```

The YAML declares Proxmox nodes, BMC devices, SSH credentials, the dashboard configuration, and DNS-rebinding allowlists. Secrets are referenced via `${ENV_VAR}` placeholders resolved at startup against the `.env` file. Validate the result without starting the server:

```bash
beaconmcp validate-config
# prints the fully-resolved config with secrets masked, and a one-line summary.
```

### 3. Provision an OAuth client

```bash
beaconmcp auth create --name "Claude Web"
```

The CLI prints a client id, a client secret, and a TOTP seed (with an ASCII QR code). **Both secrets are displayed exactly once.** Scan the QR into an authenticator app (Google Authenticator, Authy, 1Password) immediately, or store the raw seed in a secrets manager.

Repeat for each MCP client that should have access (ChatGPT, Gemini, etc.). Clients are listed and revoked with:

```bash
beaconmcp auth list
beaconmcp auth revoke <client_id>
```

### 4. Start the server

```bash
sudo systemctl enable --now beaconmcp
curl http://localhost:8420/health
# {"status":"ok","server":"beaconmcp"}
```

### 5. Expose publicly

Place BeaconMCP behind a reverse proxy that terminates TLS and forwards the public hostname to `http://localhost:8420`. Declare that hostname under `server.allowed_hosts` in `beaconmcp.yaml`; without it the MCP SDK rejects incoming requests with `421 Misdirected Request` (DNS-rebinding protection).

---

## Connecting clients

> **Security note — always type the TOTP by hand from your phone.**
> The TOTP seed belongs in an authenticator app on a device you physically control (Google Authenticator, Authy, 1Password, Aegis, a YubiKey with OTP, etc.). Do **not** generate codes programmatically with `oathtool` / `pyotp` / a shell alias, and do **not** store the raw seed in a `.env`, a secrets manager, or next to the client secret — doing so collapses the two factors into one and removes the protection TOTP exists to provide. Every flow below is designed so you read a 6-digit code off your phone and type it into either the authorization page or the dashboard.
>
> Unattended services (scheduled jobs, CI pipelines) occasionally need machine-held TOTP. That case — with its required precautions and warnings — is covered separately in [docs/totp-automation.md](docs/totp-automation.md). Read it end-to-end before considering automation.

### Claude (web, mobile, desktop)

Claude performs the full OAuth 2.1 flow against BeaconMCP, so there is no long-lived bearer to store on its side — you type the TOTP into the authorization page whenever a new token is issued.

1. **Settings → Integrations → Add custom connector.**
2. Fill in:
   - **Name:** BeaconMCP
   - **Remote MCP server URL:** `https://<your-host>/mcp`
   - **OAuth Client ID** and **OAuth Client Secret** from `beaconmcp auth create`.
3. **Add.**

On first use (and after each 24-hour token expiry) Claude redirects to the BeaconMCP authorization page. Read the current 6-digit code from your authenticator app and type it in. This is the recommended integration: Claude never holds the TOTP seed, and a leaked session cannot mint a new token without a fresh code from your phone.

### ChatGPT

ChatGPT's Developer Mode connector only accepts **OAuth with Dynamic Client Registration (RFC 7591)** — it will not take a pre-provisioned `client_id` / `client_secret` nor a static bearer header. BeaconMCP supports this by minting a one-off bootstrap URL from the dashboard: the URL lets ChatGPT register a derived OAuth client tied to your account. 2FA is preserved — at authorization time, you still type your own TOTP from your phone; the derived client has no TOTP seed of its own.

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

### Gemini CLI

Gemini CLI sends a static `Authorization` header with every call, so the bearer is generated the same way as for ChatGPT.

1. In the dashboard (`/app/tokens`), authenticate with your TOTP from your phone, then create a new token named `gemini-cli`.
2. Register the MCP server:

   ```bash
   gemini mcp add beaconmcp \
     --url https://<your-host>/mcp \
     --header "Authorization: Bearer <token>"
   ```

3. Replace the token via the same dashboard flow when it expires — do not bake TOTP generation into a shell alias or wrapper script.

### Gemini API (google-genai SDK)

For programmatic Gemini API usage, the BeaconMCP server is passed as a remote MCP tool. The SDK needs an `Authorization` header at call time; obtain the bearer interactively from the dashboard rather than letting the process derive TOTP codes on its own.

1. Create a dashboard token as in the Gemini CLI section above.
2. Put the resulting bearer in your environment (e.g. `BEACONMCP_TOKEN`) or in your secrets manager. **Do not put the TOTP seed there.**
3. Reference it when invoking the model:

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

4. When the bearer expires, re-issue it through the dashboard. Long-running services should rotate tokens on a schedule (an operator typing the TOTP) rather than embedding the seed.

### Google Antigravity

Antigravity reads MCP servers from `~/.gemini/antigravity/mcp_config.json` (macOS / Linux) or `%USERPROFILE%\.gemini\antigravity\mcp_config.json` (Windows). The top-level key is `mcpServers` and the HTTP URL field is **`serverUrl`** (not `url`):

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

If the native HTTP transport misbehaves, fall back to the `mcp-remote` proxy:

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

### Mistral

**Le Chat** — *Settings → Connectors → Add custom MCP server*. Le Chat auto-detects the auth method supported by the server; pick Bearer, paste your dashboard-issued token, and set the URL to `https://<your-host>/mcp`. Custom connectors are on Le Chat Pro / Enterprise plans.

**Mistral Vibe** — Vibe reads its config from `./.vibe/config.toml` (per-project) or `~/.vibe/config.toml` (global). **TOML format**, not JSON:

```toml
[[mcp_servers]]
name = "beaconmcp"
transport = "http"
url = "https://<your-host>/mcp"
headers = { "Authorization" = "Bearer <token>" }
```

`transport` accepts `"http"`, `"streamable-http"`, or `"stdio"`. Each server is its own `[[mcp_servers]]` array entry.

### OpenCode

OpenCode reads MCP servers from `opencode.json` (or `~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "beaconmcp": {
      "type": "remote",
      "url": "https://<your-host>/mcp",
      "enabled": true,
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

OpenCode also natively supports OAuth with Dynamic Client Registration. If you enable `allow_dynamic_registration` on the server, you can point OpenCode at a slug URL (`https://<your-host>/mcp/c/<slug>` minted from `/app/connectors`) with `"oauth": true` and skip the bearer entirely. Tokens are stashed in `~/.local/share/opencode/mcp-auth.json`.

### VS Code

VS Code's built-in MCP client picks up servers from workspace or user settings:

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

Verify with *Command Palette → MCP: List Servers*. If you rely on GitHub Copilot's MCP integration, field names may differ — check the extension's readme.

### Cursor

Cursor reads MCP servers from `.cursor/mcp.json` (per-project) or `~/.cursor/mcp.json` (global):

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

Reload the Cursor window after editing; the server shows up under *Settings → Cursor Settings → MCP Servers* with a live status indicator.

### Other MCP-over-HTTP clients

Any client that can send a bearer on `https://<your-host>/mcp` works the same way: create a token from `/app/tokens` after typing your TOTP, configure the client to send `Authorization: Bearer <token>`, revoke from the same page when you are done. If the client natively speaks OAuth 2.1 (like Claude), prefer that flow — it keeps the TOTP prompt at the authorization page instead of relying on a stored bearer.

---

## Dashboard

An optional web panel is mounted under `/app/*` on the same port as the MCP endpoint. It provides TOTP login, an API-token management page (used to wire external clients like the Gemini web UI or ChatGPT MCP without exposing the OAuth flow), and an optional integrated Gemini chat. The chat panel is gated by `GEMINI_API_KEY`; the tokens page works without it.

Full reference: [docs/dashboard.md](docs/dashboard.md).

---

## Configuration

Two files are read at startup:

- **`beaconmcp.yaml`** — topology and feature flags. Path resolution: `--config` flag → `BEACONMCP_CONFIG` env → `./beaconmcp.yaml` → `/etc/beaconmcp/config.yaml`. See [`beaconmcp.yaml.example`](beaconmcp.yaml.example) for the full schema.
- **`.env`** — secrets referenced by the YAML as `${VAR}`. Missing references fail the startup check with the offending YAML path.

Common keys:

| Section | Notes |
|---------|-------|
| `server.allowed_hosts` | DNS-rebinding allowlist — **must** include the public FQDN behind your reverse proxy. |
| `server.allowed_origins` | CORS allowlist for browser-based MCP clients. |
| `proxmox.nodes[]` | One entry per Proxmox node. Needs an API token per node. For the host BeaconMCP itself runs on, use `host: localhost` — both the API (`:8006`) and SSH (`:22`) are reachable locally without going through any reverse proxy or tunnel. Remote nodes in the cluster use their FQDN (append `:443` if a reverse proxy terminates the API). |
| `ssh.vmid_to_ip` | Optional template (e.g. `"192.168.1.{id}"`) used by `ssh_exec_command` when the `host` argument is a bare VMID. Omit to disable numeric-ID shortcuts. |
| `bmc.devices[]` | Zero or more BMCs. `type` is one of `hp_ilo`, `ipmi`, `idrac` (stub), `supermicro` (stub). `jump_host` is optional — set it to the name of a `proxmox.nodes[]` entry to route the connection over an SSH tunnel. |
| `features.dashboard.limits` | Per-5h and per-week USD caps for the Gemini chat. Set to `0` to disable a window. |

---

## Security: manual review of sensitive actions

> **Never let an LLM execute shell commands on infrastructure you care about without reading the command first.**

BeaconMCP exposes tools that cause irreversible changes: `ssh_exec_command*`, `proxmox_exec_command*`, `bmc_power_off`, `proxmox_vm_stop`, `proxmox_vm_create`, and more. Models do not always grasp the consequences of a command — an errant `rm -rf`, a `systemctl stop` on the wrong unit, a `pct destroy` mistaken for `pct stop`. A few working rules:

- **Disable auto-approve** on every external MCP client (Claude Desktop, Gemini CLI, ChatGPT MCP). Keep per-call approval enabled; refuse "always allow this tool".
- **Read the `command` argument** before approving any `ssh_exec_command*` or `proxmox_exec_command*` call. Ask: if this ran against the wrong VM or host, could I recover?
- **The integrated chat** at `/app/chat` already forces human confirmation for every `ssh_exec_command*` and `proxmox_exec_command*` call. Read the arguments shown on the confirmation card even when you click through fast. No answer within 5 minutes counts as refusal.
- **Prefer read-only tools** (`*_list_*`, `*_status`, `*_get_*`, `get_logs`, `health_status`) for exploration — they cannot break anything and are never gated by confirmation.
- **Do not share a `/app/tokens` bearer** with a client you do not fully control. A leaked token grants arbitrary shell access on your Proxmox nodes for 24 hours.

`systemctl restart beaconmcp` invalidates every in-memory bearer. When in doubt about a token, restart is the panic lever.

---

## Available tools

### Proxmox — monitoring (6)

| Tool | Description |
|------|-------------|
| `proxmox_list_nodes` | List cluster nodes and their status. |
| `proxmox_node_status` | CPU, memory, disk, uptime of a single node. |
| `proxmox_list_vms` | List every VM and container across the cluster. |
| `proxmox_vm_status` | Detailed state of a VM or container. |
| `proxmox_get_logs` | System or task logs. |
| `proxmox_get_tasks` | Recent task history. |

### Proxmox — VM lifecycle (7)

| Tool | Description |
|------|-------------|
| `proxmox_vm_start` | Start a VM or container. |
| `proxmox_vm_stop` | Stop (clean or forced). |
| `proxmox_vm_restart` | Restart. |
| `proxmox_vm_create` | Provision a new VM or container. |
| `proxmox_vm_clone` | Clone an existing one. |
| `proxmox_vm_migrate` | Migrate across nodes. |
| `proxmox_vm_config` | Read or update configuration. |

### Proxmox — system (5)

| Tool | Description |
|------|-------------|
| `proxmox_storage_status` | Storage pool status. |
| `proxmox_network_config` | Network configuration per node. |
| `proxmox_exec_command` | Command inside a VM or container (sync, via QEMU Guest Agent). |
| `proxmox_exec_command_async` | Long-running command (async). |
| `proxmox_exec_get_result` | Fetch the result of an async command. |

### SSH fallback (4)

| Tool | Description |
|------|-------------|
| `ssh_exec_command` | Command on a host (sync). `host` accepts node names, VMIDs, hostnames, or IPs. |
| `ssh_exec_command_async` | Long-running command (async). |
| `ssh_exec_get_result` | Fetch the result of an async SSH command. |
| `ssh_list_sessions` | List active and recent SSH sessions. |

### BMC — hardware management (8)

| Tool | Description |
|------|-------------|
| `bmc_list_devices` | List configured BMCs (`id`, `type`). Call first to discover valid `device_id` values. |
| `bmc_server_info` | Server model, serial, firmware. |
| `bmc_health_status` | Temperatures, fans, power supplies, disks, memory. |
| `bmc_power_status` | Current physical power state. |
| `bmc_power_on` | Power on. |
| `bmc_power_off` | ACPI shutdown (or `force=true` to cut power). |
| `bmc_power_reset` | Hard reset. |
| `bmc_get_event_log` | BMC event log (default 50, max 200). |

Each `bmc_*` action tool takes a `device_id` argument. When only one device is configured, `device_id` is optional and defaults to that device.

---

## Tests

The project ships unit tests (`pytest`) for the dashboard and configuration, plus an integration script (`python tests/test_integration.py`) that exercises a live Proxmox cluster. Flags, prerequisites, and fixtures are documented in [docs/tests.md](docs/tests.md).

---

## Troubleshooting

Common errors, their causes, and the fixes that worked are in [docs/troubleshooting.md](docs/troubleshooting.md).

---

## License

[Apache 2.0 with Commons Clause](LICENSE) — use, fork, and modification are free, but **reselling the software (including as a hosted service) requires a separate commercial license**. The code remains source-available.
