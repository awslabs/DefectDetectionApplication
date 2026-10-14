#!/usr/bin/env bash
#
# deploy-portal.sh — Portal_Deploy_Command
#
# One-command orchestrator for the Edge CV Portal. It invokes the existing
# deployment scripts in the correct order without modifying any of them:
#
#   1. deploy-account-role.sh          (IAM roles / cross-account CDK stacks)
#   2. infrastructure/deploy-auth.sh   (Cognito EdgeCVPortalAuthStack)
#   3. deploy-infrastructure.sh        (main CDK stacks, cdk deploy --all)
#   4. deploy-frontend.sh              (build/publish frontend + compute redeploy)
#
# For the `usecase` and `data` topologies only the account-role step runs.
#
# This file is a SKELETON: function bodies are placeholders that are filled in
# by later tasks. It is intentionally syntactically valid and executable so the
# harness and subsequent tasks build on integrated ground.
#
# Requirements: 1.1

set -euo pipefail

# ---------------------------------------------------------------------------
# Script location
# ---------------------------------------------------------------------------
# PORTAL_DIR is the directory containing this script — the `edge-cv-portal`
# directory. Steps that expect to run from `edge-cv-portal` (account-role,
# infrastructure, frontend) use it as their working directory so their
# relative-path operations resolve exactly as a standalone run's do,
# regardless of the operator's current working directory (Req 8.3).
PORTAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Topology → ordered step map and per-step outcome tracking
# ---------------------------------------------------------------------------
# STEPS_FOR maps a Deployment_Topology to the ordered list of Deployment_Steps
# executed for that topology. `single-account` runs the full four-step flow;
# `usecase` and `data` run only the account-role step (they configure a
# separate account and do not host the portal frontend/API there).
declare -A STEPS_FOR=(
    ["single-account"]="account-role auth infrastructure frontend"
    ["usecase"]="account-role"
    ["data"]="account-role"
)

# STEP_OUTCOMES tracks each step's result as exactly one of:
#   not-run | success | failure
# Every step defined across all topologies is initialized to `not-run`.
declare -A STEP_OUTCOMES=(
    ["account-role"]="not-run"
    ["auth"]="not-run"
    ["infrastructure"]="not-run"
    ["frontend"]="not-run"
)

# ---------------------------------------------------------------------------
# Usage / help
# ---------------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: deploy-portal.sh [TOPOLOGY] [options]

Deploys the Edge CV Portal end to end by orchestrating the existing scripts in
the correct order. Runs interactively by default; supports a non-interactive
mode for CI driven by flags and environment variables.

Positional argument:
  TOPOLOGY                    single-account | usecase | data
                              (env: PORTAL_DEPLOY_TOPOLOGY)

Options:
  --non-interactive           Supply all inputs from flags/env; never prompt
                              (env: PORTAL_DEPLOY_NON_INTERACTIVE)
  --portal-account-id <id>    Portal Account ID
                              (env: PORTAL_ACCOUNT_ID)
  --external-id <id>          External ID for cross-account trust
                              (env: EXTERNAL_ID)
  --data-buckets <csv>        Comma-separated data bucket names (data topology)
                              (env: DATA_BUCKET_NAMES)
  --sso                       Enable SSO for the auth step
                              (env: PORTAL_DEPLOY_SSO)
  --profile <name>            AWS CLI profile to use for all steps
                              (env: AWS_PROFILE)
  --region <region>           AWS region for all steps
                              (env: AWS_REGION / AWS_DEFAULT_REGION)
  --log-file <path>           Deployment_Log path
                              (default: deploy-portal-<UTC-timestamp>.log)
  --help                      Show this help message and exit

Environment passthrough (forwarded unchanged to the frontend step):
  TRUSTED_USECASE_ACCOUNT_IDS, DATA_BUCKET_ALLOWLIST, cloudFrontDomain

SSO environment (forwarded unchanged when --sso is used):
  SSO_METADATA_URL, SSO_PROVIDER_NAME, COGNITO_DOMAIN_PREFIX

Examples:
  deploy-portal.sh                                  # interactive, full deploy
  deploy-portal.sh single-account                   # single-account topology
  deploy-portal.sh usecase --non-interactive \
      --portal-account-id 123456789012 --external-id abc-123

Optional npm alias:
  This script is the canonical entrypoint. There is no portal-root package.json
  today, so no npm alias is defined and none is created here. If a portal-root
  package.json is ever introduced, add a thin passthrough only (no behavior of
  its own):
      "scripts": { "deploy:portal": "./edge-cv-portal/deploy-portal.sh" }
  invoked as `npm run deploy:portal -- <TOPOLOGY> [options]`.
EOF
}

# ---------------------------------------------------------------------------
# Argument + environment resolution
# ---------------------------------------------------------------------------
# resolve_input <flag_value> <env_value> -> echoes the effective value.
# Flag wins over env when non-empty (Req 5.5); otherwise env; else "".
resolve_input() {
    local flag="${1:-}" env="${2:-}"
    if [ -n "$flag" ]; then
        printf '%s' "$flag"
        return
    fi
    printf '%s' "$env"
}

# _is_truthy <value> -> success if the value is a recognized truthy string.
# Treats 1/true/yes/on (case-insensitive) as true; everything else as false.
_is_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# _export_passthrough_env -> export the frontend passthrough env and the SSO env
# UNCHANGED (values preserved exactly) so the frontend/auth steps observe the
# same inputs an operator would set for a standalone run (Req 8.4, 5.6). Only
# variables that are actually set are exported; their values are never altered.
_export_passthrough_env() {
    local v
    for v in TRUSTED_USECASE_ACCOUNT_IDS DATA_BUCKET_ALLOWLIST cloudFrontDomain \
             SSO_METADATA_URL SSO_PROVIDER_NAME COGNITO_DOMAIN_PREFIX; do
        if [ -n "${!v+x}" ]; then
            export "$v"
        fi
    done
}

