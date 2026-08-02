#!/usr/bin/env bats
#
# Self-tests for the one-command-portal-deploy stub harness (tests 1.2).
# These verify the harness itself behaves correctly and runs fully offline;
# they do not depend on deploy-portal.sh (task 1.1) existing.

load test_helper

setup() { portal_harness_setup; }
teardown() { portal_harness_teardown; }

@test "harness: setup helper sources cleanly and defines its API" {
  for fn in portal_harness_setup seed_portal_tree seed_config_file set_cfn_output \
            set_bootstrap_version set_account_id set_region stub_output stub_exit \
            stub_calls stub_called stub_pwd_of stub_args_of stub_env_of stub_stdin_of; do
    run type -t "$fn"
    [ "$status" -eq 0 ]
    [ "$output" = "function" ]
  done
}

@test "harness: stub source file is syntactically valid" {
  run bash -n "$PORTAL_STUB_SRC"
  [ "$status" -eq 0 ]
}

@test "harness: temp bin/ is prepended to PATH with all stubs" {
  case ":$PATH:" in *":$PORTAL_BIN:"*) : ;; *) false ;; esac
  for name in deploy-account-role.sh deploy-auth.sh deploy-infrastructure.sh \
              deploy-frontend.sh configure-bucket-cors.sh aws node cdk npx; do
    run command -v "$name"
    [ "$status" -eq 0 ]
    [[ "$output" == "$PORTAL_BIN/$name" ]]
  done
}

@test "harness: fake portal tree has scripts at the expected relative paths" {
  [ -x "$PORTAL_TREE/deploy-account-role.sh" ]
  [ -x "$PORTAL_TREE/deploy-infrastructure.sh" ]
  [ -x "$PORTAL_TREE/deploy-frontend.sh" ]
  [ -x "$PORTAL_TREE/configure-bucket-cors.sh" ]
  [ -x "$PORTAL_TREE/infrastructure/deploy-auth.sh" ]
  [ -f "$PORTAL_TREE/infrastructure/cdk.json" ]
  [ -d "$PORTAL_TREE/frontend/public" ]
}

@test "stub: records name, order, pwd and positional args" {
  ( cd "$PORTAL_TREE" && ./deploy-account-role.sh single-account )
  ( cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh --region us-west-2 )

  run stub_calls
  [ "${lines[0]}" = "deploy-account-role.sh" ]
  [ "${lines[1]}" = "deploy-auth.sh" ]

  [ "$(stub_order_of deploy-account-role.sh)" = "1" ]
  [ "$(stub_order_of deploy-auth.sh)" = "2" ]

  [ "$(stub_pwd_of deploy-account-role.sh)" = "$PORTAL_TREE" ]
  [ "$(stub_pwd_of deploy-auth.sh)" = "$PORTAL_TREE/infrastructure" ]

  run stub_args_of deploy-account-role.sh
  [ "$output" = "single-account" ]

  run stub_args_of deploy-auth.sh
  [ "${lines[0]}" = "--region" ]
  [ "${lines[1]}" = "us-west-2" ]
}

@test "stub: captures selected env vars" {
  ( cd "$PORTAL_TREE" \
      && AWS_PROFILE=prod TRUSTED_USECASE_ACCOUNT_IDS=111,222 cloudFrontDomain=d1.cloudfront.net \
         ./deploy-frontend.sh )

  [ "$(stub_env_value deploy-frontend.sh AWS_PROFILE)" = "prod" ]
  [ "$(stub_env_value deploy-frontend.sh TRUSTED_USECASE_ACCOUNT_IDS)" = "111,222" ]
  [ "$(stub_env_value deploy-frontend.sh cloudFrontDomain)" = "d1.cloudfront.net" ]
}

@test "stub: captures stdin feed and does not block without one" {
  printf 'line-one\nline-two\n' | ( cd "$PORTAL_TREE" && ./deploy-account-role.sh usecase )
  run stub_stdin_of deploy-account-role.sh
  [ "${lines[0]}" = "line-one" ]
  [ "${lines[1]}" = "line-two" ]

  # No stdin provided (redirected from /dev/null) must not hang.
  ( cd "$PORTAL_TREE" && ./deploy-infrastructure.sh < /dev/null )
  run stub_stdin_of deploy-infrastructure.sh
  [ -z "$output" ]
}

@test "stub: emits caller-controlled stdout, stderr and exit code" {
  stub_output deploy-infrastructure.sh "hello-out" "hello-err"
  stub_exit deploy-infrastructure.sh 7

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 7 ]
  [[ "$output" == *"hello-out"* ]]
  [[ "$output" == *"hello-err"* ]]
}

@test "aws stub: sts get-caller-identity returns the seeded account" {
  set_account_id "555000111222"
  run aws sts get-caller-identity --query Account --output text
  [ "$status" -eq 0 ]
  [ "$output" = "555000111222" ]
}

@test "aws stub: failed identity simulates invalid credentials" {
  fail_identity
  run aws sts get-caller-identity
  [ "$status" -ne 0 ]
}

@test "aws stub: configure get region honors seeded region and empty" {
  set_region "eu-west-1"
  run aws configure get region
  [ "$status" -eq 0 ]
  [ "$output" = "eu-west-1" ]

  set_region ""
  run aws configure get region
  [ "$status" -ne 0 ]
}

@test "aws stub: ssm bootstrap version present vs absent" {
  set_bootstrap_version "21"
  run aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query 'Parameter.Value' --output text
  [ "$status" -eq 0 ]
  [ "$output" = "21" ]

  unset_bootstrap_version
  run aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query 'Parameter.Value' --output text
  [ "$status" -ne 0 ]
}

@test "aws stub: cloudformation describe-stacks returns canned output value" {
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d123abc.cloudfront.net
  run aws cloudformation describe-stacks \
    --stack-name EdgeCVPortalFrontendStack \
    --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" \
    --output text
  [ "$status" -eq 0 ]
  [ "$output" = "d123abc.cloudfront.net" ]

  # Backtick query form (used by the existing scripts) also resolves.
  run aws cloudformation describe-stacks \
    --stack-name EdgeCVPortalFrontendStack \
    --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
    --output text
  [ "$output" = "d123abc.cloudfront.net" ]
}

@test "aws stub: cloudformation describe-stacks yields empty for unknown output" {
  run aws cloudformation describe-stacks \
    --stack-name EdgeCVPortalFrontendStack \
    --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" \
    --output text
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "seed_config_file: writes reusable *-config.txt with External ID" {
  file="$(seed_config_file usecase 444555666777)"
  [ -f "$file" ]
  [[ "$file" == *"usecase-account-444555666777-config.txt" ]]
  run grep '^EXTERNAL_ID=' "$file"
  [ "$status" -eq 0 ]
  run grep '^ROLE_ARN=arn:aws:iam::444555666777:role/DDAPortalAccessRole' "$file"
  [ "$status" -eq 0 ]
}

@test "trace helpers: call count and multiple invocations" {
  ( cd "$PORTAL_TREE" && ./deploy-infrastructure.sh < /dev/null )
  cdk deploy --all < /dev/null
  cdk bootstrap < /dev/null

  [ "$(stub_call_count cdk)" = "2" ]
  [ "$(stub_call_count deploy-infrastructure.sh)" = "1" ]
  stub_called cdk
  ! stub_called deploy-frontend.sh
}
