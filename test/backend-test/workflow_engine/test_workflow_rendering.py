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
"""Tests for launch-string rendering and element -> node failure mapping
(Requirements 9.2, 9.7)."""

from workflow_engine import rendering


def make_document(segments, executor_bindings=None):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "targetArch": "x86_64",
        "segments": segments,
        "executorBindings": executor_bindings or [],
        "pluginDependencies": [],
    }


#: A branching document: source -> tee, one branch to emltriton
#: inference, one branch to capture. Mirrors the compiler's tee/queue
#: linearization.
BRANCHING_SEGMENTS = [
    {
        "name": "s0",
        "elements": [
            {"nodeId": "n1", "factory": "videotestsrc", "args": {"num-buffers": 1}},
            {"nodeId": None, "factory": "tee", "args": {"name": "t0"}},
        ],
    },
    {
        "name": "s1",
        "from": "t0",
        "elements": [
            {"nodeId": None, "factory": "queue", "args": {}},
            {
                "nodeId": "n2",
                "factory": "emltriton",
                "args": {"model": "widget-anomaly-v3", "qos": False},
            },
            {"nodeId": None, "factory": "fakesink", "args": {}},
        ],
    },
    {
        "name": "s2",
        "from": "t0",
        "elements": [
            {"nodeId": None, "factory": "queue", "args": {}},
            {"nodeId": "n3", "factory": "jpegenc", "args": {}},
            {"nodeId": "n3", "factory": "fakesink", "args": {}},
        ],
    },
]


class TestRenderLaunchString:
    def test_element_args_and_bool_rendering(self):
        element = {
            "nodeId": "n1",
            "factory": "emltriton",
            "args": {"model": "m1", "qos": False, "batch": 4},
        }
        assert rendering.render_element(element) == "emltriton model=m1 qos=false batch=4"

    def test_segments_joined_with_tee_branch_references(self):
        document = make_document(BRANCHING_SEGMENTS)
        assert rendering.render_launch_string(document) == (
            "videotestsrc num-buffers=1 ! tee name=t0 "
            "t0. ! queue ! emltriton model=widget-anomaly-v3 qos=false ! fakesink "
            "t0. ! queue ! jpegenc ! fakesink"
        )

    def test_funnel_link_suffix(self):
        segment = {
            "name": "s0",
            "linkTo": "f0",
            "elements": [{"nodeId": "n1", "factory": "videotestsrc", "args": {}}],
        }
        assert rendering.render_segment(segment) == "videotestsrc ! f0."

    def test_empty_segments_are_skipped(self):
        document = make_document(
            [
                {"name": "s0", "elements": []},
                {
                    "name": "s1",
                    "elements": [
                        {"nodeId": "n1", "factory": "videotestsrc", "args": {}}
                    ],
                },
            ]
        )
        assert rendering.render_launch_string(document) == "videotestsrc"

    def test_document_without_segments_renders_empty(self):
        assert rendering.render_launch_string(make_document([])) == ""


class TestElementNameMap:
    def test_per_factory_counters_and_explicit_names(self):
        document = make_document(BRANCHING_SEGMENTS)
        name_map = rendering.element_name_map(document)
        assert name_map == {
            "videotestsrc0": "n1",
            "t0": None,  # explicit name= overrides the auto name
            "queue0": None,
            "emltriton0": "n2",
            "fakesink0": None,
            "queue1": None,
            "jpegenc0": "n3",
            "fakesink1": "n3",
        }

    def test_node_id_for_element(self):
        name_map = rendering.element_name_map(make_document(BRANCHING_SEGMENTS))
        assert rendering.node_id_for_element(name_map, "emltriton0") == "n2"
        assert rendering.node_id_for_element(name_map, "queue0") is None
        assert rendering.node_id_for_element(name_map, "nonexistent") is None


class TestFailingNodeIdFromError:
    def setup_method(self):
        self.name_map = rendering.element_name_map(
            make_document(BRANCHING_SEGMENTS)
        )

    def test_maps_gstreamer_object_path_to_node(self):
        # The shape GstPipelineManager produces: error message + debug text
        error = (
            "Pipeline failed with: Could not configure Triton. "
            "gstemltriton.cc(412): gst_emltriton_start (): "
            "/GstPipeline:pipeline0/GstEmltriton:emltriton0:\n"
            "server unreachable"
        )
        assert (
            rendering.failing_node_id_from_error(self.name_map, error) == "n2"
        )

    def test_falls_back_to_element_name_token(self):
        error = "Pipeline failed with: element jpegenc0 reported a failure."
        assert (
            rendering.failing_node_id_from_error(self.name_map, error) == "n3"
        )

    def test_synthetic_element_yields_none(self):
        error = (
            "Pipeline failed with: streaming stopped. "
            "/GstPipeline:pipeline0/GstQueue:queue0:"
        )
        assert rendering.failing_node_id_from_error(self.name_map, error) is None

    def test_unidentifiable_error_yields_none(self):
        assert (
            rendering.failing_node_id_from_error(
                self.name_map, "Pipeline timed out after 120s"
            )
            is None
        )
        assert rendering.failing_node_id_from_error(self.name_map, None) is None
        assert rendering.failing_node_id_from_error(self.name_map, "") is None
