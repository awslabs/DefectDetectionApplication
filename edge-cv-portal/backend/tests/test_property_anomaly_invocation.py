"""Property tests for the shared Invocation_Builder (spec task 1.3).

Three correctness properties of
``workflow_core.anomaly_invocation`` — the pure module every call path
builds anomaly-inspection requests through:

- **Feature: quality-prompt-tuning, Property 6: Executor,
  Bedrock_Scorer and Device_Score_Job build identical invocations**
  (pure-module half) — **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
- **Feature: quality-prompt-tuning, Property 8: Outcome categorization
  is total and exact** — **Validates: Requirements 6.5**
- **Feature: quality-prompt-tuning, Property 9: The Score_Summary is a
  function of the persisted outcomes** — **Validates: Requirements 6.7,
  6.11, 6.14, 10.4**

Scope of the Property 6 half asserted here
------------------------------------------

The three call paths differ only in how they *assemble* the builder's
arguments; the construction itself is this one module. This file
therefore pins the pure half: the same effective (Node_Parameters,
Prompt_Set, image bytes) assembled the way each path assembles them
produces invocations that are equal field for field.

- **executor path** — the compiled binding hands the node's parameters
  (Prompt_Set included) straight to the builder, images read from the
  device's captures.
- **Bedrock_Scorer path** — ``node_parameters ∪ candidate Prompt_Set``
  (the Candidate wins), the node parameters having travelled through
  the stored Workflow_Definition JSON, images read from the
  Sample_Store.
- **Device_Score_Job path** — the job manifest's ``nodeParameters`` and
  ``promptSet`` (a JSON document in S3), merged on the device, images
  read from the Sample_Store.

That the three *call sites* actually route through this module is
asserted by tasks 3.4 (device, vendored copy) and 6.3/6.4 (Portal), as
the design's property-placement table says; here the paths are modelled
by the argument assembly each performs, including the JSON round trip
their transports impose. ``llm_inference`` has only two paths (the
Portal never invokes a device-local model), so its half compares the
executor and the job runner.

The expected requests are **restated independently** in this file from
Requirements 6.2/6.3/6.4, the glossary's Verdict_Instruction and the
design's request layout — never imported from the module under test —
so a change of instruction text, image label, field name, ordering or
default is a failure rather than a silent agreement.

Harness: pure values only (no AWS, no moto, no boto3, no device). The
injected downscaler is a deterministic fake standing in for the
executor's contained Pillow helper.
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# The workflow_core layer is on sys.path via tests/conftest.py; repeated
# here so the file also runs standalone.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKFLOW_CORE_LAYER = os.path.abspath(
    os.path.join(_HERE, "..", "layers", "workflow_core", "python"))
if _WORKFLOW_CORE_LAYER not in sys.path:
    sys.path.append(_WORKFLOW_CORE_LAYER)

from workflow_core import anomaly_invocation as ai  # noqa: E402


# ---------------------------------------------------------------------------
# Reference expectations, restated from the requirements and the design.
# ---------------------------------------------------------------------------

#: Glossary "Verdict_Instruction" — the canonical text appended to every
#: Anomaly_Mode prompt.
VERDICT_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'
)
#: The blank line between the operator's prompt and the instruction.
INSTRUCTION_SEPARATOR = "\n\n"

#: Catalog defaults for an absent parameter.
DEFAULT_MODEL = "us.amazon.nova-lite-v1:0"
DEFAULT_REGION = "us-east-1"
DEFAULT_MAX_TOKENS = 256

#: Design's Converse content layout: the two image labels, in order.
INPUT_IMAGE_LABEL = "Input image"
REFERENCE_IMAGE_LABEL = "Reference image"

#: Requirement 6.4 / the Text_Generation_API body's generation keys.
GENERATION_KEYS = ("max_tokens", "temperature", "top_p")

#: Requirement 6.5 categories.
CATEGORY_CORRECT = "correct"
CATEGORY_FALSE_PASS = "false_pass"
CATEGORY_FALSE_FAIL = "false_fail"
CATEGORY_PARSE_FAILURE = "parse_failure"
CATEGORY_INVOCATION_ERROR = "invocation_error"
ALL_CATEGORIES = (
    CATEGORY_CORRECT, CATEGORY_FALSE_PASS, CATEGORY_FALSE_FAIL,
    CATEGORY_PARSE_FAILURE, CATEGORY_INVOCATION_ERROR,
)

#: Sentinel for "the builder is expected to raise".
RAISES = "<raises>"


def ref_coerce(value):
    """The executor's parameter coercion, restated."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def ref_bedrock_anomaly_mode(anomaly_mode):
    """`bedrock_inference`: absent/None defaults to Anomaly_Mode."""
    coerced = ref_coerce(anomaly_mode)
    return True if coerced is None else bool(coerced)


