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
"""Property tests for the executor's per-run capture-output routing (Task 2).

**Feature: deployed-workflow-run-observability, Property 3: Artifact routing
is capture-gated**

*``_route_capture_outputs`` mutates only a terminal ``emlcapture`` element and
only adds ``triton_inference_output_*`` targets for outputs the model
declares; documents without a capture terminal render byte-identically to
today.*

**Validates: Requirements 1.1, 1.5, 8.2**

**Feature: deployed-workflow-run-observability, Property 4: Additive tags**

*The ``{is_anomalous, confidence}`` tag values returned for a run are
unchanged by the presence or absence of artifact routing.*

**Validates: Requirements 1.6**

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
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

from workflow_engine import gst_plugins, rendering
from workflow_engine import pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

_ALL_OUTPUTS = (
    "output_overlay",
    "output_mask",
    "output_capture",
    "output_anomalous",
    "output_confidence",
)
_MODEL_NAME = "model-widget"

# --- generators --------------------------------------------------------------

#: Non-terminal launch-safe elements the routing never touches.
_MIDDLE_FACTORIES = st.sampled_from(
    ["videotestsrc", "videoconvert", "videoscale", "queue", "jpegenc"]
)


@st.composite
def _prefix_elements(draw):
    """0..3 launch-safe non-terminal elements, optionally including the
    model-inference element whose declared outputs gate routing."""
    elements = []
    counter = itertools.count(1)
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        elements.append({
            "nodeId": "n{0}".format(next(counter)),
            "factory": draw(_MIDDLE_FACTORIES),
            "args": {},
        })
    if draw(st.booleans()):
        elements.append({
            "nodeId": "model-{0}".format(next(counter)),
            "factory": "emltriton",
            "args": {"model": _MODEL_NAME},
        })
    return elements


@st.composite
def _documents(draw):
    """A compiled document whose terminal element is either ``emlcapture``
    (a File_Output_Node) or a plain sink (not), with an optional
    model-inference element earlier in the segment."""
    counter = itertools.count(100)
    prefix = draw(_prefix_elements())
    has_model = any(el["factory"] == "emltriton" for el in prefix)
    terminal_is_capture = draw(st.booleans())
    terminal_factory = "emlcapture" if terminal_is_capture else "fakesink"
    prefix.append({
        "nodeId": "term-{0}".format(next(counter)),
        "factory": terminal_factory,
        "args": {},
    })
    document = {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": DEVICE_ARCH,
        "segments": [{"name": "s0", "elements": prefix}],
        "executorBindings": [],
        "pluginDependencies": [],
    }
    return document, terminal_is_capture, has_model


def _write_repo(root, outputs):
    model_dir = os.path.join(root, _MODEL_NAME)
    os.makedirs(model_dir, exist_ok=True)
    lines = ['name: "{0}"'.format(_MODEL_NAME), 'platform: "ensemble"']
    for name in outputs:
        lines += ['output {', '  name: "{0}"'.format(name),
                  '  data_type: TYPE_UINT8', '  dims: [ -1 ]', '}']
    with open(os.path.join(model_dir, "config.pbtxt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _expected_meta(outputs, prefix):
    targets = [
        pipeline_executor._CAPTURE_OUTPUT_TARGETS[name].format(p=prefix)
        for name in pipeline_executor._CAPTURE_OUTPUT_ORDER
        if name in outputs
    ]
    return ",".join(targets)


# --- Property 3 --------------------------------------------------------------


@given(
    data=_documents(),
    declared=st.sets(st.sampled_from(_ALL_OUTPUTS)),
)
@settings(deadline=None)
def test_artifact_routing_is_capture_gated(data, declared):
    """**Feature: deployed-workflow-run-observability, Property 3: Artifact
    routing is capture-gated**

    **Validates: Requirements 1.1, 1.5, 8.2**
    """
    document, terminal_is_capture, has_model = data
    before = copy.deepcopy(document)
    render_before = rendering.render_launch_string(document)

    repo = tempfile.mkdtemp(prefix="capture-routing-repo-")
    try:
        _write_repo(repo, declared)
        output_dir = "/aws_dda/captures/wf-1/exec-1"
        capture_id = "wf-1-exec-1"
        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo):
            routed = WorkflowExecutor._route_capture_outputs(
                document, output_dir, capture_id
            )
    finally:
        shutil.rmtree(repo, ignore_errors=True)

    if not terminal_is_capture:
        # No capture terminal: nothing is routed and the document (and its
        # rendered launch string) is byte-identical to today (R1.5, 8.2).
        assert routed is False
        assert document == before
        assert rendering.render_launch_string(document) == render_before
        return

    # Capture terminal: it is a File_Output_Node run.
    assert routed is True
    terminal = document["segments"][0]["elements"][-1]

    # Only the model's declared outputs are targeted (R1.1, R1.3). With no
    # model in the segment, nothing is declared for the run, so no meta.
    expected_outputs = declared if has_model else set()
    # The broker appends {capture_id}.{ext}; the routed prefix is output_dir.
    prefix = output_dir
    expected_meta = _expected_meta(expected_outputs, prefix)

    if expected_meta:
        assert terminal["args"]["meta"] == expected_meta
        # Every target names only a declared output.
        for name in _ALL_OUTPUTS:
            token = "triton_inference_{0}".format(name)
            assert (token in terminal["args"]["meta"]) == (
                name in expected_outputs
            )
    else:
        assert "meta" not in terminal["args"]

    # The ONLY mutations are on the terminal emlcapture element (its
    # buffer-message-id and, when the model declares outputs, its meta): strip
    # those and the document is otherwise byte-identical to before routing
    # (Property 3 "mutates only a terminal emlcapture element").
    stripped = copy.deepcopy(document)
    stripped["segments"][0]["elements"][-1]["args"].pop("meta", None)
    stripped["segments"][0]["elements"][-1]["args"].pop("buffer-message-id", None)
    assert stripped == before


# --- Property 4 --------------------------------------------------------------

_CAPTURE_DOC = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [{
        "name": "s0",
        "elements": [
            {"nodeId": "n1", "factory": "videotestsrc",
             "args": {"num-buffers": 1}},
            {"nodeId": "n2", "factory": "emltriton",
             "args": {"model": _MODEL_NAME}},
            {"nodeId": "n3", "factory": "jpegenc", "args": {}},
            {"nodeId": "n4", "factory": "emlcapture", "args": {}},
        ],
    }],
    "executorBindings": [],
    "pluginDependencies": [],
}

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
            workflow_id="wf-1",
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


class _Manager:
    def __init__(self, tag_values):
        self.tag_values = tag_values

    def run_pipeline(self, pipeline_str, frame_data=None, latency_metrics=None,
                     status_sink=None):
        return dict(self.tag_values)


@given(
    is_anomalous=st.booleans(),
    confidence=st.floats(min_value=0.0, max_value=1.0,
                         allow_nan=False, allow_infinity=False),
    declared=st.sets(st.sampled_from(_ALL_OUTPUTS)),
)
@settings(deadline=None)
def test_additive_tags_unchanged_by_routing(is_anomalous, confidence, declared):
    """**Feature: deployed-workflow-run-observability, Property 4: Additive
    tags**

    **Validates: Requirements 1.6**
    """
    tag_values = {"is_anomalous": is_anomalous, "confidence": confidence}
    session_factory = _session_factory()
    sequence = next(_IDS)
    root = tempfile.mkdtemp(prefix="capture-routing-tags-")
    repo = tempfile.mkdtemp(prefix="capture-routing-tags-repo-")
    try:
        _write_repo(repo, declared)
        artifact_path = write_artifact_set(root, compiled=_CAPTURE_DOC)
        execution_id = _seed(session_factory, artifact_path, sequence)
        received = []
        capture_root = os.path.join(root, "captures")

        with patch.object(pipeline_executor, "_TRITON_MODEL_REPO", repo), \
                patch.object(
                    pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root), \
                patch.object(gst_plugins, "_scan_registry", return_value=True):
            WorkflowExecutor(
                session_factory=session_factory,
                pipeline_manager_factory=lambda: _Manager(tag_values),
                post_run_handler=lambda reg, doc, tags: received.append(tags),
            ).execute(execution_id)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)

    # Routing was applied (capture terminal), yet the tag values reaching the
    # post-run handler are exactly what the pipeline produced (R1.6).
    assert received == [tag_values]
