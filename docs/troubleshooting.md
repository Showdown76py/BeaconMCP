# Dépannage

## Erreurs courantes

| Erreur | Cause | Solution |
|--------|-------|----------|
| `PVE1_HOST ... required` | `.env` non chargé | Vérifier `/opt/tarkamcp/.env` |
| `Node 'pveX' is unreachable` | API Proxmox down | `curl -sk https://pve1:8006/api2/json/version` |
| `QEMU Guest Agent may not be running` | Agent non installé | Voir [README#installer-le-qemu-guest-agent](../README.md#installer-le-qemu-guest-agent) |
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
| Chat bloqué sur une card SSH / exec avec deux boutons | Confirmation obligatoire pour `ssh_exec_command*` et `proxmox_exec_command*` | Cliquer **Autoriser** ou **Refuser**. Timeout à 5 min sinon auto-reject |
| Tokens API : "Limite atteinte : maximum 3 tokens" | 3 tokens nommés déjà actifs pour ce client | Révoquer un token existant dans la liste avant d'en créer un nouveau |

## Exemples d'utilisation

> **"pve2 ne répond plus, qu'est-ce qui se passe ?"**
>
> L'IA va : `proxmox_list_nodes` → voit pve2 offline → `ilo_power_status` → vérifie si allumé → `ilo_health_status` → checker le hardware → proposer un diagnostic

> **"Mets à jour les paquets sur tous les conteneurs"**
>
> L'API Proxmox n'expose pas d'endpoint `exec` pour les LXC. L'IA utilise
> donc `proxmox_list_vms` → liste les CTs → `ssh_exec_command_async` sur le
> nœud hôte avec `pct exec <vmid> -- sh -c 'apt update && apt upgrade -y'`
> pour chacun → poll les résultats. Chaque appel `ssh_exec_command*` et
> `proxmox_exec_command*` **exige une approbation manuelle** dans le chat
> intégré ; les clients MCP externes (Claude, ChatGPT, Gemini) doivent faire
> pareil si l'auto-approve n'est pas désactivé.
