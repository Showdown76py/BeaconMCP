"""Proxmox TLS verification with a CA path, and SSH host-key trust on first use."""

from __future__ import annotations

import textwrap
from pathlib import Path

import asyncssh
import pytest

from beaconmcp.config import Config, ConfigError
from beaconmcp.ssh.client import (
    HostKeyChangedError,
    HostKeyPolicy,
    _connect_to_host,
    host_key_policy,
    tofu_known_hosts_path,
)


def _load(tmp_path: Path, body: str) -> Config:
    path = tmp_path / "beaconmcp.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            version: 1
            server:
              clients_file: {tmp_path}/clients.json
            """
        ).lstrip()
        + textwrap.dedent(body).lstrip()
    )
    return Config.load(config_path=path)


def _pve(verify: str) -> str:
    return f"""
    proxmox:
      verify_ssl: {verify}
      nodes:
        - name: pve1
          host: 10.0.0.1
          token_id: "root@pam!t"
          token_secret: s
    """


# --- proxmox.verify_ssl ----------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("false", False), ("true", True), ('"no"', False), ('"yes"', True)],
)
def test_verify_ssl_booleans(tmp_path, raw, expected) -> None:
    assert _load(tmp_path, _pve(raw)).verify_ssl is expected


def test_verify_ssl_accepts_a_ca_bundle_path(tmp_path) -> None:
    ca = tmp_path / "pve-root-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    assert _load(tmp_path, _pve(str(ca))).verify_ssl == str(ca)


def test_verify_ssl_rejects_a_missing_path(tmp_path) -> None:
    # Used to become False without a word.
    with pytest.raises(ConfigError, match="verify_ssl"):
        _load(tmp_path, _pve("/etc/pve/pve-root-ca.pem.typo"))


# --- SSH host-key policy ---------------------------------------------------


def _ssh(global_lines: str = "", host_lines: str = "") -> str:
    return f"""
    ssh:
      {global_lines}
      hosts:
        - name: h
          host: 10.0.0.9
          user: root
          password: pw
          {host_lines}
    """


@pytest.mark.parametrize(
    "global_lines, host_lines, mode",
    [
        ("", "", "tofu"),
        ("strict_host_key_checking: true", "", "system"),
        ("strict_host_key_checking: false", "", "any"),
        ("known_hosts: /etc/kh", "", "file"),
        # Per-host values win over the global ones...
        ("strict_host_key_checking: false", "strict_host_key_checking: true", "system"),
        ("", "strict_host_key_checking: false", "any"),
        ("", "known_hosts: /etc/kh2", "file"),
        # ...but a known_hosts file, once set, is never loosened.
        ("known_hosts: /etc/kh", "strict_host_key_checking: false", "file"),
    ],
)
def test_host_key_policy(tmp_path, global_lines, host_lines, mode) -> None:
    cfg = _load(tmp_path, _ssh(global_lines, host_lines))
    policy = host_key_policy(cfg, cfg.get_ssh_host("h"))
    assert policy.mode == mode
    if mode == "tofu":
        assert policy.path == str(tmp_path / "ssh_known_hosts")


def test_tofu_file_follows_clients_file_env(tmp_path, monkeypatch) -> None:
    cfg = _load(tmp_path, _ssh())
    monkeypatch.setenv("BEACONMCP_CLIENTS_FILE", str(tmp_path / "elsewhere" / "c.json"))
    assert tofu_known_hosts_path(cfg) == tmp_path / "elsewhere" / "ssh_known_hosts"


# --- TOFU against a real SSH server ----------------------------------------


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return password == "pw"


async def _serve(host_key, port: int = 0):
    return await asyncssh.listen(
        "127.0.0.1", port, server_host_keys=[host_key],
        server_factory=_Server, process_factory=lambda p: p.exit(0),
    )


async def test_tofu_records_then_pins_then_refuses_a_changed_key(tmp_path) -> None:
    cfg = _load(tmp_path, _ssh())
    key1 = asyncssh.generate_private_key("ssh-ed25519")
    server = await _serve(key1)
    port = server.sockets[0].getsockname()[1]
    spec = cfg.get_ssh_host("h")
    spec.host, spec.port = "127.0.0.1", port
    policy = host_key_policy(cfg, spec)
    kh = Path(policy.path)

    # First contact: accepted and recorded, owner-only.
    conn = await _connect_to_host(spec, policy)
    conn.close()
    lines = kh.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"[127.0.0.1]:{port} ssh-ed25519 ")
    assert (kh.stat().st_mode & 0o777) == 0o600

    # Second contact with the same key: accepted, nothing appended.
    conn = await _connect_to_host(spec, policy)
    conn.close()
    assert kh.read_text().splitlines() == lines

    # Same address, new key: refused with an actionable message.
    server.close()
    await server.wait_closed()
    server = await _serve(asyncssh.generate_private_key("ssh-ed25519"), port)
    try:
        with pytest.raises(HostKeyChangedError, match="ssh-keygen -R"):
            await _connect_to_host(spec, policy)
        assert kh.read_text().splitlines() == lines
    finally:
        server.close()
        await server.wait_closed()


async def test_explicit_false_accepts_any_key_without_recording(tmp_path) -> None:
    cfg = _load(tmp_path, _ssh("strict_host_key_checking: false"))
    server = await _serve(asyncssh.generate_private_key("ssh-ed25519"))
    try:
        spec = cfg.get_ssh_host("h")
        spec.host, spec.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        conn = await _connect_to_host(spec, host_key_policy(cfg, spec))
        conn.close()
        assert not (tmp_path / "ssh_known_hosts").exists()
    finally:
        server.close()
        await server.wait_closed()


async def test_known_hosts_file_refuses_unknown_key(tmp_path) -> None:
    kh = tmp_path / "pinned"
    kh.write_text("")
    server = await _serve(asyncssh.generate_private_key("ssh-ed25519"))
    try:
        cfg = _load(tmp_path, _ssh(f"known_hosts: {kh}"))
        spec = cfg.get_ssh_host("h")
        spec.host, spec.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        with pytest.raises(asyncssh.HostKeyNotVerifiable):
            await _connect_to_host(spec, HostKeyPolicy("file", str(kh)))
    finally:
        server.close()
        await server.wait_closed()



# --- the Proxmox CA really verifies ----------------------------------------


def _pve_like_pki(tmp_path: Path, cn: str):
    """A CA shaped like /etc/pve/pve-root-ca.pem (no keyUsage) and a leaf."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(1)
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve1")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key()).serial_number(2)
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_path = tmp_path / f"{cn}-ca.pem"
    ca_path.write_bytes(ca.public_bytes(pem))
    chain = tmp_path / f"{cn}-leaf.pem"
    chain.write_bytes(
        leaf.public_bytes(pem)
        + leaf_key.private_bytes(
            pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )
    )
    return ca_path, chain


