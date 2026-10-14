"""
Podium payload example tests: the DDA job detail route's additive
`podium` key on GET /labeling/{id}.

Spec: labeling-job-cleanup-work-stealing-and-podium, task 1.10.

Example (non-property) coverage, complementing the Property 8/9 suites
in test_property_labeling_podium.py:

- A Completed team job's detail payload carries the ranked podium
  entries — a concrete 3-submitter scenario with a count tie broken by
  the earlier final submission, emails joined for current members, and
  the bare user id for a departed submitter (Req 7.5).
- A non-team Skip_Verification_Mode Completed job carries no podium
  key (Req 8.2, 10.4).
- A non-Completed (InProgress) team job carries no podium key
  (Req 8.2, 10.4).
- member_progress and every other detail field ride unchanged
  alongside the podium (Req 10.1).

**Validates: Requirements 7.5, 8.2, 10.1, 10.4**

Moto-backed through the real GET /labeling/{id} handler path: jobs,
tasks, and teams seeded directly in DynamoDB, synthetic API Gateway
events with admin-shaped Cognito claims (the PodiumEnv harness shape
from test_property_labeling_podium.py, itself the
test_labeling_backend_switch.py LabelingEnv reduced to the job detail
path). Seeded states respect the shipped structural invariants: a
Completed team job holds no unsubmitted active tasks, every Submitted
task carries submitted_by/submitted_at, and Skip_Verification_Mode
jobs have no team (their items are AUTO result rows).
"""
import json
import sys
import uuid

import pytest

REGION = "us-east-1"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def labeling(aws_stack):
    """The real labeling module imported inside the moto mock."""
    sys.modules.pop("labeling", None)
    import labeling

    return labeling


class PodiumEnv:
    """Per-test seeding + invocation facade with a fresh Use_Case id
    (the test_property_labeling_podium.py PodiumEnv shape)."""

    def __init__(self, stack, labeling):
        self.stack = stack
        self.labeling = labeling
        self.usecase_id = f"uc-{uuid.uuid4()}"
        user_id = f"admin-{uuid.uuid4()}"
        self.user = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "DataScientist",
        }

    # ------------------------------------------------------------ setup
    def put_job(self, **attrs):
        job_id = f"labeling-{uuid.uuid4().hex[:10]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": "podium payload job",
            "created_at": 1,
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, index, assignee, status, **attrs):
        item = {
            "job_id": job_id,
            "task_id": f"task-{index:06d}",
            "usecase_id": self.usecase_id,
            "assignee_user_id": assignee,
            "status": status,
        }
        item.update(attrs)
        self.stack.tables.labeling_tasks.put_item(Item=item)

    def put_team(self, member_ids):
        """Team META + one MEMBER# item per current member; member
        emails follow the shipped `{user_id}@example.com` convention."""
        team_id = f"team-{uuid.uuid4().hex[:10]}"
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id, "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Podium Team", "created_at": 1,
        })
        for user_id in member_ids:
            self.stack.tables.labeling_teams.put_item(Item={
                "team_id": team_id, "sk": f"MEMBER#{user_id}",
                "user_id": user_id,
                "email": f"{user_id}@example.com",
                "added_at": 1,
            })
        return team_id

    # ------------------------------------------------------------ invoke
    def get_job(self, job_id):
        """GET /labeling/{id} through the real handler."""
        event = {
            "httpMethod": "GET",
            "resource": "/labeling/{id}",
            "path": f"/v1/labeling/{job_id}",
            "pathParameters": {"id": job_id},
            "queryStringParameters": None,
            "body": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": self.user["user_id"],
                        "email": self.user["email"],
                        "cognito:username": self.user["username"],
                        "custom:role": self.user["role"],
                    }
                }
            },
        }
        response = self.labeling.handler(event, None)
        return response["statusCode"], json.loads(response["body"])


# ------------------------------------------------------------------- tests

