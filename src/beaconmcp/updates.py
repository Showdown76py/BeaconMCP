"""Update detection and self-update for BeaconMCP.

BeaconMCP ships no PyPI package: the canonical install is a ``git clone``
at ``/opt/beaconmcp`` with a venv and a systemd unit (see
``deploy/install.sh``). So "is there an update?" means *is this checkout
behind its update target?*: the remote default branch (``channel: branch``,
the default) or the newest ``vX.Y.Z`` release tag (``channel: release``).

Three things live here:

* :func:`detect_installation` -- how this server was installed, so the
  advice we give matches reality instead of assuming everyone ran the
  install script.
* :func:`check_for_update` -- a cached, fail-soft, read-only check. It also
  diffs the *new* ``.env.example`` / ``beaconmcp.yaml.example`` against the
  operator's actual files, which is how we can say "this update wants a
  variable you haven't set" before they apply it.
* :func:`apply_update` -- optionally verify the target's signature,
  fast-forward, reinstall the pinned dependencies, **check that the new
  code starts** (config, imports, the whole HTTP app), and roll back if it
  does not. After the restart a detached watchdog polls ``/health`` and
  rolls back to the previous revision if the new process never answers.
  BeaconMCP is the tool used to reach the box when something is wrong, so
  an update that takes it down must undo itself.

Nothing here ever raises into a caller: an air-gapped box, a missing git
binary or a detached HEAD all degrade to "couldn't check", never to a
broken dashboard or a failed tool call.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import UpdatesConfig

_REPO_URL = "https://github.com/Showdown76py/BeaconMCP"

#: Versioned pin file installed with ``-c`` (see constraints.txt).
CONSTRAINTS_FILE = "constraints.txt"
#: Written next to the checkout by the post-restart watchdog: how the last
#: self-update ended (healthy, rolled back, ...). Untracked, git-ignored.
STATUS_FILE = ".beaconmcp-update.json"
#: How long the watchdog waits for the restarted server to answer /health
#: before rolling back.
HEALTH_WAIT_SECONDS = 90
#: Release tags the release channel follows. Pre-releases (v2.1.0-rc1) are
#: skipped on purpose.
_RELEASE_TAG = re.compile(r"^v\d+(?:\.\d+)*$")
#: Where the release channel keeps its copy of the upstream tags.
_TAG_NAMESPACE = "refs/beaconmcp/tags/"

#: How long a successful check stays fresh. Updates are not urgent and the
#: check shells out to git, so once every few hours is plenty.
CHECK_TTL_SECONDS = 6 * 3600
#: Failures are retried sooner -- a transient DNS blip shouldn't mean six
#: hours of "unknown".
FAILED_CHECK_TTL_SECONDS = 15 * 60

#: Ceiling on any git/pip subprocess. `pip install -e .` on a cold cache is
#: the slow one; the rest finish in well under a second.
_GIT_TIMEOUT = 60
_PIP_TIMEOUT = 900
#: ``check-startup`` imports the whole server and builds the HTTP app.
_STARTUP_CHECK_TIMEOUT = 180
#: After the first good /health, the new process must keep answering, with
#: the same PID and restart count, for this long before the update counts as
#: healthy. The unit restarts a crashed process after 5 s, so two bare
#: samples a few seconds apart can both land on different incarnations.
_SETTLE_SECONDS = 30
#: Longest a post-restart watchdog can still act: two health waits (with
#: their settle re-check), a pip reinstall and some slack. While the last
#: status is a ``pending`` younger than this, a new update is refused, so a
#: watchdog that is still waiting cannot roll back a newer update.
_WATCHDOG_WINDOW_SECONDS = 2 * (HEALTH_WAIT_SECONDS + _SETTLE_SECONDS + 15) + _PIP_TIMEOUT + 60


@dataclass
class _Runtime:
    """What the running server tells this module at startup (see :func:`configure`)."""

    settings: UpdatesConfig = field(default_factory=UpdatesConfig)
    #: Where the post-restart watchdog polls the new process.
    health_url: str | None = None


_runtime = _Runtime()


def configure(settings: UpdatesConfig | None = None, *, health_url: str | None = None) -> None:
    """Record the update settings and the server's own /health URL.

    Called once by ``beaconmcp serve``; the dashboard and the MCP tools then
    share them without threading them through every call.
    """
    _runtime.settings = settings or UpdatesConfig()
    _runtime.health_url = health_url


def current_version() -> str:
    """Installed version string, preferring package metadata."""
    try:
        from importlib.metadata import version

        return version("beaconmcp")
    except Exception:  # noqa: BLE001 - not installed as a distribution
        from . import __version__

        return __version__


# ---------------------------------------------------------------------------
# Installation shape
# ---------------------------------------------------------------------------

@dataclass
class Installation:
    """How this particular server was installed."""

    #: "git" (clone, the documented install), "pip" (installed as a
    #: distribution from a URL/wheel), "docker", or "unknown".
    kind: str
    #: Root of the git checkout, when there is one.
    root: Path | None
    #: Interpreter running us -- also the venv's python when there is a venv.
    python: str
    #: Virtualenv prefix, or None when running against a system interpreter.
    venv: Path | None
    #: True when the package is imported straight from the checkout.
    editable: bool
    #: True when the process was started by systemd.
    under_systemd: bool
    #: systemd unit to restart, when we can name one.
    service: str | None
    #: True when running inside a container.
    in_container: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "root": str(self.root) if self.root else None,
            "python": self.python,
            "venv": str(self.venv) if self.venv else None,
            "editable": self.editable,
            "under_systemd": self.under_systemd,
            "service": self.service,
            "in_container": self.in_container,
        }


def _package_root() -> Path:
    """Directory holding ``src/beaconmcp`` -- i.e. the repo root when cloned."""
    # .../<root>/src/beaconmcp/updates.py -> parents[2] == <root>
    return Path(__file__).resolve().parents[2]


def _detect_service() -> str | None:
    """Name the systemd unit, if one is installed for us."""
    for candidate in (
        "/etc/systemd/system/beaconmcp.service",
        "/lib/systemd/system/beaconmcp.service",
        "/usr/lib/systemd/system/beaconmcp.service",
    ):
        if Path(candidate).is_file():
            return "beaconmcp"
    return None


def detect_installation() -> Installation:
    """Inspect the runtime to work out how BeaconMCP got here."""
    root = _package_root()
    is_git = (root / ".git").exists()
    venv = Path(sys.prefix) if sys.prefix != sys.base_prefix else None
    # systemd exports INVOCATION_ID to every unit it starts; it is the one
    # signal that does not require guessing at pid 1 or parsing /proc.
    under_systemd = bool(os.environ.get("INVOCATION_ID"))
    in_container = (
        Path("/.dockerenv").exists()
        or os.environ.get("container") is not None
    )

    if is_git:
        kind = "git"
    elif in_container:
        kind = "docker"
    else:
        try:
            from importlib.metadata import distribution

            distribution("beaconmcp")
            kind = "pip"
        except Exception:  # noqa: BLE001
            kind = "unknown"

    return Installation(
        kind=kind,
        root=root if is_git else None,
        python=sys.executable,
        venv=venv,
        editable=is_git,
        under_systemd=under_systemd,
        service=_detect_service(),
        in_container=in_container,
    )


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run a git command in ``root``. Never raises."""
    if not shutil.which("git"):
        return 127, "", "git is not installed"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            # Never let git try to prompt for credentials: on a private
            # remote it would hang until the timeout instead of failing.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""},
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out"
    except OSError as exc:
        return 1, "", str(exc)


