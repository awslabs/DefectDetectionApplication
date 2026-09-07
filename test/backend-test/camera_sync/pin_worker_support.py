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
"""Shared fakes, strategies, and oracles for the StaticImagePinWorker
property suites (feature: cloud-static-camera-provisioning).

Mirrors the conventions of the camera_sync suites (injectable fake clock /
shadow accessor driven deterministically, no threads) and of the
static_image_camera suites (Pillow-generated valid images over a temp-dir
:class:`~utils.static_image_camera.StaticImageStore`).
"""
import hashlib
import io
import os
import random

from hypothesis import strategies as st
from PIL import Image

from camera_sync.pin_worker import StaticImagePinWorker
from utils.static_image_camera import StaticImageStore

FORMATS = ("JPEG", "PNG", "BMP")

#: (width, height, pixel_seed, format) — a full description of a generated
#: valid image (the static_image_camera suites' convention). Dimensions
#: stay small so 100-example runs stay fast; the store is
#: dimension-agnostic.
image_specs = st.tuples(
    st.integers(min_value=1, max_value=32),
    st.integers(min_value=1, max_value=32),
    st.integers(min_value=0, max_value=2**32 - 1),
    st.sampled_from(FORMATS),
)


def render_image_bytes(width, height, seed, img_format):
    """Encode a random-pixel RGB image of the given dimensions/format."""
    pixels = random.Random(seed).randbytes(width * height * 3)
    image = Image.frombytes("RGB", (width, height), pixels)
    buffer = io.BytesIO()
    image.save(buffer, format=img_format)
    return buffer.getvalue()


# --- desired-document builders -------------------------------------------------

TEST_BUCKET = "test-component-bucket"


def pin_desired(request_id, data, file_name="sample.img"):
    """A ``desired.staticImagePin`` pin document referencing ``data`` with
    its true sha256 (design section 3 document shape)."""
    return {
        "requestId": request_id,
        "op": "pin",
        "bucket": TEST_BUCKET,
        "key": "static-image-pins/test-device/{}".format(request_id),
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
        "format": "IMG",
        "fileName": file_name,
        "requestedAtEpochMs": 1_730_000_000_000,
    }


def remove_desired(request_id):
    """A removal document: only requestId, op, requestedAtEpochMs."""
    return {
        "requestId": request_id,
        "op": "remove",
        "requestedAtEpochMs": 1_730_000_000_000,
    }


# --- fakes ---------------------------------------------------------------------


class FakeClock:
    """Injectable monotonic clock advanced explicitly (or by FakeSleep)."""

    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSleep:
    """Records every requested sleep and advances the fake clock."""

    def __init__(self, clock):
        self.calls = []
        self._clock = clock

    def __call__(self, seconds):
        self.calls.append(seconds)
        self._clock.advance(seconds)


class FakeShadowAccessor:
    """Records reported writes; optionally appends ordering events."""

    def __init__(self, events=None):
        self.writes = []
        self._events = events

    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        self.writes.append(state)
        if self._events is not None:
            self._events.append(("reported_write", state))

    @property
    def pin_reports(self):
        """Every ``reported.staticImagePin`` document written, in order."""
        return [
            state["reported"]["staticImagePin"]
            for state in self.writes
            if isinstance(state.get("reported"), dict)
            and "staticImagePin" in state["reported"]
        ]


class Body:
    """A chunked S3 GetObject body over in-memory bytes."""

    def __init__(self, data):
        self._buffer = io.BytesIO(data)

    def read(self, n=-1):
        return self._buffer.read(n)


class FakeS3Client:
    """objects: {(bucket, key): bytes}; records every get_object call."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": Body(self.objects[(Bucket, Key)])}


class RecordingStore:
    """Counts the store's mutating operations, delegating to a real store."""

    def __init__(self, store):
        self.store = store
        self.pin_calls = []
        self.unpin_calls = 0

    def pin_bytes(self, data, file_name):
        result = self.store.pin_bytes(data, file_name)
        self.pin_calls.append((bytes(data), file_name))
        return result

    def unpin(self):
        self.unpin_calls += 1
        return self.store.unpin()

    def __getattr__(self, name):
        return getattr(self.store, name)


# --- worker/state helpers -------------------------------------------------------


def make_worker(store, shadow, s3_client, clock, sleep, marker_path):
    """A worker with every collaborator injected — no env, no threads."""
    return StaticImagePinWorker(
        iot_shadow_accessor=shadow,
        thing_name="test-thing",
        shadow_name="dda-camera-registry",
        store_factory=lambda: store,
        s3_client_factory=lambda: s3_client,
        marker_path=marker_path,
        clock=clock,
        sleep=sleep,
    )


def observable_state(store):
    """The Device_Pin_API-observable pin state: status output plus the
    frame grab result (``None`` when no usable Pinned_Image exists)."""
    state = {"status": store.status(), "is_pinned": store.is_pinned()}
    try:
        frame = store.get_frame()
        state["frame"] = (
            frame["data"],
            frame["width"],
            frame["height"],
            frame["pixel_format"],
        )
    except Exception:
        state["frame"] = None
    return state


def disk_state(base_dir):
    """Byte-exact snapshot of the store directory's regular files."""
    files = {}
    if os.path.isdir(base_dir):
        for name in sorted(os.listdir(base_dir)):
            path = os.path.join(base_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    files[name] = handle.read()
    return files


def fresh_store_dirs(tmp_dir):
    """(store_base_dir, marker_path) laid out under one temp dir, keeping
    the marker outside the store dir so disk_state snapshots only the
    store's own files."""
    store_dir = os.path.join(tmp_dir, "store")
    marker_path = os.path.join(tmp_dir, "applied_pin_request.json")
    return store_dir, marker_path
