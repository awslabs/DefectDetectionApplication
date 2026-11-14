"""``bedrock_inference`` ``additional_model_request_fields``
(feature: bedrock-additional-model-request-fields).

The parameter carries a JSON object sent as the Converse API
``additionalModelRequestFields``: request fields only some models accept,
e.g. ``{"thinking": {"type": "disabled"}}`` for a Claude model that thinks
by default (Amazon Nova refuses that object with a ValidationException).

Pinned here:

- catalog: appended last, a ``string`` defaulting to ``""``, multiline,
  examples that are JSON objects;
- ``resolve_additional_model_request_fields``: empty values and ``{}``
  send nothing, a JSON object (text or mapping) becomes compact JSON
  text, anything else raises ``ValueError`` naming the parameter;
- the Invocation_Builder: absent/empty keeps ``converse_kwargs()``
  byte-identical to the pre-feature request, a set object is sent as
  ``additionalModelRequestFields`` in every answer format, as a fresh
  dict per call;
- the validator: BEDROCK_REQUEST_FIELDS_JSON fires once per node with a
  non-object value and never for an empty or object value;
- the compiler: the binding carries the value verbatim, and ``""`` by
  default.
"""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from workflow_core.anomaly_invocation import (
    ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER,
    ANSWER_FORMAT_INSPECTION,
    BedrockInvocation,
    build_bedrock_invocation,
    resolve_additional_model_request_fields,
)
from workflow_core.catalog import get_node_type
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_BEDROCK_REQUEST_FIELDS_JSON,
    SEVERITY_ERROR,
    is_parameter_value_valid,
    validate,
)

THINKING_OFF = '{"thinking": {"type": "disabled"}}'
_IMAGE = b"\xff\xd8\xffinput"
_REFERENCE = b"\xff\xd8\xffreference"

#: Any JSON value (no NaN/infinity: Bedrock takes strict JSON).
_JSON_SCALARS = (st.none() | st.booleans() | st.integers()
                 | st.floats(allow_nan=False, allow_infinity=False)
                 | st.text(max_size=8))
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda children: (st.lists(children, max_size=3)
                      | st.dictionaries(st.text(max_size=8), children,
                                        max_size=3)),
    max_leaves=10)
_JSON_OBJECTS = st.dictionaries(st.text(max_size=8), _JSON_VALUES,
                                min_size=1, max_size=4)


def _parameter():
    (param,) = [p for p in get_node_type("bedrock_inference").parameters
                if p.name == ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER]
    return param


class TestCatalogParameter:
    def test_appended_last_with_its_declared_shape(self):
        names = [p.name for p in get_node_type("bedrock_inference").parameters]
        assert names[-1] == ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER
        assert names.count(ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER) == 1
        param = _parameter()
        assert param.param_type == "string"
        assert param.default == ""
        assert param.required is False
        assert param.constraints == {}
        assert param.depends_on is None
        assert param.multiline is True
        assert "additionalModelRequestFields" in param.description

    def test_default_and_examples_are_valid_values_and_json_objects(self):
        param = _parameter()
        assert is_parameter_value_valid(param, param.default)
        assert THINKING_OFF in param.examples
        for example in param.examples:
            assert is_parameter_value_valid(param, example)
            assert isinstance(json.loads(example), dict)
            assert resolve_additional_model_request_fields(example) is not None


