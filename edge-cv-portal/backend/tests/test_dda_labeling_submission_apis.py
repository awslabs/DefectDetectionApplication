"""
Labeler submission and presentation-failure APIs in dda_labeling.py
(dda-data-labeling, task 8.4).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac_middleware, synthetic API Gateway events with
Cognito claims, moto DynamoDB + S3):

- POST /labeler/tasks/{taskId}/submit: a complete annotation per
  modality is persisted with the submitter's identity and timestamp,
  the task marked Submitted and human-annotated (Req 7.7, 8.4);
  Segmentation annotations land in S3 with annotation_s3_key on the
  item; incomplete annotations are rejected identifying the missing
  element with the task left unsubmitted (Req 7.8); Stopped jobs
  answer 409 with nothing persisted (Req 11.8); double submits are
  rejected by the conditional write (Req 7.9); the last submission
  (atomic submitted_count == image_count) async-invokes the worker
  with {action: 'generate_manifest'} while earlier ones don't
  (Req 11.6)
- Ownership: another labeler's task answers 403 carrying no resource
  data plus a labeler_access_denied audit event (Req 2.6)
- POST /labeler/tasks/{taskId}/presentation-failure: marks the task
  PresentationFailed with the reason, withholding it from the
  next-task gating (Req 7.12)
"""
import json
import sys
import time
from types import SimpleNamespace

import boto3
import pytest

from test_dda_labeling_labeler_apis import (
    ARTIFACTS_BUCKET,
    DATASET_BUCKET,
    LabelerEnv,
    REGION,
)


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    # Idempotent in moto's us-east-1 (BucketAlreadyOwnedByYou is a 200).
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)
    return SimpleNamespace(module=dda_labeling)


@pytest.fixture
def env(aws_stack, dda):
    return SubmissionEnv(aws_stack, dda)


class SubmissionEnv(LabelerEnv):
    """LabelerEnv plus the task-8.4 POST routes and table peeking."""

    def post(self, resource, path_params, body, user=None):
        event = self.event("POST", resource, user or self.labeler,
                           path_params)
        event["body"] = json.dumps(body) if body is not None else None
        response = self.dda.module.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def submit(self, task_id, job_id, annotation, user=None):
        return self.post("/labeler/tasks/{taskId}/submit",
                         {"taskId": task_id},
                         {"job_id": job_id, "annotation": annotation},
                         user=user)

    def presentation_failure(self, task_id, job_id, reason="broken image",
                             user=None):
        return self.post("/labeler/tasks/{taskId}/presentation-failure",
                         {"taskId": task_id},
                         {"job_id": job_id, "reason": reason},
                         user=user)

    def get_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")


CLASSIFICATION_OK = {"modality": "Classification", "label": "anomaly"}


def assert_unsubmitted(task):
    """Req 7.8/11.8: the Task_Assignment carries no submission state."""
    assert task["status"] == "Assigned"
    for field in ("annotation", "annotation_s3_key", "submitted_by",
                  "submitted_at", "submitted_at_iso", "human_annotated"):
        assert field not in task, f"unexpected {field} on unsubmitted task"


# ----------------------------------------------------- valid submissions

