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
Dispatch preflight decision logic and agent-script contract tests
(build-fleet-execution-failures tasks 7.1, 7.2, 7.3, 7.5).

**Validates: Requirements 2.8, 2.9, 2.21, 2.22, 2.23, 3.7, 3.9, 3.15**

Evidence gate (historical-evidence.md task 3.3):
  - Row 1 (CONFIRMED, with its caveat): the 2026-08-06 AMD64 dispatch
    executed /opt/dda/DefectDetectionApplication/scripts/
    portal-build-agent.sh, which did not exist, while the fleet
    bootstrap had cloned to /home/ubuntu/DefectDetectionApplication.
    The resolver's evidenced fallback correction and the preflight
    repository/script validation are the authorized corrections; fixing
    the path alone is NOT claimed sufficient for a successful build.
  - Row 8 (CONFIRMED): an invalid path/script contract reached a
    dispatched SSM command with zero pre-checks; task 7.1's preflight
    (local contract decision + generated guard + agent preflight) is the
    authorized correction. Preflight is not claimed to address every
    failure mode.
  - Row 9 (CONFIRMED): the JP6 disk exhaustion + head-keeping truncation
    defect; the agent's disk-capacity recording and tail-preserving
    derivation contracts are asserted at the text level here.

PURE tests only: no AWS call, SSM send, instance action, deployment,
artifact publication, or build. The dispatcher module is imported with
dummy credentials/table names and a stubbed shared_utils layer; only its
pure functions and generated command TEXT are exercised.
"""
import os
import shlex
import shutil
import sys
import tempfile
import types

import pytest

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3 handles
# and env-derived configuration at import time. Nothing is ever called.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ.setdefault("BUILD_JOBS_TABLE", "dda-portal-build-jobs-preflight")
os.environ.setdefault("BUILD_SERVERS_TABLE",
                      "dda-portal-build-servers-preflight")
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
import build_events  # noqa: E402
import build_reconciliation as br  # noqa: E402
import build_source  # noqa: E402

AGENT_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "portal-build-agent.sh")
PORTAL_BUILD_SCRIPT = os.path.join(_REPO_ROOT, "portal-build.sh")


def _job(target="AMD64", mode="dedicated", **overrides):
    job = {
        "build_job_id": "job-preflight-1",
        "build_target": target,
        "execution_mode": mode,
        "component_name":
            build_domain.BUILD_TARGETS.get(target, {}).get("component_name"),
        "config_snapshot": {"max_runtime_hours": 4},
    }
    job.update(overrides)
    return job


REPO_DIR = "/home/ubuntu/DefectDetectionApplication"


# ===========================================================================
# Task 7.1 — pure local-contract preflight decision (evidence rows 1, 8)
# ===========================================================================

class TestDecidePreflight:
    """`decide_preflight` — the dispatch preflight's pure local-contract
    portion, run BEFORE any SSM send (Req 2.8, 2.10, 2.12)."""

    def test_valid_dedicated_amd64_contract_passes(self):
        decision = build_dispatcher.decide_preflight(
            _job(), REPO_DIR, event_bus="dda-bus", region="us-east-1")
        assert decision.ok
        assert decision.failures == ()
        assert decision.checks["required_arch"] == build_domain.ARCH_X86_64
        assert decision.checks["component_name"] == \
            "aws.edgeml.dda.LocalServer.amd64"
        assert decision.checks["repo_dir"] == REPO_DIR

    def test_unsupported_target_fails_before_costly_work(self):
        decision = build_dispatcher.decide_preflight(
            _job(target="__NOT_A_TARGET__", component_name=None), REPO_DIR,
            event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "unsupported_target" in decision.failures

    def test_component_identity_mismatch_fails(self):
        decision = build_dispatcher.decide_preflight(
            _job(component_name="aws.edgeml.dda.LocalServer.arm64JP5"),
            REPO_DIR, event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "component_identity_mismatch" in decision.failures

    def test_unsupported_execution_mode_fails(self):
        decision = build_dispatcher.decide_preflight(
            _job(mode="serverless"), REPO_DIR,
            event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "unsupported_execution_mode" in decision.failures

    @pytest.mark.parametrize("bad_dir", ["", "   ", "relative/path", None])
    def test_invalid_repository_dir_fails(self, bad_dir):
        decision = build_dispatcher.decide_preflight(
            _job(), bad_dir, event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "repository_dir_invalid" in decision.failures

    def test_missing_callback_bus_and_region_fail(self):
        decision = build_dispatcher.decide_preflight(
            _job(), REPO_DIR, event_bus="", region="")
        assert not decision.ok
        assert "callback_bus_missing" in decision.failures
        assert "callback_region_missing" in decision.failures

    def test_quoting_contract_violation_fails(self):
        job = _job()
        # a non-text ref shape can never round-trip the generated
        # command's shlex quoting contract
        job["config_snapshot"]["source_ref"] = 123
        decision = build_dispatcher.decide_preflight(
            job, REPO_DIR, event_bus="dda-bus", region="us-east-1")
        assert not decision.ok
        assert "quoting_contract_violated:source_ref" in decision.failures

    def test_quoting_round_trips_for_shell_special_values(self):
        for value in ("feature/x y", "a;rm -rf /", "$(evil)", "a'b\"c"):
            assert build_dispatcher.quoting_round_trips(value)
        assert not build_dispatcher.quoting_round_trips(123)

    def test_stable_error_code_is_command_preflight_failed(self):
        assert build_dispatcher.ERROR_COMMAND_PREFLIGHT_FAILED == \
            "COMMAND_PREFLIGHT_FAILED"
        assert build_dispatcher.ERROR_COMMAND_PREFLIGHT_FAILED in \
            br.STABLE_ERROR_CODES


# ===========================================================================
# Task 7.3 — targets and modes preserved through preflight (Req 3.7, 2.9)
# ===========================================================================

class TestTargetMatrixThroughPreflight:
    """JP5/JP6 -> arm64, AMD64/AMD64_NVIDIA -> x86_64 and the current
    component identities survive preflight for BOTH execution modes,
    with no cross-target assumption (Req 2.9, 3.7, 3.9)."""

    EXPECTED = {
        "JP5": ("arm64", "aws.edgeml.dda.LocalServer.arm64JP5"),
        "JP6": ("arm64", "aws.edgeml.dda.LocalServer.arm64JP6"),
        "AMD64": ("x86_64", "aws.edgeml.dda.LocalServer.amd64"),
        "AMD64_NVIDIA": ("x86_64", "aws.edgeml.dda.LocalServer.amd64Nvidia"),
    }

    @pytest.mark.parametrize("target", sorted(EXPECTED))
    @pytest.mark.parametrize("mode", ["dedicated", "ephemeral"])
    def test_every_supported_combination_passes_with_exact_mapping(
            self, target, mode):
        decision = build_dispatcher.decide_preflight(
            _job(target=target, mode=mode), REPO_DIR,
            event_bus="dda-bus", region="us-east-1")
        arch, component = self.EXPECTED[target]
        assert decision.ok, decision.failures
        assert decision.checks["required_arch"] == arch
        assert decision.checks["component_name"] == component

    def test_intentional_validation_remains_in_build_domain(self):
        """Preflight adds no new target/mode combination and removes no
        intentional restriction: the definitions it reads are exactly
        the frozen build_domain table (Req 3.7)."""
        assert set(self.EXPECTED) == set(build_domain.BUILD_TARGETS)
        for target, (arch, component) in self.EXPECTED.items():
            assert build_domain.BUILD_TARGETS[target]["required_arch"] == \
                arch
            assert build_domain.BUILD_TARGETS[target]["component_name"] == \
                component

    def test_agent_preflight_imposes_no_amd64_only_assumption(self):
        """The agent-side preflight checks target-specific architecture
        without imposing AMD64-only tooling on JP5/JP6 or vice versa:
        one arch case per target family, common tools otherwise."""
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            script = handle.read()
        assert "JP5|JP6)" in script
        assert "AMD64|AMD64_NVIDIA)" in script
        assert 'aarch64|arm64' in script
        assert '"$MACHINE_ARCH" = "x86_64"' in script


# ===========================================================================
# Task 7.1 — generated on-server guard + evidenced path correction
# ===========================================================================

class TestPreflightGuardAndPathCorrection:

    def test_guard_precedes_the_agent_invocation_for_no_ref_jobs(self):
        """The incident's own shape (source_ref NULL, evidence row 1):
        the guard sits between the exports and the invocation, which
        stays the last body line — an invalid repository/script contract
        exits with the marker instead of the incident's bare 127."""
        body = build_dispatcher.agent_run_body(_job(), REPO_DIR)
        lines = body.splitlines()
        assert br.PREFLIGHT_FAILURE_MARKER in body
        assert lines[-1].startswith("bash ")
        assert body.index(br.PREFLIGHT_FAILURE_MARKER) < \
            body.index(lines[-1])
        assert f"exit {build_dispatcher.PREFLIGHT_EXIT_CODE}" in body

    def test_guard_runs_after_the_sync_preamble_for_ref_jobs(self):
        """A selected ref legitimately (re)creates the tree and the
        agent script through the Source_Sync preamble, so the guard
        validates the contract AFTER the sync and before the invocation
        (Req 3.9: successful ref dispatches are unchanged)."""
        import unittest.mock as _mock
        job = _job()
        job["config_snapshot"]["source_ref"] = "feature/x"
        with _mock.patch.object(build_dispatcher, "BUILD_REPO_URL",
                                "https://example.invalid/dda.git"):
            body = build_dispatcher.agent_run_body(job, REPO_DIR)
            preamble = build_dispatcher.agent_preamble_commands(
                job, REPO_DIR)
        assert preamble
        last_preamble_at = body.index(preamble[-1])
        guard_at = body.index(br.PREFLIGHT_FAILURE_MARKER)
        assert guard_at > last_preamble_at
        assert guard_at < body.index(body.splitlines()[-1])

    def test_attempt_id_is_injected_without_touching_the_body(self):
        """ATTEMPT_ID travels via the temp-script export injection (task
        7.2): the frozen here-doc body and the agent's KEY=VALUE argument
        contract are byte-identical with or without an attempt, and the
        injection appears only when an attempt exists."""
        job = _job()
        without_attempt = build_dispatcher.agent_command(job, REPO_DIR)
        with_attempt = build_dispatcher.agent_command(
            job, REPO_DIR, attempt_id="2d428fe1-955a-41a4-8c6c-8bb2488d")
        assert "ATTEMPT_ID" not in without_attempt
        inject = build_dispatcher.attempt_env_inject_command(
            "2d428fe1-955a-41a4-8c6c-8bb2488d")
        assert inject in with_attempt.splitlines()
        # the here-doc body is unchanged by the attempt
        assert build_dispatcher.agent_run_body(job, REPO_DIR) in \
            with_attempt
        assert with_attempt.replace(inject + "\n", "") == without_attempt

    def test_guard_checks_repository_dir_and_agent_script(self):
        commands = build_dispatcher.preflight_guard_commands(REPO_DIR)
        text = "\n".join(commands)
        assert "check=repository_dir" in text
        assert "check=agent_script" in text
        assert shlex.quote(REPO_DIR) in text

    def test_guard_never_shadows_the_argument_contract_token(self):
        """No guard token ends with scripts/portal-build-agent.sh: the
        frozen argument-contract oracle keys on the FIRST such token
        being the actual invocation."""
        for token in shlex.split(
                "\n".join(build_dispatcher.preflight_guard_commands(
                    REPO_DIR))):
            assert not token.endswith("scripts/portal-build-agent.sh")

    def test_preflight_marker_classifies_command_preflight_failed(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation={"Status": "Failed", "ResponseCode": 78,
                        "StandardErrorContent":
                            "DDA_PREFLIGHT_FAILED check=agent_script "
                            "path=/x/scripts/portal-build-agent.sh"})
        assert outcome.decided
        assert outcome.error_code == br.CODE_COMMAND_PREFLIGHT_FAILED

    def test_resolver_corrects_the_evidenced_fallback_mismatch(self):
        """Hypothesis row 1 (CONFIRMED): the fallback directory exists
        WITHOUT the agent script while the other evidenced clone root
        carries it -> resolution selects the actual clone. Recorded
        directories and clean fallbacks stay byte-identical (Req 5.3)."""
        root = tempfile.mkdtemp(prefix="dda-preflight-path-")
        try:
            registered = os.path.join(
                root, "home", "ubuntu", "DefectDetectionApplication")
            os.makedirs(os.path.join(registered, "scripts"))
            with open(os.path.join(registered, "scripts",
                                   "portal-build-agent.sh"), "w") as handle:
                handle.write("#!/bin/bash\nexit 0\n")
            legacy_default = os.path.join(
                root, "opt", "dda", "DefectDetectionApplication")
            os.makedirs(legacy_default)

            # legacy server record (no repo_dir) + mismatched fallback
            resolved = build_source.resolve_repo_dir(
                {"build_job_id": "j"}, {"server_id": "s"},
                env_default=legacy_default)
            assert resolved == registered

            # a recorded directory always wins untouched
            assert build_source.resolve_repo_dir(
                None, {"repo_dir": legacy_default},
                env_default=registered) == legacy_default

            # a fallback with no filesystem evidence stays unchanged
            missing = os.path.join(root, "opt", "dda", "absent")
            assert build_source.resolve_repo_dir(
                None, None, env_default=missing) == missing
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ===========================================================================
# Task 7.5 — disk-capacity evidence contracts (Req 2.23, 3.14)
# ===========================================================================

