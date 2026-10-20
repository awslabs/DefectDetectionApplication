# Copyright 2025 Amazon Web Services, Inc.
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
"""The live-frame sender (workflow-telemetry-dashboards design §4.3, §5.2
``live_frames.py``, §9.3; tasks.md 1.3) against a fake S3 client and sample
JPEGs: slot arithmetic; downscale limits; the 80-then-60 re-encode; a frame
still too large is skipped and counted ``framesTooLarge``; upload-then-publish
ordering; the ``maxFps`` gate; lease lapse by expiry and by removal; a failed
PUT publishes nothing; ``seq`` restarts at 0 with a new sender instance.

Validates: R10.2, R15.3, R19.4.
"""
import json
import random
from types import SimpleNamespace

import pytest
from PIL import Image

from workflow_engine.telemetry import live_frames as live_module
from workflow_engine.telemetry.live_frames import QUALITIES, SLOTS, Lease, LiveFrameSender, encode_frame

THING = "thing-1"
UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
WF_OTHER = "1b2c3d4e-5f6a-4b7c-9d0e-1f2a3b4c5d6e"
INSTANCE = "7c1e7d4e-3b2a-4c9d-8e1f-0a2b3c4d5e6f"
PREFIX = "dda-telemetry/live/%s/%s/%s/" % (UC, THING, WF)
TOPIC = "dda/telemetry/%s/%s/live" % (UC, WF)
NOW_MS = 1_792_075_062_000


class FakeClock:
    def __init__(self):
        self.now = NOW_MS / 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeS3:
    def __init__(self, fail=False):
        self.puts = []
        self.fail = fail
        self.log = None

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(kwargs)
        if self.log is not None:
            self.log.append(("put", kwargs["Key"]))


def _flat_jpeg(path, size=(1920, 1080), color=(40, 90, 160)):
    Image.new("RGB", size, color).save(path, format="JPEG", quality=90)


def _noise_jpeg(path, size=(1920, 1080), seed=7):
    rng = random.Random(seed)
    image = Image.frombytes("RGB", size, bytes(rng.getrandbits(8) for _ in range(size[0] * size[1] * 3)))
    image.save(path, format="JPEG", quality=95)


def lease(workflow_id=WF, expires_at=NOW_MS + 60_000, max_fps=1, max_width=1280, max_height=720,
          max_bytes=307200, bucket="results-bucket", region="eu-west-1", prefix=PREFIX):
    return Lease(workflow_id=workflow_id, expires_at_ms=expires_at, max_fps=max_fps, max_width=max_width,
                 max_height=max_height, max_bytes=max_bytes, bucket=bucket, region=region, prefix=prefix)


def run(tmp_path, execution_id="exec-1", workflow_id=WF, overlay=True, base=True, noise=False, status="completed",
        detections=None):
    output_dir = tmp_path / "out"
    output_dir.mkdir(exist_ok=True)
    capture_id = "wf-" + execution_id
    maker = _noise_jpeg if noise else _flat_jpeg
    if overlay:
        maker(str(output_dir / (capture_id + ".overlay.jpg")))
    if base:
        _flat_jpeg(str(output_dir / (capture_id + ".jpg")), color=(200, 20, 20))
    return SimpleNamespace(execution_id=execution_id, workflow_id=workflow_id, workflow_version="3", status=status,
                           output_dir=str(output_dir), capture_id=capture_id, detections=detections)


class World:
    def __init__(self, instance=INSTANCE):
        self.clock = FakeClock()
        self.s3 = FakeS3()
        self.log = []
        self.s3.log = self.log
        self.published = []
        self.clients = {}
        self.topic_for_calls = []
        self.usecase_id = UC

        def factory(region):
            self.clients[region] = self.clients.get(region, 0) + 1
            return self.s3

        def publish(topic, payload, qos):
            self.log.append(("publish", topic))
            self.published.append((topic, json.loads(payload), qos))

        def topic_for(workflow_id):
            self.topic_for_calls.append(workflow_id)
            if self.usecase_id is None:
                return None
            return "dda/telemetry/%s/%s/live" % (self.usecase_id, workflow_id)

        self.sender = LiveFrameSender(instance=instance, device_id=THING, publish=publish, topic_for=topic_for,
                                      person_classes=lambda: ("person", "human"), client_factory=factory,
                                      clock=self.clock, run_thread=False)


