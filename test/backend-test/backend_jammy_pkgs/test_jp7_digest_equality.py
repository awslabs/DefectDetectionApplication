# Copyright 2026 Amazon Web Services, Inc.
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
"""JP7 base-image digest-equality check (jetpack7-support, Requirement 2.6).

The two JP7 Dockerfiles -- ``src/backend/Dockerfile.jp7`` and
``src/edgemlsdk/Dockerfile.jp7`` -- MUST reference the identical JP7 base-image
digest in their FROM references. This is intentionally stricter than JP6 (where
the backend and edgemlsdk bases differ): the JP7 backend image and the Triton
Python-backend stub built by the edgemlsdk image must share one CUDA 13 /
Ubuntu 24.04 userspace, so a digest drift between the two files is a build
misconfiguration the baseline suite fails loudly on.

Deliberately structured as INDEPENDENT per-file parsing plus one cross-file
equality assertion (Req 9.5): text-only (no docker, no subprocess), no shared
state with the jp5/jp6 baseline goldens in this suite, import-light so it runs
under ``pytest test/backend-test/backend_jammy_pkgs/ --noconftest``.

**Validates: Requirements 2.6**

Run (finite, non-watch):
    python3 -m pytest test/backend-test/backend_jammy_pkgs/test_jp7_digest_equality.py \
        -p no:cacheprovider --noconftest -v
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))

BACKEND_JP7_REL = os.path.join("src", "backend", "Dockerfile.jp7")
EDGEMLSDK_JP7_REL = os.path.join("src", "edgemlsdk", "Dockerfile.jp7")

# ``FROM <ref> [AS <stage>]`` (ignoring any --flags). Comment lines never match
# (handled by the comment skip in _from_refs).
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<ref>\S+)(?:\s+[Aa][Ss]\s+\S+)?\s*$"
)
# The immutable digest pin inside a FROM reference.
_DIGEST_RE = re.compile(r"@(?P<digest>sha256:[0-9a-f]{64})\b")


def _from_refs(rel_path):
    """Return the FROM reference strings (portion after FROM, before any
    ``AS <stage>``) of every non-comment FROM line in ``rel_path``."""
    path = os.path.join(REPO_ROOT, rel_path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    refs = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _FROM_RE.match(line)
        if m:
            refs.append(m.group("ref"))
    return refs


def _from_digests(rel_path):
    """Return the sha256 digest of every FROM reference in ``rel_path``,
    failing loudly if any FROM carries no digest pin."""
    refs = _from_refs(rel_path)
    assert refs, f"{rel_path}: expected at least one FROM instruction"
    digests = []
    for ref in refs:
        m = _DIGEST_RE.search(ref)
        assert m, (
            f"{rel_path}: FROM reference carries no @sha256: digest pin: "
            f"{ref!r}"
        )
        digests.append(m.group("digest"))
    return digests


# Validates: Requirements 2.6 -- independent per-file check (backend jp7).
def test_backend_jp7_from_is_digest_pinned():
    """src/backend/Dockerfile.jp7 has exactly one FROM and it is digest-pinned."""
    digests = _from_digests(BACKEND_JP7_REL)
    assert len(digests) == 1, (
        f"{BACKEND_JP7_REL}: expected exactly one FROM (single-stage JP7 "
        f"backend image), got {len(digests)}"
    )


# Validates: Requirements 2.6 -- independent per-file check (edgemlsdk jp7).
def test_edgemlsdk_jp7_from_is_digest_pinned():
    """src/edgemlsdk/Dockerfile.jp7 has exactly one FROM and it is digest-pinned."""
    digests = _from_digests(EDGEMLSDK_JP7_REL)
    assert len(digests) == 1, (
        f"{EDGEMLSDK_JP7_REL}: expected exactly one FROM (single builder "
        f"stage), got {len(digests)}"
    )


# Validates: Requirements 2.6 -- the digest-equality gate.
def test_jp7_dockerfiles_pin_the_identical_base_digest():
    """Both JP7 Dockerfiles reference the IDENTICAL base-image digest in their
    FROM references; the baseline suite fails if the two digests differ."""
    backend_digests = set(_from_digests(BACKEND_JP7_REL))
    edgemlsdk_digests = set(_from_digests(EDGEMLSDK_JP7_REL))
    assert backend_digests == edgemlsdk_digests, (
        f"JP7 base-image digest mismatch (Requirement 2.6): the two JP7 "
        f"Dockerfiles must pin the IDENTICAL base-image digest.\n"
        f"  {BACKEND_JP7_REL}: {sorted(backend_digests)}\n"
        f"  {EDGEMLSDK_JP7_REL}: {sorted(edgemlsdk_digests)}"
    )
