"""
Preview_Run executor in dda_labeling.py (llm-autolabel-prompt-tuning,
task 9.8).

Feature: llm-autolabel-prompt-tuning

Example-based coverage of `execute_preview_run` (task 9.1), driven
through the same `{'action': 'execute_preview_run', 'run_id': ...}`
payload the deployed function receives, against the moto-backed stack
from conftest.py. The harness is reused rather than re-created:
`PreviewFlowEnv` from test_preview_flow_integration.py (and through it
`PreviewEnv` / `CreateJobEnv`) supplies the Use_Case, dataset prefix,
authorized creator, `POST /labeling-preview/runs` and `GET
/labeling-preview/runs/{runId}` builders, the preview-state readers and
inline executor driving.

What is asserted here, and nowhere else:

- **Sequential processing** (Req 3.1, 3.5): with N Sample_Images the
  invocation order is the request order (`IMAGE#000`, `001`, ...) and
  each sample's payload *and* item write complete before the next sample
  is invoked — asserted from a single recorded event log, not from
  per-call snapshots.
- **Per-sample writes** (Req 3.5, 3.7): the payload lands at
  `labeling-previews/{usecase_id}/{run_id}/{i}.json` *before* the
  `IMAGE#{i:03d}` item that references it, checked by reading the object
  at the moment the item write is issued, so `result_s3_key` can never
  dangle.
- **Terminal transition with every sample failed** (Req 3.7): the run
  still reaches `Completed`, with no `run_error`; `Failed` is reserved
  for a run-level failure.
- **Lock release** (Req 8.8): on the success path, when a helper raises
  unexpectedly, and even when the terminal status write itself fails.
- **No retry and no second invocation per sample** (Req 3.1): exactly one
  invocation per sample whatever the outcome, a duplicated async
  delivery of a `Completed` run is skipped, and a replayed delivery of a
  run forced back to `Running` re-invokes nothing because every sample is
  already resolved.

Test seams: `dda_labeling.get_bedrock_client` (a stub Converse client,
for the tests that exercise the real `generate_llm_prelabel`) and
`dda_labeling.generate_llm_prelabel` (a recorder, for invocation
ordering and counting). The state helpers are wrapped, never replaced —
the real DynamoDB and S3 writes still happen behind the recording.

Per-sample failure categorization, example-image isolation, the absence
of labeling-pipeline state and request content restriction are Properties
9-12 in test_property_preview_run_outcomes.py and are deliberately not
repeated.

Requirements: 3.1, 3.5, 3.7, 8.8
"""
import json

import pytest
from botocore.exceptions import ClientError

from dda_llm_prelabel import LlmPrelabelError
from test_dda_labeling_preview_routes import ARTIFACTS_BUCKET
from test_preview_flow_integration import (  # noqa: F401 — `dda` is a fixture
    MODEL,
    MODEL_ID,
    PROMPT,
    PreviewFlowEnv,
    dda,
    guidance,
)

LABELS = ["scratch", "dent"]
BOX = {"class": "scratch",
       "box": {"left": 4, "top": 6, "width": 20, "height": 10}}

# What the recorder returns for a sample that is meant to succeed. Its
# shape is irrelevant to these tests — they are about ordering, writes,
# transitions and call counts, not about Pre_Label conversion.
PRELABEL = {"modality": "ObjectDetection", "boxes": [], "image_width": 120,
            "image_height": 90}


