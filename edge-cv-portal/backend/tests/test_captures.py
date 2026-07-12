"""
Unit tests for the portal captures endpoint (functions/captures.py).

Task 8.2 (spec: object-detection-visualization). Covers metadata parsing for
detection / zero-object / anomaly captures, presigned-URL assembly, numeric key
ordering, and graceful handling of missing / unparseable artifacts.
_Requirements: 4.3, 4.5_

The captures module imports a shared Lambda layer via
`sys.path.append('/opt/python'); from shared_utils import ...` and creates a
module-level `boto3.client('s3')` at import time. This test is self-contained:
it injects a fake `shared_utils` module into `sys.modules` and puts the
`functions/` dir on the path BEFORE importing `captures`, and stubs the S3
client so no AWS calls are made.
"""
import base64
import json
import os
import sys
import types

import pytest

# --------------------------------------------------------------------------- #
# Import shim: inject a fake `shared_utils` and expose functions/ on the path
# BEFORE importing the module under test.
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS_DIR = os.path.abspath(os.path.join(_HERE, "..", "functions"))
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


def _make_fake_shared_utils():
    """Build a stand-in for the shared Lambda layer's `shared_utils` module."""
    mod = types.ModuleType("shared_utils")

    def create_response(status_code, body, headers=None):
        # Mirror the real layer: body is JSON-encoded unless already a string.
        return {
            "statusCode": status_code,
            "headers": headers or {},
            "body": body if isinstance(body, str) else json.dumps(body),
        }

    def handle_error(error, message_or_headers="Operation failed"):
        return {
            "statusCode": 500,
            "headers": {},
            "body": json.dumps({"error": str(message_or_headers), "detail": str(error)}),
        }

    def get_usecase(usecase_id):
        return {
            "usecase_id": usecase_id,
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:role/dda",
            "external_id": "ext-id",
        }

    def assume_usecase_role(role_arn, external_id, session_name):
        return {
            "AccessKeyId": "AKIAFAKE",
            "SecretAccessKey": "secretfake",
            "SessionToken": "tokenfake",
        }

    mod.create_response = create_response
    mod.handle_error = handle_error
    mod.get_usecase = get_usecase
    mod.assume_usecase_role = assume_usecase_role
    return mod


sys.modules.setdefault("shared_utils", _make_fake_shared_utils())

import captures  # noqa: E402  (import after shim is installed)


# --------------------------------------------------------------------------- #
# Fake S3 client
# --------------------------------------------------------------------------- #
class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class _FakePaginator:
    def __init__(self, keys):
        self._keys = list(keys)

    def paginate(self, Bucket=None, Prefix=None):
        contents = [
            {"Key": k}
            for k in self._keys
            if Prefix is None or k.startswith(Prefix)
        ]
        # Split across two pages to exercise multi-page handling.
        mid = len(contents) // 2
        yield {"Contents": contents[:mid]}
        yield {"Contents": contents[mid:]}


