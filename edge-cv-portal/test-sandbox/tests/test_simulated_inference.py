"""Simulated model inference in cloud test runs (Requirement 12.6).

In simulation the model_inference node is stubbed (the sandbox image has
no proprietary emltriton plugin and registered models are
device-compiled): the compiled document carries a pass-through identity
element named ``sim_inference_<nodeId>`` instead. The harness must

1. identify the stubbed inference nodes from the compiled document,
2. read the user-configured outcome from the SIMULATED_INFERENCE env
   JSON ({"is_anomalous": bool, "confidence": 0..1}, defaulting to
   {"is_anomalous": false, "confidence": 0.9}),
3. inject that outcome as the inference metadata driving downstream
   executor bindings (filters, conditionals, output recorders), and
4. record a "simulated_inference" stub activity entry on each stubbed
   node (which marks it Simulated in the report) and complete it.
"""

import json

import pytest

from harness import harness, renderer
from harness.results import ResultsStore


# ---------------------------------------------------------------------------
# SIMULATED_INFERENCE env parsing
# ---------------------------------------------------------------------------

class TestParseSimulatedInference:
    def test_defaults_when_absent(self):
        assert harness.parse_simulated_inference(None) == {
            "is_anomalous": False, "confidence": 0.9}
        assert harness.parse_simulated_inference("") == {
            "is_anomalous": False, "confidence": 0.9}

    def test_valid_payload(self):
        raw = json.dumps({"is_anomalous": True, "confidence": 0.42})
        assert harness.parse_simulated_inference(raw) == {
            "is_anomalous": True, "confidence": 0.42}

    def test_boundary_confidences(self):
        for confidence in (0, 1, 0.0, 1.0):
            raw = json.dumps({"is_anomalous": False, "confidence": confidence})
            parsed = harness.parse_simulated_inference(raw)
            assert parsed["confidence"] == float(confidence)

    def test_malformed_json_falls_back_to_defaults(self):
        assert harness.parse_simulated_inference("{not json") == \
            harness.DEFAULT_SIMULATED_INFERENCE

    def test_non_object_falls_back_to_defaults(self):
        assert harness.parse_simulated_inference("[1, 2]") == \
            harness.DEFAULT_SIMULATED_INFERENCE

    def test_invalid_fields_fall_back_individually(self):
        # A bad confidence keeps the valid is_anomalous, and vice versa.
        raw = json.dumps({"is_anomalous": True, "confidence": 7})
        assert harness.parse_simulated_inference(raw) == {
            "is_anomalous": True, "confidence": 0.9}
        raw = json.dumps({"is_anomalous": "yes", "confidence": 0.3})
        assert harness.parse_simulated_inference(raw) == {
            "is_anomalous": False, "confidence": 0.3}
        # Booleans are not numbers for the confidence field.
        raw = json.dumps({"confidence": True})
        assert harness.parse_simulated_inference(raw) == \
            harness.DEFAULT_SIMULATED_INFERENCE

    def test_env_reader(self, monkeypatch):
        monkeypatch.setenv("SIMULATED_INFERENCE",
                           json.dumps({"is_anomalous": True, "confidence": 0.5}))
        assert harness.simulated_inference_from_env() == {
            "is_anomalous": True, "confidence": 0.5}
        monkeypatch.delenv("SIMULATED_INFERENCE")
        assert harness.simulated_inference_from_env() == \
            harness.DEFAULT_SIMULATED_INFERENCE


# ---------------------------------------------------------------------------
# Stubbed-inference node identification in the compiled document
# ---------------------------------------------------------------------------

def _element(factory, node_id=None, **args):
    return {"nodeId": node_id, "factory": factory, "args": args}


def _segment(elements):
    return {"name": "s0", "from": None, "linkTo": None, "elements": elements}


class TestSimInferenceNodeIds:
    def test_detects_named_identity_stubs(self):
        document = {"segments": [_segment([
            _element("multifilesrc", "src", location="/tmp/ds/frame_%05d.jpg"),
            _element("capsfilter", "inf", caps="video/x-raw,format=RGB"),
            _element("identity", "inf", name="sim_inference_inf"),
            _element("jpegenc", "cap"),
        ])]}
        assert renderer.sim_inference_node_ids(document) == ["inf"]

    def test_ignores_unrelated_identity_elements(self):
        document = {"segments": [_segment([
            _element("identity", "n1"),                       # unnamed
            _element("identity", "n2", name="other_name"),    # foreign name
            _element("identity", None, name="sim_inference_x"),  # synthetic
        ])]}
        assert renderer.sim_inference_node_ids(document) == []

    def test_multiple_stubs_in_document_order_once(self):
        document = {"segments": [_segment([
            _element("identity", "b", name="sim_inference_b"),
            _element("identity", "a", name="sim_inference_a"),
            _element("identity", "a", name="sim_inference_a"),
        ])]}
        assert renderer.sim_inference_node_ids(document) == ["b", "a"]


