"""
Preview_API routes in dda_labeling.py (llm-autolabel-prompt-tuning,
task 8.7).

Feature: llm-autolabel-prompt-tuning

Example-based coverage of `POST /labeling-preview/runs` (task 8.2) and
`GET /labeling-preview/runs/{runId}` (task 8.3), driven through
`dda_labeling.handler` with synthetic API Gateway events against the
moto-backed stack from conftest.py (real shared_utils / rbac_middleware,
the `CreateJobEnv` seeding harness, `FakeCognitoClient` and
`FakeLambdaClient` from test_dda_labeling_create_job.py):

- 403 with the fixed `{'error': 'Not authorized'}` body on both routes,
  byte-identical whether or not the Use_Case exists, carrying none of
  @rbac_check's own permission/scope detail (Req 8.2)
- `usecase_id`-not-found reported only *after* authorization: the same
  request is a 403 for an unauthorized caller and a 400 naming the
  Use_Case for an authorized one (Req 8.2, 8.6)
- 400 `{'error': 'Preview run validation failed', 'validation_errors':
  [...]}` enumerating every violation together, with nothing persisted,
  no lock claimed and no executor invoked (Req 8.4)
- 202 `{run_id, sample_count, status: 'Running'}` with the RUN item, one
  Pending IMAGE#{i} item per sample, the `preview_run` audit event's
  fields, and the `{action: 'execute_preview_run', run_id}` async
  self-invoke resolved from `context.function_name` (Req 3.8, 8.1)
- A failed async invoke: 202 reporting `status: 'Failed'` with
  `run_error`, the run item flipped to Failed and the lock released
- 409 `{'error': 'A preview run is already in progress for this use
  case'}` for a second in-flight run, the lock's
  `min(sample_count * 120 + 60, 900)` TTL, an *expired* lock allowing a
  later run, and per-user scoping (Req 8.8)
- GET 200 body shape: Pending entries carrying exactly index/sample_key/
  state, resolved entries adding resolved_at, the presigned result URL
  and its 900-second expiry, failures adding category and reason,
  few-shot attached/omitted counts, and `run_error` only when the RUN
  item carries one (Req 3.5, 4.6)
- GET 404 `{'error': 'Preview run not found'}` byte-identical for an
  unknown run id and for a run created by another user
- The 500 envelopes of both routes, with the start route releasing the
  lock it had claimed

The Preview_Run executor (task 9.1) is deliberately never exercised:
the self-invoke goes to `FakeLambdaClient`, which only records it, so
per-sample resolution is simulated by calling the state helpers
directly.

Spec: llm-model-token-and-image-sizing, task 7.4 (sizing inputs on the
same two routes). Extends the same harness — every pre-existing case
above holds unchanged:

- The `downscale_max_edge` and `token_budget` validation branches, each
  rejection carrying its fixed message (the six permitted values / the
  accepted range), singly and combined with a pre-existing rule in the
  one all-rules-evaluated pass, with nothing persisted, no lock and no
  executor invoked (Req 5.5, 3.5)
- The RUN item's two new attributes: the validated Max_Image_Edge
  (absent for Downscale_Off) and the resolved Effective_Token_Budget
  (Req 5.3, 1.6)
- The single `preview_run` audit event's `downscale_max_edge` (null for
  Off) and `token_budget` details (Req 9.5)
- The status response's `downscale_max_edge` (null for Off) and
  `token_budget` fields (Req 5.10)
- A run started with the budget key omitted resolving the shared-layer
  default of 10000 everywhere the budget is reported (Req 3.10, 1.6)
"""
import json
import sys
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    POOL_ID,
    REGION,
    CreateJobEnv,
    FakeCognitoClient,
    FakeLambdaClient,
)

LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
PROMPT = "Find every visible surface defect"
FUNCTION_NAME = "test-dda-labeling"
ARTIFACTS_BUCKET = "test-portal-artifacts"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients (the create-job convention)."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

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


