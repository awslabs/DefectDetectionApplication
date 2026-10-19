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
"""The Shutdown cleanup waits, unloads, renames, then deletes
(triton-model-cleanup-wedge D2 and design-review finding 1, task 3.4).

``release_model`` runs against a temporary repository, its sibling lock
directory and a fake backend; time is a fake clock that only ``sleep``
advances. A tracked load is a real shared ``flock`` held by another
process. The optional-import cases load the script from a copy of the
package in which one helper is half-written (a ``SyntaxError``), as a
non-atomic re-seed can leave it.

Runs under Python 3.8 as well.

_Requirements: 2.1, 2.2, 3.1, 3.14; Property 1_
"""
import importlib
import logging
import os
import re
import shutil
import sys

import pytest

import dda_triton.convert_model_cleanup as cleanup
from dda_triton import model_locks, model_status_records, repo_entries

from triton_cleanup_wedge.fakes import (
    BACKEND_SRC,
    STATE_LOADING,
    STATE_READY,
    STATE_UNKNOWN,
    FakeBackend,
    FakeTime,
    LockHolder,
    hidden_entries,
    lock_is_free,
    published_dirs,
    write_entry,
)

M = "model-cleanup-jetson-xavier-jp5"


class Env:
    def __init__(self, tmp_path):
        self.repo = str(tmp_path / "dda_triton" / "triton_model_repo")
        self.records = str(tmp_path / "dda_triton" / "model_status")
        self.lock_dir = model_locks.lock_dir_for(self.repo)
        self.lock_path = model_locks.lock_path(M, self.lock_dir)
        write_entry(self.repo, M)
        self.fake = FakeTime()
        self.backend = FakeBackend(states={M: STATE_UNKNOWN})
        self.reachable = True
        self.on_sleep = None

    def sleep(self, seconds):
        self.fake.sleep(seconds)
        if self.on_sleep is not None:
            self.on_sleep(self.fake.now)

    def run(self, module=cleanup, **kwargs):
        options = dict(repo=self.repo, http=self.backend, clock=self.fake.monotonic,
                       sleep=self.sleep, server_waiter=lambda *a: self.reachable,
                       records_dir=self.records)
        options.update(kwargs)
        return module.release_model(M, **options)

    def entries_left(self):
        return [p for p in published_dirs(self.repo, M) if os.path.isdir(p)]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _messages(caplog):
    return ["{0}:{1}".format(r.levelname, r.getMessage()) for r in caplog.records]


# ------------------------------------------------------------ decision table

def test_unknown_is_removed_with_the_existing_lines(env, caplog):
    caplog.set_level(logging.DEBUG)
    model_status_records.write_load_outcome(M, state="LOADING", reason="r",
                                            records_dir=env.records)
    assert env.run() == "removed"
    assert env.entries_left() == []
    assert hidden_entries(env.repo) == []
    lines = _messages(caplog)
    assert "INFO:Release {0}: backend reachable, state UNKNOWN".format(M) in lines
    for path in published_dirs(env.repo, M):
        assert "INFO:Cleaned directory: {0}".format(path) in lines
    assert lines[-1] == "INFO:Directory cleanup finished"
    assert env.backend.urls("/stop") == []
    assert model_status_records.read_load_outcomes(env.records) == {}
    assert lock_is_free(env.lock_path)
    assert all(call["timeout"] == 10 for call in env.backend.calls)


def test_a_tracked_load_is_waited_for_then_unloaded_and_removed(env, caplog):
    caplog.set_level(logging.DEBUG)
    os.makedirs(env.lock_dir)
    holder = LockHolder(env.lock_path, exclusive=False)
    env.backend.states[M] = STATE_LOADING
    env.backend.on_stop[M] = STATE_UNKNOWN

    def on_sleep(now):
        if now - 1000.0 >= 16.0 and holder.proc.poll() is None:
            env.backend.states[M] = STATE_READY
            holder.release()  # the tracker saw READY

    env.on_sleep = on_sleep
    try:
        assert env.run() == "removed"
    finally:
        holder.kill()
    assert env.entries_left() == []
    assert env.backend.urls("/stop") == [
        "http://localhost:5000/feature-configurations/models/{0}/stop".format(M)]
    lines = _messages(caplog)
    assert ("INFO:Release {0}: a load is in flight (state LOADING); waiting up to 300s for it "
            "to finish before removing its repository entries").format(M) in lines
    assert ("INFO:Release {0}: load finished (READY); unloading before removing its repository "
            "entries").format(M) in lines
    assert "INFO:Model stopped successfully!" in lines
    assert "INFO:Release {0}: unloaded (state UNKNOWN)".format(M) in lines


