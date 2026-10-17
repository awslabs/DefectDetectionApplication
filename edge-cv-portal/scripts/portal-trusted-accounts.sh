#!/usr/bin/env bash
#
# Trusted UseCase account list for a portal deploy.
#
# Sourced by deploy-portal.sh (single-account topology), deploy-frontend.sh
# (its EdgeCVPortalComputeStack redeploy) and
# infrastructure/deploy_portal_fixes.sh, so the three always pass the CDK app
# the same list.
#
# The list (CDK context `trustedUseCaseAccountIds`, comma-separated 12-digit
# account IDs) bounds what the portal may do in other accounts
# (infrastructure/lib/compute-stack.ts):
#   * every portal Lambda role and the QuickSetup role may sts:AssumeRole only
#     arn:aws:iam::<id>:role/DDAPortalAccessRole for the listed IDs;
#   * the detector-export ECR repository lets only the listed IDs'
#     DDASageMakerExecutionRole pull;
#   * the camera-shadow and account-sync SQS queues accept SendMessage only
#     from the listed accounts and the portal account.
# The NodeDesigner and SyntheticData stacks scope their sts:AssumeRole grants
# to it too, and every one of these stacks refuses to synthesize an empty list.
#
# portal_trusted_accounts_resolve uses bin/app.ts's own order
# (resolveTrustedUseCaseAccountIds), then one read-back:
#   1. CDK context trustedUseCaseAccountIds, in the CDK CLI's file order:
#      infrastructure/cdk.json "context", infrastructure/cdk.context.json,
#      ~/.cdk.json "context". The first file that defines the key decides; an
#      empty or false value there counts as unset, as `if (fromContext)` does.
#   2. TRUSTED_USECASE_ACCOUNT_IDS, when not blank.
#   3. SSM parameter /dda-portal/trusted-usecase-account-ids.
#   4. The list the deployed portal stacks grant: the account IDs of the
#      DDAPortalAccessRole ARNs in the EdgeCVPortalComputeStack template, in
#      template order. The NodeDesigner and SyntheticData templates, where
#      they name any, must grant the same set.
#   5. "none": nothing is configured and EdgeCVPortalComputeStack is not
#      deployed. The caller decides what a fresh install uses.
#
# Fail closed: a value that is not a list of 12-digit IDs, an SSM read error
# other than ParameterNotFound, a template that cannot be read, a deployed
# ComputeStack with no account-scoped DDAPortalAccessRole grant (e.g. the
# pre-scoping wildcard), or stacks that grant different lists all stop with
# the reason and the remedy (rc 1). Nothing falls through to a later source,
# which could be a default or a stale list.
#
# Safe under `set -euo pipefail`, `set -e` and `set -uo pipefail`, and bash
# 3.2 (macOS): no associative arrays. Sourcing makes no AWS call and never
# fails. The functions call only `aws` (plus python3 to read a context file
# that names the key).

PORTAL_TRUSTED_SSM_PARAMETER="/dda-portal/trusted-usecase-account-ids"
PORTAL_TRUSTED_STACKS="EdgeCVPortalComputeStack EdgeCVPortalNodeDesignerStack EdgeCVPortalSyntheticDataStack"
# infrastructure/, the directory the deploy scripts run the CDK CLI from: its
# cdk.json and cdk.context.json are the project context files. Empty when it
# cannot be resolved (then only ~/.cdk.json is consulted).
PORTAL_TRUSTED_INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../infrastructure" 2>/dev/null && pwd)" \
  || PORTAL_TRUSTED_INFRA_DIR=""

# Reads trustedUseCaseAccountIds from one context file the way the CDK app
# sees it: String(value). argv: <file> <1 = under "context", 0 = top level>.
# Exit 0 value printed, 3 key not defined, 4 defined but falsy, 1 unreadable.
_PORTAL_TRUSTED_CONTEXT_PY='
import json, sys
path, nested = sys.argv[1], sys.argv[2] == "1"
try:
    with open(path, encoding="utf-8-sig") as handle:
        data = json.load(handle)
except Exception as error:
    print("not valid JSON (%s)" % error)
    sys.exit(1)
if nested:
    data = data.get("context") if isinstance(data, dict) else None
if not isinstance(data, dict) or "trustedUseCaseAccountIds" not in data:
    sys.exit(3)
value = data["trustedUseCaseAccountIds"]
if value is None or value is False or value == "" or (not isinstance(value, bool) and value == 0):
    sys.exit(4)