class PreviewEnv(CreateJobEnv):
    """CreateJobEnv (fresh Use_Case, dataset prefix, authorized creator)
    plus Preview_API request helpers and preview-state readers."""

    def __init__(self, stack, dda):
        super().__init__(stack, dda)
        self.module = dda.module
        self.tasks = stack.tables.labeling_tasks
        self.context = SimpleNamespace(function_name=FUNCTION_NAME)
        self.dda.lambda_client.invocations.clear()
        self._preview_baseline = len(self.preview_items())

    # ------------------------------------------------------------ bodies
    def preview_body(self, **overrides):
        base = {
            "usecase_id": self.usecase_id,
            "dataset_prefix": self.prefix,
            "model": LLM_MODEL,
            "detection_prompt": PROMPT,
            "task_type": "Classification",
            "sample_images": [f"{self.prefix}a.jpg"],
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def samples(self, count):
        return [f"{self.prefix}sample{i}.jpg" for i in range(count)]

    def few_shot(self, good=1, bad=1):
        examples = []
        for position in range(good):
            examples.append({"ref": f"labeling-examples/good{position}.jpg",
                             "designation": "good", "position": position})
        for position in range(bad):
            examples.append({"ref": f"labeling-examples/bad{position}.png",
                             "designation": "bad", "position": position})
        return {"enabled": True, "examples": examples}

    # ------------------------------------------------------------ events
    @staticmethod
    def _claims(user):
        return {"requestContext": {"authorizer": {"claims": {
            "sub": user["user_id"],
            "email": user["email"],
            "cognito:username": user["username"],
            "custom:role": user["role"],
        }}}}

    def start_raw(self, user=None, context=..., **overrides):
        """The raw handler response for POST /labeling-preview/runs."""
        body = self.preview_body(**overrides)
        event = {
            "httpMethod": "POST",
            "resource": "/labeling-preview/runs",
            "path": "/v1/labeling-preview/runs",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": json.dumps(body),
        }
        event.update(self._claims(user or self.creator))
        return self.module.handler(
            event, self.context if context is ... else context)

    def start(self, user=None, context=..., **overrides):
        response = self.start_raw(user=user, context=context, **overrides)
        return response["statusCode"], json.loads(response["body"])

    def status_raw(self, run_id, user=None):
        """The raw handler response for GET /labeling-preview/runs/{runId}."""
        event = {
            "httpMethod": "GET",
            "resource": "/labeling-preview/runs/{runId}",
            "path": f"/v1/labeling-preview/runs/{run_id}",
            "pathParameters": {"runId": run_id},
            "queryStringParameters": None,
            "body": None,
        }
        event.update(self._claims(user or self.creator))
        return self.module.handler(event, self.context)

    def status(self, run_id, user=None):
        response = self.status_raw(run_id, user=user)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------------- store
    def preview_items(self):
        items, kwargs = [], {
            "FilterExpression": Attr("job_id").begins_with("PREVIEW#")}
        while True:
            response = self.tasks.scan(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            kwargs["ExclusiveStartKey"] = last_key

    def run_item(self, run_id):
        return self.tasks.get_item(
            Key={"job_id": f"PREVIEW#{run_id}", "task_id": "RUN"}).get("Item")

    def sample_items(self, run_id):
        return self.tasks.query(
            KeyConditionExpression=(
                Key("job_id").eq(f"PREVIEW#{run_id}")
                & Key("task_id").begins_with("IMAGE#"))).get("Items", [])

    def lock_item(self, user=None):
        user = user or self.creator
        return self.tasks.get_item(Key={
            "job_id": f"PREVIEWLOCK#{self.usecase_id}",
            "task_id": f"USER#{user['user_id']}"}).get("Item")

    def put_expired_lock(self, user=None, run_id="preview-stale"):
        """An already-expired lock item, written directly rather than
        waiting for a real TTL to pass."""
        user = user or self.creator
        now = self.module._now_epoch()
        self.tasks.put_item(Item={
            "job_id": f"PREVIEWLOCK#{self.usecase_id}",
            "task_id": f"USER#{user['user_id']}",
            "run_id": run_id,
            "claimed_at": now - 1000,
            "expires_at": now - 10,
            "ttl": now + 3600,
        })

    def resolve_sample(self, run_id, index, state, payload,
                       failure_category=None, failure_reason=None):
        """Stand in for the executor: write the result payload and
        resolve the sample item, without invoking any model."""
        key = self.module._write_preview_result_payload(
            self.usecase_id, run_id, index, payload)
        self.module._update_preview_sample_state(
            run_id, index, state, failure_category=failure_category,
            failure_reason=failure_reason, result_s3_key=key)
        return key

    def invocations(self):
        return self.dda.lambda_client.invocations

    def assert_no_preview_state(self):
        """No run/sample items, no lock, no executor invoke and no
        preview_run audit event were produced."""
        assert len(self.preview_items()) == self._preview_baseline
        assert self.lock_item() is None
        assert self.invocations() == []
        assert self.audit_events("preview_run") == []


@pytest.fixture
def env(aws_stack, dda):
    return PreviewEnv(aws_stack, dda)


def viewer(env):
    return env.make_user(role="Viewer")


def parameters(body):
    return {err["parameter"] for err in body["validation_errors"]}


# ----------------------------------------------------------- authorization

class TestStartAuthorization:
    def test_viewer_denied_with_fixed_body(self, env):
        """Req 8.2: one fixed 403 body carrying no permission list, no
        scope and no dataset content; nothing is persisted."""
        response = env.start_raw(user=viewer(env))
        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "Not authorized"}
        env.assert_no_preview_state()

    def test_denial_is_audited(self, env):
        """The @rbac_check `unauthorized_access` event records the
        denial the flattened body no longer describes."""
        status, _ = env.start(user=viewer(env))
        assert status == 403
        events = env.audit_events("unauthorized_access")
        assert len(events) == 1
        assert events[0]["result"] == "denied"

    def test_403_identical_for_unknown_usecase(self, env):
        """Req 8.2: the denial discloses nothing about whether the
        target Use_Case exists — both responses are byte-identical."""
        denied = viewer(env)
        existing = env.start_raw(user=denied)
        unknown = env.start_raw(user=denied,
                                usecase_id=f"uc-{uuid.uuid4()}")
        assert existing["statusCode"] == unknown["statusCode"] == 403
        assert existing["body"] == unknown["body"]

    def test_usecase_not_found_only_after_authorization(self, env):
        """Req 8.6: the same unknown-Use_Case request is a 403 for an
        unauthorized caller and only becomes a 400 naming the Use_Case
        once the caller holds the permission."""
        unknown_id = f"uc-{uuid.uuid4()}"

        status, body = env.start(user=viewer(env), usecase_id=unknown_id)
        assert status == 403
        assert "validation_errors" not in body

        status, body = env.start(usecase_id=unknown_id)
        assert status == 400
        assert "usecase_id" in parameters(body)
        assert any("Use case not found" in err["message"]
                   for err in body["validation_errors"])
        env.assert_no_preview_state()


# ------------------------------------------------------------- validation

class TestStartValidation:
    def test_error_envelope(self, env):
        """Req 8.4: the fixed 400 envelope, each entry naming its
        parameter and message."""
        status, body = env.start(detection_prompt="   ")
        assert status == 400
        assert body["error"] == "Preview run validation failed"
        assert body["validation_errors"]
        for err in body["validation_errors"]:
            assert isinstance(err["parameter"], str)
            assert isinstance(err["message"], str)
        env.assert_no_preview_state()

    def test_every_violation_enumerated_together(self, env):
        """Req 8.4: one response, every violated rule — not the first."""
        status, body = env.start(
            model="bedrock:anthropic.claude-3-haiku",
            detection_prompt="",
            task_type="Pose",
            sample_images=env.samples(6),
        )
        assert status == 400
        assert {"model", "detection_prompt", "task_type",
                "sample_images"} <= parameters(body)
        env.assert_no_preview_state()

    @pytest.mark.parametrize("overrides,parameter", [
        ({"model": "sam"}, "model"),
        ({"detection_prompt": "x" * 2001}, "detection_prompt"),
        ({"sample_images": []}, "sample_images"),
        ({"dataset_prefix": None}, "dataset_prefix"),
        ({"task_type": "ObjectDetection", "label_set": []}, "label_set"),
    ])
    def test_single_rule_rejections(self, env, overrides, parameter):
        status, body = env.start(**overrides)
        assert status == 400
        assert parameter in parameters(body)
        env.assert_no_preview_state()

    def test_out_of_scope_samples_identified(self, env):
        """Req 8.3, 8.7: each out-of-scope reference is named after
        resolution, and nothing is read."""
        status, body = env.start(sample_images=[
            f"{env.prefix}inside.jpg",
            "s3://other-bucket/elsewhere.jpg",
            "outside-the-prefix/x.jpg",
        ])
        assert status == 400
        offenders = {err.get("sample_image")
                     for err in body["validation_errors"]
                     if err["parameter"] == "sample_images"}
        assert offenders == {"s3://other-bucket/elsewhere.jpg",
                             "outside-the-prefix/x.jpg"}
        env.assert_no_preview_state()

    def test_few_shot_enabled_without_examples_rejected(self, env):
        status, body = env.start(few_shot={"enabled": True, "examples": []})
        assert status == 400
        assert "few_shot" in parameters(body)
        env.assert_no_preview_state()

    def test_unparseable_body_reports_missing_parameters(self, env):
        """A body that is not a JSON object reads as empty, so the
        missing elements are reported rather than a parse error."""
        event = {
            "httpMethod": "POST",
            "resource": "/labeling-preview/runs",
            "path": "/v1/labeling-preview/runs",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": "not json",
        }
        event.update(env._claims(env.creator))
        response = env.module.handler(event, env.context)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "Preview run validation failed"
        assert {"usecase_id", "model", "detection_prompt",
                "sample_images"} <= parameters(body)


# --------------------------------------------------------------- 202 start

class TestStartSuccess:
    def test_accepted_response_and_state(self, env):
        """Req 8.1: 202 {run_id, sample_count, status} with the RUN item
        and one Pending sample item per requested Sample_Image."""
        status, body = env.start(sample_images=env.samples(3))
        assert status == 202
        assert set(body) == {"run_id", "sample_count", "status"}
        assert body["sample_count"] == 3
        assert body["status"] == "Running"

        run = env.run_item(body["run_id"])
        assert run["status"] == "Running"
        assert run["usecase_id"] == env.usecase_id
        assert run["created_by"] == env.creator["user_id"]
        assert run["model"] == LLM_MODEL
        assert run["detection_prompt"] == PROMPT
        assert int(run["sample_count"]) == 3

        items = env.sample_items(body["run_id"])
        assert [item["task_id"] for item in items] == [
            "IMAGE#000", "IMAGE#001", "IMAGE#002"]
        assert {item["state"] for item in items} == {"Pending"}
        assert [item["sample_key"] for item in items] == env.samples(3)
        # Req 1.6: preview items must stay out of assignee-index.
        assert all("assignee_user_id" not in item for item in items)

    def test_audit_event_fields(self, env):
        """Req 3.8: identity, Use_Case, model and Sample_Image count."""
        status, body = env.start(sample_images=env.samples(2),
                                 few_shot=env.few_shot(good=1, bad=1))
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
        assert details["model"] == LLM_MODEL
        assert int(details["sample_count"]) == 2
        assert details["task_type"] == "Classification"
        assert details["few_shot_enabled"] is True
        assert int(details["attached_example_count"]) == 2

    def test_executor_self_invoked_from_context(self, env):
        """The executor is the same function, async-invoked with the
        run id and the name from the invocation context."""
        status, body = env.start()
        assert status == 202

        invocations = env.invocations()
        assert len(invocations) == 1
        assert invocations[0]["FunctionName"] == FUNCTION_NAME
        assert invocations[0]["InvocationType"] == "Event"
        assert json.loads(invocations[0]["Payload"]) == {
            "action": "execute_preview_run", "run_id": body["run_id"]}

    def test_function_name_falls_back_to_environment(self, env, monkeypatch):
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "env-preview-fn")
        status, _ = env.start(context=None)
        assert status == 202
        assert env.invocations()[0]["FunctionName"] == "env-preview-fn"

    def test_no_function_name_still_records_the_run(self, env, monkeypatch):
        """Guard: with no resolvable function name the run is recorded
        and no invoke is attempted."""
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        status, body = env.start(context=None)
        assert status == 202
        assert body["status"] == "Running"
        assert env.invocations() == []
        assert env.run_item(body["run_id"])["status"] == "Running"

    def test_failed_invoke_reports_failed_and_releases_lock(self, env,
                                                            monkeypatch):
        """A self-invoke that raises flips the run to Failed with its
        reason and gives the lock back immediately."""
        def raising_invoke(**kwargs):
            raise RuntimeError("invoke rejected")

        monkeypatch.setattr(env.dda.lambda_client, "invoke", raising_invoke)

        status, body = env.start()
        assert status == 202
        assert body["status"] == "Failed"
        assert "invoke rejected" in body["run_error"]

        run = env.run_item(body["run_id"])
        assert run["status"] == "Failed"
        assert "invoke rejected" in run["run_error"]
        assert env.lock_item() is None

    def test_internal_failure_releases_the_lock(self, env, monkeypatch):
        """The 500 envelope, and no claim left behind for a run that
        will never execute."""
        def raising_write(**kwargs):
            raise RuntimeError("dynamo down")

        monkeypatch.setattr(env.module, "_write_preview_run_item",
                            raising_write)

        status, body = env.start()
        assert status == 500
        assert body == {"error": "Failed to start the preview run"}
        assert env.lock_item() is None
        assert env.invocations() == []


