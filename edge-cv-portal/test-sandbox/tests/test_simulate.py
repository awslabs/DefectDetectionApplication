"""Simulate-mode pure logic (custom-node-designer Requirements 7.2, 7.3,
7.6): launch-string assembly for the single-plugin pipeline, per-frame
result-record shaping, S3 frame-reference derivation, and the
incrementally flushed simulation results store — all runnable without
GStreamer or AWS."""

import pytest

from harness.simulate import (
    CAPTURE_SINK_NAME,
    ELEMENT_NOT_AVAILABLE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    SimulationResultsStore,
    element_unavailable_message,
    error_output_tail,
    extend_plugin_path,
    frame_metadata,
    frame_record,
    input_frame_key,
    invalid_identifier,
    missing_frame_records,
    output_frame_key,
    parse_element_parameters,
    render_argument_value,
    render_element_invocation,
    render_simulation_launch,
    run_prefix,
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
    return SimulationResultsStore("myfilter", {"threshold": 3}, flush)


# ---------------------------------------------------------------------------
# ELEMENT_PARAMETERS parsing
# ---------------------------------------------------------------------------

class TestParseElementParameters:
    def test_absent_yields_empty(self):
        assert parse_element_parameters(None) == {}
        assert parse_element_parameters("") == {}

    def test_valid_scalars_kept(self):
        raw = '{"threshold": 3, "ratio": 0.5, "label": "a", "on": true}'
        assert parse_element_parameters(raw) == {
            "threshold": 3, "ratio": 0.5, "label": "a", "on": True}

    def test_malformed_json_yields_empty(self):
        assert parse_element_parameters("{not json") == {}

    def test_non_object_yields_empty(self):
        assert parse_element_parameters('["a"]') == {}

    def test_non_scalar_entries_dropped(self):
        raw = '{"good": 1, "bad": {"nested": true}, "worse": [1]}'
        assert parse_element_parameters(raw) == {"good": 1}


# ---------------------------------------------------------------------------
# Launch-safety validation
# ---------------------------------------------------------------------------

class TestInvalidIdentifier:
    def test_plain_names_accepted(self):
        assert invalid_identifier("myfilter", {"threshold": 1,
                                               "model-repo": "x"}) is None

    @pytest.mark.parametrize("factory", [
        "", "bad name", "a!b", 'x"y', "el ! fakesink", "1leading-digit"])
    def test_unsafe_factory_rejected(self, factory):
        message = invalid_identifier(factory, {})
        assert message is not None
        assert repr(factory) in message

    @pytest.mark.parametrize("param", ["bad name", "a=b", "p!q"])
    def test_unsafe_parameter_name_rejected(self, param):
        message = invalid_identifier("myfilter", {param: 1})
        assert message is not None
        assert repr(param) in message


# ---------------------------------------------------------------------------
# Launch-string assembly
# ---------------------------------------------------------------------------

class TestRenderArgumentValue:
    def test_scalars(self):
        assert render_argument_value(3) == "3"
        assert render_argument_value(0.5) == "0.5"
        assert render_argument_value(True) == "true"
        assert render_argument_value(False) == "false"
        assert render_argument_value("plain") == "plain"

    def test_unsafe_strings_quoted_and_escaped(self):
        assert render_argument_value("a b") == '"a b"'
        assert render_argument_value("x!y") == '"x!y"'
        assert render_argument_value('say "hi"') == '"say \\"hi\\""'
        assert render_argument_value("back\\slash") == '"back\\\\slash"'
        assert render_argument_value("") == '""'


class TestRenderSimulationLaunch:
    def test_single_plugin_pipeline_shape(self):
        launch = render_simulation_launch(
            "myfilter", {"threshold": 3, "mode": "fast"},
            "/tmp/ds/frame_%05d.jpg")
        assert launch == (
            "multifilesrc location=/tmp/ds/frame_%05d.jpg ! jpegparse ! "
            "jpegdec ! videoconvert ! myfilter threshold=3 mode=fast ! "
            "videoconvert ! jpegenc ! appsink name={0} emit-signals=true "
            "sync=false".format(CAPTURE_SINK_NAME))

    def test_no_parameters(self):
        launch = render_simulation_launch("myfilter", {}, "/d/f_%05d.jpg")
        assert " myfilter ! " in launch

    def test_unsafe_parameter_value_cannot_break_pipeline(self):
        launch = render_simulation_launch(
            "myfilter", {"label": "x ! fakesink"}, "/d/f_%05d.jpg")
        # The injected "!" stays inside the quoted value; the capture
        # chain still follows the element.
        assert 'myfilter label="x ! fakesink" ! videoconvert ! jpegenc' \
            in launch

    def test_element_invocation_preserves_declaration_order(self):
        assert render_element_invocation(
            "el", {"b": 1, "a": 2}) == "el b=1 a=2"


# ---------------------------------------------------------------------------
# S3 frame references
# ---------------------------------------------------------------------------

class TestFrameKeys:
    def test_run_prefix_from_results_key(self):
        assert run_prefix("runs/abc/results.json") == "runs/abc/"
        assert run_prefix("results.json") == ""

    def test_input_and_output_frame_keys(self):
        key = "simulations/run-1/results.json"
        assert input_frame_key(key, 0) == \
            "simulations/run-1/frames/input_00000.jpg"
        assert output_frame_key(key, 12) == \
            "simulations/run-1/frames/output_00012.jpg"


# ---------------------------------------------------------------------------
# Result-record shaping
# ---------------------------------------------------------------------------

class TestFrameRecords:
    def test_record_shape(self):
        record = frame_record(4, "in/4.jpg", "out/4.jpg", {"bytes": 10})
        assert record == {"frameIndex": 4, "inputRef": "in/4.jpg",
                          "outputRef": "out/4.jpg", "metadata": {"bytes": 10}}

    def test_none_metadata_becomes_empty_dict(self):
        assert frame_record(0, None, None, None)["metadata"] == {}

    def test_frame_metadata_shape(self):
        metadata = frame_metadata(100, 40, 2048, "image/jpeg",
                                  {"is_anomalous": True})
        assert metadata == {"ptsNs": 100, "durationNs": 40, "bytes": 2048,
                            "caps": "image/jpeg",
                            "tags": {"is_anomalous": True}}

    def test_frame_metadata_copies_tags(self):
        tags = {"a": 1}
        metadata = frame_metadata(None, None, 0, None, tags)
        tags["b"] = 2
        assert metadata["tags"] == {"a": 1}

    def test_missing_frame_records_backfill(self):
        refs = ["in/0", "in/1", "in/2"]
        records = missing_frame_records(3, {1}, refs)
        assert [r["frameIndex"] for r in records] == [0, 2]
        for record in records:
            assert record["inputRef"] == refs[record["frameIndex"]]
            assert record["outputRef"] is None
            assert "note" in record["metadata"]

    def test_missing_frame_records_none_when_all_produced(self):
        assert missing_frame_records(2, {0, 1}, ["a", "b"]) == []


# ---------------------------------------------------------------------------
# Results store: incremental flush, failure retention (7.2, 7.6)
# ---------------------------------------------------------------------------

class TestSimulationResultsStore:
    def test_initial_document_shape(self, store):
        document = store.to_document()
        assert document == {"element": "myfilter",
                            "parameters": {"threshold": 3},
                            "status": STATUS_RUNNING, "frameCount": None,
                            "frames": [], "error": None}

    def test_each_frame_flushes_full_document(self, store, flush):
        store.set_frame_count(2)
        store.add_frame(frame_record(0, "in/0", "out/0", {}))
        store.add_frame(frame_record(1, "in/1", "out/1", {}))
        assert len(flush.snapshots) == 3
        assert [f["frameIndex"] for f in flush.last["frames"]] == [0, 1]
        assert flush.last["frameCount"] == 2

    def test_error_retains_produced_frames(self, store, flush):
        store.add_frame(frame_record(0, "in/0", "out/0", {}))
        store.set_error("plugin crashed", code="PIPELINE_EXECUTION_ERROR",
                        error_output="segfault in myfilter")
        document = flush.last
        assert document["status"] == STATUS_FAILED
        assert document["error"] == {
            "code": "PIPELINE_EXECUTION_ERROR",
            "message": "plugin crashed",
            "errorOutput": "segfault in myfilter"}
        assert len(document["frames"]) == 1
        assert store.has_failure()

    def test_completed(self, store, flush):
        store.set_completed()
        assert flush.last["status"] == STATUS_COMPLETED
        assert not store.has_failure()

    def test_document_orders_frames_by_index(self, store):
        store.add_frame(frame_record(1, None, "out/1", {}), flush=False)
        store.add_frame(frame_record(0, None, None, {}), flush=False)
        assert [f["frameIndex"] for f in store.to_document()["frames"]] \
            == [0, 1]

    def test_flushed_snapshot_is_isolated(self, store, flush):
        store.flush()
        snapshot = flush.last
        store.set_error("later failure", flush=False)
        assert snapshot["status"] == STATUS_RUNNING

    def test_produced_indexes(self, store):
        store.add_frame(frame_record(0, None, "o", {}), flush=False)
        store.add_frame(frame_record(2, None, "o", {}), flush=False)
        assert store.produced_indexes == {0, 2}


# ---------------------------------------------------------------------------
# Plugin staging path + error output helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_extend_plugin_path(self):
        assert extend_plugin_path(None, "/scan") == "/scan"
        assert extend_plugin_path("", "/scan") == "/scan"
        assert extend_plugin_path("/a:/b", "/scan") == "/scan:/a:/b"
        assert extend_plugin_path("/scan:/a", "/scan") == "/scan:/a"

    def test_error_output_tail_bounded(self):
        assert error_output_tail("short") == "short"
        assert error_output_tail("x" * 10, limit=4) == "xxxx"

    def test_element_unavailable_message_distinguishes_plugin_element(self):
        own = element_unavailable_message("myfilter", "myfilter",
                                          "/scan/libmyfilter.so")
        assert "failed to load" in own
        assert "/scan/libmyfilter.so" in own
        other = element_unavailable_message("jpegdec", "myfilter", "/x.so")
        assert "sandbox image" in other

    def test_error_code_exported(self):
        assert ELEMENT_NOT_AVAILABLE == "ELEMENT_NOT_AVAILABLE"
