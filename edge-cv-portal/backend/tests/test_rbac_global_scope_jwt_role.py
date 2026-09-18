"""
JWT-role propagation through the RBAC middleware at global scope
(build-fleet 403 bugfix).

Bug: rbac_middleware.rbac_check calls rbac_manager.has_permission(...)
WITHOUT the user_info kwarg, so the JWT custom:role claim (extracted by
get_user_from_event into user['role']) never reaches role resolution.
For allow_global=True routes the scope is 'global', which has no
per-usecase UserRoles row for JWT-only users, so the role defaults to
Viewer and the request is denied with 403 "Insufficient permissions".
Live evidence: a Cognito PortalAdmin (custom:role=PortalAdmin, no
UserRoles rows) denied builds:read on the deployed BuildFleetHandler.
super_user_only shares the gap via is_portal_admin without user_info.

These tests must FAIL on unfixed code (403) and pass once rbac_check /
super_user_only thread the already-extracted user dict as user_info
into every rbac_manager call.

Runs against the moto-backed stack from conftest.py, exercising the
real shared_utils role resolution (no RBACManager patching).

===========================================================================
RECORDED REPOINT (spec portal-jwt-role-privilege-escalation, task 3.3,
Requirement 7.4; design.md "Repoint, recorded")
===========================================================================

The sentence struck from the paragraph above was: "the users below have NO
dda-portal-user-roles rows, only the JWT claim". That premise is the direct
inverse of Requirement 1.1 — a self-asserted `custom:role` claim must grant
NOTHING, because the recorded incident (bugfix.md) is exactly a Cognito user
created out of band with `custom:role=PortalAdmin`, no registry row, that
submitted a JP7 build and was deleted 6 seconds later. A suite asserting
that such a principal gets 200 cannot coexist with the fix.

This is a REPOINT, not a weakening. Every case keeps its assertion
(200/403, same envelope) and every case's ORIGINAL body is recorded
verbatim in a comment directly above it. The only change is that each
principal is now PROVISIONED: a global Portal_Identity row
(`dda-portal-user-roles`, `usecase_id='global'`, `status='enabled'`)
naming the same role its claim asserts — the post-backfill state task 3.1's
`backfill_portal_registry.py` produces for every enabled pool user. What the
suite proves is therefore the half of its intent that survives enforcement,
and it is the half the live 403 was about:

* a **provisioned** PortalAdmin / DataScientist is NOT spuriously denied at
  the 'global' scope used by `allow_global=True` build routes, even though
  no per-Use_Case row exists for that scope (the deployed BuildFleetHandler
  403);
* the fix does not over-grant: a provisioned Viewer is still denied;
* `rbac_check` / `super_user_only` still thread the extracted user dict as
  `user_info` into every `rbac_manager` call — the literal mechanism the
  original bug was (see TestUserInfoStillThreaded, which pins it
  independently of whether the claim decides anything).

Each case now runs in BOTH modes of `PORTAL_REGISTRY_ENFORCED`
(`registry_mode`): off, the deployed default today, where resolution is the
pre-fix order; and on, where the registry is the only source of privilege
(task 5 flips it). A provisioned principal must be authorized either way,
which is precisely what makes the flip safe for the accounts that work
today.

The one thing this suite no longer asserts — that an UNPROVISIONED claim is
authorized — is now asserted in the negative, with the incident's own
submission, by `test_portal_registry_privilege_exploration.py`.
"""
import json
import sys
import uuid

import pytest


# The conftest stack's region (see tests/conftest.py REGION).
REGION = "us-east-1"


# --------------------------------------------------------------- fixtures

@pytest.fixture
def shared(aws_stack):
    """The real shared_utils module imported inside the moto mock."""
    import shared_utils
    return shared_utils


@pytest.fixture
def middleware(aws_stack):
    """The real rbac_middleware module, re-imported inside the moto
    mock so it binds the same shared_utils (Permission / rbac_manager)
    the stack was built with."""
    sys.modules.pop("rbac_middleware", None)
    import rbac_middleware
    return rbac_middleware


