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
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


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
        # Amazon Nova user guide, "Supported image resolutions": each
        # side 320-4096 px, fewer than 4,194,304 pixels in total.
        "input_image_limits": {"min_side": 320, "max_side": 4096,
                               "max_pixels": 4194303},
        # Same guide: an IMAGE_VARIATION output defaults to 1024x1024 and
        # may be any size whose sides are multiples of 16 within 320-4096
        # px and fewer than 4,194,304 pixels in total.
        "image_variation_output": {"side_multiple": 16, "min_side": 320,
                                   "max_side": 4096,
                                   "max_pixels": 4194303},
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
        # Bedrock Titan Image Generator parameters: inpainting /
        # image-variation input images up to 1,408 px on the longest side.
        "input_image_limits": {"max_side": 1408},
        # Same page: an IMAGE_VARIATION output defaults to 1408x1408 and
        # may only be one of these permitted width x height sizes.
        "image_variation_output": {"sizes": [
            [1024, 1024], [768, 768], [512, 512],
            [768, 1152], [384, 576], [1152, 768], [576, 384],
            [768, 1280], [384, 640], [1280, 768], [640, 384],
            [896, 1152], [448, 576], [1152, 896], [576, 448],
            [768, 1408], [384, 704], [1408, 768], [704, 384],
            [640, 1408], [320, 704], [1408, 640], [704, 320],
            [1152, 640], [1173, 640],
        ]},
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
        # AWS Stability AI Image Services guide, Inpaint parameters: every
        # side at least 64 px, 4,096-9,437,184 pixels in total (the bounds
        # the model's ValidationException quotes).
        "input_image_limits": {"min_side": 64, "min_pixels": 4096,
                               "max_pixels": 9437184},
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
# Worker deadline, continuation and staleness (fix-synthetic-worker-deadline)
#
# A variation costs about 25 s (20 s of model time plus 3-5 s of image
# work), so a 50-variation pass needs about 20 minutes against the worker
# Lambda's 900 s timeout. Pass 2 of session 6115b94c ran 43 of 50 tasks,
# timed out, and Lambda's default asynchronous retry policy (2 retries)
# re-ran the whole plan from task 0 twice: 130 PREVIEW items for 50 tasks,
# all with the same seeds, and a session stuck in `generating` because the
# worker only reports `awaiting_review` after the last task. Lambda also
# documents that an asynchronous event can be delivered more than once
# even without an error. The helpers below let the worker stop before the
# deadline, hand the unfinished tail of the plan to a continuation
# invocation that runs only the tasks without a PREVIEW record, and let
# the read path recognise a pass whose worker died without reporting.
# ---------------------------------------------------------------------------

# Remaining time below which the worker stops starting tasks and hands the
# rest of the plan to a continuation: one measured task is ~25 s, a hung
# Bedrock call runs into botocore's 60 s read timeout before it fails, and
# the hand-over itself is one DynamoDB write and one lambda:Invoke.
CONTINUATION_RESERVE_MS = 120_000

# Grace after the worker's deadline before a `generating` session counts as
# dead: the continuation event's 2 min maximum age (synthetic-data-stack.ts)
# plus a cold start.
STALE_WORKER_GRACE_MS = 180_000


def source_image_key(source_ref) -> str:
    """S3 key of a Source_Image reference (dict with 'key' or plain str);
    the same rule as the handler's ``_source_key``."""
    if isinstance(source_ref, dict):
        return source_ref.get("key", "")
    return str(source_ref)


def preview_task_identity(preview) -> tuple:
    """Identity of the plan task a PREVIEW record belongs to:
    ``('index', task_index)`` when the record carries one, else
    ``('key', source_image_key, variation_index)`` for records written
    before task indexes were stored."""
    task_index = preview.get("task_index")
    if task_index is not None:
        return ("index", int(task_index))
    return ("key", source_image_key(preview.get("source_image_key")),
            int(preview.get("variation_index") or 0))


