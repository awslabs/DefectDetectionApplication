"""
Skip-verification Admin_Review APIs in dda_labeling.py
(dda-data-labeling, task 11.3).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims, moto DynamoDB + S3), seeding skip-verification jobs and
AUTO result items (pre-label payloads in the portal artifacts bucket,
the shape dda_autolabel_worker leaves behind):

- GET /labeling/{id}/review: every dataset image listed with its
  succeeded/failed status, the pre-label annotation inline, the
  failure reason for failed items, the current decision, and a
  presigned image URL (Req 9.5, 9.10); Limit + ExclusiveStartKey
  pagination through an opaque base64 next_token
- POST /labeling/{id}/review/decisions: batch upserts persist and stay
  mutable until finalize (Req 9.6); accepting a failed item is a 400
  identifying it with nothing persisted (Req 9.10); post-finalize
  changes answer 409 (Req 9.6)
- POST /labeling/{id}/review/finalize: undecided Available results ->
  400 with the undecided count, review open, decisions retained
  (Req 9.7); zero accepted -> 400 (Req 9.8); success sets
  review_finalized and async-invokes the worker with
  {action: 'generate_manifest', job_id} (Req 9.9, 11.6); an already
  finalized review answers 409
- Authorization: only UseCaseAdmin/PortalAdmin — a Viewer is denied by
  @rbac_check and a DataScientist (who holds MANAGE_LABELING_JOBS) by
  the explicit admin check matching skip-verification creation
- Non-skip-verification jobs (team DDA, Ground Truth) answer 400 on
  every review route
"""
import json
import sys
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from boto3.dynamodb.conditions import Key

REGION = "us-east-1"
DATASET_BUCKET = "test-review-dataset"
ARTIFACTS_BUCKET = "test-portal-artifacts"  # conftest PORTAL_ARTIFACTS_BUCKET


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, plus
    the dataset bucket the presigned image URLs point at."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)
    return SimpleNamespace(module=dda_labeling)


@pytest.fixture
def env(aws_stack, dda):
    """Per-test facade with a fresh Use_Case and an admin caller."""
    return ReviewEnv(aws_stack, dda)


class ReviewEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Admin Review Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.admin = self.make_user("UseCaseAdmin")

    # ------------------------------------------------------------ setup
    def make_user(self, role):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def put_job(self, skip_verification=True, backend="DDA",
                image_count=0, **attrs):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": backend,
            "status": "InProgress",
            "task_type": "Classification",
            "label_set": ["normal", "anomaly"],
            "dataset_bucket": DATASET_BUCKET,
            "dataset_prefix": "datasets/x/",
            "image_count": image_count,
            "skip_verification": skip_verification,
            "created_at": 1,
            "created_by": self.admin["user_id"],
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def auto_task(self, job_id, index, prelabel=None, decision=None,
                  failed=False, error=None):
        """A skip-verification result item as the auto-label worker
        (and the review-decision API) leave it. `error` mirrors the
        LLM worker's _mark_task, which records the failure reason as
        prelabel_error (and autolabel_error in skip-verification
        mode)."""
        task_id = f"task-{index:06d}"
        image_key = f"datasets/x/img-{index:03d}.jpg"
        item = {
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
            "image_key": image_key,
            "usecase_id": self.usecase_id,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "created_at": 1,
            "updated_at": 1700000000,
        }
        if failed:
            item["prelabel_status"] = "Failed"
            if error is not None:
                item["prelabel_error"] = error
                item["autolabel_error"] = error
            else:
                item["autolabel_error"] = "model failure"
        else:
            key = (f"labeling/{self.usecase_id}/{job_id}/"
                   f"prelabels/{task_id}.json")
            self.s3.put_object(
                Bucket=ARTIFACTS_BUCKET, Key=key,
                Body=json.dumps(
                    prelabel
                    or {"modality": "Classification",
                        "label": "anomaly"}).encode())
            item["prelabel_status"] = "Available"
            item["prelabel_s3_key"] = key
        if decision:
            item["review_decision"] = decision
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return item

    # ------------------------------------------------------------ invoke
    def event(self, method, resource, job_id, user=None, body=None,
              query=None):
        user = user or self.admin
        return {
            "httpMethod": method,
            "resource": resource,
            "path": resource.replace("{id}", job_id),
            "pathParameters": {"id": job_id},
            "queryStringParameters": query,
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

    def invoke(self, method, resource, job_id, user=None, body=None,
               query=None):
        response = self.dda.module.handler(
            self.event(method, resource, job_id, user, body, query), None)
        return response["statusCode"], json.loads(response["body"])

    def get_review(self, job_id, user=None, query=None):
        return self.invoke("GET", "/labeling/{id}/review", job_id,
                           user=user, query=query)

    def post_decisions(self, job_id, decisions, user=None):
        return self.invoke("POST", "/labeling/{id}/review/decisions",
                           job_id, user=user,
                           body={"decisions": decisions})

    def finalize(self, job_id, user=None):
        return self.invoke("POST", "/labeling/{id}/review/finalize",
                           job_id, user=user, body={})

    # ------------------------------------------------------------- store
    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def tasks(self, job_id):
        return self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=Key("job_id").eq(job_id),
        ).get("Items", [])


