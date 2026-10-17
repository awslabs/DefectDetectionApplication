#!/usr/bin/env bats
#
# scripts/portal-trusted-accounts.sh on its own: how the trusted UseCase
# account list is resolved (CDK context > TRUSTED_USECASE_ACCOUNT_IDS > SSM
# /dda-portal/trusted-usecase-account-ids > the list the deployed portal
# stacks grant > none), how it fails closed, how it is compared with the
# deployed stacks, and how the first-deploy value is stored (create-only).
#
# Hermetic: the AWS environment is scrubbed in setup, and aws/node/cdk/npx
# must resolve to the stubs. Stack templates are the hand-written fixtures in
# fixtures/trusted-accounts/.

bats_require_minimum_version 1.5.0

load test_helper

HELPER_REL="scripts/portal-trusted-accounts.sh"
STACKS="EdgeCVPortalComputeStack EdgeCVPortalNodeDesignerStack EdgeCVPortalSyntheticDataStack"

setup() {
  portal_harness_setup
  portal_scrub_aws_env
  portal_require_stubbed_tools
  install_real_if_present "$HELPER_REL"
  trusted_param_absent
  portal_not_deployed
}
teardown() { portal_harness_teardown; }

# pt <function> [args...] -> call a helper function the way the deploy scripts
# source it (under set -euo pipefail), from the copy in the fake tree.
pt() {
  bash -c 'set -euo pipefail; . "$1"; shift; "$@"' _ "$PORTAL_TREE/$HELPER_REL" "$@"
}

# ---------------------------------------------------------------------------
# resolve: sources and their order
# ---------------------------------------------------------------------------

@test "resolve: TRUSTED_USECASE_ACCOUNT_IDS is used when no context file names the key" {
  export TRUSTED_USECASE_ACCOUNT_IDS=123456789012
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "env 123456789012" ]
}

@test "resolve: env entries are trimmed, and empty and repeated entries dropped" {
  export TRUSTED_USECASE_ACCOUNT_IDS=' 123456789012 , 222222222222,,123456789012'
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "env 123456789012,222222222222" ]
}

@test "resolve: a blank TRUSTED_USECASE_ACCOUNT_IDS falls through to the SSM parameter" {
  export TRUSTED_USECASE_ACCOUNT_IDS='   '
  trusted_param_found 333333333333
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "ssm 333333333333" ]
}

@test "resolve: the SSM parameter wins over the deployed stacks" {
  trusted_param_found 333333333333
  deploy_portal_stacks two-accounts
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "ssm 333333333333" ]
  [ -z "$(stub_call_index aws '^cloudformation get-template')" ]
}

@test "resolve: with nothing configured, the list the deployed ComputeStack grants, in template order" {
  deploy_portal_stacks two-accounts two-accounts-reordered -
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "deployed 123456789012,222222222222" ]
}

@test "resolve: the read-back ignores a longer role name" {
  deploy_portal_stacks one-account - -
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "deployed 123456789012" ]
}

@test "resolve: nothing configured and no EdgeCVPortalComputeStack is none" {
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "none" ]
}

@test "resolve: infrastructure/cdk.context.json wins over TRUSTED_USECASE_ACCOUNT_IDS" {
  set_cdk_context_json '{"trustedUseCaseAccountIds": "444444444444"}'
  export TRUSTED_USECASE_ACCOUNT_IDS=555555555555
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "context:infrastructure/cdk.context.json 444444444444" ]
}

@test "resolve: infrastructure/cdk.json context wins over cdk.context.json" {
  set_cdk_json_context '{"trustedUseCaseAccountIds": "777777777777"}'
  set_cdk_context_json '{"trustedUseCaseAccountIds": "444444444444"}'
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "context:infrastructure/cdk.json 777777777777" ]
}

@test "resolve: ~/.cdk.json context is read last and still wins over env" {
  set_user_cdk_json '{"context": {"trustedUseCaseAccountIds": "666666666666"}}'
  export TRUSTED_USECASE_ACCOUNT_IDS=555555555555
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "context:~/.cdk.json 666666666666" ]
}

@test "resolve: an empty value in cdk.json hides cdk.context.json, so env wins (leftmost bag)" {
  set_cdk_json_context '{"trustedUseCaseAccountIds": ""}'
  set_cdk_context_json '{"trustedUseCaseAccountIds": "444444444444"}'
  export TRUSTED_USECASE_ACCOUNT_IDS=555555555555
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "env 555555555555" ]
}

@test "resolve: false, null, 0 and \"\" context values count as unset" {
  local value
  export TRUSTED_USECASE_ACCOUNT_IDS=555555555555
  for value in false null 0 '""'; do
    set_cdk_context_json "{\"trustedUseCaseAccountIds\": $value}"
    run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
    [ "$status" -eq 0 ]
    [ "$output" = "env 555555555555" ]
  done
}

