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

"""Live ``workflowTuning`` configuration (AC-16, FR-11, FR-6.2).

The 5 s configuration poller hands every good read to the
``TuningExportReconciler``, which enables, disables and relocates tuning
sample export, the backfill and the Device_Score_Job runner without a
LocalServer restart. Covered here: the poller's listeners, the exporter's
hand-over of its queue, the job runner's close, the backfill's abort, the
reconciler's transitions and the runtime wiring.

Every test uses an interface this change adds (``add_listener``,
``close_and_take``, ``JobRunner.close``/``handled_jobs``,
``BackfillSummary.aborted``, ``workflow_engine.tuning.live_config`` or the
runtime's ``_stop_job_runner``/``_tuning_shadow_subscription``), imported
or called inside the test, so on a LocalServer without live tuning
configuration each one fails with ``ImportError`` or ``AttributeError``.
Synthetic device, buckets, workflows and jobs throughout; no AWS call.
"""
import copy
import functools
import json
import logging
import os
import sys
import threading
import time
import types

import pytest

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.config_overrides import WorkflowOverridesConfig
from workflow_engine.tuning import backfill as backfill_module
from workflow_engine.tuning.job_runner import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TUNING_SHADOW_NAME,
    JobRunner,
    job_runner,
    manifest_key,
    set_job_runner,
    start_job_runner,
    tuning_delta_topic_prefix,
)
from workflow_engine.tuning.sample_export import (
    ExportConfig,
    ExportedSample,
    SampleExporter,
    sample_exporter,
    set_sample_exporter,
)

THING_NAME = "device-under-test"
PREFIX = "workflow-tuning/samples/"
BUCKET_A = "tuning-bucket-a"
BUCKET_B = "tuning-bucket-b"
CONFIG_A = ExportConfig(bucket=BUCKET_A, prefix=PREFIX)
CONFIG_B = ExportConfig(bucket=BUCKET_B, prefix=PREFIX)
SECTION_A = {"enabled": True, "bucket": BUCKET_A, "prefix": PREFIX}
SECTION_B = {"enabled": True, "bucket": BUCKET_B, "prefix": PREFIX}
REASON = "tuning export was reconfigured on the device"
CLOSE_THREAD = "workflow-tuning-job-close"
SYNC_THREAD = "workflow-tuning-job-sync"
STOPPED_LINE = ("Tuning sample backfill stopped: export was reconfigured; "
                "it runs again when export is next enabled")
LISTENER_WARNING = ("A workflow configuration listener failed; overrides "
                    "and the other listeners are unaffected")


def configuration(section=None, **extra):
    """A LocalServer component configuration as the poller reads it."""
    document = {"WorkflowDisabled": {"wf-off": "true"}}
    if section is not None:
        document["workflowTuning"] = copy.deepcopy(section)
    document.update(extra)
    return document


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def messages(caplog, level=None):
    return [record.getMessage() for record in caplog.records
            if level is None or record.levelno == level]


