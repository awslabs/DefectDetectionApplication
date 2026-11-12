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
"""S3 bucket-squatting regression tests for the device-side writers of the
DERIVED inference-results bucket (``workflow_engine.s3_bucket_owner``).

The Portal hands the device ``dda-inference-results-<account>`` (the use
case's default results bucket) through the live lease, the debug session
entry and the tuning configuration; nothing creates that bucket for the
operator, so another account can pre-create the name and receive the
device's frames, images and samples. Every device-side S3 call against a
name of that shape must assert the account in the name as
``ExpectedBucketOwner`` (S3 answers 403 when any other account owns the
bucket), and a bucket of any other name (an override) must carry no
assertion.

The tuning exporter and job runner are covered by their own suites, whose
fakes now refuse an unasserted call (``_assert_owner``); this module covers
the helper itself, the live-frame sender and the debug image uploader, and
proves a refused PUT (the squatted bucket) publishes nothing.

Run on the host with ``--noconftest`` (the workflow_engine lane).
"""
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from workflow_engine.s3_bucket_owner import RESULTS_BUCKET_PATTERN, derived_bucket_owner, owner_args
from workflow_engine.telemetry.live_frames import Lease, LiveFrameSender
from workflow_engine.telemetry.debug.images import ImageUploader

ACCOUNT = "123456789012"
DERIVED = "dda-inference-results-" + ACCOUNT
NOW_MS = 1_792_075_062_000


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bucket, owner", [
    (DERIVED, ACCOUNT),
    ("dda-inference-results-000000000000", "000000000000"),
    ("results-bucket", None),                      # an override: no encoded owner
    ("dda-inference-results-", None),
    ("dda-inference-results-12345", None),         # not an account id
    ("dda-inference-results-123456789012-x", None),
    ("xdda-inference-results-123456789012", None),
    ("dda-component-us-east-1-123456789012", None),
    (None, None),
    ("", None),
])
def test_derived_bucket_owner_is_the_account_the_name_encodes(bucket, owner):
    assert derived_bucket_owner(bucket) == owner
    assert owner_args(bucket) == ({"ExpectedBucketOwner": owner} if owner else {})
    assert bool(RESULTS_BUCKET_PATTERN.match(str(bucket or ""))) == (owner is not None)


# ---------------------------------------------------------------------------
# Live frames
# ---------------------------------------------------------------------------

class SquattableS3:
    """Owned by `owner`; refuses (403) a call whose ExpectedBucketOwner names
    another account, exactly as S3 does. Records every PUT."""

    def __init__(self, owner=ACCOUNT):
        self.owner = owner
        self.puts = []
        self.refused = []

    def put_object(self, **kwargs):
        expected = kwargs.get("ExpectedBucketOwner")
        if expected is not None and expected != self.owner:
            self.refused.append(kwargs)
            raise RuntimeError("An error occurred (AccessDenied) when calling the PutObject operation: Access Denied")
        self.puts.append(kwargs)
        return {}


def _frame(path):
    Image.new("RGB", (640, 480), (40, 90, 160)).save(str(path), format="JPEG", quality=90)


def _run(tmp_path, execution_id="exec-1"):
    output_dir = tmp_path / "out"
    output_dir.mkdir(exist_ok=True)
    capture_id = "wf-" + execution_id
    _frame(output_dir / (capture_id + ".overlay.jpg"))
    return SimpleNamespace(execution_id=execution_id, workflow_id="wf-1", workflow_version="3",
                           status="completed", output_dir=str(output_dir), capture_id=capture_id,
                           detections=None)


def _sender(s3):
    published = []
    sender = LiveFrameSender(
        instance="inst-1", device_id="thing-1",
        publish=lambda topic, payload, qos: published.append((topic, json.loads(payload), qos)),
        topic_for=lambda workflow_id: "dda/telemetry/uc-1/%s/live" % workflow_id,
        person_classes=lambda: ("person",), client_factory=lambda region: s3,
        clock=lambda: NOW_MS / 1000.0, run_thread=False)
    return sender, published


