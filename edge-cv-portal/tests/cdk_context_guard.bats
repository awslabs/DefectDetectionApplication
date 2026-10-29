#!/usr/bin/env bats
#
# Every deploy path that synthesizes the portal app passes the Portal_Identity
# enforcement context (portal-authorization-and-iam-scoping, design I7 D7-A).
#
# bin/app.ts refuses to synthesize when `portalRegistryEnforced` is absent or
# unrecognized and `allowLegacyRoles` is not given, and the CDK CLI
# synthesizes the whole app even when it deploys one named stack. So every
# script under edge-cv-portal/ that runs `cdk deploy|synth|diff` against the
# default app must carry one of the two keys, and the set of such scripts is
# pinned here so it cannot drift. `cdk bootstrap` synthesizes an app too (the
# one cdk.json names, to find the environments to bootstrap), so each
# bootstrap command must itself name an app with -a or carry the enforcement
# context. deploy-portal.sh bootstraps through a command array, from the
# directory it is run in (edge-cv-portal/, which has no cdk.json), so this
# text rule does not see that call. The deploy scripts also log the
# enforcement provenance and the data-plane scoping.
#
# Runs on bats 1.2 and later (no bats_require_minimum_version, no
# --separate-stderr): the host's bats is 1.2.

load test_helper

HELPER_REL="scripts/portal-registry-enforcement.sh"

# The rules, one file at a time, in two passes over it.
#
# Pass 1 collects the context variables: names assigned (NAME=, NAME+=, an
# array assignment continued over lines included) on a non-comment line that
# contains portalRegistryEnforced or allowLegacyRoles.
#
# Pass 2 reads logical commands: backslash-continued lines are joined first;
# comment lines (leading whitespace, then '#') are skipped. Commands and -a are
# looked for outside quoted text, so a message such as
# `echo "Running cdk bootstrap..."` is no command. A command running
# `cdk deploy|synth|diff` with no -a/--app override synthesizes the default
# app, and any non-comment line of the script naming one of the two keys
# guards it ("guarded", else "unguarded"). A `cdk bootstrap` command passes
# only when that command carries -a/--app, one of the keys, or the expansion
# of a context variable ("bootstrap-ok", else "bootstrap-unguarded").
#
# Prints the verdicts of a script that has such commands, nothing otherwise.
GUARD_AWK='
BEGIN { KEY = "portalRegistryEnforced|allowLegacyRoles"; ASSIGN_PREFIX = "^[[:space:]]*((export|local|readonly|declare)[[:space:]]+(-[[:alpha:]]+[[:space:]]+)?)?" }
function unquoted(text,   sq) {
  sq = sprintf("%c", 39)
  gsub(/"[^"]*"/, "\"\"", text)
  gsub(sq "[^" sq "]*" sq, "", text)
  return text
}
function expands(command, name,   rest, i, before, before2, after) {
  rest = command
  while ((i = index(rest, name)) > 0) {
    before = (i > 1) ? substr(rest, i - 1, 1) : ""
    before2 = (i > 2) ? substr(rest, i - 2, 2) : ""
    after = substr(rest, i + length(name), 1)
    if ((before == "$" || before2 == "${") && after !~ /[[:alnum:]_]/) return 1
    rest = substr(rest, i + length(name))
  }
  return 0
}
function carries_context(command,   name) {
  if (command ~ KEY) return 1
  for (name in context_var) if (expands(command, name)) return 1
  return 0
}
function check(command,   bare, app) {
  bare = unquoted(command)
  app = bare ~ /(^|[[:space:]])(-a|--app)([[:space:]=]|$)/
  if (bare ~ /(^|[^[:alnum:]_.-])cdk[[:space:]]+(deploy|synth|diff)([^[:alnum:]_-]|$)/ && !app) synth = 1
  if (bare ~ /(^|[^[:alnum:]_.-])cdk[[:space:]]+bootstrap([^[:alnum:]_-]|$)/) {
    boot = 1
    if (!app && !carries_context(command)) bad_boot = 1
  }
}
NR == FNR {
  if (open != "") { if ($0 ~ KEY) context_var[open] = 1; if ($0 ~ /\)/) open = ""; next }
  if ($0 ~ /^[[:space:]]*#/) next
  if (match($0, ASSIGN_PREFIX "[[:alpha:]_][[:alnum:]_]*[+]?=")) {
    name = substr($0, RSTART, RLENGTH)
    sub(ASSIGN_PREFIX, "", name)
    sub(/[+]?=$/, "", name)
    if ($0 ~ KEY) context_var[name] = 1
    rest = substr($0, RSTART + RLENGTH)
    if (rest ~ /^\(/ && rest !~ /\)/) open = name
  }
  next
}
pending == "" && /^[[:space:]]*#/ { next }
{
  if ($0 ~ KEY) key = 1
  if ($0 ~ /\\$/) { pending = pending substr($0, 1, length($0) - 1) " "; next }
  check(pending $0)
  pending = ""
}
END {
  if (pending != "") check(pending)
  verdict = synth ? (key ? "guarded" : "unguarded") : ""
  if (boot) verdict = verdict (verdict == "" ? "" : " ") (bad_boot ? "bootstrap-unguarded" : "bootstrap-ok")
  if (verdict != "") print verdict
}'

