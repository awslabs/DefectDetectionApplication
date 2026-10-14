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
"""Property test for producer metadata merging (Task 10.2).

**Feature: custom-python-source, Property 12: Producer metadata merges
under the node's key**

*For any* metadata dict a Frame_Producer returns alongside its frame,
the executor's Run_Metadata after the run carries that dict under a key
identifying the node (``python_source.<nodeId>``), with all other
Run_Metadata entries unaffected by the merge.

Exercised through the real ``WorkflowExecutor.execute()`` wiring with a
REAL producer handler subprocess: the handler echoes the metadata it
finds in the run's Trigger_Context, the fake pipeline manager returns
the generated pipeline Run_Metadata (``tag_values``), and the post-run
handler observes the merged ``tag_values`` — the exact dict the
Bedrock/LLM processors and output bindings receive — following the fake
pipeline-manager/session harness the aravis-free identity tests
established.

**Validates: Requirements 6.7**
"""
import copy
import json
import os
import tempfile
import time
import uuid
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    make_session_factory,
    write_artifact_set,
)

from workflow_engine import gst_plugins, pipeline_executor
from workflow_engine.models import WorkflowExecution, WorkflowRegistration
from workflow_engine.pipeline_executor import (
    EXECUTION_STATUS_COMPLETED,
    EXECUTION_STATUS_PENDING,
    WorkflowExecutor,
)

NODE_ID = "src1"

#: Echoes the metadata carried in the Trigger_Context back as the
#: Produced_Frame's metadata, so hypothesis drives the producer's
#: metadata through the real subprocess protocol.
ECHO_METADATA_HANDLER = """\
def produce_frame(context):
    return {
        "data": b"\\x00" * 4,
        "width": 2,
        "height": 2,
        "format": "GRAY8",
        "metadata": context.get("meta") or {},
    }
"""

SOURCE_DOCUMENT = {
    "schemaVersion": 1,
    "workflowId": "wf-1",
    "workflowVersion": "3",
    "targetArch": DEVICE_ARCH,
    "segments": [
        {
            "name": "s0",
            "elements": [
                {"nodeId": NODE_ID, "factory": "appsrc",
                 "args": {"name": "appsrc_{0}".format(NODE_ID)}},
                {"nodeId": NODE_ID, "factory": "videoconvert", "args": {}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
    ],
    "bindingPoints": [
        {
            "nodeId": NODE_ID,
            "nodeType": "custom_python_source",
            "pythonSourceBinding": True,
            "parameters": {"allowed_uri_prefixes": ""},
            "slots": [],
        }
    ],
    "executorBindings": [],
    "pluginDependencies": [],
}


# --- shared per-module state (one sqlite database and artifact set) ----------

_session_factory = None
_artifact_path = None


def _shared_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory()
    return _session_factory


def _shared_artifact_path():
    global _artifact_path
    if _artifact_path is None:
        root = tempfile.mkdtemp(prefix="pysource_meta_artifacts_")
        _artifact_path = write_artifact_set(root, compiled=SOURCE_DOCUMENT)
        handler_dir = os.path.join(_artifact_path, "python", NODE_ID)
        os.makedirs(handler_dir, exist_ok=True)
        with open(os.path.join(handler_dir, "handler.py"), "w") as f:
            f.write(ECHO_METADATA_HANDLER)
    return _artifact_path


class _FakePipelineManager:
    """Returns the generated pipeline Run_Metadata as the tag values."""

    def __init__(self, tag_values):
        self._tag_values = tag_values

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        return copy.deepcopy(self._tag_values)


def _run_one(tag_values, metadata):
    """One executor run whose producer returns ``metadata``; returns
    (row_status, observed_tag_values)."""
    factory = _shared_session_factory()
    registration_id = "reg-{0}".format(uuid.uuid4().hex)
    execution_id = "exec-{0}".format(uuid.uuid4().hex)
    session = factory()
    try:
        session.add(WorkflowRegistration(
            id=registration_id,
            workflow_id="wf-1",
            version="3",
            arch=DEVICE_ARCH,
            artifact_path=str(_shared_artifact_path()),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id=execution_id,
            registration_id=registration_id,
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
            trigger_context_json=json.dumps({"meta": metadata}),
        ))
        session.commit()
    finally:
        session.close()

    observed = []

    capture_root = tempfile.mkdtemp(prefix="pysource_meta_captures_")
    with patch.object(
        pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
    ), patch.object(gst_plugins, "_scan_registry", return_value=True):
        WorkflowExecutor(
            session_factory=factory,
            pipeline_manager_factory=(
                lambda: _FakePipelineManager(tag_values)
            ),
            post_run_handler=(
                lambda registration, document, tags: observed.append(tags)
            ),
        ).execute(execution_id)

    session = factory()
    try:
        status = session.get(WorkflowExecution, execution_id).status
    finally:
        session.close()
    assert len(observed) == 1
    return status, observed[0]


# --- strategies ---------------------------------------------------------------

_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 9), max_value=10 ** 9),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=15),
)

_JSON_VALUES = st.one_of(
    _JSON_SCALARS,
    st.lists(_JSON_SCALARS, max_size=3),
    st.dictionaries(st.text(max_size=8), _JSON_SCALARS, max_size=3),
)

#: Producer metadata dicts (including empty — the no-merge case).
_METADATA = st.dictionaries(st.text(max_size=10), _JSON_VALUES, max_size=3)

#: Pipeline-produced Run_Metadata. ``python_source`` and ``trigger`` are
#: excluded so the merge's key additions are exactly attributable.
_TAG_VALUES = st.dictionaries(
    st.text(min_size=1, max_size=12).filter(
        lambda k: k not in ("python_source", "trigger")
    ),
    _JSON_VALUES,
    max_size=4,
)


# ---------------------------------------------------------------------------
# Feature: custom-python-source, Property 12: Producer metadata merges
# under the node's key
#
# For any metadata dict a Frame_Producer returns alongside its frame, the
# executor's Run_Metadata after the run carries that dict under
# python_source.<nodeId>, with all other Run_Metadata entries unaffected
# by the merge.
#
# **Validates: Requirements 6.7**
# ---------------------------------------------------------------------------


@settings(max_examples=25, deadline=None)
@given(tag_values=_TAG_VALUES, metadata=_METADATA)
def test_property_12_producer_metadata_merges_under_the_node_key(
    tag_values, metadata
):
    """**Feature: custom-python-source, Property 12: Producer metadata
    merges under the node's key**

    **Validates: Requirements 6.7**
    """
    produced = copy.deepcopy(tag_values)

    status, observed = _run_one(tag_values, metadata)

    assert status == EXECUTION_STATUS_COMPLETED
    # Every pipeline-produced entry is unaffected by the merge.
    for key, value in produced.items():
        assert observed[key] == value
    if metadata:
        # The producer's metadata lands under python_source.<nodeId>.
        assert observed["python_source"] == {NODE_ID: metadata}
        assert set(observed) == set(produced) | {"python_source", "trigger"}
    else:
        # Empty producer metadata merges nothing: the only delta beyond
        # the pipeline's own entries is the seeded trigger key.
        assert "python_source" not in observed
        assert set(observed) == set(produced) | {"trigger"}
