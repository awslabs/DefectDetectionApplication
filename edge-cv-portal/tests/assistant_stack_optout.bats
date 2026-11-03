#!/usr/bin/env bats
#
# Two-step opt-out of the Portal assistant stack
# (scripts/portal-assistant-stack.sh, portal-ai-assistant design D8).
#
# `cdk deploy --all` never deletes a stack that merely left the app, and the
# producing stacks cannot drop the exports a deployed EdgeCVPortalAssistantStack
# imports, so DEPLOY_ASSISTANT=false must be refused while that stack exists
# (the operator destroys it first), must fail rather than guess when its
# existence cannot be read, and only an exact `false` (trimmed, any case)
# turns the stack off.

bats_require_minimum_version 1.5.0

load test_helper

HELPER_REL="scripts/portal-assistant-stack.sh"
STACK="EdgeCVPortalAssistantStack"

setup() {
  portal_harness_setup
  unset DEPLOY_ASSISTANT
  _install_stub "$PORTAL_BIN/npm"
}

teardown() { portal_harness_teardown; }

# guard <value> -> source the helper and run the guard; prints CDK_CONTEXT_ARGS
# on stdout after the guard's own lines.
guard() {
  bash -c '. "$1"; CDK_CONTEXT_ARGS="-c cloudFrontDomain=d1"; portal_assistant_optout_guard us-east-1 "$2" || exit $?; echo "ARGS=$CDK_CONTEXT_ARGS"' _ \
    "$PORTAL_REAL_DIR/$HELPER_REL" "$1"
}

install_real() {
  local rel="$1"
  mkdir -p "$(dirname "$PORTAL_TREE/$rel")"
  rm -f "$PORTAL_TREE/$rel"
  cp "$PORTAL_REAL_DIR/$rel" "$PORTAL_TREE/$rel"
  chmod +x "$PORTAL_TREE/$rel"
}

# (1) off while the stack is deployed
@test "DEPLOY_ASSISTANT=false with the assistant stack deployed is refused with the destroy instruction" {
  run --separate-stderr guard false
  [ "$status" -eq 1 ]
  [[ "$stderr" == *"npx cdk destroy EdgeCVPortalAssistantStack --force"* ]]
  # the printed step 1 synthesizes: the deploy's region and resolved context
  [[ "$stderr" == *"AWS_REGION=us-east-1 npx cdk destroy EdgeCVPortalAssistantStack --force -c cloudFrontDomain=d1"* ]]
  [[ "$output" != *"deployAssistant"* ]]
  [[ "$stderr" != *"deployAssistant"* ]]
  stub_called aws
}

# (2) off once the stack is gone
@test "DEPLOY_ASSISTANT=false with the assistant stack absent appends -c deployAssistant=false" {
  missing_stacks "$STACK"
  run --separate-stderr guard false
  [ "$status" -eq 0 ]
  [[ "$output" == *"ARGS=-c cloudFrontDomain=d1 -c deployAssistant=false"* ]]
}

@test "a padded, upper-case FALSE is off" {
  missing_stacks "$STACK"
  run --separate-stderr guard " FALSE "
  [ "$status" -eq 0 ]
  [[ "$output" == *"-c deployAssistant=false"* ]]
}

# (3) every other value leaves the CDK default, with or without the stack
@test "unset, empty, true, 0, no and off add no context and make no AWS call" {
  local value
  for value in "" "true" "0" "no" "off" "TRUE" "falsey"; do
    : > "$STUB_TRACE"
    run --separate-stderr guard "$value"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ARGS=-c cloudFrontDomain=d1" ]]
    [[ "$output" != *"deployAssistant"* ]]
    [ "$(stub_call_count aws)" = "0" ]
  done
  missing_stacks "$STACK"
  run --separate-stderr guard "true"
  [ "$status" -eq 0 ]
  [[ "$output" != *"deployAssistant"* ]]
}

# (4) a describe-stacks failure other than "does not exist"
@test "an unreadable stack status stops the deploy instead of guessing" {
  export STUB_CFN_DESCRIBE_EXIT=254
  run --separate-stderr guard false
  [ "$status" -eq 1 ]
  [[ "$stderr" == *"Could not read whether EdgeCVPortalAssistantStack is deployed"* ]]
  [[ "$output" != *"deployAssistant"* ]]
}

# (5) end to end: deploy-infrastructure.sh never reaches cdk deploy in case (1)
@test "deploy-infrastructure.sh stops before cdk deploy while the assistant stack is deployed" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE" && DEPLOY_ASSISTANT=false ./deploy-infrastructure.sh'
  [ "$status" -ne 0 ]
  [[ "$output" == *"npx cdk destroy EdgeCVPortalAssistantStack --force"* ]]
  # bin/app.ts refuses to synthesize without the enforcement context
  local destroy_line
  destroy_line="$(printf '%s\n' "$output" | grep -F 'npx cdk destroy EdgeCVPortalAssistantStack --force')"
  [[ "$destroy_line" == *" -c portalRegistryEnforced=true"* ]]
  [[ "$destroy_line" != *"--force  "* ]]
  [[ "$destroy_line" != *"deployAssistant"* ]]
  ! stub_called npx
}

@test "deploy-infrastructure.sh passes -c deployAssistant=false once the assistant stack is gone" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true
  missing_stacks "$STACK"

  run bash -c 'cd "$PORTAL_TREE" && DEPLOY_ASSISTANT=false ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run stub_args_of npx
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [[ "$output" == *"deployAssistant=false"* ]]
}

@test "deploy-infrastructure.sh without DEPLOY_ASSISTANT deploys the assistant stack (no context)" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-registry-enforcement.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  run stub_args_of npx
  [[ "$output" != *"deployAssistant"* ]]
}
