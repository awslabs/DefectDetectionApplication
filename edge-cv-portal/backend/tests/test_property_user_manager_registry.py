"""
Property test for the User Manager as the registry's writer —
portal-jwt-role-privilege-escalation task 2.4.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

**Feature: portal-jwt-role-privilege-escalation, Property 6: The User
Manager keeps Cognito and the registry in step.**

_For any_ generated sequence of create / role-change / disable / enable /
delete through the User Manager routes, the registry ends consistent with
Cognito, and after a delete or disable the principal is denied.

_Validates: Requirements 3.1, 3.3, 3.4_

Why this is the property that matters: the registry is the source of
portal privilege after the fix, and the User Manager is its only writer
(design.md Decision 1). If the two drift, either an account the
administrator created cannot use the portal (Cognito ahead of the
registry) or an account they deleted or disabled still can (registry
ahead of Cognito) — the second is the incident's own failure mode, one
step removed: Cognito's `AdminDisableUser` / `AdminDeleteUser` only stops
NEW sign-ins, so it is the registry row that has to stop an
already-minted token on its next request (Requirement 3.4, design.md
Decision 6).

Everything is real except the Cognito API: `user_admin.handler` routes the
five real endpoints, the registry writes go to the moto-backed
`test-user-roles` table, and the denial half of the property is evaluated
through the real `RBACManager` under enforcement. `FakePool` is a
stateful in-memory user pool that behaves like `cognito-idp` for the six
admin calls this module makes (including reporting each account's `sub`,
which is the Portal_Identity key) — it is the state the registry is
compared against, so a fake that drifted would be caught as an
inconsistency, not hidden by one.

An enabled `PortalAdmin` "keeper" account is seeded in both the pool and
the registry and never targeted, so the last-PortalAdmin guard has
something to count and the generated sequences are not silently rejected
(the guard's own arithmetic is task 2.5's unit-test territory).

Run from `edge-cv-portal/backend` with
`~/.venvs/dda-portal-tests/bin/python -m pytest <this file> -q
-p no:cacheprovider`.
"""
import json
import os
import sys
import uuid

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from dynamo_helpers import all_table_names

REGION = "us-east-1"
EDGE_CREDENTIALS_TABLE = "test-user-manager-registry-credentials"
POOL_ID = "us-east-1_testpool"

# The roles the User Manager accepts (user_admin.PORTAL_ROLES).
PORTAL_ROLES = ("PortalAdmin", "UseCaseAdmin", "DataScientist", "Operator",
                "Viewer", "DataLabeler")

# The account names a generated sequence operates on: a small set, so
# sequences repeatedly hit the same account (create-after-delete,
# double create, role change on a deleted account, ...).
TARGET_USERNAMES = ("acct-alpha", "acct-beta", "acct-gamma")

OPERATIONS = ("create", "role_change", "disable", "enable", "delete")

KEEPER_USERNAME = "keeper-admin"


def operations():
    """A generated User Manager operation: (kind, username, role)."""
    return st.tuples(
        st.sampled_from(OPERATIONS),
        st.sampled_from(TARGET_USERNAMES),
        st.sampled_from(PORTAL_ROLES),
    )


def sequences():
    return st.lists(operations(), min_size=1, max_size=6)


# ------------------------------------------------------------- fake pool

def cognito_error(code, operation):
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} (fake pool)"}},
        operation)