def is_presigned(url):
    params = parse_qs(urlparse(url).query)
    return "X-Amz-Expires" in params or "Expires" in params


# ------------------------------------------------------------ review listing

class TestReviewListing:
    def test_succeeded_and_failed_items(self, env):
        """Req 9.5/9.10: every dataset image listed — succeeded items
        carry the pre-label inline and a presigned image URL; failed
        items carry autolabel_error."""
        job_id = env.put_job(image_count=3)
        prelabels = [
            {"modality": "Classification", "label": "anomaly"},
            {"modality": "Classification", "label": "normal"},
        ]
        env.auto_task(job_id, 0, prelabel=prelabels[0])
        env.auto_task(job_id, 1, prelabel=prelabels[1],
                      decision="accepted")
        env.auto_task(job_id, 2, failed=True)

        status, body = env.get_review(job_id)
        assert status == 200
        assert body["job_id"] == job_id
        assert body["count"] == 3
        assert body["review_finalized"] is False
        assert "next_token" not in body

        items = {item["task_id"]: item for item in body["items"]}
        assert set(items) == {"task-000000", "task-000001", "task-000002"}

        for index, task_id in enumerate(["task-000000", "task-000001"]):
            item = items[task_id]
            assert item["status"] == "succeeded"
            assert item["prelabel"] == prelabels[index]
            assert item["image_key"] == f"datasets/x/img-{index:03d}.jpg"
            assert is_presigned(item["image_url"])
            assert "autolabel_error" not in item
        assert "review_decision" not in items["task-000000"]
        assert items["task-000001"]["review_decision"] == "accepted"

        failed = items["task-000002"]
        assert failed["status"] == "failed"
        assert failed["autolabel_error"] == "model failure"
        assert "prelabel" not in failed
        assert is_presigned(failed["image_url"])

    def test_pagination_covers_every_image_exactly_once(self, env):
        """Req 9.5: Limit + ExclusiveStartKey pagination via the base64
        next_token walks the whole dataset without gaps or repeats."""
        job_id = env.put_job(image_count=5)
        for index in range(5):
            env.auto_task(job_id, index)

        seen = []
        token = None
        pages = 0
        while True:
            query = {"limit": "2"}
            if token:
                query["next_token"] = token
            status, body = env.get_review(job_id, query=query)
            assert status == 200
            assert len(body["items"]) <= 2
            seen.extend(item["task_id"] for item in body["items"])
            pages += 1
            token = body.get("next_token")
            if not token:
                break
        assert pages == 3
        assert seen == [f"task-{index:06d}" for index in range(5)]

    def test_invalid_next_token_rejected(self, env):
        job_id = env.put_job()
        env.auto_task(job_id, 0)
        status, body = env.get_review(
            job_id, query={"next_token": "not-a-token"})
        assert status == 400
        assert "next_token" in body["error"]

    def test_non_skip_verification_jobs_rejected(self, env):
        """Only skip-verification DDA jobs have an Admin_Review; a team
        DDA job and a Ground Truth job answer 400 on every route."""
        team_job = env.put_job(skip_verification=False)
        gt_job = env.put_job(skip_verification=False,
                             backend="GroundTruth")
        for job_id in (team_job, gt_job):
            assert env.get_review(job_id)[0] == 400
            assert env.post_decisions(
                job_id, {"task-000000": "accepted"})[0] == 400
            assert env.finalize(job_id)[0] == 400

    def test_unknown_job_404(self, env):
        status, body = env.get_review(f"labeling-{uuid.uuid4().hex[:8]}")
        assert status == 404


