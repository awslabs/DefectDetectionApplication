"""
dda_sam_worker — container-image Lambda running CPU ONNX SAM inference.

Invoked synchronously by dda_autolabel_worker with an image payload and
returns class-agnostic region proposals as RLE-encoded masks at source
resolution. Since SAM is class-agnostic, every region is returned with
``class: null``; the Labeler_Interface (or Admin_Review) assigns a
Label_Set class to each kept region downstream.

Handler contract (design: Auto-Labeler pipeline):

    event    {"image_bytes_base64": "<b64>"            # one of the two
              | "image_s3_presigned_url": "https://...",
              "max_regions": 20}                        # optional
    returns  {"regions": [{"class": null, "rle": "<counts>",
                           "score": <float>}],
              "image_width": W, "image_height": H}

The RLE format matches the canonical shared-layer implementation
(``dda_manifest.rle_encode``): COCO-style uncompressed counts over the
column-major flattening, background run first.

Model loading: the automatic-mask-generation pipeline runs against ONNX
encoder/decoder exports (e.g. MobileSAM) found under the directory in
the ``SAM_MODEL_PATH`` env var (files matching ``*encoder*.onnx`` /
``*decoder*.onnx``, or explicit ``SAM_ENCODER_PATH`` /
``SAM_DECODER_PATH`` overrides). The Dockerfile bakes the MobileSAM
exports into the image at build time.

Heavy dependencies (numpy, onnxruntime, Pillow) are imported lazily so
the module itself — and the pure logic in ``mask_utils`` — can be
imported and tested without them.

Requirements: 8.1, 8.2
"""
from __future__ import annotations

import base64
import glob
import json
import logging
import os
import urllib.request
from typing import Dict, List, Optional, Tuple

