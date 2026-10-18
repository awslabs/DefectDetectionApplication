#!/usr/bin/env bats
#
# The scripts that use the trusted UseCase account list
# (scripts/portal-trusted-accounts.sh): deploy-portal.sh single-account
# resolves it once before any step (and stores the first-deploy value,
# create-only), deploy-frontend.sh and infrastructure/deploy_portal_fixes.sh
# redeploy the compute stack with it, and deploy-infrastructure.sh shows the
# list the CDK app deploys and how it differs from the deployed stacks
# without changing it. deploy-auth.sh keeps an exported AWS_PROFILE.
#
# Hermetic: the AWS environment is scrubbed in setup, and aws/node/cdk/npx/
# npm/sleep must resolve to the stubs. The orchestrator's steps stay stubs
# unless a test installs the real script.

bats_require_minimum_version 1.5.0

load test_helper

PARAM=/dda-portal/trusted-usecase-account-ids
STACKS="EdgeCVPortalComputeStack EdgeCVPortalNodeDesignerStack EdgeCVPortalSyntheticDataStack"
STEPS="deploy-account-role.sh deploy-auth.sh deploy-infrastructure.sh deploy-frontend.sh"

setup() {
  portal_harness_setup
  portal_scrub_aws_env
  _install_stub "$PORTAL_BIN/npm"
  _install_stub "$PORTAL_BIN/sleep"
  portal_require_stubbed_tools npm sleep
  # print_summary needs the Portal URL, or the orchestrator exits 1.
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  trusted_param_absent
  portal_not_deployed
  install_real_if_present deploy-portal.sh
  install_real_if_present scripts/portal-trusted-accounts.sh
  install_real_if_present scripts/portal-registry-enforcement.sh
}
teardown() { portal_harness_teardown; }

# ---- runners ----------------------------------------------------------------
orchestrate() {
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh single-account --non-interactive --log-file "$PORTAL_SANDBOX/deploy.log" "$@"' \
    _ "$@" < /dev/null
}
frontend() {
  install_real_if_present deploy-frontend.sh
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-frontend.sh' < /dev/null
}
fixes() {
  install_real_if_present infrastructure/deploy_portal_fixes.sh
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy_portal_fixes.sh' < /dev/null
}
infra() {
  install_real_if_present deploy-infrastructure.sh
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh' < /dev/null
}
auth() {
  install_real_if_present infrastructure/deploy-auth.sh
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh "$@"' _ "$@" < /dev/null
}

# ---- assertions ---------------------------------------------------------------
# stopped_before_any_change -> no CDK call and none of the four steps ran.
stopped_before_any_change() {
  [ "$(stub_call_count cdk)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
  [ "$(stub_call_count deploy-account-role.sh)" = 0 ]
  [ "$(stub_call_count deploy-auth.sh)" = 0 ]
  [ "$(stub_call_count deploy-infrastructure.sh)" = 0 ]
  [ "$(stub_call_count deploy-frontend.sh)" = 0 ]
}
# every_step_got_trusted <value> -> each of the four step stubs ran with
# TRUSTED_USECASE_ACCOUNT_IDS=<value>.
every_step_got_trusted() {
  local step
  for step in $STEPS; do
    stub_called "$step"
    [ "$(stub_env_value "$step" TRUSTED_USECASE_ACCOUNT_IDS)" = "$1" ]
  done
}
# output_has_line_starting <prefix> -> one line of the last `run` output starts with <prefix>
output_has_line_starting() {
  local l
  for l in "${lines[@]}"; do
    case "$l" in "$1"*) return 0 ;; esac
  done
  return 1
}
# output_line_count <text> -> how many lines of the last `run` output are exactly <text>
output_line_count() {
  local l c=0
  for l in "${lines[@]}"; do [ "$l" = "$1" ] && c=$((c + 1)); done
  echo "$c"
}

# ---------------------------------------------------------------------------
# deploy-portal.sh single-account
# ---------------------------------------------------------------------------

