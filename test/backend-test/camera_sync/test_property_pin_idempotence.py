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
"""Property test for idempotent redelivery of a Pin_Request.

# Feature: cloud-static-camera-provisioning, Property 12: Idempotent
# redelivery

*For any* Pin_Request and any number of deliveries N >= 1 of that same
request identifier (pin or removal, including removals targeting an
unpinned device), the observable outcome equals exactly-once delivery: the
pin store's mutating operations are invoked at most once, and the
Pinned_Image state, enumeration state, and reported Sync_Status are
identical after every delivery.

**Validates: Requirements 3.5, 7.4**

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import StaticImageStore

from pin_worker_support import (
    FakeClock,
    FakeS3Client,
    FakeShadowAccessor,
    FakeSleep,
    RecordingStore,
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

# A request: a pin of a generated valid image, or a removal (which may
# also target an unpinned store when prior_spec is None).
_requests = st.one_of(
    st.tuples(st.just("pin"), image_specs),
    st.tuples(st.just("remove"), st.none()),
)


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    request=_requests,
    deliveries=st.integers(min_value=1, max_value=4),
)
def test_idempotent_redelivery(prior_spec, request, deliveries):
    """# Feature: cloud-static-camera-provisioning, Property 12: Idempotent
    redelivery

    **Validates: Requirements 3.5, 7.4**
    """
    op, payload_spec = request
    tmp_dir = tempfile.mkdtemp(prefix="pin-worker-idem-")
    try:
        store_dir, marker_path = fresh_store_dirs(tmp_dir)
        real_store = StaticImageStore(base_dir=store_dir)
        if prior_spec is not None:
            real_store.pin_bytes(render_image_bytes(*prior_spec), "prior.img")

        if op == "pin":
            payload = render_image_bytes(*payload_spec)
            desired = pin_desired("req-idem-1", payload)
            objects = {(TEST_BUCKET, desired["key"]): payload}
        else:
            desired = remove_desired("req-idem-1")
            objects = {}

        clock = FakeClock()
        sleep = FakeSleep(clock)
        shadow = FakeShadowAccessor()
        s3 = FakeS3Client(objects)

        # RecordingStore counts the mutating operations across deliveries.
        store = RecordingStore(real_store)
        worker = make_worker(store, shadow, s3, clock, sleep, marker_path)

        reports = []
        states = []
        for _ in range(deliveries):
            reports.append(worker.process_one(dict(desired)))
            states.append(
                (observable_state(real_store), disk_state(store_dir))
            )

        # Store mutations (and the retrieval itself) happen at most once
        # (3.5, 7.4).
        assert len(store.pin_calls) + store.unpin_calls <= 1
        assert len(s3.calls) <= 1

        # Every delivery re-reports the identical recorded outcome, and the
        # pin/enumeration state is identical after each one (3.5, 7.4).
        assert all(report == reports[0] for report in reports)
        assert all(state == states[0] for state in states)
        assert len(shadow.pin_reports) == deliveries
        assert all(echo == shadow.pin_reports[0] for echo in shadow.pin_reports)

        # The recorded outcome is the exactly-once one: pins applied,
        # removals converged to "no Pinned_Image" (a no-op success on an
        # already-unpinned store).
        assert reports[0]["status"] == "applied"
        if op == "pin":
            assert real_store.is_pinned() is True
            assert store.pin_calls == [(payload, desired["fileName"])]
        else:
            assert real_store.is_pinned() is False
            assert store.unpin_calls == 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
