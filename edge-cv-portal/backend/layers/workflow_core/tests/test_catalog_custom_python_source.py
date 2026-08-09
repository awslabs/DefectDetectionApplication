"""Catalog content unit tests for the Custom Python source descriptor
(custom-python-source task 1.2).

The ``custom_python_source`` node type is a ``CATEGORY_INPUT`` node whose
user-authored ``produce_frame(context)`` supplies the run's frame. Its
device-architecture realization is byte-for-byte the Aravis camera
source's appsrc-headed chain (the executor's single-frame Frame_Feed
model), and the catalog change is strictly additive.

Validates: custom-python-source Requirements 1.1, 1.2, 1.3, 1.4, 1.5,
1.6, 1.7, 1.8, 5.5, 11.4
"""

from workflow_core.catalog import (
    ARCH_SIM,
    CATEGORY_INPUT,
    DEVICE_ARCHITECTURES,
    NODE_CATALOG,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_VIDEO_FRAMES,
    get_node_type,
)
from workflow_core.catalog.nodes import SOURCE_KIND_TO_SOURCE_TYPE

TYPE_ID = "custom_python_source"

#: The pre-feature catalog order (custom-python-source Requirement 11.4):
#: every one of these descriptors must keep its position; the new source
#: descriptor may only be appended after them.
_PRE_EXISTING_TYPE_ID_ORDER = (
    "csi_camera_source",
    "icam_source",
    "aravis_camera_source",
    "folder_source",
    "digital_input",
    "dewarp",
    "rotate",
    "crop",
    "format_convert",
    "custom_python_preprocess",
    "model_inference",
    "bedrock_inference",
    "custom_python",
    "inference_filter",
    "conditional",
    "digital_output",
    "mqtt_publish",
    "opcua_write",
    "capture",
    "llm_inference",
    "unified_input",
    "mqtt_subscribe",
    "opcua_subscribe",
    "modbus_write",
)


def _descriptor():
    descriptor = get_node_type(TYPE_ID)
    assert descriptor is not None, "custom_python_source missing from catalog"
    return descriptor


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


class TestDescriptorIdentity:
    def test_present_with_input_category_and_display_name(self):
        # Requirement 1.1
        descriptor = _descriptor()
        assert descriptor.type_id == TYPE_ID
        assert descriptor.category == CATEGORY_INPUT
        assert descriptor.display_name == "Custom Python (Source)"
        assert descriptor.hardware_dependent is True

    def test_exactly_the_declared_ports(self):
        # Requirement 1.2: exactly one activation EventSignal input and
        # one out VideoFrames output.
        descriptor = _descriptor()
        assert [(p.name, p.port_type) for p in descriptor.inputs] == [
            ("activation", PORT_TYPE_EVENT_SIGNAL)]
        assert [(p.name, p.port_type) for p in descriptor.outputs] == [
            ("out", PORT_TYPE_VIDEO_FRAMES)]


class TestParameters:
    def test_parameter_set_and_requiredness(self):
        # Requirements 1.3, 1.4: required code, optional requirements,
        # optional allowed_uri_prefixes defaulting to empty.
        params = _params_by_name(_descriptor())
        assert list(params) == ["code", "requirements", "allowed_uri_prefixes"]

        assert params["code"].param_type == "code"
        assert params["code"].required is True
        assert params["code"].default is None
        assert params["code"].constraints == {"min_length": 1}

        assert params["requirements"].param_type == "string"
        assert params["requirements"].required is False
        assert params["requirements"].default == ""

        assert params["allowed_uri_prefixes"].param_type == "string"
        assert params["allowed_uri_prefixes"].required is False
        assert params["allowed_uri_prefixes"].default == ""

    def test_code_description_documents_the_contract(self):
        # Requirement 1.7: the description names produce_frame, the MQTT
        # and OPC UA Trigger_Context keys, and the dda_frames helpers.
        description = _params_by_name(_descriptor())["code"].description
        assert "produce_frame" in description
        for key in ("topic", "payload", "payload_json", "qos", "timestamp"):
            assert key in description, key
        for key in ("endpoint", "node_id", "value", "source_timestamp"):
            assert key in description, key
        assert "dda_frames" in description
        assert "load_image" in description
        assert "load_bytes" in description

    def test_every_code_example_defines_a_callable_produce_frame(self):
        # Requirement 1.7: examples are usable verbatim — each exec's to
        # a module defining a callable produce_frame.
        code_param = _params_by_name(_descriptor())["code"]
        assert code_param.examples, "code parameter has no examples"
        for example in code_param.examples:
            assert "produce_frame" in example
            namespace = {}
            exec(compile(example, "<example>", "exec"), namespace)
            assert callable(namespace.get("produce_frame")), (
                "example does not define a callable produce_frame: "
                "{0!r}".format(example))

    def test_allowed_uri_prefixes_description(self):
        # Requirements 1.4, 5.5: newline-separated prefixes; the
        # description states the restriction is not a sandbox boundary.
        description = _params_by_name(
            _descriptor())["allowed_uri_prefixes"].description
        assert "newline" in description.lower()
        assert "not a sandbox boundary" in description


class TestMappings:
    def test_device_arch_chain_equals_the_aravis_appsrc_chain(self):
        # Requirement 1.5: on every device architecture the element chain
        # and plugin dependencies are byte-for-byte the Aravis source's
        # appsrc-headed chain.
        descriptor = _descriptor()
        aravis = get_node_type("aravis_camera_source")
        for arch in DEVICE_ARCHITECTURES:
            mapping = descriptor.mapping_for(arch)
            aravis_mapping = aravis.mapping_for(arch)
            assert mapping is not None, arch
            assert mapping.element_chain == aravis_mapping.element_chain, arch
            assert mapping.element_chain == [
                {"factory": "appsrc",
                 "args_template": {"name": "appsrc_{nodeId}"}},
                {"factory": "videoconvert", "args_template": {}},
            ], arch
            assert mapping.plugin_dependencies == ["app", "videoconvertscale"], arch
            assert mapping.executor_binding is None, arch

    def test_simulation_mapping_exists_and_is_dataset_fed(self):
        # Requirement 1.6: the sim mapping is the same dataset-fed stub
        # the other hardware frame sources use, so sandbox test runs
        # compile and run.
        sim = _descriptor().mapping_for(ARCH_SIM)
        assert sim is not None
        assert sim == get_node_type("aravis_camera_source").mapping_for(ARCH_SIM)


class TestCatalogDiscipline:
    def test_not_a_unified_input_source_kind(self):
        # Requirement 1.8: SOURCE_KIND_TO_SOURCE_TYPE is unchanged.
        assert TYPE_ID not in SOURCE_KIND_TO_SOURCE_TYPE
        assert TYPE_ID not in SOURCE_KIND_TO_SOURCE_TYPE.values()
        assert SOURCE_KIND_TO_SOURCE_TYPE == {
            "csi_camera": "csi_camera_source",
            "icam": "icam_source",
            "aravis_camera": "aravis_camera_source",
            "folder": "folder_source",
        }

    def test_catalog_addition_is_a_pure_append(self):
        # Requirement 11.4: additivity as a prefix-order assertion — every
        # pre-existing descriptor keeps its position; the new descriptor
        # is appended after them.
        catalog_order = tuple(d.type_id for d in NODE_CATALOG)
        prefix = catalog_order[:len(_PRE_EXISTING_TYPE_ID_ORDER)]
        assert prefix == _PRE_EXISTING_TYPE_ID_ORDER
        assert TYPE_ID in catalog_order[len(_PRE_EXISTING_TYPE_ID_ORDER):]
