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
"""Pytest configuration for the workflow engine test suite."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "edge_device: integration test that needs the real device container "
        "(GStreamer/gi, DDA plugins, embedded Triton); skipped elsewhere "
        "with a clear reason.",
    )


# Hypothesis profile: cap property tests at 25 examples for fast local runs.
# Per-test @settings decorators take precedence; keep them at or below this
# budget. Override with HYPOTHESIS_PROFILE=ci for a larger run.
import os as _os

from hypothesis import settings as _hyp_settings

_hyp_settings.register_profile("engine-fast", max_examples=25)
_hyp_settings.register_profile("ci", max_examples=100)
_hyp_settings.load_profile(_os.environ.get("HYPOTHESIS_PROFILE", "engine-fast"))
