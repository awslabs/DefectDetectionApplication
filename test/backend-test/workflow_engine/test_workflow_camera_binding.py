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
"""Unit tests for the pure ``resolve_bindings`` device-side resolver.

Feature: camera-registry-sync (Requirements 10.1, 10.3, 10.5, 11.1).
"""
import copy

from camera_sync import CameraSourceState
from workflow_engine.camera_binding import (
    STATUS_INVALID,
    STATUS_RESOLVED,
    ResolutionResult,
    resolve_bindings,
)


def make_document(binding_point=None, slots=None):
    """A minimal compiled_pipeline.json-shaped document with one
    camera_source node rendered as v4l2src (the x86_64 shape)."""
    document = {
        "schemaVersion": 1,
        "segments": [
            {
                "elements": [
                    {"nodeId": "n1", "type": "v4l2src",
                     "args": {"device": "/dev/video0"}},
                    {"nodeId": "n1", "type": "videoconvert", "args": {}},
                ]
            }
        ],
    }
    if binding_point is not None:
        document["bindingPoints"] = [binding_point]
    elif slots is not None:
        document["bindingPoints"] = [{
            "nodeId": "n1",
            "nodeType": "icam_source",
            "parameters": {"device": "/dev/video0"},
            "slots": slots,
        }]
    return document


DEVICE_SLOT = [{"param": "device", "segment": 0, "element": 0, "arg": "device"}]


def make_inventory_entry(camera_source_id="cfg-is-1", device_path="/dev/video2",
                         gain=None, exposure=None):
    params = {"devicePath": device_path, "cameraId": "cam-1"}
    if gain is not None:
        params["gain"] = gain
    if exposure is not None:
        params["exposure"] = exposure
    return CameraSourceState(
        camera_source_id=camera_source_id,
        name="Line 1 cam",
        type="Camera",
        origin="edge-configured",
        params=params,
    )


class TestCameraSourceIdResolution:
    def test_lookup_substitutes_device_path_into_declared_slot(self):
        """Requirement 10.1: the bound source's devicePath resolves the
        device slot parameter."""
        document = make_document(slots=DEVICE_SLOT)
        inventory = {"cfg-is-1": make_inventory_entry(device_path="/dev/video2")}

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-is-1"}}, inventory)

        assert result.status == STATUS_RESOLVED
        assert result.missing == ()
        element = result.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video2"

    def test_gain_and_exposure_substituted_when_present_and_declared(self):
        document = make_document(binding_point={
            "nodeId": "n1", "nodeType": "camera_source",
            "parameters": {"device": "/dev/video0", "gain": 4},
            "slots": DEVICE_SLOT + [
                {"param": "gain", "segment": 0, "element": 0, "arg": "gain"},
            ],
        })
        document["segments"][0]["elements"][0]["args"]["gain"] = 4
        inventory = [make_inventory_entry(gain=10, exposure=16000000)]

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-is-1"}}, inventory)

        args = result.document["segments"][0]["elements"][0]["args"]
        assert args["device"] == "/dev/video2"
        assert args["gain"] == 10

    def test_missing_camera_source_marks_invalid_with_reason(self):
        """Requirement 10.2 shape: unresolved id -> invalid + missing list."""
        document = make_document(slots=DEVICE_SLOT)

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-gone"}}, {})

        assert result.status == STATUS_INVALID
        assert result.missing == ({"nodeId": "n1", "cameraSourceId": "cfg-gone"},)
        assert result.errors == ("missing camera source cfg-gone",)
        # The compiled default is left in place — the registration is
        # invalid, so the document never runs.
        element = result.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video0"

    def test_input_document_is_never_mutated(self):
        document = make_document(slots=DEVICE_SLOT)
        snapshot = copy.deepcopy(document)
        inventory = {"cfg-is-1": make_inventory_entry()}

        resolve_bindings(document, {"n1": {"cameraSourceId": "cfg-is-1"}},
                         inventory)

        assert document == snapshot


