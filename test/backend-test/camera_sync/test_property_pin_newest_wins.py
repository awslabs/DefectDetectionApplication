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
"""Property test for newest-request-wins on the device.

# Feature: cloud-static-camera-provisioning, Property 14:
# Newest-request-wins on the device

*For any* sequence of two or more desired-slot documents observed by the
worker (modeling requests issued while disconnected followed by
reconnection), only the newest request's operation is executed — zero
superseded requests are applied — and the device's pin state converges to
the newest request's requested state (pinned for a pin operation, unpinned
for a removal); worker removal of any starting state leaves the store in
exactly the state a direct Device_Pin_API unpin produces, with removals of
an already-unpinned store confirming as no-ops.

**Validates: Requirements 5.4, 7.3, 7.4**

The worker's dedicated thread is never started: the documents are queued
through ``on_desired`` (the single-slot newest-wins mirror) and then
drained synchronously, exactly like the reconnect burst the offline model
describes.

Runs with the hypothesis profiles registered in the root conftest
(``fast`` = 25 examples locally, ``HYPOTHESIS_PROFILE=ci`` = 100).
"""
import os
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from utils.static_image_camera import (
    StaticImagePinError,
    StaticImageStore,
)

from pin_worker_support import (
    FakeClock,
    FakeS3Client,
    FakeShadowAccessor,
    FakeSleep,
    RecordingStore,
    TEST_BUCKET,
    fresh_store_dirs,
    image_specs,
    make_worker,
    pin_desired,
    remove_desired,
    render_image_bytes,
)

# One request issued while the device was disconnected: a pin of a
# generated valid image, or a removal.
_operations = st.one_of(
    st.tuples(st.just("pin"), image_specs),
    st.tuples(st.just("remove"), st.none()),
)


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    operations=st.lists(_operations, min_size=2, max_size=5),
)
def test_newest_request_wins(prior_spec, operations):
    """# Feature: cloud-static-camera-provisioning, Property 14:
    Newest-request-wins on the device

    **Validates: Requirements 5.4, 7.3, 7.4**
    """
    tmp_dir = tempfile.mkdtemp(prefix="pin-worker-newest-")
    try:
        store_dir, marker_path = fresh_store_dirs(tmp_dir)
        real_store = StaticImageStore(base_dir=store_dir)
        prior_data = (
            render_image_bytes(*prior_spec) if prior_spec is not None else None
        )
        if prior_data is not None:
            real_store.pin_bytes(prior_data, "prior.img")

        # Build every request's document; every pin's object is
        # retrievable (the superseded ones must simply never be fetched).
        documents = []
        payloads = {}
        for index, (op, payload_spec) in enumerate(operations):
            request_id = "req-newest-{}".format(index)
            if op == "pin":
                payload = render_image_bytes(*payload_spec)
                doc = pin_desired(request_id, payload)
                payloads[doc["key"]] = payload
            else:
                doc = remove_desired(request_id)
            documents.append(doc)

        clock = FakeClock()
        sleep = FakeSleep(clock)
        shadow = FakeShadowAccessor()
        s3 = FakeS3Client(
            {(TEST_BUCKET, key): data for key, data in payloads.items()}
        )
        store = RecordingStore(real_store)
        worker = make_worker(store, shadow, s3, clock, sleep, marker_path)

        # The single desired slot observes each document in order (the
        # offline burst); the worker thread is not running, so the slot
        # holds only the newest when processing begins (reconnection).
        for doc in documents:
            worker.on_desired(doc)
        processed = worker.process_pending()
        assert worker.process_pending() is None  # nothing else was queued

        newest_op = operations[-1][0]
        newest_doc = documents[-1]

        # Only the newest request executed; zero superseded requests were
        # applied or even fetched (5.4).
        assert processed is not None
        assert processed["requestId"] == newest_doc["requestId"]
        assert len(store.pin_calls) + store.unpin_calls <= 1
        assert [key for _, key in s3.calls] == (
            [newest_doc["key"]] if newest_op == "pin" else []
        )
        assert len(shadow.pin_reports) == 1
        assert shadow.pin_reports[0]["requestId"] == newest_doc["requestId"]

        if newest_op == "pin":
            # Converged to the newest request's pinned state (5.4, 7.3).
            assert processed["status"] == "applied"
            assert real_store.is_pinned() is True
            assert store.pin_calls == [
                (payloads[newest_doc["key"]], newest_doc["fileName"])
            ]
            expected = StaticImageStore(
                base_dir=_control_dir(tmp_dir, "pin-control")
            )
            expected.pin_bytes(payloads[newest_doc["key"]], newest_doc["fileName"])
            assert real_store.get_frame() == expected.get_frame()
        else:
            # Worker removal == direct Device_Pin_API unpin, including the
            # no-op confirmation on an already-unpinned store (7.3, 7.4).
            assert processed["status"] == "applied"
            assert store.unpin_calls == 1
            control = StaticImageStore(
                base_dir=_control_dir(tmp_dir, "remove-control")
            )
            if prior_data is not None:
                control.pin_bytes(prior_data, "prior.img")
            try:
                control.unpin()
            except StaticImagePinError:
                pass  # direct unpin of an unpinned store: state unchanged
            assert real_store.status() == control.status()
            assert real_store.is_pinned() is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _control_dir(tmp_dir, name):
    return os.path.join(tmp_dir, name)
