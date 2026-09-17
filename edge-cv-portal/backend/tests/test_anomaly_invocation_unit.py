"""Unit tests for the shared Invocation_Builder (spec task 1.6).

Table-driven unit coverage of
``workflow_core.anomaly_invocation`` — the design's unit-test list for
this module (Testing Strategy → Unit tests → ``workflow_core.
anomaly_invocation``):

- the Verdict_Instruction is appended **iff** the node is in
  Anomaly_Mode, per node type (``bedrock_inference`` defaults to
  Anomaly_Mode, ``llm_inference`` does not);
- the system prompt is passed VERBATIM or is ``None``, never stripped;
- the two images carry the "Input image" / "Reference image" labels in
  that order;
- the model / region / token defaults;
- the ``converse_kwargs`` shape;
- the ``llm_inference`` downscale-before-base64 and Output_Token_Budget
  rules (the executor's helpers' parity, restated here; that the
  executor uses *these* functions is asserted device-side in
  ``test/backend-test/workflow_engine/
  test_anomaly_invocation_delegation.py``);
- ``prompt_fingerprint`` stability and sensitivity;
- the ``categorize_outcome`` table;
- ``summarize_outcomes`` on empty and partial outcome sets.

**Validates: Requirements 6.2, 11.1**

Scope: these are unit tests, not property tests — the properties of this
module are Properties 1, 6, 8 and 9 in
``test_property_anomaly_invocation.py`` /
``test_property_anomaly_invocation_eligibility.py``. Here every case is
an explicitly enumerated expectation, so a change of instruction text,
label, ordering, default or category name is a named failure.

Harness: pure values only — no AWS, no moto, no boto3, no device. The
injected downscaler is a deterministic fake standing in for the
executor's contained Pillow helper.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import sys

import pytest

# The workflow_core layer is on sys.path via tests/conftest.py; repeated
# here so the file also runs standalone.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_CORE_LAYER = os.path.abspath(
    os.path.join(_HERE, "..", "layers", "workflow_core", "python"))
if _WORKFLOW_CORE_LAYER not in sys.path:
    sys.path.append(_WORKFLOW_CORE_LAYER)

from workflow_core import anomaly_invocation as ai  # noqa: E402

INPUT_JPEG = b"\xff\xd8input-image-bytes\xff\xd9"
REFERENCE_JPEG = b"\xff\xd8reference-image-bytes\xff\xd9"

#: The Verdict_Instruction and its separator, restated from Requirement
#: 6.2 / the glossary rather than imported, so a change of either is a
#: failure here.
EXPECTED_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'
)
EXPECTED_SEPARATOR = "\n\n"


def bedrock_parameters(**overrides):
    """A complete ``bedrock_inference`` parameter mapping."""
    parameters = {
        "model": "us.amazon.nova-pro-v1:0",
        "region": "eu-west-1",
        "prompt": "Inspect the plate.",
        "max_tokens": 512,
    }
    parameters.update(overrides)
    return parameters


def llm_parameters(**overrides):
    """A complete ``llm_inference`` parameter mapping."""
    parameters = {
        "modelName": "qwen2-vl-2b",
        "max_tokens": 128,
        "temperature": 0.7,
        "top_p": 0.9,
    }
    parameters.update(overrides)
    return parameters


class RecordingDownscaler:
    """Deterministic stand-in for the executor's Pillow helper.

    Records every ``(data, max_dim, port)`` call and returns bytes that
    are distinguishable from the input, so "downscaled BEFORE base64"
    is observable in the encoded payload.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, data, max_dim, port):
        self.calls.append((data, max_dim, port))
        return b"scaled:" + str(max_dim).encode("ascii") + b":" + data


# ---------------------------------------------------------------------------
# Verdict_Instruction: appended iff Anomaly_Mode, per node type
# ---------------------------------------------------------------------------

