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
"""Unit tests for ``workflow_engine.detections``
(detection-guided-bedrock-inspection Requirements 1.1, 1.2, 1.4, 1.5,
1.7, 1.8).

The primary fixtures are REAL capture records copied from jetson-thor1's
yolo-world blue-plate workflow (workflow ``515zkeve``, run of 2026-08-28,
``model-yolo-world-blue-plate-jetson-xavier-jp7`` v3):

- ``fixtures/515zkeve-326caa317dc4474cad2db8c2f69095ac.jsonl`` — three
  "blue box" detections (the true marshal contract, boxes in source-frame
  pixels);
- ``fixtures/515zkeve-5267106cd0874b339818519b351e26f5.jsonl`` — the
  marshal's zero-object record (``{"detections": {}}``).

Hand-mutated variants of the real record cover the contained failure
paths: no detections block, malformed base64/JSON, segmentation payloads.
"""
import base64
import copy
import json
import os
from unittest import mock

import pytest

from workflow_engine import detections

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

#: The real thor1 record with three detections (2026-08-28 22:30:10 UTC).
THOR1_CAPTURE_ID = "515zkeve-326caa317dc4474cad2db8c2f69095ac"

#: The real thor1 zero-object record ({"detections": {}}).
THOR1_EMPTY_CAPTURE_ID = "515zkeve-5267106cd0874b339818519b351e26f5"

#: The three raw boxes exactly as the marshal emitted them (decoded from
#: the fixture's json_with_base64_encoding block).
THOR1_BOXES = [
    [727.13883228302, 459.18015890121455, 1581.9408876419068, 1069.6540051460265],
    [880.5637826919556, 1435.5390385150909, 1744.1607801437376, 2064.7602468967434],
    [229.37965364456176, 903.8866567611693, 833.4318779945373, 1751.5087280273435],
]
THOR1_CONFIDENCES = [0.19878113269805908, 0.15921247005462646, 0.10260778665542603]


def read_fixture_record(capture_id):
    path = os.path.join(FIXTURES_DIR, capture_id + ".jsonl")
    with open(path, "r") as jsonl_file:
        return json.loads(jsonl_file.read().strip().splitlines()[-1])


def write_record(tmp_path, capture_id, record):
    path = os.path.join(str(tmp_path), capture_id + ".jsonl")
    with open(path, "w") as jsonl_file:
        jsonl_file.write(json.dumps(record) + "\n")
    return str(tmp_path)


def detections_block(record):
    """The record's json_with_base64_encoding output entry (in place)."""
    for entry in record["deviceFleetAuxiliaryOutputs"]:
        if entry.get("observedContentType") == "json_with_base64_encoding":
            return entry
    raise AssertionError("fixture record has no detections block")