@test "deploy-portal.sh first deploy: the deploying account, stored create-only before the bootstrap, given to every step" {
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012"
  output_has_line "  source: first deploy: the deploying account; stored in SSM parameter $PARAM"
  output_has_line "Trusted accounts: 123456789012 (first deploy: the deploying account; stored in SSM parameter $PARAM)"
  log_has_line "Trusted UseCase accounts: 123456789012"
  log_has_line "  source: first deploy: the deploying account; stored in SSM parameter $PARAM"

  local put boot
  put="$(stub_call_index aws '^ssm put-parameter')"
  boot="$(stub_call_index cdk '^bootstrap')"
  [ -n "$put" ]
  [ -n "$boot" ]
  [ "$put" -lt "$boot" ]
  stub_call_has_pair "$put" --name "$PARAM"
  stub_call_has_pair "$put" --type String
  stub_call_has_pair "$put" --value 123456789012
  stub_call_has_pair "$put" --region eu-west-1
  [ "$(stub_call_args_n "$put" | grep -cx -- --overwrite)" = 0 ]
  [ "$(trusted_param_value)" = "123456789012" ]
  [ "$(stub_call_pwd_n "$boot")" = "$PORTAL_TREE" ]
  [ "$(stub_call_args_n "$boot")" = "$(printf 'bootstrap\naws://123456789012/eu-west-1')" ]
  every_step_got_trusted 123456789012
}

@test "deploy-portal.sh re-run: reads back the parameter the first run stored and writes nothing" {
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  [ "$(trusted_param_value)" = "123456789012" ]

  deploy_portal_stacks one-account
  : > "$STUB_TRACE"
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012"
  output_has_line "  source: SSM parameter $PARAM"
  output_has_line "  matches the deployed stacks"
  [ "$(stub_call_count_matching aws '^ssm put-parameter')" = 0 ]
  every_step_got_trusted 123456789012
}

@test "deploy-portal.sh from infrastructure/: the bootstrap still runs in edge-cv-portal/" {
  local boot
  unset_bootstrap_version
  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ../deploy-portal.sh single-account --non-interactive --region eu-west-1 --log-file "$PORTAL_SANDBOX/deploy.log"' < /dev/null
  [ "$status" -eq 0 ]
  boot="$(stub_call_index cdk '^bootstrap')"
  [ -n "$boot" ]
  [ "$(stub_call_pwd_n "$boot")" = "$PORTAL_TREE" ]
}

@test "deploy-portal.sh --profile: the first deploy trusts and stores that profile's account" {
  local put
  set_profile_account prodprofile 333333333333
  set_profile_region prodprofile eu-west-1
  orchestrate --profile prodprofile
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 333333333333"
  put="$(stub_call_index aws '^ssm put-parameter')"
  [ -n "$put" ]
  stub_call_has_pair "$put" --value 333333333333
  stub_call_has_pair "$put" --region eu-west-1
  [ "$(stub_call_env_value_n "$put" AWS_PROFILE)" = prodprofile ]
  every_step_got_trusted 333333333333
}

@test "deploy-portal.sh existing portal with nothing configured: reuses the deployed list, prints the store hint, writes nothing" {
  deploy_portal_stacks two-accounts
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012,222222222222"
  output_has_line "  source: the deployed portal stacks (nothing configured)"
  output_has_line "  To let deploy-auth.sh and deploy-infrastructure.sh run on their own, store it: aws ssm put-parameter --name $PARAM --type String --value 123456789012,222222222222 --region eu-west-1"
  [ "$(stub_call_count_matching aws '^ssm put-parameter')" = 0 ]
  every_step_got_trusted 123456789012,222222222222
}

@test "deploy-portal.sh: an exported list reaches every step unchanged and is compared" {
  export TRUSTED_USECASE_ACCOUNT_IDS='222222222222, 333333333333'
  deploy_portal_stacks one-account
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  output_has_line "  source: TRUSTED_USECASE_ACCOUNT_IDS"
  [[ "$output" == *"  ${PT_WARN}  EdgeCVPortalComputeStack: removes 123456789012; adds 222222222222,333333333333"* ]]
  [ "$(stub_call_count_matching aws 'trusted-usecase-account-ids')" = 0 ]
  every_step_got_trusted '222222222222, 333333333333'
}

