"""
LLM sizing flow end-to-end integration (llm-model-token-and-image-sizing,
task 9.7).

Feature: llm-model-token-and-image-sizing

Drives the *whole* sizing path against the moto-backed stack from
conftest.py — real `shared_utils` / `rbac_middleware`, real DynamoDB and
S3, real `dda_llm_image` / `dda_llm_request` / `dda_llm_prelabel` — with
only the Converse client stubbed. The harnesses are reused rather than
re-created: `PreviewFlowEnv` (and through it `PreviewEnv` /
`CreateJobEnv`) from test_preview_flow_integration.py for the Use_Case,
dataset prefix, authorized creator, API event builders, recorded S3
seams and inline executor drive, extended here with fully decodable
Pillow-generated images (the downscale path genuinely decodes and
re-encodes) and with the worker / settings modules.

Four integration surfaces:

- **End-to-end preview with downscaling** (Req 1.6, 5.4, 7.3): seed a
  3000x2000 JPEG, `POST /labeling-preview/runs` with
  `downscale_max_edge: 1024` and `token_budget: 20000`, drive the async
  self-invoke inline, poll `GET /labeling-preview/runs/{runId}` to
  `Completed`. The captured request's image block decodes to 1024x682,
  `inferenceConfig.maxTokens == 20000` (never the Global_Max_Tokens),
  the prompt names 1024x682, and the result payload's Pre_Label
  geometry is mapped back into Source space by the Requirement 7.3
  rounding rule — within 3000x2000, with `image_width`/`image_height`
  keeping their Source meaning of 3000/2000 beside the explicit
  source/sent sizing report.

- **The cross-account read path with a Max_Image_Edge selected**
  (Req 8.4): the Sample_Image and both attached example images are read
  through `get_s3_client_for_bucket`, resolving in this single-account
  stack through `assume_usecase_role`'s direct-access fallback (root
  ARN -> default credentials). The bytes that reach the model prove the
  fallback produced usable clients: the oversize example re-encoded to
  the bound, the already-fitting example byte-identical pass-through.

- **The worker path through the SQS record path** (Req 5.8, 6.8, 8.4):
  the same job configuration — same object, same examples, same
  Downscale_Setting and Token_Budget_Selection persisted on the
  Labeling_Job record — driven through the real
  `dda_autolabel_worker.handler`, with the captured Converse request
  asserted equal element-for-element (bytes included) to the preview's,
  and the stored Pre_Label equal to the preview payload's.

- **Settings round trip** (Req 1.6, 4.1, 4.4, 4.8): `PUT
  /data-accounts/bedrock-configuration/token-limits` through the real
  `data_accounts.handler` as PortalAdmin; the *same imported worker
  module* (no redeploy) resolves the new value on its next invocation;
  the `bedrock_configuration` item is untouched by every token-limits
  write; a `PUT {}` makes the default of 10000 apply again.

The three function modules and `bedrock_common` are all pinned to one
dedicated settings table, so the persisted `llm_model_token_limits`
item the Settings_API writes is the one both request paths read —
per-model configuration delivery is exercised end to end, not simulated.

Requirements: 1.6, 4.1, 4.4, 4.8, 5.4, 5.8, 6.8, 7.3, 8.4
"""
import io
import json
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

from test_dda_autolabel_worker import SAM_FUNCTION
from test_dda_labeling_create_job import (
    DATASET_BUCKET,
    POOL_ID,
    REGION,
    FakeCognitoClient,
    FakeLambdaClient,
)
from test_dda_labeling_preview_routes import ARTIFACTS_BUCKET
from test_preview_flow_integration import (
    BAD,
    GOOD,
    PreviewFlowEnv,
    StubConverseClient,
    guidance,
)

MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"
PROMPT = 'Find every "scratch" on the panel'
LABELS = ["scratch", "dent"]
MODALITY = "ObjectDetection"

