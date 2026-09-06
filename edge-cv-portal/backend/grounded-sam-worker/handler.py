"""
dda_grounded_sam_worker — container-image Lambda running CPU ONNX
Grounded-SAM inference (Grounding DINO + SAM-family mask model).

Invoked synchronously by dda_autolabel_worker with an image payload, a
Prompt_Map, and a modality; returns *classified* pre-label regions —
every region carries a ``class`` drawn from the Prompt_Map's labels, so
labelers verify annotations instead of drawing and classifying them.

Handler contract (design: Grounded_SAM_Worker event/response):

    event    {"image_bytes_base64": "<b64>"            # one of the two
              | "image_s3_presigned_url": "https://...",
              "prompts": [{"label": "dent",
                           "prompt": "small surface dent"}, ...],
              "modality": "Segmentation" | "ObjectDetection",
              "max_detections": 20}                    # optional
    returns  Segmentation:
             {"regions": [{"class": "dent", "rle": "<counts>",
                           "score": <float>}],
              "image_width": W, "image_height": H}
             ObjectDetection (no mask pass):
             {"regions": [{"class": "dent", "score": <float>,
                           "box": {"left", "top", "width", "height"}}],
              "image_width": W, "image_height": H}

Pipeline: all prompts are joined into one Grounding DINO caption
('small surface dent. scratch.') and the model runs once per image;
each detection query is attributed back to exactly one prompt via the
caption's phrase token spans (pure logic in ``gsam_utils``), thresholded
(Box_Threshold / Text_Threshold), converted to clamped pixel boxes,
deduplicated per label, and capped. For Segmentation each retained box
prompts the SAM ONNX decoder (canonical two-point encoding, labels 2/3)
and the thresholded mask is RLE-encoded at source resolution in the
portal's canonical form (``mask_utils.runs_to_rle``).

Model loading: files are baked into the image at build time and
resolved under ``GROUNDED_SAM_MODEL_PATH`` (globs
``grounding_dino*.onnx``, ``tokenizer.json``, ``*encoder*.onnx``,
``*decoder*.onnx``) or through the explicit-path overrides
(``GROUNDING_DINO_MODEL_PATH``, ``GROUNDING_DINO_TOKENIZER_PATH``,
``SAM_ENCODER_PATH``, ``SAM_DECODER_PATH``).

Heavy dependencies (numpy, onnxruntime, Pillow, tokenizers) are
imported lazily so the module itself — and the pure logic in
``gsam_utils`` / ``mask_utils`` — can be imported and tested without
them, and so malformed events are rejected before any model import.

Requirements: 3.1, 3.2, 3.3, 3.5, 3.8, 3.9, 3.10 (grounded-sam-autolabel)
"""
from __future__ import annotations

import base64
import glob
import json
import logging
import os
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

from gsam_utils import (
    DEFAULT_BOX_NMS_IOU,
    DEFAULT_BOX_THRESHOLD,
    DEFAULT_MAX_DETECTIONS,
    DEFAULT_TEXT_THRESHOLD,
    attribute_detection,
    build_caption,
    cxcywh_to_pixel_box,
    normalize_prompts,
    phrase_token_spans,
    select_detections,
)
from mask_utils import runs_to_rle

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration (env-var tunable, read once at import) -------------------
GROUNDED_SAM_MODEL_PATH = os.environ.get('GROUNDED_SAM_MODEL_PATH', '/opt/models')
# Explicit model-file overrides (globs under GROUNDED_SAM_MODEL_PATH otherwise)
GROUNDING_DINO_MODEL_PATH = os.environ.get('GROUNDING_DINO_MODEL_PATH')
GROUNDING_DINO_TOKENIZER_PATH = os.environ.get('GROUNDING_DINO_TOKENIZER_PATH')
SAM_ENCODER_PATH = os.environ.get('SAM_ENCODER_PATH')
SAM_DECODER_PATH = os.environ.get('SAM_DECODER_PATH')
# Detection_Thresholds (req 3.5): minimum overall detection confidence and
# minimum prompt-attribution confidence — Grounded-SAM demo defaults
BOX_THRESHOLD = float(os.environ.get(
    'GROUNDED_SAM_BOX_THRESHOLD', str(DEFAULT_BOX_THRESHOLD)))
