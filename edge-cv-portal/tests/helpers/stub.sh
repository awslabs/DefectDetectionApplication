#!/usr/bin/env bash
#
# Generic PATH-shadow stub used by the one-command-portal-deploy bats suite.
#
# A single implementation backs every stubbed executable. The concrete name is
# taken from $0 (basename), so the same file is symlinked/copied as
# deploy-account-role.sh, deploy-auth.sh, deploy-infrastructure.sh,
# deploy-frontend.sh, configure-bucket-cors.sh, aws, node, cdk and npx.
#
# On every invocation the stub:
#   1. Records a single tab-separated line to $STUB_TRACE containing the stub
#      name, $PWD, its positional args, a selected set of env vars and any
#      stdin it received (args/env/stdin are base64 encoded so the line stays
#      single-line and delimiter-safe).
#   2. Emits caller-controlled stdout / stderr and exits with a caller-chosen
#      code, all driven by env fixtures:
#         STUB_<NAME>_STDOUT   text written to stdout
#         STUB_<NAME>_STDERR   text written to stderr
#         STUB_<NAME>_EXIT     exit code (default 0)
#      where <NAME> is the executable name upper-cased with every non
#      alphanumeric character replaced by '_' (deploy-account-role.sh ->
#      DEPLOY_ACCOUNT_ROLE_SH, aws -> AWS, npx -> NPX).
#   3. For `aws` it additionally serves canned responses for the calls the
#      orchestrator makes (sts get-caller-identity, configure get region,
#      ssm get-parameter bootstrap version, cloudformation describe-stacks).
#
# Everything runs fully offline; no real AWS account or network is touched.

set -u

# ---- identity ---------------------------------------------------------------
STUB_NAME="$(basename -- "$0")"
# Upper-case + sanitize for env-fixture lookups.
STUB_KEY="$(printf '%s' "$STUB_NAME" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')"

# ---- capture stdin (never block on a tty) -----------------------------------
STUB_STDIN=""
if [ ! -t 0 ]; then
  # Reads a piped feed or /dev/null; returns immediately at EOF.
  STUB_STDIN="$(cat 2>/dev/null || true)"
fi

# ---- record the invocation --------------------------------------------------
_b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

_record() {
  [ -n "${STUB_TRACE:-}" ] || return 0

  # positional args joined by newline
  local args_joined=""
  local a first=1
  for a in "$@"; do
    if [ "$first" -eq 1 ]; then args_joined="$a"; first=0
    else args_joined="$args_joined
$a"; fi
  done

  # selected env vars (only those that are set) as VAR=value lines
  local capture="${STUB_ENV_CAPTURE:-AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION CDK_DEFAULT_REGION CDK_DEFAULT_ACCOUNT SSO_ENABLED SSO_METADATA_URL SSO_PROVIDER_NAME COGNITO_DOMAIN_PREFIX TRUSTED_USECASE_ACCOUNT_IDS DATA_BUCKET_ALLOWLIST cloudFrontDomain EXTERNAL_ID PORTAL_ACCOUNT_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN}"
  local env_joined="" v
  for v in $capture; do
    if [ -n "${!v+x}" ]; then
      if [ -z "$env_joined" ]; then env_joined="$v=${!v}"
      else env_joined="$env_joined
$v=${!v}"; fi
    fi
  done

  # name<TAB>pwd<TAB>b64(args)<TAB>b64(env)<TAB>b64(stdin)
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$STUB_NAME" "$PWD" \
    "$(_b64 "$args_joined")" "$(_b64 "$env_joined")" "$(_b64 "$STUB_STDIN")" \
    >> "$STUB_TRACE"
}

_record "$@"

# ---- caller-controlled output helpers ---------------------------------------
_emit_fixture_output() {
  local out_var="STUB_${STUB_KEY}_STDOUT"
  local err_var="STUB_${STUB_KEY}_STDERR"
  [ -n "${!out_var:-}" ] && printf '%s\n' "${!out_var}"
  [ -n "${!err_var:-}" ] && printf '%s\n' "${!err_var}" >&2
  return 0
}

