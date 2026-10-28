#!/usr/bin/env bash
#
# bats setup helper for the one-command-portal-deploy orchestrator suite.
#
# Provides:
#   * portal_harness_setup / portal_harness_teardown
#       - build a per-test temp sandbox
#       - build a temp bin/ prepended to PATH with PATH-shadow stubs for the
#         five orchestrated scripts and for aws / node / cdk / npx / docker
#       - seed a fake `edge-cv-portal` working tree (with the five script
#         stubs at the exact relative paths the orchestrator invokes)
#   * seed helpers: seed_config_file, set_cfn_output, set_bootstrap_version,
#     set_account_id, set_region, set_deployed_enforcement,
#     fail_lambda_list, stub_output / stub_exit
#   * trace query helpers: stub_calls, stub_called, stub_call_count,
#     stub_pwd_of, stub_args_of, stub_env_of, stub_env_value, stub_stdin_of,
#     stub_order_of
#   * hermetic setup, trusted-account, template and trace helpers (bottom of
#     this file): portal_scrub_aws_env, portal_require_stubbed_tools,
#     install_real_if_present, stub_tools_ignore_stdin, trusted_param_*,
#     portal_not_deployed / deploy_stack_template / deploy_portal_stacks /
#     deny_stack_template / missing_stacks / deny_describe_stacks /
#     set_stack_status, component_bucket_absent / head_bucket_fails /
#     create_bucket_fails / ecr_repositories_exist / dynamodb_tables_exist,
#     set_profile_account / set_profile_region, set_cdk_json_context /
#     set_cdk_context_json /
#     set_user_cdk_json, seed_frontend_stack_outputs, output_has_line,
#     log_has_line / log_line_of and the per-call trace queries
#     (stub_trace_table, stub_call_index, stub_call_count_matching,
#     stub_call_*_n, stub_call_has_pair, stub_env_values_all,
#     stub_regions_used)
#
# The harness is fully offline. Nothing here contacts AWS or the network.

# Absolute path to this helper's directory (tests/).
PORTAL_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORTAL_STUB_SRC="$PORTAL_TESTS_DIR/helpers/stub.sh"
# Real edge-cv-portal directory (parent of tests/), where deploy-portal.sh lives.
PORTAL_REAL_DIR="$(cd "$PORTAL_TESTS_DIR/.." && pwd)"

# Names invoked by the orchestrator.
PORTAL_SCRIPT_STUBS=(
  deploy-account-role.sh
  deploy-infrastructure.sh
  deploy-frontend.sh
  configure-bucket-cors.sh
)
PORTAL_TOOL_STUBS=(aws node cdk npx docker)

# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

# portal_harness_setup: initialise the sandbox for a single test.
portal_harness_setup() {
  # Per-test working area under the bats temp dir.
  PORTAL_SANDBOX="$(mktemp -d "${BATS_TMPDIR:-/tmp}/portal-deploy.XXXXXX")"
  export PORTAL_SANDBOX

  # Where stubs append their invocation trace.
  export STUB_TRACE="$PORTAL_SANDBOX/trace.tsv"
  : > "$STUB_TRACE"

  # Temp bin/ shadowing real tools on PATH.
  export PORTAL_BIN="$PORTAL_SANDBOX/bin"
  mkdir -p "$PORTAL_BIN"

  local name
  for name in "${PORTAL_SCRIPT_STUBS[@]}" deploy-auth.sh "${PORTAL_TOOL_STUBS[@]}"; do
    _install_stub "$PORTAL_BIN/$name"
  done

  # Prepend so our stubs win over any real aws/node/cdk/npx.
  export PATH="$PORTAL_BIN:$PATH"

  # Fake edge-cv-portal working tree.
  seed_portal_tree

  # Deterministic, offline-safe defaults for the AWS stub. Individual tests
  # override these via the seed helpers below.
  set_account_id "123456789012"
  set_region "us-east-1"
  set_bootstrap_version "21"
}

