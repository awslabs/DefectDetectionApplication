"""
create_dda_job validation, enumeration, and persistence in
dda_labeling.py (dda-data-labeling, task 5.3).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils / rbac path, moto DynamoDB + S3, fake Cognito for member
role resolution — the test_dda_labeling_teams.py convention),
calling `create_dda_job(body, user)` directly (as labeling.py's
backend switch, task 5.1, will):

- Parameter validation before enumeration, each rejection identifying
  its offending elements and persisting nothing (Req 4.1, 4.2, 4.4,
  4.8, 4.9, 4.10): name length/uniqueness, modality, Label_Set,
  missing/empty team, instructions length, example image count/format
- Fixed ['normal','anomaly'] Label_Set for Classification (Req 4.3)
- Dataset enumeration with nested prefixes via get_s3_client_for_bucket
  (single-account direct fallback); non-image objects skipped and counted
  rather than rejected, a prefix yielding zero images rejected (Req 4.5,
  4.6, 4.7, 12.1-12.3)
- Auto-label model/modality compatibility matrix (Req 8.1, 8.8)
- Skip-verification: admin-only (403 + audit for non-admins, Req 9.1),
  Bedrock model + per-label prompts covering every label (Req 9.2, 9.3)
- Success: job persisted with status=InProgress, labeling_backend=DDA,
  image_count, submitted fields, submitted_count=0, blocked=false
  (Req 4.11, 11.3, 12.8); job_created audit event (Req 11.7);
  async worker invoke {action: distribute, job_id} guarded on the
  DDA_LABELING_WORKER_FUNCTION_NAME env var

Feature: llm-auto-labeling (task 6.2) adds, against the same stack:

- 'llm:<id>' accepted for Classification, Segmentation, and
  ObjectDetection (Req 1.3); identifier rejections (empty, 257 chars,
  embedded space, control char) each naming the model parameter and
  persisting no job or task items (Req 1.5)
- detection_prompt rejections (absent, empty, whitespace-only, 2001
  chars) each naming the prompt, persisting nothing (Req 2.2-2.4);
  the accepted prompt stored byte-identical (Req 2.5, 1.6)
- job_created audit details carry auto_label_model and auto_label_mode
  ('llm' | 'sam' | 'bedrock' | 'none') (Req 9.4)
- skip-verification with an llm: model still requires per-label
  prompts (Req 2.6), still 403s non-admins with the unauthorized_access
  audit event before validation errors are assembled (Req 9.3, 9.1),
  and a caller without the create permission is rejected through the
  existing rbac_check gate with nothing persisted (Req 9.2)

Feature: llm-model-token-and-image-sizing (task 7.4) adds, against the
same stack:

- `auto_label.downscale_max_edge` and `auto_label.token_budget`
  persisted unchanged for the `llm:` family when submitted (Req 5.7,
  3.6); absent when not submitted, and a null downscale (the wizard's
  blank select) left absent too, so an unconfigured submission's record
  is byte-identical to a pre-feature record (Req 10.6); never written
  for `sam` or `bedrock:` jobs, even when planted on the submission
  (Req 10.4)

Feature: grounded-sam-autolabel (task 2.3) adds, against the same
stack (new tests only — every pre-existing assertion untouched):

- 'grounded-sam' + Classification rejected with a validation error
  identifying the model value and the modality, persisting nothing
  (Req 1.6)
- job_created audit details carry auto_label_model 'grounded-sam' and
  auto_label_mode 'grounded-sam' (Req 1.7)
- creation accepted while no grounded-sam worker is deployed — job
  creation has no worker dependency, whatever the worker env says
  (Req 5.4)
"""
import json
import os
import re
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

REGION = "us-east-1"
POOL_ID = "us-east-1_dda-create-test-pool"
DATASET_BUCKET = "test-usecase-data"


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
    """Records async invocations of dda_labeling_worker."""

    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock, with
    fake Cognito and Lambda clients, plus the dataset bucket."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID

    fake_lambda = FakeLambdaClient()
    dda_labeling.lambda_client = fake_lambda

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=DATASET_BUCKET)

    return SimpleNamespace(module=dda_labeling, cognito=fake_cognito,
                           lambda_client=fake_lambda)


@pytest.fixture
def env(aws_stack, dda):
    """Per-test helper facade with a fresh Use_Case, dataset prefix, and
    a labeling team with one Data_Labeler member."""
    return CreateJobEnv(aws_stack, dda)


