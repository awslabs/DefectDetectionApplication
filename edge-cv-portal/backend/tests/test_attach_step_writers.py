"""
Handler-level tests for the Attach_Step on the four bucket record writers and
the Registration_Report (portal-authorization-and-iam-scoping, task 5.5;
design I5 "The Attach_Step", "Registration-time reporting", "Region").

The real `usecases` and `data_management` handlers run on the conftest moto
stack (use-case, registry and audit tables). Every S3 client the shared layer
builds comes from one fake S3 (`World`) installed as
`shared_utils.create_boto3_client`. It models the Tag_Condition: on a client
of the execution role or of DDAPortalAccessRole, HeadBucket and ListObjectsV2
answer a bucket only once it carries dda-portal:managed=true AND its ABAC is
Enabled, while GetBucketTagging and GetBucketAbac (the tagDiscovery
carve-out) always answer; DDAPortalDataAccessRole's own grants are
unconditional. A denied HeadBucket is a 403 that still carries
x-amz-bucket-region, as S3 sends it. `create_s3_bucket`'s own client is the
same fake (installed per test on the usecases module). `assume_usecase_role`,
the bounded wait (`shared_utils.time.sleep`) and `log_audit_event` are
recorders.

Fixture accounts only: 123456789012 (moto's caller identity, so the portal
account of a single-account record), 222222222222 (trusted use-case account)
and 333333333333 (data account).

Validates: Requirements 3.1, 7.2, 7.6, 7.8, 7.11, 10.4
"""
import ast
import json
import os
import sys
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PORTAL = "123456789012"
TRUSTED = "222222222222"
DATA = "333333333333"
ROOT_ARN = f"arn:aws:iam::{PORTAL}:root"
ACCESS_ROLE = f"arn:aws:iam::{TRUSTED}:role/DDAPortalAccessRole"
DATA_ROLE = f"arn:aws:iam::{DATA}:role/DDAPortalDataAccessRole"
MANAGED = "dda-portal:managed"
CREATED_BY = "dda-portal:created-by"
MANAGED_TAGS = {MANAGED: "true"}


def client_error(code: str, status: int, operation: str = "Op",
                 headers: Optional[Dict[str, str]] = None) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status,
                                             "HTTPHeaders": headers or {}}}, operation)


def reached(remedy=None, *, tagged=True, abac_enabled=True, iam_reachable=True):
    """A Registration_Report entry (Requirement 7.11)."""
    return {"tagged": tagged, "abac_enabled": abac_enabled,
            "iam_reachable": iam_reachable, "remedy": remedy}


# --------------------------------------------------------------- the fake S3

class World:
    """The buckets S3 knows, shared by every fake client; `calls` records
    (who, region, operation, bucket) in order, `denials` the conditioned calls
    the Tag_Condition refused, `plans` scripts the first answers of an
    operation (an exception is raised, 'deny' refuses, 'no-header' is a 403
    HeadBucket without x-amz-bucket-region, None is the modelled answer)."""

    def __init__(self):
        self.buckets: Dict[str, SimpleNamespace] = {}
        self.calls: List[tuple] = []
        self.denials: List[tuple] = []
        self.plans: Dict[str, List[Any]] = {}
        self.factory_calls: List[tuple] = []          # (who, region) per shared-layer client

    def add(self, name, *, tags=None, abac="Disabled", region="us-east-1"):
        self.buckets[name] = SimpleNamespace(tags=dict(tags or {}), abac=abac, region=region)
        return name

    def plan(self, operation, *steps):
        self.plans.setdefault(operation, []).extend(steps)

    def ops(self, bucket=None, who=None):
        return [op for caller, _, op, name in self.calls
                if (bucket is None or name == bucket) and (who is None or caller == who)]

    def regions_of(self, operation, bucket):
        return [region for _, region, op, name in self.calls if op == operation and name == bucket]