# One dedicated portal settings table for this module: data_accounts
# writes the `llm_model_token_limits` item into it and both request
# paths (and bedrock_common) read it back — the production delivery
# mechanism, on one table (Req 1.6, 1.8).
SETTINGS_TABLE_NAME = "test-settings-llm-sizing-integration"
TOKEN_LIMITS_KEY = "llm_model_token_limits"
BEDROCK_CONFIG_KEY = "bedrock_configuration"

# The shared-layer Model_Token_Limit_Default (Req 4.8).
DEFAULT_TOKEN_BUDGET = 10000

# The target: 3000x2000 at a bound of 1024 floors to 1024x682
# (floor(2000 * 1024 / 3000) = 682).
SOURCE_W, SOURCE_H = 3000, 2000
BOUND = 1024
SENT_W, SENT_H = 1024, 682

# Coordinate_Guidance in *Sent* space (10, 5, 30x20 inside 1024x682) and
# its Source-space image under the Requirement 7.3 rule — corners mapped
# with round-half-up (floor(v * source / sent + 0.5)), extents re-derived
# as differences:
#   left   floor(10 * 3000/1024 + 0.5) = 29
#   top    floor( 5 * 2000/682  + 0.5) = 15
#   right  floor(40 * 3000/1024 + 0.5) = 117  -> width  88
#   bottom floor(25 * 2000/682  + 0.5) = 73   -> height 58
SENT_BOX = {"class": "scratch",
            "box": {"left": 10, "top": 5, "width": 30, "height": 20}}
EXPECTED_SOURCE_BOX = {"class": "scratch",
                       "left": 29, "top": 15, "width": 88, "height": 58}


def real_image_bytes(width, height, image_format, seed=0):
    """A fully decodable image — the downscale path genuinely decodes
    and re-encodes, so header-only bytes would be refused
    (test_property_preview_worker_request_identity's convention). The
    seed varies the fill color so distinct images carry distinct bytes
    even at equal dimensions."""
    from PIL import Image  # lazy, matching the imaging-layer convention

    color = ((37 * seed + 11) % 256, (59 * seed + 23) % 256,
             (83 * seed + 41) % 256)
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(
        buffer, format="PNG" if image_format == "png" else "JPEG")
    return buffer.getvalue()


def decoded_size(image_bytes):
    """(width, height) of a Converse image block's bytes, via a real
    decode."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as image:
        return image.size


def image_blocks(call):
    return [block["image"] for block in call["messages"][0]["content"]
            if "image" in block]


def prompt_text(call):
    return call["messages"][0]["content"][-1]["text"]


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def settings_table(aws_stack):
    """The dedicated moto-backed portal settings table."""
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        client.create_table(
            TableName=SETTINGS_TABLE_NAME,
            KeySchema=[{"AttributeName": "setting_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "setting_key",
                                   "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except client.exceptions.ResourceInUseException:
        pass
    return boto3.resource("dynamodb",
                          region_name=REGION).Table(SETTINGS_TABLE_NAME)


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


@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock.

    Imported exactly once for the module: the settings round trip's "no
    redeploy" claim is that this same module object resolves each newly
    persisted mapping on its next handler invocation.
    """
    import os
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    dda_autolabel_worker.PORTAL_ARTIFACTS_BUCKET = ARTIFACTS_BUCKET
    return dda_autolabel_worker


@pytest.fixture(scope="module")
def accounts(aws_stack):
    """The real data_accounts module (the Settings_API) inside the mock."""
    sys.modules.pop("data_accounts", None)
    import data_accounts
    return data_accounts