class PrelabelRecorder:
    """Stands in for `generate_llm_prelabel`, recording every call and
    replaying a per-sample outcome.

    `outcomes` maps a Sample_Image key to either an Exception to raise or
    a Pre_Label dict to return; anything unlisted succeeds with
    `PRELABEL`.
    """

    def __init__(self, outcomes=None, events=None):
        self.outcomes = outcomes or {}
        self.calls = []
        self.events = events if events is not None else []

    @property
    def counts(self):
        """Invocations per Sample_Image key."""
        counts = {}
        for call in self.calls:
            key = call["image_key"]
            counts[key] = counts.get(key, 0) + 1
        return counts

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.events.append(("invoke", kwargs["image_key"]))
        outcome = self.outcomes.get(kwargs["image_key"], PRELABEL)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ExecutorEnv(PreviewFlowEnv):
    """PreviewFlowEnv plus the executor's own seams: a recording stand-in
    for `generate_llm_prelabel`, an ordered write log, and replay of the
    async delivery."""

    # ------------------------------------------------------------- seams
    def use_prelabel(self, outcomes=None, events=None):
        """Patch `dda_labeling.generate_llm_prelabel` with a recorder."""
        recorder = PrelabelRecorder(outcomes=outcomes, events=events)
        self.monkeypatch.setattr(self.module, "generate_llm_prelabel",
                                 recorder)
        return recorder

    def record_writes(self, events=None):
        """Wrap the payload / item / run-status writers so their order is
        observable, keeping the real writes.

        The item wrapper reads the object `result_s3_key` names at the
        moment the item write is issued, which is what makes "the payload
        exists before anything references it" directly assertable.
        """
        events = events if events is not None else []
        module = self.module
        real_payload = module._write_preview_result_payload
        real_item = module._update_preview_sample_state
        real_status = module._update_preview_run_status

        def payload_writer(usecase_id, run_id, index, payload):
            key = real_payload(usecase_id, run_id, index, payload)
            events.append(("payload", index, key))
            return key

        def item_writer(run_id, index, state, **kwargs):
            events.append(("item", index, state,
                           self._payload_readable(kwargs.get("result_s3_key"))))
            return real_item(run_id, index, state, **kwargs)

        def status_writer(run_id, status, **kwargs):
            events.append(("run", status))
            return real_status(run_id, status, **kwargs)

        self.monkeypatch.setattr(module, "_write_preview_result_payload",
                                 payload_writer)
        self.monkeypatch.setattr(module, "_update_preview_sample_state",
                                 item_writer)
        self.monkeypatch.setattr(module, "_update_preview_run_status",
                                 status_writer)
        return events

    def _payload_readable(self, result_s3_key):
        if not result_s3_key:
            return None
        try:
            self.s3.head_object(Bucket=ARTIFACTS_BUCKET, Key=result_s3_key)
            return True
        except ClientError:
            return False

    def break_helper(self, name, error):
        """Make one executor helper raise, to exercise the unexpected
        failure path."""
        def raising(*args, **kwargs):
            raise error

        self.monkeypatch.setattr(self.module, name, raising)

    # --------------------------------------------------------- execution
    def replay_executor(self, run_id):
        """A duplicated async delivery of the same run."""
        return self.module.handler(
            {"action": "execute_preview_run", "run_id": run_id}, self.context)

    def force_run_status(self, run_id, status):
        self.module._update_preview_run_status(run_id, status)

    # ---------------------------------------------------------- readback
    def result_key(self, run_id, index):
        """The payload key the item's `result_s3_key` must name."""
        return f"labeling-previews/{self.usecase_id}/{run_id}/{index}.json"

    def start_detection_run(self, sample_keys, **overrides):
        status, started = self.start(
            model=MODEL, detection_prompt=PROMPT,
            task_type="ObjectDetection", label_set=LABELS,
            sample_images=list(sample_keys), **overrides)
        assert status == 202, started
        return started["run_id"]

    def sample_states(self, run_id):
        return [item.get("state") for item in self.sample_items(run_id)]


@pytest.fixture
def env(aws_stack, dda, monkeypatch):  # noqa: F811 — `dda` is the fixture
    monkeypatch.delenv("LLM_MODEL_IMAGE_LIMITS", raising=False)
    return ExecutorEnv(aws_stack, dda, monkeypatch)


# ------------------------------------------------------------- sequencing