class TestInstructionAppendedIffAnomalyMode:
    """The instruction rides the USER prompt exactly in Anomaly_Mode."""

    # bedrock_inference: absent/None/truthy ⇒ Anomaly_Mode.
    BEDROCK_ANOMALY = ("absent", None, True, "true", "TRUE", " true ", 1, "7")
    # bedrock_inference: only an explicitly falsy value is freeform.
    BEDROCK_FREEFORM = (False, "false", "False", " false ", 0, "0", "", "0.0")
    # llm_inference: only a truthy value is Anomaly_Mode.
    LLM_ANOMALY = (True, "true", "TRUE", " true ", 1, "7")
    # llm_inference: absent/None/falsy ⇒ freeform (the shipped default).
    LLM_FREEFORM = ("absent", None, False, "false", 0, "0", "", "0.0")

    @staticmethod
    def _with_mode(parameters, anomaly_mode):
        if anomaly_mode != "absent":
            parameters["anomaly_mode"] = anomaly_mode
        return parameters

    @pytest.mark.parametrize("anomaly_mode", BEDROCK_ANOMALY)
    def test_bedrock_anomaly_mode_appends_instruction(self, anomaly_mode):
        parameters = self._with_mode(
            bedrock_parameters(prompt="Inspect."), anomaly_mode)

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.anomaly_mode is True
        assert invocation.prompt == (
            "Inspect." + EXPECTED_SEPARATOR + EXPECTED_INSTRUCTION)

    @pytest.mark.parametrize("anomaly_mode", BEDROCK_FREEFORM)
    def test_bedrock_freeform_sends_prompt_unchanged(self, anomaly_mode):
        parameters = self._with_mode(
            bedrock_parameters(prompt="Describe."), anomaly_mode)

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.anomaly_mode is False
        assert invocation.prompt == "Describe."

    @pytest.mark.parametrize("anomaly_mode", LLM_ANOMALY)
    def test_llm_anomaly_mode_appends_instruction(self, anomaly_mode):
        parameters = self._with_mode(llm_parameters(), anomaly_mode)

        invocation = ai.build_llm_invocation(parameters, "Compare them.")

        assert invocation.anomaly_mode is True
        assert invocation.prompt == (
            "Compare them." + EXPECTED_SEPARATOR + EXPECTED_INSTRUCTION)

    @pytest.mark.parametrize("anomaly_mode", LLM_FREEFORM)
    def test_llm_freeform_sends_rendered_prompt_unchanged(self, anomaly_mode):
        parameters = self._with_mode(llm_parameters(), anomaly_mode)

        invocation = ai.build_llm_invocation(parameters, "Summarize the run.")

        assert invocation.anomaly_mode is False
        assert invocation.prompt == "Summarize the run."

    def test_instruction_is_appended_once_even_when_prompt_contains_it(self):
        """Appending is unconditional, never de-duplicated."""
        loaded = "Inspect. " + EXPECTED_INSTRUCTION
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(prompt=loaded), INPUT_JPEG)

        assert invocation.prompt == (
            loaded + EXPECTED_SEPARATOR + EXPECTED_INSTRUCTION)

    def test_empty_prompt_still_carries_the_instruction(self):
        """An absent/empty prompt yields the instruction alone — never
        ``None`` and never the string ``"None"``."""
        for prompt in (None, "", 0):
            invocation = ai.build_bedrock_invocation(
                bedrock_parameters(prompt=prompt), INPUT_JPEG)
            assert invocation.prompt == (
                EXPECTED_SEPARATOR + EXPECTED_INSTRUCTION)

        llm = ai.build_llm_invocation(
            llm_parameters(anomaly_mode=True), "")
        assert llm.prompt == EXPECTED_SEPARATOR + EXPECTED_INSTRUCTION

    def test_instruction_never_touches_the_system_prompt(self):
        """Anomaly_Mode appends to the USER prompt only."""
        parameters = bedrock_parameters(system_prompt="You are an inspector.")

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.system_prompt == "You are an inspector."
        assert EXPECTED_INSTRUCTION in invocation.prompt

        llm = ai.build_llm_invocation(
            llm_parameters(anomaly_mode=True,
                           system_prompt="You are an inspector."),
            "Compare.")
        assert llm.system_prompt == "You are an inspector."
        assert EXPECTED_INSTRUCTION in llm.prompt


# ---------------------------------------------------------------------------
# System prompt: verbatim or None
# ---------------------------------------------------------------------------

