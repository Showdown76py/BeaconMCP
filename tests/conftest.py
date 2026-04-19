"""Shared pytest configuration.

``test_integration.py`` is a standalone script with its own ``TestRunner``
and is designed to be executed as ``python tests/test_integration.py``
against live infrastructure. Its ``test_*`` functions take a ``runner``
and ``tools`` positional argument, which pytest tries (and fails) to
resolve as fixtures -- producing a pile of ERRORs on every ``pytest``
run even when nothing destructive would have happened.

We tell pytest to skip that file at collection time unless the opt-in
environment variable ``BEACONMCP_RUN_INTEGRATION=1`` is set. The script
path stays runnable as a plain Python program.
"""

from __future__ import annotations

import os


def _run_integration_enabled() -> bool:
    return os.environ.get("BEACONMCP_RUN_INTEGRATION", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Collected by pytest; a relative path listed here is skipped entirely.
collect_ignore: list[str] = []
if not _run_integration_enabled():
    collect_ignore.append("test_integration.py")
