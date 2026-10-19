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
#      ssm get-parameter bootstrap version, cloudformation describe-stacks)
#      and for the enforcement lookup the deploy scripts make (lambda
#      list-functions).
#   4. Opt-in fixtures for the trusted UseCase account list and per-profile
#      identities; each is off (the answers above) while its variable is unset:
#         STUB_SSM_TRUSTED_FILE      state file of SSM
#                                    /dda-portal/trusted-usecase-account-ids:
#                                    get-parameter prints it (ParameterNotFound
#                                    when absent); put-parameter creates it
#                                    (ParameterAlreadyExists without --overwrite)
#         STUB_SSM_TRUSTED_GET_STDERR / STUB_SSM_TRUSTED_PUT_STDERR
#                                    that get / put fails with this error (254)
#         STUB_CFN_TEMPLATES_DIR     cloudformation get-template serves
#                                    <dir>/<stack>.json; <dir>/<stack>.denied is
#                                    AccessDenied; any other stack does not exist
#         STUB_CFN_MISSING_STACKS    describe-stacks of these stacks (space-
#                                    separated) fails with "does not exist"
#         STUB_AWS_PROFILE_ACCOUNTS / STUB_AWS_PROFILE_REGIONS
#                                    "profile=value ..." maps: the account sts
#                                    and the region `configure get region`
#                                    answer under that AWS_PROFILE
#         STUB_IGNORE_STDIN_FOR      stub names (space-separated) that leave
#                                    stdin unread
#      `aws configure export-credentials` always fails (no credentials).
#
# Everything runs fully offline; no real AWS account or network is touched.

set -u

# ---- identity ---------------------------------------------------------------
STUB_NAME="$(basename -- "$0")"
# Upper-case + sanitize for env-fixture lookups.
STUB_KEY="$(printf '%s' "$STUB_NAME" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')"

# ---- capture stdin (never block on a tty) -----------------------------------
# A name listed in STUB_IGNORE_STDIN_FOR (space-separated) leaves stdin unread,
# so a feed piped to a real script reaches that script's own `read`s instead of
# the first stubbed tool it runs.
STUB_STDIN=""
case " ${STUB_IGNORE_STDIN_FOR:-} " in
  *" $STUB_NAME "*) : ;;
  *)
    if [ ! -t 0 ]; then
      # Reads a piped feed or /dev/null; returns immediately at EOF.
      STUB_STDIN="$(cat 2>/dev/null || true)"
    fi
    ;;
esac

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
  local capture="${STUB_ENV_CAPTURE:-AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION CDK_DEFAULT_REGION CDK_DEFAULT_ACCOUNT SSO_ENABLED SSO_METADATA_URL SSO_PROVIDER_NAME COGNITO_DOMAIN_PREFIX TRUSTED_USECASE_ACCOUNT_IDS DATA_BUCKET_ALLOWLIST cloudFrontDomain EXTERNAL_ID PORTAL_ACCOUNT_ID PORTAL_REGISTRY_ENFORCED AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN}"
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

# _has_flag <flag> <args...> -> success when <flag> is one of the args.
_has_flag() {
  local wanted="$1" a; shift
  for a in "$@"; do [ "$a" = "$wanted" ] && return 0; done
  return 1
}

