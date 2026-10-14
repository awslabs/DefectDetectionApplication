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
"""Device half of **Property 11** (spec task 3.4).

**Feature: quality-prompt-tuning, Property 11: Device_Score_Jobs are
delivered, executed, reported and bounded exactly** — the device's half:

*For any* job manifest and any device behaviour (progress, silence,
failure, cancellation), the device SHALL replay each ``(sample, repeat)``
once with concurrency 1 on a worker distinct from the executor, append
outcome batches of ≤ 20, and report ``{status, done, total}``; and
cancellation SHALL stop the device after the batch in flight.

(The Portal's half — ingesting every batch exactly once, finalizing on
``done == total``, the 15-minute silence and the reported failure — is
``edge-cv-portal/backend/tests/test_property_tuning_score_runs.py``,
spec task 6.4.)

**Validates: Requirements 6.9, 6.11, 6.15**

Everything is a fake: a dict-backed S3 client holds the manifest, the
sample images and the outcome batches; a dict-backed shadow accessor
holds ``desired.jobs`` / ``reported.jobs`` with IoT's merge semantics
(a null value removes a key); the Text_Generation_API is a recording
invoker that also proves the replay never runs on the caller's thread
and never overlaps itself. No AWS call, no device, no network.
"""
import base64
import copy
import json
import math
import threading

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine.tuning.job_runner import (
    JOB_CONCURRENCY,
    MAX_BATCH_ATTEMPTS,
    MAX_REPEATS,
    MIN_REPEATS,
    OUTCOME_BATCH_SIZE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TUNING_SHADOW_NAME,
    JobRunner,
    manifest_key,
    outcomes_key,
    outcomes_prefix,
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

VERDICT_TRUE = '{"is_anomalous": true, "confidence": 0.91}'
VERDICT_FALSE = '{"is_anomalous": false, "confidence": 0.12}'
UNPARSEABLE = "the plate looks fine to me"
OUTPUT_TOKENS = 42

INPUT_BYTES = b"\xff\xd8input-image-bytes\xff\xd9"
REFERENCE_BYTES = b"\xff\xd8reference-image-bytes\xff\xd9"

NODE_PARAMETERS = {
    "modelName": "qwen2-vl-2b",
    "temperature": 0.7,
    "top_p": 0.9,
    "max_image_dimension": None,
}
PROMPT_SET = {
    "prompt_template": "Compare the input to {part_id}.",
    "system_prompt": "You are a QA inspector.",
    "max_tokens": 192,
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    """A dict-backed S3 client. ``fail_keys`` always rejects a put."""

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

    def written(self, prefix):
        return {
            key: value for key, value in self.objects.items()
            if key.startswith(prefix)
        }


class FakeShadow:
    """A named-shadow accessor with IoT's merge semantics."""

    def __init__(self, desired=None):
        self.state = {
            "desired": {"jobs": dict(desired or {})},
            "reported": {"jobs": {}},
        }
        self.reports = []
        self.names = set()

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        self.names.add((thing_name, shadow_name))
        return copy.deepcopy(self.state)

    def update_thing_shadow_state_request(self, thing_name, shadow_name,
                                         payload):
        self.names.add((thing_name, shadow_name))
        jobs = (payload or {}).get("reported", {}).get("jobs", {})
        for job_id, entry in jobs.items():
            self.reports.append((job_id, copy.deepcopy(entry)))
            if entry is None:
                self.state["reported"]["jobs"].pop(job_id, None)
            else:
                self.state["reported"]["jobs"][job_id] = copy.deepcopy(entry)
        return b"{}"

    def reported_entries(self, job_id):
        return [entry for reported, entry in self.reports
                if reported == job_id]


class RecordingInvoker:
    """The Text_Generation_API, faked.

    Records every call's positional arguments and keywords, reports
    generation metrics (so ``outputTokens`` lands in the outcome),
    proves the calls never overlap (concurrency 1) and never happen on
    the thread that delivered the job (Requirement 6.15).

    The answer is resolved from the image bytes the call carries (each
    sample's bytes embed its id), never from a call counter — samples
    whose objects are missing never reach the invoker at all.
    """

    def __init__(self, answer_for_image, on_call=None):
        self.answer_for_image = answer_for_image
        self.on_call = on_call
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
            self.calls.append({
                "positional": list(args),
                "system_prompt": system_prompt,
            })
            self.threads.append(threading.current_thread())
            if metrics_sink is not None:
                metrics_sink({"output_tokens": OUTPUT_TOKENS})
            if self.on_call is not None:
                self.on_call(index)
            image_b64 = args[3] if len(args) > 3 else None
            answer = self.answer_for_image(image_b64)
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            with self._lock:
                self._in_flight -= 1


class _EmptyRegistrations:
    """A session with no registrations, so the manifest's Node_Parameters
    stand alone (the device-registration overlay is task 3.3's own
    concern, not Property 11's)."""

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


def empty_session_factory():
    return _EmptyRegistrations()


# ---------------------------------------------------------------------------
# The drawn job
# ---------------------------------------------------------------------------

SAMPLE_SITUATIONS = (
    "ok",
    "no_reference",
    "missing_input_object",
    "missing_reference_object",
    "unresolved_placeholder",
)
ANSWERS = ("verdict_true", "verdict_false", "unparseable", "raises")
LABELS = ("OK", "NOK")
#: Labels and shapes a manifest sample may carry that cannot be replayed
#: (Requirement 4.3: only labelled, non-excluded samples are scored).
UNUSABLE_SHAPES = ("excluded", "unlabelled", "no_id", "no_input_key",
                   "not_a_mapping")

BEHAVIOURS = (
    "normal",
    "cancel",
    "batch_write_fails",
    "manifest_missing",
    "manifest_malformed",
    "no_model_name",
)

#: Repeats values a manifest may carry, including out-of-range ones the
#: device clamps defensively.
REPEATS_VALUES = (1, 2, 3, 0, -1, 9, "2", None, "abc", 2.0)


def clamped(raw):
    """An independent restatement of the device's repeats clamp."""
    try:
        repeats = int(raw)
    except (TypeError, ValueError):
        return MIN_REPEATS
    return max(MIN_REPEATS, min(MAX_REPEATS, repeats))


def expected_category(label, situation, answer):
    """Requirement 6.5, restated: the category of one replay."""
    if situation in ("missing_input_object", "missing_reference_object",
                     "unresolved_placeholder"):
        return "invocation_error"
    if answer == "raises":
        return "invocation_error"
    if answer == "unparseable":
        return "parse_failure"
    anomalous = answer == "verdict_true"
    if label == "NOK":
        return "correct" if anomalous else "false_pass"
    return "false_fail" if anomalous else "correct"


def build_job(sample_count, situations, answers, labels, repeats_raw,
              unusable_shapes, behaviour):
    """Build the manifest document, the S3 objects and the per-sample
    expectations for one drawn job."""
    objects = {}
    samples = []
    expectations = {}
    for index in range(sample_count):
        situation = situations[index % len(situations)]
        answer = answers[index % len(answers)]
        label = labels[index % len(labels)]
        sample_id = "sample-{0:03d}".format(index)
        input_key = "{0}{1}/{2}/dev/{3}.input.jpg".format(
            PREFIX, WORKFLOW_ID, NODE_ID, sample_id)
        reference_key = input_key[:-len(".input.jpg")] + ".reference.jpg"
        entry = {
            "sampleId": sample_id,
            "inputKey": input_key,
            "label": label,
            "metadataSnippet": {"part_id": "part-{0}".format(index)},
        }
        if situation != "missing_input_object":
            objects[input_key] = INPUT_BYTES + sample_id.encode("ascii")
        if situation == "no_reference":
            pass
        else:
            entry["referenceKey"] = reference_key
            if situation != "missing_reference_object":
                objects[reference_key] = (
                    REFERENCE_BYTES + sample_id.encode("ascii"))
        if situation == "unresolved_placeholder":
            entry["metadataSnippet"] = {}
        samples.append(entry)
        expectations[sample_id] = {
            "situation": situation,
            "answer": answer,
            "label": label,
            "inputKey": input_key,
            "referenceKey": (
                None if situation == "no_reference" else reference_key),
            "category": expected_category(label, situation, answer),
        }

    unusable = []
    for shape in unusable_shapes:
        if shape == "not_a_mapping":
            unusable.append("sample-x")
        elif shape == "no_id":
            unusable.append({"inputKey": "k", "label": "OK"})
        elif shape == "no_input_key":
            unusable.append({"sampleId": "u1", "label": "OK"})
        elif shape == "unlabelled":
            unusable.append({"sampleId": "u2", "inputKey": "k"})
        else:
            unusable.append({"sampleId": "u3", "inputKey": "k",
                             "label": "EXCLUDE"})

    document = {
        "jobId": JOB_ID,
        "sessionId": SESSION_ID,
        "runId": RUN_ID,
        "workflowId": WORKFLOW_ID,
        "nodeId": NODE_ID,
        "nodeParameters": dict(NODE_PARAMETERS),
        "promptSet": dict(PROMPT_SET),
        "repeats": repeats_raw,
        "samples": samples + unusable,
    }
    if behaviour == "no_model_name":
        document["nodeParameters"] = {"temperature": 0.7}
    key = manifest_key(PREFIX, JOB_ID)
    if behaviour == "manifest_missing":
        pass
    elif behaviour == "manifest_malformed":
        objects[key] = json.dumps({"jobId": JOB_ID}).encode("utf-8")
    else:
        objects[key] = json.dumps(document).encode("utf-8")
    return document, objects, expectations, key


ANSWER_TEXT = {
    "verdict_true": VERDICT_TRUE,
    "verdict_false": VERDICT_FALSE,
    "unparseable": UNPARSEABLE,
}


def answer_for(expectation):
    answer = expectation["answer"]
    if answer == "raises":
        return RuntimeError("the Text_Generation_API refused the request")
    return ANSWER_TEXT[answer]


# ---------------------------------------------------------------------------
# Property 11 (device half)
# ---------------------------------------------------------------------------

@given(
    sample_count=st.integers(min_value=0, max_value=25),
    situations=st.lists(st.sampled_from(SAMPLE_SITUATIONS),
                        min_size=5, max_size=5),
    answers=st.lists(st.sampled_from(ANSWERS), min_size=4, max_size=4),
    labels=st.lists(st.sampled_from(LABELS), min_size=2, max_size=2),
    repeats_raw=st.sampled_from(REPEATS_VALUES),
    unusable_shapes=st.lists(st.sampled_from(UNUSABLE_SHAPES), max_size=3),
    behaviour=st.sampled_from(BEHAVIOURS),
    cancel_after=st.integers(min_value=1, max_value=30),
    failing_batch=st.integers(min_value=1, max_value=4),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_property_device_score_jobs_are_executed_and_reported_exactly(
        sample_count, situations, answers, labels, repeats_raw,
        unusable_shapes, behaviour, cancel_after, failing_batch):
    """**Feature: quality-prompt-tuning, Property 11: Device_Score_Jobs
    are delivered, executed, reported and bounded exactly** (device
    half).

    For any job manifest and any device behaviour (progress, failure,
    cancellation), the device replays each ``(sample, repeat)`` exactly
    once with concurrency 1 on a worker distinct from the executor's
    threads, appends outcome batches of at most 20 outcomes under the
    run's prefix only, reports ``{status, done, total}`` at every step,
    and on cancellation stops after flushing the batch in flight,
    keeping every outcome already produced.

    **Validates: Requirements 6.9, 6.11, 6.15**"""
    document, objects, expectations, key = build_job(
        sample_count, situations, answers, labels, repeats_raw,
        unusable_shapes, behaviour)
    repeats = clamped(repeats_raw)
    #: The units the device must replay, in the order it replays them:
    #: manifest sample order, each sample repeated before the next.
    units = [
        (entry["sampleId"], repeat)
        for entry in document["samples"]
        if isinstance(entry, dict) and entry.get("sampleId") in expectations
        for repeat in range(1, repeats + 1)
    ]
    expected_pairs = set(units)
    total = len(units)
    #: Units that reach the model at all (a missing object or an
    #: unresolved placeholder is an outcome without an invocation).
    invoking = [
        index for index, (sample_id, _repeat) in enumerate(units)
        if expectations[sample_id]["situation"] in ("ok", "no_reference")
    ]
    run_prefix = outcomes_prefix(PREFIX, SESSION_ID, RUN_ID)

    fail_keys = ()
    if behaviour == "batch_write_fails":
        fail_keys = (outcomes_key(PREFIX, SESSION_ID, RUN_ID, failing_batch),)
    s3 = FakeS3(objects, fail_keys=fail_keys)
    shadow = FakeShadow({JOB_ID: {"manifestKey": key, "cancel": False}})

    by_image = {
        base64.b64encode(objects[expectation["inputKey"]]).decode("ascii"):
        expectation
        for expectation in expectations.values()
        if expectation["inputKey"] in objects
    }

    def answer_for_image(image_b64):
        expectation = by_image.get(image_b64)
        assert expectation is not None, "the invoker got unknown image bytes"
        return answer_for(expectation)

    runner = None

    def on_call(index):
        # Cancellation arrives as a shadow delta while the job runs, on
        # the MQTT thread the runner subscribes with.
        if behaviour == "cancel" and index + 1 == cancel_after:
            shadow.state["desired"]["jobs"][JOB_ID]["cancel"] = True
            runner.on_delta(None)

    invoker = RecordingInvoker(answer_for_image, on_call=on_call)
    runner = JobRunner(
        ExportConfig(bucket=BUCKET, prefix=PREFIX),
        shadow_accessor=shadow,
        s3_factory=lambda: s3,
        thing_name=THING_NAME,
        invoker=invoker,
        session_factory=empty_session_factory,
        backoff_base_seconds=0.0,
        sleep=lambda _seconds: None,
    )
    try:
        runner.sync()
        assert runner.wait_idle(30.0), "the job worker did not finish"
    finally:
        runner.stop(2.0)

    # -- reporting shape (Requirement 6.9) ------------------------------
    entries = shadow.reported_entries(JOB_ID)
    assert entries, "the device reported nothing"
    assert shadow.names == {(THING_NAME, TUNING_SHADOW_NAME)}
    assert entries[0]["status"] == STATUS_QUEUED
    for entry in entries:
        assert set(entry) <= {"status", "done", "total", "updatedAt", "error"}
        assert isinstance(entry["done"], int)
        assert entry["total"] is None or isinstance(entry["total"], int)
        assert isinstance(entry["updatedAt"], int)
    final = entries[-1]
    assert final["status"] in (
        STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)
    assert shadow.state["reported"]["jobs"][JOB_ID] == final
    progress = [entry["done"] for entry in entries]
    assert progress == sorted(progress), progress
    if behaviour in ("manifest_missing", "manifest_malformed"):
        assert final["total"] is None
    else:
        assert final["total"] == total

    # -- the outcome batches -------------------------------------------
    written = s3.written(run_prefix)
    batches = []
    for index in range(1, len(written) + 1):
        batch_key = outcomes_key(PREFIX, SESSION_ID, RUN_ID, index)
        assert batch_key in s3.objects, (
            "batch {0} is missing: outcome objects must be numbered from "
            "1 without gaps".format(index))
        batches.append(json.loads(s3.objects[batch_key].decode("utf-8")))
    # NOTHING outside the run's own prefix is written (Requirement 9.6).
    assert all(put.startswith(run_prefix) for put in s3.puts), s3.puts

    persisted = []
    for batch in batches:
        assert len(batch["outcomes"]) <= OUTCOME_BATCH_SIZE == 20
        assert batch["sessionId"] == SESSION_ID
        assert batch["runId"] == RUN_ID
        assert batch["jobId"] == JOB_ID
        assert batch["thingName"] == THING_NAME
        persisted.extend(batch["outcomes"])
    pairs = [(outcome["sampleId"], outcome["repeat"])
             for outcome in persisted]
    assert len(pairs) == len(set(pairs)), (
        "a (sample, repeat) was replayed twice")
    assert set(pairs) <= expected_pairs
    # The persisted outcomes are a PREFIX of the replay order: nothing is
    # reordered and nothing is skipped.
    assert pairs == units[:len(pairs)]
    assert final["done"] == len(persisted)

    # -- concurrency 1, on a worker of its own (Requirement 6.15) -------
    assert JOB_CONCURRENCY == 1
    assert invoker.max_in_flight <= 1
    for thread in invoker.threads:
        assert thread is not threading.current_thread()
        assert thread.name == "tuning-job-runner"

    # -- per-behaviour bounds ------------------------------------------
    if behaviour in ("manifest_missing", "manifest_malformed",
                     "no_model_name"):
        assert final["status"] == STATUS_FAILED
        assert final["error"]
        assert invoker.calls == []
        assert persisted == []
        if behaviour == "manifest_missing":
            assert key in final["error"]
        return

    #: Cancellation is only observable while work remains: the flag is
    #: set DURING the ``cancel_after``-th invocation, and the runner
    #: checks it before each remaining unit. A cancellation that arrives
    #: during the last unit therefore lets the job complete — no further
    #: invocation was issued either way (Requirement 6.11).
    cancelled_expected = (
        behaviour == "cancel"
        and cancel_after <= len(invoking)
        and invoking[cancel_after - 1] < total - 1)
    if cancelled_expected:
        assert final["status"] == STATUS_CANCELLED
        # No invocation after the cancellation, and every outcome
        # produced is persisted (the batch in flight was flushed).
        assert len(invoker.calls) == cancel_after
        assert len(persisted) == invoking[cancel_after - 1] + 1
        assert STATUS_RUNNING in [entry["status"] for entry in entries]
        return

    if behaviour == "batch_write_fails" and failing_batch <= math.ceil(
            total / OUTCOME_BATCH_SIZE):
        assert final["status"] == STATUS_FAILED
        assert final["error"]
        # Every batch before the failing one landed; nothing after it.
        assert len(batches) == failing_batch - 1
        assert len(persisted) == OUTCOME_BATCH_SIZE * (failing_batch - 1)
        # The write was attempted 3 times before the job failed.
        assert s3.puts.count(fail_keys[0]) == MAX_BATCH_ATTEMPTS == 3
        return

    # -- the completed job ---------------------------------------------
    assert final["status"] == STATUS_COMPLETED
    assert set(pairs) == expected_pairs
    assert final["done"] == total
    assert len(batches) == math.ceil(total / OUTCOME_BATCH_SIZE)
    if total:
        assert all(
            len(batch["outcomes"]) == OUTCOME_BATCH_SIZE
            for batch in batches[:-1])
    assert len(invoker.calls) == len(invoking)

    # Every outcome carries the categorization and the recorded answer.
    by_pair = {(outcome["sampleId"], outcome["repeat"]): outcome
               for outcome in persisted}
    for (sample_id, _repeat), outcome in by_pair.items():
        expectation = expectations[sample_id]
        assert outcome["label"] == expectation["label"]
        assert outcome["category"] == expectation["category"]
        assert set(outcome) <= {
            "sampleId", "repeat", "label", "category", "isAnomalous",
            "confidence", "rawAnswer", "outputTokens", "latencyMs", "error",
            "parseError"}
        if expectation["situation"] in ("ok", "no_reference"):
            # A latency is recorded around every real invocation; a
            # sample that never reached the model has none.
            assert isinstance(outcome["latencyMs"], int)
        else:
            assert "latencyMs" not in outcome
        if expectation["category"] == "invocation_error":
            assert outcome["error"]
            assert outcome["isAnomalous"] is None
            assert outcome["parseError"] is None
            if expectation["situation"] == "missing_input_object":
                assert expectation["inputKey"] in outcome["error"]
            if expectation["situation"] == "missing_reference_object":
                assert expectation["referenceKey"] in outcome["error"]
            if expectation["situation"] == "unresolved_placeholder":
                assert "part_id" in outcome["error"]
        elif expectation["category"] == "parse_failure":
            assert outcome["rawAnswer"] == UNPARSEABLE
            assert outcome["parseError"]
            assert outcome["error"] is None
            assert outcome["isAnomalous"] is None
        else:
            assert outcome["rawAnswer"] == ANSWER_TEXT[expectation["answer"]]
            assert outcome["isAnomalous"] is (
                expectation["answer"] == "verdict_true")
            assert outcome["outputTokens"] == OUTPUT_TOKENS
            assert outcome["error"] is None
            assert outcome["parseError"] is None

    # The images are read from the Sample_Store and no batch carries any
    # image bytes (Requirement 9.6).
    for batch_key, body in written.items():
        for data in (INPUT_BYTES, REFERENCE_BYTES):
            assert data not in body, batch_key
            assert base64.b64encode(data) not in body, batch_key

    # -- every replay was sent its own sample's bytes -------------------
    for call in invoker.calls:
        positional = call["positional"]
        image_b64 = positional[3]
        expectation = by_image[image_b64]
        assert positional[0] == NODE_PARAMETERS["modelName"]
        assert positional[2]["max_tokens"] == PROMPT_SET["max_tokens"]
        assert call["system_prompt"] == PROMPT_SET["system_prompt"]
        # The prompt is the manifest's template rendered against THIS
        # sample's metadata snippet, with the Verdict_Instruction
        # appended (the node is in Anomaly_Mode).
        assert positional[1].startswith("Compare the input to part-")
        assert positional[1].endswith(
            'Respond with JSON: {"is_anomalous": true|false, '
            '"confidence": 0..1}.')
        if expectation["referenceKey"] is not None:
            assert positional[4] == base64.b64encode(
                s3.objects[expectation["referenceKey"]]).decode("ascii")
        else:
            assert len(positional) == 4

    # -- a finalized job's reported entry is pruned --------------------
    shadow.state["desired"]["jobs"].pop(JOB_ID)
    runner.sync()
    assert JOB_ID not in shadow.state["reported"]["jobs"]


# ---------------------------------------------------------------------------
# Supporting checks: the documented layout and bounds
# ---------------------------------------------------------------------------

def test_documented_layout_and_bounds():
    """The keys and bounds Property 11 asserts against are the design's."""
    assert OUTCOME_BATCH_SIZE == 20
    assert (MIN_REPEATS, MAX_REPEATS) == (1, 3)
    assert JOB_CONCURRENCY == 1
    assert MAX_BATCH_ATTEMPTS == 3
    assert TUNING_SHADOW_NAME == "dda-workflow-tuning"
    assert tuning_root(PREFIX) == "workflow-tuning/"
    assert manifest_key(PREFIX, "job-1") == (
        "workflow-tuning/jobs/job-1/manifest.json")
    assert outcomes_key(PREFIX, "sess-1", "run-1", 3) == (
        "workflow-tuning/sessions/sess-1/runs/run-1/outcomes-3.json")


@pytest.mark.parametrize("shadow_state", [None, False, {}, {"desired": 1},
                                          {"desired": {"jobs": "x"}}])
def test_unusable_shadow_states_accept_no_job(shadow_state):
    """A missing or malformed tuning shadow yields no job and no work."""

    updates = []

    class _Shadow:
        def get_thing_shadow_state_request(self, thing_name, shadow_name):
            return shadow_state

        def update_thing_shadow_state_request(self, *args):
            updates.append(args)
            return b"{}"

    invoker = RecordingInvoker(lambda image_b64: VERDICT_TRUE)
    runner = JobRunner(
        ExportConfig(bucket=BUCKET, prefix=PREFIX),
        shadow_accessor=_Shadow(), s3_factory=lambda: FakeS3(),
        thing_name=THING_NAME, invoker=invoker,
        session_factory=empty_session_factory)
    runner.sync()
    assert runner.pending_jobs == 0
    assert invoker.calls == []
    assert updates == []


def test_cancellation_mid_job_keeps_what_was_produced():
    """The cancellation clause of Property 11, deterministically: a
    cancellation delivered while work remains stops the device after the
    batch in flight and keeps every outcome already produced
    (Requirement 6.11). The property covers this too whenever it draws
    it; this pins the branch."""
    document, objects, expectations, key = build_job(
        5, ["ok"] * 5, ["verdict_true"] * 4, ["NOK", "NOK"], 2, [], "normal")
    s3 = FakeS3(objects)
    shadow = FakeShadow({JOB_ID: {"manifestKey": key, "cancel": False}})
    runner = None

    def on_call(index):
        if index == 2:  # during the 3rd of 10 invocations
            shadow.state["desired"]["jobs"][JOB_ID]["cancel"] = True
            runner.on_delta(None)

    invoker = RecordingInvoker(lambda image_b64: VERDICT_TRUE,
                              on_call=on_call)
    runner = JobRunner(
        ExportConfig(bucket=BUCKET, prefix=PREFIX), shadow_accessor=shadow,
        s3_factory=lambda: s3, thing_name=THING_NAME, invoker=invoker,
        session_factory=empty_session_factory, backoff_base_seconds=0.0,
        sleep=lambda _seconds: None)
    try:
        runner.sync()
        assert runner.wait_idle(30.0)
    finally:
        runner.stop(2.0)

    final = shadow.reported_entries(JOB_ID)[-1]
    assert final["status"] == STATUS_CANCELLED
    assert (final["done"], final["total"]) == (3, 10)
    assert len(invoker.calls) == 3
    batch = json.loads(s3.objects[
        outcomes_key(PREFIX, SESSION_ID, RUN_ID, 1)].decode("utf-8"))
    assert len(batch["outcomes"]) == 3
    assert [(outcome["sampleId"], outcome["repeat"])
            for outcome in batch["outcomes"]] == [
        ("sample-000", 1), ("sample-000", 2), ("sample-001", 1)]
    assert outcomes_key(PREFIX, SESSION_ID, RUN_ID, 2) not in s3.objects


def test_an_unconfigured_runner_reads_nothing():
    """Without tuning configuration the runner is inert (Requirement
    2.6): no shadow read, no S3 client, no worker."""

    class _Shadow:
        def get_thing_shadow_state_request(self, *args):
            raise AssertionError("an unconfigured runner reads no shadow")

        def update_thing_shadow_state_request(self, *args):
            raise AssertionError("an unconfigured runner reports nothing")

    def _factory():
        raise AssertionError("an unconfigured runner builds no client")

    runner = JobRunner(None, shadow_accessor=_Shadow(), s3_factory=_factory)
    assert runner.enabled is False
    runner.sync()
    runner.on_delta({"state": {}})
    assert runner.pending_jobs == 0
    assert runner.running_job is None
