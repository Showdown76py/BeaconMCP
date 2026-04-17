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
- [Dashboard web](#dashboard-web)
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

Un panel web optionnel est servi par TarkaMCP sur la même URL (`https://mcp.tarkacore.dev/app/...`). Trois pages :

- **`/app/login`** — récupère un bearer MCP via Client ID + Secret + TOTP, session persistée 90 jours dans un cookie HttpOnly. Plus de curl sur téléphone.
- **`/app/chat`** — chat multi-conversations avec Gemini 2.5 Flash/Pro (stable) ou Gemini 3 Flash / 3.1 Pro (preview, allowlist Google requise).
- **`/app/tokens`** — génère des bearers nommés pour brancher TarkaMCP sur Gemini web, ChatGPT, Claude Desktop, etc. sans passer par curl.

### Activation

1. Ajouter une clé Gemini au `.env` :
   ```env
   GEMINI_API_KEY=...
   ```
2. La clé de chiffrement de session (`TARKAMCP_SESSION_KEY`) est auto-générée par `install.sh` au premier run. Si tu déploies à la main :
   ```bash
   echo "TARKAMCP_SESSION_KEY=$(openssl rand -base64 32)" >> /opt/tarkamcp/.env
   ```
3. Redémarrer : `systemctl restart tarkamcp`.

Au démarrage, le serveur affiche :
```
Dashboard: http://0.0.0.0:8420/app/login
```
(ou `disabled` si `GEMINI_API_KEY` manque).

### Flow

1. Tu ouvres `https://mcp.tarkacore.dev/` sur ton téléphone → redirige vers `/app/login`.
2. Tu tapes Client ID + Client Secret + code TOTP (une seule fois tous les 90 jours).
3. Tu arrives sur `/app/chat` avec l'historique de tes conversations. Une card "Tokens API" prominente dans la sidebar mène à `/app/tokens`.
4. Toutes les 24 h le bearer MCP expire — le dashboard redemande *juste* le code TOTP (client_id et secret stockés chiffrés côté serveur).

### Page Tokens API

Accessible via la card "Tokens API" dans la sidebar du chat, ou directement à `/app/tokens`. Conçue pour les utilisateurs qui branchent TarkaMCP sur un client MCP externe plutôt que d'utiliser le chat intégré.

Elle affiche :
- L'URL MCP à coller dans le client externe (bouton Copier).
- Un formulaire de création qui **exige un nom** (max 60 caractères, ex. "Gemini Web", "ChatGPT macOS") + le code TOTP courant.
- Le token généré une **seule fois** dans une card orange avec bouton Copier — après rechargement il n'est plus affiché.
- La liste des tokens actifs : nom, préfixe 12 car, heures avant expiration, bouton Révoquer.

Contraintes :

| Paramètre | Valeur |
|-----------|--------|
| Expiration | **24 h** (hérité de `TokenStore.TOKEN_TTL`) |
| Cap par client | **3 tokens actifs** maximum |
| TOTP | Re-vérifié à chaque création |
| Révocation | Par préfixe (≥ 6 car), scoped au client propriétaire |
| Stockage | **En mémoire** — un `systemctl restart` invalide tous les tokens |

### Chat : modèles & thinking

- **Gemini 2.5 Flash / Pro** : dispos sur toute clé AI Studio. **Utilisés par défaut** (`gemini-2.5-flash`).
- **Gemini 3 Flash / 3.1 Pro (preview)** : gated par allowlist Google. Sans allowlist, le dashboard affiche un message clair indiquant de rebasculer sur 2.5.
- **Effort de thinking** : `minimal` / `low` / `medium` / `high` via dropdown. `gemini-2.5-pro` ne peut pas désactiver le thinking — le budget est automatiquement clampé à 128 tokens minimum.
- **Rendu markdown** : le client parse les headings (`#`–`######`), listes ordonnées/non-ordonnées, blockquotes, horizontal rules, code fences (avec `lang-*` class), inline code, bold/italic/strike et links HTTP(S).

### Confirmation obligatoire pour SSH exec

`ssh_exec_command` et `ssh_exec_command_async` ne s'exécutent **jamais** sans clic manuel depuis le chat. Quand Gemini demande à lancer une commande SSH :

1. La tool-card apparaît en état "approbation requise" (badge orange, auto-ouverte pour voir les `args`).
2. Deux boutons : **Autoriser** / **Refuser**.
3. Tant que tu n'as pas cliqué, le turn Gemini reste bloqué côté serveur (timeout à 5 min).
4. Si tu refuses, Gemini reçoit un `FunctionResponse {"error": "user_rejected"}` et peut adapter sa réponse.

L'allow-list est hardcodée dans `src/tarkamcp/dashboard/chat.py` (`_NEEDS_CONFIRMATION`). Pour l'étendre (par exemple aux outils `proxmox_exec_*`), ajoute les noms à ce set.

### Données stockées

SQLite à `/opt/tarkamcp/dashboard.db` (WAL). Trois tables :
- `sessions` — cookie → client_id + client_secret chiffré AES-GCM + bearer courant.
- `conversations` — titre, modèle, effort, client propriétaire.
- `messages` — user/assistant, contenu, tool_calls JSON, thinking résumé.

Tout est scopé par `client_id` ; tu peux avoir plusieurs sessions actives (téléphone + PC) pour un même client.

### Architecture MCP interne

Le dashboard tient lui-même une session MCP (`streamablehttp_client` + `ClientSession`) vers le `/mcp` local (`http://127.0.0.1:8420/mcp`). Les outils sont convertis manuellement en `FunctionDeclaration` et la boucle `function_call` / `function_response` est orchestrée côté serveur (AFC SDK désactivé via `AutomaticFunctionCallingConfig(disable=True)`) pour contourner des bugs connus de google-genai sur Gemini 2.5 Pro avec MCP + streaming + thinking.

> Un mode "remote" (McpServer backend-driven) existait mais a été désactivé : il produisait systématiquement des 500 INTERNAL à cause de la perte de l'header Authorization via Cloudflare Tunnel. Si `TARKAMCP_DASHBOARD_MCP_MODE=remote` est défini, le service affiche un warning au boot et chaque turn chat retourne un message d'erreur actionable.

### Robustesse aux redémarrages

Le `TokenStore` est en mémoire : après `systemctl restart tarkamcp`, les bearers sont invalidés alors que les sessions dashboard (SQLite) persistent. Le dashboard détecte cela via `TokenStore.validate()` sur chaque route sensible ; si le bearer n'existe plus côté MCP mais que la session est encore timestamp-valide, l'utilisateur est redirigé vers `/app/refresh` pour retaper son TOTP et émettre un nouveau bearer.

Conséquence pour les tokens externes (`/app/tokens`) : un restart du service force toutes les intégrations Gemini web / ChatGPT / Claude Desktop à régénérer leur token. Si ça devient gênant, migrer le `TokenStore` vers SQLite (non fait actuellement).

### Désactiver

```env
TARKAMCP_DASHBOARD_ENABLED=false
```
Ou retire simplement `GEMINI_API_KEY`.

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
| `421 Misdirected Request` | Hostname public absent de l'allowlist | Ajouter le domaine à `TARKAMCP_ALLOWED_HOSTS` dans `.env` puis redémarrer |
| `{"error":"unauthorized"}` sur `/authorize` | Client ID inexistant côté serveur | Créer le client avec `tarkamcp auth create`, puis recoller l'ID dans le connecteur |
| `invalid_grant` + `missing or invalid totp` | Code 2FA faux, expiré (>30 s), ou déjà utilisé | Générer un nouveau code dans l'app. Vérifier l'horloge du serveur vs celle du téléphone (`timedatectl`). |
| Page 2FA affiche "Trop de tentatives" | 5 codes faux consécutifs → lockout 5 min | Attendre. Le compteur se réinitialise à la prochaine validation correcte. |
| Clients silencieusement révoqués après update | Migration 2FA : les anciens clients sans TOTP sont rejetés au démarrage | Regarder `journalctl -u tarkamcp` pour la liste, recréer via `tarkamcp auth create` |
| Dashboard boucle entre `/app/refresh` et `/app/chat` | Bearer wipe après redémarrage du service | Taper le code TOTP sur la page de refresh pour regénérer un bearer |
| Clients MCP externes déconnectés après un restart | `TokenStore` en mémoire, wipé au restart | Recréer les tokens dans `/app/tokens` (max 3, expire 24 h) |
| `event: error ... "code": "remote_mode_disabled"` dans le chat | `TARKAMCP_DASHBOARD_MCP_MODE=remote` dans `.env` | Retirer la ligne du `.env` et redémarrer — seul le mode local est supporté |
| Chat bloqué sur une card SSH avec deux boutons | Confirmation obligatoire pour `ssh_exec_command*` | Cliquer **Autoriser** ou **Refuser**. Timeout à 5 min sinon auto-reject |
| Tokens API : "Limite atteinte : maximum 3 tokens" | 3 tokens nommés déjà actifs pour ce client | Révoquer un token existant dans la liste avant d'en créer un nouveau |

---

## Exemple d'utilisation

> **"pve2 ne répond plus, qu'est-ce qui se passe ?"**
>
> L'IA va : `proxmox_list_nodes` → voit pve2 offline → `ilo_power_status` → vérifie si allumé → `ilo_health_status` → checker le hardware → proposer un diagnostic

> **"Mets à jour les paquets sur tous les conteneurs"**
>
> L'API Proxmox n'expose pas d'endpoint `exec` pour les LXC. L'IA utilise
> donc `proxmox_list_vms` → liste les CTs → `ssh_exec_command_async` sur le
> nœud hôte avec `pct exec <vmid> -- sh -c 'apt update && apt upgrade -y'`
> pour chacun → poll les résultats.

---

## Licence

[Apache 2.0](LICENSE)

<div align="center">
<sub>Construit avec <a href="https://claude.ai/code">Claude Code</a></sub>
</div>