# cdk_synth_scripts <root> -> "<path under root> <verdict...>" for every *.sh
# under <root> (node_modules excluded) that runs one of those commands.
cdk_synth_scripts() {
  local root="$1" file verdict
  while IFS= read -r file; do
    verdict="$(awk "$GUARD_AWK" "$file" "$file")"
    [ -n "$verdict" ] && printf '%s %s\n' "${file#"$root"/}" "$verdict"
  done < <(find "$root" -name node_modules -prune -o -type f -name '*.sh' -print | LC_ALL=C sort)
  return 0
}

setup() {
  portal_harness_setup
  unset PORTAL_REGISTRY_ENFORCED
  # The deploy scripts run npm ci / npm install / npm run build.
  _install_stub "$PORTAL_BIN/npm"
  # deploy-frontend.sh, deploy_portal_fixes.sh and deploy-portal.sh stop before
  # any step without a region and without a trusted UseCase account list
  # (scripts/portal-trusted-accounts.sh); seed both so the enforcement context
  # under test is what decides each outcome.
  export AWS_REGION=us-east-1
  trusted_param_found 123456789012
  install_real_if_present scripts/portal-trusted-accounts.sh
}
teardown() { portal_harness_teardown; }

# install_real <path relative to edge-cv-portal/>: copy a real script into the
# fake tree (the harness seeds some of these paths as links to the stub).
install_real() {
  local rel="$1"
  mkdir -p "$(dirname "$PORTAL_TREE/$rel")"
  rm -f "$PORTAL_TREE/$rel"
  cp "$PORTAL_REAL_DIR/$rel" "$PORTAL_TREE/$rel"
  chmod +x "$PORTAL_TREE/$rel"
}

# npx_args -> the arguments of the first npx call, one per line.
npx_args() { stub_args_of npx; }

# call_index <name> <pwd> <first argument> -> 1-based trace position of the
# first such call (empty when there is none).
call_index() {
  local index=0 name pwd args
  while IFS=$'\t' read -r name pwd args _; do
    index=$((index + 1))
    if [ "$name" = "$1" ] && [ "$pwd" = "$2" ] && [ "$(_b64d "$args" | head -n 1)" = "$3" ]; then
      echo "$index"
      return 0
    fi
  done < "$STUB_TRACE"
  return 0
}

# assert_data_plane_line_after_build: the infrastructure build ran, then the
# scoping was read from infrastructure/ with node (never npx, whose first call
# is the deploy) and logged.
assert_data_plane_line_after_build() {
  local build node
  build="$(call_index npm "$PORTAL_TREE/infrastructure" run)"
  node="$(call_index node "$PORTAL_TREE/infrastructure" -e)"
  [ -n "$build" ]
  [ -n "$node" ]
  [ "$node" -gt "$build" ]
  [ "$(_b64d "$(awk -F'\t' -v i="$node" 'NR == i {print $3}' "$STUB_TRACE")" | sed -n 2p)" = \
    "console.log(require('./lib/context-helpers').DATA_PLANE_SCOPING)" ]
  [[ "$output" == *"Data-plane scoping: tag"* ]]
}

stub_frontend_outputs() {
  set_cfn_output EdgeCVPortalFrontendStack FrontendBucketName portal-frontend-bucket
  set_cfn_output EdgeCVPortalFrontendStack DistributionId EDISTRIBUTION1
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  set_cfn_output EdgeCVPortalAuthStack AuthConfig \
    '{"userPoolId":"us-east-1_pool","userPoolWebClientId":"client1","region":"us-east-1"}'
  set_cfn_output EdgeCVPortalComputeStack ApiUrl https://api.example.invalid/v1/
}

# ---------------------------------------------------------------------------
# The synth-path guard
# ---------------------------------------------------------------------------

