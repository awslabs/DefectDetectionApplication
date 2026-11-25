"""
Instance-segmentation captures on the Portal captures route
(functions/captures.py) -- rfdetr-seg-support task 11.2 (Requirements 11.1,
11.5, 13.4, 14.3; design §11 "captures.py", "Page cost", DD4).

Two request shapes on the one route:
- PAGE (no `key`): the light form -- boxes, `mask_area`, `mask_truncated`,
  `has_masks`, `detections_truncated`, `jsonl_key`, presigned URLs -- and never
  `mask_rle`, whatever `include_masks` says (the Portal MCP tool
  `portal_data_captures_list` shares the handler and has no per-field drop).
  The per-record work runs on a bounded ThreadPoolExecutor over
  order-preserving slices of `limit`.
- DETAIL (`key` present): `check_user_access` first, then the key validation
  (a `.jsonl` object under the scoped prefix, 1..1024 bytes) before any
  credential resolution, one `get_object` and no LIST, `mask_rle` per entry
  with `include_masks`, artifact URLs None, `total_found` 1.

Size bounds: a detail body for a Detection_List at the device's
DETECTION_LIST_MAX_BYTES ceiling (4 MiB) and a 50 x 300-box light page with
realistic float reprs both stay under the 6 MB Lambda synchronous response cap.

The import shim (fake `shared_utils`, functions/ on sys.path), `FakeS3` and the
metadata builders come from test_captures.py; this file adds a recording S3
fake (LIST calls, long presigned URLs) and instance fixtures.
"""
import json

import pytest

from test_captures import (  # noqa: E402  (installs the shared_utils shim)
    FakeS3,
    _detection_entry,
    build_jsonl,
    build_metadata,
)

import captures  # noqa: E402


# A Detection_List is capped on the device at 4 MiB
# (lfv_model_template.DETECTION_LIST_MAX_BYTES, Requirement 10.3 / DD4).
DETECTION_LIST_MAX_BYTES = 4 * 1024 * 1024
LAMBDA_RESPONSE_CAP = 6_000_000

RLE_A = "0 5 2 2 2 5"            # 4x4 border (shared vector `border_touching`)
RLE_B = "5 2 2 2 5"              # the 2x2 centre of a 4x4 frame
PREFIX = "dev"
DEVICE = "thing-1"
SEARCH_PREFIX = f"{PREFIX}/{DEVICE}"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class RecordingS3(FakeS3):
    """FakeS3 that counts LIST calls and can sign realistically long URLs
    (a SigV4 GET URL with a session token is ~1.2-1.8 KB)."""

    def __init__(self, objects=None, url_padding=0, raising_keys=()):
        super().__init__(objects=objects)
        self.list_calls = 0
        self._url_padding = url_padding
        self._raising_keys = set(raising_keys)

    def get_paginator(self, name):
        self.list_calls += 1
        return super().get_paginator(name)

    def get_object(self, Bucket=None, Key=None, ExpectedBucketOwner=None):
        if Key in self._raising_keys:
            raise RuntimeError(f"transient read failure: {Key}")
        return super().get_object(Bucket=Bucket, Key=Key, ExpectedBucketOwner=ExpectedBucketOwner)

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        url = super().generate_presigned_url(op, Params=Params, ExpiresIn=ExpiresIn)
        return url + "?X-Amz-Security-Token=" + "t" * self._url_padding if self._url_padding else url


def _instance_entry(index, label, box, conf, rle=None, area=None, truncated=False):
    entry = _detection_entry(str(index), label, box, conf)
    if rle is not None:
        entry["mask_rle"] = rle
    if area is not None:
        entry["mask_area"] = area
    if truncated:
        entry["mask_truncated"] = True
    return entry


def instance_block(entries, detections_truncated=False):
    block = {"detections": {str(i): e for i, e in enumerate(entries)}}
    if detections_truncated:
        block["detections_truncated"] = True
    return block


def instance_metadata(entries, detections_truncated=False, count=None):
    summary = {"Inference result": "Detection",
               "Detection_count": len(entries) if count is None else count}
    return build_metadata(summary=summary,
                          block=instance_block(entries, detections_truncated))


