"""
Property tests for the model-import sink of the use-case bucket
configuration (portal-usecase-bucket-config, design §3, §14; tasks.md 6.4,
P6–P7).

The oracle is hand-written from the record fields (the membership grammar
`[A-Za-z0-9._-]{3,255}` minus the portal-owned names from the test
environment) and never calls the implementation.

6. The import path never fetches outside the set — for any record and any
   artifact bucket, `import_model` reaches the artifact validator only
   when the bucket is a member, and every persisted `artifact_s3` names a
   member; on the consolidated `validate_model_artifact` itself, a bucket
   outside the set raises before `download_file`.
7. A refused validate never reaches the credential step — for any role
   verdict and any bucket, a denied principal gets 403 before the use case
   is read, a bucket outside the set gets 400 before `assume_usecase_role`,
   `boto3.client` or `download_file`.

Validates: Requirements 2.3, 3.1, 3.3, 15.3
"""
import importlib
import json
import re
import sys
from types import SimpleNamespace
from typing import Any, List

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

import shared_utils as su

ARTIFACTS = "test-portal-artifacts"
COMPONENT = "dda-component-us-east-1-portal"
PORTAL_OWNED = frozenset({ARTIFACTS, COMPONENT})
MEMBERSHIP = re.compile(r"[A-Za-z0-9._-]{3,255}")
USECASE_ID = "uc-import-prop"
USER_ID = "user-import-prop"
CROSS_ROLE = "arn:aws:iam::acct-usecase:role/DDAPortalAccessRole"
DATA_ROLE = "arn:aws:iam::acct-data:role/DDAPortalDataAccessRole"

VALIDATION_RESULT = {
    "valid": True,
    "metadata": {"image_width": 224, "image_height": 224, "input_shape": [1, 3, 224, 224],
                 "model_type": "classification", "pt_file": "m.pt", "model_file": "m.pt",
                 "framework": "PYTORCH", "framework_version": "1.8"},
    "files_found": [], "warnings": [],
}


def oracle_set(record) -> set:
    names = [record.get("s3_bucket"), record.get("data_s3_bucket"),
             *(record.get("additional_s3_buckets") or [])]
    return {n for n in names if isinstance(n, str) and MEMBERSHIP.fullmatch(n)} - PORTAL_OWNED


NAMES = ["uc-one-bucket", "uc-one-data", "uc-one-extra", "someone-elses-bucket",
         "Legacy_Bucket", "models.vendor", ARTIFACTS, COMPONENT]
pool_name = st.one_of(st.sampled_from(NAMES), st.none(),
                      st.from_regex(MEMBERSHIP, fullmatch=True).filter(lambda s: len(s) <= 30))
# Buckets that keep the request well-formed (the handlers' s3:// / .tar.gz
# shape checks pass) so the decision is the binding's alone.
request_bucket = st.one_of(st.sampled_from(NAMES),
                           st.from_regex(MEMBERSHIP, fullmatch=True).filter(lambda s: len(s) <= 30))


@st.composite
def records(draw):
    record = {"usecase_id": USECASE_ID, "account_id": "acct-usecase",
              "cross_account_role_arn": CROSS_ROLE, "external_id": "ext-1"}
    if draw(st.booleans()):
        record["s3_bucket"] = draw(pool_name)
    if draw(st.booleans()):
        record["data_s3_bucket"] = draw(pool_name)
        record["data_account_id"] = draw(st.sampled_from(["acct-usecase", "acct-data"]))
        record["data_account_role_arn"] = DATA_ROLE
        record["data_account_external_id"] = "ext-data"
    if draw(st.booleans()):
        record["additional_s3_buckets"] = draw(st.lists(pool_name, max_size=3))
    return record