@pytest.fixture
def registry(shared):
    """The Portal_Identity registry table (`dda-portal-user-roles` in the
    account, the conftest table here), resolved through the name
    shared_utils itself reads so a suite that re-points that name cannot
    leave this one writing to a table nobody reads."""
    import boto3
    return boto3.resource("dynamodb", region_name=REGION).Table(
        shared.USER_ROLES_TABLE)


@pytest.fixture(params=["legacy", "enforced"])
def registry_mode(request, monkeypatch):
    """Run every case under both settings of PORTAL_REGISTRY_ENFORCED.

    'legacy' is today's deployed default (the flag absent, pre-fix
    resolution order); 'enforced' is the state task 5 flips to, where the
    Portal_Identity registry is the only source of privilege. A
    provisioned principal must be authorized in both. The flag is set
    through monkeypatch so it is restored after each test (a leaked flag
    silently changes every later suite in the process).
    """
    if request.param == "enforced":
        monkeypatch.setenv("PORTAL_REGISTRY_ENFORCED", "true")
    else:
        monkeypatch.delenv("PORTAL_REGISTRY_ENFORCED", raising=False)
    return request.param


def make_user(role="Viewer"):
    """A user with a JWT role claim. A fresh uuid `sub` per call, so the
    registry rows provisioned below never collide with another case's (the
    conftest tables are session-scoped)."""
    user_id = f"user-{uuid.uuid4()}"
    return {
        "user_id": user_id,
        "email": f"{user_id}@example.com",
        "username": user_id,
        "role": role,
    }


def provision(registry, user, role=None, usecase_id="global"):
    """Write the user's Portal_Identity row exactly as the backfill and
    the User Manager write it: `role`, `username`, `email`,
    `status='enabled'` (task 3.1 `backfill_portal_registry.py`,
    task 2.3 `user_admin._put_registry_identity`).

    Defaults to the role the JWT claim asserts, i.e. the post-backfill
    state of an account that works today — the claim and the registry
    agree, so the case's outcome is the same in both flag modes.
    """
    registry.put_item(Item={
        "user_id": user["user_id"],
        "usecase_id": usecase_id,
        "role": role or user["role"],
        "username": user["username"],
        "email": user["email"],
        "status": "enabled",
        "assigned_by": "backfill",
        "assigned_at": 1,
    })


def api_event(user, method="GET", resource="/builds", body=None):
    """Synthetic API Gateway event with Cognito claims (no usecase_id:
    build routes are global-scope)."""
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            }
        },
    }


def ok_handler(event, context):
    return {"statusCode": 200, "body": json.dumps({"ok": True})}


# ------------------------------------------- rbac_check at global scope

