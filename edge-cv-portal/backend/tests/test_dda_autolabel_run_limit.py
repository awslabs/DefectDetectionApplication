"""
dda_autolabel_worker.py — Images per worker run (Settings ->
Auto-Labeling).

The queue hands a run up to 5 records. At the start of every run the
worker reads the saved Images per worker run (`autolabel.images_per_run`
in the portal settings table). With N saved and more than N records it
sends records N+1 onwards back to its own queue as new messages, then
labels the first N plus any record it could not send back.

Covers, against the moto-backed stack from conftest.py (moto DynamoDB,
S3 and SQS, the fake Bedrock and Lambda clients of
test_dda_autolabel_worker.py):

- **Nothing saved** (TestNothingSaved): with no item, no settings table,
  a settings table that does not exist, or a stored value that is not a
  whole number from 1 to 5, a run labels every record of its batch and
  sends nothing back, as before the setting existed. These tests pass on
  the worker without the setting.
- **A saved value** (TestSavedImagesPerRun): with K saved, a run of 5
  records labels the first K and sends the bodies of the others back to
  the queue, unchanged; later runs label the copies, the
  skip-verification counters move once per task, a change applies to
  the next run, and Grounded-SAM records follow the same limit.
- **Records that cannot be sent back** are labeled in the same run: no
  queue URL, an entry SQS reports as failed, a send that raises.
- **The run log line** the deploy check looks for.
- **A Hypothesis property** over the saved value and the record count:
  the first min(K, n) records are processed in order and the rest are
  sent back in order.

Runs as its own module: the module fixture re-imports the worker, sets no
os.environ key, and each test points the worker at this module's
settings table and queue, emptied before and after the test.
"""
import json
import logging
import sys
import uuid
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

from test_dda_autolabel_worker import (
    DATASET_BUCKET,
    SAM_FUNCTION,
    AutolabelEnv,
    FakeSamLambdaClient,
)

REGION = "us-east-1"
ARTIFACTS_BUCKET = "test-portal-artifacts"
GROUNDED_SAM_FUNCTION = "test-dda-grounded-sam-worker"
SETTINGS_TABLE_NAME = "test-settings-autolabel-run-limit"
QUEUE_NAME = "test-autolabel-run-limit-queue"
SETTING_KEY = "autolabel.images_per_run"
LABELS = ["normal", "anomaly"]
BEDROCK_MODEL = "bedrock:test-model-id"
NORMAL = '{"label": "normal"}'


# ------------------------------------------------------------- fake clients

class RecordingRequeueClient:
    """Fake SQS client for the worker's re-queue seam: records every
    send_message_batch call and returns `response`, raises `error`, or by
    default reports every entry successful."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def send_message_batch(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        return {"Successful": [{"Id": entry["Id"]}
                               for entry in kwargs["Entries"]],
                "Failed": []}


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock, with
    this module's settings table and queue created."""
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    # The module read env at import; set the values these tests need on
    # the module itself, so no os.environ key leaks to later modules.
    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    dda_autolabel_worker.GROUNDED_SAM_WORKER_FUNCTION_NAME = (
        GROUNDED_SAM_FUNCTION)
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=SETTINGS_TABLE_NAME,
        KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "setting_key",
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("sqs", region_name=REGION).create_queue(QueueName=QUEUE_NAME)
    return dda_autolabel_worker


