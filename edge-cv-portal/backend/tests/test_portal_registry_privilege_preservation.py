"""
Preservation suite — portal-jwt-role-privilege-escalation task 1.2.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

**THESE TESTS PASS ON THE UNFIXED TREE AND MUST KEEP PASSING AFTER THE
FIX.** They are the immutable oracle for everything the registry fix must
NOT move (bugfix.md "Unchanged Behavior"), and are the counterweight to
`test_portal_registry_privilege_exploration.py` (which asserts the fixed
behaviour and fails today): together they say "deny the unprovisioned
principal, change nothing else".

Every case is evaluated in the **post-backfill state** the fix ships with
— a `dda-portal-user-roles` global row naming the account's role
(Requirement 2.1, design.md Decision 5) plus the matching `custom:role`
claim — because that is the state in which "unchanged" is a meaningful
claim. Under today's resolution order the claim decides; under
enforcement the row decides; with the two in agreement the outcome must
be identical, and that identity is exactly what the backfill buys
(Requirement 2.3).

What is pinned:

1. **The pool's 6 real accounts** (`admin` PortalAdmin bootstrap,
   `demoadmin`, `demoViewer`, `ryan-labeler`, `ryan-labeler2`,
   `demoDataScientist` — bugfix.md records the pool holds 6 users) keep
   their resolved role, their `is_portal_admin` answer, their permission
   set, and their outcome on four representative real route guards
   (`require_builds_submit`, `workflow:read` at Use_Case scope,
   `labeling:tasks-self`, `super_user_only`).
2. **Per-Use_Case precedence over the global role** (Requirement 1.3,
   today's step-3 precedence), including that it does not leak to the
   `global` scope or to another Use_Case, and `get_accessible_usecases`.
3. **The response envelopes, byte-identical**: the 403
   `{'error': 'Insufficient permissions', 'required_permissions': [...],
   'usecase_id': ...}`, the `super_user_only` 403, the 400 missing-scope
   envelope, and the 500 `{'error': 'Authorization check failed'}` the
   fix reuses for `RegistryUnavailable` (design.md Decision 3) — plus
   the `unauthorized_access` / `unauthorized_super_user_access` audit
   actions and the default response headers.
4. **The audit-log schema, backward compatible**: `event_id` shape
   `f"{user_id}_{timestamp}"` for `log_audit_event` and
   `f"{user_id}_{timestamp}_{8 hex}"` for the strict helper, the nine
   legacy keys, the 90-day `ttl`, the `password`/`verifier`/`hash`/
   `temp*` denylist, and the finalize merge (design.md Decision 7).
5. **Both deployed audit GSIs still resolve**: `user-actions-index`
   (`user_id` + `timestamp`) and `usecase-actions-index` (`usecase_id` +
   `timestamp`), against a suite-local table carrying the
   `storage-stack.ts` index definitions (the conftest audit table has
   none).

Deliberately NOT pinned, because design.md changes them on purpose and a
preservation test asserting today's answer would be a false alarm at
task 2.1:

* **global `PortalAdmin` row + a downgrading Use_Case row.** Today the
  PortalAdmin short-circuit (step 1/2) wins, so the Use_Case row is never
  read; design.md's Expected Behavior makes the Use_Case row override it.
  Precedence is therefore pinned only for non-PortalAdmin global roles —
  which is the only shape Team Management actually produces and the only
  shape today's code can express.
* **a Use_Case row with no global row.** Requirement 1.1 ("no
  Portal_Identity registry entry" → deny) and the Glossary's
  Unprovisioned_Principal ("no enabled *global* Portal_Identity") read
  differently here; task 2.1 settles it. Every precedence case below
  seeds both rows, which is unambiguous under either reading.
* **an unprovisioned principal's outcome** — that is the defect, owned by
  the exploration suite.

Account emails are `<username>@example.com` placeholders; only the
usernames and roles are load-bearing. `demoadmin`'s role is not recorded
anywhere in-repo, so it is pinned for BOTH candidate readings
(`UseCaseAdmin` and `PortalAdmin`) rather than guessed.

_Requirements: 2.3, 1.3, and bugfix.md "Unchanged Behavior"_
"""
import json
import re
import sys
import uuid

import pytest
from boto3.dynamodb.conditions import Key
from dynamo_helpers import all_table_names

