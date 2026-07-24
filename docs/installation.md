# Installation

Two supported paths: Docker, or the bare-metal install script that registers a systemd service.
They expose the same CLI and the same HTTP surface, so pick whichever fits your infra.

## Requirements

- Python 3.11+ (bare-metal path only; the image ships its own)
- Proxmox VE 8.x with an API token per node (Datacenter → Permissions → API Tokens)
- `ipmitool` on the BeaconMCP host, if any BMC is declared with `type: ipmi`
- a reachable jump host (usually a Proxmox node) if your HP iLO devices sit on a private
  management VLAN
- `GEMINI_API_KEY`, if you want the integrated chat panel

## Option A — Docker

Needs Docker Engine 20.10+ with the Compose plugin. Run it on the Proxmox node itself, in an LXC/VM
on the same LAN, or on any box that reaches every node's API and SSH port directly.

```bash
git clone https://github.com/Showdown76py/BeaconMCP.git
cd BeaconMCP
cp beaconmcp.yaml.example beaconmcp.yaml
cp .env.example .env
docker compose up -d
```

The bundled [`docker-compose.yml`](../docker-compose.yml) uses `network_mode: host`, so the
container sits directly on the LAN and LAN IPs in `proxmox.nodes[].host` work for both the Proxmox
API and SSH. State (OAuth clients, dashboard DB, usage history) lives in a named volume
`beaconmcp-state` and survives `docker compose up --build`.

Run the one-time setup while the container is up:

```bash
docker compose exec beaconmcp beaconmcp validate-config
docker compose exec beaconmcp beaconmcp auth create --name "Assistant Web"
curl http://localhost:8420/health        # {"status":"ok","server":"beaconmcp"}
```

**SSH key files.** Host paths such as `~/.ssh/id_ed25519` are resolved inside the container, where
they don't exist. If any `ssh.hosts[]` entry (or `ssh.defaults`) uses `key_file:`, either copy the
keys into the `beaconmcp-state` volume and point at `/state/keys/...`, or uncomment the `~/.ssh`
bind mount in the compose file.

## Option B — bare metal (systemd)

SSH to the machine that will host BeaconMCP, usually your primary Proxmox node:

```bash
git clone https://github.com/Showdown76py/BeaconMCP.git /opt/beaconmcp
cd /opt/beaconmcp
sudo bash deploy/install.sh
```

The script creates a `beaconmcp` system user, installs the package in editable mode, registers a
systemd unit, and uses `/opt/beaconmcp` for persistent state.

Then write the config (below), provision a client, and start the service:

```bash
sudo systemctl enable --now beaconmcp
curl http://localhost:8420/health
```

## Writing the config

`beaconmcp.yaml` holds the topology, `.env` holds the secrets it references. Two ways to produce
them.

**Guided.** A terminal wizard walks through each capability (Proxmox nodes, SSH, BMC, server) with a
live YAML preview, and appends `${VAR}` placeholders to `.env` for the secrets you fill in
afterwards. The same command edits an existing file: it parses your YAML back into the forms, so you
can tweak and re-save without losing anything.

```bash
pip install 'beaconmcp[wizard]'   # pulls the optional textual dependency
beaconmcp init                    # creates OR edits beaconmcp.yaml, extends .env
beaconmcp init --blank            # force a fresh draft even if the YAML exists
```

Arrow keys browse sections, `enter` opens a form, `ctrl+s` saves without quitting, `q` exits.

**Manual.** Copy the example and edit it:

```bash
cp beaconmcp.yaml.example /opt/beaconmcp/beaconmcp.yaml
cp .env.example /opt/beaconmcp/.env
```

Either way, check the result before starting the server. `validate-config` resolves every `${VAR}`,
prints the config with secrets masked, and summarises what would be registered:

```bash
beaconmcp validate-config
```

Key-by-key reference: [configuration.md](configuration.md).

## Provisioning an OAuth client

```bash
beaconmcp auth create --name "Assistant Web"
```

This prints a client id, a client secret and a TOTP seed with an ASCII QR code. **Both secrets are
shown exactly once.** Scan the QR into an authenticator app (Google Authenticator, Authy, 1Password,
Aegis) right away, or store the raw seed in a secrets manager.

Create one client per MCP client that should have access. To review or remove them:

```bash
beaconmcp auth list
beaconmcp auth revoke <client_id>
```

## Exposing it publicly

Put a reverse proxy in front that terminates TLS and forwards your public hostname to
`http://localhost:8420` (Caddy, nginx, Traefik and Cloudflare Tunnel all work).

Then declare that hostname under `server.allowed_hosts` in `beaconmcp.yaml`. Without it the MCP SDK
rejects requests with `421 Misdirected Request`, which is its DNS-rebinding protection doing its
job.

If you proxy through Cloudflare, add `cloudflare` to `server.trusted_proxies` so forwarded client
IPs can be trusted for auth rate limiting. Cloudflare's bot, WAF and Access defaults also block
headless MCP clients or strip the `Authorization` header outright: [cloudflare.md](cloudflare.md)
lists the skip and cache-bypass rules you need.

Browser-based clients (Assistant Web, ChatGPT, Le Chat, Gemini Web) additionally need their origin
in `server.allowed_origins`, because they send a CORS preflight before reaching `/mcp`, and OAuth
HTTPS `redirect_uri` checks use the same list.

## Updating

Pull the new code and restart.

Docker:

```bash
cd BeaconMCP
git pull
docker compose up -d --build
```

Bare metal — the installer doubles as an updater. It stashes local state, pulls, installs new
dependencies into the virtualenv, and restarts the unit:

```bash
sudo bash /opt/beaconmcp/deploy/install.sh
```
