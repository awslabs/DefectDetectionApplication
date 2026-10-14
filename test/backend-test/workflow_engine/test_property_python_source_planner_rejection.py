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
"""Property test for multi-fed-source planner rejection.

**Feature: custom-python-source, Property 18: The device planner rejects
multi-fed-source documents naming every offender**

*For any* compiled document whose ``bindingPoints`` carry two or more
frame-feed sources in any mix of ``pythonSourceBinding`` and
``aravisBinding`` markers, ``plan_python_sources`` raises an error whose
message names every offending node id, and no Frame_Producer is planned.

**Validates: Requirements 8.5**

Generators mirror the real input space: the packager emits one binding
point per frame-feed node — the ``pythonSourceBinding: true`` shape with
rendered ``allowed_uri_prefixes`` and empty slots, or the
``aravisBinding: true`` shape with rendered camera parameters —
surrounded by any number of non-feed binding points (other camera
families), shuffled into arbitrary order.

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import copy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.python_source import (
    PythonSourceError,
    plan_python_sources,
)

# --- generators --------------------------------------------------------------

_NODE_IDS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
    min_size=1, max_size=12,
)

_PREFIX_PARAMS = st.sampled_from(
    ["", "s3://plant-images/", "s3://a/\nhttps://mes.local/"]
)


def _python_point(node_id, prefixes):
    """The packager's pythonSourceBinding shape (task 3.1)."""
    return {
        "nodeId": node_id,
        "nodeType": "custom_python_source",
        "pythonSourceBinding": True,
        "parameters": {"allowed_uri_prefixes": prefixes},
        "slots": [],
    }


def _aravis_point(node_id):
    """The packager's aravisBinding shape."""
    return {
        "nodeId": node_id,
        "nodeType": "aravis_camera_source",
        "parameters": {"camera_id": "Aravis-Fake-GV01"},
        "slots": [],
        "aravisBinding": True,
    }


@st.composite
def _multi_fed_documents(draw):
    """A compiled document with two or more frame-feed binding points in
    any mix of markers, among optional non-feed points."""
    node_ids = draw(st.lists(_NODE_IDS, min_size=2, max_size=5,
                             unique=True))
    markers = draw(st.lists(st.sampled_from(["python", "aravis"]),
                            min_size=len(node_ids),
                            max_size=len(node_ids)))
    points = []
    for node_id, marker in zip(node_ids, markers):
        if marker == "python":
            points.append(_python_point(node_id, draw(_PREFIX_PARAMS)))
        else:
            points.append(_aravis_point(node_id))

    for index in range(draw(st.integers(min_value=0, max_value=2))):
        points.insert(
            draw(st.integers(min_value=0, max_value=len(points))),
            {
                "nodeId": "v4l2-{0}".format(index),
                "nodeType": "camera_source",
                "parameters": {"device": "/dev/video{0}".format(index)},
                "slots": [{"param": "device", "segment": 0,
                           "element": 0, "arg": "device"}],
            })

    document = {
        "schemaVersion": 1,
        "segments": [{"elements": []}],
        "bindingPoints": points,
    }
    return document, node_ids


# --- property ----------------------------------------------------------------


@given(case=_multi_fed_documents())
def test_planner_rejects_multi_fed_documents_naming_every_offender(case):
    """**Feature: custom-python-source, Property 18: The device planner
    rejects multi-fed-source documents naming every offender**

    **Validates: Requirements 8.5**
    """
    document, node_ids = case
    snapshot = copy.deepcopy(document)

    with pytest.raises(PythonSourceError) as excinfo:
        plan_python_sources(document)

    # Pure over its input: the document is never mutated.
    assert document == snapshot

    # Document-level failure: no single node owns it.
    assert excinfo.value.node_id is None

    # The message names every offending node id.
    message = str(excinfo.value)
    for node_id in node_ids:
        assert "'{0}'".format(node_id) in message
