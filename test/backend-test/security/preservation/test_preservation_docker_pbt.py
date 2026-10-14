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
"""Property-based preservation baselines — Docker non-ECR base image spec
(Req 3.1, 3.5).

Spec: security-docker-non-ecr-base-image-fixes — Property 2: Preservation.

Two property-based baselines (Hypothesis — the repo already vendors
``.hypothesis/``):

* **PBT 1 — ``BASE_REGISTRY`` resolution (D1–D5)**: model the fixed ``FROM``
  reference resolution as ``resolve(base_registry, image, tag, digest)``.
  Generate ``base_registry ∈ {unset, "nvcr.io", an ECR host, "my-mirror.internal",
  random host-like strings}`` with the tag + digest FIXED per image. Invariants:
  (a) unset/``nvcr.io`` resolves to ``nvcr.io/nvidia/<image>:<tag>@sha256:<digest>``
  (the byte-identical default pull); (b) any override changes ONLY the registry
  prefix — the ``/nvidia/<image>:<tag>@sha256:<digest>`` suffix is invariant.

* **PBT 2 — arbitrary ``FROM`` → ``disallowed_hits()`` classification (D6)**:
  generate arbitrary ``FROM`` lines from a grammar over {ECR host | ``nvcr.io``
  literal | ``${BASE_REGISTRY}`` seam} × {digest present | absent} × {``AS <stage>``
  present | absent} × {``# nosec`` present | absent}. Invariant: the REAL
  ``docker_base_image_audit.is_disallowed_from(line)`` is True iff the ref is a
  non-ECR literal registry AND (not ``${BASE_REGISTRY}``-parameterized OR not
  ``@sha256``-pinned), AND not ``# nosec``-annotated.

**Validates: Requirements 3.1, 3.5**

Run:
    python3 -m pytest \
        test/backend-test/security/preservation/test_preservation_docker_pbt.py \
        -p no:cacheprovider --noconftest -v
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from _docker_preservation_support import (
    DEFAULT_REGISTRY,
    DIGESTS,
    resolve,
    _ensure_audit_on_path,
)

_ensure_audit_on_path()
import docker_base_image_audit as audit  # noqa: E402


# --------------------------------------------------------------------------- #
# PBT 1 — BASE_REGISTRY resolution
# --------------------------------------------------------------------------- #
# The three in-scope (image, tag, bare-digest) tuples the fix pins.
_IMAGE_TAG_DIGEST = [
    (img, tag, dig.split(":", 1)[1]) for (img, tag), dig in DIGESTS.items()
]

# base_registry domain: unset (None), the default nvcr.io, an ECR host, an
# internal mirror, and random host-like strings.
_BASE_REGISTRY = st.one_of(
    st.none(),
    st.just("nvcr.io"),
    st.just("123456789012.dkr.ecr.us-west-2.amazonaws.com"),
    st.just("my-mirror.internal"),
    st.from_regex(r"\A[a-z0-9][a-z0-9.-]{1,40}(?::[0-9]{2,5})?\Z"),
)


# Validates: Requirements 3.1 — the default (unset / nvcr.io) resolution equals
# the byte-identical current pull reference plus the pinned digest.
@settings(max_examples=25, deadline=None)
@given(
    image_tag_digest=st.sampled_from(_IMAGE_TAG_DIGEST),
    base_registry=_BASE_REGISTRY,
)
def test_pbt1_base_registry_resolution(image_tag_digest, base_registry):
    image, tag, digest = image_tag_digest
    resolved = resolve(base_registry, image, tag, digest)

    expected_registry = base_registry or DEFAULT_REGISTRY
    suffix = f"/nvidia/{image}:{tag}@sha256:{digest}"

    # (b) override changes ONLY the registry prefix; the tag + digest suffix is
    # invariant.
    assert resolved == expected_registry + suffix
    assert resolved.endswith(suffix)

    # (a) unset / nvcr.io resolves to the byte-identical default pull reference.
    if base_registry in (None, "nvcr.io"):
        assert resolved == f"nvcr.io/nvidia/{image}:{tag}@sha256:{digest}"


# Validates: Requirements 3.1 — unset and explicit "nvcr.io" resolve identically
# (the ARG default is nvcr.io), so the default build is unchanged.
@settings(max_examples=30, deadline=None)
@given(image_tag_digest=st.sampled_from(_IMAGE_TAG_DIGEST))
def test_pbt1_unset_equals_explicit_nvcr(image_tag_digest):
    image, tag, digest = image_tag_digest
    assert resolve(None, image, tag, digest) == resolve("nvcr.io", image, tag, digest)


# --------------------------------------------------------------------------- #
# PBT 2 — arbitrary FROM → is_disallowed_from classification (the REAL audit)
# --------------------------------------------------------------------------- #
_DIGEST_SUFFIX = "@sha256:" + "a" * 64

# registry component of the generated FROM ref.
_REGISTRY_CHOICE = st.sampled_from([
    ("ecr_public", "public.ecr.aws"),
    ("ecr_private", "123456789012.dkr.ecr.us-west-2.amazonaws.com"),
    ("nvcr_literal", "nvcr.io"),
    ("param_seam", "${BASE_REGISTRY}"),
])
_IMAGE_PATH = st.sampled_from([
    "nvidia/l4t-jetpack", "nvidia/l4t-cuda", "ubuntu/ubuntu", "docker/library/node",
])
_TAG = st.sampled_from(["r35.4.1", "r36.3.0", "11.4.19-runtime", "stable", "18-alpine"])


def _build_from_line(reg_kind, registry, image_path, tag, digest_present,
                     stage_present, nosec_present):
    ref = f"{registry}/{image_path}:{tag}"
    if digest_present:
        ref += _DIGEST_SUFFIX
    line = f"FROM {ref}"
    if stage_present:
        line += " AS somestage"
    if nosec_present:
        line += "  # nosec"
    return line, ref


def _expected_disallowed(reg_kind, digest_present, nosec_present):
    """Model of the expected classification.

    Cleared when: # nosec present; ECR host; OR (${BASE_REGISTRY} parameterized
    AND digest present). Disallowed iff a non-ECR literal registry (nvcr.io) AND
    (not parameterized OR not digest-pinned)."""
    if nosec_present:
        return False
    if reg_kind in ("ecr_public", "ecr_private"):
        return False
    if reg_kind == "param_seam":
        # Parameterized: disallowed only if NOT digest-pinned... but the audit's
        # _references_nonecr_literal_registry returns False for a parameterized
        # registry (it is not a *literal* registry), so a ${BASE_REGISTRY} ref is
        # never flagged regardless of digest.
        return False
    # nvcr_literal: non-ECR literal registry -> disallowed unless (parameterized
    # AND pinned); it is never parameterized here, so disallowed iff not pinned...
    # actually disallowed iff (not parameterized OR not pinned) == True always for
    # a literal registry that is not both. A literal nvcr.io is not parameterized,
    # so it is disallowed whether or not it is digest-pinned.
    return True


# Validates: Requirements 3.5
@settings(max_examples=25, deadline=None)
@given(
    reg=_REGISTRY_CHOICE,
    image_path=_IMAGE_PATH,
    tag=_TAG,
    digest_present=st.booleans(),
    stage_present=st.booleans(),
    nosec_present=st.booleans(),
)
def test_pbt2_from_classification(reg, image_path, tag, digest_present,
                                  stage_present, nosec_present):
    reg_kind, registry = reg
    line, ref = _build_from_line(
        reg_kind, registry, image_path, tag, digest_present, stage_present,
        nosec_present,
    )
    got = audit.is_disallowed_from(line)
    expected = _expected_disallowed(reg_kind, digest_present, nosec_present)
    assert got is expected, (
        f"is_disallowed_from({line!r}) = {got}, expected {expected} "
        f"(reg_kind={reg_kind}, digest={digest_present}, nosec={nosec_present})"
    )


# Validates: Requirements 3.5 — concrete anchor cases the grammar must honor.
def test_pbt2_anchor_cases():
    # Literal nvcr.io without digest IS flagged.
    assert audit.is_disallowed_from(
        "FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1"
    ) is True
    # Literal nvcr.io WITH digest but no ${BASE_REGISTRY} seam is STILL flagged
    # (not parameterized).
    assert audit.is_disallowed_from(
        "FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1" + _DIGEST_SUFFIX
    ) is True
    # ${BASE_REGISTRY} + @sha256 is compliant (cleared) even though the ARG
    # default is nvcr.io elsewhere in the file.
    assert audit.is_disallowed_from(
        "FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1" + _DIGEST_SUFFIX + " AS builder"
    ) is False
    # ECR hosts are never flagged.
    assert audit.is_disallowed_from(
        "FROM public.ecr.aws/ubuntu/ubuntu:22.04"
    ) is False
    # # nosec clears an otherwise-disallowed line.
    assert audit.is_disallowed_from(
        "FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1  # nosec"
    ) is False
