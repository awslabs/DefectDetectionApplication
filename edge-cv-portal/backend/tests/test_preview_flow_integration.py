"""
Prompt_Tuning_Preview end-to-end integration (llm-autolabel-prompt-tuning,
task 9.9).

Feature: llm-autolabel-prompt-tuning

Drives the *whole* preview path against the moto-backed stack from
conftest.py — real `shared_utils` / `rbac_middleware`, real DynamoDB and
S3, real `dda_llm_request` / `dda_llm_prelabel` — with only the Converse
client stubbed. The harnesses are reused rather than re-created:
`PreviewEnv` (and through it `CreateJobEnv`) from
test_dda_labeling_preview_routes.py for the Use_Case, dataset prefix,
authorized creator, API Gateway event builders and preview-state readers,
plus `AutolabelEnv` / `jpeg_bytes` / `png_bytes` from
test_dda_autolabel_worker.py for the SQS path.

Three integration surfaces:

- **The preview flow** (Req 3.5, 3.6): seed a dataset prefix, `POST
  /labeling-preview/runs` as an authorized Job_Creator, drive the async
  self-invoke *inline* through `handler({'action':
  'execute_preview_run', ...})` with the payload the fake Lambda client
  recorded, then poll `GET /labeling-preview/runs/{runId}` to
  `Completed`. Asserts the per-sample result payloads (success, an
  `image_access_failure` that issues no invocation, and an
  `unusable_model_output` carrying the raw text verbatim), the
  `preview_run` audit event, the released in-flight lock, and that the
  jobs/tasks tables are unchanged — no Labeling_Job record, no item a
  labeler could reach through `assignee-index`, and nothing written
  under `labeling/{usecase_id}/`.

- **The cross-account read path** (Req 3.6): both the Sample_Image and
  the attached example image are read through
  `get_s3_client_for_bucket`, and in this single-account stack both
  resolve credentials through `assume_usecase_role`'s direct-access
  fallback (root ARN → default credentials). Credentials are cached per
  bucket for the life of the run; image bytes never are.

- **The worker few-shot path through the SQS record path** (Req 6.5,
  6.6, 10.2): with the Few_Shot_Option on, the Converse content carries
  the header, then each example identified immediately before its image
  in good-then-bad stored order, then the target intro, target image and
  prompt. With it off, the request is exactly `[image(target),
  text(prompt)]` — byte-identical to the pre-feature request.

Example references are full `s3://bucket/key` URIs throughout: they
resolve identically on the preview path (against the resolved dataset
bucket) and on the worker path (against the URI's own bucket), so the
same reference shape exercises both.

Requirements: 3.5, 3.6, 6.5, 6.6, 10.2
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

from dda_llm_request import FEW_SHOT_HEADER, FEW_SHOT_TARGET_INTRO
from test_dda_autolabel_worker import (
    AutolabelEnv,
    jpeg_bytes,
    png_bytes,
)
from test_dda_autolabel_worker import DATASET_BUCKET as WORKER_DATASET_BUCKET
from test_dda_autolabel_worker import SAM_FUNCTION
from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    POOL_ID,
    REGION,
    FakeCognitoClient,
    FakeLambdaClient,
)
from test_dda_labeling_preview_routes import ARTIFACTS_BUCKET, PreviewEnv

MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"
PROMPT = 'Find every "scratch" on the panel'
LABELS = ["scratch", "dent"]
FUNCTION_NAME = "test-dda-labeling-preview-flow"

GOOD = "good"
BAD = "bad"

BOX = {"class": "scratch",
       "box": {"left": 10, "top": 5, "width": 30, "height": 20}}
UNUSABLE_OUTPUT = "I'm sorry, I can't help with that request."


def guidance(detections):
    return json.dumps({"detections": detections})


def image_bytes_for(key, width, height):
    """PNG or JPEG header bytes, chosen from the key's extension the same
    way `image_format_for_key` chooses the Converse format."""
    return (png_bytes(width, height) if key.lower().endswith(".png")
            else jpeg_bytes(width, height))


# ------------------------------------------------------------ stub Converse

class StubConverseClient:
    """Records Converse kwargs and replies in order (the last reply
    repeats). A reply that is an Exception is raised instead."""

    def __init__(self, replies):
        self.calls = []
        self.replies = list(replies)

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        reply = (self.replies.pop(0) if len(self.replies) > 1
                 else self.replies[0])
        if isinstance(reply, Exception):
            raise reply
        return {"output": {"message": {"content": [{"text": reply}]}}}


class RecordingS3:
    """S3 client proxy recording every (Bucket, Key) read."""

    def __init__(self, inner, calls):
        self._inner = inner
        self._calls = calls

    def get_object(self, **kwargs):
        self._calls.append((kwargs.get("Bucket"), kwargs.get("Key")))
        return self._inner.get_object(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --------------------------------------------------------- preview fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients (the create-job convention)."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    try:
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=DATASET_BUCKET)
    except ClientError:
        pass  # a sibling module already created the shared dataset bucket

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


class PreviewFlowEnv(PreviewEnv):
    """PreviewEnv plus what an end-to-end run needs: real dataset and
    example objects, a stubbed Converse client, recorded S3 credential
    resolution and reads, and inline execution of the self-invoke."""

    def __init__(self, stack, dda, monkeypatch):
        super().__init__(stack, dda)
        self.monkeypatch = monkeypatch
        self.context = SimpleNamespace(function_name=FUNCTION_NAME)
        self.sample_bytes = {}
        self.example_bytes = {}
        self.client_requests = []
        self.get_object_calls = []
        self.assume_role_calls = []
        self._wrap_s3_access()

    # ------------------------------------------------------------- seams
    def _wrap_s3_access(self):
        """Record every dataset S3 client request and read, and every
        credential resolution behind it, without replacing either — the
        real `get_s3_client_for_bucket` (and so the real single-account
        fallback) still runs."""
        import shared_utils

        real_factory = self.module.get_s3_client_for_bucket
        requests, reads = self.client_requests, self.get_object_calls

        def recording_factory(usecase, bucket,
                              session_name="portal-s3-access"):
            requests.append((bucket, session_name))
            return RecordingS3(real_factory(usecase, bucket, session_name),
                               reads)

        self.monkeypatch.setattr(self.module, "get_s3_client_for_bucket",
                                 recording_factory)

        real_assume = shared_utils.assume_usecase_role
        resolutions = self.assume_role_calls

        def recording_assume(role_arn, external_id, session_name):
            credentials = real_assume(role_arn, external_id, session_name)
            resolutions.append({"role_arn": role_arn,
                                "session_name": session_name,
                                "credentials": credentials})
            return credentials

        self.monkeypatch.setattr(shared_utils, "assume_usecase_role",
                                 recording_assume)

    def use_bedrock(self, replies):
        stub = StubConverseClient(replies)
        recorded = {}

        def factory(region, timeout_seconds):
            recorded["region"] = region
            recorded["timeout_seconds"] = timeout_seconds
            return stub

        self.monkeypatch.setattr(self.module, "get_bedrock_client", factory)
        return stub, recorded

    # ---------------------------------------------------------- seeding
    def put_sample(self, name, width=120, height=90):
        """One real dataset object under the Use_Case dataset prefix."""
        key = f"{self.prefix}{name}"
        body = image_bytes_for(key, width, height)
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=key, Body=body)
        self.sample_bytes[key] = body
        return key

    def absent_sample(self, name):
        """A reference inside the dataset location with no object behind
        it: in scope for validation, unreadable for the executor."""
        return f"{self.prefix}{name}"

    def put_example(self, designation, position, ext="png"):
        """One example image in the Use_Case data bucket, referenced as a
        full s3:// URI (the shape both read paths resolve identically)."""
        key = (f"labeling-examples/{uuid.uuid4().hex[:8]}/"
               f"{designation}{position}.{ext}")
        body = image_bytes_for(key, 20, 10)
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=key, Body=body)
        ref = f"s3://{DATASET_BUCKET}/{key}"
        self.example_bytes[ref] = body
        return {"ref": ref, "designation": designation, "position": position}

    # -------------------------------------------------------- execution
    def drive_executor(self):
        """Run the executor inline from the recorded async self-invoke —
        the payload the deployed function would have received."""
        invocations = self.invocations()
        assert len(invocations) == 1, invocations
        assert invocations[0]["InvocationType"] == "Event"
        payload = json.loads(invocations[0]["Payload"])
        assert payload["action"] == "execute_preview_run"
        return self.module.handler(payload, self.context)

    # --------------------------------------------------------- readback
    def result_payload(self, run_id, index):
        key = f"labeling-previews/{self.usecase_id}/{run_id}/{index}.json"
        body = self.s3.get_object(Bucket=ARTIFACTS_BUCKET,
                                  Key=key)["Body"].read()
        return json.loads(body)

    def read_keys(self):
        return [key for _bucket, key in self.get_object_calls]

    def pipeline_object_count(self):
        """Objects under the labeling pipeline's own prefix — a
        Preview_Run must write none (Req 1.6, 3.5)."""
        return self.s3.list_objects_v2(
            Bucket=ARTIFACTS_BUCKET,
            Prefix=f"labeling/{self.usecase_id}/").get("KeyCount", 0)


