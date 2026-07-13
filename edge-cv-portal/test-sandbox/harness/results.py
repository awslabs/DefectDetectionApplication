"""Per-node results assembly with incremental flushing.

The results document is ``{"nodes": [{nodeId, status, outputs,
stubActivity, error}, ...]}`` — the shape workflow_test_steps.step_collect
and workflow_testing.py consume (Requirement 12.7).

Every mutation flushes the complete document through the injected flush
callable (an S3 put in the container), so a mid-run failure retains all
results produced before it and the failing node stays identified in
what was last flushed (Requirement 12.10).
"""

import copy
from typing import Callable, Dict, List, Optional

#: Node result statuses.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
#: Nodes never reached because an earlier node failed (12.10).
STATUS_SKIPPED = "skipped"


class ResultsStore:
    """Ordered per-node result records, flushed incrementally."""

    def __init__(self, node_ids: List[str],
                 flush: Optional[Callable[[Dict], None]] = None):
        self._flush = flush or (lambda document: None)
        self._order: List[str] = list(node_ids)
        #: Run-level error records (nodeId null): failures that cannot be
        #: attributed to any workflow node, e.g. a synthetic linking
        #: element (tee/queue) whose factory is unavailable in the sandbox.
        self._run_errors: List[Dict] = []
        self._records: Dict[str, Dict] = {
            node_id: {
                "nodeId": node_id,
                "status": STATUS_PENDING,
                "outputs": [],
                "stubActivity": [],
                "error": None,
            }
            for node_id in node_ids
        }

    # -- document ---------------------------------------------------------

    def to_document(self) -> Dict:
        """The full results document (deep-copied so flushed snapshots
        cannot be mutated afterwards)."""
        return copy.deepcopy(
            {"nodes": [self._records[node_id] for node_id in self._order]
                      + self._run_errors}
        )

    def flush(self) -> None:
        """Push the current document through the flush callable
        (incremental S3 write — Requirements 12.7, 12.10)."""
        self._flush(self.to_document())

    # -- record access ----------------------------------------------------

    def record(self, node_id: str) -> Dict:
        return self._records[node_id]

    @property
    def node_ids(self) -> List[str]:
        return list(self._order)

    # -- mutations (each flushes) ------------------------------------------

    def set_status(self, node_id: str, status: str, flush: bool = True) -> None:
        self._records[node_id]["status"] = status
        if flush:
            self.flush()

    def set_statuses(self, node_ids: List[str], status: str) -> None:
        """One flush covering a batch of same-status updates."""
        for node_id in node_ids:
            self._records[node_id]["status"] = status
        self.flush()

    def add_output(self, node_id: str, output: Dict, flush: bool = True) -> None:
        self._records[node_id]["outputs"].append(output)
        if flush:
            self.flush()

    def add_stub_activity(self, node_id: str, activity: Dict,
                          flush: bool = True) -> None:
        """Record what a stubbed node would have consumed or emitted
        (Requirement 12.6)."""
        self._records[node_id]["stubActivity"].append(activity)
        if flush:
            self.flush()

    def set_error(self, node_id: str, message: str,
                  code: Optional[str] = None, flush: bool = True) -> None:
        """Mark ``node_id`` failed with an error description — the
        failing-node identification collect relies on (12.10)."""
        self._records[node_id]["status"] = STATUS_FAILED
        self._records[node_id]["error"] = {"code": code, "message": message}
        if flush:
            self.flush()

    def add_run_error(self, message: str, code: Optional[str] = None,
                      flush: bool = True) -> None:
        """Record a run-level failure that maps to no workflow node
        (``nodeId`` null): appended to the results document so collect
        still marks the run failed with the error description."""
        self._run_errors.append({
            "nodeId": None,
            "status": STATUS_FAILED,
            "outputs": [],
            "stubActivity": [],
            "error": {"code": code, "message": message},
        })
        if flush:
            self.flush()

    def skip_remaining(self, flush: bool = True) -> None:
        """Mark every still-pending/running node skipped after a failure;
        completed/failed records are retained untouched (12.10)."""
        for record in self._records.values():
            if record["status"] in (STATUS_PENDING, STATUS_RUNNING):
                record["status"] = STATUS_SKIPPED
        if flush:
            self.flush()

    def has_failure(self) -> bool:
        return bool(self._run_errors) or any(
            record["status"] == STATUS_FAILED or record["error"]
            for record in self._records.values())
