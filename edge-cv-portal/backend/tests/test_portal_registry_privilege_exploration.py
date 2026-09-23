"""
Bug-condition exploration suite — portal-jwt-role-privilege-escalation
task 1.1.

Spec: .kiro/specs/portal-jwt-role-privilege-escalation/
      (bugfix.md = requirements, design.md = source of truth)

**THESE TESTS ASSERT THE FIXED BEHAVIOUR AND ARE EXPECTED TO FAIL ON THE
UNFIXED TREE.** Their failure IS the proof of the two bug conditions
(bugfix.md "Bug Condition"): today an unprovisioned Cognito principal is
authorized from its own `custom:role` claim, and the audit row it leaves
behind names nothing but a `sub` that stops resolving the moment the
Cognito user is deleted. Do not weaken the assertions — the same file,
unmodified, is the fix check at task 7.

Incident replayed (verified evidence, account 164152369890,
2026-09-17T14:26 UTC, pool `us-east-1_2r9jpbWIe`, source IP
`12.148.187.67`, user agent `aws-cli/1.36.4`):

    AdminCreateUser (custom:role=PortalAdmin) -> AdminSetUserPassword ->
    InitiateAuth (sub 74189498-9061-7062-a1e0-f9db61c506c2) ->
    POST /builds accepted (Build_Job 2ec3d7ab-..., target JP7, ref
    feat/capture-phase-outputs, published LocalServer 1.0.40) ->
    AdminDeleteUser

    Surviving evidence: `dda-portal-audit-log` action=build_requested,
    event_id "74189498-..._1789655167050", user_id "74189498-...", and
    `dda-portal-build-jobs.requested_by = "74189498-..."`. That sub
    resolves to nothing in any of the account's 7 user pools.

What this suite exercises, deliberately end to end and with nothing about
authorization mocked, unwrapped or redecorated:

* the real `build_jobs.handler` routing `POST /builds` to the real
  `submit_build`, decorated with the real `@require_builds_submit()`
  (-> `rbac_check(..., allow_global=True)`, scope 'global');
* the real `shared_utils.get_user_from_event`,
  `shared_utils.RBACManager` role resolution, the real role/permission
  matrix, and the real `log_audit_event` writes;
* moto-backed DynamoDB: the conftest `test-user-roles` (the
  Portal_Identity registry) and `test-audit-log` tables plus a
  BuildJobs table carrying the deployed GSIs.

Nothing can reach AWS and no build can launch: every client is
moto-backed and `BUILD_DISPATCHER_FUNCTION_NAME` is unset on the module
under test, so `invoke_dispatcher` is a logged no-op.

Expected failures on the unfixed tree:

* the three unprovisioned-claim cases answer **201** instead of 403 (the
  escalation: `custom:role` alone carries `builds:submit`);
* the registry-below-claim case answers **201** instead of 403;
* the denial-audit case has no denial to audit at all;
* the accepted-path audit rows carry **no** `username` / `email` /
  `source_ip` / `user_agent` / `identity_source` (log_audit_event writes
  only `user_id`), and the Build_Job carries no human-readable actor.

_Requirements: 7.1, 7.2, 7.3, 4.1, 4.2, 4.3, 4.4, 4.5, 1.1, 1.4_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from dynamo_helpers import all_table_names

REGION = "us-east-1"

# Build tables owned by this suite (created inside the conftest moto
# mock; the module globals of build_jobs are repointed at them so no
# environment variable other suites read is disturbed).
BUILD_JOBS_TABLE = "test-registry-explore-build-jobs"
BUILD_SERVERS_TABLE = "test-registry-explore-build-servers"
BUILD_SETTINGS_TABLE = "test-registry-explore-settings"

# The incident's request metadata (CloudTrail, bugfix.md Incident Record).
INCIDENT_SOURCE_IP = "12.148.187.67"
INCIDENT_USER_AGENT = "aws-cli/1.36.4"

# The claim values that actually carry builds:submit today: PortalAdmin
# short-circuits resolution (step 1), DataScientist / UseCaseAdmin come
# through the JWT fallback (step 4). Viewer does not hold builds:submit,
# which is why the incident's principal must have carried an injected
# custom:role (bugfix.md "Where privilege is granted today").
ESCALATING_CLAIMS = ("PortalAdmin", "DataScientist", "UseCaseAdmin")

# The five Attribution_Fields (design.md Glossary; Requirement 4.1).
ATTRIBUTION_FIELDS = ("username", "email", "source_ip", "user_agent",
                      "identity_source")


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def build_api(aws_stack):
    """The real build_jobs handler (plus shared_utils / rbac_middleware)
    imported inside the moto mock, wired to this suite's BuildJobs,
    BuildServers and Settings tables.

    rbac_middleware and build_jobs are re-imported so they bind the same
    shared_utils instance the conftest stack was built with (the
    Permission enum and rbac_manager must be identical objects).
    """
    import boto3

    ddb = boto3.client("dynamodb", region_name=REGION)
    existing = all_table_names(ddb)

    if BUILD_JOBS_TABLE not in existing:
        # Deployed BuildJobs schema, GSIs included (the sibling suite
        # test/backend-test/portal_builds/test_jwt_admin_build_submit_
        # authorization.py records that omitting the GSIs hides real
        # persistence failures).
        ddb.create_table(
            TableName=BUILD_JOBS_TABLE,
            KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "build_job_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "N"},
                {"AttributeName": "server_id", "AttributeType": "S"},
                {"AttributeName": "request_id", "AttributeType": "S"},
                {"AttributeName": "request_order", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {"IndexName": "status-index",
                 "KeySchema": [
                     {"AttributeName": "status", "KeyType": "HASH"},
                     {"AttributeName": "created_at", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
                {"IndexName": "server-index",
                 "KeySchema": [
                     {"AttributeName": "server_id", "KeyType": "HASH"},
                     {"AttributeName": "created_at", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
                {"IndexName": "request-index",
                 "KeySchema": [
                     {"AttributeName": "request_id", "KeyType": "HASH"},
                     {"AttributeName": "request_order", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    for name, key in ((BUILD_SERVERS_TABLE, "server_id"),
                      (BUILD_SETTINGS_TABLE, "setting_key")):
        if name not in existing:
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )

    for module_name in ("build_jobs", "rbac_middleware"):
        sys.modules.pop(module_name, None)
    import rbac_middleware
    import build_jobs
    import build_domain
    import shared_utils

    # Repoint the handler's table globals (read per call) at this suite's
    # tables, and make dispatch impossible.
    build_jobs.BUILD_JOBS_TABLE = BUILD_JOBS_TABLE
    build_jobs.BUILD_SERVERS_TABLE = BUILD_SERVERS_TABLE
    build_jobs.SETTINGS_TABLE = BUILD_SETTINGS_TABLE
    build_jobs.BUILD_DISPATCHER_FUNCTION_NAME = None

    resource = boto3.resource("dynamodb", region_name=REGION)
    return SimpleNamespace(
        build_jobs=build_jobs,
        build_domain=build_domain,
        rbac_middleware=rbac_middleware,
        shared=shared_utils,
        jobs_table=resource.Table(BUILD_JOBS_TABLE),
        registry=aws_stack.tables.user_roles,
        audit=aws_stack.tables.audit_log,
    )


# ---------------------------------------------------------------- helpers

def new_sub():
    """A fresh Cognito `sub` (the incident's principal shape: a uuid with
    no portal-side record anywhere)."""
    return str(uuid.uuid4())


def cognito_claims(sub, claimed_role="PortalAdmin", username=None,
                   email=None, include_role=True):
    """Authorizer claims exactly as the Cognito User Pools authorizer
    forwards them. `username`/`email` set to None omit the claim."""
    claims = {"sub": sub}
    if username is not None:
        claims["cognito:username"] = username
    if email is not None:
        claims["email"] = email
    if include_role:
        claims["custom:role"] = claimed_role
    return claims


def post_builds_event(claims, body, source_ip=INCIDENT_SOURCE_IP,
                      user_agent=INCIDENT_USER_AGENT, with_identity=True):
    """API Gateway REST event for POST /builds, carrying the
    requestContext.identity block the fix must read attribution from
    (Requirement 4.2)."""
    request_context = {
        "requestId": str(uuid.uuid4()),
        "authorizer": {"claims": claims},
    }
    if with_identity:
        request_context["identity"] = {"sourceIp": source_ip,
                                       "userAgent": user_agent}
    return {
        "resource": "/builds",
        "httpMethod": "POST",
        "path": "/builds",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body),
        "requestContext": request_context,
    }


def incident_request(build_api):
    """The incident's own submission: target JP7, ephemeral mode, the
    branch the recorded Build_Job was built from."""
    return {
        "targets": [build_api.build_domain.TARGET_JP7],
        "execution_mode": build_api.build_domain.EXECUTION_MODE_EPHEMERAL,
        "source_ref": "feat/capture-phase-outputs",
    }


def submit(build_api, claims, body=None, **event_kwargs):
    """Drive the real dispatch: build_jobs.handler -> submit_build behind
    the real @require_builds_submit(). Returns (status, parsed body)."""
    event = post_builds_event(claims, body or incident_request(build_api),
                              **event_kwargs)
    response = build_api.build_jobs.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def provision(build_api, sub, role, usecase_id="global", **attrs):
    """Write a Portal_Identity registry row (the only privilege source
    after the fix)."""
    item = {"user_id": sub, "usecase_id": usecase_id, "role": role,
            "assigned_by": "test", "assigned_at": 1}
    item.update(attrs)
    build_api.registry.put_item(Item=item)


def registry_rows(build_api, sub):
    return build_api.registry.query(
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": sub},
    ).get("Items", [])


def jobs_of(build_api, sub):
    """Build_Jobs persisted for this requester (full scan; the table is
    tiny and per-suite)."""
    return [item for item in build_api.jobs_table.scan().get("Items", [])
            if item.get("requested_by") == sub]


def audit_rows(build_api, sub, action):
    return [item for item in build_api.audit.scan().get("Items", [])
            if item.get("user_id") == sub and item.get("action") == action]


def only_row(rows, what):
    assert len(rows) == 1, f"expected exactly one {what}, got {rows}"
    return rows[0]


def with_attribution(entry):
    """Assert the audit entry carries all five Attribution_Fields before
    their values are checked, so the bug condition (fields absent
    entirely) reports as a named defect and not a KeyError."""
    missing = [field for field in ATTRIBUTION_FIELDS if field not in entry]
    assert not missing, (
        f"audit entry carries no durable attribution: {missing} absent "
        f"(Requirement 4.1). Entry: {entry}")
    return entry


def with_keys(item, keys, what):
    """Same, for a persisted record's expected attributes."""
    missing = [key for key in keys if key not in item]
    assert not missing, f"{what} carries no {missing}. Record: {item}"
    return item