TWO_INSTANCES = [
    _instance_entry(0, "scratch", [1, 1, 3, 3], 0.91, rle=RLE_A, area=12),
    _instance_entry(1, "dent", [2, 2, 4, 4], 0.64, rle=RLE_B, area=4),
]
TRUNCATED_MIX = [
    _instance_entry(0, "scratch", [1, 1, 3, 3], 0.91, rle=RLE_A, area=12),
    _instance_entry(1, "dent", [2, 2, 4, 4], 0.64, area=4, truncated=True),
]
ALL_TRUNCATED = [
    _instance_entry(0, "scratch", [1, 1, 3, 3], 0.91, area=12, truncated=True),
    _instance_entry(1, "dent", [2, 2, 4, 4], 0.64, area=4, truncated=True),
]


def _objects(capture_id, metadata, with_mask_png=True):
    base = f"{SEARCH_PREFIX}/{capture_id}"
    objects = {f"{base}.jsonl": build_jsonl(metadata), f"{base}.jpg": b"src",
               f"{base}.overlay.jpg": b"ovl"}
    if with_mask_png:
        objects[f"{base}.mask.png"] = b"png"
    return objects


def _event(**params):
    query = {"usecase_id": "uc1", "prefix": PREFIX, "device_id": DEVICE}
    query.update(params)
    return {"queryStringParameters": query}


@pytest.fixture
def s3(monkeypatch):
    """Install a RecordingS3 as the per-request boto3 client; returns a setter."""
    def install(fake):
        monkeypatch.setattr(captures.boto3, "client", lambda *a, **k: fake)
        return fake

    return install


def _body(response):
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


# --------------------------------------------------------------------------- #
# Page: the light form
# --------------------------------------------------------------------------- #
def test_instance_capture_on_a_page_is_the_light_form(s3):
    fake = s3(RecordingS3(_objects("cap-a", instance_metadata(TWO_INSTANCES))))

    payload = _body(captures.list_captures(_event()))

    assert payload["total_found"] == 1
    cap = payload["captures"][0]
    assert cap["inference_result_type"] == "Detection"      # the device summary's value, verbatim
    assert cap["detection_count"] == 2
    assert cap["has_masks"] is True
    assert cap["detections_truncated"] is False
    assert cap["jsonl_key"] == f"{SEARCH_PREFIX}/cap-a.jsonl"
    assert cap["mask_url"] is not None                       # the Composite_Mask is presigned as today
    assert [d["mask_area"] for d in cap["detections"]] == [12, 4]
    for det in cap["detections"]:
        assert "mask_rle" not in det
        assert "mask_truncated" not in det                   # only when the raw entry carries it
        assert set(det) == {"class_index", "class_label", "bounding_box", "confidence", "mask_area"}
    assert fake.list_calls == 1


def test_page_ignores_include_masks(s3):
    s3(RecordingS3(_objects("cap-a", instance_metadata(TWO_INSTANCES))))

    for value in ("1", "true"):
        payload = _body(captures.list_captures(_event(include_masks=value)))
        cap = payload["captures"][0]
        assert cap["has_masks"] is True
        assert all("mask_rle" not in d for d in cap["detections"])


def test_detection_only_capture_keeps_its_four_key_entries_and_false_flags(s3):
    metadata = build_metadata(
        summary={"Inference result": "Detection", "Detection_count": 1},
        block={"detections": {"0": _detection_entry("17", "dog", [1, 2, 3, 4], 0.9)}})
    s3(RecordingS3(_objects("cap-d", metadata, with_mask_png=False)))

    cap = _body(captures.list_captures(_event(include_masks="1")))["captures"][0]

    assert cap["detections"] == [{"class_index": "17", "class_label": "dog",
                                  "bounding_box": [1, 2, 3, 4], "confidence": 0.9}]
    assert cap["has_masks"] is False
    assert cap["detections_truncated"] is False
    assert cap["jsonl_key"] == f"{SEARCH_PREFIX}/cap-d.jsonl"