# ---------------------------------------------------------------------------
# End-to-end injection through harness.execute (pipeline layer mocked)
# ---------------------------------------------------------------------------

#: Compiled document a simulation compile of
#: folder_source -> model_inference -> {inference_filter -> mqtt_publish}
#: produces: the source stub, the inference pass-through stub, and the
#: executor bindings.
SIM_DOCUMENT = {
    "segments": [_segment([
        _element("multifilesrc", "src", location="{dataset_location}"),
        _element("jpegparse", "src"),
        _element("jpegdec", "src", **{"idct-method": 2}),
        _element("videoconvert", "src"),
        _element("capsfilter", "inf", caps="video/x-raw,format=RGB"),
        _element("identity", "inf", name="sim_inference_inf"),
    ])],
    "executorBindings": [
        {"nodeId": "filt", "binding": "inference_filter",
         "parameters": {"condition": "is_anomalous == true && confidence >= 0.4"},
         "upstreamNodeIds": ["inf"], "downstreamNodeIds": ["mq"]},
        {"nodeId": "mq", "binding": "recording_mqtt_publish",
         "parameters": {"topic": "dda/out"},
         "upstreamNodeIds": ["filt"], "downstreamNodeIds": []},
    ],
}


#: Compiled document a simulation compile of the two-input
#: bedrock_inference node produces (camera + reference folder source,
#: both dataset-fed, funneled into the model_inference-style identity
#: stub). The harness needs NO bedrock-specific handling: the stub is
#: recognized through its ``sim_inference_<nodeId>`` identity element,
#: the configured simulated outcome is injected, and the Bedrock model
#: is never invoked — the sandbox VPC has no internet (12.6).
BEDROCK_SIM_DOCUMENT = {
    "segments": [
        {"name": "s0", "from": None, "linkTo": "f0", "elements": [
            _element("multifilesrc", "cam", location="{dataset_location}"),
            _element("videoconvert", "cam"),
        ]},
        {"name": "s1", "from": None, "linkTo": "f0", "elements": [
            _element("multifilesrc", "ref", location="{dataset_location}"),
            _element("videoconvert", "ref"),
        ]},
        {"name": "s2", "from": None, "linkTo": None, "elements": [
            {"nodeId": None, "factory": "funnel", "args": {"name": "f0"}},
            _element("capsfilter", "bedrock1", caps="video/x-raw,format=RGB"),
            _element("identity", "bedrock1", name="sim_inference_bedrock1"),
        ]},
    ],
    "executorBindings": [
        {"nodeId": "mq", "binding": "recording_mqtt_publish",
         "parameters": {"topic": "dda/out"},
         "upstreamNodeIds": ["bedrock1"], "downstreamNodeIds": []},
    ],
}


@pytest.fixture
def executed(monkeypatch):
    """Run harness.execute over a compiled document with the
    S3/dataset/GStreamer layers mocked; returns (exit_code, final
    results document)."""
    def run(simulated_inference=None, document=None):
        if simulated_inference is None:
            monkeypatch.delenv("SIMULATED_INFERENCE", raising=False)
        else:
            monkeypatch.setenv("SIMULATED_INFERENCE",
                               json.dumps(simulated_inference))
        monkeypatch.setattr(harness, "download_dataset",
                            lambda s3, bucket, prefix, target: {"a.jpg": "/x/a.jpg"})
        monkeypatch.setattr(harness.dataset_module, "stage_dataset",
                            lambda files, staging: "/x/ds/frame_%05d.jpg")
        # No custom plugins staged for these runs (12.1).
        monkeypatch.setattr(
            harness, "load_custom_plugins_manifest",
            lambda s3, bucket, key: dict(harness.EMPTY_CUSTOM_PLUGINS_MANIFEST))
        # The pipeline itself produces no tags: no emltriton runs in sim.
        monkeypatch.setattr(harness, "run_gst_pipeline",
                            lambda launch, sim_sources, store: ({}, None))

        document = json.loads(json.dumps(
            SIM_DOCUMENT if document is None else document))
        snapshots = []
        store = ResultsStore(renderer.all_node_ids(document), snapshots.append)
        exit_code = harness.execute(
            None, "bucket", "results.json", "prefix/", document, store)
        return exit_code, snapshots[-1]
    return run