class SizingFlowEnv(PreviewFlowEnv):
    """PreviewFlowEnv plus what the sizing flow needs: fully decodable
    Pillow-generated images, the worker's SQS entry point aimed at the
    same Use_Case and objects, one Converse stub served to both paths,
    and the Settings_API invoked as PortalAdmin."""

    def __init__(self, stack, dda, worker, accounts, settings_table,
                 monkeypatch):
        super().__init__(stack, dda, monkeypatch)
        self.worker = worker
        self.accounts = accounts
        self.settings_table = settings_table

    # ------------------------------------------------------------ seeding
    def put_real_sample(self, name, width, height, seed=3):
        """One fully decodable dataset object under the Use_Case dataset
        prefix (JPEG unless the name says .png), sized to order."""
        key = f"{self.prefix}{name}"
        image_format = "png" if name.lower().endswith(".png") else "jpeg"
        body = real_image_bytes(width, height, image_format, seed=seed)
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=key, Body=body)
        self.sample_bytes[key] = body
        return key

    def put_real_example(self, designation, position, ext="png", *,
                         width, height, seed):
        """One fully decodable example image in the Use_Case data bucket,
        referenced as a full s3:// URI — the shape that resolves
        identically on the preview and the worker path."""
        key = (f"labeling-examples/{uuid.uuid4().hex[:8]}/"
               f"{designation}{position}.{ext}")
        image_format = "png" if ext.lower() == "png" else "jpeg"
        body = real_image_bytes(width, height, image_format, seed=seed)
        self.s3.put_object(Bucket=DATASET_BUCKET, Key=key, Body=body)
        ref = f"s3://{DATASET_BUCKET}/{key}"
        self.example_bytes[ref] = body
        return {"ref": ref, "designation": designation, "position": position}

    @staticmethod
    def example_key(example):
        return example["ref"].split("/", 3)[3]

    # -------------------------------------------------------------- seams
    def use_bedrock_everywhere(self, replies):
        """One stub Converse client behind every path's client seam: the
        preview executor's, the worker's, and the shared chokepoint's
        (each caller rebinds the chokepoint's to its own before
        delegating, so all three must point at the same stub for the two
        paths to record into one call list)."""
        import dda_llm_prelabel

        stub = StubConverseClient(replies)

        def factory(region, timeout_seconds):
            return stub

        self.monkeypatch.setattr(self.module, "get_bedrock_client", factory)
        self.monkeypatch.setattr(self.worker, "get_bedrock_client", factory)
        self.monkeypatch.setattr(dda_llm_prelabel, "get_bedrock_client",
                                 factory)
        return stub

    # ------------------------------------------------------ worker driving
    def make_worker_job(self, *, downscale_max_edge=None, token_budget=None,
                        few_shot=None):
        """A persisted `llm:` Labeling_Job record in this env's Use_Case,
        carrying the sizing values exactly as create_dda_job persists
        them: present only when configured (Req 5.7, 3.6)."""
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        auto_label = {"enabled": True, "model": MODEL,
                      "detection_prompt": PROMPT}
        if few_shot is not None:
            auto_label["few_shot"] = few_shot
        if downscale_max_edge is not None:
            auto_label["downscale_max_edge"] = downscale_max_edge
        if token_budget is not None:
            auto_label["token_budget"] = token_budget
        self.stack.tables.labeling_jobs.put_item(Item={
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "task_type": MODALITY,
            "label_set": LABELS,
            "skip_verification": False,
            "auto_label": auto_label,
            "created_at": 1,
            "updated_at": 1,
        })
        return job_id

    def run_worker_task(self, job_id, sample_key):
        """One image through the real SQS handler — a fresh
        Task_Assignment per invocation, against the same dataset object
        the preview read."""
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        image_uri = f"s3://{DATASET_BUCKET}/{sample_key}"
        self.stack.tables.labeling_tasks.put_item(Item={
            "job_id": job_id,
            "task_id": task_id,
            "usecase_id": self.usecase_id,
            "image_s3_uri": image_uri,
            "assignee_user_id": "AUTO",
            "status": "Assigned",
            "prelabel_status": "Pending",
        })
        record = {"messageId": f"msg-{uuid.uuid4().hex[:8]}",
                  "body": json.dumps({
                      "job_id": job_id,
                      "task_id": task_id,
                      "image_s3_uri": image_uri,
                      "modality": MODALITY,
                      "label_set": LABELS,
                      "model": MODEL,
                      "detection_prompt": PROMPT,
                  })}
        result = self.worker.handler({"Records": [record]}, None)
        return result, task_id

    def worker_task(self, job_id, task_id):
        return self.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task_id}).get("Item")

    def worker_prelabel(self, job_id, task_id):
        key = (f"labeling/{self.usecase_id}/{job_id}/prelabels/"
               f"{task_id}.json")
        body = self.s3.get_object(Bucket=ARTIFACTS_BUCKET,
                                  Key=key)["Body"].read()
        return json.loads(body)

    # --------------------------------------------------------- settings API
    def put_token_limits(self, mapping):
        """PUT /data-accounts/bedrock-configuration/token-limits through
        the real data_accounts.handler as PortalAdmin."""
        user_id = f"user-{uuid.uuid4()}"
        event = {
            "httpMethod": "PUT",
            "resource": "/data-accounts/{id}/token-limits",
            "path": "/data-accounts/bedrock-configuration/token-limits",
            "pathParameters": {"id": "bedrock-configuration"},
            "body": json.dumps({"model_token_limits": mapping}),
            "requestContext": {"authorizer": {"claims": {
                "sub": user_id,
                "email": f"{user_id}@example.com",
                "cognito:username": user_id,
                "custom:role": "PortalAdmin",
            }}},
        }
        response = self.accounts.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    def stored_bedrock_configuration(self):
        return self.settings_table.get_item(
            Key={"setting_key": BEDROCK_CONFIG_KEY}).get("Item")


