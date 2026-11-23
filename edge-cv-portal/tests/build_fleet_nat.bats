#!/usr/bin/env bats
#
# The Build Fleet NAT gateway setting across portal deploys
# (scripts/portal-build-fleet-nat.sh).
#
# The CDK resolves `buildFleetNatGateway` default-OFF, so a deploy that
# omitted BUILD_FLEET_NAT_GATEWAY would remove a NAT gateway an operator
# turned on. An unset value must keep the deployed state (the Build Fleet
# stack's BuildNatGatewayId output), an explicit value must win, a fresh
# install must keep the CDK default, and an unreadable state must stop the
# deploy instead of guessing.

bats_require_minimum_version 1.5.0

load test_helper

HELPER_REL="scripts/portal-build-fleet-nat.sh"
STACK="EdgeCVPortalBuildFleetStack"

setup() {
  portal_harness_setup
  unset BUILD_FLEET_NAT_GATEWAY
  # The deploy scripts run npm ci / npm run build; stub npm like the other tools.
  _install_stub "$PORTAL_BIN/npm"
}
teardown() { portal_harness_teardown; }

# resolve [region] -> run the helper exactly as the deploy scripts source it.
resolve() {
  bash -c '. "$1"; portal_build_fleet_nat_for_deploy "${2:-}"' _ \
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

# nat_deployed -> the deployed Build Fleet stack has the NAT gateway output.
nat_deployed() { set_cfn_output "$STACK" BuildNatGatewayId nat-test; }

# install_infra -> the real deploy-infrastructure.sh and the helpers it sources
# (the harness copies this helper and the boundary helper into every tree).
install_infra() {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real scripts/portal-mcp-stack.sh
  install_real scripts/portal-assistant-stack.sh
  set_deployed_enforcement true
}

# install_fixes -> the real deploy_portal_fixes.sh and the helpers it sources.
install_fixes() {
  install_real infrastructure/deploy_portal_fixes.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real scripts/portal-trusted-accounts.sh
  set_region ""
  set_deployed_enforcement true
}

# ---------------------------------------------------------------------------
# The resolution rules
# ---------------------------------------------------------------------------

@test "unset keeps a deployed NAT gateway on" {
  nat_deployed
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
  [[ "$stderr" == *"Build Fleet NAT gateway: keeping the deployed value 'true'. Set BUILD_FLEET_NAT_GATEWAY=true or =false to change it."* ]]
}

@test "unset keeps 'false' when the Build Fleet stack has no NAT gateway output" {
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
  [[ "$stderr" == *"keeping the deployed value 'false'"* ]]
}

@test "an empty value counts as unset" {
  nat_deployed
  export BUILD_FLEET_NAT_GATEWAY=
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
  [[ "$stderr" == *"keeping the deployed value 'true'"* ]]
}

@test "unset on a fresh install passes nothing, leaving the CDK default (off)" {
  missing_stacks "$STACK"
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [[ "$stderr" == *"Build Fleet NAT gateway: EdgeCVPortalBuildFleetStack is not deployed yet; the CDK default (off) applies."* ]]
}

@test "a refused lookup (AccessDenied) refuses to guess" {
  deny_describe_stacks "$STACK"
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"Could not read whether the Build Fleet NAT gateway is deployed (aws cloudformation describe-stacks --stack-name EdgeCVPortalBuildFleetStack failed)."* ]]
  [[ "$stderr" == *"Set BUILD_FLEET_NAT_GATEWAY=true or BUILD_FLEET_NAT_GATEWAY=false explicitly and re-run."* ]]
}

@test "a failed lookup without a reason (exit 254) refuses to guess" {
  export STUB_CFN_DESCRIBE_EXIT=254
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"Set BUILD_FLEET_NAT_GATEWAY=true or BUILD_FLEET_NAT_GATEWAY=false explicitly and re-run."* ]]
}

@test "an explicit value resolves to true or false with the CDK's truthy set" {
  local value
  for value in true " TRUE " 1 " yes " on enabled; do
    export BUILD_FLEET_NAT_GATEWAY="$value"
    run --separate-stderr resolve us-east-1
    echo "value=[$value] status=$status output=[$output]"
    [ "$status" -eq 0 ]
    [ "$output" = "true" ]
  done
  for value in false 0 no off disabled maybe "true --all"; do
    export BUILD_FLEET_NAT_GATEWAY="$value"
    run --separate-stderr resolve us-east-1
    echo "value=[$value] status=$status output=[$output]"
    [ "$status" -eq 0 ]
    [ "$output" = "false" ]
  done
}