def ref_llm_anomaly_mode(anomaly_mode):
    """`llm_inference`: Anomaly_Mode only when truthy."""
    return bool(ref_coerce(anomaly_mode))


def ref_system_text(raw):
    """Absent/blank ⇒ no system text; otherwise verbatim."""
    if raw is None:
        return None
    text = str(raw)
    return text if text.strip() else None


def ref_output_token_budget(raw):
    """Requirement 5.5's budget resolution: an integral number >= 1, else
    the documented default."""
    if raw is None:
        return DEFAULT_MAX_TOKENS
    if not isinstance(raw, bool):
        if isinstance(raw, int) and raw >= 1:
            return raw
        if isinstance(raw, float) and raw.is_integer() and raw >= 1:
            return int(raw)
    return DEFAULT_MAX_TOKENS


def ref_token_budget_substituted(raw):
    """True when the budget resolution had to substitute the default for
    an invalid configured value (a notice is expected)."""
    if raw is None:
        return False
    return ref_output_token_budget(raw) == DEFAULT_MAX_TOKENS and not (
        (isinstance(raw, int) and not isinstance(raw, bool)
         and raw == DEFAULT_MAX_TOKENS)
        or (isinstance(raw, float) and raw.is_integer()
            and int(raw) == DEFAULT_MAX_TOKENS)
    )


def ref_max_image_dimension(raw):
    """The downscaling bound: an integral number >= 1, else unconfigured."""
    if raw is None:
        return None
    if not isinstance(raw, bool):
        if isinstance(raw, int) and raw >= 1:
            return raw
        if isinstance(raw, float) and raw.is_integer() and raw >= 1:
            return int(raw)
    return None


def ref_converse_kwargs(parameters, input_bytes, reference_bytes):
    """The exact Converse keyword arguments Requirement 6.2 pins."""
    prompt = str(parameters.get("prompt") or "")
    if ref_bedrock_anomaly_mode(parameters.get("anomaly_mode")):
        prompt = prompt + INSTRUCTION_SEPARATOR + VERDICT_INSTRUCTION
    content = [
        {"text": prompt},
        {"text": INPUT_IMAGE_LABEL + ":"},
        {"image": {"format": "jpeg", "source": {"bytes": input_bytes}}},
    ]
    if reference_bytes is not None:
        content.append({"text": REFERENCE_IMAGE_LABEL + ":"})
        content.append(
            {"image": {"format": "jpeg", "source": {"bytes": reference_bytes}}})
    kwargs = {
        "modelId": str(parameters.get("model") or DEFAULT_MODEL),
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {
            "maxTokens": int(parameters.get("max_tokens") or DEFAULT_MAX_TOKENS),
        },
    }
    system_text = ref_system_text(parameters.get("system_prompt"))
    if system_text:
        kwargs["system"] = [{"text": system_text}]
    return kwargs


def ref_llm_body(parameters, rendered_prompt, input_bytes, reference_bytes,
                 downscaler):
    """The exact Text_Generation_API body Requirement 6.4 pins."""
    prompt = str(rendered_prompt or "")
    if ref_llm_anomaly_mode(parameters.get("anomaly_mode")):
        prompt = prompt + INSTRUCTION_SEPARATOR + VERDICT_INSTRUCTION
    max_dim = ref_max_image_dimension(parameters.get("max_image_dimension"))

    def encode(data, port):
        if data is None:
            return None
        if max_dim is not None and downscaler is not None:
            data = downscaler(data, max_dim, port)
        return base64.b64encode(data).decode("ascii")

    image = encode(input_bytes, "in")
    # The API's reference-requires-image rule: no input frame ⇒ the
    # executor's 3-argument invocation, which carries no reference.
    reference = encode(reference_bytes, "reference") if image is not None else None

    body = {"prompt": prompt}
    budget = ref_output_token_budget(parameters.get("max_tokens"))
    for key in GENERATION_KEYS:
        value = budget if key == "max_tokens" else parameters.get(key)
        if value is not None:
            body[key] = value
    if image is not None:
        body["image"] = image
    if reference is not None:
        body["reference_image"] = reference
    system_text = ref_system_text(parameters.get("system_prompt"))
    if system_text:
        body["system_prompt"] = system_text
    return body


