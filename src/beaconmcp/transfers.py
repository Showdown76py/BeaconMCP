"""One-shot transfer links: upload into, or download out of, a VM/CT over plain HTTP.

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

``proxmox_download_link`` is the mirror image, see the "One-shot download
links" section below.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

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


# ---------------------------------------------------------------------------
# One-shot download links (proxmox_download_link)
# ---------------------------------------------------------------------------
#
# The mirror image of the upload link: the file is first pulled out of the
# guest into ``server.transfers_dir`` (same code path as
# ``proxmox_download_file``), then handed to whoever holds a single-use
# ``GET /transfers/download/<token>`` URL. The staged copy is deleted once
# it was sent, when the link expires, or on the next start.

#: Path prefix of the download route. The auth middleware lets it through.
DOWNLOAD_PATH_PREFIX = "/transfers/download/"

#: Staged files live in this sub folder of ``server.transfers_dir``. The
#: transit tools only take plain basenames, so they cannot name anything in it.
DOWNLOAD_SUBDIR = ".dlinks"

#: Staged files are named ``dlink-<link_id>``; nothing else is ever swept.
_STAGED_RE = re.compile(r"^dlink-[0-9a-f]{12}(\.part)?$")

#: A leftover is only an orphan once it is older than any live link can be
#: (a link lives at most MAX_TTL, then a transfer may run as long again),
#: plus a margin. Safe whichever process runs the sweep.
ORPHAN_AGE = 2 * MAX_TTL + 2 * 3600

_SEND_CHUNK = 256 * 1024
_MAX_FILENAME = 200


@dataclass
class DownloadLink:
    link_id: str
    token: str = field(repr=False)
    node: str
    vmid: int
    source: str
    filename: str
    staged_path: Path
    size: int
    sha256: str | None
    expires_at: float
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending -> downloading -> success | error | expired
    result: dict[str, Any] | None = None
    finished_at: float | None = None
    _timer: threading.Timer | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """Status view returned to tools. Never includes the token."""
        out: dict[str, Any] = {
            "link_id": self.link_id,
            "status": self.status,
            "node": self.node,
            "vmid": self.vmid,
            "source": self.source,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "expires_at": int(self.expires_at),
        }
        if self.result is not None:
            out["result"] = self.result
        return out


def new_link_id() -> str:
    return uuid.uuid4().hex[:12]


def download_staged_name(link_id: str) -> str:
    return f"dlink-{link_id}"


def sweep_orphan_downloads(transfers_dir: Path, now: float | None = None) -> int:
    """Delete staged download files that no live link can still own.

    Links live in memory, so after a restart no token reaches the files a
    previous process staged. The age test (``ORPHAN_AGE``) keeps this safe
    when another process, e.g. ``beaconmcp check-startup`` next to the running
    server, sweeps the same directory. Only exact ``dlink-<12 hex>[.part]``
    names inside ``DOWNLOAD_SUBDIR`` are touched.
    """
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list((Path(transfers_dir).expanduser() / DOWNLOAD_SUBDIR).iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not _STAGED_RE.match(entry.name):
            continue
        try:
            if entry.is_file() and now - entry.stat().st_mtime > ORPHAN_AGE:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def safe_filename(raw: Any) -> str:
    """A display name that cannot break out of a header or point at a path.

    Directory parts are dropped, control characters removed, quotes and
    backslashes replaced. Never empty, never only dots.
    """
    name = re.split(r"[\\/]", str(raw))[-1]
    name = "".join(
        "_" if c in '"\\' else c
        for c in name
        if ord(c) >= 32 and ord(c) != 127
    )
    name = name.strip().strip(".").strip()[:_MAX_FILENAME]
    return name or "download"


def content_disposition(filename: str) -> str:
    """``attachment`` header: an ASCII fallback, plus RFC 5987 for the rest."""
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
    header = f'attachment; filename="{ascii_name}"'
    if ascii_name != filename:
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header


class DownloadLinkStore:
    """In-memory, thread-safe registry of download links and their staged files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_token: dict[str, DownloadLink] = {}
        self._by_id: dict[str, DownloadLink] = {}

    # -- internals (lock held) ------------------------------------------------

    def _prune(self, now: float) -> None:
        for link_id, link in list(self._by_id.items()):
            if link.status == "pending" and now >= link.expires_at:
                link.status = "expired"
                link.finished_at = now
                self._by_token.pop(link.token, None)
                self._drop_file(link)
            if (
                link.finished_at is not None
                and now - link.finished_at > _RESULT_TTL
            ):
                del self._by_id[link_id]
                self._by_token.pop(link.token, None)

    @staticmethod
    def _drop_file(link: DownloadLink) -> None:
        if link._timer is not None:
            link._timer.cancel()
            link._timer = None
        try:
            link.staged_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _held(self) -> list[DownloadLink]:
        return [
            link for link in self._by_id.values()
            if link.status in ("pending", "downloading")
        ]

    def _check_count(self) -> None:
        pending = sum(1 for link in self._held() if link.status == "pending")
        if pending >= MAX_PENDING_LINKS:
            raise RuntimeError(
                f"Too many pending download links ({pending}). Use or let "
                "some expire before minting more."
            )

    # -- public API -----------------------------------------------------------

    def pending_bytes(self) -> int:
        """Bytes currently staged for links that are pending or being sent."""
        with self._lock:
            self._prune(time.time())
            return sum(link.size for link in self._held())

    def check_capacity(self) -> None:
        """Raise ``RuntimeError`` when no more links may be minted. Cheap:
        called before the file is moved, ``issue`` checks again after."""
        with self._lock:
            self._prune(time.time())
            self._check_count()

    def issue(
        self,
        *,
        node: str,
        vmid: int,
        source: str,
        filename: str,
        staged_path: Path,
        size: int,
        sha256: str | None,
        max_total_bytes: int,
        link_id: str | None = None,
        ttl: int = DEFAULT_TTL,
    ) -> DownloadLink:
        """Register a link for a file already staged at ``staged_path``.

        On ``RuntimeError`` the caller still owns (and must delete) the file.
        """
        ttl = max(MIN_TTL, min(int(ttl), MAX_TTL))
        now = time.time()
        with self._lock:
            self._prune(now)
            self._check_count()
            held = sum(link.size for link in self._held())
            if held + size > max_total_bytes:
                raise RuntimeError(
                    f"Pending download links already hold {held} staged bytes; "
                    f"{size} more would exceed the {max_total_bytes} byte cap "
                    "(server.transfers_max_mb counts every staged file). Use "
                    "or let some expire before minting more."
                )
            link = DownloadLink(
                link_id=link_id or new_link_id(),
                token=secrets.token_urlsafe(32),
                node=node,
                vmid=vmid,
                source=source,
                filename=filename,
                staged_path=staged_path,
                size=size,
                sha256=sha256,
                expires_at=now + ttl,
            )
            # No request may ever come: the timer drops the staged file at
            # expiry all the same.
            timer = threading.Timer(ttl + 1, self.sweep)
            timer.daemon = True
            timer.start()
            link._timer = timer
            self._by_token[link.token] = link
            self._by_id[link.link_id] = link
            return link

    def peek(self, token: str) -> DownloadLink | None:
        """The pending link behind ``token`` without spending it (HEAD)."""
        with self._lock:
            self._prune(time.time())
            link = self._by_token.get(token)
            if link is None or link.status != "pending":
                return None
            return link

    def claim(self, token: str) -> DownloadLink | None:
        """Spend ``token``: return its link and mark it in use, at most once."""
        with self._lock:
            self._prune(time.time())
            link = self._by_token.pop(token, None)
            if link is None or link.status != "pending":
                return None
            link.status = "downloading"
            if link._timer is not None:
                link._timer.cancel()
                link._timer = None
            return link

    def finish(self, link: DownloadLink, result: dict[str, Any]) -> None:
        with self._lock:
            link.status = "success" if result.get("status") == "success" else "error"
            link.result = result
            link.finished_at = time.time()
            self._drop_file(link)

    def sweep(self) -> None:
        """Expire links and delete their files (also run by the link's timer)."""
        with self._lock:
            self._prune(time.time())

    def get(self, link_id: str) -> DownloadLink | None:
        with self._lock:
            self._prune(time.time())
            return self._by_id.get(link_id)