def text(item):
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, float) and item.is_integer():
        return str(int(item))
    if isinstance(item, (str, int, float)):
        return str(item)
    raise ValueError(item)
try:
    if isinstance(value, list):
        print(",".join(text(item) for item in value))
    else:
        print(text(value))
except ValueError:
    print("trustedUseCaseAccountIds is %s, not a string or a list of account IDs" % json.dumps(value))
    sys.exit(1)
'

# _portal_trusted_tmp -> path of a new temp file (rc 1 if none can be made).
_portal_trusted_tmp() {
  mktemp "${TMPDIR:-/tmp}/portal-trusted.XXXXXX" 2>/dev/null
}

# _portal_trusted_trim <text> -> <text> without leading/trailing whitespace.
_portal_trusted_trim() {
  local text="$1"
  text="${text#"${text%%[![:space:]]*}"}"
  text="${text%"${text##*[![:space:]]}"}"
  printf '%s' "$text"
}

# _portal_trusted_normalize <raw>
#   Sets _PT_LIST to the list: entries trimmed, empty ones dropped, duplicates
#   removed (first kept). rc 1, with the offending entries in _PT_BAD, unless
#   every entry is a 12-digit account ID and at least one is left.
_portal_trusted_normalize() {
  local rest="$1," entry
  _PT_LIST=""
  _PT_BAD=""
  while [ -n "$rest" ]; do
    entry="${rest%%,*}"
    rest="${rest#*,}"
    entry="$(_portal_trusted_trim "$entry")"
    if [ -z "$entry" ]; then
      continue
    fi
    case "$entry" in
      *[!0123456789]*)
        _PT_BAD="${_PT_BAD:+$_PT_BAD, }'$entry'"
        continue
        ;;
    esac
    if [ "${#entry}" -ne 12 ]; then
      _PT_BAD="${_PT_BAD:+$_PT_BAD, }'$entry'"
      continue
    fi
    case ",$_PT_LIST," in
      *",$entry,"*) ;;
      *) _PT_LIST="${_PT_LIST:+$_PT_LIST,}$entry" ;;
    esac
  done
  if [ -n "$_PT_BAD" ]; then
    return 1
  fi
  if [ -z "$_PT_LIST" ]; then
    _PT_BAD="(no account ID)"
    return 1
  fi
  return 0
}

# _portal_trusted_context
#   The CDK context value, from the first context file that defines the key.
#   rc 0: _PT_CTX_FILE / _PT_CTX_VALUE set; 3: no file defines it; 4: the first
#   file that defines it has an empty/false value (_PT_CTX_FILE set), which
#   the CDK app treats as unset; 1: that file cannot be read (stderr).
_portal_trusted_context() {
  local dir="${PORTAL_TRUSTED_INFRA_DIR:-}" i path nested label out rc
  _PT_CTX_FILE=""
  _PT_CTX_VALUE=""
  for i in 1 2 3; do
    # shellcheck disable=SC2088  # "~/.cdk.json" is the label printed, not a path
    case "$i" in
      1) path="${dir:+$dir/cdk.json}"; nested=1; label="infrastructure/cdk.json" ;;
      2) path="${dir:+$dir/cdk.context.json}"; nested=0; label="infrastructure/cdk.context.json" ;;
      *) path="${HOME:+$HOME/.cdk.json}"; nested=1; label="~/.cdk.json" ;;
    esac
    if [ -z "$path" ] || [ ! -f "$path" ]; then
      continue
    fi
    # A file that never mentions the key cannot define it.
    if ! grep -qs trustedUseCaseAccountIds "$path"; then
      continue
    fi
    if ! command -v python3 >/dev/null 2>&1; then
      echo "❌ $label mentions trustedUseCaseAccountIds, and python3 (needed to read it) is not installed." >&2
      return 1
    fi
    rc=0
    out="$(python3 -c "$_PORTAL_TRUSTED_CONTEXT_PY" "$path" "$nested" 2>&1)" || rc=$?
    case "$rc" in
      0) _PT_CTX_FILE="$label"; _PT_CTX_VALUE="$out"; return 0 ;;
      3) continue ;;
      4) _PT_CTX_FILE="$label"; return 4 ;;
      *)
        echo "❌ Could not read trustedUseCaseAccountIds from $label: $out" >&2
        return 1
        ;;
    esac
  done
  return 3
}

