"""
Frontend parity fixtures for the rtsp-rtmp-stream-cameras rules
(spec task 11.2: Property 2's TypeScript port and Property 6's inline-check
parity).

The Python modules are the source of truth; the Workflow_Builder carries
TypeScript ports (``pages/workflows/streamUrl.ts``, ``sceneConfig.ts`` and
the V7/V9/V11/V12/V13/W3 mirrors in ``inlineChecks.ts``, plus the Layout
Check mirrors V14/W4 with their ``parseExpectedRegions`` port,
car-assembly-check task 7.3). This module generates two deterministic
corpora from the Python implementation, which the frontend property tests
replay:

- ``streamUrlCorpus.json``: values run through ``check_stream_url`` with
  each Stream_Camera_Source_Node type's schemes (and a few custom scheme
  lists), recording the verdict, the problem code and the message.
- ``inlineParityCorpus.json``: generated workflow graphs run through the
  real validator, recording every finding of the mirrored codes, plus the
  served (camelCase wire form) catalog descriptors the graphs use.

``test_frontend_stream_parity_fixtures.py`` fails when the committed
fixtures differ from what this module generates, so a change to the Python
rules cannot silently leave the TypeScript ports behind. Regenerate with::

    cd edge-cv-portal/backend
    ~/.venvs/dda-portal-tests/bin/python tests/frontend_parity_fixtures.py --write
"""
import json
import math
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
FIXTURES_DIR = os.path.join(
    os.path.dirname(_BACKEND), "frontend", "src", "pages", "workflows",
    "__fixtures__")
STREAM_URL_CORPUS = os.path.join(FIXTURES_DIR, "streamUrlCorpus.json")
INLINE_PARITY_CORPUS = os.path.join(FIXTURES_DIR, "inlineParityCorpus.json")

#: The validator codes the frontend mirrors for this feature (Property 6),
#: plus V9, whose continuous-stream skip is part of the same rule set, and
#: the Layout Check rules V14 and W4 (car-assembly-check task 7.3).
MIRRORED_CODES = (
    "V7_COEXISTENCE_CONFLICT",
    "V9_MIXED_ACTIVATION_MODEL",
    "V11_STREAM_URL",
    "V12_CONTINUOUS_ACTIVATION",
    "V13_ANALYTICS_CONFIG_INVALID",
    "W3_ANALYTICS_NO_DETECTOR",
    "V14_LAYOUT_CONFIG_INVALID",
    "W4_LAYOUT_NO_DETECTOR",
)

SEED = 20260926


def _ensure_paths():
    """Import paths for running this module as a script (the test
    conftest sets the same ones)."""
    # workflow_validation creates boto3 resources at import time.
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    for path in (os.path.join(_BACKEND, "functions"),
                 os.path.join(_BACKEND, "layers", "shared", "python")):
        if path not in sys.path:
            sys.path.insert(0, path)
    layer = os.path.join(_BACKEND, "layers", "workflow_core", "python")
    if layer not in sys.path:
        # Appended: the layer's vendored native wheels are for the Lambda
        # runtime, the interpreter's own site-packages must win.
        sys.path.append(layer)


def _dump(value) -> str:
    """One corpus as JSON with one entry per line (stable, reviewable)."""
    header = {key: value[key] for key in value if key != "entries"}
    lines = [json.dumps(entry, ensure_ascii=False, sort_keys=True)
             for entry in value["entries"]]
    body = ",\n".join(lines)
    head = json.dumps(header, ensure_ascii=False, sort_keys=True)[:-1]
    separator = ", " if header else ""
    return f'{head}{separator}"entries": [\n{body}\n]}}\n'


# ---------------------------------------------------------------------------
# Stream_URL corpus (Property 2)
# ---------------------------------------------------------------------------

_SCHEMES = ["rtsp", "rtsps", "rtmp", "rtmps", "RTSP", "Rtmp", "http", "file",
            "rt-sp", "r"]
