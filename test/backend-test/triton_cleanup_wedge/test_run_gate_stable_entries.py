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
"""The gates request a load only for stable entries, and ask again after a
refusal (triton-model-cleanup-wedge D1 and D4, task 3.3).

* ``ensure_model_ready``'s ``UNKNOWN`` branch inspects the entry before its
  first kick, and re-inspects after a ``ModelReleaseInProgress`` refusal.
* ``ModelGate._load`` returns False on that refusal and the model leaves
  ``_load_requested``.

_Requirements: 2.2, 2.3, 3.9; Properties 2, 4_
"""
import logging
import os
import shutil
import tempfile

import pytest

from dda_triton import model_readiness, repo_entries
from dda_triton.load_tracker import ModelReleaseInProgress
from dda_triton.repo_entries import EntryInspection
from workflow_engine import model_gate
from workflow_engine.model_gate import ModelGate

from triton_cleanup_wedge.fakes import (
    STATE_LOADING,
    STATE_READY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    FakeTime,
    FakeTritonClient,
    age_tree,
    set_age,
    write_entry,
)

MODEL = "ppe-detector"
T = "model-gate-jetson-xavier-jp5"


@pytest.fixture(autouse=True)
def repo_has_models(monkeypatch):
    monkeypatch.setattr(model_readiness, "_repo_has_models", lambda: True)


@pytest.fixture
def repo(tmp_path):
    return str(tmp_path / "dda_triton" / "triton_model_repo")


def gate(client, fake, inspector, timeout_s=600.0, sleep=None):
    return model_readiness.ensure_model_ready(
        MODEL, T, client=client, sleep=sleep or fake.sleep, clock=fake.monotonic,
        timeout_s=timeout_s, poll_interval_s=3.0, entry_inspector=inspector)


def real_inspector(repo, fake):
    return lambda target: repo_entries.inspect_entry(repo, target, wall=fake.wall,
                                                     quiet_period_s=10.0)


def fixed(blocker, present=True):
    return lambda target: EntryInspection(present=present, blocker=blocker, missing_path=None,
                                          paths=(), newest_path=None, newest_mtime=None)


def _infos(caplog, text):
    return [r for r in caplog.records if text in r.getMessage()]


def test_an_absent_entry_is_requested_as_today(repo):
    os.makedirs(repo)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN, STATE_UNAVAILABLE])
    outcome = gate(client, fake, real_inspector(repo, fake))
    assert client.start_calls == [T]
    assert outcome.ready is False and outcome.state == STATE_UNAVAILABLE
    assert fake.sleeps == [3.0]


def test_the_default_inspector_reads_triton_model_dir_at_call_time(repo, monkeypatch):
    write_entry(repo, T, age_s=1.0)
    monkeypatch.setattr(model_readiness, "TRITON_MODEL_DIR", repo)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN])
    outcome = model_readiness.ensure_model_ready(MODEL, T, client=client, sleep=fake.sleep,
                                                 clock=fake.monotonic, timeout_s=6.0)
    assert client.start_calls == []
    assert "changed" in outcome.message


def test_a_stable_entry_is_requested_once(repo):
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN, STATE_UNKNOWN, STATE_LOADING, STATE_READY])
    outcome = gate(client, fake, real_inspector(repo, fake))
    assert outcome.ready is True
    assert client.start_calls == [T]


def test_a_changing_entry_is_requested_once_it_settles(repo, caplog):
    caplog.set_level(logging.INFO)
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    set_age(os.path.join(repo, "marshal_" + T, "1", "model.py"), 2.0)
    client = FakeTritonClient([STATE_UNKNOWN])
    seen = []

    def on_start(_model):
        seen.append(fake.now)
        client.push_statuses([STATE_LOADING, STATE_READY])

    client.on_start = on_start
    outcome = gate(client, fake, real_inspector(repo, fake))
    assert outcome.ready is True
    assert client.start_calls == [T]
    assert seen[0] - 1000.0 >= 8.0 - 0.01  # not before the file was 10 s old
    infos = _infos(caplog, "is UNKNOWN but its repository entry is not ready (")
    assert len(infos) == 1  # once per call
    assert infos[0].getMessage().endswith(
        "s ago); waiting before requesting a load (workflow model {0})".format(MODEL))