class TestDiskEvidence:

    def test_unmeasured_disk_is_marked_unavailable_not_fabricated(self):
        assert br.sanitize_disk_evidence(None) == {"available": False}
        assert br.sanitize_disk_evidence({}) == {"available": False}
        assert br.sanitize_disk_evidence("not a dict") == \
            {"available": False}

    def test_measured_disk_keeps_only_the_known_fields(self):
        block = br.sanitize_disk_evidence({
            "docker_storage_path": "/var/snap/docker/common",
            "available_gb": 12, "used_gb": 88, "total_gb": 100,
            "measured_at": 1754500000000,
            "unexpected": "dropped",
        })
        assert block == {
            "docker_storage_path": "/var/snap/docker/common",
            "available_gb": 12, "used_gb": 88, "total_gb": 100,
            "measured_at": 1754500000000, "available": True,
        }

    def test_execution_diagnostic_always_carries_a_disk_block(self):
        diagnostic = br.build_execution_diagnostic(
            attempt={"attempt_id": "a-1", "command_id": "cmd-1",
                     "instance_id": "i-1"},
            invocation={"Status": "Failed", "ResponseCode": 1},
            classification=br.CODE_RUNNER_DISK_FULL,
            source="eventbridge", observed_at=0)
        assert diagnostic["disk"] == {"available": False}
        measured = br.build_execution_diagnostic(
            attempt={}, invocation={}, classification=None,
            source="eventbridge", observed_at=0,
            disk={"docker_storage_path": "/var/snap/docker/common",
                  "available_gb": 3})
        assert measured["disk"]["available"] is True
        assert measured["disk"]["available_gb"] == 3

    def test_agent_records_disk_capacity_for_the_docker_storage_path(self):
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            script = handle.read()
        assert '/var/snap/docker/common' in script
        assert 'df -k' in script
        assert '"available":false' in script  # unmeasured marker
        # evidence-only: failing on capacity requires the separately
        # configured minimum
        assert 'PREFLIGHT_MIN_DISK_GB' in script


