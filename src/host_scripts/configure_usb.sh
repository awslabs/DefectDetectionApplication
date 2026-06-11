#!/bin/bash
#
#
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
# Raises the usbfs memory limit used by libusb for USB transfers.
#
# USB3Vision / GenICam cameras (e.g. Basler ace USB 3.0) stream large frames
# through usbfs. The kernel default of 16 MB is too small for high-resolution
# cameras and leads to failed buffer allocations ("No space left on device" /
# bootstrap and streaming failures) even when the camera is detected.
#
# This sets /sys/module/usbcore/parameters/usbfs_memory_mb to USBFS_MEMORY_MB
# (default 1000). It only ever raises the value, never lowers it, so it is safe
# to run repeatedly and will not override a larger value set elsewhere. The
# setting is applied at runtime; because the LocalServer component Run lifecycle
# executes on every component start (including boot), it is re-applied
# automatically and does not require a kernel boot parameter.

set -u

USBFS_PARAM_PATH="/sys/module/usbcore/parameters/usbfs_memory_mb"
DESIRED_USBFS_MEMORY_MB="${USBFS_MEMORY_MB:-1000}"

if [ ! -w "${USBFS_PARAM_PATH}" ]; then
    echo "configure_usb: ${USBFS_PARAM_PATH} not present or not writable; skipping usbfs tuning."
    exit 0
fi

current_value="$(cat "${USBFS_PARAM_PATH}" 2>/dev/null || echo 0)"

# Only raise the limit; never reduce a value that is already higher.
if [ "${current_value}" -ge "${DESIRED_USBFS_MEMORY_MB}" ]; then
    echo "configure_usb: usbfs_memory_mb already ${current_value} MB (>= ${DESIRED_USBFS_MEMORY_MB} MB); leaving unchanged."
    exit 0
fi

if echo "${DESIRED_USBFS_MEMORY_MB}" > "${USBFS_PARAM_PATH}" 2>/dev/null; then
    echo "configure_usb: raised usbfs_memory_mb from ${current_value} MB to ${DESIRED_USBFS_MEMORY_MB} MB."
else
    # Non-fatal: USB3 cameras may still work for smaller frames. Do not block startup.
    echo "configure_usb: WARNING failed to set usbfs_memory_mb to ${DESIRED_USBFS_MEMORY_MB} MB (current ${current_value} MB)."
fi

exit 0
