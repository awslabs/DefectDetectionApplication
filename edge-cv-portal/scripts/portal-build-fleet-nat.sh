#!/usr/bin/env bash
#
# The Build Fleet NAT gateway setting for a portal deploy.
#
# Sourced by the two scripts that deploy EdgeCVPortalBuildFleetStack:
# deploy-infrastructure.sh and infrastructure/deploy_portal_fixes.sh.
#
# The CDK context value `buildFleetNatGateway` adds one NAT gateway to the
# build VPC (lib/build-fleet-stack.ts), for accounts where VPC Block Public
# Access blocks the build instances' internet gateway traffic. The CDK
# resolves it default-OFF (lib/context-helpers.ts), so a deploy that omitted
# it would remove a gateway an operator turned on. Each deploy script asks
# this helper for the value to pass:
#
#   1. BUILD_FLEET_NAT_GATEWAY set and non-empty -> 'true' for
#      1/true/yes/on/enabled (trimmed, any case), 'false' for anything else.
#      This is how an operator changes it: =true turns the gateway on,
#      =false removes it.
#   2. Otherwise -> the deployed state: 'true' while EdgeCVPortalBuildFleetStack
#      has its BuildNatGatewayId output, 'false' when it has none.
#   3. Otherwise, when that stack is not deployed yet -> no value, so the CDK
#      default applies: off.
#
# If the deployed state cannot be read (credentials, permissions, throttling),
# the helper fails rather than guess, because guessing "off" removes the
# gateway. A hand-run `cdk deploy` or `cdk diff` must pass
# `-c buildFleetNatGateway=true` itself while the gateway is on.
#
# Safe under `set -euo pipefail` and bash 3.2 (macOS).

PORTAL_BUILD_FLEET_STACK="EdgeCVPortalBuildFleetStack"
PORTAL_BUILD_FLEET_NAT_OUTPUT="BuildNatGatewayId"

# The CLI truthy set, identical to context-helpers.buildFleetNatGatewayEnabled.
_portal_build_fleet_nat_value_is_on() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
    1|true|yes|on|enabled) return 0 ;;
    *) return 1 ;;
  esac
}

# portal_build_fleet_nat_deployed [region]
#   Prints 'true' while the Build Fleet stack has the NAT gateway output and
#   'false' when it has none. Prints nothing and succeeds when the stack does
#   not exist; fails when it cannot be read.
portal_build_fleet_nat_deployed() {
  local region="${1:-}" err out rc
  err="$(mktemp "${TMPDIR:-/tmp}/portal-nat.XXXXXX")" || return 1
  out="$(aws cloudformation describe-stacks --stack-name "$PORTAL_BUILD_FLEET_STACK" \
    ${region:+--region "$region"} \
    --query "Stacks[0].Outputs[?OutputKey=='$PORTAL_BUILD_FLEET_NAT_OUTPUT'].OutputValue" \
    --output text 2>"$err")" && rc=0 || rc=$?
  if [ "$rc" -ne 0 ]; then
    if grep -q "does not exist" "$err"; then
      rm -f "$err"
      return 0
    fi
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  case "$(printf '%s' "$out" | tr -d '[:space:]')" in
    ""|None) printf '%s\n' false ;;
    *) printf '%s\n' true ;;
  esac
}

# portal_build_fleet_nat_for_deploy [region]
#   Prints the value to pass as `-c buildFleetNatGateway=<value>` ('true' or
#   'false'), or nothing when the CDK default should apply (the Build Fleet
#   stack is not deployed yet). Explains the decision on stderr. Returns 1
#   when the deployed state is needed but cannot be read.
portal_build_fleet_nat_for_deploy() {
  local region="${1:-}" requested="${BUILD_FLEET_NAT_GATEWAY:-}" deployed="" readable=true

  deployed="$(portal_build_fleet_nat_deployed "$region")" || readable=false

  if [ -n "$requested" ]; then
    local wanted=false
    _portal_build_fleet_nat_value_is_on "$requested" && wanted=true
    echo "Build Fleet NAT gateway requested: BUILD_FLEET_NAT_GATEWAY=$requested ($wanted)" >&2
    if [ "$readable" = true ] && [ -n "$deployed" ] && [ "$deployed" != "$wanted" ]; then
      echo "   This changes the Build Fleet NAT gateway from '$deployed' (deployed now) to '$wanted'." >&2
    fi
    printf '%s\n' "$wanted"
    return 0
  fi

  if [ "$readable" != true ]; then
    echo "❌ Could not read whether the Build Fleet NAT gateway is deployed (aws cloudformation describe-stacks --stack-name $PORTAL_BUILD_FLEET_STACK failed)." >&2
    echo "   Set BUILD_FLEET_NAT_GATEWAY=true or BUILD_FLEET_NAT_GATEWAY=false explicitly and re-run." >&2
    return 1
  fi

  if [ -z "$deployed" ]; then
    echo "Build Fleet NAT gateway: $PORTAL_BUILD_FLEET_STACK is not deployed yet; the CDK default (off) applies." >&2
    return 0
  fi

  echo "Build Fleet NAT gateway: keeping the deployed value '$deployed'. Set BUILD_FLEET_NAT_GATEWAY=true or =false to change it." >&2
  printf '%s\n' "$deployed"
}