# _profile_value <"profile=value ..."> -> the value mapped to $AWS_PROFILE
# (rc 1, nothing printed, when AWS_PROFILE is unset or not in the map).
_profile_value() {
  local kv
  [ -n "${AWS_PROFILE:-}" ] || return 1
  for kv in $1; do
    if [ "${kv%%=*}" = "$AWS_PROFILE" ]; then printf '%s' "${kv#*=}"; return 0; fi
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
      local acct="${STUB_AWS_ACCOUNT_ID-123456789012}" mapped
      if mapped="$(_profile_value "${STUB_AWS_PROFILE_ACCOUNTS:-}")"; then acct="$mapped"; fi
      local query; query="$(_arg_value --query "$@" || true)"
      if printf '%s' "$query" | grep -qi 'Account'; then
        printf '%s\n' "$acct"
      else
        printf '{"Account":"%s","Arn":"arn:aws:iam::%s:user/stub","UserId":"STUBUSERID"}\n' "$acct" "$acct"
      fi
      exit 0
      ;;
    configure)
      # configure export-credentials: no credentials (never eval'd output).
      if [ "${1:-}" = "export-credentials" ]; then
        printf '%s\n' 'Unable to locate credentials. You can configure credentials by running "aws configure".' >&2
        exit 255
      fi
      # configure get region
      local region="${STUB_AWS_CONFIG_REGION-}" mapped
      if mapped="$(_profile_value "${STUB_AWS_PROFILE_REGIONS:-}")"; then region="$mapped"; fi
      if [ -z "$region" ]; then exit 1; fi
      printf '%s\n' "$region"
      exit 0
      ;;
    ssm)
      # The trusted UseCase account list parameter, when STUB_SSM_TRUSTED_FILE
      # opts in (see the header); every other name keeps the answers below.
      local ssm_op="${1:-}" ssm_name
      ssm_name="$(_arg_value --name "$@" || true)"
      if [ -n "${STUB_SSM_TRUSTED_FILE:-}" ] && [ "$ssm_name" = "/dda-portal/trusted-usecase-account-ids" ]; then
        case "$ssm_op" in
          get-parameter)
            if [ -n "${STUB_SSM_TRUSTED_GET_STDERR:-}" ]; then
              printf '\n%s\n' "$STUB_SSM_TRUSTED_GET_STDERR" >&2; exit 254
            fi
            if [ -f "$STUB_SSM_TRUSTED_FILE" ]; then
              printf '%s\n' "$(cat "$STUB_SSM_TRUSTED_FILE")"; exit 0
            fi
            printf '\n%s\n' "An error occurred (ParameterNotFound) when calling the GetParameter operation: " >&2
            exit 254
            ;;
          put-parameter)
            if [ -n "${STUB_SSM_TRUSTED_PUT_STDERR:-}" ]; then
              printf '\n%s\n' "$STUB_SSM_TRUSTED_PUT_STDERR" >&2; exit 254
            fi
            if [ -f "$STUB_SSM_TRUSTED_FILE" ] && ! _has_flag --overwrite "$@"; then
              printf '\n%s\n' "An error occurred (ParameterAlreadyExists) when calling the PutParameter operation: The parameter already exists. To overwrite this value, set the overwrite option in the request to true." >&2
              exit 254
            fi
            printf '%s' "$(_arg_value --value "$@" || true)" > "$STUB_SSM_TRUSTED_FILE"
            printf '%s\n' '{"Version": 1, "Tier": "Standard"}'
            exit 0
            ;;
        esac
      fi
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
      # Opt-in: get-template from STUB_CFN_TEMPLATES_DIR, and describe-stacks
      # of the stacks in STUB_CFN_MISSING_STACKS fails (see the header).
      local cfn_op="${1:-}"
      if [ "$cfn_op" = "get-template" ] && [ -n "${STUB_CFN_TEMPLATES_DIR:-}" ]; then
        if [ -e "$STUB_CFN_TEMPLATES_DIR/$stack.denied" ]; then
          printf '\n%s\n' "An error occurred (AccessDenied) when calling the GetTemplate operation: User: arn:aws:sts::${STUB_AWS_ACCOUNT_ID-123456789012}:assumed-role/stub/stub is not authorized to perform: cloudformation:GetTemplate on resource: $stack" >&2
          exit 254
        fi
        if [ -f "$STUB_CFN_TEMPLATES_DIR/$stack.json" ]; then
          cat "$STUB_CFN_TEMPLATES_DIR/$stack.json"; exit 0
        fi
        printf '\n%s\n' "An error occurred (ValidationError) when calling the GetTemplate operation: Stack with id $stack does not exist" >&2
        exit 254
      fi
      if [ "$cfn_op" = "describe-stacks" ]; then
        case " ${STUB_CFN_MISSING_STACKS:-} " in
          *" $stack "*)
            printf '\n%s\n' "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id $stack does not exist" >&2
            exit 254 ;;
        esac
      fi
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
    lambda)
      # list-functions --query "...PORTAL_REGISTRY_ENFORCED" --output text
      # (scripts/portal-registry-enforcement.sh). STUB_LAMBDA_ENFORCED_VALUES
      # holds the deployed handlers' values, space-separated; unset or empty
      # means no portal handler is deployed. Printed tab-separated on one
      # line, as the real CLI's text output does. A non-zero
      # STUB_AWS_LAMBDA_EXIT simulates a failed lookup.
      if [ -n "${STUB_AWS_LAMBDA_EXIT:-}" ] && [ "${STUB_AWS_LAMBDA_EXIT}" != "0" ]; then
        printf '%s\n' "${STUB_AWS_LAMBDA_STDERR:-An error occurred (AccessDeniedException) when calling the ListFunctions operation}" >&2
        exit "$STUB_AWS_LAMBDA_EXIT"
      fi
      if [ -n "${STUB_LAMBDA_ENFORCED_VALUES:-}" ]; then
        printf '%s\n' "$STUB_LAMBDA_ENFORCED_VALUES" | tr ' ' '\t'
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
