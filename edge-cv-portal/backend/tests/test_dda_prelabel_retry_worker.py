"""
dda_labeling_worker.py retry_prelabels action — example tests.

Feature: grounded-sam-prompt-guardrails-and-prelabel-retry, task 2.3.

Covers, against the moto-backed stack from conftest.py (real
shared_utils, moto DynamoDB + S3, fake SQS / Lambda clients injected
through the module seams — the test_dda_labeling_worker_distribute.py
worker conventions plus the test_dda_grounded_sam_consumer.py consumer
scaffolding), invoking the worker handler with
{action: 'retry_prelabels', job_id}:

- **Partial send_message_batch failure** (Req 6.6): a batch response
  reporting some entries Failed logs each failed entry loudly and the
  remaining batches still proceed; enqueued_count reflects only the
  successes and every reset task stays Pending (the platform's
  lost-fan-out posture).
- **Retry-then-consume wiring** (Req 6.7): after the route's
  persist-before-trigger updates a grounded-sam job's
  auto_label.prompt_overrides, the retry re-enqueues the Failed task
  and the *unmodified* dda_autolabel_worker consumer processes the
  captured message with the job record's then-current overrides — the
  grounded-sam worker invoke payload carries the UPDATED prompt text
  and the task resolves Available.
- **Skipped invocations** (Req 6.8): a Stopped / review-finalized /
  auto-label-off job records the invocation skipped with zero task
  writes and zero enqueues; an eligible job with zero Failed tasks
  resets nothing, enqueues nothing, and moves no counter.

Requirements: 6.6, 6.7, 6.8
"""
import json
import logging
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest

from test_dda_autolabel_worker import FakeSamLambdaClient, png_bytes

REGION = "us-east-1"
DATASET_BUCKET = "test-prelabel-retry-data"
ARTIFACTS_BUCKET = "test-portal-artifacts"
GROUNDED_SAM_FUNCTION = "test-dda-grounded-sam-worker"
# _send_autolabel_fanout only reads the env var; with a fake SQS client
# the queue itself never has to exist.
QUEUE_URL = ("https://sqs.us-east-1.amazonaws.com/123456789012/"
             "test-prelabel-retry-queue")


# ------------------------------------------------------------- fake clients

class FakeSqsClient:
    """Records send_message_batch calls; reports the configured entry
    Ids as Failed (the SQS partial-batch-failure response shape),
    everything else Successful."""

    def __init__(self, fail_ids=None):
        self.batches = []
        self.fail_ids = set(fail_ids or [])

    def send_message_batch(self, QueueUrl=None, Entries=None):
        self.batches.append({"QueueUrl": QueueUrl,
                             "Entries": list(Entries)})
        failed = [{"Id": entry["Id"], "SenderFault": False,
                   "Code": "InternalError", "Message": "injected failure"}
                  for entry in Entries if entry["Id"] in self.fail_ids]
        successful = [{"Id": entry["Id"],
                       "MessageId": f"m-{entry['Id']}",
                       "MD5OfMessageBody": "0" * 32}
                      for entry in Entries
                      if entry["Id"] not in self.fail_ids]
        return {"Successful": successful, "Failed": failed}


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def workers(aws_stack):
    """The real dda_labeling_worker (retry action under test) and the
    unmodified dda_autolabel_worker consumer (the Req 6.7 wiring
    example) imported inside the moto mock, with the grounded-sam
    worker function name configured (the
    test_dda_grounded_sam_consumer.py convention)."""
    os.environ["GROUNDED_SAM_WORKER_FUNCTION_NAME"] = GROUNDED_SAM_FUNCTION
    for name in ("dda_labeling", "dda_labeling_worker",
                 "dda_autolabel_worker"):
        sys.modules.pop(name, None)
    import dda_labeling_worker
    import dda_autolabel_worker

    # The consumer read env at import; make sure the test values stuck.
    dda_autolabel_worker.GROUNDED_SAM_WORKER_FUNCTION_NAME = (
        GROUNDED_SAM_FUNCTION)
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return SimpleNamespace(labeling=dda_labeling_worker,
                           autolabel=dda_autolabel_worker)


@pytest.fixture
def env(aws_stack, workers, monkeypatch):
    monkeypatch.delenv("AUTOLABEL_QUEUE_URL", raising=False)
    return RetryEnv(aws_stack, workers, monkeypatch)


