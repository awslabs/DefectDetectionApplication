"""Pure logic for the synthetic defect data generation feature.

This module deliberately contains NO AWS imports: every function here is
side-effect free so the correctness properties from the design document
(placeholder resolution, generation planning, approval filtering, bounding
box derivation, manifest record building / appending) can be property-tested
with Hypothesis without moto or mocks.

The Lambda handler (`synthetic_data.py`) provides the I/O around these
functions: DynamoDB persistence, Bedrock invocation, and the ETag-conditional
S3 manifest write that gives `append_manifest_lines` its atomicity.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SyntheticCoreError(Exception):
    """Base class for synthetic_core errors."""


class ValidationError(SyntheticCoreError):
    """A user-facing validation failure; the message identifies the
    violated condition."""


class StabilityGenerationError(SyntheticCoreError):
    """Stability response carried no usable image; the message includes
    the finish reason reported by the model (Req 2.6)."""


class UnresolvedPlaceholderError(SyntheticCoreError):
    """Raised when a prompt template references placeholder variables that
    are missing from the resolution context (Req 2.6).

    ``names`` lists EVERY missing placeholder name, sorted, deduplicated.
    """

    def __init__(self, names):
        self.names = sorted(set(names))
        super().__init__(
            "Unresolved placeholder variable(s) in prompt template: "
            + ", ".join(self.names)
        )


# ---------------------------------------------------------------------------
# Model catalog (Req 1.1, 1.3)
# ---------------------------------------------------------------------------

MODEL_CATALOG = [
    {
        "model_id": "amazon.nova-canvas-v1:0",
        "display_name": "Amazon Nova Canvas",
        "capabilities": {
            "text_to_image": True,
            "inpainting": True,
            "image_variation": True,
            "seed": True,
            "cfg_scale": True,
        },
        "max_images_per_call": 1,
        "randomization_defaults": {"seed": None, "cfg_scale": 6.5},
    },
    {
        "model_id": "amazon.titan-image-generator-v2:0",
        "display_name": "Amazon Titan Image Generator v2",
        "capabilities": {
            "text_to_image": True,
            "inpainting": True,
            "image_variation": True,
            "seed": True,
            "cfg_scale": True,
        },
        "max_images_per_call": 1,
        "randomization_defaults": {"seed": None, "cfg_scale": 8.0},
    },
    {
        "model_id": "stability.stable-image-inpaint-v1:0",
        "invocation_id": "us.stability.stable-image-inpaint-v1:0",
        "display_name": "Stability Stable Image Inpaint",
        "capabilities": {
            "text_to_image": False,
            "inpainting": True,
            "image_variation": False,
            "seed": True,
            "cfg_scale": False,
        },
        "max_images_per_call": 1,
        "randomization_defaults": {"seed": None},
    },
]


def invocation_model_id(entry):
    """The identifier to pass to Bedrock InvokeModel (Req 4.2, 4.4):
    the entry's inference-profile ``invocation_id`` when present,
    otherwise the bare ``model_id``."""
    return entry.get("invocation_id") or entry["model_id"]


def filter_available_models(catalog, available_models):
    """Catalog entries whose bare model_id appears in available_models
    with lifecycle status ACTIVE, preserving catalog order (Req 1.3,
    5.1, 5.2, 6.2).

    ``available_models``: iterable of ``{"model_id", "lifecycle_status"}``
    summaries. Matching uses the entry's bare ``model_id``, never
    ``invocation_id`` (Req 4.3).
    """
    active = {m["model_id"] for m in available_models
              if m.get("lifecycle_status") == "ACTIVE"}
    return [entry for entry in catalog if entry["model_id"] in active]


# ---------------------------------------------------------------------------
# Prompt templates and placeholder resolution (Req 2.3, 2.5, 2.6)
# ---------------------------------------------------------------------------

DEFAULT_PROMPT_TEMPLATE = (
    "Photorealistic industrial inspection image of a {object_type} "
    "exhibiting a {defect_type}. The {defect_type} must look physically "
    "plausible with realistic texture, lighting and scale. Preserve the "
    "original background, camera angle and part appearance."
)

# Placeholder grammar: {identifier} where identifier = [A-Za-z_][A-Za-z0-9_]*
# Literal braces are escaped as {{ and }}. The alternation order matters:
# escapes are consumed before a placeholder can start.
_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def extract_placeholders(template):
    """The placeholder names referenced by ``template``, in order of first
    appearance (duplicates removed). ``{{``/``}}`` escapes are skipped."""
    seen = []
    for match in _TOKEN_RE.finditer(template):
        name = match.group(1)
        if name is not None and name not in seen:
            seen.append(name)
    return seen


def resolve_prompt(template, context):
    """Substitute every ``{name}`` placeholder in ``template`` from
    ``context`` (Req 2.5).

    ``{{`` and ``}}`` resolve to literal ``{`` and ``}``. Raises
    :class:`UnresolvedPlaceholderError` listing every placeholder name
    missing from ``context`` (Req 2.6). Brace sequences that are neither an
    escape nor a valid ``{identifier}`` placeholder are left as literals.
    """
    missing = [n for n in extract_placeholders(template) if n not in context]
    if missing:
        raise UnresolvedPlaceholderError(missing)

    def _substitute(match):
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return str(context[match.group(1)])

    return _TOKEN_RE.sub(_substitute, template)


# ---------------------------------------------------------------------------
# Generation request validation (Req 3.2, 3.3, 3.6, 4.1, 4.4)
# ---------------------------------------------------------------------------

VARIATION_COUNT_MIN = 1
VARIATION_COUNT_MAX = 20

VALID_SOURCE_CLASSES = ("defect", "normal")


def validate_variation_count(value):
    """Return ``value`` iff it is an integer between 1 and 20 inclusive
    (Req 4.1); otherwise raise :class:`ValidationError` reporting the valid
    range (Req 4.4). Booleans, floats, strings, and out-of-range integers
    are all rejected."""
    if (isinstance(value, bool) or not isinstance(value, int)
            or not (VARIATION_COUNT_MIN <= value <= VARIATION_COUNT_MAX)):
        raise ValidationError(
            f"Variation count must be an integer between "
            f"{VARIATION_COUNT_MIN} and {VARIATION_COUNT_MAX} inclusive"
        )
    return value


def validate_generation_request(source_images, source_class, defect_type,
                                variation_count):
    """Validate a generation request; each rejection identifies the violated
    condition (Req 3.2, 3.3, 3.6, 4.1, 4.4).

    Returns the validated request as a dict on success.
    """
    if not source_images:
        raise ValidationError(
            "At least one source image is required to start generation"
        )
    if source_class not in VALID_SOURCE_CLASSES:
        raise ValidationError(
            "Source images must be classified as 'defect' or 'normal'"
        )
    if source_class == "normal" and (
            not isinstance(defect_type, str) or not defect_type.strip()):
        raise ValidationError(
            "A defect type to synthesize is required when source images "
            "are classified as normal"
        )
    count = validate_variation_count(variation_count)
    return {
        "source_images": list(source_images),
        "source_class": source_class,
        "defect_type": defect_type,
        "variation_count": count,
    }


# ---------------------------------------------------------------------------
# Generation planning (Req 1.2, 4.2)
# ---------------------------------------------------------------------------

# Deterministic per-task seed derivation stays inside the most restrictive
# model seed domain (Nova Canvas accepts 0..858,993,459).
SEED_MODULUS = 858_993_460


def derive_task_seed(base_seed, task_index):
    """Deterministic per-task seed derived from the base seed."""
    return (int(base_seed) + int(task_index)) % SEED_MODULUS


def build_generation_plan(session_meta, source_images, variation_count,
                          resolved_prompt, params=None):
    """Build the full generation plan: exactly
    ``len(source_images) * variation_count`` tasks, ``variation_count`` per
    source image, each carrying the session's selected model id, the
    resolved prompt text, and a deterministic per-task seed (Req 1.2, 4.2).
    """
    params = dict(params or {})
    base_seed = params.pop("seed", None)
    if base_seed is None:
        base_seed = 0
    model_id = session_meta["generation_model_id"]

    tasks = []
    task_index = 0
    for source_index, source_image in enumerate(source_images):
        for variation_index in range(variation_count):
            tasks.append({
                "task_index": task_index,
                "source_index": source_index,
                "source_image": source_image,
                "variation_index": variation_index,
                "model_id": model_id,
                "resolved_prompt": resolved_prompt,
                "seed": derive_task_seed(base_seed, task_index),
                "params": dict(params),
            })
            task_index += 1
    return tasks


# ---------------------------------------------------------------------------
# Generation method selection (Req 3.1, 3.5)
# ---------------------------------------------------------------------------

def select_generation_method(source_class, capabilities):
    """'inpainting' for normal sources on inpainting-capable models
    (Req 3.1); 'image_variation' otherwise -- but raises
    :class:`ValidationError` naming the missing capability when the
    required method is unsupported (Req 3.5)."""
    if source_class == "normal" and capabilities.get("inpainting"):
        return "inpainting"
    if capabilities.get("image_variation"):
        return "image_variation"
    raise ValidationError(
        "The selected model does not support image variation, which is "
        "required for this source classification"
    )


# ---------------------------------------------------------------------------
# Mask rectangle derivation (Req 3.2, 3.3)
# ---------------------------------------------------------------------------

_MASK_SIDE_FRACTION_MIN = 0.15
_MASK_SIDE_FRACTION_MAX = 0.40
_MASK_MARGIN_FRACTION = 0.10

_UINT64_MASK = 0xFFFFFFFFFFFFFFFF
_SPLITMIX64_GAMMA = 0x9E3779B97F4A7C15


def _mix64(seed, salt):
    """Splitmix64-style integer mixer: deterministic across Python
    versions and processes (no ``random`` module)."""
    z = (int(seed) + (int(salt) + 1) * _SPLITMIX64_GAMMA) & _UINT64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _UINT64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _UINT64_MASK
    return (z ^ (z >> 31)) & _UINT64_MASK


def _mask_side(mixed, dimension):
    """Side length in the clamped 15-40% band of ``dimension``
    (min 1 px, max the full dimension)."""
    fraction = _MASK_SIDE_FRACTION_MIN + (mixed % 10_000) / 10_000 * (
        _MASK_SIDE_FRACTION_MAX - _MASK_SIDE_FRACTION_MIN)
    return min(max(int(round(dimension * fraction)), 1), dimension)


def _mask_offset(mixed, dimension, side):
    """Center-biased placement offset: uniform within a 10% margin band
    when the rectangle fits inside it, otherwise uniform over the full
    valid placement range."""
    margin = int(round(dimension * _MASK_MARGIN_FRACTION))
    low, high = margin, dimension - side - margin
    if low > high:
        low, high = 0, dimension - side
    return low + mixed % (high - low + 1)


def derive_mask_rect(task_seed, image_width, image_height):
    """Deterministic inpainting rectangle ``{left, top, width, height}``
    derived from the Task_Seed (Req 3.3).

    - width is 15-40% of image_width (min 1 px); height likewise.
    - Placement is center-biased: a 10% margin is kept on each side when
      the rectangle fits inside it, otherwise the full valid placement
      range is used.
    - The rectangle always lies fully within the image; degenerate 1x1
      images produce the 1x1 rectangle at the origin.
    """
    width = _mask_side(_mix64(task_seed, 0), image_width)
    height = _mask_side(_mix64(task_seed, 1), image_height)
    left = _mask_offset(_mix64(task_seed, 2), image_width, width)
    top = _mask_offset(_mix64(task_seed, 3), image_height, height)
    return {"left": left, "top": top, "width": width, "height": height}


# ---------------------------------------------------------------------------
# Bedrock request adapters (Req 2.1-2.4, 7.2, 8.1)
# ---------------------------------------------------------------------------

def build_amazon_request_body(model_entry: Dict, method: str, prompt: str,
                              source_b64: str, seed, params: Dict,
                              mask_prompt: Optional[str]) -> Dict:
    """Bedrock invoke_model JSON body for one task (numberOfImages: 1)."""
    capabilities = model_entry.get('capabilities', {})
    config: Dict[str, Any] = {'numberOfImages': 1}
    if seed is not None and capabilities.get('seed'):
        config['seed'] = int(seed)
    cfg_scale = params.get('cfg_scale')
    if cfg_scale is None:
        cfg_scale = model_entry.get('randomization_defaults', {}).get(
            'cfg_scale')
    if cfg_scale is not None and capabilities.get('cfg_scale'):
        config['cfgScale'] = float(cfg_scale)

    if method == 'inpainting':
        return {
            'taskType': 'INPAINTING',
            'inPaintingParams': {
                'image': source_b64,
                'maskPrompt': mask_prompt or 'the defect region',
                'text': prompt,
            },
            'imageGenerationConfig': config,
        }
    return {
        'taskType': 'IMAGE_VARIATION',
        'imageVariationParams': {
            'images': [source_b64],
            'text': prompt,
        },
        'imageGenerationConfig': config,
    }


def build_stability_inpaint_request_body(prompt, source_image_b64,
                                         mask_image_b64, seed,
                                         output_format="png"):
    """Bedrock Stability inpaint request body (Req 2.3, 7.2).

    Exactly the fields ``{image, mask, prompt, seed, output_format}``;
    seed passed unmodified (Task_Seed range 0..858,993,459 is inside
    Stability's 0..4,294,967,294). ``seed=None`` omits the field.
    negative_prompt and guidance parameters are omitted: the entry's
    capability flags exclude them (Req 2.4).
    """
    body = {
        "image": source_image_b64,
        "mask": mask_image_b64,
        "prompt": prompt,
        "output_format": output_format,
    }
    if seed is not None:
        body["seed"] = int(seed)
    return body


def extract_stability_result(payload):
    """First generated image (base64 string) from a Stability response
    ``{images, seeds, finish_reasons}`` (Req 2.5).

    Raises :class:`StabilityGenerationError` when ``finish_reasons[0]``
    is non-null (content filtered / inference error) or ``images`` is
    empty, with the reported reason in the message (Req 2.6).
    """
    finish_reasons = payload.get("finish_reasons") or []
    reason = finish_reasons[0] if finish_reasons else None
    images = payload.get("images") or []
    if reason is not None:
        raise StabilityGenerationError(
            f"Stability generation did not return a usable image: {reason}")
    if not images:
        raise StabilityGenerationError(
            "Stability generation did not return a usable image: "
            "the response contained no images")
    return images[0]


# ---------------------------------------------------------------------------
# Bedrock invocation failure classification (Req 9.1, 9.2)
# ---------------------------------------------------------------------------

def classify_bedrock_invocation_error(error_code, error_message, model_id):
    """User-facing per-task failure reason for a Bedrock InvokeModel
    ClientError (Req 9.1, 9.2). Total: always returns a non-empty string.

    - AccessDeniedException -> Bedrock model access not granted for
      ``model_id``.
    - ResourceNotFoundException whose message marks the model as Legacy
      -> the model's lifecycle status is the cause.
    - anything else -> ``"<code>: <message>"`` passthrough.
    """
    code = str(error_code or "")
    message = str(error_message or "")
    if code == "AccessDeniedException":
        return (f"Bedrock model access is not granted for {model_id}: "
                f"{message}")
    if code == "ResourceNotFoundException" and "legacy" in message.lower():
        return (f"Model {model_id} is marked Legacy by the provider "
                f"(lifecycle status): {message}")
    return f"{code}: {message}"


# ---------------------------------------------------------------------------
# Approval filtering (Req 6.3, 6.5, 6.6)
# ---------------------------------------------------------------------------

def select_approved(previews):
    """Exactly the previews with ``approval_state == 'approved'``, in their
    original order (Req 6.3, 6.6). Raises :class:`ValidationError` when the
    approved subset is empty (Req 6.5)."""
    approved = [p for p in previews if p.get("approval_state") == "approved"]
    if not approved:
        raise ValidationError(
            "At least one approved image is required to integrate the session"
        )
    return approved


# ---------------------------------------------------------------------------
# Auto-annotation: bounding box derivation (Req 7.1, 7.2)
# ---------------------------------------------------------------------------

def bbox_from_mask(mask):
    """Minimal bounding box ``{left, top, width, height}`` containing every
    nonzero cell of ``mask`` (a list of rows); ``None`` for an all-zero or
    empty mask (Req 7.2)."""
    min_row = min_col = max_row = max_col = None
    for row_index, row in enumerate(mask or []):
        for col_index, value in enumerate(row):
            if value:
                if min_row is None:
                    min_row = max_row = row_index
                    min_col = max_col = col_index
                else:
                    max_row = row_index
                    min_col = min(min_col, col_index)
                    max_col = max(max_col, col_index)
    if min_row is None:
        return None
    return {
        "left": min_col,
        "top": min_row,
        "width": max_col - min_col + 1,
        "height": max_row - min_row + 1,
    }


def _image_dims(pixels):
    """(width, height) of a rectangular pixel grid, or None when the grid is
    empty or ragged (incomparable)."""
    if not pixels or not pixels[0]:
        return None
    width = len(pixels[0])
    if any(len(row) != width for row in pixels):
        return None
    return (width, len(pixels))


def _full_image_bbox(pixels):
    """Bounding box covering the whole generated image (fallback)."""
    dims = _image_dims(pixels)
    if dims is None:
        height = len(pixels) if pixels else 0
        width = max((len(row) for row in pixels), default=0) if pixels else 0
        return {"left": 0, "top": 0, "width": width, "height": height}
    width, height = dims
    return {"left": 0, "top": 0, "width": width, "height": height}


def _pixel_diff(a, b):
    """Per-pixel absolute difference; for multi-channel pixels the maximum
    channel difference. Mismatched channel counts count as fully changed."""
    a_channels = a if isinstance(a, (list, tuple)) else (a,)
    b_channels = b if isinstance(b, (list, tuple)) else (b,)
    if len(a_channels) != len(b_channels):
        return float("inf")
    return max(abs(x - y) for x, y in zip(a_channels, b_channels))


def bbox_from_diff(source_px, generated_px, threshold=0):
    """Pixel-difference bounding box for the changed region between
    ``source_px`` and ``generated_px`` (Req 7.1).

    Pixels differing by more than ``threshold`` (max channel difference)
    count as changed. Falls back to the full-image bounding box when the
    diff is empty or the images are incomparable (different dimensions,
    ragged, or empty grids)."""
    generated_dims = _image_dims(generated_px)
    source_dims = _image_dims(source_px)
    if generated_dims is None or source_dims != generated_dims:
        return _full_image_bbox(generated_px)
    mask = [
        [1 if _pixel_diff(s, g) > threshold else 0
         for s, g in zip(source_row, generated_row)]
        for source_row, generated_row in zip(source_px, generated_px)
    ]
    box = bbox_from_mask(mask)
    return box if box is not None else _full_image_bbox(generated_px)


# ---------------------------------------------------------------------------
# Manifest record building and appending (Req 7.1, 7.4, 7.5, 7.8, 10.3)
# ---------------------------------------------------------------------------

def build_manifest_record(image_s3_uri, defect_type, bbox, image_size,
                          session_meta, resolved_prompt, timestamp,
                          bbox_source="full_image"):
    """Ground Truth style augmented manifest record for one approved
    synthetic image (Req 7.1, 7.4, 10.3).

    Always includes ``source-ref`` / ``anomaly-label`` /
    ``anomaly-label-metadata`` so the Training_Subsystem's
    ``validate_marketplace_manifest`` accepts the manifest unchanged
    (Req 7.8), plus the ``synthetic-defect`` bounding-box annotation and
    ``synthetic-defect-metadata`` (synthetic marker, generation model id,
    session id, resolved prompt, bounding-box source).

    ``image_size`` is a dict with ``width`` / ``height`` (``depth``
    optional, default 3). ``timestamp`` is an ISO-8601 string or an epoch
    number (seconds)."""
    if isinstance(timestamp, str):
        creation_date = timestamp
    else:
        creation_date = datetime.fromtimestamp(
            timestamp, tz=timezone.utc).replace(tzinfo=None).isoformat()

    return {
        "source-ref": image_s3_uri,
        "anomaly-label": 1,
        "anomaly-label-metadata": {
            "confidence": 1.0,
            "class-name": defect_type,
            "human-annotated": "no",
            "creation-date": creation_date,
            "type": "groundtruth/image-classification",
            "job-name": f"synthetic/{session_meta['session_id']}",
        },
        "synthetic-defect": {
            "image_size": [{
                "width": image_size["width"],
                "height": image_size["height"],
                "depth": image_size.get("depth", 3),
            }],
            "annotations": [{
                "class_id": 0,
                "left": bbox["left"],
                "top": bbox["top"],
                "width": bbox["width"],
                "height": bbox["height"],
            }],
        },
        "synthetic-defect-metadata": {
            "synthetic": True,
            "class-map": {"0": defect_type},
            "generation-model-id": session_meta["generation_model_id"],
            "generation-session-id": session_meta["session_id"],
            "resolved-prompt": resolved_prompt,
            "bounding-box-source": bbox_source,
            "human-annotated": "no",
            "type": "groundtruth/object-detection",
        },
    }


def append_manifest_lines(existing_content, records):
    """Existing manifest content preserved byte-for-byte (trailing newline
    normalized) + one JSON line per record (Req 7.4, 7.5).

    Pure function: atomicity is provided by the caller's ETag-conditional
    S3 write."""
    if existing_content:
        base = (existing_content if existing_content.endswith("\n")
                else existing_content + "\n")
    else:
        base = ""
    new_lines = "".join(json.dumps(record) + "\n" for record in records)
    return base + new_lines


def parse_manifest_lines(content):
    """Parse JSON Lines manifest content into a list of records, skipping
    blank lines (round-trip helper, Req 7.8)."""
    return [json.loads(line) for line in content.splitlines() if line.strip()]
