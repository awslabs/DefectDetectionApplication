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
"""Smoke test: the vendored workflow_core catalog mirror stays byte-identical.

The edge workflow engine ships a vendored copy of the portal's workflow_core
catalog at ``src/backend/workflow_engine/vendor/workflow_core``. Whenever the
portal layer copy changes (e.g. new node descriptors such as
``custom_python_preprocess``, or catalog data-model changes such as
``CATEGORY_TRIGGER`` in ``models.py``), the mirror must be re-synced so both
sides of the system agree on the node catalog.

Validates: Requirements 1.6, 3.10 (custom-python-frames)
Validates: Requirements 1.3, 6.5 (triggers-stage-and-unified-input)
Validates: Requirements 1.7, 13.9, 14.6, 15.6 (rtsp-rtmp-stream-cameras)
"""

import hashlib
from pathlib import Path

import pytest

PORTAL_CATALOG_RELATIVE = Path(
    "edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog"
)
VENDORED_CATALOG_RELATIVE = Path(
    "src/backend/workflow_engine/vendor/workflow_core/catalog"
)

# Both catalog sources must stay byte-identical between the portal layer and
# the edge vendor mirror: nodes.py (descriptors) and models.py (the catalog
# data model: CATEGORY_TRIGGER, PORT_TYPE_EVENT_SIGNAL, trigger port wiring).
MIRRORED_FILENAMES = ("nodes.py", "models.py")

PORTAL_PACKAGE_RELATIVE = Path(
    "edge-cv-portal/backend/layers/workflow_core/python/workflow_core"
)
VENDORED_PACKAGE_RELATIVE = Path(
    "src/backend/workflow_engine/vendor/workflow_core"
)

# Package-root modules that must stay byte-identical too.
# ``anomaly_invocation.py`` is the shared Invocation_Builder
# (quality-prompt-tuning Requirements 6.1, 6.2): the executor, the Portal's
# Bedrock_Scorer and the device's Device_Score_Job runner must build the
# same request, which only holds while the vendored copy is an exact mirror
# — so a hand edit under ``vendor/`` (or a forgotten ``re_vendor.sh``) has
# to fail the suite.
MIRRORED_PACKAGE_FILENAMES = ("anomaly_invocation.py",)

# Shared rule modules that must stay byte-identical, given as paths relative to
# the package root (``workflow_core/``) so sub-packages are covered too.
#
# rtsp-rtmp-stream-cameras keeps its rules in workflow_core precisely so that
# the Portal and the LocalServer cannot disagree:
#   * ``stream_url.py`` — the Stream_URL checker, normalizer and redactor. The
#     Portal validates a URL before it is stored and the device validates the
#     same URL again before it connects (Requirements 1.7, 6.1, 6.3).
#   * ``analytics/`` — the detection counter, the association matcher and the
#     event-gate automaton. The device and the Portal's cloud test sandbox must
#     produce identical metadata for identical inputs (Requirements 13.9, 14.6,
#     15.6), which only holds while both trees run the same code.
#   * ``validator/checks.py`` and ``validator/__init__.py`` — the validator
#     rules (the generalized V7 plus V11, V12, V13 and W3) and the codes they
#     are re-exported under. The device re-validates a compiled document, so a
#     drifted copy would accept on one side what the other side rejects.
#
# Re-sync these with a per-file ``cp``, not with ``re_vendor.sh``: a wholesale
# re-vendor would also copy the portal-only ``catalog/platforms.py``, which the
# vendor tree deliberately omits.
MIRRORED_SHARED_MODULE_RELPATHS = (
    "stream_url.py",
    "analytics/__init__.py",
    "analytics/scene.py",
    "validator/checks.py",
    "validator/__init__.py",
)


