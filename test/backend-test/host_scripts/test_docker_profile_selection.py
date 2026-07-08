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
"""Regression tests for the Docker profile selection in
src/host_scripts/get_nvidia_libs_versions.sh.

Background: a GPU-capable aarch64 Jetson must run the `tegra` docker-compose
profile, because only that profile mounts the CUDA libraries
(/usr/local/cuda, /usr/lib/aarch64-linux-gnu/tegra). Two separate regressions
forced the `generic` (CPU, no CUDA) profile on GPU-capable devices and broke
model loading with "libcudart.so.* cannot open shared object file":
  1. An L4T-version override forcing generic for L4T ^32.[7-9] / ^3[3-9]
     (this swept up JetPack 4.6 / L4T r32.7.x on Xavier).
  2. A "disable gpu for orin" guard keyed on /sys/devices/soc0/soc_id
     (broke JetPack 5 / Orin).

These tests exercise the real decision block from the script (so the tegra/
generic contract is verified behaviorally) and assert the two override
patterns are not present, so neither can be reintroduced silently.
"""
import os
import re
import subprocess
import unittest

SCRIPT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
        "src", "host_scripts", "get_nvidia_libs_versions.sh",
    )
)


def _read_script():
    with open(SCRIPT_PATH) as f:
        return f.read()


def _run_profile_decision(is_gpu, arch):
    """Run the script's actual DOCKER_PROFILE decision block with controlled
    is_gpu/arch and return the chosen profile ('tegra' or 'generic')."""
    content = _read_script()
    match = re.search(r"(if \[ \$is_gpu -eq 1 \].*?\nfi)", content, re.DOTALL)
    assert match, "Could not find the DOCKER_PROFILE decision block in the script"
    block = match.group(1).replace(">> /tmp/.dda.env", "")  # echo to stdout instead

    snippet = f'is_gpu={is_gpu}\narch="{arch}"\n{block}\n'
    # nosem: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit -- test-only: `snippet` is built from STATIC, in-repo `.sh` content (regex-extracted from the checked-in get_nvidia_libs_versions.sh) plus fixed is_gpu/arch literals; no untrusted/external input reaches this bash -c invocation.
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)  # nosem: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
    assert result.returncode == 0, f"decision block failed: {result.stderr}"

    m = re.search(r"DOCKER_PROFILE=(\w+)", result.stdout)
    assert m, f"No DOCKER_PROFILE in output: {result.stdout!r}"
    return m.group(1)


class TestDockerProfileSelection(unittest.TestCase):
    def test_gpu_aarch64_selects_tegra(self):
        # GPU-capable Jetson (JP4 Xavier or JP5 Orin) must use tegra (CUDA mounts).
        self.assertEqual(_run_profile_decision(1, "aarch64"), "tegra")

    def test_no_gpu_aarch64_selects_generic(self):
        # aarch64 without a usable GPU/CUDA falls back to generic.
        self.assertEqual(_run_profile_decision(0, "aarch64"), "generic")

    def test_x86_64_selects_generic(self):
        # x86_64 has no tegra GPU; always generic, even if is_gpu were set.
        self.assertEqual(_run_profile_decision(1, "x86_64"), "generic")
        self.assertEqual(_run_profile_decision(0, "x86_64"), "generic")


class TestNoProfileRegressions(unittest.TestCase):
    """Guard against the two specific overrides that broke GPU devices."""

    def setUp(self):
        self.content = _read_script()

    def test_no_l4t_version_generic_override(self):
        # The L4T-version override (^32.[7-9] / ^3[3-9]) forced generic on
        # GPU-capable JP4 devices. It must not come back.
        self.assertNotIn("^32\\.[7-9]", self.content,
                         "L4T-version generic-profile override was reintroduced")
        self.assertNotIn("^3[3-9]\\.", self.content,
                         "L4T-version generic-profile override was reintroduced")
        self.assertNotIn("using generic profile for compatibility", self.content,
                         "L4T-version generic-profile override was reintroduced")

    def test_no_orin_gpu_disable(self):
        # The "disable gpu for orin" guard (soc0/soc_id -> is_gpu=0) broke JP5/Orin.
        self.assertNotIn("soc0/soc_id", self.content,
                         "Orin GPU-disable guard was reintroduced")
        self.assertNotIn("Disable gpu for orin", self.content,
                         "Orin GPU-disable guard was reintroduced")

    def test_decision_block_uses_tegra_for_gpu_aarch64(self):
        # Positive guard: the final decision still maps gpu+aarch64 -> tegra.
        self.assertRegex(
            self.content,
            r"if \[ \$is_gpu -eq 1 \] && \[ \$arch = \"aarch64\" \]; then\s*\n\s*echo DOCKER_PROFILE='tegra'",
            "Final DOCKER_PROFILE decision no longer selects tegra for gpu+aarch64",
        )
