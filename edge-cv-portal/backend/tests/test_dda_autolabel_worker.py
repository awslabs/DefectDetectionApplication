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

Feature: llm-auto-labeling (task 9.2) — the `llm:<id>` path:

- Dispatch reaches _generate_llm_prelabel for `llm:<id>`; an empty
  identifier (`llm:`) stays an unsupported model (Req 3.1)
- Exactly one Converse call per image carrying the image bytes, the
  key-derived format, the verbatim Detection_Prompt, every Label_Set
  name, and the pixel dimensions (Req 3.1, 9.5)
- Undeterminable dimensions fail the task before any model call
  (Req 3.3); ReadTimeoutError vs generic errors give distinguishable
  reasons (Req 3.4)
- Unparseable output, out-of-Label_Set class, out-of-bounds box, and
  101 detections each mark Failed with one reason and no pre-label
  object (Req 4.2, 4.4, 4.5, 4.7)
- Success paths for all three modalities write the pre-label and mark
  Available with prelabel_s3_key; a valid empty result is a success
  with empty regions/boxes (Req 5.5, 6.1, 6.3)
- An image whose S3 read fails marks Failed with the access reason
  (Req 9.6)

Feature: llm-auto-labeling (task 10.2) — storage-failure scoping:

- An LLM job whose artifacts put_object raises marks the task Failed
  with the storage reason, sets no prelabel_s3_key, and reports no
  batch item failure (Req 6.2)
- SAM and Bedrock jobs whose put_object raises still surface a batch
  item failure and leave prelabel_status Pending (Req 1.7)
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


