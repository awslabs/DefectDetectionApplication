"""Corrected cloud test-run failure attribution and error surfacing
(Requirements 12.14, 12.15, 12.16, 12.17, 12.18).

Background — the regression these tests pin: a synchronous
``pipeline.set_state(PLAYING) == FAILURE`` (most commonly a staged
``emltriton``/CPU-Triton model that fails to load) used to report only
the opaque "failed to change state to PLAYING" and defaulted the failing
node onto ``gst_nodes[0]`` (the Folder/Camera source), mislabeling
downstream inference/sink failures as a source-node failure.

The corrected behavior, exercised here:

* ``run_gst_pipeline`` drains the pipeline bus (bounded wait) for the
  terminal ``GST_MESSAGE_ERROR`` the failing element already posted, so
  the returned error carries the real element name and its backend
  detail; only when no bus error arrives does it fall back to the
  generic message with ``element=None`` (12.14).
* ``execute`` maps the captured element back to its owning node via the
  compiled-document element->node map and attributes the failure there
  (with the backend detail in the message); when no owning node can be
  determined it records a **run-level** error instead of defaulting onto
  the source node (12.15).
* A staged model unusable on CPU Triton is NON-FATAL (best-effort
  inference, 12.16/12.17): the harness reverts the node to its
  sim_inference stub, re-runs once, injects the simulated outcome, and
  marks the node inferenceMode "simulated" with a fallbackReason — the
  run succeeds (this supersedes the earlier "fails attributed to the
  inference node" behavior; the 12.14/12.15 attribution path remains for
  genuine non-model failures).

The ``run_gst_pipeline`` tests inject a fake ``gi`` module (GStreamer is
not importable outside the sandbox container image); the ``execute``
tests mock the S3/dataset/GStreamer layers and feed a crafted
``(tag_values, error)`` return, following the pattern in
tests/test_simulated_inference.py and tests/test_custom_plugins.py.
"""

import json
import sys
import types

import pytest

from harness import harness, renderer
from harness.results import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    ResultsStore,
)


# ---------------------------------------------------------------------------
# Compiled-document helpers (same shape the other suites use)
# ---------------------------------------------------------------------------

def _element(factory, node_id=None, **args):
    return {"nodeId": node_id, "factory": factory, "args": args}


def _segment(elements, name="s0", from_=None, link_to=None):
    return {"name": name, "from": from_, "linkTo": link_to, "elements": elements}


#: folder_source -> model_inference (emltriton) -> sink, as a device/
#: staged build renders it: the emltriton element is auto-named
#: ``emltriton0`` by parse_launch and maps back to the "inf" node.
INFERENCE_DOCUMENT = {
    "segments": [_segment([
        _element("multifilesrc", "src", location="{dataset_location}"),
        _element("jpegparse", "src"),
        _element("jpegdec", "src"),
        _element("videoconvert", "src"),
        _element("emltriton", "inf", model="defect_model"),
        _element("fakesink", "sink"),
    ])],
    "executorBindings": [],
}


#: A simulation compile of folder_source -> model_inference: the
#: inference node is a ``sim_inference_<nodeId>`` identity stub that the
#: staging path rewrites into a real emltriton element (12.16).
STAGED_INFERENCE_DOCUMENT = {
    "segments": [_segment([
        _element("multifilesrc", "src", location="{dataset_location}"),
        _element("jpegparse", "src"),
        _element("jpegdec", "src"),
        _element("videoconvert", "src"),
        _element("capsfilter", "inf", caps="video/x-raw,format=RGB"),
        _element("identity", "inf", name="sim_inference_inf"),
    ])],
    "executorBindings": [],
}


# ---------------------------------------------------------------------------
# Fake gi / GStreamer for run_gst_pipeline (GStreamer is not importable
# outside the sandbox container image)
# ---------------------------------------------------------------------------

