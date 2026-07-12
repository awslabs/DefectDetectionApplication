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
"""Shell-installer inline-JSON preservation baseline (I7–I14).

Spec: security-iam-authorization-fixes — Property 2: Preservation
(F(X) = F'(X) for every legitimate flow).

Methodology: observation-first. On the UNFIXED tree we ``jq``-style extract each
inline heredoc / ``--policy-document`` JSON body from the three customer-run
shell installers and pin it to a golden captured under
``test/backend-test/security/baselines/``. These tests PASS now (identity) and
are re-run in task 9 against the fixed tree: the golden for a MODIFIED policy
(the I7–I14 sites) will be regenerated to the narrowed shape, while every
NON-modified sibling heredoc (``LOGS_POLICY``, ``PASS_ROLE_POLICY``,
``ECR_POLICY``, and the already-scoped sibling statements) must stay
byte-for-byte identical to the baseline recorded here.

Covered sites:
  * ``deploy-account-role.sh`` — S3_POLICY (I7), SAGEMAKER_POLICY (I8),
    GREENGRASS_POLICY / AllowDDABucketPatternAccess (I9), plus preserved
    LOGS_POLICY / PASS_ROLE_POLICY / ECR_POLICY.
  * ``create-edge-device-iam-role.sh`` — INLINE_POLICY (I10 Greengrass /
    I11 IoT / I12 S3, plus preserved CloudWatch / ECR / STS sids).
  * ``launch-arm64-build-server.sh`` — DDABuildPolicy (I13 IoT / I14 S3,
    plus preserved EC2 / logs / metrics / ECR sids).

**Validates: Requirements 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_shell_installers.py \
        -p no:cacheprovider --noconftest -v
"""
import json
import os

import pytest

from _iam_preservation_support import (
    read_repo_file,
    extract_heredoc_json,
    extract_inline_policy_document,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))

DEPLOY_REL = "edge-cv-portal/deploy-account-role.sh"
EDGE_REL = "station_install/create-edge-device-iam-role.sh"
BUILD_REL = "edge-cv-portal/launch-arm64-build-server.sh"


