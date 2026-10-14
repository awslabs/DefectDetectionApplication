# Design Document

## Overview

This feature adds a third Auto_Labeler family — a prompt-guided LLM mode — alongside the existing `sam` and `bedrock:<model_id>` modes. The Job_Creator picks an image-capable model and writes a Detection_Prompt; for each dataset image the Auto_Labeler makes exactly one model call asking for a JSON document of Coordinate_Guidance (boxes and/or polygons in pixel coordinates), then converts that guidance into the modality's existing Pre_Label shape.

The design is deliberately additive. It reuses, unmodified, the machinery that already carries pre-labels to reviewers and manifests:

- the auto-label SQS fan-out (`dda_labeling_worker._enqueue_autolabel_messages`) and its consumer (`dda_autolabel_worker`),
- pre-label storage at `labeling/{usecase_id}/{job_id}/prelabels/{task_id}.json` (`_write_prelabel`),
- the conditional `prelabel_status` resolution that makes duplicate SQS delivery idempotent (`_mark_task`),
- the skip-verification completion counter and `review_ready` flip (`_resolve_skip_verification_counters`),
- the labeler canvas, Admin_Review, and the whole manifest path (`serialize_manifest`, `render_mask_png`, `_validate_manifest_lines`).

Three things are genuinely new: a model family identifier and a Detection_Prompt on the job record, a strict Guidance_Parser, and a Mask_Converter that rasterizes polygons/boxes into the RLE the segmentation path already consumes. Everything else is threading a field through existing code.

Two pre-existing gaps must be closed for Segmentation LLM pre-labels to reach a manifest, and both fixes are narrow and additive:

1. `dda_labeling_worker._canonical_annotation` adds the canonical `image_size` only for `ObjectDetection`; Segmentation mask rendering requires it. The normalization is extended to Segmentation.
2. Pre-label storage failure is currently a transient (retried) error. Requirement 6.2 makes it a terminal `Failed` resolution. This is scoped to the LLM family so SAM/Bedrock retry semantics stay exactly as they are (Requirement 1.7).

### Design principles

- **One extension point per layer.** Model-family dispatch already exists in exactly two places (`create_dda_job` validation, `_generate_prelabel` dispatch) plus one frontend mirror. The LLM family plugs into those three and nowhere else.
- **Pure, testable core.** Prompt construction, JSON extraction, validation, and rasterization are pure functions in a new shared-layer module with no boto3, no Pillow, and no I/O. They carry the property-based tests.
- **All-or-nothing per image.** A single bad detection fails the whole image with one failure reason (Requirement 4.8). No repair, no re-ask, no partial pre-label.
- **No new failure surface for existing jobs.** Every change is gated on the LLM family or is a strictly additive normalization.

## Architecture

### End-to-end flow

```
Job creation (dda_labeling.create_dda_job)
  ├─ validate auto_label.model = 'llm:<identifier>'  (family + identifier rules)
  ├─ validate auto_label.detection_prompt            (1..2000 chars, verbatim)
  ├─ persist job item with auto_label {enabled, model, detection_prompt}
  ├─ job_created audit event + auto_label_model / auto_label_mode
  └─ async invoke worker {action: 'distribute'}

Distribution (dda_labeling_worker._distribute)
  ├─ one task per image, prelabel_status = 'Pending'   (unchanged)
  └─ _enqueue_autolabel_messages
       └─ message {job_id, task_id, image_s3_uri, modality, label_set,
                   model: 'llm:<id>', detection_prompt,
                   per_label_prompts?}                 (2 new fields)

Per-image consumption (dda_autolabel_worker, SQS batchSize 5)
  _generate_prelabel dispatch
    ├─ 'sam'       -> _generate_sam_prelabel           (unchanged)
    ├─ 'bedrock:*' -> _generate_bedrock_prelabel       (unchanged)
    └─ 'llm:*'     -> _generate_llm_prelabel           (NEW)
         1. _read_image_bytes            cross-account + direct fallback
         2. _image_dimensions            None => Failed, no model call
         3. build_detection_prompt       pure
         4. converse()                   exactly one call, no retries, <=120 s
         5. parse_guidance               pure, strict
         6. guidance_to_prelabel         pure, per-modality
    -> _write_prelabel -> _mark_task(Available)         (unchanged)
    -> on failure: _mark_task(Failed, reason)           (unchanged)
    -> skip-verification: counter + review_ready        (unchanged)

Review (unchanged)
  ├─ team job:            labeler canvas loads prelabel_s3_key
  └─ skip-verification:   Admin_Review accept/reject

Completion (unchanged, plus one normalization fix)
  └─ _generate_manifest -> _canonical_annotation -> serialize_manifest
     -> render_mask_png (Segmentation) -> _validate_manifest_lines
```

### Component map

| Layer | File | Change |
|---|---|---|
| Guidance core (new) | `backend/layers/shared/python/dda_llm_guidance.py` | new pure module |
| Job creation API | `backend/functions/dda_labeling.py` | validation, persistence, audit |
| Fan-out producer | `backend/functions/dda_labeling_worker.py` | message fields, model resolution, `_canonical_annotation` |
| Per-image consumer | `backend/functions/dda_autolabel_worker.py` | dispatch + `_generate_llm_prelabel` |
| Job detail API | `backend/functions/labeling.py` | pre-label counts |
| Wizard | `frontend/src/pages/CreateLabelingJob.tsx` | model options, prompt field, validation |
| API types | `frontend/src/services/api.ts` | `detection_prompt` param |
| Job detail view | `frontend/src/pages/LabelingDetail.tsx` | model, prompt, counts |
| Admin review | `frontend/src/pages/labeling/AdminReview.tsx` | failure reason display |
| Infrastructure | `infrastructure/lib/compute-stack.ts` | none required |

