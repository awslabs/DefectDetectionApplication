"""Invocation_Builder: the shared anomaly-inspection request construction.

The single implementation of how a `bedrock_inference` or `llm_inference`
node's request is built and how its answer is parsed, used by all three
call paths so a Candidate's score predicts the deployed node
(quality-prompt-tuning Requirements 6.1, 6.2, 6.3, 6.4):

- the LocalServer workflow executor (`src/backend/workflow_engine/
  output_bindings.py`, through the vendored copy of this package),
- the Portal's Bedrock_Scorer (`workflow_tuning.py`),
- the device's Device_Score_Job runner (`workflow_engine/tuning/
  job_runner.py`).

It is the literal extraction of the construction and parsing that were
inline in ``BedrockInferenceProcessor._run_one`` /
``LlmInferenceProcessor._run_one`` and their default transports, so the
extraction is behaviour-neutral (Requirements 11.1, 11.2, Property 7).
What stays in the executor: image resolution (detection crops, payload
references, capturePaths, the fail-closed rules), ``render_prompt``,
artifact persistence, metadata assembly and the transports themselves.

Pure module: standard library only — no boto3, no HTTP, no filesystem,
no logging, and no imports outside this package. Callers that need to
report the parameter-substitution notices this module applies pass a
``notice_sink``; callers that need image downscaling pass a
``downscaler`` (the executor's contained Pillow helper on the device).
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

# ---------------------------------------------------------------------------
# Node types and the Anomaly_Mode / Tunable_Node rule (Requirement 1.5)
# ---------------------------------------------------------------------------

#: The two Inspection_Node types.
NODE_TYPE_BEDROCK_INFERENCE = "bedrock_inference"
NODE_TYPE_LLM_INFERENCE = "llm_inference"

#: Inspection_Node types, in catalog order.
INSPECTION_NODE_TYPES = (NODE_TYPE_BEDROCK_INFERENCE, NODE_TYPE_LLM_INFERENCE)


def coerce_parameter_value(value: Any) -> Any:
    """Normalize a parameter/tag value the way the executor does.

    Verbatim copy of ``output_bindings._coerce``: ``'true'``/``'false'``
    strings (case-insensitive, surrounding whitespace ignored) become
    booleans, numeric strings become ``int``/``float``, everything else
    is returned unchanged. The Anomaly_Mode decision below is expressed
    in terms of this function so a definition carrying ``"false"`` as a
    string is read on the device, in the Portal and in the frontend
    identically.
    """
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


def is_anomaly_mode(node_type: Any, anomaly_mode: Any) -> bool:
    """True when the node runs in Anomaly_Mode — the executor's rule.

    ``anomaly_mode`` is the node's raw ``anomaly_mode`` parameter value
    (``None`` when absent, i.e. ``parameters.get("anomaly_mode")``):

    - ``bedrock_inference``: absent/``None`` defaults to Anomaly_Mode;
      any other value is coerced and taken for its truth
      (``output_bindings``: ``True if coerced is None else
      bool(coerced)``).
    - ``llm_inference``: Anomaly_Mode only when the coerced value is
      truthy — absent/``None``/false keeps the freeform path
      (``bool(coerce(...))``).
    - any other node type: never.
    """
    coerced = coerce_parameter_value(anomaly_mode)
    if node_type == NODE_TYPE_BEDROCK_INFERENCE:
        return True if coerced is None else bool(coerced)
    if node_type == NODE_TYPE_LLM_INFERENCE:
        return bool(coerced)
    return False


def is_tunable_node(node_type: Any, anomaly_mode: Any) -> bool:
    """True when the node is a Tunable_Node (Requirement 1.5).

    A Tunable_Node is an Inspection_Node in Anomaly_Mode, so this is
    :func:`is_anomaly_mode` under the tuning-facing name; both exist so
    the executor and the Portal each read the rule in their own terms
    while sharing one implementation (Property 1).
    """
    return is_anomaly_mode(node_type, anomaly_mode)


# ---------------------------------------------------------------------------
# Bedrock (Converse API) invocations
# ---------------------------------------------------------------------------

#: Canonical JSON-format instruction appended to every Anomaly_Mode user
#: prompt — the Verdict_Instruction, the single source of truth for the
#: verdict answer contract.
BEDROCK_JSON_INSTRUCTION = (
    'Respond with JSON: {"is_anomalous": true|false, "confidence": 0..1}.'
)

#: Separator between the operator's prompt and the Verdict_Instruction.
ANOMALY_INSTRUCTION_SEPARATOR = "\n\n"

#: Default model when the binding parameter is absent (catalog default).
BEDROCK_DEFAULT_MODEL = "us.amazon.nova-lite-v1:0"

#: Default region when the binding parameter is absent.
BEDROCK_DEFAULT_REGION = "us-east-1"

#: Default token budget when the binding parameter is absent or falsy —
#: both node types' documented default.
DEFAULT_MAX_TOKENS = 256

#: Content-block labels of the two images, in the order the request
#: carries them.
INPUT_IMAGE_LABEL = "Input image"
REFERENCE_IMAGE_LABEL = "Reference image"

#: Fixed client-side read timeout of a Bedrock runtime invocation. The
#: transports own the client construction; the value is published here so
#: the executor and the Bedrock_Scorer configure it identically
#: (Requirement 6.3).
BEDROCK_READ_TIMEOUT_SEC = 30

#: Image format declared for an attached content block whose bytes are not
#: recognized by :func:`converse_image_format` (the historical default,
#: since every captured frame and Detection_Crop is JPEG-encoded).
BEDROCK_IMAGE_FORMAT = "jpeg"
#: Magic-byte signatures of the image formats the Bedrock Converse API
#: accepts, mapped to the ``format`` string it expects.
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def converse_image_format(data: bytes) -> str:
    """The Converse ``format`` string for ``data``, sniffed from its
    magic bytes and defaulting to :data:`BEDROCK_IMAGE_FORMAT`.

    Every image the binding sends used to be declared ``jpeg`` because
    every image WAS a JPEG — the captured frames and the Detection_Crop
    are encoded by the executor. A Payload_Reference, however, is
    whatever the publisher's URI serves: the IMTS design references are
    PNG, and Bedrock rejected the whole request with "The detected file
    MIME type image/png does not match the expected type image/jpeg"
    (observed on adlink-dlap-701, 2026-09-14). Declaring the true format
    keeps the reference bytes intact — re-encoding a reference to JPEG
    would add a lossy generation to the very image the model compares
    against. Living here, the sniff is shared by the executor's transport
    and the Portal's Bedrock_Scorer, so both declare the same format for
    the same bytes (quality-prompt-tuning Requirement 6.3).

    Unrecognized bytes keep the historical ``jpeg`` declaration, so a
    JPEG variant this table does not cover behaves exactly as before.
    """
    prefix = bytes(data[:12]) if data else b""
    for magic, fmt in _IMAGE_MAGIC:
        if prefix.startswith(magic):
            return fmt
    # RIFF....WEBP
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "webp"
    return BEDROCK_IMAGE_FORMAT


@dataclass(frozen=True)
class BedrockInvocation:
    """One Bedrock Converse request, transport-independent.

    ``images`` is the ordered ``(label, jpeg_bytes)`` sequence the
    request attaches; ``prompt`` already carries the appended
    Verdict_Instruction in Anomaly_Mode; ``system_prompt`` is ``None``
    when no system parameter is sent.
    """

    model: str
    prompt: str
    images: Tuple[Tuple[str, bytes], ...]
    region: str
    max_tokens: int
    system_prompt: Optional[str] = None
    anomaly_mode: bool = True

    def converse_kwargs(self) -> Dict[str, Any]:
        """The exact ``client.converse(**kwargs)`` keyword arguments.

        Byte-identical to the construction in the executor's
        ``_default_bedrock_invoker``: the prompt block, then per image a
        ``"{label}:"`` text block followed by the image block;
        ``inferenceConfig={"maxTokens": ...}``; the top-level ``system``
        parameter only when a system prompt is present (so a
        system-prompt-less request stays byte-identical to the
        pre-feature invocation).
        """
        content: List[Dict[str, Any]] = [{"text": self.prompt}]
        for label, data in self.images:
            content.append({"text": "{0}:".format(label)})
            content.append({
                "image": {
                    "format": converse_image_format(data),
                    "source": {"bytes": data},
                },
            })
        kwargs: Dict[str, Any] = dict(
            modelId=self.model,
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": int(self.max_tokens)},
        )
        if self.system_prompt:
            kwargs["system"] = [{"text": self.system_prompt}]
        return kwargs


def build_bedrock_invocation(
    parameters: Mapping[str, Any],
    input_image: bytes,
    reference_image: Optional[bytes] = None,
) -> BedrockInvocation:
    """Build a :class:`BedrockInvocation` from a node's parameters.

    ``parameters`` is the Inspection_Node's parameter mapping —
    Node_Parameters (``model``, ``region``) and the Prompt_Set
    (``prompt``, ``system_prompt``, ``max_tokens``) — as the compiled
    binding carries them; the Portal merges a Candidate's Prompt_Set over
    the node's parameters before calling.

    The executor's rules, unchanged:

    - Anomaly_Mode appends the Verdict_Instruction to the USER prompt
      only, separated by a blank line; freeform sends the prompt as-is.
    - an absent/empty/whitespace-only ``system_prompt`` is ``None``; a
      non-empty one is passed VERBATIM (never stripped, never rendered).
    - ``model``/``region`` fall back to the catalog defaults on any falsy
      value; ``max_tokens`` is ``int(value or 256)``.
    - the input image is always the first attachment, labelled
      "Input image"; the reference image, when present, follows labelled
      "Reference image". A ``None`` reference means single-image
      inference.
    """
    anomaly_mode = is_anomaly_mode(
        NODE_TYPE_BEDROCK_INFERENCE, parameters.get("anomaly_mode"))
    prompt = str(parameters.get("prompt") or "")
    if anomaly_mode:
        prompt = (prompt + ANOMALY_INSTRUCTION_SEPARATOR
                  + BEDROCK_JSON_INSTRUCTION)
    images: List[Tuple[str, bytes]] = [(INPUT_IMAGE_LABEL, input_image)]
    if reference_image is not None:
        images.append((REFERENCE_IMAGE_LABEL, reference_image))
    return BedrockInvocation(
        model=str(parameters.get("model") or BEDROCK_DEFAULT_MODEL),
        prompt=prompt,
        images=tuple(images),
        region=str(parameters.get("region") or BEDROCK_DEFAULT_REGION),
        max_tokens=int(parameters.get("max_tokens") or DEFAULT_MAX_TOKENS),
        system_prompt=normalize_system_prompt(parameters.get("system_prompt")),
        anomaly_mode=anomaly_mode,
    )


def normalize_system_prompt(raw: Any) -> Optional[str]:
    """Normalize a configured ``system_prompt`` the executor's way.

    Absent/``None``/empty/whitespace-only ⇒ ``None`` (no system
    parameter/field is sent at all). Anything else ⇒ ``str(raw)``
    VERBATIM — not stripped — so the operator's text reaches the model
    unmodified. Both node types share this rule.
    """
    if raw is None:
        return None
    text = str(raw)
    return text if text.strip() else None


# ---------------------------------------------------------------------------
# Text_Generation_API (device-local vLLM) invocations
# ---------------------------------------------------------------------------

#: Generation parameters forwarded from the binding to the API body, in
#: the order the body carries them.
LLM_GENERATION_PARAMETERS = ("max_tokens", "temperature", "top_p")

#: The executor's capture port names, passed to an injected downscaler so
#: it can name the frame it failed on.
LLM_INPUT_PORT = "in"
LLM_REFERENCE_PORT = "reference"


def resolve_output_token_budget(raw: Any) -> Tuple[int, Optional[str]]:
    """Resolve a configured ``max_tokens`` into the Output_Token_Budget:
    ``(budget, substitution_notice)``.

    Verbatim the executor's rule: valid = an integral number >= 1 (bool
    excluded; integral floats accepted as their int value) ⇒
    ``(value, None)``; absent (``None``) ⇒
    ``(DEFAULT_MAX_TOKENS, None)``; anything else ⇒
    ``(DEFAULT_MAX_TOKENS, notice)`` with the notice naming the rejected
    value, so a previously-invalid value generates with the documented
    default instead of failing the node.
    """
    if raw is None:
        return DEFAULT_MAX_TOKENS, None
    if not isinstance(raw, bool):
        if isinstance(raw, int) and raw >= 1:
            return raw, None
        # ``is_integer()`` is False for inf/nan, so ``int(raw)`` below
        # never overflows.
        if isinstance(raw, float) and raw.is_integer() and raw >= 1:
            return int(raw), None
    notice = (
        "invalid max_tokens value {0!r} (expected an integral number "
        ">= 1); substituting the default Output_Token_Budget of "
        "{1} tokens".format(raw, DEFAULT_MAX_TOKENS)
    )
    return DEFAULT_MAX_TOKENS, notice


def resolve_max_image_dimension(
    raw: Any,
) -> Tuple[Optional[int], Optional[str]]:
    """Resolve a configured ``max_image_dimension`` into the downscaling
    bound: ``(max_dim, invalid_notice)``.

    Verbatim the executor's rule: absent (``None``) ⇒ ``(None, None)``
    (unconfigured, silent); valid = an integral number >= 1 (bool
    excluded; integral floats accepted) ⇒ ``(value, None)``; anything
    else ⇒ ``(None, notice)`` naming the rejected value — treated as
    unconfigured, the caller emitting the notice as a warning.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, bool):
        if isinstance(raw, int) and raw >= 1:
            return raw, None
        if isinstance(raw, float) and raw.is_integer() and raw >= 1:
            return int(raw), None
    notice = (
        "invalid max_image_dimension value {0!r} (expected an integral "
        "number >= 1); treating the image downscaling option as "
        "unconfigured and sending captured frames unmodified".format(raw)
    )
    return None, notice