@test "deploy-portal.sh: a CDK context list wins and TRUSTED_USECASE_ACCOUNT_IDS stays as set" {
  set_cdk_context_json '{"trustedUseCaseAccountIds": "444444444444"}'
  export TRUSTED_USECASE_ACCOUNT_IDS=555555555555
  deploy_portal_stacks one-account
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 444444444444"
  output_has_line "  source: CDK context trustedUseCaseAccountIds in infrastructure/cdk.context.json"
  every_step_got_trusted 555555555555
}

@test "deploy-portal.sh: an SSM list that adds an account is flagged on the terminal and in the Deployment_Log" {
  local stack
  trusted_param_found 123456789012,222222222222
  deploy_portal_stacks one-account
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  for stack in $STACKS; do
    output_has_line "  ${PT_WARN}  $stack: adds 222222222222${PT_ADDS_NOTE}"
    log_has_line "  ${PT_WARN}  $stack: adds 222222222222${PT_ADDS_NOTE}"
  done
}

@test "deploy-portal.sh: a malformed list stops before the bootstrap and every step" {
  export TRUSTED_USECASE_ACCOUNT_IDS=11111111111
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"TRUSTED_USECASE_ACCOUNT_IDS is not a comma-separated list of 12-digit account IDs: '11111111111'."* ]]
  output_has_line "   This account is 123456789012."
  stopped_before_any_change
  [ "$(stub_call_count_matching aws '^ssm put-parameter')" = 0 ]
}

@test "deploy-portal.sh: an SSM read error stops before the bootstrap and every step" {
  trusted_param_read_fails
  deploy_portal_stacks one-account
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"Could not read SSM parameter $PARAM in eu-west-1"* ]]
  stopped_before_any_change
}

@test "deploy-portal.sh: a wildcard-era ComputeStack stops before any step" {
  deploy_portal_stacks wildcard-era
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"pre-scoping grant"* ]]
  stopped_before_any_change
}

@test "deploy-portal.sh: deployed stacks that grant different lists stop before any step" {
  deploy_portal_stacks one-account two-accounts -
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"grant different trusted lists"* ]]
  stopped_before_any_change
}

@test "deploy-portal.sh: an unreadable ComputeStack template stops before any step" {
  deploy_portal_stacks one-account
  deny_stack_template EdgeCVPortalComputeStack
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"Could not read the deployed EdgeCVPortalComputeStack template in eu-west-1"* ]]
  stopped_before_any_change
}

@test "deploy-portal.sh: a parameter created concurrently stops with re-run before the bootstrap" {
  trusted_param_write_fails "An error occurred (ParameterAlreadyExists) when calling the PutParameter operation: The parameter already exists. To overwrite this value, set the overwrite option in the request to true."
  unset_bootstrap_version
  orchestrate --region eu-west-1
  [ "$status" -eq 2 ]
  [[ "$output" == *"was created after it was read; re-run to use it"* ]]
  stopped_before_any_change
  [ ! -e "$PORTAL_SANDBOX/deploy.log" ]
}

@test "deploy-portal.sh: a denied store warns, labels the source and still deploys" {
  trusted_param_write_fails
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  [[ "$output" == *"Could not store SSM parameter $PARAM in eu-west-1: An error occurred (AccessDeniedException)"* ]]
  output_has_line "  source: first deploy: the deploying account; not stored in SSM (see the error above)"
  every_step_got_trusted 123456789012
}

@test "deploy-portal.sh: a failed store's error reaches the Deployment_Log above the source line" {
  local err src step
  trusted_param_write_fails
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  err="$(log_line_of "Could not store SSM parameter $PARAM in eu-west-1: An error occurred (AccessDeniedException)")"
  src="$(log_line_of "not stored in SSM (see the error above)")"
  step="$(log_line_of "==> account-role")"
  [ -n "$err" ]
  [ -n "$src" ]
  [ -n "$step" ]
  [ "$err" -lt "$src" ]
  [ "$src" -lt "$step" ]
}

@test "deploy-portal.sh: a failed store's error is redacted on the terminal and in the Deployment_Log" {
  trusted_param_write_fails "An error occurred (AccessDeniedException) when calling the PutParameter operation: AWS_SECRET_ACCESS_KEY=abcdEXAMPLEsecret"
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  [[ "$output" != *"abcdEXAMPLEsecret"* ]]
  [[ "$output" == *"AWS_SECRET_ACCESS_KEY=***REDACTED***"* ]]
  grep -qF 'AWS_SECRET_ACCESS_KEY=***REDACTED***' "$PORTAL_SANDBOX/deploy.log"
  [ -z "$(grep -F abcdEXAMPLEsecret "$PORTAL_SANDBOX/deploy.log" || true)" ]
}

