"""Catalog content and additivity for the stream camera and scene
analytics node types (rtsp-rtmp-stream-cameras task 2.3).

Pins the five appended descriptors — ``rtsp_camera_source``,
``rtmp_stream_source``, ``detection_counter``, ``object_association`` and
``event_gate`` — and, above all, pins the append as *additive*: every
descriptor that predates the feature keeps its catalog position and its
content byte-for-byte, and the only pre-existing entry the feature touches
(``unified_input``) changes only by appending.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 13.1, 14.1, 15.1,
18.1
"""

import dataclasses
import json
import os

from workflow_core.catalog import (
    ARCHITECTURES,
    ARCH_SIM,
    CATEGORY_INPUT,
    CATEGORY_POST_PROCESSING,
    CONDITION_EXAMPLES,
    CONDITION_LANGUAGE_DESCRIPTION,
    DEVICE_ARCHITECTURES,
    LOCALSERVER_BUNDLED_PLUGINS,
    NODE_CATALOG,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    get_node_type,
)
from workflow_core.catalog.nodes import (
    _STREAM_SOURCE_PARAMETERS,
    SOURCE_KIND_TO_SOURCE_TYPE,
)
from workflow_core.analytics.scene import (
    DEFAULT_ACTIVATE_AFTER,
    DEFAULT_CLEAR_AFTER,
    DEFAULT_EMIT,
    DEFAULT_MIN_OVERLAP,
    DEFAULT_ZONE_RULE,
    EMIT_MODES,
    ZONE_RULES,
)
from workflow_core.validator import check_parameter_value


_HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_HERE, "catalog_baseline.json"), encoding="utf-8") as _fh:
    BASELINE = json.load(_fh)

#: The catalog order exactly as it stood before this feature — the 26
#: descriptors the append must leave in place (Requirement 1.7). Recorded as
#: a literal so a reordering of ``NODE_CATALOG`` fails here rather than
#: silently moving a node in every palette.
PRE_FEATURE_TYPE_ID_ORDER = (
    "csi_camera_source", "icam_source", "aravis_camera_source",
    "folder_source",
    "digital_input",
    "dewarp", "rotate", "crop", "format_convert",
    "custom_python_preprocess",
    "model_inference", "bedrock_inference",
    "custom_python", "inference_filter", "conditional",
    "digital_output", "mqtt_publish", "opcua_write", "capture",
    "llm_inference",
    "unified_input",
    "mqtt_subscribe", "opcua_subscribe",
    "modbus_write",
    "custom_python_source",
    "metadata",
)

#: The five appended type ids, in the order the design fixes for them
#: (design "Catalog order and the frontend mirror").
APPENDED_TYPE_IDS = (
    "rtsp_camera_source", "rtmp_stream_source",
    "detection_counter", "object_association", "event_gate",
)

STREAM_TYPE_IDS = ("rtsp_camera_source", "rtmp_stream_source")
ANALYTICS_TYPE_IDS = ("detection_counter", "object_association", "event_gate")

#: ``mqtt_publish`` is the one descriptor whose recorded baseline entry is
#: deliberately NOT the live serialization: it is the pre-Bug-2 recording
#: that ``test_bug_catalog_preservation.py`` compares against field by field
#: (``TestMqttPublishPreservation``), so a byte-identity check here would
#: assert the opposite of what that suite documents. Its preservation is
#: pinned there; excluded from the content sweep below for that reason
#: alone.
_STALE_BASELINE_TYPE_ID = "mqtt_publish"


def _params(descriptor):
    return {param.name: param for param in descriptor.parameters}


def _param_names(descriptor):
    return [param.name for param in descriptor.parameters]


def _ports(ports):
    return [(port.name, port.port_type) for port in ports]


# --------------------------------------------------------------------------
# Requirements 1.7, 18.1: the append is additive
# --------------------------------------------------------------------------