@test "exactly the four deploy scripts synthesize the portal app, and each passes the enforcement context" {
  run cdk_synth_scripts "$PORTAL_REAL_DIR"
  [ "$status" -eq 0 ]
  # The four "guarded" scripts are the pinned synth set; deploy-account-role.sh
  # appears for its bootstraps only, which name the account app they deploy.
  [ "${#lines[@]}" -eq 5 ]
  [ "${lines[0]}" = "deploy-account-role.sh bootstrap-ok" ]
  [ "${lines[1]}" = "deploy-frontend.sh guarded" ]
  [ "${lines[2]}" = "deploy-infrastructure.sh guarded" ]
  [ "${lines[3]}" = "infrastructure/deploy-auth.sh guarded bootstrap-ok" ]
  [ "${lines[4]}" = "infrastructure/deploy_portal_fixes.sh guarded" ]
}

@test "the guard joins continuations, skips comments and excludes -a overrides" {
  local fixtures="$PORTAL_SANDBOX/fixtures"
  mkdir -p "$fixtures/sub" "$fixtures/node_modules/pkg"
  # Its only cdk deploy is in a comment.
  printf '#!/bin/bash\n# npx cdk deploy --all\n  # cdk synth\necho done\n' > "$fixtures/comment-only.sh"
  # The key travels in an array spliced into a continued deploy.
  printf '%s\n' '#!/bin/bash' 'ARGS=(' '  -c "portalRegistryEnforced=$VALUE"' ')' \
    'npx cdk deploy SomeStack \' '  --require-approval never \' '  "${ARGS[@]}"' \
    > "$fixtures/sub/array-continuation.sh"
  # The -a override sits on the continuation line.
  printf '%s\n' '#!/bin/bash' 'npx cdk deploy OtherStack \' '    -a "npx ts-node bin/other-app.ts" \' \
    '    --require-approval never' > "$fixtures/app-override.sh"
  # A synth with neither key.
  printf '%s\n' '#!/bin/bash' 'cd infrastructure && npx cdk synth -q' > "$fixtures/unguarded.sh"
  # node_modules is never scanned.
  printf '%s\n' '#!/bin/bash' 'cdk diff' > "$fixtures/node_modules/pkg/vendored.sh"

  run cdk_synth_scripts "$fixtures"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 2 ]
  [ "${lines[0]}" = "sub/array-continuation.sh guarded" ]
  [ "${lines[1]}" = "unguarded.sh unguarded" ]
}

@test "a bootstrap passes only with -a or the enforcement context on the command itself" {
  local fixtures="$PORTAL_SANDBOX/fixtures"
  mkdir -p "$fixtures"
  # -a form, positive: the app override on the continuation line; the quoted
  # message names the command but is none.
  printf '%s\n' '#!/bin/bash' 'echo "Running cdk bootstrap..."' 'npx cdk bootstrap \' \
    '    -a "npx ts-node bin/other-app.ts" \' '    $CDK_CONTEXT' > "$fixtures/boot-app-continued.sh"
  # -a form, negative: the override line is not joined to the bootstrap.
  printf '%s\n' '#!/bin/bash' 'npx cdk bootstrap' '    -a "npx ts-node bin/other-app.ts"' \
    > "$fixtures/boot-app-not-continued.sh"
  # Context form, positive: the bootstrap expands the resolved context array.
  printf '%s\n' '#!/bin/bash' 'if [ -n "$VALUE" ]; then' '  CTX=(-c "portalRegistryEnforced=$VALUE")' \
    'else' '  CTX=(-c "allowLegacyRoles=true")' 'fi' \
    'npx cdk bootstrap --region "$REGION" "${CTX[@]}"' > "$fixtures/boot-context-array.sh"
  # Context form, negative: the script's deploy carries the key, which is
  # enough for the deploy rule, but the bootstrap expands other arguments.
  printf '%s\n' '#!/bin/bash' 'CTX=(-c "portalRegistryEnforced=$VALUE")' 'OTHER=(--region "$REGION")' \
    'npx cdk bootstrap "${OTHER[@]}"' 'npx cdk deploy SomeStack "${CTX[@]}"' \
    > "$fixtures/boot-context-missing.sh"

  run cdk_synth_scripts "$fixtures"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 4 ]
  [ "${lines[0]}" = "boot-app-continued.sh bootstrap-ok" ]
  [ "${lines[1]}" = "boot-app-not-continued.sh bootstrap-unguarded" ]
  [ "${lines[2]}" = "boot-context-array.sh bootstrap-ok" ]
  [ "${lines[3]}" = "boot-context-missing.sh guarded bootstrap-unguarded" ]
}