# _portal_trusted_ssm <region>
#   rc 0: _PT_SSM_VALUE holds the parameter value; 3: the parameter does not
#   exist (or is empty); 1: the read failed for another reason (stderr).
_portal_trusted_ssm() {
  local region="${1:-}" out="" err="" rc=0 tmp
  _PT_SSM_VALUE=""
  if ! tmp="$(_portal_trusted_tmp)"; then
    echo "❌ Could not create a temp file to read SSM parameter $PORTAL_TRUSTED_SSM_PARAMETER." >&2
    return 1
  fi
  out="$(aws ssm get-parameter --name "$PORTAL_TRUSTED_SSM_PARAMETER" \
    --query Parameter.Value --output text ${region:+--region "$region"} 2>"$tmp")" || rc=$?
  err="$(_portal_trusted_trim "$(cat "$tmp" 2>/dev/null || true)")"
  rm -f "$tmp"
  if [ "$rc" -ne 0 ]; then
    case "$err" in
      *ParameterNotFound*) return 3 ;;
    esac
    echo "❌ Could not read SSM parameter $PORTAL_TRUSTED_SSM_PARAMETER${region:+ in $region}: ${err:-aws exited $rc}" >&2
    return 1
  fi
  out="$(_portal_trusted_trim "$out")"
  if [ -z "$out" ] || [ "$out" = "None" ]; then
    return 3
  fi
  _PT_SSM_VALUE="$out"
  return 0
}

# _portal_trusted_stack_ids <region> <stack>
#   Reads the deployed template. rc 0: _PT_IDS holds the account IDs of its
#   DDAPortalAccessRole ARNs (first appearance first; possibly empty) and
#   _PT_WILDCARD is 1 when it grants arn:aws:iam::*:role/DDAPortalAccessRole;
#   3: the stack does not exist; 1: the template cannot be read (stderr).
_portal_trusted_stack_ids() {
  local region="${1:-}" stack="$2" out err rc=0
  _PT_IDS=""
  _PT_WILDCARD=0
  if ! out="$(_portal_trusted_tmp)"; then
    echo "❌ Could not create a temp file to read the $stack template." >&2
    return 1
  fi
  if ! err="$(_portal_trusted_tmp)"; then
    rm -f "$out"
    echo "❌ Could not create a temp file to read the $stack template." >&2
    return 1
  fi
  aws cloudformation get-template --stack-name "$stack" ${region:+--region "$region"} \
    --query TemplateBody --output json >"$out" 2>"$err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if grep -qs "does not exist" "$err"; then
      rm -f "$out" "$err"
      return 3
    fi
    echo "❌ Could not read the deployed $stack template${region:+ in $region}: $(_portal_trusted_trim "$(cat "$err" 2>/dev/null || true)")" >&2
    rm -f "$out" "$err"
    return 1
  fi
  # The closing quote keeps a longer role name from matching.
  _PT_IDS="$( { grep -oE ':iam::[[:digit:]]{12}:role/DDAPortalAccessRole"' "$out" || true; } \
    | sed -e 's/^:iam:://' -e 's/:.*$//' \
    | awk '!seen[$0]++ { list = list (list == "" ? "" : ",") $0 } END { print list }')"
  if grep -qs ':iam::\*:role/DDAPortalAccessRole' "$out"; then
    _PT_WILDCARD=1
  fi
  rm -f "$out" "$err"
  return 0
}

# _portal_trusted_sorted <list> -> the list's IDs sorted, comma-separated.
_portal_trusted_sorted() {
  printf '%s\n' "$1" | tr ',' '\n' | sed -e '/^$/d' | LC_ALL=C sort -u \
    | awk '{ list = list (list == "" ? "" : ",") $0 } END { print list }'
}

# _portal_trusted_minus <a> <b> -> the IDs of list <a> that are not in <b>.
_portal_trusted_minus() {
  local rest="$1," entry out=""
  while [ -n "$rest" ]; do
    entry="${rest%%,*}"
    rest="${rest#*,}"
    [ -n "$entry" ] || continue
    case ",$2," in
      *",$entry,"*) ;;
      *) out="${out:+$out,}$entry" ;;
    esac
  done
  printf '%s' "$out"
}

