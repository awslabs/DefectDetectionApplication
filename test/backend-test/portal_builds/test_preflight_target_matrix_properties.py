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
Preflight/target-matrix property tests
(build-fleet-execution-failures task 10.6).

**Property 11: Target and Mode Matrix** — _for any_ supported target and
valid execution mode, preflight SHALL preserve ``JP5``/``JP6 -> arm64``,
``AMD64``/``AMD64_NVIDIA -> x86_64``, the current component identities,
and intentional architecture-specific differences; invalid combinations
SHALL continue to fail existing validation.

**Property 12: Preflight Fails Before Costly Work** — _for any_ missing
or invalid repository path, agent executable, required tool,
architecture mapping, component identity, callback bus, source ref,
quoting contract, or required AWS environment, preflight SHALL emit a
redacted actionable diagnostic and prevent the build/publish step; for
any valid contract it SHALL not change the resulting command semantics.

**Validates: Requirements 2.8, 2.9, 2.10, 2.11, 2.12, 3.7, 3.9**

These properties GENERALIZE (never duplicate) the focused examples of
the sibling ``test_preflight_agent_contract.py``: where that file pins
individual counterexamples, this file generates the full target x mode x
component-identity matrix plus arbitrary valid/invalid paths, quoting
payloads (shell-special strings), source refs, and callback settings.

PURE + mocked only: no AWS call, SSM send, instance action, deployment,
artifact publication, or live build. The dispatch flow tests patch the
dispatcher's persistence and SSM helper seams with recorders, so "zero
build/publish calls" is asserted against captured recorders, never a
live service.

Run ONLY this file (with the sibling no-live-action contract), from the
repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_preflight_target_matrix_properties.py \\
        test/backend-test/portal_builds/test_no_live_validation_contract.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import json
import os
import shlex
import sys
import types
from unittest import mock

from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3 handles
# and env-derived configuration at import time. Nothing live is called.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ.setdefault("BUILD_JOBS_TABLE", "dda-portal-build-jobs-matrix")
os.environ.setdefault("BUILD_SERVERS_TABLE",
                      "dda-portal-build-servers-matrix")
os.environ.pop("BUILD_REPO_DIR", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

# Stand-in for the Lambda layer module the handlers import.
if "shared_utils" not in sys.modules:
    _stub = types.ModuleType("shared_utils")
    _stub.log_audit_event = lambda **kwargs: None
    sys.modules["shared_utils"] = _stub

import build_domain  # noqa: E402
import build_dispatcher  # noqa: E402
import build_reconciliation as br  # noqa: E402

# ---------------------------------------------------------------------------
# The FROZEN intentional matrix (Property 11 / Req 3.7): re-spelled here
# on purpose, so a production drift in build_domain fails this oracle.
# ---------------------------------------------------------------------------
FROZEN_MATRIX = {
    "JP5": ("arm64", "aws.edgeml.dda.LocalServer.arm64JP5"),
    "JP6": ("arm64", "aws.edgeml.dda.LocalServer.arm64JP6"),
    "JP7": ("arm64", "aws.edgeml.dda.LocalServer.arm64JP7"),
    "AMD64": ("x86_64", "aws.edgeml.dda.LocalServer.amd64"),
    "AMD64_NVIDIA": ("x86_64", "aws.edgeml.dda.LocalServer.amd64Nvidia"),
}
VALID_MODES = (build_domain.EXECUTION_MODE_DEDICATED,
               build_domain.EXECUTION_MODE_EPHEMERAL)

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

targets = st.sampled_from(sorted(FROZEN_MATRIX))
modes = st.sampled_from(VALID_MODES)

#: A secret-shaped canary that the shared redaction MUST remove from
#: every application-controlled sink (an AWS access key id shape).
SECRET_CANARY = "AKIAAAAABBBBCCCCDDDD"

_path_segment = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"),
    min_size=1, max_size=12)

#: Valid absolute repository directories (design preflight check 1).
valid_repo_dirs = st.lists(_path_segment, min_size=1, max_size=4).map(
    lambda segments: "/" + "/".join(segments))

#: Invalid repository directories: empty, whitespace, relative, non-str.
invalid_repo_dirs = st.one_of(
    st.sampled_from(["", "   ", "\t"]),
    _path_segment,                       # relative
    st.lists(_path_segment, min_size=1, max_size=3).map("/".join),
    st.none(),
    st.integers(),
)

