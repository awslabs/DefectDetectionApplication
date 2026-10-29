#!/usr/bin/env bash
#
# The DDA permissions boundary for a portal deploy.
#
# iam/setup-account.sh bootstraps the Portal Account with the customer managed
# policy dda-portal-permissions-boundary as the permissions boundary of the
# CloudFormation execution role (cdk bootstrap --custom-permissions-boundary).
# That boundary refuses to create a role, or change a role's boundary, unless
# the role carries the same boundary, so every role the portal app creates
# must carry it: bin/app.ts puts it on every role when the CDK context value
# ddaPermissionsBoundary names it (lib/permissions-boundary.ts). Sourced by the
# scripts that deploy the portal app (deploy-infrastructure.sh,
# deploy-frontend.sh, infrastructure/deploy-auth.sh,
# infrastructure/deploy_portal_fixes.sh), which ask it for the name:
#
#   1. DDA_PERMISSIONS_BOUNDARY set and non-empty -> that policy name, or no
#      boundary for "none".
#   2. Otherwise -> the boundary the CDKToolkit stack was bootstrapped with
#      (its InputPermissionsBoundary parameter); no boundary when it has none.
#
# If the CDKToolkit stack cannot be read, the helper fails rather than guess:
# deploying without the boundary into an account bootstrapped with it fails on
# the first role anyway.
#
# Safe under `set -euo pipefail` and bash 3.2 (macOS).

# portal_permissions_boundary [region]
#   Prints the policy name to pass as `-c ddaPermissionsBoundary=<name>`, or
#   nothing for no boundary. Explains the decision on stderr. Returns 1 when
#   the bootstrap cannot be read.
portal_permissions_boundary() {
  local region="${1:-}" requested="${DDA_PERMISSIONS_BOUNDARY:-}" out
  local query="Stacks[0].Parameters[?ParameterKey=='InputPermissionsBoundary'].ParameterValue | [0]"
  if [ -n "$requested" ]; then
    if [ "$requested" = "none" ]; then
      echo "🔐 Permissions boundary: none (DDA_PERMISSIONS_BOUNDARY=none)" >&2
      return 0
    fi
    echo "🔐 Permissions boundary: $requested (DDA_PERMISSIONS_BOUNDARY)" >&2
    printf '%s\n' "$requested"
    return 0
  fi
  local err
  err=$(mktemp)
  if [ -n "$region" ]; then
    out=$(aws cloudformation describe-stacks --stack-name CDKToolkit --region "$region" \
      --query "$query" --output text 2>"$err") || out="__failed__"
  else
    out=$(aws cloudformation describe-stacks --stack-name CDKToolkit \
      --query "$query" --output text 2>"$err") || out="__failed__"
  fi
  # No CDKToolkit stack: not bootstrapped (yet), so no bootstrap boundary.
  if [ "$out" = "__failed__" ] && grep -q "does not exist" "$err"; then
    out=""
  fi
  rm -f "$err"
  case "$out" in
    __failed__)
      echo "❌ Could not read the CDKToolkit stack to find its permissions boundary." >&2
      echo "   Set DDA_PERMISSIONS_BOUNDARY=<policy name> (or =none) and re-run." >&2
      return 1 ;;
    ""|None|null)
      echo "🔐 Permissions boundary: none (the CDK bootstrap has none)" >&2
      return 0 ;;
  esac
  echo "🔐 Permissions boundary: $out (from the CDK bootstrap)" >&2
  printf '%s\n' "$out"
}