# portal_harness_teardown: remove the sandbox.
portal_harness_teardown() {
  [ -n "${PORTAL_SANDBOX:-}" ] && rm -rf "$PORTAL_SANDBOX"
}

# Install a stub at the given path (symlink to the shared implementation,
# falling back to a copy on filesystems without symlink support).
_install_stub() {
  local dest="$1"
  mkdir -p "$(dirname "$dest")"
  if ln -s "$PORTAL_STUB_SRC" "$dest" 2>/dev/null; then
    :
  else
    cp "$PORTAL_STUB_SRC" "$dest"
  fi
  chmod +x "$PORTAL_STUB_SRC" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Fake working tree
# ---------------------------------------------------------------------------

# seed_portal_tree: create a fake edge-cv-portal tree at $PORTAL_TREE with the
# five orchestrated scripts stubbed at the exact relative paths the
# orchestrator uses (./deploy-account-role.sh from the root,
# infrastructure/deploy-auth.sh from the infrastructure subdir), plus the
# minimal files the standalone scripts assert on (infrastructure/cdk.json,
# frontend/public/).
seed_portal_tree() {
  export PORTAL_TREE="$PORTAL_SANDBOX/edge-cv-portal"
  mkdir -p "$PORTAL_TREE/infrastructure" "$PORTAL_TREE/frontend/public"

  local s
  for s in "${PORTAL_SCRIPT_STUBS[@]}"; do
    _install_stub "$PORTAL_TREE/$s"
  done
  _install_stub "$PORTAL_TREE/infrastructure/deploy-auth.sh"

  # deploy-auth.sh asserts cdk.json exists in its CWD.
  printf '{\n  "app": "npx ts-node bin/app.ts"\n}\n' > "$PORTAL_TREE/infrastructure/cdk.json"
}

# install_deploy_portal: copy the real deploy-portal.sh (task 1.1) into the
# fake tree so a test can execute the orchestrator under stubs. Returns
# non-zero (and skips nothing) if the script does not exist yet.
install_deploy_portal() {
  if [ -f "$PORTAL_REAL_DIR/deploy-portal.sh" ]; then
    cp "$PORTAL_REAL_DIR/deploy-portal.sh" "$PORTAL_TREE/deploy-portal.sh"
    chmod +x "$PORTAL_TREE/deploy-portal.sh"
    return 0
  fi
  return 1
}

# seed_config_file <topology> <account> [key=value ...]
#   Writes a fake usecase-/data-account-<account>-config.txt in the portal
#   tree, mirroring what deploy-account-role.sh emits. Defaults supply
#   PORTAL_ACCOUNT_ID / ROLE_ARN / EXTERNAL_ID; extra key=value args append or
#   override lines.
seed_config_file() {
  local topology="$1" account="$2"; shift 2
  local file
  case "$topology" in
    usecase) file="$PORTAL_TREE/usecase-account-${account}-config.txt" ;;
    data)    file="$PORTAL_TREE/data-account-${account}-config.txt" ;;
    *) echo "seed_config_file: unknown topology '$topology'" >&2; return 1 ;;
  esac

  local role_suffix="DDAPortalAccessRole"
  [ "$topology" = "data" ] && role_suffix="DDAPortalDataAccessRole"

  {
    echo "# DDA Portal - ${topology} Account Configuration (test fixture)"
    echo "PORTAL_ACCOUNT_ID=999999999999"
    echo "ROLE_ARN=arn:aws:iam::${account}:role/${role_suffix}"
    echo "EXTERNAL_ID=stub-external-id-${account}"
  } > "$file"

  local kv
  for kv in "$@"; do
    echo "$kv" >> "$file"
  done
  printf '%s' "$file"
}

# ---------------------------------------------------------------------------
# AWS stub fixtures
# ---------------------------------------------------------------------------