#: Source refs that DO round-trip the generated command's shlex quoting
#: contract — including shell-special payloads (Req 2.9 quoting).
quotable_source_refs = st.one_of(
    st.sampled_from([
        "feature/x y", "a;rm -rf /", "$(evil)", "a'b\"c", "release-1.2",
        "refs/heads/main", "`backtick`", "&& echo pwned", "|pipe", "-flag",
    ]),
    # no line-boundary characters: real refs never carry them, and the
    # single-invocation-line oracle below splits on them
    st.text(min_size=1, max_size=40).filter(
        lambda ref: ref == "".join(ref.splitlines())
        and build_dispatcher.quoting_round_trips(ref)),
)

#: TRUTHY values that can never satisfy the quoting round trip
#: (non-text shapes; the round trip holds for every ordinary string).
#: Falsy values (0, False, "", empty list) are deliberately excluded:
#: the command generator treats them as "no ref selected" (`if
#: source_ref:`), which is a VALID contract, not a quoting violation.
unquotable_source_refs = st.one_of(
    st.integers().filter(bool),
    st.just(True),
    st.floats(allow_nan=False).filter(bool),
    st.lists(st.text(max_size=3), min_size=1, max_size=2),
    st.binary(min_size=1, max_size=8),
)

#: Free-form target names outside the supported set (Req 3.7:
#: intentionally invalid combinations keep failing).
unsupported_targets = st.text(min_size=1, max_size=20).filter(
    lambda t: t not in FROZEN_MATRIX)
unsupported_modes = st.text(min_size=1, max_size=20).filter(
    lambda m: m not in VALID_MODES)


def _job(target, mode, component_name="__MATCHING__", source_ref=None,
         job_id="job-matrix-1"):
    definition = build_domain.BUILD_TARGETS.get(target, {})
    if component_name == "__MATCHING__":
        component_name = definition.get("component_name")
    snapshot = {"max_runtime_hours": 4}
    if source_ref is not None:
        snapshot["source_ref"] = source_ref
    return {
        "build_job_id": job_id,
        "build_target": target,
        "execution_mode": mode,
        "component_name": component_name,
        "status": build_domain.STATUS_QUEUED,
        "config_snapshot": snapshot,
    }


REPO_DIR = "/home/ubuntu/DefectDetectionApplication"


# ===========================================================================
# **Property 11: Target and Mode Matrix**
# **Validates: Requirements 2.9, 2.11, 3.7**
# ===========================================================================

