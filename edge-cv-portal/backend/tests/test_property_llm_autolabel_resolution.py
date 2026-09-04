"""
Resolution idempotence and failure isolation for the LLM auto-label
path (llm-auto-labeling, task 17.1).

Property tests exercising the real dda_autolabel_worker handler
(_process_message -> _mark_task -> _resolve_skip_verification_counters)
against the moto-backed stack from conftest.py, with a fake Bedrock
client supplying per-record Coordinate_Guidance replies:

- **Feature: llm-auto-labeling, Property 13: Resolution idempotence**
  **Validates: Requirements 6.4, 6.6**
  A generated sequence of redeliveries of the same task's message,
  interleaving success and failure outcomes, leaves the final
  prelabel_status / prelabel_error / prelabel_s3_key exactly those of
  the first resolution, advances autolabel_completed_count exactly
  once, and decrements autolabel_pending exactly once.

- **Feature: llm-auto-labeling, Property 15: Failure isolation**
  **Validates: Requirements 3.5**
  A generated batch with a per-record outcome vector resolves each
  task according to its own outcome, independent of the others, and
  review_ready flips exactly when the resolved count reaches the
  job's image count.

Task 17.2 (Req 10.5) adds the example-based all-images-fail cases,
end to end through the real worker and then the real dda_labeling
APIs: a job where every image genuinely fails generation never
transitions to a failed/terminal state; a team job serves every
Failed task to the labeler for annotation from scratch through the
next-task gating; a skip-verification job becomes review-ready with
every image Failed and finalize answers 400 for zero accepted
results.

Fixture pattern: hypothesis reuses function-scoped fixtures across
examples, so the moto harness is module-scoped and every example does
its own setup with fresh uuid-based job/task ids — examples never
interfere through the shared tables.
"""
import json
import sys
import uuid

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from test_dda_autolabel_worker import FakeBedrockClient, png_bytes

REGION = "us-east-1"
DATASET_BUCKET = "test-autolabel-data"

MODEL = "llm:us.amazon.nova-pro-v1:0"
LABELS = ["scratch", "dent"]
DETECTION_PROMPT = "Find every scratch and dent on the panel."

# A reply whose guidance parses and validates -> prelabel Available.
SUCCESS_REPLY = json.dumps({"detections": [
    {"class": "scratch",
     "box": {"left": 10, "top": 5, "width": 30, "height": 20}},
]})
# A reply with no JSON object at all -> GuidanceError -> Failed.
FAILURE_REPLY = "I inspected the image but cannot answer as requested."
FAILURE_REASON_SUBSTRING = "parseable JSON"


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


class ResolutionEnv:
    """Module-scoped harness: one Use_Case, per-example jobs/tasks."""

    def __init__(self, stack, worker):
        self.stack = stack
        self.worker = worker
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Resolution Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })

    # ------------------------------------------------------------ setup
    def put_image(self):
        key = f"imgs/{uuid.uuid4()}.png"
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=key,
                           Body=png_bytes(100, 80))
        return f"s3://{DATASET_BUCKET}/{key}"

    def make_job(self, autolabel_pending=None, skip_verification=True,
                 team_id=None):
        """By default a skip-verification LLM job so the completion
        counter and review_ready transitions (Req 6.6) are observable;
        skip_verification=False seeds a team job (no counter, tasks
        assigned to labelers)."""
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "task_type": "ObjectDetection",
            "label_set": LABELS,
            "skip_verification": skip_verification,
            "auto_label": {"enabled": True, "model": MODEL,
                           "detection_prompt": DETECTION_PROMPT},
            "created_at": 1,
            "updated_at": 1,
        }
        if autolabel_pending is not None:
            item["autolabel_pending"] = autolabel_pending
        if team_id is not None:
            item["team_id"] = team_id
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def make_task(self, job_id, image_uri, assignee="AUTO"):
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        self.stack.tables.labeling_tasks.put_item(Item={
            "job_id": job_id,
            "task_id": task_id,
            "usecase_id": self.usecase_id,
            "image_s3_uri": image_uri,
            "assignee_user_id": assignee,
            "status": "Assigned",
            "prelabel_status": "Pending",
        })
        return task_id

    def use_replies(self, replies):
        """Point the worker at a fake Bedrock client answering the
        given replies in converse-call order."""
        fake = FakeBedrockClient(replies=list(replies))
        self.worker.get_bedrock_client = (
            lambda region, timeout_seconds: fake)
        return fake

    # ------------------------------------------------------------ invoke
    def record(self, job_id, task_id, image_uri):
        return {
            "messageId": f"msg-{uuid.uuid4().hex[:8]}",
            "body": json.dumps({
                "job_id": job_id,
                "task_id": task_id,
                "image_s3_uri": image_uri,
                "modality": "ObjectDetection",
                "label_set": LABELS,
                "model": MODEL,
                "detection_prompt": DETECTION_PROMPT,
            }),
        }

    def run(self, records):
        return self.worker.handler({"Records": records}, None)

    # ------------------------------------------------------------- store
    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")


