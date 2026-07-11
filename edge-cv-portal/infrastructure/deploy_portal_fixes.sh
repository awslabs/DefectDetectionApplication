#!/bin/bash
# Deploy the Edge CV Portal CDK stacks (Auth / Storage / Compute / Frontend).
#
# Handles two things that a bare `cdk deploy` trips over in this repo:
#   1. Credentials: CDK's JS SDK does not read the SSO / login-helper credential
#      cache that the AWS CLI resolves, so this exports the resolved session into
#      standard env vars first (drops AWS_CREDENTIAL_EXPIRATION so botocore treats
#      them as static and does not try to "refresh" them into an expired state).
#   2. trustedUseCaseAccountIds: the ComputeStack constructor REJECTS an empty
#      list at synth time (the IAM least-privilege fix removed the wildcard-account
#      trust fallback), so a value must be supplied. Defaults to the deploying
#      account (single-account setup); override for cross-account.
#
# Usage:
#   ./deploy_portal_fixes.sh
#   TRUSTED_USECASE_ACCOUNT_IDS="111111111111,222222222222" ./deploy_portal_fixes.sh
#   CDK_STACKS="EdgeCVPortalComputeStack" ./deploy_portal_fixes.sh   # subset
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Resolve + export credentials for CDK's JS SDK.
if _CREDS_ENV=$(aws configure export-credentials --format env 2>/dev/null | grep -v AWS_CREDENTIAL_EXPIRATION); then
  eval "$_CREDS_ENV"
  unset _CREDS_ENV
fi

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}"
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"
if [ -z "$ACCOUNT" ] || [ "$ACCOUNT" = "None" ]; then
  echo "ERROR: could not resolve AWS account. Check credentials (aws sts get-caller-identity)." >&2
  exit 1
fi
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"

# Trusted UseCase account IDs for the portal's cross-account role assumption.
# Default to the deploying account (single-account setup).
TRUSTED="${TRUSTED_USECASE_ACCOUNT_IDS:-$ACCOUNT}"

# Which stacks to deploy (default: all).
CDK_STACKS="${CDK_STACKS:---all}"

echo "=== $(date -u '+%FT%TZ') portal deploy start ==="
echo "  account=$ACCOUNT region=$REGION"
echo "  trustedUseCaseAccountIds=$TRUSTED"
echo "  stacks=$CDK_STACKS"

npx cdk deploy $CDK_STACKS \
  --require-approval never \
  -c "trustedUseCaseAccountIds=$TRUSTED"
rc=$?
echo "=== $(date -u '+%FT%TZ') portal deploy END exit=$rc ==="
exit $rc
