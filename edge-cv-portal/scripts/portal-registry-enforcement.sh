#!/usr/bin/env bash
#
# Portal_Identity enforcement value for a portal deploy
# (portal-jwt-role-privilege-escalation, design Decision 4).
#
# Sourced by the scripts that deploy portal stacks: deploy-infrastructure.sh,
# deploy-frontend.sh (its ComputeStack redeploy) and
# infrastructure/deploy_portal_fixes.sh.
#
# PORTAL_REGISTRY_ENFORCED reaches every portal handler's Lambda environment
# through the CDK context value `portalRegistryEnforced`, which the CDK
# resolves default-OFF (lib/context-helpers.ts). A deploy that omits it
# therefore switches enforcement off for every stack it touches, which is how
# a routine deploy turned production enforcement off on 2026-09-26. Each
# deploy script now asks this helper for the value to pass:
#
#   1. PORTAL_REGISTRY_ENFORCED set and non-empty -> that value, unchanged
#      (the CDK normalizes it; anything but 1/true/yes/on/enabled means off).
#      This is how an operator changes it: =true turns enforcement on, =false
#      turns it off.
#   2. Otherwise -> the value the deployed portal handlers carry now: 'true'
#      when any EdgeCVPortal* handler has it on (a mixed state is the
#      signature of this very bug, so it resolves to on), 'false' when they
#      all have it off.
#   3. Otherwise, when no portal handler is deployed yet -> no value, so the
#      CDK default applies: off, as a fresh install requires until the
#      registry is backfilled.
#
# If the deployed value cannot be read (credentials, permissions, throttling),
# the helper fails rather than guess, because guessing "off" is the bug.
#
# Safe under `set -euo pipefail` and bash 3.2 (macOS).

# The CLI truthy set, identical to context-helpers.portalRegistryEnforced and
# shared_utils._ENFORCEMENT_TRUE_VALUES.
_portal_registry_value_is_on() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
    1|true|yes|on|enabled) return 0 ;;
    *) return 1 ;;
  esac
}

# portal_registry_deployed_values [region]
#   The PORTAL_REGISTRY_ENFORCED values the deployed EdgeCVPortal* handlers
#   carry, one per line. Succeeds with no output when none is deployed;
#   fails when the lookup itself fails.
portal_registry_deployed_values() {
  local query out
  query="Functions[?starts_with(FunctionName, 'EdgeCVPortal')].Environment.Variables.PORTAL_REGISTRY_ENFORCED"
  if [ -n "${1:-}" ]; then
    out=$(aws lambda list-functions --region "$1" --query "$query" --output text) || return 1
  else
    out=$(aws lambda list-functions --query "$query" --output text) || return 1
  fi
  printf '%s\n' "$out" | tr '\t' '\n' | sed -e '/^[[:space:]]*$/d' -e '/^None$/d'
}

# portal_registry_enforced_for_deploy [region]
#   Prints the value to pass as `-c portalRegistryEnforced=<value>`, or
#   nothing when the CDK default should apply (nothing deployed yet). Explains
#   the decision on stderr. Returns 1 when the deployed value is needed but
#   cannot be read.
portal_registry_enforced_for_deploy() {
  local region="${1:-}" requested="${PORTAL_REGISTRY_ENFORCED:-}"
  local values="" value on=0 off=0 deployed=""

  if values=$(portal_registry_deployed_values "$region"); then
    while IFS= read -r value; do
      [ -z "$value" ] && continue
      if _portal_registry_value_is_on "$value"; then
        on=$((on + 1))
      else
        off=$((off + 1))
      fi
    done <<< "$values"
    if [ "$on" -gt 0 ]; then
      deployed=true
    elif [ "$off" -gt 0 ]; then
      deployed=false
    fi
  elif [ -z "$requested" ]; then
    echo "❌ Could not read the deployed Portal_Identity enforcement value (aws lambda list-functions failed)." >&2
    echo "   Set PORTAL_REGISTRY_ENFORCED=true or PORTAL_REGISTRY_ENFORCED=false explicitly and re-run." >&2
    return 1
  fi

  if [ -n "$requested" ]; then
    local wanted=false
    _portal_registry_value_is_on "$requested" && wanted=true
    echo "🔐 Portal_Identity enforcement requested: PORTAL_REGISTRY_ENFORCED=$requested" >&2
    if [ -n "$deployed" ] && [ "$deployed" != "$wanted" ]; then
      echo "   This changes enforcement from '$deployed' (deployed now) to '$wanted'." >&2
    fi
    printf '%s\n' "$requested"
    return 0
  fi

  if [ -z "$deployed" ]; then
    echo "🔐 Portal_Identity enforcement: no portal handler is deployed yet; the CDK default (off) applies." >&2
    return 0
  fi

  echo "🔐 Portal_Identity enforcement: keeping the deployed value '$deployed' ($on handler(s) on, $off off)." >&2
  echo "   Set PORTAL_REGISTRY_ENFORCED=true or =false to change it." >&2
  printf '%s\n' "$deployed"
}
