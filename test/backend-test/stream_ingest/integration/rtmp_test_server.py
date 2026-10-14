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
"""A synthetic RTMP source for the integration tests: the ``ffmpeg`` CLI in
RTMP listen mode serves one client at a time, and is started again when the
client goes, so a worker can reconnect.

H.265 over RTMP needs FFmpeg 6.1 or later (Enhanced RTMP). The system
``ffmpeg`` of the flask-app image is 4.4, so the test run installs the
static FFmpeg of ``imageio-ffmpeg`` (a test-only dependency); H.264 works
with either.
"""
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

SIZE = "1280x720"
FPS = 15


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def modern_ffmpeg() -> Optional[str]:
    """An FFmpeg 6.1+ executable (imageio-ffmpeg's), or None."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed
        return None


def ffmpeg_for(codec: str) -> Optional[str]:
    return modern_ffmpeg() or (shutil.which("ffmpeg") if codec == "h264" else None)


class RtmpTestServer:
    """Serves ``codec`` at ``url`` until closed; a context manager."""

    def __init__(self, codec: str = "h264", path: str = "live/test", size: str = SIZE):
        self.codec = codec
        self.path = path
        self.size = size
        self.port = free_port()
        self.executable = ffmpeg_for(codec)
        self.connections = 0
        self._stop = threading.Event()
        self._process = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"rtmp://127.0.0.1:{self.port}/{self.path}"

    def _command(self):
        encode = (["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", str(FPS)]
                  if self.codec == "h264" else
                  ["-c:v", "libx265", "-preset", "ultrafast",
                   "-x265-params", f"log-level=error:keyint={FPS}:min-keyint={FPS}"])
        return ([self.executable, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
                 f"testsrc=size={self.size}:rate={FPS}"] + encode
                + ["-pix_fmt", "yuv420p", "-f", "flv", "-listen", "1", self.url])

    def _serve(self):
        while not self._stop.is_set():
            self._process = subprocess.Popen(self._command(), stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.connections += 1
            self._process.wait()
            time.sleep(0.1)

    def __enter__(self):
        if self.executable is None:
            raise RuntimeError(f"no FFmpeg that can serve {self.codec} over RTMP")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        time.sleep(1.0)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._process is not None:
            self._process.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False