# parse_args "$@" -> parses the full CLI surface and resolves flag/env values
# with flag-over-env precedence (Req 5.5). On success it sets the resolved run
# context globals and exports the passthrough/SSO env unchanged. Unknown options
# and a duplicate positional argument are rejected with a non-zero return. When
# --help/-h is seen, SHOW_HELP is set to 1 and parsing stops.
#
# Resolved globals:
#   TOPOLOGY           positional | PORTAL_DEPLOY_TOPOLOGY
#   NON_INTERACTIVE    1 if --non-interactive or PORTAL_DEPLOY_NON_INTERACTIVE truthy, else 0
#   PORTAL_ACCOUNT_ID  --portal-account-id | PORTAL_ACCOUNT_ID
#   EXTERNAL_ID        --external-id | EXTERNAL_ID
#   DATA_BUCKETS       --data-buckets | DATA_BUCKET_NAMES
#   SSO                1 if --sso or PORTAL_DEPLOY_SSO truthy, else 0
#   PROFILE            --profile | AWS_PROFILE
#   REGION             --region | AWS_REGION | AWS_DEFAULT_REGION
#   LOG_FILE           --log-file (no env fallback)
#   SHOW_HELP          1 when help was requested, else 0
parse_args() {
    # Flag-supplied values (empty until the corresponding flag is seen).
    local flag_topology="" flag_non_interactive="" flag_portal_account_id=""
    local flag_external_id="" flag_data_buckets="" flag_sso=""
    local flag_profile="" flag_region="" flag_log_file=""
    local topology_seen=0

    SHOW_HELP=0

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --help|-h)
                SHOW_HELP=1
                return 0
                ;;
            --non-interactive)
                flag_non_interactive="1"
                ;;
            --sso)
                flag_sso="1"
                ;;
            --portal-account-id)
                flag_portal_account_id="${2:-}"; shift
                ;;
            --portal-account-id=*)
                flag_portal_account_id="${1#*=}"
                ;;
            --external-id)
                flag_external_id="${2:-}"; shift
                ;;
            --external-id=*)
                flag_external_id="${1#*=}"
                ;;
            --data-buckets)
                flag_data_buckets="${2:-}"; shift
                ;;
            --data-buckets=*)
                flag_data_buckets="${1#*=}"
                ;;
            --profile)
                flag_profile="${2:-}"; shift
                ;;
            --profile=*)
                flag_profile="${1#*=}"
                ;;
            --region)
                flag_region="${2:-}"; shift
                ;;
            --region=*)
                flag_region="${1#*=}"
                ;;
            --log-file)
                flag_log_file="${2:-}"; shift
                ;;
            --log-file=*)
                flag_log_file="${1#*=}"
                ;;
            --)
                # End of options; remaining args are positional.
                shift
                while [ "$#" -gt 0 ]; do
                    if [ "$topology_seen" -eq 1 ]; then
                        echo "deploy-portal.sh: unexpected argument '$1'" >&2
                        return 2
                    fi
                    flag_topology="$1"; topology_seen=1
                    shift
                done
                break
                ;;
            -*)
                echo "deploy-portal.sh: unknown option '$1'" >&2
                usage >&2
                return 2
                ;;
            *)
                if [ "$topology_seen" -eq 1 ]; then
                    echo "deploy-portal.sh: unexpected argument '$1'" >&2
                    return 2
                fi
                flag_topology="$1"; topology_seen=1
                ;;
        esac
        shift
    done

    # Resolve each input with flag-over-env precedence (Req 5.1, 5.5).
    TOPOLOGY="$(resolve_input "$flag_topology" "${PORTAL_DEPLOY_TOPOLOGY:-}")"
    PORTAL_ACCOUNT_ID="$(resolve_input "$flag_portal_account_id" "${PORTAL_ACCOUNT_ID:-}")"
    EXTERNAL_ID="$(resolve_input "$flag_external_id" "${EXTERNAL_ID:-}")"
    DATA_BUCKETS="$(resolve_input "$flag_data_buckets" "${DATA_BUCKET_NAMES:-}")"
    PROFILE="$(resolve_input "$flag_profile" "${AWS_PROFILE:-}")"
    REGION="$(resolve_input "$flag_region" "${AWS_REGION:-${AWS_DEFAULT_REGION:-}}")"
    LOG_FILE="$flag_log_file"

    # Boolean modes: the flag or a truthy env value enables them (flag still
    # takes precedence — either being set turns the mode on).
    NON_INTERACTIVE=0
    if [ -n "$flag_non_interactive" ] || _is_truthy "${PORTAL_DEPLOY_NON_INTERACTIVE:-}"; then
        NON_INTERACTIVE=1
    fi

    SSO=0
    if [ -n "$flag_sso" ] || _is_truthy "${PORTAL_DEPLOY_SSO:-}"; then
        SSO=1
    fi

    # Forward the frontend passthrough env and the SSO env unchanged (Req 8.4, 5.6).
    _export_passthrough_env

    return 0
}

# ---------------------------------------------------------------------------
# Topology validation
# ---------------------------------------------------------------------------
# validate_topology <value> -> success (return 0) if the value is one of the
# supported Deployment_Topologies {single-account, usecase, data}; otherwise
# write a message to stderr naming the invalid value and return non-zero so the
# caller can reject it before any Deployment_Step runs (Req 4.3).
validate_topology() {
    local value="${1:-}"
    case "$value" in
        single-account|usecase|data)
            return 0
            ;;
        *)
            echo "deploy-portal.sh: invalid topology '$value' (expected one of: single-account, usecase, data)" >&2
            return 2
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Resolved AWS account (sts caller-identity), cached
# ---------------------------------------------------------------------------
# _resolve_account -> echoes the AWS account id reported by the read-only
# `aws sts get-caller-identity`, resolving it AT MOST ONCE and caching the
# result for the remainder of the run. A read-only identity lookup is safe to
# perform before any mutating step, so both the non-interactive input gate
# (External-ID reuse reconciliation, which must key on the SAME account
# deploy-account-role.sh uses to name its `*-config.txt`) and check_prerequisites
# consume this single resolved account rather than each issuing their own call
# (Req 2.4, 3.3, 5.3). This is the fix for the reuse mismatch: the config file
# name is keyed on the sts caller-identity account (deploy-account-role.sh's
# CURRENT_ACCOUNT), NOT the operator-supplied PORTAL_ACCOUNT_ID.
#
# A failed / empty / "None" identity is cached and echoed as the empty string;
# callers treat an empty account as "no account resolved" (reuse unavailable at
# the input gate; a fatal credential failure in check_prerequisites).
_ACCOUNT_RESOLVED=0
_ACCOUNT_CACHE=""
_resolve_account() {
    if [ "${_ACCOUNT_RESOLVED:-0}" -ne 1 ]; then
        local account
        account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
        if [ "$account" = "None" ]; then
            account=""
        fi
        _ACCOUNT_CACHE="$account"
        _ACCOUNT_RESOLVED=1
    fi
    printf '%s' "$_ACCOUNT_CACHE"
}

