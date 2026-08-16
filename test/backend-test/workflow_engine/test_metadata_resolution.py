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
"""Unit tests for the pure metadata resolution functions in
``output_bindings.py`` (workflow-manager-gaps Requirements 7.2, 7.3,
7.4, 7.5, 7.6, 7.9).

Pure-function tests: no processor, no clients, no hardware. Property
coverage lives in test/backend-test/output_bindings_metadata/ (task 4.2).
"""
import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.output_bindings import (
    BINDING_METADATA,
    attached_metadata_by_output,
    resolve_field_path,
    resolve_metadata_binding,
)


def metadata_binding(node_id="meta-1", mappings=(), static=None, attach_to=()):
    return {
        "nodeId": node_id,
        "binding": BINDING_METADATA,
        "parameters": {},
        "metadataMappings": [
            {"fieldPath": path, "key": key} for path, key in mappings
        ],
        "staticJson": dict(static or {}),
        "attachTo": list(attach_to),
    }


def trigger_with_payload(payload_json):
    return {
        "topic": "swagfactory/invoke",
        "payload": "<raw>",
        "payload_json": payload_json,
        "qos": 1,
        "timestamp": "2025-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# resolve_field_path (Requirements 7.2, 7.3)
# ---------------------------------------------------------------------------

class TestResolveFieldPath:
    def test_top_level_key(self):
        assert resolve_field_path({"job_id": "J-42"}, "job_id") == (True, "J-42")

    def test_nested_objects(self):
        doc = {"order": {"meta": {"job_id": 7}}}
        assert resolve_field_path(doc, "order.meta.job_id") == (True, 7)

    def test_numeric_segment_indexes_lists(self):
        doc = {"items": [{"id": "a"}, {"id": "b"}]}
        assert resolve_field_path(doc, "items.1.id") == (True, "b")

    def test_resolved_null_is_found(self):
        found, value = resolve_field_path({"job_id": None}, "job_id")
        assert found is True
        assert value is None

    def test_missing_key_not_found(self):
        assert resolve_field_path({"a": 1}, "b") == (False, None)

    def test_missing_nested_key_not_found(self):
        assert resolve_field_path({"a": {"b": 1}}, "a.c") == (False, None)

    def test_traversal_through_scalar_not_found(self):
        assert resolve_field_path({"a": 5}, "a.b") == (False, None)

    def test_non_numeric_list_segment_not_found(self):
        assert resolve_field_path({"a": [1, 2]}, "a.x") == (False, None)

    def test_out_of_range_index_not_found(self):
        assert resolve_field_path({"a": [1, 2]}, "a.2") == (False, None)
        assert resolve_field_path({"a": [1, 2]}, "a.-1") == (False, None)

    def test_empty_or_non_string_path_not_found(self):
        assert resolve_field_path({"a": 1}, "") == (False, None)
        assert resolve_field_path({"a": 1}, "   ") == (False, None)
        assert resolve_field_path({"a": 1}, None) == (False, None)


# ---------------------------------------------------------------------------
# resolve_metadata_binding (Requirements 7.2-7.6, 7.9)
# ---------------------------------------------------------------------------

class TestResolveMetadataBinding:
    def test_mappings_resolved_from_payload(self):
        binding = metadata_binding(
            mappings=[("job_id", "job_id"), ("meta.station", "station")])
        trigger = trigger_with_payload(
            {"job_id": "J-42", "meta": {"station": "line-1"}})
        assert resolve_metadata_binding(binding, trigger) == {
            "job_id": "J-42", "station": "line-1",
        }

    def test_resolved_null_attached(self):
        binding = metadata_binding(mappings=[("job_id", "job_id")])
        trigger = trigger_with_payload({"job_id": None})
        assert resolve_metadata_binding(binding, trigger) == {"job_id": None}

    def test_unresolved_path_omitted(self, caplog):
        binding = metadata_binding(
            mappings=[("job_id", "job_id"), ("missing", "gone")])
        trigger = trigger_with_payload({"job_id": "J-42"})
        with caplog.at_level("INFO"):
            attached = resolve_metadata_binding(binding, trigger)
        assert attached == {"job_id": "J-42"}
        assert any("did not resolve" in r.message for r in caplog.records)

    def test_static_entries_attached_alongside_mappings(self):
        binding = metadata_binding(
            mappings=[("job_id", "job_id")], static={"station": "line-1"})
        trigger = trigger_with_payload({"job_id": "J-42"})
        assert resolve_metadata_binding(binding, trigger) == {
            "job_id": "J-42", "station": "line-1",
        }

    def test_resolved_mapping_overrides_static_with_logged_collision(
        self, caplog
    ):
        binding = metadata_binding(
            mappings=[("job_id", "job_id")], static={"job_id": "static"})
        trigger = trigger_with_payload({"job_id": "resolved"})
        with caplog.at_level("INFO"):
            attached = resolve_metadata_binding(binding, trigger)
        assert attached == {"job_id": "resolved"}
        assert any("collision" in r.message for r in caplog.records)

    def test_unresolved_mapping_keeps_static_entry(self):
        binding = metadata_binding(
            mappings=[("missing", "job_id")], static={"job_id": "static"})
        trigger = trigger_with_payload({"other": 1})
        assert resolve_metadata_binding(binding, trigger) == {
            "job_id": "static",
        }

    def test_non_json_payload_degrades_to_static_only_one_log_line(
        self, caplog
    ):
        binding = metadata_binding(
            mappings=[("a", "a"), ("b", "b")], static={"station": "line-1"})
        trigger = trigger_with_payload(None)  # payload did not parse as JSON
        with caplog.at_level("INFO"):
            attached = resolve_metadata_binding(binding, trigger)
        assert attached == {"station": "line-1"}
        degrade_lines = [
            r for r in caplog.records
            if "attaching static JSON metadata only" in r.message
        ]
        assert len(degrade_lines) == 1

    def test_non_object_json_payload_degrades_to_static_only(self):
        binding = metadata_binding(
            mappings=[("0", "first")], static={"station": "line-1"})
        trigger = trigger_with_payload(["not", "an", "object"])
        assert resolve_metadata_binding(binding, trigger) == {
            "station": "line-1",
        }

    def test_absent_trigger_context_degrades_to_static_only(self):
        binding = metadata_binding(
            mappings=[("job_id", "job_id")], static={"station": "line-1"})
        assert resolve_metadata_binding(binding, {}) == {"station": "line-1"}
        assert resolve_metadata_binding(binding, None) == {"station": "line-1"}

    def test_static_only_binding_without_trigger(self):
        binding = metadata_binding(static={"station": "line-1"})
        assert resolve_metadata_binding(binding, {}) == {"station": "line-1"}

    def test_empty_binding_yields_empty_map(self):
        assert resolve_metadata_binding(metadata_binding(), {}) == {}

    def test_never_raises_on_malformed_binding(self):
        assert resolve_metadata_binding(None, {}) == {}
        assert resolve_metadata_binding({"staticJson": "not-a-dict"}, {}) == {}
        assert resolve_metadata_binding(
            {"metadataMappings": "not-a-list"}, {}) == {}
        malformed = {
            "metadataMappings": [None, {"fieldPath": "a"}, {"key": ""}],
            "staticJson": {"k": 1},
        }
        trigger = trigger_with_payload({"a": 1})
        assert resolve_metadata_binding(malformed, trigger) == {"k": 1}


# ---------------------------------------------------------------------------
# attached_metadata_by_output
# ---------------------------------------------------------------------------

class TestAttachedMetadataByOutput:
    def test_fans_out_to_attach_to_outputs(self):
        bindings = [
            metadata_binding(
                mappings=[("job_id", "job_id")],
                attach_to=["out-1", "out-2"]),
            {"nodeId": "mqtt-1", "binding": "mqtt_publish", "parameters": {}},
        ]
        trigger = trigger_with_payload({"job_id": "J-42"})
        assert attached_metadata_by_output(bindings, trigger) == {
            "out-1": {"job_id": "J-42"},
            "out-2": {"job_id": "J-42"},
        }

    def test_non_metadata_bindings_ignored(self):
        bindings = [
            {"nodeId": "mqtt-1", "binding": "mqtt_publish", "parameters": {}},
        ]
        assert attached_metadata_by_output(bindings, {}) == {}
        assert attached_metadata_by_output(None, None) == {}
        assert attached_metadata_by_output([], {}) == {}

    def test_later_binding_wins_on_shared_output(self, caplog):
        bindings = [
            metadata_binding(
                node_id="meta-1", static={"k": "first", "only1": 1},
                attach_to=["out-1"]),
            metadata_binding(
                node_id="meta-2", static={"k": "second", "only2": 2},
                attach_to=["out-1"]),
        ]
        with caplog.at_level("INFO"):
            result = attached_metadata_by_output(bindings, {})
        assert result == {
            "out-1": {"k": "second", "only1": 1, "only2": 2},
        }
        assert any(
            "later binding wins" in r.message for r in caplog.records)

    def test_output_maps_are_independent_copies(self):
        bindings = [
            metadata_binding(static={"k": 1}, attach_to=["out-1", "out-2"]),
        ]
        result = attached_metadata_by_output(bindings, {})
        result["out-1"]["extra"] = True
        assert "extra" not in result["out-2"]

    def test_empty_attached_map_still_fans_out(self):
        # A Metadata_Node that resolves nothing (Req 7.9: empty metadata)
        # still marks its attachTo outputs as downstream of a metadata
        # node, with an empty map.
        bindings = [metadata_binding(attach_to=["out-1"])]
        assert attached_metadata_by_output(bindings, {}) == {"out-1": {}}

    def test_missing_or_invalid_attach_to_ignored(self):
        binding = metadata_binding(static={"k": 1})
        del binding["attachTo"]
        assert attached_metadata_by_output([binding], {}) == {}
        binding["attachTo"] = "not-a-list"
        assert attached_metadata_by_output([binding], {}) == {}
