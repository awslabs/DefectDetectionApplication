#!/usr/bin/env bats
#
# One region for every call, and the AWS profile first: each deploy script
# uses an exported AWS_REGION / AWS_DEFAULT_REGION before the profile's
# configured region (deploy-auth.sh: --region first), stops with a hint when
# none is set, and deploy-portal.sh exports --profile before its first AWS
# call and passes the region it resolved to every step.
#
# Hermetic: the AWS environment is scrubbed in setup, and aws/node/cdk/npx/
# npm/sleep must resolve to the stubs. `regions` lists every region the
# recorded aws/npx/cdk calls would use (helpers: stub_regions_used).

bats_require_minimum_version 1.5.0

load test_helper

STEPS="deploy-account-role.sh deploy-auth.sh deploy-infrastructure.sh deploy-frontend.sh"

setup() {
  portal_harness_setup
  portal_scrub_aws_env
  _install_stub "$PORTAL_BIN/npm"
  _install_stub "$PORTAL_BIN/sleep"
  portal_require_stubbed_tools npm sleep
  seed_frontend_stack_outputs
  trusted_param_absent
  portal_not_deployed
}
teardown() { portal_harness_teardown; }

# install_all_real -> every real deploy script and helper this checkout has.
install_all_real() {
  local rel
  for rel in deploy-portal.sh deploy-account-role.sh deploy-infrastructure.sh deploy-frontend.sh \
             infrastructure/deploy-auth.sh infrastructure/deploy_portal_fixes.sh \
             scripts/portal-trusted-accounts.sh scripts/portal-registry-enforcement.sh; do
    install_real_if_present "$rel"
  done
}

# regions -> the distinct regions of the recorded regional calls, space-separated.
regions() { stub_regions_used | awk '{ print $3 }' | sort -u | tr '\n' ' ' | sed 's/ $//'; }

# aws_calls_except <ERE> -> how many aws calls have args that do not match <ERE>.
aws_calls_except() {
  stub_trace_table | STUB_Q_RE="$1" awk -F'\t' '$2 == "aws" && $4 !~ ENVIRON["STUB_Q_RE"] { c++ } END { print c + 0 }'
}

# ground_truth_trust_is_eu_west_1 -> DDASageMakerExecutionRole trusts the
# eu-west-1 SageMaker Ground Truth account, not the us-east-1 one.
ground_truth_trust_is_eu_west_1() {
  local n
  n="$(stub_call_index aws '^iam create-role --role-name DDASageMakerExecutionRole')"
  [ -n "$n" ]
  stub_call_args_n "$n" | grep -qF 'arn:aws:iam::685385470294:root'
  [ "$(stub_call_args_n "$n" | grep -c 432418664414)" = 0 ]
}

infra() {
  install_all_real
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh' < /dev/null
}
frontend() {
  install_all_real
  export TRUSTED_USECASE_ACCOUNT_IDS=123456789012
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-frontend.sh' < /dev/null
}
auth() {
  install_all_real
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh "$@"' _ "$@" < /dev/null
}
account_role() {
  install_all_real
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-account-role.sh "$@"' _ "$@" < /dev/null
}
orchestrate() {
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh single-account --non-interactive --log-file "$PORTAL_SANDBOX/deploy.log" "$@"' \
    _ "$@" < /dev/null
}

# ---------------------------------------------------------------------------
# deploy-infrastructure.sh
# ---------------------------------------------------------------------------

@test "deploy-infrastructure.sh: AWS_REGION wins over the configured region" {
  set_region us-east-1
  export AWS_REGION=eu-west-1
  infra
  [ "$status" -eq 0 ]
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-infrastructure.sh: AWS_DEFAULT_REGION alone is enough" {
  set_region ""
  export AWS_DEFAULT_REGION=eu-west-1
  infra
  [ "$status" -eq 0 ]
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-infrastructure.sh: the configured region is used when nothing is exported" {
  set_region us-east-1
  infra
  [ "$status" -eq 0 ]
  [ "$(regions)" = us-east-1 ]
}

