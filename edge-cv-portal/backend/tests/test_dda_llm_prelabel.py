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

Extended for llm-model-token-and-image-sizing, task 4.3 — the sizing
and budget seams of the same chokepoint:

- The Image_Downscaler runs exactly once for the target image and once
  per attached Few_Shot_Example, and not at all at Downscale_Off
  (Req 6.1, 8.1)
- The Detection_Prompt carries the Sent_Dimensions of the image
  actually sent; `parse_guidance` validates in Sent space and
  `guidance_to_prelabel` converts in Source space, with the geometry
  scaled back between the two (Req 7.1, 7.2)
- `build_inference_config`'s returned dict is never mutated in place:
  `maxTokens` is replaced on a copy and the sampling parameters pass
  through unchanged (Req 1.3, 10.2)
- The Effective_Token_Budget tiers reach the Converse request — a valid
  Token_Budget_Selection wins, then the Model_Token_Limits entry, then
  the 10000 default — never the Global_Max_Tokens (Req 1.3)
- An Image_Downscaler refusal of the target is
  `unsupported_image_content` and of an attached example is
  `unreadable_example_image`, each carrying the design's error-table
  reason with zero invocations (Req 9.1)
"""
import io
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

# The Global_Max_Tokens carried by bind_client's Bedrock_Configuration
# stub. Deliberately different from every budget the sizing tests
# resolve (20000 / 5000 / 10000), so a sent maxTokens equal to it would
# mean the global value leaked through
# (llm-model-token-and-image-sizing Req 1.3).
GLOBAL_MAX_TOKENS = 4096

# An exact 2x reduction for the downscale tests: a 1024x800 source at
# the 512 bound sends 512x400, so every hand-checked coordinate in the
# Sent -> Source scale-back is a whole number.
SOURCE_W, SOURCE_H = 1024, 800
SENT_W, SENT_H = 512, 400

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
                region="us-west-2", temperature=None, top_p=None):
    """Bind a stub Converse client into the module, recording the
    (region, timeout) the module asks for."""
    recorded = {}
    monkeypatch.setattr(prelabel, "get_bedrock_configuration", lambda: {
        "model_id": MODEL_ID,
        "region": region,
        "max_tokens": GLOBAL_MAX_TOKENS,
        "temperature": temperature,
        "top_p": top_p,
        "timeout_seconds": timeout_seconds,
    })

    def factory(region_arg, timeout_arg):
        recorded["region"] = region_arg
        recorded["timeout_seconds"] = timeout_arg
        return client

    monkeypatch.setattr(prelabel, "get_bedrock_client", factory)
    return recorded


def invoke(prelabel, modality="ObjectDetection", label_set=None,
           image_key="imgs/a.png", **overrides):
    """Call generate_llm_prelabel with the file's standard arguments.

    `overrides` update or extend the keyword arguments, so the sizing
    tests can pass their own image bytes, Source_Dimensions,
    `downscale_setting`, `token_budget_selection`, `model_token_limits`
    and `few_shot_images` while every pre-existing call keeps its exact
    pre-feature argument set.
    """
    kwargs = dict(
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
    kwargs.update(overrides)
    return prelabel.generate_llm_prelabel(**kwargs)


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
        image block plus the verbatim Detection_Prompt.

        Since llm-model-token-and-image-sizing task 4.1 the chokepoint
        returns `LlmPrelabelResult(prelabel, sent_width, sent_height)`;
        the Pre_Label itself, and every assertion on it, is unchanged.
        """
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        result = invoke(prelabel)

        assert result.prelabel["modality"] == "ObjectDetection"
        assert [box["class"] for box in result.prelabel["boxes"]] == ["scratch"]
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


# ---------------------------------------------------------------------------
# llm-model-token-and-image-sizing, task 4.3 — the sizing and budget
# seams of the same chokepoint (Req 1.3, 6.1, 7.1, 7.2, 8.1, 9.1, 10.2)
# ---------------------------------------------------------------------------

def real_png_bytes(width, height):
    """A fully decodable PNG — unlike `png_bytes`' header-only bytes —
    for the tests that drive the resize path through a real decode."""
    from PIL import Image  # lazy, matching the imaging-layer convention

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 40, 200)).save(buffer,
                                                           format="PNG")
    return buffer.getvalue()


def spy_downscale(prelabel, monkeypatch):
    """Wrap the chokepoint's `downscale_image` binding with a recorder
    that delegates to the real Image_Downscaler, so tests can count and
    attribute calls without changing behavior."""
    real = prelabel.downscale_image
    calls = []

    def recording(image_bytes, image_format, downscale_setting, **kwargs):
        calls.append({
            "bytes": image_bytes,
            "format": image_format,
            "setting": downscale_setting,
            "source_dimensions": kwargs.get("source_dimensions"),
        })
        return real(image_bytes, image_format, downscale_setting, **kwargs)

    monkeypatch.setattr(prelabel, "downscale_image", recording)
    return calls


