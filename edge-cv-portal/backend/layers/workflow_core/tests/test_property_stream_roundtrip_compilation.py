"""Property test for the stream sources' generic catalog paths (task 2.4).

**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

*For any* valid workflow definition containing Stream_Camera_Source_Nodes:

- Serializing then parsing the definition SHALL produce an equivalent
  graph.
- Compiling it for any device architecture SHALL render each stream node as
  its ``appsrc name=appsrc_<nodeId> ! videoconvert`` chain, with no
  parameter in any element argument.

**Validates: Requirements 1.4, 1.5**

What "valid definition" means here. A Stream_Camera_Source_Node is a
Frame_Feed_Source_Node, and the device runtime serves exactly one frame feed
per workflow, so a valid definition carries exactly **one** stream feed
(Requirement 2.4 — the V7 coexistence rule that reports every member of a
multi-feed document is task 3.1's, and its own Property 4 covers the
multi-feed finding set). Every compile-side property below therefore runs on
single-feed documents. The serializer is validation-independent, so the
round-trip clause is additionally exercised on 2..3-feed documents, which
keeps this file's assertions stable once task 3.1 makes such documents
V7-invalid.

Both spellings of a feed are generated (``generators.stream_graph_strategy``):
the ``rtsp_camera_source`` / ``rtmp_stream_source`` node types directly, and
the unified ``Input Source`` node carrying the ``rtsp_camera`` /
``rtmp_stream`` source kind (Requirement 1.6). The unified spelling is
additionally pinned to compile identically to its underlying source type,
which is what "processed through the generic descriptor-driven paths" means
for the unified expansion.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import (
    ARCH_SIM,
    DEVICE_ARCHITECTURES,
    bundled_plugins_for,
    get_node_type,
)
from workflow_core.compiler import CompiledPipelineDocument, compile
from workflow_core.serializer import Node, WorkflowGraph, parse, serialize
from workflow_core.stream_url import SCHEMES_BY_NODE_TYPE, check_stream_url
from workflow_core.validator import SEVERITY_ERROR, check_parameter_value, validate

from .generators import STREAM_SOURCE_TYPES, stream_graph_strategy

#: The element chain every physical device architecture renders for a
#: Stream_Camera_Source_Node (Requirement 1.4).
_STREAM_FACTORIES = ("appsrc", "videoconvert")

#: The plugins the stream mapping declares; both are LocalServer-bundled, so
#: a compiled document must not list either as a dependency.
_STREAM_PLUGIN_DEPENDENCIES = ("app", "videoconvertscale")

#: The dataset-fed simulation stub's chain (Requirement 1.4, sim clause).
_SIM_FACTORIES = ("multifilesrc", "jpegparse", "jpegdec", "videoconvert")

_EXAMPLES = settings(max_examples=100)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _elements_of(document: CompiledPipelineDocument, node_id: str):
    """The ordered elements the document renders for ``node_id``."""
    return [
        element
        for segment in document.segments
        for element in segment["elements"]
        if element["nodeId"] == node_id
    ]


def _all_elements(document: CompiledPipelineDocument):
    return [
        element
        for segment in document.segments
        for element in segment["elements"]
    ]


def _effective_parameters(feed):
    """``feed``'s parameters with the source descriptor's defaults applied.

    The effective set is what the compiler would have to render if the
    mapping had any binding slots, so it is what Requirement 1.4's "no
    parameter shall appear in any element argument" is checked against.
    """
    descriptor = get_node_type(feed.source_type)
    effective = {}
    for parameter in descriptor.parameters:
        if parameter.name in feed.parameters:
            effective[parameter.name] = feed.parameters[parameter.name]
        else:
            effective[parameter.name] = parameter.default
    return effective


def _direct_spelling(stream_graph, feed):
    """``stream_graph``'s graph with ``feed`` written as its source type.

    The unified node's union parameters are narrowed to the ones the
    underlying source descriptor declares — exactly what
    ``expand_unified_inputs`` keeps — so the two documents differ only in
    how the feed is spelled.
    """
    applicable = {p.name for p in get_node_type(feed.source_type).parameters}
    nodes = []
    for node in stream_graph.graph.nodes:
        if node.id != feed.node_id:
            nodes.append(node)
            continue
        nodes.append(Node(
            id=node.id,
            type=feed.source_type,
            position=node.position,
            parameters={name: value for name, value in node.parameters.items()
                        if name in applicable},
            data=node.data,
        ))
    return WorkflowGraph(nodes=nodes, connections=list(stream_graph.graph.connections))


# ---------------------------------------------------------------------------
# The generated corpus really is a corpus of *valid* definitions
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(stream_graph=stream_graph_strategy())
def test_generated_single_feed_stream_definitions_are_valid(stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    The property's premise: a single-feed stream document is a *valid*
    workflow definition — ``validate()`` reports no error-severity finding
    through the generic descriptor-driven checks, and every generated
    Stream_URL is credential-free and carries a scheme its node type accepts
    (Requirements 1.3, 1.5).
    """
    errors = [finding for finding in validate(stream_graph.graph)
              if finding.severity == SEVERITY_ERROR]
    assert not errors, (
        "validate reported errors for a valid stream definition: {0}".format(errors))

    url_descriptor = next(
        parameter for parameter in get_node_type(STREAM_SOURCE_TYPES[0]).parameters
        if parameter.name == "url"
    )
    for feed in stream_graph.feeds:
        url = feed.parameters["url"]
        schemes = SCHEMES_BY_NODE_TYPE[feed.source_type]
        assert check_stream_url(url, schemes) is None, (
            "generated url {0!r} is not a valid Stream_URL for "
            "'{1}'".format(url, feed.source_type))
        assert check_parameter_value(url_descriptor, url) is None, (
            "generated url {0!r} violates the catalog constraint".format(url))


