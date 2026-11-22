"""
dda_labeling_worker.py manifest generation for the `InstanceSegmentation`
modality (rfdetr-seg-support, task 2.4; Requirements 2.1, 2.5, 3.1).

Feature: rfdetr-seg-support

Covers, against the moto-backed stack from conftest.py (real
shared_utils + dda_manifest) and the ManifestEnv harness of the
generate-manifest suite:

- An InstanceSegmentation team job generates an Instance_Manifest: one
  JSON Lines object per submitted task carrying `source-ref`,
  `instance-segmentation` (image_size + per-instance class_id, the box
  derived from the RLE, the RLE itself) and
  `instance-segmentation-metadata` (objects / class-map / type /
  human-annotated / creation-date / job-name); the lines pass
  `_validate_manifest_lines(lines, 'InstanceSegmentation')`; no PNG mask
  is rendered and the job record gains neither `mask_output_prefix` nor
  `color_map`; `output_manifest_s3_uri` is recorded and the job completes
- `_validate_manifest_lines` reports a line missing
  `instance-segmentation-metadata` and a line whose pair is not objects
- The `human_annotated` / `confidence` markers apply as to Segmentation
  (Requirement 2.5)
- A stored annotation with a corrupt RLE fails the whole generation (no
  partial manifest), the existing failure semantics
"""
import json

import pytest

import dda_manifest
from test_dda_labeling_worker_generate_manifest import (  # noqa: F401
    OUTPUT_BUCKET,
    ManifestEnv,
    worker,
)

MODALITY = "InstanceSegmentation"
LABELS = ["scratch", "dent"]
WIDTH, HEIGHT = 8, 6


def rle_of(pixels, width=WIDTH, height=HEIGHT):
    mask = [0] * (width * height)
    for x, y in pixels:
        mask[y * width + x] = 1
    return dda_manifest.rle_encode(mask, width, height)


def rect(left, top, box_w, box_h):
    return {(x, y) for x in range(left, left + box_w)
            for y in range(top, top + box_h)}


RECT_A = (1, 1, 3, 2)
RECT_B = (5, 3, 2, 3)
RLE_A = rle_of(rect(*RECT_A))
RLE_B = rle_of(rect(*RECT_B))


def submitted_annotation(instances):
    """The shape submit_labeler_task stores (boxes recomputed,
    classification derived)."""
    return {
        "modality": MODALITY,
        "image_size": {"width": WIDTH, "height": HEIGHT},
        "instances": instances,
        "classification": "anomaly" if instances else "normal",
    }


def box_dict(left, top, box_w, box_h):
    return {"left": left, "top": top, "width": box_w, "height": box_h}


@pytest.fixture
def env(aws_stack, worker):
    return ManifestEnv(aws_stack, worker)


class TestInstanceSegmentationTeamJob:
    def test_end_to_end(self, env):
        """Req 3.1 / 2.5: the Instance_Manifest is generated from the
        S3-stored annotations, validated by the worker's own gate, and
        recorded on the job; nothing Segmentation-only is written."""
        job_id = env.put_job(task_type=MODALITY, label_set=LABELS,
                             image_count=3)
        env.submitted_task(job_id, 0, submitted_annotation([
            {"class": "scratch", "rle": RLE_A, "box": box_dict(*RECT_A)},
            {"class": "dent", "rle": RLE_B, "box": box_dict(*RECT_B)},
        ]), inline=False)
        env.submitted_task(job_id, 1, submitted_annotation([]), inline=False)
        # An unsubmitted task is excluded (the labeling spec's Req 10.2).
        env.put_task(job_id, 2)

        result = env.generate(job_id)
        assert result["status"] == "Completed", result

        entries = env.manifest_entries(job_id)
        assert len(entries) == 2
        first, second = entries
        assert set(first) == {"source-ref", "instance-segmentation",
                              "instance-segmentation-metadata"}
        assert first["source-ref"].startswith("s3://")
        assert first["instance-segmentation"] == {
            "image_size": [{"width": WIDTH, "height": HEIGHT, "depth": 3}],
            "annotations": [
                {"class_id": 0, "rle": RLE_A, **box_dict(*RECT_A)},
                {"class_id": 1, "rle": RLE_B, **box_dict(*RECT_B)},
            ],
        }
        metadata = first["instance-segmentation-metadata"]
        assert metadata == {
            "objects": [{"confidence": 1.0}, {"confidence": 1.0}],
            "class-map": {"0": "scratch", "1": "dent"},
            "type": "groundtruth/instance-segmentation",
            "human-annotated": "yes",
            "creation-date": "2023-11-14T22:13:20Z",
            "job-name": env.get_job(job_id)["job_name"],
        }
        # No mask reference, no PNG — the instances carry their geometry.
        for entry in entries:
            assert "anomaly-mask-ref" not in entry
            assert "anomaly-label" not in entry
        assert second["instance-segmentation"]["annotations"] == []
        assert second["instance-segmentation-metadata"]["objects"] == []

        # The worker's own validation gate accepts what it generated.
        lines = [json.dumps(entry) for entry in entries]
        assert env.worker._validate_manifest_lines(lines, MODALITY) == []

        # Round trip through the shared parser.
        records = dda_manifest.parse_manifest(lines, MODALITY)
        assert records[0]["annotation"]["instances"] == [
            {"class": "scratch", "rle": RLE_A, "box": box_dict(*RECT_A)},
            {"class": "dent", "rle": RLE_B, "box": box_dict(*RECT_B)},
        ]
        assert records[0]["annotation"]["classification"] == "anomaly"
        assert records[1]["annotation"]["classification"] == "normal"

        job = env.get_job(job_id)
        assert job["status"] == "Completed"
        assert job["output_manifest_s3_uri"] == (
            f"s3://{OUTPUT_BUCKET}/labeled/{job_id}/output.manifest")
        assert "mask_output_prefix" not in job
        assert "color_map" not in job
        listed = env.s3.list_objects_v2(Bucket=OUTPUT_BUCKET,
                                        Prefix=f"labeled/{job_id}/")
        assert [obj["Key"] for obj in listed.get("Contents", [])] == [
            f"labeled/{job_id}/output.manifest"]

    def test_manifest_box_is_rederived_from_the_rle(self, env):
        """DD9: the manifest's box comes from the RLE, so a stored box that
        drifted from its mask (still inside the image) is corrected on
        export rather than written through."""
        job_id = env.put_job(task_type=MODALITY, label_set=LABELS,
                             image_count=1)
        env.submitted_task(job_id, 0, submitted_annotation([
            {"class": "dent", "rle": RLE_B, "box": box_dict(0, 0, 1, 1)},
        ]), inline=False)
        assert env.generate(job_id)["status"] == "Completed"
        [entry] = env.manifest_entries(job_id)
        annotation = entry["instance-segmentation"]["annotations"][0]
        assert {k: annotation[k] for k in ("left", "top", "width",
                                           "height")} == box_dict(*RECT_B)

    def test_corrupt_rle_fails_the_whole_generation(self, env):
        """The labeling spec's Req 10.9 semantics hold: a stored RLE that
        does not match image_size fails generation with no manifest URI."""
        job_id = env.put_job(task_type=MODALITY, label_set=LABELS,
                             image_count=2)
        env.submitted_task(job_id, 0, submitted_annotation([
            {"class": "scratch", "rle": RLE_A, "box": box_dict(*RECT_A)},
        ]), inline=False)
        env.submitted_task(job_id, 1, submitted_annotation([
            {"class": "dent", "rle": "1 2 3", "box": box_dict(0, 0, 1, 1)},
        ]), inline=False)
        result = env.generate(job_id)
        assert result["status"] == "Failed"
        job = env.get_job(job_id)
        assert job["status"] == "Failed"
        assert "output_manifest_s3_uri" not in job
        assert "RLE counts sum" in job["failure_reason"]
        listed = env.s3.list_objects_v2(Bucket=OUTPUT_BUCKET,
                                        Prefix=f"labeled/{job_id}/")
        assert listed.get("KeyCount", 0) == 0


