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
#      trust fallback), so a value must be supplied. It is resolved like
#      deploy-portal.sh does (../scripts/portal-trusted-accounts.sh): CDK context >
#      TRUSTED_USECASE_ACCOUNT_IDS > SSM /dda-portal/trusted-usecase-account-ids >
#      the list the deployed portal stacks grant. Only a fresh install (none of
#      these, no EdgeCVPortalComputeStack) uses the deploying account.
#
# Portal_Identity enforcement is kept at its deployed value unless
# PORTAL_REGISTRY_ENFORCED=true|false says otherwise (see
# ../scripts/portal-registry-enforcement.sh); without it the CDK default
# would turn enforcement off for every stack deployed here.
#
# Usage:
#   ./deploy_portal_fixes.sh
#   TRUSTED_USECASE_ACCOUNT_IDS="111111111111,222222222222" ./deploy_portal_fixes.sh
#   CDK_STACKS="EdgeCVPortalComputeStack" ./deploy_portal_fixes.sh   # subset
#   PORTAL_REGISTRY_ENFORCED=true ./deploy_portal_fixes.sh           # turn on
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

# Trusted UseCase account IDs for the portal's cross-account role assumption
# (see the header): never the deploying account on an existing portal.
. "$SCRIPT_DIR/../scripts/portal-trusted-accounts.sh"
if ! TRUSTED_RESOLVED=$(portal_trusted_accounts_resolve "$REGION"); then
  exit 1
fi
if [ "$TRUSTED_RESOLVED" = "none" ]; then
  TRUSTED_SOURCE="fresh-default"
  TRUSTED="$ACCOUNT"
else
  TRUSTED_SOURCE="${TRUSTED_RESOLVED%% *}"
  TRUSTED="${TRUSTED_RESOLVED#* }"
fi
echo "Trusted UseCase accounts: $TRUSTED ($(portal_trusted_accounts_label "$TRUSTED_SOURCE"))"
if [ "$TRUSTED_SOURCE" != "deployed" ] && [ "$TRUSTED_SOURCE" != "fresh-default" ]; then
  portal_trusted_accounts_compare "$REGION" "$TRUSTED"
fi

# Which stacks to deploy (default: all).
CDK_STACKS="${CDK_STACKS:---all}"

# Optional data-bucket allowlist (comma-separated bucket names or ARNs) for the
# portal Lambda roles' S3 data plane. Read only under the 'tag-objects-only' and
# 'allowlist' data-plane scopings (dataPlaneGrant in lib/context-helpers.ts);
# the deployed 'tag' scoping conditions the data plane on the bucket's
# dda-portal:managed tag and ignores it.
DATA_BUCKET_ALLOWLIST="${DATA_BUCKET_ALLOWLIST:-}"

# Portal_Identity enforcement: keep the deployed value unless asked otherwise.
. "$SCRIPT_DIR/../scripts/portal-registry-enforcement.sh"
if ! ENFORCED=$(portal_registry_enforced_for_deploy "$REGION"); then
  exit 1
fi
# bin/app.ts refuses to synthesize without a recognized portalRegistryEnforced
# unless allowLegacyRoles=true acknowledges a fresh install (no portal handler
# deployed yet); the effective value and its provenance go to the deploy log
# (portal-authorization-and-iam-scoping, D7-A).
if [ -n "$ENFORCED" ]; then
  ENFORCED_ARGS=(-c "portalRegistryEnforced=$ENFORCED")
  if [ -n "${PORTAL_REGISTRY_ENFORCED:-}" ]; then
    ENFORCEMENT_PROVENANCE="Portal_Identity enforcement: $ENFORCED (requested)"
  else
    ENFORCEMENT_PROVENANCE="Portal_Identity enforcement: $ENFORCED (kept from deployed)"
  fi
else
  ENFORCED_ARGS=(-c "allowLegacyRoles=true")
  ENFORCEMENT_PROVENANCE="Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)"
fi

# The deploy itself needs no build (cdk.json runs bin/app.ts through ts-node);
# the build writes lib/context-helpers.js, from which the data-plane scoping
# this deploy gives every portal handler (DATA_PLANE_SCOPING;
# portal-authorization-and-iam-scoping, design I5) is read for the log.
npm run build || exit 1

echo "=== $(date -u '+%FT%TZ') portal deploy start ==="
echo "  account=$ACCOUNT region=$REGION"
echo "  trustedUseCaseAccountIds=$TRUSTED"
echo "  dataBucketAllowlist=${DATA_BUCKET_ALLOWLIST:-<unset>}"
echo "  portalRegistryEnforced=${ENFORCED:-<CDK default (off)>}"
echo "  stacks=$CDK_STACKS"
echo "$ENFORCEMENT_PROVENANCE"
echo "Data-plane scoping: $(node -e "console.log(require('./lib/context-helpers').DATA_PLANE_SCOPING)")"

npx cdk deploy $CDK_STACKS \
  --require-approval never \
  -c "trustedUseCaseAccountIds=$TRUSTED" \
  -c "dataBucketAllowlist=$DATA_BUCKET_ALLOWLIST" \
  ${ENFORCED_ARGS[@]+"${ENFORCED_ARGS[@]}"}
rc=$?
echo "=== $(date -u '+%FT%TZ') portal deploy END exit=$rc ==="
exit $rc