def remaining_tasks(plan, previews, generation_pass) -> list:
    """The plan tasks, in plan order, that have no PREVIEW record in
    ``generation_pass`` -- by task index or by (source key, variation
    index), so a continuation never repeats a task already written."""
    wanted_pass = int(generation_pass)
    done = set()
    for preview in previews:
        if int(preview.get("generation_pass", -1)) != wanted_pass:
            continue
        done.add(preview_task_identity(preview))
    pending = []
    for task in plan:
        by_index = ("index", int(task.get("task_index")))
        by_key = ("key", source_image_key(task.get("source_image")),
                  int(task.get("variation_index") or 0))
        if by_index in done or by_key in done:
            continue
        pending.append(task)
    return pending


def should_hand_over(remaining_ms, reserve_ms=CONTINUATION_RESERVE_MS) -> bool:
    """True when the invocation has less than ``reserve_ms`` left and must
    not start another task."""
    return int(remaining_ms) < int(reserve_ms)


def worker_deadline_ms(meta, worker_timeout_ms) -> int:
    """When the pass's worker must have reported by: the recorded
    ``worker_deadline_at``, or ``updated_at`` plus the worker timeout for a
    session written before the field existed."""
    deadline = meta.get("worker_deadline_at")
    if deadline is not None:
        return int(deadline)
    return int(meta.get("updated_at") or 0) + int(worker_timeout_ms)


def generation_is_stale(meta, now_ms, worker_timeout_ms,
                        grace_ms=STALE_WORKER_GRACE_MS) -> bool:
    """True iff the session reads ``generating`` but its worker's deadline
    plus ``grace_ms`` has passed, so no invocation can still be running."""
    if meta.get("status") != "generating":
        return False
    return int(now_ms) > worker_deadline_ms(meta, worker_timeout_ms) + int(
        grace_ms)


def stale_generation_failure_reason(generation_pass, written, planned) -> str:
    """``last_failure.reason`` for a pass whose worker died without
    reporting back."""
    return (
        f"Generation pass {int(generation_pass)} stopped before it "
        f"finished: the worker wrote {int(written)} preview record(s) for "
        f"{int(planned)} planned variation(s) and did not report back "
        f"within its time limit. Completed previews can still be "
        f"reviewed; Regenerate starts a new pass."
    )


def running_generation_message(generation_pass, written, planned) -> str:
    """409 body for a generate request while a pass is still running."""
    return (
        f"A generation pass is already running (pass "
        f"{int(generation_pass)}: {int(written)} of {int(planned)} "
        f"variations written). Wait for it to finish before regenerating."
    )


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
# Source image size limits
# ---------------------------------------------------------------------------
#
# Each catalog entry declares the input image sizes its model accepts as
# ``input_image_limits`` (keys ``min_side``, ``max_side``, ``min_pixels``,
# ``max_pixels``, every one optional). A source image outside those limits
# is handled per the ``oversize_handling`` generation parameter: scaled to
# fit before the model call with the output scaled back to the source
# dimensions (the default), or recorded as a per-variation failure that
# names the violated bound without calling the model.

OVERSIZE_HANDLING_RESIZE_RESTORE = "resize_restore"
OVERSIZE_HANDLING_FAIL = "fail"
OVERSIZE_HANDLING_CHOICES = (OVERSIZE_HANDLING_RESIZE_RESTORE,
                             OVERSIZE_HANDLING_FAIL)
DEFAULT_OVERSIZE_HANDLING = OVERSIZE_HANDLING_RESIZE_RESTORE


def resolve_oversize_handling(params) -> str:
    """The ``oversize_handling`` value of a generation_params mapping:
    missing or None selects the default; any other value must be one of
    OVERSIZE_HANDLING_CHOICES."""
    value = (params or {}).get("oversize_handling")
    if value is None:
        return DEFAULT_OVERSIZE_HANDLING
    if value not in OVERSIZE_HANDLING_CHOICES:
        raise ValidationError(
            "oversize_handling must be 'resize_restore' or 'fail'")
    return value