@test "an explicit value wins and says what it changes" {
  nat_deployed
  export BUILD_FLEET_NAT_GATEWAY=false
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
  [[ "$stderr" == *"Build Fleet NAT gateway requested: BUILD_FLEET_NAT_GATEWAY=false (false)"* ]]
  [[ "$stderr" == *"This changes the Build Fleet NAT gateway from 'true' (deployed now) to 'false'."* ]]
}

@test "a failed lookup does not block an explicit value" {
  deny_describe_stacks "$STACK"
  export BUILD_FLEET_NAT_GATEWAY=true
  run --separate-stderr resolve us-east-1
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
  [[ "$stderr" == *"Build Fleet NAT gateway requested: BUILD_FLEET_NAT_GATEWAY=true (true)"* ]]
  [[ "$stderr" != *"This changes"* ]]
}

@test "the lookup reads the Build Fleet stack in the given region" {
  nat_deployed
  run resolve eu-west-1
  [ "$status" -eq 0 ]
  [ "$(stub_call_count_matching aws 'describe-stacks --stack-name EdgeCVPortalBuildFleetStack.*--region eu-west-1')" = 1 ]
}

# ---------------------------------------------------------------------------
# deploy-infrastructure.sh passes the resolved value to cdk deploy
# ---------------------------------------------------------------------------

@test "deploy-infrastructure.sh keeps a deployed NAT gateway when the variable is unset" {
  install_infra
  nat_deployed

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [[ "$output" == *"buildFleetNatGateway=true"* ]]
}

@test "deploy-infrastructure.sh passes BUILD_FLEET_NAT_GATEWAY=true to a stack without the gateway" {
  install_infra

  run bash -c 'cd "$PORTAL_TREE" && BUILD_FLEET_NAT_GATEWAY=true ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"This changes the Build Fleet NAT gateway from 'false' (deployed now) to 'true'."* ]]
  run npx_args
  [[ "$output" == *"buildFleetNatGateway=true"* ]]
}

@test "deploy-infrastructure.sh passes BUILD_FLEET_NAT_GATEWAY=false to a stack with the gateway" {
  install_infra
  nat_deployed

  run bash -c 'cd "$PORTAL_TREE" && BUILD_FLEET_NAT_GATEWAY=false ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run npx_args
  [[ "$output" == *"buildFleetNatGateway=false"* ]]
  [[ "$output" != *"buildFleetNatGateway=true"* ]]
}

@test "deploy-infrastructure.sh on a fresh install passes no buildFleetNatGateway" {
  install_infra
  missing_stacks "$STACK"

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  stub_called npx
  run npx_args
  [[ "$output" != *"buildFleetNatGateway"* ]]
}

@test "deploy-infrastructure.sh stops before cdk deploy when the Build Fleet stack cannot be read" {
  install_infra
  deny_describe_stacks "$STACK"

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -ne 0 ]
  [[ "$output" == *"Could not read whether the Build Fleet NAT gateway is deployed"* ]]
  [ "$(stub_call_count npx)" = 0 ]
}

# ---------------------------------------------------------------------------
# deploy_portal_fixes.sh passes the resolved value to cdk deploy
# ---------------------------------------------------------------------------

@test "deploy_portal_fixes.sh keeps a deployed NAT gateway and logs the value" {
  install_fixes
  nat_deployed

  # The trusted UseCase account list no longer defaults to the deploying
  # account; set it, as the aws stub answers every ssm read with "21".
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && TRUSTED_USECASE_ACCOUNT_IDS=123456789012 ./deploy_portal_fixes.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"  buildFleetNatGateway=true"* ]]
  run npx_args
  [[ "$output" == *"buildFleetNatGateway=true"* ]]
}

@test "deploy_portal_fixes.sh stops before cdk deploy when the Build Fleet stack cannot be read" {
  install_fixes
  deny_describe_stacks "$STACK"

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && TRUSTED_USECASE_ACCOUNT_IDS=123456789012 ./deploy_portal_fixes.sh'
  [ "$status" -eq 1 ]
  [[ "$output" == *"Could not read whether the Build Fleet NAT gateway is deployed"* ]]
  [ "$(stub_call_count npx)" = 0 ]
}
