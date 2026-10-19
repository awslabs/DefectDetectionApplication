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

"""Device-side telemetry shared by the workflow dashboards and the workflow
debug mode (workflow-telemetry-dashboards design §5.2).

The modules in this package are observers of the workflow engine, never
participants: the engine calls the ``notify_*`` hooks of :mod:`hooks` and
carries on, every hook contains and rate-limits observer failures, and the
agents built on top of them (the Telemetry_Agent and the Debug_Agent under
:mod:`debug`) run on their own daemon threads with their own IPC publisher
queues. Nothing here is imported by the engine except :mod:`hooks`.

Importable without the Greengrass SDK: ``awsiot`` and the IPC client are
imported lazily, inside the methods that need them, so the host unit tests
run with fakes.
"""
