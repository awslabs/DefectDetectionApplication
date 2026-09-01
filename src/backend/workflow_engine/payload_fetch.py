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

"""Payload_Reference resolution and fetching for ``bedrock_inference``
(Requirements 3.2, 3.3, 3.4, 3.7, 3.8).

A Bedrock_Binding configured with ``reference_payload_path`` resolves a
dotted path (for example ``refs.0.image``) against the run's
Trigger_Context ``payload_json`` and turns the resolved value into
reference-image bytes:

- ``s3://bucket/key`` URIs fetch through boto3 ``get_object`` (streamed,
  size-capped) using the device's ambient AWS credentials — the same
  default credential chain as the Bedrock client (Greengrass TES on
  device);
- ``http(s)://`` URLs fetch through urllib with a bounded network
  timeout (:data:`REFERENCE_FETCH_TIMEOUT_SEC`, Requirement 3.7);
- ``file://`` URIs read the device's own filesystem, gated harder than
  the remote schemes: a non-empty ``file://`` allow-list entry is
  MANDATORY, the path is canonicalized before the prefix re-check, and
  only regular files are read (see :data:`FILE_SCHEME`);
- ``data:`` URLs and bare base64 strings decode locally (no gate — the
  bytes are already in the payload).

Every fetch is bounded by :data:`MAX_REFERENCE_BYTES` (Requirement 3.7),
URI fetches are gated by the node's ``allowed_uri_prefixes`` allow-list
(empty permits any source, Requirement 3.4), and the result must decode
as an image (``cv2.imdecode``) before it is returned.

Every failure raises :class:`PayloadReferenceError` identifying the
source — the URI, the failing path segment, or the literal
``"base64 payload data"`` — and **never** the image bytes themselves
(Requirement 3.8). Callers record the outcome as a per-node error; this
module never falls back or swallows a failure (Requirement 3.5 is
enforced at the call site by construction: there is no partial-success
return).

Deliberately free of ``python_bridge`` imports: that module's fetch
helpers live in a subprocess source string and cannot be shared.
"""

import base64
import logging
import os
import stat
import urllib.request
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: Upper bound on accepted reference-image bytes, applied to every
#: source (URI fetches while streaming, base64 after decoding), so an
#: oversized reference cannot exhaust memory or stall the run
#: (Requirement 3.7).
MAX_REFERENCE_BYTES = 8 * 1024 * 1024  # 8 MiB

#: Bounded network timeout applied to every payload reference fetch, in
#: seconds (Requirement 3.7). Kept well below the run's wall-clock
#: limits so a slow endpoint cannot stall the run past them.
REFERENCE_FETCH_TIMEOUT_SEC = 10.0

#: The source string recorded for base64-sourced references (bare
#: base64 or ``data:`` URLs) — the run log must never carry the decoded
#: bytes (Requirement 3.8).
BASE64_SOURCE = "base64 payload data"

#: S3 streaming chunk size; the cap is enforced as the stream
#: accumulates so an oversized object is abandoned early.
_S3_CHUNK_BYTES = 1 << 20

#: The local-file scheme. UNLIKE every other accepted source, a
#: ``file://`` reference reads the device's own filesystem, and the value
#: is resolved from the run's Trigger_Context — untrusted external input.
#: It is therefore gated harder than the remote schemes: a NON-EMPTY
#: ``allowed_uri_prefixes`` is MANDATORY (an empty allow-list permits
#: every remote source but DENIES every local file), the path is
#: canonicalized before the prefix is re-checked so ``..`` and symlinks
#: cannot escape the allowed directory, and only regular files are read
#: (never a directory, device node, or FIFO).
FILE_SCHEME = "file://"

_URI_SCHEMES = ("s3://", "http://", "https://", FILE_SCHEME)

_s3_client = None


class PayloadReferenceError(Exception):
    """A Payload_Reference could not be resolved, fetched, or decoded.

    The message identifies the source (URI, path segment, or
    ``"base64 payload data"``) and the reason — never the image bytes
    (Requirement 3.8). The Bedrock processor records it as the node's
    error outcome (Requirement 3.5)."""