# ---------------------------------------------------------------------------
# Clause 1: serializing then parsing produces an equivalent graph
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(stream_graph=stream_graph_strategy())
def test_single_feed_stream_definitions_round_trip(stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    **Validates: Requirements 1.5**
    """
    _assert_round_trips(stream_graph)


@_EXAMPLES
@given(stream_graph=st.integers(min_value=2, max_value=3).flatmap(
    lambda count: stream_graph_strategy(stream_nodes=count)))
def test_multi_feed_stream_definitions_round_trip(stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    The serializer is a generic, descriptor-driven, validation-independent
    path, so the round-trip clause holds for documents carrying several
    stream nodes too — including the multi-feed documents the frame-feed
    coexistence rule rejects (Requirement 2.4, task 3.1/Property 4). Only
    the round trip is asserted here; nothing about validation or
    compilation.

    **Validates: Requirements 1.5**
    """
    assert len(stream_graph.feeds) >= 2
    _assert_round_trips(stream_graph)


def _assert_round_trips(stream_graph):
    graph = stream_graph.graph
    document = serialize(graph)

    result = parse(document)
    assert result.ok, (
        "parse rejected a serialized stream definition: {0}".format(result.error))
    assert result.graph is not None
    assert result.graph.is_equivalent_to(graph), (
        "parsed graph is not equivalent to the original")
    assert graph.is_equivalent_to(result.graph), (
        "original graph is not equivalent to the parsed graph")
    assert serialize(result.graph) == document, (
        "re-serialized document is not byte-identical to the original")

    # Every stream node survives with its type and its parameters intact —
    # the URL in particular, byte for byte.
    parsed_by_id = {node.id: node for node in result.graph.nodes}
    for feed in stream_graph.feeds:
        parsed = parsed_by_id.get(feed.node_id)
        assert parsed is not None, (
            "stream node '{0}' did not survive the round trip".format(feed.node_id))
        assert parsed.type == feed.node_type
        assert parsed.parameters == feed.parameters, (
            "stream node '{0}' parameters changed across the round "
            "trip".format(feed.node_id))
        assert parsed.parameters["url"] == feed.parameters["url"]


# ---------------------------------------------------------------------------
# Clause 2: compilation renders the appsrc chain on every device arch
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(stream_graph=stream_graph_strategy())
def test_stream_nodes_compile_to_the_appsrc_chain_on_every_device_architecture(
        stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    **Validates: Requirements 1.4, 1.5**
    """
    for arch in DEVICE_ARCHITECTURES:
        compiled = compile(stream_graph.graph, arch)
        assert isinstance(compiled, CompiledPipelineDocument), (
            "compile failed on '{0}': {1}".format(arch, compiled))

        for feed in stream_graph.feeds:
            expected_name = "appsrc_{0}".format(feed.node_id)
            elements = _elements_of(compiled, feed.node_id)
            assert elements == [
                {"nodeId": feed.node_id, "factory": "appsrc",
                 "args": {"name": expected_name}},
                {"nodeId": feed.node_id, "factory": "videoconvert", "args": {}},
            ], (
                "stream node '{0}' ({1}) rendered {2} on '{3}'".format(
                    feed.node_id, feed.node_type, elements, arch))

            # The {nodeId} token resolves to this node and nothing else
            # claims the resulting element name.
            other_appsrcs = [element for element in _all_elements(compiled)
                             if element["factory"] == "appsrc"
                             and element["nodeId"] != feed.node_id]
            assert all(element["args"].get("name") != expected_name
                       for element in other_appsrcs), (
                "another appsrc claims {0!r} on '{1}'".format(expected_name, arch))

        # Requirement 1.4's dependency clause: the mapping declares exactly
        # ``app`` and ``videoconvertscale``, both LocalServer-bundled, so the
        # stream node contributes no dependency to the compiled document.
        bundled = bundled_plugins_for(arch)
        for feed in stream_graph.feeds:
            mapping = get_node_type(feed.source_type).mapping_for(arch)
            declared = set(mapping.plugin_dependencies)
            assert declared == set(_STREAM_PLUGIN_DEPENDENCIES), (
                "'{0}' declares {1} on '{2}'".format(
                    feed.source_type, sorted(declared), arch))
            assert declared <= bundled, (
                "'{0}' declares plugins that are not bundled on '{1}': "
                "{2}".format(feed.source_type, arch, sorted(declared - bundled)))
            assert not declared & set(compiled.plugin_dependencies), (
                "compiled document on '{0}' lists bundled plugins "
                "{1}".format(arch, sorted(declared & set(compiled.plugin_dependencies))))


@_EXAMPLES
@given(stream_graph=stream_graph_strategy(),
       arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_no_stream_parameter_appears_in_any_element_argument(stream_graph, arch):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    Requirement 1.4's last sentence: the stream mapping has no binding
    slots, so no stream parameter — the Stream_URL above all — reaches any
    element argument, on the device chain or in simulation.

    **Validates: Requirements 1.4**
    """
    for simulation in (False, True):
        compiled = compile(stream_graph.graph, arch, simulation=simulation)
        assert isinstance(compiled, CompiledPipelineDocument), (
            "compile failed on '{0}' (simulation={1}): {2}".format(
                arch, simulation, compiled))

        rendered_args = json.dumps(
            [element["args"] for element in _all_elements(compiled)],
            ensure_ascii=False, sort_keys=True)

        for feed in stream_graph.feeds:
            # The feed's own chain carries exactly one argument, the
            # {nodeId}-derived element name (or the sim stub's dataset
            # placeholder) — never a parameter.
            for element in _elements_of(compiled, feed.node_id):
                for name, value in element["args"].items():
                    assert (name, value) in (
                        ("name", "appsrc_{0}".format(feed.node_id)),
                        ("location", "{dataset_location}"),
                        ("idct-method", 2),
                    ), (
                        "stream node '{0}' element '{1}' carries argument "
                        "{2}={3!r} on '{4}'".format(
                            feed.node_id, element["factory"], name, value, arch))

            # Document-wide: the URL never appears in any element argument.
            url = feed.parameters["url"]
            assert url not in rendered_args, (
                "the Stream_URL of '{0}' appears in an element argument on "
                "'{1}'".format(feed.node_id, arch))
            for name, value in _effective_parameters(feed).items():
                if isinstance(value, str) and len(value) >= 8:
                    assert value not in rendered_args, (
                        "parameter '{0}' of '{1}' appears in an element "
                        "argument on '{2}'".format(name, feed.node_id, arch))


@_EXAMPLES
@given(stream_graph=stream_graph_strategy(),
       arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_stream_nodes_compile_to_the_dataset_fed_stub_in_simulation(
        stream_graph, arch):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    The other half of Requirement 1.4's mapping table: on the ``sim``
    architecture, and for any device architecture compiled in simulation
    mode, a stream node resolves the shared dataset-fed simulation stub
    instead of the appsrc chain.

    **Validates: Requirements 1.4**
    """
    documents = [
        compile(stream_graph.graph, ARCH_SIM),
        compile(stream_graph.graph, arch, simulation=True),
    ]
    for compiled in documents:
        assert isinstance(compiled, CompiledPipelineDocument), (
            "simulation compile failed: {0}".format(compiled))
        for feed in stream_graph.feeds:
            factories = tuple(element["factory"]
                              for element in _elements_of(compiled, feed.node_id))
            assert factories == _SIM_FACTORIES, (
                "stream node '{0}' rendered {1} in simulation".format(
                    feed.node_id, factories))


# ---------------------------------------------------------------------------
# The unified spelling goes through the same generic paths
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(stream_graph=stream_graph_strategy(unified=True))
def test_unified_stream_kinds_compile_identically_to_their_source_type(
        stream_graph):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    Requirement 1.6: the ``rtsp_camera`` / ``rtmp_stream`` source kinds
    expand to ``rtsp_camera_source`` / ``rtmp_stream_source``. Compiling the
    unified spelling therefore yields the very same document as writing the
    underlying source type by hand with the same id and parameters — the
    unified type never reaches mapping resolution.

    **Validates: Requirements 1.5, 1.6**
    """
    feed = stream_graph.feeds[0]
    assert feed.is_unified, "strategy must produce the unified spelling"

    direct = _direct_spelling(stream_graph, feed)
    direct_errors = [finding for finding in validate(direct)
                     if finding.severity == SEVERITY_ERROR]
    assert not direct_errors, (
        "the direct spelling is not valid: {0}".format(direct_errors))

    for arch in DEVICE_ARCHITECTURES:
        unified_document = compile(stream_graph.graph, arch)
        direct_document = compile(direct, arch)
        assert isinstance(unified_document, CompiledPipelineDocument), (
            "unified compile failed on '{0}': {1}".format(arch, unified_document))
        assert isinstance(direct_document, CompiledPipelineDocument), (
            "direct compile failed on '{0}': {1}".format(arch, direct_document))
        assert unified_document.to_dict() == direct_document.to_dict(), (
            "the unified spelling of '{0}' compiled differently from "
            "'{1}' on '{2}'".format(feed.node_id, feed.source_type, arch))


# ---------------------------------------------------------------------------
# The two clauses agree: a round-tripped definition compiles identically
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(stream_graph=stream_graph_strategy(),
       arch=st.sampled_from(DEVICE_ARCHITECTURES))
def test_round_tripped_stream_definitions_compile_identically(stream_graph, arch):
    """**Feature: rtsp-rtmp-stream-cameras, Property 1: Stream node definitions round-trip and compile through generic catalog paths**

    "Equivalent graph" in the operational sense: the parsed definition is
    interchangeable with the original everywhere downstream, so it validates
    to the same findings and compiles to the same document.

    **Validates: Requirements 1.4, 1.5**
    """
    result = parse(serialize(stream_graph.graph))
    assert result.ok and result.graph is not None

    assert [f.to_dict() for f in validate(result.graph)] == \
        [f.to_dict() for f in validate(stream_graph.graph)], (
            "the round-tripped definition validates differently")

    original = compile(stream_graph.graph, arch)
    reparsed = compile(result.graph, arch)
    assert isinstance(original, CompiledPipelineDocument), original
    assert isinstance(reparsed, CompiledPipelineDocument), reparsed
    assert reparsed.to_dict() == original.to_dict(), (
        "the round-tripped definition compiled differently on "
        "'{0}'".format(arch))