class CreateJobEnv:
    def __init__(self, stack, dda):
        self.stack = stack
        self.dda = dda
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.prefix = f"datasets/{uuid.uuid4()}/"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Create Job Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.creator = self.make_user(role="DataScientist")
        self.team_id = self.make_team(with_labeler=True)
        # Baseline of the (shared) tasks table so rejections can assert
        # zero task items were added (llm-auto-labeling Req 1.5, 2.3, 2.4).
        self._task_baseline = self._count_task_items()

    # ------------------------------------------------------------ setup
    def make_user(self, role="DataScientist"):
        user_id = f"user-{uuid.uuid4()}"
        return {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": role,
        }

    def make_team(self, with_labeler=True, usecase_id=None):
        team_id = f"team-{uuid.uuid4()}"
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id,
            "sk": "META",
            "usecase_id": usecase_id or self.usecase_id,
            "team_name": f"Team {team_id[:13]}",
            "created_at": 1,
            "created_by": self.creator["user_id"],
        })
        if with_labeler:
            self.add_labeler(team_id)
        return team_id

    def add_labeler(self, team_id, role="DataLabeler"):
        username = f"labeler-{uuid.uuid4()}"
        email = f"{username}@example.com"
        sub = self.dda.cognito.add_user(username, email, role=role)
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id,
            "sk": f"MEMBER#{sub}",
            "user_id": sub,
            "email": email,
            "added_at": 1,
            "added_by": self.creator["user_id"],
        })
        return SimpleNamespace(username=username, sub=sub, email=email)

    def put_images(self, keys):
        for key in keys:
            self.s3.put_object(Bucket=DATASET_BUCKET,
                               Key=f"{self.prefix}{key}", Body=b"fakeimage")

    # ------------------------------------------------------------ invoke
    def body(self, **overrides):
        base = {
            "usecase_id": self.usecase_id,
            "job_name": f"job-{uuid.uuid4().hex[:12]}",
            "dataset_prefix": self.prefix,
            "task_type": "Classification",
            "team_id": self.team_id,
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def create(self, user=None, **overrides):
        response = self.dda.module.create_dda_job(
            self.body(**overrides), user or self.creator)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------------- store
    def usecase_jobs(self):
        response = self.stack.tables.labeling_jobs.query(
            IndexName="usecase-jobs-index",
            KeyConditionExpression=boto3.dynamodb.conditions.Key(
                "usecase_id").eq(self.usecase_id))
        return response.get("Items", [])

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def audit_events(self, action):
        response = self.stack.tables.audit_log.scan()
        return [item for item in response.get("Items", [])
                if item.get("action") == action
                and item.get("details", {}).get("usecase_id")
                == self.usecase_id]

    def _count_task_items(self):
        count = 0
        kwargs = {"Select": "COUNT"}
        while True:
            response = self.stack.tables.labeling_tasks.scan(**kwargs)
            count += response["Count"]
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return count
            kwargs["ExclusiveStartKey"] = last_key

    def assert_nothing_persisted(self):
        """Zero job items for the use case AND zero new task items."""
        assert self.usecase_jobs() == []
        assert self._count_task_items() == self._task_baseline


def messages(body):
    return " | ".join(err["message"] for err in body["validation_errors"])


# ------------------------------------------------------- successful create

class TestSuccessfulCreation:
    def test_creates_in_progress_job_with_all_fields(self, env):
        """Req 4.4, 4.5, 4.11, 11.3, 12.8: complete job record with
        status InProgress, image_count over nested prefixes, and every
        submitted field persisted."""
        env.put_images(["a.jpg", "b.PNG", "nested/deep/c.jpeg"])
        status, body = env.create(
            instructions="Label carefully",
            example_images={"good": ["ex/good1.jpg"],
                            "bad": ["ex/bad1.png"]},
        )
        assert status == 201
        assert body["status"] == "InProgress"
        assert body["labeling_backend"] == "DDA"
        assert body["image_count"] == 3

        job = env.get_job(body["job_id"])
        assert job["status"] == "InProgress"
        assert job["labeling_backend"] == "DDA"
        assert job["task_type"] == "Classification"
        assert job["label_set"] == ["normal", "anomaly"]
        assert job["image_count"] == 3
        assert job["dataset_prefix"] == env.prefix
        assert job["dataset_bucket"] == DATASET_BUCKET
        assert job["team_id"] == env.team_id
        assert job["instructions"] == "Label carefully"
        assert job["example_images"] == {"good": ["ex/good1.jpg"],
                                         "bad": ["ex/bad1.png"]}
        assert job["submitted_count"] == 0
        assert job["blocked"] is False
        assert job["skip_verification"] is False
        assert job["auto_label"] == {"enabled": False}
        assert job["created_by"] == env.creator["user_id"]

    def test_job_created_audit_event_written(self, env):
        """Req 11.7: a job_created audit event with the acting user."""
        env.put_images(["a.jpg"])
        status, body = env.create()
        assert status == 201
        events = env.audit_events("job_created")
        assert len(events) == 1
        assert events[0]["user_id"] == env.creator["user_id"]
        assert events[0]["resource_id"] == body["job_id"]

    def test_worker_invoked_async_with_distribute_action(
            self, env, monkeypatch):
        """The worker is async-invoked with {action: distribute, job_id}
        when DDA_LABELING_WORKER_FUNCTION_NAME is configured."""
        monkeypatch.setenv("DDA_LABELING_WORKER_FUNCTION_NAME",
                           "test-dda-worker")
        env.dda.lambda_client.invocations.clear()
        env.put_images(["a.jpg"])
        status, body = env.create()
        assert status == 201

        invocations = env.dda.lambda_client.invocations
        assert len(invocations) == 1
        assert invocations[0]["FunctionName"] == "test-dda-worker"
        assert invocations[0]["InvocationType"] == "Event"
        assert json.loads(invocations[0]["Payload"]) == {
            "action": "distribute", "job_id": body["job_id"]}

    def test_worker_env_unset_still_creates_job(self, env, monkeypatch):
        """Guard: without the worker env var the job is still created
        and no invoke is attempted."""
        monkeypatch.delenv("DDA_LABELING_WORKER_FUNCTION_NAME",
                           raising=False)
        env.dda.lambda_client.invocations.clear()
        env.put_images(["a.jpg"])
        status, _ = env.create()
        assert status == 201
        assert env.dda.lambda_client.invocations == []

    def test_segmentation_label_set_persisted(self, env):
        """Req 4.2: a valid Segmentation Label_Set is persisted in
        submitted order."""
        env.put_images(["a.png"])
        status, body = env.create(task_type="Segmentation",
                                  label_set=["scratch", "dent"])
        assert status == 201
        assert env.get_job(body["job_id"])["label_set"] == [
            "scratch", "dent"]

    def test_classification_label_set_fixed(self, env):
        """Req 4.3: Classification always uses ['normal','anomaly'],
        overriding any submitted label set."""
        env.put_images(["a.jpg"])
        status, body = env.create(task_type="Classification",
                                  label_set=["cat", "dog"])
        assert status == 201
        assert env.get_job(body["job_id"])["label_set"] == [
            "normal", "anomaly"]


# ------------------------------------------------------- validation errors

class TestParameterValidation:
    @pytest.mark.parametrize("bad_name", ["", "   ", "x" * 64, None])
    def test_bad_job_name_rejected(self, env, bad_name):
        """Req 4.1: name must be 1-63 characters."""
        env.put_images(["a.jpg"])
        status, body = env.create(job_name=bad_name)
        assert status == 400
        assert "between 1 and 63" in messages(body)
        env.assert_nothing_persisted()

    def test_job_name_63_chars_accepted(self, env):
        env.put_images(["a.jpg"])
        status, _ = env.create(job_name="x" * 63)
        assert status == 201

    def test_duplicate_job_name_in_usecase_rejected(self, env):
        """Req 4.1: job name unique among the Use_Case's jobs (both
        backends live in the same table)."""
        env.put_images(["a.jpg"])
        status, _ = env.create(job_name="dup-name")
        assert status == 201
        status, body = env.create(job_name="dup-name")
        assert status == 400
        assert "already exists" in messages(body)
        assert len(env.usecase_jobs()) == 1

    def test_invalid_modality_rejected(self, env):
        env.put_images(["a.jpg"])
        status, body = env.create(task_type="Pose")
        assert status == 400
        assert "modality" in messages(body).lower()
        env.assert_nothing_persisted()

    @pytest.mark.parametrize("bad_set,fragment", [
        (None, "required"),
        ([], "required"),
        (["a"] * 11, "at most 10"),
        (["ok", ""], "non-empty"),
        (["ok", "x" * 65], "exceeds 64"),
        (["dup", "dup"], "duplicated"),
    ])
    def test_bad_label_set_rejected(self, env, bad_set, fragment):
        """Req 4.2: 1-10 distinct non-empty names of at most 64 chars,
        each offense identified."""
        env.put_images(["a.jpg"])
        status, body = env.create(task_type="ObjectDetection",
                                  label_set=bad_set)
        assert status == 400
        assert fragment in messages(body)
        env.assert_nothing_persisted()

    def test_missing_team_rejected(self, env):
        """Req 4.1: team required when skip-verification is disabled."""
        env.put_images(["a.jpg"])
        status, body = env.create(team_id=None)
        assert status == 400
        assert "team is required" in messages(body)
        env.assert_nothing_persisted()

    def test_unknown_team_rejected(self, env):
        env.put_images(["a.jpg"])
        status, body = env.create(team_id=f"team-{uuid.uuid4()}")
        assert status == 400
        assert "not found" in messages(body)
        env.assert_nothing_persisted()

    def test_team_with_zero_members_rejected(self, env):
        """Req 4.8: empty team identified."""
        env.put_images(["a.jpg"])
        empty_team = env.make_team(with_labeler=False)
        status, body = env.create(team_id=empty_team)
        assert status == 400
        assert "no members with the Data_Labeler role" in messages(body)
        env.assert_nothing_persisted()

    def test_team_whose_member_lost_the_role_rejected(self, env):
        """Req 4.8: the Data_Labeler role is re-resolved at creation —
        a member whose role was revoked does not count."""
        env.put_images(["a.jpg"])
        team = env.make_team(with_labeler=False)
        member = env.add_labeler(team)
        env.dda.cognito.set_role(member.username, "Viewer")
        status, body = env.create(team_id=team)
        assert status == 400
        assert "no members with the Data_Labeler role" in messages(body)
        env.assert_nothing_persisted()

    def test_instructions_over_5000_chars_rejected(self, env):
        """Req 4.4: instructions capped at 5,000 characters."""
        env.put_images(["a.jpg"])
        status, body = env.create(instructions="x" * 5001)
        assert status == 400
        assert "5000" in messages(body)
        env.assert_nothing_persisted()

    def test_more_than_10_examples_rejected(self, env):
        """Req 4.4: at most 10 good and 10 bad example images."""
        env.put_images(["a.jpg"])
        status, body = env.create(example_images={
            "good": [f"g{i}.jpg" for i in range(11)], "bad": []})
        assert status == 400
        assert "At most 10 good example images" in messages(body)
        env.assert_nothing_persisted()

    def test_non_jpeg_png_example_ref_identified(self, env):
        """Req 4.4: each non-JPEG/PNG example reference identified."""
        env.put_images(["a.jpg"])
        status, body = env.create(example_images={
            "good": ["ok.png", "bad.gif"], "bad": ["worse.bmp"]})
        assert status == 400
        offenders = {err.get("example_ref")
                     for err in body["validation_errors"]}
        assert offenders == {"bad.gif", "worse.bmp"}
        env.assert_nothing_persisted()

    def test_all_invalid_parameters_enumerated_together(self, env):
        """Req 4.9: a rejection identifies each missing or invalid
        parameter, not just the first."""
        env.put_images(["a.jpg"])
        status, body = env.create(
            job_name="", task_type="Pose", team_id=None,
            instructions="x" * 5001)
        assert status == 400
        parameters = {err["parameter"] for err in body["validation_errors"]}
        assert {"job_name", "task_type", "team_id",
                "instructions"} <= parameters
        env.assert_nothing_persisted()

    def test_unknown_usecase_rejected(self, env):
        env.put_images(["a.jpg"])
        status, body = env.create(usecase_id=f"uc-{uuid.uuid4()}")
        assert status == 400
        assert "Use case not found" in messages(body)


# ------------------------------------------------------------- enumeration

class TestDatasetEnumeration:
    def test_empty_prefix_rejected_identifying_prefix(self, env):
        """Req 4.6: zero image objects -> error identifying the empty
        prefix, nothing persisted."""
        status, body = env.create()
        assert status == 400
        assert env.prefix in body["error"]
        assert body["dataset_prefix"] == env.prefix
        env.assert_nothing_persisted()

    def test_non_image_objects_skipped_not_rejected(self, env):
        """Req 4.7: unsupported objects are skipped, the job is created
        over the images alone, and the skipped count is reported."""
        env.put_images(["a.jpg", "b.png", "notes.txt", "nested/video.mp4"])
        status, body = env.create()
        assert status == 201
        assert body["image_count"] == 2
        assert body["skipped_object_count"] == 2

    def test_manifest_beside_images_still_creates_job(self, env):
        """Req 4.7: the common layout of a manifest living under the image
        prefix must not block job creation."""
        env.put_images(["a.jpg", "manifests/train.manifest"])
        status, body = env.create()
        assert status == 201
        assert body["image_count"] == 1
        assert body["skipped_object_count"] == 1

    def test_skipped_count_persisted_on_job(self, env):
        """Req 4.7: image_count stays explainable via the persisted
        skipped_object_count."""
        env.put_images(["a.jpg", "notes.txt"])
        status, body = env.create()
        assert status == 201
        job = env.get_job(body["job_id"])
        assert job["image_count"] == 1
        assert job["skipped_object_count"] == 1

    def test_prefix_with_only_non_image_objects_rejected(self, env):
        """Req 4.6/4.7: skipping everything leaves zero images, which is
        still a rejection — and it says why."""
        env.put_images(["notes.txt", "nested/video.mp4"])
        status, body = env.create()
        assert status == 400
        assert env.prefix in body["error"]
        assert body["skipped_object_count"] == 2
        env.assert_nothing_persisted()

    def test_folder_placeholders_ignored(self, env):
        """Zero-byte folder marker keys are not offending objects."""
        env.put_images(["a.jpg"])
        env.s3.put_object(Bucket=DATASET_BUCKET,
                          Key=f"{env.prefix}nested/", Body=b"")
        status, body = env.create()
        assert status == 201
        assert body["image_count"] == 1

    def test_validation_precedes_enumeration(self, env):
        """Req 4.5: parameter errors are reported even when the dataset
        prefix is empty — validation runs first."""
        status, body = env.create(job_name="")
        assert status == 400
        assert "validation_errors" in body  # not the empty-prefix error
        env.assert_nothing_persisted()


# --------------------------------------------------- auto-label matrix

class TestAutoLabelMatrix:
    @pytest.mark.parametrize("model,task_type,ok", [
        ("sam", "Segmentation", True),
        ("sam", "ObjectDetection", True),
        ("sam", "Classification", False),
        ("bedrock:anthropic.claude-3-haiku", "Classification", True),
        ("bedrock:anthropic.claude-3-haiku", "ObjectDetection", True),
        ("bedrock:anthropic.claude-3-haiku", "Segmentation", False),
    ])
    def test_model_modality_matrix(self, env, model, task_type, ok):
        """Req 8.8: SAM -> Segmentation/ObjectDetection; Bedrock ->
        Classification/ObjectDetection."""
        env.put_images(["a.jpg"])
        label_set = (["scratch"] if task_type != "Classification" else None)
        status, body = env.create(
            task_type=task_type, label_set=label_set,
            auto_label={"enabled": True, "model": model})
        if ok:
            assert status == 201
            job = env.get_job(body["job_id"])
            assert job["auto_label"] == {"enabled": True, "model": model}
        else:
            assert status == 400
            assert "does not support" in messages(body)
            env.assert_nothing_persisted()

    def test_unknown_model_rejected(self, env):
        """Req 8.1: the model must come from the supported options."""
        env.put_images(["a.jpg"])
        status, body = env.create(
            auto_label={"enabled": True, "model": "yolo"})
        assert status == 400
        assert "'sam' or 'bedrock:<model_id>'" in messages(body)
        env.assert_nothing_persisted()

    def test_auto_label_disabled_model_not_required(self, env):
        env.put_images(["a.jpg"])
        status, _ = env.create(auto_label={"enabled": False})
        assert status == 201


# ------------------------------------------------------- skip-verification

class TestSkipVerification:
    def valid_skip_body(self):
        return dict(
            team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts={"normal": "Is it normal?",
                               "anomaly": "Is it anomalous?"},
        )

    def test_non_admin_rejected_with_authorization_error(self, env):
        """Req 9.1: skip-verification is admin-only; non-admins get an
        authorization error, nothing persisted, audit event written."""
        env.put_images(["a.jpg"])
        status, body = env.create(user=env.make_user(role="DataScientist"),
                                  **self.valid_skip_body())
        assert status == 403
        assert "administrator" in body["error"]
        env.assert_nothing_persisted()
        assert len(env.audit_events("unauthorized_access")) == 1

    @pytest.mark.parametrize("role", ["UseCaseAdmin", "PortalAdmin"])
    def test_admin_creates_skip_job_without_team(self, env, role):
        """Req 4.1, 9.1, 9.2: admins may create a skip-verification job
        with no team; the skip fields are persisted."""
        env.put_images(["a.jpg"])
        status, body = env.create(user=env.make_user(role=role),
                                  **self.valid_skip_body())
        assert status == 201
        job = env.get_job(body["job_id"])
        assert job["skip_verification"] is True
        assert job["bedrock_model_id"] == "anthropic.claude-3-haiku"
        assert job["per_label_prompts"] == {
            "normal": "Is it normal?", "anomaly": "Is it anomalous?"}
        assert "team_id" not in job

    def test_usecase_scoped_admin_role_authorizes_skip(self, env):
        """Req 9.1: a per-usecase UseCaseAdmin row (UserRoles table)
        also authorizes skip-verification."""
        env.put_images(["a.jpg"])
        user = env.make_user(role="Viewer")
        env.stack.tables.user_roles.put_item(Item={
            "user_id": user["user_id"],
            "usecase_id": env.usecase_id,
            "role": "UseCaseAdmin",
        })
        status, _ = env.create(user=user, **self.valid_skip_body())
        assert status == 201

    def test_missing_bedrock_model_rejected(self, env):
        """Req 9.3: missing Bedrock model selection identified."""
        env.put_images(["a.jpg"])
        overrides = self.valid_skip_body()
        overrides["bedrock_model_id"] = None
        status, body = env.create(user=env.make_user(role="PortalAdmin"),
                                  **overrides)
        assert status == 400
        assert "Bedrock model" in messages(body)
        env.assert_nothing_persisted()

    def test_missing_and_empty_prompts_identified_per_label(self, env):
        """Req 9.2, 9.3: every label needs a non-empty prompt; each
        missing/empty label identified."""
        env.put_images(["a.jpg"])
        overrides = self.valid_skip_body()
        overrides["task_type"] = "ObjectDetection"
        overrides["label_set"] = ["scratch", "dent", "crack"]
        overrides["per_label_prompts"] = {"scratch": "Find scratches",
                                          "dent": "   "}
        status, body = env.create(user=env.make_user(role="PortalAdmin"),
                                  **overrides)
        assert status == 400
        offending = {err.get("label") for err in body["validation_errors"]}
        assert offending == {"dent", "crack"}
        env.assert_nothing_persisted()

    def test_empty_team_not_required_for_skip(self, env):
        """Req 4.8 applies only when skip-verification is disabled."""
        env.put_images(["a.jpg"])
        status, _ = env.create(user=env.make_user(role="PortalAdmin"),
                               **self.valid_skip_body())
        assert status == 201


# --------------------------------------------- LLM auto-label (task 6.2)
# Feature: llm-auto-labeling

LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
LLM_PROMPT = "Find every visible surface defect"


def llm_auto_label(prompt=LLM_PROMPT, model=LLM_MODEL):
    """An auto_label body for the llm: family. prompt=None omits the
    detection_prompt key entirely (the 'absent' rejection case)."""
    auto_label = {"enabled": True, "model": model}
    if prompt is not None:
        auto_label["detection_prompt"] = prompt
    return auto_label


class TestLlmAutoLabel:
    @pytest.mark.parametrize("task_type,label_set", [
        ("Classification", None),
        ("Segmentation", ["scratch", "dent"]),
        ("ObjectDetection", ["scratch"]),
    ])
    def test_llm_accepted_for_each_modality(self, env, task_type,
                                            label_set):
        """Req 1.3, 1.6, 2.2: llm:<id> is accepted for all three
        modalities; the model identifier and prompt are persisted on the
        job record."""
        env.put_images(["a.jpg"])
        status, body = env.create(task_type=task_type, label_set=label_set,
                                  auto_label=llm_auto_label())
        assert status == 201
        job = env.get_job(body["job_id"])
        assert job["auto_label"] == {
            "enabled": True,
            "model": LLM_MODEL,
            "detection_prompt": LLM_PROMPT,
        }

    @pytest.mark.parametrize("bad_model", [
        "llm:",                    # empty identifier
        "llm:" + "x" * 257,        # over the 256-char cap
        "llm:us.nova pro-v1:0",    # embedded space
        "llm:us.nova\x01pro",      # control character
    ])
    def test_invalid_identifier_rejected_naming_model(self, env, bad_model):
        """Req 1.5: each invalid identifier is rejected with an error
        naming the model parameter; no job or task items persisted."""
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_auto_label(model=bad_model))
        assert status == 400
        model_errors = [err for err in body["validation_errors"]
                        if "model identifier" in err["message"]]
        assert len(model_errors) == 1
        assert model_errors[0]["parameter"] == "auto_label"
        assert model_errors[0]["model"] == bad_model
        env.assert_nothing_persisted()

    @pytest.mark.parametrize("bad_prompt", [
        None,           # detection_prompt key absent
        "",             # empty
        "   \t\n  ",    # whitespace-only
        "x" * 2001,     # over the 2000-char cap
    ])
    def test_invalid_prompt_rejected_naming_prompt(self, env, bad_prompt):
        """Req 2.2, 2.3, 2.4: absent/empty/whitespace-only and over-length
        prompts are rejected with an error naming the prompt; no job or
        task items persisted."""
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_auto_label(bad_prompt))
        assert status == 400
        assert "detection_prompt" in messages(body)
        env.assert_nothing_persisted()

    def test_over_length_prompt_error_reports_length(self, env):
        """Req 2.4: the length violation is distinct from the missing
        prompt error and carries the offending length."""
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_auto_label("x" * 2001))
        assert status == 400
        assert "at most 2000 characters" in messages(body)
        assert "required" not in messages(body)
        env.assert_nothing_persisted()

    def test_identifier_and_prompt_errors_enumerated_together(self, env):
        """Req 1.5, 2.3: an invalid identifier and a missing prompt are
        both reported in the single 400 (validation joins the shared
        pre-enumeration error list)."""
        env.put_images(["a.jpg"])
        status, body = env.create(
            auto_label=llm_auto_label(prompt="", model="llm:"))
        assert status == 400
        assert "model identifier" in messages(body)
        assert "detection_prompt" in messages(body)
        env.assert_nothing_persisted()

    def test_prompt_stored_byte_identical(self, env):
        """Req 2.5: the prompt is stored character-for-character —
        leading/trailing whitespace, embedded newlines, and quote/brace
        characters all survive."""
        prompt = ('  Find "cracks" and {holes}\n'
                  '\tignore [reflections]; keep \'sliver\' defects\r\n  ')
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_auto_label(prompt))
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"][
            "detection_prompt"] == prompt