@pytest.fixture(scope="module")
def henv(aws_stack, worker):
    original = worker.get_bedrock_client
    yield ResolutionEnv(aws_stack, worker)
    worker.get_bedrock_client = original


@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, for
    the labeler next-task and admin finalize routes (task 17.2)."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling
    return dda_labeling


def _assert_matches_outcome(task, outcome):
    """A task's resolution reflects its own generation outcome."""
    if outcome:
        assert task["prelabel_status"] == "Available"
        assert task.get("prelabel_s3_key")
        assert "prelabel_error" not in task
    else:
        assert task["prelabel_status"] == "Failed"
        assert FAILURE_REASON_SUBSTRING in task["prelabel_error"]
        assert "prelabel_s3_key" not in task


# ---------------------------------------------------------------- properties

@settings(deadline=None)
@given(outcomes=st.lists(st.booleans(), min_size=2, max_size=4),
       image_count=st.integers(min_value=1, max_value=3))
def test_resolution_idempotence(henv, outcomes, image_count):
    """**Feature: llm-auto-labeling, Property 13: Resolution idempotence**

    **Validates: Requirements 6.4, 6.6**

    For any sequence of redeliveries of the same task's message,
    interleaving success and failure outcomes, the final
    prelabel_status, prelabel_error, and prelabel_s3_key are those of
    the first resolution, autolabel_completed_count advanced exactly
    once, and autolabel_pending decremented exactly once.
    """
    job_id = henv.make_job(autolabel_pending=image_count)
    image_uri = henv.put_image()
    task_id = henv.make_task(job_id, image_uri)
    # The identical message, redelivered len(outcomes) times.
    record = henv.record(job_id, task_id, image_uri)

    # First delivery performs the resolution.
    henv.use_replies([SUCCESS_REPLY if outcomes[0] else FAILURE_REPLY])
    assert henv.run([record]) == {"batchItemFailures": []}
    first = henv.get_task(job_id, task_id)
    _assert_matches_outcome(first, outcomes[0])

    # Every redelivery, whatever its own outcome, changes nothing.
    for outcome in outcomes[1:]:
        henv.use_replies([SUCCESS_REPLY if outcome else FAILURE_REPLY])
        assert henv.run([record]) == {"batchItemFailures": []}

    final = henv.get_task(job_id, task_id)
    assert final["prelabel_status"] == first["prelabel_status"]
    assert final.get("prelabel_error") == first.get("prelabel_error")
    assert final.get("prelabel_s3_key") == first.get("prelabel_s3_key")

    # Req 6.6: the counters moved exactly once, on the first delivery.
    job = henv.get_job(job_id)
    assert int(job["autolabel_completed_count"]) == 1
    assert int(job["autolabel_pending"]) == image_count - 1
    assert bool(job.get("review_ready")) == (image_count == 1)


