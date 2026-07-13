"""Test_Dataset staging for the simulation source elements.

The simulation compiler feeds camera sources with
``multifilesrc location={dataset_location}`` (Requirement 12.5).
``multifilesrc`` needs a printf-style sequence pattern, so the harness
stages the downloaded dataset images as ``frame_%05d.jpg`` in a local
directory and resolves the placeholder to that pattern.

Datasets are verified JPEG/PNG image sets at upload time (12.3/12.11);
PNG members are converted to JPEG at staging when Pillow is available
(it is installed in the container image) so the compiled
``multifilesrc ! jpegparse ! jpegdec`` chain can decode every frame.
"""

import os
import shutil
from typing import Dict, List, Tuple

#: Staged frame filename pattern handed to multifilesrc.
FRAME_PATTERN = "frame_%05d.jpg"

_JPEG_EXTENSIONS = (".jpg", ".jpeg")
_PNG_EXTENSIONS = (".png",)


def plan_staging(filenames: List[str]) -> List[Tuple[str, str, bool]]:
    """Deterministic staging plan for the dataset image files.

    Returns ``[(source_filename, staged_filename, needs_conversion)]``
    in sorted source order with sequential frame numbering starting at
    0 (multifilesrc's default start index). Non-image entries are
    excluded (the upload path only admits JPEG/PNG, but be safe).
    """
    images = sorted(
        name for name in filenames
        if name.lower().endswith(_JPEG_EXTENSIONS + _PNG_EXTENSIONS)
    )
    plan: List[Tuple[str, str, bool]] = []
    for index, name in enumerate(images):
        staged = FRAME_PATTERN % index
        needs_conversion = name.lower().endswith(_PNG_EXTENSIONS)
        plan.append((name, staged, needs_conversion))
    return plan


def stage_dataset(files: Dict[str, str], staging_dir: str) -> str:
    """Stage downloaded dataset files (``{filename: local_path}``) into
    ``staging_dir`` as a sequential JPEG frame set.

    Returns the ``{dataset_location}`` value: the multifilesrc pattern
    path. Raises ``ValueError`` when the dataset holds no usable images.
    """
    plan = plan_staging(list(files.keys()))
    if not plan:
        raise ValueError("The Test_Dataset contains no JPEG/PNG images")

    os.makedirs(staging_dir, exist_ok=True)
    for source_name, staged_name, needs_conversion in plan:
        source_path = files[source_name]
        target_path = os.path.join(staging_dir, staged_name)
        if needs_conversion:
            _convert_to_jpeg(source_path, target_path)
        else:
            shutil.copyfile(source_path, target_path)
    return os.path.join(staging_dir, FRAME_PATTERN)


def _convert_to_jpeg(source_path: str, target_path: str) -> None:
    """PNG -> JPEG so the jpegdec-based sim chain decodes it."""
    from PIL import Image  # installed in the container image
    with Image.open(source_path) as image:
        image.convert("RGB").save(target_path, "JPEG", quality=95)
