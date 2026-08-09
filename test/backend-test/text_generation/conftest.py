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
"""Pytest configuration for the Text_Generation_API endpoint test suite.

Hypothesis profile: cap property tests at 25 examples for fast local runs.
Per-test @settings decorators take precedence; keep them at or below this
budget. Override with HYPOTHESIS_PROFILE=ci for a larger run. Mirrors the
setup in workflow_engine/conftest.py ("engine-fast"/"ci").
"""
import os as _os

from hypothesis import settings as _hyp_settings

_hyp_settings.register_profile("textgen-fast", max_examples=25, deadline=None)
_hyp_settings.register_profile("ci", max_examples=100)
_hyp_settings.load_profile(_os.environ.get("HYPOTHESIS_PROFILE", "textgen-fast"))

import asyncio as _asyncio

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _restore_default_event_loop():
    """Keep the main thread's default asyncio loop usable across the suite.

    Tests here drive async endpoint/runtime code with ``asyncio.run()``,
    which on Python 3.9 closes its loop and unsets the thread's default
    loop on exit. Later suites in the same pytest process import modules
    (e.g. ``utils/server_setup.py``) that call ``asyncio.get_event_loop()``
    at import time and would raise ``RuntimeError: There is no current
    event loop``. Restore a fresh default loop whenever a test left the
    default loop unset or closed (edge-vlm-image-inference final
    checkpoint: baseline isolation)."""
    yield
    policy = _asyncio.get_event_loop_policy()
    try:
        loop = policy.get_event_loop()
        broken = loop.is_closed()
    except RuntimeError:
        broken = True
    if broken:
        policy.set_event_loop(policy.new_event_loop())
