"""
Property tests for the Portal_Identity resolution path —
portal-jwt-role-privilege-escalation task 2.4.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

Three of the six correctness properties of design.md "Correctness
Properties", one property-based test each, ≥ 100 examples:

* **Property 1: No Claimed_Role grants any permission.** For any
  generated `custom:role` (every valid `Role` value, invalid strings,
  empty, absent) and any generated permission, with no registry row
  present and enforcement on, `has_permission` is false and the
  decorated route answers 403. _Validates: 1.1, 1.4_
* **Property 2: The Effective_Role is exactly the registry's role.** For
  any generated registry state (global row, Use_Case row, both, neither,
  disabled) and any scope, the resolved role equals the row the
  precedence rules select, independent of the token's claim.
  _Validates: 1.2, 1.3, 1.6_
* **Property 3: Absent and unavailable are distinguishable.** For any
  generated failure mode, an absent row yields 403 with
  `identity_source='absent'`, and a raising lookup yields 500 with an
  audited `failure`; neither yields `Viewer`. _Validates: 1.5_

All three are statements about the **enforced** mode (design.md Expected
Behavior), so the module turns `PORTAL_REGISTRY_ENFORCED` on for its own
tests only and restores the environment afterwards — the deployed default
is off until the backfill has run (design.md Decision 4, task 5 flips it).
Nothing about authorization is mocked: the real `rbac_check` /
`super_user_only` decorators, the real `RBACManager`, the real
role/permission matrix, and the real `log_audit_event` writes run against
the conftest moto stack (`test-user-roles` is the Portal_Identity
registry, `test-audit-log` the audit table).

The expected-role oracle (`expected_effective_role` below) is written out
by hand from design.md's Expected Behavior table and the Requirement 1
criteria — it never calls the implementation, so an implementation change
that alters precedence fails here instead of agreeing with itself.

_Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_
"""
import json
import os
import sys
import uuid
from contextlib import contextmanager

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"

# ---------------------------------------------------------------------------
# Generated Claimed_Role values (design.md Glossary: the token's
# `custom:role`, descriptive metadata only). `None` means the claim is
# absent from the token entirely.
# ---------------------------------------------------------------------------

VALID_ROLE_CLAIMS = ("PortalAdmin", "UseCaseAdmin", "DataScientist",
                     "Operator", "Viewer", "DataLabeler")

INVALID_ROLE_CLAIMS = (
    "portaladmin", "PORTALADMIN", "Portal Admin", "Admin", "SuperUser",
    "root", "*", "", "   ", " PortalAdmin", "PortalAdmin ", "PortalAdmin\n",
    "null", "None", "PortalAdmin,DataScientist",
    '{"role": "PortalAdmin"}', "Viewer' OR '1'='1",
)

# Portal_Identity `status` values a row may carry. `None` means the
# attribute is absent, which pre-spec Team Management rows are (treated as
# enabled); anything other than 'enabled' denies (Requirement 1.6).
STATUS_VALUES = (None, "enabled", "ENABLED", "Enabled", "disabled",
                 "DISABLED", "", "active", "suspended")

# Role values a registry row may name, valid and not.
ROW_ROLE_VALUES = VALID_ROLE_CLAIMS + ("", "viewer", "Administrator",
                                       "PortalAdmin ", "Guest")

# The audit actions the two authorization outcomes write
# (rbac_middleware; pinned byte-identical by the preservation suite).
DENIAL_ACTION = "unauthorized_access"
SUPER_USER_DENIAL_ACTION = "unauthorized_super_user_access"
UNAVAILABLE_ACTION = "authorization_unavailable"


def claim_values():
    """A Claimed_Role: valid, invalid, empty, absent, or arbitrary text.

    The free-text arm is restricted to printable ASCII: a Cognito
    attribute value is arbitrary text, but unpaired surrogates cannot be
    encoded to UTF-8 and would fail the DynamoDB write of the audit
    entry rather than the property under test.
    """
    return st.one_of(
        st.sampled_from(VALID_ROLE_CLAIMS),
        st.sampled_from(INVALID_ROLE_CLAIMS),
        st.none(),
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126),
                max_size=24),
    )