class TestCatalogAppendIsAdditive:
    """Every pre-existing descriptor keeps its position and its content;
    the new types are appended after all of them."""

    def test_pre_feature_order_is_an_unchanged_prefix(self):
        # Requirement 1.7: positional additivity. The pre-feature order is
        # a prefix of the live order — nothing moved, nothing dropped, and
        # nothing was inserted between existing entries.
        order = tuple(d.type_id for d in NODE_CATALOG)
        assert order[:len(PRE_FEATURE_TYPE_ID_ORDER)] == \
            PRE_FEATURE_TYPE_ID_ORDER

    def test_new_types_are_appended_after_every_pre_existing_entry(self):
        # Requirement 1.7: the five new descriptors follow `metadata`, the
        # last pre-feature entry, in the order the design fixes.
        order = tuple(d.type_id for d in NODE_CATALOG)
        assert order[len(PRE_FEATURE_TYPE_ID_ORDER):] == APPENDED_TYPE_IDS
        assert order.index("rtsp_camera_source") == order.index("metadata") + 1
        for type_id in APPENDED_TYPE_IDS:
            assert order.count(type_id) == 1, type_id

    def test_catalog_is_exactly_the_pre_feature_set_plus_the_new_types(self):
        assert {d.type_id for d in NODE_CATALOG} == \
            set(PRE_FEATURE_TYPE_ID_ORDER) | set(APPENDED_TYPE_IDS)

    def test_every_pre_existing_descriptor_content_is_byte_identical(self):
        # Requirements 1.7, 18.1: content additivity. Every descriptor that
        # predates the feature serializes exactly as recorded in
        # catalog_baseline.json — display name, ports, parameters,
        # mappings, category and hardware_dependent all unchanged. The
        # single intentional pre-existing delta (``unified_input``, whose
        # baseline entry the feature regenerated) is scoped by
        # TestUnifiedInputDeltaIsAppendOnly below; ``mqtt_publish`` is
        # excluded for the documented stale-recording reason.
        for descriptor in NODE_CATALOG:
            if descriptor.type_id in APPENDED_TYPE_IDS:
                continue
            if descriptor.type_id == _STALE_BASELINE_TYPE_ID:
                continue
            assert dataclasses.asdict(descriptor) == \
                BASELINE[descriptor.type_id], descriptor.type_id

    def test_baseline_covers_exactly_the_live_catalog(self):
        # The regenerated baseline gained the five new entries and nothing
        # else went missing, so the preservation suite keeps sampling over
        # the whole catalog.
        assert set(BASELINE) == {d.type_id for d in NODE_CATALOG}

    def test_baseline_entries_for_the_new_types_match_the_live_descriptors(self):
        # The baseline delta is scoped to the appended descriptors (the
        # same discipline test_catalog_modbus_write.py applies to its own
        # append): each new entry equals the live serialization.
        for type_id in APPENDED_TYPE_IDS:
            assert BASELINE[type_id] == \
                dataclasses.asdict(get_node_type(type_id)), type_id


# --------------------------------------------------------------------------
# Requirement 1.6: the unified input delta is append-only
# --------------------------------------------------------------------------

