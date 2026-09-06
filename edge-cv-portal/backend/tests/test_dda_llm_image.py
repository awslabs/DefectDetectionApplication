"""
Example-based unit tests for the Image_Downscaler shared-layer module
(layers/shared/python/dda_llm_image.py): normalize_downscale_setting,
declared_dimensions and downscale_image.

Spec: llm-model-token-and-image-sizing, task 1.4.
Requirements: 5.1, 5.9, 6.2, 6.3, 6.4, 6.5, 6.7, 6.10

The module is dependency-light (no boto3, no I/O) so these tests need no
moto fixtures — conftest.py already places the shared layer on sys.path,
and Pillow comes from the imaging layer's pin.

Technique note for the "Downscale_Off imports no Pillow" case: this file
imports Pillow itself (to build real source images), and so do several
other tests in this directory, so an in-process `'PIL' not in sys.modules`
assertion would be vacuous. That claim is therefore checked in a **clean
subprocess** (`test_downscale_off_imports_no_pillow_in_a_clean_process`)
that receives the source bytes over stdin as base64 and never touches
Pillow itself. The in-process cases assert the observable consequence
instead — the *same* `bytes` object comes back, and no re-encode path is
reachable (`_import_pillow_image` / `_resize_and_encode` are replaced with
functions that fail the test if called).
"""
import base64
import io
import os
import subprocess
import sys

import pytest
from PIL import Image

import dda_llm_image
from dda_llm_image import (
    DOWNSCALE_OFF,
    IMAGE_FORMAT_JPEG,
    IMAGE_FORMAT_PNG,
    MAX_IMAGE_EDGE_OPTIONS,
    MAX_SOURCE_PIXEL_COUNT,
    RESAMPLING_FILTER_NAME,
    DownscaleError,
    declared_dimensions,
    downscale_image,
    normalize_downscale_setting,
)

_SHARED_LAYER = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', 'layers', 'shared', 'python'))

# An ICC profile only has to survive Pillow's embed/extract round trip for
# these tests; it is never colour-managed, so a plausible header plus filler
# avoids depending on ImageCms being present in the test environment.
FAKE_ICC_PROFILE = (b'\x00\x00\x01\x00acspMNTRRGB XYZ '
                    + bytes(range(200)))


# ---------------------------------------------------------------------------
# Source image builders — real bytes, built with C-speed Pillow ops only
# ---------------------------------------------------------------------------

def _gradient(mode, size):
    """A deterministic, non-uniform image of `mode` at `size`.

    Non-uniform matters: a flat colour would resample to itself, so a
    re-encode would be hard to distinguish from a pass-through.
    """
    base = Image.linear_gradient('L').resize(size)
    if mode == 'L':
        return base
    rgb = Image.merge('RGB', (base,
                              base.transpose(Image.Transpose.ROTATE_180),
                              base))
    return rgb if mode == 'RGB' else rgb.convert(mode)


def _encode(image, container, **save_params):
    buffer = io.BytesIO()
    image.save(buffer, format=container, **save_params)
    return buffer.getvalue()


def _source(mode, size, container, **save_params):
    return _encode(_gradient(mode, size), container, **save_params)


def _opened(image_bytes):
    """The output re-opened, so its container, mode, size and `info` can be
    inspected the way a downstream consumer would see them."""
    return Image.open(io.BytesIO(image_bytes))


def _explode(*args, **kwargs):
    raise AssertionError('the Pillow / re-encode path must not be reached')


@pytest.fixture(scope='module')
def source_3000x2000():
    """One 3000x2000 JPEG shared by the floor-formula cases."""
    return _source('RGB', (3000, 2000), 'JPEG', quality=90)


# ---------------------------------------------------------------------------
# normalize_downscale_setting (Requirements 5.1, 5.9)
# ---------------------------------------------------------------------------

class TestNormalizeDownscaleSetting:
    def test_the_seven_settings_are_downscale_off_plus_six_edges(self):
        assert DOWNSCALE_OFF is None
        assert MAX_IMAGE_EDGE_OPTIONS == (512, 768, 1024, 1280, 1536, 2048)

    @pytest.mark.parametrize('value', MAX_IMAGE_EDGE_OPTIONS)
    def test_each_max_image_edge_resolves_to_itself(self, value):
        assert normalize_downscale_setting(value) == value

    def test_downscale_off_resolves_to_downscale_off(self):
        assert normalize_downscale_setting(DOWNSCALE_OFF) is DOWNSCALE_OFF

    @pytest.mark.parametrize('value', [
        False, True, '1024', 1024.0, 1023, 4096, None, {},
    ])
    def test_every_other_value_degrades_to_downscale_off(self, value):
        assert normalize_downscale_setting(value) is DOWNSCALE_OFF