class TestGlobalScopeJwtRole:
    """A PROVISIONED user (registry row + matching claim) must be
    authorized by rbac_check(..., allow_global=True) when that role holds
    the required permission — the live build-fleet 403, which was about
    the 'global' scope having no per-Use_Case row, not about the claim
    being a privilege source.

    REPOINTED (task 3.3): the original class docstring read "A user whose
    ONLY role source is the JWT custom:role claim must be authorized by
    rbac_check(..., allow_global=True) when that role holds the required
    permission (the live build-fleet 403)." — see the module docstring.
    """

    def test_jwt_portal_admin_authorized_for_builds_read(
            self, shared, middleware, registry, registry_mode):
        """A provisioned PortalAdmin -> builds:read at global scope must
        be AUTHORIZED (was 403 on unfixed code; must stay 200 under
        enforcement, since the account is in the registry).

        REPOINTED (task 3.3). SUPERSEDED body, recorded verbatim::

            \"\"\"custom:role=PortalAdmin, no UserRoles rows -> builds:read at
            global scope must be AUTHORIZED (was 403 on unfixed code).\"\"\"
            admin = make_user(role="PortalAdmin")

            decorated = middleware.rbac_check(
                [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
            response = decorated(api_event(admin), None)

            assert response["statusCode"] == 200, (
                "JWT PortalAdmin denied builds:read at global scope: "
                f"{response['body']}"
            )

        The assertion is unchanged; only the setup gained the registry row
        (Requirement 1.1: the claim alone grants nothing).
        """
        admin = make_user(role="PortalAdmin")
        provision(registry, admin)

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
        response = decorated(api_event(admin), None)

        assert response["statusCode"] == 200, (
            f"provisioned PortalAdmin denied builds:read at global scope "
            f"({registry_mode}): {response['body']}"
        )

    def test_jwt_data_scientist_authorized_for_builds_submit(
            self, shared, middleware, registry, registry_mode):
        """A provisioned DataScientist (a Build_Operator role) ->
        builds:submit at global scope must be AUTHORIZED.

        REPOINTED (task 3.3). SUPERSEDED body, recorded verbatim::

            \"\"\"custom:role=DataScientist (a Build_Operator role), no
            UserRoles rows -> builds:submit at global scope must be
            AUTHORIZED via the JWT-role fallback.\"\"\"
            scientist = make_user(role="DataScientist")

            decorated = middleware.rbac_check(
                [shared.Permission.BUILDS_SUBMIT], allow_global=True)(ok_handler)
            response = decorated(
                api_event(scientist, method="POST", resource="/builds"), None)

            assert response["statusCode"] == 200, (
                "JWT DataScientist denied builds:submit at global scope: "
                f"{response['body']}"
            )

        Requirement 7.2 in the same words: a registry entry naming a
        build-capable role is accepted, so the fix denies only
        unprovisioned principals.
        """
        scientist = make_user(role="DataScientist")
        provision(registry, scientist)

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_SUBMIT], allow_global=True)(ok_handler)
        response = decorated(
            api_event(scientist, method="POST", resource="/builds"), None)

        assert response["statusCode"] == 200, (
            f"provisioned DataScientist denied builds:submit at global "
            f"scope ({registry_mode}): {response['body']}"
        )

    def test_jwt_viewer_still_denied(self, shared, middleware, registry,
                                     registry_mode):
        """The fix must not over-grant: a Viewer (holds no build
        permissions) is still denied at global scope — now even with a
        registry row, so the denial is a permission decision and not the
        by-product of an absent entry.

        REPOINTED (task 3.3). SUPERSEDED body, recorded verbatim::

            \"\"\"The fix must not over-grant: a JWT Viewer (holds no build
            permissions) is still denied at global scope.\"\"\"
            viewer = make_user(role="Viewer")

            decorated = middleware.rbac_check(
                [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
            response = decorated(api_event(viewer), None)

            assert response["statusCode"] == 403
            body = json.loads(response["body"])
            assert body["error"] == "Insufficient permissions"
        """
        viewer = make_user(role="Viewer")
        provision(registry, viewer)

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
        response = decorated(api_event(viewer), None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "Insufficient permissions"


# ------------------------------------------------------ super_user_only

class TestSuperUserOnlyJwtRole:
    """super_user_only must honor a provisioned PortalAdmin (same
    user_info gap through rbac_manager.is_portal_admin).

    REPOINTED (task 3.3): the original class docstring read
    "super_user_only must honor the JWT PortalAdmin claim (same user_info
    gap through rbac_manager.is_portal_admin)."
    """

    def test_jwt_portal_admin_passes_super_user_only(
            self, middleware, registry, registry_mode):
        """REPOINTED (task 3.3). SUPERSEDED body, recorded verbatim::

            admin = make_user(role="PortalAdmin")

            decorated = middleware.super_user_only(ok_handler)
            response = decorated(
                api_event(admin, resource="/admin/settings"), None)

            assert response["statusCode"] == 200, (
                "JWT PortalAdmin denied by super_user_only: "
                f"{response['body']}"
            )
        """
        admin = make_user(role="PortalAdmin")
        provision(registry, admin)

        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            api_event(admin, resource="/admin/settings"), None)

        assert response["statusCode"] == 200, (
            f"provisioned PortalAdmin denied by super_user_only "
            f"({registry_mode}): {response['body']}"
        )

    def test_jwt_viewer_denied_by_super_user_only(
            self, middleware, registry, registry_mode):
        """REPOINTED (task 3.3). SUPERSEDED body, recorded verbatim::

            viewer = make_user(role="Viewer")

            decorated = middleware.super_user_only(ok_handler)
            response = decorated(
                api_event(viewer, resource="/admin/settings"), None)

            assert response["statusCode"] == 403
        """
        viewer = make_user(role="Viewer")
        provision(registry, viewer)

        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            api_event(viewer, resource="/admin/settings"), None)

        assert response["statusCode"] == 403