# ---------------------------------------- job_created audit (Req 9.4)

class TestJobCreatedAuditDetails:
    def details(self, env):
        events = env.audit_events("job_created")
        assert len(events) == 1
        return events[0]["details"]

    def test_llm_job_records_model_and_llm_mode(self, env):
        env.put_images(["a.jpg"])
        status, _ = env.create(auto_label=llm_auto_label())
        assert status == 201
        details = self.details(env)
        assert details["auto_label_model"] == LLM_MODEL
        assert details["auto_label_mode"] == "llm"

    def test_sam_job_records_sam_mode(self, env):
        env.put_images(["a.jpg"])
        status, _ = env.create(task_type="Segmentation",
                               label_set=["scratch"],
                               auto_label={"enabled": True, "model": "sam"})
        assert status == 201
        details = self.details(env)
        assert details["auto_label_model"] == "sam"
        assert details["auto_label_mode"] == "sam"

    def test_bedrock_job_records_bedrock_mode(self, env):
        env.put_images(["a.jpg"])
        status, _ = env.create(auto_label={
            "enabled": True, "model": "bedrock:anthropic.claude-3-haiku"})
        assert status == 201
        details = self.details(env)
        assert details["auto_label_model"] == (
            "bedrock:anthropic.claude-3-haiku")
        assert details["auto_label_mode"] == "bedrock"

    def test_no_auto_label_records_none_mode_without_model(self, env):
        env.put_images(["a.jpg"])
        status, _ = env.create()
        assert status == 201
        details = self.details(env)
        assert details["auto_label_mode"] == "none"
        assert "auto_label_model" not in details


