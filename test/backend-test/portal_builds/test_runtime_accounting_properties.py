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
Runtime accounting / storage-amendment property tests
(build-fleet-execution-failures). Task 8.5 contributes **Property 16**
here; task 10.5 adds Properties 7, 8, 9, and 14 plus the immutable
budget-resolution-order property.

**Property 7: Timeout Boundary and Hard Ceiling**
**Property 8: Heartbeat and Progress Leases**
**Property 9: Queue and Provisioning Time Isolation**
**Property 14: Evidence-Gated Timeout Diagnosis**

**Validates: Requirements 2.13, 2.14, 2.15, 2.16, 2.17, 2.18, 3.2,
3.4, 3.11, 3.12**

**Property 16: Volume-Size Default and Per-Target Resolution**

**Validates: Requirements 2.20, 3.13**

_For any_ explicit, per-target, or defaulted submission and any
previously created job:

  - the documented global default is 200 GB (raised from 100);
  - any configured JP6 per-target entry below 200 GB is rejected by
    validation (JP6 >= 200);
  - the volume size is resolved ONCE at submission in design order
    (explicit request value, per-target map entry, global value) and
    snapshotted immutably into ``config_snapshot.volume_size_gb``;
  - later configuration changes never alter a created job's snapshot,
    and ``plan_runner`` keeps reading the snapshot unchanged;
  - previously created jobs keep their snapshotted volume size,
    instance type, and spot choice (no retroactive adoption).

Everything here is pure (``build_domain.py`` / ``build_planner.py``):
no AWS, no I/O, no moto.

Run ONLY this file, from the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_runtime_accounting_properties.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import copy
import os
import sys

from hypothesis import given, settings, strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_reconciliation  # noqa: E402

_TARGETS = ("JP5", "JP6", "AMD64", "AMD64_NVIDIA")

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Valid per-target volume-size maps: positive sizes, JP6 >= 200.
_valid_by_target_maps = st.dictionaries(
    st.sampled_from(_TARGETS),
    st.integers(min_value=20, max_value=2000),
    max_size=4,
).map(lambda m: {
    target: (max(size, build_domain.MIN_JP6_VOLUME_SIZE_GB)
             if target == "JP6" else size)
    for target, size in m.items()
})

#: Optional stored global volume value (None -> the documented default).
_stored_globals = st.one_of(
    st.none(), st.integers(min_value=20, max_value=2000))

#: Optional explicit request value.
_explicit_values = st.one_of(
    st.none(), st.integers(min_value=20, max_value=2000))


@st.composite
def _submissions(draw):
    """One submission: targets, optional explicit value, optional
    per-target map, optional stored global volume."""
    targets = draw(st.lists(st.sampled_from(_TARGETS),
                            min_size=1, max_size=4))
    explicit = draw(_explicit_values)
    by_target = draw(st.one_of(st.none(), _valid_by_target_maps))
    stored_global = draw(_stored_globals)
    stored = {}
    if stored_global is not None:
        stored["volume_size_gb"] = stored_global
    if by_target is not None:
        stored["volume_size_gb_by_target"] = by_target
    return targets, explicit, by_target, stored


def _create_jobs(targets, explicit, config):
    job_ids = [f"job-{index}" for index in range(len(targets))]
    return build_domain.create_build_jobs(
        targets=list(targets), execution_mode="ephemeral", server_id=None,
        request_id="req-p16", job_ids=job_ids, requested_by="user-p16",
        created_at=1_754_000_000_000, config_snapshot=config,
        volume_size_gb=explicit)


# ---------------------------------------------------------------------------
# Property 16: Volume-Size Default and Per-Target Resolution
# **Validates: Requirements 2.20, 3.13**
# ---------------------------------------------------------------------------

def test_documented_global_default_is_200():
    """The documented global default rose from 100 to 200 GB and applies
    on read when nothing is stored (Req 2.20)."""
    assert build_domain.DEFAULT_BUILD_CONFIG["volume_size_gb"] == 200
    assert build_domain.effective_build_config(None)["volume_size_gb"] == 200
    assert build_domain.effective_build_config(
        {"volume_size_gb": None})["volume_size_gb"] == 200


@settings(max_examples=150, deadline=None)
@given(case=_submissions())
def test_submission_time_resolution_order_and_snapshot(case):
    """For any submission, each created job's snapshot carries exactly
    the design-order resolution — explicit request value, per-target map
    entry, then the effective global value (default 200) — resolved once
    at submission (Req 2.20)."""
    targets, explicit, by_target, stored = case
    config = build_domain.effective_build_config(stored)
    effective_global = config["volume_size_gb"]

    jobs = _create_jobs(targets, explicit, config)

    for job in jobs:
        target = job["build_target"]
        if explicit is not None:
            expected = explicit
        elif by_target is not None and by_target.get(target) is not None:
            expected = by_target[target]
        else:
            expected = effective_global
        observed = job["config_snapshot"]["volume_size_gb"]
        assert observed == expected, (
            f"target={target} explicit={explicit} by_target={by_target} "
            f"global={effective_global}: snapshot volume {observed!r}, "
            f"expected {expected!r}")
        # plan_runner keeps reading the snapshot unchanged (Req 3.13).
        plan = build_planner.plan_runner(job)
        assert plan.volume_size_gb == expected


@settings(max_examples=100, deadline=None)
@given(case=_submissions(),
       later_global=st.integers(min_value=20, max_value=2000),
       later_jp6=st.integers(min_value=200, max_value=4000))