def ref_category(label, verdict, error):
    """Requirement 6.5's categorization, restated.

    ``RAISES`` for a label outside {OK, NOK} that the ordering actually
    reaches: only labelled, non-excluded samples enter a Score_Run
    (Requirement 4.3), so such a call is a caller defect rather than a
    silent ``correct``.
    """
    if error is not None and str(error).strip():
        return CATEGORY_INVOCATION_ERROR
    if verdict is None:
        return CATEGORY_PARSE_FAILURE
    normalized = str(label).strip().upper() if label is not None else ""
    if normalized not in ("OK", "NOK"):
        return RAISES
    is_anomalous = bool(verdict.get("is_anomalous"))
    if normalized == "NOK":
        return CATEGORY_CORRECT if is_anomalous else CATEGORY_FALSE_PASS
    return CATEGORY_FALSE_FAIL if is_anomalous else CATEGORY_CORRECT


def ref_summary(outcomes):
    """The design's Score_Summary definition, restated.

    An unrecognized category counts towards ``invocations`` only (task
    1.2's recorded decision); a token count or latency is "reported"
    only when it is a real number (``bool`` is not a number here).
    """
    def numeric(value):
        if isinstance(value, bool) or value is None:
            return None
        return value if isinstance(value, (int, float)) else None

    keys = {
        CATEGORY_CORRECT: "correct",
        CATEGORY_FALSE_PASS: "falsePass",
        CATEGORY_FALSE_FAIL: "falseFail",
        CATEGORY_PARSE_FAILURE: "parseFailure",
        CATEGORY_INVOCATION_ERROR: "invocationError",
    }
    counts = {name: 0 for name in keys.values()}
    invocations = 0
    verdict_values = {}
    tokens = []
    latencies = []
    for outcome in outcomes:
        invocations += 1
        category = outcome.get("category")
        if category in keys:
            counts[keys[category]] += 1
        sample_id = outcome.get("sampleId")
        if category in (CATEGORY_PARSE_FAILURE, CATEGORY_INVOCATION_ERROR):
            value = category
        else:
            value = bool(outcome.get("isAnomalous"))
        verdict_values.setdefault(sample_id, set()).add(value)
        token_count = numeric(outcome.get("outputTokens"))
        if token_count is not None:
            tokens.append(token_count)
        latency = numeric(outcome.get("latencyMs"))
        if latency is not None:
            latencies.append(latency)

    summary = {
        "samples": len(verdict_values),
        "invocations": invocations,
        "accuracy": (counts["correct"] / float(invocations)
                     if invocations else None),
        "unstable": sum(1 for values in verdict_values.values()
                        if len(values) > 1),
        "meanOutputTokens": (sum(tokens) / float(len(tokens))
                             if tokens else None),
        "maxOutputTokens": max(tokens) if tokens else None,
        "meanLatencyMs": (sum(latencies) / float(len(latencies))
                          if latencies else None),
    }
    summary.update(counts)
    return summary


# ---------------------------------------------------------------------------
# Call-path models: how each of the three assembles the builder's inputs.
# ---------------------------------------------------------------------------

def _json_document(value):
    """A value that has travelled through a JSON document (the stored
    Workflow_Definition, a job manifest in S3)."""
    return json.loads(json.dumps(value))


def _attempt(build):
    """Run a builder, returning either its result or a comparable
    description of the exception it raised, so a configuration that the
    executor rejects today is compared across paths too."""
    try:
        return ("built", build())
    except Exception as exc:  # noqa: BLE001 - the raise itself is the datum
        return ("raised", type(exc).__name__, str(exc))


def _make_downscaler():
    """A deterministic stand-in for the executor's contained Pillow
    downscaler (``downscaler(data, max_dim, port)``)."""
    def downscale(data, max_dim, port):
        return b"|".join((b"downscaled", str(max_dim).encode("ascii"),
                          port.encode("ascii"), data))
    return downscale


def executor_bedrock(node_parameters, prompt_set, input_bytes, reference_bytes):
    """Executor path: the compiled binding's parameter mapping (the
    node's Prompt_Set is part of it), frames read from the device."""
    parameters = dict(node_parameters)
    parameters.update(prompt_set)
    return _attempt(lambda: ai.build_bedrock_invocation(
        parameters, input_bytes, reference_bytes))


