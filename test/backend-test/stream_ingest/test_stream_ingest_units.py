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
"""Unit tests of the Stream_Ingest_Service parts that need no GStreamer
(rtsp-rtmp-stream-cameras task 16): failure classification, the control
protocol and shared-memory frames, sources, device settings, the worker
environment, and the session's watchdog and fallback paths.
"""
import errno
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from stream_fakes import CAPABILITIES, ManualClock, Spawner, make_session, rtsp_source  # noqa: E402
from stream_ingest import classify, health, protocol  # noqa: E402
from stream_ingest.health import StreamError  # noqa: E402
from stream_ingest.launch import worker_environment  # noqa: E402
from stream_ingest.session import (  # noqa: E402
    CONNECT_TIMEOUT_S,
    HEARTBEAT_TIMEOUT_S,
    STALL_BACKSTOP_MARGIN_S,
    STOP_GRACE_S,
)

SECRET = "pw-UNIT-73c1"


class TestClassification:
    @pytest.mark.parametrize("domain, code, message, expected", [
        (classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_NOT_AUTHORIZED, "Unauthorized", health.AUTHENTICATION_FAILED),
        (classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_NOT_FOUND, "Not found", health.NOT_FOUND),
        (classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_OPEN_READ_WRITE,
         "Could not open resource for reading and writing.", health.NETWORK_ERROR),
        (classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_READ, "Got error response: 503 (Service Unavailable).",
         health.SERVER_ERROR),
        (classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_OPEN_READ_WRITE,
         "Could not connect: Unacceptable TLS certificate", health.TLS_VERIFICATION_FAILED),
        (classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_READ,
         "Could not receive any UDP packets for 5.0000 seconds", health.TIMEOUT),
        (classify.GST_CORE_DOMAIN, classify.CORE_MISSING_PLUGIN, "no element", health.DECODER_UNAVAILABLE),
        (classify.GST_STREAM_DOMAIN, classify.STREAM_CODEC_NOT_FOUND, "no decoder", health.UNSUPPORTED_CODEC),
        ("other-quark", 1, "something odd", health.NETWORK_ERROR),
    ])
    def test_gstreamer_errors(self, domain, code, message, expected):
        assert classify.classify_gst_error(domain, code, message) == expected

    def test_ports_and_addresses_are_not_status_codes(self):
        assert classify.classify_gst_error(classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_OPEN_READ_WRITE,
                                           "Could not connect to 10.0.4.21:5003") == health.NETWORK_ERROR
        assert classify.classify_text("rtsp://10.0.40.1:4401/x") is None

    def test_decoder_errors_are_hardware_failures_only_for_hardware(self):
        assert classify.classify_gst_error(classify.GST_STREAM_DOMAIN, classify.STREAM_DECODE, "decode",
                                           from_decoder=True, hardware_decoder=True) == health.HARDWARE_DECODER_FAILED
        assert classify.classify_gst_error(classify.GST_STREAM_DOMAIN, classify.STREAM_DECODE, "decode",
                                           hardware_decoder=False) == health.NETWORK_ERROR

    @pytest.mark.parametrize("error_number, message, log, expected", [
        (None, "Input/output error", ["Server error: NetStream.Play.StreamNotFound"], health.NOT_FOUND),
        (None, "Input/output error", ["Server error: Authentication failed"], health.AUTHENTICATION_FAILED),
        (None, "Input/output error", ["Unable to verify peer certificate"], health.TLS_VERIFICATION_FAILED),
        (errno.ETIMEDOUT, "Connection timed out", [], health.TIMEOUT),
        (errno.ECONNREFUSED, "Connection refused", [], health.NETWORK_ERROR),
        (None, "Immediate exit requested", [], health.TIMEOUT),
    ])
    def test_pyav_errors(self, error_number, message, log, expected):
        assert classify.classify_av_error(error_number, message, log) == expected

    def test_categories_partition_the_retry_classes(self):
        assert set(health.TRANSIENT_CATEGORIES).isdisjoint(health.CONFIGURATION_CATEGORIES)
        assert health.normalize_category("made-up") == health.NETWORK_ERROR
        assert health.coarse_health(None) == health.COARSE_IDLE
        assert health.coarse_health({"state": health.CONNECTING}) == health.COARSE_RECONNECTING

    def test_messages_are_redacted_single_lines_of_bounded_length(self):
        text = health.clean_message(f"rtsp://admin:{SECRET}@10.0.4.21/x\nsecond {SECRET} " + "y" * 900, [SECRET])
        assert SECRET not in text and "\n" not in text
        assert len(text) <= health.MAX_MESSAGE_LENGTH and text.endswith("...")


