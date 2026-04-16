import base64
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon

from .config import Config
from .proxmox.client import ProxmoxClient
from .proxmox.monitoring import register_monitoring_tools
from .proxmox.vms import register_vm_tools
from .proxmox.system import register_system_tools
from .ssh.client import SSHClient
from .ssh.tools import register_ssh_tools
from .ilo.client import ILOClient
from .ilo.tools import register_ilo_tools
from .security.tools import register_security_tools

config = Config.from_env()
proxmox_client = ProxmoxClient(config)
ssh_client = SSHClient(config)
ilo_client = ILOClient(config)


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [v.strip() for v in raw.split(",") if v.strip()]


# DNS-rebinding protection: the MCP SDK rejects any Host header that is not
# explicitly allowlisted. The public hostname this server is reverse-proxied
# behind (e.g. mcp.example.com) MUST be set via TARKAMCP_ALLOWED_HOSTS.
_allowed_hosts = _csv_env(
    "TARKAMCP_ALLOWED_HOSTS",
    ["127.0.0.1:*", "localhost:*", "[::1]:*"],
)
_allowed_origins = _csv_env(
    "TARKAMCP_ALLOWED_ORIGINS",
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
    "tarkamcp",
    instructions=(
        "TarkaMCP provides tools to manage a Proxmox VE infrastructure. "
        "Use proxmox_* tools for VM/CT management and diagnostics, "
        "ilo_* tools for hardware management (power, health), "
        "and ssh_* tools for direct shell access as fallback. "
        "Start with proxmox_list_nodes to see cluster status."
    ),
    website_url="https://github.com/Showdown76py/TarkaMCP",
    icons=_load_icons(),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


@mcp.resource("tarkamcp://infrastructure")
def get_infrastructure() -> str:
    """Infrastructure context: node topology, naming conventions, and access constraints."""
    if not config.infrastructure:
        return "No infrastructure.yaml configured."

    import yaml

    return yaml.dump(config.infrastructure, default_flow_style=False, allow_unicode=True)


@mcp.prompt()
def tarkamcp_context() -> str:
    """Inject infrastructure context into the conversation for Proxmox management tasks."""
    nodes_info = ", ".join(n.name for n in config.pve_nodes)
    ilo_info = "iLO available (via SSH tunnel through pve1)" if config.ilo else "iLO not configured"
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
Hardware: {ilo_info}
Access: {ssh_info}

Conventions:
{conventions}

Notes:
{notes}

Diagnostic workflow:
1. Check cluster status with proxmox_list_nodes
2. For a specific node, use proxmox_node_status
3. If a node is unreachable via API, try ssh_exec_command on the host
4. If the host is completely unresponsive, use ilo_health_status and ilo_power_status
5. For in-VM issues, use proxmox_exec_command (QEMU Guest Agent) or ssh_exec_command"""


# Register tool modules
register_monitoring_tools(mcp, proxmox_client)
register_vm_tools(mcp, proxmox_client)
register_system_tools(mcp, proxmox_client)
if config.ssh:
    register_ssh_tools(mcp, ssh_client)
if config.ilo:
    register_ilo_tools(mcp, ilo_client)
register_security_tools(mcp)
