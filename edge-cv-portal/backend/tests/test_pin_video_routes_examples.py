"""
Example tests for the Portal_Video_Pin_API (static-camera-video-loop
tasks 7.8 and 11.5).

- Routes and authorization: upload URL on the shared staging prefix,
  Viewer can read but not mutate, unknown devices 404 on every route.
- Asynchronous validation: the pin route answers 202 and hands the job
  to the function; the status view reports validating, accepted,
  rejected, and expired; a newer submission or a removal supersedes a
  submission still validating (at submit time, and in the job before and
  after the decode); a failed dispatch rejects the submission; the job is
  idempotent; the entry point routes job events and the Event invocation
  goes to the configured function.
- Validation outcomes: the timeout message (stub runner and the real
  child process with a tiny budget), a crashed child, a non-video file
  through the real child (rejected by the sniff before OpenCV loads), and
  a real clip through the real child (skipped without OpenCV).
- Isolation: the image and video status views each show only their own
  requests; a video removal supersedes only the video request.
- Ingest: ``reported.staticVideoPin`` confirmations reach the video
  family; an applied remove marks ``CAMERA#static-video-camera`` absent
  without a shadow cleanup write; an omitted video entry is kept absent,
  never deleted.
- The deployment validator accepts ``StaticVideo`` for
  ``aravis_camera_source``.

_Requirements: 4.8, 8.1–8.9_
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, STAGING_PREFIX, audit_events,
    create_usecase, image_bytes, invoke, make_user, pin_env,
    pin_request_items, register_device, submit_pin,
)
from video_pin_helpers import (
    QueuedDispatch, cv2_unavailable_reason, fake_clip, inline_dispatch,
    real_clips, real_probe, stage_video, staged_object_exists, stub_probe,
    submit_video, validation_record, validation_records,
    video_pin_request_items,
)

TIMEOUT_MESSAGE = (
    "The video took too long to validate (over 60 seconds); use a lower "
    "resolution or more frequent keyframes.")
EXPIRED_MESSAGE = (
    "Video validation did not complete in time; submit the video again.")


@pytest.fixture(scope="module")
def usecase(pin_env):
    return create_usecase(pin_env, name="Video Pin Examples")


@pytest.fixture(scope="module")
def operator():
    return make_user("Operator")


@pytest.fixture
def fakes(pin_env):
    fake_s3 = RecordingS3(pin_env.s3)
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow
    return SimpleNamespace(s3=fake_s3, shadow=fake_shadow)


@pytest.fixture
def stub_runner(pin_env, monkeypatch):
    monkeypatch.setattr(pin_env.module, "run_video_probe", stub_probe)


@pytest.fixture(autouse=True)
def inline_jobs(pin_env, monkeypatch):
    """By default the validation job runs inside the pin route."""
    monkeypatch.setattr(pin_env.module, "dispatch_video_validation",
                        inline_dispatch(pin_env))


@pytest.fixture
def queued(pin_env, monkeypatch):
    """Jobs queued for the test to run later, in any order."""
    queue = QueuedDispatch(pin_env)
    monkeypatch.setattr(pin_env.module, "dispatch_video_validation", queue)
    return queue


def submit_and_validate(pin_env, device_id, user, payload,
                        file_name="scene.mp4"):
    """Submit through the pin route (the job runs inline): the 202 body and
    the resulting validation record."""
    status, body = submit_video(pin_env, device_id, user, payload, file_name)
    assert status == 202, body
    return body, validation_record(pin_env, device_id, body["validationId"])


def _viewer(pin_env, usecase):
    user = make_user("DataLabeler")
    pin_env.user_roles.put_item(Item={
        "user_id": user["user_id"], "usecase_id": usecase, "role": "Viewer"})
    return user


# --- routes and authorization ------------------------------------------------------


def test_upload_url_issues_a_shared_staging_key(pin_env, usecase, operator,
                                               fakes):
    device_id = register_device(pin_env, usecase)
    status, body = invoke(pin_env, "POST", device_id, operator,
                          sub_path="/static-video/upload-url")
    assert status == 200, body
    assert body["stagingKey"].startswith(STAGING_PREFIX)
    assert body["uploadUrl"]
    assert body["expiresInSeconds"] == 900


def test_viewer_reads_but_cannot_mutate(pin_env, usecase, fakes, stub_runner):
    device_id = register_device(pin_env, usecase)
    viewer = _viewer(pin_env, usecase)
    for method, sub_path in (("POST", "/static-video/upload-url"),
                             ("POST", "/static-video/pin"),
                             ("DELETE", "/static-video/pin")):
        status, body = invoke(pin_env, method, device_id, viewer,
                              sub_path=sub_path,
                              body={"stagingKey": STAGING_PREFIX + "x",
                                    "fileName": "a.mp4"})
        assert status == 403, (method, sub_path, body)
    status, body = invoke(pin_env, "GET", device_id, viewer,
                          sub_path="/static-video")
    assert status == 200, body
    assert body["noPinRequest"] is True and body["latest"] is None
    assert video_pin_request_items(pin_env, device_id) == []
    assert fakes.shadow.updates == []


def test_unknown_device_is_404_on_every_route(pin_env, operator, fakes):
    device_id = f"thing-unregistered-{uuid.uuid4().hex[:8]}"
    for method, sub_path in (("POST", "/static-video/upload-url"),
                             ("POST", "/static-video/pin"),
                             ("DELETE", "/static-video/pin"),
                             ("GET", "/static-video")):
        status, body = invoke(pin_env, method, device_id, operator,
                              sub_path=sub_path)
        assert status == 404, (method, sub_path, body)
        assert device_id in body["error"]


# --- asynchronous validation -----------------------------------------------------------


def _view(pin_env, device_id, user):
    status, view = invoke(pin_env, "GET", device_id, user,
                          sub_path="/static-video")
    assert status == 200, view
    return view


def test_pin_route_answers_202_and_the_job_delivers(pin_env, usecase, operator,
                                                   fakes, stub_runner, queued):
    device_id = register_device(pin_env, usecase)

    status, body = submit_video(pin_env, device_id, operator,
                                fake_clip("MP4", codec="HEVC"), "line.mp4")

    assert status == 202, body
    assert body == {"validationId": body["validationId"],
                    "deviceId": device_id, "status": "validating"}
    assert queued.jobs == [{"deviceId": device_id,
                            "validationId": body["validationId"]}]
    # Nothing is delivered before the job runs.
    assert video_pin_request_items(pin_env, device_id) == []
    assert fakes.s3.copies == [] and fakes.shadow.updates == []
    view = _view(pin_env, device_id, operator)
    assert view["latest"] is None
    assert view["validation"] == {
        "validationId": body["validationId"], "status": "validating",
        "createdAt": view["validation"]["createdAt"], "fileName": "line.mp4"}

    assert queued.run() == {"status": "accepted"}

    (item,) = video_pin_request_items(pin_env, device_id)
    view = _view(pin_env, device_id, operator)
    assert view["validation"]["status"] == "accepted"
    assert view["validation"]["pinRequestId"] == item["pin_request_id"]
    assert view["validation"]["validatedMetadata"]["codec"] == "HEVC"
    assert view["validation"]["validatedMetadata"]["format"] == "MP4"
    assert view["latest"]["pinRequestId"] == item["pin_request_id"]
    assert view["latest"]["status"] == "pending"
    (update,) = fakes.shadow.updates
    assert update["payload"]["state"]["desired"]["staticVideoPin"][
        "requestId"] == item["pin_request_id"]
    (event,) = audit_events(pin_env, device_id, action="pin_static_video")
    assert event["user_id"] == operator["user_id"]
    assert event["details"]["validation_id"] == body["validationId"]


def test_newer_submission_supersedes_one_still_validating(
        pin_env, usecase, operator, fakes, stub_runner, queued):
    device_id = register_device(pin_env, usecase)
    status, first = submit_video(pin_env, device_id, operator,
                                 fake_clip("MP4"), "first.mp4")
    assert status == 202, first
    first_key = validation_record(pin_env, device_id,
                                  first["validationId"])["staging_key"]

    status, second = submit_video(pin_env, device_id, operator,
                                  fake_clip("MKV"), "second.mkv")
    assert status == 202, second

    assert validation_record(pin_env, device_id, first["validationId"])[
        "status"] == "superseded"
    assert not staged_object_exists(pin_env, first_key)
    assert _view(pin_env, device_id, operator)["validation"][
        "validationId"] == second["validationId"]
    # The first job finds its record superseded and does nothing.
    assert queued.run(0) == {"status": "ignored"}
    assert queued.run(1) == {"status": "accepted"}
    (item,) = video_pin_request_items(pin_env, device_id)
    assert item["file_name"] == "second.mkv"
    assert len(fakes.shadow.updates) == 1


def test_removal_supersedes_a_submission_still_validating(
        pin_env, usecase, operator, fakes, stub_runner, queued):
    device_id = register_device(pin_env, usecase)
    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))
    assert status == 202, body

    status, removal = invoke(pin_env, "DELETE", device_id, operator,
                             sub_path="/static-video/pin")
    assert status == 200, removal

    assert validation_record(pin_env, device_id, body["validationId"])[
        "status"] == "superseded"
    assert queued.run() == {"status": "ignored"}
    (item,) = video_pin_request_items(pin_env, device_id)
    assert item["pin_request_id"] == removal["pinRequestId"]
    assert item["op"] == "remove"
    assert fakes.s3.copies == []


def test_job_rechecks_supersession_after_the_decode(
        pin_env, usecase, operator, fakes, monkeypatch, queued):
    """A newer submission recorded while the job decodes (a race the pin
    route's own supersede missed) stops the job before any delivery."""
    import pin_requests

    device_id = register_device(pin_env, usecase)
    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))
    assert status == 202, body

    def _probe_then_race(path):
        newer = pin_requests.build_video_validation_item(
            device_id, usecase, pin_env.module.now_ms() + 1_000,
            staging_key="static-image-pins/staging/newer", file_name="n.mp4",
            size_bytes=1, requested_by=operator["user_id"])
        pin_env.registry.put_item(Item=newer)
        return stub_probe(path)

    monkeypatch.setattr(pin_env.module, "run_video_probe", _probe_then_race)

    assert queued.run() == {"status": "superseded"}
    assert validation_record(pin_env, device_id, body["validationId"])[
        "status"] == "superseded"
    assert video_pin_request_items(pin_env, device_id) == []
    assert fakes.s3.copies == [] and fakes.shadow.updates == []


