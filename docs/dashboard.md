# Dashboard web TarkaMCP

Panel web optionnel servi par TarkaMCP sur la même URL que le MCP (`https://mcp.example.com/app/...`). Trois pages :

- **`/app/login`** — récupère un bearer MCP via Client ID + Secret + TOTP, session persistée 90 jours dans un cookie HttpOnly. Plus de curl sur téléphone.
- **`/app/chat`** — chat multi-conversations avec Gemini 2.5 Flash/Pro (stable) ou Gemini 3 Flash / 3.1 Pro (preview, allowlist Google requise). **Nécessite `GEMINI_API_KEY`.**
- **`/app/tokens`** — génère des bearers nommés pour brancher TarkaMCP sur Gemini web, ChatGPT, Claude Desktop, etc. sans passer par curl. **Fonctionne sans `GEMINI_API_KEY`.**

## Activation

Le dashboard est actif par défaut dès qu'une `TARKAMCP_SESSION_KEY` est posée. Deux modes :

| Mode | Condition | Pages actives |
|------|-----------|---------------|
| **Complet** | `GEMINI_API_KEY` défini | `/app/login`, `/app/chat`, `/app/tokens` |
| **Tokens only** | pas de `GEMINI_API_KEY` | `/app/login`, `/app/tokens` (le chat redirige vers tokens) |

1. (optionnel) Ajouter une clé Gemini pour activer le chat intégré :
   ```env
   GEMINI_API_KEY=...
   ```
2. La clé de chiffrement de session (`TARKAMCP_SESSION_KEY`) est auto-générée par `install.sh` au premier run. En déploiement manuel :
   ```bash
   echo "TARKAMCP_SESSION_KEY=$(openssl rand -base64 32)" >> /opt/tarkamcp/.env
   ```
3. Redémarrer : `systemctl restart tarkamcp`.

Au démarrage, le serveur affiche :
```
Dashboard: http://0.0.0.0:8420/app/login (chat: enabled)
```
…ou `(chat: disabled, tokens only)` si la clé Gemini manque, ou `disabled` si `TARKAMCP_DASHBOARD_ENABLED=false`.

## Flow

1. Tu ouvres `https://mcp.example.com/` sur ton téléphone → redirige vers `/app/login`.
2. Tu tapes Client ID + Client Secret + code TOTP (une seule fois tous les 90 jours).
3. Tu arrives sur `/app/chat` (ou directement sur `/app/tokens` en mode tokens only) avec l'historique de tes conversations.
4. Toutes les 24 h le bearer MCP expire — le dashboard redemande *juste* le code TOTP (client_id et secret stockés chiffrés côté serveur).

## Page Tokens API

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

## Chat : modèles & thinking

- **Gemini 2.5 Flash / Pro** : dispos sur toute clé AI Studio. **Utilisés par défaut** (`gemini-2.5-flash`).
- **Gemini 3 Flash / 3.1 Pro (preview)** : gated par allowlist Google. Sans allowlist, le dashboard affiche un message clair indiquant de rebasculer sur 2.5.
- **Effort de thinking** : `minimal` / `low` / `medium` / `high` via dropdown. `gemini-2.5-pro` ne peut pas désactiver le thinking — le budget est automatiquement clampé à 128 tokens minimum.
- **Rendu markdown** : le client parse les headings (`#`–`######`), listes ordonnées/non-ordonnées, blockquotes, horizontal rules, code fences (avec `lang-*` class), inline code, bold/italic/strike et links HTTP(S).

## Confirmation obligatoire pour les outils qui font du shell

`ssh_exec_command`, `ssh_exec_command_async`, `proxmox_exec_command` et `proxmox_exec_command_async` ne s'exécutent **jamais** sans clic manuel depuis le chat. Quand Gemini demande à lancer une de ces commandes :

1. La tool-card apparaît en état "approbation requise" (badge orange, auto-ouverte pour voir les `args`).
2. Deux boutons : **Autoriser** / **Refuser**.
3. Tant que tu n'as pas cliqué, le turn Gemini reste bloqué côté serveur (timeout à 5 min).
4. Si tu refuses, Gemini reçoit un `FunctionResponse {"error": "user_rejected"}` et peut adapter sa réponse.

L'allow-list est hardcodée dans `src/tarkamcp/dashboard/chat.py` (`_NEEDS_CONFIRMATION`). Seul le chat intégré applique cette confirmation — les clients MCP externes (Claude Desktop, Gemini CLI, ChatGPT MCP) doivent avoir leur propre mode "approuver chaque appel" activé côté client (voir la section **Sécurité** du README principal).

## Données stockées

