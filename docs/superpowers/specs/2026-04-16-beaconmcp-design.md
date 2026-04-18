# BeaconMCP -- Proxmox Infrastructure MCP Server

> **Note (post-v1):** the three-tool exec surface described below
> (`proxmox_exec_command` / `_async` / `_get_result` and its SSH twin)
> has been **superseded by the unified `proxmox_run` and `ssh_run`
> tools**. Each unified tool exposes the same three call patterns via
> parameters: sync (default), async start (`wait=False`), and poll
> (`exec_id=…`). The design intent -- auto-detect VM vs CT, in-memory
> session registry, timeout fallback to async -- is preserved; only the
> public tool names changed. See `README.md` § *Available tools* and
> `docs/dashboard.md` for the current contract.

## Context

BeaconMCP is an MCP server that gives Claude direct access to a Proxmox VE infrastructure for diagnostics, VM management, system administration, and hardware management. The motivation: when a server crashes or misbehaves, Claude should be able to diagnose the issue, check hardware health, and propose/execute resolutions -- rather than the user having to manually SSH, check logs, and relay information back and forth.

**Infrastructure:**
- **pve1.example.com** -- Proxmox VE node (active), exposed on the internet via HTTPS
- **pve2.example.com** -- Proxmox VE node (currently down)
- **iLO 4** -- HP Integrated Lights-Out, one unit, accessible only from the local network (not publicly exposed)
- **Zyxel USG 210** -- Firewall, deferred from v1 (no REST API available)

## Architecture

Single Python MCP server (`beaconmcp`) with modular design, running in **stdio** mode. Three core modules:

```
src/beaconmcp/
├── __init__.py
├── __main__.py           # Entry point
├── server.py             # FastMCP server, registers all tools
├── config.py             # Environment variable loading & validation
├── proxmox/
│   ├── __init__.py
│   ├── client.py          # proxmoxer wrapper, connection management
│   ├── vms.py             # VM/CT lifecycle tools
│   ├── monitoring.py      # Node & VM monitoring tools
│   └── system.py          # Storage, network, command execution tools
├── ilo/
│   ├── __init__.py
│   └── client.py          # python-hpilo wrapper + SSH tunnel management
└── ssh/
    ├── __init__.py
    └── client.py           # asyncssh wrapper, session management
```

**Dependencies:**
- `mcp` -- MCP Python SDK (FastMCP)
- `proxmoxer` + `requests` -- Proxmox VE API client
- `python-hpilo` -- HP iLO 4 management (synchronous library, runs in asyncio executor)
- `asyncssh` -- Async SSH connections
- `python-dotenv` -- Environment variable loading

## MCP Tools

### Module Proxmox -- Monitoring & Diagnostic

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `proxmox_list_nodes` | List cluster nodes with status (online/offline/unknown) | -- |
| `proxmox_node_status` | Detailed node status: CPU, RAM, disk, uptime, kernel version, PVE version | `node` |
| `proxmox_list_vms` | List all VMs/CTs with status, resource usage | `node` (optional, all nodes if omitted) |
| `proxmox_vm_status` | Detailed VM/CT status: CPU, RAM, disk I/O, network I/O, uptime | `node`, `vmid` |
| `proxmox_get_logs` | Retrieve system logs (syslog, tasks, journal) | `node`, `source` (syslog/tasks), `limit` |
| `proxmox_get_tasks` | List recent Proxmox tasks (migrations, backups, etc.) | `node` (optional), `limit` |

### Module Proxmox -- VM/CT Management

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `proxmox_vm_start` | Start a VM or CT | `node`, `vmid` |
| `proxmox_vm_stop` | Stop a VM or CT (clean shutdown or force) | `node`, `vmid`, `force` |
| `proxmox_vm_restart` | Restart a VM or CT | `node`, `vmid` |
| `proxmox_vm_create` | Create a new VM or CT | `node`, `config` (dict) |
| `proxmox_vm_clone` | Clone an existing VM/CT | `node`, `vmid`, `newid`, `name` |
| `proxmox_vm_migrate` | Migrate a VM/CT to another node | `node`, `vmid`, `target_node` |
| `proxmox_vm_config` | Read or modify VM/CT configuration | `node`, `vmid`, `updates` (optional) |

