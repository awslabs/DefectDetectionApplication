"""
API tests for the use-case bucket configuration record writers
(portal-usecase-bucket-config, design §5; tasks.md 6.2).

Run end to end on the moto `aws_stack` fixture with the real shared layer:
the use case, user-role and audit tables, and moto S3 buckets whose tags the
eligibility probe reads with the use case's own credentials (single-account
records resolve to the execution role, so the probe hits moto S3 directly;
the Data Account role path stubs `assume_usecase_role`). What moto cannot
play — `CreateBucketConfiguration.Tags` is not round-tripped, `PutBucketAbac`
/ `TagResource` are recorded only, the Resource Groups Tagging API needs an
absent dependency — is served by a scripted client factory installed on the
`usecases` module.

Covered:
- GET /usecases/{id}: `allowed_s3_buckets` (portal-owned names excluded even
  when the raw record names them) and `additional_s3_buckets` (absent ≡ []);
- PUT /usecases/{id}: idempotent add, remove / `not_present`, 409
  `concurrent_update` through an injected condition failure,
  `unsupported_field`, `invalid_bucket_name`, `invalid_field` (non-list, more
  than 10 names), the 100 cap, portal-owned add refused for a PortalAdmin
  too, other-use-case refusal and the PortalAdmin sharing override with
  `shared_with_usecases`, a stale `dda-portal:usecase-id` claim refused with
  REMEDY_STALE_CLAIM and admitted for a PortalAdmin with `stale_claim`, a
  refusal writes nothing and lists every failing name in `rejected`, a
  deleted record → 404 and no phantom item, an unknown id → 404 and nothing
  written, Clear semantics (absent ≡ None ≡ '' — the Edit modal body), the
  configured-bucket probe only on a change, `test_bulk_add_never_sleeps`,
  the attach audit rows with the actor's attribution;
- POST /usecases: eligibility of `s3_bucket`, `data_s3_bucket` (Data Account
  role path and `credentials_unavailable`) and `additional_s3_buckets`
  (≤ 10, no retry), the bounded read-after-write retry
  (`test_create_usecase_retries_bounded`), the `create_usecase` failure row,
  the `source: 'create'` rows, `tag_bucket_for_portal` performing no write;
- POST /s3-buckets: name grammar, both authorization branches, pre-create
  refusals with the `create_bucket` recorder empty, the call order
  create_bucket(Tags) → put_bucket_abac → get_bucket_tagging, the one
  `tag_resource` fallback and the 500 `bucket_tagging_failed` branch, every
  `Tags` argument exactly the dda-portal:* keys, 409 on an already-owned
  name with nothing tagged, `abac` reporting, auto-add success with the
  `source: 'auto_add'` row and each `add_error` path;
- POST /usecases/{id}/data/configure: 404, Clear / unchanged without a
  probe, a changed bucket probed and audited with `source: 'configure'`;
- GET /s3-buckets and GET .../data/buckets advisory flags.

The moto probe recorded in verification-notes.md: moto 5.1.22 honours
`ReturnValuesOnConditionCheckFailure='ALL_OLD'`, so the `already_present` /
`usecase_not_found` branches of `_add_additional_bucket` run against moto
itself; only the 409 and the write-failure cases inject a ClientError.

Validates: Requirements 8.3, 9.1–9.11, 10.1–10.11, 12.1–12.8, 15.1, 15.2, 15.7
"""
import json
import sys
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

ACCOUNT = "acct-main"
ROOT_ARN = f"arn:aws:iam::{ACCOUNT}:root"
CROSS_ROLE = "arn:aws:iam::222222222222:role/DDAPortalAccessRole"
DATA_ROLE = "arn:aws:iam::333333333333:role/DDAPortalDataAccessRole"
SAGEMAKER_ROLE = "arn:aws:iam::222222222222:role/DDASageMakerExecutionRole"
ARTIFACTS = "test-portal-artifacts"              # conftest's PORTAL_ARTIFACTS_BUCKET
PORTAL_ACCOUNT = "acct-portal"
MANAGED = "dda-portal:managed"
CREATED_BY = "dda-portal:created-by"
CLAIM = "dda-portal:usecase-id"
SOURCE_IP = "203.0.113.9"
# The 201 `bucket` keys this suite pins exactly; the registration fields of
# portal-authorization-and-iam-scoping ride beside them (Requirement 10.4(g)).
SIBLING_BUCKET_KEYS = ("name", "region", "portal_created", "tags", "abac", "added_to_usecase")


# --------------------------------------------------------------- helpers

