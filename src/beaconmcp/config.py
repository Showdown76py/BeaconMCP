"""Configuration loader for BeaconMCP.

Loads a YAML config file (``beaconmcp.yaml``) that describes the
infrastructure topology: Proxmox nodes, BMC devices (HP iLO / IPMI / ...),
SSH credentials, server settings, and feature toggles. Secrets are kept
out of the YAML by referencing environment variables with
``${VAR_NAME}`` placeholders that are resolved at load time.

A legacy path is preserved: if no YAML is found but ``PVE1_HOST`` is set
in the environment, the loader synthesizes an equivalent structure from
the old ``PVE*_``, ``ILO_``, and ``SSH_`` variables and emits a
``DeprecationWarning``. The legacy path is removed in 2.1.
"""

from __future__ import annotations

import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# --- Dataclasses -----------------------------------------------------------


@dataclass
class PVENode:
    name: str
    host: str
    token_id: str
    token_secret: str


@dataclass
class BMCDevice:
    id: str
    type: str  # "hp_ilo" | "ipmi" | "idrac" | "supermicro"
    host: str
    user: str
    password: str
    jump_host: str | None = None  # Proxmox node name, or None for direct access


@dataclass
class SSHConfig:
    user: str
    password: str
    # Template for resolving bare numeric IDs (VMIDs) to IP addresses,
    # e.g. "192.168.1.{id}". None disables the numeric-ID fallback.
    vmid_to_ip: str | None = None


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8420
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    clients_file: Path = Path("/opt/beaconmcp/clients.json")
    session_key: str | None = None


@dataclass
class DashboardConfig:
    enabled: bool = True
    gemini_api_key: str = ""
    limit_5h_usd: float = 2.0
    limit_week_usd: float = 10.0
    public_url: str | None = None
    mcp_mode: str = "local"  # "local" | "remote"


@dataclass
class FeaturesConfig:
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    ssh_enabled: bool = True


