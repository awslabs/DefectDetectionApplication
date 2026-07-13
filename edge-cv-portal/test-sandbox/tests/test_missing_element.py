"""Missing-element diagnosis for ``Gst.parse_launch`` failures.

When the launch string references an element factory that is not
installed in the sandbox image (expected for the proprietary DDA
plugins), the harness maps the factory back to its owning node via the
compiled document's segments and records a clear per-node error, so the
portal shows which node cannot run in the cloud sandbox instead of a
raw parse error. A factory owned by no node (synthetic tee/queue)
records a run-level error with nodeId null. Partial results are
retained and the harness still exits 1 (Requirement 12.10).
"""

import pytest

from harness.harness import (
    ELEMENT_NOT_AVAILABLE,
    missing_element_factory,
    missing_element_message,
    record_missing_element,
)
from harness.results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    ResultsStore,
)

# Compiled document: a camera node feeding an inference node through a
# synthetic tee; the inference chain carries the proprietary emltriton
# factory that is absent from the sandbox image.
DOCUMENT = {
    "segments": [
        {"elements": [
            {"factory": "multifilesrc", "nodeId": "n_src",
             "args": {"location": "/data/%06d.jpg"}},
            {"factory": "tee", "nodeId": None, "args": {"name": "t0"}},
        ]},
        {"from": "t0", "elements": [
            {"factory": "queue", "nodeId": None, "args": {}},
            {"factory": "emltriton", "nodeId": "n_inf",
             "args": {"model": "defect"}},
        ]},
    ],
    "executorBindings": [],
}


@pytest.fixture
def flushes():
    snapshots = []
    return snapshots


@pytest.fixture
def store(flushes):
    return ResultsStore(["n_src", "n_inf"], flushes.append)


class TestMissingElementFactoryExtraction:
    def test_extracts_factory_from_glib_error_message(self):
        assert missing_element_factory(
            'gst_parse_error: no element "emltriton" (1)') == "emltriton"

    def test_extracts_factory_from_bare_message(self):
        assert missing_element_factory('no element "tee"') == "tee"

    def test_returns_none_for_other_parse_errors(self):
        assert missing_element_factory(
            "could not link queue0 to emltriton0") is None
        assert missing_element_factory("") is None
        assert missing_element_factory(None) is None


class TestPerNodeAttribution:
    def test_factory_maps_to_owning_node_with_clear_message(self, store,
                                                            flushes):
        """The factory is mapped back to its owning nodeId through the
        document's segments and a per-node error is recorded on it."""
        failing = record_missing_element(DOCUMENT, store, "emltriton")
        assert failing == "n_inf"

        document = flushes[-1]
        by_id = {n["nodeId"]: n for n in document["nodes"]}
        record = by_id["n_inf"]
        assert record["status"] == STATUS_FAILED
        assert record["error"]["code"] == ELEMENT_NOT_AVAILABLE
        assert record["error"]["message"] == (
            "The 'emltriton' element required by this node is not "
            "available in the cloud test sandbox (proprietary DDA plugins "
            "are not installed in the sandbox image). This node can only "
            "run on a device.")

    def test_message_helper_names_the_factory(self):
        message = missing_element_message("emgpioinput")
        assert "'emgpioinput'" in message
        assert "cloud test sandbox" in message
        assert "only run on a device" in message

    def test_remaining_nodes_are_skipped_and_prior_results_retained(
            self, store, flushes):
        """Completed records survive; still-pending nodes are skipped
        (partial results retained per 12.10)."""
        store.set_status("n_src", STATUS_COMPLETED, flush=False)
        record_missing_element(DOCUMENT, store, "emltriton")

        by_id = {n["nodeId"]: n for n in flushes[-1]["nodes"]}
        assert by_id["n_src"]["status"] == STATUS_COMPLETED
        assert by_id["n_inf"]["status"] == STATUS_FAILED
        assert store.has_failure()

    def test_skips_pending_nodes_when_failure_is_elsewhere(self, store,
                                                           flushes):
        record_missing_element(DOCUMENT, store, "multifilesrc")
        by_id = {n["nodeId"]: n for n in flushes[-1]["nodes"]}
        assert by_id["n_src"]["status"] == STATUS_FAILED
        assert by_id["n_inf"]["status"] == STATUS_SKIPPED


class TestRunLevelAttribution:
    def test_unowned_factory_records_run_level_error_with_null_node(
            self, store, flushes):
        """A synthetic tee/queue factory maps to no node: a run-level
        error record with nodeId null is appended instead."""
        failing = record_missing_element(DOCUMENT, store, "tee")
        assert failing is None

        document = flushes[-1]
        run_records = [n for n in document["nodes"] if n["nodeId"] is None]
        assert len(run_records) == 1
        record = run_records[0]
        assert record["status"] == STATUS_FAILED
        assert record["error"]["code"] == ELEMENT_NOT_AVAILABLE
        assert "'tee'" in record["error"]["message"]
        assert "cloud test sandbox" in record["error"]["message"]

        # Every real node is skipped; the store reports the failure so
        # the harness exits 1.
        node_records = [n for n in document["nodes"]
                        if n["nodeId"] is not None]
        assert all(n["status"] == STATUS_SKIPPED for n in node_records)
        assert store.has_failure()

    def test_unknown_factory_also_records_run_level_error(self, store,
                                                          flushes):
        """A factory absent from the document entirely still produces a
        run-level record rather than crashing."""
        assert record_missing_element(DOCUMENT, store, "videoconvert") is None
        assert any(n["nodeId"] is None for n in flushes[-1]["nodes"])