# set_account_id <id>   ("" simulates an empty identity)
set_account_id() { export STUB_AWS_ACCOUNT_ID="$1"; }

# fail_identity [exit] [message] - simulate invalid credentials.
fail_identity() { export STUB_AWS_STS_EXIT="${1:-255}"; export STUB_AWS_STS_STDERR="${2:-Unable to locate credentials}"; }

# set_region <region>   ("" simulates no configured region)
set_region() { export STUB_AWS_CONFIG_REGION="$1"; }

# set_bootstrap_version <version>  (unset var / "" simulates an absent bootstrap)
set_bootstrap_version() { export STUB_BOOTSTRAP_VERSION="$1"; }
unset_bootstrap_version() { unset STUB_BOOTSTRAP_VERSION; }

# set_deployed_enforcement [value ...]
#   The PORTAL_REGISTRY_ENFORCED values the deployed EdgeCVPortal* handlers
#   carry (`aws lambda list-functions`). No values simulates a portal with no
#   handler deployed yet.
set_deployed_enforcement() { export STUB_LAMBDA_ENFORCED_VALUES="$*"; }

# fail_lambda_list [exit] - simulate a failed `aws lambda list-functions`.
fail_lambda_list() { export STUB_AWS_LAMBDA_EXIT="${1:-255}"; }

# set_cfn_output <stack-name> <output-key> <value>
#   Canned `aws cloudformation describe-stacks` output value.
set_cfn_output() {
  local stack key value var
  stack="$1"; key="$2"; value="$3"
  var="STUB_CFN_$(printf '%s' "$stack" | tr -c 'A-Za-z0-9' '_')_${key}"
  export "$var=$value"
}

# ---------------------------------------------------------------------------
# Generic stub fixtures (any stubbed executable)
# ---------------------------------------------------------------------------

# _stub_key <name>  ->  env-fixture key (DEPLOY_ACCOUNT_ROLE_SH, AWS, ...)
_stub_key() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_'; }

# stub_output <name> <stdout> [stderr]
stub_output() {
  local key; key="$(_stub_key "$1")"
  export "STUB_${key}_STDOUT=${2:-}"
  # an if, so that leaving out [stderr] does not return non-zero and fail the test
  if [ "$#" -ge 3 ]; then export "STUB_${key}_STDERR=$3"; fi
}

# stub_exit <name> <code>
stub_exit() {
  local key; key="$(_stub_key "$1")"
  export "STUB_${key}_EXIT=$2"
}

# ---------------------------------------------------------------------------
# Trace query helpers
# ---------------------------------------------------------------------------

_b64d() { printf '%s' "$1" | base64 --decode 2>/dev/null; }

# _first_line_for <name> -> the trace line for the first call of <name>
_first_line_for() { awk -F'\t' -v n="$1" '$1==n {print; exit}' "$STUB_TRACE"; }

# stub_calls -> names of every invocation, in order (one per line)
stub_calls() { cut -f1 "$STUB_TRACE"; }

# stub_called <name> -> success if <name> was invoked at least once
stub_called() { cut -f1 "$STUB_TRACE" | grep -qx "$1"; }

# stub_call_count <name>
stub_call_count() { cut -f1 "$STUB_TRACE" | grep -cx "$1" || true; }

# stub_order_of <name> -> 1-based invocation index of the first call (empty if none)
stub_order_of() { grep -n -m1 -P "^\Q$1\E\t" "$STUB_TRACE" 2>/dev/null | cut -d: -f1; }

# stub_pwd_of <name> -> $PWD recorded at the first call
stub_pwd_of() { _first_line_for "$1" | cut -f2; }

# stub_args_of <name> -> positional args of the first call, one per line
stub_args_of() { _b64d "$(_first_line_for "$1" | cut -f3)"; }

# stub_env_of <name> -> captured "VAR=value" env lines of the first call
stub_env_of() { _b64d "$(_first_line_for "$1" | cut -f4)"; }