@dataclass
class Config:
    server: ServerConfig
    pve_nodes: list[PVENode]
    bmc_devices: list[BMCDevice]
    ssh: SSHConfig | None
    features: FeaturesConfig
    verify_ssl: bool
    infrastructure: dict

    # --- Loading ----------------------------------------------------------

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        """Load config from YAML.

        Resolution order for the YAML path:
            1. ``config_path`` argument (CLI ``--config`` flag)
            2. ``BEACONMCP_CONFIG`` environment variable
            3. ``./beaconmcp.yaml``
            4. ``/etc/beaconmcp/config.yaml``

        If no YAML is found, falls back to the legacy env-var loader with
        a ``DeprecationWarning``.
        """
        path = cls._resolve_config_path(config_path)
        if path is not None:
            return cls._from_yaml(path)
        if os.environ.get("PVE1_HOST"):
            warnings.warn(
                "No beaconmcp.yaml found; falling back to legacy environment "
                "variables (PVE1_*, PVE2_*, ILO_*, SSH_*). This path is "
                "deprecated and will be removed in 2.1. Migrate to "
                "beaconmcp.yaml (see beaconmcp.yaml.example).",
                DeprecationWarning,
                stacklevel=2,
            )
            return cls._from_legacy_env()
        print(
            "ERROR: No configuration file found. Create beaconmcp.yaml in the "
            "working directory or set BEACONMCP_CONFIG. See beaconmcp.yaml.example.",
            file=sys.stderr,
        )
        sys.exit(1)

    @classmethod
    def from_env(cls) -> Config:
        """Deprecated alias for :meth:`load`.

        Kept so existing call sites (``server.py``) keep working through the
        refactor. Prefer ``Config.load()`` in new code.
        """
        return cls.load()

    @staticmethod
    def _resolve_config_path(override: Path | None) -> Path | None:
        if override is not None:
            if not override.exists():
                print(
                    f"ERROR: Config file {override} does not exist.",
                    file=sys.stderr,
                )
                sys.exit(1)
            return override
        env_path = os.environ.get("BEACONMCP_CONFIG", "").strip()
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p
            print(
                f"ERROR: BEACONMCP_CONFIG={env_path} points at a missing file.",
                file=sys.stderr,
            )
            sys.exit(1)
        for candidate in (Path("beaconmcp.yaml"), Path("/etc/beaconmcp/config.yaml")):
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def _from_yaml(cls, path: Path) -> Config:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top-level YAML must be a mapping.")
        resolved = _resolve_env_refs(raw, path=path)
        return cls._build(resolved)

    @classmethod
    def _from_legacy_env(cls) -> Config:
        """Synthesize an equivalent YAML structure from legacy env vars."""
        raw: dict[str, Any] = {"version": 1, "proxmox": {"nodes": []}}

        # Walk PVE1..PVE9
        for i in range(1, 10):
            host = os.environ.get(f"PVE{i}_HOST", "")
            tok_id = os.environ.get(f"PVE{i}_TOKEN_ID", "")
            tok_sec = os.environ.get(f"PVE{i}_TOKEN_SECRET", "")
            if host and tok_id and tok_sec:
                raw["proxmox"]["nodes"].append({
                    "name": f"pve{i}",
                    "host": host,
                    "token_id": tok_id,
                    "token_secret": tok_sec,
                })

        raw["proxmox"]["verify_ssl"] = (
            os.environ.get("PVE_VERIFY_SSL", "false").strip().lower() == "true"
        )

        ilo_host = os.environ.get("ILO_HOST", "")
        if ilo_host:
            raw["bmc"] = {
                "devices": [
                    {
                        "id": "ilo",
                        "type": "hp_ilo",
                        "host": ilo_host,
                        "user": os.environ.get("ILO_USER", ""),
                        "password": os.environ.get("ILO_PASSWORD", ""),
                        "jump_host": os.environ.get("ILO_JUMP_HOST", "")
                        or (
                            raw["proxmox"]["nodes"][0]["name"]
                            if raw["proxmox"]["nodes"]
                            else None
                        ),
                    }
                ]
            }

        ssh_user = os.environ.get("SSH_USER", "")
        ssh_pw = os.environ.get("SSH_PASSWORD", "")
        if ssh_user and ssh_pw:
            raw["ssh"] = {
                "user": ssh_user,
                "password": ssh_pw,
                "vmid_to_ip": os.environ.get("SSH_VMID_TO_IP") or None,
            }

        infra_path = Path(os.environ.get("INFRA_YAML_PATH", "infrastructure.yaml"))
        if infra_path.exists():
            with open(infra_path) as f:
                raw["infrastructure"] = yaml.safe_load(f) or {}

        return cls._build(raw)

    @classmethod
    def _build(cls, raw: dict) -> Config:
        proxmox_raw = raw.get("proxmox") or {}
        nodes_raw = proxmox_raw.get("nodes") or []
        if not nodes_raw:
            print(
                "ERROR: At least one Proxmox node is required. Add one under "
                "'proxmox.nodes:' in beaconmcp.yaml.",
                file=sys.stderr,
            )
            sys.exit(1)

        pve_nodes = [
            PVENode(
                name=_required(n, "name", "proxmox.nodes"),
                host=_required(n, "host", "proxmox.nodes"),
                token_id=_required(n, "token_id", "proxmox.nodes"),
                token_secret=_required(n, "token_secret", "proxmox.nodes"),
            )
            for n in nodes_raw
        ]

        bmc_raw = (raw.get("bmc") or {}).get("devices") or []
        bmc_devices: list[BMCDevice] = []
        seen_ids: set[str] = set()
        for d in bmc_raw:
            device_id = _required(d, "id", "bmc.devices")
            if device_id in seen_ids:
                raise ConfigError(f"Duplicate BMC device id: {device_id!r}")
            seen_ids.add(device_id)
            bmc_devices.append(
                BMCDevice(
                    id=device_id,
                    type=_required(d, "type", f"bmc.devices[{device_id}]"),
                    host=_required(d, "host", f"bmc.devices[{device_id}]"),
                    user=_required(d, "user", f"bmc.devices[{device_id}]"),
                    password=_required(d, "password", f"bmc.devices[{device_id}]"),
                    jump_host=d.get("jump_host"),
                )
            )

        ssh_raw = raw.get("ssh")
        ssh: SSHConfig | None = None
        if ssh_raw:
            ssh = SSHConfig(
                user=_required(ssh_raw, "user", "ssh"),
                password=_required(ssh_raw, "password", "ssh"),
                vmid_to_ip=ssh_raw.get("vmid_to_ip"),
            )

        srv_raw = raw.get("server") or {}
        server = ServerConfig(
            host=srv_raw.get("host", "0.0.0.0"),
            port=int(srv_raw.get("port", 8420)),
            allowed_hosts=list(srv_raw.get("allowed_hosts") or []),
            allowed_origins=list(srv_raw.get("allowed_origins") or []),
            clients_file=Path(
                srv_raw.get("clients_file", "/opt/beaconmcp/clients.json")
            ),
            session_key=srv_raw.get("session_key"),
        )

        feat_raw = raw.get("features") or {}
        dash_raw = feat_raw.get("dashboard") or {}
        limits_raw = dash_raw.get("limits") or {}
        dashboard = DashboardConfig(
            enabled=_bool(dash_raw.get("enabled", True)),
            gemini_api_key=dash_raw.get("gemini_api_key") or "",
            limit_5h_usd=float(limits_raw.get("per_5h_usd", 2.0)),
            limit_week_usd=float(limits_raw.get("per_week_usd", 10.0)),
            public_url=dash_raw.get("public_url"),
            mcp_mode=(dash_raw.get("mcp_mode") or "local").strip().lower(),
        )
        features = FeaturesConfig(
            dashboard=dashboard,
            ssh_enabled=_bool((feat_raw.get("ssh") or {}).get("enabled", True)),
        )

        return cls(
            server=server,
            pve_nodes=pve_nodes,
            bmc_devices=bmc_devices,
            ssh=ssh,
            features=features,
            verify_ssl=_bool(proxmox_raw.get("verify_ssl", False)),
            infrastructure=raw.get("infrastructure") or {},
        )

    # --- Accessors --------------------------------------------------------

    def get_node(self, name: str) -> PVENode | None:
        for node in self.pve_nodes:
            if node.name == name:
                return node
        return None

    def get_node_host(self, name: str) -> str | None:
        """Resolve a node name to a bare hostname suitable for SSH.

        ``proxmox.nodes[].host`` may carry a port (e.g. ``pve1.example.com:8006``
        or ``pve1.example.com:443``) for the Proxmox API, but SSH and
        BMC-over-SSH-tunnel clients always need the bare hostname — asyncssh
        treats the ``host:port`` string as a literal DNS label and fails to
        resolve. Strip the port here so every caller gets a usable value.
        """
        node = self.get_node(name)
        if node is None:
            return None
        return _strip_port(node.host)

    def get_bmc_device(self, device_id: str) -> BMCDevice | None:
        for d in self.bmc_devices:
            if d.id == device_id:
                return d
        return None

    def redacted(self) -> dict:
        """Return a dict suitable for printing/logging with secrets masked."""
        def mask(value: str) -> str:
            if not value:
                return ""
            if len(value) <= 4:
                return "***"
            return value[:2] + "***" + value[-2:]

        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "allowed_hosts": self.server.allowed_hosts,
                "allowed_origins": self.server.allowed_origins,
                "clients_file": str(self.server.clients_file),
                "session_key": mask(self.server.session_key or ""),
            },
            "proxmox": {
                "verify_ssl": self.verify_ssl,
                "nodes": [
                    {
                        "name": n.name,
                        "host": n.host,
                        "token_id": n.token_id,
                        "token_secret": mask(n.token_secret),
                    }
                    for n in self.pve_nodes
                ],
            },
            "bmc": {
                "devices": [
                    {
                        "id": d.id,
                        "type": d.type,
                        "host": d.host,
                        "user": d.user,
                        "password": mask(d.password),
                        "jump_host": d.jump_host,
                    }
                    for d in self.bmc_devices
                ],
            },
            "ssh": (
                {
                    "user": self.ssh.user,
                    "password": mask(self.ssh.password),
                    "vmid_to_ip": self.ssh.vmid_to_ip,
                }
                if self.ssh
                else None
            ),
            "features": {
                "dashboard": {
                    "enabled": self.features.dashboard.enabled,
                    "gemini_api_key": mask(self.features.dashboard.gemini_api_key),
                    "limit_5h_usd": self.features.dashboard.limit_5h_usd,
                    "limit_week_usd": self.features.dashboard.limit_week_usd,
                    "public_url": self.features.dashboard.public_url,
                    "mcp_mode": self.features.dashboard.mcp_mode,
                },
                "ssh_enabled": self.features.ssh_enabled,
            },
            "infrastructure": self.infrastructure,
        }


