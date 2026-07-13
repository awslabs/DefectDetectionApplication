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

"""Vendored third-party-style packages for the workflow engine.

``workflow_core`` is vendored from
``edge-cv-portal/backend/layers/workflow_core/python/workflow_core``.
Do not edit the vendored files here; re-vendor with ``re_vendor.sh``
(see ``README.md`` in this directory).

Import it as::

    from workflow_engine.vendor import workflow_core

All of workflow_core's internal imports are package-relative, so it works
unmodified at this nesting. Its only external dependency is ``jsonschema``,
already pinned in the LocalServer ``requirements.txt``.
"""
