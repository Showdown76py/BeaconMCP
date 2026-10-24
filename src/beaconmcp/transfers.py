"""One-shot upload links: push a file into a VM/CT over plain HTTP.

MCP tool arguments are JSON, so the only way to hand a binary file to a
tool is to base64 it into the call, which caps out long before a real file
does. Instead, ``proxmox_upload_link`` mints a short-lived capability URL
bound to one ``(node, vmid, dest)`` target. Whoever holds the URL streams
the raw bytes to it (``curl -T file <url>``); the server stages them under
``server.transfers_dir`` and pushes them into the guest with the same code
path as ``proxmox_upload_file``.

The token *is* the credential, so the route sits outside the bearer
middleware (like the slug-scoped DCR endpoints). It is 256 bits of
randomness, single-use (consumed as soon as a request presents it), bound
to one target chosen when the link was minted, and expires after a few
minutes. Links live in memory only: a restart voids every pending link.
"""

from __future__ import annotations

import ipaddress
import secrets
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import audit
from .ratelimit import client_ip

#: Path prefix of the upload route. The auth middleware lets it through.
UPLOAD_PATH_PREFIX = "/transfers/upload/"

#: Link lifetime bounds, in seconds.
DEFAULT_TTL = 600
MIN_TTL = 60
MAX_TTL = 3600

#: Pending links are capped so a looping model can't pile them up.
MAX_PENDING_LINKS = 50

#: Finished uploads are kept this long so ``proxmox_upload_link(link_id=...)``
#: can report the outcome when the HTTP reply got lost (proxy timeout, ...).
_RESULT_TTL = 3600

#: Set by the HTTP auth middleware to the externally visible base URL of the
#: in-flight request, so a tool can build an absolute link without a config
#: entry. Only a fallback: ``features.dashboard.public_url`` wins when set.
current_public_base: ContextVar[str | None] = ContextVar(
    "current_public_base", default=None,
)

#: ``(node, vmid, staged_path, dest, verify_checksum) -> result dict``.
#: Registered by ``register_system_tools`` (it owns the Proxmox + SSH
#: clients); ``None`` when the tools were never registered.
Pusher = Callable[[str, int, Path, str, bool], Awaitable[dict[str, Any]]]


@dataclass
class UploadLink:
    link_id: str
    token: str
    node: str
    vmid: int
    dest: str
    max_bytes: int
    staging_dir: Path
    verify_checksum: bool
    expires_at: float
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending -> uploading -> success | error
    result: dict[str, Any] | None = None
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        """Status view returned to tools. Never includes the token."""
        out: dict[str, Any] = {
            "link_id": self.link_id,
            "status": self.status,
            "node": self.node,
            "vmid": self.vmid,
            "dest": self.dest,
            "expires_at": int(self.expires_at),
        }
        if self.result is not None:
            out["result"] = self.result
        return out