#: GStreamer's GST_SECOND (ns per second); timed_pop_filtered takes ns.
_GST_SECOND = 1_000_000_000


class _FakeGLibError:
    def __init__(self, message):
        self.message = message


class _FakeErrorMessage:
    """Stand-in for a GST_MESSAGE_ERROR bus message."""

    def __init__(self, element_name, error_message, debug=None):
        self.type = "ERROR"
        self._error = _FakeGLibError(error_message)
        self._debug = debug
        self.src = (types.SimpleNamespace(get_name=lambda: element_name)
                    if element_name is not None else None)

    def parse_error(self):
        return self._error, self._debug


class _FakeBus:
    """Records the bounded-wait drain call and returns a preconfigured
    terminal error message (or None)."""

    def __init__(self, error_message=None):
        self._error_message = error_message
        self.timed_pop_calls = []

    def add_signal_watch(self):
        pass

    def connect(self, signal, callback):
        self._callback = callback

    def timed_pop_filtered(self, timeout_ns, message_type):
        self.timed_pop_calls.append((timeout_ns, message_type))
        return self._error_message


class _FakePipeline:
    def __init__(self, bus, playing_return):
        self._bus = bus
        self._playing_return = playing_return
        self.state_changes = []

    def get_bus(self):
        return self._bus

    def set_state(self, state):
        self.state_changes.append(state)
        if state == "PLAYING":
            return self._playing_return
        return "SUCCESS"

    def get_by_name(self, name):
        return None


def _install_fake_gi(monkeypatch, pipeline):
    """Install a minimal fake ``gi``/``gi.repository`` exposing exactly
    the Gst/GLib surface run_gst_pipeline uses."""
    gst = types.SimpleNamespace(
        State=types.SimpleNamespace(PLAYING="PLAYING", NULL="NULL"),
        StateChangeReturn=types.SimpleNamespace(FAILURE="FAILURE",
                                                SUCCESS="SUCCESS"),
        MessageType=types.SimpleNamespace(ERROR="ERROR", EOS="EOS", TAG="TAG"),
        SECOND=_GST_SECOND,
        init=lambda *a, **k: None,
        parse_launch=lambda launch: pipeline,
    )

    class _FakeMainLoop:
        def __init__(self):
            self._running = False

        def run(self):
            self._running = True

        def quit(self):
            self._running = False

        def is_running(self):
            return self._running

    glib = types.SimpleNamespace(
        MainLoop=_FakeMainLoop,
        timeout_add_seconds=lambda seconds, callback: 1,
        source_remove=lambda source_id: None,
    )

    repository = types.ModuleType("gi.repository")
    repository.Gst = gst
    repository.GLib = glib
    gi_module = types.ModuleType("gi")
    gi_module.require_version = lambda *a, **k: None
    gi_module.repository = repository

    monkeypatch.setitem(sys.modules, "gi", gi_module)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)


# ---------------------------------------------------------------------------
# 12.14: run_gst_pipeline drains the bus on a synchronous PLAYING failure
# ---------------------------------------------------------------------------