def test_job_supersedes_itself_when_a_newer_request_exists(
        pin_env, usecase, operator, fakes, stub_runner, queued):
    import pin_requests

    device_id = register_device(pin_env, usecase)
    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))
    assert status == 202, body
    newer = pin_requests.build_pin_request_item(
        device_id, usecase, pin_requests.OP_REMOVE,
        pin_env.module.now_ms() + 1_000,
        sk_prefix=pin_requests.SK_VIDEO_PIN_REQUEST_PREFIX)
    pin_requests.insert_pin_request(pin_env.registry, newer)

    assert queued.run() == {"status": "superseded"}
    items = video_pin_request_items(pin_env, device_id)
    assert [item["pin_request_id"] for item in items] == [
        newer["pin_request_id"]]


def test_failed_dispatch_rejects_the_submission(pin_env, usecase, operator,
                                                fakes, monkeypatch):
    def _broken(job):
        raise RuntimeError("Lambda Invoke throttled")

    monkeypatch.setattr(pin_env.module, "dispatch_video_validation", _broken)
    device_id = register_device(pin_env, usecase)

    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))

    assert status == 502, body
    assert body["error"] == ("Video validation could not be started; "
                             "submit the video again")
    (record,) = validation_records(pin_env, device_id)
    assert record["status"] == "rejected"
    assert record["validation_error"] == body["error"]
    assert not staged_object_exists(pin_env, record["staging_key"])
    assert video_pin_request_items(pin_env, device_id) == []


