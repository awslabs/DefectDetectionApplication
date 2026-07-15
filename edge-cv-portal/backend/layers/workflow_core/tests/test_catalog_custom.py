"""Unit tests for workflow_core.catalog.custom (custom-node-designer task 1.2).

Covers declaration -> descriptor conversion (valid round trip, each
invalid-field rejection with the offending field named, the DeepStream
architecture restriction) and merged catalog resolution (duplicate
type_id rejection with built-ins winning, result ordering).

The exhaustive valid/invalid declaration property is task 1.3; these
tests pin the concrete behavior with examples.

_Requirements: 5.3, 8.2, 8.5, 8.6_
"""

import copy

import pytest

from workflow_core.catalog import (
    ARCH_ARM64_JP5,
    ARCH_X86_64,
    DEEPSTREAM_ARCHITECTURES,
    NODE_CATALOG,
    DeclarationError,
    GstMapping,
    NodeTypeDescriptor,
    ParameterDescriptor,
    PortDescriptor,
    descriptor_from_declaration,
    resolve_catalog,
)


def valid_declaration(**overrides):
    """The design document's Custom_Node_Type declaration example
    (wire JSON), with the field-level help Requirement 8.1 collects."""
    decl = {
        "typeId": "custom.blur_regions",
        "category": "preprocessing",
        "displayName": "Blur Regions",
        "inputs": [{"name": "in", "portType": "VideoFrames"}],
        "outputs": [{"name": "out", "portType": "VideoFrames"}],
        "parameters": [
            {
                "name": "radius",
                "paramType": "int",
                "required": True,
                "default": 5,
                "constraints": {"min": 1, "max": 64},
                "description": "Blur radius in pixels.",
                "examples": [5, 9],
            }
        ],
        "mappings": [
            {
                "arch": "x86_64",
                "elementChain": [
                    {"factory": "blurregions", "argsTemplate": {"radius": "{radius}"}}
                ],
                "pluginDependencies": ["custom:uc-123/blurregions"],
            }
        ],
        "hardwareDependent": False,
        # Extra keys the stored declaration may carry are ignored.
        "typeVersion": 1,
        "lifecycleState": "test",
    }
    decl.update(overrides)
    return decl


class TestValidDeclarationConversion:
    def test_round_trip_of_the_design_example(self):
        descriptor = descriptor_from_declaration(valid_declaration())

        assert isinstance(descriptor, NodeTypeDescriptor)
        assert descriptor.type_id == "custom.blur_regions"
        assert descriptor.category == "preprocessing"
        assert descriptor.display_name == "Blur Regions"
        assert descriptor.hardware_dependent is False

        assert descriptor.inputs == [PortDescriptor("in", "VideoFrames")]
        assert descriptor.outputs == [PortDescriptor("out", "VideoFrames")]

        assert descriptor.parameters == [
            ParameterDescriptor(
                name="radius",
                param_type="int",
                required=True,
                default=5,
                constraints={"min": 1, "max": 64},
                depends_on=None,
                description="Blur radius in pixels.",
                examples=[5, 9],
            )
        ]

        assert descriptor.mappings == [
            GstMapping(
                arch="x86_64",
                element_chain=[
                    {"factory": "blurregions", "args_template": {"radius": "{radius}"}}
                ],
                executor_binding=None,
                plugin_dependencies=["custom:uc-123/blurregions"],
            )
        ]
        # Plugin dependency recorded so the compiler includes the plugin
        # per Target_Architecture (Requirement 8.6).
        assert descriptor.mapping_for("x86_64").plugin_dependencies == [
            "custom:uc-123/blurregions"
        ]

    def test_descriptor_is_frozen(self):
        descriptor = descriptor_from_declaration(valid_declaration())
        with pytest.raises(Exception):
            descriptor.type_id = "other"

    def test_wire_constraint_keys_convert_to_python_keys(self):
        decl = valid_declaration(
            parameters=[
                {
                    "name": "label",
                    "paramType": "string",
                    "required": False,
                    "default": "ok",
                    "constraints": {"minLength": 1, "maxLength": 8},
                    "description": "Overlay label text.",
                    "examples": ["ok", "defect"],
                }
            ]
        )
        descriptor = descriptor_from_declaration(decl)
        assert descriptor.parameters[0].constraints == {"min_length": 1, "max_length": 8}

    def test_deepstream_declaration_with_jetpack_mappings_is_accepted(self):
        decl = valid_declaration(
            deepstream=True,
            mappings=[
                {
                    "arch": arch,
                    "elementChain": [{"factory": "nvblur", "argsTemplate": {}}],
                    "pluginDependencies": ["custom:uc-123/nvblur"],
                }
                for arch in DEEPSTREAM_ARCHITECTURES
            ],
        )
        descriptor = descriptor_from_declaration(decl)
        assert [m.arch for m in descriptor.mappings] == list(DEEPSTREAM_ARCHITECTURES)