@pytest.fixture
def world():
    return World()


class TestUploadAndPublish:
    def test_overlay_is_downscaled_put_with_metadata_then_published(self, world, tmp_path):
        world.sender.set_leases({WF: lease()})
        assert world.sender.on_run_finished(run(tmp_path, detections=[{"label": "person"}, {"label": "person"},
                                                                       {"label": "hat"}])) is True
        assert world.sender.drain() == 1
        assert len(world.s3.puts) == 1
        put = world.s3.puts[0]
        assert put["Bucket"] == "results-bucket" and put["Key"] == PREFIX + "0.jpg"
        assert put["ContentType"] == "image/jpeg" and put["Metadata"] == {"seq": "0"}
        with Image.open(__import__("io").BytesIO(put["Body"])) as image:
            assert image.size == (1280, 720)
            r, g, b = image.getpixel((10, 10))
            assert b > r  # the overlay's colour, not the red base image
        assert world.clients == {"eu-west-1": 1}
        assert len(world.published) == 1
        topic, payload, qos = world.published[0]
        assert topic == TOPIC and qos == 0
        assert payload["schema"] == "dda.live.v1" and payload["instance"] == INSTANCE and payload["seq"] == 0
        assert payload["deviceId"] == THING and payload["workflowId"] == WF and payload["workflowVersion"] == "3"
        assert payload["frame"] == {"bucket": "results-bucket", "key": PREFIX + "0.jpg", "width": 1280,
                                    "height": 720, "bytes": len(put["Body"])}
        assert payload["frame"]["bytes"] <= 307200
        assert payload["perClass"] == {"person": 2, "hat": 1} and payload["people"] == 2
        assert payload["ts"] == NOW_MS
        assert "violation" not in payload and "reasons" not in payload  # the engine knows no PPE rules
        # upload-then-publish ordering
        assert world.log == [("put", PREFIX + "0.jpg"), ("publish", TOPIC)]
        assert world.sender.live_status() == {WF: {"activeUntil": NOW_MS + 60_000, "lastSeq": 0,
                                                   "framesTooLarge": 0}}

    def test_base_image_when_no_overlay_exists_and_nothing_without_a_frame(self, world, tmp_path):
        world.sender.set_leases({WF: lease()})
        world.sender.on_run_finished(run(tmp_path, overlay=False))
        assert world.sender.drain() == 1
        with Image.open(__import__("io").BytesIO(world.s3.puts[0]["Body"])) as image:
            r, g, b = image.getpixel((10, 10))
            assert r > b  # the red base image
        world.clock.advance(2)
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        world.sender.on_run_finished(run(empty_dir, "exec-2", overlay=False, base=False))
        assert world.sender.drain() == 0
        assert len(world.s3.puts) == 1 and len(world.published) == 1

    def test_slot_arithmetic_wraps_at_16_and_seq_keeps_counting(self, world, tmp_path):
        world.sender.set_leases({WF: lease(max_fps=2)})
        for i in range(18):
            assert world.sender.on_run_finished(run(tmp_path, "exec-%d" % i)) is True
            world.sender.drain()
            world.clock.advance(0.5)
        keys = [p["Key"] for p in world.s3.puts]
        assert keys[:3] == [PREFIX + "0.jpg", PREFIX + "1.jpg", PREFIX + "2.jpg"]
        assert keys[15] == PREFIX + "15.jpg" and keys[16] == PREFIX + "0.jpg" and keys[17] == PREFIX + "1.jpg"
        assert [p["Metadata"]["seq"] for p in world.s3.puts] == [str(i) for i in range(18)]
        assert [p[1]["seq"] for p in world.published] == list(range(18))
        assert all(p[1]["frame"]["key"] == PREFIX + "%d.jpg" % (p[1]["seq"] % SLOTS) for p in world.published)
        assert world.sender.live_status()[WF]["lastSeq"] == 17

    def test_seq_restarts_at_zero_with_a_new_sender_instance(self, world, tmp_path):
        world.sender.set_leases({WF: lease()})
        world.sender.on_run_finished(run(tmp_path))
        world.sender.drain()
        world.clock.advance(1)
        world.sender.on_run_finished(run(tmp_path, "exec-2"))
        world.sender.drain()
        assert [p[1]["seq"] for p in world.published] == [0, 1]
        restarted = World(instance="11111111-2222-4333-8444-5a5b5c5d5e5f")
        restarted.sender.set_leases({WF: lease()})
        restarted.sender.on_run_finished(run(tmp_path, "exec-3"))
        restarted.sender.drain()
        assert [(p[1]["seq"], p[1]["instance"]) for p in restarted.published] == [
            (0, "11111111-2222-4333-8444-5a5b5c5d5e5f")]
        assert restarted.s3.puts[0]["Key"] == PREFIX + "0.jpg"

    def test_one_client_per_bucket_and_region(self, world, tmp_path):
        other_prefix = "dda-telemetry/live/%s/%s/%s/" % (UC, THING, WF_OTHER)
        world.sender.set_leases({WF: lease(), WF_OTHER: lease(workflow_id=WF_OTHER, prefix=other_prefix,
                                                             region="us-east-1", bucket="other")})
        world.sender.on_run_finished(run(tmp_path, "exec-1"))
        world.sender.on_run_finished(run(tmp_path, "exec-2", workflow_id=WF_OTHER))
        world.sender.drain()
        world.clock.advance(2)
        world.sender.on_run_finished(run(tmp_path, "exec-3"))
        world.sender.drain()
        assert world.clients == {"eu-west-1": 1, "us-east-1": 1}
        assert world.s3.puts[1]["Key"] == other_prefix + "0.jpg" and world.s3.puts[1]["Bucket"] == "other"


