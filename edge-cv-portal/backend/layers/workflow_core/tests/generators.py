"""Shared hypothesis generators for workflow_core property tests (task 2.3).

Three families of generators, used by the serializer, validator, and
compiler property tests (tasks 2.4, 2.5, 3.4, 4.3-4.8):

1. ``graph_strategy`` - random *valid* Workflow_Definitions built from the
   node catalog: random node subsets, valid parameter values,
   type-compatible DAG wiring, and optional fan-out (a drawn "hub" mode
   funnels every consumer onto the first compatible source, producing
   maximal fan-out). Edge cases covered: empty/whitespace/unicode strings
   in parameter values and node/connection ids, minimal two-node graphs,
   and maximal fan-out. Every generated graph passes ``validate()`` with
   no error-severity findings (warnings such as unused output ports may
   be present).

   ``single_node_graph_strategy`` complements it with well-formed
   single-node graphs (serializable, parseable) for serializer edge
   cases; a single node can never satisfy validator check V1, so these
   are intentionally *not* validator-valid.

2. Defect-seeding combinators - ``seeded_graph_strategy`` produces
   controlled *invalid* graphs for a drawn (or caller-fixed) set of
   defect classes: missing input/output nodes, incompatible-port
   connections, injected cycles, cleared required parameters, and
   detached unreachable nodes. Each :class:`SeededGraph` carries the
   exact set of error-severity findings ``validate()`` must return
   (including findings implied by a seeding, e.g. removing all input
   nodes necessarily makes every remaining node unreachable), so the
   finding-set exactness property (Property 3) can compare directly.

3. Schema-corrupting document mutators - ``corrupted_document_strategy``
   serializes a well-formed graph and applies one drawn corruption
   (dropped required keys, wrong types, bad schema versions, extra
   properties, duplicate ids, dangling node references). Every produced
   document is rejected by ``parse()`` with a descriptive error.

**Validates: Requirements 3.4, 4.6, 6.6**
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from hypothesis import strategies as st

from workflow_core.catalog import (
    NODE_CATALOG,
    PORT_TYPES,
    are_port_types_compatible,
    get_node_type,
)
from workflow_core.catalog.models import ParameterDescriptor
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
    graph_to_document,
)
from workflow_core.validator import (
    CODE_V1_NO_INPUT_NODE,
    CODE_V1_NO_OUTPUT_NODE,
    CODE_V2_INCOMPATIBLE_TYPES,
    CODE_V2_SOURCE_NOT_OUTPUT,
    CODE_V2_TARGET_NOT_INPUT,
    CODE_V3_CYCLE,
    CODE_V4_MISSING_REQUIRED_PARAMETER,
    CODE_V5_UNREACHABLE_NODE,
    COEXISTENCE_SINGLETON_TYPES,
    check_parameter_value,
    is_parameter_value_valid,
)

__all__ = [
    "graph_strategy",
    "modbus_write_graph_strategy",
    "single_node_graph_strategy",
    "valid_parameter_value_strategy",
    "node_parameters_strategy",
    "DEFECT_MISSING_INPUT_NODE",
    "DEFECT_MISSING_OUTPUT_NODE",
    "DEFECT_INCOMPATIBLE_CONNECTION",
    "DEFECT_CYCLE",
    "DEFECT_CLEARED_REQUIRED_PARAMETER",
    "DEFECT_UNREACHABLE_NODE",
    "ALL_DEFECT_CLASSES",
    "ExpectedFinding",
    "SeededGraph",
    "seeded_graph_strategy",
    "corrupted_document_strategy",
]

# ---------------------------------------------------------------------------
# Catalog groupings used for wiring-feasible type selection
# ---------------------------------------------------------------------------

#: Input node types that emit VideoFrames (a graph always gets one so a
#: downstream chain is guaranteed to be wireable).
_VIDEO_INPUT_TYPES = ("csi_camera_source", "icam_source",
                      "aravis_camera_source", "folder_source")

#: All input-category node types.
_INPUT_TYPES = ("csi_camera_source", "icam_source", "aravis_camera_source",
                "folder_source", "digital_input")

#: Intermediate (non-input, non-output) node types. ``conditional`` is the
#: multi-output executor node: both of its output ports register as
#: available sources, so downstream consumers may wire to either path.
_INTERMEDIATE_TYPES = (
    "dewarp",
    "rotate",
    "crop",
    "format_convert",
    "model_inference",
    "custom_python",
    "inference_filter",
    "conditional",
)

#: Intermediate types safe to place without any connection (used when a
#: seeded graph deliberately has no input nodes): no per-instance port
#: typing to coordinate, and all parameters can be generated valid.
_DETACHED_SAFE_INTERMEDIATE_TYPES = (
    "dewarp",
    "rotate",
    "crop",
    "format_convert",
    "model_inference",
    "inference_filter",
    "conditional",
)

#: Output-category node types.
_OUTPUT_TYPES = ("digital_output", "mqtt_publish", "opcua_write", "capture")

#: Per-instance port typing parameters (custom_python). Clearing these
#: would change port resolution and cascade into V2 findings, so the
#: cleared-required-parameter combinator never touches them.
_PORT_TYPING_PARAMETER_NAMES = frozenset({"input_port_type", "output_port_type"})

# ---------------------------------------------------------------------------
# Parameter value strategies (valid values only)
# ---------------------------------------------------------------------------

#: Curated strings covering the required edge cases: empty, whitespace-only,
#: unicode, and embedded/surrounding whitespace. Filtered per-descriptor
#: against length constraints below.
_CURATED_STRINGS = (
    "",
    " ",
    "\t",
    "  \n\t ",
    "0",
    "with space",
    " padded ",
    "naïve",
    "ノード-Ω✓",
    "définition-ワークフロー",
)

_STRING_LIKE_TYPES = ("string", "code", "model_ref")


def valid_parameter_value_strategy(descriptor: ParameterDescriptor) -> st.SearchStrategy:
    """A strategy of values that satisfy ``descriptor``'s type and constraints."""
    constraints = descriptor.constraints or {}

    if "values" in constraints:
        # Enums and discrete value sets: membership is the whole rule.
        return st.sampled_from(list(constraints["values"]))

    if descriptor.param_type in _STRING_LIKE_TYPES:
        return _string_value_strategy(constraints)
    if descriptor.param_type == "int":
        return st.integers(
            min_value=constraints.get("min"), max_value=constraints.get("max")
        )
    if descriptor.param_type == "float":
        # ``min_exclusive`` is a strict lower bound (e.g. llm_inference's
        # top_p > 0.0); ``min`` stays inclusive.
        min_exclusive = constraints.get("min_exclusive")
        if min_exclusive is not None:
            return st.floats(
                min_value=min_exclusive,
                max_value=constraints.get("max"),
                exclude_min=True,
                allow_nan=False,
                allow_infinity=False,
            )
        return st.floats(
            min_value=constraints.get("min"),
            max_value=constraints.get("max"),
            allow_nan=False,
            allow_infinity=False,
        )
    if descriptor.param_type == "bool":
        return st.booleans()

    raise ValueError(
        "no value strategy for parameter type {!r}".format(descriptor.param_type)
    )


