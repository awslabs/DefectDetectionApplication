"""
Property-based test for Portal_Pin_API authorization and audit
(cloud-static-camera-provisioning task 2.6).

# Feature: cloud-static-camera-provisioning, Property 5: Authorization decisions and audit

*For any* combination of user permission grants and requested operation,
mutation requests succeed exactly when the user holds the
device-mutation permission for the device's Use_Case and status queries
succeed exactly when the user holds the device-view permission; every
denial returns an authorization error naming the required permission,
produces zero side effects and zero status data, and logs an
``unauthorized_access`` audit event recording the acting user, the
device, the attempted operation type, and the timestamp.

**Validates: Requirements 8.1, 8.2, 8.3, 8.6**

Grant model: each example creates a fresh user whose JWT fallback role
is DataLabeler (holding neither device permission) and optionally writes
a per-use-case role assignment into the user-roles table — so the drawn
assignment IS the user's grant set for the device's Use_Case. MANAGE_
DEVICES is held by Operator/UseCaseAdmin, VIEW_DEVICES additionally by
Viewer/DataScientist, and DataLabeler (or no assignment) holds neither.
Example counts come from the conftest hypothesis profiles (portal-fast
locally; the spec-minimum 100 with HYPOTHESIS_PROFILE=ci).
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, audit_events, create_usecase,
    image_bytes, invoke, make_user, pin_env, pin_request_items,
    register_device, submit_pin,
)

# Role -> (holds VIEW_DEVICES, holds MANAGE_DEVICES); None = no
# assignment (the DataLabeler JWT fallback applies).
ROLE_GRANTS = {
    None: (False, False),
    "DataLabeler": (False, False),
    "Viewer": (True, False),
    "DataScientist": (True, False),
    "Operator": (True, True),
    "UseCaseAdmin": (True, True),
}

MUTATIONS = ("pin", "replace", "remove", "upload-url")
OPERATIONS = MUTATIONS + ("status",)


@pytest.fixture(scope="module")
def usecase_id(pin_env):
    return create_usecase(pin_env)


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(role=st.sampled_from(sorted(ROLE_GRANTS, key=str)),
       op=st.sampled_from(OPERATIONS))
def test_authorization_decisions_and_audit(pin_env, usecase_id, role, op):
    """Property 5: mutations succeed iff MANAGE_DEVICES, status iff
    VIEW_DEVICES; denials name the required permission, have zero side
    effects and zero status data, and log unauthorized_access."""
    device_id = register_device(pin_env, usecase_id, prefix="thing-prop5")
    user = make_user("DataLabeler")  # JWT fallback: neither permission
    if role is not None:
        pin_env.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": usecase_id,
            "role": role,
        })

    fake_s3 = RecordingS3(pin_env.s3)
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow

    if op in ("pin", "replace"):
        status, body = submit_pin(pin_env, device_id, user, image_bytes())
    elif op == "remove":
        status, body = invoke(pin_env, "DELETE", device_id, user,
                              sub_path="/static-image/pin")
    elif op == "upload-url":
        status, body = invoke(pin_env, "POST", device_id, user,
                              sub_path="/static-image/upload-url")
    else:
        status, body = invoke(pin_env, "GET", device_id, user,
                              sub_path="/static-image")

    can_view, can_manage = ROLE_GRANTS[role]
    allowed = can_manage if op in MUTATIONS else can_view
    required = "manage_devices" if op in MUTATIONS else "view_devices"

    if allowed:
        assert status in (200, 201), body
        assert audit_events(pin_env, device_id,
                            action="unauthorized_access") == []
        return

    # Denial: authorization error naming the required permission.
    assert status == 403, body
    assert body["required_permission"] == required
    # Zero status data beyond the error shape.
    for field in ("latest", "history", "deviceReported", "noPinRequest",
                  "pinRequestId"):
        assert field not in body

    # Zero side effects.
    assert pin_request_items(pin_env, device_id) == []
    assert fake_s3.copies == []
    assert fake_shadow.updates == []

    # One unauthorized_access audit event: acting user, device, attempted
    # operation type (method + path), timestamp.
    events = audit_events(pin_env, device_id, action="unauthorized_access")
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == user["user_id"]
    assert event["resource_id"] == device_id
    assert int(event["timestamp"]) > 0
    details = event["details"]
    assert details["required_permission"] == required
    expected_method = {"pin": "POST", "replace": "POST", "remove": "DELETE",
                       "upload-url": "POST", "status": "GET"}[op]
    assert details["method"] == expected_method
    assert details["path"].startswith(f"/devices/{device_id}/cameras")
    # And no acceptance audit events for the denied operation.
    assert len(audit_events(pin_env, device_id)) == 1
