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
"""The debug redaction module exists twice and must stay byte-identical
(workflow-debug-mode design §5.5; tasks.md 2.1, decision P3): the device copy
``src/backend/workflow_engine/telemetry/debug/redact.py`` and the Portal copy
``edge-cv-portal/backend/functions/workflow_debug/redact.py``.

Skipped with the reason "Portal copy not present" until the Portal package
lands (debug tasks.md 3.4).
"""
import hashlib
from pathlib import Path

import pytest

DEVICE_RELATIVE = Path("src/backend/workflow_engine/telemetry/debug/redact.py")
PORTAL_RELATIVE = Path("edge-cv-portal/backend/functions/workflow_debug/redact.py")


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / DEVICE_RELATIVE).is_file() and (candidate / "edge-cv-portal").is_dir():
            return candidate
    raise AssertionError("Could not locate the repository root containing " + str(DEVICE_RELATIVE))


def test_device_and_portal_redact_copies_are_byte_identical():
    root = _repo_root()
    portal = root / PORTAL_RELATIVE
    if not portal.is_file():
        pytest.skip("Portal copy not present")
    device_bytes = (root / DEVICE_RELATIVE).read_bytes()
    portal_bytes = portal.read_bytes()
    assert device_bytes == portal_bytes, (
        "redact.py copies differ:\n  device sha256=%s (%s)\n  portal sha256=%s (%s)\n"
        "Copy one over the other: cp %s %s" % (
            hashlib.sha256(device_bytes).hexdigest(), DEVICE_RELATIVE,
            hashlib.sha256(portal_bytes).hexdigest(), PORTAL_RELATIVE,
            DEVICE_RELATIVE, PORTAL_RELATIVE,
        )
    )