REGION = "us-east-1"

# Suite-local audit table carrying the two GSIs storage-stack.ts defines
# on dda-portal-audit-log (the conftest test-audit-log table has none).
GSI_AUDIT_TABLE = "test-registry-preservation-audit-gsi"
USER_ACTIONS_INDEX = "user-actions-index"
USECASE_ACTIONS_INDEX = "usecase-actions-index"

# The audit-log keys that existed before this spec and must keep their
# meaning (bugfix.md "Unchanged Behavior": new identity fields are
# additive).
LEGACY_AUDIT_KEYS = ("event_id", "timestamp", "user_id", "action",
                     "resource_type", "resource_id", "result", "details",
                     "ttl")

NINETY_DAYS_MS = 90 * 24 * 60 * 60 * 1000

# create_response's default headers (shared_utils.create_response).
DEFAULT_RESPONSE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
}


# ---------------------------------------------------------------------------
# The pool's 6 real accounts (bugfix.md Incident Record: "The pool holds 6
# users"). Each entry is (username, role, expected outcomes) where the
# outcomes are literal, hand-written expectations — not recomputed from the
# permission matrix — so a change to the matrix is caught here too.
#
# Route guards exercised (the real decorators from rbac_middleware):
#   builds_submit  -> require_builds_submit()      (global scope)
#   workflow_read  -> rbac_check([WORKFLOW_READ])  (Use_Case scope)
#   labeling_self  -> rbac_check([LABELING_TASKS_SELF], allow_global=True)
#   super_user     -> super_user_only              (/admin/users)
# ---------------------------------------------------------------------------

ACCOUNT_OUTCOMES = {
    "PortalAdmin": {"is_portal_admin": True, "builds_submit": 200,
                    "workflow_read": 200, "labeling_self": 200,
                    "super_user": 200},
    "UseCaseAdmin": {"is_portal_admin": False, "builds_submit": 200,
                     "workflow_read": 200, "labeling_self": 200,
                     "super_user": 403},
    "DataScientist": {"is_portal_admin": False, "builds_submit": 200,
                      "workflow_read": 200, "labeling_self": 200,
                      "super_user": 403},
    "Viewer": {"is_portal_admin": False, "builds_submit": 403,
               "workflow_read": 200, "labeling_self": 403,
               "super_user": 403},
    "DataLabeler": {"is_portal_admin": False, "builds_submit": 403,
                    "workflow_read": 403, "labeling_self": 200,
                    "super_user": 403},
}

# (username, role, has_email_claim). The bootstrap `admin` was created
# without an email attribute, so its ID token carries no `email` claim
# (shared_utils._display_identity documents this exact account) — its
# authorization must not depend on that.
POOL_ACCOUNTS = (
    ("admin", "PortalAdmin", False),
    ("demoadmin", "UseCaseAdmin", True),      # role not recorded in-repo:
    ("demoadmin", "PortalAdmin", True),       # pinned for both readings
    ("demoViewer", "Viewer", True),
    ("demoDataScientist", "DataScientist", True),
    ("ryan-labeler", "DataLabeler", True),
    ("ryan-labeler2", "DataLabeler", True),
)


# --------------------------------------------------------------- fixtures

@pytest.fixture
def shared(aws_stack):
    """The real shared_utils module imported inside the moto mock."""
    import shared_utils
    return shared_utils


@pytest.fixture
def middleware(aws_stack):
    """The real rbac_middleware, re-imported inside the moto mock so it
    binds the same shared_utils (Permission enum / rbac_manager
    singleton) the conftest stack was built with."""
    sys.modules.pop("rbac_middleware", None)
    import rbac_middleware
    return rbac_middleware


@pytest.fixture
def registry(aws_stack):
    """The Portal_Identity registry table (dda-portal-user-roles)."""
    return aws_stack.tables.user_roles


@pytest.fixture
def audit(aws_stack):
    """The moto-backed audit log table the helpers write to."""
    return aws_stack.tables.audit_log


