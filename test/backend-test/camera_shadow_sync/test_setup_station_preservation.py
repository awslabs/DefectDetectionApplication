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
"""Preservation tests — setup_station.sh content anchors
(camera-shadow-sync-provisioning).

Spec: camera-shadow-sync-provisioning — preservation tests written BEFORE the
fix (observation-first). They PASS on the unfixed tree, recording the golden
behavior, and must KEEP passing after the fix: the Gap 1 fix rewrites only the
thing-policy ensure block and the step 3.6 comment/warning prose; everything
anchored here must survive the fix byte-identical (design Property 5, D8).

Anchors (content, not line numbers), observed byte-exact on the unfixed tree:

1. The Greengrass installer invocation
   (``--thing-policy-name GreengrassV2IoTThingPolicy``).
2. The step 3.5 ECR access policy block (comment + ``put-role-policy``
   command + its embedded policy heredoc).
3. The step 3.6 ``aws iam put-role-policy`` command + its
   ``ShadowManagerSyncPolicy`` policy document (D8: byte-identical; only the
   surrounding comment/warning prose may change).
4. The step 4 role-policy verification block.

NOTE: some anchored lines carry trailing whitespace (lines of exactly four
spaces inside the step 4 block) — that whitespace is part of the golden bytes.
Do not let an editor strip it from this file.

**Feature: camera-shadow-sync-provisioning, Property 5: Preservation —
setup_station.sh outside the fix sites, and the golden**

**Validates: Requirements 3.2**

Run:
    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
        test/backend-test/camera_shadow_sync/test_setup_station_preservation.py -v
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_STATION = REPO_ROOT / "station_install" / "setup_station.sh"


def _script_text():
    return SETUP_STATION.read_text()


# Anchor 1 — the installer invocation. This is the line that provisions
# GreengrassV2IoTThingPolicy by name; the Gap 1 fix reads and augments that
# policy but never changes how it is provisioned.
INSTALLER_INVOCATION = r'''    run_cmd "java -Droot=/aws_dda/greengrass/v2 -Dlog.store=FILE $JAVA_CRED_PROPS -jar ./GreengrassInstaller/lib/Greengrass.jar --aws-region ${aws_region} --thing-name ${thing_name} --thing-group-name ${thing_group_name} --thing-policy-name GreengrassV2IoTThingPolicy --tes-role-name GreengrassV2TokenExchangeRole --tes-role-alias-name GreengrassCoreTokenExchangeRoleAlias --component-default-user ggc_user:ggc_group ${gg_init_config_arg} --setup-system-service true --provision true"; then'''


# Anchor 2 — the step 3.5 ECR access policy block (comment through fi),
# including the embedded ECR policy document heredoc.
STEP_3_5_ECR_BLOCK = r'''
    # 3.5 Add ECR access policy (inline policy)
    # Required for Docker-based components (e.g. aws.edgeml.dda.LocalServer) whose
    # artifacts are published to ECR. Without ecr:GetAuthorizationToken the device
    # fails with GET_ECR_CREDENTIAL_ERROR / "Failed to get auth token for docker login".
    # ecr:GetAuthorizationToken does not support resource scoping and must use "*".
    echo "3.5 Adding ECR access policy..."
    if run_cmd "aws iam put-role-policy \
      --role-name GreengrassV2TokenExchangeRole \
      --policy-name ECRComponentAccess \
      --policy-document '{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Sid\": \"AllowEcrAuthToken\",
            \"Effect\": \"Allow\",
            \"Action\": [
              \"ecr:GetAuthorizationToken\"
            ],
            \"Resource\": \"*\"
          },
          {
            \"Sid\": \"AllowEcrImagePull\",
            \"Effect\": \"Allow\",
            \"Action\": [
              \"ecr:BatchGetImage\",
              \"ecr:GetDownloadUrlForLayer\",
              \"ecr:BatchCheckLayerAvailability\"
            ],
            \"Resource\": \"arn:aws:ecr:*:${aws_account_id}:repository/dda/*\"
          }
        ]
      }'"; then
        echo "   ✓ ECR access policy attached"
    else
        add_warning "Could not attach ECR access policy. Device may not be able to pull Docker-based components from ECR."
    fi
'''[1:]


# Anchor 3 — the step 3.6 put-role-policy command + its ShadowManagerSyncPolicy
# policy document. Per design decision D8 this must stay byte-identical; only
# the leading comment and the failure add_warning text (NOT anchored here) are
# corrected by the fix.
STEP_3_6_PUT_ROLE_POLICY = r'''
    if run_cmd "aws iam put-role-policy \
      --role-name GreengrassV2TokenExchangeRole \
      --policy-name ShadowManagerSyncPolicy \
      --policy-document '{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Sid\": \"ShadowManagerCloudSync\",
            \"Effect\": \"Allow\",
            \"Action\": [
              \"iot:GetThingShadow\",
              \"iot:UpdateThingShadow\",
              \"iot:DeleteThingShadow\",
              \"iot:ListNamedShadowsForThing\"
            ],
            \"Resource\": \"arn:aws:iot:*:${aws_account_id}:thing/*\"
          }
        ]
      }'"; then
'''[1:]


# Anchor 4 — the step 4 verification block (list attached/inline role
# policies and report). Three lines inside this block are exactly four
# spaces — golden trailing whitespace.
STEP_4_VERIFICATION_BLOCK = r'''
    # 4. Verify all policies are attached
    echo "4. Verifying role policies..."
    ATTACHED_POLICIES=$(aws iam list-attached-role-policies --role-name GreengrassV2TokenExchangeRole --query 'AttachedPolicies[].PolicyName' --output text 2>/dev/null)
    INLINE_POLICIES=$(aws iam list-role-policies --role-name GreengrassV2TokenExchangeRole --query 'PolicyNames' --output text 2>/dev/null)
    
    echo "   Attached managed policies:"
    if [ -n "$ATTACHED_POLICIES" ]; then
        echo "$ATTACHED_POLICIES" | tr ' ' '\n' | sed 's/^/     - /'
    else
        echo "     (none)"
    fi
    
    echo "   Inline policies:"
    if [ -n "$INLINE_POLICIES" ]; then
        echo "$INLINE_POLICIES" | tr ' ' '\n' | sed 's/^/     - /'
    else
        echo "     (none)"
    fi
    echo ""
    
    echo "✓ GreengrassV2TokenExchangeRole updated successfully"
'''[1:]


# **Feature: camera-shadow-sync-provisioning, Property 5: Preservation —
# setup_station.sh outside the fix sites, and the golden**
# Validates: Requirements 3.2
def test_installer_invocation_byte_identical():
    """The Greengrass installer invocation (--thing-policy-name
    GreengrassV2IoTThingPolicy) survives the fix byte-identical."""
    script = _script_text()
    assert script.count(INSTALLER_INVOCATION) == 1, (
        "the Greengrass installer invocation (--thing-policy-name "
        "GreengrassV2IoTThingPolicy) is no longer byte-identical to the "
        "pre-fix golden — the fix must not touch how the thing policy is "
        "provisioned"
    )


# **Feature: camera-shadow-sync-provisioning, Property 5: Preservation —
# setup_station.sh outside the fix sites, and the golden**
# Validates: Requirements 3.2
def test_step_3_5_ecr_policy_block_byte_identical():
    """The step 3.5 ECR access policy block (comment + put-role-policy +
    embedded ECR policy heredoc) survives the fix byte-identical."""
    script = _script_text()
    assert script.count(STEP_3_5_ECR_BLOCK) == 1, (
        "the step 3.5 ECR access policy block is no longer byte-identical to "
        "the pre-fix golden — the fix touches only the thing-policy ensure "
        "block and the step 3.6 comment/warning prose"
    )


# **Feature: camera-shadow-sync-provisioning, Property 5: Preservation —
# setup_station.sh outside the fix sites, and the golden**
# Validates: Requirements 3.2
def test_step_3_6_put_role_policy_command_and_document_byte_identical():
    """The step 3.6 `aws iam put-role-policy` command and its
    ShadowManagerSyncPolicy policy document survive the fix byte-identical
    (design D8: only the comment/warning prose around them may change)."""
    script = _script_text()
    assert script.count(STEP_3_6_PUT_ROLE_POLICY) == 1, (
        "the step 3.6 put-role-policy command + ShadowManagerSyncPolicy "
        "policy document is no longer byte-identical to the pre-fix golden — "
        "design D8 requires the command and document untouched (only the "
        "surrounding comment/warning prose is corrected)"
    )


# **Feature: camera-shadow-sync-provisioning, Property 5: Preservation —
# setup_station.sh outside the fix sites, and the golden**
# Validates: Requirements 3.2
def test_step_4_verification_block_byte_identical():
    """The step 4 role-policy verification block survives the fix
    byte-identical (including its trailing-whitespace lines)."""
    script = _script_text()
    assert script.count(STEP_4_VERIFICATION_BLOCK) == 1, (
        "the step 4 verification block is no longer byte-identical to the "
        "pre-fix golden — the fix must not touch role-policy verification"
    )