def row_states():
    """A generated Portal_Identity row, or None for "no row"."""
    return st.one_of(
        st.none(),
        st.fixed_dictionaries({
            "role": st.sampled_from(ROW_ROLE_VALUES),
            "status": st.sampled_from(STATUS_VALUES),
        }),
    )


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    """The real shared_utils imported inside the moto mock."""
    import shared_utils
    assert hasattr(shared_utils, "RegistryUnavailable"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    return shared_utils


@pytest.fixture(scope="module")
def enforcement_on(shared):
    """PORTAL_REGISTRY_ENFORCED on for this module only.

    `registry_enforcement_enabled()` reads the environment on every call
    (design.md Decision 4), so setting the variable is enough; it is
    restored on teardown so the rest of the session keeps the deployed
    default (off).
    """
    variable = shared.PORTAL_REGISTRY_ENFORCED_ENV
    previous = os.environ.get(variable)
    os.environ[variable] = "true"
    assert shared.registry_enforcement_enabled() is True
    yield
    if previous is None:
        os.environ.pop(variable, None)
    else:
        os.environ[variable] = previous


@pytest.fixture(scope="module")
def middleware(aws_stack):
    """The real rbac_middleware, re-imported inside the moto mock so it
    binds the same shared_utils (Permission enum / rbac_manager
    singleton) the conftest stack was built with."""
    sys.modules.pop("rbac_middleware", None)
    import rbac_middleware
    return rbac_middleware


@pytest.fixture(scope="module")
def registry(aws_stack):
    """The Portal_Identity registry table (dda-portal-user-roles)."""
    return aws_stack.tables.user_roles


@pytest.fixture(scope="module")
def audit(aws_stack):
    """The audit log table the real helpers write to."""
    return aws_stack.tables.audit_log


# ---------------------------------------------------------------- helpers

def new_sub():
    """A fresh Cognito `sub`, so examples never collide in the shared
    session-scoped tables (and so "no registry row" is guaranteed)."""
    return str(uuid.uuid4())


def claims_of(sub, claim, username="generated-user",
              email="generated-user@example.com"):
    """Authorizer claims as the Cognito User Pools authorizer forwards
    them; `claim is None` omits `custom:role` entirely."""
    payload = {"sub": sub, "cognito:username": username, "email": email}
    if claim is not None:
        payload["custom:role"] = claim
    return payload


def request_event(sub, claim, usecase_id=None, resource="/builds",
                  method="POST", source_ip="203.0.113.7",
                  user_agent="property-test/1.0"):
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "pathParameters": {"usecase_id": usecase_id} if usecase_id else None,
        "queryStringParameters": None,
        "body": None,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "authorizer": {"claims": claims_of(sub, claim)},
            "identity": {"sourceIp": source_ip, "userAgent": user_agent},
        },
    }


def user_info_of(sub, claim, username="generated-user",
                 email="generated-user@example.com"):
    """What get_user_from_event returns for these claims: an absent
    `custom:role` becomes the literal 'Viewer' default."""
    return {"user_id": sub, "email": email, "username": username,
            "role": "Viewer" if claim is None else claim}


def ok_handler(event, context):
    """Sentinel handler: reaching it means the guard authorized."""
    return {"statusCode": 200, "body": json.dumps({"ok": True})}


def put_row(registry, sub, usecase_id, row):
    """Write a generated Portal_Identity row (absent `status` when the
    generated state carries None, exactly like a pre-spec Team
    Management row)."""
    item = {"user_id": sub, "usecase_id": usecase_id,
            "role": row["role"], "username": "generated-user",
            "email": "generated-user@example.com",
            "assigned_by": "test", "assigned_at": 1}
    if row["status"] is not None:
        item["status"] = row["status"]
    registry.put_item(Item=item)


def registry_rows(registry, sub):
    return registry.query(
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": sub},
    ).get("Items", [])


def audit_rows(audit, sub, action=None):
    return [item for item in audit.scan().get("Items", [])
            if item.get("user_id") == sub
            and (action is None or item.get("action") == action)]


