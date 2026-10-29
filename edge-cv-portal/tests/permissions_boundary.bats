#!/usr/bin/env bats
#
# scripts/portal-permissions-boundary.sh and the four scripts that deploy the
# portal app: in an account bootstrapped with the DDA permissions boundary
# (iam/setup-account.sh), every cdk deploy passes -c ddaPermissionsBoundary=
# <name>, so bin/app.ts puts the boundary on every role; without one nothing
# is passed. The stub aws answers the CDKToolkit lookup with
# STUB_CDKTOOLKIT_BOUNDARY (None when unset).

bats_require_minimum_version 1.5.0

load test_helper

HELPER_REL="scripts/portal-permissions-boundary.sh"
BOUNDARY=dda-portal-permissions-boundary

setup() {
  portal_harness_setup
  unset DDA_PERMISSIONS_BOUNDARY STUB_CDKTOOLKIT_BOUNDARY PORTAL_REGISTRY_ENFORCED
  _install_stub "$PORTAL_BIN/npm"
}
teardown() { portal_harness_teardown; }

resolve() {
  bash -c 'set -euo pipefail; . "$1"; portal_permissions_boundary "${2:-}"' _ \
    "$PORTAL_REAL_DIR/$HELPER_REL" "${1:-}"
}

install_real() {
  local rel="$1"
  mkdir -p "$(dirname "$PORTAL_TREE/$rel")"
  rm -f "$PORTAL_TREE/$rel"
  cp "$PORTAL_REAL_DIR/$rel" "$PORTAL_TREE/$rel"
  chmod +x "$PORTAL_TREE/$rel"
}

npx_args() { stub_args_of npx; }

# --- the helper --------------------------------------------------------------

@test "the bootstrap's boundary is used" {
  export STUB_CDKTOOLKIT_BOUNDARY="$BOUNDARY"
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "$BOUNDARY" ]
  [[ "$stderr" == *"Permissions boundary: $BOUNDARY (from the CDK bootstrap)"* ]]
  [ "$(stub_call_count_matching aws '^cloudformation describe-stacks --stack-name CDKToolkit --region us-east-1 ')" -eq 1 ]
}

@test "a bootstrap without a boundary gives none" {
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [[ "$stderr" == *"none (the CDK bootstrap has none)"* ]]
}

@test "DDA_PERMISSIONS_BOUNDARY wins, and =none gives none" {
  export STUB_CDKTOOLKIT_BOUNDARY="$BOUNDARY"
  DDA_PERMISSIONS_BOUNDARY=other-boundary run --separate-stderr resolve us-east-1
  [ "$output" = "other-boundary" ]
  DDA_PERMISSIONS_BOUNDARY=none run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  ! stub_called aws
}

@test "an account that is not bootstrapped has no boundary" {
  missing_stacks CDKToolkit
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "a CDKToolkit lookup that fails otherwise refuses to guess" {
  deny_describe_stacks CDKToolkit
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 1 ]
  [[ "$stderr" == *"Could not read the CDKToolkit stack"* ]]
}

# --- the deploy scripts --------------------------------------------------------

@test "deploy-infrastructure.sh passes the bootstrap's boundary to cdk deploy" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-mcp-stack.sh
  install_real scripts/portal-registry-enforcement.sh
  set_deployed_enforcement true
  export STUB_CDKTOOLKIT_BOUNDARY="$BOUNDARY"
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [[ "$output" == *"ddaPermissionsBoundary=$BOUNDARY"* ]]
}

@test "deploy-infrastructure.sh passes none without a bootstrap boundary" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-mcp-stack.sh
  install_real scripts/portal-registry-enforcement.sh
  set_deployed_enforcement true
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  stub_called npx
  run npx_args
  [[ "$output" != *"ddaPermissionsBoundary"* ]]
}

@test "deploy-infrastructure.sh stops before cdk deploy when the bootstrap cannot be read" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-mcp-stack.sh
  install_real scripts/portal-registry-enforcement.sh
  set_deployed_enforcement true
  deny_describe_stacks CDKToolkit
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -ne 0 ]
  [[ "$output" == *"Could not read the CDKToolkit stack"* ]]
  ! stub_called npx
}

@test "deploy-frontend.sh's compute redeploy passes the boundary" {
  install_real deploy-frontend.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real scripts/portal-trusted-accounts.sh
  set_cfn_output EdgeCVPortalFrontendStack FrontendBucketName portal-frontend-bucket
  set_cfn_output EdgeCVPortalFrontendStack DistributionId EDISTRIBUTION1
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  set_cfn_output EdgeCVPortalAuthStack AuthConfig \
    '{"userPoolId":"us-east-1_pool","userPoolWebClientId":"client1","region":"us-east-1"}'
  set_cfn_output EdgeCVPortalComputeStack ApiUrl https://api.example.invalid/v1/
  set_region ""
  set_deployed_enforcement true
  export STUB_CDKTOOLKIT_BOUNDARY="$BOUNDARY"
  run bash -c 'cd "$PORTAL_TREE" && AWS_REGION=us-east-1 TRUSTED_USECASE_ACCOUNT_IDS=123456789012 ./deploy-frontend.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [ "${lines[2]}" = "EdgeCVPortalComputeStack" ]
  [[ "$output" == *"ddaPermissionsBoundary=$BOUNDARY"* ]]
}

@test "deploy-auth.sh passes the boundary to its deploy" {
  install_real infrastructure/deploy-auth.sh
  install_real scripts/portal-registry-enforcement.sh
  set_deployed_enforcement true
  export STUB_CDKTOOLKIT_BOUNDARY="$BOUNDARY"
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh --region us-east-1'
  [ "$status" -eq 0 ]
  [ "$(stub_call_count_matching npx "^cdk deploy EdgeCVPortalAuthStack .*ddaPermissionsBoundary=$BOUNDARY")" -eq 1 ]
}

@test "deploy_portal_fixes.sh passes the boundary" {
  install_real infrastructure/deploy_portal_fixes.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real scripts/portal-trusted-accounts.sh
  set_region ""
  set_deployed_enforcement true
  export STUB_CDKTOOLKIT_BOUNDARY="$BOUNDARY"
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && TRUSTED_USECASE_ACCOUNT_IDS=123456789012 ./deploy_portal_fixes.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [[ "$output" == *"ddaPermissionsBoundary=$BOUNDARY"* ]]
}