### Module Proxmox -- System Administration

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `proxmox_storage_status` | Storage status across the cluster | `node` (optional) |
| `proxmox_network_config` | Network configuration of a node | `node` |
| `proxmox_run` | Execute a command inside a QEMU VM (QEMU Guest Agent). Sync by default; pass `wait=False` to start async (returns `exec_id`), or `exec_id=…` to poll an existing session. LXC exec is not exposed by the Proxmox API; use `ssh_run` + `pct exec` on the host node. | `node`, `vmid`, `command`, `timeout` (default 60s), `wait`, `exec_id` |

**Command execution design:**
- The tool auto-detects whether the target is a VM or CT. VMs execute via QEMU Guest Agent; CTs return an actionable error that points to `ssh_run` + `pct exec <vmid> -- <command>` on the node.
- `proxmox_run` (default, `wait=True`) blocks until the command completes or timeout is reached. Returns `{"status": "ok", "stdout": "...", "stderr": "...", "exit_code": N, "duration_s": ...}`. On timeout it auto-switches to async and returns `{"status": "running", "exec_id": "..."}`.
- `proxmox_run(..., wait=False)` returns immediately with `{"status": "running", "exec_id": "..."}`. Internally uses QEMU Guest Agent's native async exec for VMs (start -> PID -> poll).
- `proxmox_run(exec_id="...")` polls an existing session and returns `{"status": "running|ok|failed|timeout", "stdout": "...", "stderr": "...", "exit_code": N}`.
- Async exec state is held in-memory in the server process. A dict of `{exec_id: {pid, node, vmid, type, status, output}}`.

### Module iLO

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `ilo_server_info` | Server model, serial number, firmware versions (iLO, BIOS) | -- |
| `ilo_health_status` | Full health: temperatures, fans, power supplies, disks, memory | -- |
| `ilo_power_status` | Current power state of the server | -- |
| `ilo_power_on` | Power on the physical server | -- |
| `ilo_power_off` | Power off the physical server (use when server is unresponsive) | `force` (default false) |
| `ilo_power_reset` | Hard reset the physical server | -- |
| `ilo_get_event_log` | iLO event log (hardware errors, reboots, etc.) | `limit` |

**iLO access via SSH tunnel:**
Since iLO is only accessible from the local network, the module establishes an SSH tunnel through pve1:
1. asyncssh opens a tunnel: `localhost:dynamic_port -> pve1 -> ilo_local_ip:443`
2. python-hpilo connects to `localhost:dynamic_port`
3. Tunnel is created on-demand and reused for subsequent calls
4. If pve1 is unreachable, iLO tools return an error explaining the dependency

### Module SSH

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `ssh_run` | Execute a command on any host via SSH. Sync by default; `wait=False` starts async and returns `exec_id`; `exec_id=…` polls an existing session. | `host`, `command`, `timeout` (default 60s), `wait`, `exec_id` |
| `ssh_list_sessions` | List active async command sessions with their status | -- |

SSH uses password authentication. The `host` parameter accepts:
- A Proxmox node name (`pve1`, `pve2`) -- resolved to the configured host from env vars
- A VMID (e.g., `101`) -- resolved to IP via the infrastructure.yaml convention (192.168.1.{VMID})
- A direct IP or hostname (e.g., `192.168.1.50`)

## Configuration

All configuration via environment variables, loaded from `.env` file by `python-dotenv`:

```env
# Proxmox nodes -- API tokens (to be created on the Proxmox nodes)
PVE1_HOST=pve1.example.com
PVE1_TOKEN_ID=root@pam!beaconmcp
PVE1_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

PVE2_HOST=pve2.example.com
PVE2_TOKEN_ID=root@pam!beaconmcp
PVE2_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# iLO -- single unit, local network only
ILO_HOST=192.168.x.x
ILO_USER=Administrator
ILO_PASSWORD=xxxxx
ILO_JUMP_HOST=pve1          # Proxmox node used as SSH jump host

# SSH credentials (fallback access)
SSH_USER=root
SSH_PASSWORD=xxxxx

# Options
PVE_VERIFY_SSL=false         # Set to true if using valid SSL certificates
```