class TestDownscaleInvocation:
    """Req 6.1, 8.1: the Image_Downscaler runs exactly once per image —
    the target and every attached example — and never at Downscale_Off."""

    def test_called_once_for_the_target_and_once_per_attached_example(
            self, prelabel, monkeypatch):
        calls = spy_downscale(prelabel, monkeypatch)
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)
        # Every image fits the bound, so all bytes pass through
        # unmodified; distinct dimensions make each call attributable.
        # The target's dimensions ride along as arguments, so header-only
        # bytes suffice for it; the examples carry no dimensions, so the
        # downscaler reads their headers and needs real PNGs.
        target = png_bytes(WIDTH, HEIGHT)
        good = real_png_bytes(60, 40)
        bad = real_png_bytes(50, 30)

        invoke(prelabel, image_bytes=target, downscale_setting=512,
               few_shot_images=[
                   {"bytes": bad, "format": "png", "designation": "bad"},
                   {"bytes": good, "format": "png", "designation": "good"},
               ])

        # Exactly one call per image: the target first, then every
        # attached example in attachment order (good before bad), all
        # with the target's setting (Req 8.1).
        assert [call["bytes"] for call in calls] == [target, good, bad]
        assert all(call["setting"] == 512 for call in calls)
        # The target's already-parsed Source_Dimensions ride along so
        # its header is never parsed a second time (Req 7.6).
        assert calls[0]["source_dimensions"] == (WIDTH, HEIGHT)
        assert len(client.calls) == 1

    def test_not_called_at_downscale_off(self, prelabel, monkeypatch):
        """Downscale_Off short-circuits the downscaler entirely, even
        with examples attached (Req 6.1, 10.1)."""
        calls = spy_downscale(prelabel, monkeypatch)
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        invoke(prelabel, downscale_setting=None, few_shot_images=[
            {"bytes": png_bytes(60, 40), "format": "png",
             "designation": "good"},
        ])

        assert calls == []
        assert len(client.calls) == 1

    @pytest.mark.parametrize("setting", [True, "1024", 999])
    def test_malformed_setting_degrades_to_downscale_off(
            self, prelabel, monkeypatch, setting):
        """A setting outside the seven permitted values normalizes to
        Downscale_Off at the chokepoint: nothing is downscaled and
        nothing fails (Req 5.9, 5.12)."""
        calls = spy_downscale(prelabel, monkeypatch)
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        result = invoke(prelabel, downscale_setting=setting)

        assert calls == []
        assert len(client.calls) == 1
        assert (result.sent_width, result.sent_height) == (WIDTH, HEIGHT)