# ------------------------------------------------------------- concurrency

class TestInFlightLock:
    def test_second_run_rejected_with_409(self, env):
        """Req 8.8: one in-flight run per user and Use_Case, and the
        rejected request creates nothing."""
        status, first = env.start()
        assert status == 202

        before = len(env.preview_items())
        response = env.start_raw()
        assert response["statusCode"] == 409
        assert json.loads(response["body"]) == {
            "error": "A preview run is already in progress for this use case"}
        assert len(env.preview_items()) == before
        assert len(env.invocations()) == 1
        assert env.lock_item()["run_id"] == first["run_id"]

    @pytest.mark.parametrize("sample_count", [1, 5])
    def test_lock_ttl_follows_the_sample_count(self, env, sample_count):
        """`min(sample_count * 120 + 60, 900)`."""
        status, _ = env.start(sample_images=env.samples(sample_count))
        assert status == 202
        lock = env.lock_item()
        expected = min(sample_count * 120 + 60, 900)
        assert int(lock["expires_at"]) - int(lock["claimed_at"]) == expected
        assert int(lock["ttl"]) > int(lock["expires_at"])

    def test_expired_lock_allows_a_later_run(self, env):
        """An expired-but-unreaped claim must not wedge the Use_Case:
        the conditional write compares expires_at explicitly."""
        env.put_expired_lock(run_id="preview-stale")

        status, body = env.start()
        assert status == 202
        assert env.lock_item()["run_id"] == body["run_id"]

    def test_lock_is_per_user(self, env):
        """The claim is scoped to the user: a second Job_Creator can
        preview the same Use_Case concurrently."""
        status, _ = env.start()
        assert status == 202

        other = env.make_user(role="DataScientist")
        status, body = env.start(user=other)
        assert status == 202
        assert env.lock_item(user=other)["run_id"] == body["run_id"]