def test_zero_instance_truncated_and_all_truncated_records(s3):
    objects = {}
    objects.update(_objects("cap-zero", instance_metadata([])))
    objects.update(_objects("cap-trunc", instance_metadata(TRUNCATED_MIX, detections_truncated=True)))
    objects.update(_objects("cap-alltrunc", instance_metadata(ALL_TRUNCATED, detections_truncated=True)))
    s3(RecordingS3(objects))

    caps = {c["capture_id"]: c for c in _body(captures.list_captures(_event()))["captures"]}

    zero = caps["cap-zero"]
    assert (zero["detection_count"], zero["detections"]) == (0, [])
    assert (zero["has_masks"], zero["detections_truncated"]) == (False, False)

    trunc = caps["cap-trunc"]
    assert trunc["has_masks"] is True
    assert trunc["detections_truncated"] is True
    assert "mask_truncated" not in trunc["detections"][0]
    assert trunc["detections"][1]["mask_truncated"] is True
    assert trunc["detections"][1]["mask_area"] == 4

    alltrunc = caps["cap-alltrunc"]
    assert alltrunc["has_masks"] is True        # every entry carried a mask on the device
    assert alltrunc["detections_truncated"] is True
    assert all(d["mask_truncated"] is True and "mask_rle" not in d for d in alltrunc["detections"])


def test_light_page_of_50_captures_x_300_boxes_stays_under_the_lambda_cap(s3):
    # Realistic 17-digit float reprs for boxes and confidences, a mask_area
    # and a mask_rle per entry (withheld on the page), long presigned URLs.
    entries = [
        _instance_entry(i, f"class_{i % 7}",
                        [12.345678901234567 + i, 40.123456789012345, 220.98765432109876, 310.11111111111111],
                        0.83456789012345678, rle=RLE_A, area=123456)
        for i in range(300)
    ]
    metadata = instance_metadata(entries)
    objects = {}
    for i in range(50):
        objects.update(_objects(f"cap-{i:03d}", metadata))
    s3(RecordingS3(objects, url_padding=1500))

    response = captures.list_captures(_event(limit="50"))

    assert response["statusCode"] == 200
    assert len(response["body"]) < LAMBDA_RESPONSE_CAP
    payload = json.loads(response["body"])
    assert len(payload["captures"]) == 50
    assert all(len(c["detections"]) == 300 for c in payload["captures"])
    assert all("mask_rle" not in d for c in payload["captures"] for d in c["detections"])


# --------------------------------------------------------------------------- #
# Page: the executor
# --------------------------------------------------------------------------- #
def test_page_parses_on_a_bounded_executor_in_order(s3, monkeypatch):
    objects = {}
    for i in range(12):
        objects.update(_objects(f"cap-{i:02d}", instance_metadata(TWO_INSTANCES)))
    # Key 02 raises on read: it degrades to an empty-detections entry in place.
    raising = f"{SEARCH_PREFIX}/cap-02.jsonl"
    s3(RecordingS3(objects, raising_keys=[raising]))

    seen = {}
    real_executor = captures.ThreadPoolExecutor

    class SpyExecutor(real_executor):
        def __init__(self, max_workers=None, **kwargs):
            seen["max_workers"] = max_workers
            super().__init__(max_workers=max_workers, **kwargs)

    monkeypatch.setattr(captures, "ThreadPoolExecutor", SpyExecutor)

    payload = _body(captures.list_captures(_event(limit="5")))

    assert seen["max_workers"] == 8
    assert payload["total_found"] == 12
    ids = [c["capture_id"] for c in payload["captures"]]
    assert ids == [f"cap-{i:02d}" for i in range(5)]        # order preserved, exactly `limit`
    degraded = payload["captures"][2]
    assert degraded["capture_id"] == "cap-02"
    assert degraded["detections"] == []
    assert (degraded["has_masks"], degraded["detections_truncated"]) == (False, False)
    assert degraded["jsonl_key"] == raising
    assert degraded["source_url"] is not None
    assert all(len(c["detections"]) == 2 for i, c in enumerate(payload["captures"]) if i != 2)


def test_page_with_limit_beyond_the_keys_returns_every_capture(s3):
    objects = {}
    for i in range(3):
        objects.update(_objects(f"cap-{i}", instance_metadata(TWO_INSTANCES)))
    s3(RecordingS3(objects))

    payload = _body(captures.list_captures(_event(limit="20")))

    assert [c["capture_id"] for c in payload["captures"]] == ["cap-0", "cap-1", "cap-2"]


