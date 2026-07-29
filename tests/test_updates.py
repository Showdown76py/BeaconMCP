"""Update detection / self-update tests.

git operations run for real against throwaway repositories built in
``tmp_path``: the whole point of this module is that it drives git
correctly, and a mocked ``subprocess.run`` would only prove the mock
agrees with itself. The two genuinely unsafe steps -- ``pip install`` and
the config validation subprocess -- are the ones we stub, so the tests
exercise the *orchestration* (pull, validate, roll back) without touching
the interpreter this suite runs in.

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
    """Stub pip + validate-config; git stays real.

    Returns the recorded command list plus a dict the test mutates to make
    a given step fail.
    """
    recorded: list[list[str]] = []
    outcomes = {"pip": 0, "validate": 0}

    def fake_run(cmd, cwd, timeout):
        recorded.append(list(cmd))
        if "pip" in " ".join(cmd):
            return outcomes["pip"], "pip output"
        if "validate-config" in cmd:
            return outcomes["validate"], "config error: BEACONMCP_NEW_KEY is required"
        return 0, ""

    monkeypatch.setattr(updates, "_run", fake_run)
    monkeypatch.setattr(updates, "_schedule_restart", lambda service, delay: True)
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

    result = updates.apply_update(install=install, restart_delay=3)

    assert result.ok is True
    assert result.rolled_back is False
    assert result.from_ref != result.to_ref
    names = [s.name for s in result.steps]
    assert names == [
        "preflight", "git-pull", "pip-install", "validate-config", "restart",
    ]
    # The pull actually moved the checkout.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(checkout),
        capture_output=True, text=True,
    ).stdout.strip()
    assert head.startswith(result.to_ref)
    assert result.restart_scheduled is True
    assert result.restart_in_seconds == 3
    assert any("validate-config" in c for cmd in recorded for c in cmd)


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
    assert "cannot load your configuration" in result.message
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
        totp_locked=lambda cid: False,
        totp_record_failure=lambda cid: None,
        totp_record_success=lambda cid: None,
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


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

class _RecordingMCP:
    """Minimal stand-in for FastMCP that just collects registrations."""

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
