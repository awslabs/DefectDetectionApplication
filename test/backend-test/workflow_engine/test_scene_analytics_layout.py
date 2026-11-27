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
"""The Layout_Check on the device (car-assembly-check task 8.3 -
Requirements 5.4, 5.9, 12.1; design section 3, DD4-DD6, DD23).

- ``layout.<nodeId>`` is merged with the Data-contracts shape, read from a
  capture record whose detections block carries ``mask_rle`` entries and
  whose input image gives the frame size, and carries no mask;
- the node status texts: ``PASS``, ``FAIL: ...``, the four warning texts,
  the failure text and the ``; ignored outside the car outline: ...``
  suffix;
- an ``error`` gates a binding wired directly to the node, not one behind
  a ``conditional``;
- ``capture_record_input_size`` and its fallbacks;
- the Detection_IDs are the ones ``build_detection_list`` drew;
- the node times itself, the counters stay untimed;
- ``frame`` is seeded for a ``layout_check``-only document, and the
  executor passes ``output_dir`` and ``capture_id``.
"""
import base64
import json
import random
from unittest.mock import patch

import pytest

from test_workflow_stream_executor import FakePipelineManager, seed_run
from workflow_engine_test_utils import DEVICE_ARCH, make_session_factory, write_artifact_set

from workflow_engine import detections, gst_plugins, pipeline_executor, scene_analytics, stream_feed
from workflow_engine.node_status import NodeStatusCollector
from workflow_engine.output_bindings import OutputBindingProcessor
from workflow_engine.pipeline_executor import WorkflowExecutor
from workflow_engine.scene_analytics import (
    LAYOUT_KEY,
    OUTCOMES_KEY,
    EventGateStateStore,
    apply_scene_analytics,
    capture_record_input_size,
    has_scene_analytics,
    needs_frame_size,
)
from workflow_engine.vendor.workflow_core.analytics import layout

WIDTH, HEIGHT = 40, 30
CAPTURE_ID = "cap-1"

REGIONS = {
    "left": [[0.1, 0.3], [0.45, 0.3], [0.45, 0.8], [0.1, 0.8]],
    "right": [[0.55, 0.3], [0.9, 0.3], [0.9, 0.8], [0.55, 0.8]],
    "roof": [[0.3, 0.05], [0.7, 0.05], [0.7, 0.25], [0.3, 0.25]],
}
REGIONS_TEXT = json.dumps(REGIONS)


def rle(intervals):
    """Canonical_RLE of an interval list over the WIDTH x HEIGHT frame."""
    runs, position = [], 0
    for start, end in intervals:
        runs.extend([start - position, end - start])
        position = end
    runs.append(WIDTH * HEIGHT - position)
    return " ".join(str(run) for run in runs)


def region_mask(label):
    """The exact pixel set of a region, so its Position_Score is 1."""
    return layout._region_intervals(REGIONS[label], WIDTH, HEIGHT)


def rect_mask(x0, y0, x1, y1):
    return [(x * HEIGHT + y0, x * HEIGHT + y1) for x in range(x0, x1)]


