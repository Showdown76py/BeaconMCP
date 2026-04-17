# Tests

Unit tests (dashboard + configuration) run without live infrastructure, using SQLite in-memory and Starlette mocks:

```bash
pytest tests/
```

To scope to specific suites:

```bash
pytest tests/test_dashboard_unit.py \
       tests/test_dashboard_chat.py \
       tests/test_dashboard_usage.py \
       tests/test_dashboard_integration.py
```

**Integration tests** target a live Proxmox cluster, BMC devices, and SSH hosts, and run from a dedicated script:

```bash
# All sections
python tests/test_integration.py

# Restrict to a section
python tests/test_integration.py --section proxmox
python tests/test_integration.py --section ssh
python tests/test_integration.py --section bmc

# Include VM lifecycle coverage (start/stop/clone)
python tests/test_integration.py --test-vmid 9999
```

| Section | Tests | Description |
|---------|-------|-------------|
| Proxmox monitoring | 12 | nodes, status, VMs, logs, tasks |
| Proxmox system | 4 | storage, network |
| Proxmox exec | 6 | QEMU Guest Agent + LXC, sync/async |
| VM lifecycle | 7 | start/stop/restart/config/clone |
| SSH | 8 | exec, host resolution, async |
| BMC | 6 | health, power, event log |
| Resources & errors | 8 | config, prompts, error handling |

Integration tests require a filled-in `beaconmcp.yaml` (plus `.env` for secrets) and reachable targets. They **do not run in CI** — treat them as local pre-deploy verification.
