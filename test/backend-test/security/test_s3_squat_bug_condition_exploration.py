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
"""Fix-checking test (flipped in Task 7) for
security-s3-bucket-squatting-fixes.

Property 1: Fix Checking -- every in-scope S3 access is squatting-resistant:
read/write sites carry an ``aws s3api head-bucket --expected-bucket-owner``
preflight, team-owned writes read their bucket from an env var defaulting to the
current literal, docs / notebook references are placeholders or owner-noted, and
the repo audit returns zero disallowed hits -- across six in-scope sites
(B1-B6).

This file was WRITTEN IN TASK 1 to OBSERVE the counterexample shape on the
UNFIXED tree (the targeted B1-B6 tests asserted the unverified access existed,
so they PASSED on the unfixed tree). In TASK 7 -- now that the B1-B6 fixes are
applied to source -- the targeted assertions are FLIPPED to assert the
NEUTRALIZED / SECURE post-fix invariant instead. The single audit-gate test
``test_s3_squat_audit_returns_no_disallowed_hits`` already asserts the SECURE
state, so it is unchanged and is now GREEN on the fixed tree.

Secure post-fix invariants this file now asserts:
  * B1 -- deploy.py's SSM list has an ``aws s3api head-bucket
    --expected-bucket-owner`` entry preceding the ``panorama-sdk-v2-artifacts``
    sync AND preceding the three ``edgeml-sdk-longevity-tests`` accesses, and
    ``_block_unverified_accesses`` reports none unverified,
  * B2 -- publish.sh reads ``ARTIFACT_BUCKET`` (default ``panorama-sdk-v2-artifacts``)
    and a ``head-bucket`` preflight precedes the ``.deb``/``.whl`` uploads,
  * B3 -- publish.sh reads ``DOCS_BUCKET`` (default ``edgeml-sdk-docs``) and a
    ``head-bucket`` preflight runs inside the ``if [ -d "./sphinx" ]`` guard,
  * B4 -- index.rst carries an ownership ``.. note::`` and a documented
    ``head-bucket`` preflight, and ``_rst_undocumented_doc_commands`` is empty,
  * B5 -- s3.rst config sample ``"bucket"`` value is the ``<your-bucket-name>``
    placeholder (not the bare literal),
  * B6 -- the notebook derives ``old_prefix`` from a single-source
    ``sample_data_bucket`` variable, preceded by a prerequisite markdown cell,
  * the repo audit finds zero disallowed bug-condition hits.

A CLI-support characterization test records the root cause that drives the
head-bucket-preflight mechanism (the high-level ``aws s3 cp``/``sync`` reject
``--expected-bucket-owner`` while ``aws s3api head-bucket`` accepts it); it skips
gracefully when the AWS CLI is not installed in the runner.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7
"""
import os
import re
import shutil
import subprocess

import pytest

import s3_squat_audit as audit

REPO_ROOT = audit.REPO_ROOT


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _ssm_entries():
    """The ordered SSM download list from deploy.py (ast-reconstructed)."""
    return audit.extract_ssm_list(_read(audit.DEPLOY_REL))


# --------------------------------------------------------------------------- #
# Repo audit (B7 / Req 2.7) -- the gate re-run in task 7.
# --------------------------------------------------------------------------- #
def test_s3_squat_audit_returns_no_disallowed_hits():
    """The S3-squat audit must return ZERO disallowed bug-condition hits across
    the five in-scope source files, other than occurrences carrying a documented
    ``# nosec`` / placeholder / preflight exception.

    FIXED-TREE EXPECTATION: this PASSES -- the B1-B6 fixes neutralize every
    counterexample. Validates Req 2.7 (audit gate) and is the pattern gate
    re-run in task 7.
    """
    all_hits = audit.run_audit()

    # No out-of-scope tree (vendored duplicate / cdk.out) may leak in.
    leaked = [
        h for h in all_hits
        if audit.VENDORED_DUP_SUBSTRING in h.path
        or audit.EXCLUDED_PATH_SUBSTRING in h.path
    ]
    assert not leaked, f"vendored/cdk.out copies must be excluded, got: {leaked}"

    disallowed = audit.disallowed_hits()
    grouped = audit.disallowed_by_category(disallowed)

    detail_lines = []
    for label, frag in audit.IN_SCOPE_SITES.items():
        site = audit.hits_for(frag, all_hits)
        detail_lines.append(f"  {label}: {len(site)} raw hit(s)")
        detail_lines.extend(
            f"      {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno} "
            f"[{h.category}] {h.text.strip()}"
            for h in site
        )
    detail = "\n".join(detail_lines)

    assert not disallowed, (
        f"S3-squat audit found {len(disallowed)} disallowed bug-condition hit(s) "
        f"across the in-scope source (counterexamples confirming the bug), in "
        f"categories {sorted(grouped)}:\n"
        + "\n".join(
            f"  [{h.category}] {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: "
            f"{h.text.strip()}"
            for h in disallowed
        )
        + f"\n\nRaw per-site enumeration:\n{detail}"
    )


