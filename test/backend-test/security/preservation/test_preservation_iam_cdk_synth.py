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
and asserts the emitted IAM grants equal the committed FIXED baseline (an
identity guard against future live-tree drift). The live comparison is at
grant-atom granularity (one effect/action/resource/condition tuple per
action x resource, see ``iam_grant_atoms``) so an aws-cdk-lib upgrade that
only regroups the statements a grant method emits is not reported as drift,
while any permission added or removed still is.

Independently of the toolchain, ``test_baseline_drift_confined_to_I*`` compares
the committed unfixed and fixed baselines and asserts the ONLY differences are
the enumerated I1–I4 statement rewrites recorded in
``iam_baseline_cdk_i_changes.json`` — which is exactly the preservation
guarantee: every statement that is not one of the I1–I4 rewrites is unchanged.

**Post-fix record (security-scan-remediation-high).** The owner-approved
``iam_baseline_post_fix_changes.json`` records later narrowing of the FIXED
baselines, which ``iam_post_fix_approved_additions.json`` can't express: each
``rescope`` replaces one statement by narrower ones in its carrier, and each
``refreshed_resources`` entry takes its encryption properties (or, for a KMS
key or alias, the whole resource) from a fresh synth. The drift test reverses
the rescopes before its I1–I4 comparison, so the Unfixed_Snapshots never
change; ``test_post_fix_rescopes_only_narrow``,
``test_refreshed_resources_match_synth`` and ``test_unfixed_snapshots_unchanged``
check the record itself.

Two more kinds (portal-authorization-and-iam-scoping, design I5 "IAM-baseline
golden update") describe changes to statements the ``createLambdaRole``
factory emits once per role: each ``conditioned`` entry narrows a statement
with a tag condition (plus, at most, an unconditioned ``carve_out`` on the same
resources whose extra actions are the ABAC status read), each ``retired`` entry
removes one with a reason naming its replacement, and both carry ``count``, the
statement's copies in the fixed baseline before the edit. They are applied to
the fixed baseline by a scripted edit, reversed by ``count`` before the I1–I4
comparison, and checked by ``test_post_fix_conditioned_only_narrow``,
``test_post_fix_retired_statements``, ``test_factory_role_count_matches_record``
and ``test_approved_additions_object_wildcards_conditioned``.

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
import collections
import fnmatch
import hashlib
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
    iam_grant_atoms,
    iam_statements_multiset,
    statement_grant_atoms,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))
INFRA_DIR = os.path.join(REPO_ROOT, "edge-cv-portal", "infrastructure")

FIXTURE_CONTEXT = [
    "--context", "portalAccountId=111111111111",
    "--context", "externalId=fixture-eid",
    "--context", "trustedUseCaseAccountIds=222222222222,333333333333",
    "--context", "portalRegistryEnforced=false",
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
            "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_ROLE_ARN",
        ):
            env.pop(key, None)
        # The shared-config files are REDIRECTED, not unset: unsetting
        # AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE makes the SDK fall back
        # to its DEFAULTS (~/.aws/credentials, ~/.aws/config), so on any
        # developer box with a configured default profile the CLI still
        # resolves a caller account and clobbers the fixture value — the very
        # leak this block exists to prevent (observed here: the fixture
        # portalAccountId synthesized as 111111111111 while every
        # `this.account` statement carried the real account, producing false
        # drift in DDAPortalUseCaseAccountStack). Point them at paths that
        # cannot exist instead.
        env["AWS_SHARED_CREDENTIALS_FILE"] = os.path.join(
            outdir, "no-such-credentials")
        env["AWS_CONFIG_FILE"] = os.path.join(outdir, "no-such-config")
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


def _approved_additions(stack):
    """Return a Counter of the statements this stack is REVIEWED to synthesize on
    top of its fixed baseline.

    Features merged after the I1–I4 rewrite legitimately add IAM statements. Those
    are recorded here — not folded into ``iam_baseline_cdk_i_changes.json``, which
    must keep describing only the I1–I4 rewrite so that
    ``test_baseline_drift_confined_to_I1_I4`` keeps proving that confinement, and
    not merged into the fixed baseline template, which stays the reviewed F'(X)
    capture. Anything the tree synthesizes that is neither in the baseline nor in
    this allowlist still fails the gate, and removals still fail unconditionally.
    """
    path = os.path.join(BASELINES, "iam_post_fix_approved_additions.json")
    if not os.path.exists(path):
        return collections.Counter()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    entry = (data.get("stacks") or {}).get(stack) or {}
    return collections.Counter(entry.get("approved_additions") or [])