No new AWS resources. The existing `DdaAutolabelWorker` IAM policy already grants `bedrock:InvokeModel` on `foundation-model/*` and `inference-profile/*`, and the worker already has the shared layer where the new module lives. Rasterization is pure Python, so the worker does not need the Pillow-bearing `imagingLayer`.

## Components and Interfaces

### 1. Model identifier and job configuration

The auto-label model stays a single opaque string, extended with a third family prefix:

```
auto_label = {
  'enabled': True,
  'model': 'llm:<model_identifier>',
  'detection_prompt': '<1..2000 chars, verbatim>'
}
```

Splitting is on the **first** colon only (`model.split(':', 1)[1]`), because Bedrock model ids legitimately contain colons (`anthropic.claude-3-5-sonnet-20241022-v2:0`). This matches the existing `bedrock:` handling.

`AUTO_LABEL_MODEL_MODALITIES` in `dda_labeling.py` gains the new family covering all three modalities (Requirement 1.3):

```python
AUTO_LABEL_MODEL_MODALITIES = {
    'sam':     ('Segmentation', 'ObjectDetection'),
    'bedrock': ('Classification', 'ObjectDetection'),
    'llm':     ('Classification', 'Segmentation', 'ObjectDetection'),
}
```

**Identifier validation** (Requirement 1.5) — a shared helper so backend and message consumer agree:

```python
MODEL_IDENTIFIER_MAX_LENGTH = 256

def validate_model_identifier(identifier) -> Optional[str]:
    """None when valid, else the reason it is not."""
    if not isinstance(identifier, str) or not identifier:
        return 'model identifier is required'
    if len(identifier) > MODEL_IDENTIFIER_MAX_LENGTH:
        return f'model identifier must be at most {MODEL_IDENTIFIER_MAX_LENGTH} characters'
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in identifier):
        return 'model identifier must not contain whitespace or control characters'
    return None
```

**Detection_Prompt validation** (Requirement 2): present, non-whitespace-only, `len(prompt) <= 2000` measured on the raw string. The stored value is the raw string, not the stripped one (Requirement 2.5: character-for-character). Emptiness is judged on `prompt.strip()`; length is judged on `prompt`.

Both validations join the existing `errors` list in `create_dda_job`, so a bad request returns the standard single 400 with `validation_errors` **before** any S3 enumeration or DynamoDB write (Requirements 1.5, 2.3, 2.4 — "persist nothing").

**Audit** (Requirement 9.4): the `job_created` event `details` gains `auto_label_model` (the full `llm:<id>` string, or the existing model string, or absent) and `auto_label_mode` (`'llm' | 'sam' | 'bedrock' | 'none'`).

**Authorization** (Requirement 9): untouched. Creation is authorized by the existing `create_labeling_jobs` permission set; skip-verification still runs the `SKIP_VERIFICATION_ADMIN_ROLES` check with its `unauthorized_access` audit event and 403 before any validation errors are assembled.

### 2. `dda_llm_guidance` — the pure core (new)

New shared-layer module, `backend/layers/shared/python/dda_llm_guidance.py`. No boto3, no Pillow, no I/O. It imports only `json`, `math`, typing, and `rle_encode` companions conceptually (RLE emission is implemented here; see below).

#### Guidance wire format

```json
{"detections": [
  {"class": "scratch", "box": {"left": 12, "top": 30, "width": 40, "height": 25}},
  {"class": "dent",    "polygon": [[10, 20], [48, 22], [40, 60], [12, 55]]}
]}
```

Exactly one of `box` / `polygon` per detection (Requirement 4.3). `{"detections": []}` is valid and means "nothing found" (Requirement 4.9).

#### Internal detection model

```python
Detection = {
  'class': str,                  # exact Label_Set entry
  'geometry': 'box' | 'polygon',
  # geometry == 'box':
  'box': {'left': float, 'top': float, 'width': float, 'height': float},
  # geometry == 'polygon':
  'vertices': [(float, float), ...],   # >= 3
}
```

#### Public interface

```python
class GuidanceError(Exception):
    """Coordinate_Guidance is unusable; the image is a generation failure."""

MAX_DETECTIONS = 100
POLYGON_MIN_VERTICES = 3

def build_detection_prompt(modality: str, label_set: list[str],
                           detection_prompt: str,
                           width: int, height: int,
                           per_label_prompts: dict[str, str] | None) -> str: ...

def extract_first_json(text: str) -> dict:
    """First parseable JSON object in reading order (Req 4.1)."""

def parse_guidance(raw_text: str, label_set: list[str],
                   width: int, height: int) -> list[Detection]:
    """extract_first_json + full structural/geometric validation."""

def serialize_guidance(detections: list[Detection]) -> str:
    """Wire-format JSON for a detection list (round-trip counterpart)."""

def guidance_to_prelabel(detections: list[Detection], modality: str,
                         label_set: list[str],
                         width: int, height: int) -> dict:
    """Modality Pre_Label in the existing on-disk shape."""

def rasterize_to_rle(detection: Detection, width: int, height: int) -> str:
    """COCO-style column-major RLE for one detection's filled interior."""

def polygon_bounding_box(vertices) -> dict:
    """Axis-aligned hull of a polygon, as a box dict."""
```

#### `extract_first_json` — first document in reading order

The existing `_extract_json` takes the span from the first `{` to the **last** `}`, which is "outermost", not "first parseable". Requirement 4.1 asks for the first parseable document in reading order, so this is a separate function (the Bedrock path keeps its own helper untouched, Requirement 1.7):