class TestProperty11TargetAndModeMatrix:

    @settings(max_examples=200, deadline=None)
    @given(target=targets, mode=modes, source_ref=quotable_source_refs,
           repo_dir=valid_repo_dirs)
    def test_every_supported_combination_maps_exactly(
            self, target, mode, source_ref, repo_dir):
        """JP5/JP6 -> arm64 and AMD64/AMD64_NVIDIA -> x86_64 with the
        current component identities survive preflight for EVERY
        supported target x mode x valid path/ref combination — no
        cross-target assumption is introduced (Req 2.9, 3.7)."""
        decision = build_dispatcher.decide_preflight(
            _job(target, mode, source_ref=source_ref), repo_dir,
            event_bus="dda-bus", region="us-east-1")
        arch, component = FROZEN_MATRIX[target]
        assert decision.ok, decision.failures
        assert decision.checks["required_arch"] == arch
        assert decision.checks["component_name"] == component
        assert decision.checks["execution_mode"] == mode
        assert decision.checks["repo_dir"] == repo_dir

    @settings(max_examples=200, deadline=None)
    @given(target=targets, mode=modes, other=targets)
    def test_cross_wired_component_identity_always_fails(
            self, target, mode, other):
        """A job recording ANOTHER supported target's component identity
        is cross-wired and must fail preflight in every mode; recording
        the target's OWN component always passes (Req 2.9, 3.7)."""
        own_component = FROZEN_MATRIX[target][1]
        other_component = FROZEN_MATRIX[other][1]
        decision = build_dispatcher.decide_preflight(
            _job(target, mode, component_name=other_component), REPO_DIR,
            event_bus="dda-bus", region="us-east-1")
        if other == target:
            assert decision.ok, decision.failures
        else:
            assert not decision.ok
            assert "component_identity_mismatch" in decision.failures
        # the matching identity is always valid
        assert build_dispatcher.decide_preflight(
            _job(target, mode, component_name=own_component), REPO_DIR,
            event_bus="dda-bus", region="us-east-1").ok

    @settings(max_examples=200, deadline=None)
    @given(bad_target=unsupported_targets, mode=modes,
           bad_mode=unsupported_modes, target=targets)
    def test_intentional_restrictions_remain_valid(
            self, bad_target, mode, bad_mode, target):
        """Unsupported targets and execution modes keep failing the
        existing validation — preflight adds no new combination and
        removes no intentional restriction (Req 2.9, 3.7)."""
        decision = build_dispatcher.decide_preflight(
            _job(bad_target, mode, component_name=None), REPO_DIR,
            event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "unsupported_target" in decision.failures

        decision = build_dispatcher.decide_preflight(
            _job(target, bad_mode), REPO_DIR,
            event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "unsupported_execution_mode" in decision.failures

    def test_the_frozen_matrix_is_exactly_the_domain_table(self):
        """The generator's frozen matrix and build_domain agree exactly:
        no target added, none removed, no mapping changed (Req 3.7)."""
        assert set(FROZEN_MATRIX) == set(build_domain.BUILD_TARGETS)
        for target, (arch, component) in FROZEN_MATRIX.items():
            assert build_domain.BUILD_TARGETS[target]["required_arch"] \
                == arch
            assert build_domain.BUILD_TARGETS[target]["component_name"] \
                == component

    @settings(max_examples=150, deadline=None)
    @given(target=targets, mode=modes, source_ref=quotable_source_refs,
           repo_dir=valid_repo_dirs)
    def test_valid_command_equivalence(self, target, mode, source_ref,
                                       repo_dir):
        """For any VALID contract, preflight does not change the
        resulting command semantics (Property 12's preservation arm,
        Req 3.9): the generated body still ends with exactly the frozen
        agent invocation (path + KEY=VALUE contract, correctly quoted),
        the guard validates the SAME resolved directory, and the guard
        precedes the invocation."""
        job = _job(target, mode, source_ref=source_ref)
        # decide_preflight is a pure READ: computing it must not alter
        # the generated command text (byte equality across the call).
        with mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                               "https://example.invalid/dda.git"):
            before = build_dispatcher.agent_run_body(job, repo_dir)
            decision = build_dispatcher.decide_preflight(
                job, repo_dir, event_bus="dda-bus", region="us-east-1")
            after = build_dispatcher.agent_run_body(job, repo_dir)
        assert decision.ok, decision.failures
        assert before == after

        last_line = after.splitlines()[-1]
        expected = [
            "bash",
            shlex.quote(os.path.join(repo_dir, "scripts",
                                     "portal-build-agent.sh")),
            shlex.quote(f"BUILD_JOB_ID={job['build_job_id']}"),
            shlex.quote(f"BUILD_TARGET={target}"),
            shlex.quote(f"EVENT_BUS={build_dispatcher.BUILD_EVENT_BUS}"),
            shlex.quote(f"SOURCE_REF={source_ref}"),
        ]
        assert last_line == " ".join(expected)
        # the shell-special ref survives the quoting contract intact
        parsed = shlex.split(last_line)
        assert parsed[-1] == f"SOURCE_REF={source_ref}"
        assert parsed[3] == f"BUILD_TARGET={target}"
        # the guard sits between the exports and the invocation and
        # targets the same resolved directory (no drift)
        guard_at = after.index(br.PREFLIGHT_FAILURE_MARKER)
        assert guard_at < after.index(last_line)
        assert shlex.quote(repo_dir) in "\n".join(
            build_dispatcher.preflight_guard_commands(repo_dir))


# ===========================================================================
# **Property 12: Preflight Fails Before Costly Work** — pure decision
# **Validates: Requirements 2.8, 2.9, 2.10, 2.12, 3.9**
# ===========================================================================

#: The generated invalidity dimensions of the dispatch contract.
_INVALID_DIMENSIONS = (
    "repository_dir_invalid",
    "component_identity_mismatch",
    "unsupported_target",
    "unsupported_execution_mode",
    "callback_bus_missing",
    "callback_region_missing",
    "quoting_contract_violated",
)


@st.composite
def invalid_contracts(draw):
    """One dispatch contract with at least one generated invalidity,
    returning (job, repo_dir, event_bus, region, expected_failures)."""
    target = draw(targets)
    mode = draw(modes)
    job = _job(target, mode, source_ref=draw(st.one_of(
        st.none(), quotable_source_refs)))
    repo_dir = draw(valid_repo_dirs)
    event_bus, region = "dda-bus", "us-east-1"
    dimensions = draw(st.lists(st.sampled_from(_INVALID_DIMENSIONS),
                               min_size=1, max_size=3, unique=True))
    expected = set()
    for dimension in dimensions:
        if dimension == "repository_dir_invalid":
            repo_dir = draw(invalid_repo_dirs)
            expected.add("repository_dir_invalid")
        elif dimension == "component_identity_mismatch":
            other = draw(targets.filter(lambda t: t != target))
            job["component_name"] = FROZEN_MATRIX[other][1]
            expected.add("component_identity_mismatch")
        elif dimension == "unsupported_target":
            job["build_target"] = draw(unsupported_targets)
            job["component_name"] = None
            expected.add("unsupported_target")
        elif dimension == "unsupported_execution_mode":
            job["execution_mode"] = draw(unsupported_modes)
            expected.add("unsupported_execution_mode")
        elif dimension == "callback_bus_missing":
            event_bus = ""
            expected.add("callback_bus_missing")
        elif dimension == "callback_region_missing":
            region = ""
            expected.add("callback_region_missing")
        elif dimension == "quoting_contract_violated":
            job["config_snapshot"]["source_ref"] = draw(
                unquotable_source_refs)
            expected.add("quoting_contract_violated:source_ref")
    if "unsupported_target" in expected:
        # An unsupported target never reaches the component-identity
        # check (the existing validation fails first), so a cross-wired
        # component applied in the same draw is not separately named.
        expected.discard("component_identity_mismatch")
    return job, repo_dir, event_bus, region, expected


class TestProperty12InvalidContractDecision:

    @settings(max_examples=250, deadline=None)
    @given(contract=invalid_contracts())
    def test_every_invalid_contract_is_refused_with_stable_vocabulary(
            self, contract):
        """Any generated combination of invalid path, target,
        component identity, mode, callback bus/region, or quoting
        payload is refused BEFORE dispatch, and every generated
        invalidity is named by its stable actionable identifier —
        no raw configuration value leaks into the vocabulary
        (Req 2.8, 2.9, 2.10)."""
        job, repo_dir, event_bus, region, expected = contract
        decision = build_dispatcher.decide_preflight(
            job, repo_dir, event_bus=event_bus, region=region)
        assert not decision.ok
        assert expected.issubset(set(decision.failures)), (
            expected, decision.failures)
        # actionable = stable machine-readable vocabulary only
        for failure in decision.failures:
            assert failure.split(":")[0] in {
                "repository_dir_invalid", "component_identity_mismatch",
                "unsupported_target", "unsupported_execution_mode",
                "callback_bus_missing", "callback_region_missing",
                "quoting_contract_violated"}

    @settings(max_examples=150, deadline=None)
    @given(repo_dir=valid_repo_dirs)
    def test_generated_guard_fails_before_the_agent_invocation(
            self, repo_dir):
        """For any resolved directory, the generated on-server guard
        (machine-side path/script portion of the contract) validates
        the repository and agent script BEFORE the invocation, exits
        with the preflight code + marker, and its marker classifies as
        the stable COMMAND_PREFLIGHT_FAILED (Req 2.8, 2.12)."""
        job = _job("AMD64", build_domain.EXECUTION_MODE_DEDICATED)
        body = build_dispatcher.agent_run_body(job, repo_dir)
        guard = "\n".join(build_dispatcher.preflight_guard_commands(
            repo_dir))
        assert shlex.quote(repo_dir) in guard
        assert f"exit {build_dispatcher.PREFLIGHT_EXIT_CODE}" in guard
        assert body.index(br.PREFLIGHT_FAILURE_MARKER) < \
            body.index(body.splitlines()[-1])
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation={
                "Status": "Failed",
                "ResponseCode": build_dispatcher.PREFLIGHT_EXIT_CODE,
                "StandardErrorContent":
                    f"{br.PREFLIGHT_FAILURE_MARKER} check=agent_script "
                    f"path={repo_dir}/scripts/portal-build-agent.sh"})
        assert outcome.decided
        assert outcome.error_code == br.CODE_COMMAND_PREFLIGHT_FAILED