class FakeS3:
    """A minimal fake S3 client covering only what captures.py uses."""

    def __init__(self, objects=None, presign_raises=False):
        # objects: {key: bytes}. get_object raises NoSuchKey for missing keys.
        self._objects = dict(objects or {})
        self._presign_raises = presign_raises
        self.presigned_calls = []

    def get_object(self, Bucket=None, Key=None):
        if Key not in self._objects:
            raise KeyError(f"NoSuchKey: {Key}")
        return {"Body": _FakeBody(self._objects[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(self._objects.keys())

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        self.presigned_calls.append((op, Params, ExpiresIn))
        if self._presign_raises:
            raise RuntimeError("presign boom")
        return f"https://signed.example/{Params['Key']}"


# --------------------------------------------------------------------------- #
# Metadata builders (mirror the Marshal capture-metadata shape)
# --------------------------------------------------------------------------- #
def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")


def _detection_entry(class_index, class_label, box, conf):
    return {
        "class_index": class_index,
        "class_label": class_label,
        "bounding_box": box,
        "confidence": conf,
    }


def build_metadata(summary=None, block=None, extra_outputs=None):
    """Build a Capture_Metadata dict with the given aux outputs."""
    outputs = []
    if summary is not None:
        outputs.append({"observedContentType": "json", "data": _b64(summary)})
    if block is not None:
        outputs.append(
            {"observedContentType": "json_with_base64_encoding", "data": _b64(block)}
        )
    if extra_outputs:
        outputs.extend(extra_outputs)
    return {"deviceFleetAuxiliaryOutputs": outputs}


def build_jsonl(metadata) -> bytes:
    return (json.dumps(metadata) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# _extract_inference_data
# --------------------------------------------------------------------------- #
def test_detection_metadata_parsing():
    """Detection summary + detections block -> Detection, count 2, ordered list."""
    summary = {"Inference result": "Detection", "Detection_count": 2, "Confidence": 0.9}
    block = {
        "detections": {
            "0": _detection_entry("17", "dog", [12, 40, 220, 310], 0.83),
            "1": _detection_entry("2", "car", [5, 6, 7, 8], 0.71),
        }
    }
    metadata = build_metadata(summary=summary, block=block)

    result_type, count, detections = captures._extract_inference_data(metadata)

    assert result_type == "Detection"
    assert count == 2
    assert len(detections) == 2
    assert detections[0] == {
        "class_index": "17",
        "class_label": "dog",
        "bounding_box": [12, 40, 220, 310],
        "confidence": 0.83,
    }
    assert detections[1]["class_label"] == "car"


def test_zero_object_detection_metadata():
    """Detection_count 0 with empty detections map -> Detection, 0, []."""
    summary = {"Inference result": "Detection", "Detection_count": 0, "Confidence": 0.0}
    block = {"detections": {}}
    metadata = build_metadata(summary=summary, block=block)

    result_type, count, detections = captures._extract_inference_data(metadata)

    assert result_type == "Detection"
    assert count == 0
    assert detections == []


def test_anomaly_capture_metadata():
    """Anomaly summary + anomalies block (no detections map) -> empty detections."""
    summary = {"Inference result": "Anomaly", "Confidence": 0.6}
    block = {"anomalies": {"0": {"label": "scratch", "score": 0.6}}}
    metadata = build_metadata(summary=summary, block=block)

    result_type, count, detections = captures._extract_inference_data(metadata)

    assert result_type == "Anomaly"
    assert count == 0
    assert detections == []


def test_extract_handles_missing_outputs():
    """Metadata with no aux outputs degrades to (None, 0, [])."""
    result_type, count, detections = captures._extract_inference_data({})
    assert result_type is None
    assert count == 0
    assert detections == []


def test_extract_handles_bad_base64_block():
    """A corrupt detections block is skipped without raising."""
    summary = {"Inference result": "Detection", "Detection_count": 1}
    metadata = {
        "deviceFleetAuxiliaryOutputs": [
            {"observedContentType": "json", "data": _b64(summary)},
            {"observedContentType": "json_with_base64_encoding", "data": "!!!not-base64!!!"},
        ]
    }
    result_type, count, detections = captures._extract_inference_data(metadata)
    assert result_type == "Detection"
    assert count == 1
    assert detections == []


# --------------------------------------------------------------------------- #
# _normalize_detections
# --------------------------------------------------------------------------- #
def test_normalize_detections_numeric_key_ordering():
    """Numeric string keys (>10) are ordered numerically, not lexicographically."""
    det_map = {
        str(i): _detection_entry(str(i), f"c{i}", [i, i, i + 1, i + 1], i / 100.0)
        for i in range(13)
    }
    # Present the map in shuffled / lexicographic-unfriendly order.
    shuffled = {k: det_map[k] for k in ["10", "2", "0", "11", "1", "12"] +
                [str(i) for i in range(3, 10)]}

    detections = captures._normalize_detections(shuffled)

    indices = [d["class_index"] for d in detections]
    assert indices == [str(i) for i in range(13)]


def test_normalize_detections_skips_non_dict_entries():
    det_map = {"0": _detection_entry("1", "a", [1, 2, 3, 4], 0.5), "1": "not-a-dict"}
    detections = captures._normalize_detections(det_map)
    assert len(detections) == 1
    assert detections[0]["class_label"] == "a"


# --------------------------------------------------------------------------- #
# _load_last_metadata
# --------------------------------------------------------------------------- #
def test_load_last_metadata_uses_last_nonempty_line():
    body = (
        json.dumps({"n": 1}) + "\n"
        + "\n"
        + json.dumps({"n": 2}) + "\n"
    ).encode("utf-8")
    assert captures._load_last_metadata(body) == {"n": 2}


def test_load_last_metadata_empty_body_returns_none():
    assert captures._load_last_metadata(b"") is None


def test_load_last_metadata_bad_json_raises():
    with pytest.raises(json.JSONDecodeError):
        captures._load_last_metadata(b"{not json}")


# --------------------------------------------------------------------------- #
# _presign_if_exists
# --------------------------------------------------------------------------- #
def test_presign_returns_url_when_key_present():
    s3 = FakeS3()
    all_keys = {"dev/cap/abc.jpg"}
    url = captures._presign_if_exists(s3, "bucket", "dev/cap/abc.jpg", all_keys)
    assert url == "https://signed.example/dev/cap/abc.jpg"
    assert s3.presigned_calls  # generate_presigned_url was invoked


def test_presign_returns_none_when_key_missing():
    s3 = FakeS3()
    all_keys = {"dev/cap/abc.jpg"}
    url = captures._presign_if_exists(s3, "bucket", "dev/cap/abc.overlay.jpg", all_keys)
    assert url is None
    assert not s3.presigned_calls  # skipped entirely for missing keys


def test_presign_returns_none_when_generation_raises():
    s3 = FakeS3(presign_raises=True)
    all_keys = {"dev/cap/abc.jpg"}
    url = captures._presign_if_exists(s3, "bucket", "dev/cap/abc.jpg", all_keys)
    assert url is None


# --------------------------------------------------------------------------- #
# _parse_capture
# --------------------------------------------------------------------------- #
def test_parse_capture_detection_with_all_artifacts():
    summary = {"Inference result": "Detection", "Detection_count": 1}
    block = {"detections": {"0": _detection_entry("17", "dog", [1, 2, 3, 4], 0.9)}}
    metadata = build_metadata(summary=summary, block=block)
    base = "dev/folder/cap123"
    objects = {
        f"{base}.jsonl": build_jsonl(metadata),
        f"{base}.jpg": b"src",
        f"{base}.overlay.jpg": b"ovl",
    }
    all_keys = set(objects.keys())
    s3 = FakeS3(objects=objects)

    capture = captures._parse_capture(s3, "bucket", f"{base}.jsonl", all_keys)

    assert capture["capture_id"] == "cap123"
    assert capture["inference_result_type"] == "Detection"
    assert capture["detection_count"] == 1
    assert len(capture["detections"]) == 1
    assert capture["source_url"] == f"https://signed.example/{base}.jpg"
    assert capture["overlay_url"] == f"https://signed.example/{base}.overlay.jpg"
    # mask.png is absent from the listed keys -> null URL
    assert capture["mask_url"] is None


def test_parse_capture_missing_overlay_and_mask_null_urls():
    summary = {"Inference result": "Detection", "Detection_count": 0}
    block = {"detections": {}}
    metadata = build_metadata(summary=summary, block=block)
    base = "dev/folder/capZero"
    objects = {
        f"{base}.jsonl": build_jsonl(metadata),
        f"{base}.jpg": b"src",
    }
    s3 = FakeS3(objects=objects)

    capture = captures._parse_capture(s3, "bucket", f"{base}.jsonl", set(objects.keys()))

    assert capture["inference_result_type"] == "Detection"
    assert capture["detection_count"] == 0
    assert capture["detections"] == []
    assert capture["source_url"] is not None
    assert capture["overlay_url"] is None
    assert capture["mask_url"] is None


def test_parse_capture_get_object_raises_degrades_gracefully():
    """Metadata fetch failure -> entry with empty detections, not an exception."""
    base = "dev/folder/broken"
    # jsonl key is in all_keys (so it was listed) but get_object has no body for it.
    all_keys = {f"{base}.jsonl", f"{base}.jpg"}
    s3 = FakeS3(objects={f"{base}.jpg": b"src"})  # no jsonl object -> get_object raises

    capture = captures._parse_capture(s3, "bucket", f"{base}.jsonl", all_keys)

    assert capture is not None
    assert capture["capture_id"] == "broken"
    assert capture["inference_result_type"] is None
    assert capture["detection_count"] == 0
    assert capture["detections"] == []
    assert capture["source_url"] is not None


def test_parse_capture_unparseable_metadata_degrades_gracefully():
    base = "dev/folder/badjson"
    objects = {
        f"{base}.jsonl": b"{this is : not json}",
        f"{base}.jpg": b"src",
    }
    s3 = FakeS3(objects=objects)

    capture = captures._parse_capture(s3, "bucket", f"{base}.jsonl", set(objects.keys()))

    assert capture["inference_result_type"] is None
    assert capture["detections"] == []
    assert capture["source_url"] is not None


# --------------------------------------------------------------------------- #
# list_captures (end-to-end through the handler path, S3 client stubbed)
# --------------------------------------------------------------------------- #
def test_list_captures_returns_parsed_captures(monkeypatch):
    det_meta = build_metadata(
        summary={"Inference result": "Detection", "Detection_count": 1},
        block={"detections": {"0": _detection_entry("17", "dog", [1, 2, 3, 4], 0.9)}},
    )
    anom_meta = build_metadata(
        summary={"Inference result": "Anomaly"},
        block={"anomalies": {"0": {"label": "scratch"}}},
    )
    objects = {
        "dev/cap1.jsonl": build_jsonl(det_meta),
        "dev/cap1.jpg": b"src1",
        "dev/cap1.overlay.jpg": b"ovl1",
        "dev/cap2.jsonl": build_jsonl(anom_meta),
        "dev/cap2.jpg": b"src2",
        "dev/cap2.mask.png": b"mask2",
    }
    fake = FakeS3(objects=objects)
    monkeypatch.setattr(captures.boto3, "client", lambda *a, **k: fake)

    event = {"queryStringParameters": {"usecase_id": "uc1", "prefix": "dev"}}
    response = captures.list_captures(event)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["total_found"] == 2
    caps = {c["capture_id"]: c for c in payload["captures"]}
    assert set(caps) == {"cap1", "cap2"}
    assert caps["cap1"]["inference_result_type"] == "Detection"
    assert caps["cap1"]["overlay_url"] is not None
    assert caps["cap1"]["mask_url"] is None
    assert caps["cap2"]["inference_result_type"] == "Anomaly"
    assert caps["cap2"]["detections"] == []
    assert caps["cap2"]["mask_url"] is not None
    assert caps["cap2"]["overlay_url"] is None


def test_list_captures_requires_usecase_and_prefix():
    response = captures.list_captures({"queryStringParameters": {"usecase_id": "uc1"}})
    assert response["statusCode"] == 400


def test_list_captures_respects_limit(monkeypatch):
    objects = {}
    for i in range(5):
        meta = build_metadata(
            summary={"Inference result": "Detection", "Detection_count": 0},
            block={"detections": {}},
        )
        objects[f"dev/cap{i}.jsonl"] = build_jsonl(meta)
        objects[f"dev/cap{i}.jpg"] = b"src"
    fake = FakeS3(objects=objects)
    monkeypatch.setattr(captures.boto3, "client", lambda *a, **k: fake)

    event = {"queryStringParameters": {"usecase_id": "uc1", "prefix": "dev", "limit": "2"}}
    response = captures.list_captures(event)

    payload = json.loads(response["body"])
    assert payload["total_found"] == 5
    assert len(payload["captures"]) == 2