_AUTHORITIES = [
    "10.0.4.21", "cam.local", "cam.local:8554", "10.0.0.5:554", "[::1]",
    "[::1]:554", "[fe80::1%25eth0]", "[::1", "a]b", "host:", ":554", "",
    "host:abc", "host:\u00b2", "host:\u0663", "h\u00f3st.local",
    "\u30db\u30b9\u30c8", "user@host", "user:pass@host", "@host", "a@b@c",
    "host:1935", "HOST.Example.COM:443",
]
_TAILS = [
    "", "/", "/live", "/Streaming/Channels/101", "/live/line1",
    "?channel=1", "/x?token=abc", "/x?Password=1", "/x?keyframe=1",
    "/x?a=1&stream_key=2", "/x?a=1;sig=3", "/x?PWD", "/x?=1", "/x?&&",
    "/x?auth =1", "/x?\u212aEY=1", "#frag", "/x#y", " /x", "/x\ty",
    "/x\u00a0y", "/x\ufeffy", "/x\x1cy", "/x\x85y", "\n", "/x\n",
    "/p%20q", "/x?keypad=1&passthrough=0", "/a/b/c?x=1#", "/\u00e9",
]
_WHOLE_VALUES = [
    None, 5, True, [], {}, "", " ", "\x1c", "\ufeff", "\t\n",
    "not a url", "rtsp:", "rtsp:/host", "rtsp//host", "://host",
    "rtsp://", " rtsp://10.0.0.5/live", "rtsp://10.0.0.5/live ",
    "rtsp://10.0.0.5/live\n", "\u00a0rtsp://10.0.0.5/live",
    "rtsp://10.0.0.5/live\ufeff", "rtsp://user:p%40ss@10.0.0.5/live",
    "RTSP://10.0.0.5/live", "rtmps://media.example.com:443/live/key",
    "rtsp://[2001:db8::1]:8554/stream?profile=main",
]
_CUSTOM_SCHEMES = [[], ["RTSP"], [" rtmp ", "rtmp"], "rtsp", [5, "rtsps"]]
_NODE_TYPES = ("rtsp_camera_source", "rtmp_stream_source")


def stream_url_corpus():
    """Deterministic Stream_URL corpus with the Python verdicts."""
    _ensure_paths()
    from workflow_core.stream_url import SCHEMES_BY_NODE_TYPE, check_stream_url

    rng = random.Random(SEED)
    values = list(_WHOLE_VALUES)
    # Every scheme with every authority, and every authority with a sample
    # of the tails (every tail appears), each with the rtsp scheme.
    for scheme in _SCHEMES:
        for authority in _AUTHORITIES:
            values.append(f"{scheme}://{authority}{rng.choice(_TAILS)}")
    for index, tail in enumerate(_TAILS):
        for authority in rng.sample(_AUTHORITIES, 5) + [_AUTHORITIES[index % len(_AUTHORITIES)]]:
            values.append(f"rtsp://{authority}{tail}")
    # Random strings over a URL-ish alphabet with the Python/JavaScript
    # whitespace and digit differences mixed in.
    alphabet = ("abcdefghijklmnopqrstuvwxyzAB0123456789:/@?#&=;.-_[]%~ "
                "\t\n\x1c\x85\u00a0\ufeff\u00b2\u00e9")
    for _ in range(150):
        prefix = rng.choice(["rtsp://", "rtmp://", "rtsps://", "", "rtmps://"])
        length = rng.randint(0, 24)
        values.append(prefix + "".join(rng.choice(alphabet)
                                       for _ in range(length)))

    messages = []
    entries = []

    def entry(value, schemes, node_type):
        problem = check_stream_url(value, schemes)
        record = {"url": value, "code": None, "m": None}
        if problem is not None:
            if problem.message not in messages:
                messages.append(problem.message)
            record["code"] = problem.code
            record["m"] = messages.index(problem.message)
        if node_type is not None:
            record["nodeType"] = node_type
        else:
            record["schemes"] = schemes
        entries.append(record)

    for value in values:
        for node_type in _NODE_TYPES:
            entry(value, SCHEMES_BY_NODE_TYPE[node_type], node_type)
    for schemes in _CUSTOM_SCHEMES:
        for value in ("rtsp://10.0.0.5/live", "rtmp://m/live",
                      "rtsp://h:1/x?key=1"):
            entry(value, schemes, None)
    return {"generatedFrom": "workflow_core/stream_url.py",
            "messages": messages, "entries": entries}


