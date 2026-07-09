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
"""Bug-condition exploration test (Task 1) for
security-iam-authorization-fixes.

Property 1: Bug Condition -- an IAM policy statement in an in-scope file grants
privileges wider than the calling code path uses: a scopable action on
``resources: ['*']`` / ``"Resource": "*"``, a ``service:*`` action wildcard over
an enumerable subset, ``sts:AssumeRole`` on ``resources: ['*']`` /
``arn:aws:iam::*:role/...`` (wildcard account), or a tag-based restriction
declared in a code comment but not enforced by a ``Condition`` -- across the
seventeen in-scope sites (I1-I17).

**These tests are written to assert the SECURE (post-fix) behavior, so most are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure surfaces the counterexample
that confirms the bug exists:

  * I1  -- compute-stack combined PolicyStatement grants ``resources: ['*']``
    alongside ``sagemaker:CreateTrainingJob`` / ``iot:UpdateThingShadow`` / ...;
    the sibling ``iam:PassRole`` statement is on ``resources: ['*']``,
  * I2  -- the portal-account S3 statement is on ``resources: ['*']`` with no
    tag ``conditions:`` block,
  * I3  -- the Ground Truth role S3 grant is on ``arn:aws:s3:::*`` with no
    ``conditions:``,
  * I4  -- ``S3BucketAccess`` / ``S3ObjectAccess`` carry no ``conditions:``
    despite the ``dda-portal:managed`` comment,
  * I5  -- labeling monitor ``sts:AssumeRole`` on ``arn:aws:iam::*:role/...``,
  * I6  -- training assume-role Lambda ``sts:AssumeRole`` on ``resources: ['*']``,
  * I7/I8/I9 -- deploy-account-role.sh heredocs on ``"Resource": "*"`` /
    ``arn:aws:s3:::*`` / ``*-dda-*``,
  * I10/I11/I12 -- create-edge-device-iam-role.sh ``greengrass:*`` / ``iot:*`` /
    S3 ``"Resource": "*"``,
  * I13/I14 -- launch-arm64-build-server.sh ``iot:*`` / S3 ``"Resource": "*"``,
  * I15 -- edge-device-iam-policy.json ``IoTDataPlane`` on ``"Resource": "*"``,
  * I16/I17 -- README example JSONs with ``greengrass:*`` / ``iot:*`` / S3
    wildcards,
  * the repo audit still finds disallowed bug-condition hits (non-empty).

The SAME audit-gate test (``test_iam_audit_returns_no_disallowed_hits``) is
re-run in task 8 against the fixed tree, where it must PASS (zero disallowed
hits, minus documented exceptions).

Hypothesis (vendored under .hypothesis/) is used minimally where the input
domain is generatable (I5/I6 trusted-vs-untrusted account IDs against the
wildcard-account grant), scoped to concrete failing shapes for reproducibility.
The full preservation PBTs live in the Task 2 suite.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11,
1.12, 1.13, 1.14, 1.15, 1.16, 1.17, 1.18
"""
import os
import re
import sys

import pytest
from hypothesis import given, settings, HealthCheck, example
from hypothesis import strategies as st

import iam_audit

REPO_ROOT = iam_audit.REPO_ROOT

# The extraction helpers this spec's Task 2 baselines already use live in the
# preservation/ subdir; add it to the path so we reuse the SAME heredoc /
# inline-policy / .ts-block / README-fence extractors (keeps Task 1 consistent
# with the preservation suite).
_PRESERVATION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preservation")
if _PRESERVATION_DIR not in sys.path:
    sys.path.insert(0, _PRESERVATION_DIR)

from _iam_preservation_support import (  # noqa: E402
    read_repo_file,
    extract_heredoc_json,
    extract_inline_policy_document,
    extract_ts_policy_block,
    iter_json_fences,
)