```python
def extract_first_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    for index, ch in enumerate(text):
        if ch != '{':
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise GuidanceError('model output contains no parseable JSON object')
```

`raw_decode` ignores trailing content, so surrounding prose and ```json fences are tolerated for free (Requirement 4.1). A truncated leading object is skipped and the next candidate tried, which is what "first *parseable*" means.

#### `parse_guidance` — strict validation

Order of checks, each raising `GuidanceError` with a reason naming the offending element:

1. `detections` must be a list (Requirement 4.3). A missing or non-list `detections` is a structural mismatch.
2. `len(detections) <= 100`, checked **before** per-detection validation so an oversized document reports the cap, not the first bad box (Requirement 4.7).
3. Per detection: a dict; exactly one of `box` / `polygon` present; a `class` string.
4. Class name: `class.strip()` must be an exact, case-sensitive member of `label_set` (Requirement 4.4). The trimmed form is what gets stored.
5. Box: all four fields numeric and not `bool` (`isinstance(v, bool)` is rejected first, since `bool` is an `int` in Python); `width > 0` and `height > 0`; `left >= 0`, `top >= 0`, `left + width <= width_px`, `top + height <= height_px` (Requirement 4.5).
6. Polygon: a list of at least 3 vertices, each a 2-element sequence of non-bool numbers with `0 <= x <= width_px` and `0 <= y <= height_px` (Requirement 4.6).
7. `NaN` / `±inf` are rejected by the numeric check (`math.isfinite`) — they would otherwise slip through every comparison.

Any rejection aborts the whole document: no partial guidance, exactly one failure reason for the image (Requirement 4.8).

`serialize_guidance` exists so the round-trip property (Requirement 4.10) is expressible as `parse_guidance(serialize_guidance(d), ...) == d` over generated valid guidance.

#### `rasterize_to_rle` — polygon/box fill without Pillow

The existing RLE is a space-separated, **column-major** run-length string starting with the background count (`dda_manifest.rle_encode` / `rle_decode`). Column-major is convenient: the natural fill unit is a per-column y-span, which is exactly the ordering the RLE walks.

Building a dense `width * height` mask per detection would be prohibitive in the consumer Lambda — a 4000×3000 image with 100 detections is 1.2 billion byte writes in pure Python. Instead, spans are computed only where geometry exists, and the RLE is emitted directly from them:

```
spans(detection) -> {column_x: [(y_start, y_end), ...]}   # half-open, clamped
emit:
  cursor = 0                     # column-major pixel index
  counts = []                    # alternating background/foreground
  for x in ascending columns with spans:
      for (y0, y1) in ascending, merged spans of column x:
          absolute = x * height + y0
          push_background(absolute - cursor)
          push_foreground(y1 - y0)
          cursor = x * height + y1
  push_background(width * height - cursor)
```

- **Box**: columns `ceil(left - 0.5) .. ` sampled by pixel center — a pixel `(x, y)` is filled when its center `(x + 0.5, y + 0.5)` lies inside the rectangle. Cost O(box width).
- **Polygon**: for each column `x` in the bounding box, sample the vertical line `x + 0.5`, intersect it with every edge, sort the intersection `y` values, and pair them under the even-odd rule. Each pair `(ya, yb)` becomes the span of pixel rows whose centers fall in `[ya, yb)`. Cost O(columns × edges).

Both clamp spans to `[0, height)` and columns to `[0, width)`, so emitted geometry is in-bounds by construction (Requirement 5.6) even before validation is considered. Spans within a column are merged, so the emitted counts are always a strictly advancing alternating sequence summing to `width * height` — the invariant `rle_decode` enforces.

A geometry that covers no pixel center (a sub-pixel sliver, a degenerate polygon) yields zero spans, which the Mask_Converter turns into a generation failure (Requirement 5.7).

Because masks reach the manifest through the shared `render_mask_png` → `rle_decode` path, a mask rendered from an LLM pre-label is pixel-identical to one rendered from any other annotation with the same RLE (Requirement 8.3).

#### `guidance_to_prelabel` — modality conversion

Emitted shapes are exactly the ones already written to the artifacts bucket:

**Segmentation** (Requirements 5.1, 5.2, 5.5) — one region per detection, in guidance order, never merged across detections sharing a class:

```json
{"modality": "Segmentation",
 "regions": [{"class": "scratch", "rle": "0 12 40 ..."}],
 "image_width": 1920, "image_height": 1080}
```

**ObjectDetection** (Requirements 5.3, 5.5) — one box per detection; box detections keep their validated coordinates verbatim, polygons collapse to their axis-aligned hull:

```json
{"modality": "ObjectDetection",
 "boxes": [{"class": "dent", "left": 12.0, "top": 30.0, "width": 40.0, "height": 25.0}],
 "image_width": 1920, "image_height": 1080}
```

`_serialize_object_detection` casts coordinates with `int()`, which truncates. Truncation only shrinks values, so a validated in-bounds box stays in bounds — but a box narrower than one pixel would truncate to zero width. The converter therefore rejects any box whose truncated width or height is below 1, as a zero-extent conversion failure (Requirement 5.7).

**Classification** (Requirement 5.4) — derived from detection count against the fixed binary Label_Set:

```json
{"modality": "Classification", "label": "anomaly"}
```

`anomaly` when one or more detections, `normal` when zero. The label set is fixed to `['normal', 'anomaly']` at creation, so the classes are always available.

Zero detections in Segmentation/ObjectDetection produce an empty `regions` / `boxes` list and count as a **success** (Requirement 5.5); the labeler then sees an empty editable annotation (Requirement 7.2).

#### `build_detection_prompt`

One request per image carrying image data, prompt, Label_Set, and pixel dimensions with instructions to answer in those coordinates (Requirement 3.1):

```
You are labeling images for a defect-detection dataset.
Locate every object matching the detection request below and report its
location in pixel coordinates.
The image is {width} pixels wide and {height} pixels tall; every coordinate
must lie within these bounds.
Allowed class names: {', '.join(label_set)}.