@pytest.fixture
def env(aws_stack, dda, worker, accounts, settings_table, monkeypatch):
    """Fresh Use_Case + creator, every module pinned to the dedicated
    settings table, no environment bootstraps, no leftover settings
    items."""
    import bedrock_common

    for module in (dda.module, worker, accounts, bedrock_common):
        monkeypatch.setattr(module, "SETTINGS_TABLE", SETTINGS_TABLE_NAME)
    monkeypatch.delenv("LLM_MODEL_TOKEN_LIMITS", raising=False)
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    for setting_key in (TOKEN_LIMITS_KEY, BEDROCK_CONFIG_KEY):
        settings_table.delete_item(Key={"setting_key": setting_key})
    return SizingFlowEnv(aws_stack, dda, worker, accounts, settings_table,
                         monkeypatch)


# ---------------------------------------------- end-to-end preview sizing

class TestPreviewSizingEndToEnd:
    """Req 1.6, 5.4, 7.3: POST with both sizing inputs -> executor ->
    Completed, with the request downscaled and budgeted and the payload
    reporting Source and Sent dimensions with Source-space geometry."""

    def test_downscaled_run_reports_sent_dimensions_and_source_geometry(
            self, env):
        sample = env.put_real_sample("panel-3000x2000.jpg",
                                     SOURCE_W, SOURCE_H)
        stub, recorded = env.use_bedrock([guidance([SENT_BOX])])

        # --- start ------------------------------------------------------
        status, started = env.start(
            model=MODEL, detection_prompt=PROMPT, task_type=MODALITY,
            label_set=LABELS, sample_images=[sample],
            downscale_max_edge=BOUND, token_budget=20000)
        assert status == 202
        assert started["status"] == "Running"
        run_id = started["run_id"]

        # --- execute (inline, from the recorded self-invoke) ------------
        outcome = env.drive_executor()
        assert outcome == {"run_id": run_id,
                           "action": "execute_preview_run",
                           "status": "Completed", "sample_count": 1,
                           "succeeded": 1, "failed": 0}

        # --- poll to Completed -------------------------------------------
        status, polled = env.status(run_id)
        assert status == 200
        assert polled["status"] == "Completed"
        # The run reports the applied Downscale_Setting and the resolved
        # Effective_Token_Budget (Req 5.10, 1.6).
        assert polled["downscale_max_edge"] == BOUND
        assert polled["token_budget"] == 20000
        assert polled["results"][0]["state"] == "Succeeded"

        # --- the captured Converse request -------------------------------
        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["modelId"] == MODEL_ID
        # The Effective_Token_Budget is the request's maxTokens — the
        # Token_Budget_Selection, not the Global_Max_Tokens (Req 1.6).
        assert call["inferenceConfig"]["maxTokens"] == 20000

        blocks = image_blocks(call)
        assert len(blocks) == 1
        # The image actually sent decodes to the floored Sent_Dimensions,
        # re-encoded (not the source bytes) in the key-derived format.
        assert blocks[0]["format"] == "jpeg"
        assert decoded_size(blocks[0]["source"]["bytes"]) == (SENT_W, SENT_H)
        assert blocks[0]["source"]["bytes"] != env.sample_bytes[sample]
        # The prompt names the Sent_Dimensions of that image (Req 7.1).
        assert (f"The image is {SENT_W} pixels wide and {SENT_H} pixels "
                f"tall") in prompt_text(call)

        # --- the result payload ------------------------------------------
        payload = env.result_payload(run_id, 0)
        assert set(payload) == {"sample_key", "state", "prelabel",
                                "image_width", "image_height",
                                "source_width", "source_height",
                                "sent_width", "sent_height",
                                "downscale_max_edge"}
        # `image_width`/`image_height` keep their Source meaning (Req 7.7)
        # beside the explicit sizing report (Req 5.4).
        assert payload["image_width"] == SOURCE_W
        assert payload["image_height"] == SOURCE_H
        assert payload["source_width"] == SOURCE_W
        assert payload["source_height"] == SOURCE_H
        assert payload["sent_width"] == SENT_W
        assert payload["sent_height"] == SENT_H
        assert payload["downscale_max_edge"] == BOUND

        # The Pre_Label geometry is expressed in Source space: the exact
        # image of the Sent-space guidance under the Requirement 7.3
        # round-half-up corner mapping, inside 3000x2000 (Req 7.3, 7.4).
        prelabel = payload["prelabel"]
        assert prelabel["modality"] == MODALITY
        assert prelabel["image_width"] == SOURCE_W
        assert prelabel["image_height"] == SOURCE_H
        assert len(prelabel["boxes"]) == 1
        box = prelabel["boxes"][0]
        assert box == EXPECTED_SOURCE_BOX
        assert 0 <= box["left"] and box["left"] + box["width"] <= SOURCE_W
        assert 0 <= box["top"] and box["top"] + box["height"] <= SOURCE_H


