"""MCP tools for keeping the BeaconMCP server itself up to date."""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .. import audit, updates
from ..auth import current_client_id
from ..config import UpdatesConfig


def register_maintenance_tools(
    mcp: MCPServer,
    settings: UpdatesConfig | None = None,
    *,
    config_path: Path | None = None,
) -> None:
    """Register ``beaconmcp_check_update`` and ``beaconmcp_self_update``.

    ``settings.enabled`` gates the whole module (no network egress at all);
    ``settings.allow_self_update`` keeps the check but refuses to apply.
    """
    settings = settings or UpdatesConfig()
    if not settings.enabled:
        return

    @mcp.tool()
    def beaconmcp_check_update() -> dict:
        """Check whether a newer BeaconMCP revision is available.

        Reports the running version, how many commits this install is
        behind its update target (the upstream default branch, or the
        newest release tag on the ``release`` channel), the changelog
        between the two, how the last self-update ended (``last_update``),
        and — importantly — any configuration the new revision knows about
        that this install has not set yet (new ``.env`` variables, new
        ``beaconmcp.yaml`` settings).

        Also returns the exact shell commands that would update *this*
        install, which differ between a git checkout, a pip install and a
        container.

        Read-only and safe to call at any time: it fetches git objects but
        never modifies the working tree. Results are cached for a few hours;
        this returns the cached answer when it is still fresh.
        """
        info = updates.check_for_update(config_path=config_path)
        payload = info.to_json()
        payload["self_update_allowed"] = settings.allow_self_update
        if info.can_self_update and not settings.allow_self_update:
            payload["can_self_update"] = False
            payload["blockers"] = [
                *payload.get("blockers", []),
                "self-update is disabled by features.updates.allow_self_update",
            ]
        return payload

    if not settings.allow_self_update:
        return

    @mcp.tool()
    def beaconmcp_self_update(confirm: bool = False, restart: bool = True) -> dict:
        """Update this BeaconMCP server to the latest upstream revision.

        Runs, in order: fetch the target (default branch tip, or newest
        release tag) → verify its signature when ``require_signed`` is on →
        fast-forward → reinstall dependencies pinned by ``constraints.txt``
        → **start the new code against the real configuration**
        (``beaconmcp check-startup``) → schedule a watched service restart.

        The startup check is a hard gate. If the new revision cannot start
        with the operator's config (a renamed or newly required setting, a
        broken import), the checkout is rolled back to exactly where it
        started, dependencies are restored, and nothing is restarted. The
        return value says so explicitly. After the restart, a watchdog rolls
        back to the previous revision if ``/health`` does not answer.

        Requires ``confirm=True``. Call ``beaconmcp_check_update`` first and
        show the user what is about to change — including any new config
        variables — before asking them to confirm.

        Refuses to run when the checkout has uncommitted changes, or when
        this is not a git install; ``beaconmcp_check_update`` reports those
        blockers in advance along with manual instructions.

        The restart is deliberately deferred a few seconds so this response
        reaches you before the process dies. After that, expect the server
        to be briefly unreachable.
        """
        if not confirm:
            info = updates.check_for_update(config_path=config_path)
            return {
                "ok": False,
                "applied": False,
                "reason": "confirmation_required",
                "message": (
                    "This will pull new code, reinstall dependencies and "
                    "restart the server. Review the pending changes, then "
                    "call again with confirm=True."
                ),
                "pending": info.to_json(),
            }

        client_id = current_client_id()
        audit.emit("maintenance.self_update.start", client_id=client_id)
        result = updates.apply_update(restart=restart, config_path=config_path)
        audit.emit(
            "maintenance.self_update.finish",
            client_id=client_id,
            ok=result.ok,
            from_ref=result.from_ref,
            to_ref=result.to_ref,
            rolled_back=result.rolled_back,
        )
        # The next check must not serve a stale "update available".
        updates.invalidate_cache()

        payload = result.to_json()
        payload["applied"] = result.ok
        return payload
