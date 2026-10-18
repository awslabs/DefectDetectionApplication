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
"""``dda_triton.load_tracker`` and the single load path (triton-model-cleanup-wedge
D1, task 3.2).

The tracker thread never runs on its own here: ``ensure_tracker_thread`` is
replaced, and passes are driven with ``_tracker_pass``/``_contained_pass``
and a fake monotonic clock. Locks are real ``flock`` files in a temporary
lock directory; the exclusive holder is another process.

_Requirements: 2.2, 3.4, 3.10; Properties 2, 3_
"""
import logging
import os
import threading

import pytest
from fastapi import HTTPException

from dda_triton import load_tracker, model_locks
from dda_triton.load_tracker import ModelReleaseInProgress

from triton_cleanup_wedge.fakes import (
    FakeNativeTriton,
    FakeTritonClient,
    LockHolder,
    import_with_awsiot_stubs,
    install_fake_panorama,
    lock_is_free,
)

M = "model-a"


@pytest.fixture(autouse=True)
def tracker(monkeypatch):
    load_tracker._reset_for_tests()
    started = []
    monkeypatch.setattr(load_tracker, "ensure_tracker_thread", lambda: started.append(1))
    yield started
    load_tracker._reset_for_tests()


@pytest.fixture
def lock_dir(tmp_path):
    return str(tmp_path / "locks")


class Index:
    """A fake ``index_states`` reader."""

    def __init__(self, states=None):
        self.states = dict(states or {})
        self.reads = 0
        self.raises = None

    def __call__(self):
        self.reads += 1
        if self.raises is not None:
            raise self.raises
        return dict(self.states)


class Native:
    def __init__(self):
        self.calls = []
        self.raises = None

    def __call__(self, model_id):
        self.calls.append(model_id)
        if self.raises is not None:
            raise self.raises


def _path(lock_dir, model=M):
    return model_locks.lock_path(model, lock_dir)


def test_the_shared_lock_is_held_until_the_load_is_ready(lock_dir):
    index, native = Index({M: ("LOADING", None)}), Native()
    load_tracker.request_load(M, native, index, lock_dir=lock_dir)
    assert native.calls == [M]
    assert load_tracker.inflight() == [M]
    assert not lock_is_free(_path(lock_dir), exclusive=True)
    assert lock_is_free(_path(lock_dir), exclusive=False)
    load_tracker._tracker_pass(1000.0)
    assert not lock_is_free(_path(lock_dir))
    index.states[M] = ("READY", None)
    load_tracker._tracker_pass(1002.0)
    assert lock_is_free(_path(lock_dir))
    assert load_tracker.inflight() == []
    assert load_tracker.requested_at(M) is not None  # kept after the release


def test_a_native_exception_releases_the_lock_and_propagates(lock_dir):
    native = Native()
    native.raises = RuntimeError("native load failed")
    with pytest.raises(RuntimeError):
        load_tracker.request_load(M, native, Index(), lock_dir=lock_dir)
    assert lock_is_free(_path(lock_dir))
    assert load_tracker.inflight() == []
    assert load_tracker.requested_at(M) is None


def test_an_exclusive_holder_refuses_the_load_without_the_native_call(lock_dir, caplog):
    caplog.set_level(logging.INFO)
    os.makedirs(lock_dir)
    native = Native()
    with LockHolder(_path(lock_dir), exclusive=True):
        with pytest.raises(ModelReleaseInProgress):
            load_tracker.request_load(M, native, Index(), lock_dir=lock_dir)
        with pytest.raises(ModelReleaseInProgress):
            load_tracker.request_load("base_" + M, native, Index(), lock_dir=lock_dir)
    assert native.calls == []
    assert any(r.levelname == "INFO" and r.getMessage() == (
        "Refusing a load of model-a: its repository entries are being removed or replaced "
        "(component shutdown or update in progress)") for r in caplog.records)


