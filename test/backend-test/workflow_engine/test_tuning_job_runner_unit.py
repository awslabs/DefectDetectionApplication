# Copyright 2026 Amazon Web Services, Inc.
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
"""Device unit tests for the Device_Score_Job runner (spec task 3.5).

The design's unit-test list for the device side reads: "job runner shadow
delta handling, cancel flag, batch sizes, ``reported`` updates, separate
worker". Those mechanisms are covered here as ENUMERATED expectations —
the invariant over them is Property 11's device half in
``test_property_tuning_job_runner.py`` (spec task 3.4), so nothing here
draws inputs.

Everything is a fake: a dict-backed S3 client holds the manifest, the
sample images and the outcome batches; a dict-backed shadow accessor
holds ``desired.jobs`` / ``reported.jobs`` with IoT's merge semantics (a
null value removes a key); the Text_Generation_API is a recording
invoker; the Greengrass IPC stream handler is built against a stub
``awsiot`` module. No AWS call, no device, no network.

Requirements: 6.9, 6.15.
"""
import base64
import copy
import json
import os
import sys
import threading
import types

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.tuning import job_runner as job_runner_module
from workflow_engine.tuning.job_runner import (
    BATCH_BACKOFF_BASE_SECONDS,
    JOB_CONCURRENCY,
    MAX_BATCH_ATTEMPTS,
    MAX_REPEATS,
    MIN_REPEATS,
    OUTCOME_BATCH_SIZE,
    OUTCOME_SCHEMA_VERSION,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TUNING_SHADOW_NAME,
    JobManifest,
    JobRunner,
    job_runner,
    make_tuning_shadow_handler,
    manifest_key,
    outcomes_key,
    outcomes_prefix,
    set_job_runner,
    shutdown_job_runner,
    start_job_runner,
    tuning_delta_topic_prefix,
    tuning_root,
)
from workflow_engine.tuning.sample_export import ExportConfig

BUCKET = "dda-inference-results-000000000000"
PREFIX = "workflow-tuning/samples/"
THING_NAME = "dda-edge-under-test"
WORKFLOW_ID = "wf-24680"
NODE_ID = "llm_1"
SESSION_ID = "sess-1a2b"
RUN_ID = "run-9f8e"
JOB_ID = "job-5c4d"

CONFIG = ExportConfig(bucket=BUCKET, prefix=PREFIX)

VERDICT_TRUE = '{"is_anomalous": true, "confidence": 0.91}'
VERDICT_FALSE = '{"is_anomalous": false, "confidence": 0.08}'
UNPARSEABLE = "the plate looks fine to me"

NODE_PARAMETERS = {"modelName": "qwen2-vl-2b", "temperature": 0.7,
                   "top_p": 0.9}
PROMPT_SET = {"prompt_template": "Inspect {part_id}.",
              "system_prompt": "You are a QA inspector.", "max_tokens": 192}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    """A dict-backed S3 client; ``fail_keys`` always rejects a put."""

    def __init__(self, objects=None, fail_keys=()):
        self.objects = dict(objects or {})
        self.gets = []
        self.puts = []
        self.fail_keys = set(fail_keys)

    def get_object(self, Bucket=None, Key=None):
        self.gets.append(Key)
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey: " + str(Key))
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.puts.append(Key)
        if Key in self.fail_keys:
            raise RuntimeError("fake S3 rejected " + str(Key))
        self.objects[Key] = Body
        return {}


class FakeShadow:
    """A named-shadow accessor with IoT's merge semantics."""

    def __init__(self, desired=None):
        self.state = {
            "desired": {"jobs": dict(desired or {})},
            "reported": {"jobs": {}},
        }
        self.reports = []
        self.reads = 0

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        self.reads += 1
        assert (thing_name, shadow_name) == (THING_NAME, TUNING_SHADOW_NAME)
        return copy.deepcopy(self.state)

    def update_thing_shadow_state_request(self, thing_name, shadow_name,
                                         payload):
        jobs = (payload or {}).get("reported", {}).get("jobs", {})
        for identifier, entry in jobs.items():
            self.reports.append((identifier, copy.deepcopy(entry)))
            if entry is None:
                self.state["reported"]["jobs"].pop(identifier, None)
            else:
                self.state["reported"]["jobs"][identifier] = copy.deepcopy(
                    entry)
        return b"{}"

    def entries(self, job_id=JOB_ID):
        return [entry for identifier, entry in self.reports
                if identifier == job_id]

    def statuses(self, job_id=JOB_ID):
        return [entry["status"] for entry in self.entries(job_id)
                if entry is not None]


class RecordingInvoker:
    """The Text_Generation_API, faked: records calls, reports metrics and
    proves the replay never overlaps itself nor runs on the caller's
    thread."""

    def __init__(self, answers=None, on_call=None, output_tokens=42):
        self.answers = answers
        self.on_call = on_call
        self.output_tokens = output_tokens
        self.calls = []
        self.threads = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, *args, system_prompt=None, metrics_sink=None):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            index = len(self.calls)
            self.calls.append({"positional": list(args),
                               "system_prompt": system_prompt})
            self.threads.append(threading.current_thread())
            if metrics_sink is not None:
                metrics_sink({"output_tokens": self.output_tokens})
            if self.on_call is not None:
                self.on_call(index)
            answer = (self.answers[index % len(self.answers)]
                      if self.answers else VERDICT_TRUE)
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            with self._lock:
                self._in_flight -= 1