SQLite à `/opt/tarkamcp/dashboard.db` (WAL). Cinq tables :
- `sessions` — cookie → client_id + client_secret chiffré AES-GCM + bearer courant.
- `conversations` — titre, modèle, effort, client propriétaire.
- `messages` — user/assistant, contenu, tool_calls JSON, thinking résumé.
- `usage_events` — ledger per-turn : client_id, tokens (prompt/cached/output), coût USD, horodatage.
- `usage_5h_sessions` — une ligne par client avec la session 5h courante (matérialisée pour éviter un `GROUP BY` à chaque pré-check).

Tout est scopé par `client_id` ; tu peux avoir plusieurs sessions actives (téléphone + PC) pour un même client.

## Limites d'usage & coût

Chaque tour du chat calcule son coût USD à partir du `usage_metadata` renvoyé par Gemini (tokens input facturés au tarif cache-réduit quand `cachedContentTokenCount` est non-nul, ce que Gemini 2.5+ applique automatiquement via l'implicit caching dès que le prompt dépasse 1024 tokens pour Flash / 4096 pour Pro — zéro code à écrire côté client).

Deux fenêtres sont appliquées **par client OAuth** :

| Fenêtre | Semantique | Variable d'env | Défaut |
|---------|-----------|----------------|--------|
| **5h** | Session Anthropic-style : ouvre au 1er message après ≥5h d'inactivité, dure 5h pile, puis ferme | `TARKAMCP_DASHBOARD_LIMIT_5H_USD` | `2.0` |
| **Semaine** | Somme rolling sur les 7 derniers jours glissants | `TARKAMCP_DASHBOARD_LIMIT_WEEK_USD` | `10.0` |

Poser la variable à `0` désactive la fenêtre correspondante. Au dépassement, la prochaine requête est rejetée **avant** d'être envoyée à Gemini, avec un message précisant l'heure de reset pour la fenêtre 5h.

Le panel chat affiche en footer une ligne discrète `5H XX% · 7J XX%` (pourcentage consommé par fenêtre), mise à jour après chaque tour via SSE `usage_update`. Cliquer la barre ouvre un modal style Claude avec barres de progression, heure de réinitialisation de la session 5h, label « fenêtre glissante 7j » et bouton « Actualiser ».

Tarifs utilisés (USD / 1M tokens, alignés sur le tarif public Google AI Studio au 2026-04-17) :

| Modèle | Input | Cached | Output |
|--------|-------|--------|--------|
| `gemini-2.5-flash` | $0.30 | $0.03 | $2.50 |
| `gemini-2.5-pro` (≤200k / >200k) | $1.25 / $2.50 | $0.125 / $0.25 | $10.00 / $15.00 |
| `gemini-3-flash-preview` | $0.50 | $0.05 | $3.00 |
| `gemini-3.1-pro-preview` (≤200k / >200k) | $2.00 / $4.00 | $0.20 / $0.40 | $12.00 / $18.00 |

Les constantes vivent dans `src/tarkamcp/dashboard/usage.py` — mettre à jour si Google ajuste ses prix.

## Architecture MCP interne

Le dashboard tient lui-même une session MCP (`streamablehttp_client` + `ClientSession`) vers le `/mcp` local (`http://127.0.0.1:8420/mcp`). Les outils sont convertis manuellement en `FunctionDeclaration` et la boucle `function_call` / `function_response` est orchestrée côté serveur (AFC SDK désactivé via `AutomaticFunctionCallingConfig(disable=True)`) pour contourner des bugs connus de google-genai sur Gemini 2.5 Pro avec MCP + streaming + thinking.

> Un mode "remote" (McpServer backend-driven) existait mais a été désactivé : il produisait systématiquement des 500 INTERNAL à cause de la perte de l'header Authorization via Cloudflare Tunnel. Si `TARKAMCP_DASHBOARD_MCP_MODE=remote` est défini, le service affiche un warning au boot et chaque turn chat retourne un message d'erreur actionable.

## Robustesse aux redémarrages

Le `TokenStore` est en mémoire : après `systemctl restart tarkamcp`, les bearers sont invalidés alors que les sessions dashboard (SQLite) persistent. Le dashboard détecte cela via `TokenStore.validate()` sur chaque route sensible ; si le bearer n'existe plus côté MCP mais que la session est encore timestamp-valide, l'utilisateur est redirigé vers `/app/refresh` pour retaper son TOTP et émettre un nouveau bearer.

Conséquence pour les tokens externes (`/app/tokens`) : un restart du service force toutes les intégrations Gemini web / ChatGPT / Claude Desktop à régénérer leur token. Si ça devient gênant, migrer le `TokenStore` vers SQLite (non fait actuellement).

## Désactiver complètement

```env
TARKAMCP_DASHBOARD_ENABLED=false
```

Pour garder les tokens mais couper le chat : ne mets simplement pas `GEMINI_API_KEY`.
