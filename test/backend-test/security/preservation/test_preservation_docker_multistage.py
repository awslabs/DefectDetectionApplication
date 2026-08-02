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
"""Multi-stage structure golden (D2, D3, D5) — Docker non-ECR base image spec
(Req 3.3).

Spec: security-docker-non-ecr-base-image-fixes — Property 2: Preservation.

Records the multi-stage structure that the fix MUST preserve so cross-stage
``COPY --from`` references keep resolving:

* ``src/edgemlsdk/Dockerfile.jp5`` and ``src/edgemlsdk/Dockerfile.jp6`` each carry
  an ``AS builder`` stage name on their first ``FROM``.
* ``src/backend/Dockerfile.jp6`` carries ``AS cuda114`` on the l4t-cuda ``FROM``
  (line 18), and later the ``COPY --from=cuda114 /usr/local/cuda-11.4/targets/
  aarch64-linux/lib/`` cudart staging line plus the ``ldconfig`` /
  ``libcudart.so.11`` verification block — captured byte-for-byte.

Golden: ``baselines/docker_baseline_multistage.json``. Task 8 asserts the fixed
tree still carries the same stage names and the same ``COPY --from=cuda114`` +
verification block (only the ``FROM`` refs change; the ``AS <stage>`` names and
the copy/verify block are untouched).

**Validates: Requirements 3.3**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_docker_multistage.py \
        -p no:cacheprovider --noconftest -v
"""
from _docker_preservation_support import (
    BACKEND_JP6_REL,
    EDGEMLSDK_JP5_REL,
    EDGEMLSDK_JP6_REL,
    capture_or_assert_json,
    in_scope_from_lines,
    read_repo_file,
)


def _stage_names(rel_path):
    """Ordered list of ``AS <stage>`` names on the in-scope ``FROM`` lines."""
    return [p["stage"] for (_ln, _line, p) in in_scope_from_lines(rel_path)]


def _extract_cuda114_block(rel_path):
    """Return the ``COPY --from=cuda114 ...`` line (with its continuation) and the
    following ``RUN ... ldconfig ... libcudart.so.11 ...`` verification block from
    jp6 backend, as a list of byte-for-byte source lines."""
    lines = read_repo_file(rel_path).splitlines()
    block = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("COPY --from=cuda114 "):
            # Capture the COPY (which spans a line-continuation) and the very next
            # RUN ... block (also continued) that registers + verifies the libs.
            # Collect until we hit a line that does NOT end with a backslash after
            # having started, spanning the COPY then the RUN verification.
            j = i
            # COPY (may continue with trailing backslash)
            block.append(lines[j])
            while lines[j].rstrip().endswith("\\"):
                j += 1
                block.append(lines[j])
            j += 1
            # The RUN verification block immediately follows.
            if j < len(lines) and lines[j].startswith("RUN "):
                block.append(lines[j])
                while lines[j].rstrip().endswith("\\"):
                    j += 1
                    block.append(lines[j])
            break
        i += 1
    return block


# Validates: Requirements 3.3
def test_multistage_structure_golden():
    """Capture / assert the multi-stage golden (stage names + cuda114 copy+verify
    block)."""
    cuda114_block = _extract_cuda114_block(BACKEND_JP6_REL)
    golden = {
        "edgemlsdk_jp5_stages": _stage_names(EDGEMLSDK_JP5_REL),
        "edgemlsdk_jp6_stages": _stage_names(EDGEMLSDK_JP6_REL),
        "backend_jp6_stages": _stage_names(BACKEND_JP6_REL),
        "backend_jp6_cuda114_block": cuda114_block,
    }

    # Structural sanity on the captured baseline (fails loudly if the files drift
    # in a way that would make the golden meaningless).
    assert golden["edgemlsdk_jp5_stages"] == ["builder"], golden["edgemlsdk_jp5_stages"]
    assert golden["edgemlsdk_jp6_stages"] == ["builder"], golden["edgemlsdk_jp6_stages"]
    # jp6 backend has three FROMs: the cuda114 provider, the trt8 TensorRT 8
    # provider (Neo/DLR runtime), then the (unnamed) final runtime.
    assert golden["backend_jp6_stages"] == ["cuda114", "trt8", None], golden["backend_jp6_stages"]
    assert cuda114_block, "expected a COPY --from=cuda114 + verification block"
    assert cuda114_block[0].startswith(
        "COPY --from=cuda114 /usr/local/cuda-11.4/targets/aarch64-linux/lib/"
    )
    joined = "\n".join(cuda114_block)
    assert "ldconfig" in joined
    assert "libcudart.so.11" in joined

    capture_or_assert_json("docker_baseline_multistage.json", golden)
