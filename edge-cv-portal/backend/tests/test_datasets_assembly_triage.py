# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `datasets/assembly_triage.py` -- the shared Triage_File
reader of car-assembly-check (task 2.4; design DD18, Interfaces).

The fixture copies the confirmed file's eleven-column header and one row of
each kind it holds (plus the synthetic kinds the rules must handle: a
`correct` row with an occluder, a stray-piece Known_Good row); the smoke
test reads the real file when it is present and prints the set sizes
rather than pinning them.
"""
import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATASETS = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "datasets"))
if _DATASETS not in sys.path:
    sys.path.append(_DATASETS)

import assembly_triage as at  # noqa: E402

REAL_TRIAGE = Path("/home/ubuntu/github/jp5/main/.agents/tasks/car-assembly-check/"
                   "placement-triage.csv")

HEADER = ("filename,set,source_path,verdict,misplaced_pieces,missing_pieces,"
          "occluder,confidence,notes,piece_counts,unlabeled_stray")

ROWS = {
    "good.jpg": "good.jpg,assembly48,/data/good.jpg,correct,,,none,high,"
                "\"all three flush, seated\",front_fender=1;rear_fender=1;roof=1,0",
    "swapped.jpg": "swapped.jpg,assembly48,/data/swapped.jpg,incorrect,"
                   "front_fender;rear_fender,,none,high,fenders SWAPPED,"
                   "front_fender=1;rear_fender=1;roof=1,0",
    "handmiss.jpg": "handmiss.jpg,anomaly,/data/handmiss.jpg,incorrect_and_missing,"
                    "rear_fender,front_fender,hand,medium,hand holds a piece,"
                    "front_fender=0;rear_fender=1;roof=1,1",
    "empty.jpg": "empty.jpg,anomaly,/data/empty.jpg,missing,,"
                 "front_fender;rear_fender;roof,none,high,empty tray,"
                 "front_fender=0;rear_fender=0;roof=0,0",
    "handgood.jpg": "handgood.jpg,extra,/data/handgood.jpg,correct,,,hand,low,"
                    "a hand over the roof,front_fender=1;rear_fender=1;roof=1,0",
    "stray.jpg": "stray.jpg,assembly48,/data/stray.jpg,correct,,,none,high,"
                 "a stray piece at the image edge,front_fender=1;rear_fender=1;roof=1,1",
    "twofront.jpg": "twofront.jpg,assembly48,/data/twofront.jpg,incorrect_and_missing,"
                    "front_fender,rear_fender,none,high,two nose pieces,"
                    "front_fender=2;rear_fender=0;roof=1,0",
}


def write_csv(tmp_path, rows=None, header=HEADER, name="triage.csv"):
    lines = [header] + list(ROWS.values() if rows is None else rows)
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def triage(tmp_path):
    return at.read_triage(write_csv(tmp_path))


def test_pinned_vocabulary():
    assert at.KNOWN_GOOD_VERDICTS == ("correct",)
    assert at.RECOGNISED_VERDICTS == ("correct", "incorrect", "incorrect_and_missing", "missing")
    assert at.NO_OCCLUDER_VALUES == ("", "none")
    assert at.PIECE_CLASSES == ("front_fender", "rear_fender", "roof")
    assert at.PIECE_COUNTS_RE.pattern == r"^front_fender=(\d+);rear_fender=(\d+);roof=(\d+)$"
    assert at.REQUIRED_COLUMNS == tuple(HEADER.split(","))
    assert at.STRAY_ROWS_ARE_KNOWN_GOOD is True


def test_set_membership(triage):
    assert triage.problems == []
    assert triage.known_good == {"good.jpg", "stray.jpg"}
    assert triage.fail == {"swapped.jpg", "handmiss.jpg", "empty.jpg", "twofront.jpg"}
    assert triage.occluded_correct == {"handgood.jpg"}
    assert triage.set_of("good.jpg") == at.SET_KNOWN_GOOD
    assert triage.set_of("handgood.jpg") == at.SET_OCCLUDED_CORRECT
    assert triage.set_of("nope.jpg") is None


def test_stray_row_flips_with_the_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "STRAY_ROWS_ARE_KNOWN_GOOD", False)
    triage = at.read_triage(write_csv(tmp_path))
    assert "stray.jpg" in triage.occluded_correct
    assert "stray.jpg" not in triage.known_good
    assert "good.jpg" in triage.known_good


def test_expected_pieces(triage):
    assert triage.expected_pieces("good.jpg") == set()
    assert triage.expected_pieces("swapped.jpg") == {"front_fender", "rear_fender"}
    assert triage.expected_pieces("handmiss.jpg") == {"rear_fender", "front_fender"}
    assert triage.expected_pieces("empty.jpg") == {"front_fender", "rear_fender", "roof"}
    assert triage.expected_pieces("unknown.jpg") == set()


@pytest.mark.parametrize("name, counts", [
    ("good.jpg", (1, 1, 1)),
    ("swapped.jpg", (1, 1, 1)),
    ("handmiss.jpg", (0, 1, 1)),
    ("empty.jpg", (0, 0, 0)),
    ("handgood.jpg", (1, 1, 1)),
    ("stray.jpg", (1, 1, 1)),
    ("twofront.jpg", (2, 0, 1)),
])
def test_multiplicity_rule_is_the_parsed_piece_counts(triage, name, counts):
    assert triage.multiplicity_rule(name) == dict(zip(at.PIECE_CLASSES, counts))


def test_row_fields(triage):
    row = triage.rows["handmiss.jpg"]
    assert row.misplaced == ("rear_fender",)
    assert row.missing == ("front_fender",)
    assert row.occluder == "hand"
    assert row.unlabeled_stray == 1
    assert row.confidence == "medium"
    assert row.source_path == "/data/handmiss.jpg"
    assert triage.rows["good.jpg"].notes == "all three flush, seated"


def test_none_and_empty_cell_both_mean_no_occluder(tmp_path):
    rows = [ROWS["good.jpg"],
            "blank.jpg,assembly48,/d,correct,,,,high,n,front_fender=1;rear_fender=1;roof=1,0",
            "upper.jpg,assembly48,/d,correct,,, None ,high,n,front_fender=1;rear_fender=1;roof=1,0"]
    triage = at.read_triage(write_csv(tmp_path, rows))
    assert triage.known_good == {"good.jpg", "blank.jpg", "upper.jpg"}


def test_piece_lists_split_on_comma_semicolon_and_whitespace(tmp_path):
    rows = ['a.jpg,x,/d,incorrect,"front_fender, rear_fender",,none,high,n,'
            'front_fender=1;rear_fender=1;roof=1,0',
            "b.jpg,x,/d,missing,,front_fender rear_fender;roof,none,high,n,"
            "front_fender=0;rear_fender=0;roof=0,0"]
    triage = at.read_triage(write_csv(tmp_path, rows))
    assert triage.problems == []
    assert triage.rows["a.jpg"].misplaced == ("front_fender", "rear_fender")
    assert triage.rows["b.jpg"].missing == ("front_fender", "rear_fender", "roof")


def test_unknown_verdict_and_bad_piece_name_listed(tmp_path):
    rows = ["v.jpg,x,/d,mostly_ok,,,none,low,odd verdict,front_fender=1;rear_fender=1;roof=1,0",
            "p.jpg,x,/d,incorrect,wheel,,none,medium,bad name,front_fender=1;rear_fender=1;roof=1,0"]
    triage = at.read_triage(write_csv(tmp_path, rows))
    assert len(triage.problems) == 2
    assert "unrecognised verdict 'mostly_ok'" in triage.problems[0]
    assert "confidence=low" in triage.problems[0] and "odd verdict" in triage.problems[0]
    assert "'wheel' is not a Piece_Class" in triage.problems[1]
    # An unrecognised verdict is never placed in a set by guess.
    assert triage.set_of("v.jpg") is None


@pytest.mark.parametrize("cell", [
    "front_fender=1;roof=1",
    "front_fender=1;rear_fender=x;roof=1",
    "rear_fender=1;front_fender=1;roof=1",
    "front_fender=1; rear_fender=1;roof=1",
])
def test_malformed_piece_counts_listed(tmp_path, cell):
    rows = ["m.jpg,x,/d,correct,,,none,high,n,{0},0".format(cell)]
    triage = at.read_triage(write_csv(tmp_path, rows))
    assert len(triage.problems) == 1
    assert "piece_counts" in triage.problems[0] and "m.jpg" in triage.problems[0]
    assert triage.multiplicity_rule("m.jpg") == {}


@pytest.mark.parametrize("cell", ["x", "-1", "1.5", ""])
def test_bad_unlabeled_stray_listed(tmp_path, cell):
    rows = ["s.jpg,x,/d,correct,,,none,high,n,front_fender=1;rear_fender=1;roof=1,{0}".format(cell)]
    triage = at.read_triage(write_csv(tmp_path, rows))
    assert len(triage.problems) == 1
    assert "unlabeled_stray" in triage.problems[0]


def test_duplicate_filename_listed(tmp_path):
    triage = at.read_triage(write_csv(tmp_path, [ROWS["good.jpg"], ROWS["good.jpg"]]))
    assert len(triage.problems) == 1
    assert "duplicate filename" in triage.problems[0]
    assert len(triage.rows) == 1


def test_join(triage):
    matched, without_row, without_image = triage.join(["good.jpg", "swapped.jpg", "extra.jpg"])
    assert matched == {"good.jpg", "swapped.jpg"}
    assert without_row == {"extra.jpg"}
    assert without_image == set(ROWS) - {"good.jpg", "swapped.jpg"}


def test_nine_column_v1_layout_refused(tmp_path):
    header = ",".join(HEADER.split(",")[:9])
    rows = [",".join(ROWS["good.jpg"].rsplit(",", 2)[:1])]
    triage = at.read_triage(write_csv(tmp_path, rows, header=header))
    assert triage.rows == {}
    assert len(triage.problems) == 1
    assert "piece_counts, unlabeled_stray" in triage.problems[0]


def test_unreadable_file_is_a_problem(tmp_path):
    triage = at.read_triage(str(tmp_path / "absent.csv"))
    assert triage.rows == {} and len(triage.problems) == 1


def test_script_exit_codes(tmp_path, capsys):
    path = write_csv(tmp_path)
    assert at.main(["--triage", path]) == 0
    out = capsys.readouterr().out
    assert "Known_Good: 2" in out and "Fail_Set: 4" in out and "Occluded_Correct: 1" in out

    manifest = tmp_path / "m.manifest"
    manifest.write_text("\n".join(
        '{{"source-ref": "s3://b/p/{0}"}}'.format(name) for name in list(ROWS) + ["extra.jpg"]
    ) + "\n")
    assert at.main(["--triage", path, "--manifest", str(manifest)]) == 1
    assert "image without a triage row: extra.jpg" in capsys.readouterr().out

    bad = write_csv(tmp_path, [ROWS["good.jpg"], ROWS["good.jpg"]], name="dup.csv")
    assert at.main(["--triage", bad]) == 1

    with pytest.raises(SystemExit) as usage:
        at.main([])
    assert usage.value.code == 2


@pytest.mark.skipif(not REAL_TRIAGE.is_file(), reason="the confirmed Triage_File is not on this host")
def test_smoke_real_triage_file():
    triage = at.read_triage(str(REAL_TRIAGE))
    assert len(triage.rows) == 70
    assert triage.problems == []
    assert all(set(row.piece_counts) == set(at.PIECE_CLASSES) for row in triage.rows.values())
    sizes = (len(triage.known_good), len(triage.fail), len(triage.occluded_correct))
    assert sum(sizes) == 70
    print("real Triage_File sets: Known_Good {0} / Fail_Set {1} / Occluded_Correct {2}".format(*sizes))