class Recorder:
    def __init__(self, result=None):
        self.calls: List[Any] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class RecordingS3:
    def __init__(self):
        self.downloads: List[Any] = []

    def download_file(self, Bucket, Key, Filename):
        self.downloads.append((Bucket, Key))
        raise ClientError({"Error": {"Code": "404", "Message": "404"},
                           "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")


@pytest.fixture(scope="module")
def h():
    """Module-scoped harness (hypothesis bodies may not use function-scoped
    fixtures): the real model_import module, RBAC and the use-case lookup
    as controllable stubs, recorders on every STS / S3 / DynamoDB seam."""
    assert hasattr(su, "assert_bucket_allowed"), "this suite needs the real shared layer"
    mp = pytest.MonkeyPatch()
    mp.setenv("PORTAL_ARTIFACTS_BUCKET", ARTIFACTS)
    mp.setenv("COMPONENT_BUCKET", COMPONENT)
    mp.delenv("PORTAL_ACCOUNT_ID", raising=False)
    sys.modules.pop("model_import", None)
    mi = importlib.import_module("model_import")

    s3 = RecordingS3()
    state = SimpleNamespace(
        mi=mi, s3=s3, record={}, admit=True,
        assume=Recorder(result={"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}),
        boto3_client=Recorder(result=s3),
        lookup=Recorder(),
        validator=Recorder(result=VALIDATION_RESULT),
        items=[],
    )
    mp.setattr(su, "assume_usecase_role", state.assume)
    mp.setattr(su, "create_boto3_client", lambda service, credentials, region=None: s3)
    mp.setattr(su.boto3, "client", state.boto3_client)
    mp.setattr(su, "log_audit_event", Recorder())
    mp.setattr(su, "attribution_from", Recorder(result={}))
    mp.setattr(mi, "log_audit_event", Recorder())
    mp.setattr(mi, "check_user_access", lambda *a, **k: state.admit)

    def lookup(usecase_id):
        state.lookup(usecase_id)
        return dict(state.record)

    mp.setattr(mi, "get_usecase_details", lookup)
    real_validator = mi.validate_model_artifact

    def validator(uri, usecase, *, purpose):
        state.validator(uri, usecase, purpose=purpose)
        return json.loads(json.dumps(VALIDATION_RESULT))

    mp.setattr(mi, "validate_model_artifact", validator)
    state.real_validator = real_validator
    table = SimpleNamespace(put_item=lambda Item: state.items.append(Item))
    mp.setattr(mi, "dynamodb", SimpleNamespace(Table=lambda _n: table))
    mp.setattr(mi, "TRAINING_JOBS_TABLE", "test-training-jobs")

    def reset(record, admit=True):
        state.record = record
        state.admit = admit
        for recorder in (state.assume, state.boto3_client, state.lookup, state.validator):
            recorder.calls.clear()
        s3.downloads.clear()
        state.items.clear()

    state.reset = reset
    yield state
    mp.undo()


def event(handler_body):
    return {"httpMethod": "POST", "path": "/models", "resource": "/models",
            "body": json.dumps(handler_body),
            "requestContext": {"authorizer": {"claims": {
                "sub": USER_ID, "email": "user-import-prop@example.com",
                "cognito:username": USER_ID, "custom:role": "DataScientist"}}}}


def import_event(bucket):
    return event({"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
                  "model_s3_uri": f"s3://{bucket}/models/m.tar.gz"})


def validate_event(bucket):
    return event({"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{bucket}/models/m.tar.gz"})


class TestProperty6ImportNeverFetchesOutsideTheSet:
    """**Feature: portal-usecase-bucket-config, Property 6: Import never
    fetches outside the set** — the artifact validator is reached only for
    a member bucket and every persisted `artifact_s3` is in the set; the
    consolidated validator refuses a non-member before `download_file`.
    Validates: Requirements 2.3, 2.10"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), bucket=request_bucket)
    def test_import_model_binds_before_validation_and_persists_members_only(self, h, record,
                                                                             bucket):
        h.reset(record)
        allowed = oracle_set(record)

        response = h.mi.import_model(import_event(bucket), None)

        if bucket in allowed:
            assert response["statusCode"] == 201, response["body"]
            (args, kwargs), = h.validator.calls
            assert args[0] == f"s3://{bucket}/models/m.tar.gz" and kwargs["purpose"] == "model-import"
            (item,) = h.items
            persisted_bucket, _ = su.parse_s3_uri(item["artifact_s3"])
            assert persisted_bucket in allowed
        else:
            assert response["statusCode"] == 400, response["body"]
            assert json.loads(response["body"])["error_code"] == "bucket_not_allowed"
            assert h.validator.calls == []
            assert h.items == []
            assert h.assume.calls == [] and h.boto3_client.calls == []

    @settings(max_examples=150, deadline=None)
    @given(record=records(), bucket=request_bucket)
    def test_validate_model_artifact_downloads_members_only(self, h, record, bucket):
        h.reset(record)
        uri = f"s3://{bucket}/models/m.tar.gz"

        if bucket in oracle_set(record):
            with pytest.raises(h.mi.ModelValidationError):      # the recorded 404
                h.real_validator(uri, record, purpose="model-import")
            assert h.s3.downloads == [(bucket, "models/m.tar.gz")]
            assert len(h.assume.calls) == 1
        else:
            with pytest.raises(su.BucketNotAllowed):
                h.real_validator(uri, record, purpose="model-import")
            assert h.s3.downloads == []
            assert h.assume.calls == [] and h.boto3_client.calls == []


class TestProperty7RefusedValidateNeverReachesCredentials:
    """**Feature: portal-usecase-bucket-config, Property 7: A refused
    validate never reaches the credential step** — for any role verdict
    and any bucket, denial answers 403 before the use case is read and a
    non-member answers 400 before any STS or S3 call.
    Validates: Requirements 3.1, 3.3, 2.3"""

    @settings(max_examples=150, deadline=None)
    @given(record=records(), bucket=request_bucket, admitted=st.booleans())
    def test_validate_refuses_before_credentials(self, h, record, bucket, admitted):
        h.reset(record, admit=admitted)

        response = h.mi.validate_model(validate_event(bucket), None)

        if not admitted:
            assert response["statusCode"] == 403
            assert h.lookup.calls == []
            assert h.validator.calls == []
            assert h.assume.calls == [] and h.boto3_client.calls == [] and h.s3.downloads == []
        elif bucket not in oracle_set(record):
            assert response["statusCode"] == 400, response["body"]
            assert json.loads(response["body"])["error_code"] == "bucket_not_allowed"
            assert h.validator.calls == []
            assert h.assume.calls == [] and h.boto3_client.calls == [] and h.s3.downloads == []
        else:
            assert response["statusCode"] == 200, response["body"]
            (args, kwargs), = h.validator.calls
            assert kwargs["purpose"] == "model-validate"
