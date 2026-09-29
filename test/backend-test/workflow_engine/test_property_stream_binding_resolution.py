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
"""Property test for device-side stream binding resolution.

**Feature: rtsp-rtmp-stream-cameras, Property 14: Device-side stream
binding resolution**

*For any* document with stream binding points, bindings, and inventory:

- a binding to an entry of the matching type yields a stream assignment
  and no slot substitution;
- a missing or mismatched entry marks the resolution invalid, naming the
  camera;
- a violating override marks the resolution invalid;
- non-stream points resolve exactly as before.

**Validates: Requirements 10.1, 10.2**

Generators mirror the real input space: the packager emits one
``streamBinding: true`` point per Stream_Camera_Source_Node with empty
slots, the rendered stream parameters and (usually) ``streamProtocol``;
the inventory is the ``build_inventory`` shape, where a stream camera is
an ``edge-configured`` ``RTSP``/``RTMP`` entry whose params carry the
Stream_URL and its settings. Non-stream points are slot-substituted V4L2
points and Aravis points, resolved against Camera and AravisDiscovered
entries. Overrides are drawn on both sides of the vendored catalog
constraints and of the per-type scheme rule (validator rule V11).
"""
import copy

from hypothesis import given
from hypothesis import strategies as st

from camera_sync import CameraSourceState
from workflow_engine.camera_binding import (
    STATUS_INVALID,
    STATUS_RESOLVED,
    resolve_bindings,
)

# --- generators --------------------------------------------------------------

_PROTOCOLS = ("rtsp", "rtmp")
_NODE_TYPE = {"rtsp": "rtsp_camera_source", "rtmp": "rtmp_stream_source"}
_SOURCE_TYPE = {"rtsp": "RTSP", "rtmp": "RTMP"}
_OTHER = {"rtsp": "rtmp", "rtmp": "rtsp"}


def _url(protocol, index, secure=False):
    scheme = protocol + ("s" if secure else "")
    if protocol == "rtsp":
        return "{0}://192.168.1.{1}:554/Streaming/Channels/101".format(scheme, 10 + index)
    return "{0}://media.local/live/line{1}".format(scheme, index)


@st.composite
def _stream_params(draw, protocol, index):
    """A stream entry's reported params: the URL, some settings and the
    device-managed credential markers."""
    params = {"url": _url(protocol, index, draw(st.booleans()))}
    if protocol == "rtsp" and draw(st.booleans()):
        params["transport"] = draw(st.sampled_from(["tcp", "udp", "auto"]))
    if draw(st.booleans()):
        params["decoder"] = draw(st.sampled_from(["auto", "hardware", "software"]))
    params["credentialsConfigured"] = draw(st.booleans())
    if params["credentialsConfigured"] and draw(st.booleans()):
        params["credentialRef"] = {"secretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:c",
                                   "versionId": "v{0}".format(index)}
    return params


@st.composite
def _inventories(draw):
    """Stream entries of both types plus the other camera families."""
    entries = []
    for index in range(draw(st.integers(min_value=0, max_value=4))):
        protocol = draw(st.sampled_from(_PROTOCOLS))
        entries.append(CameraSourceState(
            camera_source_id="cfg-st-{0}".format(index),
            name="Stream {0}".format(index),
            type=_SOURCE_TYPE[protocol],
            origin="edge-configured",
            params=draw(_stream_params(protocol, index)),
            capabilities={"stream": {"state": "idle", "codec": None, "width": None,
                                     "height": None, "decoder": None}},
        ))
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        entries.append(CameraSourceState(
            camera_source_id="cfg-cam-{0}".format(index),
            name="USB {0}".format(index),
            type="Camera",
            origin="edge-configured",
            params={"devicePath": "/dev/video{0}".format(index)},
        ))
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        entries.append(CameraSourceState(
            camera_source_id="arv-{0:012x}".format(index),
            name="Vendor {0}".format(index),
            type="AravisDiscovered",
            origin="edge-discovered",
            params={"cameraId": "Basler-{0}".format(index), "serial": "SN{0}".format(index)},
        ))
    return entries


@st.composite
def _valid_stream_overrides(draw, protocol):
    """Non-empty overrides every catalog constraint and V11 accept."""
    override = {}
    if draw(st.booleans()):
        override["url"] = _url(protocol, draw(st.integers(min_value=0, max_value=50)), draw(st.booleans()))
    if draw(st.booleans()):
        override["processing_mode"] = draw(st.sampled_from(["continuous", "on_trigger"]))
    if draw(st.booleans()):
        override["frames_per_second"] = draw(st.floats(min_value=0.05, max_value=10.0))
    if draw(st.booleans()):
        override["max_frame_age_ms"] = draw(st.integers(min_value=100, max_value=60000))
    if draw(st.booleans()):
        override["keep_recent_runs"] = draw(st.integers(min_value=1, max_value=200))
    if not override or draw(st.booleans()):
        override["keep_notable_runs"] = draw(st.integers(min_value=0, max_value=5000))
    return override


