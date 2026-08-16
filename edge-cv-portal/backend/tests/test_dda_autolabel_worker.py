"""
dda_autolabel_worker.py — SQS auto-label consumer (dda-data-labeling,
task 10.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils cross-account fallback, moto DynamoDB + S3, a fake Bedrock
client recording Converse calls, and a fake Lambda client standing in
for dda_sam_worker):

- Bedrock Classification success: prelabel_status=Available with the
  validated pre-label JSON written to the portal artifacts bucket at
  labeling/{usecase_id}/{job_id}/prelabels/{task_id}.json, the Converse
  request carrying the image block and the label set, and the read
  timeout capped at 120 s (Req 8.2, 8.5, 12.1, 12.2)
- Strict output validation: a class outside the Label_Set, malformed
  box geometry, and out-of-bounds boxes each mark the task Failed with
  a recorded reason and write no pre-label object (Req 8.2, 8.5)
- SAM path: synchronous invoke of the SAM worker with a presigned image
  URL; regions stored as a class-agnostic (class: null) pre-label
  (Req 8.2)
- Skip-verification mode: per_label_prompts appended to the prompt,
  autolabel_pending decremented per resolution, review_ready set at
  zero, failures recording autolabel_error for review ineligibility,
  and duplicate deliveries never double-decrementing (Req 9.4, 9.5,
  9.10)
- Per-record isolation: one bad record in a batch neither fails the
  batch nor blocks the remaining records
"""
import io
import json
import struct
import sys
import uuid
import zlib
from types import SimpleNamespace

import boto3
import pytest

REGION = "us-east-1"
DATASET_BUCKET = "test-autolabel-data"
ARTIFACTS_BUCKET = "test-portal-artifacts"
SAM_FUNCTION = "test-dda-sam-worker"


