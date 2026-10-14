# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for ``workflow_engine.payload_fetch`` (detection-guided
Bedrock inspection, task 2).

Covers dotted-path resolution over nested dict/list payloads, base64 and
``data:`` URL decoding, the ``allowed_uri_prefixes`` gate (allowed,
denied, empty-permits-all), the size cap, timeout wiring, non-image
rejection, and the errors-carry-source-not-bytes contract. HTTP cases
use an in-process localhost server; S3 uses a stubbed client.

Requirements: 3.2, 3.3, 3.4, 3.5, 3.7, 3.8

The security-scan-remediation-high cases at the end cover unsupported
schemes, per-hop redirect checks, the opener's handlers and URL
redaction (that spec's Requirements 9.1, 9.3, 9.4 and 9.5).
"""
import base64
import io
import itertools
import os
import select
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from workflow_engine import payload_fetch
from workflow_engine.payload_fetch import (
    BASE64_SOURCE,
    MAX_REFERENCE_BYTES,
    REFERENCE_FETCH_TIMEOUT_SEC,
    PayloadReferenceError,
    describe_reference_source,
    fetch_reference_bytes,
    resolve_payload_path,
)

_path_counter = itertools.count()


def encode_png():
    """A small real PNG the reference validator accepts."""
    array = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
    ok, encoded = cv2.imencode(".png", array)
    assert ok
    return encoded.tobytes()


PNG_BYTES = encode_png()


# ---------------------------------------------------------------------------
# In-process HTTP server (mirrors the dda_frames HTTP test pattern)
# ---------------------------------------------------------------------------

class _ContentHandler(BaseHTTPRequestHandler):
    """Serves the bytes registered on the server under each path, answers
    the redirects registered for a path as ``(status, Location)`` and the
    error status registered for it (404 for any other path), and records
    every requested path."""

    def do_GET(self):
        self.server.requested.append(self.path)
        redirect = self.server.redirects.get(self.path)
        if redirect is not None:
            status, location = redirect
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.server.content.get(self.path)
        if body is None:
            self.send_response(self.server.statuses.get(self.path, 404))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep test output clean


@pytest.fixture(scope="module")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ContentHandler)
    server.content = {}
    server.redirects = {}
    server.statuses = {}
    server.requested = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def serve(server, body):
    """Register ``body`` under a fresh path; return its localhost URL."""
    path = "/ref-{0}".format(next(_path_counter))
    server.content[path] = body
    return "http://127.0.0.1:{0}{1}".format(server.server_address[1], path)


def serve_redirect(server, status, location, directory=""):
    """Register a ``status`` redirect to ``location`` under a fresh path
    (below ``directory``); return its localhost URL."""
    path = "{0}/redirect-{1}".format(directory, next(_path_counter))
    server.redirects[path] = (status, location)
    return "http://127.0.0.1:{0}{1}".format(server.server_address[1], path)


def base_url(server):
    return "http://127.0.0.1:{0}".format(server.server_address[1])


# ---------------------------------------------------------------------------
# Stubbed S3 client
# ---------------------------------------------------------------------------

class _StubBody:
    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)


class _StubS3Client:
    """get_object over an in-memory (bucket, key) -> bytes map."""

    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        self.calls.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("NoSuchKey: the object does not exist")
        return {"Body": _StubBody(self.objects[(Bucket, Key)])}


# ---------------------------------------------------------------------------
# resolve_payload_path: dotted paths over nested dict/list payloads
# (Requirement 3.2 groundwork, 3.5 unresolvable-path reason)
# ---------------------------------------------------------------------------

PAYLOAD = {
    "refs": [
        {"id": "plate-A", "image": "s3://bucket/refA.jpg"},
        {"id": "plate-B", "image": "data:image/jpeg;base64,Zm9v"},
    ],
    "meta": {"lot": "L42", "grid": [[1, 2], [3, 4]]},
}


def test_resolve_dict_and_list_mix():
    assert resolve_payload_path(PAYLOAD, "refs.0.image") == (
        "s3://bucket/refA.jpg"
    )
    assert resolve_payload_path(PAYLOAD, "refs.1.id") == "plate-B"
    assert resolve_payload_path(PAYLOAD, "meta.lot") == "L42"
    assert resolve_payload_path(PAYLOAD, "meta.grid.1.0") == 3


def test_resolve_single_segment_and_whole_containers():
    assert resolve_payload_path({"image": "x"}, "image") == "x"
    assert resolve_payload_path(PAYLOAD, "refs") is PAYLOAD["refs"]


def test_resolve_missing_dict_key_names_segment():
    with pytest.raises(PayloadReferenceError) as e:
        resolve_payload_path(PAYLOAD, "refs.0.picture")
    message = str(e.value)
    assert "'picture'" in message
    assert "refs.0.picture" in message


def test_resolve_index_out_of_range_names_segment_and_count():
    with pytest.raises(PayloadReferenceError) as e:
        resolve_payload_path(PAYLOAD, "refs.2.image")
    message = str(e.value)
    assert "2" in message
    assert "out of range" in message


def test_resolve_non_integer_list_segment_names_segment():
    with pytest.raises(PayloadReferenceError) as e:
        resolve_payload_path(PAYLOAD, "refs.first.image")
    assert "'first'" in str(e.value)


def test_resolve_negative_index_rejected():
    with pytest.raises(PayloadReferenceError):
        resolve_payload_path(PAYLOAD, "refs.-1.image")


def test_resolve_cannot_descend_into_scalar():
    with pytest.raises(PayloadReferenceError) as e:
        resolve_payload_path(PAYLOAD, "meta.lot.deeper")
    message = str(e.value)
    assert "'deeper'" in message
    assert "str" in message


def test_resolve_empty_path_rejected():
    with pytest.raises(PayloadReferenceError):
        resolve_payload_path(PAYLOAD, "")
    with pytest.raises(PayloadReferenceError):
        resolve_payload_path(PAYLOAD, None)


def test_resolve_none_payload_root():
    with pytest.raises(PayloadReferenceError) as e:
        resolve_payload_path(None, "refs.0.image")
    assert "the payload root" in str(e.value)


# ---------------------------------------------------------------------------
# Base64 and data: URL decode (Requirement 3.3)
# ---------------------------------------------------------------------------

def test_bare_base64_decodes_to_image_bytes():
    value = base64.b64encode(PNG_BYTES).decode("ascii")
    assert fetch_reference_bytes(value, ()) == PNG_BYTES


def test_bare_base64_tolerates_embedded_newlines():
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    wrapped = "\n".join(
        encoded[i:i + 60] for i in range(0, len(encoded), 60)
    )
    assert fetch_reference_bytes(wrapped, ()) == PNG_BYTES


def test_data_url_decodes_to_image_bytes():
    value = "data:image/png;base64," + base64.b64encode(
        PNG_BYTES
    ).decode("ascii")
    assert fetch_reference_bytes(value, ()) == PNG_BYTES


def test_data_url_without_base64_marker_rejected():
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("data:text/plain,hello", ())
    assert "base64" in str(e.value)


def test_invalid_bare_base64_rejected_without_echoing_value():
    value = "!!!not-base64-at-all!!!"
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(value, ())
    assert value not in str(e.value)


def test_base64_bypasses_prefix_gate():
    # Requirement 3.4: the allow-list gates URI fetches; base64 payload
    # data needs no gate.
    value = base64.b64encode(PNG_BYTES).decode("ascii")
    assert fetch_reference_bytes(
        value, ("s3://only-this-bucket/",)
    ) == PNG_BYTES


def test_non_string_value_rejected():
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes({"image": "x"}, ())
    assert "dict" in str(e.value)


# ---------------------------------------------------------------------------
# Prefix gating on URI fetches (Requirement 3.4)
# ---------------------------------------------------------------------------

def test_http_fetch_allowed_by_matching_prefix(http_server):
    url = serve(http_server, PNG_BYTES)
    prefix = url[:len("http://127.0.0.1:")]
    assert fetch_reference_bytes(url, (prefix,)) == PNG_BYTES


def test_http_fetch_denied_by_non_matching_prefix(http_server):
    url = serve(http_server, PNG_BYTES)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ("https://allowed.example/",))
    message = str(e.value)
    assert url in message
    assert "allowed URI prefixes" in message


def test_empty_prefix_list_permits_all(http_server):
    url = serve(http_server, PNG_BYTES)
    assert fetch_reference_bytes(url, ()) == PNG_BYTES
    assert fetch_reference_bytes(url, None) == PNG_BYTES


def test_s3_fetch_denied_prefix_never_touches_client():
    client = _StubS3Client({("bucket", "ref.png"): PNG_BYTES})
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(
            "s3://bucket/ref.png", ("s3://other-bucket/",), s3_client=client
        )
    assert "s3://bucket/ref.png" in str(e.value)
    assert client.calls == []


# ---------------------------------------------------------------------------
# S3 fetch through the stubbed client (Requirement 3.2)
# ---------------------------------------------------------------------------

def test_s3_fetch_returns_object_bytes():
    client = _StubS3Client({("bucket", "path/ref.png"): PNG_BYTES})
    data = fetch_reference_bytes(
        "s3://bucket/path/ref.png", ("s3://bucket/",), s3_client=client
    )
    assert data == PNG_BYTES
    assert client.calls == [("bucket", "path/ref.png")]


def test_s3_fetch_failure_names_source():
    client = _StubS3Client({})
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("s3://bucket/missing.png", (), s3_client=client)
    assert "s3://bucket/missing.png" in str(e.value)


def test_s3_malformed_uri_rejected():
    client = _StubS3Client({})
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("s3://bucket-only", (), s3_client=client)
    assert "s3://bucket-only" in str(e.value)
    assert client.calls == []


# ---------------------------------------------------------------------------
# HTTP fetch through the localhost server (Requirement 3.2)
# ---------------------------------------------------------------------------

def test_http_fetch_returns_served_bytes(http_server):
    url = serve(http_server, PNG_BYTES)
    assert fetch_reference_bytes(url, ()) == PNG_BYTES


def test_http_404_names_source(http_server):
    url = "http://127.0.0.1:{0}/no-such-ref".format(
        http_server.server_address[1]
    )
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    assert url in str(e.value)


# ---------------------------------------------------------------------------
# Size cap (Requirement 3.7). The cap value itself is pinned below; the
# enforcement paths are exercised with a monkeypatched cap so the tests
# stay small and fast.
# ---------------------------------------------------------------------------

def test_size_cap_constant_is_8_mib():
    assert MAX_REFERENCE_BYTES == 8 * 1024 * 1024


def test_http_size_cap_enforced(http_server, monkeypatch):
    monkeypatch.setattr(payload_fetch, "MAX_REFERENCE_BYTES", 64)
    url = serve(http_server, b"x" * 65)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    message = str(e.value)
    assert url in message
    assert "size cap" in message


def test_s3_size_cap_enforced(monkeypatch):
    monkeypatch.setattr(payload_fetch, "MAX_REFERENCE_BYTES", 64)
    client = _StubS3Client({("bucket", "big.png"): b"x" * 65})
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("s3://bucket/big.png", (), s3_client=client)
    message = str(e.value)
    assert "s3://bucket/big.png" in message
    assert "size cap" in message


def test_base64_size_cap_enforced(monkeypatch):
    monkeypatch.setattr(payload_fetch, "MAX_REFERENCE_BYTES", 16)
    value = base64.b64encode(b"y" * 32).decode("ascii")
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(value, ())
    message = str(e.value)
    assert BASE64_SOURCE in message
    assert "size cap" in message


# ---------------------------------------------------------------------------
# Timeout wiring (Requirement 3.7): every HTTP fetch passes the bounded
# timeout to urllib.
# ---------------------------------------------------------------------------

def test_timeout_constant_is_10_seconds():
    assert REFERENCE_FETCH_TIMEOUT_SEC == 10.0


def test_http_fetch_passes_bounded_timeout(monkeypatch):
    captured = {}

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=-1):
            return PNG_BYTES

    # ``context`` is always passed by _fetch_http (the CA-bundle fix):
    # a verifying SSLContext for https://, None for plain http.
    class _FakeOpener:
        def open(self, url, timeout=None):
            captured["url"] = url
            captured["timeout"] = timeout
            return _FakeResponse()

    def fake_build_opener(prefixes, context=None):
        captured["context"] = context
        return _FakeOpener()

    monkeypatch.setattr(payload_fetch, "_build_opener", fake_build_opener)
    data = fetch_reference_bytes("http://example.invalid/ref.png", ())
    assert data == PNG_BYTES
    assert captured["url"] == "http://example.invalid/ref.png"
    assert captured["timeout"] == REFERENCE_FETCH_TIMEOUT_SEC
    # A plain-http fetch needs no trust store, so no context is built.
    assert captured["context"] is None
    # An https fetch verifies against the certifi-backed context.
    fetch_reference_bytes("https://example.invalid/ref.png", ())
    assert captured["context"] is payload_fetch.https_ssl_context()


def test_http_timeout_error_names_source(monkeypatch):
    class _FakeOpener:
        def open(self, url, timeout=None):
            raise TimeoutError("timed out")

    monkeypatch.setattr(
        payload_fetch, "_build_opener",
        lambda prefixes, context=None: _FakeOpener()
    )
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("http://example.invalid/slow.png", ())
    assert "http://example.invalid/slow.png" in str(e.value)


# ---------------------------------------------------------------------------
# Non-image rejection (Requirement 3.5 reason: bytes not a decodable
# image) and the errors-carry-source-not-bytes contract (Requirement 3.8)
# ---------------------------------------------------------------------------

def test_http_non_image_rejected_and_error_carries_source_not_bytes(
    http_server,
):
    body = b"definitely-not-an-image-payload"
    url = serve(http_server, body)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    message = str(e.value)
    assert url in message
    assert "not a decodable image" in message
    assert body.decode("ascii") not in message


def test_s3_non_image_rejected():
    client = _StubS3Client({("bucket", "notes.txt"): b"just text"})
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("s3://bucket/notes.txt", (), s3_client=client)
    message = str(e.value)
    assert "s3://bucket/notes.txt" in message
    assert "just text" not in message


def test_base64_non_image_rejected_without_bytes():
    payload = b"plain text, valid base64 target, not an image"
    value = base64.b64encode(payload).decode("ascii")
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(value, ())
    message = str(e.value)
    assert BASE64_SOURCE in message
    assert value not in message
    assert payload.decode("ascii") not in message


def test_empty_base64_payload_rejected():
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("", ())
    assert "not a decodable image" in str(e.value)


# ---------------------------------------------------------------------------
# describe_reference_source (Requirement 3.8): URIs by value, everything
# else as "base64 payload data"
# ---------------------------------------------------------------------------

def test_describe_reference_source():
    assert describe_reference_source("s3://b/k.jpg") == "s3://b/k.jpg"
    assert describe_reference_source("https://x/y.png") == "https://x/y.png"
    assert describe_reference_source("http://x/y.png") == "http://x/y.png"
    assert describe_reference_source(
        "data:image/png;base64,Zm9v"
    ) == BASE64_SOURCE
    assert describe_reference_source("bm90IGEgdXJp") == BASE64_SOURCE
    assert describe_reference_source(12) == BASE64_SOURCE


# ---------------------------------------------------------------------------
# file:// local references. The value comes from the run's Trigger_Context
# (untrusted MQTT input) and the bytes are sent to a cloud model, so the
# allow-list is MANDATORY here, the path is canonicalized before the
# prefix re-check (no ../ or symlink escape), and only regular files are
# read.
# ---------------------------------------------------------------------------

def test_file_uri_reads_an_allowed_local_image(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG_BYTES)
    prefixes = ("file://{0}/".format(tmp_path),)
    assert fetch_reference_bytes(
        "file://{0}".format(ref), prefixes
    ) == PNG_BYTES


def test_file_uri_accepts_localhost_authority(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG_BYTES)
    prefixes = ("file://{0}/".format(tmp_path),)
    assert fetch_reference_bytes(
        "file://localhost{0}".format(ref), prefixes
    ) == PNG_BYTES


def test_file_uri_denied_when_allow_list_is_empty(tmp_path):
    """Empty permits every REMOTE source but never a local file."""
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG_BYTES)
    for prefixes in ((), None):
        with pytest.raises(PayloadReferenceError) as e:
            fetch_reference_bytes("file://{0}".format(ref), prefixes)
        assert "allowed URI prefixes" in str(e.value)


def test_file_uri_denied_when_only_remote_prefixes_configured(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG_BYTES)
    with pytest.raises(PayloadReferenceError):
        fetch_reference_bytes(
            "file://{0}".format(ref), ("s3://bucket/", "https://host/")
        )


def test_file_uri_traversal_cannot_escape_the_allowed_root(tmp_path):
    allowed = tmp_path / "refs"
    allowed.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG_BYTES)
    prefixes = ("file://{0}/".format(allowed),)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(
            "file://{0}/../secret.png".format(allowed), prefixes
        )
    assert "outside" in str(e.value)


def test_file_uri_symlink_cannot_escape_the_allowed_root(tmp_path):
    allowed = tmp_path / "refs"
    allowed.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG_BYTES)
    link = allowed / "sneaky.png"
    link.symlink_to(secret)
    prefixes = ("file://{0}/".format(allowed),)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("file://{0}".format(link), prefixes)
    assert "outside" in str(e.value)


def test_file_uri_rejects_a_directory(tmp_path):
    prefixes = ("file://{0}/".format(tmp_path),)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("file://{0}".format(tmp_path), prefixes)
    assert "not a regular file" in str(e.value) or "outside" in str(e.value)


def test_file_uri_rejects_a_non_regular_file(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    prefixes = ("file://{0}/".format(tmp_path),)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("file://{0}".format(fifo), prefixes)
    assert "not a regular file" in str(e.value)


def test_file_uri_missing_file_names_the_source(tmp_path):
    prefixes = ("file://{0}/".format(tmp_path),)
    missing = "file://{0}/nope.png".format(tmp_path)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(missing, prefixes)
    assert missing in str(e.value)


def test_file_uri_size_cap_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(payload_fetch, "MAX_REFERENCE_BYTES", 16)
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * 64)
    prefixes = ("file://{0}/".format(tmp_path),)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("file://{0}".format(big), prefixes)
    assert "size cap" in str(e.value)


def test_file_uri_non_image_rejected_without_bytes(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"definitely not an image")
    prefixes = ("file://{0}/".format(tmp_path),)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes("file://{0}".format(notes), prefixes)
    message = str(e.value)
    assert "not a decodable image" in message
    assert "definitely not an image" not in message


def test_file_uri_malformed_rejected():
    for bad in ("file://", "file:///", "file://remotehost/refs/a.png"):
        with pytest.raises(PayloadReferenceError):
            fetch_reference_bytes(bad, ("file:///refs/",))


def test_describe_reference_source_reports_file_uris_by_value():
    assert describe_reference_source(
        "file:///aws_dda/refs/a.png"
    ) == "file:///aws_dda/refs/a.png"


# ---------------------------------------------------------------------------
# URL fetching limits (security-scan-remediation-high, Requirement 9):
# initial schemes, per-hop redirect checks (Property 5), the opener's
# handlers, and URL redaction (Property 4). No outbound network: requests
# go to the localhost server, to a local listener that only records
# connection attempts, or to a closed local port.
# ---------------------------------------------------------------------------

#: Every redirect status this runtime follows (CPython 3.10 doesn't follow 308).
FOLLOWED_STATUSES = [301, 302, 303, 307] + (
    [308] if hasattr(urllib.request.HTTPRedirectHandler, "http_error_308")
    else [])

#: A visibly fake presigned-URL query.
FAKE_QUERY = "X-Amz-Signature=FAKE-SIGNATURE-FOR-TESTS"


@pytest.fixture
def listener():
    """A local port that records connection attempts and answers none."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield sock
    finally:
        sock.close()


def attempted(sock):
    """Whether anything tried to connect to the ``listener`` socket."""
    readable, _, _ = select.select([sock], [], [], 0.2)
    return bool(readable)


@pytest.mark.parametrize("template", [
    "ftp://127.0.0.1:{port}{path}",
    "gopher://127.0.0.1:{port}{path}",
    "HTTP://127.0.0.1:{port}{path}",
    "javascript:fetch('{path}')",
])
def test_unsupported_initial_scheme_is_rejected_without_echo(
        http_server, template):
    path = "/scheme-{0}".format(next(_path_counter))
    value = template.format(port=http_server.server_address[1], path=path)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(value, ())
    assert path not in str(e.value)
    assert path not in http_server.requested


def assert_scheme_refusal(message, scheme, target_path):
    assert "redirect to scheme '{0}' is not allowed".format(scheme) in message
    assert target_path not in message
    assert "FAKE-SIGNATURE" not in message


@pytest.mark.parametrize("status", FOLLOWED_STATUSES)
@pytest.mark.parametrize("scheme", ["file", "gopher"])
def test_redirect_to_a_non_http_scheme_is_refused_for_every_status(
        http_server, listener, tmp_path, status, scheme):
    target_path = "/target-{0}.png".format(next(_path_counter))
    if scheme == "file":
        (tmp_path / target_path[1:]).write_bytes(PNG_BYTES)
        target = "file://{0}{1}?{2}".format(tmp_path, target_path, FAKE_QUERY)
    else:
        target = "gopher://127.0.0.1:{0}{1}?{2}".format(
            listener.getsockname()[1], target_path, FAKE_QUERY)
    url = serve_redirect(http_server, status, target)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    assert_scheme_refusal(str(e.value), scheme, target_path)
    assert str(tmp_path) not in str(e.value)
    assert not attempted(listener)


def test_redirect_to_ftp_is_refused_and_never_connects(http_server, listener):
    target_path = "/ftp-target-{0}.png".format(next(_path_counter))
    target = "ftp://127.0.0.1:{0}{1}?{2}".format(
        listener.getsockname()[1], target_path, FAKE_QUERY)
    url = serve_redirect(http_server, 302, target)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    assert_scheme_refusal(str(e.value), "ftp", target_path)
    assert not attempted(listener)


def test_redirect_inside_the_allowed_prefix_is_followed(http_server):
    target = serve(http_server, PNG_BYTES)
    url = serve_redirect(http_server, 302, target)
    assert fetch_reference_bytes(url, (base_url(http_server) + "/",)) == PNG_BYTES
    assert target[len(base_url(http_server)):] in http_server.requested


def test_redirect_outside_the_allowed_prefix_is_refused_and_never_requested(
        http_server):
    target = serve(http_server, PNG_BYTES)
    url = serve_redirect(http_server, 302, target, directory="/inside")
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, (base_url(http_server) + "/inside/",))
    message = str(e.value)
    assert "the redirect to '{0}' is outside".format(target) in message
    assert target[len(base_url(http_server)):] not in http_server.requested


