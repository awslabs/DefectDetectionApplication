"""
Failure_Reason_Summary property test for the DDA job detail
(labeling.py `_get_dda_labeling_job`, driven through the real
GET /labeling/{id} handler path).

Spec: grounded-sam-prompt-guardrails-and-prelabel-retry, task 3.2.

One property against the moto-backed stack from conftest.py (the
test_labeling_backend_switch.py scaffolding: real labeling module
imported inside the mock, jobs/tasks seeded directly in DynamoDB,
synthetic API Gateway events with Cognito claims), 100 Hypothesis
examples:

**Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
Property 3: Failure_Reason_Summary is the capped descending
distinct-count aggregation, present iff failures exist** — *For any*
task population (statuses mixing Assigned/Submitted/Inactive,
`prelabel_status` mixing Failed/Available/Pending/absent, and
`prelabel_error` values mixing arbitrary strings with duplicates across
tasks, the empty string, and absence), the job detail SHALL carry
`prelabel_failure_reasons` exactly when at least one active Failed task
exists, equal to the distinct error values of active Failed tasks
(absent errors as `'unknown'`) with their counts, in descending count
order, capped at 5 — and the summed counts of the uncapped aggregation
SHALL equal `prelabel_failed_count`.
**Validates: Requirements 4.1, 4.4**

Oracle (restated here, never imported from the code under test)
----------------------------------------------------------------
- *Active* task: `status != 'Inactive'` — Inactive tasks never
  contribute, whatever their `prelabel_status` (the same filter
  `prelabel_failed_count` applies).
- *Failed* task: active task with `prelabel_status == 'Failed'`.
  Non-Failed tasks never contribute their `prelabel_error`, even when
  (adversarially) they carry one.
- *Reason* of a Failed task: `task.get('prelabel_error') or 'unknown'`
  — the implementation note of task 3.1: an absent error AND an empty
  string both bucket under `'unknown'`, merging with any literal
  `'unknown'` reason.
- *Entries*: one per distinct reason with its occurrence count, ordered
  by descending count with ties broken by first occurrence in task
  order (task order = ascending `task_id`, the DynamoDB query order),
  capped at 5 distinct reasons.
- *Presence*: the `prelabel_failure_reasons` key exists iff at least
  one active Failed task exists; a zero-failed response carries NO such
  key (Req 4.4's byte-identical guarantee).
- *Conservation*: the uncapped counts sum to exactly
  `prelabel_failed_count` (both aggregate over the same active/Failed
  population, each task under exactly one reason).

Harness reuse (Hypothesis cannot consume function-scoped fixtures): the
module-scoped `labeling` fixture follows test_labeling_backend_switch.py;
per-example environments (fresh Use_Case + job + tasks, uuid-isolated)
are built inside the test body.
"""
import json
import sys
import uuid

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def labeling(aws_stack):
    """The real labeling module imported inside the moto mock."""
    sys.modules.pop("labeling", None)
    import labeling

    return labeling


