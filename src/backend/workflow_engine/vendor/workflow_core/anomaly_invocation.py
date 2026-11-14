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
references, capturePaths, the fail-closed rules), artifact persistence,
metadata assembly and the transports themselves. The prompt renderers
(``render_prompt``, ``render_prompt_text``) and the inspection request
preparation live here, so the device and the Portal's replay of an
``answer_format: inspection`` node render and count the same way; the
reference-list parsing and the attachment blocks of a list of reference
files live here too, for the same reason.

Pure module: standard library only — no boto3, no HTTP, no filesystem,
no logging, and no imports outside this package (the inspection answer
schema is enforced by the small standard-library interpreter
``_validate_schema``, cross-checked against ``jsonschema`` by the layer
tests, so this rule still holds). Callers that need to
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


#: The ``bedrock_inference`` parameter selecting the answer contract.
ANSWER_FORMAT_PARAMETER = "answer_format"


def is_inspection_format(parameters: Mapping[str, Any]) -> bool:
    """True when ``parameters`` select ``answer_format: inspection``.

    ``str(parameters.get("answer_format") or "") == "inspection"``: the
    executor's own comparison (no strip, no case folding).
    """
    return (str(parameters.get(ANSWER_FORMAT_PARAMETER) or "")
            == ANSWER_FORMAT_INSPECTION)


def is_tunable_node(node_type: Any, anomaly_mode: Any,
                    answer_format: Any = None) -> bool:
    """True when the node is a Tunable_Node (Requirement 1.5).

    A ``bedrock_inference`` node whose ``answer_format`` is exactly
    ``inspection`` (``str(answer_format or "")``, the executor's
    comparison) is tunable whatever its ``anomaly_mode``: the executor
    exports every answered inspection run. Otherwise a Tunable_Node is an
    Inspection_Node in Anomaly_Mode, so this is :func:`is_anomaly_mode`
    under the tuning-facing name; both exist so the executor and the
    Portal each read the rule in their own terms while sharing one
    implementation (Property 1). Two-argument callers get the
    Anomaly_Mode answer, unchanged.
    """
    if (node_type == NODE_TYPE_BEDROCK_INFERENCE
            and str(answer_format or "") == ANSWER_FORMAT_INSPECTION):
        return True
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

#: The two ``answer_format`` values of a ``bedrock_inference`` node
#: (bedrock-inspection-chain). ``legacy`` (default/absent) is the Anomaly_Mode /
#: freeform behaviour above, byte for byte; ``inspection`` is the
#: structured verdict answer of :data:`INSPECTION_ANSWER_SCHEMA`.
ANSWER_FORMAT_LEGACY = "legacy"
ANSWER_FORMAT_INSPECTION = "inspection"

#: Canonical instruction appended (after ANOMALY_INSTRUCTION_SEPARATOR,
#: INSTEAD of the Verdict_Instruction) to the user prompt of an
#: ``answer_format: inspection`` node — one sentence naming the exact
#: answer shape :func:`parse_inspection_answer` accepts.
INSPECTION_JSON_INSTRUCTION = (
    "Respond with exactly one JSON object and nothing else: "
    '{"verdict": "pass"|"fail", "confidence": 0..1, "reason": "<one '
    'sentence>", "objects": [{"name": "<part>", "qc": "OK"|"NOK", '
    '"reason": "<short>", "bounding_box": {"x_min": <int>, "y_min": '
    '<int>, "x_max": <int>, "y_max": <int>}}, ...]} with one entry in '
    "objects per expected part."
)

#: Coordinate conventions of ``objects[].bounding_box`` in an inspection
#: answer; the caller resolves the node's ``auto`` by model id before
#: calling :func:`parse_inspection_answer`.
BOX_SCALE_PIXELS = "pixels"
BOX_SCALE_NORMALIZED_1000 = "normalized_1000"
BOX_SCALES = (BOX_SCALE_PIXELS, BOX_SCALE_NORMALIZED_1000)

#: Upper bound (inclusive) of a ``normalized_1000`` coordinate.
NORMALIZED_BOX_MAX = 1000

#: The structured inspection answer contract as a JSON Schema (draft-07)
#: dict: ``verdict`` is the pass|fail vocabulary, ``qc`` the per-part
#: OK|NOK drawn on the overlay, every object is closed
#: (``additionalProperties: false``); anything that does not validate is
#: a malformed answer. Enforced by :func:`_validate_schema`.
INSPECTION_ANSWER_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["verdict", "confidence", "reason", "objects"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"enum": ["pass", "fail"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "objects": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "qc", "reason", "bounding_box"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "qc": {"enum": ["OK", "NOK"]},
                    "reason": {"type": "string"},
                    "bounding_box": {
                        "type": "object",
                        "required": ["x_min", "y_min", "x_max", "y_max"],
                        "additionalProperties": False,
                        "properties": {
                            "x_min": {"type": "integer"},
                            "y_min": {"type": "integer"},
                            "x_max": {"type": "integer"},
                            "y_max": {"type": "integer"},
                        },
                    },
                },
            },
        },
    },
}

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

#: Client-side connect timeout of a Bedrock runtime invocation when the
#: node owns its attempt/budget policy (``attempts``, ``attempt_timeout_s``
#: or ``budget_s`` off their defaults): an in-Region TCP connect either
#: completes well under a second or the network is down. The default
#: single-call client is untouched by this constant.
BEDROCK_CONNECT_TIMEOUT_SEC = 2

#: Image format of every attached content block.
BEDROCK_IMAGE_FORMAT = "jpeg"