class Recorder:
    def __init__(self, result=None, raises=None, side_effect=None):
        self.calls: List[Any] = []
        self.result = result
        self.raises = raises
        self.side_effect = side_effect

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.side_effect is not None:
            return self.side_effect(*args, **kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


def client_error(code: str, status: int = 400, operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


def tag_set(**tags) -> List[Dict[str, str]]:
    """{'dda_portal__managed': 'true'} -> [{'Key': 'dda-portal:managed', 'Value': 'true'}]."""
    return [{"Key": key.replace("__", ":").replace("_", "-"), "Value": value}
            for key, value in tags.items()]


def body_of(response) -> Dict:
    return json.loads(response["body"])


class AuditLog:
    """Recording wrapper around `log_audit_event` (installed on the shared
    layer and on both handler modules) that still writes the moto table."""

    def __init__(self, real):
        self.real = real
        self.rows: List[Dict] = []

    def __call__(self, user_id, action, resource_type, resource_id, result,
                 details=None, identity=None, event=None):
        self.rows.append({"user_id": user_id, "action": action, "resource_type": resource_type,
                          "resource_id": resource_id, "result": result,
                          "details": details or {}, "event": event})
        return self.real(user_id, action, resource_type, resource_id, result, details,
                         identity=identity, event=event)

    def of(self, action, **match):
        return [row for row in self.rows if row["action"] == action
                and all(row.get(key) == value for key, value in match.items())]

    def clear(self):
        self.rows.clear()


class ScriptedBucketS3:
    """The `s3` client `create_s3_bucket` builds: records every operation;
    `get_bucket_tagging` plays `script` (a list whose entries are 'sent' —
    echo the tags the handler sent, the fallback's if it ran — a TagSet
    list, an exception, or a callable returning one of those; the last entry
    repeats)."""

    def __init__(self, script=None, create_error=None, abac_error=None):
        self.script = list(script or ["sent"])
        self.create_error = create_error
        self.abac_error = abac_error
        self.calls: List[Any] = []
        self.sent_tags: List[Dict] = []

    def ops(self):
        return [name for name, _ in self.calls]

    def create_bucket(self, **kwargs):
        self.calls.append(("create_bucket", kwargs))
        if self.create_error is not None:
            raise self.create_error
        self.sent_tags = list(kwargs.get("CreateBucketConfiguration", {}).get("Tags", []))
        return {}

    def put_bucket_abac(self, **kwargs):
        self.calls.append(("put_bucket_abac", kwargs))
        if self.abac_error is not None:
            raise self.abac_error
        return {}

    def get_bucket_tagging(self, **kwargs):
        self.calls.append(("get_bucket_tagging", kwargs))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(step):
            step = step()
        if isinstance(step, Exception):
            raise step
        if step == "sent":
            return {"TagSet": list(self.sent_tags)}
        return {"TagSet": step}

    def __getattr__(self, name):          # any other S3 call is a test failure
        raise AssertionError(f"unexpected S3 call {name}")


class FakeS3Control:
    def __init__(self, s3: ScriptedBucketS3, error=None):
        self.s3 = s3
        self.error = error
        self.calls: List[Dict] = []

    def tag_resource(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        self.s3.sent_tags = list(kwargs["Tags"])
        return {}


class FakeTagging:
    """resourcegroupstaggingapi stand-in: `pages` is a list of
    ResourceTagMappingList pages."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: List[Dict] = []

    def get_paginator(self, name):
        assert name == "get_resources"
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return [{"ResourceTagMappingList": page} for page in self.pages]


class ListingS3:
    """The `s3` client `list_s3_buckets` builds."""

    def __init__(self, names):
        self.names = names
        self.calls: List[Any] = []

    def list_buckets(self):
        self.calls.append("list_buckets")
        return {"Buckets": [{"Name": name} for name in self.names]}

    def get_bucket_location(self, Bucket):
        self.calls.append(("get_bucket_location", Bucket))
        return {"LocationConstraint": None}

    def __getattr__(self, name):
        raise AssertionError(f"unexpected S3 call {name}")


class TableProxy:
    """A use-cases table whose selected operations are overridden; everything
    else goes to the real moto table."""

    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return lambda **kwargs: self._overrides[name](self._real, **kwargs)
        return getattr(self._real, name)


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def shared(aws_stack):
    import shared_utils
    assert hasattr(shared_utils, "check_bucket_attachable") and hasattr(
        shared_utils, "RegistryUnavailable"), "this suite needs the real shared layer"
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


class Harness:
    def __init__(self, stack, shared, uc, dm, audit, sleeps, probes, monkeypatch):
        self.stack = stack
        self.shared = shared
        self.uc = uc
        self.dm = dm
        self.audit = audit
        self.sleeps = sleeps
        self.probes = probes
        self.monkeypatch = monkeypatch
        self.tables = stack.tables
        self.s3 = stack.s3

    # ------------------------------------------------------------ names
    @staticmethod
    def name(prefix="b"):
        return f"api-{prefix}-{uuid.uuid4().hex[:10]}"

    # ------------------------------------------------------------ users
    def user(self, claim, usecase_id=None, registry_role=None):
        sub = f"user-{uuid.uuid4().hex[:12]}"
        if registry_role:
            self.tables.user_roles.put_item(Item={
                "user_id": sub, "usecase_id": usecase_id, "role": registry_role,
                "username": sub, "email": f"{sub}@example.com", "status": "enabled",
                "assigned_by": "test", "assigned_at": 1})
        return {"user_id": sub, "email": f"{sub}@example.com", "username": sub, "role": claim}

    def usecase_admin(self, usecase_id):
        return self.user("UseCaseAdmin", usecase_id, "UseCaseAdmin")

    def portal_admin(self):
        return self.user("PortalAdmin", "global", "PortalAdmin")

    def viewer(self, usecase_id):
        return self.user("Viewer", usecase_id, "Viewer")

    # ---------------------------------------------------------- buckets
    def bucket(self, tags=None, prefix="b", exists=True):
        name = self.name(prefix)
        if exists:
            self.s3.create_bucket(Bucket=name)
            if tags:
                self.s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": tags})
        return name

    def tagged_bucket(self, claim=None, prefix="tagged", **extra):
        tags = tag_set(dda_portal__managed="true", **extra)
        if claim:
            tags.append({"Key": CLAIM, "Value": claim})
        return self.bucket(tags, prefix)

    def untagged_bucket(self):
        return self.bucket(None, "untagged")

    def missing_bucket(self):
        return self.bucket(None, "missing", exists=False)

    # ---------------------------------------------------------- records
    def put_usecase(self, **fields):
        usecase_id = f"uc-{uuid.uuid4().hex[:12]}"
        item = {"usecase_id": usecase_id, "name": f"Use case {usecase_id}", "account_id": ACCOUNT,
                "cross_account_role_arn": ROOT_ARN, "region": "us-east-1"}
        item.setdefault("s3_bucket", fields.pop("s3_bucket", None) or self.tagged_bucket(prefix="cfg"))
        item.update(fields)
        self.tables.usecases.put_item(Item=item)
        return usecase_id

    def record(self, usecase_id):
        return self.tables.usecases.get_item(Key={"usecase_id": usecase_id}).get("Item")

    # ----------------------------------------------------------- events
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
                "identity": {"sourceIp": SOURCE_IP, "userAgent": "api-test/1.0"},
            },
        }

    def get(self, usecase_id, user):
        response = self.uc.handler(self.event("GET", f"/usecases/{usecase_id}", user, usecase_id), None)
        return response["statusCode"], body_of(response)

    def put(self, usecase_id, user, body):
        response = self.uc.handler(self.event("PUT", f"/usecases/{usecase_id}", user, usecase_id, body), None)
        return response["statusCode"], body_of(response)

    def delete(self, usecase_id, user):
        response = self.uc.handler(self.event("DELETE", f"/usecases/{usecase_id}", user, usecase_id), None)
        return response["statusCode"], body_of(response)

    def create_usecase(self, user, body):
        body = {"provision_shared_components": False, **body}
        response = self.uc.handler(self.event("POST", "/usecases", user, body=body), None)
        return response["statusCode"], body_of(response)

    def create_bucket(self, user, body):
        response = self.uc.handler(self.event("POST", "/s3-buckets", user, body=body), None)
        return response["statusCode"], body_of(response)

    def list_s3_buckets(self, user):
        response = self.uc.handler(self.event("GET", "/s3-buckets", user), None)
        return response["statusCode"], body_of(response)

    def configure(self, usecase_id, user, body):
        event = self.event("POST", f"/usecases/{usecase_id}/data/configure", user, usecase_id, body)
        response = self.dm.handler(event, None)
        return response["statusCode"], body_of(response)

    # ------------------------------------------------------------ stubs
    def stub_bucket_clients(self, script=None, create_error=None, abac_error=None,
                            tag_resource_error=None, tagging_pages=None, listing=None):
        """Install the scripted client factory on the usecases module; the
        handler's `boto3.client('s3' | 's3control' | 'resourcegroupstaggingapi')`
        calls land here, anything else reaches the real (moto) boto3."""
        import boto3 as real_boto3
        s3 = ListingS3(listing) if listing is not None else ScriptedBucketS3(
            script, create_error=create_error, abac_error=abac_error)
        s3control = FakeS3Control(s3, error=tag_resource_error) if listing is None else None
        tagging = FakeTagging(tagging_pages or [])
        factory = Recorder()

        def client(service, **kwargs):
            factory.calls.append((service, kwargs))
            if service == "s3":
                return s3
            if service == "s3control":
                return s3control
            if service == "resourcegroupstaggingapi":
                return tagging
            return real_boto3.client(service, **kwargs)

        self.monkeypatch.setattr(self.uc, "boto3", SimpleNamespace(
            client=client, resource=real_boto3.resource))
        return SimpleNamespace(s3=s3, s3control=s3control, tagging=tagging, factory=factory)

    def proxy_table(self, module, **overrides):
        """Replace `module.dynamodb` so its use-cases table is a TableProxy."""
        real = self.tables.usecases
        proxy = TableProxy(real, **overrides)
        self.monkeypatch.setattr(module, "dynamodb", SimpleNamespace(Table=lambda _name: proxy))
        return proxy


@pytest.fixture
def h(aws_stack, shared, uc, dm, monkeypatch):
    monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.setenv("TRUSTED_USECASE_ACCOUNT_IDS", "222222222222")
    monkeypatch.delenv("COMPONENT_BUCKET", raising=False)
    audit = AuditLog(shared.log_audit_event)
    for module in (shared, uc, dm):
        monkeypatch.setattr(module, "log_audit_event", audit)
    sleeps = Recorder()
    monkeypatch.setattr(shared.time, "sleep", sleeps)
    real_probe = shared.probe_bucket_eligibility
    probes = Recorder(side_effect=lambda *a, **kw: real_probe(*a, **kw))
    monkeypatch.setattr(shared, "probe_bucket_eligibility", probes)
    return Harness(aws_stack, shared, uc, dm, audit, sleeps, probes, monkeypatch)


def allowed_rows(h, **match):
    return h.audit.of("s3_bucket_allowed", **match)


# ============================================================ GET /usecases/{id}

def test_get_usecase_returns_allowed_and_additional_lists(h):
    data = h.tagged_bucket(prefix="data")
    extra = h.tagged_bucket(prefix="extra")
    usecase_id = h.put_usecase(data_s3_bucket=data, additional_s3_buckets=[extra])
    status, payload = h.get(usecase_id, h.viewer(usecase_id))
    assert status == 200, payload
    record = h.record(usecase_id)
    assert payload["usecase"]["allowed_s3_buckets"] == sorted({record["s3_bucket"], data, extra})
    assert payload["usecase"]["additional_s3_buckets"] == [extra]

    plain = h.put_usecase()
    status, payload = h.get(plain, h.viewer(plain))
    assert status == 200
    assert payload["usecase"]["additional_s3_buckets"] == []
    assert payload["usecase"]["allowed_s3_buckets"] == [h.record(plain)["s3_bucket"]]


def test_get_usecase_portal_owned_record_field_is_returned_but_never_allowed(h):
    usecase_id = h.put_usecase(data_s3_bucket=ARTIFACTS)
    status, payload = h.get(usecase_id, h.viewer(usecase_id))
    assert status == 200
    assert payload["usecase"]["data_s3_bucket"] == ARTIFACTS          # raw field, as stored
    assert ARTIFACTS not in payload["usecase"]["allowed_s3_buckets"]
    assert payload["usecase"]["allowed_s3_buckets"] == [h.record(usecase_id)["s3_bucket"]]


# ============================================================ PUT /usecases/{id}

def test_update_add_is_idempotent_and_audited(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    bucket = h.tagged_bucket()
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_add": [bucket]})
    assert status == 200, payload
    assert payload["added"] == [bucket] and payload["already_present"] == []
    assert payload["removed"] == [] and payload["not_present"] == []
    assert bucket in payload["allowed_s3_buckets"]
    assert h.record(usecase_id)["additional_s3_buckets"] == [bucket]
    rows = allowed_rows(h, user_id=admin["user_id"], resource_id=usecase_id)
    assert [row["details"] for row in rows] == [
        {"bucket": bucket, "attribute": "additional_s3_buckets", "source": "api"}]
    assert rows[0]["result"] == "success" and rows[0]["resource_type"] == "usecase"
    assert rows[0]["event"] is not None                      # request attribution rides along

    h.audit.clear()
    h.probes.calls.clear()
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_add": [bucket]})
    assert status == 200 and payload["already_present"] == [bucket] and payload["added"] == []
    assert h.record(usecase_id)["additional_s3_buckets"] == [bucket]
    assert allowed_rows(h) == [] and h.probes.calls == []     # present: no probe, no row


def test_update_remove_then_not_present(h):
    bucket = h.tagged_bucket()
    usecase_id = h.put_usecase(additional_s3_buckets=[bucket])
    admin = h.usecase_admin(usecase_id)
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_remove": [bucket]})
    assert status == 200, payload
    assert payload["removed"] == [bucket] and bucket not in payload["allowed_s3_buckets"]
    assert h.record(usecase_id)["additional_s3_buckets"] == []
    removed = h.audit.of("s3_bucket_removed", user_id=admin["user_id"])
    assert [row["details"] for row in removed] == [
        {"bucket": bucket, "attribute": "additional_s3_buckets", "source": "api"}]

    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_remove": [bucket]})
    assert status == 200 and payload["not_present"] == [bucket] and payload["removed"] == []


def test_update_remove_concurrent_update_is_409(h):
    bucket = h.tagged_bucket()
    usecase_id = h.put_usecase(additional_s3_buckets=[bucket])
    admin = h.usecase_admin(usecase_id)
    attempts = []

    def conflicting_update(real, **kwargs):
        if ":expected" in kwargs.get("ExpressionAttributeValues", {}):
            attempts.append(kwargs)
            raise client_error("ConditionalCheckFailedException", 400, "UpdateItem")
        return real.update_item(**kwargs)

    h.proxy_table(h.uc, update_item=conflicting_update)
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_remove": [bucket]})
    assert status == 409, payload
    assert payload["error_code"] == "concurrent_update" and payload["bucket"] == bucket
    assert payload["applied"] == {"added": [], "removed": []}
    assert len(attempts) == 2                                  # one retry, then give up
    assert h.record(usecase_id)["additional_s3_buckets"] == [bucket]


def test_update_raw_additional_list_is_unsupported_field(h):
    usecase_id = h.put_usecase()
    before = h.record(usecase_id)
    status, payload = h.put(usecase_id, h.usecase_admin(usecase_id),
                            {"additional_s3_buckets": [h.tagged_bucket()], "name": "renamed"})
    assert status == 400 and payload["error_code"] == "unsupported_field"
    assert payload["field"] == "additional_s3_buckets"
    assert h.record(usecase_id) == before


@pytest.mark.parametrize("field", ["additional_s3_buckets_add", "additional_s3_buckets_remove"])
def test_update_list_validation(h, field):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    before = h.record(usecase_id)

    status, payload = h.put(usecase_id, admin, {field: ["fine-bucket", "bad name!"]})
    assert status == 400 and payload["error_code"] == "invalid_bucket_name"
    assert payload["bucket"] == "bad name!"

    status, payload = h.put(usecase_id, admin, {field: "not-a-list"})
    assert status == 400 and payload["error_code"] == "invalid_field" and payload["field"] == field

    status, payload = h.put(usecase_id, admin, {field: [f"bucket-{i}" for i in range(11)]})
    assert status == 400 and payload["error_code"] == "invalid_field" and payload["field"] == field
    assert "10" in payload["error"]

    status, payload = h.put(usecase_id, admin, {field: [None]})
    assert status == 400 and payload["error_code"] == "invalid_bucket_name" and "bucket" not in payload

    assert h.record(usecase_id) == before and h.probes.calls == []


def test_update_total_cap_of_100_checked_before_any_probe(h):
    current = [f"existing-bucket-{i:03d}" for i in range(100)]
    usecase_id = h.put_usecase(additional_s3_buckets=current)
    admin = h.usecase_admin(usecase_id)
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_add": [h.tagged_bucket()]})
    assert status == 400, payload
    assert (payload["error_code"], payload["reason"]) == ("bucket_not_eligible", "too_many_buckets")
    assert payload["remedy"] == h.shared.REMEDY_TOO_MANY
    assert h.probes.calls == [] and h.record(usecase_id)["additional_s3_buckets"] == current

    # Removing one in the same request makes room.
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_add": [h.tagged_bucket()],
                                                "additional_s3_buckets_remove": [current[0]]})
    assert status == 200, payload
    assert len(h.record(usecase_id)["additional_s3_buckets"]) == 100


def test_update_portal_owned_add_is_refused_for_everyone(h):
    usecase_id = h.put_usecase()
    for user in (h.usecase_admin(usecase_id), h.portal_admin()):
        h.audit.clear()
        status, payload = h.put(usecase_id, user, {"additional_s3_buckets_add": [ARTIFACTS]})
        assert status == 400, payload
        assert (payload["error_code"], payload["reason"]) == ("bucket_not_eligible", "portal_bucket")
        assert payload["remedy"] == h.shared.REMEDY_PORTAL_BUCKET
        assert payload["rejected"] == [{"bucket": ARTIFACTS, "error_code": "bucket_not_eligible",
                                        "reason": "portal_bucket"}]
        refused = h.audit.of("s3_bucket_refused", user_id=user["user_id"])
        assert [row["details"] for row in refused] == [
            {"bucket": ARTIFACTS, "purpose": "allow-additional-bucket", "reason": "portal_bucket"}]
        assert h.probes.calls == []
    assert "additional_s3_buckets" not in h.record(usecase_id)


def test_update_other_usecase_refused_then_shared_by_portal_admin(h):
    shared_bucket = h.tagged_bucket(prefix="shared")
    h.put_usecase(s3_bucket=shared_bucket)                     # configured elsewhere
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_add": [shared_bucket]})
    assert status == 400, payload
    assert payload["reason"] == "bucket_configured_on_other_usecase"
    assert payload["remedy"] == h.shared.REMEDY_OTHER_USECASE
    assert "additional_s3_buckets" not in h.record(usecase_id)

    portal_admin = h.portal_admin()
    status, payload = h.put(usecase_id, portal_admin, {"additional_s3_buckets_add": [shared_bucket]})
    assert status == 200, payload
    assert payload["added"] == [shared_bucket]
    rows = allowed_rows(h, user_id=portal_admin["user_id"])
    assert [row["details"] for row in rows] == [{
        "bucket": shared_bucket, "attribute": "additional_s3_buckets", "source": "api",
        "shared_with_usecases": 1}]


def test_update_stale_claim_refused_then_admitted_by_portal_admin(h):
    stale = h.tagged_bucket(claim=f"uc-deleted-{uuid.uuid4().hex[:8]}", prefix="stale")
    usecase_id = h.put_usecase()
    status, payload = h.put(usecase_id, h.usecase_admin(usecase_id),
                            {"additional_s3_buckets_add": [stale]})
    assert status == 400, payload
    assert payload["reason"] == "bucket_configured_on_other_usecase"
    assert payload["remedy"] == h.shared.REMEDY_STALE_CLAIM
    assert "additional_s3_buckets" not in h.record(usecase_id)

    portal_admin = h.portal_admin()
    status, payload = h.put(usecase_id, portal_admin, {"additional_s3_buckets_add": [stale]})
    assert status == 200, payload
    rows = allowed_rows(h, user_id=portal_admin["user_id"])
    assert [row["details"] for row in rows] == [{
        "bucket": stale, "attribute": "additional_s3_buckets", "source": "api",
        "shared_with_usecases": 1, "stale_claim": True}]


def test_update_live_claim_by_another_usecase_is_refused_for_usecase_admin(h):
    other = h.put_usecase()
    claimed = h.tagged_bucket(claim=other, prefix="claimed")
    usecase_id = h.put_usecase()
    status, payload = h.put(usecase_id, h.usecase_admin(usecase_id),
                            {"additional_s3_buckets_add": [claimed]})
    assert status == 400 and payload["remedy"] == h.shared.REMEDY_OTHER_USECASE


def test_update_eligibility_refusal_writes_nothing_and_lists_every_name(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    before = h.record(usecase_id)
    untagged, missing, tagged = h.untagged_bucket(), h.missing_bucket(), h.tagged_bucket()
    status, payload = h.put(usecase_id, admin, {
        "name": "should not apply", "additional_s3_buckets_add": [untagged, missing, tagged]})
    assert status == 400, payload
    assert (payload["error_code"], payload["reason"], payload["bucket"]) == (
        "bucket_not_eligible", "no_tags", untagged)                 # the first failure's body
    assert payload["remedy"] == h.shared.REMEDY_TAG
    assert payload["rejected"] == [
        {"bucket": untagged, "error_code": "bucket_not_eligible", "reason": "no_tags"},
        {"bucket": missing, "error_code": "bucket_not_eligible", "reason": "bucket_not_found"}]
    assert len(h.probes.calls) == 3                            # every add is probed, once
    assert h.record(usecase_id) == before                      # neither the name nor the list
    assert allowed_rows(h) == []


def test_update_deleted_record_is_404_and_no_phantom_record(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    bucket = h.tagged_bucket()

    def read_then_delete(real, **kwargs):
        response = real.get_item(**kwargs)
        real.delete_item(Key=kwargs["Key"])
        return response

    h.proxy_table(h.uc, get_item=read_then_delete)
    status, payload = h.put(usecase_id, admin, {"name": "gone", "additional_s3_buckets_add": [bucket]})
    assert status == 404 and payload["error_code"] == "usecase_not_found"
    assert h.record(usecase_id) is None
    assert allowed_rows(h) == []


def test_update_unknown_usecase_is_404_and_writes_nothing(h):
    unknown = f"uc-unknown-{uuid.uuid4().hex[:8]}"
    count_before = h.tables.usecases.scan(Select="COUNT")["Count"]
    status, payload = h.put(unknown, h.portal_admin(), {"name": "phantom", "s3_bucket": h.tagged_bucket()})
    assert status == 404 and payload["error_code"] == "usecase_not_found"
    assert h.record(unknown) is None
    assert h.tables.usecases.scan(Select="COUNT")["Count"] == count_before
    assert h.probes.calls == []


def test_edit_modal_body_without_data_bucket(h):
    """UseCases.tsx sends data_s3_bucket: useCase.data_s3_bucket || '' — for a
    use case without a data bucket that is unchanged: 200, no probe."""
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    record = h.record(usecase_id)
    status, payload = h.put(usecase_id, admin, {
        "name": record["name"], "description": "edited", "s3_bucket": record["s3_bucket"],
        "data_s3_bucket": "", "region": "us-east-1"})
    assert status == 200, payload
    assert h.probes.calls == [] and allowed_rows(h) == []
    after = h.record(usecase_id)
    assert after["description"] == "edited" and after["data_s3_bucket"] == ""
    assert after["s3_bucket"] == record["s3_bucket"]


def test_update_clear_of_data_bucket_is_stored_as_today_without_a_probe(h):
    data = h.tagged_bucket(prefix="data")
    usecase_id = h.put_usecase(data_s3_bucket=data)
    admin = h.usecase_admin(usecase_id)
    for clear in ("", "   ", None):
        status, payload = h.put(usecase_id, admin, {"data_s3_bucket": clear})
        assert status == 200, payload
        assert h.record(usecase_id).get("data_s3_bucket") == clear
    assert h.probes.calls == [] and allowed_rows(h) == []


def test_update_s3_bucket_clear_is_invalid(h):
    usecase_id = h.put_usecase()
    before = h.record(usecase_id)
    for clear in ("", None, "  "):
        status, payload = h.put(usecase_id, h.usecase_admin(usecase_id), {"s3_bucket": clear})
        assert status == 400 and payload["error_code"] == "invalid_bucket_name"
    assert h.record(usecase_id) == before and h.probes.calls == []


def test_update_non_string_bucket_is_400_not_500(h):
    usecase_id = h.put_usecase()
    status, payload = h.put(usecase_id, h.usecase_admin(usecase_id), {"s3_bucket": 42})
    assert status == 400 and payload["error_code"] == "invalid_bucket_name" and "bucket" not in payload
    status, payload = h.put(usecase_id, h.usecase_admin(usecase_id), {"additional_s3_buckets_add": [["x"]]})
    assert status == 400 and payload["error_code"] == "invalid_bucket_name"


def test_update_configured_bucket_change_is_probed_once_and_audited(h):
    data = h.tagged_bucket(prefix="data")
    usecase_id = h.put_usecase(data_s3_bucket=data)
    admin = h.usecase_admin(usecase_id)
    record = h.record(usecase_id)
    new_main, new_data = h.tagged_bucket(prefix="main"), h.tagged_bucket(prefix="data")

    # Unchanged values: no probe at all.
    status, _ = h.put(usecase_id, admin, {"s3_bucket": record["s3_bucket"], "data_s3_bucket": data})
    assert status == 200 and h.probes.calls == []

    status, payload = h.put(usecase_id, admin, {"s3_bucket": new_main, "data_s3_bucket": new_data})
    assert status == 200, payload
    assert [call[0][1] for call in h.probes.calls] == [new_main, new_data]
    assert set(payload["allowed_s3_buckets"]) == {new_main, new_data}
    rows = allowed_rows(h, user_id=admin["user_id"])
    assert [row["details"] for row in rows] == [
        {"bucket": new_main, "attribute": "s3_bucket", "source": "api"},
        {"bucket": new_data, "attribute": "data_s3_bucket", "source": "api"}]

    # A refused configured bucket writes nothing and names itself in rejected.
    untagged = h.untagged_bucket()
    status, payload = h.put(usecase_id, admin, {"s3_bucket": untagged, "name": "nope"})
    assert status == 400 and payload["purpose"] == "configure-s3-bucket"
    assert payload["rejected"] == [{"bucket": untagged, "error_code": "bucket_not_eligible", "reason": "no_tags"}]
    after = h.record(usecase_id)
    assert after["s3_bucket"] == new_main and after["name"] == record["name"]


def test_update_audit_row_portal_admin_repoints_s3_bucket_onto_shared_bucket(h):
    shared_bucket = h.tagged_bucket(prefix="shared")
    h.put_usecase(s3_bucket=shared_bucket)
    usecase_id = h.put_usecase()
    portal_admin = h.portal_admin()
    status, payload = h.put(usecase_id, portal_admin, {"s3_bucket": shared_bucket})
    assert status == 200, payload
    rows = allowed_rows(h, user_id=portal_admin["user_id"], resource_id=usecase_id)
    assert [row["details"] for row in rows] == [{
        "bucket": shared_bucket, "attribute": "s3_bucket", "source": "api", "shared_with_usecases": 1}]
    # The same change by a UseCaseAdmin of another use case is refused.
    third = h.put_usecase()
    status, payload = h.put(third, h.usecase_admin(third), {"s3_bucket": shared_bucket})
    assert status == 400 and payload["reason"] == "bucket_configured_on_other_usecase"
    assert payload["remedy"] == h.shared.REMEDY_OTHER_USECASE


def test_update_audit_row_portal_admin_repoints_data_bucket_onto_stale_claim(h):
    stale = h.tagged_bucket(claim=f"uc-gone-{uuid.uuid4().hex[:8]}", prefix="stale")
    usecase_id = h.put_usecase()
    portal_admin = h.portal_admin()
    status, payload = h.put(usecase_id, portal_admin, {"data_s3_bucket": stale})
    assert status == 200, payload
    rows = allowed_rows(h, user_id=portal_admin["user_id"], resource_id=usecase_id)
    assert [row["details"] for row in rows] == [{
        "bucket": stale, "attribute": "data_s3_bucket", "source": "api",
        "shared_with_usecases": 1, "stale_claim": True}]


def test_bulk_add_never_sleeps(h):
    usecase_id = h.put_usecase()
    names = [h.untagged_bucket() for _ in range(10)]
    status, payload = h.put(usecase_id, h.usecase_admin(usecase_id), {"additional_s3_buckets_add": names})
    assert status == 400, payload
    assert h.sleeps.calls == []
    assert len(h.probes.calls) == 10
    assert all(call[1] == {"retry": False} for call in h.probes.calls)
    assert [entry["bucket"] for entry in payload["rejected"]] == names
    assert {entry["reason"] for entry in payload["rejected"]} == {"no_tags"}
    assert "additional_s3_buckets" not in h.record(usecase_id)


def test_update_audit_rows_carry_the_actor_attribution(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    bucket = h.tagged_bucket()
    status, _ = h.put(usecase_id, admin, {"additional_s3_buckets_add": [bucket]})
    assert status == 200
    stored = [item for item in h.tables.audit_log.scan()["Items"]
              if item.get("action") == "s3_bucket_allowed" and item.get("user_id") == admin["user_id"]]
    assert len(stored) == 1
    assert stored[0]["source_ip"] == SOURCE_IP and stored[0]["user_agent"] == "api-test/1.0"
    assert stored[0]["email"] == admin["email"] and stored[0]["username"] == admin["username"]
    assert stored[0]["details"]["bucket"] == bucket


def test_update_viewer_is_forbidden_before_any_read(h):
    usecase_id = h.put_usecase()
    status, payload = h.put(usecase_id, h.viewer(usecase_id), {"additional_s3_buckets_add": [h.tagged_bucket()]})
    assert status == 403 and h.probes.calls == []


def test_update_write_failure_mid_sequence_reports_applied(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    first, second = h.tagged_bucket(prefix="one"), h.tagged_bucket(prefix="two")
    seen = []

    def failing_second_add(real, **kwargs):
        if "list_append" in kwargs.get("UpdateExpression", ""):
            seen.append(kwargs["ExpressionAttributeValues"][":b"])
            if len(seen) == 2:
                raise client_error("ProvisionedThroughputExceededException", 400, "UpdateItem")
        return real.update_item(**kwargs)

    h.proxy_table(h.uc, update_item=failing_second_add)
    status, payload = h.put(usecase_id, admin, {"additional_s3_buckets_add": [first, second]})
    assert status == 500, payload
    assert payload["applied"] == {"added": [first], "removed": []}
    assert h.record(usecase_id)["additional_s3_buckets"] == [first]
    assert [row["details"]["bucket"] for row in allowed_rows(h)] == [first]


# ============================================================ POST /usecases

def single_account_body(h, **fields):
    body = {"name": f"created-{uuid.uuid4().hex[:6]}", "s3_bucket": h.tagged_bucket(prefix="new")}
    body.update(fields)
    return body


def test_create_usecase_without_the_creation_permission_is_403(h):
    """POST /usecases requires CREATE_USECASES (portal-authorization-and-iam-
    scoping, D1): a DataScientist claim is refused before any eligibility
    probe and no record is written."""
    creator = h.user("DataScientist")
    body = single_account_body(h)
    status, payload = h.create_usecase(creator, body)
    assert status == 403, payload
    assert payload["error_code"] == "INSUFFICIENT_PERMISSIONS"
    assert h.probes.calls == []
    assert not [item for item in h.tables.usecases.scan()["Items"] if item.get("name") == body["name"]]


def test_create_usecase_requires_an_eligible_s3_bucket(h):
    creator = h.portal_admin()
    untagged = h.untagged_bucket()
    status, payload = h.create_usecase(creator, single_account_body(h, s3_bucket=untagged))
    assert status == 400, payload
    assert (payload["error_code"], payload["reason"], payload["purpose"]) == (
        "bucket_not_eligible", "no_tags", "configure-s3-bucket")
    assert payload["rejected"] == [{"bucket": untagged, "error_code": "bucket_not_eligible", "reason": "no_tags"}]
    failures = h.audit.of("create_usecase", user_id=creator["user_id"], result="failure")
    assert [row["details"] for row in failures] == [
        {"reason": "bucket_not_eligible", "bucket": untagged, "eligibility_reason": "no_tags"}]
    refused = h.audit.of("s3_bucket_refused", user_id=creator["user_id"])
    assert len(refused) == 1 and refused[0]["details"]["purpose"] == "configure-s3-bucket"
    assert allowed_rows(h) == []
    assert not [item for item in h.tables.usecases.scan()["Items"] if item.get("s3_bucket") == untagged]

    status, payload = h.create_usecase(creator, single_account_body(h, s3_bucket=ARTIFACTS))
    assert status == 400 and payload["reason"] == "portal_bucket" and h.probes.calls[-1][0][1] != ARTIFACTS

    status, payload = h.create_usecase(creator, single_account_body(h, s3_bucket="Bad Name"))
    assert status == 400 and payload["error_code"] == "invalid_bucket_name"


def test_create_usecase_audits_source_create_for_every_attached_bucket(h):
    creator = h.portal_admin()
    data, extra_a, extra_b = h.tagged_bucket(prefix="data"), h.tagged_bucket(prefix="xa"), h.tagged_bucket(prefix="xb")
    body = single_account_body(h, data_s3_bucket=data, additional_s3_buckets=[extra_a, extra_b, extra_a])
    status, payload = h.create_usecase(creator, body)
    assert status == 201, payload
    usecase_id = payload["usecase"]["usecase_id"]
    record = h.record(usecase_id)
    assert record["s3_bucket"] == body["s3_bucket"] and record["data_s3_bucket"] == data
    assert record["additional_s3_buckets"] == [extra_a, extra_b]            # de-duplicated
    rows = allowed_rows(h, user_id=creator["user_id"], resource_id=usecase_id)
    assert [row["details"] for row in rows] == [
        {"bucket": body["s3_bucket"], "attribute": "s3_bucket", "source": "create"},
        {"bucket": data, "attribute": "data_s3_bucket", "source": "create"},
        {"bucket": extra_a, "attribute": "additional_s3_buckets", "source": "create"},
        {"bucket": extra_b, "attribute": "additional_s3_buckets", "source": "create"}]
    # retry=True for the two configured buckets only.
    assert [(call[0][1], call[1]["retry"]) for call in h.probes.calls] == [
        (body["s3_bucket"], True), (data, True), (extra_a, False), (extra_b, False)]
    assert h.sleeps.calls == []                                 # tags were present at once
    assert h.audit.of("create_usecase", user_id=creator["user_id"], result="success")


def test_create_usecase_additional_list_validation(h):
    creator = h.portal_admin()
    status, payload = h.create_usecase(creator, single_account_body(
        h, additional_s3_buckets=[f"bucket-{i}" for i in range(11)]))
    assert status == 400 and payload["error_code"] == "invalid_field"
    assert payload["field"] == "additional_s3_buckets" and h.probes.calls == []

    status, payload = h.create_usecase(creator, single_account_body(h, additional_s3_buckets="x"))
    assert status == 400 and payload["error_code"] == "invalid_field"

    status, payload = h.create_usecase(creator, single_account_body(h, additional_s3_buckets=["ok-bucket", "no!"]))
    assert status == 400 and payload["error_code"] == "invalid_bucket_name" and payload["bucket"] == "no!"

    untagged = h.untagged_bucket()
    body = single_account_body(h, additional_s3_buckets=[untagged])
    status, payload = h.create_usecase(creator, body)
    assert status == 400 and payload["purpose"] == "allow-additional-bucket"
    assert payload["rejected"] == [{"bucket": untagged, "error_code": "bucket_not_eligible", "reason": "no_tags"}]
    assert h.sleeps.calls == []                                 # the additional list never retries
    assert not [item for item in h.tables.usecases.scan()["Items"] if item.get("name") == body["name"]]


def test_create_usecase_probes_data_bucket_through_the_data_account_role(h):
    creds = {"AccessKeyId": "testing", "SecretAccessKey": "testing", "SessionToken": "testing"}
    sts = Recorder(result=creds)
    h.monkeypatch.setattr(h.shared, "assume_usecase_role", sts)
    creator = h.portal_admin()
    data = h.tagged_bucket(prefix="data")
    body = single_account_body(h, account_id="222222222222", cross_account_role_arn=CROSS_ROLE,
                               sagemaker_execution_role_arn=SAGEMAKER_ROLE, external_id="ext-1",
                               data_account_id="333333333333", data_account_role_arn=DATA_ROLE,
                               data_account_external_id="ext-data", data_s3_bucket=data)
    status, payload = h.create_usecase(creator, body)
    assert status == 201, payload
    probe_sessions = [call[0] for call in sts.calls if call[0][2] == h.shared.ELIGIBILITY_SESSION_NAME]
    assert probe_sessions == [(CROSS_ROLE, "ext-1", h.shared.ELIGIBILITY_SESSION_NAME),
                              (DATA_ROLE, "ext-data", h.shared.ELIGIBILITY_SESSION_NAME)]
    record = h.record(payload["usecase"]["usecase_id"])
    assert record["data_account_role_arn"] == DATA_ROLE and record["data_s3_bucket"] == data
    # The data-bucket tag check stays read-only: no tag is written on a customer bucket.
    assert payload["data_bucket_tag"]["status"] in ("already_configured", "failed")
    assert h.s3.get_bucket_tagging(Bucket=data)["TagSet"] == tag_set(dda_portal__managed="true")


def test_create_usecase_credentials_unavailable_when_the_data_account_role_is_denied(h):
    creds = {"AccessKeyId": "testing", "SecretAccessKey": "testing", "SessionToken": "testing"}

    def assume(role_arn, external_id, session_name):
        if role_arn == DATA_ROLE:
            raise client_error("AccessDenied", 403, "AssumeRole")
        return creds

    h.monkeypatch.setattr(h.shared, "assume_usecase_role", assume)
    creator = h.portal_admin()
    data = h.tagged_bucket(prefix="data")
    body = single_account_body(h, account_id="222222222222", cross_account_role_arn=CROSS_ROLE,
                               sagemaker_execution_role_arn=SAGEMAKER_ROLE, external_id="ext-1",
                               data_account_id="333333333333", data_account_role_arn=DATA_ROLE,
                               data_account_external_id="ext-data", data_s3_bucket=data)
    status, payload = h.create_usecase(creator, body)
    assert status == 400, payload
    assert (payload["reason"], payload["purpose"], payload["bucket"]) == (
        "credentials_unavailable", "configure-data-s3-bucket", data)
    assert payload["remedy"] == h.shared.REMEDY_CREDENTIALS
    assert not [item for item in h.tables.usecases.scan()["Items"] if item.get("name") == body["name"]]


def test_create_usecase_retries_bounded(h):
    """NoSuchTagSet twice then the tags: two sleeps and 201; a third miss
    concludes no_tags after two sleeps (ELIGIBILITY_RETRY_ATTEMPTS = 3)."""
    class ScriptedS3:
        def __init__(self, script):
            self.script = list(script)
            self.calls = []

        def get_bucket_tagging(self, **kwargs):
            self.calls.append(kwargs)
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

    creator = h.portal_admin()
    fresh = h.name("fresh")
    scripted = ScriptedS3([client_error("NoSuchTagSet", 404), client_error("NoSuchTagSet", 404),
                           {"TagSet": tag_set(dda_portal__managed="true")}])
    h.monkeypatch.setattr(h.shared, "create_boto3_client", lambda service, credentials, region=None: scripted)
    status, payload = h.create_usecase(creator, single_account_body(h, s3_bucket=fresh))
    assert status == 201, payload
    assert len(scripted.calls) == 3
    assert h.sleeps.calls == [((h.shared.ELIGIBILITY_RETRY_DELAY_SECONDS,), {})] * 2

    h.sleeps.calls.clear()
    scripted = ScriptedS3([client_error("NoSuchTagSet", 404)] * 3)
    h.monkeypatch.setattr(h.shared, "create_boto3_client", lambda service, credentials, region=None: scripted)
    status, payload = h.create_usecase(creator, single_account_body(h, s3_bucket=h.name("fresh")))
    assert status == 400 and payload["reason"] == "no_tags"
    assert len(scripted.calls) == 3 and len(h.sleeps.calls) == 2


def test_tag_bucket_for_portal_performs_no_write(h):
    class ReadOnlyS3:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def get_bucket_tagging(self, **kwargs):
            self.calls.append(("get_bucket_tagging", kwargs))
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

        def __getattr__(self, name):
            raise AssertionError(f"tag_bucket_for_portal must not call {name}")

    h.monkeypatch.setattr(h.uc, "assume_role", lambda *a: {
        "AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"})
    import boto3 as real_boto3
    for response, expected in (({"TagSet": tag_set(dda_portal__managed="true")}, "already_configured"),
                               (client_error("NoSuchTagSet", 404), "failed"),
                               ({"TagSet": tag_set(Owner="x")}, "failed")):
        s3 = ReadOnlyS3(response)
        h.monkeypatch.setattr(h.uc, "boto3", SimpleNamespace(
            client=lambda service, **kw: s3 if service == "s3" else real_boto3.client(service, **kw),
            resource=real_boto3.resource))
        result = h.uc.tag_bucket_for_portal(DATA_ROLE, "ext", "customer-bucket", "uc-1")
        assert result["status"] == expected and result["bucket"] == "customer-bucket"
        assert s3.calls == [("get_bucket_tagging", {"Bucket": "customer-bucket"})]
        if expected == "failed":
            assert "dda-portal:managed" in result["error"]


# ============================================================ POST /s3-buckets

def test_create_s3_bucket_requires_a_valid_new_bucket_name(h):
    stubs = h.stub_bucket_clients()
    portal_admin = h.portal_admin()
    status, payload = h.create_bucket(portal_admin, {"name": ""})
    assert status == 400 and "required" in payload["error"]
    for bad in ("Has-Capitals", "under_score", "a..b", "sh", "x" * 64, "192.168.1.1"):
        status, payload = h.create_bucket(portal_admin, {"name": bad})
        assert status == 400 and payload["error_code"] == "invalid_bucket_name", bad
        assert payload["purpose"] == "create-s3-bucket"
    assert stubs.s3.calls == []


def test_create_s3_bucket_without_usecase_requires_portal_admin(h):
    stubs = h.stub_bucket_clients()
    usecase_id = h.put_usecase()
    status, payload = h.create_bucket(h.usecase_admin(usecase_id), {"name": h.name()})
    assert status == 403 and stubs.s3.calls == []

    portal_admin = h.portal_admin()
    name = h.name()
    status, payload = h.create_bucket(portal_admin, {"name": name})
    assert status == 201, payload
    # The sibling's six keys, plus the registration fields of
    # portal-authorization-and-iam-scoping (Requirement 7.11).
    assert {key: payload["bucket"][key] for key in SIBLING_BUCKET_KEYS} == {
        "name": name, "region": "us-east-1", "portal_created": True,
        "tags": [MANAGED, CREATED_BY], "abac": "enabled", "added_to_usecase": False}
    assert payload["bucket"]["tagged"] is True and payload["bucket"]["abac_enabled"] is True
    assert stubs.s3control.calls == []
    success = h.audit.of("create_s3_bucket", user_id=portal_admin["user_id"], result="success")
    assert [row["details"] for row in success] == [{
        "region": "us-east-1", "tags": [MANAGED, CREATED_BY], "abac": "enabled",
        "usecase_id": None, "added_to_usecase": False}]
    assert success[0]["resource_id"] == name


def test_create_s3_bucket_with_usecase_authorization_branches(h):
    stubs = h.stub_bucket_clients()
    status, payload = h.create_bucket(h.portal_admin(), {"name": h.name(), "usecase_id": "uc-does-not-exist"})
    assert status == 404 and payload["error_code"] == "usecase_not_found"

    usecase_id = h.put_usecase()
    status, payload = h.create_bucket(h.viewer(usecase_id), {"name": h.name(), "usecase_id": usecase_id})
    assert status == 403

    cross = h.put_usecase(account_id="acct-remote", cross_account_role_arn=CROSS_ROLE)
    status, payload = h.create_bucket(h.usecase_admin(cross), {"name": h.name(), "usecase_id": cross})
    assert status == 400, payload
    assert (payload["error_code"], payload["reason"]) == ("bucket_not_eligible", "account_mismatch")
    assert payload["remedy"] == h.shared.REMEDY_ACCOUNT

    full = h.put_usecase(additional_s3_buckets=[f"existing-{i:03d}" for i in range(100)])
    status, payload = h.create_bucket(h.usecase_admin(full), {"name": h.name(), "usecase_id": full})
    assert status == 400 and payload["reason"] == "too_many_buckets"
    assert stubs.s3.calls == [] and stubs.factory.calls == []  # no AWS call on any refusal


def test_create_s3_bucket_pre_create_refusals_create_nothing(h):
    stubs = h.stub_bucket_clients()
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    status, payload = h.create_bucket(admin, {"name": ARTIFACTS, "usecase_id": usecase_id})
    assert status == 400, payload
    assert (payload["reason"], payload["remedy"]) == ("portal_bucket", h.shared.REMEDY_PORTAL_BUCKET)
    refused = h.audit.of("s3_bucket_refused", user_id=admin["user_id"])
    assert [row["details"] for row in refused] == [
        {"bucket": ARTIFACTS, "purpose": "create-s3-bucket", "reason": "portal_bucket"}]

    configured_elsewhere = h.name("elsewhere")
    h.put_usecase(s3_bucket=configured_elsewhere)                # a record naming a bucket that does not exist yet
    status, payload = h.create_bucket(admin, {"name": configured_elsewhere, "usecase_id": usecase_id})
    assert status == 400, payload
    assert payload["reason"] == "bucket_configured_on_other_usecase"
    assert payload["remedy"] == h.shared.REMEDY_OTHER_USECASE
    assert stubs.s3.calls == []                                 # nothing created, nothing tagged
    assert "additional_s3_buckets" not in h.record(usecase_id)

    # A PortalAdmin may create it; the auto-add records the sharing.
    portal_admin = h.portal_admin()
    status, payload = h.create_bucket(portal_admin, {"name": configured_elsewhere, "usecase_id": usecase_id})
    assert status == 201, payload
    assert payload["bucket"]["added_to_usecase"] is True
    rows = allowed_rows(h, user_id=portal_admin["user_id"])
    assert [row["details"] for row in rows] == [{
        "bucket": configured_elsewhere, "attribute": "additional_s3_buckets", "source": "auto_add",
        "shared_with_usecases": 1}]


def test_create_s3_bucket_call_order(h):
    stubs = h.stub_bucket_clients()
    usecase_id = h.put_usecase()
    name = h.name()
    status, payload = h.create_bucket(h.usecase_admin(usecase_id), {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    assert stubs.s3.ops() == ["create_bucket", "put_bucket_abac", "get_bucket_tagging"]
    assert stubs.s3control.calls == []                          # the read confirmed the keys
    assert stubs.factory.calls[0] == ("s3", {"region_name": "us-east-1"})
    create_kwargs = stubs.s3.calls[0][1]
    assert create_kwargs == {"Bucket": name, "CreateBucketConfiguration": {"Tags": [
        {"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"},
        {"Key": CLAIM, "Value": usecase_id}]}}                 # no LocationConstraint in us-east-1
    assert stubs.s3.calls[1][1] == {"Bucket": name, "AbacStatus": {"Status": "Enabled"}}
    assert stubs.s3.calls[2][1] == {"Bucket": name}
    assert h.sleeps.calls == []

    stubs = h.stub_bucket_clients()
    name = h.name("eu")
    status, payload = h.create_bucket(h.portal_admin(), {"name": name, "region": "eu-west-1"})
    assert status == 201 and payload["bucket"]["region"] == "eu-west-1"
    assert stubs.factory.calls[0] == ("s3", {"region_name": "eu-west-1"})
    assert stubs.s3.calls[0][1]["CreateBucketConfiguration"]["LocationConstraint"] == "eu-west-1"


def test_create_s3_bucket_tag_resource_fallback(h):
    missing = client_error("NoSuchTagSet", 404)
    stubs = h.stub_bucket_clients(script=[missing, missing, missing, "sent"])
    usecase_id = h.put_usecase()
    name = h.name()
    status, payload = h.create_bucket(h.usecase_admin(usecase_id), {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    assert stubs.s3.ops() == ["create_bucket", "put_bucket_abac",
                              "get_bucket_tagging", "get_bucket_tagging", "get_bucket_tagging",
                              "get_bucket_tagging"]
    assert stubs.s3control.calls == [{
        "AccountId": PORTAL_ACCOUNT, "ResourceArn": f"arn:aws:s3:::{name}",
        "Tags": [{"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"},
                 {"Key": CLAIM, "Value": usecase_id}]}]
    assert stubs.factory.calls[1] == ("s3control", {"region_name": "us-east-1"})
    assert len(h.sleeps.calls) == 2                             # the bounded 3x1 s read, once
    assert payload["bucket"]["added_to_usecase"] is True
    assert name in h.record(usecase_id)["additional_s3_buckets"]

    # Still missing after the fallback: 500, nothing attached, failure audited.
    stubs = h.stub_bucket_clients(script=[missing])
    h.audit.clear()
    h.sleeps.calls.clear()
    name = h.name("unconfirmed")
    admin = h.usecase_admin(usecase_id)
    status, payload = h.create_bucket(admin, {"name": name, "usecase_id": usecase_id})
    assert status == 500, payload
    assert payload == {"error": "The bucket was created but could not be tagged",
                       "error_code": "bucket_tagging_failed", "bucket": name,
                       "remedy": h.uc.REMEDY_TAGGING_FAILED}
    assert stubs.s3.ops().count("get_bucket_tagging") == 6 and len(stubs.s3control.calls) == 1
    assert len(h.sleeps.calls) == 4
    assert name not in h.record(usecase_id)["additional_s3_buckets"]
    assert allowed_rows(h) == []
    failures = h.audit.of("create_s3_bucket", user_id=admin["user_id"], result="failure")
    assert len(failures) == 1 and failures[0]["details"]["reason"] == "bucket_tagging_failed"

    # A tag_resource error is the same 500.
    stubs = h.stub_bucket_clients(script=[missing], tag_resource_error=client_error("AccessDenied", 403, "TagResource"))
    status, payload = h.create_bucket(admin, {"name": h.name("denied"), "usecase_id": usecase_id})
    assert status == 500 and payload["error_code"] == "bucket_tagging_failed"
    assert stubs.s3.ops().count("get_bucket_tagging") == 3      # no re-read after a failed write


def test_create_s3_bucket_tags_argument_exact(h):
    missing = client_error("NoSuchTagSet", 404)
    usecase_id = h.put_usecase()
    expected_with_usecase = [{"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"},
                             {"Key": CLAIM, "Value": usecase_id}]
    expected_without = expected_with_usecase[:2]
    for body, expected in (({"usecase_id": usecase_id}, expected_with_usecase), ({}, expected_without)):
        user = h.usecase_admin(usecase_id) if body else h.portal_admin()
        stubs = h.stub_bucket_clients(script=[missing, missing, missing, "sent"])
        status, payload = h.create_bucket(user, {"name": h.name(), **body})
        assert status == 201, payload
        tags_arguments = [kwargs["CreateBucketConfiguration"]["Tags"] for op, kwargs in stubs.s3.calls
                          if op == "create_bucket"] + [call["Tags"] for call in stubs.s3control.calls]
        assert len(tags_arguments) == 2                          # create and the one fallback
        for tags in tags_arguments:
            assert tags == expected
            assert all(tag["Key"].startswith("dda-portal:") for tag in tags)
        assert payload["bucket"]["tags"] == [tag["Key"] for tag in expected]
        assert "LocationConstraint" not in stubs.s3.calls[0][1]["CreateBucketConfiguration"]


def test_create_s3_bucket_already_owned_is_409_with_nothing_tagged(h):
    stubs = h.stub_bucket_clients(create_error=client_error("BucketAlreadyOwnedByYou", 409, "CreateBucket"))
    usecase_id = h.put_usecase()
    name = h.name("owned")
    status, payload = h.create_bucket(h.usecase_admin(usecase_id), {"name": name, "usecase_id": usecase_id})
    assert status == 409, payload
    assert payload["error_code"] == "bucket_already_exists" and payload["bucket"] == name
    assert payload["remedy"] == h.uc.REMEDY_BUCKET_ALREADY_EXISTS
    assert stubs.s3.ops() == ["create_bucket"] and stubs.s3control.calls == []
    assert "additional_s3_buckets" not in h.record(usecase_id)

    stubs = h.stub_bucket_clients(create_error=client_error("BucketAlreadyExists", 409, "CreateBucket"))
    status, payload = h.create_bucket(h.portal_admin(), {"name": h.name()})
    assert status == 409 and "already taken" in payload["error"] and "error_code" not in payload

    stubs = h.stub_bucket_clients(create_error=client_error("InvalidBucketName", 400, "CreateBucket"))
    status, payload = h.create_bucket(h.portal_admin(), {"name": h.name()})
    assert status == 400 and payload["error"].startswith("Failed to create bucket")


def test_create_s3_bucket_reports_abac_failure_and_still_tags(h):
    stubs = h.stub_bucket_clients(abac_error=client_error("AccessDenied", 403, "PutBucketAbac"))
    usecase_id = h.put_usecase()
    name = h.name()
    status, payload = h.create_bucket(h.usecase_admin(usecase_id), {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    assert payload["bucket"]["abac"] == "failed" and payload["bucket"]["added_to_usecase"] is True
    assert stubs.s3.ops() == ["create_bucket", "put_bucket_abac", "get_bucket_tagging"]
    success = h.audit.of("create_s3_bucket", result="success")
    assert success[-1]["details"]["abac"] == "failed"


def test_create_s3_bucket_auto_add_success_row(h):
    stubs = h.stub_bucket_clients()
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    name = h.name()
    status, payload = h.create_bucket(admin, {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    # The sibling's six keys, plus the registration fields of
    # portal-authorization-and-iam-scoping (Requirement 7.11).
    assert {key: payload["bucket"][key] for key in SIBLING_BUCKET_KEYS} == {
        "name": name, "region": "us-east-1", "portal_created": True,
        "tags": [MANAGED, CREATED_BY, CLAIM], "abac": "enabled", "added_to_usecase": True}
    assert payload["bucket"]["tagged"] is True and payload["bucket"]["abac_enabled"] is True
    assert h.record(usecase_id)["additional_s3_buckets"] == [name]
    rows = allowed_rows(h, user_id=admin["user_id"], resource_id=usecase_id)
    assert [row["details"] for row in rows] == [
        {"bucket": name, "attribute": "additional_s3_buckets", "source": "auto_add"}]
    success = h.audit.of("create_s3_bucket", user_id=admin["user_id"], result="success")
    assert [row["details"] for row in success] == [{
        "region": "us-east-1", "tags": [MANAGED, CREATED_BY, CLAIM], "abac": "enabled",
        "usecase_id": usecase_id, "added_to_usecase": True}]
    assert h.probes.calls == []                                 # evidence, never a probe
    assert stubs.s3control.calls == []


def test_create_s3_bucket_add_error_usecase_not_found(h):
    h.stub_bucket_clients()
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)

    def read_then_delete(real, **kwargs):
        response = real.get_item(**kwargs)
        real.delete_item(Key=kwargs["Key"])
        return response

    h.proxy_table(h.uc, get_item=read_then_delete)
    status, payload = h.create_bucket(admin, {"name": h.name(), "usecase_id": usecase_id})
    assert status == 201, payload
    assert payload["bucket"]["added_to_usecase"] is False
    assert payload["bucket"]["add_error"] == {"error_code": "usecase_not_found", "remedy": h.uc.REMEDY_ADD_LATER}
    assert h.record(usecase_id) is None                          # no phantom record
    assert allowed_rows(h) == []


def test_create_s3_bucket_add_error_bucket_not_eligible(h):
    """A record naming the bucket appears between the pre-create Scan and the
    auto-add (planted by the first tag read): still 201, nothing attached."""
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    name = h.name("raced")

    def plant_record_then_echo():
        h.put_usecase(s3_bucket=name)
        return "sent"

    h.stub_bucket_clients(script=[plant_record_then_echo, "sent"])
    status, payload = h.create_bucket(admin, {"name": name, "usecase_id": usecase_id})
    assert status == 201, payload
    assert payload["bucket"]["added_to_usecase"] is False
    assert payload["bucket"]["add_error"] == {
        "error_code": "bucket_not_eligible", "reason": "bucket_configured_on_other_usecase",
        "remedy": h.shared.REMEDY_OTHER_USECASE}
    assert "additional_s3_buckets" not in h.record(usecase_id)
    assert allowed_rows(h) == []
    refused = h.audit.of("s3_bucket_refused", user_id=admin["user_id"])
    assert [row["details"] for row in refused] == [
        {"bucket": name, "purpose": "auto-add-bucket", "reason": "bucket_configured_on_other_usecase"}]


def test_create_s3_bucket_add_error_usecase_update_failed(h):
    h.stub_bucket_clients()
    usecase_id = h.put_usecase()

    def failing_update(real, **kwargs):
        raise client_error("ProvisionedThroughputExceededException", 400, "UpdateItem")

    h.proxy_table(h.uc, update_item=failing_update)
    status, payload = h.create_bucket(h.usecase_admin(usecase_id), {"name": h.name(), "usecase_id": usecase_id})
    assert status == 201, payload
    assert payload["bucket"]["add_error"] == {"error_code": "usecase_update_failed", "remedy": h.uc.REMEDY_ADD_LATER}
    assert payload["bucket"]["added_to_usecase"] is False
    assert "additional_s3_buckets" not in h.record(usecase_id) and allowed_rows(h) == []


# ============================================================ GET /s3-buckets

def test_list_s3_buckets_advisory_flags_from_one_tagging_query(h):
    portal_made, admin_tagged, plain = h.name("portal"), h.name("tagged"), h.name("plain")
    stubs = h.stub_bucket_clients(listing=[portal_made, admin_tagged, plain], tagging_pages=[
        [{"ResourceARN": f"arn:aws:s3:::{portal_made}",
          "Tags": tag_set(dda_portal__managed="true", dda_portal__created_by="portal")}],
        [{"ResourceARN": f"arn:aws:s3:::{admin_tagged}", "Tags": tag_set(dda_portal__managed="true")}]])
    status, payload = h.list_s3_buckets(h.portal_admin())
    assert status == 200, payload
    flags = {bucket["name"]: (bucket["eligible"], bucket["portal_created"]) for bucket in payload["buckets"]}
    assert flags == {portal_made: (True, True), admin_tagged: (True, False), plain: (False, False)}
    assert all(set(bucket) == {"name", "creation_date", "region", "eligible", "portal_created"}
               for bucket in payload["buckets"])
    assert stubs.tagging.calls == [{"TagFilters": [{"Key": MANAGED, "Values": ["true"]}],
                                    "ResourceTypeFilters": ["s3:bucket"]}]
    assert "get_bucket_tagging" not in [call for call in stubs.s3.calls if isinstance(call, str)]
    assert [service for service, _ in stubs.factory.calls] == ["s3", "resourcegroupstaggingapi"]


def test_list_buckets_flags_in_data_management(h):
    extra = h.name("extra")
    usecase_id = h.put_usecase(additional_s3_buckets=[extra])
    main = h.record(usecase_id)["s3_bucket"]
    other = h.name("other")
    pages = [[{"ResourceARN": f"arn:aws:s3:::{main}", "Tags": tag_set(dda_portal__managed="true")},
              {"ResourceARN": f"arn:aws:s3:::{extra}",
               "Tags": tag_set(dda_portal__managed="true", dda_portal__created_by="portal")},
              {"ResourceARN": f"arn:aws:s3:::{other}", "Tags": tag_set(dda_portal__managed="true")}]]
    tagging = FakeTagging(pages)
    import boto3 as real_boto3
    h.monkeypatch.setattr(h.dm, "boto3", SimpleNamespace(
        client=lambda service, **kw: tagging if service == "resourcegroupstaggingapi" else real_boto3.client(service, **kw),
        resource=real_boto3.resource))
    event = h.event("GET", f"/usecases/{usecase_id}/data/buckets", h.viewer(usecase_id), usecase_id)
    response = h.dm.handler(event, None)
    assert response["statusCode"] == 200, response["body"]
    flags = {bucket["name"]: (bucket["allowed"], bucket["portal_created"]) for bucket in body_of(response)["buckets"]}
    assert flags == {main: (True, False), extra: (True, True), other: (False, False)}


# ============================================ POST /usecases/{id}/data/configure

def test_configure_data_account_unknown_usecase_is_404(h):
    status, payload = h.configure(f"uc-none-{uuid.uuid4().hex[:6]}", h.portal_admin(),
                                  {"data_s3_bucket": h.tagged_bucket()})
    assert status == 404 and payload["error_code"] == "usecase_not_found"
    assert h.probes.calls == []


def test_configure_data_account_clear_and_unchanged_without_a_probe(h):
    data = h.tagged_bucket(prefix="data")
    usecase_id = h.put_usecase(data_s3_bucket=data)
    admin = h.usecase_admin(usecase_id)
    status, payload = h.configure(usecase_id, admin, {"data_s3_bucket": data, "data_account_id": ACCOUNT})
    assert status == 200, payload
    assert h.record(usecase_id)["data_account_id"] == ACCOUNT
    status, payload = h.configure(usecase_id, admin, {"data_s3_bucket": ""})
    assert status == 200 and h.record(usecase_id)["data_s3_bucket"] == ""
    status, payload = h.configure(usecase_id, admin, {"data_s3_bucket": None})
    assert status == 200 and h.record(usecase_id)["data_s3_bucket"] is None
    assert h.probes.calls == [] and allowed_rows(h) == []
    success = h.audit.of("configure_data_account", user_id=admin["user_id"])
    assert all("bucket" not in row["details"] for row in success)


def test_configure_data_account_changed_bucket_is_probed_and_audited(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)
    untagged = h.untagged_bucket()
    status, payload = h.configure(usecase_id, admin, {"data_s3_bucket": untagged})
    assert status == 400, payload
    assert (payload["error_code"], payload["reason"], payload["purpose"]) == (
        "bucket_not_eligible", "no_tags", "configure-data-s3-bucket")
    assert "data_s3_bucket" not in h.record(usecase_id)

    data = h.tagged_bucket(prefix="data")
    status, payload = h.configure(usecase_id, admin, {"data_s3_bucket": data})
    assert status == 200, payload
    assert h.record(usecase_id)["data_s3_bucket"] == data
    assert [call[1] for call in h.probes.calls] == [{"retry": False}] * 2
    success = h.audit.of("configure_data_account", user_id=admin["user_id"], result="success")
    assert [row["details"] for row in success] == [{"updated_fields": ["data_s3_bucket"], "bucket": data}]
    rows = allowed_rows(h, user_id=admin["user_id"], resource_id=usecase_id)
    assert [row["details"] for row in rows] == [{"bucket": data, "attribute": "data_s3_bucket", "source": "configure"}]
    assert rows[0]["event"] is not None


def test_configure_data_account_portal_admin_sharing_is_audited(h):
    shared_bucket = h.tagged_bucket(prefix="shared")
    h.put_usecase(s3_bucket=shared_bucket)
    usecase_id = h.put_usecase()
    status, payload = h.configure(usecase_id, h.usecase_admin(usecase_id), {"data_s3_bucket": shared_bucket})
    assert status == 400 and payload["reason"] == "bucket_configured_on_other_usecase"
    portal_admin = h.portal_admin()
    status, payload = h.configure(usecase_id, portal_admin, {"data_s3_bucket": shared_bucket})
    assert status == 200, payload
    rows = allowed_rows(h, user_id=portal_admin["user_id"])
    assert [row["details"] for row in rows] == [{
        "bucket": shared_bucket, "attribute": "data_s3_bucket", "source": "configure",
        "shared_with_usecases": 1}]


def test_configure_data_account_deleted_mid_request_is_404(h):
    usecase_id = h.put_usecase()
    admin = h.usecase_admin(usecase_id)

    def read_then_delete(real, **kwargs):
        response = real.get_item(**kwargs)
        real.delete_item(Key=kwargs["Key"])
        return response

    h.proxy_table(h.dm, get_item=read_then_delete)
    status, payload = h.configure(usecase_id, admin, {"data_account_id": ACCOUNT})
    assert status == 404 and payload["error_code"] == "usecase_not_found"
    assert h.record(usecase_id) is None