class TestSentDimensions:
    """Req 7.1: the Detection_Prompt describes the image actually sent,
    and the result reports its dimensions."""

    def test_prompt_carries_the_sent_dimensions_after_a_downscale(
            self, prelabel, monkeypatch):
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)
        source = real_png_bytes(SOURCE_W, SOURCE_H)

        result = invoke(prelabel, image_bytes=source, width=SOURCE_W,
                        height=SOURCE_H, downscale_setting=SENT_W)

        content = client.calls[0]["messages"][0]["content"]
        prompt_text = content[-1]["text"]
        assert (f"The image is {SENT_W} pixels wide and {SENT_H} pixels "
                f"tall" in prompt_text)
        assert f"The image is {SOURCE_W} pixels wide" not in prompt_text
        assert (result.sent_width, result.sent_height) == (SENT_W, SENT_H)
        # The image block holds the Downscaled_Image, not the source —
        # and its pixels agree with the prompt.
        sent_bytes = content[0]["image"]["source"]["bytes"]
        assert sent_bytes != source
        from PIL import Image
        with Image.open(io.BytesIO(sent_bytes)) as sent_image:
            assert sent_image.size == (SENT_W, SENT_H)

    def test_prompt_carries_the_source_dimensions_at_downscale_off(
            self, prelabel, monkeypatch):
        """At Downscale_Off the Sent_Dimensions are the
        Source_Dimensions, and the very bytes object passed in is what
        is sent (Req 10.1)."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)
        source = png_bytes(WIDTH, HEIGHT)

        result = invoke(prelabel, image_bytes=source)

        content = client.calls[0]["messages"][0]["content"]
        assert (f"The image is {WIDTH} pixels wide and {HEIGHT} pixels "
                f"tall" in content[-1]["text"])
        assert content[0]["image"]["source"]["bytes"] is source
        assert (result.sent_width, result.sent_height) == (WIDTH, HEIGHT)


class TestCoordinateSpaces:
    """Req 7.2: `parse_guidance` validates in Sent space,
    `guidance_to_prelabel` converts in Source space, and the geometry
    is scaled back between the two."""

    def test_parse_receives_sent_and_convert_receives_source(
            self, prelabel, monkeypatch):
        recorded = {}
        real_parse = prelabel.parse_guidance
        real_convert = prelabel.guidance_to_prelabel

        def parse_spy(text, label_set, width, height):
            recorded["parse"] = (width, height)
            return real_parse(text, label_set, width, height)

        def convert_spy(detections, modality, label_set, width, height):
            recorded["convert"] = (width, height)
            return real_convert(detections, modality, label_set, width,
                                height)

        monkeypatch.setattr(prelabel, "parse_guidance", parse_spy)
        monkeypatch.setattr(prelabel, "guidance_to_prelabel", convert_spy)
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        result = invoke(prelabel,
                        image_bytes=real_png_bytes(SOURCE_W, SOURCE_H),
                        width=SOURCE_W, height=SOURCE_H,
                        downscale_setting=SENT_W)

        assert recorded["parse"] == (SENT_W, SENT_H)
        assert recorded["convert"] == (SOURCE_W, SOURCE_H)
        # The sent-space box (10, 5, 30, 20) reaches the Pre_Label
        # mapped through the exact 2x factor into Source space.
        box = result.prelabel["boxes"][0]
        assert (box["left"], box["top"],
                box["width"], box["height"]) == (20, 10, 60, 40)
        assert result.prelabel["image_width"] == SOURCE_W
        assert result.prelabel["image_height"] == SOURCE_H

    def test_guidance_is_validated_against_the_sent_bounds(
            self, prelabel, monkeypatch):
        """A coordinate that fits the Source_Dimensions but lies outside
        the Sent_Dimensions is rejected: validation runs in the space
        the model saw, after exactly one invocation."""
        raw = guidance([{"class": "scratch",
                         "box": {"left": 600, "top": 5,
                                 "width": 30, "height": 20}}])
        client = RecordingConverseClient(reply=raw)
        bind_client(prelabel, monkeypatch, client)

        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel,
                   image_bytes=real_png_bytes(SOURCE_W, SOURCE_H),
                   width=SOURCE_W, height=SOURCE_H,
                   downscale_setting=SENT_W)

        failure = excinfo.value
        assert failure.category == prelabel.CATEGORY_UNUSABLE_MODEL_OUTPUT
        assert f"{SENT_W}x{SENT_H}" in failure.reason
        assert failure.raw_text == raw
        assert len(client.calls) == 1


class TestInferenceConfigHandling:
    """Req 1.3, 10.2: `maxTokens` is replaced on a copy of
    `build_inference_config`'s result; the returned dict itself is never
    mutated and the sampling parameters pass through unchanged."""

    def _capture_build(self, prelabel, monkeypatch):
        real_build = prelabel.build_inference_config
        captured = {}

        def capturing(config):
            built = real_build(config)
            captured["built"] = built
            captured["snapshot"] = dict(built)
            return built

        monkeypatch.setattr(prelabel, "build_inference_config", capturing)
        return captured

    def test_build_inference_config_result_is_not_mutated_in_place(
            self, prelabel, monkeypatch):
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client, temperature=0.5)
        captured = self._capture_build(prelabel, monkeypatch)

        invoke(prelabel, token_budget_selection=20000)

        # The dict build_inference_config returned is exactly as it
        # returned it: maxTokens still the Global_Max_Tokens, sampling
        # untouched.
        assert captured["built"] == captured["snapshot"]
        assert captured["built"]["maxTokens"] == GLOBAL_MAX_TOKENS
        # What was sent is a different dict with only maxTokens replaced.
        sent = client.calls[0]["inferenceConfig"]
        assert sent is not captured["built"]
        assert sent["maxTokens"] == 20000
        assert sent["temperature"] == 0.5
        assert set(sent) == {"maxTokens", "temperature"}

    @pytest.mark.parametrize("temperature,top_p,expected_keys", [
        (0.5, None, {"maxTokens", "temperature"}),
        (None, 0.9, {"maxTokens", "topP"}),
        (None, None, {"maxTokens"}),
    ])
    def test_sampling_parameters_pass_through_unchanged(
            self, prelabel, monkeypatch, temperature, top_p, expected_keys):
        """Req 10.2: the temperature/topP exclusivity rule is untouched
        by the maxTokens override."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client, temperature=temperature,
                    top_p=top_p)

        invoke(prelabel, token_budget_selection=20000)

        sent = client.calls[0]["inferenceConfig"]
        assert set(sent) == expected_keys
        assert sent["maxTokens"] == 20000
        if temperature is not None:
            assert sent["temperature"] == temperature
        elif top_p is not None:
            assert sent["topP"] == top_p


