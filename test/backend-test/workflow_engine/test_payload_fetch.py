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
"""
import base64
import io
import itertools
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

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
    """Serves the bytes registered on the server under each path."""

    def do_GET(self):
        body = self.server.content.get(self.path)
        if body is None:
            self.send_response(404)
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

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(
        payload_fetch.urllib.request, "urlopen", fake_urlopen
    )
    data = fetch_reference_bytes("http://example.invalid/ref.png", ())
    assert data == PNG_BYTES
    assert captured["url"] == "http://example.invalid/ref.png"
    assert captured["timeout"] == REFERENCE_FETCH_TIMEOUT_SEC


def test_http_timeout_error_names_source(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(
        payload_fetch.urllib.request, "urlopen", fake_urlopen
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