class TestDdaLabelingPodiumPayload:
    def test_completed_team_job_carries_ranked_podium_entries(
            self, aws_stack, labeling):
        """A Completed team job's detail payload carries the ranked
        podium: count descending, the 3-vs-3 count tie between
        user-alice and user-bob broken by user-bob's earlier final
        submission, emails joined for the current members, and the
        departed user-carol carried by bare user id alone.

        **Validates: Requirements 7.5**
        """
        env = PodiumEnv(aws_stack, labeling)
        # user-carol has left the team: her MEMBER# item is absent.
        team_id = env.put_team(["user-alice", "user-bob"])
        job_id = env.put_job(
            labeling_backend="DDA", task_type="Classification",
            status="Completed", team_id=team_id, image_count=8)

        # 8 Submitted tasks (a Completed team job holds no unsubmitted
        # active tasks): user-alice 3 (final 300), user-bob 3
        # (final 250), user-carol 2 (final 100).
        submissions = [
            ("user-alice", 120), ("user-alice", 200), ("user-alice", 300),
            ("user-bob", 50), ("user-bob", 150), ("user-bob", 250),
            ("user-carol", 80), ("user-carol", 100),
        ]
        for index, (user_id, submitted_at) in enumerate(submissions):
            env.put_task(job_id, index, user_id, "Submitted",
                         submitted_by=user_id, submitted_at=submitted_at)

        status_code, response = env.get_job(job_id)
        assert status_code == 200, response

        # The count tie is broken by the earlier final submission:
        # user-bob (final 250) places above user-alice (final 300)
        # despite the lexically smaller user id — the timestamp
        # tie-break, not the user-id one, decides. Exact-dict equality
        # also pins that the departed user-carol's entry carries no
        # email key at all.
        assert response["job"]["podium"] == [
            {"place": 1, "user_id": "user-bob", "submitted": 3,
             "final_submitted_at": 250, "email": "user-bob@example.com"},
            {"place": 2, "user_id": "user-alice", "submitted": 3,
             "final_submitted_at": 300, "email": "user-alice@example.com"},
            {"place": 3, "user_id": "user-carol", "submitted": 2,
             "final_submitted_at": 100},
        ]

    def test_skip_verification_completed_job_has_no_podium_key(
            self, aws_stack, labeling):
        """A Completed Skip_Verification_Mode job (no team, AUTO result
        items) carries no podium key — the key is strictly additive and
        team-job-only.

        **Validates: Requirements 8.2, 10.4**
        """
        env = PodiumEnv(aws_stack, labeling)
        job_id = env.put_job(
            labeling_backend="DDA", task_type="Classification",
            status="Completed", skip_verification=True,
            image_count=2, autolabel_completed_count=2)
        for index in range(2):
            env.put_task(job_id, index, "AUTO", "Assigned",
                         prelabel_status="Available")

        status_code, response = env.get_job(job_id)
        assert status_code == 200, response
        assert "podium" not in response["job"]

    def test_inprogress_team_job_has_no_podium_key(
            self, aws_stack, labeling):
        """A team job that is not Completed carries no podium key even
        though submissions with submitted_by/submitted_at are already
        recorded.

        **Validates: Requirements 8.2, 10.4**
        """
        env = PodiumEnv(aws_stack, labeling)
        team_id = env.put_team(["user-alice", "user-bob"])
        job_id = env.put_job(
            labeling_backend="DDA", task_type="Classification",
            status="InProgress", team_id=team_id, image_count=4)
        env.put_task(job_id, 0, "user-alice", "Submitted",
                     submitted_by="user-alice", submitted_at=10)
        env.put_task(job_id, 1, "user-bob", "Submitted",
                     submitted_by="user-bob", submitted_at=20)
        env.put_task(job_id, 2, "user-alice", "Assigned")
        env.put_task(job_id, 3, "user-bob", "Assigned")

        status_code, response = env.get_job(job_id)
        assert status_code == 200, response
        assert "podium" not in response["job"]

    def test_member_progress_and_detail_fields_unchanged_alongside_podium(
            self, aws_stack, labeling):
        """A qualifying job's payload carries the podium alongside the
        pre-existing detail fields, each exactly what the shipped
        aggregation produced — member_progress keeps its pre-feature
        shape (one {user_id, email, submitted, remaining} entry per
        current member in MEMBER# sort order, zero-progress members
        included).

        **Validates: Requirements 10.1**
        """
        env = PodiumEnv(aws_stack, labeling)
        team_id = env.put_team(["user-alice", "user-bob", "user-dave"])
        job_id = env.put_job(
            labeling_backend="DDA", task_type="Classification",
            status="Completed", team_id=team_id, image_count=3)
        submissions = [
            ("user-alice", 10), ("user-alice", 20), ("user-bob", 5),
        ]
        for index, (user_id, submitted_at) in enumerate(submissions):
            env.put_task(job_id, index, user_id, "Submitted",
                         submitted_by=user_id, submitted_at=submitted_at)

        status_code, response = env.get_job(job_id)
        assert status_code == 200, response
        job = response["job"]

        # The podium is present for this Completed team job...
        assert job["podium"] == [
            {"place": 1, "user_id": "user-alice", "submitted": 2,
             "final_submitted_at": 20, "email": "user-alice@example.com"},
            {"place": 2, "user_id": "user-bob", "submitted": 1,
             "final_submitted_at": 5, "email": "user-bob@example.com"},
        ]

        # ...the raw job item passes through wholesale...
        assert job["job_id"] == job_id
        assert job["usecase_id"] == env.usecase_id
        assert job["status"] == "Completed"
        assert job["team_id"] == team_id
        assert job["image_count"] == 3
        assert job["labeling_backend"] == "DDA"
        assert job["task_type"] == "Classification"
        assert job["job_name"] == "podium payload job"
        assert job["created_at"] == 1

        # ...the shipped aggregates are computed as before...
        assert job["submitted_count"] == 3
        assert job["progress_percent"] == 100
        assert job["unassigned_count"] == 0
        assert job["blocked"] is False
        assert job["notifications_skipped"] is False
        assert job["notification_failures"] == []
        assert job["prelabel_available_count"] == 0
        assert job["prelabel_failed_count"] == 0
        assert "prelabel_failure_reasons" not in job

        # ...and member_progress holds its exact pre-feature shape.
        assert job["member_progress"] == [
            {"user_id": "user-alice", "email": "user-alice@example.com",
             "submitted": 2, "remaining": 0},
            {"user_id": "user-bob", "email": "user-bob@example.com",
             "submitted": 1, "remaining": 0},
            {"user_id": "user-dave", "email": "user-dave@example.com",
             "submitted": 0, "remaining": 0},
        ]
