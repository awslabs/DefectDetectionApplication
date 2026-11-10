"""bedrock-inspection-chain: the ``answer_format: inspection`` path of
``BedrockInferenceProcessor`` (``src/backend/workflow_engine/output_bindings.py``).

Covers ``render_prompt_text``, the expected-count rule, ``box_scale``,
the metadata key set on the pass/fail/error paths, ``on_error``,
``error_verdict``, ``enabled: false``, ``max_image_dimension``, the OK/NOK
overlay, and the Bedrock-branch regressions (``work_dir`` forwarding; a
recorded error still running the branch's outputs). Fake invoker,
uploader and publisher; a real JPEG fixture; no network, no AWS.
"""
import json
import os

import cv2
import numpy as np
import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.llm_inference import UnresolvedPlaceholderError
from workflow_engine.node_status import STATUS_FAILURE, NodeStatusCollector
from workflow_engine.output_bindings import (
    BEDROCK_DISABLED_MESSAGE,
    BedrockInferenceError,
    BedrockInferenceProcessor,
    OutputBindingProcessor,
    RunContext,
    draw_defect_objects,
    draw_verdict_banner,
    render_prompt_text,
)

FRAME_NAME = "bedrock_frame_camera.jpg"
CAPTURE_ID = "cap-1"
NODE_ID = "check"
NOVA = "us.amazon.nova-pro-v1:0"
CLAUDE = "us.anthropic.claude-opus-5-5"
PAYLOAD = {
    "order_id": "o-1",
    "body": {
        "qc_stage": "qc2",
        "image_key": "images/o-1/u.jpg",
        "order": {"items": [
            {"shape": "roof", "color": "red"},
            {"shape": "fender", "color": "blue"},
            {"shape": "fender", "color": "blue"},
        ]},
    },
}
TRIGGER = {
    "topic": "plant/line1/evaluate",
    "payload": json.dumps(PAYLOAD),
    "payload_json": PAYLOAD,
}
ERROR_KEYS = {
    "outcome", "error", "attempts", "latency_ms", "model", "box_scale",
    "expected_count", "input_image_path", "input_width", "input_height",
}
PASS_KEYS = {
    "outcome", "verdict", "confidence", "reason", "objects", "objects_px",
    "box_scale", "expected_count", "text", "attempts", "latency_ms",
    "model", "input_image_path", "input_width", "input_height",
    "annotated_image_path",
}


def write_frame(directory, width=64, height=48, name=FRAME_NAME):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    path = os.path.join(str(directory), name)
    assert cv2.imwrite(path, frame)
    return path


def obj(name="roof", qc="OK", box=(100, 200, 500, 1000)):
    x0, y0, x1, y1 = box
    return {"name": name, "qc": qc, "reason": "r",
            "bounding_box": {"x_min": x0, "y_min": y0,
                             "x_max": x1, "y_max": y1}}


def answer(verdict="pass", objects=None, confidence=0.9):
    return json.dumps({
        "verdict": verdict, "confidence": confidence, "reason": "checked",
        "objects": [obj()] if objects is None else objects,
    })


