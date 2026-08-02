#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Device environment probes for the workflow engine.

Answers two questions the WorkflowWatcher needs when validating a
discovered Workflow_Component artifact set (Requirement 9.1):

- Which workflow_core target architecture does this device correspond
  to (``x86_64`` / ``arm64_jp4`` / ``arm64_jp5`` / ``arm64_jp6``)?
- Which LocalServer component version is running here (compared against
  the manifest's ``minLocalServerVersion``)?

Both are derived from the same signals the existing code uses:
``platform.machine()`` plus the ``LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH``
environment variable (see ``endpoints/system.py`` and
``gstreamer/pipeline_builder.py::_is_jp6``). No Greengrass IPC is used so
this module stays importable and testable off-device.
"""

import logging
import os
import platform
import re
from typing import Optional, Tuple

from workflow_engine.vendor.workflow_core.catalog import (
    ARCH_ARM64_JP4,
    ARCH_ARM64_JP5,
    ARCH_ARM64_JP6,
    ARCH_X86_64,
)

logger = logging.getLogger(__name__)

_DECOMPRESSED_PATH_ENV = "LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH"

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def device_arch() -> str:
    """The workflow_core architecture identifier for this device.

    x86 machines map to ``x86_64``. On aarch64 the JetPack generation is
    read from the LocalServer component path (variant names embed
    ``JP5``/``JP6``; the plain ``arm64`` variant is JetPack 4), the same
    signal ``pipeline_builder._is_jp6`` relies on.
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return ARCH_X86_64

    component_path = os.environ.get(_DECOMPRESSED_PATH_ENV, "")
    if "JP6" in component_path:
        return ARCH_ARM64_JP6
    if "JP5" in component_path:
        return ARCH_ARM64_JP5
    return ARCH_ARM64_JP4


def local_server_version() -> Optional[str]:
    """The running LocalServer component version, or None if unknown.

    Parsed from this component's own artifact path (ground truth for the
    running component — see endpoints/system.py for the full rationale):

        .../artifacts-unarchived/<component-name>/<version>/<component-name>-<arch>

    The Greengrass IPC fallback used by the system-health endpoint is
    deliberately not replicated here; when the version cannot be
    determined the compatibility check is skipped with a warning rather
    than blocking registration.
    """
    decompressed_path = os.environ.get(_DECOMPRESSED_PATH_ENV, "")
    if not decompressed_path:
        return None

    match = re.search(
        r"/(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)/[^/]+/?$", decompressed_path
    )
    if match:
        return match.group(1)
    return None


def parse_version(version: str) -> Optional[Tuple[int, int, int]]:
    """``"1.2.3"`` (with optional pre-release/build suffix) -> (1, 2, 3).

    Returns None when the string carries no ``major.minor.patch`` core,
    which callers treat as a malformed manifest value.
    """
    if not isinstance(version, str):
        return None
    match = _SEMVER_RE.search(version)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_version_compatible(min_version: str, running_version: Optional[str]) -> bool:
    """True when ``running_version`` satisfies ``min_version``.

    An unknown running version cannot be proven incompatible, so the
    check passes with a warning (development/off-device environments).
    A malformed ``min_version`` must be rejected by the caller before
    calling this (``parse_version`` returning None).
    """
    minimum = parse_version(min_version)
    if minimum is None:
        raise ValueError(f"Unparseable minLocalServerVersion: {min_version!r}")

    if running_version is None:
        logger.warning(
            "LocalServer version unknown; skipping minLocalServerVersion "
            "compatibility check for minimum %s",
            min_version,
        )
        return True

    running = parse_version(running_version)
    if running is None:
        logger.warning(
            "Running LocalServer version %r is unparseable; skipping "
            "minLocalServerVersion compatibility check",
            running_version,
        )
        return True

    return running >= minimum