def _string_value_strategy(constraints: Dict[str, Any]) -> st.SearchStrategy:
    min_length = constraints.get("min_length", 0)
    max_length = constraints.get("max_length")
    pattern = constraints.get("regex")

    if pattern is not None:
        base = st.from_regex(pattern)
    else:
        base = st.one_of(
            st.sampled_from(_CURATED_STRINGS),
            st.text(max_size=max_length if max_length is not None else 24),
        )

    def satisfies_lengths(value: str) -> bool:
        if len(value) < min_length:
            return False
        if max_length is not None and len(value) > max_length:
            return False
        return True

    return base.filter(satisfies_lengths)


@st.composite
def node_parameters_strategy(draw, descriptor, forced: Optional[Dict[str, Any]] = None):
    """Valid parameter values for one node of type ``descriptor``.

    ``forced`` entries are used verbatim (the graph builders force
    custom_python's per-instance port types to match the wiring). Other
    parameters are either omitted (when omission is valid, i.e. the
    parameter is optional or its default satisfies its constraints) or
    given a drawn valid value.
    """
    forced = dict(forced or {})
    parameters: Dict[str, Any] = {}
    for parameter in descriptor.parameters:
        if parameter.name in forced:
            parameters[parameter.name] = forced[parameter.name]
            continue
        omission_valid = check_parameter_value(parameter, parameter.default) is None
        if omission_valid and draw(st.booleans()):
            continue
        value = draw(valid_parameter_value_strategy(parameter))
        assert is_parameter_value_valid(parameter, value)
        parameters[parameter.name] = value

    # Bug 2 (workflow-manager-integration-bugfixes, Requirements 2.2/3.2):
    # broker_host is no longer statically required, so an mqtt_publish node
    # with every optional parameter omitted declares no publish target and is
    # rejected by the V6 check. That is a genuinely-invalid config, not a
    # seeded defect, so guarantee a target here to keep the generator emitting
    # valid mqtt_publish nodes (enable the off-by-default Greengrass path when
    # neither aws_iot nor a non-empty broker_host is present).
    if getattr(descriptor, "type_id", None) == "mqtt_publish":
        broker_host = parameters.get("broker_host")
        has_broker_host = isinstance(broker_host, str) and broker_host.strip() != ""
        if not (
            bool(parameters.get("greengrass"))
            or bool(parameters.get("aws_iot"))
            or has_broker_host
        ):
            parameters["greengrass"] = True
    return parameters