#: Every verb is routed to the endpoint so that a wrong one gets a 405 there,
#: not the 404 of the catch-all mount behind it.
DOWNLOAD_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

_download_store = DownloadLinkStore()


def get_download_store() -> DownloadLinkStore:
    return _download_store


def _download_headers(link: DownloadLink, length: int) -> dict[str, str]:
    return {
        "content-type": "application/octet-stream",
        "content-disposition": content_disposition(link.filename),
        "content-length": str(length),
        "x-content-type-options": "nosniff",
        "cache-control": "no-store",
    }


class _SettlingResponse(StreamingResponse):
    """Streams a file and settles its link once the response is over.

    ``settle`` runs whether the body went out, the client hung up or the
    request was cancelled, so the link never stays "downloading" and the
    staged file never outlives the transfer.
    """

    def __init__(self, *args: Any, settle: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._settle = settle

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._settle()


def make_download_endpoint(trusted_proxies: tuple[str, ...] = ()):
    """Build the ``GET|HEAD /transfers/download/{token}`` Starlette endpoint."""

    def not_found() -> Response:
        # One answer for unknown, spent and expired tokens: no detail.
        return Response(
            "Not found\n", status_code=404, media_type="text/plain",
            headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
        )

    async def download_endpoint(request: Request) -> Response:
        if request.method not in ("GET", "HEAD"):
            # Never touches the token: only a GET (or HEAD) concerns it.
            return Response(
                status_code=405,
                headers={"allow": "GET, HEAD", "cache-control": "no-store"},
            )
        token = request.path_params.get("token", "")
        if request.method == "HEAD":
            # Headers only; the token stays valid.
            peeked = _download_store.peek(token) if token else None
            if peeked is None:
                return not_found()
            return Response(status_code=200, headers=_download_headers(peeked, peeked.size))

        link = _download_store.claim(token) if token else None
        if link is None:
            return not_found()

        ip = client_ip(request, trusted_proxies)
        state = {"sent": 0, "complete": False}
        fh = None
        try:
            fh = open(link.staged_path, "rb")  # noqa: SIM115 -- closed by settle()
            length = os.fstat(fh.fileno()).st_size
        except OSError:
            length = -1

        def settle() -> None:
            if fh is not None:
                fh.close()
            if length >= 0 and state["complete"] and state["sent"] == length:
                result: dict[str, Any] = {"status": "success"}
            elif length < 0:
                result = _error("Staged file is gone.")
            else:
                result = _error("Download interrupted before the whole file was sent.")
            result["bytes"] = state["sent"]
            result["link_id"] = link.link_id
            _download_store.finish(link, result)
            audit.emit(
                "transfer.download",
                link_id=link.link_id,
                node=link.node,
                vmid=link.vmid,
                source=link.source,
                status=result.get("status"),
                bytes=result.get("bytes"),
                ip=ip,
            )

        if fh is None:
            settle()
            return not_found()

        async def body():
            while True:
                chunk = await anyio.to_thread.run_sync(fh.read, _SEND_CHUNK)
                if not chunk:
                    break
                state["sent"] += len(chunk)
                yield chunk
            state["complete"] = True

        return _SettlingResponse(
            body(), status_code=200, headers=_download_headers(link, length),
            settle=settle,
        )

    return download_endpoint
