"""Per-node results assembly + incremental flush (Requirements 12.7,
12.10): every flush carries the full document, prior results survive a
mid-run failure, and the failing node stays identified."""

import pytest

from harness.results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    ResultsStore,
)


class FlushRecorder:
    """Captures every flushed document snapshot."""

    def __init__(self):
        self.snapshots = []

    def __call__(self, document):
        self.snapshots.append(document)

    @property
    def last(self):
        return self.snapshots[-1]


@pytest.fixture
def flush():
    return FlushRecorder()


@pytest.fixture
def store(flush):
    return ResultsStore(["src", "inf", "out"], flush)


class TestDocumentShape:
    def test_initial_records_have_full_shape(self, store):
        document = store.to_document()
        assert [n["nodeId"] for n in document["nodes"]] == ["src", "inf", "out"]
        for record in document["nodes"]:
            assert record["status"] == STATUS_PENDING
            assert record["outputs"] == []
            assert record["stubActivity"] == []
            assert record["error"] is None

    def test_flushed_snapshot_is_isolated(self, store, flush):
        store.flush()
        snapshot = flush.last
        store.set_status("src", STATUS_COMPLETED, flush=False)
        assert snapshot["nodes"][0]["status"] == STATUS_PENDING


class TestIncrementalFlush:
    def test_each_mutation_flushes(self, store, flush):
        store.set_status("src", STATUS_RUNNING)
        store.add_output("inf", {"type": "inference_metadata"})
        store.add_stub_activity("out", {"type": "recorded_actuation"})
        assert len(flush.snapshots) == 3

    def test_every_flush_contains_all_prior_results(self, store, flush):
        store.set_status("src", STATUS_COMPLETED)
        store.add_output("inf", {"type": "inference_metadata", "s3Key": "k"})
        store.set_error("out", "boom", code="X")
        # The final snapshot retains everything recorded before it (12.10).
        final = flush.last
        by_id = {n["nodeId"]: n for n in final["nodes"]}
        assert by_id["src"]["status"] == STATUS_COMPLETED
        assert by_id["inf"]["outputs"] == [
            {"type": "inference_metadata", "s3Key": "k"}]
        assert by_id["out"]["status"] == STATUS_FAILED

    def test_batch_status_update_flushes_once(self, store, flush):
        store.set_statuses(["src", "inf"], STATUS_RUNNING)
        assert len(flush.snapshots) == 1
        assert flush.last["nodes"][0]["status"] == STATUS_RUNNING
        assert flush.last["nodes"][2]["status"] == STATUS_PENDING


class TestFailureHandling:
    def test_set_error_identifies_failing_node(self, store, flush):
        store.set_error("inf", "Pipeline failed with: no model",
                        code="PIPELINE_EXECUTION_ERROR")
        record = flush.last["nodes"][1]
        assert record["status"] == STATUS_FAILED
        assert record["error"] == {
            "code": "PIPELINE_EXECUTION_ERROR",
            "message": "Pipeline failed with: no model",
        }
        assert store.has_failure()

    def test_skip_remaining_preserves_completed_and_failed(self, store, flush):
        store.set_status("src", STATUS_COMPLETED, flush=False)
        store.set_error("inf", "boom", flush=False)
        store.skip_remaining()
        by_id = {n["nodeId"]: n for n in flush.last["nodes"]}
        assert by_id["src"]["status"] == STATUS_COMPLETED
        assert by_id["inf"]["status"] == STATUS_FAILED
        assert by_id["out"]["status"] == STATUS_SKIPPED

    def test_no_failure_initially(self, store):
        assert not store.has_failure()