def test_a_load_still_in_flight_at_the_bound_is_skipped(env, caplog):
    caplog.set_level(logging.DEBUG)
    os.makedirs(env.lock_dir)
    env.backend.states[M] = STATE_LOADING
    with LockHolder(env.lock_path, exclusive=False):
        assert env.run() == "skipped"
        assert lock_is_free(env.lock_path, exclusive=False)
    assert env.entries_left() == published_dirs(env.repo, M)
    assert env.fake.now - 1000.0 == pytest.approx(300.0)
    lines = _messages(caplog)
    assert ("WARNING:Release {0}: skipped deleting the repository entries: a load of the model "
            "is still in flight after 300s (state LOADING). The entries are left in "
            "place.").format(M) in lines
    progress = [l for l in lines if "still waiting for the in-flight load" in l]
    assert progress[0] == "INFO:Release {0}: still waiting for the in-flight load (30s of 300s)".format(M)
    assert len(progress) == 9
    assert "INFO:Directory cleanup finished" not in lines


def test_unreachable_with_a_free_lock_is_removed(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.reachable = False
    env.backend.raise_on = lambda url: ConnectionError("refused")
    assert env.run() == "removed"
    assert env.entries_left() == []
    assert ("INFO:Release {0}: backend not reachable; relying on the model "
            "lock").format(M) in _messages(caplog)


def test_unreachable_with_an_unusable_lock_is_skipped(env, caplog):
    caplog.set_level(logging.DEBUG)
    os.makedirs(env.lock_path)  # a directory where the lock file belongs
    env.reachable = False
    env.backend.raise_on = lambda url: ConnectionError("refused")
    assert env.run() == "skipped"
    assert env.entries_left() == published_dirs(env.repo, M)
    lines = _messages(caplog)
    assert any(l.startswith("WARNING:Release {0}: the model lock is unavailable (".format(M))
               and l.endswith("); falling back to the backend's reported state") for l in lines)
    assert ("WARNING:Release {0}: skipped deleting the repository entries: the model lock is "
            "unavailable and the backend is not reachable, so no in-flight load can be ruled "
            "out").format(M) in lines


def test_reachable_with_an_unusable_lock_follows_the_backend_state(env):
    os.makedirs(env.lock_path)
    env.backend.states[M] = [STATE_LOADING, STATE_LOADING, STATE_READY]
    assert env.run() == "removed"
    assert env.entries_left() == []
    assert len(env.backend.urls("/stop")) == 1


def test_an_untracked_load_under_the_exclusive_lock_loops_then_skips(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.backend.states[M] = STATE_LOADING
    acquisitions = []
    real_acquire = model_locks.acquire

    def counting(*args, **kwargs):
        acquisitions.append(kwargs.get("timeout_s"))
        return real_acquire(*args, **kwargs)

    cleanup_locks = cleanup.model_locks
    original = cleanup_locks.acquire
    cleanup_locks.acquire = counting
    try:
        assert env.run() == "skipped"
    finally:
        cleanup_locks.acquire = original
    assert env.entries_left() == published_dirs(env.repo, M)
    assert len(acquisitions) > 10  # re-checked under the lock every 3 s
    assert lock_is_free(env.lock_path)
    assert env.fake.now - 1000.0 <= 300.0 + 3.0


def test_the_fresh_read_under_the_lock_happens_even_when_the_ladder_failed(env):
    env.reachable = False
    env.backend.states[M] = [STATE_READY, STATE_UNKNOWN]
    assert env.run() == "removed"
    assert len(env.backend.urls("/stop")) == 1
    assert env.backend.urls("/feature-configurations")  # read although "unreachable"


def test_a_refused_stop_and_a_slow_unload_still_remove(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.backend.states[M] = STATE_READY
    env.backend.stop_status = 500
    assert env.run() == "removed"
    assert env.entries_left() == []
    lines = _messages(caplog)
    assert any(l.startswith("WARNING:StopModel: Request failed with status code: 500")
               for l in lines)
    assert ("WARNING:Release {0}: the unload did not finish within 60s (state READY); removing "
            "the entries anyway, no load is in flight").format(M) in lines


def test_trash_names_are_stamped_and_renamed_under_the_lock(env, monkeypatch):
    renames = []
    real_rename = os.rename

    def rename(src, dst):
        renames.append((src, dst, lock_is_free(env.lock_path)))
        return real_rename(src, dst)

    monkeypatch.setattr(cleanup.os, "rename", rename)
    assert env.run() == "removed"
    assert [os.path.basename(s) for s, _d, _f in renames] == [
        "base_" + M, "marshal_" + M, M]
    for src, dst, free in renames:
        assert free is False  # the exclusive lock was held
        match = repo_entries._STAMPED_TRASH.match(os.path.basename(dst))
        assert match and match.group("dir") == os.path.basename(src)
        assert match.group("pid") == str(os.getpid())


def test_a_rename_failure_deletes_in_place_while_locked(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def rename(src, dst):
        raise OSError("cross-device")

    monkeypatch.setattr(cleanup.os, "rename", rename)
    assert env.run() == "removed"
    assert env.entries_left() == []
    assert any(l.startswith("WARNING:Release {0}: could not rename".format(M))
               for l in _messages(caplog))


def test_enoent_on_the_trash_delete_counts_as_done(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    real_rmtree = shutil.rmtree

    def rmtree(path, *a, **k):
        if os.path.basename(path).startswith(".trash-"):
            real_rmtree(path)
            raise FileNotFoundError(path)  # a concurrent sweep got there first
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(cleanup.shutil, "rmtree", rmtree)
    assert env.run() == "removed"
    for path in published_dirs(env.repo, M):
        assert "INFO:Cleaned directory: {0}".format(path) in _messages(caplog)


def test_a_failed_trash_delete_is_left_for_the_next_sweep(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def rmtree(path, *a, **k):
        raise OSError("busy")

    monkeypatch.setattr(cleanup.shutil, "rmtree", rmtree)
    assert env.run() == "removed"
    assert env.entries_left() == []
    assert len([n for n in hidden_entries(env.repo) if n.startswith(".trash-")]) == 3
    assert len([l for l in _messages(caplog) if l.startswith("ERROR:Release")]) == 3


def test_the_sweep_removes_stale_trash_and_staging_and_logs_them(env, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    trash = os.path.join(env.repo, ".trash-model-old-ffff0000-1-12")
    staging = os.path.join(env.repo, ".staging-model-old-ffff0000-1-abcdefgh")
    os.makedirs(trash)
    os.makedirs(staging)
    monkeypatch.setattr(repo_entries, "boot_stamp", lambda *a, **k: "0123abcd-5000")
    assert env.run() == "removed"
    lines = _messages(caplog)
    assert "INFO:Release {0}: removed stale trash .trash-model-old-ffff0000-1-12".format(M) in lines
    assert "INFO:Removed stale staging directory .staging-model-old-ffff0000-1-abcdefgh" in lines
    assert hidden_entries(env.repo) == []


# ---------------------------------------------------------------- main and exits

def test_main_exits_1_only_for_argument_errors(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    called = []
    monkeypatch.setattr(cleanup, "release_model", lambda m: called.append(m))
    assert cleanup.main([]) == 1
    assert cleanup.main(["--model_name", "../etc"]) == 1
    assert called == []
    assert cleanup.main(["--model_name", M]) == 0
    assert called == [M]


def test_an_unexpected_error_logs_the_traceback_leaves_the_entries_and_exits_0(env, monkeypatch,
                                                                               caplog):
    caplog.set_level(logging.DEBUG)
    states = []

    def boom(m, repo, warned):
        states.append(lock_is_free(env.lock_path))
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cleanup, "_rename_to_trash", boom)
    real_release = cleanup.release_model
    monkeypatch.setattr(cleanup, "release_model",
                        lambda m: real_release(m, repo=env.repo, http=env.backend,
                                               clock=env.fake.monotonic, sleep=env.sleep,
                                               server_waiter=lambda *a: True,
                                               records_dir=env.records))
    assert cleanup.main(["--model_name", M]) == 0
    assert states == [False]
    assert lock_is_free(env.lock_path)
    assert env.entries_left() == published_dirs(env.repo, M)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors[0].getMessage() == (
        "Release {0}: unexpected error (unexpected); the repository entries are left as they "
        "are and the Shutdown exits 0").format(M)
    assert errors[0].exc_info is not None


def test_the_script_keeps_its_cli_and_log_format():
    assert cleanup.parser.parse_args(["--model_name", M]).model_name == M
    with open(os.path.join(BACKEND_SRC, "dda_triton", "convert_model_cleanup.py")) as handle:
        text = handle.read()
    assert 'format="%(levelname)s:%(message)s"' in text
    assert "sys.exit(main())" in text


# ------------------------------------------------ half-written optional helpers

_PACKAGE_FILES = ("__init__.py", "constants.py", "model_autostart_utils.py",
                  "convert_model_cleanup.py", "model_locks.py", "repo_entries.py",
                  "model_status_records.py")


@pytest.fixture
def isolated_cleanup(tmp_path):
    """Load the cleanup from a copy of ``dda_triton`` in which ``broken`` is
    half-written, then restore the real package."""
    loaded = []

    def load(broken):
        root = tmp_path / "isolated"
        pkg = root / "dda_triton"
        pkg.mkdir(parents=True)
        for name in _PACKAGE_FILES:
            shutil.copy(os.path.join(BACKEND_SRC, "dda_triton", name), str(pkg / name))
        (pkg / (broken + ".py")).write_text("def half_written(:\n")
        saved = {k: v for k, v in sys.modules.items()
                 if k == "dda_triton" or k.startswith("dda_triton.")}
        for name in saved:
            del sys.modules[name]
        sys.path.insert(0, str(root))
        try:
            module = importlib.import_module("dda_triton.convert_model_cleanup")
        finally:
            sys.path.remove(str(root))
            for name in [k for k in sys.modules
                         if k == "dda_triton" or k.startswith("dda_triton.")]:
                del sys.modules[name]
            sys.modules.update(saved)
        loaded.append(module)
        return module

    return load


def test_a_half_written_model_locks_takes_the_http_fallback(env, isolated_cleanup, caplog):
    caplog.set_level(logging.DEBUG)
    module = isolated_cleanup("model_locks")
    assert module.model_locks is None
    assert isinstance(module._IMPORT_ERRORS["model_locks"], SyntaxError)
    assert env.run(module) == "removed"
    assert env.entries_left() == []
    warnings = [l for l in _messages(caplog) if l.startswith("WARNING:Could not import")]
    assert len(warnings) == 1
    assert warnings[0].startswith("WARNING:Could not import dda_triton.model_locks (")
    assert warnings[0].endswith("); falling back to the backend's reported state")


def test_a_half_written_model_locks_with_a_loading_model_skips(env, isolated_cleanup):
    module = isolated_cleanup("model_locks")
    env.backend.states[M] = STATE_LOADING
    assert env.run(module) == "skipped"
    assert env.entries_left() == published_dirs(env.repo, M)


def test_a_half_written_repo_entries_still_renames_and_deletes(env, isolated_cleanup,
                                                                monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    module = isolated_cleanup("repo_entries")
    assert module.repo_entries is None
    stale = os.path.join(env.repo, ".trash-model-old-ffff0000-1-12")
    os.makedirs(stale)
    renames = []
    real_rename = os.rename
    monkeypatch.setattr(module.os, "rename",
                        lambda s, d: (renames.append(d), real_rename(s, d))[1])
    assert env.run(module) == "removed"
    assert env.entries_left() == []
    pattern = re.compile(r"^\.trash-(base_|marshal_)?{0}-{1}-[0-9]+$".format(
        re.escape(M), os.getpid()))
    assert len(renames) == 3 and all(pattern.match(os.path.basename(d)) for d in renames)
    assert os.path.isdir(stale)  # the sweep was skipped
    warnings = [l for l in _messages(caplog) if l.startswith("WARNING:Could not import")]
    assert len(warnings) == 1
    assert warnings[0].startswith("WARNING:Could not import dda_triton.repo_entries (")
    assert warnings[0].endswith("); skipping the hidden-directory sweep")


def test_a_half_written_status_records_skips_the_record_clear(env, isolated_cleanup, caplog):
    caplog.set_level(logging.DEBUG)
    module = isolated_cleanup("model_status_records")
    model_status_records.write_load_outcome(M, state="LOADING", reason="r",
                                            records_dir=env.records)
    assert env.run(module) == "removed"
    assert M in model_status_records.read_load_outcomes(env.records)
    assert any(l.endswith("); not clearing the load outcome record") for l in _messages(caplog))
