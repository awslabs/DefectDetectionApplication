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
"""Negative-fixture tests for the S3 bucket-squatting audit gate (Task 6 /
finding B7 / Req 2.7).

Task 6 finalizes ``s3_squat_audit`` so that ``disallowed_hits()`` returns ``[]``
on the fixed tree. The subtle part of the gate is that it must associate a
``head-bucket --expected-bucket-owner`` preflight with the access it guards on a
PER-BUCKET, ORDER-SENSITIVE basis -- NOT a file-global "is there a head-bucket
anywhere?" presence check. If the gate degraded to a file-global presence check,
an attacker (or an accidental regression) could drop the preflight for ONE
predictable bucket while leaving another bucket's preflight in place and the
gate would stay green -- re-opening the squatting hole.

These tests prove the per-bucket association holds by driving the module's own
parsing helpers (``extract_ssm_list``, ``_block_unverified_accesses``,
``_publish_unverified_accesses``, ``_shell_var_defaults``) against SYNTHETIC
in-memory fixtures. They deliberately DO NOT touch the real source files (those
are exercised by the live ``disallowed_hits()`` gate and the exploration test),
so a reintroduced unverified access is caught structurally regardless of the
current on-disk state.

They also confirm the two-layer contract on the current fixed tree: the raw
``run_audit()`` still enumerates the predictable literals (non-empty) while the
precise ``disallowed_hits()`` gate is empty, and both layers exclude the vendored
``src/backend/edgemlsdk/edgemlsdk/...`` duplicate and the generated ``cdk.out``.

Validates: Requirements 2.7
"""

import s3_squat_audit as audit


# --------------------------------------------------------------------------- #
# Synthetic SSM-list fixtures (deploy.py-shape) -- per-bucket association.
#
# Every fixture keeps a real ``head-bucket`` preflight token present SOMEWHERE
# in the list, so a naive file-global "head-bucket appears?" check would pass.
# The per-bucket gate must still flag the bucket whose OWN preflight is missing.
# --------------------------------------------------------------------------- #

_ACCOUNT = "123456789012"

_PANORAMA_ACCESS = (
    "aws s3 sync s3://panorama-sdk-v2-artifacts/release/1.0/x86_64/ /edgemlsdk"
)
_PANORAMA_PREFLIGHT = (
    "aws s3api head-bucket --bucket panorama-sdk-v2-artifacts "
    f"--expected-bucket-owner {_ACCOUNT}"
)
_LONGEVITY_ACCESS = "aws s3 cp s3://edgeml-sdk-longevity-tests/longevity.json /edgemlsdk/"
_LONGEVITY_PREFLIGHT = (
    "aws s3api head-bucket --bucket edgeml-sdk-longevity-tests "
    f"--expected-bucket-owner {_ACCOUNT}"
)


def _buckets(unverified):
    """Collapse a list of (idx, bucket, entry) tuples to the flagged bucket set."""
    return {bucket for _idx, bucket, _entry in unverified}


def test_ssm_list_missing_panorama_preflight_still_fails_gate():
    """Dropping the panorama-sdk-v2-artifacts preflight while KEEPING the
    edgeml-sdk-longevity-tests one must still flag the panorama access."""
    entries = [
        "mkdir -p /edgemlsdk",
        # No head-bucket for panorama-sdk-v2-artifacts here.
        _PANORAMA_ACCESS,
        _LONGEVITY_PREFLIGHT,
        _LONGEVITY_ACCESS,
        "dpkg -i /edgemlsdk/*.deb",
    ]
    flagged = _buckets(audit._block_unverified_accesses(entries))
    assert "panorama-sdk-v2-artifacts" in flagged
    assert "edgeml-sdk-longevity-tests" not in flagged


def test_ssm_list_missing_longevity_preflight_still_fails_gate():
    """Vice-versa: dropping the longevity preflight while KEEPING the panorama
    one must still flag the longevity access."""
    entries = [
        _PANORAMA_PREFLIGHT,
        _PANORAMA_ACCESS,
        # No head-bucket for edgeml-sdk-longevity-tests here.
        _LONGEVITY_ACCESS,
        "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
    ]
    flagged = _buckets(audit._block_unverified_accesses(entries))
    assert "edgeml-sdk-longevity-tests" in flagged
    assert "panorama-sdk-v2-artifacts" not in flagged