# _portal_trusted_deployed <region>
#   rc 0: _PT_DEPLOYED holds the list the deployed stacks grant; 3:
#   EdgeCVPortalComputeStack is not deployed; 1: the list cannot be
#   determined (reason on stderr).
_portal_trusted_deployed() {
  local region="${1:-}" stack rc compute="" compute_sorted=""
  _PT_DEPLOYED=""
  for stack in $PORTAL_TRUSTED_STACKS; do
    rc=0
    _portal_trusted_stack_ids "$region" "$stack" || rc=$?
    if [ "$stack" = "EdgeCVPortalComputeStack" ]; then
      case "$rc" in
        0) ;;
        3) return 3 ;;
        *) return 1 ;;
      esac
      if [ -z "$_PT_IDS" ]; then
        if [ "$_PT_WILDCARD" -eq 1 ]; then
          echo "❌ EdgeCVPortalComputeStack is deployed with the pre-scoping grant on arn:aws:iam::*:role/DDAPortalAccessRole (every account), so it has no list to keep." >&2
        else
          echo "❌ EdgeCVPortalComputeStack is deployed, but its template grants sts:AssumeRole on no account-scoped DDAPortalAccessRole." >&2
        fi
        return 1
      fi
      compute="$_PT_IDS"
      compute_sorted="$(_portal_trusted_sorted "$compute")"
      continue
    fi
    case "$rc" in
      0) ;;
      3) continue ;;
      *) return 1 ;;
    esac
    if [ -n "$_PT_IDS" ] && [ "$(_portal_trusted_sorted "$_PT_IDS")" != "$compute_sorted" ]; then
      echo "❌ The deployed portal stacks grant different trusted lists: EdgeCVPortalComputeStack $compute; $stack $_PT_IDS." >&2
      return 1
    fi
  done
  _PT_DEPLOYED="$compute"
  return 0
}

# _portal_trusted_remedy <region> -> how to set the list explicitly (stderr).
_portal_trusted_remedy() {
  local region="${1:-<region>}"
  echo "   Set the list explicitly and re-run, for this run:" >&2
  echo "     export TRUSTED_USECASE_ACCOUNT_IDS=<account-id>[,<account-id>...]" >&2
  echo "   or for every deploy (add --overwrite to replace an existing value):" >&2
  echo "     aws ssm put-parameter --name $PORTAL_TRUSTED_SSM_PARAMETER --type String --value <account-id>[,<account-id>...] --region $region" >&2
}

# portal_trusted_accounts_resolve <region>
#   Prints "<source> <ids>" (source: context:<file>, env, ssm or deployed) or
#   "none" (nothing configured, EdgeCVPortalComputeStack not deployed).
#   rc 1, with the reason and the remedy on stderr, when the list cannot be
#   determined. Order and rules: see the header.
portal_trusted_accounts_resolve() {
  local region="${1:-}" rc env_value

  rc=0
  _portal_trusted_context || rc=$?
  case "$rc" in
    0)
      if ! _portal_trusted_normalize "$_PT_CTX_VALUE"; then
        echo "❌ trustedUseCaseAccountIds in $_PT_CTX_FILE is not a comma-separated list of 12-digit account IDs: $_PT_BAD." >&2
        echo "   Fix it there (it overrides TRUSTED_USECASE_ACCOUNT_IDS and the SSM parameter)." >&2
        return 1
      fi
      printf 'context:%s %s\n' "$_PT_CTX_FILE" "$_PT_LIST"
      return 0
      ;;
    3|4) ;;
    *) return 1 ;;
  esac

  env_value="$(_portal_trusted_trim "${TRUSTED_USECASE_ACCOUNT_IDS:-}")"
  if [ -n "$env_value" ]; then
    if ! _portal_trusted_normalize "$env_value"; then
      echo "❌ TRUSTED_USECASE_ACCOUNT_IDS is not a comma-separated list of 12-digit account IDs: $_PT_BAD." >&2
      echo "   Fix it, e.g. export TRUSTED_USECASE_ACCOUNT_IDS=111111111111,222222222222" >&2
      return 1
    fi
    printf 'env %s\n' "$_PT_LIST"
    return 0
  fi

  rc=0
  _portal_trusted_ssm "$region" || rc=$?
  case "$rc" in
    0)
      if ! _portal_trusted_normalize "$_PT_SSM_VALUE"; then
        echo "❌ SSM parameter $PORTAL_TRUSTED_SSM_PARAMETER is not a comma-separated list of 12-digit account IDs: $_PT_BAD." >&2
        echo "   Fix it: aws ssm put-parameter --name $PORTAL_TRUSTED_SSM_PARAMETER --type String --overwrite --value <account-id>[,<account-id>...] --region ${region:-<region>}" >&2
        return 1
      fi
      printf 'ssm %s\n' "$_PT_LIST"
      return 0
      ;;
    3) ;;
    *)
      echo "   Fix the error above, or set TRUSTED_USECASE_ACCOUNT_IDS for this run (it is used instead of the parameter)." >&2
      return 1
      ;;
  esac

  rc=0
  _portal_trusted_deployed "$region" || rc=$?
  case "$rc" in
    0)
      printf 'deployed %s\n' "$_PT_DEPLOYED"
      return 0
      ;;
    3)
      printf 'none\n'
      return 0
      ;;
    *)
      echo "   No trusted UseCase account list is configured, and the deployed one could not be determined." >&2
      _portal_trusted_remedy "$region"
      return 1
      ;;
  esac
}