# ---------------------------------------------------------------- decisions

class TestReviewDecisions:
    def test_decisions_persist_and_are_mutable_until_finalize(self, env):
        """Req 9.6: batch upserts persist per image and any decision can
        be changed while the review is open."""
        job_id = env.put_job(image_count=2)
        env.auto_task(job_id, 0)
        env.auto_task(job_id, 1)

        status, body = env.post_decisions(job_id, {
            "task-000000": "accepted",
            "task-000001": "rejected",
        })
        assert status == 200
        assert body["updated_count"] == 2
        assert env.get_task(job_id,
                            "task-000000")["review_decision"] == "accepted"
        assert env.get_task(job_id,
                            "task-000001")["review_decision"] == "rejected"

        # Still mutable: flip a decision before finalization.
        status, _ = env.post_decisions(job_id,
                                       {"task-000000": "rejected"})
        assert status == 200
        assert env.get_task(job_id,
                            "task-000000")["review_decision"] == "rejected"

        # The listing reflects the persisted decisions.
        _, review = env.get_review(job_id)
        decisions = {item["task_id"]: item.get("review_decision")
                     for item in review["items"]}
        assert decisions == {"task-000000": "rejected",
                             "task-000001": "rejected"}

    def test_accepting_failed_item_rejected_and_nothing_persisted(
            self, env):
        """Req 9.10: failed results are ineligible for acceptance — the
        400 identifies them and no decision in the batch is written."""
        job_id = env.put_job(image_count=2)
        env.auto_task(job_id, 0)
        env.auto_task(job_id, 1, failed=True)

        status, body = env.post_decisions(job_id, {
            "task-000000": "accepted",
            "task-000001": "accepted",
        })
        assert status == 400
        assert body["ineligible_task_ids"] == ["task-000001"]
        # Nothing persisted, including the eligible item in the batch.
        assert "review_decision" not in env.get_task(job_id, "task-000000")
        assert "review_decision" not in env.get_task(job_id, "task-000001")

    def test_rejecting_failed_item_allowed(self, env):
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0, failed=True)
        status, _ = env.post_decisions(job_id, {"task-000000": "rejected"})
        assert status == 200
        assert env.get_task(job_id,
                            "task-000000")["review_decision"] == "rejected"

    def test_unknown_task_ids_rejected(self, env):
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0)
        status, body = env.post_decisions(job_id, {
            "task-000000": "accepted",
            "task-999999": "accepted",
        })
        assert status == 400
        assert body["unknown_task_ids"] == ["task-999999"]
        assert "review_decision" not in env.get_task(job_id, "task-000000")

    def test_invalid_decision_values_rejected(self, env):
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0)
        status, body = env.post_decisions(job_id,
                                          {"task-000000": "maybe"})
        assert status == 400
        assert body["invalid_decisions"] == {"task-000000": "maybe"}

        status, _ = env.post_decisions(job_id, {})
        assert status == 400

    def test_post_finalize_decision_change_rejected_409(self, env):
        """Req 9.6/9.10: decisions are immutable after finalization."""
        job_id = env.put_job(image_count=1, review_finalized=True)
        env.auto_task(job_id, 0, decision="accepted")

        status, body = env.post_decisions(job_id,
                                          {"task-000000": "rejected"})
        assert status == 409
        assert "finalized" in body["error"]
        assert env.get_task(job_id,
                            "task-000000")["review_decision"] == "accepted"