# ---------------------------------------------------------------------------
# deploy-auth.sh passes the context and logs its provenance
# ---------------------------------------------------------------------------

# not_bootstrapped: `aws cloudformation describe-stacks --stack-name
# CDKToolkit` fails, as it does in a region without the CDK toolkit stack, so
# deploy-auth.sh runs its bootstrap; every other aws call reaches the stub.
not_bootstrapped() {
  local real="$PORTAL_SANDBOX/aws-stub"
  mkdir -p "$real"
  ln -s "$PORTAL_STUB_SRC" "$real/aws" 2>/dev/null || cp "$PORTAL_STUB_SRC" "$real/aws"
  rm -f "$PORTAL_BIN/aws"
  printf '%s\n' '#!/usr/bin/env bash' 'case " $* " in *InputPermissionsBoundary*) ;; *" CDKToolkit "*) exit 255 ;; esac' \
    "exec \"$real/aws\" \"\$@\"" > "$PORTAL_BIN/aws"
  chmod +x "$PORTAL_BIN/aws"
}

# nth_args <name> <n> -> the arguments of the n-th call of <name>, one per line.
nth_args() {
  _b64d "$(awk -F'\t' -v name="$1" -v n="$2" '$1 == name && ++seen == n {print $3; exit}' "$STUB_TRACE")"
}

# assert_bootstrap_then_deploy <region> <context>: deploy-auth.sh bootstrapped
# and then deployed, each with exactly `-c <context>`.
assert_bootstrap_then_deploy() {
  [ "$(stub_call_count npx)" = "2" ]
  run nth_args npx 1
  [ "${#lines[@]}" -eq 6 ]
  [ "${lines[0]} ${lines[1]} ${lines[2]} ${lines[3]}" = "cdk bootstrap --region $1" ]
  [ "${lines[4]}" = "-c" ]
  [ "${lines[5]}" = "$2" ]
  run nth_args npx 2
  [ "${#lines[@]}" -eq 7 ]
  [ "${lines[0]} ${lines[1]} ${lines[2]} ${lines[3]} ${lines[4]}" = \
    "cdk deploy EdgeCVPortalAuthStack --require-approval never" ]
  [ "${lines[5]}" = "-c" ]
  [ "${lines[6]}" = "$2" ]
}

@test "deploy-auth.sh bootstraps a fresh install with exactly its deploy's acknowledgement" {
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement
  not_bootstrapped

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh --region us-east-1'
  [ "$status" -eq 0 ]
  [[ "$output" == *"CDK not bootstrapped in this region. Bootstrapping..."* ]]
  assert_bootstrap_then_deploy us-east-1 allowLegacyRoles=true
}

@test "deploy-portal.sh keeps the auth step's enforcement line in its Deployment_Log" {
  install_deploy_portal
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  local log="$PORTAL_SANDBOX/deploy.log"

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-portal.sh single-account --non-interactive --log-file "$1"' _ "$log"
  [ "$status" -eq 0 ]
  grep -qF "Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)" "$log"
}

@test "deploy-auth.sh bootstraps with the deployed value its deploy keeps" {
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true
  not_bootstrapped

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh --region eu-west-1'
  [ "$status" -eq 0 ]
  assert_bootstrap_then_deploy eu-west-1 portalRegistryEnforced=true
}

@test "deploy-auth.sh on a fresh install acknowledges the CDK default" {
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh --region us-east-1'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)"* ]]
  [[ "$output" != *"Data-plane scoping"* ]]
  run npx_args
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [ "${lines[2]}" = "EdgeCVPortalAuthStack" ]
  [ "${lines[$((${#lines[@]} - 2))]}" = "-c" ]
  [ "${lines[$((${#lines[@]} - 1))]}" = "allowLegacyRoles=true" ]
  [[ "$output" != *"portalRegistryEnforced"* ]]
}

@test "deploy-auth.sh keeps the deployed value" {
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true true

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh --region eu-west-1'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: true (kept from deployed)"* ]]
  run npx_args
  [ "${lines[2]}" = "EdgeCVPortalAuthStack" ]
  [ "${lines[$((${#lines[@]} - 2))]}" = "-c" ]
  [ "${lines[$((${#lines[@]} - 1))]}" = "portalRegistryEnforced=true" ]
  [[ "$output" != *"allowLegacyRoles"* ]]
  # The lookup reads the region the script deploys to.
  run stub_args_of aws
  [ "${lines[0]}" = "lambda" ]
  [ "${lines[3]}" = "eu-west-1" ]
}

