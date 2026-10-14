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
"""Fakes for the Stream_Ingest_Service: a manual clock, a scripted
Stream_Worker, a frame reader over in-memory frames, and a recording timer.

``FakeWorker`` speaks the real control protocol to a ``StreamSession``: it
answers ``frame`` requests synchronously from the frames the test produced,
and the test drives health lines, errors and exits.
"""
import os
import sys
import threading
import time

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from stream_ingest.sources import StreamSource  # noqa: E402

CAPABILITIES = {"codecs": {"h264": {"hardware": "nvv4l2decoder", "software": "avdec_h264"},
                           "h265": {"hardware": None, "software": "avdec_h265"}}}


class ManualClock:
    """A monotonic clock and a wall clock the test advances."""

    def __init__(self, start: float = 1000.0, wall_start: float = 1_790_000_000.0):
        self.now = start
        self._wall_offset = wall_start - start

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.now + self._wall_offset

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class FakeWorker:
    """One scripted worker process.

    By default frame requests are answered at once. With ``blocking=True`` a
    request with ``waitMs`` and no newer frame is answered from a thread when
    a frame is produced or the wait runs out, as the real worker does; use
    it with a real clock.
    """

    def __init__(self, config, on_message, on_exit, secrets, clock=None, blocking=False):
        self.config = dict(config)
        self.config["credentials"] = dict(config.get("credentials") or {})
        self.on_message = on_message
        self.on_exit = on_exit
        self.secrets = list(secrets)
        self.clock = clock
        self.blocking = blocking
        self.sent = []
        self.frames = []  # (seq, data, width, height, acquired_at_ms)
        self.killed = False
        self.stopped = False
        self.exited = False
        self.answer_frames = True
        self._produced = threading.Condition()

    # -- the process interface the session uses ---------------------------
    def send(self, message):
        if self.exited:
            return False
        self.sent.append(dict(message))
        if message.get("op") == "stop":
            self.stopped = True
        elif message.get("op") == "frame" and self.answer_frames:
            if self.blocking and not self._has_newer(message) and message.get("waitMs"):
                threading.Thread(target=self._reply_when_ready, args=(dict(message),), daemon=True).start()
            else:
                self._reply(message)
        return True

    def _has_newer(self, request):
        return bool(self.frames) and self.frames[-1][0] > int(request.get("after") or 0)

    def _reply_when_ready(self, request):
        deadline = time.monotonic() + int(request["waitMs"]) / 1000.0
        with self._produced:
            while not self._has_newer(request) and not self.exited:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._produced.wait(remaining)
        self._reply(request)

    def stop(self):
        self.send({"op": "stop"})

    def kill(self):
        self.killed = True

    def alive(self):
        return not self.exited and not self.killed

    def stderr_tail(self):
        return []

    # -- test controls ---------------------------------------------------
    def health(self, state="connecting", **fields):
        message = {"op": "health", "state": state, "seq": self.frames[-1][0] if self.frames else 0}
        message.update(fields)
        self.on_message(self, message)

    def stream(self, **fields):
        fields.setdefault("codec", "h264")
        fields.setdefault("decoder", "software")
        fields.setdefault("width", 1280)
        fields.setdefault("height", 720)
        self.health("streaming", **fields)

    def produce(self, count=1, width=4, height=2, acquired_at_ms=None):
        for _ in range(count):
            seq = (self.frames[-1][0] if self.frames else 0) + 1
            stamp = acquired_at_ms if acquired_at_ms is not None else (
                int(self.clock.wall() * 1000) if self.clock is not None else 0)
            data = bytes([seq % 256]) * (width * height * 3)
            with self._produced:
                self.frames.append((seq, data, width, height, stamp))
                # The worker keeps only its newest frame, as the real one does.
                del self.frames[:-1]
                self._produced.notify_all()

    def error(self, category, message="failed"):
        self.on_message(self, {"op": "error", "category": category, "message": message})

    def exit(self, code=1):
        if not self.exited:
            self.exited = True
            self.on_exit(self, code)

    def _reply(self, request):
        after = int(request.get("after") or 0)
        newest = self.frames[-1] if self.frames else None
        if newest is None or newest[0] <= after:
            self.on_message(self, {"op": "frame", "id": request.get("id"), "seq": None})
            return
        seq, data, width, height, stamp = newest
        self.on_message(self, {"op": "frame", "id": request.get("id"), "seq": seq, "width": width,
                               "height": height, "stride": width * 3, "channels": 3,
                               "acquiredAtMs": stamp, "_data": data})


class FakeReader:
    """Reads the in-memory frame a FakeWorker reply carries. ``on_read`` is
    called while the copy is in flight."""

    def __init__(self, on_read=None):
        self.on_read = on_read
        self.path = None

    def read(self, header):
        if self.on_read is not None:
            self.on_read(header)
        return bytes(header["_data"])

    def close(self):
        pass


class Spawner:
    """``spawn`` for a StreamSession: records every FakeWorker. With
    ``streaming=True`` each new worker reports streaming at once."""

    def __init__(self, clock=None, fail_with=None, blocking=False, streaming=False):
        self.clock = clock
        self.workers = []
        self.fail_with = fail_with
        self.blocking = blocking
        self.streaming = streaming

    def __call__(self, config, on_message, on_exit, secrets):
        if self.fail_with is not None:
            raise self.fail_with
        worker = FakeWorker(config, on_message, on_exit, secrets, clock=self.clock, blocking=self.blocking)
        self.workers.append(worker)
        if self.streaming:
            threading.Timer(0.01, worker.stream).start()
        return worker

    @property
    def current(self):
        return self.workers[-1] if self.workers else None


class RecordingTimer:
    """``timer`` for a StreamSession: runs nothing until ``fire_all``."""

    def __init__(self):
        self.pending = []

    def __call__(self, delay_s, action):
        self.pending.append((delay_s, action))

    def fire_all(self):
        pending, self.pending = self.pending, []
        for _delay, action in pending:
            action()


def rtsp_source(credentials=None, **settings):
    base = {"transport": "tcp", "latencyMs": 200, "decoder": "auto", "maxFrameDimension": 1920,
            "stallTimeoutS": 10}
    base.update(settings)
    return StreamSource("rtsp", "rtsp://10.0.4.21:554/stream1", base, dict(credentials or {}))


def make_session(clock=None, source=None, spawner=None, on_health=None, reader=None, **kwargs):
    """A StreamSession wired to fakes; returns (session, spawner, clock, timer)."""
    from stream_ingest.session import StreamSession

    clock = clock or ManualClock()
    spawner = spawner or Spawner(clock)
    timer = RecordingTimer()
    source = source or rtsp_source()
    session = StreamSession(
        "cfg-test", lambda: source, lambda: CAPABILITIES, spawn=spawner, clock=clock,
        wall_clock=clock.wall, on_health=on_health,
        reader_factory=(lambda: reader) if reader is not None else FakeReader,
        timer=timer, **kwargs)
    return session, spawner, clock, timer
