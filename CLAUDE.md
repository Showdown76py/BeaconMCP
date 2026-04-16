# TarkaMCP

Remote MCP server for managing a Proxmox VE infrastructure (pve1.tarkacore.dev, pve2.tarkacore.dev) with HP iLO 4 hardware management and SSH fallback access. Runs as an HTTP server with OAuth 2.1 client credentials authentication.

## Quick Start

```bash
pip install -e .
cp .env.example .env             # Fill in Proxmox credentials
tarkamcp auth create --name "x"  # Create OAuth client
tarkamcp serve                   # Start HTTP server on :8420
```

## Project Structure

```
src/tarkamcp/
  __main__.py         CLI: serve (HTTP server) + auth (client management)
  server.py           FastMCP server, registers all tool modules
  config.py           Environment variable loading & validation
  auth.py             OAuth 2.1 client credentials (ClientStore + TokenStore)
  proxmox/
    client.py         proxmoxer wrapper (API token auth, error handling)
    monitoring.py     6 tools: list_nodes, node_status, list_vms, vm_status, get_logs, get_tasks
    vms.py            7 tools: vm_start/stop/restart/create/clone/migrate/config
    system.py         5 tools: storage_status, network_config, exec_command (sync+async+get_result)
  ssh/
    client.py         asyncssh wrapper (host resolution, connection caching)
    tools.py          4 tools: ssh_exec_command (sync+async+get_result), ssh_list_sessions
  ilo/
    client.py         python-hpilo wrapper (SSH tunnel via pve1 to local iLO)
    tools.py          7 tools: server_info, health_status, power_status/on/off/reset, event_log
deploy/
  install.sh          One-command install script for Proxmox nodes
  tarkamcp.service    systemd unit file
```

## Configuration

All via environment variables (`.env` file). See `.env.example`.

**Required:** `PVE1_HOST`, `PVE1_TOKEN_ID`, `PVE1_TOKEN_SECRET`
**Optional:** PVE2, iLO, SSH credentials (modules conditionally registered)

## Auth

OAuth 2.1 client credentials. Manage with `tarkamcp auth create/list/revoke`.

## Design Spec

See `docs/superpowers/specs/2026-04-16-tarkamcp-design.md`
