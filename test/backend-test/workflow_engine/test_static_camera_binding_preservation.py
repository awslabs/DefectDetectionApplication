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
"""Preservation suite — every camera source that is NOT the virtual
Static_Image_Camera resolves and plans exactly as it does today.

Bugfix: `.kiro/specs/static-camera-workflow-binding-invisible/`, task 1.3.

Written observation-first against UNFIXED code and PASSING there: these are
the assertions the fix may not disturb. The expected values are written out
explicitly rather than recomputed from the production helpers, so a change
in those helpers fails here instead of being silently mirrored.

Covers bugfix.md 3.4, 3.5, 3.6:

* physical Aravis, V4L2, CSI, `override` and unbound binding points keep
  their resolved values, assignment shape and feed plan;
* a genuinely missing camera source still marks the set invalid with
  `missing camera source {id}` — the fix removes a false positive, not the
  check;
* a configured Image_Source whose `cameraId` is `static-image-camera` still
  resolves with a populated `params.cameraId` — the wrapping-Image_Source
  path the RF-DETR task-11 verification used as its workaround
  (`docs/detection-training-gap.md`).

_Requirements: 3.4, 3.5, 3.6, Property 2_
"""
import pytest

from camera_sync import CameraSourceState
from camera_sync.inventory import build_inventory
from utils.static_image_camera import STATIC_IMAGE_CAMERA_ID
from workflow_engine.aravis_feed import AravisFeedError, plan_aravis_feeds
from workflow_engine.camera_binding import (
    STATUS_INVALID,
    STATUS_RESOLVED,
    _resolved_parameter_values,
    resolve_bindings,
)


NODE_ID = "n2"


# ---------------------------------------------------------------- helpers

def aravis_document(node_id=NODE_ID, parameters=None):
    return {
        "schemaVersion": 1,
        "segments": [
            {"elements": [{"nodeId": node_id, "type": "appsrc", "args": {}}]}
        ],
        "bindingPoints": [
            {
                "nodeId": node_id,
                "nodeType": "aravis_camera_source",
                "parameters": parameters or {
                    "camera_id": "Aravis-Fake-GV01",
                    "gain": 4,
                    "exposure": 5000000,
                },
                "slots": [],
                "aravisBinding": True,
            }
        ],
    }


def v4l2_document(node_id="n1"):
    """A slot-substituting (non-Aravis) binding point: the x86_64 shape."""
    return {
        "schemaVersion": 1,
        "segments": [
            {
                "elements": [
                    {"nodeId": node_id, "type": "v4l2src",
                     "args": {"device": "/dev/video0"}},
                ]
            }
        ],
        "bindingPoints": [
            {
                "nodeId": node_id,
                "nodeType": "camera_source",
                "parameters": {"device": "/dev/video0"},
                "slots": [
                    {"param": "device", "segment": 0, "element": 0,
                     "arg": "device"}
                ],
            }
        ],
    }


def physical_aravis_entry(camera_id="Aravis-Fake-GV01"):
    return CameraSourceState(
        camera_source_id="arv-deadbeef",
        name="Fake Vendor Fake Model",
        type="AravisDiscovered",
        origin="edge-discovered",
        params={
            "cameraId": camera_id,
            "serial": "1",
            "protocol": "Fake",
            "address": "0.0.0.0",
        },
        capabilities={"aravis": {"model": "Fake Model", "serial": "1"}},
        discovered=True,
    )


def v4l2_entry(device_path="/dev/video3"):
    return CameraSourceState(
        camera_source_id="v4l-cafebabe",
        name="Fake USB Camera",
        type="V4L2Discovered",
        origin="edge-discovered",
        params={"devicePath": device_path},
        capabilities={},
        discovered=True,
    )


def configured_static_wrapper_entry():
    """A configured Image_Source of type Camera whose `cameraId` is the
    static identifier — produced by the normal configured merge, which
    Requirement 3.18 of the discoverability bugfix leaves untouched. This is
    the RF-DETR workaround binding (`cfg-my6j3zx1` on jetson-thor1)."""
    return CameraSourceState(
        camera_source_id="cfg-my6j3zx1",
        name="static-image-camera1",
        type="Camera",
        origin="edge-configured",
        params={
            "cameraId": STATIC_IMAGE_CAMERA_ID,
            "gain": 1,
            "exposure": 500,
        },
        capabilities={},
        discovered=False,
    )