# ---------------------------------------------------------------------------
# Non-interactive input validation + External-ID reuse reconciliation
# ---------------------------------------------------------------------------
# _external_id_reuse_available <topology> -> success (return 0) when the
# Account_Role_Script would resolve the External ID from an existing
# `*-config.txt`, which makes EXTERNAL_ID *not* a required input and means the
# orchestrator must not override it (Req 2.4, 5.3).
#
# It stats the SAME config file the Account_Role_Script reads on a re-deploy:
#   usecase -> usecase-account-<acct>-config.txt
#   data    -> data-account-<acct>-config.txt
# resolved with a bare relative path in the current working directory (the
# `edge-cv-portal` CWD), exactly as deploy-account-role.sh reads it. `<acct>` is
# the sts caller-identity account (deploy-account-role.sh's CURRENT_ACCOUNT),
# resolved once and cached via _resolve_account — NOT the operator-supplied
# PORTAL_ACCOUNT_ID, which names a different (wrong) file and would make a
# reusable config look absent (Req 2.4, 5.3).
#
# Reuse is available (return 0) only when BOTH hold:
#   * no `--external-id`/`EXTERNAL_ID` was supplied — a supplied value always
#     wins, so reuse is declined and the supplied value is used (Req 2.4); and
#   * that config file already exists on disk (the script's reuse path will
#     resolve the External ID from it).
# In every other case reuse is declined (return non-zero), so EXTERNAL_ID stays
# a required input for the usecase/data topologies (Req 5.1, 5.3).
_external_id_reuse_available() {
    local topology="${1:-}"

    # A supplied External ID always wins: decline reuse so the supplied value is
    # used rather than the config file's value (Req 2.4).
    if [ -n "${EXTERNAL_ID:-}" ]; then
        return 1
    fi

    # Only the multi-account topologies write/read a reusable *-config.txt.
    # Key the filename on the sts caller-identity account (CURRENT_ACCOUNT), the
    # same account deploy-account-role.sh uses to name the file (Req 2.4).
    local acct
    acct="$(_resolve_account)"
    local config_file
    case "$topology" in
        usecase) config_file="usecase-account-${acct}-config.txt" ;;
        data)    config_file="data-account-${acct}-config.txt" ;;
        *)       return 1 ;;
    esac

    # Without a resolved account we cannot name the file the script would read,
    # so reuse cannot be assumed available.
    [ -n "$acct" ] || return 1

    # Reuse is available only when the config file already exists in the
    # edge-cv-portal CWD — the same relative path deploy-account-role.sh reads.
    [ -f "$config_file" ]
}

# required_inputs_for <topology> -> echoes, one per line, the names of the
# inputs required in Non_Interactive_Mode for the given topology (Req 5.1). The
# names are the canonical environment-variable names, which also match the
# resolved run-context globals set by parse_args, so check_required_inputs can
# look each one up by name.
#
#   single-account : (none beyond the topology itself)
#   usecase / data : PORTAL_ACCOUNT_ID, and EXTERNAL_ID unless a reusable
#                    config exists (see _external_id_reuse_available, task 4.2).
#                    Data bucket names are optional (auto-config), so they are
#                    never reported as required.
required_inputs_for() {
    local topology="${1:-}"
    case "$topology" in
        single-account)
            : # No inputs required beyond the topology itself.
            ;;
        usecase|data)
            printf '%s\n' "PORTAL_ACCOUNT_ID"
            if ! _external_id_reuse_available "$topology"; then
                printf '%s\n' "EXTERNAL_ID"
            fi
            ;;
        *)
            : # Unknown topology: no required-input names (validate_topology
              # rejects invalid topologies before any step runs, Req 4.3).
            ;;
    esac
}