def test_job_is_idempotent(pin_env, usecase, operator, fakes, stub_runner,
                           queued):
    device_id = register_device(pin_env, usecase)
    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))
    assert status == 202, body

    assert queued.run() == {"status": "accepted"}
    assert queued.run() == {"status": "ignored"}

    assert len(video_pin_request_items(pin_env, device_id)) == 1
    assert len(fakes.shadow.updates) == 1
    assert len(audit_events(pin_env, device_id,
                            action="pin_static_video")) == 1


def test_unknown_or_malformed_jobs_are_ignored(pin_env, usecase):
    device_id = register_device(pin_env, usecase)
    run = pin_env.module.run_video_validation
    assert run({"deviceId": device_id,
                "validationId": "00000000000001#deadbeef"}) == {
        "status": "ignored"}
    assert run({"deviceId": device_id}) == {"status": "ignored"}
    assert run("not a job") == {"status": "ignored"}


def test_validation_expires_when_the_job_never_reports(
        pin_env, usecase, operator, fakes, stub_runner, queued, monkeypatch):
    device_id = register_device(pin_env, usecase)
    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))
    assert status == 202, body
    created = validation_record(pin_env, device_id,
                                body["validationId"])["created_at"]

    later = int(created) + 5 * 60 * 1000 + 1
    monkeypatch.setattr(pin_env.module, "now_ms", lambda: later)
    view = _view(pin_env, device_id, operator)
    assert view["validation"]["status"] == "expired"
    assert view["validation"]["error"] == EXPIRED_MESSAGE

    # A job that finally starts that late refuses to deliver.
    assert queued.run() == {"status": "rejected"}
    record = validation_record(pin_env, device_id, body["validationId"])
    assert record["validation_error"] == EXPIRED_MESSAGE
    assert video_pin_request_items(pin_env, device_id) == []
    assert not staged_object_exists(pin_env, record["staging_key"])