# ===========================================================================
# **Property 12: Preflight Fails Before Costly Work** — driven dispatch
# flow with recorders: ZERO build/publish calls on invalid contracts
# **Validates: Requirements 2.8, 2.10, 2.11, 2.12, 3.9**
# ===========================================================================

class _Recorder:
    """Recording seam double: captures every call, returns a fixed
    value, and lets tests assert exact-zero costly work."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _drive_dedicated_dispatch(job, server, now=1_754_500_000_000):
    """Run verify_and_start_dedicated with EVERY persistence and SSM
    seam replaced by recorders (mocked services; nothing live). Returns
    the dict of recorders for assertion."""
    recorders = {
        # costly-work seams: must stay EMPTY on invalid contracts
        "send_agent": _Recorder(("cmd-recorded", "stream-recorded")),
        "send_shell_command": _Recorder("cmd-recorded"),
        "run_runner_instance": _Recorder("i-recorded"),
        # persistence seams
        "transition_job": _Recorder(True),
        "update_job_fields": _Recorder(None),
        "release_server": _Recorder(True),
        "audit": _Recorder(None),
        # pre-dispatch verification (mocked pgrep: clean)
        "run_shell_sync": _Recorder(""),
    }
    with mock.patch.multiple(build_dispatcher, **recorders):
        build_dispatcher.verify_and_start_dedicated(job, server, now)
    return recorders


def _captured_sink_text(recorders):
    """Every application-controlled sink captured by the recorders,
    JSON-serialized for canary scanning."""
    return json.dumps(
        {name: recorder.calls for name, recorder in recorders.items()},
        default=str, sort_keys=True)


class TestProperty12ZeroCostlyWorkOnInvalidContracts:

    @settings(max_examples=60, deadline=None)
    @given(target=targets, data=st.data())
    def test_invalid_contract_never_reaches_build_or_publish(
            self, target, data):
        """Driving the REAL dedicated dispatch flow with an invalid
        contract (cross-wired component identity, invalid recorded
        repository directory, or non-quotable truthy ref): the job
        fails with the stable COMMAND_PREFLIGHT_FAILED code through the
        common terminal flow, the allocation is released, and the
        recorders prove ZERO build/publish calls — no agent
        SendCommand, no RunInstances, no shell command beyond the
        mocked pre-dispatch pgrep verification (Req 2.8, 2.11, 2.12)."""
        kind = data.draw(st.sampled_from(
            ["component", "repo_dir", "quoting"]), label="invalidity")
        job = _job(target, build_domain.EXECUTION_MODE_DEDICATED,
                   job_id="job-invalid-1")
        server = {"server_id": "srv-1", "instance_id": "i-000001",
                  "repo_dir": REPO_DIR,
                  "running_build_job_id": "job-invalid-1"}
        if kind == "component":
            mismatched = data.draw(
                targets.filter(lambda t: t != target), label="other")
            job["component_name"] = FROZEN_MATRIX[mismatched][1]
        elif kind == "repo_dir":
            # A RECORDED relative directory is used as-is by resolution
            # (a recorded value always wins) and is invalid for the
            # dispatch contract; empty values would legitimately fall
            # through to the configured default (Req 5.3), so they are
            # not an invalid recorded contract.
            server["repo_dir"] = data.draw(st.sampled_from(
                ["relative/clone", "opt/dda/clone", "home/ubuntu/DDA"]),
                label="bad_dir")
        else:
            job["config_snapshot"]["source_ref"] = data.draw(
                unquotable_source_refs, label="bad_ref")

        recorders = _drive_dedicated_dispatch(job, server)

        # ZERO costly work (Req 2.12): nothing beyond the mocked pgrep.
        assert recorders["send_agent"].calls == []
        assert recorders["run_runner_instance"].calls == []
        assert recorders["send_shell_command"].calls == []
        assert len(recorders["run_shell_sync"].calls) == 1  # pgrep only

        # The stable terminal outcome through the common flow (Req 2.8):
        # exactly one failed transition, never queued -> building.
        transitions = recorders["transition_job"].calls
        assert len(transitions) == 1
        args, kwargs = transitions[0]
        assert args[0] == "job-invalid-1"
        assert args[2] == build_domain.STATUS_FAILED
        extra = kwargs["extra"]
        assert extra["error"]["code"] == \
            build_dispatcher.ERROR_COMMAND_PREFLIGHT_FAILED
        assert "before any build/publish work" in \
            extra["error"]["message"]
        # the held allocation is released for the failed job
        assert recorders["release_server"].calls == [
            (("srv-1", "job-invalid-1"), {})]
        # the sanitized preflight evidence was recorded separately
        recorded_fields = [call[0][1]
                           for call in recorders["update_job_fields"].calls]
        assert any("preflight" in fields for fields in recorded_fields)

    def test_preflight_failure_diagnostic_is_redacted(self):
        """A secret-shaped value inside the recorded repository
        directory never survives into ANY captured sink — persisted
        preflight evidence, terminal error record, or audit details
        (Req 2.10): the shared redaction runs before every write."""
        job = _job("AMD64", build_domain.EXECUTION_MODE_DEDICATED,
                   job_id="job-redact-1",
                   # cross-wired identity makes the contract invalid
                   component_name=FROZEN_MATRIX["JP5"][1])
        server = {"server_id": "srv-1", "instance_id": "i-000001",
                  "repo_dir": f"/tmp/{SECRET_CANARY}/clone",
                  "running_build_job_id": "job-redact-1"}
        recorders = _drive_dedicated_dispatch(job, server)
        assert recorders["send_agent"].calls == []
        captured = _captured_sink_text(recorders)
        assert SECRET_CANARY not in captured
        assert br.REDACTED in captured

    def test_valid_contract_proceeds_unchanged(self):
        """The preservation arm (Req 3.9): a valid dedicated AMD64
        contract passes the same driven flow — queued -> building, ONE
        agent send through the seam with the SAME resolved directory,
        and no preflight failure."""
        job = _job("AMD64", build_domain.EXECUTION_MODE_DEDICATED,
                   job_id="job-valid-1")
        server = {"server_id": "srv-1", "instance_id": "i-000001",
                  "repo_dir": REPO_DIR,
                  "running_build_job_id": "job-valid-1"}
        recorders = _drive_dedicated_dispatch(job, server)

        transitions = recorders["transition_job"].calls
        assert len(transitions) == 1
        args, _ = transitions[0]
        assert args[1] == build_domain.STATUS_QUEUED
        assert args[2] == build_domain.STATUS_BUILDING
        sends = recorders["send_agent"].calls
        assert len(sends) == 1
        send_args, send_kwargs = sends[0]
        assert send_args[0]["build_job_id"] == "job-valid-1"
        assert send_args[1] == "i-000001"
        assert send_args[2] == REPO_DIR
        assert recorders["release_server"].calls == []
        assert recorders["run_runner_instance"].calls == []


# ===========================================================================
# **Property 11: Target and Mode Matrix** — runtime budgeting arm
# **Validates: Requirements 2.11, 2.17, 3.7**
# ===========================================================================

class TestProperty11RuntimeBudgetMatrix:
    """Runtime budgeting preserves the target/mode matrix (Req 2.17):
    snapshotted budgets resolve in design order — target/mode override,
    target default, snapshotted ``max_runtime_hours``, compatibility
    default — and a budget recorded for ANOTHER target never leaks
    into this job's resolution."""

    @settings(max_examples=80, deadline=None)
    @given(target=targets, mode=modes,
           override_hours=st.one_of(
               st.none(), st.integers(min_value=1, max_value=48)),
           default_hours=st.one_of(
               st.none(), st.integers(min_value=1, max_value=48)),
           max_runtime_hours=st.one_of(
               st.none(), st.integers(min_value=1, max_value=48)))
    def test_budget_resolution_has_no_cross_target_leakage(
            self, target, mode, override_hours, default_hours,
            max_runtime_hours):
        other_target = next(t for t in sorted(FROZEN_MATRIX)
                            if t != target)
        budgets = {
            # A decoy budget on another target must never apply here.
            other_target: {mode: {"hard_runtime_hours": 99}},
        }
        target_entry = {}
        if override_hours is not None:
            target_entry[mode] = {"hard_runtime_hours": override_hours}
        if default_hours is not None:
            target_entry["default"] = {"hard_runtime_hours": default_hours}
        if target_entry:
            budgets[target] = target_entry
        snapshot = {"runtime_budgets": budgets}
        if max_runtime_hours is not None:
            snapshot["max_runtime_hours"] = max_runtime_hours

        budget = br.effective_budget(
            {"build_target": target, "execution_mode": mode,
             "config_snapshot": snapshot})

        hour_ms = 60 * 60 * 1000
        if override_hours is not None:
            assert budget.source == "target_mode_override"
            assert budget.hard_runtime_ms == override_hours * hour_ms
        elif default_hours is not None:
            assert budget.source == "target_default"
            assert budget.hard_runtime_ms == default_hours * hour_ms
        elif max_runtime_hours is not None:
            assert budget.source == "snapshot_max_runtime_hours"
            assert budget.hard_runtime_ms == max_runtime_hours * hour_ms
        else:
            assert budget.source == "compatibility_default"
        # The decoy's 99 h never applies unless it IS the resolution.
        if override_hours != 99 and default_hours != 99 \
                and max_runtime_hours != 99:
            assert budget.hard_runtime_ms != 99 * hour_ms
