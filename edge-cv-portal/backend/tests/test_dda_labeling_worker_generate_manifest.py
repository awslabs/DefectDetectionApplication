"""
dda_labeling_worker.py generate_manifest action (dda-data-labeling,
task 12.1).

Feature: dda-data-labeling

Covers, against the moto-backed stack from conftest.py (real
shared_utils + dda_manifest, moto DynamoDB + S3), seeding job and task
items directly in DynamoDB and invoking the worker handler with
{action: 'generate_manifest', job_id}:

- Classification team job end-to-end: JSON Lines content per Req 10.3
  (source-ref, anomaly-label 0/1, anomaly-label-metadata with
  class-name, confidence, type, job-name, human-annotated 'yes',
  creation-date = submitted_at_iso), output_manifest_s3_uri recorded
  (the same field GT jobs use, Req 10.8), status Completed +
  completed_at, job_completed audit event (Req 10.1, 11.6, 11.7);
  non-Submitted tasks excluded (Req 10.2)
- Segmentation: PNG masks rendered with the job-wide color map and
  written under labeled/{job_id}/masks/ with colon-free keys; decoded
  through dda_manifest.decode_mask_png back to the submitted regions;
  anomaly-mask-ref / anomaly-mask-ref-metadata emission (Req 10.4)
- Object detection: the GT bounding-box structure with zero-based
  class ids and the class-map metadata (Req 10.5)
- Skip-verification: exactly the accepted results included, rejected
  and failed excluded (Req 9.9), human-annotated 'no' (Req 9.11)
- S3 write failure: status Failed with failure_reason, no manifest
  URI, annotations untouched (Req 10.9, 12.5)
- Validation failure is generation failure (Req 10.6)

Feature: llm-auto-labeling (task 16.1) — manifest indistinguishability
end to end, against real dda_llm_guidance-built Pre_Labels:

- A Segmentation team job whose submissions originate from LLM
  pre-labels (image_width/image_height, no image_size — the LLM
  pre-label shape) renders PNG masks through the job-wide color map,
  exercising the task 8 _canonical_annotation normalization
  (Req 8.1, 8.3, 8.6)
- A Segmentation skip-verification job whose accepted results are LLM
  pre-labels generates a manifest through the pre-label ->
  _canonical_annotation -> render_mask_png path (Req 8.1, 8.3, 8.5)
- ObjectDetection and Classification LLM jobs generate manifests that
  pass _validate_manifest_lines untransformed, with no LLM-specific
  attributes in any entry (Req 8.2, 8.4)
- A team job where every pre-label failed and every task was labeled
  from scratch still produces one entry per submission (Req 8.6)
- Skip-verification includes exactly the accepted images and excludes
  rejected ones; team submissions record human-annotated 'yes' and
  accepted skip-verification results 'no' (Req 8.5, 8.6)
"""
import json
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key

import dda_llm_guidance
import dda_manifest

REGION = "us-east-1"
OUTPUT_BUCKET = "test-manifest-usecase-output"
PORTAL_BUCKET = "test-portal-artifacts"  # created by conftest
DATASET_BUCKET = "test-manifest-dataset"


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_labeling_worker imported inside the moto mock."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling_worker

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=OUTPUT_BUCKET)
    return dda_labeling_worker


@pytest.fixture
def env(aws_stack, worker):
    return ManifestEnv(aws_stack, worker)