def _default_branch(root: Path) -> str:
    """Remote default branch name, falling back to ``main``."""
    code, out, _ = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and out.startswith("origin/"):
        return out.split("/", 1)[1]
    # Not every clone has origin/HEAD set (shallow clones, older git).
    code, out, _ = _git(root, "remote", "show", "origin")
    if code == 0:
        match = re.search(r"HEAD branch:\s*(\S+)", out)
        if match:
            return match.group(1)
    return "main"


def working_tree_dirty(root: Path) -> bool:
    """True when tracked files have uncommitted modifications."""
    code, out, _ = _git(root, "status", "--porcelain", "--untracked-files=no")
    return code == 0 and bool(out)


# ---------------------------------------------------------------------------
# Config drift: what the new revision wants that the operator hasn't set
# ---------------------------------------------------------------------------

_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")


def _env_names(text: str) -> list[str]:
    """Variable names assigned in a dotenv-style file (comments included).

    Comments count on purpose: ``.env.example`` documents optional settings
    as ``# GEMINI_API_KEY=`` and those are exactly the ones an operator
    wants to hear about after an update.
    """
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.lstrip()
        if line.startswith("#"):
            line = line.lstrip("#").lstrip()
        match = _ENV_ASSIGNMENT.match(line)
        if match:
            names.append(match.group(1))
    return names


def _yaml_paths(text: str) -> set[str]:
    """Dotted key paths in a YAML document, list items collapsed away."""
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - malformed example, nothing to diff
        return set()

    paths: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                paths.add(path)
                walk(value, path)
        elif isinstance(node, list):
            # Sequence entries are instances (nodes, hosts, devices), not
            # settings -- their *shape* is what matters, so recurse without
            # adding an index to the path.
            for item in node:
                walk(item, prefix)

    walk(data, "")
    return paths


@dataclass
class ConfigDrift:
    """Settings the incoming revision knows about and this install does not."""

    new_env_vars: list[str] = field(default_factory=list)
    new_config_keys: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.new_env_vars and not self.new_config_keys

    def to_json(self) -> dict[str, Any]:
        return {
            "new_env_vars": self.new_env_vars,
            "new_config_keys": self.new_config_keys,
        }


def _read_local(root: Path, *names: str) -> str | None:
    for name in names:
        path = root / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