@dataclass(frozen=True)
class LlmInvocation:
    """One Text_Generation_API generate request, transport-independent.

    ``prompt`` is the RENDERED prompt, already carrying the appended
    Verdict_Instruction in Anomaly_Mode. ``generation`` holds only the
    generation parameters the body forwards, with ``max_tokens`` already
    resolved to the Output_Token_Budget. ``image_b64`` /
    ``reference_b64`` are the base64-encoded JPEGs actually sent (after
    any downscaling); a reference is only ever carried beside an input
    image (the API's reference-requires-image rule, which the executor
    enforces by selecting the 3-argument invocation when it has no input
    frame).
    """

    model_name: str
    prompt: str
    generation: Dict[str, Any] = field(default_factory=dict)
    image_b64: Optional[str] = None
    reference_b64: Optional[str] = None
    system_prompt: Optional[str] = None
    anomaly_mode: bool = False

    def request_body(self) -> Dict[str, Any]:
        """The exact Text_Generation_API JSON body.

        Byte-identical to the construction in the executor's
        ``_default_llm_invoker``: ``prompt``, then each present
        generation parameter in :data:`LLM_GENERATION_PARAMETERS` order,
        then ``image``/``reference_image`` when images ride along, then
        ``system_prompt`` when configured — every optional field omitted
        otherwise, so a text-only request stays byte-identical to the
        pre-feature body.
        """
        body: Dict[str, Any] = {"prompt": self.prompt}
        for key in LLM_GENERATION_PARAMETERS:
            value = self.generation.get(key)
            if value is not None:
                body[key] = value
        if self.image_b64 is not None:
            body["image"] = self.image_b64
        if self.reference_b64 is not None:
            body["reference_image"] = self.reference_b64
        if self.system_prompt:
            body["system_prompt"] = self.system_prompt
        return body