# ------------------------------------------------- cross-account read path

class TestCrossAccountReadPathWithBound:
    """Req 8.4: with a Max_Image_Edge selected, Sample_Images and example
    images are still read through `get_s3_client_for_bucket`'s
    single-account direct-access fallback, and the fallback's bytes are
    what the Image_Downscaler transforms."""

    def test_samples_and_examples_read_through_the_direct_fallback(
            self, env):
        sample = env.put_real_sample("target.jpg", SOURCE_W, SOURCE_H)
        # The good example exceeds the bound (1600x1200 -> 1024x768); the
        # bad one already fits (800x600 -> byte-identical pass-through).
        good = env.put_real_example(GOOD, 0, ext="png",
                                    width=1600, height=1200, seed=11)
        bad = env.put_real_example(BAD, 0, ext="jpg",
                                   width=800, height=600, seed=17)
        stub, _ = env.use_bedrock([guidance([SENT_BOX])])

        # Stored order interleaves the designations; attachment order is
        # good-then-bad, independent of the Downscale_Setting (Req 8.3).
        status, started = env.start(
            model=MODEL, detection_prompt=PROMPT, task_type=MODALITY,
            label_set=LABELS, sample_images=[sample],
            few_shot={"enabled": True, "examples": [bad, good]},
            downscale_max_edge=BOUND, token_budget=20000)
        assert status == 202
        env.drive_executor()

        status, polled = env.status(started["run_id"])
        assert status == 200
        assert polled["status"] == "Completed"
        assert polled["few_shot"] == {"enabled": True, "attached": 2,
                                      "omitted": 0}
        assert polled["results"][0]["state"] == "Succeeded"

        # Target first, then the attached examples in good-then-bad order,
        # every read through a client the Use_Case's own access mechanism
        # produced (one client request per bucket, preview session name).
        assert env.get_object_calls == [
            (DATASET_BUCKET, sample),
            (DATASET_BUCKET, env.example_key(good)),
            (DATASET_BUCKET, env.example_key(bad)),
        ]
        assert env.client_requests == [
            (DATASET_BUCKET, "dda-labeling-preview")]

        # Single-account setup: the root cross-account ARN resolves to the
        # Lambda's own credentials rather than an assumed role.
        assert env.assume_role_calls
        for resolution in env.assume_role_calls:
            assert resolution["role_arn"].endswith(":root")
            assert resolution["session_name"] == "dda-labeling-preview"
            assert resolution["credentials"]["is_default_credentials"] is True

        # The bytes the fallback read are the bytes the Image_Downscaler
        # transformed: the oversize example re-encoded to the bound in its
        # own key-derived format, the already-fitting example carried
        # byte-for-byte (Req 8.4, 6.3), the target downscaled last.
        blocks = image_blocks(stub.calls[0])
        assert [block["format"] for block in blocks] == ["png", "jpeg",
                                                         "jpeg"]
        assert decoded_size(blocks[0]["source"]["bytes"]) == (1024, 768)
        assert blocks[0]["source"]["bytes"] != env.example_bytes[good["ref"]]
        assert blocks[1]["source"]["bytes"] == env.example_bytes[bad["ref"]]
        assert decoded_size(blocks[2]["source"]["bytes"]) == (SENT_W, SENT_H)