def jpeg_bytes(width, height):
    """A minimal JPEG (SOI + SOF0) carrying the given dimensions,
    enough for header-based dimension parsing."""
    return (b"\xff\xd8\xff\xc0" + struct.pack(">H", 11) + b"\x08"
            + struct.pack(">HH", height, width))


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
    def put_image(self, key, width=100, height=80, body=None):
        self.s3.put_object(
            Bucket=DATASET_BUCKET, Key=key,
            Body=png_bytes(width, height) if body is None else body)
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
               per_label_prompts=None, body_override=None,
               detection_prompt=None):
        body = body_override if body_override is not None else json.dumps({
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": image_uri,
            "modality": modality,
            "label_set": label_set,
            "model": model,
            **({"per_label_prompts": per_label_prompts}
               if per_label_prompts else {}),
            **({"detection_prompt": detection_prompt}
               if detection_prompt is not None else {}),
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


# ------------------------------------------------------- LLM guidance path

class TestLlmPath:
    """llm-auto-labeling task 9.2: the `llm:<id>` dispatch branch and
    _generate_llm_prelabel (Req 3.1, 3.3, 3.4, 3.6, 4.2, 4.4, 4.5, 4.7,
    5.5, 6.1, 6.3, 9.5, 9.6)."""

    MODEL = "llm:us.amazon.nova-pro-v1:0"
    MODEL_ID = "us.amazon.nova-pro-v1:0"
    LABELS = ["scratch", "dent"]
    # Deliberately awkward: leading/trailing whitespace, quotes, braces,
    # and an embedded newline must survive verbatim (Req 3.1).
    PROMPT = '  Find every "scratch" {and dent}\n  on the panel.  '

    @staticmethod
    def guidance(detections):
        return json.dumps({"detections": detections})

    @staticmethod
    def box(cls="scratch", left=10, top=5, width=30, height=20):
        return {"class": cls,
                "box": {"left": left, "top": top,
                        "width": width, "height": height}}

    def _run_llm(self, env, modality, reply=None, error=None,
                 label_set=None, image_body=None, image_key=None,
                 image_uri=None, model=None):
        """One llm-model job + task + record through the handler."""
        label_set = label_set if label_set is not None else self.LABELS
        model = model or self.MODEL
        job_id = env.make_job(task_type=modality, label_set=label_set,
                              model=model)
        if image_uri is None:
            key = image_key or f"imgs/{uuid.uuid4()}.png"
            image_uri = env.put_image(key, width=100, height=80,
                                      body=image_body)
        task_id = env.make_task(job_id, image_uri)
        bedrock, recorded = env.use_bedrock(
            replies=[reply] if reply is not None else None, error=error)
        result = env.run([env.record(job_id, task_id, image_uri, modality,
                                     label_set, model,
                                     detection_prompt=self.PROMPT)])
        return SimpleNamespace(job_id=job_id, task_id=task_id,
                               bedrock=bedrock, recorded=recorded,
                               result=result)

    # ------------------------------------------------------------ dispatch

    def test_empty_identifier_is_unsupported_model(self, env):
        """Req 3.1: `llm:` with an empty identifier never reaches the
        LLM path — the task fails as an unsupported model with zero
        converse calls."""
        run = self._run_llm(env, "ObjectDetection", model="llm:")
        assert run.result == {"batchItemFailures": []}
        task = env.get_task(run.job_id, run.task_id)
        assert task["prelabel_status"] == "Failed"
        assert "unsupported auto-label model" in task["prelabel_error"]
        assert len(run.bedrock.calls) == 0
        assert not env.prelabel_exists(run.job_id, run.task_id)

    def test_single_converse_call_carries_image_prompt_and_dimensions(self, env):
        """Req 3.1, 9.5: exactly one converse call per image, with the
        image bytes, the key-derived format, the verbatim
        Detection_Prompt, every Label_Set name, and the pixel
        dimensions; the model id keeps its embedded colon."""
        run = self._run_llm(env, "ObjectDetection",
                            reply=self.guidance([self.box()]))
        assert run.result == {"batchItemFailures": []}
        assert len(run.bedrock.calls) == 1
        call = run.bedrock.calls[0]
        assert call["modelId"] == self.MODEL_ID
        content = call["messages"][0]["content"]
        image_block = content[0]["image"]
        assert image_block["format"] == "png"
        assert image_block["source"]["bytes"].startswith(b"\x89PNG")
        prompt = content[1]["text"]
        assert self.PROMPT in prompt          # verbatim, unaltered
        for label in self.LABELS:
            assert label in prompt
        assert "100 pixels wide" in prompt
        assert "80 pixels tall" in prompt

    def test_jpeg_key_derives_jpeg_format(self, env):
        """Req 3.1: the image format sent to the model is derived from
        the object key."""
        run = self._run_llm(env, "ObjectDetection",
                            reply=self.guidance([self.box()]),
                            image_key=f"imgs/{uuid.uuid4()}.jpg",
                            image_body=jpeg_bytes(100, 80))
        assert len(run.bedrock.calls) == 1
        image_block = run.bedrock.calls[0]["messages"][0]["content"][0]["image"]
        assert image_block["format"] == "jpeg"
        assert image_block["source"]["bytes"].startswith(b"\xff\xd8")
        assert env.get_task(run.job_id,
                            run.task_id)["prelabel_status"] == "Available"

    # ------------------------------------------------- pre-invocation gates

    def test_undeterminable_dimensions_fail_without_model_call(self, env):
        """Req 3.3: a body that is neither PNG nor JPEG fails with the
        unsupported-content reason and makes zero converse calls."""
        run = self._run_llm(env, "ObjectDetection",
                            image_body=b"definitely not an image")
        assert run.result == {"batchItemFailures": []}
        task = env.get_task(run.job_id, run.task_id)
        assert task["prelabel_status"] == "Failed"
        assert "unsupported image content" in task["prelabel_error"]
        assert len(run.bedrock.calls) == 0
        assert not env.prelabel_exists(run.job_id, run.task_id)

    def test_unreadable_image_marks_failed_with_access_reason(self, env):
        """Req 9.6: an image whose S3 read fails marks the task Failed
        with the access reason."""
        missing_uri = f"s3://{DATASET_BUCKET}/missing/{uuid.uuid4()}.png"
        run = self._run_llm(env, "ObjectDetection", image_uri=missing_uri)
        task = env.get_task(run.job_id, run.task_id)
        assert task["prelabel_status"] == "Failed"
        assert "not accessible" in task["prelabel_error"]
        assert len(run.bedrock.calls) == 0

    # --------------------------------------------------- invocation failure

    def test_read_timeout_and_model_error_reasons_are_distinct(self, env):
        """Req 3.4: a ReadTimeoutError yields a timeout reason and a
        generic exception a model-error reason, with distinguishable
        substrings."""
        from botocore.exceptions import ReadTimeoutError

        timeout_run = self._run_llm(
            env, "ObjectDetection",
            error=ReadTimeoutError(endpoint_url="https://bedrock.test"))
        timeout_reason = env.get_task(
            timeout_run.job_id, timeout_run.task_id)["prelabel_error"]
        assert "timed out" in timeout_reason
        assert len(timeout_run.bedrock.calls) == 1

        error_run = self._run_llm(env, "ObjectDetection",
                                  error=RuntimeError("kaboom"))
        error_reason = env.get_task(
            error_run.job_id, error_run.task_id)["prelabel_error"]
        assert "model error" in error_reason
        assert "kaboom" in error_reason

        # The two reasons are distinguishable substrings.
        assert "timed out" not in error_reason
        assert "model error" not in timeout_reason
        for run in (timeout_run, error_run):
            assert env.get_task(run.job_id,
                                run.task_id)["prelabel_status"] == "Failed"
            assert not env.prelabel_exists(run.job_id, run.task_id)

    # ------------------------------------------------------ guidance gates

    def _assert_failed(self, env, run, reason_substring):
        assert run.result == {"batchItemFailures": []}
        task = env.get_task(run.job_id, run.task_id)
        assert task["prelabel_status"] == "Failed"
        assert reason_substring in task["prelabel_error"]
        assert "prelabel_s3_key" not in task
        assert not env.prelabel_exists(run.job_id, run.task_id)

    def test_unparseable_output_marks_failed(self, env):
        """Req 4.2: no parseable JSON in the response is one failure."""
        run = self._run_llm(env, "ObjectDetection",
                            reply="I could not find anything to report.")
        self._assert_failed(env, run, "parseable JSON")

    def test_class_outside_label_set_marks_failed(self, env):
        """Req 4.4: a class not in the Label_Set fails the document."""
        run = self._run_llm(env, "ObjectDetection",
                            reply=self.guidance([self.box(cls="crack")]))
        self._assert_failed(env, run, "crack")

    def test_out_of_bounds_box_marks_failed(self, env):
        """Req 4.5: a box overflowing the 100x80 frame fails."""
        run = self._run_llm(env, "ObjectDetection",
                            reply=self.guidance(
                                [self.box(left=90, width=30)]))  # 90+30 > 100
        self._assert_failed(env, run, "bounds")

    def test_101_detections_mark_failed_with_cap_reason(self, env):
        """Req 4.7: more than 100 detections rejects the document with
        the cap reason."""
        run = self._run_llm(env, "ObjectDetection",
                            reply=self.guidance([self.box()] * 101))
        self._assert_failed(env, run, "at most 100")

    # ------------------------------------------------------- success paths

    def _assert_available(self, env, run):
        assert run.result == {"batchItemFailures": []}
        task = env.get_task(run.job_id, run.task_id)
        assert task["prelabel_status"] == "Available"
        assert task["prelabel_s3_key"] == (
            f"labeling/{env.usecase_id}/{run.job_id}/prelabels/"
            f"{run.task_id}.json")
        return env.prelabel_json(run.job_id, run.task_id)

    def test_segmentation_success_writes_rle_prelabel(self, env):
        """Req 5.1, 6.1: a Segmentation guidance detection becomes one
        RLE region in the stored pre-label."""
        run = self._run_llm(env, "Segmentation",
                            reply=self.guidance([self.box()]))
        prelabel = self._assert_available(env, run)
        assert prelabel["modality"] == "Segmentation"
        assert prelabel["image_width"] == 100
        assert prelabel["image_height"] == 80
        assert len(prelabel["regions"]) == 1
        region = prelabel["regions"][0]
        assert region["class"] == "scratch"
        assert isinstance(region["rle"], str) and region["rle"]

    def test_object_detection_success_keeps_coordinates(self, env):
        """Req 5.3, 6.1: box detections keep their validated
        coordinates verbatim in the stored pre-label."""
        run = self._run_llm(env, "ObjectDetection",
                            reply=self.guidance(
                                [self.box(left=10, top=5,
                                          width=30, height=20)]))
        prelabel = self._assert_available(env, run)
        assert prelabel["modality"] == "ObjectDetection"
        assert prelabel["image_width"] == 100
        assert prelabel["image_height"] == 80
        assert prelabel["boxes"] == [
            {"class": "scratch", "left": 10.0, "top": 5.0,
             "width": 30.0, "height": 20.0}]

    def test_classification_success_derives_label(self, env):
        """Req 5.4, 6.1: one or more detections mean the anomaly class."""
        run = self._run_llm(env, "Classification",
                            label_set=["normal", "anomaly"],
                            reply=self.guidance([self.box(cls="anomaly")]))
        prelabel = self._assert_available(env, run)
        assert prelabel == {"modality": "Classification", "label": "anomaly"}

    def test_empty_guidance_is_success_with_empty_lists(self, env):
        """Req 5.5: a valid empty result is a success — an empty
        regions/boxes list, not a failure."""
        seg = self._run_llm(env, "Segmentation", reply=self.guidance([]))
        seg_prelabel = self._assert_available(env, seg)
        assert seg_prelabel["regions"] == []

        det = self._run_llm(env, "ObjectDetection", reply=self.guidance([]))
        det_prelabel = self._assert_available(env, det)
        assert det_prelabel["boxes"] == []


# ------------------------------------------- storage-failure scoping (10.2)

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


class TestStorageFailureScoping:
    """llm-auto-labeling task 10.2: a _write_prelabel put_object failure
    is terminal for the LLM family (task Failed with the storage
    reason, no batch item failure) but stays transient for sam and
    bedrock: (batch item failure, task still Pending) (Req 6.2, 1.7)."""

    LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
    STORAGE_EXC = RuntimeError("S3 outage: put_object refused")

    def _break_put_object(self, env):
        stub = _RaisingPutObjectS3(self.STORAGE_EXC)
        env.monkeypatch.setattr(env.worker, "s3_client", stub)
        return stub

    def test_llm_storage_failure_is_terminal(self, env):
        """Req 6.2: an LLM job whose put_object raises marks the task
        Failed with the storage reason, sets no prelabel_s3_key, and
        reports no batch item failure."""
        job_id = env.make_job(task_type="ObjectDetection",
                              label_set=["scratch"], model=self.LLM_MODEL)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_bedrock(replies=[json.dumps({"detections": [
            {"class": "scratch",
             "box": {"left": 10, "top": 5, "width": 30, "height": 20}}]})])
        stub = self._break_put_object(env)

        result = env.run([env.record(
            job_id, task_id, image_uri, "ObjectDetection", ["scratch"],
            self.LLM_MODEL, detection_prompt="Find every scratch.")])

        assert result == {"batchItemFailures": []}
        assert stub.calls == 1
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "pre-label storage failed" in task["prelabel_error"]
        assert "put_object refused" in task["prelabel_error"]
        assert "prelabel_s3_key" not in task

    def test_sam_storage_failure_stays_transient(self, env):
        """Req 1.7: a SAM job whose put_object raises surfaces a batch
        item failure and leaves prelabel_status Pending."""
        job_id = env.make_job(task_type="Segmentation",
                              label_set=["scratch"], model="sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_sam(payload={
            "regions": [{"class": None, "rle": "12 5 3 5"}],
            "image_width": 100,
            "image_height": 80,
        })
        stub = self._break_put_object(env)

        record = env.record(job_id, task_id, image_uri, "Segmentation",
                            ["scratch"], "sam")
        result = env.run([record])

        assert result == {"batchItemFailures": [
            {"itemIdentifier": record["messageId"]}]}
        assert stub.calls == 1
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Pending"
        assert "prelabel_error" not in task
        assert "prelabel_s3_key" not in task

    def test_bedrock_storage_failure_stays_transient(self, env):
        """Req 1.7: a Bedrock job whose put_object raises surfaces a
        batch item failure and leaves prelabel_status Pending."""
        job_id = env.make_job(task_type="Classification")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        env.use_bedrock(replies=['{"label": "anomaly"}'])
        stub = self._break_put_object(env)

        record = env.record(job_id, task_id, image_uri, "Classification",
                            ["normal", "anomaly"], "bedrock:test-model-id")
        result = env.run([record])

        assert result == {"batchItemFailures": [
            {"itemIdentifier": record["messageId"]}]}
        assert stub.calls == 1
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Pending"
        assert "prelabel_error" not in task
        assert "prelabel_s3_key" not in task
