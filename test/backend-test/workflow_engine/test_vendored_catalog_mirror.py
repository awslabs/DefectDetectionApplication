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