class TestValidSubmissions:
    def test_classification_submission_persists_identity_and_marks_submitted(
            self, env):
        """Req 7.7/8.4: the annotation is persisted inline with the
        submitter's identity, epoch + ISO timestamps, human_annotated,
        and the task marked Submitted."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=5)
        env.put_task(job_id, "task-0000000", caller)

        before = int(time.time())
        status, body = env.submit("task-0000000", job_id,
                                  CLASSIFICATION_OK)
        assert status == 200
        assert body["status"] == "Submitted"
        assert body["task_id"] == "task-0000000"
        assert body["job_id"] == job_id

        task = env.get_task(job_id, "task-0000000")
        assert task["status"] == "Submitted"
        assert task["annotation"] == CLASSIFICATION_OK
        assert task["submitted_by"] == caller
        assert before <= int(task["submitted_at"]) <= int(time.time())
        assert task["submitted_at_iso"].endswith("Z")
        assert task["human_annotated"] is True
        assert "annotation_s3_key" not in task

    def test_object_detection_submission_persists_inline(self, env):
        """Req 7.7: a complete ObjectDetection annotation (label-set
        classes, integer in-bounds pixel coordinates) is stored inline."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type="ObjectDetection",
                             label_set=["scratch", "dent"], image_count=5)
        env.put_task(job_id, "task-0000000", caller)

        annotation = {
            "modality": "ObjectDetection",
            "image_size": {"width": 640, "height": 480},
            "boxes": [
                {"class": "scratch", "left": 0, "top": 0,
                 "width": 640, "height": 480},
                {"class": "dent", "left": 100, "top": 50,
                 "width": 32, "height": 16},
            ],
        }
        status, body = env.submit("task-0000000", job_id, annotation)
        assert status == 200

        task = env.get_task(job_id, "task-0000000")
        assert task["status"] == "Submitted"
        boxes = task["annotation"]["boxes"]
        assert [box["class"] for box in boxes] == ["scratch", "dent"]
        assert int(boxes[1]["left"]) == 100
        assert task["submitted_by"] == caller
        assert task["human_annotated"] is True

    def test_segmentation_submission_lands_in_s3_with_key_on_item(
            self, env):
        """Req 7.7/8.4: Segmentation regions are written to the portal
        artifacts bucket at labeling/{usecase}/{job}/annotations/
        {task}.json, with annotation_s3_key on the item and no inline
        annotation attribute."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type="Segmentation",
                             label_set=["scratch", "dent"], image_count=5)
        env.put_task(job_id, "task-0000000", caller)

        annotation = {
            "modality": "Segmentation",
            "image_size": {"width": 64, "height": 64},
            "regions": [{"class": "scratch", "rle": "12 5 40 3"}],
        }
        status, body = env.submit("task-0000000", job_id, annotation)
        assert status == 200

        task = env.get_task(job_id, "task-0000000")
        assert task["status"] == "Submitted"
        assert "annotation" not in task
        expected_key = (f"labeling/{env.usecase_id}/{job_id}/"
                        f"annotations/task-0000000.json")
        assert task["annotation_s3_key"] == expected_key
        stored = json.loads(env.s3.get_object(
            Bucket=ARTIFACTS_BUCKET, Key=expected_key)["Body"].read())
        assert stored == annotation
        assert task["submitted_by"] == caller
        assert task["human_annotated"] is True


# ----------------------------------------------- incomplete annotations

class TestIncompleteAnnotationsRejected:
    def test_classification_without_selection_rejected(self, env):
        """Req 7.8: no selection made (or a label outside the label
        set) -> 400 identifying the label, task unsubmitted."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=1)
        env.put_task(job_id, "task-0000000", caller)

        for annotation in ({"modality": "Classification"},
                           {"modality": "Classification", "label": "maybe"}):
            status, body = env.submit("task-0000000", job_id, annotation)
            assert status == 400
            assert any(error["parameter"] == "label"
                       for error in body["validation_errors"])
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))
        assert "submitted_count" not in env.get_job(job_id)

    def test_segmentation_region_lacking_class_or_rle_rejected(self, env):
        """Req 7.8: a region with class null (unclassified SAM
        proposal) or without RLE data is rejected identifying the
        region."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type="Segmentation",
                             label_set=["scratch"], image_count=1)
        env.put_task(job_id, "task-0000000", caller)

        classless = {"modality": "Segmentation",
                     "regions": [{"class": None, "rle": "1 2 3"}]}
        status, body = env.submit("task-0000000", job_id, classless)
        assert status == 400
        assert any(error["parameter"] == "regions"
                   and "class" in error["message"]
                   for error in body["validation_errors"])

        rleless = {"modality": "Segmentation",
                   "regions": [{"class": "scratch"}]}
        status, body = env.submit("task-0000000", job_id, rleless)
        assert status == 400
        assert any(error["parameter"] == "regions"
                   and "RLE" in error["message"]
                   for error in body["validation_errors"])

        assert_unsubmitted(env.get_task(job_id, "task-0000000"))
        # Nothing was written to S3 either.
        listed = env.s3.list_objects_v2(
            Bucket=ARTIFACTS_BUCKET,
            Prefix=f"labeling/{env.usecase_id}/{job_id}/annotations/")
        assert listed.get("KeyCount", 0) == 0

    def test_object_detection_incomplete_boxes_rejected(self, env):
        """Req 7.8: a box lacking a class, out-of-bounds or
        non-integer coordinates, or a missing image_size -> 400
        identifying the element, task unsubmitted."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type="ObjectDetection",
                             label_set=["scratch"], image_count=1)
        env.put_task(job_id, "task-0000000", caller)

        size = {"width": 100, "height": 100}
        cases = [
            # box lacking a class
            ({"modality": "ObjectDetection", "image_size": size,
              "boxes": [{"left": 0, "top": 0, "width": 10, "height": 10}]},
             "boxes"),
            # out-of-bounds box (left + width > W)
            ({"modality": "ObjectDetection", "image_size": size,
              "boxes": [{"class": "scratch", "left": 95, "top": 0,
                         "width": 10, "height": 10}]},
             "boxes"),
            # non-integer pixel coordinates
            ({"modality": "ObjectDetection", "image_size": size,
              "boxes": [{"class": "scratch", "left": 0.5, "top": 0,
                         "width": 10, "height": 10}]},
             "boxes"),
            # missing image bounds
            ({"modality": "ObjectDetection",
              "boxes": [{"class": "scratch", "left": 0, "top": 0,
                         "width": 10, "height": 10}]},
             "image_size"),
        ]
        for annotation, parameter in cases:
            status, body = env.submit("task-0000000", job_id, annotation)
            assert status == 400, f"accepted invalid case: {annotation}"
            assert any(error["parameter"] == parameter
                       for error in body["validation_errors"])
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))

    def test_modality_mismatch_rejected(self, env):
        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=1)  # Classification job
        env.put_task(job_id, "task-0000000", caller)

        status, body = env.submit(
            "task-0000000", job_id,
            {"modality": "ObjectDetection", "image_size": {}, "boxes": []})
        assert status == 400
        assert any(error["parameter"] == "modality"
                   for error in body["validation_errors"])
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))


