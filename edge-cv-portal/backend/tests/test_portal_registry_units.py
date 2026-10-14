"""
Unit tests for the Portal_Identity read path and audit attribution —
portal-jwt-role-privilege-escalation task 2.5.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

design.md "Testing Strategy" -> **Units**: "`_lookup_identity` branches;
the flag's two modes; the `would_deny` WARNING; `attribution_from` with
partial/absent claims". The User Manager's five transitions and the
last-PortalAdmin count — the other half of the same task — live in
`test_user_manager_registry_units.py`.

Where the property suites (task 2.4) state *invariants* over generated
input, these pin the individual branches by name, so a branch that
silently stops being reachable (or starts returning something else) fails
here with a message naming the branch:

* `registry_enforcement_enabled()` — the single enforcement switch and
  its accepted spellings (Requirement 2.4).
* `_lookup_identity` — every path of design.md's Expected Behavior
  precedence, plus the two failure conversions
  (`ClientError` / arbitrary exception -> `RegistryUnavailable`,
  Requirement 1.5) and the disabled-row cases (Requirement 1.6).
* `RBACManager.get_user_role` in **both** flag modes on the same registry
  state, so the flag is demonstrably the only difference (and the legacy
  mode still shows the pre-fix claim behaviour that task 5 flips).
* the `would_deny` dry-run WARNING (caplog), which is what task 5.1 reads
  from deployed logs to decide whether the backfill is complete.
* `attribution_from` with full / partial / absent claims (Requirement
  4.1, 4.2, 4.3), including the `identity_source` a registry outage
  records and the guarantee that attribution never raises.

Isolation: the Portal_Identity registry reads go to a table private to
this module (`test-portal-registry-units-roles`), pointed at by
monkeypatching `shared_utils.USER_ROLES_TABLE` — which the production
code reads at call time — and emptied per test. The session-scoped
`test-user-roles` table other suites seed is therefore never read or
written here, in either direction.

Run from `edge-cv-portal/backend` with
`~/.venvs/dda-portal-tests/bin/python -m pytest <this file> -q
-p no:cacheprovider`.

_Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 2.4, 4.1, 4.2, 4.3_
"""
import logging
import uuid

import pytest
from botocore.exceptions import ClientError
from dynamo_helpers import all_table_names

REGION = "us-east-1"

# A registry table private to this module (see the docstring).
UNIT_REGISTRY_TABLE = "test-portal-registry-units-roles"


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
def _unit_registry_table(aws_stack):
    """Create this module's private registry table (same key schema as
    `dda-portal-user-roles`: (user_id, usecase_id))."""
    import boto3

    ddb = boto3.client("dynamodb", region_name=REGION)
    if UNIT_REGISTRY_TABLE not in all_table_names(ddb):
        ddb.create_table(
            TableName=UNIT_REGISTRY_TABLE,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "usecase_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    return boto3.resource("dynamodb", region_name=REGION).Table(
        UNIT_REGISTRY_TABLE)


@pytest.fixture
def registry(shared, _unit_registry_table, monkeypatch):
    """The private registry table, wired into shared_utils and emptied.

    `_lookup_identity` / `_legacy_role` resolve `USER_ROLES_TABLE` at
    call time, so re-pointing the module attribute is enough; monkeypatch
    restores it after every test.
    """
    monkeypatch.setattr(shared, "USER_ROLES_TABLE", UNIT_REGISTRY_TABLE)
    for item in _unit_registry_table.scan().get("Items", []):
        _unit_registry_table.delete_item(
            Key={"user_id": item["user_id"],
                 "usecase_id": item["usecase_id"]})
    return _unit_registry_table


@pytest.fixture
def enforcement_off(shared, monkeypatch):
    """The deployed default (design.md Decision 4)."""
    monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)


@pytest.fixture
def enforcement_on(shared, monkeypatch):
    monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")


# ---------------------------------------------------------------- helpers

def new_sub():
    return str(uuid.uuid4())


def put_row(registry, sub, usecase_id="global", role="Viewer", status=None,
            **extra):
    item = {"user_id": sub, "usecase_id": usecase_id, "role": role}
    if status is not None:
        item["status"] = status
    item.update(extra)
    registry.put_item(Item=item)
    return item