**Startup validation:**
- PVE1 credentials are required (server won't start without them)
- PVE2 credentials are optional (graceful degradation if missing or node is down)
- iLO credentials are optional (iLO tools disabled if not configured)
- SSH credentials are optional (SSH tools disabled if not configured)

## Error Handling

- **Node unreachable:** Tools return a clear error message indicating which node is unreachable, rather than raising exceptions. Claude can then suggest remediation (check iLO, try SSH, etc.).
- **Authentication failures:** Logged and returned as structured errors with guidance (check token, check password, etc.).
- **Command timeouts:** Async commands that exceed timeout are marked as `timeout` status. Partial output is preserved.
- **iLO tunnel failure:** If pve1 (jump host) is unreachable, iLO tools return an error explaining that iLO is only accessible through pve1.

## Claude Code Integration

Add to `~/.claude/settings.json` or project `.claude/settings.json`:

```json
{
  "mcpServers": {
    "beaconmcp": {
      "command": "python",
      "args": ["-m", "beaconmcp"],
      "cwd": "/path/to/BeaconMCP/src",
      "env": {
        "PVE1_HOST": "pve1.example.com",
        "PVE1_TOKEN_ID": "root@pam!beaconmcp",
        "PVE1_TOKEN_SECRET": "..."
      }
    }
  }
}
```

Or use a `.env` file in the project directory and configure only the command.

## Verification Plan

1. **Unit:** Test each module's client wrapper independently with mocked API responses
2. **Integration:** Test against pve1 with real API token:
   - List nodes, check node status
   - List VMs, start/stop a test VM
   - Execute a simple command via QEMU Guest Agent (`echo hello`)
   - Run an async command and poll for result
3. **iLO:** Test tunnel creation + health check against the real iLO
4. **SSH:** Test direct SSH command execution on pve1
5. **End-to-end:** Start the MCP server, use it from Claude Code to diagnose a real scenario (e.g., "why is pve2 down?")

## MCP Resources & Prompts

### Infrastructure Context Resource

An `infrastructure.yaml` file at the project root provides contextual information about the infrastructure. The MCP server exposes it as a resource so Claude can read it automatically.

```yaml
# infrastructure.yaml
conventions:
  vmid_to_ip: "CT VMID corresponds to local IP 192.168.1.{VMID}"
  naming: "VMs are prefixed by their role (e.g., web-101, db-102)"

nodes:
  pve1:
    host: pve1.example.com
    role: "Primary node"
    local_network: "192.168.1.0/24"
  pve2:
    host: pve2.example.com
    role: "Secondary node"
    notes: "Currently down"

ilo:
  host: "192.168.x.x"
  access: "Local network only, via SSH tunnel through pve1"

firewall:
  model: "Zyxel USG 210"
  notes: "No API, managed via web GUI"

notes:
  - "iLO is accessible only through pve1 as SSH jump host"
  - "Zyxel USG 210 is the network gateway"
  - "API tokens must be created on each Proxmox node before use"
```

The server exposes this as `beaconmcp://infrastructure` -- a readable resource that provides Claude with the full infrastructure context.

### MCP Prompt: Infrastructure Overview

The server registers an MCP prompt `beaconmcp-context` that injects a concise infrastructure summary into the conversation. This follows prompt engineering best practices (from `docs/prompt-engineering-guide.md`):
- Role definition: "You are managing a Proxmox VE infrastructure"
- Context: node topology, naming conventions, access constraints
- Positive instructions: what to check first, how to diagnose

### Tool Description Quality

All MCP tool descriptions follow best practices:
- **Self-sufficient**: each description is understandable without external context
- **Namespaced**: `proxmox_*`, `ilo_*`, `ssh_*` prefixes
- **When to use / when not to use**: each tool specifies its use case and alternatives
- **Actionable errors**: error messages include what went wrong and what to try next
- **Semantic parameter names**: `vmid` not `id`, `target_node` not `dest`

Reference: `/docs/prompt-engineering-guide.md` -- sections 4.1 through 4.6.

## Out of Scope (v1)

- Zyxel USG 210 firewall integration (no API available)
- Proxmox built-in firewall management (can be added later)
- Backup management (can be added later via Proxmox Backup Server API)
- User/permission management on Proxmox
- Automated alerting/monitoring (this is a tool for Claude, not a monitoring stack)