# ------------------------------------------- the mechanism, pinned directly

class _RecordingRbacManager:
    """Delegates every call to the real RBACManager, recording the
    (name, args, kwargs) of each, so a test can assert what the
    middleware passed rather than what the outcome happened to be."""

    def __init__(self, real, calls):
        self._real = real
        self._calls = calls

    def __getattr__(self, name):
        attribute = getattr(self._real, name)
        if not callable(attribute):
            return attribute

        def recorder(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return attribute(*args, **kwargs)

        return recorder


class TestUserInfoStillThreaded:
    """The literal regression the original suite was written for, pinned
    on the MECHANISM instead of on a claim deciding privilege.

    Added by the task 3.3 repoint: once the registry supplies the role,
    an outcome assertion can no longer distinguish "user_info reached
    rbac_manager" from "user_info was dropped again", so the fix's own
    mechanism — every rbac_manager call in rbac_check / super_user_only
    receives the dict get_user_from_event extracted — is asserted here.
    It holds in both flag modes and stays true after task 5.
    """

    def test_rbac_check_passes_user_info_to_every_rbac_manager_call(
            self, shared, middleware, registry, registry_mode, monkeypatch):
        admin = make_user(role="PortalAdmin")
        provision(registry, admin)

        calls = []
        monkeypatch.setattr(
            middleware, "rbac_manager",
            _RecordingRbacManager(shared.rbac_manager, calls))

        decorated = middleware.rbac_check(
            [shared.Permission.BUILDS_READ], allow_global=True)(ok_handler)
        response = decorated(api_event(admin), None)

        assert response["statusCode"] == 200, response["body"]
        assert [name for name, _, _ in calls].count("has_permission") == 1, (
            f"rbac_check did not consult has_permission: {calls}")
        for name, args, kwargs in calls:
            assert kwargs.get("user_info") is not None, (
                f"rbac_manager.{name} called without user_info "
                f"(the original build-fleet 403): args={args}")
            assert kwargs["user_info"]["user_id"] == admin["user_id"]
            assert kwargs["user_info"]["role"] == "PortalAdmin"
        # The global scope is what allow_global=True substitutes, and the
        # scope that had no per-Use_Case row in the live incident. (Only
        # the scope-taking calls: is_portal_admin takes the user alone.)
        scoped = [(name, args) for name, args, _ in calls
                  if name != "is_portal_admin"]
        assert scoped, calls
        assert all("global" in args for _, args in scoped), scoped

    def test_super_user_only_passes_user_info_to_is_portal_admin(
            self, shared, middleware, registry, registry_mode, monkeypatch):
        admin = make_user(role="PortalAdmin")
        provision(registry, admin)

        calls = []
        monkeypatch.setattr(
            middleware, "rbac_manager",
            _RecordingRbacManager(shared.rbac_manager, calls))

        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            api_event(admin, resource="/admin/settings"), None)

        assert response["statusCode"] == 200, response["body"]
        assert [name for name, _, _ in calls].count("is_portal_admin") >= 1, (
            f"super_user_only did not consult is_portal_admin: {calls}")
        for name, args, kwargs in calls:
            assert kwargs.get("user_info") is not None, (
                f"rbac_manager.{name} called without user_info: args={args}")
            assert kwargs["user_info"]["user_id"] == admin["user_id"]
