"""
Bug condition exploration test for the Grounded-SAM Segmentation mask
offset (.kiro/specs/grounded-sam-mask-offset).

Property 1: Bug Condition — Segmentation Masks Align With The Prompted
Box.

**Validates: Requirements 1.1, 1.2** (unfixed-code counterexamples; the
same assertions encode Expected Behavior 2.1/2.2 and become the fix
check after task 3.1 — they must then pass unchanged).

EXPECTED TO FAIL ON UNFIXED CODE: the three geometry assertions run the
handler's real ``_segment_masks`` against the REAL MobileSAM ONNX
artifacts on deterministic synthetic images (bright rectangle at a
known off-center bbox on a dark background, detection box = rectangle
bbox) and require IoU ≥ 0.85 with mask-centroid displacement < 3 % of
the image diagonal. Their failure is the counterexample that confirms
the bug (live evidence: job ``labeling-8022a9dc``, 576×768 portrait,
mask ≈150–200 px low). DO NOT weaken the thresholds to make them pass.

The probe-matrix test is a pure diagnostic (never asserts alignment):
it drives the SAM decoder on one embedding per geometry under the
candidate coordinate conventions from the design (H1–H4) and prints
IoU per variant per geometry to pin the convention the deployed
samexporter export actually expects. Exactly one variant should score
≥ 0.85 everywhere; the fix implements that one.

PINNED MECHANISM (probe results on the real mobile_sam_20230629
artifacts): the design's four variants (a)–(d) ALL fail. A canvas-frame
control run — ``orig_im_size = (683, 1024)``, the export's tracing
geometry — places every mask EXACTLY on the prompt in canvas
coordinates (portrait prompt (85,128,341,427) → mask bbox
(86,128,340,425)), so the handler's ``coords * scale`` feed is correct
(H1/H3/H4 refuted) and the defect is isolated to the graph's mask
postprocess: its pad-crop is CONSTANT-FOLDED to the tracing shape
(683, 1024) instead of derived from ``orig_im_size``. A baked-crop
model (crop rows at 683, all 1024 cols, then resize to
``orig_im_size``) reproduces the observed mask bboxes exactly on all
three geometries. The winning convention — variant (e), H2 refined —
bypasses the graph's ``masks`` output entirely: take ``low_res_masks``
(256×256, canvas frame) and apply the official postprocess externally
(upsample to the 1024 canvas, crop the pre-padded region
``[:new_h, :new_w]``, resize to source W×H, threshold). Probe IoU:
portrait 0.9942, landscape 0.9979, square 0.9996 (displacement
≤ 0.5 px) — the convention task 3.1 implements.

TASK 2 RECLASSIFICATION (observation-first preservation): the baked
crop makes the bug condition broader than the design's ``scale ≠ 1`` —
alignment requires the ``_sam_preprocess`` resized geometry
``(new_h, new_w)`` to EQUAL (683, 1024), not ``scale = 1``. Observed on
unfixed code at scale = 1: landscape 1024×768 (resized (768, 1024))
IoU 0.8009 with a +26.0 px downward centroid shift; portrait 768×1024
(resized (1024, 768)) IoU 0.2939, displacement 138.0 px. Both joined
this file's expected-fail set (``SCALE1_MISALIGNED_GEOMETRIES``), and
bugfix.md 3.2 / the Bug Condition function were updated to
``resized(image) ≠ (683, 1024)``. The one spared geometry — 1024×683,
whose resize IS the traced crop (IoU 0.9948, 0.2 px on unfixed code) —
is the true preservation boundary, encoded as a passing test in
``test_gsam_mask_offset_preservation.py``.

Environment: needs onnxruntime + numpy + Pillow plus the MobileSAM
export bundle, downloaded once to ``/tmp/gsam-models`` (~40 MB) and
reused on later runs; skips cleanly when onnxruntime is missing or the
download fails, so the file is safe in the general suite.

Module-loading follows ``test_dda_grounded_sam_worker_utils.py``: the
worker's modules load from explicit file paths via importlib
(``gsam_utils``/``mask_utils`` registered first so the handler's
imports resolve), the worker directory never goes on ``sys.path``, and
``sys.modules['handler']`` is never populated (the sam-worker tests own
that name). The loaded handler is pointed at the extracted models via
its ``SAM_ENCODER_PATH``/``SAM_DECODER_PATH`` module attributes, with
``_SAM_SESSIONS`` reset whenever the configuration changes.
"""
import glob
import importlib.util
import math
import os
import sys
import zipfile