class TestRunGstPipelineBusDrain:
    def test_synchronous_failure_drains_bus_error_with_element_and_detail(
            self, monkeypatch):
        """On set_state(PLAYING) == FAILURE the harness drains the bus for
        the terminal ERROR and returns the failing element name plus its
        backend detail — not the generic PLAYING message (12.14)."""
        bus = _FakeBus(_FakeErrorMessage(
            "emltriton0",
            "Failed to load model 'defect_model' on CPU Triton: "
            "unsupported operator",
            debug="gstemltriton.c(412): triton backend init failed"))
        pipeline = _FakePipeline(bus, playing_return="FAILURE")
        _install_fake_gi(monkeypatch, pipeline)
        store = ResultsStore([])

        tag_values, error = harness.run_gst_pipeline("launch ! string", [],
                                                     store)

        # The bus was drained with a bounded wait derived from the
        # STATE_CHANGE_ERROR_DRAIN_SEC constant (in nanoseconds).
        assert bus.timed_pop_calls == [
            (harness.STATE_CHANGE_ERROR_DRAIN_SEC * _GST_SECOND, "ERROR")]
        assert tag_values == {}
        assert error["element"] == "emltriton0"
        # Backend detail (message + debug) is surfaced ...
        assert "Failed to load model 'defect_model'" in error["message"]
        assert "unsupported operator" in error["message"]
        assert "triton backend init failed" in error["message"]
        # ... instead of the opaque generic message.
        assert "failed to change state to PLAYING" not in error["message"]
        # The pipeline was still torn down to NULL in the finally block.
        assert "NULL" in pipeline.state_changes

    def test_synchronous_failure_without_bus_error_falls_back_generic(
            self, monkeypatch):
        """When no terminal ERROR arrives within the bounded wait, fall
        back to the generic message with no attributable element (12.14)."""
        bus = _FakeBus(error_message=None)
        pipeline = _FakePipeline(bus, playing_return="FAILURE")
        _install_fake_gi(monkeypatch, pipeline)
        store = ResultsStore([])

        tag_values, error = harness.run_gst_pipeline("launch ! string", [],
                                                     store)

        assert bus.timed_pop_calls == [
            (harness.STATE_CHANGE_ERROR_DRAIN_SEC * _GST_SECOND, "ERROR")]
        assert error == {"element": None,
                         "message": "Pipeline failed to change state to "
                                    "PLAYING"}


# ---------------------------------------------------------------------------
# execute() attribution: shared runner (S3/dataset/GStreamer mocked)
# ---------------------------------------------------------------------------

def _run_execute(monkeypatch, document, pipeline_result,
                 staged_models=None, staged_by_node=None):
    """Run harness.execute over ``document`` with the S3/dataset layers
    mocked and run_gst_pipeline returning ``pipeline_result``; returns
    ``(exit_code, final_results_document, document)``.

    ``pipeline_result`` may be a single ``(tag_values, error)`` tuple or
    a list of them delivered to successive run_gst_pipeline calls (the
    last repeats) — the list form drives the model-load fallback re-run
    (first call fails, second succeeds).

    When ``staged_models`` is given, STAGED_MODELS is set and
    model_staging.download_and_stage_best_effort is mocked to return
    ``(staged_by_node, [])`` so the real realize_inference_elements
    rewrites the document's sim stubs into emltriton (12.16)."""
    monkeypatch.delenv("SIMULATED_INFERENCE", raising=False)
    monkeypatch.delenv("STAGING_FALLBACKS", raising=False)
    if staged_models is None:
        monkeypatch.delenv("STAGED_MODELS", raising=False)
    else:
        monkeypatch.setenv("STAGED_MODELS", json.dumps(staged_models))
        monkeypatch.setattr(
            harness.model_staging, "download_and_stage_best_effort",
            lambda s3, bucket, entries, repo, workdir: (dict(staged_by_node),
                                                        []))

    monkeypatch.setattr(harness, "download_dataset",
                        lambda s3, bucket, prefix, target: {"a.jpg": "/x/a.jpg"})
    monkeypatch.setattr(harness.dataset_module, "stage_dataset",
                        lambda files, staging: "/x/ds/frame_%05d.jpg")
    monkeypatch.setattr(
        harness, "load_custom_plugins_manifest",
        lambda s3, bucket, key: dict(harness.EMPTY_CUSTOM_PLUGINS_MANIFEST))

    sequence = (list(pipeline_result)
                if isinstance(pipeline_result, list) else [pipeline_result])
    calls = {"n": 0}

    def fake_run(launch, sim_sources, store):
        index = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[index]

    monkeypatch.setattr(harness, "run_gst_pipeline", fake_run)

    document = json.loads(json.dumps(document))
    snapshots = []
    store = ResultsStore(renderer.all_node_ids(document), snapshots.append)
    exit_code = harness.execute(None, "bucket", "results.json", "prefix/",
                                document, store)
    return exit_code, snapshots[-1], document


