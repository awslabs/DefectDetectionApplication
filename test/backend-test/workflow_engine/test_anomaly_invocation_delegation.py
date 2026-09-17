# Copyright 2025 Amazon Web Services, Inc.
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
"""quality-prompt-tuning task 1.6: the executor's delegation to the
shared Invocation_Builder.

Unit coverage of the device half of the extraction (design → Testing
Strategy → Unit tests → **Device**: "processors delegate and preserve
invoker arities"):

- ``output_bindings`` re-exports resolve to the vendored
  ``workflow_core.anomaly_invocation`` objects themselves (same function
  objects, same constants), so there is no second copy of the rules;
- ``BedrockInferenceProcessor._run_one`` builds through
  ``build_bedrock_invocation`` — the resolved image bytes and the node's
  parameters go in, the invocation's fields come out as the invoker's
  positional arguments — and parses through the shared Verdict_Parser;
- ``LlmInferenceProcessor._run_one`` builds through
  ``build_llm_invocation`` with the executor's Pillow downscaler
  injected, and the rendered prompt / frames / resolved
  Output_Token_Budget reach the invoker unchanged;
- both processors preserve their **pre-feature invoker arities**: the
  Bedrock 5-positional (6 with a system prompt) form and the LLM
  3-/4-/5-positional forms with ``system_prompt`` / ``metrics_sink``
  keywords supplied only when configured/accepted, so injected fakes
  that predate those parameters keep working;
- the two default transports send exactly ``converse_kwargs()`` /
  ``request_body()``.

**Validates: Requirements 6.2, 11.1**

Harness follows the suite's existing processor tests
(``test_bedrock_response_mode.py``, ``test_llm_reference_attachment.py``):
injectable recording invokers, a minimal compiled document, temp work
dirs. ``boto3`` is not installed in the device test venv, so the Bedrock
transport test injects a stub module the way the security-preservation
suite does; the LLM transport test patches ``requests.post``.
"""
import base64
import copy
import json
import sys
import types

import pytest

from workflow_engine import output_bindings
from workflow_engine.output_bindings import (
    BedrockInferenceProcessor,
    BedrockInvocation,
    LlmInferenceProcessor,
    LlmInvocation,
    _default_bedrock_invoker,
    _default_llm_invoker,
)
from workflow_engine.vendor.workflow_core import anomaly_invocation as ai

INPUT_JPEG = b"\xff\xd8\xff\xe0input-frame-bytes\xff\xd9"
REFERENCE_JPEG = b"\xff\xd8\xff\xe0reference-frame-bytes\xff\xd9"
VERDICT_ANSWER = '{"is_anomalous": true, "confidence": 0.9}'


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

#: Sentinel for "the keyword was not supplied at all", distinct from an
#: explicitly passed ``None``.
_ABSENT = object()


class ArityRecordingInvoker:
    """Records every call's raw positional args and the keywords it
    declares, so the arity and the keyword gating are observable.

    It declares ``system_prompt`` but deliberately NOT ``metrics_sink``,
    and takes no ``**kwargs`` — the shape of an invoker that predates the
    metrics sink, which the executor's ``_accepts_keyword`` gate must
    therefore never receive it.
    """

    def __init__(self, answer=VERDICT_ANSWER):
        self.answer = answer
        self.calls = []

    def __call__(self, *args, system_prompt=_ABSENT):
        keywords = {}
        if system_prompt is not _ABSENT:
            keywords["system_prompt"] = system_prompt
        self.calls.append((args, keywords))
        return self.answer

    @property
    def args(self):
        assert len(self.calls) == 1, self.calls
        return self.calls[0][0]

    @property
    def kwargs(self):
        assert len(self.calls) == 1, self.calls
        return self.calls[0][1]