class _NoRegistrations:
    """A session with no registrations, so the manifest's Node_Parameters
    stand alone."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return []


def no_registrations():
    return _NoRegistrations()


def manifest_document(sample_count=1, repeats=1, labels=("NOK",),
                      with_reference=True, **overrides):
    """A manifest document plus the S3 objects its samples read."""
    objects = {}
    samples = []
    for index in range(sample_count):
        sample_id = "sample-{0:03d}".format(index)
        input_key = "{0}{1}/{2}/dev/{3}.input.jpg".format(
            PREFIX, WORKFLOW_ID, NODE_ID, sample_id)
        objects[input_key] = b"\xff\xd8input-" + sample_id.encode("ascii")
        entry = {"sampleId": sample_id, "inputKey": input_key,
                 "label": labels[index % len(labels)],
                 "metadataSnippet": {"part_id": "part-{0}".format(index)}}
        if with_reference:
            reference_key = input_key[:-len(".input.jpg")] + ".reference.jpg"
            objects[reference_key] = (
                b"\xff\xd8reference-" + sample_id.encode("ascii"))
            entry["referenceKey"] = reference_key
        samples.append(entry)
    document = {
        "jobId": JOB_ID,
        "sessionId": SESSION_ID,
        "runId": RUN_ID,
        "workflowId": WORKFLOW_ID,
        "nodeId": NODE_ID,
        "nodeParameters": dict(NODE_PARAMETERS),
        "promptSet": dict(PROMPT_SET),
        "repeats": repeats,
        "samples": samples,
    }
    document.update(overrides)
    objects[manifest_key(PREFIX, JOB_ID)] = json.dumps(document).encode(
        "utf-8")
    return document, objects


def build_runner(s3, shadow, invoker=None, **kwargs):
    kwargs.setdefault("session_factory", no_registrations)
    kwargs.setdefault("backoff_base_seconds", 0.0)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return JobRunner(
        CONFIG, shadow_accessor=shadow, s3_factory=lambda: s3,
        thing_name=THING_NAME,
        invoker=invoker or RecordingInvoker(), **kwargs)


def run_to_completion(runner, timeout=30.0):
    try:
        runner.sync()
        assert runner.wait_idle(timeout), "the job worker did not finish"
    finally:
        runner.stop(2.0)


def batches(s3, session_id=SESSION_ID, run_id=RUN_ID):
    """The outcome batch documents in order, 1..n."""
    documents = []
    index = 1
    while True:
        key = outcomes_key(PREFIX, session_id, run_id, index)
        if key not in s3.objects:
            return documents
        documents.append(json.loads(s3.objects[key].decode("utf-8")))
        index += 1


# ===========================================================================
# S3 layout
# ===========================================================================

@pytest.mark.parametrize("prefix,expected_root", [
    ("workflow-tuning/samples/", "workflow-tuning/"),
    ("tenant-a/workflow-tuning/samples/", "tenant-a/workflow-tuning/"),
    # A prefix that is not the samples prefix becomes the root as-is.
    ("custom/tuning/", "custom/tuning/"),
    ("custom/tuning", "custom/tuning/"),
    ("", ""),
    (None, ""),
])
def test_tuning_root_derivation(prefix, expected_root):
    """Jobs and sessions are siblings of ``samples/`` under the same
    root, so a Use_Case never writes outside its own prefix."""
    assert tuning_root(prefix) == expected_root


def test_manifest_and_outcome_keys_are_the_documented_ones():
    """The design's job/outcome layout, literally (Requirement 9.6)."""
    assert manifest_key(PREFIX, JOB_ID) == (
        "workflow-tuning/jobs/job-5c4d/manifest.json")
    assert outcomes_prefix(PREFIX, SESSION_ID, RUN_ID) == (
        "workflow-tuning/sessions/sess-1a2b/runs/run-9f8e/")
    assert outcomes_key(PREFIX, SESSION_ID, RUN_ID, 1) == (
        "workflow-tuning/sessions/sess-1a2b/runs/run-9f8e/outcomes-1.json")
    # Un-padded numbering: the Portal ingests by set difference, not by
    # lexicographic order.
    assert outcomes_key(PREFIX, SESSION_ID, RUN_ID, 12).endswith(
        "outcomes-12.json")


# ===========================================================================
# Manifest parsing
# ===========================================================================

@pytest.mark.parametrize("document", [
    None, "manifest", [1], {}, {"sessionId": SESSION_ID},
    {"runId": RUN_ID}, {"sessionId": "", "runId": RUN_ID},
])
def test_a_manifest_without_session_and_run_is_unusable(document):
    """The outcome objects are keyed by session/run, so a manifest
    missing either cannot be executed at all."""
    assert JobManifest.from_document(document, JOB_ID) is None


@pytest.mark.parametrize("raw,expected", [
    (1, 1), (2, 2), (3, 3), ("2", 2),
    # Out of range or unusable values clamp to the 1..3 bound.
    (0, 1), (-5, 1), (9, 3), (None, 1), ("abc", 1), (2.9, 2), (True, 1),
])
def test_repeats_are_clamped_to_one_to_three(raw, expected):
    """The Portal enforces 1..3; the device clamps defensively so a
    malformed manifest cannot issue unbounded invocations."""
    document, _objects = manifest_document(repeats=raw)
    manifest = JobManifest.from_document(document, JOB_ID)
    assert manifest.repeats == expected
    assert manifest.total == len(manifest.samples) * expected


