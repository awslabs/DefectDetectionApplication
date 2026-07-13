"""Unit tests for validate() with all checks (task 3.2).

Covers V1 (input/output presence), V2 (connection port direction and
type compatibility), V3 (cycle detection with cycle membership), V4
(required parameters satisfy constraints), V5 (reachability from input
nodes), and W1 warnings — plus the completeness contract: all checks
always run and the full findings list is returned.

_Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_
"""

from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import (
    CODE_UNKNOWN_NODE_TYPE,
    CODE_V1_NO_INPUT_NODE,
    CODE_V1_NO_OUTPUT_NODE,
    CODE_V2_INCOMPATIBLE_TYPES,
    CODE_V2_SOURCE_NOT_OUTPUT,
    CODE_V2_TARGET_NOT_INPUT,
    CODE_V2_UNKNOWN_NODE,
    CODE_V2_UNKNOWN_PORT,
    CODE_V3_CYCLE,
    CODE_V4_INVALID_PARAMETER_VALUE,
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    CODE_V5_UNREACHABLE_NODE,
    CODE_W1_OUTPUT_NODE_NO_INPUT,
    CODE_W1_UNUSED_OUTPUT_PORT,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    validate,
)

# --------------------------------------------------------------------------
# Graph-building helpers
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


def _rotate(node_id="rot"):
    return _node(node_id, "rotate", method="clockwise")


def _inference(node_id="inf"):
    return _node(node_id, "model_inference", modelName="widget-anomaly-v3")


def _valid_graph():
    """folder_source -> capture: passes every error check."""
    return WorkflowGraph(
        nodes=[_folder(), _capture()],
        connections=[_conn("c1", "src", "cap")],
    )


def _codes(findings):
    return [finding.code for finding in findings]


def _errors(findings):
    return [f for f in findings if f.severity == SEVERITY_ERROR]


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


# --------------------------------------------------------------------------
# Baseline: a valid graph has no error findings
# --------------------------------------------------------------------------

class TestValidGraph:
    def test_valid_graph_has_no_errors(self):
        assert _errors(validate(_valid_graph())) == []

    def test_finding_dict_shape(self):
        findings = validate(WorkflowGraph())
        assert findings, "empty graph must produce findings"
        entry = findings[0].to_dict()
        assert set(entry) == {"severity", "code", "message", "nodeId", "connectionId"}


# --------------------------------------------------------------------------
# V1: at least one input and one output node (Requirement 4.1)
# --------------------------------------------------------------------------

class TestV1:
    def test_missing_input_node_reported(self):
        graph = WorkflowGraph(nodes=[_capture()])
        assert CODE_V1_NO_INPUT_NODE in _codes(validate(graph))

    def test_missing_output_node_reported(self):
        graph = WorkflowGraph(nodes=[_folder()])
        assert CODE_V1_NO_OUTPUT_NODE in _codes(validate(graph))

    def test_empty_graph_reports_both(self):
        codes = _codes(validate(WorkflowGraph()))
        assert CODE_V1_NO_INPUT_NODE in codes
        assert CODE_V1_NO_OUTPUT_NODE in codes

    def test_satisfied_graph_reports_neither(self):
        codes = _codes(validate(_valid_graph()))
        assert CODE_V1_NO_INPUT_NODE not in codes
        assert CODE_V1_NO_OUTPUT_NODE not in codes


# --------------------------------------------------------------------------
# V2: connection direction and type compatibility (Requirement 4.2)
# --------------------------------------------------------------------------