class TestSystemPromptVerbatimOrNone:
    """``normalize_system_prompt``'s rule, through both builders."""

    ABSENT = ("absent", None, "", " ", "\n", "\t \n")
    VERBATIM = (
        "You are an inspector.",
        "  leading and trailing kept  ",
        "\nmultiline\nsystem\n",
        'Answer {"text": ..., "objects": [...]}',
        "a" * 300,
    )

    @pytest.mark.parametrize("raw", ABSENT)
    def test_blank_system_prompt_is_none(self, raw):
        parameters = bedrock_parameters()
        if raw != "absent":
            parameters["system_prompt"] = raw

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.system_prompt is None
        # No system parameter is sent at all.
        assert "system" not in invocation.converse_kwargs()

        llm_params = llm_parameters()
        if raw != "absent":
            llm_params["system_prompt"] = raw
        llm = ai.build_llm_invocation(llm_params, "Compare.")
        assert llm.system_prompt is None
        assert "system_prompt" not in llm.request_body()

    @pytest.mark.parametrize("raw", VERBATIM)
    def test_non_blank_system_prompt_is_verbatim(self, raw):
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(system_prompt=raw), INPUT_JPEG)

        assert invocation.system_prompt == raw
        assert invocation.converse_kwargs()["system"] == [{"text": raw}]

        llm = ai.build_llm_invocation(
            llm_parameters(system_prompt=raw), "Compare.")
        assert llm.system_prompt == raw
        assert llm.request_body()["system_prompt"] == raw

    def test_non_string_system_prompt_is_stringified(self):
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(system_prompt=42), INPUT_JPEG)

        assert invocation.system_prompt == "42"

    @pytest.mark.parametrize("falsy", (None, ""))
    def test_directly_constructed_invocations_omit_a_falsy_system(
            self, falsy):
        """The dataclasses are constructed directly too (the transports,
        the Portal's Bedrock_Scorer), so omitting a falsy system field is
        the dataclass's rule, not only the builder's. Whitespace-only
        text is *normalized away by the builder* (above) rather than by
        the dataclass, which — like the pre-feature transports — sends
        whatever truthy text it is handed."""
        bedrock = ai.BedrockInvocation(
            model="m", prompt="p", images=(("Input image", INPUT_JPEG),),
            region="us-east-1", max_tokens=256, system_prompt=falsy)
        assert "system" not in bedrock.converse_kwargs()

        llm = ai.LlmInvocation(
            model_name="m", prompt="p", generation={"max_tokens": 256},
            system_prompt=falsy)
        assert "system_prompt" not in llm.request_body()


# ---------------------------------------------------------------------------
# Image labels and their order
# ---------------------------------------------------------------------------

class TestImageLabelOrder:
    def test_input_then_reference(self):
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(), INPUT_JPEG, REFERENCE_JPEG)

        assert invocation.images == (
            ("Input image", INPUT_JPEG),
            ("Reference image", REFERENCE_JPEG),
        )

    def test_single_image_when_reference_is_none(self):
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(), INPUT_JPEG, None)

        assert invocation.images == (("Input image", INPUT_JPEG),)

    def test_empty_reference_bytes_are_still_attached(self):
        """Only ``None`` means single-image; empty bytes are a frame the
        executor read and must still be sent (the transport, not the
        builder, decides what Bedrock makes of it)."""
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(), INPUT_JPEG, b"")

        assert invocation.images == (
            ("Input image", INPUT_JPEG),
            ("Reference image", b""),
        )

    def test_image_bytes_are_carried_unmodified(self):
        raw = bytes(range(256))
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(), raw, raw)

        for _label, data in invocation.images:
            assert data == raw


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    FALSY = ("absent", None, "", 0, False)

    @pytest.mark.parametrize("raw", FALSY)
    def test_model_default(self, raw):
        parameters = bedrock_parameters()
        parameters.pop("model")
        if raw != "absent":
            parameters["model"] = raw

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.model == "us.amazon.nova-lite-v1:0"

    @pytest.mark.parametrize("raw", FALSY)
    def test_region_default(self, raw):
        parameters = bedrock_parameters()
        parameters.pop("region")
        if raw != "absent":
            parameters["region"] = raw

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.region == "us-east-1"

    @pytest.mark.parametrize("raw", FALSY)
    def test_bedrock_max_tokens_default(self, raw):
        parameters = bedrock_parameters()
        parameters.pop("max_tokens")
        if raw != "absent":
            parameters["max_tokens"] = raw

        invocation = ai.build_bedrock_invocation(parameters, INPUT_JPEG)

        assert invocation.max_tokens == 256

    def test_configured_values_win_and_are_normalized(self):
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(model="anthropic.claude-3-haiku",
                               region="ap-south-1", max_tokens="1024"),
            INPUT_JPEG)

        assert invocation.model == "anthropic.claude-3-haiku"
        assert invocation.region == "ap-south-1"
        assert invocation.max_tokens == 1024

    def test_published_defaults_match_the_catalog(self):
        """The constants the Portal shows as defaults."""
        assert ai.BEDROCK_DEFAULT_MODEL == "us.amazon.nova-lite-v1:0"
        assert ai.BEDROCK_DEFAULT_REGION == "us-east-1"
        assert ai.DEFAULT_MAX_TOKENS == 256
        assert ai.BEDROCK_READ_TIMEOUT_SEC == 30
        assert ai.BEDROCK_JSON_INSTRUCTION == EXPECTED_INSTRUCTION

    def test_llm_model_name_defaults_to_empty_string(self):
        parameters = llm_parameters()
        parameters.pop("modelName")

        invocation = ai.build_llm_invocation(parameters, "Compare.")

        assert invocation.model_name == ""


