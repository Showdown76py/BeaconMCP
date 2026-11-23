# MCP tools

52 tools across eight modules. The infrastructure modules are only registered when the matching
capability is configured, so an SSH-only deployment exposes the 2 SSH tools and nothing else from
Proxmox or BMC. `security_end_session` and the two maintenance tools are always registered,
whatever the topology.

Long-running commands (`proxmox_run`, `ssh_run`) are synchronous by default. Pass `wait=False` to
start one in the background and get an `exec_id` back, then call the same tool with `exec_id=` to
poll for the result.

## Start here — aggregators (4)

These collapse several calls into one. They are what a model should reach for first; the granular
tools below exist for when you already know what you're looking at.

| Tool | Description |
|------|-------------|
| `cluster_overview` | Nodes, VMs and (optionally) storage in a single call. Replaces `proxmox_list_nodes` + `proxmox_list_vms` + `proxmox_storage_status`. `fields=[...]` trims each entry to the keys you need. |
| `cluster_health` | Metrics, BMC health and recent errors for one node or all of them. BMC data is attached only for nodes that have a device declared with `jump_host: <node>`. |
| `vm_find` | Find VMs and containers by name, glob (`web-*`) or substring. Returns a compact hit list. |
| `vm_bulk_action` | `start` / `stop` / `restart` across many VMIDs in parallel, returning per-VM UPIDs or errors. Capped at 50 per call. |

## Proxmox — monitoring (6)

| Tool | Description |
|------|-------------|
| `proxmox_list_nodes` | Cluster nodes and their status. |
| `proxmox_node_status` | CPU, memory, disk and uptime of one node. |
| `proxmox_list_vms` | Every VM and container across the cluster. |
| `proxmox_vm_status` | Detailed state of one VM or container. |
| `proxmox_get_logs` | System or task logs. |
| `proxmox_get_tasks` | Recent task history. |

## Proxmox — VM lifecycle (14)

| Tool | Description |
|------|-------------|
| `proxmox_vm_start` | Start a VM or container. |
| `proxmox_vm_stop` | Stop it, cleanly or forced. |
| `proxmox_vm_restart` | Restart it. |
| `proxmox_vm_create` | Provision a new VM or container. |
| `proxmox_vm_clone` | Clone an existing one (optional full or linked, target storage, target node). |
| `proxmox_vm_migrate` | Migrate across nodes. |
| `proxmox_vm_config` | Read or update the configuration. |
| `proxmox_snapshot_list` | List snapshots. |
| `proxmox_snapshot_create` | Take a snapshot. |
| `proxmox_snapshot_rollback` | Roll back to a snapshot. |
| `proxmox_snapshot_delete` | Delete a snapshot. |
| `proxmox_backup_create` | Trigger a backup. |
| `proxmox_backup_list` | List vzdump archives on a storage pool. |
| `proxmox_backup_restore` | Restore from an archive. |

## Proxmox — interactive panels (3)

| Tool | Description |
|------|-------------|
| `proxmox_vm_panel` | Control panel for one guest: live CPU/RAM/disk, power buttons, CPU and memory fields. |
| `proxmox_logs_panel` | Scrollable syslog and task viewer for one node, with level and substring filters. |
| `cluster_overview_interactive` | Cluster dashboard: node pressure, searchable guest table with inline start/stop, storage bars. |

