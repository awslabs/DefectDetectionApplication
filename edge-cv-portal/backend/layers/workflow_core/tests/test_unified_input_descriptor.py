"""Example unit tests for the Unified_Input_Node descriptor (task 3.2).

Pins the ``unified_input`` descriptor added by the
triggers-stage-and-unified-input feature: category, ``source_kind`` enum,
ports, the per-source_kind parameter-subset equivalence against the four
retained source descriptors, and the coexistence/unchanged guarantees on
the retained source and output descriptors.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.8, 6.4
"""

from workflow_core.catalog import (
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    get_node_type,
)
from workflow_core.catalog.nodes import SOURCE_KIND_TO_SOURCE_TYPE


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


def _ports(ports):
    return [(port.name, port.port_type) for port in ports]


# --------------------------------------------------------------------------
# Requirement 3.1, 3.3: descriptor identity and the source_kind enum
# --------------------------------------------------------------------------

class TestUnifiedInputIdentity:
    def test_descriptor_present_in_input_category(self):
        # Requirement 3.1: unified_input lives in CATEGORY_INPUT.
        descriptor = get_node_type("unified_input")
        assert descriptor is not None
        assert descriptor.type_id == "unified_input"
        assert descriptor.category == CATEGORY_INPUT

    def test_source_kind_enum_parameterization(self):
        # Requirement 3.1: required source_kind enum selecting among the
        # four retained (non-digital) sources.
        params = _params_by_name(get_node_type("unified_input"))
        source_kind = params["source_kind"]
        assert source_kind.required is True
        assert source_kind.param_type == "enum"
        assert source_kind.constraints["values"] == [
            "csi_camera", "icam", "aravis_camera", "folder"]
        # NOTE: the implemented descriptor defaults to "folder" (the
        # refined requirement text for 3.1 says "csi_camera"; the code
        # deliberately uses "folder" — asserting the implemented value
        # and flagging the requirement-text discrepancy here).
        assert source_kind.default == "folder"

    def test_source_kind_offers_no_digital_option(self):
        # Requirement 3.3: digital input is a trigger, never a selectable
        # source_kind.
        params = _params_by_name(get_node_type("unified_input"))
        values = params["source_kind"].constraints["values"]
        assert "digital" not in values
        assert "digital_input" not in values
        assert "digital_input" not in SOURCE_KIND_TO_SOURCE_TYPE
        assert "digital_input" not in SOURCE_KIND_TO_SOURCE_TYPE.values()


# --------------------------------------------------------------------------
# Requirements 3.5, 3.8: ports
# --------------------------------------------------------------------------

class TestUnifiedInputPorts:
    def test_single_video_frames_output_named_out(self):
        # Requirement 3.5: exactly one VideoFrames output port named "out",
        # matching the retained source descriptors' output.
        descriptor = get_node_type("unified_input")
        assert _ports(descriptor.outputs) == [("out", PORT_TYPE_VIDEO_FRAMES)]

    def test_single_event_signal_activation_input(self):
        # Requirement 3.8: exactly one optional EventSignal activation
        # input port.
        descriptor = get_node_type("unified_input")
        assert _ports(descriptor.inputs) == [
            ("activation", PORT_TYPE_EVENT_SIGNAL)]


# --------------------------------------------------------------------------
# Requirement 3.4: per-source_kind parameter-subset equivalence
# --------------------------------------------------------------------------

class TestUnifiedInputParameterUnion:
    def test_per_source_kind_subset_matches_underlying_descriptor(self):
        # Requirement 3.4: for each source_kind, the unified node's gated
        # parameter subset (the underlying descriptor's parameter names,
        # looked up on the unified descriptor) equals the underlying
        # source descriptor's parameters on name, param_type, default,
        # and constraints. The only permitted difference is the union
        # copies being required-relaxed (required=False), because V4
        # cannot express "required only when source_kind == X".
        unified_params = _params_by_name(get_node_type("unified_input"))
        for source_kind, source_type in SOURCE_KIND_TO_SOURCE_TYPE.items():
            source = get_node_type(source_type)
            assert source is not None, source_type
            for source_param in source.parameters:
                unified_param = unified_params.get(source_param.name)
                assert unified_param is not None, (
                    source_kind, source_param.name)
                assert unified_param.param_type == source_param.param_type, (
                    source_kind, source_param.name)
                assert unified_param.default == source_param.default, (
                    source_kind, source_param.name)
                assert unified_param.constraints == source_param.constraints, (
                    source_kind, source_param.name)
                # required is the single permitted difference: always
                # relaxed to False on the unified copies.
                assert unified_param.required is False, (
                    source_kind, source_param.name)

    def test_union_carries_no_stray_parameters(self):
        # The unified parameters are exactly source_kind plus the union
        # of the four source descriptors' parameter names — nothing else.
        unified_names = {p.name for p in get_node_type("unified_input").parameters}
        expected = {"source_kind"}
        for source_type in SOURCE_KIND_TO_SOURCE_TYPE.values():
            expected |= {p.name for p in get_node_type(source_type).parameters}
        assert unified_names == expected


# --------------------------------------------------------------------------
# Requirement 3.2: the four source descriptors are retained unchanged
# --------------------------------------------------------------------------

class TestRetainedSourceDescriptors:
    def test_four_sources_present_with_identity_and_ports(self):
        # Requirement 3.2: the retained sources keep their type_id,
        # CATEGORY_INPUT category, no input ports, and the single
        # VideoFrames "out" output.
        for source_type in ("csi_camera_source", "icam_source",
                            "aravis_camera_source", "folder_source"):
            descriptor = get_node_type(source_type)
            assert descriptor is not None, source_type
            assert descriptor.type_id == source_type
            assert descriptor.category == CATEGORY_INPUT, source_type
            assert descriptor.inputs == [], source_type
            assert _ports(descriptor.outputs) == [
                ("out", PORT_TYPE_VIDEO_FRAMES)], source_type


# --------------------------------------------------------------------------
# Requirement 6.4: mqtt_publish / opcua_write output descriptors unchanged
# --------------------------------------------------------------------------

class TestOutputDescriptorsUnchanged:
    def test_mqtt_publish_category_ports_and_parameter_names(self):
        descriptor = get_node_type("mqtt_publish")
        assert descriptor is not None
        assert descriptor.category == CATEGORY_OUTPUT
        assert _ports(descriptor.inputs) == [("in", PORT_TYPE_INFERENCE_META)]
        assert descriptor.outputs == []
        assert [p.name for p in descriptor.parameters] == [
            "broker_host", "broker_port", "topic", "payload_template",
            "qos", "greengrass", "aws_iot", "iot_thing_name",
            "iot_ca_cert_path", "iot_client_cert_path",
            "iot_private_key_path"]

    def test_opcua_write_category_ports_and_parameter_names(self):
        descriptor = get_node_type("opcua_write")
        assert descriptor is not None
        assert descriptor.category == CATEGORY_OUTPUT
        assert _ports(descriptor.inputs) == [("in", PORT_TYPE_INFERENCE_META)]
        assert descriptor.outputs == []
        assert [p.name for p in descriptor.parameters] == [
            "endpoint", "node_id", "value_template", "username",
            "password", "security_policy", "security_mode",
            "client_cert_path", "client_key_path", "server_cert_path"]