def test_a_live_staging_sibling_holds_back_the_request(repo):
    write_entry(repo, T, age_s=60.0)
    staging = tempfile.mkdtemp(prefix=repo_entries.staging_prefix("base_" + T), dir=repo)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN])
    outcome = gate(client, fake, real_inspector(repo, fake), timeout_s=30.0)
    assert client.start_calls == []
    assert outcome.message == (
        "Model '{0}' (Triton model '{1}') is not ready for inference: state UNKNOWN; its "
        "repository entry is not ready ({2} is being published). Waited 30s; no load was "
        "requested.").format(MODEL, T, staging)


def test_a_permanently_incomplete_entry_is_never_requested_and_names_the_remedy(repo, caplog):
    caplog.set_level(logging.DEBUG)
    write_entry(repo, T, skip=(os.path.join("base_" + T, "1", "model.py"),))
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN])
    outcome = gate(client, fake, real_inspector(repo, fake))
    missing = os.path.join(repo, "base_" + T, "1", "model.py")
    assert client.start_calls == []
    assert outcome.ready is False and outcome.state == STATE_UNKNOWN
    assert outcome.waited_seconds >= 600.0
    assert outcome.message == (
        "Model '{0}' (Triton model '{1}') is not ready for inference: state UNKNOWN; its "
        "repository entry is not ready ({2} is missing). Waited 600s; no load was requested. "
        "If the entry stays incomplete, restart or redeploy the model component {1} so that "
        "its Startup republishes it.").format(MODEL, T, missing)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("blocker, remedy", [
    ("/r/{0}/config.pbtxt is unreadable".format(T), True),
    ("/r/base_{0}/<version>/model.py is missing".format(T), True),
    ("/r/{0}/config.pbtxt changed 2.0 s ago".format(T), False),
    ("/r/.staging-{0}-x is being published".format(T), False),
])
def test_the_remedy_sentence_only_for_a_missing_or_unreadable_path(blocker, remedy):
    fake = FakeTime()
    outcome = gate(FakeTritonClient([STATE_UNKNOWN]), fake, fixed(blocker), timeout_s=9.0)
    assert ("restart or redeploy the model component" in outcome.message) is remedy
    assert "({0}). Waited 9s; no load was requested.".format(blocker) in outcome.message


def test_a_stale_staging_dir_is_swept_and_then_the_load_is_requested(repo):
    write_entry(repo, T, age_s=60.0)
    boot = "0123abcd"
    stale = tempfile.mkdtemp(prefix=".staging-base_{0}-{1}-{2}-".format(T, boot, 100), dir=repo)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN])
    client.on_start = lambda _m: client.push_statuses([STATE_LOADING, STATE_READY])
    sleeps = []

    def sleep(seconds):
        fake.sleep(seconds)
        sleeps.append(seconds)
        if len(sleeps) == 2:  # the next publish, cleanup or watchdog tick
            removed = repo_entries.sweep_stale_hidden_dirs(
                repo, stamp_reader=lambda: "{0}-{1}".format(boot, 100 + 1200))
            assert removed == [os.path.basename(stale)]

    outcome = gate(client, fake, real_inspector(repo, fake), sleep=sleep)
    assert outcome.ready is True
    assert client.start_calls == [T]
    assert len(sleeps) >= 2


def test_a_refusal_then_a_removed_entry_fails_fast(repo, caplog):
    caplog.set_level(logging.DEBUG)
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN])

    def start_raises(_model):
        if len(client.start_calls) == 1:
            for name in ("base_" + T, "marshal_" + T, T):
                shutil.rmtree(os.path.join(repo, name))  # the cleanup removes the entry
            return ModelReleaseInProgress(T)
        client.push_statuses([STATE_UNAVAILABLE])  # NOT_FOUND
        return None

    client.start_raises = start_raises
    outcome = gate(client, fake, real_inspector(repo, fake))
    assert client.start_calls == [T, T]
    assert outcome.ready is False and outcome.state == STATE_UNAVAILABLE
    assert outcome.waited_seconds < 10.0
    refusals = _infos(caplog, "Load request for Triton model {0} was refused: its repository "
                              "entries are being removed or replaced; checking the entry again "
                              "before the next request (workflow model {1})".format(T, MODEL))
    assert len(refusals) == 1 and refusals[0].levelname == "INFO"
    assert refusals[0].exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_refusal_then_a_replaced_entry_reaches_ready(repo):
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN])

    def start_raises(_model):
        if len(client.start_calls) == 1:
            return ModelReleaseInProgress(T)
        client.push_statuses([STATE_LOADING, STATE_READY])
        return None

    client.start_raises = start_raises
    outcome = gate(client, fake, real_inspector(repo, fake))
    assert outcome.ready is True
    assert client.start_calls == [T, T]


