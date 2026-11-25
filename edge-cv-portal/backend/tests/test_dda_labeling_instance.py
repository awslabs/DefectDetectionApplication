"""
The `InstanceSegmentation` labeling modality in dda_labeling.py and the
Auto_Labeler worker (rfdetr-seg-support, task 2.4).

Feature: rfdetr-seg-support

Covers, against the moto-backed stack from conftest.py and the labeling
test harnesses (CreateJobEnv, LabelerEnv / SubmissionEnv, the Preview_Run
flow envs and the Grounded-SAM consumer env):

- The modality is accepted everywhere the three existing ones are: job
  creation, the auto-label matrix (sam / grounded-sam / llm offered,
  bedrock not), task fetch and submission (Requirements 2.1, 2.4)
- Submission: an instance with an all-background RLE, a two-vertex
  polygon's empty rasterisation, and an RLE that does not match
  image_size are rejected naming the instance; the client box is
  overwritten with the box derived from the RLE and `classification` is
  derived (Requirement 2.2, DD9); the annotation lands in S3 with
  annotation_s3_key on the item and an S3 failure leaves the task
  Assigned with the existing 500 (Requirement 2.2, design section 2)
- The skip-verification invariant: an InstanceSegmentation job with
  skip_verification is rejected at creation with the new 400
  (Requirements 2.4, 2.5)
- Pre-labels: the Auto_Labeler sends `Segmentation` on the wire to the
  Grounded-SAM worker for an InstanceSegmentation job and stores the
  returned regions; the labeler's task fetch maps each stored region to
  one instance with the derived box, class carried, null kept
  (Requirement 2.4)
- One case per Prompt_Tuning_Preview family: a Grounded-SAM Preview_Run
  and an LLM Preview_Run for InstanceSegmentation are accepted,
  `Segmentation` is on the wire / passed to `generate_llm_prelabel`, and
  each stored result carries `regions` (Requirement 2.1)
"""
import json
import uuid

import pytest

import dda_manifest
from test_dda_grounded_sam_consumer import (  # noqa: F401 — `worker` fixture
    GROUNDED_SAM_FUNCTION,
    GroundedSamEnv,
    worker,
)
from test_dda_gsam_preview_executor import GSAM_FUNCTION, GsamExecutorEnv
from test_dda_labeling_create_job import CreateJobEnv, messages
from test_dda_labeling_labeler_apis import ARTIFACTS_BUCKET
from test_dda_labeling_submission_apis import SubmissionEnv, assert_unsubmitted
from test_preview_flow_integration import (  # noqa: F401 — `dda` fixture
    MODEL as LLM_MODEL,
    PROMPT as LLM_PROMPT,
    PreviewFlowEnv,
    dda,
    guidance,
)

MODALITY = "InstanceSegmentation"
LABELS = ["scratch", "dent"]
WIDTH, HEIGHT = 8, 6


# ---------------------------------------------------------------- helpers

def rle_of(pixels, width=WIDTH, height=HEIGHT):
    """Canonical_RLE of the row-major (x, y) pixel set, through the
    shared-layer encoder (the oracle every codec in the repo pins to)."""
    mask = [0] * (width * height)
    for x, y in pixels:
        mask[y * width + x] = 1
    return dda_manifest.rle_encode(mask, width, height)


def rect(left, top, box_w, box_h):
    return {(x, y) for x in range(left, left + box_w)
            for y in range(top, top + box_h)}


RECT_A = (1, 1, 3, 2)   # left, top, width, height
RECT_B = (5, 3, 2, 3)
RLE_A = rle_of(rect(*RECT_A))
RLE_B = rle_of(rect(*RECT_B))
EMPTY_RLE = str(WIDTH * HEIGHT)         # all background
# What a two-vertex polygon rasterises to: a zero-length foreground run.
TWO_VERTEX_RLE = f"0 0 {WIDTH * HEIGHT}"


def box_dict(left, top, box_w, box_h):
    return {"left": left, "top": top, "width": box_w, "height": box_h}


