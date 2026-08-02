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
"""Unit tests for Aravis binding point resolution in ``resolve_bindings``.

Feature: aravis-camera-input (Requirements 6.1, 6.2, 6.3).

Aravis binding points (``aravisBinding: true``, empty slots) never
substitute element arguments: resolved bindings contribute to the
dedicated ``aravis_assignments`` field on ``ResolutionResult``, consumed
by the executor's Aravis frame feed.
"""
import copy

from camera_sync import CameraSourceState
from workflow_engine.camera_binding import (
    STATUS_INVALID,
    STATUS_RESOLVED,
    resolve_bindings,
)


def make_aravis_document(binding_point=None):
    """A minimal compiled_pipeline.json-shaped document with one
    aravis_camera_source node rendered as an appsrc-headed chain (the
    physical-architecture shape)."""
    document = {
        "schemaVersion": 1,
        "segments": [
            {
                "elements": [
                    {"nodeId": "n2", "type": "appsrc",
                     "args": {"name": "appsrc_n2"}},
                    {"nodeId": "n2", "type": "videoconvert", "args": {}},
                ]
            }
        ],
    }
    if binding_point is not None:
        document["bindingPoints"] = [binding_point]
    return document


def make_aravis_binding_point(node_id="n2", parameters=None):
    """The packager's Aravis binding point shape: aravisBinding marker,
    empty slots, rendered parameter values."""
    return {
        "nodeId": node_id,
        "nodeType": "aravis_camera_source",
        "parameters": parameters or {"camera_id": "Aravis-Fake-GV01",
                                     "gain": 4, "exposure": 5000000},
        "slots": [],
        "aravisBinding": True,
    }


def make_aravis_inventory_entry(camera_source_id="cfg-is-1",
                                camera_id="Aravis-Fake-GV01",
                                gain=None, exposure=None):
    """A configured Camera-type entry from ``build_inventory``: params
    carry the Aravis ``cameraId`` (and gain/exposure when configured)."""
    params = {"cameraId": camera_id}
    if gain is not None:
        params["gain"] = gain
    if exposure is not None:
        params["exposure"] = exposure
    return CameraSourceState(
        camera_source_id=camera_source_id,
        name="GenICam line cam",
        type="Camera",
        origin="edge-configured",
        params=params,
    )


class TestAravisCameraSourceIdResolution:
    def test_resolution_yields_assignment_with_camera_id_params(self):
        """Requirement 6.1: a cameraSourceId binding resolves against the
        local inventory into an aravis assignment; the cameraId inventory
        param aliases to the node's camera_id parameter name."""
        document = make_aravis_document(make_aravis_binding_point())
        inventory = {"cfg-is-1": make_aravis_inventory_entry(
            camera_id="Basler-12345678", gain=10, exposure=16000000)}

        result = resolve_bindings(
            document, {"n2": {"cameraSourceId": "cfg-is-1"}}, inventory)

        assert result.status == STATUS_RESOLVED
        assert result.missing == ()
        assignment = result.aravis_assignments["n2"]
        assert assignment["cameraSourceId"] == "cfg-is-1"
        assert assignment["params"]["camera_id"] == "Basler-12345678"
        assert assignment["params"]["cameraId"] == "Basler-12345678"
        assert assignment["params"]["gain"] == 10
        assert assignment["params"]["exposure"] == 16000000
        # No adapter assignment, and no slot substitution: the rendered
        # segments run exactly as compiled.
        assert result.adapter_assignments == {}
        assert result.document["segments"] == document["segments"]

    def test_input_document_is_never_mutated(self):
        document = make_aravis_document(make_aravis_binding_point())
        snapshot = copy.deepcopy(document)
        inventory = {"cfg-is-1": make_aravis_inventory_entry()}

        resolve_bindings(document, {"n2": {"cameraSourceId": "cfg-is-1"}},
                         inventory)

        assert document == snapshot

    def test_missing_camera_source_marks_invalid_with_reason(self):
        """Requirement 6.3: an id with no local inventory entry follows
        the existing invalid path (missing entry + error reason)."""
        document = make_aravis_document(make_aravis_binding_point())

        result = resolve_bindings(
            document, {"n2": {"cameraSourceId": "cfg-gone"}}, {})

        assert result.status == STATUS_INVALID
        assert result.missing == ({"nodeId": "n2",
                                   "cameraSourceId": "cfg-gone"},)
        assert result.errors == ("missing camera source cfg-gone",)
        assert result.aravis_assignments == {}


class TestAravisOverrideResolution:
    def test_override_yields_assignment_from_override_values(self):
        """Requirement 6.2: constraint-valid override values become the
        assignment params without any inventory lookup."""
        document = make_aravis_document(make_aravis_binding_point())

        result = resolve_bindings(
            document,
            {"n2": {"override": {"camera_id": "Basler-12345678",
                                 "gain": 20, "exposure": 8000000}}},
            {})

        assert result.status == STATUS_RESOLVED
        assignment = result.aravis_assignments["n2"]
        assert assignment["cameraSourceId"] is None
        assert assignment["params"] == {"camera_id": "Basler-12345678",
                                        "gain": 20, "exposure": 8000000}
        assert result.document["segments"] == document["segments"]

    def test_override_violating_catalog_constraints_is_invalid(self):
        """Requirement 6.2: the vendored aravis_camera_source descriptor
        declares gain max 100; a violating override marks the resolution
        invalid with a reason."""
        document = make_aravis_document(make_aravis_binding_point())

        result = resolve_bindings(
            document, {"n2": {"override": {"gain": 500}}}, {})

        assert result.status == STATUS_INVALID
        assert len(result.errors) == 1
        assert "gain" in result.errors[0]
        assert result.aravis_assignments == {}

    def test_empty_camera_id_override_is_invalid(self):
        """camera_id declares min_length 1 in the vendored catalog."""
        document = make_aravis_document(make_aravis_binding_point())

        result = resolve_bindings(
            document, {"n2": {"override": {"camera_id": ""}}}, {})

        assert result.status == STATUS_INVALID
        assert "camera_id" in result.errors[0]
        assert result.aravis_assignments == {}


class TestAravisFreeIdentity:
    def test_document_without_aravis_points_resolves_as_before(self):
        """Documents without Aravis binding points produce results
        identical to the pre-feature behavior: aravis_assignments stays
        empty and slot substitution proceeds unchanged."""
        document = {
            "schemaVersion": 1,
            "segments": [
                {
                    "elements": [
                        {"nodeId": "n1", "type": "v4l2src",
                         "args": {"device": "/dev/video0"}},
                    ]
                }
            ],
            "bindingPoints": [{
                "nodeId": "n1",
                "nodeType": "camera_source",
                "parameters": {"device": "/dev/video0"},
                "slots": [{"param": "device", "segment": 0,
                           "element": 0, "arg": "device"}],
            }],
        }
        inventory = {"cfg-is-1": CameraSourceState(
            camera_source_id="cfg-is-1", name="Line 1 cam", type="Camera",
            origin="edge-configured",
            params={"devicePath": "/dev/video2", "cameraId": "cam-1"})}

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-is-1"}}, inventory)

        assert result.status == STATUS_RESOLVED
        assert result.aravis_assignments == {}
        element = result.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video2"

    def test_document_without_binding_points_has_empty_aravis_assignments(self):
        """Pre-feature documents pass through untouched with the new
        field defaulted empty."""
        document = make_aravis_document()

        result = resolve_bindings(
            document, {"n2": {"cameraSourceId": "cfg-is-1"}}, {})

        assert result.status == STATUS_RESOLVED
        assert result.document is document
        assert result.aravis_assignments == {}