# ---------------------------------------------------------------------------
# Graph builder (shared by the valid and defect-seeded strategies)
# ---------------------------------------------------------------------------

_POSITION_COORDINATE = st.floats(
    min_value=-5000, max_value=5000, allow_nan=False, allow_infinity=False
)

#: Id prefixes exercise unicode and embedded whitespace in identifiers.
_NODE_ID_PREFIXES = ("n", "node-", "ノード", "n ")
_CONNECTION_ID_PREFIXES = ("c", "conn-", "接続 ")


class _GraphBuilder:
    """Accumulates nodes/connections while tracking available output ports."""

    def __init__(self, draw):
        self._draw = draw
        self._node_prefix = draw(st.sampled_from(_NODE_ID_PREFIXES))
        self._connection_prefix = draw(st.sampled_from(_CONNECTION_ID_PREFIXES))
        self._node_counter = 0
        self._connection_counter = 0
        self.nodes: List[Node] = []
        self.connections: List[Connection] = []
        #: (node_id, port_name, effective_port_type) for every output port.
        self.sources: List[Tuple[str, str, str]] = []
        #: Coexistence-singleton node types already present in the graph
        #: (see :meth:`coexistence_safe`).
        self._singleton_types_present: set = set()

    def coexistence_safe(self, type_ids: Sequence[str]) -> Tuple[str, ...]:
        """``type_ids`` minus any coexistence-singleton type the graph
        already contains.

        The device runtime allows at most one node of each type in
        :data:`COEXISTENCE_SINGLETON_TYPES` per workflow — today
        ``aravis_camera_source``, whose single-frame appsrc Frame_Feed
        supports exactly one Aravis camera source per workflow
        (``workflow_engine.aravis_feed.plan_aravis_feeds`` raises on
        documents carrying more than one Aravis binding point). The
        validator's V7_COEXISTENCE_CONFLICT check
        (portal-build-fleet-and-workflow-gates Requirement 8.2) enforces
        the same contract, so *valid*-graph generation must never emit a
        second instance of such a type.
        """
        return tuple(
            type_id for type_id in type_ids
            if not (type_id in COEXISTENCE_SINGLETON_TYPES
                    and type_id in self._singleton_types_present)
        )

    def add_node(self, type_id: str, forced_params: Optional[Dict[str, Any]] = None,
                 register_outputs: bool = True) -> Node:
        descriptor = get_node_type(type_id)
        assert descriptor is not None, type_id
        if type_id in COEXISTENCE_SINGLETON_TYPES:
            self._singleton_types_present.add(type_id)
        self._node_counter += 1
        node = Node(
            id="{}{}".format(self._node_prefix, self._node_counter),
            type=type_id,
            position=Position(
                x=self._draw(_POSITION_COORDINATE),
                y=self._draw(_POSITION_COORDINATE),
            ),
            parameters=self._draw(node_parameters_strategy(descriptor, forced_params)),
        )
        self.nodes.append(node)

        if not register_outputs:
            return node
        output_override = None
        parameter_names = {p.name for p in descriptor.parameters}
        if "output_port_type" in parameter_names:
            candidate = node.parameters.get("output_port_type")
            if candidate in PORT_TYPES:
                output_override = candidate
        for port in descriptor.outputs:
            self.sources.append(
                (node.id, port.name, output_override or port.port_type)
            )
        return node

    def add_bedrock_node(self) -> Node:
        """Add a two-input bedrock_inference node fed by dedicated fresh
        VideoFrames sources (Requirement: two-input inference wiring).

        The feeders' output ports are deliberately NOT registered as
        wiring sources for other consumers, and neither is the bedrock
        node's InferenceMeta output: on device architectures the
        compiler terminates each feeding branch in a frame-capture sink
        (frames do not flow through the node), so generated graphs keep
        bedrock branches self-contained — matching the topology the
        compiler supports while still exercising the two-input node
        through every property. A drawn boolean shares one feeder
        between both input ports (same-source comparison) or gives each
        port its own feeder.
        """
        shared_feeder = self._draw(st.booleans())
        # coexistence_safe: at most one aravis_camera_source per graph
        # (see the method docstring for the runtime-contract grounding).
        first = self.add_node(
            self._draw(st.sampled_from(self.coexistence_safe(_VIDEO_INPUT_TYPES))),
            register_outputs=False,
        )
        second = (
            first if shared_feeder
            else self.add_node(
                self._draw(
                    st.sampled_from(self.coexistence_safe(_VIDEO_INPUT_TYPES))
                ),
                register_outputs=False,
            )
        )
        bedrock = self.add_node("bedrock_inference", register_outputs=False)
        self.connect((first.id, "out"), bedrock.id, "in")
        self.connect((second.id, "out"), bedrock.id, "reference")
        return bedrock

    def connect(self, source: Tuple[str, str], target_node_id: str, target_port: str) -> Connection:
        self._connection_counter += 1
        connection = Connection(
            id="{}{}".format(self._connection_prefix, self._connection_counter),
            source=PortEndpoint(node=source[0], port=source[1]),
            target=PortEndpoint(node=target_node_id, port=target_port),
        )
        self.connections.append(connection)
        return connection

    def compatible_sources(self, input_type: str) -> List[Tuple[str, str, str]]:
        return [
            source for source in self.sources
            if are_port_types_compatible(source[2], input_type)
        ]

    def add_wired_consumer(self, type_id: str, hub: bool) -> Node:
        """Add a node of ``type_id`` fed from a type-compatible existing
        output port; ``hub`` mode always picks the first compatible source,
        producing maximal fan-out on that source."""
        descriptor = get_node_type(type_id)
        input_port = descriptor.inputs[0]

        if type_id == "custom_python":
            # Per-instance port typing (Requirement 2.7): declare the input
            # type to exactly match the chosen source's output type.
            candidates = self.sources
            source = candidates[0] if hub else self._draw(st.sampled_from(candidates))
            forced = {
                "input_port_type": source[2],
                "output_port_type": self._draw(st.sampled_from(PORT_TYPES)),
            }
        else:
            candidates = self.compatible_sources(input_port.port_type)
            source = candidates[0] if hub else self._draw(st.sampled_from(candidates))
            forced = None

        node = self.add_node(type_id, forced)
        self.connect((source[0], source[1]), node.id, input_port.name)
        return node

    def feasible_consumer_types(self, type_ids: Sequence[str]) -> List[str]:
        """The subset of ``type_ids`` whose single input port can be fed by
        some currently available output port."""
        available_types = {source[2] for source in self.sources}
        feasible = []
        for type_id in type_ids:
            if type_id == "custom_python":
                if self.sources:
                    feasible.append(type_id)
                continue
            descriptor = get_node_type(type_id)
            input_type = descriptor.inputs[0].port_type
            if any(are_port_types_compatible(t, input_type) for t in available_types):
                feasible.append(type_id)
        return feasible

    def build(self) -> WorkflowGraph:
        return WorkflowGraph(nodes=self.nodes, connections=self.connections)


