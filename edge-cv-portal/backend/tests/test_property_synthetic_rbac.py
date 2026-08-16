"""Property test for RBAC gating of the synthetic API (synthetic-defect-
data-generation, task 4.2).

**Feature: synthetic-defect-data-generation, Property 12: RBAC gating of
all synthetic API operations**

_For any_ Synthetic_Data_Generator API route and any user role: the
request is executed only when the role satisfies Data_Scientist_Access
(DataScientist, UseCaseAdmin, or PortalAdmin for the Use_Case); for all
other roles the response is 403, an audit event recording the denied
attempt is logged, and no state change occurs.

**Validates: Requirements 9.1, 9.2**

Runs against the moto DynamoDB stack from conftest.py + synthetic_env.py,
exercising the real check_user_access / audit code paths through the
handler's route table.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from synthetic_env import SyntheticEnv

ROLES = ("DataScientist", "UseCaseAdmin", "PortalAdmin",
         "Operator", "Viewer", "none")
ALLOWED_ROLES = {"DataScientist", "UseCaseAdmin", "PortalAdmin"}


# Route registry: name -> (method, resource, needs_session, body, query_fn)
def _route_specs():
    return {
        "get_models": ("GET", "/synthetic/models", False, None,
                       lambda uc: {"usecase_id": uc}),
        "get_template": ("GET", "/synthetic/prompt-templates", False, None,
                         lambda uc: {"usecase_id": uc,
                                     "object_type": "casting",
                                     "defect_type": "scratch"}),
        "put_template": ("PUT", "/synthetic/prompt-templates", False,
                         {"object_type": "casting",
                          "defect_type": "scratch",
                          "template_text": "T {object_type} {defect_type}"},
                         None),
        "create_session": ("POST", "/synthetic/sessions", False, {}, None),
        "list_sessions": ("GET", "/synthetic/sessions", False, None,
                          lambda uc: {"usecase_id": uc}),
        "get_session": ("GET", "/synthetic/sessions/{id}", True, None, None),
        "patch_session": ("PATCH", "/synthetic/sessions/{id}", True,
                          {"object_type": "patched"}, None),
        "generate": ("POST", "/synthetic/sessions/{id}/generate", True,
                     {}, None),
        "approval": ("POST", "/synthetic/sessions/{id}/previews/approval",
                     True, {"all": True, "approval_state": "approved"},
                     None),
        "integrate": ("POST", "/synthetic/sessions/{id}/integrate", True,
                      {}, None),
        "retrain": ("POST", "/synthetic/sessions/{id}/retrain", True,
                    {}, None),
    }


ROUTE_NAMES = sorted(_route_specs())


@pytest.fixture(scope="module")
def senv(aws_stack):
    env = SyntheticEnv(aws_stack)
    # The catalog and worker seams are stubbed for the whole module so no
    # route ever reaches Bedrock / Lambda self-invocation.
    sd = env.synthetic_data
    original_list = sd._list_available_model_ids
    original_invoke = sd._invoke_worker_async
    sd._list_available_model_ids = lambda: [
        m["model_id"] for m in sd.MODEL_CATALOG]
    sd._invoke_worker_async = lambda payload: None
    yield env
    sd._list_available_model_ids = original_list
    sd._invoke_worker_async = original_invoke


def _route_table_covers_design_matrix(senv):
    """The handler's route table contains exactly the design's routes."""
    expected = {(m, r) for name, (m, r, *_rest) in _route_specs().items()}
    assert set(senv.synthetic_data.ROUTES) == expected


def test_route_table_matches_design_matrix(senv):
    _route_table_covers_design_matrix(senv)


@settings(deadline=None)
@given(role=st.sampled_from(ROLES), route_name=st.sampled_from(ROUTE_NAMES))
def test_rbac_gating_of_all_synthetic_routes(senv, role, route_name):
    """Executed iff the role satisfies Data_Scientist_Access; otherwise
    403 + unauthorized_access audit event + no state change
    (Requirements 9.1, 9.2)."""
    method, resource, needs_session, body, query_fn = \
        _route_specs()[route_name]

    usecase_id = senv.create_usecase()
    session_id = None
    if needs_session:
        session_id = senv.put_session_meta(usecase_id)
        senv.put_preview(session_id)
    if body is not None and not needs_session:
        body = dict(body)
        body["usecase_id"] = usecase_id
    query = query_fn(usecase_id) if query_fn else None

    actor = senv.actor_with_role(usecase_id, role)
    allowed = role in ALLOWED_ROLES

    before = senv.state_snapshot() if not allowed else None

    status, response_body = senv.invoke(
        method, resource, actor, session_id=session_id, body=body,
        query=query)

    if allowed:
        # Executed: past the RBAC gate (may still fail validation, but
        # never with the RBAC denial).
        assert status != 403, (
            f"{role} must satisfy Data_Scientist_Access on {route_name}: "
            f"{response_body}")
        assert not senv.audit_entries("unauthorized_access",
                                      actor["user_id"])
    else:
        # Denied: 403 with the denial audited and no state change.
        assert status == 403, (
            f"{role} must be denied {route_name}, got {status}: "
            f"{response_body}")
        entries = senv.audit_entries("unauthorized_access",
                                     actor["user_id"])
        assert len(entries) == 1
        entry = entries[0]
        assert entry["result"] == "denied"
        assert entry["details"]["usecase_id"] == usecase_id
        assert senv.state_snapshot() == before, (
            f"denied {route_name} must not change persisted state")


@settings(deadline=None)
@given(route_name=st.sampled_from(
    [n for n in ROUTE_NAMES
     if _route_specs()[n][2]]))  # session-scoped routes only
def test_data_scientist_of_other_usecase_denied(senv, route_name):
    """Data_Scientist_Access is evaluated for the target Use_Case: a
    DataScientist of a different Use_Case is denied (Requirement 9.1)."""
    method, resource, _needs_session, body, _query_fn = \
        _route_specs()[route_name]
    usecase_id = senv.create_usecase()
    session_id = senv.put_session_meta(usecase_id)
    other_usecase = senv.create_usecase("Other Use Case")
    outsider = senv.actor_with_role(other_usecase, "DataScientist")

    status, response_body = senv.invoke(
        method, resource, outsider, session_id=session_id, body=body)
    assert status == 403, (
        f"foreign DataScientist must be denied {route_name}, got "
        f"{status}: {response_body}")