@pytest.fixture()
def https_server(tmp_path):
    import http.server
    import ssl
    import threading

    ca, chain = _pve_like_pki(tmp_path, "pve")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"data": {"version": "8"}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(chain)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], ca
    srv.shutdown()


def test_pve_ca_bundle_verifies_and_other_ca_fails(tmp_path, https_server, monkeypatch) -> None:
    import requests

    from beaconmcp.proxmox.client import _CABundleAdapter

    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    port, ca = https_server
    url = f"https://127.0.0.1:{port}/api2/json/version"

    s = requests.Session()
    s.mount("https://", _CABundleAdapter(str(ca)))
    assert s.get(url, verify=str(ca), timeout=5).json()["data"]["version"] == "8"

    other_ca, _ = _pve_like_pki(tmp_path, "other")
    s = requests.Session()
    s.mount("https://", _CABundleAdapter(str(other_ca)))
    with pytest.raises(requests.exceptions.SSLError):
        s.get(url, verify=str(other_ca), timeout=5)


async def test_tofu_parallel_first_contacts_both_accept_the_same_key(tmp_path) -> None:
    """Two connections racing to a new host must not call each other a MITM.

    Both start before the key is recorded; the first pins it, and the second
    then finds that same key on file, which is a match, not a change.
    """
    import asyncio

    cfg = _load(tmp_path, _ssh())
    server = await _serve(asyncssh.generate_private_key("ssh-ed25519"))
    try:
        spec = cfg.get_ssh_host("h")
        spec.host, spec.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        policy = host_key_policy(cfg, spec)
        results = await asyncio.gather(
            *(_connect_to_host(spec, policy) for _ in range(4)), return_exceptions=True,
        )
        for conn in results:
            if not isinstance(conn, BaseException):
                conn.close()
        errors = [r for r in results if isinstance(r, BaseException)]
        assert errors == []
        assert len(Path(policy.path).read_text().splitlines()) == 1
    finally:
        server.close()
        await server.wait_closed()


async def test_tofu_keeps_ports_apart(tmp_path) -> None:
    """A key pinned for port 22 says nothing about another port on that address.

    Port forwards on one NAT address usually lead to different machines;
    asyncssh's matcher would retry ``[host]:port`` as plain ``host``.
    """
    cfg = _load(tmp_path, _ssh())
    server = await _serve(asyncssh.generate_private_key("ssh-ed25519"))
    try:
        spec = cfg.get_ssh_host("h")
        spec.host, spec.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        policy = host_key_policy(cfg, spec)
        other = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh")
        kh = Path(policy.path)
        kh.parent.mkdir(parents=True, exist_ok=True)
        kh.write_text(f"127.0.0.1 {other.decode().strip()}\n")

        conn = await _connect_to_host(spec, policy)
        conn.close()
        assert kh.read_text().splitlines()[1].startswith(f"[127.0.0.1]:{spec.port} ")
    finally:
        server.close()
        await server.wait_closed()