class TestProtocol:
    def test_lines_round_trip_and_junk_is_ignored(self):
        message = {"op": "frame", "after": 3, "id": 1}
        assert protocol.decode(protocol.encode(message)) == message
        for junk in (b"", b"   \n", b"not json\n", b"[1,2]\n", b'{"no":"op"}\n',
                     b"x" * (protocol.MAX_LINE_BYTES + 1)):
            assert protocol.decode(junk) is None

    def test_a_segment_round_trips_packed_and_strided_frames(self):
        width, height = 5, 3  # 15 bytes a row, padded to a 16-byte stride
        stride = 16
        rows = [bytes([row]) * (width * 3) for row in range(height)]
        padded = b"".join(row + b"\xff" * (stride - width * 3) for row in rows)
        segment = protocol.FrameSegment(len(padded))
        reader = protocol.FrameReader()
        try:
            assert protocol.is_segment_path(segment.path)
            assert oct(os.stat(segment.path).st_mode & 0o777) == "0o600"
            slot = segment.write(padded)
            header = {"shm": segment.path, "slot": slot, "width": width, "height": height,
                      "stride": stride, "channels": 3}
            assert reader.read(header) == b"".join(rows)
            # The next write goes to the other slot, so the first is intact.
            second = segment.write(b"\x07" * len(padded))
            assert second != slot
            assert reader.read(header) == b"".join(rows)
        finally:
            reader.close()
            segment.close()
        assert not os.path.exists(segment.path)

    @pytest.mark.parametrize("header", [
        {"shm": "/etc/passwd", "slot": 0, "width": 1, "height": 1, "stride": 3},
        {"shm": None, "slot": 0, "width": 1, "height": 1, "stride": 3},
        {"slot": 0, "width": 1, "height": 1, "stride": 3},
    ])
    def test_a_header_naming_anything_but_a_segment_is_refused(self, header):
        with pytest.raises(ValueError):
            protocol.FrameReader().read(header)

    def test_an_oversized_or_malformed_header_is_refused(self):
        segment = protocol.FrameSegment(12)
        try:
            with pytest.raises(ValueError):
                protocol.FrameReader().read({"shm": segment.path, "slot": 0, "width": 4, "height": 4, "stride": 12})
            with pytest.raises(ValueError):
                protocol.FrameReader().read({"shm": segment.path, "slot": 2, "width": 1, "height": 1, "stride": 3})
        finally:
            segment.close()

    def test_segments_of_dead_workers_are_swept(self):
        live = protocol.FrameSegment(8)
        dead_path = os.path.join(protocol.shm_directory(), f"{protocol.SHM_PREFIX}999999999-deadbeef")
        with open(dead_path, "wb") as handle:
            handle.write(b"x")
        try:
            removed = protocol.sweep_segments(lambda pid: pid == os.getpid())
            assert removed >= 1
            assert not os.path.exists(dead_path) and os.path.exists(live.path)
        finally:
            live.close()


