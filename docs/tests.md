# Tests

Les tests unitaires et dashboard tournent sans infra réelle (SQLite in-memory + mocks Starlette) :

```bash
pytest tests/test_dashboard_unit.py tests/test_dashboard_chat.py \
       tests/test_dashboard_usage.py tests/test_dashboard_integration.py
```

Les **tests d'intégration** ciblent la vraie infra Proxmox / iLO / SSH et se lancent via un script dédié :

```bash
# Tous les tests
python tests/test_integration.py

# Par section
python tests/test_integration.py --section proxmox
python tests/test_integration.py --section ssh
python tests/test_integration.py --section ilo

# Avec tests VM lifecycle (start/stop/clone)
python tests/test_integration.py --test-vmid 9999
```

| Section | Tests | Description |
|---------|-------|-------------|
| Proxmox Monitoring | 12 | nodes, status, VMs, logs, tasks |
| Proxmox System | 4 | storage, network |
| Proxmox Exec | 6 | QEMU GA + LXC, sync/async |
| VM Lifecycle | 7 | start/stop/restart/config/clone |
| SSH | 8 | exec, host resolution, async |
| iLO | 6 | health, power, event log |
| Resources & Errors | 8 | config, prompts, error handling |

Ces tests exigent que `.env` soit renseigné avec des credentials valides et que l'infra cible soit joignable. Ils **ne tournent pas en CI** — c'est de la vérification locale avant un deploy sur pve1.