TEXT_THRESHOLD = float(os.environ.get(
    'GROUNDED_SAM_TEXT_THRESHOLD', str(DEFAULT_TEXT_THRESHOLD)))
# Greedy per-label NMS threshold for near-duplicate boxes of one label
NMS_IOU_THRESHOLD = float(os.environ.get(
    'GROUNDED_SAM_NMS_IOU_THRESHOLD', str(DEFAULT_BOX_NMS_IOU)))
# Cap on returned detections (highest scores kept)
MAX_DETECTIONS = int(os.environ.get(
    'GROUNDED_SAM_MAX_DETECTIONS', str(DEFAULT_MAX_DETECTIONS)))
# SAM mask logit threshold (the model's canonical cutoff is 0.0)
MASK_LOGIT_THRESHOLD = float(os.environ.get('GROUNDED_SAM_MASK_THRESHOLD', '0.0'))
# Grounding DINO resize target for the image's shortest edge — applies only
# when the ONNX export declares dynamic spatial dims; fixed-shape exports
# (e.g. onnx-community grounding-dino-tiny-ONNX pins pixel_values to
# 1x3x800x800) dictate their own input size (see _dino_input_hw).
DINO_SIZE = int(os.environ.get('GROUNDED_SAM_DINO_SIZE', '800'))
# Longest-edge cap during the shortest-edge resize (the model's canonical 1333)
DINO_MAX_SIZE = 1333
# Timeout for fetching a presigned image URL
URL_FETCH_TIMEOUT_SECONDS = int(os.environ.get('GROUNDED_SAM_URL_FETCH_TIMEOUT', '30'))

# Grounding DINO pixel normalization (ImageNet mean/std over [0, 1] pixels,
# per the model repo's preprocessor_config.json)
DINO_PIXEL_MEAN = (0.485, 0.456, 0.406)
DINO_PIXEL_STD = (0.229, 0.224, 0.225)
# SAM pixel normalization constants (ImageNet-derived, per the original repo)
SAM_PIXEL_MEAN = (123.675, 116.28, 103.53)
SAM_PIXEL_STD = (58.395, 57.12, 57.375)
# Fallback SAM encoder input resolution when the ONNX graph has dynamic dims
SAM_ENCODER_FALLBACK_SIZE = 1024

MODALITY_SEGMENTATION = 'Segmentation'
MODALITY_OBJECT_DETECTION = 'ObjectDetection'
SUPPORTED_MODALITIES = (MODALITY_SEGMENTATION, MODALITY_OBJECT_DETECTION)

# Cached model sessions and tokenizer (persist across warm invocations)
_DINO_SESSION: Optional[object] = None
_TOKENIZER: Optional[object] = None
_SAM_SESSIONS: Optional[Tuple[object, object]] = None


# ---------------------------------------------------------------------------
# Model discovery and session loading
# ---------------------------------------------------------------------------

def _find_model_file(explicit_path: Optional[str], pattern: str,
                     override_env: str) -> str:
    """Resolve a model file from an explicit path or a glob under GROUNDED_SAM_MODEL_PATH."""
    if explicit_path:
        if not os.path.isfile(explicit_path):
            raise FileNotFoundError(f'model file not found: {explicit_path}')
        return explicit_path
    matches = sorted(glob.glob(os.path.join(GROUNDED_SAM_MODEL_PATH, pattern)))
    if not matches:
        raise FileNotFoundError(
            f'no file matching {pattern!r} under {GROUNDED_SAM_MODEL_PATH!r}; '
            f'set GROUNDED_SAM_MODEL_PATH or {override_env}'
        )
    return matches[0]