class TestEncoding:
    def test_downscale_limits_and_no_upscale(self, tmp_path):
        big = tmp_path / "big.jpg"
        _flat_jpeg(str(big), size=(1920, 1080))
        encoded, width, height = encode_frame(big.read_bytes(), 1280, 720, 307200)
        assert (width, height) == (1280, 720)
        encoded, width, height = encode_frame(big.read_bytes(), 640, 640, 307200)
        assert (width, height) == (640, 360)
        small = tmp_path / "small.jpg"
        _flat_jpeg(str(small), size=(320, 240))
        encoded, width, height = encode_frame(small.read_bytes(), 1280, 720, 307200)
        assert (width, height) == (320, 240)
        with Image.open(__import__("io").BytesIO(encoded)) as image:
            assert image.format == "JPEG"

    def test_re_encode_at_80_then_60(self, tmp_path, monkeypatch):
        path = tmp_path / "n.jpg"
        _noise_jpeg(str(path), size=(640, 360))
        data = path.read_bytes()
        assert QUALITIES == (80, 60)
        qualities_used = []
        from PIL import Image as PilImage

        original_save = PilImage.Image.save

        def recording_save(self, fp, format=None, **params):
            qualities_used.append(params.get("quality"))
            return original_save(self, fp, format=format, **params)

        monkeypatch.setattr(PilImage.Image, "save", recording_save)
        generous = encode_frame(data, 1280, 720, 10 ** 7)
        assert qualities_used == [80]
        size_80 = len(generous[0])
        qualities_used.clear()
        tighter = encode_frame(data, 1280, 720, size_80 - 1)
        assert qualities_used == [80, 60]
        assert tighter is not None and len(tighter[0]) < size_80
        qualities_used.clear()
        assert encode_frame(data, 1280, 720, 100) is None
        assert qualities_used == [80, 60]

    def test_a_frame_still_too_large_is_skipped_and_counted(self, world, tmp_path):
        world.sender.set_leases({WF: lease(max_bytes=2000)})
        world.sender.on_run_finished(run(tmp_path, noise=True))
        assert world.sender.drain() == 0
        assert world.s3.puts == [] and world.published == []
        assert world.sender.frames_too_large == 1
        assert world.sender.live_status()[WF]["framesTooLarge"] == 1
        assert world.sender.status()["framesTooLarge"] == 1
        # the next frame is still accepted: nothing is stuck
        world.clock.advance(2)
        world.sender.set_leases({WF: lease(max_bytes=307200)})
        world.sender.on_run_finished(run(tmp_path, "exec-2"))
        assert world.sender.drain() == 1

    def test_an_undecodable_frame_is_counted_and_publishes_nothing(self, world, tmp_path):
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        (output_dir / "wf-exec-1.overlay.jpg").write_bytes(b"not a jpeg")
        record = SimpleNamespace(execution_id="exec-1", workflow_id=WF, workflow_version="3", status="completed",
                                 output_dir=str(output_dir), capture_id="wf-exec-1", detections=None)
        world.sender.set_leases({WF: lease()})
        world.sender.on_run_finished(record)
        assert world.sender.drain() == 0 and world.published == [] and world.sender.put_failures == 1


