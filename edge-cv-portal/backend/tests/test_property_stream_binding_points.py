"""
Property-based test for stream binding points and stream-free packaging
identity (task 6.3).

**Feature: rtsp-rtmp-stream-cameras, Property 9: Stream binding points and stream-free packaging identity**

*For any* definition:

- If it has stream nodes, every architecture's document SHALL carry one
  binding point per stream node. Each point SHALL have
  ``streamBinding: true``, the node's protocol, empty slots, and the
  node's rendered parameters.
- If it has no stream nodes, ``compiled_document_json`` SHALL be
  byte-equal to the pre-feature output.

**Validates: Requirements 9.1, 9.2**

The layer under test is the packager's pure binding-point pipeline —
``gather_camera_input_nodes`` -> ``binding_hints_from_definition`` ->
``build_binding_points`` -> ``compiled_document_json`` ->
``camera_input_nodes_record`` — driven over compiler output exactly as
the handler drives it (same call sequence, same built-in catalog), with
no AWS. The module is imported through the shared moto-backed session
fixture only so its module-level boto3 clients bind to the mock (the
re-import pattern of test_property_aravis_binding_points.py).
Requirement 9.1's second clause, ``has_binding_points: true``, is
asserted through the value the handler stores: ``bool(camera_nodes)``.

Reference models. The expected protocol (``rtsp``/``rtmp``) and the
expected rendered parameter values (the six declared stream parameters
with their catalog defaults overlaid by the node's explicit values) are
re-spelled here from the design's descriptor and packager contracts
rather than imported from ``workflow_packaging`` or the catalog, so a
change to either side is a test failure rather than a silent agreement.

Pre-feature oracle (clause 2). The only packaging behaviour this feature
changed is gated on the two new stream type ids — their membership in
``gather_camera_input_nodes`` and the ``streamBinding`` branch of
``build_binding_points`` — plus the feature floor, which is not part of
any document (Requirement 18.1's floor clause is task 6.4's). The
pre-feature output over a stream-free definition is therefore
reconstructed by running the same pure pipeline with the pre-feature
gather rule and asserting byte equality, the way
test_property_aravis_free_packaging_identity.py reconstructs its own.
The structure is pinned in addition: a cameraless document IS the
compiler's own serialization, a camera document is that serialization
plus only the pre-existing ``bindingPoints`` section, and no stream key
appears anywhere in any produced document or version-item record.

Corpus scope, and why one stream node per definition. A
Stream_Camera_Source_Node is a Frame_Feed_Source_Node, and the device
runtime serves exactly one frame feed per workflow, so the validator's
V7 coexistence rule (Requirement 2.4) makes ``compile()`` refuse a
document carrying two or more frame-feed sources. Every property that
compiles therefore runs on single-stream definitions. The cardinality
clause ("one binding point per stream node") is additionally exercised
on 2..3-stream documents, which the serializer still parses, through
``build_binding_points`` alone — sound because the stream branch reads
nothing from the compiled document, which
``test_stream_binding_points_do_not_depend_on_the_compiled_document``
pins against a real one.

Stream nodes are generated in their direct spelling
(``rtsp_camera_source`` / ``rtmp_stream_source``), not as a unified
``Input Source`` node carrying ``source_kind: rtsp_camera``:
``gather_camera_input_nodes`` keys on ``node.type`` and the compiler's
expansion pre-pass is what rewrites the unified spelling, so a unified
stream node is not gathered — the pre-existing behaviour for
``source_kind: aravis_camera``, deliberately preserved by task 6.1 and
out of scope for this property.
"""
import json
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.serializer import parse
from workflow_core.compiler import compile as compile_workflow, CompileContext
from workflow_core.catalog import DEVICE_ARCHITECTURES
from workflow_core.catalog.custom import resolve_catalog
from workflow_core.stream_url import check_stream_url
from workflow_core.validator import SEVERITY_ERROR, validate


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


#: 100 examples per property: the shared portal profile caps at 25, and
#: this feature's property tests carry the larger budget explicitly.
_EXAMPLES = settings(max_examples=100, deadline=None)


