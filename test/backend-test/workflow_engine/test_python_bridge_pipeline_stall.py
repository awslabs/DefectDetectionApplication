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
"""Regression tests for the Custom Python workflow pipeline stall.

Production incident (jetson-thor1, LocalServer.arm64JP7, 2026-08-26):
every workflow containing a Custom Python node timed out after 120 s
with "no EOS/ERROR received" (executions of both `IMTS - Swagfactory`
workflows). Interrogating the stalled pipeline on-device showed FOUR
stacked defects:

1. PREROLL DEADLOCK - the pipeline wedged in the PAUSED->PLAYING async
   transition: both bridge appsinks held UN-PULLED preroll samples
   (only ``new-sample`` was connected, which fires in PLAYING), while
   sinks downstream of the bridge appsrcs could not preroll until the
   bridge pumped. Fix: pump on ``new-preroll`` too, deduping the buffer
   that is re-delivered as the first ``new-sample``.
2. UNCONSTRAINED APPSINK NEGOTIATION - the bridge appsink negotiated
   RGBA64_LE caps over a 1-byte/pixel Bayer buffer; Bayer sources
   delivered RGBx, rejected by the handler runtime's FORMAT_CHANNELS
   before user code ran. Fix: pin ``caps=video/x-raw,format=RGB``.
3. DANGLING TERMINAL APPSRC - a Custom Python node whose only consumers
   are executor bindings (mqtt_publish) left its ``py_out_*`` appsrc
   unlinked; pushing into it failed the pipeline with "Internal data
   stream error". Fix: terminate the trailing part with a fakesink.
4. (compiler) jpegenc got RGBx on the capture branches and errored with
   "Output state was not configured". Fix: I420 capsfilter - covered by
   the workflow_core compiler tests, not this file.

The full fix combination was validated GREEN on jetson-thor1 with the
deployed v3 handlers before these tests were written: the previously
120 s-stalling topology completed in 1.4 s writing both capture JPEGs
(harness: .kiro/harness/defectB_fix_validation.py).
"""

from workflow_engine.python_bridge import (
    BRIDGE_APPSINK_CAPS,
    _appsink_element,
    _split_segment,
    rewrite_document,
)
from workflow_engine.rendering import render_launch_string


EMLPYTHON = {
    "nodeId": "pynode",
    "factory": "emlpython",
    "args": {"handler-path": "python/pynode/handler.py"},
}


