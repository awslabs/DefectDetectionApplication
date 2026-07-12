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
"""Vendored ``auth.py`` + portal pins + vendored ``urllib3`` sha256 golden
(Req 3.5, 3.6) -- dependency CVE spec.

Spec: security-dependency-cve-fixes -- Property 2: Preservation.

Record sha256 baselines for the out-of-scope references that MUST stay
byte-for-byte unchanged:

* ``edge-cv-portal/backend/layers/shared/python/requests/auth.py`` -- the
  vendored HTTP Digest-auth code (B324 md5/sha1 at 148/156/205). F3 is a
  DOCUMENTED suppression in the audit gate, NOT a code edit, so this file is
  unchanged (Req 3.5).
* the portal ``layers/jwt/requirements.txt`` and ``functions/requirements.txt``
  (``requests==2.31.0``) -- out of scope, NOT bumped (Req 3.6).
* the vendored ``urllib3`` package -- ``urllib3 2.6.3`` has zero md5/sha1 usage;
  nothing to change (Req 3.6).

The golden metadata also records the Req 3.4 effective-runtime-``requests`` note:
the edge Dockerfiles run ``pip install --upgrade requests`` AFTER installing
``requirements.txt``, so the F2 bump is reproducibility / scan-cleanliness only
and does not change the runtime ``requests`` version.

Golden: ``baselines/dependency_baseline_out_of_scope.json``.

**Validates: Requirements 3.4, 3.5, 3.6**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_dependency_out_of_scope.py \
        -p no:cacheprovider --noconftest -v
"""
from _dependency_preservation_support import (
    PORTAL_FUNCTIONS_REQS_REL,
    PORTAL_JWT_REQS_REL,
    VENDORED_AUTH_REL,
    VENDORED_URLLIB3_DIR_REL,
    capture_or_assert_json,
    import_audit,
    read_lines,
    sha256_file,
    sha256_tree,
)

_GOLDEN = "dependency_baseline_out_of_scope.json"

_RUNTIME_NOTE = (
    "Req 3.4: the edge Dockerfiles run 'pip install --upgrade requests' AFTER "
    "installing requirements.txt, so the effective runtime requests version is "
    "unchanged; the F2 pin bump is reproducibility / scan-cleanliness only."
)


def _current_golden():
    return {
        "note": _RUNTIME_NOTE,
        "vendored_requests_auth_py_sha256": sha256_file(VENDORED_AUTH_REL),
        "portal_jwt_requirements_sha256": sha256_file(PORTAL_JWT_REQS_REL),
        "portal_functions_requirements_sha256": sha256_file(PORTAL_FUNCTIONS_REQS_REL),
        "vendored_urllib3": sha256_tree(VENDORED_URLLIB3_DIR_REL),
    }


# Validates: Requirements 3.4, 3.5, 3.6
def test_out_of_scope_sha256_golden():
    """The vendored ``auth.py``, both portal ``requirements.txt`` pins, and the
    vendored ``urllib3`` package match the captured sha256 golden byte-for-byte."""
    current = _current_golden()
    recorded = capture_or_assert_json(_GOLDEN, current)
    assert current == recorded


# Validates: Requirements 3.6
def test_portal_pins_are_requests_2_31_0_and_out_of_scope():
    """The portal pins record ``requests==2.31.0`` (out of scope, NOT the two
    in-scope pin files), so the audit never parses/bumps them."""
    for rel_path in (PORTAL_JWT_REQS_REL, PORTAL_FUNCTIONS_REQS_REL):
        lines = read_lines(rel_path)
        assert any(ln.strip() == "requests==2.31.0" for ln in lines), (
            f"{rel_path} should pin requests==2.31.0 (out of scope), got: {lines!r}"
        )


# Validates: Requirements 3.5, 3.6
def test_out_of_scope_paths_not_in_audit_scope():
    """The vendored/portal paths are NOT in the audit's ``IN_SCOPE_PIN_FILES`` so
    they are never parsed/flagged by the gate."""
    import os

    audit = import_audit()
    for rel_path in (
        VENDORED_AUTH_REL,
        PORTAL_JWT_REQS_REL,
        PORTAL_FUNCTIONS_REQS_REL,
    ):
        assert os.path.normpath(rel_path) not in audit.IN_SCOPE_PIN_FILES, (
            f"{rel_path} must be out of the audit's in-scope pin files"
        )