def _get_dino_session():
    """Load (and cache) the Grounding DINO onnxruntime session."""
    global _DINO_SESSION
    if _DINO_SESSION is None:
        import onnxruntime as ort  # lazy: container-only dependency

        model_path = _find_model_file(
            GROUNDING_DINO_MODEL_PATH, 'grounding_dino*.onnx',
            'GROUNDING_DINO_MODEL_PATH')
        logger.info('Loading Grounding DINO ONNX model: %s', model_path)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _DINO_SESSION = ort.InferenceSession(
            model_path, options, providers=['CPUExecutionProvider'])
    return _DINO_SESSION


def _get_tokenizer():
    """Load (and cache) the Grounding DINO tokenizer (tokenizers Rust wheel)."""
    global _TOKENIZER
    if _TOKENIZER is None:
        from tokenizers import Tokenizer  # lazy: container-only dependency

        tokenizer_path = _find_model_file(
            GROUNDING_DINO_TOKENIZER_PATH, 'tokenizer.json',
            'GROUNDING_DINO_TOKENIZER_PATH')
        logger.info('Loading Grounding DINO tokenizer: %s', tokenizer_path)
        _TOKENIZER = Tokenizer.from_file(tokenizer_path)
    return _TOKENIZER


def _get_sam_sessions() -> Tuple[object, object]:
    """Load (and cache) the SAM encoder/decoder onnxruntime sessions."""
    global _SAM_SESSIONS
    if _SAM_SESSIONS is None:
        import onnxruntime as ort  # lazy: container-only dependency

        encoder_path = _find_model_file(
            SAM_ENCODER_PATH, '*encoder*.onnx', 'SAM_ENCODER_PATH')
        decoder_path = _find_model_file(
            SAM_DECODER_PATH, '*decoder*.onnx', 'SAM_DECODER_PATH')
        logger.info('Loading SAM ONNX models: encoder=%s decoder=%s',
                    encoder_path, decoder_path)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ['CPUExecutionProvider']
        encoder = ort.InferenceSession(encoder_path, options, providers=providers)
        decoder = ort.InferenceSession(decoder_path, options, providers=providers)
        _SAM_SESSIONS = (encoder, decoder)
    return _SAM_SESSIONS


def _sam_encoder_expects_hwc(encoder) -> bool:
    """
    Whether the SAM encoder takes a rank-3 HWC image rather than a
    rank-4 NCHW tensor.

    samexporter-style exports (the MobileSAM bundle this image ships by
    default) declare `input_image: ['image_height', 'image_width', 3]`
    and perform normalization and square padding *inside* the graph, so
    they take the resized RGB image as-is. Exports without baked-in
    preprocessing declare `[1, 3, S, S]` and need the caller to
    normalize, pad and transpose.
    """
    return len(encoder.get_inputs()[0].shape) == 3


def _sam_encoder_input_size(encoder) -> int:
    """
    SAM encoder square input resolution, from the graph or the fallback.

    Layout matters here: for a rank-3 HWC input the trailing dimension is
    the channel count, so reading the last two dims would mistake the 3
    channels for the resolution and shrink every image to 3 pixels. The
    spatial dims are leading for HWC and trailing for NCHW; when they are
    dynamic (as in the samexporter exports) the fallback applies.
    """
    shape = encoder.get_inputs()[0].shape
    spatial = shape[:2] if len(shape) == 3 else shape[-2:]
    for dim in spatial:
        if isinstance(dim, int) and dim > 0:
            return dim
    return SAM_ENCODER_FALLBACK_SIZE


# ---------------------------------------------------------------------------
# Image loading and preprocessing
# ---------------------------------------------------------------------------

def _resolve_image_source(event: Dict) -> Dict:
    """
    Validate and resolve the event's image source without fetching.

    Pure (no network, no heavy imports) so malformed events raise before
    any model import (req 3.8).
    """
    encoded = event.get('image_bytes_base64')
    if encoded:
        try:
            return {'bytes': base64.b64decode(encoded, validate=True)}
        except Exception as e:
            raise ValueError(f'invalid image_bytes_base64: {e}')
    url = event.get('image_s3_presigned_url')
    if url:
        if not isinstance(url, str) or not url.lower().startswith('https://'):
            raise ValueError('image_s3_presigned_url must be an https URL')
        return {'url': url}
    raise ValueError(
        'event must include image_bytes_base64 or image_s3_presigned_url'
    )


