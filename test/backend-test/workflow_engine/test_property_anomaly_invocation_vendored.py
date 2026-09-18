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
"""Device half of **Property 6** (spec task 3.4).

**Feature: quality-prompt-tuning, Property 6: Executor, Bedrock_Scorer
and Device_Score_Job build identical invocations** — the device's half:
the VENDORED ``workflow_core.anomaly_invocation`` is driven through the
two device call paths, so what the executor sends at run time and what
the Device_Score_Job runner sends on replay are the *same request*.

*For any* Node_Parameters, Prompt_Set and input/reference bytes:

- ``llm_inference``: the Text_Generation_API body POSTed by the executor
  (through its own default transport), the body POSTed by the job
  runner's replay, and ``build_llm_invocation(...).request_body()``
  from the vendored module are equal field for field **and in key
  order**, at the same URL;
- ``bedrock_inference``: the ``client.converse(**kwargs)`` keyword
  arguments the executor's transport sends equal
  ``build_bedrock_invocation(...).converse_kwargs()``;
- ``parse_verdict`` yields equal results for the same answer text on
  every path (and rejects the same answers).

The Portal's own path is the same module: the Bedrock_Scorer and the
shared-module comparison live in
``edge-cv-portal/backend/tests/test_property_anomaly_invocation.py``
(spec task 1.3), and the vendored copy is asserted byte-identical to the
Portal layer's copy below, so "identical invocations" holds across all
three call paths by construction.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

Nothing here talks to a model: ``requests.post`` is replaced for the
duration of a call and ``boto3``/``botocore.config`` are replaced by
stubs in ``sys.modules`` (neither client is ever constructed against
AWS).
"""
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
from contextlib import contextmanager

import cv2
import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import workflow_engine_test_utils  # noqa: F401 - sets COMPONENT_WORK_PATH

from workflow_engine import output_bindings
from workflow_engine.output_bindings import (
    BedrockInferenceError,
    BedrockInferenceProcessor,
    LlmInferenceProcessor,
    _downscale_frame_or_original,
)
from workflow_engine.tuning.job_runner import (
    JobRunner,
    manifest_key,
    outcomes_key,
)
from workflow_engine.tuning.sample_export import ExportConfig
from workflow_engine.vendor.workflow_core import anomaly_invocation as ai

BUCKET = "dda-inference-results-000000000000"
PREFIX = "workflow-tuning/samples/"
THING_NAME = "dda-edge-under-test"
WORKFLOW_ID = "wf-24680"
SESSION_ID = "sess-1a2b"
RUN_ID = "run-9f8e"
JOB_ID = "job-5c4d"
SAMPLE_ID = "sample-000"
INPUT_KEY = PREFIX + "wf/node/dev/sample-000.input.jpg"
REFERENCE_KEY = PREFIX + "wf/node/dev/sample-000.reference.jpg"

PART_ID = "part-7"
TEMPLATE_WITH_PLACEHOLDER = "Inspect {part_id} against the reference."
TEMPLATE_PLAIN = "Inspect the part against the reference."

VERDICT_TRUE = '{"is_anomalous": true, "confidence": 0.91}'
VERDICT_FALSE = '{"is_anomalous": false, "confidence": 0.12}'
FENCED = "```json\n" + VERDICT_TRUE + "\n```"
PROSE = "Here is my answer.\n" + VERDICT_FALSE + "\nHope that helps."
UNPARSEABLE = "the plate looks fine to me"

ANSWERS = ("verdict_true", "verdict_false", "fenced", "prose", "unparseable")
ANSWER_TEXT = {
    "verdict_true": VERDICT_TRUE,
    "verdict_false": VERDICT_FALSE,
    "fenced": FENCED,
    "prose": PROSE,
    "unparseable": UNPARSEABLE,
}


# ---------------------------------------------------------------------------
# Image pool (fixed content: the property is about composition, not JPEG
# encoder output)
# ---------------------------------------------------------------------------

def _jpeg(width, height, tint):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = np.uint8(tint)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


IMAGES = {
    "jpeg_large": _jpeg(320, 240, 40),
    "jpeg_small": _jpeg(24, 18, 90),
    # Bytes Pillow cannot decode: the executor's downscaler contains the
    # failure and sends the original, and so must the replay.
    "opaque": b"\xff\xd8not-a-decodable-jpeg\xff\xd9",
}
IMAGE_NAMES = sorted(IMAGES)


