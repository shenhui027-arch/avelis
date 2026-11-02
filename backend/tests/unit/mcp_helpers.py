"""真实 TCP/TLS 夹具：不替换 ASGI 传输或 SDK OAuth 实现。"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def make_local_tls(directory: Path) -> tuple[Path, Path]:
    """生成短期本地测试证书，与 OAuth 令牌无关。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Avelis MCP local fixture")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "fixture-cert.pem"
    key_path = directory / "fixture-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


@contextmanager
def tcp_server(
    app_factory: Callable[[str], object], *, tls: tuple[Path, Path] | None = None
) -> Iterator[tuple[str, object]]:
    """先绑定随机端口，再启动 uvicorn；每条退出路径都关闭线程和套接字。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    host, port = sock.getsockname()
    base_url = f"{'https' if tls else 'http'}://{host}:{port}"
    try:
        app = app_factory(base_url)
        config = uvicorn.Config(
            app,
            log_level="error",
            lifespan="on",
            timeout_graceful_shutdown=3,
            ssl_certfile=str(tls[0]) if tls else None,
            ssl_keyfile=str(tls[1]) if tls else None,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Local uvicorn fixture failed to start")
                time.sleep(0.01)
            yield base_url, app
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=3)
            assert not thread.is_alive(), "Local uvicorn fixture did not shut down"
    finally:
        sock.close()