def _lease(bucket):
    return Lease(workflow_id="wf-1", expires_at_ms=NOW_MS + 60_000, max_fps=1, max_width=1280,
                 max_height=720, max_bytes=307200, bucket=bucket, region="eu-west-1",
                 prefix="dda-telemetry/live/uc-1/thing-1/wf-1/")


class TestLiveFrames:
    def test_derived_bucket_put_asserts_the_account_in_its_name(self, tmp_path):
        s3 = SquattableS3(owner=ACCOUNT)
        sender, published = _sender(s3)
        sender.set_leases({"wf-1": _lease(DERIVED)})
        assert sender.on_run_finished(_run(tmp_path)) is True
        assert sender.drain() == 1
        assert len(s3.puts) == 1
        assert s3.puts[0]["Bucket"] == DERIVED
        assert s3.puts[0]["ExpectedBucketOwner"] == ACCOUNT
        assert len(published) == 1

    def test_squatted_derived_bucket_refuses_the_put_and_nothing_is_published(self, tmp_path):
        """The name exists under another account: S3 refuses the owner-
        asserted PUT, the frame is counted as a failure and no live message
        points a viewer at the attacker's object."""
        s3 = SquattableS3(owner="999999999999")
        sender, published = _sender(s3)
        sender.set_leases({"wf-1": _lease(DERIVED)})
        sender.on_run_finished(_run(tmp_path))
        sender.drain()
        assert s3.puts == []
        assert len(s3.refused) == 1 and s3.refused[0]["ExpectedBucketOwner"] == ACCOUNT
        assert published == []
        assert sender.put_failures == 1

    def test_override_bucket_carries_no_assertion(self, tmp_path):
        s3 = SquattableS3(owner="999999999999")  # whoever owns the override: not asserted
        sender, published = _sender(s3)
        sender.set_leases({"wf-1": _lease("customer-results")})
        sender.on_run_finished(_run(tmp_path))
        sender.drain()
        assert len(s3.puts) == 1 and "ExpectedBucketOwner" not in s3.puts[0]
        assert len(published) == 1


# ---------------------------------------------------------------------------
# Debug images
# ---------------------------------------------------------------------------

def _session(bucket):
    return SimpleNamespace(
        entry={"images": True, "imageMaxKb": 150, "bucket": bucket, "region": "eu-west-1",
               "prefix": "dda-telemetry/debug/uc-1/thing-1/wf-1/sess-1/"},
        counters={}, images_sent=0)


def _debug_run(tmp_path, execution_id="exec-1"):
    output_dir = tmp_path / "dbg"
    output_dir.mkdir(exist_ok=True)
    capture_id = "wf-" + execution_id
    _frame(output_dir / (capture_id + ".overlay.jpg"))
    return SimpleNamespace(execution_id=execution_id, output_dir=str(output_dir), capture_id=capture_id)


class TestDebugImages:
    def test_derived_bucket_put_asserts_the_account_in_its_name(self, tmp_path):
        s3 = SquattableS3(owner=ACCOUNT)
        uploader = ImageUploader(client_factory=lambda region: s3, clock=lambda: 1000.0)
        image = uploader.maybe_upload(_session(DERIVED), _debug_run(tmp_path))
        assert image is not None and image["bucket"] == DERIVED
        assert s3.puts[0]["ExpectedBucketOwner"] == ACCOUNT

    def test_squatted_derived_bucket_refuses_the_put_and_the_image_is_null(self, tmp_path):
        s3 = SquattableS3(owner="999999999999")
        uploader = ImageUploader(client_factory=lambda region: s3, clock=lambda: 1000.0)
        session = _session(DERIVED)
        assert uploader.maybe_upload(session, _debug_run(tmp_path)) is None
        assert s3.puts == [] and len(s3.refused) == 1
        assert session.counters.get("imageFailures") == 1

    def test_override_bucket_carries_no_assertion(self, tmp_path):
        s3 = SquattableS3(owner="999999999999")
        uploader = ImageUploader(client_factory=lambda region: s3, clock=lambda: 1000.0)
        image = uploader.maybe_upload(_session("customer-results"), _debug_run(tmp_path))
        assert image is not None
        assert "ExpectedBucketOwner" not in s3.puts[0]
