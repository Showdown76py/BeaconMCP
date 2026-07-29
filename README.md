<div align="center">

# BeaconMCP

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![MCP Protocol](https://img.shields.io/badge/MCP-Model_Context_Protocol-5A67D8)](https://modelcontextprotocol.io/)
[![Proxmox VE](https://img.shields.io/badge/Proxmox-VE_8.x-E57000?logo=proxmox&logoColor=white)](https://www.proxmox.com/)
[![License](https://img.shields.io/badge/license-Apache_2.0_%2B_Commons_Clause-red)](LICENSE)

**One MCP endpoint for a Proxmox cluster, the hardware under it, and your SSH hosts.**

</div>

---

BeaconMCP is a remote MCP server (Streamable HTTP, OAuth 2.1 + TOTP). It exposes a Proxmox VE
cluster, the BMCs of the machines running it (HP iLO, IPMI, Redfish), and arbitrary SSH-reachable
hosts as a single authenticated endpoint. An MCP client can then diagnose a crash, power-cycle a
frozen host, migrate a VM, or run a command inside a guest without you opening four different web
UIs.

Capabilities are independent: configure a full cluster, three VPS reachable only by SSH, a rack of
IPMI BMCs, or any mix. Tools are registered per capability, so an SSH-only deployment never exposes
`proxmox_*` tools. There is no hard-coded node count. Nodes, BMC devices and SSH hosts are all lists
in one YAML file, with secrets referenced as `${ENV_VAR}`.

Tested with Assistant (web, mobile, desktop), ChatGPT, Gemini, Mistral, VS Code, Cursor and
OpenCode.

## Quick start

Docker is the fastest path. Run it on a Proxmox node, or anywhere on the same LAN.

```bash
git clone https://github.com/Showdown76py/BeaconMCP.git
cd BeaconMCP
cp beaconmcp.yaml.example beaconmcp.yaml    # describe your topology
cp .env.example .env                        # fill in the ${VAR} secrets
docker compose up -d

docker compose exec beaconmcp beaconmcp validate-config
docker compose exec beaconmcp beaconmcp auth create --name "Assistant Web"
curl http://localhost:8420/health
```

`auth create` prints a client id, a client secret and a TOTP seed as a QR code, **once**. Scan it
into an authenticator app before closing the terminal.

The server listens on `:8420` in plain HTTP. Put a reverse proxy with TLS in front of it, list the
public hostname under `server.allowed_hosts`, then add the connector to your client.

There is also a TUI wizard (`beaconmcp init`) that writes the YAML for you, and a bare-metal systemd
install. Both are covered in [Installation](docs/installation.md).

## Documentation

| Guide | What's in it |
|-------|--------------|
| [Installation](docs/installation.md) | Requirements, Docker, systemd install, config wizard, reverse proxy, updates |
| [Configuration](docs/configuration.md) | The two config files, every YAML key that matters, where to run the server |
| [Tools](docs/tools.md) | The 46 MCP tools, grouped by module |
| [Updates](docs/updates.md) | The update notice, the self-update tools, and how to turn both off |
| [Client setup](docs/clients.md) | Assistant, ChatGPT, Gemini, Mistral, VS Code, Cursor, OpenCode |
| [Security](docs/security.md) | What to review before approving a tool call, token handling, TOTP hygiene |
| [Dashboard](docs/dashboard.md) | The optional `/app/*` web panel: login, API tokens, Gemini chat |
| [Behind Cloudflare](docs/cloudflare.md) | WAF, Access and caching rules that stop Cloudflare from eating MCP traffic |
| [TOTP automation](docs/totp-automation.md) | Machine-held TOTP for unattended jobs, and why you probably shouldn't |
| [Troubleshooting](docs/troubleshooting.md) | Common errors and what actually fixed them |
| [Tests](docs/tests.md) | Unit tests and the live-cluster integration script |

## Architecture

```
Clients (Assistant, ChatGPT, Gemini, …)
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
             ▼
Proxmox nodes (N)  ·  BMC devices (N)  ·  SSH hosts (N)
```

Run BeaconMCP on the same local network as the cluster. Every `proxmox.nodes[].host` is then a plain
LAN IP that works for both the Proxmox API (`:8006`) and SSH (`:22`), which is what makes the
`ssh.inherit_proxmox_nodes` shortcut and the iLO SSH-jump tunnel usable.
[Configuration](docs/configuration.md#where-to-run-it) explains what breaks when a node is only
reachable through a public FQDN.

## Before you point a model at your infrastructure

BeaconMCP exposes tools that destroy things: `ssh_run`, `proxmox_run`, `bmc_power_off`,
`vm_bulk_action`, `proxmox_vm_create`. A model that misreads a VMID will run the command anyway.

- Turn off auto-approve in every client. Never accept "always allow this tool".
- Read the `command` argument before approving. If it hit the wrong host, could you recover?
- Treat a `/app/tokens` bearer as root on your nodes for its whole lifetime (30 days by default).

The full checklist, including what the integrated chat confirms on your behalf, is in
[docs/security.md](docs/security.md).

## License

[Apache 2.0 with Commons Clause](LICENSE). Use, fork and modification are free; reselling the
software, including as a hosted service, requires a separate commercial license. The code stays
source-available.
