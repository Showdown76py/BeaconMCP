# Updates

BeaconMCP publishes no releases and no PyPI package: the canonical install is a `git clone` at
`/opt/beaconmcp` with a venv and a systemd unit. So "is there an update?" means **is this checkout
behind the upstream default branch?**

The server answers that question itself, tells signed-in operators, and can apply the update.

## The notice

Signed in to the dashboard, a card appears bottom-right on any `/app/*` page when the checkout is
behind:

- how far behind, and the revision range (`9f496cb → abc1234`);
- the last few commit subjects, and a link to the full diff on GitHub;
- **any configuration the new revision knows about that you have not set** — new `.env` variables,
  new `beaconmcp.yaml` settings;
- the exact commands to update *this* install;
- an **Update now** button, when an automatic update is possible.

Dismissing it hides that specific revision; the card returns when a newer one lands.

On the **"You're signed in"** screen — the moment the session is created, one click before the panel
— you get a one-line mention instead of the full card. That screen has a single primary action, and
on a narrow viewport a bottom-anchored card this tall would sit right on top of it. The card itself
opts out of the auth pages entirely and shows on the landing page.

The endpoint behind it (`GET /app/api/update`) requires a live session and returns `401` otherwise.
That is deliberate: the exact revision a server runs is free reconnaissance for anyone who has not
authenticated, and the card is only ever rendered to someone signed in.

## Instructions match your install

Detection is not a guess about how you *should* have installed it:

| Detected | What you are told |
|----------|-------------------|
| git checkout | `cd <root>` → `git pull --ff-only` → `<venv>/bin/pip install -e .` (the real venv path, when there is one) → `systemctl restart beaconmcp` if a unit file exists |
| container | `docker compose pull` → `docker compose up -d` |
| pip distribution | `pip install --upgrade 'beaconmcp @ git+https://github.com/Showdown76py/BeaconMCP.git'` |
| unknown | Re-run `deploy/install.sh` from a checkout |

## MCP tools

Two tools, registered on every deployment shape:

- **`beaconmcp_check_update`** — read-only. Version, commits behind, changelog, new configuration,
  and the commands for this install. Cached for a few hours.
- **`beaconmcp_self_update`** — applies it. Requires `confirm=True`.

Ask your assistant to "check whether BeaconMCP has an update" and it will read the changelog and any
new settings back to you before touching anything.

## What the self-update actually does

In order, stopping at the first failure:

1. **Preflight** — refuses on a non-git install, and refuses when the checkout has uncommitted
   changes. Local edits are never discarded.
2. **`git pull --ff-only`** — a fast-forward or nothing. No merges, no rebases.
3. **Reinstall** — `pip install -e .` in the detected venv, so new or bumped dependencies land.
4. **Validate the config** — runs `beaconmcp validate-config` in a subprocess, so the *new* code
   parses your *actual* configuration.
5. **Restart** — `systemctl restart`, deferred a few seconds so the response reaches you first.

Step 4 is a hard gate, and it is the reason this is safe to run unattended. If the new revision
cannot load your config — a setting was renamed, a new one is now required — the checkout is reset
to exactly where it started, dependencies are restored, **nothing is restarted**, and the error from
the validator is handed back to you. An update that bricks the server is worse than no update.

### From the dashboard

The **Update now** button asks for a fresh 2FA code before it runs. Pulling code and restarting the
process is the most privileged thing the panel can do, so a session alone is not enough — same bar
as minting an API token.

## Turning it off

```yaml
features:
  updates:
    enabled: true            # false: never contact the remote, no tools, no notice
    allow_self_update: true  # false: keep the notice, forbid applying it
```

`enabled: false` is the air-gap switch. `allow_self_update: false` is for deployments where updates
go through a pipeline: operators still see that one is available, with instructions, but neither the
dashboard button nor the MCP tool exists.

## Stale assets after an update

A server that can update itself must not leave browsers running the previous release's JavaScript.
Starlette serves static files with `ETag`/`Last-Modified` but no `Cache-Control`, which puts
browsers on *heuristic* freshness: a file untouched for weeks is reused for a long time without ever
revalidating.

Asset URLs therefore carry a fingerprint of the bundle (`app.css?v=6a69fedf`), recomputed at each
start from the newest mtime in the static directory — which a `git pull` bumps. New bytes mean a new
URL, so no cache can satisfy the request from an old entry:

| Response | `Cache-Control` |
|----------|-----------------|
| Asset with `?v=` | `public, max-age=31536000, immutable` |
| Asset without | `no-cache` (revalidate every time — a legacy or hand-typed URL can never pin stale code) |
| Any `/app/*` page or API reply | `no-store` (per-session, and it carries the fingerprint) |

## Audit trail

Every attempt is logged (see [security.md](security.md#audit-trail)):

| Event | When |
|-------|------|
| `dashboard.update.start` / `dashboard.update.finish` | Update applied from the panel |
| `maintenance.self_update.start` / `maintenance.self_update.finish` | Update applied over MCP |

The `finish` events carry `ok`, `from_ref`, `to_ref` and `rolled_back`, so a rollback is visible in
the log without reading the tool output.