# ===========================================================================
# Task 7.2 / 7.4 — agent script contract (text-level, like the frozen
# oracle's own assertions; the REAL script is never executed here)
# ===========================================================================

class TestAgentScriptContract:

    @pytest.fixture(scope="class")
    def script(self):
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            return handle.read()

    def test_attempt_id_is_parsed_and_documented(self, script):
        assert "ATTEMPT_ID=*)" in script
        assert "ATTEMPT_ID=<id>" in script

    def test_execution_start_heartbeat_progress_events(self, script):
        assert '"phase":"execution_start"' in script
        assert '"phase":"heartbeat"' in script
        assert '"phase":"progress"' in script
        # monotonic sequences and unique event ids
        assert 'hb_sequence=$((hb_sequence + 1))' in script
        assert 'progress_sequence=$((progress_sequence + 1))' in script
        assert "agent_event_id" in script

    def test_heartbeat_monitor_is_stopped_on_every_exit(self, script):
        assert "trap stop_heartbeat_monitor EXIT" in script

    def test_heartbeats_carry_no_raw_output_or_environment(self, script):
        """The heartbeat/progress printf formats carry identities,
        sequences, timestamps and byte counts only."""
        for line in script.splitlines():
            if '"phase":"heartbeat"' in line or '"phase":"progress"' in line:
                assert "error_message" not in line
                assert "$ERROR_TAIL" not in line
                assert "env" not in line.split("printf")[0]

    def test_terminal_events_carry_completion_time_with_attempt(self, script):
        assert "attempt_terminal_fields" in script
        assert '"completed_at":%s' in script

    def test_error_tail_is_tail_preserving(self, script):
        """Task 7.4 (Req 2.22): the durable error-tail derivation keeps
        the root-cause END of over-length lines — no ERROR_TAIL
        derivation uses the head-keeping cut anymore."""
        assert 'tail -n 5 "$BUILD_LOG" 2>/dev/null | tail -c 512' in script
        for line in script.splitlines():
            if line.strip().startswith("ERROR_TAIL="):
                assert "head -c" not in line, line

    def test_local_enospc_detection_reports_error_kind_disk(self, script):
        assert "no space left on device|enospc" in script
        assert 'BUILD_FAILURE_KIND="disk"' in script
        assert 'emit_failed "$BUILD_FAILURE_KIND"' in script

    def test_preflight_failure_marker_and_exit_code(self, script):
        assert "DDA_PREFLIGHT_FAILED" in script
        assert "exit 78" in script
        assert 'emit_failed "preflight"' in script

    def test_frozen_contracts_are_preserved(self, script):
        """The frozen agent contracts of the preservation baseline: the
        flock mutual exclusion, exit 75 deferral, exit 64 usage, and the
        exact target argument mapping (Req 3.2, 3.7)."""
        assert 'LOCK_FILE="/var/lock/dda-build.lock"' in script
        assert "flock -n 9" in script
        assert "exit 75" in script
        assert "exit 64" in script
        assert "JP5)          BUILD_ARGS=(aarch64 5)" in script
        assert "JP6)          BUILD_ARGS=(aarch64 6)" in script
        assert "AMD64)        BUILD_ARGS=(x86_64)" in script
        assert "AMD64_NVIDIA) BUILD_ARGS=(x86_64_nvidia)" in script

    def test_portal_build_sh_propagates_attempt_identity(self):
        with open(PORTAL_BUILD_SCRIPT, encoding="utf-8") as handle:
            text = handle.read()
        assert 'ATTEMPT_ID' in text
        assert 'attempt_id' in text
        # the agent exports it for portal-build.sh
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            agent = handle.read()
        assert "export BUILD_JOB_ID EVENT_BUS ATTEMPT_ID" in agent


