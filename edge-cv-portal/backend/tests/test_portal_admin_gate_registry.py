"""
The PortalAdmin gate resolves privilege from the Portal_Identity registry,
not from the token's `custom:role` claim.

Spec: `.kiro/specs/portal-jwt-role-privilege-escalation/`
      (bugfix.md = requirements, design.md = source of truth)

Why this suite exists
---------------------
Task 5.2 turned `PORTAL_REGISTRY_ENFORCED` on in account 164152369890 on
2026-09-22 and then verified the incident sequence against the deployed
portal. The enforced `rbac_check` path denied it correctly — a pool account
carrying only `custom:role=PortalAdmin`, with no registry row, got 403 from
`POST /builds`, the exact call the incident got accepted. But the same
token read the **entire user directory** from `GET /admin/users` with 200,
because `user_admin.require_portal_admin` compared
`get_user_from_event(event)['role']` directly: a second authorization path
that never reached `RBACManager`, so enforcement did not apply to it. The
mirror case failed too — a registry PortalAdmin with no claim was denied.

`shared_utils.caller_is_portal_admin` is now the one helper those gates
share, and these tests pin both directions so the claim can never become an
authorization decision again. The assertions are about the **gate**, so
they drive the decorator (and the helper) directly rather than standing up
Cognito: the routes' own wiring is pinned separately by
`test_portal_admin_gate_routes_are_gated`.

Isolation: registry reads go to the conftest `user_roles` table, emptied of
the subs each test uses; every principal is a fresh uuid.

Run from `edge-cv-portal/backend` with
`~/.venvs/dda-portal-tests/bin/python -m pytest <this file> -q
-p no:cacheprovider`.

_Requirements: 1.1, 1.4, 1.5, 2.4, 4.1, 4.2_
"""
import json
import sys
import uuid

import pytest


REGION = "us-east-1"

# The claim the 2026-09-17 incident asserted, and that must now grant
# nothing on its own.
INCIDENT_CLAIM = "PortalAdmin"
INCIDENT_SOURCE_IP = "12.148.187.67"
INCIDENT_USER_AGENT = "aws-cli/1.36.4"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def gate(aws_stack):
    """The real `user_admin` gate plus the shared layer, imported inside
    the moto mock.

    `user_admin` and `rbac_middleware` are re-imported so they bind the
    same `shared_utils` instance (and therefore the same `rbac_manager`)
    the conftest stack was built with.
    """
    for module_name in ("user_admin", "rbac_middleware"):
        sys.modules.pop(module_name, None)
    import rbac_middleware  # noqa: F401 - rebinding, imported by user_admin
    import shared_utils
    import user_admin

    assert hasattr(shared_utils, "caller_is_portal_admin"), (
        "shared_utils has no caller_is_portal_admin: the gate helper this "
        "suite pins is missing from the layer")
    from types import SimpleNamespace
    return SimpleNamespace(
        user_admin=user_admin,
        shared=shared_utils,
        registry=aws_stack.tables.user_roles,
        audit=aws_stack.tables.audit_log,
    )


@pytest.fixture
def enforcement_on(gate, monkeypatch):
    """Enforcement as task 5.2 deployed it."""
    monkeypatch.setenv(gate.shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")


@pytest.fixture
def enforcement_off(gate, monkeypatch):
    """The pre-5.2 deployed default, kept so the flag is demonstrably the
    only difference in behaviour."""
    monkeypatch.delenv(gate.shared.PORTAL_REGISTRY_ENFORCED_ENV,
                       raising=False)


# ---------------------------------------------------------------- helpers

def new_sub():
    """A fresh Cognito `sub`: the incident principal's shape, with no
    portal-side record anywhere."""
    return str(uuid.uuid4())


def claims(sub, claimed_role=INCIDENT_CLAIM, include_role=True,
           username="kiro-gate-test"):
    """Authorizer claims exactly as the Cognito User Pools authorizer
    forwards them. `include_role=False` omits `custom:role` entirely, which
    is the shape of an account provisioned only in the registry."""
    out = {"sub": sub, "cognito:username": username,
           "email": f"{username}@example.test"}
    if include_role:
        out["custom:role"] = claimed_role
    return out


def admin_users_event(user_claims):
    """API Gateway REST event for `GET /admin/users`, carrying the
    identity block attribution is read from (Requirement 4.2)."""
    return {
        "resource": "/admin/users",
        "httpMethod": "GET",
        "path": "/admin/users",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": None,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "authorizer": {"claims": user_claims},
            "identity": {"sourceIp": INCIDENT_SOURCE_IP,
                         "userAgent": INCIDENT_USER_AGENT},
        },
    }


