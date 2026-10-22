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
"""Edge_MCP_Server: the Model Context Protocol endpoint of the LocalServer.

The package runs in two interpreters (spec: .kiro/specs/mcp-servers/,
design §5). The Backend_API process imports the backend-side modules
(``config``, ``supervisor``, ``log_relay``, ``status`` and the ``/mcp``
proxy), which use only the standard library and the backend's own pinned
packages. The supervised child process runs ``mcp_edge.child`` from the
in-image virtualenv, the only place the ``mcp`` SDK is installed. Nothing
under ``mcp_edge/child/`` is imported by the Backend_API process.

This module is imported by both, so it holds constants and one entry point,
``start_supervisor``, whose import of the backend-side modules is lazy.
"""

# Semver of the edge catalog; serverInfo.version is "<this>+<component>".
__version__ = "1.0.0"

# The only phase gate: an edge catalog spec whose phase is not listed here is
# neither listed nor accepted.
DELIVERED_PHASES = (1,)

# The in-image virtualenv's interpreter (built from requirements.lock.txt).
VENV_PYTHON = "/opt/dda/mcp-venv/bin/python"

# The loopback port the child listens on.
CHILD_PORT = 5790


def start_supervisor(backend_base_url, features=(), component_version="unknown"):
    """Start the Edge MCP supervisor of the Backend_API process (app.py).
    Backend process only; see ``mcp_edge.supervisor.start_supervisor``."""
    from mcp_edge import supervisor  # noqa: WPS433 — lazy: the child never needs it

    return supervisor.start_supervisor(backend_base_url, features=features,
                                       component_version=component_version)
