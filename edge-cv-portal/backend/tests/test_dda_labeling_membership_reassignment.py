"""
Membership-change reassignment wiring in dda_labeling.py
(dda-data-labeling, task 7.2).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, moto DynamoDB + S3, fake Cognito for
member identity/role resolution — the test_dda_labeling_teams.py
convention):

- DELETE /labeling-teams/{teamId}/members/{userId}: the removed
  member's unsubmitted (status=Assigned) tasks in the team's InProgress
  jobs are reassigned across the remaining Data_Labeler members with a
  per-member reassigned-count spread of at most one; submitted tasks
  and their annotations are untouched (Req 5.3)
- Last member removed: unsubmitted tasks -> assignee_user_id=
  'UNASSIGNED', job blocked=true, status stays InProgress (Req 5.4)
- POST /labeling-teams/{teamId}/members to a team with blocked jobs:
  UNASSIGNED tasks distributed across the team's current Data_Labeler
  members, blocked cleared, and the worker invoked with
  {action: 'notify_new_members', job_id, member_ids} for exactly the
  members who previously held zero tasks in the job (Req 5.5, 6.7)
- Rollback: a partial conditional-write failure restores the prior
  assignments and leaves the membership unchanged (Req 5.7)
- Exclusion: a removed member receives no tasks in subsequently created
  jobs (Req 3.6, via the real create_dda_job + worker distribute path)
- Worker notify_new_members action: dispatches the job and the
  member-filtered assignment map to send_distribution_notifications
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
POOL_ID = "us-east-1_dda-reassign-test-pool"
DATASET_BUCKET = "test-reassign-usecase-data"


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
    """Records dda_labeling's fire-and-forget worker invocations
    (notify_new_members payloads, distribute after job creation)."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}

    def payloads(self, action=None):
        payloads = [json.loads(call["Payload"]) for call in self.invocations]
        if action:
            payloads = [p for p in payloads if p.get("action") == action]
        return payloads


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

    import dda_labeling_worker

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)

    return SimpleNamespace(module=dda_labeling, worker=dda_labeling_worker,
                           cognito=fake_cognito)


@pytest.fixture
def env(aws_stack, dda, monkeypatch):
    """Per-test facade: fresh Use_Case, team, fake lambda client, and
    the worker function name wired so notify_new_members invocations
    are recorded."""
    fake_lambda = FakeLambdaClient()
    monkeypatch.setattr(dda.module, "lambda_client", fake_lambda)
    monkeypatch.setenv("DDA_LABELING_WORKER_FUNCTION_NAME",
                       "test-dda-labeling-worker")
    monkeypatch.delenv("AUTOLABEL_QUEUE_URL", raising=False)
    return ReassignEnv(aws_stack, dda, fake_lambda)


