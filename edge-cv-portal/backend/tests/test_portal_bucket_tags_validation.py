"""
The one tag list the portal writes (portal-authorization-and-iam-scoping,
tasks.md 5.4; design I5 "ABAC lifecycle"; Requirement 7.7).

`usecases._portal_bucket_tags()` builds the list POST /s3-buckets sends and
returns it through the shared layer's `assert_portal_tags`, so both of its
write consumers — the `CreateBucketConfiguration.Tags` argument of
`create_bucket` and the `Tags` argument of the one fallback
`s3control.tag_resource` — receive a validated list by construction. The
handler runs on the moto `aws_stack` (use-case and user-role tables) with a
scripted S3 / S3 Control pair on the `usecases` module's `boto3`; the source
checks read the handler files.

Validates: Requirements 7.6, 7.7
"""
import ast
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(HERE, ".."))
USECASES_SOURCE = os.path.join(BACKEND, "functions", "usecases.py")

MANAGED = "dda-portal:managed"
CREATED_BY = "dda-portal:created-by"
CLAIM = "dda-portal:usecase-id"
PORTAL_ACCOUNT = "111111111111"


def client_error(code, status, operation="Op"):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class TagRecordingS3:
    """The `s3` client `create_s3_bucket` builds. `create_bucket` keeps the
    exact `Tags` object it received; `get_bucket_tagging` plays `script`
    ('sent' echoes the tags last written, the last entry repeats); the probe
    calls of the wizard path answer as an empty, reachable bucket."""

    def __init__(self, script=("sent",), abac_error=None):
        self.script = list(script)
        self.abac_error = abac_error
        self.calls = []
        self.create_tags = None
        self.sent = []

    def create_bucket(self, **kwargs):
        self.calls.append("create_bucket")
        self.create_tags = kwargs["CreateBucketConfiguration"]["Tags"]
        self.sent = list(self.create_tags)
        return {}

    def put_bucket_abac(self, **kwargs):
        self.calls.append("put_bucket_abac")
        if self.abac_error is not None:
            raise self.abac_error
        return {}

    def get_bucket_tagging(self, **kwargs):
        self.calls.append("get_bucket_tagging")
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return {"TagSet": list(self.sent)}

    def head_bucket(self, **kwargs):
        self.calls.append("head_bucket")
        return {"ResponseMetadata": {"HTTPStatusCode": 200,
                                     "HTTPHeaders": {"x-amz-bucket-region": "us-east-1"}}}

    def list_objects_v2(self, **kwargs):
        self.calls.append("list_objects_v2")
        return {"KeyCount": 0}


class TagRecordingS3Control:
    def __init__(self, s3):
        self.s3 = s3
        self.tags = None
        self.calls = 0

    def tag_resource(self, **kwargs):
        self.calls += 1
        self.tags = kwargs["Tags"]
        self.s3.sent = list(kwargs["Tags"])
        return {}


@pytest.fixture(scope="module")
def uc(aws_stack):
    # Import the real layer while the moto mock is active, before usecases
    # (which imports it); the name itself is not used here.
    import shared_utils
    sys.modules.pop("usecases", None)
    import usecases
    return usecases


@pytest.fixture
def env(aws_stack, uc, monkeypatch):
    import boto3 as real_boto3
    import shared_utils as shared
    monkeypatch.setenv("PORTAL_ACCOUNT_ID", PORTAL_ACCOUNT)
    monkeypatch.delenv(shared.PORTAL_REGISTRY_ENFORCED_ENV, raising=False)
    monkeypatch.setattr(shared.time, "sleep", lambda seconds: None)
    sub = f"user-{uuid.uuid4().hex[:12]}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": sub, "usecase_id": "global", "role": "PortalAdmin", "username": sub,
        "email": f"{sub}@example.com", "status": "enabled", "assigned_by": "test", "assigned_at": 1})
    admin = {"user_id": sub, "email": f"{sub}@example.com", "username": sub, "role": "PortalAdmin"}

    def install(**kwargs):
        s3 = TagRecordingS3(**kwargs)
        s3control = TagRecordingS3Control(s3)

        def client(service, **client_kwargs):
            if service == "s3":
                return s3
            if service == "s3control":
                return s3control
            return real_boto3.client(service, **client_kwargs)

        monkeypatch.setattr(uc, "boto3", SimpleNamespace(client=client, resource=real_boto3.resource))
        return SimpleNamespace(s3=s3, s3control=s3control)

    def create_bucket(body):
        event = {
            "httpMethod": "POST", "path": "/s3-buckets", "resource": "/s3-buckets",
            "pathParameters": None, "queryStringParameters": None, "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": {
                "sub": admin["user_id"], "email": admin["email"],
                "cognito:username": admin["username"], "custom:role": admin["role"]}}},
        }
        response = uc.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    return SimpleNamespace(uc=uc, shared=shared, install=install, create_bucket=create_bucket,
                           monkeypatch=monkeypatch)


