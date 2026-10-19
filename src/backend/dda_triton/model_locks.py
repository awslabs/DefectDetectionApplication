#  Copyright 2026 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""One cross-process lock per model (triton-model-cleanup-wedge, design D1).

The model components' lifecycle scripts run on the host; the backend's load
requests run inside the backend container. Both see ``/aws_dda`` (the
container bind-mounts it), so an ``flock`` on a file under
``/aws_dda/dda_triton/locks/`` coordinates them:

* every load the backend requests holds the model's lock SHARED until the
  load reaches a terminal state (``dda_triton.load_tracker``);
* removing the model's repository entries (the Shutdown cleanup) or
  replacing them with changed content (a publish) takes it EXCLUSIVE.

The kernel drops a process's locks when the process dies, so a restarted
backend never leaves a stale lock. Lock files are never unlinked: an unlink
while another process holds the lock would let a new opener lock a different
inode.

Host-shared: this module is seeded to ``/aws_dda/dda_triton/`` and runs
under the host's system ``python3`` (3.8 on JP5). Standard library only.
"""
import errno
import fcntl
import os
import re
import time
from typing import Callable, Optional

from dda_triton.constants import TRITON_MODEL_DIR

#: The ``locks`` sibling of a model repository.
LOCK_DIR_NAME = "locks"

#: A component name, and therefore a lock key (design "Input Validation").
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_PREFIXES = ("base_", "marshal_")

DIR_MODE = 0o755
FILE_MODE = 0o644


def lock_dir_for(repo_dir: str) -> str:
    """The lock directory of the model repository ``repo_dir``."""
    return os.path.join(os.path.dirname(os.path.abspath(repo_dir)), LOCK_DIR_NAME)


#: The production lock directory, ``/aws_dda/dda_triton/locks``.
LOCK_DIR = lock_dir_for(TRITON_MODEL_DIR)


class LockTimeout(Exception):
    """The lock stayed held by another process for the whole wait."""


class LockUnavailable(Exception):
    """The lock file could not be opened or locked for another reason."""


def validate_name(name: str) -> str:
    """``name`` if it is a valid component name, else ``ValueError``."""
    if not isinstance(name, str) or not _NAME_PATTERN.match(name) or name in (".", ".."):
        raise ValueError("Invalid model name {0!r}".format(name))
    return name


def lock_key(name: str) -> str:
    """The lock key of ``name``: one leading ``base_`` or ``marshal_`` is
    removed, so the three directories of a model share one lock."""
    if not isinstance(name, str):
        raise ValueError("Invalid model name {0!r}".format(name))
    key = name
    for prefix in _PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return validate_name(key)


def lock_path(name: str, lock_dir: Optional[str] = None) -> str:
    return os.path.join(lock_dir or LOCK_DIR, lock_key(name) + ".lock")


class LockHandle:
    """A held lock. ``release()`` is idempotent."""

    def __init__(self, fd: int, path: str, exclusive: bool):
        self._fd = fd
        self.path = path
        self.exclusive = exclusive

    @property
    def held(self) -> bool:
        return self._fd is not None

    def fileno(self) -> Optional[int]:
        return self._fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        # Unlock explicitly before closing: a process forked without exec
        # (the vLLM engine core) shares the open file description, and
        # close() alone would not drop the lock while that copy is open.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __repr__(self) -> str:
        return "LockHandle({0!r}, exclusive={1}, held={2})".format(
            self.path, self.exclusive, self.held)


def _open(path: str) -> int:
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
        try:
            os.chmod(directory, DIR_MODE)  # makedirs applies the umask
        except OSError:
            pass
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
    try:
        if os.fstat(fd).st_mode & 0o777 != FILE_MODE:
            os.fchmod(fd, FILE_MODE)
    except OSError:
        pass  # the mode is cosmetic; the lock still works
    return fd


def acquire(name: str, exclusive: bool, timeout_s: float, poll_s: float = 1.0,
            on_wait: Optional[Callable[[float], None]] = None,
            lock_dir: Optional[str] = None,
            clock: Optional[Callable[[], float]] = None,
            sleep: Optional[Callable[[float], None]] = None) -> LockHandle:
    """Take the model's lock, polling a non-blocking ``flock`` until it
    succeeds or ``timeout_s`` has passed (``0`` means a single try).

    ``on_wait(elapsed_s)`` is called before each sleep. Raises ``ValueError``
    for an invalid name, :class:`LockTimeout` when the lock stays held, and
    :class:`LockUnavailable` for any other ``OSError``.
    """
    path = lock_path(name, lock_dir)
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    try:
        fd = _open(path)
    except OSError as exc:
        raise LockUnavailable("{0}: {1}".format(path, exc))
    operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    started = clock()
    try:
        while True:
            try:
                fcntl.flock(fd, operation)
                return LockHandle(fd, path, exclusive)
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise LockUnavailable("{0}: {1}".format(path, exc))
            elapsed = clock() - started
            if elapsed >= timeout_s:
                raise LockTimeout("{0} is held (waited {1:.0f}s)".format(path, elapsed))
            if on_wait is not None:
                on_wait(elapsed)
            sleep(max(0.0, min(poll_s, timeout_s - elapsed)))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