@settings(deadline=None)
@given(outcomes=st.lists(st.booleans(), min_size=1, max_size=4))
def test_failure_isolation(henv, outcomes):
    """**Feature: llm-auto-labeling, Property 15: Failure isolation**

    **Validates: Requirements 3.5**

    For any batch with a per-record outcome vector, each task's
    resolution matches its own outcome, independent of the others,
    and review_ready flips exactly when the resolved count reaches
    the job's image count.
    """
    image_count = len(outcomes)
    job_id = henv.make_job(autolabel_pending=image_count)
    tasks, records = [], []
    for _ in outcomes:
        image_uri = henv.put_image()
        task_id = henv.make_task(job_id, image_uri)
        tasks.append(task_id)
        records.append(henv.record(job_id, task_id, image_uri))

    # One fake serving the whole vector: converse calls arrive in
    # record order, so reply i belongs to task i.
    henv.use_replies([SUCCESS_REPLY if outcome else FAILURE_REPLY
                      for outcome in outcomes])

    # All but the last record as one batch: no review_ready yet.
    if len(records) > 1:
        assert henv.run(records[:-1]) == {"batchItemFailures": []}
        job = henv.get_job(job_id)
        assert int(job["autolabel_pending"]) == 1
        assert not job.get("review_ready")

    # The final record drains the counter and flips review_ready.
    assert henv.run(records[-1:]) == {"batchItemFailures": []}
    job = henv.get_job(job_id)
    assert int(job["autolabel_pending"]) == 0
    assert job["review_ready"] is True
    assert int(job["autolabel_completed_count"]) == image_count

    # Each task's resolution is exactly its own outcome.
    for task_id, outcome in zip(tasks, outcomes):
        _assert_matches_outcome(henv.get_task(job_id, task_id), outcome)


# --------------------------------------------- all-images-fail (task 17.2)