@test "deploy-portal.sh usecase and data runs make no trusted-list call" {
  local topology
  for topology in usecase data; do
    : > "$STUB_TRACE"
    run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh "$1" --non-interactive --region eu-west-1 --portal-account-id 222222222222 --external-id ext-0123456789 --log-file "$PORTAL_SANDBOX/deploy-$1.log"' \
      _ "$topology" < /dev/null
    [ "$status" -eq 0 ]
    stub_called deploy-account-role.sh
    [ "$(stub_call_count_matching aws 'trusted-usecase-account-ids')" = 0 ]
    [ "$(stub_call_count_matching aws '^cloudformation get-template')" = 0 ]
  done
}

# ---------------------------------------------------------------------------
# deploy-frontend.sh (its EdgeCVPortalComputeStack redeploy)
# ---------------------------------------------------------------------------

@test "deploy-frontend.sh: an SSM list wider than the deploying account reaches the compute redeploy" {
  local n
  seed_frontend_stack_outputs
  export AWS_REGION=eu-west-1
  trusted_param_found 123456789012,222222222222
  deploy_portal_stacks two-accounts
  frontend
  [ "$status" -eq 0 ]
  n="$(stub_call_index npx '^cdk deploy EdgeCVPortalComputeStack')"
  [ -n "$n" ]
  stub_call_has_pair "$n" -c "trustedUseCaseAccountIds=123456789012,222222222222"
}

@test "deploy-frontend.sh: with nothing configured it keeps the list the deployed stacks grant" {
  local n
  seed_frontend_stack_outputs
  export AWS_REGION=eu-west-1
  deploy_portal_stacks two-accounts
  frontend
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012,222222222222 (the deployed portal stacks (nothing configured))"
  n="$(stub_call_index npx '^cdk deploy EdgeCVPortalComputeStack')"
  [ -n "$n" ]
  stub_call_has_pair "$n" -c "trustedUseCaseAccountIds=123456789012,222222222222"
}

