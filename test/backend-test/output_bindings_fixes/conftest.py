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
"""Shared fixtures for the workflow-output-bindings-fixes test suite
(task 1 exploration tests and task 2 preservation tests).

Makes the workflow_engine test utilities importable (they live in the sibling
``test/backend-test/workflow_engine/`` directory, which pytest only puts on
sys.path for files inside it) and provides the executor-harness fixtures the
engine-level cases (4-6) share: a temp sqlite session factory, a stubbed
GStreamer registry scan, and a tmp-dir ``_WORKFLOW_CAPTURE_ROOT`` so runs
never touch the real ``/aws_dda`` tree.
"""
import os
import sys
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_TESTS = os.path.join(os.path.dirname(_HERE), "workflow_engine")
for _path in (_HERE, _ENGINE_TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from workflow_engine_test_utils import make_session_factory  # noqa: E402

from workflow_engine import gst_plugins  # noqa: E402
from workflow_engine import pipeline_executor  # noqa: E402


@pytest.fixture
def session_factory():
    return make_session_factory()


@pytest.fixture(autouse=True)
def no_registry_scan():
    """Never import gi in these tests; record scan calls instead."""
    with patch.object(gst_plugins, "_scan_registry", return_value=True) as scan:
        yield scan


@pytest.fixture
def capture_root(tmp_path):
    """A tmp-dir default capture root so the run's makedirs/log capture
    never touch the real /aws_dda tree."""
    root = os.path.join(str(tmp_path), "captures")
    with patch.object(pipeline_executor, "_WORKFLOW_CAPTURE_ROOT", root):
        yield root