# ---------------------------------------------------------------------------
# 1. Valid workflow graphs
# ---------------------------------------------------------------------------

@st.composite
def graph_strategy(
    draw,
    max_intermediates: int = 4,
    max_extra_inputs: int = 2,
    max_extra_outputs: int = 2,
):
    """Random *valid* Workflow_Definition graphs from the node catalog.

    Guarantees (checked by the smoke tests in ``test_generators.py``):
    ``validate(graph)`` returns no error-severity findings, and the graph
    serializes canonically. Structure: one guaranteed VideoFrames input,
    optional extra inputs, 0..max_intermediates wired intermediate nodes,
    and 1..1+max_extra_outputs wired output nodes; wiring is always
    forward (DAG) and type-compatible. A drawn hub mode funnels all
    consumers onto the first compatible source (maximal fan-out).
    """
    builder = _GraphBuilder(draw)
    hub = draw(st.booleans())

    # V1 + wiring feasibility: at least one VideoFrames-producing input.
    # coexistence_safe keeps the graph valid under the V7-coexistence rule
    # (portal-build-fleet-and-workflow-gates Requirement 8.2): at most one
    # aravis_camera_source per workflow, matching the device runtime's
    # single Frame_Feed contract (workflow_engine.aravis_feed.
    # plan_aravis_feeds rejects >1 Aravis binding point per workflow).
    builder.add_node(draw(st.sampled_from(_VIDEO_INPUT_TYPES)))
    for _ in range(draw(st.integers(min_value=0, max_value=max_extra_inputs))):
        builder.add_node(draw(st.sampled_from(builder.coexistence_safe(_INPUT_TYPES))))

    for _ in range(draw(st.integers(min_value=0, max_value=max_intermediates))):
        feasible = builder.feasible_consumer_types(_INTERMEDIATE_TYPES)
        builder.add_wired_consumer(draw(st.sampled_from(feasible)), hub)

    # Optionally exercise the two-input Bedrock inference node with its
    # dedicated feeder sources (see add_bedrock_node).
    if draw(st.booleans()):
        builder.add_bedrock_node()

    for _ in range(1 + draw(st.integers(min_value=0, max_value=max_extra_outputs))):
        feasible = builder.feasible_consumer_types(_OUTPUT_TYPES)
        builder.add_wired_consumer(draw(st.sampled_from(feasible)), hub)

    return builder.build()