def test_snapshot_immutability_under_later_config_changes(
        case, later_global, later_jp6):
    """For any created job, later configuration changes (global or
    per-target) never alter the job's snapshotted volume size, instance
    type, or spot choice, and its runner plan is identical before and
    after the change (Req 3.13)."""
    targets, explicit, _, stored = case
    config = build_domain.effective_build_config(stored)
    jobs = _create_jobs(targets, explicit, config)
    snapshots_before = copy.deepcopy(
        [job["config_snapshot"] for job in jobs])
    plans_before = [build_planner.plan_runner(job) for job in jobs]

    # Simulate an admin configuration change AFTER the jobs exist.
    new_stored, result = build_domain.apply_config_update(
        stored, {"volume_size_gb": later_global,
                 "volume_size_gb_by_target": {"JP6": later_jp6}})
    assert result.valid, result.errors
    build_domain.effective_build_config(new_stored)  # read-side only

    for job, snapshot_before, plan_before in zip(
            jobs, snapshots_before, plans_before):
        assert job["config_snapshot"] == snapshot_before
        plan_after = build_planner.plan_runner(job)
        assert plan_after == plan_before
        assert plan_after.volume_size_gb == \
            snapshot_before["volume_size_gb"]
        assert plan_after.instance_type == plan_before.instance_type
        assert plan_after.spot == plan_before.spot


@settings(max_examples=100, deadline=None)
@given(prior_volume=st.integers(min_value=20, max_value=199),
       target=st.sampled_from(_TARGETS),
       instance_suffix=st.sampled_from(["4xlarge", "8xlarge"]),
       spot=st.booleans())
def test_previously_created_jobs_keep_their_snapshots(
        prior_volume, target, instance_suffix, spot):
    """For any previously created job (snapshotted under the old 100 GB
    era, possibly below 200), the raised default is NOT retroactively
    adopted: the plan honors the job's own snapshotted volume size,
    instance type, and spot choice exactly (Req 3.13)."""
    arch = build_domain.required_arch_for_target(target)
    instance_key = ("arm64_instance_type" if arch == "arm64"
                    else "x86_64_instance_type")
    family = "m6g" if arch == "arm64" else "m6i"
    snapshot = {
        instance_key: f"{family}.{instance_suffix}",
        "volume_size_gb": prior_volume,
        "max_runtime_hours": 4,
        "use_spot_for_ephemeral": spot,
    }
    job = {"build_job_id": "prior-job", "build_target": target,
           "execution_mode": "ephemeral",
           "config_snapshot": copy.deepcopy(snapshot)}
    plan = build_planner.plan_runner(job)
    assert plan.volume_size_gb == prior_volume
    assert plan.instance_type == f"{family}.{instance_suffix}"
    assert plan.spot is spot
    assert job["config_snapshot"] == snapshot


@settings(max_examples=100, deadline=None)
@given(size=st.integers(min_value=1, max_value=199))
def test_jp6_per_target_entries_below_200_are_rejected(size):
    """For any configured JP6 per-target entry below 200 GB, validation
    rejects the update atomically (JP6 >= 200, Req 2.20)."""
    result = build_domain.validate_build_config(
        {"volume_size_gb_by_target": {"JP6": size}})
    assert not result.valid
    assert any(e["rule"] == build_domain.RULE_CONFIG_JP6_VOLUME_MINIMUM
               for e in result.errors), [dict(e) for e in result.errors]
    # Atomic reject: the stored configuration is unchanged.
    stored = {"volume_size_gb": 200}
    new_stored, result = build_domain.apply_config_update(
        stored, {"volume_size_gb_by_target": {"JP6": size}})
    assert not result.valid
    assert new_stored == stored


@settings(max_examples=100, deadline=None)
@given(by_target=_valid_by_target_maps)
def test_valid_per_target_maps_are_accepted(by_target):
    """For any per-target map with positive sizes and JP6 >= 200,
    validation accepts (Req 2.20)."""
    result = build_domain.validate_build_config(
        {"volume_size_gb_by_target": by_target})
    assert result.valid, [dict(e) for e in result.errors]


# ===========================================================================
# Task 10.5 — Properties 7, 8, 9, 14: runtime accounting and the
# evidence gate (build_reconciliation phase clocks / leases / budgets,
# build_planner watchdog and queue ordering). Everything pure: no AWS,
# no I/O, no moto.
# ===========================================================================

_MS_PER_MINUTE = 60 * 1000
_MS_PER_HOUR = 60 * _MS_PER_MINUTE
_BASE_MS = 1_754_000_000_000
_MODES = (build_domain.EXECUTION_MODE_EPHEMERAL,
          build_domain.EXECUTION_MODE_DEDICATED)
_RUNNING_STATUSES = (build_domain.STATUS_BUILDING,
                     build_domain.STATUS_PUBLISHING)

#: Hours in quarter-hour steps: every derived deadline lands on an
#: exact integral millisecond, so `now == deadline` is representable.
_quarter_hours = st.integers(min_value=1, max_value=32).map(
    lambda n: n / 4)

#: The complete timing-evidence key set every decision must carry
#: (Req 2.18); unavailable fields are PRESENT with value None.
_EVIDENCE_KEYS = frozenset({
    "phase", "observed_ms", "queue_wait_ms", "provisioning_ms",
    "execution_runtime_ms", "budget_ms", "budget_source",
    "hard_runtime_ms", "last_heartbeat_at", "last_progress_at",
    "last_progress_kind", "execution_started_at", "build_target",
    "execution_mode", "evaluated_at",
})

