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
# Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
"""
Build agent exit-75 deferral — PRESERVATION baseline
(build-agent-exit75-deferral, task 2).

**Property 2: Preservation** — non-bug-condition behavior preserved.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9,
3.10**

Observation-first: every oracle in this file was RECORDED from the
CURRENT (unfixed) working tree. These tests MUST PASS now and MUST keep
passing verbatim after the exit-75 deferral fix lands. This oracle is
IMMUTABLE: it is never rebaselined after implementation.

The input domain of this file is the complement of bugfix.md's
``isBugCondition(X)`` (terminal 'Failed' invocation, ResponseCode 75, no
agent result, no preflight/ENOSPC evidence) and of the Defect-B
double-send exposure. Nothing here exercises an rc-75 'Failed'
invocation without a higher-authority input — that is the exploration
file's domain (task 1) and MAY change with the fix.

Pinned per bugfix.md's preservation pseudocode
(``classify_attempt(X) = classify_attempt'(X)`` on decided / status /
error_code / authority):

* **3.1** — terminal 'Failed' invocations with ResponseCode != 75 and no
  preflight/ENOSPC evidence return exactly
  ``(decided=True, STATUS_FAILED, CODE_COMMAND_EXECUTION_FAILED,
  authority=5)``, including when the stdout carries the agent's
  lock-held marker text (the fix keys on the response code, never the
  text alone).
* **3.2 / 3.3** — TimedOut / Cancelled / preflight-evidence /
  ENOSPC-evidence rows keep their exact codes and authorities for EVERY
  response code including 75; agent results keep authority 1, user
  cancellation authority 2, the hard ceiling authority 3, and
  infrastructure loss authority 4 even when the invocation also carries
  rc 75; SendCommand rejection, Success settlement (authority 6, strict
  ``now > deadline`` boundary), and evidence-unavailable (authority 7)
  keep their exact tuples.
* **3.5** — ``command_comment``/``parse_command_comment`` round-trip;
  ``claim_resend`` conditional one-writer-wins semantics (real
  conditional writes against moto DynamoDB: success AND
  ConditionalCheckFailedException directions);
  ``recover_ambiguous_send`` still attaches the CURRENT attempt's found
  command WITHOUT a resend, and never resends inside the visibility
  bound.
* **3.4** — the ``PREDISPATCH_DEFER`` requeue shape from
  ``build_planner.decide_predispatch`` (status queued, ORIGINAL
  created_at retained, deferred_at = verification time) and the
  5-minute ``is_reverification_due`` interval.
* **3.6** — the termination watchdog still terminates a GENUINELY
  terminal ephemeral job's runner (and only that: nonterminal,
  dedicated, and already-terminated jobs are untouched).
* **3.8** — ``lock_ownership_heal_command`` content untouched (the job
  01b18948 root-owned-lock heal, a DIFFERENT exit-75 bug).
* **3.9** — every existing STABLE_ERROR_CODES member keeps its
  membership and literal value.

SAFETY: no test here launches EC2 compute, sends a real SSM command, or
calls real AWS. Every AWS interaction runs against moto (`mock_aws`
started at module scope before any handler import) or a recording fake
patched over the module client.

Run from the repository root (the ci profile is registered below
because ``--noconftest`` skips the conftest registration):

    HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test \
        ~/.dda-test-venv/bin/python -m pytest \
        test/backend-test/portal_builds/test_exit75_deferral_preservation.py \
        --noconftest -q -p no:cacheprovider
"""
import json
import os
import sys
import types
import uuid
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Hypothesis profile: --noconftest skips the conftest registration, so
# the suite registers/loads the ci profile itself (>= 100 examples).
# ---------------------------------------------------------------------------
settings.register_profile(
    "ci", max_examples=100, deadline=None,
    suppress_health_check=[HealthCheck.too_slow,
                           HealthCheck.filter_too_much])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handler binds its boto3
