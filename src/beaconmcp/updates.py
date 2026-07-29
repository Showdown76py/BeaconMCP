"""Update detection and self-update for BeaconMCP.

BeaconMCP ships no PyPI package and cuts no releases: the canonical install
is a ``git clone`` at ``/opt/beaconmcp`` with a venv and a systemd unit (see
``deploy/install.sh``). So "is there an update?" means *is this checkout
behind the remote default branch?*, not "is there a newer version string".

Three things live here:

* :func:`detect_installation` -- how this server was installed, so the
  advice we give matches reality instead of assuming everyone ran the
  install script.
* :func:`check_for_update` -- a cached, fail-soft, read-only check. It also
  diffs the *new* ``.env.example`` / ``beaconmcp.yaml.example`` against the
  operator's actual files, which is how we can say "this update wants a
  variable you haven't set" before they apply it.
* :func:`apply_update` -- pull, reinstall dependencies, **validate the
  config**, and roll back if the new revision cannot load it. Restarting
  into a config that refuses to parse would take the server down with no
  one at the keyboard, so validation is a hard gate, not a warning.

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

_REPO_URL = "https://github.com/Showdown76py/BeaconMCP"

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

    def to_json(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
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


def manual_instructions(install: Installation) -> list[str]:
    """Shell commands that update *this* install, in order."""
    if install.kind == "git" and install.root:
        root = install.root
        pip = (
            str(install.venv / "bin" / "pip")
            if install.venv and (install.venv / "bin" / "pip").exists()
            else f"{install.python} -m pip"
        )
        steps = [f"cd {root}", "git pull --ff-only", f"{pip} install -e ."]
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
) -> UpdateInfo:
    """Return update status, using a cached result when it is still fresh.

    Read-only: it fetches git objects (which never touches the working tree)
    and shells out to ``git show``. Failures are captured in
    :attr:`UpdateInfo.error`, never raised.

    ``install`` overrides autodetection; passing one also bypasses the
    cache, since the cache is keyed on "this server" and nothing else.
    """
    global _cached

    if install is not None:
        return _check_uncached(config_path=config_path, install=install)

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
        info = _check_uncached(config_path=config_path)
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


def _check_uncached(
    *, config_path: Path | None = None, install: Installation | None = None,
) -> UpdateInfo:
    install = install or detect_installation()
    info = UpdateInfo(
        checked_at=time.time(),
        version=current_version(),
        install_kind=install.kind,
        instructions=manual_instructions(install),
    )

    root = install.root
    if install.kind != "git" or root is None:
        info.error = (
            f"cannot check automatically: this is a {install.kind} install, "
            "not a git checkout"
        )
        info.blockers = _self_update_blockers(install, root)
        return info

    code, head, err = _git(root, "rev-parse", "--short", "HEAD")
    if code != 0:
        info.error = f"could not read the local revision ({err or 'git failed'})"
        return info
    info.current_ref = head

    branch = _default_branch(root)
    info.branch = branch

    code, _, err = _git(root, "fetch", "--quiet", "origin", branch)
    if code != 0:
        info.error = f"could not reach the remote ({err or 'git fetch failed'})"
        info.blockers = _self_update_blockers(install, root)
        return info

    remote_ref = f"origin/{branch}"
    code, latest, err = _git(root, "rev-parse", "--short", remote_ref)
    if code != 0:
        info.error = f"could not read {remote_ref} ({err or 'git failed'})"
        return info
    info.latest_ref = latest

    code, count, _ = _git(root, "rev-list", "--count", f"HEAD..{remote_ref}")
    info.behind = int(count) if code == 0 and count.isdigit() else 0
    info.available = info.behind > 0

    if not info.available:
        return info

    code, log, _ = _git(
        root, "log", "--no-merges", "--max-count=20",
        "--pretty=format:%h\x1f%s\x1f%aI", f"HEAD..{remote_ref}",
    )
    if code == 0 and log:
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                info.commits.append(
                    {"sha": parts[0], "subject": parts[1], "date": parts[2]}
                )

    info.drift = config_drift(root, remote_ref, config_path)
    info.blockers = _self_update_blockers(install, root)
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


def _schedule_restart(service: str, delay: int) -> bool:
    """Restart the unit after ``delay`` seconds, detached from this process.

    A direct ``systemctl restart`` would kill us mid-response, so the caller
    would never learn whether the update worked. Detaching and sleeping lets
    the tool result (or the HTTP response) reach the client first.
    """
    if not shutil.which("systemctl"):
        return False
    try:
        subprocess.Popen(
            # Values go through argv, never interpolated into the script:
            # `service` is a literal today, but a future change that made it
            # configurable must not turn this into a shell injection.
            [
                "sh", "-c", 'sleep "$1"; systemctl restart "$2"',
                "sh", str(int(delay)), service,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def apply_update(
    *,
    restart: bool = True,
    restart_delay: int = 5,
    config_path: Path | None = None,
    install: Installation | None = None,
) -> UpdateResult:
    """Pull, reinstall dependencies, validate the config, then restart.

    The config validation is a **gate**: if the new revision cannot load the
    operator's configuration (a newly required setting, a renamed key), the
    checkout is rolled back to where it started and nothing is restarted.
    An unattended update that bricks the server is worse than no update.
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
        )
    finally:
        _apply_lock.release()


