"""
Handler-level tests for the use-case bucket configuration sinks
(portal-usecase-bucket-config, design §3, §4, §7; tasks.md 6.1).

Every handler that takes a bucket from the request (a bare name or an
`s3://` URI) binds it to the use case's configured buckets — `s3_bucket`,
`data_s3_bucket` and the `additional_s3_buckets` list, minus the portal's
own buckets — before any STS or S3 call. Covered here, with the repo's
monkeypatch harness (test_model_import_fine_tunable.py style: RBAC and the
use-case lookup are stubs, recorders sit on `assume_usecase_role`,
`create_boto3_client`, `boto3.client` and every S3 method):

- for every sink, a foreign bucket -> 400 with `error_code`
  `bucket_not_allowed`, the sink's `purpose`, and every recorder empty;
- each portal-owned bucket (artifacts, component) refused by every sink,
  including when the use case record names it as `s3_bucket`;
- allowed members proceed with the pre-change role: the execution role
  (root ARN) for single-account use cases, the Data Account role for
  `data_s3_bucket` on a separate-Data-Account use case, the cross-account
  role for `s3_bucket` and additional entries — on the consolidated
  `validate_model_artifact` path and on the data_management handlers;
- `create_training_job` binds the manifest whatever the `model_source`;
  `transform_manifest` binds source then output (default output = source
  bucket; explicit allowed output through its own client; explicit foreign
  output refused);
- manifest-internal references outside the set are skipped with a warning,
  no helper call and no audit row (design §4);
- `evaluate_fit_check` reports `unverified` for a refused artifact;
- `model_converter`'s trusted-source rule is an exact bucket-name rule
  (configured buckets plus TRUSTED_MODEL_BUCKETS; the former account
  substring rule admits nothing), checked before any STS call, and the
  inspect gate requires DataScientist;
- `data_management.list_buckets` entries carry `allowed` / `portal_created`
  and the tagging instructions name the S3 Control TagResource form;
- labeling: `create_dda_job` without CREATE_LABELING_JOBS -> 403 through
  `has_permission(..., user_info)` with empty recorders; bare-key and
  foreign `s3://` example references -> 400 `validation_errors` with a
  per-ref `error_code`; `rerun_prelabels` refuses stored refs outside the
  set before the worker invoke; `_example_image_urls` skips without
  presigning; the worker resolves a bare key to nothing.

The flag-state / three-principal matrix of the role gates lives in
test_property_model_validate_role_gate.py (moto registry).

Validates: Requirements 2.1–2.10, 3.2, 4.5, 4.6, 6.1–6.3, 15.1, 15.2
"""
import importlib
import io
import json
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

import shared_utils as su

ARTIFACTS = "test-portal-artifacts"   # conftest's PORTAL_ARTIFACTS_BUCKET
COMPONENT = "dda-component-us-east-1-portal"
FOREIGN = "someone-elses-bucket"
UC_BUCKET = "uc-one-bucket"
DATA_BUCKET = "uc-one-data"
EXTRA_BUCKET = "uc-one-extra"
USECASE_ID = "uc-1"
USER_ID = "user-1"
CROSS_ACCOUNT_ROLE = "arn:aws:iam::acct-usecase:role/DDAPortalAccessRole"
ROOT_ARN = "arn:aws:iam::acct-usecase:root"
DATA_ROLE = "arn:aws:iam::acct-data:role/DDAPortalDataAccessRole"

HANDLER_MODULES = ("training", "model_import", "model_converter",
                   "pre_labeled_datasets", "data_management",
                   "dda_labeling", "dda_autolabel_worker")


# --------------------------------------------------------------- helpers

class Recorder:
    """Callable recording every call; returns `result` or raises."""

    def __init__(self, result=None, raises=None):
        self.calls: List[Any] = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class FakeS3:
    """An S3 client stand-in recording every operation.

    `objects` maps (bucket, key) -> bytes; head/get of anything else answer
    404 / NoSuchKey, download_file always answers 404 (the artifact
    validator only needs to show it reached the download with the right
    bucket).
    """

    def __init__(self, objects: Dict = None):
        self.objects = dict(objects or {})
        self.calls: List[Any] = []

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def head_object(self, Bucket, Key):
        self._record("head_object", Bucket=Bucket, Key=Key)
        if (Bucket, Key) in self.objects:
            return {"ContentLength": len(self.objects[(Bucket, Key)])}
        raise client_error("404", 404, "HeadObject")

    def get_object(self, Bucket, Key):
        self._record("get_object", Bucket=Bucket, Key=Key)
        if (Bucket, Key) in self.objects:
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        raise client_error("NoSuchKey", 404, "GetObject")

    def put_object(self, **kwargs):
        self._record("put_object", **kwargs)
        return {}

    def download_file(self, Bucket, Key, Filename):
        self._record("download_file", Bucket=Bucket, Key=Key)
        raise client_error("404", 404, "HeadObject")

    def list_objects_v2(self, **kwargs):
        self._record("list_objects_v2", **kwargs)
        return {"CommonPrefixes": [], "Contents": [], "IsTruncated": False}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self._record("generate_presigned_url", operation=operation, **Params)
        return f"https://{Params['Bucket']}.s3.example/{Params['Key']}?X-Amz-Expires={ExpiresIn}"

    def names(self):
        return [name for name, _ in self.calls]