def _load_image_bytes(source: Dict) -> bytes:
    """Materialize the image bytes from a resolved image source."""
    if 'bytes' in source:
        return source['bytes']
    with urllib.request.urlopen(source['url'],
                                timeout=URL_FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def _decode_image(image_bytes: bytes):
    """Decode image bytes into an RGB numpy array (H, W, 3) uint8."""
    import io

    import numpy as np
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    return np.asarray(image, dtype=np.uint8)


def _dino_input_hw() -> Optional[Tuple[int, int]]:
    """
    The (height, width) the DINO export pins for ``pixel_values``, or None
    when the export declares dynamic spatial dims.

    Some ONNX exports fix the image input shape — the onnx-community
    grounding-dino-tiny export declares ``pixel_values`` as
    ``[1, 3, 800, 800]`` — and reject any other size. Dynamic exports
    carry symbolic dims (strings) or zero/negative placeholders there.
    """
    session = _get_dino_session()
    for graph_input in session.get_inputs():
        if graph_input.name != 'pixel_values':
            continue
        shape = graph_input.shape
        if len(shape) != 4:
            return None
        height, width = shape[2], shape[3]
        if isinstance(height, int) and isinstance(width, int) \
                and height > 0 and width > 0:
            return height, width
        return None
    return None


def _dino_preprocess(image_rgb):
    """
    Build the Grounding DINO ``pixel_values`` tensor.

    When the export pins fixed spatial dims (see ``_dino_input_hw``), the
    image is resized to exactly that size (aspect stretched — verified against
    the pinned onnx-community export: its ``pred_boxes`` stay normalized
    over the full source image, so a square stretch round-trips correctly
    while letterbox padding does not). Otherwise the canonical dynamic
    convention applies: shortest edge to DINO_SIZE with the longest edge
    capped at DINO_MAX_SIZE (aspect preserved). Pixels are scaled to
    [0, 1], ImageNet-normalized, returned as a 1x3xHxW float32 tensor.
    """
    import numpy as np
    from PIL import Image

    height, width = image_rgb.shape[:2]
    fixed_hw = _dino_input_hw()
    if fixed_hw is not None:
        new_h, new_w = fixed_hw
    else:
        scale = DINO_SIZE / min(width, height)
        if scale * max(width, height) > DINO_MAX_SIZE:
            scale = DINO_MAX_SIZE / max(width, height)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))

    resized = np.asarray(
        Image.fromarray(image_rgb).resize((new_w, new_h), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0
    mean = np.asarray(DINO_PIXEL_MEAN, dtype=np.float32)
    std = np.asarray(DINO_PIXEL_STD, dtype=np.float32)
    normalized = (resized - mean) / std
    return np.ascontiguousarray(normalized.transpose(2, 0, 1))[np.newaxis, ...]


def _sam_preprocess(image_rgb, encoder_size: int, expects_hwc: bool = False):
    """
    Resize the longest side to the SAM encoder resolution and return the
    encoder input tensor plus the resize scale.

    Two export conventions are supported (see `_sam_encoder_expects_hwc`):

    - `expects_hwc`: the graph normalizes and pads internally, so the
      resized RGB image is returned unchanged as rank-3 HWC float32.
      Normalizing or padding here would corrupt the input.
    - otherwise: pad bottom/right to a square, normalize with the SAM
      pixel statistics, and return a rank-4 NCHW float32 tensor.

    Box prompts are scaled into the resized frame by the returned
    `scale` in both cases.
    """
    import numpy as np
    from PIL import Image

    height, width = image_rgb.shape[:2]
    scale = encoder_size / max(width, height)
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))

    resized = np.asarray(
        Image.fromarray(image_rgb).resize((new_w, new_h), Image.BILINEAR),
        dtype=np.float32,
    )

    if expects_hwc:
        return resized, scale

    mean = np.asarray(SAM_PIXEL_MEAN, dtype=np.float32)
    std = np.asarray(SAM_PIXEL_STD, dtype=np.float32)
    normalized = (resized - mean) / std

    padded = np.zeros((encoder_size, encoder_size, 3), dtype=np.float32)
    padded[:new_h, :new_w, :] = normalized
    tensor = padded.transpose(2, 0, 1)[np.newaxis, ...]  # 1x3xSxS
    return tensor, scale