# ----------------------------------------------------------- status route

def prelabel_payload(sample_key):
    return {"sample_key": sample_key, "state": "Succeeded",
            "prelabel": {"label": "anomaly"},
            "image_width": 640, "image_height": 480}


class TestStatusRoute:
    def test_pending_entries_carry_only_index_key_and_state(self, env):
        """Req 3.5, 4.6: one entry per requested Sample_Image in request
        order from the moment the run exists, with no half-populated
        outcome."""
        _, started = env.start(sample_images=env.samples(3))
        status, body = env.status(started["run_id"])
        assert status == 200
        assert body["run_id"] == started["run_id"]
        assert body["status"] == "Running"
        assert body["sample_count"] == 3
        assert body["few_shot"] == {"enabled": False, "attached": 0,
                                    "omitted": 0}
        assert "run_error" not in body
        assert body["results"] == [
            {"index": index, "sample_key": key, "state": "Pending"}
            for index, key in enumerate(env.samples(3))
        ]

    def test_resolved_entry_carries_presigned_result_url(self, env):
        """A resolved sample adds its resolution time and a presigned,
        15-minute, single-object result URL."""
        _, started = env.start(sample_images=env.samples(2))
        run_id = started["run_id"]
        key = env.resolve_sample(run_id, 0, "Succeeded",
                                 prelabel_payload(env.samples(2)[0]))

        status, body = env.status(run_id)
        assert status == 200
        resolved, pending = body["results"]
        assert pending["state"] == "Pending"
        assert resolved["state"] == "Succeeded"
        assert resolved["index"] == 0
        assert isinstance(resolved["resolved_at"], int)
        assert resolved["result_url_expires_in"] == 900

        parsed = urlparse(resolved["result_url"])
        query = parse_qs(parsed.query)
        assert key in parsed.path  # scoped to exactly this one payload
        assert ARTIFACTS_BUCKET in parsed.netloc + parsed.path
        # 15 minutes, however the signer spells it (sigv4 carries a
        # relative X-Amz-Expires, sigv2 an absolute Expires).
        if "X-Amz-Expires" in query:
            assert query["X-Amz-Expires"] == ["900"]
        else:
            lifetime = int(query["Expires"][0]) - env.module._now_epoch()
            assert 840 <= lifetime <= 900

        payload = json.loads(boto3.client("s3", region_name=REGION)
                             .get_object(Bucket=ARTIFACTS_BUCKET, Key=key)
                             ["Body"].read())
        assert payload == prelabel_payload(env.samples(2)[0])

    def test_failed_entry_carries_category_and_reason(self, env):
        """Req 9.6: the failure category and reason ride the status
        response, so a failure renders without fetching its payload."""
        _, started = env.start()
        run_id = started["run_id"]
        env.resolve_sample(
            run_id, 0, "Failed",
            {"sample_key": env.preview_body()["sample_images"][0],
             "state": "Failed", "failure_category": "unusable_model_output",
             "failure_reason": "no parseable JSON in the model response",
             "raw_model_output": "I cannot help with that."},
            failure_category="unusable_model_output",
            failure_reason="no parseable JSON in the model response")

        status, body = env.status(run_id)
        assert status == 200
        entry = body["results"][0]
        assert entry["state"] == "Failed"
        assert entry["failure_category"] == "unusable_model_output"
        assert entry["failure_reason"] == (
            "no parseable JSON in the model response")
        assert entry["result_url_expires_in"] == 900

    def test_few_shot_counts_reported(self, env):
        """Req 7.5: the attached/omitted counts recorded at start time."""
        _, started = env.start(few_shot=env.few_shot(good=2, bad=1))
        status, body = env.status(started["run_id"])
        assert status == 200
        assert body["few_shot"] == {"enabled": True, "attached": 3,
                                    "omitted": 0}

    def test_run_error_reported_only_when_recorded(self, env):
        """`run_error` appears exactly when the RUN item carries one."""
        _, clean = env.start()
        _, body = env.status(clean["run_id"])
        assert "run_error" not in body

        env.module._update_preview_run_status(
            clean["run_id"], "Failed", run_error="the executor never started")
        status, body = env.status(clean["run_id"])
        assert status == 200
        assert body["status"] == "Failed"
        assert body["run_error"] == "the executor never started"

    def test_unknown_and_foreign_run_ids_are_indistinguishable(self, env):
        """One fixed 404 body for an unknown run and for a run created
        by another user, with no run data in either."""
        _, started = env.start()

        unknown = env.status_raw(f"preview-{uuid.uuid4().hex[:8]}")
        foreign = env.status_raw(started["run_id"],
                                 user=env.make_user(role="DataScientist"))
        assert unknown["statusCode"] == foreign["statusCode"] == 404
        assert json.loads(unknown["body"]) == {
            "error": "Preview run not found"}
        assert unknown["body"] == foreign["body"]

    def test_viewer_denied_with_fixed_body(self, env):
        """The status route flattens @rbac_check's 403 too, so the
        run's Use_Case is not described to a caller who cannot see it."""
        _, started = env.start()
        response = env.status_raw(started["run_id"], user=viewer(env))
        assert response["statusCode"] == 403
        assert json.loads(response["body"]) == {"error": "Not authorized"}

    def test_read_failure_returns_500(self, env, monkeypatch):
        _, started = env.start()

        def raising_read(run_id):
            raise RuntimeError("query failed")

        monkeypatch.setattr(env.module, "_read_preview_sample_items",
                            raising_read)
        status, body = env.status(started["run_id"])
        assert status == 500
        assert body == {"error": "Failed to read the preview run"}