def scorer_bedrock(node_parameters, prompt_set, input_bytes, reference_bytes):
    """Bedrock_Scorer path: Node_Parameters (out of the stored
    definition) merged under the Candidate's Prompt_Set, bytes from the
    Sample_Store."""
    parameters = dict(_json_document(node_parameters))
    parameters.update(_json_document(prompt_set))
    stored_input = bytes(input_bytes)
    stored_reference = None if reference_bytes is None else bytes(reference_bytes)
    return _attempt(lambda: ai.build_bedrock_invocation(
        parameters, stored_input, stored_reference))


def job_runner_bedrock(node_parameters, prompt_set, input_bytes,
                       reference_bytes):
    """Device_Score_Job path: the manifest's ``nodeParameters`` /
    ``promptSet`` merged on the device, bytes GET from the Sample_Store.

    (A Bedrock node is scored from the Portal; the runner is modelled
    here as well because the manifest carries the same Prompt_Set +
    Node_Parameters shape and the property is about the construction
    being path-independent.)
    """
    manifest = _json_document({
        "nodeParameters": node_parameters, "promptSet": prompt_set,
    })
    parameters = dict(manifest["nodeParameters"])
    parameters.update(manifest["promptSet"])
    fetched_input = bytes(input_bytes)
    fetched_reference = None if reference_bytes is None else bytes(reference_bytes)
    return _attempt(lambda: ai.build_bedrock_invocation(
        parameters, fetched_input, fetched_reference))


def executor_llm(node_parameters, prompt_set, rendered_prompt, input_bytes,
                 reference_bytes, notices):
    """Executor path for ``llm_inference``: ``render_prompt`` has already
    produced ``rendered_prompt``; the module downscales through the
    executor's helper."""
    parameters = dict(node_parameters)
    parameters.update(prompt_set)
    return _attempt(lambda: ai.build_llm_invocation(
        parameters, rendered_prompt, input_bytes, reference_bytes,
        downscaler=_make_downscaler(), notice_sink=notices.append))


def job_runner_llm(node_parameters, prompt_set, rendered_prompt, input_bytes,
                   reference_bytes, notices):
    """Device_Score_Job path for ``llm_inference``: same rendered prompt
    (rendered against the manifest's metadata snippet), parameters out
    of the manifest, the executor's downscaler and transport."""
    manifest = _json_document({
        "nodeParameters": node_parameters, "promptSet": prompt_set,
        "renderedPrompt": rendered_prompt,
    })
    parameters = dict(manifest["nodeParameters"])
    parameters.update(manifest["promptSet"])
    return _attempt(lambda: ai.build_llm_invocation(
        parameters, manifest["renderedPrompt"], bytes(input_bytes)
        if input_bytes is not None else None,
        bytes(reference_bytes) if reference_bytes is not None else None,
        downscaler=_make_downscaler(), notice_sink=notices.append))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

TEXT = st.text(max_size=40)

#: Anomaly_Mode parameter values, including the string/numeric shapes a
#: tag-substituted definition can carry.
ANOMALY_MODE = st.sampled_from([
    None, True, False, "true", "false", "TRUE", " False ", 1, 0, "", "yes",
])

PROMPTS = st.one_of(st.none(), st.just(""), TEXT, st.integers(-3, 3))
SYSTEM_PROMPTS = st.one_of(
    st.none(), st.just(""), st.just("   "), st.just("\n"), TEXT,
    st.just("Answer with {text, objects[]}."),
    # Padded text: a non-blank system prompt must travel VERBATIM, never
    # stripped, so the operator's text reaches the model unmodified.
    st.builds(lambda text: "  " + text + "\n", TEXT),
    st.just("\n  Answer strictly in one JSON object.  \n"),
)
MAX_TOKENS = st.one_of(
    st.none(), st.just(0), st.just(True), st.integers(-5, 4096),
    st.sampled_from(["512", "0", "abc", "12.5", ""]),
    st.sampled_from([1.0, 1.5, 256.0, float("nan"), float("inf")]),
)
MODELS = st.one_of(
    st.none(), st.just(""),
    st.sampled_from(["us.amazon.nova-lite-v1:0", "us.amazon.nova-pro-v1:0"]),
    TEXT,
)
REGIONS = st.one_of(
    st.none(), st.just(""), st.sampled_from(["us-east-1", "eu-central-1"]),
)
IMAGE_BYTES = st.binary(min_size=0, max_size=48)
OPTIONAL_IMAGE_BYTES = st.one_of(st.none(), IMAGE_BYTES)