def provision(gate, sub, role, usecase_id="global", status="enabled"):
    """Write a Portal_Identity row — the only privilege source under
    enforcement."""
    gate.registry.put_item(Item={
        "user_id": sub, "usecase_id": usecase_id, "role": role,
        "status": status, "username": "kiro-gate-test",
        "assigned_by": "test", "assigned_at": 1,
    })


def call_gate(gate, user_claims):
    """Drive a trivial handler behind the real `@require_portal_admin`.

    Returns (status_code, parsed_body). The wrapped function records that
    it ran, so "allowed" is proven by execution rather than by the status
    code alone.
    """
    ran = []

    @gate.user_admin.require_portal_admin
    def protected(event, *args, **kwargs):
        ran.append(True)
        return gate.shared.create_response(200, {"ok": True})

    response = protected(admin_users_event(user_claims), None)
    return (response["statusCode"], json.loads(response["body"]),
            bool(ran))


def audit_entries(gate, sub):
    """Audit rows written for one principal."""
    return [item for item in gate.audit.scan().get("Items", [])
            if item.get("user_id") == sub]


# ------------------------------------------------- the incident direction

def test_claim_only_portal_admin_is_denied_under_enforcement(
        gate, enforcement_on):
    """The 2026-09-22 finding: `custom:role=PortalAdmin` with no registry
    row read the whole user directory. It must now be denied, and the
    protected function must not run at all (Requirements 1.1, 1.4)."""
    sub = new_sub()

    status, body, ran = call_gate(gate, claims(sub))

    assert status == 403, (
        f"a claim-only PortalAdmin ({sub}) was allowed through the gate "
        f"with {status}: the escalation the spec exists to close is open "
        f"on every @require_portal_admin route")
    assert not ran, "the protected handler ran despite the 403"
    assert body["message"] == "PortalAdmin role required"


def test_claim_only_denial_is_attributable_and_records_the_claim(
        gate, enforcement_on):
    """The denial is audited with the Claimed_Role and the request's own
    identity, so it stays attributable after the Cognito account is
    deleted — the second half of the incident (Requirements 4.1, 4.2)."""
    sub = new_sub()

    call_gate(gate, claims(sub))

    denials = [entry for entry in audit_entries(gate, sub)
               if entry.get("action") == "unauthorized_access"]
    assert denials, (
        f"no unauthorized_access audit entry for {sub}: a denied "
        f"escalation attempt left no record")
    entry = denials[0]
    assert entry.get("result") == "denied"
    details = entry.get("details") or {}
    if isinstance(details, str):
        details = json.loads(details)
    assert details.get("claimed_role") == INCIDENT_CLAIM, (
        "the Claimed_Role is missing from the denial details; a claim of "
        "PortalAdmin on a denied request is the escalation's signature")
    assert details.get("required_role") == "PortalAdmin"


def test_registry_viewer_beats_a_portal_admin_claim(gate, enforcement_on):
    """A real row that says Viewer wins over a claim that says
    PortalAdmin: the registry is the only source (Requirement 1.4)."""
    sub = new_sub()
    provision(gate, sub, "Viewer")

    status, _, ran = call_gate(gate, claims(sub, claimed_role="PortalAdmin"))

    assert status == 403, (
        "a Viewer registry row did not override a PortalAdmin claim")
    assert not ran


def test_disabled_registry_admin_is_denied(gate, enforcement_on):
    """A disabled Portal_Identity row denies exactly as an absent one
    (Requirement 1.6), even with a PortalAdmin claim."""
    sub = new_sub()
    provision(gate, sub, "PortalAdmin", status="disabled")

    status, _, ran = call_gate(gate, claims(sub))

    assert status == 403
    assert not ran


