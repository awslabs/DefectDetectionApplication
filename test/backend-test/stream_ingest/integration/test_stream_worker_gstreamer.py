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
"""Real-GStreamer tests of the Stream_Ingest_Service (rtsp-rtmp-stream-cameras
task 16.9 — Requirements 6.1, 6.4, 7.1-7.3, 8.7).

Run in the flask-app image, with the real ``gi`` (so without the suite
conftest's ``gi`` mock), PyAV and a modern FFmpeg for the test source:

    python3.11 -m pip install av==17.1.0 imageio-ffmpeg==0.6.0
    PYTHONPATH=src/backend python3.11 -m pytest \
        test/backend-test/stream_ingest/integration --noconftest -q

They skip cleanly anywhere GStreamer or PyAV is missing. Covered:

- the capability probe on this image (software decoders only);
- the decode tails over generated H.264 and H.265 samples, scaled;
- H.264 and Enhanced-RTMP H.265 pulled over RTMP from an FFmpeg listen-mode
  source, through the real manager, session and worker processes;
- a SIGKILLed worker is restarted under the backoff while another camera's
  session keeps streaming;
- the RTSP head against a closed port reports a network error;
- a stream in another codec fails with ``unsupported_codec``, naming it;
- no credential appears in the worker's ``/proc/<pid>/cmdline`` or
  ``/proc/<pid>/environ``, in any log record, or in its stderr.
"""
import logging
import os
import signal
import sys
import time

