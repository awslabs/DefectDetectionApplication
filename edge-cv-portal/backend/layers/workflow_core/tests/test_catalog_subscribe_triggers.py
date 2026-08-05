"""Example unit tests for the subscribe trigger descriptors
(trigger-activation-runtime task 1.2).

Pins the descriptor content of the two new subscribe-side trigger node
types: category/ports, the shared activation policy parameter family
with its ``"name=value"`` gating strings, the OPC UA subscription
interval and mode/poll gating, and the device/sim mappings.

Validates: Requirements 1.1, 1.3, 1.4, 1.5, 2.1, 2.4, 2.6
"""

import pytest

from workflow_core.catalog import (
    ARCHITECTURES,
    DEVICE_ARCHITECTURES,
    CATEGORY_TRIGGER,
    PORT_TYPE_EVENT_SIGNAL,
    get_node_type,
)


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


# --------------------------------------------------------------------------
# Requirements 1.1, 2.1: identity — CATEGORY_TRIGGER, zero input ports,
# exactly one EventSignal output port named "out".
# --------------------------------------------------------------------------

class TestTriggerDescriptorIdentity:
    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_category_and_ports(self, type_id):
        descriptor = get_node_type(type_id)
        assert descriptor is not None, type_id
        assert descriptor.type_id == type_id
        assert descriptor.category == CATEGORY_TRIGGER
        assert descriptor.inputs == []
        assert [(port.name, port.port_type) for port in descriptor.outputs] == [
            ("out", PORT_TYPE_EVENT_SIGNAL)]


# --------------------------------------------------------------------------
# Requirements 1.3, 1.4, 2.4 (policy family on both descriptors):
# concurrency_policy + gated queue_depth/debounce_ms, retry_limit,
# priority — identical types, defaults, constraints, and gating.
# --------------------------------------------------------------------------

class TestTriggerPolicyParameterFamily:
    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_concurrency_policy(self, type_id):
        params = _params_by_name(get_node_type(type_id))
        policy = params["concurrency_policy"]
        assert policy.param_type == "enum"
        assert policy.required is False
        assert policy.default == "queue"
        assert policy.constraints == {"values": ["queue", "drop", "debounce"]}
        assert policy.depends_on is None

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_queue_depth_gated_on_queue_selection(self, type_id):
        params = _params_by_name(get_node_type(type_id))
        queue_depth = params["queue_depth"]
        assert queue_depth.param_type == "int"
        assert queue_depth.required is False
        assert queue_depth.default == 10
        assert queue_depth.constraints == {"min": 1, "max": 1000}
        # Dependent_Parameter_Gating "name=value" form (Requirement 1.3).
        assert queue_depth.depends_on == "concurrency_policy=queue"

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_debounce_ms_gated_on_debounce_selection(self, type_id):
        params = _params_by_name(get_node_type(type_id))
        debounce_ms = params["debounce_ms"]
        assert debounce_ms.param_type == "int"
        assert debounce_ms.required is False
        assert debounce_ms.default == 500
        assert debounce_ms.constraints == {"min": 1, "max": 60000}
        assert debounce_ms.depends_on == "concurrency_policy=debounce"

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_retry_limit(self, type_id):
        # Requirement 1.4: 0 = retry forever (the default sentinel).
        params = _params_by_name(get_node_type(type_id))
        retry_limit = params["retry_limit"]
        assert retry_limit.param_type == "int"
        assert retry_limit.required is False
        assert retry_limit.default == 0
        assert retry_limit.constraints == {"min": 0, "max": 1000}
        assert retry_limit.depends_on is None
        assert "retry forever" in retry_limit.description

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_priority(self, type_id):
        # Requirement 1.4: lower value = higher priority.
        params = _params_by_name(get_node_type(type_id))
        priority = params["priority"]
        assert priority.param_type == "int"
        assert priority.required is False
        assert priority.default == 100
        assert priority.constraints == {"min": 0, "max": 1000}
        assert priority.depends_on is None
        assert "lower value = higher priority" in priority.description


# --------------------------------------------------------------------------
# Requirement 2.2 (interval) and 2.4 (mode/poll gating) on
# opcua_subscribe.
# --------------------------------------------------------------------------

class TestOpcuaSubscribeIntervalAndMode:
    def test_sampling_interval_ms(self):
        params = _params_by_name(get_node_type("opcua_subscribe"))
        sampling = params["sampling_interval_ms"]
        assert sampling.param_type == "int"
        assert sampling.required is False
        assert sampling.default == 100
        assert sampling.constraints == {"min": 10, "max": 60000}
        assert sampling.depends_on is None

    def test_mode_enum(self):
        params = _params_by_name(get_node_type("opcua_subscribe"))
        mode = params["mode"]
        assert mode.param_type == "enum"
        assert mode.required is False
        assert mode.default == "subscribe"
        assert mode.constraints == {"values": ["subscribe", "poll"]}
        assert mode.depends_on is None

    def test_poll_interval_ms_gated_on_poll_selection(self):
        params = _params_by_name(get_node_type("opcua_subscribe"))
        poll_interval = params["poll_interval_ms"]
        assert poll_interval.param_type == "int"
        assert poll_interval.required is False
        assert poll_interval.default == 500
        assert poll_interval.constraints == {"min": 10, "max": 60000}
        # Dependent_Parameter_Gating "name=value" form (Requirement 2.4).
        assert poll_interval.depends_on == "mode=poll"

    def test_mqtt_subscribe_has_no_opcua_only_parameters(self):
        params = _params_by_name(get_node_type("mqtt_subscribe"))
        for name in ("sampling_interval_ms", "mode", "poll_interval_ms"):
            assert name not in params


# --------------------------------------------------------------------------
# Requirements 1.5, 2.6: device mappings (executor bindings + plugin
# dependencies), the ARCH_SIM appsrc stub mirroring digital_input, and
# hardware_dependent=True.
# --------------------------------------------------------------------------

class TestTriggerMappings:
    EXPECTED = {
        "mqtt_subscribe": ("mqtt_subscribe",
                           ["python:paho-mqtt", "python:awsiotsdk"]),
        "opcua_subscribe": ("opcua_subscribe", ["python:opcua"]),
    }

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_device_mappings_are_executor_bindings(self, type_id):
        descriptor = get_node_type(type_id)
        binding, plugin_deps = self.EXPECTED[type_id]
        assert {m.arch for m in descriptor.mappings} == set(ARCHITECTURES)
        for arch in DEVICE_ARCHITECTURES:
            mapping = descriptor.mapping_for(arch)
            assert mapping is not None, arch
            assert mapping.executor_binding == binding, arch
            assert mapping.plugin_dependencies == plugin_deps, arch
            # Executor-level trigger: no GStreamer element chain on device.
            assert mapping.element_chain == [], arch

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_sim_mapping_is_the_digital_input_appsrc_stub(self, type_id):
        # The sim stub mirrors digital_input's appsrc simulation stub
        # form exactly (Requirements 1.5, 2.6).
        sim = get_node_type(type_id).mapping_for("sim")
        digital_sim = get_node_type("digital_input").mapping_for("sim")
        assert sim is not None
        assert sim == digital_sim
        assert sim.element_chain == [
            {"factory": "appsrc",
             "args_template": {"name": "{sim_source_name}"}},
        ]
        assert sim.plugin_dependencies == ["app"]
        assert sim.executor_binding is None

    @pytest.mark.parametrize("type_id", ["mqtt_subscribe", "opcua_subscribe"])
    def test_hardware_dependent(self, type_id):
        assert get_node_type(type_id).hardware_dependent is True
