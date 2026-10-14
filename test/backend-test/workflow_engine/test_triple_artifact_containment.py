# Copyright 2026 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Containment and preservation unit tests for the additive
per-inspection artifact persists (imts-triple-inspection-hmi task 1.5,
Requirement 4.4).

The two additive persists — ``_persist_original_frame`` (the
Inspection's Original_Image, written at crop time) and
``_persist_annotated_frame`` (the Inspection's Annotated_Image, written
after the Bedrock answer returns) — are best-effort in the
``pipeline_executor._persist_node_frames`` containment style. These
tests pin that containment and the additive-only surface:

- a raising ``cv2`` stub or a failing filesystem write in EITHER persist
  leaves the run status, the node outcome, and the run metadata (the
  nested ``bedrock.{nodeId}.*`` entry plus the flat
  ``is_anomalous``/``confidence`` keys) identical to the pre-change path
  — modelled by running the very same binding with both persists patched
  out;
- a binding WITHOUT ``crop_detection_index`` (the legacy whole-frame
  path every other workflow uses) produces no ``original`` and no
  ``annotated`` artifact at all, and its run artifacts stay
  byte-identical to the pre-change path.

No network: an injected fake invoker returns a scripted answer.
"""
import os

import cv2
import numpy as np
import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import output_bindings
from workflow_engine.node_status import (
    STATUS_FAILURE,
    NodeStatusCollector,
)
from workflow_engine.output_bindings import (
    ANNOTATED_FRAME_ARTIFACT_TEMPLATE,
    ORIGINAL_FRAME_ARTIFACT_TEMPLATE,
    BedrockInferenceProcessor,
    RunContext,
    sanitize_node_id_for_artifact,
)

NODE_ID = "bedrock1"
CAPTURE_ID = "cap-15"
FRAME_NAME = "containment_frame_cam.jpg"
FRAME_WIDTH, FRAME_HEIGHT = 64, 48
#: A comfortably in-bounds detection, so the crop path always succeeds
#: and the persists are the only variable of these tests.
DETECTION = {
    "id": "cc33dd44", "label": "blue plate", "confidence": 0.9,
    "x_min": 4.0, "y_min": 4.0, "x_max": 60.0, "y_max": 44.0,
}
#: An anomaly-mode answer carrying both the verdict the existing parse
#: consumes and an ``objects`` list, so the Annotated_Image is attempted.
ANSWER = (
    '{"is_anomalous": true, "confidence": 0.87, "objects": ['
    '{"name": "scratch", "qc": "NOK", "bounding_box": '
    '{"x_min": 5, "y_min": 5, "x_max": 30, "y_max": 25}},'
    '{"name": "edge", "qc": "OK", "bounding_box": '
    '{"x_min": 10, "y_min": 12, "x_max": 40, "y_max": 34}}]}'
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class RecordingInvoker:
    """Injectable Bedrock invoker returning a scripted answer."""

    def __init__(self, answer=ANSWER):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append({"images": list(images)})
        return self.answer


def make_binding(parameters):
    return {
        "nodeId": NODE_ID,
        "binding": "bedrock_inference",
        "parameters": dict(parameters),
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": {
            "in": "{work_dir}/" + FRAME_NAME, "reference": None},
    }


def make_document(parameters):
    return {
        "schemaVersion": 1,
        "executorBindings": [make_binding(parameters)],
    }


def write_frame(work_dir):
    """A real synthetic JPEG frame (gradient, so the crop decodes)."""
    frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, FRAME_WIDTH, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, FRAME_HEIGHT, dtype=np.uint8)[:, None]
    frame[:, :, 2] = 127
    path = os.path.join(work_dir, FRAME_NAME)
    assert cv2.imwrite(path, frame)
    return path


def artifact_path(work_dir, template):
    return os.path.join(work_dir, template.format(
        capture_id=CAPTURE_ID,
        safe_node_id=sanitize_node_id_for_artifact(NODE_ID),
    ))


def status_snapshot(collector):
    """The node-status map without the wall-clock ``durationMs`` field,
    so two runs of the same binding compare exactly."""
    return {
        node_id: {
            key: value for key, value in entry.items()
            if key != "durationMs"
        }
        for node_id, entry in collector.to_map().items()
    }


def file_snapshot(work_dir, skip=()):
    """``{filename: bytes}`` of the run artifact directory's files."""
    skipped = {os.path.basename(path) for path in skip}
    snapshot = {}
    for name in sorted(os.listdir(work_dir)):
        path = os.path.join(work_dir, name)
        if name in skipped or not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            snapshot[name] = handle.read()
    return snapshot


def run_binding(work_dir, parameters):
    """Run the processor over one binding in ``work_dir`` and return
    ``(metadata, status_snapshot, file_snapshot, invoker)``."""
    os.makedirs(work_dir, exist_ok=True)
    write_frame(work_dir)
    invoker = RecordingInvoker()
    collector = NodeStatusCollector(extra_node_ids=[NODE_ID])
    run_context = RunContext(
        tag_values={"detections": [DETECTION]},
        output_dir=work_dir,
        capture_id=CAPTURE_ID,
        graph_document=None,
        node_status=collector,
    )
    metadata = BedrockInferenceProcessor(invoker=invoker).process(
        make_document(parameters), {"kept": 1}, work_dir,
        run_context=run_context)
    return metadata, status_snapshot(collector), work_dir, invoker


def run_pre_change_baseline(monkeypatch, work_dir, parameters):
    """The same run with both additive persists patched out — the
    pre-change path, exactly."""
    with monkeypatch.context() as patched:
        patched.setattr(
            BedrockInferenceProcessor, "_persist_original_frame",
            staticmethod(lambda *args, **kwargs: None))
        patched.setattr(
            BedrockInferenceProcessor, "_persist_annotated_frame",
            staticmethod(lambda *args, **kwargs: None))
        return run_binding(work_dir, parameters)


def raise_boom(*args, **kwargs):
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Containment: a failing persist changes nothing about the run
# ---------------------------------------------------------------------------

class TestPersistFailureContainment:
    """Requirement 4.4: both persists are best-effort — a raising cv2 or
    a failing write leaves the run status, the node outcome, and the
    metadata identical to the pre-change path."""

    @pytest.fixture()
    def baseline(self, tmp_path, monkeypatch):
        metadata, statuses, _dir, invoker = run_pre_change_baseline(
            monkeypatch, str(tmp_path / "baseline"),
            {"crop_detection_index": 0})
        # The pre-change path writes neither additive artifact.
        assert not os.path.exists(
            artifact_path(str(tmp_path / "baseline"),
                          ORIGINAL_FRAME_ARTIFACT_TEMPLATE))
        assert not os.path.exists(
            artifact_path(str(tmp_path / "baseline"),
                          ANNOTATED_FRAME_ARTIFACT_TEMPLATE))
        assert len(invoker.calls) == 1
        return metadata, statuses

    def assert_matches_baseline(self, baseline, metadata, statuses):
        expected_metadata, expected_statuses = baseline
        assert metadata == expected_metadata, (
            "a failing additive persist changed the run metadata")
        assert statuses == expected_statuses, (
            "a failing additive persist changed the node outcome")
        # Spelled out, so a future metadata-shape regression is obvious.
        assert metadata["is_anomalous"] is True
        assert metadata["confidence"] == 0.87
        assert metadata["kept"] == 1
        entry = metadata["bedrock"][NODE_ID]
        assert entry["is_anomalous"] is True
        assert entry["confidence"] == 0.87
        assert entry["text"] == ANSWER
        assert entry["detection_id"] == DETECTION["id"]
        assert "error" not in entry
        assert statuses[NODE_ID]["status"] != STATUS_FAILURE

    def test_original_persist_write_failure_is_contained(
        self, tmp_path, baseline
    ):
        # A directory where the Original_Image belongs: the write fails
        # inside the persist, which must swallow it.
        work_dir = str(tmp_path / "original-write")
        os.makedirs(work_dir)
        os.makedirs(artifact_path(work_dir, ORIGINAL_FRAME_ARTIFACT_TEMPLATE))

        metadata, statuses, _dir, invoker = run_binding(
            work_dir, {"crop_detection_index": 0})

        assert len(invoker.calls) == 1  # the inspection still happened
        self.assert_matches_baseline(baseline, metadata, statuses)
        # The write really did fail (nothing was written at that path)...
        assert not os.path.isfile(
            artifact_path(work_dir, ORIGINAL_FRAME_ARTIFACT_TEMPLATE))
        # ...and the sibling additive persist is unaffected by it.
        assert os.path.isfile(
            artifact_path(work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE))

    def test_annotated_persist_cv2_failure_is_contained(
        self, tmp_path, monkeypatch, baseline
    ):
        # A raising cv2 stub on the only cv2 entry point the annotated
        # persist uses (the crop path itself reads/encodes, never decodes).
        monkeypatch.setattr(cv2, "imdecode", raise_boom)

        metadata, statuses, _dir, invoker = run_binding(
            str(tmp_path / "annotated-cv2"), {"crop_detection_index": 0})

        assert len(invoker.calls) == 1
        self.assert_matches_baseline(baseline, metadata, statuses)
        assert not os.path.exists(artifact_path(
            str(tmp_path / "annotated-cv2"),
            ANNOTATED_FRAME_ARTIFACT_TEMPLATE))
        # The Original_Image, written before the failure, still landed.
        assert os.path.exists(artifact_path(
            str(tmp_path / "annotated-cv2"),
            ORIGINAL_FRAME_ARTIFACT_TEMPLATE))

    def test_annotated_persist_draw_failure_is_contained(
        self, tmp_path, monkeypatch, baseline
    ):
        monkeypatch.setattr(
            output_bindings, "draw_defect_objects", raise_boom)

        metadata, statuses, _dir, invoker = run_binding(
            str(tmp_path / "annotated-draw"), {"crop_detection_index": 0})

        assert len(invoker.calls) == 1
        self.assert_matches_baseline(baseline, metadata, statuses)
        assert not os.path.exists(artifact_path(
            str(tmp_path / "annotated-draw"),
            ANNOTATED_FRAME_ARTIFACT_TEMPLATE))

    def test_annotated_persist_write_failure_is_contained(
        self, tmp_path, baseline
    ):
        work_dir = str(tmp_path / "annotated-write")
        os.makedirs(work_dir)
        os.makedirs(artifact_path(work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE))

        metadata, statuses, _dir, invoker = run_binding(
            work_dir, {"crop_detection_index": 0})

        assert len(invoker.calls) == 1
        self.assert_matches_baseline(baseline, metadata, statuses)
        assert not os.path.isfile(
            artifact_path(work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE))
        # The Original_Image, written before the failing sibling, landed.
        assert os.path.isfile(
            artifact_path(work_dir, ORIGINAL_FRAME_ARTIFACT_TEMPLATE))

    def test_both_persists_failing_together_is_contained(
        self, tmp_path, monkeypatch, baseline
    ):
        work_dir = str(tmp_path / "both")
        os.makedirs(work_dir)
        os.makedirs(artifact_path(work_dir, ORIGINAL_FRAME_ARTIFACT_TEMPLATE))
        monkeypatch.setattr(
            output_bindings, "draw_defect_objects", raise_boom)

        metadata, statuses, _dir, invoker = run_binding(
            work_dir, {"crop_detection_index": 0})

        assert len(invoker.calls) == 1
        self.assert_matches_baseline(baseline, metadata, statuses)
        assert not os.path.exists(
            artifact_path(work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE))


# ---------------------------------------------------------------------------
# Preservation: the whole-frame path gains no artifacts at all
# ---------------------------------------------------------------------------

class TestWholeFramePathGainsNoArtifacts:
    """Requirement 4.4: the additive artifacts exist only for bindings
    that inspect a Detection_Crop, so runs of every other workflow stay
    byte-identical."""

    def test_binding_without_crop_index_writes_no_additive_artifact(
        self, tmp_path
    ):
        work_dir = str(tmp_path / "whole-frame")

        metadata, statuses, _dir, invoker = run_binding(work_dir, {})

        assert len(invoker.calls) == 1
        assert not os.path.exists(
            artifact_path(work_dir, ORIGINAL_FRAME_ARTIFACT_TEMPLATE))
        assert not os.path.exists(
            artifact_path(work_dir, ANNOTATED_FRAME_ARTIFACT_TEMPLATE))
        # No node-frame artifact of ANY additive port appeared, and the
        # whole-frame path records no detection attribution.
        assert [
            name for name in os.listdir(work_dir)
            if name.endswith((".original.jpg", ".annotated.jpg"))
        ] == []
        assert "detection_id" not in metadata["bedrock"][NODE_ID]
        assert statuses[NODE_ID]["status"] != STATUS_FAILURE

    def test_whole_frame_run_is_byte_identical_to_the_pre_change_path(
        self, tmp_path, monkeypatch
    ):
        baseline_dir = str(tmp_path / "pre-change")
        actual_dir = str(tmp_path / "post-change")

        expected_metadata, expected_statuses, _b, _bi = \
            run_pre_change_baseline(monkeypatch, baseline_dir, {})
        metadata, statuses, _a, _ai = run_binding(actual_dir, {})

        assert metadata == expected_metadata
        assert statuses == expected_statuses
        assert file_snapshot(actual_dir) == file_snapshot(baseline_dir), (
            "the whole-frame path's run artifacts are no longer "
            "byte-identical to the pre-change path")