def _golden(name):
    with open(os.path.join(BASELINES, name), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# deploy-account-role.sh — six heredocs (I7, I8, I9 + preserved siblings)
# --------------------------------------------------------------------------- #
# Heredocs NOT touched by the I7–I9 fix must be byte-for-byte identical to the
# unfixed-tree golden (pure preservation).
@pytest.mark.parametrize("varname", ["LOGS_POLICY", "PASS_ROLE_POLICY", "ECR_POLICY"])
# Validates: Requirements 3.7 — the three sibling heredocs the fix never touches.
def test_deploy_account_role_unmodified_heredocs_identical(varname):
    src = read_repo_file(DEPLOY_REL)
    current = extract_heredoc_json(src, varname)
    golden = _golden(f"iam_baseline_heredoc_deploy-account-role_{varname}.json")
    assert current == golden, (
        f"deploy-account-role.sh heredoc {varname} is NOT touched by the "
        f"I7–I9 fix and must stay byte-for-byte identical to the unfixed-tree "
        f"baseline, but it drifted"
    )


# S3_POLICY (I7): only Statement[0].Resource is narrowed from the wildcard to
# the dda-* / sagemaker-* union. Statement[1] (the already-scoped sagemaker-*
# read allowlist) and the Version must be byte-for-byte identical.
# Validates: Requirements 3.7
def test_deploy_s3_policy_preserves_sibling_statement():
    src = read_repo_file(DEPLOY_REL)
    current = extract_heredoc_json(src, "S3_POLICY")
    golden = _golden("iam_baseline_heredoc_deploy-account-role_S3_POLICY.json")
    # Preserved: everything except the I7 first statement.
    assert current["Version"] == golden["Version"]
    assert current["Statement"][1:] == golden["Statement"][1:], (
        "deploy-account-role.sh S3_POLICY sibling statement(s) drifted; the I7 "
        "fix must only narrow the first statement's Resource"
    )
    # Drift confined to I7: the first statement is no longer the arn:aws:s3:::*
    # wildcard, its Action list is unchanged, and it now scopes to dda-*/sagemaker-*.
    assert current["Statement"][0]["Action"] == golden["Statement"][0]["Action"]
    assert current["Statement"][0]["Resource"] == [
        "arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*",
        "arn:aws:s3:::sagemaker-*", "arn:aws:s3:::sagemaker-*/*",
    ]


# SAGEMAKER_POLICY (I8): only Statement[0].Resource is narrowed from "*" to the
# dda-*-prefixed job/model ARNs. Statement[1] (iam:PassRole / iam:GetRole,
# already scoped to DDASageMakerExecutionRole) must be byte-for-byte identical.
# Validates: Requirements 3.8
def test_deploy_sagemaker_policy_preserves_passrole_statement():
    src = read_repo_file(DEPLOY_REL)
    current = extract_heredoc_json(src, "SAGEMAKER_POLICY")
    golden = _golden("iam_baseline_heredoc_deploy-account-role_SAGEMAKER_POLICY.json")
    assert current["Version"] == golden["Version"]
    assert current["Statement"][1:] == golden["Statement"][1:], (
        "deploy-account-role.sh SAGEMAKER_POLICY iam:PassRole/GetRole sibling "
        "statement drifted; the I8 fix must only narrow the first statement's "
        "Resource"
    )
    # Drift confined to I8: action list unchanged, Resource no longer "*".
    assert current["Statement"][0]["Action"] == golden["Statement"][0]["Action"]
    assert current["Statement"][0]["Resource"] != "*"


# GREENGRASS_POLICY (I9): only the AllowDDABucketPatternAccess sid's Resource
# list is narrowed (the two *-dda-* substring entries removed). Every other sid
# must be byte-for-byte identical.
# Validates: Requirements 3.9
def test_deploy_greengrass_policy_preserves_other_sids():
    src = read_repo_file(DEPLOY_REL)
    current = {s["Sid"]: s for s in extract_heredoc_json(src, "GREENGRASS_POLICY")["Statement"]}
    golden = {s["Sid"]: s for s in _golden(
        "iam_baseline_heredoc_deploy-account-role_GREENGRASS_POLICY.json")["Statement"]}
    modified = {"AllowDDABucketPatternAccess"}
    for sid, gstmt in golden.items():
        if sid in modified:
            continue
        assert sid in current, f"preserved sid {sid} disappeared from GREENGRASS_POLICY"
        assert current[sid] == gstmt, (
            f"GREENGRASS_POLICY sid {sid} is NOT touched by I9 and must stay "
            f"byte-for-byte identical, but it drifted"
        )
    # Drift confined to I9: the substring-match entries are gone; only the exact
    # dda-* prefix ARNs remain.
    assert current["AllowDDABucketPatternAccess"]["Resource"] == [
        "arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*"
    ]


# Validates: Requirements 3.7 — F(X) on the unfixed tree: the I7 first statement
# is the wildcard on arn:aws:s3:::* (the baseline the fix must narrow while
# preserving the already-scoped sibling statement).
def test_deploy_s3_policy_baseline_shape():
    golden = _golden("iam_baseline_heredoc_deploy-account-role_S3_POLICY.json")
    assert golden["Statement"][0]["Resource"] == ["arn:aws:s3:::*", "arn:aws:s3:::*/*"]
    # The already-scoped sagemaker-* sibling statement is preserved verbatim.
    assert golden["Statement"][1]["Resource"] == [
        "arn:aws:s3:::sagemaker-*", "arn:aws:s3:::sagemaker-*/*"
    ]


# Validates: Requirements 3.9 — the AllowDDABucketPatternAccess sid still carries
# the substring-match entries on the unfixed tree (removed by the I9 fix); the
# AllowInferenceResultsUpload / AllowPortalComponentBucketAccess siblings are
# recorded so task 9 can assert they stay byte-for-byte identical.
def test_deploy_greengrass_policy_baseline_shape():
    golden = _golden("iam_baseline_heredoc_deploy-account-role_GREENGRASS_POLICY.json")
    sids = {s["Sid"]: s for s in golden["Statement"]}
    assert sids["AllowDDABucketPatternAccess"]["Resource"] == [
        "arn:aws:s3:::dda-*", "arn:aws:s3:::dda-*/*",
        "arn:aws:s3:::*-dda-*", "arn:aws:s3:::*-dda-*/*",
    ]
    assert "AllowInferenceResultsUpload" in sids
    assert "AllowPortalComponentBucketAccess" in sids


# --------------------------------------------------------------------------- #
# create-edge-device-iam-role.sh — INLINE_POLICY (I10, I11, I12 + preserved)
# --------------------------------------------------------------------------- #
# INLINE_POLICY: I10 rewrites GreengrassPermissions (enumerated subset), I11
# replaces IoTPermissions with resource-type-split IoT statements, I12 narrows
# S3Permissions. The CloudWatch / ECR / STS sids are NOT touched and must stay
# byte-for-byte identical to the unfixed-tree golden.
EDGE_PRESERVED_SIDS = [
    "CloudWatchLogsPermissions", "CloudWatchMetricsPermissions",
    "ECRPermissions", "STSPermissions",
]


# Validates: Requirements 3.10, 3.11, 3.12
def test_edge_device_inline_policy_preserves_untouched_sids():
    src = read_repo_file(EDGE_REL)
    current = {s.get("Sid"): s for s in extract_heredoc_json(src, "INLINE_POLICY")["Statement"]}
    golden = {s.get("Sid"): s for s in _golden(
        "iam_baseline_heredoc_create-edge-device-iam-role_INLINE_POLICY.json")["Statement"]}
    for sid in EDGE_PRESERVED_SIDS:
        assert sid in current, f"preserved sid {sid} disappeared from INLINE_POLICY"
        assert current[sid] == golden[sid], (
            f"INLINE_POLICY sid {sid} is NOT touched by I10–I12 and must stay "
            f"byte-for-byte identical, but it drifted"
        )
    # Drift confined to I10–I12: the service-wildcard sids are gone/rewritten.
    assert current["GreengrassPermissions"]["Action"] != ["greengrass:*", "greengrassv2:*"]
    assert "IoTPermissions" not in current, "I11 must split the iot:* IoTPermissions sid"
    assert current["S3Permissions"].get("Resource") != "*"


# Validates: Requirements 3.10, 3.11, 3.12 — F(X): service-wildcard actions on
# the unfixed tree, plus the preserved CloudWatch / ECR / STS siblings.
def test_edge_device_inline_policy_baseline_shape():
    golden = _golden(
        "iam_baseline_heredoc_create-edge-device-iam-role_INLINE_POLICY.json"
    )
    sids = {s["Sid"]: s for s in golden["Statement"]}
    assert sids["GreengrassPermissions"]["Action"] == ["greengrass:*", "greengrassv2:*"]
    assert sids["IoTPermissions"]["Action"] == ["iot:*"]
    assert sids["S3Permissions"]["Resource"] == "*"
    # Preserved siblings (not touched by I10–I12).
    for preserved in ("CloudWatchLogsPermissions", "CloudWatchMetricsPermissions",
                      "ECRPermissions", "STSPermissions"):
        assert preserved in sids


# --------------------------------------------------------------------------- #
# launch-arm64-build-server.sh — DDABuildPolicy (I13, I14 + preserved)
# --------------------------------------------------------------------------- #
# DDABuildPolicy: I13 replaces IoTPermissions with resource-type-split IoT
# statements, I14 narrows S3Permissions and isolates s3:ListAllMyBuckets. The
# Greengrass / EC2 / logs / metrics / ECR sids are NOT touched and must stay
# byte-for-byte identical to the unfixed-tree golden.
BUILD_PRESERVED_SIDS = [
    "GreengrassPermissions", "EC2Permissions", "CloudWatchLogsPermissions",
    "CloudWatchMetricsPermissions", "ECRPermissions",
]


# Validates: Requirements 3.13, 3.14
def test_build_server_inline_policy_preserves_untouched_sids():
    src = read_repo_file(BUILD_REL)
    current = {s.get("Sid"): s for s in extract_inline_policy_document(src, "DDABuildPolicy")["Statement"]}
    golden = {s.get("Sid"): s for s in _golden(
        "iam_baseline_heredoc_launch-arm64-build-server_DDABuildPolicy.json")["Statement"]}
    for sid in BUILD_PRESERVED_SIDS:
        assert sid in current, f"preserved sid {sid} disappeared from DDABuildPolicy"
        assert current[sid] == golden[sid], (
            f"DDABuildPolicy sid {sid} is NOT touched by I13/I14 and must stay "
            f"byte-for-byte identical, but it drifted"
        )
    # Drift confined to I13/I14: the iot:* sid is gone; the S3 wildcard is scoped.
    assert "IoTPermissions" not in current, "I13 must split the iot:* IoTPermissions sid"
    assert current["S3Permissions"].get("Resource") != "*"


# Validates: Requirements 3.13, 3.14 — F(X): iot:* wildcard and the S3 statement
# bundling s3:ListAllMyBuckets on "*"; EC2 / logs / metrics / ECR sids preserved.
def test_build_server_inline_policy_baseline_shape():
    golden = _golden(
        "iam_baseline_heredoc_launch-arm64-build-server_DDABuildPolicy.json"
    )
    sids = {s["Sid"]: s for s in golden["Statement"]}
    assert sids["IoTPermissions"]["Action"] == ["iot:*"]
    assert sids["S3Permissions"]["Resource"] == "*"
    assert "s3:ListAllMyBuckets" in sids["S3Permissions"]["Action"]
    for preserved in ("EC2Permissions", "CloudWatchLogsPermissions",
                      "CloudWatchMetricsPermissions", "ECRPermissions"):
        assert preserved in sids