# --------------------------------------------------------------------------- #
# Detail: one capture, mask_rle on request
# --------------------------------------------------------------------------- #
def test_detail_returns_the_rle_without_a_list(s3):
    fake = s3(RecordingS3(_objects("cap-a", instance_metadata(TWO_INSTANCES))))
    key = f"{SEARCH_PREFIX}/cap-a.jsonl"

    payload = _body(captures.list_captures(_event(key=key, include_masks="1", limit="1")))

    assert fake.list_calls == 0                              # no list_objects_v2
    assert fake.owner_assertions == [("get", "123456789012")]
    assert payload["total_found"] == 1
    assert payload["prefix"] == SEARCH_PREFIX
    assert payload["bucket"] == "dda-inference-results-123456789012"
    assert payload["expires_in_seconds"] == captures.PRESIGNED_URL_EXPIRY_SECONDS
    assert len(payload["captures"]) == 1
    cap = payload["captures"][0]
    assert cap["capture_id"] == "cap-a"
    assert cap["jsonl_key"] == key
    assert cap["has_masks"] is True
    assert [d["mask_rle"] for d in cap["detections"]] == [RLE_A, RLE_B]
    assert [d["mask_area"] for d in cap["detections"]] == [12, 4]
    # Only the metadata key was "listed": the artifact URLs are None and
    # nothing was presigned (the viewer keeps the page's URLs).
    assert (cap["source_url"], cap["overlay_url"], cap["mask_url"]) == (None, None, None)
    assert fake.presigned_calls == []


def test_detail_without_include_masks_withholds_the_rle(s3):
    s3(RecordingS3(_objects("cap-a", instance_metadata(TWO_INSTANCES))))

    cap = _body(captures.list_captures(_event(key=f"{SEARCH_PREFIX}/cap-a.jsonl")))["captures"][0]

    assert cap["has_masks"] is True
    assert all("mask_rle" not in d for d in cap["detections"])


def test_detail_of_a_truncated_record_marks_the_entries(s3):
    s3(RecordingS3(_objects("cap-t", instance_metadata(TRUNCATED_MIX, detections_truncated=True))))

    cap = _body(captures.list_captures(
        _event(key=f"{SEARCH_PREFIX}/cap-t.jsonl", include_masks="true")))["captures"][0]

    assert cap["detections_truncated"] is True
    assert cap["has_masks"] is True
    assert cap["detections"][0]["mask_rle"] == RLE_A
    assert "mask_rle" not in cap["detections"][1]
    assert cap["detections"][1]["mask_truncated"] is True


def test_detail_at_the_device_ceiling_stays_under_the_lambda_cap(s3):
    # A Detection_List of exactly DETECTION_LIST_MAX_BYTES (as the device
    # serialises it): 299 small instances plus one whose synthetic RLE pads
    # the block to the byte.
    entries = [_instance_entry(i, f"c{i}", [1, 2, 3, 4], 0.5, rle="1 1", area=1) for i in range(300)]
    block = instance_block(entries)
    base_len = len(json.dumps(block))
    assert base_len < DETECTION_LIST_MAX_BYTES
    needed = DETECTION_LIST_MAX_BYTES - base_len + len("1 1")
    block["detections"]["299"]["mask_rle"] = ("1 " * (needed // 2 + 1))[:needed]
    assert len(json.dumps(block)) == DETECTION_LIST_MAX_BYTES
    metadata = build_metadata(summary={"Inference result": "Detection", "Detection_count": 300},
                              block=block)
    s3(RecordingS3(_objects("cap-big", metadata), url_padding=1500))

    response = captures.list_captures(_event(key=f"{SEARCH_PREFIX}/cap-big.jsonl", include_masks="1"))

    assert response["statusCode"] == 200
    assert len(response["body"]) < LAMBDA_RESPONSE_CAP
    cap = json.loads(response["body"])["captures"][0]
    assert len(cap["detections"]) == 300
    assert len(cap["detections"][299]["mask_rle"]) == needed


def test_detail_of_a_missing_object_degrades_to_an_empty_entry(s3):
    fake = s3(RecordingS3({}))
    key = f"{SEARCH_PREFIX}/gone.jsonl"

    payload = _body(captures.list_captures(_event(key=key, include_masks="1")))

    assert payload["total_found"] == 1
    cap = payload["captures"][0]
    assert cap["capture_id"] == "gone"
    assert cap["inference_result_type"] is None
    assert cap["detections"] == []
    assert (cap["has_masks"], cap["detections_truncated"]) == (False, False)
    assert cap["jsonl_key"] == key
    assert fake.list_calls == 0


# --------------------------------------------------------------------------- #
# Detail: key validation and its place in the request
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_key", [
    "other/thing-1/cap-a.jsonl",                   # outside the scoped prefix
    f"{SEARCH_PREFIX}/cap-a.jpg",                  # not a .jsonl object
    "",                                            # empty
    f"{SEARCH_PREFIX}/" + "k" * (1025 - len(SEARCH_PREFIX) - 1 - len(".jsonl")) + ".jsonl",  # 1025 bytes
], ids=["outside-prefix", "not-jsonl", "empty", "1025-bytes"])
def test_invalid_detail_key_is_a_400_before_any_credential_work(bad_key, monkeypatch):
    reached = []
    monkeypatch.setattr(captures, "get_usecase",
                        lambda usecase_id: reached.append("get_usecase") or {})
    monkeypatch.setattr(captures.boto3, "client",
                        lambda *a, **k: reached.append("boto3.client") or RecordingS3({}))

    response = captures.list_captures(_event(key=bad_key, include_masks="1"))

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": captures.DETAIL_KEY_ERROR}
    assert reached == []


