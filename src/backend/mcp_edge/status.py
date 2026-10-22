# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The ``GET /mcp/status`` document (backend process; design §5
``status.py``, Data Models "``GET /mcp/status``"; Requirement 12.8)."""
from typing import Optional

from mcp_edge import DELIVERED_PHASES

ENDPOINT = "/mcp"
TRANSPORT = "streamable-http"
# The MCP protocol revisions the child negotiates, newest first.
PROTOCOL_REVISIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")


def build_status(config, supervisor, version: str,
                 component_version: Optional[str]) -> dict:
    """The status document from the configuration and the supervisor. Pure:
    reads attributes only. ``supervisor`` may be None (never started)."""
    state = config.state
    return {
        "enabled": state.server_enabled,
        "mutatingToolsEnabled": state.mutating_enabled,
        "listening": bool(supervisor is not None and supervisor.listening),
        "endpoint": ENDPOINT,
        "transport": TRANSPORT,
        "protocolRevisions": list(PROTOCOL_REVISIONS),
        "serverVersion": version,
        "deliveredPhases": list(DELIVERED_PHASES),
        "componentVersion": component_version,
        "features": list(supervisor.features) if supervisor is not None else [],
        "restartCount": supervisor.restart_count if supervisor is not None else 0,
        "lastExitCode": supervisor.last_exit_code if supervisor is not None else None,
        "lastError": supervisor.last_error if supervisor is not None else None,
    }