# In-scope source paths (relative to REPO_ROOT).
COMPUTE_REL = "edge-cv-portal/infrastructure/lib/compute-stack.ts"
USECASE_REL = "edge-cv-portal/infrastructure/lib/usecase-account-stack.ts"
LABELING_REL = "edge-cv-portal/infrastructure/lib/labeling-workflow-stack.ts"
TRAINING_REL = "edge-cv-portal/infrastructure/lib/training-workflow-stack.ts"
DEPLOY_REL = "edge-cv-portal/deploy-account-role.sh"
EDGE_REL = "station_install/create-edge-device-iam-role.sh"
BUILD_REL = "edge-cv-portal/launch-arm64-build-server.sh"
EDGE_JSON_REL = "station_install/edge-device-iam-policy.json"
README_REL = "README_main.md"


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _block_has_wildcard_resource(block):
    """True if a CDK ``new iam.PolicyStatement({...})`` block declares
    ``resources: ['*']``."""
    return re.search(r"resources\s*:\s*\[\s*['\"]\*['\"]", block) is not None


def _block_actions(block):
    """Return the quoted actions of an ``actions: [...]`` array in a CDK block."""
    m = re.search(r"actions\s*:\s*\[(.*?)\]", block, re.DOTALL)
    if not m:
        return []
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))


def _block_has_conditions(block):
    """True if a CDK statement block declares a ``conditions:`` key."""
    return re.search(r"\bconditions\s*:", block) is not None


def _statements(policy):
    stmts = policy.get("Statement", [])
    return [stmts] if isinstance(stmts, dict) else stmts


def _sid_map(policy):
    return {s.get("Sid"): s for s in _statements(policy) if isinstance(s, dict)}


def _as_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


# --------------------------------------------------------------------------- #
# Repo audit (I18 / Req 2.18) -- the gate re-run in task 8.
# --------------------------------------------------------------------------- #
def test_iam_audit_returns_no_disallowed_hits():
    """The IAM audit must return ZERO disallowed bug-condition hits across the
    nine in-scope source files, other than statements carrying a documented
    ``# nosec`` / ``// nosec`` / ``"//"`` exception.

    UNFIXED-TREE EXPECTATION: this FAILS -- the disallowed hits it lists ARE the
    I1-I17 counterexamples. Validates Req 1.18 (audit gate) and is the pattern
    gate re-run in task 8.
    """
    all_hits = iam_audit.run_audit()

    # None of the generated CDK artifacts may leak into the audit result.
    leaked = [h for h in all_hits if iam_audit.EXCLUDED_PATH_SUBSTRING in h.path]
    assert not leaked, f"cdk.out/asset.* copies must be excluded, got: {leaked}"

    disallowed = iam_audit.disallowed_hits()
    grouped = iam_audit.disallowed_by_category(disallowed)

    # Per-site raw enumeration for the failure message.
    per_site = {}
    for label, frag in iam_audit.IN_SCOPE_SITES.items():
        per_site[label] = [
            f"{os.path.relpath(h.path, REPO_ROOT)}:{h.lineno} [{h.category}] {h.text.strip()}"
            for h in iam_audit.hits_for(frag, all_hits)
        ]
    detail_lines = []
    for label, lines in per_site.items():
        detail_lines.append(f"  {label}: {len(lines)} raw hit(s)")
        detail_lines.extend(f"      {ln}" for ln in lines)
    detail = "\n".join(detail_lines)

    assert not disallowed, (
        f"IAM audit found {len(disallowed)} disallowed bug-condition hit(s) "
        f"across the in-scope source (counterexamples confirming the bug), in "
        f"categories {sorted(grouped)}:\n"
        + "\n".join(
            f"  [{h.category}] {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: {h.text.strip()}"
            for h in disallowed
        )
        + f"\n\nRaw per-site enumeration:\n{detail}"
    )


def test_run_audit_neutralized_across_all_four_categories():
    """Enumeration sanity anchor (fix-checking, task 8): after the fix the RAW
    ``run_audit()`` may still surface documented / unscopable wildcards, but
    ``disallowed_hits()`` drops to ZERO -- so NONE of the four bug categories
    retains a disallowed counterexample.

    FLIPPED from the exploration form: on the unfixed tree this asserted the RAW
    enumeration was NON-EMPTY across all four categories (proving the bug); it is
    re-run in task 8 to assert the NEUTRALIZED state (every category clean).
    """
    disallowed = iam_audit.disallowed_hits()
    grouped = iam_audit.disallowed_by_category(disallowed)
    categories = set(grouped)
    bug_categories = {
        "cdk_wildcard_resource",
        "json_resource_wildcard",
        "service_wildcard_action",
        "assume_role_wildcard_account",
    }
    assert not (categories & bug_categories), (
        "Expected ZERO disallowed counterexamples in every bug category on the "
        f"fixed tree; still found disallowed hits in "
        f"{sorted(categories & bug_categories)}"
    )