class TestReadDetections:
    """Record parsing against the real marshal contract plus mutations."""

    def test_real_thor1_record_yields_raw_entries_in_key_order(self):
        raw = detections.read_detections(FIXTURES_DIR, THOR1_CAPTURE_ID)
        assert raw is not None
        assert len(raw) == 3
        for index, entry in enumerate(raw):
            assert entry["class_label"] == "blue box"
            assert entry["bounding_box"] == THOR1_BOXES[index]
            assert entry["confidence"] == THOR1_CONFIDENCES[index]

    def test_real_thor1_zero_object_record_yields_empty_list(self):
        # Requirement 1.5 groundwork: the marshal's zero-object record
        # carries an EMPTY detections map, distinct from "no block".
        raw = detections.read_detections(FIXTURES_DIR, THOR1_EMPTY_CAPTURE_ID)
        assert raw == []

    def test_missing_record_returns_none(self, tmp_path):
        assert detections.read_detections(str(tmp_path), "no-such-capture") is None

    def test_missing_arguments_return_none(self):
        assert detections.read_detections(None, THOR1_CAPTURE_ID) is None
        assert detections.read_detections(FIXTURES_DIR, None) is None

    def test_record_without_detections_block_returns_none(self, tmp_path):
        # A non-detection model's record has no json_with_base64_encoding
        # block carrying "detections" (Requirement 1.8).
        record = read_fixture_record(THOR1_CAPTURE_ID)
        record["deviceFleetAuxiliaryOutputs"] = [
            entry
            for entry in record["deviceFleetAuxiliaryOutputs"]
            if entry.get("observedContentType") != "json_with_base64_encoding"
        ]
        output_dir = write_record(tmp_path, "mutated", record)
        assert detections.read_detections(output_dir, "mutated") is None

    def test_segmentation_label_block_is_not_a_detections_block(self, tmp_path):
        # Segmentation records share the content type but carry
        # "anomalies" — the discriminator is the top-level "detections"
        # key (same as inference_results_utils).
        record = read_fixture_record(THOR1_CAPTURE_ID)
        payload = {"anomalies": {"0": {"class-name": "background"}}}
        detections_block(record)["data"] = base64.b64encode(
            json.dumps(payload).encode()
        ).decode()
        output_dir = write_record(tmp_path, "mutated", record)
        assert detections.read_detections(output_dir, "mutated") is None

    def test_empty_detections_map_returns_empty_list(self, tmp_path):
        record = read_fixture_record(THOR1_CAPTURE_ID)
        detections_block(record)["data"] = base64.b64encode(
            json.dumps({"detections": {}}).encode()
        ).decode()
        output_dir = write_record(tmp_path, "mutated", record)
        assert detections.read_detections(output_dir, "mutated") == []

    def test_malformed_base64_returns_none(self, tmp_path):
        record = read_fixture_record(THOR1_CAPTURE_ID)
        detections_block(record)["data"] = "!!! not base64 at all !!!"
        output_dir = write_record(tmp_path, "mutated", record)
        assert detections.read_detections(output_dir, "mutated") is None

    def test_malformed_record_json_returns_none(self, tmp_path):
        path = os.path.join(str(tmp_path), "mutated.jsonl")
        with open(path, "w") as jsonl_file:
            jsonl_file.write("{this is not json\n")
        assert detections.read_detections(str(tmp_path), "mutated") is None

    def test_empty_record_file_returns_none(self, tmp_path):
        path = os.path.join(str(tmp_path), "mutated.jsonl")
        with open(path, "w") as jsonl_file:
            jsonl_file.write("\n\n")
        assert detections.read_detections(str(tmp_path), "mutated") is None

    def test_non_dict_detections_value_returns_none(self, tmp_path):
        record = read_fixture_record(THOR1_CAPTURE_ID)
        detections_block(record)["data"] = base64.b64encode(
            json.dumps({"detections": [1, 2, 3]}).encode()
        ).decode()
        output_dir = write_record(tmp_path, "mutated", record)
        assert detections.read_detections(output_dir, "mutated") is None


def write_sidecar(tmp_path, capture_id, payload):
    """Write a marshal-shaped detections sidecar
    ``{tmp_path}/{capture_id}.detections.json`` (raw string for malformed
    cases, otherwise JSON-dumped)."""
    path = os.path.join(str(tmp_path), capture_id + ".detections.json")
    with open(path, "w") as sidecar_file:
        if isinstance(payload, str):
            sidecar_file.write(payload)
        else:
            sidecar_file.write(json.dumps(payload))
    return str(tmp_path)


class TestReadDetectionsSidecar:
    """The marshal-written sidecar fallback (design Risk 1): graphs
    without a capture node never route the broker file targets that land
    the ``.jsonl`` record, so the marshal persists
    ``{capture_id}.detections.json`` directly and ``read_detections``
    falls back to it."""

    def sidecar_payload(self):
        """The exact payload the marshal's sidecar carries — the same
        shape as the capture record's decoded base64 block, built from
        the real thor1 entries."""
        record = read_fixture_record(THOR1_CAPTURE_ID)
        return json.loads(base64.b64decode(detections_block(record)["data"]))

    def test_sidecar_only_yields_raw_entries_in_key_order(self, tmp_path):
        # No .jsonl present at all — the capture-node-less case.
        output_dir = write_sidecar(tmp_path, "run", self.sidecar_payload())
        raw = detections.read_detections(output_dir, "run")
        assert raw is not None
        assert len(raw) == 3
        for index, entry in enumerate(raw):
            assert entry["class_label"] == "blue box"
            assert entry["bounding_box"] == THOR1_BOXES[index]
            assert entry["confidence"] == THOR1_CONFIDENCES[index]

    def test_jsonl_wins_when_both_present(self, tmp_path):
        # The record path stays byte-identical for capture-node
        # workflows: a divergent sidecar is ignored when the .jsonl
        # carries a detections block.
        record = read_fixture_record(THOR1_CAPTURE_ID)
        output_dir = write_record(tmp_path, "run", record)
        divergent = {
            "detections": {
                "0": raw_entry([1.0, 2.0, 3.0, 4.0], 0.99, label="sidecar")
            }
        }
        write_sidecar(tmp_path, "run", divergent)
        raw = detections.read_detections(output_dir, "run")
        assert len(raw) == 3
        assert all(entry["class_label"] == "blue box" for entry in raw)

    def test_sidecar_used_when_jsonl_has_no_detections_block(self, tmp_path):
        # A record without a detections block (non-detection capture
        # framing) falls through to the sidecar.
        record = read_fixture_record(THOR1_CAPTURE_ID)
        record["deviceFleetAuxiliaryOutputs"] = [
            entry
            for entry in record["deviceFleetAuxiliaryOutputs"]
            if entry.get("observedContentType") != "json_with_base64_encoding"
        ]
        output_dir = write_record(tmp_path, "run", record)
        write_sidecar(tmp_path, "run", self.sidecar_payload())
        raw = detections.read_detections(output_dir, "run")
        assert raw is not None
        assert len(raw) == 3

    def test_empty_sidecar_detections_map_returns_empty_list(self, tmp_path):
        output_dir = write_sidecar(tmp_path, "run", {"detections": {}})
        assert detections.read_detections(output_dir, "run") == []

    def test_malformed_sidecar_json_returns_none(self, tmp_path):
        output_dir = write_sidecar(tmp_path, "run", "{this is not json")
        assert detections.read_detections(output_dir, "run") is None

    def test_sidecar_without_detections_map_returns_none(self, tmp_path):
        output_dir = write_sidecar(tmp_path, "run", {"anomalies": {}})
        assert detections.read_detections(output_dir, "run") is None

    def test_non_dict_sidecar_detections_value_returns_none(self, tmp_path):
        output_dir = write_sidecar(tmp_path, "run", {"detections": [1, 2]})
        assert detections.read_detections(output_dir, "run") is None

    def test_malformed_sidecar_is_contained_beside_valid_jsonl(self, tmp_path):
        # Containment: a broken sidecar never masks a good record.
        record = read_fixture_record(THOR1_CAPTURE_ID)
        output_dir = write_record(tmp_path, "run", record)
        write_sidecar(tmp_path, "run", "not json at all")
        raw = detections.read_detections(output_dir, "run")
        assert raw is not None
        assert len(raw) == 3


