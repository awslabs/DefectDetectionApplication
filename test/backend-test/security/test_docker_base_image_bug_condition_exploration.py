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
"""Bug-condition exploration test (FLIPPED in Task 7) for
security-docker-non-ecr-base-image-fixes.

Property 1: Expected Behavior -- every in-scope base-image ``FROM`` is now
registry-parameterized via the ``${BASE_REGISTRY}`` ARG seam AND digest-pinned
with ``@sha256:`` while retaining its human-readable tag (``r35.4.1`` /
``r36.3.0`` / ``11.4.19-runtime``) and any ``AS <stage>`` name (``builder`` /
``cuda114``), across the five in-scope sites (D1-D5).

This file was WRITTEN IN TASK 1 to OBSERVE the counterexample shape (literal
``nvcr.io``, no ARG seam, no digest) on the UNFIXED tree. Per the bug-condition
methodology, TASK 7 FLIPS each D1-D5 assertion to the SECURE / neutralized
post-fix invariant: the counterexample is gone and the ``FROM`` is now
``${BASE_REGISTRY}``-parameterized + ``@sha256``-pinned + tag/stage preserved,
with ``is_disallowed_from`` returning False. The audit-gate test
``test_docker_audit_returns_no_disallowed_hits`` (unchanged) now PASSES because
``disallowed_hits() == []`` on the fixed tree.

Fixed-tree secure invariants this file now asserts:
  * D1 -- src/backend/Dockerfile.jp5  first FROM ==
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971...b2a0e``
    (exactly one ``ARG BASE_REGISTRY=nvcr.io`` before the first FROM),
  * D2 -- src/edgemlsdk/Dockerfile.jp5 first FROM ==
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971...b2a0e AS builder``
    (``ARG BASE_REGISTRY=nvcr.io`` is the new first line),
  * D3 -- src/backend/Dockerfile.jp6 first FROM ==
    ``${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff08...8da78 AS cuda114``,
  * D4 -- src/backend/Dockerfile.jp6 second FROM ==
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3...e2fa``
    (exactly ONE shared ``ARG BASE_REGISTRY`` in the file, no re-declaration),
  * D5 -- src/edgemlsdk/Dockerfile.jp6 first FROM ==
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3...e2fa AS builder``
    (``ARG BASE_REGISTRY=nvcr.io`` is the new first line),
  * the repo audit finds ZERO disallowed non-ECR base-image FROMs.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""
import os

import pytest

import docker_base_image_audit as audit

REPO_ROOT = audit.REPO_ROOT

# The three immutable manifest-list digests the fix pins to (design table).
DIGEST_L4T_R3541 = "sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e"
DIGEST_L4T_R3630 = "sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa"
DIGEST_CUDA_11419 = "sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78"


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _lines(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read().splitlines()


def _from_lines(rel_path):
    """Return ``[(lineno, line), ...]`` for each non-comment ``FROM`` line
    (1-indexed). Robust to the extra ``ARG BASE_REGISTRY`` line shifting the
    original hard-coded line numbers."""
    result = []
    for idx, ln in enumerate(_lines(rel_path), start=1):
        if ln.lstrip().startswith("#"):
            continue
        if audit.from_reference(ln) is not None:
            result.append((idx, ln))
    return result


def _first_from(rel_path):
    return _from_lines(rel_path)[0]


def _arg_base_registry_lines(rel_path):
    """Return ``[(lineno, line), ...]`` for each ``ARG BASE_REGISTRY`` line."""
    return [
        (idx, ln)
        for idx, ln in enumerate(_lines(rel_path), start=1)
        if ln.strip().startswith("ARG BASE_REGISTRY")
    ]


def _first_from_lineno(rel_path):
    return _from_lines(rel_path)[0][0]


def _assert_secure_from(line, *, image_tag, digest, stage=None):
    """Assert a FROM line is the fixed secure invariant: ``${BASE_REGISTRY}``
    seam present, exact ``@sha256:`` digest present, the human-readable tag
    retained, any ``AS <stage>`` preserved, and the per-FROM gate clears it."""
    ref = audit.from_reference(line)
    assert ref is not None, f"expected a FROM line, got {line!r}"

    # 1. registry parameterization via the ${BASE_REGISTRY} ARG seam.
    assert "${BASE_REGISTRY}" in ref, (
        f"expected the ${{BASE_REGISTRY}} seam in {ref!r}"
    )
    assert audit._PARAMETERIZED_REGISTRY_RE.search(ref), (
        f"the FROM must be ${{BASE_REGISTRY}}-parameterized: {ref!r}"
    )
    # No residual literal nvcr.io registry host prefix (the seam replaced it).
    assert not ref.startswith("nvcr.io/"), (
        f"the literal nvcr.io registry prefix must be gone: {ref!r}"
    )

    # 2. immutable @sha256: digest pin, with the EXACT expected digest.
    assert audit._DIGEST_RE.search(ref), f"missing @sha256: digest pin: {ref!r}"
    assert f"@{digest}" in ref, (
        f"expected the pinned digest @{digest} in {ref!r}"
    )

    # 3. the human-readable tag is retained (image:tag preserved before digest).
    assert image_tag in ref, f"expected the tag {image_tag!r} retained in {ref!r}"

    # 4. any AS <stage> name preserved.
    assert audit.from_stage(line) == stage, (
        f"expected AS stage {stage!r}, got {audit.from_stage(line)!r} in {line!r}"
    )

    # 5. the precise per-FROM gate now CLEARS this FROM (neutralized).
    assert not audit.is_disallowed_from(line), (
        f"the fixed FROM must NOT be flagged as disallowed: {line!r}"
    )


# --------------------------------------------------------------------------- #
# Repo audit (D6 / Req 2.6) -- the gate re-run in task 7.
# --------------------------------------------------------------------------- #
def test_docker_audit_returns_no_disallowed_hits():
    """The Docker non-ECR audit must return ZERO disallowed bug-condition hits
    across the four in-scope Dockerfiles.

    FIXED-TREE EXPECTATION: this PASSES -- all five in-scope ``FROM``s (D1-D5)
    are now ``${BASE_REGISTRY}``-parameterized + ``@sha256``-pinned, so the
    per-FROM gate clears every one and ``disallowed_hits() == []``. Validates
    Req 2.6 (audit gate)."""
    all_hits = audit.run_audit()

    # No out-of-scope tree (vendored duplicate / cdk.out) may leak in.
    leaked = [
        h for h in all_hits
        if audit.VENDORED_DUP_SUBSTRING in h.path
        or audit.EXCLUDED_PATH_SUBSTRING in h.path
    ]
    assert not leaked, f"vendored/cdk.out copies must be excluded, got: {leaked}"

    disallowed = audit.disallowed_hits()
    assert disallowed == [], (
        f"Docker non-ECR audit found {len(disallowed)} disallowed base-image "
        f"FROM(s) (the fix should have neutralized all of them):\n"
        + "\n".join(
            f"  [{h.category}] {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: "
            f"{h.text.strip()}"
            for h in disallowed
        )
    )


def test_run_audit_non_empty():
    """The RAW ``run_audit()`` enumeration is NON-EMPTY and enumerates the five
    in-scope ``FROM``s (D1-D5) while excluding the vendored duplicate. This is
    the enumeration anchor and stays green before and after the fix."""
    all_hits = audit.run_audit()
    assert all_hits, "expected a non-empty raw enumeration of in-scope FROM lines"

    # Exactly five in-scope FROMs (D1, D2, D3, D4, D5) -- unchanged by the fix.
    assert len(all_hits) == 5, (
        f"expected 5 in-scope FROM lines (D1-D5), got {len(all_hits)}:\n"
        + "\n".join(
            f"  {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: {h.text.strip()}"
            for h in all_hits
        )
    )

    # Each of the four in-scope files is represented; jp6 backend carries two.
    assert len(audit.hits_for(audit.BACKEND_JP5_REL, all_hits)) == 1
    assert len(audit.hits_for(audit.EDGEMLSDK_JP5_REL, all_hits)) == 1
    assert len(audit.hits_for(audit.BACKEND_JP6_REL, all_hits)) == 2
    assert len(audit.hits_for(audit.EDGEMLSDK_JP6_REL, all_hits)) == 1

    # The vendored edgemlsdk/edgemlsdk/... duplicate is never enumerated.
    vendored = [h for h in all_hits if audit.VENDORED_DUP_SUBSTRING in h.path]
    assert vendored == [], f"vendored duplicate must be excluded, got {vendored}"


# --------------------------------------------------------------------------- #
# D1 -- src/backend/Dockerfile.jp5 base FROM (Req 2.1)
# --------------------------------------------------------------------------- #
def test_d1_backend_jp5_from_is_parameterized_and_pinned():
    """D1 (Req 2.1): src/backend/Dockerfile.jp5's base FROM is now
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971...b2a0e`` --
    registry-parameterized + digest-pinned with the ``r35.4.1`` tag retained --
    and exactly ONE ``ARG BASE_REGISTRY=nvcr.io`` exists before the first FROM.
    FIXED-TREE SECURE INVARIANT (flipped from the task-1 counterexample)."""
    lineno, line = _first_from(audit.BACKEND_JP5_REL)
    print(f"\n[D1 fixed] Dockerfile.jp5:{lineno} == {line!r}")
    _assert_secure_from(
        line, image_tag="l4t-jetpack:r35.4.1", digest=DIGEST_L4T_R3541, stage=None
    )

    # Exactly one ARG BASE_REGISTRY=nvcr.io, declared before the first FROM.
    arg_lines = _arg_base_registry_lines(audit.BACKEND_JP5_REL)
    assert len(arg_lines) == 1, (
        f"expected exactly one ARG BASE_REGISTRY, got {arg_lines}"
    )
    arg_lineno, arg_line = arg_lines[0]
    assert arg_line.strip() == "ARG BASE_REGISTRY=nvcr.io", (
        f"expected 'ARG BASE_REGISTRY=nvcr.io', got {arg_line!r}"
    )
    assert arg_lineno < lineno, (
        "the ARG BASE_REGISTRY must be declared before the first FROM "
        f"(ARG at {arg_lineno}, FROM at {lineno})."
    )


# --------------------------------------------------------------------------- #
# D2 -- src/edgemlsdk/Dockerfile.jp5 builder FROM (Req 2.2)
# --------------------------------------------------------------------------- #
def test_d2_edgemlsdk_jp5_builder_from_is_parameterized_and_pinned():
    """D2 (Req 2.2): src/edgemlsdk/Dockerfile.jp5's builder FROM is now
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971...b2a0e AS builder``
    -- parameterized + pinned with the ``r35.4.1`` tag and ``AS builder`` stage
    preserved -- and ``ARG BASE_REGISTRY=nvcr.io`` is the new first line.
    FIXED-TREE SECURE INVARIANT (flipped from the task-1 counterexample)."""
    lineno, line = _first_from(audit.EDGEMLSDK_JP5_REL)
    print(f"\n[D2 fixed] edgemlsdk/Dockerfile.jp5:{lineno} == {line!r}")
    _assert_secure_from(
        line, image_tag="l4t-jetpack:r35.4.1", digest=DIGEST_L4T_R3541,
        stage="builder",
    )

    # ARG BASE_REGISTRY=nvcr.io is the new FIRST line of the file.
    first_src_line = _lines(audit.EDGEMLSDK_JP5_REL)[0]
    assert first_src_line.strip() == "ARG BASE_REGISTRY=nvcr.io", (
        f"expected the new first line 'ARG BASE_REGISTRY=nvcr.io', got "
        f"{first_src_line!r}"
    )


# --------------------------------------------------------------------------- #
# D3 -- src/backend/Dockerfile.jp6 cuda114 provider FROM (Req 2.3)
# --------------------------------------------------------------------------- #
def test_d3_backend_jp6_cuda114_from_is_parameterized_and_pinned():
    """D3 (Req 2.3): src/backend/Dockerfile.jp6's cuda114 provider FROM is now
    ``${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff08...8da78 AS cuda114``
    -- parameterized + pinned with the ``11.4.19-runtime`` tag and ``AS cuda114``
    stage preserved (the later COPY --from=cuda114 still resolves). FIXED-TREE
    SECURE INVARIANT (flipped from the task-1 counterexample)."""
    from_lines = _from_lines(audit.BACKEND_JP6_REL)
    lineno, line = from_lines[0]  # the FIRST FROM is the cuda114 provider stage
    print(f"\n[D3 fixed] Dockerfile.jp6:{lineno} == {line!r}")
    _assert_secure_from(
        line, image_tag="l4t-cuda:11.4.19-runtime", digest=DIGEST_CUDA_11419,
        stage="cuda114",
    )


# --------------------------------------------------------------------------- #
# D4 -- src/backend/Dockerfile.jp6 final runtime FROM (Req 2.4)
# --------------------------------------------------------------------------- #
def test_d4_backend_jp6_final_from_is_parameterized_and_pinned():
    """D4 (Req 2.4): src/backend/Dockerfile.jp6's final runtime FROM is now
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3...e2fa`` --
    parameterized + pinned with the ``r36.3.0`` tag retained -- and there is
    exactly ONE ``ARG BASE_REGISTRY`` in the whole file (the single shared ARG
    declared before the first FROM, no re-declaration). FIXED-TREE SECURE
    INVARIANT (flipped from the task-1 counterexample)."""
    from_lines = _from_lines(audit.BACKEND_JP6_REL)
    assert len(from_lines) == 2, (
        f"expected two FROMs in Dockerfile.jp6, got {len(from_lines)}"
    )
    lineno, line = from_lines[1]  # the SECOND FROM is the final runtime stage
    print(f"\n[D4 fixed] Dockerfile.jp6:{lineno} == {line!r}")
    _assert_secure_from(
        line, image_tag="l4t-jetpack:r36.3.0", digest=DIGEST_L4T_R3630, stage=None
    )

    # Exactly ONE ARG BASE_REGISTRY in the file (shared across both FROMs, no
    # post-FROM re-declaration), declared before the first FROM.
    arg_lines = _arg_base_registry_lines(audit.BACKEND_JP6_REL)
    assert len(arg_lines) == 1, (
        f"expected exactly ONE shared ARG BASE_REGISTRY (no re-declaration), "
        f"got {arg_lines}"
    )
    arg_lineno, arg_line = arg_lines[0]
    assert arg_line.strip() == "ARG BASE_REGISTRY=nvcr.io", (
        f"expected 'ARG BASE_REGISTRY=nvcr.io', got {arg_line!r}"
    )
    assert arg_lineno < from_lines[0][0], (
        "the single shared ARG BASE_REGISTRY must be declared before the first "
        f"FROM (ARG at {arg_lineno}, first FROM at {from_lines[0][0]})."
    )


# --------------------------------------------------------------------------- #
# D5 -- src/edgemlsdk/Dockerfile.jp6 builder FROM (Req 2.5)
# --------------------------------------------------------------------------- #
def test_d5_edgemlsdk_jp6_builder_from_is_parameterized_and_pinned():
    """D5 (Req 2.5): src/edgemlsdk/Dockerfile.jp6's builder FROM is now
    ``${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3...e2fa AS builder``
    -- parameterized + pinned with the ``r36.3.0`` tag and ``AS builder`` stage
    preserved -- and ``ARG BASE_REGISTRY=nvcr.io`` is the new first line.
    FIXED-TREE SECURE INVARIANT (flipped from the task-1 counterexample)."""
    lineno, line = _first_from(audit.EDGEMLSDK_JP6_REL)
    print(f"\n[D5 fixed] edgemlsdk/Dockerfile.jp6:{lineno} == {line!r}")
    _assert_secure_from(
        line, image_tag="l4t-jetpack:r36.3.0", digest=DIGEST_L4T_R3630,
        stage="builder",
    )

    # ARG BASE_REGISTRY=nvcr.io is the new FIRST line of the file.
    first_src_line = _lines(audit.EDGEMLSDK_JP6_REL)[0]
    assert first_src_line.strip() == "ARG BASE_REGISTRY=nvcr.io", (
        f"expected the new first line 'ARG BASE_REGISTRY=nvcr.io', got "
        f"{first_src_line!r}"
    )


# --------------------------------------------------------------------------- #
# Per-FROM (not file-global) classification characterization
# --------------------------------------------------------------------------- #
def test_gate_keys_on_from_reference_not_file_global():
    """Characterization (design 'Critical parsing subtlety'): the gate keys on
    the FROM *reference string*, NOT the whole file. A compliant
    ``FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:<64hex>`` line is
    CLEARED even though an ``ARG BASE_REGISTRY=nvcr.io`` default (which contains
    ``nvcr.io``) sits elsewhere; a literal ``nvcr.io`` FROM without a digest is
    flagged; an ECR host is never flagged."""
    digest = "@sha256:" + "d" * 64
    compliant = f"FROM ${{BASE_REGISTRY}}/nvidia/l4t-jetpack:r35.4.1{digest} AS builder"
    literal_unpinned = "FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1"
    literal_pinned_only = f"FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1{digest}"
    param_only = "FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1"
    ecr = "FROM public.ecr.aws/ubuntu/ubuntu:22.04"
    nosec = literal_unpinned + "  # nosec"

    assert not audit.is_disallowed_from(compliant), (
        "a ${BASE_REGISTRY}+@sha256 FROM must be cleared regardless of an "
        "ARG BASE_REGISTRY=nvcr.io default elsewhere in the file."
    )
    assert audit.is_disallowed_from(literal_unpinned), (
        "a literal nvcr.io FROM with no digest must be flagged."
    )
    assert audit.is_disallowed_from(literal_pinned_only), (
        "a literal nvcr.io FROM (pinned but NOT parameterized) must be flagged."
    )
    # Per the design's precise gate semantics, the gate keys on a non-ECR
    # *literal* registry; a ${BASE_REGISTRY}-parameterized registry is not a
    # literal, so a parameterized-but-unpinned FROM is NOT flagged by this gate
    # (this edge case does not occur in the in-scope tree).
    assert not audit.is_disallowed_from(param_only), (
        "a ${BASE_REGISTRY}-parameterized FROM has no literal non-ECR registry, "
        "so it is not flagged by the literal-registry gate."
    )
    assert not audit.is_disallowed_from(ecr), "an ECR host FROM must be cleared."
    assert not audit.is_disallowed_from(nosec), (
        "a FROM carrying a # nosec marker must be cleared."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