# --------------------------------------------------------------------------- #
# I1 -- compute-stack combined PolicyStatement + sibling iam:PassRole
# --------------------------------------------------------------------------- #
def test_i1_combined_statement_wildcard_resource_with_scopable_actions():
    """I1 (Req 1.1): the combined portal Lambda PolicyStatement grants
    ``resources: ['*']`` alongside scopable SageMaker / Greengrass / IoT /
    execute-api actions. UNFIXED-TREE EXPECTATION: FAILS (the block IS the
    wildcard-resource counterexample)."""
    ts = read_repo_file(COMPUTE_REL)
    block = extract_ts_policy_block(ts, "'sagemaker:CreateTrainingJob'")
    actions = _block_actions(block)
    print(f"\n[I1 counterexample] combined statement actions ({len(actions)}), "
          f"resources wildcard={_block_has_wildcard_resource(block)}")

    scopable = {
        "sagemaker:CreateTrainingJob", "iot:UpdateThingShadow",
        "execute-api:Invoke", "greengrass:CreateComponentVersion",
    }
    present = scopable.intersection(actions)
    assert not (_block_has_wildcard_resource(block) and present), (
        "COUNTEREXAMPLE (I1): the portal Lambda combined PolicyStatement grants "
        f"resources: ['*'] with scopable actions {sorted(present)} in the same "
        "block."
    )


def test_i1_sibling_pass_role_wildcard_resource():
    """I1 (Req 1.1): the sibling ``iam:PassRole`` statement is on
    ``resources: ['*']``. UNFIXED-TREE EXPECTATION: FAILS."""
    ts = read_repo_file(COMPUTE_REL)
    block = extract_ts_policy_block(ts, "'iam:PassRole'")
    assert "iam:PassRole" in _block_actions(block)
    assert not _block_has_wildcard_resource(block), (
        "COUNTEREXAMPLE (I1): the sibling iam:PassRole statement grants "
        "resources: ['*'] (should be scoped to arn:aws:iam::*:role/DDA*Role)."
    )


# --------------------------------------------------------------------------- #
# I2 -- portal-account S3 statement on resources: ['*'], no tag condition
# --------------------------------------------------------------------------- #
def test_i2_portal_s3_statement_wildcard_no_tag_condition():
    """I2 (Req 1.2): the portal-account S3 statement (~line 183) is on
    ``resources: ['*']`` with scopable S3 actions and NO tag ``conditions:``.
    UNFIXED-TREE EXPECTATION: FAILS."""
    ts = read_repo_file(COMPUTE_REL)
    block = extract_ts_policy_block(ts, "'s3:GetBucketTagging'")
    actions = _block_actions(block)
    assert "s3:GetObject" in actions and "s3:PutObject" in actions
    print(f"\n[I2 counterexample] portal S3 statement wildcard="
          f"{_block_has_wildcard_resource(block)} conditions="
          f"{_block_has_conditions(block)}")

    scopable = [a for a in actions if a != "s3:ListAllMyBuckets"]
    assert not (_block_has_wildcard_resource(block) and scopable
                and not _block_has_conditions(block)), (
        "COUNTEREXAMPLE (I2): the portal-account S3 statement grants "
        f"resources: ['*'] with scopable actions {scopable} and no tag "
        "conditions block."
    )


# --------------------------------------------------------------------------- #
# I3 -- Ground Truth DDASageMakerExecutionRole S3 grant, no tag condition
# --------------------------------------------------------------------------- #
def test_i3_ground_truth_s3_wildcard_no_condition():
    """I3 (Req 1.3): the Ground Truth role S3 grant (~line 197) is on
    ``arn:aws:s3:::*`` / ``arn:aws:s3:::*/*`` with NO ``conditions:`` block.
    UNFIXED-TREE EXPECTATION: FAILS."""
    ts = read_repo_file(USECASE_REL)
    block = extract_ts_policy_block(ts, "'s3:GetBucketCors'")
    assert "s3:GetObject" in _block_actions(block)
    has_bucket_wildcard = "arn:aws:s3:::*" in block
    print(f"\n[I3 counterexample] groundTruthRole S3 bucket-wildcard="
          f"{has_bucket_wildcard} conditions={_block_has_conditions(block)}")

    assert not (has_bucket_wildcard and not _block_has_conditions(block)), (
        "COUNTEREXAMPLE (I3): the Ground Truth DDASageMakerExecutionRole S3 grant "
        "uses arn:aws:s3:::* / arn:aws:s3:::*/* with no "
        "aws:ResourceTag/dda-portal:managed=true Condition."
    )