class TestOverrideResolution:
    def test_override_substitutes_directly_without_inventory(self):
        """Requirement 10.3: override values apply in place of a lookup."""
        document = make_document(slots=DEVICE_SLOT)

        result = resolve_bindings(
            document, {"n1": {"override": {"device": "/dev/video7"}}}, {})

        assert result.status == STATUS_RESOLVED
        element = result.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video7"

    def test_override_violating_catalog_constraints_is_invalid(self):
        """icam_source device declares min_length 1 in the vendored
        catalog (csi-icam-input-nodes Requirement 6.3)."""
        document = make_document(slots=DEVICE_SLOT)

        result = resolve_bindings(
            document, {"n1": {"override": {"device": ""}}}, {})

        assert result.status == STATUS_INVALID
        assert len(result.errors) == 1
        assert "device" in result.errors[0]

    def test_override_of_undeclared_parameter_is_invalid(self):
        document = make_document(slots=DEVICE_SLOT)

        result = resolve_bindings(
            document, {"n1": {"override": {"bogus": 1}}}, {})

        assert result.status == STATUS_INVALID
        assert "bogus" in result.errors[0]

    def test_override_on_unknown_node_type_passes_unchecked(self):
        """Camera-backed Custom_Node_Types are absent from the vendored
        catalog; the Portal validated their overrides before delivery."""
        document = make_document(binding_point={
            "nodeId": "n1", "nodeType": "acme.custom_cam",
            "parameters": {"device": "/dev/video0"},
            "slots": DEVICE_SLOT,
        })

        result = resolve_bindings(
            document, {"n1": {"override": {"device": "/dev/video3"}}}, {})

        assert result.status == STATUS_RESOLVED
        element = result.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video3"


class TestAdapterBindingPoints:
    def test_adapter_binding_produces_assignment_not_substitution(self):
        """JP4/JP5: the binding selects the adapter's camera, the document
        is untouched."""
        document = make_document(binding_point={
            "nodeId": "n1", "nodeType": "camera_source",
            "parameters": {"device": "/dev/video0"},
            "slots": [], "adapterBinding": True,
        })
        document["segments"][0]["elements"][0] = {
            "nodeId": "n1", "type": "appsrc", "args": {"name": "appsrc"}}
        inventory = {"cfg-is-1": make_inventory_entry(device_path="/dev/video2")}

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-is-1"}}, inventory)

        assert result.status == STATUS_RESOLVED
        assert result.document == document
        assignment = result.adapter_assignments["n1"]
        assert assignment["cameraSourceId"] == "cfg-is-1"
        assert assignment["params"]["devicePath"] == "/dev/video2"
        assert assignment["params"]["cameraId"] == "cam-1"

    def test_adapter_binding_missing_source_is_invalid(self):
        document = make_document(binding_point={
            "nodeId": "n1", "nodeType": "camera_source",
            "parameters": {}, "slots": [], "adapterBinding": True,
        })

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-gone"}}, {})

        assert result.status == STATUS_INVALID
        assert result.adapter_assignments == {}
        assert result.missing == ({"nodeId": "n1", "cameraSourceId": "cfg-gone"},)


class TestNoBindingIdentity:
    def test_document_without_binding_points_is_returned_unchanged(self):
        """Requirement 11.1: pre-feature documents pass through untouched."""
        document = make_document()

        result = resolve_bindings(
            document, {"n1": {"cameraSourceId": "cfg-is-1"}}, {})

        assert result.status == STATUS_RESOLVED
        assert result.document is document
        assert result.missing == ()
        assert result.adapter_assignments == {}

    def test_no_bindings_supplied_returns_document_unchanged(self):
        """Requirement 10.5: compiled-in values run as-is."""
        document = make_document(slots=DEVICE_SLOT)

        for bindings in (None, {}):
            result = resolve_bindings(document, bindings, {})
            assert result.status == STATUS_RESOLVED
            assert result.document is document

    def test_unbound_binding_point_keeps_rendered_defaults(self):
        """A bindings map covering other nodes leaves this point alone."""
        document = make_document(slots=DEVICE_SLOT)
        inventory = {"cfg-is-1": make_inventory_entry()}

        result = resolve_bindings(
            document, {"other": {"cameraSourceId": "cfg-is-1"}}, inventory)

        assert result.status == STATUS_RESOLVED
        element = result.document["segments"][0]["elements"][0]
        assert element["args"]["device"] == "/dev/video0"


class TestResolutionResultShape:
    def test_result_is_frozen(self):
        result = resolve_bindings(make_document(), None, {})
        assert isinstance(result, ResolutionResult)
        try:
            result.status = "changed"
            raised = False
        except AttributeError:
            raised = True
        assert raised