class ManifestEnv:
    """Per-test facade: fresh Use_Case whose s3_bucket is the output
    bucket, and helpers seeding job/task items in the shape
    create_dda_job / distribute / submit_labeler_task persist."""

    def __init__(self, stack, worker):
        self.stack = stack
        self.worker = worker
        self.s3 = boto3.client("s3", region_name=REGION)
        self.usecase_id = f"uc-{uuid.uuid4()}"
        self.creator = f"user-{uuid.uuid4()}"
        # Single-account use case: root cross_account_role_arn makes
        # get_s3_client_for_bucket fall back to default (moto) creds.
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Manifest Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": OUTPUT_BUCKET,
        })

    # ------------------------------------------------------------ seeding
    def put_job(self, task_type="Classification", label_set=None,
                image_count=0, skip_verification=False, **overrides):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{uuid.uuid4().hex[:8]}",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "task_type": task_type,
            "label_set": label_set or ["normal", "anomaly"],
            "dataset_bucket": DATASET_BUCKET,
            "dataset_prefix": "datasets/x/",
            "image_count": image_count,
            "skip_verification": skip_verification,
            "submitted_count": image_count,
            "created_at": 1,
            "updated_at": 1,
            "created_by": self.creator,
        }
        item.update(overrides)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, index, image_name=None, **attrs):
        task_id = f"task-{index:06d}"
        image_key = f"datasets/x/{image_name or f'img-{index:03d}.jpg'}"
        item = {
            "job_id": job_id,
            "task_id": task_id,
            "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
            "image_key": image_key,
            "usecase_id": self.usecase_id,
            "assignee_user_id": f"labeler-{uuid.uuid4().hex[:8]}",
            "status": "Assigned",
            "prelabel_status": "None",
            "created_at": 1,
        }
        item.update(attrs)
        self.stack.tables.labeling_tasks.put_item(Item=item)
        return item

    def submitted_task(self, job_id, index, annotation=None,
                       inline=True, image_name=None, **attrs):
        """A task in the shape submit_labeler_task leaves behind."""
        submission = {
            "status": "Submitted",
            "submitted_by": f"labeler-{uuid.uuid4().hex[:8]}",
            "submitted_at": 1700000000 + index,
            "submitted_at_iso": f"2023-11-14T22:13:{20 + index:02d}Z",
            "human_annotated": True,
        }
        if annotation is not None:
            if inline:
                submission["annotation"] = annotation
            else:
                key = (f"labeling/{self.usecase_id}/{job_id}/"
                       f"annotations/task-{index:06d}.json")
                self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                                   Body=json.dumps(annotation).encode())
                submission["annotation_s3_key"] = key
        submission.update(attrs)
        return self.put_task(job_id, index, image_name=image_name,
                             **submission)

    def auto_task(self, job_id, index, prelabel=None, decision=None,
                  failed=False):
        """A skip-verification result item as the auto-label worker
        (and the review-decision API) leave it."""
        attrs = {"assignee_user_id": "AUTO", "updated_at": 1700000000}
        if failed:
            attrs["prelabel_status"] = "Failed"
            attrs["autolabel_error"] = "model failure"
        else:
            key = (f"labeling/{self.usecase_id}/{job_id}/"
                   f"prelabels/task-{index:06d}.json")
            self.s3.put_object(Bucket=PORTAL_BUCKET, Key=key,
                               Body=json.dumps(prelabel).encode())
            attrs["prelabel_status"] = "Available"
            attrs["prelabel_s3_key"] = key
        if decision:
            attrs["review_decision"] = decision
        return self.put_task(job_id, index, **attrs)

    # ------------------------------------------------------------ invoke
    def generate(self, job_id):
        return self.worker.handler(
            {"action": "generate_manifest", "job_id": job_id}, None)

    # ------------------------------------------------------------- store
    def get_job(self, job_id):
        return self.stack.tables.labeling_jobs.get_item(
            Key={"job_id": job_id}).get("Item")

    def tasks(self, job_id):
        return self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=Key("job_id").eq(job_id),
        ).get("Items", [])

    def manifest_entries(self, job_id):
        body = self.s3.get_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"labeled/{job_id}/output.manifest")["Body"].read()
        return [json.loads(line)
                for line in body.decode().strip().split("\n")]

    def audit_events(self, job_id, action):
        return self.stack.tables.audit_log.scan(
            FilterExpression=(Attr("resource_id").eq(job_id)
                              & Attr("action").eq(action)),
        ).get("Items", [])


# ------------------------------------------------ classification team job

