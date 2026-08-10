#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Per-node run-status collection for the WorkflowExecutor (Requirement 3).

``NodeStatusCollector`` accumulates a per-node execution state over a run.
It is built from ``rendering.element_name_map(document)`` (element-name ->
nodeId) so it maps the pipeline's bus signals (via the optional
``run_pipeline`` ``status_sink``) back to the workflow's nodes without any
dependency on GStreamer — it is therefore fully testable in isolation.
Executor-binding nodes (no pipeline element) are seeded through the
optional ``extra_node_ids`` constructor argument so they participate in
the same lifecycle and always reach a terminal status.

Lifecycle over a run (design §3.3):

* every participating nodeId (the distinct non-None nodeIds in the map)
  starts ``pending``;
* :meth:`mark_running_all` (on run start) and the ``status_sink`` drive
  ``running``/``warning`` live when the pipeline reports per-element bus
  signals;
* on clean completion :meth:`mark_success_all` marks participating nodes
  ``success`` (but never downgrades a ``warning``);
* on failure :meth:`mark_failure` marks the mapped failing node ``failure``
  with its error detail (via ``rendering.failing_node_id_from_error``);
* :meth:`finalize` guarantees a fully-terminal map (Property 1 / R3.6): no
  node may remain ``pending``/``running`` in the terminal map.