class TestAllImagesFail:
    """llm-auto-labeling task 17.2 (Req 10.5): a job where every image
    genuinely fails Pre_Label generation — end to end through the real
    dda_autolabel_worker handler — never transitions to a failed or
    terminal state. A team job serves every Failed task to the labeler
    for annotation from scratch through the real next-task gating; a
    skip-verification job becomes review-ready with every image Failed
    and finalize answers 400 for zero accepted results."""

    IMAGE_COUNT = 3

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _user(role):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    @staticmethod
    def _event(method, resource, user, path_params=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": None,
            "body": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": user["user_id"],
                        "email": user["email"],
                        "cognito:username": user["username"],
                        "custom:role": user["role"],
                    }
                }
            },
        }

    def _invoke(self, dda, method, resource, user, path_params=None):
        response = dda.handler(
            self._event(method, resource, user, path_params), None)
        return response["statusCode"], json.loads(response["body"])

    def _fail_every_image(self, henv, job_id, count):
        """Run `count` tasks through the real handler with replies that
        all fail guidance parsing; returns the task ids."""
        tasks, records = [], []
        for _ in range(count):
            image_uri = henv.put_image()
            task_id = henv.make_task(job_id, image_uri)
            tasks.append(task_id)
            records.append(henv.record(job_id, task_id, image_uri))
        henv.use_replies([FAILURE_REPLY] * count)
        assert henv.run(records) == {"batchItemFailures": []}
        return tasks

    def _assert_job_not_terminal(self, henv, job_id):
        """Req 10.5: the job never transitions to a failed or terminal
        state — it stays InProgress with no failure/terminal marker."""
        job = henv.get_job(job_id)
        assert job["status"] == "InProgress"
        assert not job.get("review_finalized")
        return job

    # -------------------------------------------------------------- cases

    def test_team_job_all_failed_stays_open_and_serves_every_task(
            self, henv, dda, aws_stack):
        """Req 10.5: a team job where every image fails generation is
        not terminal, and the real labeler next-task gating presents
        every Failed task as a bare image for labeling from scratch."""
        # A team whose single labeler holds every task.
        labeler = self._user("DataLabeler")
        team_id = f"team-{uuid.uuid4()}"
        aws_stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id,
            "sk": "META",
            "usecase_id": henv.usecase_id,
            "team_name": "All-Fail Team",
            "created_at": 1,
            "created_by": "admin",
        })
        aws_stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id,
            "sk": f"MEMBER#{labeler['user_id']}",
            "user_id": labeler["user_id"],
            "email": labeler["email"],
            "added_at": 1,
            "added_by": "admin",
        })

        job_id = henv.make_job(skip_verification=False, team_id=team_id)
        tasks, records = [], []
        for _ in range(self.IMAGE_COUNT):
            image_uri = henv.put_image()
            task_id = henv.make_task(job_id, image_uri,
                                     assignee=labeler["user_id"])
            tasks.append(task_id)
            records.append(henv.record(job_id, task_id, image_uri))
        henv.use_replies([FAILURE_REPLY] * self.IMAGE_COUNT)
        assert henv.run(records) == {"batchItemFailures": []}

        # Every task resolved Failed with the retained reason; team
        # mode never records the review-ineligibility marker.
        for task_id in tasks:
            task = henv.get_task(job_id, task_id)
            assert task["prelabel_status"] == "Failed"
            assert FAILURE_REASON_SUBSTRING in task["prelabel_error"]
            assert "prelabel_s3_key" not in task
            assert "autolabel_error" not in task
            assert task["status"] == "Assigned"

        # The job is neither failed nor terminal, and carries none of
        # the skip-verification review machinery.
        job = self._assert_job_not_terminal(henv, job_id)
        assert "review_ready" not in job

        # The real next-task gating serves every Failed task in turn as
        # a bare image (no pre-label) carrying its status and reason.
        served = []
        for _ in tasks:
            status, body = self._invoke(
                dda, "GET", "/labeler/jobs/{jobId}/next", labeler,
                {"jobId": job_id})
            assert status == 200
            assert body["complete"] is False
            assert body["prelabel_status"] == "Failed"
            assert FAILURE_REASON_SUBSTRING in body["prelabel_error"]
            assert "prelabel" not in body  # from scratch (Req 7.5)
            assert body["image_url"].startswith("https://")
            served.append(body["task_id"])
            # Hand the served task back as submitted so the next call
            # advances to the next Failed task.
            aws_stack.tables.labeling_tasks.update_item(
                Key={"job_id": job_id, "task_id": body["task_id"]},
                UpdateExpression="SET #s = :submitted",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":submitted": "Submitted"},
            )
        assert sorted(served) == sorted(tasks)  # every task, exactly once

        # All tasks labeled: the labeler sees completion, nothing was
        # withheld, and the job is still not terminal.
        status, body = self._invoke(
            dda, "GET", "/labeler/jobs/{jobId}/next", labeler,
            {"jobId": job_id})
        assert status == 200
        assert body["complete"] is True
        assert body["submitted_count"] == self.IMAGE_COUNT
        assert body["withheld_count"] == 0
        self._assert_job_not_terminal(henv, job_id)

    def test_skip_verification_all_failed_review_ready_finalize_rejected(
            self, henv, dda):
        """Req 10.5: a skip-verification job where every image fails
        generation still becomes review-ready with every image Failed,
        finalize answers 400 for zero accepted results, and the job
        stays open, not terminal."""
        job_id = henv.make_job(autolabel_pending=self.IMAGE_COUNT)
        tasks = self._fail_every_image(henv, job_id, self.IMAGE_COUNT)

        # Every image Failed with the reason retained; skip-verification
        # failures also carry the review-ineligibility marker.
        for task_id in tasks:
            task = henv.get_task(job_id, task_id)
            assert task["prelabel_status"] == "Failed"
            assert FAILURE_REASON_SUBSTRING in task["prelabel_error"]
            assert task["autolabel_error"] == task["prelabel_error"]
            assert "prelabel_s3_key" not in task

        # The counter drained and review_ready flipped even though not
        # a single image succeeded — the job is reviewable, not failed.
        job = self._assert_job_not_terminal(henv, job_id)
        assert job["review_ready"] is True
        assert int(job["autolabel_pending"]) == 0
        assert int(job["autolabel_completed_count"]) == self.IMAGE_COUNT

        # Finalize is rejected for zero accepted results (the existing
        # gate), and the rejection leaves the job open and unchanged.
        admin = self._user("UseCaseAdmin")
        status, body = self._invoke(
            dda, "POST", "/labeling/{id}/review/finalize", admin,
            {"id": job_id})
        assert status == 400
        assert body["accepted_count"] == 0

        job = self._assert_job_not_terminal(henv, job_id)
        assert job["review_ready"] is True
