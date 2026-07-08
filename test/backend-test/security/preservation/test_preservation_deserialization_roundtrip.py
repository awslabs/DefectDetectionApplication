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
"""#5 / #6 / #7 deserialization round-trip preservation baseline (Req 3.5).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

The three pickle/dill sites round-trip a well-defined structure produced by a
trusted in-process/config producer:

  * #5 reference-image map: ``dill.load`` -> ``{'image_index': {path: feature}}``;
    the postprocessor builds ``train_feature_gallery = np.vstack([...])`` and the
    ordered ``reference_image_paths = [...]``.
  * #6 camera frame: ``pickle.loads`` -> ``{'data': bytes, 'height': int,
    'width': int}`` (and ``None`` on timeout/failure).
  * #7 DIO health message: ``pickle.loads`` -> ``{'status': <enum>, 'error_type':
    <None|str>, 'last_updated': <float>}``.

The fix replaces these with a safe serialization (JSON + numpy ``allow_pickle=
False`` for #5, framed JSON for #7, a non-executable header + raw bytes / numpy
``frombuffer`` for #6) while keeping the exact in-memory structure.

Per the design's PBT plan, the preservation invariant is:
    **safe-format round-trip  ==  the pickle/dill round-trip of F**
for every legitimate payload. This file implements BOTH round-trips — the current
``pickle``/``dill`` round-trip (F) and the proposed safe-format round-trip (the
design's chosen format) — and asserts they yield the identical consumer
structure across a generated payload domain. It PASSES now (both round-trips are
computed here) and re-runs unchanged in task 13; the wiring of the safe format
into the production modules is exercised by the fix's own unit + integration
tests (tasks 8/9/7 and 14).

**Validates: Requirements 3.5**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_deserialization_roundtrip.py \
        -p no:cacheprovider --noconftest -v
"""
import io
import json
import pickle
import struct

import numpy as np
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

import dill

from _preservation_support import load_module_from_path

# Real health-status enum (common.py only imports `enum`, safe to load alone).
_common = load_module_from_path(
    "common_preservation", "src/backend/utils/common.py"
)
DIOProcessHealthStatusEnum = _common.DIOProcessHealthStatusEnum


# =========================================================================== #
# #6 camera frame — {'data': bytes, 'height': int, 'width': int} (+ None)
# =========================================================================== #
def pickle_roundtrip_frame(frame):
    """F: Camera.get_frame() returns pickle.dumps(frame); _get_camera_frame does
    pickle.loads(...)."""
    return pickle.loads(pickle.dumps(frame))


def safe_encode_frame(frame):
    """Proposed safe transport: a length-prefixed JSON header (height/width, or a
    null marker) followed by the raw ``data`` bytes. No executable payload."""
    if frame is None:
        header = json.dumps({"null": True}).encode("utf-8")
        return struct.pack(">I", len(header)) + header
    header = json.dumps(
        {"null": False, "height": frame["height"], "width": frame["width"]}
    ).encode("utf-8")
    return struct.pack(">I", len(header)) + header + frame["data"]


def safe_decode_frame(buf):
    (hlen,) = struct.unpack(">I", buf[:4])
    header = json.loads(buf[4:4 + hlen].decode("utf-8"))
    if header.get("null"):
        return None
    data = buf[4 + hlen:]
    return {"data": data, "height": header["height"], "width": header["width"]}


# Validates: Requirements 3.5
def test_camera_frame_none_roundtrip_baseline():
    """Timeout/failure path: pickle round-trips ``None`` -> ``None``; the safe
    transport must too."""
    assert pickle_roundtrip_frame(None) is None
    assert safe_decode_frame(safe_encode_frame(None)) is None


# Validates: Requirements 3.5
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.binary(min_size=0, max_size=4096),
       height=st.integers(min_value=1, max_value=4320),
       width=st.integers(min_value=1, max_value=8192))
def test_camera_frame_roundtrip_equivalence_property(data, height, width):
    """Invariant: for any legitimate frame the safe-format round-trip equals the
    pickle round-trip of F (identical ``{'data','height','width'}`` dict)."""
    frame = {"data": data, "height": height, "width": width}
    f_result = pickle_roundtrip_frame(frame)
    safe_result = safe_decode_frame(safe_encode_frame(frame))

    assert f_result == frame  # F preserves the dict
    assert safe_result == f_result
    assert safe_result["data"] == data
    assert safe_result["height"] == height
    assert safe_result["width"] == width


# =========================================================================== #
# #7 DIO health message — {'status': enum, 'error_type': None|str, 'last_updated'}
# =========================================================================== #
def pickle_roundtrip_health(message):
    """F: __update_health_status pickles the message into shared memory;
    get_dio_process_health_report does pickle.loads(shm.buf)."""
    return pickle.loads(pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL))


def safe_encode_health(message):
    """Proposed framed-JSON transport: status -> enum name, error_type -> str form
    (or None), last_updated -> float; 4-byte length header + UTF-8 JSON."""
    payload = {
        "status": message["status"].name,
        "error_type": None if message["error_type"] is None else str(message["error_type"]),
        "last_updated": message["last_updated"],
    }
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def safe_decode_health(buf):
    (blen,) = struct.unpack(">I", buf[:4])
    payload = json.loads(buf[4:4 + blen].decode("utf-8"))
    return {
        "status": DIOProcessHealthStatusEnum[payload["status"]],
        "error_type": payload["error_type"],
        "last_updated": payload["last_updated"],
    }


