"""The Portal assistant backend (spec: .kiro/specs/portal-ai-assistant/).

An agent loop over the Bedrock model of the Portal's `Bedrock_Configuration`
that performs every look-up and task through the Portal MCP tool catalog and
pipeline (`mcp_portal.pipeline.call_tool`) as the signed-in user. Two Lambda
entry points: `assistant.api.handler` (the REST routes) and
`assistant.worker.handler` (one Turn, asynchronously).
"""

__version__ = '1.0.0'

SERVER_NAME = 'dda-portal-assistant'

# Phase gate of the mutating tools (design D8, Requirement 15). Phase 1 ships
# with actions undelivered: no mutating tool is ever exposed to the model and
# the Actions_Switch is hidden and forced off. Phase 2 flips this to True and
# fills DESTRUCTIVE_ALLOWLIST from the owner's sign-off (Requirement 15.2).
ACTIONS_DELIVERED = False

# Destructive catalog tools the assistant may propose once actions are
# delivered (owner decision 6: an allow-list, approved at Phase 2 sign-off).
DESTRUCTIVE_ALLOWLIST: tuple = ()

# Names of the assistant-local tools (design §5); never catalog tools.
LOCAL_TOOL_NAMES = ('guide_read_section', 'journey_status', 'find_tools')

# Setting keys in SETTINGS_TABLE.
CONFIGURATION_SETTING_KEY = 'assistant_configuration'
MODEL_CAPABILITY_SETTING_KEY = 'assistant_model_capability'