class TestResolve:
    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\t", "{}", " {} ", {}])
    def test_empty_values_send_nothing(self, raw):
        assert resolve_additional_model_request_fields(raw) is None

    def test_object_text_becomes_compact_json(self):
        assert resolve_additional_model_request_fields(
            ' {\n  "thinking": {"type": "disabled"}\n} ') == \
            '{"thinking":{"type":"disabled"}}'

    def test_mapping_is_accepted_and_key_order_kept(self):
        raw = {"b": 1, "a": {"z": True, "y": [1, "x"]}}
        assert resolve_additional_model_request_fields(raw) == \
            '{"b":1,"a":{"z":true,"y":[1,"x"]}}'

    @pytest.mark.parametrize("raw", ["[1, 2]", '"thinking"', "3", "true",
                                     "null", "[]"])
    def test_json_that_is_not_an_object_is_refused(self, raw):
        with pytest.raises(ValueError) as caught:
            resolve_additional_model_request_fields(raw)
        assert ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER in str(caught.value)
        assert "JSON object" in str(caught.value)

    @pytest.mark.parametrize("raw", ["{thinking: off}", '{"a": 1', "off", 0, 1.5])
    def test_text_that_is_not_json_is_refused(self, raw):
        with pytest.raises(ValueError) as caught:
            resolve_additional_model_request_fields(raw)
        assert ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER in str(caught.value)

    @pytest.mark.parametrize("raw", ['{"a": NaN}', '{"a": Infinity}',
                                     {"a": float("nan")}, {"a": object()}])
    def test_values_bedrock_cannot_take_are_refused(self, raw):
        with pytest.raises(ValueError):
            resolve_additional_model_request_fields(raw)

    @given(_JSON_OBJECTS)
    def test_any_json_object_round_trips(self, obj):
        text = resolve_additional_model_request_fields(json.dumps(obj))
        assert json.loads(text) == obj
        assert resolve_additional_model_request_fields(obj) == text


