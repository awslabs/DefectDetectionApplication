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
Property-based tests for the ``build_fleet.resolve_ubuntu_ami`` Ubuntu
Pro branch (ubuntu-pro-build-servers tasks 2.4 and 2.5).

* Property 4 — a successful Pro SSM read short-circuits: for any
  supported release/architecture and any non-empty AMI id from the Pro
  SSM read, the resolver resolves to exactly that id with ZERO
  DescribeImages calls.
  **Validates: Requirements 2.6**
* Property 5 — the Pro fallback selects the newest available image: for
  any supported combination where the Pro SSM read fails or returns an
  empty value, and any non-empty candidate set with distinct creation
  timestamps, the fallback queries owner 099720109477 with the Pro name
  pattern and ``state=available`` and resolves the most recently created
  image.
  **Validates: Requirements 2.2**

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test calls real AWS: ``build_fleet.ssm`` and ``build_fleet.ec2`` are
replaced by in-process recording stubs before every resolution call, and
the EC2 stub's guard rail trips on any operation other than
DescribeImages (RunInstances in particular).

Run ONLY this file, from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_ubuntu_pro_resolver_properties.py \\
        --noconftest -q
"""
import datetime
import os
import sys
import types

from botocore.exceptions import ClientError
from hypothesis import given, settings, strategies as st

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

_SUFFIX = "ubuntu-pro-resolver-props"
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
        "user_id": "property-user", "role": "PortalAdmin"}
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

PRO = build_domain.UBUNTU_FLAVOR_PRO

#: Every (release, arch) combination the Fleet_Manager supports —
#: derived from the Pro dispatch table itself, whose parity with the
#: standard table is pinned by test_ubuntu_pro_ami_tables_unit.py.
SUPPORTED_PRO_COMBINATIONS = sorted(
    (release, arch)
    for release, by_arch in
    build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[PRO].items()
    for arch in by_arch)


# ---------------------------------------------------------------------------
# Recording stubs
# ---------------------------------------------------------------------------

class RecordingSsm:
    """``build_fleet.ssm`` stand-in: records every GetParameter name and
    serves a scripted value (or raises a scripted ClientError)."""

    def __init__(self, value=None, error=False):
        self.get_parameter_names = []
        self._value = value
        self._error = error

    def get_parameter(self, **kwargs):
        self.get_parameter_names.append(kwargs.get("Name", ""))
        if self._error:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound",
                           "Message": "no such parameter"}},
                "GetParameter")
        return {"Parameter": {"Value": self._value}}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected SSM operation '{item}' during AMI resolution")


class RecordingEc2:
    """``build_fleet.ec2`` stand-in: records DescribeImages calls and
    serves a configurable image list. Any other operation (RunInstances
    in particular) trips the guard rail."""

    def __init__(self, images=None):
        self.describe_images_calls = []
        self._images = images or []

    def describe_images(self, **kwargs):
        self.describe_images_calls.append(dict(kwargs))
        return {"Images": [dict(image) for image in self._images]}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(
            f"unexpected EC2 operation '{item}' — no instance may be "
            f"launched by these tests")


def _install(ssm_stub, ec2_stub):
    build_fleet.ssm = ssm_stub
    build_fleet.ec2 = ec2_stub


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: A supported (release, arch) Pro combination.
combinations = st.sampled_from(SUPPORTED_PRO_COMBINATIONS)

#: A non-empty AMI id as the SSM parameter would publish it.
ami_ids = st.from_regex(r"ami-0[0-9a-f]{16}", fullmatch=True)

#: ISO-8601 creation timestamps in the form Canonical publishes
#: (distinct-ness is enforced where the property requires it).
creation_dates = st.datetimes(
    min_value=datetime.datetime(2020, 1, 1),
    max_value=datetime.datetime(2035, 12, 31),
).map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z"))


@st.composite
def fallback_candidate_sets(draw):
    """A non-empty candidate image list with DISTINCT creation
    timestamps, unsorted, paired with the expected newest image id."""
    n = draw(st.integers(min_value=1, max_value=8))
    dates = draw(st.lists(creation_dates, min_size=n, max_size=n,
                          unique=True))
    ids = draw(st.lists(ami_ids, min_size=n, max_size=n, unique=True))
    images = [{"ImageId": image_id, "CreationDate": date}
              for image_id, date in zip(ids, dates)]
    newest = max(images, key=lambda i: i["CreationDate"])["ImageId"]
    images = draw(st.permutations(images))
    return list(images), newest


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 4: A successful Pro SSM
# read short-circuits
# ===========================================================================

class TestProperty4ProSsmShortCircuit:

    # Feature: ubuntu-pro-build-servers, Property 4: A successful Pro
    # SSM read short-circuits
    # **Validates: Requirements 2.6**
    @settings(max_examples=100, deadline=None)
    @given(combination=combinations, ami_id=ami_ids)
    def test_non_empty_pro_ssm_value_resolves_with_zero_describe_images(
            self, combination, ami_id):
        release, arch = combination
        ssm_stub = RecordingSsm(value=ami_id)
        ec2_stub = RecordingEc2()
        _install(ssm_stub, ec2_stub)

        resolved = build_fleet.resolve_ubuntu_ami(arch, release, PRO)

        # Exactly the SSM-published id, from exactly one read of the Pro
        # parameter for this combination.
        assert resolved == ami_id
        assert ssm_stub.get_parameter_names == [
            build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[PRO][release][arch]]
        # Zero DescribeImages calls (Req 2.6).
        assert ec2_stub.describe_images_calls == []


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 5: The Pro fallback
# selects the newest available image
# ===========================================================================

class TestProperty5ProFallbackNewestAvailable:

    def _assert_fallback(self, release, arch, ssm_stub, ec2_stub, newest):
        resolved = build_fleet.resolve_ubuntu_ami(arch, release, PRO)

        # The Pro SSM parameter was attempted first.
        assert ssm_stub.get_parameter_names == [
            build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[PRO][release][arch]]

        # Exactly one DescribeImages fallback query: Canonical owner
        # 099720109477, the Pro name pattern for this combination, and
        # state=available (Req 2.2).
        assert len(ec2_stub.describe_images_calls) == 1
        call = ec2_stub.describe_images_calls[0]
        assert call["Owners"] == ["099720109477"]
        filters = {f["Name"]: f["Values"] for f in call["Filters"]}
        assert filters["name"] == [
            build_fleet.UBUNTU_NAME_FILTER_BY_FLAVOR[PRO][release][arch]]
        assert filters["state"] == ["available"]

        # The most recently created candidate wins.
        assert resolved == newest

    # Feature: ubuntu-pro-build-servers, Property 5: The Pro fallback
    # selects the newest available image
    # **Validates: Requirements 2.2**
    @settings(max_examples=100, deadline=None)
    @given(combination=combinations, candidates=fallback_candidate_sets())
    def test_ssm_client_error_falls_back_to_newest_available(
            self, combination, candidates):
        release, arch = combination
        images, newest = candidates
        ssm_stub = RecordingSsm(error=True)
        ec2_stub = RecordingEc2(images=images)
        _install(ssm_stub, ec2_stub)
        self._assert_fallback(release, arch, ssm_stub, ec2_stub, newest)

    # Feature: ubuntu-pro-build-servers, Property 5: The Pro fallback
    # selects the newest available image
    # **Validates: Requirements 2.2**
    @settings(max_examples=100, deadline=None)
    @given(combination=combinations, candidates=fallback_candidate_sets(),
           empty_value=st.sampled_from(["", None]))
    def test_empty_ssm_value_falls_back_to_newest_available(
            self, combination, candidates, empty_value):
        release, arch = combination
        images, newest = candidates
        ssm_stub = RecordingSsm(value=empty_value)
        ec2_stub = RecordingEc2(images=images)
        _install(ssm_stub, ec2_stub)
        self._assert_fallback(release, arch, ssm_stub, ec2_stub, newest)