def body_of(response):
    return json.loads(response["body"])


def counterexample(**fields):
    return json.dumps(fields, indent=2, default=str)


# ---------------------------------------------------------------------------
# The expected-role oracle: design.md's Expected Behavior table and the
# Requirement 1 criteria, written out by hand. It must NOT call the
# implementation.
#
#   1. no enabled global row                 -> None (deny)
#   2. enabled Use_Case row naming a valid role, when the scope IS that
#      Use_Case                              -> that row's role
#   3. otherwise                             -> the global row's role
#   4. a row naming a value that is not a Role grants nothing
#
# "enabled" = the row exists and either carries no `status` (pre-spec
# rows) or carries exactly 'enabled', case-insensitively; every other
# value denies (Requirement 1.6, fail closed).
# ---------------------------------------------------------------------------

def row_is_enabled(row):
    if row is None:
        return False
    status = row["status"]
    if status is None:
        return True
    return str(status).strip().lower() == "enabled"


def row_role(row):
    if row is None:
        return None
    return row["role"] if row["role"] in VALID_ROLE_CLAIMS else None


def expected_effective_role(global_row, usecase_row, scope_is_usecase):
    """The Effective_Role the registry state must produce, as a role
    string or None."""
    if not row_is_enabled(global_row):
        return None
    if scope_is_usecase and row_is_enabled(usecase_row):
        usecase_role = row_role(usecase_row)
        if usecase_role is not None:
            return usecase_role
    return row_role(global_row)


# ===========================================================================
# Property 1: No Claimed_Role grants any permission
# ===========================================================================

class TestProperty1NoClaimedRoleGrantsAnyPermission:
    """**Feature: portal-jwt-role-privilege-escalation, Property 1: No
    Claimed_Role grants any permission.**

    _For any_ generated `custom:role` (every valid `Role` value, invalid
    strings, empty, absent) and any generated permission, with no
    registry row present and enforcement on, `has_permission` is false
    and the decorated route answers 403.

    This is the incident's own condition generalized over every claim and
    every permission: `AdminCreateUser` with an injected `custom:role`
    must buy nothing at all (bugfix.md Bug Condition C1).

    _Validates: Requirements 1.1, 1.4_
    """

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(claim=claim_values(),
           permission_index=st.integers(min_value=0, max_value=9999),
           scope_is_usecase=st.booleans())
    # The recorded incident: custom:role=PortalAdmin, no registry row.
    @example(claim="PortalAdmin", permission_index=0, scope_is_usecase=False)
    # The two claims that actually carry builds:submit through the old
    # JWT fallback (bugfix.md "Where privilege is granted today").
    @example(claim="DataScientist", permission_index=1,
             scope_is_usecase=False)
    @example(claim="UseCaseAdmin", permission_index=2, scope_is_usecase=True)
    # No custom:role attribute at all (get_user_from_event defaults it to
    # 'Viewer'), which must still grant nothing.
    @example(claim=None, permission_index=3, scope_is_usecase=False)
    def test_no_claim_grants_any_permission(self, shared, middleware,
                                            registry, audit,
                                            enforcement_on, claim,
                                            permission_index,
                                            scope_is_usecase):
        # The permission is selected modulo the real Permission enum, so
        # a permission added later is covered without editing this file.
        permissions = sorted(shared.Permission, key=lambda p: p.value)
        permission = permissions[permission_index % len(permissions)]

        sub = new_sub()
        usecase_id = f"uc-{uuid.uuid4()}" if scope_is_usecase else None
        scope = usecase_id or "global"
        info = user_info_of(sub, claim)

        assert registry_rows(registry, sub) == [], (
            "precondition: the principal must be unprovisioned")

        # 1. The permission layer grants nothing.
        assert shared.rbac_manager.has_permission(
            sub, scope, permission, user_info=info) is False, (
            f"claim {claim!r} granted {permission.value} at scope "
            f"{scope!r} with no Portal_Identity row (Requirement 1.1)")

        # 2. No role is resolved at all — not even Viewer.
        resolved = shared.rbac_manager.get_user_role(sub, scope,
                                                     user_info=info)
        assert resolved is None, (
            f"claim {claim!r} resolved {resolved} with no Portal_Identity "
            f"row; an Unprovisioned_Principal holds no role")
        assert shared.rbac_manager.get_user_permissions(
            sub, scope, user_info=info) == set()
        assert shared.rbac_manager.is_portal_admin(sub, user_info=info) \
            is False

        # 3. The real decorated route answers the standard 403 envelope.
        guard = middleware.rbac_check([permission],
                                      allow_global=not scope_is_usecase)
        response = guard(ok_handler)(
            request_event(sub, claim, usecase_id=usecase_id), None)
        assert response["statusCode"] == 403, counterexample(
            claim=claim, permission=permission.value, scope=scope,
            body=response["body"])
        assert body_of(response) == {
            "error": "Insufficient permissions",
            "required_permissions": [permission.value],
            "usecase_id": scope,
        }

        # 4. `super_user_only` denies the same principal, so no claim
        #    reaches the /admin routes' guard either.
        super_response = middleware.super_user_only(ok_handler)(
            request_event(sub, claim, resource="/admin/users",
                          method="GET"), None)
        assert super_response["statusCode"] == 403, counterexample(
            claim=claim, body=super_response["body"])
        assert body_of(super_response) == {
            "error": "Super user access required",
            "required_role": "PortalAdmin",
        }

        # 5. Both denials are attributable and name the Claimed_Role
        #    (Requirement 4.4): identity_source 'absent', user_role
        #    'none' — never 'Viewer'.
        for action in (DENIAL_ACTION, SUPER_USER_DENIAL_ACTION):
            entries = audit_rows(audit, sub, action)
            assert entries, f"the denial was not audited as {action}"
            for entry in entries:
                assert entry["result"] == "denied", entry
                assert entry["identity_source"] == "absent", entry
                assert entry["details"]["user_role"] == "none", entry
                assert entry["details"]["claimed_role"] == (
                    "Viewer" if claim is None else claim), entry


