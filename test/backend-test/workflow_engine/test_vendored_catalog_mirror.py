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
``custom_python_preprocess``), the mirror must be re-synced so both sides of
the system agree on the node catalog.

Validates: Requirements 1.6, 3.10 (custom-python-frames)
"""

import hashlib
from pathlib import Path

PORTAL_RELATIVE = Path(
    "edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py"
)
VENDORED_RELATIVE = Path(
    "src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py"
)


def _repo_root() -> Path:
    """Walk up from this file until both catalog copies are present."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / PORTAL_RELATIVE).is_file() and (
            candidate / VENDORED_RELATIVE
        ).is_file():
            return candidate
    raise AssertionError(
        "Could not locate the repository root containing both "
        f"{PORTAL_RELATIVE} and {VENDORED_RELATIVE}"
    )


def test_vendored_catalog_nodes_is_byte_identical_to_portal_copy():
    root = _repo_root()
    portal_bytes = (root / PORTAL_RELATIVE).read_bytes()
    vendored_bytes = (root / VENDORED_RELATIVE).read_bytes()

    portal_sha = hashlib.sha256(portal_bytes).hexdigest()
    vendored_sha = hashlib.sha256(vendored_bytes).hexdigest()

    assert portal_bytes == vendored_bytes, (
        "Vendored workflow_core catalog mirror is out of sync with the portal "
        f"layer copy.\n  portal   sha256={portal_sha} ({PORTAL_RELATIVE})\n"
        f"  vendored sha256={vendored_sha} ({VENDORED_RELATIVE})\n"
        "Re-sync with: cp "
        f"{PORTAL_RELATIVE} {VENDORED_RELATIVE}"
    )
