# Configuration

BeaconMCP reads two files at startup.

**`beaconmcp.yaml`** — topology and feature flags. Path resolution, in order: the `--config` flag,
the `BEACONMCP_CONFIG` env var, `./beaconmcp.yaml`, `/etc/beaconmcp/config.yaml`. The full annotated
schema lives in [`beaconmcp.yaml.example`](../beaconmcp.yaml.example).

**`.env`** — the secrets the YAML references as `${VAR}`. A missing reference fails the startup
check and names the offending YAML path.

`beaconmcp validate-config` resolves everything and prints the result with secrets masked, without
starting the server. Run it after every edit.

Legacy `PVE*_*`, `ILO_*` and `SSH_*` env vars still work when no YAML is found. They are deprecated
and disappear in 2.1.

## Where to run it

Put BeaconMCP on the same local network as the cluster: on one of the nodes, in a dedicated LXC or
VM, or in a container with host networking.

That way every `proxmox.nodes[].host` is a plain LAN IP (`10.0.0.1`, `10.0.0.2`) usable as-is for
the Proxmox API on `:8006` **and** for SSH on `:22`. Two features depend on that being true: the
`ssh.inherit_proxmox_nodes` shortcut, and the SSH-jump tunnel `bmc_*` uses to reach HP iLO devices
on a private management VLAN.

A public FQDN with a reverse-proxy port (`pve2.example.com:443`) pins the entry to HTTPS and breaks
SSH inheritance, since sshd listens on port 22 of the node and not behind your HTTPS tunnel. For a
genuinely remote node, declare it explicitly under `ssh.hosts[]` with its real SSH address: a
Tailscale IP, a VPN address, a bastion.

## Server

