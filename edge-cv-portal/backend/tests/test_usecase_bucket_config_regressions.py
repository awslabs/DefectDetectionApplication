"""
Regression tests for the use-case bucket configuration
(portal-usecase-bucket-config, Requirement 15.4; tasks.md 6.5).

Each test pins one behaviour the feature introduced at the model-import
sink and in the shared helper, with recorders on every path to STS and S3:

- `get_s3_client_for_bucket` refuses a bucket outside the use case's
  configured buckets with `assume_usecase_role`, `create_boto3_client` and
  `boto3.client` never called;
- `POST /models/validate` answers 403 for a principal that holds no role on
  the use case (the gate now requires DataScientist, resolved through the
  real `check_user_access`) and reads nothing;
- `POST /models/validate` for a DataScientist naming an unconfigured bucket
  answers 400 `bucket_not_allowed` and never calls `download_file`;
- `validate_model_artifact('s3://<foreign>/m.tar.gz', usecase,
  purpose=...)` raises `BucketNotAllowed` directly, with `download_file`,
  `assume_usecase_role` and `boto3.client` recorders empty — the
  consolidated client path refuses even for a caller that skipped the
  handler's pre-check (design-review finding 13).

Validates: Requirements 1.3, 1.5, 2.3, 3.1, 3.3, 15.4
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

FOREIGN = "someone-elses-bucket"
UC_BUCKET = "uc-reg-bucket"
USECASE_ID = "uc-reg"
USER_ID = "user-reg"
ROLE_ARN = "arn:aws:iam::acct-reg:role/DDAPortalAccessRole"


class Recorder:
    def __init__(self, result=None):
        self.calls: List[Any] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class RecordingS3:
    def __init__(self):
        self.calls: List[Any] = []

    def download_file(self, Bucket, Key, Filename):
        self.calls.append(("download_file", Bucket, Key))
        raise AssertionError("download_file must not be reached")


def usecase_record() -> dict:
    return {"usecase_id": USECASE_ID, "account_id": "acct-reg", "s3_bucket": UC_BUCKET,
            "cross_account_role_arn": ROLE_ARN, "external_id": "ext-reg"}


@pytest.fixture(scope="module")
def mi():
    _bind_live_shared_utils()
    sys.modules.pop("model_import", None)
    return importlib.import_module("model_import")


@pytest.fixture
def recorders(monkeypatch):
    """Recorders on the shared layer's STS / S3 client construction and on
    the audit writer (so a refusal never reaches DynamoDB)."""
    s3 = RecordingS3()
    rec = SimpleNamespace(
        assume=Recorder(result={"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}),
        create_client=Recorder(result=s3),
        boto3_client=Recorder(result=s3),
        audit=Recorder(),
        s3=s3,
    )
    monkeypatch.setattr(su, "assume_usecase_role", rec.assume)
    monkeypatch.setattr(su, "create_boto3_client", rec.create_client)
    monkeypatch.setattr(su.boto3, "client", rec.boto3_client)
    monkeypatch.setattr(su, "log_audit_event", rec.audit)
    monkeypatch.setattr(su, "attribution_from", Recorder(result={}))
    return rec


def validate_event(bucket=FOREIGN, role_claim="DataScientist"):
    return {
        "httpMethod": "POST", "path": "/models/validate", "resource": "/models/validate",
        "body": json.dumps({"usecase_id": USECASE_ID,
                            "model_s3_uri": f"s3://{bucket}/models/m.tar.gz"}),
        "requestContext": {"authorizer": {"claims": {
            "sub": USER_ID, "email": "user-reg@example.com",
            "cognito:username": USER_ID, "custom:role": role_claim}}},
    }


def test_helper_refuses_a_bucket_outside_the_set_with_empty_recorders(recorders):
    """Req 1.3, 1.5: the refusal happens before role selection and STS."""
    with pytest.raises(su.BucketNotAllowed) as excinfo:
        su.get_s3_client_for_bucket(usecase_record(), FOREIGN, "regression")

    assert excinfo.value.error_code == "bucket_not_allowed"
    assert excinfo.value.bucket == FOREIGN
    assert recorders.assume.calls == []
    assert recorders.create_client.calls == []
    assert recorders.boto3_client.calls == []
    assert recorders.s3.calls == []


def test_validate_model_is_403_for_a_principal_without_a_role(mi, recorders, monkeypatch):
    """Req 3.1, 3.3: a caller the registry resolves to no role is refused
    by the real check_user_access before the use case is read."""
    monkeypatch.setattr(su.rbac_manager, "get_user_role", lambda *a, **k: None)
    lookup = Recorder(result=usecase_record())
    monkeypatch.setattr(mi, "get_usecase_details", lookup)

    response = mi.validate_model(validate_event(bucket=UC_BUCKET), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "Insufficient permissions"}
    assert lookup.calls == []
    assert recorders.assume.calls == [] and recorders.s3.calls == []


def test_validate_model_refuses_an_unconfigured_bucket_without_downloading(mi, recorders,
                                                                              monkeypatch):
    """Req 2.3, 3.1: a DataScientist naming a bucket outside the set gets
    400 bucket_not_allowed; download_file is never called."""
    monkeypatch.setattr(mi, "check_user_access", lambda *a, **k: True)
    monkeypatch.setattr(mi, "get_usecase_details", lambda _id: usecase_record())

    response = mi.validate_model(validate_event(bucket=FOREIGN), None)

    assert response["statusCode"] == 400, response["body"]
    body = json.loads(response["body"])
    assert body["error_code"] == "bucket_not_allowed"
    assert body["bucket"] == FOREIGN and body["purpose"] == "model-validate"
    assert recorders.s3.calls == []
    assert recorders.assume.calls == []
    assert recorders.boto3_client.calls == []


def test_validate_model_artifact_refuses_foreign_bucket_directly(mi, recorders):
    """Req 2.3, 15.4 (finding 13): the consolidated function itself raises
    BucketNotAllowed for a bucket outside the set, before any credential or
    download step, so a caller that skips the handler's pre-check cannot
    fetch from it."""
    with pytest.raises(su.BucketNotAllowed) as excinfo:
        mi.validate_model_artifact(f"s3://{FOREIGN}/m.tar.gz", usecase_record(),
                                   purpose="model-validate")

    assert excinfo.value.bucket == FOREIGN
    assert excinfo.value.purpose.startswith("model-validate-")
    assert recorders.s3.calls == []            # download_file
    assert recorders.assume.calls == []        # assume_usecase_role
    assert recorders.boto3_client.calls == []  # boto3.client
    assert recorders.create_client.calls == []
    assert not hasattr(mi, "assume_usecase_role")
    assert not hasattr(mi, "sts")