class TestSequentialProcessing:
    """Req 3.1, 3.5: one Sample_Image at a time, in request order, each
    fully written before the next begins."""

    def test_invocations_and_writes_interleave_in_request_order(self, env):
        keys = [env.put_sample(f"seq-{index}.png") for index in range(3)]
        events = []
        recorder = env.use_prelabel(events=events)
        env.record_writes(events)

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome["status"] == "Completed"
        assert outcome["succeeded"] == 3
        # One event log, so "sequential" is asserted directly: sample i is
        # invoked, its payload is written, its item is resolved, and only
        # then is sample i+1 invoked.
        assert events == [
            ("invoke", keys[0]), ("payload", 0, env.result_key(run_id, 0)),
            ("item", 0, "Succeeded", True),
            ("invoke", keys[1]), ("payload", 1, env.result_key(run_id, 1)),
            ("item", 1, "Succeeded", True),
            ("invoke", keys[2]), ("payload", 2, env.result_key(run_id, 2)),
            ("item", 2, "Succeeded", True),
            ("run", "Completed"),
        ]
        assert [call["image_key"] for call in recorder.calls] == keys
        # The item sort keys carry the request order the log asserts.
        assert [item["task_id"] for item in env.sample_items(run_id)] == [
            "IMAGE#000", "IMAGE#001", "IMAGE#002"]

    def test_a_failed_sample_neither_stops_nor_reorders_the_rest(self, env):
        """Req 3.7: the loop continues, and the failed sample is written
        in its own place in the sequence."""
        keys = [env.put_sample(f"mixed-{index}.png") for index in range(3)]
        events = []
        env.use_prelabel(
            outcomes={keys[1]: LlmPrelabelError("timeout",
                                                "model invocation timed out")},
            events=events)
        env.record_writes(events)

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome == {"run_id": run_id, "action": "execute_preview_run",
                           "status": "Completed", "sample_count": 3,
                           "succeeded": 2, "failed": 1}
        assert events == [
            ("invoke", keys[0]), ("payload", 0, env.result_key(run_id, 0)),
            ("item", 0, "Succeeded", True),
            ("invoke", keys[1]), ("payload", 1, env.result_key(run_id, 1)),
            ("item", 1, "Failed", True),
            ("invoke", keys[2]), ("payload", 2, env.result_key(run_id, 2)),
            ("item", 2, "Succeeded", True),
            ("run", "Completed"),
        ]
        assert env.sample_states(run_id) == ["Succeeded", "Failed",
                                             "Succeeded"]


# ---------------------------------------------------------- per-sample writes

class TestPerSampleWrites:
    """Req 3.5, 3.7: each outcome is persisted the moment it resolves,
    payload first."""

    def test_payload_exists_before_the_item_that_references_it(self, env):
        keys = [env.put_sample(f"write-{index}.png") for index in range(2)]
        events = []
        env.use_prelabel(
            outcomes={keys[1]: LlmPrelabelError(
                "unusable_model_output",
                "unusable model output: no JSON object found",
                "I cannot help with that.")},
            events=events)
        env.record_writes(events)

        run_id = env.start_detection_run(keys)
        env.drive_executor()

        # Every item write named a key whose object was already there.
        item_events = [event for event in events if event[0] == "item"]
        assert [event[3] for event in item_events] == [True, True]

        succeeded, failed = env.sample_items(run_id)
        assert succeeded["state"] == "Succeeded"
        assert succeeded["result_s3_key"] == env.result_key(run_id, 0)
        assert "failure_category" not in succeeded
        assert int(succeeded["resolved_at"]) > 0
        assert failed["state"] == "Failed"
        assert failed["failure_category"] == "unusable_model_output"
        assert failed["failure_reason"] == (
            "unusable model output: no JSON object found")
        assert failed["result_s3_key"] == env.result_key(run_id, 1)

        assert env.result_payload(run_id, 0) == {
            "sample_key": keys[0], "state": "Succeeded",
            "prelabel": PRELABEL, "image_width": 120, "image_height": 90}
        assert env.result_payload(run_id, 1) == {
            "sample_key": keys[1], "state": "Failed",
            "failure_category": "unusable_model_output",
            "failure_reason": "unusable model output: no JSON object found",
            "raw_model_output": "I cannot help with that."}