# ---------------------------------------------------------------------------
# Reference models, restated from the design rather than imported
# ---------------------------------------------------------------------------

#: The two Stream_Camera_Source_Node types and the ``streamProtocol``
#: discriminator Requirement 9.1 demands for each (design component 5's
#: STREAM_SOURCE_PROTOCOLS, re-spelled).
EXPECTED_STREAM_PROTOCOLS = {
    "rtsp_camera_source": "rtsp",
    "rtmp_stream_source": "rtmp",
}

#: The schemes each stream type's Stream_URL may carry (Requirement 2.1),
#: re-spelled so the generated corpus does not inherit the module's map.
STREAM_SCHEMES = {
    "rtsp_camera_source": ("rtsp", "rtsps"),
    "rtmp_stream_source": ("rtmp", "rtmps"),
}

#: Default port per scheme, for a URL that spells its scheme's default.
DEFAULT_PORT = {"rtsp": 554, "rtsps": 322, "rtmp": 1935, "rtmps": 443}

#: The declared defaults of the five optional stream parameters (design
#: component 2). ``url`` is required and has no default.
STREAM_PARAMETER_DEFAULTS = {
    "processing_mode": "continuous",
    "frames_per_second": 1.0,
    "max_frame_age_ms": 2000,
    "keep_recent_runs": 20,
    "keep_notable_runs": 200,
}

#: Binding-point keys a stream entry carries (``bindingHint`` only when the
#: definition records one).
STREAM_POINT_KEYS = {"nodeId", "nodeType", "parameters", "slots",
                     "streamBinding", "streamProtocol"}

#: The other camera families' markers: a stream point carries none of them.
FOREIGN_BINDING_MARKERS = ("aravisBinding", "csiSensorBinding",
                           "pythonSourceBinding", "adapterBinding")

#: Binding-point keys the pre-feature packager could produce: never a
#: stream key (camera-registry-sync, aravis-camera-input,
#: custom-python-source).
PRE_FEATURE_POINT_KEYS = {"nodeId", "nodeType", "parameters", "slots",
                          "bindingHint", "adapterBinding", "csiSensorBinding",
                          "aravisBinding", "pythonSourceBinding"}

#: The keys this feature adds to a binding point.
STREAM_KEYS = ("streamBinding", "streamProtocol")


def expected_rendered_parameters(node_parameters):
    """The rendered parameter values Requirement 9.1 demands: the declared
    stream defaults overlaid with the node's explicit values (the
    compiler's effective-value rule), with the required ``url``."""
    rendered = dict(STREAM_PARAMETER_DEFAULTS)
    rendered["url"] = node_parameters["url"]
    rendered.update(node_parameters)
    return rendered


