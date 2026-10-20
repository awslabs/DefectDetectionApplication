"""
Properties 4 and 5 of portal-authorization-and-iam-scoping (design I2 "Test
strategy"; task 2.3): a gateway event is never an Internal_Invocation, and
the compile / package / publish role decision is exactly
`internal or has_role`.

Property 5 runs the real handlers behind internal_gate_fakes (module-scoped:
hypothesis bodies may not use function-scoped fixtures), with the role check
on the moto user-roles table in both enforcement states.

Validates: Requirements 4.1, 4.2, 4.3, 4.5, 4.6
"""
import json

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

import internal_gate_fakes as gf
import internal_invocation
from internal_invocation import INTERNAL_MARKER_KEY, internal_marker

PROPERTY_SETTINGS = settings(max_examples=100, deadline=None,
                             suppress_health_check=[HealthCheck.too_slow])
TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20)
JSONISH = st.recursive(st.none() | st.booleans() | st.integers() | TEXT,
                       lambda children: st.lists(children, max_size=3)
                       | st.dictionaries(TEXT, children, max_size=3), max_leaves=6)
ROLE_HOLDERS = ("DataScientist", "UseCaseAdmin", "PortalAdmin")


# --------------------------------------------------------------- Property 4

class TestProperty4GatewayEventsAreNeverInternal:
    """**Feature: portal-authorization-and-iam-scoping, Property 4: A gateway
    event is never an Internal_Invocation** — for any event with arbitrary
    body, headers, extra keys and marker (well-formed or not) whose
    requestContext carries a gateway requestId, is_internal_invocation is
    false; and the shared layer's re-export is the same function.
    Validates: Requirements 4.1"""

    @PROPERTY_SETTINGS
    @given(body=st.one_of(st.none(), TEXT), headers=st.dictionaries(TEXT, TEXT, max_size=3),
           extra=st.dictionaries(TEXT, JSONISH, max_size=3),
           marker=st.one_of(st.none(), JSONISH, st.builds(
               internal_marker, TEXT, TEXT, TEXT)),
           request_id=st.text(min_size=1, max_size=20),
           api_id=st.one_of(st.none(), TEXT),
           claims=st.dictionaries(TEXT, TEXT, max_size=3))
    def test_request_id_means_external(self, body, headers, extra, marker, request_id, api_id, claims):
        candidate = dict(extra)
        candidate.update({"httpMethod": "POST", "body": body, "headers": headers,
                          "requestContext": {"requestId": request_id, "apiId": api_id,
                                             "authorizer": {"claims": claims}}})
        if marker is not None:
            candidate[INTERNAL_MARKER_KEY] = marker
        event("marker dict" if isinstance(marker, dict) else "no usable marker")
        assert internal_invocation.is_internal_invocation(candidate) is False

    def test_shared_utils_re_exports_the_same_names(self, aws_stack):
        import shared_utils
        assert shared_utils.is_internal_invocation is internal_invocation.is_internal_invocation
        assert shared_utils.internal_marker is internal_invocation.internal_marker
        assert shared_utils.INTERNAL_MARKER_KEY == INTERNAL_MARKER_KEY == "dda_internal"


# --------------------------------------------------------------- Property 5

@pytest.fixture(scope="module")
def gates(aws_stack):
    import shared_utils
    mp = pytest.MonkeyPatch()
    harness = gf.install(aws_stack, mp)
    harness.shared = shared_utils
    harness.mp = mp
    yield harness
    mp.undo()


def request_for(gates, handler, training_id, caller, internal, replayed_marker):
    path = f"/api/v1/training/{training_id}/{handler}"
    body = {"compile": {"targets": ["x86_64-cpu"], "auto_triggered": True},
            "package": {"auto_triggered": True},
            "publish": {"component_name": "model-gate-model", "component_version": "1.0.0",
                        "auto_triggered": True}}[handler]
    marker = internal_marker("property", handler, caller["user_id"])
    if internal:
        return {"httpMethod": "POST", "path": path, "pathParameters": {"id": training_id},
                "body": json.dumps(body),
                "requestContext": {"authorizer": {"claims": {
                    "sub": caller["user_id"], "email": caller["email"],
                    "cognito:username": caller["username"]}}},
                INTERNAL_MARKER_KEY: marker}
    extra = {INTERNAL_MARKER_KEY: marker} if replayed_marker else {}
    return gates.gateway_event(path, training_id, caller, body, **extra)


class TestProperty5DecisionIsInternalOrRole:
    """**Feature: portal-authorization-and-iam-scoping, Property 5: The
    compile / package / publish role decision is internal or has_role** —
    for any generated (Internal_Invocation, role on the use case) pair, with
    or without a marker replayed onto a gateway event, in both enforcement
    states, each handler admits the request exactly when it is an
    Internal_Invocation or the caller holds DataScientist or above; an
    admitted request is attributed to the marker's user when internal and to
    the caller otherwise.
    Validates: Requirements 4.2, 4.3, 4.5, 4.6"""

    @PROPERTY_SETTINGS
    @given(handler=st.sampled_from(["compile", "package", "publish"]),
           internal=st.booleans(),
           replayed_marker=st.booleans(),
           role=st.one_of(st.none(), st.sampled_from(gf.ROLES)),
           enforced=st.booleans())
    def test_decision(self, gates, handler, internal, replayed_marker, role, enforced):
        if enforced:
            gates.mp.setenv(gates.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
        else:
            gates.mp.delenv(gates.shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
        gates.reset()
        ids = gates.seed_jobs()
        caller = gates.user(role)
        has_role = role in ROLE_HOLDERS
        assert gates.shared.check_user_access(
            caller["user_id"], gates.usecase_id, "DataScientist", caller) is has_role
        event(f"{handler} internal={internal} has_role={has_role}")

        status, payload = getattr(gates, handler)(
            request_for(gates, handler, ids[handler], caller, internal, replayed_marker))
        assert (status == 200) is (internal or has_role), payload
        if status != 200:
            assert status == 403 and gates.audit.rows == []
        else:
            action = {"compile": "start_compilation", "package": "package_components",
                      "publish": "publish_greengrass_component"}[handler]
            [row] = gates.audit.of(action)
            assert row["user_id"] == caller["user_id"]
            if handler == "compile":
                assert ("internal_source" in row["details"]) is internal
