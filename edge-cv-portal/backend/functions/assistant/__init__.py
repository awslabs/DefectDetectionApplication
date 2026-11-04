"""The Portal assistant backend (spec: .kiro/specs/portal-ai-assistant/).

An agent loop over the Bedrock model of the Portal's `Bedrock_Configuration`
that performs every look-up and task through the Portal MCP tool catalog and
pipeline (`mcp_portal.pipeline.call_tool`) as the signed-in user. Two Lambda
entry points: `assistant.api.handler` (the REST routes) and
`assistant.worker.handler` (one Turn, asynchronously).
"""

__version__ = '1.1.0'

SERVER_NAME = 'dda-portal-assistant'

# Phase gate of the mutating tools (design D8, Requirement 15). Phase 1 shipped
# with actions undelivered: no mutating tool exposed to the model and the
# Actions_Switch hidden and forced off. Phase 2 delivers them (the owner's
# allow-list sign-off, decision 3a of 2026-11-04): with the Actions_Switch on,
# the non-destructive mutating tools of the current page's domain are offered
# as proposals the user confirms in the Portal (design §8.3), and destructive
# tools only from DESTRUCTIVE_ALLOWLIST below.
ACTIONS_DELIVERED = True

# Destructive catalog tools the assistant may propose (owner decision 6: an
# allow-list approved at Phase 2 sign-off, Requirement 15.2). Signed off empty
# (decision 3a, 2026-11-04): the assistant proposes non-destructive mutating
# tools only, each behind its Action_Confirmation. A catalog phase delivered
# later follows the same rule with no change here. Every name must be a
# destructive tool of the catalog (tests/assistant/test_units.py pins that).
# A tool outside this list is never offered to the model, even with the
# Actions_Switch on; the user does it in the Portal or through the MCP server
# instead. Changing the list is a code change plus a deploy, on purpose: it is
# the owner's sign-off, not a runtime setting.
DESTRUCTIVE_ALLOWLIST: tuple = ()

# Names of the assistant-local tools (design §5); never catalog tools.
LOCAL_TOOL_NAMES = ('guide_read_section', 'journey_status', 'find_tools')

# Setting keys in SETTINGS_TABLE.
CONFIGURATION_SETTING_KEY = 'assistant_configuration'
MODEL_CAPABILITY_SETTING_KEY = 'assistant_model_capability'
