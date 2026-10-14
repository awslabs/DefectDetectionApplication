"""Test_Dataset staging for the simulation source elements.

The simulation compiler feeds camera sources with
``multifilesrc location={dataset_location}`` (Requirement 12.5).
``multifilesrc`` needs a printf-style sequence pattern, so the harness
stages the downloaded dataset images as ``frame_%05d.jpg`` in a local
directory and resolves the placeholder to that pattern.

Datasets are verified JPEG/PNG image sets at upload time (12.3/12.11),
but the compiled ``multifilesrc ! jpegparse ! jpegdec`` chain only
decodes *baseline* JPEGs. Non-baseline JPEGs (progressive, CMYK,
EXIF-thumbnail, unusual subsampling) make stock ``jpegdec`` fail with
"Improper call to JPEG library". To guarantee every staged frame is
decodable, staging now normalizes *every* source image through Pillow
into a clean baseline JPEG (RGB, progressive=False, no EXIF), not just
PNGs.
"""

import logging
import os
from typing import Dict, List, Tuple

logger = logging.getLogger("sandbox-harness")

#: Staged frame filename pattern handed to multifilesrc.
FRAME_PATTERN = "frame_%05d.jpg"

_JPEG_EXTENSIONS = (".jpg", ".jpeg")
_PNG_EXTENSIONS = (".png",)
_SUPPORTED_EXTENSIONS = _JPEG_EXTENSIONS + _PNG_EXTENSIONS


def plan_staging(filenames: List[str]) -> List[Tuple[str, str]]:
    """Deterministic staging plan for the dataset image files.

    Returns ``[(source_filename, staged_filename)]`` in sorted source
    order with sequential frame numbering starting at 0 (multifilesrc's
    default start index). Non-image entries are excluded (the upload
    path only admits JPEG/PNG, but be safe).

    Every staged frame is re-encoded into a baseline JPEG at staging
    time regardless of source format, so there is no longer a
    per-frame "needs conversion" distinction.
    """
    images = sorted(
        name for name in filenames
        if name.lower().endswith(_SUPPORTED_EXTENSIONS)
    )
    return [(name, FRAME_PATTERN % index) for index, name in enumerate(images)]


def stage_dataset(files: Dict[str, str], staging_dir: str) -> str:
    """Stage downloaded dataset files (``{filename: local_path}``) into
    ``staging_dir`` as a sequential baseline-JPEG frame set.

    Every source image is normalized through Pillow into a clean,
    stock-``jpegdec``-decodable baseline JPEG. Images Pillow cannot open
    are skipped with a warning so a single corrupt frame does not abort
    the run; the remaining usable frames are renumbered sequentially.

    Returns the ``{dataset_location}`` value: the multifilesrc pattern
    path. Raises ``ValueError`` when the dataset holds no usable images.
    """
    plan = plan_staging(list(files.keys()))
    if not plan:
        raise ValueError("The Test_Dataset contains no JPEG/PNG images")

    os.makedirs(staging_dir, exist_ok=True)

    staged_index = 0
    for source_name, _ in plan:
        source_path = files[source_name]
        target_path = os.path.join(staging_dir, FRAME_PATTERN % staged_index)
        try:
            _normalize_to_baseline_jpeg(source_path, target_path)
        except Exception as error:  # noqa: BLE001 - skip unreadable frames
            logger.warning(
                "Skipping unreadable dataset image %s: %s", source_name, error
            )
            continue
        staged_index += 1

    if staged_index == 0:
        raise ValueError("The Test_Dataset contains no usable images")

    return os.path.join(staging_dir, FRAME_PATTERN)


def _normalize_to_baseline_jpeg(source_path: str, target_path: str) -> None:
    """Re-encode any supported image into a clean baseline JPEG.

    Converting to RGB drops alpha, CMYK, and palette modes; saving with
    ``progressive=False`` and without EXIF strips the progressive/
    EXIF-thumbnail quirks that break stock ``jpegdec``.
    """
    from PIL import Image  # installed in the container image
    with Image.open(source_path) as image:
        rgb = image.convert("RGB")
        rgb.save(target_path, "JPEG", quality=95, progressive=False)