# ------------------------------- skip-verification with an llm: model

class TestSkipVerificationWithLlm:
    def skip_llm_body(self, **auto_label_kwargs):
        return dict(
            team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts={"normal": "Is it normal?",
                               "anomaly": "Is it anomalous?"},
            auto_label=llm_auto_label(**auto_label_kwargs),
        )

    def test_admin_creates_llm_skip_job(self, env):
        """Req 2.6: an admin skip-verification job with an llm: model
        persists both the detection_prompt and the per-label prompts."""
        env.put_images(["a.jpg"])
        status, body = env.create(user=env.make_user(role="PortalAdmin"),
                                  **self.skip_llm_body())
        assert status == 201
        job = env.get_job(body["job_id"])
        assert job["skip_verification"] is True
        assert job["auto_label"]["model"] == LLM_MODEL
        assert job["auto_label"]["detection_prompt"] == LLM_PROMPT
        assert job["per_label_prompts"] == {
            "normal": "Is it normal?", "anomaly": "Is it anomalous?"}

    def test_missing_per_label_prompt_still_rejected(self, env):
        """Req 2.6, 9.1: the llm: model does not relax the per-label
        prompt requirement — every label still needs a non-empty
        prompt, each offender identified."""
        env.put_images(["a.jpg"])
        overrides = self.skip_llm_body()
        overrides["task_type"] = "ObjectDetection"
        overrides["label_set"] = ["scratch", "dent"]
        overrides["per_label_prompts"] = {"scratch": "Find scratches",
                                          "dent": "   "}
        status, body = env.create(user=env.make_user(role="PortalAdmin"),
                                  **overrides)
        assert status == 400
        offending = {err.get("label") for err in body["validation_errors"]
                     if err["parameter"] == "per_label_prompts"}
        assert offending == {"dent"}
        env.assert_nothing_persisted()

    def test_non_admin_rejected_403_before_validation(self, env):
        """Req 9.3, 9.1: a non-admin submitting a skip-verification llm
        job gets the 403 with the unauthorized_access audit event before
        any validation errors are assembled — even when the llm config
        itself is invalid."""
        env.put_images(["a.jpg"])
        status, body = env.create(
            user=env.make_user(role="DataScientist"),
            **self.skip_llm_body(prompt=None, model="llm:"))
        assert status == 403
        assert "administrator" in body["error"]
        assert "validation_errors" not in body
        assert len(env.audit_events("unauthorized_access")) == 1
        env.assert_nothing_persisted()