def test_run_audit_non_empty_across_all_six_sites():
    """The RAW ``run_audit()`` enumeration is NON-EMPTY at every in-scope site
    (B1-B6) -- the broad token surface still enumerates each site (now dominated
    by the added ``owner_assertion`` / ``placeholder`` clearing tokens). This is
    the enumeration anchor and stays green before and after the fix."""
    all_hits = audit.run_audit()
    assert all_hits, "expected a non-empty raw enumeration across the in-scope tree"
    for label, frag in audit.IN_SCOPE_SITES.items():
        site = audit.hits_for(frag, all_hits)
        assert site, f"expected raw run_audit() hits for {label} ({frag})"


# --------------------------------------------------------------------------- #
# B1 -- deploy.py SSM download list (Req 2.1)
# --------------------------------------------------------------------------- #
def test_b1_ssm_list_accesses_have_head_bucket_preflight():
    """B1 (Req 2.1): the ``download_edgemlsdk_release_artifacts`` SSM list now
    HAS an ``aws s3api head-bucket --expected-bucket-owner`` preflight entry
    preceding the ``aws s3 sync s3://panorama-sdk-v2-artifacts/release/...`` entry
    AND preceding the three ``aws s3 cp/sync s3://edgeml-sdk-longevity-tests/...``
    entries for their bucket, so a squatted bucket fails the batch closed before
    ``dpkg -i`` / ``pip install``. FIXED-TREE SECURE INVARIANT (this PASSES on
    the fixed tree)."""
    entries = _ssm_entries()
    assert entries, "expected to ast-extract the SSM download list from deploy.py"

    # The finding's four accesses are still present (byte-for-byte preserved).
    panorama = [i for i, e in enumerate(entries)
                if audit._S3_CLI_RE.search(e) and "panorama-sdk-v2-artifacts" in e]
    longevity = [i for i, e in enumerate(entries)
                 if audit._S3_CLI_RE.search(e) and "edgeml-sdk-longevity-tests" in e]
    assert len(panorama) == 1, f"expected 1 panorama sync entry, got {panorama}"
    assert len(longevity) == 3, (
        f"expected 3 edgeml-sdk-longevity-tests entries, got {longevity}"
    )

    # A head-bucket --expected-bucket-owner preflight now precedes each bucket's
    # accesses, associated per-bucket by list order.
    def _preflight_idx(bucket):
        return [
            i for i, e in enumerate(entries)
            if "head-bucket" in e and "--expected-bucket-owner" in e and bucket in e
        ]

    panorama_pf = _preflight_idx("panorama-sdk-v2-artifacts")
    longevity_pf = _preflight_idx("edgeml-sdk-longevity-tests")
    print(f"\n[B1 secure] panorama preflight idx={panorama_pf} sync idx={panorama}; "
          f"longevity preflight idx={longevity_pf} accesses idx={longevity}")
    assert len(panorama_pf) == 1, (
        "SECURE INVARIANT (B1): expected exactly one panorama-sdk-v2-artifacts "
        f"head-bucket preflight entry, got {panorama_pf}"
    )
    assert len(longevity_pf) == 1, (
        "SECURE INVARIANT (B1): expected exactly one edgeml-sdk-longevity-tests "
        f"head-bucket preflight entry, got {longevity_pf}"
    )
    assert panorama_pf[0] < panorama[0], (
        "SECURE INVARIANT (B1): the panorama head-bucket preflight must precede "
        "the panorama-sdk-v2-artifacts sync."
    )
    assert longevity_pf[0] < min(longevity), (
        "SECURE INVARIANT (B1): the longevity head-bucket preflight must precede "
        "all three edgeml-sdk-longevity-tests accesses."
    )

    # The precise gate now flags NO unverified accesses in the SSM list.
    unverified = audit._block_unverified_accesses(entries)
    assert unverified == [], (
        "SECURE INVARIANT (B1): the per-bucket preflight association reports NO "
        f"unverified SSM accesses, got {unverified}"
    )