def resolve_payload_path(payload_json: Any, dotted_path: str) -> Any:
    """The value at ``dotted_path`` inside ``payload_json``.

    Path segments are split on ``.``; each segment is a dict key or a
    non-negative integer list index (``refs.0.image``). An unresolvable
    path raises :class:`PayloadReferenceError` naming the failing
    segment (Requirement 3.5's "path does not resolve" reason).
    """
    path = str(dotted_path or "").strip()
    if not path:
        raise PayloadReferenceError(
            "reference_payload_path is empty: nothing to resolve"
        )
    current = payload_json
    consumed = []
    for segment in path.split("."):
        location = (
            "'" + ".".join(consumed) + "'" if consumed else "the payload root"
        )
        if isinstance(current, dict):
            if segment not in current:
                raise PayloadReferenceError(
                    "payload path '{0}': key '{1}' not found at {2}".format(
                        path, segment, location
                    )
                )
            current = current[segment]
        elif isinstance(current, (list, tuple)):
            if not segment.isdigit():
                raise PayloadReferenceError(
                    "payload path '{0}': segment '{1}' is not a "
                    "non-negative list index at {2}".format(
                        path, segment, location
                    )
                )
            index = int(segment)
            if index >= len(current):
                raise PayloadReferenceError(
                    "payload path '{0}': index {1} is out of range at "
                    "{2} ({3} entrie(s) available)".format(
                        path, index, location, len(current)
                    )
                )
            current = current[index]
        else:
            raise PayloadReferenceError(
                "payload path '{0}': segment '{1}' cannot descend into "
                "the {2} at {3}".format(
                    path, segment, type(current).__name__, location
                )
            )
        consumed.append(segment)
    return current


def describe_reference_source(value: Any) -> str:
    """The loggable source string for a resolved payload value: the URI
    itself for URI values, :data:`BASE64_SOURCE` for everything else —
    never decoded bytes (Requirement 3.8)."""
    if isinstance(value, str) and value.startswith(_URI_SCHEMES):
        return value
    return BASE64_SOURCE


def fetch_reference_bytes(
    value: Any,
    allowed_prefixes: Optional[Iterable[str]] = None,
    s3_client=None,
) -> bytes:
    """Reference-image bytes for a resolved payload value.

    ``s3://`` and ``http(s)://`` values fetch the referenced object
    (Requirement 3.2), gated by ``allowed_prefixes`` (an iterable of
    URI prefixes; empty or ``None`` permits any source — Requirement
    3.4). ``data:`` URLs and bare base64 strings decode locally
    (Requirement 3.3, no gate). Every result is bounded by
    :data:`MAX_REFERENCE_BYTES` and validated as a decodable image
    before it is returned (Requirement 3.7).

    Raises :class:`PayloadReferenceError` identifying the source and
    the reason on any failure — never the bytes (Requirement 3.8).
    ``s3_client`` is injectable for tests; by default a lazily created
    boto3 client using the device's ambient AWS credentials.
    """
    if not isinstance(value, str):
        raise PayloadReferenceError(
            "resolved payload value is not a string (got {0}); expected "
            "an s3://, http(s):// URI, a data: URL, or base64 image "
            "data".format(type(value).__name__)
        )
    if value.startswith(FILE_SCHEME):
        # Stricter gate than the remote schemes (see FILE_SCHEME).
        data = _fetch_file(value, allowed_prefixes)
        source = value
    elif value.startswith(_URI_SCHEMES):
        _check_allowed(value, allowed_prefixes)
        if value.startswith("s3://"):
            data = _fetch_s3(value, s3_client)
        else:
            data = _fetch_http(value)
        source = value
    elif value.startswith("data:"):
        data = _decode_data_url(value)
        source = BASE64_SOURCE
    else:
        data = _decode_bare_base64(value)
        source = BASE64_SOURCE
    if len(data) > MAX_REFERENCE_BYTES:
        raise PayloadReferenceError(
            "reference from {0} exceeds the {1}-byte size cap".format(
                _quote(source), MAX_REFERENCE_BYTES
            )
        )
    _validate_image(source, data)
    return data


# ---------------------------------------------------------------------------
# Prefix gate (Requirement 3.4)
# ---------------------------------------------------------------------------