@st.composite
def _invalid_stream_overrides(draw, protocol):
    """Overrides violating exactly one rule: a catalog constraint, or the
    node type's scheme and secret-free URL rules that only V11 states."""
    kind = draw(st.sampled_from([
        "foreign-scheme", "user-info", "other-protocol", "secret-query",
        "fps-high", "fps-low", "age-low", "mode", "undeclared"]))
    if kind == "foreign-scheme":
        return {"url": "http://192.168.1.9/stream"}
    if kind == "user-info":
        return {"url": "{0}://admin:hunter22@192.168.1.9/stream".format(protocol)}
    if kind == "other-protocol":
        return {"url": _url(_OTHER[protocol], 3)}
    if kind == "secret-query":
        return {"url": "{0}://192.168.1.9/live?token=s3cr3tvalue".format(protocol)}
    if kind == "fps-high":
        return {"frames_per_second": draw(st.floats(min_value=10.5, max_value=1e6))}
    if kind == "fps-low":
        return {"frames_per_second": draw(st.floats(min_value=0.0, max_value=0.04))}
    if kind == "age-low":
        return {"max_frame_age_ms": draw(st.integers(min_value=-5, max_value=99))}
    if kind == "mode":
        return {"processing_mode": "sometimes"}
    return {"bogus": 1}


@st.composite
def _cases(draw):
    """A document mixing stream points with V4L2 slot and Aravis points,
    bindings for each, an inventory, and each point's variant."""
    inventory = draw(_inventories())
    stream_entries = [entry for entry in inventory if entry.type in ("RTSP", "RTMP")]

    elements = [{"nodeId": "v{0}".format(index), "factory": "v4l2src",
                 "args": {"device": "/dev/video99"}} for index in range(2)]
    document = {"schemaVersion": 1, "segments": [{"name": "s0", "elements": elements}],
                "bindingPoints": []}

    variants = {}
    bindings = {}
    points = []
    for index in range(draw(st.integers(min_value=1, max_value=3))):
        protocol = draw(st.sampled_from(_PROTOCOLS))
        node_id = "stream{0}".format(index)
        point = {
            "nodeId": node_id,
            "nodeType": _NODE_TYPE[protocol],
            "parameters": {"url": _url(protocol, 40 + index), "processing_mode": "continuous",
                           "frames_per_second": 1.0, "max_frame_age_ms": 2000,
                           "keep_recent_runs": 20, "keep_notable_runs": 200},
            "slots": [],
            "streamBinding": True,
        }
        if draw(st.booleans()):
            point["streamProtocol"] = protocol
        points.append(point)

        matching = [entry for entry in stream_entries if entry.type == _SOURCE_TYPE[protocol]]
        mismatched = [entry for entry in inventory if entry.type != _SOURCE_TYPE[protocol]]
        options = ["unbound", "missing", "override-valid", "override-invalid"]
        if matching:
            options += ["present", "present"]
        if mismatched:
            options.append("mismatched")
        variant = draw(st.sampled_from(options))
        variants[node_id] = (variant, protocol)
        if variant == "present":
            bindings[node_id] = {"cameraSourceId": draw(st.sampled_from(matching)).camera_source_id}
        elif variant == "mismatched":
            bindings[node_id] = {"cameraSourceId": draw(st.sampled_from(mismatched)).camera_source_id}
        elif variant == "missing":
            bindings[node_id] = {"cameraSourceId": "cfg-gone-{0}".format(index)}
        elif variant == "override-valid":
            bindings[node_id] = {"override": draw(_valid_stream_overrides(protocol))}
        elif variant == "override-invalid":
            bindings[node_id] = {"override": draw(_invalid_stream_overrides(protocol))}

    # Non-stream points: V4L2 slot points and an Aravis point.
    v4l2_entries = [entry for entry in inventory if entry.type == "Camera"]
    for index in range(draw(st.integers(min_value=0, max_value=2))):
        node_id = "v{0}".format(index)
        points.append({
            "nodeId": node_id, "nodeType": "camera_source",
            "parameters": {"device": "/dev/video99"},
            "slots": [{"param": "device", "segment": 0, "element": index, "arg": "device"}],
        })
        choice = draw(st.sampled_from(["unbound", "missing"] + (["present"] if v4l2_entries else [])))
        if choice == "present":
            bindings[node_id] = {"cameraSourceId": draw(st.sampled_from(v4l2_entries)).camera_source_id}
        elif choice == "missing":
            bindings[node_id] = {"cameraSourceId": "cfg-gone-{0}".format(node_id)}
    aravis_entries = [entry for entry in inventory if entry.type == "AravisDiscovered"]
    if draw(st.booleans()):
        points.append({
            "nodeId": "arv", "nodeType": "aravis_camera_source",
            "parameters": {"camera_id": "Aravis-Fake-GV01", "gain": 4, "exposure": 5000},
            "slots": [], "aravisBinding": True,
        })
        if aravis_entries and draw(st.booleans()):
            bindings["arv"] = {"cameraSourceId": draw(st.sampled_from(aravis_entries)).camera_source_id}
        elif draw(st.booleans()):
            bindings["arv"] = {"override": {"gain": 250}}

    document["bindingPoints"] = draw(st.permutations(points))
    return document, bindings, inventory, variants