# ---------------------------------------------------------------------------
# Grounding DINO detection (one forward per image)
# ---------------------------------------------------------------------------

def _marker_token_ids(tokenizer) -> Tuple[Set[int], Set[int]]:
    """
    The caption's separator ('.') and special ([CLS]/[SEP]/[PAD]) token
    ids for the phrase-span walk. [UNK] deliberately stays out of the
    break set so an unknown token inside a phrase does not split its span.
    """
    separator_ids: Set[int] = set()
    dot_id = tokenizer.token_to_id('.')
    if dot_id is not None:
        separator_ids.add(dot_id)
    special_ids: Set[int] = set()
    for token in ('[CLS]', '[SEP]', '[PAD]'):
        token_id = tokenizer.token_to_id(token)
        if token_id is not None:
            special_ids.add(token_id)
    return separator_ids, special_ids


def _encode_caption(tokenizer, caption: str):
    """
    Tokenize the caption into the DINO graph's int64 (1, N) text tensors.

    Some tokenizer configurations emit no type ids; the graph still wants
    a `token_type_ids` tensor, which is all zeros for a single sequence.
    """
    import numpy as np

    encoding = tokenizer.encode(caption)
    input_ids = np.asarray([encoding.ids], dtype=np.int64)
    attention_mask = np.asarray([encoding.attention_mask], dtype=np.int64)
    type_ids = getattr(encoding, 'type_ids', None)
    if type_ids and len(type_ids) == len(encoding.ids):
        token_type_ids = np.asarray([list(type_ids)], dtype=np.int64)
    else:
        token_type_ids = np.zeros_like(input_ids)
    return input_ids, attention_mask, token_type_ids


def _run_grounding_dino(pixel_values, input_ids, attention_mask, token_type_ids):
    """
    One Grounding DINO forward. Returns the batch (logits, pred_boxes).

    The feed is built from the graph's own declared inputs — some exports
    omit `token_type_ids` (or add `pixel_mask`); feeding exactly what the
    graph asks for handles both. Outputs are matched by name with a
    positional fallback.
    """
    import numpy as np

    session = _get_dino_session()
    available = {
        'pixel_values': pixel_values,
        'input_ids': input_ids,
        'attention_mask': attention_mask,
        'token_type_ids': token_type_ids,
        # No padding is applied, so a pixel mask (when the export wants
        # one) is all-ones at the resized resolution.
        'pixel_mask': np.ones((1,) + pixel_values.shape[-2:], dtype=np.int64),
    }
    feeds = {}
    for graph_input in session.get_inputs():
        name = graph_input.name
        if name not in available:
            raise ValueError(f'unsupported Grounding DINO graph input: {name!r}')
        feeds[name] = available[name]

    outputs = session.run(None, feeds)
    by_name = dict(zip((output.name for output in session.get_outputs()), outputs))
    logits = by_name.get('logits')
    pred_boxes = by_name.get('pred_boxes')
    if logits is None or pred_boxes is None:  # positional fallback
        logits, pred_boxes = outputs[0], outputs[1]
    return logits, pred_boxes