@pytest.mark.parametrize("entry", [
    "sample-x",                                        # not a mapping
    {"inputKey": "k", "label": "OK"},                  # no id
    {"sampleId": "s", "label": "OK"},                  # no input key
    {"sampleId": "s", "inputKey": "k"},                # unlabelled
    {"sampleId": "s", "inputKey": "k", "label": ""},   # blank label
    {"sampleId": "s", "inputKey": "k", "label": "EXCLUDE"},
    {"sampleId": "s", "inputKey": "k", "label": "MAYBE"},
])
def test_unusable_samples_are_counted_and_excluded_from_total(entry):
    """Only labelled, non-excluded samples are scored (Requirement 4.3),
    and the excluded ones are out of ``total`` so ``done == total`` stays
    reachable."""
    document, _objects = manifest_document(sample_count=2)
    document["samples"].append(entry)
    manifest = JobManifest.from_document(document, JOB_ID)
    assert manifest.unusable == 1
    assert len(manifest.samples) == 2
    assert manifest.total == 2


def test_manifest_fields_and_defaults():
    """A well-formed manifest keeps its identifiers, parameters and
    Prompt_Set; malformed sections degrade to empty mappings."""
    document, _objects = manifest_document(
        sample_count=2, repeats=2, labels=("OK", "NOK"))
    manifest = JobManifest.from_document(document, "other-job")
    assert manifest.job_id == JOB_ID  # the document wins over the shadow id
    assert (manifest.session_id, manifest.run_id) == (SESSION_ID, RUN_ID)
    assert (manifest.workflow_id, manifest.node_id) == (WORKFLOW_ID, NODE_ID)
    assert manifest.node_parameters == NODE_PARAMETERS
    assert manifest.prompt_set == PROMPT_SET
    assert manifest.total == 4
    assert [entry.label for entry in manifest.samples] == ["OK", "NOK"]
    assert manifest.samples[0].metadata_snippet == {"part_id": "part-0"}
    assert manifest.outcomes_prefix_override is None
    # A lower-case label is normalized; malformed sections are dropped.
    document["samples"][0]["label"] = "ok"
    document["nodeParameters"] = "not a mapping"
    document["promptSet"] = [1]
    document["samples"] = document["samples"][:1]
    degraded = JobManifest.from_document(document, JOB_ID)
    assert degraded.samples[0].label == "OK"
    assert degraded.node_parameters == {}
    assert degraded.prompt_set == {}
    # An absent job id falls back to the shadow's.
    document.pop("jobId")
    assert JobManifest.from_document(document, "job-from-shadow").job_id == (
        "job-from-shadow")


# ===========================================================================
# Shadow delta handling and reconciliation (Requirement 6.9)
# ===========================================================================

