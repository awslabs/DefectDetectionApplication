#!/usr/bin/env bats
#
# Portal_Identity enforcement across portal deploys
# (scripts/portal-registry-enforcement.sh).
#
# The CDK resolves `portalRegistryEnforced` default-OFF, so a deploy that
# omitted PORTAL_REGISTRY_ENFORCED used to switch enforcement off for every
# stack it touched (production, 2026-09-26). An unset value must now keep
# what the deployed handlers carry, an explicit value must win, a fresh
# install must keep the CDK default, and an unreadable deployed value must
# stop the deploy instead of guessing.

bats_require_minimum_version 1.5.0

load test_helper

HELPER_REL="scripts/portal-registry-enforcement.sh"

setup() {
  portal_harness_setup
  unset PORTAL_REGISTRY_ENFORCED
  # The deploy scripts run npm ci / npm run build; stub npm like the other tools.
  _install_stub "$PORTAL_BIN/npm"
}
teardown() { portal_harness_teardown; }

# resolve [region] -> run the helper exactly as the deploy scripts source it.
resolve() {
  bash -c '. "$1"; portal_registry_enforced_for_deploy "${2:-}"' _ \
    "$PORTAL_REAL_DIR/$HELPER_REL" "${1:-}"
}

# install_real <path relative to edge-cv-portal/>
#   Copy a real script into the fake tree. The harness seeds some of these
#   paths as symlinks to helpers/stub.sh, so remove the path first rather
#   than copy through the link onto the stub itself.
install_real() {
  local rel="$1"
  mkdir -p "$(dirname "$PORTAL_TREE/$rel")"
  rm -f "$PORTAL_TREE/$rel"
  cp "$PORTAL_REAL_DIR/$rel" "$PORTAL_TREE/$rel"
  chmod +x "$PORTAL_TREE/$rel"
}

# npx_args -> the arguments of the (single) `npx cdk deploy` call, one per line.
npx_args() { stub_args_of npx; }

# ---------------------------------------------------------------------------
# The resolution rules
# ---------------------------------------------------------------------------

@test "unset keeps a deployed 'true'" {
  set_deployed_enforcement true true true
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
  [[ "$stderr" == *"keeping the deployed value 'true' (3 handler(s) on, 0 off)"* ]]
}

@test "unset resolves a mixed deployed state to on" {
  set_deployed_enforcement false true false
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
  [[ "$stderr" == *"(1 handler(s) on, 2 off)"* ]]
}

@test "unset keeps a deployed 'false'" {
  set_deployed_enforcement false false
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}

@test "unset on a fresh install passes nothing, leaving the CDK default" {
  set_deployed_enforcement
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [[ "$stderr" == *"no portal handler is deployed yet"* ]]
}

@test "an explicit value wins and says what it changes" {
  set_deployed_enforcement true true
  PORTAL_REGISTRY_ENFORCED=false run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
  [[ "$stderr" == *"changes enforcement from 'true' (deployed now) to 'false'"* ]]
}

@test "an explicit value is passed through unchanged" {
  set_deployed_enforcement true
  PORTAL_REGISTRY_ENFORCED=yes run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "yes" ]
  [[ "$stderr" != *"changes enforcement"* ]]
}

@test "an empty value counts as unset" {
  set_deployed_enforcement true
  PORTAL_REGISTRY_ENFORCED= run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
}

@test "a failed lookup refuses to guess" {
  fail_lambda_list
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"Set PORTAL_REGISTRY_ENFORCED=true or PORTAL_REGISTRY_ENFORCED=false explicitly"* ]]
}

@test "a failed lookup does not block an explicit value" {
  fail_lambda_list
  PORTAL_REGISTRY_ENFORCED=true run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
}

@test "the lookup reads the EdgeCVPortal handlers in the given region" {
  set_deployed_enforcement true
  run resolve eu-west-1
  run stub_args_of aws
  [ "${lines[0]}" = "lambda" ]
  [ "${lines[1]}" = "list-functions" ]
  [ "${lines[2]}" = "--region" ]
  [ "${lines[3]}" = "eu-west-1" ]
  [[ "$output" == *"starts_with(FunctionName, 'EdgeCVPortal')"* ]]
  [[ "$output" == *"Environment.Variables.PORTAL_REGISTRY_ENFORCED"* ]]
}

@test "the helper is safe under set -euo pipefail with no region" {
  set_deployed_enforcement
  run bash -c 'set -euo pipefail; . "$1"; portal_registry_enforced_for_deploy; echo done' _ \
    "$PORTAL_REAL_DIR/$HELPER_REL"
  [ "$status" -eq 0 ]
  [[ "$output" == *"done"* ]]
}

# ---------------------------------------------------------------------------
# The deploy scripts pass the resolved value to cdk deploy
# ---------------------------------------------------------------------------

@test "deploy-infrastructure.sh keeps the deployed value when the variable is unset" {
  install_real deploy-infrastructure.sh
  install_real "$HELPER_REL"
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  set_deployed_enforcement true true

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]

  run npx_args
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [[ "$output" == *"portalRegistryEnforced=true"* ]]
  [[ "$output" == *"cloudFrontDomain=d1.cloudfront.net"* ]]
}

@test "deploy-infrastructure.sh passes an explicit PORTAL_REGISTRY_ENFORCED=false" {
  install_real deploy-infrastructure.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE" && PORTAL_REGISTRY_ENFORCED=false ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [[ "$output" == *"portalRegistryEnforced=false"* ]]
  [[ "$output" != *"portalRegistryEnforced=true"* ]]
}

@test "deploy-infrastructure.sh on a fresh install passes no enforcement value" {
  install_real deploy-infrastructure.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  stub_called npx
  run npx_args
  [[ "$output" != *"portalRegistryEnforced"* ]]
}

@test "deploy-infrastructure.sh stops before cdk deploy when the lookup fails" {
  install_real deploy-infrastructure.sh
  install_real "$HELPER_REL"
  fail_lambda_list

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -ne 0 ]
  [[ "$output" == *"Could not read the deployed Portal_Identity enforcement value"* ]]
  ! stub_called npx
}

@test "deploy-frontend.sh's ComputeStack redeploy keeps the deployed value" {
  install_real deploy-frontend.sh
  install_real "$HELPER_REL"
  set_cfn_output EdgeCVPortalFrontendStack FrontendBucketName portal-frontend-bucket
  set_cfn_output EdgeCVPortalFrontendStack DistributionId EDISTRIBUTION1
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  set_cfn_output EdgeCVPortalAuthStack AuthConfig \
    '{"userPoolId":"us-east-1_pool","userPoolWebClientId":"client1","region":"us-east-1"}'
  set_cfn_output EdgeCVPortalComputeStack ApiUrl https://api.example.invalid/v1/
  # `aws configure export-credentials` must fail so its output is not eval'd.
  set_region ""
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-frontend.sh'
  [ "$status" -eq 0 ]

  run npx_args
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [ "${lines[2]}" = "EdgeCVPortalComputeStack" ]
  [[ "$output" == *"portalRegistryEnforced=true"* ]]
}

@test "deploy_portal_fixes.sh passes an explicit PORTAL_REGISTRY_ENFORCED" {
  install_real infrastructure/deploy_portal_fixes.sh
  install_real "$HELPER_REL"
  set_region ""
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && PORTAL_REGISTRY_ENFORCED=false ./deploy_portal_fixes.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"portalRegistryEnforced=false"* ]]
  run npx_args
  [[ "$output" == *"portalRegistryEnforced=false"* ]]
}