# stub_env_value <name> <VAR> -> value of captured env var at the first call
stub_env_value() { stub_env_of "$1" | sed -n "s/^$2=//p"; }

# stub_stdin_of <name> -> stdin the first call received
stub_stdin_of() { _b64d "$(_first_line_for "$1" | cut -f5)"; }

# ---------------------------------------------------------------------------
# Hermetic setup, trusted-account, template and trace helpers
# ---------------------------------------------------------------------------

# portal_scrub_aws_env -> nothing from the caller's shell: no AWS identity or
# credential source, region, trusted list or deploy input; config/credentials
# /dev/null, IMDS off; HOME (so ~/.cdk.json) and TMPDIR inside the sandbox.
# Call after portal_harness_setup; a test then sets only what it needs.
portal_scrub_aws_env() {
  unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY \
        AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_ROLE_ARN AWS_ROLE_SESSION_NAME \
        AWS_WEB_IDENTITY_TOKEN_FILE AWS_CONTAINER_CREDENTIALS_RELATIVE_URI \
        AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_CONTAINER_AUTHORIZATION_TOKEN \
        AWS_REGION AWS_DEFAULT_REGION \
        CDK_DEFAULT_REGION CDK_DEFAULT_ACCOUNT TRUSTED_USECASE_ACCOUNT_IDS \
        PORTAL_TRUSTED_ACCOUNTS_COMPARED PORTAL_REGISTRY_ENFORCED DATA_BUCKET_ALLOWLIST \
        cloudFrontDomain CDK_STACKS PORTAL_DEPLOY_TOPOLOGY PORTAL_DEPLOY_NON_INTERACTIVE \
        PORTAL_DEPLOY_SSO PORTAL_ACCOUNT_ID EXTERNAL_ID DATA_BUCKET_NAMES \
        SSO_METADATA_URL SSO_PROVIDER_NAME COGNITO_DOMAIN_PREFIX \
        PORTAL_DEPLOY_INSTALL_DOCKER CDK_DOCKER
  export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true
  export HOME="$PORTAL_SANDBOX/home" TMPDIR="$PORTAL_SANDBOX/tmp"
  mkdir -p "$HOME" "$TMPDIR"
}

# portal_require_stubbed_tools [name ...] -> fail unless aws, node, cdk, npx,
# docker and every named tool resolve to the stubs in $PORTAL_BIN.
portal_require_stubbed_tools() {
  local t got
  for t in aws node cdk npx docker "$@"; do
    got="$(command -v "$t" 2>/dev/null || true)"
    if [ "$got" != "$PORTAL_BIN/$t" ]; then
      echo "refusing to run: '$t' resolves to '${got:-nothing}', not $PORTAL_BIN/$t" >&2
      return 1
    fi
  done
}

# install_real_if_present <path relative to edge-cv-portal/> -> copy the real
# file into the fake tree (replacing a seeded stub link). A file this checkout
# does not have (an older commit) is skipped: the test then fails on its
# assertions, not in setup.
install_real_if_present() {
  local rel="$1"
  [ -f "$PORTAL_REAL_DIR/$rel" ] || return 0
  mkdir -p "$(dirname "$PORTAL_TREE/$rel")"
  rm -f "$PORTAL_TREE/$rel"
  cp "$PORTAL_REAL_DIR/$rel" "$PORTAL_TREE/$rel"
  chmod +x "$PORTAL_TREE/$rel"
}

# stub_tools_ignore_stdin -> the tool stubs leave stdin unread, so answers piped
# to a real deploy-account-role.sh reach its own prompts.
stub_tools_ignore_stdin() { export STUB_IGNORE_STDIN_FOR="aws npx npm cdk node sleep"; }

