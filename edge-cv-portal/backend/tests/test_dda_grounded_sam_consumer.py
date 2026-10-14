"""
dda_autolabel_worker.py — grounded-sam consumer path, example tests.

Feature: grounded-sam-autolabel, task 3.4.

Covers, against the moto-backed stack from conftest.py (real
shared_utils cross-account fallback, moto DynamoDB + S3, fake Lambda
clients injected through the module seams `grounded_sam_lambda_client`
/ `sam_lambda_client` — the test_dda_autolabel_worker.py scaffolding):

- **Worker not configured** (Req 4.2, 5.4): with no
  GROUNDED_SAM_WORKER_FUNCTION_NAME (the flag-off deploy), a
  grounded-sam message resolves the task Failed with the exact reason
  'Grounded-SAM worker function is not configured', reaches no Lambda
  client, and writes no artifact — the sam family's degradation
  verbatim.
- **Client config** (Req 4.3, 7.4): _get_grounded_sam_lambda_client
  bounds the synchronous invocation wall clock at 240 s
  (GROUNDED_SAM_MAX_TIMEOUT_SECONDS as the read timeout, retries
  disabled), while the sam family's _get_sam_lambda_client keeps its
  untouched 120 s constant.
- **Invocation exception** (Req 4.3): a raising fake client (the shape
  a read timeout at the 240 s bound takes) marks the task Failed with
  the invocation error as the reason and writes no artifact.
- **Empty regions are a success** (Req 3.10): a `{"regions": []}`
  worker response stores an empty classified pre-label (empty
  `regions` for Segmentation, empty `boxes` for ObjectDetection) and
  the task becomes Available.
- **sam-family resolution semantics** (Req 4.8): a duplicate SQS
  delivery never double-resolves and the skip-verification counters
  move exactly once; an artifacts put_object failure stays transient —
  a batch item failure with the task left Pending
  (storage_failure_is_terminal stays `llm:`-only).
- **Dispatch routing** (Req 7.4): a `grounded-sam` message reaches
  _generate_grounded_sam_prelabel, while `sam`, `bedrock:<id>` and
  `llm:<id>` messages take their existing paths.

Requirements: 3.10, 4.2, 4.3, 4.8, 5.4, 7.4
"""
import json
import os
import sys
import uuid

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError

from test_dda_autolabel_worker import (
    DATASET_BUCKET,
    SAM_FUNCTION,
    AutolabelEnv,
    FakeSamLambdaClient,
)

REGION = "us-east-1"
ARTIFACTS_BUCKET = "test-portal-artifacts"
GROUNDED_SAM_FUNCTION = "test-dda-grounded-sam-worker"
LABELS = ["scratch", "dent"]


# ------------------------------------------------------------- fake clients

class _RaisingLambdaClient:
    """Fake Lambda client whose invoke always raises — the shape a
    botocore read timeout takes when the 240 s bound expires."""

    def __init__(self, exc):
        self.exc = exc
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        raise self.exc


class _RaisingPutObjectS3:
    """Stub for the worker's artifacts s3_client whose put_object always
    raises, exercising the real _write_prelabel wrapper at the
    put_object level."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def put_object(self, **kwargs):
        self.calls += 1
        raise self.exc


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock,
    with both worker function names configured."""
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    os.environ["GROUNDED_SAM_WORKER_FUNCTION_NAME"] = GROUNDED_SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    # The module read env at import; make sure the test values stuck.
    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    dda_autolabel_worker.GROUNDED_SAM_WORKER_FUNCTION_NAME = (
        GROUNDED_SAM_FUNCTION)
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


class GroundedSamEnv(AutolabelEnv):
    """AutolabelEnv plus the grounded-sam fake-client injection seam."""

    def use_grounded_sam(self, payload=None, function_error=None):
        fake = FakeSamLambdaClient(payload=payload,
                                   function_error=function_error)
        self.monkeypatch.setattr(self.worker, "grounded_sam_lambda_client",
                                 fake)
        return fake


@pytest.fixture
def env(aws_stack, worker, monkeypatch):
    return GroundedSamEnv(aws_stack, worker, monkeypatch)


# --------------------------------------------------- worker not configured