class TestTokenBudgetOverride:
    """Req 1.3: the request's maxTokens is the Token_Budget_Resolver's
    output — selection, then mapping, then default — and never the
    Global_Max_Tokens."""

    @pytest.mark.parametrize("selection,limits,expected", [
        # A valid Token_Budget_Selection wins over the mapping.
        (20000, {MODEL_ID: 5000}, 20000),
        # No selection: the Model_Token_Limits entry wins.
        (None, {MODEL_ID: 5000}, 5000),
        # Invalid selections fall through to the mapping — a digit-only
        # string and a bool are not integers to the resolver.
        ("20000", {MODEL_ID: 5000}, 5000),
        (True, {MODEL_ID: 5000}, 5000),
        # No usable selection and no usable entry: the 10000 default.
        (None, {"someone-else": 5000}, 10000),
        (None, None, 10000),
    ])
    def test_budget_tiers_reach_the_converse_request(
            self, prelabel, monkeypatch, selection, limits, expected):
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        invoke(prelabel, token_budget_selection=selection,
               model_token_limits=limits)

        assert len(client.calls) == 1
        sent = client.calls[0]["inferenceConfig"]["maxTokens"]
        assert sent == expected
        assert sent != GLOBAL_MAX_TOKENS


class TestDownscaleRefusals:
    """Req 9.1: an Image_Downscaler refusal is a categorized failure
    carrying the design's error-table reason, with zero invocations."""

    def test_refused_target_is_unsupported_image_content(self, prelabel,
                                                         monkeypatch):
        """A target that must be resized but cannot be decoded fails as
        `unsupported image content: {key} could not be resized to a
        longer edge of {n} pixels: {cause}` before any invocation."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)
        # Header-only bytes declaring 5000x4000: the resize path must
        # decode pixels, and there are none to decode.
        source = png_bytes(5000, 4000)

        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel, image_bytes=source, width=5000, height=4000,
                   downscale_setting=1024)

        failure = excinfo.value
        assert (failure.category == prelabel.CATEGORY_UNSUPPORTED_IMAGE
                == "unsupported_image_content")
        assert failure.reason.startswith(
            "unsupported image content: imgs/a.png could not be resized "
            "to a longer edge of 1024 pixels: ")
        assert failure.raw_text is None
        assert client.calls == []

    def test_oversize_target_reason_names_the_declared_pixel_count(
            self, prelabel, monkeypatch):
        """The Max_Source_Pixel_Count refusal reason is the error
        table's, character-for-character."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)
        source = png_bytes(20000, 20000)

        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel, image_bytes=source, width=20000, height=20000,
                   downscale_setting=512)

        failure = excinfo.value
        assert failure.category == "unsupported_image_content"
        assert failure.reason == (
            "unsupported image content: imgs/a.png declares 20000x20000 "
            "= 400000000 pixels, above the 100000000 pixel limit")
        assert client.calls == []

    def test_refused_example_is_unreadable_example_image(self, prelabel,
                                                         monkeypatch):
        """An attached example that cannot be decoded fails as
        `few-shot example image {ref} could not be resized to a longer
        edge of {n} pixels: {cause}`, still with zero invocations."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)
        ref = "s3://bucket/labeling-examples/job/good/0-a.jpg"

        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel, downscale_setting=512, few_shot_images=[
                {"bytes": b"not an image at all", "format": "jpeg",
                 "designation": "good", "ref": ref},
            ])

        failure = excinfo.value
        assert (failure.category == prelabel.CATEGORY_UNREADABLE_EXAMPLE
                == "unreadable_example_image")
        assert failure.reason.startswith(
            f"few-shot example image {ref} could not be resized to a "
            f"longer edge of 512 pixels: ")
        assert failure.raw_text is None
        assert client.calls == []

    def test_refused_example_without_ref_is_named_by_position(
            self, prelabel, monkeypatch):
        """An example the caller passed without a stored reference is
        still identified in the reason, by attachment position."""
        client = RecordingConverseClient(reply=guidance([BOX]))
        bind_client(prelabel, monkeypatch, client)

        with pytest.raises(prelabel.LlmPrelabelError) as excinfo:
            invoke(prelabel, downscale_setting=512, few_shot_images=[
                {"bytes": b"junk", "format": "png", "designation": "bad"},
            ])

        assert excinfo.value.reason.startswith(
            "few-shot example image at position 1 could not be resized "
            "to a longer edge of 512 pixels: ")
        assert client.calls == []