@test "deploy-frontend.sh: an unreadable template stops before anything is read, built, uploaded or deployed" {
  seed_frontend_stack_outputs
  export AWS_REGION=eu-west-1
  deploy_portal_stacks one-account
  deny_stack_template EdgeCVPortalComputeStack
  frontend
  [ "$status" -eq 1 ]
  [ "$(stub_call_count npm)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
  [ "$(stub_call_count_matching aws '^s3 ')" = 0 ]
  [ "$(stub_call_count_matching aws '^cloudfront ')" = 0 ]
  [ "$(stub_call_count_matching aws 'describe-stacks --stack-name EdgeCVPortalFrontendStack')" = 0 ]
}

@test "deploy-frontend.sh: nothing configured and no compute stack stops before anything changes" {
  seed_frontend_stack_outputs
  export AWS_REGION=eu-west-1
  frontend
  [ "$status" -eq 1 ]
  [[ "$output" == *"no trusted UseCase account list is configured and EdgeCVPortalComputeStack is not deployed"* ]]
  [ "$(stub_call_count npm)" = 0 ]
  [ "$(stub_call_count npx)" = 0 ]
  [ "$(stub_call_count_matching aws '^s3 ')" = 0 ]
  [ "$(stub_call_count_matching aws '^cloudfront ')" = 0 ]
  [ "$(stub_call_count_matching aws 'describe-stacks --stack-name EdgeCVPortalFrontendStack')" = 0 ]
}

@test "deploy-frontend.sh: an added account is flagged" {
  seed_frontend_stack_outputs
  export AWS_REGION=eu-west-1 TRUSTED_USECASE_ACCOUNT_IDS=123456789012,222222222222
  deploy_portal_stacks one-account - -
  frontend
  [ "$status" -eq 0 ]
  output_has_line "  ${PT_WARN}  EdgeCVPortalComputeStack: adds 222222222222${PT_ADDS_NOTE}"
}

# ---------------------------------------------------------------------------
# infrastructure/deploy_portal_fixes.sh
# ---------------------------------------------------------------------------

@test "deploy_portal_fixes.sh: a fresh install uses the deploying account and says so" {
  local n
  fixes
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012 (fresh install: the deploying account)"
  n="$(stub_call_index npx '^cdk deploy')"
  [ -n "$n" ]
  stub_call_has_pair "$n" -c "trustedUseCaseAccountIds=123456789012"
}

@test "deploy_portal_fixes.sh: an existing multi-account portal keeps its deployed list" {
  local n
  deploy_portal_stacks two-accounts
  fixes
  [ "$status" -eq 0 ]
  n="$(stub_call_index npx '^cdk deploy')"
  [ -n "$n" ]
  stub_call_has_pair "$n" -c "trustedUseCaseAccountIds=123456789012,222222222222"
}

@test "deploy_portal_fixes.sh: an added account is flagged" {
  trusted_param_found 123456789012,222222222222
  deploy_portal_stacks one-account - -
  fixes
  [ "$status" -eq 0 ]
  output_has_line "  ${PT_WARN}  EdgeCVPortalComputeStack: adds 222222222222${PT_ADDS_NOTE}"
}

# ---------------------------------------------------------------------------
# deploy-infrastructure.sh: shows the list, never changes it, never stops
# ---------------------------------------------------------------------------

@test "deploy-infrastructure.sh: shows that the SSM list drops a deployed account, then deploys it unchanged" {
  local stack deploy get n
  set_region us-east-1
  export AWS_REGION=eu-west-1
  trusted_param_found 123456789012
  deploy_portal_stacks two-accounts
  infra
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012 (SSM parameter $PARAM)"
  for stack in $STACKS; do
    output_has_line "  ${PT_WARN}  $stack: removes 222222222222"
  done
  deploy="$(stub_call_index npx '^cdk deploy --all')"
  [ -n "$deploy" ]
  get="$(stub_call_index aws "^ssm get-parameter --name $PARAM")"
  [ -n "$get" ]
  stub_call_has_pair "$get" --region eu-west-1
  [ "$get" -lt "$deploy" ]
  [ "$(stub_call_count_matching aws '^cloudformation get-template')" = 3 ]
  [ "$(stub_call_count_matching aws '^cloudformation get-template .*--region eu-west-1( |$)')" = 3 ]
  while IFS= read -r n; do
    [ "$n" -lt "$deploy" ]
  done < <(stub_trace_table | awk -F'\t' '$2 == "aws" && $4 ~ /^cloudformation get-template/ { print $1 }')
  [ "$(stub_call_args_n "$deploy" | grep -c trustedUseCaseAccountIds)" = 0 ]
  [ -z "$(stub_call_env_n "$deploy" | grep '^TRUSTED_USECASE_ACCOUNT_IDS=' || true)" ]
}

@test "deploy-infrastructure.sh: an added account is flagged" {
  local stack
  export AWS_REGION=eu-west-1
  trusted_param_found 123456789012,222222222222
  deploy_portal_stacks one-account
  infra
  [ "$status" -eq 0 ]
  for stack in $STACKS; do
    output_has_line "  ${PT_WARN}  $stack: adds 222222222222${PT_ADDS_NOTE}"
  done
}

@test "deploy-infrastructure.sh: a malformed value or a failed read never stops the deploy" {
  export AWS_REGION=eu-west-1
  trusted_param_found not-an-id
  deploy_portal_stacks one-account
  infra
  [ "$status" -eq 0 ]
  output_has_line_starting "${PT_WARN}  Trusted UseCase accounts: not compared with the deployed stacks: SSM parameter $PARAM is not a comma-separated list"
  [ "$(stub_call_count_matching npx '^cdk deploy --all')" = 1 ]

  : > "$STUB_TRACE"
  trusted_param_read_fails
  infra
  [ "$status" -eq 0 ]
  output_has_line_starting "${PT_WARN}  Trusted UseCase accounts: not compared with the deployed stacks: Could not read SSM parameter $PARAM in eu-west-1"
  [ "$(stub_call_count_matching npx '^cdk deploy --all')" = 1 ]
}

@test "deploy-infrastructure.sh: an unreadable template is not compared and the deploy goes on" {
  export AWS_REGION=eu-west-1
  trusted_param_found 123456789012
  deploy_portal_stacks one-account - -
  deny_stack_template EdgeCVPortalNodeDesignerStack
  infra
  [ "$status" -eq 0 ]
  output_has_line "Trusted UseCase accounts: 123456789012 (SSM parameter $PARAM)"
  output_has_line "  EdgeCVPortalNodeDesignerStack: not compared (its template could not be read)"
  [ "$(stub_call_count_matching npx '^cdk deploy --all')" = 1 ]
}

@test "deploy-infrastructure.sh: with nothing configured it says so and passes no list of its own" {
  local deploy
  export AWS_REGION=eu-west-1
  deploy_portal_stacks two-accounts
  infra
  [ "$status" -eq 0 ]
  output_has_line_starting "Trusted UseCase accounts: none configured"
  deploy="$(stub_call_index npx '^cdk deploy --all')"
  [ -n "$deploy" ]
  [ "$(stub_call_args_n "$deploy" | grep -c trustedUseCaseAccountIds)" = 0 ]
  [ -z "$(stub_call_env_n "$deploy" | grep '^TRUSTED_USECASE_ACCOUNT_IDS=' || true)" ]
}

@test "deploy-portal.sh: the infrastructure step does not repeat the comparison made before the first step" {
  install_real_if_present deploy-infrastructure.sh
  trusted_param_found 123456789012,222222222222
  deploy_portal_stacks one-account
  orchestrate --region eu-west-1
  [ "$status" -eq 0 ]
  [ "$(output_line_count "  ${PT_WARN}  EdgeCVPortalComputeStack: adds 222222222222${PT_ADDS_NOTE}")" = 1 ]
  output_has_line "  compared with the deployed stacks by deploy-portal.sh before its first step"
  log_has_line "  ${PT_WARN}  EdgeCVPortalComputeStack: adds 222222222222${PT_ADDS_NOTE}"
}

@test "deploy-infrastructure.sh: a PORTAL_TRUSTED_ACCOUNTS_COMPARED for another list does not hide the comparison" {
  local stack
  export AWS_REGION=eu-west-1 PORTAL_TRUSTED_ACCOUNTS_COMPARED=999999999999
  trusted_param_found 123456789012
  deploy_portal_stacks two-accounts
  infra
  [ "$status" -eq 0 ]
  for stack in $STACKS; do
    output_has_line "  ${PT_WARN}  $stack: removes 222222222222"
  done
  [[ "$output" != *"compared with the deployed stacks by deploy-portal.sh"* ]]
}

# ---------------------------------------------------------------------------
# infrastructure/deploy-auth.sh: the AWS profile
# ---------------------------------------------------------------------------

@test "deploy-auth.sh: an exported AWS_PROFILE reaches every call" {
  export AWS_PROFILE=prod AWS_REGION=eu-west-1
  auth
  [ "$status" -eq 0 ]
  output_has_line "Using AWS profile: prod"
  [ "$(stub_env_values_all aws AWS_PROFILE | sort -u)" = prod ]
  [ "$(stub_env_values_all npm AWS_PROFILE | sort -u)" = prod ]
  [ "$(stub_env_values_all npx AWS_PROFILE | sort -u)" = prod ]
}

@test "deploy-auth.sh: --profile still wins over the exported AWS_PROFILE" {
  export AWS_PROFILE=prod AWS_REGION=eu-west-1
  auth --profile other
  [ "$status" -eq 0 ]
  [ "$(stub_env_values_all aws AWS_PROFILE | sort -u)" = other ]
  [ "$(stub_env_values_all npm AWS_PROFILE | sort -u)" = other ]
  [ "$(stub_env_values_all npx AWS_PROFILE | sort -u)" = other ]
}

@test "deploy-auth.sh: no profile stays unset instead of exported empty" {
  export AWS_REGION=eu-west-1
  auth
  [ "$status" -eq 0 ]
  [ "$(stub_env_values_all aws AWS_PROFILE | sort -u)" = "<unset>" ]
  [ "$(stub_env_values_all npm AWS_PROFILE | sort -u)" = "<unset>" ]
  [ "$(stub_env_values_all npx AWS_PROFILE | sort -u)" = "<unset>" ]
}
