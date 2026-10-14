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
"""Property test for the pure ``resolve_bindings`` device-side resolver.

**Feature: camera-registry-sync, Property 20: Device-side binding resolution**

*For any* compiled document with binding points, any bindings, and any
local inventory: bindings referencing a ``cameraSourceId`` present in the
inventory substitute that source's parameter values into exactly the
declared slots; override bindings substitute the override values
regardless of inventory; and any binding referencing an id absent from
the inventory yields status invalid with a reason identifying the missing
Camera_Source, leaving the registration non-runnable.

**Validates: Requirements 10.1, 10.2, 10.3**

Generators mirror the real input space: the packager emits one binding
point per Camera_Input_Node with slots addressing real
segment/element/arg positions (disjoint across nodes), JP4/JP5 points are
``adapterBinding: true`` with empty slots, and the local inventory is the
``build_inventory`` merge shape (``devicePath`` plus optional
``gain``/``exposure``). Overrides are drawn on both sides of the vendored
camera_source catalog constraints (device ``min_length: 1``, gain
``0-100``, exposure ``min: 0``).

Runs with the hypothesis profiles registered in this directory's conftest
(``engine-fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
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

#: The icam_source parameter the packager declares a slot for
#: (csi-icam-input-nodes: the generic camera_source was removed, so the
#: sole slot-bearing built-in camera input is icam_source's device path).
_PARAMS = ("device",)

_DEVICE_PATHS = st.integers(min_value=0, max_value=9).map(
    "/dev/video{}".format
)

_VALID_GAIN = st.integers(min_value=0, max_value=100)
_VALID_EXPOSURE = st.integers(min_value=0, max_value=10_000_000)


@st.composite
def _inventories(draw):
    """Local Camera_Source inventories in the ``build_inventory`` shape:
    unique stable ids, ``devicePath`` always present, ``gain``/``exposure``
    sometimes, never a literal ``device`` key and never ``None`` values."""
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
def _valid_overrides(draw):
    """Non-empty overrides satisfying every icam_source constraint (device
    ``min_length: 1``)."""
    return {"device": draw(_DEVICE_PATHS)}


@st.composite
def _invalid_overrides(draw):
    """Overrides violating exactly one icam_source catalog constraint: an
    empty device path (``min_length: 1``) or an undeclared parameter."""
    kind = draw(st.sampled_from(["empty-device", "undeclared"]))
    if kind == "empty-device":
        return {"device": ""}
    return {"bogus": draw(st.integers(min_value=0, max_value=10))}


@st.composite
def _resolution_cases(draw):
    """A compiled document with binding points, a bindings map, a local
    inventory, and the per-node binding variant used to model the
    expected outcome."""
    inventory = draw(_inventories())

    # Document skeleton: rendered segments/elements whose args carry
    # distinct defaults for every parameter a slot could address.
    segments = []
    for s in range(draw(st.integers(min_value=1, max_value=3))):
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
    document = {"schemaVersion": 1, "segments": segments}

    # Binding points: unique node ids; slot targets drawn without
    # replacement from every (segment, element, param) position so each
    # slot addresses a real arg and no two slots collide (the packager
    # emits disjoint slots per node).
    target_pool = draw(st.permutations([
        (s, e, param)
        for s, segment in enumerate(segments)
        for e in range(len(segment["elements"]))
        for param in _PARAMS
    ]))
    target_pool = list(target_pool)

    binding_points = []
    for index in range(draw(st.integers(min_value=1, max_value=4))):
        point = {"nodeId": "n{}".format(index), "nodeType": "icam_source"}
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

    # Bindings: each binding point independently unbound, bound to a
    # present inventory id, bound to a missing id, or overridden with
    # valid or constraint-violating values.
    variant_options = ["unbound", "missing",
                       "override-valid", "override-invalid"]
    if inventory:
        variant_options += ["present", "present"]  # weight toward 10.1

    bindings = {}
    variants = {}
    for point in binding_points:
        node_id = point["nodeId"]
        variant = draw(st.sampled_from(variant_options))
        variants[node_id] = variant
        if variant == "unbound":
            continue
        if variant == "present":
            entry = draw(st.sampled_from(inventory))
            bindings[node_id] = {"cameraSourceId": entry.camera_source_id}
        elif variant == "missing":
            bindings[node_id] = {
                "cameraSourceId": "cfg-gone-{}".format(node_id)}
        elif variant == "override-valid":
            bindings[node_id] = {"override": draw(_valid_overrides())}
        else:
            bindings[node_id] = {"override": draw(_invalid_overrides())}

    # A stray binding for a node with no binding point must have no effect.
    if draw(st.booleans()):
        bindings["stray-node"] = {"cameraSourceId": "cfg-gone-stray"}

    return document, bindings, inventory, variants


# --- expected-outcome model ---------------------------------------------------


def _model_resolved_values(entry):
    """Requirement 10.1: the bound source's resolved parameter values,
    with the reported ``devicePath`` resolving the node's ``device``
    parameter (and ``cameraId`` aliasing ``camera_id``, the
    aravis-camera-input addition to ``_PARAM_ALIASES``). Exact because
    the inventory generator never emits ``None`` values or a literal
    ``device``/``camera_id`` key."""
    values = dict(entry.params)
    values["device"] = values["devicePath"]
    values["camera_id"] = values["cameraId"]
    return values


# --- property ----------------------------------------------------------------


@given(case=_resolution_cases())
def test_device_side_binding_resolution(case):
    """**Feature: camera-registry-sync, Property 20: Device-side binding
    resolution**

    **Validates: Requirements 10.1, 10.2, 10.3**
    """
    document, bindings, inventory, variants = case
    snapshot = copy.deepcopy(document)
    inventory_by_id = {entry.camera_source_id: entry for entry in inventory}

    result = resolve_bindings(document, bindings, inventory)

    # The input document is never mutated.
    assert document == snapshot

    # Model the expected outcome per binding point, in document order.
    expected_document = copy.deepcopy(snapshot)
    expected_missing = []
    expected_missing_errors = []
    invalid_override_nodes = []
    expected_assignments = {}

    for point in snapshot["bindingPoints"]:
        node_id = point["nodeId"]
        variant = variants[node_id]
        if variant == "unbound":
            continue  # untouched point keeps its rendered defaults
        binding = bindings[node_id]

        if variant == "missing":
            # 10.2: absent id -> invalid with the missing source recorded.
            camera_source_id = binding["cameraSourceId"]
            expected_missing.append(
                {"nodeId": node_id, "cameraSourceId": camera_source_id})
            expected_missing_errors.append(
                "missing camera source {}".format(camera_source_id))
            continue
        if variant == "override-invalid":
            # 10.3: constraint-violating override -> invalid, no change.
            invalid_override_nodes.append(node_id)
            continue

        if variant == "present":
            # 10.1: the source's resolved values.
            camera_source_id = binding["cameraSourceId"]
            values = _model_resolved_values(inventory_by_id[camera_source_id])
        else:  # override-valid, 10.3: the override values, no lookup.
            camera_source_id = None
            values = dict(binding["override"])

        if point.get("adapterBinding") is True:
            # Adapter points assign, never touch the document.
            expected_assignments[node_id] = {
                "cameraSourceId": camera_source_id, "params": values}
        else:
            # Substitution lands in exactly the declared slots; slots
            # whose parameter has no resolved value keep their defaults.
            for slot in point["slots"]:
                if slot["param"] in values:
                    element = expected_document["segments"][
                        slot["segment"]]["elements"][slot["element"]]
                    element["args"][slot["arg"]] = values[slot["param"]]

    # Exactly the declared slots changed: everything else (elements, args,
    # unbound points, missing/invalid points) keeps its rendered defaults.
    assert result.document == expected_document

    # 10.2: every missing id reported, in binding-point order, with a
    # reason identifying the missing Camera_Source.
    assert result.missing == tuple(expected_missing)
    for error in expected_missing_errors:
        assert error in result.errors

    # 10.3: each constraint-violating override is reported against its node.
    for node_id in invalid_override_nodes:
        assert any("'{}'".format(node_id) in error for error in result.errors)

    # Invalid exactly when something failed to resolve; a resolved result
    # carries no errors and the registration is runnable.
    if expected_missing or invalid_override_nodes:
        assert result.status == STATUS_INVALID
    else:
        assert result.status == STATUS_RESOLVED
        assert result.errors == ()

    # Adapter points produce assignments without document changes.
    assert result.adapter_assignments == expected_assignments
