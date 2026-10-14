"""
Property-based test for Portal_Pin_API use-case resolution
(cloud-static-camera-provisioning task 2.7).

# Feature: cloud-static-camera-provisioning, Property 6: Use-case resolution ignores caller scoping

*For any* devices-table use-case value, caller-supplied use-case
parameter, and permission grant set, the authorization decision for a
Pin_Request submission or status query depends only on the devices-table
value — a caller-supplied parameter differing from the Portal record
never changes the outcome.

**Validates: Requirements 8.5**

Generators: the device is registered to Use_Case A; the caller supplies
a ``usecase_id`` query parameter drawn from {A, another real Use_Case B,
garbage, absent}; the user's grants are drawn independently for A and B
(role assignment or none, with a DataLabeler JWT fallback holding
neither device permission). The oracle computes the expected outcome
from the grant on A alone and asserts the response — and, on denials,
the audited use case — never varies with the caller's parameter or the
grant on B. Example counts come from the conftest hypothesis profiles
(portal-fast locally; the spec-minimum 100 with HYPOTHESIS_PROFILE=ci).
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
# assignment (DataLabeler JWT fallback: neither).
ROLE_GRANTS = {
    None: (False, False),
    "Viewer": (True, False),
    "Operator": (True, True),
}

_roles = st.sampled_from(sorted(ROLE_GRANTS, key=str))
_params = st.sampled_from(["A", "B", "garbage", None])
_ops = st.sampled_from(["pin", "status"])


@pytest.fixture(scope="module")
def usecases(pin_env):
    return {"A": create_usecase(pin_env, "Use Case A"),
            "B": create_usecase(pin_env, "Use Case B")}


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(role_a=_roles, role_b=_roles, param=_params, op=_ops)
def test_usecase_resolution_ignores_caller_scoping(pin_env, usecases,
                                                   role_a, role_b, param,
                                                   op):
    """Property 6: the outcome depends only on the devices-table
    Use_Case's grant — never on the caller parameter or the grant on B."""
    usecase_a, usecase_b = usecases["A"], usecases["B"]
    device_id = register_device(pin_env, usecase_a, prefix="thing-prop6")
    user = make_user("DataLabeler")
    if role_a is not None:
        pin_env.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": usecase_a,
            "role": role_a})
    if role_b is not None:
        pin_env.user_roles.put_item(Item={
            "user_id": user["user_id"], "usecase_id": usecase_b,
            "role": role_b})

    query = None
    if param is not None:
        query = {"usecase_id": {
            "A": usecase_a, "B": usecase_b, "garbage": "uc-garbage-000",
        }[param]}

    fake_s3 = RecordingS3(pin_env.s3)
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow

    if op == "pin":
        status, body = submit_pin(pin_env, device_id, user, image_bytes(),
                                  query=query)
    else:
        status, body = invoke(pin_env, "GET", device_id, user,
                              sub_path="/static-image", query=query)

    # The oracle uses ONLY the devices-table value's grant (Req 8.5).
    can_view, can_manage = ROLE_GRANTS[role_a]
    allowed = can_manage if op == "pin" else can_view

    if allowed:
        assert status in (200, 201), \
            (f"grant on the devices-table Use_Case must authorize "
             f"regardless of caller parameter {param!r}: {body}")
        if op == "pin":
            assert pin_request_items(pin_env, device_id)[0]["usecase_id"] \
                == usecase_a
    else:
        assert status == 403, \
            (f"caller parameter {param!r} / grant on B must never "
             f"authorize: {body}")
        # The denial was evaluated against the devices-table Use_Case.
        events = audit_events(pin_env, device_id,
                              action="unauthorized_access")
        assert len(events) == 1
        assert events[0]["details"]["usecase_id"] == usecase_a
        # Zero side effects on denial.
        assert pin_request_items(pin_env, device_id) == []
        assert fake_s3.copies == []
        assert fake_shadow.updates == []