| Key | Notes |
|-----|-------|
| `server.allowed_hosts` | DNS-rebinding allowlist, enforced on **every** route (`/mcp`, `/oauth/*`, `/app/*`, `/.well-known/*`, `/metrics`), not only the MCP endpoint. **Must** include the public FQDN behind your reverse proxy, or requests come back `421 Misdirected Request`. Also list any host you scrape `/metrics` through (an IP needs a `:*` entry, e.g. `10.0.0.5:*`). `/health` is exempt so liveness probes keep working by IP. |
| `server.allowed_origins` | Web-origin allowlist, used for browser CORS preflights and for OAuth HTTPS redirect URIs. Desktop and CLI callbacks (`vscode://`, `cursor://`, loopback) are handled separately. |
| `server.trusted_proxies` | Direct peers whose forwarded headers are trusted, as IPs or CIDRs. Governs `X-Forwarded-For` (the auth rate-limit client IP), `X-Forwarded-Host` (the host advertised in the OAuth issuer, the passkey RP ID and the token/connector MCP URLs) **and** `X-Forwarded-Proto` (the scheme behind the OAuth issuer, the passkey origin and the `Secure` cookie flag). When empty, `X-Forwarded-For` and `X-Forwarded-Host` from any peer are ignored (the request's own peer / `Host` win) while `X-Forwarded-Proto` is still honoured, so a TLS edge can report `https`. Once set, `X-Forwarded-Proto` is also accepted from any non-public peer (loopback, LAN, Docker), which is where cloudflared or nginx usually sit. The value `cloudflare` auto-expands to Cloudflare's edge ranges; list the tunnel or proxy host itself too (`127.0.0.1`, the Docker subnet) so its `X-Forwarded-For` is trusted for rate limiting. A forwarded header that arrives from an unlisted peer is logged once (`... not in server.trusted_proxies`). |
| `server.tokens_db` | SQLite file persisting named API tokens (the `/app/tokens` page), OAuth access tokens and OAuth refresh tokens across restarts. OAuth tokens are stored as SHA-256 hashes. Created owner-only (0600). Defaults to `tokens.db` next to `clients_file`. Env override: `BEACONMCP_TOKENS_DB`. |
| `server.named_token_ttl` | Lifetime of named API tokens, in seconds. Default `2592000` (30 days); `0` means never expires, revoke-only. OAuth access tokens and dashboard session bearers keep their fixed 24 h TTL either way. Env override: `BEACONMCP_NAMED_TOKEN_TTL`. |
| `server.refresh_token_ttl` | Idle lifetime of OAuth refresh tokens (issued to `authorization_code` clients such as claude.ai, ChatGPT or Le Chat), in seconds. Default and maximum `86400` (24 h); `0` is rejected. Every refresh rotates the token and restarts this clock, but no sign-in outlives 24 h: every refresh token of a sign-in expires 24 h after the authorization, whatever this value says, and the client goes back through the TOTP page. A shorter value only adds an idle timeout inside that day. Env override: `BEACONMCP_REFRESH_TOKEN_TTL`. |
| `server.confirm_tools` | Tools that ask for a yes/no confirmation through MCP form elicitation. Default: `bmc_power_off`, `bmc_power_reset`, `proxmox_snapshot_rollback`, `proxmox_snapshot_delete`, `proxmox_backup_restore` (with `force` only), `beaconmcp_self_update`, `proxmox_vm_create` and `proxmox_vm_config` (only when the config carries a host-reaching key: `hookscript`, `args`, `cicustom`, `lxc.*`, `devN`, `hostpciN`, `usbN`, `parallelN`, `virtiofsN`, `serialN` other than `socket`, `features` other than `nesting`, `unprivileged` false, a disk key (`mpN`, `rootfs`, `scsiN`, `virtioN`, `sataN`, `ideN`, `efidisk0`, `tpmstate0`, `unusedN`) that points at an existing volume, a path or `import-from` (new `<storage>:<size>` volumes, `none` and ISO CD-ROMs are not asked), or `restore`/`archive`/`force`). A container created without `unprivileged` is created unprivileged (PVE 9 does this already, PVE 8 would make it privileged). Dry runs are never asked. Add `ssh_run` or `proxmox_run` to also confirm shell commands (polling with `exec_id` alone is never asked). Only clients that declare form elicitation (Claude Code does, claude.ai does not today) see the dialog; on the others the tool runs without confirming unless `server.confirm_strict` is on. |
| `server.confirm_strict` | Default `false`. When `true`, a tool that needs a confirmation is refused instead of run if the client cannot show it (no form elicitation, or no request context). Turn it on once your clients support elicitation, or accept that those clients cannot use the gated tools. The dashboard chat and panels keep working: their MCP sessions accept the dialog because the chat approval card (which also covers every tool in `confirm_tools`) or the panel click already asked the human. |
| `server.audit_log` | JSON-lines audit log covering tool calls, dashboard logins, OAuth authorize and client revokes. Created owner-only (0600). Default `/opt/beaconmcp/audit.log`; `-` keeps stderr only. Env override: `BEACONMCP_AUDIT_LOG`. |
| `server.transfers_max_mb` | Size cap for `proxmox_upload_file` / `proxmox_download_file` / `proxmox_upload_link` / `proxmox_fetch_url`. Default 500. Behind Cloudflare, an upload link request body is also capped by Cloudflare (100 MB on the free plan, 413 from Cloudflare above that); `proxmox_fetch_url` is not affected. |
| `server.oidc` | Optional SSO through an external OpenID Connect provider (Ory Hydra, Keycloak, Authentik...). Off by default. See [SSO (OpenID Connect)](#sso-openid-connect). |
| `server.auth_mode` | `builtin`, `both` or `sso`: how humans sign in. Defaults to `both` with `server.oidc` on, `builtin` otherwise. See [SSO (OpenID Connect)](#sso-openid-connect). |

## SSO (OpenID Connect)

BeaconMCP can delegate sign-in to your identity provider (Keycloak, Authentik, Ory Hydra, any
OpenID Connect provider). It stays the OAuth server that MCP clients talk to, so nothing changes on
the client side: they still do OAuth 2.1 against BeaconMCP, and BeaconMCP sends the human to your
provider for the sign-in step. The id_token is validated before anything is granted (see
[What BeaconMCP checks](#what-beaconmcp-checks)).

### Pick a sign-in mode

`server.auth_mode` decides how **humans** sign in:

| `auth_mode` | Humans sign in with | When to use it |
|-------------|---------------------|----------------|
| `builtin` | client id + client secret, then TOTP or a passkey | No identity provider. Default when `server.oidc` is off. |
| `both` | builtin, or **Continue with &lt;label&gt;** | Moving to SSO, or keeping a fallback. Default when `server.oidc` is on. |
| `sso` | only **Continue with &lt;label&gt;** | Your provider is the single source of identity. No human path accepts a client secret, a TOTP code or a passkey. |

Scripts using the `client_credentials` grant work in every mode. They have no browser, and their
clients still come from `beaconmcp auth create`.

### SSO only for your team (the usual setup)

```yaml
server:
  auth_mode: sso
  allow_dynamic_registration: true     # connectors register themselves on /mcp
  allowed_origins:                     # where an authorization code may be sent
    - https://claude.ai
    - https://chatgpt.com
  oidc:
    enabled: true
    issuer: https://sso.example.com/realms/infra   # exactly as in the provider's discovery doc
    client_id: beaconmcp
    client_secret: ${BEACONMCP_OIDC_CLIENT_SECRET}
    label: Example SSO                 # button text: "Continue with Example SSO"
    allow:
      - groups: [beaconmcp-users]      # who may sign in (identity rule, see below)
```

With that:

- **Dashboard:** `/app/login` shows one button. The first sign-in creates the person's own space
  (conversations, API tokens, connectors). Nobody handles a client id or a secret.
- **MCP clients** (claude.ai, ChatGPT, Claude Code, Cursor...): add `https://<host>/mcp` and leave
  the OAuth client id and secret empty. The client registers itself, the browser opens the
  BeaconMCP authorization page, the person clicks **Continue with &lt;label&gt;**, checks the redirect
  host and clicks **Finish signing in**. The connector belongs to that person.

Register BeaconMCP at your provider as a confidential client (authorization code, PKCE) with the
redirect URI `https://<host>/oauth/oidc/callback`; see [Ory Hydra](#ory-hydra) for an example.

### Allow rules

Access is denied by default: an account gets in only if an `allow` rule matches its id_token. A
rule matches when any of its `emails`, `groups` or `subjects` (the provider's `sub`) matches. Each
rule takes one of three forms:

```yaml
allow:
  # 1. identity rule (no `client` key): the account signs in as itself
  - groups: [beaconmcp-users]
  # 2. SSO replaces the TOTP step of one builtin client
  - client: beaconmcp_7f3a...          # from `beaconmcp auth list`
    emails: [alice@example.com]
  # 3. same, for every builtin client, present and future (quote the *)
  - client: "*"
    groups: [beaconmcp-admins]
```

1. **Identity rule.** On first sign-in BeaconMCP creates a hidden client for the account
   (`beaconmcp auth list` shows it as `sso user`). It has no secret anyone knows and no TOTP seed,
   so SSO is its only way in. Everything the person creates (dashboard data, API tokens,
   connectors) hangs off it. `auth_mode: sso` needs at least one identity rule.
2. **`client: <id>`.** Only for clients made with `beaconmcp auth create`: on `/oauth/authorize`,
   SSO stands in for that client's TOTP. A connector created from the dashboard's bootstrap URL is
   covered by its owner's rules.
3. **`client: "*"`.** Form 2 for every builtin client. There is no globbing: `beaconmcp_*`, an empty
   or a null `client` is refused at load.

Emails count only with `email_verified: true`, unless `require_email_verified` is `false`. Groups
are read from the `groups_claim` claim (default `groups`); your provider has to put it in the
id_token. A rule only accepts `client`, `emails`, `groups` and `subjects`: any other key (a
misspelt `client`, say) is refused at load, so a typo can't turn into an identity rule.

### Connectors registering themselves

With an identity rule and `server.allow_dynamic_registration: true`, BeaconMCP advertises
`registration_endpoint` in `/.well-known/oauth-authorization-server` and accepts OAuth Dynamic
Client Registration (RFC 7591) on `POST /oauth/register`. This is what claude.ai's "register
automatically" and most MCP clients expect.

- A registration grants nothing by itself: such a client has no TOTP or passkey path, only an SSO
  sign-in matched by an identity rule can authorize it.
- It belongs to the first SSO account that clicks **Finish signing in** for it. Signing in at the
  provider alone doesn't claim it, and anyone else is refused afterwards.
- Its `redirect_uris` must be on `server.allowed_origins` (loopback and `vscode://`-style
  callbacks are built in), otherwise registration fails with `invalid_redirect_uri`.
- A client nobody authorized is deleted after 24 hours, and at most 200 can be waiting at once
  (registration answers 503 above that). Registration shares the per-IP login rate limit.

Without an identity rule, `/oauth/register` answers 403 and only the dashboard's single-use
bootstrap URLs (`/mcp/c/<slug>`) can register a client.

### Dashboard with SSO

- **Sign-in:** the **Continue with &lt;label&gt;** button is a plain link to `/app/sso`, which
  sends the browser to the provider. The provider sends it back to `/oauth/oidc/callback`, and
  the new session replaces the one the browser had.
- **Expired MCP bearer** (every 24 h): an SSO user goes straight back through the provider, which
  usually signs them in again without a prompt. If it fails, `/app/login` shows why.
- **Sensitive actions** (new API token, new connector URL, self-update) need an authentication at
  the provider less than 10 minutes old, read from the id_token's `auth_time` claim. Neither a
  silent sign-in from an old provider session nor a fresh BeaconMCP session counts on its own.
  **Sign in again** sends `max_age=0`, which asks the provider to authenticate the person again
  (OpenID Connect Core 3.1.2.1), and the id_token is refused if `auth_time` is missing or too old.
  **Your provider has to send `auth_time` and honour `max_age`** (Ory Hydra and Keycloak
  do). Without them, sign-in works but these actions stay refused.
- **Passkeys** on the dashboard are off for SSO users: the provider owns MFA.
- **Switching to `auth_mode: sso`** ends dashboard sessions opened with a client secret.

### Reference

| Key | Notes |
|-----|-------|
| `server.auth_mode` | `builtin`, `both` or `sso`, see above. `builtin` with `oidc` on, or `both`/`sso` with it off, is refused at load. |
| `issuer` | Provider base URL. Its `/.well-known/openid-configuration` is fetched on the first sign-in (not at boot, so a provider outage never stops BeaconMCP) and cached for an hour. Must be `https` (plain `http` only for localhost). |
| `client_id` / `client_secret` | The confidential client registered for BeaconMCP at the provider. |
| `scopes` | Default `[openid, email, profile]`; `openid` is always added. |
| `label` | Button text after "Continue with". Default `SSO`. |
| `redirect_uri` | Defaults to `<public URL>/oauth/oidc/callback`, built from the request host (`X-Forwarded-Host` from `server.trusted_proxies`). Set it when the provider needs one fixed value. |
| `token_auth_method` | `client_secret_basic` (default, Hydra's default too) or `client_secret_post`. |
| `groups_claim` | id_token claim holding the groups. Default `groups`. |
| `require_email_verified` | Default `true`. |
| `allow` | Required, at least one rule, see [Allow rules](#allow-rules). |

### What BeaconMCP checks

- **The round trip:** `state`, PKCE (S256), a `nonce`, and a short-lived cookie that ties the
  callback to the browser that started it.
- **The id_token:** signature against the provider's JWKS (asymmetric algorithms only, key rotation
  picked up automatically), `iss`, `aud`, `azp`, expiry, `nonce`, and `auth_time` when `max_age`
  was sent.
- **The decision:** allow rules are evaluated at every sign-in.
- **The click:** an authorization always ends on the approval screen, which shows the redirect host
  in large type. Nothing is issued before **Finish signing in**.

Audit events: `auth.authorize.2fa` with `via=oidc` and `rule=<client id>`, `rule=*` or
`rule=user` (an SSO user's own client); `dashboard.login.ok` with `via=oidc`; `auth.register` with
`via=dcr`; failures as `auth.authorize.fail` / `dashboard.login.fail` with an `oidc:` reason.
Trade-offs and limits are in [security.md](security.md#sso-openid-connect).

### Ory Hydra

Register BeaconMCP as a client on Hydra's admin API:

```bash
hydra create oauth2-client \
  --endpoint http://hydra-admin:4445 \
  --name BeaconMCP \
  --grant-type authorization_code \
  --response-type code \
  --scope openid --scope email --scope profile \
  --redirect-uri https://mcp.example.com/oauth/oidc/callback \
  --token-endpoint-auth-method client_secret_basic
```

Put the printed client id and secret in `server.oidc`, and set `issuer` to Hydra's public URL
(`urls.self.issuer` in Hydra's config). The id_token only carries what your login and consent app
puts in `session.id_token` when it accepts the consent request: `email` and `email_verified` for
email rules, a `groups` claim for group rules. `sub` is the subject your login app accepted, so
`subjects` rules work without anything extra. Hydra puts `auth_time` in the id_token and honours
`max_age`: when the last authentication is too old, the login request arrives with `skip: false`,
so your login app has to authenticate the user again instead of reusing its session.

## Proxmox

| Key | Notes |
|-----|-------|
| `proxmox.verify_ssl` | TLS verification of the Proxmox API. `false` (default) skips it, `true` uses the system CA store, and a file path verifies against that CA bundle. For the default self-signed cluster certificate, copy `/etc/pve/pve-root-ca.pem` from any node to the BeaconMCP host and point `verify_ssl` at the copy. Any other value is refused at startup, so a mistyped path can't silently turn verification off. |
| `proxmox.nodes[]` | One entry per node, each with its own API token. Prefer a LAN IP in `host:` — it's the one string that works for both the API and SSH inheritance. `localhost` is fine when BeaconMCP runs on that node. Reserve FQDN-with-port for nodes you cannot reach on the LAN, and give those an explicit `ssh.hosts[]` entry. |

## SSH

| Key | Notes |
|-----|-------|
| `ssh.hosts[]` | One entry per SSH target: VPS, Proxmox node, jump box. Each carries its own `user` plus exactly one of `password` / `key_file`. Names may match `proxmox.nodes[].name`. Per-host `known_hosts` and `strict_host_key_checking` override the global `ssh.*` settings (a `known_hosts` file, once set at either level, always applies). |
| `ssh.known_hosts` / `ssh.strict_host_key_checking` | Host-key verification. **Unset (default): trust on first use**, like OpenSSH `accept-new`: the first connection to a host records its key in `ssh_known_hosts` next to `clients_file`, and a different key later is refused. If a host is reinstalled, or a VMID's address is reused by a new VM, the old pin refuses it until you remove its line with `ssh-keygen -R <host> -f <that file>`. `known_hosts: <file>` only accepts the keys in that file; `strict_host_key_checking: true` only accepts `~/.ssh/known_hosts`; `strict_host_key_checking: false` accepts any key (lab networks only). The iLO jump-host tunnel follows the same rules. |
| `ssh.defaults` + `ssh.inherit_proxmox_nodes` | Homelab shortcut: set `defaults:` (user + password/key_file), flip `inherit_proxmox_nodes: true`, and every Proxmox node becomes SSH-reachable under its own name without duplication. An explicit `ssh.hosts[]` entry still wins when it matches a node by name or address. |
| `ssh.vmid_to_ip` | Optional template (`"192.168.1.{id}"`) used by `ssh_run` when `host` is a bare VMID. The resolved IP must match an `ssh.hosts[].host` to authenticate. Omit it to disable numeric shortcuts entirely. |

## BMC

| Key | Notes |
|-----|-------|
| `bmc.devices[]` | Zero or more BMCs. `type` is `hp_ilo`, `ipmi`, `idrac`, `supermicro` or `redfish`. iDRAC (14G+) and Supermicro (X11+) are served by the Redfish backend. `jump_host` is optional: set it to a `proxmox.nodes[]` name to route the connection over an SSH tunnel, which is how you reach a management VLAN. |

## Dashboard

| Key | Notes |
|-----|-------|
| `features.dashboard.limits` | Per-5h and per-week USD caps on the Gemini chat. `0` disables that window. |

Everything else about the panel — enabling it, the tokens page, cost tracking, the confirmation
modal — is in [dashboard.md](dashboard.md).

## Updates

| Key | Notes |
|-----|-------|
| `features.updates.enabled` | Default `true`. Compares this checkout against the upstream default branch and shows a notice to signed-in operators. Set to `false` on an air-gapped or change-controlled box: the server then never contacts the git remote, and the `beaconmcp_*_update` MCP tools are not registered. |
| `features.updates.allow_self_update` | Default `true`. Set to `false` to keep the notification but forbid applying it from the dashboard or over MCP — the right setting when deploys go through a pipeline. Manual instructions are still shown. |
| `features.updates.channel` | `branch` (default) follows the tip of the upstream default branch. `release` follows the newest `vX.Y.Z` tag only, and reports "no release tag" until one is published. |
| `features.updates.require_signed` | Default `false`. When `true`, an update whose target is not signed by a key this host trusts is refused: `git verify-tag` on the release tag, `git verify-commit` on the branch tip. Trust comes from the service user's git setup (GPG keyring where the key is valid, lsigned by a local ultimately trusted key or given ultimate trust, since a key merely present in the keyring is refused, see [updates.md](updates.md); or `gpg.format=ssh` with `gpg.ssh.allowedSignersFile`). |

See [updates.md](updates.md) for the notice, the MCP tools, and what the self-update does.