def _check_allowed(
    source: str, allowed_prefixes: Optional[Iterable[str]]
) -> None:
    """Raise :class:`PayloadReferenceError` when prefixes are declared
    and none matches the source; an empty declaration permits every
    source (Requirement 3.4)."""
    prefixes: Sequence[str] = tuple(
        str(p) for p in (allowed_prefixes or ())
    )
    if not prefixes:
        return
    for prefix in prefixes:
        if source.startswith(prefix):
            return
    raise PayloadReferenceError(
        "reference fetch of '{0}' denied: the source is outside the "
        "node's allowed URI prefixes".format(source)
    )


# ---------------------------------------------------------------------------
# Fetchers and decoders
# ---------------------------------------------------------------------------

def _fetch_http(source: str) -> bytes:
    """HTTP(S) fetch with the bounded timeout (Requirement 3.7);
    non-success status, timeout, and connection failures raise
    :class:`PayloadReferenceError` naming the source."""
    try:
        with urllib.request.urlopen(
            source, timeout=REFERENCE_FETCH_TIMEOUT_SEC
        ) as response:
            status = getattr(response, "status", None)
            if status is None:  # pragma: no cover - pre-3.9 fallback
                status = response.getcode()
            if not 200 <= int(status) < 300:
                raise PayloadReferenceError(
                    "could not fetch reference '{0}': HTTP status {1} "
                    "is not a success".format(source, status)
                )
            data = response.read(MAX_REFERENCE_BYTES + 1)
    except PayloadReferenceError:
        raise
    except Exception as e:
        raise PayloadReferenceError(
            "could not fetch reference '{0}': {1}".format(source, e)
        )
    return data