def _detect(image_rgb, caption: str, phrases: List[str],
            max_detections: int) -> List[Dict]:
    """
    Run the detection pipeline once: tokenize the caption, locate the
    phrase token spans, run Grounding DINO, attribute every query to its
    best phrase, threshold, convert to clamped pixel boxes, and select
    (per-label NMS + cap).

    Returns [{'label_index', 'score', 'box'}] in descending score order;
    `label_index` indexes the phrase (= Prompt_Map entry) list.
    """
    import numpy as np

    height, width = image_rgb.shape[:2]
    tokenizer = _get_tokenizer()
    input_ids, attention_mask, token_type_ids = _encode_caption(tokenizer, caption)
    separator_ids, special_ids = _marker_token_ids(tokenizer)
    spans = phrase_token_spans(input_ids[0].tolist(), separator_ids, special_ids)
    if len(spans) != len(phrases):
        # One span per phrase is what makes attribution well-defined
        # (req 3.3); a prompt with inner sentence punctuation (e.g. an
        # embedded '.') breaks the alignment, so fail the invocation
        # descriptively instead of mislabeling detections.
        raise ValueError(
            f'caption token spans ({len(spans)}) do not align with the '
            f'{len(phrases)} prompts; a prompt likely contains inner '
            'sentence punctuation'
        )

    pixel_values = _dino_preprocess(image_rgb)
    logits, pred_boxes = _run_grounding_dino(
        pixel_values, input_ids, attention_mask, token_type_ids)

    with np.errstate(over='ignore'):  # exp overflow saturates to score 0.0
        scores = 1.0 / (1.0 + np.exp(-np.asarray(logits, dtype=np.float32)[0]))
    boxes = np.asarray(pred_boxes, dtype=np.float32)[0]

    # Vectorized pre-filter: a query's span max is bounded by its row max,
    # so rows whose overall max misses both thresholds cannot survive
    # attribute_detection — skip the pure-python span walk for the vast
    # majority of the model's queries.
    floor = min(BOX_THRESHOLD, TEXT_THRESHOLD)
    plausible = np.flatnonzero(scores.max(axis=1) >= floor)

    candidates: List[Dict] = []
    for query_index in plausible:
        attributed = attribute_detection(
            scores[query_index].tolist(), spans, BOX_THRESHOLD, TEXT_THRESHOLD)
        if attributed is None:
            continue
        label_index, score = attributed
        box = cxcywh_to_pixel_box(boxes[query_index].tolist(), width, height)
        if box is None:
            continue
        candidates.append({'label_index': label_index, 'score': score, 'box': box})

    logger.info('Grounding DINO: %d/%d plausible queries -> %d candidates',
                len(plausible), scores.shape[0], len(candidates))
    return select_detections(candidates, max_detections=max_detections,
                             iou_threshold=NMS_IOU_THRESHOLD)


# ---------------------------------------------------------------------------
# SAM mask pass (Segmentation only)
# ---------------------------------------------------------------------------

def _run_sam_decoder(decoder, embeddings, box: Dict, scale: float,
                     width: int, height: int):
    """
    Run the SAM ONNX decoder for one box prompt. Returns
    (masks, iou_predictions) with masks at the source resolution.

    The box rides the canonical two-point encoding — top-left labeled 2,
    bottom-right labeled 3 — scaled into the encoder's resized frame the
    same way point prompts are.
    """
    import numpy as np

    left = float(box['left'])
    top = float(box['top'])
    right = left + float(box['width'])
    bottom = top + float(box['height'])
    point_coords = np.asarray(
        [[[left * scale, top * scale], [right * scale, bottom * scale]]],
        dtype=np.float32,
    )
    point_labels = np.asarray([[2.0, 3.0]], dtype=np.float32)

    feeds = {}
    for graph_input in decoder.get_inputs():
        name = graph_input.name
        if name == 'image_embeddings':
            feeds[name] = embeddings
        elif name == 'point_coords':
            feeds[name] = point_coords
        elif name == 'point_labels':
            feeds[name] = point_labels
        elif name == 'mask_input':
            feeds[name] = np.zeros((1, 1, 256, 256), dtype=np.float32)
        elif name == 'has_mask_input':
            feeds[name] = np.zeros(1, dtype=np.float32)
        elif name == 'orig_im_size':
            feeds[name] = np.asarray([height, width], dtype=np.float32)

    outputs = decoder.run(None, feeds)
    masks, iou_predictions = outputs[0], outputs[1]
    return masks, iou_predictions