class FixedArityBedrockInvoker:
    """Pre-feature Bedrock invoker: exactly the five shipped positional
    parameters, no ``system_prompt``. Calling it with more arguments is a
    ``TypeError`` — which is the point."""

    def __init__(self, answer=VERDICT_ANSWER):
        self.answer = answer
        self.calls = []

    def __call__(self, model, prompt, images, region, max_tokens):
        self.calls.append((model, prompt, list(images), region, max_tokens))
        return self.answer


class FixedArityLlmInvoker:
    """Pre-feature Text_Generation_API invoker: the three shipped
    positional parameters, no image, no ``system_prompt``, no
    ``metrics_sink``."""

    def __init__(self, text=VERDICT_ANSWER):
        self.text = text
        self.calls = []

    def __call__(self, model_name, prompt, parameters):
        self.calls.append((model_name, prompt, dict(parameters)))
        return self.text


class MetricsAwareLlmInvoker(ArityRecordingInvoker):
    """An invoker that declares ``metrics_sink`` (the post-feature
    shape), so the executor's ``_accepts_keyword`` gate forwards it."""

    def __call__(self, *args, system_prompt=_ABSENT, metrics_sink=_ABSENT):
        keywords = {}
        if system_prompt is not _ABSENT:
            keywords["system_prompt"] = system_prompt
        if metrics_sink is not _ABSENT:
            keywords["metrics_sink"] = metrics_sink
        self.calls.append((args, keywords))
        return self.answer


def bedrock_binding(node_id="bedrock1", capture_paths=None, **params):
    parameters = {
        "model": "us.amazon.nova-pro-v1:0",
        "prompt": "Inspect the plate.",
        "region": "eu-west-1",
        "max_tokens": 512,
    }
    parameters.update(params)
    return {
        "nodeId": node_id,
        "binding": "bedrock_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
        "capturePaths": (
            {"in": "{work_dir}/in.jpg", "reference": None}
            if capture_paths is None else capture_paths
        ),
    }


def llm_binding(node_id="llm1", capture_paths=None, **params):
    parameters = {
        "modelName": "qwen2-vl-2b",
        "prompt_template": "Compare the input to the reference.",
        "max_tokens": 128,
        "temperature": 0.7,
        "top_p": 0.9,
    }
    parameters.update(params)
    binding = {
        "nodeId": node_id,
        "binding": "llm_inference",
        "parameters": parameters,
        "upstreamNodeIds": ["cam"],
        "downstreamNodeIds": ["mqtt"],
    }
    if capture_paths is not None:
        binding["capturePaths"] = capture_paths
    return binding


def make_document(bindings):
    return {
        "schemaVersion": 1,
        "workflowId": "wf-1",
        "workflowVersion": "3",
        "segments": [],
        "executorBindings": list(bindings),
        "pluginDependencies": [],
    }


@pytest.fixture
def work_dir(tmp_path):
    (tmp_path / "in.jpg").write_bytes(INPUT_JPEG)
    (tmp_path / "ref.jpg").write_bytes(REFERENCE_JPEG)
    return str(tmp_path)


def recording_builder(monkeypatch, name):
    """Wrap ``output_bindings.<name>`` so the delegation is observable
    while the REAL shared builder still produces the invocation."""
    real = getattr(output_bindings, name)
    calls = []

    def wrapper(*args, **kwargs):
        invocation = real(*args, **kwargs)
        calls.append({"args": args, "kwargs": dict(kwargs),
                      "invocation": invocation})
        return invocation

    monkeypatch.setattr(output_bindings, name, wrapper)
    return calls


# ---------------------------------------------------------------------------
# One implementation: the re-exports ARE the shared module's objects
# ---------------------------------------------------------------------------