def _fetch_s3(source: str, s3_client=None) -> bytes:
    """``s3://bucket/key`` fetch through boto3 ``get_object``, streamed
    in chunks so the size cap abandons an oversized object early
    (Requirement 3.7)."""
    remainder = source[len("s3://"):]
    bucket, _, key = remainder.partition("/")
    if not bucket or not key:
        raise PayloadReferenceError(
            "malformed S3 reference URI '{0}' (expected "
            "s3://bucket/key)".format(source)
        )
    if s3_client is None:
        try:
            import boto3
        except ImportError:
            raise PayloadReferenceError(
                "cannot fetch reference '{0}': boto3 is not "
                "installed".format(source)
            )
        global _s3_client
        if _s3_client is None:
            _s3_client = boto3.client("s3")
        s3_client = _s3_client
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"]
        chunks = []
        total = 0
        while True:
            chunk = body.read(_S3_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REFERENCE_BYTES:
                raise PayloadReferenceError(
                    "reference from '{0}' exceeds the {1}-byte size "
                    "cap".format(source, MAX_REFERENCE_BYTES)
                )
            chunks.append(chunk)
    except PayloadReferenceError:
        raise
    except Exception as e:
        raise PayloadReferenceError(
            "could not fetch reference '{0}': {1}".format(source, e)
        )
    return b"".join(chunks)


def _local_path(source: str) -> str:
    """The absolute local path a ``file://`` reference names.

    Accepts ``file:///abs/path`` (empty authority) and ``file://
    localhost/abs/path``; any other authority is refused — a remote host
    is not a local file and must not be silently read from disk."""
    remainder = source[len(FILE_SCHEME):]
    authority, sep, tail = remainder.partition("/")
    if authority not in ("", "localhost"):
        raise PayloadReferenceError(
            "malformed local reference URI '{0}': only file:/// or "
            "file://localhost/ are supported (got authority "
            "'{1}')".format(source, authority)
        )
    path = "/" + tail if sep else ""
    if not path or path == "/":
        raise PayloadReferenceError(
            "malformed local reference URI '{0}' (expected "
            "file:///absolute/path)".format(source)
        )
    return path


def _allowed_file_roots(allowed_prefixes):
    """The canonicalized directory roots ``file://`` reads are confined
    to, taken from the node's ``allowed_uri_prefixes`` entries that name
    the file scheme. Empty means nothing is permitted."""
    roots = []
    for prefix in (allowed_prefixes or ()):
        text = str(prefix).strip()
        if not text.startswith(FILE_SCHEME):
            continue
        raw = text[len(FILE_SCHEME):]
        if raw.startswith("localhost/"):
            raw = raw[len("localhost"):]
        if not raw.startswith("/"):
            continue
        roots.append(os.path.realpath(raw))
    return roots


def _fetch_file(source: str, allowed_prefixes) -> bytes:
    """Read a local reference image named by a ``file://`` URI.

    The allow-list is MANDATORY here: with no ``file://`` prefix
    configured the fetch is refused, because the URI comes from the
    run's Trigger_Context (untrusted MQTT input) and the bytes are sent
    to a cloud model. The path is canonicalized with ``realpath`` before
    the prefix check, so ``..`` segments and symlinks cannot escape the
    allowed root, and only regular files are read.
    """
    path = _local_path(source)
    roots = _allowed_file_roots(allowed_prefixes)
    if not roots:
        raise PayloadReferenceError(
            "local reference fetch of '{0}' denied: reading device files "
            "requires an explicit file:// entry in the node's allowed "
            "URI prefixes (an empty allow-list permits remote sources "
            "but never local files)".format(source)
        )
    resolved = os.path.realpath(path)
    if not any(
        resolved == root or resolved.startswith(root.rstrip("/") + "/")
        for root in roots
    ):
        raise PayloadReferenceError(
            "local reference fetch of '{0}' denied: the resolved path is "
            "outside the node's allowed file:// prefixes".format(source)
        )
    try:
        st = os.stat(resolved)
    except OSError as e:
        raise PayloadReferenceError(
            "could not read local reference '{0}': {1}".format(
                source, e.strerror or e
            )
        )
    if not stat.S_ISREG(st.st_mode):
        raise PayloadReferenceError(
            "local reference '{0}' is not a regular file".format(source)
        )
    if st.st_size > MAX_REFERENCE_BYTES:
        raise PayloadReferenceError(
            "reference from '{0}' exceeds the {1}-byte size cap".format(
                source, MAX_REFERENCE_BYTES
            )
        )
    try:
        with open(resolved, "rb") as handle:
            data = handle.read(MAX_REFERENCE_BYTES + 1)
    except OSError as e:
        raise PayloadReferenceError(
            "could not read local reference '{0}': {1}".format(
                source, e.strerror or e
            )
        )
    return data


def _decode_data_url(value: str) -> bytes:
    """``data:<mediatype>;base64,<payload>`` -> decoded bytes
    (Requirement 3.3). Non-base64 data URLs and undecodable payloads
    raise :class:`PayloadReferenceError` (source recorded as
    :data:`BASE64_SOURCE`, never the payload — Requirement 3.8)."""
    header, sep, payload = value.partition(",")
    if not sep or not header[len("data:"):].lower().endswith(";base64"):
        raise PayloadReferenceError(
            "unsupported data: URL in the payload: only base64-encoded "
            "data: URLs (data:<mediatype>;base64,...) are supported"
        )
    try:
        return base64.b64decode(payload.strip(), validate=True)
    except Exception as e:
        raise PayloadReferenceError(
            "could not decode {0} (data: URL): {1}".format(
                BASE64_SOURCE, e
            )
        )


def _decode_bare_base64(value: str) -> bytes:
    """A bare base64 string -> decoded bytes (Requirement 3.3).
    Embedded whitespace/newlines are tolerated; anything undecodable
    raises :class:`PayloadReferenceError` without echoing the value."""
    cleaned = "".join(value.split())
    try:
        return base64.b64decode(cleaned, validate=True)
    except Exception:
        raise PayloadReferenceError(
            "resolved payload value is neither a supported URI "
            "(s3://, http://, https://, data:) nor valid base64 "
            "image data"
        )


def _validate_image(source: str, data: bytes) -> None:
    """Raise :class:`PayloadReferenceError` naming the source when the
    bytes do not decode as an image (``cv2.imdecode``) — the Converse
    request must never carry a non-image reference."""
    import cv2
    import numpy as np

    array = np.frombuffer(data, dtype=np.uint8)
    if array.size == 0 or cv2.imdecode(array, cv2.IMREAD_UNCHANGED) is None:
        raise PayloadReferenceError(
            "reference from {0} is not a decodable image".format(
                _quote(source)
            )
        )


def _quote(source: str) -> str:
    """URIs quoted for readability; the base64 description bare."""
    return source if source == BASE64_SOURCE else "'" + source + "'"
