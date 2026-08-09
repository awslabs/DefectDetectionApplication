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
"""Unit tests for the Custom Python source planner
(``plan_python_sources``).

Feature: custom-python-source (Requirements 7.3, 8.5).

``plan_python_sources`` is pure over a compiled document: binding points
marked ``pythonSourceBinding: true`` plan one ``PythonSourceFeed`` with
the artifact-relative handler path and the parsed
``allowed_uri_prefixes``; documents with no such points — including
pre-feature documents with no ``bindingPoints`` and documents with only
Aravis/camera points — plan zero Python sources; more than one fed
source across the union of ``pythonSourceBinding`` and ``aravisBinding``
points fails planning naming every offending node.
"""
import pytest

from workflow_engine.python_source import (
    PythonSourceFeed,
    PythonSourceError,
    plan_python_sources,
)


def make_document(*binding_points):
    """A minimal compiled_pipeline.json-shaped document. Passing no
    binding points omits the section entirely (the pre-feature shape)."""
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


def make_python_point(node_id="n2", prefixes=""):
    """The packager's Python source binding point shape (task 3.1):
    pythonSourceBinding marker, empty slots, the rendered
    allowed_uri_prefixes parameter."""
    return {
        "nodeId": node_id,
        "nodeType": "custom_python_source",
        "pythonSourceBinding": True,
        "parameters": {"allowed_uri_prefixes": prefixes},
        "slots": [],
    }


def make_aravis_point(node_id="n3"):
    """The packager's Aravis binding point shape."""
    return {
        "nodeId": node_id,
        "nodeType": "aravis_camera_source",
        "parameters": {"camera_id": "Aravis-Fake-GV01"},
        "slots": [],
        "aravisBinding": True,
    }


def make_camera_point(node_id="n4"):
    """A non-feed binding point of another camera family."""
    return {
        "nodeId": node_id,
        "nodeType": "camera_source",
        "parameters": {"device": "/dev/video0"},
        "slots": [{"param": "device", "segment": 0,
                   "element": 0, "arg": "device"}],
    }


class TestZeroSourceDocuments:
    """Requirement 7.3: no Python source points plan zero producers."""

    def test_empty_document_plans_zero_feeds(self):
        assert plan_python_sources({}) == []

    def test_document_without_binding_points_plans_zero_feeds(self):
        """Pre-feature documents carry no bindingPoints section."""
        assert plan_python_sources(make_document()) == []

    def test_empty_binding_points_plan_zero_feeds(self):
        document = make_document()
        document["bindingPoints"] = []
        assert plan_python_sources(document) == []

    def test_non_dict_document_plans_zero_feeds(self):
        assert plan_python_sources(None) == []

    def test_aravis_only_document_plans_zero_python_sources(self):
        """A single Aravis point belongs to plan_aravis_feeds."""
        document = make_document(make_aravis_point())
        assert plan_python_sources(document) == []

    def test_camera_only_document_plans_zero_python_sources(self):
        document = make_document(make_camera_point())
        assert plan_python_sources(document) == []


class TestSinglePythonSource:
    def test_single_point_plans_one_feed_with_handler_path(self):
        document = make_document(make_python_point(node_id="src-1"))
        feeds = plan_python_sources(document)
        assert feeds == [PythonSourceFeed(
            node_id="src-1",
            handler_path="python/src-1/handler.py",
            allowed_uri_prefixes=(),
        )]

    def test_prefixes_are_newline_split_stripped_and_empties_dropped(self):
        document = make_document(make_python_point(
            node_id="src-1",
            prefixes="  s3://plant-images/  \n\n   \nhttps://mes.local/\n"))
        (feed,) = plan_python_sources(document)
        assert feed.allowed_uri_prefixes == (
            "s3://plant-images/", "https://mes.local/")

    def test_empty_prefix_parameter_yields_empty_tuple(self):
        document = make_document(make_python_point(prefixes=""))
        (feed,) = plan_python_sources(document)
        assert feed.allowed_uri_prefixes == ()

    def test_missing_prefix_parameter_yields_empty_tuple(self):
        point = make_python_point()
        point["parameters"] = {}
        (feed,) = plan_python_sources(make_document(point))
        assert feed.allowed_uri_prefixes == ()

    def test_missing_parameters_section_yields_empty_tuple(self):
        point = make_python_point()
        del point["parameters"]
        (feed,) = plan_python_sources(make_document(point))
        assert feed.allowed_uri_prefixes == ()

    def test_non_feed_points_are_ignored(self):
        document = make_document(
            make_camera_point(), make_python_point(node_id="src-1"))
        (feed,) = plan_python_sources(document)
        assert feed.node_id == "src-1"


class TestMultiFedSourceRejection:
    """Requirement 8.5: >1 fed source across the union of markers."""

    def test_two_python_points_fail_naming_both_nodes(self):
        document = make_document(
            make_python_point(node_id="src-1"),
            make_python_point(node_id="src-2"))
        with pytest.raises(PythonSourceError) as excinfo:
            plan_python_sources(document)
        assert excinfo.value.node_id is None
        assert "'src-1'" in str(excinfo.value)
        assert "'src-2'" in str(excinfo.value)

    def test_python_plus_aravis_fails_naming_both_nodes(self):
        document = make_document(
            make_python_point(node_id="src-1"),
            make_aravis_point(node_id="cam-1"))
        with pytest.raises(PythonSourceError) as excinfo:
            plan_python_sources(document)
        assert excinfo.value.node_id is None
        assert "'src-1'" in str(excinfo.value)
        assert "'cam-1'" in str(excinfo.value)

    def test_two_aravis_points_fail_naming_both_nodes(self):
        """The union count covers Aravis-only violations too."""
        document = make_document(
            make_aravis_point(node_id="cam-1"),
            make_aravis_point(node_id="cam-2"))
        with pytest.raises(PythonSourceError) as excinfo:
            plan_python_sources(document)
        assert excinfo.value.node_id is None
        assert "'cam-1'" in str(excinfo.value)
        assert "'cam-2'" in str(excinfo.value)
