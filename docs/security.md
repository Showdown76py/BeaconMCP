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

The integrated chat at `/app/chat` already forces a human confirmation for every `ssh_run` /
`proxmox_run` call that carries a `command`; polling-only calls that pass just an `exec_id` are
read-only and skip the modal. Read the arguments on the confirmation card even when you're clicking
through fast. No answer within 5 minutes counts as a refusal.

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

## Audit trail

`server.audit_log` records tool calls, dashboard logins, OAuth authorizations and client revocations
as JSON lines, in a file created owner-only (0600). It's the first place to look after something
unexpected happens. See [configuration.md](configuration.md#server).