# ---------------------------------------------------------------------------
# Inline-check parity corpus (Property 6)
# ---------------------------------------------------------------------------

_URLS = [
    "rtsp://10.0.4.21:554/Streaming/Channels/101", "rtmp://media.local/live/l1",
    "rtsps://cam.local/x", "rtmps://m.example.com/live", "rtsp://u:p@h/x",
    "rtsp://h/x?token=1", "http://h/x", "", "   ", None, "rtsp://", 7,
]
_MODES = ["continuous", "on_trigger", None, "bogus"]
_CLASSES = ["person, hardhat", "person", "a,,b", "!!!", "", "  ",
            ",".join(f"c{i}" for i in range(33)), ["person", "vest"],
            ["person", None], 5, "Person, person", "\u00e9t\u00e9, car"]
_SUBJECTS = ["person", "person, dog", "", "!!", ["forklift"], ["a", "b"]]
_REQUIRED = ["hardhat, vest", ",".join(f"r{i}" for i in range(11)), "a,,b",
             "", ["hardhat"], "vest, !!"]
_ZONES = [
    "[[0, 0], [1, 0], [0.5, 1]]", '{"points": [[0, 0], [1, 0], [1, 1]]}',
    '[{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}]',
    "not json", "[[0, 0], [1, 1]]", "[[0, 0], [1, 0], [0.5, 1.5]]",
    '{"poly": []}', '"text"', "", "[[0, 0], [1, 0], [0.5, true]]",
    "[[0, 0], [1, 0], [0.5, NaN]]", "[" + ",".join(["[0, 0]"] * 33) + "]",
    [[0, 0], [1, 0], [0, 1]], 42,
    # Python json.loads extensions and look-alikes the port must read the
    # same way.
    '[{"x": -Infinity, "y": 0}, [1, 0], [0, 1]]', "NaN", '{"points": Infinity}',
    '[[0, 0], [1, 0], ["NaN", 1]]', "[[0, 0], [1, 0], [0, -NaN]]",
    '[[0, 0], [1, 0], [0, 1], "Infinity"]', "[[1e999, 0], [1, 0], [0, 1]]",
]

_ALL_TYPES = (
    "rtsp_camera_source", "rtmp_stream_source", "unified_input",
    "aravis_camera_source", "custom_python_source", "folder_source",
    "mqtt_subscribe", "opcua_subscribe", "digital_input", "model_inference",
    "detection_counter", "object_association", "event_gate", "capture",
)

#: The descriptors the corpus serves: every generated type plus
#: ``layout_check``, which only the handwritten graphs use. ``_ALL_TYPES``
#: itself stays as it is, because ``_graph`` draws from it with a
#: positional weights list and the seeded graphs must not change.
_CATALOG_TYPES = _ALL_TYPES + ("layout_check",)


def _maybe(rng, parameters, name, pool, absent_weight=0.2):
    """Set ``name`` from ``pool``, or leave it unset."""
    if rng.random() < absent_weight:
        return
    parameters[name] = rng.choice(pool)