def box_of(intervals):
    xs = [start // HEIGHT for start, _end in intervals]
    ys = [start % HEIGHT for start, _end in intervals] + [(end - 1) % HEIGHT for _start, end in intervals]
    return [float(min(xs)), float(min(ys)), float(max(xs) + 1), float(max(ys) + 1)]


def raw(label, intervals=None, confidence=0.9, mask=True):
    """A marshal (raw, list-form) entry; ``intervals`` default to the
    label's own region."""
    intervals = region_mask(label) if intervals is None else intervals
    entry = {"class_index": "0", "class_label": label, "confidence": confidence,
             "bounding_box": box_of(intervals)}
    if mask:
        entry["mask_rle"] = rle(intervals)
        entry["mask_area"] = sum(end - start for start, end in intervals)
    return entry


OUTSIDE = rect_mask(0, 26, 3, 30)  # bottom-left corner, outside every region


def write_record(directory, entries, image="png", capture_id=CAPTURE_ID):
    """A capture record ``{capture_id}.jsonl`` whose detections block
    carries ``entries`` and whose input references the image: ``png`` (a
    WIDTH x HEIGHT file), ``garbage`` (unreadable), ``s3`` (not
    ``file://``) or None (no input)."""
    inputs = []
    if image in ("png", "garbage"):
        path = directory / "{0}.{1}".format(capture_id, "png" if image == "png" else "jpg")
        if image == "png":
            from PIL import Image
            Image.new("L", (WIDTH, HEIGHT)).save(str(path))
        else:
            path.write_bytes(b"not an image")
        inputs.append({"data-ref": "file://{0}".format(path)})
    elif image == "s3":
        inputs.append({"data-ref": "s3://bucket/{0}.jpg".format(capture_id)})
    record = {"deviceFleetAuxiliaryInputs": inputs}
    if entries is not None:
        payload = {"detections": {str(index): entry for index, entry in enumerate(entries)}}
        record["deviceFleetAuxiliaryOutputs"] = [{
            "observedContentType": "json_with_base64_encoding",
            "data": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"),
        }]
    (directory / "{0}.jsonl".format(capture_id)).write_text(json.dumps(record) + "\n")
    return str(directory)


def layout_node(node_id="lay", upstream=("det",), **parameters):
    values = {"expected_regions": REGIONS_TEXT, "min_iou": 0.7, "min_confidence": 0.5,
              "min_outline_overlap": 0.1}
    values.update(parameters)
    return {"nodeId": node_id, "binding": "layout_check", "parameters": values,
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def counter(node_id="count_1", upstream=("det",)):
    return {"nodeId": node_id, "binding": "detection_counter",
            "parameters": {"classes": "left", "min_confidence": 0.0, "zone": "", "zone_rule": "center"},
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def output(node_id, upstream):
    return {"nodeId": node_id, "binding": "mqtt_publish",
            "parameters": {"topic": "line/{0}".format(node_id), "greengrass": True,
                           "payload_template": "{inference_json}"},
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": []}


def conditional(node_id, upstream, condition, true_nodes):
    return {"nodeId": node_id, "binding": "conditional", "parameters": {"condition": condition},
            "portConditions": {"true": condition, "false": "not ({0})".format(condition)},
            "downstreamNodeIdsByPort": {"true": list(true_nodes), "false": []},
            "upstreamNodeIds": list(upstream), "downstreamNodeIds": list(true_nodes)}


def document(*bindings):
    return {"schemaVersion": 1, "workflowId": "wf-1", "workflowVersion": "1", "targetArch": DEVICE_ARCH,
            "segments": [{"name": "s0", "elements": [
                {"nodeId": "det", "factory": "videotestsrc", "args": {"num-buffers": 1}},
                {"nodeId": None, "factory": "fakesink", "args": {}}]}],
            "executorBindings": list(bindings), "pluginDependencies": []}


def built(entries):
    """The run's Detection_List for ``entries``, as the executor builds it."""
    return detections.build_detection_list(entries, detections.DEFAULT_SORT_ORDER, rng=random.Random(7))


def run(tmp_path, entries, *bindings, image="png", tags=None, collector=None):
    """Write the record, build the Detection_List and apply the analytics;
    returns ``(tag_values, outcomes, collector map)``."""
    output_dir = write_record(tmp_path, entries, image=image) if entries is not False else str(tmp_path)
    doc = document(*(bindings or (layout_node(),)))
    node_ids = [binding["nodeId"] for binding in doc["executorBindings"]]
    collector = collector or NodeStatusCollector({}, extra_node_ids=node_ids)
    tag_values = dict(tags or {})
    if entries:
        tag_values["detections"] = built(entries)
    outcomes = apply_scene_analytics(doc, tag_values, collector=collector,
                                     output_dir=output_dir, capture_id=CAPTURE_ID)
    return tag_values, outcomes, collector.to_map(), doc


def ids_by_label(tag_values):
    found = {}
    for entry in tag_values["detections"]:
        found.setdefault(entry["label"], []).append(entry["id"])
    return found


COMPLETE = [raw("left"), raw("right"), raw("roof")]


class TestTheMergedSection:
    def test_a_correct_build_passes_with_the_data_contracts_shape(self, tmp_path):
        tags, outcomes, status, doc = run(tmp_path, COMPLETE)
        assert outcomes == {"lay": "ok"} and doc[OUTCOMES_KEY] == outcomes
        section = tags[LAYOUT_KEY]["lay"]
        assert tuple(section) == layout.LAYOUT_METADATA_KEYS
        assert section["pass"] is True
        assert [piece["status"] for piece in section["pieces"].values()] == ["ok", "ok", "ok"]
        assert section["frame"] == {"width": WIDTH, "height": HEIGHT}
        assert section["flagged"] == [] and section["ignored"] == [] and section["unexpected_classes"] == []
        assert section["expected_regions"] == REGIONS
        assert (section["min_iou"], section["min_confidence"], section["min_outline_overlap"]) == (0.7, 0.5, 0.1)
        (record,) = section["pieces"]["left"]["detections"]
        assert set(record) == {"id", "class_label", "confidence", "bounding_box", "iou", "outline_overlap"}
        assert record["iou"] == 1.0 and record["outline_overlap"] == 1.0
        assert status["lay"]["detail"] == "PASS"

    def test_the_run_metadata_carries_no_mask(self, tmp_path):
        tags, _outcomes, _status, _doc = run(tmp_path, COMPLETE + [raw("left", OUTSIDE)])
        assert "mask_rle" not in json.dumps(tags)

    def test_the_detection_ids_are_the_ones_the_detection_list_drew(self, tmp_path):
        entries = [raw("left", region_mask("right")), raw("right"), raw("roof"), raw("roof", confidence=0.8)]
        tags, _outcomes, _status, _doc = run(tmp_path, entries)
        section = tags[LAYOUT_KEY]["lay"]
        ids = ids_by_label(tags)
        drawn = {detection["id"] for piece in section["pieces"].values() for detection in piece["detections"]}
        assert drawn == {entry["id"] for entry in tags["detections"]}
        assert section["pieces"]["left"]["detections"][0]["id"] == ids["left"][0]
        assert set(section["flagged"]) == set(ids["left"] + ids["roof"])

    def test_without_layout_check_nothing_changes(self, tmp_path):
        doc = document(counter())
        tags = {"detections": built(COMPLETE)}
        apply_scene_analytics(doc, tags, output_dir=write_record(tmp_path, COMPLETE), capture_id=CAPTURE_ID)
        assert LAYOUT_KEY not in tags
        assert tags["counter"]["count_1"]["counts"]["left"] == 1


class TestNodeStatusTexts:
    def test_misplaced_and_missing(self, tmp_path):
        tags, outcomes, status, _doc = run(tmp_path, [raw("left", region_mask("right")), raw("right")])
        assert outcomes == {"lay": "ok"}
        assert tags[LAYOUT_KEY]["lay"]["pass"] is False
        assert status["lay"]["detail"] == "FAIL: left misplaced (IoU 0.00), roof missing"

    def test_duplicate(self, tmp_path):
        _tags, _outcomes, status, _doc = run(tmp_path, COMPLETE + [raw("roof", confidence=0.8)])
        assert status["lay"]["detail"] == "FAIL: roof duplicate (2)"

    def test_an_instance_outside_the_car_outline_is_ignored_and_named(self, tmp_path):
        tags, outcomes, status, _doc = run(tmp_path, COMPLETE + [raw("left", OUTSIDE)])
        section = tags[LAYOUT_KEY]["lay"]
        assert outcomes == {"lay": "ok"} and section["pass"] is True
        (ignored,) = section["ignored"]
        assert ignored["class_label"] == "left" and ignored["outline_overlap"] == 0.0
        assert ignored["id"] in ids_by_label(tags)["left"]
        assert status["lay"]["detail"] == "PASS; ignored outside the car outline: left (1)"

    def test_the_suffix_follows_a_failure_too(self, tmp_path):
        _tags, _outcomes, status, _doc = run(tmp_path, [raw("left"), raw("right"), raw("roof", OUTSIDE)])
        assert status["lay"]["detail"] == "FAIL: roof missing; ignored outside the car outline: roof (1)"

    def test_a_detection_without_a_mask_is_a_misplaced_warning(self, tmp_path):
        tags, outcomes, status, _doc = run(tmp_path, [raw("left"), raw("right"), raw("roof", mask=False)])
        roof_id = ids_by_label(tags)["roof"][0]
        assert outcomes == {"lay": "warning"}
        assert status["lay"]["status"] == "warning"
        assert status["lay"]["detail"] == "FAIL: roof misplaced (no mask: {0})".format(roof_id)

    def test_no_detections_to_check(self, tmp_path):
        _tags, outcomes, status, _doc = run(tmp_path, False, tags={"frame": {"width": WIDTH, "height": HEIGHT}})
        assert outcomes == {"lay": "warning"}
        assert status["lay"]["status"] == "warning"
        assert status["lay"]["detail"] == "no detections to check"

    def test_frame_size_unknown(self, tmp_path):
        tags, outcomes, status, _doc = run(tmp_path, COMPLETE, image="s3")
        assert outcomes == {"lay": "warning"}
        assert status["lay"]["detail"] == "frame size unknown"
        section = tags[LAYOUT_KEY]["lay"]
        assert section["frame"] is None
        assert [piece["status"] for piece in section["pieces"].values()] == ["missing"] * 3

    def test_unexpected_classes(self, tmp_path):
        _tags, outcomes, status, _doc = run(tmp_path, COMPLETE + [raw("wheel", region_mask("roof"))])
        assert outcomes == {"lay": "warning"}
        assert status["lay"]["detail"] == "unexpected classes: wheel"

    def test_unusable_expected_regions_fail_the_node_not_the_run(self, tmp_path):
        tags, outcomes, status, _doc = run(tmp_path, COMPLETE, layout_node(expected_regions=""))
        assert outcomes == {"lay": "error"}
        assert status["lay"]["status"] == "failure"
        assert status["lay"]["detail"] == "expected_regions: No expected regions are set."
        assert tags[LAYOUT_KEY]["lay"]["pass"] is False and tags[LAYOUT_KEY]["lay"]["pieces"] == {}


class Published:
    def __init__(self):
        self.topics = []

    def __call__(self, topic, payload, qos, **kwargs):
        self.topics.append(topic)


class TestGating:
    def test_an_error_gates_a_direct_binding_but_not_one_behind_a_conditional(self, tmp_path):
        output_dir = write_record(tmp_path, COMPLETE)
        doc = document(layout_node(expected_regions="not json"), output("direct", ["lay"]),
                       conditional("cond", ["lay"], "layout.lay.pass == false", ["reject"]),
                       output("reject", ["cond"]))
        tags = {"detections": built(COMPLETE)}
        assert apply_scene_analytics(doc, tags, output_dir=output_dir, capture_id=CAPTURE_ID) == {"lay": "error"}
        published = Published()
        details = {}
        OutputBindingProcessor(greengrass_publisher=published, event_gate_store=EventGateStateStore())(
            None, doc, tags, detail_sink=details.__setitem__)
        assert published.topics == ["line/reject"]
        assert details["direct"].startswith("not sent: gated out")


class TestFrameSize:
    def test_the_input_image_header_gives_the_size(self, tmp_path):
        assert capture_record_input_size(write_record(tmp_path, COMPLETE), CAPTURE_ID) == (WIDTH, HEIGHT)

    @pytest.mark.parametrize("image", [None, "s3", "garbage"])
    def test_no_usable_input_image_is_none(self, tmp_path, image):
        assert capture_record_input_size(write_record(tmp_path, COMPLETE, image=image), CAPTURE_ID) is None

    def test_no_record_is_none(self, tmp_path):
        assert capture_record_input_size(str(tmp_path), CAPTURE_ID) is None
        assert capture_record_input_size(None, CAPTURE_ID) is None
        assert capture_record_input_size(str(tmp_path), None) is None

    def test_an_unreadable_image_falls_back_to_the_seeded_frame(self, tmp_path):
        tags, outcomes, _status, _doc = run(tmp_path, COMPLETE, image="garbage",
                                            tags={"frame": {"width": WIDTH, "height": HEIGHT}})
        assert outcomes == {"lay": "ok"}
        assert tags[LAYOUT_KEY]["lay"]["frame"] == {"width": WIDTH, "height": HEIGHT}

    def test_needs_frame_size_is_unchanged(self):
        assert not needs_frame_size(document(layout_node()))
        assert has_scene_analytics(document(layout_node()))


class TestSelfTiming:
    def test_only_the_layout_node_records_a_duration(self, tmp_path):
        collector = NodeStatusCollector({}, extra_node_ids=["count_1", "lay"])
        with patch.object(collector, "record_invocation_duration",
                          wraps=collector.record_invocation_duration) as spy:
            _tags, _outcomes, status, _doc = run(tmp_path, COMPLETE, counter(), layout_node(),
                                                 collector=collector)
        assert spy.call_count == 1
        (node_id, duration_ms), _kwargs = spy.call_args
        assert node_id == "lay" and duration_ms >= 0
        assert "durationMs" in status["lay"] and "durationMs" not in status["count_1"]

    def test_the_capture_record_is_read_once_per_run(self, tmp_path):
        with patch.object(scene_analytics.detection_records, "read_detections",
                          wraps=detections.read_detections) as spy:
            tags, _outcomes, _status, _doc = run(tmp_path, COMPLETE, layout_node("a"), layout_node("b"))
        assert spy.call_count == 1
        assert tags[LAYOUT_KEY]["a"] == tags[LAYOUT_KEY]["b"]


class TestFeedAndExecutor:
    def test_a_layout_check_only_document_seeds_the_frame(self):
        assert stream_feed.document_has_stream_or_analytics(
            {"executorBindings": [layout_node()]})

    def test_the_executor_passes_the_capture_location(self, tmp_path):
        calls = []

        def stub(document, tag_values, **kwargs):
            calls.append(kwargs)
            return {}

        session_factory = make_session_factory()
        doc = document(layout_node())
        artifact_path = write_artifact_set(tmp_path / "workflows", compiled=doc)
        execution_id = seed_run(session_factory, artifact_path)
        executor = WorkflowExecutor(session_factory=session_factory,
                                    pipeline_manager_factory=lambda: FakePipelineManager(),
                                    post_run_handler=lambda registration, document, tags: None)
        with patch.object(gst_plugins, "_scan_registry", return_value=True), \
                patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", str(tmp_path / "captures")), \
                patch.object(pipeline_executor.scene_analytics, "apply_scene_analytics", side_effect=stub):
            executor.execute(execution_id)
        (kwargs,) = calls
        assert set(kwargs) >= {"frame_size", "collector", "output_dir", "capture_id"}
        assert kwargs["output_dir"] and kwargs["capture_id"]
        assert kwargs["output_dir"].startswith(str(tmp_path / "captures"))
