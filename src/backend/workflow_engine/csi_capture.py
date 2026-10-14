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
"""NVIDIA CSI capture config write (csi-icam-input-nodes Requirement 7.1).

The NVIDIA CSI camera is captured by an on-device host service
(``nvidia-csi-capture.service``) that continuously stages frames to
``/aws_dda/nvidia-csi-capture/latest.jpg`` and reads its acquisition
settings (gain/exposure, optional crop) from
``/aws_dda/nvidia-csi-capture/config.json``.

``write_csi_config`` is the single place that config file is written. It
is factored out of ``gstreamer.pipeline_builder._add_nvidia_csi_image_source``
(the legacy Pipeline_Configuration path) so the deployed-workflow
executor writes the exact same config the legacy builder did. Writing is
tolerant: a failure is logged and swallowed (returns ``False``) so the
CSI capture service keeps its last-known settings and the run continues,
matching the legacy builder's behavior.

This module performs no GStreamer work and is importable without the
``gi`` runtime, so both the executor and its tests can use it directly.
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: The on-device NVIDIA CSI host capture service directory.
CSI_CAPTURE_DIR = "/aws_dda/nvidia-csi-capture"

#: Acquisition settings file the CSI host service reads.
CSI_CONFIG_FILE = os.path.join(CSI_CAPTURE_DIR, "config.json")

#: The frame the CSI host service continuously stages.
CSI_LATEST_JPG = os.path.join(CSI_CAPTURE_DIR, "latest.jpg")

#: Default acquisition settings (mirror the ``csi_camera_source``
#: descriptor defaults and the legacy builder fallbacks).
DEFAULT_GAIN = 4
DEFAULT_EXPOSURE = 5000000


def write_csi_config(gain: int = DEFAULT_GAIN,
                     exposure: int = DEFAULT_EXPOSURE,
                     crop: Optional[dict] = None,
                     config_file: str = CSI_CONFIG_FILE) -> bool:
    """Write the CSI host capture service acquisition config.

    Writes ``{"gain": int, "exposure": int, "crop"?: {...}}`` to
    ``config_file`` (creating its directory) and makes it world-writable
    so the host service can read it. Tolerant of I/O failures: logs and
    returns ``False`` (the service keeps its last-known settings), never
    raising — the legacy ``_add_nvidia_csi_image_source`` behavior.
    Returns ``True`` on a successful write.
    """
    try:
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        config = {"gain": gain, "exposure": exposure}
        if crop:
            config["crop"] = {
                "top": crop.get("top", 0),
                "bottom": crop.get("bottom", 0),
                "left": crop.get("left", 0),
                "right": crop.get("right", 0),
            }
        with open(config_file, "w") as handle:
            json.dump(config, handle)
        os.chmod(config_file, 0o666)
        logger.info(
            "NVIDIA CSI: updated config - gain=%s, exposure=%s, crop=%s",
            config["gain"], config["exposure"], config.get("crop", "none"),
        )
        return True
    except Exception as e:  # noqa: BLE001 - tolerant, matches legacy path
        logger.error("NVIDIA CSI: failed to write config file %s: %s",
                     config_file, e)
        return False