# check_required_inputs -> in Non_Interactive_Mode, collect EVERY missing or
# empty required input for the selected topology and report them all in a
# single failure message, then return non-zero so the caller runs no mutating
# step and exits non-zero (Req 5.3). In Interactive_Mode this is a no-op: the
# Account_Role_Script collects those inputs by prompting.
#
# Each required-input name maps to the identically named resolved global set by
# parse_args (PORTAL_ACCOUNT_ID, EXTERNAL_ID), so the value is looked up by
# indirect expansion. All missing inputs are aggregated before reporting so the
# operator sees the complete set at once rather than one failure at a time.
check_required_inputs() {
    # Only enforced without a terminal to prompt on (Req 5.2, 5.3).
    if [ "${NON_INTERACTIVE:-0}" -ne 1 ]; then
        return 0
    fi

    local missing=() name value
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        value="${!name:-}"
        if [ -z "$value" ]; then
            missing+=("$name")
        fi
    done < <(required_inputs_for "${TOPOLOGY:-}")

    if [ "${#missing[@]}" -gt 0 ]; then
        echo "deploy-portal.sh: non-interactive mode is missing required input(s) for topology '${TOPOLOGY:-}': ${missing[*]}" >&2
        return 2
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Prerequisite checks + CDK bootstrap
# ---------------------------------------------------------------------------
# check_prerequisites -> run all checks before any mutating step; on the first
# failure write which check failed to stderr, run no step, and return non-zero
# so the caller exits without invoking any Deployment_Step (Req 3.1). On success
# it resolves the run context, exports it for the downstream steps, and prints
# the resolved account id and region to stdout (Req 3.8).
#
# Checks, in order (Req 3.1 — all performed before any mutating step):
#   1. `command -v aws`                                   (Req 3.2)
#   2. `aws sts get-caller-identity` -> non-empty/non-None (Req 3.3)
#   3. region resolved via the precedence chain           (Req 3.4)
#   4. `command -v node` and `cdk` (or `npx cdk`)         (Req 3.5)
#
# On success it sets and exports:
#   ACCOUNT             resolved AWS account id (also CDK_DEFAULT_ACCOUNT)
#   REGION              resolved AWS region (also AWS_REGION/AWS_DEFAULT_REGION/
#                       CDK_DEFAULT_REGION)
#
# NOTE: The CDK bootstrap version check + auto-bootstrap is a separate concern
# handled by task 5.2; this gate resolves and publishes ACCOUNT/REGION so that
# the bootstrap step (and every subsequent CDK-deploying step) can consume them.
check_prerequisites() {
    # 1. AWS CLI must be resolvable on the PATH (Req 3.2).
    if ! command -v aws >/dev/null 2>&1; then
        echo "deploy-portal.sh: AWS CLI not found on PATH" >&2
        return 2
    fi

    # 2. Credentials must resolve to a valid AWS account identifier (Req 3.3).
    #    A missing/None/empty account means the identity check failed. The
    #    account is resolved once and cached (_resolve_account, which maps a
    #    None/empty identity to ""), so the reuse reconciliation at the input
    #    gate and this gate observe the SAME account (Req 2.4, 3.3).
    local account
    account="$(_resolve_account)"
    if [ -z "$account" ]; then
        echo "deploy-portal.sh: AWS credential validation failed — 'aws sts get-caller-identity' did not return a valid account id" >&2
        return 2
    fi

    # 3. Resolve the AWS region (Req 3.4). parse_args already folded the
    #    --region -> AWS_REGION -> AWS_DEFAULT_REGION portion of the chain into
    #    REGION; continue it here with `aws configure get region` and then
    #    CDK_DEFAULT_REGION. An unresolved region is a fatal prerequisite.
    local region="${REGION:-}"
    if [ -z "$region" ]; then
        region="$(aws configure get region 2>/dev/null || true)"
    fi
    if [ -z "$region" ]; then
        region="${CDK_DEFAULT_REGION:-}"
    fi
    if [ -z "$region" ]; then
        echo "deploy-portal.sh: no AWS region resolved (set --region, AWS_REGION, AWS_DEFAULT_REGION, 'aws configure' region, or CDK_DEFAULT_REGION)" >&2
        return 2
    fi

    # 4. Node.js and the CDK command must be resolvable on the PATH (Req 3.5).
    #    CDK is satisfied by a standalone `cdk` or by `npx` (used as `npx cdk`).
    if ! command -v node >/dev/null 2>&1; then
        echo "deploy-portal.sh: Node.js ('node') not found on PATH" >&2
        return 2
    fi
    if ! command -v cdk >/dev/null 2>&1 && ! command -v npx >/dev/null 2>&1; then
        echo "deploy-portal.sh: CDK not found on PATH ('cdk' or 'npx cdk' required)" >&2
        return 2
    fi

    # Success: publish the resolved run context for every downstream step,
    # keeping the region/account exports consistent across AWS and CDK (Req 3.8).
    ACCOUNT="$account"
    REGION="$region"
    export AWS_REGION="$region"
    export AWS_DEFAULT_REGION="$region"
    export CDK_DEFAULT_REGION="$region"
    export CDK_DEFAULT_ACCOUNT="$account"

    # Print the resolved account id and region to stdout before the first
    # mutating step runs (Req 3.8).
    echo "AWS account: $account"
    echo "AWS region:  $region"

    return 0
}

# ---------------------------------------------------------------------------
# CDK bootstrap version check + auto-bootstrap
# ---------------------------------------------------------------------------
# Minimum CDK bootstrap version the orchestrated CDK apps require (matches the
# threshold the existing scripts enforce).
BOOTSTRAP_MIN_VERSION=21

# check_bootstrap -> ensure the target account/region is CDK-bootstrapped at a
# sufficient version BEFORE any CDK-deploying Deployment_Step runs (Req 3.6,
# 3.7). It must run after check_prerequisites has resolved and published the run
# context, so it consumes the ACCOUNT and REGION globals that gate exported.
#
# Behavior:
#   * Reads the bootstrap version from the same SSM parameter the existing
#     scripts use: /cdk-bootstrap/hnb659fds/version.
#   * An absent parameter (the read fails or yields empty/None) is treated as
#     version 0 — i.e. below the threshold, so a bootstrap is required.
#   * A non-numeric version is likewise treated as below the threshold.
#   * When the version is missing or numerically < 21, runs
#     `cdk bootstrap aws://<account>/<region>` (preferring a standalone `cdk`,
#     falling back to `npx cdk`) before any CDK-deploying step.
#   * If the bootstrap invocation exits non-zero, writes "CDK bootstrap failed"
#     to stderr and returns non-zero so the caller runs no CDK-deploy step and
#     exits non-zero (Req 3.7).
#   * When the version is already >= 21, it is a no-op and no bootstrap runs.
check_bootstrap() {
    local account="${ACCOUNT:-}" region="${REGION:-}"

    # Read the current bootstrap version. A failed read / empty / "None" means
    # the bootstrap stack is absent; treat that as version 0 (Req 3.6).
    local version
    version="$(aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version \
        --query 'Parameter.Value' --output text 2>/dev/null || true)"
    if [ -z "$version" ] || [ "$version" = "None" ]; then
        version=0
    fi

    # Compare numerically. A non-numeric value is treated as below threshold so
    # a re-bootstrap corrects it.
    local numeric_version=0
    if printf '%s' "$version" | grep -qE '^[0-9]+$'; then
        numeric_version="$version"
    fi

    # Already bootstrapped at a sufficient version: nothing to do (Req 3.6).
    if [ "$numeric_version" -ge "$BOOTSTRAP_MIN_VERSION" ]; then
        return 0
    fi

    # Below threshold or absent: bootstrap before any CDK-deploying step. Prefer
    # a standalone `cdk`, falling back to `npx cdk` (the prerequisite gate
    # guarantees at least one is resolvable, Req 3.5).
    local -a cdk_cmd
    if command -v cdk >/dev/null 2>&1; then
        cdk_cmd=(cdk)
    else
        cdk_cmd=(npx cdk)
    fi

    echo "CDK bootstrap version '${version}' is below required ${BOOTSTRAP_MIN_VERSION}; running cdk bootstrap for aws://${account}/${region}"
    if ! "${cdk_cmd[@]}" bootstrap "aws://${account}/${region}"; then
        echo "CDK bootstrap failed" >&2
        return 2
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Deployment_Log initialization
# ---------------------------------------------------------------------------
# _LOG_READY guards init_log so the log path is created and announced exactly
# once, before the first Deployment_Step (Req 6.6).
_LOG_READY=0

# init_log -> resolve, create, and announce the Deployment_Log path.
#   * Uses the --log-file override when parse_args set LOG_FILE; otherwise
#     defaults to `deploy-portal-<UTC-timestamp>.log` in the current working
#     directory (the `edge-cv-portal` CWD), matching the design default of
#     `edge-cv-portal/deploy-portal-<UTC-timestamp>.log`.
#   * Resolves the path to absolute so steps that run from a subdirectory
#     (e.g. `infrastructure/` for the auth step) still append to the SAME log
#     rather than creating a second file under their own CWD (Req 6.5, 8.3).
#   * Creates the file without truncating (append-open) so a re-run against an
#     explicit --log-file keeps prior content, and prints the resolved path to
#     stdout before the first step runs (Req 6.6).
init_log() {
    if [ -z "${LOG_FILE:-}" ]; then
        LOG_FILE="deploy-portal-$(date -u +%Y%m%dT%H%M%SZ).log"
    fi

    # Make the log path absolute relative to the current working directory.
    case "$LOG_FILE" in
        /*) : ;;
        *)  LOG_FILE="$PWD/$LOG_FILE" ;;
    esac

    # Create the file if absent without truncating existing content.
    : >> "$LOG_FILE"

    _LOG_READY=1
    echo "Deployment log: $LOG_FILE"
}

# _redact -> a stdin→stdout filter that masks AWS credential secret material so
# it can never reach the Deployment_Log, standard output, or the summary
# (Req 7.4). It masks the VALUE that follows any of the secret keys
#   AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN / aws_secret_access_key /
#   aws_session_token
# when the key is followed by an `=` or `:` separator (optionally wrapped in
# quotes/space, covering both env-assignment and JSON/credentials-file forms).
# The key and separator are preserved; only the secret value is replaced with a
# fixed sentinel. The underlying scripts do not print secrets today, so this is
# defense-in-depth that guarantees exclusion regardless of what a step emits.
_redact() {
    sed -E "s/(AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|aws_secret_access_key|aws_session_token)([[:space:]\"']*[=:][[:space:]\"']*)[^[:space:],\"']+/\1\2***REDACTED***/g"
}

# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------
# run_step <step_name> <cwd> <interactive?> [stdin_feed] -- <command...>
#   - prints the step name to stdout when the step starts (Req 6.4)
#   - runs <command...> from <cwd> so the invoked script's relative-path
#     operations resolve as they do standalone (Req 8.3)
#   - pipes the command's combined stdout+stderr through the secret-redaction
#     filter and `tee -a` into the Deployment_Log, so the captured output is
#     recorded in the log and mirrored (redacted) to stdout (Req 6.5, 7.4)
#   - records the step's outcome in STEP_OUTCOMES as success|failure (Req 6.2)
#   - performs NO rollback or compensating action on failure; it simply returns
#     the command's exit code so the caller can stop the sequence (Req 6.2)
#
# I/O modes:
#   * interactive (`true`/`1`): the command's stdin is left inherited from the
#     terminal so its `read -p` prompts reach and read from the operator, while
#     its stdout+stderr are still streamed through redaction into the log and
#     back to the terminal (Req 2.2). A pipeline only redirects stdout, so the
#     command keeps the terminal on stdin.
#   * non-interactive: no terminal prompting occurs (Req 5.2). When a
#     [stdin_feed] is supplied it is piped in verbatim as the command's stdin;
#     otherwise stdin is /dev/null so a step never blocks waiting for input.
#
# Argument parsing: after <step_name> <cwd> <interactive?>, an optional
# [stdin_feed] may precede the mandatory `--` separator; everything after `--`
# is the command and its arguments.
run_step() {
    local name="$1" cwd="$2" interactive="$3"
    shift 3

    # Optional [stdin_feed] before the mandatory `--` separator.
    local stdin_feed="" has_feed=0
    if [ "${1:-}" = "--" ]; then
        shift
    else
        stdin_feed="${1:-}"; has_feed=1; shift
        [ "${1:-}" = "--" ] && shift
    fi

    # Ensure the Deployment_Log exists and its path is announced before the
    # first step (Req 6.6). Idempotent across steps via the _LOG_READY guard.
    [ "${_LOG_READY:-0}" -eq 1 ] || init_log

    # Announce the step name to stdout when it starts (Req 6.4); also mark the
    # step boundary in the log.
    printf '==> %s\n' "$name"
    printf '==> %s\n' "$name" >> "$LOG_FILE"

    local rc=0
    if [ "$interactive" = "true" ] || [ "$interactive" = "1" ]; then
        # Interactive: preserve the terminal on stdin; stream stdout+stderr
        # through redaction into the log and back to the terminal.
        if ( cd "$cwd" && "$@" ) 2>&1 | _redact | tee -a "$LOG_FILE"; then
            rc=0
        else
            rc=${PIPESTATUS[0]}
        fi
    elif [ "$has_feed" -eq 1 ]; then
        # Non-interactive with a constructed stdin feed.
        if ( cd "$cwd" && "$@" ) < <(printf '%s' "$stdin_feed") 2>&1 \
                | _redact | tee -a "$LOG_FILE"; then
            rc=0
        else
            rc=${PIPESTATUS[0]}
        fi
    else
        # Non-interactive with no feed: never block on input.
        if ( cd "$cwd" && "$@" ) < /dev/null 2>&1 | _redact | tee -a "$LOG_FILE"; then
            rc=0
        else
            rc=${PIPESTATUS[0]}
        fi
    fi

    # Record the outcome; take no rollback/compensating action on failure
    # (Req 6.2). The caller decides whether to stop the sequence.
    if [ "$rc" -eq 0 ]; then
        STEP_OUTCOMES["$name"]="success"
    else
        STEP_OUTCOMES["$name"]="failure"
    fi

    return "$rc"
}

# ---------------------------------------------------------------------------
# Account-role invocation (interactive)
# ---------------------------------------------------------------------------
# run_account_role_interactive [topology] -> invoke the existing
# Account_Role_Script as the account-role Deployment_Step in Interactive_Mode
# (Req 2.1, 2.2, 4.1, 4.2).
#
# It does NOT reimplement any of the script's prompts: it simply invokes
# ./deploy-account-role.sh via run_step in interactive mode. run_step's
# interactive path leaves the terminal on the command's stdin (only stdout+stderr
# are teed through redaction into the log), so the script's own `read -p` prompts
# reach and read from the operator's terminal while the output is still copied,
# redacted, into the Deployment_Log (Req 2.2, 6.5).
#
# Topology handling:
#   * When a topology is supplied (argument or the resolved TOPOLOGY global), it
#     is passed as the script's first POSITIONAL argument — equivalent to the
#     operator picking that menu item, but without going through the menu
#     (Req 4.2). Note the non-interactive constructed-stdin feed is task 8.2.
#   * When no topology is supplied, the script is invoked with NO argument so it
#     presents its own interactive selection menu for the operator to choose the
#     Deployment_Topology (Req 4.1).
#
# The step always runs from PORTAL_DIR (the `edge-cv-portal` directory) so the
# script's relative-path operations resolve as they do standalone (Req 8.3).
run_account_role_interactive() {
    local topology="${1:-${TOPOLOGY:-}}"

    if [ -n "$topology" ]; then
        run_step "account-role" "$PORTAL_DIR" true -- \
            ./deploy-account-role.sh "$topology"
    else
        run_step "account-role" "$PORTAL_DIR" true -- \
            ./deploy-account-role.sh
    fi
}

# ---------------------------------------------------------------------------
# Account-role invocation (non-interactive)
# ---------------------------------------------------------------------------
# _account_role_config_file <topology> -> echoes the `*-config.txt` filename
# that deploy-account-role.sh reads (and writes) for a multi-account topology,
# or nothing for a topology that uses no config file. The account component of
# the name matches the convention used by _external_id_reuse_available (the
# sts caller-identity account resolved via _resolve_account, i.e. the script's
# CURRENT_ACCOUNT) so the reuse decision and the constructed stdin feed stay
# perfectly consistent (Req 2.4). The name is bare/relative, exactly as the
# script reads it from the `edge-cv-portal` CWD.
_account_role_config_file() {
    local topology="${1:-}" acct
    acct="$(_resolve_account)"
    [ -n "$acct" ] || return 0
    case "$topology" in
        usecase) printf '%s' "usecase-account-${acct}-config.txt" ;;
        data)    printf '%s' "data-account-${acct}-config.txt" ;;
        *)       : ;;
    esac
}