#: The ``bedrock_inference`` parameter whose JSON object is sent as the
#: Converse ``additionalModelRequestFields`` (bedrock-additional-model-
#: request-fields): request fields one model family understands and
#: another refuses, e.g. ``{"thinking": {"type": "disabled"}}`` for a
#: Claude model that thinks by default. Amazon Nova refuses that same
#: object with a ``ValidationException`` ("extraneous key [thinking] is
#: not permitted") before generating anything, so the fields are opt-in
#: per node and never defaulted or inferred from the model id.
ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER = "additional_model_request_fields"

# ---------------------------------------------------------------------------
# Reference lists: one reference per line of reference_payload_path
# ---------------------------------------------------------------------------

#: The two kinds of a reference file.
KIND_IMAGE, KIND_DOCUMENT = "image", "document"

#: Extension of a static reference's URI path -> Converse document format.
DOCUMENT_FORMATS_BY_EXTENSION = {
    ".pdf": "pdf", ".txt": "txt", ".md": "md", ".markdown": "md",
    ".csv": "csv", ".html": "html", ".htm": "html", ".doc": "doc",
    ".docx": "docx", ".xls": "xls", ".xlsx": "xlsx"}

#: Converse limits of one request.
MAX_DOCUMENT_REFERENCES = 5          # up to five documents
MAX_IMAGE_REFERENCES = 19            # up to 20 images, input included
MAX_DOCUMENT_BYTES = 4_500_000       # 4.5 MB per document
MAX_SENT_IMAGE_BYTES = 3_750_000     # 3.75 MB per image
MAX_IMAGE_SIDE_PX = 8000             # 8000 px per side

#: Label of each reference of a list form (1-based position).
LIST_REFERENCE_LABEL = "Reference {n}"

#: A file uploaded in the workflow editor and packaged with the workflow:
#: ``upload://{sha256}.{ext}``. Not one of REFERENCE_URI_SCHEMES: it is
#: resolved against the workflow package only, never fetched as a URI.
UPLOAD_REFERENCE_SCHEME = "upload://"
UPLOAD_IMAGE_EXTENSIONS = ("jpg", "png")
UPLOAD_DOCUMENT_EXTENSIONS = ("pdf", "txt", "md", "csv", "html",
                              "doc", "docx", "xls", "xlsx")
UPLOAD_LINE_RE = re.compile(
    r"^upload://([0-9a-f]{64})\.(jpg|png|pdf|txt|md|csv|html|doc|docx|xls|xlsx)$")
#: Directory of the uploaded files inside the workflow version's directory.
PACKAGED_REFERENCES_DIR = "references"
#: Key of the uploaded files' checksums in the package's manifest.json.
MANIFEST_REFERENCE_FILES_KEY = "referenceFiles"

#: The URI schemes of a static reference — the device's
#: ``payload_fetch._URI_SCHEMES`` (case-sensitive).
REFERENCE_URI_SCHEMES = ("s3://", "http://", "https://", "file://")

#: The four ``document_signature_problem`` reasons and the OLE2 magic.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"
_TEXT_DOCUMENT_FORMATS = ("txt", "md", "csv", "html")

#: The malformed-upload-line problem text (``{n}`` = the line's position).
_MALFORMED_UPLOAD_LINE = (
    "line {n} of reference_payload_path is not a valid uploaded-file "
    "reference: expected upload://<64 lowercase hex characters>."
    "<jpg|png|pdf|txt|md|csv|html|doc|docx|xls|xlsx>")


@dataclass(frozen=True)
class Attachment:
    """One labelled file of a list-form request, after the input image."""

    label: str
    kind: str       # KIND_IMAGE or KIND_DOCUMENT
    format: str     # "jpeg" for an image; the document format
    data: bytes


@dataclass(frozen=True)
class ReferenceFile:
    """One loaded reference, as :func:`build_bedrock_invocation` takes it."""

    kind: str
    format: str
    data: bytes     # an image as prepared for sending; a document unchanged