def _node(rng, index, node_type):
    parameters = {}
    if node_type in ("rtsp_camera_source", "rtmp_stream_source"):
        _maybe(rng, parameters, "url", _URLS)
        _maybe(rng, parameters, "processing_mode", _MODES, 0.4)
    elif node_type == "unified_input":
        _maybe(rng, parameters, "source_kind",
               ["rtsp_camera", "rtmp_stream", "folder", "aravis_camera", "bogus", 3],
               0.15)
        _maybe(rng, parameters, "url", _URLS)
        _maybe(rng, parameters, "processing_mode", _MODES, 0.4)
    elif node_type == "detection_counter":
        _maybe(rng, parameters, "classes", _CLASSES)
        _maybe(rng, parameters, "zone", _ZONES)
    elif node_type == "object_association":
        _maybe(rng, parameters, "subject_class", _SUBJECTS)
        _maybe(rng, parameters, "required_classes", _REQUIRED)
        _maybe(rng, parameters, "zone", _ZONES)
    return {"id": f"n{index}", "type": node_type,
            "position": {"x": index * 10, "y": 0}, "parameters": parameters}


def _graph(rng):
    count = rng.randint(1, 6)
    weights = [4, 4, 4, 2, 2, 2, 2, 2, 1, 3, 4, 4, 1, 2]
    nodes = [_node(rng, i, rng.choices(_ALL_TYPES, weights)[0])
             for i in range(count)]
    connections = []
    for index in range(rng.randint(0, count + 2)):
        source, target = rng.choice(nodes), rng.choice(nodes)
        if source is target:
            continue
        connections.append({
            "id": f"c{index}",
            "from": {"node": source["id"], "port": "out"},
            "to": {"node": target["id"],
                   "port": rng.choice(["in", "in", "activation"])},
        })
    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


_HANDWRITTEN = [
    # Two frame-feed types, the generalized mixed rule.
    ("rtsp plus aravis", [("rtsp_camera_source", {"url": _URLS[0]}),
                           ("aravis_camera_source", {})], []),
    ("rtsp plus rtmp", [("rtsp_camera_source", {"url": _URLS[0]}),
                         ("rtmp_stream_source", {"url": _URLS[1]})], []),
    ("two rtsp", [("rtsp_camera_source", {"url": _URLS[0]}),
                   ("rtsp_camera_source", {"url": _URLS[2]})], []),
    ("three frame-feed types", [("rtsp_camera_source", {"url": _URLS[0]}),
                                 ("custom_python_source", {}),
                                 ("aravis_camera_source", {})], []),
    # Continuous vs on_trigger with a subscription trigger (V9 / V12).
    ("continuous with mqtt", [("rtsp_camera_source", {"url": _URLS[0]}),
                               ("mqtt_subscribe", {})], []),
    ("on_trigger with mqtt, unconnected",
     [("rtsp_camera_source", {"url": _URLS[0], "processing_mode": "on_trigger"}),
      ("mqtt_subscribe", {})], []),
    ("on_trigger with mqtt, connected",
     [("rtsp_camera_source", {"url": _URLS[0], "processing_mode": "on_trigger"}),
      ("mqtt_subscribe", {})], [(1, 0, "activation")]),
    ("continuous with an activation edge",
     [("rtsp_camera_source", {"url": _URLS[0]}), ("digital_input", {})],
     [(1, 0, "activation")]),
    ("unified rtsp kind with opcua",
     [("unified_input", {"source_kind": "rtsp_camera", "url": _URLS[4]}),
      ("opcua_subscribe", {})], []),
    # Analytics.
    ("counter without detector", [("folder_source", {}),
                                  ("detection_counter", {"classes": "person"})],
     [(0, 1, "in")]),
    ("counter with detector", [("folder_source", {}), ("model_inference", {}),
                               ("detection_counter", {"zone": "not json"})],
     [(0, 1, "in"), (1, 2, "in")]),
    ("association malformed", [("model_inference", {}),
                               ("object_association",
                                {"subject_class": "person, dog",
                                 "required_classes": "a,,b"})],
     [(0, 1, "in")]),
    ("event gate only", [("event_gate", {})], []),
    ("no new node types", [("folder_source", {}), ("mqtt_subscribe", {}),
                           ("capture", {})], [(0, 2, "in")]),
    # Node types and source kinds named like Object.prototype members. A
    # graph is external input, so the frontend's map lookups must behave
    # like dict.get here rather than resolve a prototype member.
    ("prototype-named node types",
     [("constructor", {"url": "rtsp://user:pw@10.0.0.5/stream"}),
      ("__proto__", {"url": "not a url"}),
      ("toString", {"url": _URLS[0]})], []),
    ("unified prototype-named source kinds",
     [("unified_input", {"source_kind": "constructor",
                         "url": "rtsp://user:pw@10.0.0.5/stream"}),
      ("unified_input", {"source_kind": "__proto__", "url": "x"}),
      ("unified_input", {"source_kind": "hasOwnProperty"})], []),
    # Two of a prototype-named type are not a singleton conflict (V7).
    ("two prototype-named nodes of one type",
     [("constructor", {}), ("constructor", {}), ("valueOf", {}),
      ("valueOf", {})], []),
]

