"""
Property-based test for Portal video pin submissions
(static-camera-video-loop task 7.7, asynchronous validation: task 11.5).

# Feature: static-camera-video-loop, Property 12: Portal video submission acceptance and isolation

*For any* staged payload (library clips, invalid clips, non-video bytes)
and a video limit straddling its size:

- a payload over the limit is rejected at once (400 with the limit's
  message) without a validation record; any other submission is accepted
  for validation (202) and its validation job decides;
- the job accepts exactly when the probe accepts the payload; a rejection
  records the probe's message on the validation record, deletes the
  staged object, and has no other side effect (no Video_Pin_Request, no
  transport copy, no shadow write, no audit event);
- an acceptance records the Video_Pin_Request it created, supersedes only
  pending video requests, leaves every image Pin_Request item and
  ``desired.staticImagePin`` unchanged, and writes ``desired.staticVideoPin``
  with ``format`` = container.

**Validates: Requirements 8.2, 8.3, 8.5, 8.9**

The probe runs in-process through the ``run_video_probe`` seam: the real
``video_loop.probe_video`` when OpenCV (the video layer's library) is
installed, with real MJPEG/MPEG-4 clips in the payload mix, otherwise a
stub that applies the real container sniff to synthetic clips. The
oracle is the same runner's verdict on the same bytes, so the property is
about the submission flow honoring that verdict; the child-process runner
is covered by the example tests. The validation job runs inline through
the ``dispatch_video_validation`` seam (the asynchronous Event invocation
in production). Example counts come from the conftest hypothesis
profiles.
"""
import shutil
import tempfile

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, audit_events, create_usecase,
    image_bytes, make_user, pin_env, pin_request_items, register_device,
    submit_pin,
)
from video_pin_helpers import (
    fake_clip, inline_dispatch, probe_runner, real_clips, run_on_bytes,
    stage_video, staged_object_exists, validation_record,
    validation_records, video_pin_request_items,
)
from pin_route_helpers import invoke


@pytest.fixture(scope="module")
def operator(pin_env):
    usecase_id = create_usecase(pin_env, name="Video Pin Property Use Case")
    return make_user("Operator"), usecase_id


@pytest.fixture(scope="module")
def runner(pin_env):
    mode, run = probe_runner()
    original_probe = pin_env.module.run_video_probe
    original_dispatch = pin_env.module.dispatch_video_validation
    pin_env.module.run_video_probe = run
    pin_env.module.dispatch_video_validation = inline_dispatch(pin_env)
    yield mode, run
    pin_env.module.run_video_probe = original_probe
    pin_env.module.dispatch_video_validation = original_dispatch