def test_1024_byte_key_under_the_prefix_is_accepted(s3):
    stem = "k" * (1024 - len(SEARCH_PREFIX) - 1 - len(".jsonl"))
    key = f"{SEARCH_PREFIX}/{stem}.jsonl"
    assert len(key.encode("utf-8")) == 1024
    objects = {key: build_jsonl(instance_metadata(TWO_INSTANCES))}
    s3(RecordingS3(objects))

    payload = _body(captures.list_captures(_event(key=key, include_masks="1")))

    assert payload["captures"][0]["detections"][0]["mask_rle"] == RLE_A


def test_membership_refusal_comes_before_the_key_validation(monkeypatch):
    """A refused caller gets the 403 (one audit row), not the 400 of its bad key."""
    audits = []
    monkeypatch.setattr(captures, "check_user_access", lambda *a, **k: False)
    monkeypatch.setattr(captures, "log_audit_event", lambda **kw: audits.append(kw))
    monkeypatch.setattr(captures, "get_usecase",
                        lambda usecase_id: pytest.fail("get_usecase reached after a refusal"))

    response = captures.list_captures(_event(key="elsewhere/cap.jpg", include_masks="1"))

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
    assert len(audits) == 1 and audits[0]["action"] == "unauthorized_access"


def test_validate_detail_key_rules():
    assert captures._validate_detail_key(f"{SEARCH_PREFIX}/c.jsonl", SEARCH_PREFIX) is True
    assert captures._validate_detail_key(f"{SEARCH_PREFIX}c.jsonl", SEARCH_PREFIX) is True  # prefix match is textual, as the LIST's
    assert captures._validate_detail_key("dev/other/c.jsonl", SEARCH_PREFIX) is False
    assert captures._validate_detail_key(f"{SEARCH_PREFIX}/c.jsonl ", SEARCH_PREFIX) is False
    assert captures._validate_detail_key(None, SEARCH_PREFIX) is False
    assert captures._validate_detail_key(["a"], SEARCH_PREFIX) is False
    assert captures._validate_detail_key("", SEARCH_PREFIX) is False
    # Byte length, not character count: a 2-byte character counts twice.
    wide = f"{SEARCH_PREFIX}/" + "\u00e9" * ((1024 - len(SEARCH_PREFIX) - 1 - len(".jsonl")) // 2) + ".jsonl"
    assert len(wide.encode("utf-8")) == 1024
    assert captures._validate_detail_key(wide, SEARCH_PREFIX) is True
    assert captures._validate_detail_key(wide.replace(".jsonl", "\u00e9.jsonl"), SEARCH_PREFIX) is False


# --------------------------------------------------------------------------- #
# Helpers: _capture_mask_flags, _decode_detections_block, _extract_inference_data
# --------------------------------------------------------------------------- #
def test_capture_mask_flags():
    assert captures._capture_mask_flags(None) == (False, False)
    assert captures._capture_mask_flags({"anomalies": {"0": {"label": "scratch"}}}) == (False, False)
    assert captures._capture_mask_flags({"detections": {}}) == (False, False)
    assert captures._capture_mask_flags(
        {"detections": {"0": _detection_entry("1", "dog", [1, 2, 3, 4], 0.9)}}) == (False, False)
    assert captures._capture_mask_flags(instance_block(TWO_INSTANCES)) == (False, True)
    assert captures._capture_mask_flags(instance_block(TRUNCATED_MIX, detections_truncated=True)) == (True, True)
    assert captures._capture_mask_flags(instance_block(ALL_TRUNCATED, detections_truncated=True)) == (True, True)
    # The marker alone (no entry with a mask) is honoured; a non-dict entry is skipped.
    assert captures._capture_mask_flags({"detections": {"0": "junk"}, "detections_truncated": True}) == (True, False)


def test_decode_detections_block(capsys):
    block = instance_block(TWO_INSTANCES)
    assert captures._decode_detections_block(instance_metadata(TWO_INSTANCES)) == block
    assert captures._decode_detections_block({}) is None
    assert captures._decode_detections_block(build_metadata(summary={"Inference result": "Normal"})) is None

    corrupt = {"deviceFleetAuxiliaryOutputs": [
        {"observedContentType": "json_with_base64_encoding", "data": "!!!not-base64!!!"}]}
    assert captures._decode_detections_block(corrupt) is None
    assert "Failed to decode detections block" in capsys.readouterr().out


def test_extract_inference_data_decodes_once_when_given_the_block():
    metadata = instance_metadata(TWO_INSTANCES)
    block = captures._decode_detections_block(metadata)

    # Caller-supplied block: identical result to the self-decoding call.
    assert captures._extract_inference_data(metadata, include_masks=True, block=block) == \
        captures._extract_inference_data(metadata, include_masks=True)
    # An explicit None block (corrupt / absent) yields no detections.
    result_type, count, detections = captures._extract_inference_data(metadata, block=None)
    assert (result_type, count, detections) == ("Detection", 2, [])
    # Default call: the light form (no mask_rle), mask_area through.
    _, _, light = captures._extract_inference_data(metadata)
    assert light[0] == {"class_index": "0", "class_label": "scratch", "bounding_box": [1, 1, 3, 3],
                        "confidence": 0.91, "mask_area": 12}
    _, _, full = captures._extract_inference_data(metadata, include_masks=True)
    assert full[0]["mask_rle"] == RLE_A


def test_normalize_detections_passes_instance_keys_only_when_present():
    det_map = {
        "0": _detection_entry("1", "dog", [1, 2, 3, 4], 0.9),
        "1": _instance_entry(2, "cat", [1, 2, 3, 4], 0.8, rle=RLE_B, area=4),
        "2": _instance_entry(3, "cow", [1, 2, 3, 4], 0.7, area=9, truncated=True),
    }
    light = captures._normalize_detections(det_map)
    assert set(light[0]) == {"class_index", "class_label", "bounding_box", "confidence"}
    assert light[1]["mask_area"] == 4 and "mask_rle" not in light[1] and "mask_truncated" not in light[1]
    assert light[2]["mask_truncated"] is True and light[2]["mask_area"] == 9

    full = captures._normalize_detections(det_map, include_masks=True)
    assert "mask_rle" not in full[0]
    assert full[1]["mask_rle"] == RLE_B
    assert "mask_rle" not in full[2]


def test_parse_capture_detail_keywords_default_to_the_page_form():
    metadata = instance_metadata(TWO_INSTANCES)
    key = f"{SEARCH_PREFIX}/cap-a.jsonl"
    fake = RecordingS3({key: build_jsonl(metadata), f"{SEARCH_PREFIX}/cap-a.jpg": b"src"})

    page = captures._parse_capture(fake, "bucket", key, set(fake._objects))
    detail = captures._parse_capture(fake, "bucket", key, {key}, None, include_masks=True)

    assert page["jsonl_key"] == detail["jsonl_key"] == key
    assert page["has_masks"] is True and detail["has_masks"] is True
    assert "mask_rle" not in page["detections"][0]
    assert detail["detections"][0]["mask_rle"] == RLE_A
    assert page["source_url"] is not None and detail["source_url"] is None


def test_detail_request_builds_no_executor(s3, monkeypatch):
    """The detail path is one read: no pool, no slicing."""
    def no_pool(*a, **k):
        raise AssertionError("pool built on a detail request")

    s3(RecordingS3(_objects("cap-a", instance_metadata(TWO_INSTANCES))))
    monkeypatch.setattr(captures, "ThreadPoolExecutor", no_pool)

    payload = _body(captures.list_captures(_event(key=f"{SEARCH_PREFIX}/cap-a.jsonl")))

    assert payload["captures"][0]["capture_id"] == "cap-a"