@st.composite
def modbus_write_graph_strategy(draw, max_modbus_nodes=3, max_intermediates=2):
    """Random *valid* graphs guaranteed to contain 1..max_modbus_nodes
    ``modbus_write`` output nodes (modbus-tcp-output Properties 10-12).

    Structure: one VideoFrames input feeding a ``model_inference`` node
    (guaranteeing an InferenceMeta source for the Modbus nodes' ``in``
    port), 0..max_intermediates wired intermediate nodes, a guaranteed
    ``capture`` output keeping the graph valid regardless of wiring, and
    1..max_modbus_nodes wired ``modbus_write`` nodes with drawn valid
    parameter values (optionals randomly omitted so effective-parameter
    resolution covers both explicit values and applied defaults).
    """
    builder = _GraphBuilder(draw)
    hub = draw(st.booleans())

    builder.add_node(draw(st.sampled_from(_VIDEO_INPUT_TYPES)))
    builder.add_wired_consumer("model_inference", hub)

    for _ in range(draw(st.integers(min_value=0, max_value=max_intermediates))):
        feasible = builder.feasible_consumer_types(_INTERMEDIATE_TYPES)
        builder.add_wired_consumer(draw(st.sampled_from(feasible)), hub)

    builder.add_wired_consumer("capture", hub)
    for _ in range(draw(st.integers(min_value=1, max_value=max_modbus_nodes))):
        builder.add_wired_consumer("modbus_write", hub)

    return builder.build()


@st.composite
def single_node_graph_strategy(draw):
    """Well-formed single-node graphs (serializer edge case).

    Serializable and parseable, with valid parameter values — but a single
    node can never satisfy validator check V1 (a valid workflow needs both
    an input and an output node), so these graphs are not validator-valid.
    """
    builder = _GraphBuilder(draw)
    builder.add_node(draw(st.sampled_from([d.type_id for d in NODE_CATALOG])))
    return builder.build()


# ---------------------------------------------------------------------------
# 2. Defect-seeding combinators
# ---------------------------------------------------------------------------

DEFECT_MISSING_INPUT_NODE = "missing_input_node"
DEFECT_MISSING_OUTPUT_NODE = "missing_output_node"
DEFECT_INCOMPATIBLE_CONNECTION = "incompatible_connection"
DEFECT_CYCLE = "cycle"
DEFECT_CLEARED_REQUIRED_PARAMETER = "cleared_required_parameter"
DEFECT_UNREACHABLE_NODE = "unreachable_node"

