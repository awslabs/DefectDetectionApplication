"""
Grounded_SAM_Preview_Run executor in dda_labeling.py
(grounded-sam-prompt-tuning-preview, task 1.5).

Feature: grounded-sam-prompt-tuning-preview

Example-based coverage of `execute_preview_run`'s grounded-sam family,
driven through the same `{'action': 'execute_preview_run', 'run_id':
...}` payload the deployed function receives, against the moto-backed
stack from conftest.py. The harness is reused rather than re-created:
`ExecutorEnv` from test_dda_labeling_preview_executor.py (and through
it `PreviewFlowEnv` / `PreviewEnv` / `CreateJobEnv`) supplies the
Use_Case, dataset prefix, authorized creator, the start/status route
builders, the ordered write log with its payload-readable probe, and
the inline executor driving. The worker is a fake Lambda client
injected at the module's own `grounded_sam_lambda_client` seam — the
test_dda_grounded_sam_consumer.py convention.

What is asserted here, and nowhere else:

- **The preview's Lambda client config** (Req 4.1): the
  `_get_grounded_sam_preview_lambda_client` factory hands
  boto3.client('lambda') a BotoConfig bounding the synchronous
  invocation wall clock at 240 s (`PREVIEW_GSAM_PER_SAMPLE_SECONDS` as
  the read timeout, connect timeout 10 s, retries disabled) — captured
  through a monkeypatched boto3.client with the injection seam and the
  cache both neutralized, the test_dda_grounded_sam_consumer.py
  captured-config pattern.
- **Sequential request-order processing** (Req 4.9): with N
  Sample_Images the worker invocations arrive in request order
  (`IMAGE#000`, `001`, ...) and each sample's result payload object
  exists in S3 *before* the item update that references it — asserted
  from a single recorded event log whose item entries probe the payload
  object at the moment the item write is issued.
- **Representative categorized failures beside a succeeding sibling**
  (Req 4.5): a `FunctionError` whose body exceeds 512 characters
  resolves `model_error` with the body truncated to 512; unparseable
  payload bytes resolve `model_error`; an out-of-Label_Set class
  resolves `model_error` naming the class — and the sibling sample in
  the same run still resolves Succeeded.
- **Terminal transition with every sample failed** (Req 4.8): the run
  still reaches `Completed` with no `run_error`, and the in-flight lock
  is released.

Payload-derivation equivalence, response-validation equivalence,
failure-categorization totality, the Run_Deadline_Guard, and the
absence of labeling-pipeline state are Properties 5-8 and 10 in
test_property_gsam_preview_executor.py and are deliberately not
repeated here.

Requirements: 4.1, 4.5, 4.8, 4.9
"""
import io
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from test_dda_labeling_create_job import DATASET_BUCKET
from test_dda_labeling_preview_executor import ExecutorEnv
from test_preview_flow_integration import dda  # noqa: F401 — pytest fixture

LABELS = ["scratch", "dent"]
GSAM_FUNCTION = "test-dda-grounded-sam-worker"
FUNCTION_NAME = "test-dda-labeling-gsam-preview"

# A worker response every transcribed validation rule accepts; what the
# fake replays for any sample without a scripted outcome. Its region
# carries a score, which the preview keeps in the stored Segmentation
# shape.
SEG_RESPONSE = {
    "regions": [{"class": "scratch", "rle": "0 12 5 3", "score": 0.87}],
    "image_width": 120,
    "image_height": 90,
}


def presigned_object_key(url):
    """The S3 object key a presigned URL grants, handling both the
    virtual-hosted style (path is `/{key}`) and the path style (path is
    `/{bucket}/{key}`)."""
    path = urlparse(url).path.lstrip("/")
    if path.startswith(f"{DATASET_BUCKET}/"):
        path = path[len(DATASET_BUCKET) + 1:]
    return path


# ------------------------------------------------------- fake worker client