class TestClassificationTeamJob:
    def test_end_to_end(self, env):
        """Req 10.1/10.2/10.3/10.8/11.6/11.7: one JSON Lines object per
        submitted task with the exact classification fields; the
        manifest URI recorded in the GT jobs' field; Completed +
        completed_at; job_completed audit event; non-submitted tasks
        excluded."""
        job_id = env.put_job(image_count=3)
        labels = ["anomaly", "normal", "anomaly"]
        tasks = [
            env.submitted_task(job_id, i, annotation={
                "modality": "Classification", "label": labels[i]})
            for i in range(3)
        ]
        # Excluded from the manifest (Req 10.2): a withheld task never
        # carries a submitted annotation.
        env.put_task(job_id, 3, status="PresentationFailed")

        result = env.generate(job_id)
        assert result["status"] == "Completed"
        assert result["entry_count"] == 3

        job = env.get_job(job_id)
        expected_uri = (f"s3://{OUTPUT_BUCKET}/labeled/{job_id}/"
                        f"output.manifest")
        assert result["output_manifest_s3_uri"] == expected_uri
        assert job["output_manifest_s3_uri"] == expected_uri
        assert job["status"] == "Completed"
        assert job["completed_at"]

        entries = env.manifest_entries(job_id)
        assert len(entries) == 3
        by_ref = {entry["source-ref"]: entry for entry in entries}
        assert set(by_ref) == {task["image_s3_uri"] for task in tasks}
        for i, task in enumerate(tasks):
            entry = by_ref[task["image_s3_uri"]]
            assert set(entry) == {"source-ref", "anomaly-label",
                                  "anomaly-label-metadata"}
            assert entry["anomaly-label"] == (
                1 if labels[i] == "anomaly" else 0)
            metadata = entry["anomaly-label-metadata"]
            assert metadata["class-name"] == labels[i]
            assert metadata["confidence"] == 1.0
            assert metadata["type"] == "groundtruth/image-classification"
            assert metadata["job-name"] == job["job_name"]
            assert metadata["human-annotated"] == "yes"
            assert metadata["creation-date"] == task["submitted_at_iso"]

        events = env.audit_events(job_id, "job_completed")
        assert len(events) == 1
        assert events[0]["user_id"] == env.creator
        assert events[0]["result"] == "success"
        assert (events[0]["details"]["output_manifest_s3_uri"]
                == expected_uri)


# ------------------------------------------------------------ segmentation

class TestSegmentation:
    WIDTH, HEIGHT = 4, 3

    def region(self, class_name, pixels):
        mask = [0] * (self.WIDTH * self.HEIGHT)
        for x, y in pixels:
            mask[y * self.WIDTH + x] = 1
        return {"class": class_name,
                "rle": dda_manifest.rle_encode(mask, self.WIDTH,
                                               self.HEIGHT)}

    def test_masks_rendered_with_job_color_map(self, env):
        """Req 10.4: masks rendered as PNGs with the job-wide color
        map, written under labeled/{job_id}/masks/ with colon-free
        keys; every entry carries anomaly-mask-ref plus the identical
        internal-color-map."""
        label_set = ["scratch", "dent"]
        job_id = env.put_job(task_type="Segmentation",
                             label_set=label_set, image_count=2)
        regions_by_task = [
            [self.region("scratch", [(0, 0), (1, 0)]),
             self.region("dent", [(3, 2)])],
            [self.region("dent", [(2, 1)])],
        ]
        tasks = []
        for i, regions in enumerate(regions_by_task):
            annotation = {
                "modality": "Segmentation",
                "image_size": {"width": self.WIDTH,
                               "height": self.HEIGHT},
                "regions": regions,
                "classification": "anomaly",
            }
            # Segmentation submissions persist through annotation_s3_key
            # (the submit path's shape).
            tasks.append(env.submitted_task(
                job_id, i, annotation=annotation, inline=False))

        result = env.generate(job_id)
        assert result["status"] == "Completed"

        color_map = dda_manifest.build_color_map(label_set)
        entries = env.manifest_entries(job_id)
        assert len(entries) == 2
        by_ref = {entry["source-ref"]: entry for entry in entries}
        for i, task in enumerate(tasks):
            entry = by_ref[task["image_s3_uri"]]
            mask_uri = entry["anomaly-mask-ref"]
            expected_stem = task["image_key"].rsplit(
                "/", 1)[-1].rsplit(".", 1)[0]
            assert mask_uri == (f"s3://{OUTPUT_BUCKET}/labeled/{job_id}/"
                                f"masks/{expected_stem}.png")
            mask_key = mask_uri[len(f"s3://{OUTPUT_BUCKET}/"):]
            assert ":" not in mask_key
            mask_metadata = entry["anomaly-mask-ref-metadata"]
            assert mask_metadata["internal-color-map"] == color_map
            assert mask_metadata["type"] == (
                "groundtruth/semantic-segmentation")
            assert entry["anomaly-label"] == 1

            # Decode the rendered PNG back through the color map: the
            # regions must be pixel-identical to the submission.
            png = env.s3.get_object(Bucket=OUTPUT_BUCKET,
                                    Key=mask_key)["Body"].read()
            decoded_regions, width, height = dda_manifest.decode_mask_png(
                png, color_map)
            assert (width, height) == (self.WIDTH, self.HEIGHT)
            expected = {region["class"]: region["rle"]
                        for region in regions_by_task[i]}
            assert {region["class"]: region["rle"]
                    for region in decoded_regions} == expected