ALL_DEFECT_CLASSES = (
    DEFECT_MISSING_INPUT_NODE,
    DEFECT_MISSING_OUTPUT_NODE,
    DEFECT_INCOMPATIBLE_CONNECTION,
    DEFECT_CYCLE,
    DEFECT_CLEARED_REQUIRED_PARAMETER,
    DEFECT_UNREACHABLE_NODE,
)


@dataclass(frozen=True)
class ExpectedFinding:
    """One error-severity finding ``validate()`` must return."""

    code: str
    node_id: Optional[str] = None
    connection_id: Optional[str] = None


@dataclass(frozen=True, eq=False)
class SeededGraph:
    """A deliberately invalid graph plus its exact expected error findings.

    ``expected`` is the complete set of *error-severity* findings
    ``validate(graph)`` must return — the seeded defects and their implied
    consequences (e.g. with no input nodes, every node is unreachable), and
    nothing else. Warning-severity findings are not constrained.
    """

    graph: WorkflowGraph
    defects: FrozenSet[str]
    expected: FrozenSet[ExpectedFinding]


@st.composite
def seeded_graph_strategy(draw, defect_classes: Optional[Sequence[str]] = None):
    """Controlled invalid graphs for a set of defect classes.

    Draws a nonempty subset of :data:`ALL_DEFECT_CLASSES` (or uses the
    caller-fixed ``defect_classes``) and constructs a graph containing
    exactly those defect classes. The returned :class:`SeededGraph` lists
    the exact expected error findings, so the validator finding-set
    exactness property can assert equality.
    """
    if defect_classes is None:
        defects = frozenset(
            draw(st.sets(st.sampled_from(ALL_DEFECT_CLASSES), min_size=1))
        )
    else:
        defects = frozenset(defect_classes)
        unknown = defects - set(ALL_DEFECT_CLASSES)
        if unknown:
            raise ValueError("unknown defect classes: {}".format(sorted(unknown)))
        if not defects:
            raise ValueError("at least one defect class is required")

    include_inputs = DEFECT_MISSING_INPUT_NODE not in defects
    include_outputs = DEFECT_MISSING_OUTPUT_NODE not in defects

    builder = _GraphBuilder(draw)
    hub = draw(st.booleans())
    expected: set = set()
    #: Nodes deliberately left unreachable while input nodes exist.
    detached_node_ids: List[str] = []

    # --- valid base structure (minus the classes seeded by omission) ------
    if include_inputs:
        # coexistence_safe: at most one aravis_camera_source (singleton
        # runtime contract, see graph_strategy) so seeded graphs never
        # carry V7_COEXISTENCE_CONFLICT findings beyond `expected`.
        builder.add_node(draw(st.sampled_from(_VIDEO_INPUT_TYPES)))
        for _ in range(draw(st.integers(min_value=0, max_value=1))):
            builder.add_node(
                draw(st.sampled_from(builder.coexistence_safe(_INPUT_TYPES)))
            )
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            feasible = builder.feasible_consumer_types(_INTERMEDIATE_TYPES)
            builder.add_wired_consumer(draw(st.sampled_from(feasible)), hub)
    else:
        # No input nodes: intermediates are placed detached (every node
        # will be unreachable anyway; see the V5 expectation below).
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            builder.add_node(
                draw(st.sampled_from(_DETACHED_SAFE_INTERMEDIATE_TYPES))
            )

    if include_outputs:
        for _ in range(1 + draw(st.integers(min_value=0, max_value=1))):
            if include_inputs:
                feasible = builder.feasible_consumer_types(_OUTPUT_TYPES)
                builder.add_wired_consumer(draw(st.sampled_from(feasible)), hub)
            else:
                builder.add_node(draw(st.sampled_from(_OUTPUT_TYPES)))

    # --- injected cycle (V3) ----------------------------------------------
    if DEFECT_CYCLE in defects:
        cycle_length = draw(st.integers(min_value=1, max_value=3))
        if include_inputs:
            # Choose the reachability feed *before* adding the cycle nodes
            # so the cycle is never "fed" from inside itself.
            feed = draw(st.sampled_from(builder.compatible_sources("VideoFrames")))
        cycle_nodes = [builder.add_node("rotate") for _ in range(cycle_length)]
        if include_inputs:
            # Keep the cycle reachable so no V5 findings are implied.
            builder.connect((feed[0], feed[1]), cycle_nodes[0].id, "in")
        for position, node in enumerate(cycle_nodes):
            successor = cycle_nodes[(position + 1) % cycle_length]
            builder.connect((node.id, "out"), successor.id, "in")
        for node in cycle_nodes:
            expected.add(ExpectedFinding(CODE_V3_CYCLE, node_id=node.id))

    # --- incompatible-port connection (V2) --------------------------------
    if DEFECT_INCOMPATIBLE_CONNECTION in defects:
        # A fresh VideoFrames-typed feeder, wired for reachability when
        # possible; the bad connection then targets a fresh node so it can
        # never create a cycle or unseat other invariants.
        if include_inputs:
            feeder = builder.add_wired_consumer("rotate", hub)
        else:
            feeder = builder.add_node("rotate")
        variant = draw(
            st.sampled_from(("incompatible_types", "source_not_output", "target_not_input"))
        )
        if variant == "incompatible_types":
            # VideoFrames output -> InferenceMeta input: no coercion exists.
            target = builder.add_node("inference_filter")
            bad = builder.connect((feeder.id, "out"), target.id, "in")
            expected.add(
                ExpectedFinding(CODE_V2_INCOMPATIBLE_TYPES, connection_id=bad.id)
            )
        elif variant == "source_not_output":
            target = builder.add_node("rotate")
            bad = builder.connect((feeder.id, "in"), target.id, "in")
            expected.add(
                ExpectedFinding(CODE_V2_SOURCE_NOT_OUTPUT, connection_id=bad.id)
            )
        else:  # target_not_input
            target = builder.add_node("rotate")
            bad = builder.connect((feeder.id, "out"), target.id, "out")
            expected.add(
                ExpectedFinding(CODE_V2_TARGET_NOT_INPUT, connection_id=bad.id)
            )
        # Reachability note: V5's BFS follows connections regardless of
        # port validity, so `target` is reachable through the bad edge
        # whenever `feeder` is.

    # --- detached unreachable node (V5) ------------------------------------
    if DEFECT_UNREACHABLE_NODE in defects:
        detached = builder.add_node(
            draw(st.sampled_from(_DETACHED_SAFE_INTERMEDIATE_TYPES))
        )
        detached_node_ids.append(detached.id)

    # --- cleared required parameter (V4) ------------------------------------
    if DEFECT_CLEARED_REQUIRED_PARAMETER in defects:
        candidates = []
        for node in builder.nodes:
            descriptor = get_node_type(node.type)
            for parameter in descriptor.parameters:
                if parameter.required and parameter.name not in _PORT_TYPING_PARAMETER_NAMES:
                    candidates.append((node, parameter.name))
        if not candidates:
            # Guarantee a target: a fresh rotate node ('method' is required),
            # wired for reachability when inputs exist.
            if include_inputs:
                fresh = builder.add_wired_consumer("rotate", hub)
            else:
                fresh = builder.add_node("rotate")
            candidates = [(fresh, "method")]
        node, parameter_name = draw(st.sampled_from(candidates))
        # An explicit null counts as cleared (validator V4 semantics).
        node.parameters[parameter_name] = None
        expected.add(
            ExpectedFinding(CODE_V4_MISSING_REQUIRED_PARAMETER, node_id=node.id)
        )

    # --- V1 and V5 expectations ---------------------------------------------
    if not include_inputs:
        expected.add(ExpectedFinding(CODE_V1_NO_INPUT_NODE))
        # With no input nodes there are no BFS roots: every node in the
        # final graph is unreachable.
        for node in builder.nodes:
            expected.add(ExpectedFinding(CODE_V5_UNREACHABLE_NODE, node_id=node.id))
    else:
        for node_id in detached_node_ids:
            expected.add(ExpectedFinding(CODE_V5_UNREACHABLE_NODE, node_id=node_id))
    if not include_outputs:
        expected.add(ExpectedFinding(CODE_V1_NO_OUTPUT_NODE))

    return SeededGraph(
        graph=builder.build(), defects=defects, expected=frozenset(expected)
    )