def user_info(sub, claim="Viewer", username="unit-user",
              email="unit-user@example.com"):
    """A `get_user_from_event` dict whose `role` is the Claimed_Role."""
    info = {"user_id": sub, "username": username, "email": email}
    if claim is not None:
        info["role"] = claim
    return info


def request_event(sub, claim="Viewer", username="unit-user",
                  email="unit-user@example.com",
                  source_ip="12.148.187.67", user_agent="aws-cli/1.36.4"):
    """An API Gateway proxy event with Cognito authorizer claims."""
    claims = {"sub": sub}
    if username is not None:
        claims["cognito:username"] = username
    if email is not None:
        claims["email"] = email
    if claim is not None:
        claims["custom:role"] = claim

    identity = {}
    if source_ip is not None:
        identity["sourceIp"] = source_ip
    if user_agent is not None:
        identity["userAgent"] = user_agent

    return {
        "httpMethod": "POST",
        "path": "/builds",
        "requestContext": {"authorizer": {"claims": claims},
                           "identity": identity},
    }


class _FailingTable:
    """A registry table handle whose every call raises — "DynamoDB is
    down" (design.md Decision 3)."""

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
    """DynamoDB resource proxy failing ONLY the registry table, so an
    audit write on the failure path still lands."""

    def __init__(self, real, registry_table_name, error):
        self._real = real
        self._registry_table_name = registry_table_name
        self._error = error

    def Table(self, name):  # noqa: N802 - boto3 resource API
        if name == self._registry_table_name:
            return _FailingTable(self._error)
        return self._real.Table(name)


@pytest.fixture
def break_registry(shared, monkeypatch):
    """Make the registry unreadable with a chosen exception."""
    def _break(error=None):
        error = error or ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "throttled"}},
            "GetItem")
        monkeypatch.setattr(
            shared, "dynamodb",
            _RegistryFailureResource(shared.dynamodb,
                                     shared.USER_ROLES_TABLE, error))
        return error
    return _break


# ===========================================================================
# The enforcement switch (Requirement 2.4)
# ===========================================================================

class TestRegistryEnforcementFlag:
    """One explicit configuration value, defaulting off."""

    def test_absent_variable_means_enforcement_off(self, shared, monkeypatch):
        monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
        assert shared.registry_enforcement_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes",
                                       "Yes", "on", "ON", "enabled",
                                       "  true  ", "\tenabled\n"])
    def test_truthy_spellings_enable_enforcement(self, shared, monkeypatch,
                                                 value):
        monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, value)
        assert shared.registry_enforcement_enabled() is True

    @pytest.mark.parametrize("value", ["", " ", "0", "false", "FALSE", "no",
                                       "off", "disabled", "maybe", "2",
                                       "truthy", "enable"])
    def test_anything_else_leaves_enforcement_off(self, shared, monkeypatch,
                                                  value):
        """Fail *safe* on an unrecognized value: the flag turning itself on
        by accident locks the portal out (design.md Decision 4)."""
        monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, value)
        assert shared.registry_enforcement_enabled() is False

    def test_read_per_call_not_cached_at_import(self, shared, monkeypatch):
        monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
        assert shared.registry_enforcement_enabled() is True
        monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, "false")
        assert shared.registry_enforcement_enabled() is False


# ===========================================================================
# `_identity_is_enabled` / `_identity_role`
# ===========================================================================

class TestIdentityRowHelpers:
    """The two predicates `_lookup_identity` and role resolution share."""

    @pytest.mark.parametrize("row,expected", [
        (None, False),
        ({}, False),
        ({"role": "Viewer"}, True),                    # pre-spec row
        ({"role": "Viewer", "status": "enabled"}, True),
        ({"role": "Viewer", "status": "ENABLED"}, True),
        ({"role": "Viewer", "status": " Enabled "}, True),
        ({"role": "Viewer", "status": "disabled"}, False),
        ({"role": "Viewer", "status": ""}, False),
        ({"role": "Viewer", "status": "suspended"}, False),
    ])
    def test_status_decides_enabled(self, shared, row, expected):
        assert shared._identity_is_enabled(row) is expected

    @pytest.mark.parametrize("role_value,expected", [
        ("Viewer", "Viewer"),
        ("PortalAdmin", "PortalAdmin"),
        ("DataLabeler", "DataLabeler"),
    ])
    def test_valid_role_values_resolve(self, shared, role_value, expected):
        assert shared._identity_role({"role": role_value}) == \
            shared.Role(expected)

    @pytest.mark.parametrize("role_value", [None, "", "viewer", "PORTALADMIN",
                                            "PortalAdmin ", "Administrator",
                                            "Guest"])
    def test_unusable_role_values_grant_nothing(self, shared, role_value):
        """Corrupt registry data must not grant anything."""
        assert shared._identity_role({"role": role_value}) is None
        assert shared._identity_role(None) is None


