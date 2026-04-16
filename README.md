# TarkaMCP

Serveur MCP pour la gestion d'infrastructure Proxmox VE via Claude. Donne a Claude un acces direct a tes noeuds Proxmox, a l'iLO HP, et au SSH pour diagnostiquer, gerer les VMs/CTs, et resoudre les problemes d'infrastructure.

## Table des matieres

- [Architecture](#architecture)
- [Installation cote client (ta machine)](#installation-cote-client)
- [Configuration cote serveur (Proxmox)](#configuration-cote-serveur-proxmox)
- [Configuration cote serveur (iLO)](#configuration-cote-serveur-ilo)
- [Configuration du .env](#configuration-du-env)
- [Integration Claude Code](#integration-claude-code)
- [Tests](#tests)
- [Outils disponibles](#outils-disponibles)
- [Depannage](#depannage)

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
                                +---> |  iLO 4 (reseau local)      |
                            tunnel    |    via pve1 SSH             |
                            SSH       +-----------------------------+
```

## Installation cote client

### Prerequis

- Python >= 3.11
- pip
- Acces reseau vers pve1.example.com (port 8006 pour l'API, port 22 pour SSH)

### Installation

```bash
cd TarkaMCP
pip install -e .
```

### Verification rapide

```bash
# Avec les variables d'environnement configurees
python -c "
from dotenv import load_dotenv; load_dotenv()
from tarkamcp.server import mcp
print(f'OK: {len(mcp._tool_manager._tools)} outils enregistres')
"
```

---

## Configuration cote serveur (Proxmox)

### 1. Creer un API token sur chaque noeud

Se connecter a l'interface web Proxmox (`https://pve1.example.com`).

1. Aller dans **Datacenter** > **Permissions** > **API Tokens**
2. Cliquer **Add**
3. Remplir :
   - **User** : `root@pam`
   - **Token ID** : `tarkamcp`
   - **Privilege Separation** : **decocher** (important, sinon le token n'a aucun privilege)
4. Cliquer **Add**
5. **Copier le token secret** affiche (il ne sera plus visible apres)

Le Token ID complet sera : `root@pam!tarkamcp`

Repeter sur pve2 quand il sera de retour.

### 2. Installer le QEMU Guest Agent dans les VMs

Le Guest Agent est necessaire pour executer des commandes a l'interieur des VMs via l'API Proxmox.

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

**Verification :**
```bash
systemctl status qemu-guest-agent
# Doit afficher "active (running)"
```

Puis dans Proxmox, activer le Guest Agent pour la VM :
1. Aller dans la VM > **Options** > **QEMU Guest Agent**
2. Cocher **Use QEMU Guest Agent**
3. Redemarrer la VM

**Note :** Le Guest Agent n'est pas necessaire pour les conteneurs LXC -- Proxmox a un acces direct.

### 3. Configurer l'acces SSH (optionnel mais recommande)

Le serveur MCP utilise SSH comme fallback quand l'API Proxmox ne suffit pas. L'acces SSH par mot de passe doit etre actif sur les noeuds Proxmox.

Verifier que c'est le cas :
```bash
# Sur le noeud Proxmox
grep -E "^PasswordAuthentication" /etc/ssh/sshd_config
# Doit afficher: PasswordAuthentication yes
```

Si non :
```bash
sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl restart sshd
```

### 4. Verifier les ports ouverts

Le serveur MCP a besoin de ces acces reseau :

| Service | Port | Protocole | Depuis |
|---------|------|-----------|--------|
| Proxmox API | 8006 | HTTPS | Ta machine |
| SSH (noeuds) | 22 | SSH | Ta machine |
| iLO | 443 | HTTPS | pve1 (reseau local) |

---

## Configuration cote serveur (iLO)

L'iLO est sur le reseau local uniquement. TarkaMCP y accede via un tunnel SSH a travers pve1.

### Prerequis

- iLO 4 accessible depuis le reseau local de pve1
- Credentials iLO (par defaut : `Administrator` / mot de passe configure)

### Trouver l'IP de l'iLO

Depuis pve1 :
```bash
# Scanner le reseau local pour trouver l'iLO
# L'iLO repond generalement sur le port 443 et 17988
nmap -sn 192.168.1.0/24 | grep -B2 "HP\|iLO\|Hewlett"

# Ou si tu connais l'IP, verifier
curl -sk https://192.168.1.X/xmldata?item=All | head -20
```

### Tester l'acces iLO depuis pve1

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

Editer `.env` :

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

# OPTIONNEL -- SSH (recommande)
SSH_USER=root
SSH_PASSWORD=ton_mot_de_passe_root

# OPTIONS
PVE_VERIFY_SSL=false
```

Les modules sont charges conditionnellement :
- **Sans SSH** : les 4 outils `ssh_*` ne sont pas disponibles
- **Sans iLO** : les 7 outils `ilo_*` ne sont pas disponibles
- **Sans PVE2** : les outils fonctionnent mais seul pve1 est interroge

---

## Integration Claude Code

### Option A : Settings globaux

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

Avec cette option, le `.env` doit etre dans le dossier `TarkaMCP/`.

### Option B : Settings avec variables inline

```json
{
  "mcpServers": {
    "tarkamcp": {
      "command": "python",
      "args": ["-m", "tarkamcp"],
      "cwd": "/chemin/vers/TarkaMCP",
      "env": {
        "PVE1_HOST": "pve1.example.com",
        "PVE1_TOKEN_ID": "root@pam!tarkamcp",
        "PVE1_TOKEN_SECRET": "ton-token-secret",
        "SSH_USER": "root",
        "SSH_PASSWORD": "ton-password",
        "ILO_HOST": "192.168.1.X",
        "ILO_USER": "Administrator",
        "ILO_PASSWORD": "ton-password-ilo",
        "ILO_JUMP_HOST": "pve1",
        "PVE_VERIFY_SSL": "false"
      }
    }
  }
}
```

### Verification dans Claude Code

Une fois configure, relancer Claude Code et verifier :
```
> Utilise proxmox_list_nodes pour voir l'etat du cluster
```

Claude devrait appeler l'outil et afficher les noeuds.

---

## Contexte infrastructure

Editer `infrastructure.yaml` pour definir tes conventions. Ce fichier est expose comme ressource MCP (`tarkamcp://infrastructure`) et donne a Claude le contexte de ton infra.

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

### Lancer les tests d'integration

Les tests se lancent contre la vraie infrastructure. Ils necessitent un `.env` rempli.

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

### Ce que les tests verifient

| Section | Tests | Description |
|---------|-------|-------------|
| **Proxmox Monitoring** | 12 | list_nodes, node_status, list_vms, vm_status, get_logs, get_tasks |
| **Proxmox System** | 4 | storage_status, network_config |
| **Proxmox Exec (QEMU)** | 5 | exec sync, exit codes, async+poll, invalid VMID |
| **Proxmox Exec (LXC)** | 1 | exec dans un conteneur LXC |
| **VM Lifecycle** | 7 | start, stop, restart, config read/write, clone (necessite --test-vmid) |
| **SSH** | 8 | exec sur pve1, exit codes, host resolution, async+poll, sessions |
| **iLO** | 6 | server_info, health, power_status, event_log |
| **Resources** | 4 | infrastructure resource, prompt, config validation |
| **Error Handling** | 4 | invalid node, VMID, exec_id |

Total : **~50 tests**

### Creer une VM de test (optionnel)

Pour les tests de lifecycle (start/stop/clone), creer une VM legere :

```bash
# Sur pve1, creer une VM vide VMID 9999
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
| `proxmox_list_nodes` | Liste les noeuds du cluster avec leur statut |
| `proxmox_node_status` | CPU, RAM, disque, uptime, version PVE d'un noeud |
| `proxmox_list_vms` | Liste toutes les VMs/CTs avec statut et ressources |
| `proxmox_vm_status` | Etat detaille d'une VM/CT specifique |
| `proxmox_get_logs` | Logs systeme (syslog) ou taches Proxmox |
| `proxmox_get_tasks` | Taches recentes (migrations, backups, etc.) |

### Proxmox -- Gestion VMs (7 outils)

| Outil | Description |
|-------|-------------|
| `proxmox_vm_start` | Demarrer une VM/CT |
| `proxmox_vm_stop` | Arreter une VM/CT (clean ou force) |
| `proxmox_vm_restart` | Redemarrer une VM/CT |
| `proxmox_vm_create` | Creer une nouvelle VM/CT |
| `proxmox_vm_clone` | Cloner une VM/CT existante |
| `proxmox_vm_migrate` | Migrer une VM/CT vers un autre noeud |
| `proxmox_vm_config` | Lire ou modifier la config d'une VM/CT |

### Proxmox -- Systeme (5 outils)

| Outil | Description |
|-------|-------------|
| `proxmox_storage_status` | Etat du stockage (local, NFS, CEPH, etc.) |
| `proxmox_network_config` | Configuration reseau du noeud |
| `proxmox_exec_command` | Executer une commande dans une VM/CT (sync) |
| `proxmox_exec_command_async` | Lancer une commande longue (async) |
| `proxmox_exec_get_result` | Recuperer le resultat d'une commande async |

### SSH (4 outils)

| Outil | Description |
|-------|-------------|
| `ssh_exec_command` | Commande SSH sur un hote (sync) |
| `ssh_exec_command_async` | Commande SSH longue (async) |
| `ssh_exec_get_result` | Resultat d'une commande SSH async |
| `ssh_list_sessions` | Lister les sessions SSH actives |

### iLO (7 outils)

| Outil | Description |
|-------|-------------|
| `ilo_server_info` | Modele, serial, firmware du serveur |
| `ilo_health_status` | Temperatures, ventilateurs, alims, disques, RAM |
| `ilo_power_status` | Etat d'alimentation (ON/OFF) |
| `ilo_power_on` | Allumer le serveur physique |
| `ilo_power_off` | Eteindre le serveur (clean ou force) |
| `ilo_power_reset` | Hard reset du serveur |
| `ilo_get_event_log` | Journal d'evenements iLO |

---

## Depannage

### "PVE1_HOST, PVE1_TOKEN_ID, and PVE1_TOKEN_SECRET are required"

Le `.env` n'est pas charge ou les variables ne sont pas definies. Verifier :
```bash
cat .env | grep PVE1
```

### "Node 'pveX' is unreachable"

Le noeud Proxmox ne repond pas sur le port 8006. Verifier :
```bash
curl -sk https://pve1.example.com:8006/api2/json/version
```

### "QEMU Guest Agent may not be running"

Le Guest Agent n'est pas installe ou pas actif dans la VM. Voir la section [Installer le QEMU Guest Agent](#2-installer-le-qemu-guest-agent-dans-les-vms).

### "iLO is accessible only through pve1 (SSH tunnel)"

L'iLO est sur le reseau local. Si pve1 est down, l'iLO est inaccessible. Verifier pve1 d'abord.

### "SSH connection to 'X' failed"

Verifier que SSH par mot de passe est actif et que les credentials sont corrects :
```bash
ssh root@pve1.example.com
```

### Certificats SSL

Par defaut `PVE_VERIFY_SSL=false` car Proxmox utilise des certificats self-signed. Si tu as configure des certificats valides (Let's Encrypt), mets `PVE_VERIFY_SSL=true`.