def bucket_name(prefix="tags"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ValidatorRecorder:
    """Wraps the real `assert_portal_tags` and keeps every list it returned."""

    def __init__(self, real):
        self.real = real
        self.returned = []

    def __call__(self, tags):
        result = self.real(tags)
        self.returned.append(result)
        return result


# ----------------------------------------------------------- the builder

def test_the_builder_returns_the_sibling_lists(uc):
    assert uc._portal_bucket_tags(None) == [
        {"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"}]
    assert uc._portal_bucket_tags("uc-7") == [
        {"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"},
        {"Key": CLAIM, "Value": "uc-7"}]


def test_the_builder_returns_through_assert_portal_tags(env):
    sentinel = [{"Key": MANAGED, "Value": "true"}]
    seen = []

    def validator(tags):
        seen.append(list(tags))
        return sentinel

    env.monkeypatch.setattr(env.uc, "assert_portal_tags", validator)
    assert env.uc._portal_bucket_tags("uc-8") is sentinel
    assert seen == [[{"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"},
                     {"Key": CLAIM, "Value": "uc-8"}]]


def test_a_key_outside_the_namespace_stops_before_any_s3_call(env):
    env.monkeypatch.setattr(env.uc, "PORTAL_CREATED_BY_TAG", "created-by")
    with pytest.raises(ValueError):
        env.uc._portal_bucket_tags(None)
    stubs = env.install()
    status, payload = env.create_bucket({"name": bucket_name()})
    assert status == 500, payload
    assert stubs.s3.calls == [] and stubs.s3control.calls == 0


# --------------------------------------------------- the two consumers

def test_both_write_consumers_receive_the_validated_list(env):
    validator = ValidatorRecorder(env.uc.assert_portal_tags)
    env.monkeypatch.setattr(env.uc, "assert_portal_tags", validator)
    missing = client_error("NoSuchTagSet", 404, "GetBucketTagging")
    stubs = env.install(script=[missing, missing, missing, "sent"])
    status, payload = env.create_bucket({"name": bucket_name()})
    assert status == 201, payload
    [validated] = validator.returned
    assert validated == [{"Key": MANAGED, "Value": "true"}, {"Key": CREATED_BY, "Value": "portal"}]
    # The very list the validator returned, in both writes.
    assert stubs.s3.create_tags is validated
    assert stubs.s3control.calls == 1 and stubs.s3control.tags is validated


def test_created_bucket_reports_tagged_and_abac_enabled(env):
    env.install()
    status, payload = env.create_bucket({"name": bucket_name()})
    assert status == 201, payload
    bucket = payload["bucket"]
    assert bucket["abac"] == "enabled"
    assert bucket["tagged"] is True and bucket["abac_enabled"] is True

    env.install(abac_error=client_error("AccessDenied", 403, "PutBucketAbac"))
    status, payload = env.create_bucket({"name": bucket_name()})
    assert status == 201, payload
    bucket = payload["bucket"]
    assert bucket["abac"] == "failed"
    assert bucket["tagged"] is True and bucket["abac_enabled"] is False


# ------------------------------------------------------- source checks

def _function(tree, name):
    [found] = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == name]
    return found


def test_the_builder_result_has_exactly_two_write_consumers():
    """Every place create_s3_bucket hands the built list to S3 as `Tags`:
    the CreateBucketConfiguration of create_bucket and the fallback
    s3control.tag_resource — and no other function builds the list."""
    with open(USECASES_SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    builders = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "_portal_bucket_tags"]
    assert len(builders) == 1
    handler = _function(tree, "create_s3_bucket")
    [assignment] = [node for node in ast.walk(handler) if isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "_portal_bucket_tags"]
    variable = assignment.targets[0].id

    def is_variable(node):
        return isinstance(node, ast.Name) and node.id == variable

    create_configurations = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "CreateBucketConfiguration"
                        and isinstance(value, ast.Dict)):
                    create_configurations.add(id(value))
    consumers = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "Tags" and is_variable(value):
                    consumers.append("CreateBucketConfiguration.Tags"
                                     if id(node) in create_configurations else "Tags")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            consumers.extend(f"{node.func.attr}(Tags=)" for keyword in node.keywords
                             if keyword.arg == "Tags" and is_variable(keyword.value))
    assert sorted(consumers) == ["CreateBucketConfiguration.Tags", "tag_resource(Tags=)"]


def _portal_python_files():
    """Every .py under edge-cv-portal/backend except the tests."""
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_no_put_bucket_tagging_call_site_under_the_backend():
    sites = []
    for path in _portal_python_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if "put_bucket_tagging" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "put_bucket_tagging"):
                sites.append(os.path.relpath(path, BACKEND))
    assert sites == []