def test_staged_object_gone_before_the_job(pin_env, usecase, operator, fakes,
                                           stub_runner, queued):
    device_id = register_device(pin_env, usecase)
    status, body = submit_video(pin_env, device_id, operator, fake_clip("MP4"))
    assert status == 202, body
    record = validation_record(pin_env, device_id, body["validationId"])
    pin_env.s3.delete_object(Bucket=pin_env.bucket, Key=record["staging_key"])

    assert queued.run() == {"status": "rejected"}
    record = validation_record(pin_env, device_id, body["validationId"])
    assert record["validation_error"].startswith("Staged upload not found")
    assert video_pin_request_items(pin_env, device_id) == []


def test_missing_staged_object_is_rejected_at_submit(pin_env, usecase,
                                                    operator, fakes):
    device_id = register_device(pin_env, usecase)
    status, body = invoke(pin_env, "POST", device_id, operator,
                          sub_path="/static-video/pin",
                          body={"stagingKey": STAGING_PREFIX + "missing",
                                "fileName": "a.mp4"})
    assert status == 400, body
    assert body["error"].startswith("Staged upload not found")
    assert validation_records(pin_env, device_id) == []


@pytest.fixture
def video_entry_module(pin_env):
    """camera_video_pin bound to this module's camera_registry."""
    sys.modules.pop("camera_video_pin", None)
    import camera_video_pin

    assert camera_video_pin.camera_registry is pin_env.module
    yield camera_video_pin
    sys.modules.pop("camera_video_pin", None)


def test_entry_point_runs_jobs_and_serves_only_video_routes(
        pin_env, usecase, operator, fakes, stub_runner, queued,
        video_entry_module):
    from pin_route_helpers import make_event

    device_id = register_device(pin_env, usecase)
    staging_key = stage_video(pin_env, fake_clip("AVI", codec="MJPEG"))
    response = video_entry_module.handler(make_event(
        "POST", device_id, operator, "/static-video/pin",
        {"stagingKey": staging_key, "fileName": "loop.avi"}), None)
    assert response["statusCode"] == 202, response

    (job,) = queued.jobs
    assert video_entry_module.handler({"videoValidation": job}, None) == {
        "status": "accepted"}
    assert len(video_pin_request_items(pin_env, device_id)) == 1

    response = video_entry_module.handler(
        make_event("GET", device_id, operator, ""), None)
    assert response["statusCode"] == 404