# ------------------------------------------------ the availability direction

def test_registry_portal_admin_without_a_claim_is_allowed(
        gate, enforcement_on):
    """The mirror of the finding: an account provisioned only in the
    registry (no `custom:role` at all) must be allowed. Before the fix the
    gate denied it, so a correctly provisioned admin could not administer
    users (Requirement 1.1)."""
    sub = new_sub()
    provision(gate, sub, "PortalAdmin")

    status, body, ran = call_gate(gate, claims(sub, include_role=False))

    assert status == 200, (
        f"a registry PortalAdmin with no claim was denied with {status}: "
        f"enforcement would lock legitimate admins out of user "
        f"administration")
    assert ran, "the protected handler did not run despite the 200"
    assert body == {"ok": True}


def test_registry_outage_answers_500_not_403(gate, enforcement_on,
                                             monkeypatch):
    """An unreadable registry is an availability failure, never a
    privilege decision: 500, and never a silent denial
    (Requirement 1.5, design.md Decision 3)."""
    sub = new_sub()
    provision(gate, sub, "PortalAdmin")

    def explode(*_args, **_kwargs):
        raise gate.shared.RegistryUnavailable("registry is down")

    monkeypatch.setattr(gate.shared.rbac_manager, "is_portal_admin", explode)

    status, body, ran = call_gate(gate, claims(sub))

    assert status == 500, (
        f"a registry outage answered {status}; a DynamoDB failure must not "
        f"be reported as a privilege decision")
    assert not ran
    assert body["error"] == "Authorization check failed"
    failures = [entry for entry in audit_entries(gate, sub)
                if entry.get("result") == "failure"]
    assert failures, "the outage was not audited as a failure"


# ---------------------------------------------------------- the flag itself

def test_with_enforcement_off_the_legacy_claim_behaviour_is_unchanged(
        gate, enforcement_off):
    """With the flag off, resolution is the pre-fix order — the claim still
    grants. This is what task 5.2 flipped, and pinning it keeps the flag
    the only difference between the two modes (Requirement 2.4)."""
    sub = new_sub()

    status, _, ran = call_gate(gate, claims(sub))

    assert status == 200, (
        "with PORTAL_REGISTRY_ENFORCED off the legacy claim path changed; "
        "the flag is no longer the only difference between the modes")
    assert ran


# ------------------------------------------------------- the wiring itself

def test_portal_admin_gate_routes_are_gated(gate):
    """Every PortalAdmin-only route in `user_admin` is still wrapped by
    `require_portal_admin`.

    The gate is only worth as much as its application: this fails if a
    route is added or un-decorated, which is how the surface silently grew
    before.
    """
    expected = [
        "list_accounts", "create_account", "set_password", "forgot_password",
        "change_role", "disable_account", "enable_account", "delete_account",
        "list_sync_devices", "sync_device",
    ]
    ungated = [name for name in expected
               if not hasattr(getattr(gate.user_admin, name), "__wrapped__")]
    assert not ungated, (
        f"these PortalAdmin-only routes are not wrapped by "
        f"require_portal_admin: {ungated}")


def test_no_handler_authorizes_on_the_claim_directly():
    """No portal handler decides PortalAdmin from the Claimed_Role.

    A grep-level guard: `get_user_from_event(event)['role']` is descriptive
    metadata (design.md Decision 2), and comparing it to 'PortalAdmin' is
    exactly the bug this suite closes. Reintroducing that comparison in any
    handler fails here rather than in production.
    """
    import pathlib
    import re

    functions_dir = pathlib.Path(__file__).resolve().parents[1] / "functions"
    pattern = re.compile(
        r"""user(?:_info)?(?:\s*or\s*\{\})?\.get\(\s*['"]role['"]\s*\)"""
        r"""\s*[!=]=\s*['"]PortalAdmin['"]""")
    offenders = []
    for path in sorted(functions_dir.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        f"claim-based PortalAdmin authorization reintroduced at "
        f"{offenders}; resolve the Effective_Role with "
        f"shared_utils.caller_is_portal_admin instead")
