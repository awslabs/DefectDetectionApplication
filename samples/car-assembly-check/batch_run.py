#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Device batch driver for the car-assembly-check workflows (task 14.1;
design §13, DD15, DD18, DD22; Requirements 10.3, 10.9).

Runs ON THE DEVICE HOST, where --folder (e.g. /aws_dda/batch-input) is the
directory the LocalServer container reads through its /aws_dda bind
mount. There is no remote mode: the folder checks are local file-system
operations. Standard library only, Python 3.8+, so the Jetson's system
interpreter runs it unchanged.

    python3 batch_run.py [--base-url http://localhost:5000] [--token env:VAR] \\
        --registration-id <id> --workflow-id <id> --folder /aws_dda/batch-input \\
        --images <dir> [--pattern *.jpg] [--timeout-s 180] [--poll-s 0.5] \\
        [--triage placement-triage.csv] [--expect-local-server-version N.N.N] \\
        --out gray.json

1. GET /system-health -> localServerVersion. With
   --expect-local-server-version a different value, an unreachable route
   or NOT_FOUND exits 1 before any trigger. The value read goes into the
   export header as local_server_version.
2. The registration must exist and be `registered`; --images must be
   readable; --folder must exist on this host, be a writable directory and
   be empty.
3. Per image, in sorted filename order: the folder must be empty; copy the
   image in; POST .../trigger; poll the execution until it has finished
   and the folder is empty (or --timeout-s passes: status "timeout", the
   leftover file removed); then read .../metadata (layout.layout),
   .../node-status (durationMs per node), the execution's
   startedAt/finishedAt (total) and .../overlay (instances with mask_rle,
   detectionsTruncated). A failed read leaves its field null and is named
   in the record's error.
4. GET /system-health again -> local_server_version_end;
   local_server_changed is true when the two reads differ (a revision
   landed mid-batch; the consumers refuse such an export).

The Result_Export: {workflow_id, registration_id, device, started,
local_server_version, local_server_version_end, local_server_changed,
records: [{file, execution_id, capture_id, status, error, layout,
timings_ms, instances, detections_truncated}]}.

The summary line counts runs, passes, fails and errors. With --triage
(the eleven-column placement-triage.csv; the set rules of DD18 and the
piece_counts pattern copied here, nothing else) it adds the false-fail and
false-pass lists, the Occluded_Correct verdicts, the duplicate list
(expected where the row's piece_counts gives the class 2), the ignored list
(expected on a row with unlabeled_stray > 0) and the files without a row.
Verdicts are not re-derived here (layout_reference.py --calibrate --results
and compare_variants.py do that).

--token env:VAR sends the bearer token held in the environment variable
VAR; the token itself is never accepted on the command line.

Exit codes: 0 the export is written, 1 a refusal or local_server_changed,
2 usage.
"""
import argparse
import csv
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:5000"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
RUNNING_STATUSES = ("pending", "running")
LAYOUT_NODE_ID = "layout"

# The Triage_File rules of datasets/assembly_triage.py (DD18 with O19),
# copied so this script stays standard-library only on the device.
TRIAGE_COLUMNS = (
    "filename", "set", "source_path", "verdict", "misplaced_pieces",
    "missing_pieces", "occluder", "confidence", "notes", "piece_counts",
    "unlabeled_stray",
)
PIECE_COUNTS_RE = re.compile(r"^front_fender=(\d+);rear_fender=(\d+);roof=(\d+)$")
PIECE_CLASSES = ("front_fender", "rear_fender", "roof")
NO_OCCLUDER_VALUES = ("", "none")


class Device(object):
    """The LocalServer HTTP API."""

    def __init__(self, base_url, token=None, timeout=30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method, path):
        """``(status, body, error)``: status None when unreachable, body the
        parsed JSON or None."""
        request = urllib.request.Request(self.base_url + path, method=method,
                                         data=b"" if method == "POST" else None)
        request.add_header("Accept", "application/json")
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, None, "{0} {1}: {2}".format(method, path, exc)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        if not 200 <= status < 300:
            text = raw.decode("utf-8", "replace")[:500] if raw else ""
            return status, body, "{0} {1} -> HTTP {2} {3}".format(method, path, status, text).strip()
        return status, body, None

    def local_server_version(self):
        """``(version, error)``."""
        status, body, error = self.call("GET", "/system-health")
        if error:
            return None, error
        version = body.get("localServerVersion") if isinstance(body, dict) else None
        if not isinstance(version, str) or not version:
            return None, "GET /system-health carries no localServerVersion"
        return version, None


def read_token(spec):
    if spec is None:
        return None
    if not spec.startswith("env:") or len(spec) <= 4:
        raise ValueError("--token takes env:VAR (the token is read from the environment)")
    name = spec[4:]
    value = os.environ.get(name)
    if not value:
        raise ValueError("--token env:{0}: the environment variable is not set".format(name))
    return value


def folder_entries(folder):
    return sorted(entry.name for entry in Path(folder).iterdir())


def remove_from(folder):
    removed = []
    for name in folder_entries(folder):
        path = Path(folder) / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    return removed


def total_ms(execution):
    started = execution.get("startedAt") if isinstance(execution, dict) else None
    finished = execution.get("finishedAt") if isinstance(execution, dict) else None
    if not isinstance(started, (int, float)) or not isinstance(finished, (int, float)):
        return None
    elapsed = finished - started
    # The LocalServer stores both as epoch seconds; a millisecond epoch is
    # kept as it is.
    return int(round(elapsed * 1000)) if started < 1e11 else int(round(elapsed))


def run_one(device, args, image, folder):
    record = {"file": image.name, "execution_id": None, "capture_id": None, "status": None,
              "error": None, "layout": None, "timings_ms": None, "instances": None,
              "detections_truncated": None}
    errors = []
    shutil.copy(str(image), str(folder / image.name))
    status, body, error = device.call(
        "POST", "/workflows/registrations/{0}/trigger".format(args.registration_id))
    if error:
        record["status"] = "trigger_failed"
        record["error"] = error
        remove_from(folder)
        return record
    body = body if isinstance(body, dict) else {}
    execution_id = body.get("executionId")
    record["execution_id"] = execution_id
    record["capture_id"] = body.get("captureId")
    deadline = time.monotonic() + args.timeout_s
    execution = body
    while True:
        status, polled, error = device.call("GET", "/workflows/executions/{0}".format(execution_id))
        if not error and isinstance(polled, dict):
            execution = polled
        finished = execution.get("status") not in RUNNING_STATUSES
        if finished and not folder_entries(folder):
            break
        if time.monotonic() >= deadline:
            leftover = remove_from(folder)
            record["status"] = "timeout"
            record["error"] = "no result within {0} s{1}".format(
                args.timeout_s, "; leftover removed: " + ", ".join(leftover) if leftover else "")
            record["capture_id"] = execution.get("captureId") or record["capture_id"]
            return record
        time.sleep(args.poll_s)
    record["status"] = execution.get("status")
    record["capture_id"] = execution.get("captureId") or record["capture_id"]
    if execution.get("error"):
        errors.append(str(execution.get("error")))

    base = "/workflows/executions/{0}".format(execution_id)
    _, metadata, error = device.call("GET", base + "/metadata")
    if error:
        errors.append(error)
    elif isinstance(metadata, dict):
        sections = metadata.get("layout")
        if isinstance(sections, dict) and isinstance(sections.get(LAYOUT_NODE_ID), dict):
            record["layout"] = sections[LAYOUT_NODE_ID]
    _, node_status, error = device.call("GET", base + "/node-status")
    timings = {}
    if error:
        errors.append(error)
    elif isinstance(node_status, dict):
        for node_id, entry in node_status.items():
            value = entry.get("durationMs") if isinstance(entry, dict) else None
            timings[node_id] = value if isinstance(value, (int, float)) else None
    timings["total"] = total_ms(execution)
    record["timings_ms"] = timings
    _, overlay, error = device.call("GET", base + "/overlay")
    if error:
        errors.append(error)
    elif isinstance(overlay, dict):
        record["instances"] = overlay.get("instances")
        record["detections_truncated"] = overlay.get("detectionsTruncated")
    record["error"] = "; ".join(errors) if errors else None
    return record


# ---------------------------------------------------------------------------
# --triage summary (the inline copy of DD18's rules)
# ---------------------------------------------------------------------------


def read_triage(path):
    """``(rows, problem)``; rows by filename with ``set_name``,
    ``piece_counts`` and ``unlabeled_stray``."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in TRIAGE_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            return None, "{0}: missing column(s) {1}; the eleven columns are required".format(
                path, ", ".join(missing))
        rows = {}
        for raw in reader:
            values = {c: (raw.get(c) or "").strip() for c in TRIAGE_COLUMNS}
            match = PIECE_COUNTS_RE.match(values["piece_counts"])
            counts = dict(zip(PIECE_CLASSES, (int(v) for v in match.groups()))) if match else {}
            stray = int(values["unlabeled_stray"]) if values["unlabeled_stray"].isdigit() else 0
            if values["misplaced_pieces"] or values["missing_pieces"]:
                set_name = "Fail_Set"
            elif values["verdict"] == "correct" and values["occluder"].lower() in NO_OCCLUDER_VALUES:
                set_name = "Known_Good"
            else:
                set_name = "Occluded_Correct"
            rows[values["filename"]] = {"set_name": set_name, "piece_counts": counts,
                                        "unlabeled_stray": stray}
        return rows, None


def summary(records, rows):
    lines = []
    passes = sum(1 for r in records if (r.get("layout") or {}).get("pass") is True)
    fails = sum(1 for r in records if (r.get("layout") or {}).get("pass") is False)
    errors = sum(1 for r in records if r.get("status") != "completed" or r.get("layout") is None)
    lines.append("runs: {0}, passes: {1}, fails: {2}, errors: {3}".format(
        len(records), passes, fails, errors))
    if rows is None:
        return lines
    false_fail, false_pass, occluded, duplicate, ignored, no_row = [], [], [], [], [], []
    for record in records:
        name = record["file"]
        row = rows.get(name)
        if row is None:
            no_row.append(name)
            continue
        section = record.get("layout") or {}
        verdict = section.get("pass")
        if row["set_name"] == "Known_Good" and verdict is False:
            false_fail.append(name)
        elif row["set_name"] == "Fail_Set" and verdict is True:
            false_pass.append(name + " (O13 signal)")
        elif row["set_name"] == "Occluded_Correct":
            occluded.append("{0}: {1}".format(name, {True: "pass", False: "fail"}.get(verdict, "no verdict")))
        for label, piece in sorted((section.get("pieces") or {}).items()):
            if isinstance(piece, dict) and piece.get("status") == "duplicate":
                kind = "expected" if row["piece_counts"].get(label) == 2 else "signal"
                duplicate.append("{0} [{1}] {2}: {3}".format(name, row["set_name"], label, kind))
        for entry in section.get("ignored") or []:
            if not isinstance(entry, dict):
                continue
            kind = "expected" if row["unlabeled_stray"] > 0 else "signal"
            ignored.append("{0} [{1}] {2} outline_overlap {3}: {4}".format(
                name, row["set_name"], entry.get("class_label"), entry.get("outline_overlap"), kind))
    for title, items in (("false-fail (Known_Good, pass = false)", false_fail),
                         ("false-pass (Fail_Set, pass = true)", false_pass),
                         ("Occluded_Correct (no expectation)", occluded),
                         ("duplicate", duplicate), ("ignored (outside the car outline)", ignored),
                         ("files without a triage row", no_row)):
        lines.append("{0}: {1}".format(title, len(items)))
        lines.extend("  " + item for item in items)
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _version(value):
    if not VERSION_RE.match(value):
        raise argparse.ArgumentTypeError("expected N.N.N, got {0!r}".format(value))
    return value


def build_parser():
    ap = argparse.ArgumentParser(
        description="Run a folder_source workflow once per image on this device host "
                    "and write the Result_Export.")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--token", help="env:VAR -- the bearer token is read from $VAR")
    ap.add_argument("--registration-id", required=True)
    ap.add_argument("--workflow-id", required=True, help="stamps the export only")
    ap.add_argument("--folder", required=True,
                    help="the folder_source location on this host (e.g. /aws_dda/batch-input)")
    ap.add_argument("--images", required=True, help="directory of the images to run")
    ap.add_argument("--pattern", default="*.jpg")
    ap.add_argument("--timeout-s", type=float, default=180.0)
    ap.add_argument("--poll-s", type=float, default=0.5)
    ap.add_argument("--triage", help="placement-triage.csv (eleven columns) for the summary")
    ap.add_argument("--expect-local-server-version", type=_version,
                    help="refuse to run unless GET /system-health reports this N.N.N")
    ap.add_argument("--out", required=True, help="the Result_Export JSON to write")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        token = read_token(args.token)
    except ValueError as exc:
        ap.error(str(exc))
    device = Device(args.base_url, token)

    version, error = device.local_server_version()
    if args.expect_local_server_version is not None and version != args.expect_local_server_version:
        print("error: GET /system-health localServerVersion is {0!r}{1}; "
              "--expect-local-server-version is {2!r}. No run was triggered.".format(
                  version, " ({0})".format(error) if error else "",
                  args.expect_local_server_version), file=sys.stderr)
        return 1
    if args.expect_local_server_version is None and error:
        print("warning: {0}".format(error), file=sys.stderr)

    rows = None
    if args.triage:
        try:
            rows, problem = read_triage(args.triage)
        except OSError as exc:
            rows, problem = None, "{0}: {1}".format(args.triage, exc)
        if problem:
            print("error: --triage refused: {0}".format(problem), file=sys.stderr)
            return 1

    _, registration, error = device.call(
        "GET", "/workflows/registrations/{0}".format(args.registration_id))
    if error or not isinstance(registration, dict):
        print("error: registration {0}: {1}".format(args.registration_id, error), file=sys.stderr)
        return 1
    if registration.get("status") != "registered":
        print("error: registration {0} is {1!r}, not 'registered'".format(
            args.registration_id, registration.get("status")), file=sys.stderr)
        return 1

    images_dir = Path(args.images)
    if not images_dir.is_dir() or not os.access(str(images_dir), os.R_OK):
        print("error: --images {0} is not a readable directory".format(images_dir), file=sys.stderr)
        return 1
    images = sorted((p for p in images_dir.glob(args.pattern) if p.is_file()), key=lambda p: p.name)
    if not images:
        print("error: no {0} files under {1}".format(args.pattern, images_dir), file=sys.stderr)
        return 1

    folder = Path(args.folder)
    if not folder.exists():
        print("error: --folder {0} does not exist on this host. Run this script on the device "
              "host, where /aws_dda is the folder the LocalServer container reads.".format(folder),
              file=sys.stderr)
        return 1
    if not folder.is_dir() or not os.access(str(folder), os.W_OK):
        print("error: --folder {0} is not a writable directory".format(folder), file=sys.stderr)
        return 1
    leftover = folder_entries(folder)
    if leftover:
        print("error: --folder {0} is not empty: {1}".format(folder, ", ".join(leftover)),
              file=sys.stderr)
        return 1

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    records = []
    stopped = None
    for image in images:
        leftover = folder_entries(folder)
        if leftover:
            stopped = "--folder {0} is not empty before {1}: {2}".format(
                folder, image.name, ", ".join(leftover))
            break
        record = run_one(device, args, image, folder)
        records.append(record)
        print("{0}: {1} pass={2}".format(record["file"], record["status"],
                                         (record.get("layout") or {}).get("pass")))

    version_end, error = device.local_server_version()
    if error:
        print("warning: {0}".format(error), file=sys.stderr)
    changed = version != version_end
    export = {"workflow_id": args.workflow_id, "registration_id": args.registration_id,
              "device": socket.gethostname(), "started": started,
              "local_server_version": version, "local_server_version_end": version_end,
              "local_server_changed": changed, "records": records}
    Path(args.out).write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")

    for line in summary(records, rows):
        print(line)
    if changed:
        print("local_server_changed: localServerVersion was {0!r} before the first run and {1!r} "
              "after the last; repeat the batch once the device is back on one version".format(
                  version, version_end))
    print("wrote {0}".format(args.out))
    if stopped:
        print("error: stopped: " + stopped, file=sys.stderr)
        return 1
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