@dataclass(frozen=True)
class BedrockInvocation:
    """One Bedrock Converse request, transport-independent.

    ``images`` is the ordered ``(label, jpeg_bytes)`` sequence the
    request attaches; ``prompt`` already carries the appended
    Verdict_Instruction in Anomaly_Mode (or the inspection instruction
    when ``answer_format`` is ``inspection``); ``system_prompt`` is
    ``None`` when no system parameter is sent. ``answer_format`` tells
    the caller how to parse the answer; ``anomaly_mode`` is ``False`` in
    inspection format because the legacy verdict parser does not apply.
    ``additional_model_request_fields`` is the compact JSON text of a
    non-empty object (see :func:`resolve_additional_model_request_fields`)
    or ``None`` when the request carries no such field; kept as text so
    the invocation stays immutable and hashable.
    ``attachments`` is the list form of several reference files (the
    input image first, then every reference with its label); it is empty
    for a request with at most one reference image, which ``images``
    describes alone.
    """

    model: str
    prompt: str
    images: Tuple[Tuple[str, bytes], ...]
    region: str
    max_tokens: int
    system_prompt: Optional[str] = None
    anomaly_mode: bool = True
    answer_format: str = ANSWER_FORMAT_LEGACY
    additional_model_request_fields: Optional[str] = None
    attachments: Tuple[Attachment, ...] = ()

    @property
    def image_count(self) -> int:
        """The number of image blocks, the input image included."""
        if self.attachments:
            return sum(1 for item in self.attachments
                       if item.kind == KIND_IMAGE)
        return len(self.images)

    @property
    def document_count(self) -> int:
        """The number of document blocks."""
        return sum(1 for item in self.attachments
                   if item.kind == KIND_DOCUMENT)

    def converse_kwargs(self) -> Dict[str, Any]:
        """The exact ``client.converse(**kwargs)`` keyword arguments.

        Byte-identical to the construction in the executor's
        ``_default_bedrock_invoker``: the prompt block, then per image a
        ``"{label}:"`` text block followed by the image block;
        ``inferenceConfig={"maxTokens": ...}``; the top-level ``system``
        parameter only when a system prompt is present (so a
        system-prompt-less request stays byte-identical to the
        pre-feature invocation); ``additionalModelRequestFields`` only
        when the node configures a non-empty object (a fresh dict per
        call, so a caller mutating the kwargs never changes the
        invocation). A list form renders ``attachments`` instead of
        ``images``: per attachment the ``"{label}:"`` text block, then
        the image block or a ``document`` block named by the label.
        """
        content: List[Dict[str, Any]] = [{"text": self.prompt}]
        if self.attachments:
            for attachment in self.attachments:
                content.append({"text": "{0}:".format(attachment.label)})
                if attachment.kind == KIND_DOCUMENT:
                    content.append({
                        "document": {
                            "format": attachment.format,
                            "name": attachment.label,
                            "source": {"bytes": attachment.data},
                        },
                    })
                else:
                    content.append({
                        "image": {
                            "format": BEDROCK_IMAGE_FORMAT,
                            "source": {"bytes": attachment.data},
                        },
                    })
        else:
            for label, data in self.images:
                content.append({"text": "{0}:".format(label)})
                content.append({
                    "image": {
                        "format": BEDROCK_IMAGE_FORMAT,
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
        if self.additional_model_request_fields:
            kwargs["additionalModelRequestFields"] = json.loads(
                self.additional_model_request_fields)
        return kwargs


def resolve_additional_model_request_fields(raw: Any) -> Optional[str]:
    """Normalize a configured ``additional_model_request_fields`` value.

    Absent/``None``/empty/whitespace-only text, and an empty object, ⇒
    ``None``: the request carries no ``additionalModelRequestFields`` and
    stays byte-identical to the pre-feature request. A JSON object —
    given as its JSON text (the catalog's ``string`` parameter, as the
    designer stores it) or as an already-parsed mapping — ⇒ its compact
    JSON text (``separators=(",", ":")``, key order kept).

    Anything else (unparseable text, or JSON that is not an object) raises
    ``ValueError`` naming the parameter: the validator rejects such a
    value at save time (``BEDROCK_REQUEST_FIELDS_JSON``), so this only
    fires on a document that bypassed validation, and it fails closed
    instead of silently sending the request without the fields.
    """
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        parsed: Any = dict(raw)
    else:
        text = str(raw)
        if not text.strip():
            return None
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(
                "{0} is not valid JSON ({1})".format(
                    ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER, exc))
    if not isinstance(parsed, dict):
        raise ValueError(
            "{0} must be a JSON object, got {1}".format(
                ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER,
                type(parsed).__name__))
    if not parsed:
        return None
    try:
        return json.dumps(parsed, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:  # a mapping holding non-JSON values
        raise ValueError(
            "{0} is not JSON-serializable ({1})".format(
                ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER, exc))


def build_bedrock_invocation(
    parameters: Mapping[str, Any],
    input_image: bytes,
    reference_image: Optional[bytes] = None,
    references: Optional[Iterable[ReferenceFile]] = None,
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
    - ``answer_format`` (bedrock-inspection-chain): absent, empty or ``legacy``
      keeps every rule above byte for byte; ``inspection`` appends
      :data:`INSPECTION_JSON_INSTRUCTION` (same separator) INSTEAD of the
      Verdict_Instruction and ignores ``anomaly_mode``.
    - ``additional_model_request_fields`` (bedrock-additional-model-
      request-fields): absent/empty keeps every rule above byte for byte;
      a JSON object is sent as ``additionalModelRequestFields`` in every
      format (:func:`resolve_additional_model_request_fields`, which
      raises ``ValueError`` on anything that is not a JSON object).
    - ``references`` (:class:`ReferenceFile` items, mutually exclusive
      with ``reference_image``; passing both raises ``ValueError``):
      exactly one image reference is built exactly as
      ``reference_image=`` its bytes, with ``attachments`` empty; any
      other non-empty list is the list form: ``attachments`` holds the
      input image, then each reference labelled by
      :func:`reference_labels`, and ``images`` holds the image
      attachments as ``(label, data)`` pairs in the same order.
    """
    if reference_image is not None and references is not None:
        raise ValueError(
            "reference_image and references are mutually exclusive")
    reference_files = tuple(references or ())
    if len(reference_files) == 1 and reference_files[0].kind == KIND_IMAGE:
        reference_image = reference_files[0].data
        reference_files = ()
    answer_format = str(parameters.get("answer_format") or ANSWER_FORMAT_LEGACY)
    prompt = str(parameters.get("prompt") or "")
    if answer_format == ANSWER_FORMAT_INSPECTION:
        anomaly_mode = False
        prompt = (prompt + ANOMALY_INSTRUCTION_SEPARATOR
                  + INSPECTION_JSON_INSTRUCTION)
    else:
        anomaly_mode = is_anomaly_mode(
            NODE_TYPE_BEDROCK_INFERENCE, parameters.get("anomaly_mode"))
        if anomaly_mode:
            prompt = (prompt + ANOMALY_INSTRUCTION_SEPARATOR
                      + BEDROCK_JSON_INSTRUCTION)
    images: List[Tuple[str, bytes]] = [(INPUT_IMAGE_LABEL, input_image)]
    attachments: List[Attachment] = []
    if reference_image is not None:
        images.append((REFERENCE_IMAGE_LABEL, reference_image))
    elif reference_files:
        labels = reference_labels([item.kind for item in reference_files])
        attachments.append(Attachment(
            INPUT_IMAGE_LABEL, KIND_IMAGE, BEDROCK_IMAGE_FORMAT, input_image))
        for label, item in zip(labels, reference_files):
            attachments.append(
                Attachment(label, item.kind, item.format, item.data))
        images = [(item.label, item.data) for item in attachments
                  if item.kind == KIND_IMAGE]
    return BedrockInvocation(
        model=str(parameters.get("model") or BEDROCK_DEFAULT_MODEL),
        prompt=prompt,
        images=tuple(images),
        region=str(parameters.get("region") or BEDROCK_DEFAULT_REGION),
        max_tokens=int(parameters.get("max_tokens") or DEFAULT_MAX_TOKENS),
        system_prompt=normalize_system_prompt(parameters.get("system_prompt")),
        anomaly_mode=anomaly_mode,
        answer_format=answer_format,
        additional_model_request_fields=resolve_additional_model_request_fields(
            parameters.get(ADDITIONAL_MODEL_REQUEST_FIELDS_PARAMETER)),
        attachments=tuple(attachments),
    )


def is_static_reference(value: Any) -> bool:
    """True when ``value`` is a str whose stripped text starts with one of
    :data:`REFERENCE_URI_SCHEMES` (case-sensitive, like the device's
    ``fetch_reference_bytes``)."""
    return isinstance(value, str) and value.strip().startswith(
        REFERENCE_URI_SCHEMES)


def is_inline_reference_value(value: Any) -> bool:
    """True when ``value`` is a str that does not start with one of
    :data:`REFERENCE_URI_SCHEMES`: base64 or a data: URL. This is the
    device's ``describe_reference_source`` rule."""
    return isinstance(value, str) and not value.startswith(
        REFERENCE_URI_SCHEMES)


@dataclass(frozen=True)
class ReferenceSpec:
    """One line of a ``reference_payload_path`` list."""

    index: int      # 1-based position in the list
    text: str       # the trimmed line
    static: bool    # is_static_reference(text) or uploaded
    uploaded: bool  # text starts with UPLOAD_REFERENCE_SCHEME
    upload_name: Optional[str]   # "{sha256}.{ext}" of a well-formed upload line
    kind: str       # KIND_IMAGE or KIND_DOCUMENT
    format: str     # "jpeg" for an image; the document format
    label: str      # REFERENCE_IMAGE_LABEL or "Reference {n}"


def parse_upload_line(text: Any) -> Optional[Tuple[str, str]]:
    """``(sha256, ext)`` when ``text`` matches :data:`UPLOAD_LINE_RE`
    exactly (lowercase hex, one of the eleven extensions, nothing else);
    ``None`` otherwise."""
    if not isinstance(text, str):
        return None
    match = UPLOAD_LINE_RE.match(text)
    if match is None or match.end() != len(text):
        return None
    return match.group(1), match.group(2)


def document_format_of(text: Any) -> Optional[str]:
    """The Converse document format a reference line names, or ``None``.

    An upload line: its extension when it is one of
    :data:`UPLOAD_DOCUMENT_EXTENSIONS` (already the Converse format).
    Another static reference: the format of the extension of the URI's
    path (the part before any ``?`` or ``#``, compared
    case-insensitively), from :data:`DOCUMENT_FORMATS_BY_EXTENSION`.
    ``None`` for every other text, payload paths included.
    """
    if not isinstance(text, str):
        return None
    line = text.strip()
    upload = parse_upload_line(line)
    if upload is not None:
        return upload[1] if upload[1] in UPLOAD_DOCUMENT_EXTENSIONS else None
    if not is_static_reference(line):
        return None
    scheme = next(s for s in REFERENCE_URI_SCHEMES if line.startswith(s))
    rest = re.split(r"[?#]", line[len(scheme):], maxsplit=1)[0]
    slash = rest.find("/")
    if slash < 0:
        return None
    # The extension as posixpath.splitext reads it (leading dots of the
    # file name are not an extension), without importing posixpath.
    name = rest[slash:].rsplit("/", 1)[-1].lstrip(".")
    dot = name.rfind(".")
    extension = name[dot:].lower() if dot >= 0 else ""
    return DOCUMENT_FORMATS_BY_EXTENSION.get(extension)


def document_signature_problem(fmt: str, data: bytes) -> Optional[str]:
    """The format check of a document as a pure rule: ``None`` when
    ``data`` passes for ``fmt``, otherwise the reason — "it does not
    start with %PDF-", "it is not a ZIP archive", "it is not an OLE2
    compound file", "it is not UTF-8 text" or "it is empty". Shared by
    the device and the Portal's upload validation. An unknown ``fmt`` is
    a caller defect (``ValueError``)."""
    data = bytes(data or b"")
    if fmt == "pdf":
        return None if data.startswith(_PDF_MAGIC) else (
            "it does not start with %PDF-")
    if fmt in ("docx", "xlsx"):
        return None if data.startswith(_ZIP_MAGIC) else (
            "it is not a ZIP archive")
    if fmt in ("doc", "xls"):
        return None if data.startswith(_OLE2_MAGIC) else (
            "it is not an OLE2 compound file")
    if fmt in _TEXT_DOCUMENT_FORMATS:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return "it is not UTF-8 text"
        return None if text.strip() else "it is empty"
    raise ValueError("unsupported document format {0!r}".format(fmt))


def reference_labels(kinds: Iterable[str]) -> Tuple[str, ...]:
    """``("Reference image",)`` when ``kinds`` is exactly
    ``(KIND_IMAGE,)``; otherwise ``("Reference 1", ..., "Reference N")``."""
    kinds = tuple(kinds)
    if kinds == (KIND_IMAGE,):
        return (REFERENCE_IMAGE_LABEL,)
    return tuple(LIST_REFERENCE_LABEL.format(n=index)
                 for index in range(1, len(kinds) + 1))


def parse_reference_list(value: Any) -> Tuple[ReferenceSpec, ...]:
    """The non-blank lines of ``value`` (``\\n``, ``\\r\\n`` or ``\\r``
    separated), each stripped, in order. A line's origin: uploaded
    (starts with ``upload://``; a malformed one keeps ``uploaded`` with
    no ``upload_name`` and counts as an image), static URI (one of
    :data:`REFERENCE_URI_SCHEMES`; kind by extension), else a payload
    path (always an image). Labels from :func:`reference_labels`. A
    non-str or blank value gives ``()``."""
    if not isinstance(value, str) or not value.strip():
        return ()
    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", value)]
    lines = [line for line in lines if line]
    entries = []
    for line in lines:
        uploaded = line.startswith(UPLOAD_REFERENCE_SCHEME)
        upload = parse_upload_line(line) if uploaded else None
        fmt = document_format_of(line)
        entries.append((
            line,
            uploaded or is_static_reference(line),
            uploaded,
            "{0}.{1}".format(*upload) if upload is not None else None,
            KIND_DOCUMENT if fmt is not None else KIND_IMAGE,
            fmt if fmt is not None else BEDROCK_IMAGE_FORMAT,
        ))
    labels = reference_labels([entry[4] for entry in entries])
    return tuple(
        ReferenceSpec(index=index, text=text, static=static,
                      uploaded=uploaded, upload_name=upload_name,
                      kind=kind, format=fmt, label=label)
        for index, ((text, static, uploaded, upload_name, kind, fmt), label)
        in enumerate(zip(entries, labels), start=1))


def reference_list_problems(specs: Iterable[ReferenceSpec],
                            answer_format: Any) -> Tuple[str, ...]:
    """The statically known problems of a parsed list, in this order:
    each malformed upload line; a list form or a document on a node
    whose ``answer_format`` is not ``inspection``; more than
    :data:`MAX_DOCUMENT_REFERENCES` documents; more than
    :data:`MAX_IMAGE_REFERENCES` images. Each text is the reason the
    device records and the message the validator reports."""
    specs = tuple(specs)
    problems: List[str] = []
    for spec in specs:
        if spec.uploaded and spec.upload_name is None:
            problems.append(_MALFORMED_UPLOAD_LINE.format(n=spec.index))
    documents = sum(1 for spec in specs if spec.kind == KIND_DOCUMENT)
    images = len(specs) - documents
    if (str(answer_format or "") != ANSWER_FORMAT_INSPECTION
            and (len(specs) > 1 or documents)):
        problems.append(
            "reference_payload_path lists {0} reference(s) or a document; "
            "only answer_format inspection accepts several references or "
            "documents".format(len(specs)))
    if documents > MAX_DOCUMENT_REFERENCES:
        problems.append(
            "reference_payload_path lists {0} documents; Bedrock Converse "
            "accepts at most {1} per request".format(
                documents, MAX_DOCUMENT_REFERENCES))
    if images > MAX_IMAGE_REFERENCES:
        problems.append(
            "reference_payload_path lists {0} images; with the input image "
            "that is more than the {1} images Bedrock Converse accepts per "
            "request".format(images, MAX_IMAGE_REFERENCES + 1))
    return tuple(problems)


def uploaded_reference_names(parameters: Mapping[str, Any]) -> Tuple[str, ...]:
    """The ``"{sha256}.{ext}"`` names of the well-formed upload lines of
    ``parameters["reference_payload_path"]``, in list order, without
    duplicates; ``()`` when there are none."""
    names: List[str] = []
    for spec in parse_reference_list(parameters.get("reference_payload_path")):
        if spec.upload_name is not None and spec.upload_name not in names:
            names.append(spec.upload_name)
    return tuple(names)


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
# Inspection answer contract (bedrock-inspection-chain, answer_format: inspection)
# ---------------------------------------------------------------------------

_BOX_KEYS = ("x_min", "y_min", "x_max", "y_max")


def _single_json_object(text: str) -> Dict[str, Any]:
    """Exactly one JSON object: the whole text, or the content of the
    single fenced block. Prose around a bare object, a second object or
    a second fenced block is malformed (``ValueError``). Deliberately NOT
    :func:`parse_verdict`, which tolerates prose around the object."""
    candidate = (text or "").strip()
    fenced = _FENCED_BLOCK.findall(candidate)
    if len(fenced) > 1:
        raise ValueError("more than one fenced block")
    if len(fenced) == 1:
        candidate = fenced[0].strip()
    if not candidate:
        raise ValueError("empty answer")
    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        raise ValueError("not a single JSON object ({0})".format(exc))
    if not isinstance(parsed, dict):
        raise ValueError("JSON value is not an object")
    return parsed


def _schema_type_ok(instance: Any, type_name: str) -> bool:
    """Draft-07 type semantics for the types the answer schema uses:
    booleans are never numbers/integers, a float with no fractional part
    is an integer."""
    if type_name == "object":
        return isinstance(instance, dict)
    if type_name == "array":
        return isinstance(instance, list)
    if type_name == "string":
        return isinstance(instance, str)
    if type_name == "boolean":
        return isinstance(instance, bool)
    if type_name == "null":
        return instance is None
    if isinstance(instance, bool) or not isinstance(instance, (int, float)):
        return False
    if type_name == "integer":
        return isinstance(instance, int) or float(instance).is_integer()
    if type_name == "number":
        return True
    raise ValueError("unsupported schema type {0!r}".format(type_name))


def _validate_schema(instance: Any, schema: Mapping[str, Any], path: str) -> None:
    """Validate ``instance`` against the JSON Schema subset the inspection
    answer contract uses — ``type``, ``enum``, ``required``,
    ``properties``, ``additionalProperties: false``, ``items``,
    ``minItems``, ``minLength``, ``minimum``, ``maximum`` — with draft-07
    semantics (standard library only, so this module stays pure; the
    layer tests cross-check it against ``jsonschema``). Raises
    ``ValueError`` naming the offending path."""
    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(_schema_type_ok(instance, t) for t in types):
            raise ValueError("schema: {0} is not of type {1}".format(
                path, "/".join(types)))
    if "enum" in schema and not any(
            instance == option
            and isinstance(instance, bool) == isinstance(option, bool)
            for option in schema["enum"]):
        raise ValueError("schema: {0} is not one of {1!r}".format(
            path, list(schema["enum"])))
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                raise ValueError("schema: {0} is missing required property "
                                 "{1!r}".format(path, key))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [key for key in instance if key not in properties]
            if extra:
                raise ValueError("schema: {0} has unexpected property(ies) "
                                 "{1!r}".format(path, sorted(extra)))
        for key, subschema in properties.items():
            if key in instance:
                _validate_schema(instance[key], subschema,
                                 "{0}.{1}".format(path, key))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            raise ValueError("schema: {0} has fewer than {1} item(s)".format(
                path, schema["minItems"]))
        if "items" in schema:
            for index, element in enumerate(instance):
                _validate_schema(element, schema["items"],
                                 "{0}[{1}]".format(path, index))
    if isinstance(instance, str) and len(instance) < schema.get("minLength", 0):
        raise ValueError("schema: {0} is shorter than {1} character(s)".format(
            path, schema["minLength"]))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValueError("schema: {0} is below the minimum {1}".format(
                path, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            raise ValueError("schema: {0} is above the maximum {1}".format(
                path, schema["maximum"]))


def parse_inspection_answer(
    text: str,
    expected_count: Optional[int] = None,
    box_scale: str = BOX_SCALE_PIXELS,
) -> Dict[str, Any]:
    """Parse a structured inspection answer; any ``ValueError`` is a
    malformed answer (one failed attempt, never a pass).

    Rules, in order: exactly one JSON object (:func:`_single_json_object`);
    :data:`INSPECTION_ANSWER_SCHEMA` validation (:func:`_validate_schema`,
    draft-07 semantics); a ``pass`` with any object
    ``qc != "OK"`` is malformed; a ``pass`` carrying fewer than
    ``expected_count`` objects is malformed (a ``fail`` with fewer objects
    stays a ``fail``; extra objects are accepted); under
    ``box_scale="normalized_1000"`` every coordinate must lie in
    0..1000. A box with ``x_min >= x_max`` or ``y_min >= y_max`` does not
    invalidate the answer: its index is listed under
    ``dropped_box_indices`` so the caller skips it when drawing.

    Returns ``{"verdict", "confidence" (float), "reason", "objects" (the
    validated list, raw coordinates), "dropped_box_indices",
    "box_scale", "expected_count"}``.
    """
    if box_scale not in BOX_SCALES:
        raise ValueError("unsupported box_scale {0!r}".format(box_scale))
    answer = _single_json_object(text)
    _validate_schema(answer, INSPECTION_ANSWER_SCHEMA, "answer")
    verdict = answer["verdict"]
    objects = answer["objects"]
    if verdict == "pass":
        not_ok = [obj["name"] for obj in objects if obj["qc"] != "OK"]
        if not_ok:
            raise ValueError(
                "verdict pass with NOK object(s): {0}".format(", ".join(not_ok)))
        if expected_count is not None and len(objects) < int(expected_count):
            raise ValueError(
                "verdict pass with {0} object(s), expected at least {1}".format(
                    len(objects), int(expected_count)))
    dropped: List[int] = []
    for index, obj in enumerate(objects):
        box = obj["bounding_box"]
        if box_scale == BOX_SCALE_NORMALIZED_1000:
            for key in _BOX_KEYS:
                if not 0 <= box[key] <= NORMALIZED_BOX_MAX:
                    raise ValueError(
                        "bounding_box.{0} = {1} outside 0..{2} for "
                        "box_scale normalized_1000 (object {3!r})".format(
                            key, box[key], NORMALIZED_BOX_MAX, obj["name"]))
        if box["x_min"] >= box["x_max"] or box["y_min"] >= box["y_max"]:
            dropped.append(index)
    return {
        "verdict": verdict,
        "confidence": float(answer["confidence"]),
        "reason": answer["reason"],
        "objects": objects,
        "dropped_box_indices": dropped,
        "box_scale": box_scale,
        "expected_count": (None if expected_count is None
                           else int(expected_count)),
    }


def inspection_verdict(parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """The :func:`categorize_outcome` input of a parsed inspection answer:
    ``fail`` counts as anomalous."""
    return {
        "is_anomalous": parsed["verdict"] == "fail",
        "confidence": float(parsed["confidence"]),
        "verdict": parsed["verdict"],
    }


# ---------------------------------------------------------------------------
# Prompt rendering, moved verbatim from the device (llm_inference.py and
# output_bindings.py, which import them back under their old names)
# ---------------------------------------------------------------------------

#: A placeholder: '{' + identifier (optionally dotted) + '}'.
PLACEHOLDER_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_.]*)\}')


class UnresolvedPlaceholderError(Exception):
    """A Prompt_Template placeholder has no value in the upstream
    Inference_Metadata (Requirement 7.5)."""

    def __init__(self, name: str):
        super().__init__("unresolved placeholder {0}".format(name))
        #: The full (possibly dotted) placeholder name that failed.
        self.name = name


def _resolve(name: str, metadata: Dict[str, Any]) -> Any:
    """Resolve a possibly dotted placeholder name against nested dicts."""
    value: Any = metadata
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            raise UnresolvedPlaceholderError(name)
        value = value[part]
    return value


def render_prompt(template: str, metadata: Dict[str, Any]) -> str:
    """Render a Prompt_Template against upstream Inference_Metadata.

    Strict substitution: every ``{placeholder}`` is replaced by
    ``str(metadata[name])`` (dotted names resolve nested keys); literal
    text is preserved; ``'{{'``/``'}}'`` escape a literal brace. Raises
    :class:`UnresolvedPlaceholderError` on the first missing key
    (Requirements 7.3, 7.5).
    """
    rendered = []
    position = 0
    length = len(template)
    while position < length:
        char = template[position]
        if char == "{":
            if template.startswith("{{", position):
                rendered.append("{")
                position += 2
                continue
            match = PLACEHOLDER_RE.match(template, position)
            if match:
                rendered.append(str(_resolve(match.group(1), metadata)))
                position = match.end()
                continue
            rendered.append(char)
            position += 1
        elif char == "}":
            if template.startswith("}}", position):
                rendered.append("}")
                position += 2
                continue
            rendered.append(char)
            position += 1
        else:
            rendered.append(char)
            position += 1
    return "".join(rendered)


def resolve_field_path(document: Any, dotted_path: Any) -> Tuple[bool, Any]:
    """Resolve a dotted field path (``a.b.c``) against a parsed JSON
    document, segment by segment (Requirements 7.2, 7.3).

    - a dict is traversed by key;
    - a list is traversed by a numeric segment used as an index
      (``items.0.id``);
    - any other intermediate value, a missing key, a non-numeric or
      out-of-range index, or an empty path fails the resolution.

    Returns ``(found, value)`` so a resolved JSON ``null`` (``None``) is
    distinguishable from "not found" (Requirement 7.2 attaches resolved
    nulls; Requirement 7.3 omits unresolved keys).
    """
    if not isinstance(dotted_path, str):
        return False, None
    path = dotted_path.strip()
    if not path:
        return False, None
    current = document
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, list):
            try:
                index = int(segment)
            except ValueError:
                return False, None
            if not 0 <= index < len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


#: A ``{json:<dotted.path>}`` token of a rendered prompt — the compact
#: JSON of the run-metadata value replaces it (:func:`render_prompt_text`).
JSON_TOKEN_RE = re.compile(r"\{json:([A-Za-z_][A-Za-z0-9_.]*)\}")


def render_prompt_text(template: str, values: Dict[str, Any]) -> str:
    """Render an ``answer_format: inspection`` prompt against the run
    metadata, segment-wise so no substituted text is ever scanned again.

    The raw template is split at its ``{json:<dotted.path>}`` tokens;
    each literal segment is rendered with the LLM node's strict
    :func:`render_prompt` (``{identifier}``
    / ``{dotted.path}`` substitution, ``{{``/``}}`` → one brace, other
    braces literal); each token becomes the compact
    ``json.dumps(value, ensure_ascii=False, separators=(",", ":"))`` of
    the value :func:`resolve_field_path` finds. Raises
    :class:`UnresolvedPlaceholderError` (``name`` = the identifier, or
    ``json:<path>``) on the first value that does not resolve.
    """
    text = str(template or "")
    pieces: List[str] = []
    position = 0
    for match in JSON_TOKEN_RE.finditer(text):
        pieces.append(render_prompt(text[position:match.start()], values))
        found, value = resolve_field_path(values, match.group(1))
        if not found:
            raise UnresolvedPlaceholderError("json:" + match.group(1))
        pieces.append(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        position = match.end()
    pieces.append(render_prompt(text[position:], values))
    return "".join(pieces)


def resolve_box_scale(raw: Any, model: Any) -> str:
    """The coordinate convention of the answer's bounding boxes:
    ``auto`` (or absent) → ``normalized_1000`` when the effective model id
    contains ``amazon.nova`` (the 0-1000 grid Nova answers on), else
    ``pixels``; an explicit convention is returned unchanged."""
    value = str(raw or "auto").strip() or "auto"
    if value != "auto":
        return value
    if "amazon.nova" in str(model or "").lower():
        return BOX_SCALE_NORMALIZED_1000
    return BOX_SCALE_PIXELS


def resolve_expected_count(found: bool, value: Any) -> Optional[int]:
    """The ONE expected-count rule: a list → its length, an int (not a
    bool) → itself, and the result must be >= 1; anything else (path not
    found, other type, empty list, 0, negative) is ``None``."""
    if not found:
        return None
    if isinstance(value, list):
        count = len(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        count = value
    else:
        return None
    return count if count >= 1 else None


# ---------------------------------------------------------------------------
# Inspection request preparation, shared by the device's inspection path
# and the Portal's replay of an inspection-format sample
# ---------------------------------------------------------------------------

class InspectionRequestError(ValueError):
    """A pre-call failure of an inspection request; ``str()`` is the exact
    message the executor records."""


def inspection_expected_count(parameters: Mapping[str, Any],
                              values: Any) -> Optional[int]:
    """``expected_count_path`` resolved against ``values`` by
    :func:`resolve_field_path` + :func:`resolve_expected_count`; ``None``
    when no path is configured. Raises :class:`InspectionRequestError`
    when the path does not resolve to a positive count."""
    count_path = str(parameters.get("expected_count_path") or "").strip()
    if not count_path:
        return None
    found, value = resolve_field_path(values, count_path)
    expected_count = resolve_expected_count(found, value)
    if expected_count is None:
        raise InspectionRequestError(
            "expected count path {0} did not resolve to a positive "
            "count".format(count_path))
    return expected_count


def _render_prompt_enabled(raw: Any) -> bool:
    """``render_prompt`` as the device reads it: absent or blank is off,
    anything else is ``bool(coerce_parameter_value(raw))``."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False
    return bool(coerce_parameter_value(raw))


def render_inspection_parameters(
    parameters: Mapping[str, Any],
    values: Mapping[str, Any],
    image_size: Optional[Tuple[int, int]],
) -> Mapping[str, Any]:
    """The parameters an inspection request is built from.

    ``render_prompt`` off: the SAME mapping. On: a copy whose ``prompt``
    and (non-blank) ``system_prompt`` are :func:`render_prompt_text`
    against ``dict(values)`` plus ``{"image": {"width", "height"}}`` when
    ``image_size`` is given. Raises :class:`InspectionRequestError`
    ("unresolved prompt placeholder {name}") on the first placeholder
    that does not resolve.
    """
    if not _render_prompt_enabled(parameters.get("render_prompt")):
        return parameters
    merged = dict(values)
    if image_size is not None:
        merged["image"] = {"width": image_size[0], "height": image_size[1]}
    rendered = dict(parameters)
    try:
        rendered["prompt"] = render_prompt_text(
            str(parameters.get("prompt") or ""), merged)
        system_prompt = parameters.get("system_prompt")
        if system_prompt is not None and str(system_prompt).strip():
            rendered["system_prompt"] = render_prompt_text(
                str(system_prompt), merged)
    except UnresolvedPlaceholderError as error:
        raise InspectionRequestError(
            "unresolved prompt placeholder {0}".format(error.name))
    return rendered


@dataclass(frozen=True)
class InspectionRequest:
    """What an inspection request needs besides the images."""

    parameters: Mapping[str, Any]
    expected_count: Optional[int]
    box_scale: str


def prepare_inspection_request(
    parameters: Mapping[str, Any],
    values: Mapping[str, Any],
    image_size: Optional[Tuple[int, int]],
) -> InspectionRequest:
    """The three steps in the executor's order: the expected count, the
    rendering, the box scale (:func:`resolve_box_scale` of ``box_scale``
    and the effective model). Raises :class:`InspectionRequestError`."""
    expected_count = inspection_expected_count(parameters, values)
    rendered = render_inspection_parameters(parameters, values, image_size)
    box_scale = resolve_box_scale(
        parameters.get("box_scale"),
        parameters.get("model") or BEDROCK_DEFAULT_MODEL)
    return InspectionRequest(parameters=rendered,
                             expected_count=expected_count,
                             box_scale=box_scale)


def _placeholder_names(template: str) -> List[str]:
    """The PLACEHOLDER_RE names :func:`render_prompt` would resolve in
    ``template``, in order, skipping the ``{{`` and ``}}`` escapes."""
    names: List[str] = []
    position = 0
    length = len(template)
    while position < length:
        char = template[position]
        if char == "{":
            if template.startswith("{{", position):
                position += 2
                continue
            match = PLACEHOLDER_RE.match(template, position)
            if match:
                names.append(match.group(1))
                position = match.end()
                continue
        elif char == "}" and template.startswith("}}", position):
            position += 2
            continue
        position += 1
    return names


def prompt_field_paths(text: Any) -> Tuple[str, ...]:
    """Every dotted path ``text`` would render, read exactly as
    :func:`render_prompt_text` reads it: the :data:`JSON_TOKEN_RE`
    tokens, then the :data:`PLACEHOLDER_RE` matches of the literal
    segments, skipping ``{{`` and ``}}`` escapes. Order of first
    appearance, without duplicates."""
    text = str(text or "")
    found: List[str] = []
    position = 0
    for match in JSON_TOKEN_RE.finditer(text):
        found.extend(_placeholder_names(text[position:match.start()]))
        found.append(match.group(1))
        position = match.end()
    found.extend(_placeholder_names(text[position:]))
    unique: List[str] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return tuple(unique)


def inspection_metadata_roots(parameters: Mapping[str, Any]) -> Tuple[str, ...]:
    """Sorted top-level keys a replay needs: the first segment of every
    :func:`prompt_field_paths` entry of ``prompt`` and ``system_prompt``,
    and of ``expected_count_path``, plus ``"trigger"``; never ``"image"``
    (injected from the image size)."""
    roots = {"trigger"}
    for key in ("prompt", "system_prompt"):
        for path in prompt_field_paths(parameters.get(key)):
            roots.add(path.split(".", 1)[0])
    count_path = str(parameters.get("expected_count_path") or "").strip()
    if count_path:
        roots.add(count_path.split(".", 1)[0])
    roots.discard("image")
    return tuple(sorted(roots))


def omitted_path_conflict(paths: Iterable[str],
                          omitted: Iterable[str]) -> Optional[str]:
    """The first omitted path ``o`` that some rendered path ``p`` equals,
    lies inside (``p`` starts with ``o + "."``) or contains (``o`` starts
    with ``p + "."``); ``None`` when there is none."""
    paths = tuple(paths)
    for omitted_path in omitted:
        for path in paths:
            if (path == omitted_path
                    or path.startswith(omitted_path + ".")
                    or omitted_path.startswith(path + ".")):
                return omitted_path
    return None


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
