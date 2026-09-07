"""
Preservation property tests for the Grounded-SAM Segmentation mask
offset (.kiro/specs/grounded-sam-mask-offset) — task 2, written BEFORE
the fix, observation-first.

Property 2: Preservation — Non-Bug Inputs Unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5** (the real-model
preservation boundary; the validation/RLE/selection domains are covered
by the unmodified pure-logic suite, see baseline below).

PASSES ON UNFIXED CODE and must keep passing after task 3.1 — it pins
the one Segmentation geometry the defect spares.

RECORDED OBSERVATIONS (UNFIXED code, real MobileSAM artifacts under
/tmp/gsam-models, handler's real ``_segment_masks``):

- The design's original scale = 1 preservation boundary is REFUTED.
  Task 1 isolated the defect to the export's in-graph mask postprocess:
  its pad-crop is CONSTANT-FOLDED to the tracing shape (683, 1024)
  instead of derived from ``orig_im_size``, so alignment depends on the
  ``_sam_preprocess`` resized geometry ``(new_h, new_w)`` matching that
  baked shape — not on ``scale`` being 1. Observed at scale = 1:
    * landscape 1024×768 → resized (768, 1024) ≠ (683, 1024):
      IoU 0.8009, centroid displacement 26.0 px, vector (−0.3, +26.0)
      — MISALIGNED (rows stretched by 768/683)
    * portrait 768×1024 → resized (1024, 768) ≠ (683, 1024):
      IoU 0.2939, centroid displacement 138.0 px, vector (−51.9, +127.9)
      — MISALIGNED
  Both are therefore BUG-CONDITION inputs (bugfix.md 3.2 and the Bug
  Condition function were updated by task 2) and live as expected-fail
  Property 1 cases in ``test_gsam_mask_offset_exploration.py``.

- The TRUE preservation boundary is the export's traced-crop geometry:
    * landscape 1024×683 → resized (683, 1024) == the baked crop, where
      the constant-folded postprocess is the identity:
      IoU 0.9948, centroid displacement 0.2 px, vector (−0.2, +0.1)
      — ALIGNED on unfixed code. That observation is what this file
      encodes (IoU ≥ 0.85, centroid < 3 % of the diagonal).

- Pure-logic suite baseline (UNFIXED code):
  ``python3 -m pytest tests/test_dda_grounded_sam_worker_utils.py -q``
  → 23 passed (includes the mask_utils drift-guard byte-identity;
  ``mask_utils.py`` is not touched by this spec).

Environment and module loading mirror the exploration file (sibling
precedent: test files stand alone, helpers duplicated, never imported
across test modules): real artifacts cached under ``/tmp/gsam-models``,
clean skip when onnxruntime or the download is unavailable, worker
modules loaded from explicit file paths via importlib with the worker
directory never on ``sys.path`` and ``sys.modules['handler']`` never
populated.
"""
import glob
import importlib.util
import math
import os
import sys
import zipfile

import pytest

np = pytest.importorskip(
    'numpy', reason='preservation test needs numpy for the real ONNX run')
pytest.importorskip(
    'onnxruntime',
    reason='preservation test needs onnxruntime for the real ONNX run')
pytest.importorskip(
    'PIL.Image', reason='preservation test needs Pillow for the real ONNX run')

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, '..'))
_GSAM_WORKER_DIR = os.path.join(_BACKEND, 'grounded-sam-worker')
_SHARED_LAYER = os.path.join(_BACKEND, 'layers', 'shared', 'python')
if _SHARED_LAYER not in sys.path:
    sys.path.insert(0, _SHARED_LAYER)

import dda_manifest  # noqa: E402  (canonical RLE decoder, shared layer)

# ---------------------------------------------------------------------------
# Worker module loading (importlib by explicit path — no sys.path games)
# ---------------------------------------------------------------------------