def test_b1_downstream_install_steps_exist():
    """B1 (Req 2.1): the downstream ``dpkg -i`` / ``pip install`` of the
    downloaded artifacts still exists (the read-then-install path is preserved
    byte-for-byte) -- the preflight only guards it, it does not remove it."""
    text = _read(audit.DEPLOY_REL)
    assert "dpkg -i Panorama" in text, "expected the dpkg -i install step"
    assert "pip install panorama-1.0-py3-none-any.whl" in text, (
        "expected the pip install step for the downloaded wheel"
    )


# --------------------------------------------------------------------------- #
# B2 -- publish.sh .deb/.whl uploads (Req 2.2)
# --------------------------------------------------------------------------- #
def test_b2_publish_deb_whl_uploads_have_env_var_and_preflight():
    """B2 (Req 2.2): the four ``.deb``/``.whl`` ``aws s3 cp`` uploads now target
    ``s3://${ARTIFACT_BUCKET}/release/...`` (env-var indirection defaulting to
    ``panorama-sdk-v2-artifacts``) and a ``head-bucket --expected-bucket-owner``
    preflight precedes them, so the uploads resolve to the same bucket while
    failing closed on an owner mismatch. FIXED-TREE SECURE INVARIANT (this PASSES
    on the fixed tree)."""
    text = _read(audit.PUBLISH_REL)
    lines = text.splitlines()

    # ARTIFACT_BUCKET env-var indirection defaulting to the current literal.
    defaults = audit._shell_var_defaults(text)
    print(f"\n[B2 secure] shell var defaults = {defaults}")
    assert defaults.get("ARTIFACT_BUCKET") == "panorama-sdk-v2-artifacts", (
        "SECURE INVARIANT (B2): ARTIFACT_BUCKET must default to "
        f"'panorama-sdk-v2-artifacts', got {defaults.get('ARTIFACT_BUCKET')!r}"
    )

    # The four uploads now resolve to the same bucket via ${ARTIFACT_BUCKET}.
    upload_lines = [
        i for i, ln in enumerate(lines)
        if audit._S3_CLI_RE.search(ln) and "${ARTIFACT_BUCKET}" in ln
    ]
    assert len(upload_lines) == 4, (
        f"expected 4 .deb/.whl uploads to s3://${{ARTIFACT_BUCKET}}/, "
        f"got {len(upload_lines)}"
    )

    # A head-bucket preflight for ARTIFACT_BUCKET precedes the uploads.
    preflight_lines = [
        i for i, ln in enumerate(lines)
        if "head-bucket" in ln and "--expected-bucket-owner" in ln
        and "$ARTIFACT_BUCKET" in ln
    ]
    assert preflight_lines, (
        "SECURE INVARIANT (B2): expected an 'aws s3api head-bucket "
        "--expected-bucket-owner \"$ARTIFACT_BUCKET\"' preflight."
    )
    assert preflight_lines[0] < min(upload_lines), (
        "SECURE INVARIANT (B2): the ARTIFACT_BUCKET head-bucket preflight must "
        "precede the .deb/.whl uploads."
    )

    # The precise gate now flags NO unverified publish.sh accesses.
    unverified = audit._publish_unverified_accesses(text)
    assert unverified == [], (
        f"SECURE INVARIANT (B2/B3): expected no unverified publish.sh uploads, "
        f"got {unverified}"
    )


# --------------------------------------------------------------------------- #
# B3 -- publish.sh docs sync (Req 2.3)
# --------------------------------------------------------------------------- #
def test_b3_publish_docs_sync_has_env_var_and_preflight():
    """B3 (Req 2.3): the docs sync now targets ``s3://${DOCS_BUCKET}/...``
    (env-var indirection defaulting to ``edgeml-sdk-docs``) and a ``head-bucket
    --expected-bucket-owner`` preflight runs INSIDE the ``if [ -d "./sphinx" ]``
    guard before the sync. FIXED-TREE SECURE INVARIANT (this PASSES on the fixed
    tree)."""
    text = _read(audit.PUBLISH_REL)
    lines = text.splitlines()

    defaults = audit._shell_var_defaults(text)
    assert defaults.get("DOCS_BUCKET") == "edgeml-sdk-docs", (
        "SECURE INVARIANT (B3): DOCS_BUCKET must default to 'edgeml-sdk-docs', "
        f"got {defaults.get('DOCS_BUCKET')!r}"
    )

    guard_idx = next(
        (i for i, ln in enumerate(lines) if 'if [ -d "./sphinx" ]' in ln), None
    )
    assert guard_idx is not None, "expected the ./sphinx guard to exist"

    docs_preflight_idx = next(
        (i for i, ln in enumerate(lines)
         if "head-bucket" in ln and "--expected-bucket-owner" in ln
         and "$DOCS_BUCKET" in ln),
        None,
    )
    sync_idx = next(
        (i for i, ln in enumerate(lines)
         if audit._S3_CLI_RE.search(ln) and "${DOCS_BUCKET}" in ln),
        None,
    )
    print(f"\n[B3 secure] guard idx={guard_idx} preflight idx={docs_preflight_idx} "
          f"sync idx={sync_idx}")
    assert docs_preflight_idx is not None, (
        "SECURE INVARIANT (B3): expected an 'aws s3api head-bucket "
        "--expected-bucket-owner \"$DOCS_BUCKET\"' preflight."
    )
    assert sync_idx is not None, (
        "SECURE INVARIANT (B3): expected the docs sync to target "
        "s3://${DOCS_BUCKET}/..."
    )
    assert guard_idx < docs_preflight_idx < sync_idx, (
        "SECURE INVARIANT (B3): the DOCS_BUCKET head-bucket preflight must run "
        "inside the ./sphinx guard and before the sync."
    )


