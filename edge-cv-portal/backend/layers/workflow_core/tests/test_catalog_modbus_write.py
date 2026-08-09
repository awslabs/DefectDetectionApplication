"""Unit tests for the ``modbus_write`` catalog descriptor (task 1.4).

Feature: modbus-tcp-output. Asserts the descriptor's identity, ports,
every parameter shape (including the ``pulse_ms``
``depends_on="register_type=coil"`` gating string), the device
architecture mappings (executor binding ``modbus_write`` with zero
plugin dependencies), the ``sim`` recording stub
(``recording_modbus_write``), ``hardware_dependent``, the appended
catalog list position, and the baseline delta scope (the recorded
``catalog_baseline.json`` covers exactly the live catalog and its
``modbus_write`` entry equals the live descriptor).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.3
"""

import dataclasses
import json
import os

from workflow_core.catalog import (
    ARCH_SIM,
    CATEGORY_OUTPUT,
    DEVICE_ARCHITECTURES,
    NODE_CATALOG,
    PORT_TYPE_INFERENCE_META,
    SIM_RECORDING_BINDING_PREFIX,
    get_node_type,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _descriptor():
    descriptor = get_node_type("modbus_write")
    assert descriptor is not None
    return descriptor


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


class TestModbusWriteIdentityAndPorts:
    """Requirement 1.1: identity, category, ports."""

    def test_identity_and_category(self):
        descriptor = _descriptor()
        assert descriptor.type_id == "modbus_write"
        assert descriptor.category == CATEGORY_OUTPUT
        assert descriptor.display_name == "Modbus TCP Write"

    def test_exactly_one_inference_meta_input_and_zero_outputs(self):
        descriptor = _descriptor()
        assert len(descriptor.inputs) == 1
        assert descriptor.inputs[0].name == "in"
        assert descriptor.inputs[0].port_type == PORT_TYPE_INFERENCE_META
        assert descriptor.outputs == []


class TestModbusWriteParameters:
    """Requirements 1.2-1.5: every parameter's declared shape."""

    def test_parameter_names_in_order(self):
        descriptor = _descriptor()
        assert [p.name for p in descriptor.parameters] == [
            "host", "port", "unit_id", "register_type", "address",
            "value_template", "pulse_ms",
        ]

    def test_host(self):
        param = _params_by_name(_descriptor())["host"]
        assert param.param_type == "string"
        assert param.required is True
        assert param.default is None
        assert param.constraints == {"min_length": 1}

    def test_port(self):
        param = _params_by_name(_descriptor())["port"]
        assert param.param_type == "int"
        assert param.required is False
        assert param.default == 502
        assert param.constraints == {"min": 1, "max": 65535}

    def test_unit_id(self):
        param = _params_by_name(_descriptor())["unit_id"]
        assert param.param_type == "int"
        assert param.required is False
        assert param.default == 1
        assert param.constraints == {"min": 0, "max": 255}

    def test_register_type(self):
        param = _params_by_name(_descriptor())["register_type"]
        assert param.param_type == "enum"
        assert param.required is True
        assert param.default == "coil"
        assert param.constraints == {"values": ["coil", "holding_register"]}

    def test_address(self):
        param = _params_by_name(_descriptor())["address"]
        assert param.param_type == "int"
        assert param.required is True
        assert param.default is None
        assert param.constraints == {"min": 0, "max": 65535}

    def test_value_template(self):
        param = _params_by_name(_descriptor())["value_template"]
        assert param.param_type == "string"
        assert param.required is False
        assert param.default == "{is_anomalous}"
        assert param.constraints == {}
        # The description documents the opcua_write placeholder set plus
        # the per-target write coercion (Requirement 1.4).
        for token in ("{is_anomalous}", "{confidence}", "{inference_json}",
                      "native type", "boolean", "integer 0-65535"):
            assert token in param.description, token

    def test_pulse_ms(self):
        param = _params_by_name(_descriptor())["pulse_ms"]
        assert param.param_type == "int"
        assert param.required is False
        assert param.default == 0
        assert param.constraints == {"min": 0, "max": 60000}
        # Visible only while register_type is coil, via the existing
        # "name=value" dependent-gating form (Requirement 1.5).
        assert param.depends_on == "register_type=coil"

    def test_only_pulse_ms_is_gated(self):
        for param in _descriptor().parameters:
            if param.name != "pulse_ms":
                assert param.depends_on is None, param.name


class TestModbusWriteMappings:
    """Requirement 1.6: device mappings + sim recording stub."""

    def test_device_mappings_bind_modbus_write_with_zero_plugin_deps(self):
        descriptor = _descriptor()
        device_mappings = [m for m in descriptor.mappings
                           if m.arch != ARCH_SIM]
        assert [m.arch for m in device_mappings] == list(DEVICE_ARCHITECTURES)
        for mapping in device_mappings:
            assert mapping.executor_binding == "modbus_write"
            assert mapping.element_chain == []
            assert mapping.plugin_dependencies == []

    def test_sim_mapping_is_the_recording_stub(self):
        descriptor = _descriptor()
        sim_mappings = [m for m in descriptor.mappings if m.arch == ARCH_SIM]
        assert len(sim_mappings) == 1
        stub = sim_mappings[0]
        assert stub.executor_binding == \
            SIM_RECORDING_BINDING_PREFIX + "modbus_write"
        assert stub.executor_binding == "recording_modbus_write"
        assert stub.element_chain == []
        assert stub.plugin_dependencies == []

    def test_hardware_dependent(self):
        assert _descriptor().hardware_dependent is True


class TestModbusWriteCatalogPositionAndBaseline:
    """Requirements 2.1, 2.3: appended last; baseline delta scope."""

    def test_appended_after_every_pre_modbus_descriptor(self):
        # modbus_write was appended after every descriptor that predates
        # it (Requirement 2.1); later additive features (e.g.
        # custom_python_source) may only append *after* it, so the
        # durable assertion is positional: modbus_write keeps its place
        # right after the 23 pre-modbus descriptors.
        order = [d.type_id for d in NODE_CATALOG]
        assert order.index("modbus_write") == 23
        # Exactly one catalog entry carries the type id.
        assert order.count("modbus_write") == 1

    def test_baseline_delta_scoped_to_the_new_descriptor(self):
        # The recorded baseline covers exactly the live catalog (nothing
        # dropped, nothing extra) and its modbus_write entry equals the
        # live descriptor's canonical serialization — so the baseline
        # update is scoped to the appended descriptor while
        # test_bug_catalog_preservation.py keeps pinning every
        # pre-existing entry byte-identical (Requirement 2.3).
        with open(os.path.join(_HERE, "catalog_baseline.json"),
                  encoding="utf-8") as fh:
            baseline = json.load(fh)
        assert set(baseline) == {d.type_id for d in NODE_CATALOG}
        assert baseline["modbus_write"] == \
            dataclasses.asdict(get_node_type("modbus_write"))
