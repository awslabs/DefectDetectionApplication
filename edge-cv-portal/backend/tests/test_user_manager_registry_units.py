"""
Unit tests for the User Manager as the Portal_Identity registry's writer —
portal-jwt-role-privilege-escalation task 2.5.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

design.md "Testing Strategy" -> **Units**: "the User Manager's five
transitions; the last-PortalAdmin count". The shared-layer half of the
same task (`_lookup_identity`, the flag's two modes, the `would_deny`
WARNING, `attribution_from`) lives in `test_portal_registry_units.py`.

The five transitions are create / role-change / disable / enable / delete
(design.md "Fix Implementation" -> `user_admin.py`, Requirements 3.1-3.4).
Property 6 (task 2.4) already states the end-state invariant "the registry
ends consistent with Cognito" over generated sequences; what it does not
pin is each transition's **row content** and its **failure branch**, which
is what these tests do:

* the exact attributes written (`role`, `username`, `email`, `status`,
  `assigned_by`/`assigned_at`, `updated_by`/`updated_at`)
* the three new 502 responses — `account was not provisioned`,
  `role change incomplete`, `<verb> incomplete` — and the partial-state
  detail each records, plus the registry arm of the delete path's
  existing `partial deletion` 502 (Requirement 3.2 and its analogues)
* the fail-closed no-`sub` branch (task 2.3 decision (b))
* `_count_registry_portal_admins` arithmetic and the flag gate in
  `_count_enabled_portal_admins` (Requirement 3.5), including the reason
  the gate exists: counting the near-empty pre-backfill registry would
  reject every PortalAdmin change as "the last admin"

Cognito is a stateful in-memory `FakePool` (the pattern used by the
`test_user_admin_*.py` suites); everything else is real — the module's
own routing, the two-phase audit protocol against the moto-backed audit
table, and the registry writes against a table **private to this module**
(`test-user-manager-units-roles`, pointed at by monkeypatching
`user_admin.USER_ROLES_TABLE` / `shared_utils.USER_ROLES_TABLE`, which
production code reads at call time). The shared `test-user-roles` table
other suites seed is therefore never touched, and the last-PortalAdmin
scan cannot see their rows.

Run from `edge-cv-portal/backend` with
`~/.venvs/dda-portal-tests/bin/python -m pytest <this file> -q
-p no:cacheprovider`.

_Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1_
"""
import json
import sys
import uuid

import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
POOL_ID = "us-east-1_unitpool"
UNIT_REGISTRY_TABLE = "test-user-manager-units-roles"
UNIT_CREDENTIALS_TABLE = "test-user-manager-units-credentials"
AUDIT_LOG_TABLE = "test-audit-log"


# ------------------------------------------------------------- fake Cognito

def cognito_error(code, operation, message=None):
    return ClientError(
        {"Error": {"Code": code, "Message": message or f"{code} (fake)"}},
        operation)