def _load_module_from(file_path, module_name):
    """Load a module instance from an explicit file path (no sys.path)."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The handler's `from gsam_utils import ...` / `from mask_utils import ...`
# must resolve; setdefault keeps us compatible with the sibling test files
# registering their own (byte-identical — drift-guarded) instances first.
sys.modules.setdefault('gsam_utils', _load_module_from(
    os.path.join(_GSAM_WORKER_DIR, 'gsam_utils.py'), 'gsam_utils'))
sys.modules.setdefault('mask_utils', _load_module_from(
    os.path.join(_GSAM_WORKER_DIR, 'mask_utils.py'),
    'gsam_worker_mask_utils_offset_preservation'))

# Environment variables the handler reads at import time — scrubbed during
# load so host state cannot leak into the module globals.
_HANDLER_ENV_PREFIXES = ('GROUNDED_SAM_', 'GROUNDING_DINO_')
_HANDLER_ENV_EXACT = ('SAM_ENCODER_PATH', 'SAM_DECODER_PATH')


def _load_gsam_handler():
    """Load a fresh grounded-sam handler instance with a clean env."""
    saved = {}
    for key in list(os.environ):
        if key.startswith(_HANDLER_ENV_PREFIXES) or key in _HANDLER_ENV_EXACT:
            saved[key] = os.environ.pop(key)
    try:
        return _load_module_from(
            os.path.join(_GSAM_WORKER_DIR, 'handler.py'),
            'gsam_worker_handler_offset_preservation',
        )
    finally:
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# Real MobileSAM ONNX artifacts (cached under /tmp/gsam-models)
# ---------------------------------------------------------------------------

_MODEL_CACHE_DIR = os.environ.get('GSAM_MODEL_CACHE_DIR', '/tmp/gsam-models')
_SAM_ARCHIVE_URL = (
    'https://huggingface.co/vietanhdev/segment-anything-onnx-models/'
    'resolve/main/mobile_sam_20230629.zip'
)
_DOWNLOAD_TIMEOUT_SECONDS = 300


def _find_cached_models():
    """(encoder, decoder) paths under the cache dir, or None."""
    encoders = sorted(glob.glob(
        os.path.join(_MODEL_CACHE_DIR, '**', '*encoder*.onnx'),
        recursive=True))
    decoders = sorted(glob.glob(
        os.path.join(_MODEL_CACHE_DIR, '**', '*decoder*.onnx'),
        recursive=True))
    if encoders and decoders:
        return encoders[0], decoders[0]
    return None


def _ensure_models():
    """
    Return (encoder_path, decoder_path), downloading + extracting the
    MobileSAM archive on first use. Skips (never fails) when the
    download is impossible so the file stays safe in the general suite.
    """
    cached = _find_cached_models()
    if cached:
        return cached

    import urllib.request

    os.makedirs(_MODEL_CACHE_DIR, exist_ok=True)
    archive_path = os.path.join(_MODEL_CACHE_DIR, 'mobile_sam_20230629.zip')
    try:
        with urllib.request.urlopen(
                _SAM_ARCHIVE_URL,
                timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response, \
                open(archive_path, 'wb') as archive_file:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                archive_file.write(chunk)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(_MODEL_CACHE_DIR)
    except Exception as e:  # network-restricted host: skip, don't fail
        pytest.skip(f'MobileSAM archive unavailable ({_SAM_ARCHIVE_URL}): {e}')
    finally:
        if os.path.exists(archive_path):
            os.remove(archive_path)

    cached = _find_cached_models()
    if not cached:
        pytest.skip(
            f'MobileSAM archive had no *encoder*.onnx/*decoder*.onnx '
            f'under {_MODEL_CACHE_DIR}'
        )
    return cached


@pytest.fixture(scope='module')
def sam(request):
    """The real handler module wired to the real MobileSAM artifacts."""
    encoder_path, decoder_path = _ensure_models()
    handler = _load_gsam_handler()
    handler.SAM_ENCODER_PATH = encoder_path
    handler.SAM_DECODER_PATH = decoder_path
    handler._SAM_SESSIONS = None  # force re-resolve from the paths above
    yield handler
    handler._SAM_SESSIONS = None


# ---------------------------------------------------------------------------
# The preserved geometry (observation-first: the ONE aligned geometry)
# ---------------------------------------------------------------------------

# The export's constant-folded pad-crop shape (H, W), pinned by the task 1
# probe: the graph's masks postprocess crops to exactly this shape
# regardless of orig_im_size, so it is the identity only here.
_TRACED_CROP_HW = (683, 1024)

# 1024×683 landscape: scale = 1024/1024 = 1, resized (new_h, new_w) =
# (683, 1024) == the baked crop. Bright rectangle at a known off-center
# bbox (same construction as the exploration file's geometries).
_PRESERVED_GEOMETRY = ('traced-crop-1024x683', 1024, 683,
                       {'left': 128.0, 'top': 85.0,
                        'width': 256.0, 'height': 192.0})

_BACKGROUND_GRAY = 20
_RECTANGLE_GRAY = 230
_IOU_THRESHOLD = 0.85
_CENTROID_DIAGONAL_FRACTION = 0.03


def _synthetic_image(width, height, box):
    """Dark background with a bright rectangle at the given bbox."""
    image = np.full((height, width, 3), _BACKGROUND_GRAY, dtype=np.uint8)
    left, top = int(box['left']), int(box['top'])
    right, bottom = left + int(box['width']), top + int(box['height'])
    image[top:bottom, left:right, :] = _RECTANGLE_GRAY
    return image


def _rectangle_mask(width, height, box):
    """Ground-truth binary mask of the rectangle, shape (H, W)."""
    mask = np.zeros((height, width), dtype=np.uint8)
    left, top = int(box['left']), int(box['top'])
    right, bottom = left + int(box['width']), top + int(box['height'])
    mask[top:bottom, left:right] = 1
    return mask


def _mask_iou(mask_a, mask_b):
    intersection = int(np.logical_and(mask_a, mask_b).sum())
    union = int(np.logical_or(mask_a, mask_b).sum())
    return (intersection / union) if union else 0.0


def _centroid(mask):
    """(x, y) centroid of a binary mask, or None when empty."""
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return float(xs.mean()), float(ys.mean())


def _decoded_rle_mask(rle, width, height):
    """dda_manifest RLE -> (H, W) uint8 mask (decode is row-major)."""
    flat = np.frombuffer(
        bytes(dda_manifest.rle_decode(rle, width, height)), dtype=np.uint8)
    return flat.reshape(height, width)


def _alignment_report(mask, width, height, box):
    """(iou, displacement, (dx, dy) or None) of mask vs the rectangle."""
    truth = _rectangle_mask(width, height, box)
    iou = _mask_iou(mask, truth)
    mask_center = _centroid(mask)
    truth_center = _centroid(truth)
    if mask_center is None:
        return iou, float('inf'), None
    dx = mask_center[0] - truth_center[0]
    dy = mask_center[1] - truth_center[1]
    return iou, math.hypot(dx, dy), (dx, dy)


# ---------------------------------------------------------------------------
# Property 2 — PASSES on unfixed code, must keep passing after the fix
# ---------------------------------------------------------------------------


class TestProperty2TracedCropGeometryStaysAligned:
    """
    Feature: grounded-sam-mask-offset, Property 2: Preservation —
    Non-Bug Inputs Unchanged (the real-model boundary geometry).

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
    """

    def test_traced_crop_geometry_mask_stays_aligned(self, sam):
        """
        Segmentation on the traced-crop geometry (1024×683 → resized
        (683, 1024) == the export's baked pad-crop) is aligned on
        UNFIXED code — observed IoU 0.9948, centroid displacement
        0.2 px — and must stay aligned after the fix (bugfix.md 3.2).
        """
        case_id, width, height, box = _PRESERVED_GEOMETRY

        # Boundary premise guard: this geometry only pins preservation
        # if the handler's own preprocessing maps it onto the export's
        # baked crop shape. Computed with _sam_preprocess's arithmetic
        # from the real encoder graph.
        encoder, _decoder = sam._get_sam_sessions()
        encoder_size = sam._sam_encoder_input_size(encoder)
        scale = encoder_size / max(width, height)
        resized_hw = (max(1, int(round(height * scale))),
                      max(1, int(round(width * scale))))
        assert resized_hw == _TRACED_CROP_HW, (
            f'{case_id}: resized geometry {resized_hw} no longer matches '
            f'the traced crop {_TRACED_CROP_HW} (encoder size '
            f'{encoder_size}) — the preservation boundary premise broke'
        )

        image = _synthetic_image(width, height, box)
        detection = {'label_index': 0, 'score': 0.9, 'box': dict(box)}

        regions = sam._segment_masks(image, [detection])

        assert regions, (
            f'{case_id}: _segment_masks dropped the mask entirely '
            f'(empty after thresholding) for box {box} — this geometry '
            f'was aligned (IoU 0.9948) on unfixed code'
        )
        returned_detection, rle = regions[0]
        assert returned_detection is detection  # contract: detection echoed

        mask = _decoded_rle_mask(rle, width, height)
        iou, displacement, vector = _alignment_report(
            mask, width, height, box)
        diagonal = math.hypot(width, height)
        limit = _CENTROID_DIAGONAL_FRACTION * diagonal

        print(
            f'\n[preservation {case_id} scale={scale:.4f} '
            f'resized={resized_hw}] IoU={iou:.4f} '
            f'(need >= {_IOU_THRESHOLD}), centroid '
            f'displacement={displacement:.1f}px vector={vector} '
            f'(limit {limit:.1f}px) — unfixed baseline was '
            f'IoU 0.9948 / 0.2px'
        )

        assert iou >= _IOU_THRESHOLD and displacement < limit, (
            f'{case_id}: preservation regression — IoU={iou:.4f} '
            f'(threshold {_IOU_THRESHOLD}), centroid displacement='
            f'{displacement:.1f}px vector={vector} (limit {limit:.1f}px); '
            f'this geometry (resized == baked crop {_TRACED_CROP_HW}) was '
            f'aligned on unfixed code (IoU 0.9948, 0.2 px) and must stay '
            f'aligned'
        )
