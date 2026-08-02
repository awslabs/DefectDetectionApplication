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
"""Device-side Camera_Binding resolution (camera-registry-sync
Requirements 10.1, 10.3, 10.5, 11.1).

``resolve_bindings`` is a pure function over a compiled pipeline document
(the ``compiled_pipeline.json`` shape, optionally carrying the packager's
``bindingPoints`` section), the Camera_Bindings delivered for one
``{workflow_id}/{version}`` in the ``dda-camera-bindings`` shadow
(``{node_id: {"cameraSourceId": id} | {"override": {param: value}}}``),
and the device-local Camera_Source inventory (the ``build_inventory``
merge of Image_Source records and Camera_Discovery results, keyed by the
same stable ids the Portal registry shows).

- A ``cameraSourceId`` binding looks the id up in the local inventory and
  substitutes the source's resolved parameter values into the binding
  point's declared ``slots`` of a copy of the document (10.1). An id with
  no inventory entry marks the resolution ``invalid`` and records the id
  in ``missing`` so the watcher can report ``missing camera source
  {csid}`` (10.2).
- An ``override`` binding substitutes its values directly, regardless of
  inventory, after constraint-checking them against the vendored
  workflow_core catalog descriptor for the node type (10.3); a violation
  marks the resolution ``invalid``.
- JP4/JP5 adapter binding points (``adapterBinding: true``, empty slots)
  never substitute into the document — they yield ``adapter_assignments``
  (node id -> resolved camera parameters) consumed by the executor when
  it connects the camera adapter to the appsrc.
- Aravis binding points (``aravisBinding: true``, empty slots — the
  aravis-camera-input feature, Requirements 6.1, 6.2, 6.3) likewise never
  substitute into the document: a resolved ``cameraSourceId`` or
  constraint-valid ``override`` binding contributes to
  ``aravis_assignments`` (same shape as ``adapter_assignments``),
  consumed by the executor's Aravis frame feed. Missing ids and override
  violations follow the same invalid path as every other binding point.
- A document without ``bindingPoints`` (every pre-feature component), or
  a registration with no bindings supplied, is returned unchanged — the
  compiled-in parameter values run exactly as before (10.5, 11.1).

No I/O, no shadow access: the CameraBindingStore / watcher integration
lives elsewhere.
"""
import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

STATUS_RESOLVED = "resolved"
STATUS_INVALID = "invalid"

#: Inventory parameter keys whose values land in a differently named node
#: parameter: the ``build_inventory`` reported shape calls the V4L2 node
#: path ``devicePath`` while the icam_source node declares ``device``,
#: and the Aravis camera identity ``cameraId`` while the
#: aravis_camera_source node declares ``camera_id``.
_PARAM_ALIASES = {"devicePath": "device", "cameraId": "camera_id"}


