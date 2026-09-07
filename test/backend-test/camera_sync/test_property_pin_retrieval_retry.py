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
"""Property test for the pin worker's retrieval verification and retry.

# Feature: cloud-static-camera-provisioning, Property 9: Retrieval
# verification and retry

*For any* served byte sequence, declared Content_Checksum, and injected
failure pattern (timeouts, errors, mismatched bytes): the worker applies a
pin only when retrieved bytes match the checksum; every timed-out or
mismatched attempt discards its bytes and counts as one failed attempt;
the worker makes at most 3 attempts with at least 5 seconds between
consecutive attempts; and when all 3 attempts fail, the pin store is never
invoked (the prior Pinned_Image state is byte-identical) and the reported
Sync_Status is ``failed`` with a reason identifying the final attempt's
cause as retrieval failure or checksum mismatch.

**Validates: Requirements 2.8, 2.9, 2.10, 2.11, 5.5**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from camera_sync.pin_worker import (
    RETRIEVAL_ATTEMPT_TIMEOUT_SECONDS,
    RETRIEVAL_MAX_ATTEMPTS,
    RETRIEVAL_RETRY_SPACING_SECONDS,
)
from utils.static_image_camera import StaticImageStore

from pin_worker_support import (
    Body,
    FakeClock,
    FakeShadowAccessor,
    FakeSleep,
    RecordingStore,
    disk_state,
    fresh_store_dirs,
    image_specs,
    make_worker,
    observable_state,
    pin_desired,
    render_image_bytes,
)

# Failure pattern: the outcome of each successive attempt before the first
# success. 0..2 failures => success on the next attempt; 3 failures =>
# total failure (the worker must stop at RETRIEVAL_MAX_ATTEMPTS).
_failure_kinds = st.sampled_from(["error", "timeout", "mismatch"])
_failure_prefixes = st.lists(
    _failure_kinds, min_size=0, max_size=RETRIEVAL_MAX_ATTEMPTS
)


class _TimeoutBody:
    """A body whose read stalls past the per-attempt wall-clock bound."""

    def __init__(self, clock):
        self._clock = clock

    def read(self, n=-1):
        self._clock.advance(RETRIEVAL_ATTEMPT_TIMEOUT_SECONDS + 1.0)
        return b"partial-bytes"


class _ScriptedS3:
    """Serves one scripted outcome per get_object call, then the good
    bytes; records the number of attempts made."""

    def __init__(self, script, good_bytes, clock):
        self._script = list(script)
        self._good = good_bytes
        self._clock = clock
        self.calls = 0

    def get_object(self, Bucket, Key):
        self.calls += 1
        kind = self._script.pop(0) if self._script else "ok"
        if kind == "error":
            raise ConnectionError("injected S3 retrieval failure")
        if kind == "timeout":
            return {"Body": _TimeoutBody(self._clock)}
        if kind == "mismatch":
            return {"Body": Body(self._good + b"\x00tampered")}
        return {"Body": Body(self._good)}


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    payload_spec=image_specs,
    failures=_failure_prefixes,
)
def test_retrieval_verification_and_retry(prior_spec, payload_spec, failures):
    """# Feature: cloud-static-camera-provisioning, Property 9: Retrieval
    verification and retry

    **Validates: Requirements 2.8, 2.9, 2.10, 2.11, 5.5**
    """
    tmp_dir = tempfile.mkdtemp(prefix="pin-worker-retry-")
    try:
        store_dir, marker_path = fresh_store_dirs(tmp_dir)
        real_store = StaticImageStore(base_dir=store_dir)
        if prior_spec is not None:
            real_store.pin_bytes(render_image_bytes(*prior_spec), "prior.img")
        prior_disk = disk_state(store_dir)
        prior_state = observable_state(real_store)

        payload = render_image_bytes(*payload_spec)
        clock = FakeClock()
        sleep = FakeSleep(clock)
        shadow = FakeShadowAccessor()
        s3 = _ScriptedS3(failures, payload, clock)
        store = RecordingStore(real_store)
        worker = make_worker(store, shadow, s3, clock, sleep, marker_path)

        desired = pin_desired("req-retry-1", payload)
        report = worker.process_one(desired)

        expect_success = len(failures) < RETRIEVAL_MAX_ATTEMPTS
        expected_attempts = (
            len(failures) + 1 if expect_success else RETRIEVAL_MAX_ATTEMPTS
        )

        # At most 3 attempts; each failed attempt counted exactly once (2.9,
        # 2.10, 2.11).
        assert s3.calls == expected_attempts
        assert s3.calls <= RETRIEVAL_MAX_ATTEMPTS

        # At least 5 seconds between consecutive attempts (2.10).
        assert len(sleep.calls) == expected_attempts - 1
        assert all(
            seconds >= RETRIEVAL_RETRY_SPACING_SECONDS for seconds in sleep.calls
        )

        if expect_success:
            # Applied only with checksum-matching bytes (2.8): exactly one
            # store call, carrying exactly the verified payload.
            assert store.pin_calls == [(payload, desired["fileName"])]
            assert report["status"] == "applied"
            assert real_store.is_pinned() is True
        else:
            # Total failure: the store is never invoked and the prior
            # Pinned_Image state is byte-identical (2.11, 5.5).
            assert store.pin_calls == []
            assert store.unpin_calls == 0
            assert disk_state(store_dir) == prior_disk
            assert observable_state(real_store) == prior_state
            assert report["status"] == "failed"
            final_cause = failures[-1]
            if final_cause == "mismatch":
                assert report["reason"] == "checksum mismatch"
            else:
                assert report["reason"].startswith("retrieval failure")

        # The outcome was echoed through the shadow either way.
        assert shadow.pin_reports[-1]["status"] == report["status"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