def config_drift(root: Path, ref: str, config_path: Path | None = None) -> ConfigDrift:
    """Diff the example files at ``ref`` against what this install actually has.

    Deliberately compares against the operator's *real* files rather than
    the local examples: someone who set ``GEMINI_API_KEY`` before it was
    documented should not be told to set it again.
    """
    drift = ConfigDrift()

    code, new_env, _ = _git(root, "show", f"{ref}:.env.example")
    if code == 0:
        local_env = _read_local(root, ".env") or ""
        known = set(_env_names(local_env)) | set(os.environ)
        for name in _env_names(new_env):
            if name not in known and name not in drift.new_env_vars:
                drift.new_env_vars.append(name)

    code, new_yaml, _ = _git(root, "show", f"{ref}:beaconmcp.yaml.example")
    if code == 0:
        local_yaml = None
        if config_path and config_path.is_file():
            try:
                local_yaml = config_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                local_yaml = None
        if local_yaml is None:
            local_yaml = _read_local(root, "beaconmcp.yaml") or ""
        have = _yaml_paths(local_yaml)
        # Anything the operator already configured, plus its ancestors, is
        # "known"; only genuinely new leaves are worth reporting.
        for path in sorted(_yaml_paths(new_yaml) - have):
            if any(p.startswith(path + ".") for p in have):
                continue  # a parent of something already configured
            drift.new_config_keys.append(path)

    return drift


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    """Result of one update check. Always renderable, even on failure."""

    checked_at: float
    available: bool = False
    error: str | None = None
    version: str = ""
    install_kind: str = "unknown"
    branch: str | None = None
    current_ref: str | None = None
    latest_ref: str | None = None
    behind: int = 0
    commits: list[dict[str, str]] = field(default_factory=list)
    drift: ConfigDrift = field(default_factory=ConfigDrift)
    instructions: list[str] = field(default_factory=list)
    can_self_update: bool = False
    blockers: list[str] = field(default_factory=list)
    repo_url: str = _REPO_URL
    #: "branch" or "release" (features.updates.channel).
    channel: str = "branch"
    #: Release tag being offered, on the release channel.
    release_tag: str | None = None
    #: True/False once the target's signature was checked, None when not
    #: required.
    signature_ok: bool | None = None
    #: How the previous self-update ended, from the watchdog's status file.
    last_update: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "channel": self.channel,
            "release_tag": self.release_tag,
            "signature_ok": self.signature_ok,
            "last_update": self.last_update,
            "available": self.available,
            "error": self.error,
            "version": self.version,
            "install_kind": self.install_kind,
            "branch": self.branch,
            "current_ref": self.current_ref,
            "latest_ref": self.latest_ref,
            "behind": self.behind,
            "commits": self.commits,
            "config": self.drift.to_json(),
            "instructions": self.instructions,
            "can_self_update": self.can_self_update,
            "blockers": self.blockers,
            "repo_url": self.repo_url,
            "compare_url": (
                f"{self.repo_url}/compare/{self.current_ref}...{self.latest_ref}"
                if self.current_ref and self.latest_ref and self.available
                else None
            ),
        }


def manual_instructions(install: Installation, release_tag: str | None = None) -> list[str]:
    """Shell commands that update *this* install, in order.

    ``release_tag`` switches the git steps to "fast-forward to that tag".
    """
    if install.kind == "git" and install.root:
        root = install.root
        pip = (
            str(install.venv / "bin" / "pip")
            if install.venv and (install.venv / "bin" / "pip").exists()
            else f"{install.python} -m pip"
        )
        if release_tag:
            git_steps = ["git fetch --tags origin", f"git merge --ff-only {release_tag}"]
        else:
            git_steps = ["git pull --ff-only"]
        steps = [
            f"cd {root}", *git_steps,
            f"{pip} install -e . -c {CONSTRAINTS_FILE}",
        ]
        if install.service:
            steps.append(f"systemctl restart {install.service}")
        return steps
    if install.kind == "docker":
        return [
            "docker compose pull",
            "docker compose up -d",
            "# (or: docker pull <your-image> && docker compose up -d)",
        ]
    if install.kind == "pip":
        pip = f"{install.python} -m pip"
        steps = [f"{pip} install --upgrade 'beaconmcp @ git+{_REPO_URL}.git'"]
        if install.service:
            steps.append(f"systemctl restart {install.service}")
        return steps
    return [
        "# Could not determine how BeaconMCP was installed here.",
        "# Re-run the installer from a checkout: bash deploy/install.sh",
    ]


def _self_update_blockers(install: Installation, root: Path | None) -> list[str]:
    """Reasons ``apply_update`` would refuse, as operator-facing sentences."""
    blockers: list[str] = []
    if install.kind != "git" or root is None:
        blockers.append(
            f"this is a {install.kind} install, and automatic updates only "
            "support a git checkout"
        )
        return blockers
    if not shutil.which("git"):
        blockers.append("the git binary is not on PATH")
    if working_tree_dirty(root):
        blockers.append(
            "the checkout has uncommitted changes -- commit or stash them "
            "first so the update cannot discard your work"
        )
    return blockers


_cache_lock = threading.Lock()
_cached: UpdateInfo | None = None