import pytest

np = pytest.importorskip(
    'numpy', reason='exploration test needs numpy for the real ONNX run')
pytest.importorskip(
    'onnxruntime',
    reason='exploration test needs onnxruntime for the real ONNX run')
PIL_Image = pytest.importorskip(
    'PIL.Image', reason='exploration test needs Pillow for the real ONNX run')

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
    'gsam_worker_mask_utils_offset_exploration'))

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
            'gsam_worker_handler_offset_exploration',
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
# Deterministic synthetic geometries (design: Examples / Test Cases)
# ---------------------------------------------------------------------------

# (id, width, height, rectangle bbox) — bright rectangle biased toward the
# upper-left so the predicted down/right displacement is measurable.
GEOMETRIES = [
    ('portrait-576x768-incident', 576, 768,
     {'left': 64.0, 'top': 96.0, 'width': 192.0, 'height': 224.0}),
    ('landscape-768x576', 768, 576,
     {'left': 96.0, 'top': 64.0, 'width': 224.0, 'height': 192.0}),
    ('square-512x512', 512, 512,
     {'left': 64.0, 'top': 64.0, 'width': 160.0, 'height': 160.0}),
]

# Scale = 1 geometries RECLASSIFIED into the expected-fail set by task 2
# (observation-first): their resized (new_h, new_w) — (768, 1024) and
# (1024, 768) — differ from the export's constant-folded pad-crop
# (683, 1024), so the graph's masks postprocess still warps them despite
# scale = 1 (observed unfixed: IoU 0.8009 / +26.0 px down, and
# IoU 0.2939 / 138.0 px). Property 1 cases only — the probe matrix keeps
# its original three geometries (the convention is already pinned).
SCALE1_MISALIGNED_GEOMETRIES = [
    ('landscape-1024x768-scale1', 1024, 768,
     {'left': 128.0, 'top': 96.0, 'width': 256.0, 'height': 224.0}),
    ('portrait-768x1024-scale1', 768, 1024,
     {'left': 96.0, 'top': 128.0, 'width': 224.0, 'height': 256.0}),
]

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
# Property 1 assertions — EXPECTED TO FAIL on unfixed code
# ---------------------------------------------------------------------------


class TestProperty1MasksAlignWithPromptedBox:
    """
    Feature: grounded-sam-mask-offset, Property 1: Bug Condition —
    Segmentation Masks Align With The Prompted Box.

    **Validates: Requirements 1.1, 1.2** (and, post-fix, 2.1/2.2 —
    the identical assertions become the fix check).
    """

    @pytest.mark.parametrize(
        ('width', 'height', 'box'),
        [pytest.param(w, h, b, id=case_id)
         for case_id, w, h, b in GEOMETRIES + SCALE1_MISALIGNED_GEOMETRIES],
    )
    def test_segment_masks_aligns_mask_with_detection_box(
            self, sam, width, height, box):
        image = _synthetic_image(width, height, box)
        detection = {'label_index': 0, 'score': 0.9, 'box': dict(box)}

        regions = sam._segment_masks(image, [detection])

        diagonal = math.hypot(width, height)
        limit = _CENTROID_DIAGONAL_FRACTION * diagonal
        scale = 1024.0 / max(width, height)
        truth_center = _centroid(_rectangle_mask(width, height, box))
        predicted = ((scale - 1.0) * truth_center[0],
                     (scale - 1.0) * truth_center[1])

        assert regions, (
            f'counterexample {width}x{height}: _segment_masks dropped the '
            f'mask entirely (empty after thresholding) for box {box} — '
            f'IoU 0.0'
        )
        _, rle = regions[0]
        mask = _decoded_rle_mask(rle, width, height)
        iou, displacement, vector = _alignment_report(
            mask, width, height, box)

        print(
            f'\n[counterexample {width}x{height} scale={scale:.4f}] '
            f'IoU={iou:.4f} (need >= {_IOU_THRESHOLD}), '
            f'centroid displacement={displacement:.1f}px '
            f'vector={vector} (limit {limit:.1f}px), '
            f'H1-predicted (scale-1)*position displacement '
            f'~({predicted[0]:.1f}, {predicted[1]:.1f})px'
        )

        assert iou >= _IOU_THRESHOLD and displacement < limit, (
            f'counterexample {width}x{height} (scale {scale:.4f}): mask '
            f'IoU={iou:.4f} (threshold {_IOU_THRESHOLD}), centroid '
            f'displacement={displacement:.1f}px vector={vector} '
            f'(limit {limit:.1f}px = 3% of diagonal {diagonal:.1f}px); '
            f'H1 predicts ~({predicted[0]:.1f}, {predicted[1]:.1f})px'
        )