# --------------------------------------------------------------------------- #
# I4 -- DDAPortalAccessRole S3BucketAccess / S3ObjectAccess unenforced tag
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sid", ["S3BucketAccess", "S3ObjectAccess"])
def test_i4_portal_access_role_sid_missing_tag_condition(sid):
    """I4 (Req 1.4): the ``S3BucketAccess`` / ``S3ObjectAccess`` sids carry NO
    ``conditions:`` block despite the two-lines-up comment declaring buckets
    must be tagged ``dda-portal:managed=true``. UNFIXED-TREE EXPECTATION:
    FAILS."""
    ts = read_repo_file(USECASE_REL)
    block = extract_ts_policy_block(ts, f"'{sid}'")
    print(f"\n[I4 counterexample] sid={sid} conditions="
          f"{_block_has_conditions(block)}")
    assert _block_has_conditions(block), (
        f"COUNTEREXAMPLE (I4): the {sid} sid has no conditions block, so the "
        "'dda-portal:managed=true' restriction promised by the in-code comment "
        "is NOT enforced."
    )


def test_i4_tag_comment_present_but_unenforced():
    """I4 (Req 1.4): confirm the comment that PROMISES the tag restriction is
    actually present in the file (so the missing Condition is a real gap, not a
    misread)."""
    ts = read_repo_file(USECASE_REL)
    assert "dda-portal:managed" in ts and "must be tagged" in ts.lower(), (
        "expected the 'Buckets must be tagged with dda-portal:managed' comment "
        "to exist in usecase-account-stack.ts"
    )


# --------------------------------------------------------------------------- #
# I5 -- labeling monitor sts:AssumeRole on wildcard account
# --------------------------------------------------------------------------- #
def test_i5_labeling_assume_role_wildcard_account():
    """I5 (Req 1.5): after the fix the labeling monitor bounds ``sts:AssumeRole``
    to the mapped ``props.trustedUseCaseAccountIds`` list -- no
    ``arn:aws:iam::*:role/`` wildcard account. VECTOR NEUTRALIZED.

    NOTE: the fix added an explanatory comment mentioning ``sts:AssumeRole``
    ahead of the statement, so we anchor extraction on the neutralized
    ``trustedUseCaseAccountIds.map(...)`` resource expression instead."""
    ts = read_repo_file(LABELING_REL)
    block = extract_ts_policy_block(ts, "trustedUseCaseAccountIds.map")
    assert "sts:AssumeRole" in _block_actions(block)
    print(f"\n[I5 neutralized] labeling assume-role resources bounded to "
          f"trustedUseCaseAccountIds (wildcard-account present="
          f"{'arn:aws:iam::*:role/' in block})")
    assert "arn:aws:iam::*:role/" not in block, (
        "VECTOR NEUTRALIZED CHECK (I5): the LabelingMonitorFunction sts:AssumeRole "
        "statement must not contain an arn:aws:iam::*:role/ wildcard account."
    )
    assert "trustedUseCaseAccountIds.map" in block, (
        "expected the sts:AssumeRole resources to be bounded to the mapped "
        "props.trustedUseCaseAccountIds list."
    )