# ---------------------------------------------------------------------------
# Transport interception
# ---------------------------------------------------------------------------

class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@contextmanager
def intercepted_text_generation(answer):
    """Replace ``requests.post`` and collect ``(url, body)`` per call."""
    import requests

    posted = []
    original = requests.post

    def fake_post(url, json=None, timeout=None):
        posted.append({"url": url, "body": json, "timeout": timeout})
        return _Response({"generated_text": answer})

    requests.post = fake_post
    try:
        yield posted
    finally:
        requests.post = original


class _StubConverseClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.answer}]}}}


@contextmanager
def intercepted_converse(answer):
    """Replace ``boto3``/``botocore.config`` with stubs and collect the
    ``converse`` keyword arguments and the client configuration."""
    client = _StubConverseClient(answer)
    captured = {}

    boto3 = types.ModuleType("boto3")

    def _client(service_name, region_name=None, config=None):
        captured.update(service_name=service_name, region_name=region_name,
                        config=config)
        return client

    boto3.client = _client

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    botocore_config.Config = Config
    botocore.config = botocore_config

    previous = {name: sys.modules.get(name)
                for name in ("boto3", "botocore", "botocore.config")}
    sys.modules["boto3"] = boto3
    sys.modules["botocore"] = botocore
    sys.modules["botocore.config"] = botocore_config
    try:
        yield client, captured
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


# ---------------------------------------------------------------------------
# The job runner's S3, shadow and session fakes
# ---------------------------------------------------------------------------

class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey: " + str(Key))
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.objects[Key] = Body
        return {}


class _EmptyRegistrations:
    """No registration on this device, so the manifest's Node_Parameters
    are used exactly as the Portal wrote them."""

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
# Drawn Node_Parameters / Prompt_Set
# ---------------------------------------------------------------------------

MAX_TOKENS_VALUES = (None, 0, 1, 64, 192, 4096, "abc", 2.0, True)
MAX_DIM_VALUES = (None, 16, 32, 4096, -5, "wide")
SYSTEM_VALUES = (None, "", "   ", "You are a QA inspector.", "  padded  ")
TEMPERATURES = (None, 0.0, 0.7)
TOP_PS = (None, 0.9)
MODELS = ("qwen2-vl-2b", "us.amazon.nova-lite-v1:0")
REGIONS = (None, "", "eu-west-1")