class FunctionErrorReply:
    """A worker invocation that returns FunctionError with these raw
    payload bytes as the error body."""

    def __init__(self, body: bytes):
        self.body = body


class RawReply:
    """A 200 invocation whose payload bytes are returned verbatim —
    the unparseable-output case when they are not JSON."""

    def __init__(self, body: bytes):
        self.body = body


class FakeGroundedSamWorkerClient:
    """Records synchronous Grounded-SAM worker invocations and replays
    a scripted per-sample outcome (the FakeSamLambdaClient shape with
    per-key scripting, keyed on the presigned URL's object key).

    `outcomes` maps a Sample_Image key to an Exception to raise, a
    FunctionErrorReply, a RawReply, or a payload dict to return as JSON;
    anything unlisted succeeds with SEG_RESPONSE.
    """

    def __init__(self, outcomes=None, events=None):
        self.outcomes = outcomes or {}
        self.invocations = []
        self.events = events if events is not None else []

    @property
    def invoked_keys(self):
        """The Sample_Image keys behind the invocations, in call order."""
        return [
            presigned_object_key(
                json.loads(call["Payload"])["image_s3_presigned_url"])
            for call in self.invocations]

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        key = presigned_object_key(
            json.loads(kwargs["Payload"])["image_s3_presigned_url"])
        self.events.append(("invoke", key))
        outcome = self.outcomes.get(key, SEG_RESPONSE)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, FunctionErrorReply):
            return {"StatusCode": 200, "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(outcome.body)}
        if isinstance(outcome, RawReply):
            return {"StatusCode": 200, "Payload": io.BytesIO(outcome.body)}
        return {"StatusCode": 200,
                "Payload": io.BytesIO(json.dumps(outcome).encode())}


# ---------------------------------------------------------------- harness

class GsamExecutorEnv(ExecutorEnv):
    """ExecutorEnv (ordered write log, result keys, state readers) plus
    the grounded-sam seams: the worker function name, the injected fake
    worker client, and an executor context carrying remaining-time
    headroom so the Run_Deadline_Guard never trips in these examples."""

    def __init__(self, stack, dda, monkeypatch):
        super().__init__(stack, dda, monkeypatch)
        self.context = SimpleNamespace(
            function_name=FUNCTION_NAME,
            get_remaining_time_in_millis=lambda: 900_000)
        # The deployed value compute-stack.ts wires inside the
        # deployGroundedSamWorker-gated block; without it the start
        # route answers the not-deployed validation error.
        monkeypatch.setattr(self.module, "GROUNDED_SAM_WORKER_FUNCTION_NAME",
                            GSAM_FUNCTION)

    def use_worker(self, outcomes=None, events=None):
        """Inject the fake worker at `grounded_sam_lambda_client` — the
        module's own seam, taken before any boto3 construction."""
        fake = FakeGroundedSamWorkerClient(outcomes=outcomes, events=events)
        self.monkeypatch.setattr(self.module, "grounded_sam_lambda_client",
                                 fake)
        return fake

    def start_gsam_run(self, sample_keys, **overrides):
        """One accepted grounded-sam Segmentation start (202)."""
        status, started = self.start(
            model="grounded-sam", detection_prompt=None,
            task_type="Segmentation", label_set=LABELS,
            sample_images=list(sample_keys), **overrides)
        assert status == 202, started
        return started["run_id"]


@pytest.fixture
def env(aws_stack, dda, monkeypatch):  # noqa: F811 — `dda` is the fixture
    return GsamExecutorEnv(aws_stack, dda, monkeypatch)


# --------------------------------------------------------- client config

class TestPreviewWorkerClientConfig:
    """Req 4.1: the preview's synchronous worker invocations ride a
    Lambda client bounding the read timeout at the family's 240 s
    per-sample bound with retries disabled — the consumer's client
    construction verbatim, behind this module's own injection seam."""

    def test_client_bounds_reads_at_240s_with_retries_disabled(
            self, dda, monkeypatch):
        module = dda.module
        assert module.PREVIEW_GSAM_PER_SAMPLE_SECONDS == 240

        captured = {}

        def capturing_client(service_name, **kwargs):
            captured["service"] = service_name
            captured["config"] = kwargs.get("config")
            return object()  # stands in for the boto3 Lambda client

        # Neutralize the injection seam and the cache so the factory
        # actually constructs, then capture what it hands boto3.
        monkeypatch.setattr(module, "grounded_sam_lambda_client", None)
        monkeypatch.setattr(module, "_cached_grounded_sam_lambda_client",
                            None)
        monkeypatch.setattr(module.boto3, "client", capturing_client)

        module._get_grounded_sam_preview_lambda_client()

        assert captured["service"] == "lambda"
        config = captured["config"]
        assert config.connect_timeout == 10
        assert config.read_timeout == 240
        assert config.read_timeout == module.PREVIEW_GSAM_PER_SAMPLE_SECONDS
        assert config.retries == {"max_attempts": 0}


# ------------------------------------------------------------- sequencing

class TestSequentialRequestOrderProcessing:
    """Req 4.9: Sample_Images are processed sequentially in request
    order, each result payload written to S3 before the sample item
    that references it."""

    def test_invocations_and_writes_interleave_in_request_order(self, env):
        keys = [env.put_sample(f"gsam-seq-{index}.png") for index in range(3)]
        events = []
        fake = env.use_worker(events=events)
        env.record_writes(events)

        run_id = env.start_gsam_run(keys)
        outcome = env.drive_executor()

        assert outcome == {"run_id": run_id, "action": "execute_preview_run",
                           "status": "Completed", "sample_count": 3,
                           "succeeded": 3, "failed": 0}
        # One event log, so "sequential in request order" is asserted
        # directly: sample i is invoked, its payload is written, its
        # item is resolved — with the payload object already readable
        # (the True flag) — and only then is sample i+1 invoked.
        assert events == [
            ("invoke", keys[0]), ("payload", 0, env.result_key(run_id, 0)),
            ("item", 0, "Succeeded", True),
            ("invoke", keys[1]), ("payload", 1, env.result_key(run_id, 1)),
            ("item", 1, "Succeeded", True),
            ("invoke", keys[2]), ("payload", 2, env.result_key(run_id, 2)),
            ("item", 2, "Succeeded", True),
            ("run", "Completed"),
        ]
        # The worker saw the samples in request order, one synchronous
        # invoke of the configured function per Sample_Image.
        assert fake.invoked_keys == keys
        for call in fake.invocations:
            assert call["FunctionName"] == GSAM_FUNCTION
            assert call["InvocationType"] == "RequestResponse"
        # The item sort keys carry the request order the log asserts.
        assert [item["task_id"] for item in env.sample_items(run_id)] == [
            "IMAGE#000", "IMAGE#001", "IMAGE#002"]
        # The written payload is the renderer-shaped success payload
        # with the worker's dimensions and the score carried through.
        assert env.result_payload(run_id, 0) == {
            "sample_key": keys[0], "state": "Succeeded",
            "prelabel": {"modality": "Segmentation",
                         "regions": SEG_RESPONSE["regions"],
                         "image_width": 120, "image_height": 90},
            "image_width": 120, "image_height": 90}


# ------------------------------------------------- categorized failures

class TestCategorizedFailuresBesideASucceedingSibling:
    """Req 4.5: representative worker failures each resolve Failed with
    the `model_error` category and the worker's detail carried in the
    reason, while a sibling sample of the same run still succeeds."""

    def test_each_failure_categorized_with_the_worker_detail(self, env):
        keys = [env.put_sample(f"gsam-cat-{index}.png") for index in range(4)]
        error_body = "worker exploded: " + "x" * 600
        assert len(error_body) > 512  # the truncation below actually bites
        env.use_worker(outcomes={
            keys[0]: FunctionErrorReply(error_body.encode()),
            keys[1]: RawReply(b"<html>bad gateway</html>"),
            keys[2]: {"regions": [{"class": "intruder", "rle": "0 4"}],
                      "image_width": 120, "image_height": 90},
            # keys[3] is unlisted: SEG_RESPONSE, the succeeding sibling.
        })

        run_id = env.start_gsam_run(keys)
        outcome = env.drive_executor()

        assert outcome["status"] == "Completed"
        assert outcome["succeeded"] == 1 and outcome["failed"] == 3
        function_error, unparseable, bad_class, sibling = (
            env.sample_items(run_id))

        # A FunctionError carries the worker's error body, truncated to
        # the consumer's 512 characters.
        assert function_error["state"] == "Failed"
        assert function_error["failure_category"] == "model_error"
        assert function_error["failure_reason"] == (
            "Grounded-SAM worker failed: " + error_body[:512])

        # Unparseable payload bytes are a model_error with the parse
        # detail.
        assert unparseable["state"] == "Failed"
        assert unparseable["failure_category"] == "model_error"
        assert unparseable["failure_reason"].startswith(
            "Grounded-SAM worker returned unparseable output: ")

        # An out-of-Label_Set class is a model_error naming the class.
        assert bad_class["state"] == "Failed"
        assert bad_class["failure_category"] == "model_error"
        assert bad_class["failure_reason"] == (
            "Grounded-SAM worker returned class 'intruder', "
            "which is not in the job's label set ['scratch', 'dent']")

        # The sibling sample resolved independently, as a success.
        assert sibling["state"] == "Succeeded"
        assert "failure_category" not in sibling

        # The failure payloads carry the same category and reason; the
        # sibling's carries its Pre_Label.
        assert env.result_payload(run_id, 0) == {
            "sample_key": keys[0], "state": "Failed",
            "failure_category": "model_error",
            "failure_reason": ("Grounded-SAM worker failed: "
                               + error_body[:512])}
        assert env.result_payload(run_id, 3)["state"] == "Succeeded"
        assert env.result_payload(run_id, 3)["prelabel"]["regions"] == (
            SEG_RESPONSE["regions"])


# ------------------------------------------------------ terminal + lock

class TestAllFailedRunCompletesAndReleasesTheLock:
    """Req 4.8: a run in which every Sample_Image failed still reaches
    Completed — `Failed` stays reserved for run-level failures — and
    the in-flight lock is released on the terminal path."""

    def test_all_failed_run_reaches_completed_with_the_lock_released(
            self, env):
        keys = [env.put_sample(f"gsam-allfail-{index}.png")
                for index in range(2)]
        env.use_worker(outcomes={
            keys[0]: FunctionErrorReply(b"worker crashed"),
            keys[1]: RawReply(b"not json"),
        })

        run_id = env.start_gsam_run(keys)
        # The claim is held for the life of the run.
        lock = env.lock_item()
        assert lock is not None and lock["run_id"] == run_id

        outcome = env.drive_executor()

        assert outcome == {"run_id": run_id, "action": "execute_preview_run",
                           "status": "Completed", "sample_count": 2,
                           "succeeded": 0, "failed": 2}
        run_item = env.run_item(run_id)
        assert run_item["status"] == "Completed"
        assert "run_error" not in run_item
        assert env.sample_states(run_id) == ["Failed", "Failed"]
        assert [item["failure_category"]
                for item in env.sample_items(run_id)] == [
            "model_error", "model_error"]
        assert env.lock_item() is None

        # The status route reports the same terminal status to the
        # panel, one Failed entry per sample.
        status, polled = env.status(run_id)
        assert status == 200
        assert polled["status"] == "Completed"
        assert "run_error" not in polled
        assert [entry["state"] for entry in polled["results"]] == (
            ["Failed"] * 2)