class RetryEnv:
    """Per-test facade: fresh Use_Case, directly seeded job / task
    records (the AutolabelEnv convention — the retry acts on stored
    items, never re-enumerating the dataset), and the fake-client
    injection seams."""

    def __init__(self, stack, workers, monkeypatch):
        self.stack = stack
        self.worker = workers.labeling
        self.consumer = workers.autolabel
        self.monkeypatch = monkeypatch
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Prelabel Retry Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })

    # ------------------------------------------------------------ setup
    def make_job(self, model="grounded-sam", task_type="Segmentation",
                 label_set=None, status="InProgress",
                 auto_label_enabled=True, skip_verification=False,
                 prompt_overrides=None, review_finalized=None, **extra):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        auto_label = {"enabled": auto_label_enabled, "model": model}
        if prompt_overrides is not None:
            auto_label["prompt_overrides"] = dict(prompt_overrides)
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": "DDA",
            "status": status,
            "task_type": task_type,
            "label_set": label_set or ["cookie_gap"],
            "skip_verification": skip_verification,
            "auto_label": auto_label,
            "created_at": 1,
            "updated_at": 1,
        }
        if review_finalized is not None:
            item["review_finalized"] = review_finalized
        item.update(extra)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def make_task(self, job_id, index=0, prelabel_status="Failed",
                  prelabel_error=None):
        task_id = f"task-{index:06d}"
        image_key = f"{job_id}/img-{index:03d}.png"
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=image_key,
                           Body=png_bytes(100, 80))
        item = {
            "job_id": job_id,
            "task_id": task_id,
            "usecase_id": self.usecase_id,
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
            "image_key": image_key,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "prelabel_status": prelabel_status,
            "created_at": 1,
        }
        if prelabel_error is not None:
            item["prelabel_error"] = prelabel_error
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return task_id

    def use_fake_sqs(self, fail_ids=None):
        fake = FakeSqsClient(fail_ids=fail_ids)
        self.monkeypatch.setattr(self.worker, "sqs_client", fake)
        self.monkeypatch.setenv("AUTOLABEL_QUEUE_URL", QUEUE_URL)
        return fake

    def use_grounded_sam(self, payload=None):
        fake = FakeSamLambdaClient(payload=payload)
        self.monkeypatch.setattr(self.consumer,
                                 "grounded_sam_lambda_client", fake)
        return fake

    # ------------------------------------------------------------ invoke
    def retry(self, job_id):
        return self.worker.handler(
            {"action": "retry_prelabels", "job_id": job_id}, None)

    def consume(self, message_body):
        return self.consumer.handler({"Records": [
            {"messageId": f"msg-{uuid.uuid4().hex[:8]}",
             "body": message_body}]}, None)

    # ------------------------------------------------------------- store
    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")


# ----------------------------------------------- partial enqueue failure

class TestPartialEnqueueFailure:
    """Req 6.6: a batch of retry messages partially failing to enqueue
    logs each failed entry and continues with the remaining batches —
    the distributor's enqueue-failure posture."""

    def test_failed_entries_logged_and_remaining_batches_proceed(
            self, env, caplog):
        job_id = env.make_job()
        task_ids = [env.make_task(job_id, index=i,
                                  prelabel_error="Grounded-SAM worker "
                                                 "failed: boom")
                    for i in range(12)]
        # Entry Ids are assigned in reset (task_id) order: "1" and "3"
        # both land in the first SQS batch (SQS_BATCH_SIZE = 10).
        fake = env.use_fake_sqs(fail_ids={"1", "3"})

        with caplog.at_level(logging.ERROR):
            result = env.retry(job_id)

        # Every Failed task was reset; the count reflects only the
        # entries the batch responses reported successful.
        assert result["reset_count"] == 12
        assert result["enqueued_count"] == 10

        # The first batch's partial failure never blocked the second:
        # both batches reached SQS with the full entry split.
        assert [len(batch["Entries"]) for batch in fake.batches] == [10, 2]
        assert all(batch["QueueUrl"] == QUEUE_URL
                   for batch in fake.batches)

        # Each failed entry is logged loudly with the job id.
        assert "failed to enqueue" in caplog.text
        assert job_id in caplog.text
        assert "'Id': '1'" in caplog.text
        assert "'Id': '3'" in caplog.text

        # Reset tasks stay Pending regardless of enqueue outcome (the
        # platform's lost-fan-out posture — loudly logged, re-runnable).
        for task_id in task_ids:
            task = env.get_task(job_id, task_id)
            assert task["prelabel_status"] == "Pending"
            assert "prelabel_error" not in task


# --------------------------------------------- retry-then-consume wiring