class _AcceptingRunner(JobRunner):
    """A runner that records the jobs its worker picked up instead of
    executing them, so acceptance and worker semantics are observable on
    their own."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []
        self.threads = []
        self.gate = threading.Event()
        self.gate.set()

    def run_job(self, job_id, entry=None):
        self.executed.append((job_id, dict(entry or {})))
        self.threads.append(threading.current_thread())
        self.gate.wait(10.0)


def accepting_runner(shadow):
    return _AcceptingRunner(
        CONFIG, shadow_accessor=shadow, s3_factory=lambda: FakeS3(),
        thing_name=THING_NAME, session_factory=no_registrations)


def test_a_job_is_accepted_once_however_many_deltas_arrive():
    """A job stays in ``desired`` until the Portal finalizes it, so it
    must not be re-executed on every delta."""
    shadow = FakeShadow({JOB_ID: {"manifestKey": "k"}})
    runner = accepting_runner(shadow)
    try:
        runner.on_delta({"state": {}})
        runner.on_delta({"state": {}})
        runner.sync()
        assert runner.wait_idle(10.0)
    finally:
        runner.stop(2.0)
    assert [job_id for job_id, _entry in runner.executed] == [JOB_ID]
    assert runner.executed[0][1]["manifestKey"] == "k"
    # The acceptance is reported as queued, exactly once.
    assert shadow.statuses() == [STATUS_QUEUED]
    assert shadow.reads == 3


def test_two_jobs_run_one_at_a_time_on_the_runners_own_worker():
    """Concurrency 1 on a worker distinct from the caller
    (Requirement 6.15)."""
    shadow = FakeShadow({"job-a": {"manifestKey": "a"},
                         "job-b": {"manifestKey": "b"}})
    runner = accepting_runner(shadow)
    try:
        run_to_completion(runner, timeout=10.0)
    finally:
        runner.stop(2.0)
    assert sorted(job_id for job_id, _entry in runner.executed) == [
        "job-a", "job-b"]
    assert JOB_CONCURRENCY == 1
    for thread in runner.threads:
        assert thread is not threading.current_thread()
        assert thread.name == "tuning-job-runner"
        assert thread.daemon is True
    assert len({id(thread) for thread in runner.threads}) == 1


def test_the_cancel_flag_is_read_and_cleared_from_desired():
    """``desired.jobs[jobId].cancel`` governs cancellation and clearing
    it un-cancels the job (Requirement 6.11)."""
    shadow = FakeShadow({JOB_ID: {"manifestKey": "k", "cancel": True}})
    runner = accepting_runner(shadow)
    try:
        runner.sync()
        assert runner.is_cancelled(JOB_ID) is True
        shadow.state["desired"]["jobs"][JOB_ID]["cancel"] = False
        runner.sync()
        assert runner.is_cancelled(JOB_ID) is False
        # The shadow's string form counts too.
        shadow.state["desired"]["jobs"][JOB_ID]["cancel"] = "true"
        runner.sync()
        assert runner.is_cancelled(JOB_ID) is True
        for value in (False, "false", None, 1, "yes"):
            shadow.state["desired"]["jobs"][JOB_ID]["cancel"] = value
            runner.sync()
            assert runner.is_cancelled(JOB_ID) is False, value
    finally:
        runner.gate.set()
        runner.stop(2.0)


def test_a_stale_reported_entry_is_pruned_with_a_null_value():
    """The Portal removes a finalized job from ``desired``; its
    ``reported`` entry is then deleted (a shadow null removes the key)."""
    shadow = FakeShadow({JOB_ID: {"manifestKey": "k"}})
    runner = accepting_runner(shadow)
    try:
        runner.sync()
        assert runner.wait_idle(10.0)
        assert JOB_ID in shadow.state["reported"]["jobs"]
        shadow.state["desired"]["jobs"].pop(JOB_ID)
        runner.sync()
    finally:
        runner.stop(2.0)
    assert JOB_ID not in shadow.state["reported"]["jobs"]
    assert (JOB_ID, None) in shadow.reports


def test_a_running_job_is_never_pruned():
    """A job executing when its ``desired`` entry disappears keeps its
    bookkeeping so its final report still lands."""
    shadow = FakeShadow({JOB_ID: {"manifestKey": "k"}})
    runner = accepting_runner(shadow)
    runner.gate.clear()
    try:
        runner.sync()
        for _attempt in range(200):
            if runner.running_job == JOB_ID:
                break
            threading.Event().wait(0.01)
        assert runner.running_job == JOB_ID
        shadow.state["desired"]["jobs"].pop(JOB_ID)
        runner.sync()
        assert (JOB_ID, None) not in shadow.reports
        assert JOB_ID in shadow.state["reported"]["jobs"]
    finally:
        runner.gate.set()
        runner.stop(2.0)


@pytest.mark.parametrize("state", [
    None, False, {}, {"desired": None}, {"desired": {"jobs": "x"}},
    {"desired": {"jobs": {"": {}}}},
])
def test_an_unusable_shadow_state_accepts_no_job(state):
    """A missing or malformed shadow yields no job and no report."""

    class _Shadow:
        def get_thing_shadow_state_request(self, *args):
            return state

        def update_thing_shadow_state_request(self, *args):
            raise AssertionError("nothing to report")

    runner = accepting_runner(_Shadow())
    runner.sync()
    assert runner.pending_jobs == 0
    assert runner.executed == []


def test_a_shadow_read_failure_is_contained():
    """A transport error leaves the runner idle; the next delta retries."""

    class _Shadow:
        def get_thing_shadow_state_request(self, *args):
            raise RuntimeError("IPC unavailable")

        def update_thing_shadow_state_request(self, *args):
            raise AssertionError("nothing to report")

    runner = accepting_runner(_Shadow())
    runner.on_delta({"state": {}})
    assert runner.pending_jobs == 0


def test_a_reporting_failure_is_contained():
    """Reporting is best-effort: the Portal finalizes on its silence
    timeout rather than the device raising."""

    class _Shadow(FakeShadow):
        def update_thing_shadow_state_request(self, *args):
            raise RuntimeError("shadow update rejected")

    shadow = _Shadow({JOB_ID: {"manifestKey": "k"}})
    runner = accepting_runner(shadow)
    try:
        run_to_completion(runner, timeout=10.0)
    finally:
        runner.stop(2.0)
    assert [job_id for job_id, _entry in runner.executed] == [JOB_ID]


def test_on_delta_contains_a_sync_failure(monkeypatch):
    """A delta handler failure must never close the subscription
    stream."""
    runner = accepting_runner(FakeShadow())

    def _raise():
        raise RuntimeError("defective sync")

    monkeypatch.setattr(runner, "sync", _raise)
    runner.on_delta({"state": {}})  # contained


# ---------------------------------------------------------------------------
# The Greengrass IPC stream handler
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_awsiot(monkeypatch):
    """A stub ``awsiot.greengrasscoreipc.client`` so the shadow handler
    can be built off-device (the module imports it lazily)."""
    awsiot = types.ModuleType("awsiot")
    ggipc = types.ModuleType("awsiot.greengrasscoreipc")
    client = types.ModuleType("awsiot.greengrasscoreipc.client")

    class SubscribeToIoTCoreStreamHandler:
        pass

    client.SubscribeToIoTCoreStreamHandler = SubscribeToIoTCoreStreamHandler
    ggipc.client = client
    awsiot.greengrasscoreipc = ggipc
    monkeypatch.setitem(sys.modules, "awsiot", awsiot)
    monkeypatch.setitem(sys.modules, "awsiot.greengrasscoreipc", ggipc)
    monkeypatch.setitem(sys.modules, "awsiot.greengrasscoreipc.client",
                        client)
    return client


class _Event:
    def __init__(self, topic_name, payload):
        self.message = types.SimpleNamespace(
            topic_name=topic_name, payload=payload)


class _RecordingRunner:
    """The fields the shadow handler reads off a runner."""

    thing_name = THING_NAME
    shadow_name = TUNING_SHADOW_NAME

    def __init__(self):
        self.deltas = []

    def on_delta(self, message=None):
        self.deltas.append(message)


def test_delta_topic_prefix_is_the_named_shadow_update_prefix():
    """The subscription prefix the MQTT handler filters on."""
    assert tuning_delta_topic_prefix(THING_NAME) == (
        "$aws/things/dda-edge-under-test/shadow/name/dda-workflow-tuning/"
        "update/")


def test_the_shadow_handler_acts_only_on_delta_notifications(stub_awsiot):
    """``delta`` re-reads ``desired.jobs``; ``accepted``/``documents``
    need no edge-side action (the camera-bindings contract)."""
    runner = _RecordingRunner()
    handler = make_tuning_shadow_handler(runner)
    prefix = tuning_delta_topic_prefix(THING_NAME)
    payload = json.dumps({"state": {"jobs": {JOB_ID: {}}}}).encode("utf-8")
    handler.on_stream_event(_Event(prefix + "delta", payload))
    assert runner.deltas == [{"state": {"jobs": {JOB_ID: {}}}}]
    for subtopic in ("accepted", "rejected", "documents"):
        handler.on_stream_event(_Event(prefix + subtopic, payload))
    assert len(runner.deltas) == 1


def test_the_shadow_handler_contains_a_malformed_payload(stub_awsiot):
    """A defect in one message must not close the stream."""
    runner = _RecordingRunner()
    handler = make_tuning_shadow_handler(runner)
    prefix = tuning_delta_topic_prefix(THING_NAME)
    handler.on_stream_event(_Event(prefix + "delta", b"not json"))
    handler.on_stream_event(_Event(prefix + "delta", None))
    assert runner.deltas == []
    # The stream lifecycle callbacks follow the camera-bindings contract.
    assert handler.on_stream_error(RuntimeError("boom")) is True
    assert handler.on_stream_closed() is None


# ===========================================================================
# Execution: batch sizes, reported updates and cancellation
# ===========================================================================

def test_reported_updates_track_every_step_of_a_job():
    """``reported.jobs[jobId] = {status, done, total, updatedAt}`` at
    queued, each batch and the finalization (Requirement 6.9)."""
    document, objects = manifest_document(sample_count=3, repeats=2)
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {"manifestKey": manifest_key(PREFIX,
                                                              JOB_ID)}})
    invoker = RecordingInvoker([VERDICT_TRUE])
    runner = build_runner(s3, shadow, invoker, batch_size=2,
                         clock=lambda: 1_700_000_000.5)
    run_to_completion(runner)

    entries = shadow.entries()
    assert [(entry["status"], entry["done"], entry["total"])
            for entry in entries] == [
        (STATUS_QUEUED, 0, None),
        (STATUS_RUNNING, 0, 6),
        (STATUS_RUNNING, 2, 6),
        (STATUS_RUNNING, 4, 6),
        (STATUS_RUNNING, 6, 6),
        (STATUS_COMPLETED, 6, 6),
    ]
    for entry in entries:
        assert set(entry) == {"status", "done", "total", "updatedAt"}
        assert entry["updatedAt"] == 1_700_000_000
    assert shadow.state["reported"]["jobs"][JOB_ID] == entries[-1]


def test_batches_are_capped_at_twenty_outcomes():
    """Outcome batches carry at most 20 Sample_Outcomes, numbered from 1
    (Requirement 6.9)."""
    document, objects = manifest_document(sample_count=25,
                                          labels=("NOK", "OK"))
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})   # no manifestKey -> derived key
    invoker = RecordingInvoker([VERDICT_TRUE, VERDICT_FALSE, UNPARSEABLE])
    runner = build_runner(s3, shadow, invoker)
    run_to_completion(runner)

    documents = batches(s3)
    assert [len(entry["outcomes"]) for entry in documents] == [
        OUTCOME_BATCH_SIZE, 5]
    assert OUTCOME_BATCH_SIZE == 20
    assert [entry["batch"] for entry in documents] == [1, 2]
    for entry in documents:
        assert entry["schemaVersion"] == OUTCOME_SCHEMA_VERSION
        assert entry["jobId"] == JOB_ID
        assert entry["sessionId"] == SESSION_ID
        assert entry["runId"] == RUN_ID
        assert entry["thingName"] == THING_NAME
    # Nothing is written outside the run's own prefix (Requirement 9.6).
    run_prefix = outcomes_prefix(PREFIX, SESSION_ID, RUN_ID)
    assert all(key.startswith(run_prefix) for key in s3.puts)
    assert [entry["done"] for entry in shadow.entries()] == [0, 0, 20, 25, 25]
    assert shadow.statuses()[-1] == STATUS_COMPLETED


def test_an_outcome_carries_the_category_answer_and_metrics():
    """One replay's Sample_Outcome, field for field."""
    document, objects = manifest_document(sample_count=1, labels=("NOK",))
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    invoker = RecordingInvoker([VERDICT_TRUE], output_tokens=17)
    runner = build_runner(s3, shadow, invoker)
    run_to_completion(runner)

    outcome = batches(s3)[0]["outcomes"][0]
    assert outcome["sampleId"] == "sample-000"
    assert outcome["repeat"] == 1
    assert outcome["label"] == "NOK"
    assert outcome["category"] == "correct"
    assert outcome["isAnomalous"] is True
    assert outcome["confidence"] == 0.91
    assert outcome["rawAnswer"] == VERDICT_TRUE
    assert outcome["outputTokens"] == 17
    assert isinstance(outcome["latencyMs"], int)
    assert outcome["error"] is None
    assert outcome["parseError"] is None