# Layout Check (car-assembly-check task 7.3): every V14 rule of
# ``analytics.layout.parse_expected_regions`` (including the wrap-around
# ring the cyclic duplicate rule reads as closed), the three range rules,
# and W4 with and without an upstream model_inference.
_LAYOUT_TRIANGLE = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]
_LAYOUT_VALID = json.dumps({"roof": _LAYOUT_TRIANGLE})
_UNSET_PARAMETER = object()
_LAYOUT_CIRCLE_65 = [[round(0.5 + 0.4 * math.cos(2 * math.pi * i / 65), 6),
                      round(0.5 + 0.4 * math.sin(2 * math.pi * i / 65), 6)]
                     for i in range(65)]

_LAYOUT_REGION_CASES = [
    ("not json", "not json"),
    ("not an object", "[[0, 0], [1, 0], [0, 1]]"),
    ("a JSON string", '"text"'),
    ("a number", 5),
    ("no classes", "{}"),
    ("eleven classes",
     json.dumps({f"c{i}": _LAYOUT_TRIANGLE for i in range(11)})),
    ("ten classes", json.dumps({f"c{i}": _LAYOUT_TRIANGLE for i in range(10)})),
    ("empty label", json.dumps({"": _LAYOUT_TRIANGLE})),
    ("blank label", json.dumps({"roof": _LAYOUT_TRIANGLE, "  ": _LAYOUT_TRIANGLE})),
    ("65-character label", json.dumps({"x" * 65: _LAYOUT_TRIANGLE})),
    ("label repeated once stripped",
     json.dumps({"roof": _LAYOUT_TRIANGLE, " roof ": _LAYOUT_TRIANGLE})),
    ("polygon not a list", '{"roof": "x"}'),
    ("polygon an object", '{"roof": {"points": []}}'),
    ("point not a pair", '{"roof": [[0, 0], [1, 0], [0.5]]}'),
    ("point with a boolean", '{"roof": [[0, 0], [1, 0], [0.5, true]]}'),
    ("point with NaN", '{"roof": [[0, 0], [1, 0], [NaN, 1]]}'),
    ("point with -Infinity", '{"roof": [[0, 0], [1, 0], [0.5, -Infinity]]}'),
    ("point with a string", '{"roof": [[0, 0], [1, 0], ["0.5", 1]]}'),
    ("x out of range", '{"roof": [[0, 0], [1.5, 0], [0.5, 1]]}'),
    ("y out of range", '{"roof": [[0, 0], [1, 0], [0.5, -0.1]]}'),
    ("huge coordinate", '{"roof": [[0, 0], [1e999, 0], [0.5, 1]]}'),
    ("two points", '{"roof": [[0, 0], [1, 1]]}'),
    ("wrap-around ring of two", '{"roof": [[0, 0], [1, 0], [0, 0]]}'),
    ("consecutive duplicates of two", '{"roof": [[0, 0], [0, 0], [1, 0], [1, 0]]}'),
    ("wrap-around ring accepted",
     json.dumps({"roof": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]]})),
    ("three plus the repeated first accepted",
     json.dumps({"roof": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9], [0.1, 0.1]]})),
    ("65 points", json.dumps({"roof": _LAYOUT_CIRCLE_65})),
    ("64 points and the repeated first",
     json.dumps({"roof": _LAYOUT_CIRCLE_65[:64] + [_LAYOUT_CIRCLE_65[0]]})),
    ("tiny region", '{"roof": [[0, 0], [0.05, 0], [0, 0.05]]}'),
    ("self-intersecting ring",
     '{"roof": [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.5]]}'),
    ("symmetric bow tie", '{"roof": [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]]}'),
    ("touching non-adjacent edges",
     '{"roof": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.5], [0.9, 0.9], [0.1, 0.9], [0.5, 0.1]]}'),
    ("several problems",
     '{"roof": [[0, 0], [2, 0], [0.5]], "": [[0, 0], [1, 0], [0, 1]], '
     '"wheel": [[0, 0], [1, 1]]}'),
    ("three classes accepted", json.dumps({
        "rear_fender": [[0.12, 0.44], [0.19, 0.38], [0.30, 0.37], [0.47, 0.37],
                        [0.47, 0.86], [0.40, 0.90], [0.31, 0.90], [0.26, 0.82],
                        [0.12, 0.80]],
        "front_fender": [[0.47, 0.37], [0.74, 0.37], [0.88, 0.47], [0.90, 0.60],
                         [0.86, 0.78], [0.78, 0.81], [0.72, 0.88], [0.63, 0.88],
                         [0.58, 0.80], [0.47, 0.80]],
        "roof": [[0.30, 0.36], [0.33, 0.24], [0.40, 0.16], [0.50, 0.13],
                 [0.60, 0.16], [0.68, 0.24], [0.72, 0.36]]})),
    ("an already-parsed object", {"roof": _LAYOUT_TRIANGLE}),
    ("an already-parsed object with a problem", {"roof": [[0, 0], [1, 1]]}),
    # Blank or missing: V4's concern, no V14 finding.
    ("blank", "   "),
    ("empty", ""),
    ("null", None),
    ("missing", _UNSET_PARAMETER),
]

