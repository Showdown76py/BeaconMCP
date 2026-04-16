# TarkaMCP

MCP server for managing a Proxmox VE infrastructure (pve1.example.com, pve2.example.com) with HP iLO 4 hardware management and SSH fallback access.

## Quick Start

```bash
pip install -e .
cp .env.example .env  # Fill in real credentials
python -m tarkamcp     # Runs MCP server on stdio
```

## Project Structure

```
src/tarkamcp/
  __main__.py         Entry point (loads .env, starts MCP server)
  server.py           FastMCP server, registers all tool modules
  config.py           Environment variable loading & validation
  proxmox/
    client.py         proxmoxer wrapper (API token auth, error handling)
    monitoring.py     6 tools: list_nodes, node_status, list_vms, vm_status, get_logs, get_tasks
    vms.py            7 tools: vm_start, vm_stop, vm_restart, vm_create, vm_clone, vm_migrate, vm_config
    system.py         5 tools: storage_status, network_config, exec_command (sync + async + get_result)
  ssh/
    client.py         asyncssh wrapper (host resolution, connection caching)
    tools.py          4 tools: ssh_exec_command (sync + async + get_result), ssh_list_sessions
  ilo/
    client.py         python-hpilo wrapper (SSH tunnel via pve1 to local iLO)
    tools.py          7 tools: server_info, health_status, power_status, power_on/off/reset, event_log
```

## Configuration

All via environment variables (`.env` file). See `.env.example` for full list.

**Required:** `PVE1_HOST`, `PVE1_TOKEN_ID`, `PVE1_TOKEN_SECRET`
**Optional:** PVE2, iLO, SSH credentials (modules are conditionally registered)

## Creating Proxmox API Tokens

On each Proxmox node: Datacenter > Permissions > API Tokens > Add
- User: `root@pam`
- Token ID: `tarkamcp`
- Uncheck "Privilege Separation" for full access

## Claude Code Integration

Add to settings.json:
```json
{
  "mcpServers": {
    "tarkamcp": {
      "command": "python",
      "args": ["-m", "tarkamcp"],
      "cwd": "/path/to/TarkaMCP",
      "env": { "DOTENV_PATH": ".env" }
    }
  }
}
```

## Infrastructure Context

Edit `infrastructure.yaml` to define naming conventions, node roles, and notes.
The server exposes it as `tarkamcp://infrastructure` resource.

## Design Spec

See `docs/superpowers/specs/2026-04-16-tarkamcp-design.md`
