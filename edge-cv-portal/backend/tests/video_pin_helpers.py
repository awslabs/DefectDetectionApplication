"""
Shared helpers for the Portal_Video_Pin_API tests (static-camera-video-loop).

- Video payloads: real clips written with OpenCV's ``VideoWriter`` (MJPEG in
  AVI, MPEG-4 in MP4) when OpenCV is installed — the video layer's library,
  present in the video test image — and synthetic "fake clips" otherwise: a
  real container signature followed by a marker the stub probe reads.
- Probe runners for ``camera_registry.run_video_probe``: the real probe
  in-process (``video_loop.probe_video``) and a stub that honors the real
  sniff plus the fake-clip marker, with ``probe_runner()`` picking the real
  one whenever OpenCV is available.
- Route helpers over the ``pin_env`` fixture (``pin_route_helpers``).
"""
import json
import os
import tempfile
import uuid

from pin_route_helpers import STAGING_PREFIX, invoke

VIDEO_SK_PREFIX = "VIDEO_PIN_REQUEST#"

#: Container signatures (the shared golden vectors' heads).
CONTAINER_HEADS = {
    "MP4": bytes.fromhex("000000186674797069736f6d0000020069736f6d6d703431"),
    "MOV": bytes.fromhex("0000001466747970717420200000000071742020"),
    "AVI": b"RIFF\x24\x00\x00\x00AVI LIST",
    "MKV": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x88matroska",
    "WEBM": b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm",
}

FAKE_CLIP_MARKER = b"\nDDA-FAKE-CLIP:"


def cv2_unavailable_reason():
    """None when OpenCV imports, else why not."""
    try:
        import cv2  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return "OpenCV (the video layer) is not installed: {}".format(exc)
    return None


def fake_clip(container, decodable=True, codec="H264", width=32, height=24,
              fps=12.0, frame_count=24, padding=0):
    """A synthetic payload: ``container``'s signature, then the marker and
    the metadata the stub probe reports (or its undecodable verdict)."""
    info = {"decodable": decodable, "codec": codec, "width": width,
            "height": height, "fps": fps, "frameCount": frame_count}
    return (CONTAINER_HEADS[container] + b"\x00" * 48 + FAKE_CLIP_MARKER
            + json.dumps(info).encode("ascii") + b"\n" + b"\x00" * padding)


def stub_probe(path):
    """The stub runner: the real sniff decides "not a video"; the fake-clip
    marker decides decodability; anything else sniffable is undecodable."""
    import video_loop

    with open(path, "rb") as handle:
        data = handle.read()
    container = video_loop.sniff_video_container(data[:video_loop.SNIFF_BYTES])
    if container is None:
        return {"ok": False, "error": video_loop.not_a_video_message()}
    index = data.find(FAKE_CLIP_MARKER)
    if index < 0:
        return {"ok": False, "error": video_loop.undecodable_message("unknown")}
    line = data[index + len(FAKE_CLIP_MARKER):].split(b"\n", 1)[0]
    info = json.loads(line.decode("ascii"))
    if not info.pop("decodable"):
        return {"ok": False,
                "error": video_loop.undecodable_message(info["codec"])}
    metadata = video_loop.VideoInfo(
        container=container, codec=info["codec"], width=info["width"],
        height=info["height"], fps=info["fps"],
        frame_count=info["frameCount"]).as_metadata()
    return {"ok": True, "info": metadata}


def real_probe(path):
    """The real probe, in-process (the child runs exactly this)."""
    import video_loop

    try:
        info = video_loop.probe_video(path)
    except video_loop.VideoValidationError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "info": info.as_metadata()}


def probe_runner():
    """(mode, runner): the real probe when OpenCV is available."""
    if cv2_unavailable_reason() is None:
        return "real", real_probe
    return "stub", stub_probe


def run_on_bytes(runner, payload):
    """The runner's verdict for ``payload`` (the tests' oracle)."""
    fd, path = tempfile.mkstemp(prefix="oracle-video-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        return runner(path)
    finally:
        os.remove(path)


def write_clip(directory, name, fourcc, size, fps, frames):
    """A real clip written with OpenCV's VideoWriter; returns its bytes, or
    None when this OpenCV build cannot write the codec."""
    import cv2
    import numpy

    path = os.path.join(directory, name)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc),
                             float(fps), size)
    if not writer.isOpened():
        return None
    width, height = size
    for index in range(frames):
        frame = numpy.full((height, width, 3),
                           (index * 37 % 256, index * 91 % 256, 128),
                           dtype=numpy.uint8)
        writer.write(frame)
    writer.release()
    with open(path, "rb") as handle:
        return handle.read()


def real_clips(directory):
    """name -> bytes for a few small real clips (MJPEG/AVI, MPEG-4/MP4)."""
    specs = [
        ("mjpeg_a.avi", "MJPG", (16, 16), 8, 4),
        ("mjpeg_b.avi", "MJPG", (32, 24), 25, 6),
        ("mpeg4_a.mp4", "mp4v", (32, 32), 12, 5),
        ("mpeg4_b.mp4", "mp4v", (48, 16), 30, 3),
    ]
    clips = {}
    for name, fourcc, size, fps, frames in specs:
        data = write_clip(directory, name, fourcc, size, fps, frames)
        if data:
            clips[name] = data
    return clips


def stage_video(env, payload):
    staging_key = f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    env.s3.put_object(Bucket=env.bucket, Key=staging_key, Body=payload)
    return staging_key


def submit_video(env, device_id, user, payload, file_name="scene.mp4"):
    """POST the pin route for a freshly staged payload: (status, body)."""
    staging_key = stage_video(env, payload)
    return invoke(env, "POST", device_id, user, sub_path="/static-video/pin",
                  body={"stagingKey": staging_key, "fileName": file_name})


def video_pin_request_items(env, device_id):
    import pin_requests

    return pin_requests.query_pin_request_items(
        env.registry, device_id, sk_prefix=VIDEO_SK_PREFIX)


# --- the asynchronous validation job ---------------------------------------------

VALIDATION_SK_PREFIX = "VIDEO_VALIDATION#"


def inline_dispatch(env):
    """A ``dispatch_video_validation`` seam running the job at once, inside
    the pin route (so a 202 response already has its outcome recorded)."""
    return lambda job: env.module.run_video_validation(job)


class QueuedDispatch:
    """A ``dispatch_video_validation`` seam that queues jobs for the test
    to run later, in any order."""

    def __init__(self, env):
        self.env = env
        self.jobs = []

    def __call__(self, job):
        self.jobs.append(dict(job))

    def run(self, index=0):
        return self.env.module.run_video_validation(self.jobs[index])

    def run_all(self):
        return [self.env.module.run_video_validation(job) for job in self.jobs]


def validation_records(env, device_id):
    """The device's VIDEO_VALIDATION# records, newest first."""
    import pin_requests

    return pin_requests.query_pin_request_items(
        env.registry, device_id, sk_prefix=VALIDATION_SK_PREFIX)


def validation_record(env, device_id, validation_id):
    import pin_requests

    return pin_requests.get_video_validation_item(env.registry, device_id,
                                                  validation_id)


def staged_object_exists(env, staging_key):
    try:
        env.s3.head_object(Bucket=env.bucket, Key=staging_key)
    except Exception:  # noqa: BLE001 — moto raises ClientError (404)
        return False
    return True
