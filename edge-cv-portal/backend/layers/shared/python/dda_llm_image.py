"""
DDA LLM Image Utility — the Image_Downscaler for the `llm:` auto-label
family.

Module contract: no boto3, no I/O, no network, and **no Pillow at import
time**. Pillow is imported lazily, inside `_import_pillow_image()`, which
only the resize path and the Pillow header fallback call. Importing this
module therefore never requires the imaging layer, and the Downscale_Off
path never pays Pillow's import cost — which is what makes attaching this
module to two more Lambda functions free for every existing job
(Requirements 6.1, 6.2).

The Downscaled_Image is a `(bytes, width, height)` triple. The output
container is always the Converse format the caller derived from the object
key and is never re-derived from the content, so no png/jpeg conversion can
occur in either direction (Requirement 6.7).

Determinism is bought with explicitly pinned resampling and encoder
parameters rather than with Pillow's defaults: every default that could vary
with the source (JPEG subsampling, metadata passthrough, mode conversion) or
with the Pillow build is pinned in the constant block below, so the same
source bytes, format and Downscale_Setting yield the same output bytes in
every process and in both request paths (Requirement 6.6).

Requirements: 5.1, 5.9, 5.12, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.9, 6.10,
6.11, 7.6
"""
from __future__ import annotations

import io
import struct
from typing import Any, Dict, Optional, Tuple

# The seven Downscale_Setting values (Requirement 5.1). Downscale_Off is
# None on the wire and in every record, so no string can be confused for a
# bound.
DOWNSCALE_OFF = None
MAX_IMAGE_EDGE_OPTIONS = (512, 768, 1024, 1280, 1536, 2048)

# Largest decoded source we accept, refused from the header alone and before
# any pixel decode (Requirement 6.10).
MAX_SOURCE_PIXEL_COUNT = 100_000_000

IMAGE_FORMAT_PNG = 'png'
IMAGE_FORMAT_JPEG = 'jpeg'