#: Serializes the git work. Two entry points can reach this concurrently --
#: the dashboard button and the MCP tool -- and two ``git pull`` /
#: ``pip install`` runs in one checkout would fight over index.lock and
#: could leave a half-applied tree. ``_apply_lock`` is never waited on: a
#: second updater is told one is already running rather than queueing
#: behind a pip that may take minutes.
_apply_lock = threading.Lock()
#: Held across an uncached check so N dashboard tabs opening at once cause
#: one ``git fetch``, not N. Waiters get the result the winner cached.
_check_lock = threading.RLock()


def check_for_update(
    *,
    force: bool = False,
    config_path: Path | None = None,
    install: Installation | None = None,
    settings: UpdatesConfig | None = None,
) -> UpdateInfo:
    """Return update status, using a cached result when it is still fresh.

    Read-only: it fetches git objects (which never touches the working tree)
    and shells out to ``git show``. Failures are captured in
    :attr:`UpdateInfo.error`, never raised.

    ``install`` overrides autodetection; passing one also bypasses the
    cache, since the cache is keyed on "this server" and nothing else.
    ``settings`` defaults to what :func:`configure` recorded.
    """
    global _cached

    if install is not None:
        return _check_uncached(config_path=config_path, install=install, settings=settings)

    def _fresh() -> UpdateInfo | None:
        with _cache_lock:
            cached = _cached
        if cached is None:
            return None
        ttl = FAILED_CHECK_TTL_SECONDS if cached.error else CHECK_TTL_SECONDS
        return cached if time.time() - cached.checked_at < ttl else None

    if not force:
        hit = _fresh()
        if hit is not None:
            return hit

    with _check_lock:
        # Someone may have refreshed it while we waited for the lock; a
        # forced check still runs, since that is the point of forcing.
        if not force:
            hit = _fresh()
            if hit is not None:
                return hit
        info = _check_uncached(config_path=config_path, settings=settings)
        with _cache_lock:
            _cached = info
    return info


def cached_update() -> UpdateInfo | None:
    """Last check result, without triggering a new one."""
    with _cache_lock:
        return _cached


def invalidate_cache() -> None:
    global _cached
    with _cache_lock:
        _cached = None


@dataclass
class _Target:
    """The revision an update would move to."""

    #: "branch" or "release".
    kind: str
    #: Human name: ``origin/main`` or ``v2.1.0``.
    name: str
    #: Full commit sha.
    sha: str


def _resolve_target(root: Path, channel: str) -> tuple[_Target | None, str | None]:
    """Fetch and name the update target, or explain why there is none."""
    if channel == "release":
        # Mirrored into a private namespace, forced and pruned: a plain
        # `fetch --tags` exits 1 for good once upstream re-points a tag the
        # checkout already has, and never forgets a deleted one. Forcing is
        # safe, the ff-only merge and the signature check still gate the move.
        code, _, err = _git(
            root, "fetch", "--quiet", "--no-tags", "--prune", "--force", "origin",
            f"+refs/tags/v*:{_TAG_NAMESPACE}v*",
        )
        if code != 0:
            return None, f"could not reach the remote ({err or 'git fetch failed'})"
        code, out, err = _git(
            root, "for-each-ref", "--sort=-v:refname", "--format=%(refname:strip=3)",
            _TAG_NAMESPACE,
        )
        if code != 0:
            return None, f"could not list release tags ({err or 'git failed'})"
        tags = [t for t in out.splitlines() if _RELEASE_TAG.match(t.strip())]
        if not tags:
            return None, (
                "no release tag (vX.Y.Z) has been published yet; set "
                "features.updates.channel to 'branch' to follow the default branch"
            )
        tag = tags[0].strip()
        code, sha, err = _git(root, "rev-parse", f"{_TAG_NAMESPACE}{tag}^{{commit}}")
        if code != 0:
            return None, f"could not resolve {tag} ({err or 'git failed'})"
        return _Target("release", tag, sha), None

    branch = _default_branch(root)
    code, _, err = _git(root, "fetch", "--quiet", "origin", branch)
    if code != 0:
        return None, f"could not reach the remote ({err or 'git fetch failed'})"
    remote_ref = f"origin/{branch}"
    code, sha, err = _git(root, "rev-parse", remote_ref)
    if code != 0:
        return None, f"could not read {remote_ref} ({err or 'git failed'})"
    return _Target("branch", remote_ref, sha), None


def _verify_signature(root: Path, target: _Target) -> tuple[bool, str]:
    """Check the target's signature against the keys this host trusts.

    A release tag must be a signed annotated tag (``git verify-tag``); on
    the branch channel the tip commit itself must be signed
    (``git verify-commit``; GitHub signs the merges it makes).
    """
    if target.kind == "release":
        ref = f"{_TAG_NAMESPACE}{target.name}"
        # verify-tag checks the signature, not that the signed object is
        # this tag: a signed v2.1.0-rc1 republished as v2.1.0 would pass.
        _, own_name, _ = _git(root, "for-each-ref", "--format=%(tag)", ref)
        if own_name != target.name:
            return False, (
                f"the signed tag object is named {own_name or 'nothing'!r}, "
                f"not {target.name!r}"
            )
        code, out, err = _git(root, "verify-tag", ref)
    else:
        code, out, err = _git(root, "verify-commit", target.sha)
    detail = (err or out or "").strip()
    return code == 0, detail