# ---------------------------------------------------------------------------
# converse_kwargs shape
# ---------------------------------------------------------------------------

class TestConverseKwargsShape:
    def test_two_image_request_shape(self):
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(prompt="Inspect.", system_prompt="Be terse."),
            INPUT_JPEG, REFERENCE_JPEG)

        assert invocation.converse_kwargs() == {
            "modelId": "us.amazon.nova-pro-v1:0",
            "messages": [{
                "role": "user",
                "content": [
                    {"text": "Inspect." + EXPECTED_SEPARATOR
                             + EXPECTED_INSTRUCTION},
                    {"text": "Input image:"},
                    {"image": {"format": "jpeg",
                               "source": {"bytes": INPUT_JPEG}}},
                    {"text": "Reference image:"},
                    {"image": {"format": "jpeg",
                               "source": {"bytes": REFERENCE_JPEG}}},
                ],
            }],
            "inferenceConfig": {"maxTokens": 512},
            "system": [{"text": "Be terse."}],
        }

    def test_single_image_request_shape_without_system(self):
        parameters = bedrock_parameters(prompt="Inspect.", anomaly_mode=False)
        parameters.pop("model")
        parameters.pop("max_tokens")

        kwargs = ai.build_bedrock_invocation(
            parameters, INPUT_JPEG).converse_kwargs()

        assert kwargs == {
            "modelId": "us.amazon.nova-lite-v1:0",
            "messages": [{
                "role": "user",
                "content": [
                    {"text": "Inspect."},
                    {"text": "Input image:"},
                    {"image": {"format": "jpeg",
                               "source": {"bytes": INPUT_JPEG}}},
                ],
            }],
            "inferenceConfig": {"maxTokens": 256},
        }
        # No system key at all — not ``None``, not an empty list.
        assert "system" not in kwargs

    def test_key_order_is_stable(self):
        """The Converse kwargs' key order, which pins the request the
        transport sends (``modelId``, ``messages``, ``inferenceConfig``,
        then ``system`` when present)."""
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(system_prompt="Be terse."), INPUT_JPEG)

        assert list(invocation.converse_kwargs().keys()) == [
            "modelId", "messages", "inferenceConfig", "system"]

    def test_max_tokens_is_an_int_in_the_kwargs(self):
        invocation = ai.BedrockInvocation(
            model="m", prompt="p", images=(("Input image", INPUT_JPEG),),
            region="us-east-1", max_tokens=64.0)

        max_tokens = invocation.converse_kwargs()["inferenceConfig"]["maxTokens"]
        assert max_tokens == 64
        assert isinstance(max_tokens, int)

    def test_kwargs_are_freshly_built_each_call(self):
        """A caller mutating the kwargs cannot affect the next request."""
        invocation = ai.build_bedrock_invocation(
            bedrock_parameters(), INPUT_JPEG)

        first = invocation.converse_kwargs()
        first["messages"][0]["content"].append({"text": "injected"})

        assert ai.build_bedrock_invocation(
            bedrock_parameters(), INPUT_JPEG).converse_kwargs() == \
            invocation.converse_kwargs()
        assert len(invocation.converse_kwargs()["messages"][0]["content"]) == 3

    def test_builders_never_mutate_the_caller_mapping(self):
        parameters = bedrock_parameters(system_prompt=" keep ")
        before = copy.deepcopy(parameters)

        ai.build_bedrock_invocation(parameters, INPUT_JPEG, REFERENCE_JPEG)

        assert parameters == before

        llm_params = llm_parameters(max_tokens="nope", max_image_dimension="x")
        llm_before = copy.deepcopy(llm_params)
        ai.build_llm_invocation(llm_params, "Compare.", INPUT_JPEG)
        assert llm_params == llm_before


# ---------------------------------------------------------------------------
# llm_inference: downscale, base64 and the Output_Token_Budget
# ---------------------------------------------------------------------------

