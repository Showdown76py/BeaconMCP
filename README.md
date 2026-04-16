<div align="center">

# TarkaMCP

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![MCP Protocol](https://img.shields.io/badge/MCP-Model_Context_Protocol-5A67D8)](https://modelcontextprotocol.io/)
[![Proxmox VE](https://img.shields.io/badge/Proxmox-VE_8.x-E57000?logo=proxmox&logoColor=white)](https://www.proxmox.com/)
[![HP iLO 4](https://img.shields.io/badge/HP-iLO_4-0096D6?logo=hp&logoColor=white)](https://www.hpe.com/us/en/servers/integrated-lights-out-ilo.html)
[![ChatGPT](https://img.shields.io/badge/ChatGPT-Compatible-74AA9C?logo=openai&logoColor=white)](https://chatgpt.com/)
[![Gemini](https://img.shields.io/badge/Gemini-Compatible-4285F4?logo=google&logoColor=white)](https://gemini.google.com/)
[![License](https://img.shields.io/github/license/Showdown76py/TarkaMCP)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-F97316)](https://claude.ai/code)

**Serveur MCP remote pour la gestion d'infrastructure Proxmox VE.**

Compatible **Claude** (web, mobile) &bull; **ChatGPT** &bull; **Gemini** (CLI, API)

[Installation](#installation) &bull; [Connexion](#connexion-par-plateforme) &bull; [Outils](#outils-disponibles) &bull; [Tests](#tests)

</div>

---

### Fonctionnalités

- **29 outils MCP** répartis en 3 modules (Proxmox, SSH, iLO)
- **Multi-plateforme** -- Claude, ChatGPT, Gemini via Streamable HTTP + OAuth 2.1
- **Diagnostic automatisé** -- l'IA identifie les crashs, vérifie le hardware, propose des résolutions
- **Exécution de commandes** dans les VMs/CTs via QEMU Guest Agent ou SSH (sync et async)
- **Gestion hardware à distance** -- power on/off/reset, températures, ventilateurs via iLO 4
- **Architecture modulaire** -- chaque module se charge uniquement si ses credentials sont configurés

---

## Table des matières

- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration Proxmox](#configuration-proxmox)
- [Configuration iLO](#configuration-ilo)
- [Configuration .env](#configuration-env)
- [Connexion par plateforme](#connexion-par-plateforme)
- [Tests](#tests)
- [Outils disponibles](#outils-disponibles)
- [Dépannage](#dépannage)

---

## Architecture

```
Clients (Claude, ChatGPT, Gemini)
        |
        | HTTPS (Cloudflare Tunnel)
        v
+--[ pve1.example.com ]-------------------+
|                                            |
|  TarkaMCP (HTTP :8420)                     |
|    +-- proxmox/ -----> Proxmox API :8006   |
|    +-- ssh/ ---------> SSH :22             |
|    +-- ilo/ ---------> iLO 4 (réseau local)|
|                                            |
+--------------------------------------------+
        |
        | API Proxmox
        v
  pve2.example.com
```

Le serveur tourne sur pve1 et expose un endpoint MCP via Cloudflare Tunnel. Toutes les plateformes s'y connectent avec des credentials OAuth.

---

## Installation

### 1. Installer sur pve1

```bash
git clone https://github.com/Showdown76py/TarkaMCP.git /opt/tarkamcp
cd /opt/tarkamcp
sudo bash deploy/install.sh
```

### 2. Configurer les credentials Proxmox

```bash
nano /opt/tarkamcp/.env
```

Remplir au minimum `PVE1_HOST`, `PVE1_TOKEN_ID`, `PVE1_TOKEN_SECRET` (voir [Configuration .env](#configuration-env)).

### 3. Créer un client OAuth

```bash
tarkamcp auth create --name "Claude Web"
```

```
  Client ID:     tarkamcp_a1b2c3...
  Client Secret: sk_d4e5f6...
```

Conserver ces credentials -- le secret ne sera plus affiché.

### 4. Démarrer le serveur

```bash
sudo systemctl start tarkamcp
curl http://localhost:8420/health
# → {"status": "ok", "server": "tarkamcp"}
```

### 5. Exposer via Cloudflare Tunnel

Dans le dashboard Cloudflare Zero Trust, ajouter un tunnel :

| Paramètre | Valeur |
|-----------|--------|
| **Hostname** | `mcp.example.com` |
| **Service** | `http://localhost:8420` |

### Gérer les clients

```bash
# Créer un client par plateforme
tarkamcp auth create --name "ChatGPT"
tarkamcp auth create --name "Gemini"

# Lister
tarkamcp auth list

# Révoquer un accès
tarkamcp auth revoke tarkamcp_abc123...
```

---

## Connexion par plateforme

### Claude (web & mobile)

1. **Settings** > **Integrations** > **Add custom connector**
2. Remplir :
   - **Name** : `TarkaMCP`
   - **Remote MCP server URL** : `https://mcp.example.com/mcp`
   - **OAuth Client ID** : `tarkamcp_a1b2c3...`
   - **OAuth Client Secret** : `sk_d4e5f6...`
3. **Add**

### ChatGPT

1. **Settings** > **Developer Mode** > **MCP Servers**
2. URL : `https://mcp.example.com/mcp`
3. Obtenir un bearer token :
   ```bash
   curl -X POST https://mcp.example.com/oauth/token \
     -d "grant_type=client_credentials&client_id=ID&client_secret=SECRET"
   ```
4. Utiliser l'`access_token` retourné comme bearer token

### Gemini CLI

```bash
TOKEN=$(curl -s -X POST https://mcp.example.com/oauth/token \
  -d "grant_type=client_credentials&client_id=ID&client_secret=SECRET" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

gemini mcp add tarkamcp --url https://mcp.example.com/mcp \
  --header "Authorization: Bearer $TOKEN"
```

### Gemini API

```python
import requests
from google import genai

token = requests.post("https://mcp.example.com/oauth/token", data={
    "grant_type": "client_credentials",
    "client_id": "tarkamcp_...",
    "client_secret": "sk_...",
}).json()["access_token"]

client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="Liste les VMs sur pve1",
    config={"tools": [{"mcp_servers": [{
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }]}]},
)
```

---

## Configuration Proxmox

### Créer un API token

Sur l'interface web Proxmox (`https://pve1.example.com`) :

1. **Datacenter** > **Permissions** > **API Tokens** > **Add**
2. **User** : `root@pam`, **Token ID** : `tarkamcp`
3. **Décocher** Privilege Separation
4. Copier le secret affiché

Répéter sur pve2 quand disponible.

### Installer le QEMU Guest Agent

Nécessaire pour exécuter des commandes à l'intérieur des VMs.

```bash
# Debian/Ubuntu
apt install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent

# CentOS/RHEL
dnf install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent
```

Puis dans Proxmox : VM > **Options** > **QEMU Guest Agent** > cocher > redémarrer la VM.

Les conteneurs LXC n'ont pas besoin du Guest Agent.

### Configurer SSH (optionnel)

SSH sert de fallback quand l'API Proxmox ne suffit pas.

```bash
# Vérifier que l'auth par mot de passe est active
grep "^PasswordAuthentication" /etc/ssh/sshd_config
```

---

## Configuration iLO

L'iLO est sur le réseau local. Comme TarkaMCP tourne sur pve1, il y accède directement.

```bash
# Trouver l'IP de l'iLO depuis pve1
nmap -sn 192.168.1.0/24 | grep -B2 "HP\|iLO"

# Tester
curl -sk https://IP_ILO/xmldata?item=All | grep PRODUCT_NAME
```

---

## Configuration .env

```bash
cp .env.example .env && nano .env
```

```env
# OBLIGATOIRE
PVE1_HOST=pve1.example.com
PVE1_TOKEN_ID=root@pam!tarkamcp
PVE1_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# OPTIONNEL -- PVE2
PVE2_HOST=pve2.example.com
PVE2_TOKEN_ID=root@pam!tarkamcp
PVE2_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# OPTIONNEL -- iLO
ILO_HOST=192.168.1.X
ILO_USER=Administrator
ILO_PASSWORD=xxxxx
ILO_JUMP_HOST=pve1

# OPTIONNEL -- SSH
SSH_USER=root
SSH_PASSWORD=xxxxx

# OPTIONS
PVE_VERIFY_SSL=false
# TARKAMCP_PORT=8420
```

Modules chargés conditionnellement : sans SSH → pas de `ssh_*`, sans iLO → pas de `ilo_*`.

---

## Tests

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

---

## Outils disponibles

### Proxmox -- Monitoring (6)

| Outil | Description |
|-------|-------------|
| `proxmox_list_nodes` | Liste les nœuds avec leur statut |
| `proxmox_node_status` | CPU, RAM, disque, uptime d'un nœud |
| `proxmox_list_vms` | Liste toutes les VMs/CTs |
| `proxmox_vm_status` | État détaillé d'une VM/CT |
| `proxmox_get_logs` | Logs système ou tâches |
| `proxmox_get_tasks` | Tâches récentes |

### Proxmox -- Gestion VMs (7)

| Outil | Description |
|-------|-------------|
| `proxmox_vm_start` | Démarrer une VM/CT |
| `proxmox_vm_stop` | Arrêter (clean ou force) |
| `proxmox_vm_restart` | Redémarrer |
| `proxmox_vm_create` | Créer une VM/CT |
| `proxmox_vm_clone` | Cloner |
| `proxmox_vm_migrate` | Migrer vers un autre nœud |
| `proxmox_vm_config` | Lire/modifier la config |

### Proxmox -- Système (5)

| Outil | Description |
|-------|-------------|
| `proxmox_storage_status` | État du stockage |
| `proxmox_network_config` | Config réseau du nœud |
| `proxmox_exec_command` | Commande dans une VM/CT (sync) |
| `proxmox_exec_command_async` | Commande longue (async) |
| `proxmox_exec_get_result` | Résultat d'une commande async |

### SSH (4)

| Outil | Description |
|-------|-------------|
| `ssh_exec_command` | Commande sur un hôte (sync) |
| `ssh_exec_command_async` | Commande longue (async) |
| `ssh_exec_get_result` | Résultat d'une commande async |
| `ssh_list_sessions` | Sessions SSH actives |

### iLO (7)

| Outil | Description |
|-------|-------------|
| `ilo_server_info` | Modèle, serial, firmware |
| `ilo_health_status` | Températures, ventilateurs, alims, disques |
| `ilo_power_status` | État d'alimentation (ON/OFF) |
| `ilo_power_on` | Allumer le serveur |
| `ilo_power_off` | Éteindre (clean ou force) |
| `ilo_power_reset` | Hard reset |
| `ilo_get_event_log` | Journal d'événements iLO |

---

## Dépannage

| Erreur | Cause | Solution |
|--------|-------|----------|
| `PVE1_HOST ... required` | `.env` non chargé | Vérifier `/opt/tarkamcp/.env` |
| `Node 'pveX' is unreachable` | API Proxmox down | `curl -sk https://pve1:8006/api2/json/version` |
| `QEMU Guest Agent may not be running` | Agent non installé | Voir [Configuration Proxmox](#installer-le-qemu-guest-agent) |
| `iLO ... unreachable` | pve1 down ou iLO injoignable | Vérifier pve1 d'abord |
| `SSH connection failed` | Auth SSH désactivée | `grep PasswordAuthentication /etc/ssh/sshd_config` |
| `invalid_client` | Mauvais Client ID/Secret | `tarkamcp auth list` pour vérifier |

---

## Exemple d'utilisation

> **"pve2 ne répond plus, qu'est-ce qui se passe ?"**
>
> L'IA va : `proxmox_list_nodes` → voit pve2 offline → `ilo_power_status` → vérifie si allumé → `ilo_health_status` → checker le hardware → proposer un diagnostic

> **"Mets à jour les paquets sur tous les conteneurs"**
>
> L'IA utilise `proxmox_list_vms` → liste les CTs → `proxmox_exec_command_async` → lance `apt update && apt upgrade -y` dans chacun → poll les résultats

---

## Licence

[Apache 2.0](LICENSE)

<div align="center">
<sub>Construit avec <a href="https://claude.ai/code">Claude Code</a></sub>
</div>
