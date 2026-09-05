"""
dda_llm_prelabel.py — the one `llm:` family invocation implementation,
shared by the Auto_Labeler and the Prompt_Tuning_Preview.

Spec: llm-autolabel-prompt-tuning, task 2.4 (example-based unit tests).

Covers, against the moto-backed stack from conftest.py with a recording
stub Converse client:

- Client construction: `get_bedrock_client` is called with the resolved
  region and the read timeout clamped to
  `min(config['timeout_seconds'], 120)`, and the real
  `bedrock_common.get_bedrock_client` builds that client with retries
  disabled — so total wall time cannot exceed the bound and no branch
  re-invokes (Req 3.1, 3.3)
- Exactly one `converse` call per invocation for every outcome:
  success, timeout, model error, and unusable model output (Req 3.1)
- Exception -> category mapping: `ReadTimeoutError` and
  `ConnectTimeoutError` -> `timeout`; a throttling `ClientError`, a
  validation `ClientError` and a generic exception -> `model_error`;
  `GuidanceError` -> `unusable_model_output` (Req 3.10, 9.1, 9.2, 9.3)
- `raw_text` is populated only when a response was received: the model's
  text character-for-character on `unusable_model_output`, and `None`
  for timeouts, model errors, and a response carrying no text block at
  all (Req 9.3)
- Worker delegation: each `LlmPrelabelError` category surfaces through
  `dda_autolabel_worker` as the pre-feature `GenerationFailure` reason
  string recorded in `prelabel_error` (Req 3.10, 3.11)

Requirements: 3.1, 3.3, 3.10, 3.11, 9.1, 9.2, 9.3
"""
import json
import os
import sys
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from test_dda_autolabel_worker import (
    AutolabelEnv,
    DATASET_BUCKET,
    SAM_FUNCTION,
    png_bytes,
)

REGION = "us-east-1"
MODEL_ID = "us.amazon.nova-pro-v1:0"
MODEL = f"llm:{MODEL_ID}"
LABELS = ["scratch", "dent"]
# Awkward on purpose: whitespace, quotes and a newline must survive
# verbatim into the prompt.
PROMPT = '  Find every "scratch" {and dent}\n  on the panel.  '

WIDTH, HEIGHT = 100, 80

# The pre-feature reason strings the Auto_Labeler records for the `llm:`
# family (llm-auto-labeling design): translating LlmPrelabelError back
# into GenerationFailure must reproduce these character-for-character.
TIMEOUT_REASON = "model invocation timed out after 120s"
MODEL_ERROR_REASON = "model error: kaboom"
UNPARSEABLE_REASON = "model output contains no parseable JSON object"


def guidance(detections):
    return json.dumps({"detections": detections})


BOX = {"class": "scratch",
       "box": {"left": 10, "top": 5, "width": 30, "height": 20}}


def client_error(code, message):
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "Converse")


class RecordingConverseClient:
    """Records every `converse` call; replies with canned text or raises."""

    def __init__(self, reply=None, error=None, response=None):
        self.calls = []
        self.reply = reply
        self.error = error
        self.response = response

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        return {"output": {"message": {"content": [{"text": self.reply}]}}}


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def prelabel(aws_stack):
    """The real dda_llm_prelabel imported inside the moto mock."""
    import dda_llm_prelabel
    return dda_llm_prelabel


