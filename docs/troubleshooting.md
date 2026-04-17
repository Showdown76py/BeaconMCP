# Troubleshooting

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| `ERROR: No configuration file found.` | No `beaconmcp.yaml` in the working directory and `BEACONMCP_CONFIG` unset | Copy `beaconmcp.yaml.example`, fill in the topology, point `BEACONMCP_CONFIG` at it or place it at `/opt/beaconmcp/beaconmcp.yaml`. |
| `environment variable ${X} referenced at '...' is not set` | YAML references a secret that is not in the environment | Add the variable to `.env` (or export it in the systemd unit), then restart. |
| `Node 'X' is unreachable` | Proxmox API is down on that node | `curl -sk https://<node>:8006/api2/json/version` to confirm. |
| `QEMU Guest Agent may not be running` | Agent not installed or VM not restarted after enabling the option | `apt install qemu-guest-agent && systemctl enable --now qemu-guest-agent`, then reboot the VM. |
| `Failed to open SSH tunnel to BMC ... via X` | Jump host is down or SSH credentials missing | Confirm the Proxmox node is reachable with `proxmox_list_nodes`; make sure `ssh:` is configured in `beaconmcp.yaml`. |
| `ipmitool is not installed on this host` | IPMI backend configured but binary missing | Install `ipmitool` on the BeaconMCP host (`apt install ipmitool`, `dnf install ipmitool`, …). |
| `SSH connection to 'X' failed` | SSH auth disabled or firewall dropping the connection | `grep PasswordAuthentication /etc/ssh/sshd_config` on the target; check firewall rules. |
| `invalid_client` | Wrong client id or secret | `beaconmcp auth list` to confirm the id, re-check the secret (or revoke + recreate). |
| `421 Misdirected Request` | Public hostname missing from the DNS-rebinding allowlist | Add the FQDN under `server.allowed_hosts` in `beaconmcp.yaml` (or `BEACONMCP_ALLOWED_HOSTS` in `.env`), then restart. |
| `{"error":"unauthorized"}` on `/authorize` | Client id not registered server-side | Create the client with `beaconmcp auth create`, then paste the returned id into the connector. |
| `invalid_grant` with `missing or invalid totp` | 2FA code wrong, expired (>30 s), or already used | Generate a fresh code. Compare the server clock against the phone (`timedatectl` vs. authenticator). |
| `Too many attempts` on the 2FA page | 5 consecutive failed TOTP codes triggered a 5-minute lockout | Wait. The counter resets on the next valid code. |
| Clients silently rejected after upgrade | 2FA migration: clients without a TOTP secret are refused at startup | Inspect `journalctl -u beaconmcp` for the list, recreate them with `beaconmcp auth create`. |
| Dashboard loops between `/app/refresh` and `/app/chat` | Bearer wiped by a service restart while the session cookie is still valid | Enter the TOTP code on the refresh page to mint a new bearer. |
| External MCP clients disconnected after a restart | `TokenStore` is in-memory and wiped on restart | Recreate tokens from `/app/tokens` (max 3, 24 h expiry). |
| `remote_mode_disabled` error event in the chat | `BEACONMCP_DASHBOARD_MCP_MODE=remote` is set | Remove the variable; only `local` mode is supported. |
| Chat blocked on an SSH/exec card with two buttons | Mandatory confirmation for `ssh_exec_command*` / `proxmox_exec_command*` | Click **Approve** or **Reject**. The card auto-rejects after 5 minutes of inactivity. |
| `Limit reached: max 3 tokens` on the tokens page | 3 named tokens already active for this client | Revoke one before creating a new one. |
| `Unknown BMC type 'X'` at startup | `bmc.devices[].type` set to an unsupported value | Valid types: `hp_ilo`, `ipmi`, `idrac` (stub), `supermicro` (stub). |

## Usage patterns

> **"pve2 is not responding, what is happening?"**
>
> The model runs: `proxmox_list_nodes` → sees pve2 offline → `bmc_list_devices` → `bmc_power_status` on the matching device → confirms power state → `bmc_health_status` → surfaces hardware anomalies → proposes a diagnosis.

> **"Upgrade packages on every container."**
>
> The Proxmox API does not expose an `exec` endpoint for LXCs, so the model falls back to `proxmox_list_vms` → filters containers → `ssh_exec_command_async` on the host node with `pct exec <vmid> -- sh -c 'apt update && apt upgrade -y'` per container → polls the results. Every `ssh_exec_command*` and `proxmox_exec_command*` call requires manual approval in the integrated chat; external MCP clients must enable equivalent per-call approval.