# ------------------------------------------- worker SQS path, byte equality

class TestWorkerPathMatchesPreview:
    """Req 5.8, 6.8, 8.4: the same job configuration through the real SQS
    record path issues a Converse request byte-equal to the preview's,
    notwithstanding the different cross-account read mechanisms."""

    def test_worker_request_is_byte_equal_to_the_previews(self, env):
        sample = env.put_real_sample("shared-target.jpg",
                                     SOURCE_W, SOURCE_H)
        good = env.put_real_example(GOOD, 0, ext="png",
                                    width=1600, height=1200, seed=23)
        bad = env.put_real_example(BAD, 0, ext="jpg",
                                   width=800, height=600, seed=29)
        examples = [bad, good]
        stub = env.use_bedrock_everywhere([guidance([SENT_BOX])])

        # --- the preview run ---------------------------------------------
        status, started = env.start(
            model=MODEL, detection_prompt=PROMPT, task_type=MODALITY,
            label_set=LABELS, sample_images=[sample],
            few_shot={"enabled": True, "examples": examples},
            downscale_max_edge=BOUND, token_budget=20000)
        assert status == 202
        env.drive_executor()
        _, polled = env.status(started["run_id"])
        assert polled["status"] == "Completed"
        assert polled["results"][0]["state"] == "Succeeded"

        # --- the worker, over the same persisted configuration ------------
        job_id = env.make_worker_job(
            downscale_max_edge=BOUND, token_budget=20000,
            few_shot={"enabled": True, "examples": examples})
        result, task_id = env.run_worker_task(job_id, sample)
        assert result == {"batchItemFailures": []}
        assert env.worker_task(job_id, task_id)["prelabel_status"] == (
            "Available")

        # Exactly one invocation per path, and the whole request — model
        # id, ordered content blocks, image bytes and formats, prompt
        # text, inference configuration — is equal element for element
        # (Req 6.8): the downscaled examples and target byte-for-byte
        # (Req 8.4) and the persisted budget as maxTokens on both
        # (Req 5.8, 3.7).
        assert len(stub.calls) == 2
        preview_call, worker_call = stub.calls
        assert worker_call == preview_call
        assert worker_call["inferenceConfig"]["maxTokens"] == 20000
        blocks = image_blocks(worker_call)
        assert [decoded_size(block["source"]["bytes"])
                for block in blocks] == [(1024, 768), (800, 600),
                                         (SENT_W, SENT_H)]
        assert (f"The image is {SENT_W} pixels wide and {SENT_H} pixels "
                f"tall") in prompt_text(worker_call)

        # The same shared conversion yields the same Source-space
        # Pre_Label on both paths (Req 7.3).
        preview_prelabel = env.result_payload(started["run_id"],
                                              0)["prelabel"]
        assert env.worker_prelabel(job_id, task_id) == preview_prelabel
        assert preview_prelabel["boxes"][0] == EXPECTED_SOURCE_BOX


