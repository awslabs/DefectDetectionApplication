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
"""Negative-fixture tests for the Docker non-ECR base-image audit gate
(Task 6 / finding D6 / Req 2.6).

Task 6 finalizes ``docker_base_image_audit`` so that ``disallowed_hits()``
returns ``[]`` on the fixed tree. The subtle part of the gate is that it must
classify each ``FROM`` PER-``FROM`` (keyed on the reference string after
``FROM`` and before any ``AS <stage>``) -- NOT file-global. A file-global
"does ``nvcr.io`` appear anywhere?" check would either (a) flag the compliant
``${BASE_REGISTRY}/...@sha256:...`` lines because the file's
``ARG BASE_REGISTRY=nvcr.io`` default still contains ``nvcr.io``, or (b) clear a
reverted literal-``nvcr.io`` ``FROM`` just because a compliant ``FROM`` /
``ARG`` sits elsewhere in the same file. Either degradation re-opens the
non-ECR / unpinned base-image hole.

These tests prove the per-``FROM`` classification holds by driving the module's
own parsing helpers (``is_disallowed_from``, ``from_reference``, ``from_stage``)
against SYNTHETIC in-memory ``FROM``-line fixtures. They deliberately DO NOT
mutate the real Dockerfiles (those are exercised by the live ``disallowed_hits()``
gate and the exploration test), so a reintroduced literal / unpinned ``FROM`` is
caught structurally regardless of the current on-disk state.

Because the audit module exposes only a file-based ``disallowed_hits()``, this
test file adds a tiny module-level helper (``_scan_fixture_lines``) that mirrors
how ``disallowed_hits()`` iterates lines -- skip comment-only lines, then apply
``is_disallowed_from`` per line -- so a multi-line synthetic Dockerfile can be
classified in memory WITHOUT weakening the audit module.

They also confirm the two-layer contract on the current fixed tree: the raw
``run_audit()`` still enumerates the five in-scope ``FROM``s (non-empty) while
the precise ``disallowed_hits()`` gate is empty, and both layers exclude the
vendored ``src/backend/edgemlsdk/edgemlsdk/...`` duplicate.

Validates: Requirements 2.6
"""

import docker_base_image_audit as audit


# The three real pinned manifest-list digests (from the design's digest table),
# reused so the compliant fixtures mirror the actual fixed tree exactly.
_JETPACK_R3541_DIGEST = (
    "sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e"
)
_JETPACK_R3630_DIGEST = (
    "sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa"
)
_CUDA_11419_DIGEST = (
    "sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78"
)

# --- Individual FROM references (the portion after FROM, before AS) --------- #

# The reverted / bug-condition literal: non-ECR literal registry, mutable tag,
# no ${BASE_REGISTRY} seam, no @sha256 pin (D1/D2 shape).
_REVERTED_JETPACK = "FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1"
# The reverted cuda114 provider (D3 shape), literal + no digest, AS cuda114.
_REVERTED_CUDA114 = "FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114"
# The reverted final runtime (D4 shape), literal + no digest.
_REVERTED_RUNTIME = "FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0"

# Compliant fixed forms: ${BASE_REGISTRY} seam + @sha256 pin, tag + stage kept.
_FIXED_JETPACK = f"FROM ${{BASE_REGISTRY}}/nvidia/l4t-jetpack:r35.4.1@{_JETPACK_R3541_DIGEST}"
_FIXED_CUDA114 = (
    f"FROM ${{BASE_REGISTRY}}/nvidia/l4t-cuda:11.4.19-runtime@{_CUDA_11419_DIGEST} AS cuda114"
)
_FIXED_RUNTIME = f"FROM ${{BASE_REGISTRY}}/nvidia/l4t-jetpack:r36.3.0@{_JETPACK_R3630_DIGEST}"

# Positive-control non-bug forms.
_ECR_FROM = "FROM public.ecr.aws/ubuntu/ubuntu:22.04"
_NOSEC_LITERAL_FROM = "FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1  # nosec"