MAX_IMAGE_DIMENSION = st.one_of(
    st.none(), st.just(0), st.just(True), st.integers(-3, 2048),
    st.sampled_from([640.0, 640.5, "640", "abc"]),
)
SAMPLING = st.one_of(
    st.none(), st.floats(0, 1, allow_nan=False, allow_infinity=False),
    st.integers(0, 1),
)


@st.composite
def bedrock_case(draw):
    """A `bedrock_inference` node's Node_Parameters and a Candidate's
    Prompt_Set, plus the sample's bytes."""
    node_parameters = {
        "model": draw(MODELS),
        "region": draw(REGIONS),
        "anomaly_mode": draw(ANOMALY_MODE),
        # A non-invocation parameter the node also carries.
        "crop_margin_percent": draw(st.one_of(st.none(), st.integers(0, 50))),
        # The deployed Prompt_Set the Candidate overrides.
        "prompt": draw(PROMPTS),
        "system_prompt": draw(SYSTEM_PROMPTS),
        "max_tokens": draw(MAX_TOKENS),
    }
    prompt_set = {
        "prompt": draw(PROMPTS),
        "system_prompt": draw(SYSTEM_PROMPTS),
        "max_tokens": draw(MAX_TOKENS),
    }
    return (node_parameters, prompt_set, draw(IMAGE_BYTES),
            draw(OPTIONAL_IMAGE_BYTES))


@st.composite
def llm_case(draw):
    """An `llm_inference` node's Node_Parameters and Prompt_Set, the
    rendered prompt, and the sample's bytes (either port may be unfed)."""
    node_parameters = {
        "modelName": draw(st.one_of(st.none(), st.just(""),
                                    st.sampled_from(["Qwen2.5-VL", "opt125m"]),
                                    TEXT)),
        "temperature": draw(SAMPLING),
        "top_p": draw(SAMPLING),
        "max_image_dimension": draw(MAX_IMAGE_DIMENSION),
        "anomaly_mode": draw(ANOMALY_MODE),
        "prompt_template": draw(PROMPTS),
        "system_prompt": draw(SYSTEM_PROMPTS),
        "max_tokens": draw(MAX_TOKENS),
    }
    prompt_set = {
        "prompt_template": draw(PROMPTS),
        "system_prompt": draw(SYSTEM_PROMPTS),
        "max_tokens": draw(MAX_TOKENS),
    }
    return (node_parameters, prompt_set, draw(st.one_of(st.none(), TEXT)),
            draw(OPTIONAL_IMAGE_BYTES), draw(OPTIONAL_IMAGE_BYTES))


# ---------------------------------------------------------------------------
# Property 6 (pure-module half)
# ---------------------------------------------------------------------------

