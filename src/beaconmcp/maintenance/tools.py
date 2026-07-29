"""MCP tools for keeping the BeaconMCP server itself up to date."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .. import audit, updates
from ..auth import current_client_id
from ..config import UpdatesConfig


def register_maintenance_tools(
    mcp: FastMCP,
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
        behind the upstream default branch, the changelog between the two,
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

        Runs, in order: ``git pull --ff-only`` → reinstall the Python
        package and its dependencies → **validate the configuration against
        the new code** → schedule a service restart.

        The configuration check is a hard gate. If the new revision cannot
        load the operator's config (because a setting was renamed, or a new
        one is now required), the checkout is rolled back to exactly where
        it started, dependencies are restored, and nothing is restarted. The
        return value says so explicitly.

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