class TestUnifiedInputDeltaIsAppendOnly:
    """The only pre-existing descriptor this feature touches changes only
    by appending: two source kinds and the stream parameter family."""

    #: The unified parameter union exactly as it stood before the feature:
    #: ``source_kind`` plus the four original source kinds' parameters, in
    #: source-kind order, de-duplicated by name.
    PRE_FEATURE_UNION = (
        "source_kind",
        "gain", "exposure",     # csi_camera_source
        "device",               # icam_source
        "camera_id",            # aravis_camera_source (gain/exposure dedup)
        "location", "file_pattern",   # folder_source
    )

    def test_source_kind_map_appends_the_two_stream_kinds(self):
        # Requirement 1.6: the map gains rtsp_camera -> rtsp_camera_source
        # and rtmp_stream -> rtmp_stream_source, appended after the four
        # pre-existing kinds, which keep their position and target.
        assert list(SOURCE_KIND_TO_SOURCE_TYPE.items()) == [
            ("csi_camera", "csi_camera_source"),
            ("icam", "icam_source"),
            ("aravis_camera", "aravis_camera_source"),
            ("folder", "folder_source"),
            ("rtsp_camera", "rtsp_camera_source"),
            ("rtmp_stream", "rtmp_stream_source"),
        ]

    def test_source_kind_enum_values_mirror_the_map(self):
        # The offered enum values are the map's keys, in the map's order,
        # so the palette and the compiler expansion cannot drift.
        source_kind = _params(get_node_type("unified_input"))["source_kind"]
        assert source_kind.constraints["values"] == \
            list(SOURCE_KIND_TO_SOURCE_TYPE)
        # Only the constraint's value list moved: type, requiredness and
        # default are as recorded.
        baseline = next(p for p in BASELINE["unified_input"]["parameters"]
                        if p["name"] == "source_kind")
        assert source_kind.param_type == baseline["param_type"]
        assert source_kind.required == baseline["required"]
        assert source_kind.default == baseline["default"]

    def test_union_order_is_the_pre_feature_prefix_plus_the_stream_family(self):
        # Requirement 1.6 / design D1: the stream family's parameter names
        # are all new, so every pre-existing unified parameter keeps its
        # position, and the six stream parameters are appended in the
        # shared family's own order.
        names = _param_names(get_node_type("unified_input"))
        assert tuple(names[:len(self.PRE_FEATURE_UNION)]) == \
            self.PRE_FEATURE_UNION
        assert names[len(self.PRE_FEATURE_UNION):] == \
            [p.name for p in _STREAM_SOURCE_PARAMETERS]

    def test_pre_existing_union_parameters_are_byte_identical(self):
        # The appended tail is the only change: each pre-feature union
        # parameter still serializes exactly as recorded.
        current = {p["name"]: p for p in
                   [dataclasses.asdict(p) for p in
                    get_node_type("unified_input").parameters]}
        baseline = {p["name"]: p
                    for p in BASELINE["unified_input"]["parameters"]}
        for name in self.PRE_FEATURE_UNION:
            if name == "source_kind":
                continue  # the one intentional delta, scoped above
            assert current[name] == baseline[name], name

    def test_stream_union_parameters_are_the_family_required_relaxed(self):
        # The union copies differ from the shared family only in
        # ``required`` (relaxed to False, as for every other source kind).
        union = {p.name: p for p in get_node_type("unified_input").parameters}
        for param in _STREAM_SOURCE_PARAMETERS:
            copy = union[param.name]
            assert copy.required is False, param.name
            assert dataclasses.asdict(
                dataclasses.replace(param, required=False)) == \
                dataclasses.asdict(copy), param.name

    def test_unified_input_ports_and_mappings_unchanged(self):
        descriptor = get_node_type("unified_input")
        baseline = BASELINE["unified_input"]
        assert [dataclasses.asdict(p) for p in descriptor.inputs] == \
            baseline["inputs"]
        assert [dataclasses.asdict(p) for p in descriptor.outputs] == \
            baseline["outputs"]
        assert descriptor.mappings == []
        assert baseline["mappings"] == []
        assert descriptor.category == baseline["category"]
        assert descriptor.display_name == baseline["display_name"]
        assert descriptor.hardware_dependent == baseline["hardware_dependent"]


# --------------------------------------------------------------------------
# Requirements 1.1-1.4: the two stream camera source descriptors
# --------------------------------------------------------------------------