def counterexample(status, body, extra=None):
    return json.dumps({"status": status, "body": body,
                       **(extra or {})}, indent=2, default=str)


@pytest.fixture(scope="module")
def enforcement_on(build_api):
    """PORTAL_REGISTRY_ENFORCED on for this module only.

    Every assertion in this suite is a statement about the ENFORCED mode
    (design.md "Expected Behavior"): the deployed default is off until the
    registry has been backfilled (design.md Decision 4, task 5.2 flips
    it), and with it off the legacy resolution order still grants
    privilege from `custom:role` — which is the bug, not the fix. So the
    suite must assert against the enforced path, exactly as its siblings
    (`test_property_portal_registry_roles.py`,
    `test_portal_registry_privilege_preservation.py`) do.

    `registry_enforcement_enabled()` reads the environment on every call,
    so setting the variable is enough; it is restored on teardown so the
    rest of the session keeps the deployed default.

    This does NOT weaken the suite: on the unfixed tree the module has no
    `PORTAL_REGISTRY_ENFORCED_ENV` to read and the same assertions still
    fail, so the file remains both the bug-condition proof (task 1.1) and
    the fix check (task 7).
    """
    import shared_utils

    variable = shared_utils.PORTAL_REGISTRY_ENFORCED_ENV
    previous = os.environ.get(variable)
    os.environ[variable] = "true"
    assert shared_utils.registry_enforcement_enabled() is True
    yield
    if previous is None:
        os.environ.pop(variable, None)
    else:
        os.environ[variable] = previous