def test_a_parse_failure_records_the_reason_beside_the_raw_answer():
    """Requirement 7.4: the parser's rejection reason, with ``error``
    left null so a parse failure stays a parse failure."""
    document, objects = manifest_document(sample_count=1, labels=("OK",))
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    runner = build_runner(s3, shadow, RecordingInvoker([UNPARSEABLE]))
    run_to_completion(runner)

    outcome = batches(s3)[0]["outcomes"][0]
    assert outcome["category"] == "parse_failure"
    assert outcome["rawAnswer"] == UNPARSEABLE
    assert outcome["parseError"]
    assert outcome["error"] is None
    assert outcome["isAnomalous"] is None


@pytest.mark.parametrize("kind", ["missing_input", "missing_reference",
                                  "unresolved_placeholder", "transport"])
def test_per_sample_failures_are_invocation_errors_not_job_failures(kind):
    """A per-sample problem is one ``invocation_error`` outcome and the
    run continues (Requirement 6.5)."""
    document, objects = manifest_document(sample_count=2)
    key = manifest_key(PREFIX, JOB_ID)
    first = document["samples"][0]
    if kind == "missing_input":
        objects.pop(first["inputKey"])
        expected_in_error = first["inputKey"]
    elif kind == "missing_reference":
        objects.pop(first["referenceKey"])
        expected_in_error = first["referenceKey"]
    elif kind == "unresolved_placeholder":
        first["metadataSnippet"] = {}
        expected_in_error = "part_id"
    else:
        expected_in_error = "the Text_Generation_API refused"
    objects[key] = json.dumps(document).encode("utf-8")
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    answers = ([RuntimeError("the Text_Generation_API refused"),
                VERDICT_TRUE] if kind == "transport" else [VERDICT_TRUE])
    runner = build_runner(s3, shadow, RecordingInvoker(answers))
    run_to_completion(runner)

    outcomes = batches(s3)[0]["outcomes"]
    assert len(outcomes) == 2
    failed = outcomes[0]
    assert failed["category"] == "invocation_error"
    assert expected_in_error in failed["error"]
    assert failed["isAnomalous"] is None
    assert failed["parseError"] is None
    # The second sample still ran, and the job completed.
    assert outcomes[1]["category"] in ("correct", "false_pass", "false_fail")
    assert shadow.statuses()[-1] == STATUS_COMPLETED
    assert shadow.entries()[-1]["done"] == 2