class RunLimitEnv(AutolabelEnv):
    """AutolabelEnv plus the settings item, the queue and the re-queue
    client seam."""

    def __init__(self, stack, worker, monkeypatch):
        super().__init__(stack, worker, monkeypatch)
        self.settings_table = boto3.resource(
            "dynamodb", region_name=REGION).Table(SETTINGS_TABLE_NAME)
        self.sqs = boto3.client("sqs", region_name=REGION)
        self.queue_url = self.sqs.get_queue_url(
            QueueName=QUEUE_NAME)["QueueUrl"]

    # ---------------------------------------------------------- setting
    def save(self, value):
        self.settings_table.put_item(Item={
            "setting_key": SETTING_KEY, "value": value,
            "updated_at": 1, "updated_by": "user-test"})

    def clear(self):
        self.settings_table.delete_item(Key={"setting_key": SETTING_KEY})

    # ------------------------------------------------------------ queue
    def drain(self):
        """Receive and delete every message on the queue; their bodies."""
        bodies = []
        while True:
            messages = self.sqs.receive_message(
                QueueUrl=self.queue_url, MaxNumberOfMessages=10,
                WaitTimeSeconds=0).get("Messages", [])
            if not messages:
                return bodies
            for message in messages:
                bodies.append(message["Body"])
                self.sqs.delete_message(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=message["ReceiptHandle"])

    def use_requeue(self, fake):
        self.monkeypatch.setattr(self.worker, "requeue_sqs_client", fake)
        return fake

    # ------------------------------------------------------------ batch
    def make_batch(self, count, job_id=None, **job_kwargs):
        """A Bedrock Classification job (or `job_id`) with `count`
        Pending tasks, and one record per task in task order."""
        if job_id is None:
            job_id = self.make_job(task_type="Classification", **job_kwargs)
        task_ids, records = [], []
        for _ in range(count):
            image_uri = self.put_image(f"imgs/{uuid.uuid4()}.png")
            task_id = self.make_task(job_id, image_uri)
            task_ids.append(task_id)
            records.append(self.record(job_id, task_id, image_uri,
                                       "Classification", LABELS,
                                       BEDROCK_MODEL))
        return job_id, task_ids, records

    def statuses(self, job_id, task_ids):
        return [self.get_task(job_id, task_id)["prelabel_status"]
                for task_id in task_ids]

    @staticmethod
    def as_records(bodies):
        """SQS records for message bodies, as a later run receives them."""
        return [{"messageId": f"msg-{uuid.uuid4().hex[:8]}", "body": body}
                for body in bodies]

    def run_until_empty(self, records, max_runs=10):
        """Run, then run the copies sent back, until the queue is empty.
        Returns the number of runs."""
        runs = 0
        while records:
            assert runs < max_runs, "the queue never emptied"
            assert self.run(records) == {"batchItemFailures": []}
            runs += 1
            records = self.as_records(self.drain())
        return runs


@pytest.fixture
def env(aws_stack, worker, monkeypatch):
    run_env = RunLimitEnv(aws_stack, worker, monkeypatch)
    monkeypatch.setattr(worker, "SETTINGS_TABLE", SETTINGS_TABLE_NAME)
    # raising=False: the attribute is new with the setting, and the
    # nothing-saved tests also run on the worker without it.
    monkeypatch.setattr(worker, "AUTOLABEL_QUEUE_URL", run_env.queue_url,
                        raising=False)
    run_env.clear()
    run_env.drain()
    yield run_env
    run_env.clear()
    run_env.drain()


# -------------------------------------------------------------- nothing saved

class TestNothingSaved:
    """With nothing usable saved a run labels every record of its batch
    and sends nothing back to the queue, as before the setting existed."""

    def _assert_every_record_processed(self, env):
        job_id, task_ids, records = env.make_batch(5)
        bedrock, _ = env.use_bedrock(replies=['{"label": "normal"}'])

        result = env.run(records)

        assert result == {"batchItemFailures": []}
        assert env.statuses(job_id, task_ids) == ["Available"] * 5
        assert len(bedrock.calls) == 5
        assert env.drain() == []

    def test_no_item(self, env):
        self._assert_every_record_processed(env)

    def test_no_settings_table(self, env):
        env.monkeypatch.setattr(env.worker, "SETTINGS_TABLE", None)
        self._assert_every_record_processed(env)

    def test_settings_table_does_not_exist(self, env):
        env.monkeypatch.setattr(env.worker, "SETTINGS_TABLE",
                                f"test-settings-missing-{uuid.uuid4().hex[:8]}")
        self._assert_every_record_processed(env)

    @pytest.mark.parametrize("stored", [
        0, 6, -1, Decimal("2.5"), "3", True, None, {"n": 1}],
        ids=["0", "6", "-1", "2.5", "string-3", "True", "None", "map"])
    def test_stored_value_the_worker_ignores(self, env, stored):
        env.save(stored)
        self._assert_every_record_processed(env)


# ---------------------------------------------------------------- saved limit

