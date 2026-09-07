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
"""Property test: a failing application retains the prior Pinned_Image.

# Feature: cloud-static-camera-provisioning, Property 11: Failure retains
# the prior image

*For any* prior pinned state and any failing application (checksum-valid
but undecodable bytes, storage failure, or a failing replacement or
removal), the Pinned_Image content, the pin-status output, and
``get_frame`` results are unchanged from before the attempt, and the
reported document carries ``failed`` with a descriptive reason.

**Validates: Requirements 3.4, 7.8**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import random
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import StaticImagePinError, StaticImageStore

from pin_worker_support import (
    FakeClock,
    FakeS3Client,
    FakeShadowAccessor,
    FakeSleep,
    TEST_BUCKET,
    disk_state,
    fresh_store_dirs,
    image_specs,
    make_worker,
    observable_state,
    pin_desired,
    remove_desired,
    render_image_bytes,
)

# Failure modes (task 6.3): checksum-valid undecodable bytes, storage
# failure during a pin/replace, and a failing removal.
_failure_modes = st.sampled_from(["undecodable", "storage", "removal"])


class _StorageFailingStore(StaticImageStore):
    """A real store whose persistence layer fails: ``pin_bytes`` surfaces
    the store's own descriptive StaticImagePinError."""

    def _atomic_write(self, dest_path, payload):
        raise OSError("injected disk failure")


class _RemovalFailingStore(StaticImageStore):
    """A real store whose unpin fails for a reason other than "nothing is
    pinned" (which the worker must NOT map to a no-op success)."""

    def unpin(self):
        raise StaticImagePinError(
            "Failed to remove the pinned image: injected disk failure"
        )


def _undecodable_bytes(seed):
    """Checksum-valid (any bytes are), never a decodable image: no image
    container starts with this prefix."""
    return b"\x00UNDECODABLE\x00" + random.Random(seed).randbytes(24)


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    mode=_failure_modes,
    payload_spec=image_specs,
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_failure_retains_the_prior_image(prior_spec, mode, payload_spec, seed):
    """# Feature: cloud-static-camera-provisioning, Property 11: Failure
    retains the prior image

    **Validates: Requirements 3.4, 7.8**
    """
    tmp_dir = tempfile.mkdtemp(prefix="pin-worker-retention-")
    try:
        store_dir, marker_path = fresh_store_dirs(tmp_dir)
        # Prior state is established through a plain store over the same
        # directory; the worker gets the failing variant.
        plain_store = StaticImageStore(base_dir=store_dir)
        if prior_spec is not None:
            plain_store.pin_bytes(render_image_bytes(*prior_spec), "prior.img")
        prior_disk = disk_state(store_dir)
        prior_state = observable_state(plain_store)

        if mode == "undecodable":
            worker_store = StaticImageStore(base_dir=store_dir)
            data = _undecodable_bytes(seed)
            desired = pin_desired("req-fail-1", data)
            expected_reason_part = "could not be decoded"
        elif mode == "storage":
            worker_store = _StorageFailingStore(base_dir=store_dir)
            data = render_image_bytes(*payload_spec)
            desired = pin_desired("req-fail-1", data)
            expected_reason_part = "Failed to store"
        else:  # failing removal
            worker_store = _RemovalFailingStore(base_dir=store_dir)
            data = None
            desired = remove_desired("req-fail-1")
            expected_reason_part = "Failed to remove"

        clock = FakeClock()
        sleep = FakeSleep(clock)
        shadow = FakeShadowAccessor()
        objects = (
            {(TEST_BUCKET, desired["key"]): data} if data is not None else {}
        )
        s3 = FakeS3Client(objects)
        worker = make_worker(worker_store, shadow, s3, clock, sleep, marker_path)

        report = worker.process_one(desired)

        # The reported document carries `failed` with a descriptive reason
        # (3.4, 7.8).
        assert report["status"] == "failed"
        assert expected_reason_part in report["reason"]
        assert shadow.pin_reports[-1]["status"] == "failed"

        # Pinned content, pin status, and get_frame are unchanged —
        # byte-for-byte on disk and through every observable (3.4, 7.8).
        assert disk_state(store_dir) == prior_disk
        assert observable_state(plain_store) == prior_state
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