# ---------------------------------------------------------------------------
# 3. Schema-corrupting document mutators
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _DocumentMutation:
    name: str
    applicable: Callable[[Dict[str, Any]], bool]
    apply: Callable[..., None]  # (draw, document) -> None, mutates in place


def _always(_document: Dict[str, Any]) -> bool:
    return True


def _has_nodes(document: Dict[str, Any]) -> bool:
    return bool(document.get("nodes"))


def _has_connections(document: Dict[str, Any]) -> bool:
    return bool(document.get("connections"))


def _pick_node(draw, document):
    nodes = document["nodes"]
    return nodes[draw(st.integers(min_value=0, max_value=len(nodes) - 1))]


def _pick_connection(draw, document):
    connections = document["connections"]
    return connections[draw(st.integers(min_value=0, max_value=len(connections) - 1))]


def _drop_top_level_key(draw, document):
    del document[draw(st.sampled_from(("schemaVersion", "nodes", "connections")))]


def _bad_schema_version(draw, document):
    document["schemaVersion"] = draw(st.sampled_from((0, -1, 2, 99, "1", None, True)))


def _wrong_top_level_type(draw, document):
    key = draw(st.sampled_from(("nodes", "connections")))
    document[key] = draw(st.sampled_from(({}, "not-a-list", 5)))