# ===========================================================================
# `_lookup_identity` branches (design.md Expected Behavior)
# ===========================================================================

class TestLookupIdentityBranches:

    # ------------------------------------------------- unidentifiable caller
    @pytest.mark.parametrize("user_id", [None, "", "unknown"])
    def test_unidentifiable_caller_resolves_nothing_without_a_read(
            self, shared, registry, break_registry, user_id):
        """An unidentifiable caller holds no Portal_Identity, and that is
        not an availability failure — proven by making every registry read
        raise: reaching DynamoDB at all would surface as
        RegistryUnavailable."""
        break_registry()
        assert shared._lookup_identity(user_id, "global") is None

    # ----------------------------------------------------- global row branch
    def test_no_rows_at_all_resolves_nothing(self, shared, registry):
        assert shared._lookup_identity(new_sub(), "global") is None

    def test_enabled_global_row_is_returned(self, shared, registry):
        sub = new_sub()
        put_row(registry, sub, role="DataScientist", status="enabled")
        identity = shared._lookup_identity(sub, "global")
        assert identity is not None
        assert identity["usecase_id"] == "global"
        assert identity["role"] == "DataScientist"

    def test_global_row_without_status_counts_as_enabled(self, shared,
                                                        registry):
        """Rows written before this spec (Team Management grants) carry no
        `status`."""
        sub = new_sub()
        put_row(registry, sub, role="Operator")
        assert shared._lookup_identity(sub, "global")["role"] == "Operator"

    @pytest.mark.parametrize("status", ["disabled", "DISABLED", "", "revoked"])
    def test_disabled_global_row_resolves_nothing(self, shared, registry,
                                                 status):
        """Requirement 1.6: denied exactly as an absent entry is."""
        sub = new_sub()
        put_row(registry, sub, role="PortalAdmin", status=status)
        assert shared._lookup_identity(sub, "global") is None

    def test_global_row_naming_an_unknown_role_is_still_the_deciding_row(
            self, shared, registry):
        """The lookup returns the row; granting nothing for an unusable
        role is `_identity_role`'s job, which is what lets the audit log
        distinguish "a row decided this" from "no row exists"."""
        sub = new_sub()
        put_row(registry, sub, role="Administrator", status="enabled")
        identity = shared._lookup_identity(sub, "global")
        assert identity is not None and identity["role"] == "Administrator"
        assert shared._identity_role(identity) is None

    # ---------------------------------------------------- Use_Case precedence
    def test_usecase_row_overrides_the_global_row_at_that_scope(
            self, shared, registry):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        put_row(registry, sub, usecase_id="uc-1", role="UseCaseAdmin",
                status="enabled")
        identity = shared._lookup_identity(sub, "uc-1")
        assert identity["usecase_id"] == "uc-1"
        assert identity["role"] == "UseCaseAdmin"

    def test_usecase_row_does_not_leak_to_the_global_scope(self, shared,
                                                          registry):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        put_row(registry, sub, usecase_id="uc-1", role="PortalAdmin",
                status="enabled")
        identity = shared._lookup_identity(sub, "global")
        assert identity["usecase_id"] == "global"
        assert identity["role"] == "Viewer"

    def test_usecase_row_does_not_leak_to_another_usecase(self, shared,
                                                         registry):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        put_row(registry, sub, usecase_id="uc-1", role="PortalAdmin",
                status="enabled")
        assert shared._lookup_identity(sub, "uc-2")["role"] == "Viewer"

    def test_disabled_usecase_row_falls_back_to_the_global_row(self, shared,
                                                              registry):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        put_row(registry, sub, usecase_id="uc-1", role="UseCaseAdmin",
                status="disabled")
        identity = shared._lookup_identity(sub, "uc-1")
        assert identity["usecase_id"] == "global"
        assert identity["role"] == "Viewer"

    def test_usecase_row_with_an_unusable_role_falls_back_to_the_global_row(
            self, shared, registry):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        put_row(registry, sub, usecase_id="uc-1", role="Administrator",
                status="enabled")
        assert shared._lookup_identity(sub, "uc-1")["usecase_id"] == "global"

    def test_usecase_row_without_a_global_row_resolves_nothing(self, shared,
                                                              registry):
        """Fail-closed reading settled by task 2.1: the enabled *global*
        row is the provisioning record (design.md's Glossary definition of
        an Unprovisioned_Principal), and the backfill writes one for every
        enabled pool user."""
        sub = new_sub()
        put_row(registry, sub, usecase_id="uc-1", role="UseCaseAdmin",
                status="enabled")
        assert shared._lookup_identity(sub, "uc-1") is None

    def test_disabled_global_row_denies_even_with_an_enabled_usecase_row(
            self, shared, registry):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="disabled")
        put_row(registry, sub, usecase_id="uc-1", role="PortalAdmin",
                status="enabled")
        assert shared._lookup_identity(sub, "uc-1") is None

    # ------------------------------------------------------ failure branches
    def test_client_error_becomes_registry_unavailable(self, shared, registry,
                                                       break_registry):
        error = break_registry()
        with pytest.raises(shared.RegistryUnavailable) as raised:
            shared._lookup_identity(new_sub(), "global")
        assert raised.value.__cause__ is error

    @pytest.mark.parametrize("error", [
        RuntimeError("boom"),
        ConnectionError("connection reset"),
        TimeoutError("timed out"),
    ])
    def test_any_other_exception_becomes_registry_unavailable(
            self, shared, registry, break_registry, error):
        """Fail closed and *loud*: never swallowed into a Viewer
        downgrade (Requirement 1.5)."""
        break_registry(error)
        with pytest.raises(shared.RegistryUnavailable) as raised:
            shared._lookup_identity(new_sub(), "uc-1")
        assert raised.value.__cause__ is error


