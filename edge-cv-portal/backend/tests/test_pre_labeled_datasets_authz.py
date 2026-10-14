"""
Authorization of every pre-labeled dataset route
(portal-usecase-bucket-config, design §7; tasks.md 6.3).

`pre_labeled_datasets.lambda_handler` resolves the caller once and every
route checks membership (and the DataScientist role where it writes or
reads S3) against the use case it acts on:

- list / browse: `check_user_access(user_id, usecase_id)` for the query's
  use case -> 403 with no DynamoDB query and no S3 call;
- get: the record is read first (404 for an unknown id, before any
  authorization decision), then membership of the record's use case;
- delete: record -> 404 -> DataScientist on the record's use case -> the
  delete; a refused caller deletes nothing;
- create / validate: DataScientist on the request's use case before any
  S3 read or DynamoDB write; `created_by` is the authenticated caller
  through `_display_identity` (email -> username -> sub), never the body;
- every 403 is `{'error': 'Insufficient permissions'}` with one
  `unauthorized_access` audit row.

Harness (test_model_import_fine_tunable.py style): `check_user_access` is
a recorder with a per-test verdict, the DynamoDB table and the S3 client
factory are recorders, the use case lookup returns a crafted record.

Validates: Requirements 4.1–4.4, 15.2
"""
import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any, List

import pytest

import shared_utils as su