@pytest.fixture
def preview_env(aws_stack, dda, monkeypatch):
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    return PreviewFlowEnv(aws_stack, dda, monkeypatch)


# --------------------------------------------------------- the preview flow

class TestPreviewFlowEndToEnd:
    """Req 3.5, 3.6: POST → executor → status, with per-sample outcomes
    and no labeling-pipeline state."""

    def test_run_completes_with_one_categorized_payload_per_sample(
            self, preview_env):
        env = preview_env
        succeeds = env.put_sample("panel-a.png", width=120, height=90)
        unusable = env.put_sample("panel-b.jpg", width=64, height=48)
        unreadable = env.absent_sample("panel-c.png")
        stub, recorded = env.use_bedrock([guidance([BOX]), UNUSABLE_OUTPUT])

        # --- start ------------------------------------------------------
        status, started = env.start(
            model=MODEL, detection_prompt=PROMPT,
            task_type="ObjectDetection", label_set=LABELS,
            sample_images=[succeeds, unusable, unreadable])
        assert status == 202
        assert started == {"run_id": started["run_id"], "sample_count": 3,
                           "status": "Running"}
        run_id = started["run_id"]

        # The status route answers one Pending entry per requested
        # Sample_Image from the moment the run exists, in request order.
        status, polled = env.status(run_id)
        assert status == 200
        assert polled["status"] == "Running"
        assert polled["results"] == [
            {"index": index, "sample_key": key, "state": "Pending"}
            for index, key in enumerate([succeeds, unusable, unreadable])]

        # --- execute (inline, from the recorded self-invoke) ------------
        outcome = env.drive_executor()
        assert outcome == {"run_id": run_id,
                           "action": "execute_preview_run",
                           "status": "Completed", "sample_count": 3,
                           "succeeded": 1, "failed": 2}

        # --- poll to Completed -----------------------------------------
        status, polled = env.status(run_id)
        assert status == 200
        assert polled["status"] == "Completed"
        assert polled["sample_count"] == 3
        assert polled["few_shot"] == {"enabled": False, "attached": 0,
                                      "omitted": 0}
        assert "run_error" not in polled

        succeeded, failed_output, failed_access = polled["results"]
        assert [entry["sample_key"] for entry in polled["results"]] == [
            succeeds, unusable, unreadable]
        assert succeeded["state"] == "Succeeded"
        assert succeeded["result_url_expires_in"] == 900
        assert succeeded["result_url"]
        assert isinstance(succeeded["resolved_at"], int)
        assert "failure_category" not in succeeded
        assert failed_output["state"] == "Failed"
        assert failed_output["failure_category"] == "unusable_model_output"
        assert failed_output["failure_reason"]
        assert failed_access["state"] == "Failed"
        assert failed_access["failure_category"] == "image_access_failure"
        assert unreadable in failed_access["failure_reason"]

        # --- per-sample payloads ---------------------------------------
        success_payload = env.result_payload(run_id, 0)
        assert set(success_payload) == {"sample_key", "state", "prelabel",
                                       "image_width", "image_height"}
        assert success_payload["sample_key"] == succeeds
        assert success_payload["state"] == "Succeeded"
        assert success_payload["image_width"] == 120
        assert success_payload["image_height"] == 90
        assert success_payload["prelabel"] == {
            "modality": "ObjectDetection",
            "boxes": [{"class": "scratch", "left": 10, "top": 5,
                       "width": 30, "height": 20}],
            "image_width": 120, "image_height": 90}

        unusable_payload = env.result_payload(run_id, 1)
        assert set(unusable_payload) == {"sample_key", "state",
                                        "failure_category",
                                        "failure_reason",
                                        "raw_model_output"}
        assert unusable_payload["failure_category"] == "unusable_model_output"
        # Verbatim, untruncated (Req 9.3).
        assert unusable_payload["raw_model_output"] == UNUSABLE_OUTPUT

        access_payload = env.result_payload(run_id, 2)
        assert set(access_payload) == {"sample_key", "state",
                                       "failure_category", "failure_reason"}
        assert access_payload["failure_category"] == "image_access_failure"

        # --- exactly one invocation per readable sample ----------------
        assert len(stub.calls) == 2
        assert [call["modelId"] for call in stub.calls] == [MODEL_ID, MODEL_ID]
        assert recorded["timeout_seconds"] == 120
        # The unreadable sample was attempted exactly once and produced
        # no invocation at all: three samples, two Converse calls.
        assert env.read_keys().count(unreadable) == 1

        # --- audit event (Req 3.8) -------------------------------------
        events = env.audit_events("preview_run")
        assert len(events) == 1
        assert events[0]["user_id"] == env.creator["user_id"]
        assert events[0]["resource_id"] == run_id
        assert events[0]["result"] == "success"
        assert int(events[0]["details"]["sample_count"]) == 3
        assert events[0]["details"]["model"] == MODEL

        # --- released lock ---------------------------------------------
        assert env.lock_item() is None

        # --- unchanged jobs / tasks tables (Req 1.6, 3.5) --------------
        assert env.usecase_jobs() == []
        assert env.audit_events("job_created") == []
        preview_items = env.preview_items()
        # Only the RUN item and one item per sample were added, and none
        # of them carries assignee_user_id, which is what keeps them out
        # of the assignee-index GSI a labeler is served from.
        assert env._count_task_items() == env._task_baseline + 4
        assert all("assignee_user_id" not in item for item in preview_items)
        assert env.pipeline_object_count() == 0

    def test_a_second_run_is_possible_once_the_lock_is_released(
            self, preview_env):
        """The claim released in the executor's `finally` is what makes
        the wizard's next iteration possible (Req 8.8)."""
        env = preview_env
        sample = env.put_sample("iterate.png")
        env.use_bedrock([guidance([])])

        status, first = env.start(
            model=MODEL, detection_prompt=PROMPT,
            task_type="Classification", sample_images=[sample])
        assert status == 202
        env.drive_executor()
        assert env.lock_item() is None

        env.invocations().clear()
        status, second = env.start(
            model=MODEL, detection_prompt="A revised prompt",
            task_type="Classification", sample_images=[sample])
        assert status == 202
        assert second["run_id"] != first["run_id"]
        env.drive_executor()

        _, polled = env.status(second["run_id"])
        assert polled["status"] == "Completed"
        # Zero detections are a *successful* empty result, not a failure.
        assert env.result_payload(second["run_id"], 0)["prelabel"] == {
            "modality": "Classification", "label": "normal"}