@given(
    node_type=st.sampled_from(("bedrock", "llm")),
    prompt=st.sampled_from(("", "Inspect the part.", TEMPLATE_PLAIN)),
    placeholder=st.booleans(),
    system_prompt=st.sampled_from(SYSTEM_VALUES),
    max_tokens=st.sampled_from(MAX_TOKENS_VALUES),
    max_dim=st.sampled_from(MAX_DIM_VALUES),
    temperature=st.sampled_from(TEMPERATURES),
    top_p=st.sampled_from(TOP_PS),
    model=st.sampled_from(MODELS),
    region=st.sampled_from(REGIONS),
    image=st.sampled_from(IMAGE_NAMES),
    reference=st.sampled_from((None,) + tuple(IMAGE_NAMES)),
    answer=st.sampled_from(ANSWERS),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_property_the_vendored_builder_is_the_one_every_path_uses(
        node_type, prompt, placeholder, system_prompt, max_tokens, max_dim,
        temperature, top_p, model, region, image, reference, answer):
    """**Feature: quality-prompt-tuning, Property 6: Executor,
    Bedrock_Scorer and Device_Score_Job build identical invocations**
    (device half).

    For any Node_Parameters, Prompt_Set and input/reference bytes, the
    request the executor sends at run time, the request the
    Device_Score_Job runner sends on replay and the request the vendored
    Invocation_Builder produces are equal — model, full prompt including
    the Verdict_Instruction, image labels/order/bytes (or base64),
    region, max_tokens, system text and generation parameters — and
    ``parse_verdict`` yields equal results for the same answer text on
    every path.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.4**"""
    answer_text = ANSWER_TEXT[answer]
    input_bytes = IMAGES[image]
    reference_bytes = IMAGES[reference] if reference is not None else None
    if node_type == "bedrock":
        _assert_bedrock_paths_agree(
            prompt, system_prompt, max_tokens, model, region,
            input_bytes, reference_bytes, answer, answer_text)
    else:
        _assert_llm_paths_agree(
            prompt, placeholder, system_prompt, max_tokens, max_dim,
            temperature, top_p, model, input_bytes, reference_bytes,
            answer, answer_text)


# ---------------------------------------------------------------------------
# bedrock_inference: the executor's transport vs the vendored builder
# ---------------------------------------------------------------------------

def _assert_bedrock_paths_agree(prompt, system_prompt, max_tokens, model,
                               region, input_bytes, reference_bytes, answer,
                               answer_text):
    node_id = "bedrock_1"
    parameters = {"prompt": prompt, "model": model, "max_tokens": max_tokens}
    if region is not None:
        parameters["region"] = region
    if system_prompt is not None:
        parameters["system_prompt"] = system_prompt
    parameters["anomaly_mode"] = True

    # -- the vendored builder, directly --------------------------------
    rejection = None
    expected = None
    try:
        expected = ai.build_bedrock_invocation(
            dict(parameters), input_bytes, reference_bytes)
    except Exception as error:  # noqa: BLE001 - a rejected configuration
        rejection = error

    root = tempfile.mkdtemp(prefix="dda-tuning-vendored-")
    try:
        with open(os.path.join(root, "in.jpg"), "wb") as handle:
            handle.write(input_bytes)
        capture_paths = {"in": "{work_dir}/in.jpg", "reference": None}
        if reference_bytes is not None:
            with open(os.path.join(root, "ref.jpg"), "wb") as handle:
                handle.write(reference_bytes)
            capture_paths["reference"] = "{work_dir}/ref.jpg"
        document = {"schemaVersion": 1, "executorBindings": [{
            "nodeId": node_id,
            "binding": "bedrock_inference",
            "parameters": parameters,
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
            "capturePaths": capture_paths,
        }]}
        # The executor's OWN transport (no injected invoker), so the real
        # Converse request is observed.
        with intercepted_converse(answer_text) as (client, captured):
            processor = BedrockInferenceProcessor()
            raised = None
            metadata = None
            try:
                metadata = processor.process(document, {}, root)
            except BedrockInferenceError as error:
                raised = str(error)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if rejection is not None:
        # A configuration the shared builder rejects is rejected by the
        # executor too, before any request is issued — the same failure
        # on every path (Property 6's "identical invocations" includes
        # identical refusals).
        assert client.calls == []
        assert raised is not None
        assert str(rejection) in raised
        return

    assert len(client.calls) == 1
    sent = client.calls[0]
    # Field for field, and in key order: the executor sends exactly what
    # the vendored builder produced.
    assert sent == expected.converse_kwargs()
    assert json.dumps(sent, default=repr) == json.dumps(
        expected.converse_kwargs(), default=repr)
    # ... and the request carries the drawn content where the design says.
    content = sent["messages"][0]["content"]
    assert content[0]["text"] == expected.prompt
    assert content[0]["text"].endswith(ai.BEDROCK_JSON_INSTRUCTION)
    labels = [block["text"] for block in content if "text" in block][1:]
    expected_labels = ["Input image:"]
    if reference_bytes is not None:
        expected_labels.append("Reference image:")
    assert labels == expected_labels
    images = [block["image"]["source"]["bytes"]
              for block in content if "image" in block]
    assert images == [data for _label, data in expected.images]
    assert images[0] == input_bytes
    assert sent["inferenceConfig"] == {"maxTokens": expected.max_tokens}
    assert ("system" in sent) is bool(expected.system_prompt)
    assert captured["region_name"] == expected.region
    assert captured["config"].kwargs == {
        "read_timeout": ai.BEDROCK_READ_TIMEOUT_SEC,
        "retries": {"max_attempts": 1},
    }

    # -- the parser agrees on the same answer text ---------------------
    _assert_parse_agrees(
        answer, answer_text,
        recorded=(None if metadata is None else
                  {"is_anomalous": metadata.get("is_anomalous"),
                   "confidence": metadata.get("confidence")}),
        rejected_message=raised)


# ---------------------------------------------------------------------------
# llm_inference: the executor's transport vs the job runner vs the builder
# ---------------------------------------------------------------------------

def _assert_llm_paths_agree(prompt, placeholder, system_prompt, max_tokens,
                            max_dim, temperature, top_p, model, input_bytes,
                            reference_bytes, answer, answer_text):
    node_id = "llm_1"
    template = TEMPLATE_WITH_PLACEHOLDER if placeholder else prompt
    rendered = (
        template.replace("{part_id}", PART_ID) if placeholder else template)
    node_parameters = {"modelName": model, "max_image_dimension": max_dim}
    if temperature is not None:
        node_parameters["temperature"] = temperature
    if top_p is not None:
        node_parameters["top_p"] = top_p
    prompt_set = {"prompt_template": template, "max_tokens": max_tokens}
    if system_prompt is not None:
        prompt_set["system_prompt"] = system_prompt
    parameters = dict(node_parameters)
    parameters.update(prompt_set)
    parameters["anomaly_mode"] = True

    # -- the vendored builder, directly (with the executor's own
    #    contained Pillow downscaler injected) --------------------------
    expected = ai.build_llm_invocation(
        dict(parameters), rendered, input_bytes, reference_bytes,
        downscaler=(
            lambda data, dimension, port: _downscale_frame_or_original(
                data, dimension, node_id, port)),
    )
    expected_body = expected.request_body()

    # -- the executor's path -------------------------------------------
    root = tempfile.mkdtemp(prefix="dda-tuning-vendored-")
    try:
        with open(os.path.join(root, "in.jpg"), "wb") as handle:
            handle.write(input_bytes)
        capture_paths = {"in": "{work_dir}/in.jpg", "reference": None}
        if reference_bytes is not None:
            with open(os.path.join(root, "ref.jpg"), "wb") as handle:
                handle.write(reference_bytes)
            capture_paths["reference"] = "{work_dir}/ref.jpg"
        document = {"schemaVersion": 1, "executorBindings": [{
            "nodeId": node_id,
            "binding": "llm_inference",
            "parameters": dict(parameters),
            "upstreamNodeIds": ["cam"],
            "downstreamNodeIds": ["mqtt"],
            "capturePaths": capture_paths,
        }]}
        with intercepted_text_generation(answer_text) as posted:
            # The executor's OWN transport (no injected invoker).
            executor_metadata = LlmInferenceProcessor().process(
                document, {"part_id": PART_ID}, root)
        assert len(posted) == 1, posted
        executor_request = posted[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # -- the Device_Score_Job runner's path ----------------------------
    objects = {INPUT_KEY: input_bytes}
    manifest_sample = {
        "sampleId": SAMPLE_ID,
        "inputKey": INPUT_KEY,
        "label": "NOK",
        "metadataSnippet": {"part_id": PART_ID} if placeholder else {},
    }
    if reference_bytes is not None:
        objects[REFERENCE_KEY] = reference_bytes
        manifest_sample["referenceKey"] = REFERENCE_KEY
    manifest = {
        "jobId": JOB_ID,
        "sessionId": SESSION_ID,
        "runId": RUN_ID,
        "workflowId": WORKFLOW_ID,
        "nodeId": node_id,
        # The Portal writes the Node_Parameters and the Candidate's
        # Prompt_Set separately; the runner layers them.
        "nodeParameters": dict(node_parameters),
        "promptSet": dict(prompt_set),
        "repeats": 1,
        "samples": [manifest_sample],
    }
    objects[manifest_key(PREFIX, JOB_ID)] = json.dumps(manifest).encode(
        "utf-8")
    s3 = FakeS3(objects)
    runner = JobRunner(
        ExportConfig(bucket=BUCKET, prefix=PREFIX),
        shadow_accessor=None, s3_factory=lambda: s3, thing_name=THING_NAME,
        session_factory=empty_session_factory, backoff_base_seconds=0.0,
        sleep=lambda _seconds: None)
    with intercepted_text_generation(answer_text) as posted:
        runner.run_job(JOB_ID, {"manifestKey": manifest_key(PREFIX, JOB_ID)})
    assert len(posted) == 1, posted
    runner_request = posted[0]

    # -- the three requests are the same -------------------------------
    assert executor_request["body"] == expected_body
    assert runner_request["body"] == expected_body
    # Key order is part of the request.
    canonical = json.dumps(expected_body)
    assert json.dumps(executor_request["body"]) == canonical
    assert json.dumps(runner_request["body"]) == canonical
    assert executor_request["url"] == runner_request["url"]
    assert executor_request["url"] == output_bindings.TEXT_GENERATION_URL\
        .format(model_name=model)
    assert executor_request["timeout"] == runner_request["timeout"]

    # -- and they carry what the design says ---------------------------
    body = expected_body
    assert body["prompt"] == rendered + "\n\n" + ai.BEDROCK_JSON_INSTRUCTION
    assert body["max_tokens"] == ai.resolve_output_token_budget(
        max_tokens)[0]
    dimension, _notice = ai.resolve_max_image_dimension(max_dim)
    assert body["image"] == base64.b64encode(
        _bytes_as_sent(input_bytes, dimension)).decode("ascii")
    if reference_bytes is None:
        assert "reference_image" not in body
    else:
        assert body["reference_image"] == base64.b64encode(
            _bytes_as_sent(reference_bytes, dimension)).decode("ascii")
    assert ("system_prompt" in body) is bool(
        ai.normalize_system_prompt(system_prompt))

    # -- the parser agrees on the same answer text ---------------------
    outcome_batch = json.loads(
        s3.objects[outcomes_key(PREFIX, SESSION_ID, RUN_ID, 1)].decode(
            "utf-8"))
    outcome = outcome_batch["outcomes"][0]
    node_outcome = executor_metadata["llm"][node_id]
    if answer == "unparseable":
        assert outcome["category"] == "parse_failure"
        assert outcome["parseError"]
        assert node_outcome["error"] == outcome["parseError"]
        _assert_parse_agrees(answer, answer_text, recorded=None,
                             rejected_message=node_outcome["error"])
    else:
        assert {"is_anomalous": outcome["isAnomalous"],
                "confidence": outcome["confidence"]} == {
            "is_anomalous": node_outcome["is_anomalous"],
            "confidence": node_outcome["confidence"]}
        _assert_parse_agrees(
            answer, answer_text,
            recorded={"is_anomalous": node_outcome["is_anomalous"],
                      "confidence": node_outcome["confidence"]},
            rejected_message=None)


def _bytes_as_sent(data, dimension):
    """The bytes a configured ``max_image_dimension`` sends, restated.

    Downscaled when the dimension is configured and the frame decodes;
    the ORIGINAL bytes when it does not — the executor's contained
    helper logs and falls back, and a replay must fall back identically.
    """
    if dimension is None:
        return data
    try:
        return output_bindings.downscale_image_bytes(data, dimension)
    except Exception:  # noqa: BLE001 - the containment rule under test
        return data


def _assert_parse_agrees(answer, answer_text, recorded, rejected_message):
    """The vendored Verdict_Parser and the path's recorded verdict agree,
    and an unparseable answer is rejected on every path."""
    if answer == "unparseable":
        assert rejected_message, (
            "an unparseable answer must be surfaced, not recorded")
        with pytest.raises(ValueError) as excinfo:
            ai.parse_verdict(answer_text)
        assert answer_text[:40] in str(excinfo.value)
        assert str(excinfo.value) in rejected_message
        return
    verdict = ai.parse_verdict(answer_text)
    assert recorded == {"is_anomalous": verdict["is_anomalous"],
                        "confidence": verdict["confidence"]}


# ---------------------------------------------------------------------------
# Supporting check: the device's copy IS the Portal's copy
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
PORTAL_MODULE = os.path.join(
    REPO_ROOT, "edge-cv-portal", "backend", "layers", "workflow_core",
    "python", "workflow_core", "anomaly_invocation.py")


def test_the_vendored_module_is_the_portal_layers_module():
    """Property 6 claims one implementation across three call paths; on
    the device that holds because ``re_vendor.sh`` mirrors the Portal
    layer's file byte-identically (the drift guard in
    ``test_vendored_catalog_mirror.py`` owns enforcement — this is the
    fact Property 6 rests on)."""
    if not os.path.isfile(PORTAL_MODULE):
        pytest.skip("the Portal layer source is not present in this tree")
    with open(PORTAL_MODULE, "rb") as handle:
        portal = handle.read()
    with open(ai.__file__.replace(".pyc", ".py"), "rb") as handle:
        vendored = handle.read()
    assert hashlib.sha256(vendored).hexdigest() == hashlib.sha256(
        portal).hexdigest()
