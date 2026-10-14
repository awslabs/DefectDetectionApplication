"""Catalog content tests for the detection-guided-bedrock-inspection
descriptors (task 3.2).

Pins the new ``model_inference`` parameter (``detection_sort_order``) and
the four new ``bedrock_inference`` parameters (``crop_detection_index``,
``crop_margin_percent``, ``reference_payload_path``,
``allowed_uri_prefixes``) with their exact types, defaults, and
constraints; asserts catalog additivity as a prefix-order assertion
(the pre-feature parameter order is preserved verbatim, new parameters
strictly appended); and checks the descriptions document the dotted
payload-path syntax and the five sort-order values.

Validates: detection-guided-bedrock-inspection Requirements 1.4
(descriptor half), 2.1, 2.3, 3.1, 3.4, 6.1, 6.4, 6.6
"""

from workflow_core.catalog import get_node_type


def _params_by_name(descriptor):
    return {param.name: param for param in descriptor.parameters}


#: The exact parameter order each descriptor carried BEFORE this feature.
#: The additivity contract (Requirement 6.6 posture): this list stays a
#: prefix of the live parameter order — nothing reordered, nothing
#: removed, new parameters only ever appended.
MODEL_INFERENCE_PRIOR_PARAMETERS = ["modelName"]
BEDROCK_PRIOR_PARAMETERS = [
    "model", "prompt", "anomaly_mode", "region", "max_tokens",
    "system_prompt",
]

SORT_ORDER_VALUES = [
    "left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top",
    "confidence_desc",
]


# ---------------------------------------------------------------------------
# model_inference: detection_sort_order (Requirements 1.4, 6.1)
# ---------------------------------------------------------------------------

class TestModelInferenceDetectionSortOrder:
    def test_parameterization(self):
        # Requirement 1.4 (descriptor half): optional enum, default
        # left_to_right, exactly the five glossary values in order.
        params = _params_by_name(get_node_type("model_inference"))
        sort_order = params["detection_sort_order"]
        assert sort_order.param_type == "enum"
        assert sort_order.required is False
        assert sort_order.default == "left_to_right"
        assert sort_order.constraints == {"values": SORT_ORDER_VALUES}

    def test_description_names_every_sort_order_value(self):
        # Requirement 6.1: the parameter is documented with the value
        # semantics (all five orderings named) and examples.
        sort_order = _params_by_name(
            get_node_type("model_inference"))["detection_sort_order"]
        for value in SORT_ORDER_VALUES:
            assert value in sort_order.description, value
        assert isinstance(sort_order.examples, list) and sort_order.examples

    def test_description_states_executor_read_not_compiled(self):
        # Design posture: the parameter is deliberately executor-read
        # from the registered workflow definition, never compiled into
        # the pipeline document.
        sort_order = _params_by_name(
            get_node_type("model_inference"))["detection_sort_order"]
        assert "executor" in sort_order.description
        assert "never compiled" in sort_order.description

    def test_parameter_appears_in_no_element_chain(self):
        # model_inference compiles to GStreamer elements only, so the
        # parameter must not be referenced by any argument template
        # (otherwise compilation would demand a binding slot for it).
        descriptor = get_node_type("model_inference")
        for mapping in descriptor.mappings:
            for element in mapping.element_chain:
                for value in (element.get("args_template") or {}).values():
                    if isinstance(value, str):
                        assert "{detection_sort_order}" not in value


# ---------------------------------------------------------------------------
# bedrock_inference: the four inspection parameters
# (Requirements 2.1, 2.3, 3.1, 3.4, 6.1, 6.4)
# ---------------------------------------------------------------------------