def test_same_host_redirect_is_followed_without_prefixes(http_server):
    target_path = serve(http_server, PNG_BYTES)[len(base_url(http_server)):]
    url = serve_redirect(http_server, 307, target_path)  # relative Location
    assert fetch_reference_bytes(url, ()) == PNG_BYTES
    assert target_path in http_server.requested


def test_redirect_to_a_url_with_userinfo_is_refused(http_server):
    target_path = serve(http_server, PNG_BYTES)[len(base_url(http_server)):]
    target = "http://{0}:{1}@127.0.0.1:{2}{3}".format(
        "fake-user", "fake-credential", http_server.server_address[1],
        target_path)
    url = serve_redirect(http_server, 302, target)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    message = str(e.value)
    assert "embedded credentials" in message
    assert "fake-user" not in message and "fake-credential" not in message
    assert target_path not in http_server.requested


class _ClosingResponse:
    closed = False

    def close(self):
        self.closed = True


def test_https_to_http_redirect_is_refused():
    handler = payload_fetch._GatedRedirectHandler(())
    request = urllib.request.Request("https://example.invalid/start")
    response = _ClosingResponse()
    with pytest.raises(PayloadReferenceError) as e:
        handler.redirect_request(
            request, response, 302, "Found", {},
            "http://example.invalid/next?" + FAKE_QUERY)
    message = str(e.value)
    assert "from https to http" in message
    assert "FAKE-SIGNATURE" not in message
    assert response.closed
    # https to https stays allowed; redirect_request sends nothing itself.
    followed = handler.redirect_request(
        request, _ClosingResponse(), 302, "Found", {},
        "https://example.invalid/next")
    assert followed.full_url == "https://example.invalid/next"


