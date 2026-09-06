"""
Property-based test for the Image_Downscaler shared-layer module
(layers/shared/python/dda_llm_image.py): bounds and totality.

Spec: llm-model-token-and-image-sizing, task 1.3.

**Feature: llm-model-token-and-image-sizing, Property 13: Downscaling is
bounded in resource use and always yields one outcome**
**Validates: Requirements 6.9, 6.10, 6.11**

The module under test is pure (no boto3, no I/O), so this file needs no
moto fixtures and no AWS credentials — conftest.py already places the
shared layer on sys.path. Pillow is used here only to *build* the small
corpus of genuinely valid source images.

Generator notes (from the design's Property 13 test strategy):

- Arbitrary byte strings from `st.binary(max_size=4096)`.
- Valid PNG/JPEG bytes truncated at a drawn offset.
- Valid bytes with one drawn byte corrupted.
- `b''`.
- PNG IHDR and JPEG SOF headers hand-built to declare zero dimensions and
  to declare dimensions whose product exceeds 100,000,000 pixels
  (20000 x 20000 among them), with a body of a few hundred bytes.
- Crossed with `st.sampled_from((None,) + MAX_IMAGE_EDGE_OPTIONS)` — every
  Downscale_Setting of Requirement 5.1.

Per-example assertions: the call either returns a `(bytes, width, height)`
triple or raises exactly `DownscaleError` — nothing else escapes, checked
with a bare `except BaseException` re-raise guard; the source `bytes`
object is unchanged, and is not aliased into the output on the re-encode
path; every call returns within Downscale_Duration_Bound (5 seconds).

How "one failure signal identifying the image and the requested
Downscale_Setting" maps onto the module: the module never sees the object
key, so per the design's error-handling table the *caller* composes the
identifying reason (`{subject} could not be resized to a longer edge of
{n} pixels: {cause}` — the chokepoint's `_refusal_reason`) from this
module's signal. What the module must therefore guarantee, and what this
test asserts, is the signal itself: exactly one `DownscaleError` carrying
a non-empty reason, with `pixel_count` set on the Max_Source_Pixel_Count
refusal and **only** there — the discriminator the caller uses to select
the oversize reason shape, whose module-side text names the declared
pixel count verbatim (`declares {w}x{h} = {n} pixels, above the {max}
pixel limit`).

Resource bounds (Requirements 6.10, 6.11) get dedicated instrumented
tests: `tracemalloc` plus `time.perf_counter()` around the 20000 x 20000
oversize refusal demonstrate "without decoding the full image", with a
raising patch on `_resize_and_encode` proving the decode-and-re-encode
path never runs; and the largest *accepted* corpus source is sized to be
representative (8 MP) rather than at the 100 MP ceiling, which the design
notes would be flaky under parallel test load — the ceiling case is
covered by the refusal branch instead.
"""
import io
import struct
import time
import tracemalloc
import zlib

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

import dda_llm_image
from dda_llm_image import (
    DOWNSCALE_OFF,
    MAX_IMAGE_EDGE_OPTIONS,
    MAX_SOURCE_PIXEL_COUNT,
    DownscaleError,
    downscale_image,
)

# Downscale_Duration_Bound (Requirement 6.11).
_DURATION_BOUND_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Valid source corpus — built once; sizes representative, not at the ceiling
# ---------------------------------------------------------------------------

def _encoded_source(container, size, mode='RGB'):
    """A deterministic gradient image of `size` in `container`."""
    image = Image.linear_gradient('L').resize(size)
    if mode != 'L':
        image = image.convert(mode)
    buffer = io.BytesIO()
    if container == 'png':
        image.save(buffer, format='PNG', compress_level=1)
    else:
        image.save(buffer, format='JPEG', quality=80)
    return buffer.getvalue()