class FakePool:
    """In-memory user pool for the six admin calls `user_admin` makes.

    `report_sub=False` reproduces the (production-unreachable) case where
    Cognito names no `sub` for an account, which the module treats as
    fail-closed: no registry row is written and the account has no portal
    access (task 2.3 decision (b)).
    """

    def __init__(self, report_sub=True):
        self.users = {}
        self.report_sub = report_sub
        self.calls = []

    # ------------------------------------------------------------- setup
    def seed(self, username, role="Viewer", enabled=True, email=None):
        sub = str(uuid.uuid4())
        self.users[username] = {
            "sub": sub, "enabled": enabled,
            "attrs": {"email": email or f"{username}@example.com",
                      "email_verified": "true", "custom:role": role},
        }
        return sub

    def sub_of(self, username):
        return self.users[username]["sub"]

    def role_of(self, username):
        return self.users[username]["attrs"].get("custom:role")

    # ------------------------------------------------------------- reads
    def _record(self, username):
        if username not in self.users:
            raise cognito_error("UserNotFoundException", "AdminGetUser")
        return self.users[username]

    def _attribute_list(self, username):
        record = self.users[username]
        attributes = [{"Name": name, "Value": value}
                      for name, value in record["attrs"].items()]
        if self.report_sub:
            attributes.append({"Name": "sub", "Value": record["sub"]})
        return attributes

    def admin_get_user(self, UserPoolId, Username):
        self.calls.append(("admin_get_user", Username))
        record = self._record(Username)
        return {"Username": Username, "Enabled": record["enabled"],
                "UserStatus": "CONFIRMED",
                "UserAttributes": self._attribute_list(Username)}

    def list_users(self, UserPoolId, Limit=None, PaginationToken=None):
        self.calls.append(("list_users", None))
        return {"Users": [
            {"Username": username, "Enabled": record["enabled"],
             "UserStatus": "CONFIRMED",
             "Attributes": self._attribute_list(username)}
            for username, record in sorted(self.users.items())]}

    # ------------------------------------------------------------ writes
    def admin_create_user(self, UserPoolId, Username, UserAttributes,
                          **kwargs):
        self.calls.append(("admin_create_user", Username))
        if Username in self.users:
            raise cognito_error("UsernameExistsException", "AdminCreateUser")
        sub = str(uuid.uuid4())
        self.users[Username] = {
            "sub": sub, "enabled": True,
            "attrs": {attribute["Name"]: attribute["Value"]
                      for attribute in UserAttributes},
        }
        return {"User": {"Username": Username, "Enabled": True,
                         "UserStatus": "FORCE_CHANGE_PASSWORD",
                         "Attributes": self._attribute_list(Username)}}

    def admin_update_user_attributes(self, UserPoolId, Username,
                                     UserAttributes):
        self.calls.append(("admin_update_user_attributes", Username))
        record = self._record(Username)
        for attribute in UserAttributes:
            record["attrs"][attribute["Name"]] = attribute["Value"]

    def admin_disable_user(self, UserPoolId, Username):
        self.calls.append(("admin_disable_user", Username))
        self._record(Username)["enabled"] = False

    def admin_enable_user(self, UserPoolId, Username):
        self.calls.append(("admin_enable_user", Username))
        self._record(Username)["enabled"] = True

    def admin_delete_user(self, UserPoolId, Username):
        self.calls.append(("admin_delete_user", Username))
        self._record(Username)
        del self.users[Username]


# ------------------------------------------------- registry write failures

class _FailingTable:
    """A registry table handle whose every call raises."""

    def __init__(self, error):
        self._error = error

    def _raise(self, *args, **kwargs):
        raise self._error

    get_item = _raise
    query = _raise
    scan = _raise
    put_item = _raise
    update_item = _raise
    delete_item = _raise


class _RegistryFailureResource:
    """DynamoDB proxy failing ONLY the registry table, so the audit and
    sync writes on the same code path still land."""

    def __init__(self, real, registry_table_name, error):
        self._real = real
        self._registry_table_name = registry_table_name
        self._error = error

    def Table(self, name):  # noqa: N802 - boto3 resource API
        if name == self._registry_table_name:
            return _FailingTable(self._error)
        return self._real.Table(name)


class _PagingTable:
    """A registry table whose `scan` returns the given pages, so the
    guard's pagination is exercised without a 1 MB fixture."""

    def __init__(self, pages):
        self.pages = pages
        self.scan_calls = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        index = int(kwargs.get("ExclusiveStartKey", {}).get("page", 0))
        page = self.pages[index]
        response = {"Items": page}
        if index + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"page": index + 1}
        return response


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "RegistryUnavailable"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    return shared_utils


