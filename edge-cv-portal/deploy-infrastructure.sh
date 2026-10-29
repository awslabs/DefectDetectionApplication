#!/bin/bash

# Deploy Edge CV Portal Infrastructure
# This script deploys the CDK infrastructure with the fixed shared_utils.py

set -e

# This script's directory (edge-cv-portal/), captured before the cd below.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Get AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
# Region: an exported AWS_REGION / AWS_DEFAULT_REGION wins (the AWS CLI's own
# order; deploy-portal.sh exports the region it resolved), else the profile's
# configured region. `aws configure get region` reads only the config file and
# exits 1 when none is set, hence `|| true` (a failing substitution would end
# the script here under set -e). Exported so cdk deploy, describe-stacks and
# the enforcement lookup below all use this region.
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
if [ -z "$AWS_REGION" ]; then
    echo "❌ ERROR: No AWS region set."
    echo "   Run: export AWS_REGION=us-east-1"
    echo "   Or:  aws configure set region us-east-1"
    exit 1
fi
export AWS_REGION

echo "🚀 Starting Edge CV Portal Infrastructure Deployment..."
echo "📍 AWS Account: $AWS_ACCOUNT_ID | Region: $AWS_REGION"
echo ""

# Change to infrastructure directory
cd infrastructure

echo "📦 Installing dependencies..."
npm ci

echo "🔨 Building TypeScript..."
npm run build

# The S3 data-plane scoping this build gives every portal handler
# (DATA_PLANE_SCOPING; portal-authorization-and-iam-scoping, design I5), read
# from the built lib/context-helpers.js, the source the CDK synthesizes from.
echo "Data-plane scoping: $(node -e "console.log(require('./lib/context-helpers').DATA_PLANE_SCOPING)")"

echo "🧹 Clearing CDK cache to force layer update..."
rm -rf cdk.out

echo "🚀 Deploying CDK stacks with forced updates..."

# If frontend stack already exists, pass CloudFront domain for auto-CORS
CLOUDFRONT_URL=$(aws cloudformation describe-stacks \
  --stack-name EdgeCVPortalFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text 2>/dev/null || echo "")

CDK_CONTEXT_ARGS=""
if [ -n "$CLOUDFRONT_URL" ] && [ "$CLOUDFRONT_URL" != "None" ]; then
  echo "📡 Found existing CloudFront domain: $CLOUDFRONT_URL"
  CDK_CONTEXT_ARGS="-c cloudFrontDomain=$CLOUDFRONT_URL"
fi

# Portal_Identity registry enforcement (portal-jwt-role-privilege-escalation
# Req 2.4). With enforcement on and a registry row missing, the portal denies
# that principal everything, including the bootstrap `admin`, so turn it on
# only AFTER backfill_portal_registry.py has been applied and the accounts
# verified:
#
#   PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh    # turn on
#   PORTAL_REGISTRY_ENFORCED=false ./deploy-infrastructure.sh   # turn off
#
# Unset keeps the value the deployed handlers carry (on if any is on), so a
# routine deploy never changes it; a fresh install gets the CDK default, off.
# See scripts/portal-registry-enforcement.sh.
. "$SCRIPT_DIR/scripts/portal-registry-enforcement.sh"
if ! ENFORCED=$(portal_registry_enforced_for_deploy "$AWS_REGION"); then
  exit 1
fi
# bin/app.ts refuses to synthesize without a recognized portalRegistryEnforced
# unless allowLegacyRoles=true acknowledges a fresh install (no portal handler
# deployed yet, so the CDK default, off, applies); the effective value and its
# provenance go to the deploy log (portal-authorization-and-iam-scoping, D7-A).
if [ -n "$ENFORCED" ]; then
  CDK_CONTEXT_ARGS="$CDK_CONTEXT_ARGS -c portalRegistryEnforced=$ENFORCED"
  if [ -n "${PORTAL_REGISTRY_ENFORCED:-}" ]; then
    echo "Portal_Identity enforcement: $ENFORCED (requested)"
  else
    echo "Portal_Identity enforcement: $ENFORCED (kept from deployed)"
  fi
else
  CDK_CONTEXT_ARGS="$CDK_CONTEXT_ARGS -c allowLegacyRoles=true"
  echo "Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)"
