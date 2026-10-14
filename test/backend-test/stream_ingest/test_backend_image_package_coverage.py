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
"""Every backend package is copied into every backend image
(rtsp-rtmp-stream-cameras, found on hardware in task 25.2).

The backend Dockerfiles copy ``src/backend`` package by package, so a new
top-level package needs its own ``COPY <package> ./<package>`` line in each
of them. ``stream_ingest`` had none: the JP7 ``1.0.50`` image built, passed
its gates (which import the package from the mounted repository) and then
crash-looped on the device with ``ModuleNotFoundError: No module named
'stream_ingest'``, and Greengrass rolled the deployment back.

This test needs no image: it compares each Dockerfile's COPY lines with the
top-level Python packages of ``src/backend``. The in-image half of the check
is in ``test_image_stream_components.py``.
"""
import os
import re

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND = os.path.join(_REPO, "src", "backend")
DOCKERFILES = ("Dockerfile", "Dockerfile.jp5", "Dockerfile.jp6", "Dockerfile.jp7", "Dockerfile.x86_64_nvidia")
COPY_LINE = re.compile(r"^COPY\s+([A-Za-z0-9_]+)/?\s+\./([A-Za-z0-9_]+)/?\s*$", re.M)


def backend_packages():
    """The top-level directories of ``src/backend`` that hold Python code."""
    packages = []
    for name in sorted(os.listdir(BACKEND)):
        path = os.path.join(BACKEND, name)
        if name.startswith((".", "__")) or not os.path.isdir(path):
            continue
        if any(file.endswith(".py") for _, _, files in os.walk(path) for file in files):
            packages.append(name)
    return packages


def copied_packages(dockerfile):
    with open(os.path.join(BACKEND, dockerfile), encoding="utf-8") as handle:
        return {source for source, target in COPY_LINE.findall(handle.read()) if source == target}


def test_the_backend_has_the_packages_this_check_expects():
    packages = backend_packages()
    assert {"stream_ingest", "workflow_engine", "camera_sync", "resources"} <= set(packages)


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_every_backend_package_has_a_copy_line(dockerfile):
    missing = [package for package in backend_packages() if package not in copied_packages(dockerfile)]
    assert not missing, (
        f"src/backend/{dockerfile} does not COPY {missing}: the image would lack them and the "
        "backend could not import them (add `COPY <package> ./<package>` next to the others)")
