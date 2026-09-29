# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Property test for stream feed plan precedence.

**Feature: rtsp-rtmp-stream-cameras, Property 15: Stream feed plan
precedence**

*For any* document, resolution, and set of configured cameras, the planned
camera SHALL be the first of these that applies:

1. the assignment's camera, when present;
2. the configured camera whose normalized URL equals the rendered ``url``;
3. the anonymous key of the normalized URL.

**Validates: Requirement 10.6**

Generators mirror the real input space: one packaged ``streamBinding``
point (the single-feed contract admits one) among V4L2 slot points, with
its rendered parameters; a resolution that is absent, has no assignment
for the node, binds a camera, or carries an override (with or without a
``url``); and the device's Image_Sources, stream and not, whose
Stream_URLs are spelled like the node's in ways normalization equates
(host case, an explicit default port) and in ways it does not (path
case). The configured-camera map is built by the production
``configured_stream_cameras``, so the property covers it too.
"""
import copy
import hashlib

from hypothesis import given
from hypothesis import strategies as st

from workflow_engine.camera_binding import STATUS_RESOLVED, ResolutionResult
from workflow_engine.stream_feed import (
    DEFAULTS,
    StreamFeed,
    configured_stream_cameras,
    plan_stream_feeds,
)

_NODE_TYPE = {"rtsp": "rtsp_camera_source", "rtmp": "rtmp_stream_source"}
_SOURCE_TYPE = {"rtsp": "RTSP", "rtmp": "RTMP"}
_DEFAULT_PORT = {"rtsp": 554, "rtmp": 1935}


def _canonical(protocol, index):
    """The normalized form of camera ``index``'s Stream_URL."""
    return "{0}://cam{1}.local/stream{1}".format(protocol, index)


@st.composite
def _spelling(draw, protocol, index):
    """A spelling of camera ``index``'s URL that normalizes to
    :func:`_canonical`."""
    host = "cam{0}.local".format(index)
    if draw(st.booleans()):
        host = host.upper()
    if draw(st.booleans()):
        host = "{0}:{1}".format(host, _DEFAULT_PORT[protocol])
    return "{0}://{1}/stream{2}".format(protocol, host, index)


def _distinct(protocol, index):
    """A URL that differs from camera ``index``'s only by path case, which
    normalization keeps: never the same camera."""
    return "{0}://cam{1}.local/Stream{1}".format(protocol, index)


@st.composite
def _processing(draw):
    """Rendered processing parameters: valid values, a missing key, or a
    junk value that falls back to the default."""
    values = {}
    for name, valid in (
            ("processing_mode", st.sampled_from(["continuous", "on_trigger"])),
            ("frames_per_second", st.floats(min_value=0.05, max_value=10.0)),
            ("max_frame_age_ms", st.integers(min_value=100, max_value=60000)),
            ("keep_recent_runs", st.integers(min_value=1, max_value=200)),
            ("keep_notable_runs", st.integers(min_value=0, max_value=5000))):
        choice = draw(st.sampled_from(["valid", "valid", "absent", "junk"]))
        if choice == "valid":
            values[name] = draw(valid)
        elif choice == "junk":
            values[name] = draw(st.sampled_from([None, "fast", True, [1]]))
    return values


@st.composite
def _cases(draw):
    protocol = draw(st.sampled_from(["rtsp", "rtmp"]))
    node_id = "cam_node{0}".format(draw(st.integers(min_value=1, max_value=9)))
    # Cameras 0..4: the node renders camera `rendered_index`'s URL.
    rendered_index = draw(st.integers(min_value=0, max_value=4))
    parameters = dict(draw(_processing()), url=draw(_spelling(protocol, rendered_index)))
    point = {"nodeId": node_id, "nodeType": _NODE_TYPE[protocol], "parameters": parameters,
             "slots": [], "streamBinding": True}
    if draw(st.booleans()):
        point["streamProtocol"] = protocol
    others = [{"nodeId": "v{0}".format(index), "nodeType": "camera_source",
               "parameters": {"device": "/dev/video0"},
               "slots": [{"param": "device", "segment": 0, "element": 0, "arg": "device"}]}
              for index in range(draw(st.integers(min_value=0, max_value=2)))]
    points = others + [point]
    document = {"schemaVersion": 1, "segments": [{"name": "s0", "elements": [
        {"nodeId": node_id, "factory": "appsrc", "args": {"name": "appsrc_" + node_id}},
        {"nodeId": node_id, "factory": "videoconvert", "args": {}}]}],
        "bindingPoints": draw(st.permutations(points))}

    # The device's Image_Sources: stream cameras of either type spelled
    # like camera i (or deceptively unlike it), and non-stream sources.
    sources = []
    for index in range(draw(st.integers(min_value=0, max_value=6))):
        camera = draw(st.integers(min_value=0, max_value=4))
        kind = draw(st.sampled_from(["same", "same", "distinct", "other-type", "not-stream"]))
        if kind == "same":
            location = draw(_spelling(protocol, camera))
            source_type = _SOURCE_TYPE[protocol]
        elif kind == "distinct":
            location, source_type = _distinct(protocol, camera), _SOURCE_TYPE[protocol]
        elif kind == "other-type":
            other = "rtmp" if protocol == "rtsp" else "rtsp"
            location, source_type = draw(_spelling(other, camera)), _SOURCE_TYPE[other]
        else:
            location, source_type = _canonical(protocol, camera), draw(st.sampled_from(["Camera", "Folder"]))
        sources.append({"imageSourceId": "is-{0:02d}".format(index), "type": source_type,
                        "location": location})
    draw(st.randoms()).shuffle(sources)

    variant = draw(st.sampled_from(["none", "no-assignment", "camera", "override", "override-no-url"]))
    assignment = None
    if variant == "camera":
        camera = draw(st.integers(min_value=0, max_value=4))
        assignment = {"cameraSourceId": "cfg-bound-{0}".format(camera),
                      "params": {"url": _canonical(protocol, camera), "credentialsConfigured": True}}
    elif variant == "override":
        camera = draw(st.integers(min_value=0, max_value=4))
        assignment = {"cameraSourceId": None,
                      "params": dict(draw(_processing()), url=draw(_spelling(protocol, camera)))}
    elif variant == "override-no-url":
        assignment = {"cameraSourceId": None, "params": draw(_processing())}
    if variant == "none":
        resolution = None
    else:
        assignments = {"stray": {"cameraSourceId": "cfg-stray", "params": {"url": _canonical(protocol, 9)}}}
        if assignment is not None:
            assignments[node_id] = assignment
        resolution = ResolutionResult(document=document, status=STATUS_RESOLVED,
                                      stream_assignments=assignments)
    return document, resolution, sources, node_id, protocol, parameters, assignment