@pytest.fixture(scope="module")
def _unit_tables(aws_stack):
    """This module's private registry and edge-credentials tables."""
    import boto3

    ddb = boto3.client("dynamodb", region_name=REGION)
    existing = ddb.list_tables()["TableNames"]
    if UNIT_REGISTRY_TABLE not in existing:
        ddb.create_table(
            TableName=UNIT_REGISTRY_TABLE,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                       {"AttributeName": "usecase_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    if UNIT_CREDENTIALS_TABLE not in existing:
        ddb.create_table(
            TableName=UNIT_CREDENTIALS_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    resource = boto3.resource("dynamodb", region_name=REGION)
    return (resource.Table(UNIT_REGISTRY_TABLE),
            resource.Table(UNIT_CREDENTIALS_TABLE),
            resource.Table(AUDIT_LOG_TABLE))


@pytest.fixture(scope="module")
def user_admin_module(aws_stack):
    """The real user_admin imported inside the moto mock."""
    sys.modules.pop("user_admin", None)
    import user_admin
    return user_admin


@pytest.fixture
def registry(shared, user_admin_module, _unit_tables, monkeypatch):
    """The private registry table, wired into both modules and emptied."""
    table = _unit_tables[0]
    monkeypatch.setattr(user_admin_module, "USER_ROLES_TABLE",
                        UNIT_REGISTRY_TABLE)
    monkeypatch.setattr(shared, "USER_ROLES_TABLE", UNIT_REGISTRY_TABLE)
    for item in table.scan().get("Items", []):
        table.delete_item(Key={"user_id": item["user_id"],
                               "usecase_id": item["usecase_id"]})
    return table


@pytest.fixture
def audit_table(_unit_tables):
    return _unit_tables[2]


@pytest.fixture
def user_admin(user_admin_module, registry, monkeypatch):
    """user_admin wired to the fake pool and this module's tables."""
    monkeypatch.setattr(user_admin_module, "USER_POOL_ID", POOL_ID)
    monkeypatch.setattr(user_admin_module, "EDGE_CREDENTIALS_TABLE",
                        UNIT_CREDENTIALS_TABLE)
    return user_admin_module


@pytest.fixture
def pool(user_admin, monkeypatch):
    def _install(report_sub=True):
        fake = FakePool(report_sub=report_sub)
        monkeypatch.setattr(user_admin, "cognito_client", fake)
        return fake
    return _install


@pytest.fixture
def break_registry_writes(user_admin, monkeypatch):
    def _break(error=None):
        error = error or ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException",
                       "Message": "registry write rejected"}},
            "PutItem")
        monkeypatch.setattr(
            user_admin, "dynamodb",
            _RegistryFailureResource(user_admin.dynamodb,
                                     UNIT_REGISTRY_TABLE, error))
        return error
    return _break


@pytest.fixture
def enforcement_off(shared, monkeypatch):
    monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)


@pytest.fixture
def enforcement_on(shared, monkeypatch):
    monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")


# ---------------------------------------------------------------- helpers

ACTING_IP = "203.0.113.7"
ACTING_AGENT = "unit-test/1.0"


