"""Dataset staging plans for the multifilesrc-fed simulation sources
(Requirement 12.5).

Staging normalizes every source frame through Pillow into a clean
baseline JPEG so the ``multifilesrc ! jpegparse ! jpegdec`` chain can
decode it, so these tests exercise real images rather than fake bytes.
"""

import os

import pytest
from PIL import Image

from harness.dataset import FRAME_PATTERN, plan_staging, stage_dataset


def _write_image(path, *, mode="RGB", size=(16, 12), fmt="JPEG", **save_kwargs):
    """Write a real image to ``path`` and return its string path."""
    image = Image.new(mode, size, color=0)
    image.save(str(path), fmt, **save_kwargs)
    return str(path)


class TestPlanStaging:
    def test_sequential_frames_in_sorted_order(self):
        plan = plan_staging(["b.jpg", "a.jpg", "c.jpeg"])
        assert plan == [
            ("a.jpg", "frame_00000.jpg"),
            ("b.jpg", "frame_00001.jpg"),
            ("c.jpeg", "frame_00002.jpg"),
        ]

    def test_png_and_jpeg_both_included(self):
        plan = plan_staging(["x.png", "y.JPG"])
        assert plan == [
            ("x.png", "frame_00000.jpg"),
            ("y.JPG", "frame_00001.jpg"),
        ]

    def test_non_images_excluded(self):
        assert plan_staging(["notes.txt", "manifest.json"]) == []

    def test_empty_input(self):
        assert plan_staging([]) == []


def _assert_baseline_jpeg(path):
    """Assert the staged file is a baseline (non-progressive) RGB JPEG."""
    with Image.open(path) as staged:
        staged.load()
        assert staged.format == "JPEG"
        assert staged.mode == "RGB"
        # Baseline JPEGs do not carry the progressive marker.
        assert not staged.info.get("progressive")
        assert not staged.info.get("progression")


class TestStageDataset:
    def test_stages_frames_and_returns_pattern(self, tmp_path):
        source = tmp_path / "download"
        source.mkdir()
        files = {
            "b.jpg": _write_image(source / "b.jpg", fmt="JPEG"),
            "a.jpg": _write_image(source / "a.jpg", fmt="JPEG"),
        }

        staging = str(tmp_path / "staged")
        location = stage_dataset(files, staging)

        assert location == os.path.join(staging, FRAME_PATTERN)
        assert sorted(os.listdir(staging)) == \
            ["frame_00000.jpg", "frame_00001.jpg"]
        for staged_name in ("frame_00000.jpg", "frame_00001.jpg"):
            _assert_baseline_jpeg(os.path.join(staging, staged_name))

    def test_normalizes_non_baseline_and_non_rgb_inputs(self, tmp_path):
        source = tmp_path / "download"
        source.mkdir()
        files = {
            # progressive JPEG -> must become baseline
            "prog.jpg": _write_image(
                source / "prog.jpg", fmt="JPEG", progressive=True
            ),
            # CMYK JPEG -> must be converted to RGB
            "cmyk.jpg": _write_image(
                source / "cmyk.jpg", mode="CMYK", fmt="JPEG"
            ),
            # palette PNG -> must be converted to RGB JPEG
            "pal.png": _write_image(source / "pal.png", mode="P", fmt="PNG"),
            # RGBA PNG -> alpha must be dropped
            "alpha.png": _write_image(
                source / "alpha.png", mode="RGBA", fmt="PNG"
            ),
        }

        staging = str(tmp_path / "staged")
        stage_dataset(files, staging)

        staged_files = sorted(os.listdir(staging))
        assert staged_files == [
            "frame_00000.jpg",
            "frame_00001.jpg",
            "frame_00002.jpg",
            "frame_00003.jpg",
        ]
        for staged_name in staged_files:
            _assert_baseline_jpeg(os.path.join(staging, staged_name))

    def test_corrupt_image_is_skipped_and_frames_renumbered(self, tmp_path):
        source = tmp_path / "download"
        source.mkdir()
        good_a = _write_image(source / "a.jpg", fmt="JPEG")
        corrupt = source / "b.jpg"
        corrupt.write_bytes(b"\xff\xd8not a real jpeg")
        good_c = _write_image(source / "c.jpg", fmt="JPEG")
        files = {"a.jpg": good_a, "b.jpg": str(corrupt), "c.jpg": good_c}

        staging = str(tmp_path / "staged")
        location = stage_dataset(files, staging)

        # The corrupt middle frame is skipped; survivors renumber 0,1.
        assert location == os.path.join(staging, FRAME_PATTERN)
        assert sorted(os.listdir(staging)) == \
            ["frame_00000.jpg", "frame_00001.jpg"]
        for staged_name in ("frame_00000.jpg", "frame_00001.jpg"):
            _assert_baseline_jpeg(os.path.join(staging, staged_name))

    def test_all_corrupt_dataset_raises(self, tmp_path):
        source = tmp_path / "download"
        source.mkdir()
        for name in ("a.jpg", "b.jpg"):
            (source / name).write_bytes(b"\xff\xd8garbage")
        files = {name: str(source / name) for name in ("a.jpg", "b.jpg")}

        with pytest.raises(ValueError):
            stage_dataset(files, str(tmp_path / "staged"))

    def test_empty_dataset_raises(self, tmp_path):
        with pytest.raises(ValueError):
            stage_dataset({}, str(tmp_path / "staged"))