def _ahead_behind(root: Path, sha: str) -> tuple[int, int] | None:
    """(commits HEAD has that ``sha`` lacks, commits ``sha`` has that HEAD lacks).

    ``None`` when git cannot compare them: that is an error, never "up to date".
    """
    code, out, _ = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{sha}")
    parts = out.split()
    if code != 0 or len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _watchdog_in_flight(root: Path) -> dict[str, Any] | None:
    """The last update's status while its post-restart watchdog may still act."""
    status = last_update_status(root)
    if not status or status.get("outcome") != "pending":
        return None
    try:
        age = time.time() - float(status.get("at", 0))
    except (TypeError, ValueError):
        return None
    return status if 0 <= age < _WATCHDOG_WINDOW_SECONDS else None


def _in_flight_message(status: dict[str, Any]) -> str:
    return (
        f"the previous update ({str(status.get('to', ''))[:12]}) is still being "
        "checked after its restart; wait for it to finish (the outcome lands in "
        f"{STATUS_FILE})"
    )


def last_update_status(root: Path | None) -> dict[str, Any] | None:
    """How the last self-update ended, as written by the restart watchdog."""
    if root is None:
        return None
    path = root / STATUS_FILE
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _check_uncached(
    *,
    config_path: Path | None = None,
    install: Installation | None = None,
    settings: UpdatesConfig | None = None,
) -> UpdateInfo:
    settings = settings or _runtime.settings
    install = install or detect_installation()
    info = UpdateInfo(
        checked_at=time.time(),
        version=current_version(),
        install_kind=install.kind,
        instructions=manual_instructions(install),
        channel=settings.channel,
    )

    root = install.root
    if install.kind != "git" or root is None:
        info.error = (
            f"cannot check automatically: this is a {install.kind} install, "
            "not a git checkout"
        )
        info.blockers = _self_update_blockers(install, root)
        return info
    info.last_update = last_update_status(root)

    code, head, err = _git(root, "rev-parse", "--short", "HEAD")
    if code != 0:
        info.error = f"could not read the local revision ({err or 'git failed'})"
        return info
    info.current_ref = head
    if settings.channel == "branch":
        info.branch = _default_branch(root)

    target, error = _resolve_target(root, settings.channel)
    if target is None:
        info.error = error
        info.blockers = _self_update_blockers(install, root)
        return info
    info.latest_ref = target.sha[:7]
    if target.kind == "release":
        info.release_tag = target.name
        info.instructions = manual_instructions(install, release_tag=target.name)

    counts = _ahead_behind(root, target.sha)
    if counts is None:
        info.error = f"could not compare this checkout with {target.name} (git rev-list failed)"
        info.blockers = _self_update_blockers(install, root)
        return info
    ahead, behind = counts
    info.behind = behind
    info.available = behind > 0

    if not info.available:
        return info

    code, log, _ = _git(
        root, "log", "--no-merges", "--max-count=20",
        "--pretty=format:%h\x1f%s\x1f%aI", f"HEAD..{target.sha}",
    )
    if code == 0 and log:
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                info.commits.append(
                    {"sha": parts[0], "subject": parts[1], "date": parts[2]}
                )

    info.drift = config_drift(root, target.sha, config_path)
    info.blockers = _self_update_blockers(install, root)
    in_flight = _watchdog_in_flight(root)
    if in_flight:
        info.blockers.append(_in_flight_message(in_flight))
    if ahead:
        info.blockers.append(
            f"this checkout has {ahead} commit(s) that {target.name} does not, "
            "so it cannot be fast-forwarded"
        )
    if settings.require_signed:
        ok, detail = _verify_signature(root, target)
        info.signature_ok = ok
        if not ok:
            info.blockers.append(
                f"{target.name} is not signed by a key this host trusts "
                f"(features.updates.require_signed): {_tail(detail, 300) or 'no signature'}"
            )
    info.can_self_update = not info.blockers
    return info


# ---------------------------------------------------------------------------
# Applying an update
# ---------------------------------------------------------------------------

@dataclass
class UpdateStep:
    name: str
    ok: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"step": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class UpdateResult:
    ok: bool
    steps: list[UpdateStep] = field(default_factory=list)
    from_ref: str | None = None
    to_ref: str | None = None
    rolled_back: bool = False
    restart_scheduled: bool = False
    restart_in_seconds: int = 0
    message: str = ""
    drift: ConfigDrift = field(default_factory=ConfigDrift)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [s.to_json() for s in self.steps],
            "from_ref": self.from_ref,
            "to_ref": self.to_ref,
            "rolled_back": self.rolled_back,
            "restart_scheduled": self.restart_scheduled,
            "restart_in_seconds": self.restart_in_seconds,
            "message": self.message,
            "config": self.drift.to_json(),
        }