def test_opener_has_only_http_handlers(monkeypatch):
    # With a proxy configured, ProxyHandler must be part of the chain.
    monkeypatch.setenv("https_proxy", "http://proxy.example.invalid:3128")
    opener = payload_fetch._build_opener(
        ("https://allowed.example/",), payload_fetch.https_ssl_context())
    handlers = opener.handlers
    for absent in (urllib.request.FTPHandler, urllib.request.FileHandler,
                   urllib.request.DataHandler):
        assert not any(isinstance(h, absent) for h in handlers)
    redirects = [h for h in handlers
                 if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert len(redirects) == 1
    assert isinstance(redirects[0], payload_fetch._GatedRedirectHandler)
    assert redirects[0].allowed_prefixes == ("https://allowed.example/",)
    https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    assert https[0]._context is payload_fetch.https_ssl_context()
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)
    assert any(type(h) is urllib.request.HTTPHandler for h in handlers)
    # Without a context (plain http fetches) urllib keeps its default.
    plain = payload_fetch._build_opener(())
    assert [h._context for h in plain.handlers
            if isinstance(h, urllib.request.HTTPSHandler)] == [None]


def test_url_with_userinfo_is_refused_before_any_request(http_server):
    path = serve(http_server, PNG_BYTES)[len(base_url(http_server)):]
    url = "http://{0}:{1}@127.0.0.1:{2}{3}".format(
        "fake-user", "fake-credential", http_server.server_address[1], path)
    for prefixes in ((), (base_url(http_server) + "/",), ("http://",)):
        with pytest.raises(PayloadReferenceError) as e:
            fetch_reference_bytes(url, prefixes)
        message = str(e.value)
        assert "URLs with embedded credentials are not supported" in message
        assert "(scheme 'http', host '127.0.0.1')" in message
        assert "fake-user" not in message
        assert "fake-credential" not in message
    assert path not in http_server.requested


