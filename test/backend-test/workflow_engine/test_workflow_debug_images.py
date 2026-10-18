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
"""The image uploader (workflow-debug-mode design §5.1 ``images.py``, R4.5;
tasks.md 2.7) against a fake S3 client and sample JPEGs.

The region comes from the entry; the key layout is ``<prefix><executionId>.jpg``;
the two-step re-encode; a noise image that stays above 50 KB is not uploaded,
counts ``imagesTooLarge`` and leaves ``image: null``; the per-second and
per-session budgets; a PUT failure leaves ``image: null`` and counts.

Validates: debug R4.5, R12.3.
"""
import io
import random
from types import SimpleNamespace

import pytest
from PIL import Image

from workflow_engine.telemetry.debug import images as images_module
from workflow_engine.telemetry.debug.images import ImageUploader, encode_within_budget

UC = "2f6d3b1a-5c0e-4d7f-9a8b-1c2d3e4f5a6b"
WF = "0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
SESSION = "11111111-1111-4111-8111-111111111111"
PREFIX = "dda-telemetry/debug/%s/thing-1/%s/%s/" % (UC, WF, SESSION)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeS3:
    def __init__(self, fail=False):
        self.puts = []
        self.fail = fail

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(kwargs)


def _flat_jpeg(path, size=(1920, 1080), color=(40, 90, 160)):
    Image.new("RGB", size, color).save(path, format="JPEG", quality=90)


def _noise_jpeg(path, size=(1920, 1080), seed=7):
    rng = random.Random(seed)
    image = Image.frombytes("RGB", size, bytes(rng.getrandbits(8) for _ in range(size[0] * size[1] * 3)))
    image.save(path, format="JPEG", quality=95)


def _session(images=True, image_max_kb=150, sent=0):
    return SimpleNamespace(
        entry={"images": images, "imageMaxKb": image_max_kb, "bucket": "results-bucket",
               "region": "eu-west-1", "prefix": PREFIX},
        counters={}, images_sent=sent,
    )


def _run(tmp_path, execution_id="exec-1", overlay=True, base=True, noise=False):
    output_dir = tmp_path / "out"
    output_dir.mkdir(exist_ok=True)
    capture_id = "wf-" + execution_id
    maker = _noise_jpeg if noise else _flat_jpeg
    if overlay:
        maker(str(output_dir / (capture_id + ".overlay.jpg")))
    if base:
        _flat_jpeg(str(output_dir / (capture_id + ".jpg")), color=(200, 20, 20))
    return SimpleNamespace(execution_id=execution_id, output_dir=str(output_dir), capture_id=capture_id)


@pytest.fixture
def uploader():
    clients = {}
    s3 = FakeS3()

    def factory(region):
        clients[region] = clients.get(region, 0) + 1
        return s3

    up = ImageUploader(client_factory=factory, clock=FakeClock())
    up.test_s3 = s3
    up.test_clients = clients
    return up


class TestUpload:
    def test_overlay_is_preferred_downscaled_and_put_with_the_entry_region(self, uploader, tmp_path):
        session = _session()
        run = _run(tmp_path)
        image = uploader.maybe_upload(session, run)
        assert image is not None
        assert image["bucket"] == "results-bucket"
        assert image["key"] == PREFIX + "exec-1.jpg"
        assert image["width"] == 960 and image["height"] == 540
        assert 0 < image["bytes"] <= 150 * 1024
        put = uploader.test_s3.puts[0]
        assert put["Bucket"] == "results-bucket" and put["Key"] == image["key"]
        assert put["ContentType"] == "image/jpeg"
        assert len(put["Body"]) == image["bytes"]
        assert uploader.test_clients == {"eu-west-1": 1}
        # The overlay (blue), not the base (red).
        with Image.open(io.BytesIO(put["Body"])) as sent:
            r, g, b = sent.getpixel((10, 10))
        assert b > r
        assert session.images_sent == 1 and session.counters["imagesSent"] == 1

    def test_base_image_when_no_overlay_exists(self, uploader, tmp_path):
        image = uploader.maybe_upload(_session(), _run(tmp_path, overlay=False))
        assert image is not None
        with Image.open(io.BytesIO(uploader.test_s3.puts[0]["Body"])) as sent:
            r, g, b = sent.getpixel((10, 10))
        assert r > b

    def test_no_frame_on_disk_means_null_without_a_put(self, uploader, tmp_path):
        assert uploader.maybe_upload(_session(), _run(tmp_path, overlay=False, base=False)) is None
        assert uploader.test_s3.puts == []

    def test_images_off_uploads_nothing(self, uploader, tmp_path):
        session = _session(images=False)
        assert uploader.maybe_upload(session, _run(tmp_path)) is None
        assert uploader.test_s3.puts == [] and session.counters == {}

    def test_one_client_per_bucket_and_region(self, uploader, tmp_path):
        uploader.maybe_upload(_session(), _run(tmp_path, "exec-1"))
        uploader._clock.now += 1
        uploader.maybe_upload(_session(), _run(tmp_path, "exec-2"))
        assert uploader.test_clients == {"eu-west-1": 1}