def _apply_update_locked(
    *,
    restart: bool,
    restart_delay: int,
    config_path: Path | None,
    install: Installation | None,
) -> UpdateResult:
    install = install or detect_installation()
    result = UpdateResult(ok=False)

    root = install.root
    blockers = _self_update_blockers(install, root)
    if blockers or root is None:
        result.message = "Refusing to update: " + "; ".join(blockers)
        result.steps.append(UpdateStep("preflight", False, result.message))
        return result
    result.steps.append(UpdateStep("preflight", True, "git checkout is clean"))

    code, from_ref, _ = _git(root, "rev-parse", "HEAD")
    if code != 0:
        result.message = "Could not read the current revision."
        result.steps.append(UpdateStep("read-head", False, result.message))
        return result
    result.from_ref = from_ref[:12]

    branch = _default_branch(root)
    code, out, err = _git(root, "pull", "--ff-only", "origin", branch)
    if code != 0:
        detail = _tail(err or out)
        result.message = (
            f"git pull failed: {detail}. Nothing was changed."
        )
        result.steps.append(UpdateStep("git-pull", False, detail))
        return result
    code, to_ref, _ = _git(root, "rev-parse", "HEAD")
    result.to_ref = to_ref[:12] if code == 0 else None
    result.steps.append(
        UpdateStep("git-pull", True, f"{result.from_ref} -> {result.to_ref}")
    )

    if result.from_ref == result.to_ref:
        result.ok = True
        result.message = "Already up to date; nothing to do."
        return result

    def _rollback(reason: str) -> UpdateResult:
        code, out, err = _git(root, "reset", "--hard", from_ref)
        rolled = code == 0
        if rolled:
            # Put the dependency set back too, so a half-applied update
            # doesn't leave newer libraries against older code.
            _run([*_pip_command(install), "install", "-e", "."], root, _PIP_TIMEOUT)
        result.rolled_back = rolled
        result.steps.append(
            UpdateStep(
                "rollback", rolled,
                f"restored {result.from_ref}" if rolled else _tail(err or out),
            )
        )
        result.ok = False
        result.message = reason + (
            " The checkout was rolled back and the server was NOT restarted."
            if rolled
            else " ROLLBACK FAILED -- fix the checkout by hand before restarting."
        )
        return result

    code, out = _run(
        [*_pip_command(install), "install", "-e", "."], root, _PIP_TIMEOUT,
    )
    if code != 0:
        result.steps.append(UpdateStep("pip-install", False, _tail(out)))
        return _rollback(f"Dependency install failed: {_tail(out, 400)}.")
    result.steps.append(UpdateStep("pip-install", True, "dependencies up to date"))

    # Config gate. Run in a subprocess so the *new* code parses the config,
    # not the copy this process imported at boot.
    validate = [install.python, "-m", "beaconmcp", "validate-config"]
    if config_path:
        validate += ["--config", str(config_path)]
    code, out = _run(validate, root, _GIT_TIMEOUT)
    if code != 0:
        result.steps.append(UpdateStep("validate-config", False, _tail(out)))
        return _rollback(
            f"The new revision cannot load your configuration: {_tail(out, 600)}"
        )
    result.steps.append(UpdateStep("validate-config", True, "config still loads"))

    result.drift = config_drift(root, "HEAD", config_path)
    result.ok = True

    if restart and install.service:
        scheduled = _schedule_restart(install.service, restart_delay)
        result.restart_scheduled = scheduled
        result.restart_in_seconds = restart_delay if scheduled else 0
        result.steps.append(
            UpdateStep(
                "restart", scheduled,
                f"systemctl restart {install.service} in {restart_delay}s"
                if scheduled else "could not schedule a restart (no systemctl)",
            )
        )

    bits = [f"Updated {result.from_ref} -> {result.to_ref}."]
    if result.restart_scheduled:
        bits.append(
            f"The service restarts in {restart_delay}s to run the new code."
        )
    elif install.service:
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