# --- model ---------------------------------------------------------------------


def _model_normalize(url):
    """Normalization over the generated spellings: lowercase the host and
    drop the default port; the path is kept byte for byte."""
    scheme, rest = url.split("://", 1)
    authority, _, path = rest.partition("/")
    host, _, port = authority.partition(":")
    authority = host.lower() if not port or int(port) == _DEFAULT_PORT[scheme] else authority.lower()
    return "{0}://{1}/{2}".format(scheme, authority, path)


def _model_configured(sources, protocol):
    """{normalized URL: cfg-id} of the stream sources; the lowest
    Image_Source id wins a shared URL."""
    cameras = {}
    for source in sorted(sources, key=lambda item: item["imageSourceId"]):
        if source["type"] not in ("RTSP", "RTMP"):
            continue
        cameras.setdefault(_model_normalize(source["location"]), "cfg-" + source["imageSourceId"])
    return cameras


def _model_value(values, name, cast):
    value = values.get(name)
    if isinstance(value, bool) or value is None or isinstance(value, (str, list)):
        return DEFAULTS[name]
    return cast(value)


def _model_feed(node_id, protocol, parameters, assignment, configured):
    effective = dict(parameters)
    if assignment is not None and not assignment["cameraSourceId"]:
        effective.update(assignment["params"])
    if assignment is not None and assignment["cameraSourceId"]:
        url = _model_normalize(assignment["params"]["url"])
        key, camera, selected = assignment["cameraSourceId"], assignment["cameraSourceId"], "binding"
    else:
        url = _model_normalize(effective["url"])
        if url in configured:
            key, camera, selected = configured[url], configured[url], "url_match"
        else:
            key = "url-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            camera, selected = None, "anonymous"
    mode = effective.get("processing_mode")
    return StreamFeed(
        node_id=node_id, protocol=protocol, camera_key=key, url=url,
        processing_mode=mode if mode in ("continuous", "on_trigger") else "continuous",
        frames_per_second=_model_value(effective, "frames_per_second", float),
        max_frame_age_ms=_model_value(effective, "max_frame_age_ms", int),
        keep_recent_runs=_model_value(effective, "keep_recent_runs", int),
        keep_notable_runs=_model_value(effective, "keep_notable_runs", int),
        camera_source_id=camera, selected_by=selected)


# --- property ------------------------------------------------------------------


@given(case=_cases())
def test_stream_feed_plan_precedence(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 15: Stream feed plan
    precedence**

    **Validates: Requirement 10.6**
    """
    document, resolution, sources, node_id, protocol, parameters, assignment = case
    snapshot = copy.deepcopy(document)

    configured = configured_stream_cameras(sources)
    assert configured == _model_configured(sources, protocol)

    lookups = []

    def configured_cameras():
        lookups.append(1)
        return configured

    feeds = plan_stream_feeds(document, resolution, configured_cameras=configured_cameras)

    assert document == snapshot
    assert feeds == [_model_feed(node_id, protocol, parameters, assignment, configured)]
    # The configured cameras are only consulted when no binding chose one.
    bound = assignment is not None and bool(assignment["cameraSourceId"])
    assert lookups == ([] if bound else [1])
    # A mapping works the same as the callable.
    assert plan_stream_feeds(document, resolution, configured_cameras=configured) == feeds