# ----------------------------------------------------------------- finalize

class TestFinalize:
    def test_undecided_results_reject_with_count(self, env):
        """Req 9.7: finalizing while Available results are undecided is
        a 400 naming the undecided count, with the review left open and
        every persisted decision retained."""
        job_id = env.put_job(image_count=4)
        env.auto_task(job_id, 0, decision="accepted")
        env.auto_task(job_id, 1)             # undecided
        env.auto_task(job_id, 2)             # undecided
        env.auto_task(job_id, 3, failed=True)  # failed: never blocks

        status, body = env.finalize(job_id)
        assert status == 400
        assert body["undecided_count"] == 2

        job = env.get_job(job_id)
        assert not job.get("review_finalized")
        assert env.get_task(job_id,
                            "task-000000")["review_decision"] == "accepted"

    def test_zero_accepted_rejected(self, env):
        """Req 9.8: at least one accepted result is required."""
        job_id = env.put_job(image_count=2)
        env.auto_task(job_id, 0, decision="rejected")
        env.auto_task(job_id, 1, failed=True)

        status, body = env.finalize(job_id)
        assert status == 400
        assert body["accepted_count"] == 0
        assert not env.get_job(job_id).get("review_finalized")

    def test_successful_finalize_sets_flag_and_invokes_worker(
            self, env, monkeypatch):
        """Req 9.9/11.6: success sets review_finalized=true and
        async-invokes the worker with {action: 'generate_manifest'}."""
        invocations = []
        monkeypatch.setattr(env.dda.module, "_invoke_labeling_worker",
                            invocations.append)
        job_id = env.put_job(image_count=3)
        env.auto_task(job_id, 0, decision="accepted")
        env.auto_task(job_id, 1, decision="rejected")
        env.auto_task(job_id, 2, failed=True)

        status, body = env.finalize(job_id)
        assert status == 200
        assert body["review_finalized"] is True
        assert body["accepted_count"] == 1
        assert env.get_job(job_id)["review_finalized"] is True
        assert invocations == [
            {"action": "generate_manifest", "job_id": job_id}]

    def test_already_finalized_409_without_second_generation(
            self, env, monkeypatch):
        invocations = []
        monkeypatch.setattr(env.dda.module, "_invoke_labeling_worker",
                            invocations.append)
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0, decision="accepted")

        assert env.finalize(job_id)[0] == 200
        status, body = env.finalize(job_id)
        assert status == 409
        assert "finalized" in body["error"]
        assert len(invocations) == 1


# ----------------------------------------- LLM failure visibility (12.2)