# ------------------------------------ create permission gate (Req 9.2)

class TestCreatePermission:
    def test_caller_without_create_permission_rejected(self, env):
        """Req 9.2: a caller without the labeling job creation permission
        is rejected by the existing rbac_check gate (the permission set
        that authorizes POST /labeling) with nothing persisted."""
        import shared_utils
        sys.modules.pop("rbac_middleware", None)
        import rbac_middleware

        env.put_images(["a.jpg"])
        labeler = env.make_user(role="DataLabeler")
        request_body = env.body(auto_label=llm_auto_label())

        # Mirror labeling.py's delegation, guarded by the existing
        # create permission (Requirement 9.1: no new access paths).
        @rbac_middleware.rbac_check(
            [shared_utils.Permission.CREATE_LABELING_JOBS])
        def create_route(event, context):
            return env.dda.module.create_dda_job(
                json.loads(event["body"]),
                shared_utils.get_user_from_event(event))

        response = create_route({
            "httpMethod": "POST",
            "resource": "/labeling",
            "path": "/labeling",
            "pathParameters": None,
            "queryStringParameters": {"usecase_id": env.usecase_id},
            "body": json.dumps(request_body),
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": labeler["user_id"],
                        "email": labeler["email"],
                        "cognito:username": labeler["username"],
                        "custom:role": labeler["role"],
                    }
                }
            },
        }, None)

        assert response["statusCode"] == 403
        assert json.loads(response["body"])["error"] == (
            "Insufficient permissions")
        env.assert_nothing_persisted()