class TestStreamSourceDescriptors:
    def test_identity_category_and_ports(self):
        # Requirement 1.1: display names, input category, one activation
        # EventSignal input and exactly one "out" VideoFrames output.
        expected_display_names = {
            "rtsp_camera_source": "RTSP Camera",
            "rtmp_stream_source": "RTMP Stream",
        }
        for type_id in STREAM_TYPE_IDS:
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.type_id == type_id
            assert descriptor.category == CATEGORY_INPUT, type_id
            assert descriptor.display_name == \
                expected_display_names[type_id], type_id
            assert _ports(descriptor.inputs) == [
                ("activation", PORT_TYPE_EVENT_SIGNAL)], type_id
            assert _ports(descriptor.outputs) == [
                ("out", PORT_TYPE_VIDEO_FRAMES)], type_id
            # Requirement 1.4: hardware dependent (the LocalServer
            # Stream_Ingest_Service acquires the frames).
            assert descriptor.hardware_dependent is True, type_id

    def test_both_types_share_one_parameter_family_object_for_object(self):
        # Design D1: the two descriptors reference the very same
        # ParameterDescriptor objects, so they cannot drift and the
        # unified union de-duplicates them exactly once.
        rtsp = get_node_type("rtsp_camera_source").parameters
        rtmp = get_node_type("rtmp_stream_source").parameters
        assert list(_STREAM_SOURCE_PARAMETERS) == rtsp
        for shared, from_rtsp, from_rtmp in zip(
                _STREAM_SOURCE_PARAMETERS, rtsp, rtmp):
            assert from_rtsp is shared
            assert from_rtmp is shared

    def test_parameter_family_shape(self):
        # Requirement 1.2: the declared parameters, their order, types,
        # defaults, ranges and continuous-only gating.
        params = _params(get_node_type("rtsp_camera_source"))
        assert _param_names(get_node_type("rtsp_camera_source")) == [
            "url", "processing_mode", "frames_per_second",
            "max_frame_age_ms", "keep_recent_runs", "keep_notable_runs"]

        url = params["url"]
        assert url.param_type == "string"
        assert url.required is True
        assert url.default is None
        assert url.constraints["min_length"] == 1
        assert url.constraints["max_length"] == 2048
        assert url.depends_on is None

        mode = params["processing_mode"]
        assert mode.param_type == "enum"
        assert mode.required is False
        assert mode.default == "continuous"
        assert mode.constraints == {"values": ["continuous", "on_trigger"]}

        fps = params["frames_per_second"]
        assert fps.param_type == "float"
        assert fps.required is False
        assert fps.default == 1.0
        assert fps.constraints == {"min": 0.05, "max": 10.0}

        age = params["max_frame_age_ms"]
        assert age.param_type == "int"
        assert age.required is False
        assert age.default == 2000
        assert age.constraints == {"min": 100, "max": 60000}
        # Applies to both processing modes, so it is never gated.
        assert age.depends_on is None

        recent = params["keep_recent_runs"]
        assert recent.param_type == "int"
        assert recent.required is False
        assert recent.default == 20
        assert recent.constraints == {"min": 1, "max": 200}

        notable = params["keep_notable_runs"]
        assert notable.param_type == "int"
        assert notable.required is False
        assert notable.default == 200
        assert notable.constraints == {"min": 0, "max": 5000}

    def test_continuous_only_parameters_are_gated_on_processing_mode(self):
        # Requirement 1.2: frames_per_second, keep_recent_runs and
        # keep_notable_runs are visible only while processing_mode is
        # continuous (the "name=value" depends_on form).
        params = _params(get_node_type("rtmp_stream_source"))
        for name in ("frames_per_second", "keep_recent_runs",
                     "keep_notable_runs"):
            assert params[name].depends_on == "processing_mode=continuous", name

    def test_every_parameter_documents_itself_with_working_examples(self):
        # Requirement 1.2: a description and at least one example that
        # satisfies the parameter's own constraints, on every parameter of
        # both types.
        for type_id in STREAM_TYPE_IDS:
            for param in get_node_type(type_id).parameters:
                context = (type_id, param.name)
                assert isinstance(param.description, str)
                assert param.description.strip(), context
                assert isinstance(param.examples, list) and param.examples, \
                    context
                for example in param.examples:
                    assert check_parameter_value(param, example) is None, \
                        (context, example)

    def test_url_constraint_rejects_foreign_scheme_no_host_and_user_info(self):
        # Requirement 1.3: the catalog's own url constraint (its regex)
        # rejects a non-stream scheme, a URL with no host, and embedded
        # user information — before any validator rule runs.
        url = _params(get_node_type("rtsp_camera_source"))["url"]
        for accepted in ("rtsp://192.168.1.64:554/Streaming/Channels/101",
                         "rtsps://cam.local/stream",
                         "rtmp://media.local/live/line1",
                         "rtmps://media.local:443/live/line1"):
            assert check_parameter_value(url, accepted) is None, accepted
        for rejected in ("http://cam.local/stream",       # foreign scheme
                         "file:///tmp/clip.mp4",
                         "rtsp:///Streaming/Channels/101",  # no host
                         "rtsp://",
                         "rtsp://admin:secret@cam.local/s",  # user info
                         "rtmp://user@media.local/live"):
            assert check_parameter_value(url, rejected) is not None, rejected

    def test_url_regex_is_the_shared_stream_url_pattern(self):
        # The constraint carries the single source of truth the Portal
        # frontend mirrors, not a second hand-written pattern.
        from workflow_core.stream_url import STREAM_URL_PATTERN

        for type_id in STREAM_TYPE_IDS:
            url = _params(get_node_type(type_id))["url"]
            assert url.constraints["regex"] == STREAM_URL_PATTERN, type_id

    def test_device_arch_mappings_are_the_appsrc_chain(self):
        # Requirement 1.4: every physical device architecture renders
        # appsrc name=appsrc_{nodeId} ! videoconvert with the app and
        # videoconvertscale plugin dependencies, and no executor binding.
        for type_id in STREAM_TYPE_IDS:
            descriptor = get_node_type(type_id)
            assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
            for arch in DEVICE_ARCHITECTURES:
                mapping = descriptor.mapping_for(arch)
                assert mapping is not None, (type_id, arch)
                assert mapping.element_chain == [
                    {"factory": "appsrc",
                     "args_template": {"name": "appsrc_{nodeId}"}},
                    {"factory": "videoconvert", "args_template": {}},
                ], (type_id, arch)
                assert mapping.plugin_dependencies == [
                    "app", "videoconvertscale"], (type_id, arch)
                assert mapping.executor_binding is None, (type_id, arch)

    def test_plugin_dependencies_are_localserver_bundled(self):
        # Design: every declared dependency (``app`` and
        # ``videoconvertscale`` on the device chains, the dataset stub's on
        # sim) is already bundled on that architecture, so the compiled
        # pluginDependencies stay empty and packaging ships no plugin.
        for type_id in STREAM_TYPE_IDS:
            for mapping in get_node_type(type_id).mappings:
                bundled = LOCALSERVER_BUNDLED_PLUGINS[mapping.arch]
                for dependency in mapping.plugin_dependencies:
                    assert dependency in bundled, \
                        (type_id, mapping.arch, dependency)

    def test_sim_mapping_is_the_shared_dataset_fed_stub(self):
        # Requirement 1.4: the sim mapping is the shared dataset-fed stub,
        # byte-equal to the other frame sources' sim mapping.
        reference = get_node_type("aravis_camera_source").mapping_for(ARCH_SIM)
        for type_id in STREAM_TYPE_IDS:
            assert get_node_type(type_id).mapping_for(ARCH_SIM) == reference, \
                type_id

    def test_no_parameter_appears_in_any_element_argument(self):
        # Requirement 1.4: no binding slots — the packaged binding point
        # carries the rendered parameters instead, so a credential-bearing
        # value can never reach a launch string.
        for type_id in STREAM_TYPE_IDS:
            descriptor = get_node_type(type_id)
            names = {p.name for p in descriptor.parameters}
            for mapping in descriptor.mappings:
                for element in mapping.element_chain:
                    for value in (element.get("args_template") or {}).values():
                        if isinstance(value, str):
                            assert value.strip("{}") not in names, \
                                (type_id, value)


