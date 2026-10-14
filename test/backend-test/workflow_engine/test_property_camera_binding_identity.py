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
"""Property test for the no-binding identity of ``resolve_bindings``.

**Feature: camera-registry-sync, Property 22: No-binding identity**

*For any* compiled document — with or without binding points, including
documents packaged before this feature — resolving with no bindings
supplied returns the document unchanged, so execution uses exactly the
compiled-in parameter values.

**Validates: Requirements 10.5, 11.1, 11.5**

Three faces of the identity, split across one test each:

- A legacy document lacking ``bindingPoints`` entirely (every component
  packaged before this feature) is returned as the same object, resolved,
  for *any* bindings and *any* local inventory (11.1, 11.5).
- A document *with* binding points resolved with no bindings supplied
  (``None`` or ``{}``) is likewise returned as the same object, resolved
  (10.5).
- Binding points left unbound by a bindings map that only covers other
  node ids keep every rendered default: the resolved document is deeply
  equal to the input (10.5).

Generators mirror the real input space of the Property 20 test in
``test_property_camera_binding_resolution.py``: rendered segments whose
element args carry distinct defaults, packager-shaped binding points with
disjoint slots (JP4/JP5 points ``adapterBinding: true`` with empty
slots), and ``build_inventory``-shaped local inventories. Legacy
documents additionally cover degenerate shapes (no segments, extra
top-level keys, ``bindingPoints`` present but empty).

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import copy

from hypothesis import given
from hypothesis import strategies as st

from camera_sync import CameraSourceState
from workflow_engine.camera_binding import STATUS_RESOLVED, resolve_bindings

# --- generators --------------------------------------------------------------

#: The camera_source parameters the packager declares slots for.
_PARAMS = ("device", "gain", "exposure")

_DEVICE_PATHS = st.integers(min_value=0, max_value=9).map(
    "/dev/video{}".format
)

_VALID_GAIN = st.integers(min_value=0, max_value=100)
_VALID_EXPOSURE = st.integers(min_value=0, max_value=10_000_000)


@st.composite
def _inventories(draw):
    """Local Camera_Source inventories in the ``build_inventory`` shape."""
    entries = []
    for index in range(draw(st.integers(min_value=0, max_value=3))):
        params = {
            "devicePath": draw(_DEVICE_PATHS),
            "cameraId": "cam-{}".format(index),
        }
        if draw(st.booleans()):
            params["gain"] = draw(_VALID_GAIN)
        if draw(st.booleans()):
            params["exposure"] = draw(_VALID_EXPOSURE)
        entries.append(
            CameraSourceState(
                camera_source_id="cfg-is-{}".format(index),
                name="Camera {}".format(index),
                type=draw(st.sampled_from(["Camera", "ICam", "NvidiaCSI"])),
                origin="edge-configured",
                params=params,
            )
        )
    return entries


@st.composite
def _segments(draw, min_segments=1):
    """Rendered pipeline segments whose element args carry distinct
    compiled-in defaults for every parameter a slot could address."""
    segments = []
    for s in range(draw(st.integers(min_value=min_segments, max_value=3))):
        elements = []
        for e in range(draw(st.integers(min_value=1, max_value=3))):
            elements.append({
                "type": "v4l2src",
                "args": {
                    "device": "/dev/default-{}-{}".format(s, e),
                    "gain": draw(_VALID_GAIN),
                    "exposure": draw(_VALID_EXPOSURE),
                    "extra": "keep-{}-{}".format(s, e),
                },
            })
        segments.append({"elements": elements})
    return segments


@st.composite
def _legacy_documents(draw):
    """Compiled documents packaged before this feature: no
    ``bindingPoints`` key at all, or (from a hypothetical no-camera
    packager run) an empty ``bindingPoints`` list. Degenerate shapes —
    no segments, extra top-level keys — are drawn too."""
    document = {"schemaVersion": 1,
                "segments": draw(_segments(min_segments=0))}
    if draw(st.booleans()):
        document["metadata"] = {"workflowId": "wf-1", "version": "3"}
    if draw(st.booleans()):
        document["bindingPoints"] = []
    return document


@st.composite
def _documents_with_binding_points(draw):
    """Packager-shaped documents carrying at least one binding point with
    disjoint slots addressing real segment/element/arg positions; JP4/JP5
    points are ``adapterBinding: true`` with empty slots."""
    segments = draw(_segments())
    document = {"schemaVersion": 1, "segments": segments}

    target_pool = draw(st.permutations([
        (s, e, param)
        for s, segment in enumerate(segments)
        for e in range(len(segment["elements"]))
        for param in _PARAMS
    ]))
    target_pool = list(target_pool)

    binding_points = []
    for index in range(draw(st.integers(min_value=1, max_value=4))):
        point = {"nodeId": "n{}".format(index), "nodeType": "camera_source"}
        if draw(st.booleans()):
            point["adapterBinding"] = True
            point["slots"] = []
        else:
            slots = []
            for _ in range(draw(st.integers(
                    min_value=0, max_value=min(3, len(target_pool))))):
                s, e, param = target_pool.pop()
                slots.append({"param": param, "segment": s,
                              "element": e, "arg": param})
            point["slots"] = slots
        binding_points.append(point)
    document["bindingPoints"] = binding_points
    return document


#: A single Camera_Binding entry of either delivered shape.
_BINDING_VALUES = st.one_of(
    st.integers(min_value=0, max_value=5).map(
        lambda i: {"cameraSourceId": "cfg-is-{}".format(i)}),
    st.dictionaries(st.sampled_from(_PARAMS), _VALID_GAIN,
                    min_size=1, max_size=2).map(
        lambda values: {"override": values}),
)

#: Any bindings map, empty through several entries, arbitrary node ids.
_ANY_BINDINGS = st.dictionaries(
    st.sampled_from(["n0", "n1", "n2", "other-a", "other-b"]),
    _BINDING_VALUES, max_size=4)


# --- properties ---------------------------------------------------------------


@given(document=_legacy_documents(),
       bindings=st.one_of(st.none(), _ANY_BINDINGS),
       inventory=_inventories())
def test_legacy_document_without_binding_points_is_identity(
        document, bindings, inventory):
    """**Feature: camera-registry-sync, Property 22: No-binding identity**

    **Validates: Requirements 10.5, 11.1, 11.5**

    A pre-feature document (no ``bindingPoints``) is returned as the very
    same object, resolved, for any bindings and any inventory — the
    compiled-in parameter values run exactly as before this feature.
    """
    snapshot = copy.deepcopy(document)

    result = resolve_bindings(document, bindings, inventory)

    assert result.document is document
    assert document == snapshot  # never mutated
    assert result.status == STATUS_RESOLVED
    assert result.missing == ()
    assert result.adapter_assignments == {}
    assert result.errors == ()


@given(document=_documents_with_binding_points(),
       bindings=st.sampled_from([None, {}]),
       inventory=_inventories())
def test_no_bindings_supplied_is_identity(document, bindings, inventory):
    """**Feature: camera-registry-sync, Property 22: No-binding identity**

    **Validates: Requirements 10.5, 11.1, 11.5**

    A document with binding points resolved with no bindings supplied
    (``None`` or ``{}``) is returned as the very same object, resolved,
    with nothing recorded.
    """
    snapshot = copy.deepcopy(document)

    result = resolve_bindings(document, bindings, inventory)

    assert result.document is document
    assert document == snapshot  # never mutated
    assert result.status == STATUS_RESOLVED
    assert result.missing == ()
    assert result.adapter_assignments == {}
    assert result.errors == ()


@given(document=_documents_with_binding_points(),
       stray_bindings=st.dictionaries(
           st.sampled_from(["other-a", "other-b", "other-c"]),
           _BINDING_VALUES, min_size=1, max_size=3),
       inventory=_inventories())
def test_unbound_points_keep_every_rendered_default(
        document, stray_bindings, inventory):
    """**Feature: camera-registry-sync, Property 22: No-binding identity**

    **Validates: Requirements 10.5, 11.1, 11.5**

    Binding points left unbound by a bindings map that only covers other
    node ids keep every rendered default: the resolved document is deeply
    equal to the input and the resolution is clean.
    """
    # Every binding key misses every binding point by construction.
    point_ids = {point["nodeId"] for point in document["bindingPoints"]}
    assert point_ids.isdisjoint(stray_bindings)

    snapshot = copy.deepcopy(document)

    result = resolve_bindings(document, stray_bindings, inventory)

    assert result.document == snapshot  # every rendered default kept
    assert document == snapshot  # input never mutated
    assert result.status == STATUS_RESOLVED
    assert result.missing == ()
    assert result.adapter_assignments == {}
    assert result.errors == ()