def admin_event(method, path, username=None, body=None, acting_sub=None):
    """A PortalAdmin request to a User Manager route.

    `require_portal_admin` gates /admin/* on the `custom:role` claim (task
    2.3 recorded that as a separate gap, owned by task 5), so the acting
    administrator's claim is what gets it through.
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
                "sub": acting_sub or str(uuid.uuid4()),
                "cognito:username": "portal-admin",
                "email": "portal-admin@example.com",
                "custom:role": "PortalAdmin",
            }},
            "identity": {"sourceIp": ACTING_IP, "userAgent": ACTING_AGENT},
        },
    }


def invoke(user_admin, event):
    response = user_admin.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def create(user_admin, username, role="Viewer", email=None,
           acting_sub=None):
    return invoke(user_admin, admin_event(
        "POST", "/api/v1/admin/users",
        body={"username": username, "email": email or f"{username}@example.com",
              "role": role},
        acting_sub=acting_sub))


def change_role(user_admin, username, role, acting_sub=None):
    return invoke(user_admin, admin_event(
        "PUT", f"/api/v1/admin/users/{username}/role", username=username,
        body={"role": role}, acting_sub=acting_sub))


def set_enabled(user_admin, username, enabled, acting_sub=None):
    verb = "enable" if enabled else "disable"
    return invoke(user_admin, admin_event(
        "POST", f"/api/v1/admin/users/{username}/{verb}", username=username,
        acting_sub=acting_sub))


def delete(user_admin, username, acting_sub=None):
    return invoke(user_admin, admin_event(
        "DELETE", f"/api/v1/admin/users/{username}", username=username,
        acting_sub=acting_sub))


def global_row(registry, sub):
    return registry.get_item(
        Key={"user_id": sub, "usecase_id": "global"}).get("Item")


def rows_of(registry, sub):
    return registry.query(
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": sub}).get("Items", [])


def put_row(registry, sub, usecase_id="global", role="Viewer",
            status="enabled", **extra):
    item = {"user_id": sub, "usecase_id": usecase_id, "role": role,
            "username": f"user-{sub[:8]}", "status": status}
    item.update(extra)
    registry.put_item(Item=item)
    return item


def audit_rows(audit_table, acting_sub, action=None):
    rows = [item for item in audit_table.scan().get("Items", [])
            if item.get("user_id") == acting_sub
            and (action is None or item.get("action") == action)]
    return sorted(rows, key=lambda item: item["timestamp"])


# ===========================================================================
# Transition 1 — create writes the global row (Requirements 3.1, 3.2)
# ===========================================================================

class TestCreateWritesTheGlobalRow:

    def test_created_account_is_provisioned_with_every_attribute(
            self, user_admin, registry, pool, audit_table):
        fake = pool()
        acting = str(uuid.uuid4())

        status, body = create(user_admin, "new-scientist",
                              role="DataScientist",
                              email="new-scientist@example.com",
                              acting_sub=acting)
        assert status == 201, body

        row = global_row(registry, fake.sub_of("new-scientist"))
        assert row is not None, "the account was created but not provisioned"
        assert row["role"] == "DataScientist"
        assert row["username"] == "new-scientist"
        assert row["email"] == "new-scientist@example.com"
        assert row["status"] == "enabled"
        assert row["assigned_by"] == acting
        assert int(row["assigned_at"]) > 0
        # The row is keyed on the Cognito `sub` — the value the token
        # carries — not on the username.
        assert row["user_id"] == fake.sub_of("new-scientist")

    def test_audit_entry_records_the_registry_write_and_the_request(
            self, user_admin, registry, pool, audit_table):
        fake = pool()
        acting = str(uuid.uuid4())
        create(user_admin, "audited-account", role="Operator",
               acting_sub=acting)

        rows = audit_rows(audit_table, acting, "account_create")
        assert len(rows) == 1
        entry = rows[0]
        assert entry["result"] == "success"
        assert entry["details"]["registry_entry"] == "written"
        assert entry["details"]["created_user_id"] == \
            fake.sub_of("audited-account")
        # Durable attribution taken from the request (Requirement 4.1/4.2).
        assert entry["username"] == "portal-admin"
        assert entry["email"] == "portal-admin@example.com"
        assert entry["source_ip"] == ACTING_IP
        assert entry["user_agent"] == ACTING_AGENT

    def test_registry_write_failure_answers_502_and_audits_the_partial_state(
            self, user_admin, registry, pool, audit_table,
            break_registry_writes):
        """Requirement 3.2: the Cognito account exists but is inert,
        because an absent registry entry is denied (Requirement 1.1)."""
        fake = pool()
        error = break_registry_writes()
        acting = str(uuid.uuid4())

        status, body = create(user_admin, "half-created", role="PortalAdmin",
                              acting_sub=acting)
        assert status == 502
        assert body["error"] == "account was not provisioned"
        assert "no portal access" in body["message"]
        # Cognito holds the account; the registry does not name it.
        assert "half-created" in fake.users
        assert global_row(registry, fake.sub_of("half-created")) is None

        entry = audit_rows(audit_table, acting, "account_create")[0]
        assert entry["result"] == "failure"
        assert "Portal_Identity registry entry was not" in \
            entry["details"]["partial_state"]
        assert entry["details"]["created_user_id"] == \
            fake.sub_of("half-created")
        assert str(error) in entry["details"]["reason"]

    def test_an_account_with_no_sub_is_left_unprovisioned_and_recorded(
            self, user_admin, registry, pool, audit_table):
        """Fail-closed: a row is never written under an invented key,
        because the read path looks rows up by the token's `sub` (task 2.3
        decision (b))."""
        fake = pool(report_sub=False)
        acting = str(uuid.uuid4())

        status, body = create(user_admin, "no-sub-account", role="Viewer",
                              acting_sub=acting)
        assert status == 201, body
        assert registry.scan().get("Items", []) == []

        entry = audit_rows(audit_table, acting, "account_create")[0]
        assert entry["details"]["registry_entry"].startswith("skipped")
        assert entry["details"]["created_user_id"] == "unknown"

    def test_a_rejected_creation_writes_no_row(self, user_admin, registry,
                                               pool):
        fake = pool()
        fake.seed("taken", role="Viewer")
        status, body = create(user_admin, "taken", role="Viewer")
        assert status == 409
        assert rows_of(registry, fake.sub_of("taken")) == []


# ===========================================================================
# Transition 2 — a role change updates the row (Requirement 3.3)
# ===========================================================================

class TestRoleChangeUpdatesTheGlobalRow:

    def test_registry_role_follows_the_change(self, user_admin, registry,
                                              pool):
        fake = pool()
        sub = fake.seed("scientist", role="Viewer")
        put_row(registry, sub, role="Viewer")
        acting = str(uuid.uuid4())

        status, body = change_role(user_admin, "scientist", "DataScientist",
                                   acting_sub=acting)
        assert status == 200, body

        row = global_row(registry, sub)
        assert row["role"] == "DataScientist"
        assert row["username"] == "scientist"
        assert row["email"] == "scientist@example.com"
        assert row["status"] == "enabled"
        assert row["updated_by"] == acting
        assert int(row["updated_at"]) > 0
        # Cognito's descriptive attribute is updated too, but it is the
        # registry value that takes effect.
        assert fake.role_of("scientist") == "DataScientist"

    def test_a_missing_row_is_created_by_the_change(self, user_admin,
                                                    registry, pool):
        """A PortalAdmin changing an account's role through the portal IS
        the provisioning act; before the backfill most accounts have no
        row yet."""
        fake = pool()
        sub = fake.seed("unprovisioned", role="Viewer")
        assert global_row(registry, sub) is None

        status, body = change_role(user_admin, "unprovisioned", "Operator")
        assert status == 200, body
        row = global_row(registry, sub)
        assert row["role"] == "Operator"
        assert row["status"] == "enabled"

    def test_a_disabled_account_keeps_its_disabled_status(self, user_admin,
                                                          registry, pool):
        """`status` is written from the account's Cognito enabled state, so
        a role change cannot silently re-enable portal access."""
        fake = pool()
        sub = fake.seed("suspended", role="Viewer", enabled=False)
        put_row(registry, sub, role="Viewer", status="disabled")

        status, body = change_role(user_admin, "suspended", "UseCaseAdmin")
        assert status == 200, body
        row = global_row(registry, sub)
        assert row["role"] == "UseCaseAdmin"
        assert row["status"] == "disabled"

    def test_registry_update_failure_answers_502_role_change_incomplete(
            self, user_admin, registry, pool, audit_table,
            break_registry_writes):
        fake = pool()
        sub = fake.seed("stuck", role="Viewer")
        put_row(registry, sub, role="Viewer")
        break_registry_writes()
        acting = str(uuid.uuid4())

        status, body = change_role(user_admin, "stuck", "PortalAdmin",
                                   acting_sub=acting)
        assert status == 502
        assert body["error"] == "role change incomplete"
        assert "effective role is unchanged" in body["message"]
        # Cognito carries the new claim; the effective role did not move.
        assert fake.role_of("stuck") == "PortalAdmin"
        assert global_row(registry, sub)["role"] == "Viewer"

        entry = audit_rows(audit_table, acting, "role_change")[0]
        assert entry["result"] == "failure"
        assert "effective role is unchanged" in \
            entry["details"]["partial_state"]

    def test_a_role_change_on_a_missing_account_writes_nothing(
            self, user_admin, registry, pool):
        pool()
        status, _ = change_role(user_admin, "ghost", "PortalAdmin")
        assert status == 404
        assert registry.scan().get("Items", []) == []


# ===========================================================================
# Transitions 3 and 4 — disable / enable set `status` (Requirement 3.4)
# ===========================================================================

class TestDisableAndEnableSetTheRegistryStatus:

    def test_disable_marks_the_row_disabled(self, user_admin, registry, pool):
        fake = pool()
        sub = fake.seed("to-disable", role="DataScientist")
        put_row(registry, sub, role="DataScientist")
        acting = str(uuid.uuid4())

        status, body = set_enabled(user_admin, "to-disable", False,
                                   acting_sub=acting)
        assert status == 200 and body["changed"] is True
        row = global_row(registry, sub)
        assert row["status"] == "disabled"
        assert row["role"] == "DataScientist"   # the role is not erased
        assert row["updated_by"] == acting

    def test_enable_marks_the_row_enabled(self, user_admin, registry, pool):
        fake = pool()
        sub = fake.seed("to-enable", role="Operator", enabled=False)
        put_row(registry, sub, role="Operator", status="disabled")

        status, body = set_enabled(user_admin, "to-enable", True)
        assert status == 200 and body["changed"] is True
        assert global_row(registry, sub)["status"] == "enabled"

    def test_a_disabled_account_is_denied_on_its_next_request(
            self, user_admin, shared, registry, pool, enforcement_on):
        """The point of Requirement 3.4: Cognito's own disable only blocks
        NEW sign-ins, so it is the row that has to stop an already-minted
        token."""
        fake = pool()
        sub = fake.seed("token-holder", role="DataScientist")
        put_row(registry, sub, role="DataScientist")
        claim = {"user_id": sub, "username": "token-holder",
                 "email": "token-holder@example.com",
                 "role": "DataScientist"}
        assert shared.rbac_manager.get_user_role(sub, "global", claim) == \
            shared.Role.DATA_SCIENTIST

        assert set_enabled(user_admin, "token-holder", False)[0] == 200
        assert shared.rbac_manager.get_user_role(sub, "global", claim) is None

        assert set_enabled(user_admin, "token-holder", True)[0] == 200
        assert shared.rbac_manager.get_user_role(sub, "global", claim) == \
            shared.Role.DATA_SCIENTIST

    def test_a_no_op_request_writes_nothing(self, user_admin, registry, pool):
        """Already in the requested state: no Cognito mutation, no audit
        pending write, and no registry write either."""
        fake = pool()
        sub = fake.seed("already-enabled", role="Viewer")
        put_row(registry, sub, role="Viewer")
        before = global_row(registry, sub)

        status, body = set_enabled(user_admin, "already-enabled", True)
        assert status == 200 and body["changed"] is False
        assert global_row(registry, sub) == before
        assert "admin_enable_user" not in [call[0] for call in fake.calls]

    @pytest.mark.parametrize("target_enabled,expected_error", [
        (False, "disable incomplete"),
        (True, "enable incomplete"),
    ])
    def test_registry_status_failure_answers_502(
            self, user_admin, registry, pool, audit_table,
            break_registry_writes, target_enabled, expected_error):
        fake = pool()
        sub = fake.seed("flaky", role="Viewer", enabled=not target_enabled)
        put_row(registry, sub, role="Viewer",
                status="disabled" if target_enabled else "enabled")
        break_registry_writes()
        acting = str(uuid.uuid4())

        status, body = set_enabled(user_admin, "flaky", target_enabled,
                                   acting_sub=acting)
        assert status == 502
        assert body["error"] == expected_error
        assert "portal access is unchanged" in body["message"]
        # Cognito moved; the registry did not.
        assert fake.users["flaky"]["enabled"] is target_enabled
        assert global_row(registry, sub)["status"] == (
            "disabled" if target_enabled else "enabled")

        action = "account_enable" if target_enabled else "account_disable"
        entry = audit_rows(audit_table, acting, action)[0]
        assert entry["result"] == "failure"
        assert "portal access is unchanged" in \
            entry["details"]["partial_state"]


# ===========================================================================
# Transition 5 — delete removes the rows (Requirement 3.4)
# ===========================================================================

class TestDeleteRemovesTheRegistryRows:

    def test_delete_removes_the_global_row_and_every_usecase_grant(
            self, user_admin, registry, pool):
        fake = pool()
        sub = fake.seed("departing", role="DataScientist")
        put_row(registry, sub, role="DataScientist")
        # A Team Management per-Use_Case grant in the same table.
        put_row(registry, sub, usecase_id="uc-1", role="Operator")

        status, body = delete(user_admin, "departing")
        assert status == 200, body
        assert rows_of(registry, sub) == []

    def test_the_key_is_resolved_before_the_cognito_delete(
            self, user_admin, registry, pool, enforcement_off):
        """After `admin_delete_user` the pool can no longer resolve the
        username to its `sub` — exactly what the recorded incident
        exploited — so the row would be unreachable if the key were read
        afterwards."""
        fake = pool()
        sub = fake.seed("gone-in-4-seconds", role="PortalAdmin")
        put_row(registry, sub, role="PortalAdmin")
        # Keepers for the last-PortalAdmin guard in either flag mode.
        put_row(registry, fake.seed("keeper-admin", role="PortalAdmin"),
                role="PortalAdmin")

        assert delete(user_admin, "gone-in-4-seconds")[0] == 200
        assert "gone-in-4-seconds" not in fake.users
        assert rows_of(registry, sub) == []

    def test_a_deleted_principal_resolves_no_role(
            self, user_admin, shared, registry, pool, enforcement_on):
        fake = pool()
        sub = fake.seed("deleted-actor", role="PortalAdmin")
        put_row(registry, sub, role="PortalAdmin")
        put_row(registry, str(uuid.uuid4()), role="PortalAdmin")  # keeper
        claim = {"user_id": sub, "username": "deleted-actor",
                 "email": "deleted-actor@example.com", "role": "PortalAdmin"}

        assert delete(user_admin, "deleted-actor")[0] == 200
        assert shared.rbac_manager.get_user_role(sub, "global", claim) is None
        assert shared.rbac_manager.is_portal_admin(sub, claim) is False

    def test_registry_removal_failure_reports_the_partial_deletion(
            self, user_admin, registry, pool, audit_table,
            break_registry_writes):
        fake = pool()
        sub = fake.seed("stubborn", role="Viewer")
        put_row(registry, sub, role="Viewer")
        break_registry_writes()
        acting = str(uuid.uuid4())

        status, body = delete(user_admin, "stubborn", acting_sub=acting)
        assert status == 502
        assert body["error"] == "partial deletion"
        assert "portal registry entry" in body["message"]
        # The account is gone from the pool but still named in the registry
        # — which is why the response must not claim success.
        assert "stubborn" not in fake.users
        assert global_row(registry, sub) is not None

        entry = audit_rows(audit_table, acting, "account_delete")[0]
        assert "Portal_Identity registry entry" in \
            entry["details"]["partial_cleanup"]

    def test_deleting_a_missing_account_writes_nothing(self, user_admin,
                                                       registry, pool):
        pool()
        status, _ = delete(user_admin, "never-existed")
        assert status == 404
        assert registry.scan().get("Items", []) == []


# ===========================================================================
# The last-PortalAdmin count (Requirement 3.5)
# ===========================================================================

class TestRegistryPortalAdminCount:
    """`_count_registry_portal_admins` arithmetic."""

    def test_counts_enabled_global_portal_admin_rows_only(self, user_admin,
                                                          registry):
        put_row(registry, str(uuid.uuid4()), role="PortalAdmin")
        put_row(registry, str(uuid.uuid4()), role="PortalAdmin")
        # Not counted: a disabled admin, a non-admin, and a per-Use_Case
        # grant (which decides nothing at the account level).
        put_row(registry, str(uuid.uuid4()), role="PortalAdmin",
                status="disabled")
        put_row(registry, str(uuid.uuid4()), role="DataScientist")
        put_row(registry, str(uuid.uuid4()), usecase_id="uc-1",
                role="PortalAdmin")
        assert user_admin._count_registry_portal_admins() == 2

    def test_a_row_without_status_counts_as_enabled(self, user_admin,
                                                    registry):
        """Matching the read path: rows written before this spec carry
        no `status`."""
        sub = str(uuid.uuid4())
        registry.put_item(Item={"user_id": sub, "usecase_id": "global",
                                "role": "PortalAdmin"})
        assert user_admin._count_registry_portal_admins() == 1

    def test_an_empty_registry_counts_zero(self, user_admin, registry):
        assert user_admin._count_registry_portal_admins() == 0

    def test_the_scan_is_paginated(self, user_admin, registry, monkeypatch):
        paging = _PagingTable([
            [{"user_id": "a", "usecase_id": "global", "role": "PortalAdmin",
              "status": "enabled"}],
            [{"user_id": "b", "usecase_id": "global", "role": "PortalAdmin"}],
            [{"user_id": "c", "usecase_id": "global", "role": "PortalAdmin",
              "status": "disabled"}],
        ])
        monkeypatch.setattr(user_admin, "_registry_table", lambda: paging)
        assert user_admin._count_registry_portal_admins() == 2
        assert len(paging.scan_calls) == 3
        assert paging.scan_calls[-1]["ExclusiveStartKey"] == {"page": 2}


class TestCountEnabledPortalAdminsIsFlagGated:
    """The guard must count whatever currently decides privilege
    (task 2.3 decision (a))."""

    def test_enforcement_off_counts_the_pool(self, user_admin, registry, pool,
                                             enforcement_off):
        fake = pool()
        fake.seed("admin-one", role="PortalAdmin")
        fake.seed("admin-two", role="PortalAdmin")
        fake.seed("admin-disabled", role="PortalAdmin", enabled=False)
        fake.seed("viewer", role="Viewer")
        # The registry is empty before the backfill; counting it here
        # would report zero admins and reject every change.
        assert registry.scan().get("Items", []) == []
        assert user_admin._count_enabled_portal_admins() == 2

    def test_enforcement_on_counts_the_registry(self, user_admin, registry,
                                                pool, enforcement_on):
        fake = pool()
        fake.seed("claims-admin-one", role="PortalAdmin")
        fake.seed("claims-admin-two", role="PortalAdmin")
        # Only one of the two claims is backed by a registry row, and a
        # `custom:role` claim administers nothing under enforcement.
        put_row(registry, fake.sub_of("claims-admin-one"), role="PortalAdmin")
        assert user_admin._count_enabled_portal_admins() == 1

    def test_enforcement_on_ignores_pool_only_admins(self, user_admin,
                                                     registry, pool,
                                                     enforcement_on):
        fake = pool()
        fake.seed("pool-only-admin", role="PortalAdmin")
        assert user_admin._count_enabled_portal_admins() == 0


class TestLastPortalAdminGuardUsesTheCount:
    """The count reaches the three guarded transitions."""

    def _seed_admin(self, fake, registry, username="solo-admin"):
        sub = fake.seed(username, role="PortalAdmin")
        put_row(registry, sub, role="PortalAdmin")
        return sub

    def test_demoting_the_last_registry_admin_is_rejected(
            self, user_admin, registry, pool, audit_table, enforcement_on):
        fake = pool()
        self._seed_admin(fake, registry)
        acting = str(uuid.uuid4())

        status, body = change_role(user_admin, "solo-admin", "Viewer",
                                   acting_sub=acting)
        assert status == 409
        assert body["error"] == "Role change rejected"
        assert "last remaining enabled PortalAdmin" in body["message"]
        # Nothing moved, and the rejected attempt is audited.
        assert fake.role_of("solo-admin") == "PortalAdmin"
        assert global_row(registry, fake.sub_of("solo-admin"))["role"] == \
            "PortalAdmin"
        entry = audit_rows(audit_table, acting, "role_change")[0]
        assert entry["result"] == "rejected"

    def test_disabling_the_last_registry_admin_is_rejected(
            self, user_admin, registry, pool, enforcement_on):
        fake = pool()
        self._seed_admin(fake, registry)
        status, body = set_enabled(user_admin, "solo-admin", False)
        assert status == 409 and body["error"] == "Disable rejected"
        assert global_row(registry, fake.sub_of("solo-admin"))["status"] == \
            "enabled"

    def test_deleting_the_last_registry_admin_is_rejected(
            self, user_admin, registry, pool, enforcement_on):
        fake = pool()
        sub = self._seed_admin(fake, registry)
        status, body = delete(user_admin, "solo-admin")
        assert status == 409 and body["error"] == "Deletion rejected"
        assert "solo-admin" in fake.users
        assert global_row(registry, sub) is not None

    def test_a_second_registry_admin_allows_the_change(
            self, user_admin, registry, pool, enforcement_on):
        fake = pool()
        sub = self._seed_admin(fake, registry)
        second = fake.seed("second-admin", role="PortalAdmin")
        put_row(registry, second, role="PortalAdmin")

        status, body = change_role(user_admin, "solo-admin", "Viewer")
        assert status == 200, body
        assert global_row(registry, sub)["role"] == "Viewer"

    def test_before_the_backfill_the_pool_count_is_what_allows_it(
            self, user_admin, registry, pool, enforcement_off):
        """The reason the count is flag-gated: with enforcement off the
        registry may be empty while the pool holds several admins, and
        every PortalAdmin change must still be possible."""
        fake = pool()
        fake.seed("pool-admin-one", role="PortalAdmin")
        fake.seed("pool-admin-two", role="PortalAdmin")
        assert registry.scan().get("Items", []) == []

        status, body = change_role(user_admin, "pool-admin-one", "Viewer")
        assert status == 200, body
        # The change still provisions/updates the row it will need later.
        assert global_row(registry, fake.sub_of("pool-admin-one"))["role"] == \
            "Viewer"

    def test_a_single_pool_admin_is_still_protected_with_the_flag_off(
            self, user_admin, registry, pool, enforcement_off):
        fake = pool()
        fake.seed("only-pool-admin", role="PortalAdmin")
        status, body = change_role(user_admin, "only-pool-admin", "Viewer")
        assert status == 409, body
        assert fake.role_of("only-pool-admin") == "PortalAdmin"