def raw_entry(box, confidence, label="blue box"):
    return {
        "class_index": label,
        "class_label": label,
        "bounding_box": box,
        "confidence": confidence,
    }


class TestBuildDetectionList:
    """Normalization, Detection_IDs, and every sort order incl. ties."""

    def test_normalizes_real_thor1_entries(self):
        raw = detections.read_detections(FIXTURES_DIR, THOR1_CAPTURE_ID)
        built = detections.build_detection_list(raw, "left_to_right")
        assert len(built) == 3
        for entry in built:
            assert set(entry) == {
                "id", "label", "confidence",
                "x_min", "y_min", "x_max", "y_max",
            }
            assert entry["label"] == "blue box"
            for key in ("confidence", "x_min", "y_min", "x_max", "y_max"):
                assert isinstance(entry[key], float)
            assert isinstance(entry["id"], str)
            assert len(entry["id"]) == 8
        assert len({entry["id"] for entry in built}) == 3

    # Real thor1 box centers: entry0 (1154.5, 764.4), entry1
    # (1312.4, 1750.1), entry2 (531.4, 1327.7); confidences descend
    # 0.199 > 0.159 > 0.103 in raw order.
    @pytest.mark.parametrize(
        "sort_order,expected_conf_order",
        [
            ("left_to_right", [2, 0, 1]),
            ("right_to_left", [1, 0, 2]),
            ("top_to_bottom", [0, 2, 1]),
            ("bottom_to_top", [1, 2, 0]),
            ("confidence_desc", [0, 1, 2]),
        ],
    )
    def test_sort_orders_on_real_thor1_detections(
        self, sort_order, expected_conf_order
    ):
        raw = detections.read_detections(FIXTURES_DIR, THOR1_CAPTURE_ID)
        built = detections.build_detection_list(raw, sort_order)
        expected = [THOR1_CONFIDENCES[i] for i in expected_conf_order]
        assert [entry["confidence"] for entry in built] == expected

    def test_left_to_right_ties_break_by_center_y_ascending(self):
        # Same center-x (50), center-y 100 vs 20 -> y ascending.
        raw = [
            raw_entry([0.0, 90.0, 100.0, 110.0], 0.5),
            raw_entry([0.0, 10.0, 100.0, 30.0], 0.4),
        ]
        built = detections.build_detection_list(raw, "left_to_right")
        assert [entry["confidence"] for entry in built] == [0.4, 0.5]

    def test_right_to_left_ties_break_by_center_y_ascending(self):
        raw = [
            raw_entry([0.0, 90.0, 100.0, 110.0], 0.5),
            raw_entry([0.0, 10.0, 100.0, 30.0], 0.4),
        ]
        built = detections.build_detection_list(raw, "right_to_left")
        assert [entry["confidence"] for entry in built] == [0.4, 0.5]

    def test_top_to_bottom_ties_break_by_center_x_ascending(self):
        # Same center-y (50), center-x 100 vs 20 -> x ascending.
        raw = [
            raw_entry([90.0, 0.0, 110.0, 100.0], 0.5),
            raw_entry([10.0, 0.0, 30.0, 100.0], 0.4),
        ]
        built = detections.build_detection_list(raw, "top_to_bottom")
        assert [entry["confidence"] for entry in built] == [0.4, 0.5]

    def test_bottom_to_top_ties_break_by_center_x_ascending(self):
        raw = [
            raw_entry([90.0, 0.0, 110.0, 100.0], 0.5),
            raw_entry([10.0, 0.0, 30.0, 100.0], 0.4),
        ]
        built = detections.build_detection_list(raw, "bottom_to_top")
        assert [entry["confidence"] for entry in built] == [0.4, 0.5]

    def test_confidence_ties_break_by_left_to_right(self):
        # Equal confidence; center-x 100 vs 20 -> left_to_right.
        raw = [
            raw_entry([90.0, 0.0, 110.0, 100.0], 0.5, label="right"),
            raw_entry([10.0, 0.0, 30.0, 100.0], 0.5, label="left"),
        ]
        built = detections.build_detection_list(raw, "confidence_desc")
        assert [entry["label"] for entry in built] == ["left", "right"]

    def test_unknown_sort_order_defaults_to_left_to_right(self):
        raw = [
            raw_entry([90.0, 0.0, 110.0, 100.0], 0.5, label="right"),
            raw_entry([10.0, 0.0, 30.0, 100.0], 0.4, label="left"),
        ]
        built = detections.build_detection_list(raw, "bogus_order")
        assert [entry["label"] for entry in built] == ["left", "right"]

    def test_label_is_stringified_class_label_and_none_becomes_empty(self):
        raw = [
            {"class_index": 7, "class_label": 7,
             "bounding_box": [0.0, 0.0, 1.0, 1.0], "confidence": 0.5},
            {"class_index": 0, "class_label": None,
             "bounding_box": [2.0, 0.0, 3.0, 1.0], "confidence": 0.4},
        ]
        built = detections.build_detection_list(raw, "left_to_right")
        assert [entry["label"] for entry in built] == ["7", ""]

    def test_malformed_entries_are_skipped(self):
        raw = [
            raw_entry([0.0, 0.0, 1.0, 1.0], 0.5),
            {"class_label": "bad", "bounding_box": [1.0, 2.0], "confidence": 0.4},
            "not a dict",
            {"class_label": "bad", "bounding_box": None, "confidence": 0.4},
        ]
        built = detections.build_detection_list(raw, "left_to_right")
        assert len(built) == 1
        assert built[0]["confidence"] == 0.5

    def test_id_collision_is_redrawn(self):
        # First two draws collide; the third is unique. The injected rng
        # exercises the same re-draw loop the uuid4 path uses.
        colliding_rng = mock.Mock()
        colliding_rng.getrandbits.side_effect = [
            0xAAAAAAAA, 0xAAAAAAAA, 0xBBBBBBBB,
        ]
        raw = [
            raw_entry([0.0, 0.0, 1.0, 1.0], 0.5),
            raw_entry([2.0, 0.0, 3.0, 1.0], 0.4),
        ]
        built = detections.build_detection_list(
            raw, "left_to_right", rng=colliding_rng
        )
        assert [entry["id"] for entry in built] == ["aaaaaaaa", "bbbbbbbb"]
        assert colliding_rng.getrandbits.call_count == 3

    def test_empty_raw_builds_empty_list(self):
        assert detections.build_detection_list([], "left_to_right") == []