# ===========================================================================
# `get_user_role` — the flag's two modes on the same registry state
# ===========================================================================

class TestGetUserRoleUnderEnforcement:
    """design.md Expected Behavior: the registry is the only input."""

    def test_unprovisioned_principal_resolves_no_role(
            self, shared, registry, enforcement_on):
        """The incident's principal: a PortalAdmin claim and no row."""
        sub = new_sub()
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "PortalAdmin")) is None

    def test_claim_cannot_raise_the_registry_role(self, shared, registry,
                                                  enforcement_on):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "PortalAdmin")) == shared.Role.VIEWER

    def test_registry_role_holds_without_any_claim(self, shared, registry,
                                                   enforcement_on):
        sub = new_sub()
        put_row(registry, sub, role="DataScientist", status="enabled")
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, claim=None)) == \
            shared.Role.DATA_SCIENTIST

    def test_usecase_row_precedence_is_preserved(self, shared, registry,
                                                 enforcement_on):
        sub = new_sub()
        put_row(registry, sub, role="Viewer", status="enabled")
        put_row(registry, sub, usecase_id="uc-1", role="UseCaseAdmin",
                status="enabled")
        assert shared.rbac_manager.get_user_role(
            sub, "uc-1", user_info(sub)) == shared.Role.USECASE_ADMIN

    def test_disabled_row_resolves_no_role(self, shared, registry,
                                           enforcement_on):
        sub = new_sub()
        put_row(registry, sub, role="PortalAdmin", status="disabled")
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "PortalAdmin")) is None

    def test_row_naming_an_unusable_role_resolves_no_role(
            self, shared, registry, enforcement_on):
        sub = new_sub()
        put_row(registry, sub, role="Administrator", status="enabled")
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "Viewer")) is None

    def test_no_role_means_no_permissions_and_no_portal_admin(
            self, shared, registry, enforcement_on):
        sub = new_sub()
        info = user_info(sub, "PortalAdmin")
        assert shared.rbac_manager.get_user_permissions(
            sub, "global", info) == set()
        assert shared.rbac_manager.has_permission(
            sub, "global", shared.Permission.VIEW_USECASES, info) is False
        assert shared.rbac_manager.is_portal_admin(sub, info) is False

    def test_registry_unavailable_escapes_instead_of_downgrading(
            self, shared, registry, enforcement_on, break_registry):
        """Requirement 1.5: an outage is a 500, never a Viewer and never a
        403 — so it must reach the decorator as an exception."""
        break_registry()
        sub = new_sub()
        info = user_info(sub, "PortalAdmin")
        with pytest.raises(shared.RegistryUnavailable):
            shared.rbac_manager.get_user_role(sub, "global", info)
        with pytest.raises(shared.RegistryUnavailable):
            shared.rbac_manager.get_user_permissions(sub, "global", info)
        with pytest.raises(shared.RegistryUnavailable):
            shared.rbac_manager.has_permission(
                sub, "global", shared.Permission.VIEW_USECASES, info)


