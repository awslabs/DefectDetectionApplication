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
