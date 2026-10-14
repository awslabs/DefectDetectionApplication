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
"""Regression tests for partial shadow-delta deliveries (feature:
cloud-static-camera-provisioning — bug found during on-device
verification on jetson-thor1 / LocalServer.arm64JP7 1.0.23).

AWS IoT computes the shadow delta per-field against the reported state,
so after the device echoes request 1 under ``reported.staticImagePin``,
a second request whose ``op``/``bucket`` (or any other field) equals the
echo's is delivered as a PARTIAL document missing those fields. The
unpatched worker read ``op=''``, could not process the request, and the
request hung ``pending`` forever.

The fix: when a delivered document is missing required fields, the worker
fetches the CURRENT full ``desired.staticImagePin`` document through the
shadow accessor and uses it iff its ``requestId`` matches the delivery's;
a failed GET or a mismatched requestId is reported ``failed`` naming the
incomplete delivery — never a silent hang. Newest-wins is preserved: the
GET returns the current single slot, definitionally the newest request.

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
    fresh_store_dirs,
    image_specs,
    make_worker,
    observable_state,
    pin_desired,
    remove_desired,
    render_image_bytes,
)


def iot_partial_delta(full_doc, reported_echo):
    """The document AWS IoT actually delivers: the per-field diff of the
    desired document against the reported echo — every field equal to the
    echo's value is OMITTED (the hardware-observed behavior)."""
    return {
        key: value
        for key, value in full_doc.items()
        if reported_echo.get(key) != value
    }


def _harness(tmp_dir, objects=None, get_state=None):
    store_dir, marker_path = fresh_store_dirs(tmp_dir)
    real_store = StaticImageStore(base_dir=store_dir)
    clock = FakeClock()
    sleep = FakeSleep(clock)
    shadow = FakeShadowAccessor(get_state=get_state)
    s3 = FakeS3Client(objects or {})
    store = RecordingStore(real_store)
    worker = make_worker(store, shadow, s3, clock, sleep, marker_path)
    return worker, store, real_store, shadow, s3


# --- the hardware scenario: pin -> pin replace over a partial delta -------------