def _without_stream_points(document, bindings):
    """The same document and bindings, minus every stream point: the
    pre-feature input of the non-stream points."""
    stripped = copy.deepcopy(document)
    stripped["bindingPoints"] = [point for point in stripped["bindingPoints"]
                                 if point.get("streamBinding") is not True]
    stream_ids = {point["nodeId"] for point in document["bindingPoints"]
                  if point.get("streamBinding") is True}
    return stripped, {key: value for key, value in bindings.items() if key not in stream_ids}


def _model_values(entry):
    """An entry's resolved values: every non-None param as reported (a
    stream entry has no parameter aliases or camera identity)."""
    return {key: value for key, value in entry.params.items() if value is not None}


# --- property ----------------------------------------------------------------


@given(case=_cases())
def test_device_side_stream_binding_resolution(case):
    """**Feature: rtsp-rtmp-stream-cameras, Property 14: Device-side
    stream binding resolution**

    **Validates: Requirements 10.1, 10.2**
    """
    document, bindings, inventory, variants = case
    snapshot = copy.deepcopy(document)
    by_id = {entry.camera_source_id: entry for entry in inventory}

    result = resolve_bindings(document, bindings, inventory)
    assert document == snapshot

    stream_missing = []
    stream_failures = []
    expected_assignments = {}
    for node_id, (variant, protocol) in variants.items():
        binding = bindings.get(node_id)
        if variant == "present":
            expected_assignments[node_id] = {
                "cameraSourceId": binding["cameraSourceId"],
                "params": _model_values(by_id[binding["cameraSourceId"]]),
            }
        elif variant == "override-valid":
            expected_assignments[node_id] = {"cameraSourceId": None, "params": dict(binding["override"])}
        elif variant in ("missing", "mismatched"):
            stream_missing.append({"nodeId": node_id, "cameraSourceId": binding["cameraSourceId"]})
            # 10.2: the reason names the camera the binding asked for.
            assert any(binding["cameraSourceId"] in error for error in result.errors)
            stream_failures.append(node_id)
        elif variant == "override-invalid":
            assert any("'{0}'".format(node_id) in error for error in result.errors), result.errors
            stream_failures.append(node_id)

    # 10.1: exactly the matching and valid-override stream points yield
    # stream assignments, and they never reach another assignment family.
    assert result.stream_assignments == expected_assignments
    for family in (result.aravis_assignments, result.adapter_assignments, result.csi_assignments):
        assert not set(family) & set(variants)

    # Non-stream points resolve exactly as they did without the stream
    # points, and stream points never substitute a slot.
    stripped, stripped_bindings = _without_stream_points(document, bindings)
    before = resolve_bindings(stripped, stripped_bindings, inventory)
    assert result.document["segments"] == before.document["segments"]
    assert result.aravis_assignments == before.aravis_assignments
    assert result.adapter_assignments == before.adapter_assignments
    assert result.csi_assignments == before.csi_assignments
    assert [item for item in result.missing if item["nodeId"] not in variants] == list(before.missing)
    for error in before.errors:
        assert error in result.errors

    # The missing list reports stream points in document order.
    order = [point["nodeId"] for point in document["bindingPoints"]]
    assert [item for item in result.missing if item["nodeId"] in variants] == sorted(
        stream_missing, key=lambda item: order.index(item["nodeId"]))

    # An override's credential never reaches the invalid-registration
    # reason (it is logged and served by the API).
    reasons = " ".join(result.errors)
    assert "hunter22" not in reasons and "s3cr3tvalue" not in reasons

    if stream_failures or before.status == STATUS_INVALID:
        assert result.status == STATUS_INVALID
    else:
        assert result.status == STATUS_RESOLVED
        assert result.errors == ()