def test_an_unusable_lock_still_loads_with_one_warning_per_key(lock_dir, caplog):
    caplog.set_level(logging.INFO)
    os.makedirs(os.path.join(lock_dir, M + ".lock"))  # a directory: open() fails
    os.makedirs(os.path.join(lock_dir, "model-b.lock"))
    native = Native()
    for model in (M, M, "base_" + M, "model-b"):
        load_tracker.request_load(model, native, Index(), lock_dir=lock_dir)
        load_tracker._tracked.clear()  # untracked again, so the lock is tried again
    assert native.calls == [M, M, "base_" + M, "model-b"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert warnings[0].startswith("Model lock for model-a is unavailable (")
    assert warnings[0].endswith("); loading without it")
    assert warnings[1].startswith("Model lock for model-b is unavailable (")


def test_an_invalid_name_raises_value_error_without_a_native_call(lock_dir):
    native = Native()
    with pytest.raises(ValueError):
        load_tracker.request_load("../x", native, Index(), lock_dir=lock_dir)
    assert native.calls == []


def test_sixteen_threads_hold_exactly_one_handle(lock_dir, monkeypatch):
    handles = []
    real_acquire = model_locks.acquire

    def counting(*args, **kwargs):
        handle = real_acquire(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(load_tracker.model_locks, "acquire", counting)
    index, native = Index({M: ("LOADING", None)}), Native()
    barrier = threading.Barrier(16)
    errors = []

    def worker():
        try:
            barrier.wait()
            load_tracker.request_load(M, native, index, lock_dir=lock_dir)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert len(native.calls) == 16
    assert len(handles) == 1 and handles[0].held
    index.states[M] = ("READY", None)
    load_tracker._tracker_pass(1000.0)
    assert not handles[0].held
    assert lock_is_free(_path(lock_dir))


def test_a_request_after_the_terminal_read_keeps_the_handle(lock_dir):
    native = Native()
    index = Index({M: ("UNAVAILABLE", "failed: earlier load")})
    load_tracker.request_load(M, native, index, lock_dir=lock_dir)
    real = index.__call__

    def racing_reader():
        states = real()
        # A new request lands between the read and the release.
        load_tracker.request_load(M, native, index, lock_dir=lock_dir)
        return states

    load_tracker._read_states = racing_reader
    load_tracker._tracker_pass(1000.0)
    assert load_tracker.inflight() == [M]
    assert not lock_is_free(_path(lock_dir))
    # The next pass reads the new load's LOADING and keeps the handle, then
    # READY releases it.
    index.states[M] = ("LOADING", None)
    load_tracker._tracker_pass(1002.0)
    assert load_tracker.inflight() == [M]
    index.states[M] = ("READY", None)
    load_tracker._tracker_pass(1004.0)
    assert load_tracker.inflight() == []
    assert native.calls == [M, M]


def test_unavailable_without_a_reason_is_not_confirmation(lock_dir):
    index = Index({M: ("UNAVAILABLE", None)})
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)
    load_tracker._tracker_pass(1000.0)
    load_tracker._tracker_pass(1119.0)
    assert load_tracker.inflight() == [M]
    index.states[M] = ("UNAVAILABLE", "load failed: model file not found")
    load_tracker._tracker_pass(1121.0)
    assert load_tracker.inflight() == []


@pytest.mark.parametrize("state", ["UNKNOWN", "UNLOADING", "UNAVAILABLE", None])
def test_unconfirmed_states_release_after_120_s_with_the_info_line(lock_dir, caplog, state):
    caplog.set_level(logging.INFO)
    index = Index({} if state is None else {M: (state, None)})
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)
    load_tracker._tracker_pass(1000.0)
    load_tracker._tracker_pass(1100.0)
    assert load_tracker.inflight() == [M]
    load_tracker._tracker_pass(1119.9)
    assert load_tracker.inflight() == [M]
    load_tracker._tracker_pass(1120.0)
    assert load_tracker.inflight() == []
    assert lock_is_free(_path(lock_dir))
    shown = state or "UNKNOWN"
    assert ("Released the model lock of model-a: its load state stayed {0} without a recorded "
            "reason for 120s, so the load is treated as ended").format(shown) in [
                r.getMessage() for r in caplog.records if r.levelname == "INFO"]