class UploadLinkStore:
    """In-memory, thread-safe registry of upload links."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_token: dict[str, UploadLink] = {}
        self._by_id: dict[str, UploadLink] = {}

    def _prune(self, now: float) -> None:
        for link_id, link in list(self._by_id.items()):
            if link.status == "pending" and now >= link.expires_at:
                link.status = "expired"
                link.finished_at = now
                self._by_token.pop(link.token, None)
            if (
                link.finished_at is not None
                and now - link.finished_at > _RESULT_TTL
            ):
                del self._by_id[link_id]
                self._by_token.pop(link.token, None)

    def issue(
        self,
        *,
        node: str,
        vmid: int,
        dest: str,
        max_bytes: int,
        staging_dir: Path,
        verify_checksum: bool = True,
        ttl: int = DEFAULT_TTL,
    ) -> UploadLink:
        ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))
        now = time.time()
        with self._lock:
            self._prune(now)
            pending = sum(1 for link in self._by_id.values() if link.status == "pending")
            if pending >= MAX_PENDING_LINKS:
                raise RuntimeError(
                    f"Too many pending upload links ({pending}). Use or let "
                    "some expire before minting more."
                )
            link = UploadLink(
                link_id=uuid.uuid4().hex[:12],
                token=secrets.token_urlsafe(32),
                node=node,
                vmid=vmid,
                dest=dest,
                max_bytes=max_bytes,
                staging_dir=staging_dir,
                verify_checksum=verify_checksum,
                expires_at=now + ttl,
            )
            self._by_token[link.token] = link
            self._by_id[link.link_id] = link
            return link

    def claim(self, token: str) -> UploadLink | None:
        """Spend ``token``: return its link and mark it in use, at most once."""
        now = time.time()
        with self._lock:
            self._prune(now)
            link = self._by_token.pop(token, None)
            if link is None or link.status != "pending":
                return None
            link.status = "uploading"
            return link

    def finish(self, link: UploadLink, result: dict[str, Any]) -> None:
        with self._lock:
            link.status = "success" if result.get("status") == "success" else "error"
            link.result = result
            link.finished_at = time.time()

    def get(self, link_id: str) -> UploadLink | None:
        with self._lock:
            self._prune(time.time())
            return self._by_id.get(link_id)


_store = UploadLinkStore()
_pusher: Pusher | None = None


def get_store() -> UploadLinkStore:
    return _store


def register_pusher(pusher: Pusher | None) -> None:
    global _pusher
    _pusher = pusher


def public_base_url(configured: str | None) -> str | None:
    """Base URL for upload links: the configured public URL, else the request's.

    A loopback request base (the dashboard chat's own MCP session runs over
    127.0.0.1) is no use to whoever uploads, so it counts as unknown.
    """
    configured = (configured or "").strip()
    if configured:
        return configured.rstrip("/")
    base = current_public_base.get()
    if not base or _is_loopback_url(base):
        return None
    return base.rstrip("/")


def _is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def staged_name(link: UploadLink) -> str:
    return f"upload-{link.link_id}"


def make_upload_endpoint(trusted_proxies: tuple[str, ...] = ()):
    """Build the ``PUT|POST /transfers/upload/{token}`` Starlette endpoint."""

    async def upload_endpoint(request: Request) -> JSONResponse:
        token = request.path_params.get("token", "")
        link = _store.claim(token) if token else None
        if link is None:
            return JSONResponse(
                {
                    "status": "error",
                    "error": (
                        "Unknown, expired or already used upload link. "
                        "Mint a new one with proxmox_upload_link."
                    ),
                },
                status_code=404,
            )
        # The link is spent: settle it and audit it whatever happens, a
        # request cancelled by a restart included.
        result, status_code = _error("Upload interrupted (server shutting down)."), 500
        try:
            result, status_code = await _receive_and_push(request, link)
        finally:
            _store.finish(link, result)
            audit.emit(
                "transfer.upload",
                link_id=link.link_id,
                node=link.node,
                vmid=link.vmid,
                dest=link.dest,
                status=result.get("status"),
                bytes=result.get("bytes"),
                ip=client_ip(request, trusted_proxies),
            )
        return JSONResponse(result, status_code=status_code)

    return upload_endpoint


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


async def _receive_and_push(request: Request, link: UploadLink) -> tuple[dict[str, Any], int]:
    """Stream the raw body into the staging dir, then push it into the guest."""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        return _error(
            "Send the raw file as the request body (curl -T <file> <url>), "
            "not a multipart form."
        ), 415
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > link.max_bytes:
        return _error(
            f"File is {declared} bytes, over the {link.max_bytes} byte transfer cap."
        ), 413
    if _pusher is None:
        return _error("File transfer tools are not available on this server."), 503

    link.staging_dir.mkdir(parents=True, exist_ok=True)
    staged = link.staging_dir / staged_name(link)
    part = staged.with_name(staged.name + ".part")
    received = 0
    try:
        with open(part, "wb") as fh:
            async for chunk in request.stream():
                received += len(chunk)
                if received > link.max_bytes:
                    return _error(
                        f"Upload exceeded the {link.max_bytes} byte transfer cap."
                    ), 413
                fh.write(chunk)
        if received == 0:
            return _error("Empty request body."), 400
        part.replace(staged)
        result = await _pusher(link.node, link.vmid, staged, link.dest, link.verify_checksum)
    except Exception as exc:  # noqa: BLE001 -- the link must always settle
        result = _error(f"Upload failed: {exc}")
    finally:
        part.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)

    result.setdefault("bytes", received)
    result["link_id"] = link.link_id
    return result, _status_code(result)


def _status_code(result: dict[str, Any]) -> int:
    """HTTP status for a finished transfer.

    Not 502: Cloudflare swaps an origin 502 for its own "error code: 502"
    page, so the uploader would never see why the push failed.
    """
    if result.get("status") == "success":
        return 200
    if "guest_status" in result:
        return 409  # target stopped since the link was minted
    return 500
