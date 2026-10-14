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
Unit tests for the JP7 dedicated capability gate (task 5.7 of
jp7-ephemeral-runner-provisioning): example cases and rule composition.

The gate lives in `build_domain.validate_build_request` under rule id
``RULE_SERVER_OS_RELEASE_MISMATCH`` ('server_os_release_mismatch'). A
server record with no ``ubuntu_version`` field predates commit ec1dc38
and is treated as the 22.04 host it is.

Covered example cases:
  - JP7 + dedicated REJECTED against recorded ``ubuntu_version``
    '22.04', '20.04', and ABSENT; ACCEPTED against '24.04'; the
    diagnostic names both the required capability (Ubuntu 24.04 arm64)
    and the server's actual release (Req 2.7)
  - rule composition: the not-found, not-running, and arch-mismatch
    rejections still fire and are NOT masked when the release also
    mismatches (Req 3.5)
  - JP5/JP6 dedicated requests are accepted against a '22.04', a
    '24.04', and a field-less server alike — the gate constrains JP7
    only (Req 3.9)

**Validates: Requirements 2.7, 3.5, 3.9**

All tests are pure-function unit tests on `validate_build_request`; no
AWS client is exercised. Run only this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_capability_gate_unit.py \\
        --noconftest -q
"""
import os
import sys
import types

import pytest

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

_SUFFIX = "jp7-capability-gate-unit"
os.environ["BUILD_JOBS_TABLE"] = f"dda-portal-build-jobs-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


# ---------------------------------------------------------------------------
# Minimal stand-ins for the Lambda layer modules the handlers import
# (same convention as the sibling suites).
# ---------------------------------------------------------------------------
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
    module.require_builds_submit = _identity_decorator_factory
    module.require_builds_cancel = _identity_decorator_factory
    module.super_user_only = lambda func: func
    return module


for _module in ("build_domain", "build_source",
                "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

import build_domain  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

SERVER_ID = "srv-jp7-gate-unit-0001"


def _server(lifecycle_state=build_domain.SERVER_STATE_RUNNING,
            arch=build_domain.ARCH_ARM64, ubuntu_version=...):
    """A Dedicated_Build_Server record. ``ubuntu_version=...`` (the
    default sentinel) omits the field entirely — a pre-ec1dc38 record."""
    record = {
        "server_id": SERVER_ID,
        "name": "dedicated-unit-1",
        "lifecycle_state": lifecycle_state,
        "arch": arch,
    }
    if ubuntu_version is not ...:
        record["ubuntu_version"] = ubuntu_version
    return record


def _request(targets, server_id=SERVER_ID):
    return {
        "targets": list(targets),
        "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
        "server_id": server_id,
    }


def _rules(result):
    return [error["rule"] for error in result.errors]


def _gate_messages(result):
    return [error["message"] for error in result.errors
            if error["rule"] == build_domain.RULE_SERVER_OS_RELEASE_MISMATCH]


# ===========================================================================
# Example cases: JP7 + dedicated against each recorded release (Req 2.7)
# ===========================================================================

class TestJp7DedicatedExampleCases:

    @pytest.mark.parametrize(
        "ubuntu_version, actual_release",
        [("22.04", "22.04"),   # explicit jammy record
         ("20.04", "20.04"),   # older release, still not capable
         (..., "22.04")],      # field ABSENT: pre-ec1dc38 22.04 host
        ids=["ubuntu_version=22.04", "ubuntu_version=20.04",
             "ubuntu_version-absent"])
    def test_jp7_rejected_on_non_noble_release(
            self, ubuntu_version, actual_release):
        """JP7 + dedicated on a running arm64 server whose recorded
        release is not 24.04 is rejected with the capability rule, and
        the diagnostic names the required capability and the actual
        release."""
        server = _server(ubuntu_version=ubuntu_version)
        result = build_domain.validate_build_request(
            _request([build_domain.TARGET_JP7]), [server])

        assert not result.valid
        assert _rules(result) == [
            build_domain.RULE_SERVER_OS_RELEASE_MISMATCH]

        (message,) = _gate_messages(result)
        # The diagnostic names the required capability...
        assert "Ubuntu 24.04 arm64" in message
        assert "JP7" in message
        # ...and the server's actual release (absent field => 22.04).
        assert f"Ubuntu {actual_release}" in message
        assert SERVER_ID in message

    def test_jp7_accepted_on_noble_release(self):
        """JP7 + dedicated on a running arm64 server recorded at 24.04
        is accepted with no errors."""
        server = _server(ubuntu_version="24.04")
        result = build_domain.validate_build_request(
            _request([build_domain.TARGET_JP7]), [server])
        assert result.valid
        assert result.errors == ()

    def test_rule_id_is_the_specified_identifier(self):
        """The gate's rule id is the design's identifier."""
        assert (build_domain.RULE_SERVER_OS_RELEASE_MISMATCH
                == "server_os_release_mismatch")


# ===========================================================================
# Rule composition: existing rules still fire, never masked (Req 3.5)
# ===========================================================================

class TestRuleComposition:

    def test_not_found_still_fires(self):
        """A JP7 dedicated request naming a server that does not exist
        is rejected by the existing not-found rule."""
        result = build_domain.validate_build_request(
            _request([build_domain.TARGET_JP7], server_id="srv-missing"),
            [_server(ubuntu_version="22.04")])
        assert not result.valid
        assert build_domain.RULE_SERVER_NOT_FOUND in _rules(result)

    def test_not_running_composes_with_release_mismatch(self):
        """A stopped 22.04 server rejects with BOTH the existing
        not-running rule AND the new release-mismatch rule — neither
        masks the other."""
        server = _server(lifecycle_state="stopped", ubuntu_version="22.04")
        result = build_domain.validate_build_request(
            _request([build_domain.TARGET_JP7]), [server])
        assert not result.valid
        rules = _rules(result)
        assert build_domain.RULE_SERVER_NOT_RUNNING in rules
        assert build_domain.RULE_SERVER_OS_RELEASE_MISMATCH in rules

    def test_arch_mismatch_composes_with_release_mismatch(self):
        """A running x86_64 22.04 server rejects a JP7 request with BOTH
        the existing arch-mismatch rule AND the new release-mismatch
        rule — neither masks the other."""
        server = _server(arch=build_domain.ARCH_X86_64,
                         ubuntu_version="22.04")
        result = build_domain.validate_build_request(
            _request([build_domain.TARGET_JP7]), [server])
        assert not result.valid
        rules = _rules(result)
        assert build_domain.RULE_SERVER_ARCH_MISMATCH in rules
        assert build_domain.RULE_SERVER_OS_RELEASE_MISMATCH in rules

    def test_all_three_failures_reported_together(self):
        """A stopped x86_64 22.04 server reports not-running,
        arch-mismatch, AND release-mismatch on one JP7 request."""
        server = _server(lifecycle_state="stopped",
                         arch=build_domain.ARCH_X86_64,
                         ubuntu_version="22.04")
        result = build_domain.validate_build_request(
            _request([build_domain.TARGET_JP7]), [server])
        assert not result.valid
        rules = _rules(result)
        assert build_domain.RULE_SERVER_NOT_RUNNING in rules
        assert build_domain.RULE_SERVER_ARCH_MISMATCH in rules
        assert build_domain.RULE_SERVER_OS_RELEASE_MISMATCH in rules


# ===========================================================================
# Preservation: the gate constrains JP7 only (Req 3.9)
# ===========================================================================

class TestNonJp7DedicatedUnconstrained:

    @pytest.mark.parametrize(
        "target",
        [build_domain.TARGET_JP5, build_domain.TARGET_JP6],
        ids=["JP5", "JP6"])
    @pytest.mark.parametrize(
        "ubuntu_version",
        ["22.04", "24.04", ...],
        ids=["ubuntu_version=22.04", "ubuntu_version=24.04",
             "ubuntu_version-absent"])
    def test_jp5_jp6_dedicated_accepted_on_any_release(
            self, target, ubuntu_version):
        """JP5/JP6 dedicated requests are accepted against a running
        arm64 server regardless of its recorded release — '22.04',
        '24.04', and the field-less pre-ec1dc38 record alike."""
        server = _server(ubuntu_version=ubuntu_version)
        result = build_domain.validate_build_request(
            _request([target]), [server])
        assert result.valid
        assert result.errors == ()