class ReassignEnv:
    def __init__(self, stack, dda, fake_lambda):
        self.stack = stack
        self.dda = dda
        self.fake_lambda = fake_lambda
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.prefix = f"datasets/{uuid.uuid4()}/"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Reassignment Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.admin = self.jwt_user(role="UseCaseAdmin")
        self.team_id = f"team-{uuid.uuid4()}"
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": f"Team {self.team_id[:13]}",
            "created_at": 1,
            "created_by": self.admin["user_id"],
        })

    # ------------------------------------------------------------ users
    def jwt_user(self, role="UseCaseAdmin"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def labeler(self, join_team=True):
        """A Cognito Data_Labeler account, optionally already a member
        of the team."""
        username = f"labeler-{uuid.uuid4()}"
        email = f"{username}@example.com"
        sub = self.dda.cognito.add_user(username, email, role="DataLabeler")
        if join_team:
            self.stack.tables.labeling_teams.put_item(Item={
                "team_id": self.team_id,
                "sk": f"MEMBER#{sub}",
                "user_id": sub,
                "email": email,
                "added_at": 1,
                "added_by": self.admin["user_id"],
            })
        return SimpleNamespace(username=username, sub=sub, email=email)

    def members(self):
        response = self.stack.tables.labeling_teams.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key(
                "team_id").eq(self.team_id))
        return [item for item in response.get("Items", [])
                if item["sk"].startswith("MEMBER#")]

    # ------------------------------------------------------------- jobs
    def put_job(self, status="InProgress", blocked=False, **attrs):
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": job_id,
            "labeling_backend": "DDA",
            "status": status,
            "team_id": self.team_id,
            "blocked": blocked,
            "created_at": 1,
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, task_id, assignee, status="Assigned",
                 **attrs):
        item = {
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
        }
        item.update(attrs)
        self.stack.tables.labeling_tasks.put_item(Item=item)

    def seed_tasks(self, job_id, assignee, count, start=0,
                   status="Assigned", **attrs):
        task_ids = [f"task-{index:06d}" for index in range(start,
                                                           start + count)]
        for task_id in task_ids:
            self.put_task(job_id, task_id, assignee, status=status, **attrs)
        return task_ids

    def tasks(self, job_id):
        response = self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key(
                "job_id").eq(job_id))
        return response.get("Items", [])

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    # ------------------------------------------------------------ invoke
    def event(self, method, resource, user, path_params=None, body=None):
        path = resource
        for key, value in (path_params or {}).items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path,
            "pathParameters": path_params or None,
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
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

    def remove_member(self, user_id):
        response = self.dda.module.handler(self.event(
            "DELETE", "/labeling-teams/{teamId}/members/{userId}",
            self.admin,
            path_params={"teamId": self.team_id, "userId": user_id}), None)
        return response["statusCode"], json.loads(response["body"])

    def add_member(self, user_id):
        response = self.dda.module.handler(self.event(
            "POST", "/labeling-teams/{teamId}/members", self.admin,
            path_params={"teamId": self.team_id},
            body={"user_id": user_id}), None)
        return response["statusCode"], json.loads(response["body"])

    # -------------------------------------------- real create/distribute
    def put_images(self, count):
        for index in range(count):
            self.s3.put_object(Bucket=DATASET_BUCKET,
                               Key=f"{self.prefix}img-{index:03d}.jpg",
                               Body=b"fakeimage")

    def create_and_distribute_job(self):
        body = {
            "usecase_id": self.usecase_id,
            "job_name": f"job-{uuid.uuid4().hex[:12]}",
            "dataset_prefix": self.prefix,
            "task_type": "Classification",
            "team_id": self.team_id,
        }
        response = self.dda.module.create_dda_job(
            body, self.jwt_user(role="DataScientist"))
        payload = json.loads(response["body"])
        assert response["statusCode"] == 201, payload
        job_id = payload["job_id"]
        self.dda.worker.handler(
            {"action": "distribute", "job_id": job_id}, None)
        return job_id


# ------------------------------------------------------- member removal

class TestRemovalReassignsUnsubmittedTasks:
    def test_unsubmitted_tasks_rebalanced_submitted_untouched(self, env):
        """Req 5.3: the removed member's unsubmitted tasks are
        reassigned across the remaining members with a reassigned-count
        spread of at most one; submitted tasks and their annotations are
        untouched."""
        keeper_a = env.labeler()
        keeper_b = env.labeler()
        removed = env.labeler()
        job_id = env.put_job()
        # Removed member: 5 unsubmitted + 2 submitted (with annotations).
        moved = env.seed_tasks(job_id, removed.sub, count=5)
        submitted = env.seed_tasks(
            job_id, removed.sub, count=2, start=5, status="Submitted",
            annotation={"label": "anomaly"})
        # Keepers already hold work of their own.
        env.seed_tasks(job_id, keeper_a.sub, count=2, start=7)
        env.seed_tasks(job_id, keeper_b.sub, count=1, start=9)

        status, _ = env.remove_member(removed.sub)
        assert status == 200

        tasks = {task["task_id"]: task for task in env.tasks(job_id)}
        # Reassigned tasks: only to remaining members, spread <= 1.
        reassigned = Counter(tasks[task_id]["assignee_user_id"]
                             for task_id in moved)
        assert set(reassigned) <= {keeper_a.sub, keeper_b.sub}
        assert sum(reassigned.values()) == 5
        assert max(reassigned.values()) - min(reassigned.values()) <= 1
        # Submitted tasks keep the removed member and their annotations.
        for task_id in submitted:
            assert tasks[task_id]["assignee_user_id"] == removed.sub
            assert tasks[task_id]["status"] == "Submitted"
            assert tasks[task_id]["annotation"] == {"label": "anomaly"}
        # Membership deleted, job untouched.
        assert removed.sub not in {m["user_id"] for m in env.members()}
        job = env.get_job(job_id)
        assert job["status"] == "InProgress"
        assert job["blocked"] is False

    def test_jobs_of_other_states_untouched(self, env):
        """Only InProgress jobs are reassigned: a Completed job's tasks
        keep their assignee."""
        env.labeler()  # remaining member
        removed = env.labeler()
        done_job = env.put_job(status="Completed")
        env.seed_tasks(done_job, removed.sub, count=2)

        status, _ = env.remove_member(removed.sub)
        assert status == 200
        assert all(task["assignee_user_id"] == removed.sub
                   for task in env.tasks(done_job))