# ----------------------------------------- sizing inputs (task 7.4)
# Spec: llm-model-token-and-image-sizing — Req 3.5, 3.10, 5.5, 5.10,
# 9.5, plus 5.3/1.6 for the RUN item's recorded values.

DOWNSCALE_MESSAGE = ("downscale_max_edge must be null for no downscaling "
                     "or one of 512, 768, 1024, 1280, 1536, 2048")
TOKEN_BUDGET_MESSAGE = ("token_budget must be a whole number between 1 "
                        "and 128000")
# The shared-layer Model_Token_Limit_Default: a run started with no
# usable Token_Budget_Selection and no mapping entry must record
# exactly this budget (Req 3.10).
DEFAULT_TOKEN_BUDGET = 10000


def start_with_body(env, body):
    """POST /labeling-preview/runs with an explicit body dict.

    `preview_body` drops None-valued overrides, so explicit JSON nulls
    (the wizard's blank downscale select, a null budget) can only reach
    validation through a hand-built event."""
    event = {
        "httpMethod": "POST",
        "resource": "/labeling-preview/runs",
        "path": "/v1/labeling-preview/runs",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body),
    }
    event.update(env._claims(env.creator))
    response = env.module.handler(event, env.context)
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def budget_env(env, monkeypatch):
    """`env` pinned to the environment-bootstrap Model_Token_Limits
    source with no settings-table read and no inherited mapping (the
    sizing_env convention from test_dda_autolabel_worker_few_shot.py),
    so budget resolution lands on the shared-layer default."""
    monkeypatch.setattr(env.module, "SETTINGS_TABLE", None)
    monkeypatch.delenv("LLM_MODEL_TOKEN_LIMITS", raising=False)
    return env