def _extra_top_level_property(_draw, document):
    document["unexpectedProperty"] = 1


def _drop_node_key(draw, document):
    node = _pick_node(draw, document)
    del node[draw(st.sampled_from(("id", "type", "position", "parameters")))]


def _bad_node_value(draw, document):
    node = _pick_node(draw, document)
    key, values = draw(st.sampled_from((
        ("id", ("", 7, None)),
        ("type", ("", 3)),
        ("position", ([1, 2], "here", {"x": 0}, {"x": "a", "y": 0})),
        ("parameters", ([], "params", 3)),
    )))
    node[key] = draw(st.sampled_from(values))


def _duplicate_node_id(draw, document):
    nodes = document["nodes"]
    nodes.append(copy.deepcopy(_pick_node(draw, document)))


def _drop_connection_key(draw, document):
    connection = _pick_connection(draw, document)
    del connection[draw(st.sampled_from(("id", "from", "to")))]


def _bad_connection_value(draw, document):
    connection = _pick_connection(draw, document)
    bad_endpoints = (
        "n1",
        5,
        {"node": "x"},  # missing "port"
        {"node": "", "port": "out"},  # empty node id
        {"node": "a", "port": "b", "extra": 1},  # additional property
    )
    key, values = draw(st.sampled_from((
        ("id", ("", 3)),
        ("from", bad_endpoints),
        ("to", bad_endpoints),
    )))
    connection[key] = draw(st.sampled_from(values))


def _unknown_node_reference(draw, document):
    connection = _pick_connection(draw, document)
    existing_ids = {node["id"] for node in document["nodes"]}
    missing = "__missing-node__"
    while missing in existing_ids:
        missing += "_"
    connection[draw(st.sampled_from(("from", "to")))]["node"] = missing


_DOCUMENT_MUTATIONS = (
    _DocumentMutation("drop_top_level_key", _always, _drop_top_level_key),
    _DocumentMutation("bad_schema_version", _always, _bad_schema_version),
    _DocumentMutation("wrong_top_level_type", _always, _wrong_top_level_type),
    _DocumentMutation("extra_top_level_property", _always, _extra_top_level_property),
    _DocumentMutation("drop_node_key", _has_nodes, _drop_node_key),
    _DocumentMutation("bad_node_value", _has_nodes, _bad_node_value),
    _DocumentMutation("duplicate_node_id", _has_nodes, _duplicate_node_id),
    _DocumentMutation("drop_connection_key", _has_connections, _drop_connection_key),
    _DocumentMutation("bad_connection_value", _has_connections, _bad_connection_value),
    _DocumentMutation("unknown_node_reference", _has_connections, _unknown_node_reference),
)


@st.composite
def corrupted_document_strategy(draw, graphs: Optional[st.SearchStrategy] = None):
    """Workflow_Definition JSON documents corrupted to violate the schema
    (or the parse-level structural rules: duplicate ids, dangling node
    references). Every produced document string is rejected by ``parse()``
    with a descriptive error and never yields a graph.
    """
    graph = draw(
        graphs if graphs is not None
        else st.one_of(graph_strategy(), single_node_graph_strategy())
    )
    document = copy.deepcopy(graph_to_document(graph))
    applicable = [m for m in _DOCUMENT_MUTATIONS if m.applicable(document)]
    mutation = draw(st.sampled_from(applicable))
    mutation.apply(draw, document)
    return json.dumps(document)
