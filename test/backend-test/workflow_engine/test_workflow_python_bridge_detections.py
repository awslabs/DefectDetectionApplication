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
"""Tests for the bridge pump's detections injection
(detection-guided-bedrock-inspection Requirement 1.10, design Decision 1b).

Custom nodes stream-downstream of ``model_inference`` (segment element
order in the compiled document) get ``metadata["detections"]`` /
``metadata["detection_count"]`` — Detection_IDs included — injected
before frame dispatch, built via the shared run-state cache so the
handler and every post-pipeline consumer see identical Detection_IDs
(design Property 1). Nodes not downstream see byte-identical metadata;
budget exhaustion is a degraded path (keys absent, run-log warning),
never a failure.

Handler-facing tests run real handler subprocesses through
:class:`CustomPythonBridge`, following ``test_workflow_python_bridge``;
the topology tests are pure functions over the compiled document; the
detections data comes from the real thor1 capture-record fixtures
(``test_workflow_detections``).
"""
import inspect
import logging
import os
import re
import shutil
import threading
import time

from workflow_engine import detections
from workflow_engine.python_bridge import (
    DETECTIONS_POLL_BUDGET_SEC,
    DETECTIONS_POLL_INTERVAL_SEC,
    CustomPythonBridge,
    DetectionsInjector,
    detection_downstream_node_ids,
    run_bridged_pipeline,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

#: The real thor1 record with three "blue box" detections.
THOR1_CAPTURE_ID = "515zkeve-326caa317dc4474cad2db8c2f69095ac"
#: The real thor1 zero-object record ({"detections": {}}).
THOR1_EMPTY_CAPTURE_ID = "515zkeve-5267106cd0874b339818519b351e26f5"

CAPTURE_ID = "run-capture-1"
NODE_ID = "pynode"
OTHER_NODE_ID = "othernode"

#: Generous per-frame limit: the first frame of each subprocess pays
#: the interpreter start-up cost (the existing bridge test convention).
WALL_CLOCK_LIMIT_SEC = 30.0

#: Echoes what the handler observed in its metadata argument back to
#: the executor, so the tests assert on the handler's view.
METADATA_ECHO_HANDLER = """\
def handle(frame, metadata):
    return frame, {
        "echo_keys": sorted(metadata.keys()),
        "echo_detections": metadata.get("detections"),
        "echo_count": metadata.get("detection_count"),
    }
"""


def write_handler(tmp_path, code, name="handler.py"):
    path = os.path.join(str(tmp_path), name)
    with open(path, "w") as f:
        f.write(code)
    return path


def make_bridge(handler_path, node_id=NODE_ID):
    return CustomPythonBridge(
        node_id, handler_path, wall_clock_limit_sec=WALL_CLOCK_LIMIT_SEC
    )


def make_output_dir(tmp_path, fixture_capture_id=THOR1_CAPTURE_ID):
    """A per-test output_dir carrying the real thor1 capture record
    under this run's ``CAPTURE_ID``."""
    output_dir = os.path.join(str(tmp_path), "captures")
    os.makedirs(output_dir, exist_ok=True)
    land_record(output_dir, fixture_capture_id)
    return output_dir


def land_record(output_dir, fixture_capture_id=THOR1_CAPTURE_ID):
    shutil.copyfile(
        os.path.join(FIXTURES_DIR, fixture_capture_id + ".jsonl"),
        os.path.join(output_dir, CAPTURE_ID + ".jsonl"),
    )


def make_injector(output_dir, downstream=(NODE_ID,), cache=None, **kwargs):
    kwargs.setdefault("poll_budget_sec", 0.5)
    kwargs.setdefault("poll_interval_sec", 0.01)
    return DetectionsInjector(
        downstream, output_dir, CAPTURE_ID, cache=cache, **kwargs
    )


# ---------------------------------------------------------------------------
# Downstream topology from segment element order (pure functions)
# ---------------------------------------------------------------------------


def element(factory, node_id=None, **args):
    return {"nodeId": node_id, "factory": factory, "args": args}


def emltriton(node_id="model_1"):
    return element("emltriton", node_id, model="m")


def emlpython(node_id):
    return element(
        "emlpython", node_id,
        **{"handler-path": "python/{0}/handler.py".format(node_id)}
    )


def make_document(segments):
    return {"segments": segments, "executorBindings": []}


class TestDetectionDownstreamNodeIds:
    def test_bridge_after_model_in_one_segment_is_downstream(self):
        document = make_document([
            {"name": "s0", "elements": [
                element("videotestsrc"), emltriton(), emlpython(NODE_ID),
                element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == {NODE_ID}

    def test_bridge_before_model_is_not_downstream(self):
        # A custom_python_preprocess shape: the bridge feeds the model.
        document = make_document([
            {"name": "s0", "elements": [
                element("videotestsrc"), emlpython(NODE_ID), emltriton(),
                element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == frozenset()

    def test_tee_branch_after_model_is_downstream(self):
        document = make_document([
            {"name": "s0", "elements": [
                element("videotestsrc"), emltriton(),
                element("tee", name="t0"),
                element("fakesink"),
            ]},
            {"name": "s1", "from": "t0", "elements": [
                element("queue"), emlpython(NODE_ID), element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == {NODE_ID}

    def test_tee_branch_before_model_is_not_downstream(self):
        document = make_document([
            {"name": "s0", "elements": [
                element("videotestsrc"), element("tee", name="t0"),
                emltriton(), element("fakesink"),
            ]},
            {"name": "s1", "from": "t0", "elements": [
                element("queue"), emlpython(NODE_ID), element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == frozenset()

    def test_segment_order_in_the_document_does_not_matter(self):
        # The downstream branch listed BEFORE the segment carrying the
        # tee it hangs off: the fixpoint walk still finds it.
        document = make_document([
            {"name": "s1", "from": "t0", "elements": [
                element("queue"), emlpython(NODE_ID), element("fakesink"),
            ]},
            {"name": "s0", "elements": [
                element("videotestsrc"), emltriton(),
                element("tee", name="t0"),
                element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == {NODE_ID}

    def test_funnel_fed_by_a_downstream_segment_is_downstream(self):
        document = make_document([
            {"name": "s0", "linkTo": "f0", "elements": [
                element("videotestsrc"), emltriton(),
            ]},
            {"name": "s1", "elements": [
                element("funnel", name="f0"), emlpython(NODE_ID),
                element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == {NODE_ID}

    def test_funnel_fed_only_upstream_of_the_model_is_not_downstream(self):
        document = make_document([
            {"name": "s0", "linkTo": "f0", "elements": [
                element("videotestsrc"),
            ]},
            {"name": "s1", "elements": [
                element("funnel", name="f0"), emlpython(NODE_ID),
                emltriton(), element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == frozenset()

    def test_mixed_document_separates_the_two_bridges(self):
        document = make_document([
            {"name": "s0", "elements": [
                element("videotestsrc"), emlpython(OTHER_NODE_ID),
                emltriton(), emlpython(NODE_ID), element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == {NODE_ID}

    def test_empty_and_model_less_documents_yield_nothing(self):
        assert detection_downstream_node_ids({}) == frozenset()
        document = make_document([
            {"name": "s0", "elements": [
                element("videotestsrc"), emlpython(NODE_ID),
                element("fakesink"),
            ]},
        ])
        assert detection_downstream_node_ids(document) == frozenset()


# ---------------------------------------------------------------------------
# Injection for downstream nodes (real handler subprocesses)
# ---------------------------------------------------------------------------


class TestInjectionForDownstreamNodes:
    def test_handler_sees_the_detection_list_with_ids(self, tmp_path):
        output_dir = make_output_dir(tmp_path)
        injector = make_injector(output_dir)
        metadata = injector.metadata_for(NODE_ID)

        bridge = make_bridge(write_handler(tmp_path, METADATA_ECHO_HANDLER))
        try:
            _, out_meta = bridge.process_frame(b"frame", metadata=metadata)
        finally:
            bridge.stop()

        # The handler runtime adds "frame" (caps info) to every dispatch;
        # the injected keys ride alongside it (Requirement 1.10).
        assert out_meta["echo_keys"] == [
            "detection_count", "detections", "frame",
        ]
        assert out_meta["echo_count"] == 3
        echoed = out_meta["echo_detections"]
        assert len(echoed) == 3
        for entry in echoed:
            assert set(entry) == {
                "id", "label", "confidence",
                "x_min", "y_min", "x_max", "y_max",
            }
            # Detection_IDs included, in the uuid4().hex[:8] shape.
            assert re.fullmatch(r"[0-9a-f]{8}", entry["id"])
        assert len({entry["id"] for entry in echoed}) == 3

    def test_zero_detections_inject_an_empty_list_with_count_zero(
        self, tmp_path
    ):
        # The marshal's real zero-object record: "ran with no
        # detections" is distinguishable from "no detection model".
        output_dir = make_output_dir(
            tmp_path, fixture_capture_id=THOR1_EMPTY_CAPTURE_ID
        )
        metadata = make_injector(output_dir).metadata_for(NODE_ID)
        assert metadata == {"detections": [], "detection_count": 0}

    def test_record_landing_mid_poll_is_injected(self, tmp_path):
        # The marshal writes during buffer processing, possibly async
        # (design Risk 2): a record landing inside the poll budget is
        # picked up by the 50 ms-interval loop.
        output_dir = os.path.join(str(tmp_path), "captures")
        os.makedirs(output_dir)
        timer = threading.Timer(0.3, land_record, args=(output_dir,))
        timer.start()
        try:
            metadata = make_injector(
                output_dir, poll_budget_sec=5.0
            ).metadata_for(NODE_ID)
        finally:
            timer.cancel()
        assert metadata["detection_count"] == 3
        assert len(metadata["detections"]) == 3


# ---------------------------------------------------------------------------
# Absence for non-downstream nodes (byte-identical metadata)
# ---------------------------------------------------------------------------


class TestAbsenceForNonDownstreamNodes:
    def test_non_downstream_node_gets_the_empty_dict(self, tmp_path):
        # Even with a readable record on disk: a node not downstream of
        # model_inference never polls and never sees the keys.
        output_dir = make_output_dir(tmp_path)
        injector = make_injector(output_dir, downstream=(NODE_ID,))
        assert injector.metadata_for(OTHER_NODE_ID) == {}
        # No build happened on the other node's behalf.
        assert injector.cache == {}

    def test_non_downstream_handler_view_matches_todays_dispatch(
        self, tmp_path
    ):
        output_dir = make_output_dir(tmp_path)
        injector = make_injector(output_dir, downstream=(NODE_ID,))

        bridge = make_bridge(
            write_handler(tmp_path, METADATA_ECHO_HANDLER),
            node_id=OTHER_NODE_ID,
        )
        try:
            _, injected_view = bridge.process_frame(
                b"frame", metadata=injector.metadata_for(OTHER_NODE_ID)
            )
            _, todays_view = bridge.process_frame(b"frame", metadata={})
        finally:
            bridge.stop()

        # Byte-identical to today's dispatch: only the runtime's own
        # "frame" key, no detections keys.
        assert injected_view == todays_view
        assert injected_view["echo_keys"] == ["frame"]
        assert injected_view["echo_detections"] is None
        assert injected_view["echo_count"] is None


# ---------------------------------------------------------------------------
# Budget exhaustion: degraded path, never a failure
# ---------------------------------------------------------------------------


class TestBudgetExhaustion:
    def test_keys_absent_and_warning_logged(self, tmp_path, caplog):
        output_dir = str(tmp_path)  # no record ever lands
        injector = make_injector(output_dir, poll_budget_sec=0.2)
        with caplog.at_level(
            logging.WARNING, logger="workflow_engine.python_bridge"
        ):
            started = time.monotonic()
            metadata = injector.metadata_for(NODE_ID)
            elapsed = time.monotonic() - started
        assert metadata == {}
        assert elapsed >= 0.2  # the budget was actually spent
        warnings = [
            record for record in caplog.records
            if "poll budget" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert NODE_ID in warnings[0].getMessage()

    def test_subsequent_frames_do_not_respend_the_budget(
        self, tmp_path, caplog
    ):
        output_dir = str(tmp_path)
        injector = make_injector(output_dir, poll_budget_sec=0.2)
        with caplog.at_level(
            logging.WARNING, logger="workflow_engine.python_bridge"
        ):
            injector.metadata_for(NODE_ID)
            started = time.monotonic()
            metadata = injector.metadata_for(NODE_ID)
            elapsed = time.monotonic() - started
        assert metadata == {}
        assert elapsed < 0.2  # no second poll loop
        warnings = [
            record for record in caplog.records
            if "poll budget" in record.getMessage()
        ]
        assert len(warnings) == 1  # warned once, not per frame

    def test_exhaustion_never_raises_through_the_dispatch(self, tmp_path):
        # Degraded, not failed: the frame still dispatches and the
        # handler runs with the keys absent (Requirement 1.10).
        injector = make_injector(str(tmp_path), poll_budget_sec=0.1)
        metadata = injector.metadata_for(NODE_ID)
        bridge = make_bridge(write_handler(tmp_path, METADATA_ECHO_HANDLER))
        try:
            out_frame, out_meta = bridge.process_frame(
                b"frame", metadata=metadata
            )
        finally:
            bridge.stop()
        assert out_frame == b"frame"
        assert out_meta["echo_keys"] == ["frame"]


# ---------------------------------------------------------------------------
# Cache identity with the post-pipeline merge (design Property 1 tie-in)
# ---------------------------------------------------------------------------


class TestCacheIdentityWithPostPipelineMerge:
    def test_pump_and_post_pipeline_consumers_share_ids(self, tmp_path):
        output_dir = make_output_dir(tmp_path)
        cache = {}  # the executor's run-state cache, shared by design

        injector = make_injector(output_dir, cache=cache)
        pump_metadata = injector.metadata_for(NODE_ID)

        # The handler's view of the IDs, through a real subprocess.
        bridge = make_bridge(write_handler(tmp_path, METADATA_ECHO_HANDLER))
        try:
            _, out_meta = bridge.process_frame(
                b"frame", metadata=pump_metadata
            )
        finally:
            bridge.stop()

        # Remove the record: the post-pipeline merge MUST come from the
        # shared cache, not a rebuild (which would redraw the IDs).
        os.remove(os.path.join(output_dir, CAPTURE_ID + ".jsonl"))

        tag_values = {}
        built = detections.merge_detections(
            tag_values, output_dir, CAPTURE_ID, None, cache
        )
        assert built is not None
        # Same list object on the run state -> identical entries and
        # Detection_IDs for the pump and the post-pipeline consumers.
        assert tag_values["detections"] is pump_metadata["detections"]
        assert tag_values["detection_count"] == (
            pump_metadata["detection_count"]
        )
        assert [entry["id"] for entry in out_meta["echo_detections"]] == [
            entry["id"] for entry in tag_values["detections"]
        ]

    def test_a_prefilled_cache_is_reused_without_reading_disk(self, tmp_path):
        # The post-pipeline merge may run first (or another node's poll
        # already built the list): the pump serves the cached entries
        # even when the record is unreadable.
        prebuilt = [{
            "id": "aabbccdd", "label": "blue box", "confidence": 0.9,
            "x_min": 1.0, "y_min": 2.0, "x_max": 3.0, "y_max": 4.0,
        }]
        cache = {detections.CACHE_KEY_DETECTIONS: prebuilt}
        injector = make_injector(str(tmp_path), cache=cache)
        metadata = injector.metadata_for(NODE_ID)
        assert metadata["detections"] is prebuilt
        assert metadata["detection_count"] == 1


# ---------------------------------------------------------------------------
# Pump wiring and defaults (the GStreamer pump itself needs a device;
# the wiring is pinned the same way test_python_bridge_pipeline_stall
# pins the preroll fix)
# ---------------------------------------------------------------------------


class TestPumpWiring:
    def test_injector_parameter_defaults_to_none_for_existing_callers(self):
        parameters = inspect.signature(run_bridged_pipeline).parameters
        assert parameters["detections_injector"].default is None

    def test_pump_dispatches_the_injector_metadata(self):
        src = inspect.getsource(run_bridged_pipeline)
        assert "detections_injector.metadata_for(" in src
        assert "metadata=frame_metadata" in src

    def test_poll_constants_match_the_design(self):
        assert DETECTIONS_POLL_BUDGET_SEC == 2.0
        assert DETECTIONS_POLL_INTERVAL_SEC == 0.05