def instance_annotation(instances, width=WIDTH, height=HEIGHT):
    return {
        "modality": MODALITY,
        "image_size": {"width": width, "height": height},
        "instances": instances,
    }


# --------------------------------------------------------------- fixtures

@pytest.fixture
def create_env(aws_stack, dda):
    return CreateJobEnv(aws_stack, dda)


@pytest.fixture
def labeler_env(aws_stack, dda):
    return SubmissionEnv(aws_stack, dda)


@pytest.fixture
def gsam_env(aws_stack, dda, monkeypatch):
    return GsamExecutorEnv(aws_stack, dda, monkeypatch)


@pytest.fixture
def llm_env(aws_stack, dda, monkeypatch):
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    return PreviewFlowEnv(aws_stack, dda, monkeypatch)


@pytest.fixture
def autolabel_env(aws_stack, worker, monkeypatch):
    return GroundedSamEnv(aws_stack, worker, monkeypatch)


# ------------------------------------------------------------ vocabulary

class TestVocabulary:
    def test_modality_listed_everywhere_the_three_are(self, dda):
        """Req 2.1: VALID_MODALITIES and the auto-label matrix carry the
        fourth value; bedrock (boxes only) does not (Req 2.4)."""
        module = dda.module
        assert module.MODALITY_INSTANCE_SEGMENTATION == MODALITY
        assert module.VALID_MODALITIES == (
            "Classification", "Segmentation", "ObjectDetection", MODALITY)
        matrix = module.AUTO_LABEL_MODEL_MODALITIES
        assert MODALITY in matrix["sam"]
        assert MODALITY in matrix["grounded-sam"]
        assert MODALITY in matrix["llm"]
        assert MODALITY not in matrix["bedrock"]
        # The three existing rows are extended, never relaxed (Req 17.3).
        assert matrix["sam"][:2] == ("Segmentation", "ObjectDetection")
        assert matrix["grounded-sam"][:2] == ("Segmentation",
                                              "ObjectDetection")
        assert matrix["bedrock"] == ("Classification", "ObjectDetection")
        assert matrix["llm"][:3] == ("Classification", "Segmentation",
                                     "ObjectDetection")

    def test_wire_modality_translates_only_the_new_value(self, dda):
        """Req 2.4: InstanceSegmentation rides as Segmentation; every
        other value (and None) is itself."""
        wire = dda.module._wire_modality
        assert wire(MODALITY) == "Segmentation"
        for value in ("Classification", "Segmentation", "ObjectDetection",
                      "Detection", None):
            assert wire(value) == value


# ---------------------------------------------------------- job creation

