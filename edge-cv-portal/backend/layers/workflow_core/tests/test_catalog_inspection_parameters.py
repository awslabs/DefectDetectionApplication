"""Catalog descriptor tests for the built-in inspection chain parameters.

Feature: bedrock-inspection-chain. Asserts, for every parameter the feature
APPENDS to ``bedrock_inference``, ``mqtt_publish`` and
``mqtt_subscribe``, its type, default and constraints, and that it is
appended after the pre-existing parameters (so every pre-existing
``ParameterDescriptor`` keeps its position and content). The ``s3_upload``
additions are covered by ``test_catalog_s3_upload.py``; the baseline
delta scope by ``test_bug_catalog_preservation.py``.

Every default below reproduces today's behaviour: a document that sets
none of these parameters compiles, validates and runs as at the pin.
"""

from workflow_core.catalog import get_node_type
from workflow_core.catalog.nodes import BEDROCK_MODEL_ID_REGEX

_LEGACY_BEDROCK_PARAMETERS = (
    "model", "prompt", "anomaly_mode", "region", "max_tokens",
    "system_prompt", "crop_detection_index", "crop_margin_percent",
    "reference_payload_path", "allowed_uri_prefixes",
)

#: name -> (param_type, default, constraints) of the appended parameters.
NEW_BEDROCK_PARAMETERS = {
    "answer_format": ("enum", "legacy", {"values": ["legacy", "inspection"]}),
    "render_prompt": ("bool", False, {}),
    "expected_count_path": ("string", "", {}),
    "attempts": ("int", 1, {"min": 1, "max": 3}),
    "attempt_timeout_s": ("int", 30, {"min": 1, "max": 120}),
    "budget_s": ("int", 0, {"min": 0, "max": 120}),
    "box_scale": ("enum", "auto",
                  {"values": ["auto", "pixels", "normalized_1000"]}),
    "on_error": ("enum", "fail_run", {"values": ["fail_run", "record"]}),
    "error_verdict": ("enum", "error", {"values": ["error", "fail"]}),
    "enabled": ("bool", True, {}),
    "max_image_dimension": ("int", 0, {"min": 0, "max": 8000}),
    "annotate": ("bool", False, {}),
}


def _params_by_name(type_id):
    descriptor = get_node_type(type_id)
    assert descriptor is not None, type_id
    return {param.name: param for param in descriptor.parameters}


def _names(type_id):
    return [param.name for param in get_node_type(type_id).parameters]


class TestBedrockInferenceAppendedParameters:
    def test_appended_after_the_legacy_parameters_in_order(self):
        names = _names("bedrock_inference")
        assert tuple(names[:len(_LEGACY_BEDROCK_PARAMETERS)]) == \
            _LEGACY_BEDROCK_PARAMETERS
        assert names[len(_LEGACY_BEDROCK_PARAMETERS):] == \
            list(NEW_BEDROCK_PARAMETERS)

    def test_each_parameter_type_default_and_constraints(self):
        params = _params_by_name("bedrock_inference")
        for name, (param_type, default, constraints) in \
                NEW_BEDROCK_PARAMETERS.items():
            param = params[name]
            assert param.param_type == param_type, name
            assert param.default == default, name
            assert param.constraints == constraints, name
            assert param.required is False, name
            assert param.depends_on is None, name
            assert param.description, name
            assert param.examples, name

    def test_every_example_and_default_is_valid(self):
        from workflow_core.validator import is_parameter_value_valid
        params = _params_by_name("bedrock_inference")
        for name in NEW_BEDROCK_PARAMETERS:
            assert is_parameter_value_valid(params[name], params[name].default), name
            for example in params[name].examples:
                assert is_parameter_value_valid(params[name], example), (name, example)

    def test_model_is_a_regex_constrained_string(self):
        from workflow_core.validator import is_parameter_value_valid
        param = _params_by_name("bedrock_inference")["model"]
        assert param.param_type == "string"
        assert param.constraints == {"regex": BEDROCK_MODEL_ID_REGEX}
        assert is_parameter_value_valid(param, param.default)
        for accepted in param.examples + [
                "arn:aws:bedrock:us-west-2:123456789012:inference-profile/"
                "us.amazon.nova-pro-v1:0"]:
            assert is_parameter_value_valid(param, accepted), accepted
        for rejected in ("", " us.amazon.nova-pro-v1:0", "nova pro",
                         "'us.amazon.nova-pro-v1:0'", "-leading-dash"):
            assert not is_parameter_value_valid(param, rejected), rejected


class TestMqttPublishPayloadFormat:
    def test_appended_last_with_text_default(self):
        names = _names("mqtt_publish")
        assert names[-1] == "payload_format"
        assert names[-2] == "iot_private_key_path"
        param = _params_by_name("mqtt_publish")["payload_format"]
        assert param.param_type == "enum"
        assert param.required is False
        assert param.default == "text"
        assert param.constraints == {"values": ["text", "json"]}
        assert param.depends_on is None
        for token in ("{ulid}", "{now_iso}", "{latency_ms}", "omitted"):
            assert token in param.description


class TestMqttSubscribePayloadCondition:
    def test_appended_last_after_the_trigger_policy_family(self):
        names = _names("mqtt_subscribe")
        assert names[-1] == "payload_condition"
        # The shared trigger policy family keeps its position right before.
        assert "concurrency_policy" in names[:-1]
        assert names.index("concurrency_policy") < names.index("payload_condition")

    def test_shape_and_grammar_reference(self):
        from workflow_core.catalog import CONDITION_LANGUAGE_DESCRIPTION
        param = _params_by_name("mqtt_subscribe")["payload_condition"]
        assert param.param_type == "string"
        assert param.required is False
        assert param.default == ""
        assert param.constraints == {}
        assert param.depends_on is None
        assert CONDITION_LANGUAGE_DESCRIPTION in param.description
        for key in ("trigger.topic", "trigger.payload", "trigger.payload_json"):
            assert key in param.description
        assert any("trigger.payload_json" in example for example in param.examples)

    def test_opcua_subscribe_is_untouched(self):
        assert "payload_condition" not in _names("opcua_subscribe")