def assert_no_stream_key(value, path="$"):
    """No stream key anywhere in a document or record tree."""
    if isinstance(value, dict):
        for key in STREAM_KEYS:
            assert key not in value, (
                "stream key {0!r} appeared at {1} of a stream-free "
                "document".format(key, path))
        for name, child in value.items():
            assert_no_stream_key(child, "{0}.{1}".format(path, name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_stream_key(child, "{0}[{1}]".format(path, index))


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Hosts a Stream_URL may carry: DNS names, IPv4, the bracketed IPv6 form
#: and unicode names. None contains whitespace, '@', '/', '?' or '#'.
_HOSTS = ("cam.local", "camera-7", "192.168.1.64", "media.example.com",
          "[2001:db8::64]", "[::1]", "caméra.local", "カメラ.local")

#: Paths, each starting with '/', free of whitespace and '#'.
_PATHS = ("", "/", "/Streaming/Channels/101", "/live/line1",
          "/axis-media/media.amp", "/поток/1", "/deep" * 12)

#: Query strings carrying no Secret_Query_Parameter name — a Stream_URL is
#: credential-free (Requirement 2.2).
_QUERIES = ("", "transport=tcp", "latency=200&profile=main", "codec=h265")

_hint_text = st.text(min_size=1, max_size=24)

_hints = st.fixed_dictionaries(
    {"cameraSourceId": _hint_text},
    optional={"cameraName": _hint_text, "sourceDeviceId": _hint_text},
)

_device_paths = st.integers(min_value=0, max_value=63).map(
    lambda n: "/dev/video{}".format(n))


@st.composite
def _stream_urls(draw, node_type):
    """A valid, credential-free Stream_URL for ``node_type``: one of its
    two schemes, a DNS / IPv4 / bracketed-IPv6 / unicode host, an absent,
    default or non-default port, an absent, root, deep or unicode path,
    and a non-secret query."""
    scheme = draw(st.sampled_from(STREAM_SCHEMES[node_type]))
    host = draw(st.sampled_from(_HOSTS))
    port = draw(st.one_of(st.none(), st.just(DEFAULT_PORT[scheme]),
                          st.integers(min_value=1, max_value=65535)))
    path = draw(st.sampled_from(_PATHS))
    query = draw(st.sampled_from(_QUERIES))
    return "{0}://{1}{2}{3}{4}".format(
        scheme, host,
        "" if port is None else ":{0}".format(port),
        path,
        "" if not query else "?" + query)


@st.composite
def _stream_parameters(draw, node_type):
    """A stream node's explicit parameters: the required ``url`` plus any
    subset of the five optional parameters, each inside its declared
    constraint range."""
    parameters = {"url": draw(_stream_urls(node_type))}
    if draw(st.booleans()):
        parameters["processing_mode"] = draw(
            st.sampled_from(("continuous", "on_trigger")))
    if draw(st.booleans()):
        parameters["frames_per_second"] = draw(st.floats(
            min_value=0.05, max_value=10.0,
            allow_nan=False, allow_infinity=False))
    if draw(st.booleans()):
        parameters["max_frame_age_ms"] = draw(
            st.integers(min_value=100, max_value=60000))
    if draw(st.booleans()):
        parameters["keep_recent_runs"] = draw(
            st.integers(min_value=1, max_value=200))
    if draw(st.booleans()):
        parameters["keep_notable_runs"] = draw(
            st.integers(min_value=0, max_value=5000))
    return parameters


def _chain(source, index):
    """``source`` wired to its own capture node."""
    capture = {"id": "cap{}".format(index), "type": "capture",
               "position": {"x": 100.0 * index, "y": 200.0},
               "parameters": {"output_path": "/out/{}".format(index)}}
    connection = {"id": "c{}".format(index),
                  "from": {"node": source["id"], "port": "out"},
                  "to": {"node": capture["id"], "port": "in"}}
    return [source, capture], connection


@st.composite
def _stream_definitions(draw):
    """A valid definition of 1..3 source->capture chains, exactly one
    headed by a Stream_Camera_Source_Node and the rest (mixed) by
    icam_source or folder_source chains.

    Exactly one stream chain: the validator's V7 coexistence rule
    (Requirement 2.4) makes ``compile()`` refuse a document with more than
    one Frame_Feed_Source_Node, mirroring the device runtime's
    single-frame appsrc feed.

    Returns ``(definition, stream_specs, camera_node_ids)`` where
    ``stream_specs`` maps stream node id -> (node type, parameters,
    hint-or-None) and ``camera_node_ids`` is the full ordered list of
    Camera_Input_Node ids.
    """
    chain_count = draw(st.integers(min_value=1, max_value=3))
    stream_index = draw(st.integers(min_value=0, max_value=chain_count - 1))

    nodes, connections = [], []
    stream_specs, camera_node_ids = {}, []
    for index in range(chain_count):
        if index == stream_index:
            node_type = draw(st.sampled_from(sorted(EXPECTED_STREAM_PROTOCOLS)))
            parameters = draw(_stream_parameters(node_type))
            source = {"id": "str{}".format(index), "type": node_type,
                      "position": {"x": 100.0 * index, "y": 0.0},
                      "parameters": parameters}
            hint = draw(st.one_of(st.none(), _hints))
            if hint is not None:
                source["data"] = {"cameraBindingHint": hint}
            stream_specs[source["id"]] = (node_type, parameters, hint)
            camera_node_ids.append(source["id"])
        elif draw(st.booleans()):
            source = {"id": "cam{}".format(index), "type": "icam_source",
                      "position": {"x": 100.0 * index, "y": 0.0},
                      "parameters": {"device": draw(_device_paths)}}
            if draw(st.booleans()):
                source["data"] = {"cameraBindingHint": draw(_hints)}
            camera_node_ids.append(source["id"])
        else:
            source = {"id": "src{}".format(index), "type": "folder_source",
                      "position": {"x": 100.0 * index, "y": 0.0},
                      "parameters": {"location": "/data/{}".format(index)}}
        chain_nodes, connection = _chain(source, index)
        nodes.extend(chain_nodes)
        connections.append(connection)

    definition = {"schemaVersion": 1, "nodes": nodes,
                  "connections": connections}
    return definition, stream_specs, camera_node_ids


@st.composite
def _multi_stream_definitions(draw):
    """A 2..3-stream document: parseable, V7-invalid, and never packaged.
    Used only for the cardinality clause through ``build_binding_points``,
    whose stream branch reads nothing from the compiled document."""
    chain_count = draw(st.integers(min_value=2, max_value=3))
    nodes, connections = [], []
    stream_specs, stream_ids = {}, []
    for index in range(chain_count):
        node_type = draw(st.sampled_from(sorted(EXPECTED_STREAM_PROTOCOLS)))
        parameters = draw(_stream_parameters(node_type))
        source = {"id": "str{}".format(index), "type": node_type,
                  "position": {"x": 100.0 * index, "y": 0.0},
                  "parameters": parameters}
        hint = draw(st.one_of(st.none(), _hints))
        if hint is not None:
            source["data"] = {"cameraBindingHint": hint}
        stream_specs[source["id"]] = (node_type, parameters, hint)
        stream_ids.append(source["id"])
        chain_nodes, connection = _chain(source, index)
        nodes.extend(chain_nodes)
        connections.append(connection)
    definition = {"schemaVersion": 1, "nodes": nodes,
                  "connections": connections}
    return definition, stream_specs, stream_ids


@st.composite
def _stream_free_definitions(draw):
    """A valid definition of 1..3 source->capture chains carrying NO
    stream node: folder_source and icam_source chains, plus at most one
    aravis_camera_source chain (the V7 singleton bound), so the corpus
    covers a cameraless document, camera documents with rendered device
    slots, and a non-stream Frame_Feed_Source_Node."""
    chain_count = draw(st.integers(min_value=1, max_value=3))
    aravis_index = draw(st.one_of(
        st.none(), st.integers(min_value=0, max_value=chain_count - 1)))

    nodes, connections = [], []
    camera_node_ids = []
    for index in range(chain_count):
        if index == aravis_index:
            parameters = {"camera_id": "cam-{}".format(index)}
            if draw(st.booleans()):
                parameters["gain"] = draw(st.integers(min_value=0, max_value=100))
            source = {"id": "arv{}".format(index),
                      "type": "aravis_camera_source",
                      "position": {"x": 100.0 * index, "y": 0.0},
                      "parameters": parameters}
            camera_node_ids.append(source["id"])
        elif draw(st.booleans()):
            source = {"id": "cam{}".format(index), "type": "icam_source",
                      "position": {"x": 100.0 * index, "y": 0.0},
                      "parameters": {"device": draw(_device_paths)}}
            camera_node_ids.append(source["id"])
        else:
            source = {"id": "src{}".format(index), "type": "folder_source",
                      "position": {"x": 100.0 * index, "y": 0.0},
                      "parameters": {"location": "/data/{}".format(index)}}
        if source["type"] != "folder_source" and draw(st.booleans()):
            source["data"] = {"cameraBindingHint": draw(_hints)}
        chain_nodes, connection = _chain(source, index)
        nodes.extend(chain_nodes)
        connections.append(connection)

    definition = {"schemaVersion": 1, "nodes": nodes,
                  "connections": connections}
    return definition, camera_node_ids


# ---------------------------------------------------------------------------
# Helpers shared by the properties
# ---------------------------------------------------------------------------

def _parsed(definition):
    result = parse(json.dumps(definition))
    assert result.ok, "generated definition did not parse: {0}".format(
        result.error)
    return result.graph


def _catalog():
    catalog = resolve_catalog([])
    return catalog, {descriptor.type_id: descriptor for descriptor in catalog}


def _compiled(graph, arch, catalog, workflow_id):
    context = CompileContext(workflow_id=workflow_id, workflow_version="1")
    compiled = compile_workflow(graph, arch, context, simulation=False,
                               catalog=catalog)
    assert not isinstance(compiled, list), (
        "compilation failed on {0}: {1}".format(arch, compiled))
    return compiled


def _assert_stream_point(point, node_type, parameters, hint):
    """Every clause Requirement 9.1 states about one stream binding point."""
    assert point["nodeId"].startswith("str")
    assert point["nodeType"] == node_type
    # streamBinding: true — the marker, identically true, not merely truthy.
    assert point["streamBinding"] is True
    # The node's protocol.
    assert point["streamProtocol"] == EXPECTED_STREAM_PROTOCOLS[node_type]
    # Empty slots: no stream parameter is ever substituted into an element
    # argument (the chain is ``appsrc ! videoconvert``).
    assert point["slots"] == []
    # The node's rendered parameters: declared defaults overlaid with the
    # node's explicit values.
    assert point["parameters"] == expected_rendered_parameters(parameters)
    # Exactly the keys of a stream point, plus the hint when recorded.
    expected_keys = set(STREAM_POINT_KEYS)
    if hint is not None:
        expected_keys.add("bindingHint")
        assert point["bindingHint"] == hint
    else:
        assert "bindingHint" not in point
    assert set(point) == expected_keys
    for marker in FOREIGN_BINDING_MARKERS:
        assert marker not in point


# ---------------------------------------------------------------------------
# Property 9, clause 1: stream binding points
# ---------------------------------------------------------------------------

@_EXAMPLES
@given(case=_stream_definitions())
def test_every_architecture_document_carries_the_stream_binding_point(
        packaging, case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 9: Stream binding points and stream-free packaging identity**

    For any definition with a stream node, every architecture's compiled
    document carries one binding point for it, with ``streamBinding:
    true``, the node's protocol, empty slots, and the node's rendered
    parameters; and the node is recorded in ``camera_input_nodes`` with
    ``has_binding_points: true``.

    **Validates: Requirements 9.1, 9.2**
    """
    definition, stream_specs, camera_node_ids = case
    graph = _parsed(definition)

    # Premise: the generated document really is a valid definition, and
    # its Stream_URL really is credential-free and correctly schemed.
    errors = [finding for finding in validate(graph)
              if finding.severity == SEVERITY_ERROR]
    assert not errors, "generated definition is not valid: {0}".format(errors)
    for node_type, parameters, _ in stream_specs.values():
        assert check_stream_url(
            parameters["url"], STREAM_SCHEMES[node_type]) is None

    # Every stream node is gathered as a Camera_Input_Node, in graph order.
    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    assert [node.id for node in camera_nodes] == camera_node_ids

    hints = packaging.binding_hints_from_definition(definition)
    catalog, descriptors_by_id = _catalog()

    arch_binding_points, arch_compiled_dicts = {}, {}
    for arch in DEVICE_ARCHITECTURES:
        compiled = _compiled(graph, arch, catalog, "wf-p9")
        compiled_dict = compiled.to_dict()
        points = packaging.build_binding_points(
            camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
        arch_compiled_dicts[arch] = compiled_dict
        arch_binding_points[arch] = points

        # One entry per Camera_Input_Node, hence exactly one per stream
        # node per architecture.
        assert [point["nodeId"] for point in points] == camera_node_ids
        stream_points = [point for point in points
                         if point["nodeId"] in stream_specs]
        assert len(stream_points) == len(stream_specs)

        for point in stream_points:
            node_type, parameters, hint = stream_specs[point["nodeId"]]
            _assert_stream_point(point, node_type, parameters, hint)

        # Non-stream camera entries never gain a stream key.
        for point in points:
            if point["nodeId"] not in stream_specs:
                for key in STREAM_KEYS:
                    assert key not in point

        # ...and the point reaches the architecture's *document* verbatim.
        document = json.loads(
            packaging.compiled_document_json(compiled, points))
        assert document["bindingPoints"] == points

    # Requirement 9.1's record clause: the stream node is recorded in
    # camera_input_nodes, with no parameter landing in a device path.
    records = packaging.camera_input_nodes_record(
        camera_nodes, hints, arch_binding_points, arch_compiled_dicts)
    assert [record["node_id"] for record in records] == camera_node_ids
    for record in records:
        if record["node_id"] in stream_specs:
            node_type, _, hint = stream_specs[record["node_id"]]
            assert record["node_type"] == node_type
            assert record["compiled_device_paths"] == {}
            if hint is None:
                assert "binding_hint" not in record
            else:
                assert record["binding_hint"] == hint

    # ...with has_binding_points: true (the handler stores
    # bool(camera_nodes); a stream workflow always has camera nodes).
    assert bool(camera_nodes) is True


@_EXAMPLES
@given(case=_stream_definitions())
def test_stream_binding_points_do_not_depend_on_the_compiled_document(
        packaging, case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 9: Stream binding points and stream-free packaging identity**

    A stream binding point is a function of the node alone: it is the same
    entry on every architecture and against any compiled document,
    because the ``streamBinding`` branch substitutes no element argument.
    This is what licenses the cardinality property below to drive
    ``build_binding_points`` with a stand-in document.

    **Validates: Requirement 9.1**
    """
    definition, stream_specs, _ = case
    graph = _parsed(definition)
    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    hints = packaging.binding_hints_from_definition(definition)
    catalog, descriptors_by_id = _catalog()

    def stream_entries(compiled_dict, arch):
        return [point for point in packaging.build_binding_points(
            camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
            if point["nodeId"] in stream_specs]

    per_arch = {}
    for arch in DEVICE_ARCHITECTURES:
        compiled_dict = _compiled(graph, arch, catalog, "wf-p9").to_dict()
        per_arch[arch] = stream_entries(compiled_dict, arch)
        # Same entries against an empty stand-in document.
        assert stream_entries({}, arch) == per_arch[arch]

    first = per_arch[DEVICE_ARCHITECTURES[0]]
    for arch in DEVICE_ARCHITECTURES:
        assert per_arch[arch] == first, (
            "stream binding point differs on {0}".format(arch))


@_EXAMPLES
@given(case=_multi_stream_definitions())
def test_one_binding_point_per_stream_node(packaging, case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 9: Stream binding points and stream-free packaging identity**

    The cardinality clause, on documents carrying 2..3 stream nodes: one
    binding point per stream node, each with its own node's protocol and
    rendered parameters. Such a document is V7-invalid (Requirement 2.4)
    so it never reaches packaging; it is driven through
    ``build_binding_points`` with a stand-in compiled document, which the
    property above shows the stream branch never reads.

    **Validates: Requirement 9.1**
    """
    definition, stream_specs, stream_ids = case
    graph = _parsed(definition)

    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    assert [node.id for node in camera_nodes] == stream_ids

    hints = packaging.binding_hints_from_definition(definition)
    _, descriptors_by_id = _catalog()
    for arch in DEVICE_ARCHITECTURES:
        points = packaging.build_binding_points(
            camera_nodes, {}, arch, hints, descriptors_by_id)
        assert [point["nodeId"] for point in points] == stream_ids
        for point in points:
            node_type, parameters, hint = stream_specs[point["nodeId"]]
            _assert_stream_point(point, node_type, parameters, hint)


# ---------------------------------------------------------------------------
# Property 9, clause 2: stream-free packaging identity
# ---------------------------------------------------------------------------

def pre_feature_camera_nodes(graph):
    """The pre-feature gather rule for the built-in camera inputs in play
    here — icam_source and aravis_camera_source nodes, never a stream type
    (no custom camera-backed types: resolved_items is empty)."""
    return [node for node in graph.nodes
            if node.type in ("icam_source", "aravis_camera_source")]


@_EXAMPLES
@given(case=_stream_free_definitions())
def test_stream_free_packaging_is_byte_identical_to_pre_feature_output(
        packaging, case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 9: Stream binding points and stream-free packaging identity**

    For any definition with no stream node, ``compiled_document_json`` is
    byte-equal to the pre-feature packaging output on every architecture,
    the version-item record is unchanged, and no stream key appears
    anywhere.

    **Validates: Requirement 9.2**
    """
    definition, camera_node_ids = case
    graph = _parsed(definition)

    errors = [finding for finding in validate(graph)
              if finding.severity == SEVERITY_ERROR]
    assert not errors, "generated definition is not valid: {0}".format(errors)

    # The feature's gather gate adds nothing on a stream-free graph: the
    # gathered Camera_Input_Nodes ARE the pre-feature set.
    camera_nodes = packaging.gather_camera_input_nodes(graph, set())
    expected_nodes = pre_feature_camera_nodes(graph)
    assert [node.id for node in camera_nodes] == camera_node_ids
    assert [node.id for node in camera_nodes] == [
        node.id for node in expected_nodes]

    hints = packaging.binding_hints_from_definition(definition)
    catalog, descriptors_by_id = _catalog()

    arch_binding_points, arch_compiled_dicts = {}, {}
    pre_feature_points_by_arch, pre_feature_docs = {}, {}
    for arch in DEVICE_ARCHITECTURES:
        compiled = _compiled(graph, arch, catalog, "wf-p9-free")
        compiled_dict = compiled.to_dict()

        binding_points = packaging.build_binding_points(
            camera_nodes, compiled_dict, arch, hints, descriptors_by_id)
        packaged_text = packaging.compiled_document_json(
            compiled, binding_points)

        # Pre-feature reconstruction: the same pure pipeline driven by the
        # pre-feature gather rule (the type-id membership is the only
        # change this feature made to it).
        pre_feature_points = packaging.build_binding_points(
            expected_nodes, compiled_dict, arch, hints, descriptors_by_id)
        pre_feature_text = packaging.compiled_document_json(
            compiled, pre_feature_points)

        # 9.2: byte-identical output.
        assert packaged_text == pre_feature_text

        arch_binding_points[arch] = binding_points
        arch_compiled_dicts[arch] = compiled_dict
        pre_feature_points_by_arch[arch] = pre_feature_points
        pre_feature_docs[arch] = compiled_dict

        # Structure pinning, as the camera-registry-sync snapshots did.
        document = json.loads(packaged_text)
        assert_no_stream_key(document)
        if not camera_nodes:
            # Cameraless: byte-identical to the compiler's own
            # serialization — exactly the pre-feature packager output.
            assert packaged_text == compiled.to_json()
            assert "bindingPoints" not in document
        else:
            expected_doc = compiled.to_dict()
            expected_doc["bindingPoints"] = binding_points
            assert packaged_text == json.dumps(
                expected_doc, sort_keys=True, indent=2, ensure_ascii=True)
            for point in document["bindingPoints"]:
                assert set(point) <= PRE_FEATURE_POINT_KEYS

    # The version-item record is the pre-feature record too, stream key
    # free.
    records = packaging.camera_input_nodes_record(
        camera_nodes, hints, arch_binding_points, arch_compiled_dicts)
    pre_feature_records = packaging.camera_input_nodes_record(
        expected_nodes, hints, pre_feature_points_by_arch, pre_feature_docs)
    assert records == pre_feature_records
    assert_no_stream_key(records)


# ---------------------------------------------------------------------------
# A stream source spelled as an Input Source (unified_input)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,node_type,url", [
    ("rtsp_camera", "rtsp_camera_source", "rtsp://camera.example:8554/line1"),
    ("rtmp_stream", "rtmp_stream_source", "rtmp://relay.example:1935/live/line1"),
])
def test_an_input_source_set_to_a_stream_kind_gets_the_stream_binding_point(
        packaging, kind, node_type, url):
    """Since unified-input-camera-binding (merged 2026-09-29), packaging
    gathers Camera_Input_Nodes from the unified-input-expanded graph: an
    Input Source set to a stream kind is the dedicated stream node it
    stands for, under the same id, so it gets the same ``streamBinding``
    point on every architecture and is recorded as a Camera_Input_Node.
    **Validates: Requirements 1.6, 9.1**
    """
    from workflow_core.compiler.compiler import expand_unified_inputs

    definition = {
        "schemaVersion": 1,
        "nodes": [
            {"id": "strsrc", "type": "unified_input", "position": {"x": 0.0, "y": 0.0},
             "parameters": {"source_kind": kind, "url": url}},
            {"id": "cap", "type": "capture", "position": {"x": 300.0, "y": 0.0},
             "parameters": {"output_path": "/out/line1"}},
        ],
        "connections": [{"id": "c1", "from": {"node": "strsrc", "port": "out"},
                         "to": {"node": "cap", "port": "in"}}],
    }
    graph = _parsed(definition)
    catalog, descriptors_by_id = _catalog()
    camera_nodes = packaging.gather_camera_input_nodes(
        expand_unified_inputs(graph, catalog), set())
    assert [(node.id, node.type) for node in camera_nodes] == [("strsrc", node_type)]
    assert packaging.gather_stream_feature_node_ids(graph) == ["strsrc"]
    for arch in DEVICE_ARCHITECTURES:
        compiled = _compiled(graph, arch, catalog, "wf-unified-stream")
        points = packaging.build_binding_points(
            camera_nodes, compiled.to_dict(), arch, {}, descriptors_by_id)
        assert len(points) == 1, points
        point = points[0]
        assert point["nodeId"] == "strsrc" and point["nodeType"] == node_type
        assert point["streamBinding"] is True
        assert point["streamProtocol"] == EXPECTED_STREAM_PROTOCOLS[node_type]
        assert point["slots"] == []
        assert point["parameters"]["url"] == url
        for marker in FOREIGN_BINDING_MARKERS:
            assert marker not in point


def test_the_packaging_handler_binds_an_input_source_set_to_a_stream_kind(
        aws_stack, packaging, monkeypatch):
    """The same through the real packaging handler, with the feature floor
    configured: every architecture's compiled document carries the
    ``streamBinding`` point for the Input Source, and the version item
    records it as a Camera_Input_Node of the dedicated stream type.
    **Validates: Requirements 1.6, 9.1, 9.7**
    """
    from test_unified_input_camera_binding import PackagingHarness

    monkeypatch.setattr(packaging, "STREAM_CAMERA_MIN_LOCAL_SERVER_VERSIONS", {
        arch: "1.0.50" for arch in packaging.ARCH_TO_LOCAL_SERVER_COMPONENT})
    harness = PackagingHarness(aws_stack, packaging, monkeypatch)
    url = "rtsp://camera.example:8554/line2"
    packaged = harness.package({
        "schemaVersion": 1,
        "nodes": [
            {"id": "strsrc", "type": "unified_input", "position": {"x": 0.0, "y": 0.0},
             "parameters": {"source_kind": "rtsp_camera", "url": url}},
            {"id": "cap", "type": "capture", "position": {"x": 300.0, "y": 0.0},
             "parameters": {"output_path": "/out/line2"}},
        ],
        "connections": [{"id": "c1", "from": {"node": "strsrc", "port": "out"},
                         "to": {"node": "cap", "port": "in"}}],
    })
    recorded = packaged.version_item["camera_input_nodes"]
    assert [(n["node_id"], n["node_type"]) for n in recorded] == [("strsrc", "rtsp_camera_source")]
    assert packaged.version_item["has_binding_points"] is True
    for arch in DEVICE_ARCHITECTURES:
        points = packaged.binding_points(arch)
        assert [(p["nodeId"], p.get("streamBinding"), p.get("streamProtocol")) for p in points] == \
            [("strsrc", True, "rtsp")], (arch, points)
        assert points[0]["parameters"]["url"] == url