# ---------------------------------------------------------------------------
# 12.15: element-named failure attributed to its owning node, detail kept
# ---------------------------------------------------------------------------

class TestExecuteElementAttribution:
    def test_emltriton_error_attributed_to_inference_node_with_detail(
            self, monkeypatch):
        """A bus error naming ``emltriton0`` is attributed to the model
        inference node with the backend detail in the message — NOT the
        source node and NOT the generic PLAYING message (12.15)."""
        error = {
            "element": "emltriton0",
            "message": "Pipeline failed with: Failed to load model "
                       "'defect_model' on CPU Triton: unsupported operator. "
                       "gstemltriton.c(412): triton backend init failed",
        }
        exit_code, report, _ = _run_execute(
            monkeypatch, INFERENCE_DOCUMENT, ({}, error))

        assert exit_code == 1
        records = {r["nodeId"]: r for r in report["nodes"]}

        # Attributed to the inference node, with the backend detail and
        # the failing element name in the message.
        inf = records["inf"]
        assert inf["status"] == STATUS_FAILED
        assert inf["error"]["code"] == "PIPELINE_EXECUTION_ERROR"
        assert "Failed to load model 'defect_model'" in inf["error"]["message"]
        assert "unsupported operator" in inf["error"]["message"]
        assert "(element emltriton0)" in inf["error"]["message"]
        assert "failed to change state to PLAYING" not in inf["error"]["message"]

        # NOT the source node: the Folder/Camera source is not failed and
        # carries no error (it is skipped, not defaulted onto).
        src = records["src"]
        assert src["status"] != STATUS_FAILED
        assert src["error"] is None
        assert src["status"] == STATUS_SKIPPED

        # No run-level (unattributed) error was recorded — the element
        # mapped to a node.
        assert all(r["nodeId"] is not None for r in report["nodes"])


# ---------------------------------------------------------------------------
# 12.15: unresolvable element -> run-level error, never defaulted to source
# ---------------------------------------------------------------------------

class TestExecuteRunLevelAttribution:
    def test_unresolvable_element_records_run_level_error_not_source(
            self, monkeypatch):
        """A synchronous failure whose element cannot be mapped to a node
        (generic PLAYING fallback) records a run-level error rather than
        defaulting onto the first/source node (12.15)."""
        error = {"element": None,
                 "message": "Pipeline failed to change state to PLAYING"}
        exit_code, report, _ = _run_execute(
            monkeypatch, INFERENCE_DOCUMENT, ({}, error))

        assert exit_code == 1

        # A run-level record (nodeId null) carries the failure.
        run_records = [r for r in report["nodes"] if r["nodeId"] is None]
        assert len(run_records) == 1
        run_error = run_records[0]
        assert run_error["status"] == STATUS_FAILED
        assert run_error["error"]["code"] == "PIPELINE_EXECUTION_ERROR"
        assert run_error["error"]["message"] == \
            "Pipeline failed to change state to PLAYING"

        # No real node was marked failed — in particular the source node
        # was not defaulted onto (the pre-fix regression).
        node_records = {r["nodeId"]: r for r in report["nodes"]
                        if r["nodeId"] is not None}
        assert all(r["status"] != STATUS_FAILED for r in node_records.values())
        assert node_records["src"]["error"] is None
        assert node_records["src"]["status"] == STATUS_SKIPPED


