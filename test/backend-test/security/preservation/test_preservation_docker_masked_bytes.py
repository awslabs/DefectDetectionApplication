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
"""Non-``FROM`` bytes golden (D1–D5) — Docker non-ECR base image spec
(Req 3.1, 3.2, 3.4).

Spec: security-docker-non-ecr-base-image-fixes — Property 2: Preservation.

This is the **primary preservation guarantee**. For each of the four in-scope
Dockerfiles we capture every line with the ``FROM`` lines and any
``ARG BASE_REGISTRY`` line masked/removed (the only lines this spec is allowed to
touch). On the UNFIXED tree the golden is captured (there is no
``ARG BASE_REGISTRY`` yet and the ``FROM``s are literal ``nvcr.io`` refs). Task 8
re-runs this against the FIXED tree and asserts the masked (non-``FROM`` /
non-``ARG BASE_REGISTRY``) lines are byte-for-byte identical — proving the fix
changed ONLY the ``FROM`` line(s) plus exactly one added ``ARG BASE_REGISTRY``
line per file.

Goldens: ``baselines/docker_baseline_<file>_masked.txt`` (one per in-scope file).

**Validates: Requirements 3.1, 3.2, 3.4**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_docker_masked_bytes.py \
        -p no:cacheprovider --noconftest -v
"""
import pytest

from _docker_preservation_support import (
    BACKEND_JP5_REL,
    BACKEND_JP6_REL,
    BACKEND_JP7_REL,
    EDGEMLSDK_JP5_REL,
    EDGEMLSDK_JP6_REL,
    EDGEMLSDK_JP7_REL,
    IN_SCOPE_FILES,
    capture_or_assert_text,
    mask_dockerfile,
    read_repo_file,
)

# baseline-name key per in-scope file (dir-prefixed to disambiguate the
# per-JetPack Dockerfile basenames across backend/ and edgemlsdk/). The JP7
# pair was registered by the jetpack7-support spec (Req 9.3); each file is an
# independent parametrized check, so the JP7 checks pass regardless of any
# unrelated jp5/jp6 baseline state (Req 9.5) and fail identifying the file
# when its content drifts from the captured baseline (Req 9.6).
_GOLDEN_NAME = {
    BACKEND_JP5_REL: "docker_baseline_backend_Dockerfile.jp5_masked.txt",
    EDGEMLSDK_JP5_REL: "docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt",
    BACKEND_JP6_REL: "docker_baseline_backend_Dockerfile.jp6_masked.txt",
    EDGEMLSDK_JP6_REL: "docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt",
    BACKEND_JP7_REL: "docker_baseline_backend_Dockerfile.jp7_masked.txt",
    EDGEMLSDK_JP7_REL: "docker_baseline_edgemlsdk_Dockerfile.jp7_masked.txt",
}


# Validates: Requirements 3.1, 3.2, 3.4
@pytest.mark.parametrize("rel_path", IN_SCOPE_FILES)
def test_non_from_bytes_masked_golden(rel_path):
    """The non-``FROM`` / non-``ARG BASE_REGISTRY`` lines of each in-scope
    Dockerfile match the captured golden byte-for-byte."""
    masked = mask_dockerfile(rel_path)
    # Join with '\n' and a trailing newline so the golden is a stable text blob.
    current_text = "\n".join(masked) + "\n"
    capture_or_assert_text(_GOLDEN_NAME[rel_path], current_text)


# Validates: Requirements 3.1 — sanity: masking actually removed the FROM line(s)
# (and, on the FIXED tree, the one added ARG BASE_REGISTRY line) so the golden is
# a genuine non-FROM / non-ARG-BASE_REGISTRY view, not the whole file.
@pytest.mark.parametrize("rel_path", IN_SCOPE_FILES)
def test_masking_removed_the_from_lines(rel_path):
    full = read_repo_file(rel_path).splitlines()
    masked = mask_dockerfile(rel_path)
    from_count = sum(1 for ln in full if ln.lstrip().startswith("FROM "))
    # The fix adds exactly one shared ``ARG BASE_REGISTRY=nvcr.io`` line per file
    # (D3+D4 share one in jp6 backend). On the unfixed tree this count is 0; on the
    # fixed tree it is exactly 1. Either way the masking helper strips it, which is
    # the intended preservation semantics (the added ARG line is a line this spec
    # is allowed to touch, so it must NOT appear in the non-FROM golden view).
    arg_base_registry_count = sum(
        1 for ln in full if ln.lstrip().startswith("ARG BASE_REGISTRY")
    )
    assert from_count >= 1, f"{rel_path}: expected at least one FROM line"
    assert arg_base_registry_count <= 1, (
        f"{rel_path}: the fix adds exactly one shared ARG BASE_REGISTRY line, "
        f"found {arg_base_registry_count}"
    )
    # Every retained line is a non-FROM line.
    assert all(not ln.lstrip().startswith("FROM ") for ln in masked)
    # No retained line is an ARG BASE_REGISTRY line (masking stripped it).
    assert all(not ln.lstrip().startswith("ARG BASE_REGISTRY") for ln in masked)
    # And masking dropped exactly the FROM line(s) plus any added ARG BASE_REGISTRY
    # line — an EXPECTED consequence of the fix, not a masked regression.
    assert len(masked) == len(full) - from_count - arg_base_registry_count, (
        f"{rel_path}: masking should drop the {from_count} FROM line(s) plus the "
        f"{arg_base_registry_count} added ARG BASE_REGISTRY line(s)"
    )