# ---------------------------------------------------------------------------
# Bug condition C1 — privilege from an unprovisioned claim
# (bugfix.md "Bug Condition"; Requirements 7.1, 1.1, 1.4)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("enforcement_on")
class TestIncidentReplay:
    """An authenticated principal with NO Portal_Identity registry row
    must be denied, whatever its `custom:role` claims."""

    @pytest.mark.parametrize("claimed_role", ESCALATING_CLAIMS)
    def test_unprovisioned_claim_is_denied(self, build_api, claimed_role):
        """The recorded incident, replayed through the real POST /builds
        authorization boundary: created directly in Cognito with
        `custom:role`, never provisioned in the portal, submits a JP7
        build. FAILS TODAY WITH 201 (Requirement 7.1, 1.1)."""
        sub = new_sub()
        assert registry_rows(build_api, sub) == [], (
            "precondition: the principal must be unprovisioned (no "
            "dda-portal-user-roles row, global or per-Use_Case)")

        status, body = submit(
            build_api, cognito_claims(sub, claimed_role,
                                      username="cli-created",
                                      email="cli-created@example.com"))

        assert status == 403, (
            f"an unprovisioned Cognito principal claiming "
            f"custom:role={claimed_role} was authorized for builds:submit "
            f"— the escalation.\n{counterexample(status, body)}")
        assert body["error"] == "Insufficient permissions", \
            counterexample(status, body)
        assert jobs_of(build_api, sub) == [], (
            "a denied submission must create no Build_Job.\n"
            f"{counterexample(status, body)}")

    @pytest.mark.parametrize("claimed_role", ESCALATING_CLAIMS)
    def test_unprovisioned_claim_grants_no_permission(self, build_api,
                                                      claimed_role):
        """Same statement one layer down, where the fix lives: with no
        registry row, `has_permission` is false for builds:submit
        regardless of the claim (Requirement 1.1)."""
        sub = new_sub()
        user_info = {"user_id": sub, "email": "cli@example.com",
                     "username": "cli-created", "role": claimed_role}

        granted = build_api.shared.rbac_manager.has_permission(
            sub, "global", build_api.shared.Permission.BUILDS_SUBMIT,
            user_info=user_info)

        assert granted is False, (
            f"custom:role={claimed_role} granted builds:submit with no "
            f"Portal_Identity row")

    def test_claim_cannot_raise_the_registry_role(self, build_api):
        """A provisioned Viewer whose token claims PortalAdmin stays a
        Viewer: the claim never raises the registry role
        (Requirement 1.4). FAILS TODAY WITH 201."""
        sub = new_sub()
        provision(build_api, sub, "Viewer", username="demoviewer",
                  email="demoviewer@example.com", status="enabled")

        status, body = submit(
            build_api, cognito_claims(sub, "PortalAdmin",
                                      username="demoviewer",
                                      email="demoviewer@example.com"))

        assert status == 403, (
            "a registry Viewer was raised to PortalAdmin by its own "
            f"custom:role claim.\n{counterexample(status, body)}")
        assert body["error"] == "Insufficient permissions", \
            counterexample(status, body)
        assert jobs_of(build_api, sub) == [], counterexample(status, body)

    def test_denial_is_audited_with_absent_identity_source(self, build_api):
        """The denial itself is attributable: an `unauthorized_access`
        entry naming the human, the request's source IP / user agent,
        `identity_source='absent'`, and the Claimed_Role
        (Requirements 7.1, 4.1, 4.4). FAILS TODAY — there is no denial,
        and log_audit_event writes none of these fields."""
        sub = new_sub()
        status, body = submit(
            build_api, cognito_claims(sub, "PortalAdmin",
                                      username="cli-created",
                                      email="cli-created@example.com"))
        assert status == 403, (
            "no denial to audit: the unprovisioned principal was "
            f"authorized.\n{counterexample(status, body)}")

        entry = with_attribution(
            only_row(audit_rows(build_api, sub, "unauthorized_access"),
                     "unauthorized_access audit entry"))

        assert entry["result"] == "denied", entry
        assert entry["username"] == "cli-created", entry
        assert entry["email"] == "cli-created@example.com", entry
        assert entry["source_ip"] == INCIDENT_SOURCE_IP, entry
        assert entry["user_agent"] == INCIDENT_USER_AGENT, entry
        assert entry["identity_source"] == "absent", entry

        # The Claimed_Role is recorded as descriptive metadata in details
        # (design.md Decision 2); the key name is the implementation's
        # choice, so any details value naming it satisfies this.
        details_values = {str(value)
                          for value in (entry.get("details") or {}).values()}
        assert "PortalAdmin" in details_values, (
            "the denied request's Claimed_Role must be recorded in the "
            f"audit details: {entry.get('details')}")


