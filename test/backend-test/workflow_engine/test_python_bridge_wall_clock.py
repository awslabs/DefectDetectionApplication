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
"""Per-node wall clock for bridged Custom Python nodes.

The catalog's ``wall_clock_limit_s`` parameter compiles onto the
``emlpython`` element as ``wall-clock-limit-sec``. ``bridge_specs`` reads
it into ``BridgeSpec.wall_clock_limit_sec`` (None when absent, not
numeric or not positive) and ``build_bridges`` applies it per bridge,
falling back to its caller's default. ``rewrite_document`` drops the
element's arguments with the element itself, so launch strings of every
workflow stay byte-identical whatever the value.
"""

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH
from workflow_engine import rendering
from workflow_engine.python_bridge import (
    DEFAULT_WALL_CLOCK_LIMIT_SEC,
    WALL_CLOCK_LIMIT_ARG,
    BridgeSpec,
    bridge_specs,
    build_bridges,
    rewrite_document,
)

NODE_ID = "pynode"


def _document(extra_args=None):
    args = {"handler-path": "python/pynode/handler.py"}
    args.update(extra_args or {})
    return {
        "schemaVersion": 1,
        "segments": [
            {
                "name": "s0",
                "elements": [
                    {"nodeId": "n1", "factory": "videotestsrc",
                     "args": {"num-buffers": 1}},
                    {"nodeId": NODE_ID, "factory": "emlpython", "args": args},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }
        ],
        "executorBindings": [],
    }


class TestBridgeSpecs:
    def test_absent_argument_leaves_the_limit_unset(self):
        (spec,) = bridge_specs(_document())
        assert spec.wall_clock_limit_sec is None
        assert spec == BridgeSpec(
            node_id=NODE_ID, handler_path="python/pynode/handler.py",
            sink_name="py_in_pynode", src_name="py_out_pynode",
        )

    def test_integer_argument_is_read_as_seconds(self):
        (spec,) = bridge_specs(_document({WALL_CLOCK_LIMIT_ARG: 18}))
        assert spec.wall_clock_limit_sec == 18.0

    def test_numeric_string_argument_is_read_as_seconds(self):
        (spec,) = bridge_specs(_document({WALL_CLOCK_LIMIT_ARG: "18"}))
        assert spec.wall_clock_limit_sec == 18.0

    @pytest.mark.parametrize("raw", [0, -3, "x", "", None, True])
    def test_non_positive_or_unparseable_argument_is_ignored(self, raw):
        (spec,) = bridge_specs(_document({WALL_CLOCK_LIMIT_ARG: raw}))
        assert spec.wall_clock_limit_sec is None


class TestBuildBridges:
    def test_default_limit_without_the_argument(self, tmp_path):
        (bridge,) = build_bridges(bridge_specs(_document()), str(tmp_path))
        assert bridge._wall_clock_limit_sec == DEFAULT_WALL_CLOCK_LIMIT_SEC == 10.0

    def test_spec_limit_overrides_the_default(self, tmp_path):
        (bridge,) = build_bridges(
            bridge_specs(_document({WALL_CLOCK_LIMIT_ARG: 18})), str(tmp_path))
        assert bridge._wall_clock_limit_sec == 18.0

    def test_spec_limit_overrides_an_explicit_caller_default(self, tmp_path):
        (bridge,) = build_bridges(
            bridge_specs(_document({WALL_CLOCK_LIMIT_ARG: 18})), str(tmp_path),
            wall_clock_limit_sec=5.0)
        assert bridge._wall_clock_limit_sec == 18.0

    def test_ignored_argument_falls_back_to_the_caller_default(self, tmp_path):
        (bridge,) = build_bridges(
            bridge_specs(_document({WALL_CLOCK_LIMIT_ARG: 0})), str(tmp_path),
            wall_clock_limit_sec=5.0)
        assert bridge._wall_clock_limit_sec == 5.0


class TestLaunchStringsUnchanged:
    def test_rewritten_document_carries_no_wall_clock_argument(self):
        rewritten = rewrite_document(_document({WALL_CLOCK_LIMIT_ARG: 18}))
        for segment in rewritten["segments"]:
            for element in segment["elements"]:
                assert element["factory"] != "emlpython"
                assert WALL_CLOCK_LIMIT_ARG not in (element.get("args") or {})

    def test_launch_string_identical_with_and_without_the_argument(self):
        with_arg = rendering.render_launch_string(
            rewrite_document(_document({WALL_CLOCK_LIMIT_ARG: 18})))
        without = rendering.render_launch_string(rewrite_document(_document()))
        assert with_arg == without
        assert WALL_CLOCK_LIMIT_ARG not in with_arg