class TestBedrockInspectionParameters:
    def test_crop_detection_index_parameterization(self):
        # Requirement 2.1: optional 0-based int, default absent; the
        # min: 0 constraint makes a negative index a descriptor-level
        # validation error (Requirement 6.4).
        params = _params_by_name(get_node_type("bedrock_inference"))
        crop_index = params["crop_detection_index"]
        assert crop_index.param_type == "int"
        assert crop_index.required is False
        assert crop_index.default is None
        assert crop_index.constraints == {"min": 0}

    def test_crop_margin_percent_parameterization(self):
        # Requirement 2.3: optional int, default 0; the 0-100 range is a
        # descriptor constraint (Requirement 6.4).
        params = _params_by_name(get_node_type("bedrock_inference"))
        margin = params["crop_margin_percent"]
        assert margin.param_type == "int"
        assert margin.required is False
        assert margin.default == 0
        assert margin.constraints == {"min": 0, "max": 100}

    def test_reference_payload_path_parameterization(self):
        # Requirement 3.1: optional string, empty default keeps the
        # reference-port behavior.
        params = _params_by_name(get_node_type("bedrock_inference"))
        path = params["reference_payload_path"]
        assert path.param_type == "string"
        assert path.required is False
        assert path.default == ""
        assert path.constraints == {}

    def test_allowed_uri_prefixes_parameterization(self):
        # Requirement 3.4: optional string mirroring the Custom Python
        # source node's parameter shape (newline-separated prefixes,
        # empty permits any source).
        params = _params_by_name(get_node_type("bedrock_inference"))
        prefixes = params["allowed_uri_prefixes"]
        assert prefixes.param_type == "string"
        assert prefixes.required is False
        assert prefixes.default == ""
        assert prefixes.constraints == {}
        # Shape parity with custom_python_source's parameter.
        source = _params_by_name(
            get_node_type("custom_python_source"))["allowed_uri_prefixes"]
        assert prefixes.param_type == source.param_type
        assert prefixes.required == source.required
        assert prefixes.default == source.default
        assert prefixes.constraints == source.constraints
        # Mirrored wording: the shared allow-list semantics.
        for phrase in ("newline-separated", "empty permits any source"):
            assert phrase in prefixes.description
            assert phrase in source.description

    def test_descriptions_document_the_dotted_payload_path_syntax(self):
        # Requirement 6.1: the payload-path syntax (dotted, dict keys +
        # integer list indices) is named, with a working example.
        params = _params_by_name(get_node_type("bedrock_inference"))
        path = params["reference_payload_path"]
        assert "dotted path" in path.description
        assert "refs.0.image" in path.description
        assert "refs.0.image" in path.examples
        # And the accepted source forms (Requirements 3.2, 3.3 surface).
        for form in ("s3://", "http(s)://", "base64"):
            assert form in path.description, form

    def test_crop_description_names_the_sort_order_dependence(self):
        # Requirement 6.1: crop_detection_index documents that the index
        # follows the model_inference node's detection_sort_order.
        crop_index = _params_by_name(
            get_node_type("bedrock_inference"))["crop_detection_index"]
        assert "detection_sort_order" in crop_index.description
        assert "model_inference" in crop_index.description

    def test_every_new_parameter_is_documented_with_examples(self):
        # Requirement 6.1: descriptions and examples for all four.
        params = _params_by_name(get_node_type("bedrock_inference"))
        for name in ("crop_detection_index", "crop_margin_percent",
                     "reference_payload_path", "allowed_uri_prefixes"):
            parameter = params[name]
            assert parameter.description and parameter.description.strip(), name
            assert isinstance(parameter.examples, list) and parameter.examples, name


# ---------------------------------------------------------------------------
# Catalog additivity: prefix-order assertions (Requirement 6.6 posture)
# ---------------------------------------------------------------------------

class TestCatalogAdditivity:
    def test_model_inference_parameters_are_appended_only(self):
        # The pre-feature parameter order is a strict prefix of the live
        # order: nothing reordered or removed, the new parameter appended.
        names = [p.name for p in get_node_type("model_inference").parameters]
        prior = MODEL_INFERENCE_PRIOR_PARAMETERS
        assert names[:len(prior)] == prior
        assert names[len(prior):] == ["detection_sort_order"]

    def test_bedrock_inference_parameters_are_appended_only(self):
        names = [p.name for p in get_node_type("bedrock_inference").parameters]
        prior = BEDROCK_PRIOR_PARAMETERS
        assert names[:len(prior)] == prior
        assert names[len(prior):] == [
            "crop_detection_index",
            "crop_margin_percent",
            "reference_payload_path",
            "allowed_uri_prefixes",
        ]

    def test_ports_and_identity_are_untouched(self):
        # The feature adds parameters only: node identity, categories,
        # and port lists stay exactly as before.
        model = get_node_type("model_inference")
        assert [(p.name, p.port_type) for p in model.inputs] == [
            ("in", "VideoFrames")]
        assert [(p.name, p.port_type) for p in model.outputs] == [
            ("out", "InferenceMeta")]
        bedrock = get_node_type("bedrock_inference")
        assert [(p.name, p.port_type) for p in bedrock.inputs] == [
            ("in", "VideoFrames"), ("reference", "VideoFrames")]
        assert [(p.name, p.port_type) for p in bedrock.outputs] == [
            ("out", "InferenceMeta")]