class TestExecuteInjection:
    def test_configured_outcome_drives_downstream_bindings(self, executed):
        exit_code, report = executed({"is_anomalous": True, "confidence": 0.75})
        assert exit_code == 0
        records = {r["nodeId"]: r for r in report["nodes"]}

        # The stubbed inference node is completed and carries the
        # simulated_inference stub activity (-> Simulated badge, 12.8).
        inf = records["inf"]
        assert inf["status"] == "completed"
        activities = [a for a in inf["stubActivity"]
                      if a["type"] == "simulated_inference"]
        assert len(activities) == 1
        assert activities[0]["isAnomalous"] is True
        assert activities[0]["confidence"] == 0.75
        assert "was not executed" in activities[0]["note"]

        # The injected metadata passed the filter and triggered the
        # downstream recorder with the configured values.
        filt = records["filt"]
        assert filt["status"] == "completed"
        assert filt["outputs"][0]["result"] is True
        assert filt["outputs"][0]["metadata"] == {
            "is_anomalous": True, "confidence": 0.75}

        actuation = records["mq"]["stubActivity"][0]
        assert actuation["triggered"] is True
        assert actuation["triggeringMetadata"] == {
            "is_anomalous": True, "confidence": 0.75}

    def test_default_outcome_when_env_absent(self, executed):
        exit_code, report = executed(None)
        assert exit_code == 0
        records = {r["nodeId"]: r for r in report["nodes"]}
        activity = records["inf"]["stubActivity"][0]
        assert activity["isAnomalous"] is False
        assert activity["confidence"] == 0.9
        # is_anomalous == false: the filter gates the recorder out.
        assert records["filt"]["outputs"][0]["result"] is False
        assert records["mq"]["stubActivity"][0]["triggered"] is False

    def test_non_anomalous_low_confidence_gates_filter_out(self, executed):
        exit_code, report = executed({"is_anomalous": True, "confidence": 0.1})
        assert exit_code == 0
        records = {r["nodeId"]: r for r in report["nodes"]}
        # confidence 0.1 < 0.4: condition false despite the anomaly flag.
        assert records["filt"]["outputs"][0]["result"] is False
        assert records["mq"]["stubActivity"][0]["triggered"] is False


class TestBedrockInferenceStubbing:
    """The two-input bedrock_inference node in simulation: stubbed
    exactly like model_inference (identity ``sim_inference_<nodeId>``),
    the configured outcome injected, the Bedrock model never invoked
    (the sandbox VPC has no internet) — Requirement 12.6."""

    def test_stub_is_recognized_via_the_sim_inference_identity(self):
        assert renderer.sim_inference_node_ids(BEDROCK_SIM_DOCUMENT) == [
            "bedrock1"]

    def test_configured_outcome_flows_downstream_without_invoking_bedrock(
        self, executed
    ):
        exit_code, report = executed(
            {"is_anomalous": True, "confidence": 0.66},
            document=BEDROCK_SIM_DOCUMENT,
        )
        assert exit_code == 0
        records = {r["nodeId"]: r for r in report["nodes"]}

        # The stubbed node completed with the simulated_inference stub
        # activity noting the model was not invoked in the sandbox.
        bedrock = records["bedrock1"]
        assert bedrock["status"] == "completed"
        activities = [a for a in bedrock["stubActivity"]
                      if a["type"] == "simulated_inference"]
        assert len(activities) == 1
        assert activities[0]["isAnomalous"] is True
        assert activities[0]["confidence"] == 0.66
        assert "was not executed in the cloud sandbox" in activities[0]["note"]

        # Both dataset-fed input branches recorded their stub feeds.
        for source in ("cam", "ref"):
            feeds = [a for a in records[source]["stubActivity"]
                     if a["type"] == "dataset_source"]
            assert len(feeds) == 1

        # The injected outcome drove the downstream recorder.
        actuation = records["mq"]["stubActivity"][0]
        assert actuation["triggered"] is True
        assert actuation["triggeringMetadata"] == {
            "is_anomalous": True, "confidence": 0.66}
