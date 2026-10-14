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
"""Preservation property tests (Task 2) for folder-source-image-consumption.

Property 2: Preservation — Non-Folder Runs and Run Semantics Unchanged:
for any run where the bug condition does NOT hold (no ``filesrc`` element
whose ``location`` is a directory), the fixed executor must produce the
same result as the original — no files deleted or relocated, the same
document mutations, the same folder-resolution selection order, and the
same execution status / metadata / ``failing_node_id`` attribution.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

Observation-first: each property below encodes behavior OBSERVED on the
current (UNFIXED) tree — these tests MUST PASS today and are re-run after
the fix (task 3.3) to prove nothing outside the bug condition changed:

* a single-file ``filesrc`` location is handed to the pipeline verbatim
  (``_stage_frame_sources`` leaves non-directory locations untouched on a
  ``jpegdec`` chain) and the file — and every sibling in its directory —
  survives repeated successful runs byte-for-byte (Requirement 3.1);
* a directory location resolves to the oldest ``.jpg``/``.jpeg`` by mtime
  (``_oldest_image_in_folder``), case-insensitively, ignoring other
  extensions, without touching any file (Requirement 3.2);
* documents with no ``filesrc`` at all complete with the same execution
  row (status/output_dir/capture_id/has_image_results/error) and a launch
  string byte-identical to the compiled document, touching no files in a
  watched directory (Requirements 3.1, 3.3);
* the empty-folder failure mode records the same ``failed`` status, the
  exact "No .jpg/.jpeg image files found" error, and the source node id as
  ``failing_node_id``, without invoking the pipeline or touching the
  folder (Requirement 3.4);
* a pipeline exception on a single-file (non-folder) run records the same
  ``failed`` status, the exception string as ``error``, and a None
  ``failing_node_id`` for unidentifiable errors, leaving the source file
  in place and never creating a ``failed/`` relocation directory
  (Requirements 3.1, 3.4).

Follows the ``test_tritonless_routing_preservation.py`` executor-run
pattern: a module-scoped temp-sqlite session factory, per-example
registrations and tmp dirs, a fake pipeline manager recording launch
strings, and a per-example ``_WORKFLOW_CAPTURE_ROOT`` so runs never touch
the real ``/aws_dda`` tree. Hypothesis profiles come from the suite
conftests (``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
import itertools
import os
import shutil
import tempfile
import time
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from utils import constants

from workflow_engine import gst_plugins, pipeline_executor, rendering
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

WORKFLOW_ID = "wf-1"


# ---------------------------------------------------------------------------
# Document builders (jpegdec chains — never pngdec, so no JP6 staging)
# ---------------------------------------------------------------------------

def _filesrc_document(location, node_id="folder_source_1"):
    """The compiled shape a portal folder_source/file source produces on a
    JPEG-decoding arch: filesrc ! jpegdec ! fakesink."""
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": node_id, "factory": "filesrc",
                     "args": {"location": location}},
                    {"nodeId": None, "factory": "jpegdec", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def _plain_document(middles):
    """A frame-source-free document: videotestsrc ! [middles] ! fakesink."""
    elements = [{"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}}]
    for index, factory in enumerate(middles):
        elements.append(
            {"nodeId": "m{0}".format(index), "factory": factory, "args": {}})
    elements.append({"nodeId": None, "factory": "fakesink", "args": {}})
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{"name": "s0", "elements": elements}],
        "executorBindings": [],
        "pluginDependencies": [],
    }


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _write_files(folder, spec):
    """Create ``{name: (content, mtime)}`` files inside ``folder``."""
    os.makedirs(folder, exist_ok=True)
    for name, (content, mtime) in spec.items():
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(content)
        os.utime(path, (mtime, mtime))


def _snapshot(folder):
    """``{name: (bytes, mtime_ns)}`` for every file in ``folder`` —
    detects deletions, relocations, rewrites, and additions alike."""
    result = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            content = f.read()
        result[name] = (content, os.stat(path).st_mtime_ns)
    return result


# ---------------------------------------------------------------------------
# Executor harness (module-scoped DB, per-example registration)
# ---------------------------------------------------------------------------

_SESSION_FACTORY = None
_IDS = itertools.count(1)


def _session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = make_session_factory()
    return _SESSION_FACTORY


def _seed(session_factory, artifact_path, sequence, runs=1):
    """One registration + ``runs`` pending executions; returns their ids."""
    registration_id = "wf-1:3:{0}".format(sequence)
    execution_ids = [
        "exec-{0}-{1}".format(sequence, run) for run in range(runs)]
    session = session_factory()
    try:
        session.add(WorkflowRegistration(
            id=registration_id,
            workflow_id=WORKFLOW_ID,
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(artifact_path),
            status="registered",
            registered_at=int(time.time()),
        ))
        for execution_id in execution_ids:
            session.add(WorkflowExecution(
                id=execution_id,
                registration_id=registration_id,
                started_at=int(time.time()),
                status=EXECUTION_STATUS_PENDING,
            ))
        session.commit()
    finally:
        session.close()
    return execution_ids


class _RecordingManager:
    """Stubbed GstPipelineManager: records launch strings, returns tags,
    or raises ``error`` to simulate a pipeline-run failure."""

    def __init__(self, tag_values=None, error=None):
        self.tag_values = tag_values or {}
        self.error = error
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        self.calls.append(pipeline_str)
        if self.error is not None:
            raise self.error
        return dict(self.tag_values)


def _execute(session_factory, manager, capture_root, execution_id):
    with patch.object(
            pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root), \
            patch.object(gst_plugins, "_scan_registry", return_value=True):
        WorkflowExecutor(
            session_factory=session_factory,
            pipeline_manager_factory=lambda: manager,
        ).execute(execution_id)


def _row(session_factory, execution_id):
    session = session_factory()
    try:
        row = session.get(WorkflowExecution, execution_id)
        return {
            "status": row.status,
            "error": row.error,
            "failing_node_id": row.failing_node_id,
            "output_dir": row.output_dir,
            "capture_id": row.capture_id,
            "has_image_results": bool(row.has_image_results),
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_BASENAMES = st.text(alphabet="abcdefghij", min_size=1, max_size=8)
_JPEG_EXTS = st.sampled_from([".jpg", ".jpeg", ".JPG", ".JPEG"])
_OTHER_EXTS = st.sampled_from([".txt", ".png", ".json"])
_CONTENT = st.binary(min_size=1, max_size=64)
_NODE_IDS = st.sampled_from(
    ["folder_source_1", "file_source_1", "input_a", "n-42"])


@st.composite
def _folder_contents(draw, min_jpegs=1, max_jpegs=5):
    """``{filename: (content, mtime)}`` with ``min_jpegs..max_jpegs`` fake
    JPEGs at DISTINCT mtimes plus 0..2 non-JPEG distractors; also returns
    the JPEG filename with the oldest mtime."""
    n_jpegs = draw(st.integers(min_value=min_jpegs, max_value=max_jpegs))
    n_others = draw(st.integers(min_value=0, max_value=2))
    total = n_jpegs + n_others
    bases = draw(st.lists(
        _BASENAMES, min_size=total, max_size=total, unique=True))
    mtimes = draw(st.lists(
        st.integers(min_value=1_000_000_000, max_value=1_600_000_000),
        min_size=total, max_size=total, unique=True))
    spec = {}
    jpeg_names = []
    for index in range(n_jpegs):
        name = bases[index] + draw(_JPEG_EXTS)
        spec[name] = (draw(_CONTENT), mtimes[index])
        jpeg_names.append(name)
    for index in range(n_jpegs, total):
        name = bases[index] + draw(_OTHER_EXTS)
        spec[name] = (draw(_CONTENT), mtimes[index])
    oldest = min(jpeg_names, key=lambda name: spec[name][1])
    return spec, oldest


# ---------------------------------------------------------------------------
# Property 2a: single-file filesrc locations are never consumed
# ---------------------------------------------------------------------------

@given(contents=_folder_contents(min_jpegs=1, max_jpegs=4),
       target_base=_BASENAMES, target_ext=_JPEG_EXTS, content=_CONTENT,
       node_id=_NODE_IDS)
@settings(deadline=None)
def test_single_file_source_never_consumed(
        contents, target_base, target_ext, content, node_id):
    """**Property 2: Preservation — single-file filesrc locations.**

    For any single-file ``filesrc`` location (not a directory), repeated
    successful runs complete, the launch string references the file
    verbatim each time, and neither the file nor any sibling in its
    directory is deleted, relocated, or modified.

    **Validates: Requirements 3.1**
    """
    spec, _ = contents
    root = tempfile.mkdtemp(prefix="folder-consumption-preservation-")
    try:
        images = os.path.join(root, "images")
        target_name = "target_{0}{1}".format(target_base, target_ext)
        spec = dict(spec)
        spec[target_name] = (content, 1_700_000_000)
        _write_files(images, spec)
        target_path = os.path.join(images, target_name)

        document = _filesrc_document(target_path, node_id=node_id)
        artifact_path = write_artifact_set(root, compiled=document)
        session_factory = _session_factory()
        execution_ids = _seed(
            session_factory, artifact_path, next(_IDS), runs=2)
        capture_root = os.path.join(root, "captures")
        manager = _RecordingManager()

        before = _snapshot(images)
        for execution_id in execution_ids:
            _execute(session_factory, manager, capture_root, execution_id)
            result = _row(session_factory, execution_id)
            assert result["status"] == EXECUTION_STATUS_COMPLETED, (
                "PRESERVATION REGRESSION (Property 2): a single-file "
                "filesrc run no longer completes: {0!r}".format(result))
            assert _snapshot(images) == before, (
                "PRESERVATION REGRESSION (Property 2): run {0} touched "
                "files in the single-file source's directory".format(
                    execution_id))
        assert len(manager.calls) == 2
        for launch in manager.calls:
            assert target_path in launch, (
                "PRESERVATION REGRESSION (Property 2): the single-file "
                "location {0!r} was not handed to the pipeline verbatim:"
                "\n  {1}".format(target_path, launch))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2b: folder selection order — oldest .jpg/.jpeg by mtime
# ---------------------------------------------------------------------------

@given(contents=_folder_contents(min_jpegs=1, max_jpegs=5),
       node_id=_NODE_IDS)
@settings(deadline=None)
def test_folder_selection_resolves_oldest_by_mtime(contents, node_id):
    """**Property 2: Preservation — folder selection order.**

    For any folder of fake JPEGs at random distinct mtimes (plus non-JPEG
    distractors), ``_stage_frame_sources`` resolves the ``filesrc``
    location to the oldest ``.jpg``/``.jpeg`` by mtime — case-insensitive
    on the extension, ignoring other files — and touches nothing on disk.

    **Validates: Requirements 3.2**
    """
    spec, oldest = contents
    root = tempfile.mkdtemp(prefix="folder-consumption-preservation-")
    try:
        images = os.path.join(root, "images")
        _write_files(images, spec)
        before = _snapshot(images)

        document = _filesrc_document(images, node_id=node_id)
        WorkflowExecutor._stage_frame_sources(document)

        resolved = document["segments"][0]["elements"][0]["args"]["location"]
        assert resolved == os.path.join(images, oldest), (
            "PRESERVATION REGRESSION (Property 2): selection order changed "
            "— resolved {0!r}, expected oldest-by-mtime {1!r} from "
            "{2!r}".format(resolved, oldest, sorted(spec)))
        assert _snapshot(images) == before, (
            "PRESERVATION REGRESSION (Property 2): _stage_frame_sources "
            "touched files while resolving a folder location")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2c: non-filesrc documents — outcome and filesystem untouched
# ---------------------------------------------------------------------------

_MIDDLE_FACTORIES = st.sampled_from(
    ["videoconvert", "videoscale", "queue", "jpegenc"])


@given(middles=st.lists(_MIDDLE_FACTORIES, min_size=0, max_size=2),
       contents=_folder_contents(min_jpegs=1, max_jpegs=3),
       is_anomalous=st.booleans())
@settings(deadline=None)
def test_non_filesrc_document_untouched(middles, contents, is_anomalous):
    """**Property 2: Preservation — non-filesrc documents.**

    For any document with no ``filesrc`` (no frame source at all),
    ``execute()`` completes with the same execution row (no error, no
    image results), hands the pipeline a launch string byte-identical
    to the compiled document's rendering, and touches no files in a
    watched directory.

    Contract update (vlm-parity-run-results Requirement 2.3): the
    executor now ALWAYS records ``output_dir``/``capture_id`` on the
    execution row — even for non-capture documents — so the run
    metadata JSON and inference-node frames have a destination. The
    expected row reflects that; ``has_image_results`` stays false.

    **Validates: Requirements 3.1, 3.3**
    """
    spec, _ = contents
    root = tempfile.mkdtemp(prefix="folder-consumption-preservation-")
    try:
        watched = os.path.join(root, "watched")
        _write_files(watched, spec)
        before = _snapshot(watched)

        document = _plain_document(middles)
        artifact_path = write_artifact_set(root, compiled=document)
        session_factory = _session_factory()
        (execution_id,) = _seed(session_factory, artifact_path, next(_IDS))
        capture_root = os.path.join(root, "captures")
        manager = _RecordingManager(
            tag_values={"is_anomalous": is_anomalous})

        _execute(session_factory, manager, capture_root, execution_id)

        result = _row(session_factory, execution_id)
        assert result == {
            "status": EXECUTION_STATUS_COMPLETED,
            "error": None,
            "failing_node_id": None,
            "output_dir": os.path.join(capture_root, "wf-1", execution_id),
            "capture_id": "wf-1-{0}".format(execution_id),
            "has_image_results": False,
        }, ("PRESERVATION REGRESSION (Property 2): a non-filesrc run's "
            "execution row changed: {0!r}".format(result))
        assert manager.calls == [rendering.render_launch_string(document)], (
            "PRESERVATION REGRESSION (Property 2): a non-filesrc document "
            "was mutated before rendering: {0!r}".format(manager.calls))
        assert _snapshot(watched) == before, (
            "PRESERVATION REGRESSION (Property 2): a non-filesrc run "
            "touched files in an unrelated directory")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2d: empty-folder failure mode preserved
# ---------------------------------------------------------------------------

@st.composite
def _jpegless_contents(draw):
    """``{filename: (content, mtime)}`` with NO .jpg/.jpeg files —
    empty, or only non-JPEG distractors the resolver must ignore."""
    n_others = draw(st.integers(min_value=0, max_value=3))
    bases = draw(st.lists(
        _BASENAMES, min_size=n_others, max_size=n_others, unique=True))
    return {
        base + draw(_OTHER_EXTS): (
            draw(_CONTENT),
            draw(st.integers(
                min_value=1_000_000_000, max_value=1_600_000_000)),
        )
        for base in bases
    }


@given(spec=_jpegless_contents(), node_id=_NODE_IDS)
@settings(deadline=None)
def test_empty_folder_failure_mode_preserved(spec, node_id):
    """**Property 2: Preservation — empty-folder failure mode.**

    For any folder with no ``.jpg``/``.jpeg`` files, the run fails through
    the existing ``FrameSourceError`` path: the execution row records the
    same ``failed`` status, the exact "No .jpg/.jpeg image files found"
    error, and the source node id as ``failing_node_id``; the pipeline is
    never invoked and the folder is untouched.

    **Validates: Requirements 3.4**
    """
    root = tempfile.mkdtemp(prefix="folder-consumption-preservation-")
    try:
        images = os.path.join(root, "images")
        _write_files(images, spec)
        before = _snapshot(images)

        document = _filesrc_document(images, node_id=node_id)
        artifact_path = write_artifact_set(root, compiled=document)
        session_factory = _session_factory()
        (execution_id,) = _seed(session_factory, artifact_path, next(_IDS))
        capture_root = os.path.join(root, "captures")
        manager = _RecordingManager()

        _execute(session_factory, manager, capture_root, execution_id)

        result = _row(session_factory, execution_id)
        assert result["status"] == EXECUTION_STATUS_FAILED
        assert result["error"] == (
            "No .jpg/.jpeg image files found in folder '{0}'".format(images)
        ), ("PRESERVATION REGRESSION (Property 2): the empty-folder error "
            "message changed: {0!r}".format(result["error"]))
        assert result["failing_node_id"] == node_id, (
            "PRESERVATION REGRESSION (Property 2): the empty-folder "
            "failure is no longer attributed to the source node: "
            "{0!r}".format(result["failing_node_id"]))
        assert manager.calls == [], (
            "PRESERVATION REGRESSION (Property 2): the pipeline ran "
            "despite an unresolvable frame source")
        assert _snapshot(images) == before, (
            "PRESERVATION REGRESSION (Property 2): the empty-folder "
            "failure path touched files in the source folder")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 2e: pipeline-exception failure mode preserved (non-folder run)
# ---------------------------------------------------------------------------

_PIPELINE_ERRORS = st.sampled_from([
    "Pipeline failed: internal data stream error",
    "Pipeline timed out after 120s",
    "could not link elements",
])


@given(contents=_folder_contents(min_jpegs=1, max_jpegs=3),
       target_base=_BASENAMES, target_ext=_JPEG_EXTS, content=_CONTENT,
       node_id=_NODE_IDS, message=_PIPELINE_ERRORS)
@settings(deadline=None)
def test_pipeline_failure_mode_preserved_for_single_file_sources(
        contents, target_base, target_ext, content, node_id, message):
    """**Property 2: Preservation — pipeline-exception failure mode.**

    For any single-file ``filesrc`` run whose pipeline raises, the run
    fails through the existing pipeline-exception path: the execution row
    records the same ``failed`` status, the exception string as ``error``,
    and a None ``failing_node_id`` (no element is identifiable from these
    messages). The single-file source — and every sibling in its
    directory — stays in place, and no ``failed/`` relocation directory
    appears under ``{INFERENCE_RESULTS_DIR}/{workflow_id}/``.

    **Validates: Requirements 3.1, 3.4**
    """
    spec, _ = contents
    root = tempfile.mkdtemp(prefix="folder-consumption-preservation-")
    try:
        images = os.path.join(root, "images")
        target_name = "target_{0}{1}".format(target_base, target_ext)
        spec = dict(spec)
        spec[target_name] = (content, 1_700_000_000)
        _write_files(images, spec)
        target_path = os.path.join(images, target_name)
        before = _snapshot(images)

        document = _filesrc_document(target_path, node_id=node_id)
        artifact_path = write_artifact_set(root, compiled=document)
        session_factory = _session_factory()
        (execution_id,) = _seed(session_factory, artifact_path, next(_IDS))
        capture_root = os.path.join(root, "captures")
        inference_root = os.path.join(root, "inference-results")
        manager = _RecordingManager(error=RuntimeError(message))

        with patch.object(constants, "INFERENCE_RESULTS_DIR",
                          inference_root):
            _execute(session_factory, manager, capture_root, execution_id)

        result = _row(session_factory, execution_id)
        assert result["status"] == EXECUTION_STATUS_FAILED, (
            "PRESERVATION REGRESSION (Property 2): a pipeline exception "
            "no longer fails the run: {0!r}".format(result))
        assert result["error"] == message, (
            "PRESERVATION REGRESSION (Property 2): the pipeline-failure "
            "error changed: {0!r} != {1!r}".format(result["error"], message))
        assert result["failing_node_id"] is None, (
            "PRESERVATION REGRESSION (Property 2): an unidentifiable "
            "pipeline error is now attributed to a node: {0!r}".format(
                result["failing_node_id"]))
        assert manager.calls and target_path in manager.calls[0]
        assert _snapshot(images) == before, (
            "PRESERVATION REGRESSION (Property 2): a failing single-file "
            "run deleted/relocated files in the source's directory")
        assert not os.path.exists(
            os.path.join(inference_root, WORKFLOW_ID, "failed")), (
            "PRESERVATION REGRESSION (Property 2): a failing single-file "
            "run created a failed/ relocation directory — single-file "
            "sources must never be relocated")
    finally:
        shutil.rmtree(root, ignore_errors=True)