Every mutation is contained so a collector error can never fail a run
(R8.5); :meth:`to_json` yields the ``{nodeId: {status, detail?}}`` map
persisted to ``WorkflowExecution.node_status_json``.
"""

import json
import logging
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

#: The five Node_Run_Status values (Requirement 3).
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_FAILURE = "failure"

#: States that count as terminal for Property 1 (a finished run's map holds
#: only these).
TERMINAL_STATES = frozenset({STATUS_SUCCESS, STATUS_WARNING, STATUS_FAILURE})

#: Non-terminal states finalize resolves.
_NON_TERMINAL_STATES = frozenset({STATUS_PENDING, STATUS_RUNNING})


class NodeStatusCollector:
    """Accumulates per-node run status from an element-name -> nodeId map."""

    def __init__(
        self,
        name_map: Optional[Dict[str, Optional[str]]] = None,
        extra_node_ids: Optional[Iterable[str]] = None,
    ) -> None:
        # element-name -> nodeId (synthetic elements map to None).
        self._name_map: Dict[str, Optional[str]] = dict(name_map or {})
        # nodeId -> status; only distinct non-None nodeIds participate.
        self._statuses: Dict[str, str] = {}
        # nodeId -> warning/error detail (retained for the status graph).
        self._details: Dict[str, str] = {}
        for node_id in self._name_map.values():
            if node_id is not None and node_id not in self._statuses:
                self._statuses[node_id] = STATUS_PENDING
        # Additional participating nodes with no pipeline element — the
        # compiled document's executorBindings node ids (llm_inference,
        # mqtt_publish, opcua_write, digital_output, bedrock_inference,
        # ...). Seeding them here makes them participate in
        # mark_running_all/mark_success_all/finalize so they always reach
        # a terminal status in node_status_json instead of staying absent
        # (the run view resolves absent nodes to "pending").
        for node_id in extra_node_ids or ():
            if node_id is not None and node_id not in self._statuses:
                self._statuses[node_id] = STATUS_PENDING

    # -- introspection ------------------------------------------------------

    def participating_nodes(self) -> set:
        """The distinct non-None nodeIds this collector tracks."""
        return set(self._statuses.keys())

    def status_of(self, node_id: str) -> Optional[str]:
        """The current status of ``node_id`` (or None if untracked)."""
        return self._statuses.get(node_id)

    # -- live sink ----------------------------------------------------------

    def sink(self, element_name: str, kind: str, detail: Optional[str] = None) -> None:
        """Receive a per-element bus signal from ``run_pipeline``.

        ``kind`` is ``"running"`` (element reached PLAYING) or ``"warning"``
        (a non-fatal element warning; ``detail`` is retained, R3.4). Element
        names with no mapped node (synthetic tee/queue/funnel, or the
        pipeline itself) are ignored. Failure is left to the executor's
        explicit :meth:`mark_failure`. Fully contained (R8.5)."""
        try:
            node_id = self._name_map.get(element_name)
            if node_id is None:
                return
            if kind == "warning":
                # A warning is terminal-ish: it wins over pending/running and
                # is not overwritten by a later success (see mark_success_all).
                if self._statuses.get(node_id) != STATUS_FAILURE:
                    self._statuses[node_id] = STATUS_WARNING
                    if detail:
                        self._details[node_id] = detail
            elif kind == "running":
                # Only advance pending -> running; never downgrade a node that
                # already reached a warning/success/failure state.
                if self._statuses.get(node_id) == STATUS_PENDING:
                    self._statuses[node_id] = STATUS_RUNNING
        except Exception:  # noqa: BLE001 - collector is best-effort (R8.5)
            logger.debug("NodeStatusCollector.sink ignored an error", exc_info=True)

    # -- explicit transitions ----------------------------------------------

    def mark_running_all(self) -> None:
        """Advance every still-``pending`` node to ``running`` (run start)."""
        for node_id, status in self._statuses.items():
            if status == STATUS_PENDING:
                self._statuses[node_id] = STATUS_RUNNING

    def mark_success_all(self) -> None:
        """Mark participating nodes ``success`` on clean completion (R3.3).

        Does NOT downgrade a ``warning`` (its detail is retained) and never
        overrides a ``failure``."""
        for node_id, status in self._statuses.items():
            if status in _NON_TERMINAL_STATES:
                self._statuses[node_id] = STATUS_SUCCESS

    def mark_failure(self, node_id: Optional[str], detail: Optional[str] = None) -> None:
        """Mark ``node_id`` as ``failure`` and retain its error ``detail``.

        A None ``node_id`` (unidentifiable failing element) marks nothing, so
        no node is spuriously failed (Property 2, R3.2)."""
        if node_id is None:
            return
        self._statuses[node_id] = STATUS_FAILURE
        if detail:
            self._details[node_id] = detail

    def set_detail(self, node_id: Optional[str], detail: Optional[str]) -> None:
        """Record ``detail`` for ``node_id`` WITHOUT changing its status.

        Used by output bindings to attach a sent-message / skipped-outcome
        summary to a node (output-node-sent-message feature). No-op for a
        None/untracked node or an empty ``detail``. NEVER overwrites a detail
        belonging to a node whose status is ``failure`` — failure details
        always win (Requirement 3.3). Fully contained so a recording error can
        never fail a run (R8.5 discipline)."""
        try:
            if node_id is None or not detail:
                return
            if node_id not in self._statuses:
                return
            if self._statuses.get(node_id) == STATUS_FAILURE:
                return
            self._details[node_id] = detail
        except Exception:  # noqa: BLE001 - collector is best-effort (R8.5)
            logger.debug("NodeStatusCollector.set_detail ignored an error", exc_info=True)

    def finalize(self, failure_detail: Optional[str] = None) -> None:
        """Resolve any remaining non-terminal node to a terminal state.

        On the success path (``failure_detail`` is None), and on the failure
        path when the failing node was identified (some node already holds
        ``failure``), any node still ``pending``/``running`` becomes
        ``success`` (best effort, R3.6) so the terminal map is fully resolved
        and never holds a ``pending``/``running`` entry (Property 1).

        When the run FAILED but no failing node could be identified
        (``failure_detail`` given and no node holds ``failure`` — e.g. a
        pre-parse ``gst_parse_error`` names an element the map does not
        know), remaining non-terminal nodes resolve to ``warning`` carrying
        the run's error detail instead of ``success``: an all-green terminal
        map would contradict the failed run outcome (R3.6/R6.6 "coloring
        consistent with the run outcome"), while ``warning`` keeps Property 2
        intact (no node is spuriously marked ``failure``)."""
        unattributed_failure = failure_detail is not None and not any(
            status == STATUS_FAILURE for status in self._statuses.values()
        )
        for node_id, status in self._statuses.items():
            if status in _NON_TERMINAL_STATES:
                if unattributed_failure:
                    self._statuses[node_id] = STATUS_WARNING
                    self._details.setdefault(
                        node_id,
                        "The run failed before this node reported a "
                        "result: {0}".format(failure_detail),
                    )
                else:
                    self._statuses[node_id] = STATUS_SUCCESS

    # -- serialization ------------------------------------------------------

    def to_map(self) -> Dict[str, dict]:
        """The ``{nodeId: {status, detail?}}`` map."""
        result: Dict[str, dict] = {}
        for node_id, status in self._statuses.items():
            entry = {"status": status}
            detail = self._details.get(node_id)
            if detail:
                entry["detail"] = detail
            result[node_id] = entry
        return result

    def to_json(self) -> str:
        """The map as a JSON string for ``WorkflowExecution.node_status_json``."""
        return json.dumps(self.to_map())
