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
Property-based tests for the ``build_fleet.launch_build_server`` Ubuntu
flavor wiring (ubuntu-pro-build-servers tasks 4.3, 4.4, 4.5, 4.6, 4.9).

* Property 1 — flavor selects the matching AMI lookup.
  **Validates: Requirements 1.1, 1.2, 2.1**
* Property 2 — flavorless launches preserve pre-feature standard
  resolution (byte-identical SSM paths and name filters).
  **Validates: Requirements 1.3, 2.4, 6.4**
* Property 3 — invalid launch requests are rejected with no side
  effects: 400, zero EC2 API calls, zero BuildServers writes.
  **Validates: Requirements 1.4, 1.5, 1.6**
* Property 6 — an unresolvable Pro AMI fails the launch closed: an
  error identifying flavor/release/arch, an Audit_Log failure entry,
  and zero RunInstances.
  **Validates: Requirements 2.3**
* Property 9 — audit entries carry the flavor faithfully: the effective
  flavor after determination, the raw submitted value on
  pre-determination rejection.
  **Validates: Requirements 3.4, 3.5**

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test calls real AWS. DynamoDB is moto-mocked (module scope, started
before ``build_fleet`` is imported so its module-level boto3 handles
bind to the mock); ``build_fleet.ssm`` and ``build_fleet.ec2`` are
replaced by in-process recording stubs before every launch, with guard
rails that trip on any unscripted operation (RunInstances in
particular).

Run ONLY this file, from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_ubuntu_flavor_launch_properties.py \\
        --noconftest -q
