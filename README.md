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

**Remote MCP server for Proxmox VE clusters, BMC-managed hardware, and SSH hosts.**

Works with **Assistant** (web, mobile, desktop) &bull; **ChatGPT** &bull; **Gemini** (CLI, API)

[Installation](#installation) &bull; [Connecting clients](#connecting-clients) &bull; [Tools](#available-tools) &bull; [Tests](#tests)

</div>

---

## Overview

BeaconMCP exposes a Proxmox VE cluster, the hardware underneath it (HP iLO, generic IPMI), and arbitrary SSH-reachable hosts as a single Streamable HTTP MCP server. Any MCP-capable client can diagnose a crash, power-cycle a frozen host, create or migrate VMs, and execute commands inside guests or on bare-metal nodes — through a single OAuth 2.1 endpoint.

- **Independent capabilities.** Enable only what you have: a full Proxmox cluster, a couple of VPS reachable by SSH, a rack with IPMI BMCs only, or any combination. The server registers tools per capability, so an SSH-only deployment never exposes `proxmox_*` tools.
- **Three deployment modes out of the box:**
  - *Proxmox + BMC + SSH* — the reference setup (a Proxmox cluster with iLO/IPMI hardware).
  - *SSH-only* — point it at a handful of VPS or bare-metal servers; get the unified `ssh_run` tool backed by per-host credentials.
  - *Proxmox-only* or *BMC-only* — mix and match as your inventory grows.
- **30+ MCP tools** across four modules: Proxmox (monitoring, VM lifecycle, system), SSH (per-host multi-target), BMC (hardware power/health), and security.
- **N nodes, N BMC devices, N SSH hosts.** No hard-coded counts. Each SSH host carries its own credentials (password or key file) and is declared under `ssh.hosts[]`.
- **Backend-agnostic hardware layer.** HP iLO, generic IPMI, and a universal **Redfish REST API** backend ship out of the box. Dell iDRAC (14G+) and Supermicro (X11+) automatically use the Redfish backend.
- **YAML-first configuration** with `${ENV}` references for secrets. Validation runs at startup.
- **OAuth 2.1 + TOTP.** Client credentials with mandatory second factor on every token issuance.
- **Optional web dashboard** — login, API-token management, and an (optional) integrated Gemini chat panel.

---

## Architecture

```
Clients (Assistant, ChatGPT, Gemini)
             │
             │ HTTPS (reverse proxy / tunnel)
             ▼
┌──────────────────────────────────┐
│   BeaconMCP  (HTTP :8420)        │
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

**Recommended deployment:** put BeaconMCP on the **same local network** as your Proxmox cluster — on one of the nodes, in a dedicated LXC / VM, or in a Docker container with host networking (see *Docker* below). That way every `proxmox.nodes[].host` is a plain **LAN IP** (e.g. `10.0.0.1`, `10.0.0.2`), usable as-is for both the Proxmox API (`:8006`) and for SSH (`:22`) — including the `ssh.inherit_proxmox_nodes` shortcut and the `bmc_*` SSH-jump tunnel for HP iLO on a private management VLAN.

Public FQDNs with reverse-proxy ports (`pve2.example.com:443`) pin the entry to HTTPS and break the SSH inheritance — the SSH service is on port 22 of the node, not behind the HTTPS tunnel. For a truly remote node, declare it explicitly under `ssh.hosts[]` with its real SSH address (Tailscale IP, VPN, bastion…).

---

## Requirements

- Python 3.11+
- Proxmox VE 8.x with API tokens provisioned on each node (Datacenter → Permissions → API Tokens)
- *(optional)* `ipmitool` binary on the BeaconMCP host if any IPMI BMC is configured
- *(optional)* reachable jump host (a Proxmox node) for HP iLO devices exposed only on a private management VLAN
- *(optional)* `GEMINI_API_KEY` to enable the integrated chat panel

---

## Installation

Two supported paths: **Docker** (quickest, isolated) or the **bare-metal install script** (native systemd service). Pick whichever fits your infra — they expose the same CLI and HTTP surface.

### Option A — Docker (recommended for most setups)

Requires Docker Engine 20.10+ with the Compose plugin. Runs on the Proxmox node itself, inside an LXC/VM on the same LAN, or on any box that can reach every declared node's API and SSH port directly.

```bash
git clone https://github.com/Showdown76py/BeaconMCP.git
cd BeaconMCP
cp beaconmcp.yaml.example beaconmcp.yaml    # edit for your topology
cp .env.example .env                        # fill in the ${VAR} secrets
docker compose up -d
```

The bundled [`docker-compose.yml`](docker-compose.yml) uses `network_mode: host` so the container sits directly on the LAN — LAN IPs in `proxmox.nodes[].host` just work for both the Proxmox API (`:8006`) and SSH (`:22`), which is what makes the `ssh.inherit_proxmox_nodes` shortcut practical. State (OAuth clients, dashboard DB, usage history) lives in a named volume `beaconmcp-state` and survives container recreation.

Initial setup (run once, while the container is up):

```bash
docker compose exec beaconmcp beaconmcp validate-config
docker compose exec beaconmcp beaconmcp auth create --name "Assistant Web"
curl http://localhost:8420/health        # should return {"status":"ok",...}
```

The container listens on port 8420; put HTTPS + your FQDN in front with any reverse proxy (Caddy, nginx, Traefik, Cloudflare tunnel).

**SSH key files.** If any of your `ssh.hosts[]` entries (or `ssh.defaults`) use `key_file:`, either copy the keys into the `beaconmcp-state` volume and reference them via `/state/keys/...`, or uncomment the `~/.ssh` bind mount in the compose file. Host paths like `~/.ssh/id_ed25519` don't exist inside the container — they're resolved against the container's filesystem.

### Option B — Bare-metal install script

SSH to the Proxmox node that will host BeaconMCP (we recommend your primary node — `pve1` in typical setups), then:

```bash
git clone https://github.com/Showdown76py/BeaconMCP.git /opt/beaconmcp
cd /opt/beaconmcp
sudo bash deploy/install.sh
```

The install script creates a `beaconmcp` system user, installs the package in editable mode, registers a systemd unit, and creates `/opt/beaconmcp` for persistent state.

### 2. Configure

Two ways to produce `beaconmcp.yaml`:

**Guided (TUI wizard).** A terminal UI walks you through each capability (Proxmox nodes, SSH, BMC, server) with a live YAML preview on the right and adds `${VAR}` placeholders to `.env` for the secrets you'll fill in after. The same command also **edits an existing** `beaconmcp.yaml` — it parses the file into the wizard, so you can tweak and re-save without losing anything:

```bash
pip install 'beaconmcp[wizard]'   # pulls the optional textual dep
beaconmcp init                    # creates OR edits beaconmcp.yaml, extends .env
beaconmcp init --blank            # force a fresh draft even if the YAML exists
```

Arrow keys to browse sections, `enter` to open forms, `ctrl+s` to save without quitting, `q` to exit.

**Manual.** Copy the example and edit:

```bash
cp beaconmcp.yaml.example /opt/beaconmcp/beaconmcp.yaml
cp .env.example /opt/beaconmcp/.env
# Edit both: YAML defines the topology, .env holds the secrets.
```

Either way, the YAML declares Proxmox nodes, BMC devices, SSH credentials, the dashboard configuration, and DNS-rebinding allowlists. Secrets are referenced via `${ENV_VAR}` placeholders resolved at startup against the `.env` file. Validate the result without starting the server:

```bash
beaconmcp validate-config
# prints the fully-resolved config with secrets masked, and a one-line summary.
```

### 3. Provision an OAuth client

```bash
beaconmcp auth create --name "Assistant Web"
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

Place BeaconMCP behind a reverse proxy that terminates TLS and forwards the public hostname to `http://localhost:8420`. Declare that hostname under `server.allowed_hosts` in `beaconmcp.yaml`; without it the MCP SDK rejects incoming requests with `421 Misdirected Request` (DNS-rebinding protection). If you're proxying through Cloudflare, add `cloudflare` to `server.trusted_proxies` so BeaconMCP can safely trust forwarded client IPs for auth rate limiting.

### 6. Updating BeaconMCP

Updating BeaconMCP requires pulling the latest code from GitHub and restarting the service.

**For Docker setups:**
```bash
cd BeaconMCP
git pull
docker compose up -d --build
```

**For Bare-metal (systemd) setups:**
The installer script doubles as an updater. It will automatically stash your current state, pull the latest code, install any new dependencies into the virtual environment, and restart the service:
```bash
sudo bash /opt/beaconmcp/deploy/install.sh
```

---

## Connecting clients

> **Security note — always type the TOTP by hand from your phone.**
> The TOTP seed belongs in an authenticator app on a device you physically control (Google Authenticator, Authy, 1Password, Aegis, a YubiKey with OTP, etc.). Do **not** generate codes programmatically with `oathtool` / `pyotp` / a shell alias, and do **not** store the raw seed in a `.env`, a secrets manager, or next to the client secret — doing so collapses the two factors into one and removes the protection TOTP exists to provide. Every flow below is designed so you read a 6-digit code off your phone and type it into either the authorization page or the dashboard.
>
> Unattended services (scheduled jobs, CI pipelines) occasionally need machine-held TOTP. That case — with its required precautions and warnings — is covered separately in [docs/totp-automation.md](docs/totp-automation.md). Read it end-to-end before considering automation.

### Assistant (web, mobile, desktop)

Assistant performs the full OAuth 2.1 flow against BeaconMCP, so there is no long-lived bearer to store on its side — you type the TOTP into the authorization page whenever a new token is issued.

1. **Settings → Integrations → Add custom connector.**
2. Fill in:
   - **Name:** BeaconMCP
   - **Remote MCP server URL:** `https://<your-host>/mcp`
   - **OAuth Client ID** and **OAuth Client Secret** from `beaconmcp auth create`.
3. **Add.**

On first use (and after each 24-hour token expiry) Assistant redirects to the BeaconMCP authorization page. Read the current 6-digit code from your authenticator app and type it in. Assistant never holds the TOTP seed, and a leaked session cannot mint a new token without a fresh code from your phone.

**Important — web-origin allowlist.** Every browser-based MCP client (Assistant Web, ChatGPT, Le Chat, Perplexity, Gemini Web) sends a CORS preflight before it can reach `/mcp`, and OAuth HTTPS `redirect_uri` checks use the same list. Add each client's origin to `server.allowed_origins` in `beaconmcp.yaml` (see [`beaconmcp.yaml.example`](beaconmcp.yaml.example)). Desktop and CLI callback forms (`vscode://`, `cursor://`, loopback) are handled separately.

### Other clients

Full setup for **ChatGPT** (Web / Mobile / Codex CLI), **Gemini** (CLI / Antigravity / API), **Mistral** (Le Chat + Vibe), **OpenCode**, **VS Code**, and **Cursor** lives in [docs/clients.md](docs/clients.md). The dashboard's `/app/tokens` page shows the same snippets interactively. Perplexity is deprecating MCP (March 2026) and is no longer supported.

---

## Dashboard

An optional web panel is mounted under `/app/*` on the same port as the MCP endpoint. It provides TOTP login, an API-token management page (used to wire external clients like the Gemini web UI or ChatGPT MCP without exposing the OAuth flow), and an optional integrated Gemini chat. The chat panel is gated by `GEMINI_API_KEY`; the tokens page works without it.

Full reference: [docs/dashboard.md](docs/dashboard.md). See also: [docs/clients.md](docs/clients.md) for external MCP client configuration.

---

## Configuration

Two files are read at startup:

- **`beaconmcp.yaml`** — topology and feature flags. Path resolution: `--config` flag → `BEACONMCP_CONFIG` env → `./beaconmcp.yaml` → `/etc/beaconmcp/config.yaml`. See [`beaconmcp.yaml.example`](beaconmcp.yaml.example) for the full schema.
- **`.env`** — secrets referenced by the YAML as `${VAR}`. Missing references fail the startup check with the offending YAML path.

Common keys:

| Section | Notes |
|---------|-------|
| `server.allowed_hosts` | DNS-rebinding allowlist — **must** include the public FQDN behind your reverse proxy. |
| `server.allowed_origins` | Web-origin allowlist for browser CORS and OAuth HTTPS redirect URIs. |
| `server.trusted_proxies` | Direct peers allowed to supply `X-Forwarded-For` (IPs or CIDRs). Use `cloudflare` to auto-expand Cloudflare edge ranges. |
| `proxmox.nodes[]` | One entry per Proxmox node. Needs an API token per node. Prefer a **LAN IP** in `host:` (e.g. `10.0.0.1`) — it's the one string that works for both the Proxmox API and for SSH inheritance. `localhost` is OK when BeaconMCP runs directly on that node. Only use an FQDN with a reverse-proxy port (e.g. `:443`) for nodes you can't reach on the LAN, and declare those explicitly under `ssh.hosts[]` with their real SSH address. |
| `ssh.hosts[]` | One entry per SSH target (VPS, Proxmox node, jump box, …). Each entry carries its own `user` + exactly one of `password` / `key_file`. Names may match `proxmox.nodes[].name`. |
| `ssh.defaults` + `ssh.inherit_proxmox_nodes` | Homelab shortcut. Set `defaults:` (user + password/key_file) and flip `inherit_proxmox_nodes: true` — every Proxmox node becomes SSH-reachable under its own name with those defaults, no duplication. Explicit `ssh.hosts[]` entries still win when they match a node by name or address. |
| `ssh.vmid_to_ip` | Optional template (e.g. `"192.168.1.{id}"`) used by `ssh_run` when the `host` argument is a bare VMID. The resolved IP must match an `ssh.hosts[].host` to authenticate. Omit to disable numeric-ID shortcuts. |
| `bmc.devices[]` | Zero or more BMCs. `type` is one of `hp_ilo`, `ipmi`, `idrac` (redfish), `supermicro` (redfish), or `redfish`. `jump_host` is optional — set it to the name of a `proxmox.nodes[]` entry to route the connection over an SSH tunnel. |
| `features.dashboard.limits` | Per-5h and per-week USD caps for the Gemini chat. Set to `0` to disable a window. |

---

## Security: manual review of sensitive actions

> **Never let an LLM execute shell commands on infrastructure you care about without reading the command first.**

BeaconMCP exposes tools that cause irreversible changes: `ssh_run`, `proxmox_run`, `bmc_power_off`, `proxmox_vm_stop`, `proxmox_vm_create`, `vm_bulk_action`, and more. Models do not always grasp the consequences of a command — an errant `rm -rf`, a `systemctl stop` on the wrong unit, a `pct destroy` mistaken for `pct stop`. A few working rules:

- **Disable auto-approve** on every external MCP client (Assistant Desktop, Gemini CLI, ChatGPT MCP). Keep per-call approval enabled; refuse "always allow this tool".
- **Read the `command` argument** before approving any `ssh_run` or `proxmox_run` call. Ask: if this ran against the wrong VM or host, could I recover?
- **The integrated chat** at `/app/chat` already forces human confirmation for every `ssh_run` / `proxmox_run` call that carries a `command` (polling-only calls with just `exec_id` are read-only and skip the modal). Read the arguments shown on the confirmation card even when you click through fast. No answer within 5 minutes counts as refusal.
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
| `proxmox_snapshot_list` | List all snapshots for a VM or container. |
| `proxmox_snapshot_create` | Create a new snapshot. |
| `proxmox_snapshot_rollback` | Rollback a VM/CT to a previous snapshot. |
| `proxmox_snapshot_delete` | Delete an existing snapshot. |
| `proxmox_backup_create` | Trigger a new backup of a VM or container. |
| `proxmox_backup_list` | List available vzdump backup archives on a storage pool. |
| `proxmox_backup_restore` | Restore a VM or container from a backup archive. |

### Proxmox — system (3)

| Tool | Description |
|------|-------------|
| `proxmox_storage_status` | Storage pool status. |
| `proxmox_network_config` | Network configuration per node. |
| `proxmox_run` | Command inside a QEMU VM via QEMU Guest Agent. Sync by default; pass `wait=False` to start async, or `exec_id=` to poll an existing session. For LXC containers, use `ssh_run` on the node with `pct exec <vmid> -- <command>`. |
| `proxmox_read_file` | Safely read a file from a VM (via QEMU Guest Agent). |
| `proxmox_write_file` | Safely write a file to a VM (via QEMU Guest Agent). |
| `proxmox_upload_file` | Stream a file ≤ `server.transfers_max_mb` (default 500 MB) from the staging dir into a VM (SFTP) or CT (SFTP + `pct push`). Verifies SHA-256 by default. |
| `proxmox_download_file` | Stream a file ≤ `server.transfers_max_mb` from a VM (SFTP) or CT (`pct pull` + SFTP) into the staging dir. Verifies SHA-256 by default. |
| `proxmox_list_transfers` | List files currently in the staging directory. |

### SSH fallback (2)

| Tool | Description |
|------|-------------|
| `ssh_run` | Command on a host via SSH. `host` accepts node names, VMIDs, hostnames, or IPs. Sync by default; pass `wait=False` to start async, or `exec_id=` to poll. |
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