def build_llm_invocation(
    parameters: Mapping[str, Any],
    rendered_prompt: str,
    input_image: Optional[bytes] = None,
    reference_image: Optional[bytes] = None,
    downscaler: Optional[Callable[[bytes, int, str], bytes]] = None,
    notice_sink: Optional[Callable[[str], None]] = None,
) -> LlmInvocation:
    """Build an :class:`LlmInvocation` from a node's parameters.

    ``rendered_prompt`` is the node's ``prompt_template`` already
    rendered against the run/sample metadata by the caller (the module
    never renders templates or invents metadata). ``input_image`` /
    ``reference_image`` are the raw frame bytes, or ``None`` when the
    port is unfed.

    The executor's rules, unchanged:

    - Anomaly_Mode (``anomaly_mode`` truthy — never the default for this
      node type) appends the Verdict_Instruction to the rendered prompt;
      the system prompt is never touched.
    - ``max_image_dimension`` is resolved once; when configured and a
      ``downscaler`` is supplied, both images are downscaled BEFORE
      base64 encoding. The downscaler is called as
      ``downscaler(data, max_dim, port)`` with :data:`LLM_INPUT_PORT` /
      :data:`LLM_REFERENCE_PORT` and owns its own containment (the
      executor's helper logs and returns the original bytes on failure).
      Without a downscaler the frames are sent unmodified.
    - ``max_tokens`` is resolved to the Output_Token_Budget; every
      invocation therefore carries an explicit budget.
    - a reference image is only carried when an input image is present.

    Both resolution notices (image dimension first, then token budget —
    the executor's order) are reported to ``notice_sink`` when supplied;
    the module itself never logs.
    """
    anomaly_mode = is_anomaly_mode(
        NODE_TYPE_LLM_INFERENCE, parameters.get("anomaly_mode"))
    prompt = str(rendered_prompt or "")
    if anomaly_mode:
        prompt = (prompt + ANOMALY_INSTRUCTION_SEPARATOR
                  + BEDROCK_JSON_INSTRUCTION)

    max_image_dimension, dimension_notice = resolve_max_image_dimension(
        parameters.get("max_image_dimension"))
    if dimension_notice is not None and notice_sink is not None:
        notice_sink(dimension_notice)

    def _encode(data: Optional[bytes], port: str) -> Optional[str]:
        if data is None:
            return None
        if max_image_dimension is not None and downscaler is not None:
            data = downscaler(data, max_image_dimension, port)
        return base64.b64encode(data).decode("ascii")

    image_b64 = _encode(input_image, LLM_INPUT_PORT)
    # Reference-requires-image: with no input frame the executor issues
    # the 3-argument invocation, which carries no reference either.
    reference_b64 = (
        _encode(reference_image, LLM_REFERENCE_PORT)
        if image_b64 is not None else None
    )

    budget, budget_notice = resolve_output_token_budget(
        parameters.get("max_tokens"))
    if budget_notice is not None and notice_sink is not None:
        notice_sink(budget_notice)

    generation: Dict[str, Any] = {}
    for key in LLM_GENERATION_PARAMETERS:
        value = budget if key == "max_tokens" else parameters.get(key)
        if value is not None:
            generation[key] = value

    return LlmInvocation(
        model_name=str(parameters.get("modelName") or ""),
        prompt=prompt,
        generation=generation,
        image_b64=image_b64,
        reference_b64=reference_b64,
        system_prompt=normalize_system_prompt(parameters.get("system_prompt")),
        anomaly_mode=anomaly_mode,
    )


