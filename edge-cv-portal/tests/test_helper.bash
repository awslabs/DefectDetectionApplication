#!/usr/bin/env bash
#
# bats setup helper for the one-command-portal-deploy orchestrator suite.
#
# Provides:
#   * portal_harness_setup / portal_harness_teardown
#       - build a per-test temp sandbox
#       - build a temp bin/ prepended to PATH with PATH-shadow stubs for the
#         five orchestrated scripts and for aws / node / cdk / npx
#       - seed a fake `edge-cv-portal` working tree (with the five script
#         stubs at the exact relative paths the orchestrator invokes)
#   * seed helpers: seed_config_file, set_cfn_output, set_bootstrap_version,
#     set_account_id, set_region, stub_output / stub_exit
#   * trace query helpers: stub_calls, stub_called, stub_call_count,
#     stub_pwd_of, stub_args_of, stub_env_of, stub_env_value, stub_stdin_of,
#     stub_order_of
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
PORTAL_TOOL_STUBS=(aws node cdk npx)

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
  [ "$#" -ge 3 ] && export "STUB_${key}_STDERR=$3"
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
