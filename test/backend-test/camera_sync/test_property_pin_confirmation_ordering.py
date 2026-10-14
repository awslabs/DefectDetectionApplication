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
"""Property test for the pin worker's confirmation ordering.

# Feature: cloud-static-camera-provisioning, Property 13: Confirmation
# ordering

*For any* pin application cycle, the ``reported.staticImagePin``
confirmation (carrying the Pin_Request identifier and the applied
metadata) is written only after the pin store operation returns, and every
frame grab performed before the store operation returns the previous
Pinned_Image content byte-for-byte.

**Validates: Requirements 3.6, 7.7**

The store and shadow seams are instrumented into one shared event list:
the store wrapper grabs a frame at the moment ``pin_bytes`` is entered
(before the store operation completes — it must still serve the previous
content) and records when ``pin_bytes`` returns; the shadow accessor
records every reported write. Ordering is asserted on the event sequence.

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
    TEST_BUCKET,
    fresh_store_dirs,
    image_specs,
    make_worker,
    pin_desired,
    render_image_bytes,
)


class _OrderingStore:
    """Instrumented store seam: records a frame grab taken while the store
    operation is in flight (entry of ``pin_bytes``) and the moment
    ``pin_bytes`` returns, into the shared event list."""

    def __init__(self, store, events):
        self._store = store
        self._events = events

    def pin_bytes(self, data, file_name):
        try:
            frame = self._store.get_frame()
            observed = frame["data"]
        except Exception:
            observed = None
        self._events.append(("grab_during_pin", observed))
        result = self._store.pin_bytes(data, file_name)
        self._events.append(("pin_bytes_return",))
        return result

    def __getattr__(self, name):
        return getattr(self._store, name)


@settings(deadline=None)
@given(
    prior_spec=st.none() | image_specs,
    cycle_specs=st.lists(image_specs, min_size=1, max_size=3),
)
def test_confirmation_ordering(prior_spec, cycle_specs):
    """# Feature: cloud-static-camera-provisioning, Property 13:
    Confirmation ordering

    **Validates: Requirements 3.6, 7.7**
    """
    tmp_dir = tempfile.mkdtemp(prefix="pin-worker-ordering-")
    try:
        store_dir, marker_path = fresh_store_dirs(tmp_dir)
        real_store = StaticImageStore(base_dir=store_dir)
        if prior_spec is not None:
            real_store.pin_bytes(render_image_bytes(*prior_spec), "prior.img")

        events = []
        shadow = FakeShadowAccessor(events=events)
        clock = FakeClock()
        sleep = FakeSleep(clock)
        store = _OrderingStore(real_store, events)

        # Every cycle's object is retrievable up front.
        payloads = [render_image_bytes(*spec) for spec in cycle_specs]
        desireds = [
            pin_desired("req-order-{}".format(index), payload)
            for index, payload in enumerate(payloads)
        ]
        s3 = FakeS3Client(
            {(TEST_BUCKET, doc["key"]): payload
             for doc, payload in zip(desireds, payloads)}
        )
        worker = make_worker(store, shadow, s3, clock, sleep, marker_path)

        for doc, payload in zip(desireds, payloads):
            # The previous content a mid-operation grab must still serve.
            try:
                expected_previous = real_store.get_frame()["data"]
            except Exception:
                expected_previous = None
            cycle_start = len(events)

            worker.process_one(doc)

            cycle_events = events[cycle_start:]
            kinds = [event[0] for event in cycle_events]

            # Exactly one store operation and one confirmation per cycle,
            # in that order: the reported write happens only after
            # pin_bytes returns (3.6).
            assert kinds == [
                "grab_during_pin",
                "pin_bytes_return",
                "reported_write",
            ]

            # A grab before the store operation returns serves the
            # previous Pinned_Image byte-for-byte (7.7).
            assert cycle_events[0][1] == expected_previous

            # The confirmation carries the Pin_Request identifier and the
            # applied metadata (3.6).
            reported = cycle_events[2][1]["reported"]["staticImagePin"]
            assert reported["requestId"] == doc["requestId"]
            assert reported["status"] == "applied"
            store_metadata = real_store.status()["metadata"]
            for field in ("width", "height", "format", "fileName"):
                assert reported["metadata"][field] == store_metadata[field]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
