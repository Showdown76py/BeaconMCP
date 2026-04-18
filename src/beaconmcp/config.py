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
class SSHHost:
    """One SSH target declared under ``ssh.hosts[]`` in beaconmcp.yaml.

    Each host carries its own connection parameters (host, port, user) and
    authentication material. Exactly one of ``password`` or ``key_file`` must
    be provided — enforced at load time.
    """

    name: str
    host: str
    user: str
    port: int = 22
    password: str | None = None
    key_file: str | None = None


@dataclass
class SSHDefaults:
    """Default credentials applied by ``ssh.inherit_proxmox_nodes``.

    When :attr:`SSHConfig.inherit_proxmox_nodes` is true, each declared
    Proxmox node that isn't already covered by an explicit ``ssh.hosts[]``
    entry gets one synthesized from the node's address + these defaults.
    Exactly one of ``password`` or ``key_file`` is required — same rule as
    an explicit host entry.
    """

    user: str
    port: int = 22
    password: str | None = None
    key_file: str | None = None


@dataclass
class SSHConfig:
    hosts: list[SSHHost] = field(default_factory=list)
    # Template for resolving bare numeric IDs (VMIDs) to IP addresses,
    # e.g. "192.168.1.{id}". None disables the numeric-ID fallback.
    # The resolved IP must match the ``host`` field of one of ``hosts``;
    # otherwise the SSH client returns an actionable error.
    vmid_to_ip: str | None = None
    # Default SSH credentials, consumed when ``inherit_proxmox_nodes`` is on.
    defaults: SSHDefaults | None = None
    # When true, synthesize an ``ssh.hosts[]`` entry for every declared
    # Proxmox node that isn't already covered by an explicit ``hosts[]``
    # entry. Synthesized entries inherit address from ``proxmox.nodes[].host``
    # and credentials from ``ssh.defaults``. Restores the pre-2.0 ergonomic
    # where a single credential block covered every Proxmox node.
    inherit_proxmox_nodes: bool = False


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8420
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    clients_file: Path = Path("/opt/beaconmcp/clients.json")
    session_key: str | None = None
    # Enables the OAuth Dynamic Client Registration path used by clients
    # that cannot accept a pre-provisioned client_id/client_secret pair
    # (notably ChatGPT). Still gated by a single-use bootstrap slug minted
    # from the dashboard — a human with a TOTP mints every slug, and each
    # slug can only register one client. Off by default.
    allow_dynamic_registration: bool = False


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
            # BMC jump-host tunneling now requires an ssh.hosts[] entry
            # (credentials live there). The legacy env path can no longer
            # synthesize one, so emit BMC without jump_host and warn if the
            # user had configured ILO_JUMP_HOST.
            if os.environ.get("ILO_JUMP_HOST"):
                warnings.warn(
                    "ILO_JUMP_HOST is no longer honored via env vars: the "
                    "jump-host now references an ssh.hosts[] entry whose "
                    "credentials must come from beaconmcp.yaml. iLO will "
                    "be contacted directly; migrate to YAML to restore "
                    "tunneling.",
                    DeprecationWarning,
                    stacklevel=3,
                )
            raw["bmc"] = {
                "devices": [
                    {
                        "id": "ilo",
                        "type": "hp_ilo",
                        "host": ilo_host,
                        "user": os.environ.get("ILO_USER", ""),
                        "password": os.environ.get("ILO_PASSWORD", ""),
                    }
                ]
            }

        ssh_user = os.environ.get("SSH_USER", "")
        ssh_pw = os.environ.get("SSH_PASSWORD", "")
        if ssh_user and ssh_pw:
            warnings.warn(
                "SSH credentials in environment variables (SSH_USER / "
                "SSH_PASSWORD) are no longer supported: the new multi-host "
                "model requires hosts to be declared under 'ssh.hosts:' in "
                "beaconmcp.yaml. SSH tools will NOT be registered in this "
                "legacy-env session. Migrate to the YAML config.",
                DeprecationWarning,
                stacklevel=3,
            )

        infra_path = Path(os.environ.get("INFRA_YAML_PATH", "infrastructure.yaml"))
        if infra_path.exists():
            with open(infra_path) as f:
                raw["infrastructure"] = yaml.safe_load(f) or {}

        return cls._build(raw)

    @classmethod
    def _build(cls, raw: dict) -> Config:
        proxmox_raw = raw.get("proxmox") or {}
        nodes_raw = proxmox_raw.get("nodes") or []

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
            # Reject the legacy single-credential shape explicitly so existing
            # deployments get a clear migration pointer instead of a confusing
            # "missing field" error.
            if (
                "hosts" not in ssh_raw
                and "defaults" not in ssh_raw
                and ("user" in ssh_raw or "password" in ssh_raw)
            ):
                raise ConfigError(
                    "ssh: legacy shape with top-level 'user'/'password' is no "
                    "longer supported. Migrate to 'ssh.hosts:' + optional "
                    "'ssh.defaults:' + 'ssh.inherit_proxmox_nodes:' — see "
                    "beaconmcp.yaml.example."
                )

            # Optional default credentials, consumed when inheritance is on.
            defaults: SSHDefaults | None = None
            defaults_raw = ssh_raw.get("defaults")
            if defaults_raw:
                if not isinstance(defaults_raw, dict):
                    raise ConfigError("ssh.defaults: must be a mapping.")
                d_password = defaults_raw.get("password") or None
                d_key_file = defaults_raw.get("key_file") or None
                if bool(d_password) == bool(d_key_file):
                    raise ConfigError(
                        "ssh.defaults: provide exactly one of 'password' or "
                        "'key_file' (got "
                        + ("both" if d_password and d_key_file else "neither")
                        + ")."
                    )
                defaults = SSHDefaults(
                    user=_required(defaults_raw, "user", "ssh.defaults"),
                    port=int(defaults_raw.get("port", 22)),
                    password=d_password,
                    key_file=d_key_file,
                )

            inherit_flag = _bool(ssh_raw.get("inherit_proxmox_nodes", False))
            if inherit_flag and defaults is None:
                raise ConfigError(
                    "ssh.inherit_proxmox_nodes: requires 'ssh.defaults:' with "
                    "user + password/key_file — the synthesized entries need "
                    "credentials to authenticate."
                )

            hosts_raw = ssh_raw.get("hosts") or []
            if not isinstance(hosts_raw, list):
                raise ConfigError("ssh.hosts: must be a list of mappings.")
            # An 'ssh:' section with neither 'hosts' nor 'inherit_proxmox_nodes'
            # would load into an empty SSH config -- equivalent to no SSH at
            # all. Be explicit so the user notices the mis-config early.
            if not hosts_raw and not inherit_flag:
                raise ConfigError(
                    "ssh: either declare at least one 'ssh.hosts[]' entry or "
                    "set 'ssh.inherit_proxmox_nodes: true' (with 'ssh.defaults'). "
                    "Remove the 'ssh:' section entirely to disable SSH."
                )

            ssh_hosts: list[SSHHost] = []
            seen_names: set[str] = set()
            seen_addresses: set[str] = set()
            for h in hosts_raw:
                if not isinstance(h, dict):
                    raise ConfigError(
                        "ssh.hosts[]: each entry must be a mapping."
                    )
                host_name = _required(h, "name", "ssh.hosts")
                if host_name in seen_names:
                    raise ConfigError(
                        f"ssh.hosts: duplicate name {host_name!r}."
                    )
                seen_names.add(host_name)
                password = h.get("password") or None
                key_file = h.get("key_file") or None
                if bool(password) == bool(key_file):
                    raise ConfigError(
                        f"ssh.hosts[{host_name}]: provide exactly one of "
                        "'password' or 'key_file' (got "
                        + ("both" if password and key_file else "neither")
                        + ")."
                    )
                host_addr = _required(h, "host", f"ssh.hosts[{host_name}]")
                seen_addresses.add(host_addr)
                ssh_hosts.append(
                    SSHHost(
                        name=host_name,
                        host=host_addr,
                        user=_required(h, "user", f"ssh.hosts[{host_name}]"),
                        port=int(h.get("port", 22)),
                        password=password,
                        key_file=key_file,
                    )
                )

            # Synthesize ssh.hosts[] entries for Proxmox nodes that aren't
            # already covered by an explicit declaration. Skip a node when it
            # already matches an explicit entry by name OR by address, so an
            # operator who wants different creds for a specific node just
            # declares it explicitly and the inheritance leaves it alone.
            if inherit_flag and defaults is not None:
                for node in pve_nodes:
                    if node.name in seen_names or node.host in seen_addresses:
                        continue
                    ssh_hosts.append(
                        SSHHost(
                            name=node.name,
                            host=node.host,
                            user=defaults.user,
                            port=defaults.port,
                            password=defaults.password,
                            key_file=defaults.key_file,
                        )
                    )
                    seen_names.add(node.name)
                    seen_addresses.add(node.host)

            ssh = SSHConfig(
                hosts=ssh_hosts,
                vmid_to_ip=ssh_raw.get("vmid_to_ip"),
                defaults=defaults,
                inherit_proxmox_nodes=inherit_flag,
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
            allow_dynamic_registration=_bool(
                srv_raw.get("allow_dynamic_registration", False)
            ),
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

        # Cross-capability validation ----------------------------------------

        # Require at least one capability to be configured. An empty server
        # would only expose security_end_session, which is useless in isolation.
        if not pve_nodes and not bmc_devices and not (ssh and ssh.hosts):
            raise ConfigError(
                "at least one capability must be configured: add entries "
                "under 'proxmox.nodes:', 'ssh.hosts:', or 'bmc.devices:' "
                "in beaconmcp.yaml."
            )

        # SSH host names and Proxmox node names are allowed to match: the
        # two live in separate tool namespaces (``ssh_*`` vs ``proxmox_*``)
        # so there is no routing ambiguity. Letting them match removes the
        # need for synthetic suffixes like ``pve1-ssh`` and lets
        # ``ssh.inherit_proxmox_nodes`` auto-declare hosts cleanly.

        # BMC jump_host now references ssh.hosts[].name (was proxmox.nodes[].name
        # in pre-2.0 shape). Validate the reference exists so the user gets a
        # load-time error instead of a runtime BMCTunnelError at first use.
        ssh_host_names = {h.name for h in ssh.hosts} if ssh else set()
        for d in bmc_devices:
            if d.jump_host and d.jump_host not in ssh_host_names:
                raise ConfigError(
                    f"bmc.devices[{d.id}].jump_host={d.jump_host!r} does not "
                    "match any ssh.hosts[].name. Declare the jump host under "
                    "ssh.hosts (with user and password/key_file) so BMC "
                    "tunneling can authenticate."
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

    def get_ssh_host(self, name: str) -> SSHHost | None:
        """Look up an SSH host by declared name (``ssh.hosts[].name``).

        Returns ``None`` when SSH is disabled or no host matches.
        """
        if not self.ssh:
            return None
        for h in self.ssh.hosts:
            if h.name == name:
                return h
        return None

    def get_ssh_host_by_address(self, address: str) -> SSHHost | None:
        """Look up an SSH host by its declared ``host`` field (IP/hostname).

        Used by the SSH client's numeric-VMID resolver: the template is
        applied to produce an IP, then that IP is matched against declared
        hosts to discover which credentials to use. Returns ``None`` when
        no host has that address (caller surfaces an actionable error).
        """
        if not self.ssh:
            return None
        for h in self.ssh.hosts:
            if h.host == address:
                return h
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
                "allow_dynamic_registration": self.server.allow_dynamic_registration,
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
                    "vmid_to_ip": self.ssh.vmid_to_ip,
                    "hosts": [
                        {
                            "name": h.name,
                            "host": h.host,
                            "port": h.port,
                            "user": h.user,
                            "password": mask(h.password) if h.password else None,
                            "key_file": h.key_file,
                        }
                        for h in self.ssh.hosts
                    ],
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
            location = ".".join(_crumbs) or "<root>"
            if env_name not in os.environ:
                raise ConfigError(
                    f"{path}: environment variable ${{{env_name}}} referenced "
                    f"at '{location}' is not set."
                )
            resolved = os.environ[env_name]
            # Reject empty values here rather than letting _required() later
            # blame the YAML ("missing required field") — the YAML is fine,
            # the .env placeholder is just unfilled.
            if resolved == "":
                raise ConfigError(
                    f"{path}: environment variable ${{{env_name}}} referenced "
                    f"at '{location}' is set but empty. Fill in a value in "
                    f"your .env (or unset the variable to get a clearer error)."
                )
            return resolved
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