class TestInvocation:
    @pytest.mark.parametrize("absent", [{}, {"additional_model_request_fields": ""},
                                        {"additional_model_request_fields": "{}"},
                                        {"additional_model_request_fields": None}])
    def test_absent_or_empty_keeps_the_pre_feature_request(self, absent):
        base = {"model": "us.amazon.nova-lite-v1:0", "prompt": "Compare.",
                "system_prompt": "Inspect.", "max_tokens": 300}
        pre_feature = BedrockInvocation(
            model="us.amazon.nova-lite-v1:0",
            prompt=build_bedrock_invocation(base, _IMAGE).prompt,
            images=(("Input image", _IMAGE), ("Reference image", _REFERENCE)),
            region="us-east-1", max_tokens=300, system_prompt="Inspect.",
        ).converse_kwargs()
        kwargs = build_bedrock_invocation(
            dict(base, **absent), _IMAGE, _REFERENCE).converse_kwargs()
        assert "additionalModelRequestFields" not in kwargs
        assert kwargs == pre_feature
        assert list(kwargs) == list(pre_feature)

    def test_set_object_is_sent_as_additional_model_request_fields(self):
        parameters = {"model": "us.anthropic.claude-haiku-5-5", "prompt": "Inspect.",
                      "anomaly_mode": False, ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: THINKING_OFF}
        invocation = build_bedrock_invocation(parameters, _IMAGE, _REFERENCE)
        assert invocation.additional_model_request_fields == \
            '{"thinking":{"type":"disabled"}}'
        kwargs = invocation.converse_kwargs()
        assert kwargs["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
        without = build_bedrock_invocation(
            dict(parameters, **{ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: ""}),
            _IMAGE, _REFERENCE).converse_kwargs()
        assert {k: v for k, v in kwargs.items()
                if k != "additionalModelRequestFields"} == without

    def test_sent_in_the_inspection_format_too(self):
        invocation = build_bedrock_invocation(
            {"answer_format": ANSWER_FORMAT_INSPECTION, "prompt": "Inspect.",
             ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: THINKING_OFF}, _IMAGE)
        assert invocation.converse_kwargs()["additionalModelRequestFields"] == \
            {"thinking": {"type": "disabled"}}

    def test_each_call_returns_a_fresh_dict(self):
        invocation = build_bedrock_invocation(
            {"prompt": "x", ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: THINKING_OFF}, _IMAGE)
        first = invocation.converse_kwargs()
        first["additionalModelRequestFields"]["thinking"]["type"] = "enabled"
        assert invocation.converse_kwargs()["additionalModelRequestFields"] == \
            {"thinking": {"type": "disabled"}}

    def test_invocation_stays_hashable(self):
        invocation = build_bedrock_invocation(
            {"prompt": "x", ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: THINKING_OFF}, _IMAGE)
        assert hash(invocation) == hash(build_bedrock_invocation(
            {"prompt": "x", ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: THINKING_OFF}, _IMAGE))

    def test_a_non_object_value_fails_the_build(self):
        with pytest.raises(ValueError):
            build_bedrock_invocation(
                {"prompt": "x", ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: "[1]"}, _IMAGE)

    @given(_JSON_OBJECTS)
    def test_any_object_reaches_the_request_unchanged(self, obj):
        kwargs = build_bedrock_invocation(
            {"prompt": "x", ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: json.dumps(obj)},
            _IMAGE).converse_kwargs()
        assert kwargs["additionalModelRequestFields"] == obj


# ---------------------------------------------------------------------------
# Validator and compiler
# ---------------------------------------------------------------------------

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(cid, source, target, target_port="in"):
    return Connection(id=cid, source=PortEndpoint(source, "out"),
                      target=PortEndpoint(target, target_port))


def _graph(**bedrock_parameters):
    """cam -> bedrock.in, ref folder -> bedrock.reference, bedrock -> mqtt."""
    return WorkflowGraph(
        nodes=[_node("cam", "icam_source"),
               _node("ref", "folder_source", location="/aws_dda/ref/golden.jpg"),
               _node("bedrock", "bedrock_inference", **bedrock_parameters),
               _node("mqtt", "mqtt_publish", broker_host="10.0.0.12",
                     topic="factory/line1")],
        connections=[_conn("c1", "cam", "bedrock"),
                     _conn("c2", "ref", "bedrock", target_port="reference"),
                     _conn("c3", "bedrock", "mqtt")],
    )


def _request_field_findings(graph):
    return [f for f in validate(graph) if f.code == CODE_BEDROCK_REQUEST_FIELDS_JSON]


class TestValidator:
    @pytest.mark.parametrize("value", [None, "", "  ", "{}", THINKING_OFF,
                                       '{"inferenceConfig": {"topK": 20}}'])
    def test_empty_or_object_values_raise_no_finding(self, value):
        parameters = {} if value is None else {
            ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: value}
        graph = _graph(**parameters)
        assert _request_field_findings(graph) == []
        assert [f for f in validate(graph) if f.severity == SEVERITY_ERROR] == []

    @pytest.mark.parametrize("value", ["[1]", '"thinking"', "{thinking: off}", "3"])
    def test_a_non_object_value_is_one_error_on_the_node(self, value):
        (finding,) = _request_field_findings(
            _graph(**{ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: value}))
        assert finding.severity == SEVERITY_ERROR
        assert finding.node_id == "bedrock"
        assert ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER in finding.message
        assert '{"thinking": {"type": "disabled"}}' in finding.message


class TestCompiler:
    def _binding(self, graph):
        document = compile(graph, "arm64_jp6")
        assert isinstance(document, CompiledPipelineDocument), document
        (binding,) = [b for b in document.executor_bindings
                      if b["binding"] == "bedrock_inference"]
        return binding

    def test_default_is_carried_as_empty_text(self):
        assert self._binding(_graph())["parameters"][
            ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER] == ""

    def test_set_value_is_carried_verbatim(self):
        value = '{\n  "thinking": {"type": "disabled"}\n}'
        binding = self._binding(_graph(**{ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: value}))
        assert binding["parameters"][ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER] == value

    def test_a_non_object_value_does_not_compile(self):
        result = compile(_graph(**{ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER: "[1]"}),
                         "arm64_jp6")
        assert not isinstance(result, CompiledPipelineDocument)
        assert any(ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER in e.message for e in result)