def graph_document(sort_order=None, include_model_node=True):
    """A minimal registration workflow.json-shaped graph document."""
    parameters = {"model_id": "yolo-world-blue-plate"}
    if sort_order is not None:
        parameters["detection_sort_order"] = sort_order
    nodes = [
        {"id": "camera_1", "type": "aravis_camera_source",
         "position": {"x": 0, "y": 0}, "parameters": {}},
    ]
    if include_model_node:
        nodes.append(
            {"id": "model_1", "type": "model_inference",
             "position": {"x": 200, "y": 0}, "parameters": parameters}
        )
    return {"nodes": nodes, "connections": []}


class TestResolveSortOrder:
    def test_configured_value_is_returned(self):
        document = graph_document(sort_order="confidence_desc")
        assert detections.resolve_sort_order(document) == "confidence_desc"

    def test_absent_parameter_defaults(self):
        assert detections.resolve_sort_order(graph_document()) == "left_to_right"

    def test_unknown_value_defaults(self):
        document = graph_document(sort_order="diagonal")
        assert detections.resolve_sort_order(document) == "left_to_right"

    def test_no_model_inference_node_defaults(self):
        document = graph_document(include_model_node=False)
        assert detections.resolve_sort_order(document) == "left_to_right"

    def test_missing_document_defaults(self):
        assert detections.resolve_sort_order(None) == "left_to_right"


