"""Unit tests for the detection-guided Bedrock inspection validator checks
(detection-guided-bedrock-inspection task 4.2).

Covers the three checks added by task 4.1 in
``workflow_core/validator/checks.py``:

- ``BEDROCK_REFERENCE_CONFLICT`` (error): ``reference_payload_path`` set
  AND the node's ``reference`` input port fed by a connection
  (Requirement 3.6);
- ``BEDROCK_CROP_NO_MODEL`` (warning): ``crop_detection_index`` set in a
  graph with no ``model_inference`` node (Requirement 6.2);
- ``BEDROCK_PAYLOAD_NO_TRIGGER`` (warning): ``reference_payload_path``
  set in a graph with no CATEGORY_TRIGGER node (Requirement 6.3).

Each check fires exactly once per offending node, with the right
severity, naming the node. A compliant graph (payload path + trigger +
model_inference present, no fed reference port) produces zero findings
from these checks. Range violations (negative ``crop_detection_index``,
``crop_margin_percent`` outside 0-100) are reported through the existing
V4 descriptor-constraint mechanism, not re-implemented (Requirement
6.4). The vendored LocalServer mirror of ``validator/checks.py`` stays
byte-identical to the portal layer copy (Requirement 6.5).

_Requirements: 3.6, 6.2, 6.3, 6.4, 6.5_
"""

from pathlib import Path

from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_V4_INVALID_PARAMETER_VALUE,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate,
)
from workflow_core.validator.checks import (
    CODE_BEDROCK_CROP_NO_MODEL,
    CODE_BEDROCK_PAYLOAD_NO_TRIGGER,
    CODE_BEDROCK_REFERENCE_CONFLICT,
)

BEDROCK_CODES = (
    CODE_BEDROCK_REFERENCE_CONFLICT,
    CODE_BEDROCK_CROP_NO_MODEL,
    CODE_BEDROCK_PAYLOAD_NO_TRIGGER,
)

# --------------------------------------------------------------------------
# Graph-building helpers (same conventions as test_validator_checks.py)
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node, source_port="out", target_port="in"):
    return Connection(
        id=conn_id,
        source=PortEndpoint(source_node, source_port),
        target=PortEndpoint(target_node, target_port),
    )


def _folder(node_id="src"):
    return _node(node_id, "folder_source", location="/data/images")


def _capture(node_id="cap"):
    return _node(node_id, "capture", output_path="/out")


def _model(node_id="inf"):
    return _node(node_id, "model_inference", modelName="widget-anomaly-v3")


def _trigger(node_id="trig"):
    # CATEGORY_TRIGGER node with a connection target (avoids V8 noise).
    return _node(node_id, "mqtt_subscribe",
                 topic="factory/line1/#", greengrass=True)


def _bedrock(node_id="bed", **parameters):
    # prompt is required but carries a default, so it may be omitted.
    return _node(node_id, "bedrock_inference", **parameters)


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


def _bedrock_findings(findings):
    return [f for f in findings if f.code in BEDROCK_CODES]


# --------------------------------------------------------------------------
# BEDROCK_REFERENCE_CONFLICT (error, Requirement 3.6)
# --------------------------------------------------------------------------