def test_dispatch_invokes_the_configured_function(pin_env, monkeypatch):
    calls = []

    class _Lambda:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            return {"StatusCode": 202}

    module = pin_env.module
    monkeypatch.setattr(module, "_lambda_client", _Lambda())
    monkeypatch.setattr(module, "VIDEO_VALIDATION_FUNCTION",
                        "dda-portal-camera-video-pin")
    job = {"deviceId": "thing-1", "validationId": "00000000000001#abcdef12"}

    module._dispatch_video_validation_event(job)

    (call,) = calls
    assert call["FunctionName"] == "dda-portal-camera-video-pin"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"].decode("utf-8")) == {
        "videoValidation": job}

    monkeypatch.setattr(module, "VIDEO_VALIDATION_FUNCTION", None)
    with pytest.raises(RuntimeError, match="VIDEO_VALIDATION_FUNCTION"):
        module._dispatch_video_validation_event(job)


# --- validation outcomes -------------------------------------------------------------


def test_timeout_is_rejected_with_the_timeout_message(pin_env, usecase,
                                                     operator, fakes,
                                                     monkeypatch):
    def _slow(path):
        raise pin_env.module.VideoProbeTimeout("probe exceeded 60 s")

    monkeypatch.setattr(pin_env.module, "run_video_probe", _slow)
    device_id = register_device(pin_env, usecase)

    body, record = submit_and_validate(pin_env, device_id, operator,
                                       fake_clip("MP4"))

    assert record["status"] == "rejected"
    assert record["validation_error"] == TIMEOUT_MESSAGE
    assert video_pin_request_items(pin_env, device_id) == []
    assert fakes.s3.copies == [] and fakes.shadow.updates == []
    assert not staged_object_exists(pin_env, record["staging_key"])
    status, view = invoke(pin_env, "GET", device_id, operator,
                          sub_path="/static-video")
    assert view["validation"]["status"] == "rejected"
    assert view["validation"]["error"] == TIMEOUT_MESSAGE
    assert view["latest"] is None and view["noPinRequest"] is True