# (container, (width, height), bytes): tiny, mid-size, exact-bound,
# extreme-aspect, and one 8 MP source — the largest accepted source,
# deliberately representative rather than ceiling-sized (see docstring).
_VALID_SOURCES = tuple(
    (container, size, _encoded_source(container, size, mode))
    for container, size, mode in (
        ('png', (1, 1), 'RGB'),
        ('png', (37, 53), 'L'),
        ('jpeg', (513, 512), 'RGB'),   # one pixel over the smallest bound
        ('png', (640, 480), 'RGB'),
        ('jpeg', (1600, 1200), 'RGB'),
        ('png', (2048, 2048), 'RGB'),  # exactly at the largest bound
        ('jpeg', (3264, 2448), 'RGB'),  # 8 MP — largest accepted source
        ('jpeg', (4000, 3), 'RGB'),    # extreme aspect ratio
    )
)

_LARGEST_ACCEPTED = next(entry for entry in _VALID_SOURCES
                         if entry[1] == (3264, 2448))


# ---------------------------------------------------------------------------
# Hand-built headers: declared dimensions with a body of a few hundred bytes
# ---------------------------------------------------------------------------

def _png_declared_header(width, height, body_size):
    """A PNG whose IHDR declares (width, height), with a junk IDAT body.

    The IHDR CRC is correct so a header-only `Image.open` proceeds to the
    declared size; the IDAT body is junk `Image.open` never reads (the PNG
    plugin stops at the IDAT chunk header), so any *decode* must fail.
    """
    ihdr_body = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    ihdr = (struct.pack('>I', 13) + b'IHDR' + ihdr_body
            + struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_body)))
    idat = struct.pack('>I', body_size) + b'IDAT' + bytes(body_size)
    return b'\x89PNG\r\n\x1a\n' + ihdr + idat


def _jpeg_declared_header(width, height, body_size):
    """A JPEG whose SOF0 declares (width, height), with a junk scan body.

    SOF0 then SOS lets a header-only `Image.open` read the declared size
    and stop at the start of scan; the scan body is junk, so any *decode*
    must fail. JPEG SOF dimensions are 16-bit, so width and height must be
    at most 65535.
    """
    sof = (b'\xff\xc0' + struct.pack('>HBHH', 17, 8, height, width)
           + b'\x03' + bytes([1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1]))
    sos = (b'\xff\xda' + struct.pack('>H', 12)
           + bytes([3, 1, 0, 2, 0x11, 3, 0x11, 0, 63, 0]))
    return b'\xff\xd8' + sof + sos + bytes(body_size) + b'\xff\xd9'


def _declared_header(container, width, height, body_size):
    if container == 'png':
        return _png_declared_header(width, height, body_size)
    return _jpeg_declared_header(width, height, body_size)


