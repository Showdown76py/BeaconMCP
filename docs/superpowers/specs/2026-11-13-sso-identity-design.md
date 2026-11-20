# SSO as an identity (not only a second factor)

## Why

BeaconMCP is open source. An operator should be able to pick how humans sign in:

- **builtin**: client id + client secret + TOTP / passkey (today),
- **both**: builtin plus SSO,
- **sso**: SSO only. Users never see a client id or a secret; their IdP account has the same power a
  builtin client has today, like "Sign in with Google" replaces an email + password.

Before this, SSO (861748a, #72) only replaced the TOTP step for a client someone had already
provisioned. The dashboard still needed a client secret, and claude.ai could only self-register
through a dashboard-minted slug.

## Goals

- `server.auth_mode: builtin | both | sso`.
- An SSO user signs in to the dashboard with the IdP alone and gets their own conversations,
  tokens and connectors.
- A connector (claude.ai, ChatGPT, Claude Code...) pointed at the plain `/mcp` URL can register
  itself (open DCR) and is authorized through SSO only. It belongs to the SSO user who authorized it.
- In `sso` mode no human path accepts a client secret or a TOTP code.

## Non-goals

- Roles / per-user permissions. An allowed SSO user has the full scope, like a builtin client.
- SSO for machines. `client_credentials` keeps working in every mode (scripts have no browser);
  in `sso` mode it only works for clients an admin created with the CLI.
- Back-channel logout / IdP session tracking. Access ends with the BeaconMCP session lifetime
  (24 h for OAuth sign-ins, dashboard session TTL), and allow rules are re-checked at every sign-in.

## Anti-goals

- An SSO account authorizing someone else's builtin client or connector.
- A registration endpoint that grants anything on its own.

## Approaches

- **A. Hidden client per SSO identity (picked).** On first SSO sign-in BeaconMCP provisions an
  internal client bound to `(issuer, sub)`: random secret nobody knows, no TOTP seed. Everything
  is already keyed by `client_id` (tokens, dashboard sessions, conversations, connectors, audit,
  DCR ownership), so per-user isolation comes for free.
- B. A real `users` table and a generic principal id everywhere. Cleaner, but touches most of the
  stack and needs DB migrations for the same behaviour.
- C. Reverse proxy (oauth2-proxy) in front of the dashboard. Doesn't cover the OAuth AS role.

## Design

### Config

- `server.auth_mode`: default `both` when `server.oidc.enabled`, else `builtin`. `both` / `sso`
  require oidc on; `builtin` with oidc on is refused (ambiguous).
- `server.oidc.allow` rules:
  - no `client` key: **identity rule**, the account may sign in as itself (dashboard, own
    connectors, open DCR clients). New.
  - `client: <id>`: SSO stands in for that builtin client's 2FA (unchanged).
  - `client: "*"`: same for any builtin client (#72).
- `sso` mode needs at least one identity rule.

### Client kinds (in `clients.json`)

| kind | how it's made | how it's authorized |
|------|---------------|---------------------|
| builtin | `beaconmcp auth create` | TOTP / passkey, or SSO via a client rule |
| slug DCR | `/oauth/register/c/<slug>` | like its builtin owner |
| SSO user | first SSO sign-in (`sso_subject` set) | SSO identity rule, same `sub` |
| open DCR | `POST /oauth/register` (`registration_source: dcr`) | SSO identity rule; bound to the first SSO user who clicks finish, refused for anyone else after that |

Unbound open-DCR rows older than 24 h are pruned at registration time. The loader keeps SSO-user
and open-DCR rows although they have no TOTP seed. Their TOTP check always fails (empty seed).

### Open DCR

`POST /oauth/register` and `registration_endpoint` in the main AS metadata are on when
`allow_dynamic_registration` is true, `auth_mode` isn't `builtin` and an identity rule exists.
Same validation as the slug endpoint (`redirect_uris` on the trusted-origin allowlist), rate
limited per IP by the login limiter.

### Authorize page

- SSO button: builtin / slug clients need a covering client rule (as today); SSO-user-owned and
  open-DCR clients need an identity rule.
- TOTP and passkeys are hidden and refused in `sso` mode, and for clients that can't use them
  (SSO-owned, open DCR).
- Audit `auth.authorize.2fa via=oidc rule=<client id>|*|user`.

### Dashboard

- `/app/login` shows "Continue with <label>" when SSO identity is available, and hides the secret
  form in `sso` mode (POST refused too, passkey login too).
- The IdP callback stays `/oauth/oidc/callback`; the pending login carries `flow=dashboard` and
  the callback hands the claims to the dashboard, which ensures the SSO user client and starts a
  session (no secret stored).
- Expired bearer for an SSO user: back through SSO instead of the TOTP refresh page.
- Step-up actions (new API token, new connector URL, self-update) ask SSO users for an IdP
  authentication less than 10 min old (id_token `auth_time` only, no fallback); "sign in again"
  sends `max_age=0` and the id_token is refused without a fresh `auth_time` (OIDC Core 3.1.2.1).
- The dashboard entry is `/app/sso` (under the session cookie's path) so the new session can
  replace the old one. A stale bearer renews through the IdP without `max_age`.
- With SSO on, the session cookie is `SameSite=Lax` (the IdP return is a cross-site navigation);
  CSRF tokens keep guarding state changes. The SSO button is a plain GET link.
- Dashboard passkeys are off for SSO users (the IdP owns MFA).
- Audit `dashboard.login.ok via=oidc sub=...`.

## Left to the builder

Template wording, exact pruning cadence, CLI listing layout.
