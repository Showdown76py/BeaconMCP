"""BeaconMCP -- remote MCP server for Proxmox VE and BMC infrastructure."""

try:  # pragma: no cover - trivial
    from importlib.metadata import version as _version

    __version__ = _version("beaconmcp")
except Exception:  # noqa: BLE001 - running straight from an uninstalled tree
    # Fallback when the package was never pip-installed. Keep in sync with
    # [project].version in pyproject.toml. (The old hard-coded "0.1.0" had
    # drifted three majors behind it.)
    __version__ = "2.0.0"