@pytest.fixture(scope="module")
def gsi_audit_table(aws_stack):
    """A suite-local audit table with the two deployed GSIs
    (storage-stack.ts:149-171), so the projections can be queried."""
    import boto3

    ddb = boto3.client("dynamodb", region_name=REGION)
    if GSI_AUDIT_TABLE not in all_table_names(ddb):
        ddb.create_table(
            TableName=GSI_AUDIT_TABLE,
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "timestamp", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "N"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {"IndexName": USER_ACTIONS_INDEX,
                 "KeySchema": [
                     {"AttributeName": "user_id", "KeyType": "HASH"},
                     {"AttributeName": "timestamp", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
                {"IndexName": USECASE_ACTIONS_INDEX,
                 "KeySchema": [
                     {"AttributeName": "usecase_id", "KeyType": "HASH"},
                     {"AttributeName": "timestamp", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    return boto3.resource("dynamodb", region_name=REGION).Table(
        GSI_AUDIT_TABLE)


# ---------------------------------------------------------------- helpers

def new_sub():
    """A fresh Cognito `sub`, so cases never collide in the shared
    session-scoped tables."""
    return str(uuid.uuid4())


def claims(sub, role, username, email=None):
    """Authorizer claims exactly as the Cognito User Pools authorizer
    forwards them. `email=None` omits the claim (the bootstrap admin)."""
    payload = {"sub": sub, "cognito:username": username,
               "custom:role": role}
    if email is not None:
        payload["email"] = email
    return payload


def event(sub, role, username, email=None, method="GET", resource="/builds",
          usecase_id=None, body=None):
    request_context = {"authorizer": {"claims": claims(sub, role, username,
                                                       email)}}
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "pathParameters": {"usecase_id": usecase_id} if usecase_id else None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": request_context,
    }


def ok_handler(event, context):
    return {"statusCode": 200, "body": json.dumps({"ok": True})}


def provision(registry, sub, role, usecase_id="global", username="u",
              email="u@example.com", status="enabled"):
    """Write the post-backfill Portal_Identity row: `role` plus the three
    attributes Decision 1 adds (`username`, `email`, `status`)."""
    registry.put_item(Item={
        "user_id": sub, "usecase_id": usecase_id, "role": role,
        "username": username, "email": email, "status": status,
        "assigned_by": "backfill", "assigned_at": 1,
    })


def user_info(sub, role, username, email="u@example.com"):
    """What get_user_from_event returns for these claims."""
    return {"user_id": sub, "email": email, "username": username,
            "role": role}


def audit_rows(audit, sub, action):
    return [item for item in audit.scan().get("Items", [])
            if item.get("user_id") == sub and item.get("action") == action]


def only(rows, what):
    assert len(rows) == 1, f"expected exactly one {what}, got {rows}"
    return rows[0]


# ---------------------------------------------------------------------------
# 1. The pool's 6 real accounts keep their authorization outcomes
#    (Requirement 2.3)
# ---------------------------------------------------------------------------

class TestSixRealAccountShapes:
    """Every account that works today must still work once the registry
    row exists — the backfill's whole purpose (Requirement 2.3, bugfix.md
    "Unchanged Behavior": "Every currently working portal user keeps
    exactly the access they have today")."""

    @staticmethod
    def _seed(registry, username, role, has_email):
        sub = new_sub()
        email = f"{username}@example.com"
        provision(registry, sub, role, username=username, email=email)
        return sub, (email if has_email else None), email

    @pytest.mark.parametrize("username,role,has_email", POOL_ACCOUNTS)
    def test_resolved_role_and_permissions(self, shared, registry, username,
                                           role, has_email):
        """The account resolves to its own role at both the global and a
        Use_Case scope, and carries exactly that role's permissions."""
        sub, claim_email, email = self._seed(registry, username, role,
                                             has_email)
        info = user_info(sub, role, username, email)
        expected_role = shared.Role(role)

        for scope in ("global", f"uc-{uuid.uuid4()}"):
            resolved = shared.rbac_manager.get_user_role(sub, scope,
                                                         user_info=info)
            assert resolved == expected_role, (
                f"{username} resolved {resolved} at scope {scope!r}, "
                f"expected {expected_role}")

        assert (shared.rbac_manager.is_portal_admin(sub, user_info=info)
                is ACCOUNT_OUTCOMES[role]["is_portal_admin"]), username
        assert shared.rbac_manager.get_user_permissions(
            sub, "global", user_info=info) == \
            shared.rbac_manager.role_permissions[expected_role], username

    @pytest.mark.parametrize("username,role,has_email", POOL_ACCOUNTS)
    def test_route_outcomes(self, shared, middleware, registry, username,
                            role, has_email):
        """The four representative real route guards answer exactly what
        they answer today for this account."""
        sub, claim_email, email = self._seed(registry, username, role,
                                             has_email)
        expected = ACCOUNT_OUTCOMES[role]
        usecase_id = f"uc-{uuid.uuid4()}"

        guards = {
            "builds_submit": (
                middleware.require_builds_submit()(ok_handler),
                event(sub, role, username, claim_email, method="POST",
                      resource="/builds")),
            "workflow_read": (
                middleware.rbac_check([shared.Permission.WORKFLOW_READ])(
                    ok_handler),
                event(sub, role, username, claim_email,
                      resource="/usecases/{usecase_id}/workflows",
                      usecase_id=usecase_id)),
            "labeling_self": (
                middleware.rbac_check(
                    [shared.Permission.LABELING_TASKS_SELF],
                    allow_global=True)(ok_handler),
                event(sub, role, username, claim_email,
                      resource="/labeling/my-tasks")),
            "super_user": (
                middleware.super_user_only(ok_handler),
                event(sub, role, username, claim_email,
                      resource="/admin/users")),
        }

        for name, (decorated, request) in guards.items():
            response = decorated(request, None)
            assert response["statusCode"] == expected[name], (
                f"{username} ({role}) got {response['statusCode']} from "
                f"{name}, expected {expected[name]}: {response['body']}")


# ---------------------------------------------------------------------------
# 2. Per-Use_Case precedence over the global role (Requirement 1.3)
# ---------------------------------------------------------------------------

class TestUseCasePrecedence:
    """Team Management's per-Use_Case grant keeps overriding the
    account's global role for that Use_Case, and keeps NOT applying
    anywhere else (bugfix.md "Unchanged Behavior": "Per-Use_Case role
    assignment via Team Management keeps working, including the
    precedence of a Use_Case row over the account's global role").

    Both rows are always seeded, and the global role is never
    PortalAdmin — see the module docstring for why those two shapes are
    out of scope here.
    """

    @pytest.mark.parametrize("global_role,usecase_role", [
        ("Viewer", "UseCaseAdmin"),
        ("Viewer", "DataScientist"),
        ("Viewer", "Operator"),
        ("DataScientist", "Viewer"),
        ("DataLabeler", "Viewer"),
    ])
    def test_usecase_row_overrides_global_row(self, shared, registry,
                                              global_role, usecase_role):
        sub = new_sub()
        usecase_id = f"uc-{uuid.uuid4()}"
        provision(registry, sub, global_role, username="team-member")
        provision(registry, sub, usecase_role, usecase_id=usecase_id,
                  username="team-member")
        info = user_info(sub, global_role, "team-member")

        assert shared.rbac_manager.get_user_role(
            sub, usecase_id, user_info=info) == shared.Role(usecase_role), (
            f"the Use_Case row ({usecase_role}) must decide inside its own "
            f"Use_Case, not the global row ({global_role})")

    @pytest.mark.parametrize("global_role,usecase_role", [
        ("Viewer", "UseCaseAdmin"),
        ("DataScientist", "Viewer"),
    ])
    def test_usecase_row_does_not_leak(self, shared, registry, global_role,
                                       usecase_role):
        """The Use_Case row applies to its own Use_Case only: the global
        scope and any other Use_Case keep resolving the global role."""
        sub = new_sub()
        granted = f"uc-{uuid.uuid4()}"
        other = f"uc-{uuid.uuid4()}"
        provision(registry, sub, global_role, username="team-member")
        provision(registry, sub, usecase_role, usecase_id=granted,
                  username="team-member")
        info = user_info(sub, global_role, "team-member")

        for scope in ("global", other):
            assert shared.rbac_manager.get_user_role(
                sub, scope, user_info=info) == shared.Role(global_role), (
                f"the {granted} row leaked into scope {scope!r}")

    def test_usecase_grant_reaches_the_route(self, shared, middleware,
                                            registry):
        """The precedence is visible at the route: a global Viewer with a
        UseCaseAdmin grant may edit workflows inside that Use_Case and
        may not in another."""
        sub = new_sub()
        granted = f"uc-{uuid.uuid4()}"
        other = f"uc-{uuid.uuid4()}"
        provision(registry, sub, "Viewer", username="team-member")
        provision(registry, sub, "UseCaseAdmin", usecase_id=granted,
                  username="team-member")

        decorated = middleware.require_workflow_edit()(ok_handler)
        allowed = decorated(
            event(sub, "Viewer", "team-member", "team-member@example.com",
                  method="POST", resource="/usecases/{usecase_id}/workflows",
                  usecase_id=granted), None)
        denied = decorated(
            event(sub, "Viewer", "team-member", "team-member@example.com",
                  method="POST", resource="/usecases/{usecase_id}/workflows",
                  usecase_id=other), None)

        assert allowed["statusCode"] == 200, allowed["body"]
        assert denied["statusCode"] == 403, denied["body"]

    def test_accessible_usecases_lists_the_granted_usecase(self, shared,
                                                          registry):
        """get_accessible_usecases keeps returning the Use_Case rows (and
        never the 'global' row) for a non-admin account."""
        sub = new_sub()
        granted = f"uc-{uuid.uuid4()}"
        provision(registry, sub, "Viewer", username="team-member")
        provision(registry, sub, "DataScientist", usecase_id=granted,
                  username="team-member")
        info = user_info(sub, "Viewer", "team-member")

        assert shared.rbac_manager.get_accessible_usecases(
            sub, user_info=info) == [granted]

    def test_accessible_usecases_empty_without_a_usecase_row(self, shared,
                                                            registry):
        sub = new_sub()
        provision(registry, sub, "Viewer", username="lonely")
        info = user_info(sub, "Viewer", "lonely")

        assert shared.rbac_manager.get_accessible_usecases(
            sub, user_info=info) == []


# ---------------------------------------------------------------------------
# 3. The authorization response envelopes, byte-identical
#    (bugfix.md "Unchanged Behavior"; design.md Decision 3)
# ---------------------------------------------------------------------------

class TestAuthorizationEnvelopes:
    """`rbac_check` / `super_user_only` keep their exact response bodies,
    status codes, headers, and audit actions. The 403 body is compared as
    a STRING so key order and spacing are pinned too — the portal
    frontend and every existing test read these envelopes."""

    def test_403_body_is_byte_identical(self, shared, middleware, registry):
        """A provisioned Viewer denied builds:submit at global scope."""
        sub = new_sub()
        provision(registry, sub, "Viewer", username="demoViewer")

        decorated = middleware.require_builds_submit()(ok_handler)
        response = decorated(
            event(sub, "Viewer", "demoViewer", "demoViewer@example.com",
                  method="POST", resource="/builds"), None)

        assert response["statusCode"] == 403
        assert response["body"] == (
            '{"error": "Insufficient permissions", '
            '"required_permissions": ["builds:submit"], '
            '"usecase_id": "global"}')
        assert response["headers"] == DEFAULT_RESPONSE_HEADERS

    def test_403_body_carries_every_required_permission_in_order(
            self, shared, middleware, registry):
        """A multi-permission guard lists them in the declared order."""
        sub = new_sub()
        provision(registry, sub, "Viewer", username="demoViewer")
        usecase_id = "uc-envelope-order"

        decorated = middleware.require_workflow_edit()(ok_handler)
        response = decorated(
            event(sub, "Viewer", "demoViewer", "demoViewer@example.com",
                  method="POST", resource="/usecases/{usecase_id}/workflows",
                  usecase_id=usecase_id), None)

        assert response["statusCode"] == 403
        assert response["body"] == (
            '{"error": "Insufficient permissions", "required_permissions": '
            '["workflow:create", "workflow:edit", "workflow:save", '
            '"workflow:delete"], "usecase_id": "uc-envelope-order"}')

    def test_denial_is_audited_as_unauthorized_access(self, shared,
                                                     middleware, registry,
                                                     audit):
        """The `unauthorized_access` action and its details keys stay put
        (only additive identity fields are allowed on top)."""
        sub = new_sub()
        provision(registry, sub, "Viewer", username="demoViewer")

        decorated = middleware.require_builds_submit()(ok_handler)
        response = decorated(
            event(sub, "Viewer", "demoViewer", "demoViewer@example.com",
                  method="POST", resource="/builds"), None)
        assert response["statusCode"] == 403

        entry = only(audit_rows(audit, sub, "unauthorized_access"),
                     "unauthorized_access audit entry")
        assert entry["result"] == "denied"
        assert entry["resource_type"] == "api_endpoint"
        assert entry["resource_id"] == "/builds"
        details = entry["details"]
        assert details["required_permissions"] == ["builds:submit"]
        assert details["usecase_id"] == "global"
        assert details["user_role"] == "Viewer"
        assert details["method"] == "POST"
        assert details["path"] == "/builds"

    def test_super_user_only_403_envelope_and_audit(self, shared, middleware,
                                                   registry, audit):
        sub = new_sub()
        provision(registry, sub, "UseCaseAdmin", username="demoadmin")

        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            event(sub, "UseCaseAdmin", "demoadmin", "demoadmin@example.com",
                  resource="/admin/users"), None)

        assert response["statusCode"] == 403
        assert response["body"] == (
            '{"error": "Super user access required", '
            '"required_role": "PortalAdmin"}')
        assert response["headers"] == DEFAULT_RESPONSE_HEADERS

        entry = only(
            audit_rows(audit, sub, "unauthorized_super_user_access"),
            "unauthorized_super_user_access audit entry")
        assert entry["result"] == "denied"
        assert entry["details"]["user_role"] == "UseCaseAdmin"

    def test_missing_usecase_scope_400_envelope(self, shared, middleware,
                                               registry):
        """A Use_Case-scoped guard with no resolvable Use_Case keeps
        answering 400, not 403 — the scope check runs first."""
        sub = new_sub()
        provision(registry, sub, "DataScientist", username="demoDataScientist")

        decorated = middleware.rbac_check(
            [shared.Permission.WORKFLOW_READ])(ok_handler)
        response = decorated(
            event(sub, "DataScientist", "demoDataScientist",
                  "demoDataScientist@example.com", resource="/workflows"),
            None)

        assert response["statusCode"] == 400
        assert response["body"] == (
            '{"error": "Use case ID required", "parameter": "usecase_id"}')

    def test_authorization_failure_500_envelope(self, shared, middleware,
                                               registry, monkeypatch):
        """The envelope the fix reuses for `RegistryUnavailable`
        (design.md Decision 3): a raising authorization path answers 500
        `{'error': 'Authorization check failed'}` — never 403, never a
        silent Viewer downgrade."""
        sub = new_sub()
        provision(registry, sub, "PortalAdmin", username="admin")

        def exploding(*args, **kwargs):
            raise RuntimeError("registry lookup failed")

        monkeypatch.setattr(shared.rbac_manager, "has_permission", exploding)
        decorated = middleware.require_builds_submit()(ok_handler)
        response = decorated(
            event(sub, "PortalAdmin", "admin", None, method="POST",
                  resource="/builds"), None)

        assert response["statusCode"] == 500
        assert response["body"] == '{"error": "Authorization check failed"}'
        assert response["headers"] == DEFAULT_RESPONSE_HEADERS

    def test_super_user_failure_500_envelope(self, shared, middleware,
                                             monkeypatch):
        def exploding(*args, **kwargs):
            raise RuntimeError("registry lookup failed")

        monkeypatch.setattr(shared.rbac_manager, "is_portal_admin", exploding)
        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            event(new_sub(), "PortalAdmin", "admin", None,
                  resource="/admin/users"), None)

        assert response["statusCode"] == 500
        assert response["body"] == '{"error": "Authorization check failed"}'

    def test_authorized_request_reaches_the_handler_unwrapped(
            self, shared, middleware, registry):
        """An authorized call still runs the handler outside the
        authorization try/except: a handler error must NOT be reported as
        'Authorization check failed'."""
        sub = new_sub()
        provision(registry, sub, "DataScientist", username="demoDataScientist")

        def exploding_handler(event, context):
            raise RuntimeError("handler bug")

        decorated = middleware.require_builds_submit()(exploding_handler)
        with pytest.raises(RuntimeError, match="handler bug"):
            decorated(
                event(sub, "DataScientist", "demoDataScientist",
                      "demoDataScientist@example.com", method="POST",
                      resource="/builds"), None)

    def test_rbac_context_keys_unchanged(self, shared, middleware, registry):
        """The decorator's `rbac_context` contract (handlers read these
        four keys) stays as-is."""
        sub = new_sub()
        provision(registry, sub, "DataScientist", username="demoDataScientist")
        captured = {}

        def capturing_handler(request, context):
            captured.update(middleware.get_rbac_context(request))
            return ok_handler(request, context)

        decorated = middleware.require_builds_submit()(capturing_handler)
        response = decorated(
            event(sub, "DataScientist", "demoDataScientist",
                  "demoDataScientist@example.com", method="POST",
                  resource="/builds"), None)

        assert response["statusCode"] == 200
        assert captured["user_id"] == sub
        assert captured["usecase_id"] == "global"
        assert captured["user_role"] == shared.Role.DATA_SCIENTIST
        assert captured["is_super_user"] is False
        assert shared.Permission.BUILDS_SUBMIT in captured["permissions"]


# ---------------------------------------------------------------------------
# 4. The audit-log schema stays backward compatible (design.md Decision 7)
# ---------------------------------------------------------------------------

class TestAuditSchemaBackwardCompatible:
    """`event_id` keeps its shape (it is the partition key of both the
    table and the readers' expectations), the nine legacy keys keep their
    meaning, the 90-day ttl stays, and the details denylist keeps
    applying. New identity fields are additive only."""

    def test_log_audit_event_id_shape(self, shared, audit):
        sub = new_sub()
        shared.log_audit_event(
            user_id=sub, action="build_requested", resource_type="build_job",
            resource_id="job-1", result="success", details={"target": "JP7"})

        entry = only(audit_rows(audit, sub, "build_requested"),
                     "build_requested audit entry")
        assert entry["event_id"] == f"{sub}_{int(entry['timestamp'])}", (
            "log_audit_event's event_id must stay f'{user_id}_{timestamp}' "
            "(design.md Decision 7: the two GSIs and existing readers "
            "depend on it)")
        for key in LEGACY_AUDIT_KEYS:
            assert key in entry, f"legacy audit key {key} disappeared"
        assert entry["user_id"] == sub
        assert entry["resource_type"] == "build_job"
        assert entry["resource_id"] == "job-1"
        assert entry["result"] == "success"
        assert entry["details"] == {"target": "JP7"}
        assert int(entry["ttl"]) == int(entry["timestamp"]) + NINETY_DAYS_MS

    def test_strict_helper_event_id_shape(self, shared, audit):
        sub = new_sub()
        event_id = shared.record_audit_event_strict(
            user_id=sub, action="account_create",
            resource_type=shared.USER_ACCOUNT_RESOURCE_TYPE,
            resource_id="demoViewer")

        entry = only(audit_rows(audit, sub, "account_create"),
                     "account_create audit entry")
        assert entry["event_id"] == event_id
        assert re.fullmatch(
            rf"{re.escape(sub)}_{int(entry['timestamp'])}_[0-9a-f]{{8}}",
            event_id), (
            "record_audit_event_strict's event_id must stay "
            "f'{user_id}_{timestamp}_{8 hex}'")
        assert entry["result"] == shared.AUDIT_RESULT_PENDING
        for key in LEGACY_AUDIT_KEYS:
            assert key in entry, f"legacy audit key {key} disappeared"

    def test_log_audit_event_never_raises(self, shared, monkeypatch):
        """log_audit_event keeps swallowing its own failures: audit loss
        must not fail a request (bugfix.md Current Behavior)."""
        monkeypatch.setattr(shared, "AUDIT_LOG_TABLE", "does-not-exist")
        shared.log_audit_event(new_sub(), "build_requested", "build_job",
                               "job-1", "success")

    def test_details_denylist_still_applied(self, shared, audit):
        """`password` / `verifier` / `hash` / `temp*` keep being redacted
        from details, recursively (bugfix.md Requirement 4.6)."""
        sub = new_sub()
        event_id = shared.record_audit_event_strict(
            user_id=sub, action="password_change",
            resource_type=shared.USER_ACCOUNT_RESOURCE_TYPE,
            resource_id="demoViewer",
            details={"password": "s3cret", "password_hash": "abc",
                     "credential_verifier": "v", "temp_password": "t",
                     "temporaryPassword": "t", "kept": "yes",
                     "nested": {"tempPass": "t", "reason": "ok"}})
        shared.finalize_audit_event(event_id, "success",
                                    details={"new_password": "s3cret",
                                             "outcome": "done"})

        entry = only(audit_rows(audit, sub, "password_change"),
                     "password_change audit entry")
        assert entry["result"] == "success"
        assert "completed_at" in entry
        assert entry["details"] == {"kept": "yes", "outcome": "done",
                                    "nested": {"reason": "ok"}}
        assert "s3cret" not in json.dumps(entry, default=str)

    def test_sanitize_audit_details_contract(self, shared):
        assert shared.sanitize_audit_details(None) == {}
        assert shared.sanitize_audit_details(
            {"password": 1, "hash": 2, "verifier": 3, "temp": 4,
             "tempPassword": 5, "ok": 6}) == {"ok": 6}


# ---------------------------------------------------------------------------
# 5. Both deployed audit GSIs still resolve (bugfix.md "Unchanged
#    Behavior": "the two GSIs keep working")
# ---------------------------------------------------------------------------

class TestAuditGsiProjections:
    """The audit table's two GSIs (`user-actions-index`,
    `usecase-actions-index`, projection ALL) must keep resolving the
    items the audit helpers write. Both are exercised against a
    suite-local table carrying the storage-stack.ts definitions, with
    shared_utils pointed at it."""

    @staticmethod
    def _query(table, index, key, value):
        return table.query(IndexName=index,
                           KeyConditionExpression=Key(key).eq(value)
                           )["Items"]

    def test_user_actions_index_resolves_both_writers(self, shared,
                                                     gsi_audit_table,
                                                     monkeypatch):
        monkeypatch.setattr(shared, "AUDIT_LOG_TABLE", GSI_AUDIT_TABLE)
        sub = new_sub()

        shared.log_audit_event(sub, "build_requested", "build_job", "job-1",
                               "success", {"target": "JP7"})
        strict_id = shared.record_audit_event_strict(
            sub, "account_create", shared.USER_ACCOUNT_RESOURCE_TYPE, "u")

        items = self._query(gsi_audit_table, USER_ACTIONS_INDEX, "user_id",
                            sub)
        assert len(items) == 2, (
            f"user-actions-index did not resolve both entries: {items}")
        # Projection ALL: every attribute of the item comes back.
        by_action = {item["action"]: item for item in items}
        assert set(by_action) == {"build_requested", "account_create"}
        assert by_action["account_create"]["event_id"] == strict_id
        for item in items:
            for key in LEGACY_AUDIT_KEYS:
                assert key in item, (
                    f"user-actions-index projection lost {key}: {item}")
        assert by_action["build_requested"]["details"] == {"target": "JP7"}

    def test_usecase_actions_index_resolves(self, shared, gsi_audit_table,
                                           monkeypatch):
        """The Use_Case index keeps working for the entries that carry a
        top-level `usecase_id`, and keeps returning nothing for the
        helper-written entries that do not (today's helpers put the
        Use_Case in `details`). Pinned so a future top-level identity
        field can never be written under a GSI key attribute with the
        wrong type — that would fail the write outright."""
        monkeypatch.setattr(shared, "AUDIT_LOG_TABLE", GSI_AUDIT_TABLE)
        sub = new_sub()
        usecase_id = f"uc-{uuid.uuid4()}"

        shared.log_audit_event(sub, "unauthorized_access", "api_endpoint",
                               "/builds", "denied",
                               {"usecase_id": usecase_id})
        # An entry that does carry the top-level key (the shape the index
        # exists for).
        timestamp = 1_700_000_000_000
        gsi_audit_table.put_item(Item={
            "event_id": f"{sub}_{timestamp}", "timestamp": timestamp,
            "user_id": sub, "usecase_id": usecase_id,
            "action": "usecase_scoped_action", "resource_type": "usecase",
            "resource_id": usecase_id, "result": "success", "details": {},
            "ttl": timestamp + NINETY_DAYS_MS,
        })

        items = self._query(gsi_audit_table, USECASE_ACTIONS_INDEX,
                            "usecase_id", usecase_id)
        assert [item["action"] for item in items] == \
            ["usecase_scoped_action"], (
            "usecase-actions-index must resolve exactly the entries "
            f"carrying a top-level usecase_id: {items}")
        assert items[0]["user_id"] == sub
        # The helper-written denial still resolves through the user index.
        actions = {item["action"] for item in
                   self._query(gsi_audit_table, USER_ACTIONS_INDEX,
                               "user_id", sub)}
        assert "unauthorized_access" in actions