# --------------------------------------------------- settings round trip

class TestTokenLimitSettingsRoundTrip:
    """Req 1.6, 4.1, 4.4, 4.8: a PUT through the Settings_API reaches the
    worker's next request with no redeploy, never touches the
    bedrock_configuration item, and PUT {} restores the default."""

    def test_put_reaches_the_next_worker_request_without_redeploy(
            self, env):
        # The global Bedrock_Configuration, planted with a Global_Max_Tokens
        # that must never become an `llm:` request's maxTokens (Req 1.6).
        env.settings_table.put_item(Item={
            "setting_key": BEDROCK_CONFIG_KEY,
            "value": {"model_id": "anthropic.claude-test",
                      "region": REGION, "max_tokens": 128000,
                      "temperature": None, "top_p": None,
                      "timeout_seconds": 120},
        })
        config_before = env.stored_bedrock_configuration()
        assert config_before is not None

        # An `llm:` job with no Token_Budget_Selection: every resolution
        # goes through the persisted Model_Token_Limits and the default.
        job_id = env.make_worker_job()
        sample = env.put_sample("roundtrip.png")
        stub = env.use_bedrock_everywhere([guidance([SENT_BOX])])

        # --- before any PUT: the default of 10000 -------------------------
        result, task_id = env.run_worker_task(job_id, sample)
        assert result == {"batchItemFailures": []}
        assert env.worker_task(job_id, task_id)["prelabel_status"] == (
            "Available")
        assert stub.calls[-1]["inferenceConfig"]["maxTokens"] == (
            DEFAULT_TOKEN_BUDGET)

        # --- PUT a per-model limit as PortalAdmin (Req 4.1) ----------------
        status, payload = env.put_token_limits({MODEL_ID: 22000})
        assert status == 200
        assert payload["model_token_limits"] == {MODEL_ID: 22000}

        # The *same imported worker module* picks the new value up on its
        # next invocation — no redeploy, no re-import (Req 1.6).
        result, task_id = env.run_worker_task(job_id, sample)
        assert result == {"batchItemFailures": []}
        assert stub.calls[-1]["inferenceConfig"]["maxTokens"] == 22000

        # The bedrock_configuration item is untouched by the token-limits
        # write (Req 4.4) — and its 128000 never reached any request.
        assert env.stored_bedrock_configuration() == config_before

        # --- PUT {} : the default applies again (Req 4.8) ------------------
        status, payload = env.put_token_limits({})
        assert status == 200
        assert payload["model_token_limits"] == {}

        result, task_id = env.run_worker_task(job_id, sample)
        assert result == {"batchItemFailures": []}
        assert stub.calls[-1]["inferenceConfig"]["maxTokens"] == (
            DEFAULT_TOKEN_BUDGET)
        assert env.stored_bedrock_configuration() == config_before
