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
Focused unit tests for the new pure module
``edge-cv-portal/backend/functions/build_reconciliation.py``
(build-fleet-execution-failures tasks 4.1, 4.2, 4.3).

These are the module's own unit tests; the full numbered property
suites arrive in task 10.3. Everything here is pure — no AWS, no I/O,
no moto needed.

Run from the repository root:

    python3 -m pytest \
        test/backend-test/portal_builds/test_build_reconciliation_unit.py \
        --noconftest -q
"""
import json
import os
import sys
from decimal import Decimal

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402
import build_reconciliation as br  # noqa: E402

MS_PER_MINUTE = 60 * 1000
MS_PER_HOUR = 60 * MS_PER_MINUTE
T0 = 1_786_017_773_000


# ===========================================================================
# Task 4.1 — normalization, redaction, byte bounding
# ===========================================================================

class TestNormalizeEvidence:
    def test_scalars_pass_through(self):
        for value in (None, True, 0, 1.5, "text"):
            assert br.normalize_evidence(value) == value

    def test_decimal_becomes_int_or_float(self):
        assert br.normalize_evidence(Decimal("127")) == 127
        assert isinstance(br.normalize_evidence(Decimal("127")), int)
        assert br.normalize_evidence(Decimal("1.5")) == 1.5

    def test_bytes_become_valid_utf8(self):
        assert br.normalize_evidence(b"ok") == "ok"
        # invalid UTF-8 is replaced, never fabricated or dropped whole
        result = br.normalize_evidence(b"a\xff\xfeb")
        assert result.startswith("a") and result.endswith("b")
        result.encode("utf-8")  # must be valid UTF-8

    def test_nested_map_list_preserved(self):
        value = {"a": [Decimal("1"), {"b": (2, 3)}], 4: "x"}
        assert br.normalize_evidence(value) == \
            {"a": [1, {"b": [2, 3]}], "4": "x"}

    def test_json_round_trip(self):
        value = {"list": [1, "two", None], "map": {"k": True}}
        assert json.loads(json.dumps(br.normalize_evidence(value))) == value


class TestRedaction:
    def test_aws_access_key_id(self):
        redacted = br.redact_text("key AKIAIOSFODNN7EXAMPLE used")
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert br.REDACTED in redacted

    def test_secret_assignment_keeps_key_name(self):
        redacted = br.redact_text(
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY")
        assert "wJalrXUtnFEMI" not in redacted
        assert "AWS_SECRET_ACCESS_KEY=" in redacted

    def test_session_token_assignment(self):
        redacted = br.redact_text("export AWS_SESSION_TOKEN=FwoGZXIvYXdzE")
        assert "FwoGZXIvYXdzE" not in redacted

    def test_bearer_and_basic_and_authorization(self):
        text = ("Authorization: Bearer abc.def.ghi\n"
                "auth basic QWxhZGRpbjpvcGVuc2VzYW1l\n"
                "authorization=rawvalue123")
        redacted = br.redact_text(text)
        assert "abc.def.ghi" not in redacted
        assert "QWxhZGRpbjpvcGVuc2VzYW1l" not in redacted
        assert "rawvalue123" not in redacted

    def test_password_and_token_assignments(self):
        redacted = br.redact_text(
            "password=hunter2 GIT_TOKEN: tok_123 secret='sh'")
        for value in ("hunter2", "tok_123", "'sh'"):
            assert value not in redacted

    def test_repository_url_credentials(self):
        redacted = br.redact_text(
            "cloning https://builder:s3cr3tpw@github.com/org/repo.git")
        assert "s3cr3tpw" not in redacted
        assert "github.com/org/repo.git" in redacted

    def test_signed_url_parameters(self):
        text = ("https://bucket.s3.amazonaws.com/o?X-Amz-Credential=AAA"
                "&X-Amz-Signature=beef1234&X-Amz-Security-Token=tok")
        redacted = br.redact_text(text)
        for value in ("=AAA", "=beef1234", "=tok"):
            assert value not in redacted
        assert "X-Amz-Signature=" in redacted  # key names retained

    def test_github_pat_literal(self):
        redacted = br.redact_text("remote: ghp_abcdef0123456789")
        assert "ghp_abcdef0123456789" not in redacted

    def test_configured_organization_patterns(self):
        redacted = br.redact_text(
            "corp-id ORG-0042 in output",
            extra_patterns=[r"ORG-\d{4}"])
        assert "ORG-0042" not in redacted

    def test_bad_org_pattern_fails_closed(self):
        redacted = br.redact_text("value [oops in output",
                                  extra_patterns=["[oops"])
        assert "[oops" not in redacted

    def test_non_secret_context_is_retained(self):
        text = ("bash: /opt/dda/DefectDetectionApplication/scripts/"
                "portal-build-agent.sh: No such file or directory")
        assert br.redact_text(text) == text


class TestBoundText:
    def test_within_limit_unchanged(self):
        bounded = br.bound_text("short", 1024)
        assert bounded == br.BoundedText("short", False, 5)

    def test_over_limit_preserves_head_and_tail_with_marker(self):
        text = "HEADSTART " + ("x" * 5000) + " no space left on device"
        bounded = br.bound_text(text, 512)
        assert bounded.truncated is True
        assert bounded.original_bytes == len(text.encode("utf-8"))
        assert len(bounded.text.encode("utf-8")) <= 512
        # evidence-gate row 9: the trailing root cause MUST survive
        assert bounded.text.endswith("no space left on device")
        assert bounded.text.startswith("HEADSTART")
        assert str(bounded.original_bytes) in bounded.text  # marker

    def test_bounded_output_is_valid_utf8_with_multibyte(self):
        text = "é" * 4000
        bounded = br.bound_text(text, 256)
        bounded.text.encode("utf-8")
        assert len(bounded.text.encode("utf-8")) <= 256

    def test_tail_only_truncation_keeps_root_cause_end(self):
        line = ("#109 ERROR: failed to extract layer sha256: write "
                "/var/snap/docker/common/" + "y" * 600
                + ": no space left on device")
        bounded = br.bound_tail_text(line, 512)
        assert bounded.truncated is True
        assert bounded.text.endswith("no space left on device")
        assert len(bounded.text.encode("utf-8")) <= 512

    def test_tail_truncation_within_bound_unchanged(self):
        # Req 3.15: lines within the bound are recorded unchanged.
        assert br.bound_tail_text("short error", 512).text == "short error"


class TestProviderFields:
    def test_unavailable_field(self):
        assert br.sanitize_provider_field(br.FIELD_UNAVAILABLE) == \
            {"available": False}
        assert br.provider_field({}, "StandardOutputContent") is \
            br.FIELD_UNAVAILABLE

    def test_available_empty_field_is_distinguished(self):
        field = br.sanitize_provider_field("")
        assert field["available"] is True and field["text"] == ""

    def test_present_field_is_redacted_then_bounded(self):
        secret = "AWS_SECRET_ACCESS_KEY=verysecretvalue1234"
        field = br.sanitize_provider_field(secret + "\n" + "z" * 40000)
        assert "verysecretvalue1234" not in field["text"]
        assert len(field["text"].encode("utf-8")) <= \
            br.STDOUT_STDERR_LIMIT_BYTES
        assert field["truncated"] is True

    def test_total_diagnostic_bound(self):
        diagnostic = {
            "schema_version": 1,
            "stdout": {"available": True, "text": "a" * 30000,
                       "truncated": False, "original_bytes": 30000},
            "stderr": {"available": True, "text": "b" * 30000,
                       "truncated": False, "original_bytes": 30000},
            "status_details": "c" * 4000,
        }
        bounded = br.bound_diagnostic_total(diagnostic)
        assert br.diagnostic_json_bytes(bounded) <= \
            br.TOTAL_DIAGNOSTIC_LIMIT_BYTES
        # availability structure survives bounding
        assert bounded["stdout"]["available"] is True
        assert bounded["stdout"]["original_bytes"] == 30000


# ===========================================================================
# Task 4.2 — classification, precedence, merge
# ===========================================================================

def _invocation(status="Failed", response_code=127, stderr=None,
                stdout=None, details="Failed"):
    invocation = {
        "CommandId": "cmd-1",
        "InstanceId": "i-1",
        "Status": status,
        "StatusDetails": details,
        "ResponseCode": response_code,
    }
    if stderr is not None:
        invocation["StandardErrorContent"] = stderr
    if stdout is not None:
        invocation["StandardOutputContent"] = stdout
    return invocation


class TestClassification:
    def test_incident_shape_is_command_execution_failed(self):
        # The exploration tests' expected code for Failed + rc 127.
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(stderr="bash: ...: No such file"))
        assert outcome.decided is True
        assert outcome.status == build_domain.STATUS_FAILED
        assert outcome.error_code == br.CODE_COMMAND_EXECUTION_FAILED

    def test_timed_out_and_cancelled_rows(self):
        timed = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="TimedOut"))
        assert timed.error_code == br.CODE_COMMAND_TIMED_OUT
        assert timed.status == build_domain.STATUS_FAILED
        cancelled = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="Cancelled"))
        assert cancelled.error_code == br.CODE_COMMAND_CANCELLED
        assert cancelled.status == build_domain.STATUS_INTERRUPTED

    def test_send_command_rejected(self):
        outcome = br.classify_attempt(build_domain.STATUS_QUEUED,
                                      send_command_rejected=True)
        assert outcome.error_code == br.CODE_COMMAND_LAUNCH_FAILED

    def test_success_waits_through_settlement_then_result_missing(self):
        deadline = T0 + br.DEFAULT_SETTLEMENT_WINDOW_MS
        waiting = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="Success", response_code=0),
            settlement_deadline_ms=deadline, now=deadline)  # == deadline
        assert waiting.decided is False  # strict boundary: not settled
        settled = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="Success", response_code=0),
            settlement_deadline_ms=deadline, now=deadline + 1)
        assert settled.decided is True
        assert settled.error_code == br.CODE_AGENT_RESULT_MISSING

    def test_valid_agent_result_wins_over_ssm_failure(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(),
            agent_result={"phase": "succeeded", "completed_at": T0})
        assert outcome.status == build_domain.STATUS_SUCCEEDED
        assert outcome.error_code is None
        assert outcome.authority == 1

    def test_agent_result_after_hard_deadline_does_not_qualify(self):
        deadline = T0 + 4 * MS_PER_HOUR
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            agent_result={"phase": "succeeded",
                          "completed_at": deadline + 1},
            hard_deadline_ms=deadline, now=deadline + 2)
        assert outcome.error_code == br.CODE_MAX_RUNTIME_EXCEEDED

    def test_user_cancellation_and_infrastructure_loss(self):
        cancelled = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(),
            user_cancellation_confirmed=True)
        assert cancelled.status == build_domain.STATUS_CANCELLED
        lost = br.classify_attempt(build_domain.STATUS_BUILDING,
                                   infrastructure_lost=True)
        assert lost.status == build_domain.STATUS_INTERRUPTED
        assert lost.error_code == br.CODE_INFRASTRUCTURE_LOST

    def test_no_evidence_stays_undecided(self):
        outcome = br.classify_attempt(build_domain.STATUS_BUILDING)
        assert outcome.decided is False
        assert outcome.status == build_domain.STATUS_BUILDING


class TestEnospcClassification:
    def test_enospc_in_invocation_output_is_runner_disk_full(self):
        # Evidence-gate row 9 (bd91c5d8): trailing ENOSPC text.
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(
                response_code=1,
                stdout="#109 ERROR: failed to extract layer",
                stderr="tee: /tmp/x.log: No space left on device"))
        assert outcome.error_code == br.CODE_RUNNER_DISK_FULL

    def test_enospc_keyword_variant(self):
        assert br.is_disk_exhaustion_evidence("write failed: ENOSPC")

    def test_agent_error_kind_disk_short_circuits(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            agent_result={"phase": "failed", "completed_at": T0,
                          "error_kind": "disk",
                          "message": "gdk exited 1"})
        assert outcome.error_code == br.CODE_RUNNER_DISK_FULL

    def test_without_enospc_evidence_never_runner_disk_full(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(
                stderr="compile error: undefined symbol"))
        assert outcome.error_code != br.CODE_RUNNER_DISK_FULL
        assert not br.is_disk_exhaustion_evidence(
            "plenty of space, all fine")


class TestAttemptCorrelation:
    ATTEMPT = {"attempt_id": "a-1", "command_id": "cmd-1",
               "instance_id": "i-1"}

    def test_matching_evidence_accepted(self):
        assert br.evidence_matches_attempt(
            self.ATTEMPT, {"attempt_id": "a-1", "command_id": "cmd-1"})

    def test_mismatched_evidence_rejected(self):
        for key, value in (("attempt_id", "a-2"),
                           ("command_id", "cmd-2"),
                           ("instance_id", "i-2")):
            assert not br.evidence_matches_attempt(
                self.ATTEMPT, {key: value})

    def test_legacy_evidence_without_ids_not_mismatched(self):
        assert br.evidence_matches_attempt(self.ATTEMPT, {})


class TestInvocationLookup:
    def test_retrieved(self):
        lookup = br.decide_invocation_lookup(
            _invocation(), T0, T0, 5 * MS_PER_MINUTE)
        assert lookup.state == br.LOOKUP_RETRIEVED

    def test_pending_within_window_never_fabricates_failure(self):
        window = 5 * MS_PER_MINUTE
        lookup = br.decide_invocation_lookup(None, T0, T0 + window, window)
        assert lookup.state == br.LOOKUP_PENDING  # == boundary: still in
        assert lookup.retry_after_ms is not None

    def test_unavailable_after_window(self):
        window = 5 * MS_PER_MINUTE
        lookup = br.decide_invocation_lookup(
            None, T0, T0 + window + 1, window)
        assert lookup.state == br.LOOKUP_UNAVAILABLE


class TestDiagnosticMerge:
    def _diag(self, **overrides):
        base = br.build_execution_diagnostic(
            attempt={"attempt_id": "a-1", "command_id": "cmd-1",
                     "instance_id": "i-1"},
            invocation=_invocation(stderr="boom", stdout=""),
            classification=br.CODE_COMMAND_EXECUTION_FAILED,
            source="eventbridge", observed_at=T0)
        base.update(overrides)
        return base

    def test_diagnostic_shape(self):
        diag = self._diag()
        assert diag["schema_version"] == br.DIAGNOSTIC_SCHEMA_VERSION
        assert diag["response_code"] == 127
        assert diag["stderr"] == {"available": True, "text": "boom",
                                  "truncated": False, "original_bytes": 4}
        assert diag["stdout"]["available"] is True
        assert diag["stdout"]["text"] == ""

    def test_merge_is_order_independent(self):
        partial = {"schema_version": 1, "attempt_id": "a-1",
                   "source": ["scheduled_reconciliation"],
                   "stdout": {"available": False},
                   "stderr": {"available": False},
                   "status": None, "observed_at": T0 + 5}
        full = self._diag()
        ab, _ = br.merge_diagnostics(partial, full)
        ba, _ = br.merge_diagnostics(full, partial)
        # field completeness converges regardless of delivery order
        assert ab["stderr"] == ba["stderr"] == full["stderr"]
        assert ab["status"] == ba["status"] == full["status"]
        assert sorted(ab["source"]) == sorted(ba["source"])

    def test_duplicate_merge_is_noop(self):
        diag = self._diag()
        merged, changed = br.merge_diagnostics(diag, dict(diag))
        assert changed is False
        assert merged == diag

    def test_unavailable_never_regresses_available(self):
        diag = self._diag()
        regression = dict(diag)
        regression["stderr"] = {"available": False}
        merged, changed = br.merge_diagnostics(diag, regression)
        assert merged["stderr"]["available"] is True
        assert changed is False

    def test_later_evidence_increases_completeness(self):
        partial = {"schema_version": 1, "stderr": {"available": False},
                   "response_code": None, "source": ["eventbridge"]}
        merged, changed = br.merge_diagnostics(partial, self._diag())
        assert changed is True
        assert merged["response_code"] == 127
        assert merged["stderr"]["text"] == "boom"


class TestTerminalAbsorption:
    def test_terminal_status_only_enriches_diagnostic(self):
        classification = br.classify_attempt(
            build_domain.STATUS_FAILED, invocation=_invocation())
        application = br.apply_evidence(
            build_domain.STATUS_FAILED,
            existing_diagnostic={"schema_version": 1,
                                 "stderr": {"available": False}},
            incoming_diagnostic={"schema_version": 1,
                                 "stderr": {"available": True,
                                            "text": "boom"}},
            classification=classification, now=T0)
        assert application.update_status is None
        assert application.update_ended_at is None
        assert application.update_error_code is None
        assert application.update_diagnostic["stderr"]["text"] == "boom"

    def test_nonterminal_decided_writes_terminal_fields(self):
        classification = br.classify_attempt(
            build_domain.STATUS_BUILDING, invocation=_invocation())
        application = br.apply_evidence(
            build_domain.STATUS_BUILDING, None,
            {"schema_version": 1}, classification, now=T0)
        assert application.update_status == build_domain.STATUS_FAILED
        assert application.update_error_code == \
            br.CODE_COMMAND_EXECUTION_FAILED
        assert application.update_ended_at == T0

    def test_duplicate_evidence_is_full_noop_on_terminal_job(self):
        diag = {"schema_version": 1,
                "stderr": {"available": True, "text": "boom"}}
        classification = br.classify_attempt(
            build_domain.STATUS_FAILED, invocation=_invocation())
        application = br.apply_evidence(
            build_domain.STATUS_FAILED, diag, dict(diag),
            classification, now=T0)
        assert application == br.EvidenceApplication(None, None, None, None)


# ===========================================================================
# Task 4.3 — timing, attempts, terminal effects
# ===========================================================================

BUDGETS = {
    "AMD64": {
        "dedicated": {
            "heartbeat_lease_minutes": 30,
            "progress_stall_minutes": 60,
            "hard_runtime_hours": 8,
        },
    },
}


def _job(status, *, created_at=T0, dispatched_at=None, started_at=None,
         timing=None, budgets=None, max_runtime_hours=4,
         attempt_id="a-1"):
    job = {
        "build_job_id": "job-1",
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
        "status": status,
        "created_at": created_at,
        "config_snapshot": {"max_runtime_hours": max_runtime_hours},
        "execution_attempt": {"attempt_id": attempt_id,
                              "command_id": "cmd-1",
                              "instance_id": "i-1"},
    }
    if budgets is not None:
        job["config_snapshot"]["runtime_budgets"] = budgets
    if dispatched_at is not None:
        job["dispatched_at"] = dispatched_at
    if started_at is not None:
        job["started_at"] = started_at
    if timing is not None:
        job["timing"] = timing
    return job


class TestAttemptIdentity:
    def test_command_comment_round_trip(self):
        comment = br.command_comment("job-1", "a-1")
        assert comment == "dda-build:job-1:a-1"
        assert br.parse_command_comment(comment) == ("job-1", "a-1")

    def test_parse_rejects_non_markers(self):
        for bad in (None, "", "dda-build:only-one", "other:job:attempt",
                    "dda-build::a"):
            assert br.parse_command_comment(bad) is None

    def test_new_execution_attempt_record(self):
        attempt = br.new_execution_attempt("job-1", "a-1", "i-1", T0)
        assert attempt["dispatch_state"] == br.DISPATCH_CLAIMED
        assert attempt["command_comment"] == "dda-build:job-1:a-1"
        assert attempt["command_id"] is None
        assert attempt["claimed_at"] == T0


class TestEffectiveBudget:
    def test_target_mode_override_wins(self):
        budget = br.effective_budget(_job("building", budgets=BUDGETS))
        assert budget.hard_runtime_ms == 8 * MS_PER_HOUR
        assert budget.heartbeat_lease_ms == 30 * MS_PER_MINUTE
        assert budget.progress_stall_ms == 60 * MS_PER_MINUTE
        assert budget.source == "target_mode_override"
        assert budget.queue_wait_ms is None  # optional: disabled
        assert budget.provisioning_ms is None

    def test_target_default_fallback(self):
        budgets = {"AMD64": {"default": {"hard_runtime_hours": 6}}}
        budget = br.effective_budget(_job("building", budgets=budgets))
        assert budget.hard_runtime_ms == 6 * MS_PER_HOUR
        assert budget.source == "target_default"

    def test_legacy_snapshot_max_runtime_hours(self):
        budget = br.effective_budget(_job("building"))
        assert budget.hard_runtime_ms == 4 * MS_PER_HOUR
        assert budget.source == "snapshot_max_runtime_hours"

    def test_compatibility_default(self):
        job = _job("building")
        job["config_snapshot"] = {}
        budget = br.effective_budget(job)
        assert budget.hard_runtime_ms == \
            br.DEFAULT_HARD_RUNTIME_HOURS * MS_PER_HOUR
        assert budget.source == "compatibility_default"

    def test_override_without_hard_hours_keeps_snapshot_ceiling(self):
        budgets = {"AMD64": {"dedicated":
                             {"heartbeat_lease_minutes": 30}}}
        budget = br.effective_budget(_job("building", budgets=budgets))
        assert budget.hard_runtime_ms == 4 * MS_PER_HOUR
        assert budget.source == "snapshot_max_runtime_hours"
        assert budget.heartbeat_lease_ms == 30 * MS_PER_MINUTE


class TestPhaseClocks:
    def test_queue_wait_from_created_at(self):
        job = _job("queued")
        assert br.queue_wait_ms(job, T0 + 6 * MS_PER_HOUR) == \
            6 * MS_PER_HOUR

    def test_provisioning_from_dispatched_at(self):
        job = _job("provisioning", dispatched_at=T0)
        assert br.provisioning_ms(job, T0 + 45 * MS_PER_MINUTE) == \
            45 * MS_PER_MINUTE

    def test_execution_runtime_zero_without_start_evidence(self):
        job = _job("building", started_at=T0)
        assert br.execution_runtime_ms(job, T0 + 10 * MS_PER_HOUR) == 0

    def test_execution_runtime_anchored_on_execution_started_at(self):
        job = _job("building", started_at=T0,
                   timing={"execution_started_at": T0 + 30 * MS_PER_MINUTE})
        assert br.execution_runtime_ms(job, T0 + 90 * MS_PER_MINUTE) == \
            60 * MS_PER_MINUTE


class TestActivityObservations:
    ATTEMPT_EVIDENCE = {"attempt_id": "a-1"}

    def test_heartbeat_renews_liveness_only(self):
        job = _job("building",
                   timing={"execution_started_at": T0,
                           "last_progress_at": T0})
        update = br.observe_heartbeat(job, self.ATTEMPT_EVIDENCE, 1,
                                      T0 + MS_PER_MINUTE)
        assert update.accepted is True
        assert update.timing["last_heartbeat_at"] == T0 + MS_PER_MINUTE
        assert update.timing["last_progress_at"] == T0  # unchanged

    def test_progress_renews_both_leases(self):
        job = _job("building", timing={"execution_started_at": T0})
        update = br.observe_progress(job, self.ATTEMPT_EVIDENCE, 1,
                                     T0 + MS_PER_MINUTE)
        assert update.accepted is True
        assert update.timing["last_progress_at"] == T0 + MS_PER_MINUTE
        assert update.timing["last_heartbeat_at"] == T0 + MS_PER_MINUTE

    def test_non_increasing_sequences_are_noops(self):
        job = _job("building",
                   timing={"execution_started_at": T0,
                           "heartbeat_sequence": 5,
                           "progress_sequence": 5})
        assert not br.observe_heartbeat(
            job, self.ATTEMPT_EVIDENCE, 5, T0 + 1).accepted
        assert not br.observe_progress(
            job, self.ATTEMPT_EVIDENCE, 4, T0 + 1).accepted

    def test_stale_attempt_evidence_rejected(self):
        job = _job("building", timing={"execution_started_at": T0})
        stale = {"attempt_id": "another-attempt"}
        assert not br.observe_heartbeat(job, stale, 99, T0 + 1).accepted
        assert not br.observe_progress(job, stale, 99, T0 + 1).accepted
        assert not br.observe_execution_start(job, stale, T0).accepted

    def test_execution_start_first_writer_wins(self):
        job = _job("building")
        first = br.observe_execution_start(job, self.ATTEMPT_EVIDENCE, T0)
        assert first.accepted is True
        assert first.timing["execution_started_at"] == T0
        job["timing"] = first.timing
        second = br.observe_execution_start(
            job, self.ATTEMPT_EVIDENCE, T0 + 1)
        assert second.accepted is False


class TestDecideTiming:
    def test_fresh_progress_below_hard_ceiling_continues(self):
        now = T0 + 5 * MS_PER_HOUR
        job = _job("building", started_at=T0, budgets=BUDGETS,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - MS_PER_MINUTE,
                           "last_progress_at": now - 2 * MS_PER_MINUTE})
        decision = br.decide_timing(job, now)
        assert decision.timed_out is False
        assert decision.classification == br.TIMEOUT_CONTINUE

    def test_progress_stall_classified(self):
        now = T0 + 3 * MS_PER_HOUR
        job = _job("building", started_at=T0, budgets=BUDGETS,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - MS_PER_MINUTE,
                           "last_progress_at": now - 2 * MS_PER_HOUR})
        decision = br.decide_timing(job, now)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_BUILD_PROGRESS_STALLED
        assert decision.evidence["last_progress_at"] == \
            now - 2 * MS_PER_HOUR

    def test_stale_heartbeat_classified(self):
        now = T0 + 3 * MS_PER_HOUR
        job = _job("building", started_at=T0, budgets=BUDGETS,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - 2 * MS_PER_HOUR,
                           "last_progress_at": now - 2 * MS_PER_HOUR})
        decision = br.decide_timing(job, now)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_AGENT_HEARTBEAT_EXPIRED

    def test_queued_job_never_fails_and_exposes_queue_evidence(self):
        now = T0 + 6 * MS_PER_HOUR
        job = _job("queued", budgets=BUDGETS)
        decision = br.decide_timing(job, now)
        assert decision.timed_out is False
        assert decision.evidence["phase"] == "queue_wait"
        assert decision.evidence["queue_wait_ms"] == 6 * MS_PER_HOUR
        assert decision.evidence["execution_runtime_ms"] == 0

    def test_explicit_queue_budget_expires_strictly(self):
        budgets = {"AMD64": {"dedicated": {"queue_wait_hours": 2,
                                           "hard_runtime_hours": 8}}}
        job = _job("queued", budgets=budgets)
        deadline = T0 + 2 * MS_PER_HOUR
        assert not br.decide_timing(job, deadline).timed_out
        decision = br.decide_timing(job, deadline + 1)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_QUEUE_WAIT_TIMEOUT

    def test_provisioning_isolated_and_optional_budget(self):
        now = T0 + 45 * MS_PER_MINUTE
        job = _job("provisioning", dispatched_at=T0, budgets=BUDGETS)
        decision = br.decide_timing(job, now)
        assert decision.timed_out is False
        assert decision.evidence["phase"] == "provisioning"
        assert decision.evidence["provisioning_ms"] == 45 * MS_PER_MINUTE
        assert decision.evidence["execution_runtime_ms"] == 0

    def test_wait_for_execution_evidence(self):
        job = _job("building", started_at=T0)
        decision = br.decide_timing(job, T0 + 100 * MS_PER_HOUR)
        assert decision.timed_out is False
        assert decision.classification == \
            br.TIMEOUT_WAIT_FOR_EXECUTION_EVIDENCE

    def test_hard_ceiling_strict_boundary(self):
        deadline = T0 + 4 * MS_PER_HOUR
        job = _job("building", started_at=T0,
                   timing={"execution_started_at": T0})
        assert not br.decide_timing(job, deadline).timed_out
        decision = br.decide_timing(job, deadline + 1)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_MAX_RUNTIME_EXCEEDED

    def test_hard_ceiling_not_extendable_by_fresh_activity(self):
        now = T0 + 8 * MS_PER_HOUR + 1
        job = _job("building", started_at=T0, budgets=BUDGETS,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": now - 1,
                           "last_progress_at": now - 1})
        decision = br.decide_timing(job, now)
        assert decision.timed_out is True
        assert decision.classification == br.CODE_MAX_RUNTIME_EXCEEDED

    def test_hard_ceiling_evidence_is_rich(self):
        now = T0 + 4 * MS_PER_HOUR + 1
        job = _job("building", started_at=T0,
                   timing={"execution_started_at": T0,
                           "last_heartbeat_at": T0 + 3 * MS_PER_HOUR,
                           "last_progress_at": T0 + 3 * MS_PER_HOUR})
        decision = br.decide_timing(job, now)
        evidence = decision.evidence
        assert evidence["phase"] == "execution"
        assert evidence["observed_ms"] == 4 * MS_PER_HOUR + 1
        assert evidence["budget_source"] == "snapshot_max_runtime_hours"
        assert evidence["last_heartbeat_at"] == T0 + 3 * MS_PER_HOUR
        assert evidence["build_target"] == build_domain.TARGET_AMD64
        assert evidence["execution_mode"] == \
            build_domain.EXECUTION_MODE_DEDICATED
        record = br.timeout_evidence_record(decision, now)
        assert record["timeout_kind"] == br.CODE_MAX_RUNTIME_EXCEEDED
        assert record["timeout_decided_at"] == now

    def test_execution_anchor_is_positive_evidence_not_started_at(self):
        # The exploration f-anchor case: exec start 30min after
        # started_at; at started_at + 4h + 1ms only 3h30m have run.
        execution_started = T0 + 30 * MS_PER_MINUTE
        now = T0 + 4 * MS_PER_HOUR + 1
        job = _job("building", started_at=T0,
                   timing={"execution_started_at": execution_started,
                           "last_heartbeat_at": now - MS_PER_MINUTE,
                           "last_progress_at": now - MS_PER_MINUTE})
        assert br.decide_timing(job, now).timed_out is False


class TestTerminalEffectsLedger:
    def test_stable_effect_id(self):
        assert br.terminal_effect_id("job-1", "a-1") == \
            "job-1:a-1:terminal"

    def test_dedicated_plan(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "dedicated")
        assert ledger["effect_id"] == "job-1:a-1:terminal"
        assert ledger[br.EFFECT_ALLOCATION_RELEASE] == br.EFFECT_PENDING
        assert ledger[br.EFFECT_COMPUTE_CLEANUP] == br.EFFECT_PENDING

    def test_ephemeral_plan_has_no_allocation_release(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "ephemeral")
        assert ledger[br.EFFECT_ALLOCATION_RELEASE] == \
            br.EFFECT_NOT_APPLICABLE

    def test_release_requires_verified_cleanup_first(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "dedicated")
        refused = br.advance_effect(ledger, br.EFFECT_ALLOCATION_RELEASE)
        assert refused.allowed is False
        assert refused.ledger == ledger  # unchanged

    def test_promotion_requires_release_first(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "dedicated")
        refused = br.advance_effect(ledger, br.EFFECT_PROMOTION_WAKEUP)
        assert refused.allowed is False

    def test_ordered_completion_and_duplicate_refusal(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "dedicated")
        for effect in (br.EFFECT_AUDIT, br.EFFECT_COMPUTE_CLEANUP,
                       br.EFFECT_ALLOCATION_RELEASE,
                       br.EFFECT_PROMOTION_WAKEUP):
            advanced = br.advance_effect(ledger, effect)
            assert advanced.allowed is True, effect
            ledger = advanced.ledger
        assert br.pending_effects(ledger) == []
        duplicate = br.advance_effect(ledger, br.EFFECT_AUDIT)
        assert duplicate.allowed is False  # one logical audit only

    def test_ephemeral_promotion_after_cleanup(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "ephemeral")
        ledger = br.advance_effect(ledger, br.EFFECT_COMPUTE_CLEANUP).ledger
        promoted = br.advance_effect(ledger, br.EFFECT_PROMOTION_WAKEUP)
        assert promoted.allowed is True

    def test_pending_effects_order(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "dedicated")
        assert br.pending_effects(ledger) == [
            br.EFFECT_AUDIT, br.EFFECT_COMPUTE_CLEANUP,
            br.EFFECT_ALLOCATION_RELEASE, br.EFFECT_PROMOTION_WAKEUP]

    def test_cleanup_not_required(self):
        ledger = br.plan_terminal_effects("job-1", "a-1", "dedicated",
                                          cleanup_required=False)
        assert ledger[br.EFFECT_COMPUTE_CLEANUP] == \
            br.EFFECT_NOT_APPLICABLE
        released = br.advance_effect(ledger, br.EFFECT_ALLOCATION_RELEASE)
        assert released.allowed is True


class TestModulePurity:
    def test_no_boto3_or_io_imports(self):
        import build_reconciliation
        source = open(build_reconciliation.__file__).read()
        for forbidden in ("import boto3", "from boto3", "botocore",
                          "import os", "import socket", "urllib"):
            assert forbidden not in source, forbidden


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
