#!/usr/bin/env bats
#
# UseCase / Data account setup (deploy-account-role.sh usecase|data, alone and
# through deploy-portal.sh): the CDK dependencies are installed with npm ci in
# infrastructure/ before the first npx call, an unbootstrapped account is
# bootstrapped through that account's own CDK app (never a bare
# `cdk bootstrap`, which would synthesize the portal app), and every call
# stays in one region.
#
# Hermetic: the AWS environment is scrubbed in setup, and aws/node/cdk/npx/
# npm/sleep must resolve to the stubs. The tool stubs leave stdin unread, so
# the answers fed to deploy-account-role.sh reach its own prompts.

bats_require_minimum_version 1.5.0

load test_helper

USECASE_FEED=$'222222222222\next-0123456789\n'
DATA_FEED=$'222222222222\next-0123456789\nbucket-a,bucket-b\n'
USECASE_APP="npx ts-node bin/usecase-account-app.ts"
DATA_APP="npx ts-node bin/data-account-app.ts"

setup() {
  portal_harness_setup
  portal_scrub_aws_env
  _install_stub "$PORTAL_BIN/npm"
  _install_stub "$PORTAL_BIN/sleep"
  portal_require_stubbed_tools npm sleep
  stub_tools_ignore_stdin
  install_real_if_present deploy-account-role.sh
  install_real_if_present deploy-portal.sh
}
teardown() { portal_harness_teardown; }

# account_role <topology> <feed> -> run the real script with <feed> on stdin.
account_role() {
  run bash -c 'cd "$PORTAL_TREE" && printf "%s" "$2" | ./deploy-account-role.sh "$1"' _ "$1" "$2"
}

# regions -> the distinct regions of the recorded regional calls, space-separated.
regions() { stub_regions_used | awk '{ print $3 }' | sort -u | tr '\n' ' ' | sed 's/ $//'; }

# npm_ci_before_first_npx -> `npm ci` ran in infrastructure/, before any npx call.
npm_ci_before_first_npx() {
  local ci npx
  ci="$(stub_call_index npm '^ci$')"
  npx="$(stub_call_index npx '.')"
  [ -n "$ci" ]
  [ -n "$npx" ]
  [ "$(stub_call_pwd_n "$ci")" = "$PORTAL_TREE/infrastructure" ]
  [ "$ci" -lt "$npx" ]
}

@test "deploy-account-role.sh usecase: npm ci runs in infrastructure/ before the first npx call" {
  export AWS_REGION=eu-west-1
  unset_bootstrap_version
  account_role usecase "$USECASE_FEED"
  [ "$status" -eq 0 ]
  npm_ci_before_first_npx
}

@test "deploy-account-role.sh usecase: an unbootstrapped account is bootstrapped through the UseCase app with its context" {
  local boot deploy
  export AWS_REGION=eu-west-1
  unset_bootstrap_version
  account_role usecase "$USECASE_FEED"
  [ "$status" -eq 0 ]
  boot="$(stub_call_index npx '^cdk bootstrap')"
  deploy="$(stub_call_index npx '^cdk deploy')"
  [ -n "$boot" ]
  [ -n "$deploy" ]
  [ "$boot" -lt "$deploy" ]
  [ "$(stub_call_args_n "$boot")" = "$(printf '%s\n' cdk bootstrap aws://123456789012/eu-west-1 \
      -a "$USECASE_APP" -c portalAccountId=222222222222 -c externalId=ext-0123456789)" ]
  [ "$(stub_call_args_n "$deploy")" = "$(printf '%s\n' cdk deploy DDAPortalUseCaseAccountStack \
      -a "$USECASE_APP" -c portalAccountId=222222222222 -c externalId=ext-0123456789 \
      --require-approval never)" ]
  [ "$(stub_call_pwd_n "$boot")" = "$PORTAL_TREE/infrastructure" ]
  [ "$(stub_call_pwd_n "$deploy")" = "$PORTAL_TREE/infrastructure" ]
}

@test "deploy-account-role.sh data: npm ci first, then bootstrap and deploy through the Data app with the bucket context" {
  local boot deploy
  export AWS_REGION=eu-west-1
  unset_bootstrap_version
  account_role data "$DATA_FEED"
  [ "$status" -eq 0 ]
  npm_ci_before_first_npx
  boot="$(stub_call_index npx '^cdk bootstrap')"
  deploy="$(stub_call_index npx '^cdk deploy')"
  [ -n "$boot" ]
  [ -n "$deploy" ]
  [ "$(stub_call_args_n "$boot")" = "$(printf '%s\n' cdk bootstrap aws://123456789012/eu-west-1 \
      -a "$DATA_APP" -c portalAccountId=222222222222 -c externalId=ext-0123456789 \
      -c dataBucketNames=bucket-a,bucket-b)" ]
  stub_call_has_pair "$deploy" -a "$DATA_APP"
  stub_call_has_pair "$deploy" -c portalAccountId=222222222222
  stub_call_has_pair "$deploy" -c externalId=ext-0123456789
  stub_call_has_pair "$deploy" -c dataBucketNames=bucket-a,bucket-b
}

@test "deploy-account-role.sh usecase on a bootstrapped account installs, skips the bootstrap and deploys through the UseCase app" {
  local ci deploy
  export AWS_REGION=eu-west-1
  set_bootstrap_version 21
  account_role usecase "$USECASE_FEED"
  [ "$status" -eq 0 ]
  [ -z "$(stub_call_index npx '^cdk bootstrap')" ]
  ci="$(stub_call_index npm '^ci$')"
  deploy="$(stub_call_index npx '^cdk deploy DDAPortalUseCaseAccountStack')"
  [ -n "$ci" ]
  [ -n "$deploy" ]
  [ "$ci" -lt "$deploy" ]
  stub_call_has_pair "$deploy" -a "$USECASE_APP"
}

@test "deploy-portal.sh usecase --region eu-west-1 with us-east-1 configured: Greengrass and CDK use eu-west-1" {
  set_region us-east-1
  unset_bootstrap_version
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh usecase --non-interactive --region eu-west-1 --portal-account-id 222222222222 --external-id ext-0123456789 --log-file "$PORTAL_SANDBOX/deploy.log"' < /dev/null
  [ "$status" -eq 0 ]
  npm_ci_before_first_npx
  [ "$(stub_call_count_matching aws '^greengrassv2 ')" -ge 1 ]
  [ "$(stub_call_count_matching aws '^greengrassv2 .*--region eu-west-1( |$)')" = "$(stub_call_count_matching aws '^greengrassv2 ')" ]
  [ "$(regions)" = eu-west-1 ]
}

@test "deploy-portal.sh data --region eu-west-1 installs before the CDK app and stays in one region" {
  local deploy
  set_region us-east-1
  unset_bootstrap_version
  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh data --non-interactive --region eu-west-1 --portal-account-id 222222222222 --external-id ext-0123456789 --data-buckets bucket-a,bucket-b --log-file "$PORTAL_SANDBOX/deploy.log"' < /dev/null
  [ "$status" -eq 0 ]
  npm_ci_before_first_npx
  deploy="$(stub_call_index npx '^cdk deploy DDAPortalDataAccountStack')"
  [ -n "$deploy" ]
  stub_call_has_pair "$deploy" -c dataBucketNames=bucket-a,bucket-b
  [ "$(regions)" = eu-west-1 ]
}
