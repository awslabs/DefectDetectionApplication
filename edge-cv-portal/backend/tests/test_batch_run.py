# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The device batch driver `samples/car-assembly-check/batch_run.py`
(car-assembly-check task 14.2; design §13, DD15, DD18, DD22).

An in-process `http.server` stub implements the seven device routes the
script uses -- `GET /system-health` (a configurable `localServerVersion`)
and six `workflow_engine` routes -- and no `/results/` route, so a
regression to the legacy result endpoint fails loudly. The stub scripts
each run by the name of the file in the batch folder (pending -> running ->
completed, a failed run, a run that never completes, a 409 trigger, an
overlay that 404s once, `instances: null`, `detectionsTruncated: true`) and
"consumes" the folder's file when a run completes, as `folder_source` does.
"""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SAMPLE = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "samples", "car-assembly-check"))
if _SAMPLE not in sys.path:
    sys.path.append(_SAMPLE)

import batch_run  # noqa: E402

VERSION = "1.0.7"
REG = "reg-1"
HEADER = ("filename,set,source_path,verdict,misplaced_pieces,missing_pieces,"
          "occluder,confidence,notes,piece_counts,unlabeled_stray")


def section(passed=True, duplicate=None, ignored=None):
    pieces = {label: {"status": "ok", "iou": 0.9, "detections": []}
              for label in ("front_fender", "rear_fender", "roof")}
    if not passed:
        pieces["roof"] = {"status": "misplaced", "iou": 0.2, "detections": []}
    if duplicate:
        pieces[duplicate] = {"status": "duplicate", "iou": 0.9, "detections": []}
        passed = False
    return {"pass": passed, "pieces": pieces, "flagged": [] if passed else ["d1"],
            "ignored": [{"id": "d9", "class_label": ignored, "confidence": 0.8,
                         "bounding_box": [0, 0, 1, 1], "outline_overlap": 0.0}] if ignored else [],
            "unexpected_classes": [], "frame": {"width": 4608, "height": 3288},
            "min_iou": 0.7, "min_confidence": 0.5, "min_outline_overlap": 0.1}


INSTANCES = [{"class_label": "roof", "confidence": 0.9, "bounding_box": [1, 2, 3, 4],
              "mask_rle": "0 5 7"}]


class Stub(object):
    """The device: scenarios by filename, the folder it consumes."""

    def __init__(self, folder):
        self.folder = folder
        self.versions = [VERSION]
        self.health_status = 200
        self.triggers = 0
        self.requests = []
        self.executions = {}
        self.layouts = {}
        self.overlay_404_once = set()

    def version(self):
        return self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]

    def current_file(self):
        names = sorted(os.listdir(self.folder))
        return names[0] if names else None


def make_handler(stub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            stub.requests.append(("POST", self.path))
            match = re.match(r"^/workflows/registrations/([^/]+)/trigger$", self.path)
            if not match:
                return self.send(404, {"detail": "no such route"})
            name = stub.current_file()
            if name and "409" in name:
                return self.send(409, {"detail": "registration is invalid"})
            stub.triggers += 1
            execution_id = "e{0}".format(stub.triggers)
            stub.executions[execution_id] = {"file": name, "polls": 0}
            return self.send(200, {"executionId": execution_id, "registrationId": match.group(1),
                                   "status": "pending", "captureId": "cap-" + execution_id,
                                   "outputDir": "/aws_dda/captures/x"})

        def do_GET(self):
            stub.requests.append(("GET", self.path))
            path = self.path
            if path == "/system-health":
                if stub.health_status != 200:
                    return self.send(stub.health_status, {"detail": "Not Found"})
                return self.send(200, {"cpuUsagePercent": 1.0, "localServerVersion": stub.version()})
            match = re.match(r"^/workflows/registrations/([^/]+)$", path)
            if match:
                if match.group(1) != REG:
                    return self.send(404, {"detail": "not found"})
                return self.send(200, {"registrationId": REG, "status": "registered", "executions": []})
            match = re.match(r"^/workflows/executions/([^/]+)(/[a-z-]+)?$", path)
            if not match or match.group(1) not in stub.executions:
                return self.send(404, {"detail": "no such route"})
            execution_id, tail = match.group(1), match.group(2)
            run = stub.executions[execution_id]
            name = run["file"] or ""
            if tail is None:
                run["polls"] += 1
                status = ["pending", "running", "completed"][min(run["polls"] - 1, 2)]
                error = None
                if "never" in name:
                    status = "running"
                elif "failed" in name and status == "completed":
                    status, error = "failed", "model_inference failed: boom"
                if status in ("completed", "failed"):
                    path_in = os.path.join(stub.folder, name)
                    if os.path.exists(path_in):
                        os.remove(path_in)
                return self.send(200, {"executionId": execution_id, "registrationId": REG,
                                       "status": status, "startedAt": 1764230000,
                                       "finishedAt": 1764230001 if status != "running" else None,
                                       "error": error, "captureId": "cap-" + execution_id,
                                       "outputDir": "/aws_dda/captures/x"})
            if tail == "/metadata":
                if "failed" in name:
                    return self.send(200, {})
                return self.send(200, {"layout": {"layout": stub.layouts.get(name, section())}})
            if tail == "/node-status":
                return self.send(200, {"gray": {"status": "completed", "durationMs": 412},
                                       "model": {"status": "completed", "durationMs": 96},
                                       "layout": {"status": "completed"}})
            if tail == "/overlay":
                if name in stub.overlay_404_once:
                    stub.overlay_404_once.discard(name)
                    return self.send(404, {"detail": "not found"})
                instances = None if "null" in name else INSTANCES
                return self.send(200, {"maskImage": "aGk=", "maskBackground": {"r": 0},
                                       "instances": instances,
                                       "detectionsTruncated": True if "trunc" in name else
                                       (None if instances is None else False)})
            return self.send(404, {"detail": "no such route"})

    return Handler


@pytest.fixture
def device(tmp_path):
    folder = tmp_path / "batch-input"
    folder.mkdir()
    stub = Stub(str(folder))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub.base_url = "http://127.0.0.1:{0}".format(server.server_address[1])
    yield stub
    server.shutdown()
    server.server_close()
    assert not [r for r in stub.requests if "/results/" in r[1]], "the legacy route was called"


def images(tmp_path, names):
    folder = tmp_path / "images"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"\xff\xd8 jpeg " + name.encode())
    return str(folder)


def run(tmp_path, stub, names, *extra, folder=None):
    out = tmp_path / "gray.json"
    argv = ["--base-url", stub.base_url, "--registration-id", REG, "--workflow-id", "wf-1",
            "--folder", folder or stub.folder, "--images", images(tmp_path, names),
            "--timeout-s", "1.0", "--poll-s", "0.02", "--out", str(out)] + list(extra)
    code = batch_run.main(argv)
    return code, (json.loads(out.read_text()) if out.exists() else None)


def triage_csv(tmp_path, rows, header=HEADER):
    body = [header] + rows
    path = tmp_path / "triage.csv"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def row(name, verdict="correct", misplaced="", missing="", occluder="none", counts="1;1;1", stray=0):
    front, rear, roof = counts.split(";")
    return "{0},assembly48,/d/{0},{1},{2},{3},{4},high,n,front_fender={5};rear_fender={6};roof={7},{8}".format(
        name, verdict, misplaced, missing, occluder, front, rear, roof, stray)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_every_scenario_gives_one_record_in_order(tmp_path, device, capsys):
    names = ["a_ok.jpg", "b_fail.jpg", "c_failed.jpg", "d_never.jpg", "e_409.jpg",
             "f_overlay.jpg", "g_null.jpg", "h_trunc.jpg"]
    device.layouts["b_fail.jpg"] = section(passed=False)
    device.overlay_404_once.add("f_overlay.jpg")
    code, export = run(tmp_path, device, names)
    assert code == 0
    records = {r["file"]: r for r in export["records"]}
    assert [r["file"] for r in export["records"]] == names

    ok = records["a_ok.jpg"]
    assert ok["status"] == "completed" and ok["error"] is None
    assert ok["layout"]["pass"] is True
    assert ok["instances"] == INSTANCES and ok["instances"][0]["mask_rle"] == "0 5 7"
    assert ok["detections_truncated"] is False
    assert ok["timings_ms"] == {"gray": 412, "model": 96, "layout": None, "total": 1000}
    assert ok["execution_id"] == "e1" and ok["capture_id"] == "cap-e1"
    assert set(ok) == {"file", "execution_id", "capture_id", "status", "error", "layout",
                       "timings_ms", "instances", "detections_truncated"}

    assert records["b_fail.jpg"]["layout"]["pass"] is False
    failed = records["c_failed.jpg"]
    assert failed["status"] == "failed" and "boom" in failed["error"] and failed["layout"] is None

    timeout = records["d_never.jpg"]
    assert timeout["status"] == "timeout"
    assert "leftover removed: d_never.jpg" in timeout["error"]

    refused = records["e_409.jpg"]
    assert refused["status"] == "trigger_failed" and "HTTP 409" in refused["error"]
    assert refused["execution_id"] is None

    missing_overlay = records["f_overlay.jpg"]
    assert missing_overlay["status"] == "completed"
    assert missing_overlay["instances"] is None and missing_overlay["detections_truncated"] is None
    assert "GET /workflows/executions/" in missing_overlay["error"] and "/overlay" in missing_overlay["error"]
    assert missing_overlay["layout"]["pass"] is True

    assert records["g_null.jpg"]["instances"] is None and records["g_null.jpg"]["error"] is None
    assert records["h_trunc.jpg"]["detections_truncated"] is True

    assert os.listdir(device.folder) == []
    assert device.triggers == 7
    out = capsys.readouterr().out
    assert "runs: 8, passes: 4, fails: 1, errors: 3" in out
    assert "false-fail" not in out  # no --triage, no lists


def test_header(tmp_path, device):
    code, export = run(tmp_path, device, ["a_ok.jpg"])
    assert code == 0
    assert export["workflow_id"] == "wf-1" and export["registration_id"] == REG
    assert export["local_server_version"] == VERSION
    assert export["local_server_version_end"] == VERSION
    assert export["local_server_changed"] is False
    assert "data_ref_basename" not in json.dumps(export)
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", export["started"])


def test_base_url_default():
    args = batch_run.build_parser().parse_args(
        ["--registration-id", "r", "--workflow-id", "w", "--folder", "/f", "--images", "/i",
         "--out", "o.json"])
    assert args.base_url == "http://localhost:5000"
    assert (args.pattern, args.timeout_s, args.poll_s) == ("*.jpg", 180.0, 0.5)


def test_usage_exit_2():
    with pytest.raises(SystemExit) as usage:
        batch_run.main(["--registration-id", "r"])
    assert usage.value.code == 2


def test_token_only_from_the_environment(tmp_path, device, monkeypatch):
    with pytest.raises(SystemExit) as usage:
        run(tmp_path, device, ["a_ok.jpg"], "--token", "literal-token")
    assert usage.value.code == 2
    monkeypatch.setenv("DDA_TOKEN", "abc")
    code, _ = run(tmp_path, device, ["a_ok.jpg"], "--token", "env:DDA_TOKEN")
    assert code == 0


# ---------------------------------------------------------------------------
# The folder rules
# ---------------------------------------------------------------------------


def test_leftover_file_refused(tmp_path, device, capsys):
    open(os.path.join(device.folder, "old.jpg"), "wb").close()
    code, export = run(tmp_path, device, ["a_ok.jpg"])
    assert code == 1 and export is None
    assert "is not empty: old.jpg" in capsys.readouterr().err
    assert device.triggers == 0


def test_missing_folder_names_the_device_host(tmp_path, device, capsys):
    code, export = run(tmp_path, device, ["a_ok.jpg"], folder=str(tmp_path / "nowhere"))
    assert code == 1 and export is None
    err = capsys.readouterr().err
    assert "does not exist on this host" in err and "device host" in err and "/aws_dda" in err
    assert device.triggers == 0


# ---------------------------------------------------------------------------
# The identity record (DD22)
# ---------------------------------------------------------------------------


def test_expected_version_matches(tmp_path, device):
    code, export = run(tmp_path, device, ["a_ok.jpg"], "--expect-local-server-version", VERSION)
    assert code == 0 and device.triggers == 1
    assert export["local_server_version"] == VERSION


def test_expected_version_differs(tmp_path, device, capsys):
    code, export = run(tmp_path, device, ["a_ok.jpg"], "--expect-local-server-version", "1.0.8")
    assert code == 1 and export is None
    assert device.triggers == 0
    err = capsys.readouterr().err
    assert "'1.0.7'" in err and "'1.0.8'" in err


@pytest.mark.parametrize("not_found", ["literal", "route"])
def test_not_found_is_a_mismatch(tmp_path, device, capsys, not_found):
    if not_found == "literal":
        device.versions = ["NOT_FOUND"]
    else:
        device.health_status = 404
    code, export = run(tmp_path, device, ["a_ok.jpg"], "--expect-local-server-version", VERSION)
    assert code == 1 and export is None and device.triggers == 0
    assert "'1.0.7'" in capsys.readouterr().err


def test_version_changed_mid_batch(tmp_path, device, capsys):
    device.versions = [VERSION, "1.0.8"]
    code, export = run(tmp_path, device, ["a_ok.jpg", "b_ok.jpg"], "--expect-local-server-version", VERSION)
    assert code == 1
    assert export["local_server_version"] == VERSION
    assert export["local_server_version_end"] == "1.0.8"
    assert export["local_server_changed"] is True
    assert "local_server_changed: localServerVersion was '1.0.7'" in capsys.readouterr().out


def test_header_written_without_the_flag(tmp_path, device):
    code, export = run(tmp_path, device, ["a_ok.jpg"])
    assert code == 0 and export["local_server_version"] == VERSION


# ---------------------------------------------------------------------------
# The --triage summary
# ---------------------------------------------------------------------------


def test_triage_summary(tmp_path, device, capsys):
    rows = [row("kg_pass.jpg"), row("kg_fail.jpg"),
            row("fs_pass.jpg", "incorrect", misplaced="front_fender;rear_fender"),
            row("stray.jpg", stray=1), row("kg_ignored.jpg"),
            row("two.jpg", "incorrect_and_missing", misplaced="rear_fender", missing="front_fender",
                counts="0;2;1"),
            row("kg_dup.jpg"),
            row("fs_dup.jpg", "incorrect", misplaced="front_fender;rear_fender"),
            row("hand.jpg", occluder="hand")]
    device.layouts.update({
        "kg_fail.jpg": section(passed=False),
        "stray.jpg": section(ignored="rear_fender"),
        "kg_ignored.jpg": section(ignored="roof"),
        "two.jpg": section(duplicate="rear_fender"),
        "kg_dup.jpg": section(duplicate="roof"),
        "fs_dup.jpg": section(duplicate="front_fender"),
        "hand.jpg": section(passed=False),
    })
    names = ["fs_dup.jpg", "fs_pass.jpg", "hand.jpg", "kg_dup.jpg", "kg_fail.jpg",
             "kg_ignored.jpg", "kg_pass.jpg", "norow.jpg", "stray.jpg", "two.jpg"]
    code, export = run(tmp_path, device, names, "--triage", triage_csv(tmp_path, rows))
    assert code == 0
    out = capsys.readouterr().out
    block = out.split("false-fail (Known_Good, pass = false):")[1]
    false_fail = block.split("false-pass")[0]
    # kg_dup fails through its duplicate; stray.jpg (unlabeled_stray = 1) is Known_Good and passes.
    assert "kg_fail.jpg" in false_fail and "kg_dup.jpg" in false_fail
    assert "stray.jpg" not in false_fail and "kg_pass.jpg" not in false_fail
    assert "fs_pass.jpg (O13 signal)" in out.split("false-pass (Fail_Set, pass = true):")[1].split("Occluded")[0]
    assert "hand.jpg: fail" in out.split("Occluded_Correct (no expectation):")[1].split("duplicate:")[0]
    duplicates = out.split("duplicate:")[1].split("ignored")[0]
    assert "two.jpg [Fail_Set] rear_fender: expected" in duplicates
    assert "kg_dup.jpg [Known_Good] roof: signal" in duplicates
    assert "fs_dup.jpg [Fail_Set] front_fender: signal" in duplicates
    ignored = out.split("ignored (outside the car outline):")[1].split("files without")[0]
    assert "stray.jpg [Known_Good] rear_fender outline_overlap 0.0: expected" in ignored
    assert "kg_ignored.jpg [Known_Good] roof outline_overlap 0.0: signal" in ignored
    assert "norow.jpg" in out.split("files without a triage row:")[1]


def test_nine_column_triage_refused(tmp_path, device, capsys):
    header = ",".join(HEADER.split(",")[:9])
    path = triage_csv(tmp_path, ["a_ok.jpg,assembly48,/d,correct,,,none,high,n"], header=header)
    code, export = run(tmp_path, device, ["a_ok.jpg"], "--triage", path)
    assert code == 1 and export is None and device.triggers == 0
    assert "missing column(s) piece_counts, unlabeled_stray" in capsys.readouterr().err
