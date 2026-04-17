import base64
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon

from .bmc import build_registry as build_bmc_registry
from .bmc import register_bmc_tools
from .config import Config
from .proxmox.client import ProxmoxClient
from .proxmox.monitoring import register_monitoring_tools
from .proxmox.system import register_system_tools
from .proxmox.vms import register_vm_tools
from .security.tools import register_security_tools
from .ssh.client import SSHClient
from .ssh.tools import register_ssh_tools

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


mcp = FastMCP(
    "beaconmcp",
    instructions=(
        "BeaconMCP exposes a Proxmox VE cluster (N nodes), N BMC devices "
        "(HP iLO, IPMI, iDRAC, Supermicro), and an SSH fallback as a single "
        "MCP server. Use proxmox_* tools for VM/CT management and "
        "diagnostics, bmc_* tools for hardware power and health, and ssh_* "
        "tools for direct shell access. Start with proxmox_list_nodes to "
        "see the cluster and bmc_list_devices to see hardware endpoints."
    ),
    website_url="https://github.com/Showdown76py/BeaconMCP",
    icons=_load_icons(),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


@mcp.resource("beaconmcp://infrastructure")
def get_infrastructure() -> str:
    """Infrastructure context: node topology, naming conventions, and access constraints."""
    if not config.infrastructure:
        return "No infrastructure context configured."

    import yaml

    return yaml.dump(config.infrastructure, default_flow_style=False, allow_unicode=True)


@mcp.prompt()
def beaconmcp_context() -> str:
    """Inject infrastructure context into the conversation for Proxmox management tasks."""
    nodes_info = ", ".join(n.name for n in config.pve_nodes)
    if bmc_registry:
        bmc_info = ", ".join(f"{d.id} ({d.type})" for d in config.bmc_devices)
    else:
        bmc_info = "no BMC devices configured"
    ssh_info = "SSH fallback available" if config.ssh else "SSH not configured"

    infra = config.infrastructure
    conventions = ""
    if infra.get("conventions"):
        conventions = "\n".join(f"- {k}: {v}" for k, v in infra["conventions"].items())

    notes = ""
    if infra.get("notes"):
        notes = "\n".join(f"- {n}" for n in infra["notes"])

    return f"""You are managing a Proxmox VE infrastructure with the following topology:

Nodes: {nodes_info}
BMC: {bmc_info}
Access: {ssh_info}

Conventions:
{conventions}

Notes:
{notes}

Diagnostic workflow:
1. Check cluster state with proxmox_list_nodes.
2. For a specific node, use proxmox_node_status.
3. If a node is unreachable via API, try ssh_exec_command on the host.
4. If the host is completely unresponsive, list BMC devices with
   bmc_list_devices and use bmc_health_status / bmc_power_status on the
   matching one.
5. For in-VM issues, prefer proxmox_exec_command (QEMU Guest Agent) or
   ssh_exec_command."""


# Register tool modules
register_monitoring_tools(mcp, proxmox_client)
register_vm_tools(mcp, proxmox_client)
register_system_tools(mcp, proxmox_client)
if config.ssh:
    register_ssh_tools(mcp, ssh_client)
if bmc_registry:
    register_bmc_tools(mcp, bmc_registry)
register_security_tools(mcp)