class TestSharedModuleIsTheOnlyImplementation:
    def test_verdict_parser_is_the_shared_one(self):
        assert output_bindings.parse_bedrock_answer is ai.parse_verdict

    def test_resolution_helpers_are_the_shared_ones(self):
        assert (output_bindings.resolve_output_token_budget
                is ai.resolve_output_token_budget)
        assert (output_bindings.resolve_max_image_dimension
                is ai.resolve_max_image_dimension)

    def test_builders_are_the_shared_ones(self):
        assert (output_bindings.build_bedrock_invocation
                is ai.build_bedrock_invocation)
        assert output_bindings.build_llm_invocation is ai.build_llm_invocation
        assert output_bindings.BedrockInvocation is ai.BedrockInvocation
        assert output_bindings.LlmInvocation is ai.LlmInvocation

    def test_constants_are_the_shared_values(self):
        assert (output_bindings.BEDROCK_JSON_INSTRUCTION
                == ai.BEDROCK_JSON_INSTRUCTION)
        assert output_bindings.BEDROCK_DEFAULT_MODEL == ai.BEDROCK_DEFAULT_MODEL
        assert (output_bindings.BEDROCK_READ_TIMEOUT_SEC
                == ai.BEDROCK_READ_TIMEOUT_SEC)
        assert (output_bindings.DEFAULT_OUTPUT_TOKEN_BUDGET
                == ai.DEFAULT_MAX_TOKENS)
        assert (output_bindings._LLM_GENERATION_PARAMETERS
                is ai.LLM_GENERATION_PARAMETERS)

    def test_module_no_longer_carries_its_own_construction(self):
        """The executor keeps image resolution and the transports; the
        request layout and the parser live only in the shared module."""
        with open(output_bindings.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        # The Converse content layout and the API body keys appear only
        # inside the shared module now.
        for literal in ('"is_anomalous": true|false',
                        '{"role": "user", "content"',
                        '"reference_image"'):
            assert literal not in source, literal


# ---------------------------------------------------------------------------
# BedrockInferenceProcessor delegates
# ---------------------------------------------------------------------------

class TestBedrockDelegation:
    def test_builder_receives_the_parameters_and_the_resolved_frames(
            self, monkeypatch, work_dir):
        calls = recording_builder(monkeypatch, "build_bedrock_invocation")
        binding = bedrock_binding(capture_paths={
            "in": "{work_dir}/in.jpg", "reference": "{work_dir}/ref.jpg"})
        processor = BedrockInferenceProcessor(invoker=ArityRecordingInvoker())

        processor.process(make_document([binding]), {}, work_dir)

        assert len(calls) == 1
        parameters, input_image, reference_image = calls[0]["args"]
        assert parameters == binding["parameters"]
        assert input_image == INPUT_JPEG
        assert reference_image == REFERENCE_JPEG

    def test_single_image_passes_a_none_reference(
            self, monkeypatch, work_dir):
        calls = recording_builder(monkeypatch, "build_bedrock_invocation")
        processor = BedrockInferenceProcessor(invoker=ArityRecordingInvoker())

        processor.process(
            make_document([bedrock_binding()]), {}, work_dir)

        assert calls[0]["args"][2] is None

    def test_invoker_receives_exactly_the_invocation_fields(
            self, monkeypatch, work_dir):
        """The executor no longer re-derives anything: a stubbed builder
        returning distinctive fields drives the invocation verbatim."""
        stub = BedrockInvocation(
            model="stub-model",
            prompt="stub prompt",
            images=(("Input image", b"stub-input"),
                    ("Reference image", b"stub-reference")),
            region="stub-region-1",
            max_tokens=17,
            system_prompt=None,
        )
        monkeypatch.setattr(
            output_bindings, "build_bedrock_invocation",
            lambda *a, **k: stub)
        invoker = ArityRecordingInvoker()

        BedrockInferenceProcessor(invoker=invoker).process(
            make_document([bedrock_binding()]), {}, work_dir)

        assert invoker.args == (
            "stub-model", "stub prompt",
            [("Input image", b"stub-input"),
             ("Reference image", b"stub-reference")],
            "stub-region-1", 17,
        )
        assert invoker.kwargs == {}

    def test_images_are_passed_as_a_list_of_label_bytes_pairs(
            self, work_dir):
        """The shipped invoker contract: ``images`` is a *list* of
        ``(label, bytes)`` tuples, not the invocation's tuple."""
        invoker = ArityRecordingInvoker()

        BedrockInferenceProcessor(invoker=invoker).process(
            make_document([bedrock_binding(capture_paths={
                "in": "{work_dir}/in.jpg",
                "reference": "{work_dir}/ref.jpg"})]),
            {}, work_dir)

        images = invoker.args[2]
        assert isinstance(images, list)
        assert images == [("Input image", INPUT_JPEG),
                          ("Reference image", REFERENCE_JPEG)]

    def test_parsing_goes_through_the_shared_verdict_parser(
            self, monkeypatch, work_dir):
        seen = []

        def recording_parser(text):
            seen.append(text)
            return ai.parse_verdict(text)

        monkeypatch.setattr(
            output_bindings, "parse_bedrock_answer", recording_parser)
        answer = '{"is_anomalous": false, "confidence": 0.25}'

        metadata = BedrockInferenceProcessor(
            invoker=ArityRecordingInvoker(answer=answer)).process(
                make_document([bedrock_binding()]), {}, work_dir)

        assert seen == [answer]
        assert metadata["is_anomalous"] is False
        assert metadata["confidence"] == 0.25

    def test_freeform_mode_never_parses(self, monkeypatch, work_dir):
        monkeypatch.setattr(
            output_bindings, "parse_bedrock_answer",
            lambda text: pytest.fail("freeform mode must not parse"))

        metadata = BedrockInferenceProcessor(
            invoker=ArityRecordingInvoker(answer="prose only")).process(
                make_document([bedrock_binding(anomaly_mode=False)]),
                {}, work_dir)

        assert metadata["bedrock_text"] == "prose only"


class TestBedrockInvokerArities:
    def test_five_positional_arguments_without_a_system_prompt(
            self, work_dir):
        """Pre-feature arity: a five-parameter fake keeps working."""
        invoker = FixedArityBedrockInvoker()

        BedrockInferenceProcessor(invoker=invoker).process(
            make_document([bedrock_binding()]), {}, work_dir)

        assert len(invoker.calls) == 1
        model, prompt, images, region, max_tokens = invoker.calls[0]
        assert model == "us.amazon.nova-pro-v1:0"
        assert prompt == ("Inspect the plate.\n\n"
                          + ai.BEDROCK_JSON_INSTRUCTION)
        assert images == [("Input image", INPUT_JPEG)]
        assert region == "eu-west-1"
        assert max_tokens == 512

    @pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
    def test_blank_system_prompt_keeps_the_pre_feature_arity(
            self, blank, work_dir):
        invoker = FixedArityBedrockInvoker()

        BedrockInferenceProcessor(invoker=invoker).process(
            make_document([bedrock_binding(system_prompt=blank)]),
            {}, work_dir)

        assert len(invoker.calls) == 1

    def test_configured_system_prompt_rides_as_the_sixth_argument(
            self, work_dir):
        invoker = ArityRecordingInvoker()

        BedrockInferenceProcessor(invoker=invoker).process(
            make_document(
                [bedrock_binding(system_prompt="  Be terse.  ")]),
            {}, work_dir)

        assert len(invoker.args) == 6
        # VERBATIM, never stripped.
        assert invoker.args[5] == "  Be terse.  "
        assert invoker.kwargs == {}


# ---------------------------------------------------------------------------
# LlmInferenceProcessor delegates
# ---------------------------------------------------------------------------

class TestLlmDelegation:
    def test_builder_receives_the_rendered_prompt_and_the_frames(
            self, monkeypatch, work_dir):
        calls = recording_builder(monkeypatch, "build_llm_invocation")
        binding = llm_binding(
            prompt_template="Compare {seed} to the reference.",
            capture_paths={"in": "{work_dir}/in.jpg",
                           "reference": "{work_dir}/ref.jpg"})
        parameters_before = copy.deepcopy(binding["parameters"])
        processor = LlmInferenceProcessor(invoker=ArityRecordingInvoker())

        processor.process(make_document([binding]), {"seed": 7}, work_dir)

        assert len(calls) == 1
        parameters, rendered, input_frame, reference_frame = calls[0]["args"]
        # The template is rendered by the executor, never by the module.
        assert rendered == "Compare 7 to the reference."
        assert input_frame == INPUT_JPEG
        assert reference_frame == REFERENCE_JPEG
        # The binding's own parameters mapping is never handed over.
        assert parameters == parameters_before
        assert binding["parameters"] == parameters_before
        # The executor's contained Pillow helper is injected.
        assert callable(calls[0]["kwargs"]["downscaler"])

    def test_no_frames_pass_none_for_both_images(
            self, monkeypatch, work_dir):
        calls = recording_builder(monkeypatch, "build_llm_invocation")

        LlmInferenceProcessor(invoker=ArityRecordingInvoker()).process(
            make_document([llm_binding()]), {}, work_dir)

        assert calls[0]["args"][2] is None
        assert calls[0]["args"][3] is None

    def test_unresolved_placeholder_never_reaches_the_builder(
            self, monkeypatch, work_dir):
        calls = recording_builder(monkeypatch, "build_llm_invocation")
        invoker = ArityRecordingInvoker()

        metadata = LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(prompt_template="{missing}")]),
            {}, work_dir)

        assert calls == []
        assert invoker.calls == []
        assert metadata["llm"]["llm1"] == {
            "error": "unresolved placeholder missing"}

    def test_unreadable_frame_never_reaches_the_builder(
            self, monkeypatch, work_dir):
        calls = recording_builder(monkeypatch, "build_llm_invocation")

        LlmInferenceProcessor(invoker=ArityRecordingInvoker()).process(
            make_document([llm_binding(capture_paths={
                "in": "{work_dir}/missing.jpg"})]),
            {}, work_dir)

        assert calls == []

    def test_parsing_goes_through_the_shared_verdict_parser(
            self, monkeypatch, work_dir):
        seen = []

        def recording_parser(text):
            seen.append(text)
            return ai.parse_verdict(text)

        monkeypatch.setattr(
            output_bindings, "parse_bedrock_answer", recording_parser)
        answer = '{"is_anomalous": true, "confidence": 0.5}'

        metadata = LlmInferenceProcessor(
            invoker=ArityRecordingInvoker(answer=answer)).process(
                make_document([llm_binding(anomaly_mode=True)]), {}, work_dir)

        assert seen == [answer]
        assert metadata["is_anomalous"] is True