# ===========================================================================
# Property 2: The Effective_Role is exactly the registry's role
# ===========================================================================

class TestProperty2EffectiveRoleIsTheRegistrys:
    """**Feature: portal-jwt-role-privilege-escalation, Property 2: The
    Effective_Role is exactly the registry's role.**

    _For any_ generated registry state (global row, Use_Case row, both,
    neither, disabled) and any scope, the resolved role equals the row
    the precedence rules select, independent of the token's claim.

    "Independent of the claim" is checked by resolving the same state
    three times — under a generated claim, under a deliberately
    conflicting `PortalAdmin` claim, and with no `user_info` at all — and
    requiring one answer (Requirement 1.2, 1.4).

    _Validates: Requirements 1.2, 1.3, 1.6_
    """

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(global_row=row_states(), usecase_row=row_states(),
           scope_is_usecase=st.booleans(), claim=claim_values())
    # A global PortalAdmin row downgraded by a Use_Case row: the shape the
    # preservation suite deliberately leaves unpinned because design.md's
    # Expected Behavior changes it (the Use_Case row now wins).
    @example(global_row={"role": "PortalAdmin", "status": "enabled"},
             usecase_row={"role": "Viewer", "status": "enabled"},
             scope_is_usecase=True, claim="PortalAdmin")
    # A disabled global row denies exactly as an absent one
    # (Requirement 1.6), whatever the Use_Case row says.
    @example(global_row={"role": "PortalAdmin", "status": "disabled"},
             usecase_row={"role": "UseCaseAdmin", "status": "enabled"},
             scope_is_usecase=True, claim="PortalAdmin")
    # A Use_Case row with no global row: settled fail-closed at task 2.1
    # (the enabled global row is the provisioning record).
    @example(global_row=None,
             usecase_row={"role": "UseCaseAdmin", "status": "enabled"},
             scope_is_usecase=True, claim="UseCaseAdmin")
    # A row naming a value that is not a Role grants nothing.
    @example(global_row={"role": "Administrator", "status": "enabled"},
             usecase_row=None, scope_is_usecase=False, claim="PortalAdmin")
    # A row with no `status` attribute at all (pre-spec Team Management
    # row) counts as enabled.
    @example(global_row={"role": "DataScientist", "status": None},
             usecase_row=None, scope_is_usecase=False, claim="Viewer")
    def test_resolved_role_is_the_registry_row(self, shared, registry,
                                               enforcement_on, global_row,
                                               usecase_row,
                                               scope_is_usecase, claim):
        sub = new_sub()
        usecase_id = f"uc-{uuid.uuid4()}"
        scope = usecase_id if scope_is_usecase else "global"

        if global_row is not None:
            put_row(registry, sub, "global", global_row)
        if usecase_row is not None:
            put_row(registry, sub, usecase_id, usecase_row)

        expected = expected_effective_role(global_row, usecase_row,
                                           scope_is_usecase)
        expected_role = None if expected is None else shared.Role(expected)

        resolutions = {
            "generated claim": shared.rbac_manager.get_user_role(
                sub, scope, user_info=user_info_of(sub, claim)),
            "conflicting PortalAdmin claim":
                shared.rbac_manager.get_user_role(
                    sub, scope, user_info=user_info_of(sub, "PortalAdmin")),
            "no user_info": shared.rbac_manager.get_user_role(sub, scope),
        }

        for label, resolved in resolutions.items():
            assert resolved == expected_role, counterexample(
                case=label, global_row=global_row, usecase_row=usecase_row,
                scope=scope, claim=claim, resolved=str(resolved),
                expected=str(expected_role))

        # The permission set follows the resolved role exactly, and an
        # unresolved role carries nothing.
        permissions = shared.rbac_manager.get_user_permissions(
            sub, scope, user_info=user_info_of(sub, claim))
        assert permissions == (
            set() if expected_role is None
            else shared.rbac_manager.role_permissions[expected_role])

        # is_portal_admin is the same statement at the global scope: the
        # Use_Case row never makes an account a portal admin, and a
        # PortalAdmin claim never does either.
        expected_global = expected_effective_role(global_row, usecase_row,
                                                  scope_is_usecase=False)
        assert shared.rbac_manager.is_portal_admin(
            sub, user_info=user_info_of(sub, claim)) is (
            expected_global == "PortalAdmin"), counterexample(
            global_row=global_row, usecase_row=usecase_row, claim=claim)


