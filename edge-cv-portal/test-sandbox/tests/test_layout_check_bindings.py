"""The Layout Check in the cloud test sandbox (car-assembly-check tasks
9.1, 9.2 - Requirements 5.7, 5.9; design section 4).

- ``_raw_form`` maps a simulated built-form entry to the raw form the
  check reads and keeps a raw entry as it is;
- for every case of the shared fixture
  ``fixtures/layout_check_parity_cases.json`` (whose device half is
  ``test/backend-test/workflow_engine/test_property_layout_parity_device.py``),
  ``execute_bindings`` over the built entries carrying their raw entry's
  ``mask_rle`` yields the pinned ``layout.<nodeId>`` section;
- simulated detections without masks give ``misplaced`` for the classes
  they name and ``missing`` for the others, a warning, ``pass = false``
  and no exception; a run without simulated detections is a warning;
- unusable ``expected_regions`` fail the node with
  ``ANALYTICS_CONFIG_INVALID`` and gate its direct recorder.
"""
import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEST_SANDBOX_DIR = os.path.dirname(_TESTS_DIR)
_WORKFLOW_CORE_PYTHON = os.path.join(
    os.path.dirname(_TEST_SANDBOX_DIR), "backend", "layers", "workflow_core", "python")
if _TEST_SANDBOX_DIR not in sys.path:
    sys.path.insert(0, _TEST_SANDBOX_DIR)
if _WORKFLOW_CORE_PYTHON not in sys.path:
    sys.path.append(_WORKFLOW_CORE_PYTHON)

from harness import bindings  # noqa: E402
from harness.bindings import _raw_form, execute_bindings  # noqa: E402
from harness.results import ResultsStore  # noqa: E402

FIXTURE = os.path.join(_TESTS_DIR, "fixtures", "layout_check_parity_cases.json")
with open(FIXTURE, "r", encoding="utf-8") as _handle:
    CASES = json.load(_handle)["cases"]

METADATA = {"is_anomalous": False, "confidence": 0.9}
FRAME = CASES[0]["frame"]
REGIONS = CASES[0]["regions"]


def layout_bindings(regions, parameters=None):
    """layout_check 'lay' -> recorder 'mq', as compiled."""
    values = dict(parameters or CASES[0]["parameters"])
    values["expected_regions"] = regions
    return [
        {"nodeId": "lay", "binding": "layout_check", "parameters": values,
         "upstreamNodeIds": ["det"], "downstreamNodeIds": ["mq"]},
        {"nodeId": "mq", "binding": "recording_mqtt_publish",
         "parameters": {"topic": "dda/layout"},
         "upstreamNodeIds": ["lay"], "downstreamNodeIds": []},
    ]


def store_for(node_bindings):
    return ResultsStore([binding["nodeId"] for binding in node_bindings])


def simulated(case):
    """The built entries, each extended with its raw entry's mask."""
    entries = []
    for built, raw in zip(case["built_entries"], case["raw_entries"]):
        entry = dict(built)
        if "mask_rle" in raw:
            entry["mask_rle"] = raw["mask_rle"]
        entries.append(entry)
    return entries


def test_the_binding_is_a_scene_analytics_binding():
    # The assertion task 18.3 runs inside the built image.
    assert "layout_check" in bindings.SCENE_ANALYTICS_BINDINGS


