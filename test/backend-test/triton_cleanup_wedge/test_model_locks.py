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
"""``dda_triton.model_locks`` (triton-model-cleanup-wedge D1, task 3.1).

Real ``flock`` between processes: the lock holder is a separate Python
process (``fakes.LockHolder``), as the host scripts and the backend
container are. Runs under Python 3.8 as well.

_Requirements: 2.1, 2.2, 3.14_
"""
import fcntl
import os
import stat

import pytest

from dda_triton import model_locks
from dda_triton.constants import TRITON_MODEL_DIR

from triton_cleanup_wedge.fakes import FakeTime, LockHolder, lock_is_free

MODEL = "model-locks-jetson-xavier-jp5"


@pytest.fixture
def lock_dir(tmp_path):
    return str(tmp_path / "dda_triton" / "locks")


def test_lock_dir_for_is_the_locks_sibling_of_the_repository(tmp_path):
    repo = str(tmp_path / "dda_triton" / "triton_model_repo")
    assert model_locks.lock_dir_for(repo) == str(tmp_path / "dda_triton" / "locks")
    assert model_locks.lock_dir_for(repo + "/") == str(tmp_path / "dda_triton" / "locks")
    assert model_locks.LOCK_DIR == "/aws_dda/dda_triton/locks"
    assert model_locks.lock_dir_for(TRITON_MODEL_DIR) == model_locks.LOCK_DIR


@pytest.mark.parametrize("name, key", [
    ("model-a", "model-a"),
    ("base_model-a", "model-a"),
    ("marshal_model-a", "model-a"),
    ("base_marshal_model-a", "marshal_model-a"),  # one prefix only
    ("A" * 255, "A" * 255),
])
def test_lock_key_strips_one_prefix(name, key):
    assert model_locks.lock_key(name) == key


@pytest.mark.parametrize("name", [
    "", ".", "..", "../model-a", "model/a", "-model", "_model", "model a", "A" * 256,
    "base_", "marshal_", "base_..", None, 5,
])
def test_lock_key_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        model_locks.lock_key(name)


def test_lock_path_and_modes(lock_dir):
    handle = model_locks.acquire("base_" + MODEL, exclusive=True, timeout_s=0, lock_dir=lock_dir)
    try:
        path = os.path.join(lock_dir, MODEL + ".lock")
        assert handle.path == path == model_locks.lock_path(MODEL, lock_dir)
        assert stat.S_IMODE(os.stat(lock_dir).st_mode) == 0o755
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    finally:
        handle.release()
    # Never unlinked.
    assert os.path.exists(path)


def test_shared_excludes_exclusive_across_processes(lock_dir):
    path = model_locks.lock_path(MODEL, lock_dir)
    os.makedirs(lock_dir)
    with LockHolder(path, exclusive=False):
        with pytest.raises(model_locks.LockTimeout):
            model_locks.acquire(MODEL, exclusive=True, timeout_s=0, lock_dir=lock_dir)
        shared = model_locks.acquire(MODEL, exclusive=False, timeout_s=0, lock_dir=lock_dir)
        shared.release()


def test_exclusive_excludes_shared_across_processes(lock_dir):
    path = model_locks.lock_path(MODEL, lock_dir)
    os.makedirs(lock_dir)
    with LockHolder(path, exclusive=True):
        with pytest.raises(model_locks.LockTimeout):
            model_locks.acquire(MODEL, exclusive=False, timeout_s=0, lock_dir=lock_dir)


def test_two_shared_locks_coexist(lock_dir):
    path = model_locks.lock_path(MODEL, lock_dir)
    os.makedirs(lock_dir)
    with LockHolder(path, exclusive=False), LockHolder(path, exclusive=False):
        handle = model_locks.acquire(MODEL, exclusive=False, timeout_s=0, lock_dir=lock_dir)
        handle.release()
    assert lock_is_free(path)


def test_a_held_lock_times_out_after_polling(lock_dir):
    path = model_locks.lock_path(MODEL, lock_dir)
    os.makedirs(lock_dir)
    fake = FakeTime()
    waits = []
    with LockHolder(path, exclusive=False):
        with pytest.raises(model_locks.LockTimeout):
            model_locks.acquire(MODEL, exclusive=True, timeout_s=5, poll_s=1.0,
                                on_wait=waits.append, lock_dir=lock_dir,
                                clock=fake.monotonic, sleep=fake.sleep)
    assert sum(fake.sleeps) == pytest.approx(5.0)
    assert waits == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_a_wait_succeeds_once_the_holder_releases(lock_dir):
    path = model_locks.lock_path(MODEL, lock_dir)
    os.makedirs(lock_dir)
    holder = LockHolder(path, exclusive=True)
    fake = FakeTime()

    def sleep(seconds):
        fake.sleep(seconds)
        holder.release()

    handle = model_locks.acquire(MODEL, exclusive=False, timeout_s=10, lock_dir=lock_dir,
                                 clock=fake.monotonic, sleep=sleep)
    handle.release()
    assert fake.sleeps == [1.0]


def test_a_lock_dies_with_its_process(lock_dir):
    path = model_locks.lock_path(MODEL, lock_dir)
    os.makedirs(lock_dir)
    holder = LockHolder(path, exclusive=True)
    assert not lock_is_free(path)
    holder.kill()
    handle = model_locks.acquire(MODEL, exclusive=True, timeout_s=0, lock_dir=lock_dir)
    handle.release()


def test_lock_unavailable_on_an_unwritable_directory(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    parent = tmp_path / "ro"
    parent.mkdir()
    os.chmod(str(parent), 0o555)
    try:
        with pytest.raises(model_locks.LockUnavailable):
            model_locks.acquire(MODEL, exclusive=True, timeout_s=0,
                                lock_dir=str(parent / "locks"))
    finally:
        os.chmod(str(parent), 0o755)


def test_lock_unavailable_when_the_lock_path_is_a_directory(lock_dir):
    os.makedirs(os.path.join(lock_dir, MODEL + ".lock"))
    with pytest.raises(model_locks.LockUnavailable):
        model_locks.acquire(MODEL, exclusive=True, timeout_s=0, lock_dir=lock_dir)


def test_the_descriptor_is_close_on_exec(lock_dir):
    handle = model_locks.acquire(MODEL, exclusive=False, timeout_s=0, lock_dir=lock_dir)
    try:
        assert fcntl.fcntl(handle.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        handle.release()


def test_release_unlocks_before_closing_and_is_idempotent(lock_dir, monkeypatch):
    handle = model_locks.acquire(MODEL, exclusive=True, timeout_s=0, lock_dir=lock_dir)
    fd = handle.fileno()
    calls = []
    real_flock, real_close = fcntl.flock, os.close
    monkeypatch.setattr(model_locks.fcntl, "flock",
                        lambda f, op: (calls.append(("flock", f, op)), real_flock(f, op))[1])
    monkeypatch.setattr(model_locks.os, "close",
                        lambda f: (calls.append(("close", f)), real_close(f))[1])
    handle.release()
    handle.release()
    assert calls == [("flock", fd, fcntl.LOCK_UN), ("close", fd)]
    assert not handle.held
    monkeypatch.undo()
    assert lock_is_free(model_locks.lock_path(MODEL, lock_dir))


def test_an_invalid_name_raises_value_error_before_any_file_is_made(lock_dir):
    with pytest.raises(ValueError):
        model_locks.acquire("../escape", exclusive=True, timeout_s=0, lock_dir=lock_dir)
    assert not os.path.exists(lock_dir)