# Declared products all exceed MAX_SOURCE_PIXEL_COUNT; each edge fits the
# 16-bit JPEG SOF field. 20000 x 20000 is the design's named example.
_OVERSIZE_DIMENSIONS = (
    (20000, 20000),   # 400,000,000 px — 4x the ceiling
    (10001, 10000),   # 100,010,000 px — just over the ceiling
    (10000, 10001),
    (65535, 1600),    # 104,856,000 px — extreme edge at the SOF maximum
    (1600, 65535),
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_SETTINGS = st.sampled_from((DOWNSCALE_OFF,) + MAX_IMAGE_EDGE_OPTIONS)
_KEY_FORMATS = st.sampled_from(('png', 'jpeg'))
_BODY_SIZES = st.integers(min_value=200, max_value=400)


@st.composite
def _bounds_cases(draw):
    """One (kind, source bytes, declared dimensions or None) case."""
    kind = draw(st.sampled_from((
        'arbitrary', 'truncated', 'corrupted', 'empty',
        'zero_dimension', 'oversize', 'valid')))
    if kind == 'arbitrary':
        return kind, draw(st.binary(max_size=4096)), None
    if kind == 'empty':
        return kind, b'', None
    if kind == 'truncated':
        _container, _size, data = draw(st.sampled_from(_VALID_SOURCES))
        cut = draw(st.integers(min_value=0, max_value=len(data) - 1))
        return kind, data[:cut], None
    if kind == 'corrupted':
        _container, _size, data = draw(st.sampled_from(_VALID_SOURCES))
        position = draw(st.integers(min_value=0, max_value=len(data) - 1))
        replacement = draw(st.integers(min_value=0, max_value=255))
        corrupted = data[:position] + bytes([replacement]) + data[position + 1:]
        return kind, corrupted, None
    if kind == 'zero_dimension':
        container = draw(st.sampled_from(('png', 'jpeg')))
        axis = draw(st.sampled_from(('width', 'height', 'both')))
        other = draw(st.integers(min_value=1, max_value=4096))
        width = 0 if axis in ('width', 'both') else other
        height = 0 if axis in ('height', 'both') else other
        source = _declared_header(container, width, height, draw(_BODY_SIZES))
        return kind, source, (width, height)
    if kind == 'oversize':
        container = draw(st.sampled_from(('png', 'jpeg')))
        width, height = draw(st.sampled_from(_OVERSIZE_DIMENSIONS))
        source = _declared_header(container, width, height, draw(_BODY_SIZES))
        return kind, source, (width, height)
    container, size, data = draw(st.sampled_from(_VALID_SOURCES))
    return kind, data, size


# ---------------------------------------------------------------------------
# The one-outcome harness — the bare `except BaseException` re-raise guard
# ---------------------------------------------------------------------------

def _single_outcome(image_bytes, image_format, setting, **kwargs):
    """('ok', triple) or ('refused', DownscaleError) — nothing else.

    Any other escape, however exotic, is a Property 13 violation and is
    re-raised as an explicit assertion failure with the original chained.
    """
    try:
        return 'ok', downscale_image(image_bytes, image_format, setting,
                                     **kwargs)
    except DownscaleError as exc:
        return 'refused', exc
    except BaseException as exc:
        raise AssertionError(
            f'{type(exc).__name__} escaped downscale_image — Property 13 '
            f'permits only a Downscaled_Image or a DownscaleError'
        ) from exc


# ---------------------------------------------------------------------------
# Property 13 (task 1.3) — totality over the mangled-input space
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 13: Downscaling is
# bounded in resource use and always yields one outcome
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_bounds_cases(), image_format=_KEY_FORMATS, setting=_SETTINGS,
       supply_dims=st.booleans())