class TestLastMemberRemoval:
    def test_tasks_unassigned_job_blocked_status_in_progress(self, env):
        """Req 5.4: last member removed -> unsubmitted tasks go
        UNASSIGNED, job blocked=true, status stays InProgress."""
        removed = env.labeler()
        job_id = env.put_job()
        moved = env.seed_tasks(job_id, removed.sub, count=3)
        submitted = env.seed_tasks(job_id, removed.sub, count=1, start=3,
                                   status="Submitted")

        status, _ = env.remove_member(removed.sub)
        assert status == 200

        tasks = {task["task_id"]: task for task in env.tasks(job_id)}
        for task_id in moved:
            assert tasks[task_id]["assignee_user_id"] == "UNASSIGNED"
            assert tasks[task_id]["status"] == "Assigned"
        for task_id in submitted:
            assert tasks[task_id]["assignee_user_id"] == removed.sub

        job = env.get_job(job_id)
        assert job["blocked"] is True
        assert job["status"] == "InProgress"
        assert env.members() == []


# ------------------------------------------------------- member addition

class TestAdditionUnblocksJobs:
    def test_unassigned_tasks_distributed_blocked_cleared(self, env):
        """Req 5.5: adding a member to a team with a blocked job assigns
        the UNASSIGNED tasks across the current members and clears the
        blocked indication."""
        job_id = env.put_job(blocked=True)
        unassigned = env.seed_tasks(job_id, "UNASSIGNED", count=4)
        new_member = env.labeler(join_team=False)

        status, _ = env.add_member(new_member.sub)
        assert status == 201

        tasks = {task["task_id"]: task for task in env.tasks(job_id)}
        for task_id in unassigned:
            assert tasks[task_id]["assignee_user_id"] == new_member.sub
            assert tasks[task_id]["status"] == "Assigned"
        job = env.get_job(job_id)
        assert job["blocked"] is False
        assert job["status"] == "InProgress"

    def test_distribution_balanced_across_all_current_members(self, env):
        """Req 5.5: the unassigned tasks spread across every current
        Data_Labeler member with a per-member count difference <= 1."""
        existing = env.labeler()
        job_id = env.put_job(blocked=True)
        env.seed_tasks(job_id, "UNASSIGNED", count=5)
        new_member = env.labeler(join_team=False)

        status, _ = env.add_member(new_member.sub)
        assert status == 201

        counts = Counter(task["assignee_user_id"]
                         for task in env.tasks(job_id))
        assert set(counts) == {existing.sub, new_member.sub}
        assert sum(counts.values()) == 5
        assert max(counts.values()) - min(counts.values()) <= 1

    def test_notify_new_members_invoked_for_prior_zero_members_only(
            self, env):
        """Req 6.7: the worker is invoked with {action:
        'notify_new_members', job_id, member_ids} naming exactly the
        members who previously held zero tasks in the job."""
        existing = env.labeler()
        job_id = env.put_job(blocked=True)
        # Existing member already holds work in the job.
        env.seed_tasks(job_id, existing.sub, count=1, start=10,
                       status="Submitted")
        env.seed_tasks(job_id, "UNASSIGNED", count=4)
        new_member = env.labeler(join_team=False)

        status, _ = env.add_member(new_member.sub)
        assert status == 201

        payloads = env.fake_lambda.payloads(action="notify_new_members")
        assert payloads == [{
            "action": "notify_new_members",
            "job_id": job_id,
            "member_ids": [new_member.sub],
        }]

    def test_addition_without_blocked_jobs_notifies_nobody(self, env):
        """An unblocked job is untouched by member addition and no
        notification is dispatched."""
        existing = env.labeler()
        job_id = env.put_job(blocked=False)
        env.seed_tasks(job_id, existing.sub, count=2)
        new_member = env.labeler(join_team=False)

        status, _ = env.add_member(new_member.sub)
        assert status == 201

        assert env.fake_lambda.payloads(action="notify_new_members") == []
        assert all(task["assignee_user_id"] == existing.sub
                   for task in env.tasks(job_id))


# --------------------------------------------------------------- rollback

