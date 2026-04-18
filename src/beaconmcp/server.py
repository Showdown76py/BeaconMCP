import base64
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon

from .bmc import build_registry as build_bmc_registry
from .bmc import register_bmc_tools
from .config import Config
from .proxmox.aggregators import register_aggregator_tools
from .proxmox.client import ProxmoxClient
from .proxmox.monitoring import register_monitoring_tools
from .proxmox.system import register_system_tools
from .proxmox.vms import register_vm_tools
from .security.tools import register_security_tools
from .ssh.client import SSHClient
from .ssh.tools import register_ssh_tools

from functools import wraps
import time
from .metrics import tool_calls, tool_latency_ms

config = Config.load()
proxmox_client = ProxmoxClient(config)
ssh_client = SSHClient(config)
bmc_registry = build_bmc_registry(config)


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [v.strip() for v in raw.split(",") if v.strip()]


# DNS-rebinding protection: the MCP SDK rejects any Host header that is not
# explicitly allowlisted. The public hostname behind the reverse proxy MUST
# appear either in ``server.allowed_hosts`` in beaconmcp.yaml or in the
# legacy BEACONMCP_ALLOWED_HOSTS env var.
_allowed_hosts = config.server.allowed_hosts or _csv_env(
    "BEACONMCP_ALLOWED_HOSTS",
    ["127.0.0.1:*", "localhost:*", "[::1]:*"],
)
_allowed_origins = config.server.allowed_origins or _csv_env(
    "BEACONMCP_ALLOWED_ORIGINS",
    ["https://claude.ai", "https://chat.openai.com", "https://gemini.google.com"],
)


def _load_icons() -> list[Icon]:
    """Load the bundled logo as a data-URL icon for MCP clients.

    Shipped inline so clients get the icon without needing a public static
    route, and so nothing breaks when the server is hidden behind a tunnel
    that only forwards /mcp.
    """
    logo_path = Path(__file__).parent / "assets" / "logo.webp"
    if not logo_path.is_file():
        return []
    data = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return [
        Icon(
            src=f"data:image/webp;base64,{data}",
            mimeType="image/webp",
            sizes=["512x512"],
        )
    ]


def _build_instructions() -> str:
    """Assemble the MCP server greeting from whatever capabilities are enabled.

    Each capability contributes its own blurb so a server with only SSH (e.g.
    a couple of VPS) doesn't advertise Proxmox tools it can't expose, and a
    Proxmox-only server doesn't pretend to have an SSH fallback. The greeting
    is the first thing a client model reads — keeping it truthful is what
    makes the conditional registration useful.
    """
    blurbs: list[str] = []
    entry: list[str] = []
    if config.pve_nodes:
        node_count = len(config.pve_nodes)
        blurbs.append(
            f"a Proxmox VE cluster ({node_count} node"
            f"{'s' if node_count > 1 else ''}) via proxmox_* tools"
        )
        entry.append("proxmox_list_nodes to see the cluster")
    if config.ssh and config.ssh.hosts:
        host_count = len(config.ssh.hosts)
        blurbs.append(
            f"{host_count} SSH host{'s' if host_count > 1 else ''} via "
            "ssh_* tools for direct shell access"
        )
        entry.append("ssh_list_sessions to track running commands")
    if bmc_registry:
        dev_count = len(bmc_registry)
        blurbs.append(
            f"{dev_count} BMC device{'s' if dev_count > 1 else ''} "
            "(HP iLO / IPMI / iDRAC / Supermicro) via bmc_* tools for "
            "hardware power and health"
        )
        entry.append("bmc_list_devices to see hardware endpoints")

    if not blurbs:
        # Defensive: the config loader refuses to start with no capability.
        return "BeaconMCP running with no capabilities configured."

    body = "BeaconMCP exposes " + "; ".join(blurbs) + "."
    entry_line = " Start with " + ", or ".join(entry) + "." if entry else ""
    return body + entry_line