# --------------------------------------------------------------------------- #
# I6 -- training assume-role Lambda sts:AssumeRole on resources: ['*']
# --------------------------------------------------------------------------- #
def test_i6_training_assume_role_wildcard_resource():
    """I6 (Req 1.6): after the fix the training ``assumeRoleFunction`` bounds
    ``sts:AssumeRole`` to the mapped ``props.trustedUseCaseAccountIds`` list --
    no ``resources: ['*']`` and no ``arn:aws:iam::*:role/`` wildcard account.
    VECTOR NEUTRALIZED.

    NOTE: the fix added an explanatory comment mentioning ``sts:AssumeRole``
    ahead of the statement, so we anchor extraction on the neutralized
    ``trustedUseCaseAccountIds.map(...)`` resource expression instead."""
    ts = read_repo_file(TRAINING_REL)
    block = extract_ts_policy_block(ts, "trustedUseCaseAccountIds.map")
    assert "sts:AssumeRole" in _block_actions(block)
    print(f"\n[I6 neutralized] training assume-role wildcard-resource="
          f"{_block_has_wildcard_resource(block)}")
    assert not _block_has_wildcard_resource(block), (
        "VECTOR NEUTRALIZED CHECK (I6): the assumeRoleFunction sts:AssumeRole "
        "statement must not grant resources: ['*'] (bounded to "
        "props.trustedUseCaseAccountIds)."
    )
    assert "arn:aws:iam::*:role/" not in block and "trustedUseCaseAccountIds.map" in block, (
        "expected the sts:AssumeRole resources to be bounded to the mapped "
        "props.trustedUseCaseAccountIds list (no wildcard account)."
    )


# --------------------------------------------------------------------------- #
# I7 / I8 / I9 -- deploy-account-role.sh heredocs
# --------------------------------------------------------------------------- #
def test_i7_deploy_s3_policy_first_statement_wildcard():
    """I7 (Req 1.7): the ``S3_POLICY`` first statement is on
    ``["arn:aws:s3:::*", "arn:aws:s3:::*/*"]``. UNFIXED-TREE EXPECTATION:
    FAILS."""
    policy = extract_heredoc_json(read_repo_file(DEPLOY_REL), "S3_POLICY")
    first = _statements(policy)[0]
    print(f"\n[I7 counterexample] S3_POLICY[0].Resource = {first['Resource']}")
    assert first["Resource"] != ["arn:aws:s3:::*", "arn:aws:s3:::*/*"], (
        "COUNTEREXAMPLE (I7): deploy-account-role.sh S3_POLICY first statement "
        f"is on {first['Resource']} (unscoped s3:::* wildcard)."
    )


def test_i8_deploy_sagemaker_policy_wildcard_resource():
    """I8 (Req 1.8): the ``SAGEMAKER_POLICY`` statement is on ``"Resource": "*"``.
    UNFIXED-TREE EXPECTATION: FAILS."""
    policy = extract_heredoc_json(read_repo_file(DEPLOY_REL), "SAGEMAKER_POLICY")
    first = _statements(policy)[0]
    print(f"\n[I8 counterexample] SAGEMAKER_POLICY[0].Resource = {first['Resource']}")
    assert first["Resource"] != "*", (
        "COUNTEREXAMPLE (I8): deploy-account-role.sh SAGEMAKER_POLICY statement "
        "grants sagemaker actions on \"Resource\": \"*\"."
    )


def test_i9_deploy_greengrass_substring_bucket_pattern():
    """I9 (Req 1.9): the ``AllowDDABucketPatternAccess`` sid contains the
    substring-match ``arn:aws:s3:::*-dda-*`` entries. UNFIXED-TREE EXPECTATION:
    FAILS."""
    policy = extract_heredoc_json(read_repo_file(DEPLOY_REL), "GREENGRASS_POLICY")
    sid = _sid_map(policy)["AllowDDABucketPatternAccess"]
    resources = _as_list(sid["Resource"])
    print(f"\n[I9 counterexample] AllowDDABucketPatternAccess.Resource = {resources}")
    substring_entries = [r for r in resources if "*-dda-*" in r]
    assert not substring_entries, (
        "COUNTEREXAMPLE (I9): AllowDDABucketPatternAccess contains substring-match "
        f"S3 patterns {substring_entries} (match any bucket containing 'dda')."
    )


# --------------------------------------------------------------------------- #
# I10 / I11 / I12 -- create-edge-device-iam-role.sh INLINE_POLICY
# --------------------------------------------------------------------------- #
def test_i10_edge_greengrass_service_wildcard_actions():
    """I10 (Req 1.10): ``GreengrassPermissions`` grants ``greengrass:*`` /
    ``greengrassv2:*``. UNFIXED-TREE EXPECTATION: FAILS."""
    policy = extract_heredoc_json(read_repo_file(EDGE_REL), "INLINE_POLICY")
    actions = _as_list(_sid_map(policy)["GreengrassPermissions"]["Action"])
    print(f"\n[I10 counterexample] GreengrassPermissions.Action = {actions}")
    wildcards = [a for a in actions if a in ("greengrass:*", "greengrassv2:*")]
    assert not wildcards, (
        "COUNTEREXAMPLE (I10): GreengrassPermissions grants service-wildcard "
        f"actions {wildcards}."
    )