@pytest.mark.parametrize("kind,expected_fragment", [
    ("manifest_missing", "manifest.json"),
    ("manifest_unparseable", "could not read the job manifest"),
    ("manifest_malformed", "malformed"),
    ("no_model_name", "no modelName"),
])
def test_whole_job_preconditions_report_failed_without_invoking(
        kind, expected_fragment):
    """An unreadable/malformed manifest or an unresolvable model fails
    the job as a whole rather than producing one error per sample."""
    document, objects = manifest_document(sample_count=2)
    key = manifest_key(PREFIX, JOB_ID)
    if kind == "manifest_missing":
        objects.pop(key)
    elif kind == "manifest_unparseable":
        objects[key] = b"{not json"
    elif kind == "manifest_malformed":
        document.pop("sessionId")
        objects[key] = json.dumps(document).encode("utf-8")
    else:
        document["nodeParameters"] = {"temperature": 0.7}
        objects[key] = json.dumps(document).encode("utf-8")
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    invoker = RecordingInvoker([VERDICT_TRUE])
    runner = build_runner(s3, shadow, invoker)
    run_to_completion(runner)

    final = shadow.entries()[-1]
    assert final["status"] == STATUS_FAILED
    assert expected_fragment in final["error"]
    assert invoker.calls == []
    assert batches(s3) == []


def test_a_batch_write_failure_fails_the_job_after_three_attempts():
    """A batch the Portal never sees would make ``done == total``
    unreachable, so the job fails — after 3 attempts with doubling
    backoff."""
    delays = []
    document, objects = manifest_document(sample_count=2)
    failing = outcomes_key(PREFIX, SESSION_ID, RUN_ID, 1)
    s3 = FakeS3(objects, fail_keys=(failing,))
    shadow = FakeShadow({JOB_ID: {}})
    runner = build_runner(
        s3, shadow, RecordingInvoker([VERDICT_TRUE]),
        backoff_base_seconds=BATCH_BACKOFF_BASE_SECONDS,
        sleep=delays.append)
    run_to_completion(runner)

    assert s3.puts.count(failing) == MAX_BATCH_ATTEMPTS == 3
    assert delays == [BATCH_BACKOFF_BASE_SECONDS,
                      BATCH_BACKOFF_BASE_SECONDS * 2]
    final = shadow.entries()[-1]
    assert final["status"] == STATUS_FAILED
    assert failing in final["error"]
    assert final["done"] == 0


def test_a_batch_write_that_succeeds_on_retry_completes_the_job():
    """The retry lands, so the job is not failed by a transient error."""
    document, objects = manifest_document(sample_count=1)
    key = outcomes_key(PREFIX, SESSION_ID, RUN_ID, 1)

    class _FlakyS3(FakeS3):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failures = 2

        def put_object(self, Bucket=None, Key=None, Body=None,
                       ContentType=None):
            if Key == key and self.failures:
                self.failures -= 1
                self.puts.append(Key)
                raise RuntimeError("transient")
            return super().put_object(Bucket=Bucket, Key=Key, Body=Body,
                                      ContentType=ContentType)

    s3 = _FlakyS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    runner = build_runner(s3, shadow, RecordingInvoker([VERDICT_TRUE]))
    run_to_completion(runner)
    assert shadow.statuses()[-1] == STATUS_COMPLETED
    assert len(batches(s3)) == 1


