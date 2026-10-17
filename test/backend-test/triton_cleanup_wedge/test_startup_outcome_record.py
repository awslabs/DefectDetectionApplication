# Copyright 2026 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""F3: the Startup records an outcome it cannot confirm
(triton-model-cleanup-wedge D5, tasks 3.1 and 3.5).

* ``model_status_records``: record contents, atomic replace, the write caps
  of design-review finding 9, tolerant reads.
* ``model_convertor.main``: a record per reason class, the WARNING/ERROR
  Startup line, the clear on READY, exit 0 after a successful conversion.
* The pb_stub capture with a fake ``subprocess.Popen``.

Runs under Python 3.8 as well.

_Requirements: 2.7, 3.3; Property 5_
"""
import io
import json
import logging
import os
import signal
import subprocess
import threading

import pytest

import dda_triton.model_convertor as mc
from dda_triton import model_status_records as records

from triton_cleanup_wedge.fakes import (
    STATE_LOADING,
    STATE_READY,
    STATE_UNAVAILABLE,
    FakeBackend,
    FakeTime,
    write_fixture_model,
)

M = "model-startup-jetson-xavier-jp5"


# ----------------------------------------------------------- the record module

def test_a_record_holds_every_field_and_reads_back(tmp_path):
    d = str(tmp_path / "model_status")
    assert records.write_load_outcome(
        M, state="LOADING", reason="still LOADING", failure_reason=None,
        backend_reachable=True, attempts=3, component_version="8.0.0",
        startup_started_at="2026-10-14T14:51:55Z", records_dir=d)
    record = records.read_load_outcomes(d)[M]
    assert record["schema"] == 1
    assert record["model"] == M
    assert record["outcome"] == "not_ready"
    assert record["state"] == "LOADING"
    assert record["componentVersion"] == "8.0.0"
    assert record["attempts"] == 3
    assert record["backendReachable"] is True
    assert record["pbStubMissingFile"] is None
    assert record["source"] == "model_component_startup"
    assert record["recordedAt"].endswith("Z")
    assert oct(os.stat(os.path.join(d, M + ".json")).st_mode & 0o777) == oct(0o644)
    assert oct(os.stat(d).st_mode & 0o777) == oct(0o755)


def test_writes_replace_atomically_and_leave_no_temporary_file(tmp_path, monkeypatch):
    d = str(tmp_path)
    assert records.write_load_outcome(M, state="LOADING", reason="first", records_dir=d)
    inode = os.stat(os.path.join(d, M + ".json")).st_ino
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(records.os, "replace",
                        lambda a, b: (replaced.append((a, b)), real_replace(a, b))[1])
    assert records.write_load_outcome(M, state="UNAVAILABLE", reason="second", records_dir=d)
    assert len(replaced) == 1 and os.path.basename(replaced[0][0]).startswith(".tmp-")
    assert os.stat(os.path.join(d, M + ".json")).st_ino != inode
    assert records.read_load_outcomes(d)[M]["reason"] == "second"
    assert sorted(os.listdir(d)) == [M + ".json"]


def test_a_failed_write_returns_false_and_cleans_up(tmp_path, monkeypatch):
    d = str(tmp_path)

    def boom(*_a):
        raise OSError("disk full")

    monkeypatch.setattr(records.os, "replace", boom)
    assert records.write_load_outcome(M, state="LOADING", reason="x", records_dir=d) is False
    assert os.listdir(d) == []


def test_texts_are_cut_to_2000_characters(tmp_path):
    d = str(tmp_path)
    assert records.write_load_outcome(M, state="UNAVAILABLE", reason="r" * 50000,
                                      failure_reason="f" * 50000, records_dir=d)
    record = records.read_load_outcomes(d)[M]
    assert record["reason"] == "r" * records.LOAD_OUTCOME_TEXT_MAX == "r" * 2000
    assert record["failureReason"] == "f" * 2000


def test_the_largest_record_the_writer_can_produce_passes_the_reader(tmp_path):
    """Both texts at the cap and the longest model name a file name can carry
    (250 characters plus ``.json`` is NAME_MAX), plus a pb_stub capture at its
    300-character cap. Text that JSON escapes is shortened until it fits."""
    d = str(tmp_path)
    longest = "model-" + "x" * 244
    assert len(longest) + len(".json") == 255
    pb_stub = {"line": "p" * 300, "at": "2026-10-14T14:52:21.200123456Z",
               "capturedAt": "2026-10-14T14:58:45Z"}
    for text in ("a" * 50000, "\u0001" * 50000, "\U0001F600" * 50000):
        assert records.write_load_outcome(
            longest, state="UNAVAILABLE", reason=text, failure_reason=text,
            attempts=3, component_version="1" * 64, startup_started_at="2026-10-14T14:51:55Z",
            pb_stub_missing_file=pb_stub, records_dir=d)
        path = os.path.join(d, longest + ".json")
        assert os.path.getsize(path) <= records.LOAD_OUTCOME_MAX_BYTES
        assert longest in records.read_load_outcomes(d)


def test_reads_skip_anything_they_cannot_use(tmp_path):
    d = str(tmp_path)
    assert records.write_load_outcome(M, state="LOADING", reason="ok", records_dir=d)
    with open(os.path.join(d, "model-bad.json"), "w") as handle:
        handle.write("{not json")
    with open(os.path.join(d, "model-schema2.json"), "w") as handle:
        json.dump({"schema": 2, "model": "model-schema2", "outcome": "not_ready"}, handle)
    with open(os.path.join(d, "model-big.json"), "w") as handle:
        json.dump({"schema": 1, "model": "model-big", "outcome": "not_ready",
                   "reason": "x" * 20000}, handle)
    with open(os.path.join(d, "self_heal.json"), "w") as handle:
        json.dump({"schema": 1}, handle)
    with open(os.path.join(d, "model-other.json"), "w") as handle:
        json.dump({"schema": 1, "model": "model-mismatch", "outcome": "not_ready"}, handle)
    assert sorted(records.read_load_outcomes(d)) == [M]
    assert records.read_load_outcomes(str(tmp_path / "absent")) == {}


def test_clear_and_unlisted_recorded_models(tmp_path):
    d = str(tmp_path / "status")
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo, "model-a"))
    for name in ("model-a", "model-b", "model-c"):
        assert records.write_load_outcome(name, state="UNKNOWN", reason="r", records_dir=d)
    os.makedirs(os.path.join(repo, "model-c"))
    unlisted = records.unlisted_recorded_models(["model-c"], repo, records_dir=d)
    assert [m for m, _r in unlisted] == ["model-a"]
    assert records.clear_load_outcome("model-a", records_dir=d)
    assert records.clear_load_outcome("model-a", records_dir=d)  # idempotent
    assert sorted(records.read_load_outcomes(d)) == ["model-b", "model-c"]


def test_constants_and_utc_iso():
    assert records.MODEL_STATUS_DIR == "/aws_dda/dda_triton/model_status"
    assert records.SELF_HEAL_RECORD == "self_heal.json"
    assert records.SELF_HEAL_DISABLED_FILE == "self-heal.disabled"
    assert records.utc_iso(0) == "1970-01-01T00:00:00Z"
    assert records.read_json("/nonexistent/x.json") is None


# ----------------------------------------------------------- the Startup's main

@pytest.fixture
def startup(tmp_path, monkeypatch):
    """A Startup against a fake backend: ``run(backend)`` returns
    ``(exit_code, records)``."""
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    d = str(tmp_path / "dda_triton" / "model_status")
    deployed = write_fixture_model(str(tmp_path / "deployed"))
    FakeTime().install(monkeypatch)
    monkeypatch.setattr(mc, "TRITON_MODEL_DIR", repo)
    monkeypatch.setattr(records, "MODEL_STATUS_DIR", d)
    previous = signal.getsignal(signal.SIGTERM)
    captures = []
    monkeypatch.setattr(mc, "capture_pb_stub_missing_file",
                        lambda m, since: (captures.append((m, since)), None)[1])

    def run(backend, reachable=True):
        monkeypatch.setattr(mc.requests, "get", backend.get)
        monkeypatch.setattr(mc, "wait_for_server", lambda *a, **k: reachable)
        code = mc.main(["--unarchived_model_path", deployed, "--model_version", "8.0.0",
                        "--model_name", M])
        return code, records.read_load_outcomes(d)

    yield run, d, captures
    signal.signal(signal.SIGTERM, previous)


def _levels(caplog, text):
    return [r.levelname for r in caplog.records if text in r.getMessage()]


def test_still_loading_records_and_logs_a_warning(startup, caplog):
    run, d, captures = startup
    caplog.set_level(logging.DEBUG)
    code, recorded = run(FakeBackend(states={M: STATE_LOADING}))
    assert code == 0
    record = recorded[M]
    assert record["state"] == "LOADING"
    assert record["reason"].startswith("still LOADING after 3 start attempts over ")
    assert record["reason"].endswith(" s; the load continues in the backend")
    assert record["componentVersion"] == "8.0.0"
    assert record["backendReachable"] is True
    assert captures and captures[0][0] == M
    line = "StartModel: {0} is not READY at the end of its Startup (state LOADING, 3 attempts".format(M)
    assert _levels(caplog, line) == ["WARNING"]
    assert any(("Recorded the outcome in {0}; the Startup still exits 0 so the deployment is "
                "not rolled back.").format(os.path.join(d, M + ".json")) in r.getMessage()
               for r in caplog.records)


def test_unavailable_records_triton_s_reason_and_logs_an_error(startup, caplog):
    run, _d, _c = startup
    caplog.set_level(logging.DEBUG)
    backend = FakeBackend(states={M: STATE_UNAVAILABLE},
                          failure_reasons={M: "Python model file not found"})
    code, recorded = run(backend)
    assert code == 0
    assert recorded[M]["state"] == "UNAVAILABLE"
    assert recorded[M]["reason"] == "load failed (UNAVAILABLE): Python model file not found"
    assert recorded[M]["failureReason"] == "Python model file not found"
    assert _levels(caplog, "is not READY at the end of its Startup (state UNAVAILABLE") == ["ERROR"]


def test_an_unreachable_backend_records_that_no_load_was_requested(startup, caplog):
    run, _d, _c = startup
    caplog.set_level(logging.DEBUG)
    code, recorded = run(FakeBackend(states={}), reachable=False)
    assert code == 0
    assert recorded[M]["reason"] == "LocalServer backend was not reachable; no load was requested"
    assert recorded[M]["backendReachable"] is False
    assert recorded[M]["attempts"] == 0
    assert _levels(caplog, "is not READY at the end of its Startup") == ["ERROR"]


def test_a_refused_start_records_the_last_answer(startup, caplog):
    run, _d, _c = startup
    caplog.set_level(logging.DEBUG)
    code, recorded = run(FakeBackend(states={M: "UNKNOWN"}, start_status=500))
    assert code == 0
    assert recorded[M]["reason"] == (
        "the load request was not accepted (last state UNKNOWN, last start answer HTTP 500)")
    assert _levels(caplog, "is not READY at the end of its Startup") == ["ERROR"]


def test_ready_clears_an_earlier_record_and_exits_0(startup):
    run, d, captures = startup
    assert records.write_load_outcome(M, state="LOADING", reason="old", records_dir=d)
    code, recorded = run(FakeBackend(states={M: [STATE_LOADING, STATE_READY]}))
    assert code == 0
    assert recorded == {}
    assert captures == []


def test_a_failed_record_write_still_exits_0(startup, monkeypatch, caplog):
    run, _d, _c = startup
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(records, "write_json_atomic", lambda *a: False)
    code, recorded = run(FakeBackend(states={M: STATE_LOADING}))
    assert code == 0 and recorded == {}
    assert _levels(caplog, "The outcome could not be recorded") == ["ERROR"]


def test_a_failed_conversion_still_exits_1(startup, monkeypatch):
    run, _d, _c = startup
    monkeypatch.setattr(mc, "convert_to_triton_structure", lambda **k: False)
    code, recorded = run(FakeBackend(states={M: STATE_LOADING}))
    assert code == 1 and recorded == {}


@pytest.mark.parametrize("name", ["../x", "model/a", ".", ""])
def test_an_invalid_model_name_exits_1_before_any_write(tmp_path, monkeypatch, name):
    previous = signal.getsignal(signal.SIGTERM)
    called = []
    monkeypatch.setattr(mc, "convert_to_triton_structure", lambda **k: called.append(k))
    try:
        code = mc.main(["--unarchived_model_path", str(tmp_path), "--model_version", "1",
                        "--model_name", name])
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert called == []
    assert code == (0 if name == "" else 1)  # a missing name keeps today's exit 0


def test_start_model_keeps_returning_a_bool(monkeypatch):
    FakeTime().install(monkeypatch)
    monkeypatch.setattr(mc, "wait_for_server", lambda *a, **k: True)
    monkeypatch.setattr(mc.requests, "get", FakeBackend(states={M: STATE_READY}).get)
    assert mc.start_model(M) is True
    detailed = mc.start_model_detailed(M)
    assert detailed.ready is True and detailed.state == "READY"


def test_the_observer_sees_every_state(monkeypatch):
    FakeTime().install(monkeypatch)
    monkeypatch.setattr(mc.requests, "get",
                        FakeBackend(states={M: [STATE_LOADING, STATE_LOADING, STATE_READY]}).get)
    seen = []
    assert mc._wait_for_model_ready(M, timeout=30, observer=seen.append) is True
    assert seen == ["LOADING", "LOADING", "READY"]


# ------------------------------------------------------------ pb_stub capture

class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class _BlockingStdout:
    def __init__(self, proc):
        self.proc = proc

    def __iter__(self):
        return self

    def __next__(self):
        self.proc.killed.wait(5)
        raise StopIteration

    def close(self):
        pass


class FakeDocker:
    """``subprocess.run`` for ``docker ps`` and ``subprocess.Popen`` for
    ``docker logs``; ``logs`` maps a container id to its bytes, or to
    ``"block"`` for a log that never ends until killed."""

    def __init__(self, ids, logs):
        self.ids = ids
        self.logs = logs
        self.commands = []
        self.killed_ids = []

    def run(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        return _Completed("".join(i + "\n" for i in self.ids))

    def popen(self, cmd, stdout=None, stderr=None, **kwargs):
        self.commands.append(list(cmd))
        docker = self
        container = cmd[-1]

        class Proc:
            def __init__(self):
                self.killed = threading.Event()
                content = docker.logs.get(container, b"")
                if content == "block":
                    self.stdout = _BlockingStdout(self)
                else:
                    self.stdout = io.BytesIO(content)

            def kill(self):
                docker.killed_ids.append(container)
                self.killed.set()

            def poll(self):
                return 0 if self.killed.is_set() or not isinstance(self.stdout,
                                                                   _BlockingStdout) else None

            def wait(self, timeout=None):
                return 0

        return Proc()

    def install(self, monkeypatch):
        monkeypatch.setattr(mc.subprocess, "run", self.run)
        monkeypatch.setattr(mc.subprocess, "Popen", self.popen)
        return self


LINE = ("2026-10-14T14:52:21.200123456Z E1014 14:52:21 pb_stub.cc:2081] Python model file "
        "not found in '/aws_dda/dda_triton/triton_model_repo/base_{0}/1/model.py'\n".format(M))


def test_capture_finds_the_line_with_since_and_no_until_follow_or_tail(monkeypatch):
    noise = b"2026-10-14T14:52:20Z INFO index\n" * 100
    docker = FakeDocker(["c1"], {"c1": noise + LINE.encode() + noise}).install(monkeypatch)
    found = mc.capture_pb_stub_missing_file(M, 1792000000.7)
    assert found["at"] == "2026-10-14T14:52:21.200123456Z"
    assert found["line"].startswith("E1014 14:52:21 pb_stub.cc:2081] Python model file not found")
    assert len(found["line"]) <= 300 and found["capturedAt"].endswith("Z")
    ps, logs = docker.commands
    assert ps == ["docker", "ps", "-q", "--filter", "ancestor=flask-app"]
    assert logs == ["docker", "logs", "--timestamps", "--since", "1792000000", "c1"]
    for flag in ("--until", "--follow", "-f", "--tail"):
        assert flag not in logs


def test_capture_ignores_another_model_s_line(monkeypatch):
    other = LINE.replace(M, "model-other").encode()
    FakeDocker(["c1"], {"c1": other}).install(monkeypatch)
    assert mc.capture_pb_stub_missing_file(M, 0) is None


def test_capture_scans_newest_first_and_finds_the_older_container(monkeypatch):
    docker = FakeDocker(["new", "old", "older", "oldest"],
                        {"new": b"nothing\n", "old": LINE.encode(), "older": LINE.encode()})
    docker.install(monkeypatch)
    assert mc.capture_pb_stub_missing_file(M, 0) is not None
    assert [c[-1] for c in docker.commands[1:]] == ["new", "old"]


def test_capture_scans_at_most_three_containers(monkeypatch):
    docker = FakeDocker(["a", "b", "c", "d"], {"d": LINE.encode()}).install(monkeypatch)
    assert mc.capture_pb_stub_missing_file(M, 0) is None
    assert [c[-1] for c in docker.commands[1:]] == ["a", "b", "c"]


def test_capture_stops_at_the_byte_cap(monkeypatch):
    monkeypatch.setattr(mc, "PB_STUB_SCAN_BYTES", 1000)
    FakeDocker(["c1"], {"c1": b"x" * 2000 + b"\n" + LINE.encode()}).install(monkeypatch)
    assert mc.capture_pb_stub_missing_file(M, 0) is None


def test_capture_kills_a_log_that_does_not_end(monkeypatch):
    monkeypatch.setattr(mc, "PB_STUB_PER_CONTAINER_S", 0.2)
    docker = FakeDocker(["c1"], {"c1": "block"}).install(monkeypatch)
    assert mc.capture_pb_stub_missing_file(M, 0) is None
    assert docker.killed_ids == ["c1"]


def test_capture_without_docker_is_none(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(mc.subprocess, "run", missing)
    monkeypatch.setattr(mc.subprocess, "Popen", missing)
    assert mc.capture_pb_stub_missing_file(M, 0) is None


def test_capture_within_the_total_budget(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(mc.time, "monotonic", fake.monotonic)
    docker = FakeDocker(["a", "b"], {}).install(monkeypatch)
    real_popen = docker.popen

    def slow_popen(cmd, **kwargs):
        fake.sleep(mc.PB_STUB_TOTAL_S)  # the first container used the whole budget
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(mc.subprocess, "Popen", slow_popen)
    assert mc.capture_pb_stub_missing_file(M, 0) is None
    assert [c[-1] for c in docker.commands[1:]] == ["a"]