def _rle_encode_fast(mask_2d) -> str:
    """Vectorized RLE encoding (same format as mask_utils.rle_encode)."""
    import numpy as np

    flat = np.asarray(mask_2d, dtype=np.uint8).flatten(order='F')
    change_points = np.flatnonzero(np.diff(flat)) + 1
    boundaries = np.concatenate(([0], change_points, [flat.size]))
    run_lengths = np.diff(boundaries)
    return runs_to_rle(int(flat[0]), run_lengths.tolist())


def _segment_masks(image_rgb, detections: List[Dict]) -> List[Tuple[Dict, str]]:
    """
    Convert each retained detection's box into a source-resolution
    canonical RLE mask: embed the image once, then decode per box.

    Detections whose thresholded mask comes back empty are dropped —
    they carry nothing a labeler could verify (the sibling sam-worker's
    zero-area posture).
    """
    if not detections:
        return []

    import numpy as np

    encoder, decoder = _get_sam_sessions()
    encoder_size = _sam_encoder_input_size(encoder)
    height, width = image_rgb.shape[:2]

    tensor, scale = _sam_preprocess(
        image_rgb, encoder_size, _sam_encoder_expects_hwc(encoder))
    encoder_input_name = encoder.get_inputs()[0].name
    embeddings = encoder.run(None, {encoder_input_name: tensor})[0]

    results: List[Tuple[Dict, str]] = []
    for detection in detections:
        masks, iou_predictions = _run_sam_decoder(
            decoder, embeddings, detection['box'], scale, width, height)
        pred_scores = np.asarray(iou_predictions).reshape(-1)
        best = int(np.argmax(pred_scores))
        mask_logits = np.asarray(masks).reshape(-1, height, width)[best]
        binary = (mask_logits > MASK_LOGIT_THRESHOLD).astype(np.uint8)
        if int(binary.sum()) == 0:
            logger.info(
                'Dropping detection with empty mask (label_index=%d score=%.3f)',
                detection['label_index'], detection['score'])
            continue
        results.append((detection, _rle_encode_fast(binary)))
    return results


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: Dict, context=None) -> Dict:
    """
    Return classified Grounded-SAM pre-label regions for the supplied
    image and Prompt_Map.

    Raises ValueError on malformed input — before any heavy import — so
    the synchronous caller (dda_autolabel_worker) records the invocation
    as a pre-label generation failure (req 3.8). Zero retained
    detections return an empty `regions` list as a success (req 3.10).
    """
    if isinstance(event, str):
        event = json.loads(event)
    if not isinstance(event, dict):
        raise ValueError('event must be a JSON object')

    source = _resolve_image_source(event)
    prompts = normalize_prompts(event.get('prompts'))
    caption, phrases = build_caption([entry['prompt'] for entry in prompts])
    modality = event.get('modality')
    if modality not in SUPPORTED_MODALITIES:
        raise ValueError(
            f"modality must be 'Segmentation' or 'ObjectDetection', "
            f'got {modality!r}'
        )
    max_detections = event.get('max_detections', MAX_DETECTIONS)
    try:
        max_detections = int(max_detections)
    except (TypeError, ValueError):
        raise ValueError(f'invalid max_detections: {event.get("max_detections")!r}')
    if max_detections < 1:
        raise ValueError('max_detections must be positive')

    image_bytes = _load_image_bytes(source)
    image_rgb = _decode_image(image_bytes)
    height, width = image_rgb.shape[:2]
    logger.info('Grounded-SAM inference: image=%dx%d prompts=%d modality=%s',
                width, height, len(prompts), modality)

    detections = _detect(image_rgb, caption, phrases, max_detections)

    if modality == MODALITY_OBJECT_DETECTION:
        regions = [
            {
                'class': prompts[detection['label_index']]['label'],
                'score': detection['score'],
                'box': detection['box'],
            }
            for detection in detections
        ]
    else:
        regions = [
            {
                'class': prompts[detection['label_index']]['label'],
                'score': detection['score'],
                'rle': rle,
            }
            for detection, rle in _segment_masks(image_rgb, detections)
        ]
    logger.info('Grounded-SAM result: %d detections -> %d regions',
                len(detections), len(regions))

    return {
        'regions': regions,
        'image_width': width,
        'image_height': height,
    }