class TestSavedImagesPerRun:
    """With K saved, a run labels its first K records and sends the
    others back to the queue for another run."""

    @pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
    def test_first_k_labeled_and_the_rest_sent_back(self, env, limit):
        env.save(limit)
        job_id, task_ids, records = env.make_batch(5)
        bedrock, _ = env.use_bedrock(replies=[NORMAL])

        result = env.run(records)

        assert result == {"batchItemFailures": []}
        assert env.statuses(job_id, task_ids) == (
            ["Available"] * limit + ["Pending"] * (5 - limit))
        assert len(bedrock.calls) == limit
        # The copies carry the original bodies unchanged; K = 5 sends
        # nothing back.
        assert sorted(env.drain()) == sorted(
            record["body"] for record in records[limit:])

    def test_requeued_records_are_labeled_by_later_runs(self, env):
        env.save(2)
        job_id, task_ids, records = env.make_batch(5)
        bedrock, _ = env.use_bedrock(replies=[NORMAL])

        runs = env.run_until_empty(records)

        # 5 records: 2 labeled + 3 sent back, then 2 + 1, then 1.
        assert runs == 3
        assert env.statuses(job_id, task_ids) == ["Available"] * 5
        assert len(bedrock.calls) == 5

    def test_skip_verification_counters_move_once_per_task(self, env):
        env.save(1)
        job_id, task_ids, records = env.make_batch(
            3, skip_verification=True, autolabel_pending=3)
        env.use_bedrock(replies=[NORMAL])

        assert env.run(records) == {"batchItemFailures": []}
        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 2
        assert not job.get("review_ready")

        env.run_until_empty(env.as_records(env.drain()))

        job = env.get_job(job_id)
        assert int(job["autolabel_pending"]) == 0
        assert int(job["autolabel_completed_count"]) == 3
        assert job["review_ready"] is True
        assert env.statuses(job_id, task_ids) == ["Available"] * 3

    def test_a_change_applies_to_the_next_run(self, env):
        env.save(1)
        job_id, first_tasks, first_records = env.make_batch(3)
        env.use_bedrock(replies=[NORMAL])

        env.run(first_records)
        assert env.statuses(job_id, first_tasks) == [
            "Available", "Pending", "Pending"]
        assert len(env.drain()) == 2

        # No value is cached across runs: the next run after the clear
        # labels every record.
        env.clear()
        _, second_tasks, second_records = env.make_batch(3, job_id=job_id)
        env.run(second_records)
        assert env.statuses(job_id, second_tasks) == ["Available"] * 3
        assert env.drain() == []

    def test_grounded_sam_records_follow_the_limit(self, env):
        env.save(2)
        labels = ["scratch", "dent"]
        job_id = env.make_job(task_type="ObjectDetection", label_set=labels,
                              model="grounded-sam")
        task_ids, records = [], []
        for _ in range(3):
            image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
            task_id = env.make_task(job_id, image_uri)
            task_ids.append(task_id)
            records.append(env.record(job_id, task_id, image_uri,
                                      "ObjectDetection", labels,
                                      "grounded-sam"))
        fake = FakeSamLambdaClient(payload={
            "regions": [], "image_width": 100, "image_height": 80})
        env.monkeypatch.setattr(env.worker, "grounded_sam_lambda_client",
                                fake)

        assert env.run(records) == {"batchItemFailures": []}

        assert len(fake.invocations) == 2
        assert env.statuses(job_id, task_ids) == [
            "Available", "Available", "Pending"]
        for task_id in task_ids[:2]:
            assert env.prelabel_json(job_id, task_id)["boxes"] == []
        assert env.drain() == [records[2]["body"]]


# ------------------------------------------------- records not sent back

