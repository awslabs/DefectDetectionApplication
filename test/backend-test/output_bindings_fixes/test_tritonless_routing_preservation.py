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
"""Preservation property tests (Task 2) for workflow-output-bindings-fixes.

Property 6: Preservation — tritonless gstreamer-side routing identity: for
any tritonless capture document, the executor's PRE-RUN routing output
(the recorded ``output_dir``/``capture_id``/``has_image_results``, the
terminal emlcapture's ``buffer-message-id`` rewrite, and the untouched
``meta`` placeholder) matches today's behavior exactly; non-capture
documents are untouched.

**Validates: Requirements 3.6**

A tritonless capture run IS Defect C's bug condition — but its fix is
strictly POST-run (empty-basename repair + engine-written metadata JSON):
the design requires the gstreamer-side ``meta`` routing and launch string
to stay byte-identical for every document. This module pins that pre-run
surface (nothing here inspects the run directory's post-run contents).

Observation-first: the reference transform below (``_expected_launch``)
encodes the routing behavior OBSERVED on the current (unfixed) tree:

* ``execute()`` derives the per-run dir
  ``{_WORKFLOW_CAPTURE_ROOT}/{workflow_id}/{execution_id}`` and
  ``capture_id = {workflow_id}-{execution_id}``;
* ``_route_capture_outputs`` overwrites (or appends) the terminal
  emlcapture's ``buffer-message-id`` with
  ``file-target_{output_dir}-jpg`` — whether the compiled value was absent
  or carried the default-root path;
* with no ``emltriton`` in the document nothing is declared, so NO
  ``meta`` targets are emitted and a compiled ``{capture_meta}``
  placeholder stays in the args verbatim;
* ``_ensure_terminal_sink`` appends a ``fakesink`` after the terminal
  ``emlcapture``;
* a document whose terminal is NOT an emlcapture records no
  ``output_dir``/``capture_id``/``has_image_results`` and renders
  byte-identically to the compiled document.

These tests MUST PASS today and keep passing after the fix.

Follows the ``test_property_capture_routing.py`` executor-run pattern:
a module-scoped sqlite session factory, per-example registrations, a fake
pipeline manager recording the launch string, and a per-example tmp-dir
``_WORKFLOW_CAPTURE_ROOT`` (the conftest ``capture_root`` fixture is
function-scoped and would be shared across Hypothesis examples).

Runs with the hypothesis profiles registered in ``test/backend-test/
conftest.py`` (``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci``
= 100).
"""
import copy
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

from workflow_engine import gst_plugins, pipeline_executor, rendering
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

WORKFLOW_ID = "wf-1"
_CAPTURE_META_PLACEHOLDER = "{capture_meta}"

#: Launch-safe non-terminal elements (never touched by routing).
_MIDDLE_FACTORIES = st.sampled_from(
    ["videoconvert", "videoscale", "queue", "jpegenc"])


# ---------------------------------------------------------------------------
# Generators: document shapes strictly OUTSIDE the bug condition
# ---------------------------------------------------------------------------

@st.composite
def _shapes(draw):
    """A document shape: 0..2 middle elements, terminal emlcapture (or a
    plain sink), and — for capture terminals — a compiled path that is
    either ABSENT or EQUAL to the default capture root, optionally with the
    compiled ``{capture_meta}`` placeholder."""
    return {
        "middles": draw(st.lists(_MIDDLE_FACTORIES, min_size=0, max_size=2)),
        "is_capture": draw(st.booleans()),
        # "absent": compiled emlcapture has no buffer-message-id;
        # "default_root": it carries file-target_{default root}-jpg.
        "compiled_path": draw(st.sampled_from(["absent", "default_root"])),
        "has_meta_placeholder": draw(st.booleans()),
        "is_anomalous": draw(st.booleans()),
    }


def _build_document(shape, capture_root):
    """The compiled document for a shape (capture_root is the patched
    default root, so 'default_root' compiles the default path)."""
    elements = [{"nodeId": "n1", "factory": "videotestsrc",
                 "args": {"num-buffers": 1}}]
    for index, factory in enumerate(shape["middles"]):
        elements.append(
            {"nodeId": "m{0}".format(index), "factory": factory, "args": {}})
    if shape["is_capture"]:
        args = {}
        if shape["compiled_path"] == "default_root":
            args["buffer-message-id"] = "file-target_{0}-jpg".format(
                capture_root)
        if shape["has_meta_placeholder"]:
            args[u"meta"] = _CAPTURE_META_PLACEHOLDER
        elements.append(
            {"nodeId": "capture_1", "factory": "emlcapture", "args": args})
    else:
        elements.append({"nodeId": "sink_1", "factory": "fakesink", "args": {}})
    return {
        "schemaVersion": 1,
        "workflowId": WORKFLOW_ID,
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{"name": "s0", "elements": elements}],
        "executorBindings": [],
        "pluginDependencies": [],
    }