# SSM /dda-portal/trusted-usecase-account-ids (state file; a put creates it)
trusted_param_absent() { export STUB_SSM_TRUSTED_FILE="$PORTAL_SANDBOX/ssm-trusted-usecase-account-ids"; rm -f "$STUB_SSM_TRUSTED_FILE"; }
trusted_param_found() { trusted_param_absent; printf '%s' "$1" > "$STUB_SSM_TRUSTED_FILE"; }
trusted_param_read_fails() {
  [ -n "${STUB_SSM_TRUSTED_FILE:-}" ] || trusted_param_absent
  export STUB_SSM_TRUSTED_GET_STDERR="${1:-An error occurred (AccessDeniedException) when calling the GetParameter operation: User: arn:aws:sts::123456789012:assumed-role/stub/stub is not authorized to perform: ssm:GetParameter}"
}
trusted_param_write_fails() {
  [ -n "${STUB_SSM_TRUSTED_FILE:-}" ] || trusted_param_absent
  export STUB_SSM_TRUSTED_PUT_STDERR="${1:-An error occurred (AccessDeniedException) when calling the PutParameter operation: User: arn:aws:sts::123456789012:assumed-role/stub/stub is not authorized to perform: ssm:PutParameter}"
}
trusted_param_value() { cat "$STUB_SSM_TRUSTED_FILE" 2>/dev/null; }

# Deployed stack templates for `cloudformation get-template`
# (fixtures/trusted-accounts/<name>.json). portal_not_deployed: no stack has a
# template, so get-template answers "does not exist".
portal_not_deployed() { export STUB_CFN_TEMPLATES_DIR="$PORTAL_SANDBOX/cfn-templates"; mkdir -p "$STUB_CFN_TEMPLATES_DIR"; }
deploy_stack_template() { portal_not_deployed; cp "$PORTAL_TESTS_DIR/fixtures/trusted-accounts/$2.json" "$STUB_CFN_TEMPLATES_DIR/$1.json"; }
# deploy_portal_stacks <compute> [<nodedesigner>|-] [<syntheticdata>|-]  (defaults: the previous one)
deploy_portal_stacks() {
  local c="$1" n="${2:-$1}" s="${3:-${2:-$1}}"
  deploy_stack_template EdgeCVPortalComputeStack "$c"
  [ "$n" = - ] || deploy_stack_template EdgeCVPortalNodeDesignerStack "$n"
  [ "$s" = - ] || deploy_stack_template EdgeCVPortalSyntheticDataStack "$s"
}
deny_stack_template() { portal_not_deployed; : > "$STUB_CFN_TEMPLATES_DIR/$1.denied"; }
missing_stacks() { export STUB_CFN_MISSING_STACKS="$*"; }
# deny_describe_stacks <stack ...> -> describe-stacks of these stacks fails with AccessDenied
deny_describe_stacks() { export STUB_CFN_DENIED_STACKS="$*"; }
# set_stack_status <stack> <status> -> describe-stacks --query Stacks[0].StackStatus prints <status>
set_stack_status() { export STUB_CFN_STACK_STATUSES="${STUB_CFN_STACK_STATUSES:+$STUB_CFN_STACK_STATUSES }$1=$2"; }

# The Greengrass component artifact bucket (s3api head-bucket succeeds and
# create-bucket creates it unless one of these is set) and the resources a
# failed deploy can leave behind (none exists unless named here).
component_bucket_absent() { export STUB_S3_HEAD_BUCKET_STDERR="An error occurred (404) when calling the HeadBucket operation: Not Found"; }
head_bucket_fails() { export STUB_S3_HEAD_BUCKET_STDERR="$1"; }
create_bucket_fails() { export STUB_S3_CREATE_BUCKET_STDERR="$1"; }
ecr_repositories_exist() { export STUB_ECR_REPOSITORIES="$*"; }
dynamodb_tables_exist() { export STUB_DYNAMODB_TABLES="$*"; }