# ---------------------------------------------------- terminal transitions

class TestTerminalRunTransition:
    """Req 3.7: Completed once every sample has an outcome; Failed only
    for a run-level failure."""

    def test_run_completes_when_every_sample_failed(self, env):
        keys = [env.put_sample(f"allfail-{index}.png") for index in range(3)]
        env.use_prelabel(outcomes={
            keys[0]: LlmPrelabelError("model_error", "model error: throttled"),
            keys[1]: LlmPrelabelError("timeout", "model invocation timed out"),
            keys[2]: LlmPrelabelError("unusable_model_output",
                                      "unusable model output: empty", ""),
        })

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome == {"run_id": run_id, "action": "execute_preview_run",
                           "status": "Completed", "sample_count": 3,
                           "succeeded": 0, "failed": 3}
        run_item = env.run_item(run_id)
        assert run_item["status"] == "Completed"
        assert "run_error" not in run_item
        assert env.sample_states(run_id) == ["Failed"] * 3
        assert [item["failure_category"] for item
                in env.sample_items(run_id)] == [
            "model_error", "timeout", "unusable_model_output"]

        # The status route reports the same terminal status to the wizard.
        status, polled = env.status(run_id)
        assert status == 200
        assert polled["status"] == "Completed"
        assert "run_error" not in polled
        assert [entry["state"] for entry in polled["results"]] == (
            ["Failed"] * 3)

    def test_a_run_level_write_failure_marks_the_run_failed(self, env):
        """Only a failure that prevents per-sample results reaches the
        run-level `Failed` status."""
        keys = [env.put_sample("runfail-0.png")]
        env.use_prelabel()
        env.break_helper("_write_preview_result_payload",
                         RuntimeError("artifacts bucket unavailable"))

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome["status"] == "Failed"
        assert "artifacts bucket unavailable" in outcome["error"]
        assert outcome["succeeded"] == 0 and outcome["failed"] == 0
        run_item = env.run_item(run_id)
        assert run_item["status"] == "Failed"
        assert "artifacts bucket unavailable" in run_item["run_error"]
        # The sample was never resolved, so the wizard sees it Pending.
        assert env.sample_states(run_id) == ["Pending"]


# --------------------------------------------------------------- lock release

class TestLockRelease:
    """Req 8.8: the in-flight claim is released on every terminal path,
    so the wizard's next iteration is always possible."""

    def test_lock_is_released_on_the_success_path(self, env):
        keys = [env.put_sample("lock-ok.png")]
        env.use_prelabel()

        run_id = env.start_detection_run(keys)
        # The claim is held for the life of the run.
        lock = env.lock_item()
        assert lock is not None and lock["run_id"] == run_id

        assert env.drive_executor()["status"] == "Completed"
        assert env.lock_item() is None

    def test_lock_is_released_when_a_helper_raises_unexpectedly(self, env):
        keys = [env.put_sample("lock-raise.png")]
        env.use_prelabel()
        env.break_helper("_read_preview_sample_items",
                         RuntimeError("DynamoDB is unavailable"))

        run_id = env.start_detection_run(keys)
        assert env.lock_item() is not None

        outcome = env.drive_executor()

        assert outcome["status"] == "Failed"
        assert "DynamoDB is unavailable" in outcome["error"]
        assert env.lock_item() is None

    def test_lock_is_released_when_the_terminal_status_write_fails(self, env):
        """Even the failure-status write failing must not strand the
        claim: the release lives in a `finally`."""
        keys = [env.put_sample("lock-status.png")]
        env.use_prelabel()
        env.break_helper("_update_preview_run_status",
                         RuntimeError("status write rejected"))

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome["status"] == "Failed"
        assert "status write rejected" in outcome["error"]
        # The run item keeps the status the start route wrote, and the
        # executor still returned normally rather than propagating.
        assert env.run_item(run_id)["status"] == "Running"
        assert env.lock_item() is None