@pytest.fixture(scope="module")
def library(runner):
    """Real clips when the real probe is in use (else none)."""
    mode, _run = runner
    if mode != "real":
        return {}
    directory = tempfile.mkdtemp(prefix="portal-video-clips-")
    try:
        return real_clips(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


_containers = st.sampled_from(["MP4", "MOV", "AVI", "MKV", "WEBM"])
_fake_payloads = st.builds(
    fake_clip,
    container=_containers,
    decodable=st.booleans(),
    codec=st.sampled_from(["H264", "HEVC", "VP9", "AV1"]),
    width=st.integers(min_value=1, max_value=4096),
    height=st.integers(min_value=1, max_value=4096),
    fps=st.sampled_from([5.0, 12.0, 29.97002997002997, 60.0]),
    frame_count=st.integers(min_value=1, max_value=10_000),
    padding=st.integers(min_value=0, max_value=64),
)
_non_video_payloads = st.one_of(
    st.binary(min_size=0, max_size=256),
    st.builds(image_bytes, image_format=st.sampled_from(["PNG", "JPEG"])),
)
_limit_deltas = st.one_of(st.integers(min_value=-64, max_value=64),
                          st.just(10_000_000))


def _draw_payload(data, library):
    kinds = ["fake", "non-video"] + (["clip", "truncated"] if library else [])
    kind = data.draw(st.sampled_from(kinds), label="kind")
    if kind == "fake":
        return data.draw(_fake_payloads, label="fake clip")
    if kind == "non-video":
        return data.draw(_non_video_payloads, label="non-video")
    name = data.draw(st.sampled_from(sorted(library)), label="clip")
    clip = library[name]
    if kind == "clip":
        return clip
    cut = data.draw(st.integers(min_value=64, max_value=len(clip) - 1),
                    label="truncated at")
    return clip[:cut]


def _shadow_sections(fake_shadow):
    return [update["payload"]["state"] for update in fake_shadow.updates]


def _submit(pin_env, device_id, user, payload):
    staging_key = stage_video(pin_env, payload)
    status, body = invoke(pin_env, "POST", device_id, user,
                          sub_path="/static-video/pin",
                          body={"stagingKey": staging_key,
                                "fileName": "scene.mp4"})
    return staging_key, status, body


@settings(deadline=None)
@given(data=st.data(), limit_delta=_limit_deltas,
       prior_video_pending=st.booleans())
def test_video_submission_acceptance_and_isolation(
        pin_env, operator, runner, library, data, limit_delta,
        prior_video_pending):
    user, usecase_id = operator
    mode, run = runner
    device_id = register_device(pin_env, usecase_id, prefix="thing-prop12")
    payload = _draw_payload(data, library)

    # Prior state: a pending image request (so its desired slot exists),
    # and sometimes a pending video request.
    pin_env.s3_holder["client"] = RecordingS3(pin_env.s3)
    pin_env.shadow_holder["client"] = FakeIotDataClient()
    status, body = submit_pin(pin_env, device_id, user, image_bytes())
    assert status == 201, body
    image_items_before = pin_request_items(pin_env, device_id)
    if prior_video_pending:
        prior_payload = (fake_clip("MP4") if mode == "stub"
                         else library[sorted(library)[0]] if library else None)
        if prior_payload is not None:
            _key, status, body = _submit(pin_env, device_id, user,
                                         prior_payload)
            assert status == 202, body
            prior = validation_record(pin_env, device_id, body["validationId"])
            assert prior["status"] == "accepted", prior
    videos_before = video_pin_request_items(pin_env, device_id)
    records_before = validation_records(pin_env, device_id)
    audits_before = audit_events(pin_env, device_id, action="pin_static_video")

    verdict = run_on_bytes(run, payload)
    limit = max(1, len(payload) + limit_delta)
    log = []
    fake_s3 = RecordingS3(pin_env.s3, log=log)
    fake_shadow = FakeIotDataClient(log=log)
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow
    original_limit = pin_env.module.MAX_PIN_VIDEO_BYTES
    pin_env.module.MAX_PIN_VIDEO_BYTES = limit
    try:
        staging_key, status, body = _submit(pin_env, device_id, user, payload)
    finally:
        pin_env.module.MAX_PIN_VIDEO_BYTES = original_limit

    within_limit = len(payload) <= limit
    accepted = within_limit and verdict["ok"]
    event("{} probe: {}".format(
        mode, "accepted" if accepted else
        "rejected (limit)" if not within_limit else "rejected (probe)"))
    # Image requests are never touched, whatever the outcome.
    assert pin_request_items(pin_env, device_id) == image_items_before

    if not within_limit:
        # Rejected at once, before any validation record.
        assert status == 400, body
        assert str(limit) in body["error"]
        assert validation_records(pin_env, device_id) == records_before
    else:
        assert status == 202, body
        assert body["status"] == "validating"
        record = validation_record(pin_env, device_id, body["validationId"])

    if not accepted:
        if within_limit:
            assert record["status"] == "rejected", record
            assert record["validation_error"] == verdict["error"]
            assert "pin_request_id" not in record
        assert not staged_object_exists(pin_env, staging_key)
        assert video_pin_request_items(pin_env, device_id) == videos_before
        assert fake_s3.copies == []
        assert fake_shadow.updates == []
        assert audit_events(pin_env, device_id,
                            action="pin_static_video") == audits_before
        return

    info = verdict["info"]
    assert record["status"] == "accepted", record
    videos = video_pin_request_items(pin_env, device_id)
    new = videos[0]
    assert new["pin_request_id"] == record["pin_request_id"]
    assert new["status"] == "pending"
    assert new["format"] == info["format"]
    assert int(new["size_bytes"]) == len(payload)
    assert int(new["validated_metadata"]["frameCount"]) == info["frameCount"]
    assert int(record["validated_metadata"]["frameCount"]) == \
        info["frameCount"]
    assert record["validated_metadata"]["format"] == info["format"]
    # Only pending video requests were superseded.
    for old in videos[1:]:
        before = next(item for item in videos_before
                      if item["pin_request_id"] == old["pin_request_id"])
        if before["status"] == "pending":
            assert old["status"] == "superseded"
        else:
            assert old["status"] == before["status"]
    # One canonical copy under the video key, one shadow write touching
    # only the video slot; the staged object is gone.
    pin_request_id = record["pin_request_id"]
    assert [copy["Key"] for copy in fake_s3.copies] == [
        f"static-image-pins/{device_id}/video/{pin_request_id}"]
    assert not staged_object_exists(pin_env, staging_key)
    (state,) = _shadow_sections(fake_shadow)
    assert set(state["desired"]) == {"staticVideoPin"}
    assert set(state["reported"]) == {"staticVideoPin"}
    assert state["reported"]["staticVideoPin"] is None
    desired = state["desired"]["staticVideoPin"]
    assert desired["format"] == info["format"]
    assert desired["op"] == "pin"
    assert desired["requestId"] == pin_request_id
    assert desired["sizeBytes"] == len(payload)
    assert log.index(("canonical_copy", fake_s3.copies[0]["Key"])) < \
        log.index(("shadow_write", device_id))
    (audit,) = [entry for entry in audit_events(
        pin_env, device_id, action="pin_static_video")
        if entry not in audits_before]
    assert audit["user_id"] == user["user_id"]
    assert audit["details"]["pin_request_id"] == pin_request_id