class TestGetUserRoleLegacyMode:
    """Enforcement off is the deployed default until the backfill has run,
    and must be byte-for-byte today's behaviour — including the bug, which
    task 5 flips. These assertions are expected to be *inverted* by that
    task; they exist so the flag-off tree is provably unchanged now.
    """

    def test_claimed_portal_admin_still_grants_without_any_row(
            self, shared, registry, enforcement_off):
        sub = new_sub()
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "PortalAdmin")) == \
            shared.Role.PORTAL_ADMIN

    def test_claimed_role_still_falls_through_without_any_row(
            self, shared, registry, enforcement_off):
        sub = new_sub()
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "DataScientist")) == \
            shared.Role.DATA_SCIENTIST

    def test_no_claim_and_no_row_still_defaults_to_viewer(
            self, shared, registry, enforcement_off):
        sub = new_sub()
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, claim=None)) == shared.Role.VIEWER

    def test_invalid_claim_still_defaults_to_viewer(self, shared, registry,
                                                   enforcement_off):
        sub = new_sub()
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "Administrator")) == \
            shared.Role.VIEWER

    def test_usecase_row_still_takes_precedence_over_the_claim(
            self, shared, registry, enforcement_off):
        sub = new_sub()
        put_row(registry, sub, usecase_id="uc-1", role="UseCaseAdmin")
        assert shared.rbac_manager.get_user_role(
            sub, "uc-1", user_info(sub, "Viewer")) == shared.Role.USECASE_ADMIN

    def test_registry_failure_is_still_swallowed_to_viewer(
            self, shared, registry, enforcement_off, break_registry):
        """The pre-fix conflation (design.md Decision 3) survives in legacy
        mode on purpose: turning it into a 500 before the backfill would
        change deployed behaviour."""
        break_registry()
        sub = new_sub()
        assert shared.rbac_manager.get_user_role(
            sub, "global", user_info(sub, "Viewer")) == shared.Role.VIEWER

    def test_the_flag_is_the_only_difference_on_one_registry_state(
            self, shared, registry, monkeypatch):
        """Same rows, same claim, opposite answers — which is exactly what
        task 5.2's flip changes and nothing else."""
        sub = new_sub()
        info = user_info(sub, "PortalAdmin")

        monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
        assert shared.rbac_manager.get_user_role(sub, "global", info) == \
            shared.Role.PORTAL_ADMIN

        monkeypatch.setenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, "true")
        assert shared.rbac_manager.get_user_role(sub, "global", info) is None


# ===========================================================================
# The `would_deny` dry-run WARNING (Requirement 2.4)
# ===========================================================================

def would_deny_lines(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno >= logging.WARNING
            and "would_deny" in record.getMessage()]


