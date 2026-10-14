"""jpegdec native-caps pin and feed-first compile order
(jpeg-folder-source-decode-ubuntu2204, tasks 1 and 2).

On Ubuntu 22.04 / GStreamer 1.20.3 a JPEG file chain whose downstream
offers an RGB-family format first makes ``jpegdec`` take libjpeg-turbo's
direct-output path, which fails ("Improper call to JPEG library in state
205"). The fix pins ``jpegdec`` with one ``capsfilter
caps=JPEGDEC_NATIVE_CAPS`` on x86_64, x86_64_nvidia and arm64_cpu (design
C1), and the compiler emits a mixed document's frame-feed segment first on
those arches, because both Frame_Feed consumers read the launch string's
FIRST ``caps=`` clause as the fed appsrc's caps (design C1b, D10).

Bug-condition cases (task 1; they FAIL on the unfixed tree):
- the constant, its arch tuple and the compiler's shared tuple (1a);
- Property 5, the constrained catalog chains (1b);
- the compiled ``folder_source -> capture`` source chain (1c);
- Property 9, mixed graphs compiled feed first (1c2) and the
  ``_feed_segment_first`` post-pass itself (1c3).

Preservation cases (task 2; they PASS before and after the fix):
- the unconstrained mappings and mixed compiles equal frozen goldens
  captured from the unfixed tree;
- feed-first and single-source graphs compile identically with and without
  the post-pass.

New names are looked up with ``getattr(module, NAME, None)``, so a missing
constant or function fails per case instead of breaking collection. The
goldens under ``goldens/jpegdec_native_caps/`` are frozen after task 2 and
never recaptured.
"""

import dataclasses
import importlib
import json
import os
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog import ARCHITECTURES, DEVICE_ARCHITECTURES, get_node_type
from workflow_core.catalog import nodes as catalog_nodes
from workflow_core.compiler import CompileContext, CompiledPipelineDocument, compile
from workflow_core.serializer import (
    Connection,
    Node,
    PortEndpoint,
    Position,
    WorkflowGraph,
)
from workflow_core.validator import FRAME_FEED_SOURCE_TYPES

compiler_module = importlib.import_module("workflow_core.compiler.compiler")

#: The exact design string (design C1, D2): five ';'-separated structures,
#: I420 first, no braces.
EXPECTED_CAPS = (
    "video/x-raw,format=I420;"
    "video/x-raw,format=Y42B;"
    "video/x-raw,format=Y444;"
    "video/x-raw,format=GRAY8;"
    "video/x-raw,format=RGB"
)
CONSTRAINED_ARCHS = ("x86_64", "x86_64_nvidia", "arm64_cpu")
UNCONSTRAINED_DEVICE_ARCHS = ("arm64_jp5", "arm64_jp6", "arm64_jp7")
FILE_SOURCE_TYPES = ("folder_source", "csi_camera_source")
FEED_SOURCE_PARAMETERS = {
    "aravis_camera_source": {"camera_id": "Aravis-Fake-GV01"},
    "custom_python_source": {
        "code": "def produce_frame(context):\n    return None\n"},
    "rtsp_camera_source": {"url": "rtsp://127.0.0.1:8554/dda"},
    "rtmp_stream_source": {"url": "rtmp://127.0.0.1:1935/live/dda"},
}
FILE_SOURCE_PARAMETERS = {
    "folder_source": {"location": "/data/images"},
    "csi_camera_source": {},
}
CONSTRAINED_CHAIN = ["filesrc", "emexifextract", "jpegparse", "jpegdec",
                     "capsfilter", "videoconvert", "videoflip"]
CONTEXT = CompileContext(workflow_id="wf-jpegdec", workflow_version="1")

_GOLDEN_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "goldens", "jpegdec_native_caps")


