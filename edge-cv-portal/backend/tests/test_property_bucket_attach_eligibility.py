"""
Property 9 of the use-case bucket configuration (portal-usecase-bucket-config,
design §1.6, §5, §14; tasks.md 6.4): **a bucket enters a use case's set only
if it is Eligible and Unclaimed**.

Random sequences of the five record-writing requests — `POST /usecases`,
`PUT /usecases/{id}`, `POST /s3-buckets` with and without `usecase_id`,
`POST /usecases/{id}/data/configure` and `DELETE /usecases/{id}` of a use
case the sequence created — are issued by a non-PortalAdmin (a UseCaseAdmin
claim, which with enforcement off holds UPDATE / DELETE_USECASES on every
use case: the strongest non-administrator) against the moto tables, with
the tag reads of the eligibility probe answered from a per-example
catalogue mixing untagged, unreadable, missing, tagged, tagged-for-another
and tagged-for-a-deleted-use-case buckets plus the portal's own bucket
names, next to a pre-existing use case P configured with bucket A. After
every step, over every surviving record:

- no untagged, unreadable, missing or portal-owned bucket is in any set;
- A is in no set but P's;
- a bucket claimed (`dda-portal:usecase-id`) by another use case — one the
  sequence created and then deleted, or one that never existed — is in no
  surviving set; a bucket the portal created for a use case is claimed by it
  and therefore attached to it alone.

The oracle reads the raw record fields (never `allowed_s3_buckets`).

Validates: Requirements 10.1–10.4, 10.6, 10.7, 10.9, 15.3
"""
import json
import sys
import uuid
from types import SimpleNamespace
from typing import Dict, List

import pytest
from botocore.exceptions import ClientError
from hypothesis import event, given, settings
from hypothesis import strategies as st

ACCOUNT = "acct-main"
ROOT_ARN = f"arn:aws:iam::{ACCOUNT}:root"
ARTIFACTS = "test-portal-artifacts"
PORTAL_ACCOUNT = "acct-portal"
COMPONENT = f"dda-component-us-east-1-{PORTAL_ACCOUNT}"
PORTAL_OWNED = (ARTIFACTS, COMPONENT)
MANAGED = "dda-portal:managed"
CREATED_BY = "dda-portal:created-by"
CLAIM = "dda-portal:usecase-id"
CLEAR_VALUES = ("", "   ", None)


def client_error(code, status, operation="Op"):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


def is_clear(value):
    return value is None or (isinstance(value, str) and not value.strip())


class Catalogue:
    """What the tag read answers for each bucket name: 'untagged' |
    'unreadable' | 'missing' | 'tagged' | ('claimed', usecase_id).
    Portal-owned names read as tagged so the step-0 exclusion, not the
    probe, is what keeps them out."""

    def __init__(self):
        self.kinds: Dict[str, object] = {name: "tagged" for name in PORTAL_OWNED}

    def tag_set(self, name):
        kind = self.kinds.get(name, "missing")
        if kind == "missing":
            raise client_error("NoSuchBucket", 404, "GetBucketTagging")
        if kind == "untagged":
            raise client_error("NoSuchTagSet", 404, "GetBucketTagging")
        if kind == "unreadable":
            raise client_error("AccessDenied", 403, "GetBucketTagging")
        tags = [{"Key": MANAGED, "Value": "true"}]
        if isinstance(kind, tuple):
            tags.append({"Key": CREATED_BY, "Value": "portal"})
            tags.append({"Key": CLAIM, "Value": kind[1]})
        return {"TagSet": tags}

    def exists(self, name):
        return self.kinds.get(name, "missing") != "missing"


class ProbeS3:
    """The S3 client the probe builds: answers from the catalogue."""

    def __init__(self, catalogue):
        self.catalogue = catalogue

    def get_bucket_tagging(self, Bucket):
        return self.catalogue.tag_set(Bucket)