# ===========================================================================
# Property 3: Absent and unavailable are distinguishable
# ===========================================================================

# Generated "no usable role" shapes (all must deny with 403), mapped to
# the Identity_Source each must record. Two kinds:
#
# * **Unprovisioned** — no enabled *global* row at all, so no row decided:
#   `identity_source='absent'` (design.md Expected Behavior step 3;
#   Requirement 1.6 makes a disabled row indistinguishable from an absent
#   one).
# * **Corrupt row** — an enabled global row exists but names a value that
#   is not a `Role`, so a row WAS found and it granted nothing:
#   `identity_source='registry'`. Recording 'absent' there would be a
#   false statement about the request (a row exists, and its content is
#   the thing to fix), and Requirement 4.1 defines Identity_Source as
#   "which input decided the role".
#
# Either way the answer is 403 and the resolved role is None — never
# `Viewer`, and never the 'unknown' that an unreadable registry records.
ABSENT_MODE_IDENTITY_SOURCE = {
    "no_row": "absent",
    "disabled_global": "absent",
    "usecase_row_only": "absent",
    "disabled_global_with_usecase_row": "absent",
    "role_less_global": "registry",
    "invalid_role_global": "registry",
}
ABSENT_MODES = tuple(sorted(ABSENT_MODE_IDENTITY_SOURCE))

UNAVAILABLE_CLIENT_ERRORS = (
    "ProvisionedThroughputExceededException", "InternalServerError",
    "ResourceNotFoundException", "AccessDeniedException",
    "ThrottlingException", "RequestLimitExceeded",
)

UNAVAILABLE_EXCEPTIONS = ("RuntimeError", "ConnectionError", "TimeoutError",
                          "ValueError")