class TestGatesAndLeases:
    def test_max_fps_gate(self, world, tmp_path):
        world.sender.set_leases({WF: lease(max_fps=1)})
        assert world.sender.on_run_finished(run(tmp_path, "e1")) is True
        world.clock.advance(0.4)
        assert world.sender.on_run_finished(run(tmp_path, "e2")) is False  # 400 ms later: over 1 fps
        world.clock.advance(0.7)
        assert world.sender.on_run_finished(run(tmp_path, "e3")) is True  # 1.1 s after the first
        assert world.sender.frames_skipped == 1
        assert world.sender.drain() == 2
        assert [p["Metadata"]["seq"] for p in world.s3.puts] == ["0", "1"]

    def test_only_completed_runs_of_a_leased_pair_are_queued(self, world, tmp_path):
        world.sender.set_leases({WF: lease()})
        assert world.sender.on_run_finished(run(tmp_path, "e1", status="failed")) is False
        assert world.sender.on_run_finished(run(tmp_path, "e2", workflow_id=WF_OTHER)) is False
        assert world.sender.drain() == 0

    def test_lease_lapse_by_expiry(self, world, tmp_path):
        world.sender.set_leases({WF: lease(expires_at=NOW_MS + 5_000)})
        assert world.sender.on_run_finished(run(tmp_path, "e1")) is True
        world.sender.drain()
        world.clock.advance(5.0)  # now == expiresAt: no longer active
        assert world.sender.on_run_finished(run(tmp_path, "e2")) is False
        world.sender.tick()
        assert world.sender.leases() == {} and world.sender.live_status() == {}
        assert len(world.published) == 1

    def test_a_queued_frame_is_dropped_when_the_lease_lapses_before_the_upload(self, world, tmp_path):
        world.sender.set_leases({WF: lease(expires_at=NOW_MS + 5_000)})
        assert world.sender.on_run_finished(run(tmp_path, "e1")) is True
        world.clock.advance(6)
        assert world.sender.drain() == 0
        assert world.s3.puts == [] and world.published == []

    def test_lease_lapse_by_removal_from_the_complete_document(self, world, tmp_path):
        other_prefix = "dda-telemetry/live/%s/%s/%s/" % (UC, THING, WF_OTHER)
        world.sender.set_leases({WF: lease(), WF_OTHER: lease(workflow_id=WF_OTHER, prefix=other_prefix)})
        world.sender.on_run_finished(run(tmp_path, "e1"))
        world.sender.drain()
        world.sender.set_leases({WF_OTHER: lease(workflow_id=WF_OTHER, prefix=other_prefix)})  # WF removed
        world.clock.advance(2)
        assert world.sender.on_run_finished(run(tmp_path, "e2")) is False
        assert world.sender.on_run_finished(run(tmp_path, "e3", workflow_id=WF_OTHER)) is True
        world.sender.drain()
        assert [p[1]["workflowId"] for p in world.published] == [WF, WF_OTHER]
        assert sorted(world.sender.live_status()) == [WF_OTHER]
        # the removed Pair starts a fresh rate window when leased again
        world.sender.set_leases({WF: lease()})
        assert world.sender.on_run_finished(run(tmp_path, "e4")) is True

    def test_a_failed_put_publishes_nothing_and_is_counted(self, world, tmp_path):
        world.s3.fail = True
        world.sender.set_leases({WF: lease()})
        world.sender.on_run_finished(run(tmp_path))
        assert world.sender.drain() == 0
        assert world.published == [] and world.sender.put_failures == 1
        assert world.sender.live_status()[WF]["lastSeq"] is None
        # the seq is not consumed by a failed PUT
        world.s3.fail = False
        world.clock.advance(2)
        world.sender.on_run_finished(run(tmp_path, "exec-2"))
        world.sender.drain()
        assert world.s3.puts[0]["Metadata"] == {"seq": "0"}

    def test_nothing_is_published_without_a_topic(self, world, tmp_path):
        world.usecase_id = None  # the agent has no use case id
        world.sender.set_leases({WF: lease()})
        world.sender.on_run_finished(run(tmp_path))
        assert world.sender.drain() == 0 and world.s3.puts == [] and world.published == []

    def test_newest_frames_supersede_the_queue(self, world, tmp_path):
        world.sender.set_leases({WF: lease(max_fps=2)})
        for i in range(6):
            world.sender.on_run_finished(run(tmp_path, "e%d" % i))
            world.clock.advance(0.5)
        assert world.sender.queue_drops == 2
        assert world.sender.drain() == 4

    def test_status_shape(self, world):
        status = world.sender.status()
        assert status == {"leases": [], "framesSent": 0, "framesTooLarge": 0, "putFailures": 0,
                          "framesSkipped": 0, "queueDrops": 0, "queued": 0, "threadAlive": False}

    def test_default_client_factory_builds_a_regional_s3_client_with_timeouts(self, monkeypatch):
        import boto3

        calls = []

        def fake_client(service, region_name=None, config=None):
            calls.append((service, region_name, config))
            return "client"

        monkeypatch.setattr(boto3, "client", fake_client)
        assert live_module._default_client_factory("ap-southeast-2") == "client"
        service, region, config = calls[0]
        assert service == "s3" and region == "ap-southeast-2"
        assert config.connect_timeout == 5 and config.read_timeout == 5

    def test_the_real_thread_sends_and_stops(self, tmp_path):
        import threading
        import time

        done = threading.Event()
        published = []

        def publish(topic, payload, qos):
            published.append(topic)
            done.set()

        s3 = FakeS3()
        sender = LiveFrameSender(instance=INSTANCE, device_id=THING, publish=publish, topic_for=lambda wf: TOPIC,
                                 client_factory=lambda region: s3)
        try:
            sender.set_leases({WF: lease(expires_at=int(time.time() * 1000) + 60_000)})
            assert sender.on_run_finished(run(tmp_path)) is True
            assert done.wait(10.0)
            assert published == [TOPIC] and len(s3.puts) == 1
            assert sender._thread.name == "telemetry-live-frames" and sender._thread.daemon
        finally:
            sender.stop()
            sender._thread.join(5.0)
        assert not sender._thread.is_alive()