_EXECUTION_TIMEOUT_CODES = frozenset({
    build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED,
    build_reconciliation.CODE_AGENT_HEARTBEAT_EXPIRED,
    build_reconciliation.CODE_BUILD_PROGRESS_STALLED,
})

_ATTEMPT = {"attempt_id": "att-1"}


def _running_job(target, mode, snapshot, started_at, status=None):
    """A running job with positive execution-start evidence."""
    return {
        "build_job_id": "job-runtime",
        "build_target": target,
        "execution_mode": mode,
        "status": status or build_domain.STATUS_BUILDING,
        "execution_attempt": dict(_ATTEMPT),
        "created_at": started_at - _MS_PER_HOUR,
        "timing": {
            "execution_started_at": started_at,
            "last_heartbeat_at": started_at,
            "last_progress_at": started_at,
        },
        "config_snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# Immutable budget resolution order (task 8.3 seam)
# **Validates: Requirements 2.17, 3.12**
# ---------------------------------------------------------------------------

@st.composite
def _resolution_cases(draw):
    """A config_snapshot exercising every combination of the four
    budget-resolution levels, with three DISTINCT hour values so a
    wrong pick is always detected."""
    target = draw(st.sampled_from(_TARGETS))
    mode = draw(st.sampled_from(_MODES))
    override_h, default_h, max_h = draw(st.lists(
        _quarter_hours, min_size=3, max_size=3, unique=True))
    has_override = draw(st.booleans())
    has_default = draw(st.booleans())
    has_max = draw(st.booleans())

    per_mode = {}
    if has_override:
        per_mode[mode] = {"hard_runtime_hours": override_h,
                          "heartbeat_lease_minutes": 7}
    if has_default:
        per_mode["default"] = {"hard_runtime_hours": default_h,
                               "heartbeat_lease_minutes": 13}
    snapshot = {}
    if per_mode:
        snapshot["runtime_budgets"] = {target: per_mode}
    if has_max:
        snapshot["max_runtime_hours"] = max_h

    if has_override:
        expected_hours = override_h
        expected_source = "target_mode_override"
        expected_lease = 7 * _MS_PER_MINUTE
    elif has_default:
        expected_hours = default_h
        expected_source = "target_default"
        expected_lease = 13 * _MS_PER_MINUTE
    elif has_max:
        expected_hours = max_h
        expected_source = "snapshot_max_runtime_hours"
        expected_lease = None
    else:
        expected_hours = build_reconciliation.DEFAULT_HARD_RUNTIME_HOURS
        expected_source = "compatibility_default"
        expected_lease = None
    return (target, mode, snapshot, expected_hours, expected_source,
            expected_lease)


@settings(max_examples=150, deadline=None)
@given(case=_resolution_cases())
def test_budget_resolution_order_is_immutable(case):
    """For any snapshot shape, the hard budget resolves in exactly the
    design order — target/mode override, target default, snapshotted
    max_runtime_hours, then the compatibility default — from the job's
    OWN config_snapshot only, deterministically and without mutating
    the snapshot (Req 2.17, 3.12)."""
    (target, mode, snapshot, expected_hours, expected_source,
     expected_lease) = case
    job = {"build_job_id": "job-budget", "build_target": target,
           "execution_mode": mode,
           "config_snapshot": copy.deepcopy(snapshot)}
    before = copy.deepcopy(job)

    budget = build_reconciliation.effective_budget(job)
    assert budget.source == expected_source
    assert budget.hard_runtime_ms == expected_hours * _MS_PER_HOUR
    # Soft leases come only from the SAME chosen entry (never mixed
    # across levels) and are disabled at the fallback levels.
    assert budget.heartbeat_lease_ms == expected_lease
    # Resolution is a pure read: deterministic, snapshot unchanged.
    assert build_reconciliation.effective_budget(job) == budget
    assert job == before


# ---------------------------------------------------------------------------
# Property 7: Timeout Boundary and Hard Ceiling
# **Validates: Requirements 2.16, 2.17, 3.11, 3.12**
# ---------------------------------------------------------------------------

@st.composite
def _hard_budget_jobs(draw):
    """A running job whose hard ceiling comes from one of the four
    resolution levels, with an exactly representable deadline."""
    target = draw(st.sampled_from(_TARGETS))
    mode = draw(st.sampled_from(_MODES))
    status = draw(st.sampled_from(_RUNNING_STATUSES))
    level = draw(st.sampled_from((
        "target_mode_override", "target_default",
        "snapshot_max_runtime_hours", "compatibility_default")))
    hours = draw(_quarter_hours)
    snapshot = {}
    if level == "target_mode_override":
        snapshot["runtime_budgets"] = {
            target: {mode: {"hard_runtime_hours": hours}}}
    elif level == "target_default":
        snapshot["runtime_budgets"] = {
            target: {"default": {"hard_runtime_hours": hours}}}
    elif level == "snapshot_max_runtime_hours":
        snapshot["max_runtime_hours"] = hours
    else:
        hours = build_reconciliation.DEFAULT_HARD_RUNTIME_HOURS
    started = _BASE_MS + draw(st.integers(0, _MS_PER_HOUR))
    job = _running_job(target, mode, snapshot, started, status)
    deadline = started + int(hours * _MS_PER_HOUR)
    return job, deadline, level


@settings(max_examples=150, deadline=None)
@given(case=_hard_budget_jobs(),
       before_by=st.integers(min_value=1, max_value=_MS_PER_HOUR),
       after_by=st.integers(min_value=1, max_value=_MS_PER_HOUR))
def test_hard_deadline_boundary_is_strict(case, before_by, after_by):
    """For any snapshotted hard budget (every resolution level):
    `now < deadline` and `now == deadline` continue; ONLY
    `now > deadline` expires, as MAX_RUNTIME_EXCEEDED (Req 3.12)."""
    job, deadline, level = case

    below = build_reconciliation.decide_timing(job, deadline - before_by)
    assert not below.timed_out

    at = build_reconciliation.decide_timing(job, deadline)
    assert not at.timed_out, (
        f"now == hard deadline must NOT expire (source={level})")
    assert at.classification == build_reconciliation.TIMEOUT_CONTINUE

    over = build_reconciliation.decide_timing(job, deadline + after_by)
    assert over.timed_out
    assert over.classification == \
        build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
    assert over.evidence["phase"] == "execution"
    assert over.evidence["budget_source"] == level
    assert over.evidence["budget_ms"] == over.evidence["hard_runtime_ms"]


@settings(max_examples=100, deadline=None)
@given(hours=st.integers(min_value=1, max_value=12),
       started_offset=st.integers(min_value=0, max_value=_MS_PER_HOUR),
       after_by=st.integers(min_value=1, max_value=_MS_PER_HOUR),
       status=st.sampled_from(_RUNNING_STATUSES))
def test_legacy_snapshot_boundary_is_strict(hours, started_offset,
                                            after_by, status):
    """For any LEGACY-shaped job (no timing map; snapshotted
    max_runtime_hours only), the frozen watchdog keeps the strict
    boundary: `now == deadline` continues, only `now > deadline`
    expires (Req 3.12)."""
    started = _BASE_MS + started_offset
    job = {"build_job_id": "job-legacy", "build_target": "AMD64",
           "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
           "status": status, "started_at": started,
           "config_snapshot": {"max_runtime_hours": hours}}
    deadline = started + hours * _MS_PER_HOUR

    at = build_planner.decide_runtime_timeout(job, deadline)
    assert not at.timed_out
    assert at.status == status

    over = build_planner.decide_runtime_timeout(job, deadline + after_by)
    assert over.timed_out
    assert over.status == build_domain.STATUS_FAILED
    assert over.classification == \
        build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED


@settings(max_examples=100, deadline=None)
@given(hours=_quarter_hours,
       gaps=st.lists(st.integers(min_value=1,
                                 max_value=5 * _MS_PER_MINUTE),
                     min_size=1, max_size=10),
       kinds=st.lists(st.sampled_from(("heartbeat", "progress")),
                      min_size=10, max_size=10),
       after_by=st.integers(min_value=1, max_value=_MS_PER_HOUR))
def test_no_activity_sequence_extends_the_hard_ceiling(
        hours, gaps, kinds, after_by):
    """For any heartbeat/progress sequence — including meaningful
    progress observed AT the deadline itself — the hard ceiling never
    moves, and `now > deadline` still expires MAX_RUNTIME_EXCEEDED
    even while every lease is fresh (Req 2.16, 2.17)."""
    started = _BASE_MS
    snapshot = {"runtime_budgets": {"JP6": {"ephemeral": {
        "hard_runtime_hours": hours,
        # Generous leases: fresh activity must NOT be the reason the
        # job survives or dies — only the hard ceiling decides here.
        "heartbeat_lease_minutes": 24 * 60,
        "progress_stall_minutes": 24 * 60,
    }}}}
    job = _running_job("JP6", "ephemeral", snapshot, started)
    deadline = started + int(hours * _MS_PER_HOUR)
    original_deadline = build_reconciliation.hard_deadline_ms(job)
    assert original_deadline == deadline

    t = started
    for seq, (kind, gap) in enumerate(zip(kinds, gaps), start=1):
        t = min(t + gap, deadline)
        observe = (build_reconciliation.observe_heartbeat
                   if kind == "heartbeat"
                   else build_reconciliation.observe_progress)
        update = observe(job, _ATTEMPT, seq, t)
        assert update.accepted
        job["timing"] = update.timing
        # The hard deadline is unchanged by every accepted event.
        assert build_reconciliation.hard_deadline_ms(job) == deadline

    # Final meaningful progress exactly AT the deadline.
    final = build_reconciliation.observe_progress(
        job, _ATTEMPT, len(kinds) + 1, deadline)
    assert final.accepted
    job["timing"] = final.timing
    assert build_reconciliation.hard_deadline_ms(job) == deadline

    at = build_reconciliation.decide_timing(job, deadline)
    assert not at.timed_out
    over = build_reconciliation.decide_timing(job, deadline + after_by)
    assert over.timed_out
    assert over.classification == \
        build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED


# ---------------------------------------------------------------------------
# Property 8: Heartbeat and Progress Leases
# **Validates: Requirements 2.16, 2.18**
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(events=st.lists(
    st.tuples(st.sampled_from(("heartbeat", "progress")),
              st.integers(min_value=1, max_value=10 * _MS_PER_MINUTE)),
    min_size=1, max_size=12))
def test_progress_renews_both_leases_heartbeat_only_liveness(events):
    """For any correlated activity sequence: fresh progress renews BOTH
    the progress lease and liveness; a heartbeat renews ONLY liveness;
    duplicate/non-increasing sequences and stale-attempt evidence are
    no-ops (Req 2.16)."""
    started = _BASE_MS
    job = _running_job("JP5", "dedicated", {}, started)
    expected_heartbeat = expected_progress = started

    t = started
    for seq, (kind, gap) in enumerate(events, start=1):
        t += gap
        if kind == "heartbeat":
            update = build_reconciliation.observe_heartbeat(
                job, _ATTEMPT, seq, t)
            assert update.accepted
            job["timing"] = update.timing
            expected_heartbeat = t
        else:
            update = build_reconciliation.observe_progress(
                job, _ATTEMPT, seq, t)
            assert update.accepted
            job["timing"] = update.timing
            expected_progress = t
            expected_heartbeat = max(expected_heartbeat, t)
        assert job["timing"]["last_heartbeat_at"] == expected_heartbeat
        assert job["timing"]["last_progress_at"] == expected_progress

        # Replaying the SAME sequence number is a no-op.
        observe = (build_reconciliation.observe_heartbeat
                   if kind == "heartbeat"
                   else build_reconciliation.observe_progress)
        replay = observe(job, _ATTEMPT, seq, t + 1)
        assert not replay.accepted
        # Evidence from a DIFFERENT attempt is rejected outright.
        stale = observe(job, {"attempt_id": "att-other"}, seq + 1, t + 1)
        assert not stale.accepted
        assert job["timing"]["last_heartbeat_at"] == expected_heartbeat
        assert job["timing"]["last_progress_at"] == expected_progress


@settings(max_examples=150, deadline=None)
@given(heartbeat_minutes=st.integers(min_value=2, max_value=60),
       stall_minutes=st.integers(min_value=2, max_value=120),
       extra=st.integers(min_value=1, max_value=_MS_PER_HOUR),
       status=st.sampled_from(_RUNNING_STATUSES))
def test_lease_classifications_are_distinct_and_strict(
        heartbeat_minutes, stall_minutes, extra, status):
    """Below the hard ceiling: a stale heartbeat classifies
    AGENT_HEARTBEAT_EXPIRED, fresh-heartbeat-with-stalled-progress
    classifies BUILD_PROGRESS_STALLED, the two codes are distinct, and
    both lease boundaries are strict (`now == lease edge` continues)
    (Req 2.16, 2.18)."""
    started = _BASE_MS
    heartbeat_ms = heartbeat_minutes * _MS_PER_MINUTE
    stall_ms = stall_minutes * _MS_PER_MINUTE
    snapshot = {"runtime_budgets": {"AMD64": {"dedicated": {
        "hard_runtime_hours": 1000,
        "heartbeat_lease_minutes": heartbeat_minutes,
        "progress_stall_minutes": stall_minutes,
    }}}}

    # Case A: no heartbeat at all since execution start.
    job_a = _running_job("AMD64", "dedicated", snapshot, started, status)
    stale_now = started + heartbeat_ms + extra
    stale = build_reconciliation.decide_timing(job_a, stale_now)
    assert stale.timed_out
    assert stale.classification == \
        build_reconciliation.CODE_AGENT_HEARTBEAT_EXPIRED
    assert stale.evidence["budget_ms"] == heartbeat_ms
    assert stale.evidence["last_heartbeat_at"] == started

    # Strict heartbeat boundary: at exactly the lease edge the
    # heartbeat is NOT expired (the decision may still be a progress
    # stall when the heartbeat lease is longer than the stall lease).
    at_edge = build_reconciliation.decide_timing(
        job_a, started + heartbeat_ms)
    assert at_edge.classification != \
        build_reconciliation.CODE_AGENT_HEARTBEAT_EXPIRED
    if heartbeat_ms <= stall_ms:
        assert not at_edge.timed_out

    # Case B: heartbeats stay fresh but progress stalls.
    job_b = _running_job("AMD64", "dedicated", snapshot, started, status)
    stalled_now = started + stall_ms + extra
    job_b["timing"]["last_heartbeat_at"] = stalled_now  # fresh liveness
    stalled = build_reconciliation.decide_timing(job_b, stalled_now)
    assert stalled.timed_out
    assert stalled.classification == \
        build_reconciliation.CODE_BUILD_PROGRESS_STALLED
    assert stalled.evidence["budget_ms"] == stall_ms
    assert stalled.evidence["last_progress_at"] == started

    # Strict progress boundary: at exactly the stall edge, continue.
    job_b["timing"]["last_heartbeat_at"] = started + stall_ms
    boundary = build_reconciliation.decide_timing(
        job_b, started + stall_ms)
    assert not boundary.timed_out

    # The two lease expiries are DISTINCT classifications.
    assert stale.classification != stalled.classification


# ---------------------------------------------------------------------------
# Property 9: Queue and Provisioning Time Isolation
# **Validates: Requirements 2.14, 2.15, 3.2, 3.4**
# ---------------------------------------------------------------------------

@settings(max_examples=150, deadline=None)
@given(queue_ms=st.integers(min_value=0, max_value=100 * _MS_PER_HOUR),
       provisioning_delay_ms=st.integers(min_value=0,
                                         max_value=10 * _MS_PER_HOUR),
       hours=_quarter_hours,
       within_by=st.integers(min_value=0, max_value=_MS_PER_HOUR),
       over_by=st.integers(min_value=1, max_value=_MS_PER_HOUR))
def test_queue_and_provisioning_never_charge_execution(
        queue_ms, provisioning_delay_ms, hours, within_by, over_by):
    """For any queue/provisioning delay — even far beyond the hard
    budget — active execution runtime stays anchored on execution-start
    evidence only: the job survives while execution runtime is within
    the hard budget and expires only when EXECUTION time (never
    queue/provisioning time) exceeds it (Req 2.14, 2.15)."""
    created = _BASE_MS
    dispatched = created + queue_ms
    execution_started = dispatched + provisioning_delay_ms
    hard_ms = int(hours * _MS_PER_HOUR)
    snapshot = {"runtime_budgets": {"JP6": {"ephemeral": {
        "hard_runtime_hours": hours}}}}
    job = {
        "build_job_id": "job-isolation", "build_target": "JP6",
        "execution_mode": "ephemeral",
        "status": build_domain.STATUS_BUILDING,
        "execution_attempt": dict(_ATTEMPT),
        "created_at": created, "dispatched_at": dispatched,
        "timing": {
            "provisioning_started_at": dispatched,
            "provisioning_ended_at": execution_started,
            "execution_started_at": execution_started,
        },
        "config_snapshot": snapshot,
    }

    # Within the hard budget of EXECUTION time: never timed out, even
    # though total wall time since submission may dwarf the budget.
    now = execution_started + min(within_by, hard_ms)
    assert build_reconciliation.execution_runtime_ms(job, now) == \
        now - execution_started
    decision = build_reconciliation.decide_timing(job, now)
    assert not decision.timed_out
    assert decision.evidence["queue_wait_ms"] == queue_ms
    assert decision.evidence["provisioning_ms"] == provisioning_delay_ms

    # Expiry happens only when EXECUTION time crosses the budget.
    over = build_reconciliation.decide_timing(
        job, execution_started + hard_ms + over_by)
    assert over.timed_out
    assert over.classification == \
        build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
    assert over.evidence["execution_runtime_ms"] == hard_ms + over_by


@settings(max_examples=100, deadline=None)
@given(elapsed=st.integers(min_value=0, max_value=200 * _MS_PER_HOUR),
       status=st.sampled_from(_RUNNING_STATUSES),
       hours=_quarter_hours)
def test_active_runtime_requires_positive_execution_evidence(
        elapsed, status, hours):
    """For any elapsed wall time WITHOUT positive execution-start
    evidence, active runtime is zero and the decision is the fail-safe
    WAIT_FOR_EXECUTION_EVIDENCE — never a runtime expiry (Req 2.14)."""
    created = _BASE_MS
    job = {"build_job_id": "job-no-start", "build_target": "AMD64",
           "execution_mode": "dedicated", "status": status,
           "created_at": created, "timing": {},
           "config_snapshot": {"max_runtime_hours": hours}}
    now = created + elapsed
    assert build_reconciliation.execution_runtime_ms(job, now) == 0
    decision = build_reconciliation.decide_timing(job, now)
    assert not decision.timed_out
    assert decision.classification == \
        build_reconciliation.TIMEOUT_WAIT_FOR_EXECUTION_EVIDENCE
    # The planner watchdog agrees (new-shape running job).
    planner = build_planner.decide_runtime_timeout(job, now)
    assert not planner.timed_out
    assert planner.status == status


@settings(max_examples=150, deadline=None)
@given(phase=st.sampled_from(("queue_wait", "provisioning")),
       configured=st.booleans(),
       budget_quarters=st.integers(min_value=1, max_value=32),
       delta=st.integers(min_value=-_MS_PER_HOUR, max_value=_MS_PER_HOUR))
def test_phase_budgets_apply_only_when_snapshotted(
        phase, configured, budget_quarters, delta):
    """Queue-wait and provisioning budgets are OPTIONAL and independent:
    with no snapshotted budget the phase never expires for ANY
    duration; with an explicit budget the strict boundary applies and
    the classification names the phase (Req 2.14, 2.15)."""
    created = _BASE_MS
    if phase == "queue_wait":
        budget_ms = budget_quarters * (_MS_PER_HOUR // 4)
        entry = {"queue_wait_hours": budget_quarters / 4}
        status = build_domain.STATUS_QUEUED
        code = build_reconciliation.CODE_QUEUE_WAIT_TIMEOUT
        job_extra = {}
    else:
        budget_ms = budget_quarters * _MS_PER_MINUTE
        entry = {"provisioning_minutes": budget_quarters}
        status = build_domain.STATUS_PROVISIONING
        code = build_reconciliation.CODE_PROVISIONING_TIMEOUT
        job_extra = {"dispatched_at": created}
    snapshot = {"max_runtime_hours": 4}
    if configured:
        snapshot["runtime_budgets"] = {"JP6": {"ephemeral": entry}}
    job = {"build_job_id": "job-phase", "build_target": "JP6",
           "execution_mode": "ephemeral", "status": status,
           "created_at": created, "config_snapshot": snapshot}
    job.update(job_extra)

    now = created + max(0, budget_ms + delta)
    decision = build_reconciliation.decide_timing(job, now)
    if not configured:
        assert not decision.timed_out, (
            "a phase without an explicitly snapshotted budget never "
            "expires")
    else:
        should_expire = max(0, budget_ms + delta) > budget_ms
        assert decision.timed_out == should_expire
        if should_expire:
            assert decision.classification == code
            assert decision.evidence["phase"] == phase
            assert decision.evidence["budget_ms"] == budget_ms
        elif delta == 0:
            # Strict boundary: exactly AT the budget continues.
            assert not decision.timed_out


@settings(max_examples=100, deadline=None)
@given(offsets=st.lists(st.integers(min_value=0, max_value=10**6),
                        min_size=2, max_size=8, unique=True),
       occupied=st.booleans())
def test_occupied_server_jobs_stay_queued_in_oldest_order(
        offsets, occupied):
    """For any set of queued dedicated jobs behind one server: while
    the server is occupied every job stays queued; when it frees,
    exactly ONE job starts and it is the oldest eligible; promotion
    selects the same oldest job (Req 3.2, 3.4)."""
    jobs = [{
        "build_job_id": f"job-{index}",
        "status": build_domain.STATUS_QUEUED,
        "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
        "server_id": "server-1",
        "created_at": _BASE_MS + offset,
        "predecessor_job_id": None,
    } for index, offset in enumerate(offsets)]
    servers = [{"server_id": "server-1",
                "running_build_job_id":
                    "job-running" if occupied else None}]

    decisions = build_planner.plan_dedicated_dispatch(jobs, servers)
    assert len(decisions) == len(jobs)
    oldest = min(jobs, key=lambda j: (j["created_at"],
                                      j["build_job_id"]))
    if occupied:
        assert all(d.action == build_planner.ALLOCATION_QUEUE
                   and d.status == build_domain.STATUS_QUEUED
                   for d in decisions)
    else:
        starts = [d for d in decisions
                  if d.action == build_planner.ALLOCATION_START]
        assert len(starts) == 1
        assert starts[0].build_job_id == oldest["build_job_id"]
        assert all(d.status == build_domain.STATUS_QUEUED
                   for d in decisions
                   if d.action == build_planner.ALLOCATION_QUEUE)

    promoted = build_planner.promote_next("server-1", jobs)
    assert promoted is not None
    assert promoted["build_job_id"] == oldest["build_job_id"]


# ---------------------------------------------------------------------------
# Property 14: Evidence-Gated Timeout Diagnosis
# **Validates: Requirements 2.13, 2.18**
# ---------------------------------------------------------------------------

@st.composite
def _arbitrary_lifecycle_jobs(draw):
    """Jobs across every status with arbitrary (consistent) timing
    shapes, optional budgets, and an arbitrary evaluation time."""
    target = draw(st.sampled_from(_TARGETS))
    mode = draw(st.sampled_from(_MODES))
    status = draw(st.sampled_from(sorted(build_domain.ALL_STATUSES)))
    created = _BASE_MS

    snapshot = {}
    if draw(st.booleans()):
        snapshot["max_runtime_hours"] = draw(
            st.integers(min_value=1, max_value=8))
    entry = {}
    for key, strategy in (
            ("hard_runtime_hours", _quarter_hours),
            ("heartbeat_lease_minutes",
             st.integers(min_value=1, max_value=90)),
            ("progress_stall_minutes",
             st.integers(min_value=1, max_value=180)),
            ("queue_wait_hours", _quarter_hours),
            ("provisioning_minutes",
             st.integers(min_value=1, max_value=120))):
        if draw(st.booleans()):
            entry[key] = draw(strategy)
    if entry and draw(st.booleans()):
        snapshot["runtime_budgets"] = {target: {mode: entry}}

    job = {"build_job_id": "job-p14", "build_target": target,
           "execution_mode": mode, "status": status,
           "created_at": created, "config_snapshot": snapshot}
    dispatched = created + draw(
        st.integers(min_value=0, max_value=2 * _MS_PER_HOUR))
    if draw(st.booleans()):
        job["dispatched_at"] = dispatched
    timing = {}
    if draw(st.booleans()):
        execution_started = dispatched + draw(
            st.integers(min_value=0, max_value=_MS_PER_HOUR))
        timing["execution_started_at"] = execution_started
        if draw(st.booleans()):
            timing["last_heartbeat_at"] = execution_started + draw(
                st.integers(min_value=0, max_value=_MS_PER_HOUR))
        if draw(st.booleans()):
            timing["last_progress_at"] = execution_started + draw(
                st.integers(min_value=0, max_value=_MS_PER_HOUR))
    if timing or draw(st.booleans()):
        job["timing"] = timing
    now = created + draw(
        st.integers(min_value=0, max_value=48 * _MS_PER_HOUR))
    return job, now


@settings(max_examples=200, deadline=None)
@given(case=_arbitrary_lifecycle_jobs())
def test_every_timing_decision_carries_complete_evidence(case):
    """For ANY job shape and evaluation time, the decision carries the
    complete timing-evidence record — phase, observed duration,
    budget/value/source, target/mode, last activity — with unavailable
    fields PRESENT as None (identified, never fabricated), and every
    timeout classification is backed by the evidence that names its
    phase and exceeded budget (Req 2.13, 2.18)."""
    job, now = case
    decision = build_reconciliation.decide_timing(job, now)
    evidence = decision.evidence

    # Complete evidence: every field exists (None == identified as
    # unavailable), and identity fields are truthful.
    assert _EVIDENCE_KEYS <= set(evidence.keys())
    assert evidence["evaluated_at"] == now
    assert evidence["build_target"] == job["build_target"]
    assert evidence["execution_mode"] == job["execution_mode"]
    timing = job.get("timing") or {}
    assert evidence["execution_started_at"] == \
        timing.get("execution_started_at")
    assert evidence["last_heartbeat_at"] == \
        timing.get("last_heartbeat_at")
    assert evidence["last_progress_at"] == timing.get("last_progress_at")

    # The persisted record identifies the timeout kind truthfully.
    record = build_reconciliation.timeout_evidence_record(decision, now)
    assert _EVIDENCE_KEYS <= set(record.keys())
    if decision.timed_out:
        assert record["timeout_kind"] == decision.classification
        assert record["timeout_decided_at"] == now
    else:
        assert record["timeout_kind"] is None
        assert record["timeout_decided_at"] is None

    if not decision.timed_out:
        return

    # Every timeout is evidence-backed: the classification names the
    # measured phase, and the observed duration strictly exceeded the
    # named budget (re-derived from the evidence itself).
    code = decision.classification
    assert evidence["budget_ms"] is not None
    if code == build_reconciliation.CODE_QUEUE_WAIT_TIMEOUT:
        assert evidence["phase"] == "queue_wait"
        assert evidence["observed_ms"] > evidence["budget_ms"]
    elif code == build_reconciliation.CODE_PROVISIONING_TIMEOUT:
        assert evidence["phase"] == "provisioning"
        assert evidence["observed_ms"] > evidence["budget_ms"]
    else:
        assert code in _EXECUTION_TIMEOUT_CODES
        assert evidence["phase"] == "execution"
        assert evidence["execution_started_at"] is not None
        if code == build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED:
            assert evidence["budget_ms"] == evidence["hard_runtime_ms"]
            assert now > evidence["execution_started_at"] + \
                evidence["budget_ms"]
        elif code == build_reconciliation.CODE_AGENT_HEARTBEAT_EXPIRED:
            assert evidence["last_heartbeat_at"] is not None
            assert now > evidence["last_heartbeat_at"] + \
                evidence["budget_ms"]
        else:
            assert evidence["last_progress_at"] is not None
            assert now > evidence["last_progress_at"] + \
                evidence["budget_ms"]


@settings(max_examples=150, deadline=None)
@given(wait=st.integers(min_value=0, max_value=1000 * _MS_PER_HOUR),
       hours=_quarter_hours)
def test_queue_delay_never_diagnosed_as_execution_timeout(wait, hours):
    """For ANY queue delay — however far beyond the execution budget —
    a queued job is never classified with an execution-timeout code, so
    a queue problem can never justify an execution-timeout increase
    (Req 2.13, 2.14)."""
    job = {"build_job_id": "job-queue-label", "build_target": "AMD64",
           "execution_mode": "dedicated",
           "status": build_domain.STATUS_QUEUED,
           "created_at": _BASE_MS,
           "config_snapshot": {"max_runtime_hours": hours}}
    decision = build_reconciliation.decide_timing(job, _BASE_MS + wait)
    assert decision.classification not in _EXECUTION_TIMEOUT_CODES
    assert not decision.timed_out  # no queue budget was snapshotted
    assert decision.evidence["execution_runtime_ms"] == 0


def test_underspecified_label_alone_selects_no_remedy():
    """A terminal job carrying ONLY a generic timeout label (no timing
    evidence) yields no timeout, no observed duration, no budget — so
    neither a timeout-increase nor a queueing recommendation is
    derivable from the label alone; unavailable evidence is identified
    as None, never fabricated (Req 2.13, 2.18)."""
    job = {"build_job_id": "job-label-only", "build_target": "AMD64",
           "execution_mode": "dedicated",
           "status": build_domain.STATUS_FAILED,
           "error": ("Build_Job exceeded its maximum runtime of "
                     "4 hours (timeout)."),
           "config_snapshot": {}}
    decision = build_reconciliation.decide_timing(job, _BASE_MS)
    assert not decision.timed_out
    assert decision.evidence["phase"] == "terminal"
    assert decision.evidence["observed_ms"] is None
    assert decision.evidence["budget_ms"] is None
    for key in ("last_heartbeat_at", "last_progress_at",
                "execution_started_at"):
        assert decision.evidence[key] is None

    record = build_reconciliation.timeout_evidence_record(
        decision, _BASE_MS)
    assert record["timeout_kind"] is None
    # No recommendation surface exists anywhere in the record.
    for key in record:
        lowered = key.lower()
        assert "recommend" not in lowered
        assert "suggest" not in lowered
        assert "increase" not in lowered


def test_same_label_different_evidence_distinct_diagnosis():
    """Two failures that a label-only model would both call 'timeout'
    are separated by measured evidence: a queue-budget expiry names the
    queue phase and a hard-ceiling expiry names the execution phase —
    the remedy follows the EVIDENCE, not the label (Req 2.13)."""
    created = _BASE_MS
    snapshot = {"runtime_budgets": {"JP6": {"ephemeral": {
        "hard_runtime_hours": 2, "queue_wait_hours": 1}}}}

    queued = {"build_job_id": "job-q", "build_target": "JP6",
              "execution_mode": "ephemeral",
              "status": build_domain.STATUS_QUEUED,
              "created_at": created, "config_snapshot": snapshot}
    queue_expiry = build_reconciliation.decide_timing(
        queued, created + _MS_PER_HOUR + 1)

    running = _running_job("JP6", "ephemeral", snapshot, created)
    ceiling_expiry = build_reconciliation.decide_timing(
        running, created + 2 * _MS_PER_HOUR + 1)

    assert queue_expiry.timed_out and ceiling_expiry.timed_out
    assert queue_expiry.classification == \
        build_reconciliation.CODE_QUEUE_WAIT_TIMEOUT
    assert ceiling_expiry.classification == \
        build_reconciliation.CODE_MAX_RUNTIME_EXCEEDED
    assert queue_expiry.classification != ceiling_expiry.classification
    assert queue_expiry.evidence["phase"] == "queue_wait"
    assert ceiling_expiry.evidence["phase"] == "execution"
    # Each diagnosis is backed by its own measured duration.
    assert queue_expiry.evidence["observed_ms"] > \
        queue_expiry.evidence["budget_ms"]
    assert ceiling_expiry.evidence["observed_ms"] > \
        ceiling_expiry.evidence["budget_ms"]