# ------------------------------------ failed pre-label from-scratch (12.2)

class TestFailedPrelabelSubmission:
    """Feature: llm-auto-labeling, task 12.2 (Req 7.5): a task whose
    LLM pre-label generation Failed is labeled from scratch and its
    submission is validated by the same per-modality completeness
    rules as any other task."""

    LLM_AUTO_LABEL = {
        "enabled": True,
        "model": "llm:us.amazon.nova-pro-v1:0",
        "detection_prompt": "Find every scratch",
    }

    def put_failed_task(self, env, job_id, task_id):
        env.put_task(job_id, task_id, env.labeler["user_id"],
                     prelabel_status="Failed",
                     prelabel_error="model error: guidance did not parse")

    def test_incomplete_submission_rejected_same_rules(self, env):
        """An incomplete annotation on a Failed-prelabel task is a 400
        identifying the missing element, with the task left
        unsubmitted and its failure record intact."""
        job_id = env.put_job(image_count=1,
                             auto_label=self.LLM_AUTO_LABEL)
        self.put_failed_task(env, job_id, "task-0000000")

        status, body = env.submit("task-0000000", job_id,
                                  {"modality": "Classification"})
        assert status == 400
        assert any(error["parameter"] == "label"
                   for error in body["validation_errors"])
        task = env.get_task(job_id, "task-0000000")
        assert_unsubmitted(task)
        # The retained failure record survives the rejection (Req 10.2).
        assert task["prelabel_status"] == "Failed"
        assert task["prelabel_error"].startswith("model error:")

    def test_incomplete_segmentation_rejected_same_rules(self, env):
        """The same gating holds per modality: a Segmentation region
        without RLE data on a Failed-prelabel task is rejected."""
        job_id = env.put_job(task_type="Segmentation",
                             label_set=["scratch"], image_count=1,
                             auto_label=self.LLM_AUTO_LABEL)
        self.put_failed_task(env, job_id, "task-0000000")

        status, body = env.submit(
            "task-0000000", job_id,
            {"modality": "Segmentation",
             "regions": [{"class": "scratch"}]})
        assert status == 400
        assert any(error["parameter"] == "regions"
                   and "RLE" in error["message"]
                   for error in body["validation_errors"])
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))

    def test_complete_submission_accepted_human_annotated(self, env):
        """A complete from-scratch annotation on a Failed-prelabel
        task submits like any other task, recorded human-annotated."""
        job_id = env.put_job(image_count=2,
                             auto_label=self.LLM_AUTO_LABEL)
        self.put_failed_task(env, job_id, "task-0000000")

        status, body = env.submit("task-0000000", job_id,
                                  CLASSIFICATION_OK)
        assert status == 200
        assert body["status"] == "Submitted"

        task = env.get_task(job_id, "task-0000000")
        assert task["status"] == "Submitted"
        assert task["annotation"] == CLASSIFICATION_OK
        assert task["human_annotated"] is True
        # The failure record stays on the task record (Req 10.2).
        assert task["prelabel_status"] == "Failed"
        assert task["prelabel_error"].startswith("model error:")


# ------------------------------------------------- stopped / double-submit

class TestSubmissionRejections:
    def test_stopped_job_rejected_with_nothing_persisted(self, env):
        """Req 11.8: a submission against a Stopped job answers 409 and
        persists nothing."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(status="Stopped", image_count=1)
        env.put_task(job_id, "task-0000000", caller)

        status, body = env.submit("task-0000000", job_id,
                                  CLASSIFICATION_OK)
        assert status == 409
        assert body["status"] == "Stopped"
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))
        assert "submitted_count" not in env.get_job(job_id)

    def test_double_submit_rejected_by_conditional_write(self, env):
        """Req 7.9: the conditional write (status = Assigned AND
        assignee = caller) rejects a second submit, leaving the first
        submission and the counter untouched."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=5)
        env.put_task(job_id, "task-0000000", caller)

        assert env.submit("task-0000000", job_id,
                          CLASSIFICATION_OK)[0] == 200
        first = env.get_task(job_id, "task-0000000")

        status, body = env.submit(
            "task-0000000", job_id,
            {"modality": "Classification", "label": "normal"})
        assert status == 409

        task = env.get_task(job_id, "task-0000000")
        assert task == first  # first submission byte-identical
        assert int(env.get_job(job_id)["submitted_count"]) == 1


