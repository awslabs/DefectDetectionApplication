"""
SES notification service in dda_labeling_worker.py
(dda-data-labeling, task 7.4).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils, moto DynamoDB + S3 + SES with a verified sender, fake
Cognito for member identity/role resolution — the
test_dda_labeling_worker_distribute.py convention), creating jobs
through the real `create_dda_job` path and invoking the worker handler:

- Exactly one email per member holding >= 1 Task_Assignment in the
  distribution, zero emails to members holding zero tasks (Req 6.1)
- Email content: job name, the recipient's assigned image count, and
  the `https://{PORTAL_DOMAIN}/labeler?job={job_id}` hyperlink, sent
  from SES_SENDER_ADDRESS (Req 6.2, 6.5)
- Per-recipient retry (3 total attempts): a transiently failing send
  succeeds on retry with no failure recorded; an always-failing
  recipient exhausts the attempts, {email, reason} is appended to the
  job's notification_failures, the remaining recipients are still
  emailed, and the job status is untouched (Req 6.3, 6.4)
- Email resolution falls back to Cognito when the team member item
  carries no email address
- SES_SENDER_ADDRESS unset: notifications_skipped=true recorded on the
  job, nothing sent, job status untouched (Req 6.6)
- notify_new_members end-to-end: exactly one email to each named new
  member with their assigned count, none to prior members (Req 6.7)
"""
import json
import re
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
POOL_ID = "us-east-1_dda-notify-test-pool"
DATASET_BUCKET = "test-notify-usecase-data"
SENDER = "portal-notifications@example.com"
PORTAL_DOMAIN = "portal.example.com"


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

    def invoke(self, **kwargs):
        return {"StatusCode": 202}


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling + dda_labeling_worker modules imported
    inside the moto mock, sharing one fake Cognito client, with the
    SES sender identity verified in moto."""
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
    boto3.client("ses", region_name=REGION).verify_email_identity(
        EmailAddress=SENDER)

    return SimpleNamespace(module=dda_labeling, worker=dda_labeling_worker,
                           cognito=fake_cognito)


@pytest.fixture
def sends(dda, monkeypatch):
    """Spy on the worker's SES client: records every send_email call
    while delegating to the real (moto) client, so verified-sender
    behavior stays real."""
    calls = []
    real_send = dda.worker.ses_client.send_email

    def spy(**kwargs):
        calls.append(kwargs)
        return real_send(**kwargs)

    monkeypatch.setattr(dda.worker.ses_client, "send_email", spy)
    return calls


@pytest.fixture
def env(aws_stack, dda, monkeypatch, sends):
    """Per-test facade: fresh Use_Case, dataset prefix, labeling team;
    SES sender + portal domain configured; retry backoff zeroed."""
    monkeypatch.setenv("SES_SENDER_ADDRESS", SENDER)
    monkeypatch.setenv("PORTAL_DOMAIN", PORTAL_DOMAIN)
    monkeypatch.delenv("AUTOLABEL_QUEUE_URL", raising=False)
    monkeypatch.setattr(dda.worker, "NOTIFICATION_RETRY_DELAY_SECONDS", 0)
    return NotifyEnv(aws_stack, dda, sends)


class NotifyEnv:
    def __init__(self, stack, dda, sends):
        self.stack = stack
        self.dda = dda
        self.sends = sends
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.prefix = f"datasets/{uuid.uuid4()}/"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Notification Test",
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

    def add_labeler(self, member_email=True):
        """A Cognito Data_Labeler on the team. member_email=False omits
        the email attribute from the member item (exercising the
        Cognito fallback resolution)."""
        username = f"labeler-{uuid.uuid4()}"
        email = f"{username}@example.com"
        sub = self.dda.cognito.add_user(username, email, role="DataLabeler")
        item = {
            "team_id": self.team_id,
            "sk": f"MEMBER#{sub}",
            "user_id": sub,
            "added_at": 1,
            "added_by": self.creator["user_id"],
        }
        if member_email:
            item["email"] = email
        self.stack.tables.labeling_teams.put_item(Item=item)
        return SimpleNamespace(username=username, sub=sub, email=email)

    def put_images(self, count):
        for index in range(count):
            self.s3.put_object(Bucket=DATASET_BUCKET,
                               Key=f"{self.prefix}img-{index:03d}.jpg",
                               Body=b"fakeimage")

    def put_task(self, job_id, task_id, assignee, status="Assigned"):
        self.stack.tables.labeling_tasks.put_item(Item={
            "job_id": job_id,
            "task_id": task_id,
            "usecase_id": self.usecase_id,
            "image_key": f"{self.prefix}{task_id}.jpg",
            "image_s3_uri":
                f"s3://{DATASET_BUCKET}/{self.prefix}{task_id}.jpg",
            "assignee_user_id": assignee,
            "status": status,
            "prelabel_status": "None",
            "created_at": 1,
        })

    # ------------------------------------------------------------ invoke
    def create_job(self, **overrides):
        body = {
            "usecase_id": self.usecase_id,
            "job_name": f"job-{uuid.uuid4().hex[:12]}",
            "dataset_prefix": self.prefix,
            "task_type": "Classification",
            "team_id": self.team_id,
        }
        body.update(overrides)
        response = self.dda.module.create_dda_job(body, self.creator)
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

    # ------------------------------------------------------------ assert
    def sent_to(self):
        """Recipient address of each recorded send, in order."""
        return [call["Destination"]["ToAddresses"][0]
                for call in self.sends]


class AlwaysFailingSes:
    """SES stand-in that raises for one recipient address and delegates
    every other send to the real (moto) client."""

    def __init__(self, real, failing_address):
        self.real = real
        self.failing_address = failing_address
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["Destination"]["ToAddresses"][0] == self.failing_address:
            raise ClientError(
                {"Error": {"Code": "MessageRejected",
                           "Message": "injected send failure"}},
                "SendEmail")
        return self.real.send_email(**kwargs)


# ------------------------------------------------------- recipients (6.1)

class TestRecipients:
    def test_one_email_per_member_with_tasks_none_to_others(self, env):
        """Req 6.1: exactly one email per member holding >= 1 task;
        zero emails to the member left without tasks."""
        members = [env.add_labeler() for _ in range(3)]
        env.put_images(count=2)  # 3 members, 2 images: one member idle
        job_id = env.create_job()
        env.distribute(job_id)

        tasks = env.tasks(job_id)
        assignees = {task["assignee_user_id"] for task in tasks}
        assert len(assignees) == 2

        expected = {member.email for member in members
                    if member.sub in assignees}
        idle = {member.email for member in members
                if member.sub not in assignees}
        recipients = env.sent_to()
        assert sorted(recipients) == sorted(expected)  # exactly one each
        assert not set(recipients) & idle
        assert env.get_job(job_id).get("notification_failures") is None

    def test_member_item_without_email_resolved_via_cognito(self, env):
        """A member item lacking the email attribute falls back to the
        Cognito lookup and is still notified."""
        member = env.add_labeler(member_email=False)
        env.put_images(count=2)
        job_id = env.create_job()
        env.distribute(job_id)

        assert env.sent_to() == [member.email]


# ---------------------------------------------------------- content (6.2)

class TestContent:
    def test_email_contains_job_name_count_and_labeler_link(self, env):
        """Req 6.2, 6.5: body carries the job name, the recipient's
        assigned image count, and the labeler hyperlink; sent from the
        configured sender."""
        member = env.add_labeler()
        env.put_images(count=3)
        job_id = env.create_job()
        env.distribute(job_id)

        job = env.get_job(job_id)
        assert len(env.sends) == 1
        call = env.sends[0]
        assert call["Source"] == SENDER
        assert call["Destination"]["ToAddresses"] == [member.email]

        link = f"https://{PORTAL_DOMAIN}/labeler?job={job_id}"
        for body in (call["Message"]["Body"]["Text"]["Data"],
                     call["Message"]["Body"]["Html"]["Data"]):
            assert job["job_name"] in body
            assert "3" in body
            assert link in body


# ------------------------------------------------- retry / failure (6.3/6.4)

class TestRetryAndFailureRecording:
    def test_transient_failure_succeeds_on_retry(self, env, monkeypatch):
        """Req 6.3: a send that fails twice and succeeds on the third
        attempt records no failure."""
        member = env.add_labeler()
        env.put_images(count=1)
        job_id = env.create_job()

        real_send = env.dda.worker.ses_client.send_email
        attempts = {"n": 0}

        def flaky(**kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ClientError(
                    {"Error": {"Code": "Throttling",
                               "Message": "injected transient failure"}},
                    "SendEmail")
            return real_send(**kwargs)

        monkeypatch.setattr(
            env.dda.worker, "ses_client",
            SimpleNamespace(send_email=flaky))
        env.distribute(job_id)

        assert attempts["n"] == 3
        job = env.get_job(job_id)
        assert job.get("notification_failures") is None
        assert job["status"] == "InProgress"

    def test_exhausted_retries_recorded_remaining_recipients_processed(
            self, env, monkeypatch):
        """Req 6.3, 6.4: an always-failing recipient is attempted 3
        times, then {email, reason} lands in notification_failures; the
        other recipient is still emailed and the job status is
        untouched."""
        member_a = env.add_labeler()
        member_b = env.add_labeler()
        env.put_images(count=4)
        job_id = env.create_job()

        failing = min(member_a, member_b, key=lambda m: m.sub)
        surviving = member_b if failing is member_a else member_a
        fake_ses = AlwaysFailingSes(env.dda.worker.ses_client,
                                    failing.email)
        monkeypatch.setattr(env.dda.worker, "ses_client", fake_ses)
        env.distribute(job_id)

        failing_attempts = [call for call in fake_ses.calls
                            if call["Destination"]["ToAddresses"]
                            == [failing.email]]
        assert len(failing_attempts) == 3

        surviving_sends = [call for call in fake_ses.calls
                           if call["Destination"]["ToAddresses"]
                           == [surviving.email]]
        assert len(surviving_sends) == 1

        job = env.get_job(job_id)
        failures = job.get("notification_failures")
        assert len(failures) == 1
        assert failures[0]["email"] == failing.email
        assert "injected send failure" in failures[0]["reason"]
        # Req 6.4: the job status never changes on notification failure.
        assert job["status"] == "InProgress"
        assert job.get("notifications_skipped") is None


# --------------------------------------------------- sender unset (6.6)

class TestSenderUnset:
    def test_notifications_skipped_recorded_and_nothing_sent(
            self, env, monkeypatch):
        """Req 6.6: SES_SENDER_ADDRESS unset — the job proceeds with
        notifications_skipped=true and zero sends."""
        monkeypatch.delenv("SES_SENDER_ADDRESS", raising=False)
        env.add_labeler()
        env.put_images(count=2)
        job_id = env.create_job()
        result = env.distribute(job_id)

        assert result["status"] == "InProgress"
        assert env.sends == []
        job = env.get_job(job_id)
        assert job["notifications_skipped"] is True
        assert job["status"] == "InProgress"


# ---------------------------------------------- notify_new_members (6.7)

class TestNotifyNewMembers:
    def test_end_to_end_sends_only_to_new_members(self, env):
        """Req 6.7: the notify_new_members action emails exactly the
        named members with their assigned counts; prior members get
        nothing."""
        prior = env.add_labeler()
        newcomer = env.add_labeler()
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        env.stack.tables.labeling_jobs.put_item(Item={
            "job_id": job_id,
            "usecase_id": env.usecase_id,
            "job_name": f"rebalance-{job_id}",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "team_id": env.team_id,
            "created_at": 1,
        })
        # Prior member already holds work; the newcomer was just
        # assigned two tasks by a membership-change rebalance.
        env.put_task(job_id, "task-000000", prior.sub, status="Submitted")
        env.put_task(job_id, "task-000001", newcomer.sub)
        env.put_task(job_id, "task-000002", newcomer.sub)

        result = env.dda.worker.handler(
            {"action": "notify_new_members", "job_id": job_id,
             "member_ids": [newcomer.sub]}, None)

        assert result["member_ids"] == [newcomer.sub]
        assert env.sent_to() == [newcomer.email]
        body = env.sends[0]["Message"]["Body"]["Text"]["Data"]
        assert "2" in body
        assert f"rebalance-{job_id}" in body
        assert f"https://{PORTAL_DOMAIN}/labeler?job={job_id}" in body
