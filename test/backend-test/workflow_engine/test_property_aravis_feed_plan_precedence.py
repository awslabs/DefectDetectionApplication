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
"""Property test for Aravis feed plan precedence.

**Feature: aravis-camera-input, Property 14: Aravis feed plan precedence**

*For any* compiled document with Aravis binding points and any optional
resolution result, the planned feed for each node SHALL use the
resolution's Aravis assignment values when present and the binding
point's rendered parameters otherwise.

**Validates: Requirements 6.4**

Generators mirror the real input space: the packager emits exactly one
``aravisBinding: true`` binding point per Aravis_Camera_Source_Node with
empty slots and rendered ``camera_id``/``gain``/``exposure`` parameters
(the single-appsrc Frame_Feed contract admits one Aravis point per
document), surrounded by any number of non-Aravis binding points; the
resolution is ``None`` (bindings never resolved), a ``ResolutionResult``
with no assignment for the node (unbound point), or one carrying an
Aravis assignment whose params come from the inventory lookup (spelled
``cameraId``) or a manual override (spelled ``camera_id``), plus stray
assignments for unrelated node ids.

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import copy

from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.aravis_feed import AravisFeed, plan_aravis_feeds
from workflow_engine.camera_binding import (
    STATUS_RESOLVED,
    ResolutionResult,
)

# --- generators --------------------------------------------------------------

_VALID_GAIN = st.integers(min_value=0, max_value=100)
_VALID_EXPOSURE = st.integers(min_value=0, max_value=10_000_000)

#: Aravis runtime camera ids as camera_manager connects by.
_CAMERA_IDS = st.sampled_from(
    ["Aravis-Fake-GV01", "Basler-12345678", "Allied-9A3F", "Lucid-224400"]
)

#: Acquisition parameters the executor forwards to the camera manager.
_CONFIG_PARAMS = ("gain", "exposure")


@st.composite
def _rendered_parameters(draw):
    """The packager's defaults-overlaid rendered parameter values: a
    non-empty ``camera_id`` plus optional ``gain``/``exposure``."""
    parameters = {"camera_id": draw(_CAMERA_IDS)}
    if draw(st.booleans()):
        parameters["gain"] = draw(_VALID_GAIN)
    if draw(st.booleans()):
        parameters["exposure"] = draw(_VALID_EXPOSURE)
    return parameters


@st.composite
def _assignment_params(draw):
    """Resolved Aravis assignment params: the inventory-lookup shape
    spells the identity ``cameraId`` (build_inventory reported params),
    the override shape spells it ``camera_id`` (the descriptor's name);
    both may carry it, in which case ``camera_id`` wins. ``gain`` /
    ``exposure`` are optional and may be ``None`` (treated as absent)."""
    params = {}
    spelling = draw(st.sampled_from(["camera_id", "cameraId", "both"]))
    if spelling in ("camera_id", "both"):
        params["camera_id"] = draw(_CAMERA_IDS)
    if spelling in ("cameraId", "both"):
        params["cameraId"] = draw(_CAMERA_IDS)
    for name in _CONFIG_PARAMS:
        choice = draw(st.sampled_from(["absent", "present", "none"]))
        if choice == "present":
            params[name] = draw(_VALID_GAIN if name == "gain"
                                else _VALID_EXPOSURE)
        elif choice == "none":
            params[name] = None
    return params


@st.composite
def _non_aravis_points(draw):
    """Binding points of other camera node families — never planned as
    Aravis feeds."""
    points = []
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        points.append({
            "nodeId": "v4l2-n{}".format(index),
            "nodeType": "camera_source",
            "parameters": {"device": "/dev/video{}".format(index)},
            "slots": [{"param": "device", "segment": 0,
                       "element": 0, "arg": "device"}],
        })
    return points


@st.composite
def _plan_cases(draw):
    """A compiled document with exactly one Aravis binding point among
    optional non-Aravis points, and an optional resolution: ``None``, a
    resolution without an assignment for the node, or one carrying an
    Aravis assignment (plus stray assignments for other node ids)."""
    node_id = "n{}".format(draw(st.integers(min_value=1, max_value=9)))
    rendered = draw(_rendered_parameters())
    aravis_point = {
        "nodeId": node_id,
        "nodeType": "aravis_camera_source",
        "parameters": rendered,
        "slots": [],
        "aravisBinding": True,
    }

    points = draw(_non_aravis_points())
    points.insert(draw(st.integers(min_value=0, max_value=len(points))),
                  aravis_point)
    document = {
        "schemaVersion": 1,
        "segments": [{"elements": [
            {"nodeId": node_id, "type": "appsrc",
             "args": {"name": "appsrc_{}".format(node_id)}},
            {"nodeId": node_id, "type": "videoconvert", "args": {}},
        ]}],
        "bindingPoints": points,
    }

    variant = draw(st.sampled_from(["none", "no-assignment", "assignment"]))
    assignment_params = None
    if variant == "none":
        resolution = None
    else:
        assignments = {}
        # Stray assignments for unrelated node ids never leak into the
        # node's plan.
        if draw(st.booleans()):
            assignments["other-node"] = {
                "cameraSourceId": "arv-aaaaaaaaaaaa",
                "params": {"cameraId": draw(_CAMERA_IDS)},
            }
        if variant == "assignment":
            assignment_params = draw(_assignment_params())
            # Keep the effective camera id usable: the precedence
            # property is about which values run, not the error path.
            if not any(isinstance(assignment_params.get(key), str)
                       and assignment_params.get(key)
                       for key in ("camera_id", "cameraId")):
                assignment_params["camera_id"] = draw(_CAMERA_IDS)
            assignments[node_id] = {
                "cameraSourceId": draw(st.one_of(
                    st.none(), st.just("cfg-is-1"))),
                "params": assignment_params,
            }
        resolution = ResolutionResult(
            document=document,
            status=STATUS_RESOLVED,
            aravis_assignments=assignments,
        )

    return document, resolution, node_id, rendered, assignment_params


# --- expected-outcome model ---------------------------------------------------


def _model_feed(node_id, effective):
    """Requirement 6.4: the feed runs the effective values — camera id
    from the first non-empty accepted spelling, gain/exposure joining
    the config exactly when present and not ``None``."""
    camera_id = None
    for key in ("camera_id", "cameraId"):
        value = effective.get(key)
        if isinstance(value, str) and value:
            camera_id = value
            break
    config = {name: effective[name] for name in _CONFIG_PARAMS
              if effective.get(name) is not None}
    return AravisFeed(node_id=node_id, camera_id=camera_id, config=config)


# --- property ----------------------------------------------------------------


@given(case=_plan_cases())
def test_aravis_feed_plan_precedence(case):
    """**Feature: aravis-camera-input, Property 14: Aravis feed plan
    precedence**

    **Validates: Requirements 6.4**
    """
    document, resolution, node_id, rendered, assignment_params = case
    snapshot = copy.deepcopy(document)

    feeds = plan_aravis_feeds(document, resolution)

    # Pure over its inputs: the document is never mutated.
    assert document == snapshot

    # 6.4 precedence: the assignment's values when the resolution
    # carries one for the node, else the rendered parameters.
    effective = (assignment_params if assignment_params is not None
                 else rendered)
    assert feeds == [_model_feed(node_id, effective)]

    # The planned feed targets the Aravis node, never a non-Aravis
    # point or a stray-assignment node id.
    assert feeds[0].node_id == node_id
