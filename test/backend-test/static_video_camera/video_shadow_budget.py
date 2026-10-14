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
"""Shared fixtures for the ``dda-camera-registry`` shadow budget tests
(static-camera-video-loop, Requirement 10.5 and task 12): a fat camera
inventory that forces capability truncation, and both pin slots at their
measured worst case (a desired section plus its reported echo each)."""
import json
import os

from camera_sync import CameraSourceState
from camera_sync.agent import make_video_pin_worker
from camera_sync.inventory import ORIGIN_EDGE_CONFIGURED
from utils.static_image_camera import MAX_PIN_FILE_BYTES
from utils.video_loop import MAX_PIN_VIDEO_BYTES

SHADOW = "dda-camera-registry"

#: A representative applied video metadata block.
VIDEO_METADATA = {"fileName": "scene.mp4", "format": "MP4", "codec": "H264",
                  "width": 64, "height": 48, "fps": 29.97002997002997,
                  "frameCount": 30, "durationMs": 1001,
                  "fileSizeBytes": 12345, "pinnedAtEpochMs": 1_790_000_000_000}


def encoded_size(document):
    """Bytes of the compact JSON encoding (what the shadow limit counts)."""
    return len(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def fat_inventory(count=12):
    """``count`` configured cameras with 8 formats x 8 resolutions each: far
    over any report cap before truncation."""
    formats = [{"pixelFormat": "FMT{}".format(i),
                "resolutions": [[3840 + i, 2160 + j] for j in range(8)]}
               for i in range(8)]
    return [
        CameraSourceState(
            camera_source_id="cfg-is-{}".format(index),
            name="Inspection camera {}".format(index),
            type="Camera",
            origin=ORIGIN_EDGE_CONFIGURED,
            params={"devicePath": "/dev/video{}".format(index),
                    "cameraId": "camera-{:04d}".format(index),
                    "location": "line-{} station-{}".format(index, index)},
            capabilities={"formats": formats, "driver": "uvcvideo",
                          "busInfo": "usb-0000:00:14.0-{}".format(index),
                          "kind": "v4l2"},
            discovered=True,
        )
        for index in range(count)
    ]


def worst_case_desired(video):
    """A Portal desired pin document at its longest."""
    thing = "t" * 128  # the longest AWS IoT thing name
    request_id = "20991231T235959-abcdef12"
    return {
        "requestId": request_id,
        "op": "pin",
        "bucket": "b" * 63,  # the longest S3 bucket name
        "key": "static-image-pins/{}/{}{}".format(
            thing, "video/" if video else "", request_id),
        "sha256": "a" * 64,
        "sizeBytes": MAX_PIN_VIDEO_BYTES if video else MAX_PIN_FILE_BYTES,
        "format": "WEBM" if video else "JPEG",
        "fileName": "f" * 128,  # the Portal's file-name bound
        "requestedAtEpochMs": 1_790_000_000_000,
    }


class _NullShadow:
    def get_thing_shadow_state_request(self, thing_name, shadow_name):
        return None

    def update_thing_shadow_state_request(self, thing_name, shadow_name, state):
        return None


def worst_case_pin_slots(marker_dir):
    """Both pin slots at their worst case, one variant per video outcome:
    a list of ``(desired_sections, reported_sections)`` pairs, the video
    echo applied with the largest metadata or failed with its reason at
    the bound."""
    image_desired = worst_case_desired(video=False)
    image_echo = dict(image_desired, status="applied", completedAtEpochMs=1,
                      metadata={"width": 65_535, "height": 65_535,
                                "format": "JPEG", "fileName": "f" * 128})
    video_desired = worst_case_desired(video=True)
    metadata = dict(VIDEO_METADATA, fileName="f" * 128, width=4096,
                    height=4096, frameCount=10_000_000,
                    durationMs=10_000_000_000_000,
                    fileSizeBytes=MAX_PIN_VIDEO_BYTES,
                    fps=0.30000000000000004)
    worker = make_video_pin_worker(_NullShadow(), "test-thing", SHADOW,
                                   marker_path=os.path.join(str(marker_dir),
                                                            "m.json"))
    video_applied = worker._build_report(video_desired, "applied", None,
                                         metadata, 1_790_000_000_000)
    video_failed = worker._build_report(video_desired, "failed", "r" * 5000,
                                        None, 1_790_000_000_000)
    desired = {"staticImagePin": image_desired, "staticVideoPin": video_desired}
    return [
        (desired, {"staticImagePin": image_echo, "staticVideoPin": echo})
        for echo in (video_applied, video_failed)
    ]


def shadow_state(report, desired_sections, reported_sections):
    """The whole state document: the camera report next to both slots."""
    return {"desired": dict(desired_sections),
            "reported": dict(report, **reported_sections)}