class FakeS3:
    def __init__(self, world: World, who: str, region: Optional[str], conditioned: bool):
        self.world = world
        self.who = who
        self.region = region
        self.conditioned = conditioned

    def _enter(self, operation, bucket):
        self.world.calls.append((self.who, self.region, operation, bucket))
        steps = self.world.plans.get(operation)
        step = steps.pop(0) if steps else None
        if isinstance(step, BaseException):
            raise step
        return step

    def _state(self, bucket):
        state = self.world.buckets.get(bucket)
        if state is None:
            raise client_error("NoSuchBucket", 404)
        return state

    def _allowed(self, operation, bucket, state, step):
        allowed = step != "deny" and (not self.conditioned or (
            state.tags.get(MANAGED) == "true" and state.abac == "Enabled"))
        if not allowed:
            self.world.denials.append((operation, bucket))
        return allowed

    # tagDiscovery: answered whatever the bucket's ABAC status
    def get_bucket_tagging(self, Bucket):
        self._enter("get_bucket_tagging", Bucket)
        state = self._state(Bucket)
        if not state.tags:
            raise client_error("NoSuchTagSet", 404, "GetBucketTagging")
        return {"TagSet": [{"Key": key, "Value": value} for key, value in state.tags.items()]}

    def get_bucket_abac(self, Bucket):
        self._enter("get_bucket_abac", Bucket)
        return {"AbacStatus": {"Status": self._state(Bucket).abac}}

    def put_bucket_abac(self, Bucket, AbacStatus):
        self._enter("put_bucket_abac", Bucket)
        self._state(Bucket).abac = AbacStatus["Status"]
        return {}

    # conditioned bucket-level actions
    def head_bucket(self, Bucket):
        step = self._enter("head_bucket", Bucket)
        state = self._state(Bucket)
        headers = {"x-amz-bucket-region": state.region}
        if step == "no-header":
            raise client_error("403", 403, "HeadBucket")
        if not self._allowed("head_bucket", Bucket, state, step):
            raise client_error("403", 403, "HeadBucket", headers=headers)
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": headers}}

    def list_objects_v2(self, Bucket, MaxKeys=None):
        step = self._enter("list_objects_v2", Bucket)
        state = self._state(Bucket)
        if not self._allowed("list_objects_v2", Bucket, state, step):
            raise client_error("AccessDenied", 403, "ListObjectsV2")
        return {"KeyCount": 0}

    # create_s3_bucket's own client
    def create_bucket(self, Bucket, CreateBucketConfiguration=None):
        self._enter("create_bucket", Bucket)
        configuration = CreateBucketConfiguration or {}
        self.world.add(Bucket, tags={tag["Key"]: tag["Value"] for tag in configuration.get("Tags", [])},
                       region=configuration.get("LocationConstraint") or "us-east-1")
        return {}

    def __getattr__(self, name):              # any other S3 call is a test failure
        raise AssertionError(f"unexpected S3 call {name}")