@test "deploy-infrastructure.sh: no region stops with a hint before npm, npx or any CloudFormation call" {
  set_region ""
  infra
  [ "$status" -eq 1 ]
  [[ "$output" == *"No AWS region set."* ]]
  [ "$(stub_call_count npm)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
  [ "$(aws_calls_except '^(sts|configure)( |$)')" = 0 ]
}

# ---------------------------------------------------------------------------
# deploy-frontend.sh
# ---------------------------------------------------------------------------

@test "deploy-frontend.sh: the compute redeploy goes to the region the outputs were read from, not a stale CDK_DEFAULT_REGION" {
  local n
  set_region ""
  export AWS_REGION=eu-west-1 CDK_DEFAULT_REGION=us-west-2
  frontend
  [ "$status" -eq 0 ]
  n="$(stub_call_index npx '^cdk deploy EdgeCVPortalComputeStack')"
  [ -n "$n" ]
  [ "$(stub_call_env_value_n "$n" AWS_REGION)" = eu-west-1 ]
  [ "$(stub_call_env_value_n "$n" CDK_DEFAULT_REGION)" = eu-west-1 ]
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-frontend.sh: a configured region alone is used for every call" {
  set_region eu-west-1
  frontend
  [ "$status" -eq 0 ]
  stub_called npx
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-frontend.sh: no region stops before any stack read" {
  set_region ""
  frontend
  [ "$status" -eq 1 ]
  [[ "$output" == *"No AWS region set."* ]]
  [ "$(stub_call_count aws)" = 1 ]
  [ -n "$(stub_call_index aws '^configure get region')" ]
  [ "$(stub_call_count npm)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
}

# ---------------------------------------------------------------------------
# infrastructure/deploy-auth.sh
# ---------------------------------------------------------------------------

@test "deploy-auth.sh: --region wins over AWS_REGION and the configured region for the bootstrap, the deploy and the outputs" {
  set_region eu-west-1
  export AWS_REGION=us-west-2
  missing_stacks CDKToolkit
  auth --region ap-south-1
  [ "$status" -eq 0 ]
  [ -n "$(stub_call_index npx '^cdk bootstrap')" ]
  [ -n "$(stub_call_index npx '^cdk deploy EdgeCVPortalAuthStack')" ]
  [ "$(regions)" = ap-south-1 ]
}

@test "deploy-auth.sh: AWS_REGION is used without --region" {
  set_region us-east-1
  export AWS_REGION=eu-west-1
  auth
  [ "$status" -eq 0 ]
  stub_called npx
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-auth.sh: no region stops with the hint before npm or npx" {
  set_region ""
  auth
  [ "$status" -eq 1 ]
  [[ "$output" == *"Error: no AWS region set."* ]]
  [ "$(stub_call_count npm)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
}

# ---------------------------------------------------------------------------
# deploy-account-role.sh
# ---------------------------------------------------------------------------

@test "deploy-account-role.sh single-account: AWS_REGION wins for Greengrass and the Ground Truth trust principal" {
  set_region us-east-1
  export AWS_REGION=eu-west-1
  account_role single-account
  [ "$status" -eq 0 ]
  [ "$(stub_call_count_matching aws '^greengrassv2 ')" -ge 1 ]
  [ "$(stub_call_count_matching aws '^greengrassv2 .*--region eu-west-1( |$)')" = "$(stub_call_count_matching aws '^greengrassv2 ')" ]
  ground_truth_trust_is_eu_west_1
}

@test "deploy-account-role.sh: no region stops before the menu with a hint" {
  set_region ""
  account_role
  [ "$status" -eq 1 ]
  [[ "$output" == *"No AWS region set."* ]]
  [[ "$output" != *"Select deployment type"* ]]
  [ "$(stub_call_count_matching aws '^(iam|greengrassv2) ')" = 0 ]
}

# ---------------------------------------------------------------------------
# deploy-portal.sh single-account
# ---------------------------------------------------------------------------

@test "deploy-portal.sh single-account --region eu-west-1 with us-east-1 configured: every step deploys to eu-west-1" {
  install_all_real
  set_region us-east-1
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  [ "$(regions)" = eu-west-1 ]
  ground_truth_trust_is_eu_west_1
}

@test "deploy-portal.sh single-account with only AWS_DEFAULT_REGION set: every step deploys there" {
  install_all_real
  set_region ""
  export AWS_DEFAULT_REGION=eu-west-1
  orchestrate
  [ "$status" -eq 0 ]
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-portal.sh --profile: the identity, the region and the bootstrap target come from that profile" {
  local boot step
  install_real_if_present deploy-portal.sh
  install_real_if_present scripts/portal-trusted-accounts.sh
  set_profile_account prodprofile 333333333333
  set_profile_region prodprofile eu-west-1
  set_region us-east-1
  unset_bootstrap_version
  orchestrate --profile prodprofile
  [ "$status" -eq 0 ]
  output_has_line "AWS account: 333333333333"
  output_has_line "AWS region:  eu-west-1"
  [ "$(sed -n 1p "$STUB_TRACE" | cut -f1)" = aws ]
  [ "$(stub_call_args_n 1 | head -n 2 | tr '\n' ' ')" = "sts get-caller-identity " ]
  [ "$(stub_call_env_value_n 1 AWS_PROFILE)" = prodprofile ]
  boot="$(stub_call_index cdk '^bootstrap')"
  [ -n "$boot" ]
  [ "$(stub_call_args_n "$boot")" = "$(printf 'bootstrap\naws://333333333333/eu-west-1')" ]
  for step in $STEPS; do
    [ "$(stub_env_value "$step" AWS_PROFILE)" = prodprofile ]
  done
}

# expect_region_everywhere <region> [deploy-portal.sh args...] -> one run with
# the steps stubbed: the region is printed, given to the auth step as
# --region and exported as AWS_REGION to every step.
expect_region_everywhere() {
  local region="$1" step; shift
  : > "$STUB_TRACE"
  orchestrate "$@"
  [ "$status" -eq 0 ]
  output_has_line "AWS region:  $region"
  stub_called deploy-auth.sh
  [ "$(stub_args_of deploy-auth.sh)" = "$(printf -- '--region\n%s' "$region")" ]
  for step in $STEPS; do
    [ "$(stub_env_value "$step" AWS_REGION)" = "$region" ]
  done
}

@test "deploy-portal.sh resolves --region > AWS_REGION > AWS_DEFAULT_REGION > configured > CDK_DEFAULT_REGION and passes it to every step" {
  install_real_if_present deploy-portal.sh
  install_real_if_present scripts/portal-trusted-accounts.sh
  export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=eu-central-1 CDK_DEFAULT_REGION=eu-west-2
  set_region us-east-1
  expect_region_everywhere ap-south-1 --region ap-south-1
  expect_region_everywhere us-west-2
  unset AWS_REGION
  expect_region_everywhere eu-central-1
  unset AWS_DEFAULT_REGION
  expect_region_everywhere us-east-1
  set_region ""
  expect_region_everywhere eu-west-2
}

@test "deploy-portal.sh with no region anywhere stops in the preflight and changes nothing" {
  local step
  install_real_if_present deploy-portal.sh
  install_real_if_present scripts/portal-trusted-accounts.sh
  set_region ""
  orchestrate
  [ "$status" -eq 2 ]
  [[ "$output" == *"no AWS region resolved"* ]]
  for step in $STEPS; do
    [ "$(stub_call_count "$step")" = 0 ]
  done
  [ "$(stub_call_count cdk)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
  [ "$(stub_call_count_matching aws '^ssm put-parameter')" = 0 ]
}
