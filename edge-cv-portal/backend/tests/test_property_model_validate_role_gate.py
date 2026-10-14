"""
Role-gate properties of the use-case bucket configuration
(portal-usecase-bucket-config, design §7, §14; Requirements 3.1, 3.4, 4.6,
15.6; tasks.md 6.5).

Everything about authorization is real: the real `check_user_access`,
`RBACManager`, role/permission matrix and `log_audit_event` run against the
conftest moto stack (`test-user-roles` is the Portal_Identity registry,
`test-audit-log` the audit table). Only the S3 side of the handlers is
stubbed (the artifact validator, the use-case lookup), so a decision never
reaches STS or S3.

Both `PORTAL_REGISTRY_ENFORCED` states are exercised inside every example
(unset, then 'true'), toggled and restored around each call the way
test_property_portal_registry_roles.py toggles it per module.

8. **validate equals import** — for any generated registry state (global
   row, use-case row, both, neither, disabled, unknown role) and any
   Claimed_Role, `POST /models/validate` and `POST /models/import` reach
   the same accept/deny decision in both flag states, because both gates
   are `check_user_access(user_id, usecase_id, 'DataScientist')` with no
   `user_info`.

Pinned by example, with the three principals Requirement 15.6 defines —
a registry DataScientist (global Viewer provisioning row + a DataScientist
row on the use case), a claim-only DataScientist (JWT custom:role =
DataScientist, a global `{usecase_id: 'global', role: 'Viewer',
status: 'enabled'}` row, **no** use-case row) and a Viewer:

- validate = import for every principal and state; the registry
  DataScientist is admitted in both states, the claim-only DataScientist
  and the Viewer denied in both;
- `create_dda_job`'s gate (`has_permission(..., CREATE_LABELING_JOBS,
  user)`) admits the registry DataScientist in both states, admits the
  claim-only DataScientist with the flag off and denies it with the flag
  on, denies the Viewer in both; a denial is 403 with an
  `unauthorized_access` audit row and no job, STS or S3 write.
"""
import json
import os
import sys
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

VALID_ROLE_CLAIMS = ("PortalAdmin", "UseCaseAdmin", "DataScientist", "Operator", "Viewer",
                     "DataLabeler")
ROW_ROLE_VALUES = VALID_ROLE_CLAIMS + ("", "viewer", "Guest")
STATUS_VALUES = (None, "enabled", "disabled", "")
BUCKET = "gate-bucket"
VALIDATION_RESULT = {
    "valid": True,
    "metadata": {"image_width": 224, "image_height": 224, "input_shape": [1, 3, 224, 224],
                 "model_type": "classification", "pt_file": "m.pt", "model_file": "m.pt",
                 "framework": "PYTORCH", "framework_version": "1.8"},
    "files_found": [], "warnings": [],
}


class Recorder:
    def __init__(self, result=None):
        self.calls: List[Any] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "RegistryUnavailable") and hasattr(
        shared_utils, "assert_bucket_allowed"), "this suite needs the real shared layer"
    return shared_utils


@pytest.fixture(scope="module")
def mi(aws_stack, shared):
    sys.modules.pop("model_import", None)
    import model_import
    return model_import


@pytest.fixture(scope="module")
def dl(aws_stack, shared):
    sys.modules.pop("dda_labeling", None)
    import dda_labeling
    return dda_labeling