def _repo_root() -> Path:
    """Walk up from this file until both catalog copies are present."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / PORTAL_CATALOG_RELATIVE / "nodes.py").is_file() and (
            candidate / VENDORED_CATALOG_RELATIVE / "nodes.py"
        ).is_file():
            return candidate
    raise AssertionError(
        "Could not locate the repository root containing both "
        f"{PORTAL_CATALOG_RELATIVE} and {VENDORED_CATALOG_RELATIVE}"
    )


# Feature: triggers-stage-and-unified-input, Property 7: Catalog copies stay byte-identical
@pytest.mark.parametrize("filename", MIRRORED_FILENAMES)
def test_vendored_catalog_file_is_byte_identical_to_portal_copy(filename):
    root = _repo_root()
    portal_relative = PORTAL_CATALOG_RELATIVE / filename
    vendored_relative = VENDORED_CATALOG_RELATIVE / filename

    portal_path = root / portal_relative
    vendored_path = root / vendored_relative
    assert portal_path.is_file(), portal_path
    assert vendored_path.is_file(), vendored_path

    portal_bytes = portal_path.read_bytes()
    vendored_bytes = vendored_path.read_bytes()

    portal_sha = hashlib.sha256(portal_bytes).hexdigest()
    vendored_sha = hashlib.sha256(vendored_bytes).hexdigest()

    assert portal_bytes == vendored_bytes, (
        "Vendored workflow_core catalog mirror is out of sync with the portal "
        f"layer copy.\n  portal   sha256={portal_sha} ({portal_relative})\n"
        f"  vendored sha256={vendored_sha} ({vendored_relative})\n"
        "Re-sync with: cp "
        f"{portal_relative} {vendored_relative}"
    )


# Feature: quality-prompt-tuning, Property 6: Executor, Bedrock_Scorer and
# Device_Score_Job build identical invocations (the vendoring precondition)
@pytest.mark.parametrize("filename", MIRRORED_PACKAGE_FILENAMES)
def test_vendored_package_module_is_byte_identical_to_portal_copy(filename):
    root = _repo_root()
    portal_relative = PORTAL_PACKAGE_RELATIVE / filename
    vendored_relative = VENDORED_PACKAGE_RELATIVE / filename

    portal_path = root / portal_relative
    vendored_path = root / vendored_relative
    assert portal_path.is_file(), portal_path
    assert vendored_path.is_file(), (
        f"{vendored_relative} is missing — run "
        "src/backend/workflow_engine/vendor/re_vendor.sh"
    )

    portal_bytes = portal_path.read_bytes()
    vendored_bytes = vendored_path.read_bytes()

    portal_sha = hashlib.sha256(portal_bytes).hexdigest()
    vendored_sha = hashlib.sha256(vendored_bytes).hexdigest()

    assert portal_bytes == vendored_bytes, (
        "Vendored workflow_core module is out of sync with the portal layer "
        f"copy.\n  portal   sha256={portal_sha} ({portal_relative})\n"
        f"  vendored sha256={vendored_sha} ({vendored_relative})\n"
        "Re-sync with: src/backend/workflow_engine/vendor/re_vendor.sh"
    )


# Feature: rtsp-rtmp-stream-cameras, Requirement 1.7 (both mirrored copies carry
# the new node types and rules identically) and the precondition for Property 27
# (analytics parity between the device and the sandbox)
@pytest.mark.parametrize("relpath", MIRRORED_SHARED_MODULE_RELPATHS)
def test_vendored_shared_module_is_byte_identical_to_portal_copy(relpath):
    root = _repo_root()
    portal_relative = PORTAL_PACKAGE_RELATIVE / relpath
    vendored_relative = VENDORED_PACKAGE_RELATIVE / relpath

    portal_path = root / portal_relative
    vendored_path = root / vendored_relative
    assert portal_path.is_file(), portal_path
    assert vendored_path.is_file(), (
        f"{vendored_relative} is missing — copy it from the portal layer: "
        f"cp {portal_relative} {vendored_relative}"
    )

    portal_bytes = portal_path.read_bytes()
    vendored_bytes = vendored_path.read_bytes()

    portal_sha = hashlib.sha256(portal_bytes).hexdigest()
    vendored_sha = hashlib.sha256(vendored_bytes).hexdigest()

    assert portal_bytes == vendored_bytes, (
        "Vendored workflow_core shared rule module is out of sync with the "
        "portal layer copy, so the Portal and the LocalServer no longer apply "
        "the same rules.\n"
        f"  portal   sha256={portal_sha} ({portal_relative})\n"
        f"  vendored sha256={vendored_sha} ({vendored_relative})\n"
        "Re-sync with: cp "
        f"{portal_relative} {vendored_relative}\n"
        "(Do not run re_vendor.sh: it would also copy the portal-only "
        "catalog/platforms.py.)"
    )