@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_autolabel_worker imported inside the moto mock."""
    os.environ["SAM_WORKER_FUNCTION_NAME"] = SAM_FUNCTION
    sys.modules.pop("dda_autolabel_worker", None)
    import dda_autolabel_worker

    dda_autolabel_worker.SAM_WORKER_FUNCTION_NAME = SAM_FUNCTION
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=DATASET_BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return dda_autolabel_worker


@pytest.fixture
def worker_env(aws_stack, worker, monkeypatch):
    return AutolabelEnv(aws_stack, worker, monkeypatch)


def bind_client(prelabel, monkeypatch, client, timeout_seconds=240,
                region="us-west-2"):
    """Bind a stub Converse client into the module, recording the
    (region, timeout) the module asks for."""
    recorded = {}
    monkeypatch.setattr(prelabel, "get_bedrock_configuration", lambda: {
        "model_id": MODEL_ID,
        "region": region,
        "max_tokens": 4096,
        "temperature": None,
        "top_p": None,
        "timeout_seconds": timeout_seconds,
    })

    def factory(region_arg, timeout_arg):
        recorded["region"] = region_arg
        recorded["timeout_seconds"] = timeout_arg
        return client

    monkeypatch.setattr(prelabel, "get_bedrock_client", factory)
    return recorded


def invoke(prelabel, modality="ObjectDetection", label_set=None,
           image_key="imgs/a.png"):
    return prelabel.generate_llm_prelabel(
        model_identifier=MODEL_ID,
        modality=modality,
        label_set=label_set if label_set is not None else LABELS,
        detection_prompt=PROMPT,
        per_label_prompts=None,
        image_bytes=png_bytes(WIDTH, HEIGHT),
        image_key=image_key,
        width=WIDTH,
        height=HEIGHT,
    )


# ------------------------------------------------------- client construction

class TestClientConstruction:
    """Req 3.1, 3.3: one bounded invocation, retries disabled."""

    def test_read_timeout_clamped_to_120_seconds(self, prelabel, monkeypatch):
        """A configured timeout above the 120 s bound is clamped, and
        the module's resolved region reaches the factory."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        recorded = bind_client(prelabel, monkeypatch, client,
                              timeout_seconds=240, region="eu-west-1")

        invoke(prelabel)

        assert recorded["timeout_seconds"] == 120
        assert recorded["region"] == "eu-west-1"

    def test_configured_timeout_below_bound_is_used_as_is(self, prelabel,
                                                          monkeypatch):
        """min(config, 120) keeps a smaller configured timeout."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        recorded = bind_client(prelabel, monkeypatch, client,
                              timeout_seconds=45)

        invoke(prelabel)

        assert recorded["timeout_seconds"] == 45

    def test_timeout_reason_reports_the_clamped_bound(self, prelabel,
                                                      monkeypatch):
        """The recorded timeout reason names the bound actually applied."""
        client = RecordingConverseClient(
            error=ReadTimeoutError(endpoint_url="https://bedrock.test"))
        bind_client(prelabel, monkeypatch, client, timeout_seconds=45)

        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel)

        assert excinfo.value.reason == "model invocation timed out after 45s"

    def test_real_client_disables_retries(self):
        """bedrock_common builds the client with retries disabled and a
        read timeout equal to the requested bound, so no re-invocation
        can happen inside one call."""
        import bedrock_common

        client = bedrock_common.get_bedrock_client(REGION, 120)

        # botocore normalizes max_attempts=0 into a total attempt budget
        # of one: the initial request and no retry.
        retries = client.meta.config.retries
        assert retries.get("total_max_attempts",
                           retries.get("max_attempts", 0) + 1) == 1
        assert client.meta.config.read_timeout == 120

    @pytest.mark.parametrize("client_kwargs", [
        {"reply": guidance([BOX])},                       # success
        {"reply": "nothing to report here"},              # unusable output
        {"error": ReadTimeoutError(endpoint_url="https://b.test")},
        {"error": client_error("ThrottlingException", "slow down")},
        {"error": RuntimeError("kaboom")},
        {"response": {"output": {"message": {"content": []}}}},
    ])
    def test_exactly_one_converse_call_per_invocation(self, prelabel,
                                                      monkeypatch,
                                                      client_kwargs):
        """Req 3.1: one call per invocation regardless of outcome."""
        client = RecordingConverseClient(**client_kwargs)
        bind_client(prelabel, monkeypatch, client)

        try:
            invoke(prelabel)
        except prelabel.LlmPrelabelError:
            pass

        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["modelId"] == MODEL_ID
        assert "maxTokens" in call["inferenceConfig"]

    def test_success_returns_the_modality_prelabel(self, prelabel,
                                                  monkeypatch):
        """The happy path returns the converted Pre_Label and sends the
        image block plus the verbatim Detection_Prompt."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        result = invoke(prelabel)

        assert result["modality"] == "ObjectDetection"
        assert [box["class"] for box in result["boxes"]] == ["scratch"]
        content = client.calls[0]["messages"][0]["content"]
        assert content[0]["image"]["format"] == "png"
        assert content[0]["image"]["source"]["bytes"].startswith(b"\x89PNG")
        assert PROMPT in content[1]["text"]