def test_property_downscaler_bounds_and_totality(case, image_format, setting,
                                                 supply_dims):
    """
    **Feature: llm-model-token-and-image-sizing, Property 13: Downscaling
    is bounded in resource use and always yields one outcome**

    For any byte string presented as source image bytes (valid png, valid
    jpeg, truncated, corrupt, empty, zero-dimension, non-image, and headers
    declaring a pixel count above Max_Source_Pixel_Count) and any
    Downscale_Setting, the Image_Downscaler returns either a
    Downscaled_Image or exactly one DownscaleError — no other exception
    escapes — leaves the source bytes unmodified and unaliased on the
    re-encode path, refuses every source whose declared pixel count
    exceeds Max_Source_Pixel_Count with a reason naming that declared
    pixel count, and returns within Downscale_Duration_Bound.

    **Validates: Requirements 6.9, 6.10, 6.11**
    """
    kind, source, declared = case
    kwargs = {}
    if supply_dims and declared is not None:
        # Mirrors the chokepoint, which parses the header first and passes
        # the Source_Dimensions in.
        kwargs['source_dimensions'] = declared
    snapshot = bytes(source)

    started = time.perf_counter()
    outcome, payload = _single_outcome(source, image_format, setting,
                                       **kwargs)
    elapsed = time.perf_counter() - started

    # --- One outcome, bounded in time, source bytes untouched -----------
    # (Requirements 6.9, 6.11)
    assert elapsed < _DURATION_BOUND_SECONDS
    assert source == snapshot

    if outcome == 'refused':
        exc = payload
        # Exactly the module's one failure signal, nothing more derived.
        assert type(exc) is DownscaleError
        assert isinstance(exc.reason, str) and exc.reason
        assert str(exc) == exc.reason
        # The oversize discriminator fires only for a genuinely oversize
        # declaration, and its reason names the declared pixel count —
        # what lets the caller compose the identifying oversize reason
        # shape (Requirement 6.10).
        if exc.pixel_count is not None:
            assert exc.pixel_count > MAX_SOURCE_PIXEL_COUNT
            assert str(exc.pixel_count) in exc.reason
    else:
        out, width, height = payload
        assert isinstance(out, bytes)
        assert isinstance(width, int) and isinstance(height, int)
        if out is not source:
            # The re-encode path: a fresh bytes object (never the source
            # aliased back), only reachable with a Max_Image_Edge setting,
            # and inside the bound (Requirement 6.9's "one outcome" is a
            # *usable* Downscaled_Image).
            assert setting is not DOWNSCALE_OFF
            assert min(width, height) >= 1
            assert max(width, height) <= setting

    # --- Kind-specific outcomes ------------------------------------------
    if kind == 'valid':
        # A decodable, in-bounds source always yields the Downscaled_Image
        # arm — never a failure signal.
        assert outcome == 'ok'
        out, width, height = payload
        source_width, source_height = declared
        if setting is DOWNSCALE_OFF or max(declared) <= setting:
            assert out is source
            assert (width, height) == (source_width, source_height)
        else:
            assert out is not source
            assert max(width, height) == setting

    elif kind == 'empty':
        # b'' can never be sized or decoded, whatever the setting.
        assert outcome == 'refused'
        assert payload.pixel_count is None

    elif kind == 'zero_dimension':
        if setting is DOWNSCALE_OFF and 'source_dimensions' in kwargs:
            # Downscale_Off trusts the caller's dimensions and never
            # validates: the same bytes object comes straight back.
            assert outcome == 'ok'
            out, width, height = payload
            assert out is source
            assert (width, height) == declared
        else:
            # Refused everywhere else: unparseable at Downscale_Off,
            # step-3 (or decode) refusal at any Max_Image_Edge. Never the
            # oversize discriminator — the declared product is zero.
            assert outcome == 'refused'
            assert payload.pixel_count is None

    elif kind == 'oversize':
        source_width, source_height = declared
        pixel_count = source_width * source_height
        if setting is DOWNSCALE_OFF:
            # Downscale_Off sends bytes unmodified and decodes nothing, so
            # there is no resource to bound and no refusal (design control
            # flow: step 1 precedes the pixel-count check).
            assert outcome == 'ok'
            out, width, height = payload
            assert out is source
            assert (width, height) == declared
        else:
            # Refused for every Max_Image_Edge, naming the declared pixel
            # count (Requirement 6.10) — via the step-4 reason shape when
            # the declared size was readable, via the aligned decompression
            # guard otherwise.
            assert outcome == 'refused'
            exc = payload
            assert str(pixel_count) in exc.reason
            if 'source_dimensions' in kwargs:
                # The chokepoint's path: dimensions supplied, so the
                # refusal is the step-4 signal with the discriminator set
                # and the error-table cause text verbatim.
                assert exc.pixel_count == pixel_count
                assert exc.reason == (
                    f'declares {source_width}x{source_height} = '
                    f'{pixel_count} pixels, above the '
                    f'{MAX_SOURCE_PIXEL_COUNT} pixel limit')

    # 'arbitrary', 'truncated' and 'corrupted' sources promise no specific
    # arm — only the universal invariants asserted above: one outcome,
    # exception discipline, untouched source bytes, and the time bound.


# ---------------------------------------------------------------------------
# Property 13 — the oversize refusal is header-only: fast and small
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('container', ('png', 'jpeg'))
@pytest.mark.parametrize('supply_dims', (False, True),
                         ids=('parsed-dims', 'supplied-dims'))