# ------------------------------------------- resolved parameter projection

def test_physical_aravis_resolved_values_are_unchanged():
    """`params` projection plus the `cameraId -> camera_id` alias, written
    out explicitly. _Requirements: 3.5_"""
    values = _resolved_parameter_values(physical_aravis_entry())

    assert values == {
        "cameraId": "Aravis-Fake-GV01",
        "camera_id": "Aravis-Fake-GV01",
        "serial": "1",
        "protocol": "Fake",
        "address": "0.0.0.0",
    }


def test_v4l2_resolved_values_are_unchanged():
    """`devicePath -> device` alias. _Requirements: 3.5_"""
    values = _resolved_parameter_values(v4l2_entry())

    assert values == {
        "devicePath": "/dev/video3",
        "device": "/dev/video3",
    }


def test_configured_static_wrapper_resolved_values_are_unchanged():
    """The workaround path keeps its populated camera id.
    _Requirements: 3.4_"""
    values = _resolved_parameter_values(configured_static_wrapper_entry())

    assert values == {
        "cameraId": STATIC_IMAGE_CAMERA_ID,
        "camera_id": STATIC_IMAGE_CAMERA_ID,
        "gain": 1,
        "exposure": 500,
    }


def test_an_entry_with_no_params_and_no_capabilities_resolves_nothing():
    """The empty case stays empty: the fix must key on a capability that is
    actually present, never fabricate a value. _Requirements: 3.5_"""
    bare = CameraSourceState(
        camera_source_id="cfg-bare",
        name="bare",
        type="Camera",
        origin="edge-configured",
        params={},
        capabilities={},
        discovered=False,
    )

    assert _resolved_parameter_values(bare) == {}


# ------------------------------------------------------- resolution status

def test_physical_aravis_binding_resolves_and_assigns():
    """Assignment shape for a physical Aravis binding. _Requirements: 3.5_"""
    document = aravis_document()
    bindings = {NODE_ID: {"cameraSourceId": "arv-deadbeef"}}

    result = resolve_bindings(document, bindings, [physical_aravis_entry()])

    assert result.status == STATUS_RESOLVED
    assert result.errors == ()
    assert result.aravis_assignments == {
        NODE_ID: {
            "cameraSourceId": "arv-deadbeef",
            "params": {
                "cameraId": "Aravis-Fake-GV01",
                "camera_id": "Aravis-Fake-GV01",
                "serial": "1",
                "protocol": "Fake",
                "address": "0.0.0.0",
            },
        }
    }
    # An Aravis binding point never substitutes element arguments.
    assert result.document["segments"][0]["elements"][0]["args"] == {}


def test_v4l2_binding_substitutes_its_slot():
    """Slot substitution for a non-Aravis binding point.
    _Requirements: 3.5_"""
    document = v4l2_document()
    bindings = {"n1": {"cameraSourceId": "v4l-cafebabe"}}

    result = resolve_bindings(document, bindings, [v4l2_entry()])

    assert result.status == STATUS_RESOLVED
    assert result.document["segments"][0]["elements"][0]["args"]["device"] == \
        "/dev/video3"
    assert result.aravis_assignments == {}


def test_unbound_binding_point_keeps_its_rendered_values():
    """No binding supplied for the point: compiled defaults run as-is.
    _Requirements: 3.5_"""
    document = aravis_document()

    result = resolve_bindings(document, {}, [physical_aravis_entry()])

    assert result.status == STATUS_RESOLVED
    assert result.aravis_assignments == {}
    feeds = plan_aravis_feeds(result.document, result)
    assert [feed.camera_id for feed in feeds] == ["Aravis-Fake-GV01"]