def test_http_404_on_a_signed_url_reports_the_path_not_the_query(http_server):
    path = "/no-such-signed-ref-{0}".format(next(_path_counter))
    query = "X-Amz-Signature={0}&X-Amz-Security-Token={1}".format(
        "FAKE-SIGNATURE-FOR-TESTS", "FAKE-SESSION-FOR-TESTS")
    url = "{0}{1}?{2}".format(base_url(http_server), path, query)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    message = str(e.value)
    assert "{0}{1}?<redacted>".format(base_url(http_server), path) in message
    assert "HTTP Error 404" in message
    assert "FAKE-SIGNATURE-FOR-TESTS" not in message
    assert "FAKE-SESSION-FOR-TESTS" not in message
    assert path + "?" + query in http_server.requested  # the fetch itself is unchanged


def test_exception_text_echoing_the_query_is_scrubbed(http_server):
    # http.client rejects the space and quotes the whole request path.
    path = "/control-{0}".format(next(_path_counter))
    url = "{0}{1}?sig=FAKE SIGNATURE\x01".format(base_url(http_server), path)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    message = str(e.value)
    assert path in message
    assert "FAKE SIGNATURE" not in message


def test_prefix_denial_names_the_redacted_url(http_server):
    url = "{0}/denied-{1}?{2}".format(
        base_url(http_server), next(_path_counter), FAKE_QUERY)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ("https://allowed.example/",))
    message = str(e.value)
    assert "'{0}?<redacted>'".format(url.partition("?")[0]) in message
    assert "FAKE-SIGNATURE" not in message