class TestLlmDownscaleBase64AndBudget:
    def test_images_are_base64_of_the_exact_bytes_when_unconfigured(self):
        downscaler = RecordingDownscaler()

        invocation = ai.build_llm_invocation(
            llm_parameters(), "Compare.", INPUT_JPEG, REFERENCE_JPEG,
            downscaler=downscaler)

        assert downscaler.calls == []
        assert invocation.image_b64 == base64.b64encode(
            INPUT_JPEG).decode("ascii")
        assert invocation.reference_b64 == base64.b64encode(
            REFERENCE_JPEG).decode("ascii")

    def test_downscaling_happens_before_encoding_for_both_frames(self):
        downscaler = RecordingDownscaler()

        invocation = ai.build_llm_invocation(
            llm_parameters(max_image_dimension=640), "Compare.",
            INPUT_JPEG, REFERENCE_JPEG, downscaler=downscaler)

        # Both frames, in port order, with the resolved bound and the
        # executor's port names.
        assert downscaler.calls == [
            (INPUT_JPEG, 640, "in"),
            (REFERENCE_JPEG, 640, "reference"),
        ]
        assert invocation.image_b64 == base64.b64encode(
            b"scaled:640:" + INPUT_JPEG).decode("ascii")
        assert invocation.reference_b64 == base64.b64encode(
            b"scaled:640:" + REFERENCE_JPEG).decode("ascii")

    def test_without_a_downscaler_frames_are_sent_unmodified(self):
        invocation = ai.build_llm_invocation(
            llm_parameters(max_image_dimension=640), "Compare.",
            INPUT_JPEG, REFERENCE_JPEG)

        assert invocation.image_b64 == base64.b64encode(
            INPUT_JPEG).decode("ascii")
        assert invocation.reference_b64 == base64.b64encode(
            REFERENCE_JPEG).decode("ascii")

    def test_reference_requires_an_input_image(self):
        downscaler = RecordingDownscaler()

        invocation = ai.build_llm_invocation(
            llm_parameters(max_image_dimension=640), "Compare.",
            None, REFERENCE_JPEG, downscaler=downscaler)

        assert invocation.image_b64 is None
        assert invocation.reference_b64 is None
        # The reference is never even downscaled: it is not sent.
        assert downscaler.calls == []
        body = invocation.request_body()
        assert "image" not in body and "reference_image" not in body

    def test_text_only_body_shape(self):
        parameters = llm_parameters()
        parameters.pop("temperature")
        parameters.pop("top_p")

        body = ai.build_llm_invocation(parameters, "Summarize.").request_body()

        assert body == {"prompt": "Summarize.", "max_tokens": 128}
        assert list(body.keys()) == ["prompt", "max_tokens"]

    def test_full_body_shape_and_key_order(self):
        body = ai.build_llm_invocation(
            llm_parameters(system_prompt="Be terse.", anomaly_mode=True),
            "Compare.", INPUT_JPEG, REFERENCE_JPEG).request_body()

        assert body == {
            "prompt": "Compare." + EXPECTED_SEPARATOR + EXPECTED_INSTRUCTION,
            "max_tokens": 128,
            "temperature": 0.7,
            "top_p": 0.9,
            "image": base64.b64encode(INPUT_JPEG).decode("ascii"),
            "reference_image": base64.b64encode(
                REFERENCE_JPEG).decode("ascii"),
            "system_prompt": "Be terse.",
        }
        assert list(body.keys()) == [
            "prompt", "max_tokens", "temperature", "top_p", "image",
            "reference_image", "system_prompt"]

    def test_absent_generation_parameters_are_omitted(self):
        parameters = llm_parameters()
        parameters["temperature"] = None
        parameters.pop("top_p")

        body = ai.build_llm_invocation(parameters, "Compare.").request_body()

        assert "temperature" not in body
        assert "top_p" not in body

    # Output_Token_Budget: the executor's rule, restated.
    BUDGETS = (
        ("absent", 256, False),
        (None, 256, False),
        (1, 1, False),
        (128, 128, False),
        (4096, 4096, False),
        (256.0, 256, False),      # integral float accepted as its int
        (0, 256, True),           # below the >= 1 bound
        (-5, 256, True),
        (0.5, 256, True),
        ("128", 256, True),       # strings are not numbers here
        ("abc", 256, True),
        (True, 256, True),        # bool is not a token count
        (float("inf"), 256, True),
        (float("nan"), 256, True),
    )

    @pytest.mark.parametrize("raw,expected,notice_expected", BUDGETS)
    def test_output_token_budget_rule(self, raw, expected, notice_expected):
        budget, notice = ai.resolve_output_token_budget(
            None if raw == "absent" else raw)

        assert budget == expected
        assert isinstance(budget, int) and not isinstance(budget, bool)
        assert (notice is not None) is notice_expected
        if notice_expected:
            assert "max_tokens" in notice and "256" in notice

    @pytest.mark.parametrize("raw,expected,notice_expected", BUDGETS)
    def test_budget_reaches_the_body_and_the_notice_sink(
            self, raw, expected, notice_expected):
        parameters = llm_parameters()
        if raw == "absent":
            parameters.pop("max_tokens")
        else:
            parameters["max_tokens"] = raw
        notices = []

        invocation = ai.build_llm_invocation(
            parameters, "Compare.", notice_sink=notices.append)

        assert invocation.generation["max_tokens"] == expected
        assert invocation.request_body()["max_tokens"] == expected
        assert len(notices) == (1 if notice_expected else 0)

    # max_image_dimension: the executor's rule, restated.
    DIMENSIONS = (
        ("absent", None, False),
        (None, None, False),
        (1, 1, False),
        (640, 640, False),
        (1024.0, 1024, False),
        (0, None, True),
        (-3, None, True),
        (12.5, None, True),
        ("640", None, True),
        (True, None, True),
        (float("inf"), None, True),
    )

    @pytest.mark.parametrize("raw,expected,notice_expected", DIMENSIONS)
    def test_max_image_dimension_rule(self, raw, expected, notice_expected):
        max_dim, notice = ai.resolve_max_image_dimension(
            None if raw == "absent" else raw)

        assert max_dim == expected
        assert (notice is not None) is notice_expected
        if notice_expected:
            assert "max_image_dimension" in notice

    @pytest.mark.parametrize("raw,expected,notice_expected", DIMENSIONS)
    def test_invalid_dimension_means_no_downscaling(
            self, raw, expected, notice_expected):
        parameters = llm_parameters()
        if raw != "absent":
            parameters["max_image_dimension"] = raw
        downscaler = RecordingDownscaler()
        notices = []

        invocation = ai.build_llm_invocation(
            parameters, "Compare.", INPUT_JPEG, downscaler=downscaler,
            notice_sink=notices.append)

        if expected is None:
            assert downscaler.calls == []
            assert invocation.image_b64 == base64.b64encode(
                INPUT_JPEG).decode("ascii")
        else:
            assert [call[1] for call in downscaler.calls] == [expected]
        assert len(notices) == (1 if notice_expected else 0)

    def test_notice_order_is_dimension_then_budget(self):
        notices = []

        ai.build_llm_invocation(
            llm_parameters(max_image_dimension="nope", max_tokens="nope"),
            "Compare.", INPUT_JPEG, notice_sink=notices.append)

        assert len(notices) == 2
        assert "max_image_dimension" in notices[0]
        assert "max_tokens" in notices[1]

    def test_notices_are_optional(self):
        """Without a sink the builder is silent, never raising."""
        invocation = ai.build_llm_invocation(
            llm_parameters(max_image_dimension="nope", max_tokens="nope"),
            "Compare.", INPUT_JPEG)

        assert invocation.generation["max_tokens"] == 256