def test_repeated_refusals_log_one_info_line_per_call(repo, caplog):
    caplog.set_level(logging.INFO)
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN], start_raises=lambda m: ModelReleaseInProgress(m))
    outcome = gate(client, fake, real_inspector(repo, fake), timeout_s=30.0)
    assert outcome.ready is False
    assert len(client.start_calls) > 3
    assert len(_infos(caplog, "was refused: its repository entries are being removed")) == 1


def test_a_generic_refusal_keeps_today_s_single_kick(repo, caplog):
    caplog.set_level(logging.INFO)
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN, STATE_LOADING, STATE_READY],
                              start_raises=RuntimeError("403 model is already loading"))
    outcome = gate(client, fake, real_inspector(repo, fake))
    assert outcome.ready is True
    assert client.start_calls == [T]
    warnings = _infos(caplog, "was refused; continuing to wait")
    assert len(warnings) == 1 and warnings[0].exc_info is not None


def test_an_inspector_that_raises_counts_as_absent(repo):
    fake = FakeTime()
    client = FakeTritonClient([STATE_UNKNOWN, STATE_LOADING, STATE_READY])

    def boom(_target):
        raise OSError("unreadable repository")

    assert gate(client, fake, boom).ready is True
    assert client.start_calls == [T]


# ------------------------------------------------------------------ ModelGate

class GateClient:
    def __init__(self, index):
        self.index = dict(index)
        self.loads = []
        self.refuse = 0

    def list_triton_models(self, quiet=False):
        return [{"model_component": k, "status": v} for k, v in self.index.items()]

    def start_triton_model(self, model_id):
        self.loads.append(model_id)
        if self.refuse:
            self.refuse -= 1
            raise ModelReleaseInProgress(model_id)
        self.index[model_id] = STATE_LOADING
        return STATE_LOADING


def _model_gate(repo, client, fake):
    return ModelGate([T], repo=repo, client_provider=lambda: client,
                     repo_has_models=lambda r: True, clock=fake.monotonic, wall=fake.wall,
                     engine_started_at=fake.wall() - 1000.0)


def test_model_gate_refusal_returns_false_and_asks_again_after_the_file_checks(repo, caplog):
    caplog.set_level(logging.INFO)
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = GateClient({T: STATE_UNKNOWN})
    client.refuse = 2
    mg = _model_gate(repo, client, fake)
    wait = mg.check()
    assert wait.state == STATE_UNKNOWN
    assert client.loads == [T]
    assert T not in mg._load_requested
    assert mg._load(T) is False  # still refused
    mg.check()  # the files are checked again, then it asks again
    assert client.loads == [T, T, T]
    assert T in mg._load_requested
    infos = _infos(caplog, "The load request for Triton model {0} was refused: its repository "
                           "entries are being removed or replaced; re-checking its files before "
                           "requesting again".format(T))
    assert len(infos) == 1 and infos[0].levelname == "INFO" and infos[0].exc_info is None
    assert not _infos(caplog, "Could not request a Triton load")


def test_model_gate_reports_not_deployed_once_the_entry_is_removed(repo):
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = GateClient({T: STATE_UNKNOWN})
    client.refuse = 1
    mg = _model_gate(repo, client, fake)
    mg.check()
    for name in ("base_" + T, "marshal_" + T, T):
        shutil.rmtree(os.path.join(repo, name))
    wait = mg.check()
    assert wait.state == model_gate.STATE_NOT_DEPLOYED
    assert client.loads == [T]


def test_model_gate_requests_a_replaced_entry_after_its_quiet_period(repo):
    write_entry(repo, T, age_s=60.0)
    fake = FakeTime()
    client = GateClient({T: STATE_UNKNOWN})
    client.refuse = 1
    mg = _model_gate(repo, client, fake)
    mg.check()
    age_tree(repo, 0.0, now=fake.wall())  # the publish replaced the entry just now
    wait = mg.check()
    assert wait.state == model_gate.STATE_INCOMPLETE and "s ago" in wait.reason
    assert client.loads == [T]
    fake.sleep(11.0)
    mg.check()
    assert client.loads == [T, T]


def test_model_gate_load_returns_true_otherwise():
    class Raising:
        def start_triton_model(self, model_id):
            raise RuntimeError("other")

    fake = FakeTime()
    mg = ModelGate([T], client_provider=lambda: Raising(), clock=fake.monotonic, wall=fake.wall)
    assert mg._load(T) is True
    mg = ModelGate([T], client_provider=lambda: GateClient({}), clock=fake.monotonic,
                   wall=fake.wall)
    assert mg._load(T) is True