Detection request:
{detection_prompt}                      # verbatim, unaltered

Guidance for label '{label}': {prompt}  # skip-verification only, per label

Respond with ONLY a JSON object of the form
{"detections": [{"class": "<allowed class>",
                 "box": {"left": <px>, "top": <px>,
                         "width": <px>, "height": <px>}},
                {"class": "<allowed class>",
                 "polygon": [[<px>, <px>], ...]}]}
Give each detection exactly one "box" or one "polygon" (at least 3 vertices).
Use {"detections": []} when nothing matches. Report at most 100 detections.
```

Both the Detection_Prompt and the per-label prompts are inserted verbatim, with no trimming or escaping (Requirement 2.6). For Classification the geometry instructions are identical — the model still returns detections, and the converter reduces them to a label. That keeps one prompt shape and one parser for all three modalities.

### 3. Fan-out producer (`dda_labeling_worker`)

**Model resolution** in `_enqueue_autolabel_messages`. Today skip-verification hardwires `bedrock:{job['bedrock_model_id']}` and ignores `auto_label.model`. The LLM family takes precedence, so a skip-verification job can be LLM-driven (Requirement 2.6):

```python
auto_label = job.get('auto_label') or {}
model = auto_label.get('model')
if not (isinstance(model, str) and model.startswith('llm:')):
    model = (f"bedrock:{job['bedrock_model_id']}"
             if skip_verification else model)
```

**Message body** gains two optional fields:

```python
if isinstance(model, str) and model.startswith('llm:'):
    message['detection_prompt'] = auto_label.get('detection_prompt') or ''
if skip_verification:
    message['per_label_prompts'] = dict(job.get('per_label_prompts') or {})
```

`_REQUIRED_FIELDS` in the consumer is **not** extended. `detection_prompt` is mode-specific, and the consumer falls back to `job['auto_label']['detection_prompt']` when the message lacks it — so messages already in flight across a deployment still work.

**`_canonical_annotation` normalization.** The `image_width`/`image_height` → `image_size` bridge currently fires only for `ObjectDetection`; Segmentation mask rendering raises `ManifestGenerationError` without it. Extend the condition to both geometry modalities:

```python
if (modality in ('ObjectDetection', 'Segmentation')
        and 'image_size' not in annotation
        and annotation.get('image_width') is not None
        and annotation.get('image_height') is not None):
    annotation['image_size'] = {...}
```

This is purely additive: it only fires when `image_size` is absent and both dimension fields are present. It cannot alter any annotation that already normalizes today (Requirement 1.7), and it is what lets a skip-verification Segmentation pre-label become a manifest entry (Requirement 8.1).

### 4. Per-image consumer (`dda_autolabel_worker`)

**Dispatch** — the third branch, and the only change to `_generate_prelabel`:

```python
def _generate_prelabel(message, job):
    model = message['model']
    if model == 'sam':
        return _generate_sam_prelabel(message, job)
    if isinstance(model, str) and model.startswith('bedrock:'):
        model_id = model.split(':', 1)[1]
        if model_id:
            return _generate_bedrock_prelabel(message, job, model_id)
    if isinstance(model, str) and model.startswith('llm:'):
        model_id = model.split(':', 1)[1]
        if model_id:
            return _generate_llm_prelabel(message, job, model_id)
    raise GenerationFailure(f'unsupported auto-label model {model!r}')
```

**`_generate_llm_prelabel`**:

```python
def _generate_llm_prelabel(message, job, model_id):
    modality = message['modality']
    label_set = message['label_set']

    # Cross-account read with the direct-access fallback (Req 9.5, 9.6).
    image_bytes, image_key = _read_image_bytes(job, message['image_s3_uri'])

    # Req 3.3: no dimensions => failure, and no model invocation.
    dimensions = _image_dimensions(image_bytes)
    if not dimensions:
        raise GenerationFailure(
            'unsupported image content: could not determine image '
            'dimensions for coordinate guidance')
    width, height = dimensions

    detection_prompt = (message.get('detection_prompt')
                        or (job.get('auto_label') or {}).get('detection_prompt')
                        or '')
    if not detection_prompt.strip():
        raise GenerationFailure('job has no detection prompt configured')

    per_label_prompts = None
    if job.get('skip_verification'):
        per_label_prompts = (message.get('per_label_prompts')
                             or job.get('per_label_prompts') or {})

    prompt = build_detection_prompt(modality, label_set, detection_prompt,
                                    width, height, per_label_prompts)

    config = get_bedrock_configuration()
    timeout = min(int(config['timeout_seconds']), BEDROCK_MAX_TIMEOUT_SECONDS)
    client = get_bedrock_client(config['region'], timeout)
    try:
        response = client.converse(
            modelId=model_id,
            messages=[{'role': 'user', 'content': [
                {'image': {'format': _image_format(image_key),
                           'source': {'bytes': image_bytes}}},
                {'text': prompt},
            ]}],
            inferenceConfig=build_inference_config(config),
        )
    except (ReadTimeoutError, ConnectTimeoutError) as exc:
        raise GenerationFailure(
            f'model invocation timed out after {timeout}s') from exc
    except Exception as exc:
        raise GenerationFailure(f'model error: {exc}') from exc

    try:
        detections = parse_guidance(_response_text(response),
                                   label_set, width, height)
        return guidance_to_prelabel(detections, modality, label_set,
                                    width, height)
    except GuidanceError as exc:
        raise GenerationFailure(str(exc)) from exc