# ---------------------------------------------------------------------------
# prompt_fingerprint
# ---------------------------------------------------------------------------

class TestPromptFingerprint:
    BASE = {"prompt": "Inspect the plate.",
            "system_prompt": "Be terse.", "max_tokens": 256}

    def test_shape_is_self_describing(self):
        fingerprint = ai.prompt_fingerprint(self.BASE)

        assert fingerprint.startswith("sha256:")
        assert len(fingerprint) == len("sha256:") + 64
        int(fingerprint[len("sha256:"):], 16)  # hex

    def test_stable_across_calls_and_key_order(self):
        reordered = {"max_tokens": 256, "system_prompt": "Be terse.",
                     "prompt": "Inspect the plate."}

        assert ai.prompt_fingerprint(self.BASE) == \
            ai.prompt_fingerprint(self.BASE)
        assert ai.prompt_fingerprint(reordered) == \
            ai.prompt_fingerprint(self.BASE)

    def test_ignores_everything_outside_the_prompt_set(self):
        with_noise = dict(self.BASE, model="anthropic.claude-3-haiku",
                          region="ap-south-1", anomaly_mode=True,
                          temperature=0.2, crop_detection_index=2)

        assert ai.prompt_fingerprint(with_noise) == \
            ai.prompt_fingerprint(self.BASE)

    def test_prompt_template_is_read_when_prompt_is_absent(self):
        template_set = {"prompt_template": "Inspect the plate.",
                        "system_prompt": "Be terse.", "max_tokens": 256}

        assert ai.prompt_fingerprint(template_set) == \
            ai.prompt_fingerprint(self.BASE)

    def test_prompt_wins_over_prompt_template(self):
        both = dict(self.BASE, prompt_template="something else")

        assert ai.prompt_fingerprint(both) == ai.prompt_fingerprint(self.BASE)

    def test_token_budget_is_coerced_like_a_parameter(self):
        assert ai.prompt_fingerprint(dict(self.BASE, max_tokens="256")) == \
            ai.prompt_fingerprint(self.BASE)

    def test_blank_system_prompts_are_all_the_same(self):
        without = {"prompt": "Inspect the plate.", "max_tokens": 256}
        for blank in (None, "", "   ", "\n"):
            assert ai.prompt_fingerprint(
                dict(without, system_prompt=blank)) == \
                ai.prompt_fingerprint(without)

    SENSITIVE = (
        {"prompt": "Inspect the plates."},          # one character
        {"prompt": "Inspect the plate. "},          # trailing space
        {"prompt": ""},
        {"system_prompt": "Be terse!"},
        {"system_prompt": " Be terse."},            # never stripped
        {"system_prompt": None},
        {"max_tokens": 257},
        {"max_tokens": None},
    )

    @pytest.mark.parametrize("change", SENSITIVE)
    def test_sensitive_to_every_prompt_set_field(self, change):
        assert ai.prompt_fingerprint(dict(self.BASE, **change)) != \
            ai.prompt_fingerprint(self.BASE)

    def test_unserializable_values_do_not_raise(self):
        """A stored Prompt_Set can carry anything; fingerprinting a
        sample must never fail the export."""
        assert ai.prompt_fingerprint(
            {"prompt": "p", "max_tokens": object()}).startswith("sha256:")