class TestJobCreation:
    def test_instance_segmentation_job_created(self, create_env):
        """Req 2.1: job creation accepts the modality with a Label_Set."""
        env = create_env
        env.put_images(["a.jpg"])
        status, body = env.create(task_type=MODALITY, label_set=LABELS)
        assert status == 201, body
        job = env.get_job(body["job_id"])
        assert job["task_type"] == MODALITY
        assert job["label_set"] == LABELS

    def test_label_set_required_as_for_the_geometry_modalities(
            self, create_env):
        env = create_env
        env.put_images(["a.jpg"])
        status, body = env.create(task_type=MODALITY, label_set=None)
        assert status == 400
        assert "label set" in messages(body).lower()
        env.assert_nothing_persisted()

    @pytest.mark.parametrize("model,ok", [
        ("sam", True),
        ("grounded-sam", True),
        ("llm:us.amazon.nova-pro-v1:0", True),
        ("bedrock:anthropic.claude-3-haiku", False),
    ])
    def test_auto_label_matrix(self, create_env, model, ok):
        """Req 2.4: sam / grounded-sam / llm pre-label instances; bedrock
        is not offered."""
        env = create_env
        env.put_images(["a.jpg"])
        auto_label = {"enabled": True, "model": model}
        if model.startswith("llm:"):
            auto_label["detection_prompt"] = "Find every defect"
        status, body = env.create(task_type=MODALITY, label_set=LABELS,
                                  auto_label=auto_label)
        if ok:
            assert status == 201, body
            assert env.get_job(body["job_id"])["auto_label"]["model"] == model
        else:
            assert status == 400
            assert "does not support the InstanceSegmentation" in messages(
                body)
            env.assert_nothing_persisted()

    def test_skip_verification_rejected_for_instance_segmentation(
            self, create_env):
        """Req 2.4 / 2.5 invariant (design section 2): a skip-verification
        InstanceSegmentation job is refused at creation with the new 400,
        even with a Bedrock model and complete per-label prompts."""
        env = create_env
        env.put_images(["a.jpg"])
        status, body = env.create(
            user=env.make_user(role="PortalAdmin"),
            task_type=MODALITY, label_set=LABELS, team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts={"scratch": "Find scratches",
                               "dent": "Find dents"})
        assert status == 400
        errors = body["validation_errors"]
        assert any(
            error["parameter"] == "skip_verification"
            and error["message"] == (
                "Skip-verification mode is not available for "
                "InstanceSegmentation (no Bedrock pre-labeler supports it)")
            for error in errors), errors
        env.assert_nothing_persisted()

    def test_skip_verification_unchanged_for_object_detection(
            self, create_env):
        """The three existing modalities' skip-verification behaviour is
        unchanged (Req 2.1, 17.1)."""
        env = create_env
        env.put_images(["a.jpg"])
        status, body = env.create(
            user=env.make_user(role="PortalAdmin"),
            task_type="ObjectDetection", label_set=LABELS, team_id=None,
            skip_verification=True,
            bedrock_model_id="anthropic.claude-3-haiku",
            per_label_prompts={"scratch": "Find scratches",
                               "dent": "Find dents"})
        assert status == 201, body
        assert env.get_job(body["job_id"])["skip_verification"] is True


# ------------------------------------------------------------- submission

