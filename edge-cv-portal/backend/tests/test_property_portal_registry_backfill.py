"""
Property test for the Portal_Identity registry backfill —
portal-jwt-role-privilege-escalation task 3.2.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

**Feature: portal-jwt-role-privilege-escalation, Property 4: Backfill is
idempotent and preserves current access.**

_For any_ generated pool state, one backfill run then a second produces
the same rows, never overwrites an existing row, skips disabled users, and
every user who was authorized for a permission before enforcement is
authorized after.

_Validates: Requirements 2.1, 2.2, 2.3_

Why this is the property that matters: the backfill
(`edge-cv-portal/backfill_portal_registry.py`, task 3.1) is what makes
task 5's flag flip survivable. If it under-provisions, enabling
enforcement locks real users — including the bootstrap `admin` — out of
the portal (Requirement 2.3). If it over-provisions, it hands an account
privilege the pool never gave it, which is the very escalation this spec
closes. If it is not idempotent, an operator cannot re-run it after
reviewing the plan, and a second run could overwrite a role the portal's
own User Manager wrote (Requirement 2.2). All three directions are
asserted here.

What is real
------------
Everything but the wrapper that removes a `sub`:

* the **pool** is a real moto `cognito-idp` user pool created per
  example, with `admin_create_user` / `admin_disable_user` and real
  `list_users` pagination (the generated `page_size` forces multiple
  pages), so `iter_pool_users` is exercised against the API it will meet;
* the **registry** is a moto DynamoDB table with the production key
  schema, private to this module (see "Isolation" below), and the
  backfill's conditional `put_item` runs against it unmodified;
* the **role resolution** on both sides of the flip is the real
  `RBACManager` — legacy mode before the backfill, enforced mode after —
  so "preserves current access" is a measurement of production code in
  both configurations, not a restatement of the oracle.

A real Cognito account always carries `sub`. `_SubStrippingPool` removes
it from designated accounts so the backfill's defensive `skip-no-sub`
classification is exercised: a row keyed on an invented id would look
like provisioning while deciding nothing.

The oracles (`expected_backfill_role`, `expected_enforced_role`,
`legacy_global_role`) are written out by hand from Requirement 2.1 and
design.md's Expected Behavior table; none of them calls the code under
test. `expected_backfill_role` matches role names **exactly**, because
`Role(claim)` does — a padded `' PortalAdmin'` account is a Viewer today,
so backfilling `PortalAdmin` for it would be an escalation. That oracle
found exactly that bug in task 3.1's `role_for` (which stripped before
matching); the production fix landed with this test.

Deliberate divergences from "access is preserved", each asserted
positively rather than skipped:

* an account with an **enabled** global row keeps that row and resolves
  **its** role, even when the token claims a higher one — the registry
  wins by design (Requirements 1.2, 1.4), so a claim-granted privilege
  the row does not name is dropped;
* an account whose global row is **disabled** is denied, exactly as an
  absent row is (Requirement 1.6) — that is the portal having disabled
  the account, and Requirement 3.4 wants it to stop working;
* a **disabled** Cognito account gets no row (Decision 5) and is denied;
  it cannot obtain a token anyway.

Isolation: the registry lives in `test-portal-registry-backfill-roles`,
pointed at by re-binding `shared_utils.USER_ROLES_TABLE` (which
production resolves at call time) for this module only. The session-scoped
`test-user-roles` table other suites seed is therefore never read or
written here, in either direction. `PORTAL_REGISTRY_ENFORCED` is likewise
flipped only inside a context manager, per example, and restored — the
deployed default stays off (design.md Decision 4).

Run from `edge-cv-portal/backend` with
`~/.venvs/dda-portal-tests/bin/python -m pytest <this file> -q
-p no:cacheprovider`.
"""
import importlib.util
import json
import os
import uuid
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"

# A registry table private to this module (see the docstring).
BACKFILL_REGISTRY_TABLE = "test-portal-registry-backfill-roles"

_HERE = os.path.dirname(os.path.abspath(__file__))
# edge-cv-portal/ — the operator script lives beside the portal, not in
# the Lambda tree, so it is loaded by path rather than imported.
_PORTAL_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
BACKFILL_SCRIPT = os.path.join(_PORTAL_ROOT, "backfill_portal_registry.py")