# ---------------------------------------------------------------------------
# categorize_outcome
# ---------------------------------------------------------------------------

ANOMALOUS = {"is_anomalous": True, "confidence": 0.9}
NOT_ANOMALOUS = {"is_anomalous": False, "confidence": 0.2}


class TestCategorizeOutcome:
    TABLE = (
        # label, verdict, error, expected category
        ("NOK", ANOMALOUS, None, "correct"),
        ("OK", NOT_ANOMALOUS, None, "correct"),
        ("NOK", NOT_ANOMALOUS, None, "false_pass"),
        ("OK", ANOMALOUS, None, "false_fail"),
        ("NOK", None, None, "parse_failure"),
        ("OK", None, None, "parse_failure"),
        ("NOK", None, "ThrottlingException", "invocation_error"),
        ("OK", ANOMALOUS, "ThrottlingException", "invocation_error"),
        # Case and whitespace of the label.
        ("ok", NOT_ANOMALOUS, None, "correct"),
        (" nok ", ANOMALOUS, None, "correct"),
        ("Nok", NOT_ANOMALOUS, None, "false_pass"),
        # A blank error is not an error.
        ("OK", NOT_ANOMALOUS, "", "correct"),
        ("OK", NOT_ANOMALOUS, "   ", "correct"),
        # Verdicts missing/odd ``is_anomalous`` are read for truthiness.
        ("NOK", {}, None, "false_pass"),
        ("OK", {"is_anomalous": "yes"}, None, "false_fail"),
        ("OK", {"is_anomalous": 0}, None, "correct"),
    )

    @pytest.mark.parametrize("label,verdict,error,expected", TABLE)
    def test_table(self, label, verdict, error, expected):
        assert ai.categorize_outcome(label, verdict, error) == expected

    def test_error_wins_over_everything(self):
        assert ai.categorize_outcome(
            "not-a-label", None, "boom") == "invocation_error"

    def test_parse_failure_wins_over_the_label_check(self):
        assert ai.categorize_outcome("EXCLUDE", None) == "parse_failure"

    @pytest.mark.parametrize(
        "label", ("EXCLUDE", "", "   ", None, "unlabelled", "OKAY", 1))
    def test_unscoreable_label_with_a_verdict_raises(self, label):
        with pytest.raises(ValueError):
            ai.categorize_outcome(label, ANOMALOUS)

    def test_error_default_is_no_error(self):
        assert ai.categorize_outcome("OK", NOT_ANOMALOUS) == "correct"

    def test_category_constants_and_reporting_order(self):
        assert ai.OUTCOME_CATEGORIES == (
            "correct", "false_pass", "false_fail", "parse_failure",
            "invocation_error")
        assert (ai.LABEL_OK, ai.LABEL_NOK, ai.LABEL_EXCLUDE) == (
            "OK", "NOK", "EXCLUDE")


# ---------------------------------------------------------------------------
# summarize_outcomes
# ---------------------------------------------------------------------------

def outcome(sample_id, category, is_anomalous=None, tokens=None,
            latency=None):
    return {
        "sampleId": sample_id,
        "category": category,
        "isAnomalous": is_anomalous,
        "outputTokens": tokens,
        "latencyMs": latency,
    }


EMPTY_SUMMARY = {
    "samples": 0, "invocations": 0, "correct": 0, "falsePass": 0,
    "falseFail": 0, "parseFailure": 0, "invocationError": 0,
    "accuracy": None, "unstable": 0, "meanOutputTokens": None,
    "maxOutputTokens": None, "meanLatencyMs": None,
}