class FakePool:
    """An in-memory Cognito user pool for the six admin calls
    `user_admin` makes. Every created account reports a `sub`, which is
    what the Portal_Identity row is keyed on."""

    def __init__(self):
        # username -> {"sub", "enabled", "attrs"}
        self.users = {}
        # every (username, sub) ever issued, so rows left behind by a
        # deleted account can be detected.
        self.history = []

    # ------------------------------------------------------------ reads
    def _record(self, username):
        if username not in self.users:
            raise cognito_error("UserNotFoundException", "AdminGetUser")
        return self.users[username]

    def _attribute_list(self, username):
        record = self.users[username]
        attributes = [{"Name": name, "Value": value}
                      for name, value in record["attrs"].items()]
        attributes.append({"Name": "sub", "Value": record["sub"]})
        return attributes

    def admin_get_user(self, UserPoolId, Username):
        record = self._record(Username)
        return {"Username": Username, "Enabled": record["enabled"],
                "UserStatus": "CONFIRMED",
                "UserAttributes": self._attribute_list(Username)}

    def list_users(self, UserPoolId, Limit=None, PaginationToken=None):
        users = [{"Username": username, "Enabled": record["enabled"],
                  "UserStatus": "CONFIRMED",
                  "Attributes": self._attribute_list(username)}
                 for username, record in sorted(self.users.items())]
        return {"Users": users}

    # ----------------------------------------------------------- writes
    def admin_create_user(self, UserPoolId, Username, UserAttributes,
                          **kwargs):
        if Username in self.users:
            raise cognito_error("UsernameExistsException",
                                "AdminCreateUser")
        sub = str(uuid.uuid4())
        attrs = {attribute["Name"]: attribute["Value"]
                 for attribute in UserAttributes}
        self.users[Username] = {"sub": sub, "enabled": True, "attrs": attrs}
        self.history.append((Username, sub))
        return {"User": {"Username": Username, "Enabled": True,
                         "UserStatus": "FORCE_CHANGE_PASSWORD",
                         "Attributes": self._attribute_list(Username)}}

    def admin_update_user_attributes(self, UserPoolId, Username,
                                     UserAttributes):
        record = self._record(Username)
        for attribute in UserAttributes:
            record["attrs"][attribute["Name"]] = attribute["Value"]

    def admin_disable_user(self, UserPoolId, Username):
        self._record(Username)["enabled"] = False

    def admin_enable_user(self, UserPoolId, Username):
        self._record(Username)["enabled"] = True

    def admin_delete_user(self, UserPoolId, Username):
        self._record(Username)
        del self.users[Username]

    # ------------------------------------------------------------ setup
    def seed(self, username, role, enabled=True, email=None):
        sub = str(uuid.uuid4())
        self.users[username] = {
            "sub": sub, "enabled": enabled,
            "attrs": {"email": email or f"{username}@example.com",
                      "email_verified": "true", "custom:role": role},
        }
        self.history.append((username, sub))
        return sub


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "RegistryUnavailable"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    return shared_utils


@pytest.fixture(scope="module")
def enforcement_on(shared):
    """PORTAL_REGISTRY_ENFORCED on for this module only.

    The registry writes do not depend on the flag, but the denial half of
    the property does: it is the enforced mode that makes a removed or
    disabled row deny (design.md Decision 4; task 5 flips it in
    production). Restored on teardown.
    """
    variable = shared.PORTAL_REGISTRY_ENFORCED_ENV
    previous = os.environ.get(variable)
    os.environ[variable] = "true"
    yield
    if previous is None:
        os.environ.pop(variable, None)
    else:
        os.environ[variable] = previous


@pytest.fixture(scope="module")
def user_manager(aws_stack):
    """The real user_admin module imported inside the moto mock, with the
    edge-credentials table its delete path cleans up."""
    import boto3

    os.environ["EDGE_CREDENTIALS_TABLE"] = EDGE_CREDENTIALS_TABLE
    ddb = boto3.client("dynamodb", region_name=REGION)
    if EDGE_CREDENTIALS_TABLE not in all_table_names(ddb):
        ddb.create_table(
            TableName=EDGE_CREDENTIALS_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    sys.modules.pop("user_admin", None)
    import user_admin
    user_admin.USER_POOL_ID = POOL_ID
    user_admin.EDGE_CREDENTIALS_TABLE = EDGE_CREDENTIALS_TABLE
    return user_admin


#: The acting administrator's `sub`. It holds a real Portal_Identity row
#: (see the `registry` fixture): `require_portal_admin` resolves the caller's
#: Effective_Role from the registry, so a claim-only actor is denied 403
#: before any route runs.
ACTING_ADMIN_SUB = "acting-admin"


@pytest.fixture(scope="module")
def registry(aws_stack):
    """The registry table, with the acting administrator provisioned.

    The row is written once per module and never targeted by the generated
    operations (they act on `acct-*` usernames), so it plays the same role
    as the untargeted second PortalAdmin this suite already keeps: the
    last-PortalAdmin guard never fires on it.
    """
    table = aws_stack.tables.user_roles
    table.put_item(Item={
        "user_id": ACTING_ADMIN_SUB, "usecase_id": "global",
        "role": "PortalAdmin", "status": "enabled",
        "username": "portal-admin", "assigned_by": "test-acting-admin",
        "assigned_at": 1,
    })
    return table


# ---------------------------------------------------------------- helpers

def admin_event(method, path, username=None, body=None,
                acting_sub=ACTING_ADMIN_SUB):
    """A PortalAdmin request to a User Manager route.

    The acting administrator's privilege comes from its Portal_Identity row;
    the `custom:role` claim below is Claimed_Role metadata and grants
    nothing (the gate stopped trusting it when task 5.2 found the flag alone
    did not reach it).
    """
    return {
        "httpMethod": method,
        "path": path,
        "resource": path,
        "pathParameters": {"username": username} if username else None,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "authorizer": {"claims": {
                "sub": acting_sub,
                "cognito:username": "portal-admin",
                "email": "portal-admin@example.com",
                "custom:role": "PortalAdmin",
            }},
            "identity": {"sourceIp": "203.0.113.11",
                         "userAgent": "property-test/1.0"},
        },
    }


