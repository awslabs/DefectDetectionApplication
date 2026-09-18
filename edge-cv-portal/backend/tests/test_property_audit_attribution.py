"""
Property test for durable audit attribution —
portal-jwt-role-privilege-escalation task 2.4.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

**Feature: portal-jwt-role-privilege-escalation, Property 5: Every audit
entry carries durable attribution.**

_For any_ generated request (claims present/absent/partial, allow and
deny paths, both audit helpers), the written item contains all five
Attribution_Fields, their values come from the request, and no
denylisted key survives in `details`.

_Validates: Requirements 4.1, 4.2, 4.3, 4.6_

The incident this closes: the actor's Cognito user was deleted 2 seconds
after `POST /builds` was accepted, so the surviving audit row named a
`sub` that resolves to nothing in any of the account's 7 user pools
(bugfix.md Bug Condition C2). Attribution therefore has to be captured
FROM THE REQUEST at write time — never resolved from Cognito later
(Requirement 4.2, design.md Decision 7) — which is what makes it durable
and what this property checks: the written item is compared against the
request that produced it, with nothing read back from any user pool.

Paths exercised, all real code, nothing about authorization or auditing
mocked (the only fake is the Cognito client of the User Manager path,
which never decides anything audited here):

| generated `path`      | production path                          | helper |
|-----------------------|------------------------------------------|--------|
| `deny_rbac`           | real `rbac_check` denial (403)           | `log_audit_event` |
| `deny_super_user`     | real `super_user_only` denial (403)      | `log_audit_event` |
| `unavailable`         | real `rbac_check` `RegistryUnavailable` (500) | `log_audit_event` |
| `allow_user_manager`  | real `POST /admin/users` (201)           | `record_audit_event_strict` + `finalize_audit_event` |
| `helper_log_identity` | `log_audit_event(identity=...)`          | `log_audit_event` |
| `helper_log_event`    | `log_audit_event(event=...)`             | `log_audit_event` |
| `helper_strict`       | `record_audit_event_strict(identity=...)`| strict pending |
| `helper_finalize`     | pending with no identity, then `finalize_audit_event(event=...)` | finalize |

Scope of the denylist arm (Requirement 4.6, "the **existing** denylist
SHALL still redact"): `sanitize_audit_details` is applied by the two
strict helpers, which are the ones whose `details` carry caller-supplied
content, and those are generated with denylisted keys here. The
`log_audit_event` paths are checked the other way round — the property
asserts their `details` keys are drawn from the handler's own fixed
metadata set and that **no generated secret from the request (body or
query string) appears anywhere in the written item** — because
`log_audit_event` has never sanitized (bugfix.md quotes its body:
`'details': details or {}`) and its production call sites compose details
from fixed keys rather than request content. That difference is recorded
rather than papered over: see the task 2.4 OUTCOME.

Run from `edge-cv-portal/backend` with
`~/.venvs/dda-portal-tests/bin/python -m pytest <this file> -q
-p no:cacheprovider` (moto-backed via conftest.py).
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
EDGE_CREDENTIALS_TABLE = "test-attribution-edge-credentials"
POOL_ID = "us-east-1_testpool"

# The five Attribution_Fields (design.md Glossary; Requirement 4.1).
ATTRIBUTION_FIELDS = ("username", "email", "source_ip", "user_agent",
                      "identity_source")

# Recorded when the request carries nothing for a field (Requirement 4.3).
UNKNOWN = "unknown"

# The generated paths (see the table in the module docstring).
PATHS = ("deny_rbac", "deny_super_user", "unavailable",
         "allow_user_manager", "helper_log_identity", "helper_log_event",
         "helper_strict", "helper_finalize")

# Which claim carries the username, or neither.
USERNAME_CLAIMS = ("cognito:username", "username", None)

USERNAMES = ("demoDataScientist", "admin", "ryan-labeler", "cli-created",
             "demoViewer")

# Email claim variants: present, absent, and the two values that are
# present but identify nothing ('unknown' is what the authorizer forwards
# for an account created without an email attribute — see
# shared_utils._display_identity).
EMAIL_VALUES = (None, "user@example.com", "first.last@sub.example.co",
                UNKNOWN, "", "   ")

SOURCE_IPS = (None, "12.148.187.67", "203.0.113.9", "::1", "")
USER_AGENTS = (None, "aws-cli/1.36.4", "Mozilla/5.0 (X11; Linux x86_64)",
               "", UNKNOWN)

# Keys the audit details denylist must drop (substring match on
# password / verifier / hash, prefix match on temp*).
DENYLISTED_DETAIL_KEYS = ("password", "new_password", "password_hash",
                          "credential_verifier", "verifier", "hash",
                          "temp", "tempPassword", "temp_password",
                          "temporaryPassword")

# The details keys each production path is allowed to write. A path that
# starts copying request content into details fails this.
ALLOWED_DETAIL_KEYS = {
    "deny_rbac": {"required_permissions", "usecase_id", "user_role",
                  "method", "path", "claimed_role"},
    "deny_super_user": {"user_role", "method", "path", "claimed_role"},
    "unavailable": {"required_permissions", "usecase_id", "method", "path",
                    "claimed_role", "error"},
    "helper_log_identity": {"target"},
    "helper_log_event": {"target"},
}


def request_shapes():
    """A generated request: which identifying claims it carries, whether
    it carries a `requestContext.identity` block, and whether its
    principal is provisioned in the Portal_Identity registry."""
    return st.fixed_dictionaries({
        "username_claim": st.sampled_from(USERNAME_CLAIMS),
        "username": st.sampled_from(USERNAMES),
        "email": st.sampled_from(EMAIL_VALUES),
        "has_identity_block": st.booleans(),
        "source_ip": st.sampled_from(SOURCE_IPS),
        "user_agent": st.sampled_from(USER_AGENTS),
        "provisioned": st.booleans(),
    })


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    """The real shared_utils imported inside the moto mock."""
    import shared_utils
    assert hasattr(shared_utils, "attribution_from"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    return shared_utils


@pytest.fixture(scope="module")
def enforcement_on(shared):
    """PORTAL_REGISTRY_ENFORCED on for this module only, so the deny
    paths deny for the reason the fix introduces. Attribution itself does
    not depend on the flag; the flag is restored on teardown."""
    variable = shared.PORTAL_REGISTRY_ENFORCED_ENV
    previous = os.environ.get(variable)
    os.environ[variable] = "true"
    yield
    if previous is None:
        os.environ.pop(variable, None)
    else:
        os.environ[variable] = previous


@pytest.fixture(scope="module")
def middleware(aws_stack):
    """The real rbac_middleware re-imported inside the moto mock."""
    sys.modules.pop("rbac_middleware", None)
    import rbac_middleware
    return rbac_middleware


class FakeCognito:
    """Minimal recording Cognito fake for the User Manager path: it
    reports a `sub` for the account it creates, which is the key the
    Portal_Identity row is written under."""

    def __init__(self):
        self.created = []

    def admin_create_user(self, UserPoolId, Username, UserAttributes,
                          **kwargs):
        sub = str(uuid.uuid4())
        self.created.append(Username)
        attributes = list(UserAttributes) + [{"Name": "sub", "Value": sub}]
        return {"User": {"Username": Username, "Enabled": True,
                         "Attributes": attributes,
                         "UserStatus": "FORCE_CHANGE_PASSWORD"}}


@pytest.fixture(scope="module")
def user_manager(aws_stack):
    """The real user_admin module (the strict-helper / allow path)."""
    import boto3

    os.environ["EDGE_CREDENTIALS_TABLE"] = EDGE_CREDENTIALS_TABLE
    ddb = boto3.client("dynamodb", region_name=REGION)
    if EDGE_CREDENTIALS_TABLE not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=EDGE_CREDENTIALS_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    sys.modules.pop("user_admin", None)
    import user_admin
    user_admin.cognito_client = FakeCognito()
    user_admin.USER_POOL_ID = POOL_ID
    return user_admin


@pytest.fixture(scope="module")
def registry(aws_stack):
    return aws_stack.tables.user_roles


@pytest.fixture(scope="module")
def audit(aws_stack):
    return aws_stack.tables.audit_log


# ---------------------------------------------------------------- helpers

def new_sub():
    return str(uuid.uuid4())


def build_claims(sub, shape, claimed_role="PortalAdmin"):
    """The authorizer claims for a generated request shape."""
    claims = {"sub": sub, "custom:role": claimed_role}
    if shape["username_claim"] is not None:
        claims[shape["username_claim"]] = shape["username"]
    if shape["email"] is not None:
        claims["email"] = shape["email"]
    return claims


def build_event(sub, shape, secret, claimed_role="PortalAdmin",
                method="POST", resource="/builds", usecase_id=None,
                body=None):
    """An API Gateway event for the generated shape.

    The generated `secret` is planted in the request body and query
    string: no audit entry may carry it (the request's content is not
    attribution and must not leak into `details`).
    """
    request_context = {
        "requestId": str(uuid.uuid4()),
        "authorizer": {"claims": build_claims(sub, shape, claimed_role)},
    }
    if shape["has_identity_block"]:
        identity = {}
        if shape["source_ip"] is not None:
            identity["sourceIp"] = shape["source_ip"]
        if shape["user_agent"] is not None:
            identity["userAgent"] = shape["user_agent"]
        request_context["identity"] = identity

    payload = dict(body or {})
    payload.setdefault("password", secret)
    payload.setdefault("temp_password", secret)
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "pathParameters": {"usecase_id": usecase_id} if usecase_id else None,
        "queryStringParameters": {"verifier": secret},
        "body": json.dumps(payload),
        "requestContext": request_context,
    }


def meaningful(value):
    """The value a field records: a present, non-blank value that is not
    the literal 'unknown' (which identifies nobody), else 'unknown'
    (Requirement 4.3)."""
    if value is None:
        return UNKNOWN
    text = str(value).strip()
    if not text or text.lower() == UNKNOWN:
        return UNKNOWN
    return text


def expected_attribution(shape, identity_source):
    """The five Attribution_Fields the request must produce, written out
    from Requirement 4.1-4.3 rather than from the implementation."""
    username = UNKNOWN
    if shape["username_claim"] is not None:
        username = meaningful(shape["username"])

    email = meaningful(shape["email"])

    source_ip = user_agent = UNKNOWN
    if shape["has_identity_block"]:
        source_ip = meaningful(shape["source_ip"])
        user_agent = meaningful(shape["user_agent"])

    return {"username": username, "email": email, "source_ip": source_ip,
            "user_agent": user_agent, "identity_source": identity_source}


def provision(registry, sub, role="Viewer", username="u",
              email="u@example.com", usecase_id="global"):
    registry.put_item(Item={
        "user_id": sub, "usecase_id": usecase_id, "role": role,
        "username": username, "email": email, "status": "enabled",
        "assigned_by": "backfill", "assigned_at": 1,
    })


def audit_rows(audit, sub):
    return [item for item in audit.scan().get("Items", [])
            if item.get("user_id") == sub]


def ok_handler(event, context):
    return {"statusCode": 200, "body": json.dumps({"ok": True})}


def denylisted_details(secret, keys, nested):
    """Caller-supplied details carrying the generated secret under
    denylisted keys, plus one key that must survive."""
    details = {"reason": "generated", **{key: secret for key in keys}}
    if nested:
        details["nested"] = {"outcome": "kept",
                             **{key: secret for key in keys}}
    return details


def denylisted_keys_present(details):
    """Every denylisted key that survived, recursively."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).lower()
                if (any(term in normalized
                        for term in ("password", "verifier", "hash"))
                        or normalized.startswith("temp")):
                    found.append(key)
                walk(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                walk(nested)

    walk(details or {})
    return found


class _FailingTable:
    """A registry table whose reads raise, standing in for a DynamoDB
    outage on the authorization path."""

    def _raise(self, *args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "InternalServerError",
                       "Message": "registry unavailable (generated)"}},
            "GetItem")

    get_item = _raise
    query = _raise
    scan = _raise