# ---------------------------------------------------------------------------
# Verdict_Parser
# ---------------------------------------------------------------------------

_FENCED_BLOCK = re.compile(r"```[A-Za-z0-9_-]*\s*(.*?)```", re.DOTALL)


def parse_verdict(text: str) -> Dict[str, Any]:
    """Parse a model answer into ``{is_anomalous, confidence}``.

    The Verdict_Parser, moved verbatim from
    ``output_bindings.parse_bedrock_answer`` (which remains as an alias
    for existing importers). Tolerates fenced code blocks (``` /
    ```json) and surrounding prose: the first JSON object carrying
    ``is_anomalous`` wins. A non-numeric/boolean ``confidence`` degrades
    to ``0.0``. Raises ``ValueError`` — carrying an excerpt of the
    answer — when no such object can be extracted.
    """
    candidates = [match.group(1) for match in _FENCED_BLOCK.finditer(text or "")]
    candidates.append(text or "")
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if not isinstance(parsed, dict) or "is_anomalous" not in parsed:
            continue
        is_anomalous = coerce_parameter_value(parsed.get("is_anomalous"))
        confidence = coerce_parameter_value(parsed.get("confidence", 0.0))
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = 0.0
        return {
            "is_anomalous": bool(is_anomalous),
            "confidence": float(confidence),
        }
    raise ValueError(
        "Bedrock response did not contain the expected JSON object "
        "{{\"is_anomalous\": ..., \"confidence\": ...}}: {0!r}".format(
            (text or "")[:200]))