@test "deploy-auth.sh passes an explicit PORTAL_REGISTRY_ENFORCED as requested" {
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && PORTAL_REGISTRY_ENFORCED=false ./deploy-auth.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: false (requested)"* ]]
  run npx_args
  [ "${lines[$((${#lines[@]} - 1))]}" = "portalRegistryEnforced=false" ]
}

@test "deploy-auth.sh stops before cdk when the lookup fails" {
  install_real infrastructure/deploy-auth.sh
  install_real "$HELPER_REL"
  fail_lambda_list
  # Not bootstrapped either: neither the bootstrap nor the deploy runs.
  not_bootstrapped

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy-auth.sh'
  [ "$status" -ne 0 ]
  [[ "$output" == *"Could not read the deployed Portal_Identity enforcement value"* ]]
  [ "$(stub_call_count npx)" = "0" ]
}

# ---------------------------------------------------------------------------
# The three building scripts: the acknowledgement and the data-plane line
# ---------------------------------------------------------------------------

@test "deploy-infrastructure.sh acknowledges a fresh install and logs the data-plane scoping" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-mcp-stack.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement
  # The node stub prints the scoping. stub_output gets an explicit empty
  # stderr: with two arguments its last command, the stderr test, fails.
  stub_output node tag ""

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)"* ]]
  assert_data_plane_line_after_build
  run npx_args
  [ "${lines[0]}" = "cdk" ]
  [ "${lines[1]}" = "deploy" ]
  [[ "$output" == *"allowLegacyRoles=true"* ]]
  [[ "$output" != *"portalRegistryEnforced"* ]]
}

@test "deploy-infrastructure.sh logs a value kept from the deployed handlers" {
  install_real deploy-infrastructure.sh
  install_real scripts/portal-mcp-stack.sh
  install_real "$HELPER_REL"
  set_deployed_enforcement true

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-infrastructure.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: true (kept from deployed)"* ]]
  run npx_args
  [[ "$output" == *"portalRegistryEnforced=true"* ]]
  [[ "$output" != *"allowLegacyRoles"* ]]
}

@test "deploy-frontend.sh logs the provenance and the data-plane scoping of its compute redeploy" {
  install_real deploy-frontend.sh
  install_real "$HELPER_REL"
  stub_frontend_outputs
  # `aws configure export-credentials` must fail so its output is not eval'd.
  set_region ""
  set_deployed_enforcement true
  stub_output node tag ""

  run bash -c 'cd "$PORTAL_TREE" && PORTAL_REGISTRY_ENFORCED=true ./deploy-frontend.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: true (requested)"* ]]
  assert_data_plane_line_after_build
  run npx_args
  [ "${lines[2]}" = "EdgeCVPortalComputeStack" ]
  [[ "$output" == *"portalRegistryEnforced=true"* ]]
}

@test "deploy-frontend.sh acknowledges a fresh install" {
  install_real deploy-frontend.sh
  install_real "$HELPER_REL"
  stub_frontend_outputs
  set_region ""
  set_deployed_enforcement

  run bash -c 'cd "$PORTAL_TREE" && ./deploy-frontend.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)"* ]]
  run npx_args
  [ "${lines[$((${#lines[@]} - 1))]}" = "allowLegacyRoles=true" ]
}

@test "deploy_portal_fixes.sh builds, logs the data-plane scoping and acknowledges a fresh install" {
  install_real infrastructure/deploy_portal_fixes.sh
  install_real "$HELPER_REL"
  set_region ""
  set_deployed_enforcement
  stub_output node tag ""

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy_portal_fixes.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: CDK default (off), acknowledged (fresh install)"* ]]
  assert_data_plane_line_after_build
  run npx_args
  [[ "$output" == *"allowLegacyRoles=true"* ]]
  [[ "$output" != *"portalRegistryEnforced"* ]]
}

@test "deploy_portal_fixes.sh logs a value kept from the deployed handlers" {
  install_real infrastructure/deploy_portal_fixes.sh
  install_real "$HELPER_REL"
  set_region ""
  set_deployed_enforcement false

  run bash -c 'cd "$PORTAL_TREE/infrastructure" && ./deploy_portal_fixes.sh'
  [ "$status" -eq 0 ]
  [[ "$output" == *"Portal_Identity enforcement: false (kept from deployed)"* ]]
  run npx_args
  [[ "$output" == *"portalRegistryEnforced=false"* ]]
}
