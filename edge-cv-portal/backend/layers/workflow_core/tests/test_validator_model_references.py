"""Unit tests for validator model-reference resolution
(vllm-triton-inference task 1.3).

The resolution pass runs only when validate() receives a
``model_registry`` snapshot: every ``model_ref`` parameter value must
name a record in the snapshot whose ``model_type`` matches the node
family — ``llm_inference`` requires a ``vllm``-typed record,
``model_inference`` (and every other family) requires a non-``vllm``
record. Unresolvable references produce a finding identifying the node
and the model reference. Structural/parameter checks (min_length,
bounds, min_exclusive) apply to ``llm_inference`` through the existing
generic V4 path — verified here, not duplicated.

The ``llm_inference`` descriptor is constructed locally (the design's
task 5 shape) so these tests do not depend on the catalog descriptor
landing: the catalog is filtered of any ``llm_inference`` entry and the
local descriptor appended.

_Requirements: 6.5, 6.6, 6.7, 6.12_
"""

from workflow_core.catalog import NODE_CATALOG
from workflow_core.catalog.models import (
    CATEGORY_INFERENCE,
    NodeTypeDescriptor,
    ParameterDescriptor,
    PortDescriptor,
    PORT_TYPE_INFERENCE_META,
)
from workflow_core.serializer import Node, Position, WorkflowGraph
from workflow_core.validator import (
    CODE_MODEL_REF_UNRESOLVED,
    CODE_V4_INVALID_PARAMETER_VALUE,
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    SEVERITY_ERROR,
    validate,
)

# --------------------------------------------------------------------------
# Local llm_inference descriptor (design task 5 shape); appended to the
# catalog with any existing llm_inference entry filtered out, so the
# tests run identically before and after the catalog descriptor lands.
# --------------------------------------------------------------------------

LLM_INFERENCE = NodeTypeDescriptor(
    type_id="llm_inference",
    category=CATEGORY_INFERENCE,
    display_name="LLM Inference",
    inputs=[PortDescriptor("in", PORT_TYPE_INFERENCE_META)],
    outputs=[PortDescriptor("out", PORT_TYPE_INFERENCE_META)],
    parameters=[
        ParameterDescriptor("modelName", "model_ref", required=True, default=None,
                            constraints={"min_length": 1}),
        ParameterDescriptor("prompt_template", "string", required=True, default=None,
                            constraints={"min_length": 1}),
        ParameterDescriptor("max_tokens", "int", required=False, default=256,
                            constraints={"min": 1}),
        ParameterDescriptor("temperature", "float", required=False, default=0.7,
                            constraints={"min": 0.0, "max": 2.0}),
        ParameterDescriptor("top_p", "float", required=False, default=1.0,
                            constraints={"min_exclusive": 0.0, "max": 1.0}),
    ],
    mappings=[],
    hardware_dependent=True,
)

CATALOG = [d for d in NODE_CATALOG if d.type_id != "llm_inference"] + [LLM_INFERENCE]

_POS = Position(0.0, 0.0)

#: Registry snapshot: one vision record, one vLLM record.
REGISTRY = {
    "widget-anomaly-v3": {"model_type": "classification"},
    "opt-125m": {"model_type": "vllm"},
}


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _model_inference(model_name, node_id="inf"):
    return _node(node_id, "model_inference", modelName=model_name)


def _llm_inference(node_id="llm", **overrides):
    parameters = {"modelName": "opt-125m", "prompt_template": "Summarize: {confidence}"}
    parameters.update(overrides)
    return _node(node_id, "llm_inference", **parameters)


def _graph(*nodes):
    return WorkflowGraph(nodes=list(nodes))


def _resolution_findings(findings):
    return [f for f in findings if f.code == CODE_MODEL_REF_UNRESOLVED]


def _validate(graph, registry=REGISTRY):
    return validate(graph, catalog=CATALOG, model_registry=registry)


class TestBackwardCompatibility:
    def test_no_registry_skips_resolution(self):
        # Existing callers (device vendored copy, current Lambda) pass no
        # snapshot: no resolution findings even for unknown references.
        graph = _graph(_model_inference("no-such-model"), _llm_inference(modelName="ghost"))
        findings = validate(graph, catalog=CATALOG)
        assert _resolution_findings(findings) == []

    def test_missing_value_left_to_v4(self):
        # A missing required modelName is V4's finding, not a resolution
        # finding (no double reporting).
        graph = _graph(_llm_inference(modelName=None))
        findings = _validate(graph)
        assert _resolution_findings(findings) == []
        v4 = [f for f in findings if f.code == CODE_V4_MISSING_REQUIRED_PARAMETER]
        assert any("modelName" in f.message for f in v4)