@test "resolve: a list value is joined like String(value)" {
  set_cdk_context_json '{"trustedUseCaseAccountIds": ["123456789012", 222222222222]}'
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "context:infrastructure/cdk.context.json 123456789012,222222222222" ]
}

# ---------------------------------------------------------------------------
# resolve: fail closed (rc 1, nothing on stdout, the reason on stderr)
# ---------------------------------------------------------------------------

@test "resolve: an 11-digit entry stops and names it" {
  export TRUSTED_USECASE_ACCOUNT_IDS=123456789012,11111111111
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"TRUSTED_USECASE_ACCOUNT_IDS is not a comma-separated list of 12-digit account IDs: '11111111111'."* ]]
}

@test "resolve: a malformed SSM value stops and names it" {
  trusted_param_found 123456789012,not-an-id
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"SSM parameter /dda-portal/trusted-usecase-account-ids is not a comma-separated list of 12-digit account IDs: 'not-an-id'."* ]]
}

@test "resolve: a context value of true, [] or an object stops" {
  local value
  for value in true '[]' '{"id": "123456789012"}'; do
    set_cdk_context_json "{\"trustedUseCaseAccountIds\": $value}"
    run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    [[ "$stderr" == *"infrastructure/cdk.context.json"* ]]
  done
}

@test "resolve: invalid JSON in a context file that names the key stops" {
  set_cdk_context_json '{"trustedUseCaseAccountIds": "444444444444",}'
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"not valid JSON"* ]]
}

@test "resolve: an SSM read error other than ParameterNotFound stops before any template read" {
  trusted_param_read_fails
  deploy_portal_stacks one-account
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"Could not read SSM parameter /dda-portal/trusted-usecase-account-ids in eu-west-1: An error occurred (AccessDeniedException)"* ]]
  [ -z "$(stub_call_index aws '^cloudformation get-template')" ]
}

@test "resolve: an unreadable ComputeStack template stops with the commands that set the list" {
  deny_stack_template EdgeCVPortalComputeStack
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"Could not read the deployed EdgeCVPortalComputeStack template in eu-west-1"* ]]
  [[ "$stderr" == *"export TRUSTED_USECASE_ACCOUNT_IDS="* ]]
  [[ "$stderr" == *"aws ssm put-parameter --name /dda-portal/trusted-usecase-account-ids --type String --value <account-id>[,<account-id>...] --region eu-west-1"* ]]
}

@test "resolve: a wildcard-era ComputeStack stops" {
  deploy_portal_stacks wildcard-era
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"pre-scoping grant on arn:aws:iam::*:role/DDAPortalAccessRole"* ]]
}

@test "resolve: a ComputeStack with no account-scoped grant stops" {
  deploy_portal_stacks no-grants
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"grants sts:AssumeRole on no account-scoped DDAPortalAccessRole"* ]]
}

@test "resolve: a NodeDesigner stack that grants a different set stops and names both lists" {
  deploy_portal_stacks one-account two-accounts -
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"The deployed portal stacks grant different trusted lists: EdgeCVPortalComputeStack 123456789012; EdgeCVPortalNodeDesignerStack 123456789012,222222222222."* ]]
}

@test "resolve: an unreadable SyntheticData template stops" {
  deploy_portal_stacks one-account - -
  deny_stack_template EdgeCVPortalSyntheticDataStack
  run --separate-stderr pt portal_trusted_accounts_resolve eu-west-1 < /dev/null
  [ "$status" -eq 1 ]
  [ -z "$output" ]
  [[ "$stderr" == *"EdgeCVPortalSyntheticDataStack template"* ]]
}

# ---------------------------------------------------------------------------
# compare: never fails; every removal and every addition is flagged
# ---------------------------------------------------------------------------

@test "compare: the same set matches the deployed stacks" {
  deploy_portal_stacks one-account
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "  matches the deployed stacks" ]
}

@test "compare: a dropped account is flagged as removed" {
  deploy_portal_stacks two-accounts - -
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "  ${PT_WARN}  EdgeCVPortalComputeStack: removes 222222222222" ]
}

@test "compare: an added account is flagged like a removal and says what it grants" {
  local stack i=0
  deploy_portal_stacks one-account
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 123456789012,222222222222 < /dev/null
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 3 ]
  for stack in $STACKS; do
    [ "${lines[$i]}" = "  ${PT_WARN}  $stack: adds 222222222222${PT_ADDS_NOTE}" ]
    i=$((i + 1))
  done
}

@test "compare: a change that removes and adds flags both on one line" {
  deploy_portal_stacks one-account - -
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 222222222222 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "  ${PT_WARN}  EdgeCVPortalComputeStack: removes 123456789012; adds 222222222222${PT_ADDS_NOTE}" ]
}