def test_a_job_cancelled_before_it_starts_invokes_nothing():
    """Cancellation is checked before every unit, including the first
    (Requirement 6.11)."""
    document, objects = manifest_document(sample_count=3)
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {"cancel": True}})
    invoker = RecordingInvoker([VERDICT_TRUE])
    runner = build_runner(s3, shadow, invoker)
    run_to_completion(runner)

    final = shadow.entries()[-1]
    assert (final["status"], final["done"], final["total"]) == (
        STATUS_CANCELLED, 0, 3)
    assert invoker.calls == []
    assert batches(s3) == []


def test_a_job_with_no_replayable_samples_completes_at_zero():
    """An empty (or wholly unusable) manifest completes at 0/0 rather
    than hanging: the Portal's run finalizes immediately."""
    document, objects = manifest_document(sample_count=0)
    document["samples"] = [{"sampleId": "u", "inputKey": "k"}]
    objects[manifest_key(PREFIX, JOB_ID)] = json.dumps(document).encode(
        "utf-8")
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    invoker = RecordingInvoker([VERDICT_TRUE])
    runner = build_runner(s3, shadow, invoker)
    run_to_completion(runner)

    final = shadow.entries()[-1]
    assert (final["status"], final["done"], final["total"]) == (
        STATUS_COMPLETED, 0, 0)
    assert invoker.calls == []
    assert batches(s3) == []


def test_an_outcomes_prefix_override_is_honoured():
    """A manifest may name its own outcome prefix (still inside the
    Use_Case bucket)."""
    override = "workflow-tuning/sessions/other/runs/other/"
    document, objects = manifest_document(sample_count=1,
                                         outcomesPrefix=override)
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    runner = build_runner(s3, shadow, RecordingInvoker([VERDICT_TRUE]))
    run_to_completion(runner)
    assert s3.puts == [override + "outcomes-1.json"]


# ===========================================================================
# Parameter resolution
# ===========================================================================

def _registration_session_factory(tmp_path, parameters,
                                  node_type="llm_inference"):
    """A session factory over one registration whose ``workflow.json``
    declares ``NODE_ID`` with ``parameters``."""
    artifact_path = tmp_path / "registration"
    artifact_path.mkdir(exist_ok=True)
    (artifact_path / "workflow.json").write_text(json.dumps({
        "nodes": [{"id": NODE_ID, "type": node_type,
                   "parameters": parameters}]}))
    registration = types.SimpleNamespace(
        id="reg-1", workflow_id=WORKFLOW_ID, version="7",
        artifact_path=str(artifact_path), registered_at=1_000_000)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def query(self, *args):
            return self

        def filter(self, *args):
            return self

        def order_by(self, *args):
            return self

        def all(self):
            return [registration]

    return lambda: _Session()


def test_registered_parameters_override_the_manifests(tmp_path):
    """The replay targets the model the device actually serves, while the
    Candidate's Prompt_Set always wins over the registered prompt."""
    factory = _registration_session_factory(tmp_path, {
        "modelName": "qwen2-vl-7b", "temperature": 0.1,
        "prompt_template": "the registered prompt", "max_tokens": 64,
        "system_prompt": "the registered system prompt",
        "anomaly_mode": True})
    document, _objects = manifest_document(sample_count=1)
    manifest = JobManifest.from_document(document, JOB_ID)
    runner = JobRunner(CONFIG, session_factory=factory)
    resolved = runner.resolve_parameters(manifest)
    assert resolved["modelName"] == "qwen2-vl-7b"      # registered wins
    assert resolved["temperature"] == 0.1
    assert resolved["top_p"] == 0.9                    # manifest fallback
    assert resolved["prompt_template"] == PROMPT_SET["prompt_template"]
    assert resolved["system_prompt"] == PROMPT_SET["system_prompt"]
    assert resolved["max_tokens"] == PROMPT_SET["max_tokens"]
    assert resolved["anomaly_mode"] is True


def test_a_prompt_only_candidate_resolves_prompt_to_the_template():
    """A Prompt_Set carrying ``prompt`` (the Bedrock spelling) still
    renders as the llm node's template."""
    document, _objects = manifest_document(sample_count=1)
    document["promptSet"] = {"prompt": "Inspect {part_id} closely."}
    manifest = JobManifest.from_document(document, JOB_ID)
    runner = JobRunner(CONFIG, session_factory=no_registrations)
    resolved = runner.resolve_parameters(manifest)
    assert resolved["prompt_template"] == "Inspect {part_id} closely."


def test_resolution_without_a_model_name_fails_the_job():
    """No resolvable ``modelName``: the job fails as a whole."""
    document, _objects = manifest_document(sample_count=1)
    document["nodeParameters"] = {"temperature": 0.7}
    manifest = JobManifest.from_document(document, JOB_ID)
    runner = JobRunner(CONFIG, session_factory=no_registrations)
    with pytest.raises(ValueError) as error:
        runner.resolve_parameters(manifest)
    assert NODE_ID in str(error.value)
    assert WORKFLOW_ID in str(error.value)


