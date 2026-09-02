"""
Unit tests for the dda_sam_worker pure-Python logic (mask_utils and the
handler's input validation).

These tests run without onnxruntime / numpy / Pillow installed: the
sam-worker module guards its heavy imports, and everything exercised
here is standard library only.

Key invariant: the worker's RLE encoding must be byte-for-byte identical
to the canonical shared-layer `dda_manifest.rle_encode` so SAM region
proposals plug straight into the portal annotation model.

Feature: dda-data-labeling, Task 10.2
Requirements: 8.1, 8.2
"""
import os
import random
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, '..'))
_SAM_WORKER_DIR = os.path.join(_BACKEND, 'sam-worker')
_SHARED_LAYER = os.path.join(_BACKEND, 'layers', 'shared', 'python')
for path in (_SAM_WORKER_DIR, _SHARED_LAYER):
    if path not in sys.path:
        sys.path.insert(0, path)

import dda_manifest  # noqa: E402  (canonical RLE implementation)
import mask_utils  # noqa: E402


# ---------------------------------------------------------------------------
# RLE encoding matches the canonical shared-layer implementation
# ---------------------------------------------------------------------------

class TestRleEncodeMatchesSharedLayer:
    def test_empty_mask(self):
        mask = [0] * 12
        assert mask_utils.rle_encode(mask, 4, 3) == dda_manifest.rle_encode(mask, 4, 3)
        assert mask_utils.rle_encode(mask, 4, 3) == '12'

    def test_full_mask_starts_with_zero_background(self):
        mask = [1] * 6
        encoded = mask_utils.rle_encode(mask, 3, 2)
        assert encoded == dda_manifest.rle_encode(mask, 3, 2)
        assert encoded == '0 6'

    def test_known_pattern_column_major(self):
        # 2x2 mask, row-major [1, 0, 0, 1] -> column-major [1, 0, 0, 1]
        mask = [1, 0, 0, 1]
        encoded = mask_utils.rle_encode(mask, 2, 2)
        assert encoded == dda_manifest.rle_encode(mask, 2, 2)
        assert encoded == '0 1 2 1'

    def test_random_masks_match_and_round_trip(self):
        rng = random.Random(42)
        for _ in range(25):
            width = rng.randint(1, 12)
            height = rng.randint(1, 12)
            mask = [rng.randint(0, 1) for _ in range(width * height)]
            encoded = mask_utils.rle_encode(mask, width, height)
            assert encoded == dda_manifest.rle_encode(mask, width, height)
            decoded = dda_manifest.rle_decode(encoded, width, height)
            assert list(decoded) == mask

    def test_rejects_bad_dimensions_and_length(self):
        with pytest.raises(ValueError):
            mask_utils.rle_encode([0], 0, 1)
        with pytest.raises(ValueError):
            mask_utils.rle_encode([0, 1], 3, 3)


class TestRunsToRle:
    def test_foreground_first_prepends_zero(self):
        assert mask_utils.runs_to_rle(1, [3, 2]) == '0 3 2'

    def test_background_first(self):
        assert mask_utils.runs_to_rle(0, [4, 1]) == '4 1'

    def test_rejects_negative_runs(self):
        with pytest.raises(ValueError):
            mask_utils.runs_to_rle(0, [-1])


# ---------------------------------------------------------------------------
# Mask arithmetic
# ---------------------------------------------------------------------------

class TestMaskIou:
    def test_identical_masks(self):
        mask = [0, 1, 1, 0]
        assert mask_utils.mask_iou(mask, mask) == 1.0

    def test_disjoint_masks(self):
        assert mask_utils.mask_iou([1, 0, 0, 0], [0, 0, 0, 1]) == 0.0

    def test_both_empty(self):
        assert mask_utils.mask_iou([0, 0], [0, 0]) == 0.0

    def test_partial_overlap(self):
        # intersection 1, union 3
        assert mask_utils.mask_iou([1, 1, 0], [0, 1, 1]) == pytest.approx(1 / 3)

    def test_length_mismatch(self):
        with pytest.raises(ValueError):
            mask_utils.mask_iou([1], [1, 0])


class TestDedupeMasks:
    def test_keeps_higher_score_of_duplicates(self):
        low = {'mask': [1, 1, 0, 0], 'score': 0.5}
        high = {'mask': [1, 1, 0, 0], 'score': 0.9}
        kept = mask_utils.dedupe_masks([low, high], iou_threshold=0.85)
        assert kept == [high]

    def test_keeps_distinct_masks(self):
        a = {'mask': [1, 1, 0, 0], 'score': 0.9}
        b = {'mask': [0, 0, 1, 1], 'score': 0.8}
        kept = mask_utils.dedupe_masks([a, b], iou_threshold=0.85)
        assert kept == [a, b]  # descending score order


# ---------------------------------------------------------------------------
# Prompt grid
# ---------------------------------------------------------------------------

class TestBuildPointGrid:
    def test_count_and_bounds(self):
        points = mask_utils.build_point_grid(100, 50, 4)
        assert len(points) == 16
        assert all(0 <= x < 100 and 0 <= y < 50 for x, y in points)

    def test_single_point_is_centered(self):
        assert mask_utils.build_point_grid(10, 8, 1) == [(5.0, 4.0)]

    def test_rejects_invalid_args(self):
        with pytest.raises(ValueError):
            mask_utils.build_point_grid(0, 10, 2)
        with pytest.raises(ValueError):
            mask_utils.build_point_grid(10, 10, 0)


# ---------------------------------------------------------------------------
# Region post-processing (class-agnostic proposals, req 8.2)
# ---------------------------------------------------------------------------