def test_i11_edge_iot_service_wildcard_action():
    """I11 (Req 1.11): after the fix the ``iot:*`` service wildcard is gone --
    the single ``IoTPermissions`` sid is split into resource-scoped sids
    (``IoTConnect`` / ``IoTDataPlane`` / ``IoTThingShadow`` /
    ``IoTDescribeEndpoint``) enumerating only the exercised actions. VECTOR
    NEUTRALIZED."""
    policy = extract_heredoc_json(read_repo_file(EDGE_REL), "INLINE_POLICY")
    iot_actions = [a for s in _statements(policy)
                   for a in _as_list(s.get("Action")) if a.startswith("iot:")]
    print(f"\n[I11 neutralized] edge IoT actions = {iot_actions}")
    assert "iot:*" not in iot_actions, (
        "VECTOR NEUTRALIZED CHECK (I11): no statement may grant the iot:* "
        f"service wildcard; got {iot_actions}."
    )
    assert iot_actions, "expected the enumerated IoT actions to remain present"


def test_i12_edge_s3_wildcard_resource():
    """I12 (Req 1.12): ``S3Permissions`` is on ``"Resource": "*"``. UNFIXED-TREE
    EXPECTATION: FAILS."""
    policy = extract_heredoc_json(read_repo_file(EDGE_REL), "INLINE_POLICY")
    resource = _sid_map(policy)["S3Permissions"]["Resource"]
    print(f"\n[I12 counterexample] S3Permissions.Resource = {resource}")
    assert resource != "*", (
        "COUNTEREXAMPLE (I12): create-edge-device-iam-role.sh S3Permissions "
        "grants S3 actions on \"Resource\": \"*\"."
    )


# --------------------------------------------------------------------------- #
# I13 / I14 -- launch-arm64-build-server.sh DDABuildPolicy
# --------------------------------------------------------------------------- #
def test_i13_build_server_iot_service_wildcard_action():
    """I13 (Req 1.13): after the fix the build-server ``iot:*`` service wildcard
    is gone -- IoT permissions are split into resource-scoped sids
    (``IoTThingPermissions`` / ``IoTJobPermissions`` / ``IoTEndpointDiscovery``)
    enumerating only the build-server-needed actions. VECTOR NEUTRALIZED."""
    policy = extract_inline_policy_document(read_repo_file(BUILD_REL), "DDABuildPolicy")
    iot_actions = [a for s in _statements(policy)
                   for a in _as_list(s.get("Action")) if a.startswith("iot:")]
    print(f"\n[I13 neutralized] build IoT actions = {iot_actions}")
    assert "iot:*" not in iot_actions, (
        "VECTOR NEUTRALIZED CHECK (I13): the build-server policy must not grant "
        f"the iot:* service wildcard; got {iot_actions}."
    )
    assert iot_actions, "expected the enumerated IoT actions to remain present"


def test_i14_build_server_s3_wildcard_with_listallmybuckets():
    """I14 (Req 1.14): the build-server ``S3Permissions`` is on ``"Resource":
    "*"`` and bundles ``s3:ListAllMyBuckets`` with scopable S3 actions.
    UNFIXED-TREE EXPECTATION: FAILS."""
    policy = extract_inline_policy_document(read_repo_file(BUILD_REL), "DDABuildPolicy")
    s3 = _sid_map(policy)["S3Permissions"]
    actions = _as_list(s3["Action"])
    print(f"\n[I14 counterexample] build S3Permissions.Resource = {s3['Resource']}, "
          f"has ListAllMyBuckets={'s3:ListAllMyBuckets' in actions}")
    scopable = [a for a in actions if a != "s3:ListAllMyBuckets"]
    assert not (s3["Resource"] == "*" and scopable), (
        "COUNTEREXAMPLE (I14): build-server S3Permissions grants scopable S3 "
        f"actions {scopable} (bundled with s3:ListAllMyBuckets) on "
        "\"Resource\": \"*\"."
    )


