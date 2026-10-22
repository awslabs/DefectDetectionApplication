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
"""The Edge_MCP_Server child process (``python -m mcp_edge.child``).

Runs from the in-image virtualenv (``mcp==2.3.0``) as a loopback process
supervised by the Backend_API (spec: .kiro/specs/mcp-servers/, design §5).
Nothing in this package imports ``utils``, ``local_auth`` or ``dao``: the
child reaches the device only through the Backend_API's HTTP routes, with
the caller's own credential, so it can never read the credential cache, the
session secret or the authorization settings (Requirement 10.8).
"""
