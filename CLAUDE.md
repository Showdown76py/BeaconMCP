# BeaconMCP

Remote MCP server for managing any Proxmox VE cluster together with its BMC-managed hardware (HP iLO, IPMI). Runs over HTTP with OAuth 2.1 + TOTP. Topology is described in a single YAML file; secrets are referenced through `${ENV_VAR}` placeholders.

## Quick Start

```bash
pip install -e .
cp beaconmcp.yaml.example beaconmcp.yaml   # Describe your infrastructure
cp .env.example .env                       # Fill in the referenced secrets
beaconmcp validate-config                  # Dry-run the loader, secrets masked
beaconmcp auth create --name "x"           # Create an OAuth client
beaconmcp serve                            # Start HTTP server on :8420
```

## Project Structure

```
src/beaconmcp/
  __main__.py         CLI: serve + auth + validate-config
  server.py           FastMCP server, registers every tool module
  config.py           YAML loader with ${ENV} resolver + legacy env fallback
  auth.py             OAuth 2.1 client credentials (ClientStore + TokenStore)
  proxmox/
    client.py         proxmoxer wrapper (API-token auth, N-node aware)
    monitoring.py     6 tools: list_nodes, node_status, list_vms, vm_status, get_logs, get_tasks
    vms.py            7 tools: vm_start/stop/restart/create/clone/migrate/config
    system.py         5 tools: storage_status, network_config, exec_command (sync+async+get_result)
  ssh/
    client.py         asyncssh wrapper with configurable VMID->IP template
    tools.py          4 tools: ssh_exec_command (sync+async+get_result), ssh_list_sessions
  bmc/
    base.py           BMCClient Protocol + shared exceptions + stub base class
    hp_ilo.py         HPILOBackend (python-hpilo, optional SSH jump tunnel)
    ipmi.py           GenericIPMIBackend (shells out to ipmitool)
    idrac.py          IDRACStubBackend (TODO)
    supermicro.py     SupermicroStubBackend (TODO)
    registry.py       build_registry(config) -> {device_id: BMCClient}
    tools.py          8 tools: bmc_list_devices + 7 action tools (device_id param)
  dashboard/          Optional web panel: /app/login, /app/chat, /app/tokens
beaconmcp.yaml.example  Template describing the full config schema
deploy/
  install.sh          One-command install script
  beaconmcp.service   systemd unit file
```

## Configuration

Two files:

- **`beaconmcp.yaml`** — topology and feature flags. Resolution order: `--config` flag → `BEACONMCP_CONFIG` env → `./beaconmcp.yaml` → `/etc/beaconmcp/config.yaml`.
- **`.env`** — secrets referenced by the YAML via `${VAR}`.

Legacy `PVE*_*`, `ILO_*`, `SSH_*` env vars still work when no YAML is found (deprecated; removed in 2.1).

## Auth

OAuth 2.1 client credentials + mandatory TOTP. Manage with `beaconmcp auth create/list/revoke`.

## Design Spec

See `docs/superpowers/specs/2026-04-16-beaconmcp-design.md`.