class TestEncoding:
    def test_two_step_re_encode_75_then_55(self, tmp_path):
        path = tmp_path / "n.jpg"
        _noise_jpeg(str(path), size=(960, 540))
        data = path.read_bytes()
        at_75 = encode_within_budget(data, 10 * 1024 * 1024)
        assert at_75 is not None
        at_55 = encode_within_budget(data, len(at_75[0]) - 1)  # the 75 encoding is too big
        assert at_55 is not None
        assert len(at_55[0]) < len(at_75[0])
        assert encode_within_budget(data, 1000) is None

    def test_small_images_are_not_upscaled(self, tmp_path):
        path = tmp_path / "s.jpg"
        _flat_jpeg(str(path), size=(320, 240))
        encoded, width, height = encode_within_budget(path.read_bytes(), 150 * 1024)
        assert (width, height) == (320, 240)

    def test_noise_image_above_50_kb_is_not_uploaded_counted_too_large(self, uploader, tmp_path):
        session = _session(image_max_kb=50)
        image = uploader.maybe_upload(session, _run(tmp_path, noise=True))
        assert image is None
        assert uploader.test_s3.puts == []
        assert session.counters.get("imagesTooLarge") == 1
        assert session.images_sent == 0


class TestBudgetsAndFailures:
    def test_per_second_budget_of_two(self, uploader, tmp_path):
        session = _session()
        results = [uploader.maybe_upload(session, _run(tmp_path, "exec-%d" % i)) for i in range(3)]
        assert results[0] is not None and results[1] is not None and results[2] is None
        assert session.counters["imagesCapped"] == 1
        uploader._clock.now += 1.0
        assert uploader.maybe_upload(session, _run(tmp_path, "exec-9")) is not None
        assert len(uploader.test_s3.puts) == 3

    def test_per_session_budget_of_2000(self, uploader, tmp_path):
        session = _session(sent=images_module.PER_SESSION_BUDGET)
        assert uploader.maybe_upload(session, _run(tmp_path)) is None
        assert session.counters["imagesCapped"] == 1
        assert uploader.test_s3.puts == []

    def test_put_failure_leaves_null_and_counts(self, tmp_path):
        s3 = FakeS3(fail=True)
        uploader = ImageUploader(client_factory=lambda region: s3, clock=FakeClock())
        session = _session()
        assert uploader.maybe_upload(session, _run(tmp_path)) is None
        assert uploader.failures == 1
        assert session.counters.get("imageFailures") == 1
        assert session.images_sent == 0

    def test_undecodable_frame_is_counted_and_null(self, tmp_path):
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        (output_dir / "wf-exec-1.overlay.jpg").write_bytes(b"not a jpeg")
        uploader = ImageUploader(client_factory=lambda region: FakeS3(), clock=FakeClock())
        run = SimpleNamespace(execution_id="exec-1", output_dir=str(output_dir), capture_id="wf-exec-1")
        assert uploader.maybe_upload(_session(), run) is None
        assert uploader.failures == 1

    def test_default_client_factory_builds_a_regional_s3_client(self, monkeypatch):
        import boto3

        created = {}

        def fake_client(service, region_name=None, config=None):
            created["service"] = service
            created["region"] = region_name
            created["config"] = config
            return object()

        monkeypatch.setattr(boto3, "client", fake_client)
        images_module._default_client_factory("us-east-1")
        assert created["service"] == "s3" and created["region"] == "us-east-1"
        assert created["config"].connect_timeout == 5 and created["config"].read_timeout == 5
