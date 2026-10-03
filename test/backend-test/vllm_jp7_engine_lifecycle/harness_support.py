# Copyright 2026 Amazon Web Services, Inc.
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
"""Standard-library-only runner for ``wakeup_harness.py`` (spec
vllm-jp7-engine-lifecycle, Defect A), so the signal suites also run in a bare
``python:3.10-slim`` / ``python:3.11-slim`` container with only pytest
installed: ``python -m pytest --noconftest test_vjel_exploration_fork_wakeup.py
test_vjel_preservation_signals.py``."""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: Defect A's subprocess harness.
HARNESS_PATH = HERE / "wakeup_harness.py"
#: ``src/backend`` of this tree.
BACKEND_DIR = HERE.parents[2] / "src" / "backend"


def run_harness(hook, signal_name, child="handler", target="child"):
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)  # the backend-test root conftest sets it
    env["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(  # nosec B603 - fixed argv, test harness
        [sys.executable, str(HARNESS_PATH), hook, signal_name, child, target],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        check=False)
    lines = [line for line in result.stdout.decode().splitlines() if line.startswith("{")]
    assert result.returncode == 0 and lines, (
        "harness failed (exit {}): {}".format(result.returncode,
                                              result.stderr.decode()[-2000:]))
    return json.loads(lines[-1])
