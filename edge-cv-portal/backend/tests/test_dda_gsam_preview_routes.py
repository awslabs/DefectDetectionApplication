"""
Grounded-sam family on the Preview_API routes in dda_labeling.py
(grounded-sam-prompt-tuning-preview, task 1.4).

Feature: grounded-sam-prompt-tuning-preview

Example-based coverage of `POST /labeling-preview/runs` and
`GET /labeling-preview/runs/{runId}` for the model value `grounded-sam`,
driven through `dda_labeling.handler` with synthetic API Gateway events
against the moto-backed stack from conftest.py — the
test_dda_labeling_preview_routes.py structure with `PreviewEnv`
subclassed to send the family's request body (per-label
`prompt_overrides`; no `detection_prompt`, no `few_shot`, no sizing
keys). The module is imported with GROUNDED_SAM_WORKER_FUNCTION_NAME
configured — the flag-on deploy — so the worker-deployed rule is
silent everywhere except the test that blanks it:

- An accepted grounded-sam request answers the existing 202
  `{run_id, sample_count, status}` shape; the RUN item carries
  `model='grounded-sam'`, the task_type, the label_set, the pruned
  surviving `prompt_overrides` (blank-after-trim entries dropped,
  survivors character-for-character), `few_shot_enabled=False` with
  zero example counts, NO `detection_prompt` and NO sizing attributes;
  one Pending IMAGE#{i:03d} item per Sample_Image; and the executor is
  async-self-invoked with `{'action': 'execute_preview_run', 'run_id'}`
  (Req 3.1)
- The single `preview_run` audit event carries the requesting identity,
  the Use_Case, the model value 'grounded-sam', the Sample_Image count
  and the Labeling_Modality (Req 3.7)
- Both routes flatten @rbac_check's denial to the fixed
  `{'error': 'Not authorized'}` 403 body, and the status route answers
  the fixed 404 for another user's run, byte-identical to an unknown
  run id (Req 3.8)
- With GROUNDED_SAM_WORKER_FUNCTION_NAME empty the start route rejects
  an otherwise-valid grounded-sam request with a 400 whose
  validation_errors carry exactly the Not_Deployed_Message, claiming no
  lock and persisting no run or sample state; with the name configured
  the rule is silent and the same request is accepted (Req 6.1)
- A second start while the caller already holds an active claim answers
  409 with the existing in-progress message and creates nothing
  (Req 3.6)

The Preview_Executor is deliberately never exercised (task 1.5's file):
the async self-invoke goes to FakeLambdaClient, which only records it.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    POOL_ID,
    REGION,
    FakeCognitoClient,
    FakeLambdaClient,
)
from test_dda_labeling_preview_routes import (
    FUNCTION_NAME,
    PreviewEnv,
    parameters,
    viewer,
)

GSAM_MODEL = "grounded-sam"
GSAM_WORKER_FUNCTION = "test-grounded-sam-worker"
LABEL_SET = ["scratch", "dent"]
NOT_DEPLOYED_MESSAGE = "Grounded-SAM worker is not deployed"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock with
    the grounded-sam worker name configured (the flag-on deploy, so the
    Req 6.1 rule is silent by default) and fake Cognito and Lambda
    clients (the preview-routes convention)."""
    os.environ["GROUNDED_SAM_WORKER_FUNCTION_NAME"] = GSAM_WORKER_FUNCTION
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    # The module read env at import; make sure the test value stuck.
    dda_labeling.GROUNDED_SAM_WORKER_FUNCTION_NAME = GSAM_WORKER_FUNCTION

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    try:
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=DATASET_BUCKET)
    except ClientError:
        pass  # a sibling module already created the shared dataset bucket

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


class GsamPreviewEnv(PreviewEnv):
    """PreviewEnv whose default body is a valid Grounded_SAM_Preview_Run
    start request: the family's prompt inputs are the per-label
    Prompt_Overrides — no detection_prompt, no few_shot, no sizing keys
    (grounded-sam-prompt-tuning-preview Req 3.1)."""

    def preview_body(self, **overrides):
        base = {
            "usecase_id": self.usecase_id,
            "dataset_prefix": self.prefix,
            "model": GSAM_MODEL,
            "task_type": "Segmentation",
            "label_set": list(LABEL_SET),
            "sample_images": [f"{self.prefix}a.jpg"],
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}


@pytest.fixture
def env(aws_stack, dda):
    return GsamPreviewEnv(aws_stack, dda)


# ----------------------------------------------------------- authorization

class TestAuthorization:
    """Req 3.8: grounded-sam requests are authorized exactly as llm:
    ones — MANAGE_LABELING_JOBS through @rbac_check with the flattened
    fixed-body 403, and the status route's fixed 404 for a run the
    caller does not own."""

    def test_start_viewer_denied_with_fixed_body(self, env):
        response = env.start_raw(user=viewer(env))
        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "Not authorized"}
        env.assert_no_preview_state()

    def test_status_viewer_denied_with_fixed_body(self, env):
        _, started = env.start()
        response = env.status_raw(started["run_id"], user=viewer(env))
        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "Not authorized"}

    def test_foreign_run_404_matches_unknown_run(self, env):
        """One fixed 404 body for another user's grounded-sam run,
        byte-identical to an unknown run id."""
        _, started = env.start()

        foreign = env.status_raw(started["run_id"],
                                 user=env.make_user(role="DataScientist"))
        unknown = env.status_raw(f"preview-{uuid.uuid4().hex[:8]}")
        assert foreign["statusCode"] == unknown["statusCode"] == 404
        assert json.loads(foreign["body"]) == {
            "error": "Preview run not found"}
        assert foreign["body"] == unknown["body"]


# ------------------------------------------------------- worker deployment

