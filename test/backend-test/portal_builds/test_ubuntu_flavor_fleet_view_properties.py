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
Property-based tests for the Ubuntu flavor's read side — record
round-trip and legacy defaulting (ubuntu-pro-build-servers tasks 4.7
and 4.8).

* Property 7 — the effective flavor round-trips through the record and
  the fleet list: the record persisted before the launch response
  carries the effective flavor, and a subsequent fleet list reports
  exactly the stored flavor in any lifecycle state.
  **Validates: Requirements 3.1, 3.2**
* Property 8 — legacy records read as standard without write-back: a
  BuildServers record with no ``ubuntu_flavor`` field is reported as
  'standard' in every response including it, and the stored record
  afterwards still carries no ``ubuntu_flavor`` attribute.
  **Validates: Requirements 3.3**

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test calls real AWS: DynamoDB, EC2, and IAM are moto-mocked at
module scope (started before ``build_fleet`` is imported so its
module-level boto3 handles bind to the mock), and AMI resolution is
pinned to a moto-known AMI.

Run ONLY this file, from the repository root::

    python3 -m pytest \\
        test/backend-test/portal_builds/test_ubuntu_flavor_fleet_view_properties.py \\
        --noconftest -q
"""
import json
import os
import sys
import types

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

_SUFFIX = "ubuntu-flavor-fleet-view-props"
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

_EC2 = boto3.client("ec2", region_name="us-east-1")
_IAM = boto3.client("iam", region_name="us-east-1")
# The hardened launch profile referenced by RunInstances (design §2).
# Idempotent: moto's IAM backend is shared across module-level mocks,
# so a sibling module (e.g. test_build_fleet_lifecycle_and_audit) may
# already have created the profile in a full-directory run.
try:
    _IAM.create_instance_profile(InstanceProfileName="dda-build-role")
except _IAM.exceptions.EntityAlreadyExistsException:
    pass

import build_domain  # noqa: E402
import build_fleet  # noqa: E402

PRO = build_domain.UBUNTU_FLAVOR_PRO
STANDARD = build_domain.UBUNTU_FLAVOR_STANDARD

#: Any AMI id known to the moto EC2 backend (AMI resolution is pinned
#: to it — the Canonical catalog is not part of the moto backend).
_MOTO_AMI = _EC2.describe_images()["Images"][0]["ImageId"]

#: A real moto instance shared by legacy-record examples that carry an
#: instance_id, so reconciliation exercises its live-state write path.
_SHARED_INSTANCE_ID = _EC2.run_instances(
    ImageId=_MOTO_AMI, MinCount=1, MaxCount=1,
    InstanceType="m6g.4xlarge")["Instances"][0]["InstanceId"]

#: Every BuildServers lifecycle state (Req 3.2: "in any lifecycle
#: state").
LIFECYCLE_STATES = (
    build_domain.SERVER_STATE_PENDING,
    build_domain.SERVER_STATE_RUNNING,
    build_domain.SERVER_STATE_STOPPING,
    build_domain.SERVER_STATE_STOPPED,
    build_domain.SERVER_STATE_SHUTTING_DOWN,
    build_domain.SERVER_STATE_TERMINATED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_servers():
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})


def _launch(body):
    event = {"resource": "/build-servers", "httpMethod": "POST",
             "body": json.dumps(body)}
    return build_fleet.launch_build_server(event, None)


def _list_servers():
    response = build_fleet.list_build_servers(
        {"resource": "/build-servers", "httpMethod": "GET"}, None)
    assert response["statusCode"] == 200, response["body"]
    return {s["server_id"]: s for s in response["body"]["servers"]}


def _stored_record(server_id):
    return _SERVERS.get_item(Key={"server_id": server_id}).get("Item")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

flavors = st.sampled_from([PRO, STANDARD])
lifecycle_states = st.sampled_from(LIFECYCLE_STATES)
server_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1,
    max_size=24)


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 7: The effective flavor
# round-trips through the record and the fleet list
# ===========================================================================

class TestProperty7FlavorRoundTrip:

    # Feature: ubuntu-pro-build-servers, Property 7: The effective
    # flavor round-trips through the record and the fleet list
    # **Validates: Requirements 3.1, 3.2**
    @settings(max_examples=100, deadline=None)
    @given(flavor=flavors, name=server_names, state=lifecycle_states)
    def test_launched_flavor_is_stored_and_listed_in_any_state(
            self, flavor, name, state):
        _clear_servers()
        original_resolve = build_fleet.resolve_ubuntu_ami
        build_fleet.resolve_ubuntu_ami = \
            lambda *args, **kwargs: _MOTO_AMI
        try:
            response = _launch({"name": name, "architecture":
                                build_domain.ARCH_ARM64,
                                "ubuntu_flavor": flavor})
        finally:
            build_fleet.resolve_ubuntu_ami = original_resolve

        assert response["statusCode"] == 201, response["body"]
        server_id = response["body"]["server"]["server_id"]

        # The record persisted before the launch response carries the
        # effective flavor, exactly 'pro' or 'standard' (Req 3.1).
        record = _stored_record(server_id)
        assert record["ubuntu_flavor"] == flavor

        # Force the record into an arbitrary lifecycle state: the fleet
        # list must report the stored flavor regardless of state
        # (Req 3.2). Terminated records skip live reconciliation; other
        # states are reconciled against the live moto instance — either
        # way the stored flavor is what the list reports.
        _SERVERS.update_item(
            Key={"server_id": server_id},
            UpdateExpression="SET lifecycle_state = :state "
                             "REMOVE pending_action",
            ExpressionAttributeValues={":state": state},
        )

        listed = _list_servers()[server_id]
        assert listed["ubuntu_flavor"] == flavor
        # And the stored record still carries exactly that flavor.
        assert _stored_record(server_id)["ubuntu_flavor"] == flavor


# ===========================================================================
# Feature: ubuntu-pro-build-servers, Property 8: Legacy records read as
# standard without write-back
# ===========================================================================

class TestProperty8LegacyRecordsReadAsStandard:

    # Feature: ubuntu-pro-build-servers, Property 8: Legacy records read
    # as standard without write-back
    # **Validates: Requirements 3.3**
    @settings(max_examples=100, deadline=None)
    @given(name=server_names, state=lifecycle_states,
           with_instance=st.booleans())
    def test_flavorless_record_reports_standard_and_is_never_written_back(
            self, name, state, with_instance):
        _clear_servers()
        server_id = f"srv-legacy-{name}"
        item = {
            "server_id": server_id,
            "name": name,
            "instance_type": "m6g.4xlarge",
            "cpu_architecture": build_domain.ARCH_ARM64,
            "lifecycle_state": state,
            "last_state_change_at": 1_700_000_000_000,
            "created_at": 1_700_000_000_000,
        }
        if with_instance:
            # A live instance makes the list reconcile (and possibly
            # rewrite) the record — the write must never add a flavor.
            item["instance_id"] = _SHARED_INSTANCE_ID
        _SERVERS.put_item(Item=item)

        listed = _list_servers()[server_id]
        # Reported as 'standard' in every response including it
        # (Req 3.3).
        assert listed["ubuntu_flavor"] == STANDARD

        # The stored record afterwards still carries NO ubuntu_flavor
        # attribute: read-side defaulting only, never written back.
        stored = _stored_record(server_id)
        assert stored is not None
        assert "ubuntu_flavor" not in stored