class SummaryEnv:
    """Per-example seeding + invocation facade with a fresh Use_Case id
    (the test_labeling_backend_switch.py LabelingEnv shape, reduced to
    what the job detail path needs)."""

    def __init__(self, stack, labeling):
        self.stack = stack
        self.labeling = labeling
        self.usecase_id = f"uc-{uuid.uuid4()}"
        user_id = f"user-{uuid.uuid4()}"
        self.user = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "DataScientist",
        }

    # ------------------------------------------------------------ setup
    def put_job(self, image_count):
        job_id = f"labeling-{uuid.uuid4().hex[:10]}"
        self.stack.tables.labeling_jobs.put_item(Item={
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": "failure summary property job",
            "labeling_backend": "DDA",
            "status": "InProgress",
            "task_type": "Segmentation",
            "label_set": ["cookie_gap"],
            "image_count": image_count,
            "auto_label": {"enabled": True, "model": "grounded-sam"},
            "created_at": 1,
        })
        return job_id

    def put_task(self, job_id, index, status, prelabel_status, error):
        """One task item; `prelabel_status` None = attribute absent,
        `error` None = attribute absent (an empty string is stored).
        task_id carries the seed index, so ascending-task_id query order
        (the implementation's task order) equals seed order."""
        item = {
            "job_id": job_id,
            "task_id": f"task-{index:06d}",
            "usecase_id": self.usecase_id,
            "assignee_user_id": f"labeler-{self.usecase_id}",
            "status": status,
            "created_at": 1,
        }
        if prelabel_status is not None:
            item["prelabel_status"] = prelabel_status
        if error is not None:
            item["prelabel_error"] = error
        self.stack.tables.labeling_tasks.put_item(Item=item)

    # ------------------------------------------------------------ invoke
    def get_job(self, job_id):
        """GET /labeling/{id} through the real handler."""
        event = {
            "httpMethod": "GET",
            "resource": "/labeling/{id}",
            "path": f"/v1/labeling/{job_id}",
            "pathParameters": {"id": job_id},
            "queryStringParameters": None,
            "body": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": self.user["user_id"],
                        "email": self.user["email"],
                        "cognito:username": self.user["username"],
                        "custom:role": self.user["role"],
                    }
                }
            },
        }
        response = self.labeling.handler(event, None)
        return response["statusCode"], json.loads(response["body"])


# -------------------------------------------------------------- generators
#
# A population is (pool, specs):
# - pool: distinct reason strings shared across tasks so duplicates occur
#   (may include the literal 'unknown', which must merge with the
#   absent/empty fallback bucket);
# - specs: one (status, prelabel_status, (error_kind, pool_index)) per
#   task, in task order. error_kind 'pool' -> pool[index % len(pool)],
#   'empty' -> the empty string (stored), 'absent' -> no attribute; the
#   error spec applies to every status so non-Failed tasks adversarially
#   carrying errors are generated too.

_REASON_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF,
                                 blacklist_categories=("Cs",))
_reason_text = st.text(alphabet=_REASON_ALPHABET, min_size=1, max_size=50)

_pools = st.lists(st.one_of(_reason_text, st.just("unknown")),
                  min_size=1, max_size=8, unique=True)

# 'Failed' weighted so populations with many Failed tasks (and >5
# distinct reasons, exercising the cap) arise organically.
_prelabel_statuses = st.sampled_from(
    ["Failed", "Failed", "Failed", "Available", "Pending", "absent"])
_statuses = st.sampled_from(["Assigned", "Submitted", "Inactive"])
_error_specs = st.tuples(
    st.sampled_from(["pool", "pool", "pool", "empty", "absent"]),
    st.integers(min_value=0, max_value=7))

_task_specs = st.lists(
    st.tuples(_statuses, _prelabel_statuses, _error_specs),
    min_size=0, max_size=16)


def _resolve_error(error_spec, pool):
    """The stored `prelabel_error` for a spec: a pool reason, the empty
    string, or None (attribute absent)."""
    kind, index = error_spec
    if kind == "pool":
        return pool[index % len(pool)]
    if kind == "empty":
        return ""
    return None


def _expected_summary(specs, pool):
    """The oracle: (expected entries or None when the key must be
    absent, uncapped sum, active Failed count) over the population in
    task order."""
    counts = {}
    first_occurrence = {}
    failed_count = 0
    for position, (status, prelabel_status, error_spec) in enumerate(specs):
        if status == "Inactive" or prelabel_status != "Failed":
            continue
        failed_count += 1
        stored = _resolve_error(error_spec, pool)
        # Task 3.1's documented fallback: absent OR empty -> 'unknown'.
        reason = stored or "unknown"
        if reason not in first_occurrence:
            first_occurrence[reason] = position
        counts[reason] = counts.get(reason, 0) + 1
    if failed_count == 0:
        return None, 0, 0
    ordered = sorted(counts,
                     key=lambda reason: (-counts[reason],
                                         first_occurrence[reason]))
    entries = [{"reason": reason, "count": counts[reason]}
               for reason in ordered[:5]]
    return entries, sum(counts.values()), failed_count


