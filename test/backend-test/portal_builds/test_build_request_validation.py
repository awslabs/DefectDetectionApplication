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
"""
Property test for build request validation in
``edge-cv-portal/backend/functions/build_domain.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 1.4, 1.8, 2.4, 2.6, 2.8**

The oracle below (``_expected_rules``) is transcribed directly from the
acceptance criteria — NOT from the implementation — so the test
independently decides which requests are valid:

  - the request selects at least one Build_Target (Req 1.8)
  - every selected target is one of JP5, JP6, AMD64, AMD64_NVIDIA (Req 1.4)
  - an execution mode is selected and is ephemeral or dedicated (Req 2.6)
  - the dedicated mode identifies a specific Dedicated_Build_Server (Req 2.6)
  - the selected server exists in the fleet and its lifecycle state is
    running (Req 2.4)
  - the selected server's CPU architecture matches the architecture required
    by every selected Build_Target: arm64 for JP5/JP6, x86_64 for
    AMD64/AMD64_NVIDIA (Req 2.8)

The property: ``validate_build_request`` accepts exactly the requests the
oracle finds valid, and every rejection names exactly the failing rule(s).
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure domain module from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402


# ---------------------------------------------------------------------------
# Requirement facts, transcribed from requirements.md (independent of the
# implementation's own tables).
# ---------------------------------------------------------------------------

_VALID_TARGETS = ["JP5", "JP6", "AMD64", "AMD64_NVIDIA"]

# Req 2.8: arm64 for JP5 and JP6, x86_64 for AMD64 and AMD64_NVIDIA.
_REQUIRED_ARCH = {
    "JP5": "arm64",
    "JP6": "arm64",
    "AMD64": "x86_64",
    "AMD64_NVIDIA": "x86_64",
}

_VALID_MODES = {"ephemeral", "dedicated"}


def _expected_rules(body, servers):
    """Oracle: the set of validation-rule identifiers a correct
    Build_Manager must reject this request with (empty set == valid),
    derived only from Requirements 1.4, 1.8, 2.4, 2.6, 2.8."""
    rules = set()

    targets = body.get("targets")
    if not isinstance(targets, list) or len(targets) == 0:
        rules.add(build_domain.RULE_TARGETS_EMPTY)  # Req 1.8
        targets = []
    if any(t not in _VALID_TARGETS for t in targets):
        rules.add(build_domain.RULE_UNSUPPORTED_TARGET)  # Req 1.4

    mode = body.get("execution_mode")
    if mode is None or mode == "":
        rules.add(build_domain.RULE_EXECUTION_MODE_MISSING)  # Req 2.6
    elif mode not in _VALID_MODES:
        rules.add(build_domain.RULE_EXECUTION_MODE_INVALID)  # Req 2.6

    if mode == "dedicated":
        server_id = body.get("server_id")
        if server_id is None or server_id == "":
            rules.add(build_domain.RULE_SERVER_ID_MISSING)  # Req 2.6
        else:
            server = next(
                (s for s in servers if s["server_id"] == server_id), None
            )
            if server is None:
                rules.add(build_domain.RULE_SERVER_NOT_FOUND)  # Req 2.4
            else:
                if server["lifecycle_state"] != "running":
                    rules.add(build_domain.RULE_SERVER_NOT_RUNNING)  # Req 2.4
                if any(
                    t in _VALID_TARGETS
                    and server["arch"] != _REQUIRED_ARCH[t]
                    for t in targets
                ):
                    rules.add(build_domain.RULE_SERVER_ARCH_MISMATCH)  # Req 2.8

    return rules


# ---------------------------------------------------------------------------
# Generators: arbitrary requests mixing valid and invalid shapes.
# ---------------------------------------------------------------------------

_INVALID_TARGETS = ["JP4", "amd64", "jp5", "ARM64", "", None, "AMD64_GPU"]

_SERVER_IDS = ["srv-1", "srv-2", "srv-3"]

_LIFECYCLE_STATES = [
    "pending", "running", "stopping", "stopped", "shutting-down", "terminated",
]

_server_st = st.fixed_dictionaries({
    "server_id": st.sampled_from(_SERVER_IDS),
    # Bias toward running so fully valid dedicated requests are common.
    "lifecycle_state": st.sampled_from(_LIFECYCLE_STATES + ["running"] * 4),
    "arch": st.sampled_from(["arm64", "x86_64"]),
})

_servers_st = st.lists(
    _server_st, min_size=0, max_size=3, unique_by=lambda s: s["server_id"]
)


@st.composite
def _request_st(draw):
    """A build request body plus a fleet state, spanning valid requests,
    each single-rule violation, and multi-rule combinations."""
    body = {}

    # Targets: usually present; sizes 0..4; mix of valid and invalid names.
    if draw(st.integers(min_value=0, max_value=9)) > 0:
        body["targets"] = draw(st.lists(
            st.sampled_from(_VALID_TARGETS * 3 + _INVALID_TARGETS),
            min_size=0,
            max_size=4,
        ))

    # Execution mode: valid modes weighted, plus missing/blank/invalid.
    mode = draw(st.sampled_from(
        ["ephemeral", "dedicated"] * 4
        + [None, "", "EPHEMERAL", "spot", "Dedicated", "OMIT"]
    ))
    if mode != "OMIT":
        body["execution_mode"] = mode

    # Server selection: known ids weighted, plus unknown/missing/blank.
    server_id = draw(st.sampled_from(
        _SERVER_IDS * 2 + ["srv-missing", None, "", "OMIT"]
    ))
    if server_id != "OMIT":
        body["server_id"] = server_id

    servers = draw(_servers_st)

    # Effective configuration: must not affect request validity.
    config = draw(st.sampled_from([
        None, {}, {"max_runtime_hours": 4, "region": "us-east-1"},
    ]))

    return body, servers, config


# Feature: portal-build-fleet-and-workflow-gates, Property 1: Build request validation accepts exactly the valid requests
@settings(max_examples=300)
@given(_request_st())
def test_build_request_validation_accepts_exactly_the_valid_requests(case):
    """For any request body and fleet state, validate_build_request accepts
    the request iff it satisfies every rule of Requirements 1.4, 1.8, 2.4,
    2.6, and 2.8, and every rejection names exactly the failing rule(s)
    with a non-empty message."""
    body, servers, config = case

    expected = _expected_rules(body, servers)
    result = build_domain.validate_build_request(body, servers, config)

    # Acceptance iff the requirement-derived oracle finds no violation.
    assert result.valid == (len(expected) == 0)

    if expected:
        # Each rejection names the failing rule(s): the reported rule
        # identifiers are exactly the violated rules, no more, no less.
        actual = {error["rule"] for error in result.errors}
        assert actual == expected
        # And every error carries a non-empty user-readable message.
        for error in result.errors:
            assert isinstance(error["message"], str) and error["message"]
    else:
        assert result.errors == ()
