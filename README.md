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
- [Connexion par plateforme](#connexion-par-plateforme)
- [Dashboard web](#dashboard-web)
- [Sécurité : review manuelle des actions sensibles](#sécurité--review-manuelle-des-actions-sensibles)
- [Configuration Proxmox](#configuration-proxmox)
- [Configuration iLO](#configuration-ilo)
- [Configuration .env](#configuration-env)
- [Tests](#tests) — détails dans [docs/tests.md](docs/tests.md)
- [Outils disponibles](#outils-disponibles)
- [Dépannage & exemples](#dépannage--exemples) — détails dans [docs/troubleshooting.md](docs/troubleshooting.md)

---

## Architecture

```
Clients (Claude, ChatGPT, Gemini)
        |
        | HTTPS (Cloudflare Tunnel)
        v
+--[ pve1.tarkacore.dev ]-------------------+
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
  pve2.tarkacore.dev
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

### 3. Créer un client OAuth (avec 2FA)

```bash
tarkamcp auth create --name "Claude Web"
```

```
  Client ID:     tarkamcp_a1b2c3...
  Client Secret: sk_d4e5f6...

  --- 2FA / Google Authenticator ---
  Scanne ce QR code dans ton app (Google Authenticator, Authy, 1Password) :

  █▀▀▀▀▀█ ▄▀ ▄█ █▀▀▀▀▀█
  █ ███ █ ▀ ▄▄▄ █ ███ █
  ...

  Secret manuel : JBSWY3DPEHPK3PXP
  URI otpauth   : otpauth://totp/TarkaMCP:tarkamcp_...?secret=...&issuer=TarkaMCP
```

**Important** : le Client Secret ET le secret TOTP ne sont affichés qu'une seule fois. Scanne le QR tout de suite dans ton app d'authentification, sinon tu devras révoquer et recréer le client.

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
| **Hostname** | `mcp.tarkacore.dev` |
| **Service** | `http://localhost:8420` |

Puis déclarer ce hostname dans `.env` via `TARKAMCP_ALLOWED_HOSTS`, sinon le
SDK MCP renverra `421 Misdirected Request` (protection DNS-rebinding).

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
   - **Remote MCP server URL** : `https://mcp.tarkacore.dev/mcp`
   - **OAuth Client ID** : `tarkamcp_a1b2c3...`
   - **OAuth Client Secret** : `sk_d4e5f6...`
3. **Add**

À la connexion, une page TarkaMCP s'ouvre dans ton navigateur et demande le code 2FA à 6 chiffres depuis Google Authenticator. Saisis-le, tu es redirigé vers Claude automatiquement. Le token dure 24 h, après quoi Claude redemande le code.

### ChatGPT

1. **Settings** > **Developer Mode** > **MCP Servers**
2. URL : `https://mcp.tarkacore.dev/mcp`
3. Obtenir un bearer token. Deux options :
   - **Via le dashboard** (recommandé) : [page Tokens API](#page-tokens-api) → crée un token nommé "ChatGPT", copie la valeur.
   - **Via curl** :
     ```bash
     TOTP=$(oathtool --totp -b "$TOTP_SECRET")   # ou tape-le depuis l'app
     curl -X POST https://mcp.tarkacore.dev/oauth/token \
       -d "grant_type=client_credentials&client_id=ID&client_secret=SECRET&totp=$TOTP"
     ```
4. Utiliser la valeur comme bearer token. Expire dans 24 h.

### Gemini CLI

```bash
# Option 1 : récupère un token depuis le dashboard (Tokens API → nouveau token "Gemini CLI")
gemini mcp add tarkamcp --url https://mcp.tarkacore.dev/mcp \
  --header "Authorization: Bearer <token-copié-depuis-le-dashboard>"

# Option 2 : via curl
TOTP=$(oathtool --totp -b "$TOTP_SECRET")
TOKEN=$(curl -s -X POST https://mcp.tarkacore.dev/oauth/token \
  -d "grant_type=client_credentials&client_id=ID&client_secret=SECRET&totp=$TOTP" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

gemini mcp add tarkamcp --url https://mcp.tarkacore.dev/mcp \
  --header "Authorization: Bearer $TOKEN"
```

Le token est valide 24 h (option 1 comme option 2). Recréer un nouveau token dans le dashboard ou relancer le bloc curl pour renouveler.

### Gemini API

```python
import requests, pyotp
from google import genai

totp = pyotp.TOTP("JBSWY3DPEHPK3PXP").now()   # le secret affiché à la création
token = requests.post("https://mcp.tarkacore.dev/oauth/token", data={
    "grant_type": "client_credentials",
    "client_id": "tarkamcp_...",
    "client_secret": "sk_...",
    "totp": totp,
}).json()["access_token"]

client = genai.Client()
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="Liste les VMs sur pve1",
    config={"tools": [{"mcp_servers": [{
        "url": "https://mcp.tarkacore.dev/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }]}]},
)
```

> Stocker le secret TOTP dans le code va à l'encontre de l'intérêt du 2FA. Préfère un vault (1Password CLI, `pass`, secret manager) ou tape le code à la main.

---

## Dashboard web

Un panel web optionnel est servi par TarkaMCP sur la même URL (`https://mcp.tarkacore.dev/app/...`) — login TOTP, chat Gemini multi-conversations, et génération de tokens API pour brancher des clients MCP externes (Gemini web, ChatGPT, Claude Desktop…).

Le chat nécessite `GEMINI_API_KEY` ; la page **Tokens API** fonctionne sans. Détails complets (modes, pages, flow, modèles, sécurité SSH, tarifs, limites d'usage, architecture) dans [docs/dashboard.md](docs/dashboard.md).

---

## Sécurité : review manuelle des actions sensibles

> **Ne laisse jamais un LLM exécuter des commandes shell sur ton infra sans les avoir relues toi-même.**

TarkaMCP expose des outils qui peuvent faire des dégâts irréversibles (`ssh_exec_command*`, `proxmox_exec_command*`, power off iLO, `vm_stop`, `vm_create`, etc.). Le LLM ne comprend pas toujours les conséquences d'une commande — un `rm -rf` "pour faire propre", un `systemctl stop` sur le mauvais service, un `pct destroy` au lieu de `pct stop`. Quelques règles :

- **Désactive l'auto-approve** sur chaque client MCP externe (Claude Desktop, Gemini CLI, ChatGPT MCP, etc.). La plupart offrent un toggle "Approve each tool call" ou équivalent — garde-le **activé**, et refuse l'option "Always allow this tool".
- **Relis l'argument `command` avant d'autoriser** un appel `ssh_exec_command*` ou `proxmox_exec_command*`. Pose-toi la question : "si cette commande tournait sur la mauvaise VM / le mauvais host, est-ce que je pourrais récupérer ?"
- **Le chat intégré (`/app/chat`) force déjà une approbation humaine** pour `ssh_exec_command`, `ssh_exec_command_async`, `proxmox_exec_command` et `proxmox_exec_command_async` — même si tu cliques vite, lis au moins les `args` de la tool-card. Le timeout est à 5 min et l'absence de réponse vaut refus.
- **Préfère les outils lecture-seule** (`*_list_*`, `*_status`, `*_get_*`, `get_logs`, `health_status`…) pour l'exploration. Ils ne sont jamais bloqués par confirmation parce qu'ils ne peuvent rien casser.
- **Ne partage jamais** un bearer `/app/tokens` avec un client MCP qui n'est pas le tien. Un token compromis = shell arbitraire sur pve1/pve2 pendant 24 h.

Un `systemctl restart tarkamcp` invalide tous les bearers en mémoire : en cas de doute sur un token qui fuite, c'est la corde de panique.

---

## Configuration Proxmox

### Créer un API token

Sur l'interface web Proxmox (`https://pve1.tarkacore.dev`) :

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

L'iLO est sur le réseau local. TarkaMCP y accède via un tunnel SSH ouvert sur
`ILO_JUMP_HOST` (par défaut `pve1`) ; les credentials SSH doivent donc être
configurés. Quand TarkaMCP tourne lui-même sur pve1, le tunnel est trivial
(localhost → iLO) mais reste nécessaire vu que python-hpilo est synchrone.

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
PVE1_HOST=pve1.tarkacore.dev
PVE1_TOKEN_ID=root@pam!tarkamcp
PVE1_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# OPTIONNEL -- PVE2
PVE2_HOST=pve2.tarkacore.dev
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

# OBLIGATOIRE en prod -- hostnames publics autorisés par la protection
# DNS-rebinding du SDK MCP (sinon 421 Misdirected Request). Virgules.
TARKAMCP_ALLOWED_HOSTS=mcp.tarkacore.dev,127.0.0.1:*,localhost:*,[::1]:*
# TARKAMCP_ALLOWED_ORIGINS=https://claude.ai,https://chat.openai.com,https://gemini.google.com
```

Modules chargés conditionnellement : sans SSH → pas de `ssh_*`, sans iLO → pas de `ilo_*`.

---

## Tests

Tests unitaires (`pytest`) pour le dashboard + tests d'intégration (`python tests/test_integration.py`) qui tapent la vraie infra. Détails des sections, flags CLI et prérequis : [docs/tests.md](docs/tests.md).

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

## Dépannage & exemples

Tableau des erreurs courantes, causes et correctifs — plus quelques scénarios d'usage type — dans [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Licence

[Apache 2.0](LICENSE)

<div align="center">
<sub>Construit avec <a href="https://claude.ai/code">Claude Code</a></sub>
</div>