GLOBAL_SCOPE = "global"

# The roles the portal recognizes (shared_utils.Role values); pinned
# against the enum by test_backfill_roles_match_the_role_enum below.
VALID_ROLE_NAMES = ("Viewer", "Operator", "DataScientist", "UseCaseAdmin",
                    "PortalAdmin", "DataLabeler")

# Generated `custom:role` attribute values. The padded and case-varied
# spellings matter: they are NOT roles (`Role(claim)` raises), so the
# accounts carrying them are Viewers today and must be backfilled as
# Viewers.
INVALID_ROLE_CLAIMS = (
    " PortalAdmin", "PortalAdmin ", "portaladmin", "PORTALADMIN",
    "Portal Admin", "Admin", "SuperUser", "root", "*", "",
    "PortalAdmin,DataScientist", '{"role": "PortalAdmin"}', "null",
)

# The account names a generated pool draws from (a small set, so a pool
# is a handful of accounts and examples stay fast).
ACCOUNT_NAMES = ("acct-alpha", "acct-beta", "acct-gamma", "acct-delta",
                 "acct-epsilon")

# What already exists in the registry for a generated account, before the
# backfill runs.
#
#   none          — the shape the backfill exists for
#   global_same   — a row naming the same role the backfill would write
#   global_lower  — a row naming Viewer (a deliberate downgrade when the
#                   claim names more: Requirement 1.4)
#   global_higher — a row naming PortalAdmin (the portal granted more
#                   than the token claims)
#   global_disabled — a disabled row: denies like an absent one (1.6)
#   usecase_only  — a Team Management per-Use_Case grant and no global
#                   row: the backfill must add the global row and leave
#                   the grant alone
PREEXISTING_SHAPES = ("none", "global_same", "global_lower", "global_higher",
                      "global_disabled", "usecase_only")

# `assigned_by` on rows this test seeds, so a row the backfill overwrote
# is detectable by more than its role.
SEEDED_BY = "seeded-by-test"


# ------------------------------------------------------------- strategies

def claim_values():
    """A generated `custom:role`: valid, invalid, empty, or absent."""
    return st.one_of(
        st.sampled_from(VALID_ROLE_NAMES),
        st.sampled_from(INVALID_ROLE_CLAIMS),
        st.none(),
    )


def account_specs():
    """One generated pool account.

    `enabled` and `has_sub` are weighted towards the ordinary case so an
    example still exercises the provisioning path when it also generates
    a skipped account.
    """
    return st.fixed_dictionaries({
        "username": st.sampled_from(ACCOUNT_NAMES),
        "claim": claim_values(),
        "enabled": st.sampled_from((True, True, True, False)),
        "has_sub": st.sampled_from((True, True, True, True, False)),
        "has_email": st.sampled_from((True, True, True, False)),
        "preexisting": st.sampled_from(PREEXISTING_SHAPES),
    })