class TestSubmission:
    def _job_and_task(self, env, image_count=5):
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type=MODALITY, label_set=LABELS,
                             image_count=image_count)
        env.put_task(job_id, "task-0000000", caller)
        return job_id

    def test_submission_lands_in_s3_with_recomputed_boxes(self, labeler_env):
        """Req 2.2 / DD9: the stored annotation carries the box derived
        from each RLE (the client box is advisory and overwritten),
        `classification` is derived, the payload is written to S3 under
        labeling/{usecase}/{job}/annotations/{task}.json, and the item
        carries annotation_s3_key and no inline annotation."""
        env = labeler_env
        caller = env.labeler["user_id"]
        job_id = self._job_and_task(env)

        annotation = instance_annotation([
            # A stale client box: left/top off by one, width too large.
            {"class": "scratch", "rle": RLE_A, "box": box_dict(0, 0, 7, 5)},
            # No client box at all.
            {"class": "dent", "rle": RLE_B},
        ])
        status, body = env.submit("task-0000000", job_id, annotation)
        assert status == 200, body
        assert body["status"] == "Submitted"

        task = env.get_task(job_id, "task-0000000")
        assert task["status"] == "Submitted"
        assert "annotation" not in task
        expected_key = (f"labeling/{env.usecase_id}/{job_id}/"
                        f"annotations/task-0000000.json")
        assert task["annotation_s3_key"] == expected_key
        assert task["submitted_by"] == caller
        assert task["human_annotated"] is True

        stored = json.loads(env.s3.get_object(
            Bucket=ARTIFACTS_BUCKET, Key=expected_key)["Body"].read())
        assert stored == {
            "modality": MODALITY,
            "image_size": {"width": WIDTH, "height": HEIGHT},
            "instances": [
                {"class": "scratch", "rle": RLE_A, "box": box_dict(*RECT_A)},
                {"class": "dent", "rle": RLE_B, "box": box_dict(*RECT_B)},
            ],
            "classification": "anomaly",
        }

    def test_zero_instances_is_a_normal_image(self, labeler_env):
        """Req 2.2: classification derives as 'normal' iff instances is
        empty — an image with no defect is a valid submission."""
        env = labeler_env
        job_id = self._job_and_task(env)
        status, _ = env.submit("task-0000000", job_id,
                               instance_annotation([]))
        assert status == 200
        task = env.get_task(job_id, "task-0000000")
        stored = json.loads(env.s3.get_object(
            Bucket=ARTIFACTS_BUCKET,
            Key=task["annotation_s3_key"])["Body"].read())
        assert stored["instances"] == []
        assert stored["classification"] == "normal"

    @pytest.mark.parametrize("rle,expected", [
        (EMPTY_RLE, "Instance 1 has no foreground pixels"),
        (TWO_VERTEX_RLE, "Instance 1 has no foreground pixels"),
        (rle_of(rect(0, 0, 2, 2), width=4, height=4),
         "Instance 1: rle does not match image_size"),
        ("3 x 4", "Instance 1: rle does not match image_size"),
    ])
    def test_empty_or_mismatching_instance_rejected(self, labeler_env, rle,
                                                   expected):
        """Req 2.2: an instance whose RLE paints no pixel (an empty mask
        or a two-vertex polygon's zero-length foreground run) or does not
        match image_size is rejected naming the instance; the task stays
        unsubmitted and nothing reaches S3."""
        env = labeler_env
        job_id = self._job_and_task(env)
        annotation = instance_annotation([
            {"class": "scratch", "rle": RLE_A},
            {"class": "dent", "rle": rle},
        ])
        status, body = env.submit("task-0000000", job_id, annotation)
        assert status == 400
        assert any(error["parameter"] == "instances"
                   and error["message"] == expected
                   and error["instance_index"] == 1
                   for error in body["validation_errors"]), body
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))
        listed = env.s3.list_objects_v2(
            Bucket=ARTIFACTS_BUCKET,
            Prefix=f"labeling/{env.usecase_id}/{job_id}/annotations/")
        assert listed.get("KeyCount", 0) == 0

    def test_unclassified_and_rle_less_instances_rejected(self, labeler_env):
        """Req 2.2: every instance needs a Label_Set class (null — an
        unclassified proposal — is rejected) and an RLE string."""
        env = labeler_env
        job_id = self._job_and_task(env)
        status, body = env.submit("task-0000000", job_id, instance_annotation([
            {"class": None, "rle": RLE_A},
            {"class": "scratch"},
            {"class": "crack", "rle": RLE_B},
        ]))
        assert status == 400
        by_index = {}
        for error in body["validation_errors"]:
            if error["parameter"] == "instances":
                by_index.setdefault(error["instance_index"], []).append(
                    error["message"])
        assert by_index[0] == ["Instance 0 lacks a class from the label set"]
        assert by_index[1] == ["Instance 1 lacks RLE mask data"]
        assert by_index[2] == ["Instance 2 lacks a class from the label set"]
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))

    def test_image_size_required(self, labeler_env):
        env = labeler_env
        job_id = self._job_and_task(env)
        status, body = env.submit("task-0000000", job_id, {
            "modality": MODALITY,
            "instances": [{"class": "scratch", "rle": RLE_A}],
        })
        assert status == 400
        assert any(error["parameter"] == "image_size"
                   for error in body["validation_errors"])
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))

    def test_s3_failure_returns_500_and_leaves_the_task_assigned(
            self, labeler_env, monkeypatch):
        """Design section 2 (submit storage): the annotation object is
        written before the conditional write, so an S3 failure answers
        the existing 500 and the Task_Assignment stays Assigned."""
        env = labeler_env
        job_id = self._job_and_task(env)
        real_s3 = env.dda.module.s3_client

        class _FailingPutObject:
            def put_object(self, **kwargs):
                raise RuntimeError("simulated S3 outage")

            def __getattr__(self, name):
                return getattr(real_s3, name)

        monkeypatch.setattr(env.dda.module, "s3_client", _FailingPutObject())
        status, body = env.submit("task-0000000", job_id, instance_annotation([
            {"class": "scratch", "rle": RLE_A}]))
        assert status == 500
        assert body == {"error": "Submission could not be saved; the task "
                                 "remains unsubmitted"}
        assert_unsubmitted(env.get_task(job_id, "task-0000000"))

    def test_modality_mismatch_rejected(self, labeler_env):
        """Req 7.8 of the labeling spec still applies: a Segmentation
        payload against an InstanceSegmentation job is refused."""
        env = labeler_env
        job_id = self._job_and_task(env)
        status, body = env.submit("task-0000000", job_id, {
            "modality": "Segmentation",
            "image_size": {"width": WIDTH, "height": HEIGHT},
            "regions": [{"class": "scratch", "rle": RLE_A}],
        })
        assert status == 400
        assert body["validation_errors"][0]["parameter"] == "modality"