# ------------------------------------------------------- cross-account read

class TestCrossAccountReadPath:
    """Req 3.6: Sample_Images *and* example images are read through
    `get_s3_client_for_bucket`, including its single-account
    direct-access fallback."""

    def test_samples_and_examples_read_through_the_direct_fallback(
            self, preview_env):
        env = preview_env
        sample = env.put_sample("target.png")
        good = env.put_example(GOOD, 0, ext="png")
        bad = env.put_example(BAD, 0, ext="jpg")
        stub, _ = env.use_bedrock([guidance([BOX])])

        status, started = env.start(
            model=MODEL, detection_prompt=PROMPT,
            task_type="ObjectDetection", label_set=LABELS,
            sample_images=[sample],
            few_shot={"enabled": True, "examples": [bad, good]})
        assert status == 202
        env.drive_executor()

        status, polled = env.status(started["run_id"])
        assert status == 200
        assert polled["status"] == "Completed"
        assert polled["few_shot"] == {"enabled": True, "attached": 2,
                                      "omitted": 0}
        assert polled["results"][0]["state"] == "Succeeded"

        # Both the Sample_Image and both attached examples were read
        # through a client the Use_Case's own access mechanism produced,
        # target first so an unreadable example can never mask an
        # unreadable target.
        assert env.get_object_calls == [
            (DATASET_BUCKET, sample),
            (DATASET_BUCKET, good["ref"].split("/", 3)[3]),
            (DATASET_BUCKET, bad["ref"].split("/", 3)[3]),
        ]
        # Credentials are cached per bucket for the life of the run: one
        # client request for three reads, under the preview's own session
        # name.
        assert env.client_requests == [
            (DATASET_BUCKET, "dda-labeling-preview")]

        # Single-account setup: the root cross-account ARN resolves to
        # the Lambda's own credentials rather than an assumed role.
        assert env.assume_role_calls
        for resolution in env.assume_role_calls:
            assert resolution["role_arn"].endswith(":root")
            assert resolution["session_name"] == "dda-labeling-preview"
            assert resolution["credentials"]["is_default_credentials"] is True
            assert resolution["credentials"]["AccessKeyId"] is None

        # The example bytes reached the model, so the fallback really did
        # produce a usable client for the example objects too.
        content = stub.calls[0]["messages"][0]["content"]
        example_bytes = [block["image"]["source"]["bytes"]
                         for block in content if "image" in block]
        assert example_bytes[:2] == [env.example_bytes[good["ref"]],
                                    env.example_bytes[bad["ref"]]]
        assert example_bytes[2] == env.sample_bytes[sample]