class TestSummarizeOutcomes:
    def test_empty(self):
        assert ai.summarize_outcomes([]) == EMPTY_SUMMARY
        assert ai.summarize_outcomes(iter([])) == EMPTY_SUMMARY

    def test_keys_are_always_the_same(self):
        populated = ai.summarize_outcomes(
            [outcome("s1", "correct", False, 30, 1200.0)])

        assert set(populated) == set(EMPTY_SUMMARY)

    def test_partial_run(self):
        """A run that has produced 5 of its planned outcomes summarizes
        those 5 — the summary is never a running counter."""
        outcomes = [
            outcome("s1", "correct", True, 24, 3000.0),
            outcome("s2", "false_pass", False, 20, 1000.0),
            outcome("s3", "false_fail", True, 40, 2000.0),
            outcome("s4", "parse_failure", None, 200, 4000.0),
            outcome("s5", "invocation_error"),
        ]

        assert ai.summarize_outcomes(outcomes) == {
            "samples": 5, "invocations": 5, "correct": 1, "falsePass": 1,
            "falseFail": 1, "parseFailure": 1, "invocationError": 1,
            "accuracy": 0.2, "unstable": 0,
            "meanOutputTokens": (24 + 20 + 40 + 200) / 4.0,
            "maxOutputTokens": 200,
            "meanLatencyMs": (3000.0 + 1000.0 + 2000.0 + 4000.0) / 4.0,
        }

    def test_repeats_and_instability(self):
        outcomes = [
            # Agreeing repeats.
            outcome("s1", "correct", True), outcome("s1", "correct", True),
            # Disagreeing repeats.
            outcome("s2", "correct", True), outcome("s2", "false_pass", False),
            # A parse failure disagrees with a verdict...
            outcome("s3", "correct", True), outcome("s3", "parse_failure"),
            # ...and agrees with itself.
            outcome("s4", "parse_failure"), outcome("s4", "parse_failure"),
            # An invocation error is its own value too.
            outcome("s5", "invocation_error"),
            outcome("s5", "parse_failure"),
        ]

        summary = ai.summarize_outcomes(outcomes)

        assert summary["samples"] == 5
        assert summary["invocations"] == 10
        assert summary["unstable"] == 3  # s2, s3, s5

    def test_accuracy_is_over_invocations_not_samples(self):
        outcomes = [outcome("s1", "correct", False),
                    outcome("s1", "false_fail", True)]

        assert ai.summarize_outcomes(outcomes)["accuracy"] == 0.5

    def test_unrecognized_category_counts_only_towards_invocations(self):
        summary = ai.summarize_outcomes([outcome("s1", "weird")])

        assert summary["invocations"] == 1
        assert summary["samples"] == 1
        assert all(summary[key] == 0 for key in (
            "correct", "falsePass", "falseFail", "parseFailure",
            "invocationError"))
        assert summary["accuracy"] == 0.0

    def test_missing_token_and_latency_values_are_not_reported(self):
        summary = ai.summarize_outcomes([
            outcome("s1", "correct", True, None, None),
            outcome("s2", "correct", True, 10, 500),
            # Non-numeric and bool values are "not reported".
            outcome("s3", "correct", True, "120", "slow"),
            outcome("s4", "correct", True, True, False),
        ])

        assert summary["meanOutputTokens"] == 10.0
        assert summary["maxOutputTokens"] == 10
        assert summary["meanLatencyMs"] == 500.0

    def test_absent_keys_are_tolerated(self):
        summary = ai.summarize_outcomes([{"category": "correct"}, {}])

        assert summary["invocations"] == 2
        assert summary["samples"] == 1  # both carry sampleId None
        assert summary["correct"] == 1
        assert summary["meanOutputTokens"] is None

    def test_is_a_pure_function_of_the_outcomes(self):
        outcomes = [outcome("s1", "correct", True, 24, 100.0),
                    outcome("s2", "false_pass", False, 20, 200.0)]
        snapshot = copy.deepcopy(outcomes)

        first = ai.summarize_outcomes(outcomes)

        assert outcomes == snapshot
        assert ai.summarize_outcomes(outcomes) == first
        assert ai.summarize_outcomes(reversed(outcomes)) == first

    def test_summary_is_json_serializable(self):
        summary = ai.summarize_outcomes(
            [outcome("s1", "correct", True, 24, 100.0)])

        assert json.loads(json.dumps(summary)) == summary


# ---------------------------------------------------------------------------
# The module's purity contract (Requirement 11.1: nothing here can touch a
# run — it cannot even reach AWS, the filesystem or the network)
# ---------------------------------------------------------------------------

def test_module_imports_only_the_standard_library():
    import ast

    with open(ai.__file__, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pytest.fail("relative import in a pure module")
            imported.add((node.module or "").split(".")[0])

    assert imported == {"base64", "hashlib", "json", "re", "dataclasses",
                        "typing"}
