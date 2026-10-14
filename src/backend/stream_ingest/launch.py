#
#  Copyright 2025 Amazon Web Services, Inc.
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
#
"""Starting Stream_Worker processes (rtsp-rtmp-stream-cameras Requirements
6.1, 6.4, 8.7; design "Security Considerations").

A worker runs ``python -m stream_ingest.worker`` with this interpreter from
the backend root. Its credentials travel only in the first stdin line, so
they never appear in ``/proc/<pid>/cmdline`` or ``/proc/<pid>/environ``; the
environment it inherits is the backend's minus ``PYTHONHOME`` (which would
break a spawned interpreter's bootstrap), the backend's GStreamer debug
settings, and anything that looks like a secret. The worker is its own
process-group leader, so a signal meant for the backend never reaches it.

Its stderr (GStreamer and FFmpeg diagnostics) is kept, redacted, in a
bounded ring buffer for the session to log when the worker fails.
"""
import collections
import os
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

from stream_ingest import protocol
from stream_ingest.health import clean_message

#: Variables never passed to a worker.
_DROPPED_VARIABLES = frozenset({"PYTHONHOME", "GST_DEBUG", "GST_DEBUG_FILE",
                                "GST_DEBUG_DUMP_DOT_DIR"})
#: A variable whose name contains one of these is treated as a secret.
_SECRET_NAME_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
                        "PRIVATE_KEY", "SVCUID", "AUTH")

STDERR_LINES = 40


def backend_root() -> str:
    """The directory ``stream_ingest`` is imported from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def worker_environment(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment of a worker or probe process."""
    environment = dict(os.environ if base is None else base)
    for name in list(environment):
        upper = name.upper()
        if (name in _DROPPED_VARIABLES or upper.startswith("AWS_")
                or any(marker in upper for marker in _SECRET_NAME_MARKERS)):
            del environment[name]
    return environment


class SubprocessWorker:
    """A running Stream_Worker.

    ``on_message(worker, message)`` receives every protocol line the worker
    writes, and ``on_exit(worker, code)`` is called once, after the last
    line was delivered. Both run on this object's reader threads.
    """

    def __init__(self, config: Dict[str, Any],
                 on_message: Callable[["SubprocessWorker", Dict[str, Any]], None],
                 on_exit: Callable[["SubprocessWorker", int], None],
                 secrets: Iterable[str] = (),
                 command: Optional[List[str]] = None):
        self._on_message = on_message
        self._on_exit = on_exit
        self._secrets = [value for value in secrets if value]
        self._stderr = collections.deque(maxlen=STDERR_LINES)
        self._send_lock = threading.Lock()
        self._process = subprocess.Popen(
            command or [sys.executable, "-m", "stream_ingest.worker"],
            cwd=backend_root(), env=worker_environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            start_new_session=True)
        self.pid = self._process.pid
        self.send(config)
        self._stdout_thread = threading.Thread(target=self._read_stdout, name=f"stream-worker-{self.pid}-out",
                                               daemon=True)
        self._stdout_thread.start()
        threading.Thread(target=self._read_stderr, name=f"stream-worker-{self.pid}-err", daemon=True).start()
        threading.Thread(target=self._wait, name=f"stream-worker-{self.pid}-wait", daemon=True).start()

    def send(self, message: Dict[str, Any]) -> bool:
        """Write one protocol line; False when the worker is gone."""
        data = protocol.encode(message)
        with self._send_lock:
            try:
                self._process.stdin.write(data)
                self._process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def stop(self) -> None:
        """Ask the worker to stop; it exits within a few seconds."""
        self.send({"op": protocol.OP_STOP})

    def kill(self) -> None:
        """SIGKILL the worker."""
        try:
            self._process.kill()
        except OSError:
            pass

    def alive(self) -> bool:
        return self._process.poll() is None

    def stderr_tail(self) -> List[str]:
        """The last redacted stderr lines."""
        return list(self._stderr)

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        for line in iter(lambda: stream.readline(protocol.MAX_LINE_BYTES), b""):
            message = protocol.decode(line)
            if message is not None:
                try:
                    self._on_message(self, message)
                except Exception:  # noqa: BLE001 - a callback bug must not kill the reader
                    pass

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        for line in iter(lambda: stream.readline(8192), b""):
            text = clean_message(line.decode("utf-8", "replace"), self._secrets)
            if text:
                self._stderr.append(text)

    def _wait(self) -> None:
        code = self._process.wait()
        self._stdout_thread.join(timeout=2.0)
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._on_exit(self, code)
        except Exception:  # noqa: BLE001 - see _read_stdout
            pass