# ---------------------------------------------------------------------------
# Probe matrix — diagnostic only, pins the decoder coordinate convention
# ---------------------------------------------------------------------------

# variant key -> (label, coords frame, orig_im_size convention)
_PROBE_VARIANTS = [
    ('a', 'original-frame coords, orig_im_size=[H,W]', 'original', 'hw'),
    ('b', '1024-frame coords, orig_im_size=[H,W] (CURRENT handler)',
     'scaled', 'hw'),
    ('c', '1024-frame coords, swapped orig_im_size=[W,H]', 'scaled', 'wh'),
    ('d', '1024-frame coords, resized-frame orig_im_size=[new_h,new_w] '
          '+ external resize (samexporter-native)', 'scaled', 'resized'),
    ('e', '1024-frame coords, low_res_masks + external official '
          'postprocess (canvas upsample -> crop [:new_h,:new_w] -> '
          'resize to source)', 'scaled', 'low-res-external'),
]

_VARIANT_HYPOTHESIS = {
    'a': 'H1 — decoder expects ORIGINAL-frame coords '
         '(handler double-transforms today)',
    'b': 'current handler convention (official SAM export semantics) — '
         'if this wins, re-hypothesize',
    'c': 'H3 — orig_im_size expects [W, H] order',
    'd': 'H2 — samexporter-native resized-frame orig_im_size '
         '+ external mask warp',
    'e': 'H2 refined — the graph\'s masks postprocess pad-crop is '
         'constant-folded to the tracing shape (683, 1024), so bypass '
         'it: low_res_masks + external official postprocess',
}

# The export's baked (constant-folded) pad-crop shape, discovered by the
# canvas-frame control run: feeding this exact orig_im_size makes the
# graph's final resize the identity on its crop, and every mask then
# lands exactly on the prompt in canvas coordinates.
_TRACED_CROP_HW = (683.0, 1024.0)


def _resize_float(array, width, height):
    """Bilinear resize of a float32 2D array to (H, W) via Pillow."""
    return np.asarray(
        PIL_Image.fromarray(np.ascontiguousarray(array, dtype=np.float32))
        .resize((width, height), PIL_Image.BILINEAR))


def _logits_to_source_mask(logits, width, height):
    """
    Map decoder mask logits of any spatial shape onto the (H, W) source
    grid: exact match passes through, a transposed match transposes
    (gives the [W, H] probe its best-shot reading), anything else is
    bilinear-resized (the samexporter-native external warp).
    """
    rows, cols = logits.shape
    if (rows, cols) == (height, width):
        aligned = logits
    elif (rows, cols) == (width, height):
        aligned = logits.T
    else:
        aligned = _resize_float(logits, width, height)
    return (aligned > 0.0).astype(np.uint8)