# --------------------------------------------------------------------------- #
# B4 -- index.rst documented install commands (Req 2.4)
# --------------------------------------------------------------------------- #
def test_b4_index_rst_cp_blocks_have_note_and_preflight():
    """B4 (Req 2.4): the dependency ``aws s3 cp
    s3://panorama-sdk-v2-artifacts/dependencies/...`` blocks and the
    ``PanoramaSDK.deb`` / ``.whl`` release-download blocks now carry an ownership
    ``.. note::`` and a documented ``aws s3api head-bucket
    --expected-bucket-owner`` preflight, so ``_rst_undocumented_doc_commands`` is
    empty. FIXED-TREE SECURE INVARIANT (this PASSES on the fixed tree)."""
    text = _read(audit.INDEX_RST_REL)
    undocumented = audit._rst_undocumented_doc_commands(text)
    print(f"\n[B4 secure] undocumented aws s3 cp commands: {undocumented}")
    assert undocumented == [], (
        "SECURE INVARIANT (B4): every documented aws s3 cp block against a "
        "predictable bucket must be preceded by an ownership .. note:: and a "
        f"documented head-bucket preflight; still undocumented: {undocumented}"
    )
    assert ".. note::" in text, (
        "SECURE INVARIANT (B4): index.rst must carry an ownership .. note:: in "
        "the installation instructions."
    )
    assert "aws s3api head-bucket --bucket panorama-sdk-v2-artifacts" in text, (
        "SECURE INVARIANT (B4): index.rst must document an 'aws s3api head-bucket "
        "--expected-bucket-owner' preflight."
    )
    assert "--expected-bucket-owner" in text, (
        "SECURE INVARIANT (B4): the documented preflight must use "
        "--expected-bucket-owner."
    )


# --------------------------------------------------------------------------- #
# B5 -- s3.rst message-broker config sample (Req 2.5)
# --------------------------------------------------------------------------- #
def test_b5_s3_rst_config_bucket_is_placeholder():
    """B5 (Req 2.5): the message-broker config sample's ``"bucket"`` value is now
    the ``<your-bucket-name>`` placeholder (not the bare
    ``"panorama-sdk-v2-artifacts"`` literal). FIXED-TREE SECURE INVARIANT (this
    PASSES on the fixed tree)."""
    text = _read(audit.S3_RST_REL)
    m = re.search(r"\"bucket\"\s*:\s*\"([^\"]+)\"", text)
    assert m is not None, "expected a \"bucket\" config value in s3.rst"
    value = m.group(1)
    print(f"\n[B5 secure] config bucket value = {value!r}")
    assert value == "<your-bucket-name>", (
        f"SECURE INVARIANT (B5): the config bucket value must be the "
        f"'<your-bucket-name>' placeholder, got {value!r}"
    )
    assert value.startswith("<") and value.endswith(">"), (
        "SECURE INVARIANT (B5): the config bucket value must be a <...> "
        "placeholder, not a bare predictable literal."
    )
    # The precise gate reports no unverified config reference in s3.rst.
    assert audit._rst_unverified_config_refs(text) == [], (
        "SECURE INVARIANT (B5): no bare predictable-literal bucket value should "
        "remain in s3.rst."
    )