# ------------------------------------------------------- failure categories

class TestFailureCategories:
    """Req 3.10, 9.1, 9.2, 9.3: one category per failure, distinct."""

    def _failure(self, prelabel, monkeypatch, **client_kwargs):
        client = RecordingConverseClient(**client_kwargs)
        bind_client(prelabel, monkeypatch, client)
        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel)
        return excinfo.value

    def test_read_timeout_is_timeout_category(self, prelabel, monkeypatch):
        error = self._failure(
            prelabel, monkeypatch,
            error=ReadTimeoutError(endpoint_url="https://bedrock.test"))

        assert error.category == prelabel.CATEGORY_TIMEOUT
        assert error.reason == TIMEOUT_REASON
        assert error.raw_text is None

    def test_connect_timeout_is_timeout_category(self, prelabel, monkeypatch):
        error = self._failure(
            prelabel, monkeypatch,
            error=ConnectTimeoutError(endpoint_url="https://bedrock.test"))

        assert error.category == prelabel.CATEGORY_TIMEOUT
        assert error.reason == TIMEOUT_REASON
        assert error.raw_text is None

    @pytest.mark.parametrize("error,fragment", [
        (client_error("ThrottlingException", "slow down"), "ThrottlingException"),
        (client_error("ValidationException", "bad image"), "ValidationException"),
        (RuntimeError("kaboom"), "kaboom"),
    ])
    def test_invocation_errors_are_model_error_category(self, prelabel,
                                                        monkeypatch, error,
                                                        fragment):
        """Throttling, validation and generic errors alike are model
        errors, carrying the invocation's own description."""
        failure = self._failure(prelabel, monkeypatch, error=error)

        assert failure.category == prelabel.CATEGORY_MODEL_ERROR
        assert failure.reason.startswith("model error: ")
        assert fragment in failure.reason
        assert failure.raw_text is None

    def test_timeout_and_model_error_reasons_are_distinguishable(
            self, prelabel, monkeypatch):
        timeout = self._failure(
            prelabel, monkeypatch,
            error=ReadTimeoutError(endpoint_url="https://bedrock.test"))
        model_error = self._failure(prelabel, monkeypatch,
                                    error=RuntimeError("kaboom"))

        assert timeout.category != model_error.category
        assert "timed out" not in model_error.reason
        assert "model error" not in timeout.reason

    def test_unparseable_output_carries_raw_text_verbatim(self, prelabel,
                                                          monkeypatch):
        """Req 9.3: the guidance reason plus the model's text
        character-for-character."""
        raw = '  I could not find anything.\n"maybe" a {scratch}?  '
        failure = self._failure(prelabel, monkeypatch, reply=raw)

        assert failure.category == prelabel.CATEGORY_UNUSABLE_MODEL_OUTPUT
        assert failure.reason == UNPARSEABLE_REASON
        assert failure.raw_text == raw

    def test_class_outside_label_set_is_unusable_output_with_raw_text(
            self, prelabel, monkeypatch):
        """A structurally valid document rejected by validation is still
        unusable model output, with the response text preserved."""
        raw = guidance([{"class": "crack",
                         "box": {"left": 1, "top": 1,
                                 "width": 5, "height": 5}}])
        failure = self._failure(prelabel, monkeypatch, reply=raw)

        assert failure.category == prelabel.CATEGORY_UNUSABLE_MODEL_OUTPUT
        assert "crack" in failure.reason
        assert failure.raw_text == raw

    def test_response_without_text_block_has_no_raw_text(self, prelabel,
                                                         monkeypatch):
        """A response was received but carries no text at all: unusable
        output with nothing to show for raw output."""
        failure = self._failure(
            prelabel, monkeypatch,
            response={"output": {"message": {"content": [{"image": {}}]}}})

        assert failure.category == prelabel.CATEGORY_UNUSABLE_MODEL_OUTPUT
        assert failure.reason == "model response contained no text output"
        assert failure.raw_text is None

    def test_response_text_joins_every_text_block(self, prelabel):
        """response_text concatenates text blocks in order, so a split
        response still reaches the parser whole."""
        response = {"output": {"message": {"content": [
            {"text": '{"detections":'}, {"image": {}}, {"text": " []}"},
        ]}}}

        assert prelabel.response_text(response) == '{"detections":\n []}'

    def test_response_text_rejects_a_textless_response(self, prelabel):
        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            prelabel.response_text({"output": {"message": {"content": []}}})

        assert (excinfo.value.category
                == prelabel.CATEGORY_UNUSABLE_MODEL_OUTPUT)