def test_a_loading_read_restarts_the_unconfirmed_window(lock_dir):
    index = Index({M: ("UNKNOWN", None)})
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)
    load_tracker._tracker_pass(1000.0)
    index.states[M] = ("LOADING", None)
    load_tracker._tracker_pass(1100.0)
    index.states[M] = ("UNKNOWN", None)
    load_tracker._tracker_pass(1110.0)
    load_tracker._tracker_pass(1200.0)
    assert load_tracker.inflight() == [M]
    load_tracker._tracker_pass(1230.0)
    assert load_tracker.inflight() == []


def test_loading_and_empty_reads_never_release(lock_dir):
    index = Index({M: ("LOADING", None)})
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)
    for now in range(1000, 5000, 500):
        load_tracker._tracker_pass(float(now))
    index.states[M] = ("", None)
    load_tracker._tracker_pass(9000.0)
    assert load_tracker.inflight() == [M]


def test_a_raising_reader_keeps_every_handle(lock_dir):
    index = Index({M: ("READY", None), "model-b": ("READY", None)})
    index.raises = RuntimeError("empty index")
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)
    load_tracker.request_load("model-b", Native(), index, lock_dir=lock_dir)
    load_tracker._tracker_pass(1000.0)
    assert load_tracker.inflight() == [M, "model-b"]
    index.raises = None
    load_tracker._tracker_pass(1002.0)
    assert load_tracker.inflight() == []


def test_one_index_read_per_pass(lock_dir):
    index = Index({"model-{0}".format(i): ("LOADING", None) for i in range(5)})
    for i in range(5):
        load_tracker.request_load("model-{0}".format(i), Native(), index, lock_dir=lock_dir)
    load_tracker._tracker_pass(1000.0)
    assert index.reads == 1
    load_tracker._tracker_pass(1002.0)
    assert index.reads == 2


def test_no_pass_reads_while_nothing_is_tracked():
    index = Index()
    load_tracker._read_states = index
    load_tracker._tracker_pass(1000.0)
    assert index.reads == 0


def test_a_raising_pass_logs_one_error_per_streak_and_keeps_the_handles(lock_dir, monkeypatch,
                                                                         caplog):
    caplog.set_level(logging.DEBUG)
    index = Index({M: ("READY", None)})
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)

    def boom(state, reason):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(load_tracker, "_classify", boom)
    assert load_tracker._contained_pass(1000.0) is False
    assert load_tracker._contained_pass(1002.0) is False
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert errors[0].getMessage() == (
        "Triton model-load tracker pass failed (classifier bug); every model lock is kept and "
        "the next pass runs in 2s")
    assert errors[0].exc_info is not None
    assert load_tracker.inflight() == [M]
    monkeypatch.undo()
    assert load_tracker._contained_pass(1004.0) is True
    assert load_tracker.inflight() == []
    # A new streak logs again.
    load_tracker.request_load(M, Native(), index, lock_dir=lock_dir)
    monkeypatch.setattr(load_tracker, "_classify", boom)
    load_tracker._contained_pass(1006.0)
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 2


def test_a_dead_tracker_thread_is_replaced(monkeypatch):
    monkeypatch.undo()  # the real ensure_tracker_thread

    class Stop(BaseException):
        pass

    release = threading.Event()
    runs = []

    def fake_run():
        runs.append(threading.current_thread().name)
        if len(runs) == 1:
            raise Stop()
        release.wait(10)

    monkeypatch.setattr(load_tracker, "_run", fake_run)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(load_tracker, "_thread", None)
    first = load_tracker.ensure_tracker_thread()
    first.join(5)
    assert not first.is_alive()
    second = load_tracker.ensure_tracker_thread()
    try:
        assert second is not first and second.is_alive()
        assert load_tracker.ensure_tracker_thread() is second
        assert second.name == "triton-load-tracker" and second.daemon
    finally:
        release.set()
        second.join(5)


# -------------------------------------------------------------- the client

def test_the_client_locks_the_lock_dir_of_its_repository(tmp_path, monkeypatch):
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    os.makedirs(repo)
    module, native = install_fake_panorama(monkeypatch, FakeNativeTriton({M: "UNKNOWN"}))
    client = module.TritonEdgeClient(model_dir=repo)
    assert client.model_dir == repo
    assert client.start_triton_model(M) == "LOADING"
    assert native.load_calls == [M]
    path = model_locks.lock_path(M, model_locks.lock_dir_for(repo))
    assert path == os.path.join(str(tmp_path / "dda_triton" / "locks"), M + ".lock")
    assert not lock_is_free(path)
    assert load_tracker._read_states == client.index_states
    native.index[M] = "READY"
    load_tracker._tracker_pass(1000.0)
    assert lock_is_free(path)