# ---------------------------- job-record sizing persistence (task 7.4)
# Feature: llm-model-token-and-image-sizing


def llm_sized_auto_label(**sizing):
    """An llm: auto_label body carrying the two sizing values
    (Req 3.6, 5.7)."""
    auto_label = llm_auto_label()
    auto_label.update(sizing)
    return auto_label


class TestLlmSizingPersistence:
    """Req 3.6, 5.7, 10.4, 10.6: `auto_label.downscale_max_edge` and
    `auto_label.token_budget` are persisted unchanged for the `llm:`
    family only, and only when submitted."""

    def test_both_values_persisted_unchanged_for_llm(self, env):
        """Req 5.7, 3.6: the submitted Max_Image_Edge and
        Token_Budget_Selection land on the record exactly as
        submitted, beside the prompt."""
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_sized_auto_label(
            downscale_max_edge=1024, token_budget=20000))
        assert status == 201
        job = env.get_job(body["job_id"])
        assert job["auto_label"] == {
            "enabled": True,
            "model": LLM_MODEL,
            "detection_prompt": LLM_PROMPT,
            "downscale_max_edge": 1024,
            "token_budget": 20000,
        }

    def test_omitted_values_leave_the_record_pre_feature(self, env):
        """Req 10.6: a submission carrying neither value yields an
        auto_label document byte-identical to a pre-feature record —
        neither key is written, and nothing rejects the omission."""
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_auto_label())
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"] == {
            "enabled": True,
            "model": LLM_MODEL,
            "detection_prompt": LLM_PROMPT,
        }

    def test_null_downscale_is_downscale_off_and_left_absent(self, env):
        """The wizard's blank select submits null; the record holds one
        representation of Downscale_Off only — the attribute absent."""
        env.put_images(["a.jpg"])
        status, body = env.create(auto_label=llm_sized_auto_label(
            downscale_max_edge=None))
        assert status == 201
        assert "downscale_max_edge" not in env.get_job(
            body["job_id"])["auto_label"]

    @pytest.mark.parametrize("model,task_type,label_set", [
        ("sam", "Segmentation", ["scratch"]),
        ("bedrock:anthropic.claude-3-haiku", "Classification", None),
    ])
    def test_never_written_for_sam_or_bedrock(self, env, model, task_type,
                                              label_set):
        """Req 10.4: sizing values planted on a sam / bedrock:
        submission never reach the record — those families carry
        neither attribute."""
        env.put_images(["a.jpg"])
        status, body = env.create(
            task_type=task_type, label_set=label_set,
            auto_label={"enabled": True, "model": model,
                        "downscale_max_edge": 1024,
                        "token_budget": 20000})
        assert status == 201
        assert env.get_job(body["job_id"])["auto_label"] == {
            "enabled": True, "model": model}