POST_FIX_RECORD = os.path.join(BASELINES, "iam_baseline_post_fix_changes.json")
# Properties a refreshed resource takes from the synth, and the resource types
# a refresh adds whole (design "R14 and R15 fixtures and deploy constraints").
ENCRYPTION_PROPERTIES = ("KmsMasterKeyId", "SqsManagedSseEnabled", "SSESpecification")
WHOLE_RESOURCE_TYPES = ("AWS::KMS::Key", "AWS::KMS::Alias")


def _post_fix_entry(stack):
    """This stack's entry in the owner-approved post-fix record: its
    ``rescopes``, ``refreshed_resources`` and ``unfixed_snapshot_sha256``."""
    with open(POST_FIX_RECORD, encoding="utf-8") as fh:
        data = json.load(fh)
    return (data.get("stacks") or {}).get(stack) or {}


def _reverse_rescopes(stack, fixed_ms):
    """Return the FIXED baseline's statement multiset with every recorded
    post-fix change undone: each rescope's ``added`` statement taken out and
    its ``removed`` one put back; each ``conditioned`` entry's ``added`` and
    ``carve_out`` statements taken out ``count`` times and its ``removed``
    one put back ``count`` times; each ``retired`` entry's ``removed``
    statement put back ``count`` times. Every recorded statement taken out
    must be in the baseline that many times."""
    ms = collections.Counter(fixed_ms)
    entry = _post_fix_entry(stack)
    for rescope in entry.get("rescopes") or []:
        for stmt in rescope["added"]:
            assert ms[stmt] > 0, (
                f"{stack}: recorded rescope statement missing from the fixed "
                f"baseline ({rescope['carrier']}): {stmt}"
            )
            ms[stmt] -= 1
        for stmt in rescope["removed"]:
            ms[stmt] += 1
    for conditioned in entry.get("conditioned") or []:
        count = conditioned["count"]
        taken = list(conditioned["added"])
        if conditioned.get("carve_out"):
            taken.append(conditioned["carve_out"])
        for stmt in taken:
            assert ms[stmt] >= count, (
                f"{stack}: recorded conditioned statement has fewer than {count} "
                f"copies in the fixed baseline ({conditioned['carrier']}): {stmt}"
            )
            ms[stmt] -= count
        for stmt in conditioned["removed"]:
            ms[stmt] += count
    for retired in entry.get("retired") or []:
        for stmt in retired["removed"]:
            ms[stmt] += retired["count"]
    return +ms


def _retired_atoms(stack):
    """The grant atoms of the statements the post-fix record retires, each
    counted once per carrier it left (``count``)."""
    atoms = collections.Counter()
    for retired in _post_fix_entry(stack).get("retired") or []:
        for stmt in retired["removed"]:
            for atom in statement_grant_atoms(json.loads(stmt)):
                atoms[atom] += retired["count"]
    return atoms


_SYNTH_CACHE = {}