class _FailingTable:
    """A registry table handle whose every read raises the generated
    failure, standing in for "DynamoDB is down" (design.md Decision 3)."""

    def __init__(self, error_factory):
        self._error_factory = error_factory

    def _raise(self, *args, **kwargs):
        raise self._error_factory()

    get_item = _raise
    query = _raise
    scan = _raise
    put_item = _raise
    update_item = _raise
    delete_item = _raise


class _RegistryFailureResource:
    """A DynamoDB resource proxy that fails ONLY the registry table, so
    the audit write on the failure path still lands (the outage must be
    recorded, not lost)."""

    def __init__(self, real, registry_table_name, error_factory):
        self._real = real
        self._registry_table_name = registry_table_name
        self._error_factory = error_factory

    def Table(self, name):  # noqa: N802 - boto3 resource API
        if name == self._registry_table_name:
            return _FailingTable(self._error_factory)
        return self._real.Table(name)


@contextmanager
def failing_registry(shared, error_factory):
    """Make the Portal_Identity registry unreadable for the duration.

    Restored on exit inside the generated example rather than by the
    `monkeypatch` fixture: a function-scoped fixture is set up once for
    the whole Hypothesis run, so an unrestored patch would leak into the
    following examples.
    """
    real_resource = shared.dynamodb
    shared.dynamodb = _RegistryFailureResource(
        real_resource, shared.USER_ROLES_TABLE, error_factory)
    try:
        yield
    finally:
        shared.dynamodb = real_resource