class TestWorkerNotDeployed:
    """Req 6.1: worker-not-deployed is a start-time validation
    rejection, not a per-sample failure — and with the name configured
    the rule is silent."""

    def test_missing_worker_rejected_with_exact_message(self, env,
                                                        monkeypatch):
        """An otherwise-valid request is rejected with exactly the
        Not_Deployed_Message: no lock claimed, no run or sample items
        persisted, no executor invoked."""
        monkeypatch.setattr(env.module,
                            "GROUNDED_SAM_WORKER_FUNCTION_NAME", "")
        status, body = env.start()
        assert status == 400
        assert body["error"] == "Preview run validation failed"
        assert [err["message"] for err in body["validation_errors"]] == [
            NOT_DEPLOYED_MESSAGE]
        assert parameters(body) == {"model"}
        env.assert_no_preview_state()

    def test_configured_worker_keeps_the_rule_silent(self, env):
        """The identical request with GROUNDED_SAM_WORKER_FUNCTION_NAME
        present (the module fixture's flag-on posture) is accepted."""
        status, body = env.start()
        assert status == 202
        assert body["status"] == "Running"
        assert not any(err["message"] == NOT_DEPLOYED_MESSAGE
                       for err in body.get("validation_errors", []))


# --------------------------------------------------------------- 202 start

class TestStartAccepted:
    """Req 3.1: an accepted grounded-sam request rides the existing
    Preview_API machinery — the 202 shape, the RUN item with the
    family's attributes, the Pending sample items, and the async
    executor self-invoke."""

    def test_accepted_response_run_item_and_samples(self, env):
        status, body = env.start(
            sample_images=env.samples(3),
            prompt_overrides={
                "scratch": "   ",                     # blank → dropped
                "dent": "  gap between pieces ",      # survives, raw
            })
        assert status == 202
        assert set(body) == {"run_id", "sample_count", "status"}
        assert body["sample_count"] == 3
        assert body["status"] == "Running"

        run = env.run_item(body["run_id"])
        assert run["status"] == "Running"
        assert run["usecase_id"] == env.usecase_id
        assert run["created_by"] == env.creator["user_id"]
        assert run["model"] == GSAM_MODEL
        assert run["task_type"] == "Segmentation"
        assert run["label_set"] == LABEL_SET
        # The pruned survivors, character-for-character: the blank
        # entry is dropped, the surviving value is the raw string.
        assert run["prompt_overrides"] == {"dent": "  gap between pieces "}
        # The family resolves no few-shot selection and no sizing.
        assert run["few_shot_enabled"] is False
        assert int(run["attached_example_count"]) == 0
        assert int(run["omitted_example_count"]) == 0
        assert "detection_prompt" not in run
        assert "downscale_max_edge" not in run
        assert "token_budget" not in run
        assert int(run["sample_count"]) == 3

        items = env.sample_items(body["run_id"])
        assert [item["task_id"] for item in items] == [
            "IMAGE#000", "IMAGE#001", "IMAGE#002"]
        assert {item["state"] for item in items} == {"Pending"}
        assert [item["sample_key"] for item in items] == env.samples(3)

    def test_override_free_run_leaves_the_attribute_absent(self, env):
        """An override-free grounded-sam RUN item reads like an llm
        run's minus the llm-only attributes: no prompt_overrides map is
        written for an empty survivor set."""
        status, body = env.start()
        assert status == 202
        run = env.run_item(body["run_id"])
        assert "prompt_overrides" not in run
        assert "detection_prompt" not in run

    def test_executor_self_invoked_async(self, env):
        """The captured async self-invoke: the same function, invoked
        Event-style with {'action': 'execute_preview_run', 'run_id'}."""
        status, body = env.start()
        assert status == 202

        invocations = env.invocations()
        assert len(invocations) == 1
        assert invocations[0]["FunctionName"] == FUNCTION_NAME
        assert invocations[0]["InvocationType"] == "Event"
        assert json.loads(invocations[0]["Payload"]) == {
            "action": "execute_preview_run", "run_id": body["run_id"]}


# -------------------------------------------------------------- audit

class TestAuditEvent:
    """Req 3.7: the same single `preview_run` event the llm: start path
    records, its details distinguishing the family."""

    def test_preview_run_event_fields(self, env):
        status, body = env.start(sample_images=env.samples(2),
                                 task_type="ObjectDetection",
                                 prompt_overrides={"dent": "shallow crater"})
        assert status == 202

        events = env.audit_events("preview_run")
        assert len(events) == 1
        event = events[0]
        assert event["user_id"] == env.creator["user_id"]
        assert event["resource_type"] == "labeling_preview"
        assert event["resource_id"] == body["run_id"]
        assert event["result"] == "success"
        details = event["details"]
        assert details["usecase_id"] == env.usecase_id
        assert details["model"] == GSAM_MODEL
        assert int(details["sample_count"]) == 2
        assert details["task_type"] == "ObjectDetection"
        assert details["few_shot_enabled"] is False


# ------------------------------------------------------------- concurrency

class TestInFlightLock:
    """Req 3.6: the existing per-user, per-Use_Case in-flight claim
    answers 409 with the existing message while active."""

    def test_second_run_rejected_with_409_while_claim_active(self, env):
        status, first = env.start()
        assert status == 202

        before = len(env.preview_items())
        response = env.start_raw()
        assert response["statusCode"] == 409
        assert json.loads(response["body"]) == {
            "error": "A preview run is already in progress for this use case"}
        # The rejected request created nothing: no new run or sample
        # items, no second executor invoke, and the first run's claim
        # is still the one held.
        assert len(env.preview_items()) == before
        assert len(env.invocations()) == 1
        assert env.lock_item()["run_id"] == first["run_id"]