class TestBridgeAppsinkCaps:
    def test_appsink_pins_rgb_caps(self):
        args = _appsink_element("pynode")["args"]
        assert args["caps"] == BRIDGE_APPSINK_CAPS == "video/x-raw,format=RGB"

    def test_pre_fix_args_preserved(self):
        # The pre-fix properties are unchanged (name, emit-signals, sync,
        # max-buffers) - caps is purely additive.
        args = _appsink_element("pynode")["args"]
        assert args["name"] == "py_in_pynode"
        assert args["emit-signals"] is True
        assert args["sync"] is False
        assert args["max-buffers"] == 1

    def test_caps_render_unquoted_in_launch(self):
        # ``video/x-raw,format=RGB`` carries no launch-unsafe characters,
        # so it must render bare - the exact form validated on-device.
        document = {
            "segments": [{
                "name": "s0",
                "elements": [
                    {"nodeId": None, "factory": "videotestsrc", "args": {}},
                    EMLPYTHON,
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }],
            "executorBindings": [],
        }
        launch = render_launch_string(rewrite_document(document))
        assert "caps=video/x-raw,format=RGB" in launch
        assert 'caps="' not in launch


class TestTrailingAppsrcTermination:
    def test_trailing_bridge_appsrc_gains_fakesink(self):
        # A Custom Python node with no downstream GStreamer consumer
        # (the IMTS shape: custom_python -> mqtt_publish only).
        segment = {
            "name": "s0",
            "elements": [
                {"nodeId": None, "factory": "videotestsrc", "args": {}},
                EMLPYTHON,
            ],
        }
        parts = _split_segment(segment)
        trailing = parts[-1]["elements"]
        assert trailing[0]["factory"] == "appsrc"
        assert trailing[-1] == {
            "nodeId": None, "factory": "fakesink", "args": {"sync": False},
        }

    def test_linked_appsrc_is_not_terminated(self):
        # A bridge feeding a funnel keeps its linkTo and gains nothing.
        segment = {
            "name": "s0",
            "elements": [
                {"nodeId": None, "factory": "videotestsrc", "args": {}},
                EMLPYTHON,
            ],
            "linkTo": "f0",
        }
        parts = _split_segment(segment)
        assert parts[-1]["linkTo"] == "f0"
        assert parts[-1]["elements"][-1]["factory"] == "appsrc"

    def test_mid_segment_bridge_is_not_terminated(self):
        # emlpython followed by real elements: the appsrc is linked to
        # them, no fakesink appears.
        segment = {
            "name": "s0",
            "elements": [
                {"nodeId": None, "factory": "videotestsrc", "args": {}},
                EMLPYTHON,
                {"nodeId": None, "factory": "videoconvert", "args": {}},
                {"nodeId": None, "factory": "fakesink", "args": {}},
            ],
        }
        parts = _split_segment(segment)
        factories = [e["factory"] for e in parts[-1]["elements"]]
        assert factories == ["appsrc", "videoconvert", "fakesink"]

    def test_non_bridge_trailing_appsrc_untouched(self):
        # Only py_out_* appsrcs are terminated; a workflow-authored
        # appsrc keeps today's shape.
        segment = {
            "name": "s0",
            "elements": [
                {"nodeId": "n1", "factory": "appsrc",
                 "args": {"name": "customsrc"}},
            ],
        }
        parts = _split_segment(segment)
        assert [e["factory"] for e in parts[-1]["elements"]] == ["appsrc"]

    def test_rendered_trailing_form(self):
        document = {
            "segments": [{
                "name": "s0",
                "elements": [
                    {"nodeId": None, "factory": "videotestsrc", "args": {}},
                    EMLPYTHON,
                ],
            }],
            "executorBindings": [],
        }
        launch = render_launch_string(rewrite_document(document))
        assert launch.endswith(
            "appsrc name=py_out_pynode is-live=true format=time block=true "
            "! fakesink sync=false"
        )


class TestBothSignalsConnected:
    """The shipped source must pump on BOTH new-preroll and new-sample.

    ``make_bridge_handlers`` is nested inside ``run_bridged_pipeline``
    (it closes over the GLib loop), so the handler pair cannot be
    imported; driving the real pair end-to-end needs a GStreamer
    pipeline, which was done on jetson-thor1 (module docstring). What
    a host test CAN pin without weakening anything: the source both
    defines the preroll handler and connects both signals, so the
    deadlock cannot be silently reintroduced by dropping either half.
    """

    def _source(self):
        import inspect
        import workflow_engine.python_bridge as pb
        return inspect.getsource(pb.run_bridged_pipeline)

    def test_preroll_handler_defined_and_connected(self):
        src = self._source()
        assert 'sink.connect("new-preroll", on_preroll)' in src
        assert 'sink.connect("new-sample", on_sample)' in src
        assert 'sink.emit("pull-preroll")' in src

    def test_pump_dedups_on_buffer_identity(self):
        # The same buffer is delivered on new-preroll (PAUSED) and again
        # as the first new-sample (PLAYING); identity is (pts, offset,
        # size) and must be checked before the buffer is mapped.
        src = self._source()
        assert "(buffer.pts, buffer.offset, buffer.get_size())" in src
        assert "if identity in pumped_ids" in src


class TestRewriteDocumentShape:
    def test_document_without_emlpython_unchanged(self):
        document = {
            "segments": [{
                "name": "s0",
                "elements": [
                    {"nodeId": None, "factory": "videotestsrc", "args": {}},
                    {"nodeId": None, "factory": "fakesink", "args": {}},
                ],
            }],
            "executorBindings": [],
        }
        assert rewrite_document(document)["segments"] == document["segments"]

    def test_two_bridges_both_rewritten_with_caps(self):
        second = {
            "nodeId": "py2",
            "factory": "emlpython",
            "args": {"handler-path": "python/py2/handler.py"},
        }
        document = {
            "segments": [{
                "name": "s0",
                "elements": [
                    {"nodeId": None, "factory": "videotestsrc", "args": {}},
                    EMLPYTHON,
                    second,
                ],
            }],
            "executorBindings": [],
        }
        launch = render_launch_string(rewrite_document(document))
        assert launch.count("caps=video/x-raw,format=RGB") == 2
        # Both trailing py_out appsrcs: only the LAST is trailing; the
        # first feeds the second bridge's appsink part.
        assert launch.count("! fakesink sync=false") == 1