"""
import json
import os
import sys
import types

from botocore.exceptions import ClientError
from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: build_fleet binds boto3 resources and
# env-derived table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "ubuntu-flavor-launch-props"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

# Import boto3 from the test environment BEFORE the Lambda layer joins
# sys.path (its vendored urllib3 targets the Lambda runtime).
import boto3  # noqa: E402

from moto import mock_aws  # noqa: E402

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
        "user_id": "property-admin", "role": "PortalAdmin"}
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

# Module-scope moto: active for every import below and the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name, _key in ((_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_SERVERS = _DDB.Table(_SERVERS_TABLE)
_SETTINGS = _DDB.Table(_SETTINGS_TABLE)

import build_domain  # noqa: E402
import build_fleet  # noqa: E402

PRO = build_domain.UBUNTU_FLAVOR_PRO
STANDARD = build_domain.UBUNTU_FLAVOR_STANDARD

#: Every (release, arch) combination the Fleet_Manager supports (the
#: standard dispatch table is the pre-feature source of truth; the Pro
#: table's key parity is pinned by test_ubuntu_pro_ami_tables_unit.py).
SUPPORTED_COMBINATIONS = sorted(
    (release, arch)
    for release, by_arch in build_fleet.UBUNTU_SSM_PARAMETER.items()
    for arch in by_arch)

#: FROZEN pre-feature standard SSM parameter paths and DescribeImages
#: name filters (independent oracles, NOT read from build_fleet): a
#: flavorless launch with no configured default must resolve through
#: these byte-identical values (Req 1.3, 2.4).
PRE_FEATURE_SSM_PATH = {
    ("22.04", build_domain.ARCH_ARM64):
        "/aws/service/canonical/ubuntu/server/22.04/stable/current/"
        "arm64/hvm/ebs-gp2/ami-id",
    ("22.04", build_domain.ARCH_X86_64):
        "/aws/service/canonical/ubuntu/server/22.04/stable/current/"
        "amd64/hvm/ebs-gp2/ami-id",
    ("24.04", build_domain.ARCH_ARM64):
        "/aws/service/canonical/ubuntu/server/24.04/stable/current/"
        "arm64/hvm/ebs-gp3/ami-id",
}
PRE_FEATURE_NAME_FILTER = {
    ("22.04", build_domain.ARCH_ARM64):
        "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*",
    ("22.04", build_domain.ARCH_X86_64):
        "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*",
    ("24.04", build_domain.ARCH_ARM64):
        "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*",
}


# ---------------------------------------------------------------------------
# Recording stubs and helpers
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
            f"unexpected SSM operation '{item}' during a launch")


class RecordingEc2:
    """``build_fleet.ec2`` stand-in: records DescribeImages and (when
    allowed) RunInstances calls. Any other operation — and RunInstances
    when not allowed — trips the guard rail."""

    def __init__(self, images=None, describe_error=False,
                 allow_run_instances=False):
        self.describe_images_calls = []
        self.run_instances_calls = []
        self._images = images or []
        self._describe_error = describe_error
        self._allow_run = allow_run_instances

    def describe_images(self, **kwargs):
        self.describe_images_calls.append(dict(kwargs))
        if self._describe_error:
            raise ClientError(
                {"Error": {"Code": "UnauthorizedOperation",
                           "Message": "scripted DescribeImages failure"}},
                "DescribeImages")
        return {"Images": [dict(image) for image in self._images]}

    def run_instances(self, **kwargs):
        if not self._allow_run:
            raise AssertionError(
                "unexpected RunInstances — this launch must fail closed")
        self.run_instances_calls.append(dict(kwargs))
        count = len(self.run_instances_calls)
        return {"Instances": [{"InstanceId": f"i-{count:017x}"}]}

    def __getattr__(self, item):  # pragma: no cover - guard rail
        raise AssertionError(f"unexpected EC2 operation '{item}'")


class AuditRecorder:
    """``build_fleet.log_audit_event`` stand-in capturing every entry."""

    def __init__(self):
        self.entries = []

    def __call__(self, **kwargs):
        self.entries.append(kwargs)

    def only(self, result):
        """The single audit entry, asserted to carry ``result``."""
        assert len(self.entries) == 1, self.entries
        entry = self.entries[0]
        assert entry["action"] == "fleet_server_launch"
        assert entry["result"] == result
        return entry


def _install(ssm_stub, ec2_stub):
    build_fleet.ssm = ssm_stub
    build_fleet.ec2 = ec2_stub
    audit = AuditRecorder()
    build_fleet.log_audit_event = audit
    return audit


def _reset_state():
    """Empty the BuildServers table and remove any stored Build_Config."""
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    _SETTINGS.delete_item(
        Key={"setting_key": build_fleet.BUILD_CONFIG_SETTING_KEY})


def _store_config(config):
    _SETTINGS.put_item(Item={
        "setting_key": build_fleet.BUILD_CONFIG_SETTING_KEY,
        "value": config,
    })


def _launch(body):
    event = {"resource": "/build-servers", "httpMethod": "POST",
             "body": json.dumps(body)}
    return build_fleet.launch_build_server(event, None)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

combinations = st.sampled_from(SUPPORTED_COMBINATIONS)
flavors = st.sampled_from([PRO, STANDARD])
ami_ids = st.from_regex(r"ami-0[0-9a-f]{16}", fullmatch=True)
server_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1,
    max_size=24).filter(lambda s: s.strip())

#: ubuntu_flavor values that are NOT exactly 'pro' or 'standard':
#: empty, differently cased, whitespace-padded, arbitrary text, and
#: non-string JSON values (Req 1.4).
invalid_flavors = st.one_of(
    st.sampled_from(["", "Pro", "PRO", "Standard", "STANDARD",
                     " pro", "pro ", "ubuntu-pro", "PRO ", "pRo"]),
    st.text(min_size=1, max_size=20).filter(
        lambda s: s not in (PRO, STANDARD)),
    st.integers(),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.lists(st.text(max_size=5), max_size=3),
    st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=2),
)

#: (release, arch) pairings outside the supported set (Req 1.5).
unsupported_combinations = st.sampled_from([
    ("24.04", build_domain.ARCH_X86_64),
    ("18.04", build_domain.ARCH_ARM64),
    ("20.04", build_domain.ARCH_X86_64),
    ("25.04", build_domain.ARCH_ARM64),
])


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 1: Flavor selects the
# matching AMI lookup
# ===========================================================================

class TestProperty1FlavorSelectsMatchingLookup:

    # Feature: ubuntu-pro-build-servers, Property 1: Flavor selects the
    # matching AMI lookup
    # **Validates: Requirements 1.1, 1.2, 2.1**
    @settings(max_examples=100, deadline=None)
    @given(flavor=flavors, combination=combinations, ami_id=ami_ids,
           name=server_names)
    def test_launch_reads_the_requested_flavors_ssm_path_and_uses_its_ami(
            self, flavor, combination, ami_id, name):
        release, arch = combination
        _reset_state()
        ssm_stub = RecordingSsm(value=ami_id)
        ec2_stub = RecordingEc2(allow_run_instances=True)
        audit = _install(ssm_stub, ec2_stub)

        response = _launch({"name": name, "architecture": arch,
                            "ubuntu_version": release,
                            "ubuntu_flavor": flavor})

        assert response["statusCode"] == 201, response["body"]
        # Exactly one SSM read, of exactly the requested flavor's
        # parameter path for this release/arch (Req 1.1, 1.2, 2.1).
        assert ssm_stub.get_parameter_names == [
            build_fleet.UBUNTU_SSM_PARAMETER_BY_FLAVOR[flavor][release][arch]]
        # The launched instance uses the AMI id that lookup resolved.
        assert len(ec2_stub.run_instances_calls) == 1
        assert ec2_stub.run_instances_calls[0]["ImageId"] == ami_id
        # No fallback was needed for the non-empty SSM value.
        assert ec2_stub.describe_images_calls == []
        audit.only("success")


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 2: Flavorless launches
# preserve pre-feature standard resolution
# ===========================================================================

class TestProperty2FlavorlessPreFeaturePreservation:

    # Feature: ubuntu-pro-build-servers, Property 2: Flavorless launches
    # preserve pre-feature standard resolution
    # **Validates: Requirements 1.3, 2.4, 6.4**
    @settings(max_examples=100, deadline=None)
    @given(combination=combinations, ami_id=ami_ids, name=server_names,
           ssm_fails=st.booleans(),
           config_stored_without_flavor=st.booleans())
    def test_flavorless_launch_resolves_byte_identical_standard_paths(
            self, combination, ami_id, name, ssm_fails,
            config_stored_without_flavor):
        release, arch = combination
        _reset_state()
        if config_stored_without_flavor:
            # A stored Build_Config that simply carries no ubuntu_flavor
            # is indistinguishable from no stored config (Req 6.4).
            _store_config({"volume_size_gb": 200})

        images = [{"ImageId": ami_id,
                   "CreationDate": "2025-01-01T00:00:00.000Z"}]
        ssm_stub = RecordingSsm(value=ami_id, error=ssm_fails)
        ec2_stub = RecordingEc2(images=images, allow_run_instances=True)
        audit = _install(ssm_stub, ec2_stub)

        response = _launch({"name": name, "architecture": arch,
                            "ubuntu_version": release})

        assert response["statusCode"] == 201, response["body"]
        # The SSM read is the FROZEN pre-feature standard path,
        # byte-identical (Req 1.3, 2.4).
        assert ssm_stub.get_parameter_names == [
            PRE_FEATURE_SSM_PATH[(release, arch)]]
        if ssm_fails:
            # The fallback, when reached, uses the byte-identical
            # pre-feature name filter (Req 2.4).
            assert len(ec2_stub.describe_images_calls) == 1
            filters = {f["Name"]: f["Values"]
                       for f in ec2_stub.describe_images_calls[0]["Filters"]}
            assert filters["name"] == [
                PRE_FEATURE_NAME_FILTER[(release, arch)]]
        else:
            assert ec2_stub.describe_images_calls == []
        assert len(ec2_stub.run_instances_calls) == 1
        assert ec2_stub.run_instances_calls[0]["ImageId"] == ami_id
        # The effective flavor is standard (Req 6.4).
        entry = audit.only("success")
        assert entry["details"]["ubuntu_flavor"] == STANDARD


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 3: Invalid launch requests
# are rejected with no side effects
# ===========================================================================

class TestProperty3RejectionWithoutSideEffects:

    def _assert_rejected_without_side_effects(self, response, ssm_stub,
                                              ec2_stub):
        assert response["statusCode"] == 400, response["body"]
        error = response["body"]["error"]
        assert error["code"] == "LAUNCH_REQUEST_INVALID"
        # Zero EC2 API calls (guard-railed stubs would also have
        # tripped) and zero SSM reads (Req 1.6).
        assert ec2_stub.describe_images_calls == []
        assert ec2_stub.run_instances_calls == []
        assert ssm_stub.get_parameter_names == []
        # Zero BuildServers writes (Req 1.6).
        assert _SERVERS.scan().get("Items", []) == []
        return error

    # Feature: ubuntu-pro-build-servers, Property 3: Invalid launch
    # requests are rejected with no side effects
    # **Validates: Requirements 1.4, 1.5, 1.6**
    @settings(max_examples=100, deadline=None)
    @given(bad_flavor=invalid_flavors, combination=combinations,
           name=server_names)
    def test_invalid_flavor_value_rejected_naming_supported_values(
            self, bad_flavor, combination, name):
        release, arch = combination
        _reset_state()
        ssm_stub = RecordingSsm()
        ec2_stub = RecordingEc2()
        _install(ssm_stub, ec2_stub)

        response = _launch({"name": name, "architecture": arch,
                            "ubuntu_version": release,
                            "ubuntu_flavor": bad_flavor})

        error = self._assert_rejected_without_side_effects(
            response, ssm_stub, ec2_stub)
        # The 400 names the two supported values (Req 1.4).
        rules = [e["rule"] for e in error["details"]["errors"]]
        assert build_domain.RULE_UBUNTU_FLAVOR_INVALID in rules
        assert "'pro'" in error["message"]
        assert "'standard'" in error["message"]

    # Feature: ubuntu-pro-build-servers, Property 3: Invalid launch
    # requests are rejected with no side effects
    # **Validates: Requirements 1.4, 1.5, 1.6**
    @settings(max_examples=100, deadline=None)
    @given(flavor=flavors, combination=unsupported_combinations,
           name=server_names)
    def test_unsupported_combination_rejected_identifying_it(
            self, flavor, combination, name):
        release, arch = combination
        _reset_state()
        ssm_stub = RecordingSsm()
        ec2_stub = RecordingEc2()
        _install(ssm_stub, ec2_stub)

        response = _launch({"name": name, "architecture": arch,
                            "ubuntu_version": release,
                            "ubuntu_flavor": flavor})

        error = self._assert_rejected_without_side_effects(
            response, ssm_stub, ec2_stub)
        # The rejection identifies the unsupported combination via the
        # release/arch validation rules (Req 1.5) — the supported
        # combinations are identical for both flavors.
        rules = [e["rule"] for e in error["details"]["errors"]]
        assert ("ubuntu_version_invalid" in rules
                or "ubuntu_version_arch_unsupported" in rules)


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 6: An unresolvable Pro AMI
# fails the launch closed
# ===========================================================================

class TestProperty6UnresolvableProAmiFailsClosed:

    # Feature: ubuntu-pro-build-servers, Property 6: An unresolvable Pro
    # AMI fails the launch closed
    # **Validates: Requirements 2.3**
    @settings(max_examples=100, deadline=None)
    @given(combination=combinations, name=server_names,
           fallback_errors=st.booleans())
    def test_pro_ssm_and_fallback_failure_fails_closed_with_audit(
            self, combination, name, fallback_errors):
        release, arch = combination
        _reset_state()
        # The Pro SSM read fails; the DescribeImages fallback either
        # raises or matches zero images. RunInstances is guard-railed:
        # any call trips an AssertionError.
        ssm_stub = RecordingSsm(error=True)
        ec2_stub = RecordingEc2(images=[], describe_error=fallback_errors)
        audit = _install(ssm_stub, ec2_stub)

        response = _launch({"name": name, "architecture": arch,
                            "ubuntu_version": release,
                            "ubuntu_flavor": PRO})

        # The launch failed (existing 502 LAUNCH_FAILED path) with zero
        # RunInstances calls (Req 2.3).
        assert response["statusCode"] == 502, response["body"]
        assert response["body"]["error"]["code"] == "LAUNCH_FAILED"
        assert ec2_stub.run_instances_calls == []
        assert _SERVERS.scan().get("Items", []) == []

        # An Audit_Log failure entry identifying the requested flavor,
        # release, and architecture was recorded (Req 2.3).
        entry = audit.only("failure")
        assert entry["details"]["ubuntu_flavor"] == PRO
        assert entry["details"]["ubuntu_version"] == release
        assert entry["details"]["architecture"] == arch
        if not fallback_errors:
            # The zero-match RuntimeError names flavor, release, and
            # architecture in the error message itself.
            message = response["body"]["error"]["message"]
            assert PRO in message
            assert release in message
            assert arch in message


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 9: Audit entries carry the
# flavor faithfully
# ===========================================================================

#: Requested flavor: explicit, or None meaning the field is omitted.
requested_flavors = st.sampled_from([PRO, STANDARD, None])
#: Configured default: stored 'pro'/'standard', or absent entirely.
configured_defaults = st.sampled_from([PRO, STANDARD, None])


def _seed_request(requested, configured, name, release, arch):
    """Store the configured default (when any) and build the launch
    body; returns (body, expected_effective_flavor)."""
    if configured is not None:
        _store_config({"ubuntu_flavor": configured})
    body = {"name": name, "architecture": arch, "ubuntu_version": release}
    if requested is not None:
        body["ubuntu_flavor"] = requested
    expected = requested if requested is not None else \
        (configured if configured is not None else STANDARD)
    return body, expected


class TestProperty9AuditCarriesFlavorFaithfully:

    # Feature: ubuntu-pro-build-servers, Property 9: Audit entries carry
    # the flavor faithfully
    # **Validates: Requirements 3.4, 3.5**
    @settings(max_examples=100, deadline=None)
    @given(requested=requested_flavors, configured=configured_defaults,
           combination=combinations, ami_id=ami_ids, name=server_names)
    def test_success_audit_carries_exactly_the_effective_flavor(
            self, requested, configured, combination, ami_id, name):
        release, arch = combination
        _reset_state()
        body, expected = _seed_request(requested, configured, name,
                                       release, arch)
        ssm_stub = RecordingSsm(value=ami_id)
        ec2_stub = RecordingEc2(allow_run_instances=True)
        audit = _install(ssm_stub, ec2_stub)

        response = _launch(body)

        assert response["statusCode"] == 201, response["body"]
        entry = audit.only("success")
        assert entry["details"]["ubuntu_flavor"] == expected

    # Feature: ubuntu-pro-build-servers, Property 9: Audit entries carry
    # the flavor faithfully
    # **Validates: Requirements 3.4, 3.5**
    @settings(max_examples=100, deadline=None)
    @given(requested=requested_flavors, configured=configured_defaults,
           combination=combinations, name=server_names)
    def test_post_determination_failure_audit_carries_effective_flavor(
            self, requested, configured, combination, name):
        release, arch = combination
        _reset_state()
        body, expected = _seed_request(requested, configured, name,
                                       release, arch)
        # Resolution fails after the effective flavor was determined.
        ssm_stub = RecordingSsm(error=True)
        ec2_stub = RecordingEc2(images=[])
        audit = _install(ssm_stub, ec2_stub)

        response = _launch(body)

        assert response["statusCode"] == 502, response["body"]
        entry = audit.only("failure")
        assert entry["details"]["ubuntu_flavor"] == expected

    # Feature: ubuntu-pro-build-servers, Property 9: Audit entries carry
    # the flavor faithfully
    # **Validates: Requirements 3.4, 3.5**
    @settings(max_examples=100, deadline=None)
    @given(bad_flavor=invalid_flavors, configured=configured_defaults,
           combination=combinations, name=server_names)
    def test_pre_determination_rejection_audits_the_raw_submitted_value(
            self, bad_flavor, configured, combination, name):
        release, arch = combination
        _reset_state()
        if configured is not None:
            _store_config({"ubuntu_flavor": configured})
        ssm_stub = RecordingSsm()
        ec2_stub = RecordingEc2()
        audit = _install(ssm_stub, ec2_stub)

        response = _launch({"name": name, "architecture": arch,
                            "ubuntu_version": release,
                            "ubuntu_flavor": bad_flavor})

        assert response["statusCode"] == 400, response["body"]
        # The rejection preceded flavor determination: the audit entry
        # carries the ubuntu_flavor exactly as submitted (Req 3.5).
        entry = audit.only("failure")
        assert entry["details"]["ubuntu_flavor"] == bad_flavor