class TestLlmInvokerArities:
    def test_three_positional_arguments_without_frames(self, work_dir):
        """Pre-feature arity: a three-parameter fake keeps working."""
        invoker = FixedArityLlmInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding()]), {}, work_dir)

        assert len(invoker.calls) == 1
        model_name, prompt, parameters = invoker.calls[0]
        assert model_name == "qwen2-vl-2b"
        assert prompt == "Compare the input to the reference."
        # The third positional argument carries the generation
        # parameters, ``max_tokens`` resolved to the Output_Token_Budget.
        assert parameters["max_tokens"] == 128

    def test_four_positional_arguments_with_the_input_frame(self, work_dir):
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(
                capture_paths={"in": "{work_dir}/in.jpg"})]),
            {}, work_dir)

        assert len(invoker.args) == 4
        assert invoker.args[3] == base64.b64encode(
            INPUT_JPEG).decode("ascii")
        assert invoker.kwargs == {}

    def test_five_positional_arguments_with_both_frames(self, work_dir):
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(capture_paths={
                "in": "{work_dir}/in.jpg",
                "reference": "{work_dir}/ref.jpg"})]),
            {}, work_dir)

        assert len(invoker.args) == 5
        assert invoker.args[3] == base64.b64encode(
            INPUT_JPEG).decode("ascii")
        assert invoker.args[4] == base64.b64encode(
            REFERENCE_JPEG).decode("ascii")
        assert invoker.kwargs == {}

    @pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
    def test_blank_system_prompt_is_not_forwarded(self, blank, work_dir):
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(system_prompt=blank)]), {}, work_dir)

        assert invoker.kwargs == {}

    def test_configured_system_prompt_rides_as_a_keyword(self, work_dir):
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(system_prompt=" Be terse. ")]),
            {}, work_dir)

        assert invoker.kwargs == {"system_prompt": " Be terse. "}

    def test_metrics_sink_only_for_invokers_that_declare_it(self, work_dir):
        plain = ArityRecordingInvoker()
        LlmInferenceProcessor(invoker=plain).process(
            make_document([llm_binding()]), {}, work_dir)
        assert "metrics_sink" not in plain.kwargs

        aware = MetricsAwareLlmInvoker()
        LlmInferenceProcessor(invoker=aware).process(
            make_document([llm_binding()]), {}, work_dir)
        assert callable(aware.kwargs["metrics_sink"])

    BUDGETS = ((None, 256), (0, 256), ("abc", 256), (True, 256),
               (64, 64), (1024.0, 1024))

    @pytest.mark.parametrize("configured,expected", BUDGETS)
    def test_resolved_budget_reaches_the_invoker(
            self, configured, expected, work_dir):
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(max_tokens=configured)]),
            {}, work_dir)

        assert invoker.args[2]["max_tokens"] == expected


