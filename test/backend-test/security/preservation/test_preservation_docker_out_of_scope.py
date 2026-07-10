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
"""Out-of-scope untouched golden — Docker non-ECR base image spec (Req 3.6, 3.7).

Spec: security-docker-non-ecr-base-image-fixes — Property 2: Preservation.

This spec's fix (D1–D6) MUST NOT touch anything outside the four maintained
Jetson Dockerfiles. This test records the sha256 of the out-of-scope files on the
UNFIXED tree and asserts they stay byte-for-byte unchanged:

  * The already-compliant ``public.ecr.aws/*`` Dockerfiles — ``src/backend/Dockerfile``,
    ``src/frontend/Dockerfile``, ``src/edgemlsdk/Dockerfile`` (Req 3.7).
  * The vendored / regenerated duplicate ``src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5|jp6``
    (Req 3.7).
  * ``src/docker-compose.yaml`` — no compose change is required for the default
    build (Req 3.6).

Golden: ``baselines/docker_baseline_out_of_scope.json`` (sha256 per file). On the
unfixed tree it is captured; task 8 re-runs it after the fix to prove the fix
stayed inside its four in-scope Dockerfiles (plus the audit module + build-custom).

Mirrors the sibling ``test_preservation_s3_out_of_scope_guard.py`` convention.

**Validates: Requirements 3.6, 3.7**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_docker_out_of_scope.py \
        -p no:cacheprovider --noconftest -v
"""
import hashlib
import os

from _docker_preservation_support import REPO_ROOT, capture_or_assert_json

# Already-compliant public.ecr.aws/* Dockerfiles (Req 3.7).
ECR_DOCKERFILES = (
    "src/backend/Dockerfile",
    "src/frontend/Dockerfile",
    "src/edgemlsdk/Dockerfile",
)
# Vendored / regenerated duplicate (Req 3.7).
VENDORED_DUPLICATES = (
    "src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5",
    "src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp6",
)
# Compose (Req 3.6).
COMPOSE_FILES = (
    "src/docker-compose.yaml",
)

ALL_OUT_OF_SCOPE = ECR_DOCKERFILES + VENDORED_DUPLICATES + COMPOSE_FILES


def _sha256(rel):
    with open(os.path.join(REPO_ROOT, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# Validates: Requirements 3.6, 3.7
def test_out_of_scope_files_unchanged():
    """Capture / assert the sha256 of every out-of-scope file."""
    # All recorded files must exist on the tree being tested.
    missing = [rel for rel in ALL_OUT_OF_SCOPE
               if not os.path.exists(os.path.join(REPO_ROOT, rel))]
    assert not missing, f"expected out-of-scope files to exist: {missing}"

    hashes = {rel: _sha256(rel) for rel in ALL_OUT_OF_SCOPE}
    capture_or_assert_json("docker_baseline_out_of_scope.json", hashes)


# Validates: Requirements 3.7 — the vendored duplicate is genuinely under the
# regenerated edgemlsdk/edgemlsdk subtree (guards against recording the wrong path).
def test_vendored_duplicates_are_under_vendored_subtree():
    for rel in VENDORED_DUPLICATES:
        assert os.path.join("edgemlsdk", "edgemlsdk") in rel, rel