class DownscaleError(Exception):
    """The source could not be decoded, sized or re-encoded
    (Requirements 6.9, 6.10).

    Carries only the reason text; the caller owns the failure *category*
    (`unsupported_image_content` for a target image,
    `unreadable_example_image` for an attached Few_Shot_Example), so this
    module never invents a category.

    `pixel_count` is set only by the Max_Source_Pixel_Count refusal, where
    it is the declared source pixel count. It lets the caller pick the
    oversize reason shape without parsing the reason text; it is None for
    every other refusal.
    """

    def __init__(self, reason: str, *, pixel_count: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.pixel_count = pixel_count


# ---------------------------------------------------------------------------
# Pinned resampling and encoder parameters (Requirement 6.6)
#
# `RESAMPLING_FILTER` cannot be a module constant without importing Pillow at
# import time, which this module's contract forbids, so the one pinned filter
# is named here and resolved as `Image.Resampling.<name>` inside the resize
# path. It is still exactly one value in exactly one place.
# ---------------------------------------------------------------------------

RESAMPLING_FILTER_NAME = 'LANCZOS'

# `resize`'s default is None (no pre-reduce). Pinning a value makes the
# deterministic integer `reduce()` pre-shrink part of the contract rather
# than an optimization, and it is what brings a large-source resize inside
# the 5 s Downscale_Duration_Bound (Requirement 6.11).
REDUCING_GAP = 2.0

JPEG_SAVE_PARAMS: Dict[str, Any] = {
    'format': 'JPEG',
    'quality': 85,
    'subsampling': 2,        # 4:2:0, pinned — the default (-1) inherits the
                             # source JPEG's subsampling
    'optimize': False,
    'progressive': False,
    'exif': b'',             # no EXIF passthrough (capture timestamps,
                             # device identity)
    'comment': b'',
    'icc_profile': None,     # no ICC passthrough
    'dpi': (0, 0),           # no JFIF density inherited from the source
}

PNG_SAVE_PARAMS: Dict[str, Any] = {
    'format': 'PNG',
    'optimize': False,
    'compress_level': 6,     # pinned zlib level; optimize=True would force 9
    'icc_profile': None,     # PngImagePlugin falls back to info['icc_profile']
    'pnginfo': None,         # no text and no tIME chunk
}

# Deterministic mode conversion, keyed on the SOURCE mode alone — never on
# `img.info`, never on "does this image happen to have transparency".
# Palette images must be converted before resampling (interpolating palette
# indices produces garbage) and JPEG can hold neither RGBA nor P.
JPEG_MODE_MAP = {'L': 'L', '1': 'L', 'I': 'L', 'I;16': 'L'}
PNG_MODE_MAP = {'L': 'L', 'LA': 'LA', 'RGB': 'RGB', 'RGBA': 'RGBA',
                '1': 'L', 'I': 'L', 'I;16': 'L',
                'P': 'RGBA', 'PA': 'RGBA'}
_DEFAULT_MODE = 'RGB'


# ---------------------------------------------------------------------------
# Downscale_Setting normalization
# ---------------------------------------------------------------------------

def normalize_downscale_setting(value: Any) -> Optional[int]:
    """
    The Downscale_Setting a record or request carries, as either None
    (Downscale_Off) or one Max_Image_Edge option (Requirements 5.9, 5.12).

    Total and safe, in the shape of `resolve_model_image_limit`: absent,
    null, boolean, string (including `'1024'`), float (including `1024.0`)
    and any integer outside MAX_IMAGE_EDGE_OPTIONS all resolve to None, so a
    malformed persisted value degrades to Downscale_Off and can never fail a
    job.

    Args:
        value: The persisted or submitted Downscale_Setting, of any type

    Returns:
        None (Downscale_Off) or one value from MAX_IMAGE_EDGE_OPTIONS
    """
    # bool is an int subclass; reject it before the int check.
    if isinstance(value, bool) or not isinstance(value, int):
        return DOWNSCALE_OFF
    if value not in MAX_IMAGE_EDGE_OPTIONS:
        return DOWNSCALE_OFF
    return value


# ---------------------------------------------------------------------------
# Dependency-free dimension parsing
# ---------------------------------------------------------------------------

def declared_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    """
    (width, height) parsed from PNG IHDR / JPEG SOF headers, or None.

    Byte-for-byte the algorithm `dda_autolabel_worker._image_dimensions` and
    `dda_labeling._preview_image_dimensions` have always used, relocated here
    so there is one copy; both of those functions become thin delegations and
    keep accepting exactly the inputs they accepted before (Requirement 7.6).

    Dependency-free: this is what lets the already-fits check and the
    Max_Source_Pixel_Count refusal happen with no Pillow at all.
    """
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n' and len(image_bytes) >= 24:
        width, height = struct.unpack('>II', image_bytes[16:24])
        return (width, height) if width and height else None
    if image_bytes[:2] == b'\xff\xd8':
        index = 2
        while index + 9 <= len(image_bytes):
            if image_bytes[index] != 0xFF:
                index += 1
                continue
            marker = image_bytes[index + 1]
            # Padding / standalone markers carry no length segment.
            if marker in (0xFF, 0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if index + 4 > len(image_bytes):
                break
            segment_length = struct.unpack(
                '>H', image_bytes[index + 2:index + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if index + 9 <= len(image_bytes):
                    height, width = struct.unpack(
                        '>HH', image_bytes[index + 5:index + 9])
                    return (width, height) if width and height else None
                break
            index += 2 + segment_length
    return None


# ---------------------------------------------------------------------------
# The Image_Downscaler
# ---------------------------------------------------------------------------

def downscale_image(image_bytes: bytes, image_format: str,
                    downscale_setting: Optional[int],
                    *, source_dimensions: Optional[Tuple[int, int]] = None,
                    ) -> Tuple[bytes, int, int]:
    """
    The Downscaled_Image for one source image.

    Steps, in the order the requirements dictate:

    1. Downscale_Off returns the caller's own `bytes` object with **no Pillow
       import and no decode** (Requirement 6.2).
    2. Dimensions still unknown from the header are read from
       `Image.open(...).size` — the container header only, never `load()`
       (Requirement 6.9).
    3. A width or height below 1 is refused (Requirement 6.9).
    4. A pixel count above MAX_SOURCE_PIXEL_COUNT is refused **before any
       `load()`** (Requirement 6.10).
    5. A source whose longer edge already fits the bound returns its own
       bytes with **no decode**, which is what keeps the request
       byte-identical to the pre-feature request (Requirement 6.3).
    6. Only then is the image resized and re-encoded (Requirements 6.4, 6.5,
       6.7).

    Args:
        image_bytes: the source bytes, never mutated
        image_format: the Converse format derived from the object key
            ('png' | 'jpeg') — the OUTPUT container, never re-derived from
            the content, so no cross-conversion can occur (Requirement 6.7)
        downscale_setting: None (Downscale_Off) or one Max_Image_Edge
        source_dimensions: the caller's already-parsed Source_Dimensions,
            passed in to avoid a second header parse; when omitted they are
            parsed here

    Returns:
        (bytes, width, height) — the source bytes and Source_Dimensions
        unchanged for Downscale_Off or an already-fitting source, otherwise
        the re-encoded bytes and the floor-scaled dimensions of
        Requirement 6.4

    Raises:
        DownscaleError: undecodable, zero-dimension, over-size or
            unencodable (Requirements 6.9, 6.10)
    """
    max_image_edge = normalize_downscale_setting(downscale_setting)
    dimensions = _known_dimensions(source_dimensions)

    # Step 1 — Downscale_Off: the same bytes object, no Pillow, no decode.
    if max_image_edge is DOWNSCALE_OFF:
        if dimensions is None:
            dimensions = declared_dimensions(image_bytes)
        if dimensions is None:
            raise DownscaleError(
                'the source image dimensions could not be determined')
        return (image_bytes, dimensions[0], dimensions[1])

    # Step 2 — the header read, no pixel decode.
    if dimensions is None:
        dimensions = _pillow_header_dimensions(image_bytes)
    source_width, source_height = dimensions

    # Step 3 — a degenerate source is refused.
    if source_width < 1 or source_height < 1:
        raise DownscaleError(
            f'the source image declares {source_width}x{source_height} '
            'pixels, which is not a usable image size')

    # Step 4 — the pixel-count refusal, before any load().
    pixel_count = source_width * source_height
    if pixel_count > MAX_SOURCE_PIXEL_COUNT:
        raise DownscaleError(
            f'declares {source_width}x{source_height} = {pixel_count} '
            f'pixels, above the {MAX_SOURCE_PIXEL_COUNT} pixel limit',
            pixel_count=pixel_count)

    # Step 5 — already fits: the same bytes object, no decode.
    if max(source_width, source_height) <= max_image_edge:
        return (image_bytes, source_width, source_height)

    # Step 6 — resize and re-encode into the caller's key-derived container.
    target_width, target_height = _target_dimensions(
        source_width, source_height, max_image_edge)
    resized_bytes = _resize_and_encode(
        image_bytes, image_format, target_width, target_height)
    return (resized_bytes, target_width, target_height)


def _known_dimensions(source_dimensions: Any) -> Optional[Tuple[int, int]]:
    """The caller's Source_Dimensions when they are a usable integer pair.

    Anything else (None, a malformed pair, non-integers, booleans) is treated
    as "not supplied" so the module falls back to its own parse rather than
    trusting a value it cannot use.
    """
    if not isinstance(source_dimensions, (tuple, list)):
        return None
    if len(source_dimensions) != 2:
        return None
    width, height = source_dimensions
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
    return (width, height)


def _target_dimensions(source_width: int, source_height: int,
                       max_image_edge: int) -> Tuple[int, int]:
    """The Sent_Dimensions of Requirement 6.4, in integer arithmetic.

    Integer floor division rather than `math.floor` on a float quotient: it
    is exact for every dimension the Max_Source_Pixel_Count bound admits, so
    there is no float-rounding term to reason about. `max(1, ...)` covers the
    extreme-aspect-ratio case — a 5000x1 source at a 512 bound floors its
    short edge to 0 (Requirement 6.5).
    """
    scale_divisor = max(source_width, source_height)
    target_width = max(1, (source_width * max_image_edge) // scale_divisor)
    target_height = max(1, (source_height * max_image_edge) // scale_divisor)
    return target_width, target_height


# ---------------------------------------------------------------------------
# The Pillow paths — the only places Pillow is imported
# ---------------------------------------------------------------------------

def _import_pillow_image():
    """`PIL.Image`, imported lazily, with the bomb guard aligned.

    `Image.MAX_IMAGE_PIXELS` is set to MAX_SOURCE_PIXEL_COUNT so Pillow's
    decompression-bomb guard coincides with this feature's bound instead of
    firing ~10.5 M pixels earlier and refusing sources the spec accepts.
    `Image.DecompressionBombError` is converted to `DownscaleError` by the
    callers of this function, so there is one refusal rule and one reason
    shape.
    """
    from PIL import Image  # noqa: PLC0415 — lazy by contract

    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXEL_COUNT
    return Image


def _pillow_header_dimensions(image_bytes: bytes) -> Tuple[int, int]:
    """(width, height) from the container header alone — no `load()`.

    `Image.open` reads only the header, so `img.size` is available before any
    pixel decode. That is what lets the Max_Source_Pixel_Count refusal happen
    without decoding the full image even for a source whose PNG IHDR / JPEG
    SOF parse failed (Requirements 6.9, 6.10).
    """
    Image = _import_pillow_image()
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            width, height = source.size
    except Exception as exc:  # includes DecompressionBombError / Warning
        raise DownscaleError(
            f'the source image could not be decoded: {exc}') from exc
    return int(width), int(height)


def _resize_and_encode(image_bytes: bytes, image_format: str,
                       target_width: int, target_height: int) -> bytes:
    """The resized image re-encoded in the caller's key-derived container.

    Every parameter that could vary with the source or with the Pillow build
    is pinned (see the constant block). `info` is cleared before `save` so no
    plugin can inherit the source's `jfif`, `dpi`, `adobe`, `transparency`,
    `icc_profile` or `gamma`. No `ImageOps.exif_transpose`: applying EXIF
    orientation would change pixel content relative to what the Downscale_Off
    path sends. First frame only — `Image.open` positions on frame 0 and
    there is no `seek` loop (Requirement 6.6).
    """
    Image = _import_pillow_image()
    is_png = _is_png_format(image_format)
    mode_map = PNG_MODE_MAP if is_png else JPEG_MODE_MAP
    save_params = dict(PNG_SAVE_PARAMS if is_png else JPEG_SAVE_PARAMS)
    resampling_filter = getattr(Image.Resampling, RESAMPLING_FILTER_NAME)

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            target_mode = mode_map.get(source.mode, _DEFAULT_MODE)
            prepared = (source if source.mode == target_mode
                        else source.convert(target_mode))
            resized = prepared.resize((target_width, target_height),
                                      resampling_filter,
                                      reducing_gap=REDUCING_GAP)
        # Nothing for any encoder to inherit from the source.
        resized.info.clear()
        buffer = io.BytesIO()
        resized.save(buffer, **save_params)
        return buffer.getvalue()
    except DownscaleError:
        raise
    except Exception as exc:  # includes DecompressionBombError / Warning
        raise DownscaleError(str(exc) or exc.__class__.__name__) from exc


def _is_png_format(image_format: Any) -> bool:
    """True only for the png Converse format the caller derived from the
    object key; every other accepted source is jpeg (Requirement 6.7)."""
    return (isinstance(image_format, str)
            and image_format.lower() == IMAGE_FORMAT_PNG)