class Recorder:
    def __init__(self):
        self.calls: List[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class AuditLog:
    """Recording `log_audit_event` (no table write)."""

    def __init__(self):
        self.rows: List[Dict] = []

    def __call__(self, user_id, action, resource_type, resource_id, result,
                 details=None, identity=None, event=None):
        self.rows.append({"user_id": user_id, "action": action, "resource_type": resource_type,
                          "resource_id": resource_id, "result": result,
                          "details": details or {}, "event": event})

    def of(self, action, **match):
        return [row for row in self.rows if row["action"] == action
                and all(row.get(key) == value for key, value in match.items())]


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    return shared_utils


@pytest.fixture(scope="module")
def uc(aws_stack, shared):
    sys.modules.pop("usecases", None)
    import usecases
    return usecases


@pytest.fixture(scope="module")
def dm(aws_stack, shared):
    sys.modules.pop("data_management", None)
    import data_management
    return data_management


class Env:
    def __init__(self, stack, shared, uc, dm, monkeypatch):
        self.stack, self.shared, self.uc, self.dm = stack, shared, uc, dm
        self.monkeypatch = monkeypatch
        self.tables = stack.tables
        self.world = World()
        self.audit = AuditLog()
        self.sleeps = Recorder()
        self.assumes: List[tuple] = []
        self.sts = Recorder()

        real_create = shared.create_boto3_client
        world = self.world

        def create_boto3_client(service, credentials, region=None):
            if service != "s3":
                return real_create(service, credentials, region)
            if credentials.get("is_default_credentials"):
                who, conditioned = "execution", True
            else:
                who = credentials["role_arn"]
                conditioned = not who.endswith(":role/DDAPortalDataAccessRole")
            world.factory_calls.append((who, region))
            return FakeS3(world, who, region, conditioned)

        def assume_usecase_role(role_arn, external_id, session_name):
            self.assumes.append((role_arn, external_id, session_name))
            if isinstance(role_arn, str) and role_arn.endswith(":root"):
                return {"AccessKeyId": None, "SecretAccessKey": None, "SessionToken": None,
                        "is_default_credentials": True}
            return {"AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test",
                    "role_arn": role_arn}

        monkeypatch.setattr(shared, "create_boto3_client", create_boto3_client)
        monkeypatch.setattr(shared, "assume_usecase_role", assume_usecase_role)
        monkeypatch.setattr(shared.time, "sleep", self.sleeps)
        for module in (shared, uc, dm):
            monkeypatch.setattr(module, "log_audit_event", self.audit)
        sts = self.sts
        monkeypatch.setattr(dm, "sts", SimpleNamespace(
            assume_role=lambda **kwargs: sts(**kwargs) or {"Credentials": {
                "AccessKeyId": "test", "SecretAccessKey": "test", "SessionToken": "test"}}))

    # ------------------------------------------------------------ buckets
    def bucket(self, prefix="b", *, tags=MANAGED_TAGS, abac="Disabled", region="us-east-1"):
        return self.world.add(f"attach-{prefix}-{uuid.uuid4().hex[:10]}", tags=tags, abac=abac,
                              region=region)

    def enabled_bucket(self, prefix="on"):
        return self.bucket(prefix, abac="Enabled")

    # -------------------------------------------------------------- users
    def user(self, claim, usecase_id=None, registry_role=None):
        sub = f"user-{uuid.uuid4().hex[:12]}"
        if registry_role:
            self.tables.user_roles.put_item(Item={
                "user_id": sub, "usecase_id": usecase_id, "role": registry_role,
                "username": sub, "email": f"{sub}@example.com", "status": "enabled",
                "assigned_by": "test", "assigned_at": 1})
        return {"user_id": sub, "email": f"{sub}@example.com", "username": sub, "role": claim}

    def portal_admin(self):
        return self.user("PortalAdmin", "global", "PortalAdmin")

    def usecase_admin(self, usecase_id):
        return self.user("UseCaseAdmin", usecase_id, "UseCaseAdmin")

    def viewer(self, usecase_id):
        return self.user("Viewer", usecase_id, "Viewer")

    # ------------------------------------------------------------ records
    def put_record(self, **fields):
        """A single-account record (the execution role reaches its buckets)."""
        usecase_id = f"uc-{uuid.uuid4().hex[:12]}"
        item = {"usecase_id": usecase_id, "name": f"Use case {usecase_id}", "account_id": PORTAL,
                "cross_account_role_arn": ROOT_ARN, "region": "us-east-1"}
        item["s3_bucket"] = fields.pop("s3_bucket", None) or self.enabled_bucket("cfg")
        item.update(fields)
        self.tables.usecases.put_item(Item=item)
        return usecase_id

    def put_multi_account_record(self):
        return self.put_record(account_id=TRUSTED, cross_account_role_arn=ACCESS_ROLE,
                               external_id="ext-usecase")

    def record(self, usecase_id):
        return self.tables.usecases.get_item(Key={"usecase_id": usecase_id}).get("Item")

    def records_named(self, name):
        return [item for item in self.tables.usecases.scan()["Items"] if item.get("name") == name]

    # ------------------------------------------------------------- events
    @staticmethod
    def event(method, path, user, usecase_id=None, body=None):
        return {
            "httpMethod": method, "path": path, "resource": path,
            "pathParameters": {"id": usecase_id} if usecase_id else None,
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {
                "authorizer": {"claims": {
                    "sub": user["user_id"], "email": user["email"],
                    "cognito:username": user["username"], "custom:role": user["role"]}},
                "identity": {"userAgent": "attach-test/1.0"},
            },
        }

    @staticmethod
    def _answer(response):
        return response["statusCode"], json.loads(response["body"])

    def create_usecase(self, user, body):
        body = {"provision_shared_components": False, **body}
        return self._answer(self.uc.handler(self.event("POST", "/usecases", user, body=body), None))

    def put(self, usecase_id, user, body):
        return self._answer(self.uc.handler(
            self.event("PUT", f"/usecases/{usecase_id}", user, usecase_id, body), None))

    def configure(self, usecase_id, user, body):
        return self._answer(self.dm.handler(
            self.event("POST", f"/usecases/{usecase_id}/data/configure", user, usecase_id, body), None))

    def create_bucket(self, user, body):
        return self._answer(self.uc.handler(self.event("POST", "/s3-buckets", user, body=body), None))

    def list_buckets(self, usecase_id, user):
        return self._answer(self.dm.handler(
            self.event("GET", f"/usecases/{usecase_id}/data/buckets", user, usecase_id), None))

    # -------------------------------------------------------------- stubs
    def stub_handler_s3(self):
        """`create_s3_bucket`'s own `boto3.client('s3', region_name=...)` is
        the fake too (the execution role, so conditioned)."""
        import boto3 as real_boto3
        world = self.world

        def client(service, **kwargs):
            if service == "s3":
                return FakeS3(world, "handler", kwargs.get("region_name"), True)
            if service == "s3control":
                raise AssertionError("the tags were confirmed by the read; no fallback expected")
            return real_boto3.client(service, **kwargs)

        self.monkeypatch.setattr(self.uc, "boto3", SimpleNamespace(client=client, resource=real_boto3.resource))

    def abac_rows(self):
        return self.audit.of("bucket_abac_enabled")


@pytest.fixture
def env(aws_stack, shared, uc, dm, monkeypatch):
    monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
    monkeypatch.delenv("DATA_PLANE_SCOPING", raising=False)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL)
    monkeypatch.setenv("TRUSTED_USECASE_ACCOUNT_IDS", TRUSTED)
    return Env(aws_stack, shared, uc, dm, monkeypatch)


def enable_remedy(shared, bucket):
    return shared.REGISTRATION_REMEDY_ENABLE_ABAC.format(bucket=bucket)


# ============================================================ POST /usecases