@settings(deadline=None)
@given(first_spec=image_specs, second_spec=image_specs)
def test_partial_delta_pin_replace_resolved_via_shadow_get(
    first_spec, second_spec
):
    """A pin→pin replace delivered as IoT's per-field partial delta (no
    ``op``, no ``bucket`` — they equal request 1's echo) is resolved
    through the shadow GET and applies the second image correctly."""
    tmp_dir = tempfile.mkdtemp(prefix="pin-partial-replace-")
    try:
        first_data = render_image_bytes(*first_spec)
        second_data = render_image_bytes(*second_spec)
        doc1 = pin_desired("req-partial-1", first_data)
        doc2 = pin_desired("req-partial-2", second_data)

        worker, store, real_store, shadow, s3 = _harness(
            tmp_dir,
            objects={
                (TEST_BUCKET, doc1["key"]): first_data,
                (TEST_BUCKET, doc2["key"]): second_data,
            },
        )

        # Request 1: full document (the first delta is always complete —
        # nothing is echoed yet). Its echo becomes the reported state.
        report1 = worker.process_one(doc1)
        assert report1["status"] == "applied"
        echo1 = shadow.pin_reports[-1]

        # Request 2 arrives as the per-field partial delta; ``op`` and
        # ``bucket`` are always equal to the echo's and therefore absent.
        delta2 = iot_partial_delta(doc2, echo1)
        assert "op" not in delta2
        assert "bucket" not in delta2
        assert delta2["requestId"] == "req-partial-2"

        # The shadow's current desired slot holds the full newest doc.
        shadow.get_state = {
            "desired": {"staticImagePin": dict(doc2)},
            "reported": {"staticImagePin": dict(echo1)},
        }

        report2 = worker.process_one(delta2)

        # Resolved through exactly one GET and applied correctly.
        assert shadow.gets == [("test-thing", "dda-camera-registry")]
        assert report2["status"] == "applied"
        assert report2["requestId"] == "req-partial-2"
        assert report2["op"] == "pin"
        assert store.pin_calls[-1] == (second_data, doc2["fileName"])
        assert real_store.is_pinned() is True
        assert real_store.get_frame()["data"] is not None
        # The store holds the SECOND image (byte-identical to a direct pin).
        control_dir = tmp_dir + "-control"
        try:
            control = StaticImageStore(base_dir=control_dir)
            control.pin_bytes(second_data, doc2["fileName"])
            assert real_store.get_frame() == control.get_frame()
        finally:
            shutil.rmtree(control_dir, ignore_errors=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_partial_delta_remove_after_remove_resolved_via_shadow_get():
    """A remove→remove sequence delivers a partial delta with no ``op``
    (equal to the previous echo's); the GET-resolved removal confirms as
    the usual no-op."""
    tmp_dir = tempfile.mkdtemp(prefix="pin-partial-remove-")
    try:
        doc1 = remove_desired("req-rm-1")
        doc2 = remove_desired("req-rm-2")

        worker, store, real_store, shadow, s3 = _harness(tmp_dir)
        report1 = worker.process_one(doc1)
        assert report1["status"] == "applied"
        echo1 = shadow.pin_reports[-1]

        delta2 = iot_partial_delta(doc2, echo1)
        assert "op" not in delta2  # equal to the echo's "remove"
        shadow.get_state = {"desired": {"staticImagePin": dict(doc2)}}

        report2 = worker.process_one(delta2)
        assert report2["status"] == "applied"
        assert report2["requestId"] == "req-rm-2"
        assert report2["op"] == "remove"
        assert store.unpin_calls == 2
        assert real_store.is_pinned() is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- fallback paths: never silently hang -----------------------------------------


def _partial_pin_delta_after_prior_pin(tmp_dir, get_state):
    """Shared setup: request 1 applied, request 2 delivered partial with
    ``get_state`` driving the resolution GET. Returns everything the
    fallback assertions need."""
    first_data = render_image_bytes(8, 8, 1234, "PNG")
    second_data = render_image_bytes(6, 6, 5678, "PNG")
    doc1 = pin_desired("req-fb-1", first_data)
    doc2 = pin_desired("req-fb-2", second_data)

    worker, store, real_store, shadow, s3 = _harness(
        tmp_dir,
        objects={
            (TEST_BUCKET, doc1["key"]): first_data,
            (TEST_BUCKET, doc2["key"]): second_data,
        },
    )
    assert worker.process_one(doc1)["status"] == "applied"
    echo1 = shadow.pin_reports[-1]
    delta2 = iot_partial_delta(doc2, echo1)
    before = observable_state(real_store)
    shadow.get_state = get_state
    return worker, store, real_store, shadow, doc2, delta2, before


def _assert_failed_incomplete(report, request_id, store, real_store, before):
    assert report["status"] == "failed"
    assert report["requestId"] == request_id
    assert "incomplete delta delivery" in report["reason"]
    # The prior Pinned_Image is untouched and the store was never asked
    # to mutate for the failed request.
    assert len(store.pin_calls) == 1  # request 1's only
    assert observable_state(real_store) == before


def test_partial_delta_get_failure_reports_failed():
    """When the resolution GET raises, the request is reported ``failed``
    naming the incomplete delivery — never silently dropped."""
    tmp_dir = tempfile.mkdtemp(prefix="pin-partial-getfail-")
    try:
        worker, store, real_store, shadow, doc2, delta2, before = (
            _partial_pin_delta_after_prior_pin(
                tmp_dir, get_state=ConnectionError("shadow unavailable"))
        )
        report = worker.process_one(delta2)
        _assert_failed_incomplete(
            report, "req-fb-2", store, real_store, before)
        assert "shadow unavailable" in report["reason"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_partial_delta_get_swallowed_error_reports_failed():
    """The production accessor swallows GET errors and returns ``None``
    (or ``False`` on a missing shadow) — both report ``failed``."""
    for swallowed in (None, False):
        tmp_dir = tempfile.mkdtemp(prefix="pin-partial-getnone-")
        try:
            worker, store, real_store, shadow, doc2, delta2, before = (
                _partial_pin_delta_after_prior_pin(
                    tmp_dir, get_state=swallowed)
            )
            report = worker.process_one(delta2)
            _assert_failed_incomplete(
                report, "req-fb-2", store, real_store, before)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def test_partial_delta_request_id_mismatch_reports_failed():
    """A partial delta whose requestId no longer matches the shadow's
    current slot (the slot was superseded by a newer request) reports
    ``failed`` for the delivered request instead of applying the wrong
    document — newest-wins is preserved: only the current slot's request
    may execute."""
    tmp_dir = tempfile.mkdtemp(prefix="pin-partial-mismatch-")
    try:
        worker, store, real_store, shadow, doc2, delta2, before = (
            _partial_pin_delta_after_prior_pin(tmp_dir, get_state=None)
        )
        # The slot has already moved on to a NEWER request.
        newer = pin_desired("req-fb-3", b"newer-image-bytes")
        shadow.get_state = {"desired": {"staticImagePin": newer}}

        report = worker.process_one(delta2)
        _assert_failed_incomplete(
            report, "req-fb-2", store, real_store, before)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_complete_document_never_triggers_resolution_get():
    """A complete desired document (the startup catch-up path and every
    first delivery) is processed without any shadow GET."""
    tmp_dir = tempfile.mkdtemp(prefix="pin-partial-complete-")
    try:
        data = render_image_bytes(5, 5, 42, "BMP")
        doc = pin_desired("req-complete-1", data)
        worker, store, real_store, shadow, s3 = _harness(
            tmp_dir, objects={(TEST_BUCKET, doc["key"]): data})
        report = worker.process_one(doc)
        assert report["status"] == "applied"
        assert shadow.gets == []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