_ARG_LINE = "ARG BASE_REGISTRY=nvcr.io"


# --------------------------------------------------------------------------- #
# Tiny in-memory line scanner mirroring how disallowed_hits() iterates lines.
# Lives in the TEST file (NOT the audit module) so the gate logic is exercised
# through the module's own per-line helper without weakening it.
# --------------------------------------------------------------------------- #
def _scan_fixture_lines(fixture_text):
    """Return the list of ``(lineno, ref)`` for every line flagged disallowed,
    mirroring ``disallowed_hits()``: skip comment-only lines, then classify each
    remaining line with the module's per-``FROM`` ``is_disallowed_from``."""
    flagged = []
    for lineno, line in enumerate(fixture_text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if audit.is_disallowed_from(line):
            flagged.append((lineno, audit.from_reference(line)))
    return flagged


# --------------------------------------------------------------------------- #
# Per-FROM (NOT file-global) classification on multi-line synthetic fixtures.
# --------------------------------------------------------------------------- #
def test_reverted_from_flagged_despite_compliant_arg_and_from_in_same_file():
    """The crux of D6: a reverted literal-``nvcr.io`` ``FROM`` must be flagged
    even when a compliant ``ARG BASE_REGISTRY=nvcr.io`` line AND a compliant
    ``${BASE_REGISTRY}/...@sha256:`` ``FROM`` exist elsewhere in the SAME file.
    A file-global check would be fooled; the per-``FROM`` gate flags ONLY the
    reverted line."""
    fixture = "\n".join(
        [
            "ARG OS",
            "ARG PLATFORM",
            _ARG_LINE,  # compliant ARG seam default -- must NOT clear a reverted FROM
            _REVERTED_JETPACK,  # <- reverted, line 4: the ONLY line that should flag
            "RUN echo build",
            _FIXED_CUDA114,  # compliant elsewhere -- must NOT be flagged
        ]
    )
    flagged = _scan_fixture_lines(fixture)
    assert flagged == [(4, "nvcr.io/nvidia/l4t-jetpack:r35.4.1")], flagged


def test_reverted_from_flagged_even_with_no_other_from():
    """A reverted literal ``FROM`` on its own is flagged."""
    assert audit.is_disallowed_from(_REVERTED_JETPACK) is True
    assert audit.is_disallowed_from(_REVERTED_CUDA114) is True
    assert audit.is_disallowed_from(_REVERTED_RUNTIME) is True


# --------------------------------------------------------------------------- #
# Positive controls -- compliant / exempt FROMs are cleared.
# --------------------------------------------------------------------------- #
def test_parameterized_and_pinned_from_is_cleared():
    """A ``${BASE_REGISTRY}/...:tag@sha256:<64hex>`` ``FROM`` is cleared even
    though ``nvcr.io`` appears in the ``ARG BASE_REGISTRY=nvcr.io`` default."""
    for line in (_FIXED_JETPACK, _FIXED_CUDA114, _FIXED_RUNTIME):
        assert audit.is_disallowed_from(line) is False, line
    # And in a full synthetic file (ARG default contains nvcr.io) -> zero flags.
    fixture = "\n".join([_ARG_LINE, _FIXED_JETPACK, _FIXED_CUDA114, _FIXED_RUNTIME])
    assert _scan_fixture_lines(fixture) == []


def test_ecr_from_is_cleared():
    """An ECR ``public.ecr.aws/...`` ``FROM`` is not a non-ECR base image."""
    assert audit.is_disallowed_from(_ECR_FROM) is False
    assert _scan_fixture_lines(_ARG_LINE + "\n" + _ECR_FROM) == []


def test_nosec_annotated_literal_from_is_cleared():
    """A documented ``# nosec`` exception clears an otherwise-disallowed literal
    ``FROM``."""
    assert audit.is_disallowed_from(_NOSEC_LITERAL_FROM) is False
    assert _scan_fixture_lines(_NOSEC_LITERAL_FROM) == []


def test_comment_only_from_line_is_not_flagged():
    """A commented-out ``FROM`` (pure comment line) is never a live FROM."""
    assert _scan_fixture_lines("# " + _REVERTED_JETPACK) == []


# --------------------------------------------------------------------------- #
# Order / independence -- reverting exactly one FROM flags exactly that one.
# --------------------------------------------------------------------------- #
def test_reverting_cuda114_keeps_runtime_compliant_flags_only_cuda114():
    """Reverting the cuda114 provider ``FROM`` while keeping the final runtime
    ``FROM`` compliant flags EXACTLY the cuda114 line (D3 reverted, D4 kept)."""
    fixture = "\n".join(
        [
            _ARG_LINE,
            _REVERTED_CUDA114,  # <- reverted, line 2
            _FIXED_RUNTIME,  # compliant
        ]
    )
    flagged = _scan_fixture_lines(fixture)
    assert flagged == [(2, "nvcr.io/nvidia/l4t-cuda:11.4.19-runtime")], flagged


def test_reverting_runtime_keeps_cuda114_compliant_flags_only_runtime():
    """Vice-versa: reverting the final runtime ``FROM`` while keeping the
    cuda114 provider compliant flags EXACTLY the runtime line (D4 reverted, D3
    kept)."""
    fixture = "\n".join(
        [
            _ARG_LINE,
            _FIXED_CUDA114,  # compliant
            _REVERTED_RUNTIME,  # <- reverted, line 3
        ]
    )
    flagged = _scan_fixture_lines(fixture)
    assert flagged == [(3, "nvcr.io/nvidia/l4t-jetpack:r36.3.0")], flagged


def test_stage_name_preserved_on_reverted_and_fixed_cuda114():
    """The ``AS cuda114`` stage name is parsed identically whether the ``FROM``
    is reverted or fixed -- classification keys on the ref, not the stage."""
    assert audit.from_stage(_REVERTED_CUDA114) == "cuda114"
    assert audit.from_stage(_FIXED_CUDA114) == "cuda114"


# --------------------------------------------------------------------------- #
# Two-layer contract on the current fixed tree (Req 2.6 / Preservation 3.7).
# --------------------------------------------------------------------------- #
def test_run_audit_non_empty_but_disallowed_hits_empty_on_fixed_tree():
    """``run_audit()`` still enumerates the raw in-scope ``FROM``s
    (non-empty) while the precise ``disallowed_hits()`` gate is empty after
    D1-D5 (the two JP7 FROMs added by jetpack7-support were created
    compliant, so they enumerate raw but never flag)."""
    raw = audit.run_audit()
    assert len(raw) == 8, (
        "run_audit() must enumerate the eight in-scope FROMs (D1-D5 + the "
        "Dockerfile.jp6 trt8 TensorRT 8 provider stage + the two JP7 "
        "Dockerfile FROMs); got "
        + "; ".join(f"{audit._rel(h.path)}:{h.lineno}" for h in raw)
    )

    disallowed = audit.disallowed_hits()
    assert disallowed == [], (
        "disallowed_hits() must be empty on the fixed tree; got: "
        + "; ".join(f"{h.category} {h.path}:{h.lineno}" for h in disallowed)
    )


def test_both_layers_exclude_vendored_duplicate():
    """Neither layer may surface a hit from the vendored
    src/backend/edgemlsdk/edgemlsdk/... duplicate or the generated cdk.out."""
    for h in audit.run_audit():
        assert audit.VENDORED_DUP_SUBSTRING not in h.path, h.path
        assert "cdk.out" not in h.path, h.path
    for h in audit.disallowed_hits():
        assert audit.VENDORED_DUP_SUBSTRING not in h.path, h.path
        assert "cdk.out" not in h.path, h.path