def test_describe_reference_source_drops_userinfo_query_and_fragment():
    value = "https://{0}@bucket.example/refs/a.png?{1}#part".format(
        "fake-user", FAKE_QUERY)
    assert describe_reference_source(value) == (
        "https://bucket.example/refs/a.png?<redacted>")
    assert describe_reference_source("s3://b/k.jpg?versionId=v1") == (
        "s3://b/k.jpg?<redacted>")
    assert describe_reference_source("https://[::1/x") == "<unparseable URI>"


@pytest.fixture(scope="module")
def closed_port():
    """A local port with no listener: connections are refused at once."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _EchoingS3Client:
    """Fails like a boto3 parameter error that quotes the bucket and key."""

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        raise RuntimeError("Invalid bucket {0!r} or key {1!r}".format(Bucket, Key))


_USERINFO_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;="
_QUERY_CHARS = _USERINFO_CHARS + ":@/? \x01"


@st.composite
def url_credential_cases(draw):
    """A reference URL carrying generated user information and/or query
    values (each with a fixed marker prefix, so it can't occur in message
    text by chance), the node's prefixes, and those values."""
    user = "fakeuser" + draw(st.text(_USERINFO_CHARS, max_size=10))
    credential = "fakecred" + draw(st.text(_USERINFO_CHARS + ":", max_size=10))
    signature = "fakesig" + draw(st.text(_QUERY_CHARS, max_size=12))
    userinfo = draw(st.sampled_from(["", "{0}@", "{0}:{1}@"]))
    query = draw(st.sampled_from(
        ["", "?X-Amz-Signature={2}", "?a=1&X-Amz-Security-Token={2}"]))
    if not userinfo and not query:
        query = "?X-Amz-Signature={2}"
    return {
        "scheme": draw(st.sampled_from(["http", "https", "s3", "file"])),
        "userinfo": userinfo.format(user, credential),
        "query": query.format(user, credential, signature),
        "remote_prefixes": draw(
            st.sampled_from([(), ("https://allowed.example/",)])),
        "values": (user, credential, signature),
    }