def test_a_registration_lookup_failure_leaves_the_manifest_standing():
    """The registration read is contained: a database failure degrades to
    the manifest's Node_Parameters."""

    def _factory():
        raise RuntimeError("the run database is unavailable")

    document, _objects = manifest_document(sample_count=1)
    manifest = JobManifest.from_document(document, JOB_ID)
    runner = JobRunner(CONFIG, session_factory=_factory)
    assert runner.registered_parameters(WORKFLOW_ID, NODE_ID) == {}
    assert runner.resolve_parameters(manifest)["modelName"] == (
        NODE_PARAMETERS["modelName"])


def test_a_non_llm_or_unknown_node_resolves_to_no_registered_parameters(
        tmp_path):
    """Only an ``llm_inference`` Tunable_Node of the same id contributes
    registered parameters."""
    factory = _registration_session_factory(
        tmp_path, {"modelName": "qwen2-vl-7b", "anomaly_mode": True},
        node_type="bedrock_inference")
    runner = JobRunner(CONFIG, session_factory=factory)
    assert runner.registered_parameters(WORKFLOW_ID, NODE_ID) == {}
    assert runner.registered_parameters(WORKFLOW_ID, "other") == {}
    assert runner.registered_parameters("", NODE_ID) == {}


# ===========================================================================
# The process-wide runner
# ===========================================================================

def test_start_job_runner_is_inert_without_tuning_configuration():
    """No configuration: nothing installed, no shadow read, no client
    (Requirements 2.6, 11.3)."""

    class _Shadow:
        def get_thing_shadow_state_request(self, *args):
            raise AssertionError("an unconfigured device reads no shadow")

        def update_thing_shadow_state_request(self, *args):
            raise AssertionError("an unconfigured device reports nothing")

    set_job_runner(None)
    assert start_job_runner(
        types.SimpleNamespace(config=None, thing_name=THING_NAME),
        shadow_accessor=_Shadow()) is None
    assert job_runner() is None


def test_start_job_runner_installs_and_reconciles_once():
    """A configured device installs the runner, inherits the exporter's
    device name and performs the initial reconciliation so jobs
    delivered while it was down are picked up."""
    shadow = FakeShadow()
    try:
        runner = start_job_runner(
            types.SimpleNamespace(config=CONFIG, thing_name=THING_NAME),
            shadow_accessor=shadow, s3_factory=lambda: FakeS3(),
            session_factory=no_registrations)
        assert runner is not None
        assert job_runner() is runner
        assert runner.thing_name == THING_NAME
        assert runner.shadow_name == TUNING_SHADOW_NAME
        assert runner.config == CONFIG
        assert shadow.reads == 1
        # ``sync=False`` skips the reconciliation.
        second = start_job_runner(
            types.SimpleNamespace(config=CONFIG, thing_name=THING_NAME),
            shadow_accessor=shadow, s3_factory=lambda: FakeS3(),
            session_factory=no_registrations, sync=False)
        assert shadow.reads == 1
        assert job_runner() is second
    finally:
        shutdown_job_runner(2.0)
    assert job_runner() is None


def test_documented_bounds():
    """The numbers these tests assert against are the design's."""
    assert OUTCOME_BATCH_SIZE == 20
    assert (MIN_REPEATS, MAX_REPEATS) == (1, 3)
    assert JOB_CONCURRENCY == 1
    assert MAX_BATCH_ATTEMPTS == 3
    assert BATCH_BACKOFF_BASE_SECONDS == 1.0
    assert TUNING_SHADOW_NAME == "dda-workflow-tuning"
    assert job_runner_module.JOBS_KEY == "jobs"
    assert os.path.basename(manifest_key(PREFIX, JOB_ID)) == "manifest.json"


def test_the_replay_sends_the_sample_bytes_and_the_rendered_prompt():
    """The invocation the runner sends is built from the sample's own
    bytes and the manifest's metadata snippet, at the executor's arity."""
    document, objects = manifest_document(sample_count=2, repeats=1)
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {}})
    invoker = RecordingInvoker([VERDICT_TRUE])
    runner = build_runner(s3, shadow, invoker)
    run_to_completion(runner)

    assert len(invoker.calls) == 2
    for index, call in enumerate(invoker.calls):
        positional = call["positional"]
        entry = document["samples"][index]
        assert positional[0] == NODE_PARAMETERS["modelName"]
        assert positional[1] == (
            "Inspect part-{0}.\n\n".format(index)
            + 'Respond with JSON: {"is_anomalous": true|false, '
              '"confidence": 0..1}.')
        assert positional[2]["max_tokens"] == PROMPT_SET["max_tokens"]
        assert positional[3] == base64.b64encode(
            objects[entry["inputKey"]]).decode("ascii")
        assert positional[4] == base64.b64encode(
            objects[entry["referenceKey"]]).decode("ascii")
        assert call["system_prompt"] == PROMPT_SET["system_prompt"]
    # A single-image sample uses the 4-argument form.
    _single_document, single_objects = manifest_document(
        sample_count=1, with_reference=False)
    single_s3 = FakeS3(single_objects)
    single_invoker = RecordingInvoker([VERDICT_TRUE])
    single_runner = build_runner(single_s3, FakeShadow({JOB_ID: {}}),
                                single_invoker)
    run_to_completion(single_runner)
    assert len(single_invoker.calls[0]["positional"]) == 4
