"""
Property-based test for accepted-submission effects
(cloud-static-camera-provisioning task 2.3).

# Feature: cloud-static-camera-provisioning, Property 2: Accepted-submission effects

*For any* accepted pin or removal submission, the observable effects are
exactly: (a) a Pin_Request item in the ``pending`` Sync_Status scoped to
the target device; (b) for pin operations, the image content stored in
the Image_Transport strictly before any Sync_Channel write; (c) a
``desired.staticImagePin`` document carrying the content reference, the
sha256 Content_Checksum, and the byte-size/format metadata — never the
image bytes — whose serialized size is at most 1024 bytes; (d) a
response carrying the Pin_Request identifier, the device identifier, and
``pending``; and (e) one audit event recording the acting user, the
device, the operation type, the Pin_Request identifier, and the
timestamp.

**Validates: Requirements 1.2, 1.5, 2.1, 2.2, 2.3, 2.4, 7.2, 8.4**

Generators: pin submissions over Pillow-generated images across all
Supported_Image_Formats with unicode file names up to 200 characters
(exercising the 128-character shadow truncation), and removal
submissions. Example counts come from the conftest hypothesis profiles
(portal-fast locally; the spec-minimum 100 with HYPOTHESIS_PROFILE=ci).
"""
import hashlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, audit_events, create_usecase,
    image_bytes, invoke, make_user, pin_env, pin_request_items,
    register_device, submit_pin,
)

AUDIT_ACTIONS = {"pin": "pin_static_image", "remove": "remove_static_image"}


@pytest.fixture(scope="module")
def operator(pin_env):
    usecase_id = create_usecase(pin_env)
    return make_user("Operator"), usecase_id


_file_names = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N", "P", "Zs")),
    min_size=1,
    max_size=200,
).filter(lambda name: name.strip())

_pin_cases = st.fixed_dictionaries({
    "op": st.just("pin"),
    "image_format": st.sampled_from(["JPEG", "PNG", "BMP"]),
    "size": st.tuples(st.integers(1, 32), st.integers(1, 32)),
    "file_name": _file_names,
})
_remove_cases = st.fixed_dictionaries({"op": st.just("remove")})


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(case=st.one_of(_pin_cases, _remove_cases))
def test_accepted_submission_effects(pin_env, operator, case):
    """Property 2: pending item, store-before-shadow ordering, reference-
    only desired document ≤ 1024 B, id/device/pending response, and one
    audit event."""
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id, prefix="thing-prop2")

    log = []
    fake_s3 = RecordingS3(pin_env.s3, log=log)
    fake_shadow = FakeIotDataClient(log=log)
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow

    if case["op"] == "pin":
        payload = image_bytes(case["image_format"], case["size"])
        status, body = submit_pin(pin_env, device_id, user, payload,
                                  file_name=case["file_name"])
        assert status == 201, body
    else:
        payload = None
        status, body = invoke(pin_env, "DELETE", device_id, user,
                              sub_path="/static-image/pin")
        assert status == 200, body

    # (d) response: Pin_Request identifier, device identifier, pending.
    assert body["deviceId"] == device_id
    assert body["status"] == "pending"
    pin_request_id = body["pinRequestId"]
    assert pin_request_id

    # (a) exactly one Pin_Request item, pending, scoped to the device.
    items = pin_request_items(pin_env, device_id)
    assert len(items) == 1
    item = items[0]
    assert item["device_id"] == device_id
    assert item["usecase_id"] == usecase_id
    assert item["status"] == "pending"
    assert item["pin_request_id"] == pin_request_id
    assert item["op"] == case["op"]

    # (c) the desired document, replaced wholesale in the single slot.
    assert len(fake_shadow.updates) == 1
    update = fake_shadow.updates[0]
    assert update["thing_name"] == device_id
    assert update["shadow_name"] == "dda-camera-registry"
    desired = update["payload"]["state"]["desired"]
    assert list(desired.keys()) == ["staticImagePin"]
    section = desired["staticImagePin"]
    assert section["requestId"] == pin_request_id
    assert section["op"] == case["op"]

    serialized = json.dumps(section, separators=(",", ":"))
    assert len(serialized.encode("utf-8")) <= 1024  # Reqs 2.3, 2.4

    if case["op"] == "pin":
        # Content reference + Content_Checksum + size/format metadata.
        assert section["bucket"] == pin_env.bucket
        assert section["key"] == \
            f"static-image-pins/{device_id}/{pin_request_id}"
        assert section["sha256"] == hashlib.sha256(payload).hexdigest()
        assert section["sizeBytes"] == len(payload)
        assert section["format"] == case["image_format"]
        # fileName truncated to 128 chars for the shadow; the original
        # full name is preserved on the Pin_Request item.
        assert section["fileName"] == case["file_name"][:128]
        assert item["file_name"] == case["file_name"]
        # Never the image bytes (Req 2.2): the section carries only the
        # known reference/metadata fields, and the payload is not
        # embedded in any of them.
        assert set(section) <= {"requestId", "op", "requestedAtEpochMs",
                                "bucket", "key", "sha256", "sizeBytes",
                                "format", "fileName"}
        for value in section.values():
            assert not isinstance(value, (bytes, bytearray))

        # (b) content stored in the Image_Transport strictly before any
        # Sync_Channel write (Req 2.1), and the canonical object holds
        # the exact submitted bytes.
        assert len(fake_s3.copies) == 1
        copy_index = log.index(("canonical_copy", section["key"]))
        shadow_index = log.index(("shadow_write", device_id))
        assert copy_index < shadow_index
        stored = pin_env.s3.get_object(
            Bucket=pin_env.bucket, Key=section["key"])["Body"].read()
        assert stored == payload
    else:
        # Removal documents carry no Image_Transport reference and no
        # transport object is written (Req 7.2).
        assert fake_s3.copies == []
        for field in ("bucket", "key", "sha256", "sizeBytes", "format",
                      "fileName"):
            assert section.get(field) is None

    # (e) exactly one audit event: acting user, device, operation type,
    # Pin_Request identifier, timestamp (Req 8.4).
    events = audit_events(pin_env, device_id,
                          action=AUDIT_ACTIONS[case["op"]])
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == user["user_id"]
    assert event["result"] == "success"
    assert int(event["timestamp"]) > 0
    details = event["details"]
    assert details["device_id"] == device_id
    assert details["pin_request_id"] == pin_request_id
    assert details["operation"] == case["op"]
    # And no other audit events for this fresh device.
    assert len(audit_events(pin_env, device_id)) == 1
