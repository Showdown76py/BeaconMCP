"""Update detection / self-update tests.

git operations run for real against throwaway repositories built in
``tmp_path``: the whole point of this module is that it drives git
correctly, and a mocked ``subprocess.run`` would only prove the mock
agrees with itself. The two genuinely unsafe steps -- ``pip install`` and
the ``check-startup`` subprocess -- are the ones we stub, so the tests
exercise the *orchestration* (fetch, verify, fast-forward, check, roll
back) without touching the interpreter this suite runs in. The post-restart
watchdog script runs for real under ``/bin/sh`` with fake ``systemctl``,
``pip`` and health probe executables.

Run with::

    pytest tests/test_updates.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from beaconmcp import updates  # noqa: E402
from beaconmcp.auth import TotpResult  # noqa: E402
from beaconmcp.config import UpdatesConfig  # noqa: E402
from beaconmcp.dashboard.app import (  # noqa: E402
    DashboardDeps,
    build_dashboard_routes,
)
from beaconmcp.dashboard.csrf import CSRF_COOKIE  # noqa: E402
from beaconmcp.dashboard.db import Database  # noqa: E402
from beaconmcp.dashboard.session import SessionStore  # noqa: E402
from beaconmcp.updates import Installation  # noqa: E402


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for update tests"
)


# ---------------------------------------------------------------------------
# git fixtures
# ---------------------------------------------------------------------------

def _run(*args: str, cwd: Path) -> None:
    subprocess.run(
        list(args), cwd=str(cwd), check=True,
        capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _commit(repo: Path, message: str, files: dict[str, str] | None = None) -> None:
    for name, content in (files or {}).items():
        (repo / name).write_text(content, encoding="utf-8")
        _run("git", "add", name, cwd=repo)
    _run("git", "commit", "--allow-empty", "-m", message, cwd=repo)


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    """An 'upstream' repo standing in for GitHub."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", cwd=repo)
    _run("git", "config", "user.email", "t@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    _commit(repo, "initial", {
        ".env.example": "BEACONMCP_SESSION_KEY=\n# GEMINI_API_KEY=\n",
        "beaconmcp.yaml.example": "server:\n  port: 8420\n",
        "pyproject.toml": "[project]\nname = 'x'\n",
    })
    return repo


@pytest.fixture()
def checkout(tmp_path: Path, upstream: Path) -> Path:
    """A clone of ``upstream``, i.e. what /opt/beaconmcp looks like."""
    local = tmp_path / "local"
    _run("git", "clone", str(upstream), str(local), cwd=tmp_path)
    _run("git", "config", "user.email", "t@example.com", cwd=local)
    _run("git", "config", "user.name", "Test", cwd=local)
    return local


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path_factory):
    """No test inherits update settings, or the developer's git config.

    A global ``commit.gpgsign`` or ``gpg.format`` would otherwise sign the
    "unsigned" commits these tests build, or change how they are checked.
    """
    monkeypatch.setattr(updates, "_runtime", updates._Runtime())
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _install(root: Path) -> Installation:
    return Installation(
        kind="git", root=root, python=sys.executable, venv=None,
        editable=True, under_systemd=False, service=None, in_container=False,
    )


def _advance(upstream: Path, message: str, files: dict[str, str] | None = None) -> None:
    """Push a new commit upstream so the checkout falls behind."""
    _commit(upstream, message, files)


# ---------------------------------------------------------------------------
# Installation detection
# ---------------------------------------------------------------------------

def test_detects_this_checkout_as_git():
    install = updates.detect_installation()
    assert install.kind == "git"
    assert install.root is not None and (install.root / ".git").exists()


def test_systemd_detected_from_invocation_id(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "deadbeef")
    assert updates.detect_installation().under_systemd is True
    monkeypatch.delenv("INVOCATION_ID")
    assert updates.detect_installation().under_systemd is False


def test_current_version_is_not_the_stale_placeholder():
    # __init__ used to hard-code 0.1.0 while pyproject said 2.0.0.
    assert updates.current_version() != "0.1.0"


# ---------------------------------------------------------------------------
# Instructions per install kind
# ---------------------------------------------------------------------------

def test_git_instructions_include_pull_and_install(tmp_path):
    steps = updates.manual_instructions(_install(tmp_path))
    assert any("git pull" in s for s in steps)
    assert any("install -e ." in s for s in steps)
    assert not any("systemctl" in s for s in steps)


def test_git_instructions_add_restart_when_a_service_exists(tmp_path):
    install = _install(tmp_path)
    install.service = "beaconmcp"
    steps = updates.manual_instructions(install)
    assert steps[-1] == "systemctl restart beaconmcp"


def test_git_instructions_prefer_the_venv_pip(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "pip").write_text("#!/bin/sh\n")
    install = _install(tmp_path)
    install.venv = venv
    steps = updates.manual_instructions(install)
    assert str(venv / "bin" / "pip") in " ".join(steps)


def test_docker_instructions_do_not_mention_pip():
    install = Installation(
        kind="docker", root=None, python=sys.executable, venv=None,
        editable=False, under_systemd=False, service=None, in_container=True,
    )
    steps = updates.manual_instructions(install)
    assert any("docker" in s for s in steps)
    assert not any("pip install" in s for s in steps)


def test_pip_instructions_point_at_the_git_url():
    install = Installation(
        kind="pip", root=None, python="/usr/bin/python3", venv=None,
        editable=False, under_systemd=False, service=None, in_container=False,
    )
    steps = updates.manual_instructions(install)
    assert any("git+https://github.com/Showdown76py/BeaconMCP" in s for s in steps)


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

def test_up_to_date_checkout_reports_no_update(checkout):
    info = updates.check_for_update(install=_install(checkout))
    assert info.error is None
    assert info.available is False
    assert info.behind == 0
    assert info.current_ref == info.latest_ref


def test_behind_checkout_reports_commits_and_log(checkout, upstream):
    _advance(upstream, "feat: shiny thing")
    _advance(upstream, "fix: subtle bug")
    info = updates.check_for_update(install=_install(checkout))
    assert info.available is True
    assert info.behind == 2
    assert [c["subject"] for c in info.commits] == [
        "fix: subtle bug", "feat: shiny thing",
    ]
    assert info.can_self_update is True
    assert info.blockers == []
    assert info.to_json()["compare_url"].endswith(
        f"{info.current_ref}...{info.latest_ref}"
    )