# ------------------------------- grounded-sam family (task 2.3)
# Feature: grounded-sam-autolabel


def grounded_sam_auto_label(**extra):
    """An auto_label body for the grounded-sam family."""
    auto_label = {"enabled": True, "model": "grounded-sam"}
    auto_label.update(extra)
    return auto_label


class TestGroundedSamClassificationRejected:
    def test_classification_rejected_identifying_model_and_modality(
            self, env):
        """Req 1.6: grounded-sam + Classification is rejected with a
        validation error identifying the model value and the modality;
        nothing persisted."""
        env.put_images(["a.jpg"])
        status, body = env.create(task_type="Classification",
                                  auto_label=grounded_sam_auto_label())
        assert status == 400
        matrix_errors = [err for err in body["validation_errors"]
                         if "does not support" in err["message"]]
        assert len(matrix_errors) == 1
        error = matrix_errors[0]
        assert error["model"] == "grounded-sam"
        assert error["task_type"] == "Classification"
        assert "'grounded-sam'" in error["message"]
        assert "Classification" in error["message"]
        env.assert_nothing_persisted()


class TestGroundedSamAuditDetails:
    def details(self, env):
        events = env.audit_events("job_created")
        assert len(events) == 1
        return events[0]["details"]

    @pytest.mark.parametrize("task_type,label_set", [
        ("Segmentation", ["scratch", "dent"]),
        ("ObjectDetection", ["scratch"]),
    ])
    def test_grounded_sam_job_records_model_and_mode(self, env, task_type,
                                                     label_set):
        """Req 1.7: the job_created audit details carry
        auto_label_model 'grounded-sam' and auto_label_mode
        'grounded-sam'."""
        env.put_images(["a.jpg"])
        status, _ = env.create(task_type=task_type, label_set=label_set,
                               auto_label=grounded_sam_auto_label())
        assert status == 201
        details = self.details(env)
        assert details["auto_label_model"] == "grounded-sam"
        assert details["auto_label_mode"] == "grounded-sam"