def image_limit_violation(width, height, limits) -> Optional[str]:
    """Why a ``width`` x ``height`` image violates ``limits``, or None when
    it is within them (or ``limits`` is empty). Bounds are checked in the
    order max_pixels, max_side, min_pixels, min_side and the first violated
    one is reported."""
    limits = limits or {}
    pixels = width * height
    max_pixels = limits.get("max_pixels")
    if max_pixels is not None and pixels > max_pixels:
        return (f"{pixels:,} pixels exceeds the maximum of "
                f"{max_pixels:,} pixels")
    max_side = limits.get("max_side")
    if max_side is not None and max(width, height) > max_side:
        return (f"the longest side ({max(width, height)} px) exceeds the "
                f"maximum of {max_side:,} px")
    min_pixels = limits.get("min_pixels")
    if min_pixels is not None and pixels < min_pixels:
        return (f"{pixels:,} pixels is below the minimum of "
                f"{min_pixels:,} pixels")
    min_side = limits.get("min_side")
    if min_side is not None and min(width, height) < min_side:
        return (f"the shortest side ({min(width, height)} px) is below "
                f"the minimum of {min_side} px")
    return None


def fit_within_limits(width, height, limits) -> Tuple[int, int]:
    """``(width, height)`` scaled uniformly to satisfy ``limits``.

    Unchanged when already within them. Otherwise one uniform scale is
    applied: when a maximum is violated, the smallest factor that meets
    every violated maximum (sides floored, at least 1 px); when only
    minima are violated, the largest factor that meets every violated
    minimum (sides rounded up). The aspect ratio is preserved up to the
    integer rounding of the sides.
    """
    limits = limits or {}
    if image_limit_violation(width, height, limits) is None:
        return width, height
    pixels = width * height
    longest, shortest = max(width, height), min(width, height)

    down = []
    max_pixels = limits.get("max_pixels")
    if max_pixels is not None and pixels > max_pixels:
        down.append(math.sqrt(max_pixels / pixels))
    max_side = limits.get("max_side")
    if max_side is not None and longest > max_side:
        down.append(max_side / longest)
    if down:
        scale = min(down)
        return (max(1, math.floor(width * scale)),
                max(1, math.floor(height * scale)))

    up = []
    min_pixels = limits.get("min_pixels")
    if min_pixels is not None and pixels < min_pixels:
        up.append(math.sqrt(min_pixels / pixels))
    min_side = limits.get("min_side")
    if min_side is not None and shortest < min_side:
        up.append(min_side / shortest)
    scale = max(up)
    return math.ceil(width * scale), math.ceil(height * scale)


def describe_image_limits(limits) -> str:
    """Human-readable form of ``limits``, e.g. ``"images between 4,096 and
    9,437,184 pixels in total, with each side at least 64 px"``."""
    limits = limits or {}
    min_pixels, max_pixels = limits.get("min_pixels"), limits.get("max_pixels")
    min_side, max_side = limits.get("min_side"), limits.get("max_side")

    pixel_part = None
    if min_pixels is not None and max_pixels is not None:
        pixel_part = (f"between {min_pixels:,} and {max_pixels:,} pixels "
                      f"in total")
    elif max_pixels is not None:
        pixel_part = f"up to {max_pixels:,} pixels in total"
    elif min_pixels is not None:
        pixel_part = f"at least {min_pixels:,} pixels in total"

    side_part = None
    if min_side is not None and max_side is not None:
        side_part = f"each side between {min_side:,} and {max_side:,} px"
    elif min_side is not None:
        side_part = f"each side at least {min_side:,} px"
    elif max_side is not None:
        side_part = f"the longest side at most {max_side:,} px"

    if pixel_part and side_part:
        return f"images {pixel_part}, with {side_part}"
    if pixel_part:
        return f"images {pixel_part}"
    if side_part:
        return f"images with {side_part}"
    return "images of any size"


