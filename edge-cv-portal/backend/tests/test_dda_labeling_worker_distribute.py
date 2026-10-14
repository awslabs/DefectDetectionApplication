"""
dda_labeling_worker.py distribute action (dda-data-labeling, task 7.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils, moto DynamoDB + S3 + SQS, fake Cognito for member role
resolution — the test_dda_labeling_create_job.py convention), creating
jobs through the real `create_dda_job` path and then invoking the
worker handler with {action: 'distribute', job_id}:

- Balanced distribution: exactly one Task_Assignment per enumerated
  image, each assigned to a current Data_Labeler, per-member counts
  differing by at most one (Req 5.1, 5.2); task item shape
  (task-<zero-padded index>, status=Assigned, prelabel_status per
  auto-label enablement)
- Skip-verification jobs: one AUTO result item per image with
  prelabel_status=Pending and the job's autolabel_pending counter
  initialized to image_count (Req 9.4, shares the distribute path;
  formalized by task 11.1)
- SQS auto-label fan-out: one message per image with
  {job_id, task_id, image_s3_uri, modality, label_set, model,
  per_label_prompts?}; guarded when AUTOLABEL_QUEUE_URL is unset
- Shortfall: written count != image_count -> job Failed with
  failure_reason and every written task Inactive (Req 5.6)
- Notification hook `send_distribution_notifications(job, assignments)`
  invoked after team-job distribution, not for skip-verification jobs
"""
import json
import re
import sys
import uuid
from collections import Counter
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
POOL_ID = "us-east-1_dda-worker-test-pool"
DATASET_BUCKET = "test-worker-usecase-data"


# ----------------------------------------------------- fake Cognito client

class FakeCognitoClient:
    """Fake for the cognito-idp APIs dda_labeling uses (moto's
    cognito-idp backend is not available here)."""

    def __init__(self):
        self.users = {}  # username -> {attr name: value}

    def add_user(self, username, email, role=None):
        sub = str(uuid.uuid4())
        attrs = {"sub": sub, "email": email}
        if role:
            attrs["custom:role"] = role
        self.users[username] = attrs
        return sub

    def set_role(self, username, role):
        if role is None:
            self.users[username].pop("custom:role", None)
        else:
            self.users[username]["custom:role"] = role

    @staticmethod
    def _shape(username, attrs, key):
        return {
            "Username": username,
            key: [{"Name": name, "Value": value}
                  for name, value in attrs.items()],
        }

    def list_users(self, UserPoolId=None, Filter=None, Limit=None):
        match = re.match(r'sub = "(.+)"', Filter or "")
        users = []
        if match:
            for username, attrs in self.users.items():
                if attrs["sub"] == match.group(1):
                    users.append(self._shape(username, attrs, "Attributes"))
        return {"Users": users[:Limit] if Limit else users}

    def admin_get_user(self, UserPoolId=None, Username=None):
        if Username not in self.users:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException",
                           "Message": "User does not exist."}},
                "AdminGetUser")
        return self._shape(Username, self.users[Username], "UserAttributes")


