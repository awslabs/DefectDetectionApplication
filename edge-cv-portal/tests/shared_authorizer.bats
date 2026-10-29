#!/usr/bin/env bats
#
# The shared API Gateway authorizer id across the infrastructure deploy
# (deploy-infrastructure.sh, CDK context `sharedAuthorizerId`).
#
# API Gateway allows 10 authorizers per REST API and the portal API carries
# one per route-registering stack, so the workflow debug and telemetry stacks
# attach their routes to the existing EdgeCVPortalAuthorizer and bin/app.ts
# instantiates them only when its id is present as CDK context. The deploy
# script resolves that id from the Compute stack's ApiId output and
# `aws apigateway get-authorizers`, an explicit SHARED_AUTHORIZER_ID wins, and
# a failed lookup must not stop the script: the CDK app then skips the two
# stacks with a warning and deploys the rest, and the script looks the id up
# again after that deploy (on a fresh install the deploy creates the API) to
# deploy the two stacks in a second pass. In these tests the stubbed lookup
# answers the same before and after the deploy, so there is one cdk deploy.
bats_require_minimum_version 1.5.0
load test_helper
setup() {
  portal_harness_setup
  unset PORTAL_REGISTRY_ENFORCED SHARED_AUTHORIZER_ID
  # The deploy scripts run npm ci / npm run build; stub npm like the other tools.
  _install_stub "$PORTAL_BIN/npm"
  install_real deploy-infrastructure.sh
  install_real scripts/portal-mcp-stack.sh
  install_real scripts/portal-registry-enforcement.sh
  set_deployed_enforcement true
}
teardown() { portal_harness_teardown; }
# install_real <path relative to edge-cv-portal/>: copy a real script into the
# fake tree, removing the harness's stub symlink first (as registry_enforcement.bats).
install_real() {
  local rel="$1"
  mkdir -p "$(dirname "$PORTAL_TREE/$rel")"
  rm -f "$PORTAL_TREE/$rel"
  cp "$PORTAL_REAL_DIR/$rel" "$PORTAL_TREE/$rel"
  chmod +x "$PORTAL_TREE/$rel"
}
# npx_args -> the arguments of the (single) `npx cdk deploy` call, one per line.
npx_args() { stub_args_of npx; }
@test "deploy-infrastructure.sh passes the authorizer id it resolves from the deployed API" {
  set_cfn_output EdgeCVPortalComputeStack ApiId apiid0
  # `aws apigateway get-authorizers` is the only call the stub answers generically.
  stub_output aws 'auth01' ''
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Shared API authorizer (EdgeCVPortalAuthorizer): auth01"* ]]
  [ "$(stub_call_count_matching aws 'apigateway get-authorizers --rest-api-id apiid0')" = 1 ]
  run npx_args
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [[ "$output" == *"sharedAuthorizerId=auth01"* ]]
}
@test "deploy-infrastructure.sh takes the first id when the lookup prints several" {
  set_cfn_output EdgeCVPortalComputeStack ApiId apiid0
  stub_output aws "$(printf 'auth01\tauth02')" ''
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [[ "$output" == *"sharedAuthorizerId=auth01"* ]]
  [[ "$output" != *"auth02"* ]]
}
@test "SHARED_AUTHORIZER_ID wins and skips the lookup" {
  set_cfn_output EdgeCVPortalComputeStack ApiId apiid0
  stub_output aws 'auth01' ''
  run bash -c 'cd "$PORTAL_TREE" && SHARED_AUTHORIZER_ID=byhand0 ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [ "$(stub_call_count_matching aws 'apigateway get-authorizers')" = 0 ]
  run npx_args
  [[ "$output" == *"sharedAuthorizerId=byhand0"* ]]
  [[ "$output" != *"auth01"* ]]
}
@test "no Compute stack, before or after the deploy, warns, passes no id and still runs cdk deploy" {
  export STUB_CFN_MISSING_STACKS="EdgeCVPortalComputeStack"
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"No EdgeCVPortalAuthorizer found on the deployed portal API"* ]]
  [ "$(stub_call_count_matching aws 'apigateway get-authorizers')" = 0 ]
  stub_called npx
  run npx_args
  [[ "$output" != *"sharedAuthorizerId"* ]]
}
@test "an empty lookup answer warns and passes no id" {
  set_cfn_output EdgeCVPortalComputeStack ApiId apiid0
  stub_output aws '' ''
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"No EdgeCVPortalAuthorizer found"* ]]
  run npx_args
  [[ "$output" != *"sharedAuthorizerId"* ]]
}