@given(case=bedrock_case(), llm=llm_case(), answer=st.one_of(
    st.just('{"is_anomalous": true, "confidence": 0.9}'),
    st.just('```json\n{"is_anomalous": false, "confidence": 0.1}\n```'),
    st.just("no verdict here"),
    st.just(""),
    TEXT,
))
@settings(max_examples=100, deadline=None)
def test_property_all_paths_build_identical_invocations(case, llm, answer):
    """**Feature: quality-prompt-tuning, Property 6: Executor,
    Bedrock_Scorer and Device_Score_Job build identical invocations.**

    For any Node_Parameters, Prompt_Set and input/reference bytes, the
    invocation built through the executor's argument assembly, through
    the Bedrock_Scorer's (Node_Parameters ∪ Candidate Prompt_Set out of
    JSON documents) and through the Device_Score_Job runner's (the
    manifest) is equal field for field — model, full prompt including
    the Verdict_Instruction, image labels/order/bytes (or base64),
    region, max_tokens, system text, generation parameters — and
    ``parse_verdict`` yields equal results for the same answer text in
    all three.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    """
    node_parameters, prompt_set, input_bytes, reference_bytes = case

    # --- Bedrock: three paths, then the independent restatement -------
    frozen = json.dumps([node_parameters, prompt_set], default=repr)
    results = [
        executor_bedrock(node_parameters, prompt_set, input_bytes,
                         reference_bytes),
        scorer_bedrock(node_parameters, prompt_set, input_bytes,
                       reference_bytes),
        job_runner_bedrock(node_parameters, prompt_set, input_bytes,
                           reference_bytes),
    ]
    # Purity: no path may mutate the caller's mappings.
    assert json.dumps([node_parameters, prompt_set], default=repr) == frozen

    assert results[0] == results[1] == results[2], (
        "the three call paths disagree: {0!r}".format(results))

    effective = dict(node_parameters)
    effective.update(prompt_set)
    expected = _attempt(lambda: ref_converse_kwargs(
        effective, input_bytes, reference_bytes))

    if results[0][0] == "raised":
        # A configuration the executor rejects today must be rejected
        # identically by the restatement (same exception, same message).
        assert expected[0] == "raised" and expected[1:] == results[0][1:]
    else:
        assert expected[0] == "built", (
            "builder accepted a configuration the restatement rejects: "
            "{0!r}".format(expected))
        invocations = [result[1] for result in results]
        assert invocations[0] == invocations[1] == invocations[2]
        for invocation in invocations:
            assert invocation.converse_kwargs() == expected[1]
            # Fields the transport (not the kwargs) consumes.
            assert invocation.region == str(
                effective.get("region") or DEFAULT_REGION)
            assert invocation.max_tokens == expected[1][
                "inferenceConfig"]["maxTokens"]
            assert invocation.system_prompt == ref_system_text(
                effective.get("system_prompt"))
            # The image bytes are the sample's, in order, labelled.
            labels = [label for label, _ in invocation.images]
            assert labels == ([INPUT_IMAGE_LABEL] if reference_bytes is None
                              else [INPUT_IMAGE_LABEL, REFERENCE_IMAGE_LABEL])
            assert invocation.images[0][1] == input_bytes
            if reference_bytes is not None:
                assert invocation.images[1][1] == reference_bytes
            # The Verdict_Instruction rides the user prompt in
            # Anomaly_Mode and only there.
            appended = invocation.prompt.endswith(
                INSTRUCTION_SEPARATOR + VERDICT_INSTRUCTION)
            assert appended is ref_bedrock_anomaly_mode(
                effective.get("anomaly_mode"))

    # --- llm_inference: the executor and the device job runner --------
    (llm_parameters, llm_prompt_set, rendered_prompt, llm_input,
     llm_reference) = llm
    executor_notices, runner_notices = [], []
    llm_results = [
        executor_llm(llm_parameters, llm_prompt_set, rendered_prompt,
                     llm_input, llm_reference, executor_notices),
        job_runner_llm(llm_parameters, llm_prompt_set, rendered_prompt,
                       llm_input, llm_reference, runner_notices),
    ]
    assert llm_results[0] == llm_results[1], (
        "executor and job runner disagree: {0!r}".format(llm_results))
    assert executor_notices == runner_notices

    llm_effective = dict(llm_parameters)
    llm_effective.update(llm_prompt_set)
    llm_expected = _attempt(lambda: ref_llm_body(
        llm_effective, rendered_prompt, llm_input, llm_reference,
        _make_downscaler()))
    if llm_results[0][0] == "raised":
        assert llm_expected[0] == "raised"
        assert llm_expected[1:] == llm_results[0][1:]
    else:
        assert llm_expected[0] == "built"
        for _, invocation in llm_results:
            body = invocation.request_body()
            assert body == llm_expected[1]
            # The body becomes a JSON payload, so field ORDER is part of
            # the request: prompt, generation parameters, images, system.
            assert json.dumps(body) == json.dumps(llm_expected[1])
            assert invocation.model_name == str(
                llm_effective.get("modelName") or "")
            assert invocation.system_prompt == ref_system_text(
                llm_effective.get("system_prompt"))
            # Every invocation carries an explicit Output_Token_Budget.
            assert invocation.generation["max_tokens"] == (
                ref_output_token_budget(llm_effective.get("max_tokens")))
            # Reference-requires-image.
            if invocation.image_b64 is None:
                assert invocation.reference_b64 is None
        # One notice per substituted parameter, image dimension first.
        expected_notices = []
        if (llm_effective.get("max_image_dimension") is not None
                and ref_max_image_dimension(
                    llm_effective.get("max_image_dimension")) is None):
            expected_notices.append("max_image_dimension")
        if ref_token_budget_substituted(llm_effective.get("max_tokens")):
            expected_notices.append("max_tokens")
        assert len(executor_notices) == len(expected_notices)
        for notice, parameter in zip(executor_notices, expected_notices):
            assert parameter in notice

    # --- parse_verdict is the same function on every path -------------
    parsed = [
        _attempt(lambda: ai.parse_verdict(answer)),                 # executor
        _attempt(lambda: ai.parse_verdict(                          # scorer
            _json_document({"rawAnswer": answer})["rawAnswer"])),
        _attempt(lambda: ai.parse_verdict(                          # runner
            _json_document({"generated_text": answer})["generated_text"])),
    ]
    assert parsed[0] == parsed[1] == parsed[2]


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------