def pool_states():
    return st.lists(account_specs(), min_size=1, max_size=4,
                    unique_by=lambda spec: spec["username"])


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
def backfill(aws_stack):
    """The operator script, loaded from its path.

    It builds no AWS clients at import time (every client is either
    injected or created inside `run_backfill`), so loading it here is
    safe; the clients this module injects are moto-backed.
    """
    assert os.path.exists(BACKFILL_SCRIPT), BACKFILL_SCRIPT
    spec = importlib.util.spec_from_file_location(
        "backfill_portal_registry", BACKFILL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry(shared, aws_stack):
    """This module's private registry table, wired into shared_utils.

    `_lookup_identity` / `_legacy_role` resolve `USER_ROLES_TABLE` at
    call time, so re-binding the module attribute is enough. Restored on
    teardown so the rest of the session keeps the conftest table.
    """
    import boto3

    ddb = boto3.client("dynamodb", region_name=REGION)
    if BACKFILL_REGISTRY_TABLE not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=BACKFILL_REGISTRY_TABLE,
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

    previous = shared.USER_ROLES_TABLE
    shared.USER_ROLES_TABLE = BACKFILL_REGISTRY_TABLE
    yield boto3.resource("dynamodb", region_name=REGION).Table(
        BACKFILL_REGISTRY_TABLE)
    shared.USER_ROLES_TABLE = previous


@pytest.fixture(scope="module")
def cognito(aws_stack):
    """A moto cognito-idp client (the mock is the session fixture's)."""
    import boto3
    return boto3.client("cognito-idp", region_name=REGION)


@pytest.fixture(scope="module")
def dynamodb_resource(aws_stack):
    import boto3
    return boto3.resource("dynamodb", region_name=REGION)


@contextmanager
def enforcement(shared, enabled):
    """Flip PORTAL_REGISTRY_ENFORCED for the duration.

    Restored inside the generated example rather than by the
    `monkeypatch` fixture: a function-scoped fixture is set up once for
    the whole Hypothesis run, so an unrestored flip would leak into the
    following examples and into the rest of the session.
    """
    variable = shared.PORTAL_REGISTRY_ENFORCED_ENV
    previous = os.environ.get(variable)
    if enabled:
        os.environ[variable] = "true"
    else:
        os.environ.pop(variable, None)
    assert shared.registry_enforcement_enabled() is enabled
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


# ------------------------------------------------------------- fake sub-less

class _SubStrippingPool:
    """A cognito-idp client whose `list_users` omits `sub` for named
    accounts.

    A real pool always reports `sub`; this exists only to exercise the
    backfill's `skip-no-sub` branch, which is what keeps it from writing
    a row under an id it invented. Every other call is the real moto
    client's.
    """

    def __init__(self, client, usernames_without_sub):
        self._client = client
        self._strip = set(usernames_without_sub)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def list_users(self, **kwargs):
        response = self._client.list_users(**kwargs)
        for user in response.get("Users", []):
            if user.get("Username") in self._strip:
                user["Attributes"] = [
                    attribute for attribute in user.get("Attributes", [])
                    if attribute.get("Name") != "sub"]
        return response


# ---------------------------------------------------------------- oracles

def email_of(spec):
    """The email attribute the account carries, or None."""
    if not spec["has_email"]:
        return None
    return f"{spec['username']}@example.com"


def expected_backfill_role(claim):
    """The role a backfilled row must name (Requirement 2.1).

    The claim when it *is* a role, otherwise Viewer. The match is exact:
    `Role(claim)` is what decides a role today, so a padded or
    case-varied spelling is not a role and its account is a Viewer.
    """
    if claim is not None and claim in VALID_ROLE_NAMES:
        return claim
    return "Viewer"


def legacy_global_role(claim):
    """The account's effective role at the `global` scope **before**
    enforcement, for an account with no registry row.

    Hand-written from `shared_utils._legacy_role`: step 1 grants
    PortalAdmin from the claim, step 2 and 3 find nothing, step 4 does
    `Role(claim)`, and step 5 falls back to Viewer. Equal to
    `expected_backfill_role` by construction — that equality *is*
    Requirement 2.3 for the accounts the backfill provisions.
    """
    if claim == "PortalAdmin":
        return "PortalAdmin"
    if claim is not None and claim in VALID_ROLE_NAMES:
        return claim
    return "Viewer"


def claimed_role_of(spec):
    """What `get_user_from_event` reports as the Claimed_Role: an absent
    `custom:role` becomes the literal 'Viewer'."""
    return "Viewer" if spec["claim"] is None else spec["claim"]


def seeded_rows(spec, sub):
    """The registry rows this test seeds for a generated account."""
    if sub is None or spec["preexisting"] == "none":
        return []
    shape = spec["preexisting"]
    if shape == "usecase_only":
        return [{"usecase_id": usecase_scope(sub), "role": "Operator",
                 "status": "enabled"}]
    role = {"global_same": expected_backfill_role(spec["claim"]),
            "global_lower": "Viewer",
            "global_higher": "PortalAdmin",
            "global_disabled": "PortalAdmin"}[shape]
    status = "disabled" if shape == "global_disabled" else "enabled"
    return [{"usecase_id": GLOBAL_SCOPE, "role": role, "status": status}]


def usecase_scope(sub):
    """The Use_Case a `usecase_only` account's grant is scoped to."""
    return f"uc-{sub}"


def expected_new_row(spec, sub):
    """The row the backfill must write for this account, or None.

    Requirement 2.1 (one global row per enabled account, carrying its
    current effective role, username and email), Requirement 2.2 (an
    existing row is left alone), Decision 5 (disabled accounts are
    skipped so they stay denied).
    """
    if not spec["enabled"] or sub is None:
        return None
    if spec["preexisting"] in ("global_same", "global_lower",
                              "global_higher", "global_disabled"):
        return None
    return {
        "user_id": sub,
        "usecase_id": GLOBAL_SCOPE,
        "role": expected_backfill_role(spec["claim"]),
        "username": spec["username"],
        "email": email_of(spec) or "",
        "status": "enabled",
        "assigned_by": "backfill",
    }


def expected_action(spec, sub):
    """The plan action the backfill must report for this account."""
    if not spec["enabled"]:
        return "skip-disabled"
    if sub is None:
        return "skip-no-sub"
    if spec["preexisting"] in ("global_same", "global_lower",
                              "global_higher", "global_disabled"):
        return "exists"
    return "create"


def expected_enforced_role(spec, sub, scope=GLOBAL_SCOPE):
    """The Effective_Role after the backfill, under enforcement.

    design.md's Expected Behavior table, applied to the post-backfill
    registry: no enabled global row denies; an enabled Use_Case row
    overrides the global row when the request is scoped to it.
    """
    shape = spec["preexisting"]
    if not spec["enabled"] or sub is None:
        # No row was written; a pre-existing row is the only thing that
        # could still grant.
        if shape in ("global_same", "global_lower", "global_higher"):
            return {"global_same": expected_backfill_role(spec["claim"]),
                    "global_lower": "Viewer",
                    "global_higher": "PortalAdmin"}[shape]
        return None

    if shape == "global_disabled":
        return None
    if shape == "global_lower":
        global_role = "Viewer"
    elif shape == "global_higher":
        global_role = "PortalAdmin"
    else:  # 'none', 'global_same', 'usecase_only' -> the backfilled row
        global_role = expected_backfill_role(spec["claim"])

    if (shape == "usecase_only" and scope != GLOBAL_SCOPE
            and scope == usecase_scope(sub)):
        return "Operator"
    return global_role


def provisioned_by_backfill(spec, sub):
    """True when the backfill is what gives this account its role, i.e.
    when Requirement 2.3's "still reaches it" is the backfill's job."""
    return expected_new_row(spec, sub) is not None


# ---------------------------------------------------------------- helpers

def counterexample(**fields):
    return json.dumps(fields, indent=2, default=str)


def create_pool(cognito):
    """A fresh moto user pool with the portal's `custom:role` attribute."""
    return cognito.create_user_pool(
        PoolName=f"backfill-property-{uuid.uuid4()}",
        Schema=[{
            "Name": "role",
            "AttributeDataType": "String",
            "Mutable": True,
            "StringAttributeConstraints": {"MinLength": "0",
                                           "MaxLength": "256"},
        }],
    )["UserPool"]["Id"]


def create_account(cognito, pool_id, spec):
    """Create one generated account in the pool; returns its `sub`."""
    attributes = []
    email = email_of(spec)
    if email:
        attributes.append({"Name": "email", "Value": email})
    if spec["claim"] is not None:
        attributes.append({"Name": "custom:role", "Value": spec["claim"]})

    created = cognito.admin_create_user(
        UserPoolId=pool_id, Username=spec["username"],
        UserAttributes=attributes, MessageAction="SUPPRESS")
    if not spec["enabled"]:
        cognito.admin_disable_user(UserPoolId=pool_id,
                                   Username=spec["username"])
    return {attribute["Name"]: attribute["Value"]
            for attribute in created["User"]["Attributes"]}["sub"]


def seed_registry(registry, spec, sub):
    """Write this account's pre-existing rows; returns them as read back."""
    written = []
    for row in seeded_rows(spec, sub):
        registry.put_item(Item={
            "user_id": sub, "usecase_id": row["usecase_id"],
            "role": row["role"], "status": row["status"],
            "username": spec["username"],
            "email": email_of(spec) or "",
            "assigned_by": SEEDED_BY, "assigned_at": 1,
        })
        written.append(read_row(registry, sub, row["usecase_id"]))
    return written


def read_row(registry, sub, usecase_id):
    return registry.get_item(
        Key={"user_id": sub, "usecase_id": usecase_id}).get("Item")


def rows_of(registry, sub):
    """Every registry row for a `sub`, keyed by `usecase_id`."""
    items = registry.query(
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": sub},
    ).get("Items", [])
    return {item["usecase_id"]: item for item in items}


def snapshot(registry, subs):
    """The registry state for the generated accounts, as plain data."""
    return {sub: rows_of(registry, sub) for sub in subs if sub}


def resolve(shared, sub, spec, scope=GLOBAL_SCOPE):
    """The real RBACManager's answer for this account at a scope."""
    info = {"user_id": sub, "username": spec["username"],
            "email": email_of(spec) or "unknown",
            "role": claimed_role_of(spec)}
    role = shared.rbac_manager.get_user_role(sub, scope, user_info=info)
    permissions = shared.rbac_manager.get_user_permissions(
        sub, scope, user_info=info)
    return role, permissions


def run(backfill, cognito_client, dynamodb_resource, pool_id, page_size,
        apply_changes):
    """One backfill run against the generated pool (output discarded)."""
    return backfill.run_backfill(
        user_pool_id=pool_id,
        table_name=BACKFILL_REGISTRY_TABLE,
        apply_changes=apply_changes,
        cognito=cognito_client,
        dynamodb_resource=dynamodb_resource,
        page_size=page_size,
        out=lambda _line: None,
    )


def entries_by_username(result):
    return {entry.username: entry for entry in result.entries}


# ===========================================================================
# Property 4
# ===========================================================================

class TestProperty4BackfillIsIdempotentAndPreservesAccess:
    """**Feature: portal-jwt-role-privilege-escalation, Property 4:
    Backfill is idempotent and preserves current access.**

    _For any_ generated pool state, one backfill run then a second
    produces the same rows, never overwrites an existing row, skips
    disabled users, and every user who was authorized for a permission
    before enforcement is authorized after.

    See the module docstring for what is real, for the hand-written
    oracles, and for the three deliberate divergences from
    "access is preserved" (each asserted positively).

    _Validates: Requirements 2.1, 2.2, 2.3_
    """

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(pool=pool_states(), page_size=st.integers(min_value=1,
                                                     max_value=4))
    # The bootstrap `admin`'s own shape: an enabled PortalAdmin with no
    # registry row at all — the account Requirement 2.3 must not lock out.
    @example(pool=[{"username": "acct-alpha", "claim": "PortalAdmin",
                    "enabled": True, "has_sub": True, "has_email": False,
                    "preexisting": "none"}],
             page_size=1)
    # An account whose `custom:role` is padded: NOT a role today (its
    # effective role is Viewer), so backfilling PortalAdmin would hand it
    # privilege it never had. This is the case that found the `.strip()`
    # escalation in task 3.1's role_for.
    @example(pool=[{"username": "acct-alpha", "claim": " PortalAdmin",
                    "enabled": True, "has_sub": True, "has_email": True,
                    "preexisting": "none"}],
             page_size=2)
    # A disabled account with no row: skipped, stays denied (Decision 5).
    @example(pool=[{"username": "acct-beta", "claim": "PortalAdmin",
                    "enabled": False, "has_sub": True, "has_email": True,
                    "preexisting": "none"}],
             page_size=1)
    # An existing row the backfill must not touch, naming less than the
    # claim: the registry wins (Requirements 1.4, 2.2).
    @example(pool=[{"username": "acct-gamma", "claim": "PortalAdmin",
                    "enabled": True, "has_sub": True, "has_email": True,
                    "preexisting": "global_lower"}],
             page_size=1)
    # A Team Management per-Use_Case grant with no global row: the global
    # row is added, the grant is left alone and still wins at its scope.
    @example(pool=[{"username": "acct-delta", "claim": "DataScientist",
                    "enabled": True, "has_sub": True, "has_email": True,
                    "preexisting": "usecase_only"}],
             page_size=1)
    # A mixed pool bigger than one page, including an account with no
    # `sub` and a disabled row that must keep denying.
    @example(pool=[{"username": "acct-alpha", "claim": "UseCaseAdmin",
                    "enabled": True, "has_sub": True, "has_email": True,
                    "preexisting": "none"},
                   {"username": "acct-beta", "claim": None,
                    "enabled": True, "has_sub": True, "has_email": False,
                    "preexisting": "global_disabled"},
                   {"username": "acct-gamma", "claim": "Operator",
                    "enabled": True, "has_sub": False, "has_email": True,
                    "preexisting": "none"},
                   {"username": "acct-delta", "claim": "DataLabeler",
                    "enabled": False, "has_sub": True, "has_email": True,
                    "preexisting": "global_same"}],
             page_size=2)
    def test_backfill_is_idempotent_and_preserves_access(
            self, shared, backfill, registry, cognito, dynamodb_resource,
            pool, page_size):
        pool_id = create_pool(cognito)
        subs = {}
        for spec in pool:
            sub = create_account(cognito, pool_id, spec)
            # `has_sub` False models an account whose record reports no
            # `sub`; the real one is kept so its rows can be inspected.
            subs[spec["username"]] = sub
        specs = {spec["username"]: spec for spec in pool}
        visible_sub = {name: (subs[name] if specs[name]["has_sub"] else None)
                       for name in subs}
        client = _SubStrippingPool(
            cognito, [name for name in subs if not specs[name]["has_sub"]])

        seeded = {}
        for name, spec in specs.items():
            seeded[name] = seed_registry(registry, spec, subs[name])
        before_rows = snapshot(registry, subs.values())

        # ------------------------------------------------ before the flip
        # The access each account has today, measured with the real
        # legacy resolution (enforcement off, the deployed default).
        with enforcement(shared, False):
            before = {name: resolve(shared, subs[name], specs[name])
                      for name in specs}

        for name, spec in specs.items():
            if not provisioned_by_backfill(spec, visible_sub[name]):
                continue
            role, _permissions = before[name]
            assert role == shared.Role(legacy_global_role(spec["claim"])), \
                counterexample(
                    problem="the legacy oracle disagrees with the legacy "
                            "implementation, so 'preserves access' would "
                            "be measured against the wrong baseline",
                    account=spec, measured=str(role),
                    oracle=legacy_global_role(spec["claim"]))

        # -------------------------------------------------- the dry run
        dry = run(backfill, client, dynamodb_resource, pool_id, page_size,
                  apply_changes=False)
        assert dry.applied is False
        assert snapshot(registry, subs.values()) == before_rows, \
            counterexample(problem="the dry run wrote to the registry",
                           pool=pool)
        assert dry.scanned == len(pool), counterexample(
            problem="not every pool account was scanned (pagination)",
            page_size=page_size, scanned=dry.scanned, pool=pool)
        for name, spec in specs.items():
            planned = entries_by_username(dry)[name]
            assert planned.action == expected_action(spec,
                                                    visible_sub[name]), \
                counterexample(account=spec, planned=planned.action,
                               expected=expected_action(spec,
                                                        visible_sub[name]))

        # --------------------------------------------------- first apply
        first = run(backfill, client, dynamodb_resource, pool_id,
                    page_size, apply_changes=True)
        assert first.errors == 0, counterexample(
            problem="the backfill failed on an account",
            details=[(entry.username, entry.detail)
                     for entry in first.by_action("error")])
        assert (first.created + first.unchanged + first.skipped_disabled
                + first.skipped_no_sub + first.errors) == first.scanned
        assert first.created == sum(
            1 for name, spec in specs.items()
            if expected_new_row(spec, visible_sub[name]) is not None)

        after_rows = snapshot(registry, subs.values())

        for name, spec in specs.items():
            sub = subs[name]
            rows = after_rows.get(sub, {})

            # 1. Existing rows are untouched, attribute for attribute
            #    (Requirement 2.2) — including a per-Use_Case grant.
            for existing in seeded[name]:
                usecase_id = existing["usecase_id"]
                assert rows.get(usecase_id) == existing, counterexample(
                    problem="an existing registry row was modified",
                    account=spec, before=existing,
                    after=rows.get(usecase_id))

            # 2. The row the backfill owes this account, and no other.
            expected = expected_new_row(spec, visible_sub[name])
            if expected is None:
                seeded_scopes = {row["usecase_id"] for row in seeded[name]}
                assert set(rows) == seeded_scopes, counterexample(
                    problem=("a row was written for an account the "
                             "backfill must skip or leave alone"),
                    account=spec, rows=rows)
                continue

            written = rows.get(GLOBAL_SCOPE)
            assert written is not None, counterexample(
                problem="no global row was written, so this account "
                        "would be locked out by enforcement (Req 2.1)",
                account=spec)
            for key, value in expected.items():
                assert written.get(key) == value, counterexample(
                    problem=f"backfilled row's {key!r} is wrong",
                    account=spec, written=written, expected=expected)
            assert int(written["assigned_at"]) > 0, written

        # --------------------------------------------- after enforcement
        with enforcement(shared, True):
            after = {name: resolve(shared, subs[name], specs[name])
                     for name in specs}
            usecase_after = {
                name: resolve(shared, subs[name], specs[name],
                              scope=usecase_scope(subs[name]))
                for name, spec in specs.items()
                if spec["preexisting"] == "usecase_only" and subs[name]}

        for name, spec in specs.items():
            sub = visible_sub[name]
            role, permissions = after[name]
            expected_role = expected_enforced_role(spec, sub)
            assert role == (None if expected_role is None
                            else shared.Role(expected_role)), \
                counterexample(problem="post-backfill role is not the "
                                       "registry's",
                               account=spec, measured=str(role),
                               expected=expected_role)

            if provisioned_by_backfill(spec, sub):
                # Requirement 2.3: the account reaches exactly what it
                # reached before, no less (locked out) and no more
                # (privilege the pool never gave it).
                before_role, before_permissions = before[name]
                assert role == before_role, counterexample(
                    problem="enabling enforcement changed this account's "
                            "role (Requirement 2.3)",
                    account=spec, before=str(before_role),
                    after=str(role))
                assert permissions == before_permissions, counterexample(
                    problem="enabling enforcement changed this account's "
                            "permissions (Requirement 2.3)",
                    account=spec,
                    lost=sorted(p.value for p in
                                before_permissions - permissions),
                    gained=sorted(p.value for p in
                                  permissions - before_permissions))
            elif spec["preexisting"] == "global_disabled":
                # Requirement 1.6 / 3.4: a disabled row denies exactly
                # as an absent one, whatever the token claims.
                assert role is None and permissions == set(), \
                    counterexample(account=spec, measured=str(role))
            elif not spec["enabled"] or sub is None:
                # Skipped by Decision 5: no row, so nothing grants —
                # unless a pre-existing enabled row already did.
                if spec["preexisting"] in ("global_same", "global_lower",
                                           "global_higher"):
                    assert role is not None
                else:
                    assert role is None and permissions == set(), \
                        counterexample(
                            problem="a skipped account resolved a role",
                            account=spec, measured=str(role))
            else:
                # An enabled pre-existing row: the registry's role wins
                # over the claim (Requirements 1.2, 1.4).
                assert role == shared.Role(expected_role), counterexample(
                    account=spec, measured=str(role))

            # The per-Use_Case grant still overrides at its own scope, so
            # the backfilled global row did not disturb Team Management.
            if name in usecase_after:
                usecase_role, _ = usecase_after[name]
                expected_usecase = expected_enforced_role(
                    spec, sub, scope=usecase_scope(subs[name]))
                assert usecase_role == (
                    None if expected_usecase is None
                    else shared.Role(expected_usecase)), counterexample(
                    problem="the Use_Case grant no longer decides its own "
                            "scope",
                    account=spec, measured=str(usecase_role),
                    expected=expected_usecase)

        # -------------------------------------------------- second apply
        second = run(backfill, client, dynamodb_resource, pool_id,
                     page_size, apply_changes=True)
        assert second.errors == 0
        assert second.created == 0, counterexample(
            problem="a second run wrote rows again, so it is not "
                    "idempotent (Requirement 2.2)",
            created=[entry.username for entry in
                     second.by_action("create")])
        for name, spec in specs.items():
            action = entries_by_username(second)[name].action
            assert action in ("exists", "skip-disabled", "skip-no-sub"), \
                counterexample(account=spec, action=action)
            if expected_action(spec, visible_sub[name]) == "create":
                assert action == "exists", counterexample(
                    problem="the row the first run created was not seen "
                            "by the second",
                    account=spec, action=action)

        assert snapshot(registry, subs.values()) == after_rows, \
            counterexample(
                problem="a second run changed the registry, so an "
                        "operator cannot safely re-run it (Req 2.2)",
                pool=pool)


# ===========================================================================
# Anchor: the conditional write, whose only unique job is the race
# ===========================================================================

class _BlindGetItemTable:
    """A table handle that reports every row as absent, while writes go
    through to the real table.

    This is the read-then-write race the conditional `put_item` exists
    for: the plan read no row (or a row was written by the portal's User
    Manager immediately after it did), so the write is the last line of
    defence. A single-process property run cannot generate that
    interleaving, hence this anchor — without it, dropping the
    `ConditionExpression` would be invisible.
    """

    def __init__(self, table):
        self._table = table

    def __getattr__(self, name):
        return getattr(self._table, name)

    def get_item(self, **kwargs):
        return {}

    def put_item(self, **kwargs):
        return self._table.put_item(**kwargs)


class _BlindResource:
    def __init__(self, resource):
        self._resource = resource

    def Table(self, name):  # noqa: N802 - boto3 resource API
        return _BlindGetItemTable(self._resource.Table(name))


class TestConditionalWriteSurvivesARowAppearingMidRun:
    """The conditional write keeps the backfill non-destructive even when
    the plan's read did not see the row (Requirement 2.2)."""

    def test_existing_row_survives_a_blind_read(self, backfill, registry,
                                                cognito,
                                                dynamodb_resource):
        pool_id = create_pool(cognito)
        spec = {"username": "acct-race", "claim": "PortalAdmin",
                "enabled": True, "has_sub": True, "has_email": True,
                "preexisting": "global_lower"}
        sub = create_account(cognito, pool_id, spec)
        registry.put_item(Item={
            "user_id": sub, "usecase_id": GLOBAL_SCOPE, "role": "Viewer",
            "status": "enabled", "username": spec["username"],
            "email": email_of(spec), "assigned_by": SEEDED_BY,
            "assigned_at": 1})
        before = read_row(registry, sub, GLOBAL_SCOPE)

        result = run(backfill, cognito, _BlindResource(dynamodb_resource),
                     pool_id, page_size=1, apply_changes=True)

        assert read_row(registry, sub, GLOBAL_SCOPE) == before, (
            "the existing row was overwritten despite the conditional "
            "write")
        entry = entries_by_username(result)[spec["username"]]
        assert entry.action == "exists", entry
        assert result.created == 0 and result.errors == 0


# ===========================================================================
# Anchors: the script's literal constants against the shared layer
# ===========================================================================

class TestBackfillConstantsMatchTheSharedLayer:
    """The script is deliberately self-contained (it must run with
    nothing but boto3 on an operator's path, and `shared_utils` builds
    AWS clients at import), so its role/scope/status literals are copies.
    These pin the copies against the layer they mirror — a role added to
    `Role` without being added here would silently be backfilled as
    Viewer, quietly downgrading that account at the flip."""

    def test_valid_roles_match_the_role_enum(self, shared, backfill):
        assert set(backfill.VALID_ROLES) == {role.value
                                             for role in shared.Role}

    def test_scope_status_and_default_role_match(self, shared, backfill):
        assert backfill.GLOBAL_SCOPE == shared.GLOBAL_SCOPE
        assert backfill.STATUS_ENABLED == shared.REGISTRY_STATUS_ENABLED
        assert backfill.DEFAULT_ROLE == shared.Role.VIEWER.value

    def test_valid_role_names_here_match_the_script(self, backfill):
        """The strategy's role list is the script's, so every role the
        portal has is exercised by the property rather than missed."""
        assert set(VALID_ROLE_NAMES) == set(backfill.VALID_ROLES)