class TestLlmFailureVisibility:
    """Feature: llm-auto-labeling, task 12.2 (Req 7.5, 7.6, 7.7, 10.4,
    10.5): every image of an LLM skip-verification job is listed with
    its succeeded/failed status, a failed image carries its
    prelabel_status/prelabel_error reason and cannot be accepted, and
    finalize with zero accepted results is still rejected."""

    LLM_AUTO_LABEL = {
        "enabled": True,
        "model": "llm:us.amazon.nova-pro-v1:0",
        "detection_prompt": "Find every scratch",
    }

    def test_every_image_listed_with_prelabel_status_and_reason(self, env):
        """Req 7.6/10.4: succeeded items carry prelabel_status
        Available (no error); a failed item carries prelabel_status
        Failed with the retained prelabel_error reason."""
        job_id = env.put_job(image_count=3,
                             auto_label=self.LLM_AUTO_LABEL)
        reason = "model invocation timed out after 60s"
        env.auto_task(job_id, 0)
        env.auto_task(job_id, 1)
        env.auto_task(job_id, 2, failed=True, error=reason)

        status, body = env.get_review(job_id)
        assert status == 200
        assert body["count"] == 3
        items = {item["task_id"]: item for item in body["items"]}
        assert set(items) == {"task-000000", "task-000001", "task-000002"}

        for task_id in ("task-000000", "task-000001"):
            item = items[task_id]
            assert item["status"] == "succeeded"
            assert item["prelabel_status"] == "Available"
            assert "prelabel_error" not in item
            assert "prelabel" in item

        failed = items["task-000002"]
        assert failed["status"] == "failed"
        assert failed["prelabel_status"] == "Failed"
        assert failed["prelabel_error"] == reason
        assert failed["autolabel_error"] == reason
        assert "prelabel" not in failed

    def test_failed_llm_image_cannot_be_accepted(self, env):
        """Req 7.7: a Failed LLM image is ineligible for acceptance —
        the 400 identifies it and nothing in the batch persists;
        rejecting it stays allowed."""
        job_id = env.put_job(image_count=2,
                             auto_label=self.LLM_AUTO_LABEL)
        env.auto_task(job_id, 0)
        env.auto_task(job_id, 1, failed=True,
                      error="model error: guidance did not parse")

        status, body = env.post_decisions(job_id, {
            "task-000000": "accepted",
            "task-000001": "accepted",
        })
        assert status == 400
        assert body["ineligible_task_ids"] == ["task-000001"]
        assert "review_decision" not in env.get_task(job_id, "task-000000")
        assert "review_decision" not in env.get_task(job_id, "task-000001")

        status, _ = env.post_decisions(job_id, {"task-000001": "rejected"})
        assert status == 200
        assert env.get_task(
            job_id, "task-000001")["review_decision"] == "rejected"

    def test_all_images_failed_lists_all_and_finalize_rejected(self, env):
        """Req 10.5/7.7: a job where every image failed still lists
        every image with its Failed status and reason; finalize is a
        400 for zero accepted results and the job stays open, not
        terminal."""
        job_id = env.put_job(image_count=3,
                             auto_label=self.LLM_AUTO_LABEL)
        for index in range(3):
            env.auto_task(job_id, index, failed=True,
                          error=f"model error: image {index}")

        status, body = env.get_review(job_id)
        assert status == 200
        assert body["count"] == 3
        for item in body["items"]:
            assert item["status"] == "failed"
            assert item["prelabel_status"] == "Failed"
            assert item["prelabel_error"].startswith("model error:")

        status, body = env.finalize(job_id)
        assert status == 400
        assert body["accepted_count"] == 0

        job = env.get_job(job_id)
        assert not job.get("review_finalized")
        assert job["status"] == "InProgress"


# ------------------------------------------------------------ authorization

class TestAuthorization:
    def test_viewer_denied_by_rbac(self, env):
        """A role without MANAGE_LABELING_JOBS is denied through the
        real @rbac_check path."""
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0)
        viewer = env.make_user("Viewer")

        for call in (
                lambda: env.get_review(job_id, user=viewer),
                lambda: env.post_decisions(
                    job_id, {"task-000000": "accepted"}, user=viewer),
                lambda: env.finalize(job_id, user=viewer)):
            status, body = call()
            assert status == 403
            assert body["error"] == "Insufficient permissions"

    def test_data_scientist_denied_by_admin_check(self, env):
        """The Admin_Review is UseCaseAdmin/PortalAdmin-only (matching
        skip-verification creation): a DataScientist holds
        MANAGE_LABELING_JOBS but is still denied."""
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0, decision="accepted")
        scientist = env.make_user("DataScientist")

        for call in (
                lambda: env.get_review(job_id, user=scientist),
                lambda: env.post_decisions(
                    job_id, {"task-000000": "rejected"}, user=scientist),
                lambda: env.finalize(job_id, user=scientist)):
            status, body = call()
            assert status == 403
            assert "administrator" in body["error"].lower()
        # Nothing changed under the denied calls.
        assert env.get_task(job_id,
                            "task-000000")["review_decision"] == "accepted"
        assert not env.get_job(job_id).get("review_finalized")

    def test_portal_admin_allowed(self, env):
        job_id = env.put_job(image_count=1)
        env.auto_task(job_id, 0)
        portal_admin = env.make_user("PortalAdmin")
        status, _ = env.get_review(job_id, user=portal_admin)
        assert status == 200