class TestMergeDetections:
    def test_merges_real_thor1_detections_and_count(self):
        tag_values = {"is_anomalous": False, "confidence": 0.97}
        detections.merge_detections(
            tag_values, FIXTURES_DIR, THOR1_CAPTURE_ID, graph_document(), {}
        )
        assert len(tag_values["detections"]) == 3
        assert tag_values["detection_count"] == 3
        # Pre-existing TAG-produced keys untouched.
        assert tag_values["is_anomalous"] is False
        assert tag_values["confidence"] == 0.97

    def test_zero_detections_merge_empty_list_and_zero_count(self):
        # Requirement 1.5: "ran with no detections" is distinguishable
        # from "no detection model in the graph".
        tag_values = {}
        detections.merge_detections(
            tag_values, FIXTURES_DIR, THOR1_EMPTY_CAPTURE_ID, graph_document(), {}
        )
        assert tag_values["detections"] == []
        assert tag_values["detection_count"] == 0

    def test_absent_record_leaves_tag_values_unchanged(self, tmp_path):
        # Requirement 1.8: no record -> keys absent, run proceeds.
        tag_values = {"is_anomalous": True}
        cache = {}
        detections.merge_detections(
            tag_values, str(tmp_path), "no-capture", graph_document(), cache
        )
        assert tag_values == {"is_anomalous": True}
        assert cache == {}

    def test_absent_detections_block_leaves_tag_values_unchanged(self, tmp_path):
        # Requirement 1.8: a record with no detections block (non-detection
        # model) leaves the metadata keys absent; the run proceeds.
        record = read_fixture_record(THOR1_CAPTURE_ID)
        record["deviceFleetAuxiliaryOutputs"] = [
            entry
            for entry in record["deviceFleetAuxiliaryOutputs"]
            if entry.get("observedContentType") != "json_with_base64_encoding"
        ]
        output_dir = write_record(tmp_path, "no-block", record)
        tag_values = {"is_anomalous": True}
        cache = {}
        detections.merge_detections(
            tag_values, output_dir, "no-block", graph_document(), cache
        )
        assert tag_values == {"is_anomalous": True}
        assert cache == {}

    def test_never_overwrites_existing_keys(self):
        sentinel_list = [{"id": "keepme"}]
        tag_values = {"detections": sentinel_list, "detection_count": 1}
        detections.merge_detections(
            tag_values, FIXTURES_DIR, THOR1_CAPTURE_ID, graph_document(), {}
        )
        assert tag_values["detections"] is sentinel_list
        assert tag_values["detection_count"] == 1

    def test_builds_once_and_caches_on_run_state(self):
        cache = {}
        first, second = {}, {}
        detections.merge_detections(
            first, FIXTURES_DIR, THOR1_CAPTURE_ID, graph_document(), cache
        )
        detections.merge_detections(
            second, FIXTURES_DIR, THOR1_CAPTURE_ID, graph_document(), cache
        )
        # Identical entries AND identical Detection_IDs: the list was
        # built exactly once (design Property 1 groundwork).
        assert first["detections"] is second["detections"]
        assert first["detections"] == second["detections"]

    def test_cached_list_survives_record_removal(self, tmp_path):
        # Once built, consumers never re-read the record: copy the real
        # fixture, merge, delete the file, merge again from the cache.
        record = read_fixture_record(THOR1_CAPTURE_ID)
        output_dir = write_record(tmp_path, "run1", copy.deepcopy(record))
        cache = {}
        first = {}
        detections.merge_detections(
            first, output_dir, "run1", graph_document(), cache
        )
        os.remove(os.path.join(output_dir, "run1.jsonl"))
        second = {}
        detections.merge_detections(
            second, output_dir, "run1", graph_document(), cache
        )
        assert second["detections"] == first["detections"]
        assert second["detection_count"] == 3

    def test_sort_order_resolved_from_graph_document(self):
        tag_values = {}
        detections.merge_detections(
            tag_values,
            FIXTURES_DIR,
            THOR1_CAPTURE_ID,
            graph_document(sort_order="confidence_desc"),
            {},
        )
        confidences = [e["confidence"] for e in tag_values["detections"]]
        assert confidences == sorted(confidences, reverse=True)