class TestRawForm:
    def test_a_built_entry_becomes_a_raw_entry(self):
        built = {"id": "d1", "label": "roof", "confidence": 0.9,
                 "x_min": 1.0, "y_min": 2.0, "x_max": 3.0, "y_max": 4.0,
                 "mask_rle": "0 5 7", "mask_area": 5, "mask_truncated": False}
        assert _raw_form(built) == {
            "id": "d1", "class_label": "roof", "confidence": 0.9,
            "bounding_box": [1.0, 2.0, 3.0, 4.0],
            "mask_rle": "0 5 7", "mask_area": 5, "mask_truncated": False}

    def test_absent_mask_fields_stay_absent(self):
        raw = _raw_form({"id": "d1", "label": "roof", "confidence": 0.9,
                         "x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4})
        assert set(raw) == {"id", "class_label", "confidence", "bounding_box"}

    def test_a_raw_entry_passes_through(self):
        raw = CASES[0]["raw_entries"][0]
        assert _raw_form(raw) is raw
        assert _raw_form("not an entry") == "not an entry"


def test_the_shared_corpus_covers_the_cases():
    assert [case["name"] for case in CASES] == [
        "pass", "one_misplaced", "one_missing", "duplicate", "no_mask_entry",
        "unexpected_class", "outside_car_outline"]
    outside = next(case for case in CASES if case["name"] == "outside_car_outline")
    assert outside["expected_section"]["ignored"] and outside["expected_section"]["pass"] is True


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_sandbox_reproduces_the_pinned_section(case):
    node_bindings = layout_bindings(case["regions"], case["parameters"])
    store = store_for(node_bindings)
    execute_bindings(node_bindings, METADATA, store, detections=simulated(case),
                     frame_size=case["frame"])
    (output,) = store.record("lay")["outputs"]
    assert output["metadata"] == case["expected_section"]
    (activity,) = store.record("mq")["stubActivity"]
    assert activity["triggeringMetadata"]["layout"]["lay"] == case["expected_section"]
    assert "mask_rle" not in json.dumps(activity["triggeringMetadata"]["layout"])
    assert store.record("lay")["status"] == "completed"
    assert not store.has_failure()


class TestSimulatedDetections:
    def test_entries_without_masks_are_misplaced_and_the_rest_missing(self):
        case = CASES[0]
        mask_less = [entry for entry in case["built_entries"] if entry["label"] != "roof"]
        node_bindings = layout_bindings(REGIONS)
        store = store_for(node_bindings)
        execute_bindings(node_bindings, METADATA, store, detections=mask_less, frame_size=FRAME)
        (output,) = store.record("lay")["outputs"]
        section = output["metadata"]
        assert output["outcome"] == "warning"
        assert section["pass"] is False
        statuses = {label: piece["status"] for label, piece in section["pieces"].items()}
        assert statuses == {"rear_fender": "misplaced", "front_fender": "misplaced", "roof": "missing"}
        assert all(piece["iou"] is None for piece in section["pieces"].values())
        assert section["ignored"] == []
        assert sorted(section["flagged"]) == sorted(entry["id"] for entry in mask_less)
        assert len(output["problems"]) == 2 and all("has no usable mask" in p for p in output["problems"])
        rear, front = (entry["id"] for entry in mask_less)
        assert output["detail"] == ("FAIL: rear_fender misplaced (no mask: {0}), front_fender misplaced "
                                    "(no mask: {1}), roof missing".format(rear, front))
        # A warning does not gate the recorder.
        assert store.record("mq")["stubActivity"][0]["triggered"] is True
        assert not store.has_failure()

    def test_a_run_without_simulated_detections_is_a_warning(self):
        node_bindings = layout_bindings(REGIONS)
        store = store_for(node_bindings)
        execute_bindings(node_bindings, METADATA, store, frame_size=FRAME)
        (output,) = store.record("lay")["outputs"]
        assert output["outcome"] == "warning"
        assert output["detail"] == "no detections to check"
        assert output["metadata"]["pass"] is False
        assert {piece["status"] for piece in output["metadata"]["pieces"].values()} == {"missing"}

    def test_an_unknown_frame_is_a_warning(self):
        node_bindings = layout_bindings(REGIONS)
        store = store_for(node_bindings)
        execute_bindings(node_bindings, METADATA, store, detections=simulated(CASES[0]))
        (output,) = store.record("lay")["outputs"]
        assert (output["outcome"], output["detail"]) == ("warning", "frame size unknown")

    def test_the_detail_names_the_ignored_instances(self):
        case = next(case for case in CASES if case["name"] == "outside_car_outline")
        node_bindings = layout_bindings(case["regions"], case["parameters"])
        store = store_for(node_bindings)
        execute_bindings(node_bindings, METADATA, store, detections=simulated(case),
                         frame_size=case["frame"])
        (output,) = store.record("lay")["outputs"]
        assert output["detail"] == "PASS; ignored outside the car outline: rear_fender (1)"

    def test_unusable_regions_fail_the_node_and_gate_its_recorder(self):
        node_bindings = layout_bindings("")
        store = store_for(node_bindings)
        execute_bindings(node_bindings, METADATA, store, detections=simulated(CASES[0]),
                         frame_size=FRAME)
        record = store.record("lay")
        assert record["outputs"][0]["outcome"] == "error"
        assert record["status"] == "failed"
        assert record["error"] == {"code": "ANALYTICS_CONFIG_INVALID",
                                   "message": "expected_regions: No expected regions are set."}
        assert store.record("mq")["stubActivity"][0]["triggered"] is False

    def test_the_caller_metadata_is_not_mutated(self):
        metadata = dict(METADATA, layout={"other": {"pass": True}})
        before = json.dumps(metadata, sort_keys=True)
        node_bindings = layout_bindings(REGIONS)
        execute_bindings(node_bindings, metadata, store_for(node_bindings),
                         detections=simulated(CASES[0]), frame_size=FRAME)
        assert json.dumps(metadata, sort_keys=True) == before

    def test_layout_unavailable_is_reported_like_the_scene_module(self, monkeypatch):
        monkeypatch.setattr(bindings, "_layout_module", lambda: None)
        node_bindings = layout_bindings(REGIONS)
        store = store_for(node_bindings)
        execute_bindings(node_bindings, METADATA, store, detections=simulated(CASES[0]),
                         frame_size=FRAME)
        assert store.record("lay")["error"]["code"] == "ANALYTICS_UNAVAILABLE"
        assert store.record("mq")["stubActivity"][0]["triggered"] is False
