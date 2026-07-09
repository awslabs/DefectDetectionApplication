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
"""Out-of-scope preservation guard (Req 3.18).

Spec: security-iam-authorization-fixes — Property 2: Preservation.

The IAM & Authorization fix (I1–I17) must NOT touch files owned by the sibling
remediation batches — ``security-injection-deserialization-fixes`` (findings
#1–#8) and ``security-secrets-credentials-jwt-fixes`` (S1–S9) — nor any
generated ``edge-cv-portal/infrastructure/cdk.out/asset.*`` build artifact
(those regenerate from source and are out of scope).

``iam_out_of_scope_baseline.json`` records the sha256 of a representative set of
those files on the unfixed tree. This test asserts they still hash to their
recorded values. On the unfixed tree this passes; task 9 re-runs it after the
fix to prove the fix stayed inside its 12 in-scope source files.

Mirrors the sibling ``test_preservation_out_of_scope_guard.py`` convention.

**Validates: Requirements 3.18**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_out_of_scope_guard.py \
        -p no:cacheprovider --noconftest -v
"""
import hashlib
import json
import os

from _iam_preservation_support import REPO_ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))


def _baseline():
    with open(os.path.join(BASELINES, "iam_out_of_scope_baseline.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _sha256(rel):
    with open(os.path.join(REPO_ROOT, rel), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# Validates: Requirements 3.18 — sibling-spec files are byte-for-byte unchanged.
def test_sibling_spec_files_unchanged():
    baseline = _baseline()
    recorded = baseline["sibling_spec_files"]
    assert recorded, "expected recorded sibling-spec file hashes"

    mismatches = []
    missing = []
    for rel, expected in recorded.items():
        full = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(full):
            missing.append(rel)
            continue
        actual = _sha256(rel)
        if actual != expected:
            mismatches.append(f"{rel}: {expected} -> {actual}")

    assert not missing, f"sibling-spec files disappeared: {missing}"
    assert not mismatches, (
        "sibling-spec files (owned by security-injection-deserialization-fixes / "
        "security-secrets-credentials-jwt-fixes) were modified by this spec:\n"
        + "\n".join(mismatches)
    )


# Validates: Requirements 3.18 — the representative generated cdk.out artifact is
# unchanged (skips gracefully if the build tree is absent).
def test_cdk_out_representative_unchanged():
    baseline = _baseline()
    rep = baseline.get("cdk_out_representative", {})
    if not rep:
        import pytest
        pytest.skip("no cdk.out asset representative recorded (build tree absent)")

    mismatches = []
    missing = []
    for rel, expected in rep.items():
        full = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(full):
            missing.append(rel)
            continue
        if _sha256(rel) != expected:
            mismatches.append(rel)

    # A regenerated/absent build artifact is not itself a fix regression, but a
    # MODIFIED one that still exists must match (the fix never edits cdk.out).
    assert not mismatches, f"generated cdk.out artifact was modified: {mismatches}"
