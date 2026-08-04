# Copyright 2025 Amazon Web Services, Inc.
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
"""Bug-condition exploration tests: folder-source image consumption
(folder-source-image-consumption, Property 1: Bug Condition).

Property 1: Bug Condition — Folder Source Frames Are Consumed After the
Run.

**These tests assert the FIXED (post-fix) executor behavior, so they are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure is the counterexample
confirming the bug: ``workflow_engine/pipeline_executor.py`` resolves a
directory ``filesrc`` location to the oldest JPEG (``_oldest_image_in_folder``)
and Pillow-stages a ``.dda_decoded.png`` on pngdec (JP6) chains
(``_stage_decoded_png``), but has NO post-run consumption path at all —
no equivalent of the legacy ``_cleanup_file_after_processing`` (delete on
pipeline success) or ``_move_bad_folder_image_source`` (relocate to
``{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`` on failure). The folder
never drains: every run re-selects the same oldest image, staged PNGs
accumulate, and a corrupt image wedges the workflow forever.

Expected counterexamples on the UNFIXED tree:
    - the resolved oldest JPEG is still in the folder after a COMPLETED
      run, and run 2 re-resolves the exact same file;
    - the staged ``<file>.jpg.dda_decoded.png`` is left in the folder;
    - an ``OutputBindingError`` run leaves the frame unconsumed;
    - a corrupt oldest image stays in place instead of moving to
      ``failed/``;
    - a pipeline failure leaves the resolved JPEG in place instead of
      relocating it to ``failed/``.

The SAME tests are re-run in task 3.2 against the fixed executor (record
resolved Folder_Frames, consume on pipeline success, relocate on failure),
where they must PASS.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4** (expected behavior
2.1, 2.2, 2.3, 2.4, 2.5)
"""
import os
import shutil
import tempfile
import time
import uuid
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from utils import constants
from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.output_bindings import OutputBindingError
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

WORKFLOW_ID = "wf-1"
REGISTRATION_ID = "wf-1:3"

#: A fixed past base for the folder's mtimes so "oldest by mtime" is
#: deterministic and never collides with the files' creation times.
_MTIME_BASE = time.time() - 10_000_000


# ---------------------------------------------------------------------------
# Harness (mirrors test_workflow_pipeline_executor.py /
# output_bindings_fixes/executor_harness.py)
# ---------------------------------------------------------------------------


class FakePipelineManager:
    """Mocked GstPipelineManager: records launch strings, returns tags,
    or raises ``error`` to simulate a pipeline-run failure."""

    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        self.calls.append(pipeline_str)
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests; record scan calls instead."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


@pytest.fixture(autouse=True)
def capture_root(tmp_path):
    """A tmp-dir capture root so run.log / per-run artifact makedirs never
    touch the real /aws_dda tree."""
    root = os.path.join(str(tmp_path), "captures")
    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", root):
        yield root


@pytest.fixture(autouse=True)
def inference_results_root(tmp_path):
    """A tmp-dir ``constants.INFERENCE_RESULTS_DIR`` so the fix's
    ``failed/`` relocation target lands inside the test sandbox."""
    root = os.path.join(str(tmp_path), "inference-results")
    with patch.object(constants, "INFERENCE_RESULTS_DIR", root):
        yield root


def failed_dir(inference_results_root):
    """The legacy relocation target: {INFERENCE_RESULTS_DIR}/{wf}/failed/."""
    return os.path.join(inference_results_root, WORKFLOW_ID, "failed")