def _mask_bbox(mask):
    """(x0, y0, x1, y1) of a binary mask's support, or None when empty."""
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def _run_decoder_feeds(decoder, embeddings, point_coords, orig_im_size):
    """One decoder run; returns outputs by name."""
    feeds = {}
    for graph_input in decoder.get_inputs():
        name = graph_input.name
        if name == 'image_embeddings':
            feeds[name] = embeddings
        elif name == 'point_coords':
            feeds[name] = point_coords
        elif name == 'point_labels':
            feeds[name] = np.asarray([[2.0, 3.0]], dtype=np.float32)
        elif name == 'mask_input':
            feeds[name] = np.zeros((1, 1, 256, 256), dtype=np.float32)
        elif name == 'has_mask_input':
            feeds[name] = np.zeros(1, dtype=np.float32)
        elif name == 'orig_im_size':
            feeds[name] = np.asarray(orig_im_size, dtype=np.float32)
    outputs = decoder.run(None, feeds)
    return dict(zip(
        (output.name for output in decoder.get_outputs()), outputs))


def _probe_decoder_variant(sam, decoder, embeddings, box, scale,
                           width, height, coords_frame, size_convention,
                           encoder_size):
    """
    One decoder run under an explicit coordinate convention; returns the
    best mask (by predicted IoU) mapped onto the source grid.
    """
    left = float(box['left'])
    top = float(box['top'])
    right = left + float(box['width'])
    bottom = top + float(box['height'])
    factor = 1.0 if coords_frame == 'original' else scale
    point_coords = np.asarray(
        [[[left * factor, top * factor], [right * factor, bottom * factor]]],
        dtype=np.float32,
    )

    new_w = max(1, int(round(width * scale)))    # _sam_preprocess rounding
    new_h = max(1, int(round(height * scale)))
    orig_im_size = {
        'hw': [float(height), float(width)],
        'wh': [float(width), float(height)],
        'resized': [float(new_h), float(new_w)],
        'low-res-external': [float(height), float(width)],  # unused output
    }[size_convention]

    by_name = _run_decoder_feeds(decoder, embeddings, point_coords,
                                 orig_im_size)
    scores = np.asarray(by_name['iou_predictions']).reshape(-1)
    best = int(np.argmax(scores))

    if size_convention == 'low-res-external':
        # Official segment-anything postprocess, applied OUTSIDE the
        # graph: low_res_masks live on the (encoder_size, encoder_size)
        # padded canvas at 256x256 — upsample to the canvas, cut the
        # pre-padding content region, and resize to the source image.
        low = np.asarray(by_name['low_res_masks'])
        logits = low.reshape(-1, low.shape[-2], low.shape[-1])[best]
        canvas = _resize_float(logits, encoder_size, encoder_size)
        content = canvas[:new_h, :new_w]
        return (_resize_float(content, width, height) > 0.0).astype(np.uint8)

    stack = np.asarray(by_name['masks'])
    rows, cols = stack.shape[-2], stack.shape[-1]
    logits = stack.reshape(-1, rows, cols)[best]
    return _logits_to_source_mask(logits, width, height)