# ---------------------------------------------------------------------------
# Downscale_Off (Requirement 6.2)
# ---------------------------------------------------------------------------

class TestDownscaleOff:
    def test_returns_the_same_bytes_object_and_the_passed_in_dimensions(
            self, monkeypatch):
        src = _source('RGB', (4000, 3000), 'PNG')
        monkeypatch.setattr(dda_llm_image, '_import_pillow_image', _explode)
        out, width, height = downscale_image(
            src, IMAGE_FORMAT_PNG, DOWNSCALE_OFF,
            source_dimensions=(4000, 3000))
        assert out is src
        assert (width, height) == (4000, 3000)

    def test_dimensions_come_from_the_header_when_not_supplied(
            self, monkeypatch):
        src = _source('RGB', (640, 480), 'JPEG')
        monkeypatch.setattr(dda_llm_image, '_import_pillow_image', _explode)
        out, width, height = downscale_image(
            src, IMAGE_FORMAT_JPEG, DOWNSCALE_OFF)
        assert out is src
        assert (width, height) == declared_dimensions(src) == (640, 480)

    def test_downscale_off_imports_no_pillow_in_a_clean_process(self):
        """`sys.modules` carries no `PIL` entry after a Downscale_Off call.

        Run in a fresh interpreter: this test module (and its neighbours)
        import Pillow, so the same assertion in-process would pass no
        matter what the module did.
        """
        src = _source('RGB', (640, 480), 'PNG')
        script = (
            'import base64, sys\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'import dda_llm_image\n'
            'data = base64.b64decode(sys.stdin.buffer.read())\n'
            'out, width, height = dda_llm_image.downscale_image(\n'
            '    data, "png", dda_llm_image.DOWNSCALE_OFF)\n'
            'assert out is data, "a different bytes object came back"\n'
            'assert (width, height) == (640, 480), (width, height)\n'
            'leaked = sorted(name for name in sys.modules\n'
            '                if name == "PIL" or name.startswith("PIL."))\n'
            'assert not leaked, "Pillow was imported: %s" % leaked\n'
            'sys.stdout.write("OK")\n'
        )
        completed = subprocess.run(
            [sys.executable, '-c', script, _SHARED_LAYER],
            input=base64.b64encode(src),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        assert completed.returncode == 0, completed.stderr.decode()
        assert completed.stdout == b'OK', completed.stderr.decode()

    def test_undeterminable_dimensions_are_refused(self):
        with pytest.raises(DownscaleError) as excinfo:
            downscale_image(b'neither png nor jpeg', IMAGE_FORMAT_PNG,
                            DOWNSCALE_OFF)
        assert type(excinfo.value) is DownscaleError
        assert excinfo.value.pixel_count is None


# ---------------------------------------------------------------------------
# The two pass-through branches and the re-encode boundary (Requirement 6.3)
# ---------------------------------------------------------------------------

class TestBoundPassThrough:
    def test_exact_bound_passes_through_with_no_pillow_at_all(
            self, monkeypatch):
        """Longer edge == the option, Source_Dimensions supplied: the
        module needs neither a header read nor a decode."""
        src = _source('RGB', (1024, 600), 'PNG')
        monkeypatch.setattr(dda_llm_image, '_import_pillow_image', _explode)
        out, width, height = downscale_image(
            src, IMAGE_FORMAT_PNG, 1024, source_dimensions=(1024, 600))
        assert out is src
        assert (width, height) == (1024, 600)

    def test_exact_bound_on_the_short_axis_passes_through(self, monkeypatch):
        """Longer edge == the option with the dimensions read from the
        header: the header read is allowed, a decode is not."""
        src = _source('RGB', (300, 512), 'JPEG')
        monkeypatch.setattr(dda_llm_image, '_resize_and_encode', _explode)
        out, width, height = downscale_image(src, IMAGE_FORMAT_JPEG, 512)
        assert out is src
        assert (width, height) == (300, 512)

    def test_below_bound_passes_through(self, monkeypatch):
        src = _source('RGB', (511, 300), 'PNG')
        monkeypatch.setattr(dda_llm_image, '_resize_and_encode', _explode)
        out, width, height = downscale_image(src, IMAGE_FORMAT_PNG, 512)
        assert out is src
        assert (width, height) == (511, 300)

    def test_one_pixel_over_the_bound_is_re_encoded(self):
        src = _source('RGB', (513, 400), 'PNG')
        out, width, height = downscale_image(src, IMAGE_FORMAT_PNG, 512)
        assert out is not src
        assert out != src
        # 400 * 512 // 513 == 399
        assert (width, height) == (512, 399)
        reopened = _opened(out)
        assert reopened.size == (512, 399)
        assert reopened.format == 'PNG'


# ---------------------------------------------------------------------------
# The floor formula (Requirements 6.4, 6.5)
# ---------------------------------------------------------------------------

class TestTargetDimensions:
    @pytest.mark.parametrize('max_image_edge, expected', [
        (512, (512, 341)),
        (768, (768, 512)),
        (1024, (1024, 682)),
        (1280, (1280, 853)),
        (1536, (1536, 1024)),
        (2048, (2048, 1365)),
    ])
    def test_hand_computed_values_for_a_3000x2000_source(
            self, source_3000x2000, max_image_edge, expected):
        out, width, height = downscale_image(
            source_3000x2000, IMAGE_FORMAT_JPEG, max_image_edge)
        assert (width, height) == expected
        # The returned dimensions describe the bytes actually produced.
        assert _opened(out).size == expected
        assert width <= 3000 and height <= 2000
        assert max(width, height) == max_image_edge

    @pytest.mark.parametrize('size, expected', [
        ((5000, 1), (512, 1)),
        ((1, 5000), (1, 512)),
    ])
    def test_extreme_aspect_ratios_keep_the_short_edge_at_one_pixel(
            self, size, expected):
        src = _source('RGB', size, 'PNG')
        out, width, height = downscale_image(src, IMAGE_FORMAT_PNG, 512)
        assert (width, height) == expected
        assert _opened(out).size == expected


# ---------------------------------------------------------------------------
# Deterministic mode conversion (Requirement 6.6's mode maps)
# ---------------------------------------------------------------------------

class TestModeCoverage:
    @pytest.mark.parametrize(
        'source_mode, source_container, image_format, '
        'expected_mode, expected_container', [
            # Palette PNG: converted before resampling, never interpolated.
            ('P', 'PNG', IMAGE_FORMAT_PNG, 'RGBA', 'PNG'),
            # JPEG can hold neither RGBA nor P; alpha is dropped.
            ('RGBA', 'PNG', IMAGE_FORMAT_JPEG, 'RGB', 'JPEG'),
            # Grayscale stays grayscale in both containers.
            ('L', 'PNG', IMAGE_FORMAT_PNG, 'L', 'PNG'),
            ('L', 'JPEG', IMAGE_FORMAT_JPEG, 'L', 'JPEG'),
            # Everything unmapped lands on RGB.
            ('CMYK', 'JPEG', IMAGE_FORMAT_JPEG, 'RGB', 'JPEG'),
        ])
    def test_output_mode_is_keyed_on_the_source_mode(
            self, source_mode, source_container, image_format,
            expected_mode, expected_container):
        src = _source(source_mode, (1000, 800), source_container)
        assert _opened(src).mode == source_mode
        out, width, height = downscale_image(src, image_format, 512)
        reopened = _opened(out)
        assert reopened.mode == expected_mode
        assert reopened.format == expected_container
        assert (width, height) == (512, 409) == reopened.size


# ---------------------------------------------------------------------------
# No source metadata reaches the output (Requirement 6.6)
# ---------------------------------------------------------------------------

class TestMetadataIsStripped:
    def _exif(self, image):
        exif = image.getexif()
        exif[0x010F] = 'TestCamera'                  # Make
        exif[0x0132] = '2020:01:02 03:04:05'         # DateTime
        return exif.tobytes()

    def test_jpeg_source_metadata_absent_from_the_output(self):
        image = _gradient('RGB', (1000, 800))
        src = _encode(image, 'JPEG', exif=self._exif(image),
                      icc_profile=FAKE_ICC_PROFILE, dpi=(300, 300),
                      comment=b'source-comment')
        source_info = _opened(src).info
        assert source_info['exif'] and source_info['icc_profile']
        assert source_info['jfif_density'] == (300, 300)

        out, _width, _height = downscale_image(src, IMAGE_FORMAT_JPEG, 512)

        info = _opened(out).info
        assert 'exif' not in info
        assert 'icc_profile' not in info
        assert 'comment' not in info
        assert 'dpi' not in info
        # A JFIF APP0 segment is still written, but it carries the pinned
        # aspect-ratio-only density rather than the source's 300 dpi.
        assert info['jfif_density'] == (1, 1)
        assert info['jfif_unit'] == 0
        assert b'TestCamera' not in out
        assert b'source-comment' not in out
        assert b'acspMNTR' not in out

    def test_png_source_metadata_absent_from_the_output(self):
        src = _encode(_gradient('RGB', (1000, 800)), 'PNG',
                      icc_profile=FAKE_ICC_PROFILE, dpi=(300, 300))
        assert _opened(src).info['icc_profile']

        out, _width, _height = downscale_image(src, IMAGE_FORMAT_PNG, 512)

        assert _opened(out).info == {}
        assert b'acspMNTR' not in out


# ---------------------------------------------------------------------------
# The pinned resampling filter (Requirement 6.6)
# ---------------------------------------------------------------------------

class TestPinnedResamplingFilter:
    def test_the_filter_is_named_at_module_level_and_resolves_to_lanczos(self):
        assert RESAMPLING_FILTER_NAME == 'LANCZOS'
        assert getattr(Image.Resampling, RESAMPLING_FILTER_NAME) \
            is Image.Resampling.LANCZOS

    def test_no_pillow_valued_filter_constant_exists(self):
        """A `RESAMPLING_FILTER = Image.Resampling.LANCZOS` module constant
        would require Pillow at import time, which the contract forbids."""
        assert not hasattr(dda_llm_image, 'RESAMPLING_FILTER')


# ---------------------------------------------------------------------------
# The pixel-count bound and the bomb guard (Requirements 6.9, 6.10)
# ---------------------------------------------------------------------------

class TestPixelCountBound:
    def test_pillow_bomb_guard_coincides_with_the_bound_after_a_resize(self):
        assert MAX_SOURCE_PIXEL_COUNT == 100_000_000
        src = _source('RGB', (600, 400), 'PNG')
        downscale_image(src, IMAGE_FORMAT_PNG, 512)
        assert Image.MAX_IMAGE_PIXELS == MAX_SOURCE_PIXEL_COUNT

    def test_oversize_declared_pixel_count_is_refused_with_its_count(self):
        src = _source('RGB', (600, 400), 'PNG')
        with pytest.raises(DownscaleError) as excinfo:
            downscale_image(src, IMAGE_FORMAT_PNG, 512,
                            source_dimensions=(20000, 20000))
        assert type(excinfo.value) is DownscaleError
        assert excinfo.value.pixel_count == 400_000_000
        assert str(MAX_SOURCE_PIXEL_COUNT) in str(excinfo.value)

    @pytest.mark.parametrize('source_dimensions', [(0, 10), (10, 0), (0, 0)])
    def test_degenerate_dimensions_are_refused_with_no_pixel_count(
            self, source_dimensions):
        src = _source('RGB', (600, 400), 'PNG')
        with pytest.raises(DownscaleError) as excinfo:
            downscale_image(src, IMAGE_FORMAT_PNG, 512,
                            source_dimensions=source_dimensions)
        assert type(excinfo.value) is DownscaleError
        assert excinfo.value.pixel_count is None

    def test_decompression_bomb_surfaces_as_a_downscale_error(
            self, monkeypatch):
        """Pillow's own guard, driven by lowering the shared bound.

        `MAX_SOURCE_PIXEL_COUNT` feeds both the declared-count refusal and
        `Image.MAX_IMAGE_PIXELS`; lowering it to a quarter of the source's
        real pixel count puts the source past Pillow's hard 2x threshold, so
        the guard fires inside the header read. `monkeypatch` restores both
        the module constant and `Image.MAX_IMAGE_PIXELS` afterwards.
        """
        src = _source('RGB', (1200, 1200), 'JPEG')
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', Image.MAX_IMAGE_PIXELS)
        monkeypatch.setattr(dda_llm_image, 'MAX_SOURCE_PIXEL_COUNT', 360_000)

        with pytest.raises(DownscaleError) as excinfo:
            downscale_image(src, IMAGE_FORMAT_JPEG, 512)

        assert type(excinfo.value) is DownscaleError
        assert excinfo.value.pixel_count is None
        assert 'could not be decoded' in str(excinfo.value)


# ---------------------------------------------------------------------------
# The output container follows the caller's key-derived format (Req 6.7)
# ---------------------------------------------------------------------------

class TestOutputContainerFollowsTheKey:
    def test_jpg_key_with_png_content_re_encodes_to_a_real_jpeg(self):
        src = _source('RGB', (1000, 800), 'PNG')
        assert _opened(src).format == 'PNG'

        out, width, height = downscale_image(src, IMAGE_FORMAT_JPEG, 512)

        assert out[:2] == b'\xff\xd8'
        assert _opened(out).format == 'JPEG'
        assert (width, height) == (512, 409)

    def test_jpg_key_with_png_content_passes_the_png_bytes_through_when_off(
            self):
        src = _source('RGB', (1000, 800), 'PNG')

        out, width, height = downscale_image(
            src, IMAGE_FORMAT_JPEG, DOWNSCALE_OFF)

        # Today's behavior, preserved: the bytes are untouched, so a
        # content/extension mismatch survives Downscale_Off even though the
        # resize path would have fixed it.
        assert out is src
        assert _opened(out).format == 'PNG'
        assert (width, height) == (1000, 800)