# --------------------------------------------------------- worker delegation

class TestWorkerDelegation:
    """Req 3.10, 3.11: every category becomes the pre-feature
    GenerationFailure reason in `prelabel_error`."""

    def _run(self, env, reply=None, error=None):
        job_id = env.make_job(task_type="ObjectDetection", label_set=LABELS,
                              model=MODEL)
        image_uri = env.put_image(f"imgs/{uuid.uuid4()}.png",
                                  width=WIDTH, height=HEIGHT)
        task_id = env.make_task(job_id, image_uri)
        bedrock, _ = env.use_bedrock(
            replies=[reply] if reply is not None else None, error=error)
        result = env.run([env.record(job_id, task_id, image_uri,
                                     "ObjectDetection", LABELS, MODEL,
                                     detection_prompt=PROMPT)])
        assert result == {"batchItemFailures": []}
        return job_id, task_id, bedrock

    def _assert_failed(self, env, job_id, task_id, reason):
        task = env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Failed"
        assert task["prelabel_error"] == reason
        assert "prelabel_s3_key" not in task
        assert not env.prelabel_exists(job_id, task_id)

    def test_timeout_category_reason(self, worker_env):
        job_id, task_id, bedrock = self._run(
            worker_env,
            error=ReadTimeoutError(endpoint_url="https://bedrock.test"))

        self._assert_failed(worker_env, job_id, task_id, TIMEOUT_REASON)
        assert len(bedrock.calls) == 1

    def test_connect_timeout_category_reason(self, worker_env):
        job_id, task_id, bedrock = self._run(
            worker_env,
            error=ConnectTimeoutError(endpoint_url="https://bedrock.test"))

        self._assert_failed(worker_env, job_id, task_id, TIMEOUT_REASON)
        assert len(bedrock.calls) == 1

    def test_model_error_category_reason(self, worker_env):
        job_id, task_id, bedrock = self._run(worker_env,
                                             error=RuntimeError("kaboom"))

        self._assert_failed(worker_env, job_id, task_id, MODEL_ERROR_REASON)
        assert len(bedrock.calls) == 1

    def test_unusable_model_output_category_reason(self, worker_env):
        job_id, task_id, bedrock = self._run(
            worker_env, reply="I could not find anything to report.")

        self._assert_failed(worker_env, job_id, task_id, UNPARSEABLE_REASON)
        assert len(bedrock.calls) == 1

    def test_success_still_marks_available(self, worker_env):
        """The delegation keeps the success path intact: one call, the
        Pre_Label stored, the task Available."""
        job_id, task_id, bedrock = self._run(worker_env,
                                             reply=guidance([BOX]))

        task = worker_env.get_task(job_id, task_id)
        assert task["prelabel_status"] == "Available"
        assert len(bedrock.calls) == 1
        prelabel_json = worker_env.prelabel_json(job_id, task_id)
        assert prelabel_json["modality"] == "ObjectDetection"
        assert [box["class"] for box in prelabel_json["boxes"]] == ["scratch"]
