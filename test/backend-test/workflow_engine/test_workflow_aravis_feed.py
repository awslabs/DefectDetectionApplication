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
"""Unit tests for the Aravis frame feed planner (``plan_aravis_feeds``).

Feature: aravis-camera-input (Requirement 6.4).

``plan_aravis_feeds`` is pure over a compiled document and an optional
``ResolutionResult``: a resolved Aravis assignment's params take
precedence, else the binding point's rendered parameters; a feed with no
usable camera id fails planning attributed to its node; more than one
Aravis binding point violates the single-appsrc Frame_Feed contract;
Aravis-free and legacy documents plan zero feeds.
"""
import pytest

from workflow_engine.aravis_feed import (
    AravisFeed,
    AravisFeedError,
    plan_aravis_feeds,
)
from workflow_engine.camera_binding import (
    STATUS_RESOLVED,
    ResolutionResult,
)


def make_document(*binding_points):
    """A minimal compiled_pipeline.json-shaped document. Passing no
    binding points omits the section entirely (the legacy shape)."""
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
    if binding_points:
        document["bindingPoints"] = list(binding_points)
    return document


def make_aravis_point(node_id="n2", parameters=None):
    """The packager's Aravis binding point shape: aravisBinding marker,
    empty slots, rendered parameter values."""
    if parameters is None:
        parameters = {"camera_id": "Aravis-Fake-GV01",
                      "gain": 4, "exposure": 5000000}
    return {
        "nodeId": node_id,
        "nodeType": "aravis_camera_source",
        "parameters": parameters,
        "slots": [],
        "aravisBinding": True,
    }


def make_resolution(document, aravis_assignments):
    return ResolutionResult(
        document=document,
        status=STATUS_RESOLVED,
        aravis_assignments=aravis_assignments,
    )


class TestAssignmentPrecedence:
    def test_resolved_assignment_params_win_over_rendered_parameters(self):
        """Requirement 6.4: the resolution's aravis_assignments params
        are the effective values when present."""
        document = make_document(make_aravis_point())
        resolution = make_resolution(document, {
            "n2": {"cameraSourceId": "cfg-is-1",
                   "params": {"camera_id": "Basler-12345678",
                              "gain": 20, "exposure": 8000000}},
        })

        feeds = plan_aravis_feeds(document, resolution)

        assert feeds == [AravisFeed(
            node_id="n2", camera_id="Basler-12345678",
            config={"gain": 20, "exposure": 8000000})]

    def test_camera_id_accepted_under_inventory_cameraId_spelling(self):
        """Resolved inventory params spell the identity ``cameraId``;
        the planner accepts either spelling."""
        document = make_document(make_aravis_point())
        resolution = make_resolution(document, {
            "n2": {"cameraSourceId": "cfg-is-1",
                   "params": {"cameraId": "Basler-12345678"}},
        })

        feeds = plan_aravis_feeds(document, resolution)

        assert feeds == [AravisFeed(node_id="n2",
                                    camera_id="Basler-12345678",
                                    config={})]

    def test_config_carries_only_present_acquisition_params(self):
        """gain/exposure join the config only when the effective values
        carry them; None values are treated as absent."""
        document = make_document(make_aravis_point())
        resolution = make_resolution(document, {
            "n2": {"cameraSourceId": "cfg-is-1",
                   "params": {"camera_id": "cam-1", "gain": 12,
                              "exposure": None}},
        })

        feeds = plan_aravis_feeds(document, resolution)

        assert feeds[0].config == {"gain": 12}


class TestRenderedParameterFallback:
    def test_none_resolution_uses_rendered_parameters(self):
        """Requirement 6.4: with no resolution (bindings never resolved)
        the binding point's rendered parameters run."""
        document = make_document(make_aravis_point(
            parameters={"camera_id": "Aravis-Fake-GV01",
                        "gain": 4, "exposure": 5000000}))

        feeds = plan_aravis_feeds(document, None)

        assert feeds == [AravisFeed(
            node_id="n2", camera_id="Aravis-Fake-GV01",
            config={"gain": 4, "exposure": 5000000})]

    def test_resolution_without_assignment_for_node_falls_back(self):
        """An unbound Aravis point (resolution present, no assignment
        for the node) runs on its rendered parameters."""
        document = make_document(make_aravis_point())
        resolution = make_resolution(document, {})

        feeds = plan_aravis_feeds(document, resolution)

        assert feeds == [AravisFeed(
            node_id="n2", camera_id="Aravis-Fake-GV01",
            config={"gain": 4, "exposure": 5000000})]


class TestEmptyCameraIdError:
    def test_empty_rendered_camera_id_fails_naming_the_node(self):
        document = make_document(make_aravis_point(
            parameters={"camera_id": "", "gain": 4}))

        with pytest.raises(AravisFeedError) as excinfo:
            plan_aravis_feeds(document, None)

        assert excinfo.value.node_id == "n2"
        assert "n2" in str(excinfo.value)
        assert "camera_id" in str(excinfo.value)

    def test_missing_camera_id_in_assignment_fails_naming_the_node(self):
        """An assignment whose params carry no camera identity is a
        planning failure attributed to the node (rendered parameters are
        not consulted once an assignment exists)."""
        document = make_document(make_aravis_point())
        resolution = make_resolution(document, {
            "n2": {"cameraSourceId": "cfg-is-1",
                   "params": {"gain": 10}},
        })

        with pytest.raises(AravisFeedError) as excinfo:
            plan_aravis_feeds(document, resolution)

        assert excinfo.value.node_id == "n2"
        assert "n2" in str(excinfo.value)


class TestMultipleAravisPoints:
    def test_two_aravis_points_fail_with_reason_naming_both_nodes(self):
        """The single-frame appsrc Frame_Feed contract supports exactly
        one Aravis camera source per workflow."""
        document = make_document(
            make_aravis_point(node_id="n2"),
            make_aravis_point(node_id="n5",
                              parameters={"camera_id": "cam-b"}),
        )

        with pytest.raises(AravisFeedError) as excinfo:
            plan_aravis_feeds(document, None)

        assert excinfo.value.node_id is None
        message = str(excinfo.value)
        assert "n2" in message
        assert "n5" in message
        assert "2" in message


class TestAravisFreeDocuments:
    def test_legacy_document_without_binding_points_plans_zero_feeds(self):
        """Requirement 6.6: pre-feature documents (no bindingPoints
        section) plan zero feeds."""
        assert plan_aravis_feeds(make_document(), None) == []

    def test_document_with_only_non_aravis_points_plans_zero_feeds(self):
        document = make_document({
            "nodeId": "n1",
            "nodeType": "camera_source",
            "parameters": {"device": "/dev/video0"},
            "slots": [{"param": "device", "segment": 0,
                       "element": 0, "arg": "device"}],
        })

        assert plan_aravis_feeds(document, None) == []

    def test_empty_binding_points_list_plans_zero_feeds(self):
        document = make_document()
        document["bindingPoints"] = []

        assert plan_aravis_feeds(document, None) == []