class TestV2:
    def test_incompatible_types_reported_with_connection_id(self):
        # EventSignal (digital_input out) -> VideoFrames (rotate in)
        graph = WorkflowGraph(
            nodes=[_node("din", "digital_input", pin=1), _rotate(), _capture()],
            connections=[_conn("bad", "din", "rot")],
        )
        found = _by_code(validate(graph), CODE_V2_INCOMPATIBLE_TYPES)
        assert len(found) == 1
        assert found[0].connection_id == "bad"
        assert found[0].severity == SEVERITY_ERROR

    def test_declared_coercion_inference_meta_to_video_frames_ok(self):
        # model_inference out (InferenceMeta) -> capture in (VideoFrames)
        graph = WorkflowGraph(
            nodes=[_folder(), _inference(), _capture()],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_V2_INCOMPATIBLE_TYPES) == []

    def test_source_must_be_output_port(self):
        # Using rotate's input port as the connection source.
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate(), _capture()],
            connections=[_conn("c1", "rot", "cap", source_port="in")],
        )
        found = _by_code(validate(graph), CODE_V2_SOURCE_NOT_OUTPUT)
        assert len(found) == 1
        assert found[0].connection_id == "c1"

    def test_target_must_be_input_port(self):
        # Using rotate's output port as the connection target.
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate(), _capture()],
            connections=[_conn("c1", "src", "rot", target_port="out")],
        )
        found = _by_code(validate(graph), CODE_V2_TARGET_NOT_INPUT)
        assert len(found) == 1
        assert found[0].connection_id == "c1"

    def test_unknown_node_reference_reported(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _capture()],
            connections=[_conn("c1", "ghost", "cap")],
        )
        found = _by_code(validate(graph), CODE_V2_UNKNOWN_NODE)
        assert len(found) == 1
        assert found[0].connection_id == "c1"

    def test_unknown_port_reference_reported(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _capture()],
            connections=[_conn("c1", "src", "cap", source_port="nope")],
        )
        found = _by_code(validate(graph), CODE_V2_UNKNOWN_PORT)
        assert len(found) == 1
        assert found[0].connection_id == "c1"

    def test_custom_python_declared_port_types_respected(self):
        # custom_python with declared EventSignal output feeding capture's
        # VideoFrames input must be incompatible.
        custom = _node(
            "py", "custom_python",
            code="def handler(x):\n    return x",
            input_port_type="InferenceMeta",
            output_port_type="EventSignal",
        )
        graph = WorkflowGraph(
            nodes=[_folder(), _inference(), custom, _capture()],
            connections=[
                _conn("c1", "src", "inf"),
                _conn("c2", "inf", "py"),
                _conn("c3", "py", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_V2_INCOMPATIBLE_TYPES)
        assert [f.connection_id for f in found] == ["c3"]


# --------------------------------------------------------------------------
# V3: cycle detection reporting cycle membership (Requirement 4.3)
# --------------------------------------------------------------------------

class TestV3:
    def test_two_node_cycle_reports_both_members(self):
        rot_a = _rotate("rotA")
        rot_b = _rotate("rotB")
        graph = WorkflowGraph(
            nodes=[_folder(), rot_a, rot_b, _capture()],
            connections=[
                _conn("c1", "src", "rotA"),
                _conn("c2", "rotA", "rotB"),
                _conn("c3", "rotB", "rotA"),
                _conn("c4", "rotA", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_V3_CYCLE)
        assert sorted(f.node_id for f in found) == ["rotA", "rotB"]
        for finding in found:
            assert "rotA" in finding.message and "rotB" in finding.message

    def test_self_loop_is_a_cycle(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate(), _capture()],
            connections=[
                _conn("c1", "src", "rot"),
                _conn("c2", "rot", "rot"),
                _conn("c3", "rot", "cap"),
            ],
        )
        found = _by_code(validate(graph), CODE_V3_CYCLE)
        assert [f.node_id for f in found] == ["rot"]

    def test_acyclic_graph_reports_no_cycles(self):
        assert _by_code(validate(_valid_graph()), CODE_V3_CYCLE) == []

    def test_diamond_fanout_is_not_a_cycle(self):
        # src -> rotA -> cap, src -> rotB -> cap (diamond, still a DAG).
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate("rotA"), _rotate("rotB"), _capture()],
            connections=[
                _conn("c1", "src", "rotA"),
                _conn("c2", "src", "rotB"),
                _conn("c3", "rotA", "cap"),
                _conn("c4", "rotB", "cap"),
            ],
        )
        assert _by_code(validate(graph), CODE_V3_CYCLE) == []


# --------------------------------------------------------------------------
# V4: required parameters satisfy constraints (Requirement 4.4)
# --------------------------------------------------------------------------

class TestV4:
    def test_missing_required_parameter_identifies_node_and_parameter(self):
        graph = WorkflowGraph(
            nodes=[_node("src2", "folder_source"), _capture()],
            connections=[_conn("c1", "src2", "cap")],
        )
        found = _by_code(validate(graph), CODE_V4_MISSING_REQUIRED_PARAMETER)
        assert len(found) == 1
        assert found[0].node_id == "src2"
        assert "location" in found[0].message

    def test_constraint_violation_reported(self):
        # capture quality must be 1..100
        bad_capture = _node("cap", "capture", output_path="/out", quality=200)
        graph = WorkflowGraph(
            nodes=[_folder(), bad_capture],
            connections=[_conn("c1", "src", "cap")],
        )
        found = _by_code(validate(graph), CODE_V4_INVALID_PARAMETER_VALUE)
        assert len(found) == 1
        assert found[0].node_id == "cap"
        assert "quality" in found[0].message

    def test_required_parameter_with_default_is_satisfied_when_omitted(self):
        # rotate.method is required but has default "clockwise".
        graph = WorkflowGraph(
            nodes=[_folder(), _node("rot", "rotate"), _capture()],
            connections=[_conn("c1", "src", "rot"), _conn("c2", "rot", "cap")],
        )
        assert _by_code(validate(graph), CODE_V4_MISSING_REQUIRED_PARAMETER) == []

    def test_explicit_null_clears_a_required_parameter(self):
        graph = WorkflowGraph(
            nodes=[_node("src2", "folder_source", location=None), _capture()],
            connections=[_conn("c1", "src2", "cap")],
        )
        found = _by_code(validate(graph), CODE_V4_MISSING_REQUIRED_PARAMETER)
        assert [f.node_id for f in found] == ["src2"]


# --------------------------------------------------------------------------
# V5: reachability from input nodes (Requirement 4.5)
# --------------------------------------------------------------------------

class TestV5:
    def test_detached_node_reported_unreachable(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _capture(), _rotate("stray")],
            connections=[_conn("c1", "src", "cap")],
        )
        found = _by_code(validate(graph), CODE_V5_UNREACHABLE_NODE)
        assert [f.node_id for f in found] == ["stray"]

    def test_transitively_connected_nodes_are_reachable(self):
        graph = WorkflowGraph(
            nodes=[_folder(), _rotate(), _capture()],
            connections=[_conn("c1", "src", "rot"), _conn("c2", "rot", "cap")],
        )
        assert _by_code(validate(graph), CODE_V5_UNREACHABLE_NODE) == []

    def test_input_nodes_are_reachable_roots(self):
        graph = WorkflowGraph(nodes=[_folder(), _capture()])
        found = _by_code(validate(graph), CODE_V5_UNREACHABLE_NODE)
        # The capture node has no path from src; src itself is a root.
        assert [f.node_id for f in found] == ["cap"]

    def test_no_input_nodes_means_all_nodes_unreachable(self):
        graph = WorkflowGraph(
            nodes=[_rotate(), _capture()],
            connections=[_conn("c1", "rot", "cap")],
        )
        found = _by_code(validate(graph), CODE_V5_UNREACHABLE_NODE)
        assert sorted(f.node_id for f in found) == ["cap", "rot"]