# -------------------------------------------------- worker few-shot via SQS

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=WORKER_DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


class WorkerFewShotEnv(AutolabelEnv):
    """AutolabelEnv plus example uploads and few-shot job documents."""

    def __init__(self, stack, worker, monkeypatch):
        super().__init__(stack, worker, monkeypatch)
        self.example_bytes = {}

    def put_example(self, designation, position, ext="png"):
        key = (f"labeling-examples/{uuid.uuid4().hex[:8]}/"
               f"{designation}{position}.{ext}")
        body = image_bytes_for(key, 20, 10)
        self.s3.put_object(Bucket=WORKER_DATASET_BUCKET, Key=key, Body=body)
        ref = f"s3://{WORKER_DATASET_BUCKET}/{key}"
        self.example_bytes[ref] = body
        return {"ref": ref, "designation": designation, "position": position}

    def make_few_shot_job(self, examples=None, task_type="ObjectDetection"):
        job_id = self.make_job(task_type=task_type, label_set=LABELS,
                               model=MODEL)
        if examples is not None:
            self.stack.tables.labeling_jobs.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET auto_label.few_shot = :fs",
                ExpressionAttributeValues={
                    ":fs": {"enabled": True, "examples": examples}})
        return job_id


@pytest.fixture
def worker_env(aws_stack, worker, monkeypatch):
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    return WorkerFewShotEnv(aws_stack, worker, monkeypatch)