#: Answer shapes, with the verdict the Verdict_Parser must reach for
#: each restated beside it (``None`` = the parser must reject).
ANSWER_SHAPES = (
    "plain_json", "fenced_json", "fenced_plain", "prose_wrapped",
    "string_values", "confidence_missing", "confidence_prose",
    "array_wrapped", "missing_key", "truncated", "empty", "prose_only",
    "two_objects",
)

PROSE = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:-!?",
    max_size=30,
)


def build_answer(shape, is_anomalous, confidence, prose):
    """Return ``(answer_text, expected_verdict_or_None)``."""
    verdict_object = json.dumps(
        {"is_anomalous": is_anomalous, "confidence": confidence})
    accepted = {"is_anomalous": is_anomalous, "confidence": float(confidence)}
    if shape == "plain_json":
        return verdict_object, accepted
    if shape == "fenced_json":
        return "```json\n" + verdict_object + "\n```", accepted
    if shape == "fenced_plain":
        return "```\n" + verdict_object + "\n```", accepted
    if shape == "prose_wrapped":
        return prose + "\n" + verdict_object + "\n" + prose, accepted
    if shape == "string_values":
        text = json.dumps({"is_anomalous": "true" if is_anomalous else "false",
                           "confidence": "0.8"})
        return text, {"is_anomalous": is_anomalous, "confidence": 0.8}
    if shape == "confidence_missing":
        return (json.dumps({"is_anomalous": is_anomalous}),
                {"is_anomalous": is_anomalous, "confidence": 0.0})
    if shape == "confidence_prose":
        return (json.dumps({"is_anomalous": is_anomalous,
                            "confidence": "high"}),
                {"is_anomalous": is_anomalous, "confidence": 0.0})
    if shape == "array_wrapped":
        return "[" + verdict_object + "]", accepted
    if shape == "missing_key":
        return json.dumps({"anomalous": is_anomalous,
                           "confidence": confidence}), None
    if shape == "truncated":
        return verdict_object[:len(verdict_object) // 2], None
    if shape == "empty":
        return "", None
    if shape == "prose_only":
        return prose, None
    if shape == "two_objects":
        return json.dumps({"note": prose}) + "\n" + verdict_object, None
    raise AssertionError("unhandled answer shape {0!r}".format(shape))


@given(
    shape=st.sampled_from(ANSWER_SHAPES),
    is_anomalous=st.booleans(),
    confidence=st.floats(0, 1, allow_nan=False, allow_infinity=False),
    prose=PROSE,
    label=st.sampled_from(["OK", "NOK", "ok", "nok", " OK ", "Nok",
                           "EXCLUDE", "", "   ", None, "maybe", 0, True]),
    error=st.one_of(
        st.none(), st.just(""), st.just("   "),
        st.sampled_from(["ThrottlingException: rate exceeded",
                         "ReadTimeoutError", "missing object key"]),
    ),
)
@settings(max_examples=100, deadline=None)
def test_property_outcome_categorization_is_total_and_exact(
        shape, is_anomalous, confidence, prose, label, error):
    """**Feature: quality-prompt-tuning, Property 8: Outcome
    categorization is total and exact.**

    For any Label in {OK, NOK}, any invocation behaviour (answer
    returned, error raised) and any answer text (valid verdict JSON,
    fenced, prose-wrapped, missing ``is_anomalous``, truncated, empty),
    ``categorize_outcome`` assigns exactly one category:
    ``invocation_error`` iff the invocation failed; else
    ``parse_failure`` iff ``parse_verdict`` rejects; else
    ``correct``/``false_pass``/``false_fail`` by comparison with the
    Label.

    **Validates: Requirements 6.5**
    """
    answer, expected_verdict = build_answer(shape, is_anomalous, confidence,
                                            prose)

    # The Verdict_Parser reaches exactly the restated verdict.
    try:
        verdict = ai.parse_verdict(answer)
    except ValueError:
        verdict = None
    assert verdict == expected_verdict, (
        "shape {0!r} parsed to {1!r}, expected {2!r}".format(
            shape, verdict, expected_verdict))

    expected = ref_category(label, verdict, error)
    if expected is RAISES:
        # A label outside {OK, NOK} that the ordering reaches is a
        # caller defect, never a silent `correct`.
        with pytest.raises(ValueError):
            ai.categorize_outcome(label, verdict, error)
        return

    category = ai.categorize_outcome(label, verdict, error)
    assert category == expected
    # Exactly one category, and it is one of the five.
    assert category in ALL_CATEGORIES
    assert sum(1 for known in ALL_CATEGORIES if known == category) == 1
    # The two "iff" clauses of Requirement 6.5, checked as equivalences.
    failed = error is not None and str(error).strip() != ""
    assert (category == CATEGORY_INVOCATION_ERROR) is failed
    assert (category == CATEGORY_PARSE_FAILURE) is (
        not failed and verdict is None)
    if category in (CATEGORY_CORRECT, CATEGORY_FALSE_PASS,
                    CATEGORY_FALSE_FAIL):
        anomalous = bool(verdict["is_anomalous"])
        expects_anomalous = str(label).strip().upper() == "NOK"
        assert (category == CATEGORY_CORRECT) is (
            anomalous == expects_anomalous)


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

#: Exactly-representable numbers, so a permuted sum is bit-identical and
#: the property is about the summary rather than float association.
REPORTED_NUMBERS = st.one_of(
    st.integers(0, 5000), st.sampled_from([0.5, 1.25, 2.75, 100.5]))

#: A reported value that is not a number (or is missing) — the outcome
#: shapes a partially-populated run produces.
UNREPORTED = st.sampled_from([None, "n/a", True, False, ""])


@st.composite
def outcome(draw):
    """One persisted Sample_Outcome, in the design's camelCase shape."""
    item = {
        "sampleId": draw(st.sampled_from(["s1", "s2", "s3"])),
        "category": draw(st.sampled_from(
            list(ALL_CATEGORIES) + ["unexpected_category"])),
    }
    if draw(st.booleans()):
        item["isAnomalous"] = draw(st.booleans())
    if draw(st.booleans()):
        item["outputTokens"] = draw(st.one_of(REPORTED_NUMBERS, UNREPORTED))
    if draw(st.booleans()):
        item["latencyMs"] = draw(st.one_of(REPORTED_NUMBERS, UNREPORTED))
    if draw(st.booleans()):
        item["repeat"] = draw(st.integers(1, 3))
    return item


@given(outcomes=st.lists(outcome(), max_size=12), seed=st.integers(0, 2 ** 16))
@settings(max_examples=100, deadline=None)
def test_property_score_summary_is_a_function_of_the_outcomes(outcomes, seed):
    """**Feature: quality-prompt-tuning, Property 9: The Score_Summary is
    a function of the persisted outcomes.**

    For any multiset of Sample_Outcomes (partial runs, cancelled runs,
    resumed runs, repeats), ``summarize_outcomes`` equals the
    definition's counts, accuracy, instability, token and latency
    statistics, identically whether computed by the Portal or the device
    and at any point in a run's life.

    **Validates: Requirements 6.7, 6.11, 6.14, 10.4**
    """
    expected = ref_summary(outcomes)
    assert ai.summarize_outcomes(outcomes) == expected

    # A function of the multiset: order must not matter (the Portal
    # reads DynamoDB pages, the device appends batches).
    shuffled = list(outcomes)
    random.Random(seed).shuffle(shuffled)
    assert ai.summarize_outcomes(shuffled) == expected

    # Any iterable, and any batching: the device summarizes batches of
    # at most 20 (modelled here at 3 so the concatenation is exercised),
    # the Portal one list.
    batches = [outcomes[index:index + 3]
               for index in range(0, len(outcomes), 3)]
    streamed = [item for batch in batches for item in batch]
    assert ai.summarize_outcomes(iter(streamed)) == expected
    assert ai.summarize_outcomes(tuple(streamed)) == expected

    # Identically on both sides: the device's outcomes travel as JSON in
    # the Sample_Store, the Portal's as items it has converted to native
    # numbers; neither representation changes the summary.
    assert ai.summarize_outcomes(json.loads(json.dumps(outcomes))) == expected

    # At any point in a run's life: every prefix (a partial, cancelled
    # or resumed run) summarizes to the definition's value for it, and
    # never to a running counter.
    for length in range(len(outcomes) + 1):
        prefix = outcomes[:length]
        assert ai.summarize_outcomes(prefix) == ref_summary(prefix)

    # Repeats of one sample never inflate the sample count, and the
    # instability count only ever names samples with >1 outcome.
    assert expected["samples"] <= expected["invocations"]
    assert expected["unstable"] <= expected["samples"]
    if expected["invocations"] == 0:
        assert expected["accuracy"] is None
        assert expected["meanOutputTokens"] is None
        assert expected["maxOutputTokens"] is None
        assert expected["meanLatencyMs"] is None