def png_bytes(width, height):
    """A minimal-but-real PNG header (signature + IHDR) carrying the
    given dimensions, enough for header-based dimension parsing."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = (struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
             + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)))
    return signature + chunk


# ------------------------------------------------------------- fake clients

class FakeBedrockClient:
    """Records Converse calls; returns canned text replies in order."""

    def __init__(self, replies=None, error=None):
        self.calls = []
        self.replies = list(replies or [])
        self.error = error

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"output": {"message": {"content": [{"text": reply}]}}}


class FakeSamLambdaClient:
    """Records synchronous SAM worker invocations; returns a canned payload."""

    def __init__(self, payload=None, function_error=None):
        self.invocations = []
        self.payload = payload
        self.function_error = function_error

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        response = {
            "StatusCode": 200,
            "Payload": io.BytesIO(json.dumps(self.payload or {}).encode()),
        }
        if self.function_error:
            response["FunctionError"] = "Unhandled"
            response["Payload"] = io.BytesIO(
                json.dumps({"errorMessage": self.function_error}).encode())
        return response


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    import os
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    # Module read env at import; make sure the test value stuck.
    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


@pytest.fixture
def env(aws_stack, worker, monkeypatch):
    return AutolabelEnv(aws_stack, worker, monkeypatch)


class AutolabelEnv:
    def __init__(self, stack, worker, monkeypatch):
        self.stack = stack
        self.worker = worker
        self.monkeypatch = monkeypatch
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Autolabel Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })

    # ------------------------------------------------------------ setup
    def put_image(self, key, width=100, height=80):
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=key,
                           Body=png_bytes(width, height))
        return f"s3://{DATASET_BUCKET}/{key}"

    def make_job(self, task_type="Classification", label_set=None,
                 skip_verification=False, autolabel_pending=None,
                 per_label_prompts=None, model="bedrock:test-model-id"):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "task_type": task_type,
            "label_set": label_set or ["normal", "anomaly"],
            "skip_verification": skip_verification,
            "auto_label": {"enabled": True, "model": model},
            "created_at": 1,
            "updated_at": 1,
        }
        if autolabel_pending is not None:
            item["autolabel_pending"] = autolabel_pending
        if per_label_prompts:
            item["per_label_prompts"] = per_label_prompts
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def make_task(self, job_id, image_uri, task_id=None):
        task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        self.stack.tables.labeling_tasks.put_item(Item={
            "job_id": job_id,
            "task_id": task_id,
            "usecase_id": self.usecase_id,
            "image_s3_uri": image_uri,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "prelabel_status": "Pending",
        })
        return task_id

    def use_bedrock(self, replies=None, error=None):
        fake = FakeBedrockClient(replies=replies, error=error)
        recorded = {}

        def fake_factory(region, timeout_seconds):
            recorded["region"] = region
            recorded["timeout_seconds"] = timeout_seconds
            return fake

        self.monkeypatch.setattr(self.worker, "get_bedrock_client",
                                 fake_factory)
        return fake, recorded

    def use_sam(self, payload=None, function_error=None):
        fake = FakeSamLambdaClient(payload=payload,
                                   function_error=function_error)
        self.monkeypatch.setattr(self.worker, "sam_lambda_client", fake)
        return fake

    # ------------------------------------------------------------ invoke
    @staticmethod
    def record(job_id, task_id, image_uri, modality, label_set, model,
               per_label_prompts=None, body_override=None):
        body = body_override if body_override is not None else json.dumps({
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": image_uri,
            "modality": modality,
            "label_set": label_set,
            "model": model,
            **({"per_label_prompts": per_label_prompts}
               if per_label_prompts else {}),
        })
        return {"messageId": f"msg-{uuid.uuid4().hex[:8]}", "body": body}

    def run(self, records):
        return self.worker.handler({"Records": records}, None)

    # ------------------------------------------------------------- store
    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def prelabel_json(self, job_id, task_id):
        key = (f"labeling/{self.usecase_id}/{job_id}/prelabels/"
               f"{task_id}.json")
        body = self.s3.get_object(Bucket=ARTIFACTS_BUCKET,
                                  Key=key)["Body"].read()
        return json.loads(body)

    def prelabel_exists(self, job_id, task_id):
        key = (f"labeling/{self.usecase_id}/{job_id}/prelabels/"
               f"{task_id}.json")
        try:
            self.s3.head_object(Bucket=ARTIFACTS_BUCKET, Key=key)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------- Bedrock

class TestBedrockClassification:
    def test_success_marks_available_with_prelabel_in_s3(self, env):
        """Req 8.2, 12.1, 12.2: valid Bedrock classification output is
        stored as a pre-label and the task becomes Available."""
        job_id = env.make_job(task_type="Classification")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        bedrock, recorded = env.use_bedrock(replies=['{"label": "anomaly"}'])

        result = env.run([env.record(
            job_id, task_id, image_uri, "Classification",
            ["normal", "anomaly"], "bedrock:test-model-id")])

        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        assert task["prelabel_s3_key"] == (
            f"labeling/{env.usecase_id}/{job_id}/prelabels/{task_id}.json")
        assert env.prelabel_json(job_id, task_id) == {
            "modality": "Classification", "label": "anomaly"}

        # Converse request: image block + structured prompt with the
        # label set, addressed to the message's model id.
        assert len(bedrock.calls) == 1
        call = bedrock.calls[0]
        assert call["modelId"] == "test-model-id"
        content = call["messages"][0]["content"]
        image_block = content[0]["image"]
        assert image_block["format"] == "png"
        assert image_block["source"]["bytes"].startswith(b"\x89PNG")
        prompt = content[1]["text"]
        assert "normal" in prompt and "anomaly" in prompt
        assert '"label"' in prompt

    def test_read_timeout_capped_at_120_seconds(self, env):
        """Req 8.5: the Bedrock client read timeout never exceeds 120 s."""
        job_id = env.make_job(task_type="Classification")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        _, recorded = env.use_bedrock(replies=['{"label": "normal"}'])

        env.run([env.record(job_id, task_id, image_uri, "Classification",
                            ["normal", "anomaly"], "bedrock:test-model-id")])

        assert recorded["timeout_seconds"] <= 120

    def test_label_outside_label_set_marks_failed(self, env):
        """Req 8.2, 8.5: a class outside the Label_Set is a generation
        failure with a recorded reason and no pre-label object."""
        job_id = env.make_job(task_type="Classification")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_bedrock(replies=['{"label": "scratched"}'])

        result = env.run([env.record(
            job_id, task_id, image_uri, "Classification",
            ["normal", "anomaly"], "bedrock:test-model-id")])

        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "scratched" in task["prelabel_error"]
        assert not env.prelabel_exists(job_id, task_id)

    def test_model_error_marks_failed(self, env):
        """Req 8.5: a Bedrock invocation error is a generation failure."""
        job_id = env.make_job(task_type="Classification")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_bedrock(error=RuntimeError("model exploded"))

        env.run([env.record(job_id, task_id, image_uri, "Classification",
                            ["normal", "anomaly"], "bedrock:test-model-id")])

        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "model exploded" in task["prelabel_error"]


class TestBedrockObjectDetection:
    LABELS = ["scratch", "dent"]

    def _detection_env(self, env, reply):
        job_id = env.make_job(task_type="ObjectDetection",
                              label_set=self.LABELS)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=100, height=80)
        task_id = env.make_task(job_id, image_uri)
        env.use_bedrock(replies=[reply])
        env.run([env.record(job_id, task_id, image_uri, "ObjectDetection",
                            self.LABELS, "bedrock:test-model-id")])
        return job_id, task_id

    def test_valid_boxes_stored(self, env):
        reply = json.dumps({"boxes": [
            {"class": "scratch", "left": 10, "top": 5,
             "width": 30, "height": 20}]})
        job_id, task_id = self._detection_env(env, reply)
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        prelabel = env.prelabel_json(job_id, task_id)
        assert prelabel["modality"] == "ObjectDetection"
        assert prelabel["image_width"] == 100
        assert prelabel["image_height"] == 80
        assert prelabel["boxes"] == [
            {"class": "scratch", "left": 10.0, "top": 5.0,
             "width": 30.0, "height": 20.0}]

    def test_out_of_bounds_box_marks_failed(self, env):
        """Req 8.2: a box exceeding the image bounds fails generation."""
        reply = json.dumps({"boxes": [
            {"class": "scratch", "left": 90, "top": 5,
             "width": 30, "height": 20}]})  # 90+30 > 100
        job_id, task_id = self._detection_env(env, reply)
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "bounds" in task["prelabel_error"]
        assert not env.prelabel_exists(job_id, task_id)

    def test_malformed_geometry_marks_failed(self, env):
        """Req 8.2: missing/non-numeric geometry fails generation."""
        reply = json.dumps({"boxes": [
            {"class": "dent", "left": 10, "width": 30, "height": 20}]})
        job_id, task_id = self._detection_env(env, reply)
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "top" in task["prelabel_error"]

    def test_box_class_outside_label_set_marks_failed(self, env):
        reply = json.dumps({"boxes": [
            {"class": "crack", "left": 10, "top": 5,
             "width": 30, "height": 20}]})
        job_id, task_id = self._detection_env(env, reply)
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "crack" in task["prelabel_error"]

    def test_negative_dimensions_mark_failed(self, env):
        reply = json.dumps({"boxes": [
            {"class": "dent", "left": 10, "top": 5,
             "width": -3, "height": 20}]})
        job_id, task_id = self._detection_env(env, reply)
        assert env.get_task(job_id, task_id)["prelabel_status"] == "Failed"


# --------------------------------------------------------------------- SAM

class TestSamPath:
    def test_regions_stored_class_null(self, env):
        """Req 8.2: SAM regions are stored as a class-agnostic pre-label
        (class: null) after a synchronous SAM worker invocation."""
        job_id = env.make_job(task_type="Segmentation",
                              label_set=["scratch"], model="sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        sam = env.use_sam(payload={
            "regions": [{"class": None, "rle": "12 5 3 5", "score": 0.91},
                        {"class": "spurious", "rle": "0 4 8"}],
            "image_width": 100,
            "image_height": 80,
        })

        result = env.run([env.record(job_id, task_id, image_uri,
                                     "Segmentation", ["scratch"], "sam")])

        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"

        prelabel = env.prelabel_json(job_id, task_id)
        assert prelabel["modality"] == "Segmentation"
        assert prelabel["image_width"] == 100
        assert prelabel["image_height"] == 80
        # Every region is class-agnostic regardless of worker output.
        assert [region["class"] for region in prelabel["regions"]] == [None, None]
        assert prelabel["regions"][0]["rle"] == "12 5 3 5"

        # Synchronous invoke of the configured SAM worker with a
        # presigned image URL.
        assert len(sam.invocations) == 1
        invocation = sam.invocations[0]
        assert invocation["FunctionName"] == SAM_FUNCTION
        assert invocation["InvocationType"] == "RequestResponse"
        payload = json.loads(invocation["Payload"])
        assert payload["image_s3_presigned_url"].startswith("https://")
        assert DATASET_BUCKET in payload["image_s3_presigned_url"]

    def test_sam_function_error_marks_failed(self, env):
        job_id = env.make_job(task_type="Segmentation",
                              label_set=["scratch"], model="sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_sam(function_error="onnx session failed")

        env.run([env.record(job_id, task_id, image_uri, "Segmentation",
                            ["scratch"], "sam")])

        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "SAM worker failed" in task["prelabel_error"]


# --------------------------------------------------- skip-verification mode

class TestSkipVerification:
    PROMPTS = {"normal": "The part is pristine.",
               "anomaly": "The part shows a visible defect."}

    def test_counter_decrement_and_review_ready_at_zero(self, env):
        """Req 9.4, 9.5, 9.10: every resolution decrements
        autolabel_pending; review_ready flips at zero; failures carry
        autolabel_error (review-ineligible)."""
        job_id = env.make_job(task_type="Classification",
                              skip_verification=True, autolabel_pending=2,
                              per_label_prompts=self.PROMPTS)
        uri_1 = env.put_image(f"imgs/{uuid.uuid4()}.png")
        uri_2 = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_1 = env.make_task(job_id, uri_1)
        task_2 = env.make_task(job_id, uri_2)
        bedrock, _ = env.use_bedrock(
            replies=['{"label": "anomaly"}', '{"label": "not-a-label"}'])

        record_1 = env.record(job_id, task_1, uri_1, "Classification",
                              ["normal", "anomaly"], "bedrock:test-model-id",
                              per_label_prompts=self.PROMPTS)
        result = env.run([record_1])
        assert result == {"batchItemFailures": []}

        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 1
        assert not job.get("review_ready")

        # Per_Label_Prompts appended to the prompt (Req 9.4).
        prompt = bedrock.calls[0]["messages"][0]["content"][1]["text"]
        assert self.PROMPTS["normal"] in prompt
        assert self.PROMPTS["anomaly"] in prompt

        result = env.run([env.record(
            job_id, task_2, uri_2, "Classification", ["normal", "anomaly"],
            "bedrock:test-model-id", per_label_prompts=self.PROMPTS)])
        assert result == {"batchItemFailures": []}

        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 0
        assert job["review_ready"] is True
        assert int(job["autolabel_completed_count"]) == 2

        assert env.get_task(job_id, task_1)["prelabel_status"] == "Available"
        failed = env.get_task(job_id, task_2)
        assert failed["prelabel_status"] == "Failed"
        # Skip-verification failure is review-ineligible (Req 9.10).
        assert failed["autolabel_error"]
        assert failed["autolabel_error"] == failed["prelabel_error"]

        # Duplicate delivery of an already-resolved task never
        # double-decrements the counter.
        env.run([record_1])
        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 0
        assert int(job["autolabel_completed_count"]) == 2

    def test_team_mode_failure_has_no_autolabel_error(self, env):
        """autolabel_error is a skip-verification-only marker."""
        job_id = env.make_job(task_type="Classification")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_bedrock(replies=['{"label": "bogus"}'])

        env.run([env.record(job_id, task_id, image_uri, "Classification",
                            ["normal", "anomaly"], "bedrock:test-model-id")])

        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert task["prelabel_error"]
        assert "autolabel_error" not in task


# ----------------------------------------------------- per-record isolation

class TestBatchIsolation:
    def test_bad_record_does_not_fail_the_batch(self, env):
        """One malformed record and one generation failure never block
        the remaining records or surface batch failures."""
        job_id = env.make_job(task_type="Classification")
        uri_good = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_good = env.make_task(job_id, uri_good)
        task_bad = env.make_task(
            job_id, f"s3://{DATASET_BUCKET}/missing/{uuid.uuid4()}.png")
        env.use_bedrock(replies=['{"label": "normal"}'])

        records = [
            # Unparseable body: logged and dropped.
            {"messageId": "msg-malformed", "body": "{not json"},
            # Missing image object: generation failure, marked Failed.
            env.record(job_id, task_bad,
                       f"s3://{DATASET_BUCKET}/missing/{uuid.uuid4()}.png",
                       "Classification", ["normal", "anomaly"],
                       "bedrock:test-model-id"),
            # Healthy record processed normally.
            env.record(job_id, task_good, uri_good, "Classification",
                       ["normal", "anomaly"], "bedrock:test-model-id"),
        ]

        result = env.run(records)

        assert result == {"batchItemFailures": []}
        assert env.get_task(job_id, task_good)["prelabel_status"] == "Available"
        failed = env.get_task(job_id, task_bad)
        assert failed["prelabel_status"] == "Failed"
        assert "not accessible" in failed["prelabel_error"]

    def test_unknown_job_is_dropped_without_batch_failure(self, env):
        result = env.run([env.record(
            "labeling-missing", "task-x",
            f"s3://{DATASET_BUCKET}/img.png", "Classification",
            ["normal", "anomaly"], "bedrock:test-model-id")])
        assert result == {"batchItemFailures": []}