@pytest.fixture(scope="module")
def h(aws_stack, shared, mi, dl):
    """Module-scoped harness: a use case record in the moto table, S3-side
    stubs on both handlers, recorders on every STS / S3 / write seam."""
    mp = pytest.MonkeyPatch()
    usecase_id = f"uc-gate-{uuid.uuid4().hex[:8]}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id, "name": "Gate", "account_id": "acct-gate",
        "cross_account_role_arn": "arn:aws:iam::acct-gate:root", "s3_bucket": BUCKET,
    })
    state = SimpleNamespace(
        usecase_id=usecase_id, mi=mi, dl=dl, shared=shared, stack=aws_stack,
        assume=Recorder(result={"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}),
        validator=Recorder(),
        items=[],
        worker=Recorder(),
    )
    mp.setattr(shared, "assume_usecase_role", state.assume)
    mp.setattr(shared, "create_boto3_client", Recorder(result=SimpleNamespace()))

    def validator(uri, usecase, *, purpose):
        state.validator(uri, usecase, purpose=purpose)
        return json.loads(json.dumps(VALIDATION_RESULT))

    mp.setattr(mi, "validate_model_artifact", validator)
    mp.setattr(mi, "get_usecase_details", lambda _id: {
        "usecase_id": usecase_id, "s3_bucket": BUCKET, "account_id": "acct-gate",
        "cross_account_role_arn": "arn:aws:iam::acct-gate:root"})
    table = SimpleNamespace(put_item=lambda Item: state.items.append(Item))
    mp.setattr(mi, "dynamodb", SimpleNamespace(Table=lambda _n: table))
    mp.setattr(mi, "TRAINING_JOBS_TABLE", "test-training-jobs")
    mp.setattr(dl, "_invoke_labeling_worker", state.worker)

    def reset():
        for recorder in (state.assume, state.validator, state.worker):
            recorder.calls.clear()
        state.items.clear()

    state.reset = reset
    yield state
    mp.undo()


# ---------------------------------------------------------------- helpers

@contextmanager
def enforcement(shared, enabled):
    """PORTAL_REGISTRY_ENFORCED 'true' or unset for the duration, restored
    afterwards (registry_enforcement_enabled reads the environment on every
    call)."""
    variable = shared.PORTAL_REGISTRY_ENFORCED_ENV
    previous = os.environ.get(variable)
    if enabled:
        os.environ[variable] = "true"
    else:
        os.environ.pop(variable, None)
    try:
        assert shared.registry_enforcement_enabled() is enabled
        yield
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


def new_sub():
    return str(uuid.uuid4())


def put_row(registry, sub, usecase_id, role, status="enabled"):
    item = {"user_id": sub, "usecase_id": usecase_id, "role": role, "username": "gate-user",
            "email": "gate-user@example.com", "assigned_by": "test", "assigned_at": 1}
    if status is not None:
        item["status"] = status
    registry.put_item(Item=item)


def claims(sub, claim):
    payload = {"sub": sub, "cognito:username": "gate-user", "email": "gate-user@example.com"}
    if claim is not None:
        payload["custom:role"] = claim
    return payload


def model_event(sub, claim, body):
    return {"httpMethod": "POST", "path": "/models", "resource": "/models",
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": claims(sub, claim)},
                               "identity": {"sourceIp": "203.0.113.5", "userAgent": "gate/1.0"}}}


def user_of(sub, claim):
    return {"user_id": sub, "email": "gate-user@example.com", "username": "gate-user",
            "role": "Viewer" if claim is None else claim}


def validate_status(h, sub, claim):
    response = h.mi.validate_model(model_event(sub, claim, {
        "usecase_id": h.usecase_id, "model_s3_uri": f"s3://{BUCKET}/models/m.tar.gz"}), None)
    return response["statusCode"]


def import_status(h, sub, claim):
    response = h.mi.import_model(model_event(sub, claim, {
        "usecase_id": h.usecase_id, "model_name": "m", "model_version": "1",
        "model_s3_uri": f"s3://{BUCKET}/models/m.tar.gz"}), None)
    return response["statusCode"]


def create_job(h, sub, claim):
    """create_dda_job with a body that passes the gate but fails later
    validation: admitted <=> 400 with validation_errors, denied <=> 403."""
    response = h.dl.create_dda_job(
        {"usecase_id": h.usecase_id, "job_name": f"gate-{uuid.uuid4().hex[:6]}"},
        user_of(sub, claim))
    return response["statusCode"], json.loads(response["body"])


def unauthorized_rows(h, sub):
    return [item for item in h.stack.tables.audit_log.scan().get("Items", [])
            if item.get("user_id") == sub and item.get("action") == "unauthorized_access"]


def jobs_of_usecase(h):
    import boto3.dynamodb.conditions as conditions
    return h.stack.tables.labeling_jobs.query(
        IndexName="usecase-jobs-index",
        KeyConditionExpression=conditions.Key("usecase_id").eq(h.usecase_id)).get("Items", [])


# ----------------------------------------------------- the three principals

def registry_datascientist(h):
    sub = new_sub()
    put_row(h.stack.tables.user_roles, sub, "global", "Viewer")
    put_row(h.stack.tables.user_roles, sub, h.usecase_id, "DataScientist")
    return sub, "DataScientist"


def claim_only_datascientist(h):
    """Requirement 15.6: JWT custom:role = DataScientist, one global
    {usecase_id: 'global', role: 'Viewer', status: 'enabled'} provisioning
    row, NO row on the use case."""
    sub = new_sub()
    put_row(h.stack.tables.user_roles, sub, "global", "Viewer", status="enabled")
    return sub, "DataScientist"


def viewer(h):
    sub = new_sub()
    put_row(h.stack.tables.user_roles, sub, "global", "Viewer")
    put_row(h.stack.tables.user_roles, sub, h.usecase_id, "Viewer")
    return sub, "Viewer"


PRINCIPALS = {"registry-datascientist": registry_datascientist,
              "claim-only-datascientist": claim_only_datascientist,
              "viewer": viewer}


# ============================================================ Property 8

@st.composite
def registry_states(draw):
    """(global row or None, use-case row or None), each (role, status)."""
    row = st.one_of(st.none(), st.tuples(st.sampled_from(ROW_ROLE_VALUES),
                                         st.sampled_from(STATUS_VALUES)))
    return draw(row), draw(row)


claim_values = st.one_of(st.sampled_from(VALID_ROLE_CLAIMS), st.none(),
                         st.sampled_from(["", "Admin", "portaladmin", "Data Scientist"]))


class TestProperty8ValidateEqualsImport:
    """**Feature: portal-usecase-bucket-config, Property 8: validate's
    accept/deny equals import's** — for any registry state and Claimed_Role,
    in both PORTAL_REGISTRY_ENFORCED states, `POST /models/validate` and
    `POST /models/import` decide identically; an admitted principal reaches
    the artifact validator on both routes, a denied one reaches no
    credential step. Validates: Requirements 3.1, 3.3, 3.4, 15.6"""

    @settings(max_examples=100, deadline=None)
    @given(state=registry_states(), claim=claim_values)
    def test_decisions_agree_in_both_flag_states(self, h, state, claim):
        sub = new_sub()
        global_row, usecase_row = state
        if global_row is not None:
            put_row(h.stack.tables.user_roles, sub, "global", *global_row)
        if usecase_row is not None:
            put_row(h.stack.tables.user_roles, sub, h.usecase_id, *usecase_row)

        for enabled in (False, True):
            with enforcement(h.shared, enabled):
                h.reset()
                v_status = validate_status(h, sub, claim)
                i_status = import_status(h, sub, claim)
            assert (v_status == 403) == (i_status == 403), (v_status, i_status, state, claim)
            if v_status == 403:
                assert h.validator.calls == []
                assert h.assume.calls == []
                assert h.items == []
            else:
                assert (v_status, i_status) == (200, 201)
                assert [kw["purpose"] for _, kw in h.validator.calls] == [
                    "model-validate", "model-import"]


@pytest.mark.parametrize("principal", sorted(PRINCIPALS))
@pytest.mark.parametrize("enabled", [False, True], ids=["flag-off", "flag-on"])
def test_validate_equals_import_for_the_three_principals(h, principal, enabled):
    """Requirement 15.6: the registry DataScientist is admitted by both
    routes in both states; the claim-only DataScientist and the Viewer are
    denied by both in both states (no user_info: the claim grants
    nothing here, in either state)."""
    sub, claim = PRINCIPALS[principal](h)
    with enforcement(h.shared, enabled):
        h.reset()
        v_status = validate_status(h, sub, claim)
        i_status = import_status(h, sub, claim)

    expected_admitted = principal == "registry-datascientist"
    assert (v_status, i_status) == ((200, 201) if expected_admitted else (403, 403))
    if not expected_admitted:
        assert h.validator.calls == [] and h.assume.calls == [] and h.items == []


# ------------------------------------------------ create_dda_job's gate

@pytest.mark.parametrize("principal, enabled, admitted", [
    ("registry-datascientist", False, True),
    ("registry-datascientist", True, True),
    ("claim-only-datascientist", False, True),
    ("claim-only-datascientist", True, False),
    ("viewer", False, False),
    ("viewer", True, False),
], ids=["registry-datascientist-flag-off", "registry-datascientist-flag-on",
        "claim-only-datascientist-flag-off", "claim-only-datascientist-flag-on",
        "viewer-flag-off", "viewer-flag-on"])
def test_create_dda_job_gate_in_both_flag_states(h, principal, enabled, admitted):
    """Requirement 4.6, 15.6: the has_permission(..., user_info) gate
    honours the Cognito claim only with the flag off — the claim-only
    DataScientist is admitted off and denied on, the registry
    DataScientist admitted in both, the Viewer denied in both. A denial
    is 403 with an unauthorized_access row and no job, STS or S3 write."""
    sub, claim = PRINCIPALS[principal](h)
    with enforcement(h.shared, enabled):
        h.reset()
        status, body = create_job(h, sub, claim)

    if admitted:
        assert status == 400, body
        assert "validation_errors" in body
        assert unauthorized_rows(h, sub) == []
    else:
        assert status == 403, body
        assert body == {"error": "Insufficient permissions"}
        (row,) = unauthorized_rows(h, sub)
        assert row["resource_type"] == "labeling_job"
        assert row["details"]["required_permission"] == "create_labeling_jobs"
        assert row["details"]["usecase_id"] == h.usecase_id
    assert jobs_of_usecase(h) == []
    assert h.assume.calls == [] and h.worker.calls == []