def test_a_genuinely_missing_camera_source_still_marks_the_set_invalid():
    """The check itself is preserved — this bugfix removes a false
    positive, not the guard. _Requirements: 3.6_"""
    document = aravis_document()
    bindings = {NODE_ID: {"cameraSourceId": "cfg-gone"}}

    result = resolve_bindings(document, bindings, [physical_aravis_entry()])

    assert result.status == STATUS_INVALID
    assert result.errors == ("missing camera source cfg-gone",)
    assert result.missing == (
        {"nodeId": NODE_ID, "cameraSourceId": "cfg-gone"},
    )


def test_configured_static_wrapper_binding_resolves_and_plans():
    """The RF-DETR workaround, end to end. _Requirements: 3.4_"""
    document = aravis_document()
    bindings = {NODE_ID: {"cameraSourceId": "cfg-my6j3zx1"}}

    result = resolve_bindings(
        document, bindings, [configured_static_wrapper_entry()]
    )

    assert result.status == STATUS_RESOLVED
    feeds = plan_aravis_feeds(result.document, result)
    assert [feed.camera_id for feed in feeds] == [STATIC_IMAGE_CAMERA_ID]


# ------------------------------------------------------------ feed planning

def test_physical_aravis_feed_plan_is_unchanged():
    """The assignment's params supply the camera id and the gain/exposure.
    _Requirements: 3.5_"""
    document = aravis_document()
    bindings = {NODE_ID: {"cameraSourceId": "arv-deadbeef"}}
    result = resolve_bindings(document, bindings, [physical_aravis_entry()])

    feeds = plan_aravis_feeds(result.document, result)

    assert len(feeds) == 1
    assert feeds[0].node_id == NODE_ID
    assert feeds[0].camera_id == "Aravis-Fake-GV01"


def test_feed_plan_without_a_resolution_uses_rendered_parameters():
    """`resolution=None` — the provider-fallback path. _Requirements: 3.5_"""
    document = aravis_document()

    feeds = plan_aravis_feeds(document, None)

    assert [feed.camera_id for feed in feeds] == ["Aravis-Fake-GV01"]


def test_a_document_with_no_binding_points_plans_no_feeds():
    """Pre-feature documents. _Requirements: 3.5_"""
    document = {"schemaVersion": 1, "segments": [{"elements": []}]}

    assert plan_aravis_feeds(document, None) == []


def test_a_node_with_no_camera_id_anywhere_still_raises():
    """The "no camera id" guard is preserved for the case it is meant for:
    a node whose rendered parameters carry no camera id and whose binding
    supplies none either. The fix must not turn this into a silent pass.
    _Requirements: 3.5_"""
    document = aravis_document(parameters={"gain": 4})

    with pytest.raises(AravisFeedError) as raised:
        plan_aravis_feeds(document, None)

    assert "no camera id" in str(raised.value)


# ------------------------------------------- the agent inventory is untouched

def test_agent_inventory_static_entry_shape_is_unchanged():
    """The virtual entry's shape is the shipped contract pinned by
    `static-image-camera-binding-and-pin-discoverability` Requirements
    3.5/3.17 — `params` MUST stay empty, identity under
    `capabilities.staticImage`. Populating `params` would be a one-line fix
    for Defect 2 and is forbidden. _Requirements: 3.2_"""
    entries = build_inventory(
        [], None, static_image_pinned=True,
        static_image_metadata={"width": 640, "height": 480},
    )

    matching = [
        entry for entry in entries
        if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
    ]
    assert len(matching) == 1
    entry = matching[0]
    assert entry.params == {}, (
        "the virtual entry's params must stay empty (Reqs 3.5/3.17)"
    )
    assert entry.type == "StaticImage"
    assert entry.origin == "edge-discovered"
    assert entry.discovered is True
    assert entry.capabilities["staticImage"]["id"] == STATIC_IMAGE_CAMERA_ID
    assert entry.capabilities["staticImage"]["width"] == 640


def test_agent_inventory_without_a_pin_appends_nothing():
    """Unpinned and never reported: no entry at all. _Requirements: 3.1_"""
    entries = build_inventory([], None)

    assert [
        entry for entry in entries
        if entry.camera_source_id == STATIC_IMAGE_CAMERA_ID
    ] == []