# build_account_role_stdin [topology] -> echoes the constructed stdin feed that
# answers deploy-account-role.sh's `read -p` prompts, IN THE EXACT ORDER the
# script issues them for the given Deployment_Topology, from the resolved
# globals (PORTAL_ACCOUNT_ID, EXTERNAL_ID, DATA_BUCKETS) and the config-reuse
# state. Feeding this stream (task's non-interactive path) means the script
# never blocks on a terminal (Req 5.2) while its own prompts are still the
# single source of question-asking (Req 2.1).
#
# The script's prompt order (see deploy-account-role.sh):
#   single-account : none (the positional topology arg bypasses the menu and
#                    the single-account branch issues no `read -p`)        -> empty feed
#   usecase        : 1) "Enter Portal Account ID:"
#                    2) External-ID prompt(s):
#                         * config exists + no --external-id supplied ->
#                             "Reuse existing External ID? (Y/n):"  -> feed "Y" (reuse; Req 2.4)
#                         * config exists + --external-id supplied ->
#                             "Reuse existing External ID? (Y/n):"  -> feed "n" (decline)
#                             "Enter new External ID ...:"           -> feed EXTERNAL_ID
#                         * no config ->
#                             "Enter External ID ...:"               -> feed EXTERNAL_ID
#   data           : 1) + 2) as usecase, then
#                    3) "Enter data bucket names ...:"  -> feed DATA_BUCKETS (may be blank)
#
# Whether a reusable config exists is decided with the SAME file the reuse
# reconciliation (_external_id_reuse_available) stats, so the "Y"/"n"/value
# branch matches what the script will actually do.
build_account_role_stdin() {
    local topology="${1:-${TOPOLOGY:-}}"

    case "$topology" in
        single-account)
            : # No prompts after the topology; emit an empty feed.
            ;;
        usecase|data)
            # Prompt 1 (always issued for usecase/data): Portal Account ID.
            printf '%s\n' "${PORTAL_ACCOUNT_ID:-}"

            # Prompt 2: External ID. The structure depends on whether the
            # script will find a reusable config and whether we supplied a value.
            local config_file=""
            config_file="$(_account_role_config_file "$topology")"
            if [ -n "$config_file" ] && [ -f "$config_file" ]; then
                # Config present -> the script asks the reuse question first.
                if _external_id_reuse_available "$topology"; then
                    # No External ID supplied -> reuse the config's value.
                    printf '%s\n' "Y"
                else
                    # External ID supplied -> decline reuse, then feed the value.
                    printf '%s\n' "n"
                    printf '%s\n' "${EXTERNAL_ID:-}"
                fi
            else
                # No config -> the script prompts for the External ID directly.
                printf '%s\n' "${EXTERNAL_ID:-}"
            fi

            # Prompt 3 (data only): data bucket names, possibly blank.
            if [ "$topology" = "data" ]; then
                printf '%s\n' "${DATA_BUCKETS:-}"
            fi
            ;;
        *)
            : # Invalid topology is rejected before any step runs (Req 4.3).
            ;;
    esac
}