def test_property_oversize_refusal_is_fast_and_decodes_nothing(
        container, supply_dims):
    """
    **Feature: llm-model-token-and-image-sizing, Property 13: Downscaling
    is bounded in resource use and always yields one outcome**

    A 20000 x 20000 declaration (400,000,000 pixels — 4x the
    Max_Source_Pixel_Count ceiling) carried by a source of a few hundred
    bytes is refused for every Max_Image_Edge without decoding the full
    image: the refusal completes in well under Downscale_Duration_Bound,
    the `tracemalloc` peak stays orders of magnitude below any full-decode
    buffer (a 400 MP decode needs at least ~400 MB at one byte per pixel),
    and the module's only decode-and-re-encode seam, `_resize_and_encode`,
    is proven never to run by patching it to raise.

    **Validates: Requirements 6.9, 6.10, 6.11**
    """
    width = height = 20000
    pixel_count = width * height
    source = _declared_header(container, width, height, body_size=300)
    kwargs = {'source_dimensions': (width, height)} if supply_dims else {}

    # Warm-up outside the measured window: Pillow's import and the
    # exception machinery allocate once, on first use.
    with pytest.raises(DownscaleError):
        downscale_image(source, container, 512, **kwargs)

    def _must_not_run(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError(
            'an oversize declaration must be refused before any '
            'decode-and-re-encode (Requirement 6.10)')

    real_resize = dda_llm_image._resize_and_encode
    dda_llm_image._resize_and_encode = _must_not_run
    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        worst_elapsed = 0.0
        for setting in MAX_IMAGE_EDGE_OPTIONS:
            started = time.perf_counter()
            with pytest.raises(DownscaleError) as caught:
                downscale_image(source, container, setting, **kwargs)
            worst_elapsed = max(worst_elapsed,
                                time.perf_counter() - started)
            exc = caught.value
            # The refusal names the declared pixel count (Req 6.10).
            assert str(pixel_count) in exc.reason
            if supply_dims:
                assert exc.pixel_count == pixel_count
                assert exc.reason == (
                    f'declares {width}x{height} = {pixel_count} pixels, '
                    f'above the {MAX_SOURCE_PIXEL_COUNT} pixel limit')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        dda_llm_image._resize_and_encode = real_resize

    # Well under the 5-second bound, per refusal.
    assert worst_elapsed < 1.0
    # Python-level allocations stay far below any full-decode buffer:
    # 8 MiB against the >= 400 MB a 400 MP decode would materialize.
    assert peak - baseline < 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Property 13 — accepted sources return within Downscale_Duration_Bound
# ---------------------------------------------------------------------------

def test_property_accepted_source_returns_within_duration_bound():
    """
    **Feature: llm-model-token-and-image-sizing, Property 13: Downscaling
    is bounded in resource use and always yields one outcome**

    Every Downscale_Setting applied to the corpus's largest accepted
    sources — an 8 MP JPEG (the representative largest, deliberately not
    the flaky 100 MP ceiling, which is covered by the refusal branch) and
    the exact-bound 2048 x 2048 PNG — returns a Downscaled_Image within
    Downscale_Duration_Bound (5 seconds), measured with
    `time.perf_counter()` (Requirement 6.11).

    **Validates: Requirements 6.9, 6.10, 6.11**
    """
    probes = (_LARGEST_ACCEPTED, next(
        entry for entry in _VALID_SOURCES if entry[1] == (2048, 2048)))
    for container, (source_width, source_height), source in probes:
        for setting in (DOWNSCALE_OFF,) + MAX_IMAGE_EDGE_OPTIONS:
            started = time.perf_counter()
            out, width, height = downscale_image(source, container, setting)
            elapsed = time.perf_counter() - started
            assert elapsed < _DURATION_BOUND_SECONDS
            # And the outcome is the Downscaled_Image arm, inside bounds.
            assert isinstance(out, bytes)
            if setting is DOWNSCALE_OFF:
                assert (width, height) == (source_width, source_height)
            else:
                assert max(width, height) <= setting