# ----------------------------------------------------- one invocation, ever

class TestNoRetryOrSecondInvocation:
    """Req 3.1: exactly one model invocation per Sample_Image, whatever
    the outcome and however often the delivery is repeated."""

    def test_exactly_one_invocation_per_sample_including_failures(self, env):
        keys = [env.put_sample(f"once-{index}.png") for index in range(3)]
        recorder = env.use_prelabel(outcomes={
            keys[0]: LlmPrelabelError("timeout", "model invocation timed out"),
            # An unexpected exception is still one invocation, not a retry.
            keys[1]: RuntimeError("boom"),
        })

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome["succeeded"] == 1 and outcome["failed"] == 2
        assert recorder.counts == {key: 1 for key in keys}
        assert env.sample_states(run_id) == ["Failed", "Failed", "Succeeded"]
        assert [item.get("failure_category")
                for item in env.sample_items(run_id)][:2] == [
            "timeout", "model_error"]

    def test_a_model_error_is_not_retried_at_the_converse_layer(self, env):
        """With the real `generate_llm_prelabel` in the loop, a failing
        sample issues one Converse request and no second attempt."""
        keys = [env.put_sample("converse-fail.png"),
                env.put_sample("converse-ok.png")]
        throttled = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "Converse")
        stub, recorded = env.use_bedrock([throttled, guidance([BOX])])

        run_id = env.start_detection_run(keys)
        outcome = env.drive_executor()

        assert outcome["succeeded"] == 1 and outcome["failed"] == 1
        # Two samples, two requests: no retry for the failing one.
        assert len(stub.calls) == 2
        assert [call["modelId"] for call in stub.calls] == [MODEL_ID, MODEL_ID]
        assert recorded["timeout_seconds"] == 120
        failed, succeeded = env.sample_items(run_id)
        assert failed["failure_category"] == "model_error"
        assert succeeded["state"] == "Succeeded"

    def test_a_duplicated_delivery_of_a_completed_run_is_skipped(self, env):
        keys = [env.put_sample("dup-0.png"), env.put_sample("dup-1.png")]
        recorder = env.use_prelabel()

        run_id = env.start_detection_run(keys)
        assert env.drive_executor()["status"] == "Completed"
        assert len(recorder.calls) == 2

        replayed = env.replay_executor(run_id)

        assert replayed == {"run_id": run_id,
                            "action": "execute_preview_run",
                            "status": "Completed", "skipped": True}
        # Zero further invocations, and the results are untouched.
        assert len(recorder.calls) == 2
        assert env.sample_states(run_id) == ["Succeeded", "Succeeded"]

    def test_resolved_samples_are_not_re_invoked_on_a_replay(self, env):
        """The per-sample guard, independent of the run-status guard: a
        run forced back to Running re-invokes nothing, so no Sample_Image
        can ever receive a second invocation."""
        keys = [env.put_sample("replay-0.png"), env.put_sample("replay-1.png")]
        recorder = env.use_prelabel()

        run_id = env.start_detection_run(keys)
        env.drive_executor()
        first_payloads = [env.result_payload(run_id, index)
                          for index in range(2)]

        env.force_run_status(run_id, "Running")
        replayed = env.replay_executor(run_id)

        assert replayed["status"] == "Completed"
        assert replayed["sample_count"] == 2
        assert replayed["succeeded"] == 0 and replayed["failed"] == 0
        assert len(recorder.calls) == 2
        assert [env.result_payload(run_id, index)
                for index in range(2)] == first_payloads

    def test_an_unknown_run_id_invokes_nothing(self, env):
        recorder = env.use_prelabel()

        outcome = env.replay_executor("preview-does-not-exist")

        assert outcome == {"run_id": "preview-does-not-exist",
                           "action": "execute_preview_run",
                           "error": "preview run not found"}
        assert recorder.calls == []
        assert json.dumps(outcome)  # the async response stays serializable