# run_account_role_noninteractive [topology] -> invoke the existing
# Account_Role_Script as the account-role Deployment_Step in Non_Interactive_Mode
# (Req 4.2, 5.2, 5.4, 2.4).
#
# It builds the constructed stdin feed (build_account_role_stdin) matching the
# script's prompt order for the topology and pipes it in via run_step's
# non-interactive path, so no terminal prompting occurs on any stream (Req 5.2).
# The topology is passed as the script's first POSITIONAL argument, invoking its
# non-interactive positional path rather than the interactive menu (Req 4.2,
# 5.4). The script is never modified; the feed only answers the questions it
# already asks (Req 2.1, 8.1). The step runs from PORTAL_DIR so the script's
# relative-path operations (including the `*-config.txt` it reads) resolve as
# they do standalone (Req 8.3).
run_account_role_noninteractive() {
    local topology="${1:-${TOPOLOGY:-}}"
    local feed

    # Capture the feed WITHOUT losing its trailing newline. deploy-account-role.sh
    # runs under `set -e`, so a `read` that hits EOF before a newline returns
    # non-zero and would abort the script. Each answer line (including the last,
    # e.g. a blank data-bucket line) must therefore be newline-terminated. A
    # plain `$(...)` strips ALL trailing newlines, so append a sentinel and strip
    # only the sentinel to keep the bytes build_account_role_stdin emitted.
    feed="$(build_account_role_stdin "$topology"; printf 'x')"
    feed="${feed%x}"

    run_step "account-role" "$PORTAL_DIR" false "$feed" -- \
        ./deploy-account-role.sh "$topology"
}