class TestProperty3AbsentAndUnavailableAreDistinguishable:
    """**Feature: portal-jwt-role-privilege-escalation, Property 3:
    Absent and unavailable are distinguishable.**

    _For any_ generated failure mode, an absent row yields 403 with
    `identity_source='absent'`, and a raising lookup yields 500 with an
    audited `failure`; neither yields `Viewer`.

    The permission exercised is `usecases:view`, which **Viewer holds** —
    so a silent downgrade to Viewer on either path (today's behaviour,
    `shared_utils.py:663-664`) would authorize the request and fail this
    test rather than pass it unnoticed (Requirement 1.5).

    The generated deny-modes cover both "no enabled global row" (recorded
    `absent`) and "an enabled row naming a value that is not a Role"
    (recorded `registry`, because a row did decide) — see
    ABSENT_MODE_IDENTITY_SOURCE. An unreadable registry records neither:
    it records `unknown`, which is what makes the outage
    distinguishable from every permission decision in the audit log.

    _Validates: Requirement 1.5_
    """

    @staticmethod
    def _error_factory(kind, code):
        if kind == "client_error":
            return lambda: ClientError(
                {"Error": {"Code": code, "Message": f"{code} (generated)"}},
                "GetItem")
        exception_type = {"RuntimeError": RuntimeError,
                          "ConnectionError": ConnectionError,
                          "TimeoutError": TimeoutError,
                          "ValueError": ValueError}[code]
        return lambda: exception_type(f"{code} (generated)")

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(failure=st.one_of(
        st.tuples(st.just("absent"), st.sampled_from(ABSENT_MODES)),
        st.tuples(st.just("client_error"),
                  st.sampled_from(UNAVAILABLE_CLIENT_ERRORS)),
        st.tuples(st.just("exception"),
                  st.sampled_from(UNAVAILABLE_EXCEPTIONS))),
        claim=claim_values(), scope_is_usecase=st.booleans())
    @example(failure=("absent", "no_row"), claim="PortalAdmin",
             scope_is_usecase=False)
    @example(failure=("absent", "disabled_global"), claim="PortalAdmin",
             scope_is_usecase=False)
    @example(failure=("absent", "invalid_role_global"), claim="PortalAdmin",
             scope_is_usecase=False)
    @example(failure=("client_error", "InternalServerError"),
             claim="PortalAdmin", scope_is_usecase=False)
    @example(failure=("exception", "ConnectionError"), claim="Viewer",
             scope_is_usecase=True)
    def test_absent_denies_and_unavailable_fails(
            self, shared, middleware, registry, audit, enforcement_on,
            failure, claim, scope_is_usecase):
        kind, detail = failure
        sub = new_sub()
        usecase_id = f"uc-{uuid.uuid4()}" if scope_is_usecase else None
        scope = usecase_id or "global"
        info = user_info_of(sub, claim)
        # A permission Viewer carries: neither path may authorize it.
        permission = shared.Permission.VIEW_USECASES
        guard = middleware.rbac_check([permission],
                                      allow_global=not scope_is_usecase)
        event = request_event(sub, claim, usecase_id=usecase_id,
                              resource="/usecases", method="GET")

        if kind == "absent":
            self._seed_absent_state(registry, sub, usecase_id, detail)

            resolved = shared.rbac_manager.get_user_role(sub, scope,
                                                         user_info=info)
            assert resolved is None, (
                f"{detail} resolved {resolved}; an absent/disabled row "
                f"must resolve to no role, never Viewer")

            response = guard(ok_handler)(event, None)
            assert response["statusCode"] == 403, counterexample(
                mode=detail, claim=claim, body=response["body"])
            assert body_of(response) == {
                "error": "Insufficient permissions",
                "required_permissions": [permission.value],
                "usecase_id": scope,
            }

            entries = audit_rows(audit, sub, DENIAL_ACTION)
            assert len(entries) == 1, entries
            assert entries[0]["result"] == "denied", entries[0]
            assert entries[0]["identity_source"] == \
                ABSENT_MODE_IDENTITY_SOURCE[detail], counterexample(
                mode=detail, entry=entries[0])
            assert entries[0]["details"]["user_role"] == "none", entries[0]
            # An availability failure was NOT recorded: this was a
            # privilege decision.
            assert audit_rows(audit, sub, UNAVAILABLE_ACTION) == []
            return

        # ------------------------------------------------ unavailable
        error_factory = self._error_factory(kind, detail)
        with failing_registry(shared, error_factory):
            # The lookup failure surfaces as RegistryUnavailable — it is
            # never converted into a role (and never into Viewer).
            with pytest.raises(shared.RegistryUnavailable):
                shared.rbac_manager.get_user_role(sub, scope,
                                                  user_info=info)
            with pytest.raises(shared.RegistryUnavailable):
                shared.rbac_manager.has_permission(sub, scope, permission,
                                                   user_info=info)

            response = guard(ok_handler)(event, None)

        assert response["statusCode"] == 500, counterexample(
            mode=detail, claim=claim, body=response["body"])
        assert body_of(response) == {"error": "Authorization check failed"}

        entries = audit_rows(audit, sub, UNAVAILABLE_ACTION)
        assert len(entries) == 1, entries
        entry = entries[0]
        assert entry["result"] == "failure", entry
        # 'absent' would be a fabricated claim about a request whose
        # registry could not be read at all, so the Identity_Source is
        # 'unknown' — which is what makes the two cases distinguishable
        # in the audit log.
        assert entry["identity_source"] == "unknown", entry
        assert entry["details"]["claimed_role"] == (
            "Viewer" if claim is None else claim), entry
        # The outage is not recorded as a privilege denial.
        assert audit_rows(audit, sub, DENIAL_ACTION) == []

    @staticmethod
    def _seed_absent_state(registry, sub, usecase_id, mode):
        """Seed one of the generated "no enabled row" shapes."""
        if mode == "no_row":
            return
        if mode == "disabled_global":
            put_row(registry, sub, "global",
                    {"role": "PortalAdmin", "status": "disabled"})
        elif mode == "role_less_global":
            put_row(registry, sub, "global",
                    {"role": "", "status": "enabled"})
        elif mode == "invalid_role_global":
            put_row(registry, sub, "global",
                    {"role": "Administrator", "status": "enabled"})
        elif mode == "usecase_row_only":
            if usecase_id:
                put_row(registry, sub, usecase_id,
                        {"role": "UseCaseAdmin", "status": "enabled"})
        elif mode == "disabled_global_with_usecase_row":
            put_row(registry, sub, "global",
                    {"role": "UseCaseAdmin", "status": "disabled"})
            if usecase_id:
                put_row(registry, sub, usecase_id,
                        {"role": "PortalAdmin", "status": "enabled"})