```

Requirement 3.4 asks that timeout be **distinguishable** from model error in the recorded reason. `bedrock_common.get_bedrock_client` already builds the client with `retries={'max_attempts': 0}` and `read_timeout=timeout`, so a slow model surfaces as `botocore.exceptions.ReadTimeoutError`, caught separately and reported as `model invocation timed out after Ns`. Everything else is `model error: ...`. Retries are disabled and no branch re-invokes, satisfying "no further invocation attempts" (Requirement 3.4) and "exactly one request" (Requirement 3.1).

Requirement 3.5 (one image's failure leaves every other image unchanged) is already the batch contract: `_process_message` absorbs `GenerationFailure` per record and the handler continues the loop.

**Storage failure as a terminal resolution** (Requirement 6.2). `_process_message` currently lets a `put_object` exception escape as transient, so SQS retries and the task stays `Pending`. For the LLM family that becomes a `Failed` resolution naming the storage failure:

```python
LLM_MODEL_PREFIX = 'llm:'

# in _process_message
storage_failure_is_terminal = str(message.get('model', '')).startswith(LLM_MODEL_PREFIX)
try:
    prelabel = _generate_prelabel(message, job)
    try:
        prelabel_key = _write_prelabel(job['usecase_id'], job_id, task_id, prelabel)
    except Exception as exc:
        if not storage_failure_is_terminal:
            raise                      # today's transient/retry behavior
        raise GenerationFailure(
            f'pre-label storage failed: {exc}') from exc
    resolved = _mark_task(job_id, task_id, 'Available', prelabel_s3_key=prelabel_key)
except GenerationFailure as exc:
    ...
```

The scoping is deliberate: making storage failures terminal for every family would change SAM and Bedrock retry behavior, which Requirement 1.7 forbids. The tradeoff is that an LLM image hit by a transient S3 error is marked `Failed` rather than retried — acceptable, because a `Failed` task is still labelable from scratch (Requirement 7.5) and a permanently `Pending` task is not (Requirement 6.5).

**Everything else in this worker is unchanged**, and that is what satisfies Requirement 6 wholesale: `_write_prelabel` writes to the existing per-task key before resolution (6.1); `_mark_task` records `Failed` with a reason truncated at 1024 characters and sets no `prelabel_s3_key` (6.3, 10.2); its `ConditionExpression` resolves each task at most once so duplicate deliveries change nothing (6.4); `Pending` tasks stay withheld from labelers (6.5); and `_resolve_skip_verification_counters` runs only for the call that performed the resolution, so the counter and `review_ready` move exactly once per task (6.6).

### 5. Review and manifest (no functional change)

Requirement 7 is satisfied by not touching these paths. The labeler canvas loads `prelabel_s3_key` regardless of which model produced it; an empty `regions`/`boxes` list renders as an empty editable annotation (7.2); a `Failed` pre-label presents the bare image (7.5); existing per-modality completeness rules validate submissions (7.4); `human_annotated` stays `True` for team submissions and `False` for accepted skip-verification results (7.8, 7.9).

Requirement 8 follows from emitting the existing pre-label shapes: the manifest path is entered unchanged, `serialize_manifest` adds no LLM-specific attributes because it has no notion of pre-label origin (8.4), skip-verification includes accepted and excludes rejected images (8.5), and team jobs include every submitted annotation including ones labeled from scratch after a pre-label failure (8.6). Requirement 10.5 (all images fail ⇒ job not terminal) also needs no code: nothing transitions a job to `Failed` on pre-label failures, `review_ready` still flips when the counter drains, and the existing finalize gate rejects a review with zero accepted results.

One frontend note for Segmentation: `DdaMaskRegion.rle` is typed `number[]` in `frontend/src/services/api.ts` while the backend writes and validates a **string**. The canvas already consumes string RLE from SAM pre-labels, so behavior is correct today; the type annotation is wrong. It is corrected to `string` rather than propagated.

### 6. Failure visibility

**Job detail** (`labeling._get_dda_labeling_job`) already queries every task, so the counts are free:

```python
job['prelabel_available_count'] = sum(
    1 for t in active_tasks if t.get('prelabel_status') == 'Available')
job['prelabel_failed_count'] = sum(
    1 for t in active_tasks if t.get('prelabel_status') == 'Failed')
```

The job item is returned as-is, so `auto_label.model` and `auto_label.detection_prompt` reach the client without further plumbing (Requirements 10.1, 10.3).

**Per-task reason** (Requirement 10.4): `prelabel_status` and `prelabel_error` are threaded into the Admin_Review item payload and the labeler task response where they are not already present, so a reviewer sees why an image failed (Requirement 7.6, 7.7).

**Retention** (Requirement 10.2): `_mark_task` already truncates to 1024 characters and nothing deletes the attribute, so it lives as long as the task record.

### 7. Frontend

**`CreateLabelingJob.tsx`**

```ts
export const LLM_MODALITIES = ['Classification', 'Segmentation', 'ObjectDetection'];