class FakeTable:
    def __init__(self):
        self.items: List[Dict] = []
        self.queries: List[Dict] = []

    def put_item(self, Item):
        self.items.append(Item)
        return {}

    def get_item(self, Key):
        return {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": []}

    def delete_item(self, Key):
        return {}


class FakeDynamo:
    def __init__(self):
        self.table = FakeTable()

    def Table(self, _name):
        return self.table


def usecase_record(**overrides) -> Dict:
    """A multi-account use case with one additional bucket."""
    record = {
        "usecase_id": USECASE_ID,
        "name": "Sinks",
        "account_id": "acct-usecase",
        "region": "us-east-1",
        "s3_bucket": UC_BUCKET,
        "cross_account_role_arn": CROSS_ACCOUNT_ROLE,
        "external_id": "ext-1",
        "additional_s3_buckets": [EXTRA_BUCKET],
    }
    record.update(overrides)
    return record


def separate_data_account_record(**overrides) -> Dict:
    return usecase_record(
        data_s3_bucket=DATA_BUCKET, data_account_id="acct-data",
        data_account_role_arn=DATA_ROLE, data_account_external_id="ext-data",
        **overrides)


def single_account_record(**overrides) -> Dict:
    return usecase_record(cross_account_role_arn=ROOT_ARN, **overrides)


def api_event(body=None, query=None, path_params=None, method="POST",
              path="/sink", role_claim="DataScientist") -> Dict:
    return {
        "httpMethod": method,
        "path": path,
        "resource": path,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query,
        "pathParameters": path_params,
        "requestContext": {
            "authorizer": {"claims": {
                "sub": USER_ID, "email": "user-1@example.com",
                "cognito:username": USER_ID, "custom:role": role_claim}},
            "identity": {"sourceIp": "203.0.113.7", "userAgent": "sinks-test/1.0"},
        },
    }


def body_of(response) -> Dict:
    return json.loads(response["body"])


MANIFEST_LINE = json.dumps({
    "source-ref": f"s3://{UC_BUCKET}/images/a.jpg",
    "anomaly-label": 0,
    "anomaly-label-metadata": {"class-name": "normal", "confidence": 1},
})


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def m():
    """Fresh handler modules bound to the real shared layer."""
    assert hasattr(su, "assert_bucket_allowed"), (
        "a fake shared_utils is installed in sys.modules; this suite needs "
        "the real layer module")
    modules = {}
    for name in HANDLER_MODULES:
        sys.modules.pop(name, None)
        modules[name] = importlib.import_module(name)
    return SimpleNamespace(**modules)


@pytest.fixture
def h(m, monkeypatch):
    """The monkeypatch harness: portal-owned names in the environment, RBAC
    stubs that admit, recorders on every path to STS / S3 / DynamoDB."""
    monkeypatch.setenv("PORTAL_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("COMPONENT_BUCKET", COMPONENT)
    monkeypatch.delenv("PORTAL_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("TRUSTED_MODEL_BUCKETS", raising=False)
    monkeypatch.delenv("TRUSTED_MODEL_ACCOUNTS", raising=False)

    fake_s3 = FakeS3({
        (UC_BUCKET, "manifests/train.manifest"): (MANIFEST_LINE + "\n").encode(),
        (UC_BUCKET, "images/a.jpg"): b"jpeg",
        (DATA_BUCKET, "manifests/train.manifest"): (MANIFEST_LINE + "\n").encode(),
        (EXTRA_BUCKET, "masks/a.png"): b"png",
    })
    credentials = {"AccessKeyId": "k", "SecretAccessKey": "s", "SessionToken": "t"}
    rec = SimpleNamespace(
        assume=Recorder(result=credentials),
        create_client=Recorder(result=fake_s3),
        boto3_client=Recorder(result=FakeS3()),
        audit=Recorder(),
        attribution=Recorder(result={}),
        s3=fake_s3,
    )
    # Shared layer seams (the helper's STS and S3 client construction).
    monkeypatch.setattr(su, "assume_usecase_role", rec.assume)
    monkeypatch.setattr(su, "create_boto3_client", rec.create_client)
    monkeypatch.setattr(su.boto3, "client", rec.boto3_client)   # every module's boto3.client
    monkeypatch.setattr(su, "log_audit_event", rec.audit)
    monkeypatch.setattr(su, "attribution_from", rec.attribution)

    # Per-module RBAC stubs (record the role argument) and audit stubs.
    access = {}
    for name in ("training", "model_import", "model_converter",
                 "pre_labeled_datasets", "data_management"):
        module = getattr(m, name)
        access[name] = Recorder(result=True)
        monkeypatch.setattr(module, "check_user_access", access[name])
        monkeypatch.setattr(module, "log_audit_event", rec.audit)

    usecase = {"record": usecase_record()}

    def get_usecase(_usecase_id):
        return dict(usecase["record"])

    monkeypatch.setattr(m.training, "get_usecase", get_usecase)
    monkeypatch.setattr(m.pre_labeled_datasets, "get_usecase", get_usecase)
    monkeypatch.setattr(m.data_management, "get_usecase", get_usecase)
    monkeypatch.setattr(m.dda_labeling, "get_usecase", get_usecase)
    monkeypatch.setattr(m.dda_autolabel_worker, "get_usecase", get_usecase)
    monkeypatch.setattr(m.model_import, "get_usecase_details", get_usecase)
    monkeypatch.setattr(m.model_converter, "get_usecase_details", get_usecase)

    # Module-local credential paths that must stay untouched by a refusal.
    rec.usecase_client = Recorder(result=fake_s3)
    monkeypatch.setattr(m.model_import, "get_usecase_client", rec.usecase_client)
    rec.converter_assume = Recorder(result={"is_default_credentials": True})
    monkeypatch.setattr(m.model_converter, "assume_usecase_role", rec.converter_assume)
    rec.converter_client = Recorder(result=fake_s3)
    monkeypatch.setattr(m.model_converter, "make_usecase_s3_client", rec.converter_client)
    rec.data_credentials = Recorder(result=None)
    monkeypatch.setattr(m.data_management, "get_data_account_credentials",
                        rec.data_credentials)
    rec.cors = Recorder()
    monkeypatch.setattr(m.data_management, "ensure_bucket_cors", rec.cors)

    # DynamoDB writes (training jobs, datasets) are recorders.
    dynamo = FakeDynamo()
    for module in (m.training, m.model_import, m.pre_labeled_datasets):
        monkeypatch.setattr(module, "dynamodb", dynamo)
    monkeypatch.setattr(m.training, "TRAINING_JOBS_TABLE", "test-training-jobs")
    monkeypatch.setattr(m.model_import, "TRAINING_JOBS_TABLE", "test-training-jobs")

    def set_usecase(record):
        usecase["record"] = record

    def refusal_audits():
        return [c for c in rec.audit.calls
                if (c[1].get("action") or (c[0][1] if len(c[0]) > 1 else None))
                == "s3_bucket_refused"]

    def reset():
        for recorder in (rec.assume, rec.create_client, rec.boto3_client,
                         rec.usecase_client, rec.converter_assume, rec.converter_client,
                         rec.data_credentials, rec.audit):
            recorder.calls.clear()
        fake_s3.calls.clear()

    def assert_no_io():
        assert rec.assume.calls == []
        assert rec.create_client.calls == []
        assert rec.boto3_client.calls == []
        assert rec.usecase_client.calls == []
        assert rec.converter_assume.calls == []
        assert rec.converter_client.calls == []
        assert rec.data_credentials.calls == []
        assert fake_s3.calls == []
        assert dynamo.table.items == []

    return SimpleNamespace(m=m, rec=rec, access=access, dynamo=dynamo, s3=fake_s3,
                           set_usecase=set_usecase, refusal_audits=refusal_audits,
                           assert_no_io=assert_no_io, reset=reset, monkeypatch=monkeypatch)


# ------------------------------------------------- the request-bucket sinks

def _training_create(h, bucket, **extra):
    body = {"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
            "model_type": "classification",
            "dataset_manifest_s3": f"s3://{bucket}/manifests/train.manifest"}
    body.update(extra)
    return h.m.training.create_training_job(api_event(body), None)


def _transform(h, source_bucket, output_bucket=None):
    body = {"usecase_id": USECASE_ID,
            "source_manifest_uri": f"s3://{source_bucket}/manifests/train.manifest"}
    if output_bucket:
        body["output_manifest_uri"] = f"s3://{output_bucket}/manifests/train-dda.manifest"
    return h.m.training.transform_manifest(api_event(body), None)


def _validate_model(h, bucket):
    return h.m.model_import.validate_model(api_event(
        {"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{bucket}/models/m.tar.gz"}), None)


def _import_model(h, bucket):
    return h.m.model_import.import_model(api_event(
        {"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
         "model_s3_uri": f"s3://{bucket}/models/m.tar.gz"}), None)


def _register_vllm(h, bucket):
    return h.m.model_import.register_vllm_model(api_event(
        {"usecase_id": USECASE_ID, "model_name": "m", "model_version": "1",
         "s3_model_artifact": f"s3://{bucket}/models/m.tar.gz"}), None)


def _convert(h, bucket):
    return h.m.model_converter.convert_model(api_event(
        {"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{bucket}/raw/model.pt",
         "model_name": "m", "model_type": "classification",
         "image_width": 224, "image_height": 224}), None)


def _inspect(h, bucket):
    return h.m.model_converter.inspect_model_endpoint(api_event(
        {"usecase_id": USECASE_ID, "model_s3_uri": f"s3://{bucket}/raw/model.pt"}), None)


def _pld_user():
    return {"user_id": USER_ID, "email": "user-1@example.com",
            "username": USER_ID, "role": "DataScientist"}


def _pld_validate(h, bucket):
    return h.m.pre_labeled_datasets.validate_manifest(api_event(
        {"usecase_id": USECASE_ID,
         "manifest_s3_uri": f"s3://{bucket}/manifests/train.manifest"}), _pld_user())


def _pld_create(h, bucket):
    return h.m.pre_labeled_datasets.create_dataset(api_event(
        {"usecase_id": USECASE_ID, "name": "ds",
         "manifest_s3_uri": f"s3://{bucket}/manifests/train.manifest"}), _pld_user())


def _dm_event(body=None, query=None, method="POST"):
    return api_event(body=body, query=query, path_params={"id": USECASE_ID},
                     method=method)


def _dm_list_folders(h, bucket):
    return h.m.data_management.list_folders(_dm_event(query={"bucket": bucket}, method="GET"))


def _dm_create_folder(h, bucket):
    return h.m.data_management.create_folder(_dm_event({"bucket": bucket, "folder_path": "raw/"}))


def _dm_upload_url(h, bucket):
    return h.m.data_management.get_upload_url(_dm_event({"bucket": bucket, "key": "raw/a.jpg"}))


def _dm_batch_upload(h, bucket):
    return h.m.data_management.get_batch_upload_urls(_dm_event(
        {"bucket": bucket, "prefix": "raw", "files": [{"filename": "a.jpg"}]}))


# (name, invoke, purpose recorded on the refusal)
SINKS = [
    ("training.create_training_job", _training_create, "training-manifest"),
    ("training.transform_manifest.source", _transform, "transform-source"),
    # The source is the additional bucket, which stays in the set when a
    # portal-owned record case re-points s3_bucket.
    ("training.transform_manifest.output",
     lambda h, b: _transform(h, EXTRA_BUCKET, output_bucket=b), "transform-output"),
    ("model_import.validate_model", _validate_model, "model-validate"),
    ("model_import.import_model", _import_model, "model-import"),
    ("model_import.register_vllm_model", _register_vllm, "vllm-register"),
    ("model_converter.convert_model", _convert, "model-convert"),
    ("model_converter.inspect_model_endpoint", _inspect, "model-inspect"),
    ("pre_labeled_datasets.validate_manifest", _pld_validate, "prelabeled-validate"),
    ("pre_labeled_datasets.create_dataset", _pld_create, "prelabeled-register"),
    ("data_management.list_folders", _dm_list_folders, "data-list-folders"),
    ("data_management.create_folder", _dm_create_folder, "data-create-folder"),
    ("data_management.get_upload_url", _dm_upload_url, "data-upload-url"),
    ("data_management.get_batch_upload_urls", _dm_batch_upload, "data-batch-upload-urls"),
]
SINK_IDS = [sink[0] for sink in SINKS]


@pytest.mark.parametrize("name, invoke, purpose", SINKS, ids=SINK_IDS)
def test_foreign_bucket_is_refused_before_any_io(h, name, invoke, purpose):
    """Req 2.1–2.8, 2.10, 5.1: a bucket outside the set -> 400
    bucket_not_allowed naming the sink's purpose; no STS, no S3, no write."""
    response = invoke(h, FOREIGN)

    assert response["statusCode"] == 400, response["body"]
    body = body_of(response)
    assert body["error_code"] == "bucket_not_allowed"
    assert body["bucket"] == FOREIGN
    assert body["usecase_id"] == USECASE_ID
    assert body["purpose"] == purpose
    assert body["remedy"] == su.REMEDY_NOT_ALLOWED
    h.assert_no_io()
    assert len(h.refusal_audits()) == 1


@pytest.mark.parametrize("name, invoke, purpose", SINKS, ids=SINK_IDS)
@pytest.mark.parametrize("owned", [ARTIFACTS, COMPONENT], ids=["artifacts", "component"])
@pytest.mark.parametrize("named_on_record", [False, True], ids=["not-on-record", "as-s3_bucket"])
def test_portal_owned_bucket_is_refused_by_every_sink(h, name, invoke, purpose, owned,
                                                      named_on_record):
    """Req 1.2, 2.10, 5.2: the portal's own buckets are refused everywhere,
    with REMEDY_PORTAL_BUCKET, even when the record names one as s3_bucket."""
    if named_on_record:
        h.set_usecase(usecase_record(s3_bucket=owned))

    response = invoke(h, owned)

    assert response["statusCode"] == 400, response["body"]
    body = body_of(response)
    assert body["error_code"] == "bucket_not_allowed"
    assert body["bucket"] == owned
    assert body["purpose"] == purpose
    assert body["remedy"] == su.REMEDY_PORTAL_BUCKET
    h.assert_no_io()


@pytest.mark.parametrize("name, invoke, purpose", SINKS, ids=SINK_IDS)
def test_malformed_bucket_is_refused_without_io(h, name, invoke, purpose):
    """Req 1.1, 5.1: a URI / name the grammar rejects -> 400 with
    invalid_s3_uri (URI sinks) or invalid_bucket_name (bare-name sinks)."""
    response = invoke(h, "bad@host:9")

    assert response["statusCode"] == 400, response["body"]
    assert body_of(response)["error_code"] in ("invalid_s3_uri", "invalid_bucket_name")
    h.assert_no_io()


# ------------------------------------------------- allowed members proceed

def test_validate_model_artifact_uses_data_account_role_for_data_bucket(h):
    """Req 2.3 (consequence pinned): on a separate-Data-Account use case an
    artifact in data_s3_bucket is read with the Data Account role, one in
    s3_bucket with the cross-account role; the download reaches S3 with
    the parsed bucket."""
    mi = h.m.model_import
    usecase = separate_data_account_record()

    with pytest.raises(mi.ModelValidationError):
        mi.validate_model_artifact(f"s3://{DATA_BUCKET}/models/m.tar.gz", usecase,
                                   purpose="model-validate")
    (args, _), = h.rec.assume.calls
    assert args[0] == DATA_ROLE and args[1] == "ext-data"
    assert args[2].startswith("model-validate-")
    assert h.s3.calls == [("download_file", {"Bucket": DATA_BUCKET, "Key": "models/m.tar.gz"})]

    h.rec.assume.calls.clear()
    h.s3.calls.clear()
    with pytest.raises(mi.ModelValidationError):
        mi.validate_model_artifact(f"s3://{UC_BUCKET}/models/m.tar.gz", usecase,
                                   purpose="model-import")
    (args, _), = h.rec.assume.calls
    assert args[0] == CROSS_ACCOUNT_ROLE and args[1] == "ext-1"
    assert h.s3.calls == [("download_file", {"Bucket": UC_BUCKET, "Key": "models/m.tar.gz"})]


def test_validate_model_proceeds_for_an_allowed_bucket_with_the_datascientist_gate(h):
    """Req 3.1: the gate is check_user_access(..., 'DataScientist') with no
    user_info; an allowed artifact reaches the download through the helper."""
    response = _validate_model(h, UC_BUCKET)

    assert response["statusCode"] == 400           # the 404 download -> ModelValidationError
    assert body_of(response)["valid"] is False
    assert "not found" in body_of(response)["error"]
    (args, kwargs), = h.access["model_import"].calls
    assert args == (USER_ID, USECASE_ID, "DataScientist") and kwargs == {}
    assert [c[0][0] for c in h.rec.assume.calls] == [CROSS_ACCOUNT_ROLE]
    assert h.s3.names() == ["download_file"]


@pytest.mark.parametrize("record, bucket, expected_role", [
    (single_account_record(), UC_BUCKET, ROOT_ARN),
    (separate_data_account_record(), DATA_BUCKET, DATA_ROLE),
    (separate_data_account_record(), UC_BUCKET, CROSS_ACCOUNT_ROLE),
    (separate_data_account_record(), EXTRA_BUCKET, CROSS_ACCOUNT_ROLE),
], ids=["single-account", "data-bucket", "s3_bucket-separate-data-account",
        "additional-separate-data-account"])
def test_data_management_uses_helper_role_choice(h, record, bucket, expected_role):
    """Req 2.6 (consequence pinned): the data_management client follows the
    bucket — execution role for single-account, Data Account role for
    data_s3_bucket, cross-account role for s3_bucket and additional entries
    — and get_data_account_credentials is no longer consulted."""
    h.set_usecase(record)

    for invoke in (_dm_list_folders, _dm_create_folder, _dm_upload_url, _dm_batch_upload):
        h.rec.assume.calls.clear()
        h.s3.calls.clear()
        response = invoke(h, bucket)
        assert response["statusCode"] in (200, 201), response["body"]
        (args, _), = h.rec.assume.calls
        assert args[0] == expected_role
        assert all(call[1].get("Bucket") == bucket for call in h.s3.calls)
    assert h.rec.data_credentials.calls == []
    assert h.rec.cors.calls and all(call[0][1] == bucket for call in h.rec.cors.calls)


def test_create_training_job_binds_the_manifest_for_every_model_source(h):
    """Req 2.1: a non-marketplace body still binds dataset_manifest_s3 —
    400 with empty recorders; the marketplace path proceeds for an allowed
    manifest and surfaces the validation warnings."""
    response = _training_create(h, FOREIGN, model_source="custom")
    assert response["statusCode"] == 400
    assert body_of(response)["error_code"] == "bucket_not_allowed"
    h.assert_no_io()

    sagemaker = Recorder(result={"TrainingJobArn": "arn:aws:sagemaker:us-east-1:acct:training-job/x"})
    h.monkeypatch.setattr(h.m.training, "get_usecase_client",
                          lambda *a, **k: SimpleNamespace(create_training_job=sagemaker))
    response = _training_create(h, UC_BUCKET)
    assert response["statusCode"] == 201, response["body"]
    assert body_of(response)["warnings"] == []
    assert [c[0][0] for c in h.rec.assume.calls] == [CROSS_ACCOUNT_ROLE]
    assert len(sagemaker.calls) == 1
    assert len(h.dynamo.table.items) == 1


def test_manifest_reference_outside_the_set_is_skipped_with_a_warning(h):
    """Req 6.1–6.3: a source-ref in a foreign bucket is not dereferenced —
    no helper call, no head_object, no audit row — and the validation stays
    valid with a warning naming the bucket, never the key."""
    foreign_line = json.dumps({
        "source-ref": f"s3://{FOREIGN}/secret/img.jpg",
        "anomaly-label": 1,
        "anomaly-label-metadata": {"class-name": "anomaly", "confidence": 1},
        "anomaly-mask-ref": "not a uri",
        "anomaly-mask-ref-metadata": {},
    })
    h.s3.objects[(UC_BUCKET, "manifests/train.manifest")] = (foreign_line + "\n").encode()

    result = h.m.training.validate_marketplace_manifest(
        f"s3://{UC_BUCKET}/manifests/train.manifest", usecase_record(), "segmentation")

    assert result["valid"] is True, result
    assert any(FOREIGN in w and "secret/img.jpg" not in w for w in result["warnings"])
    assert any("not a valid S3 URI" in w for w in result["warnings"])
    assert [c[0][0] for c in h.rec.assume.calls] == [CROSS_ACCOUNT_ROLE]   # manifest only
    assert h.s3.names() == ["get_object"]
    assert h.refusal_audits() == []

    # The detection validator skips the same way.
    detection_line = json.dumps({
        "source-ref": f"s3://{FOREIGN}/secret/img.jpg",
        "bounding-box": {"image_size": [{"width": 10, "height": 10, "depth": 3}],
                         "annotations": [{"class_id": 0, "left": 1, "top": 1,
                                          "width": 2, "height": 2}]},
        "bounding-box-metadata": {"class-map": {"0": "scratch"}, "type": "groundtruth/object-detection"},
    })
    h.s3.objects[(UC_BUCKET, "manifests/train.manifest")] = (detection_line + "\n").encode()
    h.rec.assume.calls.clear()
    h.s3.calls.clear()
    result = h.m.training.validate_detection_manifest(
        f"s3://{UC_BUCKET}/manifests/train.manifest", usecase_record())
    assert result["valid"] is True, result
    assert any(FOREIGN in w for w in result["warnings"])
    assert h.s3.names() == ["get_object"]


def test_transform_manifest_output_binding(h):
    """Req 2.2: default output (source bucket) and an explicit allowed
    output work — the output client comes from the helper for the output
    bucket — while an explicit foreign output is refused before any I/O."""
    tm = h.m.training
    h.monkeypatch.setattr(tm, "transform_manifest_lines", lambda lines, task_type: {
        "transformed_lines": ['{"source-ref": "x"}'], "errors": [],
        "stats": {"transformed": 1, "skipped": 0}, "detected_attrs": {}})

    response = _transform(h, UC_BUCKET)
    assert response["statusCode"] == 200, response["body"]
    body = body_of(response)
    assert body["transformed_manifest_uri"] == f"s3://{UC_BUCKET}/manifests/train-dda.manifest"
    assert body["warnings"] == []
    assert [c[0][0] for c in h.rec.assume.calls] == [CROSS_ACCOUNT_ROLE]
    put, = [c for c in h.s3.calls if c[0] == "put_object"]
    assert put[1]["Bucket"] == UC_BUCKET

    h.rec.assume.calls.clear()
    h.s3.calls.clear()
    response = _transform(h, UC_BUCKET, output_bucket=EXTRA_BUCKET)
    assert response["statusCode"] == 200, response["body"]
    sessions = [c[0][2] for c in h.rec.assume.calls]
    assert len(sessions) == 2 and sessions[1] == "transform-output"
    put, = [c for c in h.s3.calls if c[0] == "put_object"]
    assert put[1]["Bucket"] == EXTRA_BUCKET

    h.reset()
    response = _transform(h, UC_BUCKET, output_bucket=FOREIGN)
    assert response["statusCode"] == 400
    assert body_of(response)["purpose"] == "transform-output"
    h.assert_no_io()


def test_transform_manifest_skips_foreign_references_with_a_warning(h):
    """Req 6.1: a manifest entry referencing a foreign bucket is not
    dereferenced by transform_manifest; the 200 carries the warning."""
    foreign_line = json.dumps({"source-ref": f"s3://{FOREIGN}/secret/img.jpg",
                               "label": 1, "label-metadata": {"class-name": "anomaly"}})
    h.s3.objects[(UC_BUCKET, "manifests/train.manifest")] = (foreign_line + "\n").encode()
    h.monkeypatch.setattr(h.m.training, "transform_manifest_lines", lambda lines, task_type: {
        "transformed_lines": ['{"source-ref": "x"}'], "errors": [],
        "stats": {"transformed": 1, "skipped": 0}, "detected_attrs": {}})

    response = _transform(h, UC_BUCKET)

    assert response["statusCode"] == 200, response["body"]
    warnings = body_of(response)["warnings"]
    assert any(FOREIGN in w and "secret/img.jpg" not in w for w in warnings)
    assert "head_object" not in h.s3.names()
    assert [c[0][0] for c in h.rec.assume.calls] == [CROSS_ACCOUNT_ROLE]
    assert h.refusal_audits() == []


def test_evaluate_fit_check_reports_unverified_for_a_refused_artifact(h):
    """Req 2.4: a persisted artifact outside the set is never HEADed; the
    fit check degrades to 'unverified' exactly as for any other failure."""
    result = h.m.model_import.evaluate_fit_check({
        "usecase_id": USECASE_ID,
        "model_source": {"s3_model_artifact": f"s3://{FOREIGN}/models/m.tar.gz"},
        "engine_configuration": {},
    })

    assert result["status"] == "unverified"
    assert h.rec.usecase_client.calls == []
    assert h.rec.assume.calls == []
    assert len(h.refusal_audits()) == 1


def test_register_vllm_model_heads_an_allowed_artifact_only_after_binding(h):
    """Req 2.4: an allowed artifact is HEADed through the use-case client;
    the foreign case is covered by the sink table."""
    h.s3.objects[(UC_BUCKET, "models/m.tar.gz")] = b"tar"
    response = _register_vllm(h, UC_BUCKET)

    assert response["statusCode"] == 201, response["body"]
    assert len(h.rec.usecase_client.calls) >= 1
    assert ("head_object", {"Bucket": UC_BUCKET, "Key": "models/m.tar.gz"}) in h.s3.calls


# ----------------------------------------------------- model_converter

def test_trusted_model_source_is_an_exact_name_rule(h):
    """Req 4.5: the configured buckets and TRUSTED_MODEL_BUCKETS admit by
    exact name; the former account-substring rule admits nothing; a URI the
    shared parser rejects is never trusted."""
    mc = h.m.model_converter
    usecase = usecase_record()

    assert mc.is_trusted_model_source(f"s3://{UC_BUCKET}/m.pt", usecase) is True
    assert mc.is_trusted_model_source(f"s3://{EXTRA_BUCKET}/m.pt", usecase) is True
    assert mc.is_trusted_model_source(f"s3://{FOREIGN}/m.pt", usecase) is False
    assert mc.is_trusted_model_source(f"s3://{ARTIFACTS}/m.pt", usecase) is False
    assert mc.is_trusted_model_source(f"s3://{UC_BUCKET}-2/m.pt", usecase) is False
    assert mc.is_trusted_model_source(f"s3://{UC_BUCKET}", usecase) is False      # no key
    assert mc.is_trusted_model_source(f"s3://{UC_BUCKET}@evil/m.pt", usecase) is False
    assert mc.is_trusted_model_source(f"s3://{UC_BUCKET}/m.pt", None) is False

    h.monkeypatch.setenv("TRUSTED_MODEL_BUCKETS", "vendor-models")
    assert mc.is_trusted_model_source("s3://vendor-models/m.pt", usecase) is True
    assert mc.is_trusted_model_source("s3://vendor-models-2/m.pt", usecase) is False

    h.monkeypatch.setenv("TRUSTED_MODEL_ACCOUNTS", "acct")
    assert mc.is_trusted_model_source("s3://models-acct-west/m.pt", usecase) is False
    assert not hasattr(mc, "_config_trusted_model_accounts")


def test_converter_refuses_before_sts_and_inspect_requires_datascientist(h):
    """Req 2.8, 3.2: both converter handlers judge the source before
    assume_usecase_role (no STS on a refusal, the §1.2 body with
    bucket_not_allowed) and inspect's gate is DataScientist."""
    response = _inspect(h, FOREIGN)
    assert response["statusCode"] == 400
    assert body_of(response)["error_code"] == "bucket_not_allowed"
    (args, _), = h.access["model_converter"].calls
    assert args == (USER_ID, USECASE_ID, "DataScientist")
    assert h.rec.converter_assume.calls == []

    h.access["model_converter"].calls.clear()
    response = _convert(h, FOREIGN)
    assert response["statusCode"] == 400
    assert body_of(response)["error_code"] == "bucket_not_allowed"
    assert h.rec.converter_assume.calls == []

    h.access["model_converter"].result = False
    assert _inspect(h, UC_BUCKET)["statusCode"] == 403
    assert h.rec.converter_assume.calls == []


# -------------------------------------------------- data_management extras

def test_list_buckets_marks_allowed_and_portal_created(h):
    """Design §5.6: list_buckets entries carry allowed / portal_created and
    the message names the TagResource form and the Allow action."""
    dm = h.m.data_management
    resources = [
        {"ResourceARN": f"arn:aws:s3:::{EXTRA_BUCKET}",
         "Tags": [{"Key": "dda-portal:managed", "Value": "true"},
                  {"Key": "dda-portal:created-by", "Value": "portal"}]},
        {"ResourceARN": f"arn:aws:s3:::{FOREIGN}",
         "Tags": [{"Key": "dda-portal:managed", "Value": "true"}]},
    ]
    paginator = SimpleNamespace(paginate=lambda **kw: [{"ResourceTagMappingList": resources}])
    tagging = SimpleNamespace(get_paginator=lambda name: paginator)
    s3 = SimpleNamespace(get_bucket_location=lambda Bucket: {"LocationConstraint": None})
    h.monkeypatch.setattr(dm.boto3, "client",
                          lambda service, **kw: tagging if service == "resourcegroupstaggingapi" else s3)

    response = dm.list_buckets(_dm_event(method="GET"))

    assert response["statusCode"] == 200, response["body"]
    body = body_of(response)
    by_name = {b["name"]: b for b in body["buckets"]}
    assert by_name[UC_BUCKET]["is_configured"] is True
    assert by_name[UC_BUCKET]["allowed"] is True
    assert by_name[UC_BUCKET]["portal_created"] is False
    assert by_name[EXTRA_BUCKET]["allowed"] is True
    assert by_name[EXTRA_BUCKET]["portal_created"] is True
    assert by_name[FOREIGN]["allowed"] is False
    assert by_name[FOREIGN]["portal_created"] is False
    assert "aws s3control tag-resource --account-id" in body["message"]
    assert "Allow" in body["message"]


def test_create_bucket_instructions_name_the_tag_resource_form(h):
    """Design §5.6 (finding 12): the disabled handler's instructions name
    the S3 Control TagResource form, with put-bucket-tagging as the
    alternative for buckets without ABAC."""
    response = h.m.data_management.create_bucket(_dm_event({}))

    assert response["statusCode"] == 400
    instructions = " ".join(body_of(response)["instructions"])
    assert ("aws s3control tag-resource --account-id ACCOUNT_ID --resource-arn "
            "arn:aws:s3:::BUCKET --tags Key=dda-portal:managed,Value=true") in instructions
    assert "aws s3api put-bucket-tagging" in instructions


# -------------------------------------------------------- pre-labeled mask

def test_pre_labeled_mask_probe_follows_the_set(h):
    """Req 2.5, 6.1: the mask probe reuses the manifest client for the same
    bucket, obtains the helper client for another configured bucket, and
    skips a foreign bucket with a warning and no helper call."""
    pld = h.m.pre_labeled_datasets
    usecase = usecase_record()

    def manifest(mask_bucket):
        # The mask attribute comes first: the probe takes the first '-ref'
        # attribute of the entry as the mask reference.
        return (json.dumps({"cookie-segmentation-ref": f"s3://{mask_bucket}/masks/a.png",
                            "cookie-segmentation-ref-metadata": {},
                            "source-ref": f"s3://{UC_BUCKET}/images/a.jpg"}) + "\n").encode()

    manifest_s3 = FakeS3({(UC_BUCKET, "m.manifest"): manifest(EXTRA_BUCKET)})
    result = pld.validate_manifest_file(UC_BUCKET, "m.manifest", manifest_s3, usecase=usecase)
    assert result["stats"]["task_type"] == "segmentation"
    assert [c[0][0] for c in h.rec.assume.calls] == [CROSS_ACCOUNT_ROLE]
    assert [c[0][2] for c in h.rec.assume.calls] == ["prelabeled-mask-ref"]
    assert ("head_object", {"Bucket": EXTRA_BUCKET, "Key": "masks/a.png"}) in h.s3.calls
    assert not any("not configured" in w for w in result["warnings"])

    h.rec.assume.calls.clear()
    manifest_s3 = FakeS3({(UC_BUCKET, "m.manifest"): manifest(FOREIGN)})
    result = pld.validate_manifest_file(UC_BUCKET, "m.manifest", manifest_s3, usecase=usecase)
    assert any(FOREIGN in w and "masks/a.png" not in w for w in result["warnings"])
    assert h.rec.assume.calls == []
    assert h.refusal_audits() == []

    with pytest.raises(ValueError):
        pld.validate_manifest_file(UC_BUCKET, "m.manifest", None, usecase=usecase)


# ------------------------------------------------------------- labeling

def _labeling_user():
    return {"user_id": USER_ID, "email": "user-1@example.com",
            "username": USER_ID, "role": "DataScientist"}


def test_create_dda_job_requires_create_labeling_jobs_through_has_permission(h):
    """Req 4.6: the gate is has_permission(user_id, usecase_id,
    CREATE_LABELING_JOBS, user) right after usecase_id is read — 403 with an
    unauthorized_access audit row, no table read or write, no STS."""
    dl = h.m.dda_labeling
    gate = Recorder(result=False)
    jobs_table = FakeTable()
    audit = Recorder()
    h.monkeypatch.setattr(dl.rbac_manager, "has_permission", gate)
    h.monkeypatch.setattr(dl, "labeling_jobs_table", jobs_table)
    h.monkeypatch.setattr(dl, "log_audit_event", audit)

    response = dl.create_dda_job({"usecase_id": USECASE_ID, "job_name": "job-a",
                                  "task_type": "Classification"}, _labeling_user())

    assert response["statusCode"] == 403
    assert body_of(response) == {"error": "Insufficient permissions"}
    (args, _), = gate.calls
    assert args == (USER_ID, USECASE_ID, dl.Permission.CREATE_LABELING_JOBS, _labeling_user())
    (audit_args, _), = audit.calls
    assert audit_args[1] == "unauthorized_access" and audit_args[3] == "job-a"
    assert audit_args[5]["required_permission"] == "create_labeling_jobs"
    assert jobs_table.queries == [] and jobs_table.items == []
    h.assert_no_io()


def test_create_dda_job_refuses_bare_key_and_foreign_example_refs(h):
    """Req 2.7: every example reference must be an in-set s3:// URI — a
    bare key -> invalid_s3_uri, a foreign or portal-owned bucket ->
    bucket_not_allowed — collected into the 400 validation_errors, before
    any enumeration or write."""
    dl = h.m.dda_labeling
    h.monkeypatch.setattr(dl.rbac_manager, "has_permission", Recorder(result=True))
    h.monkeypatch.setattr(dl, "_usecase_job_names", lambda _uc: set())
    h.monkeypatch.setattr(dl, "_get_team_meta", lambda _t: {"usecase_id": USECASE_ID,
                                                             "team_name": "t"})
    h.monkeypatch.setattr(dl, "_team_data_labeler_members", lambda _t, _u: [{"user_id": "l"}])
    jobs_table = FakeTable()
    h.monkeypatch.setattr(dl, "labeling_jobs_table", jobs_table)
    h.monkeypatch.setattr(dl, "log_audit_event", Recorder())

    response = dl.create_dda_job({
        "usecase_id": USECASE_ID, "job_name": "job-b", "task_type": "Classification",
        "team_id": "team-1", "dataset_prefix": "datasets/a/",
        "example_images": {"good": ["ex/bare.jpg", f"s3://{FOREIGN}/ex/foreign.jpg",
                                    f"s3://{UC_BUCKET}/ex/fine.jpg"],
                           "bad": [f"s3://{ARTIFACTS}/ex/portal.png"]},
    }, _labeling_user())

    assert response["statusCode"] == 400, response["body"]
    errors = {e["example_ref"]: e.get("error_code")
              for e in body_of(response)["validation_errors"]
              if e["parameter"] == "example_images"}
    assert errors == {
        "ex/bare.jpg": "invalid_s3_uri",
        f"s3://{FOREIGN}/ex/foreign.jpg": "bucket_not_allowed",
        f"s3://{ARTIFACTS}/ex/portal.png": "bucket_not_allowed",
    }
    assert jobs_table.items == []
    h.assert_no_io()

    validated, errors = dl._validate_example_refs(
        {"good": [f"s3://{UC_BUCKET}/ex/a.jpg", f"s3://{EXTRA_BUCKET}/ex/b.png"], "bad": []},
        usecase_record())
    assert errors == []
    assert validated["good"] == [f"s3://{UC_BUCKET}/ex/a.jpg", f"s3://{EXTRA_BUCKET}/ex/b.png"]


def _rerun_job(refs):
    return {
        "job_id": "labeling-rerun01", "usecase_id": USECASE_ID, "labeling_backend": "DDA",
        "status": "InProgress",
        "auto_label": {"enabled": True, "model": "llm:us.amazon.nova-pro-v1:0",
                       "few_shot": {"enabled": True,
                                    "examples": [{"ref": ref, "designation": "good",
                                                  "position": i} for i, ref in enumerate(refs)]}},
    }


def test_rerun_prelabels_refuses_stored_refs_outside_the_set(h):
    """Req 2.7: a job whose stored few-shot references are not in-set
    s3:// URIs is refused with bucket_not_allowed listing bucket names (or
    the non-URI marker), never a key, before the worker invoke; an in-set
    job is re-run."""
    dl = h.m.dda_labeling
    h.monkeypatch.setattr(dl.rbac_manager, "has_permission", Recorder(result=True))
    h.monkeypatch.setattr(dl, "_count_failed_prelabel_tasks", lambda _job_id: 2)
    h.monkeypatch.setattr(dl, "log_audit_event", Recorder())
    worker = Recorder()
    h.monkeypatch.setattr(dl, "_invoke_labeling_worker", worker)
    jobs = {}
    table = SimpleNamespace(get_item=lambda Key: {"Item": jobs[Key["job_id"]]},
                            update_item=Recorder())
    h.monkeypatch.setattr(dl, "labeling_jobs_table", table)
    event = api_event(body=None, path_params={"id": "labeling-rerun01"})

    jobs["labeling-rerun01"] = _rerun_job(["ex/bare-key.jpg", f"s3://{FOREIGN}/ex/secret.jpg",
                                           f"s3://{UC_BUCKET}/ex/fine.jpg", "s3://"])
    response = dl.rerun_prelabels(event, None)
    assert response["statusCode"] == 400, response["body"]
    body = body_of(response)
    assert body["error_code"] == "bucket_not_allowed"
    assert body["refs"] == [dl.NOT_AN_S3_URI_REF, FOREIGN, dl.NOT_AN_S3_URI_REF]
    assert "secret.jpg" not in response["body"] and "bare-key" not in response["body"]
    assert worker.calls == []
    assert table.update_item.calls == []

    jobs["labeling-rerun01"] = _rerun_job([f"s3://{UC_BUCKET}/ex/fine.jpg",
                                           f"s3://{EXTRA_BUCKET}/ex/also.png"])
    response = dl.rerun_prelabels(event, None)
    assert response["statusCode"] == 202, response["body"]
    assert len(worker.calls) == 1


def test_example_image_urls_skip_without_presigning(h):
    """Req 2.7: a bare key, a malformed URI and a foreign or portal-owned
    bucket are skipped without a presign; in-set references are presigned."""
    dl = h.m.dda_labeling
    presign = FakeS3()
    h.monkeypatch.setattr(dl, "s3_client", presign)
    job = {"job_id": "labeling-urls0001", "usecase_id": USECASE_ID,
           "example_images": {"good": ["ex/bare.jpg", f"s3://{FOREIGN}/ex/a.jpg",
                                       f"s3://{ARTIFACTS}/ex/b.jpg", "s3://",
                                       f"s3://{UC_BUCKET}/ex/c.jpg"],
                              "bad": [f"s3://{EXTRA_BUCKET}/ex/d.png"]}}

    urls = dl._example_image_urls(job, usecase_record())

    assert len(urls["good"]) == 1 and len(urls["bad"]) == 1
    assert [(c[1]["Bucket"], c[1]["Key"]) for c in presign.calls] == [
        (UC_BUCKET, "ex/c.jpg"), (EXTRA_BUCKET, "ex/d.png")]
    assert dl.PORTAL_ARTIFACTS_BUCKET not in json.dumps(urls)


def test_worker_resolves_a_bare_key_to_nothing(h):
    """Req 2.7: _few_shot_ref_location -> (None, None) for a bare key, so
    _resolve_few_shot_images raises the unresolvable-reference
    GenerationFailure without obtaining a client."""
    worker = h.m.dda_autolabel_worker
    factory = Recorder(result=h.s3)
    h.monkeypatch.setattr(worker, "get_s3_client_for_bucket", factory)

    assert worker._few_shot_ref_location("labeling-examples/x/good/0.png") == (None, None)
    assert worker._few_shot_ref_location(f"s3://{UC_BUCKET}/ex/a.png") == (UC_BUCKET, "ex/a.png")

    job = {"job_id": "labeling-bare0001", "usecase_id": USECASE_ID,
           "auto_label": {"enabled": True, "model": "llm:us.amazon.nova-pro-v1:0",
                          "few_shot": {"enabled": True,
                                       "examples": [{"ref": "labeling-examples/x/good/0.png",
                                                     "designation": "good", "position": 0}]}}}
    with pytest.raises(worker.GenerationFailure) as excinfo:
        worker._resolve_few_shot_images(job, "us.amazon.nova-pro-v1:0")
    assert str(excinfo.value).endswith("could not be resolved to an S3 object")
    assert factory.calls == []
    assert h.s3.calls == []