class ScriptedInvoker:
    """Returns (or raises) the scripted items in order and records every
    call's positional arguments and keyword client options."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, *args, **options):
        self.calls.append((args, options))
        item = self.script.pop(0) if self.script else answer()
        if isinstance(item, BaseException):
            raise item
        return item


def make_binding(node_id=NODE_ID, capture=None, **parameters):
    params = {"prompt": "Inspect.", "answer_format": "inspection",
              "model": NOVA}
    params.update(parameters)
    return {
        "nodeId": node_id, "binding": "bedrock_inference",
        "parameters": params, "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": [],
        "capturePaths": {
            "in": "{work_dir}/" + FRAME_NAME if capture is None else capture,
            "reference": None,
        },
    }


def make_context(tmp_path, node_ids=(NODE_ID,), payload=None):
    collector = NodeStatusCollector(extra_node_ids=list(node_ids))
    trigger = json.loads(json.dumps(TRIGGER))
    if payload is not None:
        trigger["payload_json"] = payload
        trigger["payload"] = json.dumps(payload)
    context = RunContext(
        tag_values={"trigger": trigger}, output_dir=str(tmp_path),
        capture_id=CAPTURE_ID, node_status=collector)
    return context, collector


def run(invoker, binding, tmp_path, run_context, **processor_kwargs):
    processor = BedrockInferenceProcessor(invoker=invoker, **processor_kwargs)
    document = {"schemaVersion": 1, "executorBindings": [binding]}
    return processor.process(
        document, dict(run_context.tag_values), str(tmp_path),
        run_context=run_context)


def entry_of(metadata, node_id=NODE_ID):
    return metadata["bedrock"][node_id]


# ---------------------------------------------------------------------------
# render_prompt_text (design §3.2 / §14)
# ---------------------------------------------------------------------------

VALUES = {
    "trigger": {
        "topic": "plant/line1/evaluate",
        "payload_json": {"body": {"qc_stage": "qc2", "order": {
            "items": [{"shape": "roof", "color": "red"}]}},
            "nested": {"a": {"b": 1}}, "weird": "{x}"},
    },
    "image": {"width": 1280, "height": 720},
    "name": "{json:trigger.topic}",
}


class TestRenderPromptText:
    def test_dotted_identifiers_render(self):
        text = render_prompt_text(
            "Stage {trigger.payload_json.body.qc_stage} at "
            "{image.width}x{image.height}", VALUES)
        assert text == "Stage qc2 at 1280x720"

    def test_json_token_renders_compact_json(self):
        text = render_prompt_text(
            "Parts: {json:trigger.payload_json.body.order.items}.", VALUES)
        assert text == 'Parts: [{"shape":"roof","color":"red"}].'

    def test_json_nested_object_ending_in_double_brace_survives(self):
        text = render_prompt_text("{json:trigger.payload_json.nested}", VALUES)
        assert text == '{"a":{"b":1}}'
        assert text.endswith("}}")

    def test_json_string_value_shaped_like_a_placeholder_survives(self):
        text = render_prompt_text("v={json:trigger.payload_json.weird}", VALUES)
        assert text == 'v="{x}"'

    def test_substituted_identifier_value_is_not_re_resolved(self):
        assert render_prompt_text("{name}", VALUES) == "{json:trigger.topic}"

    def test_doubled_braces_collapse_once(self):
        text = render_prompt_text(
            '{{"verdict": "pass", "n": {image.width}}}', VALUES)
        assert text == '{"verdict": "pass", "n": 1280}'

    def test_unresolved_identifier_is_strict(self):
        with pytest.raises(UnresolvedPlaceholderError) as info:
            render_prompt_text("{trigger.payload_json.body.missing}", VALUES)
        assert info.value.name == "trigger.payload_json.body.missing"

    def test_unresolved_json_path_is_strict(self):
        with pytest.raises(UnresolvedPlaceholderError) as info:
            render_prompt_text("{json:trigger.nope}", VALUES)
        assert info.value.name == "json:trigger.nope"

    def test_ensure_ascii_false_keeps_unicode(self):
        values = {"t": {"s": "größe"}}
        assert render_prompt_text("{json:t.s}", values) == '"größe"'


# ---------------------------------------------------------------------------
# Per-run prompt rendering inside the node; the expected-count rule (§3.3)
# ---------------------------------------------------------------------------

ITEMS_PATH = "trigger.payload_json.body.order.items"


class TestRenderPromptInNode:
    def test_rendered_prompt_and_system_prompt_reach_the_invoker(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer())
        binding = make_binding(
            render_prompt=True,
            prompt="Order {trigger.payload_json.order_id}: {json:" + ITEMS_PATH
                   + "} in {image.width}x{image.height}",
            system_prompt="Stage {trigger.payload_json.body.qc_stage}.")
        run(invoker, binding, tmp_path, context)
        (args, options), = invoker.calls
        assert args[1].startswith(
            'Order o-1: [{"shape":"roof","color":"red"},'
            '{"shape":"fender","color":"blue"},{"shape":"fender","color":"blue"}]'
            ' in 64x48\n\n')
        assert args[5] == "Stage qc2."
        assert options == {}

    def test_unresolved_placeholder_is_a_pre_call_failure(self, tmp_path):
        write_frame(tmp_path)
        context, collector = make_context(tmp_path)
        invoker = ScriptedInvoker()
        binding = make_binding(render_prompt=True, on_error="record",
                               prompt="{trigger.payload_json.body.nope}")
        metadata = run(invoker, binding, tmp_path, context)
        assert invoker.calls == []
        assert entry_of(metadata)["error"] == (
            "unresolved prompt placeholder trigger.payload_json.body.nope")
        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    def test_render_prompt_false_sends_the_literal_prompt(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer())
        run(invoker, make_binding(prompt="Literal {x}"), tmp_path, context)
        assert invoker.calls[0][0][1].startswith("Literal {x}\n\n")


class TestExpectedCount:
    @pytest.mark.parametrize("items", [[], 0, -1, True, "3"],
                             ids=["empty-list", "zero", "negative", "bool",
                                  "string"])
    def test_non_positive_shapes_are_an_error_without_a_call(self, tmp_path, items):
        self.assert_count_error(tmp_path, {"body": {"order": {"items": items}}})

    def test_missing_path_is_an_error_without_a_call(self, tmp_path):
        self.assert_count_error(tmp_path, {"body": {"order": {}}})

    def assert_count_error(self, tmp_path, payload):
        write_frame(tmp_path)
        context, collector = make_context(tmp_path, payload=payload)
        invoker = ScriptedInvoker()
        binding = make_binding(expected_count_path=ITEMS_PATH, on_error="record")
        metadata = run(invoker, binding, tmp_path, context)
        assert invoker.calls == []
        entry = entry_of(metadata)
        assert entry["outcome"] == "error"
        assert entry["error"] == (
            "expected count path " + ITEMS_PATH
            + " did not resolve to a positive count")
        assert "expected_count" not in entry
        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    @pytest.mark.parametrize("items", [3, [1, 2, 3]], ids=["int", "list"])
    def test_positive_shapes_resolve_to_three(self, tmp_path, items):
        write_frame(tmp_path)
        context, _ = make_context(
            tmp_path, payload={"body": {"order": {"items": items}}})
        invoker = ScriptedInvoker(answer(objects=[obj(), obj(), obj()]))
        binding = make_binding(expected_count_path=ITEMS_PATH)
        metadata = run(invoker, binding, tmp_path, context)
        assert len(invoker.calls) == 1
        assert entry_of(metadata)["expected_count"] == 3
        assert entry_of(metadata)["outcome"] == "pass"

    def test_pass_with_fewer_objects_than_expected_is_malformed(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(objects=[obj(), obj()]))
        binding = make_binding(expected_count_path=ITEMS_PATH, on_error="record")
        entry = entry_of(run(invoker, binding, tmp_path, context))
        assert entry["outcome"] == "error"
        assert entry["expected_count"] == 3
        assert entry["error"].startswith("malformed Bedrock answer: ")
        assert entry["text"] == answer(objects=[obj(), obj()])


# ---------------------------------------------------------------------------
# box_scale (§3.3): the convention per model, the rescale into objects_px
# ---------------------------------------------------------------------------

class TestBoxScale:
    def test_normalized_box_rescales_to_pixels_of_the_image_sent(self, tmp_path):
        write_frame(tmp_path, width=1280, height=720)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(objects=[obj(box=(100, 200, 500, 1000))]))
        entry = entry_of(run(
            invoker, make_binding(box_scale="normalized_1000"), tmp_path, context))
        assert entry["box_scale"] == "normalized_1000"
        assert entry["objects_px"][0]["bounding_box"] == {
            "x_min": 128, "y_min": 144, "x_max": 640, "y_max": 720}
        # The raw answer coordinates stay in ``objects``.
        assert entry["objects"][0]["bounding_box"] == {
            "x_min": 100, "y_min": 200, "x_max": 500, "y_max": 1000}
        assert entry["objects_px"][0]["name"] == "roof"
        assert entry["objects_px"][0]["qc"] == "OK"

    def test_pixel_box_is_unchanged(self, tmp_path):
        write_frame(tmp_path, width=1280, height=720)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(objects=[obj(box=(10, 20, 300, 400))]))
        entry = entry_of(run(
            invoker, make_binding(box_scale="pixels"), tmp_path, context))
        assert entry["box_scale"] == "pixels"
        assert entry["objects_px"][0]["bounding_box"] == {
            "x_min": 10, "y_min": 20, "x_max": 300, "y_max": 400}

    @pytest.mark.parametrize("model, expected", [
        (NOVA, "normalized_1000"),
        ("us.amazon.nova-lite-v1:0", "normalized_1000"),
        (CLAUDE, "pixels"),
        ("qwen.qwen3-vl-235b-a22b", "pixels"),
    ])
    def test_auto_resolves_by_model_id(self, tmp_path, model, expected):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer())
        entry = entry_of(run(
            invoker, make_binding(model=model, box_scale="auto"), tmp_path, context))
        assert entry["box_scale"] == expected
        assert entry["model"] == model

    def test_coordinate_above_1000_is_malformed_under_normalized(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(objects=[obj(box=(0, 0, 1001, 500))]))
        entry = entry_of(run(
            invoker, make_binding(model=NOVA, on_error="record"), tmp_path, context))
        assert entry["outcome"] == "error"
        assert entry["box_scale"] == "normalized_1000"
        assert "1001" in entry["error"]
        assert entry["error"].startswith("malformed Bedrock answer: ")

    def test_same_coordinates_are_valid_pixels_for_claude(self, tmp_path):
        write_frame(tmp_path, width=1280, height=720)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(objects=[obj(box=(0, 0, 1001, 500))]))
        entry = entry_of(run(invoker, make_binding(model=CLAUDE), tmp_path, context))
        assert entry["outcome"] == "pass"
        assert entry["objects_px"][0]["bounding_box"]["x_max"] == 1001


# ---------------------------------------------------------------------------
# The metadata key set on each path (§3.3) and the flat compatibility keys
# ---------------------------------------------------------------------------

class TestMetadataKeys:
    def test_pass_path_writes_exactly_the_pass_keys(self, tmp_path):
        frame_path = write_frame(tmp_path)
        context, collector = make_context(tmp_path)
        full = answer(objects=[obj(), obj("fender_front"), obj("fender_rear")])
        invoker = ScriptedInvoker(full)
        binding = make_binding(expected_count_path=ITEMS_PATH)
        metadata = run(invoker, binding, tmp_path, context)
        entry = entry_of(metadata)
        assert set(entry) == PASS_KEYS
        assert entry["outcome"] == "pass" and entry["verdict"] == "pass"
        assert entry["confidence"] == 0.9 and entry["reason"] == "checked"
        assert entry["text"] == full
        assert entry["expected_count"] == 3
        assert len(entry["objects"]) == 3 and len(entry["objects_px"]) == 3
        assert entry["attempts"] == 1
        assert isinstance(entry["latency_ms"], int)
        assert entry["model"] == NOVA
        assert entry["input_image_path"] == frame_path
        assert (entry["input_width"], entry["input_height"]) == (64, 48)
        assert entry["annotated_image_path"] == os.path.join(
            str(tmp_path), "cap-1.node.check.annotated.jpg")
        assert os.path.isfile(entry["annotated_image_path"])
        assert metadata["is_anomalous"] is False
        assert metadata["confidence"] == 0.9
        assert metadata["bedrock_text"] == full
        assert collector.status_of(NODE_ID) != STATUS_FAILURE

    def test_fail_verdict_sets_flat_is_anomalous_true(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(
            answer(verdict="fail", objects=[obj(qc="NOK")], confidence=0.7))
        metadata = run(invoker, make_binding(), tmp_path, context)
        entry = entry_of(metadata)
        assert set(entry) == PASS_KEYS - {"expected_count"}
        assert entry["outcome"] == "fail" and entry["verdict"] == "fail"
        assert metadata["is_anomalous"] is True
        assert metadata["confidence"] == 0.7

    def test_error_path_writes_exactly_the_error_keys(self, tmp_path):
        frame_path = write_frame(tmp_path)
        context, collector = make_context(tmp_path)
        invoker = ScriptedInvoker(RuntimeError("boom"))
        binding = make_binding(expected_count_path=ITEMS_PATH, on_error="record")
        metadata = run(invoker, binding, tmp_path, context)
        entry = entry_of(metadata)
        assert set(entry) == ERROR_KEYS
        assert entry["outcome"] == "error"
        assert entry["error"] == "RuntimeError: boom"
        assert entry["attempts"] == 1
        assert entry["expected_count"] == 3
        assert entry["input_image_path"] == frame_path
        assert "is_anomalous" not in metadata and "confidence" not in metadata
        assert "bedrock_text" not in metadata
        assert collector.status_of(NODE_ID) == STATUS_FAILURE
        assert not os.path.exists(
            os.path.join(str(tmp_path), "cap-1.node.check.annotated.jpg"))

    def test_malformed_answer_error_keeps_the_raw_text(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker("not json at all")
        metadata = run(invoker, make_binding(on_error="record"), tmp_path, context)
        entry = entry_of(metadata)
        assert set(entry) == (ERROR_KEYS - {"expected_count"}) | {"text"}
        assert entry["text"] == "not json at all"
        assert metadata["bedrock_text"] == "not json at all"
        assert "is_anomalous" not in metadata

    def test_frame_missing_error_has_no_input_keys(self, tmp_path):
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker()
        metadata = run(invoker, make_binding(on_error="record"), tmp_path, context)
        entry = entry_of(metadata)
        assert invoker.calls == []
        assert set(entry) == {"outcome", "error", "attempts", "latency_ms",
                              "model", "box_scale"}
        assert entry["error"].startswith("could not read the captured 'in' frame")


# ---------------------------------------------------------------------------
# on_error / error_verdict / enabled (§3.1, §6.2; review NITs 1-2)
# ---------------------------------------------------------------------------

def failure_cases():
    """(id, invoker script, binding overrides, frame present?) per class."""
    return [
        ("frame-read", [], {}, False),
        ("pre-call-count", [], {"expected_count_path": "trigger.nope"}, True),
        ("transport", [RuntimeError("boom")], {}, True),
        ("malformed", ["nonsense"], {}, True),
    ]


class TestOnError:
    @pytest.mark.parametrize("case", failure_cases(), ids=lambda c: c[0])
    def test_record_turns_every_failure_class_into_an_error_outcome(
            self, tmp_path, case):
        _, script, overrides, frame = case
        if frame:
            write_frame(tmp_path)
        context, collector = make_context(tmp_path)
        binding = make_binding(on_error="record", **overrides)
        metadata = run(ScriptedInvoker(*script), binding, tmp_path, context)
        entry = entry_of(metadata)
        assert entry["outcome"] == "error" and entry["error"]
        assert "verdict" not in entry
        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    @pytest.mark.parametrize("case", failure_cases(), ids=lambda c: c[0])
    def test_fail_run_default_raises_for_every_failure_class(self, tmp_path, case):
        _, script, overrides, frame = case
        if frame:
            write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        with pytest.raises(BedrockInferenceError) as info:
            run(ScriptedInvoker(*script), make_binding(**overrides), tmp_path, context)
        assert info.value.node_id == NODE_ID
        assert str(info.value).startswith(
            "Bedrock inference node 'check' failed: ")


class TestErrorVerdict:
    def test_fail_body_with_the_flat_keys(self, tmp_path):
        write_frame(tmp_path)
        context, collector = make_context(tmp_path)
        binding = make_binding(on_error="record", error_verdict="fail")
        metadata = run(ScriptedInvoker(RuntimeError("boom")), binding, tmp_path, context)
        entry = entry_of(metadata)
        assert set(entry) == (ERROR_KEYS - {"error", "expected_count"}) | {
            "verdict", "confidence", "reason"}
        assert entry["outcome"] == "error"
        assert entry["verdict"] == "fail"
        assert entry["confidence"] == 0.0
        assert entry["reason"] == "RuntimeError: boom"
        assert metadata["is_anomalous"] is True
        assert metadata["confidence"] == 0.0
        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    def test_error_body_writes_no_flat_keys(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        binding = make_binding(on_error="record", error_verdict="error")
        metadata = run(ScriptedInvoker(RuntimeError("boom")), binding, tmp_path, context)
        assert "is_anomalous" not in metadata and "confidence" not in metadata
        assert entry_of(metadata)["error"] == "RuntimeError: boom"


class TestEnabledFalse:
    @pytest.mark.parametrize("on_error", ["fail_run", "record"])
    @pytest.mark.parametrize("error_verdict", ["error", "fail"])
    def test_always_the_unconditional_error_body(self, tmp_path, on_error, error_verdict):
        write_frame(tmp_path)
        context, collector = make_context(tmp_path)
        invoker = ScriptedInvoker()
        binding = make_binding(enabled=False, on_error=on_error,
                               error_verdict=error_verdict)
        metadata = run(invoker, binding, tmp_path, context)
        assert invoker.calls == []
        assert entry_of(metadata) == {
            "outcome": "error", "error": BEDROCK_DISABLED_MESSAGE,
            "attempts": 0, "latency_ms": 0, "model": NOVA,
            "box_scale": "normalized_1000",
        }
        assert "is_anomalous" not in metadata
        assert collector.status_of(NODE_ID) == STATUS_FAILURE

    def test_legacy_format_disabled_records_todays_error_shape(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker()
        binding = make_binding(enabled="false")
        del binding["parameters"]["answer_format"]
        metadata = run(invoker, binding, tmp_path, context)
        assert invoker.calls == []
        assert entry_of(metadata) == {"error": BEDROCK_DISABLED_MESSAGE}


# ---------------------------------------------------------------------------
# Overlay (§3.6): banner colour/geometry, clamped boxes, objects: [], path
# ---------------------------------------------------------------------------

GREEN = (0, 200, 0)
RED = (0, 0, 255)


def banner_pixels(frame, color):
    mask = np.all(frame == np.array(color, dtype=np.uint8), axis=2)
    ys, xs = np.nonzero(mask)
    return xs, ys


class TestOverlay:
    @pytest.mark.parametrize("verdict, color, other", [
        ("pass", GREEN, RED), ("fail", RED, GREEN)])
    def test_banner_colour_and_geometry(self, verdict, color, other):
        frame = np.zeros((120, 240, 3), dtype=np.uint8)
        draw_verdict_banner(frame, verdict)
        xs, ys = banner_pixels(frame, color)
        assert len(xs) > 0
        # Text drawn at origin (20, 50), scale 1.5, thickness 3: it sits
        # right of x=20 and above the baseline y=50, within the top band.
        assert xs.min() >= 18 and xs.max() <= 140
        assert ys.min() >= 8 and ys.max() <= 54
        assert len(banner_pixels(frame, other)[0]) == 0
        # Nothing else is painted.
        painted = np.any(frame != 0, axis=2)
        assert painted.sum() == len(xs)

    def test_nok_banner_is_wider_than_ok(self):
        ok = np.zeros((120, 240, 3), dtype=np.uint8)
        nok = np.zeros((120, 240, 3), dtype=np.uint8)
        draw_verdict_banner(ok, "pass")
        draw_verdict_banner(nok, "fail")
        assert banner_pixels(nok, RED)[0].max() > banner_pixels(ok, GREEN)[0].max()

    def test_boxes_are_clamped_to_the_frame(self):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        drawn = draw_defect_objects(frame, [
            {"name": "roof", "qc": "NOK",
             "bounding_box": {"x_min": -10, "y_min": 10, "x_max": 500, "y_max": 400}},
        ])
        assert drawn == [(0, 10, 64, 48)]

    def test_empty_objects_still_get_the_banner_and_a_path(self, tmp_path):
        context, _ = make_context(tmp_path)
        _, encoded = cv2.imencode(".jpg", np.zeros((120, 240, 3), dtype=np.uint8))
        path = BedrockInferenceProcessor._persist_annotated_frame(
            context, NODE_ID, encoded.tobytes(), "", objects=[], verdict="fail")
        assert path == os.path.join(str(tmp_path), "cap-1.node.check.annotated.jpg")
        frame = cv2.imread(path)
        band = frame[8:56, 18:140]
        assert band[:, :, 2].max() > 150 and band[:, :, 1].max() < 100

    def test_legacy_call_shape_returns_none_without_objects(self, tmp_path):
        context, _ = make_context(tmp_path)
        _, encoded = cv2.imencode(".jpg", np.zeros((10, 10, 3), dtype=np.uint8))
        assert BedrockInferenceProcessor._persist_annotated_frame(
            context, NODE_ID, encoded.tobytes(), "no objects here") is None

    def test_inspection_run_draws_the_whole_frame_with_boxes(self, tmp_path):
        write_frame(tmp_path, width=200, height=120)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(
            verdict="fail", objects=[obj(qc="NOK", box=(500, 500, 900, 900))]))
        entry = entry_of(run(invoker, make_binding(), tmp_path, context))
        frame = cv2.imread(entry["annotated_image_path"])
        assert frame.shape[:2] == (120, 200)
        # The NOK box (100..180 x 60..108 after rescale) is red; the banner
        # is red too; nothing green.
        assert frame[60:108, 100:180][:, :, 2].max() > 150
        assert len(banner_pixels(frame, GREEN)[0]) == 0


# ---------------------------------------------------------------------------
# max_image_dimension: the bytes actually sent, their sibling artifact
# ---------------------------------------------------------------------------

class TestMaxImageDimension:
    def test_downscaled_bytes_are_sent_persisted_and_measured(self, tmp_path):
        write_frame(tmp_path, width=1280, height=720)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer(objects=[obj(box=(0, 0, 1000, 500))]))
        binding = make_binding(max_image_dimension=640)
        entry = entry_of(run(invoker, binding, tmp_path, context))
        input_path = os.path.join(str(tmp_path), "cap-1.node.check.input.jpg")
        assert entry["input_image_path"] == input_path
        assert (entry["input_width"], entry["input_height"]) == (640, 360)
        sent = invoker.calls[0][0][2][0][1]
        with open(input_path, "rb") as handle:
            assert handle.read() == sent
        decoded = cv2.imdecode(np.frombuffer(sent, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert decoded.shape[:2] == (360, 640)
        # Boxes rescale against the image sent, not the capture.
        assert entry["objects_px"][0]["bounding_box"] == {
            "x_min": 0, "y_min": 0, "x_max": 640, "y_max": 180}
        annotated = cv2.imread(entry["annotated_image_path"])
        assert annotated.shape[:2] == (360, 640)

    def test_zero_sends_the_capture_unmodified(self, tmp_path):
        frame_path = write_frame(tmp_path, width=96, height=64)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer())
        entry = entry_of(run(
            invoker, make_binding(max_image_dimension=0), tmp_path, context))
        with open(frame_path, "rb") as handle:
            assert invoker.calls[0][0][2][0][1] == handle.read()
        assert entry["input_image_path"] == frame_path
        assert (entry["input_width"], entry["input_height"]) == (96, 64)
        assert not os.path.exists(
            os.path.join(str(tmp_path), "cap-1.node.check.input.jpg"))

    def test_smaller_frame_is_never_upscaled(self, tmp_path):
        frame_path = write_frame(tmp_path, width=96, height=64)
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker(answer())
        entry = entry_of(run(
            invoker, make_binding(max_image_dimension=640), tmp_path, context))
        assert entry["input_image_path"] == frame_path
        assert (entry["input_width"], entry["input_height"]) == (96, 64)

    def test_undecodable_frame_is_a_pre_call_failure(self, tmp_path):
        with open(os.path.join(str(tmp_path), FRAME_NAME), "wb") as handle:
            handle.write(b"not an image")
        context, _ = make_context(tmp_path)
        invoker = ScriptedInvoker()
        entry = entry_of(run(
            invoker, make_binding(on_error="record"), tmp_path, context))
        assert invoker.calls == []
        assert entry["outcome"] == "error"
        assert entry["error"].startswith("could not decode the captured 'in' frame")


# ---------------------------------------------------------------------------
# Bedrock branch (§6.1): work_dir forwarding (design review finding 1) and
# the branch outputs on a recorded error (on_error record vs fail_run)
# ---------------------------------------------------------------------------

class RecordingUploader:
    def __init__(self):
        self.calls = []

    def __call__(self, destination, body, content_type, timeout_s, retries, region):
        self.calls.append((destination, body, content_type, timeout_s, retries, region))


class RecordingPublisher:
    def __init__(self):
        self.log = []

    def __call__(self, host, port, topic, payload, qos, *args):
        self.log.append((topic, payload))


def upload_binding(node_id="upload_raw"):
    return {
        "nodeId": node_id, "binding": "s3_upload",
        "parameters": {
            "destination": "s3://example-bucket/{trigger.payload_json.body.image_key}",
            "content_type": "image/jpeg", "timeout_s": 2, "retries": 0,
        },
        "upstreamNodeIds": [NODE_ID], "downstreamNodeIds": [],
        "capturePaths": {"in": "{work_dir}/" + FRAME_NAME},
    }


def publish_binding(node_id="publish_t2"):
    return {
        "nodeId": node_id, "binding": "mqtt_publish",
        "parameters": {
            "broker_host": "broker.local", "topic": "plant/line1/result",
            "payload_template": '{{"outcome": "{bedrock.check.outcome}"}}',
        },
        "upstreamNodeIds": ["upload_raw"], "downstreamNodeIds": [],
    }


def run_branch(tmp_path, invoker, **check_parameters):
    frame_path = write_frame(tmp_path)
    context, collector = make_context(
        tmp_path, node_ids=(NODE_ID, "upload_raw", "publish_t2"))
    uploader, publisher = RecordingUploader(), RecordingPublisher()
    processor = BedrockInferenceProcessor(
        invoker=invoker,
        output_processor=OutputBindingProcessor(
            s3_uploader=uploader, mqtt_publisher=publisher))
    document = {"schemaVersion": 1, "executorBindings": [
        make_binding(**check_parameters), upload_binding(), publish_binding()]}
    metadata = processor.process(
        document, dict(context.tag_values), str(tmp_path), run_context=context)
    with open(frame_path, "rb") as handle:
        frame_bytes = handle.read()
    return metadata, uploader, publisher, collector, frame_bytes


class TestBedrockBranch:
    def test_branch_member_upload_resolves_work_dir_and_uploads_the_frame(
            self, tmp_path):
        metadata, uploader, publisher, _, frame_bytes = run_branch(
            tmp_path, ScriptedInvoker(answer()))
        assert entry_of(metadata)["outcome"] == "pass"
        (destination, body, content_type, timeout_s, retries, region), = uploader.calls
        assert destination == "s3://example-bucket/images/o-1/u.jpg"
        assert body == frame_bytes
        assert (content_type, timeout_s, retries) == ("image/jpeg", 2, 0)
        assert publisher.log == [("plant/line1/result", '{"outcome": "pass"}')]

    def test_recorded_error_with_on_error_record_still_runs_the_branch(
            self, tmp_path):
        metadata, uploader, publisher, collector, frame_bytes = run_branch(
            tmp_path, ScriptedInvoker(RuntimeError("boom")), on_error="record")
        assert entry_of(metadata)["error"] == "RuntimeError: boom"
        assert collector.status_of(NODE_ID) == STATUS_FAILURE
        assert [call[1] for call in uploader.calls] == [frame_bytes]
        assert publisher.log == [("plant/line1/result", '{"outcome": "error"}')]

    def test_fail_run_raises_and_publishes_nothing(self, tmp_path):
        with pytest.raises(BedrockInferenceError) as info:
            run_branch(tmp_path, ScriptedInvoker(RuntimeError("boom")))
        assert info.value.node_id == NODE_ID

    def test_fail_run_branch_outputs_never_run(self, tmp_path):
        write_frame(tmp_path)
        context, _ = make_context(tmp_path, node_ids=(NODE_ID, "upload_raw", "publish_t2"))
        uploader, publisher = RecordingUploader(), RecordingPublisher()
        processor = BedrockInferenceProcessor(
            invoker=ScriptedInvoker(RuntimeError("boom")),
            output_processor=OutputBindingProcessor(
                s3_uploader=uploader, mqtt_publisher=publisher))
        document = {"schemaVersion": 1, "executorBindings": [
            make_binding(), upload_binding(), publish_binding()]}
        with pytest.raises(BedrockInferenceError):
            processor.process(document, dict(context.tag_values), str(tmp_path),
                              run_context=context)
        assert uploader.calls == [] and publisher.log == []

    def test_legacy_recorded_error_still_skips_the_branch_outputs(self, tmp_path):
        """A legacy (fail_run) recorded error — here ``enabled: false`` on
        a legacy-format node — keeps today's skip."""
        write_frame(tmp_path)
        context, _ = make_context(tmp_path, node_ids=(NODE_ID, "upload_raw", "publish_t2"))
        uploader, publisher = RecordingUploader(), RecordingPublisher()
        processor = BedrockInferenceProcessor(
            invoker=ScriptedInvoker(),
            output_processor=OutputBindingProcessor(
                s3_uploader=uploader, mqtt_publisher=publisher))
        binding = make_binding(enabled=False)
        del binding["parameters"]["answer_format"]
        document = {"schemaVersion": 1, "executorBindings": [
            binding, upload_binding(), publish_binding()]}
        metadata = processor.process(
            document, dict(context.tag_values), str(tmp_path), run_context=context)
        assert entry_of(metadata) == {"error": BEDROCK_DISABLED_MESSAGE}
        assert uploader.calls == [] and publisher.log == []