def test_dirty_checkout_blocks_self_update(checkout, upstream):
    _advance(upstream, "feat: thing")
    (checkout / "pyproject.toml").write_text("[project]\nname = 'edited'\n")
    info = updates.check_for_update(install=_install(checkout))
    assert info.available is True
    assert info.can_self_update is False
    assert any("uncommitted" in b for b in info.blockers)


def test_non_git_install_reports_why_it_cannot_check():
    install = Installation(
        kind="pip", root=None, python=sys.executable, venv=None,
        editable=False, under_systemd=False, service=None, in_container=False,
    )
    info = updates.check_for_update(install=install)
    assert info.available is False
    assert "pip install" in (info.error or "") or "not a git checkout" in (info.error or "")
    assert info.can_self_update is False


def test_unreachable_remote_degrades_to_an_error(checkout, tmp_path):
    _run("git", "remote", "set-url", "origin", str(tmp_path / "gone"), cwd=checkout)
    info = updates.check_for_update(install=_install(checkout))
    assert info.available is False
    assert info.error and "remote" in info.error


def test_check_result_is_cached(monkeypatch):
    updates.invalidate_cache()
    calls = []

    def fake(**kwargs):
        calls.append(1)
        return updates.UpdateInfo(checked_at=time.time())

    monkeypatch.setattr(updates, "_check_uncached", fake)
    updates.check_for_update()
    updates.check_for_update()
    assert len(calls) == 1
    updates.check_for_update(force=True)
    assert len(calls) == 2
    updates.invalidate_cache()


# ---------------------------------------------------------------------------
# Config drift
# ---------------------------------------------------------------------------

def test_env_names_include_commented_examples():
    text = "A=1\n# B=\nexport C=3\n#not_a_var\n"
    assert updates._env_names(text) == ["A", "B", "C"]


def test_yaml_paths_are_dotted_and_ignore_list_indices():
    paths = updates._yaml_paths("a:\n  b: 1\nlist:\n  - x: 1\n")
    assert "a" in paths and "a.b" in paths
    assert "list" in paths and "list.x" in paths


def test_drift_reports_new_env_var(checkout, upstream, monkeypatch):
    monkeypatch.delenv("NEW_SECRET", raising=False)
    (checkout / ".env").write_text("BEACONMCP_SESSION_KEY=abc\n")
    _advance(upstream, "feat: new secret", {
        ".env.example": "BEACONMCP_SESSION_KEY=\nNEW_SECRET=\n",
    })
    info = updates.check_for_update(install=_install(checkout))
    assert info.drift.new_env_vars == ["NEW_SECRET"]


def test_drift_ignores_vars_already_set_in_the_environment(
    checkout, upstream, monkeypatch,
):
    monkeypatch.setenv("NEW_SECRET", "already-there")
    (checkout / ".env").write_text("BEACONMCP_SESSION_KEY=abc\n")
    _advance(upstream, "feat: new secret", {
        ".env.example": "BEACONMCP_SESSION_KEY=\nNEW_SECRET=\n",
    })
    info = updates.check_for_update(install=_install(checkout))
    assert info.drift.new_env_vars == []


def test_drift_reports_new_yaml_settings(checkout, upstream):
    (checkout / "beaconmcp.yaml").write_text("server:\n  port: 8420\n")
    _advance(upstream, "feat: knob", {
        "beaconmcp.yaml.example": "server:\n  port: 8420\nfeatures:\n  updates:\n    enabled: true\n",
    })
    info = updates.check_for_update(install=_install(checkout))
    assert "features" in info.drift.new_config_keys
    assert "features.updates.enabled" in info.drift.new_config_keys


def test_drift_is_empty_when_nothing_new(checkout, upstream):
    (checkout / "beaconmcp.yaml").write_text("server:\n  port: 8420\n")
    (checkout / ".env").write_text("BEACONMCP_SESSION_KEY=abc\nGEMINI_API_KEY=x\n")
    _advance(upstream, "docs: typo")
    info = updates.check_for_update(install=_install(checkout))
    assert info.drift.empty


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

@pytest.fixture()
def stub_side_effects(monkeypatch):
    """Stub pip + check-startup; git stays real.

    Returns the recorded command list plus a dict the test mutates to make
    a given step fail.
    """
    recorded: list[list[str]] = []
    outcomes = {"pip": 0, "validate": 0}

    def fake_run(cmd, cwd, timeout):
        recorded.append(list(cmd))
        if "pip" in " ".join(cmd):
            return outcomes["pip"], "pip output"
        if "check-startup" in cmd:
            return outcomes["validate"], "config error: BEACONMCP_NEW_KEY is required"
        return 0, ""

    monkeypatch.setattr(updates, "_run", fake_run)
    monkeypatch.setattr(updates, "_schedule_restart", lambda service, delay, watchdog=None: "unwatched")
    return recorded, outcomes


def test_apply_refuses_a_dirty_checkout(checkout, upstream, stub_side_effects):
    _advance(upstream, "feat: thing")
    (checkout / "pyproject.toml").write_text("[project]\nname = 'edited'\n")
    result = updates.apply_update(install=_install(checkout))
    assert result.ok is False
    assert "uncommitted" in result.message
    assert result.steps[0].name == "preflight" and result.steps[0].ok is False


def test_apply_refuses_a_non_git_install(stub_side_effects):
    install = Installation(
        kind="docker", root=None, python=sys.executable, venv=None,
        editable=False, under_systemd=False, service=None, in_container=True,
    )
    result = updates.apply_update(install=install)
    assert result.ok is False
    assert "docker install" in result.message


def test_apply_is_a_noop_when_already_current(checkout, stub_side_effects):
    result = updates.apply_update(install=_install(checkout))
    assert result.ok is True
    assert "Already up to date" in result.message


def test_apply_pulls_installs_validates_and_restarts(
    checkout, upstream, stub_side_effects,
):
    recorded, _ = stub_side_effects
    _advance(upstream, "feat: shiny")
    install = _install(checkout)
    install.service = "beaconmcp"
    install.under_systemd = True

    result = updates.apply_update(install=install, restart_delay=3)

    assert result.ok is True
    assert result.rolled_back is False
    assert result.from_ref != result.to_ref
    names = [s.name for s in result.steps]
    assert names == [
        "preflight", "fetch", "git-update", "pip-install", "check-startup", "restart",
    ]
    # The pull actually moved the checkout.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(checkout),
        capture_output=True, text=True,
    ).stdout.strip()
    assert head.startswith(result.to_ref)
    assert result.restart_scheduled is True
    assert result.restart_in_seconds == 3
    assert any("check-startup" in c for cmd in recorded for c in cmd)


