from __future__ import annotations

import logging
import time
from typing import Any

from proxmoxer import ProxmoxAPI
from requests.exceptions import ConnectionError, Timeout

from ..config import Config

_logger = logging.getLogger("beaconmcp.proxmox")

# Transient-error retry: Proxmox API over the wire frequently hiccups on
# momentary network blips (TCP reset during cluster sync, TLS renegotiation
# behind a reverse proxy, etc). One quick retry with a short backoff covers
# the overwhelming majority without turning sustained outages into slow
# failures. Keep the numbers small and obvious -- callers already get a
# descriptive error dict back if retries don't help.
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.5


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

        Returns the result or a dict with 'error' key on failure. Transient
        network errors get one quick retry; sustained unreachability returns
        the descriptive error message.
        """
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                conn = self._get_connection(node_name)
                obj = conn
                for part in path.strip("/").split("/"):
                    obj = getattr(obj, part)
                return getattr(obj, method)(**kwargs)
            except NodeNotFoundError:
                raise
            except (ConnectionError, Timeout) as e:
                last_exc = e
                # Drop the cached connection so the retry rebuilds TLS state
                # rather than re-using a half-broken socket.
                self._connections.pop(node_name, None)
                if attempt + 1 < _RETRY_ATTEMPTS:
                    _logger.warning(
                        "transient error on %s %s (attempt %d/%d): %s",
                        node_name, path, attempt + 1, _RETRY_ATTEMPTS, e,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                return {
                    "error": f"Node '{node_name}' is unreachable: {e}. "
                    "Try ssh_run to access the host directly, "
                    "or bmc_health_status if the server may be physically down."
                }
            except Exception as e:
                return {"error": f"Proxmox API error on '{node_name}': {e}"}
        # Defensive fallback -- loop should always return above.
        return {"error": f"Node '{node_name}' is unreachable: {last_exc}"}

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