def test_create_usecase_enables_abac_on_an_external_tagged_s3_bucket(env):
    """The primary-bucket path and the tagDiscovery carve-out: the eligibility
    read and the status read answer while ABAC is off, the conditioned
    HeadBucket is refused until the Attach_Step has enabled ABAC, and then the
    probe reaches the bucket."""
    bucket = env.bucket("primary")
    creator = env.portal_admin()
    name = f"created-{uuid.uuid4().hex[:6]}"
    status, payload = env.create_usecase(creator, {"name": name, "s3_bucket": bucket})
    assert status == 201, payload
    assert payload["message"] == "Use case created successfully"
    usecase_id = payload["usecase"]["usecase_id"]
    assert env.record(usecase_id)["s3_bucket"] == bucket
    assert env.world.ops(bucket) == ["get_bucket_tagging", "head_bucket", "get_bucket_abac",
                                     "put_bucket_abac", "head_bucket", "list_objects_v2"]
    assert env.world.denials == [("head_bucket", bucket)]
    assert env.world.buckets[bucket].abac == "Enabled"
    assert payload["registration"] == {bucket: reached()}
    [row] = env.abac_rows()
    assert (row["user_id"], row["resource_id"], row["result"]) == (creator["user_id"], usecase_id, "success")
    assert row["details"] == {"bucket": bucket, "source": "create_usecase"}
    assert env.sleeps.calls == []
    # The sibling's attach audit row is unchanged.
    assert [r["details"] for r in env.audit.of("s3_bucket_allowed", resource_id=usecase_id)] == [
        {"bucket": bucket, "attribute": "s3_bucket", "source": "create"}]


def test_create_usecase_single_account_data_bucket_is_enabled(env):
    primary, data = env.enabled_bucket("primary"), env.bucket("data")
    status, payload = env.create_usecase(env.portal_admin(), {
        "name": f"created-{uuid.uuid4().hex[:6]}", "s3_bucket": primary, "data_s3_bucket": data})
    assert status == 201, payload
    assert payload["registration"] == {primary: reached(), data: reached()}
    assert env.world.ops(primary).count("put_bucket_abac") == 0      # already enabled
    assert env.world.buckets[data].abac == "Enabled"
    assert [row["details"] for row in env.abac_rows()] == [{"bucket": data, "source": "create_usecase"}]


def test_create_usecase_separate_data_account_bucket_is_left_alone(env):
    primary, data = env.enabled_bucket("primary"), env.bucket("data")
    status, payload = env.create_usecase(env.portal_admin(), {
        "name": f"created-{uuid.uuid4().hex[:6]}", "s3_bucket": primary,
        "data_account_id": DATA, "data_account_role_arn": DATA_ROLE,
        "data_account_external_id": "ext-data", "data_s3_bucket": data})
    assert status == 201, payload
    assert payload["registration"][data] == reached(abac_enabled="unknown")
    assert "get_bucket_abac" not in env.world.ops(data) and "put_bucket_abac" not in env.world.ops(data)
    assert env.world.buckets[data].abac == "Disabled"
    assert env.world.ops(data, who=DATA_ROLE)[-2:] == ["head_bucket", "list_objects_v2"]
    assert (DATA_ROLE, env.shared.ATTACH_SESSION_NAME) in [(arn, session) for arn, _, session in env.assumes]
    assert env.abac_rows() == []


# ============================================================ PUT /usecases/{id}

def test_update_usecase_s3_bucket_change_is_enabled_and_reported(env):
    usecase_id = env.put_record()
    target = env.bucket("moved")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"s3_bucket": target})
    assert status == 200, payload
    assert (payload["added"], payload["already_present"], payload["removed"], payload["not_present"]) == (
        [], [], [], [])
    assert target in payload["allowed_s3_buckets"]
    assert payload["registration"] == {target: reached()}
    assert env.record(usecase_id)["s3_bucket"] == target
    assert env.world.buckets[target].abac == "Enabled"
    assert [row["details"] for row in env.abac_rows()] == [{"bucket": target, "source": "update_usecase"}]


def test_update_usecase_additional_adds_are_enabled_and_reported_per_bucket(env):
    """The allow button's path."""
    usecase_id = env.put_record()
    first, second = env.bucket("xa"), env.bucket("xb")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id),
                              {"additional_s3_buckets_add": [first, second]})
    assert status == 200, payload
    assert payload["added"] == [first, second]
    assert payload["registration"] == {first: reached(), second: reached()}
    assert env.record(usecase_id)["additional_s3_buckets"] == [first, second]
    assert [row["details"]["bucket"] for row in env.abac_rows()] == [first, second]
    assert {row["details"]["source"] for row in env.abac_rows()} == {"update_usecase"}


def test_update_usecase_unchanged_s3_bucket_runs_no_attach_step(env):
    usecase_id = env.put_record()
    current = env.record(usecase_id)["s3_bucket"]
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id),
                              {"s3_bucket": current, "name": "Renamed"})
    assert status == 200, payload
    assert payload["registration"] == {}
    assert env.world.calls == [] and env.assumes == [] and env.abac_rows() == []
    assert env.record(usecase_id)["name"] == "Renamed"


# ============================================ POST /usecases/{id}/data/configure

def test_configure_data_account_changed_bucket_is_enabled_and_reported(env):
    usecase_id = env.put_record()
    data = env.bucket("data")
    status, payload = env.configure(usecase_id, env.usecase_admin(usecase_id), {"data_s3_bucket": data})
    assert status == 200, payload
    assert payload == {"message": "Data account configuration updated", "usecase_id": usecase_id,
                       "registration": {data: reached()}}
    assert env.record(usecase_id)["data_s3_bucket"] == data
    assert [row["details"] for row in env.abac_rows()] == [{"bucket": data, "source": "configure_data_account"}]


