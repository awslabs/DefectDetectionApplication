#!/usr/bin/env bats
#
# Self-tests for the one-command-portal-deploy stub harness (tests 1.2).
# These verify the harness itself behaves correctly and runs fully offline;
# they do not depend on deploy-portal.sh (task 1.1) existing.

load test_helper

# Hermetic: no AWS identity, region or config from the caller's shell, and
# aws/node/cdk/npx must resolve to the stubs.
setup() { portal_harness_setup; portal_scrub_aws_env; portal_require_stubbed_tools; }
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

# ---------------------------------------------------------------------------
# Opt-in fixtures: the trusted UseCase account list, stack templates,
# per-profile identities (helpers/stub.sh item 4). bats 1.2 compatible.
# ---------------------------------------------------------------------------

TRUSTED_PARAM=/dda-portal/trusted-usecase-account-ids

@test "aws stub: the trusted-account parameter is found, absent or unreadable" {
  trusted_param_found 123456789012
  run aws ssm get-parameter --name "$TRUSTED_PARAM" --query Parameter.Value --output text < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "123456789012" ]

  trusted_param_absent
  run aws ssm get-parameter --name "$TRUSTED_PARAM" --query Parameter.Value --output text < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"An error occurred (ParameterNotFound) when calling the GetParameter operation"* ]]

  trusted_param_read_fails
  run aws ssm get-parameter --name "$TRUSTED_PARAM" --query Parameter.Value --output text < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"An error occurred (AccessDeniedException) when calling the GetParameter operation"* ]]
}

@test "aws stub: put-parameter on the trusted-account parameter is create-only" {
  trusted_param_absent
  run aws ssm put-parameter --name "$TRUSTED_PARAM" --type String --value 111111111111 < /dev/null
  [ "$status" -eq 0 ]
  [ "$(trusted_param_value)" = "111111111111" ]

  run aws ssm put-parameter --name "$TRUSTED_PARAM" --type String --value 222222222222 < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"An error occurred (ParameterAlreadyExists) when calling the PutParameter operation"* ]]
  [ "$(trusted_param_value)" = "111111111111" ]

  run aws ssm put-parameter --name "$TRUSTED_PARAM" --type String --overwrite --value 222222222222 < /dev/null
  [ "$status" -eq 0 ]
  [ "$(trusted_param_value)" = "222222222222" ]

  trusted_param_write_fails
  run aws ssm put-parameter --name "$TRUSTED_PARAM" --type String --overwrite --value 333333333333 < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"An error occurred (AccessDeniedException) when calling the PutParameter operation"* ]]
  [ "$(trusted_param_value)" = "222222222222" ]
}

@test "aws stub: other ssm names keep the bootstrap-version answer" {
  trusted_param_found 123456789012
  set_bootstrap_version 21
  run aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --query Parameter.Value --output text < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "21" ]

  # Without the opt-in, the trusted name gets the legacy answer too.
  unset STUB_SSM_TRUSTED_FILE
  run aws ssm get-parameter --name "$TRUSTED_PARAM" --query Parameter.Value --output text < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "21" ]
}

@test "aws stub: get-template serves fixture templates, missing stacks and denied reads" {
  deploy_stack_template EdgeCVPortalComputeStack one-account
  run aws cloudformation get-template --stack-name EdgeCVPortalComputeStack --region eu-west-1 \
    --query TemplateBody --output json < /dev/null
  [ "$status" -eq 0 ]
  [[ "$output" == *'"arn:aws:iam::123456789012:role/DDAPortalAccessRole"'* ]]

  run aws cloudformation get-template --stack-name EdgeCVPortalNodeDesignerStack --region eu-west-1 \
    --query TemplateBody --output json < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"Stack with id EdgeCVPortalNodeDesignerStack does not exist"* ]]

  deny_stack_template EdgeCVPortalSyntheticDataStack
  run aws cloudformation get-template --stack-name EdgeCVPortalSyntheticDataStack --region eu-west-1 \
    --query TemplateBody --output json < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"An error occurred (AccessDenied) when calling the GetTemplate operation"* ]]

  # Without the opt-in, get-template stays an empty success.
  unset STUB_CFN_TEMPLATES_DIR
  run aws cloudformation get-template --stack-name EdgeCVPortalComputeStack --region eu-west-1 \
    --query TemplateBody --output json < /dev/null
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "aws stub: describe-stacks reports the stacks listed as missing" {
  missing_stacks CDKToolkit
  run aws cloudformation describe-stacks --stack-name CDKToolkit --region eu-west-1 < /dev/null
  [ "$status" -eq 254 ]
  [[ "$output" == *"Stack with id CDKToolkit does not exist"* ]]

  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  run aws cloudformation describe-stacks --stack-name EdgeCVPortalFrontendStack \
    --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" --output text < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "d1.cloudfront.net" ]
}