def oversize_failure_reason(source_key, width, height, model_id,
                            limits) -> str:
    """Per-variation failure reason recorded under the ``fail`` handling
    for a source image outside the model's limits."""
    violation = image_limit_violation(width, height, limits)
    return (
        f"Source image {source_key} is {width}x{height}: {violation}. "
        f"{model_id} accepts {describe_image_limits(limits)}. Choose "
        f"'Resize to fit the model' as the size-limit handling to generate "
        f"from this image anyway."
    )


def _aspect_distance(size, target_ratio):
    """|log(aspect ratio of ``size``) - log(``target_ratio``)|: the uniform
    measure of how far two aspect ratios lie apart."""
    return abs(math.log(size[0] / size[1]) - math.log(target_ratio))


def image_variation_output_size(width, height, rules) -> Optional[
        Tuple[int, int]]:
    """The IMAGE_VARIATION output size to request for a ``width`` x
    ``height`` input so the result can be scaled back uniformly.

    Without an explicit size the Amazon models answer a fixed square
    (Nova Canvas 1024x1024, Titan 1408x1408) whatever the input's shape.
    ``rules`` is the catalog entry's ``image_variation_output``: either a
    fixed list of permitted ``sizes`` (Titan), of which the one with the
    aspect ratio closest to the input's is chosen (the larger on a tie), or
    a grid (Nova: sides multiples of ``side_multiple`` within ``min_side``
    .. ``max_side`` with at most ``max_pixels`` in total), where each side
    of the input is rounded down and up to the grid and the permitted
    combination closest to the input's aspect ratio is chosen (the one
    nearest the input's sides on a tie, so an on-grid input is requested
    as-is). None when ``rules`` is empty or no grid size is permitted.
    """
    rules = rules or {}
    if not rules:
        return None
    target_ratio = width / height
    sizes = rules.get("sizes")
    if sizes:
        chosen = min(sizes, key=lambda s: (_aspect_distance(s, target_ratio),
                                           -(s[0] * s[1])))
        return int(chosen[0]), int(chosen[1])

    multiple = int(rules.get("side_multiple") or 1)
    min_side = rules.get("min_side")
    max_side = rules.get("max_side")
    max_pixels = rules.get("max_pixels")

    def grid_sides(side):
        down = (side // multiple) * multiple
        return {down, down + multiple}

    candidates = []
    for w in grid_sides(width):
        for h in grid_sides(height):
            if min_side is not None and min(w, h) < min_side:
                continue
            if max_side is not None and max(w, h) > max_side:
                continue
            if max_pixels is not None and w * h > max_pixels:
                continue
            candidates.append((w, h))
    if not candidates:
        return None
    return min(candidates, key=lambda s: (
        _aspect_distance(s, target_ratio),
        abs(s[0] - width) + abs(s[1] - height)))


# ---------------------------------------------------------------------------
# Bedrock request adapters (Req 2.1-2.4, 7.2, 8.1)
# ---------------------------------------------------------------------------

def build_amazon_request_body(model_entry: Dict, method: str, prompt: str,
                              source_b64: str, seed, params: Dict,
                              mask_prompt: Optional[str],
                              output_size: Optional[Tuple[int, int]] = None
                              ) -> Dict:
    """Bedrock invoke_model JSON body for one task (numberOfImages: 1).

    ``output_size`` (width, height), when given, is requested as
    ``imageGenerationConfig.width``/``height`` of an image-variation task
    only: the Amazon models size an inpainting output after its input and
    the Nova guide says the two fields should not be provided for
    INPAINTING. Without it the body is unchanged from before the
    size-limit handling existed."""
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
    if output_size is not None:
        config['width'] = int(output_size[0])
        config['height'] = int(output_size[1])
    return {
        'taskType': 'IMAGE_VARIATION',
        'imageVariationParams': {
            'images': [source_b64],
            'text': prompt,
        },
        'imageGenerationConfig': config,
    }


# Stability's service refuses to return a response (the JSON carrying the
# base64 image) above 16,777,216 bytes. The 4608x3288 sources of session
# 6115b94c, sent at the 9,437,184-px cap as 3636x2594 (9,431,784 px) with
# the default PNG output, failed every variation with
#   ValidationException: {"detail":"Response payload size 18982136 bytes
#   exceeds the maximum allowed size of 16777216 bytes. Please try
#   adjusting the seed, using a different input image, or requesting JPEG
#   output_format"}
# (live payloads 18,982,136 / 19,461,701 / 19,803,661 / 19,836,096 /
# 20,276,590 bytes: PNGs of 1.51-1.61 bytes per pixel). The payload is 4/3
# of the image bytes, so a PNG answer fits only below 1.334 B/px at that
# pixel count, which a photograph does not reach (1.4-1.6 B/px); the JPEG
# of the same fitted sample is 0.32 B/px at quality 95 (24 % of the cap).
# So a source scaled *down* to the pixel cap asks for the JPEG output the
# error text itself names; in-limit sources and sources scaled *up* to the
# minimums keep the lossless PNG default.
STABILITY_RESPONSE_MAX_BYTES = 16_777_216
STABILITY_OUTPUT_FORMAT_DEFAULT = "png"
STABILITY_OUTPUT_FORMAT_DOWNSCALED = "jpeg"


def stability_output_format(source_size, model_size):
    """``output_format`` of a Stability inpaint request: ``"jpeg"`` when
    the image sent to the model (``model_size``) has fewer pixels than the
    source (``source_size``), i.e. the source was scaled down to the pixel
    cap, where a PNG answer exceeds STABILITY_RESPONSE_MAX_BYTES; ``"png"``
    otherwise, so in-limit sources and sources scaled up to the minimums
    keep today's lossless answer."""
    if model_size[0] * model_size[1] < source_size[0] * source_size[1]:
        return STABILITY_OUTPUT_FORMAT_DOWNSCALED
    return STABILITY_OUTPUT_FORMAT_DEFAULT


def build_stability_inpaint_request_body(prompt, source_image_b64,
                                         mask_image_b64, seed,
                                         output_format="png"):
    """Bedrock Stability inpaint request body (Req 2.3, 7.2).

    Exactly the fields ``{image, mask, prompt, seed, output_format}``;
    seed passed unmodified (Task_Seed range 0..858,993,459 is inside
    Stability's 0..4,294,967,294). ``seed=None`` omits the field.
    negative_prompt and guidance parameters are omitted: the entry's
    capability flags exclude them (Req 2.4). ``output_format`` is
    :func:`stability_output_format`'s choice for the task's source and
    model-side sizes; the default stays ``png``.
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


def scale_bbox(bbox, from_size, to_size):
    """``bbox`` (``{left, top, width, height}`` in the pixel grid of
    ``from_size``) mapped onto an image of ``to_size`` (both ``(width,
    height)``): the smallest integer box covering the scaled region,
    clamped to the image. The identity when the sizes are equal."""
    from_width, from_height = from_size
    to_width, to_height = to_size
    if (from_width, from_height) == (to_width, to_height):
        return dict(bbox)
    scale_x = to_width / from_width
    scale_y = to_height / from_height
    left = max(0, min(to_width, math.floor(bbox["left"] * scale_x)))
    top = max(0, min(to_height, math.floor(bbox["top"] * scale_y)))
    right = max(left, min(to_width, math.ceil(
        (bbox["left"] + bbox["width"]) * scale_x)))
    bottom = max(top, min(to_height, math.ceil(
        (bbox["top"] + bbox["height"]) * scale_y)))
    return {"left": left, "top": top, "width": right - left,
            "height": bottom - top}


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
