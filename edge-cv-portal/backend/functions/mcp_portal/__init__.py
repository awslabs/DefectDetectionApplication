"""Portal_MCP_Server: the Model Context Protocol endpoint of the DDA Portal.

One Lambda behind its own HTTP API serves five views of one declarative tool
catalog (`catalog.py`) and dispatches every tool call to the existing handler
Lambda of the matching REST route (spec: .kiro/specs/mcp-servers/).
"""

# Semver of this package; also the default of MCP_SERVER_VERSION.
__version__ = '1.0.0'

# The only phase gate: a catalog spec whose phase is not listed here is neither
# listed nor accepted. Bumped to (1, 2) and (1, 2, 3) by the release that ships
# each phase.
DELIVERED_PHASES: tuple = (1,)

# MCP protocol revisions the server negotiates, newest first.
PROTOCOL_REVISIONS = ('2026-07-28', '2025-11-25', '2025-06-18', '2025-03-26')

# One endpoint per view, plus the aggregate endpoint.
VIEWS = ('data', 'models', 'devices', 'workflows', 'admin')
