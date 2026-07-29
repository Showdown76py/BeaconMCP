# MCP tools

49 tools across eight modules. The infrastructure modules are only registered when the matching
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
| `proxmox_vm_clone` | Clone an existing one. |
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

## Proxmox — system and files (9)

| Tool | Description |
|------|-------------|
| `proxmox_storage_status` | Storage pool status. |
| `proxmox_network_config` | Network configuration per node. |
| `proxmox_run` | Run a command inside a QEMU VM through the guest agent. For LXC containers, use `ssh_run` on the node with `pct exec <vmid> -- <command>`. |
| `proxmox_read_file` | Read a file from a VM via the guest agent. |
| `proxmox_write_file` | Write a file to a VM via the guest agent. |
| `proxmox_upload_file` | Stream a file from the staging directory into a VM (SFTP) or CT (SFTP + `pct push`), up to `server.transfers_max_mb`. Verifies SHA-256 by default. |
| `proxmox_download_file` | Stream a file out of a VM (SFTP) or CT (`pct pull` + SFTP) into the staging directory, same cap and checksum. |
| `proxmox_list_transfers` | List what's currently in the staging directory. |
| `proxmox_delete_transfer` | Delete a staged file by basename. |

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