# ---------------------------------------------------------------------------
# Prompt_Set fingerprint
# ---------------------------------------------------------------------------

#: Prefix of a Prompt_Set fingerprint, so exported sidecars and indexed
#: samples carry a self-describing digest.
PROMPT_FINGERPRINT_PREFIX = "sha256:"


def prompt_fingerprint(prompt_set: Mapping[str, Any]) -> str:
    """``"sha256:<hex>"`` over the canonical JSON of a Prompt_Set.

    The Prompt_Set is the tunable content of an Inspection_Node:
    ``prompt`` (``prompt_template`` for ``llm_inference`` — either key is
    accepted, ``prompt`` first), ``system_prompt`` and ``max_tokens``.
    Values are normalized exactly as an invocation normalizes them (the
    system prompt through :func:`normalize_system_prompt`, ``max_tokens``
    through :func:`coerce_parameter_value`) so two Prompt_Sets that
    produce the same request have the same fingerprint, and any change
    to prompt text, system text or token budget changes it. Nothing else
    about the node (model, region, images) participates.
    """
    prompt = prompt_set.get("prompt")
    if prompt is None:
        prompt = prompt_set.get("prompt_template")
    payload = {
        "prompt": str(prompt or ""),
        "system_prompt": normalize_system_prompt(
            prompt_set.get("system_prompt")),
        "max_tokens": coerce_parameter_value(prompt_set.get("max_tokens")),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PROMPT_FINGERPRINT_PREFIX + digest


# ---------------------------------------------------------------------------
# Sample_Outcome categorization and the Score_Summary
# ---------------------------------------------------------------------------

#: Labels a Workflow_Author may give a Tuning_Sample.
LABEL_OK = "OK"
LABEL_NOK = "NOK"
LABEL_EXCLUDE = "EXCLUDE"

#: Sample_Outcome categories (Requirement 6.5).
CATEGORY_CORRECT = "correct"
CATEGORY_FALSE_PASS = "false_pass"
CATEGORY_FALSE_FAIL = "false_fail"
CATEGORY_PARSE_FAILURE = "parse_failure"
CATEGORY_INVOCATION_ERROR = "invocation_error"

#: Every category, in reporting order.
OUTCOME_CATEGORIES = (
    CATEGORY_CORRECT,
    CATEGORY_FALSE_PASS,
    CATEGORY_FALSE_FAIL,
    CATEGORY_PARSE_FAILURE,
    CATEGORY_INVOCATION_ERROR,
)

#: Score_Summary key per category.
_CATEGORY_SUMMARY_KEYS = {
    CATEGORY_CORRECT: "correct",
    CATEGORY_FALSE_PASS: "falsePass",
    CATEGORY_FALSE_FAIL: "falseFail",
    CATEGORY_PARSE_FAILURE: "parseFailure",
    CATEGORY_INVOCATION_ERROR: "invocationError",
}


def categorize_outcome(
    label: Any,
    verdict: Optional[Mapping[str, Any]],
    error: Optional[str] = None,
) -> str:
    """Categorize one replay of one Candidate on one Tuning_Sample.

    Exactly one category, decided in this order (Requirement 6.5,
    Property 8):

    1. ``invocation_error`` when the invocation failed — ``error`` is a
       non-blank message;
    2. ``parse_failure`` when the Verdict_Parser rejected the answer —
       ``verdict`` is ``None``;
    3. otherwise the comparison with the Label: ``false_pass`` when the
       Label is NOK and the verdict is not anomalous, ``false_fail``
       when the Label is OK and the verdict is anomalous, ``correct``
       when they agree.

    ``label`` is matched case-insensitively after stripping. A label
    outside {OK, NOK} raises ``ValueError``: only labelled,
    non-excluded samples ever enter a Score_Run (Requirement 4.3), so
    anything else is a caller defect that must not be silently counted
    as correct.
    """
    if error is not None and str(error).strip():
        return CATEGORY_INVOCATION_ERROR
    if verdict is None:
        return CATEGORY_PARSE_FAILURE
    normalized = str(label).strip().upper() if label is not None else ""
    if normalized not in (LABEL_OK, LABEL_NOK):
        raise ValueError(
            "cannot categorize an outcome for label {0!r}: only {1} and "
            "{2} samples are scored".format(label, LABEL_OK, LABEL_NOK))
    is_anomalous = bool(verdict.get("is_anomalous"))
    if normalized == LABEL_NOK:
        return CATEGORY_CORRECT if is_anomalous else CATEGORY_FALSE_PASS
    return CATEGORY_FALSE_FAIL if is_anomalous else CATEGORY_CORRECT


def _numeric(value: Any) -> Optional[Any]:
    """The value unchanged when it is a real number, else ``None``.

    ``bool`` is not a number here (a reported ``True`` token count is a
    defect, not the count 1), and non-numeric values are treated as "not
    reported" so a partially-populated outcome never breaks the summary.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def summarize_outcomes(
    outcomes: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The Score_Summary of a multiset of Sample_Outcomes.

    A pure function of the persisted outcomes — never a running counter —
    so a partial, resumed or cancelled run summarizes identically
    whether the Portal or the device computes it (Requirements 6.7,
    6.11, 6.14, 10.4, Property 9).

    Each outcome is a mapping with the persisted camelCase keys:
    ``sampleId``, ``category``, ``isAnomalous``, ``outputTokens``,
    ``latencyMs``. Returned:

    ``samples``
        distinct ``sampleId`` values with at least one outcome.
    ``invocations``
        the number of outcomes.
    ``correct``/``falsePass``/``falseFail``/``parseFailure``/
    ``invocationError``
        counts per category (an unrecognized category counts towards
        ``invocations`` only).
    ``accuracy``
        ``correct / invocations``, ``None`` when there are no outcomes.
    ``unstable``
        samples whose outcomes disagree: each outcome contributes its
        parsed ``isAnomalous`` boolean, or — when it has no verdict —
        its category as a value of its own (so ``parse_failure`` and
        ``invocation_error`` each disagree with a verdict and agree with
        themselves).
    ``meanOutputTokens``/``maxOutputTokens``
        over the outcomes reporting a token count (``max`` keeping the
        reported value's type), ``None`` when none do.
    ``meanLatencyMs``
        over the outcomes reporting a latency, ``None`` when none do.
    """
    invocations = 0
    counts = {key: 0 for key in _CATEGORY_SUMMARY_KEYS.values()}
    sample_ids: List[Any] = []
    seen_samples = set()
    verdicts: Dict[Any, set] = {}
    tokens: List[Any] = []
    latencies: List[Any] = []

    for outcome in outcomes:
        invocations += 1
        category = outcome.get("category")
        summary_key = _CATEGORY_SUMMARY_KEYS.get(category)
        if summary_key is not None:
            counts[summary_key] += 1
        sample_id = outcome.get("sampleId")
        if sample_id not in seen_samples:
            seen_samples.add(sample_id)
            sample_ids.append(sample_id)
        if category in (CATEGORY_PARSE_FAILURE, CATEGORY_INVOCATION_ERROR):
            value: Any = category
        else:
            value = bool(outcome.get("isAnomalous"))
        verdicts.setdefault(sample_id, set()).add(value)
        token_count = _numeric(outcome.get("outputTokens"))
        if token_count is not None:
            tokens.append(token_count)
        latency = _numeric(outcome.get("latencyMs"))
        if latency is not None:
            latencies.append(latency)

    summary: Dict[str, Any] = {
        "samples": len(sample_ids),
        "invocations": invocations,
    }
    summary.update(counts)
    summary["accuracy"] = (
        counts["correct"] / float(invocations) if invocations else None)
    summary["unstable"] = sum(
        1 for values in verdicts.values() if len(values) > 1)
    summary["meanOutputTokens"] = (
        sum(tokens) / float(len(tokens)) if tokens else None)
    summary["maxOutputTokens"] = max(tokens) if tokens else None
    summary["meanLatencyMs"] = (
        sum(latencies) / float(len(latencies)) if latencies else None)
    return summary