def _pip_command(install: Installation) -> list[str]:
    if install.venv:
        for candidate in (
            install.venv / "bin" / "pip",
            install.venv / "Scripts" / "pip.exe",
        ):
            if candidate.exists():
                return [str(candidate)]
    return [install.python, "-m", "pip"]


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """Run a command, returning ``(returncode, combined output)``."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)} timed out after {timeout}s"
    except OSError as exc:
        return 1, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _tail(text: str, limit: int = 1500) -> str:
    """Keep the end of a command's output -- that's where errors are."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _pip_install_command(install: Installation, root: Path) -> list[str]:
    """``pip install -e .``, pinned by ``constraints.txt`` when the tree has one."""
    cmd = [*_pip_command(install), "install", "-e", "."]
    if (root / CONSTRAINTS_FILE).is_file():
        cmd += ["-c", CONSTRAINTS_FILE]
    return cmd


def _startup_check_command(install: Installation, config_path: Path | None) -> list[str]:
    cmd = [install.python, "-m", "beaconmcp", "check-startup"]
    if config_path:
        cmd += ["--config", str(config_path)]
    return cmd


#: Post-restart watchdog. Runs detached, as its own transient systemd unit
#: (``systemd-run``): ``systemctl restart`` kills every process in the
#: service's cgroup, so a child of the server would die with it. All values
#: arrive through argv, never interpolated into the script. It restarts the
#: service, waits for the new process to answer /health (and keep answering
#: for ``settle`` seconds with the same MainPID and NRestarts, so a crash
#: loop is not mistaken for a start), and
#: otherwise resets the checkout to the previous revision,
#: reinstalls its pinned dependencies and restarts again. The outcome lands in
#: STATUS_FILE so the next check (and the operator) can see it.
#:
#: The script must never contain a dollar-brace or a double dollar: PID 1
#: expands ``${VAR}`` (and collapses ``$$``) in the ExecStart of the
#: transient unit before ``sh`` sees it, so ``${10}`` would arrive empty.
_WATCHDOG_SCRIPT = r"""
delay=$1 service=$2 url=$3 root=$4 from=$5 to=$6 py=$7 status=$8 wait=$9
shift 9
settle=$1
shift
log() { echo "beaconmcp-update: $*"; }
write_status() {
  printf '{"at": %s, "from": "%s", "to": "%s", "outcome": "%s"}\n' \
    "$(date +%s)" "$from" "$to" "$1" > "$status.tmp" && mv "$status.tmp" "$status"
}
healthy() {
  "$py" -c 'import sys, urllib.request as u; u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=3).read()' \
    "$url" >/dev/null 2>&1
}
ident() {
  systemctl show -p MainPID -p NRestarts --value "$service" 2>/dev/null | tr '\n' ' '
}
stable() { healthy && [ "$(ident)" = "$first" ]; }
wait_healthy() {
  end=$(( $(date +%s) + wait ))
  while [ "$(date +%s)" -lt "$end" ]; do
    if healthy; then
      first=$(ident)
      waited=0
      while [ "$waited" -lt "$settle" ] && stable; do
        sleep 2
        waited=$((waited + 2))
      done
      if [ "$waited" -ge "$settle" ] && stable; then return 0; fi
    fi
    sleep 2
  done
  return 1
}
pip_install() {
  if command -v timeout >/dev/null 2>&1; then timeout 900 "$@"; else "$@"; fi
}
sleep "$delay"
systemctl restart "$service"
if wait_healthy; then
  write_status healthy
  log "$to is up and healthy"
  exit 0
fi
log "no steady healthy answer from $url within $wait seconds; rolling back to $from"
if [ "$(git -C "$root" rev-parse HEAD)" != "$to" ]; then
  write_status superseded
  log "the checkout is no longer at $to (another update ran); not rolling back"
  exit 1
fi
if git -C "$root" reset --hard "$from"; then
  cd "$root" || exit 1
  if [ -f constraints.txt ]; then pip_install "$@" install -e . -c constraints.txt; else pip_install "$@" install -e .; fi \
    || log "reinstalling the dependencies of $from FAILED; run pip install by hand"
  systemctl restart "$service"
  if wait_healthy; then
    write_status rolled_back
    log "rolled back to $from, which is healthy"
  else
    write_status rollback_unhealthy
    log "rolled back to $from, but it does not answer either"
  fi
else
  write_status rollback_failed
  log "git reset to $from failed; fix the checkout by hand"
fi
exit 1
"""


@dataclass
class _Watchdog:
    """What the post-restart watchdog needs to judge and undo an update."""

    health_url: str
    root: Path
    from_sha: str
    to_sha: str
    python: str
    pip: list[str]
    wait_seconds: int = HEALTH_WAIT_SECONDS
    settle_seconds: int = _SETTLE_SECONDS


def _write_status(root: Path, payload: dict[str, Any]) -> None:
    import json

    try:
        (root / STATUS_FILE).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        pass