# resources/clients and table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "exit75-preserve"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports bz2 (sibling shim in
# test_dispatcher_command_reconciliation.py).
try:
    import bz2  # noqa: F401
except ImportError:  # pragma: no cover - depends on the runner's build
    _bz2_stub = types.ModuleType("_bz2")

    class _Bz2Unavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bz2 is unavailable in this environment")

    _bz2_stub.BZ2Compressor = _Bz2Unavailable
    _bz2_stub.BZ2Decompressor = _Bz2Unavailable
    sys.modules["_bz2"] = _bz2_stub

from moto import mock_aws  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    module.log_audit_event = log_audit_event
    return module


for _module in ("build_dispatcher", "build_planner", "build_domain",
                "build_reconciliation", "build_source", "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name, _key in ((_JOBS_TABLE, "build_job_id"),
                    (_SERVERS_TABLE, "server_id")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key,
                               "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_reconciliation as br  # noqa: E402
import build_dispatcher  # noqa: E402

T0 = 1_786_017_773_000
MS_PER_MINUTE = 60 * 1000
MS_PER_HOUR = 60 * MS_PER_MINUTE

#: The agent's deterministic lock-held stdout marker (incident command
#: a13a0825). Text alone must NEVER divert a non-75 classification.
LOCK_HELD_MARKER = ("Build lock /var/lock/dda-build.lock is held by "
                    "another build — deferring (exit 75).")


def _observation(**fields):
    return json.dumps(fields, indent=2, default=str)


def _invocation(status="Failed", response_code=127, stdout="",
                stderr="", details=None):
    return {
        "CommandId": "cmd-1",
        "InstanceId": "i-1",
        "Status": status,
        "StatusDetails": details if details is not None else status,
        "ResponseCode": response_code,
        "StandardOutputContent": stdout,
        "StandardErrorContent": stderr,
    }


def _tuple4(classification):
    """The preserved projection per bugfix.md's preservation pseudocode:
    decided, status, error_code, authority."""
    return (classification.decided, classification.status,
            classification.error_code, classification.authority)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _clean(text):
    """Non-preflight, non-ENOSPC text — judged by the module's own
    evidence predicates so the domain is exactly the production one."""
    return (not br.is_preflight_failure_evidence(text)
            and not br.is_disk_exhaustion_evidence(text))


clean_text = st.text(max_size=200).filter(_clean)

#: Broad response-code set minus 75 (bugfix.md 3.1): common shell/SSM
#: codes plus arbitrary integers, never the deferral code.
non75_codes = st.one_of(
    st.sampled_from([-1, 0, 1, 2, 64, 74, 76, 100, 126, 127, 137, 255]),
    st.integers(min_value=-256, max_value=256),
).filter(lambda code: code != 75)

#: Any response code INCLUDING 75 — used where a different invocation
#: status or a higher authority makes the input non-bug-condition.
any_codes = st.integers(min_value=-256, max_value=256)

id_chars = "abcdef0123456789-"
identifier = st.text(alphabet=id_chars, min_size=1, max_size=36).filter(
    lambda s: ":" not in s)


# ===========================================================================
# bugfix.md 3.1 — non-75 terminal 'Failed' fallthrough is byte-identical
# ===========================================================================

class TestNon75FailedFallthroughPreserved:
    """**Property 2: Preservation** — recorded from the unfixed
    `classify_attempt`: a terminal 'Failed' invocation with any
    ResponseCode other than 75 and no preflight/ENOSPC evidence is a
    decided hard failure at authority 5.

    **Validates: Requirements 3.1, 3.9**
    """

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=non75_codes, stdout=clean_text,
           stderr=clean_text, details=clean_text)
    def test_non75_failed_is_command_execution_failed(
            self, response_code, stdout, stderr, details):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=response_code,
                                   stdout=stdout, stderr=stderr,
                                   details=details))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_COMMAND_EXECUTION_FAILED, 5), _observation(
            response_code=response_code, stdout=stdout, stderr=stderr,
            details=details, outcome=outcome._asdict())

    def test_rc_minus_one_undeliverable_shape(self):
        """The incident's first command (1b538196) shape: SSM 'Failed',
        rc -1, no execution evidence — a decided hard failure today."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=-1,
                                   details="Undeliverable"))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_COMMAND_EXECUTION_FAILED, 5)

    def test_lock_marker_text_with_non75_code_still_hard_fails(self):
        """bugfix.md 3.1 pins the NON-75 domain on the response code
        alone: the lock-held stdout marker with rc 74 is (and stays) a
        genuine COMMAND_EXECUTION_FAILED — the deferral evidence is the
        response code, never the text by itself."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=74,
                                   stdout=LOCK_HELD_MARKER))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_COMMAND_EXECUTION_FAILED, 5)


