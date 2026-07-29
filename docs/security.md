# Security

> Never let a model execute shell commands on infrastructure you care about without reading the
> command first.

BeaconMCP exposes tools that cause irreversible changes: `ssh_run`, `proxmox_run`, `bmc_power_off`,
`proxmox_vm_stop`, `proxmox_vm_create`, `vm_bulk_action`, and more. Models do not reliably grasp
consequences — an errant `rm -rf`, a `systemctl stop` on the wrong unit, a `pct destroy` where `pct
stop` was meant.

## Reviewing tool calls

- **Disable auto-approve** on every external MCP client (Assistant Desktop, Gemini CLI, ChatGPT
  MCP). Keep per-call approval on and refuse "always allow this tool".
- **Read the `command` argument** before approving any `ssh_run` or `proxmox_run`. The question to
  ask: if this ran against the wrong VM or host, could I recover?
- **Prefer read-only tools** for exploration (`cluster_overview`, `cluster_health`, `*_list_*`,
  `*_status`, `proxmox_get_logs`). They cannot break anything and are never gated behind a
  confirmation.

The integrated chat at `/app/chat` forces a human confirmation for every code-execution tool
(`ssh_run`, `proxmox_run`, `proxmox_write_file` and the transfer tools) and every destructive one
(`vm_bulk_action`, `proxmox_vm_stop`, snapshot rollback/delete, backup restore, `bmc_power_off`,
`bmc_power_reset`). Writing a guest file counts as code execution: `~/.ssh/authorized_keys` and
`/etc/cron.d/` are one hop from a shell. Skipping the modal is reserved for calls that cannot
change anything — polling by `exec_id` alone, `dry_run=True` on the snapshot tools that implement
it, and the read shape of `proxmox_vm_config`. The full list is in
[dashboard.md](dashboard.md#mandatory-confirmation-for-dangerous-tools). Read the arguments on the
confirmation card even when you're clicking through fast. No answer within 5 minutes counts as a
refusal.

## Tokens

A `/app/tokens` bearer grants arbitrary shell access on your Proxmox nodes for its full lifetime
(`server.named_token_ttl`, 30 days by default). Don't hand one to a client you don't fully control,
and revoke it from `/app/tokens` the moment it leaks.

`systemctl restart beaconmcp` invalidates dashboard sessions and other internal bearers, but **named
API tokens survive restarts** — they live in `server.tokens_db`. Revoke them individually from
`/app/tokens`, or delete `tokens.db` before restarting to kill all of them at once.

`security_end_session` lets a client revoke its own bearer at the end of a task, which is a cheap way
to shrink the replay window.

## TOTP

Keep the TOTP seed in an authenticator app on a device you physically control: Google Authenticator,
Authy, 1Password, Aegis, a YubiKey with OTP. Type the 6-digit code by hand into the authorization
page or the dashboard.

Do **not** generate codes programmatically with `oathtool` / `pyotp` / a shell alias, and do not
store the raw seed in a `.env`, in a secrets manager, or anywhere near the client secret. Any of
those collapses two factors into one and removes the entire point of the second one.

Unattended services (scheduled jobs, CI pipelines) sometimes genuinely need machine-held TOTP. That
case, with its required precautions, is covered separately in
[totp-automation.md](totp-automation.md). Read it end to end before deciding.

## Passkeys

A passkey (WebAuthn) replaces the **TOTP factor**, never the client secret. Both login pages keep the
same two-factor shape:

1. `client_id` + `client_secret`
2. a 6-digit code **or** a passkey assertion

That ordering is what makes a stolen passkey worthless on its own, and it is also a practical
constraint: the dashboard session encrypts the client secret so it can re-mint MCP bearers later, so
a fully usernameless login could not build a working session anyway.

Where they work:

| Page | Sign in with a passkey | Enrol a passkey |
|------|------------------------|-----------------|
| `/app/login` | Link under the 2FA step | On the post-2FA screen |
| `/oauth/authorize` | Link under the 2FA step | On the approval screen |

Credentials are stored in the dashboard database (`passkeys` table): a credential id, a **public**
key and a signature counter. Nothing secret leaves the authenticator.

Things worth knowing before you enrol:

- **Passkeys are bound to the hostname.** The relying-party ID is derived from the request host, so
  a credential registered on `beacon.example` will not work on `beacon.internal` or on a raw IP that
  differs from the one used at registration. Settle on your public hostname first.
- **A secure context is required.** Browsers only expose the WebAuthn API over HTTPS or on loopback.
  On a plain-HTTP LAN deployment the passkey buttons are hidden and TOTP stays the only path in.
- **Keep TOTP working.** Passkeys are an alternative, not a replacement: losing every enrolled device
  must not lock you out. The authenticator seed remains the recovery path.
- **Dynamically-registered clients delegate**, exactly like TOTP: a client created through the DCR
  bootstrap is authorized by its *owner's* passkeys, so the second factor never leaves the owner.
- Registrations, revocations and passkey sign-ins are recorded in the audit log
  (`dashboard.passkey.*`, `auth.passkey.*`, and `login`/`authorize` events tagged `via=passkey`).

Passkey ceremonies are rate-limited per IP by the same limiter that guards `/app/login`, and a TOTP
lockout also blocks the passkey path for that client.

Manage enrolled credentials from `/app/tokens`, or drop them all for a client with:

```sql
DELETE FROM passkeys WHERE client_id = 'beaconmcp_...';
```

## Audit trail

`server.audit_log` records tool calls, dashboard logins, OAuth authorizations and client revocations
as JSON lines, in a file created owner-only (0600). It's the first place to look after something
unexpected happens. See [configuration.md](configuration.md#server).