def test_the_client_propagates_a_refusal_without_the_error_line(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    module, native = install_fake_panorama(monkeypatch)
    client = module.TritonEdgeClient(model_dir=repo)
    lock_dir = model_locks.lock_dir_for(repo)
    os.makedirs(lock_dir)
    with LockHolder(model_locks.lock_path(M, lock_dir), exclusive=True):
        with pytest.raises(ModelReleaseInProgress):
            client.start_triton_model(M)
    assert native.load_calls == []
    assert not [r for r in caplog.records if r.getMessage() == "Failed to start model"]
    native.load_raises = RuntimeError("native")
    with pytest.raises(RuntimeError):
        client.start_triton_model(M)
    assert [r.levelname for r in caplog.records if r.getMessage() == "Failed to start model"] == [
        "ERROR"]


def test_index_states_parses_the_index(monkeypatch):
    module, native = install_fake_panorama(
        monkeypatch, FakeNativeTriton({M: "READY", "model-b": ("UNAVAILABLE", "boom")}))
    client = module.TritonEdgeClient(model_dir="/nonexistent/repo")
    assert client.index_states() == {M: ("READY", None), "model-b": ("UNAVAILABLE", "boom")}


def test_index_states_is_quiet_on_empty_answers_but_listing_keeps_its_warning(monkeypatch,
                                                                             caplog):
    caplog.set_level(logging.DEBUG)
    module, native = install_fake_panorama(monkeypatch, FakeNativeTriton(list_answers=[""] * 6))
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    client = module.TritonEdgeClient(model_dir="/nonexistent/repo")
    caplog.clear()
    with pytest.raises(RuntimeError):
        client.index_states()
    assert [r for r in caplog.records if r.levelno > logging.DEBUG] == []
    with pytest.raises(RuntimeError):
        client.list_triton_models()
    levels = [r.levelname for r in caplog.records if "empty response" in r.getMessage()]
    assert levels == ["DEBUG"] * 3 + ["WARNING"] * 3


# ------------------------------------------------------ the /start mapping

@pytest.fixture(scope="module")
def feature_utils():
    return import_with_awsiot_stubs("utils.feature_configs_utils")


def test_start_maps_a_refusal_to_409(feature_utils):
    client = FakeTritonClient(["UNKNOWN"], start_raises=ModelReleaseInProgress(M))
    with pytest.raises(HTTPException) as info:
        feature_utils.start_model_triton(client, M)
    assert info.value.status_code == 409
    assert info.value.detail == (
        "Error while attempting to start model model-a. Its repository entries are being "
        "removed or replaced (component shutdown or update in progress). Start it again once "
        "its component is running.")


def test_start_maps_an_invalid_lock_key_to_400(feature_utils):
    client = FakeTritonClient(["UNKNOWN"])
    with pytest.raises(HTTPException) as info:
        feature_utils.start_model_triton(client, "model-a/../../etc")
    assert info.value.status_code == 400
    assert "Invalid model name 'model-a/../../etc'" in info.value.detail
    assert client.start_calls == []


def test_start_keeps_its_403_and_success_contracts(feature_utils):
    with pytest.raises(HTTPException) as info:
        feature_utils.start_model_triton(FakeTritonClient(["LOADING"]), M)
    assert info.value.status_code == 403
    result = feature_utils.start_model_triton(FakeTritonClient(["UNAVAILABLE"]), M)
    assert result.status == "LOADING"


def test_hidden_directories_are_not_models(feature_utils, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".staging-model-a-0123abcd-1-abcdefgh").mkdir(parents=True)
    (repo / ".trash-model-a-0123abcd-1-12").mkdir()
    assert feature_utils.triton_repo_has_models(str(repo)) is False
    (repo / M).mkdir()
    assert feature_utils.triton_repo_has_models(str(repo)) is True