# --- Helpers ---------------------------------------------------------------


class ConfigError(Exception):
    pass


_ENV_REF = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


def _resolve_env_refs(
    value: Any, *, path: Path, _crumbs: tuple[str, ...] = ()
) -> Any:
    """Walk the loaded YAML and replace ``${VAR}`` placeholders with env values.

    Raises :class:`ConfigError` if a referenced variable is not set, with the
    full dotted path to the offending leaf for debuggability.
    """
    if isinstance(value, dict):
        return {
            k: _resolve_env_refs(v, path=path, _crumbs=_crumbs + (str(k),))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_env_refs(v, path=path, _crumbs=_crumbs + (f"[{i}]",))
            for i, v in enumerate(value)
        ]
    if isinstance(value, str):
        m = _ENV_REF.match(value)
        if m:
            env_name = m.group(1)
            if env_name not in os.environ:
                location = ".".join(_crumbs) or "<root>"
                raise ConfigError(
                    f"{path}: environment variable ${{{env_name}}} referenced "
                    f"at '{location}' is not set."
                )
            return os.environ[env_name]
    return value


def _required(obj: dict, key: str, ctx: str) -> str:
    if key not in obj or obj[key] in (None, ""):
        raise ConfigError(f"{ctx}: missing required field '{key}'.")
    return str(obj[key])


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _strip_port(host: str) -> str:
    """Return ``host`` without a trailing ``:port`` component.

    Handles IPv6 bracket literals (``[::1]:8006`` -> ``[::1]``) and regular
    hostnames (``pve1.example.com:8006`` -> ``pve1.example.com``). Leaves
    the value unchanged when no port is present.
    """
    if not host:
        return host
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[: end + 1]
        return host
    head, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        return head
    return host