class TestWorkerErrorHelpers:
    """Pure helpers of the worker's bus error handling (found on hardware,
    task 25.3; the pipelines are covered by the integration tests)."""

    def test_only_rtspsrcs_generic_auth_setup_error_is_looked_past(self):
        from stream_ingest.worker import is_rtsp_auth_setup_error

        resource, open_read = classify.GST_RESOURCE_DOMAIN, classify.RESOURCE_OPEN_READ
        assert is_rtsp_auth_setup_error(resource, open_read, "No supported authentication protocol was found")
        assert not is_rtsp_auth_setup_error(resource, classify.RESOURCE_NOT_FOUND, "Not Found (404)")
        assert not is_rtsp_auth_setup_error(resource, open_read, "Failed to connect. (Generic error)")
        assert not is_rtsp_auth_setup_error(classify.GST_STREAM_DOMAIN, open_read,
                                            "No supported authentication protocol was found")
        assert not is_rtsp_auth_setup_error(resource, open_read, None)

    def test_certificate_problems_name_the_flags(self):
        from stream_ingest.worker import certificate_problems

        class Flags(int):
            value_nicks = ["unknown-ca", "bad-identity"]

        assert certificate_problems(Flags(3)) == "unknown-ca, bad-identity"
        assert certificate_problems(1 | 8) == "unknown-ca, expired"
        assert certificate_problems(0) == "unknown reason"
        assert certificate_problems(object()) == "unknown reason"


class TestRtmpDemuxOptions:
    """The RTMP client's open options (found on hardware, task 25.1):
    MediaMTX sends an E-RTMP H.265 track only to a client whose ``connect``
    lists ``hvc1`` in ``fourCcList``, which FFmpeg sends only when
    ``rtmp_enhanced_codecs`` is set."""

    def test_every_rtmp_open_advertises_the_enhanced_codecs(self):
        from stream_ingest.rtmp_demux import RtmpDemux
        options = RtmpDemux("rtmp://media.local/live/line1", read_timeout_s=10.0)._options()
        assert options == {"rw_timeout": "10000000", "rtmp_enhanced_codecs": "hvc1,av01,vp09"}

    def test_rtmps_also_verifies_against_the_system_bundle(self, monkeypatch):
        from stream_ingest import rtmp_demux
        monkeypatch.setattr(rtmp_demux, "system_ca_bundle", lambda: "/etc/ssl/certs/ca-certificates.crt")
        options = rtmp_demux.RtmpDemux("rtmps://media.local/live/line1")._options()
        assert options["rtmp_enhanced_codecs"] == "hvc1,av01,vp09"
        assert (options["tls_verify"], options["ca_file"]) == ("1", "/etc/ssl/certs/ca-certificates.crt")

    def test_rtmps_without_a_bundle_fails_closed(self, monkeypatch):
        from stream_ingest import rtmp_demux
        monkeypatch.setattr(rtmp_demux, "system_ca_bundle", lambda: None)
        with pytest.raises(StreamError) as raised:
            rtmp_demux.RtmpDemux("rtmps://media.local/live/line1")._options()
        assert raised.value.category == health.TLS_VERIFICATION_FAILED


class TestWorkerEnvironment:
    def test_secrets_and_backend_debug_settings_never_reach_a_worker(self):
        environment = worker_environment({
            "PATH": "/usr/bin", "LANG": "C.UTF-8", "GST_PLUGIN_PATH": "/opt/gst",
            "PYTHONHOME": "/usr/bin/python3", "GST_DEBUG": "4", "GST_DEBUG_FILE": "/x/gst-debug.log",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN": "t", "AWS_REGION": "us-east-1", "SVCUID": "u",
            "DB_PASSWORD": "p", "API_SECRET": "s", "GG_CREDENTIALS": "c", "SOME_TOKEN": "t",
        })
        assert environment == {"PATH": "/usr/bin", "LANG": "C.UTF-8", "GST_PLUGIN_PATH": "/opt/gst"}


