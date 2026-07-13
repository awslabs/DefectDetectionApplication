"""Dataset staging plans for the multifilesrc-fed simulation sources
(Requirement 12.5)."""

import os

import pytest

from harness.dataset import FRAME_PATTERN, plan_staging, stage_dataset


class TestPlanStaging:
    def test_sequential_frames_in_sorted_order(self):
        plan = plan_staging(["b.jpg", "a.jpg", "c.jpeg"])
        assert plan == [
            ("a.jpg", "frame_00000.jpg", False),
            ("b.jpg", "frame_00001.jpg", False),
            ("c.jpeg", "frame_00002.jpg", False),
        ]

    def test_png_marked_for_conversion(self):
        plan = plan_staging(["x.png", "y.JPG"])
        assert plan == [
            ("x.png", "frame_00000.jpg", True),
            ("y.JPG", "frame_00001.jpg", False),
        ]

    def test_non_images_excluded(self):
        assert plan_staging(["notes.txt", "manifest.json"]) == []

    def test_empty_input(self):
        assert plan_staging([]) == []


class TestStageDataset:
    def test_stages_jpegs_and_returns_pattern(self, tmp_path):
        source = tmp_path / "download"
        source.mkdir()
        for name in ("b.jpg", "a.jpg"):
            (source / name).write_bytes(b"\xff\xd8fakejpeg")
        files = {name: str(source / name) for name in ("b.jpg", "a.jpg")}

        staging = str(tmp_path / "staged")
        location = stage_dataset(files, staging)

        assert location == os.path.join(staging, FRAME_PATTERN)
        assert sorted(os.listdir(staging)) == \
            ["frame_00000.jpg", "frame_00001.jpg"]
        # sorted source order: a.jpg -> frame 0
        with open(os.path.join(staging, "frame_00000.jpg"), "rb") as f:
            assert f.read() == b"\xff\xd8fakejpeg"

    def test_empty_dataset_raises(self, tmp_path):
        with pytest.raises(ValueError):
            stage_dataset({}, str(tmp_path / "staged"))