class TestInvalidFieldRejection:
    """Every rejection raises DeclarationError naming the offending field
    (Requirement 8.5)."""

    def assert_rejected(self, decl, field):
        with pytest.raises(DeclarationError) as excinfo:
            descriptor_from_declaration(decl)
        assert excinfo.value.field == field
        assert field in str(excinfo.value)

    def test_missing_type_id(self):
        self.assert_rejected(valid_declaration(typeId=None), "typeId")

    def test_missing_display_name(self):
        self.assert_rejected(valid_declaration(displayName=""), "displayName")

    def test_unknown_category(self):
        self.assert_rejected(valid_declaration(category="filters"), "category")

    def test_unknown_input_port_type(self):
        decl = valid_declaration(
            inputs=[{"name": "in", "portType": "AudioFrames"}]
        )
        self.assert_rejected(decl, "inputs[0].portType")

    def test_unknown_output_port_type(self):
        decl = valid_declaration(
            outputs=[{"name": "out", "portType": "Bogus"}]
        )
        self.assert_rejected(decl, "outputs[0].portType")

    def test_missing_port_name(self):
        decl = valid_declaration(inputs=[{"name": "", "portType": "VideoFrames"}])
        self.assert_rejected(decl, "inputs[0].name")

    def test_unknown_parameter_type(self):
        decl = valid_declaration()
        decl["parameters"][0]["paramType"] = "decimal"
        self.assert_rejected(decl, "parameters[0].paramType")

    def test_default_violating_its_own_constraints(self):
        decl = valid_declaration()
        decl["parameters"][0]["default"] = 200  # constraints max is 64
        self.assert_rejected(decl, "parameters[0].default")

    def test_default_violating_its_own_type(self):
        decl = valid_declaration()
        decl["parameters"][0]["default"] = "five"
        self.assert_rejected(decl, "parameters[0].default")

    def test_example_violating_constraints(self):
        decl = valid_declaration()
        decl["parameters"][0]["examples"] = [5, 500]
        self.assert_rejected(decl, "parameters[0].examples[1]")

    def test_missing_parameter_description(self):
        decl = valid_declaration()
        del decl["parameters"][0]["description"]
        self.assert_rejected(decl, "parameters[0].description")

    def test_empty_examples(self):
        decl = valid_declaration()
        decl["parameters"][0]["examples"] = []
        self.assert_rejected(decl, "parameters[0].examples")

    def test_enum_without_values_constraint(self):
        decl = valid_declaration()
        decl["parameters"][0].update(
            paramType="enum", default=None, constraints={}, examples=["a"]
        )
        self.assert_rejected(decl, "parameters[0].constraints.values")

    def test_duplicate_parameter_names(self):
        decl = valid_declaration()
        decl["parameters"].append(copy.deepcopy(decl["parameters"][0]))
        self.assert_rejected(decl, "parameters[1].name")

    def test_depends_on_must_name_a_bool_parameter(self):
        decl = valid_declaration()
        decl["parameters"][0]["dependsOn"] = "no_such_toggle"
        self.assert_rejected(decl, "parameters[0].dependsOn")

    def test_unknown_mapping_architecture(self):
        decl = valid_declaration()
        decl["mappings"][0]["arch"] = "riscv"
        self.assert_rejected(decl, "mappings[0].arch")

    def test_duplicate_mapping_architecture(self):
        decl = valid_declaration()
        decl["mappings"].append(copy.deepcopy(decl["mappings"][0]))
        self.assert_rejected(decl, "mappings[1].arch")

    def test_element_chain_entry_without_factory(self):
        decl = valid_declaration()
        decl["mappings"][0]["elementChain"][0]["factory"] = ""
        self.assert_rejected(decl, "mappings[0].elementChain[0].factory")

    def test_non_dict_declaration(self):
        self.assert_rejected(["not", "a", "declaration"], "declaration")


class TestDeepStreamRestriction:
    """DeepStream-flagged declarations are restricted to arm64_jp4/jp5/jp6
    mappings (Requirement 5.3)."""

    def test_deepstream_architectures_are_the_jetpack_builds(self):
        assert DEEPSTREAM_ARCHITECTURES == ("arm64_jp4", "arm64_jp5", "arm64_jp6")

    def test_deepstream_x86_64_mapping_rejected(self):
        decl = valid_declaration(deepstream=True)  # mapping is x86_64
        with pytest.raises(DeclarationError) as excinfo:
            descriptor_from_declaration(decl)
        assert excinfo.value.field == "mappings[0].arch"

    def test_non_deepstream_x86_64_mapping_accepted(self):
        descriptor = descriptor_from_declaration(valid_declaration(deepstream=False))
        assert descriptor.mapping_for(ARCH_X86_64) is not None


class TestResolveCatalog:
    def _custom(self, type_id):
        return descriptor_from_declaration(valid_declaration(typeId=type_id))

    def test_empty_custom_set_returns_the_builtin_catalog(self):
        assert resolve_catalog(()) == NODE_CATALOG

    def test_ordering_builtins_first_then_customs_in_given_order(self):
        first = self._custom("custom.first")
        second = self._custom("custom.second")
        resolved = resolve_catalog([first, second])
        assert isinstance(resolved, tuple)
        assert resolved[: len(NODE_CATALOG)] == NODE_CATALOG
        assert resolved[len(NODE_CATALOG):] == (first, second)

    def test_custom_duplicate_of_builtin_is_rejected_builtin_wins(self):
        builtin_id = NODE_CATALOG[0].type_id
        impostor = self._custom(builtin_id)
        resolved = resolve_catalog([impostor])
        assert resolved == NODE_CATALOG
        # The built-in descriptor itself survives, not the custom one.
        assert resolved[0] is NODE_CATALOG[0]
        assert impostor not in resolved

    def test_duplicate_among_customs_keeps_the_first(self):
        first = self._custom("custom.dup")
        second = self._custom("custom.dup")
        other = self._custom("custom.other")
        resolved = resolve_catalog([first, second, other])
        customs = resolved[len(NODE_CATALOG):]
        assert customs == (first, other)

    def test_resolved_catalog_serves_custom_types_alongside_builtins(self):
        # Requirement 8.2: the merged catalog includes the Custom_Node_Type
        # with the same declaration structure as built-in node types.
        custom = self._custom("custom.jp5_only")
        resolved = resolve_catalog([custom])
        by_id = {d.type_id: d for d in resolved}
        assert by_id["custom.jp5_only"] is custom
        assert isinstance(custom.mapping_for(ARCH_X86_64), GstMapping)
        assert custom.mapping_for(ARCH_ARM64_JP5) is None