def _bind_live_shared_utils():
    """Rebind `su` to the shared layer module the handler modules will import.

    An earlier aws_stack-based file in the same process pops `shared_utils`
    from sys.modules and re-imports it, so the module object bound at
    collection time may no longer be the one `from shared_utils import ...`
    resolves to; patching that stale object would leave the handlers on the
    real seams. Called first by every module-scoped fixture here.
    """
    global su
    su = importlib.import_module("shared_utils")
    assert hasattr(su, "assert_bucket_allowed"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    return su

USECASE_ID = "uc-pld"
BUCKET = "uc-pld-bucket"
USER_ID = "user-pld"


class Recorder:
    def __init__(self, result=None):
        self.calls: List[Any] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class FakeTable:
    def __init__(self, records=None):
        self.records = dict(records or {})
        self.puts: List[dict] = []
        self.deletes: List[dict] = []
        self.queries: List[dict] = []

    def get_item(self, Key):
        item = self.records.get(Key["dataset_id"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item):
        self.puts.append(Item)
        return {}

    def delete_item(self, Key):
        self.deletes.append(Key)
        return {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": []}


@pytest.fixture(scope="module")
def pld():
    _bind_live_shared_utils()
    sys.modules.pop("pre_labeled_datasets", None)
    return importlib.import_module("pre_labeled_datasets")


@pytest.fixture
def h(pld, monkeypatch):
    table = FakeTable({
        "ds-1": {"dataset_id": "ds-1", "usecase_id": USECASE_ID, "name": "one"},
    })
    access = Recorder(result=True)
    audit = Recorder()
    s3_factory = Recorder(result=SimpleNamespace())
    browse = Recorder(result={"folders": [], "files": [], "current_prefix": ""})
    validate_file = Recorder(result={"valid": True, "errors": [], "warnings": [], "stats": {}})
    monkeypatch.setattr(pld, "check_user_access", access)
    monkeypatch.setattr(pld, "log_audit_event", audit)
    monkeypatch.setattr(pld, "dynamodb", SimpleNamespace(Table=lambda _name: table))
    monkeypatch.setattr(pld, "get_usecase", lambda _id: {
        "usecase_id": USECASE_ID, "s3_bucket": BUCKET, "account_id": "acct-pld",
        "cross_account_role_arn": "arn:aws:iam::acct-pld:root", "external_id": "x"})
    monkeypatch.setattr(pld, "get_s3_client_for_bucket", s3_factory)
    monkeypatch.setattr(pld, "validate_manifest_file", validate_file)
    monkeypatch.setattr(pld, "browse_s3_bucket_util", browse)
    sts = Recorder(result={"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"})
    monkeypatch.setattr(su, "assume_usecase_role", sts)
    return SimpleNamespace(pld=pld, table=table, access=access, audit=audit,
                           s3_factory=s3_factory, browse=browse, validate_file=validate_file,
                           sts=sts)


def event(method, path, body=None, query=None, email="user-pld@example.com",
          username=USER_ID):
    claims = {"sub": USER_ID, "cognito:username": username, "custom:role": "DataScientist"}
    if email is not None:
        claims["email"] = email
    return {
        "httpMethod": method, "path": path, "resource": path,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query, "pathParameters": None,
        "requestContext": {"authorizer": {"claims": claims},
                           "identity": {"sourceIp": "203.0.113.9", "userAgent": "authz-test/1.0"}},
    }


def call(h, *args, **kwargs):
    response = h.pld.lambda_handler(event(*args, **kwargs), None)
    return response["statusCode"], json.loads(response["body"])


def assert_denied(h, status, body, usecase_id=USECASE_ID, required_role=None):
    assert status == 403
    assert body == {"error": "Insufficient permissions"}
    (_, kwargs), = h.audit.calls
    assert kwargs["action"] == "unauthorized_access"
    assert kwargs["result"] == "denied"
    assert kwargs["details"]["usecase_id"] == usecase_id
    if required_role:
        assert kwargs["details"]["required_role"] == required_role
    assert h.s3_factory.calls == [] and h.sts.calls == []
    assert h.table.puts == [] and h.table.deletes == []


# ------------------------------------------------------------ list / browse

def test_list_requires_membership(h):
    h.access.result = False
    status, body = call(h, "GET", "/datasets/pre-labeled", query={"usecase_id": USECASE_ID})
    assert_denied(h, status, body)
    assert h.table.queries == []
    (args, _), = h.access.calls
    assert args == (USER_ID, USECASE_ID)


def test_list_without_usecase_id_is_400_before_any_check(h):
    status, body = call(h, "GET", "/datasets/pre-labeled", query={})
    assert status == 400 and h.access.calls == []


def test_list_for_a_member_queries_the_use_case(h):
    status, body = call(h, "GET", "/datasets/pre-labeled", query={"usecase_id": USECASE_ID})
    assert status == 200 and body["datasets"] == []
    assert len(h.table.queries) == 1


def test_browse_requires_membership(h):
    h.access.result = False
    status, body = call(h, "GET", "/datasets/pre-labeled/browse",
                        query={"usecase_id": USECASE_ID, "prefix": "a/"})
    assert_denied(h, status, body)
    assert h.browse.calls == []


def test_browse_for_a_member_reaches_the_browse_utility(h):
    status, _ = call(h, "GET", "/datasets/pre-labeled/browse", query={"usecase_id": USECASE_ID})
    assert status == 200
    (_, kwargs), = h.browse.calls
    assert kwargs["usecase_id"] == USECASE_ID


# -------------------------------------------------------------- get / delete

def test_get_unknown_dataset_is_404_before_authorization(h):
    status, body = call(h, "GET", "/datasets/pre-labeled/ds-missing")
    assert status == 404 and h.access.calls == [] and h.audit.calls == []


def test_get_requires_membership_of_the_records_use_case(h):
    h.access.result = False
    status, body = call(h, "GET", "/datasets/pre-labeled/ds-1")
    assert_denied(h, status, body)
    (args, _), = h.access.calls
    assert args == (USER_ID, USECASE_ID)


def test_get_for_a_member_returns_the_record(h):
    status, body = call(h, "GET", "/datasets/pre-labeled/ds-1")
    assert status == 200 and body["dataset"]["dataset_id"] == "ds-1"


def test_delete_unknown_dataset_is_404_before_authorization(h):
    status, _ = call(h, "DELETE", "/datasets/pre-labeled/ds-missing")
    assert status == 404 and h.access.calls == [] and h.table.deletes == []


def test_delete_requires_datascientist_on_the_records_use_case(h):
    h.access.result = False
    status, body = call(h, "DELETE", "/datasets/pre-labeled/ds-1")
    assert_denied(h, status, body, required_role="DataScientist")
    (args, _), = h.access.calls
    assert args == (USER_ID, USECASE_ID, "DataScientist")
    assert h.table.deletes == []


def test_delete_for_a_datascientist_deletes_the_record(h):
    status, _ = call(h, "DELETE", "/datasets/pre-labeled/ds-1")
    assert status == 200 and h.table.deletes == [{"dataset_id": "ds-1"}]


# ----------------------------------------------------------- create / validate

CREATE_BODY = {"name": "ds", "usecase_id": USECASE_ID,
               "manifest_s3_uri": f"s3://{BUCKET}/manifests/a.manifest",
               "created_by": "somebody-else@example.com"}


def test_create_requires_datascientist_before_any_write(h):
    h.access.result = False
    status, body = call(h, "POST", "/datasets/pre-labeled", body=CREATE_BODY)
    assert_denied(h, status, body, required_role="DataScientist")
    (args, _), = h.access.calls
    assert args == (USER_ID, USECASE_ID, "DataScientist")


def test_create_records_the_caller_not_the_body(h):
    status, body = call(h, "POST", "/datasets/pre-labeled", body=CREATE_BODY)
    assert status == 201, body
    (item,) = h.table.puts
    assert item["created_by"] == "user-pld@example.com"
    assert body["dataset"]["created_by"] == "user-pld@example.com"


def test_create_falls_back_to_username_then_sub_like_display_identity(h):
    status, body = call(h, "POST", "/datasets/pre-labeled", body=CREATE_BODY, email=None)
    assert status == 201, body
    assert h.table.puts[-1]["created_by"] == su._display_identity(None, USER_ID, USER_ID)
    assert h.table.puts[-1]["created_by"] == USER_ID

    status, body = call(h, "POST", "/datasets/pre-labeled", body=CREATE_BODY,
                        email="unknown", username="unknown")
    assert status == 201, body
    assert h.table.puts[-1]["created_by"] == USER_ID


def test_validate_requires_datascientist_before_any_s3_call(h):
    h.access.result = False
    status, body = call(h, "POST", "/datasets/validate-manifest",
                        body={"usecase_id": USECASE_ID,
                              "manifest_s3_uri": f"s3://{BUCKET}/manifests/a.manifest"})
    assert_denied(h, status, body, required_role="DataScientist")
    assert h.validate_file.calls == []


def test_validate_for_a_datascientist_reads_through_the_bucket_bound_client(h):
    status, body = call(h, "POST", "/datasets/validate-manifest",
                        body={"usecase_id": USECASE_ID,
                              "manifest_s3_uri": f"s3://{BUCKET}/manifests/a.manifest"})
    assert status == 200, body
    (args, _), = h.s3_factory.calls
    assert args[1] == BUCKET and args[2] == "prelabeled-validate"
    (args, kwargs), = h.validate_file.calls
    assert args[0] == BUCKET and args[1] == "manifests/a.manifest"
    assert kwargs["usecase"]["usecase_id"] == USECASE_ID