# -------------------------------------------------------- object detection

class TestObjectDetection:
    def test_gt_bounding_box_structure(self, env):
        """Req 10.5: the SageMaker GT bounding-box structure with
        zero-based Label_Set class ids, in-bounds pixel coordinates,
        and the class-map metadata."""
        label_set = ["scratch", "dent"]
        job_id = env.put_job(task_type="ObjectDetection",
                             label_set=label_set, image_count=1)
        boxes = [
            {"class": "dent", "left": 10, "top": 20,
             "width": 30, "height": 40},
            {"class": "scratch", "left": 0, "top": 0,
             "width": 5, "height": 5},
        ]
        task = env.submitted_task(job_id, 0, annotation={
            "modality": "ObjectDetection",
            "image_size": {"width": 100, "height": 80},
            "boxes": boxes,
        })

        result = env.generate(job_id)
        assert result["status"] == "Completed"

        entries = env.manifest_entries(job_id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["source-ref"] == task["image_s3_uri"]
        assert entry["bounding-box"] == {
            "image_size": [{"width": 100, "height": 80, "depth": 3}],
            "annotations": [
                {"class_id": 1, "left": 10, "top": 20,
                 "width": 30, "height": 40},
                {"class_id": 0, "left": 0, "top": 0,
                 "width": 5, "height": 5},
            ],
        }
        metadata = entry["bounding-box-metadata"]
        assert metadata["class-map"] == {"0": "scratch", "1": "dent"}
        assert metadata["type"] == "groundtruth/object-detection"
        assert metadata["human-annotated"] == "yes"
        assert metadata["job-name"] == env.get_job(job_id)["job_name"]
        assert len(metadata["objects"]) == 2


# ------------------------------------------------------- skip-verification

class TestSkipVerification:
    def test_exactly_accepted_results_included(self, env):
        """Req 9.9/9.11: exactly the accepted results are emitted, with
        the pre-label as the annotation and human-annotated 'no';
        rejected and failed images are excluded."""
        job_id = env.put_job(image_count=4, skip_verification=True,
                             team_id=None,
                             bedrock_model_id="anthropic.claude-3-haiku")
        accepted = [
            env.auto_task(job_id, 0, decision="accepted", prelabel={
                "modality": "Classification", "label": "anomaly"}),
            env.auto_task(job_id, 1, decision="accepted", prelabel={
                "modality": "Classification", "label": "normal"}),
        ]
        env.auto_task(job_id, 2, decision="rejected", prelabel={
            "modality": "Classification", "label": "anomaly"})
        env.auto_task(job_id, 3, failed=True)

        result = env.generate(job_id)
        assert result["status"] == "Completed"
        assert result["entry_count"] == 2

        entries = env.manifest_entries(job_id)
        assert {entry["source-ref"] for entry in entries} == {
            task["image_s3_uri"] for task in accepted}
        by_ref = {entry["source-ref"]: entry for entry in entries}
        assert by_ref[accepted[0]["image_s3_uri"]]["anomaly-label"] == 1
        assert by_ref[accepted[1]["image_s3_uri"]]["anomaly-label"] == 0
        for entry in entries:
            metadata = entry["anomaly-label-metadata"]
            assert metadata["human-annotated"] == "no"
            assert 0 <= metadata["confidence"] <= 1
            assert metadata["creation-date"]

        assert env.get_job(job_id)["status"] == "Completed"


# --------------------------------------------------------- failure paths

class FailingS3Client:
    """Output-bucket client whose writes always fail."""

    def put_object(self, **kwargs):
        raise RuntimeError("simulated S3 outage")


class TestFailureAtomicity:
    def seed_classification_job(self, env, count=2):
        job_id = env.put_job(image_count=count)
        tasks = [
            env.submitted_task(job_id, i, annotation={
                "modality": "Classification", "label": "anomaly"})
            for i in range(count)
        ]
        return job_id, tasks

    def test_s3_write_failure_fails_job_without_uri(self, env,
                                                    monkeypatch):
        """Req 10.9/12.5: an output-bucket write failure records no
        manifest URI, sets the job Failed with a failure_reason, and
        leaves every persisted annotation untouched."""
        job_id, tasks = self.seed_classification_job(env)
        monkeypatch.setattr(env.worker, "get_s3_client_for_bucket",
                            lambda *args, **kwargs: FailingS3Client())

        result = env.generate(job_id)
        assert result["status"] == "Failed"

        job = env.get_job(job_id)
        assert job["status"] == "Failed"
        assert "simulated S3 outage" in job["failure_reason"]
        assert "output_manifest_s3_uri" not in job

        stored = {task["task_id"]: task for task in env.tasks(job_id)}
        for task in tasks:
            item = stored[task["task_id"]]
            assert item["status"] == "Submitted"
            assert item["annotation"] == {
                "modality": "Classification", "label": "anomaly"}
            assert item["submitted_at_iso"] == task["submitted_at_iso"]

    def test_validation_failure_is_generation_failure(self, env,
                                                      monkeypatch):
        """Req 10.6: emitted lines that fail the existing validation
        (missing DDA attributes) fail the generation — Failed, no
        manifest URI, no manifest object."""
        job_id, _ = self.seed_classification_job(env)
        monkeypatch.setattr(
            env.worker, "serialize_manifest",
            lambda annotations, job: [json.dumps(
                {"source-ref": "s3://bucket/img.png"})])

        result = env.generate(job_id)
        assert result["status"] == "Failed"

        job = env.get_job(job_id)
        assert job["status"] == "Failed"
        assert "validation" in job["failure_reason"]
        assert "output_manifest_s3_uri" not in job
        with pytest.raises(env.s3.exceptions.NoSuchKey):
            env.s3.get_object(Bucket=OUTPUT_BUCKET,
                              Key=f"labeled/{job_id}/output.manifest")


# ------------------------------------------- llm auto-label origin (16.1)

LLM_MODEL = "llm:us.amazon.nova-pro-v1:0"
LLM_AUTO_LABEL = {"enabled": True, "model": LLM_MODEL,
                  "detection_prompt": "Find every scratch and dent."}

# Entry key sets of a manifest generated without any LLM involvement
# (the existing tests above) — LLM-origin entries must carry exactly
# these and nothing more (Req 8.4).
CLASSIFICATION_ENTRY_KEYS = {"source-ref", "anomaly-label",
                             "anomaly-label-metadata"}
SEGMENTATION_ENTRY_KEYS = CLASSIFICATION_ENTRY_KEYS | {
    "anomaly-mask-ref", "anomaly-mask-ref-metadata"}
DETECTION_ENTRY_KEYS = {"source-ref", "bounding-box",
                        "bounding-box-metadata"}


def llm_prelabel(modality, label_set, width, height, wire_detections):
    """Build a Pre_Label exactly as the LLM consumer writes it: the
    model's wire-format guidance through parse_guidance and
    guidance_to_prelabel (the real production layer, not a fixture
    imitation)."""
    detections = dda_llm_guidance.parse_guidance(
        json.dumps({"detections": wire_detections}),
        label_set, width, height)
    return dda_llm_guidance.guidance_to_prelabel(
        detections, modality, label_set, width, height)


def pixels_by_class(regions, width, height):
    """Per-class foreground pixel union of RLE regions, as bytes —
    render_mask_png paints per class, so same-class regions union in
    the rendered mask while staying separate in the Pre_Label."""
    union = {}
    for region in regions:
        mask = bytes(dda_manifest.rle_decode(region["rle"], width, height))
        name = region["class"]
        if name in union:
            union[name] = bytes(a | b for a, b in zip(union[name], mask))
        else:
            union[name] = mask
    return union


def raw_manifest_lines(env, job_id):
    """The manifest exactly as stored, line by line, untransformed."""
    body = env.s3.get_object(
        Bucket=OUTPUT_BUCKET,
        Key=f"labeled/{job_id}/output.manifest")["Body"].read()
    return [line for line in body.decode().split("\n") if line.strip()]


class TestLlmSegmentationTeamJob:
    WIDTH, HEIGHT = 8, 6
    LABELS = ["scratch", "dent"]

    def prelabel(self, wire_detections):
        return llm_prelabel("Segmentation", self.LABELS,
                            self.WIDTH, self.HEIGHT, wire_detections)

    def test_llm_prelabel_submissions_render_masks(self, env):
        """Req 8.1/8.3/8.6: a Segmentation team job whose submissions
        are LLM pre-labels (image_width/image_height, no image_size)
        generates a manifest with PNG masks rendered through the
        job-wide color map — the task 8 _canonical_annotation
        normalization bridging the LLM pre-label shape into mask
        rendering. Team submissions are human-annotated 'yes'."""
        job_id = env.put_job(task_type="Segmentation",
                             label_set=self.LABELS, image_count=2,
                             auto_label=LLM_AUTO_LABEL)
        prelabels = [
            self.prelabel([
                {"class": "scratch",
                 "box": {"left": 0, "top": 0, "width": 2, "height": 2}},
                # Same class twice: separate regions in the Pre_Label,
                # union in the rendered mask.
                {"class": "scratch",
                 "box": {"left": 5, "top": 3, "width": 2, "height": 2}},
                {"class": "dent",
                 "polygon": [[2, 2], [6, 2], [2, 5]]},
            ]),
            self.prelabel([
                {"class": "dent",
                 "box": {"left": 1, "top": 1, "width": 3, "height": 2}},
            ]),
        ]
        tasks = []
        for i, prelabel in enumerate(prelabels):
            # The LLM pre-label shape: dimension fields, no image_size
            # — precisely what the task 8 normalization must bridge.
            assert "image_size" not in prelabel
            assert prelabel["image_width"] == self.WIDTH
            assert prelabel["image_height"] == self.HEIGHT
            # The labeler accepted the pre-label unchanged; the
            # submission persists through annotation_s3_key (the
            # segmentation submit path's shape).
            tasks.append(env.submitted_task(
                job_id, i, annotation=prelabel, inline=False,
                prelabel_status="Available"))

        result = env.generate(job_id)
        assert result["status"] == "Completed"
        assert result["entry_count"] == 2

        color_map = dda_manifest.build_color_map(self.LABELS)
        entries = env.manifest_entries(job_id)
        by_ref = {entry["source-ref"]: entry for entry in entries}
        for i, task in enumerate(tasks):
            entry = by_ref[task["image_s3_uri"]]
            # Structure identical to a non-LLM segmentation entry, no
            # LLM-specific attributes (Req 8.4).
            assert set(entry) == SEGMENTATION_ENTRY_KEYS
            assert entry["anomaly-label"] == 1
            assert (entry["anomaly-label-metadata"]["human-annotated"]
                    == "yes")
            mask_metadata = entry["anomaly-mask-ref-metadata"]
            assert mask_metadata["internal-color-map"] == color_map
            assert mask_metadata["human-annotated"] == "yes"
            mask_uri = entry["anomaly-mask-ref"]
            assert mask_uri.startswith(
                f"s3://{OUTPUT_BUCKET}/labeled/{job_id}/masks/")

            # Decode the rendered PNG back through the job color map:
            # pixel-identical to the LLM pre-label's regions (Req 8.3).
            mask_key = mask_uri[len(f"s3://{OUTPUT_BUCKET}/"):]
            png = env.s3.get_object(Bucket=OUTPUT_BUCKET,
                                    Key=mask_key)["Body"].read()
            decoded_regions, width, height = dda_manifest.decode_mask_png(
                png, color_map)
            assert (width, height) == (self.WIDTH, self.HEIGHT)
            assert pixels_by_class(decoded_regions, width, height) == \
                pixels_by_class(prelabels[i]["regions"], width, height)

        # The whole manifest passes the validation gate untransformed
        # (Req 8.2).
        assert env.worker._validate_manifest_lines(
            raw_manifest_lines(env, job_id), "Segmentation") == []

    def test_llm_skip_verification_accepted_prelabels(self, env):
        """Req 8.1/8.3/8.5: a Segmentation skip-verification job whose
        accepted results are LLM pre-labels generates the manifest
        through the pre-label -> _canonical_annotation ->
        render_mask_png path; exactly the accepted images are included
        (rejected and failed excluded), human-annotated 'no'."""
        job_id = env.put_job(task_type="Segmentation",
                             label_set=self.LABELS, image_count=3,
                             skip_verification=True, team_id=None,
                             auto_label=LLM_AUTO_LABEL)
        accepted_prelabel = self.prelabel([
            {"class": "dent",
             "box": {"left": 2, "top": 1, "width": 4, "height": 3}},
        ])
        accepted = env.auto_task(job_id, 0, decision="accepted",
                                 prelabel=accepted_prelabel)
        rejected = env.auto_task(job_id, 1, decision="rejected",
                                 prelabel=self.prelabel([
                                     {"class": "scratch",
                                      "box": {"left": 0, "top": 0,
                                              "width": 1, "height": 1}},
                                 ]))
        failed = env.auto_task(job_id, 2, failed=True)

        result = env.generate(job_id)
        assert result["status"] == "Completed"
        assert result["entry_count"] == 1

        entries = env.manifest_entries(job_id)
        assert {entry["source-ref"] for entry in entries} == {
            accepted["image_s3_uri"]}
        assert rejected["image_s3_uri"] not in {
            entry["source-ref"] for entry in entries}
        assert failed["image_s3_uri"] not in {
            entry["source-ref"] for entry in entries}

        entry = entries[0]
        assert set(entry) == SEGMENTATION_ENTRY_KEYS
        assert entry["anomaly-label-metadata"]["human-annotated"] == "no"
        assert (entry["anomaly-mask-ref-metadata"]["human-annotated"]
                == "no")

        color_map = dda_manifest.build_color_map(self.LABELS)
        mask_key = entry["anomaly-mask-ref"][len(f"s3://{OUTPUT_BUCKET}/"):]
        png = env.s3.get_object(Bucket=OUTPUT_BUCKET,
                                Key=mask_key)["Body"].read()
        decoded_regions, width, height = dda_manifest.decode_mask_png(
            png, color_map)
        assert (width, height) == (self.WIDTH, self.HEIGHT)
        assert pixels_by_class(decoded_regions, width, height) == \
            pixels_by_class(accepted_prelabel["regions"], width, height)

        assert env.worker._validate_manifest_lines(
            raw_manifest_lines(env, job_id), "Segmentation") == []


class TestLlmObjectDetectionAndClassification:
    def test_object_detection_manifest_passes_validation(self, env):
        """Req 8.2/8.4: an ObjectDetection LLM job's manifest passes
        _validate_manifest_lines untransformed, with the GT
        bounding-box structure (a polygon detection collapsed to its
        axis-aligned hull) and no LLM-specific attributes."""
        label_set = ["scratch", "dent"]
        job_id = env.put_job(task_type="ObjectDetection",
                             label_set=label_set, image_count=1,
                             auto_label=LLM_AUTO_LABEL)
        prelabel = llm_prelabel("ObjectDetection", label_set, 100, 80, [
            {"class": "scratch",
             "box": {"left": 10, "top": 20, "width": 30, "height": 40}},
            {"class": "dent",
             "polygon": [[0, 0], [5, 0], [5, 5], [0, 5]]},
        ])
        # The LLM pre-label shape: dimension fields, no image_size.
        assert "image_size" not in prelabel
        # Guidance coordinates are floats; the submission persists
        # through S3 exactly as the pre-label was written.
        task = env.submitted_task(job_id, 0, annotation=prelabel,
                                  inline=False,
                                  prelabel_status="Available")

        result = env.generate(job_id)
        assert result["status"] == "Completed"

        assert env.worker._validate_manifest_lines(
            raw_manifest_lines(env, job_id), "ObjectDetection") == []

        entries = env.manifest_entries(job_id)
        assert len(entries) == 1
        entry = entries[0]
        assert set(entry) == DETECTION_ENTRY_KEYS
        assert entry["source-ref"] == task["image_s3_uri"]
        assert entry["bounding-box"] == {
            "image_size": [{"width": 100, "height": 80, "depth": 3}],
            "annotations": [
                {"class_id": 0, "left": 10, "top": 20,
                 "width": 30, "height": 40},
                # The polygon detection, collapsed to its hull.
                {"class_id": 1, "left": 0, "top": 0,
                 "width": 5, "height": 5},
            ],
        }
        metadata = entry["bounding-box-metadata"]
        assert metadata["class-map"] == {"0": "scratch", "1": "dent"}
        assert metadata["human-annotated"] == "yes"

    def test_classification_manifest_passes_validation(self, env):
        """Req 8.2/8.4: a Classification LLM job's manifest passes
        _validate_manifest_lines untransformed — a detection-bearing
        result maps to anomaly (1), a valid empty result to normal
        (0) — with no LLM-specific attributes."""
        job_id = env.put_job(image_count=2, auto_label=LLM_AUTO_LABEL)
        labels = ["normal", "anomaly"]
        prelabels = [
            llm_prelabel("Classification", labels, 64, 48, [
                {"class": "anomaly",
                 "box": {"left": 4, "top": 4, "width": 8, "height": 8}},
            ]),
            llm_prelabel("Classification", labels, 64, 48, []),
        ]
        assert [p["label"] for p in prelabels] == ["anomaly", "normal"]
        tasks = [env.submitted_task(job_id, i, annotation=prelabel,
                                    prelabel_status="Available")
                 for i, prelabel in enumerate(prelabels)]

        result = env.generate(job_id)
        assert result["status"] == "Completed"
        assert result["entry_count"] == 2

        assert env.worker._validate_manifest_lines(
            raw_manifest_lines(env, job_id), "Classification") == []

        entries = env.manifest_entries(job_id)
        by_ref = {entry["source-ref"]: entry for entry in entries}
        assert by_ref[tasks[0]["image_s3_uri"]]["anomaly-label"] == 1
        assert by_ref[tasks[1]["image_s3_uri"]]["anomaly-label"] == 0
        for entry in entries:
            assert set(entry) == CLASSIFICATION_ENTRY_KEYS
            metadata = entry["anomaly-label-metadata"]
            assert metadata["human-annotated"] == "yes"
            assert metadata["creation-date"]


class TestLlmAllPrelabelsFailed:
    def test_from_scratch_submissions_all_emitted(self, env):
        """Req 8.6: a team job where every LLM pre-label failed and
        every task was labeled from scratch still produces one manifest
        entry per submission, human-annotated 'yes'."""
        job_id = env.put_job(image_count=3, auto_label=LLM_AUTO_LABEL)
        labels = ["anomaly", "normal", "anomaly"]
        tasks = [
            env.submitted_task(
                job_id, i,
                annotation={"modality": "Classification",
                            "label": labels[i]},
                prelabel_status="Failed",
                prelabel_error="model error: simulated model failure")
            for i in range(3)
        ]

        result = env.generate(job_id)
        assert result["status"] == "Completed"
        assert result["entry_count"] == 3

        entries = env.manifest_entries(job_id)
        assert len(entries) == 3
        by_ref = {entry["source-ref"]: entry for entry in entries}
        assert set(by_ref) == {task["image_s3_uri"] for task in tasks}
        for i, task in enumerate(tasks):
            entry = by_ref[task["image_s3_uri"]]
            assert entry["anomaly-label"] == (
                1 if labels[i] == "anomaly" else 0)
            assert (entry["anomaly-label-metadata"]["human-annotated"]
                    == "yes")

        assert env.worker._validate_manifest_lines(
            raw_manifest_lines(env, job_id), "Classification") == []