class FakeLambdaClient:
    """Absorbs create_dda_job's async worker invocation (the tests
    invoke the worker handler directly)."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling + dda_labeling_worker modules imported
    inside the moto mock, sharing one fake Cognito client."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID
    dda_labeling.lambda_client = FakeLambdaClient()

    import dda_labeling_worker

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)

    return SimpleNamespace(module=dda_labeling, worker=dda_labeling_worker,
                           cognito=fake_cognito)


@pytest.fixture
def env(aws_stack, dda, monkeypatch):
    """Per-test facade: fresh Use_Case, dataset prefix, and a labeling
    team; worker fan-out env vars start unset."""
    monkeypatch.delenv("AUTOLABEL_QUEUE_URL", raising=False)
    monkeypatch.delenv("DDA_LABELING_WORKER_FUNCTION_NAME", raising=False)
    return WorkerEnv(aws_stack, dda)


class WorkerEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.s3 = boto3.client("s3", region_name=REGION)
        self.sqs = boto3.client("sqs", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.prefix = f"datasets/{uuid.uuid4()}/"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Worker Distribute Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.creator = self.make_user(role="DataScientist")
        self.team_id = f"team-{uuid.uuid4()}"
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": f"Team {self.team_id[:13]}",
            "created_at": 1,
            "created_by": self.creator["user_id"],
        })

    # ------------------------------------------------------------ setup
    def make_user(self, role="DataScientist"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def add_labeler(self, role="DataLabeler"):
        username = f"labeler-{uuid.uuid4()}"
        email = f"{username}@example.com"
        sub = self.dda.cognito.add_user(username, email, role=role)
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": f"MEMBER#{sub}",
            "user_id": sub,
            "email": email,
            "added_at": 1,
            "added_by": self.creator["user_id"],
        })
        return SimpleNamespace(username=username, sub=sub, email=email)

    def put_images(self, count=None, keys=None):
        keys = keys or [f"img-{i:03d}.jpg" for i in range(count)]
        for key in keys:
            self.s3.put_object(Bucket=DATASET_BUCKET,
                               Key=f"{self.prefix}{key}", Body=b"fakeimage")
        return keys

    def make_queue(self, monkeypatch):
        url = self.sqs.create_queue(
            QueueName=f"autolabel-{uuid.uuid4().hex[:12]}")["QueueUrl"]
        monkeypatch.setenv("AUTOLABEL_QUEUE_URL", url)
        return url

    def queue_messages(self, url):
        messages = []
        while True:
            response = self.sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
            batch = response.get("Messages", [])
            if not batch:
                break
            for message in batch:
                messages.append(json.loads(message["Body"]))
                self.sqs.delete_message(
                    QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
        return messages

    # ------------------------------------------------------------ invoke
    def create_job(self, user=None, **overrides):
        body = {
            "usecase_id": self.usecase_id,
            "job_name": f"job-{uuid.uuid4().hex[:12]}",
            "dataset_prefix": self.prefix,
            "task_type": "Classification",
            "team_id": self.team_id,
        }
        body.update(overrides)
        body = {k: v for k, v in body.items() if v is not None}
        response = self.dda.module.create_dda_job(body, user or self.creator)
        payload = json.loads(response["body"])
        assert response["statusCode"] == 201, payload
        return payload["job_id"]

    def distribute(self, job_id):
        return self.dda.worker.handler(
            {"action": "distribute", "job_id": job_id}, None)

    # ------------------------------------------------------------- store
    def tasks(self, job_id):
        response = self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key(
                "job_id").eq(job_id))
        return response.get("Items", [])

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")


# ------------------------------------------------------- team distribution

class TestTeamDistribution:
    def test_one_task_per_image_balanced_across_members(self, env):
        """Req 5.1, 5.2: exactly one task per enumerated image, each to
        a current Data_Labeler, per-member counts differing by <= 1."""
        members = [env.add_labeler() for _ in range(3)]
        keys = env.put_images(count=8)
        job_id = env.create_job()

        result = env.distribute(job_id)
        assert result["status"] == "InProgress"
        assert result["task_count"] == 8

        tasks = env.tasks(job_id)
        assert len(tasks) == 8
        assert {task["image_key"] for task in tasks} == {
            f"{env.prefix}{key}" for key in keys}

        member_subs = {member.sub for member in members}
        assert {task["assignee_user_id"] for task in tasks} <= member_subs
        counts = Counter(task["assignee_user_id"] for task in tasks)
        assert max(counts.values()) - min(counts.values()) <= 1
        assert sum(counts.values()) == 8

        assert env.get_job(job_id)["status"] == "InProgress"

    def test_task_item_shape(self, env):
        """Task items carry the design's fields: task-<zero-padded
        index> ids, Assigned status, prelabel_status None without
        auto-labeling, usecase_id and s3 URI."""
        member = env.add_labeler()
        env.put_images(keys=["a.jpg", "b.png"])
        job_id = env.create_job()
        env.distribute(job_id)

        tasks = sorted(env.tasks(job_id), key=lambda t: t["task_id"])
        assert [task["task_id"] for task in tasks] == [
            "task-000000", "task-000001"]
        for task in tasks:
            assert task["status"] == "Assigned"
            assert task["prelabel_status"] == "None"
            assert task["usecase_id"] == env.usecase_id
            assert task["assignee_user_id"] == member.sub
            assert task["image_s3_uri"] == (
                f"s3://{DATASET_BUCKET}/{task['image_key']}")

    def test_member_without_current_role_excluded(self, env):
        """Req 5.1: assignment goes only to members holding the
        Data_Labeler role at distribution time."""
        keeper = env.add_labeler()
        drifter = env.add_labeler()
        env.put_images(count=4)
        job_id = env.create_job()
        env.dda.cognito.set_role(drifter.username, "Viewer")

        env.distribute(job_id)
        tasks = env.tasks(job_id)
        assert len(tasks) == 4
        assert {task["assignee_user_id"] for task in tasks} == {keeper.sub}

    def test_non_in_progress_job_not_distributed(self, env):
        """A stop/failure racing the async invoke never creates work."""
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job()
        env.stack.tables.labeling_jobs.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :stopped",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":stopped": "Stopped"})

        result = env.distribute(job_id)
        assert result.get("skipped") is True
        assert env.tasks(job_id) == []


# ------------------------------------------------------- skip-verification

class TestSkipVerification:
    def create_skip_job(self, env, image_count=3):
        env.put_images(count=image_count)
        return env.create_job(
            user=env.make_user(role="PortalAdmin"),
            team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts={"normal": "Is it normal?",
                               "anomaly": "Is it anomalous?"})

    def test_auto_result_items_with_pending_counter(self, env):
        """Req 9.4/9.5: one AUTO result item per image with
        prelabel_status=Pending; autolabel_pending initialized to
        image_count."""
        job_id = self.create_skip_job(env, image_count=3)
        result = env.distribute(job_id)
        assert result["task_count"] == 3

        tasks = env.tasks(job_id)
        assert len(tasks) == 3
        for task in tasks:
            assert task["assignee_user_id"] == "AUTO"
            assert task["status"] == "Assigned"
            assert task["prelabel_status"] == "Pending"

        job = env.get_job(job_id)
        assert job["autolabel_pending"] == 3
        assert job["status"] == "InProgress"

    def test_notification_hook_not_invoked(self, env, monkeypatch):
        """Req 9.4: zero labeler notifications for skip-verification."""
        calls = []
        monkeypatch.setattr(
            env.dda.worker, "send_distribution_notifications",
            lambda job, assignments: calls.append((job, assignments)))
        job_id = self.create_skip_job(env)
        env.distribute(job_id)
        assert calls == []


# --------------------------------------------------------- SQS fan-out

class TestAutolabelFanout:
    def test_team_job_fanout_message_shape(self, env, monkeypatch):
        """One message per image: {job_id, task_id, image_s3_uri,
        modality, label_set, model}; no per_label_prompts for team
        jobs; tasks pend their pre-labels."""
        url = env.make_queue(monkeypatch)
        env.add_labeler()
        env.put_images(count=3)
        job_id = env.create_job(
            auto_label={"enabled": True,
                        "model": "bedrock:anthropic.claude-3-haiku"})
        env.distribute(job_id)

        tasks = env.tasks(job_id)
        assert all(task["prelabel_status"] == "Pending" for task in tasks)

        messages = env.queue_messages(url)
        assert len(messages) == 3
        by_task = {message["task_id"]: message for message in messages}
        assert set(by_task) == {task["task_id"] for task in tasks}
        for task in tasks:
            message = by_task[task["task_id"]]
            assert message == {
                "job_id": job_id,
                "task_id": task["task_id"],
                "image_s3_uri": task["image_s3_uri"],
                "modality": "Classification",
                "label_set": ["normal", "anomaly"],
                "model": "bedrock:anthropic.claude-3-haiku",
            }

    def test_skip_verification_fanout_includes_prompts(self, env,
                                                       monkeypatch):
        """Skip-verification messages carry the Bedrock model and the
        Per_Label_Prompts."""
        url = env.make_queue(monkeypatch)
        env.put_images(count=2)
        job_id = env.create_job(
            user=env.make_user(role="PortalAdmin"),
            team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts={"normal": "Is it normal?",
                               "anomaly": "Is it anomalous?"})
        env.distribute(job_id)

        messages = env.queue_messages(url)
        assert len(messages) == 2
        for message in messages:
            assert message["model"] == "bedrock:anthropic.claude-3-haiku"
            assert message["per_label_prompts"] == {
                "normal": "Is it normal?", "anomaly": "Is it anomalous?"}

    def test_no_fanout_without_auto_labeling(self, env, monkeypatch):
        """Plain team jobs enqueue nothing."""
        url = env.make_queue(monkeypatch)
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job()
        env.distribute(job_id)
        assert env.queue_messages(url) == []

    def test_queue_url_unset_guarded(self, env):
        """AUTOLABEL_QUEUE_URL unset: distribution still completes."""
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job(
            auto_label={"enabled": True,
                        "model": "bedrock:anthropic.claude-3-haiku"})
        result = env.distribute(job_id)
        assert result["status"] == "InProgress"
        assert len(env.tasks(job_id)) == 2


# ------------------------------------------------- LLM auto-label fan-out

class TestLlmAutolabelFanout:
    """llm-auto-labeling task 7.2 (Req 2.6, 3.1): the fan-out carries
    the Detection_Prompt for the llm: family, LLM model choice takes
    precedence over the skip-verification Bedrock hardwire, and the
    existing SAM / Bedrock message bodies are unchanged."""

    LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
    PROMPT = "  Find every scratch on the metal surface.\n"
    PER_LABEL = {"normal": "Is it normal?", "anomaly": "Is it anomalous?"}

    def test_llm_team_job_carries_detection_prompt(self, env, monkeypatch):
        """Req 3.1: an LLM team job enqueues model='llm:<id>' with the
        stored detection_prompt verbatim and no per_label_prompts."""
        url = env.make_queue(monkeypatch)
        env.add_labeler()
        env.put_images(count=3)
        job_id = env.create_job(
            auto_label={"enabled": True,
                        "model": self.LLM_MODEL,
                        "detection_prompt": self.PROMPT})
        env.distribute(job_id)

        tasks = env.tasks(job_id)
        messages = env.queue_messages(url)
        assert len(messages) == 3
        by_task = {message["task_id"]: message for message in messages}
        assert set(by_task) == {task["task_id"] for task in tasks}
        for task in tasks:
            assert by_task[task["task_id"]] == {
                "job_id": job_id,
                "task_id": task["task_id"],
                "image_s3_uri": task["image_s3_uri"],
                "modality": "Classification",
                "label_set": ["normal", "anomaly"],
                "model": self.LLM_MODEL,
                "detection_prompt": self.PROMPT,
            }

    def test_llm_skip_verification_precedence_over_bedrock_hardwire(
            self, env, monkeypatch):
        """Req 2.6: an LLM skip-verification job enqueues the llm:
        model (not bedrock:{bedrock_model_id}) with both the
        detection_prompt and the per_label_prompts."""
        url = env.make_queue(monkeypatch)
        env.put_images(count=2)
        job_id = env.create_job(
            user=env.make_user(role="PortalAdmin"),
            team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts=dict(self.PER_LABEL),
            auto_label={"enabled": True,
                        "model": self.LLM_MODEL,
                        "detection_prompt": self.PROMPT})
        env.distribute(job_id)

        messages = env.queue_messages(url)
        assert len(messages) == 2
        for message in messages:
            assert message["model"] == self.LLM_MODEL
            assert message["detection_prompt"] == self.PROMPT
            assert message["per_label_prompts"] == self.PER_LABEL

    def test_bedrock_skip_verification_body_unchanged(self, env,
                                                      monkeypatch):
        """Req 2.6 boundary: a Bedrock skip-verification job still
        enqueues bedrock:{bedrock_model_id} with per_label_prompts and
        no detection_prompt (byte-identical to today's body)."""
        url = env.make_queue(monkeypatch)
        env.put_images(count=2)
        job_id = env.create_job(
            user=env.make_user(role="PortalAdmin"),
            team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts=dict(self.PER_LABEL))
        env.distribute(job_id)

        tasks = env.tasks(job_id)
        messages = env.queue_messages(url)
        assert len(messages) == 2
        by_task = {message["task_id"]: message for message in messages}
        for task in tasks:
            assert by_task[task["task_id"]] == {
                "job_id": job_id,
                "task_id": task["task_id"],
                "image_s3_uri": task["image_s3_uri"],
                "modality": "Classification",
                "label_set": ["normal", "anomaly"],
                "model": "bedrock:anthropic.claude-3-haiku",
                "per_label_prompts": self.PER_LABEL,
            }

    def test_sam_team_job_body_unchanged(self, env, monkeypatch):
        """Req 1.7 by construction: a SAM job's message body carries
        neither detection_prompt nor per_label_prompts."""
        url = env.make_queue(monkeypatch)
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job(
            task_type="Segmentation",
            label_set=["scratch"],
            auto_label={"enabled": True, "model": "sam"})
        env.distribute(job_id)

        tasks = env.tasks(job_id)
        messages = env.queue_messages(url)
        assert len(messages) == 2
        by_task = {message["task_id"]: message for message in messages}
        for task in tasks:
            assert by_task[task["task_id"]] == {
                "job_id": job_id,
                "task_id": task["task_id"],
                "image_s3_uri": task["image_s3_uri"],
                "modality": "Segmentation",
                "label_set": ["scratch"],
                "model": "sam",
            }


# ------------------------------------------------------------- shortfall

class TestShortfall:
    def test_partial_distribution_fails_job_and_deactivates_tasks(
            self, env, monkeypatch):
        """Req 5.6: written count != image_count -> job Failed with
        failure_reason and every written task Inactive."""
        env.add_labeler()
        env.put_images(count=4)
        job_id = env.create_job()

        real_distribute = env.dda.worker.distribute

        def partial(task_ids, member_ids):
            assignments = real_distribute(task_ids, member_ids)
            assignments.pop(task_ids[-1], None)  # drop one task
            return assignments

        monkeypatch.setattr(env.dda.worker, "distribute", partial)
        result = env.distribute(job_id)
        assert result["status"] == "Failed"

        job = env.get_job(job_id)
        assert job["status"] == "Failed"
        assert "3 of 4" in job["failure_reason"]

        tasks = env.tasks(job_id)
        assert len(tasks) == 3
        assert all(task["status"] == "Inactive" for task in tasks)

    def test_write_exception_fails_job(self, env, monkeypatch):
        """Req 5.6: an exception mid-distribution never leaves a
        labelable partial set."""
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job()

        monkeypatch.setattr(
            env.dda.worker, "distribute",
            lambda task_ids, member_ids: (_ for _ in ()).throw(
                RuntimeError("boom")))
        result = env.distribute(job_id)
        assert result["status"] == "Failed"

        job = env.get_job(job_id)
        assert job["status"] == "Failed"
        assert "boom" in job["failure_reason"]


# ------------------------------------------------------ notification hook

class TestNotificationHook:
    def test_hook_invoked_with_job_and_assignments(self, env, monkeypatch):
        """The task 7.4 hook runs after a successful team distribution
        with the job item and the full assignment map."""
        members = [env.add_labeler() for _ in range(2)]
        env.put_images(count=4)
        job_id = env.create_job()

        calls = []
        monkeypatch.setattr(
            env.dda.worker, "send_distribution_notifications",
            lambda job, assignments: calls.append((job, assignments)))
        env.distribute(job_id)

        assert len(calls) == 1
        job, assignments = calls[0]
        assert job["job_id"] == job_id
        assert len(assignments) == 4
        assert set(assignments.values()) == {m.sub for m in members}

    def test_hook_not_invoked_on_shortfall(self, env, monkeypatch):
        """Req 5.6: a failed distribution notifies nobody."""
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job()

        calls = []
        monkeypatch.setattr(
            env.dda.worker, "send_distribution_notifications",
            lambda job, assignments: calls.append(job))
        monkeypatch.setattr(env.dda.worker, "distribute",
                            lambda task_ids, member_ids: {})
        env.distribute(job_id)
        assert calls == []