class TestSizingValidation:
    """Req 5.5, 3.5: the two sizing rules join the single
    all-rules-evaluated pass; every rejection persists nothing, claims
    no lock and invokes no executor."""

    @pytest.mark.parametrize("bad_downscale", [
        pytest.param(True, id="boolean"),
        pytest.param("1024", id="digit-string"),
        pytest.param("off", id="word-string"),
        pytest.param(1024.0, id="whole-float"),
        pytest.param(1023, id="off-by-one"),
        pytest.param(4096, id="not-an-option"),
    ])
    def test_invalid_downscale_rejected_listing_the_options(self, env,
                                                            bad_downscale):
        """Req 5.5: a boolean, string, float or out-of-set integer is
        rejected with the message listing the six permitted values."""
        status, body = env.start(downscale_max_edge=bad_downscale)
        assert status == 400
        errors = [err for err in body["validation_errors"]
                  if err["parameter"] == "downscale_max_edge"]
        assert len(errors) == 1
        assert errors[0]["message"] == DOWNSCALE_MESSAGE
        env.assert_no_preview_state()

    @pytest.mark.parametrize("bad_budget", [
        pytest.param(True, id="boolean"),
        pytest.param("20000", id="digit-string"),
        pytest.param(20000.0, id="whole-float"),
        pytest.param(0, id="below-range"),
        pytest.param(128001, id="above-ceiling"),
    ])
    def test_invalid_token_budget_rejected_naming_the_range(self, env,
                                                            bad_budget):
        """Req 3.5: a present-and-invalid budget is rejected with the
        accepted range, with no numeric conversion and no clamping."""
        status, body = env.start(token_budget=bad_budget)
        assert status == 400
        errors = [err for err in body["validation_errors"]
                  if err["parameter"] == "token_budget"]
        assert len(errors) == 1
        assert errors[0]["message"] == TOKEN_BUDGET_MESSAGE
        env.assert_no_preview_state()

    def test_null_downscale_accepted_null_budget_rejected(self, env):
        """The two nulls diverge by design: null downscale_max_edge is
        Downscale_Off, while an empty budget control omits the key
        entirely — a present null budget is a violation."""
        body = env.preview_body()
        body["downscale_max_edge"] = None
        body["token_budget"] = None
        status, response = start_with_body(env, body)
        assert status == 400
        assert parameters(response) == {"token_budget"}
        env.assert_no_preview_state()

    def test_sizing_violations_enumerated_with_existing_rules(self, env):
        """Req 3.5, 5.5: both sizing violations and a pre-existing
        rule's ride one response — the single pass, not the first
        offender."""
        status, body = env.start(downscale_max_edge=640, token_budget=-1,
                                 detection_prompt="")
        assert status == 400
        assert {"downscale_max_edge", "token_budget",
                "detection_prompt"} <= parameters(body)
        env.assert_no_preview_state()

    @pytest.mark.parametrize("overrides", [
        pytest.param({"downscale_max_edge": 512}, id="smallest-edge"),
        pytest.param({"downscale_max_edge": 2048}, id="largest-edge"),
        pytest.param({"token_budget": 1}, id="budget-floor"),
        pytest.param({"token_budget": 128000}, id="budget-ceiling"),
    ])
    def test_boundary_sizing_values_accepted(self, env, overrides):
        status, _ = env.start(**overrides)
        assert status == 202


