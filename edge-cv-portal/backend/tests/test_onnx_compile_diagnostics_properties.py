"""
Preservation property suite (Property 2) for onnx-compile-error-diagnostics
(task 2). The fix-checking properties (Properties 1, 3, 4, 5) are added to
this file by task 4.

**Property 2: Preservation — the Neo path and every non-bug input are
behaviorally identical before and after the fix.**

Observation-first methodology: every assertion below encodes behavior
OBSERVED on the UNFIXED tree that the fix is required to preserve. These
tests MUST PASS on the unfixed code (they record the baseline) and MUST
KEEP PASSING after tasks 3.x land.

Observations recorded on the unfixed tree while writing this suite:

- `start_compilation_job` submits `create_compilation_job` for the six Neo
  targets with the exact kwargs frozen in `test_neo_submission_identity`
  (job-name derivation + 63-char truncation, InputConfig PYTORCH/1.8,
  OutputConfig at s3://<bucket>/models/compilation/<job>/<target>, 3600 s
  StoppingCondition).
- Poller A stores the raw uppercase `CompilationJobStatus` verbatim,
  captures `compiled_model_s3` on COMPLETED and
  `failure_reason = response.get('FailureReason', 'Unknown')` on FAILED.
- `COMPILATION_TARGETS` has exactly seven entries, JP5/JP6 share the
  cuda-ver 11.4 / trt-ver 8.5.2 / gpu-code sm_72 triple, and there is NO
  `jetson-xavier-jp7` key.
- IMPORTANT scope note on 3.19: on the unfixed tree poller A
  (`get_compilation_status`) describes EVERY entry, terminal or not (that
  is exactly Defect 1's trigger), and the fix does not add a terminal
  filter to poller A. Requirement 3.19 scopes the zero-describe guarantee
  to the model detail page, i.e. poller B (`models.get_model`), whose
  TERMINAL = {COMPLETED, FAILED, STOPPED} filter already skips fully
  terminal records today. The terminal-record identity below therefore
  asserts zero describes from poller B only — that is the behavior that
  is genuinely preserved.
- Scope note on the 3.12 "403 when the caller lacks use-case access"
  clause: on the unfixed tree `rbac_manager.get_user_role` defaults to
  Viewer when no role row exists, so `check_user_access` with no required
  role always grants read access and GET /compile returns 200 even for a
  user with no role assignment — that 403 branch is unreachable on the
  GET path. The reachable access denial (POST without DataScientist →
  403) and the reachable GET-path outcomes (404 / 200) are what this
  suite freezes.
- The `start_compilation` audit event carries
  details = {targets, compilation_jobs: [job names], auto_triggered}; the
  job-name list is only asserted for SUCCESSFUL starts because the failed
  ONNX entry's fabricated sentinel name is bug behavior that the fix
  removes (covered by the exploration suite, not here).
- Both writers use the frozen DynamoDB update expressions asserted below;
  `compilation_events.py` writes ONLY `compilation_jobs` + `updated_at`.

Suite conventions: same FakeSageMakerService / module-loading patterns as
test_onnx_compile_diagnostics_exploration.py, but with its OWN table and
bucket so the two suites are independent. Hypothesis profiles come from
conftest (`portal-fast` / `ci`) — no hardcoded max_examples.
"""
import importlib.util
import io
import json
import os
import re
import string
import sys
import tarfile
import uuid
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import REGION

TRAINING_JOBS_TABLE_NAME = "test-training-jobs-onnx-props"
USECASE_BUCKET = "test-onnx-props-bucket"
# Hyphen-safe so safe_model_name == MODEL_NAME in the deterministic tests.
MODEL_NAME = "propsmodel"

_FUNCTIONS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "functions")

# ---------------------------------------------------------------------------
# Frozen baselines observed on the UNFIXED tree
# ---------------------------------------------------------------------------

# COMPILATION_TARGETS identity (3.3): all seven entries, byte-identical
# compiler options (dict-literal key order matches the source so the
# json.dumps strings compare equal), and NO jetson-xavier-jp7 key.
FROZEN_COMPILATION_TARGETS = {
    'jetson-xavier': {
        'os': 'LINUX', 'arch': 'ARM64', 'accelerator': 'NVIDIA',
        'compiler_options': json.dumps({
            'cuda-ver': '10.2', 'gpu-code': 'sm_72', 'trt-ver': '8.2.1',
            'max-workspace-size': '2147483648', 'precision-mode': 'fp16',
            'jetson-platform': 'xavier'}),
    },
    'jetson-xavier-jp5': {
        'os': 'LINUX', 'arch': 'ARM64', 'accelerator': 'NVIDIA',
        'compiler_options': json.dumps({
            'cuda-ver': '11.4', 'gpu-code': 'sm_72', 'trt-ver': '8.5.2',
            'max-workspace-size': '2147483648', 'precision-mode': 'fp16',
            'jetson-platform': 'xavier'}),
    },
    'jetson-xavier-jp6': {
        'os': 'LINUX', 'arch': 'ARM64', 'accelerator': 'NVIDIA',
        'compiler_options': json.dumps({
            'cuda-ver': '11.4', 'gpu-code': 'sm_72', 'trt-ver': '8.5.2',
            'max-workspace-size': '2147483648', 'precision-mode': 'fp16',
            'jetson-platform': 'xavier'}),
    },
    'x86_64-cpu': {
        'os': 'LINUX', 'arch': 'X86_64', 'accelerator': None,
        'compiler_options': None,
    },
    'x86_64-cuda': {
        'os': 'LINUX', 'arch': 'X86_64', 'accelerator': 'NVIDIA',
        'compiler_options': json.dumps({
            'cuda-ver': '10.2', 'gpu-code': 'sm_75', 'trt-ver': '8.2.1',
            'max-workspace-size': '2147483648', 'precision-mode': 'fp16'}),
    },
    'arm64-cpu': {
        'os': 'LINUX', 'arch': 'ARM64', 'accelerator': None,
        'compiler_options': None,
    },
    'onnx': {
        'os': 'LINUX', 'arch': 'ANY', 'accelerator': None,
        'compiler_options': None, 'export_format': 'onnx',
    },
}

NEO_TARGETS = [k for k in FROZEN_COMPILATION_TARGETS if k != 'onnx']

# Frozen safe-target mapping used in Neo job-name derivation.
FROZEN_TARGET_NAME_MAPPING = {
    'jetson-xavier': 'jetson',
    'jetson-xavier-jp5': 'jetsonjp5',
    'jetson-xavier-jp6': 'jetsonjp6',
    'x86_64-cpu': 'x86cpu',
    'x86_64-cuda': 'x86cuda',
    'arm64-cpu': 'arm64cpu',
}

# Frozen ONNX export submission constants (env overrides are unset here).
FROZEN_ONNX_EXPORT_IMAGE = (
    '763104351884.dkr.ecr.us-east-1.amazonaws.com/'
    'pytorch-training:1.13.1-cpu-py39')
FROZEN_ONNX_OPSET = '17'
FROZEN_INPUT_SHAPE = json.dumps([1, 3, 224, 224])   # from mochi.json fixture

# Frozen request-level update expressions.
UPDATE_EXPR_JOBS = ('SET compilation_jobs = :jobs, '
                    'compilation_status = :cstatus, updated_at = :updated')
UPDATE_EXPR_SKIPPED = 'SET compilation_skipped = :s, updated_at = :u'
UPDATE_EXPR_WRITER_C = 'SET compilation_jobs = :jobs, updated_at = :updated'

# Frozen imported-ONNX bypass message.
ONNX_BYPASS_MESSAGE = ('ONNX model runs on the ONNX Runtime engine — '
                       'compilation is not required. Proceed to packaging.')

# The modeled derivation vocabulary (3.13-3.15). 'ERROR' deliberately
# excluded: its unfixed derivation result is bug behavior the fix changes,
# covered by the exploration suite, NOT preserved here.
MODELED_STATUSES = ['STARTING', 'INPROGRESS', 'IN_PROGRESS', 'COMPLETED',
                    'FAILED', 'STOPPING', 'STOPPED']