export function isAutoLabelModelCompatible(modelValue: string, taskType: string): boolean {
  if (modelValue === 'sam') return SAM_MODALITIES.includes(taskType);
  if (modelValue.startsWith('llm:')) return LLM_MODALITIES.includes(taskType);
  if (modelValue.startsWith('bedrock:')) return BEDROCK_MODALITIES.includes(taskType);
  return false;
}
```

- `autoLabelOptions` gains one `llm:<id>` entry per catalog model from `apiService.getBedrockModels()`, labeled as prompt-guided and grouped apart from the plain Bedrock entries, so both modes stay reachable (Requirement 1.1, 1.2).
- When the catalog fails to load, an inline notice plus a free-text `Input` accepts a model identifier, mirroring the existing skip-verification degradation (Requirement 1.4).
- A `Textarea` for the Detection_Prompt appears when an `llm:` model is selected, marked required with a 2000-character constraint.
- `validateDdaSetup` blocks submission on an empty or whitespace-only prompt and on over-length (Requirement 2.1) — the backend re-validates regardless.
- The review step summarizes the model and the prompt.
- Payload: `auto_label: { enabled, model, detection_prompt }`.

**`api.ts`**: `createLabelingJob` params extend `auto_label` with `detection_prompt?: string`; the DDA job type gains `prelabel_available_count` / `prelabel_failed_count`.

**`LabelingDetail.tsx`**: displays the model identifier, the full stored Detection_Prompt (no truncation, Requirement 10.1), and the Available/Failed counts once at least one task has resolved (Requirement 10.3).

## Data Models

### Job record additions (LabelingJobs)

| Field | Type | Notes |
|---|---|---|
| `auto_label.model` | string | `llm:<identifier>` for the new family |
| `auto_label.detection_prompt` | string | verbatim, 1..2000 chars, present only for `llm:` |

No new top-level attributes; no migration. Existing jobs have no `detection_prompt` and take the pre-existing paths untouched.

### Task record

No schema change. `prelabel_status`, `prelabel_s3_key`, `prelabel_error`, and `autolabel_error` carry the same meanings as today.

### Pre-label artifacts

Same key (`labeling/{usecase_id}/{job_id}/prelabels/{task_id}.json`) and same per-modality shapes as the existing auto-label modes. This is the whole point: downstream code cannot tell an LLM pre-label from a SAM or Bedrock one.

### SQS message

```json
{"job_id": "labeling-ab12cd34", "task_id": "task-000007",
 "image_s3_uri": "s3://bucket/key.png",
 "modality": "Segmentation", "label_set": ["scratch", "dent"],
 "model": "llm:us.amazon.nova-pro-v1:0",
 "detection_prompt": "Find surface scratches and dents on the panel.",
 "per_label_prompts": {"scratch": "..."}}