# --------------------------------------------------------------------------
# Requirements 13.1, 14.1, 15.1: the three scene analytics descriptors
# --------------------------------------------------------------------------

class TestSceneAnalyticsDescriptors:
    def test_identity_category_and_ports(self):
        expected_display_names = {
            "detection_counter": "Detection Counter",
            "object_association": "Object Association",
            "event_gate": "Event Gate",
        }
        for type_id in ANALYTICS_TYPE_IDS:
            descriptor = get_node_type(type_id)
            assert descriptor is not None, type_id
            assert descriptor.type_id == type_id
            assert descriptor.category == CATEGORY_POST_PROCESSING, type_id
            assert descriptor.display_name == \
                expected_display_names[type_id], type_id
            assert _ports(descriptor.inputs) == [
                ("in", PORT_TYPE_INFERENCE_META)], type_id
            assert _ports(descriptor.outputs) == [
                ("out", PORT_TYPE_INFERENCE_META)], type_id
            # The rules live in the shared analytics module that the
            # device bindings and the cloud sandbox both run, so the
            # sandbox needs no recording stub.
            assert descriptor.hardware_dependent is False, type_id

    def test_mappings_are_the_same_executor_binding_on_every_arch(self):
        for type_id in ANALYTICS_TYPE_IDS:
            descriptor = get_node_type(type_id)
            assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
            for mapping in descriptor.mappings:
                assert mapping.element_chain == [], (type_id, mapping.arch)
                assert mapping.executor_binding == type_id, \
                    (type_id, mapping.arch)
                assert mapping.plugin_dependencies == [], \
                    (type_id, mapping.arch)

    def test_detection_counter_parameterization(self):
        # Requirement 13.1.
        descriptor = get_node_type("detection_counter")
        assert _param_names(descriptor) == [
            "classes", "min_confidence", "zone", "zone_rule"]
        params = _params(descriptor)
        assert params["classes"].param_type == "string"
        assert params["classes"].required is False
        assert params["classes"].default == ""
        assert params["min_confidence"].param_type == "float"
        assert params["min_confidence"].default == 0.0
        assert params["min_confidence"].constraints == {"min": 0.0, "max": 1.0}
        assert params["zone"].param_type == "string"
        assert params["zone"].required is False
        assert params["zone"].default == ""
        assert params["zone_rule"].param_type == "enum"
        assert params["zone_rule"].required is False
        assert params["zone_rule"].constraints == {"values": list(ZONE_RULES)}
        assert params["zone_rule"].constraints["values"] == \
            ["center", "overlap"]
        assert params["zone_rule"].default == DEFAULT_ZONE_RULE == "center"

    def test_object_association_parameterization(self):
        # Requirement 14.1.
        descriptor = get_node_type("object_association")
        assert _param_names(descriptor) == [
            "subject_class", "required_classes", "min_overlap",
            "min_confidence", "zone"]
        params = _params(descriptor)
        for name in ("subject_class", "required_classes"):
            assert params[name].param_type == "string", name
            assert params[name].required is True, name
            assert params[name].default is None, name
            assert params[name].constraints == {"min_length": 1}, name
        assert params["min_overlap"].param_type == "float"
        assert params["min_overlap"].required is False
        assert params["min_overlap"].default == DEFAULT_MIN_OVERLAP == 0.5
        assert params["min_overlap"].constraints == {"min": 0.05, "max": 1.0}
        assert params["min_confidence"].default == 0.0
        assert params["min_confidence"].constraints == {"min": 0.0, "max": 1.0}
        assert params["zone"].param_type == "string"
        assert params["zone"].default == ""

    def test_event_gate_parameterization(self):
        # Requirement 15.1.
        descriptor = get_node_type("event_gate")
        assert _param_names(descriptor) == [
            "condition", "activate_after", "clear_after", "emit",
            "repeat_interval_ms"]
        params = _params(descriptor)
        assert params["condition"].param_type == "string"
        assert params["condition"].required is True
        assert params["condition"].constraints == {"min_length": 1}
        for name, default in (("activate_after", DEFAULT_ACTIVATE_AFTER),
                              ("clear_after", DEFAULT_CLEAR_AFTER)):
            assert params[name].param_type == "int", name
            assert params[name].required is False, name
            assert params[name].default == default == 3, name
            assert params[name].constraints == {"min": 1, "max": 1000}, name
        emit = params["emit"]
        assert emit.param_type == "enum"
        assert emit.required is False
        assert emit.constraints == {"values": list(EMIT_MODES)}
        assert emit.constraints["values"] == [
            "on_activate", "on_change", "while_active"]
        assert emit.default == DEFAULT_EMIT == "on_activate"
        repeat = params["repeat_interval_ms"]
        assert repeat.param_type == "int"
        assert repeat.required is False
        assert repeat.default == 0
        assert repeat.constraints == {"min": 0, "max": 86400000}
        # Only meaningful while every active run passes.
        assert repeat.depends_on == "emit=while_active"

    def test_event_gate_condition_reuses_the_shared_condition_language(self):
        # The gate evaluates the same rule-expression language every other
        # executor-evaluated condition uses, so its description embeds the
        # shared text verbatim and its examples are the shared examples.
        condition = _params(get_node_type("event_gate"))["condition"]
        assert CONDITION_LANGUAGE_DESCRIPTION in condition.description
        assert condition.examples == list(CONDITION_EXAMPLES)
        shared = next(p for p in get_node_type("inference_filter").parameters
                      if p.name == "condition")
        assert condition.examples == shared.examples

    def test_every_parameter_documents_itself_with_working_examples(self):
        for type_id in ANALYTICS_TYPE_IDS:
            for param in get_node_type(type_id).parameters:
                context = (type_id, param.name)
                assert isinstance(param.description, str)
                assert param.description.strip(), context
                assert isinstance(param.examples, list) and param.examples, \
                    context
                for example in param.examples:
                    assert check_parameter_value(param, example) is None, \
                        (context, example)

    def test_zone_examples_parse_as_zones(self):
        # The documented zone examples are usable verbatim: they parse
        # through the shared analytics reader the validator (V13) uses.
        from workflow_core.analytics.scene import parse_zone

        for type_id in ("detection_counter", "object_association"):
            zone = _params(get_node_type(type_id))["zone"]
            for example in zone.examples:
                points, problems = parse_zone(example)
                assert problems == [], (type_id, example, problems)
                assert points and len(points) >= 3, (type_id, example)