# portal_trusted_accounts_label <source> -> how the list was chosen, for the
# deploy output and the Deployment Summary.
portal_trusted_accounts_label() {
  case "${1:-}" in
    context:*) printf 'CDK context trustedUseCaseAccountIds in %s' "${1#context:}" ;;
    env) printf 'TRUSTED_USECASE_ACCOUNT_IDS' ;;
    ssm) printf 'SSM parameter %s' "$PORTAL_TRUSTED_SSM_PARAMETER" ;;
    deployed) printf 'the deployed portal stacks (nothing configured)' ;;
    default) printf 'first deploy: the deploying account' ;;
    fresh-default) printf 'fresh install: the deploying account' ;;
    *) printf '%s' "${1:-}" ;;
  esac
}

# portal_trusted_accounts_compare <region> <ids>
#   Prints, indented, how <ids> differs from what each deployed portal stack
#   grants: "matches the deployed stacks", or per stack
#   "⚠️  <stack>: removes <ids>[; adds <ids> (…)]",
#   "⚠️  <stack>: adds <ids> (the portal may then assume their DDAPortalAccessRole)"
#   or "not compared (<reason>)". An addition is flagged like a removal: it
#   widens what the portal may do in that account. Prints nothing when
#   EdgeCVPortalComputeStack is not deployed. Never fails.
portal_trusted_accounts_compare() {
  local region="${1:-}" ids="${2:-}" stack rc adds removes lines=""
  local adds_note=" (the portal may then assume their DDAPortalAccessRole)"
  for stack in $PORTAL_TRUSTED_STACKS; do
    rc=0
    _portal_trusted_stack_ids "$region" "$stack" 2>/dev/null || rc=$?
    if [ "$rc" -eq 3 ]; then
      if [ "$stack" = "EdgeCVPortalComputeStack" ]; then
        return 0
      fi
      continue
    fi
    if [ "$rc" -ne 0 ]; then
      lines="${lines}  $stack: not compared (its template could not be read)"$'\n'
      continue
    fi
    if [ -z "$_PT_IDS" ]; then
      lines="${lines}  $stack: not compared (no account-scoped DDAPortalAccessRole grant deployed)"$'\n'
      continue
    fi
    adds="$(_portal_trusted_minus "$ids" "$_PT_IDS")"
    removes="$(_portal_trusted_minus "$_PT_IDS" "$ids")"
    if [ -n "$removes" ]; then
      lines="${lines}  ⚠️  $stack: removes $removes${adds:+; adds $adds$adds_note}"$'\n'
    elif [ -n "$adds" ]; then
      lines="${lines}  ⚠️  $stack: adds $adds$adds_note"$'\n'
    fi
  done
  if [ -z "$lines" ]; then
    printf '  matches the deployed stacks\n'
  else
    printf '%s' "$lines"
  fi
  return 0
}

# portal_trusted_accounts_store <region> <ids>
#   Creates the SSM parameter with <ids>; never overwrites an existing one.
#   rc 0 stored; 2 the parameter already exists; 1 any other failure (the
#   AWS CLI error on stderr).
portal_trusted_accounts_store() {
  local region="${1:-}" ids="${2:-}" err rc=0
  if ! err="$(_portal_trusted_tmp)"; then
    echo "❌ Could not create a temp file to store SSM parameter $PORTAL_TRUSTED_SSM_PARAMETER." >&2
    return 1
  fi
  aws ssm put-parameter --name "$PORTAL_TRUSTED_SSM_PARAMETER" --type String --value "$ids" \
    --description "DDA portal trusted UseCase account IDs (comma-separated)" \
    ${region:+--region "$region"} >/dev/null 2>"$err" || rc=$?
  if [ "$rc" -eq 0 ]; then
    rm -f "$err"
    return 0
  fi
  if grep -qs ParameterAlreadyExists "$err"; then
    rm -f "$err"
    return 2
  fi
  echo "⚠️  Could not store SSM parameter $PORTAL_TRUSTED_SSM_PARAMETER${region:+ in $region}: $(_portal_trusted_trim "$(cat "$err" 2>/dev/null || true)")" >&2
  rm -f "$err"
  return 1
}
