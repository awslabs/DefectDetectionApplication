# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Layout_Check feature-floor shape guard: car-assembly-check task 12.1
(Requirement 9.3, design §7, DD9).

The Layout_Check floor is per-deployment configuration: compute-stack.ts
holds no literal for ``WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS`` (it
serialises the parsed ``layoutCheckMinLocalServerVersions`` context, ``{}``
when absent — asserted by infrastructure/test/layout-check-infra.test.ts), so
this suite pins the SHAPE, not a key set, and does not reuse
``extract_env_json_map``:

1. any map set through the env var parses, in the packager and in the
   deployment gate alike, to keys that are packager arch ids and values that
   are ``N.N.N``, and gates exactly the archs it lacks;
2. the empty map (and absent or malformed configuration) fails closed for
   every architecture, in both Lambdas;
3. the CDK parser accepts exactly the packager's arch vocabulary, and
   compute-stack.ts carries no literal for this variable.

Runs from edge-cv-portal/backend WITH conftest:

    python3 -m pytest tests/test_layout_check_feature_floor_coverage.py \
        -q -p no:cacheprovider
"""
import json
import re
import sys
from pathlib import Path

import pytest

from test_jp7_localserver_floor_exploration import read_compute_stack_source

FEATURE_FLOOR_ENV = 'WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS'
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
CONTEXT_HELPERS_TS = (Path(__file__).resolve().parents[2]
                      / 'infrastructure' / 'lib' / 'context-helpers.ts')
LAYOUT_DEFINITION = {"nodes": [{"id": "layout", "type": "layout_check"}]}

CONFIGURED_MAPS = [
    {"arm64_jp5": "1.0.4"},
    {"arm64_jp5": "1.0.4", "arm64_jp6": "1.0.90", "x86_64": "1.0.60",
     "x86_64_nvidia": "1.0.60"},
]


@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging
    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments
    return deployments


class TestConfiguredFloorShape:
    @pytest.mark.parametrize("floor_map", CONFIGURED_MAPS)
    def test_configured_map_has_the_packager_shape(
            self, packaging, deployments, monkeypatch, floor_map):
        # Validates: Requirements 9.3
        monkeypatch.setenv(FEATURE_FLOOR_ENV, json.dumps(floor_map))
        parsed = packaging._parse_min_versions_map(FEATURE_FLOOR_ENV)
        assert parsed == floor_map
        assert deployments._parse_min_versions_map(FEATURE_FLOOR_ENV) == floor_map
        assert set(parsed) <= set(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        assert all(_SEMVER.fullmatch(version) for version in parsed.values())

    @pytest.mark.parametrize("floor_map", CONFIGURED_MAPS)
    def test_configured_map_gates_exactly_the_archs_it_lacks(
            self, packaging, monkeypatch, floor_map):
        # Validates: Requirements 9.3
        monkeypatch.setattr(packaging, "LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS",
                            dict(floor_map))
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        findings = packaging.layout_check_arch_gate_findings(LAYOUT_DEFINITION, archs)
        assert sorted(f['arch'] for f in findings) == sorted(set(archs) - set(floor_map))


class TestEmptyMapFailsClosed:
    @pytest.mark.parametrize("raw", [json.dumps({}), "not-json{", "[]", None])
    def test_empty_absent_or_malformed_configuration_is_the_empty_map(
            self, packaging, deployments, monkeypatch, raw):
        # Validates: Requirements 9.3
        if raw is None:
            monkeypatch.delenv(FEATURE_FLOOR_ENV, raising=False)
        else:
            monkeypatch.setenv(FEATURE_FLOOR_ENV, raw)
        assert packaging._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}
        assert deployments._parse_min_versions_map(FEATURE_FLOOR_ENV) == {}

    def test_empty_map_rejects_every_architecture_in_both_lambdas(
            self, packaging, deployments, monkeypatch):
        # Validates: Requirements 9.3
        monkeypatch.setattr(packaging, "LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS", {})
        monkeypatch.setattr(
            deployments, "WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS", {})
        archs = sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)
        findings = packaging.layout_check_arch_gate_findings(LAYOUT_DEFINITION, archs)
        assert sorted(f['arch'] for f in findings) == archs
        for arch in archs:
            with pytest.raises(packaging.PackagingError):
                packaging.min_local_server_version_for(arch, layout_check=True)
        for arch in deployments.LOCAL_SERVER_ARCH_IDS:
            assert deployments.layout_check_feature_floor_for(arch) is None


class TestCdkVocabulary:
    def test_cdk_parser_accepts_exactly_the_packager_archs(self, packaging):
        # Validates: Requirements 9.3
        # parseMinVersionsFloor (context-helpers.ts) refuses any other key, so
        # the two vocabularies must move together.
        source = CONTEXT_HELPERS_TS.read_text(encoding="utf-8")
        match = re.search(
            r"export const MIN_VERSIONS_FLOOR_ARCHES = \[([^\]]*)\]", source)
        assert match, f"MIN_VERSIONS_FLOOR_ARCHES not found in {CONTEXT_HELPERS_TS}"
        cdk_archs = re.findall(r"'([^']+)'", match.group(1))
        assert sorted(cdk_archs) == sorted(packaging.ARCH_TO_LOCAL_SERVER_COMPONENT)

    def test_compute_stack_holds_no_literal_for_this_floor(self):
        # Validates: Requirements 9.3
        source = read_compute_stack_source()
        assert FEATURE_FLOOR_ENV in source
        assert not re.search(
            re.escape(FEATURE_FLOOR_ENV) + r"\s*:\s*JSON\.stringify\s*\(\s*\{", source)
        assert re.search(
            re.escape(FEATURE_FLOOR_ENV)
            + r"\s*:\s*JSON\.stringify\s*\(\s*parseLayoutCheckFloor\s*\(", source)