# Per-profile identity: `aws sts` / `aws configure get region` under AWS_PROFILE=<profile>
set_profile_account() { export STUB_AWS_PROFILE_ACCOUNTS="${STUB_AWS_PROFILE_ACCOUNTS:+$STUB_AWS_PROFILE_ACCOUNTS }$1=$2"; }
set_profile_region() { export STUB_AWS_PROFILE_REGIONS="${STUB_AWS_PROFILE_REGIONS:+$STUB_AWS_PROFILE_REGIONS }$1=$2"; }

# CDK context files the helper reads (fake tree / sandbox HOME)
set_cdk_json_context() { printf '{\n  "app": "npx ts-node bin/app.ts",\n  "context": %s\n}\n' "$1" > "$PORTAL_TREE/infrastructure/cdk.json"; }
set_cdk_context_json() { printf '%s\n' "$1" > "$PORTAL_TREE/infrastructure/cdk.context.json"; }
set_user_cdk_json() { printf '%s\n' "$1" > "$HOME/.cdk.json"; }

# The stack outputs deploy-frontend.sh and the Deployment Summary read.
seed_frontend_stack_outputs() {
  set_cfn_output EdgeCVPortalFrontendStack FrontendBucketName portal-frontend-bucket
  set_cfn_output EdgeCVPortalFrontendStack DistributionId EDISTRIBUTION1
  set_cfn_output EdgeCVPortalFrontendStack DistributionDomainName d1.cloudfront.net
  set_cfn_output EdgeCVPortalAuthStack AuthConfig '{"userPoolId":"pool","userPoolWebClientId":"client1","region":"eu-west-1"}'
  set_cfn_output EdgeCVPortalComputeStack ApiUrl https://api.example.invalid/v1/
}

# Exact compare-line pieces (scripts/portal-trusted-accounts.sh), used by the
# .bats files that load this helper.
# shellcheck disable=SC2034
PT_WARN='⚠️'
# shellcheck disable=SC2034
PT_ADDS_NOTE=' (the portal may then assume their DDAPortalAccessRole)'

# output_has_line <text> -> one line of the last `run` output is exactly <text>
# shellcheck disable=SC2154  # lines: set by bats' run
output_has_line() { local l; for l in "${lines[@]}"; do [ "$l" = "$1" ] && return 0; done; return 1; }
# log_has_line <text> / log_line_of <fixed text>   ($PORTAL_SANDBOX/deploy.log)
log_has_line() { grep -qxF -- "$1" "$PORTAL_SANDBOX/deploy.log"; }
log_line_of() { grep -nF -m1 -- "$1" "$PORTAL_SANDBOX/deploy.log" | cut -d: -f1; }