_fixture_exit() {
  local exit_var="STUB_${STUB_KEY}_EXIT"
  exit "${!exit_var:-0}"
}

# ---- aws canned-response engine ---------------------------------------------
# Pulls the value of a named CLI option out of the arg list.
_arg_value() {
  local wanted="$1"; shift
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "$wanted" ]; then printf '%s' "${2:-}"; return 0; fi
    shift
  done
  return 1
}

_aws_main() {
  # Always allow a hard override first.
  local exit_var="STUB_AWS_EXIT"
  if [ -n "${!exit_var:-}" ]; then
    _emit_fixture_output
    exit "${!exit_var}"
  fi

  local sub="${1:-}"; shift || true
  case "$sub" in
    sts)
      # get-caller-identity
      if [ -n "${STUB_AWS_STS_EXIT:-}" ] && [ "${STUB_AWS_STS_EXIT}" != "0" ]; then
        [ -n "${STUB_AWS_STS_STDERR:-}" ] && printf '%s\n' "$STUB_AWS_STS_STDERR" >&2
        exit "$STUB_AWS_STS_EXIT"
      fi
      local acct="${STUB_AWS_ACCOUNT_ID-123456789012}"
      local query; query="$(_arg_value --query "$@" || true)"
      if printf '%s' "$query" | grep -qi 'Account'; then
        printf '%s\n' "$acct"
      else
        printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/stub","UserId":"STUBUSERID"}\n' "$acct" "$acct"
      fi
      exit 0
      ;;
    configure)
      # configure get region
      local region="${STUB_AWS_CONFIG_REGION-}"
      if [ -z "$region" ]; then exit 1; fi
      printf '%s\n' "$region"
      exit 0
      ;;
    ssm)
      # get-parameter --name /cdk-bootstrap/hnb659fds/version
      # Unset  -> parameter absent (non-zero, callers fall back to "0").
      # Set    -> echo the value (may be any version string).
      if [ -z "${STUB_BOOTSTRAP_VERSION+x}" ] || [ -z "${STUB_BOOTSTRAP_VERSION}" ]; then
        [ -n "${STUB_AWS_SSM_STDERR:-}" ] && printf '%s\n' "$STUB_AWS_SSM_STDERR" >&2
        exit 255
      fi
      printf '%s\n' "$STUB_BOOTSTRAP_VERSION"
      exit 0
      ;;
    cloudformation)
      # describe-stacks --stack-name <name> --query "...OutputKey=='KEY'..."
      local stack key query
      stack="$(_arg_value --stack-name "$@" || true)"
      query="$(_arg_value --query "$@" || true)"
      # Extract the OutputKey referenced by the query (handles '...' or `...`).
      key="$(printf '%s' "$query" | grep -oE "OutputKey==[\`']?[A-Za-z0-9]+" | head -n1 | sed -E "s/OutputKey==[\`']?//")"
      if [ -n "${STUB_CFN_DESCRIBE_EXIT:-}" ] && [ "${STUB_CFN_DESCRIBE_EXIT}" != "0" ]; then
        exit "$STUB_CFN_DESCRIBE_EXIT"
      fi
      local stack_key value_var
      stack_key="$(printf '%s' "$stack" | tr -c 'A-Za-z0-9' '_')"
      value_var="STUB_CFN_${stack_key}_${key}"
      if [ -n "${!value_var+x}" ]; then
        printf '%s\n' "${!value_var}"
      fi
      exit 0
      ;;
    *)
      # Unknown aws subcommand: behave as a generic recording stub.
      _emit_fixture_output
      _fixture_exit
      ;;
  esac
}

# ---- dispatch ---------------------------------------------------------------
case "$STUB_NAME" in
  aws)
    _aws_main "$@"
    ;;
  *)
    _emit_fixture_output
    _fixture_exit
    ;;
esac