_LAYOUT_THRESHOLD_CASES = [
    ("min_iou below", {"min_iou": 0.005}),
    ("min_iou above", {"min_iou": 1.5}),
    ("min_iou as text", {"min_iou": "1.5"}),
    ("min_iou at its bounds", {"min_iou": 0.01}),
    ("min_iou one", {"min_iou": 1}),
    ("min_iou not a number", {"min_iou": "abc"}),
    ("min_confidence below", {"min_confidence": -0.1}),
    ("min_confidence above", {"min_confidence": 1.2}),
    ("min_confidence at its bounds", {"min_confidence": 0}),
    ("min_outline_overlap below", {"min_outline_overlap": -0.1}),
    ("min_outline_overlap above", {"min_outline_overlap": 0.51}),
    ("min_outline_overlap at its bounds", {"min_outline_overlap": 0.5}),
    ("min_outline_overlap zero", {"min_outline_overlap": 0}),
    ("min_outline_overlap as padded text", {"min_outline_overlap": " 0.75 "}),
    ("min_outline_overlap as an overflowing text", {"min_outline_overlap": "1e999"}),
    ("min_outline_overlap as hex text", {"min_outline_overlap": "0x1"}),
    ("every threshold out of range",
     {"min_iou": 0, "min_confidence": 2, "min_outline_overlap": 0.9}),
]


def _layout_spec(name, parameters, detector=True):
    """folder_source [-> model_inference] -> layout_check."""
    if detector:
        return (name, [("folder_source", {}), ("model_inference", {}),
                       ("layout_check", parameters)],
                [(0, 1, "in"), (1, 2, "in")])
    return (name, [("folder_source", {}), ("layout_check", parameters)],
            [(0, 1, "in")])