class TestLlmDownscaleParity:
    """The injected downscaler is the executor's own Pillow helper, so
    the base64 the invoker receives equals
    ``downscale_image_bytes(frame, max_dim)``."""

    @staticmethod
    def _jpeg(width, height):
        Image = pytest.importorskip("PIL.Image")
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (width, height), (10, 120, 200)).save(
            buffer, format="JPEG")
        return buffer.getvalue()

    def test_downscaled_bytes_are_what_is_encoded(self, tmp_path):
        raw = self._jpeg(1280, 720)
        (tmp_path / "in.jpg").write_bytes(raw)
        (tmp_path / "ref.jpg").write_bytes(raw)
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(
                max_image_dimension=640,
                capture_paths={"in": "{work_dir}/in.jpg",
                               "reference": "{work_dir}/ref.jpg"})]),
            {}, str(tmp_path))

        expected = base64.b64encode(
            output_bindings.downscale_image_bytes(raw, 640)).decode("ascii")
        assert invoker.args[3] == expected
        assert invoker.args[4] == expected
        # The downscale really happened (the sent bytes are not the file).
        assert invoker.args[3] != base64.b64encode(raw).decode("ascii")

    def test_downscaling_failure_sends_the_original_bytes(self, tmp_path):
        """The helper's containment contract survives the delegation: a
        frame Pillow cannot decode is sent unmodified."""
        (tmp_path / "in.jpg").write_bytes(b"not-a-jpeg")
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(
                max_image_dimension=640,
                capture_paths={"in": "{work_dir}/in.jpg"})]),
            {}, str(tmp_path))

        assert invoker.args[3] == base64.b64encode(
            b"not-a-jpeg").decode("ascii")

    def test_unconfigured_dimension_sends_the_frame_unmodified(
            self, tmp_path):
        raw = self._jpeg(1280, 720)
        (tmp_path / "in.jpg").write_bytes(raw)
        invoker = ArityRecordingInvoker()

        LlmInferenceProcessor(invoker=invoker).process(
            make_document([llm_binding(
                capture_paths={"in": "{work_dir}/in.jpg"})]),
            {}, str(tmp_path))

        assert invoker.args[3] == base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# The default transports consume the invocation's request