@test "aws stub: configure export-credentials fails like a CLI without credentials" {
  set_region us-east-1
  run aws configure export-credentials --format env < /dev/null
  [ "$status" -ne 0 ]
  [[ "$output" != *"us-east-1"* ]]

  run aws configure get region < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "us-east-1" ]
}

@test "aws stub: sts and configure get region follow the per-profile maps" {
  set_profile_account prodprofile 333333333333
  set_profile_region prodprofile eu-west-1

  run env AWS_PROFILE=prodprofile aws sts get-caller-identity --query Account --output text < /dev/null
  [ "$output" = "333333333333" ]
  run env AWS_PROFILE=prodprofile aws configure get region < /dev/null
  [ "$output" = "eu-west-1" ]

  run aws sts get-caller-identity --query Account --output text < /dev/null
  [ "$output" = "123456789012" ]
  run aws configure get region < /dev/null
  [ "$output" = "us-east-1" ]

  run env AWS_PROFILE=unmapped aws sts get-caller-identity --query Account --output text < /dev/null
  [ "$output" = "123456789012" ]
  run env AWS_PROFILE=unmapped aws configure get region < /dev/null
  [ "$output" = "us-east-1" ]
}

@test "stub: names in STUB_IGNORE_STDIN_FOR leave a piped feed unread" {
  stub_tools_ignore_stdin
  run bash -c 'printf "feed-line\n" | { aws sts get-caller-identity >/dev/null; read -r x; echo "read=[$x]"; }'
  [ "$status" -eq 0 ]
  [ "$output" = "read=[feed-line]" ]

  unset STUB_IGNORE_STDIN_FOR
  run bash -c 'printf "feed-line\n" | { aws sts get-caller-identity >/dev/null; read -r x; echo "read=[$x]"; }'
  [ "$status" -eq 0 ]
  [ "$output" = "read=[]" ]
}

@test "fixtures: the trusted-account templates grant what their names say" {
  # Every DDAPortalAccessRole ARN string (12-digit account or *), first
  # appearance first, deduplicated: what each fixture's name promises.
  local d="$PORTAL_TESTS_DIR/fixtures/trusted-accounts" prog
  prog='
import json, re, sys
pattern = re.compile(r"arn:aws:iam::(\d{12}|\*):role/DDAPortalAccessRole")
def strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)
    elif isinstance(node, str):
        yield node
for path in sys.argv[1:]:
    with open(path) as handle:
        document = json.load(handle)
    found = []
    for text in strings(document):
        match = pattern.fullmatch(text)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    print("%s %s" % (path.rsplit("/", 1)[1][:-len(".json")], ",".join(found) or "-"))
'
  run python3 -c "$prog" "$d/one-account.json" "$d/two-accounts.json" \
    "$d/two-accounts-reordered.json" "$d/wildcard-era.json" "$d/no-grants.json" < /dev/null
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 5 ]
  [ "${lines[0]}" = "one-account 123456789012" ]
  [ "${lines[1]}" = "two-accounts 123456789012,222222222222" ]
  [ "${lines[2]}" = "two-accounts-reordered 222222222222,123456789012" ]
  [ "${lines[3]}" = "wildcard-era *" ]
  [ "${lines[4]}" = "no-grants -" ]
  # one-account also carries a longer role name, which must never count.
  grep -qF '"arn:aws:iam::999999999999:role/DDAPortalAccessRoleLegacy"' "$d/one-account.json"
}

@test "harness: portal_scrub_aws_env clears the AWS identity and keeps HOME and TMPDIR in the sandbox" {
  export AWS_PROFILE=prod AWS_ACCESS_KEY_ID=stub-access-key-id AWS_REGION=eu-west-1 \
         TRUSTED_USECASE_ACCOUNT_IDS=123456789012
  portal_scrub_aws_env
  run bash -c 'echo "${AWS_PROFILE-unset} ${AWS_ACCESS_KEY_ID-unset} ${AWS_REGION-unset} ${TRUSTED_USECASE_ACCOUNT_IDS-unset}"'
  [ "$output" = "unset unset unset unset" ]
  [ "$AWS_CONFIG_FILE" = /dev/null ]
  [ "$AWS_SHARED_CREDENTIALS_FILE" = /dev/null ]
  [ "$AWS_EC2_METADATA_DISABLED" = true ]
  [ "$HOME" = "$PORTAL_SANDBOX/home" ]
  [ "$TMPDIR" = "$PORTAL_SANDBOX/tmp" ]
  [ -d "$HOME" ]
  [ -d "$TMPDIR" ]

  run install_real_if_present no/such/file
  [ "$status" -eq 0 ]
  [ ! -e "$PORTAL_TREE/no" ]
}