import pytest

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..", "src", "backend"))
for path in (_BACKEND, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

gi = pytest.importorskip("gi", reason="native gi bindings not available", exc_type=ImportError)
try:
    gi.require_version("Gst", "1.0")
    gi.require_version("GstApp", "1.0")
    from gi.repository import Gst  # noqa: E402
    Gst.init(None)
    real_gstreamer = isinstance(Gst.version_string(), str)
except (ValueError, ImportError, AttributeError) as error:  # pragma: no cover - environment
    pytest.skip(f"GStreamer not available: {error}", allow_module_level=True)
if not real_gstreamer:  # the suite conftest's gi mock (run with --noconftest)
    pytest.skip("GStreamer is mocked here; run with --noconftest", allow_module_level=True)
pytest.importorskip("av", reason="PyAV is not installed", exc_type=ImportError)

from rtmp_test_server import RtmpTestServer, ffmpeg_for  # noqa: E402
from stream_ingest import capabilities as capabilities_module  # noqa: E402
from stream_ingest import health  # noqa: E402
from stream_ingest.manager import StreamIngestManager  # noqa: E402
from stream_ingest.pipeline import fit_within, select_decoder, tail_description  # noqa: E402
from stream_ingest.sources import StreamSource  # noqa: E402

USERNAME = "user-INTEG-2f7b"
PASSWORD = "pw-INTEG-9a44"
STREAM_KEY = "streamkey-INTEG-5d1e"
SECRETS = (USERNAME, PASSWORD, STREAM_KEY)

SOFTWARE_CAPABILITIES = {"codecs": {"h264": {"hardware": None, "software": "avdec_h264"},
                                    "h265": {"hardware": None, "software": "avdec_h265"}}}


class StaticCapabilities:
    """Stands in for the probe cache (the probe itself is tested below)."""

    def start(self):
        pass

    def get(self, wait_s=0):
        return dict(SOFTWARE_CAPABILITIES)

    def peek(self):
        return dict(SOFTWARE_CAPABILITIES)


def settings(**overrides):
    base = {"decoder": "auto", "maxFrameDimension": 640, "stallTimeoutS": 10}
    base.update(overrides)
    return base


def wait_for(predicate, timeout_s, interval_s=0.2):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return predicate()


@pytest.fixture
def manager_factory():
    managers = []

    def make(sources):
        manager = StreamIngestManager(capabilities=StaticCapabilities(),
                                      configured_source=lambda image_source_id: sources[image_source_id],
                                      max_sessions=lambda: 4, supervise=True)
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        manager.shutdown()
    time.sleep(0.5)


def worker_pid(manager, key):
    session = manager.session(key)
    worker = session._worker if session is not None else None
    return getattr(worker, "pid", None)


def assert_no_secret_in_process(pid):
    for name in ("cmdline", "environ"):
        with open(f"/proc/{pid}/{name}", "rb") as handle:
            content = handle.read().decode("utf-8", "replace")
        for secret in SECRETS:
            assert secret not in content, f"a credential is in /proc/<worker>/{name}"


class TestCapabilityProbe:
    def test_the_probe_reports_this_images_decoders(self):
        result = capabilities_module.run_probe()
        assert "probeError" not in result, result
        assert result["rtsp"] is True and result["rtmp"] is True
        assert result["codecs"]["h264"]["software"] == "avdec_h264"
        assert result["codecs"]["h265"]["software"] == "avdec_h265"
        # A hardware decoder is listed only where it decoded the sample: none
        # in a build-host image, nvv4l2decoder in a Jetson backend container.
        for codec, nvidia in (("h264", "nvh264dec"), ("h265", "nvh265dec")):
            hardware = result["codecs"][codec]["hardware"]
            assert hardware in (None, "nvv4l2decoder", nvidia), result
            if hardware is not None:
                assert Gst.ElementFactory.find(hardware) is not None
        assert result["gstreamer"].startswith("1.")
        assert result["pyav"] and result["ffmpeg"]


def _pull(description, timeout_s=10.0):
    pipeline = Gst.parse_launch(description)
    appsink = pipeline.get_by_name("frames")
    frames = []
    pipeline.set_state(Gst.State.PLAYING)
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and len(frames) < 3:
            sample = appsink.emit("try-pull-sample", 200 * Gst.MSECOND)
            if sample is None:
                if appsink.get_property("eos"):
                    break
                continue
            structure = sample.get_caps().get_structure(0)
            frames.append((structure.get_string("format"), structure.get_int("width")[1],
                           structure.get_int("height")[1], sample.get_buffer().get_size()))
    finally:
        pipeline.set_state(Gst.State.NULL)
    return frames


class TestDecodeTails:
    @pytest.mark.parametrize("codec", ["h264", "h265"])
    def test_the_software_tail_decodes_and_scales_a_generated_sample(self, codec, tmp_path):
        data = capabilities_module.sample(codec)
        assert data, "no encoder produced a sample"
        path = tmp_path / f"sample.{codec}"
        path.write_bytes(data)
        selection = select_decoder("auto", codec, SOFTWARE_CAPABILITIES)
        scale = fit_within(320, 240, 160)
        frames = _pull(f"filesrc location={path} ! {tail_description(codec, selection, scale)}")
        assert frames, "the tail produced no frame"
        for frame_format, width, height, size in frames:
            assert (frame_format, width, height) == ("RGB", 160, 120)
            assert size >= 160 * 3 * 120

    def test_a_tail_without_a_scale_keeps_the_source_size(self, tmp_path):
        path = tmp_path / "sample.h264"
        path.write_bytes(capabilities_module.sample("h264"))
        selection = select_decoder("software", "h264", SOFTWARE_CAPABILITIES)
        frames = _pull(f"filesrc location={path} ! {tail_description('h264', selection, None)}")
        assert frames and all(frame[:3] == ("RGB", 320, 240) for frame in frames)


@pytest.mark.skipif(ffmpeg_for("h265") is None, reason="no FFmpeg 6.1+ to serve E-RTMP H.265")
class TestRtmpPull:
    @pytest.mark.parametrize("codec", ["h264", "h265"])
    def test_frames_arrive_scaled_from_an_rtmp_source(self, codec, manager_factory):
        with RtmpTestServer(codec) as server:
            source = StreamSource("rtmp", server.url, settings())
            manager = manager_factory({"cam": source})
            manager.acquire_lease("cfg-cam", "test")
            state = wait_for(lambda: manager.health("cfg-cam")["state"] == health.STREAMING, 30)
            assert state, manager.health("cfg-cam")
            frame = manager.latest_frame("cfg-cam", wait_ms=5000)
            assert frame is not None
            assert (frame.width, frame.height) == (640, 360)
            assert len(frame.data) == 640 * 360 * 3
            newer = manager.latest_frame("cfg-cam", after_seq=frame.seq, wait_ms=5000)
            assert newer is not None and newer.seq > frame.seq
            document = manager.health("cfg-cam")
            assert document["codec"] == codec
            assert (document["width"], document["height"]) == (1280, 720)
            assert document["decoder"] == "software" and document["decoderElement"] == f"avdec_{codec}"

    def test_credentials_stay_out_of_the_process_the_logs_and_stderr(self, manager_factory, caplog):
        with RtmpTestServer("h264", path=f"live/{STREAM_KEY}") as server:
            base_url = server.url[:-len(STREAM_KEY) - 1]
            source = StreamSource("rtmp", base_url, settings(),
                                  {"username": USERNAME, "password": PASSWORD, "urlSecret": f"/{STREAM_KEY}"})
            manager = manager_factory({"secret": source})
            with caplog.at_level(logging.DEBUG):
                manager.acquire_lease("cfg-secret", "test")
                assert wait_for(lambda: manager.health("cfg-secret")["state"] == health.STREAMING, 30), \
                    manager.health("cfg-secret")
                pid = worker_pid(manager, "cfg-secret")
                assert pid is not None
                assert_no_secret_in_process(pid)
                assert manager.latest_frame("cfg-secret", wait_ms=5000) is not None
                stderr = "\n".join(manager.session("cfg-secret")._worker.stderr_tail())
                os.kill(pid, signal.SIGKILL)
                assert wait_for(lambda: manager.health("cfg-secret")["reconnects"] >= 1, 10)
            text = caplog.text + stderr + repr(manager.health("cfg-secret"))
            for secret in SECRETS:
                assert secret not in text

    def test_a_killed_worker_recovers_and_the_other_camera_keeps_streaming(self, manager_factory):
        with RtmpTestServer("h264") as first, RtmpTestServer("h265") as second:
            manager = manager_factory({"a": StreamSource("rtmp", first.url, settings()),
                                       "b": StreamSource("rtmp", second.url, settings())})
            manager.acquire_lease("cfg-a", "test")
            manager.acquire_lease("cfg-b", "test")
            for key in ("cfg-a", "cfg-b"):
                assert wait_for(lambda key=key: manager.health(key)["state"] == health.STREAMING, 30), \
                    manager.health(key)
            before = manager.latest_frame("cfg-a", wait_ms=5000)
            other_pid = worker_pid(manager, "cfg-b")
            os.kill(worker_pid(manager, "cfg-a"), signal.SIGKILL)
            assert wait_for(lambda: manager.health("cfg-a")["reconnects"] >= 1, 10)
            assert manager.health("cfg-a")["lastError"]["category"] == health.WORKER_EXIT
            assert wait_for(lambda: manager.health("cfg-a")["state"] == health.STREAMING, 40), \
                manager.health("cfg-a")
            after = manager.latest_frame("cfg-a", after_seq=before.seq, wait_ms=5000)
            assert after is not None and after.seq > before.seq
            # The other camera's worker was never touched.
            assert worker_pid(manager, "cfg-b") == other_pid
            assert manager.health("cfg-b")["state"] == health.STREAMING
            assert manager.health("cfg-b")["reconnects"] == 0

    def test_another_codec_is_unsupported_and_named(self, manager_factory, tmp_path):
        server = RtmpTestServer("h264")
        # Sorenson Spark (FLV1) is a video codec FLV carries that is neither
        # H.264 nor H.265.
        server._command = lambda: [server.executable, "-hide_banner", "-loglevel", "error", "-re",
                                   "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                                   "-c:v", "flv", "-f", "flv", "-listen", "1", server.url]
        with server:
            manager = manager_factory({"flv": StreamSource("rtmp", server.url, settings())})
            manager.acquire_lease("cfg-flv", "test")
            assert wait_for(lambda: manager.health("cfg-flv")["state"] == health.FAILED, 30), \
                manager.health("cfg-flv")
            error = manager.health("cfg-flv")["lastError"]
            assert error["category"] == health.UNSUPPORTED_CODEC
            # Named as FFmpeg names it (Sorenson Spark is FFmpeg's "flv").
            assert "video codec flv is not supported" in error["message"]


class TestRateStage:
    """The publish cap on frames without a duration (found on hardware,
    task 25.3: an Amcrest PTZ's H.264 carries no VUI timing, and
    ``videorate drop-only`` aborted the worker on GStreamer 1.20 and 1.24
    with ``assertion failed: (GST_BUFFER_DURATION_IS_VALID (outbuf))``)."""

    @staticmethod
    def _run(buffers, fps_cap=10, pace_s=0.0):
        """Push ``buffers`` (PTS values), ``pace_s`` apart, through the rate
        stage; returns the PTS of the frames it kept."""
        from stream_ingest.pipeline import RATE_NAME, RateLimiter
        from stream_ingest.worker import make_rate_probe

        pipeline = Gst.parse_launch(
            "appsrc name=src format=time caps=video/x-raw,format=RGB,width=8,height=8,framerate=0/1 "
            f"! identity name={RATE_NAME} silent=true "
            "! appsink name=frames sync=false max-buffers=1000 drop=false emit-signals=false")
        rate = pipeline.get_by_name(RATE_NAME)
        rate.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                              make_rate_probe(Gst, RateLimiter(fps_cap)))
        source, sink = pipeline.get_by_name("src"), pipeline.get_by_name("frames")
        pipeline.set_state(Gst.State.PLAYING)
        try:
            started = time.monotonic()
            for index, pts in enumerate(buffers):
                if pace_s:
                    time.sleep(max(0.0, started + index * pace_s - time.monotonic()))
                buffer = Gst.Buffer.new_wrapped(bytes(8 * 8 * 3))
                buffer.pts = pts
                buffer.duration = Gst.CLOCK_TIME_NONE
                source.emit("push-buffer", buffer)
            source.emit("end-of-stream")
            kept = []
            while True:
                sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
                if sample is None:
                    break
                kept.append(sample.get_buffer().pts)
            return kept
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_frames_without_a_duration_are_capped_not_fatal(self):
        kept = self._run([index * Gst.SECOND // 30 for index in range(90)], pace_s=1 / 30)  # 3 s at 30 fps
        assert 27 <= len(kept) <= 33, len(kept)
        assert kept == sorted(kept)

    def test_frames_with_and_without_a_pts_are_capped_alike(self):
        """An Amcrest PTZ's 30 fps sub stream set no PTS on a third of its
        decoded frames; a cap that timed those by the clock and the rest by
        PTS passed 17-21 frames per second (task 25.3)."""
        stamps = [Gst.CLOCK_TIME_NONE if index % 3 == 2 else index * Gst.SECOND // 30 for index in range(90)]
        kept = self._run(stamps, pace_s=1 / 30)
        assert 27 <= len(kept) <= 33, len(kept)

    def test_frames_without_a_timestamp_are_timed_on_arrival(self):
        kept = self._run([Gst.CLOCK_TIME_NONE] * 20)  # pushed in a burst
        assert 1 <= len(kept) <= 3, len(kept)


class TestRtspHead:
    def test_a_closed_port_is_a_network_error_without_leaking_credentials(self, manager_factory, caplog):
        from rtmp_test_server import free_port

        url = f"rtsp://127.0.0.1:{free_port()}/stream1"
        source = StreamSource("rtsp", url, settings(transport="tcp", latencyMs=200),
                              {"username": USERNAME, "password": PASSWORD, "urlSecret": f"?token={STREAM_KEY}"})
        manager = manager_factory({"rtsp": source})
        with caplog.at_level(logging.DEBUG):
            manager.acquire_lease("cfg-rtsp", "test")
            pid = wait_for(lambda: worker_pid(manager, "cfg-rtsp"), 5)
            if pid:
                try:
                    assert_no_secret_in_process(pid)
                except FileNotFoundError:
                    pass  # it already failed and exited
            assert wait_for(lambda: (manager.health("cfg-rtsp")["lastError"] or {}).get("category"), 30)
        error = manager.health("cfg-rtsp")["lastError"]
        assert error["category"] == health.NETWORK_ERROR, error
        assert manager.health("cfg-rtsp")["state"] == health.RECONNECTING
        text = caplog.text + repr(manager.health("cfg-rtsp"))
        for secret in SECRETS:
            assert secret not in text

    @staticmethod
    def _first_error(manager_factory, url, credentials=None):
        source = StreamSource("rtsp", url, settings(transport="tcp", latencyMs=200), dict(credentials or {}))
        manager = manager_factory({"rtsp": source})
        manager.acquire_lease("cfg-rtsp", "test")
        assert wait_for(lambda: (manager.health("cfg-rtsp")["lastError"] or {}).get("category"), 30)
        return manager.health("cfg-rtsp")["lastError"]

    def test_a_path_nobody_publishes_is_not_found(self, manager_factory):
        # Found on hardware (task 25.3, MediaMTX): rtspsrc treats a 404 like
        # a 401 and first posts "No supported authentication protocol was
        # found", which read as an authentication failure.
        from rtsp_test_responder import RtspResponder

        with RtspResponder("404 Not Found") as responder:
            error = self._first_error(manager_factory, responder.url("nosuchpath"))
            assert "DESCRIBE" in responder.requests
        assert error["category"] == health.NOT_FOUND, error

    def test_a_401_without_credentials_is_still_an_authentication_failure(self, manager_factory):
        from rtsp_test_responder import RtspResponder

        challenge = 'WWW-Authenticate: Digest realm="dda-test", nonce="0123456789abcdef"\r\n'
        with RtspResponder("401 Unauthorized", extra_headers=challenge) as responder:
            error = self._first_error(manager_factory, responder.url())
        assert error["category"] == health.AUTHENTICATION_FAILED, error

    def test_an_untrusted_certificate_is_a_tls_verification_failure(self, manager_factory):
        # Found on hardware (task 25.3): rtspsrc reports a rejected
        # certificate only as "Failed to connect", a network error.
        from gi.repository import Gio
        from rtsp_test_responder import RtspResponder

        if not Gio.TlsBackend.get_default().supports_tls():
            pytest.skip("GIO has no TLS backend here")
        try:
            responder = RtspResponder("404 Not Found", tls=True)
        except RuntimeError as error:
            pytest.skip(str(error))
        with responder:
            error = self._first_error(manager_factory, responder.url("h264"),
                                      {"username": USERNAME, "password": PASSWORD})
            assert "DESCRIBE" not in responder.requests, "nothing is sent over a rejected connection"
        assert error["category"] == health.TLS_VERIFICATION_FAILED, error
        assert "unknown-ca" in error["message"], error
        for secret in SECRETS:
            assert secret not in error["message"]