def test_ssm_list_with_both_preflights_passes_gate():
    """Positive control: a preflight for EACH bucket, each preceding its access,
    leaves no unverified access."""
    entries = [
        _PANORAMA_PREFLIGHT,
        _PANORAMA_ACCESS,
        _LONGEVITY_PREFLIGHT,
        _LONGEVITY_ACCESS,
        "aws s3 cp s3://edgeml-sdk-longevity-tests/delegates.json /edgemlsdk/",
    ]
    assert audit._block_unverified_accesses(entries) == []


def test_ssm_list_preflight_after_access_still_fails_gate():
    """Association is ORDER-SENSITIVE: a preflight that appears AFTER the access
    it purports to guard does not clear it (a batch runs sequentially)."""
    entries = [
        _PANORAMA_ACCESS,
        _PANORAMA_PREFLIGHT,  # too late -- the sync already ran
    ]
    flagged = _buckets(audit._block_unverified_accesses(entries))
    assert "panorama-sdk-v2-artifacts" in flagged


def test_file_global_preflight_presence_does_not_satisfy_gate():
    """The crux of B7: a file-global "does head-bucket appear anywhere?" check
    would be SATISFIED by the longevity preflight, but the per-bucket gate must
    still flag the unguarded panorama access. Driven through the real
    ``extract_ssm_list`` AST parse so it mirrors the deploy.py code path."""
    module_src = (
        "download_edgemlsdk_release_artifacts = [\n"
        f'    "{_LONGEVITY_PREFLIGHT}",\n'
        f'    "{_PANORAMA_ACCESS}",\n'
        f'    "{_LONGEVITY_ACCESS}",\n'
        "]\n"
    )
    entries = audit.extract_ssm_list(module_src)
    assert entries, "extract_ssm_list should parse the synthetic list"

    # A naive file-global presence check WOULD pass (a head-bucket token exists).
    assert any("head-bucket" in e for e in entries)

    # The per-bucket gate still flags the unguarded panorama access.
    flagged = _buckets(audit._block_unverified_accesses(entries))
    assert "panorama-sdk-v2-artifacts" in flagged
    assert "edgeml-sdk-longevity-tests" not in flagged


# --------------------------------------------------------------------------- #
# Synthetic publish.sh fixtures (B2 / B3 shape) -- per-bucket association with
# env-var indirection resolved through ${VAR:-default}.
# --------------------------------------------------------------------------- #

_PUBLISH_HEADER = (
    '#!/bin/bash\n'
    'ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-panorama-sdk-v2-artifacts}"\n'
    'DOCS_BUCKET="${DOCS_BUCKET:-edgeml-sdk-docs}"\n'
    'EXPECTED_BUCKET_OWNER="${EXPECTED_BUCKET_OWNER:-123456789012}"\n'
)


def test_publish_missing_artifact_preflight_still_fails_gate():
    """Dropping the ARTIFACT_BUCKET preflight while KEEPING the DOCS_BUCKET one
    must still flag the .deb/.whl upload (resolved via the env-var default)."""
    script = _PUBLISH_HEADER + (
        # No head-bucket for $ARTIFACT_BUCKET.
        'aws s3 cp foo.deb s3://${ARTIFACT_BUCKET}/release/1.0/foo.deb\n'
        'if [ -d "./sphinx" ]; then\n'
        '  aws s3api head-bucket --bucket "$DOCS_BUCKET" '
        '--expected-bucket-owner "$EXPECTED_BUCKET_OWNER" || exit 1\n'
        '  aws s3 sync ./sphinx s3://${DOCS_BUCKET}/edgeml-sdk/v1/1.0/\n'
        'fi\n'
    )
    flagged = _buckets(audit._publish_unverified_accesses(script))
    assert "panorama-sdk-v2-artifacts" in flagged
    assert "edgeml-sdk-docs" not in flagged