@test "compare: prints nothing when EdgeCVPortalComputeStack is not deployed" {
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "compare: an unreadable template is not compared and never fails" {
  deploy_portal_stacks one-account - -
  deny_stack_template EdgeCVPortalNodeDesignerStack
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "  EdgeCVPortalNodeDesignerStack: not compared (its template could not be read)" ]
}

@test "compare: a ComputeStack without an account-scoped grant is not compared" {
  deploy_portal_stacks no-grants - -
  run --separate-stderr pt portal_trusted_accounts_compare eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "  EdgeCVPortalComputeStack: not compared (no account-scoped DDAPortalAccessRole grant deployed)" ]
}

# ---------------------------------------------------------------------------
# store: create-only
# ---------------------------------------------------------------------------

@test "store: creates the parameter as a String, without --overwrite" {
  local put
  run --separate-stderr pt portal_trusted_accounts_store eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 0 ]
  [ "$(trusted_param_value)" = "123456789012" ]
  put="$(stub_call_index aws '^ssm put-parameter')"
  [ -n "$put" ]
  stub_call_has_pair "$put" --name /dda-portal/trusted-usecase-account-ids
  stub_call_has_pair "$put" --type String
  stub_call_has_pair "$put" --value 123456789012
  stub_call_has_pair "$put" --region eu-west-1
  [ "$(stub_call_args_n "$put" | grep -cx -- --overwrite)" = 0 ]
}

@test "store: an existing parameter is never overwritten (rc 2)" {
  trusted_param_found 222222222222
  run --separate-stderr pt portal_trusted_accounts_store eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 2 ]
  [ "$(trusted_param_value)" = "222222222222" ]
}

@test "store: any other failure is rc 1 with the AWS error" {
  trusted_param_write_fails
  run --separate-stderr pt portal_trusted_accounts_store eu-west-1 123456789012 < /dev/null
  [ "$status" -eq 1 ]
  [[ "$stderr" == *"Could not store SSM parameter /dda-portal/trusted-usecase-account-ids in eu-west-1: An error occurred (AccessDeniedException)"* ]]
  [ -z "$(trusted_param_value)" ]
}

# ---------------------------------------------------------------------------
# Sourcing and labels
# ---------------------------------------------------------------------------

@test "the helper is safe under set -euo pipefail, set -e and set -uo pipefail" {
  local mode
  for mode in "-euo pipefail" "-e" "-uo pipefail"; do
    trusted_param_absent
    run bash -c "set $mode"'
      . "$1"
      portal_trusted_accounts_resolve eu-west-1
      portal_trusted_accounts_compare eu-west-1 123456789012
      portal_trusted_accounts_store eu-west-1 123456789012
      if ! TRUSTED_USECASE_ACCOUNT_IDS=bad portal_trusted_accounts_resolve eu-west-1 2>/dev/null; then echo caught; fi
      echo continued' _ "$PORTAL_TREE/$HELPER_REL" < /dev/null
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 3 ]
    [ "${lines[0]}" = "none" ]
    [ "${lines[1]}" = "caught" ]
    [ "${lines[2]}" = "continued" ]
    [ "$(trusted_param_value)" = "123456789012" ]
  done
}

@test "sourcing makes no AWS call and works without an infrastructure/ directory" {
  run bash -c 'set -euo pipefail; . "$1"; echo sourced' _ "$PORTAL_TREE/$HELPER_REL" < /dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "sourced" ]
  [ ! -s "$STUB_TRACE" ]

  mkdir -p "$PORTAL_SANDBOX/lone/scripts"
  cp "$PORTAL_TREE/$HELPER_REL" "$PORTAL_SANDBOX/lone/scripts/"
  run bash -c 'set -euo pipefail; . "$1"; printf "[%s]\n" "$PORTAL_TRUSTED_INFRA_DIR"
    TRUSTED_USECASE_ACCOUNT_IDS=123456789012 portal_trusted_accounts_resolve eu-west-1' \
    _ "$PORTAL_SANDBOX/lone/scripts/portal-trusted-accounts.sh" < /dev/null
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "[]" ]
  [ "${lines[1]}" = "env 123456789012" ]
}

@test "labels name every source" {
  run bash -c '. "$1"
    for source in context:infrastructure/cdk.json env ssm deployed default fresh-default; do
      portal_trusted_accounts_label "$source"; echo
    done' _ "$PORTAL_TREE/$HELPER_REL" < /dev/null
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 6 ]
  [ "${lines[0]}" = "CDK context trustedUseCaseAccountIds in infrastructure/cdk.json" ]
  [ "${lines[1]}" = "TRUSTED_USECASE_ACCOUNT_IDS" ]
  [ "${lines[2]}" = "SSM parameter /dda-portal/trusted-usecase-account-ids" ]
  [ "${lines[3]}" = "the deployed portal stacks (nothing configured)" ]
  [ "${lines[4]}" = "first deploy: the deploying account" ]
  [ "${lines[5]}" = "fresh install: the deploying account" ]
}