class TestGroundedSamNoWorkerDependency:
    @pytest.mark.parametrize("task_type,label_set", [
        ("Segmentation", ["scratch", "dent"]),
        ("ObjectDetection", ["scratch"]),
    ])
    def test_created_while_no_worker_deployed(self, env, monkeypatch,
                                              task_type, label_set):
        """Req 5.4: job creation has no worker dependency — a
        grounded-sam job is accepted while no grounded-sam worker is
        deployed (no worker function name in the environment)."""
        monkeypatch.delenv("GROUNDED_SAM_WORKER_FUNCTION_NAME",
                           raising=False)
        monkeypatch.delenv("SAM_WORKER_FUNCTION_NAME", raising=False)
        env.put_images(["a.jpg"])
        status, body = env.create(task_type=task_type, label_set=label_set,
                                  auto_label=grounded_sam_auto_label())
        assert status == 201
        job = env.get_job(body["job_id"])
        assert job["status"] == "InProgress"
        assert job["auto_label"] == {"enabled": True,
                                     "model": "grounded-sam"}

    def test_creation_never_invokes_the_worker(self, env, monkeypatch):
        """Req 5.4: even with a grounded-sam worker name planted in the
        environment, creation only fan-outs to the distribution worker —
        the grounded-sam worker is never touched at creation time."""
        monkeypatch.setenv("GROUNDED_SAM_WORKER_FUNCTION_NAME",
                           "planted-gsam-worker")
        monkeypatch.setenv("DDA_LABELING_WORKER_FUNCTION_NAME",
                           "test-dda-worker")
        env.dda.lambda_client.invocations.clear()
        env.put_images(["a.jpg"])
        status, _ = env.create(task_type="Segmentation",
                               label_set=["scratch"],
                               auto_label=grounded_sam_auto_label())
        assert status == 201
        invoked = [inv["FunctionName"]
                   for inv in env.dda.lambda_client.invocations]
        assert invoked == ["test-dda-worker"]
