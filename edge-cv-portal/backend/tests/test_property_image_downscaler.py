"""
Property-based test for the Image_Downscaler shared-layer module
(layers/shared/python/dda_llm_image.py): the downscale algebra.

Spec: llm-model-token-and-image-sizing, task 1.2.

**Feature: llm-model-token-and-image-sizing, Property 4: Downscaling is
deterministic, shrinking, and idempotent at the bound**
**Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 6.7**

The module under test is pure (no boto3, no I/O), so this file needs no
moto fixtures and no AWS credentials — conftest.py already places the
shared layer on sys.path. Pillow is used here only to *build* real source
images and to inspect outputs.

Generator notes (from the design's Property 4 test strategy):

- Source dimensions come from `st.integers(1, 4000) x st.integers(1, 4000)`,
  mixed with extreme aspect ratios (`(1..5) x (3000..4000)` and its
  transpose) and exact-bound cases where a value from
  MAX_IMAGE_EDGE_OPTIONS is used directly as a dimension.
- Source content is a seeded deterministic pattern in modes `L`, `RGB`,
  `RGBA`, `P` and `CMYK`, encoded into a PNG or JPEG container. The
  container is drawn *independently* of the key-derived `image_format`
  argument, so content/extension mismatches are covered.
- The Downscale_Setting is drawn from `(None,) + MAX_IMAGE_EDGE_OPTIONS`
  (all seven values of Requirement 5.1).

Per-example assertions: the dimension algebra (Requirements 6.4, 6.5);
byte identity at the `is` level for the two pass-through branches
(Requirements 6.2, 6.3); the emitted container equals the key-derived
format (Requirement 6.7); in-process determinism by calling twice
(Requirement 6.6); idempotence by feeding the output back in with the
same setting; and a decoder spy asserting zero decode-and-re-encodes on
both pass-through branches.

Cross-process determinism (the rest of Requirement 6.6) is asserted by
**one** `subprocess` run over a fixed corpus: the child re-imports the
module in a clean interpreter, performs the same downscales, and reports
sha256 digests that must equal the digests computed in this process.
"""
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

import dda_llm_image
from dda_llm_image import (
    DOWNSCALE_OFF,
    MAX_IMAGE_EDGE_OPTIONS,
    downscale_image,
)

_SHARED_LAYER = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', 'layers', 'shared', 'python'))


# ---------------------------------------------------------------------------
# Source image builders — a seeded deterministic pattern, C-speed Pillow ops
# ---------------------------------------------------------------------------

def _pattern_image(mode, size, seed):
    """A deterministic, non-uniform image of `mode` at `size`.

    Non-uniform matters: a flat colour would resample to itself, so a
    re-encode would be hard to distinguish from a pass-through. The seed
    perturbs the gradient through a 256-entry lookup table so distinct
    seeds yield distinct pixel content while every build for the same
    (mode, size, seed) is identical.
    """
    base = Image.linear_gradient('L').resize(size)
    if seed:
        base = base.point([(value + 31 * seed) % 256 for value in range(256)])
    if mode == 'L':
        return base
    rgb = Image.merge('RGB', (
        base,
        base.transpose(Image.Transpose.ROTATE_180),
        base.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
    ))
    if mode == 'RGB':
        return rgb
    if mode == 'RGBA':
        rgba = rgb.convert('RGBA')
        rgba.putalpha(base.transpose(Image.Transpose.FLIP_TOP_BOTTOM))
        return rgba
    return rgb.convert(mode)  # 'P', 'CMYK'


def _source_bytes(mode, container, size, seed):
    """`mode` content at `size` encoded into `container` ('PNG'|'JPEG').

    Source encode parameters are irrelevant to the property (they shape
    the *input*); PNG level 1 keeps large examples fast.
    """
    buffer = io.BytesIO()
    image = _pattern_image(mode, size, seed)
    if container == 'PNG':
        image.save(buffer, format='PNG', compress_level=1)
    else:
        image.save(buffer, format='JPEG', quality=80)
    return buffer.getvalue()


# Content (mode, container) pairs — every entry of both mode maps is
# reachable, constrained to what each container can actually hold.
_MODE_CONTAINERS = (
    ('L', 'PNG'), ('RGB', 'PNG'), ('RGBA', 'PNG'), ('P', 'PNG'),
    ('L', 'JPEG'), ('RGB', 'JPEG'), ('CMYK', 'JPEG'),
)