# Feature: security-scan-remediation-high, Property 4: Payload reference
# errors never echo URL secrets. Validates: Requirements 9.4
# Runs at Hypothesis's own default example count, taken from its built-in
# "default" profile, because the conftests' fast profiles lower it to 25.
@settings(max_examples=settings.get_profile("default").max_examples, deadline=None)
@given(case=url_credential_cases())
@example(case={"scheme": "http", "userinfo": "fakeuser:fakecred@",
               "query": "?X-Amz-Signature=fakesig", "remote_prefixes": (),
               "values": ("fakeuser", "fakecred", "fakesig")})
@example(case={"scheme": "https", "userinfo": "",
               "query": "?X-Amz-Signature=fakesig x\x01", "remote_prefixes": (),
               "values": ("fakeuser", "fakecred", "fakesig x\x01")})
@example(case={"scheme": "s3", "userinfo": "fakeuser:fakecred@",
               "query": "?a=1&X-Amz-Security-Token=fakesig'", "remote_prefixes": (),
               "values": ("fakeuser", "fakecred", "fakesig'")})
def test_property_payload_reference_errors_never_echo_url_credentials(
        http_server, closed_port, case):
    hosts = {
        "http": "127.0.0.1:{0}".format(http_server.server_address[1]),
        "https": "127.0.0.1:{0}".format(closed_port),
        "s3": "reference-bucket",
        "file": "localhost",
    }
    scheme = case["scheme"]
    url = "{0}://{1}{2}/property-root/ref-{3}.png{4}".format(
        scheme, case["userinfo"], hosts[scheme], next(_path_counter),
        case["query"])
    prefixes = (("file:///property-root/",) if scheme == "file"
                else case["remote_prefixes"])
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, prefixes, s3_client=_EchoingS3Client())
    for text in (str(e.value), describe_reference_source(url)):
        for value in case["values"]:
            assert value not in text
            assert repr(value)[1:-1] not in text  # as exception text quotes it
        # No fragment either: every generated value starts with its marker.
        for marker in ("fakeuser", "fakecred", "fakesig"):
            assert marker not in text


# Short values are scrubbed only where they appear as URL parts, with their
# delimiter, so the rest of the error text stays readable (task 3 review).

def test_short_query_and_fragment_leave_the_status_text_intact(http_server):
    path = "/unauthorized-{0}".format(next(_path_counter))
    http_server.statuses[path + "?1"] = 401
    url = "{0}{1}?1#a".format(base_url(http_server), path)
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(url, ())
    assert str(e.value) == (
        "could not fetch reference '{0}{1}?<redacted>': HTTP Error 401: "
        "Unauthorized".format(base_url(http_server), path))
    assert path + "?1" in http_server.requested


def test_short_userinfo_and_query_are_scrubbed_only_as_url_parts():
    with pytest.raises(PayloadReferenceError) as e:
        fetch_reference_bytes(
            "s3://a:b@bucket/key.png?1", (), s3_client=_EchoingS3Client())
    assert str(e.value) == (
        "could not fetch reference 's3://bucket/key.png?<redacted>': "
        "Invalid bucket '<redacted>@bucket' or key 'key.png?<redacted>'")
