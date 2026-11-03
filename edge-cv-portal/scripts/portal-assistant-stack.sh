#!/bin/bash
# Opt-out guard of the Portal assistant stack (portal-ai-assistant design D8,
# §10; the same two-step rule as the MCP server's scripts/portal-mcp-stack.sh).
#
# Sourced by deploy-infrastructure.sh directly before its `cdk deploy`.
# Disabling the assistant on a deployed portal is two steps, because
# `cdk deploy --all` never deletes a stack that merely left the app, and the
# producing stacks cannot drop the handler-ARN exports a deployed
# EdgeCVPortalAssistantStack still imports:
#
#   1. cd infrastructure && npx cdk destroy EdgeCVPortalAssistantStack --force \
#        -c portalRegistryEnforced=<deployed value>
#   2. DEPLOY_ASSISTANT=false ./deploy-infrastructure.sh
#
# The conversations table and its KMS key are retained by the destroy; the
# ADMIN_GUIDE says how to delete them by hand.
#
# DEPLOY_ASSISTANT is off iff its value, trimmed and lower-cased, is exactly
# `false` (the assistantEnabled rule of lib/context-helpers.ts: default on).
# Any other value, unset or empty included, leaves the CDK default (on).

ASSISTANT_STACK_NAME="EdgeCVPortalAssistantStack"

# _portal_assistant_value_is_off <value>: 0 when the value turns the stack off.
_portal_assistant_value_is_off() {
  local value
  value="$(printf '%s' "${1:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
  [ "$value" = "false" ]
}

# portal_assistant_optout_guard <region> <DEPLOY_ASSISTANT value>
#   Off: refuses (return 1) while EdgeCVPortalAssistantStack still exists, or
#   when its existence cannot be read; otherwise appends
#   `-c deployAssistant=false` to CDK_CONTEXT_ARGS. Any other value: nothing
#   to do, no AWS call.
portal_assistant_optout_guard() {
  local region="${1:-}" value="${2:-}" err rc
  if ! _portal_assistant_value_is_off "$value"; then
    return 0
  fi
  err="$(aws cloudformation describe-stacks --stack-name "$ASSISTANT_STACK_NAME" \
    ${region:+--region "$region"} --query 'Stacks[0].StackStatus' --output text 2>&1 >/dev/null)" \
    && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then
    local ctx="${CDK_CONTEXT_ARGS:-}"
    ctx="${ctx#"${ctx%%[![:space:]]*}"}"
    echo "❌ DEPLOY_ASSISTANT=false, but $ASSISTANT_STACK_NAME is still deployed." >&2
    echo "   Remove it first (the conversations table and its key are retained):" >&2
    echo "     cd infrastructure && ${region:+AWS_REGION=$region }npx cdk destroy $ASSISTANT_STACK_NAME --force${ctx:+ $ctx}" >&2
    echo "   then re-run: DEPLOY_ASSISTANT=false ./deploy-infrastructure.sh" >&2
    return 1
  fi
  case "$err" in
    *"does not exist"*) ;;
    *)
      echo "❌ Could not read whether $ASSISTANT_STACK_NAME is deployed (aws cloudformation describe-stacks failed)." >&2
      echo "   Not deploying with DEPLOY_ASSISTANT=false until that can be checked." >&2
      return 1
      ;;
  esac
  echo "Portal assistant: off (DEPLOY_ASSISTANT=false; $ASSISTANT_STACK_NAME is not deployed)"
  CDK_CONTEXT_ARGS="${CDK_CONTEXT_ARGS:-} -c deployAssistant=false"
  return 0
}