# ---------------------------------------------------------------------------
# The decoder spy
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _decode_spy():
    """Counts the module's Pillow seams without changing behavior.

    `_resize_and_encode` is the module's only decode-and-re-encode path,
    `_pillow_header_dimensions` its only header read, and
    `_import_pillow_image` the only way Pillow enters at all — so the
    three counters state exactly what Requirements 6.2 and 6.3 claim:
    zero decodes on both pass-through branches, and for Downscale_Off no
    Pillow whatsoever.
    """
    counters = SimpleNamespace(resizes=0, header_reads=0, pillow_imports=0)
    real_resize = dda_llm_image._resize_and_encode
    real_header = dda_llm_image._pillow_header_dimensions
    real_import = dda_llm_image._import_pillow_image

    def counting_resize(*args, **kwargs):
        counters.resizes += 1
        return real_resize(*args, **kwargs)

    def counting_header(*args, **kwargs):
        counters.header_reads += 1
        return real_header(*args, **kwargs)

    def counting_import(*args, **kwargs):
        counters.pillow_imports += 1
        return real_import(*args, **kwargs)

    dda_llm_image._resize_and_encode = counting_resize
    dda_llm_image._pillow_header_dimensions = counting_header
    dda_llm_image._import_pillow_image = counting_import
    try:
        yield counters
    finally:
        dda_llm_image._resize_and_encode = real_resize
        dda_llm_image._pillow_header_dimensions = real_header
        dda_llm_image._import_pillow_image = real_import


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_general_dims = st.tuples(st.integers(1, 4000), st.integers(1, 4000))
# Extreme aspect ratios: the short edge floors toward zero (Req 6.5).
_extreme_wide = st.tuples(st.integers(3000, 4000), st.integers(1, 5))
_extreme_tall = st.tuples(st.integers(1, 5), st.integers(3000, 4000))
# Exact-bound cases: an option value used directly as a dimension, so the
# `<=` boundary of Requirement 6.3 is hit exactly.
_exact_bound_w = st.tuples(st.sampled_from(MAX_IMAGE_EDGE_OPTIONS),
                           st.integers(1, 4000))
_exact_bound_h = st.tuples(st.integers(1, 4000),
                           st.sampled_from(MAX_IMAGE_EDGE_OPTIONS))

_dimensions = st.one_of(_general_dims, _extreme_wide, _extreme_tall,
                        _exact_bound_w, _exact_bound_h)

_settings = st.sampled_from((DOWNSCALE_OFF,) + MAX_IMAGE_EDGE_OPTIONS)
_mode_containers = st.sampled_from(_MODE_CONTAINERS)
_key_formats = st.sampled_from(('png', 'jpeg'))
_seeds = st.integers(min_value=0, max_value=7)


# ---------------------------------------------------------------------------
# Property 4 (task 1.2)
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 4: Downscaling is
# deterministic, shrinking, and idempotent at the bound
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(dims=_dimensions, mode_container=_mode_containers,
       image_format=_key_formats, setting=_settings, seed=_seeds,
       supply_dims=st.booleans())