class TestSelectRegions:
    def _candidate(self, mask, score):
        return {'mask': mask, 'score': score}

    def test_regions_are_class_agnostic_rle(self):
        candidates = [self._candidate([1, 1, 0, 0], 0.9)]
        regions = mask_utils.select_regions(candidates, width=2, height=2)
        assert len(regions) == 1
        assert regions[0]['class'] is None
        assert regions[0]['rle'] == mask_utils.rle_encode([1, 1, 0, 0], 2, 2)
        assert regions[0]['score'] == pytest.approx(0.9)

    def test_caps_at_max_regions_by_score(self):
        candidates = [
            self._candidate([1, 0, 0, 0], 0.7),
            self._candidate([0, 1, 0, 0], 0.9),
            self._candidate([0, 0, 1, 0], 0.8),
        ]
        regions = mask_utils.select_regions(
            candidates, width=2, height=2, max_regions=2
        )
        assert [r['score'] for r in regions] == [pytest.approx(0.9), pytest.approx(0.8)]

    def test_drops_empty_and_undersized_masks(self):
        candidates = [
            self._candidate([0] * 16, 0.9),          # empty
            self._candidate([1] + [0] * 15, 0.9),    # 1/16 of the image
            self._candidate([1] * 8 + [0] * 8, 0.8),  # half the image
        ]
        regions = mask_utils.select_regions(
            candidates, width=4, height=4, min_area_fraction=0.25
        )
        assert len(regions) == 1
        assert regions[0]['score'] == pytest.approx(0.8)

    def test_dedupes_near_identical_masks(self):
        candidates = [
            self._candidate([1, 1, 1, 0], 0.9),
            self._candidate([1, 1, 1, 0], 0.5),
        ]
        regions = mask_utils.select_regions(candidates, width=2, height=2)
        assert len(regions) == 1

    def test_rejects_invalid_max_regions(self):
        with pytest.raises(ValueError):
            mask_utils.select_regions([], width=2, height=2, max_regions=0)


# ---------------------------------------------------------------------------
# Handler input validation (importable and testable without onnxruntime)
# ---------------------------------------------------------------------------

class TestHandlerInputValidation:
    def _handler(self):
        import handler
        return handler

    def test_module_imports_without_heavy_dependencies(self):
        handler = self._handler()
        assert callable(handler.lambda_handler)

    def test_rejects_event_without_image(self):
        handler = self._handler()
        with pytest.raises(ValueError, match='image_bytes_base64'):
            handler.lambda_handler({})

    def test_rejects_invalid_base64(self):
        handler = self._handler()
        with pytest.raises(ValueError, match='image_bytes_base64'):
            handler.lambda_handler({'image_bytes_base64': '!!not-base64!!'})

    def test_rejects_non_https_presigned_url(self):
        handler = self._handler()
        with pytest.raises(ValueError, match='https'):
            handler.lambda_handler(
                {'image_s3_presigned_url': 'http://example.com/img.png'}
            )

    def test_rejects_invalid_max_regions(self):
        handler = self._handler()
        with pytest.raises(ValueError, match='max_regions'):
            handler.lambda_handler({'image_bytes_base64': 'AA==', 'max_regions': 0})
        with pytest.raises(ValueError, match='max_regions'):
            handler.lambda_handler(
                {'image_bytes_base64': 'AA==', 'max_regions': 'lots'}
            )

# ---------------------------------------------------------------------------
# Encoder input layout detection (no numpy/onnxruntime needed: these read
# only the declared ONNX graph shape)
# ---------------------------------------------------------------------------

class _FakeInput:
    def __init__(self, shape, name='input_image'):
        self.shape = shape
        self.name = name


class _FakeEncoder:
    def __init__(self, shape):
        self._inputs = [_FakeInput(shape)]

    def get_inputs(self):
        return self._inputs


class TestEncoderInputLayout:
    def _handler(self):
        import handler
        return handler

    def test_hwc_export_detected(self):
        """samexporter MobileSAM declares a rank-3 HWC input."""
        handler = self._handler()
        encoder = _FakeEncoder(['image_height', 'image_width', 3])
        assert handler._encoder_expects_hwc(encoder) is True

    def test_nchw_export_not_hwc(self):
        handler = self._handler()
        encoder = _FakeEncoder([1, 3, 1024, 1024])
        assert handler._encoder_expects_hwc(encoder) is False

    def test_hwc_dynamic_dims_fall_back_to_default_size(self):
        """The trailing 3 is the channel count, not the resolution — a
        rank-3 dynamic shape must not yield an encoder size of 3."""
        handler = self._handler()
        encoder = _FakeEncoder(['image_height', 'image_width', 3])
        size = handler._encoder_input_size(encoder)
        assert size == handler.DEFAULT_ENCODER_SIZE
        assert size != 3

    def test_nchw_static_dims_use_trailing_spatial_dims(self):
        handler = self._handler()
        assert handler._encoder_input_size(_FakeEncoder([1, 3, 1024, 1024])) == 1024
        assert handler._encoder_input_size(_FakeEncoder([1, 3, 512, 512])) == 512

    def test_hwc_static_dims_use_leading_spatial_dims(self):
        handler = self._handler()
        assert handler._encoder_input_size(_FakeEncoder([1024, 1024, 3])) == 1024

    def test_nchw_dynamic_dims_fall_back_to_default_size(self):
        handler = self._handler()
        encoder = _FakeEncoder([1, 3, 'height', 'width'])
        assert handler._encoder_input_size(encoder) == handler.DEFAULT_ENCODER_SIZE
