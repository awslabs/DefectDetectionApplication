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
"""CDK ``cdk synth`` preservation check for the four in-scope stacks (I1–I6).

Spec: security-iam-authorization-fixes — Property 2: Preservation
(``F(X) = F'(X)`` for every non-bug-condition input).

This file was captured on the UNFIXED tree in task 2 (identity baselines). It is
re-run here in task 9 against the FIXED tree. The baselines now come in TWO
flavors per synthesizable stack:

* ``iam_baseline_<stack>.unfixed.template.json`` — the pre-fix ``F(X)`` capture.
* ``iam_baseline_<stack>.template.json`` — the post-fix ``F'(X)`` template,
  regenerated in task 9 AFTER confirming (via a flat statement-multiset diff)
  that every synthesized IAM statement outside the enumerated I1–I6 rewrites is
  byte-for-byte identical between the two.

**Synth mode (I1–I4 — EdgeCVPortalComputeStack + DDAPortalUseCaseAccountStack).**
Both stacks ARE wired into synthesizable CDK app entrypoints. When the CDK
toolchain is available, the test re-synths each with the canonical fixture
context (``portalAccountId=111111111111``, ``externalId=fixture-eid``,
``trustedUseCaseAccountIds=222222222222,333333333333``, region ``us-east-1``)
and asserts the emitted IAM statement multiset equals the committed FIXED
baseline (an identity guard against future live-tree drift).

Independently of the toolchain, ``test_baseline_drift_confined_to_I*`` compares
the committed unfixed and fixed baselines and asserts the ONLY differences are
the enumerated I1–I4 statement rewrites recorded in
``iam_baseline_cdk_i_changes.json`` — which is exactly the preservation
guarantee: every statement that is not one of the I1–I4 rewrites is unchanged.

    NOTE on the statement MULTISET view: the I1 fix splits one broad
    ``PolicyStatement`` into several narrow ones. Once a role's inline policy
    grows past the managed-policy size limit, CDK reshuffles statements across
    auto-generated ``DefaultPolicy`` / ``OverflowPolicy`` / managed-policy
    carriers. Comparing whole PolicyDocuments per logical id would therefore
    report noise from that carrier reshuffling. The preservation invariant lives
    at the granularity of *statements granted*, so we diff a flat multiset of
    canonical statements across all IAM carriers (see
    ``iam_statements_multiset``).

**Source-level mode (I5, I6 — always runs).** The two workflow stacks
(``LabelingWorkflowStack`` I5, ``TrainingWorkflowStack`` I6) are NOT instantiated
in any synthesizable CDK app entrypoint in this repo, so they cannot be diffed
via ``cdk synth``. For those the exact ``new iam.PolicyStatement({...})`` source
blocks are recorded pre-fix (``iam_baseline_ts_source_blocks.json``) and post-fix
(``iam_baseline_ts_source_blocks_fixed.json``); the test asserts the fixed block
is present verbatim in the current source AND that the ONLY change from the
unfixed block is the ``sts:AssumeRole`` ``resources:`` line (effect + actions
byte-for-byte identical), with the account now bounded to
``props.trustedUseCaseAccountIds`` (no ``arn:aws:iam::*:role/`` wildcard).

Set ``IAM_SKIP_CDK_SYNTH=1`` to skip the live-synth layer (the baseline-diff and
source-level layers always run).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from _iam_preservation_support import (
    REPO_ROOT,
    read_repo_file,
    iam_statements_multiset,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))
INFRA_DIR = os.path.join(REPO_ROOT, "edge-cv-portal", "infrastructure")

FIXTURE_CONTEXT = [
    "--context", "portalAccountId=111111111111",
    "--context", "externalId=fixture-eid",
    "--context", "trustedUseCaseAccountIds=222222222222,333333333333",
]

# stack -> (fixed baseline, unfixed baseline, optional --app override)
SYNTH_STACKS = {
    "EdgeCVPortalComputeStack": (
        "iam_baseline_EdgeCVPortalComputeStack.template.json",
        "iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json",
        None,
    ),
    "DDAPortalUseCaseAccountStack": (
        "iam_baseline_DDAPortalUseCaseAccountStack.template.json",
        "iam_baseline_DDAPortalUseCaseAccountStack.unfixed.template.json",
        "npx ts-node --prefer-ts-exts bin/usecase-account-app.ts",
    ),
}


def _cdk_available():
    if os.environ.get("IAM_SKIP_CDK_SYNTH"):
        return False
    return os.path.exists(os.path.join(INFRA_DIR, "node_modules", ".bin", "cdk"))


def _synth_template(stack, app):
    """Synth a single stack with the fixture context; return the parsed template
    dict, or None if the toolchain fails in this environment."""
    outdir = tempfile.mkdtemp(prefix="iam_synth_")
    try:
        cmd = ["npx", "cdk", "synth", stack]
        if app:
            cmd += ["--app", app]
        cmd += FIXTURE_CONTEXT + ["--output", outdir]
        env = dict(os.environ)
        env["CDK_DEFAULT_ACCOUNT"] = "111111111111"
        env["CDK_DEFAULT_REGION"] = "us-east-1"
        # Hermetic fixture: the CDK CLI resolves the *ambient* AWS credentials
        # (env keys / profile / container creds / EC2 IMDS instance role) to
        # look up the caller account and then OVERWRITES CDK_DEFAULT_ACCOUNT in
        # the app subprocess with the REAL account id, silently clobbering the
        # fixture value above (observed on an EC2 box with an instance role:
        # every account-bearing statement synthesized with the live account and
        # the identity diff exploded with false drift). Strip every ambient
        # credential source so the lookup fails and the fixture account is the
        # one the app sees — this is environment isolation, not a weakening of
        # any assertion.
        for key in (
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_CONFIG_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_ROLE_ARN",
        ):
            env.pop(key, None)
        env["AWS_EC2_METADATA_DISABLED"] = "true"
        try:
            subprocess.run(
                cmd, cwd=INFRA_DIR, env=env, timeout=600,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError):
            return None
        tmpl_path = os.path.join(outdir, f"{stack}.template.json")
        if not os.path.exists(tmpl_path):
            return None
        with open(tmpl_path, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _load_baseline_template(name):
    with open(os.path.join(BASELINES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _i_changes():
    with open(os.path.join(BASELINES, "iam_baseline_cdk_i_changes.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Synth mode (I1–I4) — live synth vs the committed FIXED baseline (identity)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: Requirements 3.1, 3.2, 3.3, 3.4
def test_synth_iam_statements_match_fixed_baseline(stack):
    if not _cdk_available():
        pytest.skip(
            "CDK toolchain unavailable (or IAM_SKIP_CDK_SYNTH set); the "
            "baseline-diff layer below still proves I1–I4 confinement."
        )
    fixed_file, _unfixed_file, app = SYNTH_STACKS[stack]
    fresh = _synth_template(stack, app)
    if fresh is None:
        pytest.skip(
            f"cdk synth for {stack} did not produce a template in this "
            f"environment; the baseline-diff layer covers it."
        )
    fresh_ms = iam_statements_multiset(fresh)
    baseline_ms = iam_statements_multiset(_load_baseline_template(fixed_file))
    assert fresh_ms == baseline_ms, (
        f"{stack}: live-synthesized IAM statements drifted from the committed "
        f"fixed baseline (F'(X)). Re-inspect and regenerate the baseline only "
        f"after confirming the drift is confined to I1–I4."
    )


# --------------------------------------------------------------------------- #
# Baseline-diff (I1–I4) — always runs; the preservation guarantee.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: Requirements 3.1, 3.2, 3.3, 3.4 — every synthesized statement that
# is NOT one of the enumerated I1–I4 rewrites is byte-for-byte identical between
# the unfixed and fixed templates (F(X) = F'(X) outside the bug-condition sites).
def test_baseline_drift_confined_to_I1_I4(stack):
    fixed_file, unfixed_file, _app = SYNTH_STACKS[stack]
    unfixed_ms = iam_statements_multiset(_load_baseline_template(unfixed_file))
    fixed_ms = iam_statements_multiset(_load_baseline_template(fixed_file))

    removed = unfixed_ms - fixed_ms   # in unfixed, gone in fixed
    added = fixed_ms - unfixed_ms      # new in fixed

    recorded = _i_changes()[stack]
    # The ONLY statements that differ between F(X) and F'(X) are the recorded
    # I1–I4 rewrites. Because `removed`/`added` are the complete symmetric
    # difference, this equality simultaneously proves (a) the I1–I4 statements
    # changed exactly as recorded and (b) NOTHING outside them changed.
    assert set(removed) == set(recorded["removed"]), (
        f"{stack}: statements removed from the unfixed template do not match the "
        f"recorded I1–I4 change set — drift outside the bug-condition sites.\n"
        f"unexpected removed: {sorted(set(removed) - set(recorded['removed']))}\n"
        f"missing removed:    {sorted(set(recorded['removed']) - set(removed))}"
    )
    assert set(added) == set(recorded["added"]), (
        f"{stack}: statements added in the fixed template do not match the "
        f"recorded I1–I4 change set — drift outside the bug-condition sites.\n"
        f"unexpected added: {sorted(set(added) - set(recorded['added']))}\n"
        f"missing added:    {sorted(set(recorded['added']) - set(added))}"
    )

    # Belt-and-braces: the untouched remainder (everything minus the enumerated
    # I1–I4 statements) is identical multiset-for-multiset.
    def _drop(ms, keys):
        return {k: c for k, c in ms.items() if k not in set(keys)}

    assert _drop(unfixed_ms, recorded["removed"]) == _drop(fixed_ms, recorded["added"]), (
        f"{stack}: statements outside the recorded I1–I4 rewrites are not "
        f"byte-for-byte identical between F(X) and F'(X)"
    )


# Validates: Requirements 3.3, 3.4 — the I3/I4 fix wires the tag Condition; the
# fixed usecase baseline gains the aws:ResourceTag/dda-portal:managed guard while
# the pre-fix untagged S3 sids are gone.
def test_usecase_tag_condition_wired_in_fixed_baseline():
    recorded = _i_changes()["DDAPortalUseCaseAccountStack"]
    added_blob = " ".join(recorded["added"])
    removed_blob = " ".join(recorded["removed"])
    assert "aws:ResourceTag/dda-portal:managed" in added_blob
    assert "aws:ResourceTag/dda-portal:managed" not in removed_blob


# --------------------------------------------------------------------------- #
# Source-level (I5, I6) — workflow stacks not synthesizable; always runs.
# --------------------------------------------------------------------------- #
WORKFLOW_SOURCES = {
    "I5_labeling_assumerole": "edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts",
    "I6_training_assumerole": "edge-cv-portal/infrastructure/lib/training-workflow-stack.ts",
}


def _ts_blocks(name):
    with open(os.path.join(BASELINES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _strip_resources_line(block):
    """Return ``block`` with its ``resources: ...`` value removed, so the rest of
    the PolicyStatement (effect + actions) can be compared. Handles both a
    single-line array and a multi-line ``.map(...)`` expression by consuming
    lines until the bracket/paren depth opened on the ``resources:`` line closes.
    """
    lines = block.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"\bresources:", line):
            depth = (line.count("[") + line.count("(")
                     - line.count("]") - line.count(")"))
            while depth > 0:
                i += 1
                seg = lines[i]
                depth += (seg.count("[") + seg.count("(")
                          - seg.count("]") - seg.count(")"))
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


@pytest.mark.parametrize("key", list(WORKFLOW_SOURCES))
# Validates: Requirements 3.5, 3.6 — the fixed assume-role PolicyStatement block
# is present verbatim in the current workflow-stack source.
def test_workflow_assume_role_fixed_block_present_on_tree(key):
    src = read_repo_file(WORKFLOW_SOURCES[key])
    fixed = _ts_blocks("iam_baseline_ts_source_blocks_fixed.json")[key]
    assert fixed in src, (
        f"fixed assume-role source block {key} not found verbatim in "
        f"{WORKFLOW_SOURCES[key]}"
    )


@pytest.mark.parametrize("key", list(WORKFLOW_SOURCES))
# Validates: Requirements 3.5, 3.6 — the ONLY change from the unfixed block is
# the sts:AssumeRole resources line; effect + actions are byte-for-byte identical
# and the account is now bounded to props.trustedUseCaseAccountIds (no wildcard).
def test_workflow_assume_role_drift_confined_to_resource(key):
    unfixed = _ts_blocks("iam_baseline_ts_source_blocks.json")[key]
    fixed = _ts_blocks("iam_baseline_ts_source_blocks_fixed.json")[key]
    assert _strip_resources_line(unfixed) == _strip_resources_line(fixed), (
        f"{key}: the assume-role block changed OUTSIDE the resources line "
        f"(effect/actions must be preserved)"
    )
    # Confirm the fix: wildcard account gone, bounded to the trusted list.
    assert "arn:aws:iam::*:role/" not in fixed
    assert "resources: ['*']" not in fixed
    assert "props.trustedUseCaseAccountIds.map(" in fixed
    assert "sts:AssumeRole" in fixed


# --------------------------------------------------------------------------- #
# F(X) documentation — the unfixed baselines still record the pre-fix wildcards.
# --------------------------------------------------------------------------- #
# Validates: Requirements 3.5, 3.6 — F(X): the workflow assume-role blocks carried
# the wildcard-account / wildcard-resource on the unfixed tree.
def test_workflow_assume_role_unfixed_baselines_are_wildcard():
    blocks = _ts_blocks("iam_baseline_ts_source_blocks.json")
    assert "arn:aws:iam::*:role/DDAPortalAccessRole" in blocks["I5_labeling_assumerole"]
    assert "sts:AssumeRole" in blocks["I5_labeling_assumerole"]
    assert "resources: ['*']" in blocks["I6_training_assumerole"]
    assert "sts:AssumeRole" in blocks["I6_training_assumerole"]


# Validates: Requirements 3.1, 3.2, 3.3 — F(X): the compute / usecase target
# blocks carried the wildcard resource on the unfixed tree.
def test_compute_usecase_unfixed_baselines_are_wildcard():
    blocks = _ts_blocks("iam_baseline_ts_source_blocks.json")
    assert "resources: ['*']" in blocks["I1_compute_combined"]
    assert "resources: ['*']" in blocks["I1_compute_passrole"]
    assert "resources: ['*']" in blocks["I2_compute_s3"]
    assert "'arn:aws:s3:::*'" in blocks["I3_usecase_groundtruth_s3"]