def test_property_downscaling_is_deterministic_shrinking_and_idempotent(
        dims, mode_container, image_format, setting, seed, supply_dims):
    """
    **Feature: llm-model-token-and-image-sizing, Property 4: Downscaling
    is deterministic, shrinking, and idempotent at the bound**

    For any decodable source image, any source format, and any
    Downscale_Setting (Downscale_Off or a Max_Image_Edge option), the
    Image_Downscaler yields dimensions of at least 1 pixel per edge, no
    larger than the source dimensions, with the longer edge at most the
    Max_Image_Edge whenever a Max_Image_Edge is selected, equal to the
    floor-scaled dimensions of Requirement 6.4 whenever the source
    exceeds the bound, equal to the source bytes exactly whenever the
    setting is Downscale_Off or the source already fits the bound, and
    always in the source's Converse image format; and applying the same
    setting to the result yields bytes and dimensions equal to that
    result.

    **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 6.7**
    """
    mode, container = mode_container
    source_width, source_height = dims
    src = _source_bytes(mode, container, dims, seed)
    kwargs = {'source_dimensions': dims} if supply_dims else {}

    with _decode_spy() as spy:
        out, width, height = downscale_image(src, image_format, setting,
                                             **kwargs)

    # --- The dimension algebra (Requirements 6.4, 6.5) -------------------
    # At least one pixel per edge, never larger than the source, and the
    # longer edge inside the bound whenever a Max_Image_Edge is selected.
    assert width >= 1 and height >= 1
    assert width <= source_width and height <= source_height
    if setting is not DOWNSCALE_OFF:
        assert max(width, height) <= setting

    fits = (setting is DOWNSCALE_OFF
            or max(source_width, source_height) <= setting)

    if fits:
        # --- The two pass-through branches (Requirements 6.2, 6.3) ------
        # Byte identity at the `is` level: the caller's own bytes object
        # comes back, so no re-encode can have occurred, and the returned
        # dimensions are the Source_Dimensions.
        assert out is src
        assert (width, height) == (source_width, source_height)
        # The decoder spy: zero decode-and-re-encodes on both branches.
        assert spy.resizes == 0
        if setting is DOWNSCALE_OFF:
            # Downscale_Off never touches Pillow at all (Requirement 6.2).
            assert spy.pillow_imports == 0
        elif supply_dims:
            # The fit check runs on the caller's dimensions alone.
            assert spy.pillow_imports == 0
        else:
            # Only the header read is permitted, never a pixel decode.
            assert spy.header_reads == 1
    else:
        # --- The resize branch (Requirements 6.4, 6.5, 6.7) -------------
        # Exactly the floor-scaled dimensions of Requirement 6.4,
        # recomputed here independently of the module.
        scale_divisor = max(source_width, source_height)
        assert width == max(1, (source_width * setting) // scale_divisor)
        assert height == max(1, (source_height * setting) // scale_divisor)
        assert max(width, height) == setting
        assert out is not src and out != src
        # Exactly one decode-and-re-encode, one header read when the
        # caller did not supply the Source_Dimensions.
        assert spy.resizes == 1
        assert spy.header_reads == (0 if supply_dims else 1)
        # The emitted container is the key-derived Converse format, never
        # re-derived from the content (Requirement 6.7), and the returned
        # dimensions describe the bytes actually produced.
        with Image.open(io.BytesIO(out)) as reopened:
            assert reopened.format == ('PNG' if image_format == 'png'
                                       else 'JPEG')
            assert reopened.size == (width, height)

    # --- In-process determinism (Requirement 6.6) ------------------------
    # The same source bytes, format and setting yield the same output
    # bytes and dimensions on repeated invocation.
    out_again, width_again, height_again = downscale_image(
        src, image_format, setting, **kwargs)
    assert out_again == out
    assert (width_again, height_again) == (width, height)

    # --- Idempotence at the bound -------------------------------------
    # Applying the same setting to the result yields bytes and dimensions
    # equal to that result: a resized image's longer edge equals the
    # bound, so it passes through; a passed-through image passes through
    # again.
    out_fixed, width_fixed, height_fixed = downscale_image(
        out, image_format, setting)
    assert out_fixed == out
    assert out_fixed is out  # the pass-through returns the same object
    assert (width_fixed, height_fixed) == (width, height)


# ---------------------------------------------------------------------------
# Cross-process determinism (Requirement 6.6) — the one subprocess run
# ---------------------------------------------------------------------------

# The child re-imports dda_llm_image in a clean interpreter, downscales
# the same corpus, and reports sha256 digests and dimensions as JSON.
_CHILD_SCRIPT = (
    'import base64, hashlib, json, sys\n'
    'sys.path.insert(0, sys.argv[1])\n'
    'import dda_llm_image\n'
    'payload = json.load(sys.stdin)\n'
    'results = {}\n'
    'for case in payload["cases"]:\n'
    '    data = base64.b64decode(case["b64"])\n'
    '    out, width, height = dda_llm_image.downscale_image(\n'
    '        data, case["format"], case["setting"])\n'
    '    results[case["name"]] = [hashlib.sha256(out).hexdigest(),\n'
    '                             width, height]\n'
    'json.dump(results, sys.stdout)\n'
)


def _determinism_corpus():
    """(name, source bytes, key-derived format, setting) cases.

    One resize per (mode, container) pair so every mode-map entry's
    encoded output is digest-compared across processes, plus a
    content/extension-mismatch resize, and both pass-through branches.
    """
    cases = []
    for mode, container in _MODE_CONTAINERS:
        src = _source_bytes(mode, container, (1600, 1200), 7)
        cases.append((f'resize-{mode}-{container}', src,
                      container.lower(), 512))
    png_content = _source_bytes('RGB', 'PNG', (1600, 1200), 3)
    cases.append(('resize-mismatch-png-content-jpeg-key', png_content,
                  'jpeg', 512))
    cases.append(('pass-through-off', png_content, 'png', None))
    fitting = _source_bytes('RGB', 'JPEG', (800, 600), 5)
    cases.append(('pass-through-fits', fitting, 'jpeg', 1024))
    return cases


def test_cross_process_determinism_of_downscaled_bytes():
    """
    **Feature: llm-model-token-and-image-sizing, Property 4: Downscaling
    is deterministic, shrinking, and idempotent at the bound**

    The cross-process leg of Requirement 6.6: one subprocess run
    re-imports the module in a fresh interpreter and downscales the same
    corpus; the sha256 digest and dimensions of every output equal the
    digest and dimensions computed in this process, so identical inputs
    yield identical output bytes across separate processes.

    **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.6, 6.7**
    """
    corpus = _determinism_corpus()

    local_results = {}
    for name, src, image_format, setting in corpus:
        out, width, height = downscale_image(src, image_format, setting)
        local_results[name] = [hashlib.sha256(out).hexdigest(),
                               width, height]

    payload = {'cases': [
        {'name': name, 'b64': base64.b64encode(src).decode('ascii'),
         'format': image_format, 'setting': setting}
        for name, src, image_format, setting in corpus
    ]}
    completed = subprocess.run(
        [sys.executable, '-c', _CHILD_SCRIPT, _SHARED_LAYER],
        input=json.dumps(payload).encode('utf-8'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    assert completed.returncode == 0, completed.stderr.decode()
    child_results = json.loads(completed.stdout)
    assert child_results == local_results
