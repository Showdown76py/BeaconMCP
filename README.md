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

**Serveur MCP pour la gestion d'infrastructure Proxmox VE.**

Compatible **Claude** (Code, web, mobile) &bull; **ChatGPT** &bull; **Gemini** (CLI, API)

Donne à l'IA un accès direct à tes nœuds Proxmox, à l'iLO HP, et au SSH pour diagnostiquer, gérer les VMs/CTs, et résoudre les problèmes d'infrastructure.

[Installation](#installation-côté-client) &bull; [Connexion par plateforme](#connexion-par-plateforme) &bull; [Outils](#outils-disponibles) &bull; [Tests](#tests)

</div>

---

### Fonctionnalités

- **29 outils MCP** répartis en 3 modules (Proxmox, SSH, iLO)
- **Diagnostic automatisé** -- Claude identifie les crashs, vérifie le hardware, propose des résolutions
- **Exécution de commandes** dans les VMs/CTs via QEMU Guest Agent ou SSH, avec support sync et async
- **Gestion hardware à distance** -- power on/off/reset, températures, ventilateurs via iLO 4
- **Architecture modulaire** -- chaque module se charge uniquement si ses credentials sont configurés
- **Contexte infrastructure** -- fichier YAML exposé comme ressource MCP pour guider l'IA
- **Multi-plateforme** -- stdio (Claude Code, Gemini CLI) + Streamable HTTP (Claude mobile, ChatGPT, Gemini API)

---

## Table des matières

- [Architecture](#architecture)
- [Installation côté client (ta machine)](#installation-côté-client)
- [Déploiement remote (serveur)](#déploiement-remote-serveur)
- [Connexion par plateforme](#connexion-par-plateforme)
- [Configuration côté serveur (Proxmox)](#configuration-côté-serveur-proxmox)
- [Configuration côté serveur (iLO)](#configuration-côté-serveur-ilo)
- [Configuration du .env](#configuration-du-env)
- [Tests](#tests)
- [Outils disponibles](#outils-disponibles)
- [Dépannage](#dépannage)

---

## Architecture

```
Ta machine (Claude Code)              Infrastructure
+----------------------------+        +-----------------------------+
|                            |  API   |                             |
|  TarkaMCP (MCP server)  -------->   |  pve1.example.com :8006  |
|    |                       |  SSH   |    (Proxmox VE)            |
|    +-- proxmox/ (API)   -------->   |                             |
|    +-- ssh/ (asyncssh)  -------->   |  pve2.example.com :8006  |
|    +-- ilo/ (tunnel)    ---+        |    (Proxmox VE)            |
|                            |  |     +-----------------------------+
+----------------------------+  |
                                |     +-----------------------------+
                                +---> |  iLO 4 (réseau local)      |
                            tunnel    |    via pve1 SSH             |
                            SSH       +-----------------------------+
```

## Deux modes de fonctionnement

| Mode | Transport | Usage | Commande |
|------|-----------|-------|----------|
| **Local** | stdio | Claude Code, Gemini CLI | `python -m tarkamcp` |
| **Remote** | Streamable HTTP | Claude mobile/web, ChatGPT, Gemini API | `python -m tarkamcp --http` |

Le mode **local** est pour un usage depuis ta machine. Le mode **remote** expose un serveur HTTP avec authentification par bearer token, accessible depuis n'importe quel client MCP.

## Installation côté client

### Prérequis

- Python >= 3.11
- pip
- Accès réseau vers pve1.example.com (port 8006 pour l'API, port 22 pour SSH)

### Installation

```bash
cd TarkaMCP
pip install -e .
```

### Vérification rapide

```bash
# Avec les variables d'environnement configurées
python -c "
from dotenv import load_dotenv; load_dotenv()
from tarkamcp.server import mcp
print(f'OK: {len(mcp._tool_manager._tools)} outils enregistrés')
"
```

---

## Configuration côté serveur (Proxmox)

### 1. Créer un API token sur chaque nœud

Se connecter à l'interface web Proxmox (`https://pve1.example.com`).

1. Aller dans **Datacenter** > **Permissions** > **API Tokens**
2. Cliquer **Add**
3. Remplir :
   - **User** : `root@pam`
   - **Token ID** : `tarkamcp`
   - **Privilege Separation** : **décocher** (important, sinon le token n'a aucun privilège)
4. Cliquer **Add**
5. **Copier le token secret** affiché (il ne sera plus visible après)

Le Token ID complet sera : `root@pam!tarkamcp`

Répéter sur pve2 quand il sera de retour.

### 2. Installer le QEMU Guest Agent dans les VMs

Le Guest Agent est nécessaire pour exécuter des commandes à l'intérieur des VMs via l'API Proxmox.

**Debian/Ubuntu :**
```bash
apt update && apt install -y qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

**CentOS/RHEL/AlmaLinux :**
```bash
dnf install -y qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

**Vérification :**
```bash
systemctl status qemu-guest-agent
# Doit afficher "active (running)"
```

Puis dans Proxmox, activer le Guest Agent pour la VM :
1. Aller dans la VM > **Options** > **QEMU Guest Agent**
2. Cocher **Use QEMU Guest Agent**
3. Redémarrer la VM

**Note :** Le Guest Agent n'est pas nécessaire pour les conteneurs LXC -- Proxmox a un accès direct.

### 3. Configurer l'accès SSH (optionnel mais recommandé)

Le serveur MCP utilise SSH comme fallback quand l'API Proxmox ne suffit pas. L'accès SSH par mot de passe doit être actif sur les nœuds Proxmox.

Vérifier que c'est le cas :
```bash
# Sur le nœud Proxmox
grep -E "^PasswordAuthentication" /etc/ssh/sshd_config
# Doit afficher: PasswordAuthentication yes
```

Si non :
```bash
sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl restart sshd
```

### 4. Vérifier les ports ouverts

Le serveur MCP a besoin de ces accès réseau :

| Service | Port | Protocole | Depuis |
|---------|------|-----------|--------|
| Proxmox API | 8006 | HTTPS | Ta machine |
| SSH (nœuds) | 22 | SSH | Ta machine |
| iLO | 443 | HTTPS | pve1 (réseau local) |

---

## Configuration côté serveur (iLO)

L'iLO est sur le réseau local uniquement. TarkaMCP y accède via un tunnel SSH à travers pve1.

### Prérequis

- iLO 4 accessible depuis le réseau local de pve1
- Credentials iLO (par défaut : `Administrator` / mot de passe configuré)

### Trouver l'IP de l'iLO

Depuis pve1 :
```bash
# Scanner le réseau local pour trouver l'iLO
# L'iLO répond généralement sur le port 443 et 17988
nmap -sn 192.168.1.0/24 | grep -B2 "HP\|iLO\|Hewlett"

# Ou si tu connais l'IP, vérifier
curl -sk https://192.168.1.X/xmldata?item=All | head -20
```

### Tester l'accès iLO depuis pve1

```bash
# Depuis pve1
curl -sk https://IP_ILO/xmldata?item=All | grep PRODUCT_NAME
# Doit afficher le nom du serveur HP
```

---

## Configuration du .env

Copier le template et remplir :

```bash
cp .env.example .env
```

Éditer `.env` :

```env
# OBLIGATOIRE -- PVE1
PVE1_HOST=pve1.example.com
PVE1_TOKEN_ID=root@pam!tarkamcp
PVE1_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# OPTIONNEL -- PVE2 (quand il sera de retour)
PVE2_HOST=pve2.example.com
PVE2_TOKEN_ID=root@pam!tarkamcp
PVE2_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# OPTIONNEL -- iLO
ILO_HOST=192.168.1.X
ILO_USER=Administrator
ILO_PASSWORD=ton_mot_de_passe_ilo
ILO_JUMP_HOST=pve1

# OPTIONNEL -- SSH (recommandé)
SSH_USER=root
SSH_PASSWORD=ton_mot_de_passe_root

# OPTIONS
PVE_VERIFY_SSL=false
```

Les modules sont chargés conditionnellement :
- **Sans SSH** : les 4 outils `ssh_*` ne sont pas disponibles
- **Sans iLO** : les 7 outils `ilo_*` ne sont pas disponibles
- **Sans PVE2** : les outils fonctionnent mais seul pve1 est interrogé

---

## Déploiement remote (serveur)

Pour utiliser TarkaMCP depuis Claude mobile, ChatGPT, ou Gemini, il faut le déployer comme serveur HTTP sur ton infrastructure.

### Installation rapide sur pve1

```bash
git clone https://github.com/Showdown76py/TarkaMCP.git /opt/tarkamcp
cd /opt/tarkamcp
sudo bash deploy/install.sh
```

Le script installe les dépendances, crée le `.env`, et configure le service systemd.

Ensuite :
```bash
# 1. Éditer le .env avec tes vrais credentials Proxmox
nano /opt/tarkamcp/.env

# 2. Créer un client OAuth (pour se connecter depuis Claude/ChatGPT/Gemini)
tarkamcp auth create --name "Mon iPhone"
#   → Client ID:     tarkamcp_abc123...
#   → Client Secret: sk_def456...
#   Note-les, le secret ne sera plus affiché.

# 3. Démarrer le serveur
sudo systemctl start tarkamcp

# 4. Vérifier
curl http://localhost:8420/health
```

### Gérer les clients

```bash
# Créer un client par appareil / plateforme
tarkamcp auth create --name "Claude Web"
tarkamcp auth create --name "ChatGPT"
tarkamcp auth create --name "Gemini"

# Lister les clients existants
tarkamcp auth list

# Révoquer un accès
tarkamcp auth revoke tarkamcp_abc123...
```

### Exposer via Cloudflare Tunnel

Dans ton dashboard Cloudflare Zero Trust, ajouter un tunnel public :

| Paramètre | Valeur |
|-----------|--------|
| **Hostname** | `mcp.example.com` (ou ton choix) |
| **Service** | `http://localhost:8420` |

L'URL de ton serveur MCP sera : `https://mcp.example.com/mcp`

---

## Connexion par plateforme

### Claude Code (local, stdio)

Ajouter dans `~/.claude/settings.json` :

```json
{
  "mcpServers": {
    "tarkamcp": {
      "command": "python",
      "args": ["-m", "tarkamcp"],
      "cwd": "/chemin/vers/TarkaMCP"
    }
  }
}
```

L'authentification utilise **OAuth 2.1 client credentials**. Tu crées un client sur le serveur (`tarkamcp auth create`), et tu utilises le Client ID + Secret pour te connecter depuis n'importe quelle plateforme.

### Claude (web & mobile)

1. Aller dans **Settings** > **Integrations** > **Add custom connector**
2. Remplir :
   - **Name** : `TarkaMCP`
   - **Remote MCP server URL** : `https://mcp.example.com/mcp`
   - **OAuth Client ID** : `tarkamcp_abc123...` (obtenu via `tarkamcp auth create`)
   - **OAuth Client Secret** : `sk_def456...`
3. Cliquer **Add**

### ChatGPT

1. Aller dans **Settings** > **Developer Mode** > **MCP Servers**
2. Ajouter un serveur avec l'URL : `https://mcp.example.com/mcp`
3. Pour l'auth, obtenir un bearer token :
   ```bash
   curl -X POST https://mcp.example.com/oauth/token \
     -d "grant_type=client_credentials&client_id=tarkamcp_abc123&client_secret=sk_def456"
   # → {"access_token": "xxxx", "token_type": "bearer", ...}
   ```
4. Utiliser l'`access_token` comme bearer token

### Gemini CLI

```bash
# Obtenir un token
TOKEN=$(curl -s -X POST https://mcp.example.com/oauth/token \
  -d "grant_type=client_credentials&client_id=tarkamcp_abc123&client_secret=sk_def456" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Ajouter le serveur MCP
gemini mcp add tarkamcp --url https://mcp.example.com/mcp \
  --header "Authorization: Bearer $TOKEN"
```

### Gemini API (programmatique)

```python
import requests
from google import genai

# 1. Obtenir un token
resp = requests.post("https://mcp.example.com/oauth/token", data={
    "grant_type": "client_credentials",
    "client_id": "tarkamcp_abc123",
    "client_secret": "sk_def456",
})
token = resp.json()["access_token"]

# 2. Utiliser avec Gemini
client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="Liste les VMs sur pve1",
    config={
        "tools": [{
            "mcp_servers": [{
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }]
        }]
    },
)
```

### Vérification

Depuis n'importe quelle plateforme, demander :
```
Utilise proxmox_list_nodes pour voir l'état du cluster
```

L'IA devrait appeler l'outil et afficher les nœuds.

---

## Contexte infrastructure

Éditer `infrastructure.yaml` pour définir tes conventions. Ce fichier est exposé comme ressource MCP (`tarkamcp://infrastructure`) et donne à Claude le contexte de ton infra.

```yaml
conventions:
  vmid_to_ip: "CT VMID corresponds to local IP 192.168.1.{VMID}"
  naming: "VMs are prefixed by their role (e.g., web-101, db-102)"

nodes:
  pve1:
    host: pve1.example.com
    role: "Primary node"
    local_network: "192.168.1.0/24"
  pve2:
    host: pve2.example.com
    role: "Secondary node"

ilo:
  host: "192.168.1.X"
  access: "Local network only, via SSH tunnel through pve1"

notes:
  - "iLO is accessible only through pve1 as SSH jump host"
  - "Zyxel USG 210 is the network gateway (no API)"
```

---

## Tests

### Lancer les tests d'intégration

Les tests se lancent contre la vraie infrastructure. Ils nécessitent un `.env` rempli.

```bash
# Tous les tests (sauf lifecycle VM)
python tests/test_integration.py

# Section par section
python tests/test_integration.py --section proxmox
python tests/test_integration.py --section ssh
python tests/test_integration.py --section ilo

# Avec tests de lifecycle VM (start/stop/clone -- utilise un VMID de test)
python tests/test_integration.py --test-vmid 9999
```

### Ce que les tests vérifient

| Section | Tests | Description |
|---------|-------|-------------|
| **Proxmox Monitoring** | 12 | list_nodes, node_status, list_vms, vm_status, get_logs, get_tasks |
| **Proxmox System** | 4 | storage_status, network_config |
| **Proxmox Exec (QEMU)** | 5 | exec sync, exit codes, async+poll, invalid VMID |
| **Proxmox Exec (LXC)** | 1 | exec dans un conteneur LXC |
| **VM Lifecycle** | 7 | start, stop, restart, config read/write, clone (nécessite --test-vmid) |
| **SSH** | 8 | exec sur pve1, exit codes, host resolution, async+poll, sessions |
| **iLO** | 6 | server_info, health, power_status, event_log |
| **Resources** | 4 | infrastructure resource, prompt, config validation |
| **Error Handling** | 4 | invalid node, VMID, exec_id |

Total : **~50 tests**

### Créer une VM de test (optionnel)

Pour les tests de lifecycle (start/stop/clone), créer une VM légère :

```bash
# Sur pve1, créer une VM vide VMID 9999
qm create 9999 --name tarkamcp-test --memory 128 --cores 1 --net0 virtio,bridge=vmbr0
```

Puis lancer :
```bash
python tests/test_integration.py --test-vmid 9999
```

---

## Outils disponibles

### Proxmox -- Monitoring (6 outils)

| Outil | Description |
|-------|-------------|
| `proxmox_list_nodes` | Liste les nœuds du cluster avec leur statut |
| `proxmox_node_status` | CPU, RAM, disque, uptime, version PVE d'un nœud |
| `proxmox_list_vms` | Liste toutes les VMs/CTs avec statut et ressources |
| `proxmox_vm_status` | État détaillé d'une VM/CT spécifique |
| `proxmox_get_logs` | Logs système (syslog) ou tâches Proxmox |
| `proxmox_get_tasks` | Tâches récentes (migrations, backups, etc.) |

### Proxmox -- Gestion VMs (7 outils)

| Outil | Description |
|-------|-------------|
| `proxmox_vm_start` | Démarrer une VM/CT |
| `proxmox_vm_stop` | Arrêter une VM/CT (clean ou force) |
| `proxmox_vm_restart` | Redémarrer une VM/CT |
| `proxmox_vm_create` | Créer une nouvelle VM/CT |
| `proxmox_vm_clone` | Cloner une VM/CT existante |
| `proxmox_vm_migrate` | Migrer une VM/CT vers un autre nœud |
| `proxmox_vm_config` | Lire ou modifier la config d'une VM/CT |

### Proxmox -- Système (5 outils)

| Outil | Description |
|-------|-------------|
| `proxmox_storage_status` | État du stockage (local, NFS, CEPH, etc.) |
| `proxmox_network_config` | Configuration réseau du nœud |
| `proxmox_exec_command` | Exécuter une commande dans une VM/CT (sync) |
| `proxmox_exec_command_async` | Lancer une commande longue (async) |
| `proxmox_exec_get_result` | Récupérer le résultat d'une commande async |

### SSH (4 outils)

| Outil | Description |
|-------|-------------|
| `ssh_exec_command` | Commande SSH sur un hôte (sync) |
| `ssh_exec_command_async` | Commande SSH longue (async) |
| `ssh_exec_get_result` | Résultat d'une commande SSH async |
| `ssh_list_sessions` | Lister les sessions SSH actives |

### iLO (7 outils)

| Outil | Description |
|-------|-------------|
| `ilo_server_info` | Modèle, serial, firmware du serveur |
| `ilo_health_status` | Températures, ventilateurs, alims, disques, RAM |
| `ilo_power_status` | État d'alimentation (ON/OFF) |
| `ilo_power_on` | Allumer le serveur physique |
| `ilo_power_off` | Éteindre le serveur (clean ou force) |
| `ilo_power_reset` | Hard reset du serveur |
| `ilo_get_event_log` | Journal d'événements iLO |

---

## Dépannage

### "PVE1_HOST, PVE1_TOKEN_ID, and PVE1_TOKEN_SECRET are required"

Le `.env` n'est pas chargé ou les variables ne sont pas définies. Vérifier :
```bash
cat .env | grep PVE1
```

### "Node 'pveX' is unreachable"

Le nœud Proxmox ne répond pas sur le port 8006. Vérifier :
```bash
curl -sk https://pve1.example.com:8006/api2/json/version
```

### "QEMU Guest Agent may not be running"

Le Guest Agent n'est pas installé ou pas actif dans la VM. Voir la section [Installer le QEMU Guest Agent](#2-installer-le-qemu-guest-agent-dans-les-vms).

### "iLO is accessible only through pve1 (SSH tunnel)"

L'iLO est sur le réseau local. Si pve1 est down, l'iLO est inaccessible. Vérifier pve1 d'abord.

### "SSH connection to 'X' failed"

Vérifier que SSH par mot de passe est actif et que les credentials sont corrects :
```bash
ssh root@pve1.example.com
```

### Certificats SSL

Par défaut `PVE_VERIFY_SSL=false` car Proxmox utilise des certificats self-signed. Si tu as configuré des certificats valides (Let's Encrypt), mets `PVE_VERIFY_SSL=true`.

---

## Exemple d'utilisation

Une fois configuré, tu peux demander à Claude des choses comme :

> **"pve2 ne répond plus, qu'est-ce qui se passe ?"**
>
> Claude va automatiquement :
> 1. Appeler `proxmox_list_nodes` -- voir que pve2 est offline
> 2. Appeler `ilo_power_status` -- vérifier si le serveur est physiquement allumé
> 3. Appeler `ilo_health_status` -- checker les températures, ventilateurs, disques
> 4. Te proposer un diagnostic et une action (power cycle, vérifier les logs, etc.)

> **"Combien de RAM utilise la VM 101 ?"**
>
> Claude appelle `proxmox_vm_status(node="pve1", vmid=101)` et te donne les détails.

> **"Mets à jour les paquets sur tous les conteneurs"**
>
> Claude utilise `proxmox_list_vms` pour lister les CTs, puis `proxmox_exec_command_async` pour lancer `apt update && apt upgrade -y` dans chacun, et poll les résultats avec `proxmox_exec_get_result`.

---

## Licence

Ce projet est sous licence [Apache 2.0](LICENSE).

---

<div align="center">
<sub>Construit avec <a href="https://claude.ai/code">Claude Code</a></sub>
</div>