# =========================================================================== #
# Property 3: Failure_Reason_Summary is the capped descending
# distinct-count aggregation, present iff failures exist
# =========================================================================== #

class TestProperty3FailureReasonSummary:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(pool=_pools, specs=_task_specs)
    @example(  # 7 distinct reasons -> cap at 5; ties (count 1) keep
               # first-occurrence order; Inactive Failed and a non-Failed
               # task carrying an error are both excluded
        pool=["r0", "r1", "r2", "r3", "r4", "r5", "r6"],
        specs=[
            ("Assigned", "Failed", ("pool", 1)),
            ("Submitted", "Failed", ("pool", 0)),
            ("Assigned", "Failed", ("pool", 0)),
            ("Assigned", "Failed", ("pool", 2)),
            ("Assigned", "Failed", ("pool", 3)),
            ("Submitted", "Failed", ("pool", 4)),
            ("Assigned", "Failed", ("pool", 5)),
            ("Assigned", "Failed", ("pool", 6)),
            ("Inactive", "Failed", ("pool", 0)),
            ("Assigned", "Available", ("pool", 2)),
        ])
    @example(  # zero ACTIVE Failed tasks (the only Failed one is
               # Inactive) -> no key at all (Req 4.4)
        pool=["boom"],
        specs=[
            ("Inactive", "Failed", ("pool", 0)),
            ("Assigned", "Available", ("pool", 0)),
            ("Submitted", "Pending", ("empty", 0)),
            ("Assigned", "absent", ("absent", 0)),
        ])
    @example(  # absent error, empty-string error, and the literal
               # 'unknown' reason all merge into one 'unknown' bucket
        pool=["unknown", "real reason"],
        specs=[
            ("Assigned", "Failed", ("empty", 0)),
            ("Submitted", "Failed", ("absent", 0)),
            ("Assigned", "Failed", ("pool", 0)),
            ("Assigned", "Failed", ("pool", 1)),
        ])
    @example(pool=["x"], specs=[])  # empty population -> no key
    def test_property_failure_reason_summary(
            self, aws_stack, labeling, pool, specs):
        """Feature: grounded-sam-prompt-guardrails-and-prelabel-retry,
        Property 3: Failure_Reason_Summary is the capped descending
        distinct-count aggregation, present iff failures exist — *For
        any* task population (statuses x prelabel statuses x error
        values with duplicates, the empty string, and absences), the job
        detail SHALL carry `prelabel_failure_reasons` exactly when at
        least one active Failed task exists, equal to the distinct error
        values of active Failed tasks (absent errors as 'unknown') with
        their counts, in descending count order (ties by first
        occurrence in task order), capped at 5 — and the summed counts
        of the uncapped aggregation SHALL equal `prelabel_failed_count`.

        **Validates: Requirements 4.1, 4.4**
        """
        env = SummaryEnv(aws_stack, labeling)
        job_id = env.put_job(image_count=len(specs))
        for index, (status, prelabel_status, error_spec) in enumerate(specs):
            env.put_task(
                job_id, index, status,
                None if prelabel_status == "absent" else prelabel_status,
                _resolve_error(error_spec, pool))

        status_code, response = env.get_job(job_id)
        assert status_code == 200, response
        job = response["job"]

        expected_entries, uncapped_sum, failed_count = _expected_summary(
            specs, pool)

        # Conservation: the response's failed count equals the oracle's
        # uncapped aggregation sum (each active Failed task lands in
        # exactly one reason bucket).
        assert job["prelabel_failed_count"] == uncapped_sum == failed_count

        if expected_entries is None:
            # Presence iff failures exist: a zero-failed response has NO
            # such key (Req 4.4).
            assert "prelabel_failure_reasons" not in job
        else:
            # Entries, counts, order (descending count, ties by first
            # occurrence in task order), and the cap at 5 — exact list
            # equality.
            assert job["prelabel_failure_reasons"] == expected_entries
            assert len(job["prelabel_failure_reasons"]) <= 5