# Validates: Requirements 3.5
def test_dio_health_message_example_baseline():
    """A STARTING message with no error round-trips to the identical structure."""
    message = {
        "status": DIOProcessHealthStatusEnum.STARTING,
        "error_type": None,
        "last_updated": 1700000000.5,
    }
    f_result = pickle_roundtrip_health(message)
    safe_result = safe_decode_health(safe_encode_health(message))

    assert f_result == message
    assert safe_result == message
    assert safe_result["status"] is DIOProcessHealthStatusEnum.STARTING
    assert safe_result["error_type"] is None
    assert safe_result["last_updated"] == 1700000000.5


# Validates: Requirements 3.5
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(status=st.sampled_from(list(DIOProcessHealthStatusEnum)),
       error_type=st.one_of(st.none(), st.text(max_size=120)),
       last_updated=st.floats(min_value=0, max_value=2_000_000_000,
                              allow_nan=False, allow_infinity=False))
def test_dio_health_message_roundtrip_equivalence_property(status, error_type, last_updated):
    """Invariant: for any legitimate health message (status enum, None|str
    error_type, float timestamp) the framed-JSON round-trip yields the identical
    consumer dict as F's pickle round-trip."""
    message = {"status": status, "error_type": error_type, "last_updated": last_updated}
    f_result = pickle_roundtrip_health(message)
    safe_result = safe_decode_health(safe_encode_health(message))

    # F preserves the dict exactly.
    assert f_result == message
    # The safe format reconstructs the same consumer structure.
    assert safe_result["status"] is status
    assert safe_result["error_type"] == error_type
    assert safe_result["last_updated"] == last_updated


# =========================================================================== #
# #5 reference-image map — vstack gallery + ordered paths
# =========================================================================== #
def build_gallery_from_map(data):
    """The exact transform the postprocessor applies after dill.load:
        train_feature_gallery = np.vstack([...]); reference_image_paths = [...]"""
    image_index = data["image_index"]
    gallery = []
    paths = []
    for path, feature in image_index.items():
        gallery.append(feature)
        paths.append(path)
    return np.vstack(gallery), paths


def dill_roundtrip_map(data):
    """F: dill.dump/dill.load of the reference-image map."""
    buf = io.BytesIO()
    dill.dump(data, buf)
    buf.seek(0)
    return dill.load(buf)


def safe_roundtrip_map(data):
    """Proposed safe format: JSON sidecar for the ordered paths + a numpy matrix
    of stacked features loaded with allow_pickle=False. Reconstructs the same
    ``{'image_index': {path: feature}}`` ordering."""
    image_index = data["image_index"]
    paths = list(image_index.keys())
    matrix = np.vstack([image_index[p] for p in paths]) if paths else np.empty((0, 0))

    paths_json = json.dumps(paths)
    npbuf = io.BytesIO()
    np.save(npbuf, matrix, allow_pickle=False)
    npbuf.seek(0)

    loaded_paths = json.loads(paths_json)
    loaded_matrix = np.load(npbuf, allow_pickle=False)
    rebuilt = {"image_index": {p: loaded_matrix[i] for i, p in enumerate(loaded_paths)}}
    return rebuilt


# Feature vectors: fixed-length float rows (so np.vstack yields a matrix).
_FEATURE_LEN = 4


def _feature_map(paths, rows):
    return {"image_index": {p: np.asarray(r, dtype=np.float64)
                            for p, r in zip(paths, rows)}}


# Validates: Requirements 3.5
def test_reference_image_map_example_baseline():
    """A small legitimate reference map yields the recorded vstack gallery + the
    ordered paths, identically for the dill and safe-format round-trips."""
    data = _feature_map(
        ["ref/a.png", "ref/b.png", "ref/c.png"],
        [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]],
    )
    f_gallery, f_paths = build_gallery_from_map(dill_roundtrip_map(data))
    s_gallery, s_paths = build_gallery_from_map(safe_roundtrip_map(data))

    assert f_paths == ["ref/a.png", "ref/b.png", "ref/c.png"]
    assert f_gallery.shape == (3, 4)
    assert s_paths == f_paths
    assert np.array_equal(s_gallery, f_gallery)


# Validates: Requirements 3.5
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_reference_image_map_roundtrip_equivalence_property(data):
    """Invariant: for any legitimate reference map, the safe-format round-trip
    yields the identical ``train_feature_gallery`` (np.vstack) and ordered
    ``reference_image_paths`` as F's dill round-trip."""
    n = data.draw(st.integers(min_value=1, max_value=12))
    paths = data.draw(
        st.lists(st.from_regex(r"\Aref/[a-z0-9_]{1,10}\.png\Z"),
                 min_size=n, max_size=n, unique=True)
    )
    rows = data.draw(
        st.lists(
            st.lists(st.floats(allow_nan=False, allow_infinity=False,
                               min_value=-1e6, max_value=1e6),
                     min_size=_FEATURE_LEN, max_size=_FEATURE_LEN),
            min_size=n, max_size=n,
        )
    )
    ref_map = _feature_map(paths, rows)

    f_gallery, f_paths = build_gallery_from_map(dill_roundtrip_map(ref_map))
    s_gallery, s_paths = build_gallery_from_map(safe_roundtrip_map(ref_map))

    assert s_paths == f_paths == paths
    assert s_gallery.shape == f_gallery.shape == (n, _FEATURE_LEN)
    assert np.array_equal(s_gallery, f_gallery)