# ===========================================================================
# bugfix.md 3.2 — authority-5 sibling rows keep their exact codes for
# EVERY response code (75 included: their status/evidence, not the code,
# routes them outside the bug condition)
# ===========================================================================

class TestAuthorityFiveSiblingRowsPreserved:
    """**Property 2: Preservation** — TimedOut / Cancelled / preflight /
    ENOSPC rows recorded from the unfixed tree.

    **Validates: Requirements 3.2, 3.9**
    """

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=any_codes, stdout=clean_text, stderr=clean_text)
    def test_timed_out_row(self, response_code, stdout, stderr):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="TimedOut",
                                   response_code=response_code,
                                   stdout=stdout, stderr=stderr))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_COMMAND_TIMED_OUT, 5)

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=any_codes, stdout=clean_text, stderr=clean_text)
    def test_cancelled_row(self, response_code, stdout, stderr):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="Cancelled",
                                   response_code=response_code,
                                   stdout=stdout, stderr=stderr))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_INTERRUPTED,
            br.CODE_COMMAND_CANCELLED, 5)

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=any_codes, prefix=clean_text, suffix=clean_text)
    def test_preflight_evidence_row_wins_for_every_code(
            self, response_code, prefix, suffix):
        """Preflight evidence (checked BEFORE any response-code
        handling) keeps COMMAND_PREFLIGHT_FAILED even at rc 75."""
        stderr = prefix + br.PREFLIGHT_FAILURE_MARKER + suffix
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=response_code,
                                   stderr=stderr))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_COMMAND_PREFLIGHT_FAILED, 5)

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=any_codes,
           prefix=clean_text.filter(
               lambda t: not br.is_preflight_failure_evidence(t)),
           enospc=st.sampled_from(["No space left on device", "ENOSPC",
                                   "enospc", "write error: no space "
                                   "left on device"]))
    def test_enospc_evidence_row_wins_for_every_code(
            self, response_code, prefix, enospc):
        """ENOSPC evidence (checked BEFORE any response-code handling)
        keeps RUNNER_DISK_FULL even at rc 75."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=response_code,
                                   stderr=prefix + enospc))
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_RUNNER_DISK_FULL, 5)

    def test_send_command_rejected_row(self):
        outcome = br.classify_attempt(build_domain.STATUS_QUEUED,
                                      send_command_rejected=True)
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_COMMAND_LAUNCH_FAILED, 5)


# ===========================================================================
# bugfix.md 3.2 / 3.3 — authorities 1-4 keep winning over ANY invocation
# evidence, rc 75 included
# ===========================================================================

class TestHigherAuthoritiesPreserved:
    """**Property 2: Preservation** — the precedence table's rows 1-4
    recorded from the unfixed tree, each asserted against an invocation
    that ALSO carries a terminal 'Failed' rc-75 shape.

    **Validates: Requirements 3.2, 3.3, 3.9**
    """

    RC75_INVOCATION = dict(
        CommandId="cmd-1", InstanceId="i-1", Status="Failed",
        StatusDetails="Failed", ResponseCode=75,
        StandardOutputContent=LOCK_HELD_MARKER, StandardErrorContent="")

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=any_codes)
    def test_agent_succeeded_wins_authority_1(self, response_code):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=response_code,
                                   stdout=LOCK_HELD_MARKER),
            agent_result={"phase": "succeeded", "completed_at": T0})
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_SUCCEEDED, None, 1)

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(response_code=any_codes,
           message=clean_text)
    def test_agent_failed_wins_authority_1(self, response_code, message):
        """An agent-reported real failure on a job whose invocation also
        shows rc 75 is still agent-authoritative (bugfix.md 3.3)."""
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(response_code=response_code,
                                   stdout=LOCK_HELD_MARKER),
            agent_result={"phase": "failed", "completed_at": T0,
                          "message": message})
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED, None, 1)

    def test_agent_disk_failure_wins_authority_1_with_rc75(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=dict(self.RC75_INVOCATION),
            agent_result={"phase": "failed", "completed_at": T0,
                          "error_kind": "disk", "message": "gdk exited"})
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_RUNNER_DISK_FULL, 1)

    def test_user_cancellation_wins_authority_2_with_rc75(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=dict(self.RC75_INVOCATION),
            user_cancellation_confirmed=True)
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_CANCELLED, None, 2)

    def test_hard_ceiling_wins_authority_3_with_rc75(self):
        deadline = T0 + 4 * MS_PER_HOUR
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=dict(self.RC75_INVOCATION),
            hard_deadline_ms=deadline, now=deadline + 1)
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_MAX_RUNTIME_EXCEEDED, 3)

    def test_infrastructure_loss_wins_authority_4_with_rc75(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=dict(self.RC75_INVOCATION),
            infrastructure_lost=True)
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_INTERRUPTED,
            br.CODE_INFRASTRUCTURE_LOST, 4)


# ===========================================================================
# bugfix.md 3.2 — Success settlement (authority 6) and evidence
# unavailable (authority 7)
# ===========================================================================

class TestSettlementAndUnavailablePreserved:
    """**Property 2: Preservation** — Success-settlement and
    evidence-unavailable branches recorded from the unfixed tree.

    **Validates: Requirements 3.2, 3.10**
    """

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(early=st.integers(min_value=0,
                             max_value=br.DEFAULT_SETTLEMENT_WINDOW_MS))
    def test_success_within_settlement_window_stays_undecided(self, early):
        deadline = T0 + br.DEFAULT_SETTLEMENT_WINDOW_MS
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="Success", response_code=0),
            settlement_deadline_ms=deadline, now=deadline - early)
        assert _tuple4(outcome) == (
            False, build_domain.STATUS_BUILDING, None, 6)

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(late=st.integers(min_value=1, max_value=MS_PER_HOUR))
    def test_success_past_settlement_is_agent_result_missing(self, late):
        deadline = T0 + br.DEFAULT_SETTLEMENT_WINDOW_MS
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="Success", response_code=0),
            settlement_deadline_ms=deadline, now=deadline + late)
        assert _tuple4(outcome) == (
            True, build_domain.STATUS_FAILED,
            br.CODE_AGENT_RESULT_MISSING, 6)

    def test_no_evidence_is_undecided_authority_7(self):
        for status in (build_domain.STATUS_BUILDING,
                       build_domain.STATUS_PUBLISHING):
            outcome = br.classify_attempt(status)
            assert _tuple4(outcome) == (False, status, None, 7)

    def test_nonterminal_invocation_is_undecided_authority_7(self):
        outcome = br.classify_attempt(
            build_domain.STATUS_BUILDING,
            invocation=_invocation(status="InProgress", response_code=0))
        assert _tuple4(outcome) == (
            False, build_domain.STATUS_BUILDING, None, 7)


# ===========================================================================
# bugfix.md 3.9 — every existing stable error code keeps its literal
# value and STABLE_ERROR_CODES membership
# ===========================================================================

class TestStableErrorCodesPreserved:
    """**Validates: Requirements 3.9**"""

    RECORDED_CODES = {
        "CODE_COMMAND_LAUNCH_FAILED": "COMMAND_LAUNCH_FAILED",
        "CODE_COMMAND_EXECUTION_FAILED": "COMMAND_EXECUTION_FAILED",
        "CODE_COMMAND_TIMED_OUT": "COMMAND_TIMED_OUT",
        "CODE_COMMAND_CANCELLED": "COMMAND_CANCELLED",
        "CODE_AGENT_RESULT_MISSING": "AGENT_RESULT_MISSING",
        "CODE_INFRASTRUCTURE_LOST": "INFRASTRUCTURE_LOST",
        "CODE_AGENT_HEARTBEAT_EXPIRED": "AGENT_HEARTBEAT_EXPIRED",
        "CODE_BUILD_PROGRESS_STALLED": "BUILD_PROGRESS_STALLED",
        "CODE_PROVISIONING_TIMEOUT": "PROVISIONING_TIMEOUT",
        "CODE_QUEUE_WAIT_TIMEOUT": "QUEUE_WAIT_TIMEOUT",
        "CODE_MAX_RUNTIME_EXCEEDED": "MAX_RUNTIME_EXCEEDED",
        "CODE_RUNNER_DISK_FULL": "RUNNER_DISK_FULL",
        "CODE_COMMAND_PREFLIGHT_FAILED": "COMMAND_PREFLIGHT_FAILED",
    }

    def test_existing_codes_keep_value_and_membership(self):
        """New deferral outcomes may only be ADDITIVE: every recorded
        code keeps its literal value and stays in STABLE_ERROR_CODES."""
        for name, value in self.RECORDED_CODES.items():
            assert getattr(br, name) == value
            assert value in br.STABLE_ERROR_CODES


# ===========================================================================
# bugfix.md 3.5 — attempt identity: comment round-trip
# ===========================================================================

class TestCommandCommentPreserved:
    """**Property 2: Preservation** — the deterministic command comment
    and its inverse, recorded from the unfixed tree.

    **Validates: Requirements 3.5**
    """

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(job_id=identifier, attempt_id=identifier)
    def test_round_trip(self, job_id, attempt_id):
        comment = br.command_comment(job_id, attempt_id)
        assert comment == f"dda-build:{job_id}:{attempt_id}"
        assert br.parse_command_comment(comment) == (job_id, attempt_id)

    def test_incident_identities_round_trip(self):
        job = "851042a7-434f-4f8a-9fd4-79b25d100150"
        for attempt in ("88dcd6b8", "6c6b5483"):
            assert br.parse_command_comment(
                br.command_comment(job, attempt)) == (job, attempt)

    def test_parse_rejects_non_markers(self):
        for bad in (None, "", 42, "dda-build:only-one",
                    "other:job:attempt", "dda-build::a", "dda-build:a:",
                    "dda-build:a:b:c"):
            assert br.parse_command_comment(bad) is None


# ===========================================================================
# bugfix.md 3.5 — claim_resend one-writer-wins (real conditional writes
# against moto: success AND ConditionalCheckFailedException directions)
# ===========================================================================

def _seed_attempt_job(job_id, attempt_id="att-1",
                      state=br.DISPATCH_SENDING, sending_at=T0):
    _JOBS.put_item(Item={
        "build_job_id": job_id,
        "execution_attempt": {
            "attempt_id": attempt_id,
            "dispatch_state": state,
            "sending_at": sending_at,
        },
    })


def _stored_sending_at(job_id):
    item = _JOBS.get_item(Key={"build_job_id": job_id}).get("Item")
    return build_dispatcher.to_native(
        item["execution_attempt"]["sending_at"])


class TestClaimResendPreserved:
    """**Property 2: Preservation** — `claim_resend`'s conditional
    one-writer-wins semantics, recorded from the unfixed tree.

    **Validates: Requirements 3.5**
    """

    def test_matching_claim_wins_and_advances_sending_at(self):
        job_id = f"claim-win-{uuid.uuid4()}"
        _seed_attempt_job(job_id, sending_at=T0)
        assert build_dispatcher.claim_resend(
            job_id, "att-1", T0, T0 + 1000) is True
        assert _stored_sending_at(job_id) == T0 + 1000

    def test_second_claimant_with_stale_sending_at_loses(self):
        """After one winner advanced sending_at, every concurrent or
        retried claimant holding the OLD sending_at fails the condition
        (ConditionalCheckFailedException -> False, no write)."""
        job_id = f"claim-race-{uuid.uuid4()}"
        _seed_attempt_job(job_id, sending_at=T0)
        assert build_dispatcher.claim_resend(
            job_id, "att-1", T0, T0 + 1000) is True
        assert build_dispatcher.claim_resend(
            job_id, "att-1", T0, T0 + 2000) is False
        assert _stored_sending_at(job_id) == T0 + 1000

    def test_wrong_attempt_identity_loses(self):
        job_id = f"claim-attempt-{uuid.uuid4()}"
        _seed_attempt_job(job_id, attempt_id="att-1", sending_at=T0)
        assert build_dispatcher.claim_resend(
            job_id, "att-other", T0, T0 + 1000) is False
        assert _stored_sending_at(job_id) == T0

    def test_settled_attempt_state_loses(self):
        job_id = f"claim-settled-{uuid.uuid4()}"
        _seed_attempt_job(job_id, state=br.DISPATCH_SENT, sending_at=T0)
        assert build_dispatcher.claim_resend(
            job_id, "att-1", T0, T0 + 1000) is False
        assert _stored_sending_at(job_id) == T0


# ===========================================================================
# bugfix.md 3.5 — recover_ambiguous_send: the CURRENT attempt's found
# command is attached WITHOUT a resend; inside the visibility bound
# nothing is ever resent
# ===========================================================================

class _FakeSsmListCommands:
    def __init__(self, commands):
        self.commands = commands
        self.list_calls = []

    def list_commands(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        return {"Commands": [dict(c) for c in self.commands]}


def _seed_sending_job(job_id, instance_id, sending_at):
    attempt_id = str(uuid.uuid4())
    comment = br.command_comment(job_id, attempt_id)
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": build_domain.STATUS_BUILDING,
        "requested_by": "operator-1",
        "created_at": T0 - 10 * MS_PER_MINUTE,
        "config_snapshot": {"max_runtime_hours": 4},
        "runner": {"instance_id": instance_id},
        "execution_attempt": {
            "attempt_id": attempt_id,
            "dispatch_state": br.DISPATCH_SENDING,
            "instance_id": instance_id,
            "command_id": None,
            "command_comment": comment,
            "claimed_at": sending_at,
            "sending_at": sending_at,
            "sent_at": None,
        },
    }
    _JOBS.put_item(Item=job)
    return job, comment


def _forbid_send(*args, **kwargs):  # pragma: no cover - failure path
    raise AssertionError(
        "send_agent must NOT be called on this preserved path")


class TestRecoverAmbiguousSendPreserved:
    """**Property 2: Preservation** — recorded from the unfixed
    `recover_ambiguous_send`.

    **Validates: Requirements 3.5**
    """

    def test_current_attempt_command_attached_without_resend(self):
        job_id = f"recover-attach-{uuid.uuid4()}"
        job, comment = _seed_sending_job(job_id, "i-attach",
                                         T0 - 10 * MS_PER_MINUTE)
        fake_ssm = _FakeSsmListCommands(
            [{"CommandId": "cmd-elsewhere", "Comment": "other:j:a"},
             {"CommandId": "cmd-found", "Comment": comment}])
        with mock.patch.object(build_dispatcher, "ssm", fake_ssm), \
                mock.patch.object(build_dispatcher, "send_agent",
                                  _forbid_send):
            build_dispatcher.recover_ambiguous_send(job, {}, now=T0)

        attempt = job["execution_attempt"]
        assert attempt["command_id"] == "cmd-found"
        assert attempt["dispatch_state"] == br.DISPATCH_SENT
        assert job["ssm"]["command_id"] == "cmd-found"
        stored = build_dispatcher.to_native(
            _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))
        assert stored["execution_attempt"]["command_id"] == "cmd-found"
        assert stored["execution_attempt"]["dispatch_state"] == \
            br.DISPATCH_SENT
        assert stored["ssm"]["command_id"] == "cmd-found"
        assert stored["log"]["stream"] == build_dispatcher.ssm_log_stream(
            "cmd-found", "i-attach")

    def test_inside_visibility_bound_never_resends(self):
        job_id = f"recover-wait-{uuid.uuid4()}"
        job, _ = _seed_sending_job(job_id, "i-wait", T0 - MS_PER_MINUTE)
        fake_ssm = _FakeSsmListCommands([])
        with mock.patch.object(build_dispatcher, "ssm", fake_ssm), \
                mock.patch.object(build_dispatcher, "send_agent",
                                  _forbid_send):
            build_dispatcher.recover_ambiguous_send(job, {}, now=T0)

        assert job["execution_attempt"]["dispatch_state"] == \
            br.DISPATCH_SENDING
        assert job["execution_attempt"]["command_id"] is None
        stored = build_dispatcher.to_native(
            _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))
        assert stored["execution_attempt"]["dispatch_state"] == \
            br.DISPATCH_SENDING


# ===========================================================================
# bugfix.md 3.4 — the PREDISPATCH_DEFER requeue shape (pure oracle from
# the unfixed build_planner)
# ===========================================================================

build_process_lines = st.lists(
    st.tuples(
        st.integers(min_value=100, max_value=999_999),
        st.sampled_from(sorted(build_planner.BUILD_PROCESS_PATTERNS)),
    ).map(lambda pair: f"{pair[0]} bash {pair[1]} arg"),
    min_size=1, max_size=4)

innocuous_lines = st.lists(
    st.sampled_from(["1234 sshd: ubuntu@pts/0", "77 /usr/bin/python3 app.py",
                     "901 [kworker/0:1]", "55 bash"]),
    max_size=4)


class TestPredispatchDeferShapePreserved:
    """**Property 2: Preservation** — `decide_predispatch` recorded from
    the unfixed tree: a found build process defers to QUEUED at the head
    of the queue (ORIGINAL created_at retained) with deferred_at set to
    the verification time; a clean check starts.

    **Validates: Requirements 3.4**
    """

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(created_at=st.integers(min_value=1, max_value=T0),
           now=st.integers(min_value=T0, max_value=T0 + MS_PER_HOUR),
           lines=build_process_lines, noise=innocuous_lines)
    def test_defer_requeues_at_head_with_deferred_at(
            self, created_at, now, lines, noise):
        job = {"build_job_id": "job-defer", "status": "queued",
               "created_at": created_at}
        decision = build_planner.decide_predispatch(
            job, "\n".join(noise + lines), now)
        assert decision.action == build_planner.PREDISPATCH_DEFER
        assert decision.status == build_domain.STATUS_QUEUED
        assert decision.created_at == created_at  # head of queue
        assert decision.deferred_at == now
        assert set(lines) <= set(decision.build_processes)

    # Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved
    @given(noise=innocuous_lines,
           now=st.integers(min_value=T0, max_value=T0 + MS_PER_HOUR))
    def test_clean_verification_starts(self, noise, now):
        job = {"build_job_id": "job-start", "status": "queued",
               "created_at": T0 - 1}
        decision = build_planner.decide_predispatch(
            job, "\n".join(noise), now)
        assert decision.action == build_planner.PREDISPATCH_START
        assert decision.deferred_at is None
        assert decision.build_processes == ()

    def test_reverification_interval_boundaries(self):
        interval = build_planner.PREDISPATCH_RETRY_INTERVAL_MS
        assert interval == 5 * MS_PER_MINUTE
        assert build_planner.is_reverification_due(None, T0) is True
        assert build_planner.is_reverification_due(
            T0, T0 + interval - 1) is False
        assert build_planner.is_reverification_due(
            T0, T0 + interval) is True


# ===========================================================================
# bugfix.md 3.6 — the termination watchdog still terminates a GENUINELY
# terminal ephemeral job's runner (and nothing else)
# ===========================================================================

class _FakeEc2:
    def __init__(self):
        self.terminated = []

    def terminate_instances(self, InstanceIds):
        self.terminated.append(list(InstanceIds))
        return {}


def _seed_watchdog_job(job_id, status, execution_mode="ephemeral",
                       runner=None):
    job = {
        "build_job_id": job_id,
        "execution_mode": execution_mode,
        "status": status,
    }
    if runner is not None:
        job["runner"] = dict(runner)
    _JOBS.put_item(Item=job)
    return job


class TestTerminationWatchdogPreserved:
    """**Property 2: Preservation** — `termination_watchdog` recorded
    from the unfixed tree.

    **Validates: Requirements 3.6**
    """

    def test_terminal_ephemeral_runner_is_terminated(self):
        job_id = f"watchdog-term-{uuid.uuid4()}"
        job = _seed_watchdog_job(job_id, build_domain.STATUS_FAILED,
                                 runner={"instance_id": "i-dead"})
        fake_ec2 = _FakeEc2()
        with mock.patch.object(build_dispatcher, "ec2", fake_ec2):
            build_dispatcher.termination_watchdog([job], T0)
        assert fake_ec2.terminated == [["i-dead"]]
        stored = build_dispatcher.to_native(
            _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))
        assert stored["runner"]["terminated_at"] == T0

    def test_nonterminal_dedicated_and_done_jobs_untouched(self):
        nonterminal = _seed_watchdog_job(
            f"watchdog-run-{uuid.uuid4()}", build_domain.STATUS_BUILDING,
            runner={"instance_id": "i-live"})
        dedicated = _seed_watchdog_job(
            f"watchdog-ded-{uuid.uuid4()}", build_domain.STATUS_FAILED,
            execution_mode="dedicated", runner={"instance_id": "i-srv"})
        already = _seed_watchdog_job(
            f"watchdog-done-{uuid.uuid4()}", build_domain.STATUS_FAILED,
            runner={"instance_id": "i-gone", "terminated_at": T0 - 1})
        fake_ec2 = _FakeEc2()
        with mock.patch.object(build_dispatcher, "ec2", fake_ec2):
            build_dispatcher.termination_watchdog(
                [nonterminal, dedicated, already], T0)
        assert fake_ec2.terminated == []


# ===========================================================================
# bugfix.md 3.8 — the job-01b18948 lock ownership heal is untouched
# ===========================================================================

class TestLockOwnershipHealPreserved:
    """**Validates: Requirements 3.8**"""

    RECORDED_HEAL = ("if [ -f /var/lock/dda-build.lock ]; then "
                     "chown ubuntu:ubuntu /var/lock/dda-build.lock "
                     "2>/dev/null || true; fi")

    def test_heal_command_content_untouched(self):
        assert build_dispatcher.lock_ownership_heal_command() == \
            self.RECORDED_HEAL
        assert build_dispatcher.BUILD_LOCK_FILE == \
            "/var/lock/dda-build.lock"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q", "--noconftest",
                          "-p", "no:cacheprovider"]))