_HANDWRITTEN += [
    _layout_spec(f"layout regions: {name}",
                 {} if value is _UNSET_PARAMETER else {"expected_regions": value})
    for name, value in _LAYOUT_REGION_CASES
]
_HANDWRITTEN += [
    _layout_spec(f"layout thresholds: {name}",
                 dict(parameters, expected_regions=_LAYOUT_VALID))
    for name, parameters in _LAYOUT_THRESHOLD_CASES
]
_HANDWRITTEN += [
    _layout_spec("layout without a detector", {"expected_regions": _LAYOUT_VALID},
                 detector=False),
    _layout_spec("layout with a detector and broken regions",
                 {"expected_regions": "not json"}),
    ("layout with a transitive detector",
     [("folder_source", {}), ("model_inference", {}),
      ("detection_counter", {"classes": "person"}),
      ("layout_check", {"expected_regions": _LAYOUT_VALID})],
     [(0, 1, "in"), (1, 2, "in"), (2, 3, "in")]),
    ("layout beside an unconnected detector",
     [("folder_source", {}), ("model_inference", {}),
      ("layout_check", {"expected_regions": _LAYOUT_VALID}),
      ("layout_check", {"expected_regions": "{}"})],
     [(0, 2, "in"), (3, 2, "in")]),
]


def _handwritten_graph(spec):
    _name, node_specs, edges = spec
    nodes = [{"id": f"n{i}", "type": node_type,
              "position": {"x": i * 10, "y": 0}, "parameters": parameters}
             for i, (node_type, parameters) in enumerate(node_specs)]
    connections = [{"id": f"c{i}",
                    "from": {"node": f"n{source}", "port": "out"},
                    "to": {"node": f"n{target}", "port": port}}
                   for i, (source, target, port) in enumerate(edges)]
    return {"schemaVersion": 1, "nodes": nodes, "connections": connections}


def _catalog_entry(wire):
    """A served descriptor without the fields the mirrored checks never
    read (descriptions, examples, mappings), which keeps the fixture
    small; ports, categories, parameter types, defaults and constraints
    are kept exactly as served."""
    entry = dict(wire, mappings=[])
    entry["parameters"] = [dict(p, description=None, examples=None)
                           for p in wire["parameters"]]
    return entry


def inline_parity_corpus(graph_count: int = 250):
    """Deterministic graphs with the Python validator's mirrored findings."""
    _ensure_paths()
    from workflow_core.catalog import get_node_type
    from workflow_core.serializer import parse
    from workflow_core.validator import validate
    from workflow_validation import descriptor_to_wire

    rng = random.Random(SEED)
    graphs = [(name, _handwritten_graph((name, nodes, edges)))
              for name, nodes, edges in _HANDWRITTEN]
    graphs += [(f"generated {i}", _graph(rng)) for i in range(graph_count)]

    entries = []
    for name, definition in graphs:
        result = parse(json.dumps(definition))
        if not result.ok:
            raise AssertionError(f"{name}: {result.error}")
        findings = sorted(
            [finding.code, finding.node_id, finding.severity, finding.message]
            for finding in validate(result.graph)
            if finding.code in MIRRORED_CODES)
        entries.append({"name": name, "graph": definition,
                        "findings": findings})
    catalog = [_catalog_entry(descriptor_to_wire(get_node_type(type_id)))
               for type_id in _CATALOG_TYPES]
    return {"generatedFrom": "workflow_core/validator/checks.py",
            "mirroredCodes": list(MIRRORED_CODES), "catalog": catalog,
            "entries": entries}


def generated_files():
    """``{path: content}`` of every fixture this module owns."""
    return {
        STREAM_URL_CORPUS: _dump(stream_url_corpus()),
        INLINE_PARITY_CORPUS: _dump(inline_parity_corpus()),
    }


def main(argv):
    files = generated_files()
    if "--write" in argv:
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        for path, content in files.items():
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            print(f"wrote {path} ({len(content)} bytes)")
        return 0
    stale = [path for path, content in files.items()
             if not os.path.exists(path)
             or open(path, encoding="utf-8").read() != content]
    for path in stale:
        print(f"stale: {path}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
