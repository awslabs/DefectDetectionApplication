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
Fix-checking property test for the JP7 dedicated capability gate
(jp7-ephemeral-runner-provisioning, task 5.3).

**Feature: jp7-ephemeral-runner-provisioning, Property 5: JP7 dedicated
capability gate accepts exactly the capable hosts**

**Validates: Requirements 2.7**

For any build request combining build_target JP7 with execution_mode
dedicated, and for any selected server record (generated
``lifecycle_state``, ``arch``, and ``ubuntu_version`` — including an
ABSENT field and arbitrary strings), the fixed
`build_domain.validate_build_request` accepts the request **if and only
if** the selected server exists, its ``lifecycle_state`` is 'running',
its ``arch`` is 'arm64', and its recorded ``ubuntu_version`` is exactly
'24.04' (an absent field is a pre-ec1dc38 record and is treated as the
22.04 host it is — NOT capable).

When the release is the failing condition, some error carries rule
``RULE_SERVER_OS_RELEASE_MISMATCH`` ('server_os_release_mismatch') with
a message naming BOTH the missing JP7 host capability (Ubuntu 24.04
arm64) and the server's actual release. When other conditions also fail,
their existing rules (server_not_found, server_not_running,
server_arch_mismatch) are still reported — the new gate masks no
existing rejection.

Every rejection happens AT VALIDATION TIME: the second property drives
the real request flow (`build_jobs.submit_build`) with a RECORDING
persistence seam in place of `put_new_job`, and asserts the recorder is
EMPTY for every rejected request — no Build_Job record exists for a
rejected request (jetpack7-support Req 6.4/6.5 fail-closed semantics)
and the dispatcher is never invoked for one.

--------------------------------------------------------------------------
Safety
--------------------------------------------------------------------------

No test calls real AWS, creates a table item, or dispatches a build.
`validate_build_request` is pure; the request-flow property replaces
`build_jobs.list_fleet_servers`, `effective_build_config`,
`put_new_job`, and `invoke_dispatcher` with in-process stubs before any
handler call, so persistence and dispatch are observed through
recorders only.

Run ONLY this file, from the repository root:

    python3 -m pytest \\
        test/backend-test/portal_builds/test_jp7_dedicated_capability_properties.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import json
import os
import sys
import types

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

_SUFFIX = "jp7-dedicated-capability"
os.environ["BUILD_JOBS_TABLE"] = f"dda-portal-build-jobs-{_SUFFIX}"
os.environ["BUILD_SERVERS_TABLE"] = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["SETTINGS_TABLE"] = f"dda-portal-settings-{_SUFFIX}"
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)

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
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    def create_response(status_code, body):
        return {"statusCode": status_code, "body": body}

    def get_user_from_event(event):
        return {"user_id": "capability-gate-user", "role": "PortalAdmin"}

    module.log_audit_event = log_audit_event
    module.create_response = create_response
    module.get_user_from_event = get_user_from_event
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