def make_folder_doc(folder, decoder="jpegdec"):
    """A compiled document whose ``filesrc`` location is a directory —
    the bug condition (``isBugCondition``). ``decoder='pngdec'`` compiles
    the JP6 staged-PNG chain (``_stage_frame_sources`` Pillow-stages a
    ``.dda_decoded.png``); ``jpegdec`` is the JP5/x86 direct-read chain."""
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "folder_source_1", "factory": "filesrc",
                     "args": {"location": str(folder)}},
                    {"nodeId": None, "factory": decoder, "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def seed_registration(session_factory, artifact_path):
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=REGISTRATION_ID,
            workflow_id=WORKFLOW_ID,
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(artifact_path),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.commit()
    finally:
        session.close()


def seed_execution(session_factory, execution_id):
    session = session_factory()
    try:
        session.add(WorkflowExecution(
            id=execution_id,
            registration_id=REGISTRATION_ID,
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
        ))
        session.commit()
    finally:
        session.close()
    return execution_id


def get_execution(session_factory, execution_id):
    session = session_factory()
    try:
        return session.get(WorkflowExecution, execution_id)
    finally:
        session.close()


def write_fake_jpeg(path, mtime_offset):
    """A fake JPEG (magic bytes only) for chains that never decode it."""
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0 fake jpeg bytes for " + os.fsencode(path))
    mtime = _MTIME_BASE + mtime_offset
    os.utime(path, (mtime, mtime))
    return path


def write_real_jpeg(path, mtime_offset):
    """A real tiny JPEG (Pillow) for the pngdec chain, which actually
    decodes the file during ``_stage_decoded_png``."""
    from PIL import Image

    Image.new("RGB", (4, 4), (200, 30, 30)).save(path, "JPEG")
    mtime = _MTIME_BASE + mtime_offset
    os.utime(path, (mtime, mtime))
    return path


def write_corrupt_jpeg(path, mtime_offset):
    """Garbage bytes behind a .jpg name: fails Pillow decoding."""
    with open(path, "wb") as f:
        f.write(b"this is not a jpeg at all \x00\x01\x02")
    mtime = _MTIME_BASE + mtime_offset
    os.utime(path, (mtime, mtime))
    return path


def run_execution(session_factory, execution_id, manager, post_run_handler=None):
    WorkflowExecutor(
        session_factory=session_factory,
        pipeline_manager_factory=lambda: manager,
        post_run_handler=post_run_handler,
    ).execute(execution_id)
    return get_execution(session_factory, execution_id)


# ---------------------------------------------------------------------------
# Case 1: the folder drains on successful runs (Requirement 1.1 / 2.1)
# ---------------------------------------------------------------------------


@st.composite
def mtime_spreads(draw):
    """2-4 strictly increasing mtime offsets (a folder of JPEGs at
    distinct mtimes, oldest first)."""
    count = draw(st.integers(min_value=2, max_value=4))
    gaps = draw(st.lists(
        st.integers(min_value=1, max_value=3600),
        min_size=count, max_size=count,
    ))
    offsets, acc = [], 0
    for gap in gaps:
        acc += gap
        offsets.append(acc)
    return offsets


class TestFolderDrainsOnSuccess:
    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(offsets=mtime_spreads())
    def test_successful_run_consumes_the_oldest_jpeg(self, offsets):
        """After a COMPLETED run the resolved oldest JPEG must be deleted,
        so the next run resolves the next-oldest image.

        EXPECTED FAILURE on the unfixed tree: no consumption path exists —
        the oldest JPEG survives the run and run 2 re-resolves the exact
        same file (the device evidence: every vlm-smoketest run processed
        ``zidane.jpg``).

        Validates: Requirements 1.1 (expected behavior 2.1)
        """
        folder = tempfile.mkdtemp(prefix="folder_source_")
        try:
            files = [
                write_fake_jpeg(
                    os.path.join(folder, "img_{0:03d}.jpg".format(i)), offset)
                for i, offset in enumerate(offsets)
            ]
            oldest, second_oldest = files[0], files[1]

            session_factory = make_session_factory()
            artifact_path = write_artifact_set(
                tempfile.mkdtemp(prefix="artifacts_"),
                compiled=make_folder_doc(folder))
            seed_registration(session_factory, artifact_path)

            exec_1 = seed_execution(
                session_factory, "exec-{0}".format(uuid.uuid4().hex[:8]))
            manager_1 = FakePipelineManager(tag_values={"is_anomalous": False})
            row_1 = run_execution(session_factory, exec_1, manager_1)

            # Sanity (holds on unfixed code too): the run completed and
            # resolved the oldest image by mtime.
            assert row_1.status == EXECUTION_STATUS_COMPLETED, row_1.error
            assert manager_1.calls and oldest in manager_1.calls[0], (
                "run 1 did not resolve the oldest JPEG {0!r}; launch: "
                "{1!r}".format(oldest, manager_1.calls))

            exec_2 = seed_execution(
                session_factory, "exec-{0}".format(uuid.uuid4().hex[:8]))
            manager_2 = FakePipelineManager(tag_values={"is_anomalous": False})
            row_2 = run_execution(session_factory, exec_2, manager_2)
            assert row_2.status == EXECUTION_STATUS_COMPLETED, row_2.error

            assert not os.path.exists(oldest), (
                "COUNTEREXAMPLE (Req 1.1): the resolved oldest JPEG {0!r} "
                "is still in the folder after a COMPLETED run — nothing "
                "consumed it, so run 2 re-resolved the same file (run 2 "
                "launch: {1!r}); the folder never drains".format(
                    oldest, manager_2.calls))
            assert manager_2.calls and second_oldest in manager_2.calls[0], (
                "COUNTEREXAMPLE (Req 1.1): run 2 should resolve the "
                "next-oldest JPEG {0!r} but launched {1!r}".format(
                    second_oldest, manager_2.calls))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case 2: staged .dda_decoded.png cleanup on the JP6 pngdec chain
# (Requirement 1.2 / 2.2)
# ---------------------------------------------------------------------------


class TestStagedPngCleanup:
    def test_staged_png_is_deleted_with_the_original(
        self, tmp_path, session_factory
    ):
        """On a pngdec (JP6) chain a successful run must delete BOTH the
        staged ``<file>.jpg.dda_decoded.png`` and the original JPEG.

        EXPECTED FAILURE on the unfixed tree: the staged PNG (and the
        original) survive the run — staging files accumulate in the source
        folder (the device evidence: ``zidane.jpg.dda_decoded.png``
        rewritten on every run).

        Validates: Requirements 1.2 (expected behavior 2.1, 2.2)
        """
        folder = tmp_path / "images"
        folder.mkdir()
        oldest = write_real_jpeg(str(folder / "a.jpg"), 0)
        write_real_jpeg(str(folder / "b.jpg"), 100)
        staged = oldest + ".dda_decoded.png"

        artifact_path = write_artifact_set(
            tmp_path / "artifacts",
            compiled=make_folder_doc(folder, decoder="pngdec"))
        seed_registration(session_factory, artifact_path)
        execution_id = seed_execution(session_factory, "exec-1")

        manager = FakePipelineManager(tag_values={"is_anomalous": False})
        row = run_execution(session_factory, execution_id, manager)

        # Sanity (holds on unfixed code too): the run completed against
        # the staged PNG.
        assert row.status == EXECUTION_STATUS_COMPLETED, row.error
        assert manager.calls and staged in manager.calls[0], (
            "the pngdec chain did not run against the staged PNG {0!r}; "
            "launch: {1!r}".format(staged, manager.calls))

        assert not os.path.exists(staged), (
            "COUNTEREXAMPLE (Req 1.2): the staged {0!r} is still in the "
            "source folder after a COMPLETED run — staging files "
            "accumulate".format(staged))
        assert not os.path.exists(oldest), (
            "COUNTEREXAMPLE (Req 1.1): the original JPEG {0!r} behind the "
            "staged PNG is still in the folder after a COMPLETED "
            "run".format(oldest))


# ---------------------------------------------------------------------------
# Case 3: consumption still happens when an output binding fails
# (Requirement 1.1 / 2.3)
# ---------------------------------------------------------------------------


class TestConsumptionDespiteOutputBindingFailure:
    def test_frame_is_consumed_when_post_run_handler_raises(
        self, tmp_path, session_factory
    ):
        """Pipeline processing succeeded; the post-run handler then raises
        ``OutputBindingError``. Legacy cleans up after pipeline processing
        regardless of downstream outcomes (cleanup lives in the ``else``
        of the pipeline try/except), so the frame must still be consumed.

        EXPECTED FAILURE on the unfixed tree: no consumption path exists
        on any outcome.

        Validates: Requirements 1.1 (expected behavior 2.3)
        """
        folder = tmp_path / "images"
        folder.mkdir()
        oldest = write_fake_jpeg(str(folder / "a.jpg"), 0)
        write_fake_jpeg(str(folder / "b.jpg"), 100)

        artifact_path = write_artifact_set(
            tmp_path / "artifacts", compiled=make_folder_doc(folder))
        seed_registration(session_factory, artifact_path)
        execution_id = seed_execution(session_factory, "exec-1")

        def failing_handler(registration, document, tag_values):
            raise OutputBindingError(
                ["mqtt_publish_1"], "output binding failed: broker down")

        manager = FakePipelineManager(tag_values={"is_anomalous": False})
        row = run_execution(
            session_factory, execution_id, manager,
            post_run_handler=failing_handler)

        # Sanity (holds on unfixed code too): the binding failure decides
        # the terminal status; the pipeline itself succeeded.
        assert row.status == EXECUTION_STATUS_FAILED
        assert "output binding failed" in (row.error or "")
        assert manager.calls and oldest in manager.calls[0]

        assert not os.path.exists(oldest), (
            "COUNTEREXAMPLE (Req 1.1 / expected 2.3): the folder-resolved "
            "JPEG {0!r} is still in the folder after the pipeline run "
            "succeeded — an output-binding failure must not prevent "
            "consumption (legacy cleans up in the pipeline try/except "
            "else branch, before downstream output handling)".format(oldest))


# ---------------------------------------------------------------------------
# Case 4: a corrupt image is relocated to failed/ (Requirement 1.3 / 2.4)
# ---------------------------------------------------------------------------


class TestBadImageRelocation:
    def test_corrupt_image_is_moved_to_failed_dir(
        self, tmp_path, session_factory, inference_results_root
    ):
        """A corrupt oldest JPEG fails ``_stage_decoded_png`` on the pngdec
        chain; the bad file must be relocated to
        ``{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`` (mirroring
        ``_move_bad_folder_image_source``) so the next run proceeds to the
        next image instead of wedging forever.

        EXPECTED FAILURE on the unfixed tree: the run fails but the bad
        image stays in place — every subsequent run re-selects it.

        Validates: Requirements 1.3 (expected behavior 2.4)
        """
        folder = tmp_path / "images"
        folder.mkdir()
        bad = write_corrupt_jpeg(str(folder / "bad.jpg"), 0)
        write_real_jpeg(str(folder / "good.jpg"), 100)

        artifact_path = write_artifact_set(
            tmp_path / "artifacts",
            compiled=make_folder_doc(folder, decoder="pngdec"))
        seed_registration(session_factory, artifact_path)
        execution_id = seed_execution(session_factory, "exec-1")

        manager = FakePipelineManager()
        row = run_execution(session_factory, execution_id, manager)

        # Sanity (holds on unfixed code too): the run failed on the
        # decode/stage step and the pipeline never launched.
        assert row.status == EXECUTION_STATUS_FAILED
        assert "Could not decode image" in (row.error or ""), row.error
        assert manager.calls == []

        relocated = os.path.join(
            failed_dir(inference_results_root), "bad.jpg")
        assert not os.path.exists(bad), (
            "COUNTEREXAMPLE (Req 1.3): the corrupt image {0!r} is still "
            "in the source folder after the failed run — the same bad "
            "image wedges every subsequent run of this workflow".format(bad))
        assert os.path.isfile(relocated), (
            "COUNTEREXAMPLE (Req 1.3): the corrupt image was not "
            "relocated to {0!r} (mirroring the legacy "
            "_move_bad_folder_image_source)".format(relocated))


# ---------------------------------------------------------------------------
# Case 5: a pipeline failure relocates the resolved JPEG to failed/
# (Requirement 1.4 / 2.5)
# ---------------------------------------------------------------------------


class TestPipelineFailureRelocation:
    def test_pipeline_failure_moves_the_resolved_jpeg_to_failed_dir(
        self, tmp_path, session_factory, inference_results_root
    ):
        """When the pipeline run itself fails for a folder-resolved frame,
        the resolved JPEG must be relocated to
        ``{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`` (the legacy
        ``except`` branch) so the folder still drains.

        EXPECTED FAILURE on the unfixed tree: the failure handler records
        the failed status but leaves the image in place.

        Validates: Requirements 1.4 (expected behavior 2.5)
        """
        folder = tmp_path / "images"
        folder.mkdir()
        oldest = write_fake_jpeg(str(folder / "a.jpg"), 0)
        write_fake_jpeg(str(folder / "b.jpg"), 100)

        artifact_path = write_artifact_set(
            tmp_path / "artifacts", compiled=make_folder_doc(folder))
        seed_registration(session_factory, artifact_path)
        execution_id = seed_execution(session_factory, "exec-1")

        manager = FakePipelineManager(
            error=RuntimeError("Pipeline failed: internal data stream error"))
        row = run_execution(session_factory, execution_id, manager)

        # Sanity (holds on unfixed code too): the run failed with the
        # pipeline error after resolving the oldest JPEG.
        assert row.status == EXECUTION_STATUS_FAILED
        assert "internal data stream error" in (row.error or "")
        assert manager.calls and oldest in manager.calls[0]

        relocated = os.path.join(
            failed_dir(inference_results_root), "a.jpg")
        assert not os.path.exists(oldest), (
            "COUNTEREXAMPLE (Req 1.4): the resolved JPEG {0!r} is still "
            "in the source folder after the pipeline run failed — the "
            "legacy path relocates it so the folder still drains".format(
                oldest))
        assert os.path.isfile(relocated), (
            "COUNTEREXAMPLE (Req 1.4): the resolved JPEG was not "
            "relocated to {0!r} on pipeline failure".format(relocated))