```

`detection_prompt` present for the LLM family; `per_label_prompts` present for skip-verification. Both optional, so the consumer tolerates messages from either side of a deployment.

## Error Handling

Every failure mode resolves to one of three outcomes. There is no fourth.

| Condition | Outcome | Requirement |
|---|---|---|
| Invalid model identifier / prompt at creation | 400 with `validation_errors`, nothing persisted | 1.5, 2.3, 2.4 |
| Missing create permission / admin role | 403, nothing persisted, audit event | 9.2, 9.3 |
| Dataset image unreadable (both access paths) | task `Failed`, access reason | 9.6 |
| Image dimensions undeterminable | task `Failed`, no model call | 3.3 |
| Model timeout (`ReadTimeoutError`) | task `Failed`, `model invocation timed out after Ns` | 3.4 |
| Model error (any other exception) | task `Failed`, `model error: ...` | 3.4 |
| No parseable JSON in the response | task `Failed`, unparseable-output reason | 4.2 |
| Structural mismatch / bad class / bad geometry / >100 detections | task `Failed`, one reason naming the offender | 4.3–4.8 |
| Empty rasterization / zero-extent box | task `Failed`, conversion reason | 5.7 |
| Pre-label write fails (LLM family) | task `Failed`, storage reason | 6.2 |
| DynamoDB or other infrastructure error | `batchItemFailures` for that record; SQS retries, then DLQ | existing |
| Duplicate SQS delivery of a resolved task | no-op | 6.4 |

Failure reasons are prose naming the offending element (class name, geometry, count) so a Job_Creator can act on them. They are truncated to 1024 characters by `_mark_task`.

A job never becomes terminal because of pre-label failures. Team jobs present failed tasks for labeling from scratch; skip-verification jobs list them in Admin_Review with their failure status and exclude them from accepted results (Requirements 7.5, 7.7, 10.5).

## Correctness Properties

These are the invariants the implementation must hold for all inputs, not just the examples. They are what the property-based tests below encode.

### Property 1: Guidance round trip

For every valid Coordinate_Guidance document, parsing its serialization yields detections with identical class names, geometry types, and coordinate values: `parse_guidance(serialize_guidance(d), L, w, h) == d`.

**Validates: Requirements 4.10**

### Property 2: Validation is total and all-or-nothing

For every input text, `parse_guidance` either returns a fully valid detection list or raises `GuidanceError`; it never returns a partially validated list, and a document containing one invalid detection among any number of valid ones always raises.

**Validates: Requirements 4.3, 4.4, 4.5, 4.6, 4.7, 4.8**

### Property 3: Class closure

Every class name in a returned detection list is an exact, case-sensitive member of the Label_Set. No parse result can name a class the job does not define.

**Validates: Requirements 4.4**

### Property 4: Geometric containment of guidance

Every box in a returned detection list satisfies `left >= 0 ∧ top >= 0 ∧ left + width <= w ∧ top + height <= h` with `width > 0 ∧ height > 0`; every polygon vertex satisfies `0 <= x <= w ∧ 0 <= y <= h`.

**Validates: Requirements 4.5, 4.6**

### Property 5: Cardinality bound

A returned detection list has between 0 and 100 entries; a document with more is rejected before any per-detection validation, so the reported reason is always the cap.

**Validates: Requirements 3.2, 4.7**

### Property 6: RLE well-formedness

For every valid detection and every positive `(w, h)`, `rasterize_to_rle` produces non-negative counts summing to exactly `w * h`. Equivalently, `rle_decode` accepts every string the rasterizer emits.

**Validates: Requirements 5.2**

### Property 7: Rasterization fidelity

`rle_decode(rasterize_to_rle(d, w, h), w, h)` equals the reference dense rasterization of `d` under pixel-center sampling with the even-odd rule. The span-based optimization is observationally identical to the naive fill.

**Validates: Requirements 5.1, 5.2**

### Property 8: Emitted geometry containment

No decoded foreground pixel of any emitted region, and no emitted bounding box, extends below 0 or beyond `w` / `h` — for every input, including geometry that touches the bounds exactly.

**Validates: Requirements 5.6**

### Property 9: Detection preservation

Segmentation emits exactly one region per detection and ObjectDetection exactly one box per detection, in guidance order, with class names preserved and no merging across detections sharing a class: `len(prelabel.regions) == len(detections)`.

**Validates: Requirements 5.1, 5.3**

### Property 10: Polygon hull tightness

A polygon detection converted for ObjectDetection yields precisely the axis-aligned hull of its vertices: `left = min x`, `top = min y`, `width = max x − min x`, `height = max y − min y`.

**Validates: Requirements 5.3**

### Property 11: Empty is success, degenerate is failure

Zero detections always produce an empty-but-valid Pre_Label in the modality's shape and never an error; a geometry that rasterizes to zero pixels or truncates to a zero-extent box always produces a failure and never a Pre_Label. These two cases are disjoint and exhaustive over "produced nothing".

**Validates: Requirements 5.5, 5.7**

### Property 12: Classification totality

The derived classification is `anomaly` iff the detection list is non-empty, and `normal` otherwise — total over all detection lists.

**Validates: Requirements 5.4**

### Property 13: Resolution idempotence

For any sequence of deliveries of the same task's message, the task's `prelabel_status`, failure reason, and stored Pre_Label reference are those of the first resolution, and the skip-verification counter advances exactly once.

**Validates: Requirements 6.4, 6.6**

### Property 14: Manifest indistinguishability

For every modality, a manifest generated from LLM-origin annotations has the same entry structure and attribute names as one generated from equivalent annotations of any other origin, and passes the existing validation gate without transformation. Given identical RLE, rendered masks are pixel-identical.

**Validates: Requirements 8.2, 8.3, 8.4**

### Property 15: Failure isolation

For every job, the processing outcome of each image is independent of every other image's outcome; no failure changes any other task's `prelabel_status`.

**Validates: Requirements 3.5**

### Property 16: Existing-mode invariance

For every job whose auto-label configuration is `sam`, `bedrock:<id>`, or absent, the stored job configuration, the enqueued message body, the generated Pre_Label, and the review flow are byte-identical to their pre-change behavior.

**Validates: Requirements 1.7**

## Testing Strategy

### Property-based tests (hypothesis)

New files under `edge-cv-portal/backend/tests/`, following the existing `test_property_*.py` convention. These target the pure module, where the interesting invariants live.

`test_property_llm_guidance_parse.py`
- **Round trip** (Requirement 4.10): for generated valid guidance, `parse_guidance(serialize_guidance(d), label_set, w, h)` returns detections with identical class names, geometry types, and coordinate values.
- **Acceptance**: any guidance built from in-bounds geometry with classes drawn from the Label_Set and at most 100 detections parses successfully.
- **Rejection**: for each mutation family — class not in the Label_Set, class differing only in case, non-numeric or `NaN` coordinate, non-positive extent, out-of-bounds box, polygon with fewer than 3 vertices, out-of-bounds vertex, 101 detections, both `box` and `polygon` on one detection, neither — `parse_guidance` raises `GuidanceError` and the message names the offending element.
- **Prose and fence tolerance**: wrapping valid guidance in arbitrary text that contains no earlier parseable object still parses; a truncated object before the valid one is skipped.
- **All-or-nothing**: guidance with one invalid detection among N valid ones raises rather than returning a subset.

`test_property_llm_guidance_rasterize.py`
- **RLE well-formedness**: for any valid detection, `rasterize_to_rle` produces counts that are non-negative and sum to `width * height` — i.e. `rle_decode` accepts it.
- **Agreement with a dense reference**: `rle_decode(rasterize_to_rle(d, w, h), w, h)` equals a naive per-pixel-center reference rasterization, over small generated images.
- **In-bounds** (Requirement 5.6): no decoded foreground pixel lies outside `[0, w) × [0, h)` — implied by the length invariant and asserted directly.
- **Non-empty for non-degenerate geometry**: any box with truncated extent ≥ 1 px, and any polygon containing at least one pixel center, rasterizes to a non-empty mask.

`test_property_llm_guidance_convert.py`
- **Region count and order** (Requirement 5.1): Segmentation emits exactly one region per detection, in order, with class names preserved and no merging across same-class detections.
- **Box fidelity** (Requirement 5.3): box detections pass through with unchanged coordinates; polygon detections yield exactly the min/max hull of their vertices, which is itself in bounds.
- **Classification derivation** (Requirement 5.4): zero detections ⇒ `normal`; one or more ⇒ `anomaly`.
- **Empty guidance** (Requirement 5.5): zero detections produce an empty `regions`/`boxes` list and no error.
- **Manifest survival** (Requirement 8.2): a generated pre-label, run through `_canonical_annotation` → `serialize_manifest` → `parse_manifest`, round-trips and passes the shared validation for all three modalities.

### Example-based tests (moto)

Extending the existing suites in `edge-cv-portal/backend/tests/`:

- `test_dda_labeling_create_job.py`: `llm:` accepted for all three modalities; identifier rejections (empty, 257 chars, embedded space, control character); prompt rejections (missing, whitespace-only, 2001 chars); prompt stored byte-identical including leading/trailing whitespace and newlines; no job or task items written on rejection; `job_created` details carry `auto_label_model` and `auto_label_mode`; skip-verification still requires per-label prompts alongside the Detection_Prompt; permission and role denials.
- `test_dda_autolabel_worker.py`: LLM dispatch reached for `llm:<id>`; exactly one `converse` call per image with the image block, the verbatim prompt, the Label_Set, and the dimensions; `ReadTimeoutError` vs generic exception produce distinguishable reasons; undeterminable dimensions fail without invoking the model; a batch where record 2 fails leaves records 1 and 3 `Available`; duplicate delivery of a resolved task leaves `prelabel_status`, `prelabel_error`, and `prelabel_s3_key` untouched and does not double-count; storage failure marks `Failed` for `llm:` and still raises transient for `sam`/`bedrock:`; existing SAM and Bedrock tests pass unmodified (Requirement 1.7).
- `test_dda_labeling_worker_distribute.py`: `detection_prompt` present in enqueued messages for LLM jobs; a skip-verification job with an `llm:` model enqueues the LLM model rather than `bedrock:{bedrock_model_id}`; existing SAM/Bedrock fan-out byte-identical.
- `test_dda_labeling_worker_generate_manifest.py`: end-to-end manifest for a Segmentation team job whose annotations originate from LLM pre-labels, including PNG mask rendering through the job-wide color map and the `_canonical_annotation` Segmentation normalization; skip-verification Segmentation accepted-result manifest; a job where every pre-label failed and every task was labeled from scratch still produces one entry per submission; identical attribute structure to a non-LLM job of the same modality.
- `test_dda_labeling_admin_review.py`: failed images listed with status and excluded from accepted results; finalize with zero accepted still rejected.
- `labeling` job detail: `prelabel_available_count` / `prelabel_failed_count` correct with mixed statuses and with Inactive tasks excluded.

### Frontend tests (vitest)

- `CreateLabelingJob` helpers: `isAutoLabelModelCompatible` across all three families and all modalities; `validateDdaSetup` blocks empty, whitespace-only, and over-length prompts.
- Wizard rendering: prompt field appears only for `llm:` models; catalog-unavailable notice plus free-text entry; payload shape.
- `LabelingDetail`: model, full prompt, and both counts rendered.

### Running the suites

```
cd edge-cv-portal
python3 -m pip install -r backend/requirements-dev.txt      # pytest 8.4.2, moto 5.1.22
python3 -m pytest backend/tests/test_dda_autolabel_worker.py -q -p no:cacheprovider
cd frontend && npx vitest run
```

`moto` is absent from `/home/ubuntu/.dda-test-venv`, so the DDA labeling suites error at `conftest.py` import until dev requirements are installed. `package.json` has no `test` script; vitest runs directly.

## Requirements Traceability

| Req | Where it is satisfied |
|---|---|
| 1.1–1.3 | `autoLabelOptions` + `LLM_MODALITIES` (wizard), `AUTO_LABEL_MODEL_MODALITIES['llm']` |
| 1.4 | catalog-unavailable notice + free-text identifier input |
| 1.5 | `validate_model_identifier` in the `create_dda_job` error list |
| 1.6 | `auto_label.model` on the job item |
| 1.7 | dispatch branches added, never modified; storage-failure change scoped to `llm:`; `_canonical_annotation` change strictly additive |
| 2.1–2.5 | wizard `validateDdaSetup` + `create_dda_job` prompt validation, stored verbatim |
| 2.6 | LLM model precedence in `_enqueue_autolabel_messages`; both prompt sources in `build_detection_prompt` |
| 3.1, 3.6 | single `converse` call in `_generate_llm_prelabel`, response text to `parse_guidance` |
| 3.2 | `MAX_DETECTIONS`, box/polygon geometry model |
| 3.3 | `_image_dimensions` check before invocation |
| 3.4 | timeout vs error branches, retries disabled, no re-invocation |
| 3.5 | existing per-record `GenerationFailure` absorption |
| 4.1–4.10 | `extract_first_json`, `parse_guidance`, `serialize_guidance` |
| 5.1–5.7 | `guidance_to_prelabel`, `rasterize_to_rle`, `polygon_bounding_box` |
| 6.1, 6.3–6.6 | existing `_write_prelabel`, `_mark_task`, `_resolve_skip_verification_counters` |
| 6.2 | scoped terminal storage failure in `_process_message` |
| 7.1–7.9 | unchanged labeler canvas, Admin_Review, submission paths |
| 8.1–8.6 | unchanged manifest path plus the `_canonical_annotation` Segmentation normalization |
| 9.1–9.4 | unchanged permission/role gates; audit detail additions |
| 9.5–9.7 | `_read_image_bytes` via `get_s3_client_for_bucket`; request carries image and prompt only |
| 10.1, 10.3 | job item passthrough + pre-label counts in `_get_dda_labeling_job` |
| 10.2 | `_mark_task` 1024-character truncation |
| 10.4 | `prelabel_error` threaded into task and review payloads |
| 10.5 | no terminal transition on pre-label failure; existing finalize gate |