# ---------------------------------------------------------------------------
# 12.16/12.17: staged model unusable on CPU Triton -> NON-FATAL stub fallback
#
# SUPERSEDED (task 11.12): the earlier assertion that a staged-model load
# failure fails the run attributed to the inference node no longer holds.
# Best-effort inference (Requirements 12.16, 12.17) makes a model-load
# failure NON-FATAL — the harness reverts the node to its sim_inference
# stub, re-runs once, injects the simulated outcome, and the run succeeds
# with the node marked inferenceMode "simulated" + fallbackReason. The
# full formalization lives in task 11.14; this class is updated here to
# assert the fallback rather than the old failure so the suite stays green.
# ---------------------------------------------------------------------------

class TestStagedModelCpuInferenceFallback:
    def test_staged_model_unusable_on_cpu_falls_back_to_simulated(
            self, monkeypatch):
        """A staged model that cannot be served on CPU Triton does NOT
        fail the run: the inference node reverts to its stub, the pipeline
        re-runs, the configured simulated outcome is injected, and the
        node is marked inferenceMode "simulated" with the model-load error
        as its fallbackReason (12.16, 12.17, 12.18)."""
        staged_models = [{"nodeId": "inf", "modelName": "defect_model",
                          "s3Key": "prefix/models/defect_model.zip"}]
        # First run: the CPU-Triton load failure posts a bus error naming
        # the emltriton element the staging path realized (auto-named
        # emltriton0 by parse_launch). Second run (after revert): success.
        error = {
            "element": "emltriton0",
            "message": "Pipeline failed with: model 'defect_model' load "
                       "failed: CPUExecutionProvider cannot run node "
                       "'Conv_0' (unsupported opset). triton_backend.cc(220)",
        }
        exit_code, report, document = _run_execute(
            monkeypatch, STAGED_INFERENCE_DOCUMENT,
            [({}, error), ({}, None)],
            staged_models=staged_models,
            staged_by_node={"inf": "defect_model"})

        # The run succeeded despite the model-load failure (never fatal).
        assert exit_code == 0

        # The emltriton element the staging path realized was reverted
        # back to the sim_inference stub for the re-run — no real inference
        # element survives.
        assert renderer.nodes_with_factory(document, "emltriton") == []
        assert renderer.sim_inference_node_ids(document) == ["inf"]

        records = {r["nodeId"]: r for r in report["nodes"]}
        inf = records["inf"]
        # Completed (not failed) and marked simulated with the model-load
        # error captured as the fallbackReason (12.18).
        assert inf["status"] != STATUS_FAILED
        assert inf["error"] is None
        assert inf["inferenceMode"] == "simulated"
        assert "model 'defect_model' load failed" in inf["fallbackReason"]
        assert "CPUExecutionProvider" in inf["fallbackReason"]
        assert "(element emltriton0)" in inf["fallbackReason"]

        # The simulated_inference stub activity carries the same
        # fallback metadata for the report.
        activities = [a for a in inf["stubActivity"]
                      if a.get("type") == "simulated_inference"]
        assert len(activities) == 1
        assert activities[0]["inferenceMode"] == "simulated"
        assert "model 'defect_model' load failed" in \
            activities[0]["fallbackReason"]

        # No node was failed and no run-level error was recorded.
        assert all(r["status"] != STATUS_FAILED for r in report["nodes"])
        assert all(r["nodeId"] is not None for r in report["nodes"])

    def test_staged_model_generic_playing_failure_falls_back_to_simulated(
            self, monkeypatch):
        """The live failure this fix pins: a staged emltriton element
        returns STATE_CHANGE_FAILURE WITHOUT posting a bus ERROR, so
        run_gst_pipeline returns the GENERIC unattributable failure
        ``{"element": None, "message": "Pipeline failed to change state to
        PLAYING"}``. With a staged model present this must NOT fail the
        run: even though nothing maps back to the staged node, the harness
        reverts to the sim_inference stub, re-runs, injects the configured
        simulated outcome, and marks the node inferenceMode "simulated"
        with the improved unattributable fallbackReason (12.16, 12.17,
        12.18)."""
        staged_models = [{"nodeId": "inf", "modelName": "defect_model",
                          "s3Key": "prefix/models/defect_model.zip"}]
        # First run: the CPU-Triton load failure surfaces only as the
        # opaque state-change failure with no attributable element.
        # Second run (after revert): success.
        error = {"element": None,
                 "message": "Pipeline failed to change state to PLAYING"}
        exit_code, report, document = _run_execute(
            monkeypatch, STAGED_INFERENCE_DOCUMENT,
            [({}, error), ({}, None)],
            staged_models=staged_models,
            staged_by_node={"inf": "defect_model"})

        # The run succeeded despite the unattributable model-load failure.
        assert exit_code == 0

        # The emltriton element the staging path realized was reverted
        # back to the sim_inference stub for the re-run.
        assert renderer.nodes_with_factory(document, "emltriton") == []
        assert renderer.sim_inference_node_ids(document) == ["inf"]

        records = {r["nodeId"]: r for r in report["nodes"]}
        inf = records["inf"]
        # Completed (not failed) and marked simulated with the improved
        # unattributable fallbackReason (12.18) — not the opaque generic
        # state-change text.
        assert inf["status"] != STATUS_FAILED
        assert inf["error"] is None
        assert inf["inferenceMode"] == "simulated"
        assert "could not be loaded on CPU Triton" in inf["fallbackReason"]
        assert "the configured simulated outcome was used instead" in \
            inf["fallbackReason"]
        assert "failed to change state to PLAYING" not in inf["fallbackReason"]

        # The simulated_inference stub activity carries the same fallback.
        activities = [a for a in inf["stubActivity"]
                      if a.get("type") == "simulated_inference"]
        assert len(activities) == 1
        assert activities[0]["inferenceMode"] == "simulated"
        assert "could not be loaded on CPU Triton" in \
            activities[0]["fallbackReason"]

        # No node was failed and no run-level error was recorded.
        assert all(r["status"] != STATUS_FAILED for r in report["nodes"])
        assert all(r["nodeId"] is not None for r in report["nodes"])

    def test_failure_attributed_to_non_staged_node_does_not_fall_back(
            self, monkeypatch):
        """Boundary: with a staged model present, a first-run bus error
        naming an element that maps to a DIFFERENT, non-staged node (e.g.
        the capture sink) is a GENUINE failure — it is NOT a model-load
        fallback. The staged emltriton node is NOT reverted, the run
        fails, and the failure is attributed to that non-staged node
        (preserves the 12.14/12.15 path)."""
        # A source/sink-style document with a staged inference node plus a
        # distinct non-staged sink node the bus error will name.
        document = {
            "segments": [_segment([
                _element("multifilesrc", "src", location="{dataset_location}"),
                _element("jpegparse", "src"),
                _element("jpegdec", "src"),
                _element("videoconvert", "src"),
                _element("capsfilter", "inf", caps="video/x-raw,format=RGB"),
                _element("identity", "inf", name="sim_inference_inf"),
                _element("fakesink", "sink"),
            ])],
            "executorBindings": [],
        }
        staged_models = [{"nodeId": "inf", "modelName": "defect_model",
                          "s3Key": "prefix/models/defect_model.zip"}]
        # The bus error names the sink element (auto-named fakesink0),
        # which maps to the non-staged "sink" node — not the staged model.
        error = {
            "element": "fakesink0",
            "message": "Pipeline failed with: Internal data stream error. "
                       "gstbasesink.c(6017): could not negotiate",
        }
        exit_code, report, document = _run_execute(
            monkeypatch, document, ({}, error),
            staged_models=staged_models,
            staged_by_node={"inf": "defect_model"})

        # Genuine failure: the run fails (no fallback re-run).
        assert exit_code == 1

        # The staged emltriton element was NOT reverted — the staged
        # model realization survives (no model-load fallback fired).
        assert renderer.nodes_with_factory(document, "emltriton") == ["inf"]
        assert renderer.sim_inference_node_ids(document) == []

        records = {r["nodeId"]: r for r in report["nodes"]}
        # Attributed to the non-staged sink node, with the backend detail.
        sink = records["sink"]
        assert sink["status"] == STATUS_FAILED
        assert sink["error"]["code"] == "PIPELINE_EXECUTION_ERROR"
        assert "(element fakesink0)" in sink["error"]["message"]
        assert "could not negotiate" in sink["error"]["message"]

        # The staged inference node was NOT failed and NOT marked a
        # simulated fallback — it was never reverted.
        inf = records["inf"]
        assert inf["status"] != STATUS_FAILED
        assert inf["fallbackReason"] is None
        assert [a for a in inf["stubActivity"]
                if a.get("type") == "simulated_inference"] == []


