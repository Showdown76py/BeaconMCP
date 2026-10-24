# Updates

BeaconMCP publishes no PyPI package: the canonical install is a `git clone` at `/opt/beaconmcp` with
a venv and a systemd unit. So "is there an update?" means **is this checkout behind the upstream
default branch?** (or, on the `release` channel, **behind the newest `vX.Y.Z` tag?**, see
[Channels and signatures](#channels-and-signatures)).

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
| git checkout | `cd <root>` → `git pull --ff-only` (release channel: `git fetch --tags origin` → `git merge --ff-only <tag>`; the tool itself mirrors the tags into `refs/beaconmcp/tags/`, so a re-pointed upstream tag never blocks it) → `<venv>/bin/pip install -e . -c constraints.txt` (the real venv path, when there is one) → `systemctl restart beaconmcp` if a unit file exists |
| container | `docker compose pull` → `docker compose up -d` |
| pip distribution | `pip install --upgrade 'beaconmcp @ git+https://github.com/Showdown76py/BeaconMCP.git'` |
| unknown | Re-run `deploy/install.sh` from a checkout |

## Upgrading to the mcp 2.x SDK

This release moves the SDK dependency to `mcp>=2.3,<3` (the server now builds on `MCPServer`).

1. Git checkout: `beaconmcp_self_update` handles it (the pip step reinstalls with the new `constraints.txt`, the startup gate runs under mcp 2.x before the restart). By hand: `<venv>/bin/pip install -e . -c constraints.txt`, then `systemctl restart beaconmcp`.
2. Docker: `docker compose pull` then `docker compose up -d`, or rebuild the image if you build it yourself.
3. Run `beaconmcp check-startup`: your `beaconmcp.yaml` needs no edit, and this proves the server starts with it.
4. Roll back by checking out the previous commit (or tag) and reinstalling with `-c constraints.txt`, then restart.

## MCP tools

Two tools, registered on every deployment shape:

- **`beaconmcp_check_update`** — read-only. Version, commits behind, changelog, new configuration,
  and the commands for this install. Cached for a few hours.
- **`beaconmcp_self_update`** — applies it. Requires `confirm=True`.

Ask your assistant to "check whether BeaconMCP has an update" and it will read the changelog and any
new settings back to you before touching anything.

## What the self-update actually does

In order, stopping at the first failure:

1. **Preflight**: refuses on a non-git install, and refuses when the checkout has uncommitted
   changes. Local edits are never discarded.
2. **Fetch and resolve the target**: the tip of the default branch, or the newest release tag.
3. **Verify the signature** (only with `require_signed: true`): an unsigned or untrusted target is
   refused before anything changes.
4. **Fast-forward** to that exact commit (`git merge --ff-only <sha>`). No merges, no rebases, and
   not whatever the remote points at a second later.
5. **Reinstall**: `pip install -e . -c constraints.txt` in the detected venv. `constraints.txt` pins
   every dependency, direct or not, to the set CI tested, so an update never pulls a library
   release nobody has run.
6. **Check startup**: runs `beaconmcp check-startup` in a subprocess. The *new* code loads your
   *actual* configuration, imports every module, builds the HTTP app and answers `/health`, against
   scratch copies of the token store and dashboard database.
7. **Restart**: `systemctl restart`, deferred a few seconds so the response reaches you first.
   Only when the server actually runs under the unit: a `beaconmcp serve` started by hand is told
   to restart itself instead.

Step 6 is a hard gate before the restart. If the new revision cannot start with your config (a
setting was renamed, a new one is required, an import is broken) the checkout is reset to exactly
where it started, dependencies are restored, **nothing is restarted**, and the error is handed back
to you. An update that bricks the server is worse than no update.

### After the restart

Some failures only show once the real process runs (a port, a permission, a dependency that only
breaks at runtime). So the restart itself is done by a watchdog, started as its own transient unit
with `systemd-run` (a plain child would be killed with the service's cgroup). It:

1. restarts the service;
2. waits up to 90 seconds for `/health` to answer, then requires it to keep answering for 30
   seconds with the same main PID and restart count (a process that crashes and is restarted by
   systemd every few seconds can answer every single probe);
3. otherwise runs `git reset --hard <previous revision>`, reinstalls the previous pinned
   dependencies and restarts again.

The outcome (`healthy`, `rolled_back`, `rollback_unhealthy`, `rollback_failed`, or `superseded`
when another update moved the checkout in the meantime, which the watchdog then leaves alone) is
written to `.beaconmcp-update.json` at the root of the checkout, and `beaconmcp_check_update`
reports it as `last_update`. While that file still says `pending` (the watchdog is waiting), a new
update is refused; a `pending` older than the watchdog's longest run no longer blocks. If
`systemd-run` is missing or refuses the unit, the restart still happens, but unwatched, and the
status says `unwatched`.

### Channels and signatures

> **What the defaults trust.** With `channel: branch` and `require_signed: false`, an update installs
> whatever is on the upstream default branch, and the shipped unit runs the service as `root`. Anyone
> who can push to that branch (or take over the account that can) can run code on this host the next
> time someone clicks **Update now**. On a host that matters, use `release` with
> `require_signed: true`, or `allow_self_update: false` and update by hand.

```yaml
features:
  updates:
    channel: branch        # or: release
    require_signed: false  # true: refuse unsigned targets
```

- `branch` (default) follows every change merged to the default branch.
- `release` follows only `vX.Y.Z` tags (pre-release tags such as `v2.1.0-rc1` are skipped). No
  release has been tagged yet, so this channel reports "no release tag" until the first one is
  published; `branch` stays the default until then.
- `require_signed: true` makes `git verify-tag` (release) or `git verify-commit` (branch) a hard
  gate. Git checks the signature against the keys the service user trusts: import the maintainers'
  GPG keys, or for SSH signatures set `gpg.ssh.allowedSignersFile` in that user's git config.
  **Every** public key in that user's keyring is accepted, trusted or not, so keep it to the
  maintainers' keys (or point `GNUPGHOME` at a dedicated keyring in the unit). A release tag must
  also carry its own name: a signed `v2.1.0-rc1` republished as `v2.1.0` is refused.
  On the branch channel this needs a signed tip commit; merges made through GitHub's web UI are
  signed by GitHub's key, which you then have to trust. That key signs every web-UI commit of every
  GitHub user, so on the branch channel the gate only proves the commit went through github.com:
  it does not stop anyone who can push or merge to the branch.

Only one update runs at a time. The dashboard button and the MCP tool reach the same code, and two
`git pull` / `pip install` runs in one checkout would fight over `index.lock`. A second caller is
told one is already in progress rather than queued behind a pip that may take minutes.

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
the log without reading the tool output. A rollback done by the post-restart watchdog happens after
the server answered, so it lands in `.beaconmcp-update.json` and in the journal of its transient
unit (`journalctl -u 'beaconmcp-update-*'`) instead.
