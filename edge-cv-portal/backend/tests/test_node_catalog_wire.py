"""
Node catalog wire serialization (workflow-manager Requirement 2.8 plus
the per-parameter help descriptions).

GET /workflows/node-catalog (functions/workflow_validation.py) serves the
catalog in its camelCase wire form for the frontend Node_Palette and the
node configuration panel. Covered here:

1. Every catalog parameter serializes a non-empty ``description`` string
   (field-level help rendered under the control's label).
2. The wire shape of one parameter carries exactly the documented keys,
   with snake_case constraint keys mapped to camelCase.
3. The ``conditional`` node type is served with its two typed output
   ports ("true"/"false") and a ``condition`` parameter whose description
   documents the rule-expression language (fields, operators, and the
   worked example) exactly as the shared evaluator supports it.
"""
import json
import sys

import pytest


@pytest.fixture(scope="module")
def validation_module(aws_stack):
    """functions/workflow_validation.py imported inside the moto stack."""
    sys.modules.pop("workflow_validation", None)
    import workflow_validation

    return workflow_validation


def catalog_response(module):
    event = {
        "httpMethod": "GET",
        "resource": "/workflows/node-catalog",
        "path": "/workflows/node-catalog",
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "user-1",
                    "email": "user-1@example.com",
                    "cognito:username": "user-1",
                    "custom:role": "DataScientist",
                }
            }
        },
    }
    response = module.handler(event, None)
    assert response["statusCode"] == 200
    return json.loads(response["body"])


class TestParameterDescriptions:
    def test_every_parameter_serializes_a_nonempty_description(self, validation_module):
        body = catalog_response(validation_module)
        assert body["count"] == len(body["nodeTypes"])
        for node_type in body["nodeTypes"]:
            for parameter in node_type["parameters"]:
                description = parameter.get("description")
                assert isinstance(description, str) and description.strip(), (
                    "{0}.{1} has no description".format(
                        node_type["typeId"], parameter["name"]))

    def test_every_parameter_serializes_nonempty_examples(self, validation_module):
        # Field-level help: every parameter ships at least one working
        # example value the configuration panel can offer verbatim.
        body = catalog_response(validation_module)
        for node_type in body["nodeTypes"]:
            for parameter in node_type["parameters"]:
                examples = parameter.get("examples")
                assert isinstance(examples, list) and examples, (
                    "{0}.{1} has no examples".format(
                        node_type["typeId"], parameter["name"]))

    def test_parameter_wire_shape(self, validation_module):
        from workflow_core.catalog import ParameterDescriptor

        wire = validation_module.parameter_to_wire(ParameterDescriptor(
            "location", "string", required=True, default=None,
            constraints={"min_length": 1},
            description="Path of the folder to read, e.g. /data/images.",
            examples=["/data/images"],
        ))
        assert wire == {
            "name": "location",
            "paramType": "string",
            "required": True,
            "default": None,
            "constraints": {"minLength": 1},
            "dependsOn": None,
            "description": "Path of the folder to read, e.g. /data/images.",
            "examples": ["/data/images"],
        }

    def test_help_fields_default_to_none_for_older_descriptors(self, validation_module):
        # Backward compatibility: descriptors built without a description
        # or examples serialize description/examples: null rather than
        # failing.
        from workflow_core.catalog import ParameterDescriptor

        wire = validation_module.parameter_to_wire(
            ParameterDescriptor("pin", "int", required=True))
        assert wire["description"] is None
        assert wire["examples"] is None


class TestConditionalNodeOnTheWire:
    def _node_type(self, body, type_id):
        by_id = {n["typeId"]: n for n in body["nodeTypes"]}
        assert type_id in by_id, sorted(by_id)
        return by_id[type_id]

    def test_conditional_two_typed_output_ports(self, validation_module):
        body = catalog_response(validation_module)
        conditional = self._node_type(body, "conditional")
        assert conditional["displayName"] == "Conditional"
        assert conditional["category"] == "post_processing"
        assert conditional["inputs"] == [
            {"name": "in", "portType": "InferenceMeta"}]
        assert conditional["outputs"] == [
            {"name": "true", "portType": "InferenceMeta"},
            {"name": "false", "portType": "InferenceMeta"},
        ]
        for mapping in conditional["mappings"]:
            assert mapping["executorBinding"] == "conditional"

    @pytest.mark.parametrize("type_id", ["conditional", "inference_filter"])
    def test_condition_description_documents_the_expression_language(
            self, validation_module, type_id):
        body = catalog_response(validation_module)
        node_type = self._node_type(body, type_id)
        condition = next(p for p in node_type["parameters"]
                         if p["name"] == "condition")
        description = condition["description"]
        # Fields the shared evaluator resolves from the inference metadata.
        assert "is_anomalous" in description
        assert "confidence" in description
        # Operators (comparisons and logic).
        for operator in ("==", "!=", ">=", "<=", "&&", "||"):
            assert operator in description, operator
        # A worked example in the documented grammar.
        assert "is_anomalous == true && confidence >= 0.8" in description
        # Plus 3+ working example expressions served as `examples`.
        examples = condition["examples"]
        assert isinstance(examples, list) and len(examples) >= 3
        assert "is_anomalous == true" in examples


class TestLlmInferenceLabelOnTheWire:
    """Bug 4 integration check (Requirement 2.4): the node-catalog API
    response the Node_Palette renders presents the ``llm_inference`` entry
    with the display label "VLM/LLM Inference" while keeping its type id."""

    def _node_type(self, body, type_id):
        by_id = {n["typeId"]: n for n in body["nodeTypes"]}
        assert type_id in by_id, sorted(by_id)
        return by_id[type_id]

    def test_llm_inference_display_label_is_vlm_llm_inference(self, validation_module):
        body = catalog_response(validation_module)
        llm_inference = self._node_type(body, "llm_inference")
        assert llm_inference["displayName"] == "VLM/LLM Inference"
        assert llm_inference["typeId"] == "llm_inference"
