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
"""Custom Python source planning (custom-python-source Requirements 7.3,
8.5).

``plan_python_sources`` is a pure function over a compiled pipeline
document (the ``compiled_pipeline.json`` shape, optionally carrying the
packager's ``bindingPoints`` section), mirroring
:func:`~workflow_engine.aravis_feed.plan_aravis_feeds`. For each binding
point marked ``pythonSourceBinding: true`` it plans one
:class:`PythonSourceFeed` — the executor builds a producer bridge over
the feed's artifact-relative ``handler_path`` and runs
``produce_frame(context)`` under the feed's ``allowed_uri_prefixes``.

Planning failures follow the executor's contained-failure discipline
(the :class:`~workflow_engine.aravis_feed.AravisFeedError` pattern):
:class:`PythonSourceError` carries the ``node_id`` so the executor can
set ``failing_node_id`` directly; it is ``None`` for document-level
failures no single node owns. A document carrying more than one fed
frame source — counted across the UNION of ``pythonSourceBinding`` and
``aravisBinding`` points — violates the single-frame appsrc Frame_Feed
contract and fails with a reason naming every offending node
(Requirement 8.5).

Documents with no Python source binding points — including every
pre-feature document without a ``bindingPoints`` section and documents
with only Aravis/camera points — plan zero Python sources, so the
executor takes the exact pre-feature call path (Requirement 7.3).

``allowed_uri_prefixes`` is parsed from the binding point's rendered
``allowed_uri_prefixes`` parameter: newline-split, each line stripped,
empty lines dropped; a missing or empty parameter yields ``()``
(unrestricted).

No I/O: the bridge construction and pipeline wiring live in the
executor.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: The frame-feed markers the packager stamps on binding points; a
#: document may carry at most one point across the union (8.5).
_FEED_MARKERS = ("pythonSourceBinding", "aravisBinding")


class PythonSourceError(Exception):
    """A Python source planning failure, identified by its node id.

    ``node_id`` lets the executor set ``failing_node_id`` directly —
    the failure fails only that workflow run (Requirement 7.6 error
    discipline). It is ``None`` for document-level failures (the
    multi-fed-source contract violation) that no single node owns.
    """

    def __init__(self, node_id: Optional[str], message: str) -> None:
        self.node_id = node_id
        super().__init__(
            "Custom Python source '{0}': {1}".format(node_id, message)
            if node_id is not None else message
        )


@dataclass(frozen=True)
class PythonSourceFeed:
    """One planned frame production: the executor builds a producer
    bridge over ``handler_path`` (artifact-relative,
    ``python/{nodeId}/handler.py``), runs ``produce_frame`` under
    ``allowed_uri_prefixes``, and pushes the Produced_Frame into the
    appsrc rendered for ``node_id``."""

    node_id: str
    handler_path: str
    allowed_uri_prefixes: Tuple[str, ...] = ()


def plan_python_sources(document: Dict[str, Any]) -> List[PythonSourceFeed]:
    """Plan the document's Frame_Producers. Pure over its input.

    Returns ``[]`` when the document declares no ``pythonSourceBinding``
    points — including pre-feature documents with no ``bindingPoints``
    section and documents with only Aravis/camera points (7.3).

    Raises :class:`PythonSourceError` (``node_id=None``) when the
    document carries more than one fed frame source across the union of
    ``pythonSourceBinding`` and ``aravisBinding`` points, naming every
    offending node (8.5).
    """
    binding_points = document.get("bindingPoints") if isinstance(document, dict) else None
    if not binding_points:
        # Pre-feature documents (no bindingPoints section) and documents
        # packaged without frame-feed nodes: zero Python sources (7.3).
        return []

    fed_points = [point for point in binding_points
                  if isinstance(point, Mapping)
                  and any(point.get(marker) is True
                          for marker in _FEED_MARKERS)]
    if len(fed_points) > 1:
        node_ids = ", ".join(
            "'{0}'".format(point.get("nodeId")) for point in fed_points)
        raise PythonSourceError(
            None,
            "document declares {0} frame-feed source binding points "
            "({1}); the single-frame appsrc feed serves exactly one "
            "frame-feed source per workflow".format(
                len(fed_points), node_ids))

    python_points = [point for point in fed_points
                     if point.get("pythonSourceBinding") is True]
    if not python_points:
        # Aravis-only (or feed-free) documents plan zero Python sources;
        # plan_aravis_feeds owns the Aravis point.
        return []

    point = python_points[0]
    node_id = point.get("nodeId")
    return [PythonSourceFeed(
        node_id=node_id,
        handler_path="python/{0}/handler.py".format(node_id),
        allowed_uri_prefixes=_parse_prefixes(point.get("parameters")),
    )]


# --- helpers -----------------------------------------------------------------


def _parse_prefixes(parameters: Any) -> Tuple[str, ...]:
    """The rendered ``allowed_uri_prefixes`` parameter, newline-split,
    stripped, empties dropped; ``()`` when missing, empty, or not a
    string (unrestricted)."""
    if not isinstance(parameters, Mapping):
        return ()
    raw = parameters.get("allowed_uri_prefixes")
    if not isinstance(raw, str):
        return ()
    return tuple(line.strip() for line in raw.splitlines() if line.strip())