def test_publish_missing_docs_preflight_still_fails_gate():
    """Vice-versa: dropping the DOCS_BUCKET preflight while KEEPING the
    ARTIFACT_BUCKET one must still flag the docs sync."""
    script = _PUBLISH_HEADER + (
        'aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" '
        '--expected-bucket-owner "$EXPECTED_BUCKET_OWNER" || exit 1\n'
        'aws s3 cp foo.deb s3://${ARTIFACT_BUCKET}/release/1.0/foo.deb\n'
        'if [ -d "./sphinx" ]; then\n'
        # No head-bucket for $DOCS_BUCKET.
        '  aws s3 sync ./sphinx s3://${DOCS_BUCKET}/edgeml-sdk/v1/1.0/\n'
        'fi\n'
    )
    flagged = _buckets(audit._publish_unverified_accesses(script))
    assert "edgeml-sdk-docs" in flagged
    assert "panorama-sdk-v2-artifacts" not in flagged


def test_publish_with_both_preflights_passes_gate():
    """Positive control: a preflight for EACH env-var bucket, each preceding its
    upload, leaves no unverified access."""
    script = _PUBLISH_HEADER + (
        'aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" '
        '--expected-bucket-owner "$EXPECTED_BUCKET_OWNER" || exit 1\n'
        'aws s3 cp foo.deb s3://${ARTIFACT_BUCKET}/release/1.0/foo.deb\n'
        'aws s3 cp foo.whl s3://${ARTIFACT_BUCKET}/release/latest/foo.whl\n'
        'if [ -d "./sphinx" ]; then\n'
        '  aws s3api head-bucket --bucket "$DOCS_BUCKET" '
        '--expected-bucket-owner "$EXPECTED_BUCKET_OWNER" || exit 1\n'
        '  aws s3 sync ./sphinx s3://${DOCS_BUCKET}/edgeml-sdk/v1/1.0/\n'
        'fi\n'
    )
    assert audit._publish_unverified_accesses(script) == []


def test_publish_env_var_default_resolves_to_predictable_bucket():
    """The env-var indirection is what makes the per-bucket association work for
    publish.sh: ``${ARTIFACT_BUCKET:-panorama-sdk-v2-artifacts}`` must resolve to
    the predictable literal so the preflight and the upload key on the same
    bucket identity."""
    defaults = audit._shell_var_defaults(_PUBLISH_HEADER)
    assert defaults.get("ARTIFACT_BUCKET") == "panorama-sdk-v2-artifacts"
    assert defaults.get("DOCS_BUCKET") == "edgeml-sdk-docs"


# --------------------------------------------------------------------------- #
# Two-layer contract on the current fixed tree (Req 2.7 / Preservation 3.7).
# --------------------------------------------------------------------------- #

def test_run_audit_non_empty_but_disallowed_hits_empty_on_fixed_tree():
    """``run_audit()`` still enumerates the raw predictable literals (non-empty)
    while the precise ``disallowed_hits()`` gate is empty after B1-B6."""
    raw = audit.run_audit()
    assert raw, "run_audit() must still enumerate the raw predictable literals"

    disallowed = audit.disallowed_hits()
    assert disallowed == [], (
        "disallowed_hits() must be empty on the fixed tree; got: "
        + "; ".join(f"{h.category} {h.path}:{h.lineno}" for h in disallowed)
    )


def test_both_layers_exclude_vendored_duplicate_and_cdk_out():
    """Neither layer may surface a hit from the vendored
    src/backend/edgemlsdk/edgemlsdk/... duplicate or the generated cdk.out."""
    for h in audit.run_audit():
        assert audit.VENDORED_DUP_SUBSTRING not in h.path, h.path
        assert "cdk.out" not in h.path, h.path
    for h in audit.disallowed_hits():
        assert audit.VENDORED_DUP_SUBSTRING not in h.path, h.path
        assert "cdk.out" not in h.path, h.path