# ---------------------------------------------------------------------------
# Bug condition C2 — an accepted action nobody can attribute afterwards
# (Requirements 4.1, 4.2, 4.3, 4.5, 7.2, 7.3)
# ---------------------------------------------------------------------------

class TestAcceptedActionAttribution:
    """A provisioned Build_Operator is still accepted (the fix denies
    only unprovisioned principals), and what it does stays attributable
    after its Cognito user is deleted."""

    @staticmethod
    def _provisioned_submit(build_api, username="demodatascientist",
                            email="demodatascientist@example.com",
                            **event_kwargs):
        sub = new_sub()
        provision(build_api, sub, "DataScientist", username=username,
                  email=email, status="enabled")
        status, body = submit(
            build_api,
            cognito_claims(sub, "DataScientist", username=username,
                           email=email),
            **event_kwargs)
        return sub, status, body

    def test_provisioned_principal_is_accepted(self, build_api):
        """Requirement 7.2: a registry row naming a build-capable role
        keeps POST /builds working — this must pass before AND after the
        fix, so none of the assertions below are vacuous."""
        sub, status, body = self._provisioned_submit(build_api)

        assert status == 201, (
            "a provisioned DataScientist was denied builds:submit.\n"
            f"{counterexample(status, body)}")
        job = only_row(jobs_of(build_api, sub), "persisted Build_Job")
        assert job["build_target"] == build_api.build_domain.TARGET_JP7
        assert job["status"] == build_api.build_domain.STATUS_QUEUED

    def test_accepted_audit_row_names_the_human(self, build_api):
        """The incident's `build_requested` row, made durable: username,
        email, source IP and user agent captured from the request at
        write time, so a later AdminDeleteUser cannot erase attribution
        (Requirements 4.1, 4.2, 7.3). FAILS TODAY — log_audit_event
        writes only user_id."""
        sub, status, body = self._provisioned_submit(build_api)
        assert status == 201, counterexample(status, body)

        entry = with_attribution(
            only_row(audit_rows(build_api, sub, "build_requested"),
                     "build_requested audit entry"))

        assert entry["username"] == "demodatascientist", entry
        assert entry["email"] == "demodatascientist@example.com", entry
        assert entry["source_ip"] == INCIDENT_SOURCE_IP, entry
        assert entry["user_agent"] == INCIDENT_USER_AGENT, entry

        # Attribution must be readable from the row ALONE: the deleted
        # principal's sub resolves to nothing in any pool, so nothing may
        # need resolving later (Requirement 4.2). The row still keeps its
        # backward-compatible identity fields (design.md Decision 7).
        assert entry["user_id"] == sub, entry
        assert str(entry["event_id"]).startswith(f"{sub}_"), entry
        assert "unknown" not in (entry["username"], entry["email"]), entry

    def test_accepted_audit_row_records_identity_source(self, build_api):
        """Requirement 4.1 names `identity_source` as an attribution
        field of EVERY audit entry: an accepted action resolved from the
        registry records `registry`. FAILS TODAY (field absent)."""
        sub, status, body = self._provisioned_submit(build_api)
        assert status == 201, counterexample(status, body)

        entry = with_attribution(
            only_row(audit_rows(build_api, sub, "build_requested"),
                     "build_requested audit entry"))
        assert entry["identity_source"] == "registry", entry

    def test_build_job_records_human_readable_actor(self, build_api):
        """Requirement 4.5: the Build_Job keeps `requested_by` (the sub)
        and additionally names a human, so the artifact a build publishes
        is attributable. FAILS TODAY — the recorded incident's job carries
        only `requested_by = "74189498-..."`."""
        sub, status, body = self._provisioned_submit(build_api)
        assert status == 201, counterexample(status, body)

        job = with_keys(
            only_row(jobs_of(build_api, sub), "persisted Build_Job"),
            ("requested_by", "requested_by_username", "requested_by_email"),
            "the Build_Job")
        assert job["requested_by"] == sub, job
        assert job["requested_by_username"] == "demodatascientist", job
        assert job["requested_by_email"] == "demodatascientist@example.com", \
            job

    def test_missing_claims_and_identity_record_unknown(self, build_api):
        """Requirement 4.3: a missing claim records the literal
        'unknown' and the entry is still written — the request must not
        fail and attribution must not be fabricated. FAILS TODAY (the
        fields do not exist)."""
        sub = new_sub()
        provision(build_api, sub, "DataScientist", status="enabled")

        # No cognito:username, no email, and no requestContext.identity
        # (the shape a non-proxy invocation / bootstrap user produces).
        status, body = submit(
            build_api, cognito_claims(sub, "DataScientist"),
            with_identity=False)
        assert status == 201, counterexample(status, body)

        entry = with_attribution(
            only_row(audit_rows(build_api, sub, "build_requested"),
                     "build_requested audit entry"))
        assert entry["username"] == "unknown", entry
        assert entry["source_ip"] == "unknown", entry
        assert entry["user_agent"] == "unknown", entry
        assert entry["identity_source"] == "registry", entry
