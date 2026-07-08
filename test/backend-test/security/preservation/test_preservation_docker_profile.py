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
"""#4 docker-profile-selection preservation baseline (Req 3.4).

Spec: security-injection-deserialization-fixes — Property 2: Preservation.

The fix (task 6) only adds a documented ``# nosem`` justification to the
``subprocess.run(["bash", "-c", snippet], ...)`` line in
``test/backend-test/host_scripts/test_docker_profile_selection.py``; it makes NO
behavioral change. This baseline records that the existing suite — the
``tegra``/``generic`` decision assertions and the two regression guards — passes
unchanged. Task 13 re-runs it against the fixed tree.

**Validates: Requirements 3.4**

Run:
    python3 -m pytest test/backend-test/security/preservation/test_preservation_docker_profile.py \
        -p no:cacheprovider --noconftest -v
"""
import os
import subprocess
import sys

from _preservation_support import REPO_ROOT

DOCKER_PROFILE_TEST = os.path.join(
    "test", "backend-test", "host_scripts", "test_docker_profile_selection.py"
)


# Validates: Requirements 3.4
def test_docker_profile_selection_suite_passes():
    """The existing docker-profile-selection suite passes unchanged (the real
    decision block plus the L4T / Orin regression guards)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", DOCKER_PROFILE_TEST,
         "-p", "no:cacheprovider", "--noconftest", "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"docker-profile-selection suite must pass unchanged:\n{combined}"
    )
    # 3 decision-contract tests + 3 regression guards = 6 tests.
    assert "6 passed" in combined, f"expected 6 passing tests, got:\n{combined}"


# Validates: Requirements 3.4
def test_docker_profile_decision_contract_direct():
    """Directly reproduce the decision block's tegra/generic contract so the
    baseline is recorded here too (independent of the host_scripts suite)."""
    import re

    script_path = os.path.join(
        REPO_ROOT, "src", "host_scripts", "get_nvidia_libs_versions.sh"
    )
    with open(script_path) as f:
        content = f.read()
    match = re.search(r"(if \[ \$is_gpu -eq 1 \].*?\nfi)", content, re.DOTALL)
    assert match, "Could not find the DOCKER_PROFILE decision block"
    block = match.group(1).replace(">> /tmp/.dda.env", "")

    def decide(is_gpu, arch):
        snippet = f'is_gpu={is_gpu}\narch="{arch}"\n{block}\n'
        res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        m = re.search(r"DOCKER_PROFILE=(\w+)", res.stdout)
        assert m, res.stdout
        return m.group(1)

    # Recorded baseline decisions.
    assert decide(1, "aarch64") == "tegra"
    assert decide(0, "aarch64") == "generic"
    assert decide(1, "x86_64") == "generic"
    assert decide(0, "x86_64") == "generic"