def _load_module(filename, alias):
    """Load a functions/ handler under a distinct module name, INSIDE the
    moto mock, so its module-level boto3 clients are intercepted."""
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(_FUNCTIONS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# SageMaker service stub (exploration-suite pattern + kwargs recording)
# ---------------------------------------------------------------------------

class FakeSageMakerService:
    """Behaves like the SageMaker service and records every create call's
    kwargs and every describe call, so submission identity can be asserted
    byte-for-byte."""

    def __init__(self):
        self.create_training_error = None       # ClientError to raise
        self.create_compilation_error = None    # ClientError to raise
        self.training_jobs = {}                 # name -> describe response
        self.compilation_jobs = {}              # name -> describe response
        self.describe_calls = []                # (api, name, 'ok'|'raised')
        self.create_training_calls = []         # recorded kwargs
        self.create_compilation_calls = []      # recorded kwargs

    def calls(self, api=None, name=None):
        return [c for c in self.describe_calls
                if (api is None or c[0] == api)
                and (name is None or c[1] == name)]

    # -- service surface ----------------------------------------------------
    def create_training_job(self, **kwargs):
        self.create_training_calls.append(kwargs)
        if self.create_training_error is not None:
            raise self.create_training_error
        name = kwargs["TrainingJobName"]
        self.training_jobs[name] = {
            "TrainingJobName": name,
            "TrainingJobStatus": "InProgress",
        }
        return {"TrainingJobArn":
                f"arn:aws:sagemaker:{REGION}:123456789012:training-job/{name}"}

    def create_compilation_job(self, **kwargs):
        self.create_compilation_calls.append(kwargs)
        if self.create_compilation_error is not None:
            raise self.create_compilation_error
        name = kwargs["CompilationJobName"]
        self.compilation_jobs[name] = {
            "CompilationJobName": name,
            "CompilationJobStatus": "INPROGRESS",
        }
        return {"CompilationJobArn":
                f"arn:aws:sagemaker:{REGION}:123456789012:compilation-job/{name}"}

    def describe_training_job(self, TrainingJobName):
        if TrainingJobName in self.training_jobs:
            self.describe_calls.append(
                ("describe_training_job", TrainingJobName, "ok"))
            return self.training_jobs[TrainingJobName]
        self.describe_calls.append(
            ("describe_training_job", TrainingJobName, "raised"))
        raise ClientError(
            {"Error": {"Code": "ValidationException",
                       "Message": f"Requested resource not found: Training "
                                  f"job '{TrainingJobName}' does not exist."}},
            "DescribeTrainingJob")

    def describe_compilation_job(self, CompilationJobName):
        if CompilationJobName in self.compilation_jobs:
            self.describe_calls.append(
                ("describe_compilation_job", CompilationJobName, "ok"))
            return self.compilation_jobs[CompilationJobName]
        self.describe_calls.append(
            ("describe_compilation_job", CompilationJobName, "raised"))
        raise ClientError(
            {"Error": {"Code": "ValidationException",
                       "Message": f"Could not find compilation job "
                                  f"'{CompilationJobName}'."}},
            "DescribeCompilationJob")


class _ServiceHolder:
    current = None


def fresh_service():
    _ServiceHolder.current = FakeSageMakerService()
    return _ServiceHolder.current


# ---------------------------------------------------------------------------
# Recording DynamoDB proxy: freezes the update expressions the handlers use
# ---------------------------------------------------------------------------

class _RecordingTable:
    def __init__(self, real, name, record):
        self._real, self._name, self._record = real, name, record

    def update_item(self, **kwargs):
        self._record.append((self._name, kwargs))
        return self._real.update_item(**kwargs)

    def __getattr__(self, attr):
        return getattr(self._real, attr)


class RecordingDynamoResource:
    def __init__(self, real, record):
        self._real, self.update_calls = real, record

    def Table(self, name):
        return _RecordingTable(self._real.Table(name), name, self.update_calls)

    def __getattr__(self, attr):
        return getattr(self._real, attr)


class _FakeLambdaClient:
    """Captures the async packaging invocation writer C chains on COMPLETED."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _upload_model_artifact(s3):
    """A trained-model tarball shaped how extract_and_repackage_model
    expects it: a TorchScript .pt plus mochi.json carrying input_shape."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        pt = b"not-a-real-torchscript-model"
        info = tarfile.TarInfo("mochi.pt")
        info.size = len(pt)
        tar.addfile(info, io.BytesIO(pt))
        mochi = json.dumps(
            {"stages": [{"input_shape": [1, 3, 224, 224]}]}).encode()
        info = tarfile.TarInfo("mochi.json")
        info.size = len(mochi)
        tar.addfile(info, io.BytesIO(mochi))
    s3.put_object(Bucket=USECASE_BUCKET, Key="models/model.tar.gz",
                  Body=buf.getvalue())


@pytest.fixture(scope="module")
def props_env(aws_stack):
    """Own training-jobs table (production key shape) + use-case bucket +
    compilation.py / models.py / compilation_events.py / training_events.py
    loaded inside the mock, with the SageMaker use-case client routed to the
    recording stub and DynamoDB update expressions recorded."""
    import boto3

    os.environ["TRAINING_JOBS_TABLE"] = TRAINING_JOBS_TABLE_NAME
    os.environ["PACKAGING_FUNCTION_NAME"] = "test-packaging-fn"

    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TRAINING_JOBS_TABLE_NAME,
        KeySchema=[{"AttributeName": "training_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "training_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    aws_stack.s3.create_bucket(Bucket=USECASE_BUCKET)
    _upload_model_artifact(aws_stack.s3)

    compilation = _load_module("compilation.py", "portal_compilation_props")
    models = _load_module("models.py", "portal_models_props")
    compilation_events = _load_module(
        "compilation_events.py", "portal_compilation_events_props")
    training_events = _load_module(
        "training_events.py", "portal_training_events_props")

    def _dispatch_get_usecase_client(service_name, usecase,
                                     session_name=None, region=None):
        if service_name == "sagemaker":
            return _ServiceHolder.current
        return boto3.client(service_name, region_name=region or REGION)

    compilation.get_usecase_client = _dispatch_get_usecase_client
    models.get_usecase_client = _dispatch_get_usecase_client

    # Record every table.update_item the handlers issue (3.11, 3.23).
    comp_updates = []
    compilation.dynamodb = RecordingDynamoResource(
        compilation.dynamodb, comp_updates)
    events_updates = []
    compilation_events.dynamodb = RecordingDynamoResource(
        compilation_events.dynamodb, events_updates)

    # Capture writer C's packaging chain: rebinding `boto3` inside the
    # events module's namespace only affects its handler-time
    # boto3.client('lambda') call (dynamodb/sns were bound at import).
    fake_lambda = _FakeLambdaClient()
    compilation_events.boto3 = SimpleNamespace(
        client=lambda name, **kw: fake_lambda)

    usecase_id = f"uc-{uuid.uuid4()}"
    aws_stack.tables.usecases.put_item(Item={
        "usecase_id": usecase_id,
        "name": "ONNX Preservation Use Case",
        "account_id": "123456789012",
        "s3_bucket": USECASE_BUCKET,
    })
    user_id = f"user-{uuid.uuid4()}"
    aws_stack.tables.user_roles.put_item(Item={
        "user_id": user_id,
        "usecase_id": usecase_id,
        "role": "DataScientist",
    })

    resource = boto3.resource("dynamodb", region_name=REGION)
    yield SimpleNamespace(
        compilation=compilation,
        models=models,
        compilation_events=compilation_events,
        training_events=training_events,
        training_jobs=resource.Table(TRAINING_JOBS_TABLE_NAME),
        audit_log=aws_stack.tables.audit_log,
        user_roles=aws_stack.tables.user_roles,
        usecase_id=usecase_id,
        user_id=user_id,
        comp_updates=comp_updates,
        events_updates=events_updates,
        fake_lambda=fake_lambda,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_context(user_id):
    return {"authorizer": {"claims": {
        "sub": user_id,
        "email": f"{user_id}@example.com",
        "cognito:username": user_id,
    }}}


def seed_training_record(env, **overrides):
    training_id = str(uuid.uuid4())
    item = {
        "training_id": training_id,
        "usecase_id": env.usecase_id,
        "model_name": MODEL_NAME,
        "model_type": "classification",
        "source": "trained",
        "status": "Completed",
        "artifact_s3": f"s3://{USECASE_BUCKET}/models/model.tar.gz",
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    item.update(overrides)
    env.training_jobs.put_item(Item=item)
    return training_id


def raw_start(env, training_id, targets, user_id=None):
    """POST /compile, returning the raw response (no status assertion)."""
    return env.compilation.start_compilation_job({
        "httpMethod": "POST",
        "path": f"/api/v1/training/{training_id}/compile",
        "pathParameters": {"id": training_id},
        "body": json.dumps({"targets": targets}),
        "requestContext": _auth_context(user_id or env.user_id),
    }, None)


def start_compile(env, training_id, targets):
    response = raw_start(env, training_id, targets)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def raw_status(env, training_id, user_id=None):
    """GET /compile (poller A), returning the raw response."""
    return env.compilation.get_compilation_status({
        "httpMethod": "GET",
        "path": f"/api/v1/training/{training_id}/compile",
        "pathParameters": {"id": training_id},
        "requestContext": _auth_context(user_id or env.user_id),
    }, None)


def poll_status(env, training_id):
    response = raw_status(env, training_id)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def get_model(env, training_id):
    """One invocation of models.get_model (poller B)."""
    response = env.models.get_model({
        "httpMethod": "GET",
        "path": f"/api/v1/models/{training_id}",
        "pathParameters": {"id": training_id},
        "requestContext": _auth_context(env.user_id),
    }, None)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def stored_record(env, training_id):
    item = env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    return item.get("compilation_jobs", []), item.get("compilation_status")


def make_extra_user(env, role=None):
    """A user with the given role on the suite's use case (or none at all)."""
    user_id = f"user-{uuid.uuid4()}"
    if role is not None:
        env.user_roles.put_item(Item={
            "user_id": user_id,
            "usecase_id": env.usecase_id,
            "role": role,
        })
    return user_id


def expected_neo_job_name(model_name, target, timestamp):
    """Replicates the UNFIXED job-name derivation, including the 63-char
    truncation, byte for byte (observed in start_compilation_job)."""
    safe_target = FROZEN_TARGET_NAME_MAPPING[target]
    safe_model_name = model_name.replace('.', '-').replace('_', '-')
    safe_model_name = '-'.join(filter(None, safe_model_name.split('-')))
    name = f"{safe_model_name}-{safe_target}-{timestamp}"
    name = re.sub(r'[^a-zA-Z0-9-]', '', name)
    name = re.sub(r'-+', '-', name)
    name = name.strip('-')
    if len(name) > 63:
        max_model_name_length = 63 - len(safe_target) - len(timestamp) - 2
        truncated = safe_model_name[:max_model_name_length]
        name = f"{truncated}-{safe_target}-{timestamp}"
    return name


def derivation_oracle(statuses):
    """The recorded unfixed derive_compilation_status result for MODELED
    status sets (docstring rules, observed to match the implementation)."""
    if not statuses:
        return None
    upper = [str(s).upper() for s in statuses]
    if any(s in {'STARTING', 'INPROGRESS', 'IN_PROGRESS'} for s in upper):
        return 'InProgress'
    if all(s == 'COMPLETED' for s in upper):
        return 'Completed'
    return 'Failed'


def updates_for(record, training_id):
    return [kw for (_name, kw) in record
            if kw.get("Key") == {"training_id": training_id}]


# ---------------------------------------------------------------------------
# Property 2 — Neo submission identity
# Validates: Requirements 3.1
# ---------------------------------------------------------------------------

_model_names = st.text(
    alphabet=string.ascii_lowercase + string.digits + "._-",
    min_size=1, max_size=70)


@settings(deadline=None)
@given(target=st.sampled_from(NEO_TARGETS), model_name=_model_names)
def test_neo_submission_identity(props_env, target, model_name):
    """The exact create_compilation_job kwargs for each Neo target are
    frozen: job-name derivation with 63-char truncation, OutputConfig,
    InputConfig (PYTORCH / 1.8), and the 3600 s StoppingCondition.
    # Validates: Requirements 3.1
    """
    service = fresh_service()
    training_id = seed_training_record(props_env, model_name=model_name)
    body = start_compile(props_env, training_id, [target])

    assert len(service.create_compilation_calls) == 1
    call = service.create_compilation_calls[0]

    # Job-name derivation and truncation, replicated byte for byte with the
    # timestamp the handler generated.
    job_name = call["CompilationJobName"]
    ts = job_name[-14:]
    assert ts.isdigit() and len(ts) == 14
    assert job_name == expected_neo_job_name(model_name, target, ts)
    assert len(job_name) <= 63

    cfg = FROZEN_COMPILATION_TARGETS[target]
    expected_output = {
        "S3OutputLocation":
            f"s3://{USECASE_BUCKET}/models/compilation/{job_name}/{target}",
        "TargetPlatform": {"Os": cfg["os"], "Arch": cfg["arch"]},
    }
    if cfg["accelerator"]:
        expected_output["TargetPlatform"]["Accelerator"] = cfg["accelerator"]
    if cfg["compiler_options"]:
        expected_output["CompilerOptions"] = cfg["compiler_options"]

    assert call == {
        "CompilationJobName": job_name,
        "RoleArn":
            "arn:aws:iam::123456789012:role/DDASageMakerExecutionRole",
        "InputConfig": {
            "S3Uri":
                f"s3://{USECASE_BUCKET}/models/model_for_compilation.tar.gz",
            "DataInputConfig": json.dumps({"input_shape": [1, 3, 224, 224]}),
            "Framework": "PYTORCH",
            "FrameworkVersion": "1.8",
        },
        "OutputConfig": expected_output,
        "StoppingCondition": {"MaxRuntimeInSeconds": 3600},
    }

    # The returned entry keeps its exact shape.
    (entry,) = body["compilation_jobs"]
    assert entry == {
        "target": target,
        "compilation_job_name": job_name,
        "compilation_job_arn":
            f"arn:aws:sagemaker:{REGION}:123456789012:compilation-job/{job_name}",
        "status": "InProgress",
    }


# ---------------------------------------------------------------------------
# Property 2 — Neo polling identity
# Validates: Requirements 3.2
# ---------------------------------------------------------------------------

_NEO_POLL_STATUSES = ["STARTING", "INPROGRESS", "COMPLETED", "FAILED",
                      "STOPPING", "STOPPED"]


@settings(deadline=None)
@given(status=st.sampled_from(_NEO_POLL_STATUSES),
       with_reason=st.booleans())
def test_neo_polling_identity(props_env, status, with_reason):
    """Poller A stores the raw uppercase CompilationJobStatus verbatim,
    sets compiled_model_s3 from ModelArtifacts.S3ModelArtifacts on
    COMPLETED, and failure_reason = response.get('FailureReason',
    'Unknown') on FAILED.
    # Validates: Requirements 3.2
    """
    service = fresh_service()
    name = f"neo-poll-{uuid.uuid4().hex[:12]}"
    artifact_uri = f"s3://{USECASE_BUCKET}/compiled/{name}/model.tar.gz"
    reason = f"neo-failure-{uuid.uuid4().hex[:8]}"

    describe = {"CompilationJobName": name, "CompilationJobStatus": status}
    if status == "COMPLETED":
        describe["ModelArtifacts"] = {"S3ModelArtifacts": artifact_uri}
    if status == "FAILED" and with_reason:
        describe["FailureReason"] = reason
    service.compilation_jobs[name] = describe

    training_id = seed_training_record(props_env, compilation_jobs=[{
        "target": "jetson-xavier-jp6",
        "compilation_job_name": name,
        "status": "INPROGRESS",
    }], compilation_status="InProgress")

    poll_status(props_env, training_id)

    # API choice: the Neo entry is described with the Neo API only.
    assert service.calls(api="describe_compilation_job") == [
        ("describe_compilation_job", name, "ok")]
    assert service.calls(api="describe_training_job") == []

    jobs, _ = stored_record(props_env, training_id)
    (entry,) = jobs
    assert entry["status"] == status                       # verbatim uppercase
    if status == "COMPLETED":
        assert entry["compiled_model_s3"] == artifact_uri
    if status == "FAILED":
        assert entry["failure_reason"] == (reason if with_reason
                                           else "Unknown")


# ---------------------------------------------------------------------------
# Property 2 — COMPILATION_TARGETS identity
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

def test_compilation_targets_identity(props_env):
    """All seven entries frozen, including the JP5/JP6 cuda-ver 11.4 /
    trt-ver 8.5.2 / gpu-code sm_72 triples, and NO jetson-xavier-jp7 key.
    # Validates: Requirements 3.3
    """
    targets = props_env.compilation.COMPILATION_TARGETS
    assert targets == FROZEN_COMPILATION_TARGETS
    assert "jetson-xavier-jp7" not in targets
    assert len(targets) == 7
    for jp in ("jetson-xavier-jp5", "jetson-xavier-jp6"):
        options = json.loads(targets[jp]["compiler_options"])
        assert (options["cuda-ver"], options["trt-ver"],
                options["gpu-code"]) == ("11.4", "8.5.2", "sm_72")


# ---------------------------------------------------------------------------
# Property 2 — ONNX submission identity (success path)
# Validates: Requirements 3.5, 3.6
# ---------------------------------------------------------------------------

def test_onnx_submission_identity(props_env):
    """The create_training_job kwargs on the success path are frozen, and
    the returned entry carries export_format 'onnx' / status 'InProgress'.
    # Validates: Requirements 3.5, 3.6
    """
    service = fresh_service()
    training_id = seed_training_record(props_env)
    body = start_compile(props_env, training_id, ["onnx"])

    assert len(service.create_training_calls) == 1
    call = service.create_training_calls[0]

    job_name = call["TrainingJobName"]
    ts = job_name[-14:]
    assert ts.isdigit() and len(ts) == 14
    assert job_name == f"{MODEL_NAME}-onnx-{ts}"
    assert len(job_name) <= 63

    assert call == {
        "TrainingJobName": job_name,
        "RoleArn":
            "arn:aws:iam::123456789012:role/DDASageMakerExecutionRole",
        "AlgorithmSpecification": {
            "TrainingImage": FROZEN_ONNX_EXPORT_IMAGE,
            "TrainingInputMode": "File",
        },
        "HyperParameters": {
            "sagemaker_program": "onnx_export.py",
            "sagemaker_submit_directory":
                f"s3://{USECASE_BUCKET}/models/onnx-export/{job_name}/"
                f"sourcedir.tar.gz",
            "INPUT_SHAPE": FROZEN_INPUT_SHAPE,
            "ONNX_OPSET": FROZEN_ONNX_OPSET,
        },
        "Environment": {
            "INPUT_SHAPE": FROZEN_INPUT_SHAPE,
            "ONNX_OPSET": FROZEN_ONNX_OPSET,
        },
        "InputDataConfig": [{
            "ChannelName": "model",
            "DataSource": {"S3DataSource": {
                "S3DataType": "S3Prefix",
                "S3Uri": f"s3://{USECASE_BUCKET}/models/model.tar.gz",
                "S3DataDistributionType": "FullyReplicated",
            }},
        }],
        "OutputDataConfig": {
            "S3OutputPath":
                f"s3://{USECASE_BUCKET}/models/compilation/{job_name}/onnx",
        },
        "ResourceConfig": {"InstanceType": "ml.m5.large",
                           "InstanceCount": 1, "VolumeSizeInGB": 20},
        "StoppingCondition": {"MaxRuntimeInSeconds": 1800},
    }

    (entry,) = body["compilation_jobs"]
    assert entry == {
        "target": "onnx",
        "compilation_job_name": job_name,
        "compilation_job_arn":
            f"arn:aws:sagemaker:{REGION}:123456789012:training-job/{job_name}",
        "status": "InProgress",
        "export_format": "onnx",
    }
    jobs, overall = stored_record(props_env, training_id)
    assert jobs[0]["export_format"] == "onnx"
    assert jobs[0]["status"] == "InProgress"
    assert overall == "InProgress"


# ---------------------------------------------------------------------------
# Property 2 — live ONNX export polling identity (poller A)
# Validates: Requirements 3.6
# ---------------------------------------------------------------------------

_ONNX_POLL_STATUSES = ["InProgress", "Completed", "Failed", "Stopped"]


@settings(deadline=None)
@given(status=st.sampled_from(_ONNX_POLL_STATUSES),
       with_reason=st.booleans())
def test_onnx_polling_identity(props_env, status, with_reason):
    """A live ONNX export entry is polled with describe_training_job,
    TrainingJobStatus is recorded verbatim, compiled_model_s3 is set on
    Completed, and failure_reason is captured on Failed.
    # Validates: Requirements 3.6
    """
    service = fresh_service()
    name = f"onnx-poll-{uuid.uuid4().hex[:12]}"
    artifact_uri = f"s3://{USECASE_BUCKET}/onnx-out/{name}/model.tar.gz"
    reason = f"onnx-failure-{uuid.uuid4().hex[:8]}"

    describe = {"TrainingJobName": name, "TrainingJobStatus": status}
    if status == "Completed":
        describe["ModelArtifacts"] = {"S3ModelArtifacts": artifact_uri}
    if status == "Failed" and with_reason:
        describe["FailureReason"] = reason
    service.training_jobs[name] = describe

    training_id = seed_training_record(props_env, compilation_jobs=[{
        "target": "onnx",
        "compilation_job_name": name,
        "status": "InProgress",
        "export_format": "onnx",
    }], compilation_status="InProgress")

    poll_status(props_env, training_id)

    # API choice: the ONNX export entry is described with the training API.
    assert service.calls(api="describe_training_job") == [
        ("describe_training_job", name, "ok")]
    assert service.calls(api="describe_compilation_job") == []

    jobs, _ = stored_record(props_env, training_id)
    (entry,) = jobs
    assert entry["status"] == status                       # verbatim
    if status == "Completed":
        assert entry["compiled_model_s3"] == artifact_uri
    if status == "Failed":
        assert entry["failure_reason"] == (reason if with_reason
                                           else "Unknown")


# ---------------------------------------------------------------------------
# Property 2 — derivation identity over MODELED statuses
# Validates: Requirements 3.13, 3.14, 3.15
# ---------------------------------------------------------------------------

def _mixed_case(s):
    return st.sampled_from(sorted({s, s.lower(), s.title(), s.capitalize()}))


_cased_modeled = st.sampled_from(MODELED_STATUSES).flatmap(_mixed_case)


@given(statuses=st.lists(_cased_modeled, max_size=6))
def test_derivation_identity_over_modeled_statuses(props_env, statuses):
    """Over generated mixed-case lists drawn from the MODELED vocabulary
    {STARTING, INPROGRESS, IN_PROGRESS, COMPLETED, FAILED, STOPPING,
    STOPPED}, derive_compilation_status equals the recorded unfixed result
    (any running -> InProgress; all COMPLETED -> Completed; else Failed)
    and [] -> None. 'ERROR' is deliberately NOT generated here: its
    unfixed collapse to Failed is bug behavior (exploration case 6).
    # Validates: Requirements 3.13, 3.14, 3.15
    """
    jobs = [{"target": f"t{i}", "status": s}
            for i, s in enumerate(statuses)]
    assert (props_env.compilation.derive_compilation_status(jobs)
            == derivation_oracle(statuses))
    assert props_env.compilation.derive_compilation_status([]) is None


# ---------------------------------------------------------------------------
# Property 2 — request-level identity
# Validates: Requirements 3.4, 3.9, 3.10, 3.12
# ---------------------------------------------------------------------------

# (code, message, expected status code, expected error string) — the frozen
# ClientError -> response mappings observed in start_compilation_job.
CLIENT_ERROR_MAPPINGS = [
    ("ValidationException",
     "1 validation error detected: Member must have length less than or "
     "equal to 63 at 'CompilationJobName'",
     400,
     "Compilation job name is too long. Please use a shorter model name "
     "(maximum 30 characters recommended)."),
    ("ValidationException",
     "Value at 'roleArn' failed to satisfy constraint: Member must have "
     "length less than or equal to 2048",
     400,
     "Value at 'roleArn' failed to satisfy constraint: Field must have "
     "length less than or equal to 2048"),
    ("ValidationException",
     "Value with length greater than 63 is not allowed for Member",
     400,
     "Value with length greater than 63 is not allowed for Field"),
    ("ValidationException",
     "Unrecognized compiler option combination",
     400,
     "Validation error: Unrecognized compiler option combination"),
    ("AccessDeniedException",
     "User is not authorized to perform sagemaker:CreateCompilationJob",
     403,
     "Access denied. Please check your permissions for this use case."),
    ("ResourceLimitExceeded",
     "Account-level limit for compilation jobs reached",
     429,
     "Resource limit exceeded. Please try again later or contact support."),
]

_REQUEST_SCENARIOS = ["invalid_targets", "insufficient_role",
                      "not_completed", "client_error_mapping",
                      "no_jobs_404", "outsider_read_access"]

_invalid_target_lists = st.lists(
    st.text(alphabet=string.ascii_lowercase + string.digits + "-",
            min_size=1, max_size=12)
    .filter(lambda t: t not in FROZEN_COMPILATION_TARGETS),
    min_size=1, max_size=3, unique=True)


@settings(deadline=None)
@given(scenario=st.sampled_from(_REQUEST_SCENARIOS), data=st.data())
def test_request_level_identity(props_env, scenario, data):
    """Frozen request-level behavior: 400 on invalid targets naming them
    plus the valid list, 403 on insufficient role (POST without
    DataScientist — the reachable use-case-access denial), 400 on a
    non-Completed training job, the ValidationException / AccessDenied /
    ResourceLimitExceeded mappings, 404 on no compilation jobs, and the
    observed GET-path access decision (see outsider_read_access).
    # Validates: Requirements 3.4, 3.9, 3.10, 3.12
    """
    fresh_service()

    if scenario == "invalid_targets":
        invalid = data.draw(_invalid_target_lists)
        training_id = seed_training_record(props_env)
        response = raw_start(props_env, training_id, invalid)
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == (
            f"Invalid targets: {', '.join(invalid)}. Valid targets: "
            f"{', '.join(FROZEN_COMPILATION_TARGETS.keys())}")

    elif scenario == "insufficient_role":
        role = data.draw(st.sampled_from(["Viewer", "Operator"]))
        weak_user = make_extra_user(props_env, role=role)
        training_id = seed_training_record(props_env)
        response = raw_start(props_env, training_id, ["jetson-xavier-jp6"],
                             user_id=weak_user)
        assert response["statusCode"] == 403
        assert json.loads(response["body"])["error"] == \
            "Insufficient permissions"

    elif scenario == "not_completed":
        status = data.draw(st.sampled_from(
            ["InProgress", "Failed", "Stopped", "Initializing"]))
        training_id = seed_training_record(props_env, status=status)
        response = raw_start(props_env, training_id, ["jetson-xavier-jp6"])
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == (
            f"Training job must be completed. Current status: {status}")

    elif scenario == "client_error_mapping":
        code, message, expected_status, expected_error = data.draw(
            st.sampled_from(CLIENT_ERROR_MAPPINGS))
        _ServiceHolder.current.create_compilation_error = ClientError(
            {"Error": {"Code": code, "Message": message}},
            "CreateCompilationJob")
        training_id = seed_training_record(props_env)
        response = raw_start(props_env, training_id, ["jetson-xavier-jp6"])
        assert response["statusCode"] == expected_status
        assert json.loads(response["body"])["error"] == expected_error

    elif scenario == "no_jobs_404":
        training_id = seed_training_record(props_env)  # no compilation_jobs
        response = raw_status(props_env, training_id)
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"] == \
            "No compilation jobs found for this training"

    elif scenario == "outsider_read_access":
        # OBSERVED on the unfixed tree: rbac_manager.get_user_role falls
        # back to Role.VIEWER when no role row exists, so
        # check_user_access(user, usecase) with no required role grants
        # read access — GET /compile returns 200 even for a user with no
        # role assignment. The 403 use-case-access branch is unreachable
        # on the GET path today; the reachable 403 (POST without
        # DataScientist) is frozen by the insufficient_role scenario.
        # This encodes the access decision actually preserved by the fix.
        outsider = make_extra_user(props_env, role=None)
        name = f"neo-{uuid.uuid4().hex[:12]}"
        _ServiceHolder.current.compilation_jobs[name] = {
            "CompilationJobName": name,
            "CompilationJobStatus": "COMPLETED",
            "ModelArtifacts": {"S3ModelArtifacts":
                               f"s3://{USECASE_BUCKET}/compiled/{name}.tar.gz"},
        }
        training_id = seed_training_record(props_env, compilation_jobs=[{
            "target": "jetson-xavier-jp6",
            "compilation_job_name": name,
            "status": "INPROGRESS",
        }])
        response = raw_status(props_env, training_id, user_id=outsider)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["training_id"] == training_id


# ---------------------------------------------------------------------------
# Property 2 — audit-event shape and DynamoDB update-expression identity
# Validates: Requirements 3.11
# ---------------------------------------------------------------------------

def test_start_audit_event_and_update_expression_identity(props_env):
    """The start_compilation audit event keeps its field shape, and both
    the start and the poll write the same frozen DynamoDB update
    expression (compilation_jobs, compilation_status, updated_at). Job
    names are asserted for SUCCESSFUL starts only — the failed-start
    audit name is bug territory covered by the exploration suite.
    # Validates: Requirements 3.11
    """
    service = fresh_service()
    training_id = seed_training_record(props_env)
    body = start_compile(props_env, training_id,
                         ["jetson-xavier-jp6", "onnx"])
    job_names = [j["compilation_job_name"] for j in body["compilation_jobs"]]
    assert len(job_names) == 2

    # Frozen update expression on start.
    start_updates = updates_for(props_env.comp_updates, training_id)
    assert len(start_updates) == 1
    assert start_updates[0]["UpdateExpression"] == UPDATE_EXPR_JOBS
    assert set(start_updates[0]["ExpressionAttributeValues"]) == \
        {":jobs", ":cstatus", ":updated"}
    assert start_updates[0]["ExpressionAttributeValues"][":cstatus"] == \
        "InProgress"

    # Frozen audit event shape.
    scan = props_env.audit_log.scan(
        FilterExpression="resource_id = :rid AND #a = :act",
        ExpressionAttributeNames={"#a": "action"},
        ExpressionAttributeValues={":rid": training_id,
                                   ":act": "start_compilation"})
    items = scan["Items"]
    assert len(items) == 1, items
    audit = items[0]
    assert audit["user_id"] == props_env.user_id
    assert audit["resource_type"] == "training_job"
    assert audit["result"] == "success"
    details = audit["details"]
    assert set(details) == {"targets", "compilation_jobs", "auto_triggered"}
    assert list(details["targets"]) == ["jetson-xavier-jp6", "onnx"]
    assert list(details["compilation_jobs"]) == job_names
    assert details["auto_triggered"] is False

    # The poll writes the same frozen expression.
    poll_status(props_env, training_id)
    poll_updates = updates_for(props_env.comp_updates, training_id)
    assert len(poll_updates) == 2
    assert poll_updates[1]["UpdateExpression"] == UPDATE_EXPR_JOBS
    assert set(poll_updates[1]["ExpressionAttributeValues"]) == \
        {":jobs", ":cstatus", ":updated"}


# ---------------------------------------------------------------------------
# Property 2 — imported-ONNX bypass identity
# Validates: Requirements 3.8
# ---------------------------------------------------------------------------

_stems = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8)

_onnx_import_overrides = st.one_of(
    st.sampled_from(["ONNX", "onnx", "Onnx"]).map(
        lambda fw: {"metadata": {"framework": fw}}),
    st.sampled_from(["ONNX", "onnx"]).map(
        lambda fw: {"validation_result": {"metadata": {"framework": fw}}}),
    _stems.map(lambda stem: {"metadata": {"model_file": f"{stem}.onnx"}}),
    _stems.map(lambda stem: {"metadata": {"pt_file": f"{stem}.ONNX"}}),
)


@settings(deadline=None)
@given(overrides=_onnx_import_overrides)
def test_imported_onnx_bypass_identity(props_env, overrides):
    """Over generated records satisfying _is_onnx_import: the 200 /
    compilation_skipped / empty-list response and the exact message, with
    no SageMaker create call and the frozen skip update expression.
    # Validates: Requirements 3.8
    """
    service = fresh_service()
    training_id = seed_training_record(
        props_env, source="imported", **overrides)
    response = raw_start(props_env, training_id, ["onnx"])

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {
        "training_id": training_id,
        "compilation_jobs": [],
        "message": ONNX_BYPASS_MESSAGE,
        "compilation_skipped": True,
    }

    # No SageMaker submission of either kind.
    assert service.create_compilation_calls == []
    assert service.create_training_calls == []

    # The record carries the skip marker via the frozen update expression.
    updates = updates_for(props_env.comp_updates, training_id)
    assert len(updates) == 1
    assert updates[0]["UpdateExpression"] == UPDATE_EXPR_SKIPPED
    item = props_env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    assert item["compilation_skipped"] is True


# ---------------------------------------------------------------------------
# Property 2 — per-target independence
# Validates: Requirements 3.7
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(code=st.sampled_from(["AccessDeniedException", "ValidationException",
                             "ThrottlingException"]),
       onnx_first=st.booleans())
def test_per_target_independence(props_env, code, onnx_first):
    """One target's start failure never aborts the others: the response is
    still 200 with the full compilation_jobs list, the Neo entry started
    normally, and the failed ONNX entry records the failure. (The failed
    entry's job-name field is deliberately NOT asserted — that fabricated
    sentinel is the bug the fix removes.)
    # Validates: Requirements 3.7
    """
    service = fresh_service()
    failure_message = f"independence-test-{uuid.uuid4().hex[:8]}"
    service.create_training_error = ClientError(
        {"Error": {"Code": code, "Message": failure_message}},
        "CreateTrainingJob")

    targets = (["onnx", "jetson-xavier-jp6"] if onnx_first
               else ["jetson-xavier-jp6", "onnx"])
    training_id = seed_training_record(props_env)
    response = raw_start(props_env, training_id, targets)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    jobs = body["compilation_jobs"]
    assert [j["target"] for j in jobs] == targets
    assert body["message"] == "Started compilation for 2 target(s)"

    neo = next(j for j in jobs if j["target"] == "jetson-xavier-jp6")
    assert neo["status"] == "InProgress"
    assert neo["compilation_job_name"]
    assert neo["compilation_job_arn"]
    assert len(service.create_compilation_calls) == 1

    onnx = next(j for j in jobs if j["target"] == "onnx")
    assert onnx["status"] == "Failed"
    assert failure_message in onnx["error"]

    stored_jobs, _ = stored_record(props_env, training_id)
    assert [j["target"] for j in stored_jobs] == targets


# ---------------------------------------------------------------------------
# Property 2 — terminal-record identity (poller B / model detail page)
# Validates: Requirements 3.19
# ---------------------------------------------------------------------------

_terminal_status = st.sampled_from(
    ["COMPLETED", "FAILED", "STOPPED", "Completed", "Failed", "Stopped"])


@settings(deadline=None)
@given(statuses=st.lists(_terminal_status, min_size=1, max_size=3))
def test_terminal_record_identity_poller_b(props_env, statuses):
    """A record whose jobs are all terminal issues ZERO SageMaker describe
    calls when the model detail page loads (poller B). NOTE: this is
    scoped to poller B exactly as requirement 3.19 states — on the
    unfixed tree poller A describes every entry regardless of status
    (that is Defect 1's trigger, exercised by the exploration suite), so
    a poller-A zero-describe claim would not be preserved behavior.
    # Validates: Requirements 3.19
    """
    service = fresh_service()
    training_id = seed_training_record(props_env, compilation_jobs=[
        {"target": f"t{i}",
         "compilation_job_name": f"neo-{uuid.uuid4().hex[:12]}",
         "status": s}
        for i, s in enumerate(statuses)
    ], compilation_status="Completed")

    get_model(props_env, training_id)

    assert service.describe_calls == [], (
        f"Model detail load of an all-terminal record issued describe "
        f"calls: {service.describe_calls}")


# ---------------------------------------------------------------------------
# Property 2 — writer C (compilation_events.py) identity
# Validates: Requirements 3.23
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(status=st.sampled_from(["Completed", "Failed", "Stopped"]),
       other_completed=st.booleans())
def test_writer_c_identity(props_env, status, other_completed):
    """compilation_events.py matches by compilation_job_name, normalizes
    the status to uppercase, captures failure_reason on FAILED, chains
    packaging only when ALL jobs are COMPLETED, and writes ONLY
    compilation_jobs + updated_at.
    # Validates: Requirements 3.23
    """
    matched_name = f"neo-evt-{uuid.uuid4().hex[:12]}"
    other_name = f"neo-oth-{uuid.uuid4().hex[:12]}"
    other_status = "COMPLETED" if other_completed else "INPROGRESS"
    artifact_uri = f"s3://{USECASE_BUCKET}/compiled/{matched_name}/model.tar.gz"
    reason = f"neo-event-failure-{uuid.uuid4().hex[:8]}"

    training_id = seed_training_record(props_env, compilation_jobs=[
        {"target": "jetson-xavier-jp6", "compilation_job_name": matched_name,
         "status": "INPROGRESS"},
        {"target": "jetson-xavier-jp5", "compilation_job_name": other_name,
         "status": other_status},
    ], compilation_status="InProgress")

    detail = {
        "CompilationJobName": matched_name,
        "CompilationJobStatus": status,
        "CompilationJobArn":
            f"arn:aws:sagemaker:{REGION}:123456789012:compilation-job/"
            f"{matched_name}",
    }
    if status == "Completed":
        detail["ModelArtifacts"] = {"S3ModelArtifacts": artifact_uri}
    if status == "Failed":
        detail["FailureReason"] = reason

    invocations_before = len(props_env.fake_lambda.invocations)
    response = props_env.compilation_events.handle_compilation_state_change(
        {"source": "aws.sagemaker",
         "detail-type": "SageMaker Compilation Job State Change",
         "detail": detail}, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == status.upper()          # normalized uppercase
    assert body["packaging_triggered"] == (status.upper() == "COMPLETED")

    jobs, _ = stored_record(props_env, training_id)
    matched = next(j for j in jobs
                   if j["compilation_job_name"] == matched_name)
    other = next(j for j in jobs if j["compilation_job_name"] == other_name)
    assert matched["status"] == status.upper()
    assert other["status"] == other_status           # untouched
    if status == "Completed":
        assert matched["compiled_model_s3"] == artifact_uri
    if status == "Failed":
        assert matched["failure_reason"] == reason

    # Writes ONLY compilation_jobs + updated_at.
    updates = updates_for(props_env.events_updates, training_id)
    assert len(updates) == 1
    assert updates[0]["UpdateExpression"] == UPDATE_EXPR_WRITER_C
    assert set(updates[0]["ExpressionAttributeValues"]) == \
        {":jobs", ":updated"}

    # Packaging chains only when the event completes the LAST job.
    new_invocations = \
        props_env.fake_lambda.invocations[invocations_before:]
    if status == "Completed" and other_completed:
        assert len(new_invocations) == 1
        invoke = new_invocations[0]
        assert invoke["FunctionName"] == "test-packaging-fn"
        assert invoke["InvocationType"] == "Event"
        payload = json.loads(invoke["Payload"])
        assert payload["path"] == f"/api/v1/training/{training_id}/package"
        assert json.loads(payload["body"])["auto_triggered"] is True
    else:
        assert new_invocations == []


def test_writer_c_unmatched_name_identity(props_env):
    """An event whose CompilationJobName matches no record returns 404 and
    writes nothing — which is why removing the fabricated sentinel name
    cannot regress writer C: no SageMaker job ever emitted an event
    bearing it.
    # Validates: Requirements 3.23
    """
    phantom = f"{MODEL_NAME}-onnx-failed-{uuid.uuid4().hex[:8]}"
    before = len(props_env.events_updates)
    response = props_env.compilation_events.handle_compilation_state_change(
        {"detail": {"CompilationJobName": phantom,
                    "CompilationJobStatus": "Failed"}}, None)
    assert response["statusCode"] == 404
    assert response["body"] == "Training job not found"
    assert props_env.events_updates[before:] == []


# ---------------------------------------------------------------------------
# Property 2 — training_events.py identity for ONNX export job names
# Validates: Requirements 3.24
# ---------------------------------------------------------------------------

def test_training_events_identity_for_onnx_export_job(props_env):
    """training_events.py's name scan matches only a record-level
    training_job_name; an ONNX export job name (which lives inside
    compilation_jobs) finds no record, so the event has no effect on any
    training record through that path.
    # Validates: Requirements 3.24
    """
    export_job_name = f"{MODEL_NAME}-onnx-{uuid.uuid4().hex[:12]}"
    training_id = seed_training_record(
        props_env,
        training_job_name=f"real-training-{uuid.uuid4().hex[:8]}",
        compilation_jobs=[{
            "target": "onnx",
            "compilation_job_name": export_job_name,
            "status": "InProgress",
            "export_format": "onnx",
        }],
        compilation_status="InProgress")
    before = props_env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]

    response = props_env.training_events.handle_training_state_change(
        {"source": "aws.sagemaker",
         "detail-type": "SageMaker Training Job State Change",
         "detail": {"TrainingJobName": export_job_name,
                    "TrainingJobStatus": "Failed",
                    "FailureReason": "export blew up"}}, None)

    assert response["statusCode"] == 404
    assert response["body"] == "Training job not found"

    after = props_env.training_jobs.get_item(
        Key={"training_id": training_id})["Item"]
    assert after == before, (
        "An ONNX export training-job event mutated the training record "
        "through training_events.py")


# ===========================================================================
# Fix-checking property suites (task 4): Correctness Properties 1, 3, 4, 5.
# Everything below exercises the FIXED tree. The Property 2 preservation
# suite above is unchanged.
# ===========================================================================

import logging  # noqa: E402

# The shared layer module under test. conftest puts layers/shared/python on
# sys.path, and compilation.py / models.py (loaded by props_env) import this
# SAME module instance, so identity assertions are meaningful.
from compilation_status import (  # noqa: E402
    POLL_KIND_NONE,
    POLL_KIND_TRAINING,
    POLL_KIND_COMPILATION,
    RUNNING_STATUSES,
    COMPLETED_STATUSES,
    FAILED_STATUSES,
    TRANSIENT_STATUSES,
    TERMINAL_STATUSES,
    STATUS_POLL_ERROR,
    POLL_ERROR_MAX_ATTEMPTS,
    classify_poll_kind,
    entry_reason,
    is_terminal_status,
    derive_compilation_status,
)

_ALL_POLL_KINDS = {POLL_KIND_NONE, POLL_KIND_TRAINING, POLL_KIND_COMPILATION}

# Every status value in the shared vocabulary (the statuses the poller can
# emit, including the transient 'ERROR').
EMITTABLE_STATUSES = sorted(
    RUNNING_STATUSES | COMPLETED_STATUSES | FAILED_STATUSES
    | TRANSIENT_STATUSES)

_job_name_texts = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-",
    min_size=1, max_size=40).filter(lambda s: s.strip("-"))

_reason_texts = st.text(
    alphabet=string.ascii_letters + string.digits + " .,:/'()-_",
    min_size=1, max_size=80).filter(lambda s: s.strip())

_client_error_codes = st.sampled_from(
    ["AccessDeniedException", "ValidationException", "ThrottlingException",
     "ExpiredTokenException"])


# ---------------------------------------------------------------------------
# Property 1 (Fix Checking) — originating-reason survival (task 4.1)
# Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.6
# ---------------------------------------------------------------------------

@settings(deadline=None)
@given(message=_reason_texts, code=_client_error_codes,
       n_polls=st.integers(min_value=1, max_value=4))
def test_originating_reason_survives_n_polls(props_env, message, code,
                                             n_polls):
    """Over generated error strings x poll counts N >= 1: a record created
    through the ONNX start-failure path still reports the originating
    string as entry_reason after N polls, the fixed poller issues ZERO
    describe calls for the no-live-job entry, and the entry is
    byte-for-byte unchanged (status and reason included).
    # Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.6
    """
    service = fresh_service()
    service.create_training_error = ClientError(
        {"Error": {"Code": code, "Message": message}}, "CreateTrainingJob")

    training_id = seed_training_record(props_env)
    body = start_compile(props_env, training_id, ["onnx"])

    # The start-failure entry carries the originating reason and no
    # fabricated job name.
    (started,) = body["compilation_jobs"]
    assert started["job_started"] is False
    assert "compilation_job_name" not in started
    assert message in (entry_reason(started) or "")
    assert code in started["error"]

    jobs_before, _ = stored_record(props_env, training_id)
    (entry_before,) = jobs_before
    reason_before = entry_reason(entry_before)
    assert reason_before and message in reason_before

    for _ in range(n_polls):
        poll_status(props_env, training_id)

    # No describe call of either kind, ever, for a no-live-job entry.
    assert service.describe_calls == []

    jobs_after, _ = stored_record(props_env, training_id)
    (entry_after,) = jobs_after
    assert entry_reason(entry_after) == reason_before
    assert message in entry_reason(entry_after)
    assert entry_after["status"] == "Failed"      # never flipped to 'ERROR'
    assert entry_after == entry_before            # byte-for-byte unchanged


# ---------------------------------------------------------------------------
# Property 3 (Fix Checking) — Poll_Kind totality and round-trip (task 4.2)
# Validates: Requirements 2.1, 2.2, 2.3, 2.18
# ---------------------------------------------------------------------------

# Field names the system itself reads or writes on an entry; adversarial
# extra keys are drawn OUTSIDE this set so they are genuinely "extra".
_RESERVED_ENTRY_KEYS = {
    "target", "compilation_job_name", "compilation_job_arn", "status",
    "export_format", "job_started", "error", "failure_reason", "failed_step",
    "compiled_model_s3", "poll_error", "poll_error_at", "poll_error_count",
}

_extra_keys = st.dictionaries(
    st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=12)
      .filter(lambda k: k not in _RESERVED_ENTRY_KEYS),
    # Integers are bounded so the value survives the DynamoDB Decimal
    # round-trip: boto3's serializer context cannot re-serialize the huge
    # Decimals it can *read* (e.g. 10**28 raises decimal.DivisionImpossible
    # on the poller's write-back) — a harness constraint, not a property.
    st.one_of(st.none(), st.booleans(),
              st.integers(min_value=-2**31, max_value=2**31),
              st.text(alphabet=string.ascii_letters + string.digits,
                      min_size=1, max_size=10)),
    max_size=3)

_any_status = st.sampled_from(
    ["InProgress", "Completed", "Failed", "Stopped", "Stopping",
     "INPROGRESS", "COMPLETED", "FAILED", "STOPPED", "STARTING", "ERROR"])

_ENTRY_SHAPES = ["neo_success", "onnx_success", "onnx_start_failure",
                 "absent_name", "absent_export_format",
                 "job_started_false_with_name"]


@st.composite
def _classified_entries(draw):
    """(entry, expected Poll_Kind) over the entry shapes the system writes,
    plus adversarial extra keys."""
    shape = draw(st.sampled_from(_ENTRY_SHAPES))
    name = draw(_job_name_texts)
    status = draw(_any_status)

    if shape == "neo_success":
        entry = {"target": "jetson-xavier-jp6",
                 "compilation_job_name": name,
                 "compilation_job_arn":
                     f"arn:aws:sagemaker:{REGION}:123456789012:"
                     f"compilation-job/{name}",
                 "status": status}
        expected = POLL_KIND_COMPILATION
    elif shape == "onnx_success":
        entry = {"target": "onnx",
                 "compilation_job_name": name,
                 "compilation_job_arn":
                     f"arn:aws:sagemaker:{REGION}:123456789012:"
                     f"training-job/{name}",
                 "status": status,
                 "export_format": "onnx"}
        expected = POLL_KIND_TRAINING
    elif shape == "onnx_start_failure":
        entry = {"target": "onnx", "export_format": "onnx",
                 "status": "Failed", "job_started": False,
                 "error": draw(_reason_texts),
                 "failed_step": "start_onnx_export_job"}
        expected = POLL_KIND_NONE
    elif shape == "absent_name":
        entry = {"target": draw(st.sampled_from(NEO_TARGETS + ["onnx"])),
                 "status": status}
        variant = draw(st.sampled_from(["missing", "none", "empty"]))
        if variant == "none":
            entry["compilation_job_name"] = None
        elif variant == "empty":
            entry["compilation_job_name"] = ""
        expected = POLL_KIND_NONE
    elif shape == "absent_export_format":
        entry = {"target": draw(st.sampled_from(NEO_TARGETS)),
                 "compilation_job_name": name,
                 "status": status}
        expected = POLL_KIND_COMPILATION
    else:  # job_started False with a name present
        entry = {"target": "onnx",
                 "compilation_job_name": name,
                 "status": status,
                 "job_started": False}
        if draw(st.booleans()):
            entry["export_format"] = "onnx"
        expected = POLL_KIND_NONE

    entry.update(draw(_extra_keys))
    return entry, expected


@given(case=_classified_entries())
def test_classify_poll_kind_totality(case):
    """classify_poll_kind returns exactly one of the three kinds for every
    entry shape the system can write (plus adversarial extra keys) and
    never raises.
    # Validates: Requirements 2.1, 2.3, 2.18
    """
    entry, expected = case
    kind = classify_poll_kind(entry)   # must not raise
    assert kind in _ALL_POLL_KINDS
    assert kind == expected


@settings(deadline=None)
@given(case=_classified_entries(), live=st.booleans())
def test_poll_kind_describe_correspondence_and_round_trip(props_env, case,
                                                          live):
    """Poller A issues exactly the describe call the entry's Poll_Kind
    prescribes (and none for 'none'), and re-polling any entry either
    poller just wrote raises nothing.
    # Validates: Requirements 2.1, 2.2, 2.3, 2.18
    """
    entry, expected = case
    service = fresh_service()
    name = entry.get("compilation_job_name")
    if live and name:
        service.training_jobs[name] = {
            "TrainingJobName": name, "TrainingJobStatus": "InProgress"}
        service.compilation_jobs[name] = {
            "CompilationJobName": name, "CompilationJobStatus": "INPROGRESS"}

    training_id = seed_training_record(
        props_env, compilation_jobs=[dict(entry)],
        compilation_status="InProgress")

    poll_status(props_env, training_id)          # poller A — must not raise

    calls = [(api, called_name) for (api, called_name, _outcome)
             in service.describe_calls]
    if expected == POLL_KIND_NONE:
        assert calls == []
        jobs, _ = stored_record(props_env, training_id)
        assert jobs[0] == entry                  # not mutated in any way
    elif expected == POLL_KIND_TRAINING:
        assert calls == [("describe_training_job", name)]
    else:
        assert calls == [("describe_compilation_job", name)]

    # Round-trip: whatever either poller just wrote, re-polling it raises
    # nothing (each helper asserts a 200, i.e. no unhandled exception).
    poll_status(props_env, training_id)          # re-poll poller A's write
    get_model(props_env, training_id)            # poller B over the same jobs
    poll_status(props_env, training_id)          # re-poll poller B's write


# ---------------------------------------------------------------------------
# Property 4 (Fix Checking) — additive poll diagnostics (task 4.3)
# Validates: Requirements 2.4, 2.6, 2.7
# ---------------------------------------------------------------------------

_terminal_start_statuses = sorted(TERMINAL_STATUSES) + [
    "Completed", "Failed", "Stopped", "Stopping"]
_non_terminal_start_statuses = [
    "InProgress", "INPROGRESS", "STARTING", "IN_PROGRESS", "ERROR"]


@st.composite
def _poll_fault_cases(draw):
    """(entry, ClientError) pairs: terminal and non-terminal starting
    statuses, optional pre-existing reasons, seeded poll_error_count."""
    onnx = draw(st.booleans())
    name = draw(_job_name_texts)
    status = draw(st.sampled_from(
        _terminal_start_statuses + _non_terminal_start_statuses))
    entry = {"target": "onnx" if onnx else "jetson-xavier-jp6",
             "compilation_job_name": name,
             "status": status}
    if onnx:
        entry["export_format"] = "onnx"
    pre_failure_reason = draw(st.one_of(st.none(), _reason_texts))
    if pre_failure_reason is not None:
        entry["failure_reason"] = pre_failure_reason
    pre_error = draw(st.one_of(st.none(), _reason_texts))
    if pre_error is not None:
        entry["error"] = pre_error
    pre_count = draw(st.integers(min_value=0,
                                 max_value=POLL_ERROR_MAX_ATTEMPTS))
    if pre_count or draw(st.booleans()):
        entry["poll_error_count"] = pre_count
    fault = ClientError(
        {"Error": {"Code": draw(_client_error_codes),
                   "Message": draw(_reason_texts)}},
        "DescribeTrainingJob" if onnx else "DescribeCompilationJob")
    return entry, pre_count, fault


@settings(deadline=None)
@given(case=_poll_fault_cases())
def test_poll_diagnostics_are_additive(props_env, case):
    """Over generated (entry, ClientError) pairs: `error` and
    `failure_reason` are unchanged; poll_error / poll_error_at /
    poll_error_count are set; a terminal status is never overwritten; a
    transient fault does not latch a terminal state until
    POLL_ERROR_MAX_ATTEMPTS; and when it does, failure_reason is set only
    where none existed.
    # Validates: Requirements 2.4, 2.6, 2.7
    """
    entry, pre_count, fault = case
    service = fresh_service()

    def _raise_training(TrainingJobName):
        raise fault

    def _raise_compilation(CompilationJobName):
        raise fault

    service.describe_training_job = _raise_training
    service.describe_compilation_job = _raise_compilation

    training_id = seed_training_record(
        props_env, compilation_jobs=[dict(entry)],
        compilation_status="InProgress")

    poll_status(props_env, training_id)

    jobs, _ = stored_record(props_env, training_id)
    (stored,) = jobs
    new_count = pre_count + 1

    # Additive diagnostics land in their own poll_* fields.
    assert stored["poll_error"] == str(fault)
    assert stored["poll_error_at"]
    assert stored["poll_error_count"] == new_count

    # A poll is NEVER a writer of `error`.
    assert stored.get("error") == entry.get("error")

    if is_terminal_status(entry["status"]):
        # A terminal status is never overwritten, and its reason is intact.
        assert stored["status"] == entry["status"]
        assert stored.get("failure_reason") == entry.get("failure_reason")
    elif new_count < POLL_ERROR_MAX_ATTEMPTS:
        # Transient fault: mark 'ERROR' (non-terminal), never latch, and
        # leave any pre-existing failure_reason unchanged.
        assert stored["status"] == STATUS_POLL_ERROR
        assert not is_terminal_status(stored["status"])
        assert stored.get("failure_reason") == entry.get("failure_reason")
    else:
        # The bounded window closed: latch genuinely terminal, promoting
        # the poll reason into failure_reason ONLY where none existed.
        assert stored["status"] == "FAILED"
        if entry.get("failure_reason") is not None:
            assert stored["failure_reason"] == entry["failure_reason"]
        else:
            assert "could not be retrieved" in stored["failure_reason"]


# ---------------------------------------------------------------------------
# Property 5 (Fix Checking) — derivation totality, one shared
# implementation, cross-layer vocabulary (task 4.4)
# Validates: Requirements 2.8, 2.9, 2.10, 2.11, 2.12, 2.13
# ---------------------------------------------------------------------------

_cased_emittable = st.sampled_from(EMITTABLE_STATUSES).flatmap(_mixed_case)


@given(statuses=st.lists(_cased_emittable, max_size=6))
def test_derivation_totality_over_emittable_statuses(statuses):
    """Over mixed-case subsets of the emittable statuses (including
    'ERROR'): the result is inside the documented codomain; a
    transient-only set does not yield 'Failed'; and a genuine failure
    still dominates.
    # Validates: Requirements 2.8, 2.10
    """
    jobs = [{"target": f"t{i}", "status": s}
            for i, s in enumerate(statuses)]
    result = derive_compilation_status(jobs)

    if not statuses:
        assert result is None
    else:
        assert result in {"InProgress", "Completed", "Failed"}

    upper = {s.upper() for s in statuses}
    if upper and not (upper & FAILED_STATUSES) and (upper & TRANSIENT_STATUSES):
        # Transient poll fault(s) with no genuine failure never latch.
        assert result != "Failed"
    if (upper & FAILED_STATUSES) and not (upper & RUNNING_STATUSES):
        # A genuine failure dominates (including alongside 'ERROR').
        assert result == "Failed"


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


_unmodeled_tokens = st.text(
    alphabet=string.ascii_uppercase + "_", min_size=1, max_size=12,
).filter(lambda s: s not in (RUNNING_STATUSES | COMPLETED_STATUSES
                             | FAILED_STATUSES | TRANSIENT_STATUSES))


@given(value=_unmodeled_tokens, alongside_completed=st.booleans())
def test_derivation_unmodeled_value_is_named_not_failed(value,
                                                        alongside_completed):
    """An unmodeled status value produces an explicitly named (logged)
    outcome rather than a silent 'Failed' — no silent catch-all.
    # Validates: Requirements 2.8, 2.9
    """
    jobs = [{"target": "t0", "status": value}]
    if alongside_completed:
        jobs.append({"target": "t1", "status": "COMPLETED"})

    capture = _ListHandler()
    module_logger = logging.getLogger("compilation_status")
    module_logger.addHandler(capture)
    try:
        result = derive_compilation_status(jobs)
    finally:
        module_logger.removeHandler(capture)

    assert result != "Failed"                    # never silently collapsed
    assert result in {"InProgress", "Completed", "Failed"}  # in codomain
    assert any(value in m for m in capture.messages), (
        f"unmodeled value {value!r} was not named in a log warning")


def test_derivation_transient_rule_and_failure_dominance(props_env):
    """Pinned cases: a transient-only set is not 'Failed'; ERROR alongside
    COMPLETED keeps polling; {FAILED, ERROR} still yields 'Failed' (a
    genuine failure dominates).
    # Validates: Requirements 2.9, 2.10
    """
    assert derive_compilation_status(
        [{"status": "ERROR"}]) == "InProgress"
    assert derive_compilation_status(
        [{"status": "ERROR"}, {"status": "COMPLETED"}]) == "InProgress"
    assert derive_compilation_status(
        [{"status": "FAILED"}, {"status": "ERROR"}]) == "Failed"


def test_single_shared_derivation_implementation(props_env):
    """models.py imports the shared derive_compilation_status and contains
    no inline derivation; compilation.py re-exports the same function
    object; both handlers resolve to the layer module's implementation.
    # Validates: Requirements 2.11
    """
    import compilation_status as shared

    assert props_env.models.derive_compilation_status \
        is shared.derive_compilation_status
    assert props_env.compilation.derive_compilation_status \
        is shared.derive_compilation_status

    with open(os.path.join(_FUNCTIONS_DIR, "models.py")) as f:
        models_src = f.read()
    assert "from compilation_status import" in models_src
    assert "derive_compilation_status" in models_src
    # No inline re-implementation of the derivation rules.
    assert "def derive_compilation_status" not in models_src


@settings(deadline=None)
@given(status=st.sampled_from(sorted(TERMINAL_STATUSES)).flatmap(_mixed_case))
def test_poller_b_treats_every_terminal_value_as_terminal(props_env, status):
    """Every terminal value the shared vocabulary models (COMPLETED,
    FAILED, STOPPING, STOPPED — any case) is in poller B's terminal set:
    an entry carrying it is never re-polled on a model-detail load.
    # Validates: Requirements 2.12
    """
    assert is_terminal_status(status)
    service = fresh_service()
    training_id = seed_training_record(props_env, compilation_jobs=[{
        "target": "jetson-xavier-jp6",
        "compilation_job_name": f"neo-term-{uuid.uuid4().hex[:12]}",
        "status": status,
    }], compilation_status="Failed")

    get_model(props_env, training_id)

    assert service.describe_calls == [], (
        f"poller B re-polled a terminal ({status!r}) entry: "
        f"{service.describe_calls}")


# Every status value the fixed pollers/writers can persist into
# compilation_jobs[].status, case-exact:
#   - start time: 'InProgress' (success), 'Failed' (start failure)
#   - describe_training_job verbatim: InProgress|Completed|Failed|Stopping|Stopped
#   - describe_compilation_job verbatim / writer C's uppercase normalization:
#     STARTING|INPROGRESS|COMPLETED|FAILED|STOPPING|STOPPED
#   - the poll-fault marker 'ERROR' and the bounded latch 'FAILED'
POLLER_WRITABLE_STATUS_VALUES = {
    "InProgress", "Completed", "Failed", "Stopping", "Stopped",
    "STARTING", "INPROGRESS", "COMPLETED", "FAILED", "STOPPING", "STOPPED",
    "ERROR",
}

_FRONTEND_TYPES_TS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "frontend", "src", "types", "index.ts")


def test_frontend_union_models_every_writable_status():
    """Every status value the poller can write appears in the
    CompilationJob['status'] union in frontend/src/types/index.ts, so no
    value the backend persists is untyped at the client boundary — the
    test that would have caught Defect 3 ('ERROR' missing from every
    layer's vocabulary).
    # Validates: Requirements 2.13
    """
    with open(_FRONTEND_TYPES_TS) as f:
        src = f.read()
    interface = re.search(
        r"export interface CompilationJob \{(.*?)\n\}", src, re.S)
    assert interface, "CompilationJob interface not found in types/index.ts"
    status_line = re.search(r"\bstatus:\s*([^;]+);", interface.group(1))
    assert status_line, "CompilationJob has no status member"
    union = set(re.findall(r"'([^']+)'", status_line.group(1)))

    missing = sorted(POLLER_WRITABLE_STATUS_VALUES - union)
    assert not missing, (
        f"CompilationJob['status'] union is missing poller-writable "
        f"value(s): {missing} (union = {sorted(union)})")