class TestValidateManifestLines:
    def _line(self, **overrides):
        entry = {
            "source-ref": "s3://bucket/img.jpg",
            "instance-segmentation": {
                "image_size": [{"width": WIDTH, "height": HEIGHT,
                                "depth": 3}],
                "annotations": [{"class_id": 0, "rle": RLE_A,
                                 **box_dict(*RECT_A)}],
            },
            "instance-segmentation-metadata": {
                "objects": [{"confidence": 1.0}],
                "class-map": {"0": "scratch", "1": "dent"},
                "type": "groundtruth/instance-segmentation",
                "human-annotated": "yes",
                "creation-date": "2023-11-14T22:13:20Z",
                "job-name": "job",
            },
        }
        entry.update(overrides)
        return json.dumps({k: v for k, v in entry.items() if v is not None})

    def test_valid_line_passes(self, worker):
        assert worker._validate_manifest_lines([self._line()], MODALITY) == []

    def test_missing_metadata_reported(self, worker):
        """Req 3.1: the required-attribute scan names the missing
        instance-segmentation-metadata attribute."""
        issues = worker._validate_manifest_lines(
            [self._line(**{"instance-segmentation-metadata": None})],
            MODALITY)
        assert issues == ["entry 1: missing required attributes "
                          "instance-segmentation-metadata"]

    def test_non_object_pair_reported(self, worker):
        issues = worker._validate_manifest_lines(
            [self._line(**{"instance-segmentation": "not-an-object",
                           "instance-segmentation-metadata": 7})],
            MODALITY)
        assert issues == [
            "entry 1: instance-segmentation must be an object",
            "entry 1: instance-segmentation-metadata must be an object",
        ]

    def test_bounding_box_line_rejected_for_instance_job(self, worker):
        """The final gate requires the instance pair to be the detected
        attributes (a bounding-box manifest is not an Instance_Manifest)."""
        line = json.dumps({
            "source-ref": "s3://bucket/img.jpg",
            "instance-segmentation": {},
            "instance-segmentation-metadata": {},
            "bounding-box": {},
            "bounding-box-metadata": {},
        })
        issues = worker._validate_manifest_lines([line], MODALITY)
        # Only the gate can speak here: the pair is present and objects,
        # but detect_ground_truth_attributes picks the first foreign
        # -metadata key — the instance pair, listed first, is detected.
        assert issues == []
        reordered = json.dumps({
            "source-ref": "s3://bucket/img.jpg",
            "bounding-box": {},
            "bounding-box-metadata": {},
            "instance-segmentation": {},
            "instance-segmentation-metadata": {},
        })
        issues = worker._validate_manifest_lines([reordered], MODALITY)
        assert issues == ["instance-segmentation attributes were not "
                          "detected in the manifest"]

    def test_vocabulary_tables(self, worker):
        assert worker.REQUIRED_MANIFEST_ATTRIBUTES[MODALITY] == (
            "source-ref", "instance-segmentation",
            "instance-segmentation-metadata")
        assert worker.GT_TASK_TYPES[MODALITY] == "instance-segmentation"
        # The three existing rows are unchanged (Req 17.1).
        assert worker.REQUIRED_MANIFEST_ATTRIBUTES["Segmentation"] == (
            "source-ref", "anomaly-label", "anomaly-label-metadata",
            "anomaly-mask-ref", "anomaly-mask-ref-metadata")
        assert worker.GT_TASK_TYPES["ObjectDetection"] == "object-detection"