# ------------------------------------------------------------- task fetch

class TestTaskFetchPrelabelMapping:
    def test_segmentation_shaped_prelabel_becomes_instances(self, labeler_env):
        """Req 2.4 / DD9: the stored pre-label (Segmentation on the wire)
        is presented as one instance per region, class carried and null
        kept, each with the box derived from its RLE and the image size
        the worker recorded."""
        env = labeler_env
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type=MODALITY, label_set=LABELS,
                             image_count=1)
        stored = {
            "modality": "Segmentation",
            "regions": [
                {"class": "scratch", "rle": RLE_A, "score": 0.9},
                {"class": None, "rle": RLE_B},
            ],
            "image_width": WIDTH,
            "image_height": HEIGHT,
        }
        key = env.put_prelabel(job_id, "task-0000000", stored)
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Available", prelabel_s3_key=key)

        status, body = env.next_task(job_id)
        assert status == 200, body
        assert body["task_type"] == MODALITY
        assert body["prelabel"] == {
            "modality": MODALITY,
            "image_size": {"width": WIDTH, "height": HEIGHT},
            "image_width": WIDTH,
            "image_height": HEIGHT,
            "instances": [
                {"class": "scratch", "rle": RLE_A, "box": box_dict(*RECT_A)},
                {"class": None, "rle": RLE_B, "box": box_dict(*RECT_B)},
            ],
        }
        # The stored object is never modified (the mapping is read-side).
        assert json.loads(env.s3.get_object(
            Bucket=ARTIFACTS_BUCKET, Key=key)["Body"].read()) == stored

    def test_unusable_regions_dropped_and_segmentation_job_untouched(
            self, labeler_env):
        """A region that cannot become a submittable instance (empty mask
        or counts not matching the recorded size) is dropped; a
        Segmentation job's pre-label is presented exactly as stored."""
        env = labeler_env
        caller = env.labeler["user_id"]
        job_id = env.put_job(task_type=MODALITY, label_set=LABELS,
                             image_count=1)
        key = env.put_prelabel(job_id, "task-0000000", {
            "modality": "Segmentation",
            "regions": [{"class": "scratch", "rle": EMPTY_RLE},
                        {"class": "dent", "rle": "1 2 3"},
                        {"class": "dent", "rle": RLE_B}],
            "image_width": WIDTH, "image_height": HEIGHT})
        env.put_task(job_id, "task-0000000", caller,
                     prelabel_status="Available", prelabel_s3_key=key)
        status, body = env.next_task(job_id)
        assert status == 200
        assert body["prelabel"]["instances"] == [
            {"class": "dent", "rle": RLE_B, "box": box_dict(*RECT_B)}]

        seg_job = env.put_job(task_type="Segmentation", label_set=LABELS,
                              image_count=1)
        seg_prelabel = {"modality": "Segmentation",
                        "regions": [{"class": "scratch", "rle": RLE_A}],
                        "image_width": WIDTH, "image_height": HEIGHT}
        seg_key = env.put_prelabel(seg_job, "task-0000000", seg_prelabel)
        env.put_task(seg_job, "task-0000000", caller,
                     prelabel_status="Available", prelabel_s3_key=seg_key)
        status, body = env.next_task(seg_job)
        assert status == 200
        assert body["prelabel"] == seg_prelabel


# ------------------------------------------------- Auto_Labeler (worker)