for _module in ("build_domain", "build_planner", "build_dispatcher",
                "build_fleet", "build_jobs", "build_source",
                "build_reconciliation", "shared_utils", "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

import build_domain  # noqa: E402
import build_jobs  # noqa: E402

# ---------------------------------------------------------------------------
# Recording persistence/dispatch seams for the request-flow property.
# `submit_build` resolves these collaborators as build_jobs module
# globals, so replacing the attributes routes every persistence write
# and dispatcher invocation into in-process recorders.
# ---------------------------------------------------------------------------

#: Fleet state served to `submit_build` (set per example).
_FLEET = []
#: The persistence recorder seam: every Build_Job record `submit_build`
#: would persist lands here — and NOWHERE else.
_RECORDED_JOBS = []
#: Dispatcher invocations recorded (never executed).
_DISPATCHED = []


def _stub_list_fleet_servers():
    return [dict(server) for server in _FLEET]


def _stub_effective_build_config():
    return dict(build_domain.DEFAULT_BUILD_CONFIG)


def _recording_put_new_job(job):
    record = dict(job)
    _RECORDED_JOBS.append(record)
    return record


def _recording_invoke_dispatcher(build_job_ids):
    _DISPATCHED.append(list(build_job_ids))


build_jobs.list_fleet_servers = _stub_list_fleet_servers
build_jobs.effective_build_config = _stub_effective_build_config
build_jobs.put_new_job = _recording_put_new_job
build_jobs.invoke_dispatcher = _recording_invoke_dispatcher


# ---------------------------------------------------------------------------
# Generated server records and requests.
#
# Each capability dimension draws the capable value with roughly even
# odds against generated incapable values (enumerated realistic values
# plus arbitrary strings), so accepted and rejected scenarios are BOTH
# well represented and the if-and-only-if is exercised from each side.
# The oracle is computed from the DRAWN values, so a text draw that
# happens to produce a capable value is still judged correctly.
# ---------------------------------------------------------------------------

#: Sentinel: the server record carries NO ubuntu_version field at all
#: (a pre-ec1dc38 record — a 22.04 host).
ABSENT = object()

_BAD_STATES = st.one_of(
    st.sampled_from(["stopped", "starting", "stopping", "terminated",
                     "impaired", ""]),
    st.text(max_size=10))
_BAD_ARCHES = st.one_of(
    st.sampled_from(["x86_64", "amd64", "aarch64", ""]),
    st.text(max_size=8))
_NON_NOBLE_RELEASES = st.one_of(
    st.just(ABSENT),
    st.sampled_from(["22.04", "20.04", "23.10", "24.10", "24.043", ""]),
    st.text(max_size=10))

_LIFECYCLE_STATES = st.one_of(st.just(build_domain.SERVER_STATE_RUNNING),
                              _BAD_STATES)
_ARCHES = st.one_of(st.just(build_domain.ARCH_ARM64), _BAD_ARCHES)
_UBUNTU_VERSIONS = st.one_of(st.just(build_domain.OS_RELEASE_NOBLE),
                             _NON_NOBLE_RELEASES)
_SOURCE_REFS = st.sampled_from(
    [None, "main", "release/2.4", "v1.6.2",
     "0123456789abcdef0123456789abcdef01234567"])


@st.composite
def _gate_scenarios(draw):
    """One JP7 + dedicated request against one generated fleet."""
    server_id = f"srv-{draw(st.uuids())}"
    exists = draw(st.booleans())
    lifecycle_state = draw(_LIFECYCLE_STATES)
    arch = draw(_ARCHES)
    ubuntu_version = draw(_UBUNTU_VERSIONS)
    # A fully capable DISTRACTOR server under a different id proves the
    # gate keys on the SELECTED server record, not on fleet contents.
    include_distractor = draw(st.booleans())
    source_ref = draw(_SOURCE_REFS)

    server = {
        "server_id": server_id,
        "name": "generated-dedicated-server",
        "lifecycle_state": lifecycle_state,
        "arch": arch,
    }
    if ubuntu_version is not ABSENT:
        server["ubuntu_version"] = ubuntu_version

    fleet = []
    if exists:
        fleet.append(server)
    if include_distractor:
        fleet.append({
            "server_id": f"{server_id}-distractor",
            "name": "capable-distractor",
            "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
            "arch": build_domain.ARCH_ARM64,
            "ubuntu_version": build_domain.OS_RELEASE_NOBLE,
        })

    body = {
        "targets": [build_domain.TARGET_JP7],
        "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
        "server_id": server_id,
    }
    if source_ref is not None:
        body["source_ref"] = source_ref

    recorded_release = (None if ubuntu_version is ABSENT
                        else ubuntu_version)
    # The gate's effective release: an absent (or empty) recorded value
    # is the 22.04 host the record predates.
    effective_release = recorded_release or build_domain.OS_RELEASE_JAMMY

    return {
        "server_id": server_id,
        "exists": exists,
        "lifecycle_state": lifecycle_state,
        "arch": arch,
        "recorded_release": recorded_release,
        "effective_release": effective_release,
        "fleet": fleet,
        "body": body,
    }


def _expected_accept(scenario):
    """Property 5's oracle: accept iff exists AND running AND arm64 AND
    recorded ubuntu_version exactly '24.04'."""
    return (scenario["exists"]
            and scenario["lifecycle_state"] == build_domain.SERVER_STATE_RUNNING
            and scenario["arch"] == build_domain.ARCH_ARM64
            and scenario["recorded_release"] == build_domain.OS_RELEASE_NOBLE)


def _describe(scenario):
    release = scenario["recorded_release"]
    return (f"server exists={scenario['exists']}, "
            f"lifecycle_state={scenario['lifecycle_state']!r}, "
            f"arch={scenario['arch']!r}, "
            f"ubuntu_version="
            f"{'<field absent>' if release is None else repr(release)}")


# ===========================================================================
# Property 5, part 1: the gate accepts exactly the capable hosts, the
# release rejection names the capability and the actual release, and no
# existing rule is masked.
# ===========================================================================

# Feature: jp7-ephemeral-runner-provisioning, Property 5: JP7 dedicated
# capability gate accepts exactly the capable hosts
# Validates: Requirements 2.7
@settings(max_examples=100, deadline=None)
@given(scenario=_gate_scenarios())
def test_gate_accepts_exactly_the_capable_hosts(scenario):
    """`validate_build_request` accepts a JP7 + dedicated request IFF the
    selected server exists, is running, is arm64, and records
    ubuntu_version exactly '24.04' (absent treated as '22.04'); a release
    failure carries RULE_SERVER_OS_RELEASE_MISMATCH naming Ubuntu 24.04
    arm64 and the actual release; co-failing conditions keep their
    existing rules (the gate masks nothing)."""
    result = build_domain.validate_build_request(
        scenario["body"], scenario["fleet"])

    expected = _expected_accept(scenario)
    observed_rules = [error["rule"] for error in result.errors]
    context = (f"{_describe(scenario)}\n"
               f"  observed valid={result.valid}, rules={observed_rules}")

    # --- The if-and-only-if of Property 5 ---
    assert result.valid == expected, (
        f"capability gate acceptance mismatch: expected "
        f"valid={expected}\n  {context}")
    if expected:
        assert result.errors == (), (
            f"accepted request must carry no errors\n  {context}")
        return

    # --- Release failure carries the new rule, naming capability and
    #     actual release ---
    release_capable = (scenario["recorded_release"]
                       == build_domain.OS_RELEASE_NOBLE)
    if scenario["exists"] and not release_capable:
        release_errors = [
            error for error in result.errors
            if error["rule"] == build_domain.RULE_SERVER_OS_RELEASE_MISMATCH]
        assert release_errors, (
            f"release is a failing condition but no error carries rule "
            f"'{build_domain.RULE_SERVER_OS_RELEASE_MISMATCH}'\n"
            f"  {context}")
        message = release_errors[0]["message"]
        assert "24.04" in message and "arm64" in message, (
            f"release rejection must name the missing Ubuntu 24.04 arm64 "
            f"capability\n  message: {message!r}\n  {context}")
        assert scenario["effective_release"] in message, (
            f"release rejection must name the server's actual release "
            f"({scenario['effective_release']!r})\n"
            f"  message: {message!r}\n  {context}")

    # --- The gate never fires against a capable release ---
    if release_capable:
        assert build_domain.RULE_SERVER_OS_RELEASE_MISMATCH \
            not in observed_rules, (
                f"release is '24.04' yet the release-mismatch rule "
                f"fired\n  {context}")

    # --- Existing rules still fire alongside the gate (nothing masked) ---
    if not scenario["exists"]:
        assert build_domain.RULE_SERVER_NOT_FOUND in observed_rules, (
            f"missing server must still report server_not_found\n"
            f"  {context}")
    else:
        if scenario["lifecycle_state"] != build_domain.SERVER_STATE_RUNNING:
            assert build_domain.RULE_SERVER_NOT_RUNNING in observed_rules, (
                f"non-running server must still report server_not_running "
                f"alongside the gate\n  {context}")
        if scenario["arch"] != build_domain.ARCH_ARM64:
            assert build_domain.RULE_SERVER_ARCH_MISMATCH \
                in observed_rules, (
                    f"arch mismatch must still report "
                    f"server_arch_mismatch alongside the gate\n  {context}")


# ===========================================================================
# Property 5, part 2: every rejection is at validation time — the
# persistence recorder seam stays EMPTY, so no Build_Job record exists
# for a rejected request (and the dispatcher is never invoked for one).
# ===========================================================================

# Feature: jp7-ephemeral-runner-provisioning, Property 5: JP7 dedicated
# capability gate accepts exactly the capable hosts
# Validates: Requirements 2.7
@settings(max_examples=100, deadline=None)
@given(scenario=_gate_scenarios())
def test_rejection_at_validation_creates_no_build_job(scenario):
    """Driving the REAL request flow (`build_jobs.submit_build`) with a
    recording persistence seam: a rejected JP7 + dedicated request
    returns the BUILD_REQUEST_INVALID envelope and writes NOTHING — the
    recorder holds no Build_Job record and no dispatcher invocation —
    while an accepted request persists exactly the one JP7 job
    (jetpack7-support Req 6.4/6.5 fail-closed semantics)."""
    _FLEET[:] = scenario["fleet"]
    _RECORDED_JOBS.clear()
    _DISPATCHED.clear()

    event = {
        "httpMethod": "POST",
        "resource": "/builds",
        "path": "/builds",
        "body": json.dumps(scenario["body"]),
    }
    response = build_jobs.submit_build(event, None)

    expected = _expected_accept(scenario)
    context = (f"{_describe(scenario)}\n"
               f"  response statusCode={response['statusCode']}\n"
               f"  recorded Build_Jobs={len(_RECORDED_JOBS)}, "
               f"dispatcher invocations={len(_DISPATCHED)}")

    if expected:
        assert response["statusCode"] == 201, (
            f"capable host must be accepted end-to-end\n  {context}")
        assert len(_RECORDED_JOBS) == 1, (
            f"accepted request must persist exactly one Build_Job\n"
            f"  {context}")
        job = _RECORDED_JOBS[0]
        assert job["build_target"] == build_domain.TARGET_JP7
        assert job["server_id"] == scenario["server_id"]
    else:
        assert response["statusCode"] == 400, (
            f"incapable host must be rejected at validation\n  {context}")
        assert response["body"]["error"]["code"] == "BUILD_REQUEST_INVALID", (
            f"rejection must use the BUILD_REQUEST_INVALID envelope\n"
            f"  {context}")
        # THE fail-closed assertion: the persistence recorder seam is
        # EMPTY — no Build_Job record exists for a rejected request.
        assert _RECORDED_JOBS == [], (
            f"rejected request persisted a Build_Job record\n  {context}")
        assert _DISPATCHED == [], (
            f"rejected request invoked the dispatcher\n  {context}")


# ===========================================================================
# Property 8 (preservation, task 6.4): dedicated validation outcomes
# invariant to ubuntu_version for every non-JP7 request.
#
# **Feature: jp7-ephemeral-runner-provisioning, Property 8: Dedicated
# validation outcomes invariant to ubuntu_version for every non-JP7
# request**
#
# **Validates: Requirements 3.5, 3.9**
#
# For any build request whose targets do NOT include JP7 (JP5, JP6,
# AMD64, AMD64_NVIDIA — either execution mode) and any two fleets
# differing ONLY in the selected server's recorded ``ubuntu_version``
# ('22.04', '24.04', an arbitrary other string, ABSENT), the fixed
# `validate_build_request` yields the SAME accept/reject outcome and
# the SAME ordered error rules for both fleets, and that outcome equals
# the task 2 frozen oracle (re-spelled below — the capability gate of
# 2.7 applies to JP7 only, so the release is never a validation input
# for these requests).
# ===========================================================================

#: The task 2 frozen oracle, deliberately RE-SPELLED here (the suite's
#: FROZEN_MATRIX convention) rather than imported across test files or
#: read back from the modules under test. Recorded on unfixed code.
_P8_SUPPORTED_TARGETS = frozenset(
    ["JP5", "JP6", "JP7", "AMD64", "AMD64_NVIDIA"])
_P8_REQUIRED_ARCH = {
    "JP5": "arm64", "JP6": "arm64", "JP7": "arm64",
    "AMD64": "x86_64", "AMD64_NVIDIA": "x86_64",
}
_P8_RULE_TARGETS_EMPTY = "targets_empty"
_P8_RULE_UNSUPPORTED_TARGET = "unsupported_target"
_P8_RULE_EXECUTION_MODE_MISSING = "execution_mode_missing"
_P8_RULE_EXECUTION_MODE_INVALID = "execution_mode_invalid"
_P8_RULE_SERVER_ID_MISSING = "server_id_missing"
_P8_RULE_SERVER_NOT_FOUND = "server_not_found"
_P8_RULE_SERVER_NOT_RUNNING = "server_not_running"
_P8_RULE_SERVER_ARCH_MISMATCH = "server_arch_mismatch"


def _p8_frozen_validation_rules(body, servers):
    """FROZEN decision oracle for `validate_build_request` outside the
    JP7+dedicated bug condition: the recorded accept/reject outcome and
    the ORDERED error rule ids, re-spelled from unfixed behavior (task
    2 observation run). The recorded ``ubuntu_version`` never appears:
    the release influenced NO outcome outside the bug condition."""
    rules = []
    targets = body.get("targets")
    if not isinstance(targets, list) or len(targets) == 0:
        rules.append(_P8_RULE_TARGETS_EMPTY)
        targets = []
    for target in targets:
        if target not in _P8_SUPPORTED_TARGETS:
            rules.append(_P8_RULE_UNSUPPORTED_TARGET)
    mode = body.get("execution_mode")
    if mode is None or mode == "":
        rules.append(_P8_RULE_EXECUTION_MODE_MISSING)
    elif mode not in ("ephemeral", "dedicated"):
        rules.append(_P8_RULE_EXECUTION_MODE_INVALID)
    if mode == "dedicated":
        server_id = body.get("server_id")
        if server_id is None or server_id == "":
            rules.append(_P8_RULE_SERVER_ID_MISSING)
        else:
            server = next((s for s in servers
                           if s.get("server_id") == server_id), None)
            if server is None:
                rules.append(_P8_RULE_SERVER_NOT_FOUND)
            else:
                if server.get("lifecycle_state") != "running":
                    rules.append(_P8_RULE_SERVER_NOT_RUNNING)
                for target in targets:
                    if target not in _P8_SUPPORTED_TARGETS:
                        continue
                    if server.get("arch") != _P8_REQUIRED_ARCH[target]:
                        rules.append(_P8_RULE_SERVER_ARCH_MISMATCH)
    return (len(rules) == 0, tuple(rules))


#: The four non-JP7 supported targets — the whole input domain of
#: Property 8 (JP7 never appears in a generated request here).
_P8_NON_JP7_TARGETS = ["JP5", "JP6", "AMD64", "AMD64_NVIDIA"]

_P8_LIFECYCLE_STATES = st.one_of(
    st.just("running"),
    st.sampled_from(["stopped", "starting", "stopping", "terminated",
                     "impaired", ""]),
    st.text(max_size=10))
#: Both real architectures (each is REQUIRED by some non-JP7 target),
#: plus realistic wrong spellings and arbitrary strings.
_P8_ARCHES = st.one_of(
    st.sampled_from(["arm64", "x86_64", "aarch64", "amd64", ""]),
    st.text(max_size=8))


@st.composite
def _p8_scenarios(draw):
    """One non-JP7 request plus everything needed to build fleets that
    differ ONLY in the selected server's recorded ubuntu_version."""
    targets = draw(st.lists(st.sampled_from(_P8_NON_JP7_TARGETS),
                            min_size=1, max_size=3))
    mode = draw(st.sampled_from(["ephemeral", "dedicated"]))
    server_id = f"srv-{draw(st.uuids())}"
    exists = draw(st.sampled_from([True, True, True, False]))
    lifecycle_state = draw(_P8_LIFECYCLE_STATES)
    arch = draw(_P8_ARCHES)
    #: The 'arbitrary other string' release variant of the task.
    other_release = draw(st.text(max_size=10))
    include_distractor = draw(st.booleans())
    # Occasionally omit server_id from a dedicated body so the frozen
    # server_id_missing outcome is exercised too.
    omit_server_id = (mode == "dedicated"
                      and draw(st.sampled_from([False] * 4 + [True])))

    body = {"targets": targets, "execution_mode": mode}
    if mode == "dedicated" and not omit_server_id:
        body["server_id"] = server_id

    return {
        "targets": targets,
        "mode": mode,
        "server_id": server_id,
        "exists": exists,
        "lifecycle_state": lifecycle_state,
        "arch": arch,
        "other_release": other_release,
        "include_distractor": include_distractor,
        "body": body,
    }


def _p8_fleet(scenario, release):
    """Build the fleet for ONE release variant. Everything except the
    selected server's recorded ubuntu_version is held constant across
    variants — any two of these fleets differ ONLY in that field."""
    fleet = []
    if scenario["exists"]:
        server = {
            "server_id": scenario["server_id"],
            "name": "generated-non-jp7-server",
            "lifecycle_state": scenario["lifecycle_state"],
            "arch": scenario["arch"],
        }
        if release is not ABSENT:
            server["ubuntu_version"] = release
        fleet.append(server)
    if scenario["include_distractor"]:
        # A CONSTANT bystander server (identical in every variant).
        fleet.append({
            "server_id": f"{scenario['server_id']}-bystander",
            "name": "constant-bystander",
            "lifecycle_state": "running",
            "arch": "arm64",
            "ubuntu_version": "22.04",
        })
    return fleet


# Feature: jp7-ephemeral-runner-provisioning, Property 8: Dedicated
# validation outcomes invariant to ubuntu_version for every non-JP7
# request
# Validates: Requirements 3.5, 3.9
@settings(max_examples=100, deadline=None)
@given(scenario=_p8_scenarios())
def test_non_jp7_outcomes_invariant_to_ubuntu_version(scenario):
    """For a non-JP7 request (either mode), fleets differing ONLY in
    the selected server's recorded ubuntu_version ('22.04', '24.04', an
    arbitrary other string, ABSENT) all yield the SAME accept/reject
    outcome and the SAME ordered error rules from the fixed
    `validate_build_request`, and that one outcome equals the task 2
    frozen oracle — the JP7 capability gate leaks into no non-JP7
    validation."""
    releases = ("22.04", "24.04", scenario["other_release"], ABSENT)
    outcomes = []
    for release in releases:
        fleet = _p8_fleet(scenario, release)
        result = build_domain.validate_build_request(
            scenario["body"], fleet)
        outcomes.append(
            (result.valid, tuple(e["rule"] for e in result.errors)))

    expected = _p8_frozen_validation_rules(
        scenario["body"], _p8_fleet(scenario, "22.04"))
    labels = ["'22.04'", "'24.04'",
              f"other={scenario['other_release']!r}", "<field absent>"]
    context = (
        f"body={scenario['body']!r}\n"
        f"  server exists={scenario['exists']}, "
        f"lifecycle_state={scenario['lifecycle_state']!r}, "
        f"arch={scenario['arch']!r}\n"
        + "\n".join(f"  ubuntu_version {label}: valid={valid}, "
                    f"rules={list(rules)}"
                    for label, (valid, rules) in zip(labels, outcomes))
        + f"\n  frozen oracle: valid={expected[0]}, "
          f"rules={list(expected[1])}")

    # --- Invariance: any two fleets agree (all four variants agree) ---
    assert len(set(outcomes)) == 1, (
        f"non-JP7 validation outcome varies with the selected server's "
        f"recorded ubuntu_version\n  {context}")

    # --- And the one outcome is the task 2 frozen oracle ---
    assert outcomes[0] == expected, (
        f"non-JP7 validation outcome drifted from the task 2 frozen "
        f"oracle\n  {context}")