def apply_operation(user_manager, kind, username, role):
    """Drive one operation through the real handler; returns the status."""
    if kind == "create":
        event = admin_event(
            "POST", "/api/v1/admin/users",
            body={"username": username,
                  "email": f"{username}@example.com", "role": role})
    elif kind == "role_change":
        event = admin_event(
            "PUT", f"/api/v1/admin/users/{username}/role",
            username=username, body={"role": role})
    elif kind == "disable":
        event = admin_event("POST",
                            f"/api/v1/admin/users/{username}/disable",
                            username=username)
    elif kind == "enable":
        event = admin_event("POST",
                            f"/api/v1/admin/users/{username}/enable",
                            username=username)
    elif kind == "delete":
        event = admin_event("DELETE", f"/api/v1/admin/users/{username}",
                            username=username)
    else:  # pragma: no cover - the strategy generates nothing else
        raise AssertionError(f"unknown operation {kind!r}")

    return user_manager.handler(event, None)["statusCode"]


def rows_of(registry, sub):
    return registry.query(
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": sub},
    ).get("Items", [])


def global_row(registry, sub):
    return registry.get_item(
        Key={"user_id": sub, "usecase_id": "global"}).get("Item")


def counterexample(**fields):
    return json.dumps(fields, indent=2, default=str)


# ===========================================================================
# Property 6
# ===========================================================================

