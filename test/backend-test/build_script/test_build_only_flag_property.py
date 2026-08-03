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
"""Property test for the build-only-flag conflict guard.

Property 1: Conflicting modes always rejected before any action.

For any command-line argument vector (valid or invalid arch/JetPack
arguments, in any order), invoking gdk-component-build-and-publish.sh with
both SKIP_BUILD=1 and SKIP_PUBLISH=1 set exits with a non-zero code, prints
the mutual-exclusion error, and performs no filesystem side effect.

The guard is the first statement after the `set` lines, so it must fire
before argument parsing, the AWS credential pre-flight, and every build or
publish step. Each example runs the real script via subprocess from an empty
temporary working directory: any side effect (recipe.yaml copy,
gdk-config.json write, greengrass-build/.gdk cleanup) would land in that
directory, so asserting the directory stays empty proves no side effects.

**Validates: Requirements 3.1**
"""

import os
import string
import subprocess
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
)
SCRIPT = os.path.join(REPO_ROOT, "gdk-component-build-and-publish.sh")

# Tokens the script's argument parser actually accepts, plus arbitrary junk:
# the guard must fire before parsing, so the property holds for both.
_VALID_TOKENS = st.sampled_from(
    ["x86_64", "amd64", "aarch64", "arm64", "4", "5", "6",
     "jp4", "jp5", "jp6", "JP4", "JP5", "JP6", "--jp4", "--jp5", "--jp6"]
)
_JUNK_TOKENS = st.text(
    alphabet=string.ascii_letters + string.digits + "-_./",
    min_size=1,
    max_size=12,
)
_ARG_VECTORS = st.lists(st.one_of(_VALID_TOKENS, _JUNK_TOKENS), max_size=6)


@settings(max_examples=25, deadline=None)
@given(args=_ARG_VECTORS)
def test_conflicting_modes_always_rejected_before_any_action(args):
    """SKIP_BUILD=1 + SKIP_PUBLISH=1 -> non-zero exit, error message, no side effects."""
    env = dict(os.environ, SKIP_BUILD="1", SKIP_PUBLISH="1")
    with tempfile.TemporaryDirectory() as workdir:
        result = subprocess.run(
            [SCRIPT] + args,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        leftovers = os.listdir(workdir)

    assert result.returncode != 0, (
        f"expected non-zero exit for args {args!r}, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "SKIP_BUILD=1 and SKIP_PUBLISH=1 are mutually exclusive" in result.stdout, (
        f"mutual-exclusion message missing for args {args!r}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert leftovers == [], (
        f"script performed filesystem side effects for args {args!r}: {leftovers}"
    )