mcp = FastMCP(
    "beaconmcp",
    instructions=_build_instructions(),
    website_url="https://github.com/Showdown76py/BeaconMCP",
    icons=_load_icons(),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


# Wrap mcp.tool to inject metrics tracking
_orig_tool = mcp.tool
def _metric_tool(*args, **kwargs):
    def decorator(func):
        tool_name = func.__name__
        @wraps(func)
        async def async_wrapper(*f_args, **f_kwargs):
            start = time.monotonic()
            status = "ok"
            try:
                return await func(*f_args, **f_kwargs)
            except Exception:
                status = "error"
                raise
            finally:
                latency = (time.monotonic() - start) * 1000
                tool_calls.inc(tool=tool_name, status=status)
                tool_latency_ms.observe(latency, tool=tool_name)
        
        @wraps(func)
        def sync_wrapper(*f_args, **f_kwargs):
            start = time.monotonic()
            status = "ok"
            try:
                return func(*f_args, **f_kwargs)
            except Exception:
                status = "error"
                raise
            finally:
                latency = (time.monotonic() - start) * 1000
                tool_calls.inc(tool=tool_name, status=status)
                tool_latency_ms.observe(latency, tool=tool_name)
                
        
        import inspect
        if inspect.iscoroutinefunction(func):
            wrapped = async_wrapper
        else:
            wrapped = sync_wrapper

        return _orig_tool(*args, **kwargs)(wrapped)
    return decorator
mcp.tool = _metric_tool


@mcp.resource("beaconmcp://infrastructure")
def get_infrastructure() -> str:
    """Infrastructure context: node topology, naming conventions, and access constraints."""
    if not config.infrastructure:
        return "No infrastructure context configured."

    import yaml

    return yaml.dump(config.infrastructure, default_flow_style=False, allow_unicode=True)


@mcp.prompt()
def beaconmcp_context() -> str:
    """Inject infrastructure context: topology and capability-aware diagnostic workflow."""
    topology_lines: list[str] = []
    if config.pve_nodes:
        topology_lines.append(
            "Proxmox nodes: " + ", ".join(n.name for n in config.pve_nodes)
        )
    if config.ssh and config.ssh.hosts:
        topology_lines.append(
            "SSH hosts: " + ", ".join(h.name for h in config.ssh.hosts)
        )
    if bmc_registry:
        topology_lines.append(
            "BMC devices: "
            + ", ".join(f"{d.id} ({d.type})" for d in config.bmc_devices)
        )
    topology = "\n".join(topology_lines) if topology_lines else "(no capabilities)"

    # Build a diagnostic workflow that only references tools that are
    # actually registered. A VPS-only deployment gets a one-step workflow
    # and no Proxmox/BMC references, which stops the model from suggesting
    # tool calls that would 404.
    steps: list[str] = []
    if config.pve_nodes:
        steps.append(
            "Start with cluster_overview for the whole cluster in one call, "
            "or cluster_health(node=...) for node metrics + BMC + recent errors."
        )
        steps.append(
            "Drill in with proxmox_node_status / proxmox_list_vms as needed. "
            "Pass fields=[...] on detail tools to trim the response."
        )
        steps.append(
            "Find a VM by name with vm_find('web-*'); act on many at once with "
            "vm_bulk_action(vmids=[...], action='stop')."
        )
    if config.pve_nodes and config.ssh and config.ssh.hosts:
        steps.append(
            "If a Proxmox node is unreachable via API, try ssh_run against "
            "the matching ssh.hosts entry."
        )
    if bmc_registry:
        steps.append(
            "If a host is completely unresponsive, cluster_health already "
            "includes BMC facts; otherwise use bmc_list_devices + "
            "bmc_health_status / bmc_power_status."
        )
    if config.pve_nodes and config.ssh and config.ssh.hosts:
        steps.append(
            "For in-VM issues, prefer proxmox_run (QEMU Guest Agent) or ssh_run. "
            "Both auto-switch to async on timeout and accept exec_id for polling."
        )
    elif config.pve_nodes:
        steps.append("For in-VM issues, use proxmox_run (QEMU Guest Agent).")
    elif config.ssh and config.ssh.hosts:
        steps.append(
            "Use ssh_run on declared hosts. Pass wait=False for long commands; "
            "poll with ssh_run(exec_id=...)."
        )
    workflow = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or "(no workflow: no capabilities configured)"

    infra = config.infrastructure
    conventions = ""
    if infra.get("conventions"):
        conventions = "\n".join(f"- {k}: {v}" for k, v in infra["conventions"].items())

    notes = ""
    if infra.get("notes"):
        notes = "\n".join(f"- {n}" for n in infra["notes"])

    return f"""You are operating a BeaconMCP-managed infrastructure with the following topology:

{topology}

Conventions:
{conventions}

Notes:
{notes}

Diagnostic workflow:
{workflow}"""


# Register tool modules only for the capabilities that are actually
# configured. Each ``if`` gate here is what makes a VPS-only, Proxmox-only,
# or BMC-only deployment possible: tools the server cannot honor stay out
# of the exposed tool list.
if config.pve_nodes:
    register_monitoring_tools(mcp, proxmox_client)
    register_vm_tools(mcp, proxmox_client)
    register_system_tools(mcp, proxmox_client)
    # Aggregators ride on top of the Proxmox client and opportunistically
    # pull BMC facts when the registry is non-empty.
    register_aggregator_tools(mcp, proxmox_client, config, bmc_registry)
if config.ssh and config.ssh.hosts:
    register_ssh_tools(mcp, ssh_client)
if bmc_registry:
    register_bmc_tools(mcp, bmc_registry)
register_security_tools(mcp)
