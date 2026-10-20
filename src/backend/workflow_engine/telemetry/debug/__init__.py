#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""The Debug_Agent of the workflow debug mode (workflow-debug-mode design §5).

Switched on per (device, workflow) through the ``dda-workflow-debug`` named
shadow, the agent sends every finished run of the debugged workflow (its
timing, trigger, nodes, outputs, model readiness, artifacts and the run's
log lines), the lines between runs, the discarded trigger firings and a
heartbeat as ``dda.debug.v1`` messages on
``dda/debug/{usecaseId}/{workflowId}/record``. It observes the engine only
through the shared :mod:`workflow_engine.telemetry.hooks`; nothing here is
imported by the engine.

Modules: :mod:`redact` (environment-neutral, copied to the Portal),
:mod:`runlog` (the ``run.log`` parser), :mod:`recorder` (the ``run``
record), :mod:`batcher` (envelopes, truncation, rate caps, spool back-off),
:mod:`sessions` (the per-Pair state machine), :mod:`logtap` (the log handler
and the level guard), :mod:`images` (the optional frame upload) and
:mod:`agent` (the thread that ties them together).
"""