def _expected_launch(document, shape, output_dir):
    """The launch string TODAY's executor produces for this document —
    the test's own reference transform (deliberately NOT calling
    ``_route_capture_outputs``, so a behavior change in the fixed code
    cannot silently move this expectation with it)."""
    expected = copy.deepcopy(document)
    if shape["is_capture"]:
        terminal = expected["segments"][0]["elements"][-1]
        # Overwrite-or-append the base file target; the compiled
        # {capture_meta} placeholder stays verbatim (nothing declared
        # without emltriton, so no meta targets are emitted).
        terminal["args"]["buffer-message-id"] = (
            "file-target_{0}-jpg".format(output_dir))
        # The terminal-sink guard appends a fakesink after emlcapture.
        expected["segments"][0]["elements"].append(
            {"factory": "fakesink", "nodeId": "capture_1", "args": {}})
    return rendering.render_launch_string(expected)


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


def _seed(session_factory, artifact_path, sequence):
    registration_id = "wf-1:3:{0}".format(sequence)
    execution_id = "exec-{0}".format(sequence)
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
        session.add(WorkflowExecution(
            id=execution_id,
            registration_id=registration_id,
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
        ))
        session.commit()
    finally:
        session.close()
    return execution_id


class _RecordingManager:
    def __init__(self, tag_values):
        self.tag_values = tag_values
        self.calls = []

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        self.calls.append(pipeline_str)
        return dict(self.tag_values)


def _run(shape):
    session_factory = _session_factory()
    sequence = next(_IDS)
    root = tempfile.mkdtemp(prefix="default-capture-routing-")
    try:
        capture_root = os.path.join(root, "captures")
        document = _build_document(shape, capture_root)
        artifact_path = write_artifact_set(root, compiled=document)
        execution_id = _seed(session_factory, artifact_path, sequence)
        manager = _RecordingManager(
            tag_values={"is_anomalous": shape["is_anomalous"]})

        with patch.object(
                pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root), \
                patch.object(gst_plugins, "_scan_registry",
                             return_value=True):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: manager,
            ).execute(execution_id)

        session = session_factory()
        try:
            row = session.get(WorkflowExecution, execution_id)
            result = {
                "status": row.status,
                "output_dir": row.output_dir,
                "capture_id": row.capture_id,
                "has_image_results": bool(row.has_image_results),
            }
        finally:
            session.close()
        return document, execution_id, capture_root, manager, result
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 6: tritonless routing identity
# ---------------------------------------------------------------------------

@given(shape=_shapes())
@settings(deadline=None)
def test_default_path_routing_identity(shape):
    """**Property 6: Preservation — tritonless routing identity.**

    For any capture document whose compiled path is absent or equal to the
    default root, the executor records ``output_dir =
    {capture_root}/{workflow_id}/{execution_id}`` / ``capture_id =
    {workflow_id}-{execution_id}`` / ``has_image_results``, and hands the
    pipeline the reference launch string (base file target rewritten to the
    per-run dir, ``{capture_meta}`` placeholder untouched, terminal
    fakesink appended). Non-capture documents render byte-identically to
    the compiled document and record no image results; since
    vlm-parity-run-results (Requirement 2.3) they DO record the per-run
    ``output_dir``/``capture_id`` so the run metadata JSON and
    inference-node frames have a destination (Bedrock-only runs used to
    persist nothing) — ``has_image_results`` alone stays gated on a
    routed terminal capture.

    **Validates: Requirements 3.6**
    """
    document, execution_id, capture_root, manager, result = _run(shape)

    assert result["status"] == EXECUTION_STATUS_COMPLETED
    assert len(manager.calls) == 1, (
        "expected exactly one pipeline run, got {0!r}".format(manager.calls))
    launch = manager.calls[0]

    expected_output_dir = os.path.join(
        capture_root, WORKFLOW_ID, execution_id)
    expected_capture_id = "{0}-{1}".format(WORKFLOW_ID, execution_id)

    if not shape["is_capture"]:
        # Non-capture: identical rendering and no image results. The
        # per-run artifact fields are recorded for every run
        # (vlm-parity-run-results Req 2.3).
        assert result["output_dir"] == expected_output_dir
        assert result["capture_id"] == expected_capture_id
        assert result["has_image_results"] is False
        assert launch == rendering.render_launch_string(document), (
            "PRESERVATION REGRESSION (Property 6): a non-capture document "
            "was mutated before rendering")
        return

    assert result["output_dir"] == expected_output_dir, (
        "PRESERVATION REGRESSION (Property 6): compiled path {0!r} routed "
        "to {1!r} instead of the default per-run dir {2!r}".format(
            shape["compiled_path"], result["output_dir"],
            expected_output_dir))
    assert result["capture_id"] == expected_capture_id
    assert result["has_image_results"] is True

    assert launch == _expected_launch(document, shape, expected_output_dir), (
        "PRESERVATION REGRESSION (Property 6): the routed launch string "
        "changed for a default-path capture document (compiled path "
        "{0!r}, meta placeholder {1!r}):\n  actual:   {2}\n  expected: "
        "{3}".format(
            shape["compiled_path"], shape["has_meta_placeholder"], launch,
            _expected_launch(document, shape, expected_output_dir)))

    # No meta targets without a model: routing never invents
    # triton_inference_* targets for a tritonless document.
    assert "triton_inference_" not in launch