@pytest.fixture(autouse=True)
def _clean_process_state():
    set_sample_exporter(None)
    set_job_runner(None)
    yield
    set_sample_exporter(None)
    set_job_runner(None)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    """A dict-backed S3 client recording every put's bucket and key."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []
        self._lock = threading.Lock()

    def get_object(self, Bucket=None, Key=None, **_owner):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey: " + str(Key))
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None,
                   **_owner):
        with self._lock:
            self.puts.append((Bucket, Key))
            self.objects[Key] = Body
        return {}


class FakeShadow:
    """The tuning named shadow with IoT's merge semantics; records the
    thread of every read and every reported entry."""

    def __init__(self, desired=None):
        self.state = {"desired": {"jobs": dict(desired or {})},
                      "reported": {"jobs": {}}}
        self.reads = []
        self.reports = []
        self.on_read = None
        self.block_thread = None
        self.release = threading.Event()
        self._lock = threading.Lock()

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        assert (thing_name, shadow_name) == (THING_NAME, TUNING_SHADOW_NAME)
        self.reads.append(threading.current_thread().name)
        hook, self.on_read = self.on_read, None
        if hook is not None:
            hook()
        with self._lock:
            return copy.deepcopy(self.state)

    def update_thing_shadow_state_request(self, thing_name, shadow_name,
                                          payload):
        if threading.current_thread().name == self.block_thread:
            self.release.wait(5.0)
        jobs = (payload or {}).get("reported", {}).get("jobs", {})
        with self._lock:
            for job_id, entry in jobs.items():
                self.reports.append((job_id, copy.deepcopy(entry),
                                     threading.current_thread().name))
                if entry is None:
                    self.state["reported"]["jobs"].pop(job_id, None)
                else:
                    self.state["reported"]["jobs"][job_id] = copy.deepcopy(
                        entry)
        return b"{}"

    def entries(self, job_id):
        with self._lock:
            return [(entry, thread) for identifier, entry, thread
                    in self.reports if identifier == job_id and entry]

    def statuses(self, job_id):
        return [entry["status"] for entry, _ in self.entries(job_id)]


class _IdleRunner(JobRunner):
    """A runner whose accepted jobs stay pending: no worker starts."""

    def start(self):
        return None


class _QueueOnlyExporter(SampleExporter):
    """An exporter that holds its samples: no upload worker starts."""

    def start(self):
        return None


class _RecordingCloseExporter(_QueueOnlyExporter):
    """Records which exporter was installed when it was closed."""

    installed_at_close = "never closed"

    def close_and_take(self):
        self.installed_at_close = sample_exporter()
        return super().close_and_take()


def exported_sample(index):
    return ExportedSample(
        workflow_id="wf-live", node_id="check",
        node_type="bedrock_inference",
        execution_id="exec-{0}".format(index),
        input_bytes=b"\xff\xd8input-" + str(index).encode("ascii"),
        answer='{"is_anomalous": false, "confidence": 0.9}',
        verdict={"is_anomalous": False, "confidence": 0.9},
    )


class Wiring:
    """The reconciler's collaborators: recording fakes around the real
    job runner (idle: accepted jobs stay pending) and the runtime's
    ``_stop_job_runner``."""

    def __init__(self, shadow, s3=None, backfill=None):
        from workflow_engine import runtime

        self.shadow = shadow
        self.s3 = s3 if s3 is not None else FakeS3()
        self.calls = []
        self.exporters = []
        self._backfill = backfill
        self._runtime = runtime

    def start_backfill(self, exporter, **kwargs):
        self.calls.append(("backfill", exporter, kwargs))
        if self._backfill is not None:
            return self._backfill(exporter, **kwargs)
        return None

    def start_job_runner(self, exporter, handled_jobs=None, sync=True):
        self.calls.append(("runner", exporter, handled_jobs, sync))
        runner = _IdleRunner(
            exporter.config, shadow_accessor=self.shadow,
            s3_factory=lambda: self.s3, thing_name=exporter.thing_name,
            handled_jobs=handled_jobs)
        set_job_runner(runner)
        if sync:
            runner.on_delta(None)
        return runner

    def stop_job_runner(self, reason):
        self.calls.append(("stop", reason))
        return self._runtime._stop_job_runner(reason)

    def exporter_factory(self, config):
        exporter = SampleExporter(config, s3_factory=lambda: self.s3,
                                  thing_name=THING_NAME)
        self.exporters.append(exporter)
        return exporter

    def reconciler(self):
        from workflow_engine.tuning.live_config import TuningExportReconciler

        return TuningExportReconciler(
            start_backfill=self.start_backfill,
            start_job_runner=self.start_job_runner,
            stop_job_runner=self.stop_job_runner,
            exporter_factory=self.exporter_factory,
        )

    def kinds(self):
        return [call[0] for call in self.calls]


# ===========================================================================
# C1: configuration listeners on the poller
# ===========================================================================

def test_listeners_see_every_good_read_and_only_good_reads():
    """Each successful, non-empty read reaches the listener on the calling
    thread, before the unchanged-subtree short-circuit; failed, empty and
    non-mapping reads do not."""
    good = configuration(SECTION_A)
    moved = configuration(SECTION_B)
    reads = [good, RuntimeError("ipc down"), {}, "not a map", good, moved]

    def read():
        value = reads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    config = WorkflowOverridesConfig(read_config=read)
    seen = []
    config.add_listener(
        lambda raw: seen.append((raw, threading.current_thread().name)))
    for _ in range(6):
        config.refresh()
    assert [raw for raw, _ in seen] == [good, good, moved]
    assert {thread for _, thread in seen} == {
        threading.current_thread().name}
    assert config.is_disabled("wf-off") is True


def test_a_failing_listener_warns_once_a_minute_and_changes_nothing_else(
        caplog):
    caplog.set_level(logging.WARNING)
    now = [0.0]
    config = WorkflowOverridesConfig(
        read_config=lambda: configuration(SECTION_A), clock=lambda: now[0])
    received = []

    def broken(raw):
        raise RuntimeError("listener defect")

    config.add_listener(broken)
    config.add_listener(received.append)
    for moment in (0.0, 10.0, 59.0, 61.0):
        now[0] = moment
        config.refresh()
    warnings = [record for record in caplog.records
                if record.getMessage() == LISTENER_WARNING]
    assert len(warnings) == 2
    assert all(record.exc_info for record in warnings)
    assert len(received) == 4
    assert config.is_disabled("wf-off") is True


def test_runtime_adds_the_listeners_before_the_first_read(monkeypatch):
    """``_start_overrides_config(listeners=...)``: the first read, inside
    ``start()`` on the startup thread, already reaches the listener."""
    from workflow_engine import config_overrides, runtime

    assert callable(WorkflowOverridesConfig.add_listener)
    monkeypatch.setattr(config_overrides, "default_configuration_reader",
                        lambda: (lambda: configuration(SECTION_A)))
    seen = []
    config = runtime._start_overrides_config(
        listeners=[lambda raw: seen.append(threading.current_thread().name)])
    try:
        assert seen == [threading.current_thread().name]
    finally:
        config.stop()


# ===========================================================================
# C2: a closing exporter hands over its queue
# ===========================================================================

def test_close_and_take_returns_the_queue_and_later_samples_are_forwarded():
    old = _QueueOnlyExporter(CONFIG_A, thing_name=THING_NAME)
    for index in (1, 2):
        old.enqueue(exported_sample(index))
    set_sample_exporter(old)
    taken = old.close_and_take()
    assert [sample.execution_id for sample in taken] == ["exec-1", "exec-2"]
    assert old.queue_depth == 0
    # Nothing installed: a late sample is dropped, not kept.
    set_sample_exporter(None)
    old.enqueue(exported_sample(3))
    assert old.queue_depth == 0
    # Another exporter installed: the late sample lands there.
    new = _QueueOnlyExporter(CONFIG_B, thing_name=THING_NAME)
    set_sample_exporter(new)
    old.enqueue(exported_sample(4))
    assert old.queue_depth == 0
    assert new.queue_depth == 1


def test_a_closed_exporter_worker_finishes_and_exits():
    s3 = FakeS3()
    exporter = SampleExporter(CONFIG_A, s3_factory=lambda: s3,
                              thing_name=THING_NAME)
    exporter.enqueue(exported_sample(1))
    assert exporter.wait_idle(5.0)
    worker = exporter._worker
    assert exporter.close_and_take() == []
    worker.join(5.0)
    assert not worker.is_alive()
    assert {bucket for bucket, _ in s3.puts} == {BUCKET_A}


# ===========================================================================
# C3: a job runner closes
# ===========================================================================

def test_close_returns_at_once_and_reports_on_the_close_thread():
    shadow = FakeShadow({"job-c1": {}, "job-c2": {}})
    runner = _IdleRunner(CONFIG_A, shadow_accessor=shadow,
                         thing_name=THING_NAME)
    runner.sync()
    assert runner.handled_jobs() == {"job-c1": STATUS_QUEUED,
                                     "job-c2": STATUS_QUEUED}
    shadow.block_thread = CLOSE_THREAD
    handled = runner.close(REASON)
    # The close thread is blocked on its first shadow write, yet close()
    # has returned with every taken job recorded as failed.
    assert handled == {"job-c1": STATUS_FAILED, "job-c2": STATUS_FAILED}
    assert runner.pending_jobs == 0
    assert shadow.statuses("job-c1") == [STATUS_QUEUED]
    shadow.release.set()
    for job_id in ("job-c1", "job-c2"):
        assert wait_for(lambda: shadow.statuses(job_id)
                        == [STATUS_QUEUED, STATUS_FAILED])
        entry, thread = shadow.entries(job_id)[-1]
        assert thread == CLOSE_THREAD
        assert entry["error"] == REASON
    # A closed runner reads no shadow and accepts nothing.
    reads = len(shadow.reads)
    shadow.state["desired"]["jobs"]["job-c3"] = {}
    runner.sync()
    assert len(shadow.reads) == reads
    assert "job-c3" not in runner.handled_jobs()


def test_a_delta_racing_close_is_accepted_by_exactly_one_runner():
    """A ``sync()`` that read the shadow before ``close()`` accepts nothing
    after it; the next runner accepts the job once. A job the old runner
    accepted first is inherited and never re-run."""
    shadow = FakeShadow({"job-r": {}})
    old = _IdleRunner(CONFIG_A, shadow_accessor=shadow,
                      thing_name=THING_NAME)
    shadow.on_read = lambda: old.close(REASON)
    old.sync()
    assert old.handled_jobs() == {}
    new = _IdleRunner(CONFIG_B, shadow_accessor=shadow,
                      thing_name=THING_NAME,
                      handled_jobs=old.handled_jobs())
    new.sync()
    new.sync()
    assert new.handled_jobs() == {"job-r": STATUS_QUEUED}
    assert new.pending_jobs == 1
    assert shadow.statuses("job-r") == [STATUS_QUEUED]

    shadow = FakeShadow({"job-s": {}})
    first = _IdleRunner(CONFIG_A, shadow_accessor=shadow,
                        thing_name=THING_NAME)
    first.sync()
    handled = first.close(REASON)
    assert wait_for(lambda: shadow.statuses("job-s")
                    == [STATUS_QUEUED, STATUS_FAILED])
    second = _IdleRunner(CONFIG_B, shadow_accessor=shadow,
                         thing_name=THING_NAME, handled_jobs=handled)
    second.sync()
    assert second.pending_jobs == 0
    assert shadow.statuses("job-s") == [STATUS_QUEUED, STATUS_FAILED]


def test_a_late_queued_report_never_replaces_failed():
    """``close()`` lands between a ``sync()``'s acceptance and its
    ``queued`` report: the last (and only) write for the job is
    ``failed``."""

    class _CloseBeforeQueuedReport(_IdleRunner):
        def _report(self, job_id, status, done, total, error=None):
            if status == STATUS_QUEUED and not getattr(self, "_once", False):
                self._once = True
                self.close(REASON)
            super()._report(job_id, status, done, total, error)

    shadow = FakeShadow({"job-l": {}})
    runner = _CloseBeforeQueuedReport(CONFIG_A, shadow_accessor=shadow,
                                      thing_name=THING_NAME)
    runner.sync()
    assert wait_for(lambda: shadow.statuses("job-l") == [STATUS_FAILED])
    time.sleep(0.05)
    assert shadow.statuses("job-l") == [STATUS_FAILED]
    assert runner.handled_jobs() == {"job-l": STATUS_FAILED}


def test_the_running_job_stops_with_failed_and_the_close_reason():
    """The job in flight stops at its next invocation, keeps the batch it
    produced and reports ``failed`` with the reason (``stop()`` alone keeps
    reporting ``cancelled``)."""
    job_id = "job-x"
    samples, objects = [], {}
    for index in range(3):
        key = "{0}wf-live/llm_1/dev/s{1}.input.jpg".format(PREFIX, index)
        objects[key] = b"\xff\xd8input"
        samples.append({"sampleId": "s{0}".format(index), "inputKey": key,
                        "label": "NOK"})
    manifest = {"jobId": job_id, "sessionId": "sess-1", "runId": "run-1",
                "workflowId": "wf-live", "nodeId": "llm_1",
                "nodeParameters": {"modelName": "qwen2-vl-2b"},
                "promptSet": {"prompt_template": "Inspect the part."},
                "repeats": 1, "samples": samples}
    objects[manifest_key(PREFIX, job_id)] = json.dumps(manifest).encode()
    s3 = FakeS3(objects)
    shadow = FakeShadow({job_id: {}})
    holder = {}
    calls = []

    def invoker(*args, system_prompt=None, metrics_sink=None):
        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            holder["runner"].close(REASON)
        return '{"is_anomalous": true, "confidence": 0.9}'

    class _NoRegistrations:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def query(self, *args):
            return self

        filter = order_by = query

        def all(self):
            return []

    runner = JobRunner(CONFIG_A, shadow_accessor=shadow,
                       s3_factory=lambda: s3, thing_name=THING_NAME,
                       invoker=invoker, session_factory=_NoRegistrations,
                       backoff_base_seconds=0.0, sleep=lambda _s: None)
    assert runner.handled_jobs() == {}
    holder["runner"] = runner
    runner.sync()
    assert runner.wait_idle(10.0)
    assert len(calls) == 1
    statuses = shadow.statuses(job_id)
    assert statuses[0] == STATUS_QUEUED and statuses[-1] == STATUS_FAILED
    assert STATUS_RUNNING in statuses
    final, _ = shadow.entries(job_id)[-1]
    assert final["error"] == REASON
    assert final["done"] == 1


# ===========================================================================
# C4: the backfill stops when its exporter is replaced
# ===========================================================================

@pytest.fixture
def fake_walk(monkeypatch):
    """Two registrations with three executions each, one tunable node, and
    a usable sample per execution (the walk's reads are faked; its control
    flow is the real one)."""
    registrations = [types.SimpleNamespace(
        id=index, workflow_id="wf-{0}".format(index), version="1",
        artifact_path="/nonexistent") for index in (1, 2)]
    executions = [types.SimpleNamespace(
        id="exec-{0}".format(index), output_dir="/nonexistent",
        capture_id="capture", started_at=None) for index in (1, 2, 3)]

    class _Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args):
            return self

        order_by = limit = filter

        def all(self):
            return list(self.rows)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def query(self, model):
            if model.__name__ == "WorkflowRegistration":
                return _Query(registrations)
            return _Query(executions)

    counter = iter(range(1, 1000))
    monkeypatch.setattr(backfill_module, "_graph_document",
                        lambda registration: {"nodes": []})
    monkeypatch.setattr(backfill_module, "tunable_nodes",
                        lambda document: [object()])
    monkeypatch.setattr(backfill_module.run_artifacts, "read_run_metadata",
                        lambda output_dir, capture_id: {})
    monkeypatch.setattr(backfill_module, "node_outcome",
                        lambda metadata, node: {"verdict": "pass"})
    monkeypatch.setattr(
        backfill_module, "build_sample",
        lambda node, **kwargs: (exported_sample(next(counter)), None))
    return _Session


class _HookExporter(_QueueOnlyExporter):
    """Runs ``hook(n)`` before its n-th enqueue."""

    hook = None

    def enqueue(self, sample):
        self.count = getattr(self, "count", 0) + 1
        if self.hook is not None:
            self.hook(self.count)
        super().enqueue(sample)


def test_a_backfill_whose_exporter_is_replaced_mid_walk_writes_no_marker(
        tmp_path, fake_walk, caplog):
    assert backfill_module.BackfillSummary().aborted is False
    caplog.set_level(logging.INFO)
    marker = str(tmp_path / "backfilled.json")
    exporter = _HookExporter(CONFIG_A, thing_name=THING_NAME)
    exporter.hook = lambda n: set_sample_exporter(None) if n == 2 else None
    set_sample_exporter(exporter)
    summary = backfill_module.run_backfill(
        exporter, session_factory=fake_walk, marker_path=marker,
        pace_wait_seconds=0.0, abort_if_replaced=True)
    assert summary.aborted is True
    assert summary.exported == 2
    assert summary.executions_scanned == 3
    assert summary.registrations == 1
    assert not os.path.exists(marker)
    assert STOPPED_LINE in messages(caplog, logging.INFO)
    assert "aborted" not in summary.as_document()


def test_a_backfill_replaced_after_its_last_enqueue_writes_no_marker(
        tmp_path, fake_walk, caplog):
    assert backfill_module.BackfillSummary().aborted is False
    caplog.set_level(logging.INFO)
    marker = str(tmp_path / "backfilled.json")
    exporter = _HookExporter(CONFIG_A, thing_name=THING_NAME)
    set_sample_exporter(exporter)
    replacement = _QueueOnlyExporter(CONFIG_B, thing_name=THING_NAME)
    exporter.hook = (
        lambda n: set_sample_exporter(replacement) if n == 6 else None)
    summary = backfill_module.run_backfill(
        exporter, session_factory=fake_walk, marker_path=marker,
        pace_wait_seconds=0.0, abort_if_replaced=True)
    assert summary.aborted is False
    assert summary.exported == 6
    assert not os.path.exists(marker)
    assert STOPPED_LINE in messages(caplog, logging.INFO)


def test_the_marker_document_of_a_normal_run_is_unchanged(tmp_path,
                                                          fake_walk):
    """``aborted`` never reaches the marker; without ``abort_if_replaced``
    an exporter that is not installed still completes and marks."""
    assert backfill_module.BackfillSummary().aborted is False
    for abort_if_replaced, installed in ((True, True), (False, False)):
        marker = str(tmp_path / "marker-{0}.json".format(abort_if_replaced))
        exporter = _QueueOnlyExporter(CONFIG_A, thing_name=THING_NAME)
        set_sample_exporter(exporter if installed else None)
        summary = backfill_module.run_backfill(
            exporter, session_factory=fake_walk, marker_path=marker,
            pace_wait_seconds=0.0, abort_if_replaced=abort_if_replaced)
        assert summary.aborted is False
        assert summary.exported == 6
        with open(marker) as handle:
            document = json.load(handle)
        assert "aborted" not in document
        assert set(document) == {
            "schemaVersion", "backfilledAt", "bucket", "prefix",
            "registrations", "nodes", "executionsScanned", "exported",
            "skipped", "error"}


def test_a_disable_during_the_startup_backfill_leaves_no_marker(
        tmp_path, fake_walk, caplog):
    """``adopt`` starts the backfill with ``abort_if_replaced=True``; a
    configuration update that disables export mid-walk stops it before
    the marker."""
    caplog.set_level(logging.INFO)
    marker = str(tmp_path / "backfilled.json")
    first, go = threading.Event(), threading.Event()

    def hook(n):
        if n == 1:
            first.set()
            go.wait(5.0)

    threads = []

    def real_backfill(exporter, **kwargs):
        thread = backfill_module.start_backfill(
            exporter, session_factory=fake_walk, marker_path=marker,
            pace_wait_seconds=0.0, **kwargs)
        threads.append(thread)
        return thread

    wiring = Wiring(FakeShadow(), backfill=real_backfill)
    reconciler = wiring.reconciler()
    exporter = _HookExporter(CONFIG_A, thing_name=THING_NAME)
    exporter.hook = hook
    set_sample_exporter(exporter)
    reconciler.adopt(exporter)
    assert first.wait(5.0)
    reconciler.on_configuration(configuration(None))
    go.set()
    threads[0].join(5.0)
    assert not threads[0].is_alive()
    assert wiring.calls[0][2] == {"abort_if_replaced": True}
    assert not os.path.exists(marker)
    assert STOPPED_LINE in messages(caplog, logging.INFO)
    assert exporter.queue_depth == 0


# ===========================================================================
# C5: the reconciler's transitions
# ===========================================================================

def test_enable_from_absent_reads_the_shadow_off_the_calling_thread(caplog):
    from workflow_engine.tuning.live_config import JOB_SYNC_THREAD_NAME

    caplog.set_level(logging.INFO)
    shadow = FakeShadow({"job-e": {}})
    wiring = Wiring(shadow)
    reconciler = wiring.reconciler()
    reconciler.adopt(None)
    assert wiring.calls == []
    reconciler.on_configuration(configuration(None))
    assert wiring.calls == [] and sample_exporter() is None
    release = threading.Event()
    shadow.on_read = lambda: release.wait(5.0)
    reconciler.on_configuration(configuration(SECTION_A))
    # on_configuration returned while the first shadow read still waits.
    exporter = sample_exporter()
    assert exporter is wiring.exporters[0]
    assert exporter.config == CONFIG_A
    assert wiring.kinds() == ["backfill", "runner"]
    assert wiring.calls[0] == ("backfill", exporter,
                               {"abort_if_replaced": True})
    assert wiring.calls[1] == ("runner", exporter, None, False)
    release.set()
    assert wait_for(lambda: job_runner() is not None and
                    job_runner().handled_jobs() == {"job-e": STATUS_QUEUED})
    assert shadow.reads == [JOB_SYNC_THREAD_NAME]
    assert JOB_SYNC_THREAD_NAME == SYNC_THREAD
    assert SYNC_THREAD != threading.current_thread().name
    assert ("Tuning sample export enabled for device 'device-under-test': "
            "s3://tuning-bucket-a/workflow-tuning/samples/ (configuration "
            "update, no restart)") in messages(caplog, logging.INFO)


def test_disable_drops_the_queue_fails_the_jobs_and_stops_the_runner(caplog):
    caplog.set_level(logging.INFO)
    shadow = FakeShadow({"job-d1": {}, "job-d2": {}})
    wiring = Wiring(shadow)
    reconciler = wiring.reconciler()
    exporter = _QueueOnlyExporter(CONFIG_A, thing_name=THING_NAME)
    for index in (1, 2):
        exporter.enqueue(exported_sample(index))
    set_sample_exporter(exporter)
    reconciler.adopt(exporter)
    # Startup: the backfill and a synchronous first reconciliation.
    assert wiring.kinds() == ["backfill", "runner"]
    assert wiring.calls[1][3] is True
    assert shadow.reads == [threading.current_thread().name]
    runner = job_runner()
    assert runner.handled_jobs() == {"job-d1": STATUS_QUEUED,
                                     "job-d2": STATUS_QUEUED}
    # The first poll sees the startup configuration: no action.
    reconciler.on_configuration(configuration(SECTION_A))
    assert wiring.kinds() == ["backfill", "runner"]
    reconciler.on_configuration(configuration(None))
    assert sample_exporter() is None
    assert job_runner() is None
    assert wiring.calls[-1] == ("stop", REASON)
    assert exporter.queue_depth == 0
    lines = messages(caplog, logging.INFO)
    assert ("Tuning sample export disabled by a configuration update; 2 "
            "queued sample(s) dropped") in lines
    assert ("2 tuning job(s) reported failed: tuning export was "
            "reconfigured on the device") in lines
    for job_id in ("job-d1", "job-d2"):
        assert wait_for(lambda: shadow.statuses(job_id)[-1:]
                        == [STATUS_FAILED])
        entry, thread = shadow.entries(job_id)[-1]
        assert (thread, entry["error"]) == (CLOSE_THREAD, REASON)
    # A sample reaching the closed exporter is dropped.
    exporter.enqueue(exported_sample(3))
    assert exporter.queue_depth == 0


def test_relocation_installs_first_moves_the_queue_and_inherits_the_jobs(
        caplog):
    caplog.set_level(logging.INFO)
    shadow = FakeShadow({"job-1": {}})
    wiring = Wiring(shadow)
    reconciler = wiring.reconciler()
    old = _RecordingCloseExporter(CONFIG_A, thing_name=THING_NAME)
    for index in (1, 2):
        old.enqueue(exported_sample(index))
    set_sample_exporter(old)
    reconciler.adopt(old)
    old_runner = job_runner()
    assert old_runner.handled_jobs() == {"job-1": STATUS_QUEUED}
    shadow.state["desired"]["jobs"]["job-2"] = {}
    reconciler.on_configuration(configuration(SECTION_B))

    new = sample_exporter()
    assert new is wiring.exporters[0] and new.config == CONFIG_B
    assert old.installed_at_close is new
    # A sample an executor thread fetched from the old exporter is
    # forwarded; moved and forwarded samples upload to bucket B.
    old.enqueue(exported_sample(3))
    assert new.wait_idle(5.0)
    assert len(wiring.s3.puts) == 6
    assert {bucket for bucket, _ in wiring.s3.puts} == {BUCKET_B}
    lines = messages(caplog, logging.INFO)
    assert ("Tuning sample export moved from "
            "s3://tuning-bucket-a/workflow-tuning/samples/ to "
            "s3://tuning-bucket-b/workflow-tuning/samples/ by a "
            "configuration update; 2 queued sample(s) moved") in lines
    assert ("1 tuning job(s) reported failed: tuning export was "
            "reconfigured on the device") in lines
    # The new runner inherits job-1 (failed) and accepts only job-2.
    kinds = wiring.kinds()
    assert kinds == ["backfill", "runner", "stop", "runner", "backfill"]
    assert wiring.calls[3] == ("runner", new, {"job-1": STATUS_FAILED},
                               False)
    assert wiring.calls[4] == ("backfill", new, {"abort_if_replaced": True})
    new_runner = job_runner()
    assert new_runner is not old_runner
    assert wait_for(lambda: new_runner.handled_jobs().get("job-2")
                    == STATUS_QUEUED)
    assert new_runner.pending_jobs == 1
    assert wait_for(lambda: shadow.statuses("job-1")
                    == [STATUS_QUEUED, STATUS_FAILED])
    time.sleep(0.05)
    assert shadow.statuses("job-1") == [STATUS_QUEUED, STATUS_FAILED]
    assert shadow.statuses("job-2") == [STATUS_QUEUED]
    assert shadow.reads[-1] == SYNC_THREAD


def test_a_malformed_section_warns_once_per_change(caplog):
    from workflow_engine.tuning import live_config  # noqa: F401

    caplog.set_level(logging.WARNING)
    wiring = Wiring(FakeShadow())
    reconciler = wiring.reconciler()

    def tuning_warnings():
        return [line for line in messages(caplog, logging.WARNING)
                if line.startswith("Tuning sample export is disabled")]

    # Startup already warned for its malformed section: the first poll
    # does not repeat it.
    reconciler.adopt(None)
    no_prefix = {"enabled": True, "bucket": BUCKET_A}
    reconciler.on_configuration(configuration(no_prefix))
    assert tuning_warnings() == []
    bad_prefix = {"enabled": True, "bucket": BUCKET_A, "prefix": "no-slash"}
    for _ in range(3):
        reconciler.on_configuration(configuration(bad_prefix))
    assert len(tuning_warnings()) == 1
    assert wiring.calls == [] and wiring.exporters == []
    assert sample_exporter() is None

    # valid -> malformed: export is disabled, with one WARNING.
    caplog.clear()
    wiring = Wiring(FakeShadow())
    reconciler = wiring.reconciler()
    exporter = _QueueOnlyExporter(CONFIG_A, thing_name=THING_NAME)
    set_sample_exporter(exporter)
    reconciler.adopt(exporter)
    for _ in range(3):
        reconciler.on_configuration(configuration(bad_prefix))
    assert len(tuning_warnings()) == 1
    assert sample_exporter() is None
    assert wiring.kinds().count("stop") == 1


def test_an_unchanged_configuration_takes_no_action():
    wiring = Wiring(FakeShadow())
    reconciler = wiring.reconciler()
    exporter = _QueueOnlyExporter(CONFIG_A, thing_name=THING_NAME)
    set_sample_exporter(exporter)
    reconciler.adopt(exporter)
    startup = list(wiring.calls)
    for _ in range(3):
        reconciler.on_configuration(configuration(SECTION_A))
    # Only the formatting changed: the parsed configuration is the same.
    reconciler.on_configuration(configuration(
        dict(SECTION_A, bucket="  " + BUCKET_A + " ")))
    reconciler.on_configuration(configuration(SECTION_A, Other={"x": 1}))
    assert wiring.calls == startup
    assert wiring.exporters == []
    assert sample_exporter() is exporter


def test_a_failing_step_is_contained_and_the_rest_still_runs(caplog):
    caplog.set_level(logging.WARNING)
    wiring = Wiring(FakeShadow())

    def broken_runner(exporter, handled_jobs=None, sync=True):
        raise RuntimeError("no IPC here")

    wiring.start_job_runner = broken_runner
    reconciler = wiring.reconciler()
    reconciler.adopt(None)
    reconciler.on_configuration(configuration(SECTION_A))
    assert sample_exporter() is wiring.exporters[0]
    assert wiring.kinds() == ["backfill"]
    assert any(line.startswith(
        "Tuning configuration update could not be applied fully: ")
        for line in messages(caplog, logging.WARNING))


# ===========================================================================
# C5: runtime wiring
# ===========================================================================

@pytest.fixture
def device_stubs(monkeypatch):
    """Off-device stand-ins for ``utils.server_setup``, the MQTT
    ``SubscriptionHandler`` and the Greengrass IPC stream handler base."""
    import utils as utils_package

    shadow = FakeShadow()
    server_setup = types.SimpleNamespace(iot_shadow_accessor=shadow,
                                         publish_handler=object())
    monkeypatch.setattr(utils_package, "server_setup", server_setup,
                        raising=False)
    monkeypatch.setitem(sys.modules, "utils.server_setup", server_setup)

    subscriptions = []

    class SubscriptionHandler:
        def __init__(self, topic, handler, publish_handler):
            self.topic, self.handler = topic, handler
            self.subscribed = threading.Event()
            subscriptions.append(self)

        def subscribe(self):
            self.subscribed.set()

    mqtt = types.ModuleType("mqtt")
    handler_module = types.ModuleType("mqtt.SubscriptionHandler")
    handler_module.SubscriptionHandler = SubscriptionHandler
    mqtt.SubscriptionHandler = handler_module
    monkeypatch.setitem(sys.modules, "mqtt", mqtt)
    monkeypatch.setitem(sys.modules, "mqtt.SubscriptionHandler",
                        handler_module)

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
    # Accepted jobs stay pending: no worker, no S3.
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    return types.SimpleNamespace(shadow=shadow, subscriptions=subscriptions)


class _Event:
    def __init__(self, topic_name, payload):
        self.message = types.SimpleNamespace(topic_name=topic_name,
                                             payload=payload)


def test_one_subscription_per_process_forwards_to_the_installed_runner(
        monkeypatch, device_stubs):
    from workflow_engine import runtime

    monkeypatch.setattr(runtime, "_tuning_shadow_subscription", None)
    shadow = device_stubs.shadow
    shadow.state["desired"]["jobs"]["job-p"] = {}
    first = runtime._start_job_runner(
        SampleExporter(CONFIG_A, s3_factory=FakeS3, thing_name=THING_NAME),
        sync=False)
    assert job_runner() is first
    assert shadow.reads == []
    assert len(device_stubs.subscriptions) == 1
    assert device_stubs.subscriptions[0].subscribed.wait(5.0)
    assert runtime._stop_job_runner(REASON) == {}
    assert job_runner() is None
    assert runtime._stop_job_runner(REASON) == {}
    second = runtime._start_job_runner(
        SampleExporter(CONFIG_B, s3_factory=FakeS3, thing_name=THING_NAME),
        handled_jobs={"job-old": STATUS_FAILED}, sync=False)
    assert len(device_stubs.subscriptions) == 1
    assert second.handled_jobs() == {"job-old": STATUS_FAILED}
    # A delta on the one subscription reaches the installed runner.
    subscription = device_stubs.subscriptions[0]
    prefix = tuning_delta_topic_prefix(THING_NAME)
    assert subscription.topic == prefix
    payload = json.dumps({"state": {"jobs": {"job-p": {}}}}).encode()
    subscription.handler.on_stream_event(_Event(prefix + "delta", payload))
    assert second.handled_jobs() == {"job-old": STATUS_FAILED,
                                     "job-p": STATUS_QUEUED}
    assert first.handled_jobs() == {}
    # sync=True keeps the startup behaviour: one read on this thread.
    runtime._stop_job_runner(REASON)
    reads = len(shadow.reads)
    third = runtime._start_job_runner(
        SampleExporter(CONFIG_A, s3_factory=FakeS3, thing_name=THING_NAME))
    assert shadow.reads[reads:] == [threading.current_thread().name]
    assert third.handled_jobs() == {"job-p": STATUS_QUEUED}
    assert len(device_stubs.subscriptions) == 1


def test_startup_adopts_the_exporter_before_the_poller_starts(monkeypatch):
    """``start_workflow_engine`` hands the startup exporter to the
    reconciler (backfill with ``abort_if_replaced=True``, runner with
    ``sync=True``), then starts the poller with the reconciler's
    listener."""
    from workflow_engine import runtime
    from workflow_engine.tuning import live_config

    order = []
    exporter = _QueueOnlyExporter(CONFIG_A, thing_name=THING_NAME)

    class _Reconciler(live_config.TuningExportReconciler):
        def adopt(self, adopted):
            order.append(("adopt", adopted))
            super().adopt(adopted)

    def fake_backfill(adopted, **kwargs):
        order.append(("backfill", adopted, kwargs))

    def fake_runner(adopted, handled_jobs=None, sync=True):
        order.append(("runner", adopted, sync))

    def fake_overrides(listeners=()):
        order.append(("overrides", [listener.__func__.__name__
                                    for listener in listeners]))
        return None

    class _Watcher:
        registrations_listeners = []

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def binding_resolution(self, *args, **kwargs):
            return None

        def _resync_for_bindings(self, *args, **kwargs):
            return None

    monkeypatch.setattr(live_config, "TuningExportReconciler", _Reconciler)
    monkeypatch.setattr(backfill_module, "start_backfill", fake_backfill)
    monkeypatch.setattr(runtime, "_start_job_runner", fake_runner)
    monkeypatch.setattr(runtime, "_start_overrides_config", fake_overrides)
    monkeypatch.setattr(runtime, "_configure_sample_export",
                        lambda: exporter)
    monkeypatch.setattr(runtime, "_camera_binding_dependencies",
                        lambda: (None, None))
    monkeypatch.setattr(runtime, "_wire_camera_binding_hooks",
                        lambda watcher: None)
    monkeypatch.setattr(runtime, "WorkflowWatcher", _Watcher)
    for name in ("_watcher", "_executor_instance", "_trigger_manager",
                 "_stream_lease_keeper", "_continuous_manager"):
        monkeypatch.setattr(runtime, name, None)
    monkeypatch.setattr(runtime, "_tuning_shadow_subscription", None)
    from workflow_engine import (continuous_runner, pipeline_executor,
                                 stream_leases, trigger_runtime)

    class _Inert:
        def __init__(self, *args, **kwargs):
            pass

        def on_registrations_changed(self, *args, **kwargs):
            pass

        def refusal_reason(self, *args, **kwargs):
            return None

        def persist_counters(self):
            pass

    monkeypatch.setattr(pipeline_executor, "register_workflow_executor",
                        lambda **kwargs: object())
    monkeypatch.setattr(trigger_runtime, "TriggerSubscriptionManager", _Inert)
    monkeypatch.setattr(stream_leases, "StreamLeaseKeeper", _Inert)
    monkeypatch.setattr(continuous_runner, "ContinuousRunnerManager", _Inert)
    from workflow_engine import run_retention

    monkeypatch.setattr(run_retention, "RunRetention",
                        lambda *args, **kwargs: None)
    assert runtime.start_workflow_engine() is not None
    assert order == [
        ("adopt", exporter),
        ("backfill", exporter, {"abort_if_replaced": True}),
        ("runner", exporter, True),
        ("overrides", ["on_configuration"]),
    ]