def test_configure_data_account_unchanged_or_cleared_bucket_reports_nothing(env):
    data = env.enabled_bucket("data")
    usecase_id = env.put_record(data_s3_bucket=data)
    admin = env.usecase_admin(usecase_id)
    for body in ({"data_s3_bucket": data}, {"data_s3_bucket": ""}):
        status, payload = env.configure(usecase_id, admin, body)
        assert status == 200 and payload["registration"] == {}, payload
    assert env.world.calls == []


# ============================================================ POST /s3-buckets

def test_create_s3_bucket_auto_add_finds_abac_already_enabled(env):
    env.stub_handler_s3()
    usecase_id = env.put_record()
    name = f"attach-new-{uuid.uuid4().hex[:10]}"
    status, payload = env.create_bucket(env.usecase_admin(usecase_id), {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    bucket = payload["bucket"]
    assert (bucket["abac"], bucket["added_to_usecase"]) == ("enabled", True)
    assert {key: bucket[key] for key in ("tagged", "abac_enabled", "iam_reachable", "remedy")} == reached()
    assert env.world.ops(name).count("put_bucket_abac") == 1                 # the creation-time enable only
    assert env.world.ops(name, who="execution") == ["head_bucket", "get_bucket_abac",
                                                    "head_bucket", "list_objects_v2"]
    assert env.abac_rows() == []                                            # already_enabled writes no row
    assert env.record(usecase_id)["additional_s3_buckets"] == [name]


def test_create_s3_bucket_failed_creation_enable_is_completed_by_the_attach_step(env):
    env.stub_handler_s3()
    env.world.plan("put_bucket_abac", client_error("AccessDenied", 403, "PutBucketAbac"))
    usecase_id = env.put_record()
    name = f"attach-new-{uuid.uuid4().hex[:10]}"
    status, payload = env.create_bucket(env.usecase_admin(usecase_id), {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    bucket = payload["bucket"]
    assert bucket["abac"] == "failed" and bucket["abac_enabled"] is True
    assert (bucket["tagged"], bucket["iam_reachable"], bucket["remedy"]) == (True, True, None)
    assert [(who, op) for who, _, op, b in env.world.calls if op == "put_bucket_abac"] == [
        ("handler", "put_bucket_abac"), ("execution", "put_bucket_abac")]
    [row] = env.abac_rows()
    assert (row["result"], row["details"]) == ("success", {"bucket": name, "source": "create_s3_bucket"})


@pytest.mark.parametrize("plan, expected, sleeps", [
    ([], (True, None), 0),
    (["deny", "deny"], (False, "enable"), 1),
], ids=["allowed", "denied-twice"])
def test_create_s3_bucket_without_usecase_probes_on_the_handler_client(env, plan, expected, sleeps):
    """The wizard path: no record, so no Attach_Step; the handler fills the
    four fields from one direct `_probe` on its own client, never null."""
    env.stub_handler_s3()
    env.world.plan("list_objects_v2", *plan)
    name = f"attach-wizard-{uuid.uuid4().hex[:10]}"
    status, payload = env.create_bucket(env.portal_admin(), {"name": name})
    assert status == 201, payload
    bucket = payload["bucket"]
    reachable, remedy = expected
    assert (bucket["tagged"], bucket["abac_enabled"], bucket["iam_reachable"]) == (True, True, reachable)
    assert bucket["remedy"] == (enable_remedy(env.shared, name) if remedy else None)
    assert env.world.factory_calls == []                     # the shared layer built no client
    probe = ["head_bucket", "list_objects_v2"] * (1 + sleeps)
    assert env.world.ops(name, who="handler") == ["create_bucket", "put_bucket_abac", "get_bucket_tagging",
                                                  *probe]
    assert env.sleeps.calls == [((env.shared.ATTACH_PROBE_RETRY_DELAY_SECONDS,), {})] * sleeps


# ============================================ refusals, failures, remedies

@pytest.mark.parametrize("writer", ["create_usecase", "update_usecase", "configure_data_account"])
def test_refused_attach_makes_no_abac_call_and_writes_nothing(env, writer):
    untagged = env.bucket("untagged", tags={})
    if writer == "create_usecase":
        name = f"refused-{uuid.uuid4().hex[:6]}"
        status, payload = env.create_usecase(env.portal_admin(), {"name": name, "s3_bucket": untagged})
        assert env.records_named(name) == []
    else:
        usecase_id = env.put_record()
        before = env.record(usecase_id)
        admin = env.usecase_admin(usecase_id)
        if writer == "update_usecase":
            status, payload = env.put(usecase_id, admin, {"additional_s3_buckets_add": [untagged]})
        else:
            status, payload = env.configure(usecase_id, admin, {"data_s3_bucket": untagged})
        assert env.record(usecase_id) == before
    assert status == 400, payload
    assert (payload["error_code"], payload["reason"], payload["bucket"]) == ("bucket_not_eligible", "no_tags", untagged)
    assert "registration" not in payload
    assert set(env.world.ops(untagged)) == {"get_bucket_tagging"}
    assert env.abac_rows() == []


def test_put_bucket_abac_failure_still_writes_the_record(env):
    env.world.plan("put_bucket_abac", client_error("AccessDenied", 403, "PutBucketAbac"))
    usecase_id = env.put_record()
    bucket = env.bucket("denied")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    assert env.record(usecase_id)["additional_s3_buckets"] == [bucket]
    assert payload["registration"] == {bucket: reached(
        enable_remedy(env.shared, bucket), abac_enabled=False, iam_reachable=False)}
    [row] = env.abac_rows()
    assert row["result"] == "failure" and row["details"]["reason"].startswith("ClientError")
    assert env.sleeps.calls == []


FAILURES = {
    # failing call -> (the bucket's ABAC status, the plan)
    "get_bucket_abac": ("Disabled", ("get_bucket_abac", client_error("AccessDenied", 403, "GetBucketAbac"))),
    "region": ("Disabled", ("head_bucket", "no-header")),
    "head_bucket": ("Enabled", ("head_bucket", None, EndpointConnectionError(endpoint_url="fake-endpoint"))),
    "list_objects_v2": ("Enabled", ("list_objects_v2", client_error("InternalError", 500, "ListObjectsV2"))),
}


@pytest.mark.parametrize("writer", ["create_usecase", "update_usecase", "configure_data_account",
                                    "create_s3_bucket"])
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_failing_calls_degrade_without_a_500(env, failure, writer):
    """One failing call each: the writer answers its success code, the record
    is written and the report degrades (design I5)."""
    abac, (operation, *steps) = FAILURES[failure]
    env.world.plan(operation, *steps)
    if writer == "create_s3_bucket":
        env.stub_handler_s3()
        usecase_id = env.put_record()
        bucket = f"attach-new-{uuid.uuid4().hex[:10]}"
        status, payload = env.create_bucket(env.usecase_admin(usecase_id), {"name": bucket, "usecase_id": usecase_id})
        assert status == 201, payload
        report = {key: payload["bucket"][key] for key in ("tagged", "abac_enabled", "iam_reachable", "remedy")}
        assert env.record(usecase_id)["additional_s3_buckets"] == [bucket]
        # The creation-time enable succeeded, so a failed status read or
        # region still leaves the bucket enabled and reachable.
        abac_enabled, reachable = True, failure in ("get_bucket_abac", "region")
    else:
        bucket = env.bucket("failing", abac=abac)
        if writer == "create_usecase":
            status, payload = env.create_usecase(env.portal_admin(), {
                "name": f"created-{uuid.uuid4().hex[:6]}", "s3_bucket": bucket})
            assert status == 201, payload
            assert env.record(payload["usecase"]["usecase_id"])["s3_bucket"] == bucket
        elif writer == "update_usecase":
            usecase_id = env.put_record()
            status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
            assert status == 200, payload
            assert env.record(usecase_id)["additional_s3_buckets"] == [bucket]
        else:
            usecase_id = env.put_record()
            status, payload = env.configure(usecase_id, env.usecase_admin(usecase_id), {"data_s3_bucket": bucket})
            assert status == 200, payload
            assert env.record(usecase_id)["data_s3_bucket"] == bucket
        report = payload["registration"][bucket]
        abac_enabled, reachable = abac == "Enabled", False
    assert report == reached(None if reachable else enable_remedy(env.shared, bucket),
                             abac_enabled=abac_enabled, iam_reachable=reachable)
    rows = env.abac_rows()
    if failure in ("get_bucket_abac", "region"):
        [row] = rows
        assert row["result"] == "failure"
        assert row["details"]["reason"].startswith("RegionUnknown" if failure == "region" else "ClientError")
    else:
        assert rows == []


def test_created_by_only_bucket_is_untagged_with_the_tag_remedy(env):
    usecase_id = env.put_record()
    bucket = env.bucket("created-by", tags={CREATED_BY: "portal"})
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    assert payload["added"] == [bucket]
    assert payload["registration"] == {bucket: reached(
        env.shared.REGISTRATION_REMEDY_TAG.format(account=PORTAL, bucket=bucket),
        tagged=False, abac_enabled=False, iam_reachable=False)}
    assert "get_bucket_abac" not in env.world.ops(bucket) and "put_bucket_abac" not in env.world.ops(bucket)
    assert env.abac_rows() == []


def test_cross_account_bucket_is_left_alone_with_the_administrator_remedy(env):
    usecase_id = env.put_multi_account_record()
    bucket = env.bucket("remote")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    assert payload["registration"] == {bucket: reached(
        env.shared.REGISTRATION_REMEDY_CROSS_ACCOUNT.format(bucket=bucket),
        abac_enabled="unknown", iam_reachable=False)}
    assert "get_bucket_abac" not in env.world.ops(bucket) and "put_bucket_abac" not in env.world.ops(bucket)
    assert env.world.ops(bucket, who=ACCESS_ROLE) == ["get_bucket_tagging", "head_bucket"]
    assert env.abac_rows() == []


@pytest.mark.parametrize("scoping", ["tag-objects-only", "tag"])
def test_redeploy_remedy_only_under_tag_objects_only(env, scoping):
    env.monkeypatch.setenv("DATA_PLANE_SCOPING", scoping)
    env.world.plan("list_objects_v2", "deny")
    usecase_id = env.put_record()
    bucket = env.enabled_bucket("browse")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    remedy = payload["registration"][bucket]["remedy"]
    if scoping == "tag-objects-only":
        assert remedy == env.shared.REGISTRATION_REMEDY_REDEPLOY
    else:
        assert remedy == enable_remedy(env.shared, bucket)
    assert env.sleeps.calls == []                            # not just enabled: no second probe


@pytest.mark.parametrize("plan, reachable", [(["deny"], True), (["deny", "deny"], False)],
                         ids=["denied-once", "denied-twice"])
def test_one_retry_probe_after_the_attach_step_enabled_abac(env, plan, reachable):
    env.world.plan("list_objects_v2", *plan)
    usecase_id = env.put_record()
    bucket = env.bucket("fresh")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    assert payload["registration"][bucket] == reached(
        None if reachable else enable_remedy(env.shared, bucket), iam_reachable=reachable)
    assert env.sleeps.calls == [((env.shared.ATTACH_PROBE_RETRY_DELAY_SECONDS,), {})]
    assert env.world.ops(bucket)[-4:] == ["head_bucket", "list_objects_v2", "head_bucket", "list_objects_v2"]


def test_attach_step_abac_calls_go_to_the_bucket_region(env):
    usecase_id = env.put_record()
    bucket = env.bucket("eu", region="eu-west-1")
    status, payload = env.put(usecase_id, env.usecase_admin(usecase_id), {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    assert payload["registration"][bucket] == reached()
    assert env.world.regions_of("get_bucket_abac", bucket) == ["eu-west-1"]
    assert env.world.regions_of("put_bucket_abac", bucket) == ["eu-west-1"]
    assert env.world.regions_of("head_bucket", bucket) == [None, None]      # the resolver and the probe
    assert env.world.factory_calls == [("execution", None), ("execution", None),
                                       ("execution", "eu-west-1"), ("execution", None)]


# ============================================ GET /usecases/{id}/data/buckets

class FakeTagging:
    def __init__(self, names):
        self.names = names

    def get_paginator(self, name):
        assert name == "get_resources"
        return self

    def paginate(self, **kwargs):
        return [{"ResourceTagMappingList": [
            {"ResourceARN": f"arn:aws:s3:::{name}", "Tags": [{"Key": MANAGED, "Value": "true"}]}
            for name in self.names]}]


class LocationS3:
    """The listing's s3 client: `answers` maps a bucket to a LocationConstraint
    or an exception."""

    def __init__(self, answers):
        self.answers = answers

    def get_bucket_location(self, Bucket):
        answer = self.answers.get(Bucket)
        if isinstance(answer, BaseException):
            raise answer
        return {"LocationConstraint": answer}


def stub_listing(env, names, answers):
    import boto3 as real_boto3
    tagging, s3 = FakeTagging(names), LocationS3(answers)

    def client(service, **kwargs):
        if service == "resourcegroupstaggingapi":
            return tagging
        if service == "s3":
            return s3
        return real_boto3.client(service, **kwargs)

    env.monkeypatch.setattr(env.dm, "boto3", SimpleNamespace(client=client, resource=real_boto3.resource))


@pytest.mark.parametrize("scoping", ["tag-objects-only", "tag"])
def test_list_buckets_reports_a_denied_location_as_abac_off(env, scoping):
    env.monkeypatch.setenv("DATA_PLANE_SCOPING", scoping)
    usecase_id = env.put_record()
    denied, regional, gone = (f"attach-list-{kind}-{uuid.uuid4().hex[:6]}" for kind in ("d", "r", "g"))
    stub_listing(env, [denied, regional, gone], {
        denied: client_error("AccessDenied", 403, "GetBucketLocation"),
        regional: "eu-west-1",
        gone: client_error("NoSuchBucket", 404, "GetBucketLocation")})
    status, payload = env.list_buckets(usecase_id, env.viewer(usecase_id))
    assert status == 200, payload
    entries = {entry["name"]: entry for entry in payload["buckets"]}
    assert (entries[denied]["region"], entries[denied]["abac_enabled"]) == ("unknown", False)
    expected = (env.shared.REGISTRATION_REMEDY_REDEPLOY if scoping == "tag-objects-only"
                else enable_remedy(env.shared, denied))
    assert entries[denied]["remedy"] == expected
    assert entries[regional]["region"] == "eu-west-1"
    assert "abac_enabled" not in entries[regional] and "remedy" not in entries[regional]
    assert entries[gone]["region"] == "unknown"
    assert "abac_enabled" not in entries[gone] and "remedy" not in entries[gone]


def test_list_buckets_denied_location_through_the_data_account_role(env):
    usecase_id = env.put_record(data_account_id=DATA, data_account_role_arn=DATA_ROLE,
                                data_account_external_id="ext-data")
    denied = f"attach-list-x-{uuid.uuid4().hex[:6]}"
    stub_listing(env, [denied], {denied: client_error("AccessDenied", 403, "GetBucketLocation")})
    status, payload = env.list_buckets(usecase_id, env.viewer(usecase_id))
    assert status == 200, payload
    [entry] = [entry for entry in payload["buckets"] if entry["name"] == denied]
    assert entry["abac_enabled"] is False
    assert entry["remedy"] == env.shared.REGISTRATION_REMEDY_CROSS_ACCOUNT.format(bucket=denied)


# ============================================ configure_data_account grammar

@pytest.mark.parametrize("body, code", [
    ({"data_account_id": "33333333333"}, "INVALID_ACCOUNT_ID"),
    ({"data_account_id": "data-account", "data_account_role_arn": DATA_ROLE}, "INVALID_ACCOUNT_ID"),
    ({"data_account_id": DATA, "data_account_role_arn": f"arn:aws:iam::{DATA}:role/OtherRole"},
     "INVALID_ROLE_ARN"),
    ({"data_account_id": DATA, "data_account_role_arn": f"arn:aws:iam::{TRUSTED}:role/DDAPortalDataAccessRole"},
     "INVALID_ROLE_ARN"),
    ({"data_account_id": DATA}, "INVALID_ROLE_ARN"),
    ({"data_account_role_arn": DATA_ROLE}, "INVALID_ACCOUNT_ID"),
    ({"data_account_external_id": "ext-data"}, "INVALID_ACCOUNT_ID"),
], ids=["eleven-digits", "not-digits", "other-role-name", "other-account", "no-role", "role-without-id",
        "external-id-without-id"])
def test_configure_data_account_grammar_refusals(env, body, code):
    usecase_id = env.put_record()
    before = env.record(usecase_id)
    data = env.bucket("data")
    status, payload = env.configure(usecase_id, env.usecase_admin(usecase_id), {**body, "data_s3_bucket": data})
    assert status == 400, payload
    assert set(payload) == {"error", "error_code"} and payload["error_code"] == code
    assert payload["error"]
    if code == "INVALID_ACCOUNT_ID" and "data_account_id" not in body:
        assert "supply data_account_id" in payload["error"]
    assert env.record(usecase_id) == before                     # no update_item
    assert env.world.calls == [] and env.assumes == [] and env.sts.calls == []


def test_configure_data_account_conforming_triple_is_stored(env):
    usecase_id = env.put_record()
    body = {"data_account_id": DATA, "data_account_role_arn": DATA_ROLE, "data_account_external_id": "ext-data"}
    status, payload = env.configure(usecase_id, env.usecase_admin(usecase_id), body)
    assert status == 200, payload
    assert payload["registration"] == {}
    stored = env.record(usecase_id)
    assert {field: stored[field] for field in body} == body
    assert env.world.calls == [] and env.assumes == [] and env.sts.calls == []


def test_configure_data_account_grammar_applies_only_to_the_triple(env):
    """A body touching only data_s3_bucket gets no grammar, even on a record
    whose stored triple would not pass it; touching the triple does. A data
    account id equal to the record's own account names no separate data
    account, as on creation."""
    usecase_id = env.put_record(data_account_id="legacy-data", data_account_role_arn=DATA_ROLE,
                                data_account_external_id="ext-data")
    admin = env.usecase_admin(usecase_id)
    data = env.bucket("data")
    status, payload = env.configure(usecase_id, admin, {"data_s3_bucket": data})
    assert status == 200, payload
    assert payload["registration"] == {data: reached(abac_enabled="unknown")}
    status, payload = env.configure(usecase_id, admin, {"data_account_external_id": "ext-new"})
    assert (status, payload["error_code"]) == (400, "INVALID_ACCOUNT_ID")
    assert env.record(usecase_id)["data_account_external_id"] == "ext-data"

    own = env.put_record()
    status, payload = env.configure(own, env.usecase_admin(own), {"data_account_id": PORTAL})
    assert status == 200, payload
    assert env.record(own)["data_account_id"] == PORTAL
    assert env.sts.calls == []


# ============================================ the put_bucket_abac call sites

def _backend_sources():
    for root, dirs, files in os.walk(_BACKEND):
        dirs[:] = [d for d in dirs if d not in ("tests", "node_modules", "__pycache__")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_put_bucket_abac_is_called_only_by_the_attach_step_and_create_s3_bucket():
    """Under edge-cv-portal/backend (tests excepted) the operation's name
    appears in exactly two modules, and its calls sit in exactly the
    Attach_Step and create_s3_bucket."""
    naming = set()
    callers = set()
    for path in _backend_sources():
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        if "put_bucket_abac" not in source:
            continue
        naming.add(os.path.relpath(path, _BACKEND))
        module = os.path.splitext(os.path.basename(path))[0]
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                            and inner.func.attr == "put_bucket_abac":
                        callers.add(f"{module}.{node.name}")
    assert naming == {os.path.join("functions", "usecases.py"),
                      os.path.join("layers", "shared", "python", "shared_utils.py")}
    assert callers == {"shared_utils.attach_bucket", "usecases.create_s3_bucket"}