def test_probe_matrix_pins_decoder_convention(sam):
    """
    Diagnostic probe (design Test Case 4): IoU per coordinate-convention
    variant per geometry against the real decoder. Prints the matrix and
    the winning convention; never asserts alignment — task 3.1 reads the
    numbers to implement the fix.

    **Validates: Requirements 1.1, 1.2** (empirically pins the
    convention behind the displacement; not a pass/fail alignment gate).
    """
    encoder, decoder = sam._get_sam_sessions()
    encoder_size = sam._sam_encoder_input_size(encoder)
    expects_hwc = sam._sam_encoder_expects_hwc(encoder)
    encoder_input_name = encoder.get_inputs()[0].name

    results = {}  # variant key -> {geometry id -> iou}
    displacement_current = {}  # geometry id -> (iou, disp, vector, scale)
    canvas_control = {}  # geometry id -> (prompt bbox, mask bbox) on canvas
    for case_id, width, height, box in GEOMETRIES:
        image = _synthetic_image(width, height, box)
        tensor, scale = sam._sam_preprocess(image, encoder_size, expects_hwc)
        embeddings = encoder.run(None, {encoder_input_name: tensor})[0]
        for key, _label, coords_frame, size_convention in _PROBE_VARIANTS:
            mask = _probe_decoder_variant(
                sam, decoder, embeddings, box, scale, width, height,
                coords_frame, size_convention, encoder_size)
            iou, displacement, vector = _alignment_report(
                mask, width, height, box)
            results.setdefault(key, {})[case_id] = iou
            if key == 'b':
                displacement_current[case_id] = (
                    iou, displacement, vector, scale)

        # Canvas-frame control: with orig_im_size pinned to the export's
        # baked crop shape, the graph's final resize is the identity on
        # its crop — if the mask then lands exactly on the (scaled)
        # prompt, the coords*scale feed is proven correct and the defect
        # is the constant-folded pad-crop, not the prompt transform.
        left, top = float(box['left']) * scale, float(box['top']) * scale
        right = (float(box['left']) + float(box['width'])) * scale
        bottom = (float(box['top']) + float(box['height'])) * scale
        control = _run_decoder_feeds(
            decoder, embeddings,
            np.asarray([[[left, top], [right, bottom]]], dtype=np.float32),
            list(_TRACED_CROP_HW))
        control_masks = np.asarray(control['masks'])
        control_best = int(np.argmax(
            np.asarray(control['iou_predictions']).reshape(-1)))
        control_logits = control_masks.reshape(
            -1, control_masks.shape[-2], control_masks.shape[-1]
        )[control_best]
        canvas_control[case_id] = (
            (round(left), round(top), round(right), round(bottom)),
            _mask_bbox((control_logits > 0.0).astype(np.uint8)),
        )

    print('\n=== PROBE MATRIX: IoU(decoded mask, ground-truth rectangle) '
          'per decoder coordinate convention ===')
    header = f'{"variant":<10}' + ''.join(
        f'{case_id:<28}' for case_id, *_ in GEOMETRIES)
    print(header)
    for key, label, *_ in _PROBE_VARIANTS:
        row = f'({key})' + ' ' * 7 + ''.join(
            f'{results[key][case_id]:<28.4f}' for case_id, *_ in GEOMETRIES)
        print(row)
        print(f'           {label}')

    print('\n--- current-behavior (variant b) displacement per geometry ---')
    for case_id, (iou, displacement, vector, scale) in \
            displacement_current.items():
        print(f'{case_id}: scale={scale:.4f} IoU={iou:.4f} '
              f'displacement={displacement:.1f}px vector={vector}')

    print(f'\n--- canvas-frame control (orig_im_size={_TRACED_CROP_HW}: '
          'identity resize on the baked crop) ---')
    for case_id, (prompt_bbox, mask_bbox) in canvas_control.items():
        print(f'{case_id}: canvas prompt bbox={prompt_bbox} '
              f'-> mask bbox={mask_bbox} '
              f'(match proves coords*scale correct; defect is the '
              f'constant-folded pad-crop)')

    winners = [
        key for key, *_ in _PROBE_VARIANTS
        if min(results[key].values()) >= _IOU_THRESHOLD
    ]
    verdict = winners or 'NONE — re-hypothesize (design H1-H4 all refuted)'
    print(f'\nwinning variant(s) (IoU >= {_IOU_THRESHOLD} on ALL '
          f'geometries): {verdict}')
    for key in winners:
        print(f'  ({key}) => {_VARIANT_HYPOTHESIS[key]}')

    # Diagnostic only: the probe must run on every geometry/variant, but
    # alignment itself is Property 1's job (the parametrized test above).
    assert set(results) == {key for key, *_ in _PROBE_VARIANTS}
    assert all(len(by_geometry) == len(GEOMETRIES)
               for by_geometry in results.values())