class TestRecordsNotSentBack:
    """A record the run cannot send back is labeled in the same run."""

    def test_no_queue_url_processes_every_record(self, env):
        env.save(1)
        env.monkeypatch.setattr(env.worker, "AUTOLABEL_QUEUE_URL", "")
        job_id, task_ids, records = env.make_batch(3)
        bedrock, _ = env.use_bedrock(replies=[NORMAL])

        assert env.run(records) == {"batchItemFailures": []}

        assert env.statuses(job_id, task_ids) == ["Available"] * 3
        assert len(bedrock.calls) == 3
        assert env.drain() == []

    def test_failed_entry_is_processed_in_this_run(self, env):
        env.save(1)
        job_id, task_ids, records = env.make_batch(3)
        bedrock, _ = env.use_bedrock(replies=[NORMAL])
        fake = env.use_requeue(RecordingRequeueClient(response={
            "Successful": [{"Id": "0"}],
            "Failed": [{"Id": "1", "Code": "X", "SenderFault": False}],
        }))

        assert env.run(records) == {"batchItemFailures": []}

        # One call with records 2 and 3, numbered from 0 among the
        # records sent back.
        assert fake.calls == [{
            "QueueUrl": env.queue_url,
            "Entries": [{"Id": "0", "MessageBody": records[1]["body"]},
                        {"Id": "1", "MessageBody": records[2]["body"]}],
        }]
        # Record 3 failed to send, so this run labels it; record 2 was
        # sent back and waits for another run.
        assert env.statuses(job_id, task_ids) == [
            "Available", "Pending", "Available"]
        assert len(bedrock.calls) == 2

    def test_send_error_processes_every_record(self, env):
        env.save(1)
        job_id, task_ids, records = env.make_batch(3)
        bedrock, _ = env.use_bedrock(replies=[NORMAL])
        fake = env.use_requeue(RecordingRequeueClient(error=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "SendMessageBatch")))

        assert env.run(records) == {"batchItemFailures": []}

        assert len(fake.calls) == 1
        assert env.statuses(job_id, task_ids) == ["Available"] * 3
        assert len(bedrock.calls) == 3


# ------------------------------------------------------------ run log line

class TestRunLogLine:

    def test_run_log_line(self, env, caplog):
        """The line the deploy check greps for, with and without a saved
        value."""
        caplog.set_level(logging.INFO)
        env.use_bedrock(replies=[NORMAL])

        env.save(1)
        _, _, records = env.make_batch(2)
        env.run(records)
        assert ("Auto-label images per run: 1 (settings); records 2, "
                "processed 1, re-queued 1") in caplog.messages

        env.clear()
        env.drain()
        caplog.clear()
        _, _, records = env.make_batch(2)
        env.run(records)
        assert ("Auto-label images per run: none (default); records 2, "
                "processed 2, re-queued 0") in caplog.messages


# ------------------------------------------------------------------ property

def _valid_body(index):
    """A message _parse_record accepts, with its own task_id."""
    return json.dumps({
        "job_id": "labeling-property",
        "task_id": f"task-{index}",
        "image_s3_uri": f"s3://{DATASET_BUCKET}/imgs/{index}.png",
        "modality": "Classification",
        "label_set": LABELS,
        "model": BEDROCK_MODEL,
    })


@settings(deadline=None)
@given(limit=st.sampled_from([None, 1, 2, 3, 4, 5]),
       count=st.integers(min_value=0, max_value=5))
def test_first_records_processed_and_the_rest_sent_back_in_order(
        worker, limit, count):
    """For a saved K (None: nothing saved) and n records, the run
    processes the first min(K, n) records in order, sends the others
    back in order, and reports no batch item failure."""
    bodies = [_valid_body(index) for index in range(count)]
    records = [{"messageId": f"msg-{index}", "body": body}
               for index, body in enumerate(bodies)]
    setting = (None, "default") if limit is None else (limit, "settings")
    processed = []
    fake = RecordingRequeueClient()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(worker, "_images_per_run", lambda: setting)
        patch.setattr(worker, "_process_message",
                      lambda message: processed.append(message["task_id"]))
        patch.setattr(worker, "requeue_sqs_client", fake)
        patch.setattr(worker, "AUTOLABEL_QUEUE_URL",
                      "https://sqs.us-east-1.amazonaws.com/123456789012/q")
        result = worker.handler({"Records": records}, None)

    kept = count if limit is None else min(limit, count)
    assert result == {"batchItemFailures": []}
    assert processed == [f"task-{index}" for index in range(kept)]
    sent_back = [entry["MessageBody"]
                 for call in fake.calls for entry in call["Entries"]]
    assert sent_back == bodies[kept:]