# stub_trace_table -> per call: n TAB name TAB pwd TAB args (space-joined) TAB env (space-joined)
# (fields split by parameter expansion: `IFS=$'\t' read` would merge an empty
# args or env field into the next one, tab being IFS whitespace)
stub_trace_table() {
  local n=0 line name pwd a e rest
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    name="${line%%$'\t'*}"; rest="${line#*$'\t'}"
    pwd="${rest%%$'\t'*}"; rest="${rest#*$'\t'}"
    a="${rest%%$'\t'*}"; rest="${rest#*$'\t'}"
    e="${rest%%$'\t'*}"
    printf '%s\t%s\t%s\t%s\t%s\n' "$n" "$name" "$pwd" \
      "$(_b64d "$a" | tr '\n\t' '  ' | sed 's/ $//')" "$(_b64d "$e" | tr '\n\t' '  ' | sed 's/ $//')"
  done < "$STUB_TRACE"
}
# stub_call_index <name> <ERE on the space-joined args> -> index of the first match ("" if none)
stub_call_index() {
  stub_trace_table | STUB_Q_NAME="$1" STUB_Q_RE="$2" awk -F'\t' \
    '!found && $2 == ENVIRON["STUB_Q_NAME"] && $4 ~ ENVIRON["STUB_Q_RE"] { print $1; found = 1 }'
}
# stub_call_count_matching <name> <ERE on the space-joined args> -> number of matching calls
stub_call_count_matching() {
  stub_trace_table | STUB_Q_NAME="$1" STUB_Q_RE="$2" awk -F'\t' \
    '$2 == ENVIRON["STUB_Q_NAME"] && $4 ~ ENVIRON["STUB_Q_RE"] { c++ } END { print c + 0 }'
}
# stub_call_args_n / stub_call_env_n / stub_call_env_value_n / stub_call_pwd_n <n> ...
#   -> call n's args (one per line), "VAR=value" env lines, one env value, $PWD
stub_call_args_n() { _b64d "$(sed -n "${1}p" "$STUB_TRACE" | cut -f3)"; }
stub_call_env_n() { _b64d "$(sed -n "${1}p" "$STUB_TRACE" | cut -f4)"; }
stub_call_env_value_n() { stub_call_env_n "$1" | sed -n "s/^$2=//p"; }
stub_call_pwd_n() { sed -n "${1}p" "$STUB_TRACE" | cut -f2; }
# stub_call_stdin_n <n> -> the stdin call n received
stub_call_stdin_n() { _b64d "$(sed -n "${1}p" "$STUB_TRACE" | cut -f5)"; }
# stub_call_has_pair <n> <option> <value> -> call n's argv has <option> then <value>
# (the decoded argv has no trailing newline, so the last arg is read too)
stub_call_has_pair() {
  local prev="" a
  while IFS= read -r a || [ -n "$a" ]; do
    if [ "$prev" = "$2" ] && [ "$a" = "$3" ]; then return 0; fi
    prev="$a"
  done < <(stub_call_args_n "$1")
  return 1
}
# stub_env_values_all <name> <VAR> -> per call of <name>: its value of VAR, or <unset>
stub_env_values_all() {
  local n
  while IFS= read -r n; do
    if stub_call_env_n "$n" | grep -q "^$2="; then stub_call_env_value_n "$n" "$2"; else echo "<unset>"; fi
  done < <(awk -F'\t' -v want="$1" '$1 == want { print NR }' "$STUB_TRACE")
}
# stub_regions_used -> "<n> <name> <region>" for every regional aws/npx/cdk call
# (aws configure/sts/iam skipped): an explicit --region; the env region the CLI
# would use (AWS_REGION > AWS_DEFAULT_REGION > the stub's configured region,
# else NONE), for aws only without --region; and any aws://<acct>/<region>.
stub_regions_used() {
  stub_trace_table | awk -F'\t' -v cfg="${STUB_AWS_CONFIG_REGION:-}" '
    $2 != "aws" && $2 != "npx" && $2 != "cdk" { next }
    $2 == "aws" && $4 ~ /^(configure|sts|iam)( |$)/ { next }
    {
      explicit = ""
      if (match($4, /--region [^ ]+/)) explicit = substr($4, RSTART + 9, RLENGTH - 9)
      if (match($5, /(^| )AWS_REGION=[^ ]+/)) { s = substr($5, RSTART, RLENGTH); sub(/^ /, "", s); envr = substr(s, 12) }
      else if (match($5, /(^| )AWS_DEFAULT_REGION=[^ ]+/)) { s = substr($5, RSTART, RLENGTH); sub(/^ /, "", s); envr = substr(s, 20) }
      else envr = (cfg == "" ? "NONE" : cfg)
      if (explicit != "") print $1, $2, explicit
      if ($2 != "aws" || explicit == "") print $1, $2, envr
      t = $4
      while (match(t, /aws:\/\/[0-9]+\/[^ ]+/)) { r = substr(t, RSTART, RLENGTH); sub(/^aws:\/\/[0-9]+\//, "", r); print $1, $2, r; t = substr(t, RSTART + RLENGTH) }
    }'
}
