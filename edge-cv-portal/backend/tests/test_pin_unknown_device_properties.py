"""
Property-based test for unknown device rejection
(cloud-static-camera-provisioning task 2.5).

# Feature: cloud-static-camera-provisioning, Property 4: Unknown device rejection

*For any* device identifier with no Portal-side device record, pin,
replace, and removal submissions are rejected with a not-registered
error and record zero Pin_Request items, zero Image_Transport writes,
and zero Sync_Channel writes.

**Validates: Requirements 1.8, 8.7**

Generators: URL-safe device identifiers guaranteed absent from both the
devices table and the registry (uuid-suffixed), crossed with the
operation (pin submission, replace — a second pin submission shape —
removal, and the upload-url precursor), submitted by a fully privileged
Operator so the rejection is attributable to the missing record alone.
Example counts come from the conftest hypothesis profiles (portal-fast
locally; the spec-minimum 100 with HYPOTHESIS_PROFILE=ci).
"""
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, make_user, pin_env,
    pin_request_items, submit_pin, invoke,
)


@pytest.fixture(scope="module")
def operator():
    return make_user("Operator")


_id_fragments = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=24,
)

_ops = st.sampled_from(["pin", "replace", "remove", "upload-url"])


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(fragment=_id_fragments, op=_ops)
def test_unknown_device_rejection(pin_env, operator, fragment, op):
    """Property 4: not-registered error; zero Pin_Request items, zero
    Image_Transport writes, zero Sync_Channel writes."""
    # uuid suffix guarantees the id has no devices-table or registry
    # record, whatever the drawn fragment.
    device_id = f"{fragment}-{uuid.uuid4().hex[:10]}"

    fake_s3 = RecordingS3(pin_env.s3)
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow

    if op in ("pin", "replace"):
        status, body = submit_pin(pin_env, device_id, operator,
                                  b"ignored-payload")
    elif op == "remove":
        status, body = invoke(pin_env, "DELETE", device_id, operator,
                              sub_path="/static-image/pin")
    else:
        status, body = invoke(pin_env, "POST", device_id, operator,
                              sub_path="/static-image/upload-url")

    assert status == 404, body
    assert "not registered" in body["error"]

    # Zero side effects: no Pin_Request items, no Image_Transport
    # canonical writes, no Sync_Channel writes.
    assert pin_request_items(pin_env, device_id) == []
    assert fake_s3.copies == []
    assert fake_shadow.updates == []