fi
# Production posture (ADMIN_GUIDE "The enforcement flag"): a deploy that leaves
# enforcement off must never be silent. The resolved value is passed through
# as requested (1/true/yes/on/enabled all read as on, like the Lambda layer),
# so normalise before testing; empty means "no portal handler deployed yet,
# CDK default (off) applies", which is also not on.
ENFORCED_ON=false
_portal_registry_value_is_on "$ENFORCED" && ENFORCED_ON=true
if [ "$ENFORCED_ON" != "true" ]; then
  echo "⚠️  Portal_Identity enforcement is OFF for this deploy. Production deployments must run PORTAL_REGISTRY_ENFORCED=true ./deploy-infrastructure.sh after backfill_portal_registry.py has been run." >&2
fi

# Shared Cognito authorizer for the stacks that register routes without an
# authorizer of their own (EdgeCVPortalWorkflowDebugStack,
# EdgeCVPortalTelemetryStack). API Gateway allows 10 authorizers per REST API,
# a quota only AWS Support raises, and the portal API already carries one per
# route-registering stack, so the first deploy of a stack that created its
# own rolled back on 2026-10-18. Those stacks attach their methods to the
# ApiGatewayStack's authorizer (name EdgeCVPortalAuthorizer); bin/app.ts
# instantiates them only when its id is present as CDK context
# (lib/context-helpers.ts sharedAuthorizerIdIfGiven). Resolve it from the
# deployed API and pass it as CDK context:
#
#   SHARED_AUTHORIZER_ID=<id> ./deploy-infrastructure.sh   # skip the lookup
#
# When nothing resolves (a fresh install has no API yet, or the lookup is
# denied) no value is passed; the CDK app then warns, skips those two stacks
# and deploys every other stack as before. The script then looks the id up
# again after that deploy: on a fresh install the deploy has just created the
# API, so the lookup succeeds and a second `cdk deploy` adds the two stacks
# (see the end of this script). The lookup itself never stops the deploy:
# both reads run under `|| true`.
resolve_shared_authorizer_id() {
  local api_id
  if [ -n "${SHARED_AUTHORIZER_ID:-}" ]; then
    printf '%s\n' "$SHARED_AUTHORIZER_ID"
    return 0
  fi
  api_id=$(aws cloudformation describe-stacks --stack-name EdgeCVPortalComputeStack \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiId`].OutputValue' --output text 2>/dev/null || true)
  if [ -z "$api_id" ] || [ "$api_id" = "None" ]; then
    return 0
  fi
  # Only a Cognito authorizer of that name qualifies: the methods are deployed
  # with AuthorizationType COGNITO_USER_POOLS.
  aws apigateway get-authorizers --rest-api-id "$api_id" \
    --query "items[?name=='EdgeCVPortalAuthorizer' && type=='COGNITO_USER_POOLS'].id" --output text 2>/dev/null \
    | awk 'NF { print $1; exit }' || true
}
shared_authorizer_found() { [ -n "$SHARED_AUTHORIZER" ] && [ "$SHARED_AUTHORIZER" != "None" ]; }
SHARED_AUTHORIZER=$(resolve_shared_authorizer_id || true)
if shared_authorizer_found; then
  echo "🔑 Shared API authorizer (EdgeCVPortalAuthorizer): $SHARED_AUTHORIZER"
  CDK_CONTEXT_ARGS="$CDK_CONTEXT_ARGS -c sharedAuthorizerId=$SHARED_AUTHORIZER"
else
  echo "ℹ️  No EdgeCVPortalAuthorizer found on the deployed portal API yet. The workflow debug and telemetry stacks need its id, so this deploy leaves them out; the script looks the id up again after it and then deploys those two stacks (on a fresh install, this deploy creates the API)."
fi

# Trusted UseCase account list, for visibility only. bin/app.ts synthesizes the
# portal stacks with the first of CDK context trustedUseCaseAccountIds,
# TRUSTED_USECASE_ACCOUNT_IDS and SSM /dda-portal/trusted-usecase-account-ids;
# this script passes no list of its own. Print that list and how it differs
# from what the deployed stacks grant (scripts/portal-trusted-accounts.sh), so
# a deploy that drops or adds an account is never silent. It never changes the
# list (no fallback to the deployed list) and never stops the deploy: it is
# called with `|| true`, so errexit is off inside it. Under deploy-portal.sh,
# which compared this list before its first step and exports
# PORTAL_TRUSTED_ACCOUNTS_COMPARED, the comparison is not printed again.
show_trusted_accounts() {
  local helper="$SCRIPT_DIR/scripts/portal-trusted-accounts.sh" err="" resolved kind ids reason=""
  if [ ! -f "$helper" ]; then
    echo "Trusted UseCase accounts: not compared (scripts/portal-trusted-accounts.sh is missing)"
    return 0
  fi
  # shellcheck source=scripts/portal-trusted-accounts.sh
  . "$helper"
  err="$(mktemp "${TMPDIR:-/tmp}/portal-trusted.XXXXXX" 2>/dev/null)" || err=""
  if ! resolved="$(portal_trusted_accounts_resolve "$AWS_REGION" 2>"${err:-/dev/null}")"; then
    [ -z "$err" ] || reason="$(sed -n '1{s/^❌ *//;p;}' "$err")"
    [ -z "$err" ] || rm -f "$err"
    echo "⚠️  Trusted UseCase accounts: not compared with the deployed stacks: ${reason:-the list could not be read}"
    return 0
  fi
  [ -z "$err" ] || rm -f "$err"
  case "$resolved" in
    context:*|"env "*|"ssm "*) ;;
    *)
      # "deployed" or "none": nothing is configured, so the CDK app has no list.
      echo "Trusted UseCase accounts: none configured (CDK context trustedUseCaseAccountIds, TRUSTED_USECASE_ACCOUNT_IDS or SSM $PORTAL_TRUSTED_SSM_PARAMETER); the CDK app needs one"
      return 0
      ;;
  esac
  kind="${resolved%% *}"
  ids="${resolved#* }"
  echo "Trusted UseCase accounts: $ids ($(portal_trusted_accounts_label "$kind"))"
  if [ "${PORTAL_TRUSTED_ACCOUNTS_COMPARED:-}" = "$ids" ]; then
    echo "  compared with the deployed stacks by deploy-portal.sh before its first step"
  else
    portal_trusted_accounts_compare "$AWS_REGION" "$ids"
  fi
  return 0
}
show_trusted_accounts || true

# Portal_MCP_Server opt-out (mcp-servers design D33): DEPLOY_MCP_SERVER=false
# omits EdgeCVPortalMcpStack, refused while that stack is still deployed
# (remove it first with `npx cdk destroy EdgeCVPortalMcpStack --force` and
# the CDK context resolved above; the guard prints that command).
# See scripts/portal-mcp-stack.sh.
. "$SCRIPT_DIR/scripts/portal-mcp-stack.sh"
if ! portal_mcp_optout_guard "$AWS_REGION" "${DEPLOY_MCP_SERVER:-}"; then
  exit 1
fi

# The project-local CLI (devDependency aws-cdk, installed by `npm ci` above):
# aws-cdk-lib emits a cloud-assembly schema that an older global `cdk` on
# PATH cannot read ("Cloud assembly schema version mismatch").
npx cdk deploy --all --require-approval never --force $CDK_CONTEXT_ARGS

# Second pass for the two stacks that need the shared authorizer id. When the
# lookup above found none, the deploy just run left them out; on a fresh
# install it has now created the API, so look the id up again. Naming the two
# stacks without --exclusively lets the CLI also deploy the stacks they import
# values from, should a change ever give those stacks an export that only the
# two use; without --force, it skips the ones whose template did not change.
if ! shared_authorizer_found; then
  SHARED_AUTHORIZER=$(resolve_shared_authorizer_id || true)
  if shared_authorizer_found; then
    echo "🔑 Shared API authorizer (EdgeCVPortalAuthorizer): $SHARED_AUTHORIZER"
    echo "🚀 Deploying the workflow debug and telemetry stacks..."
    # shellcheck disable=SC2086 # CDK_CONTEXT_ARGS is a word list, as in the first pass
    npx cdk deploy EdgeCVPortalWorkflowDebugStack EdgeCVPortalTelemetryStack \
      --require-approval never $CDK_CONTEXT_ARGS -c "sharedAuthorizerId=$SHARED_AUTHORIZER"
  else
    echo "⚠️  No EdgeCVPortalAuthorizer found on the deployed portal API after this deploy either (is EdgeCVPortalComputeStack deployed, and may these credentials read its authorizers with apigateway:GET?). The workflow debug and telemetry stacks were not deployed: fix the lookup and run this script again, or set SHARED_AUTHORIZER_ID=<id> to pass the id by hand." >&2
  fi
fi

echo "✅ Deployment completed successfully!"
echo ""
echo "📝 Next steps:"
echo "1. Test the API endpoints to ensure 502 errors are resolved"
echo "2. Check that use cases appear in the dropdown"
echo "3. Verify RBAC functionality is working"
echo ""
echo "🔍 To test the use cases API:"
echo "curl -H \"Authorization: Bearer YOUR_TOKEN\" https://xg0yibkeh2.execute-api.us-east-1.amazonaws.com/v1/usecases"