class TestWorkerFewShotThroughSqs:
    """Req 6.5, 6.6, 10.2 through the real SQS handler, with example
    references stored as s3:// URIs — the shape that resolves identically
    on the preview and the labeling-time path."""

    def test_option_on_attaches_identified_examples_in_stored_order(
            self, worker_env):
        env = worker_env
        # Stored order interleaves the designations; attachment order is
        # every good example first, then every bad one.
        bad_0 = env.put_example(BAD, 0, ext="jpg")
        good_0 = env.put_example(GOOD, 0, ext="png")
        good_1 = env.put_example(GOOD, 1, ext="PNG")
        job_id = env.make_few_shot_job(examples=[bad_0, good_0, good_1])
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                 width=100, height=80)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock(replies=[guidance([BOX])])

        result = env.run([env.record(job_id, task_id, image_uri,
                                    "ObjectDetection", LABELS, MODEL,
                                    detection_prompt=PROMPT)])

        assert result == {"batchItemFailures": []}
        assert env.get_task(job_id, task_id)["prelabel_status"] == "Available"
        assert env.prelabel_json(job_id, task_id)["modality"] == (
            "ObjectDetection")

        assert len(bedrock.calls) == 1
        content = bedrock.calls[0]["messages"][0]["content"]
        assert len(content) == 10
        # Header, then each example identified immediately before its own
        # image, good-then-bad in stored order (Req 6.5).
        assert [content[index]["text"] for index in (0, 1, 3, 5)] == [
            FEW_SHOT_HEADER, "Good example 1:", "Good example 2:",
            "Bad example 1:"]
        assert [content[index]["image"]["source"]["bytes"]
                for index in (2, 4, 6)] == [env.example_bytes[good_0["ref"]],
                                            env.example_bytes[good_1["ref"]],
                                            env.example_bytes[bad_0["ref"]]]
        # Formats derive from each ref's extension, case-insensitively.
        assert [content[index]["image"]["format"]
                for index in (2, 4, 6)] == ["png", "png", "jpeg"]
        # The target intro, target image and prompt keep their order.
        assert content[7]["text"] == FEW_SHOT_TARGET_INTRO
        assert content[8]["image"]["source"]["bytes"].startswith(b"\x89PNG")
        assert PROMPT in content[9]["text"]

    def test_option_off_issues_the_pre_feature_request(self, worker_env):
        """Req 10.2: without the option the request is exactly
        [image(target), text(prompt)] — the pre-feature shape."""
        env = worker_env
        job_id = env.make_few_shot_job(examples=None)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=100, height=80)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock(replies=[guidance([BOX])])

        result = env.run([env.record(job_id, task_id, image_uri,
                                     "ObjectDetection", LABELS, MODEL,
                                     detection_prompt=PROMPT)])

        assert result == {"batchItemFailures": []}
        assert env.get_task(job_id, task_id)["prelabel_status"] == "Available"

        content = bedrock.calls[0]["messages"][0]["content"]
        assert len(content) == 2
        assert set(content[0]) == {"image"}
        assert set(content[1]) == {"text"}
        assert content[0]["image"]["source"]["bytes"].startswith(b"\x89PNG")
        assert PROMPT in content[1]["text"]
        assert "example" not in content[1]["text"].lower()

    def test_examples_are_never_read_when_the_option_is_off(self,
                                                           worker_env):
        """A stored-but-disabled document reads nothing: the request is
        the pre-feature one whatever references exist (Req 10.2, 10.3)."""
        env = worker_env
        example = env.put_example(GOOD, 0)
        job_id = env.make_job(task_type="ObjectDetection", label_set=LABELS,
                              model=MODEL)
        env.stack.tables.labeling_jobs.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET auto_label.few_shot = :fs",
            ExpressionAttributeValues={
                ":fs": {"enabled": False, "examples": [example]}})
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=100, height=80)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock(replies=[guidance([BOX])])

        env.run([env.record(job_id, task_id, image_uri, "ObjectDetection",
                            LABELS, MODEL, detection_prompt=PROMPT)])

        content = bedrock.calls[0]["messages"][0]["content"]
        assert len(content) == 2
        assert env.example_bytes[example["ref"]] not in [
            block["image"]["source"]["bytes"]
            for block in content if "image" in block]