class TestModelInferenceResolution:
    def test_vision_record_resolves(self):
        findings = _validate(_graph(_model_inference("widget-anomaly-v3")))
        assert _resolution_findings(findings) == []

    def test_unknown_reference_reported_with_node_and_reference(self):
        findings = _validate(_graph(_model_inference("no-such-model", node_id="inf1")))
        resolution = _resolution_findings(findings)
        assert len(resolution) == 1
        finding = resolution[0]
        assert finding.severity == SEVERITY_ERROR
        assert finding.node_id == "inf1"
        assert "inf1" in finding.message
        assert "no-such-model" in finding.message

    def test_vllm_record_rejected_for_model_inference(self):
        findings = _validate(_graph(_model_inference("opt-125m", node_id="inf1")))
        resolution = _resolution_findings(findings)
        assert len(resolution) == 1
        assert resolution[0].node_id == "inf1"
        assert "opt-125m" in resolution[0].message
        assert "vllm" in resolution[0].message


class TestLlmInferenceResolution:
    def test_vllm_record_resolves(self):
        findings = _validate(_graph(_llm_inference()))
        assert _resolution_findings(findings) == []

    def test_unknown_reference_reported_with_node_and_reference(self):
        # Requirement 6.12: the finding identifies the node and the
        # unresolvable model reference.
        findings = _validate(_graph(_llm_inference(node_id="llm1", modelName="ghost")))
        resolution = _resolution_findings(findings)
        assert len(resolution) == 1
        finding = resolution[0]
        assert finding.severity == SEVERITY_ERROR
        assert finding.node_id == "llm1"
        assert "llm1" in finding.message
        assert "ghost" in finding.message

    def test_non_vllm_record_rejected_for_llm_inference(self):
        findings = _validate(_graph(_llm_inference(node_id="llm1",
                                                   modelName="widget-anomaly-v3")))
        resolution = _resolution_findings(findings)
        assert len(resolution) == 1
        assert resolution[0].node_id == "llm1"
        assert "widget-anomaly-v3" in resolution[0].message

    def test_bare_string_records_accepted(self):
        registry = {"opt-125m": "vllm", "widget-anomaly-v3": "classification"}
        graph = _graph(_llm_inference(), _model_inference("widget-anomaly-v3"))
        assert _resolution_findings(_validate(graph, registry)) == []

    def test_empty_registry_reports_every_reference(self):
        graph = _graph(_llm_inference(node_id="llm1"),
                       _model_inference("widget-anomaly-v3", node_id="inf1"))
        resolution = _resolution_findings(_validate(graph, registry={}))
        assert sorted(f.node_id for f in resolution) == ["inf1", "llm1"]


class TestGenericChecksApplyToLlmInference:
    """6.5/6.6: structural/parameter checks reach llm_inference through
    the existing generic V4 path — verified, not re-implemented."""

    def _v4_invalid(self, findings, parameter):
        return [f for f in findings
                if f.code == CODE_V4_INVALID_PARAMETER_VALUE and parameter in f.message]

    def test_empty_prompt_template_reported(self):
        findings = _validate(_graph(_llm_inference(prompt_template="")))
        assert self._v4_invalid(findings, "prompt_template")

    def test_top_p_zero_violates_exclusive_bound(self):
        findings = _validate(_graph(_llm_inference(top_p=0.0)))
        assert self._v4_invalid(findings, "top_p")

    def test_temperature_out_of_bounds_reported(self):
        findings = _validate(_graph(_llm_inference(temperature=2.5)))
        assert self._v4_invalid(findings, "temperature")

    def test_max_tokens_below_minimum_reported(self):
        findings = _validate(_graph(_llm_inference(max_tokens=0)))
        assert self._v4_invalid(findings, "max_tokens")

    def test_valid_parameters_yield_no_parameter_findings(self):
        findings = _validate(_graph(_llm_inference()))
        parameter_codes = {CODE_V4_INVALID_PARAMETER_VALUE,
                           CODE_V4_MISSING_REQUIRED_PARAMETER,
                           CODE_MODEL_REF_UNRESOLVED}
        assert [f for f in findings if f.code in parameter_codes] == []