# ---------------------------------------------------------------------------
# 12.18: a staged model that loads and serves runs REAL inference
#
# The success counterpart of the fallback above: when the staged
# emltriton element loads its CPU model and the pipeline reaches PLAYING
# on the first run (no bus error), the node executes the real model. The
# harness attaches the inference-metadata output to the surviving
# emltriton element and marks the node inferenceMode "real" with no
# fallbackReason — it is never reverted to a sim_inference stub and never
# injects a simulated outcome (Requirement 12.18).
# ---------------------------------------------------------------------------

class TestExecuteRealInference:
    def test_staged_model_that_loads_runs_real_inference(self, monkeypatch):
        """A staged model whose emltriton element loads and serves on CPU
        Triton (run_gst_pipeline succeeds on the first call) runs real
        inference: the node is marked inferenceMode "real" with no
        fallbackReason, carries the inference-metadata output, keeps no
        sim_inference stub, and injects no simulated outcome (12.18)."""
        staged_models = [{"nodeId": "inf", "modelName": "defect_model",
                          "s3Key": "prefix/models/defect_model.zip"}]
        # The real model emitted its inference tags; the pipeline reached
        # PLAYING and ran to EOS without a bus error on the FIRST call.
        tags = {"is_anomalous": True, "confidence": 0.87}

        # Step 4 uploads the inference metadata via the S3 client, which
        # is None in _run_execute — stub the upload to a fixed key.
        monkeypatch.setattr(
            harness, "upload_node_output",
            lambda s3, bucket, results_key, node_id, payload:
                "prefix/outputs/{0}.json".format(node_id))

        exit_code, report, document = _run_execute(
            monkeypatch, STAGED_INFERENCE_DOCUMENT,
            (tags, None),
            staged_models=staged_models,
            staged_by_node={"inf": "defect_model"})

        # The run succeeded.
        assert exit_code == 0

        # The staged stub was realized into a real emltriton element and
        # never reverted — no sim_inference stub survives for the node.
        assert renderer.nodes_with_factory(document, "emltriton") == ["inf"]
        assert renderer.sim_inference_node_ids(document) == []

        records = {r["nodeId"]: r for r in report["nodes"]}
        inf = records["inf"]
        # Completed with a real inference run and no fallback (12.18).
        assert inf["status"] != STATUS_FAILED
        assert inf["error"] is None
        assert inf["inferenceMode"] == "real"
        assert inf["fallbackReason"] is None

        # The real inference metadata (the emitted tags) is attached as
        # the node's output.
        outputs = [o for o in inf["outputs"]
                   if o.get("type") == "inference_metadata"]
        assert len(outputs) == 1
        assert outputs[0]["tags"] == tags
        assert outputs[0]["s3Key"] == "prefix/outputs/inf.json"

        # No simulated outcome was injected for a real run: no
        # simulated_inference stub activity on the node.
        assert [a for a in inf["stubActivity"]
                if a.get("type") == "simulated_inference"] == []

        # No node failed and no run-level error was recorded.
        assert all(r["status"] != STATUS_FAILED for r in report["nodes"])
        assert all(r["nodeId"] is not None for r in report["nodes"])