# --------------------------------------------------------------------------- #
# I15 -- edge-device-iam-policy.json IoTDataPlane sid
# --------------------------------------------------------------------------- #
def test_i15_edge_device_json_iot_dataplane_wildcard():
    """I15 (Req 1.15): after the fix the data-plane actions
    (``Connect`` / ``Publish`` / ``Subscribe`` / ``Receive``) are split by IoT
    resource type (``client/dda-*``, ``topic/dda/*``, ``topicfilter/dda/*``) and
    none is on ``"Resource": "*"``. VECTOR NEUTRALIZED."""
    import json
    policy = json.loads(read_repo_file(EDGE_JSON_REL))
    dataplane = {"iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"}
    found = []
    offenders = []
    for s in _statements(policy):
        acts = [a for a in _as_list(s.get("Action")) if a in dataplane]
        if acts:
            found.extend(acts)
            if "*" in _as_list(s.get("Resource")):
                offenders.append((s.get("Sid"), acts))
    print(f"\n[I15 neutralized] data-plane actions = {found}, "
          f"wildcard offenders = {offenders}")
    assert not offenders, (
        "VECTOR NEUTRALIZED CHECK (I15): IoT data-plane actions must be scoped "
        f"by resource type, none on \"Resource\": \"*\"; offenders={offenders}."
    )
    assert dataplane.issubset(set(found)), (
        f"expected all data-plane actions scoped and present; got {found}"
    )


def test_i15_edge_device_json_preserved_sids_present():
    """I15 preservation anchor: the four sids that MUST stay byte-for-byte
    (``GreengrassComponentDownload``, ``CloudWatchLogsUpload``,
    ``AssumeDataAccountRole``, ``GreengrassConnectivity``) exist on the unfixed
    tree. This PASSES now and after the fix."""
    import json
    policy = json.loads(read_repo_file(EDGE_JSON_REL))
    sids = _sid_map(policy)
    for preserved in ("GreengrassComponentDownload", "CloudWatchLogsUpload",
                      "AssumeDataAccountRole", "GreengrassConnectivity"):
        assert preserved in sids, f"expected preserved sid {preserved}"


# --------------------------------------------------------------------------- #
# I16 / I17 -- README example policies
# --------------------------------------------------------------------------- #
def _readme_fences():
    """Return (build_policy, greengrass_policy) parsed JSON from the two README
    ```json code fences.

    On the FIXED tree the ``greengrass:*`` / ``iot:*`` service wildcards used to
    identify the fences are gone (that is the neutralization), so the fences are
    now identified by their enumerated-action signatures: the build-server
    policy creates IoT things (``iot:CreateThing``); the edge-device greengrass
    policy connects to the data plane (``iot:Connect``) and never creates
    things."""
    md = read_repo_file(README_REL)
    build_policy = None
    greengrass_policy = None
    for _match, parsed in iter_json_fences(md):
        if not isinstance(parsed, dict):
            continue
        actions_flat = []
        for stmt in _statements(parsed):
            actions_flat.extend(_as_list(stmt.get("Action")))
        # dda-build-policy: build-server creates/attaches IoT things.
        if "iot:CreateThing" in actions_flat:
            build_policy = parsed
        # dda-greengrass-policy: edge-device data-plane connect (no CreateThing).
        elif "iot:Connect" in actions_flat:
            greengrass_policy = parsed
    return build_policy, greengrass_policy


def test_i16_readme_build_policy_wildcards():
    """I16 (Req 1.16): after the fix the ``dda-build-policy`` example JSON no
    longer grants ``greengrass:*`` / ``iot:*`` service wildcards, and no scopable
    S3 action sits on ``"Resource": "*"`` (``s3:ListAllMyBuckets`` isolated in
    its own statement). VECTOR NEUTRALIZED."""
    build_policy, _ = _readme_fences()
    assert build_policy is not None, "dda-build-policy JSON fence not found"
    all_actions = []
    s3_wildcard_scopable = []
    for stmt in _statements(build_policy):
        actions = _as_list(stmt.get("Action"))
        all_actions.extend(actions)
        if "*" in _as_list(stmt.get("Resource")):
            s3_wildcard_scopable.extend(
                a for a in actions
                if a.startswith("s3:") and a != "s3:ListAllMyBuckets"
            )
    service_wildcards = [a for a in all_actions
                         if a in ("greengrass:*", "greengrassv2:*", "iot:*")]
    print(f"\n[I16 neutralized] service wildcards={service_wildcards}, "
          f"scopable-S3-on-wildcard={s3_wildcard_scopable}")
    assert not service_wildcards, (
        "VECTOR NEUTRALIZED CHECK (I16): README dda-build-policy still grants "
        f"service wildcards {service_wildcards}."
    )
    assert not s3_wildcard_scopable, (
        f"VECTOR NEUTRALIZED CHECK (I16): scopable S3 actions {s3_wildcard_scopable} "
        "must not be on \"Resource\": \"*\"."
    )


