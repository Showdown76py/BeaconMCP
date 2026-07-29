# MCP tools

44 tools across six modules. The infrastructure modules are only registered when the matching
capability is configured, so an SSH-only deployment exposes the 2 SSH tools and nothing else from
Proxmox or BMC. `security_end_session` is always registered, whatever the topology.

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