class _RegistryFailureResource:
    """Fails only the registry table, so the audit write still lands."""

    def __init__(self, real, registry_table_name):
        self._real = real
        self._registry_table_name = registry_table_name

    def Table(self, name):  # noqa: N802 - boto3 resource API
        if name == self._registry_table_name:
            return _FailingTable()
        return self._real.Table(name)


@contextmanager
def failing_registry(shared):
    """Restored inside the example: a function-scoped `monkeypatch` is set
    up once for the whole Hypothesis run and would leak into the
    following examples."""
    real_resource = shared.dynamodb
    shared.dynamodb = _RegistryFailureResource(real_resource,
                                               shared.USER_ROLES_TABLE)
    try:
        yield
    finally:
        shared.dynamodb = real_resource


def counterexample(**fields):
    return json.dumps(fields, indent=2, default=str)


# ===========================================================================
# Property 5
# ===========================================================================

class TestProperty5EveryAuditEntryCarriesDurableAttribution:
    """See the module docstring for the property statement and the paths
    it is evaluated on."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(shape=request_shapes(), path=st.sampled_from(PATHS),
           denied_keys=st.lists(st.sampled_from(DENYLISTED_DETAIL_KEYS),
                                min_size=1, max_size=4, unique=True),
           nested=st.booleans())
    # The recorded incident's own request metadata on the accepted-action
    # helper: source IP 12.148.187.67, user agent aws-cli/1.36.4.
    @example(shape={"username_claim": "cognito:username",
                    "username": "cli-created",
                    "email": "cli-created@example.com",
                    "has_identity_block": True,
                    "source_ip": "12.148.187.67",
                    "user_agent": "aws-cli/1.36.4",
                    "provisioned": True},
             path="helper_log_identity", denied_keys=["password"],
             nested=False)
    # The same principal denied by the fix, unprovisioned: the denial
    # must still name the human (Requirement 4.4).
    @example(shape={"username_claim": "cognito:username",
                    "username": "cli-created",
                    "email": "cli-created@example.com",
                    "has_identity_block": True,
                    "source_ip": "12.148.187.67",
                    "user_agent": "aws-cli/1.36.4",
                    "provisioned": False},
             path="deny_rbac", denied_keys=["password"], nested=False)
    # The bootstrap `admin` account: no email claim at all, and no
    # requestContext.identity (a non-proxy invocation shape) — every
    # missing value records the literal 'unknown' and the entry is still
    # written (Requirement 4.3).
    @example(shape={"username_claim": None, "username": "admin",
                    "email": None, "has_identity_block": False,
                    "source_ip": None, "user_agent": None,
                    "provisioned": True},
             path="allow_user_manager", denied_keys=["temp_password"],
             nested=True)
    # Both strict helpers with caller-supplied denylisted details.
    @example(shape={"username_claim": "username", "username": "demoViewer",
                    "email": "demoViewer@example.com",
                    "has_identity_block": True,
                    "source_ip": "203.0.113.9", "user_agent": "Mozilla/5.0",
                    "provisioned": True},
             path="helper_strict",
             denied_keys=["password", "credential_verifier",
                          "password_hash", "tempPassword"],
             nested=True)
    @example(shape={"username_claim": "cognito:username",
                    "username": "admin", "email": UNKNOWN,
                    "has_identity_block": True, "source_ip": "",
                    "user_agent": UNKNOWN, "provisioned": False},
             path="helper_finalize", denied_keys=["hash"], nested=False)
    # A registry outage: attribution is still written, with the
    # Identity_Source recorded as undetermined rather than fabricated.
    @example(shape={"username_claim": "cognito:username",
                    "username": "demoDataScientist",
                    "email": "demoDataScientist@example.com",
                    "has_identity_block": True,
                    "source_ip": "203.0.113.9",
                    "user_agent": "aws-cli/1.36.4", "provisioned": True},
             path="unavailable", denied_keys=["verifier"], nested=False)
    def test_every_audit_entry_is_attributable(
            self, shared, middleware, user_manager, registry, audit,
            enforcement_on, shape, path, denied_keys, nested):
        secret = f"SEKRET-{uuid.uuid4().hex}"
        sub = new_sub()
        if shape["provisioned"]:
            # A Viewer row: provisioned (so the Identity_Source is
            # 'registry') but without the permissions the deny paths ask
            # for, so those paths still deny.
            provision(registry, sub, role="Viewer",
                      username=shape["username"])

        identity_source = ("registry" if shape["provisioned"] else "absent")
        if path == "unavailable":
            # The registry could not be read, so nothing decided the
            # role: 'unknown' rather than a fabricated 'registry' /
            # 'absent' (Requirement 4.1 names the two deciding values,
            # 4.3 fixes the convention for what a request cannot supply).
            identity_source = UNKNOWN
        expected = expected_attribution(shape, identity_source)

        entries = self._drive(path, shared, middleware, user_manager, audit,
                             sub, shape, secret, denied_keys, nested)

        assert entries, f"path {path} wrote no audit entry at all"
        for entry in entries:
            # 1. Every entry carries all five fields (Requirement 4.1).
            missing = [field for field in ATTRIBUTION_FIELDS
                       if field not in entry]
            assert not missing, counterexample(
                path=path, missing=missing, entry=entry)

            # 2. Their values come from THIS request (Requirement 4.2),
            #    with 'unknown' for whatever it does not carry (4.3).
            actual = {field: entry[field] for field in ATTRIBUTION_FIELDS}
            assert actual == expected, counterexample(
                path=path, shape=shape, expected=expected, actual=actual)

            # 3. The entry stays attributable by `sub` too: the legacy
            #    identity fields keep their meaning (design.md
            #    Decision 7) — a deleted user's row still names both.
            assert entry["user_id"] == sub, entry
            assert str(entry["event_id"]).startswith(f"{sub}_"), entry

            # 4. No denylisted key survives in details, and the request's
            #    own content never reaches them (Requirement 4.6).
            survivors = denylisted_keys_present(entry.get("details"))
            assert survivors == [], counterexample(
                path=path, survivors=survivors, details=entry["details"])
            assert secret not in json.dumps(entry, default=str), \
                counterexample(path=path, entry=entry)

            allowed = ALLOWED_DETAIL_KEYS.get(path)
            if allowed is not None:
                assert set((entry.get("details") or {})) <= allowed, \
                    counterexample(path=path, details=entry["details"],
                                   allowed=sorted(allowed))

    # ------------------------------------------------------------ drivers

    def _drive(self, path, shared, middleware, user_manager, audit, sub,
               shape, secret, denied_keys, nested):
        """Run the generated path and return the audit entries it wrote."""
        driver = getattr(self, f"_drive_{path}")
        driver(shared, middleware, user_manager, sub, shape, secret,
               denied_keys, nested)
        return audit_rows(audit, sub)

    @staticmethod
    def _drive_deny_rbac(shared, middleware, user_manager, sub, shape,
                         secret, denied_keys, nested):
        """A real rbac_check denial: builds:submit, which neither an
        unprovisioned principal nor a registry Viewer carries."""
        guard = middleware.require_builds_submit()(ok_handler)
        response = guard(build_event(sub, shape, secret), None)
        assert response["statusCode"] == 403, response

    @staticmethod
    def _drive_deny_super_user(shared, middleware, user_manager, sub, shape,
                               secret, denied_keys, nested):
        decorated = middleware.super_user_only(ok_handler)
        response = decorated(
            build_event(sub, shape, secret, method="GET",
                        resource="/admin/users"), None)
        assert response["statusCode"] == 403, response

    @staticmethod
    def _drive_unavailable(shared, middleware, user_manager, sub, shape,
                           secret, denied_keys, nested):
        guard = middleware.require_builds_submit()(ok_handler)
        with failing_registry(shared):
            response = guard(build_event(sub, shape, secret), None)
        assert response["statusCode"] == 500, response

    @staticmethod
    def _drive_allow_user_manager(shared, middleware, user_manager, sub,
                                  shape, secret, denied_keys, nested):
        """The real POST /admin/users accepted path: both strict helpers
        (pending + finalize) on one entry."""
        username = f"generated-{uuid.uuid4().hex[:10]}"
        event = build_event(
            sub, shape, secret, method="POST", resource="/admin/users",
            body={"username": username, "email": f"{username}@example.com",
                  "role": "Operator"})
        response = user_manager.handler(event, None)
        assert response["statusCode"] == 201, response

    @staticmethod
    def _drive_helper_log_identity(shared, middleware, user_manager, sub,
                                   shape, secret, denied_keys, nested):
        """The accepted-action shape the incident left behind
        (`build_requested`), written with attribution built by
        `attribution_from` — the single reader of
        requestContext.identity."""
        event = build_event(sub, shape, secret)
        shared.log_audit_event(
            user_id=sub, action="build_requested", resource_type="build_job",
            resource_id=str(uuid.uuid4()), result="success",
            details={"target": "JP7"},
            identity=shared.attribution_from(
                event, shared.get_user_from_event(event),
                usecase_id="global"))

    @staticmethod
    def _drive_helper_log_event(shared, middleware, user_manager, sub, shape,
                                secret, denied_keys, nested):
        """Same, through the `event=` parameter (the caller that has no
        `user` dict to hand)."""
        shared.log_audit_event(
            user_id=sub, action="build_requested", resource_type="build_job",
            resource_id=str(uuid.uuid4()), result="success",
            details={"target": "JP7"},
            event=build_event(sub, shape, secret))

    @staticmethod
    def _drive_helper_strict(shared, middleware, user_manager, sub, shape,
                             secret, denied_keys, nested):
        event = build_event(sub, shape, secret)
        shared.record_audit_event_strict(
            user_id=sub, action="password_change",
            resource_type=shared.USER_ACCOUNT_RESOURCE_TYPE,
            resource_id=shape["username"],
            details=denylisted_details(secret, denied_keys, nested),
            identity=shared.attribution_from(
                event, shared.get_user_from_event(event)))

    @staticmethod
    def _drive_helper_finalize(shared, middleware, user_manager, sub, shape,
                               secret, denied_keys, nested):
        """The pending phase carries no attribution (a caller that cannot
        reach the event); the finalize supplies it from the request."""
        event_id = shared.record_audit_event_strict(
            user_id=sub, action="role_change",
            resource_type=shared.USER_ACCOUNT_RESOURCE_TYPE,
            resource_id=shape["username"],
            details=denylisted_details(secret, denied_keys, nested))
        shared.finalize_audit_event(
            event_id, "success",
            details=denylisted_details(secret, denied_keys, nested),
            event=build_event(sub, shape, secret))