class TestRetryThenConsumeWiring:
    """Req 6.7: a retried task's message rides the existing unmodified
    consumer path, with the job record's then-current prompt_overrides
    applied — updated overrides take effect with zero consumer change."""

    BROKEN_OVERRIDE = ("draw and fill in the gaps within the bounds of "
                       "the image. If there is a large crack, fill it in.")
    CORRECTED_OVERRIDE = "gap between broken cookie pieces"
    INCIDENT_ERROR = ('Grounded-SAM worker failed: {"errorMessage": '
                      '"caption token spans (2) do not align with the 1 '
                      'prompts; a prompt likely contains inner sentence '
                      'punctuation", "errorType": "ValueError"}')

    def test_reenqueued_message_consumes_with_updated_overrides(self, env):
        job_id = env.make_job(
            prompt_overrides={"cookie_gap": self.BROKEN_OVERRIDE})
        task_id = env.make_task(job_id,
                                prelabel_error=self.INCIDENT_ERROR)

        # Simulate the route's persist-before-trigger (Req 5.5): the
        # corrected override lands on the job record before the worker
        # action runs.
        env.stack.tables.labeling_jobs.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET auto_label.prompt_overrides = :o",
            ExpressionAttributeValues={
                ":o": {"cookie_gap": self.CORRECTED_OVERRIDE}})

        fake_sqs = env.use_fake_sqs()
        result = env.retry(job_id)
        assert result == {"job_id": job_id, "action": "retry_prelabels",
                          "reset_count": 1, "enqueued_count": 1}

        # The reset flipped the task to Pending with the error removed.
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Pending"
        assert "prelabel_error" not in task

        # Exactly one captured fan-out message, built from the stored
        # task item.
        assert len(fake_sqs.batches) == 1
        [entry] = fake_sqs.batches[0]["Entries"]
        message = json.loads(entry["MessageBody"])
        assert message["task_id"] == task_id
        assert message["model"] == "grounded-sam"
        assert message["image_s3_uri"] == task["image_s3_uri"]

        # Feed the captured message through the unmodified consumer
        # with a fake grounded-sam worker returning empty regions (an
        # empty result is a success — the task resolves Available).
        fake_gsam = env.use_grounded_sam(payload={
            "regions": [], "image_width": 100, "image_height": 80})
        consume_result = env.consume(entry["MessageBody"])
        assert consume_result == {"batchItemFailures": []}

        # The worker invoke carried the UPDATED override text — the
        # consumer read prompt_overrides from the job record, not from
        # anything frozen at creation or ridden on the message.
        assert len(fake_gsam.invocations) == 1
        invocation = fake_gsam.invocations[0]
        assert invocation["FunctionName"] == GROUNDED_SAM_FUNCTION
        payload = json.loads(invocation["Payload"])
        assert payload["prompts"] == [
            {"label": "cookie_gap", "prompt": self.CORRECTED_OVERRIDE}]
        assert self.BROKEN_OVERRIDE not in json.dumps(payload)

        # The retried task resolved Available with its pre-label stored
        # and no residual error attribute.
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        assert task["prelabel_s3_key"] == (
            f"labeling/{env.usecase_id}/{job_id}/prelabels/{task_id}.json")
        assert "prelabel_error" not in task


# ------------------------------------------------------ skipped invocations

class TestSkippedInvocations:
    """Req 6.8: the Retry_Action invoked for a job that is not a
    Retry_Eligible_Job resets no task, enqueues no message, and records
    the invocation as skipped (a stop/finalize may race the async
    invoke — the distribute status-guard pattern)."""

    @pytest.mark.parametrize("job_kwargs, reason_fragment", [
        ({"status": "Stopped"}, "job is Stopped"),
        ({"review_finalized": True}, "review has been finalized"),
        ({"auto_label_enabled": False}, "auto-labeling is not enabled"),
    ])
    def test_ineligible_job_skipped_with_zero_writes(
            self, env, job_kwargs, reason_fragment):
        job_id = env.make_job(**job_kwargs)
        task_id = env.make_task(job_id, prelabel_error="boom")
        job_before = env.get_job(job_id)
        task_before = env.get_task(job_id, task_id)
        fake = env.use_fake_sqs()

        result = env.retry(job_id)

        assert result["skipped"] is True
        assert reason_fragment in result["reason"]
        # Zero task writes: the Failed task is attribute-for-attribute
        # untouched, as is the job record.
        assert env.get_task(job_id, task_id) == task_before
        assert env.get_job(job_id) == job_before
        # Zero enqueues.
        assert fake.batches == []

    def test_zero_failed_tasks_resets_and_enqueues_nothing(self, env):
        """An eligible job with zero Failed tasks: reset_count 0,
        enqueued_count 0, and no skip-verification counter movement."""
        job_id = env.make_job(skip_verification=True,
                              autolabel_pending=0,
                              autolabel_completed_count=5,
                              review_ready=True)
        task_id = env.make_task(job_id, prelabel_status="Available")
        job_before = env.get_job(job_id)
        fake = env.use_fake_sqs()

        result = env.retry(job_id)

        assert "skipped" not in result
        assert result["reset_count"] == 0
        assert result["enqueued_count"] == 0
        assert fake.batches == []

        # No counter movement: autolabel_pending, autolabel_completed_
        # count, review_ready, and updated_at are all untouched.
        assert env.get_job(job_id) == job_before
        assert env.get_task(job_id, task_id)["prelabel_status"] == (
            "Available")
