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
"""Aravis frame feed planning (aravis-camera-input Requirement 6.4).

``plan_aravis_feeds`` is a pure function over a compiled pipeline
document (the ``compiled_pipeline.json`` shape, optionally carrying the
packager's ``bindingPoints`` section) and an optional
:class:`~workflow_engine.camera_binding.ResolutionResult`. For each
binding point marked ``aravisBinding: true`` it determines the effective
camera identity the executor's frame feed grabs from:

- the resolution's ``aravis_assignments[node_id]["params"]`` when a
  binding was resolved for the node (a ``cameraSourceId`` looked up in
  the local inventory, or a constraint-valid override), else
- the binding point's rendered ``parameters`` — the compiled-in defaults
  that run when no binding was supplied.

The effective camera id is read from the ``camera_id`` parameter (the
node's declared name), accepting the inventory's ``cameraId`` spelling
too. ``gain`` and ``exposure`` join the feed's config when present —
they are the acquisition settings ``camera_manager.get_camera_frame``
applies.

Planning failures follow the executor's contained-failure discipline
(the :class:`~workflow_engine.python_bridge.CustomPythonNodeError`
pattern): :class:`AravisFeedError` carries the ``node_id`` so the
executor can set ``failing_node_id`` directly. A feed whose effective
camera id is empty or missing fails planning attributed to its node; a
document with more than one Aravis binding point violates the
single-appsrc Frame_Feed contract and fails with a reason naming every
Aravis node (registration-side validation, per the design's error
handling table).

Documents with no Aravis binding points — including every pre-feature
document without a ``bindingPoints`` section — plan zero feeds, so the
executor takes the exact pre-feature call path (Requirement 6.6).

No I/O: the camera manager and pipeline wiring live in the executor.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

#: Acquisition parameters forwarded to the camera manager when present.
_CONFIG_PARAMS = ("gain", "exposure")

#: Accepted spellings of the camera identity parameter: the node
#: descriptor declares ``camera_id``; the ``build_inventory`` reported
#: shape (and configured Image_Source params) spell it ``cameraId``.
_CAMERA_ID_KEYS = ("camera_id", "cameraId")


class AravisFeedError(Exception):
    """An Aravis feed planning failure, identified by its node id.

    ``node_id`` lets the executor set ``failing_node_id`` directly —
    the failure fails only that workflow run (Requirement 6.5 error
    discipline). It is ``None`` for document-level failures (the
    multiple-Aravis-points contract violation) that no single node owns.
    """

    def __init__(self, node_id: Optional[str], message: str) -> None:
        self.node_id = node_id
        super().__init__(
            "Aravis camera source '{0}': {1}".format(node_id, message)
            if node_id is not None else message
        )


@dataclass(frozen=True)
class AravisFeed:
    """One planned frame grab: the executor calls
    ``camera_manager.get_camera_frame(camera_id, config)`` and pushes the
    frame into the appsrc rendered for ``node_id``."""

    node_id: str
    camera_id: str
    config: Dict[str, Any] = field(default_factory=dict)


def plan_aravis_feeds(document: Dict[str, Any],
                      resolution) -> List[AravisFeed]:
    """Plan the Aravis frame feeds for one document. Pure over its
    inputs.

    ``resolution`` is the watcher's cached
    :class:`~workflow_engine.camera_binding.ResolutionResult` for the
    registration, or ``None`` when bindings were never resolved (the
    provider-fallback / unbound path) — in which case every Aravis point
    runs on its rendered parameters (Requirement 6.4).

    Raises :class:`AravisFeedError` when a feed has no usable camera id
    (attributed to the node) or when the document carries more than one
    Aravis binding point (single Frame_Feed contract).
    """
    binding_points = document.get("bindingPoints") if isinstance(document, dict) else None
    if not binding_points:
        # Pre-feature documents (no bindingPoints section) and documents
        # packaged without camera input nodes: zero feeds (6.6).
        return []

    aravis_points = [point for point in binding_points
                     if isinstance(point, Mapping)
                     and point.get("aravisBinding") is True]
    if not aravis_points:
        return []
    if len(aravis_points) > 1:
        node_ids = ", ".join(
            "'{0}'".format(point.get("nodeId")) for point in aravis_points)
        raise AravisFeedError(
            None,
            "document declares {0} Aravis camera source binding points "
            "({1}); the single-frame appsrc feed supports exactly one "
            "Aravis camera source per workflow".format(
                len(aravis_points), node_ids))

    point = aravis_points[0]
    node_id = point.get("nodeId")
    values = _effective_values(point, node_id, resolution)

    camera_id = _camera_id(values)
    if camera_id is None:
        raise AravisFeedError(
            node_id,
            "no camera id: neither the resolved binding nor the rendered "
            "parameters carry a non-empty camera_id")

    config = {name: values[name] for name in _CONFIG_PARAMS
              if values.get(name) is not None}
    return [AravisFeed(node_id=node_id, camera_id=camera_id, config=config)]


# --- helpers -----------------------------------------------------------------


def _effective_values(point: Mapping, node_id,
                      resolution) -> Dict[str, Any]:
    """The assignment's params when the resolution carries one for the
    node, else the binding point's rendered parameters (6.4)."""
    if resolution is not None:
        assignments = getattr(resolution, "aravis_assignments", None) or {}
        assignment = assignments.get(node_id)
        if isinstance(assignment, Mapping):
            params = assignment.get("params")
            if isinstance(params, Mapping):
                return dict(params)
    parameters = point.get("parameters")
    return dict(parameters) if isinstance(parameters, Mapping) else {}


def _camera_id(values: Mapping[str, Any]) -> Optional[str]:
    """The effective camera id: the first non-empty string under an
    accepted spelling, ``None`` when there is none."""
    for key in _CAMERA_ID_KEYS:
        value = values.get(key)
        if isinstance(value, str) and value:
            return value
    return None