# ===========================================================================
# Task 7.2 / 7.5 — build_events wiring (pure application functions)
# ===========================================================================

class TestFailedPhaseClassificationWiring:
    """apply_phase_event's failed-phase classification (tasks 7.1/7.5
    wiring): ENOSPC evidence -> RUNNER_DISK_FULL, agent error_kind=disk
    honored, error_kind=preflight -> COMMAND_PREFLIGHT_FAILED, disk-free
    failures keep BUILD_FAILED byte-compatibly (Req 2.21, 3.15)."""

    def _apply(self, message, error_kind="building"):
        return build_events.apply_phase_event(
            build_domain.STATUS_BUILDING,
            {"phase": "failed", "error_kind": error_kind,
             "error_message": message},
            1754500000000)

    def test_enospc_message_classifies_runner_disk_full(self):
        application = self._apply(
            "write /var/snap/docker/common/x: no space left on device")
        assert application.updates["error"]["code"] == "RUNNER_DISK_FULL"

    def test_agent_error_kind_disk_is_honored_without_patterns(self):
        application = self._apply("build failed", error_kind="disk")
        assert application.updates["error"]["code"] == "RUNNER_DISK_FULL"

    def test_error_kind_preflight_classifies_preflight_failed(self):
        application = self._apply(
            "Dispatch preflight failed before any build/publish work: "
            "DDA_PREFLIGHT_FAILED tool_gdk", error_kind="preflight")
        assert application.updates["error"]["code"] == \
            "COMMAND_PREFLIGHT_FAILED"

    def test_disk_free_failure_keeps_build_failed(self):
        application = self._apply("compile error: undefined symbol")
        assert application.updates["error"]["code"] == "BUILD_FAILED"

    def test_publishing_failures_are_preserved(self):
        application = build_events.apply_phase_event(
            build_domain.STATUS_PUBLISHING,
            {"phase": "failed", "error_kind": "publishing",
             "error_message": "push failed",
             "published_artifacts": ["image:dda/flask-app"],
             "unpublished_artifacts": ["greengrass_component"]},
            1754500000000)
        assert application.updates["error"]["code"] == "PUBLISHING_FAILED"
        assert application.updates["publish_partial"]["published"] == \
            ["image:dda/flask-app"]