# --------------------------------------------------------------------------
# W1: warnings (Requirement 4.6)
# --------------------------------------------------------------------------

class TestW1:
    def test_output_node_without_incoming_connection_warns(self):
        graph = WorkflowGraph(nodes=[_folder(), _capture()])
        found = _by_code(validate(graph), CODE_W1_OUTPUT_NODE_NO_INPUT)
        assert [f.node_id for f in found] == ["cap"]
        assert all(f.severity == SEVERITY_WARNING for f in found)

    def test_unused_output_port_warns(self):
        graph = WorkflowGraph(nodes=[_folder(), _capture()])
        found = _by_code(validate(graph), CODE_W1_UNUSED_OUTPUT_PORT)
        assert [f.node_id for f in found] == ["src"]
        assert all(f.severity == SEVERITY_WARNING for f in found)

    def test_fully_wired_graph_has_no_warnings(self):
        findings = validate(_valid_graph())
        assert [f for f in findings if f.severity == SEVERITY_WARNING] == []


# --------------------------------------------------------------------------
# Completeness: all checks run, complete list returned (Requirement 4.6)
# --------------------------------------------------------------------------

class TestCompleteness:
    def test_multiple_defect_classes_reported_together(self):
        """A graph seeded with V1/V2/V3/V4/V5 defects plus a W1 condition
        yields findings for every class in a single validate() call."""
        rot_a = _rotate("rotA")
        rot_b = _rotate("rotB")
        graph = WorkflowGraph(
            nodes=[
                # No input node at all (V1).
                rot_a, rot_b,
                _node("filt", "inference_filter"),        # missing condition (V4)
                _node("stray", "rotate", method="clockwise"),  # detached (V5)
                _capture(),                               # no incoming connection (W1)
            ],
            connections=[
                _conn("c1", "rotA", "rotB"),
                _conn("c2", "rotB", "rotA"),              # cycle (V3)
                _conn("c3", "rotA", "filt"),              # VideoFrames -> InferenceMeta (V2)
            ],
        )
        codes = set(_codes(validate(graph)))
        assert CODE_V1_NO_INPUT_NODE in codes
        assert CODE_V2_INCOMPATIBLE_TYPES in codes
        assert CODE_V3_CYCLE in codes
        assert CODE_V4_MISSING_REQUIRED_PARAMETER in codes
        assert CODE_V5_UNREACHABLE_NODE in codes
        assert CODE_W1_OUTPUT_NODE_NO_INPUT in codes

    def test_unknown_node_type_reported_and_does_not_crash_other_checks(self):
        graph = WorkflowGraph(
            nodes=[_node("mystery", "not_a_real_type"), _folder(), _capture()],
            connections=[
                _conn("c1", "src", "cap"),
                _conn("c2", "mystery", "cap"),
            ],
        )
        findings = validate(graph)
        unknown = _by_code(findings, CODE_UNKNOWN_NODE_TYPE)
        assert [f.node_id for f in unknown] == ["mystery"]
        # V1 is still satisfied by the known nodes; no crash occurred and
        # the unknown-typed node is excluded from reachability roots.
        assert CODE_V1_NO_INPUT_NODE not in _codes(findings)