def _fresh_synth(stack):
    """One fixture synth per stack per run, shared by the synth tests."""
    if stack not in _SYNTH_CACHE:
        _SYNTH_CACHE[stack] = _synth_template(stack, SYNTH_STACKS[stack][2])
    return _SYNTH_CACHE[stack]


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
    fixed_file = SYNTH_STACKS[stack][0]
    fresh = _fresh_synth(stack)
    if fresh is None:
        pytest.skip(
            f"cdk synth for {stack} did not produce a template in this "
            f"environment; the baseline-diff layer covers it."
        )
    # Grant-atom sets (see iam_grant_atoms): stable across CDK statement
    # regrouping, exact on every permission.
    fresh_ms = iam_grant_atoms(fresh)
    baseline_ms = iam_grant_atoms(_load_baseline_template(fixed_file))
    approved_ms = collections.Counter()
    for canonical, count in _approved_additions(stack).items():
        for atom in statement_grant_atoms(json.loads(canonical)):
            approved_ms[atom] += count

    # Removals are never acceptable: a statement disappearing means a grant the
    # baseline recorded is gone, which the allowlist must not be able to excuse.
    # The one exception is a statement the post-fix record retires (with the
    # replacement its reason names), and only that.
    removed = (baseline_ms - fresh_ms) - _retired_atoms(stack)
    assert not removed, (
        f"{stack}: statements recorded in the fixed baseline (F'(X)) are NO LONGER "
        f"synthesized. This is a regression, not growth, and cannot be approved "
        f"via iam_post_fix_approved_additions.json.\n"
        f"missing: {sorted(removed)[:5]}"
    )

    unreviewed = fresh_ms - baseline_ms - approved_ms
    assert not unreviewed, (
        f"{stack}: the live tree synthesizes IAM statements that are in neither the "
        f"fixed baseline (F'(X)) nor the reviewed additions allowlist. Inspect them, "
        f"then either fix the grant or add it to "
        f"iam_post_fix_approved_additions.json with its attribution. Do NOT "
        f"regenerate the fixed baseline: that would fold the change into the "
        f"recorded I1–I4 rewrite.\n"
        f"unreviewed ({sum(unreviewed.values())}): {sorted(unreviewed)[:5]}"
    )

    # The allowlist must not rot: every approved statement has to still be real.
    stale = approved_ms - fresh_ms
    assert not stale, (
        f"{stack}: iam_post_fix_approved_additions.json approves statements the tree "
        f"no longer synthesizes; drop them so the allowlist keeps describing reality.\n"
        f"stale: {sorted(stale)[:5]}"
    )

    assert fresh_ms == baseline_ms + approved_ms, (
        f"{stack}: live-synthesized IAM statements do not equal the fixed baseline "
        f"plus the reviewed additions."
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
    # The owner-approved post-fix rescopes are reversed first, so the I1–I4
    # comparison below runs unchanged against the untouched Unfixed_Snapshot.
    fixed_ms = _reverse_rescopes(
        stack, iam_statements_multiset(_load_baseline_template(fixed_file)))

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


def _actions(stmt):
    value = stmt.get("Action", [])
    return set(value if isinstance(value, list) else [value])


@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: security-scan-remediation-high Requirements 15.1, 15.2, 15.4
# (Property 9: rescopes only narrow).
def test_post_fix_rescopes_only_narrow(stack):
    fixed_ms = iam_statements_multiset(_load_baseline_template(SYNTH_STACKS[stack][0]))
    for rescope in _post_fix_entry(stack).get("rescopes") or []:
        where = f"{stack} {rescope['carrier']}"
        removed = [json.loads(s) for s in rescope["removed"]]
        added = [json.loads(s) for s in rescope["added"]]
        assert removed and added, f"{where}: a rescope needs removed and added statements"
        for stmt in removed + added:
            assert not {"NotAction", "NotResource", "NotPrincipal"} & set(stmt), (
                f"{where}: rescopes support Action/Resource statements only: {stmt}")
        # Narrowing from '*' only: any resource set is narrower, so no grant widens.
        for stmt in removed:
            assert stmt.get("Resource") == "*", f"{where}: removed statement not on '*': {stmt}"
        # Same effect and condition, so the split changes resources only.
        shape = {(s["Effect"], json.dumps(s.get("Condition"), sort_keys=True)) for s in removed + added}
        assert len(shape) == 1, f"{where}: effect or condition differs across the rescope: {shape}"
        # Exactly the removed actions: none gained, none lost (Requirement 15.4).
        removed_actions = set().union(*map(_actions, removed))
        added_actions = set().union(*map(_actions, added))
        assert added_actions == removed_actions, (
            f"{where}: gained {sorted(added_actions - removed_actions)}, "
            f"lost {sorted(removed_actions - added_actions)}")
        # Only the recorded unscopable actions stay on '*' (Requirement 15.2).
        unscopable = set(rescope["unscopable_actions"])
        assert unscopable <= removed_actions, f"{where}: unscopable actions not in the removed statement"
        for stmt in added:
            resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
            if "*" in resources:
                assert _actions(stmt) <= unscopable, (
                    f"{where}: scopable actions on '*': {sorted(_actions(stmt) - unscopable)}")
        # The rescope is applied: no removed statement remains in the fixed baseline.
        for stmt in rescope["removed"]:
            assert fixed_ms[stmt] == 0, f"{where}: removed statement still in the fixed baseline"


@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: security-scan-remediation-high Requirements 16.3, 17.5 — the
# refreshed Baseline_Template resources equal a fresh synth.
def test_refreshed_resources_match_synth(stack):
    if not _cdk_available():
        pytest.skip(
            "CDK toolchain unavailable (or IAM_SKIP_CDK_SYNTH set); the refreshed "
            "resources are compared with a fresh synth on the host."
        )
    refreshed = _post_fix_entry(stack).get("refreshed_resources") or []
    if not refreshed:
        return
    fresh = _fresh_synth(stack)
    if fresh is None:
        pytest.skip(f"cdk synth for {stack} did not produce a template in this environment.")
    fixed = _load_baseline_template(SYNTH_STACKS[stack][0])["Resources"]
    for lid in refreshed:
        assert lid in fixed and lid in fresh["Resources"], f"{stack}: {lid} missing"
        new = fresh["Resources"][lid]
        if new["Type"] in WHOLE_RESOURCE_TYPES:
            assert fixed[lid] == new, f"{stack}: {lid} differs from the fresh synth"
            continue
        got = {k: fixed[lid].get("Properties", {}).get(k) for k in ENCRYPTION_PROPERTIES}
        want = {k: new.get("Properties", {}).get(k) for k in ENCRYPTION_PROPERTIES}
        assert any(v is not None for v in want.values()), f"{stack}: {lid} is unencrypted in the synth"
        assert got == want, f"{stack}: {lid} encryption {got} != synth {want}"


@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: security-scan-remediation-high Requirement 16.4 — the
# Unfixed_Snapshots are never edited.
def test_unfixed_snapshots_unchanged(stack):
    expected = _post_fix_entry(stack).get("unfixed_snapshot_sha256")
    assert expected, f"{stack}: no unfixed_snapshot_sha256 in the post-fix record"
    with open(os.path.join(BASELINES, SYNTH_STACKS[stack][1]), "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == expected, (
            f"{stack}: {SYNTH_STACKS[stack][1]} changed; the Unfixed_Snapshots are never edited")


# --------------------------------------------------------------------------- #
# Post-fix record: conditioned and retired factory statements
# (portal-authorization-and-iam-scoping, design I5 "IAM-baseline golden update")
# --------------------------------------------------------------------------- #
# The tag condition the conditioned data-plane statements carry
# (PORTAL_MANAGED_TAG_CONDITION, edge-cv-portal/infrastructure/lib/context-helpers.ts).
PORTAL_MANAGED_TAG_CONDITION = {"StringEquals": {"aws:ResourceTag/dda-portal:managed": "true"}}
# The only action a carve-out may add to the actions of the statement it was
# split from: the ABAC status read, as the landed statements spell it.
CARVE_OUT_EXTRA_ACTIONS = {"s3:GetBucketABAC"}
S3_OBJECT_ACTIONS = ("s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectVersion",
                     "s3:PutObjectTagging")
FACTORY_CARRIER_PREFIX = "createLambdaRole"


def _resources(stmt):
    """A statement's resources as canonical JSON strings (string ARNs and
    intrinsic-function values alike)."""
    value = stmt.get("Resource", [])
    return {json.dumps(r, sort_keys=True) for r in (value if isinstance(value, list) else [value])}


def _strings(value):
    """Every string inside a JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _approved_additions_list(stack):
    with open(os.path.join(BASELINES, "iam_post_fix_approved_additions.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    return ((data.get("stacks") or {}).get(stack) or {}).get("approved_additions") or []


@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: portal-authorization-and-iam-scoping Requirement 7.10 — a
# conditioned entry only narrows, and the fixed baseline carries it exactly
# `count` times.
def test_post_fix_conditioned_only_narrow(stack):
    fixed_ms = iam_statements_multiset(_load_baseline_template(SYNTH_STACKS[stack][0]))
    for entry in _post_fix_entry(stack).get("conditioned") or []:
        where = f"{stack} {entry['site']}"
        count = entry["count"]
        assert isinstance(count, int) and count > 0, f"{where}: count must be a positive integer"
        assert entry["added_condition"], f"{where}: a conditioned entry adds a non-empty condition"
        assert len(entry["removed"]) == 1 and len(entry["added"]) == 1, (
            f"{where}: one removed and one added statement per entry")
        removed, added = json.loads(entry["removed"][0]), json.loads(entry["added"][0])
        for stmt in (removed, added):
            assert not {"NotAction", "NotResource", "NotPrincipal"} & set(stmt), (
                f"{where}: conditioned entries support Action/Resource statements only: {stmt}")
        assert added["Effect"] == removed["Effect"], f"{where}: the effect changed"
        assert "Condition" not in removed, f"{where}: a condition would be removed"
        assert added.get("Condition") == entry["added_condition"], (
            f"{where}: the added statement's condition is not the recorded added_condition")
        assert _resources(added) <= _resources(removed), f"{where}: the resources widened"
        removed_actions, added_actions = _actions(removed), _actions(added)
        assert added_actions <= removed_actions, (
            f"{where}: the conditioned statement gained {sorted(added_actions - removed_actions)}")
        carve_out = entry.get("carve_out")
        if carve_out is None:
            assert added_actions == removed_actions, (
                f"{where}: lost {sorted(removed_actions - added_actions)} without a carve-out")
        else:
            carved = json.loads(carve_out)
            assert carved["Effect"] == removed["Effect"], f"{where}: the carve-out's effect differs"
            assert "Condition" not in carved, f"{where}: the carve-out must be unconditioned"
            assert not {"NotAction", "NotResource", "NotPrincipal"} & set(carved)
            assert _resources(carved) == _resources(removed), (
                f"{where}: the carve-out is not on the removed statement's resources")
            assert added_actions | _actions(carved) >= removed_actions, (
                f"{where}: lost {sorted(removed_actions - added_actions - _actions(carved))}")
            assert _actions(carved) - removed_actions <= CARVE_OUT_EXTRA_ACTIONS, (
                f"{where}: the carve-out adds {sorted(_actions(carved) - removed_actions)}")
            assert fixed_ms[carve_out] == count, (
                f"{where}: the carve-out has {fixed_ms[carve_out]} copies in the fixed baseline, not {count}")
        # The change is applied: the removed statement is gone, the added one
        # (and the carve-out, above) present once per carrier.
        assert fixed_ms[entry["removed"][0]] == 0, f"{where}: removed statement still in the fixed baseline"
        assert fixed_ms[entry["added"][0]] == count, (
            f"{where}: the added statement has {fixed_ms[entry['added'][0]]} copies in the fixed "
            f"baseline, not {count}")


@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: portal-authorization-and-iam-scoping Requirement 7.10 — a
# retired statement is gone from the fixed baseline, its reason names the
# replacement, and its count is the number of copies the drift test puts back.
def test_post_fix_retired_statements(stack):
    fixed_ms = iam_statements_multiset(_load_baseline_template(SYNTH_STACKS[stack][0]))
    unfixed_ms = iam_statements_multiset(_load_baseline_template(SYNTH_STACKS[stack][1]))
    i_added = set(_i_changes()[stack]["added"])
    entry = _post_fix_entry(stack)
    approved_actions = set()
    for canonical in _approved_additions_list(stack):
        approved_actions |= _actions(json.loads(canonical))
    factory_counts = {fixed_ms[c["added"][0]] for c in entry.get("conditioned") or []}
    for retired in entry.get("retired") or []:
        where = f"{stack} {retired['site']}"
        count = retired["count"]
        assert isinstance(count, int) and count > 0, f"{where}: count must be a positive integer"
        assert len(retired["removed"]) == 1, f"{where}: one removed statement per entry"
        stmt = retired["removed"][0]
        assert fixed_ms[stmt] == 0, f"{where}: retired statement still in the fixed baseline"
        reason = retired.get("reason") or ""
        named = set(re.findall(r"\b[a-z0-9-]+:[A-Z][A-Za-z]+\b", reason))
        assert reason.strip() and named & approved_actions, (
            f"{where}: the reason must name the replacement grant (an action the approved "
            f"additions carry): {reason!r}")
        if stmt in i_added:
            # A factory statement the I1-I4 rewrite added: one copy per factory
            # carrier, read as the conditioned entries' added statement's copies.
            assert len(factory_counts) == 1, f"{where}: no single factory carrier count to compare"
            assert count == next(iter(factory_counts)), (
                f"{where}: count {count} is not the factory carrier count {sorted(factory_counts)}")
        else:
            # It predates the rewrite: the drift test puts back exactly the
            # copies the Unfixed_Snapshot holds.
            assert count == unfixed_ms[stmt], (
                f"{where}: count {count} != {unfixed_ms[stmt]} copies in the unfixed baseline")


@pytest.mark.parametrize("stack", list(SYNTH_STACKS))
# Validates: portal-authorization-and-iam-scoping Requirement 7.10 — the
# factory's role count is the record's: the fixture synth's principals
# holding each conditioned statement equal its copies in the fixed baseline
# plus its copies in the approved additions, and every factory entry carries
# the same count.
def test_factory_role_count_matches_record(stack):
    entry = _post_fix_entry(stack)
    factory = [e for e in (entry.get("conditioned") or []) + (entry.get("retired") or [])
               if e["carrier"].startswith(FACTORY_CARRIER_PREFIX)]
    if not factory:
        return
    assert len({e["count"] for e in factory}) == 1, (
        f"{stack}: the factory entries disagree on count: {[(e['site'], e['count']) for e in factory]}")
    if not _cdk_available():
        pytest.skip(
            "CDK toolchain unavailable (or IAM_SKIP_CDK_SYNTH set); the principal count "
            "is read from a fresh synth on the host.")
    fresh = _fresh_synth(stack)
    if fresh is None:
        pytest.skip(f"cdk synth for {stack} did not produce a template in this environment.")
    fresh_atoms = iam_grant_atoms(fresh)
    fixed_ms = iam_statements_multiset(_load_baseline_template(SYNTH_STACKS[stack][0]))
    approved = collections.Counter(_approved_additions_list(stack))
    for conditioned in entry.get("conditioned") or []:
        statements = list(conditioned["added"])
        if conditioned.get("carve_out"):
            statements.append(conditioned["carve_out"])
        for stmt in statements:
            principals = {fresh_atoms[atom] for atom in statement_grant_atoms(json.loads(stmt))}
            assert principals == {fixed_ms[stmt] + approved[stmt]}, (
                f"{stack}: {sorted(principals)} principals hold {stmt} in the synth, the record "
                f"accounts for {fixed_ms[stmt]} (fixed baseline) + {approved[stmt]} (approved additions)")


# Validates: portal-authorization-and-iam-scoping Requirement 7.10 — no
# approved addition of the compute stack grants an S3 object action on the
# objects of every bucket without the tag condition.
def test_approved_additions_object_wildcards_conditioned():
    for entry in _post_fix_entry("EdgeCVPortalComputeStack").get("conditioned") or []:
        assert entry["added_condition"] == PORTAL_MANAGED_TAG_CONDITION
    for canonical in _approved_additions_list("EdgeCVPortalComputeStack"):
        stmt = json.loads(canonical)
        actions = stmt.get("Action", stmt.get("NotAction", []))
        actions = actions if isinstance(actions, list) else [actions]
        grants_objects = "NotAction" in stmt or any(
            fnmatch.fnmatchcase(object_action, action) for action in actions for object_action in S3_OBJECT_ACTIONS)
        on_every_bucket = any(re.match(r"^arn:aws:s3:::\*/", s)
                              for s in _strings(stmt.get("Resource", stmt.get("NotResource"))))
        if grants_objects and on_every_bucket:
            assert stmt.get("Condition") == PORTAL_MANAGED_TAG_CONDITION, (
                f"approved addition grants S3 object actions on every bucket without the tag "
                f"condition: {canonical}")


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