# ---------------------------------------------------------------------------
# Output collection + Deployment_Summary
# ---------------------------------------------------------------------------
# resolve_portal_url -> echoes "https://<domain>" from the CloudFront
# DistributionDomainName output of EdgeCVPortalFrontendStack, or "" if the
# output is absent/None (Req 7.1). It reads the SAME stack/output key the
# Frontend_Script uses so the resolved URL matches what a standalone frontend
# run reports. Any describe-stacks failure is treated as "unavailable" (empty
# output) rather than a fatal error, so print_summary can decide what to do
# (Req 7.6).
resolve_portal_url() {
    local domain
    domain="$(aws cloudformation describe-stacks \
        --stack-name EdgeCVPortalFrontendStack \
        --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" \
        --output text 2>/dev/null || true)"
    if [ -z "$domain" ] || [ "$domain" = "None" ]; then
        return 0
    fi
    printf 'https://%s' "$domain"
}

# _summary_config_file <topology> <account> -> echoes the absolute path of the
# `*-config.txt` deploy-account-role.sh writes for a multi-account topology, or
# nothing for a topology that uses no config file. The account component matches
# the credential account the script uses (CURRENT_ACCOUNT == the resolved
# ACCOUNT), and the file lives in the `edge-cv-portal` directory (PORTAL_DIR),
# where the account-role step runs and writes it (Req 8.3).
_summary_config_file() {
    local topology="${1:-}" account="${2:-}"
    [ -n "$account" ] || return 0
    case "$topology" in
        usecase) printf '%s' "$PORTAL_DIR/usecase-account-${account}-config.txt" ;;
        data)    printf '%s' "$PORTAL_DIR/data-account-${account}-config.txt" ;;
        *)       : ;;
    esac
}

# _read_config_value <file> <key> -> echoes the value of `KEY=` from the config
# file (first match), or nothing when the file/key is absent. Only the config
# files deploy-account-role.sh writes are read; credential material is never
# among their keys, so nothing secret can be surfaced this way (Req 7.4).
_read_config_value() {
    local file="${1:-}" key="${2:-}"
    { [ -n "$file" ] && [ -f "$file" ]; } || return 0
    sed -n "s/^${key}=//p" "$file" | head -n1
}

# collect_role_arns <topology> <account> -> echoes the created role ARNs, one
# per line, drawn only from deterministic naming (single-account) or the
# account-role config file (usecase/data) — never from credential material
# (Req 7.2, 7.4).
#
#   single-account : the two roles deploy-account-role.sh creates in the current
#                    account — DDASageMakerExecutionRole and Greengrass_ServiceRole.
#   usecase / data : the cross-account ROLE_ARN recorded in the topology's
#                    `*-config.txt` (DDAPortalAccessRole / DDAPortalDataAccessRole).
collect_role_arns() {
    local topology="${1:-}" account="${2:-}"
    case "$topology" in
        single-account)
            if [ -n "$account" ]; then
                printf 'arn:aws:iam::%s:role/DDASageMakerExecutionRole\n' "$account"
                printf 'arn:aws:iam::%s:role/Greengrass_ServiceRole\n' "$account"
            fi
            ;;
        usecase|data)
            local file role_arn
            file="$(_summary_config_file "$topology" "$account")"
            role_arn="$(_read_config_value "$file" ROLE_ARN)"
            if [ -n "$role_arn" ]; then
                printf '%s\n' "$role_arn"
            fi
            ;;
    esac
    return 0
}