class TestReferenceConflict:
    def test_payload_path_plus_fed_reference_port_is_one_error(self):
        graph = WorkflowGraph(
            nodes=[
                _trigger(),
                _folder("srcA"), _folder("srcB"),
                _bedrock("bed", reference_payload_path="refs.0.image"),
                _capture(),
            ],
            connections=[
                _conn("c1", "srcA", "bed"),
                _conn("c2", "srcB", "bed", target_port="reference"),
                _conn("c3", "bed", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_REFERENCE_CONFLICT)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_ERROR
        assert found[0].node_id == "bed"
        assert "'bed'" in found[0].message

    def test_fires_once_per_offending_node(self):
        graph = WorkflowGraph(
            nodes=[
                _trigger(),
                _folder("srcA"), _folder("srcB"),
                _bedrock("bed1", reference_payload_path="refs.0.image"),
                _bedrock("bed2", reference_payload_path="refs.1.image"),
                _capture(),
            ],
            connections=[
                _conn("c1", "srcA", "bed1"),
                _conn("c2", "srcB", "bed1", target_port="reference"),
                _conn("c3", "srcA", "bed2"),
                _conn("c4", "srcB", "bed2", target_port="reference"),
                _conn("c5", "bed1", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_REFERENCE_CONFLICT)
        assert sorted(f.node_id for f in found) == ["bed1", "bed2"]

    def test_payload_path_without_fed_reference_port_is_clean(self):
        graph = WorkflowGraph(
            nodes=[
                _trigger(), _folder(),
                _bedrock("bed", reference_payload_path="refs.0.image"),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "bed"),
                _conn("c2", "bed", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_REFERENCE_CONFLICT) == []

    def test_fed_reference_port_without_payload_path_is_clean(self):
        graph = WorkflowGraph(
            nodes=[
                _folder("srcA"), _folder("srcB"),
                _bedrock("bed"),
                _capture(),
            ],
            connections=[
                _conn("c1", "srcA", "bed"),
                _conn("c2", "srcB", "bed", target_port="reference"),
                _conn("c3", "bed", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_REFERENCE_CONFLICT) == []

    def test_blank_payload_path_does_not_conflict(self):
        # The descriptor default is "" — a blank/whitespace value means
        # "not configured" and must never conflict with a fed port.
        graph = WorkflowGraph(
            nodes=[
                _folder("srcA"), _folder("srcB"),
                _bedrock("bed", reference_payload_path="   "),
                _capture(),
            ],
            connections=[
                _conn("c1", "srcA", "bed"),
                _conn("c2", "srcB", "bed", target_port="reference"),
                _conn("c3", "bed", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_REFERENCE_CONFLICT) == []


# --------------------------------------------------------------------------
# BEDROCK_CROP_NO_MODEL (warning, Requirement 6.2)
# --------------------------------------------------------------------------

class TestCropNoModel:
    def test_crop_index_without_model_inference_is_one_warning(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(),
                _bedrock("bed", crop_detection_index=0),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "bed"),
                _conn("c2", "bed", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_CROP_NO_MODEL)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_WARNING
        assert found[0].node_id == "bed"
        assert "'bed'" in found[0].message

    def test_fires_once_per_offending_node(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(),
                _bedrock("bed1", crop_detection_index=0),
                _bedrock("bed2", crop_detection_index=1),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "bed1"),
                _conn("c2", "src", "bed2"),
                _conn("c3", "bed1", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_CROP_NO_MODEL)
        assert sorted(f.node_id for f in found) == ["bed1", "bed2"]

    def test_crop_index_with_model_inference_is_clean(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(), _model(),
                _bedrock("bed", crop_detection_index=0),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "src", "bed"),
                _conn("c3", "bed", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_CROP_NO_MODEL) == []

    def test_absent_crop_index_in_modelless_graph_is_clean(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _bedrock("bed"), _capture()],
            connections=[
                _conn("c1", "src", "bed"),
                _conn("c2", "bed", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_CROP_NO_MODEL) == []

    def test_crop_index_zero_is_a_configured_value(self):
        # 0 is falsy but IS a configured index — the warning must still
        # fire in a modelless graph.
        graph = WorkflowGraph(
            nodes=[_folder(), _bedrock("bed", crop_detection_index=0), _capture()],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_CROP_NO_MODEL)
        assert [f.node_id for f in found] == ["bed"]


# --------------------------------------------------------------------------
# BEDROCK_PAYLOAD_NO_TRIGGER (warning, Requirement 6.3)
# --------------------------------------------------------------------------

class TestPayloadNoTrigger:
    def test_payload_path_without_trigger_is_one_warning(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(),
                _bedrock("bed", reference_payload_path="refs.0.image"),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "bed"),
                _conn("c2", "bed", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_PAYLOAD_NO_TRIGGER)
        assert len(found) == 1
        assert found[0].severity == SEVERITY_WARNING
        assert found[0].node_id == "bed"
        assert "'bed'" in found[0].message

    def test_fires_once_per_offending_node(self):
        graph = WorkflowGraph(
            nodes=[
                _folder(),
                _bedrock("bed1", reference_payload_path="refs.0.image"),
                _bedrock("bed2", reference_payload_path="refs.1.image"),
                _capture(),
            ],
        )
        found = _by_code(validate(graph), CODE_BEDROCK_PAYLOAD_NO_TRIGGER)
        assert sorted(f.node_id for f in found) == ["bed1", "bed2"]

    def test_payload_path_with_trigger_is_clean(self):
        graph = WorkflowGraph(
            nodes=[
                _trigger(), _folder(),
                _bedrock("bed", reference_payload_path="refs.0.image"),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "bed"),
                _conn("c2", "bed", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_PAYLOAD_NO_TRIGGER) == []

    def test_absent_payload_path_without_trigger_is_clean(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _bedrock("bed"), _capture()],
        )
        assert _by_code(validate(graph), CODE_BEDROCK_PAYLOAD_NO_TRIGGER) == []


# --------------------------------------------------------------------------
# Compliant graph: zero findings from the three checks
# --------------------------------------------------------------------------

class TestCompliantGraph:
    def _compliant_graph(self):
        """Trigger + model_inference + bedrock using both new features,
        reference port unfed: fully compliant with all three rules."""
        return WorkflowGraph(
            nodes=[
                _trigger(),
                _folder(),
                _model(),
                _bedrock(
                    "bed",
                    reference_payload_path="refs.0.image",
                    crop_detection_index=1,
                    crop_margin_percent=10,
                ),
                _capture(),
            ],
            connections=[
                _conn("c1", "trig", "src",
                      source_port="out", target_port="activation"),
                _conn("c2", "src", "inf"),
                _conn("c3", "inf", "bed",
                      source_port="out", target_port="in"),
                _conn("c4", "bed", "cap"),
            ],
        )

    def test_compliant_graph_has_no_bedrock_findings(self):
        assert _bedrock_findings(validate(self._compliant_graph())) == []

    def test_compliant_graph_has_no_v4_findings_for_new_parameters(self):
        # In-range crop_detection_index / crop_margin_percent values
        # produce no descriptor-constraint errors either.
        findings = validate(self._compliant_graph())
        v4 = _by_code(findings, CODE_V4_INVALID_PARAMETER_VALUE)
        assert [f for f in v4 if f.node_id == "bed"] == []

    def test_bedrock_free_graph_has_no_bedrock_findings(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _capture()],
            connections=[_conn("c1", "src", "cap")],
        )
        assert _bedrock_findings(validate(graph)) == []


# --------------------------------------------------------------------------
# Range constraints ride the V4 descriptor-constraint mechanism
# (Requirement 6.4 — asserted, not re-implemented)
# --------------------------------------------------------------------------

class TestRangeConstraintsRideV4:
    def _graph_with(self, **bedrock_params):
        return WorkflowGraph(
            nodes=[
                _trigger(), _folder(), _model(),
                _bedrock("bed", **bedrock_params),
                _capture(),
            ],
            connections=[
                _conn("c1", "src", "bed"),
                _conn("c2", "bed", "cap"),
            ],
        )

    def test_negative_crop_index_is_a_v4_error_naming_the_parameter(self):
        findings = validate(self._graph_with(crop_detection_index=-1))
        found = [
            f for f in _by_code(findings, CODE_V4_INVALID_PARAMETER_VALUE)
            if f.node_id == "bed"
        ]
        assert len(found) == 1
        assert found[0].severity == SEVERITY_ERROR
        assert "crop_detection_index" in found[0].message
        # Not duplicated by the new bedrock-specific checks.
        assert _by_code(findings, CODE_BEDROCK_CROP_NO_MODEL) == []
        assert _by_code(findings, CODE_BEDROCK_REFERENCE_CONFLICT) == []

    def test_crop_margin_above_100_is_a_v4_error(self):
        findings = validate(self._graph_with(crop_margin_percent=150))
        found = [
            f for f in _by_code(findings, CODE_V4_INVALID_PARAMETER_VALUE)
            if f.node_id == "bed"
        ]
        assert len(found) == 1
        assert "crop_margin_percent" in found[0].message

    def test_negative_crop_margin_is_a_v4_error(self):
        findings = validate(self._graph_with(crop_margin_percent=-5))
        found = [
            f for f in _by_code(findings, CODE_V4_INVALID_PARAMETER_VALUE)
            if f.node_id == "bed"
        ]
        assert len(found) == 1
        assert "crop_margin_percent" in found[0].message

    def test_boundary_values_are_valid(self):
        findings = validate(
            self._graph_with(crop_detection_index=0, crop_margin_percent=100))
        assert [
            f for f in _by_code(findings, CODE_V4_INVALID_PARAMETER_VALUE)
            if f.node_id == "bed"
        ] == []


# --------------------------------------------------------------------------
# Vendored mirror byte-identity (Requirement 6.5)
# --------------------------------------------------------------------------

PORTAL_CHECKS_RELATIVE = Path(
    "edge-cv-portal/backend/layers/workflow_core/python/workflow_core/"
    "validator/checks.py"
)
VENDORED_CHECKS_RELATIVE = Path(
    "src/backend/workflow_engine/vendor/workflow_core/validator/checks.py"
)


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / PORTAL_CHECKS_RELATIVE).is_file() and (
            candidate / VENDORED_CHECKS_RELATIVE
        ).is_file():
            return candidate
    raise AssertionError(
        "Could not locate the repository root containing both "
        f"{PORTAL_CHECKS_RELATIVE} and {VENDORED_CHECKS_RELATIVE}"
    )


class TestVendoredMirrorByteIdentity:
    def test_validator_checks_mirror_is_byte_identical(self):
        root = _repo_root()
        portal_bytes = (root / PORTAL_CHECKS_RELATIVE).read_bytes()
        vendored_bytes = (root / VENDORED_CHECKS_RELATIVE).read_bytes()
        assert portal_bytes == vendored_bytes, (
            "Vendored workflow_core validator mirror is out of sync with "
            "the portal layer copy.\nRe-sync with: cp "
            f"{PORTAL_CHECKS_RELATIVE} {VENDORED_CHECKS_RELATIVE}"
        )
