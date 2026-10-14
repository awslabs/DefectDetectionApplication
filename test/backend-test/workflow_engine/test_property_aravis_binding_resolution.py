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
"""Property test for device-side Aravis binding resolution.

**Feature: aravis-camera-input, Property 13: Device-side Aravis binding
resolution**

*For any* compiled document with Aravis binding points, binding map, and
local inventory: a ``cameraSourceId`` binding matching an inventory entry
yields an Aravis assignment whose ``camera_id`` is the entry's camera id
and leaves the document's segments unchanged; a constraint-valid override
binding yields an assignment from the override values; a
``cameraSourceId`` with no inventory entry marks the resolution invalid
recording the missing id; and a constraint-violating override marks the
resolution invalid with a reason.

**Validates: Requirements 6.1, 6.2, 6.3**

Generators mirror the real input space: the packager emits one
``aravisBinding: true`` binding point per Aravis_Camera_Source_Node with
empty slots and rendered ``camera_id``/``gain``/``exposure`` parameters;
the local inventory is the ``build_inventory`` merge shape, where an
Aravis-backed entry's params carry ``cameraId`` (discovered-only
``AravisDiscovered`` entries with serial/protocol/address, configured
``Camera`` entries with optional gain/exposure). Overrides are drawn on
both sides of the vendored aravis_camera_source catalog constraints
(camera_id ``min_length: 1``, gain ``0-100``, exposure ``min: 0``).

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

_VALID_GAIN = st.integers(min_value=0, max_value=100)
_VALID_EXPOSURE = st.integers(min_value=0, max_value=10_000_000)

#: Aravis runtime camera ids as camera_manager connects by.
_CAMERA_IDS = st.sampled_from(
    ["Aravis-Fake-GV01", "Basler-12345678", "Allied-9A3F", "Lucid-224400"]
)


@st.composite
def _inventories(draw):
    """Local Camera_Source inventories in the ``build_inventory`` shape:
    unique stable ids, ``cameraId`` always present (the Aravis identity
    every Aravis-backed entry carries), never a literal ``camera_id`` key
    and never ``None`` values. Entries are either discovered-only
    ``AravisDiscovered`` or configured ``Camera`` merges."""
    entries = []
    for index in range(draw(st.integers(min_value=0, max_value=3))):
        camera_id = "{0}-{1}".format(draw(_CAMERA_IDS), index)
        if draw(st.booleans()):
            # Discovered-only Aravis entry (identity params from discovery).
            entries.append(
                CameraSourceState(
                    camera_source_id="arv-{:012x}".format(index),
                    name="Vendor Model {}".format(index),
                    type="AravisDiscovered",
                    origin="edge-discovered",
                    params={
                        "cameraId": camera_id,
                        "serial": "SN{:04d}".format(index),
                        "protocol": draw(st.sampled_from(
                            ["GigEVision", "USB3Vision", "Fake"])),
                        "address": "192.168.1.{}".format(10 + index),
                    },
                )
            )
        else:
            # Configured Camera-type Image_Source merged by cameraId.
            params = {"cameraId": camera_id}
            if draw(st.booleans()):
                params["gain"] = draw(_VALID_GAIN)
            if draw(st.booleans()):
                params["exposure"] = draw(_VALID_EXPOSURE)
            entries.append(
                CameraSourceState(
                    camera_source_id="cfg-is-{}".format(index),
                    name="Camera {}".format(index),
                    type="Camera",
                    origin="edge-configured",
                    params=params,
                )
            )
    return entries


@st.composite
def _valid_overrides(draw):
    """Non-empty overrides satisfying every aravis_camera_source
    constraint (camera_id min_length 1, gain 0-100, exposure >= 0)."""
    override = {}
    if draw(st.booleans()):
        override["camera_id"] = draw(_CAMERA_IDS)
    if draw(st.booleans()):
        override["gain"] = draw(_VALID_GAIN)
    if not override or draw(st.booleans()):
        override["exposure"] = draw(_VALID_EXPOSURE)
    return override


@st.composite
def _invalid_overrides(draw):
    """Overrides violating exactly one aravis_camera_source catalog
    constraint."""
    kind = draw(st.sampled_from(
        ["empty-camera-id", "gain-high", "gain-negative",
         "exposure-negative", "undeclared"]))
    if kind == "empty-camera-id":
        return {"camera_id": ""}
    if kind == "gain-high":
        return {"gain": draw(st.integers(min_value=101, max_value=10**6))}
    if kind == "gain-negative":
        return {"gain": draw(st.integers(min_value=-(10**6), max_value=-1))}
    if kind == "exposure-negative":
        return {"exposure": draw(st.integers(min_value=-(10**6), max_value=-1))}
    return {"bogus": draw(st.integers(min_value=0, max_value=10))}


@st.composite
def _resolution_cases(draw):
    """A compiled document with Aravis binding points, a bindings map, a
    local inventory, and the per-node binding variant used to model the
    expected outcome."""
    inventory = draw(_inventories())

    # Document skeleton: rendered segments/elements. Aravis binding
    # points declare no slots, so every arg must survive resolution
    # unchanged.
    segments = []
    for s in range(draw(st.integers(min_value=1, max_value=3))):
        elements = []
        for e in range(draw(st.integers(min_value=1, max_value=3))):
            elements.append({
                "type": "appsrc" if e == 0 else "videoconvert",
                "args": {
                    "name": "appsrc_n{}-{}".format(s, e),
                    "extra": "keep-{}-{}".format(s, e),
                },
            })
        segments.append({"elements": elements})
    document = {"schemaVersion": 1, "segments": segments}

    # Aravis binding points: unique node ids, aravisBinding true, empty
    # slots, rendered camera_id/gain/exposure parameters (the packager's
    # defaults-overlaid values).
    binding_points = []
    for index in range(draw(st.integers(min_value=1, max_value=4))):
        binding_points.append({
            "nodeId": "n{}".format(index),
            "nodeType": "aravis_camera_source",
            "aravisBinding": True,
            "slots": [],
            "parameters": {
                "camera_id": draw(_CAMERA_IDS),
                "gain": draw(_VALID_GAIN),
                "exposure": draw(_VALID_EXPOSURE),
            },
        })
    document["bindingPoints"] = binding_points

    # Bindings: each Aravis point independently unbound, bound to a
    # present inventory id, bound to a missing id, or overridden with
    # valid or constraint-violating values.
    variant_options = ["unbound", "missing",
                       "override-valid", "override-invalid"]
    if inventory:
        variant_options += ["present", "present"]  # weight toward 6.1

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
                "cameraSourceId": "arv-gone-{}".format(node_id)}
        elif variant == "override-valid":
            bindings[node_id] = {"override": draw(_valid_overrides())}
        else:
            bindings[node_id] = {"override": draw(_invalid_overrides())}

    # A stray binding for a node with no binding point must have no effect.
    if draw(st.booleans()):
        bindings["stray-node"] = {"cameraSourceId": "arv-gone-stray"}

    return document, bindings, inventory, variants


# --- expected-outcome model ---------------------------------------------------


def _model_resolved_values(entry):
    """Requirement 6.1: the bound source's resolved parameter values,
    with the reported ``cameraId`` resolving the node's ``camera_id``
    parameter through ``_PARAM_ALIASES``. Exact because the inventory
    generator never emits ``None`` values or a literal ``camera_id``
    key."""
    values = dict(entry.params)
    values["camera_id"] = values["cameraId"]
    return values


# --- property ----------------------------------------------------------------


@given(case=_resolution_cases())
def test_device_side_aravis_binding_resolution(case):
    """**Feature: aravis-camera-input, Property 13: Device-side Aravis
    binding resolution**

    **Validates: Requirements 6.1, 6.2, 6.3**
    """
    document, bindings, inventory, variants = case
    snapshot = copy.deepcopy(document)
    inventory_by_id = {entry.camera_source_id: entry for entry in inventory}

    result = resolve_bindings(document, bindings, inventory)

    # The input document is never mutated.
    assert document == snapshot

    # 6.1: Aravis binding points never substitute element arguments —
    # the resolved document's segments are unchanged (and so is the
    # whole document, since Aravis points declare no slots).
    assert result.document["segments"] == snapshot["segments"]
    assert result.document == snapshot

    # Model the expected outcome per binding point, in document order.
    expected_missing = []
    expected_missing_errors = []
    invalid_override_nodes = []
    expected_assignments = {}

    for point in snapshot["bindingPoints"]:
        node_id = point["nodeId"]
        variant = variants[node_id]
        if variant == "unbound":
            continue  # unbound point: rendered parameters run as-is
        binding = bindings[node_id]

        if variant == "missing":
            # 6.3: absent id -> invalid with the missing source recorded.
            camera_source_id = binding["cameraSourceId"]
            expected_missing.append(
                {"nodeId": node_id, "cameraSourceId": camera_source_id})
            expected_missing_errors.append(
                "missing camera source {}".format(camera_source_id))
            continue
        if variant == "override-invalid":
            # 6.2: constraint-violating override -> invalid with a reason.
            invalid_override_nodes.append(node_id)
            continue

        if variant == "present":
            # 6.1: the entry's resolved values; camera_id is the entry's
            # Aravis camera id.
            camera_source_id = binding["cameraSourceId"]
            values = _model_resolved_values(inventory_by_id[camera_source_id])
        else:  # override-valid, 6.2: the override values, no lookup.
            camera_source_id = None
            values = dict(binding["override"])

        expected_assignments[node_id] = {
            "cameraSourceId": camera_source_id, "params": values}

    # 6.1 / 6.2: exactly the resolved and overridden Aravis points yield
    # assignments, and a cameraSourceId assignment's camera_id is the
    # bound entry's camera id.
    assert result.aravis_assignments == expected_assignments
    for node_id, variant in variants.items():
        if variant == "present":
            entry = inventory_by_id[bindings[node_id]["cameraSourceId"]]
            assignment = result.aravis_assignments[node_id]
            assert assignment["params"]["camera_id"] == entry.params["cameraId"]

    # Aravis points never contribute adapter assignments.
    assert result.adapter_assignments == {}

    # 6.3: every missing id reported, in binding-point order, with a
    # reason identifying the missing Camera_Source.
    assert result.missing == tuple(expected_missing)
    for error in expected_missing_errors:
        assert error in result.errors

    # 6.2: each constraint-violating override is reported against its node.
    for node_id in invalid_override_nodes:
        assert any("'{}'".format(node_id) in error for error in result.errors)

    # Invalid exactly when something failed to resolve; a resolved result
    # carries no errors and the registration is runnable.
    if expected_missing or invalid_override_nodes:
        assert result.status == STATUS_INVALID
    else:
        assert result.status == STATUS_RESOLVED
        assert result.errors == ()
