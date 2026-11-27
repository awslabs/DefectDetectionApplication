# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `datasets/rewrite_manifest_source_refs.py` and
`datasets/check_assembly_manifests.py` (car-assembly-check tasks 2.1-2.3;
design §9, DD13, DD18).

Synthetic 8 x 6 instance manifests (rectangle masks encoded with
`dda_manifest.rle_encode`), tiny PNGs written with Pillow, and a synthetic
eleven-column triage CSV in the real vocabulary:

* the rewriter changes only the `source-ref` prefix and refuses a wrong
  prefix, a missing file at the relative key under `--verify-dir`, and a
  line that is not JSON, writing nothing;
* every shape check's pass and fail path, a zero-annotation line accepted
  without `--triage`;
* the exact count rule against the triage `piece_counts` (never derived
  from the verdict), the join problems, a malformed `piece_counts` cell;
* the derived / merge identity checks;
* the converter's split replay with the per-split set counts, the
  no-Fail_Set-in-`train` stop, and the cross-set split identity.
"""
import json
import os
import sys

import pytest

import dda_manifest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATASETS = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "datasets"))
if _DATASETS not in sys.path:
    sys.path.append(_DATASETS)

import check_assembly_manifests as cam  # noqa: E402
import rewrite_manifest_source_refs as rw  # noqa: E402

W, H = 8, 6
CLASSES = ["front_fender", "rear_fender", "roof"]
PREFIX = "s3://bucket/assembly-photos/"
MONO = "s3://bucket/assembly-photos-monochrome/"
RECTS = {"front_fender": (0, 0, 2, 2), "rear_fender": (3, 0, 5, 2), "roof": (6, 0, 8, 2),
         "extra": (0, 3, 2, 5), "extra2": (3, 3, 5, 5)}
HEADER = ("filename,set,source_path,verdict,misplaced_pieces,missing_pieces,"
          "occluder,confidence,notes,piece_counts,unlabeled_stray")


def rect_rle(rect, width=W, height=H):
    x0, y0, x1, y1 = rect
    mask = [1 if x0 <= x < x1 and y0 <= y < y1 else 0
            for y in range(height) for x in range(width)]
    return dda_manifest.rle_encode(mask, width, height)


def line(name, instances, prefix=PREFIX, size=(W, H), class_map=None, rects=None):
    """One instance-segmentation line; ``instances`` is a list of class
    names, each drawn as its rectangle (``rects`` overrides positions)."""
    width, height = size
    rects = rects or {}
    annotations = []
    for index, cls in enumerate(instances):
        rect = rects.get(index) or RECTS[cls if cls in RECTS else "extra"]
        rle = rect_rle(rect, width, height)
        x0, y0, x1, y1 = rect
        annotations.append({"class_id": CLASSES.index(cls) if cls in CLASSES else 7,
                            "left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0,
                            "rle": rle})
    return json.dumps({
        "source-ref": prefix + name,
        "instance-segmentation": {"image_size": [{"width": width, "height": height, "depth": 3}],
                                  "annotations": annotations},
        "instance-segmentation-metadata": {
            "objects": [{"confidence": 1.0} for _ in annotations],
            "class-map": class_map or {str(i): c for i, c in enumerate(CLASSES)},
            "type": "groundtruth/instance-segmentation", "human-annotated": "yes",
            "creation-date": "2026-11-25T00:00:00", "job-name": "labeling-test"},
    })


def write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("".join(item + "\n" for item in lines), encoding="utf-8")
    return str(path)


def triage_csv(tmp_path, rows, name="triage.csv"):
    """rows: (filename, verdict, misplaced, missing, occluder, counts)."""
    body = [HEADER]
    for filename, verdict, misplaced, missing, occluder, counts in rows:
        body.append("{0},assembly48,/d/{0},{1},{2},{3},{4},high,note for {0},"
                    "front_fender={5};rear_fender={6};roof={7},0".format(
                        filename, verdict, misplaced, missing, occluder, *counts))
    path = tmp_path / name
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def run(capsys, *argv):
    code = cam.main(list(argv) + ["--image-size", "{0}x{1}".format(W, H),
                                  "--source-prefix", PREFIX])
    return code, capsys.readouterr().out


GOOD = ("good.jpg", "correct", "", "", "none", (1, 1, 1))
SWAPPED = ("swap.jpg", "incorrect", "front_fender;rear_fender", "", "none", (1, 1, 1))
TWO = ("two.jpg", "incorrect_and_missing", "front_fender", "rear_fender", "none", (2, 0, 1))
ONE_ZERO = ("onezero.jpg", "incorrect_and_missing", "", "rear_fender", "none", (1, 0, 1))
EMPTY = ("empty.jpg", "missing", "", "front_fender;rear_fender;roof", "none", (0, 0, 0))
TWO_RECTS = {0: RECTS["front_fender"], 1: RECTS["extra"]}


# ---------------------------------------------------------------------------
# 2.1 the rewriter
# ---------------------------------------------------------------------------


class TestRewrite:
    def labeled(self, tmp_path):
        lines = [line("a.jpg", ["front_fender", "roof"]), line("b.jpg", []),
                 line("c.jpg", ["rear_fender"])]
        return lines, write(tmp_path, "in.manifest", lines)

    def test_happy_path_keeps_order_and_every_other_field(self, tmp_path, capsys):
        lines, src = self.labeled(tmp_path)
        verify = tmp_path / "mono"
        verify.mkdir()
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (verify / name).write_bytes(b"x")
        out = str(tmp_path / "out.manifest")
        assert rw.main(["--manifest", src, "--out", out, "--from-prefix", PREFIX,
                        "--to-prefix", MONO, "--verify-dir", str(verify)]) == 0
        assert "lines: 3, rewritten: 3" in capsys.readouterr().out
        written = open(out, encoding="utf-8").read().splitlines()
        assert len(written) == 3
        for before, after in zip(lines, written):
            a, b = json.loads(before), json.loads(after)
            assert b["source-ref"] == a["source-ref"].replace(PREFIX, MONO)
            a.pop("source-ref")
            b.pop("source-ref")
            assert a == b
            # Byte-identical except the prefix.
            assert after == before.replace(PREFIX, MONO, 1)

    def test_wrong_prefix_refused(self, tmp_path, capsys):
        lines, _ = self.labeled(tmp_path)
        src = write(tmp_path, "in.manifest", lines + [line("d.jpg", [], prefix="s3://other/")])
        out = tmp_path / "out.manifest"
        assert rw.main(["--manifest", src, "--out", str(out), "--from-prefix", PREFIX,
                        "--to-prefix", MONO]) == 1
        text = capsys.readouterr().out
        assert "line 4: source-ref 's3://other/d.jpg' does not start with" in text
        assert not out.exists()

    def test_missing_file_at_the_relative_key_refused(self, tmp_path, capsys):
        src = write(tmp_path, "in.manifest", [line("wrong-placements/x.jpg", []),
                                              line("y.jpg", [])])
        verify = tmp_path / "mono"
        (verify / "wrong-placements").mkdir(parents=True)
        (verify / "x.jpg").write_bytes(b"flat copy does not count")
        (verify / "y.jpg").write_bytes(b"x")
        out = tmp_path / "out.manifest"
        assert rw.main(["--manifest", src, "--out", str(out), "--from-prefix", PREFIX,
                        "--to-prefix", MONO, "--verify-dir", str(verify)]) == 1
        assert "line 1: wrong-placements/x.jpg is not a file under" in capsys.readouterr().out
        assert not out.exists()
        (verify / "wrong-placements" / "x.jpg").write_bytes(b"x")
        assert rw.main(["--manifest", src, "--out", str(out), "--from-prefix", PREFIX,
                        "--to-prefix", MONO, "--verify-dir", str(verify)]) == 0
        assert json.loads(out.read_text().splitlines()[0])["source-ref"] == MONO + "wrong-placements/x.jpg"

    def test_bad_json_refused(self, tmp_path, capsys):
        lines, _ = self.labeled(tmp_path)
        src = write(tmp_path, "in.manifest", [lines[0], "{not json"])
        out = tmp_path / "out.manifest"
        assert rw.main(["--manifest", src, "--out", str(out), "--from-prefix", PREFIX,
                        "--to-prefix", MONO]) == 1
        assert "line 2: not JSON" in capsys.readouterr().out
        assert not out.exists()

    def test_usage(self):
        with pytest.raises(SystemExit) as usage:
            rw.main(["--manifest", "x"])
        assert usage.value.code == 2


# ---------------------------------------------------------------------------
# 2.2 shape checks
# ---------------------------------------------------------------------------


class TestShape:
    def test_pass_with_a_zero_annotation_line(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("a.jpg", CLASSES), line("b.jpg", [])])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "2")
        assert code == 0, out
        assert "1 lines with zero annotations" in out
        assert "counts: not checked (no --triage)" in out

    def test_no_count_rule_without_triage(self, tmp_path, capsys):
        # Four polygons of one class: nothing to compare with without --triage.
        src = write(tmp_path, "l.manifest", [line("a.jpg", ["roof", "roof", "roof", "roof"])])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 0, out

    def test_line_count(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("a.jpg", CLASSES)])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "70")
        assert code == 1
        assert "hold 1 lines, expected 70" in out

    def test_image_size(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("a.jpg", CLASSES, size=(10, 6))])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 1
        assert "line 1 (a.jpg): image_size" in out

    def test_class_id_outside_the_class_map(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("a.jpg", ["front_fender", "wheel"])])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 1
        assert "class_id 7 is not in the class-map" in out

    def test_class_map_names_must_be_the_classes(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("a.jpg", [], class_map={"0": "plate"})])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 1
        assert "does not name exactly front_fender, rear_fender, roof" in out

    def test_rle_sum(self, tmp_path, capsys):
        entry = json.loads(line("a.jpg", ["roof"]))
        entry["instance-segmentation"]["annotations"][0]["rle"] = "0 5"
        src = write(tmp_path, "l.manifest", [json.dumps(entry)])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 1
        assert "instance 0 (roof): RLE counts sum 5 != width*height 48" in out

    def test_source_prefix(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("a.jpg", CLASSES, prefix="s3://elsewhere/")])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 1
        assert "is not under s3://bucket/assembly-photos/" in out

    def test_small_instance_listed(self, tmp_path, capsys):
        size = (40, 30)  # 0.5 % of 1200 pixels is 6
        src = write(tmp_path, "l.manifest", [
            line("a.jpg", ["roof", "front_fender"], size=size,
                 rects={0: (0, 0, 10, 10), 1: (20, 20, 21, 21)})])
        code = cam.main(["--labeled", src, "--expect-lines", "1", "--image-size", "40x30",
                         "--source-prefix", PREFIX])
        out = capsys.readouterr().out
        assert code == 1
        assert "instance 1 (front_fender): 1 pixels, below 0.5 % of the frame" in out
        assert "instance 0" not in out

    def test_not_json_and_usage(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", ["{nope"])
        code, out = run(capsys, "--labeled", src, "--expect-lines", "1")
        assert code == 1 and "line 1: not JSON" in out
        with pytest.raises(SystemExit) as usage:
            cam.main([])
        assert usage.value.code == 2


# ---------------------------------------------------------------------------
# 2.2 the exact count rule against the Triage_File
# ---------------------------------------------------------------------------


class TestCounts:
    def check(self, tmp_path, capsys, rows, lines):
        src = write(tmp_path, "l.manifest", lines)
        triage = triage_csv(tmp_path, rows)
        return run(capsys, "--labeled", src, "--triage", triage,
                   "--expect-lines", str(len(lines)))

    @pytest.mark.parametrize("row, instances, rects", [
        (GOOD, CLASSES, None),
        (SWAPPED, CLASSES, None),
        (TWO, ["front_fender", "front_fender", "roof"], TWO_RECTS),
        (ONE_ZERO, ["front_fender", "roof"], None),
        (EMPTY, [], None),
    ])
    def test_pass(self, tmp_path, capsys, row, instances, rects):
        code, out = self.check(tmp_path, capsys, [row], [line(row[0], instances, rects=rects)])
        assert code == 0, out
        assert "counts: 0 failures" in out

    @pytest.mark.parametrize("row, instances, rects, text", [
        (GOOD, ["front_fender", "front_fender", "rear_fender", "roof"], TWO_RECTS,
         "front_fender: 2 labeled, piece_counts says 1"),
        # A duplicate on a swapped-fender row is a failure too.
        (SWAPPED, ["front_fender", "rear_fender", "rear_fender", "roof"], {2: RECTS["extra"]},
         "rear_fender: 2 labeled, piece_counts says 1"),
        (TWO, ["front_fender", "roof"], None, "front_fender: 1 labeled, piece_counts says 2"),
        (TWO, ["front_fender", "front_fender", "rear_fender", "roof"], TWO_RECTS,
         "rear_fender: 1 labeled, piece_counts says 0"),
        (ONE_ZERO, ["front_fender", "rear_fender", "roof"], None,
         "rear_fender: 1 labeled, piece_counts says 0"),
        (GOOD, [], None, "front_fender: 0 labeled, piece_counts says 1"),
        # The labeled stray piece: a fourth polygon of any class.
        (GOOD, ["front_fender", "rear_fender", "roof", "roof"], {3: RECTS["extra"]},
         "roof: 2 labeled, piece_counts says 1"),
    ])
    def test_fail(self, tmp_path, capsys, row, instances, rects, text):
        code, out = self.check(tmp_path, capsys, [row], [line(row[0], instances, rects=rects)])
        assert code == 1
        assert text in out
        assert "confidence=high" in out and "note for {0}".format(row[0]) in out
        assert "cannot tell whether the label or the triage row is wrong" in out

    def test_zero_annotations_valid_only_for_000(self, tmp_path, capsys):
        code, out = self.check(tmp_path, capsys, [EMPTY, ONE_ZERO],
                               [line("empty.jpg", []), line("onezero.jpg", [])])
        assert code == 1
        assert "line 2 (onezero.jpg)" in out
        assert "line 1 (empty.jpg)" not in out

    def test_join_problems(self, tmp_path, capsys):
        code, out = self.check(tmp_path, capsys, [GOOD, SWAPPED],
                               [line("good.jpg", CLASSES), line("norow.jpg", CLASSES)])
        assert code == 1
        assert "image without a triage row: norow.jpg" in out
        assert "triage row without an image: swap.jpg" in out

    def test_malformed_piece_counts_is_a_listed_problem(self, tmp_path, capsys):
        src = write(tmp_path, "l.manifest", [line("good.jpg", CLASSES)])
        triage = tmp_path / "t.csv"
        triage.write_text(HEADER + "\ngood.jpg,assembly48,/d,correct,,,none,high,n,"
                                   "front_fender=1;roof=1,0\n")
        code, out = run(capsys, "--labeled", src, "--triage", str(triage), "--expect-lines", "1")
        assert code == 1
        assert "triage: row 2 (good.jpg)" in out and "piece_counts 'front_fender=1;roof=1'" in out

    def test_set_sizes_printed(self, tmp_path, capsys):
        code, out = self.check(tmp_path, capsys, [GOOD, SWAPPED],
                               [line("good.jpg", CLASSES), line("swap.jpg", CLASSES)])
        assert code == 0, out
        assert "triage: Known_Good 1 / Fail_Set 1 / Occluded_Correct 0" in out


# ---------------------------------------------------------------------------
# 2.2 derived and merge identity
# ---------------------------------------------------------------------------


class TestDerived:
    def test_pairwise_identity_pass(self, tmp_path, capsys):
        lines = [line("a.jpg", CLASSES), line("b.jpg", [])]
        src = write(tmp_path, "l.manifest", lines)
        derived = write(tmp_path, "d.manifest", [x.replace(PREFIX, MONO) for x in lines])
        code, out = run(capsys, "--labeled", src, "--derived", derived, "--expect-lines", "2")
        assert code == 0, out
        assert "derived: 2 lines, 0 failures" in out

    def test_pairwise_identity_fail(self, tmp_path, capsys):
        lines = [line("a.jpg", CLASSES), line("b.jpg", [])]
        src = write(tmp_path, "l.manifest", lines)
        changed = [line("a.jpg", ["front_fender", "rear_fender"], prefix=MONO),
                   lines[1].replace(PREFIX, MONO)]
        derived = write(tmp_path, "d.manifest", changed)
        code, out = run(capsys, "--labeled", src, "--derived", derived, "--expect-lines", "2")
        assert code == 1
        assert "line 1: the parsed annotations differ (Requirement 2.2)" in out

    def test_two_labeled_manifests_concatenated_in_order(self, tmp_path, capsys):
        first = [line("a.jpg", CLASSES)]
        second = [line("b.jpg", [])]
        one = write(tmp_path, "one.manifest", first)
        two = write(tmp_path, "two.manifest", second)
        derived = write(tmp_path, "d.manifest", [x.replace(PREFIX, MONO) for x in first + second])
        code, out = run(capsys, "--labeled", one, "--labeled", two, "--derived", derived,
                        "--expect-lines", "2")
        assert code == 0, out
        code, out = run(capsys, "--labeled", two, "--labeled", one, "--derived", derived,
                        "--expect-lines", "2")
        assert code == 1
        assert "different images" in out


# ---------------------------------------------------------------------------
# 2.2 split replay and cross-set identity
# ---------------------------------------------------------------------------

NAMES = ["img{0:02d}.png".format(i) for i in range(10)]
LEVELS = [0, 0, 40, 70, 100, 130, 160, 190, 220, 250]  # img00 and img01 share a group


def images(tmp_path, name, noise_on=None):
    from PIL import Image
    folder = tmp_path / name
    folder.mkdir()
    for index, (file_name, level) in enumerate(zip(NAMES, LEVELS)):
        if index == noise_on:
            import numpy as np
            pixels = np.random.default_rng(0).integers(0, 255, (H, W, 3), dtype=np.uint8)
            Image.fromarray(pixels).save(str(folder / file_name))
        else:
            Image.new("RGB", (W, H), (level, level, level)).save(str(folder / file_name))
    return str(folder)


class TestSplitReplay:
    def manifest(self, tmp_path):
        return write(tmp_path, "l.manifest", [line(n, CLASSES) for n in NAMES])

    def triage(self, tmp_path, fail_files):
        rows = []
        for n in NAMES:
            if n in fail_files:
                rows.append((n, "incorrect", "front_fender;rear_fender", "", "none", (1, 1, 1)))
            else:
                rows.append((n, "correct", "", "", "none", (1, 1, 1)))
        return triage_csv(tmp_path, rows)

    def test_one_dir_split_with_set_counts(self, tmp_path, capsys):
        src = self.manifest(tmp_path)
        triage = self.triage(tmp_path, {"img04.png", "img03.png"})
        code, out = run(capsys, "--labeled", src, "--triage", triage,
                        "--images-mono", images(tmp_path, "mono"), "--expect-lines", "10")
        assert code == 0, out
        assert "split replay (mono): 9 groups" in out
        assert "train: 6 (Known_Good 5, Fail_Set 1)" in out
        assert "valid: 2 (Known_Good 2, Fail_Set 0)" in out
        assert "test: 2 (Known_Good 1, Fail_Set 1)" in out
        assert "img03.png [Fail_Set]" in out and "img08.png [Known_Good]" in out

    def test_no_fail_set_in_train_stops(self, tmp_path, capsys):
        src = self.manifest(tmp_path)
        triage = self.triage(tmp_path, {"img03.png"})
        code, out = run(capsys, "--labeled", src, "--triage", triage,
                        "--images-mono", images(tmp_path, "mono"), "--expect-lines", "10")
        assert code == 1
        assert "train holds no Fail_Set image -- stop" in out

    def test_cross_set_identity(self, tmp_path, capsys):
        src = self.manifest(tmp_path)
        mono = images(tmp_path, "mono")
        code, out = run(capsys, "--labeled", src, "--images-mono", mono,
                        "--images-color", images(tmp_path, "color"), "--expect-lines", "10")
        assert code == 0, out
        assert "cross-set split identity: identical" in out

    def test_cross_set_difference_on_a_perturbed_dir(self, tmp_path, capsys):
        src = self.manifest(tmp_path)
        mono = images(tmp_path, "mono")
        code, out = run(capsys, "--labeled", src, "--images-mono", mono,
                        "--images-color", images(tmp_path, "noisy", noise_on=1),
                        "--expect-lines", "10")
        assert code == 1
        assert "split differs across the image sets: img01.png" in out

    def test_missing_image_listed(self, tmp_path, capsys):
        src = self.manifest(tmp_path)
        mono = images(tmp_path, "mono")
        os.remove(os.path.join(mono, "img09.png"))
        code, out = run(capsys, "--labeled", src, "--images-mono", mono, "--expect-lines", "10")
        assert code == 1
        assert "img09.png: not found under" in out
