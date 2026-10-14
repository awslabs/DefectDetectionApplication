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
Unit tests for the ``build_fleet`` flavor-keyed AMI lookup tables
(ubuntu-pro-build-servers task 2.3).

Asserted here:

* **Standard-table preservation (Req 2.4)**: the standard SSM parameter
  and DescribeImages name-filter constants equal FROZEN pre-feature
  literals, byte-for-byte — the Pro tables are strictly additive and the
  flavor dispatch's standard branch references the existing constants
  unchanged (same object identity).
* **Table parity (Req 2.5)**: the Pro tables' (release, arch) key sets
  equal the standard tables' — every release/architecture combination
  the Fleet_Manager supports for standard launches (22.04 on arm64 and
  x86_64, 24.04 on arm64) has a Pro mapping, and no extra combination
  exists on either side.
* **IAM prefix coverage (Req 7.1)**: every SSM path in BOTH flavor
  tables starts with ``/aws/service/canonical/`` — the prefix of the
  ``grantAmiParameterRead`` ARN in build-fleet-stack.ts — so a future
  narrowing of the grant fails a test rather than production resolution.

_Requirements: 2.4, 2.5, 7.1_

Run ONLY this file, from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_ubuntu_pro_ami_tables_unit.py \\
        --noconftest -q