class TestRollbackOnPartialFailure:
    def test_removal_failure_restores_assignments_and_membership(
            self, env, monkeypatch):
        """Req 5.7: when a conditional write fails partway through the
        removal reassignment, the prior assignments are restored from
        the computed inverse and the membership is unchanged."""
        env.labeler()  # remaining member
        removed = env.labeler()
        job_id = env.put_job()
        moved = env.seed_tasks(job_id, removed.sub, count=4)

        real_reassign = env.dda.module._conditional_reassign
        calls = {"n": 0}

        def failing_reassign(job_id_, task_id, from_assignee, to_assignee):
            calls["n"] += 1
            if calls["n"] == 3:  # fail on the third forward write
                raise ClientError(
                    {"Error": {"Code": "InternalServerError",
                               "Message": "injected"}}, "UpdateItem")
            return real_reassign(job_id_, task_id, from_assignee,
                                 to_assignee)

        monkeypatch.setattr(env.dda.module, "_conditional_reassign",
                            failing_reassign)
        status, body = env.remove_member(removed.sub)
        assert status == 500
        assert "unchanged" in body["error"]

        # Prior assignments restored; membership intact.
        tasks = {task["task_id"]: task for task in env.tasks(job_id)}
        for task_id in moved:
            assert tasks[task_id]["assignee_user_id"] == removed.sub
            assert tasks[task_id]["status"] == "Assigned"
        assert removed.sub in {m["user_id"] for m in env.members()}
        assert env.get_job(job_id)["blocked"] is False

    def test_addition_failure_restores_unassigned_and_blocked(
            self, env, monkeypatch):
        """Req 5.7: a partial failure while rebalancing a blocked job on
        member addition restores UNASSIGNED and the blocked flag."""
        job_id = env.put_job(blocked=True)
        unassigned = env.seed_tasks(job_id, "UNASSIGNED", count=3)
        new_member = env.labeler(join_team=False)

        real_reassign = env.dda.module._conditional_reassign
        calls = {"n": 0}

        def failing_reassign(job_id_, task_id, from_assignee, to_assignee):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ClientError(
                    {"Error": {"Code": "InternalServerError",
                               "Message": "injected"}}, "UpdateItem")
            return real_reassign(job_id_, task_id, from_assignee,
                                 to_assignee)

        monkeypatch.setattr(env.dda.module, "_conditional_reassign",
                            failing_reassign)
        status, body = env.add_member(new_member.sub)
        assert status == 500
        assert "unchanged" in body["error"]

        tasks = {task["task_id"]: task for task in env.tasks(job_id)}
        for task_id in unassigned:
            assert tasks[task_id]["assignee_user_id"] == "UNASSIGNED"
        assert env.get_job(job_id)["blocked"] is True
        assert env.fake_lambda.payloads(action="notify_new_members") == []


# ------------------------------------------------- exclusion from new jobs

class TestRemovedMemberExcludedFromNewJobs:
    def test_new_job_distribution_skips_removed_member(self, env):
        """Req 3.6: after removal, subsequently created jobs distribute
        no tasks to the removed member."""
        keeper = env.labeler()
        removed = env.labeler()
        status, _ = env.remove_member(removed.sub)
        assert status == 200

        env.put_images(count=4)
        job_id = env.create_and_distribute_job()

        tasks = env.tasks(job_id)
        assert len(tasks) == 4
        assert {task["assignee_user_id"] for task in tasks} == {keeper.sub}


# --------------------------------------------------- worker action wiring

class TestWorkerNotifyNewMembersAction:
    def test_dispatches_member_filtered_assignments_to_hook(
            self, env, monkeypatch):
        """The notify_new_members action calls the task 7.4 notification
        hook with the job item and the assignment map restricted to
        exactly the requested members."""
        member_a = env.labeler()
        member_b = env.labeler()
        job_id = env.put_job()
        a_tasks = env.seed_tasks(job_id, member_a.sub, count=2)
        env.seed_tasks(job_id, member_b.sub, count=2, start=2)

        calls = []
        monkeypatch.setattr(
            env.dda.worker, "send_distribution_notifications",
            lambda job, assignments: calls.append((job, assignments)))
        result = env.dda.worker.handler(
            {"action": "notify_new_members", "job_id": job_id,
             "member_ids": [member_a.sub]}, None)

        assert result["member_ids"] == [member_a.sub]
        assert result["task_count"] == 2
        assert len(calls) == 1
        job, assignments = calls[0]
        assert job["job_id"] == job_id
        assert assignments == {task_id: member_a.sub
                               for task_id in a_tasks}

    def test_missing_member_ids_is_an_error(self, env):
        result = env.dda.worker.handler(
            {"action": "notify_new_members",
             "job_id": "job-x", "member_ids": []}, None)
        assert "error" in result