def test_apply_rolls_back_when_the_new_code_rejects_the_config(
    checkout, upstream, stub_side_effects,
):
    recorded, outcomes = stub_side_effects
    outcomes["validate"] = 1
    _advance(upstream, "feat: needs a new setting")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(checkout),
        capture_output=True, text=True,
    ).stdout.strip()

    result = updates.apply_update(install=_install(checkout))

    assert result.ok is False
    assert result.rolled_back is True
    assert "does not start with your configuration" in result.message
    assert "was NOT restarted" in result.message
    assert "BEACONMCP_NEW_KEY" in result.message
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(checkout),
        capture_output=True, text=True,
    ).stdout.strip()
    assert after == before, "checkout must be back where it started"


def test_apply_rolls_back_when_dependencies_fail(
    checkout, upstream, stub_side_effects,
):
    _, outcomes = stub_side_effects
    outcomes["pip"] = 1
    _advance(upstream, "feat: new dep")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(checkout),
        capture_output=True, text=True,
    ).stdout.strip()

    result = updates.apply_update(install=_install(checkout))

    assert result.ok is False
    assert result.rolled_back is True
    assert "Dependency install failed" in result.message
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(checkout),
        capture_output=True, text=True,
    ).stdout.strip()
    assert after == before


def test_apply_reports_new_config_after_success(
    checkout, upstream, stub_side_effects,
):
    (checkout / ".env").write_text("BEACONMCP_SESSION_KEY=abc\n")
    _advance(upstream, "feat: new knob", {
        ".env.example": "BEACONMCP_SESSION_KEY=\nBEACONMCP_NEW_THING=\n",
    })
    result = updates.apply_update(install=_install(checkout))
    assert result.ok is True
    assert result.drift.new_env_vars == ["BEACONMCP_NEW_THING"]
    assert "BEACONMCP_NEW_THING" in result.message


def test_apply_without_a_service_tells_you_to_restart(
    checkout, upstream, stub_side_effects,
):
    _advance(upstream, "feat: thing")
    result = updates.apply_update(install=_install(checkout))
    assert result.ok is True
    assert result.restart_scheduled is False
    assert "Restart the server process" in result.message


# ---------------------------------------------------------------------------
# Concurrency
#
# Two entry points reach this code: the dashboard button and the MCP tool.
# Two `git pull` / `pip install` runs in one checkout would fight over
# index.lock and could leave a half-applied tree.
# ---------------------------------------------------------------------------

def test_a_second_update_is_refused_while_one_runs(checkout, upstream, monkeypatch):
    import threading

    _advance(upstream, "feat: thing")
    started = threading.Event()
    release = threading.Event()
    second: dict[str, object] = {}

    real_run = updates._run

    def blocking_run(cmd, cwd, timeout):
        if "pip" in " ".join(cmd):
            started.set()
            release.wait(10)
            return 0, "pip output"
        if "check-startup" in cmd:
            return 0, ""
        return real_run(cmd, cwd, timeout)

    monkeypatch.setattr(updates, "_run", blocking_run)
    monkeypatch.setattr(updates, "_schedule_restart", lambda service, delay, watchdog=None: "unwatched")

    install = _install(checkout)
    first: dict[str, object] = {}

    def run_first():
        first["result"] = updates.apply_update(install=install)

    t = threading.Thread(target=run_first)
    t.start()
    try:
        assert started.wait(10), "first update never reached the pip step"
        # A second caller must be told, not queued behind a long pip run.
        second["result"] = updates.apply_update(install=install)
    finally:
        release.set()
        t.join(15)

    busy = second["result"]
    assert busy.ok is False
    assert "already running" in busy.message
    assert busy.from_ref is None, "the refused caller must not touch git"
    assert first["result"].ok is True


def test_the_lock_is_released_after_a_failure(checkout, upstream, stub_side_effects):
    _, outcomes = stub_side_effects
    outcomes["validate"] = 1
    _advance(upstream, "feat: thing")
    first = updates.apply_update(install=_install(checkout))
    assert first.ok is False and first.rolled_back is True
    # Second call must run, not report "already running".
    second = updates.apply_update(install=_install(checkout))
    assert "already running" not in second.message