class TestAutoLabelerWire:
    def test_grounded_sam_payload_carries_segmentation_for_instances(
            self, autolabel_env):
        """Req 2.4: for an InstanceSegmentation job the worker sends
        `Segmentation` to the Grounded-SAM worker (contract unchanged)
        and stores the returned regions one-to-one, Segmentation-shaped."""
        env = autolabel_env
        job_id = env.make_job(task_type=MODALITY, label_set=LABELS,
                              model="grounded-sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        fake = env.use_grounded_sam(payload={
            "regions": [{"class": "scratch", "rle": RLE_A, "score": 0.8},
                        {"class": "dent", "rle": RLE_B}],
            "image_width": WIDTH, "image_height": HEIGHT})

        result = env.run([env.record(job_id, task_id, image_uri, MODALITY,
                                     LABELS, "grounded-sam")])

        assert result == {"batchItemFailures": []}
        assert len(fake.invocations) == 1
        payload = json.loads(fake.invocations[0]["Payload"])
        assert payload["modality"] == "Segmentation"
        assert payload["prompts"] == [
            {"label": label, "prompt": label} for label in LABELS]
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        assert env.prelabel_json(job_id, task_id) == {
            "modality": "Segmentation",
            "regions": [{"class": "scratch", "rle": RLE_A, "score": 0.8},
                        {"class": "dent", "rle": RLE_B}],
            "image_width": WIDTH, "image_height": HEIGHT}

    def test_sam_prelabel_is_segmentation_shaped_for_instances(
            self, autolabel_env):
        """Req 2.4: the sam family stores class-null regions under the
        Segmentation modality for an InstanceSegmentation job."""
        env = autolabel_env
        job_id = env.make_job(task_type=MODALITY, label_set=LABELS,
                              model="sam")
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        fake = env.use_sam(payload={
            "regions": [{"rle": RLE_A}, {"rle": RLE_B, "score": 0.5}],
            "image_width": WIDTH, "image_height": HEIGHT})

        result = env.run([env.record(job_id, task_id, image_uri, MODALITY,
                                     LABELS, "sam")])

        assert result == {"batchItemFailures": []}
        assert len(fake.invocations) == 1
        assert env.prelabel_json(job_id, task_id) == {
            "modality": "Segmentation",
            "regions": [{"class": None, "rle": RLE_A},
                        {"class": None, "rle": RLE_B, "score": 0.5}],
            "image_width": WIDTH, "image_height": HEIGHT}

    def test_bedrock_refuses_the_modality(self, autolabel_env):
        """Req 2.4: bedrock is not a pre-labeler for instances; the
        worker records the generation failure (no batch failure)."""
        env = autolabel_env
        job_id = env.make_job(task_type=MODALITY, label_set=LABELS)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png")
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock(replies=['{"label": "anomaly"}'])

        result = env.run([env.record(job_id, task_id, image_uri, MODALITY,
                                     LABELS, "bedrock:test-model-id")])

        assert result == {"batchItemFailures": []}
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert "does not support the InstanceSegmentation" in task[
            "prelabel_error"]
        assert bedrock.calls == []

    def test_wire_modality_helper(self, worker):
        assert worker._wire_modality(MODALITY) == "Segmentation"
        for value in ("Classification", "Segmentation", "ObjectDetection"):
            assert worker._wire_modality(value) == value
        assert MODALITY in worker.SAM_MODALITIES
        assert MODALITY in worker.GROUNDED_SAM_MODALITIES
        assert MODALITY not in worker.BEDROCK_MODALITIES


# ------------------------------------------- Prompt_Tuning_Preview families