from mask_utils import (
    DEFAULT_MAX_REGIONS,
    build_point_grid,
    runs_to_rle,
    select_regions,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration (env-var tunable) ---------------------------------------
SAM_MODEL_PATH = os.environ.get('SAM_MODEL_PATH', '/opt/models')
SAM_ENCODER_PATH = os.environ.get('SAM_ENCODER_PATH')  # explicit override
SAM_DECODER_PATH = os.environ.get('SAM_DECODER_PATH')  # explicit override
# Automatic mask generation prompt grid density
POINTS_PER_SIDE = int(os.environ.get('SAM_POINTS_PER_SIDE', '8'))
# SAM mask logit threshold (the model's canonical cutoff is 0.0)
MASK_LOGIT_THRESHOLD = float(os.environ.get('SAM_MASK_THRESHOLD', '0.0'))
# Candidates whose predicted IoU falls below this are discarded
PRED_IOU_THRESHOLD = float(os.environ.get('SAM_PRED_IOU_THRESHOLD', '0.7'))
# Greedy NMS threshold for near-duplicate proposals
NMS_IOU_THRESHOLD = float(os.environ.get('SAM_NMS_IOU_THRESHOLD', '0.85'))
# Regions smaller than this fraction of the image are dropped as noise
MIN_AREA_FRACTION = float(os.environ.get('SAM_MIN_AREA_FRACTION', '0.0005'))
# Fallback encoder input resolution when the ONNX graph has dynamic dims
DEFAULT_ENCODER_SIZE = int(os.environ.get('SAM_ENCODER_SIZE', '1024'))
# Timeout for fetching a presigned image URL
URL_FETCH_TIMEOUT_SECONDS = int(os.environ.get('SAM_URL_FETCH_TIMEOUT', '30'))

# SAM pixel normalization constants (ImageNet-derived, per the original repo)
PIXEL_MEAN = (123.675, 116.28, 103.53)
PIXEL_STD = (58.395, 57.12, 57.375)

# Cached ONNX sessions (persist across warm invocations)
_SESSIONS: Optional[Tuple[object, object]] = None


# ---------------------------------------------------------------------------
# Model discovery and session loading
# ---------------------------------------------------------------------------

def _find_model_file(explicit_path: Optional[str], pattern: str) -> str:
    """Resolve a model file from an explicit path or a glob under SAM_MODEL_PATH."""
    if explicit_path:
        if not os.path.isfile(explicit_path):
            raise FileNotFoundError(f'model file not found: {explicit_path}')
        return explicit_path
    matches = sorted(glob.glob(os.path.join(SAM_MODEL_PATH, pattern)))
    if not matches:
        raise FileNotFoundError(
            f'no file matching {pattern!r} under {SAM_MODEL_PATH!r}; set '
            'SAM_MODEL_PATH or SAM_ENCODER_PATH/SAM_DECODER_PATH'
        )
    return matches[0]


def _get_sessions() -> Tuple[object, object]:
    """Load (and cache) the encoder/decoder onnxruntime sessions."""
    global _SESSIONS
    if _SESSIONS is None:
        import onnxruntime as ort  # lazy: container-only dependency

        encoder_path = _find_model_file(SAM_ENCODER_PATH, '*encoder*.onnx')
        decoder_path = _find_model_file(SAM_DECODER_PATH, '*decoder*.onnx')
        logger.info('Loading SAM ONNX models: encoder=%s decoder=%s',
                    encoder_path, decoder_path)
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ['CPUExecutionProvider']
        encoder = ort.InferenceSession(encoder_path, options, providers=providers)
        decoder = ort.InferenceSession(decoder_path, options, providers=providers)
        _SESSIONS = (encoder, decoder)
    return _SESSIONS


def _encoder_expects_hwc(encoder) -> bool:
    """
    Whether the encoder takes a rank-3 HWC image rather than a rank-4
    NCHW tensor.

    samexporter-style exports (the MobileSAM bundle this image ships by
    default) declare `input_image: ['image_height', 'image_width', 3]`
    and perform normalization and square padding *inside* the graph, so
    they take the resized RGB image as-is. Exports without baked-in
    preprocessing declare `[1, 3, S, S]` and need the caller to
    normalize, pad and transpose.
    """
    return len(encoder.get_inputs()[0].shape) == 3


def _encoder_input_size(encoder) -> int:
    """
    Encoder square input resolution, from the graph or the default.

    Layout matters here: for a rank-3 HWC input the trailing dimension is
    the channel count, so reading the last two dims would mistake the 3
    channels for the resolution and shrink every image to 3 pixels. The
    spatial dims are leading for HWC and trailing for NCHW; when they are
    dynamic (as in the samexporter exports) the default applies.
    """
    shape = encoder.get_inputs()[0].shape
    spatial = shape[:2] if len(shape) == 3 else shape[-2:]
    for dim in spatial:
        if isinstance(dim, int) and dim > 0:
            return dim
    return DEFAULT_ENCODER_SIZE


# ---------------------------------------------------------------------------
# Image loading and preprocessing
# ---------------------------------------------------------------------------

def _load_image_bytes(event: Dict) -> bytes:
    """Resolve the image bytes from the event payload."""
    encoded = event.get('image_bytes_base64')
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True)
        except Exception as e:
            raise ValueError(f'invalid image_bytes_base64: {e}')
    url = event.get('image_s3_presigned_url')
    if url:
        if not url.lower().startswith('https://'):
            raise ValueError('image_s3_presigned_url must be an https URL')
        with urllib.request.urlopen(url, timeout=URL_FETCH_TIMEOUT_SECONDS) as response:
            return response.read()
    raise ValueError(
        'event must include image_bytes_base64 or image_s3_presigned_url'
    )


def _decode_image(image_bytes: bytes):
    """Decode image bytes into an RGB numpy array (H, W, 3) uint8."""
    import io

    import numpy as np
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    return np.asarray(image, dtype=np.uint8)