These three ship a UI. They use the [MCP Apps extension](https://modelcontextprotocol.io/extensions/apps/overview)
(`io.modelcontextprotocol/ui`): the tool carries `_meta.ui.resourceUri` pointing at a `ui://`
resource, a self-contained HTML document served as `text/html;profile=mcp-app` that the client
renders in a sandboxed iframe and talks to over JSON-RPC on `postMessage`.

The panels live in `src/beaconmcp/proxmox/apps/`. They share `bridge.js` (the JSON-RPC client)
and `panel.css` (the look), spliced in at the `<!--mcp-runtime-->` marker when the resource is
read, so each one still ships as a single document.

**They hold no cluster access of their own.** Every button issues an ordinary `tools/call` for
the tools listed elsewhere in this document — `proxmox_vm_start`, `_stop`, `_restart`, `_config`
— which the host relays, and which the host decides whether to approve. A panel is a nicer way
to issue the call, not a channel that bypasses the host.

What the host does with that call is the host's policy. BeaconMCP's own dashboard lets a panel
drive the guest lifecycle unattended (a labelled button is the click) but refuses anything in its
confirmation list, and refuses `proxmox_vm_config` for any key that is not sizing — see
[the dashboard guide](dashboard.md#interactive-panels-mcp-apps). Other hosts set their own line.

After an action, a panel pushes the fresh state back into the conversation with
`ui/update-model-context`. Without it the model keeps whatever the tool returned when the panel
opened: stop a VM from the panel and the next turn still thinks it is running, because the
button's result goes to the iframe, not to the model. Where the host supports `ui/message`, the
VM panel also offers a button that asks the model to investigate the guest, and the cluster
dashboard one that asks it to open a specific VM's panel. Both features are gated on the host
advertising the capability and are simply hidden when it does not.

Clients that did not negotiate the extension ignore `_meta.ui` and show the tool's return value,
which is the same snapshot as data. Nothing breaks; you just don't get the frame. BeaconMCP's own
`/app/chat` negotiates it and renders the panels inline.

## Proxmox — system and files (12)

| Tool | Description |
|------|-------------|
| `proxmox_storage_status` | Storage pool status. |
| `proxmox_network_config` | Network configuration per node. |
| `proxmox_run` | Run a shell command inside a QEMU VM (guest agent) or an LXC container (`pct exec` over SSH to the node, so SSH must be configured). The command goes through `sh -c`, or `cmd.exe /c` on Windows VMs, so pipes, redirections and `&&` work. |
| `proxmox_read_file` | Read a file from a VM via the guest agent. |
| `proxmox_write_file` | Write a file to a VM via the guest agent. |
| `proxmox_upload_file` | Stream a file from the staging directory into a VM (SFTP) or CT (SFTP + `pct push`), up to `server.transfers_max_mb`. Verifies SHA-256 by default. |
| `proxmox_download_file` | Stream a file out of a VM (SFTP) or CT (`pct pull` + SFTP) into the staging directory, same cap and checksum. |
| `proxmox_list_transfers` | List what's currently in the staging directory. |
| `proxmox_upload_link` | Mint a one-shot HTTP link bound to a VM/CT path: `curl -T <file> <url>` writes the file into the guest (any bytes, up to `server.transfers_max_mb`). Pass `link_id` to read the outcome. See below. |
| `proxmox_download_link` | Mint a one-shot HTTP link for a file inside a VM/CT: the server pulls it into staging (same cap and checksum as `proxmox_download_file`), then `curl -o <file> <url>` fetches it. Pass `link_id` to read the outcome. See below. |
| `proxmox_fetch_url` | Have the server download an http(s) URL and write it into a VM/CT, same cap and checksum. URLs resolving to loopback or link-local addresses (the BeaconMCP host itself, cloud metadata) are refused, redirects included; private LAN addresses are allowed. |
| `proxmox_delete_transfer` | Delete a staged file by basename. |

### Sending a local file into a guest

MCP tool arguments are JSON, so a file can only ride inside a tool call as text. To send a real
file, binary included, ask for a link and upload the raw bytes to it:

```
proxmox_upload_link(node="pve1", vmid=201, dest="/root/backup.tar.gz")
  -> {"url": "https://beacon.example.com/transfers/upload/<token>", "link_id": "...", ...}

curl -sS -T backup.tar.gz "https://beacon.example.com/transfers/upload/<token>"
  -> {"status": "success", "bytes": ..., "sha256": "...", "checksum_verified": true}
```

The token in the URL is the credential, so the route needs no bearer. A link is bound to the
node, VMID and path chosen when it was minted, works once, expires after `expires_in` seconds
(default 600, at most 3600) and is forgotten on restart. The base of the URL is
`features.dashboard.public_url` when set, otherwise the address the MCP request came in on.
Behind Cloudflare, uploads are limited to 100 MB per request on the free plan. The link needs a running guest: minting one for a stopped VM/CT is refused, and an upload to a guest stopped since then answers 409.

### Bringing a file out of a guest

The reverse trip works the same way. The result of `proxmox_download_file` stays in the server's
staging directory, so to get a file back to the machine you work on (screenshots, logs, a dump),
ask for a download link:

```
proxmox_download_link(node="pve1", vmid=201, source="/root/shots/home.png")
  -> {"url": "https://beacon.example.com/transfers/download/<token>", "link_id": "...",
      "filename": "home.png", "size": 48213, "sha256": "...", "expires_at": ...}

curl -sS -o home.png "https://beacon.example.com/transfers/download/<token>"
```

The server first pulls the file into `server.transfers_dir` (`pct pull` for a CT, SFTP for a VM,
SHA-256 checked unless `verify_checksum=false`), refusing a source over `server.transfers_max_mb`
before any data moves, then mints the link. As for uploads, the token in the URL is the credential,
so the route needs no bearer: anyone holding the URL can download the file until it is used or
expires, so treat it like a password. Rules of the link:

- It works once. The token is spent by the first `GET`, even if the client hangs up halfway; mint
  a new link to retry. `HEAD` answers with the headers and does not spend it, any other method
  gets 405. An unknown, spent or expired token is a bare 404.
- It expires after `expires_in` seconds (default 600, at most 3600) and is forgotten on restart.
  A transfer that started but never finishes (a client that stops reading and keeps the connection
  open) is cut off one hour after the expiry, which frees its file and its share of the budget.
- The staged copy (`dlink-<link_id>`, in the hidden `.dlinks/` sub folder of the staging directory,
  out of reach of `proxmox_download_file`, `proxmox_delete_transfer` and `proxmox_list_transfers`)
  is deleted once it was sent and when the link expires. A leftover older than four hours, from a
  crash or a restart, is removed at startup. Younger files are left alone, because
  `beaconmcp check-startup` runs the same startup code next to a live server.
- At most 50 links are pending, and the files they hold count together against
  `server.transfers_max_mb`: past that, minting is refused, before the file is moved.
- The reply carries `Content-Disposition: attachment` with the basename of `source` (quotes,
  control characters and directories removed), `nosniff` and `no-store`.
- `proxmox_download_link(link_id=...)` reports `pending`, `downloading`, `success`, `error` or
  `expired` without exposing the token. Every outcome is written to the audit log as
  `transfer.download` (`status` `success` or `error` when the URL was fetched, `expired` when it
  never was). The token never appears in the audit log, nor in uvicorn's access log, where
  `/transfers/upload/<token>` and `/transfers/download/<token>` are printed as `<redacted>`.
- Mints are serialized: a second call waits for the first to finish, so the byte budget cannot be
  overrun by parallel calls.
- The source must be a regular file (a symbolic link is followed). Devices, FIFOs and directories
  are refused, and the size is checked again once the file reached the node and the server.
- **Do not paste the URL into a chat that unfurls links (Discord, Slack, and similar).** Their link
  preview robot fetches the URL with a `GET` and spends the link before you do. Pass it straight
  to `curl`, or wrap it in angle brackets in Discord.

## SSH (2)

| Tool | Description |
|------|-------------|
| `ssh_run` | Run a command on a host over SSH. `host` accepts node names, VMIDs, hostnames or IPs. |
| `ssh_list_sessions` | Active and recent SSH sessions. |

## BMC — hardware (8)

| Tool | Description |
|------|-------------|
| `bmc_list_devices` | Configured BMCs with their `id` and `type`. Call this first to learn the valid `device_id` values. |
| `bmc_server_info` | Model, serial number, firmware. |
| `bmc_health_status` | Temperatures, fans, power supplies, disks, memory. |
| `bmc_power_status` | Current physical power state. |
| `bmc_power_on` | Power on. |
| `bmc_power_off` | ACPI shutdown, or `force=true` to cut power. |
| `bmc_power_reset` | Hard reset. |
| `bmc_get_event_log` | BMC event log, 50 entries by default, 200 max. |

Every `bmc_*` action takes a `device_id`. With a single device configured it's optional and defaults
to that device.

## Security (1)

| Tool | Description |
|------|-------------|
| `security_end_session` | Revoke the bearer token used for the current request, ~8 s after responding. Call it as the last step of a task to shrink the window in which a stolen token can be replayed — never mid-task, or the next call gets a 401. |

## Maintenance (2)

Registered on every deployment shape, unless `features.updates.enabled` is `false`.

| Tool | Description |
|------|-------------|
| `beaconmcp_check_update` | Read-only. Reports the running version, how many commits this install is behind upstream, the changelog, any `.env` / `beaconmcp.yaml` settings the new revision knows about that this install has not set, and the exact commands that would update *this* install (git checkout, pip install and container each get different ones). Cached for a few hours. |
| `beaconmcp_self_update` | Applies the update: `git pull --ff-only` → reinstall the package and its dependencies → **validate the config against the new code** → schedule a restart. Requires `confirm=True`. Refuses on a dirty checkout or a non-git install. If the new revision cannot load the current config, everything is rolled back and nothing restarts. Hidden when `features.updates.allow_self_update` is `false`. |

The restart is deferred a few seconds so the tool result reaches the caller before the process dies.
Show the user `beaconmcp_check_update` output — especially any new configuration — and get an
explicit go-ahead before calling `beaconmcp_self_update`.

## Tool annotations

Every tool declares MCP `ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) in `tools/list`, so a client can group read-only tools, prompt before destructive
ones and flag tools that leave the declared infrastructure. They live in one table,
`src/beaconmcp/tool_annotations.py`, applied when a tool is registered.

- `readOnlyHint` is true only if the tool changes nothing in any call mode. `proxmox_vm_config`
  reads without `updates` but writes with it, so it is not read-only. `proxmox_read_file` changes
  nothing but declares no `readOnlyHint`: it can read any file in a guest (keys, `/etc/shadow`),
  and some clients skip the approval prompt for read-only tools.
- `destructiveHint` is true when the tool can delete, overwrite, stop or run arbitrary code, and
  false for pure additions (snapshot, clone, backup, start). `proxmox_upload_link` and `proxmox_download_link` count as
  destructive: they only mint a link, but whoever holds it can overwrite a file in the guest (upload) or read one out of it (download).
- `idempotentHint` is true only when repeating the same call has no further effect.
- `openWorldHint` is true for `proxmox_fetch_url` (arbitrary URLs) and the two update tools (they
  contact the remote git repository).

These are hints for the client, not a security barrier. The server-side control stays
`server.confirm_tools` (and the bearer and TOTP layers). A test fails if a registered tool has no
entry in the table, or an entry has no tool.