# ---------------------------------------------------------------- ownership

class TestOwnershipDenials:
    def test_other_labelers_task_submit_denied_with_audit(self, env):
        """Req 2.6: submitting another labeler's task answers 403 with
        no resource data plus a labeler_access_denied audit event, and
        the task is untouched."""
        owner = env.labeler
        job_id = env.put_job(image_count=1)
        env.put_task(job_id, "task-0000000", owner["user_id"])

        intruder = env.make_labeler()  # team member, no tasks
        status, body = env.submit("task-0000000", job_id,
                                  CLASSIFICATION_OK, user=intruder)
        assert status == 403
        assert body == {"error": "Access denied"}
        events = env.denial_audit_events(user=intruder)
        assert len(events) == 1
        assert events[0]["resource_id"] == "task-0000000"
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))

    def test_presentation_failure_ownership_checked(self, env):
        """Req 2.6: the presentation-failure route applies the same
        ownership checks."""
        owner = env.labeler
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", owner["user_id"])

        intruder = env.make_labeler()
        status, body = env.presentation_failure(
            "task-0000000", job_id, user=intruder)
        assert status == 403
        assert body == {"error": "Access denied"}
        assert len(env.denial_audit_events(user=intruder)) == 1
        assert env.get_task(job_id, "task-0000000")["status"] == "Assigned"

    def test_removed_member_denied_on_submit(self, env):
        """Req 2.4: a labeler removed from the team can no longer
        submit against their stale assignments."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=1)
        env.put_task(job_id, "task-0000000", caller)

        env.remove_member(caller)
        status, body = env.submit("task-0000000", job_id,
                                  CLASSIFICATION_OK)
        assert status == 403
        assert body == {"error": "Access denied"}
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))


# ------------------------------------------------------- manifest trigger

class TestManifestTrigger:
    def test_last_submission_triggers_generate_manifest(self, env,
                                                        monkeypatch):
        """Req 11.6: exactly the submission whose atomic
        submitted_count reaches image_count async-invokes the worker
        with {action: 'generate_manifest', job_id}."""
        invocations = []
        monkeypatch.setattr(env.dda.module, "_invoke_labeling_worker",
                            invocations.append)

        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=2)
        env.put_task(job_id, "task-0000000", caller)
        env.put_task(job_id, "task-0000001", caller)

        status, body = env.submit("task-0000000", job_id,
                                  CLASSIFICATION_OK)
        assert status == 200
        assert body["job_submitted_count"] == 1
        assert invocations == []  # earlier submissions never trigger

        status, body = env.submit("task-0000001", job_id,
                                  CLASSIFICATION_OK)
        assert status == 200
        assert body["job_submitted_count"] == 2
        assert invocations == [
            {"action": "generate_manifest", "job_id": job_id}]


# --------------------------------------------------- presentation failure

class TestPresentationFailure:
    def test_marks_task_withheld_with_reason(self, env):
        """Req 7.12: the failure is recorded with the Task_Assignment
        and the task is withheld from the next-task gating."""
        caller = env.labeler["user_id"]
        job_id = env.put_job()
        env.put_task(job_id, "task-0000000", caller)
        env.put_task(job_id, "task-0000001", caller)

        before = int(time.time())
        status, body = env.presentation_failure(
            "task-0000000", job_id, reason="image failed to render")
        assert status == 200
        assert body["status"] == "PresentationFailed"

        task = env.get_task(job_id, "task-0000000")
        assert task["status"] == "PresentationFailed"
        failure = task["presentation_failure"]
        assert failure["reason"] == "image failed to render"
        assert before <= int(failure["at"]) <= int(time.time())

        # The withheld task is never served again; the labeler advances
        # to the next presentable one.
        status, body = env.next_task(job_id)
        assert status == 200
        assert body["task_id"] == "task-0000001"
        assert body["withheld_count"] == 1

    def test_submitted_task_cannot_be_withdrawn(self, env):
        """The conditional write (status = Assigned) protects a
        submitted annotation from a late presentation-failure report."""
        caller = env.labeler["user_id"]
        job_id = env.put_job(image_count=1)
        env.put_task(job_id, "task-0000000", caller)
        assert env.submit("task-0000000", job_id,
                          CLASSIFICATION_OK)[0] == 200

        status, body = env.presentation_failure("task-0000000", job_id)
        assert status == 409
        assert env.get_task(job_id, "task-0000000")["status"] == "Submitted"