# print_summary -> print the Deployment_Summary to stdout: topology, account,
# region, the Portal URL (single-account only), the created role ARNs, the
# External ID (usecase/data only), and EVERY step defined for the topology with
# exactly one outcome of success|failure|not-run, followed by the Deployment_Log
# location (Req 7.1, 7.2, 7.3, 7.5). The summary is assembled only from
# CloudFormation outputs and the account-role config files — never from
# credential material (Req 7.4).
#
# Return code: for `single-account`, when the Portal URL cannot be resolved
# after the frontend step, the summary states it is unavailable, includes the
# log path, and print_summary returns non-zero so the command exits non-zero
# (Req 7.6). Otherwise it returns zero. On the stop-on-failure path in main the
# call is guarded (`|| true`) so this never masks the original failure exit
# code; on the all-success path its return code is the command's exit code, so
# a single-account run with an unresolvable URL correctly exits non-zero.
print_summary() {
    local topology="${TOPOLOGY:-}" account="${ACCOUNT:-}" region="${REGION:-}"
    local summary_rc=0

    printf '%s\n' "==================== Deployment Summary ===================="
    printf 'Topology:        %s\n' "$topology"
    printf 'AWS Account:     %s\n' "$account"
    printf 'Region:          %s\n' "$region"

    # Portal URL is meaningful only for the single-account topology, whose
    # frontend step publishes the CloudFront distribution (Req 7.1). An
    # unresolvable URL after the frontend step is a failure to report (Req 7.6).
    if [ "$topology" = "single-account" ]; then
        local url
        url="$(resolve_portal_url)"
        if [ -n "$url" ]; then
            printf 'Portal URL:      %s\n' "$url"
        else
            printf 'Portal URL:      unavailable\n'
            summary_rc=1
        fi
    fi

    # Created role ARNs for the topology (Req 7.2).
    local arns line
    arns="$(collect_role_arns "$topology" "$account")"
    printf 'Created roles:\n'
    if [ -n "$arns" ]; then
        while IFS= read -r line; do
            [ -n "$line" ] && printf '  - %s\n' "$line"
        done <<< "$arns"
    else
        printf '  (none)\n'
    fi

    # External ID for the multi-account topologies, so the operator can register
    # the account in the Portal (Req 7.3).
    if [ "$topology" = "usecase" ] || [ "$topology" = "data" ]; then
        local config_file external_id
        config_file="$(_summary_config_file "$topology" "$account")"
        external_id="$(_read_config_value "$config_file" EXTERNAL_ID)"
        printf 'External ID:     %s\n' "${external_id:-unavailable}"
    fi

    # Every step defined for the topology, each reported exactly once with its
    # single outcome (Req 7.5). Driving off STEPS_FOR guarantees the set is
    # exactly the topology's steps — steps before a failure are success, the
    # failing step is failure, and steps after it remain not-run.
    printf 'Steps:\n'
    local step
    for step in ${STEPS_FOR[$topology]:-}; do
        printf '  %-16s %s\n' "$step" "${STEP_OUTCOMES[$step]:-not-run}"
    done

    # Always surface the Deployment_Log location, including on the
    # URL-unavailable path (Req 7.6).
    printf 'Deployment log:  %s\n' "${LOG_FILE:-<not created>}"
    printf '%s\n' "============================================================"

    return "$summary_rc"
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------
# main "$@" -> orchestrate the full one-command deploy:
#   parse_args -> (help) -> topology validation -> non-interactive input
#   validation -> prerequisite gate -> CDK bootstrap (before CDK-deploying
#   steps) -> the per-topology ordered step sequence with stop-on-failure ->
#   Deployment_Summary.
#
# The step sequence is driven entirely by STEPS_FOR[$TOPOLOGY] (Req 1.1, 1.2,
# 1.4, 4.4): `single-account` runs account-role -> auth -> infrastructure ->
# frontend, while `usecase`/`data` run only account-role and never touch
# auth/infrastructure/frontend for that account.
main() {
    parse_args "$@"

    if [ "${SHOW_HELP:-0}" -eq 1 ]; then
        usage
        return 0
    fi

    # In Interactive_Mode a bare invocation with no topology performs the
    # default full single-account deploy (matches the usage example). In
    # Non_Interactive_Mode the topology must be supplied explicitly, so an empty
    # value is left as-is for validate_topology to reject (Req 4.3, 5.1).
    if [ -z "${TOPOLOGY:-}" ] && [ "${NON_INTERACTIVE:-0}" -ne 1 ]; then
        TOPOLOGY="single-account"
    fi

    # Reject an invalid/absent Deployment_Topology before any step runs (Req 4.3).
    validate_topology "${TOPOLOGY:-}" || exit $?

    # Non_Interactive_Mode: fail fast if any required input is missing, before
    # any mutating step, reporting them all at once (Req 5.3).
    check_required_inputs || exit $?

    # Prerequisite gate: every check runs before any mutating step; on failure
    # no step runs and the command exits non-zero. On success it resolves and
    # exports ACCOUNT/REGION and prints them (Req 3.1-3.5, 3.8).
    check_prerequisites || exit $?

    # Forward the AWS profile to every subsequent step so all AWS/CDK calls use
    # the profile the operator selected (Req 8.4).
    if [ -n "${PROFILE:-}" ]; then
        export AWS_PROFILE="$PROFILE"
    fi

    # Ordered step sequence for the resolved topology (Req 1.1, 1.2, 1.4, 4.4).
    local steps="${STEPS_FOR[$TOPOLOGY]}"

    # Ensure the target account/region is CDK-bootstrapped before any
    # CDK-deploying step (auth/infrastructure/frontend). The account-role step
    # runs its own bootstrap check for the usecase/data CDK stacks, so the
    # orchestrator only gates the stacks it drives directly (Req 3.6, 3.7).
    local step
    local needs_bootstrap=0
    for step in $steps; do
        case "$step" in
            auth|infrastructure|frontend) needs_bootstrap=1 ;;
        esac
    done
    if [ "$needs_bootstrap" -eq 1 ]; then
        check_bootstrap || exit $?
    fi

    # Create and announce the Deployment_Log before the first step (Req 6.6).
    init_log

    # Auth step arguments: forward --sso/--profile/--region as appropriate. The
    # SSO env (SSO_METADATA_URL, ...) is already exported unchanged by parse_args
    # (Req 5.6); REGION is always resolved by the prerequisite gate, so this
    # array is never empty.
    local -a auth_args=()
    [ "${SSO:-0}" -eq 1 ] && auth_args+=(--sso)
    [ -n "${PROFILE:-}" ] && auth_args+=(--profile "$PROFILE")
    [ -n "${REGION:-}" ] && auth_args+=(--region "$REGION")

    # Ordered step sequence with stop-on-failure (Req 1.1, 6.1, 6.2). On the
    # first non-zero step the sequence halts before the next step; the failed
    # step is recorded `failure` and every step after it stays `not-run` (its
    # initialized outcome). No rollback or compensating action is taken.
    local rc
    for step in $steps; do
        rc=0
        case "$step" in
            account-role)
                if [ "${NON_INTERACTIVE:-0}" -eq 1 ]; then
                    run_account_role_noninteractive "$TOPOLOGY" || rc=$?
                else
                    run_account_role_interactive "$TOPOLOGY" || rc=$?
                fi
                ;;
            auth)
                run_step "auth" "$PORTAL_DIR/infrastructure" false -- \
                    ./deploy-auth.sh "${auth_args[@]}" || rc=$?
                ;;
            infrastructure)
                run_step "infrastructure" "$PORTAL_DIR" false -- \
                    ./deploy-infrastructure.sh || rc=$?
                ;;
            frontend)
                run_step "frontend" "$PORTAL_DIR" false -- \
                    ./deploy-frontend.sh || rc=$?
                ;;
        esac

        if [ "$rc" -ne 0 ]; then
            # Stop-on-failure: no subsequent step runs; report the failed step
            # name and the Deployment_Log location to stdout, then exit non-zero
            # (Req 6.1, 6.2).
            echo "Deployment failed at step: $step"
            echo "Deployment log: $LOG_FILE"
            # print_summary is implemented by task 10.1; the call site is left
            # here so the failure summary (per-step outcomes) surfaces once it
            # exists. It must not change the failing exit code.
            print_summary || true
            return "$rc"
        fi
    done

    # Every step succeeded. Emit the Deployment_Summary (task 10.1) and exit
    # with its status: today the placeholder returns success (Req 1.3); once
    # print_summary is implemented, the single-account URL-unavailable case
    # (Req 7.6) can surface a non-zero exit through this path.
    print_summary
    return $?
}

# Only run main when executed directly, so tests can source individual
# functions without triggering the dispatcher.
if [ "${BASH_SOURCE[0]:-}" = "${0}" ]; then
    main "$@"
fi