def _write(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def test_child_process_times_out(pin_env, tmp_path, monkeypatch):
    monkeypatch.setattr(pin_env.module, "VIDEO_VALIDATION_TIMEOUT_SECONDS",
                        0.001)
    path = _write(str(tmp_path), "clip.mp4", fake_clip("MP4"))
    with pytest.raises(pin_env.module.VideoProbeTimeout):
        pin_env.module._video_probe_child(path)
    info, error = pin_env.module.validate_pin_video(path)
    assert info is None
    assert json.loads(error["body"])["error"] == TIMEOUT_MESSAGE


def test_crashed_child_is_reported_undecodable(pin_env, tmp_path,
                                               monkeypatch):
    script = _write(str(tmp_path), "crash.py",
                    b"import sys\nsys.stderr.write('segfault')\nsys.exit(139)\n")
    monkeypatch.setattr(pin_env.module.video_loop, "__file__", script)
    path = _write(str(tmp_path), "clip.mp4", fake_clip("MP4"))

    result = pin_env.module._video_probe_child(path)

    assert result["ok"] is False
    assert "could not be decoded (codec: unknown)" in result["error"]


def test_real_child_rejects_a_non_video_before_loading_opencv(pin_env,
                                                             tmp_path):
    path = _write(str(tmp_path), "photo.png", image_bytes("PNG"))

    result = pin_env.module._video_probe_child(path)

    assert result == {"ok": False,
                      "error": pin_env.module.video_loop.not_a_video_message()}


def test_real_child_decodes_a_real_clip(pin_env, tmp_path):
    reason = cv2_unavailable_reason()
    if reason:
        pytest.skip(reason)
    clips = real_clips(str(tmp_path))
    if not clips:
        pytest.skip("this OpenCV build cannot write MJPEG or MPEG-4 clips")
    for name, data in sorted(clips.items()):
        path = _write(str(tmp_path), "staged-" + name, data)
        result = pin_env.module._video_probe_child(path)
        assert result == real_probe(path), name
        assert result["ok"] is True, (name, result)
        assert result["info"]["format"] in ("AVI", "MP4")


def test_real_clip_pins_through_the_child_runner(pin_env, usecase, operator,
                                                fakes, tmp_path):
    reason = cv2_unavailable_reason()
    if reason:
        pytest.skip(reason)
    clips = real_clips(str(tmp_path))
    if not clips:
        pytest.skip("this OpenCV build cannot write MJPEG or MPEG-4 clips")
    name, data = sorted(clips.items())[0]
    device_id = register_device(pin_env, usecase)

    body, record = submit_and_validate(pin_env, device_id, operator, data,
                                       name)

    assert record["status"] == "accepted", record
    (item,) = video_pin_request_items(pin_env, device_id)
    assert item["pin_request_id"] == record["pin_request_id"]
    assert item["format"] == record["validated_metadata"]["format"]
    assert int(item["validated_metadata"]["frameCount"]) >= 1
    (update,) = fakes.shadow.updates
    assert update["payload"]["state"]["desired"]["staticVideoPin"][
        "fileName"] == name
    events = audit_events(pin_env, device_id, action="pin_static_video")
    assert len(events) == 1
    assert events[0]["details"]["validated_metadata"]["frameCount"] >= 1


# --- isolation between the two cameras ------------------------------------------------


def test_status_views_and_removal_stay_per_camera(pin_env, usecase, operator,
                                                  fakes, stub_runner):
    device_id = register_device(pin_env, usecase)
    status, image = submit_pin(pin_env, device_id, operator, image_bytes())
    assert status == 201, image
    _body, record = submit_and_validate(pin_env, device_id, operator,
                                        fake_clip("WEBM", codec="VP9"),
                                        "loop.webm")
    video = {"pinRequestId": record["pin_request_id"]}

    status, image_view = invoke(pin_env, "GET", device_id, operator,
                                sub_path="/static-image")
    assert status == 200
    assert image_view["latest"]["pinRequestId"] == image["pinRequestId"]
    assert [h["pinRequestId"] for h in image_view["history"]] == [
        image["pinRequestId"]]
    assert "validatedMetadata" not in image_view["latest"]
    assert "validation" not in image_view

    status, video_view = invoke(pin_env, "GET", device_id, operator,
                                sub_path="/static-video")
    assert status == 200
    assert video_view["latest"]["pinRequestId"] == video["pinRequestId"]
    assert video_view["latest"]["status"] == "pending"
    assert video_view["latest"]["validatedMetadata"]["codec"] == "VP9"
    assert [h["pinRequestId"] for h in video_view["history"]] == [
        video["pinRequestId"]]
    assert "connectivity" in video_view
    assert video_view["validation"]["status"] == "accepted"
    assert video_view["validation"]["pinRequestId"] == video["pinRequestId"]
    assert video_view["validation"]["fileName"] == "loop.webm"

    status, removal = invoke(pin_env, "DELETE", device_id, operator,
                             sub_path="/static-video/pin")
    assert status == 200, removal
    videos = {item["pin_request_id"]: item["status"]
              for item in video_pin_request_items(pin_env, device_id)}
    assert videos == {video["pinRequestId"]: "superseded",
                      removal["pinRequestId"]: "pending"}
    (image_item,) = pin_request_items(pin_env, device_id)
    assert image_item["status"] == "pending"
    last = fakes.shadow.updates[-1]["payload"]["state"]
    assert set(last["desired"]) == {"staticVideoPin"}
    assert last["desired"]["staticVideoPin"]["op"] == "remove"
    assert last["desired"]["staticVideoPin"]["bucket"] is None
    assert len(audit_events(pin_env, device_id,
                            action="remove_static_video")) == 1


# --- ingest routing ---------------------------------------------------------------------


def _ingest(pin_env, device_id, reported):
    record = {
        "messageId": str(uuid.uuid4()),
        "body": json.dumps({
            "thing_name": device_id,
            "current": {"state": {"reported": reported}, "version": 1},
        }),
    }
    result = pin_env.module.camera_sync.handler({"Records": [record]}, None)
    assert result == {"batchItemFailures": []}


def _echo(item_or_body, op, status="applied", **extra):
    request_id = item_or_body.get("pinRequestId") or \
        item_or_body.get("pin_request_id")
    document = {"requestId": request_id, "op": op, "status": status,
                "completedAtEpochMs": 1_790_000_100_000}
    document.update(extra)
    return document


_VIDEO_ENTRY = {
    "name": "Static Video Camera", "type": "StaticVideo",
    "origin": "edge-discovered", "params": {},
    "capabilities": {"staticVideo": {"id": "static-video-camera",
                                     "fps": 29.97002997002997}},
    "discovered": True, "absent": False, "version": 1,
}


@pytest.fixture
def recording_iot(pin_env, monkeypatch):
    writes = []

    class _Client:
        def update_thing_shadow(self, thingName, shadowName, payload):
            writes.append(json.loads(payload))
            return {}

    monkeypatch.setattr(pin_env.module.camera_sync, "iot_data_client",
                        lambda usecase_id: _Client())
    return writes


def test_video_confirmation_applies_only_the_video_request(
        pin_env, usecase, operator, fakes, stub_runner, recording_iot):
    device_id = register_device(pin_env, usecase)
    status, image = submit_pin(pin_env, device_id, operator, image_bytes())
    _body, record = submit_and_validate(pin_env, device_id, operator,
                                        fake_clip("MP4"))
    assert record["status"] == "accepted", record
    video = {"pinRequestId": record["pin_request_id"]}
    metadata = {"fileName": "scene.mp4", "format": "MP4", "codec": "H264",
                "width": 32, "height": 24, "fps": 12.0, "frameCount": 24,
                "durationMs": 2000, "fileSizeBytes": 123,
                "pinnedAtEpochMs": 1_790_000_000_000}

    _ingest(pin_env, device_id, {
        "cameras": {"static-video-camera": _VIDEO_ENTRY},
        "staticVideoPin": _echo(video, "pin", metadata=metadata),
    })

    (video_item,) = video_pin_request_items(pin_env, device_id)
    assert video_item["status"] == "applied"
    assert float(video_item["device_metadata"]["fps"]) == 12.0
    (image_item,) = pin_request_items(pin_env, device_id)
    assert image_item["status"] == "pending"
    status, view = invoke(pin_env, "GET", device_id, operator,
                          sub_path="/static-video")
    assert view["deviceMetadata"]["frameCount"] == 24
    assert view["deviceReported"] == {"present": True, "absent": False}
    entry = pin_env.registry.get_item(Key={
        "device_id": device_id, "sk": "CAMERA#static-video-camera"})["Item"]
    assert entry["type"] == "StaticVideo"
    assert recording_iot == []


def test_applied_video_remove_marks_the_entry_absent(
        pin_env, usecase, operator, fakes, stub_runner, recording_iot):
    device_id = register_device(pin_env, usecase)
    _ingest(pin_env, device_id,
            {"cameras": {"static-video-camera": _VIDEO_ENTRY}})
    status, removal = invoke(pin_env, "DELETE", device_id, operator,
                             sub_path="/static-video/pin")
    assert status == 200, removal

    # The echo arrives with the stale present entry still merged in.
    _ingest(pin_env, device_id, {
        "cameras": {"static-video-camera": _VIDEO_ENTRY},
        "staticVideoPin": _echo(removal, "remove"),
    })

    entry = pin_env.registry.get_item(Key={
        "device_id": device_id, "sk": "CAMERA#static-video-camera"})["Item"]
    assert entry["absent"] is True
    assert int(entry["absent_since"]) == 1_790_000_100_000
    (item,) = video_pin_request_items(pin_env, device_id)
    assert item["status"] == "applied"
    assert recording_iot == []  # no shadow-key cleanup for the video camera


def test_omitted_video_entry_is_kept_absent_not_deleted(
        pin_env, usecase, recording_iot):
    device_id = register_device(pin_env, usecase)
    _ingest(pin_env, device_id,
            {"reportedAt": 1_790_000_000_000,
             "cameras": {"static-video-camera": _VIDEO_ENTRY}})

    _ingest(pin_env, device_id, {"reportedAt": 1_790_000_500_000,
                                 "cameras": {}})

    entry = pin_env.registry.get_item(Key={
        "device_id": device_id, "sk": "CAMERA#static-video-camera"}).get("Item")
    assert entry is not None
    assert entry["absent"] is True
    assert int(entry["absent_since"]) == 1_790_000_500_000


# --- deployment validator ------------------------------------------------------------------


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments inside the moto mock (the established pattern)."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments as module

    return module


def test_static_video_entries_bind_to_aravis_nodes(deployments):
    assert deployments._camera_source_type_compatible(
        "aravis_camera_source", "StaticVideo") is True
    assert deployments._camera_source_type_compatible(
        "icam_source", "StaticVideo") is False
    assert deployments._camera_source_type_compatible(
        "csi_camera_source", "StaticVideo") is False