def test_i17_readme_greengrass_policy_wildcards():
    """I17 (Req 1.17): after the fix the ``dda-greengrass-policy`` example JSON
    no longer grants the ``greengrass:*`` service wildcard and its S3 statement
    is scoped to ``dda-component-*`` / ``dda-inference-results-*`` (no
    ``arn:aws:s3:::*`` wildcard). VECTOR NEUTRALIZED."""
    _, greengrass_policy = _readme_fences()
    assert greengrass_policy is not None, "dda-greengrass-policy JSON fence not found"

    all_actions = []
    s3_wildcard_resources = []
    for stmt in _statements(greengrass_policy):
        actions = _as_list(stmt.get("Action"))
        all_actions.extend(actions)
        if any(a.startswith("s3:") for a in actions):
            for r in _as_list(stmt.get("Resource")):
                if re.match(r"arn:aws:s3:::\*", r) or r == "*":
                    s3_wildcard_resources.append(r)
    print(f"\n[I17 neutralized] dda-greengrass-policy greengrass:* present="
          f"{'greengrass:*' in all_actions}, S3 wildcard resources="
          f"{s3_wildcard_resources}")

    assert "greengrass:*" not in all_actions and not s3_wildcard_resources, (
        "VECTOR NEUTRALIZED CHECK (I17): README dda-greengrass-policy must not "
        "grant the greengrass:* service wildcard nor S3 actions on "
        f"{s3_wildcard_resources or 'arn:aws:s3:::*'}."
    )


# --------------------------------------------------------------------------- #
# I5 / I6 -- minimal property test: the wildcard-account grant matches ANY
# account id (the pre-fix over-broad behavior). Hypothesis, scoped small.
# --------------------------------------------------------------------------- #
def _wildcard_arn_to_regex(arn):
    """Convert an IAM resource ARN wildcard to a regex (``*`` -> ``.*``)."""
    return "^" + re.escape(arn).replace(r"\*", ".*") + "$"


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(account_id=st.text(alphabet="0123456789", min_size=12, max_size=12))
@example(account_id="999988887777")
def test_i5_wildcard_account_no_longer_matches_arbitrary_account_property(account_id):
    """I5 (property, Req 1.5): after the fix the labeling stack no longer
    contains a ``arn:aws:iam::*:role/DDAPortalAccessRole`` wildcard-account
    grant, so an ``sts:AssumeRole`` target in an ARBITRARY (untrusted) 12-digit
    account is NOT matched by any literal resource in the source -- the account
    portion is templated from ``props.trustedUseCaseAccountIds`` at synth time.

    FLIPPED from the exploration property: on the unfixed tree this PASSED,
    demonstrating the wildcard grant matched any account (characterizing the
    bug); it is re-run here to confirm the vector is bounded (neutralized).
    """
    ts = read_repo_file(LABELING_REL)
    block = extract_ts_policy_block(ts, "trustedUseCaseAccountIds.map")
    assert "arn:aws:iam::*:role/DDAPortalAccessRole" not in block, (
        "VECTOR NEUTRALIZED CHECK (I5): the wildcard-account resource ARN must "
        "not appear in the labeling stack sts:AssumeRole statement."
    )
    # Any literal ARN in source is templated (arn:aws:iam::${id}:role/...), so it
    # can never equal a concrete arbitrary-account target.
    literal_arns = re.findall(
        r"arn:aws:iam::[^`'\"]*:role/DDAPortalAccessRole", block
    )
    target = f"arn:aws:iam::{account_id}:role/DDAPortalAccessRole"
    assert target not in literal_arns, (
        f"unexpected literal grant matching arbitrary account {account_id} "
        f"({target}); resources must be bounded to props.trustedUseCaseAccountIds."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
