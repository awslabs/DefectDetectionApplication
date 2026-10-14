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
"""Property test for non-destructive Trigger_Context seeding (Task 6.3).

**Feature: custom-python-source, Property 2: Trigger seeding never
disturbs pipeline-produced metadata**

*For any* Run_Metadata dict produced by a pipeline run and any
Trigger_Context, seeding places the context under ``trigger`` exactly
when no ``trigger`` key already exists, and leaves every pre-existing
entry (including a pre-existing ``trigger``) unchanged.

Exercised through the real ``WorkflowExecutor.execute()`` wiring — a fake
pipeline manager returns the generated Run_Metadata (``tag_values``), the
execution row carries the generated Trigger_Context as
``trigger_context_json``, and the post-run handler observes the seeded
``tag_values`` (the exact dict the Bedrock/LLM processors and output
bindings receive) — following the fake pipeline-manager/session harness
the aravis-free identity tests established.

**Validates: Requirements 2.5, 2.7**
"""
import copy
import json
import tempfile
import time
import uuid
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
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

# ---------------------------------------------------------------------------
# Shared harness (one sqlite database and artifact set across examples;
# per-example isolation through unique registration/execution ids)
# ---------------------------------------------------------------------------

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
        root = tempfile.mkdtemp(prefix="trigger_seeding_artifacts_")
        _artifact_path = write_artifact_set(root)
    return _artifact_path


class _FakePipelineManager:
    """Returns the generated Run_Metadata as the pipeline's tag values."""

    def __init__(self, tag_values):
        self._tag_values = tag_values

    def run_pipeline(self, pipeline_str, frame_data=None,
                     latency_metrics=None, status_sink=None):
        return copy.deepcopy(self._tag_values)


def _run_one(tag_values, trigger_context_json):
    """One executor run; returns (row_status, observed_tag_values)."""
    factory = _shared_session_factory()
    registration_id = "reg-{0}".format(uuid.uuid4().hex)
    execution_id = "exec-{0}".format(uuid.uuid4().hex)
    session = factory()
    try:
        session.add(WorkflowRegistration(
            id=registration_id,
            workflow_id="wf-1",
            version="3",
            arch="x86_64",
            artifact_path=str(_shared_artifact_path()),
            status="registered",
            registered_at=int(time.time()),
        ))
        session.add(WorkflowExecution(
            id=execution_id,
            registration_id=registration_id,
            started_at=int(time.time()),
            status=EXECUTION_STATUS_PENDING,
            trigger_context_json=trigger_context_json,
        ))
        session.commit()
    finally:
        session.close()

    observed = []

    def handler(registration, document, handed_tag_values):
        observed.append(handed_tag_values)

    capture_root = tempfile.mkdtemp(prefix="trigger_seeding_captures_")
    with patch.object(
        pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", capture_root
    ), patch.object(gst_plugins, "_scan_registry", return_value=True):
        WorkflowExecutor(
            session_factory=factory,
            pipeline_manager_factory=(
                lambda: _FakePipelineManager(tag_values)
            ),
            post_run_handler=handler,
        ).execute(execution_id)

    session = factory()
    try:
        status = session.get(WorkflowExecution, execution_id).status
    finally:
        session.close()
    assert len(observed) == 1
    return status, observed[0]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10 ** 9), max_value=10 ** 9),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)

_JSON_VALUES = st.one_of(
    _JSON_SCALARS,
    st.lists(_JSON_SCALARS, max_size=3),
    st.dictionaries(st.text(max_size=8), _JSON_SCALARS, max_size=3),
)

#: Trigger_Contexts as loaded for the run. ``payload`` is excluded so the
#: loaded context equals the persisted object exactly (payload_json
#: derivation is Property 1's subject, not this property's).
_TRIGGER_CONTEXTS = st.dictionaries(
    st.text(max_size=12).filter(lambda k: k != "payload"),
    _JSON_VALUES,
    max_size=4,
)

#: Pipeline-produced Run_Metadata (tag_values). Keys other than
#: ``trigger``; a pre-existing ``trigger`` entry is injected explicitly by
#: the property so both branches are exercised deliberately.
_TAG_VALUES = st.dictionaries(
    st.text(min_size=1, max_size=12).filter(lambda k: k != "trigger"),
    _JSON_VALUES,
    max_size=4,
)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


# Feature: custom-python-source, Property 2: Trigger seeding never
# disturbs pipeline-produced metadata
@settings(max_examples=25, deadline=None)
@given(
    tag_values=_TAG_VALUES,
    preexisting_trigger=st.one_of(st.none(), _JSON_VALUES),
    context=_TRIGGER_CONTEXTS,
)
def test_seeding_is_non_destructive(tag_values, preexisting_trigger,
                                    context):
    """For any pipeline-produced Run_Metadata and any Trigger_Context,
    the context lands under ``trigger`` exactly when no ``trigger`` key
    pre-exists, and every pre-existing entry (including a pre-existing
    ``trigger``) is unchanged.

    **Validates: Requirements 2.5, 2.7**
    """
    if preexisting_trigger is not None:
        tag_values = dict(tag_values)
        tag_values["trigger"] = preexisting_trigger
    produced = copy.deepcopy(tag_values)

    status, observed = _run_one(tag_values, json.dumps(context))

    assert status == EXECUTION_STATUS_COMPLETED
    # Every pipeline-produced entry is unchanged (Req 2.7).
    for key, value in produced.items():
        assert observed[key] == value
    if "trigger" in produced:
        # A TAG-produced trigger key is never overwritten (Req 2.7).
        assert observed == produced
    else:
        # The context lands under `trigger`, and nothing else changed
        # (Req 2.5).
        assert observed["trigger"] == context
        assert set(observed.keys()) == set(produced.keys()) | {"trigger"}


def test_triggerless_run_seeds_empty_context():
    """A run whose execution row has no trigger context (NULL column —
    every manual run) seeds ``{"trigger": {}}``, the only Run_Metadata
    delta allowed by Requirement 11.1.

    **Validates: Requirements 2.5**
    """
    status, observed = _run_one({"is_anomalous": True}, None)

    assert status == EXECUTION_STATUS_COMPLETED
    assert observed == {"is_anomalous": True, "trigger": {}}
