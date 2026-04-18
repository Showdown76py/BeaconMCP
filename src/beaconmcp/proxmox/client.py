from __future__ import annotations

from typing import Any

from proxmoxer import ProxmoxAPI
from requests.exceptions import ConnectionError, Timeout

from ..config import Config


class ProxmoxClient:
    """Manages connections to one or more Proxmox VE nodes via API tokens."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._connections: dict[str, ProxmoxAPI] = {}

    def _get_connection(self, node_name: str) -> ProxmoxAPI:
        if node_name in self._connections:
            return self._connections[node_name]

        pve_node = self._config.get_node(node_name)
        if not pve_node:
            raise NodeNotFoundError(node_name, [n.name for n in self._config.pve_nodes])

        conn = ProxmoxAPI(
            pve_node.host,
            user=pve_node.token_id.split("!")[0],
            token_name=pve_node.token_id.split("!")[1],
            token_value=pve_node.token_secret,
            verify_ssl=self._config.verify_ssl,
        )
        self._connections[node_name] = conn
        return conn

    def api_call(self, node_name: str, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an API call against a Proxmox node.

        Returns the result or a dict with 'error' key on failure.
        """
        try:
            conn = self._get_connection(node_name)
            obj = conn
            for part in path.strip("/").split("/"):
                obj = getattr(obj, part)
            return getattr(obj, method)(**kwargs)
        except NodeNotFoundError:
            raise
        except (ConnectionError, Timeout) as e:
            return {
                "error": f"Node '{node_name}' is unreachable: {e}. "
                "Try ssh_run to access the host directly, "
                "or bmc_health_status if the server may be physically down."
            }
        except Exception as e:
            return {"error": f"Proxmox API error on '{node_name}': {e}"}

    def get(self, node_name: str, path: str, **kwargs: Any) -> Any:
        return self.api_call(node_name, "get", path, **kwargs)

    def post(self, node_name: str, path: str, **kwargs: Any) -> Any:
        return self.api_call(node_name, "post", path, **kwargs)

    def put(self, node_name: str, path: str, **kwargs: Any) -> Any:
        return self.api_call(node_name, "put", path, **kwargs)

    def delete(self, node_name: str, path: str, **kwargs: Any) -> Any:
        return self.api_call(node_name, "delete", path, **kwargs)

    @property
    def configured_nodes(self) -> list[str]:
        return [n.name for n in self._config.pve_nodes]


class NodeNotFoundError(Exception):
    def __init__(self, node: str, available: list[str]) -> None:
        self.node = node
        self.available = available
        super().__init__(
            f"Node '{node}' is not configured. "
            f"Available nodes: {', '.join(available)}. "
            f"Check your .env file."
        )