@dataclass(frozen=True)
class ResolutionResult:
    """The outcome of resolving one document's Camera_Bindings.

    ``document`` is a substituted copy (the input object itself when
    nothing had to change), ``status`` is ``resolved`` or ``invalid``,
    ``missing`` lists every binding whose ``cameraSourceId`` has no local
    inventory entry (design shape ``{nodeId, cameraSourceId}``),
    ``adapter_assignments`` maps adapter-fed node ids to their resolved
    camera parameters, ``aravis_assignments`` maps Aravis-fed node ids to
    theirs (same shape, kept distinct so the executor can tell the JP4/5
    camera adapter apart from the Aravis frame feed), and ``errors``
    carries one human-readable reason per problem (missing sources and
    override constraint violations) for the watcher's
    invalid-registration reason.
    """

    document: Dict[str, Any]
    status: str
    missing: Tuple[Dict[str, str], ...] = ()
    adapter_assignments: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    aravis_assignments: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: CSI-fed node ids -> resolved camera parameters (same shape as
    #: ``aravis_assignments``, kept distinct so the executor's CSI capture
    #: handling can read the effective gain/exposure). csi_camera_source
    #: binding points carry ``csiSensorBinding: true`` with empty slots, so
    #: like Aravis they never substitute into the document
    #: (csi-icam-input-nodes Requirement 7.1).
    csi_assignments: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    errors: Tuple[str, ...] = ()


def resolve_bindings(document: Dict[str, Any],
                     bindings: Optional[Mapping[str, Any]],
                     local_inventory) -> ResolutionResult:
    """Resolve ``bindings`` against ``local_inventory`` and apply them to
    a copy of ``document``. Pure over its inputs.

    ``local_inventory`` is either a mapping ``{camera_source_id: entry}``
    or an iterable of entries (``camera_sync.CameraSourceState`` instances
    or dicts of the reported shape).
    """
    binding_points = document.get("bindingPoints") if isinstance(document, dict) else None
    if not binding_points or not bindings:
        # No binding points (pre-feature component) or no bindings
        # supplied: the compiled-in values run as-is (10.5, 11.1).
        return ResolutionResult(document=document, status=STATUS_RESOLVED)

    inventory = _normalize_inventory(local_inventory)
    resolved_document = copy.deepcopy(document)
    missing: List[Dict[str, str]] = []
    errors: List[str] = []
    adapter_assignments: Dict[str, Dict[str, Any]] = {}
    aravis_assignments: Dict[str, Dict[str, Any]] = {}
    csi_assignments: Dict[str, Dict[str, Any]] = {}

    for point in binding_points:
        if not isinstance(point, Mapping):
            continue
        node_id = point.get("nodeId")
        binding = bindings.get(node_id) if isinstance(node_id, str) else None
        if not isinstance(binding, Mapping):
            # Unbound binding point: its rendered defaults run as-is (10.5).
            continue

        camera_source_id = None
        values: Optional[Dict[str, Any]] = None

        if isinstance(binding.get("cameraSourceId"), str) and binding["cameraSourceId"]:
            camera_source_id = binding["cameraSourceId"]
            entry = inventory.get(camera_source_id)
            if entry is None:
                missing.append({"nodeId": node_id,
                                "cameraSourceId": camera_source_id})
                errors.append("missing camera source {0}".format(camera_source_id))
                continue
            values = _resolved_parameter_values(entry)
        elif isinstance(binding.get("override"), Mapping):
            override = dict(binding["override"])
            violations = _override_violations(
                node_id, point.get("nodeType"), override)
            if violations:
                errors.extend(violations)
                continue
            values = override
        else:
            # Unrecognized binding shape: leave the compiled defaults.
            continue

        if point.get("aravisBinding") is True:
            # Aravis: the executor's frame feed grabs from the camera
            # manager; the binding selects which camera id it grabs, not
            # an element arg (aravis-camera-input Requirements 6.1, 6.2).
            aravis_assignments[node_id] = {
                "cameraSourceId": camera_source_id,
                "params": values,
            }
        elif point.get("adapterBinding") is True:
            # JP4/JP5: the executor's camera adapter feeds the appsrc; the
            # binding selects which camera it connects, not an element arg.
            adapter_assignments[node_id] = {
                "cameraSourceId": camera_source_id,
                "params": values,
            }
        elif point.get("csiSensorBinding") is True:
            # NVIDIA CSI: the host capture service stages frames; the
            # binding selects which CSI sensor it stages from, and the
            # executor writes the resolved gain/exposure to the service
            # config file. Never substitutes into an element arg
            # (csi-icam-input-nodes Requirement 7.1).
            csi_assignments[node_id] = {
                "cameraSourceId": camera_source_id,
                "params": values,
            }
        else:
            _substitute_slots(resolved_document, point.get("slots") or [], values)

    status = STATUS_INVALID if errors else STATUS_RESOLVED
    return ResolutionResult(
        document=resolved_document,
        status=status,
        missing=tuple(missing),
        adapter_assignments=adapter_assignments,
        aravis_assignments=aravis_assignments,
        csi_assignments=csi_assignments,
        errors=tuple(errors),
    )


# --- helpers -----------------------------------------------------------------


def _normalize_inventory(local_inventory) -> Dict[str, Any]:
    """``{camera_source_id: entry}`` from either a mapping or an iterable
    of CameraSourceState / dict entries."""
    if local_inventory is None:
        return {}
    if isinstance(local_inventory, Mapping):
        return dict(local_inventory)
    normalized: Dict[str, Any] = {}
    for entry in local_inventory:
        camera_source_id = _get(entry, "camera_source_id") or _get(entry, "cameraSourceId")
        if isinstance(camera_source_id, str) and camera_source_id:
            normalized[camera_source_id] = entry
    return normalized


def _get(obj, key, default=None):
    """Dict- or attribute-style access, tolerant of both entry shapes."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _resolved_parameter_values(entry) -> Dict[str, Any]:
    """The inventory entry's parameter values keyed by node parameter
    name: ``devicePath`` resolves the ``device`` slot parameter, and
    ``gain`` / ``exposure`` (and any identically named parameter) pass
    through when present."""
    params = _get(entry, "params") or {}
    values: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        values[key] = value
        alias = _PARAM_ALIASES.get(key)
        if alias is not None and alias not in params:
            values[alias] = value
    return values


def _override_violations(node_id, node_type, override: Dict[str, Any]) -> List[str]:
    """Manual-override constraint violations against the vendored catalog
    descriptor (10.3). When the vendored catalog has no descriptor for the
    node type (a camera-backed Custom_Node_Type), the values pass through
    unchecked — the Portal's Deployment_Service already validated them
    against the full merged catalog before delivery."""
    from workflow_engine.vendor.workflow_core.catalog import get_node_type
    from workflow_engine.vendor.workflow_core.validator import check_parameter_value

    descriptor = get_node_type(node_type) if isinstance(node_type, str) else None
    if descriptor is None:
        return []
    parameters = {parameter.name: parameter
                  for parameter in descriptor.parameters}
    violations: List[str] = []
    for name in sorted(override):
        parameter = parameters.get(name)
        if parameter is None:
            violations.append(
                "override for node '{0}' sets '{1}', which is not a declared "
                "parameter of node type '{2}'".format(node_id, name, node_type))
            continue
        violation = check_parameter_value(parameter, override[name])
        if violation is not None:
            violations.append(
                "override for node '{0}': {1}".format(node_id, violation.message))
    return violations


def _substitute_slots(document: Dict[str, Any], slots, values: Dict[str, Any]) -> None:
    """Write each resolved value into the element argument its slot
    declares (``{param, segment, element, arg}``). Slots whose parameter
    has no resolved value keep their rendered default; slots that no
    longer address the document (malformed input) are skipped, matching
    the packager's tolerant slot reader."""
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        param = slot.get("param")
        if param not in values:
            continue
        try:
            segment = document["segments"][slot["segment"]]
            segment["elements"][slot["element"]]["args"][slot["arg"]] = values[param]
        except (KeyError, IndexError, TypeError):
            continue