# --------------------------------------------------------------------------- #
# B6 -- notebook segmentation-manifest prefix (Req 2.6)
# --------------------------------------------------------------------------- #
def test_b6_notebook_old_prefix_derived_from_sample_data_bucket():
    """B6 (Req 2.6): the ``seg_manifest`` cell now derives ``old_prefix`` from a
    single-source ``sample_data_bucket`` variable (``old_prefix =
    f's3://{sample_data_bucket}/getting-started/'``) and is preceded by a
    prerequisite markdown cell. FIXED-TREE SECURE INVARIANT (this PASSES on the
    fixed tree)."""
    text = _read(audit.NOTEBOOK_REL)
    src, idx, prev = audit._notebook_source_lines(text, "seg_manifest")
    assert src, "expected to json.load the seg_manifest cell source"

    print(f"\n[B6 secure] sample_data_bucket in cell = "
          f"{'sample_data_bucket' in src}")
    assert "sample_data_bucket" in src, (
        "SECURE INVARIANT (B6): the prefix must derive from a single-source "
        "sample_data_bucket variable."
    )
    # old_prefix is now an f-string built from the sample_data_bucket variable,
    # not a bare hardcoded literal.
    assert re.search(
        r"old_prefix\s*=\s*f['\"]s3://\{sample_data_bucket\}", src
    ), (
        "SECURE INVARIANT (B6): old_prefix must be built as "
        "f's3://{sample_data_bucket}/...'."
    )
    assert not re.search(
        r"old_prefix\s*=\s*['\"]s3://lookoutvision-", src
    ), (
        "SECURE INVARIANT (B6): old_prefix must NOT be a bare "
        "s3://lookoutvision-* literal."
    )

    # The immediately-preceding cell is a prerequisite markdown note.
    prev_is_prereq_md = bool(
        prev
        and prev.get("cell_type") == "markdown"
        and "prerequisite" in "".join(prev.get("source", [])).lower()
    )
    assert prev_is_prereq_md, (
        "SECURE INVARIANT (B6): a prerequisite markdown cell must precede the "
        "seg_manifest cell."
    )

    # The precise gate reports no bare-literal notebook prefix reference.
    assert audit._notebook_unverified_refs(text) == [], (
        "SECURE INVARIANT (B6): no bare predictable-literal download prefix "
        "should remain in the notebook."
    )


# --------------------------------------------------------------------------- #
# CLI-support characterization (root cause behind the head-bucket mechanism)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("aws") is None, reason="AWS CLI not installed")
@pytest.mark.parametrize("subcommand", ["cp", "sync"])
def test_high_level_s3_rejects_expected_bucket_owner(subcommand):
    """Characterization (design root cause): the high-level ``aws s3 cp`` /
    ``aws s3 sync`` commands REJECT ``--expected-bucket-owner`` with a
    ``ParamValidation: Unknown options`` error -- so appending it would break
    every access. This is WHY the fix uses an ``aws s3api head-bucket`` preflight
    instead. Skips gracefully when the AWS CLI is absent."""
    if subcommand == "cp":
        cmd = ["aws", "s3", "cp", "/tmp/does-not-exist-xyz", "s3://b/k",
               "--expected-bucket-owner", "123456789012"]
    else:
        cmd = ["aws", "s3", "sync", "/tmp", "s3://b/",
               "--expected-bucket-owner", "123456789012"]
    proc = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    combined = (proc.stdout + proc.stderr)
    print(f"\n[CLI characterization] aws s3 {subcommand} --expected-bucket-owner "
          f"-> {combined.strip()[:200]}")
    assert "Unknown options" in combined and "expected-bucket-owner" in combined, (
        f"expected 'aws s3 {subcommand} --expected-bucket-owner' to be rejected "
        f"with a ParamValidation Unknown options error; got: {combined!r}"
    )


@pytest.mark.skipif(shutil.which("aws") is None, reason="AWS CLI not installed")
def test_s3api_head_bucket_accepts_expected_bucket_owner():
    """Characterization (design root cause): ``aws s3api head-bucket
    --expected-bucket-owner`` ACCEPTS the flag (it does NOT raise a
    ParamValidation 'Unknown options' error -- it proceeds to a bucket lookup
    that fails later on credentials/404/403). This is the low-level preflight the
    fix relies on. Skips gracefully when the AWS CLI is absent."""
    cmd = ["aws", "s3api", "head-bucket",
           "--bucket", "dda-nonexistent-preflight-probe-xyz",
           "--expected-bucket-owner", "123456789012",
           "--region", "us-east-1", "--no-sign-request"]
    proc = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603
    combined = (proc.stdout + proc.stderr)
    print(f"\n[CLI characterization] aws s3api head-bucket "
          f"--expected-bucket-owner -> {combined.strip()[:200]}")
    assert not ("Unknown options" in combined
                and "expected-bucket-owner" in combined), (
        "expected 'aws s3api head-bucket --expected-bucket-owner' to ACCEPT the "
        f"flag (no ParamValidation Unknown options error); got: {combined!r}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