class TestSources:
    @pytest.fixture
    def database(self, tmp_path):
        import dao.sqlite_db.models  # noqa: F401 - registers the tables
        from dao.sqlite_db.sqlite_db_operations import Base
        import stream_ingest.models  # noqa: F401

        engine = create_engine(f"sqlite:///{tmp_path / 'sources.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        yield factory
        engine.dispose()

    def _add(self, factory, image_source_id, source_type, location, settings):
        from dao.sqlite_db.models import ImageSource, ImageSourceConfiguration

        with factory() as db:
            db.add(ImageSourceConfiguration(imageSourceConfigId=f"cfg-{image_source_id}", gain=0, exposure=0,
                                            processingPipeline="", streamSettings=settings))
            db.add(ImageSource(imageSourceId=image_source_id, name="cam", type=source_type, location=location,
                               imageSourceConfigId=f"cfg-{image_source_id}", imageCapturePath="/tmp/x"))
            db.commit()

    def test_a_configured_source_reads_the_url_settings_and_credentials(self, database, tmp_path):
        from stream_ingest.credentials import CredentialStore
        from stream_ingest.sources import configured_source

        store = CredentialStore(directory=str(tmp_path / "creds"))
        store.put("s1", {"username": "viewer", "password": SECRET})
        self._add(database, "s1", "RTSP", "rtsp://10.0.4.21:554/s1",
                  {"transport": "udp", "latencyMs": 500, "credentialRef": "ref-1"})
        source = configured_source("s1", session_factory=database, credential_store=store)
        assert (source.protocol, source.url) == ("rtsp", "rtsp://10.0.4.21:554/s1")
        assert source.settings == {"transport": "udp", "latencyMs": 500, "decoder": "auto",
                                   "maxFrameDimension": 1920, "stallTimeoutS": 10}
        assert source.credentials == {"username": "viewer", "password": SECRET}
        assert SECRET not in repr(source)

    def test_a_missing_or_non_stream_source_is_not_found(self, database, tmp_path):
        from stream_ingest.credentials import CredentialStore
        from stream_ingest.sources import configured_source

        store = CredentialStore(directory=str(tmp_path / "creds"))
        self._add(database, "f1", "Folder", "/aws_dda/images", None)
        for image_source_id in ("gone", "f1"):
            with pytest.raises(StreamError) as raised:
                configured_source(image_source_id, session_factory=database, credential_store=store)
            assert raised.value.category == health.NOT_FOUND

    def test_an_anonymous_source_is_validated_and_has_no_credentials(self):
        from stream_ingest.sources import anonymous_source

        source = anonymous_source("RTMP", "rtmp://media.local/live/line1", {"decoder": "software"})
        assert (source.protocol, source.url, source.credentials) == ("rtmp", "rtmp://media.local/live/line1", {})
        assert "transport" not in source.settings
        with pytest.raises(StreamError):
            anonymous_source("RTSP", "rtsp://user:pass@host/x")


class TestDeviceSettings:
    @pytest.fixture
    def settings(self, tmp_path):
        from dao.sqlite_db.sqlite_db_operations import Base
        import stream_ingest.models  # noqa: F401
        from stream_ingest.settings import DeviceSettings

        engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
        Base.metadata.create_all(engine)
        clock = ManualClock()
        yield DeviceSettings(session_factory=sessionmaker(bind=engine), clock=clock), clock
        engine.dispose()

    def test_defaults_then_stored_values_then_defaults_again(self, settings):
        from stream_ingest import settings as settings_module

        device, clock = settings
        assert device.limits() == settings_module.default_limits()
        assert device.limits().max_sessions == 4
        device.set(settings_module.MAX_SESSIONS, 6)
        assert device.limits().max_sessions == 6
        device.set(settings_module.MAX_SESSIONS, None)
        assert device.limits().max_sessions == 4

    @pytest.mark.parametrize("value", [0, 17, -1, True, "4", 2.5])
    def test_out_of_range_values_are_refused(self, settings, value):
        from stream_ingest import settings as settings_module

        device, _clock = settings
        with pytest.raises(ValueError):
            device.set(settings_module.MAX_SESSIONS, value)

    def test_a_bad_stored_row_means_the_default(self, settings):
        from stream_ingest import settings as settings_module
        from stream_ingest.models import DeviceSetting

        device, _clock = settings
        with device._sessions() as db:
            db.add(DeviceSetting(key=settings_module.MAX_SESSIONS, value="lots", updated_at=0))
            db.commit()
        assert device.limits().max_sessions == 4

    def test_an_unreadable_database_means_the_defaults(self):
        from stream_ingest.settings import DeviceSettings, default_limits

        def broken():
            raise OSError("database locked")

        assert DeviceSettings(session_factory=broken).limits() == default_limits()


class TestSessionWatchdogAndFallback:
    def test_the_first_worker_gets_the_config_line(self):
        source = rtsp_source(credentials={"username": "viewer", "password": SECRET})
        session, spawner, _clock, _timer = make_session(source=source)
        session.start()
        config = spawner.current.config
        assert config["op"] == "config" and config["protocol"] == "rtsp"
        assert config["credentials"] == {"username": "viewer", "password": SECRET}
        assert config["capabilities"] == CAPABILITIES and config["failedHardware"] is False
        assert spawner.current.secrets == ["viewer", SECRET]

    def test_a_restart_drops_the_frame_of_the_previous_configuration(self):
        session, spawner, _clock, _timer = make_session()
        session.start()
        spawner.current.stream()
        spawner.current.produce(2)
        frame = session.latest_frame()
        assert frame is not None and session.newest_seq() == frame.seq
        session.restart("configuration changed")
        # The new worker has not streamed: nothing is served, not even to a
        # caller that accepts any frame, and sequence numbers keep rising.
        assert session.latest_frame() is None
        assert session.buffers_held() == 0 and session.newest_seq() == frame.seq
        spawner.current.stream()
        spawner.current.produce()
        fresh = session.latest_frame()
        assert fresh is not None and fresh.seq > frame.seq

    def test_a_silent_worker_is_killed_and_retried(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        worker = spawner.current
        clock.advance(HEARTBEAT_TIMEOUT_S + 0.1)
        session.tick()
        assert worker.killed
        assert session.health()["lastError"]["category"] == health.WORKER_EXIT
        assert session.state == health.RECONNECTING

    def test_no_first_frame_is_a_timeout(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        worker = spawner.current
        for _ in range(int(CONNECT_TIMEOUT_S // 2) + 1):
            clock.advance(2.0)
            worker.health("connecting")
            session.tick()
        assert worker.killed
        assert session.health()["lastError"]["category"] == health.TIMEOUT

    def test_a_hardware_decoder_that_takes_data_but_yields_no_frame_falls_back(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        worker = spawner.current
        for _ in range(int(CONNECT_TIMEOUT_S // 2) + 1):
            clock.advance(2.0)
            worker.health("connecting", decoder="hardware", decoderElement="nvv4l2decoder", sourceFrames=90)
            session.tick()
        assert session.health()["lastError"]["category"] == health.HARDWARE_DECODER_FAILED
        session.tick()  # the fallback restarts at once
        assert len(spawner.workers) == 2 and spawner.current.config["failedHardware"] is True
        spawner.current.stream(decoder="software", decoderElement="avdec_h264", decoderFallback=True)
        assert session.health()["decoderFallback"] is True
        session.restart("decoder policy changed")
        assert spawner.current.config["failedHardware"] is False

    def test_a_reported_hardware_failure_restarts_at_once_with_the_flag(self):
        session, spawner, _clock, _timer = make_session()
        session.start()
        spawner.current.stream(decoder="hardware")
        spawner.current.error(health.HARDWARE_DECODER_FAILED, "nvv4l2decoder: decode error")
        session.tick()
        assert len(spawner.workers) == 2 and spawner.current.config["failedHardware"] is True

    def test_the_stall_backstop_catches_a_worker_whose_frames_stop(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        worker = spawner.current
        worker.stream()
        worker.produce()
        worker.health("streaming")
        elapsed = 0.0
        while elapsed <= 10 + STALL_BACKSTOP_MARGIN_S:
            clock.advance(2.0)
            elapsed += 2.0
            worker.health("streaming")  # alive, but the sequence never moves
            session.tick()
        assert worker.killed and session.health()["lastError"]["category"] == health.STALL

    def test_an_unexpected_exit_is_a_transient_worker_exit(self):
        session, spawner, _clock, _timer = make_session()
        session.start()
        spawner.current.stream()
        spawner.current.exit(139)
        assert session.state == health.RECONNECTING
        error = session.health()["lastError"]
        assert error["category"] == health.WORKER_EXIT and "139" in error["message"]

    def test_error_messages_are_redacted_with_the_session_secrets(self):
        source = rtsp_source(credentials={"password": SECRET})
        session, spawner, _clock, _timer = make_session(source=source)
        session.start()
        spawner.current.error(health.AUTHENTICATION_FAILED, f"401 for {SECRET} at rtsp://u:{SECRET}@h/x")
        message = session.health()["lastError"]["message"]
        assert SECRET not in message and "401" in message

    def test_a_worker_that_cannot_start_is_retried(self):
        clock = ManualClock()
        spawner = Spawner(clock, fail_with=OSError("no memory"))
        session, _spawner, _clock, _timer = make_session(clock=clock, spawner=spawner)
        session.start()
        assert session.health()["lastError"]["category"] == health.WORKER_EXIT
        spawner.fail_with = None
        clock.advance(1.0)
        session.tick()
        assert len(spawner.workers) == 1

    def test_a_source_that_cannot_be_resolved_fails_with_its_category(self):
        from stream_ingest.session import StreamSession

        def gone():
            raise StreamError(health.NOT_FOUND, "the stream camera is no longer configured on this device")

        clock = ManualClock()
        spawner = Spawner(clock)
        session = StreamSession("cfg-x", gone, lambda: CAPABILITIES, spawn=spawner, clock=clock,
                                wall_clock=clock.wall, timer=lambda *_: None)
        session.start()
        assert session.state == health.FAILED and spawner.workers == []

    def test_stop_asks_first_and_kills_after_the_grace(self):
        session, spawner, _clock, timer = make_session()
        session.start()
        worker = spawner.current
        session.stop("test")
        assert worker.stopped and not worker.killed
        assert [delay for delay, _ in timer.pending] == [STOP_GRACE_S]
        timer.fire_all()
        assert worker.killed
        assert session.state == health.STOPPED
        session.tick()
        assert len(spawner.workers) == 1

    def test_health_reports_the_documented_fields(self):
        heard = []
        session, spawner, clock, _timer = make_session(on_health=lambda key, doc: heard.append(doc["state"]))
        session.start()
        session.set_leases(3)
        spawner.current.stream(codec="h265", width=1920, height=1080, sourceFps=25.0, frameWidth=1280,
                               frameHeight=720, decoder="software", decoderElement="avdec_h265")
        spawner.current.produce()
        document = session.health()
        for key in ("cameraKey", "state", "codec", "width", "height", "sourceFps", "decoder",
                    "decoderFallback", "reconnects", "lastFrameAtMs", "lastError", "leases"):
            assert key in document
        assert (document["state"], document["codec"], document["width"], document["leases"]) == (
            health.STREAMING, "h265", 1920, 3)
        assert heard == [health.STREAMING]

    def test_a_stale_worker_line_is_ignored(self):
        session, spawner, clock, _timer = make_session()
        session.start()
        first = spawner.current
        first.exit(1)
        clock.advance(1.0)
        session.tick()
        first.on_message(first, {"op": "health", "state": "streaming", "seq": 5})
        assert session.state == health.RECONNECTING
