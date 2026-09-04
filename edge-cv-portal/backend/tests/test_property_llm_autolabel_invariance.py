"""
Existing-mode invariance for the LLM auto-label feature
(llm-auto-labeling, task 18.1).

- **Feature: llm-auto-labeling, Property 16: Existing-mode invariance**
  **Validates: Requirements 1.7**

For generated jobs whose auto-label configuration is `sam`,
`bedrock:<id>`, or absent, the three surfaces the llm-auto-labeling
feature touched are pinned byte-identical to a pre-change oracle
constructed literally in this file:

1. the persisted `auto_label` sub-document written by
   `dda_labeling.create_dda_job`,
2. the SQS message bodies enqueued by
   `dda_labeling_worker._enqueue_autolabel_messages`,
3. the Pre_Label payload `dda_autolabel_worker._generate_prelabel`
   writes to the portal artifacts bucket for the sam / bedrock
   families.

The expected shapes are written out literally (not derived from the
production code), so the test fails if any new key — `detection_prompt`
in particular — leaks into an existing mode. A recursive scan
additionally asserts no `detection_prompt` key appears anywhere in any
non-LLM job item, message, or Pre_Label document.

Fixture pattern (as in test_property_llm_autolabel_resolution.py):
hypothesis reuses function-scoped fixtures across examples, so the
moto harness is module-scoped and every example does its own setup
with fresh uuid-based prefixes/job names — examples never interfere
through the shared tables or the shared queue (drained per example).
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from hypothesis import given, settings
from hypothesis import strategies as st

from test_dda_autolabel_worker import (FakeBedrockClient,
                                       FakeSamLambdaClient, png_bytes)
from test_dda_labeling_create_job import (FakeCognitoClient,
                                          FakeLambdaClient)

REGION = "us-east-1"
DATASET_BUCKET = "test-invariance-data"
ARTIFACTS_BUCKET = "test-portal-artifacts"
POOL_ID = "us-east-1_dda-invariance-pool"
SAM_FUNCTION = "test-dda-sam-worker"

# Every generated image is this real PNG, so bedrock ObjectDetection
# dimension parsing sees 100x80.
IMAGE_WIDTH, IMAGE_HEIGHT = 100, 80

LABEL_POOL = ["scratch", "dent", "crack", "chip"]
BEDROCK_IDS = [
    "anthropic.claude-3-haiku",
    # Embedded colon: the id must survive the family split untouched.
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.amazon.nova-pro-v1:0",
]

_ENV_KEYS = ("AUTOLABEL_QUEUE_URL", "DDA_LABELING_WORKER_FUNCTION_NAME",
             "SAM_WORKER_FUNCTION_NAME")


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def henv(aws_stack):
    """Module-scoped harness: the real dda_labeling (fake Cognito +
    Lambda), dda_labeling_worker (moto SQS), and dda_autolabel_worker
    (fake Bedrock / SAM clients) imported inside the moto mock."""
    saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ.pop("DDA_LABELING_WORKER_FUNCTION_NAME", None)
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION

    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_labeling
    import dda_labeling_worker
    import dda_autolabel_worker

    fake_cognito = FakeCognitoClient()
    dda_labeling.cognito_client = fake_cognito
    dda_labeling.USER_POOL_ID = POOL_ID
    dda_labeling.lambda_client = FakeLambdaClient()
    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    sqs = boto3.client("sqs", region_name=REGION)
    queue_url = sqs.create_queue(
        QueueName=f"invariance-{uuid.uuid4().hex[:12]}")["QueueUrl"]
    os.environ["AUTOLABEL_QUEUE_URL"] = queue_url

    original_bedrock_factory = dda_autolabel_worker.get_bedrock_client
    original_sam_client = dda_autolabel_worker.sam_lambda_client

    yield InvarianceEnv(aws_stack, SimpleNamespace(
        labeling=dda_labeling, distributor=dda_labeling_worker,
        autolabel=dda_autolabel_worker, cognito=fake_cognito,
        sqs=sqs, queue_url=queue_url))

    dda_autolabel_worker.get_bedrock_client = original_bedrock_factory
    dda_autolabel_worker.sam_lambda_client = original_sam_client
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class InvarianceEnv:
    """One Use_Case + one team at module scope; per-example jobs."""

    def __init__(self, stack, mods):
        self.stack = stack
        self.mods = mods
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Invariance Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.creator = {
            "user_id": f"user-{uuid.uuid4()}",
            "email": "creator@example.com",
            "username": "creator",
            "role": "DataScientist",
        }
        self.admin = {
            "user_id": f"user-{uuid.uuid4()}",
            "email": "admin@example.com",
            "username": "admin",
            "role": "PortalAdmin",
        }
        # One team with one current Data_Labeler for team jobs.
        self.team_id = f"team-{uuid.uuid4()}"
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Invariance Team",
            "created_at": 1,
            "created_by": self.creator["user_id"],
        })
        username = f"labeler-{uuid.uuid4()}"
        sub = mods.cognito.add_user(username, f"{username}@example.com",
                                    role="DataLabeler")
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": f"MEMBER#{sub}",
            "user_id": sub,
            "email": f"{username}@example.com",
            "added_at": 1,
            "added_by": self.creator["user_id"],
        })

    # ------------------------------------------------------------ setup
    def put_images(self, count):
        prefix = f"datasets/{uuid.uuid4()}/"
        for index in range(count):
            self.s3.put_object(
                Bucket=DATASET_BUCKET, Key=f"{prefix}img-{index:03d}.png",
                Body=png_bytes(IMAGE_WIDTH, IMAGE_HEIGHT))
        return prefix

    def create_job(self, user, **body):
        body.setdefault("usecase_id", self.usecase_id)
        body.setdefault("job_name", f"job-{uuid.uuid4().hex[:12]}")
        response = self.mods.labeling.create_dda_job(
            {k: v for k, v in body.items() if v is not None}, user)
        payload = json.loads(response["body"])
        assert response["statusCode"] == 201, payload
        return payload["job_id"]

    def distribute(self, job_id):
        return self.mods.distributor.handler(
            {"action": "distribute", "job_id": job_id}, None)

    def use_bedrock(self, reply):
        fake = FakeBedrockClient(replies=[reply])
        self.mods.autolabel.get_bedrock_client = (
            lambda region, timeout_seconds: fake)
        return fake

    def use_sam(self, payload):
        fake = FakeSamLambdaClient(payload=payload)
        self.mods.autolabel.sam_lambda_client = fake
        return fake

    def run_autolabel(self, raw_bodies):
        records = [{"messageId": f"msg-{uuid.uuid4().hex[:8]}", "body": body}
                   for body in raw_bodies]
        return self.mods.autolabel.handler({"Records": records}, None)

    # ------------------------------------------------------------- store
    def drain_queue(self):
        """Raw SQS message body strings (byte-identity is asserted on
        the raw JSON text, not a re-serialization)."""
        bodies = []
        while True:
            response = self.mods.sqs.receive_message(
                QueueUrl=self.mods.queue_url, MaxNumberOfMessages=10,
                WaitTimeSeconds=0)
            batch = response.get("Messages", [])
            if not batch:
                break
            for message in batch:
                bodies.append(message["Body"])
                self.mods.sqs.delete_message(
                    QueueUrl=self.mods.queue_url,
                    ReceiptHandle=message["ReceiptHandle"])
        return bodies

    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def tasks(self, job_id):
        response = self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=Key("job_id").eq(job_id))
        return sorted(response.get("Items", []),
                      key=lambda task: task["task_id"])

    def prelabel_bytes(self, job_id, task_id):
        key = (f"labeling/{self.usecase_id}/{job_id}/prelabels/"
               f"{task_id}.json")
        return self.s3.get_object(Bucket=ARTIFACTS_BUCKET,
                                  Key=key)["Body"].read()


# ------------------------------------------------------------- helpers

def assert_no_detection_prompt(node):
    """Req 1.7: no detection_prompt key anywhere in a non-LLM document."""
    if isinstance(node, dict):
        assert "detection_prompt" not in node
        for value in node.values():
            assert_no_detection_prompt(value)
    elif isinstance(node, list):
        for value in node:
            assert_no_detection_prompt(value)


# ----------------------------------------------------------- strategies

@st.composite
def sam_scenarios(draw):
    """A sam team job plus the fake SAM worker payload for it."""
    modality = draw(st.sampled_from(["Segmentation", "ObjectDetection"]))
    label_set = draw(st.lists(st.sampled_from(LABEL_POOL),
                              min_size=1, max_size=3, unique=True))
    rle = st.lists(st.integers(0, 50), min_size=2, max_size=6).map(
        lambda runs: " ".join(str(run) for run in runs))
    regions = draw(st.lists(
        st.tuples(rle, st.sampled_from([None, 0.5, 0.91])),
        min_size=0, max_size=3))
    sam_width = draw(st.integers(10, 200))
    sam_height = draw(st.integers(10, 200))
    return {"mode": "sam", "modality": modality, "label_set": label_set,
            "regions": regions, "sam_width": sam_width,
            "sam_height": sam_height}


@st.composite
def bedrock_boxes(draw, label_set):
    """0-2 in-bounds integer boxes for the fixed 100x80 image."""
    boxes = []
    for _ in range(draw(st.integers(0, 2))):
        left = draw(st.integers(0, IMAGE_WIDTH - 1))
        top = draw(st.integers(0, IMAGE_HEIGHT - 1))
        boxes.append({
            "class": draw(st.sampled_from(label_set)),
            "left": left,
            "top": top,
            "width": draw(st.integers(1, IMAGE_WIDTH - left)),
            "height": draw(st.integers(1, IMAGE_HEIGHT - top)),
        })
    return boxes


@st.composite
def bedrock_scenarios(draw):
    """A bedrock job — team (auto_label model) or skip-verification
    (the pre-existing bedrock_model_id hardwire)."""
    skip = draw(st.booleans())
    modality = draw(st.sampled_from(["Classification", "ObjectDetection"]))
    if modality == "Classification":
        label_set = ["normal", "anomaly"]
        reply_label = draw(st.sampled_from(label_set))
        boxes = None
    else:
        label_set = draw(st.lists(st.sampled_from(LABEL_POOL),
                                  min_size=1, max_size=3, unique=True))
        reply_label = None
        boxes = draw(bedrock_boxes(label_set))
    return {"mode": "bedrock", "skip_verification": skip,
            "modality": modality, "label_set": label_set,
            "model_id": draw(st.sampled_from(BEDROCK_IDS)),
            "reply_label": reply_label, "boxes": boxes}


@st.composite
def absent_scenarios(draw):
    """A team job with no auto-labeling: auto_label omitted entirely or
    submitted as {'enabled': False}."""
    modality = draw(st.sampled_from(
        ["Classification", "Segmentation", "ObjectDetection"]))
    label_set = (["normal", "anomaly"] if modality == "Classification"
                 else draw(st.lists(st.sampled_from(LABEL_POOL),
                                    min_size=1, max_size=3, unique=True)))
    return {"mode": "absent", "modality": modality, "label_set": label_set,
            "auto_label_body": draw(st.sampled_from(
                [None, {"enabled": False}]))}


@st.composite
def scenarios(draw):
    scenario = draw(st.one_of(sam_scenarios(), bedrock_scenarios(),
                              absent_scenarios()))
    scenario["image_count"] = draw(st.integers(1, 2))
    return scenario


# ---------------------------------------------------------------- property

@settings(deadline=None)
@given(scenario=scenarios())
def test_existing_mode_invariance(henv, scenario):
    """**Feature: llm-auto-labeling, Property 16: Existing-mode
    invariance**

    **Validates: Requirements 1.7**

    For every generated job whose auto-label configuration is `sam`,
    `bedrock:<id>`, or absent, the persisted auto_label sub-document,
    the enqueued SQS message bodies, and the generated Pre_Label
    payload are byte-identical to the pre-change oracle written
    literally below, and no detection_prompt key appears anywhere in
    any job item, message, or Pre_Label document.
    """
    henv.drain_queue()  # leftovers from a previous (failed) example
    mode = scenario["mode"]
    modality = scenario["modality"]
    prefix = henv.put_images(scenario["image_count"])
    submitted_label_set = (None if modality == "Classification"
                           else scenario["label_set"])

    # ------------------------------------------------ create the job
    if mode == "sam":
        job_id = henv.create_job(
            henv.creator, dataset_prefix=prefix, task_type=modality,
            label_set=submitted_label_set, team_id=henv.team_id,
            auto_label={"enabled": True, "model": "sam"})
    elif mode == "bedrock" and not scenario["skip_verification"]:
        job_id = henv.create_job(
            henv.creator, dataset_prefix=prefix, task_type=modality,
            label_set=submitted_label_set, team_id=henv.team_id,
            auto_label={"enabled": True,
                        "model": f"bedrock:{scenario['model_id']}"})
    elif mode == "bedrock":  # skip-verification hardwire
        job_id = henv.create_job(
            henv.admin, dataset_prefix=prefix, task_type=modality,
            label_set=submitted_label_set, skip_verification=True,
            bedrock_model_id=scenario["model_id"],
            per_label_prompts={label: f"Look for {label}."
                               for label in scenario["label_set"]})
    else:  # absent
        job_id = henv.create_job(
            henv.creator, dataset_prefix=prefix, task_type=modality,
            label_set=submitted_label_set, team_id=henv.team_id,
            auto_label=scenario["auto_label_body"])

    # ------------------- surface 1: persisted auto_label sub-document
    job = henv.get_job(job_id)
    if mode == "sam":
        assert job["auto_label"] == {"enabled": True, "model": "sam"}
    elif mode == "bedrock" and not scenario["skip_verification"]:
        assert job["auto_label"] == {
            "enabled": True, "model": f"bedrock:{scenario['model_id']}"}
    else:
        # Skip-verification (bedrock_model_id hardwire) and absent both
        # persist the disabled sub-document, exactly as before.
        assert job["auto_label"] == {"enabled": False}
    assert_no_detection_prompt(job)

    # --------------------------- surface 2: enqueued SQS message bodies
    henv.distribute(job_id)
    tasks = henv.tasks(job_id)
    assert len(tasks) == scenario["image_count"]
    raw_bodies = henv.drain_queue()

    if mode == "absent":
        # No auto-labeling: nothing enqueued, tasks never pend.
        assert raw_bodies == []
        for task in tasks:
            assert task["prelabel_status"] == "None"
        return

    by_task = {json.loads(body)["task_id"]: body for body in raw_bodies}
    assert set(by_task) == {task["task_id"] for task in tasks}

    if mode == "sam" or not scenario["skip_verification"]:
        model = ("sam" if mode == "sam"
                 else f"bedrock:{scenario['model_id']}")
        for task in tasks:
            # Byte-identical to the pre-change body: exactly these six
            # keys in this order, serialized by json.dumps.
            assert by_task[task["task_id"]] == json.dumps({
                "job_id": job_id,
                "task_id": task["task_id"],
                "image_s3_uri": task["image_s3_uri"],
                "modality": modality,
                "label_set": scenario["label_set"],
                "model": model,
            })
    else:
        model = f"bedrock:{scenario['model_id']}"
        for task in tasks:
            raw = by_task[task["task_id"]]
            assert '"detection_prompt"' not in raw
            parsed = json.loads(raw)
            # Exactly the seven pre-change keys — per_label_prompts is
            # compared as a parsed document only because DynamoDB map
            # key order is not contractual.
            assert parsed == {
                "job_id": job_id,
                "task_id": task["task_id"],
                "image_s3_uri": task["image_s3_uri"],
                "modality": modality,
                "label_set": scenario["label_set"],
                "model": model,
                "per_label_prompts": {label: f"Look for {label}."
                                      for label in scenario["label_set"]},
            }
    for body in raw_bodies:
        assert_no_detection_prompt(json.loads(body))

    # ----------------------- surface 3: generated Pre_Label payload
    if mode == "sam":
        henv.use_sam(payload={
            "regions": [
                {"class": "ignored-by-worker", "rle": rle,
                 **({"score": score} if score is not None else {})}
                for rle, score in scenario["regions"]],
            "image_width": scenario["sam_width"],
            "image_height": scenario["sam_height"],
        })
        expected_prelabel = {
            "modality": modality,
            "regions": [
                {"class": None, "rle": rle,
                 **({"score": score} if score is not None else {})}
                for rle, score in scenario["regions"]],
            "image_width": scenario["sam_width"],
            "image_height": scenario["sam_height"],
        }
    elif modality == "Classification":
        henv.use_bedrock(json.dumps({"label": scenario["reply_label"]}))
        expected_prelabel = {
            "modality": "Classification",
            "label": scenario["reply_label"],
        }
    else:
        henv.use_bedrock(json.dumps({"boxes": scenario["boxes"]}))
        expected_prelabel = {
            "modality": "ObjectDetection",
            "boxes": [{"class": box["class"],
                       "left": float(box["left"]),
                       "top": float(box["top"]),
                       "width": float(box["width"]),
                       "height": float(box["height"])}
                      for box in scenario["boxes"]],
            "image_width": IMAGE_WIDTH,
            "image_height": IMAGE_HEIGHT,
        }

    assert henv.run_autolabel(raw_bodies) == {"batchItemFailures": []}
    for task in tasks:
        resolved = henv.stack.tables.labeling_tasks.get_item(
            Key={"job_id": job_id, "task_id": task["task_id"]})["Item"]
        assert resolved["prelabel_status"] == "Available"
        raw = henv.prelabel_bytes(job_id, task["task_id"])
        # Byte-identical to the pre-change Pre_Label object.
        assert raw == json.dumps(expected_prelabel).encode("utf-8")
        assert b"detection_prompt" not in raw
        assert_no_detection_prompt(json.loads(raw))