def _schedule_restart(
    service: str, delay: int, watchdog: _Watchdog | None = None,
) -> str | None:
    """Restart the unit after ``delay`` seconds, detached from this process.

    A direct ``systemctl restart`` would kill us mid-response, so the caller
    would never learn whether the update worked. Detaching and sleeping lets
    the tool result (or the HTTP response) reach the client first.

    With a ``watchdog`` and ``systemd-run`` available, the restart is done by
    :data:`_WATCHDOG_SCRIPT` in its own transient unit, which rolls the
    update back if the new process never becomes healthy. ``systemd-run``
    returns once the unit is started, so its exit status is checked; when it
    fails the restart falls back to the unwatched form.

    Returns ``"watched"``, ``"unwatched"``, or ``None`` when nothing could be
    scheduled.
    """
    if not shutil.which("systemctl"):
        return None
    if watchdog is not None and shutil.which("systemd-run"):
        cmd = [
            "systemd-run", "--quiet", "--collect",
            f"--unit=beaconmcp-update-{time.time_ns()}",
            "--setenv=GIT_TERMINAL_PROMPT=0",
            "/bin/sh", "-c", _WATCHDOG_SCRIPT, "beaconmcp-update",
            str(int(delay)), service, watchdog.health_url, str(watchdog.root),
            watchdog.from_sha, watchdog.to_sha, watchdog.python,
            str(watchdog.root / STATUS_FILE), str(int(watchdog.wait_seconds)),
            str(int(watchdog.settle_seconds)), *watchdog.pip,
        ]
        code, _ = _run(cmd, watchdog.root, 30)
        if code == 0:
            return "watched"
    try:
        subprocess.Popen(
            # Values go through argv, never interpolated into the script:
            # `service` is a literal today, but a future change that made it
            # configurable must not turn this into a shell injection.
            ["sh", "-c", 'sleep "$1"; systemctl restart "$2"', "sh", str(int(delay)), service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return "unwatched"
    except OSError:
        return None


def apply_update(
    *,
    restart: bool = True,
    restart_delay: int = 5,
    config_path: Path | None = None,
    install: Installation | None = None,
    settings: UpdatesConfig | None = None,
) -> UpdateResult:
    """Verify, fast-forward, reinstall pinned dependencies, check startup, restart.

    Two gates. Before the restart, the new revision must load the
    operator's configuration and build the whole server (``beaconmcp
    check-startup``); if not, the checkout is rolled back to where it
    started and nothing is restarted. After the restart, a watchdog rolls
    back to the previous revision if the new process does not answer
    ``/health``. An unattended update that bricks the server is worse than
    no update.
    """
    if not _apply_lock.acquire(blocking=False):
        busy = UpdateResult(ok=False)
        busy.message = (
            "An update is already running on this server. Wait for it to "
            "finish before starting another one."
        )
        busy.steps.append(UpdateStep("preflight", False, busy.message))
        return busy
    try:
        return _apply_update_locked(
            restart=restart,
            restart_delay=restart_delay,
            config_path=config_path,
            install=install,
            settings=settings or _runtime.settings,
        )
    finally:
        _apply_lock.release()


def _apply_update_locked(
    *,
    restart: bool,
    restart_delay: int,
    config_path: Path | None,
    install: Installation | None,
    settings: UpdatesConfig,
) -> UpdateResult:
    install = install or detect_installation()
    result = UpdateResult(ok=False)

    root = install.root
    blockers = _self_update_blockers(install, root)
    if blockers or root is None:
        result.message = "Refusing to update: " + "; ".join(blockers)
        result.steps.append(UpdateStep("preflight", False, result.message))
        return result
    in_flight = _watchdog_in_flight(root)
    if in_flight:
        result.message = "Refusing to update: " + _in_flight_message(in_flight) + "."
        result.steps.append(UpdateStep("preflight", False, result.message))
        return result
    result.steps.append(UpdateStep("preflight", True, "git checkout is clean"))

    code, from_ref, _ = _git(root, "rev-parse", "HEAD")
    if code != 0:
        result.message = "Could not read the current revision."
        result.steps.append(UpdateStep("read-head", False, result.message))
        return result
    result.from_ref = from_ref[:12]

    target, error = _resolve_target(root, settings.channel)
    if target is None:
        result.message = f"Could not find the update target: {error}. Nothing was changed."
        result.steps.append(UpdateStep("fetch", False, error or ""))
        return result
    result.steps.append(UpdateStep("fetch", True, f"{target.name} is {target.sha[:12]}"))

    counts = _ahead_behind(root, target.sha)
    if counts is None:
        result.message = (
            f"Could not compare this checkout with {target.name} (git rev-list "
            "failed). Nothing was changed."
        )
        result.steps.append(UpdateStep("compare", False, result.message))
        return result
    _, behind = counts
    if behind == 0:
        result.ok = True
        result.to_ref = result.from_ref
        result.message = "Already up to date; nothing to do."
        return result

    if settings.require_signed:
        ok, detail = _verify_signature(root, target)
        result.steps.append(UpdateStep("verify-signature", ok, _tail(detail, 600)))
        if not ok:
            result.message = (
                f"Refusing to update: {target.name} is not signed by a key this "
                "host trusts (features.updates.require_signed). Nothing was changed."
            )
            return result

    # Fast-forward to the exact commit that was resolved (and verified),
    # not whatever the remote points at by now.
    code, out, err = _git(root, "merge", "--ff-only", target.sha)
    if code != 0:
        detail = _tail(err or out)
        result.message = f"Could not fast-forward to {target.name}: {detail}. Nothing was changed."
        result.steps.append(UpdateStep("git-update", False, detail))
        return result
    code, to_ref, _ = _git(root, "rev-parse", "HEAD")
    to_sha = to_ref if code == 0 else target.sha
    result.to_ref = to_sha[:12]
    result.steps.append(
        UpdateStep("git-update", True, f"{result.from_ref} -> {result.to_ref} ({target.name})")
    )

    def _rollback(reason: str) -> UpdateResult:
        code, out, err = _git(root, "reset", "--hard", from_ref)
        rolled = code == 0
        result.rolled_back = rolled
        result.steps.append(
            UpdateStep(
                "rollback", rolled,
                f"restored {result.from_ref}" if rolled else _tail(err or out),
            )
        )
        deps_ok = True
        if rolled:
            # Put the dependency set back too, so a half-applied update
            # doesn't leave newer libraries against older code.
            pip_code, pip_out = _run(_pip_install_command(install, root), root, _PIP_TIMEOUT)
            deps_ok = pip_code == 0
            result.steps.append(UpdateStep(
                "rollback-pip-install", deps_ok,
                "previous dependencies restored" if deps_ok else _tail(pip_out),
            ))
        result.ok = False
        if not rolled:
            tail = " ROLLBACK FAILED -- fix the checkout by hand before restarting."
        elif not deps_ok:
            tail = (
                " The checkout was rolled back and the server was NOT restarted, "
                "but reinstalling the previous dependencies FAILED: run "
                f"`pip install -e . -c {CONSTRAINTS_FILE}` in {root} before the "
                "next restart."
            )
        else:
            tail = " The checkout was rolled back and the server was NOT restarted."
        result.message = reason + tail
        return result

    code, out = _run(_pip_install_command(install, root), root, _PIP_TIMEOUT)
    if code != 0:
        result.steps.append(UpdateStep("pip-install", False, _tail(out)))
        return _rollback(f"Dependency install failed: {_tail(out, 400)}.")
    pinned = (root / CONSTRAINTS_FILE).is_file()
    result.steps.append(UpdateStep(
        "pip-install", True,
        f"dependencies pinned by {CONSTRAINTS_FILE}" if pinned else "dependencies up to date",
    ))

    # Startup gate. Run in a subprocess so the *new* code loads the config,
    # imports every module and builds the HTTP app -- not the copy this
    # process imported at boot. validate-config alone only imported
    # config.py, so a broken import anywhere else sailed through.
    code, out = _run(
        _startup_check_command(install, config_path), root, _STARTUP_CHECK_TIMEOUT,
    )
    if code != 0:
        result.steps.append(UpdateStep("check-startup", False, _tail(out)))
        return _rollback(
            f"The new revision does not start with your configuration: {_tail(out, 600)}"
        )
    result.steps.append(UpdateStep("check-startup", True, "config loads and the server builds"))

    result.drift = config_drift(root, "HEAD", config_path)
    result.ok = True

    watched = False
    # A unit file on disk does not mean this process runs under it: restarting
    # the unit while `beaconmcp serve` runs by hand would leave this process
    # answering /health and the watchdog calling the update healthy.
    if restart and install.service and install.under_systemd:
        watchdog = None
        if _runtime.health_url:
            watchdog = _Watchdog(
                health_url=_runtime.health_url, root=root,
                from_sha=from_ref, to_sha=to_sha, python=install.python,
                pip=_pip_command(install),
            )
        mode = _schedule_restart(install.service, restart_delay, watchdog)
        scheduled = mode is not None
        watched = mode == "watched"
        result.restart_scheduled = scheduled
        result.restart_in_seconds = restart_delay if scheduled else 0
        if scheduled:
            _write_status(root, {
                "at": int(time.time()), "from": from_ref, "to": to_sha,
                "outcome": "pending" if watched else "unwatched",
            })
        result.steps.append(
            UpdateStep(
                "restart", scheduled,
                (
                    f"systemctl restart {install.service} in {restart_delay}s"
                    + (
                        f", rolled back if /health does not answer within {HEALTH_WAIT_SECONDS}s"
                        if watched else ", without a post-restart health check"
                    )
                )
                if scheduled else "could not schedule a restart (no systemctl)",
            )
        )

    bits = [f"Updated {result.from_ref} -> {result.to_ref} ({target.name})."]
    if result.restart_scheduled:
        bits.append(
            f"The service restarts in {restart_delay}s to run the new code."
        )
        if watched:
            bits.append(
                f"If it does not answer /health within {HEALTH_WAIT_SECONDS}s it is "
                f"rolled back to {result.from_ref} automatically."
            )
    elif install.service and install.under_systemd:
        bits.append(f"Restart it with: systemctl restart {install.service}")
    else:
        bits.append("Restart the server process to run the new code.")
    if result.drift.new_env_vars:
        bits.append(
            "New environment variables you may need to set in .env: "
            + ", ".join(result.drift.new_env_vars)
        )
    if result.drift.new_config_keys:
        bits.append(
            "New beaconmcp.yaml settings are available: "
            + ", ".join(result.drift.new_config_keys[:10])
        )
    result.message = " ".join(bits)
    return result
