# Automating the TOTP flow

> [!CAUTION]
> **Read this page end-to-end before automating anything.** Everything below describes how to derive BeaconMCP's second factor from a machine. Doing so **defeats the purpose of the second factor**: whoever holds the seed *is* the second factor. The recommended setup — the one documented in [README.md](../README.md#connecting-clients) — is to keep the TOTP seed in an authenticator app on a phone you physically control and type the 6-digit code by hand. Automation exists for a narrow set of legitimate cases (unattended services, CI pipelines, scheduled jobs on your own infra). If you are a human at a keyboard, do not automate this.

---

## When automation is *not* appropriate

Do **not** automate TOTP in any of these situations:

- You are an interactive user (desktop, CLI session, chat UI). Type the code from your phone.
- The seed would sit on a laptop, a shared workstation, or any machine where the BeaconMCP client secret already lives. That collapses two factors into one.
- The seed would be stored in a `.env` file, a dotfile, a git-tracked secrets file, a shell alias, or a password manager entry next to the client secret.
- The seed would be pasted into a chat client, an LLM prompt, a notebook, or an IDE workspace.
- The automation is a convenience shortcut ("I don't want to pick up my phone"). Pick up your phone.

If any of these apply, stop reading and go back to the [regular connection flow](../README.md#connecting-clients).

## When automation is (reluctantly) acceptable

A very small number of scenarios justify machine-held TOTP:

- **An unattended service on infrastructure you fully control** that needs to mint BeaconMCP bearers without a human present (e.g. a scheduled backup job that lists VMs before snapshotting, a monitoring daemon that calls read-only tools).
- **CI/CD pipelines** that need an integration test against a staging BeaconMCP instance. Use a *dedicated staging* seed, not the production one.
- **A hardened token-minting broker** that sits on a locked-down host, holds the seed in a KMS / HSM / Vault, and hands out short-lived bearers to other services over mTLS.

In all three cases the seed is **not** your everyday TOTP seed — it is a separate OAuth client created just for the automation, with its own TOTP, its own access audit, and a revocation plan.

## If you must automate — do it safely

> [!WARNING]
> Every rule below is a floor, not a ceiling. The seed is equivalent to a permanent bypass of the second factor. Treat it like a private key.

### 1. Create a dedicated OAuth client per automation

```bash
beaconmcp auth create --name "ci-staging"
```

- One client per job / service. Never share.
- Label it clearly so you can revoke the right one in an incident.
- Record `client_id` and owner in your inventory; do not record the secret or the seed there.

### 2. Store the seed in a real secrets backend

Acceptable:

- HashiCorp Vault with short-lived dynamic credentials (the seed itself is static, but access to it is leased).
- AWS Secrets Manager / GCP Secret Manager / Azure Key Vault with IAM scoped to the single workload that needs it.
- SOPS-encrypted file with age/gpg keys held by the automation host only.

Not acceptable:

- Plain environment variables on a shared host.
- `.env` files committed to git (even private repos).
- CI secret variables that are readable by every pipeline in the project.
- Anywhere a human can cat the value without going through an audit log.

### 3. Minimise blast radius

- **Scope the client**: if BeaconMCP grows per-client scopes, use the narrowest one. Today, assume any valid bearer can run any tool — that means the automation can reboot your cluster. Treat it as such.
- **Rate-limit at the reverse proxy**: cap `POST /oauth/token` per source IP so a leaked seed cannot be used to mint thousands of tokens.
- **IP-allowlist at the reverse proxy**: restrict `/oauth/token` to the automation host's egress IP where possible.
- **Log and alert**: every `/oauth/token` call from an automation client should be logged with source IP, user agent, and `client_id`. Alert on anything outside the expected window.

### 4. Rotate aggressively

- Rotate the TOTP seed (and ideally the client secret) on a schedule — monthly at minimum, weekly if the automation is internet-exposed.
- Rotate immediately if the automation host is rebuilt, reimaged, restored from backup, or if any operator with access leaves.
- Keep a documented rotation runbook. Seeds that can't be rotated without downtime will not be rotated.

### 5. Never mix the automation seed with a human seed

The seed you type from your phone and the seed a script reads from Vault are **different seeds on different OAuth clients**. If a human TOTP seed ever lands on a server, treat the account as compromised: revoke the client, re-enrol the authenticator app, rotate the client secret.

## Example — unattended service (illustrative)

The example below shows the *shape* of a safe automation. Before copy-pasting, confirm every item in the checklist above applies to your environment.

```python
# Runs on a locked-down service host.
# Seed lives in Vault; this process is the only thing with read access.

import os
import pyotp
import requests

VAULT = get_vault_client()                         # your infra
secret = VAULT.read("beaconmcp/ci-staging")        # {client_id, client_secret, totp_seed}

totp = pyotp.TOTP(secret["totp_seed"]).now()
resp = requests.post(
    "https://beaconmcp.internal/oauth/token",
    data={
        "grant_type": "client_credentials",
        "client_id": secret["client_id"],
        "client_secret": secret["client_secret"],
        "totp": totp,
    },
    timeout=5,
)
resp.raise_for_status()
bearer = resp.json()["access_token"]               # 24h lifetime

# Use `bearer` to call /mcp. Do not log it. Do not persist it beyond the job.
```

Things this example deliberately does **not** show, because they are your responsibility:

- How Vault authentication is bootstrapped on the host (AppRole? instance identity? workload identity federation?).
- How the host is hardened (disk encryption, SSH access policy, syslog forwarding).
- How `resp.json()` is scrubbed from any logs your HTTP client produces by default.

If you are not already confident about all three, you are not ready to automate TOTP.

## Revocation

Whenever you suspect exposure — a host reimage, an accidental `git push` of a secrets file, an ex-colleague's laptop, a weird `/oauth/token` entry in the access log:

```bash
beaconmcp auth revoke <client_id>
```

Then rotate the seed and the client secret, and audit recent tool calls in the BeaconMCP access log. Revocation is cheap. Do it first, investigate second.

---

Back to the normal flow: [Connecting clients](../README.md#connecting-clients).