"""
import os
import sys
import types

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind boto3 clients and
# env-derived settings at import time. No AWS call is ever made; the
# dummy credentials only satisfy client construction.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "ubuntu-pro-ami-tables-unit"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")
    module.log_audit_event = lambda **kwargs: None
    module.create_response = lambda status_code, body: {
        "statusCode": status_code, "body": body}
    module.get_user_from_event = lambda event: {
        "user_id": "unit-user", "role": "PortalAdmin"}
    return module


def _fake_rbac_middleware():
    module = types.ModuleType("rbac_middleware")

    def _identity_decorator_factory(*d_args, **d_kwargs):
        def decorator(func):
            return func
        return decorator

    module.require_builds_read = _identity_decorator_factory
    module.super_user_only = lambda func: func
    return module


for _module in ("build_domain", "build_planner", "build_fleet",
                "build_source", "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

import build_domain  # noqa: E402
import build_fleet  # noqa: E402


# ===========================================================================
# Frozen pre-feature literals (captured from build_fleet.py as it stood
# before the ubuntu-pro-build-servers feature). If any of these tests
# fail, the standard lookup constants were edited — which Requirement
# 2.4 forbids (the Pro tables must be strictly additive).
# ===========================================================================

FROZEN_UBUNTU_2204_SSM_PARAMETER = {
    'arm64':
        '/aws/service/canonical/ubuntu/server/22.04/stable/current/'
        'arm64/hvm/ebs-gp2/ami-id',
    'x86_64':
        '/aws/service/canonical/ubuntu/server/22.04/stable/current/'
        'amd64/hvm/ebs-gp2/ami-id',
}
FROZEN_UBUNTU_2404_SSM_PARAMETER = {
    'arm64':
        '/aws/service/canonical/ubuntu/server/24.04/stable/current/'
        'arm64/hvm/ebs-gp3/ami-id',
}
FROZEN_UBUNTU_2204_NAME_FILTER = {
    'arm64': 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*',
    'x86_64': 'ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*',
}
FROZEN_UBUNTU_2404_NAME_FILTER = {
    'arm64': 'ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*',
}
FROZEN_CANONICAL_OWNER_ID = '099720109477'
FROZEN_DEFAULT_UBUNTU_VERSION = '22.04'

#: The grantAmiParameterRead ARN in build-fleet-stack.ts is
#: ``arn:aws:ssm:{region}::parameter/aws/service/canonical/*``: every
#: parameter the resolver reads must live under this prefix (Req 7.1).
GRANT_ARN_PATH_PREFIX = '/aws/service/canonical/'


# ===========================================================================
# Standard-table preservation (Req 2.4)
# ===========================================================================

class TestStandardTablePreservation:

    def test_2204_ssm_parameters_are_the_frozen_pre_feature_literals(self):
        assert build_fleet.UBUNTU_2204_SSM_PARAMETER == \
            FROZEN_UBUNTU_2204_SSM_PARAMETER

    def test_2404_ssm_parameter_is_the_frozen_pre_feature_literal(self):
        assert build_fleet.UBUNTU_2404_SSM_PARAMETER == \
            FROZEN_UBUNTU_2404_SSM_PARAMETER

    def test_2204_name_filters_are_the_frozen_pre_feature_literals(self):
        assert build_fleet.UBUNTU_2204_NAME_FILTER == \
            FROZEN_UBUNTU_2204_NAME_FILTER

    def test_2404_name_filter_is_the_frozen_pre_feature_literal(self):
        assert build_fleet.UBUNTU_2404_NAME_FILTER == \
            FROZEN_UBUNTU_2404_NAME_FILTER

    def test_canonical_owner_and_default_release_unchanged(self):
        assert build_fleet.CANONICAL_OWNER_ID == FROZEN_CANONICAL_OWNER_ID
        assert build_fleet.DEFAULT_UBUNTU_VERSION == \
            FROZEN_DEFAULT_UBUNTU_VERSION

    def test_release_dispatch_tables_reference_the_frozen_tables(self):
        assert build_fleet.UBUNTU_SSM_PARAMETER == {
            '22.04': FROZEN_UBUNTU_2204_SSM_PARAMETER,
            '24.04': FROZEN_UBUNTU_2404_SSM_PARAMETER,
        }
        assert build_fleet.UBUNTU_NAME_FILTER == {
            '22.04': FROZEN_UBUNTU_2204_NAME_FILTER,
            '24.04': FROZEN_UBUNTU_2404_NAME_FILTER,
        }

    def test_flavor_dispatch_standard_branch_is_the_existing_object(self):
        """The standard branch of the flavor dispatch references the
        EXISTING constants unchanged — same object, not a copy (Req 2.4:
        no edit, no drift)."""
        assert build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[
            build_domain.UBUNTU_FLAVOR_STANDARD] \
            is build_fleet.UBUNTU_SSM_PARAMETER
        assert build_fleet.UBUNTU_NAME_FILTER_BY_FLAVOR[
            build_domain.UBUNTU_FLAVOR_STANDARD] \
            is build_fleet.UBUNTU_NAME_FILTER


# ===========================================================================
# Pro/standard table key-set parity (Req 2.5)
# ===========================================================================

def _key_pairs(table_by_release):
    """The (release, arch) key set of a release-keyed table."""
    return {(release, arch)
            for release, by_arch in table_by_release.items()
            for arch in by_arch}


class TestTableParity:

    def test_flavor_dispatch_covers_exactly_both_flavors(self):
        assert set(build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR) == \
            set(build_domain.UBUNTU_FLAVORS)
        assert set(build_fleet.UBUNTU_NAME_FILTER_BY_FLAVOR) == \
            set(build_domain.UBUNTU_FLAVORS)

    def test_pro_ssm_key_set_equals_standard_ssm_key_set(self):
        pro = build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[
            build_domain.UBUNTU_FLAVOR_PRO]
        assert _key_pairs(pro) == _key_pairs(build_fleet.UBUNTU_SSM_PARAMETER)

    def test_pro_name_filter_key_set_equals_standard_key_set(self):
        pro = build_fleet.UBUNTU_NAME_FILTER_BY_FLAVOR[
            build_domain.UBUNTU_FLAVOR_PRO]
        assert _key_pairs(pro) == _key_pairs(build_fleet.UBUNTU_NAME_FILTER)

    def test_supported_combinations_are_the_documented_set(self):
        """Both flavors support exactly 22.04 on arm64 + x86_64 and
        24.04 on arm64 (Req 2.5)."""
        expected = {('22.04', 'arm64'), ('22.04', 'x86_64'),
                    ('24.04', 'arm64')}
        for flavor in build_domain.UBUNTU_FLAVORS:
            assert _key_pairs(
                build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[flavor]) == \
                expected
            assert _key_pairs(
                build_fleet.UBUNTU_NAME_FILTER_BY_FLAVOR[flavor]) == \
                expected


# ===========================================================================
# IAM prefix coverage (Req 7.1)
# ===========================================================================

class TestIamPrefixCoverage:

    def test_every_ssm_path_in_both_flavor_tables_is_grant_covered(self):
        """Every SSM parameter path the resolver can read — standard AND
        pro — starts with /aws/service/canonical/, the path prefix of
        the grantAmiParameterRead ARN. A future narrowing of the grant
        (or a stray parameter outside the canonical tree) fails here
        rather than in production resolution (Req 7.1)."""
        for flavor, by_release in \
                build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR.items():
            for release, by_arch in by_release.items():
                for arch, path in by_arch.items():
                    assert path.startswith(GRANT_ARN_PATH_PREFIX), (
                        f"SSM path for flavor={flavor} release={release} "
                        f"arch={arch} is outside the grantAmiParameterRead "
                        f"prefix {GRANT_ARN_PATH_PREFIX}: {path}")

    def test_pro_paths_use_the_pro_server_subtree(self):
        """Sanity: every Pro path lives under the pro-server subtree
        (the grant comment documents both subtrees are covered)."""
        pro = build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[
            build_domain.UBUNTU_FLAVOR_PRO]
        for by_arch in pro.values():
            for path in by_arch.values():
                assert '/ubuntu/pro-server/' in path