class TestWorkerNotConfigured:
    """Req 4.2, 5.4: the flag-off deploy leaves no
    GROUNDED_SAM_WORKER_FUNCTION_NAME — every grounded-sam image
    resolves Failed with the not-configured reason while the job
    proceeds (no batch failure), the sam degradation verbatim."""

    def test_missing_function_name_fails_with_exact_reason(self, env):
        env.monkeypatch.setattr(env.worker,
                                "GROUNDED_SAM_WORKER_FUNCTION_NAME", "")
        job_id = env.make_job(task_type="Segmentation", label_set=LABELS,
                              model="grounded-sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        # Even an injected client must never be reached: the env check
        # fails first.
        fake = env.use_grounded_sam(payload={
            "regions": [], "image_width": 100, "image_height": 80})

        result = env.run([env.record(job_id, task_id, image_uri,
                                     "Segmentation", LABELS,
                                     "grounded-sam")])

        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert task["prelabel_error"] == (
            "Grounded-SAM worker function is not configured")
        assert fake.invocations == []
        assert not env.prelabel_exists(job_id, task_id)


# --------------------------------------------------------- client config

class TestLambdaClientConfig:
    """Req 4.3, 7.4: the grounded-sam Lambda client bounds the read
    timeout at 240 s with retries disabled; the sam family's client
    keeps its untouched 120 s constant."""

    def _captured_config(self, env, factory, injection_attr, cache_attr):
        """The BotoConfig the factory hands boto3.client('lambda')."""
        captured = {}

        def capturing_client(service_name, **kwargs):
            captured["service"] = service_name
            captured["config"] = kwargs.get("config")
            return object()  # stands in for the boto3 Lambda client

        env.monkeypatch.setattr(env.worker, injection_attr, None)
        env.monkeypatch.setattr(env.worker, cache_attr, None)
        env.monkeypatch.setattr(env.worker.boto3, "client", capturing_client)
        getattr(env.worker, factory)()
        assert captured["service"] == "lambda"
        return captured["config"]

    def test_grounded_sam_client_uses_240s_read_timeout_no_retries(self, env):
        assert env.worker.GROUNDED_SAM_MAX_TIMEOUT_SECONDS == 240
        config = self._captured_config(
            env, "_get_grounded_sam_lambda_client",
            "grounded_sam_lambda_client",
            "_cached_grounded_sam_lambda_client")
        assert config.read_timeout == 240
        assert config.read_timeout == env.worker.GROUNDED_SAM_MAX_TIMEOUT_SECONDS
        assert config.connect_timeout == 10
        assert config.retries == {"max_attempts": 0}

    def test_sam_client_keeps_untouched_120s_bound(self, env):
        assert env.worker.SAM_MAX_TIMEOUT_SECONDS == 120
        config = self._captured_config(
            env, "_get_sam_lambda_client",
            "sam_lambda_client", "_cached_sam_lambda_client")
        assert config.read_timeout == 120
        assert config.read_timeout == env.worker.SAM_MAX_TIMEOUT_SECONDS
        assert config.connect_timeout == 10
        assert config.retries == {"max_attempts": 0}


# --------------------------------------------------- invocation exception

class TestInvocationException:
    """Req 4.3: an invocation exception marks the task Failed with the
    invocation error as the reason and writes no artifact."""

    def test_raising_client_marks_failed(self, env):
        job_id = env.make_job(task_type="Segmentation", label_set=LABELS,
                              model="grounded-sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        raising = _RaisingLambdaClient(
            ReadTimeoutError(endpoint_url="https://lambda.test"))
        env.monkeypatch.setattr(env.worker, "grounded_sam_lambda_client",
                                raising)

        result = env.run([env.record(job_id, task_id, image_uri,
                                     "Segmentation", LABELS,
                                     "grounded-sam")])

        assert result == {"batchItemFailures": []}
        assert len(raising.invocations) == 1
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "Grounded-SAM worker invocation failed" in task["prelabel_error"]
        assert "Read timeout" in task["prelabel_error"]
        assert not env.prelabel_exists(job_id, task_id)


# ------------------------------------------------- empty regions success

class TestEmptyRegionsSuccess:
    """Req 3.10: zero surviving detections are a success — an empty
    pre-label is stored and the task becomes Available."""

    EMPTY = {"regions": [], "image_width": 100, "image_height": 80}

    def test_segmentation_empty_regions_stored_available(self, env):
        job_id = env.make_job(task_type="Segmentation", label_set=LABELS,
                              model="grounded-sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        fake = env.use_grounded_sam(payload=self.EMPTY)

        result = env.run([env.record(job_id, task_id, image_uri,
                                     "Segmentation", LABELS,
                                     "grounded-sam")])

        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        assert task["prelabel_s3_key"] == (
            f"labeling/{env.usecase_id}/{job_id}/prelabels/{task_id}.json")
        assert env.prelabel_json(job_id, task_id) == {
            "modality": "Segmentation", "regions": [],
            "image_width": 100, "image_height": 80}

        # The synchronous invoke went to the configured grounded-sam
        # worker with a presigned image URL, the label-name-fallback
        # prompts (no overrides on the job record), and the modality.
        assert len(fake.invocations) == 1
        invocation = fake.invocations[0]
        assert invocation["FunctionName"] == GROUNDED_SAM_FUNCTION
        assert invocation["InvocationType"] == "RequestResponse"
        payload = json.loads(invocation["Payload"])
        assert payload["image_s3_presigned_url"].startswith("https://")
        assert DATASET_BUCKET in payload["image_s3_presigned_url"]
        assert payload["prompts"] == [
            {"label": label, "prompt": label} for label in LABELS]
        assert payload["modality"] == "Segmentation"

    def test_object_detection_empty_regions_stored_as_empty_boxes(self, env):
        job_id = env.make_job(task_type="ObjectDetection", label_set=LABELS,
                              model="grounded-sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_grounded_sam(payload=self.EMPTY)

        result = env.run([env.record(job_id, task_id, image_uri,
                                     "ObjectDetection", LABELS,
                                     "grounded-sam")])

        assert result == {"batchItemFailures": []}
        assert env.get_task(job_id, task_id)["prelabel_status"] == "Available"
        assert env.prelabel_json(job_id, task_id) == {
            "modality": "ObjectDetection", "boxes": [],
            "image_width": 100, "image_height": 80}


# --------------------------------------- sam-family resolution semantics

class TestSamFamilyResolutionSemantics:
    """Req 4.8: grounded-sam resolutions ride the same machinery as the
    sam family — conditional (idempotent) task resolution, one-move
    skip-verification counters, and transient storage failures."""

    SEG_PAYLOAD = {
        "regions": [{"class": "scratch", "rle": "12 5 3 5", "score": 0.91}],
        "image_width": 100,
        "image_height": 80,
    }

    def test_duplicate_delivery_never_double_resolves(self, env):
        """A redelivered message neither overwrites the resolution nor
        double-decrements the skip-verification counter."""
        job_id = env.make_job(task_type="Segmentation", label_set=LABELS,
                              model="grounded-sam", skip_verification=True,
                              autolabel_pending=1)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_grounded_sam(payload=self.SEG_PAYLOAD)
        record = env.record(job_id, task_id, image_uri, "Segmentation",
                            LABELS, "grounded-sam")

        assert env.run([record]) == {"batchItemFailures": []}

        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 0
        assert job["review_ready"] is True
        assert int(job["autolabel_completed_count"]) == 1

        # Duplicate delivery: the conditional resolution is a no-op —
        # the counters move exactly once.
        assert env.run([record]) == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 0
        assert int(job["autolabel_completed_count"]) == 1

    def test_storage_failure_stays_transient(self, env):
        """An artifacts put_object failure surfaces a batch item
        failure and leaves the task Pending for SQS redrive
        (storage_failure_is_terminal stays llm:-only)."""
        job_id = env.make_job(task_type="Segmentation", label_set=LABELS,
                              model="grounded-sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_grounded_sam(payload=self.SEG_PAYLOAD)
        stub = _RaisingPutObjectS3(
            RuntimeError("S3 outage: put_object refused"))
        env.monkeypatch.setattr(env.worker, "s3_client", stub)

        record = env.record(job_id, task_id, image_uri, "Segmentation",
                            LABELS, "grounded-sam")
        result = env.run([record])

        assert result == {"batchItemFailures": [
            {"itemIdentifier": record["messageId"]}]}
        assert stub.calls == 1
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Pending"
        assert "prelabel_error" not in task
        assert "prelabel_s3_key" not in task


# --------------------------------------------------------- dispatch routing

class TestDispatchRouting:
    """Req 7.4: _generate_prelabel dispatches `grounded-sam` to the new
    path by exact match while the existing families keep their existing
    paths."""

    ROUTES = {
        "_generate_grounded_sam_prelabel": "grounded-sam",
        "_generate_sam_prelabel": "sam",
        "_generate_bedrock_prelabel": "bedrock",
        "_generate_llm_prelabel": "llm",
    }

    def _dispatch(self, env, model):
        """Route one message through _generate_prelabel with every
        generator stubbed; returns (message, job, calls, result)."""
        calls = []

        def route(name):
            def _generate(*args):
                calls.append((name, args))
                return {"stub": name}
            return _generate

        for attr, name in self.ROUTES.items():
            env.monkeypatch.setattr(env.worker, attr, route(name))

        message = {"job_id": "job-x", "task_id": "task-x",
                   "image_s3_uri": f"s3://{DATASET_BUCKET}/img.png",
                   "modality": "Segmentation", "label_set": LABELS,
                   "model": model}
        job = {"job_id": "job-x", "usecase_id": "uc-x"}
        result = env.worker._generate_prelabel(message, job)
        return message, job, calls, result

    def test_grounded_sam_reaches_the_new_path(self, env):
        message, job, calls, result = self._dispatch(env, "grounded-sam")
        assert calls == [("grounded-sam", (message, job))]
        assert result == {"stub": "grounded-sam"}

    def test_sam_keeps_its_existing_path(self, env):
        message, job, calls, result = self._dispatch(env, "sam")
        assert calls == [("sam", (message, job))]
        assert result == {"stub": "sam"}

    def test_bedrock_keeps_its_existing_path(self, env):
        message, job, calls, result = self._dispatch(
            env, "bedrock:test-model-id")
        assert calls == [("bedrock", (message, job, "test-model-id"))]
        assert result == {"stub": "bedrock"}

    def test_llm_keeps_its_existing_path(self, env):
        message, job, calls, result = self._dispatch(
            env, "llm:us.amazon.nova-pro-v1:0")
        assert calls == [("llm", (message, job, "us.amazon.nova-pro-v1:0"))]
        assert result == {"stub": "llm"}