def _preprocess(image_rgb, encoder_size: int, expects_hwc: bool = False):
    """
    Resize the longest side to the encoder resolution and return the
    encoder input tensor plus the resize scale.

    Two export conventions are supported (see `_encoder_expects_hwc`):

    - `expects_hwc`: the graph normalizes and pads internally, so the
      resized RGB image is returned unchanged as rank-3 HWC float32.
      Normalizing or padding here would corrupt the input.
    - otherwise: pad bottom/right to a square, normalize with the SAM
      pixel statistics, and return a rank-4 NCHW float32 tensor.

    Point prompts are scaled into the resized frame by the returned
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

    mean = np.asarray(PIXEL_MEAN, dtype=np.float32)
    std = np.asarray(PIXEL_STD, dtype=np.float32)
    normalized = (resized - mean) / std

    padded = np.zeros((encoder_size, encoder_size, 3), dtype=np.float32)
    padded[:new_h, :new_w, :] = normalized
    tensor = padded.transpose(2, 0, 1)[np.newaxis, ...]  # 1x3xSxS
    return tensor, scale


# ---------------------------------------------------------------------------
# Inference (automatic mask generation over a point-prompt grid)
# ---------------------------------------------------------------------------

def _run_decoder(decoder, embeddings, point_xy, scale: float,
                 width: int, height: int):
    """
    Run the SAM ONNX decoder for one point prompt. Returns
    (masks, iou_predictions) with masks at the source resolution.
    """
    import numpy as np

    # Point in the resized-image coordinate frame plus the canonical
    # padding point (label -1) the ONNX export expects.
    point_coords = np.asarray(
        [[[point_xy[0] * scale, point_xy[1] * scale], [0.0, 0.0]]],
        dtype=np.float32,
    )
    point_labels = np.asarray([[1.0, -1.0]], dtype=np.float32)

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


def _generate_candidates(image_rgb, max_prompts: Optional[int] = None) -> List[Dict]:
    """
    Standard automatic-mask-generation pipeline: embed the image once,
    prompt the decoder with an evenly spaced point grid, keep the
    best-predicted mask per prompt, threshold logits at the model
    cutoff, and return flat 0/1 candidates for post-processing.
    """
    import numpy as np

    encoder, decoder = _get_sessions()
    encoder_size = _encoder_input_size(encoder)
    height, width = image_rgb.shape[:2]

    tensor, scale = _preprocess(
        image_rgb, encoder_size, _encoder_expects_hwc(encoder))
    encoder_input_name = encoder.get_inputs()[0].name
    embeddings = encoder.run(None, {encoder_input_name: tensor})[0]

    points = build_point_grid(width, height, POINTS_PER_SIDE)
    if max_prompts is not None:
        points = points[:max_prompts]

    candidates: List[Dict] = []
    for point_xy in points:
        masks, iou_predictions = _run_decoder(
            decoder, embeddings, point_xy, scale, width, height
        )
        scores = np.asarray(iou_predictions).reshape(-1)
        best = int(np.argmax(scores))
        score = float(scores[best])
        if score < PRED_IOU_THRESHOLD:
            continue
        logits = np.asarray(masks).reshape(-1, height, width)[best]
        binary = (logits > MASK_LOGIT_THRESHOLD).astype(np.uint8)
        area = int(binary.sum())
        if area == 0:
            continue
        candidates.append({
            'mask': binary.tobytes(),   # row-major flat 0/1 bytes (NMS packing)
            'mask_2d': binary,          # kept for fast vectorized RLE encoding
            'score': score,
            'area': area,
        })
    return candidates


def _rle_encode_fast(candidate: Dict) -> str:
    """Vectorized RLE encoding (same format as mask_utils.rle_encode)."""
    import numpy as np

    mask_2d = candidate['mask_2d']
    flat = np.asarray(mask_2d, dtype=np.uint8).flatten(order='F')
    change_points = np.flatnonzero(np.diff(flat)) + 1
    boundaries = np.concatenate(([0], change_points, [flat.size]))
    run_lengths = np.diff(boundaries)
    return runs_to_rle(int(flat[0]), run_lengths.tolist())


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: Dict, context=None) -> Dict:
    """
    Return class-agnostic SAM region proposals for the supplied image.

    Raises ValueError on malformed input so the synchronous caller
    (dda_autolabel_worker) records the invocation as a pre-label
    generation failure (req 8.5).
    """
    if isinstance(event, str):
        event = json.loads(event)
    if not isinstance(event, dict):
        raise ValueError('event must be a JSON object')

    max_regions = event.get('max_regions', DEFAULT_MAX_REGIONS)
    try:
        max_regions = int(max_regions)
    except (TypeError, ValueError):
        raise ValueError(f'invalid max_regions: {event.get("max_regions")!r}')
    if max_regions < 1:
        raise ValueError('max_regions must be positive')

    image_bytes = _load_image_bytes(event)
    image_rgb = _decode_image(image_bytes)
    height, width = image_rgb.shape[:2]
    logger.info('Generating SAM proposals: image=%dx%d grid=%dx%d',
                width, height, POINTS_PER_SIDE, POINTS_PER_SIDE)

    candidates = _generate_candidates(image_rgb)
    regions = select_regions(
        candidates,
        width=width,
        height=height,
        max_regions=max_regions,
        min_area_fraction=MIN_AREA_FRACTION,
        iou_threshold=NMS_IOU_THRESHOLD,
        encode=_rle_encode_fast,
    )
    logger.info('SAM proposals: %d candidates -> %d regions',
                len(candidates), len(regions))

    return {
        'regions': regions,
        'image_width': width,
        'image_height': height,
    }