class TestWouldDenyWarning:
    """Task 5.1 reads these lines from deployed logs to decide whether the
    backfill is complete, so their content is load-bearing."""

    def test_unprovisioned_request_is_named_in_a_warning(
            self, shared, registry, enforcement_off, caplog):
        sub = new_sub()
        with caplog.at_level(logging.WARNING):
            role = shared.rbac_manager.get_user_role(
                sub, "uc-7",
                user_info(sub, "PortalAdmin", username="cli-created"))

        # The decision itself is untouched by the dry run.
        assert role == shared.Role.PORTAL_ADMIN
        lines = would_deny_lines(caplog)
        assert len(lines) == 1, lines
        message = lines[0]
        # Everything an operator needs to backfill the principal.
        assert sub in message
        assert "uc-7" in message
        assert "PortalAdmin" in message
        assert "cli-created" in message
        assert "absent" in message

    def test_provisioned_request_logs_nothing(self, shared, registry,
                                              enforcement_off, caplog):
        sub = new_sub()
        put_row(registry, sub, role="DataScientist", status="enabled")
        with caplog.at_level(logging.WARNING):
            shared.rbac_manager.get_user_role(sub, "global",
                                              user_info(sub, "DataScientist"))
        assert would_deny_lines(caplog) == []

    def test_disabled_row_is_a_would_deny(self, shared, registry,
                                          enforcement_off, caplog):
        sub = new_sub()
        put_row(registry, sub, role="DataScientist", status="disabled")
        with caplog.at_level(logging.WARNING):
            shared.rbac_manager.get_user_role(sub, "global",
                                              user_info(sub, "DataScientist"))
        assert len(would_deny_lines(caplog)) == 1

    def test_unreadable_registry_is_recorded_as_undetermined(
            self, shared, registry, enforcement_off, break_registry, caplog):
        break_registry()
        sub = new_sub()
        with caplog.at_level(logging.WARNING):
            role = shared.rbac_manager.get_user_role(
                sub, "global", user_info(sub, "Viewer"))
        # The dry run can neither decide nor break the request.
        assert role == shared.Role.VIEWER
        assert any("undetermined" in line for line in would_deny_lines(caplog))

    def test_enforcement_mode_logs_no_dry_run(self, shared, registry,
                                              enforcement_on, caplog):
        """Under enforcement the denial is real and logged as such; a
        `would_deny` line there would be noise."""
        sub = new_sub()
        with caplog.at_level(logging.WARNING):
            assert shared.rbac_manager.get_user_role(
                sub, "global", user_info(sub, "PortalAdmin")) is None
        assert would_deny_lines(caplog) == []


# ===========================================================================
# `attribution_from` (Requirements 4.1, 4.2, 4.3)
# ===========================================================================