class TestSizingRunState:
    """Req 5.3, 1.6, 9.5, 5.10: the validated Downscale_Setting and the
    resolved Effective_Token_Budget ride the RUN item, the single audit
    event and the status response."""

    def test_run_item_records_the_setting_and_the_resolved_budget(self,
                                                                  env):
        """A valid Token_Budget_Selection wins every resolution tier, so
        the recorded Effective_Token_Budget is the selection itself."""
        status, body = env.start(downscale_max_edge=1024,
                                 token_budget=20000)
        assert status == 202
        run = env.run_item(body["run_id"])
        assert int(run["downscale_max_edge"]) == 1024
        assert int(run["token_budget"]) == 20000

    def test_downscale_off_leaves_the_run_attribute_absent(self, env):
        """A run without a bound reads exactly like a pre-feature RUN
        item: the attribute is absent, not null."""
        status, body = env.start(token_budget=20000)
        assert status == 202
        run = env.run_item(body["run_id"])
        assert "downscale_max_edge" not in run
        assert int(run["token_budget"]) == 20000

    def test_audit_event_carries_both_sizing_details(self, env):
        """Req 9.5: still exactly one preview_run event, now carrying
        the applied Downscale_Setting and the Effective_Token_Budget."""
        status, _ = env.start(downscale_max_edge=768, token_budget=5000)
        assert status == 202
        events = env.audit_events("preview_run")
        assert len(events) == 1
        details = events[0]["details"]
        assert int(details["downscale_max_edge"]) == 768
        assert int(details["token_budget"]) == 5000

    def test_audit_event_records_null_downscale_for_an_off_run(self, env):
        status, _ = env.start(token_budget=5000)
        assert status == 202
        events = env.audit_events("preview_run")
        assert len(events) == 1
        assert events[0]["details"]["downscale_max_edge"] is None
        assert int(events[0]["details"]["token_budget"]) == 5000

    def test_status_reports_both_sizing_fields(self, env):
        """Req 5.10: the wizard reads the applied setting and budget off
        the same status poll it already makes."""
        _, started = env.start(downscale_max_edge=2048, token_budget=64000)
        status, body = env.status(started["run_id"])
        assert status == 200
        assert body["downscale_max_edge"] == 2048
        assert body["token_budget"] == 64000

    def test_status_reports_null_downscale_for_an_off_run(self, env):
        _, started = env.start(token_budget=64000)
        status, body = env.status(started["run_id"])
        assert status == 200
        assert body["downscale_max_edge"] is None
        assert body["token_budget"] == 64000

    def test_empty_budget_omits_the_key_and_resolves_the_default(
            self, budget_env):
        """Req 3.10, 1.6: a run started with the budget key omitted
        resolves the shared-layer default of 10000, recorded on the RUN
        item, audited, and reported by the status route."""
        env = budget_env
        assert "token_budget" not in env.preview_body()

        status, started = env.start()
        assert status == 202
        run = env.run_item(started["run_id"])
        assert int(run["token_budget"]) == DEFAULT_TOKEN_BUDGET

        events = env.audit_events("preview_run")
        assert len(events) == 1
        assert int(events[0]["details"]["token_budget"]) == (
            DEFAULT_TOKEN_BUDGET)

        status, body = env.status(started["run_id"])
        assert status == 200
        assert body["token_budget"] == DEFAULT_TOKEN_BUDGET
