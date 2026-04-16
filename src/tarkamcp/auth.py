"""OAuth 2.1 client credentials management for TarkaMCP HTTP mode."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CLIENTS_FILE = Path("/opt/tarkamcp/clients.json")


@dataclass
class Client:
    client_id: str
    client_secret_hash: str
    name: str
    created_at: float


@dataclass
class AccessToken:
    token: str
    client_id: str
    expires_at: float


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ClientStore:
    """Persistent client credential storage backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CLIENTS_FILE
        self._clients: dict[str, Client] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            data = json.loads(self._path.read_text())
            for c in data.get("clients", []):
                self._clients[c["client_id"]] = Client(**c)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "clients": [
                {
                    "client_id": c.client_id,
                    "client_secret_hash": c.client_secret_hash,
                    "name": c.name,
                    "created_at": c.created_at,
                }
                for c in self._clients.values()
            ]
        }
        self._path.write_text(json.dumps(data, indent=2))

    def create(self, name: str) -> tuple[str, str]:
        """Create a new client. Returns (client_id, client_secret)."""
        client_id = "tarkamcp_" + secrets.token_hex(8)
        client_secret = "sk_" + secrets.token_hex(32)

        self._clients[client_id] = Client(
            client_id=client_id,
            client_secret_hash=_hash_secret(client_secret),
            name=name,
            created_at=time.time(),
        )
        self._save()
        return client_id, client_secret

    def verify(self, client_id: str, client_secret: str) -> bool:
        """Verify client credentials."""
        client = self._clients.get(client_id)
        if not client:
            return False
        return client.client_secret_hash == _hash_secret(client_secret)

    def list_clients(self) -> list[dict[str, Any]]:
        """List all registered clients (without secrets)."""
        return [
            {
                "client_id": c.client_id,
                "name": c.name,
                "created_at": c.created_at,
            }
            for c in self._clients.values()
        ]

    def revoke(self, client_id: str) -> bool:
        """Revoke a client. Returns True if found and removed."""
        if client_id in self._clients:
            del self._clients[client_id]
            self._save()
            return True
        return False


class TokenStore:
    """In-memory access token store with expiration."""

    TOKEN_TTL = 3600 * 24  # 24 hours

    def __init__(self) -> None:
        self._tokens: dict[str, AccessToken] = {}

    def issue(self, client_id: str) -> tuple[str, int]:
        """Issue an access token. Returns (token, expires_in)."""
        token = secrets.token_hex(32)
        self._tokens[token] = AccessToken(
            token=token,
            client_id=client_id,
            expires_at=time.time() + self.TOKEN_TTL,
        )
        self._cleanup()
        return token, self.TOKEN_TTL

    def validate(self, token: str) -> str | None:
        """Validate a token. Returns client_id if valid, None otherwise."""
        access_token = self._tokens.get(token)
        if not access_token:
            return None
        if time.time() > access_token.expires_at:
            del self._tokens[token]
            return None
        return access_token.client_id

    def _cleanup(self) -> None:
        now = time.time()
        expired = [t for t, at in self._tokens.items() if now > at.expires_at]
        for t in expired:
            del self._tokens[t]
