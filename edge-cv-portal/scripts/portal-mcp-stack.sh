#!/bin/bash
# Opt-out guard of the Portal_MCP_Server stack (mcp-servers design D33).
#
# Sourced by deploy-infrastructure.sh directly before its `cdk deploy`.
# Disabling the MCP server on a deployed portal is two steps, because
# `cdk deploy --all` never deletes a stack that merely left the app, and the
# producing stacks cannot drop the handler-ARN exports a deployed
# EdgeCVPortalMcpStack still imports:
#
#   1. cd infrastructure && npx cdk destroy EdgeCVPortalMcpStack --force
#   2. DEPLOY_MCP_SERVER=false ./deploy-infrastructure.sh
#
# DEPLOY_MCP_SERVER is off iff its value, trimmed and lower-cased, is exactly
# `false` (the mcpServerEnabled rule of lib/context-helpers.ts: default on).
# Any other value, unset or empty included, leaves the CDK default (on).

MCP_STACK_NAME="EdgeCVPortalMcpStack"

# _portal_mcp_value_is_off <value>: 0 when the value turns the MCP stack off.
_portal_mcp_value_is_off() {
  local value
  value="$(printf '%s' "${1:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
  [ "$value" = "false" ]
}

# portal_mcp_optout_guard <region> <DEPLOY_MCP_SERVER value>
#   Off: refuses (return 1) while EdgeCVPortalMcpStack still exists, or when
#   its existence cannot be read; otherwise appends `-c deployMcpServer=false`
#   to CDK_CONTEXT_ARGS. Any other value: nothing to do, no AWS call.
portal_mcp_optout_guard() {
  local region="${1:-}" value="${2:-}" err rc
  if ! _portal_mcp_value_is_off "$value"; then
    return 0
  fi
  err="$(aws cloudformation describe-stacks --stack-name "$MCP_STACK_NAME" \
    ${region:+--region "$region"} --query 'Stacks[0].StackStatus' --output text 2>&1 >/dev/null)" \
    && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "❌ DEPLOY_MCP_SERVER=false, but $MCP_STACK_NAME is still deployed." >&2
    echo "   Remove it first (it also deletes the MCP app client, resource server and dispatch-map parameter):" >&2
    echo "     cd infrastructure && npx cdk destroy $MCP_STACK_NAME --force" >&2
    echo "   then re-run: DEPLOY_MCP_SERVER=false ./deploy-infrastructure.sh" >&2
    return 1
  fi
  case "$err" in
    *"does not exist"*) ;;
    *)
      echo "❌ Could not read whether $MCP_STACK_NAME is deployed (aws cloudformation describe-stacks failed)." >&2
      echo "   Not deploying with DEPLOY_MCP_SERVER=false until that can be checked." >&2
      return 1
      ;;
  esac
  echo "Portal_MCP_Server: off (DEPLOY_MCP_SERVER=false; $MCP_STACK_NAME is not deployed)"
  CDK_CONTEXT_ARGS="${CDK_CONTEXT_ARGS:-} -c deployMcpServer=false"
  return 0
}
