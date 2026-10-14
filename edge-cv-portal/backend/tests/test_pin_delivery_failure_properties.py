"""
Property-based test for delivery-initiation failure
(cloud-static-camera-provisioning task 2.4).

# Feature: cloud-static-camera-provisioning, Property 3: Delivery-initiation failure

*For any* accepted submission where the Image_Transport store step or
the Sync_Channel write step fails, the Pin_Request ends in the
``failed`` Sync_Status, the operator receives an error identifying the
failing step, and a store-step failure records zero Sync_Channel writes.

**Validates: Requirements 1.9, 2.5**

Generators: the failing step (the canonical CopyObject store step, pin
submissions only, or the shadow write step) crossed with the operation
type (pin or removal — removals have no store step) and varying image
payloads. Example counts come from the conftest hypothesis profiles
(portal-fast locally; the spec-minimum 100 with HYPOTHESIS_PROFILE=ci).
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, create_usecase, image_bytes, invoke,
    make_user, pin_env, pin_request_items, register_device, submit_pin,
)


@pytest.fixture(scope="module")
def operator(pin_env):
    usecase_id = create_usecase(pin_env)
    return make_user("Operator"), usecase_id


# (op, failing_step): removals carry no Image_Transport object, so the
# store step can only fail for pin submissions.
_cases = st.fixed_dictionaries({
    "scenario": st.sampled_from([("pin", "store"), ("pin", "shadow"),
                                 ("remove", "shadow")]),
    "image_format": st.sampled_from(["JPEG", "PNG", "BMP"]),
    "size": st.tuples(st.integers(1, 24), st.integers(1, 24)),
})


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(case=_cases)
def test_delivery_initiation_failure(pin_env, operator, case):
    """Property 3: the request ends failed, the error identifies the
    failing step, and a store-step failure records zero shadow writes."""
    user, usecase_id = operator
    op, failing_step = case["scenario"]
    device_id = register_device(pin_env, usecase_id, prefix="thing-prop3")

    fake_s3 = RecordingS3(pin_env.s3, fail_copy=(failing_step == "store"))
    fake_shadow = FakeIotDataClient(fail=(failing_step == "shadow"))
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow

    if op == "pin":
        payload = image_bytes(case["image_format"], case["size"])
        status, body = submit_pin(pin_env, device_id, user, payload)
    else:
        status, body = invoke(pin_env, "DELETE", device_id, user,
                              sub_path="/static-image/pin")

    # The operator receives an error identifying the failing step.
    assert status == 502, body
    error = body["error"].lower()
    if failing_step == "store":
        assert "store" in error
        # A store-step failure records zero Sync_Channel writes (Req 2.5).
        assert fake_shadow.updates == []
    else:
        assert "sync channel" in error and "delivery" in error

    # The Pin_Request ends in the failed Sync_Status (Req 1.9).
    items = pin_request_items(pin_env, device_id)
    assert len(items) == 1
    item = items[0]
    assert item["op"] == op
    assert item["status"] == "failed"
    assert item.get("failure_reason")
