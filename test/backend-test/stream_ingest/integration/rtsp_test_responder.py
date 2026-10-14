#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""A scripted RTSP responder for the integration tests: it answers
``OPTIONS`` and gives every ``DESCRIBE`` one fixed response, optionally over
TLS with a self-signed certificate. Enough to drive ``rtspsrc`` into the
failure answers a real server gives (a 404 for a path nobody publishes, a
401, an untrusted certificate) without a media server.
"""
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from typing import Optional

from rtmp_test_server import free_port

PUBLIC = "OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY, PAUSE"


def self_signed_certificate(directory: str) -> Optional[tuple]:
    """``(cert, key)`` paths of a fresh self-signed certificate for
    ``localhost``, made with the ``openssl`` CLI or the ``cryptography``
    package; None when neither is available."""
    cert = os.path.join(directory, "server.crt")
    key = os.path.join(directory, "server.key")
    openssl = shutil.which("openssl")
    if openssl is not None:
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", key, "-out", cert],
                       check=True, capture_output=True, timeout=60)
        return cert, key
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return None
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.utcnow()
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .sign(private_key, hashes.SHA256()))
    with open(key, "wb") as handle:
        handle.write(private_key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.TraditionalOpenSSL,
                                               serialization.NoEncryption()))
    with open(cert, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    return cert, key


class RtspResponder:
    """Serves until closed. ``describe`` is the status line and extra
    header lines of every DESCRIBE answer, e.g. ``"404 Not Found"``."""

    def __init__(self, describe: str, extra_headers: str = "", tls: bool = False):
        self.describe = describe
        self.extra_headers = extra_headers
        self.port = free_port()
        self.requests = []
        self._directory = tempfile.mkdtemp(prefix="rtsp-responder-")
        self._context = None
        if tls:
            pair = self_signed_certificate(self._directory)
            if pair is None:
                raise RuntimeError("no openssl CLI or cryptography package to make a certificate")
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(*pair)
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.port))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def url(self, path: str = "stream1") -> str:
        scheme = "rtsps" if self._context is not None else "rtsp"
        return f"{scheme}://127.0.0.1:{self.port}/{path}"

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection) -> None:
        try:
            connection.settimeout(5)
            if self._context is not None:
                try:
                    connection = self._context.wrap_socket(connection, server_side=True)
                except (ssl.SSLError, OSError):
                    return  # the client rejected the certificate
            buffer = b""
            while not self._closed.is_set():
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\r\n\r\n" in buffer:
                    head, buffer = buffer.split(b"\r\n\r\n", 1)
                    self._answer(connection, head.decode("latin-1"))
        except (OSError, ssl.SSLError):
            return
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _answer(self, connection, head: str) -> None:
        lines = head.split("\r\n")
        method = lines[0].split(" ", 1)[0]
        cseq = next((line.split(":", 1)[1].strip() for line in lines[1:]
                     if line.lower().startswith("cseq:")), "0")
        self.requests.append(method)
        if method == "OPTIONS":
            answer = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: {PUBLIC}\r\n\r\n"
        else:
            answer = f"RTSP/1.0 {self.describe}\r\nCSeq: {cseq}\r\n{self.extra_headers}Content-Length: 0\r\n\r\n"
        connection.sendall(answer.encode("latin-1"))

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        shutil.rmtree(self._directory, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