class TestPreviewRunFamilies:
    def test_grounded_sam_preview_run_accepted_with_segmentation_on_the_wire(
            self, gsam_env):
        """Req 2.1 (C18): a Grounded-SAM Preview_Run for InstanceSegmentation
        starts (202), the executor sends `Segmentation` to the worker and
        the stored result carries `regions`."""
        env = gsam_env
        key = env.put_sample("instance-gsam.png")
        fake = env.use_worker()

        status, started = env.start(
            model="grounded-sam", detection_prompt=None,
            task_type=MODALITY, label_set=LABELS, sample_images=[key])
        assert status == 202, started
        run_id = started["run_id"]

        outcome = env.drive_executor()
        assert outcome["status"] == "Completed"
        assert outcome["succeeded"] == 1

        assert len(fake.invocations) == 1
        payload = json.loads(fake.invocations[0]["Payload"])
        assert payload["modality"] == "Segmentation"
        assert fake.invocations[0]["FunctionName"] == GSAM_FUNCTION

        result = env.result_payload(run_id, 0)
        assert result["state"] == "Succeeded"
        assert result["prelabel"]["regions"] == [
            {"class": "scratch", "rle": "0 12 5 3", "score": 0.87}]
        assert result["prelabel"]["modality"] == "Segmentation"
        # The run item keeps the requested modality.
        assert env.run_item(run_id)["task_type"] == MODALITY

    def test_grounded_sam_preview_gate_names_the_three_modalities(
            self, gsam_env):
        """Req 2.1: the start route's family gate admits the new modality
        through the extended matrix and its message names all three."""
        env = gsam_env
        env.use_worker()
        status, body = env.start(
            model="grounded-sam", detection_prompt=None,
            task_type="Classification", label_set=LABELS,
            sample_images=[env.put_sample("instance-gate.png")])
        assert status == 400
        assert any(
            error["parameter"] == "task_type"
            and error["message"] == (
                "The grounded-sam family supports Segmentation, "
                "InstanceSegmentation and ObjectDetection")
            for error in body["validation_errors"]), body

    def test_llm_preview_run_passes_segmentation_to_generate_llm_prelabel(
            self, llm_env):
        """Req 2.1 (C18): an LLM Preview_Run for InstanceSegmentation
        starts (202) and the executor calls the real `generate_llm_prelabel`
        with `Segmentation`, so `guidance_to_prelabel` rasterises the
        model's polygon into a Segmentation-shaped result with `regions`
        (unwired, it would raise 'unsupported modality')."""
        env = llm_env
        key = env.put_sample("instance-llm.png", width=120, height=90)
        stub, _ = env.use_bedrock([guidance([
            {"class": "dent", "polygon": [[10, 20], [48, 22], [40, 60],
                                          [12, 55]]}])])

        status, started = env.start(
            model=LLM_MODEL, detection_prompt=LLM_PROMPT,
            task_type=MODALITY, label_set=LABELS, sample_images=[key])
        assert status == 202, started
        run_id = started["run_id"]

        outcome = env.drive_executor()
        assert outcome["status"] == "Completed"
        assert outcome["succeeded"] == 1, outcome
        assert len(stub.calls) == 1

        result = env.result_payload(run_id, 0)
        assert result["state"] == "Succeeded"
        prelabel = result["prelabel"]
        assert prelabel["modality"] == "Segmentation"
        assert [region["class"] for region in prelabel["regions"]] == ["dent"]
        assert dda_manifest._rle_has_foreground(prelabel["regions"][0]["rle"])
        assert prelabel["image_width"] == 120
        assert prelabel["image_height"] == 90
        assert env.run_item(run_id)["task_type"] == MODALITY

    def test_llm_preview_call_receives_the_wire_modality(self, llm_env):
        """The `generate_llm_prelabel` seam sees `Segmentation`, not the
        job's modality (Req 2.4)."""
        env = llm_env
        key = env.put_sample("instance-llm-seam.png", width=64, height=48)
        calls = []

        def recording_prelabel(**kwargs):
            calls.append(kwargs)
            return {"modality": "Segmentation", "regions": [],
                    "image_width": kwargs["width"],
                    "image_height": kwargs["height"]}

        env.monkeypatch.setattr(env.module, "generate_llm_prelabel",
                                recording_prelabel)
        status, started = env.start(
            model=LLM_MODEL, detection_prompt=LLM_PROMPT,
            task_type=MODALITY, label_set=LABELS, sample_images=[key])
        assert status == 202, started
        outcome = env.drive_executor()
        assert outcome["succeeded"] == 1
        assert len(calls) == 1
        assert calls[0]["modality"] == "Segmentation"
        assert calls[0]["label_set"] == LABELS
        assert env.result_payload(started["run_id"], 0)["prelabel"][
            "regions"] == []
