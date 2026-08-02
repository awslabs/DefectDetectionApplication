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
"""Packaged runtime dependency checks for src/backend/requirements.txt.

Bug condition exploration test for the opcua-output-node-bugfix spec
(Part 1). The ``opcua_write`` output binding's ``_default_opcua_writer``
does ``from opcua import Client``; the ``opcua`` (python-opcua) package
must therefore be present in the packaged runtime dependency list so it
is installed on the JP5 edge device.

On the UNFIXED tree this test FAILS -- ``opcua`` is absent from
``requirements.txt`` -- which is the counterexample that confirms Part 1
of the bug (Bug Analysis 1.1, 1.2). After the fix adds a pinned ``opcua``
entry it encodes the expected behaviour and PASSES (Expected Behaviour
2.1).

**Validates: Requirements 1.1, 1.2, 2.1**
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
BACKEND_REQUIREMENTS = os.path.join(
    REPO_ROOT, "src", "backend", "requirements.txt"
)

# Distribution name at the start of a requirement line, e.g. "opcua" from
# "opcua==0.98.13" or "scikit-learn>=1.1.3,<1.2".
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _parse_requirement_names(text):
    """Return the lower-cased distribution names listed in a requirements
    file, ignoring blank lines and ``#`` comments."""
    names = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _NAME_RE.match(line)
        if match:
            names.append(match.group(1).lower())
    return names


def test_requirements_file_exists():
    assert os.path.isfile(BACKEND_REQUIREMENTS), (
        "expected src/backend/requirements.txt at {0}".format(
            BACKEND_REQUIREMENTS)
    )


def test_opcua_is_a_packaged_runtime_dependency():
    """``opcua`` (python-opcua) MUST be listed in the packaged runtime
    dependency list so ``from opcua import Client`` succeeds on device.

    UNFIXED: FAILS -- ``opcua`` is absent (the counterexample that proves
    the binding cannot import its client on JP5).
    """
    with open(BACKEND_REQUIREMENTS) as f:
        names = _parse_requirement_names(f.read())

    assert "opcua" in names, (
        "'opcua' (python-opcua) is not listed in src/backend/requirements.txt; "
        "the opcua_write binding's `from opcua import Client` will raise "
        "ModuleNotFoundError on the edge device. Listed packages: {0}".format(
            sorted(names))
    )