class CreatingS3:
    """The region-bound S3 client `create_s3_bucket` builds: creating a
    bucket registers it in the catalogue as tagged and claimed by the use
    case it was created for; the read-after-write echoes the tags sent."""

    def __init__(self, catalogue):
        self.catalogue = catalogue
        self.sent: Dict[str, List[Dict]] = {}

    def create_bucket(self, Bucket, CreateBucketConfiguration=None):
        if self.catalogue.exists(Bucket):
            raise client_error("BucketAlreadyOwnedByYou", 409, "CreateBucket")
        tags = list((CreateBucketConfiguration or {}).get("Tags", []))
        claim = next((tag["Value"] for tag in tags if tag["Key"] == CLAIM), None)
        self.catalogue.kinds[Bucket] = ("claimed", claim) if claim else "tagged"
        self.sent[Bucket] = tags
        return {}

    def put_bucket_abac(self, **kwargs):
        return {}

    def get_bucket_tagging(self, Bucket):
        return {"TagSet": list(self.sent.get(Bucket, []))}


@pytest.fixture(scope="module")
def h(aws_stack):
    """Module-scoped harness (hypothesis bodies may not use function-scoped
    fixtures): the real handler modules on the moto tables, the tag reads
    answered from the example's catalogue, audit rows discarded."""
    import shared_utils
    assert hasattr(shared_utils, "check_bucket_attachable"), "this suite needs the real shared layer"
    import boto3 as real_boto3
    mp = pytest.MonkeyPatch()
    mp.delenv(shared_utils.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    mp.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    mp.delenv("COMPONENT_BUCKET", raising=False)
    for name in ("usecases", "data_management"):
        sys.modules.pop(name, None)
    import usecases
    import data_management

    state = SimpleNamespace(shared=shared_utils, uc=usecases, dm=data_management, stack=aws_stack,
                            catalogue=Catalogue())
    mp.setattr(shared_utils.time, "sleep", lambda seconds: None)
    for module in (shared_utils, usecases, data_management):
        mp.setattr(module, "log_audit_event", lambda *args, **kwargs: None)
    mp.setattr(shared_utils, "create_boto3_client",
               lambda service, credentials, region=None: ProbeS3(state.catalogue))

    def client(service, **kwargs):
        if service == "s3" and "region_name" in kwargs:
            return CreatingS3(state.catalogue)
        if service == "s3control":
            return SimpleNamespace(tag_resource=lambda **kw: {})
        return real_boto3.client(service, **kwargs)

    mp.setattr(usecases, "boto3", SimpleNamespace(client=client, resource=real_boto3.resource))
    yield state
    mp.undo()


# ---------------------------------------------------------------- example

class Example:
    """One sequence: its catalogue entries, its actor, the pre-existing use
    case P with bucket A, and the use cases it created / deleted."""

    def __init__(self, h):
        self.h = h
        self.tables = h.stack.tables
        self.catalogue = h.catalogue
        suffix = uuid.uuid4().hex[:8]
        self.actor = {"user_id": f"p9-user-{suffix}", "email": f"p9-{suffix}@example.com",
                      "username": f"p9-user-{suffix}", "role": "UseCaseAdmin"}
        self.deleted_id = f"uc-p9-gone-{suffix}"            # a claim naming a use case that never existed
        self.names = {}
        for kind in ("untagged", "unreadable", "missing", "tagged-1", "tagged-2",
                     "claimed-by-p", "claimed-by-gone"):
            self.names[kind] = f"p9-{kind}-{suffix}"
        self.A = f"p9-a-{suffix}"
        self.P = self.put_record(s3_bucket=self.A)
        self.catalogue.kinds.update({
            self.names["untagged"]: "untagged", self.names["unreadable"]: "unreadable",
            self.names["tagged-1"]: "tagged", self.names["tagged-2"]: "tagged", self.A: "tagged",
            self.names["claimed-by-p"]: ("claimed", self.P),
            self.names["claimed-by-gone"]: ("claimed", self.deleted_id)})
        self.created: List[str] = []
        self.deleted: List[str] = []
        self.fresh = 0

    def put_record(self, **fields):
        usecase_id = f"uc-p9-{uuid.uuid4().hex[:10]}"
        item = {"usecase_id": usecase_id, "name": usecase_id, "account_id": ACCOUNT,
                "cross_account_role_arn": ROOT_ARN, "region": "us-east-1"}
        item.update(fields)
        self.tables.usecases.put_item(Item=item)
        return usecase_id

    def candidate_names(self):
        return list(self.names.values()) + [self.A, *PORTAL_OWNED]

    def fresh_name(self):
        self.fresh += 1
        return f"p9-new-{self.fresh}-{self.actor['user_id'][-8:]}"

    def targets(self):
        return [self.P, *self.created]

    def event(self, method, path, usecase_id=None, body=None):
        return {"httpMethod": method, "path": path, "resource": path,
                "pathParameters": {"id": usecase_id} if usecase_id else None,
                "queryStringParameters": None,
                "body": json.dumps(body) if body is not None else None,
                "requestContext": {"authorizer": {"claims": {
                    "sub": self.actor["user_id"], "email": self.actor["email"],
                    "cognito:username": self.actor["username"], "custom:role": self.actor["role"]}}}}

    # ------------------------------------------------------------ steps
    def step_create_usecase(self, s3_bucket, data_s3_bucket, additional):
        body = {"name": f"p9 {uuid.uuid4().hex[:6]}", "s3_bucket": s3_bucket,
                "provision_shared_components": False}
        if data_s3_bucket is not False:
            body["data_s3_bucket"] = data_s3_bucket
        if additional is not None:
            body["additional_s3_buckets"] = additional
        response = self.h.uc.handler(self.event("POST", "/usecases", body=body), None)
        event(f"create_usecase -> {response['statusCode']}")
        if response["statusCode"] == 201:
            self.created.append(json.loads(response["body"])["usecase"]["usecase_id"])
        else:
            assert response["statusCode"] == 400, response["body"]

    def step_update(self, target, body):
        response = self.h.uc.handler(self.event("PUT", f"/usecases/{target}", target, body), None)
        event(f"update -> {response['statusCode']}")
        assert response["statusCode"] in (200, 400, 404), response["body"]

    def step_create_bucket(self, name, usecase_id):
        body = {"name": name}
        if usecase_id is not None:
            body["usecase_id"] = usecase_id
        response = self.h.uc.handler(self.event("POST", "/s3-buckets", body=body), None)
        event(f"create_bucket -> {response['statusCode']}")
        assert response["statusCode"] in (201, 400, 403, 404, 409), response["body"]

    def step_configure(self, target, data_s3_bucket):
        response = self.h.dm.handler(self.event(
            "POST", f"/usecases/{target}/data/configure", target, {"data_s3_bucket": data_s3_bucket}), None)
        event(f"configure -> {response['statusCode']}")
        assert response["statusCode"] in (200, 400, 404), response["body"]

    def step_delete(self, target):
        response = self.h.uc.handler(self.event("DELETE", f"/usecases/{target}", target), None)
        event(f"delete -> {response['statusCode']}")
        assert response["statusCode"] == 200, response["body"]
        self.deleted.append(target)

    # ---------------------------------------------------------- oracle
    def surviving_records(self):
        records = []
        for usecase_id in self.targets():
            item = self.tables.usecases.get_item(Key={"usecase_id": usecase_id}).get("Item")
            if item is not None:
                records.append(item)
        return records

    def check_invariants(self):
        for record in self.surviving_records():
            names = [record.get("s3_bucket"), record.get("data_s3_bucket"),
                     *(record.get("additional_s3_buckets") or [])]
            for name in names:
                if is_clear(name):
                    continue
                kind = self.catalogue.kinds.get(name, "missing")
                assert name not in PORTAL_OWNED, (record["usecase_id"], name)
                assert kind not in ("untagged", "unreadable", "missing"), (record["usecase_id"], name, kind)
                if name == self.A:
                    assert record["usecase_id"] == self.P, (record["usecase_id"], name)
                if isinstance(kind, tuple):
                    # A claim counts whether or not the claimant still exists:
                    # only the claimant itself may hold the bucket.
                    assert kind[1] == record["usecase_id"], (record["usecase_id"], name, kind)
                    assert kind[1] not in self.deleted and kind[1] != self.deleted_id


# ------------------------------------------------------------ strategies

def bucket_choice(ex, draw, extra=()):
    return draw(st.sampled_from(ex.candidate_names() + list(extra)))


def maybe_clear_or_bucket(ex, draw, extra=()):
    if draw(st.booleans()):
        return draw(st.sampled_from(CLEAR_VALUES))
    return bucket_choice(ex, draw, extra)


def created_bucket_names(ex):
    return [name for name, kind in ex.catalogue.kinds.items()
            if isinstance(kind, tuple) and kind[1] in ex.targets() + ex.deleted]


class TestProperty9BucketEntersASetOnlyIfEligibleAndUnclaimed:
    """**Feature: portal-usecase-bucket-config, Property 9: A bucket enters a
    use case's set only if it is Eligible and Unclaimed** — over random
    sequences of the five record-writing requests by a non-PortalAdmin, no
    untagged, unreadable, missing or portal-owned bucket is ever in any
    set, a bucket configured on the pre-existing use case is never in
    another use case's set, and a bucket claimed by a deleted or
    never-existing use case is in no surviving set.
    Validates: Requirements 10.1–10.4, 10.6, 10.7, 10.9"""

    @settings(max_examples=100, deadline=None)
    @given(data=st.data())
    def test_sequences_keep_every_set_eligible_and_unclaimed(self, h, data):
        ex = Example(h)
        steps = data.draw(st.integers(min_value=1, max_value=7))
        for _ in range(steps):
            extra = created_bucket_names(ex)
            op = data.draw(st.sampled_from(["create_usecase", "update", "create_bucket", "configure", "delete"]))
            if op == "create_usecase":
                additional = None
                if data.draw(st.booleans()):
                    additional = data.draw(st.lists(st.sampled_from(ex.candidate_names() + extra), max_size=3))
                data_bucket = False if data.draw(st.booleans()) else maybe_clear_or_bucket(ex, data.draw, extra)
                ex.step_create_usecase(bucket_choice(ex, data.draw, extra), data_bucket, additional)
            elif op == "update":
                target = data.draw(st.sampled_from(ex.targets()))
                body = {}
                if data.draw(st.booleans()):
                    body["s3_bucket"] = bucket_choice(ex, data.draw, extra)
                if data.draw(st.booleans()):
                    body["data_s3_bucket"] = maybe_clear_or_bucket(ex, data.draw, extra)
                if data.draw(st.booleans()):
                    body["additional_s3_buckets_add"] = data.draw(
                        st.lists(st.sampled_from(ex.candidate_names() + extra), max_size=3))
                if data.draw(st.booleans()):
                    body["additional_s3_buckets_remove"] = data.draw(
                        st.lists(st.sampled_from(ex.candidate_names() + extra), max_size=2))
                ex.step_update(target, body)
            elif op == "create_bucket":
                name = ex.fresh_name() if data.draw(st.booleans()) else bucket_choice(ex, data.draw, extra)
                usecase_id = None if data.draw(st.booleans()) else data.draw(st.sampled_from(ex.targets()))
                ex.step_create_bucket(name, usecase_id)
            elif op == "configure":
                target = data.draw(st.sampled_from(ex.targets()))
                ex.step_configure(target, maybe_clear_or_bucket(ex, data.draw, extra))
            elif op == "delete" and ex.created:
                target = data.draw(st.sampled_from(ex.created))
                if target not in ex.deleted:
                    ex.step_delete(target)
            ex.check_invariants()