def _capture_or_assert_text(name, current_text):
    """Capture ``current_text`` as the golden ``name`` when it is absent (the
    first run, on the unfixed tree), else assert byte equality with it. The
    ``_cmake_preservation_support.capture_or_assert_text`` pattern."""
    path = os.path.join(_GOLDEN_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        recorded = fh.read()
    assert current_text == recorded, (
        "frozen golden '{0}' changed: the unconstrained output differs from "
        "the unfixed-tree capture".format(name))
    return recorded


def _golden_json(data):
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------
# Graph helpers
# --------------------------------------------------------------------------

_POS = Position(0.0, 0.0)


def _node(node_id, node_type, **parameters):
    return Node(id=node_id, type=node_type, position=_POS, parameters=parameters)


def _conn(conn_id, source_node, target_node):
    return Connection(id=conn_id, source=PortEndpoint(source_node, "out"),
                      target=PortEndpoint(target_node, "in"))


def _mixed_graph(file_type, feed_type, fan_in=False, feed_first=False):
    """A JPEG file source plus a frame-feed source. Each source feeds its own
    ``capture``, or, with ``fan_in``, both feed one ``capture`` behind a
    funnel. ``feed_first`` puts the feed node first in graph order."""
    sources = [
        _node("file", file_type, **FILE_SOURCE_PARAMETERS[file_type]),
        _node("feed", feed_type, **FEED_SOURCE_PARAMETERS[feed_type]),
    ]
    if feed_first:
        sources.reverse()
    if fan_in:
        sinks = [_node("cap", "capture", output_path="/out")]
        connections = [_conn("c1", "file", "cap"), _conn("c2", "feed", "cap")]
    else:
        sinks = [_node("cap_file", "capture", output_path="/out/file"),
                 _node("cap_feed", "capture", output_path="/out/feed")]
        connections = [_conn("c1", "file", "cap_file"),
                       _conn("c2", "feed", "cap_feed")]
    return WorkflowGraph(nodes=sources + sinks, connections=connections)


def _single_source_graph():
    """folder_source -> capture, the graph of test_compiler_compile.py."""
    return WorkflowGraph(
        nodes=[_node("src", "folder_source", location="/data/images"),
               _node("cap", "capture", output_path="/out")],
        connections=[_conn("c1", "src", "cap")],
    )


MIXED_GRAPH_IDS = [
    "{0}+{1}+{2}".format(file_type, feed_type, shape)
    for file_type in FILE_SOURCE_TYPES
    for feed_type in sorted(FEED_SOURCE_PARAMETERS)
    for shape in ("split", "fan_in")
]


def _graph_for(graph_id, feed_first=False):
    file_type, feed_type, shape = graph_id.split("+")
    return _mixed_graph(file_type, feed_type, fan_in=(shape == "fan_in"),
                        feed_first=feed_first)


def _compile_ok(graph, arch, simulation=False):
    result = compile(graph, arch, CONTEXT, simulation=simulation)
    assert isinstance(result, CompiledPipelineDocument), (
        "expected a document, got errors: {0}".format(result))
    return result


def _unreordered_compile(graph, arch, simulation=False):
    """The graph-order compile: ``compile()`` with the feed-first post-pass
    patched to identity when it exists (on the unfixed tree it doesn't)."""
    if getattr(compiler_module, "_feed_segment_first", None) is None:
        return _compile_ok(graph, arch, simulation)
    with patch.object(compiler_module, "_feed_segment_first",
                      lambda segments, nodes_by_id, target_arch: segments):
        return _compile_ok(graph, arch, simulation)


def _feed_segment_index(segments, node_id="feed"):
    matches = [index for index, segment in enumerate(segments)
               if segment["elements"]
               and segment["elements"][0]["factory"] == "appsrc"
               and segment["elements"][0]["nodeId"] == node_id]
    assert len(matches) == 1, segments
    return matches[0]


def _linking_names(segments):
    """Every tee/funnel ``name`` arg, per segment name."""
    return {
        segment["name"]: [element["args"].get("name")
                          for element in segment["elements"]
                          if element["factory"] in ("tee", "funnel")]
        for segment in segments
    }


# --------------------------------------------------------------------------
# 1(a) The constant and the arch tuple (design C1, D2, D5)
# --------------------------------------------------------------------------

class TestJpegdecNativeCapsConstant:
    def test_constant_is_the_exact_design_string(self):
        caps = getattr(catalog_nodes, "JPEGDEC_NATIVE_CAPS", None)
        assert caps == EXPECTED_CAPS
        structures = caps.split(";")
        assert len(structures) == 5
        assert structures[0] == "video/x-raw,format=I420"
        assert [s.split("format=")[1] for s in structures] == [
            "I420", "Y42B", "Y444", "GRAY8", "RGB"]
        assert "{" not in caps and "}" not in caps

    def test_constant_survives_args_template_resolution(self):
        caps = getattr(catalog_nodes, "JPEGDEC_NATIVE_CAPS", None)
        assert caps is not None
        assert compiler_module._resolve_value(caps, {}) == caps

    def test_arch_tuple_is_the_three_constrained_arches(self):
        archs = getattr(catalog_nodes, "JPEGDEC_NATIVE_CAPS_ARCHS", None)
        assert archs == CONSTRAINED_ARCHS

    def test_compiler_shares_the_catalog_arch_tuple(self):
        archs = getattr(catalog_nodes, "JPEGDEC_NATIVE_CAPS_ARCHS", None)
        assert archs is not None
        assert getattr(compiler_module, "JPEGDEC_NATIVE_CAPS_ARCHS", None) is archs


# --------------------------------------------------------------------------
# 1(b) Property 5: the constrained catalog chains
# --------------------------------------------------------------------------

class TestConstrainedCatalogChains:
    @pytest.mark.parametrize("arch", CONSTRAINED_ARCHS)
    @pytest.mark.parametrize("type_id", FILE_SOURCE_TYPES)
    def test_chain_pins_jpegdec_with_the_native_caps(self, type_id, arch):
        mapping = get_node_type(type_id).mapping_for(arch)
        assert [entry["factory"] for entry in mapping.element_chain] == (
            CONSTRAINED_CHAIN)
        assert mapping.element_chain[4]["args_template"] == {"caps": EXPECTED_CAPS}


# --------------------------------------------------------------------------
# 1(c) The compiled folder_source -> capture source chain
# --------------------------------------------------------------------------

class TestCompiledSourceChain:
    def test_capsfilter_follows_jpegdec_with_the_source_node_id(self):
        document = _compile_ok(_single_source_graph(), "x86_64")
        source = [element for segment in document.segments
                  for element in segment["elements"] if element["nodeId"] == "src"]
        assert [element["factory"] for element in source] == CONSTRAINED_CHAIN
        assert source[4] == {"nodeId": "src", "factory": "capsfilter",
                             "args": {"caps": EXPECTED_CAPS}}


# --------------------------------------------------------------------------
# 1(c2) Property 9: mixed graphs compile feed first on constrained arches
# --------------------------------------------------------------------------

class TestFeedSegmentFirstCompile:
    @pytest.mark.parametrize("arch", CONSTRAINED_ARCHS)
    @pytest.mark.parametrize("graph_id", MIXED_GRAPH_IDS)
    def test_feed_segment_moves_to_index_zero(self, graph_id, arch):
        graph = _graph_for(graph_id)
        document = _compile_ok(graph, arch).to_dict()
        base = _unreordered_compile(graph, arch).to_dict()

        index = _feed_segment_index(base["segments"])
        assert index > 0, "the graph-order compile must put the file source first"
        segments = document["segments"]
        assert segments[0]["elements"][0]["factory"] == "appsrc"
        assert segments[0]["elements"][0]["nodeId"] == "feed"
        # The other segments keep the graph-order compile's relative order;
        # names, from, linkTo and every element arg are unchanged.
        expected = ([base["segments"][index]] + base["segments"][:index]
                    + base["segments"][index + 1:])
        assert segments == expected
        assert ({(s["name"], s["from"], s["linkTo"]) for s in segments}
                == {(s["name"], s["from"], s["linkTo"]) for s in base["segments"]})
        assert _linking_names(segments) == _linking_names(base["segments"])
        # Nothing else in the document changes.
        assert ({key: value for key, value in document.items() if key != "segments"}
                == {key: value for key, value in base.items() if key != "segments"})


# --------------------------------------------------------------------------
# 1(c3) Property 9: the compile-side post-pass over generated segment lists
# --------------------------------------------------------------------------

_FEED_TYPES = sorted(FRAME_FEED_SOURCE_TYPES)
_SEGMENT_KINDS = (
    "feed",              # a frame-feed source's appsrc-headed root segment
    "feed_branch",       # the same head under a tee ("from"): never moves
    "file",              # a JPEG file chain
    "branch",            # a queue-headed tee branch
    "funnel",            # a funnel-headed fan-in segment
    "non_feed_appsrc",   # an appsrc head of a non-feed node (digital_input)
    "unknown_node",      # an appsrc head whose node is not in the graph
    "empty",             # no elements
)


def _segment_of(kind, index, feed_type):
    node_id = "n{0}".format(index)
    name = "s{0}".format(index)
    if kind in ("feed", "feed_branch", "non_feed_appsrc", "unknown_node"):
        elements = [
            {"nodeId": node_id, "factory": "appsrc",
             "args": {"name": "appsrc_{0}".format(node_id)}},
            {"nodeId": node_id, "factory": "videoconvert", "args": {}},
        ]
        from_ref = "t0" if kind == "feed_branch" else None
        node = None
        if kind in ("feed", "feed_branch"):
            node = _node(node_id, feed_type)
        elif kind == "non_feed_appsrc":
            node = _node(node_id, "digital_input")
        return {"name": name, "from": from_ref, "linkTo": None,
                "elements": elements}, node
    if kind == "file":
        elements = [
            {"nodeId": node_id, "factory": factory, "args": {}}
            for factory in ("filesrc", "emexifextract", "jpegparse", "jpegdec",
                            "videoconvert", "videoflip")
        ]
        return {"name": name, "from": None, "linkTo": None,
                "elements": elements}, _node(node_id, "folder_source")
    if kind == "branch":
        return {"name": name, "from": "t0", "linkTo": None,
                "elements": [{"nodeId": None, "factory": "queue", "args": {}}]}, None
    if kind == "funnel":
        return {"name": name, "from": None, "linkTo": None,
                "elements": [{"nodeId": None, "factory": "funnel",
                              "args": {"name": "f0"}}]}, None
    return {"name": name, "from": None, "linkTo": "f0", "elements": []}, None


@st.composite
def _segment_lists(draw):
    kinds = draw(st.lists(st.sampled_from(_SEGMENT_KINDS), max_size=8))
    if draw(st.booleans()):
        # Some lists carry a second frame-feed segment (unreachable under
        # V7) to pin the stop-at-first rule.
        kinds.insert(draw(st.integers(0, len(kinds))), "feed")
        kinds.insert(draw(st.integers(0, len(kinds))), "feed")
    segments, nodes_by_id = [], {}
    for index, kind in enumerate(kinds):
        segment, node = _segment_of(kind, index, draw(st.sampled_from(_FEED_TYPES)))
        segments.append(segment)
        if node is not None:
            nodes_by_id[node.id] = node
    arch = draw(st.sampled_from(ARCHITECTURES + ("unknown",)))
    return kinds, segments, nodes_by_id, arch


@settings(max_examples=300, deadline=None)
@given(case=_segment_lists())
def test_feed_segment_first_property(case):
    """Property 9 (compile side): on a constrained arch the FIRST frame-feed
    segment ends at index 0 and every other segment keeps its relative order
    and identity; when it is already at index 0, absent, or the arch isn't
    constrained, the input list object itself is returned."""
    function = getattr(compiler_module, "_feed_segment_first", None)
    assert function is not None, "compiler._feed_segment_first is missing"
    kinds, segments, nodes_by_id, arch = case
    before = list(segments)

    result = function(segments, nodes_by_id, arch)

    assert [id(s) for s in segments] == [id(s) for s in before], (
        "the input list must not be mutated")
    first_feed = next((i for i, kind in enumerate(kinds) if kind == "feed"), None)
    if arch not in CONSTRAINED_ARCHS or first_feed is None or first_feed == 0:
        assert result is segments
        return
    assert result[0] is segments[first_feed]
    assert [id(s) for s in result[1:]] == [
        id(s) for i, s in enumerate(segments) if i != first_feed]


# --------------------------------------------------------------------------
# Task 2: preservation (frozen goldens from the unfixed tree)
# --------------------------------------------------------------------------

class TestUnconstrainedPreservation:
    def _baseline(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "catalog_baseline.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_unconstrained_mappings_equal_the_frozen_golden(self):
        live = {
            type_id: {
                arch: dataclasses.asdict(get_node_type(type_id).mapping_for(arch))
                for arch in UNCONSTRAINED_DEVICE_ARCHS + ("sim",)
            }
            for type_id in FILE_SOURCE_TYPES
        }
        _capture_or_assert_text("unconstrained_mappings.json", _golden_json(live))

    def test_unconstrained_mappings_equal_the_catalog_baseline(self):
        baseline = self._baseline()
        for type_id in FILE_SOURCE_TYPES:
            recorded = {mapping["arch"]: mapping
                        for mapping in baseline[type_id]["mappings"]}
            for arch in UNCONSTRAINED_DEVICE_ARCHS + ("sim",):
                live = dataclasses.asdict(get_node_type(type_id).mapping_for(arch))
                assert live == recorded[arch], (type_id, arch)

    def test_unconstrained_mixed_compiles_equal_the_frozen_golden(self):
        compiles = {}
        for graph_id in MIXED_GRAPH_IDS:
            graph = _graph_for(graph_id)
            per_arch = {
                arch: _compile_ok(graph, arch).to_dict()
                for arch in UNCONSTRAINED_DEVICE_ARCHS
            }
            per_arch["x86_64+simulation"] = _compile_ok(
                graph, "x86_64", simulation=True).to_dict()
            compiles[graph_id] = per_arch
        _capture_or_assert_text("unconstrained_mixed_compiles.json",
                                _golden_json(compiles))

    @pytest.mark.parametrize("simulation", [False, True])
    @pytest.mark.parametrize("arch", DEVICE_ARCHITECTURES)
    @pytest.mark.parametrize("graph_id", MIXED_GRAPH_IDS + ["single_source"])
    def test_feed_first_and_single_source_compiles_ignore_the_post_pass(
            self, graph_id, arch, simulation):
        graph = (_single_source_graph() if graph_id == "single_source"
                 else _graph_for(graph_id, feed_first=True))
        document = _compile_ok(graph, arch, simulation).to_dict()
        assert document == _unreordered_compile(graph, arch, simulation).to_dict()