class TestAttributionFromClaims:

    def test_full_request_records_every_field_from_the_request(
            self, shared, registry):
        sub = new_sub()
        put_row(registry, sub, role="DataScientist", status="enabled")
        identity = shared.attribution_from(request_event(sub, "DataScientist"))
        assert identity == {
            "username": "unit-user",
            "email": "unit-user@example.com",
            "source_ip": "12.148.187.67",
            "user_agent": "aws-cli/1.36.4",
            "identity_source": "registry",
        }

    def test_exactly_the_five_attribution_fields(self, shared, registry):
        identity = shared.attribution_from(request_event(new_sub()))
        assert set(identity) == set(shared.ATTRIBUTION_FIELDS)
        assert all(isinstance(value, str) and value
                   for value in identity.values())

    def test_absent_registry_row_records_identity_source_absent(
            self, shared, registry):
        assert shared.attribution_from(
            request_event(new_sub(), "PortalAdmin"))["identity_source"] == \
            "absent"

    def test_disabled_row_records_identity_source_absent(self, shared,
                                                         registry):
        sub = new_sub()
        put_row(registry, sub, role="PortalAdmin", status="disabled")
        assert shared.attribution_from(
            request_event(sub))["identity_source"] == "absent"

    def test_usecase_scope_is_honoured_when_given(self, shared, registry):
        """The recorded Identity_Source must describe the row that decided
        *this* scope."""
        sub = new_sub()
        put_row(registry, sub, usecase_id="uc-1", role="UseCaseAdmin",
                status="enabled")
        # A Use_Case row with no global row decides nothing (task 2.1).
        assert shared.attribution_from(
            request_event(sub), usecase_id="uc-1")["identity_source"] == \
            "absent"
        put_row(registry, sub, role="Viewer", status="enabled")
        assert shared.attribution_from(
            request_event(sub), usecase_id="uc-1")["identity_source"] == \
            "registry"

    def test_unreadable_registry_records_identity_source_unknown(
            self, shared, registry, break_registry):
        """An outage decided no role, so neither 'registry' nor 'absent'
        would be a true statement about the request (task 2.2 decision
        (a)); the human fields still come from the request."""
        break_registry()
        sub = new_sub()
        identity = shared.attribution_from(request_event(sub, "PortalAdmin"))
        assert identity["identity_source"] == "unknown"
        assert identity["username"] == "unit-user"
        assert identity["source_ip"] == "12.148.187.67"

    def test_missing_request_context_identity_records_unknown_ip_and_agent(
            self, shared, registry):
        event = request_event(new_sub(), source_ip=None, user_agent=None)
        identity = shared.attribution_from(event)
        assert identity["source_ip"] == "unknown"
        assert identity["user_agent"] == "unknown"
        # The human fields are unaffected.
        assert identity["username"] == "unit-user"
        assert identity["email"] == "unit-user@example.com"

    def test_no_claims_at_all_still_writes_all_five_fields(self, shared,
                                                           registry):
        identity = shared.attribution_from({"requestContext": {}})
        assert set(identity) == set(shared.ATTRIBUTION_FIELDS)
        assert identity["username"] == "unknown"
        assert identity["email"] == "unknown"
        assert identity["source_ip"] == "unknown"
        assert identity["user_agent"] == "unknown"

    def test_partial_claims_record_what_the_request_carries(self, shared,
                                                            registry):
        sub = new_sub()
        event = request_event(sub, username="only-a-username", email=None,
                             user_agent=None)
        identity = shared.attribution_from(event)
        assert identity["username"] == "only-a-username"
        assert identity["email"] == "unknown"
        assert identity["source_ip"] == "12.148.187.67"
        assert identity["user_agent"] == "unknown"

    def test_bootstrap_admin_without_an_email_claim_records_unknown_email(
            self, shared, registry):
        """`get_user_from_event` substitutes the username (and then the
        `sub`) for a missing email so records do not read "Created By:
        unknown" — attribution must not repeat that substitution as if it
        were an email address (task 2.2 decision (b))."""
        sub = new_sub()
        event = request_event(sub, username="admin", email=None)
        user = shared.get_user_from_event(event)
        assert user["email"] == "admin"          # the display fallback
        identity = shared.attribution_from(event, user)
        assert identity["username"] == "admin"
        assert identity["email"] == "unknown"    # never fabricated

    def test_literal_unknown_claims_are_not_treated_as_identities(
            self, shared, registry):
        sub = new_sub()
        event = request_event(sub, username="unknown", email="unknown",
                             source_ip="unknown", user_agent="  ")
        identity = shared.attribution_from(event)
        assert identity == {"username": "unknown", "email": "unknown",
                            "source_ip": "unknown", "user_agent": "unknown",
                            "identity_source": "absent"}

    def test_lambda_authorizer_context_event_is_read_too(self, shared,
                                                         registry):
        """The non-Cognito authorizer shape `get_user_from_event` also
        supports (context values instead of claims)."""
        sub = new_sub()
        event = {
            "requestContext": {
                "authorizer": {"userId": sub, "username": "ctx-user",
                               "email": "ctx-user@example.com",
                               "role": "Operator"},
                "identity": {"sourceIp": "198.51.100.9",
                             "userAgent": "portal-frontend/2"},
            },
        }
        identity = shared.attribution_from(event)
        assert identity["username"] == "ctx-user"
        assert identity["email"] == "ctx-user@example.com"
        assert identity["source_ip"] == "198.51.100.9"
        assert identity["user_agent"] == "portal-frontend/2"

    def test_explicit_identity_source_is_recorded_without_a_registry_read(
            self, shared, registry, break_registry):
        """A caller that has already resolved the decision passes it in;
        proven not to read the registry by making every read raise."""
        break_registry()
        identity = shared.attribution_from(
            request_event(new_sub()), identity_source="registry")
        assert identity["identity_source"] == "registry"

    def test_no_event_at_all_records_unknown_without_raising(self, shared,
                                                            registry):
        identity = shared.attribution_from(None)
        assert set(identity) == set(shared.ATTRIBUTION_FIELDS)
        assert identity["username"] == "unknown"
        assert identity["email"] == "unknown"
        assert identity["source_ip"] == "unknown"
        assert identity["user_agent"] == "unknown"

    @pytest.mark.parametrize("event", ["not-an-event", 42, ["requestContext"]])
    def test_a_malformed_event_never_raises(self, shared, registry, event):
        """Losing attribution must not fail a request (Requirement 4.3)."""
        identity = shared.attribution_from(event)
        assert identity == {field: "unknown"
                            for field in shared.ATTRIBUTION_FIELDS}
