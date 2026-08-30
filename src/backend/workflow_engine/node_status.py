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
* at Pipeline_EOS :meth:`mark_pipeline_success` transitions exactly the
  name_map-derived pipeline nodes that are ``running`` to ``success``,
  freezing their durations at EOS (vllm-workflow-latency-optimization R2);
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
import threading
import time
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
        # Internal mutation lock (detection-guided-bedrock-inspection
        # Requirement 5.6, design "Thread safety inventory"): concurrent
        # Bedrock branches report status/detail/duration from pool worker
        # threads, so every setter's read-modify-write sequence is guarded.
        # A reentrant lock keeps the public mutators free to share
        # ``_set_status`` without re-acquisition deadlocks. Single-writer
        # callers (every pre-feature path) are unaffected — purely
        # additive.
        self._lock = threading.RLock()
        # element-name -> nodeId (synthetic elements map to None).
        self._name_map: Dict[str, Optional[str]] = dict(name_map or {})
        # nodeId -> status; only distinct non-None nodeIds participate.
        self._statuses: Dict[str, str] = {}
        # nodeId -> warning/error detail (retained for the status graph).
        self._details: Dict[str, str] = {}
        # -- node-execution-timing state (R1.1/R1.2/R1.5) -------------------
        # nodeId -> monotonic seconds at the node's FIRST entry into running.
        self._running_since: Dict[str, float] = {}
        # nodeId -> lifecycle duration in ms, recorded exactly once at the
        # node's FIRST terminal transition (never overwritten, R1.1).
        self._durations_ms: Dict[str, int] = {}
        # nodeId -> executor-binding invocation duration in ms (takes
        # precedence over the lifecycle value at serialization time, R1.4).
        self._invocation_durations_ms: Dict[str, int] = {}
        for node_id in self._name_map.values():
            if node_id is not None and node_id not in self._statuses:
                self._statuses[node_id] = STATUS_PENDING
        # The name_map-derived Pipeline_Nodes (nodes with a pipeline
        # element). Binding nodes seeded via ``extra_node_ids`` below are,
        # by construction, not in it — mark_pipeline_success only ever
        # touches this set (R2.4).
        self._pipeline_node_ids = {
            nid for nid in self._name_map.values() if nid is not None
        }
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

    # -- centralized status write (node-execution-timing) -------------------

    def _set_status(self, node_id: str, status: str) -> None:
        """The single status-write path for every mutation method.

        Performs the identical ``self._statuses[node_id] = status``
        assignment the mutation paths previously did directly, then —
        contained per R1.7 — captures lifecycle timing:

        * the node's FIRST entry into ``running`` records
          ``time.monotonic()`` (R1.2, R1.5);
        * the node's FIRST entry into a terminal state records
          ``max(0, round((now - start) * 1000))`` only if the node ran and
          has no duration yet (R1.1, R1.2);
        * later transitions never overwrite a recorded duration (R1.1);
        * a node reaching terminal without ever running records nothing
          (R1.6).

        The timing capture is wrapped in try/except so the status
        assignment always stands and no partial value is recorded on error
        (R1.7); transition semantics are byte-identical to the pre-feature
        direct assignments (R5.1).

        Guarded by the internal reentrant lock so the assignment and its
        first-transition timing capture form one atomic step under
        concurrent Bedrock-branch writers (detection-guided-bedrock-
        inspection Requirement 5.6).
        """
        with self._lock:
            self._statuses[node_id] = status
            try:
                if status == STATUS_RUNNING:
                    if node_id not in self._running_since:
                        self._running_since[node_id] = time.monotonic()
                elif status in TERMINAL_STATES:
                    if (
                        node_id in self._running_since
                        and node_id not in self._durations_ms
                    ):
                        elapsed = (
                            time.monotonic() - self._running_since[node_id]
                        )
                        self._durations_ms[node_id] = max(
                            0, round(elapsed * 1000))
            except Exception:  # noqa: BLE001 - timing is best-effort (R1.7)
                logger.debug(
                    "NodeStatusCollector._set_status ignored a timing error",
                    exc_info=True,
                )

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
            with self._lock:
                if kind == "warning":
                    # A warning is terminal-ish: it wins over pending/running
                    # and is not overwritten by a later success (see
                    # mark_success_all).
                    if self._statuses.get(node_id) != STATUS_FAILURE:
                        self._set_status(node_id, STATUS_WARNING)
                        if detail:
                            self._details[node_id] = detail
                elif kind == "running":
                    # Only advance pending -> running; never downgrade a node
                    # that already reached a warning/success/failure state.
                    if self._statuses.get(node_id) == STATUS_PENDING:
                        self._set_status(node_id, STATUS_RUNNING)
        except Exception:  # noqa: BLE001 - collector is best-effort (R8.5)
            logger.debug("NodeStatusCollector.sink ignored an error", exc_info=True)

    # -- explicit transitions ----------------------------------------------

    def mark_running_all(self) -> None:
        """Advance every still-``pending`` node to ``running`` (run start)."""
        with self._lock:
            for node_id, status in self._statuses.items():
                if status == STATUS_PENDING:
                    self._set_status(node_id, STATUS_RUNNING)

    def mark_success_all(self) -> None:
        """Mark participating nodes ``success`` on clean completion (R3.3).

        Does NOT downgrade a ``warning`` (its detail is retained) and never
        overrides a ``failure``."""
        with self._lock:
            for node_id, status in self._statuses.items():
                if status in _NON_TERMINAL_STATES:
                    self._set_status(node_id, STATUS_SUCCESS)

    def mark_pipeline_success(self) -> None:
        """Pipeline_EOS terminal marking (R2.1). For exactly the pipeline
        nodes (name_map-derived): ``running`` -> ``success`` via
        :meth:`_set_status` (freezing the lifecycle duration at EOS, R2.2);
        ``warning`` retained with detail; ``failure`` retained; ``pending``
        untouched (R2.7). Binding nodes (``extra_node_ids``) untouched
        (R2.4). Fully contained so an EOS-marking error can never fail a
        run (R2.6, collector best-effort discipline)."""
        try:
            with self._lock:
                for node_id in self._pipeline_node_ids:
                    if self._statuses.get(node_id) == STATUS_RUNNING:
                        self._set_status(node_id, STATUS_SUCCESS)
        except Exception:  # noqa: BLE001 - collector is best-effort (R2.6)
            logger.debug(
                "NodeStatusCollector.mark_pipeline_success ignored an error",
                exc_info=True,
            )

    def mark_failure(self, node_id: Optional[str], detail: Optional[str] = None) -> None:
        """Mark ``node_id`` as ``failure`` and retain its error ``detail``.

        A None ``node_id`` (unidentifiable failing element) marks nothing, so
        no node is spuriously failed (Property 2, R3.2)."""
        if node_id is None:
            return
        with self._lock:
            self._set_status(node_id, STATUS_FAILURE)
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
            with self._lock:
                if node_id not in self._statuses:
                    return
                if self._statuses.get(node_id) == STATUS_FAILURE:
                    return
                self._details[node_id] = detail
        except Exception:  # noqa: BLE001 - collector is best-effort (R8.5)
            logger.debug("NodeStatusCollector.set_detail ignored an error", exc_info=True)

    # -- invocation timing (node-execution-timing) ---------------------------

    def record_invocation_duration(self, node_id: Optional[str], duration_ms) -> None:
        """Record an Executor_Binding_Node's invocation duration (R1.3/R1.4).

        Contained (R8.5 style): ignores None/untracked node ids and negative
        or non-numeric values; stores ``int(round(duration_ms))`` (R2.1).
        Idempotent per node per run: the FIRST recorded invocation duration
        wins and later calls never overwrite it (R1.4 precedence is then
        applied at serialization time). Any internal error is caught so a
        timing failure can never affect the run (R1.7)."""
        try:
            if node_id is None or node_id not in self._statuses:
                return
            if isinstance(duration_ms, bool) or not isinstance(
                duration_ms, (int, float)
            ):
                return
            if duration_ms < 0:
                return
            # round() raises on NaN/inf -> contained by the outer except,
            # recording no partial value (R1.7).
            with self._lock:
                self._invocation_durations_ms.setdefault(
                    node_id, int(round(duration_ms))
                )
        except Exception:  # noqa: BLE001 - timing is best-effort (R1.7)
            logger.debug(
                "NodeStatusCollector.record_invocation_duration ignored an error",
                exc_info=True,
            )

    def duration_ms_of(self, node_id: str) -> Optional[int]:
        """The duration ``to_map()`` would serialize for ``node_id``.

        Invocation duration first (R1.4 precedence), else the lifecycle
        duration, else None. Used by the executor's timing log emission."""
        return self._invocation_durations_ms.get(
            node_id, self._durations_ms.get(node_id)
        )

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
        with self._lock:
            unattributed_failure = failure_detail is not None and not any(
                status == STATUS_FAILURE for status in self._statuses.values()
            )
            for node_id, status in self._statuses.items():
                if status in _NON_TERMINAL_STATES:
                    if unattributed_failure:
                        self._set_status(node_id, STATUS_WARNING)
                        self._details.setdefault(
                            node_id,
                            "The run failed before this node reported a "
                            "result: {0}".format(failure_detail),
                        )
                    else:
                        self._set_status(node_id, STATUS_SUCCESS)

    # -- serialization ------------------------------------------------------

    def to_map(self) -> Dict[str, dict]:
        """The ``{nodeId: {status, detail?, durationMs?}}`` map.

        ``durationMs`` is additive (R2.1, R2.4): a non-negative integer
        present iff a duration was recorded for the node (R2.3), preferring
        the invocation duration over the lifecycle duration (R1.4). The
        existing ``status``/``detail`` fields are untouched."""
        result: Dict[str, dict] = {}
        for node_id, status in self._statuses.items():
            entry = {"status": status}
            detail = self._details.get(node_id)
            if detail:
                entry["detail"] = detail
            duration = self.duration_ms_of(node_id)
            if duration is not None:
                entry["durationMs"] = duration
            result[node_id] = entry
        return result

    def to_json(self) -> str:
        """The map as a JSON string for ``WorkflowExecution.node_status_json``."""
        return json.dumps(self.to_map())


# -- duration formatting (node-execution-timing) ----------------------------


def format_duration_ms(duration_ms: int) -> str:
    """Format a non-negative millisecond duration for display/logging (R3.3).

    ``< 1000`` -> the whole-millisecond value followed by ``" ms"``
    (``"0 ms"`` for 0, R3.4); ``>= 1000`` -> seconds with exactly one
    decimal place rounded to the nearest tenth followed by ``" s"``,
    e.g. ``"3.4 s"`` (R4.2 reuses these rules for the run-log lines).

    Ties at the midpoint follow Python's float formatting (round-half-even
    on the binary value), e.g. ``3450`` -> ``"3.5 s"`` because ``3.45``
    is represented slightly above the midpoint.
    """
    if duration_ms < 1000:
        return "{0} ms".format(int(duration_ms))
    return "{0:.1f} s".format(duration_ms / 1000.0)