# ---------------------------------------------------------------------------

class _StubConverseClient:
    def __init__(self):
        self.kwargs = None

    def converse(self, **kwargs):
        self.kwargs = kwargs
        return {"output": {"message": {"content": [
            {"text": VERDICT_ANSWER}]}}}


@pytest.fixture
def stub_boto3(monkeypatch):
    """Inject a minimal ``boto3``/``botocore.config`` (neither is
    installed in this venv) and expose what the transport built."""
    client = _StubConverseClient()
    captured = {}

    boto3 = types.ModuleType("boto3")

    def _client(service_name, region_name=None, config=None):
        captured["service_name"] = service_name
        captured["region_name"] = region_name
        captured["config"] = config
        return client

    boto3.client = _client

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    botocore_config.Config = Config
    botocore.config = botocore_config

    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return {"client": client, "captured": captured}


class TestDefaultTransportsSendTheInvocationRequest:
    def test_bedrock_transport_sends_converse_kwargs(self, stub_boto3):
        images = [("Input image", INPUT_JPEG),
                  ("Reference image", REFERENCE_JPEG)]

        answer = _default_bedrock_invoker(
            "us.amazon.nova-pro-v1:0", "Inspect.", images, "eu-west-1", 512,
            "Be terse.")

        assert answer == VERDICT_ANSWER
        assert stub_boto3["client"].kwargs == BedrockInvocation(
            model="us.amazon.nova-pro-v1:0",
            prompt="Inspect.",
            images=tuple(images),
            region="eu-west-1",
            max_tokens=512,
            system_prompt="Be terse.",
        ).converse_kwargs()
        # The transport keeps owning the client construction.
        captured = stub_boto3["captured"]
        assert captured["service_name"] == "bedrock-runtime"
        assert captured["region_name"] == "eu-west-1"
        assert captured["config"].kwargs == {
            "read_timeout": ai.BEDROCK_READ_TIMEOUT_SEC,
            "retries": {"max_attempts": 1},
        }

    def test_bedrock_transport_omits_system_when_absent(self, stub_boto3):
        _default_bedrock_invoker(
            "m", "Inspect.", [("Input image", INPUT_JPEG)], "us-east-1", 256)

        assert "system" not in stub_boto3["client"].kwargs

    def test_llm_transport_posts_the_request_body(self, monkeypatch):
        import requests

        posted = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"generated_text": "generated answer"}

        def fake_post(url, json=None, timeout=None):
            posted.update(url=url, body=json, timeout=timeout)
            return _Response()

        monkeypatch.setattr(requests, "post", fake_post)
        parameters = {"max_tokens": 128, "temperature": 0.7, "top_p": 0.9}
        image_b64 = base64.b64encode(INPUT_JPEG).decode("ascii")
        reference_b64 = base64.b64encode(REFERENCE_JPEG).decode("ascii")

        text = _default_llm_invoker(
            "qwen2-vl-2b", "Compare.", parameters, image_b64, reference_b64,
            "Be terse.")

        assert text == "generated answer"
        assert posted["body"] == LlmInvocation(
            model_name="qwen2-vl-2b",
            prompt="Compare.",
            generation=dict(parameters),
            image_b64=image_b64,
            reference_b64=reference_b64,
            system_prompt="Be terse.",
        ).request_body()
        assert posted["url"] == output_bindings.TEXT_GENERATION_URL.format(
            model_name="qwen2-vl-2b")
        assert posted["timeout"] == output_bindings.LLM_GENERATION_TIMEOUT_SEC
        # The body's field order is part of the request.
        assert list(posted["body"].keys()) == [
            "prompt", "max_tokens", "temperature", "top_p", "image",
            "reference_image", "system_prompt"]

    def test_llm_transport_body_is_json_serializable(self, monkeypatch):
        import requests

        posted = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"generated_text": ""}

        monkeypatch.setattr(
            requests, "post",
            lambda url, json=None, timeout=None: (
                posted.update(body=json) or _Response()))

        _default_llm_invoker("m", "Compare.", {"max_tokens": 64})

        assert json.loads(json.dumps(posted["body"])) == {
            "prompt": "Compare.", "max_tokens": 64}