def test_concurrent_checks_fetch_once(monkeypatch):
    import threading

    updates.invalidate_cache()
    calls = []
    gate = threading.Event()

    def slow_check(**kwargs):
        calls.append(1)
        gate.wait(5)
        return updates.UpdateInfo(checked_at=time.time())

    monkeypatch.setattr(updates, "_check_uncached", slow_check)
    threads = [
        threading.Thread(target=lambda: updates.check_for_update())
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    time.sleep(0.4)
    gate.set()
    for t in threads:
        t.join(10)

    assert len(calls) == 1, f"expected one fetch, got {len(calls)}"
    updates.invalidate_cache()


def test_restart_command_passes_values_through_argv(monkeypatch):
    """No shell interpolation, so a future configurable unit name is safe."""
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd

    monkeypatch.setattr(
        updates.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(updates.subprocess, "Popen", FakePopen)
    assert updates._schedule_restart("beaconmcp; rm -rf /", 5) == "unwatched"
    cmd = seen["cmd"]
    # The dangerous string is an argument, never part of the script.
    assert "beaconmcp; rm -rf /" in cmd
    assert "rm -rf" not in cmd[2]
    assert cmd[2] == 'sleep "$1"; systemctl restart "$2"'


def _watchdog(tmp_path, python="/opt/venv/bin/python", pip=("/opt/venv/bin/pip",)):
    return updates._Watchdog(
        health_url="http://127.0.0.1:8420/health", root=tmp_path,
        from_sha="a" * 40, to_sha="b" * 40, python=python, pip=list(pip),
    )


def test_restart_with_a_watchdog_runs_as_its_own_transient_unit(monkeypatch, tmp_path):
    """systemctl restart kills the service's cgroup, children included."""
    ran = []

    def fake_run(cmd, cwd, timeout):
        ran.append(cmd)
        return 0, ""

    class NoPopen:
        def __init__(self, cmd, **kwargs):
            raise AssertionError("no unwatched restart when systemd-run worked")

    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(updates, "_run", fake_run)
    monkeypatch.setattr(updates.subprocess, "Popen", NoPopen)
    assert updates._schedule_restart("beaconmcp", 5, _watchdog(tmp_path)) == "watched"
    (cmd,) = ran
    assert cmd[0] == "systemd-run"
    script_at = cmd.index("-c") + 1
    # The script is a constant; every value travels as an argument.
    assert cmd[script_at] == updates._WATCHDOG_SCRIPT
    assert cmd[script_at + 1:] == [
        "beaconmcp-update", "5", "beaconmcp", "http://127.0.0.1:8420/health",
        str(tmp_path), "a" * 40, "b" * 40, "/opt/venv/bin/python",
        str(tmp_path / updates.STATUS_FILE), str(updates.HEALTH_WAIT_SECONDS), str(updates._SETTLE_SECONDS),
        "/opt/venv/bin/pip",
    ]


def test_two_restarts_in_the_same_second_get_distinct_units(monkeypatch, tmp_path):
    units = []
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        updates, "_run",
        lambda cmd, cwd, timeout: units.append(next(a for a in cmd if a.startswith("--unit="))) or (0, ""),
    )
    updates._schedule_restart("beaconmcp", 5, _watchdog(tmp_path))
    updates._schedule_restart("beaconmcp", 5, _watchdog(tmp_path))
    assert len(set(units)) == 2


def test_a_refused_systemd_run_falls_back_to_an_unwatched_restart(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd

    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(updates, "_run", lambda cmd, cwd, timeout: (1, "Failed to start transient service unit"))
    monkeypatch.setattr(updates.subprocess, "Popen", FakePopen)
    assert updates._schedule_restart("beaconmcp", 5, _watchdog(tmp_path)) == "unwatched"
    assert seen["cmd"][:3] == ["sh", "-c", 'sleep "$1"; systemctl restart "$2"']


def test_restart_without_systemd_run_falls_back_to_an_unwatched_restart(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = cmd

    monkeypatch.setattr(
        updates.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(updates.subprocess, "Popen", FakePopen)
    assert updates._schedule_restart("beaconmcp", 5, _watchdog(tmp_path)) == "unwatched"
    assert seen["cmd"][:3] == ["sh", "-c", 'sleep "$1"; systemctl restart "$2"']


def test_a_new_update_waits_for_the_previous_watchdog(checkout, upstream, stub_side_effects):
    _advance(upstream, "feat: thing")
    updates._write_status(checkout, {
        "at": int(time.time()) - 30, "from": "a" * 40, "to": "b" * 40, "outcome": "pending",
    })
    before = _head(checkout)
    info = updates.check_for_update(install=_install(checkout))
    assert any("still being checked after its restart" in b for b in info.blockers)
    result = updates.apply_update(install=_install(checkout))
    assert result.ok is False
    assert "still being checked" in result.message
    assert _head(checkout) == before


def test_a_stale_pending_status_does_not_block(checkout, upstream, stub_side_effects):
    _advance(upstream, "feat: thing")
    updates._write_status(checkout, {
        "at": int(time.time()) - updates._WATCHDOG_WINDOW_SECONDS - 1,
        "from": "a" * 40, "to": "b" * 40, "outcome": "pending",
    })
    assert updates.apply_update(install=_install(checkout)).ok is True


def test_a_failed_comparison_is_an_error_not_up_to_date(checkout, upstream, stub_side_effects, monkeypatch):
    _advance(upstream, "feat: thing")
    monkeypatch.setattr(updates, "_ahead_behind", lambda root, sha: None)
    info = updates.check_for_update(install=_install(checkout))
    assert info.available is False and "could not compare" in info.error
    result = updates.apply_update(install=_install(checkout))
    assert result.ok is False
    assert "Already up to date" not in result.message
    assert "Could not compare" in result.message


def test_rollback_reports_a_failed_dependency_reinstall(checkout, upstream, monkeypatch):
    pip_calls = []

    def fake_run(cmd, cwd, timeout):
        if "pip" in " ".join(cmd):
            pip_calls.append(cmd)
            return (0, "ok") if len(pip_calls) == 1 else (1, "No matching distribution")
        if "check-startup" in cmd:
            return 1, "ModuleNotFoundError: nope"
        return 0, ""

    monkeypatch.setattr(updates, "_run", fake_run)
    _advance(upstream, "feat: broken")
    result = updates.apply_update(install=_install(checkout))
    assert result.rolled_back is True
    step = next(s for s in result.steps if s.name == "rollback-pip-install")
    assert step.ok is False and "No matching distribution" in step.detail
    assert "reinstalling the previous dependencies FAILED" in result.message


def test_apply_hands_the_watchdog_both_revisions(checkout, upstream, monkeypatch, stub_side_effects):
    calls = []
    monkeypatch.setattr(
        updates, "_schedule_restart",
        lambda service, delay, watchdog=None: calls.append(watchdog) or "watched",
    )
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    updates.configure(UpdatesConfig(), health_url="http://127.0.0.1:8420/health")
    before = _head(checkout)
    _advance(upstream, "feat: thing")
    install = _install(checkout)
    install.service = "beaconmcp"
    install.under_systemd = True

    result = updates.apply_update(install=install)

    assert result.ok is True
    (watchdog,) = calls
    assert watchdog.from_sha == before
    assert watchdog.to_sha == _head(checkout) != before
    assert watchdog.health_url == "http://127.0.0.1:8420/health"
    assert "rolled back if /health does not answer" in result.steps[-1].detail
    status = updates.last_update_status(checkout)
    assert status["outcome"] == "pending" and status["from"] == before


def test_pip_install_is_pinned_when_the_tree_has_constraints(tmp_path):
    install = _install(tmp_path)
    assert "-c" not in updates._pip_install_command(install, tmp_path)
    (tmp_path / "constraints.txt").write_text("requests==2.0\n")
    cmd = updates._pip_install_command(install, tmp_path)
    assert cmd[-4:] == ["-e", ".", "-c", "constraints.txt"]
    assert any("-c constraints.txt" in step for step in updates.manual_instructions(install))


def test_startup_check_command_passes_the_config(tmp_path):
    cmd = updates._startup_check_command(_install(tmp_path), tmp_path / "b.yaml")
    assert cmd == [sys.executable, "-m", "beaconmcp", "check-startup", "--config", str(tmp_path / "b.yaml")]


def test_diverged_checkout_is_reported_as_a_blocker(checkout, upstream):
    _commit(checkout, "local hotfix")
    _advance(upstream, "feat: thing")
    info = updates.check_for_update(install=_install(checkout))
    assert info.available is True
    assert info.can_self_update is False
    assert any("cannot be fast-forwarded" in b for b in info.blockers)


# ---------------------------------------------------------------------------
# Release channel and signatures
# ---------------------------------------------------------------------------

def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True,
    ).stdout.strip()


def _tag(repo: Path, name: str, *, signed: bool = False) -> None:
    if signed:
        _run("git", "tag", "-s", "-m", name, name, cwd=repo)
    else:
        _run("git", "tag", "-a", "-m", name, name, cwd=repo)


RELEASE = UpdatesConfig(channel="release")


def test_release_channel_follows_the_newest_version_tag(checkout, upstream, stub_side_effects):
    for version in ("v1.2.0", "v1.10.0"):
        _advance(upstream, f"release {version}")
        _tag(upstream, version)
    tagged = _head(upstream)
    _advance(upstream, "release v2.0.0-rc1")
    _tag(upstream, "v2.0.0-rc1")
    _advance(upstream, "feat: unreleased work on main")

    info = updates.check_for_update(install=_install(checkout), settings=RELEASE)
    assert info.channel == "release"
    # v1.10.0 sorts above v1.2.0 by version, and the rc is skipped.
    assert info.release_tag == "v1.10.0"
    assert info.behind == 2
    assert info.can_self_update is True
    assert "git merge --ff-only v1.10.0" in info.instructions

    result = updates.apply_update(install=_install(checkout), settings=RELEASE)
    assert result.ok is True
    assert _head(checkout) == tagged, "must stop at the tag, not the branch tip"


def test_release_channel_survives_a_moved_tag(checkout, upstream, stub_side_effects):
    _advance(upstream, "release v1.0.0")
    _tag(upstream, "v1.0.0")
    _run("git", "fetch", "--quiet", "--tags", "origin", cwd=checkout)
    assert updates.check_for_update(install=_install(checkout), settings=RELEASE).release_tag == "v1.0.0"
    # Upstream re-tags v1.0.0 to fix the release, then ships v1.1.0. A plain
    # `fetch --tags` now exits 1 ("would clobber existing tag") forever.
    _advance(upstream, "fix the release")
    _run("git", "tag", "-f", "-a", "-m", "v1.0.0", "v1.0.0", cwd=upstream)
    _advance(upstream, "release v1.1.0")
    _tag(upstream, "v1.1.0")

    info = updates.check_for_update(install=_install(checkout), settings=RELEASE)
    assert info.error is None, info.error
    assert info.release_tag == "v1.1.0"


def test_require_signed_refuses_a_tag_object_under_another_name(
    checkout, upstream, stub_side_effects, gpg_signer,
):
    # A signed pre-release, republished under a release name.
    _advance(upstream, "release v2.0.0-rc1")
    _tag(upstream, "v2.0.0-rc1", signed=True)
    obj = subprocess.run(
        ["git", "rev-parse", "refs/tags/v2.0.0-rc1"], cwd=str(upstream),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run("git", "update-ref", "refs/tags/v2.0.0", obj, cwd=upstream)
    before = _head(checkout)

    result = updates.apply_update(install=_install(checkout), settings=SIGNED_RELEASE)

    assert result.ok is False
    assert "v2.0.0-rc1" in result.steps[-1].detail
    assert _head(checkout) == before


def test_release_channel_without_tags_says_so(checkout, upstream, stub_side_effects):
    _advance(upstream, "feat: thing")
    info = updates.check_for_update(install=_install(checkout), settings=RELEASE)
    assert info.available is False
    assert "no release tag" in info.error
    before = _head(checkout)
    result = updates.apply_update(install=_install(checkout), settings=RELEASE)
    assert result.ok is False
    assert "no release tag" in result.message
    assert _head(checkout) == before


def test_unknown_update_channel_is_a_config_error(tmp_path):
    from beaconmcp.config import Config, ConfigError

    path = tmp_path / "beaconmcp.yaml"
    base = (
        "version: 1\nproxmox:\n  nodes:\n    - name: pve1\n      host: 10.0.0.1\n"
        "      token_id: root@pam!x\n      token_secret: s\n"
    )
    path.write_text(base + "features:\n  updates:\n    channel: nightly\n")
    with pytest.raises(ConfigError, match="features.updates.channel"):
        Config.load(config_path=path)
    path.write_text(
        base + "features:\n  updates:\n    channel: Release\n    require_signed: true\n"
    )
    cfg = Config.load(config_path=path)
    assert cfg.features.updates.channel == "release"
    assert cfg.features.updates.require_signed is True
    assert cfg.redacted()["features"]["updates"]["channel"] == "release"


@pytest.fixture()
def gpg_signer(upstream, monkeypatch):
    """A throwaway GPG key; upstream signs with it and this host trusts it."""
    if shutil.which("gpg") is None:
        pytest.skip("gpg is required for signature tests")
    import tempfile

    # Short path: gpg-agent's socket path has a ~100 character limit.
    home = tempfile.mkdtemp(prefix="bmg")
    os.chmod(home, 0o700)
    monkeypatch.setenv("GNUPGHOME", home)
    subprocess.run(
        ["gpg", "--batch", "--passphrase", "", "--quick-gen-key",
         "Release Bot <release@example.com>", "ed25519", "sign", "never"],
        check=True, capture_output=True,
    )
    out = subprocess.run(
        ["gpg", "--list-secret-keys", "--with-colons"],
        check=True, capture_output=True, text=True,
    ).stdout
    fpr = next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr"))
    _run("git", "config", "user.signingkey", fpr, cwd=upstream)
    yield home
    subprocess.run(["gpgconf", "--kill", "gpg-agent"], capture_output=True)
    shutil.rmtree(home, ignore_errors=True)


SIGNED_RELEASE = UpdatesConfig(channel="release", require_signed=True)


def test_require_signed_refuses_an_unsigned_release(checkout, upstream, stub_side_effects):
    _advance(upstream, "release v1.0.0")
    _tag(upstream, "v1.0.0")
    before = _head(checkout)

    info = updates.check_for_update(install=_install(checkout), settings=SIGNED_RELEASE)
    assert info.signature_ok is False
    assert info.can_self_update is False
    assert any("not signed by a key this host trusts" in b for b in info.blockers)

    result = updates.apply_update(install=_install(checkout), settings=SIGNED_RELEASE)
    assert result.ok is False
    assert [s.name for s in result.steps][-1] == "verify-signature"
    assert _head(checkout) == before, "nothing may change before the signature passes"


def test_require_signed_accepts_a_trusted_signed_release(
    checkout, upstream, stub_side_effects, gpg_signer,
):
    _advance(upstream, "release v1.0.0")
    _tag(upstream, "v1.0.0", signed=True)
    info = updates.check_for_update(install=_install(checkout), settings=SIGNED_RELEASE)
    assert info.signature_ok is True and info.can_self_update is True
    result = updates.apply_update(install=_install(checkout), settings=SIGNED_RELEASE)
    assert result.ok is True
    assert any(s.name == "verify-signature" and s.ok for s in result.steps)
    assert _head(checkout) == _head(upstream)


def test_require_signed_refuses_a_signature_from_an_unknown_key(
    checkout, upstream, stub_side_effects, gpg_signer, monkeypatch, tmp_path,
):
    _advance(upstream, "release v1.0.0")
    _tag(upstream, "v1.0.0", signed=True)
    # This host's keyring does not hold the release key.
    import tempfile

    other = tempfile.mkdtemp(prefix="bmo")
    os.chmod(other, 0o700)
    monkeypatch.setenv("GNUPGHOME", other)
    before = _head(checkout)
    result = updates.apply_update(install=_install(checkout), settings=SIGNED_RELEASE)
    shutil.rmtree(other, ignore_errors=True)
    assert result.ok is False
    assert _head(checkout) == before


def test_require_signed_on_the_branch_checks_the_tip_commit(
    checkout, upstream, stub_side_effects, gpg_signer,
):
    signed_branch = UpdatesConfig(require_signed=True)
    _advance(upstream, "feat: unsigned")
    result = updates.apply_update(install=_install(checkout), settings=signed_branch)
    assert result.ok is False and "not signed" in result.message
    _run("git", "commit", "--allow-empty", "-S", "-m", "feat: signed", cwd=upstream)
    result = updates.apply_update(install=_install(checkout), settings=signed_branch)
    assert result.ok is True
    assert _head(checkout) == _head(upstream)


# ---------------------------------------------------------------------------
# Post-restart watchdog: the real script, under /bin/sh
# ---------------------------------------------------------------------------

def _exe(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _run_watchdog(
    tmp_path, checkout, from_sha, to_sha, probe_body, *, settle="0", show_body="echo 1; echo 0\n",
):
    """Run _WATCHDOG_SCRIPT with fake systemctl, pip and health probe.

    ``systemctl show`` (MainPID, NRestarts) answers through ``show_body`` and
    is left out of the call log; every other systemctl call is logged.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    _exe(
        bindir / "systemctl",
        f'if [ "$1" = show ]; then\n{show_body}exit 0\nfi\necho "systemctl $*" >> {log}\n',
    )
    pip = _exe(bindir / "fake-pip", f'echo "pip $* (in $(pwd))" >> {log}\n')
    # Stands in for "$py -c <probe> <url>": healthy or not, by exit code.
    probe = _exe(bindir / "fake-python", probe_body)
    status = checkout / updates.STATUS_FILE
    proc = subprocess.run(
        ["/bin/sh", "-c", updates._WATCHDOG_SCRIPT, "beaconmcp-update",
         "0", "beaconmcp", "http://127.0.0.1:1/health", str(checkout),
         from_sha, to_sha, str(probe), str(status), "3", settle, str(pip)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    return proc, log.read_text() if log.exists() else "", updates.last_update_status(checkout)


def test_watchdog_keeps_a_healthy_update(tmp_path, checkout, upstream):
    from_sha = _head(checkout)
    _advance(upstream, "feat: thing")
    _run("git", "pull", "--ff-only", cwd=checkout)
    to_sha = _head(checkout)

    proc, calls, status = _run_watchdog(tmp_path, checkout, from_sha, to_sha, "exit 0\n")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls.splitlines() == ["systemctl restart beaconmcp"]
    assert status["outcome"] == "healthy" and status["to"] == to_sha
    assert _head(checkout) == to_sha


def test_watchdog_rolls_back_an_update_that_never_answers(tmp_path, checkout, upstream):
    _advance(upstream, "pins", {"constraints.txt": ""})
    _run("git", "pull", "--ff-only", cwd=checkout)
    from_sha = _head(checkout)
    _advance(upstream, "feat: breaks at runtime")
    _run("git", "pull", "--ff-only", cwd=checkout)
    to_sha = _head(checkout)

    # Healthy only once the checkout is back on the old revision.
    probe = f'[ "$(git -C {checkout} rev-parse HEAD)" = "{from_sha}" ]\n'
    proc, calls, status = _run_watchdog(tmp_path, checkout, from_sha, to_sha, probe)

    assert proc.returncode == 1
    assert _head(checkout) == from_sha
    assert calls.splitlines() == [
        "systemctl restart beaconmcp",
        f"pip install -e . -c constraints.txt (in {checkout})",
        "systemctl restart beaconmcp",
    ]
    assert status["outcome"] == "rolled_back"
    assert "rolling back" in proc.stdout


def test_watchdog_reports_a_rollback_that_does_not_help(tmp_path, checkout, upstream):
    from_sha = _head(checkout)
    _advance(upstream, "feat: thing")
    _run("git", "pull", "--ff-only", cwd=checkout)
    proc, _, status = _run_watchdog(tmp_path, checkout, from_sha, _head(checkout), "exit 1\n")
    assert proc.returncode == 1
    assert status["outcome"] == "rollback_unhealthy"
    assert _head(checkout) == from_sha


def test_watchdog_does_not_undo_a_newer_update(tmp_path, checkout, upstream):
    """If another update moved HEAD past ``to``, rolling back would wipe it."""
    from_sha = _head(checkout)
    _advance(upstream, "feat: first")
    _run("git", "pull", "--ff-only", cwd=checkout)
    to_sha = _head(checkout)
    _advance(upstream, "feat: second")
    _run("git", "pull", "--ff-only", cwd=checkout)
    newer = _head(checkout)

    proc, _, status = _run_watchdog(tmp_path, checkout, from_sha, to_sha, "exit 1\n")

    assert proc.returncode == 1
    assert _head(checkout) == newer
    assert status["outcome"] == "superseded"


def test_watchdog_script_survives_systemd_expansion():
    # PID 1 expands ${VAR} and collapses $$ in a transient unit's ExecStart,
    # script text included: ${10} once reached sh as an empty settle.
    assert "${" not in updates._WATCHDOG_SCRIPT
    assert "$$" not in updates._WATCHDOG_SCRIPT


def test_watchdog_rolls_back_a_crash_loop_that_answers_between_restarts(
    tmp_path, checkout, upstream,
):
    from_sha = _head(checkout)
    _advance(upstream, "feat: dies a few seconds after each start")
    _run("git", "pull", "--ff-only", cwd=checkout)
    to_sha = _head(checkout)

    # /health answers every time it is probed, but on the new revision every
    # probe meets a new MainPID: systemd keeps restarting a crashing process.
    show = (
        f'if [ "$(git -C {checkout} rev-parse HEAD)" = "{to_sha}" ]; then '
        'date +%s%N; else echo 1; fi; echo 0\n'
    )
    proc, _, status = _run_watchdog(
        tmp_path, checkout, from_sha, to_sha, "exit 0\n", settle="2", show_body=show,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert status["outcome"] == "rolled_back"
    assert _head(checkout) == from_sha


def test_no_restart_when_the_server_runs_outside_its_unit(checkout, upstream, stub_side_effects):
    # The unit file exists, but this process was started by hand: restarting
    # the unit would leave it answering /health for code it never loaded.
    _advance(upstream, "feat: thing")
    install = _install(checkout)
    install.service = "beaconmcp"
    install.under_systemd = False

    result = updates.apply_update(install=install, restart_delay=3)

    assert result.ok is True
    assert result.restart_scheduled is False
    assert "restart" not in [s.name for s in result.steps]
    assert "Restart the server process" in result.message


def test_check_reports_how_the_last_update_ended(checkout):
    updates._write_status(checkout, {"outcome": "rolled_back", "from": "a", "to": "b"})
    info = updates.check_for_update(install=_install(checkout))
    assert info.last_update["outcome"] == "rolled_back"
    assert info.to_json()["last_update"]["outcome"] == "rolled_back"


# ---------------------------------------------------------------------------
# Dashboard endpoints
# ---------------------------------------------------------------------------

class FakeClientStore:
    def __init__(self):
        self.clients = {
            "beaconmcp_test": {
                "secret": "sk_test", "name": "Test Client", "totp": "123456",
            }
        }

    def verify(self, client_id, secret):
        c = self.clients.get(client_id)
        return bool(c and c["secret"] == secret)

    def check_totp(self, client_id, code):
        c = self.clients.get(client_id)
        return TotpResult.OK if (c and c["totp"] == code) else TotpResult.INVALID

    def get_name(self, client_id):
        c = self.clients.get(client_id)
        return c["name"] if c else None


class FakeTokenStore:
    def __init__(self):
        self._tokens: dict[str, str] = {}

    def issue(self, client_id, name=None):
        token = f"bearer_{len(self._tokens)}"
        self._tokens[token] = client_id
        return token, 86400

    def validate(self, token):
        return self._tokens.get(token)

    def revoke(self, token):
        self._tokens.pop(token, None)
        return True

    def list_named(self, client_id):
        return []


def _make_client(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("BEACONMCP_DASHBOARD_DB", str(tmp_path / "d.db"))
    db = Database(tmp_path / "d.db")
    deps = DashboardDeps(
        database=db,
        session_store=SessionStore(db, key=os.urandom(32)),
        client_store=FakeClientStore(),
        token_store=FakeTokenStore(),
        totp_locked=lambda cid, ip=None: False,
        totp_record_failure=lambda cid, ip=None: None,
        totp_record_success=lambda cid, ip=None: None,
        **overrides,
    )
    client = TestClient(
        Starlette(routes=build_dashboard_routes(deps)), follow_redirects=False,
    )
    return client, deps


def _sign_in(client) -> str:
    client.get("/app/login")
    token = client.cookies.get(CSRF_COOKIE)
    res = client.post(
        "/app/login",
        data={
            "csrf_token": token, "client_id": "beaconmcp_test",
            "client_secret": "sk_test", "totp": "123456",
        },
        headers={"X-CSRF-Token": token, "X-BeaconMCP-Mode": "json"},
    )
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def test_update_status_requires_a_session(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    assert client.get("/app/api/update").status_code == 401


def test_update_status_returns_the_check(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _sign_in(client)
    monkeypatch.setattr(
        updates, "check_for_update",
        lambda **kw: updates.UpdateInfo(
            checked_at=time.time(), available=True, behind=3, branch="main",
            current_ref="aaaaaaa", latest_ref="bbbbbbb", install_kind="git",
            can_self_update=True, instructions=["git pull --ff-only"],
        ),
    )
    body = client.get("/app/api/update").json()
    assert body["enabled"] is True
    assert body["available"] is True and body["behind"] == 3
    assert body["self_update_allowed"] is True


def test_update_status_is_inert_when_disabled(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, updates_enabled=False)
    _sign_in(client)
    body = client.get("/app/api/update").json()
    assert body == {"enabled": False, "available": False}


def test_status_hides_self_update_when_not_allowed(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, allow_self_update=False)
    _sign_in(client)
    monkeypatch.setattr(
        updates, "check_for_update",
        lambda **kw: updates.UpdateInfo(
            checked_at=time.time(), available=True, can_self_update=True,
        ),
    )
    body = client.get("/app/api/update").json()
    assert body["can_self_update"] is False
    assert body["self_update_allowed"] is False


def test_apply_requires_csrf(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    _sign_in(client)
    res = client.post("/app/api/update/apply", json={"totp": "123456"})
    assert res.status_code == 403


def test_apply_requires_a_fresh_totp(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    token = _sign_in(client)
    res = client.post(
        "/app/api/update/apply", json={"totp": "000000"},
        headers={"X-CSRF-Token": token},
    )
    assert res.status_code == 401
    assert "2FA" in res.json()["error"]

    res = client.post(
        "/app/api/update/apply", json={},
        headers={"X-CSRF-Token": token},
    )
    assert res.status_code == 400


def test_apply_refused_when_self_update_is_disabled(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch, allow_self_update=False)
    token = _sign_in(client)
    res = client.post(
        "/app/api/update/apply", json={"totp": "123456"},
        headers={"X-CSRF-Token": token},
    )
    assert res.status_code == 403
    assert "disabled" in res.json()["error"]


def test_apply_runs_the_update_with_a_valid_code(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    token = _sign_in(client)
    seen = {}

    def fake_apply(**kwargs):
        seen.update(kwargs)
        return updates.UpdateResult(
            ok=True, from_ref="aaaaaaa", to_ref="bbbbbbb",
            restart_scheduled=True, restart_in_seconds=5, message="Updated.",
        )

    monkeypatch.setattr(updates, "apply_update", fake_apply)
    res = client.post(
        "/app/api/update/apply", json={"totp": "123456"},
        headers={"X-CSRF-Token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["restart_scheduled"] is True
    assert "config_path" in seen


def test_failed_apply_surfaces_a_500(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    token = _sign_in(client)
    monkeypatch.setattr(
        updates, "apply_update",
        lambda **kw: updates.UpdateResult(
            ok=False, rolled_back=True, message="rolled back",
        ),
    )
    res = client.post(
        "/app/api/update/apply", json={"totp": "123456"},
        headers={"X-CSRF-Token": token},
    )
    assert res.status_code == 500
    assert res.json()["rolled_back"] is True


def test_every_page_carries_the_toast_and_its_script(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    body = client.get("/app/login").text
    assert 'id="update-toast"' in body
    assert "/app/static/update_banner.js" in body


def test_signed_in_screen_has_a_slot_for_the_update_mention(tmp_path, monkeypatch):
    """The toast opts out of the auth pages; login.js fills this instead."""
    client, _ = _make_client(tmp_path, monkeypatch)
    assert 'id="update-note"' in client.get("/app/login").text


# ---------------------------------------------------------------------------
# Asset cache busting
#
# A server that updates itself must not leave browsers executing the
# previous release's JavaScript, which is what Starlette's default
# (ETag/Last-Modified but no Cache-Control -> heuristic freshness) allows.
# ---------------------------------------------------------------------------

def test_asset_urls_carry_a_fingerprint(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    body = client.get("/app/login").text
    for asset in ("app.css", "theme.js", "update_banner.js", "login.js"):
        assert f"/app/static/{asset}?v=" in body, asset


def test_fingerprint_changes_when_an_asset_changes(tmp_path, monkeypatch):
    from beaconmcp.dashboard import app as dashboard_app

    before = dashboard_app._compute_asset_version()
    static_dir = dashboard_app._DASHBOARD_DIR / "static"
    target = static_dir / "app.css"
    original = target.stat()
    # Push past the newest file in the directory, not just past this one:
    # the fingerprint is the directory maximum, so bumping a file that is
    # not currently the newest changes nothing and the assertion below
    # would fail for a reason that has nothing to do with fingerprinting.
    newest = max(p.stat().st_mtime for p in static_dir.iterdir() if p.is_file())
    try:
        os.utime(target, (original.st_atime, newest + 120))
        assert dashboard_app._compute_asset_version() != before
    finally:
        os.utime(target, (original.st_atime, original.st_mtime))
    assert dashboard_app._compute_asset_version() == before


def test_versioned_assets_are_cacheable_forever(tmp_path, monkeypatch):
    from beaconmcp.dashboard.app import ASSET_VERSION

    client, _ = _make_client(tmp_path, monkeypatch)
    res = client.get(f"/app/static/app.css?v={ASSET_VERSION}")
    assert res.status_code == 200
    assert "immutable" in res.headers["cache-control"]


def test_unversioned_assets_must_revalidate(tmp_path, monkeypatch):
    """A hand-typed or legacy URL can never pin stale code."""
    client, _ = _make_client(tmp_path, monkeypatch)
    res = client.get("/app/static/app.css")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_pages_and_api_are_never_cached(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, monkeypatch)
    assert client.get("/app/login").headers["cache-control"] == "no-store"
    _sign_in(client)
    assert client.get("/app/api/update").headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

class _RecordingMCP:
    """Minimal stand-in for MCPServer that just collects registrations."""

    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def test_tools_are_not_registered_when_updates_are_disabled():
    from beaconmcp.maintenance import register_maintenance_tools

    mcp = _RecordingMCP()
    register_maintenance_tools(mcp, UpdatesConfig(enabled=False))
    assert mcp.tools == {}


def test_only_the_check_tool_when_self_update_is_disabled():
    from beaconmcp.maintenance import register_maintenance_tools

    mcp = _RecordingMCP()
    register_maintenance_tools(mcp, UpdatesConfig(allow_self_update=False))
    assert set(mcp.tools) == {"beaconmcp_check_update"}


def test_both_tools_by_default():
    from beaconmcp.maintenance import register_maintenance_tools

    mcp = _RecordingMCP()
    register_maintenance_tools(mcp, UpdatesConfig())
    assert set(mcp.tools) == {"beaconmcp_check_update", "beaconmcp_self_update"}


def test_self_update_tool_requires_confirmation(monkeypatch):
    from beaconmcp.maintenance import register_maintenance_tools

    mcp = _RecordingMCP()
    register_maintenance_tools(mcp, UpdatesConfig())
    monkeypatch.setattr(
        updates, "check_for_update",
        lambda **kw: updates.UpdateInfo(checked_at=time.time(), available=True),
    )
    called = []
    monkeypatch.setattr(
        updates, "apply_update",
        lambda **kw: called.append(1) or updates.UpdateResult(ok=True),
    )

    out = mcp.tools["beaconmcp_self_update"]()
    assert out["ok"] is False
    assert out["reason"] == "confirmation_required"
    assert out["pending"]["available"] is True
    assert called == [], "must not touch the checkout without confirm=True"

    out = mcp.tools["beaconmcp_self_update"](confirm=True)
    assert called == [1]
    assert out["applied"] is True


def test_check_tool_reports_the_self_update_policy(monkeypatch):
    from beaconmcp.maintenance import register_maintenance_tools

    mcp = _RecordingMCP()
    register_maintenance_tools(mcp, UpdatesConfig(allow_self_update=False))
    monkeypatch.setattr(
        updates, "check_for_update",
        lambda **kw: updates.UpdateInfo(
            checked_at=time.time(), available=True, can_self_update=True,
        ),
    )
    out = mcp.tools["beaconmcp_check_update"]()
    assert out["can_self_update"] is False
    assert any("allow_self_update" in b for b in out["blockers"])