class TestProperty6UserManagerKeepsCognitoAndRegistryInStep:
    """See the module docstring for the property statement."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(sequence=sequences(), team_grant=st.booleans())
    # The provisioning act itself, then the two transitions Requirement
    # 3.4 promises take effect on the next request.
    @example(sequence=[("create", "acct-alpha", "DataScientist"),
                       ("disable", "acct-alpha", "Viewer")],
             team_grant=True)
    @example(sequence=[("create", "acct-alpha", "PortalAdmin"),
                       ("delete", "acct-alpha", "Viewer")],
             team_grant=True)
    # A role change is the value that takes effect (Requirement 3.3).
    @example(sequence=[("create", "acct-beta", "Viewer"),
                       ("role_change", "acct-beta", "UseCaseAdmin")],
             team_grant=False)
    # Disable then enable restores access; a create after a delete is a
    # different principal (a new `sub`, a new row).
    @example(sequence=[("create", "acct-gamma", "Operator"),
                       ("disable", "acct-gamma", "Viewer"),
                       ("enable", "acct-gamma", "Viewer"),
                       ("delete", "acct-gamma", "Viewer"),
                       ("create", "acct-gamma", "DataScientist")],
             team_grant=True)
    # Operations against accounts that do not exist, and a duplicate
    # create: rejected, and nothing may drift.
    @example(sequence=[("role_change", "acct-alpha", "PortalAdmin"),
                       ("disable", "acct-beta", "Viewer"),
                       ("delete", "acct-gamma", "Viewer"),
                       ("create", "acct-alpha", "Viewer"),
                       ("create", "acct-alpha", "PortalAdmin")],
             team_grant=False)
    def test_registry_ends_consistent_with_cognito(
            self, shared, user_manager, registry, enforcement_on, sequence,
            team_grant):
        pool = FakePool()
        user_manager.cognito_client = pool

        # The keeper: an enabled PortalAdmin in both the pool and the
        # registry, never targeted, so the last-PortalAdmin guard has
        # something to count.
        keeper_sub = pool.seed(KEEPER_USERNAME, "PortalAdmin")
        registry.put_item(Item={
            "user_id": keeper_sub, "usecase_id": "global",
            "role": "PortalAdmin", "username": KEEPER_USERNAME,
            "email": f"{KEEPER_USERNAME}@example.com", "status": "enabled",
            "assigned_by": "test", "assigned_at": 1})

        # Team Management's per-Use_Case grants live in the same table and
        # must be swept when the account is deleted.
        usecase_id = f"uc-{uuid.uuid4()}"

        for kind, username, role in sequence:
            status = apply_operation(user_manager, kind, username, role)
            assert status in (200, 201, 404, 409), counterexample(
                operation=[kind, username, role], status=status,
                sequence=sequence)
            if team_grant and kind == "create" and status == 201:
                registry.put_item(Item={
                    "user_id": pool.users[username]["sub"],
                    "usecase_id": usecase_id, "role": "Operator",
                    "username": username, "status": "enabled",
                    "assigned_by": "team-management", "assigned_at": 1})

        # ------------------------------------------------- consistency
        for username, record in pool.users.items():
            sub = record["sub"]
            row = global_row(registry, sub)
            assert row is not None, counterexample(
                problem="a Cognito account has no Portal_Identity row, so "
                        "it cannot use the portal at all (Req 3.1)",
                username=username, sequence=sequence)
            assert row["role"] == record["attrs"]["custom:role"], \
                counterexample(problem="registry role != Cognito role "
                                       "(Req 3.3)", username=username,
                               row=row, cognito=record, sequence=sequence)
            expected_status = "enabled" if record["enabled"] else "disabled"
            assert row["status"] == expected_status, counterexample(
                problem="registry status != Cognito enabled state "
                        "(Req 3.4)", username=username, row=row,
                cognito=record, sequence=sequence)
            # The row names the human, so the audit trail survives the
            # account (Requirement 4.2's motivation, design.md Decision 1).
            assert row["username"] == username, counterexample(
                username=username, row=row)
            assert row["email"] == record["attrs"]["email"], counterexample(
                username=username, row=row)

        # Deleted accounts leave nothing behind — neither the global row
        # that carries privilege nor a per-Use_Case grant naming a role.
        for username, sub in pool.history:
            if pool.users.get(username, {}).get("sub") == sub:
                continue
            assert rows_of(registry, sub) == [], counterexample(
                problem="a deleted account kept Portal_Identity rows, so "
                        "an already-minted token stays privileged "
                        "(Req 3.4)",
                username=username, sub=sub, sequence=sequence)

        # ------------------------------------------------------ denial
        # Every enabled account resolves exactly its registry role; every
        # disabled or deleted one resolves nothing, whatever its token
        # claims — which is what "denied on its next request" means.
        for username, record in pool.users.items():
            sub = record["sub"]
            claimed = record["attrs"]["custom:role"]
            info = {"user_id": sub, "username": username,
                    "email": record["attrs"]["email"], "role": claimed}
            resolved = shared.rbac_manager.get_user_role(sub, "global",
                                                         user_info=info)
            if record["enabled"]:
                assert resolved == shared.Role(claimed), counterexample(
                    username=username, resolved=str(resolved),
                    expected=claimed, sequence=sequence)
            else:
                assert resolved is None, counterexample(
                    problem="a disabled account still resolves a role "
                            "(Req 3.4 / 1.6)",
                    username=username, resolved=str(resolved),
                    sequence=sequence)
                assert shared.rbac_manager.has_permission(
                    sub, "global", shared.Permission.VIEW_USECASES,
                    user_info=info) is False

        for username, sub in pool.history:
            if pool.users.get(username, {}).get("sub") == sub:
                continue
            info = {"user_id": sub, "username": username,
                    "email": f"{username}@example.com",
                    "role": "PortalAdmin"}
            assert shared.rbac_manager.get_user_role(
                sub, "global", user_info=info) is None, counterexample(
                problem="a deleted account still resolves a role "
                        "(Req 3.4)", username=username, sub=sub,
                sequence=sequence)
            assert shared.rbac_manager.is_portal_admin(
                sub, user_info=info) is False
