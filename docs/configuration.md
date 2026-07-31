# Configuration

BeaconMCP reads two files at startup.

**`beaconmcp.yaml`** — topology and feature flags. Path resolution, in order: the `--config` flag,
the `BEACONMCP_CONFIG` env var, `./beaconmcp.yaml`, `/etc/beaconmcp/config.yaml`. The full annotated
schema lives in [`beaconmcp.yaml.example`](../beaconmcp.yaml.example).

**`.env`** — the secrets the YAML references as `${VAR}`. A missing reference fails the startup
check and names the offending YAML path.

`beaconmcp validate-config` resolves everything and prints the result with secrets masked, without
starting the server. Run it after every edit.

Legacy `PVE*_*`, `ILO_*` and `SSH_*` env vars still work when no YAML is found. They are deprecated
and disappear in 2.1.

## Where to run it

Put BeaconMCP on the same local network as the cluster: on one of the nodes, in a dedicated LXC or
VM, or in a container with host networking.

That way every `proxmox.nodes[].host` is a plain LAN IP (`10.0.0.1`, `10.0.0.2`) usable as-is for
the Proxmox API on `:8006` **and** for SSH on `:22`. Two features depend on that being true: the
`ssh.inherit_proxmox_nodes` shortcut, and the SSH-jump tunnel `bmc_*` uses to reach HP iLO devices
on a private management VLAN.

A public FQDN with a reverse-proxy port (`pve2.example.com:443`) pins the entry to HTTPS and breaks
SSH inheritance, since sshd listens on port 22 of the node and not behind your HTTPS tunnel. For a
genuinely remote node, declare it explicitly under `ssh.hosts[]` with its real SSH address: a
Tailscale IP, a VPN address, a bastion.

## Server

| Key | Notes |
|-----|-------|
| `server.allowed_hosts` | DNS-rebinding allowlist. **Must** include the public FQDN behind your reverse proxy, or requests come back `421 Misdirected Request`. |
| `server.allowed_origins` | Web-origin allowlist, used for browser CORS preflights and for OAuth HTTPS redirect URIs. Desktop and CLI callbacks (`vscode://`, `cursor://`, loopback) are handled separately. |
| `server.trusted_proxies` | Direct peers whose forwarded headers are trusted, as IPs or CIDRs. Governs `X-Forwarded-For` (the auth rate-limit client IP) **and** `X-Forwarded-Host` (the host advertised in the OAuth issuer and the token/connector MCP URLs). When empty, both fall back to the request's own peer / `Host` and forwarded values from any peer are ignored. The value `cloudflare` auto-expands to Cloudflare's edge ranges. |
| `server.tokens_db` | SQLite file persisting *named* API tokens (the `/app/tokens` page) across restarts. Created owner-only (0600). Defaults to `tokens.db` next to `clients_file`. Env override: `BEACONMCP_TOKENS_DB`. |
| `server.named_token_ttl` | Lifetime of named API tokens, in seconds. Default `2592000` (30 days); `0` means never expires, revoke-only. Internal OAuth and session bearers keep their fixed 24 h TTL either way. Env override: `BEACONMCP_NAMED_TOKEN_TTL`. |
| `server.audit_log` | JSON-lines audit log covering tool calls, dashboard logins, OAuth authorize and client revokes. Created owner-only (0600). Default `/opt/beaconmcp/audit.log`; `-` keeps stderr only. Env override: `BEACONMCP_AUDIT_LOG`. |
| `server.transfers_max_mb` | Size cap for `proxmox_upload_file` / `proxmox_download_file`. Default 500. |

## Proxmox

| Key | Notes |
|-----|-------|
| `proxmox.nodes[]` | One entry per node, each with its own API token. Prefer a LAN IP in `host:` — it's the one string that works for both the API and SSH inheritance. `localhost` is fine when BeaconMCP runs on that node. Reserve FQDN-with-port for nodes you cannot reach on the LAN, and give those an explicit `ssh.hosts[]` entry. |

## SSH

| Key | Notes |
|-----|-------|
| `ssh.hosts[]` | One entry per SSH target: VPS, Proxmox node, jump box. Each carries its own `user` plus exactly one of `password` / `key_file`. Names may match `proxmox.nodes[].name`. Per-host `known_hosts` and `strict_host_key_checking` override the global `ssh.*` settings, but only to tighten them. |
| `ssh.defaults` + `ssh.inherit_proxmox_nodes` | Homelab shortcut: set `defaults:` (user + password/key_file), flip `inherit_proxmox_nodes: true`, and every Proxmox node becomes SSH-reachable under its own name without duplication. An explicit `ssh.hosts[]` entry still wins when it matches a node by name or address. |
| `ssh.vmid_to_ip` | Optional template (`"192.168.1.{id}"`) used by `ssh_run` when `host` is a bare VMID. The resolved IP must match an `ssh.hosts[].host` to authenticate. Omit it to disable numeric shortcuts entirely. |

## BMC

| Key | Notes |
|-----|-------|
| `bmc.devices[]` | Zero or more BMCs. `type` is `hp_ilo`, `ipmi`, `idrac`, `supermicro` or `redfish`. iDRAC (14G+) and Supermicro (X11+) are served by the Redfish backend. `jump_host` is optional: set it to a `proxmox.nodes[]` name to route the connection over an SSH tunnel, which is how you reach a management VLAN. |

## Dashboard

| Key | Notes |
|-----|-------|
| `features.dashboard.limits` | Per-5h and per-week USD caps on the Gemini chat. `0` disables that window. |

Everything else about the panel — enabling it, the tokens page, cost tracking, the confirmation
modal — is in [dashboard.md](dashboard.md).

## Updates

| Key | Notes |
|-----|-------|
| `features.updates.enabled` | Default `true`. Compares this checkout against the upstream default branch and shows a notice to signed-in operators. Set to `false` on an air-gapped or change-controlled box: the server then never contacts the git remote, and the `beaconmcp_*_update` MCP tools are not registered. |
| `features.updates.allow_self_update` | Default `true`. Set to `false` to keep the notification but forbid applying it from the dashboard or over MCP — the right setting when deploys go through a pipeline. Manual instructions are still shown. |

See [updates.md](updates.md) for the notice, the MCP tools, and what the self-update does.
