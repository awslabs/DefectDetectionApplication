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
"""
Unit tests for ``build_domain.required_os_release_for_target``
(jp7-ephemeral-runner-provisioning task 5.5).

Asserted here:

* the five supported Build_Targets map to the FROZEN release table —
  ``JP7 -> '24.04'`` (noble) and JP5 / JP6 / AMD64 / AMD64_NVIDIA ->
  ``'22.04'`` (jammy) — deliberately re-spelled in this file rather
  than derived from production constants, so a table regression cannot
  silently rewrite the oracle;
* unsupported target names raise ``ValueError`` (the accessor delegates
  to ``target_definition``, so the unsupported-target contract is
  unchanged), and the diagnostic names the offending target;
* existing ``target_definition`` consumers are unaffected: the named
  keys they read (``component_name``, ``recipe``, ``required_arch``)
  are still present with their pre-fix values, ``required_arch_for_target``
  still answers for every supported target, and mutating a returned
  definition does not corrupt the module table.

**Validates: Requirements 2.1, 2.2**

Safety: ``build_domain`` is a pure module (no AWS clients, no side
effects); these tests make no network calls and start no subprocesses.

Run with ``--noconftest`` like the rest of the ``portal_builds`` suite,
from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_os_release_unit.py \\
        --noconftest -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402


# ---------------------------------------------------------------------------
# Frozen oracles — re-spelled literally, NOT derived from build_domain
# constants, so the test still catches a rewritten production table.
# ---------------------------------------------------------------------------

#: Build_Target -> required build-host Ubuntu release (design Property 1
#: frozen oracle; Req 2.1, 2.2).
FROZEN_OS_RELEASE_TABLE = {
    'JP5': '22.04',
    'JP6': '22.04',
    'JP7': '24.04',
    'AMD64': '22.04',
    'AMD64_NVIDIA': '22.04',
}

#: The named keys existing target_definition consumers read
#: (create_build_jobs, retry_clone, dispatch preflight), with their
#: pre-fix values frozen per target.
FROZEN_DEFINITION_KEYS = {
    'JP5': {
        'component_name': 'aws.edgeml.dda.LocalServer.arm64JP5',
        'recipe': 'recipe-arm64-jp5.yaml',
        'required_arch': 'arm64',
    },
    'JP6': {
        'component_name': 'aws.edgeml.dda.LocalServer.arm64JP6',
        'recipe': 'recipe-arm64-jp6.yaml',
        'required_arch': 'arm64',
    },
    'JP7': {
        'component_name': 'aws.edgeml.dda.LocalServer.arm64JP7',
        'recipe': 'recipe-arm64-jp7.yaml',
        'required_arch': 'arm64',
    },
    'AMD64': {
        'component_name': 'aws.edgeml.dda.LocalServer.amd64',
        'recipe': 'recipe-amd64.yaml',
        'required_arch': 'x86_64',
    },
    'AMD64_NVIDIA': {
        'component_name': 'aws.edgeml.dda.LocalServer.amd64Nvidia',
        'recipe': 'recipe-amd64-nvidia.yaml',
        'required_arch': 'x86_64',
    },
}


# ---------------------------------------------------------------------------
# The frozen release table (Req 2.1, 2.2)
# ---------------------------------------------------------------------------

class TestFrozenReleaseTable:
    """The five supported targets map exactly to the frozen table."""

    @pytest.mark.parametrize(
        "target,expected_release", sorted(FROZEN_OS_RELEASE_TABLE.items()))
    def test_supported_target_release(self, target, expected_release):
        assert build_domain.required_os_release_for_target(target) \
            == expected_release

    def test_exactly_five_supported_targets(self):
        """The supported-target set is exactly the frozen table's keys —
        no target was added or dropped by the fix."""
        assert set(build_domain.SUPPORTED_BUILD_TARGETS) \
            == set(FROZEN_OS_RELEASE_TABLE)

    def test_jp7_is_the_only_noble_target(self):
        noble = [t for t in build_domain.SUPPORTED_BUILD_TARGETS
                 if build_domain.required_os_release_for_target(t)
                 == '24.04']
        assert noble == ['JP7']


# ---------------------------------------------------------------------------
# Unsupported targets keep raising ValueError (Req 2.2 via
# target_definition delegation)
# ---------------------------------------------------------------------------

class TestUnsupportedTargets:

    @pytest.mark.parametrize("bad_target", [
        'JP4',            # plausible-but-unsupported sibling name
        'jp7',            # case-sensitive: lowercase is NOT supported
        'JP7 ',           # trailing whitespace is a different name
        'AMD64_NVIDIA2',
        '',
        'noble',
        '24.04',          # a release string is not a target name
    ])
    def test_unsupported_target_raises_value_error(self, bad_target):
        with pytest.raises(ValueError) as excinfo:
            build_domain.required_os_release_for_target(bad_target)
        assert repr(bad_target)[1:-1] in str(excinfo.value) or \
            f"'{bad_target}'" in str(excinfo.value)

    def test_none_target_raises_value_error(self):
        with pytest.raises(ValueError):
            build_domain.required_os_release_for_target(None)

    def test_diagnostic_names_supported_targets(self):
        """The unsupported-target diagnostic still enumerates the
        supported names (unchanged target_definition contract)."""
        with pytest.raises(ValueError) as excinfo:
            build_domain.required_os_release_for_target('JP4')
        message = str(excinfo.value)
        for target in FROZEN_OS_RELEASE_TABLE:
            assert target in message


# ---------------------------------------------------------------------------
# Existing target_definition consumers unchanged (Req 2.1 preservation)
# ---------------------------------------------------------------------------

class TestTargetDefinitionConsumersUnchanged:
    """The named keys read by create_build_jobs, retry_clone, and the
    dispatch preflight are still present with their pre-fix values."""

    @pytest.mark.parametrize("target", sorted(FROZEN_DEFINITION_KEYS))
    def test_named_keys_still_read(self, target):
        definition = build_domain.target_definition(target)
        for key, expected in FROZEN_DEFINITION_KEYS[target].items():
            assert definition[key] == expected

    @pytest.mark.parametrize("target", sorted(FROZEN_DEFINITION_KEYS))
    def test_required_arch_accessor_unchanged(self, target):
        assert build_domain.required_arch_for_target(target) \
            == FROZEN_DEFINITION_KEYS[target]['required_arch']

    @pytest.mark.parametrize("target", sorted(FROZEN_DEFINITION_KEYS))
    def test_definition_carries_release_alongside_named_keys(self, target):
        """The additive required_os_release key coexists with the named
        keys and agrees with the accessor."""
        definition = build_domain.target_definition(target)
        assert definition['required_os_release'] \
            == FROZEN_OS_RELEASE_TABLE[target]
        assert definition['required_os_release'] \
            == build_domain.required_os_release_for_target(target)

    def test_returned_definition_is_a_copy(self):
        """target_definition returns a copy: consumer-side mutation must
        not corrupt the module table or the release accessor."""
        definition = build_domain.target_definition('JP7')
        definition['required_os_release'] = 'mutated'
        definition['required_arch'] = 'mutated'
        assert build_domain.required_os_release_for_target('JP7') == '24.04'
        assert build_domain.required_arch_for_target('JP7') == 'arm64'
