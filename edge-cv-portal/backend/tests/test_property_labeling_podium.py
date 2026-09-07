"""
Podium property tests: the shared Podium_Ranking pure function and the
DDA job detail payload's additive `podium` key.

Spec: labeling-job-cleanup-work-stealing-and-podium, task 1.7.

Two properties, 100 Hypothesis examples each
(`@settings(max_examples=100, deadline=None)`):

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 8:
Podium_Ranking is the total deterministic order** — *For any* multiset
of (user_id, submitted_at) submissions, `podium_ranking` SHALL emit
`min(3, distinct submitters)` entries with places 1..N in an order
sorted by descending count, then ascending final submission timestamp,
then ascending user id; each entry's count and final timestamp SHALL
equal the submitter's true aggregate; and any permutation of the input
SHALL produce the identical output.
**Validates: Requirements 7.1, 7.2, 7.3, 7.4**

Pure-function test against the shared layer's
`labeling_distribution.podium_ranking` (no moto). The oracle is an
independent reimplementation (per-user sum/max comprehensions), never
the implementation's own incremental aggregation. Count ties and
timestamp ties are forced by drawing user ids from a 5-name pool and
timestamps from a small range; free-text user ids exercise the
residual user-id tie-break on unusual orderings.

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 9:
The detail payload carries the podium exactly for Completed team jobs,
emails joined per membership** — *For any* job state (status, team
presence, backend, skip-verification) and any membership subset of the
submitters, the DDA job detail payload SHALL carry a `podium` key
exactly when the job is a Completed DDA team job — its entries equal
to the shared oracle with `email` present on an entry exactly when
that submitter is a current team member — and every non-qualifying
job's payload SHALL carry no podium key.
**Validates: Requirements 7.5, 8.2, 10.4**

Moto-backed, through the real GET /labeling/{id} handler path (the
test_labeling_backend_switch.py scaffolding: real labeling module
imported inside the mock, jobs/tasks/teams seeded directly in
DynamoDB, synthetic API Gateway events with admin-shaped Cognito
claims). The entry oracle is the shared
`labeling_distribution.podium_ranking` itself (its own correctness is
Property 8's subject) plus the membership email join computed in the
test.

Generated job states respect the shipped structural invariants —
skip-verification jobs have no team (their items are `AUTO` result
rows), Ground Truth jobs have no team and no task items, Completed
team jobs hold no unsubmitted active tasks, and every Submitted task
carries `submitted_by`/`submitted_at` — so the property quantifies
over the reachable input space:

- backend x status x (team | skip-verification) job kinds;
- Submitted (user, timestamp) populations with forced count and
  timestamp ties;
- membership subsets of the submitters (a False flag = a departed
  submitter) plus non-submitting current members;
- non-Submitted decoy tasks (Assigned / PresentationFailed on live
  jobs, Inactive everywhere) that must never reach the podium.

Harness reuse (Hypothesis cannot consume function-scoped fixtures):
the module-scoped `labeling` fixture follows
test_labeling_backend_switch.py; per-example environments (fresh
Use_Case + job + team, uuid-isolated) are built inside the test body.
"""
import json
import sys
import uuid

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# Pure shared-layer module (no boto3): safe to import at collection
# time, before any moto mock is active.
from labeling_distribution import podium_ranking

REGION = "us-east-1"


# ===========================================================================
# Property 8: Podium_Ranking is the total deterministic order
# ===========================================================================

# Small pools force count ties and timestamp ties organically;
# free-text ids exercise the user-id tie-break on arbitrary orderings.
_POOL_USER_IDS = ["user-a", "user-b", "user-c", "user-d", "user-e"]
_user_ids = st.one_of(
    st.sampled_from(_POOL_USER_IDS),
    st.text(min_size=1, max_size=8),
)
_timestamps = st.one_of(
    st.integers(min_value=0, max_value=6),          # tie-heavy
    st.integers(min_value=0, max_value=10**12),     # realistic epochs
)


@st.composite
def _submission_multisets(draw):
    """(submissions, permuted): a submission multiset and one of its
    permutations, both as lists of (user_id, submitted_at) tuples."""
    submissions = draw(st.lists(
        st.tuples(_user_ids, _timestamps), min_size=0, max_size=20))
    permuted = draw(st.permutations(submissions))
    return submissions, permuted


def _expected_podium(submissions):
    """Independent Podium_Ranking oracle (Req 7.1-7.4), restated with
    per-user sum/max comprehensions rather than the implementation's
    incremental aggregation: rank by (-count, final_ts, user_id), take
    three, place = rank index + 1."""
    users = {user_id for user_id, _ in submissions}
    aggregates = {
        user_id: (
            sum(1 for uid, _ in submissions if uid == user_id),
            max(ts for uid, ts in submissions if uid == user_id),
        )
        for user_id in users
    }
    ranked = sorted(
        users,
        key=lambda uid: (-aggregates[uid][0], aggregates[uid][1], uid))
    return [
        {
            "place": place,
            "user_id": user_id,
            "submitted": aggregates[user_id][0],
            "final_submitted_at": aggregates[user_id][1],
        }
        for place, user_id in enumerate(ranked[:3], start=1)
    ]


class TestProperty8PodiumRankingTotalOrder:
    @settings(max_examples=100, deadline=None)
    @given(case=_submission_multisets())
    @example(case=(  # full tie (count and final ts) -> user-id order
        [("user-b", 5), ("user-a", 5)],
        [("user-a", 5), ("user-b", 5)],
    ))
    @example(case=(  # count tie -> earlier final submission first
        [("user-a", 3), ("user-a", 9), ("user-b", 5), ("user-b", 3)],
        [("user-b", 3), ("user-b", 5), ("user-a", 9), ("user-a", 3)],
    ))
    @example(case=(  # 4 submitters -> cap at 3; mixed tie-breaks
        [("user-d", 0), ("user-c", 1), ("user-b", 1), ("user-b", 2),
         ("user-a", 0), ("user-a", 3)],
        [("user-a", 3), ("user-a", 0), ("user-b", 2), ("user-b", 1),
         ("user-c", 1), ("user-d", 0)],
    ))
    @example(case=(  # single submitter, duplicate timestamps
        [("user-e", 1), ("user-e", 1), ("user-e", 0)],
        [("user-e", 0), ("user-e", 1), ("user-e", 1)],
    ))
    @example(case=([], []))  # empty in -> empty out
    def test_property_podium_ranking_total_order(self, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 8: Podium_Ranking is the total deterministic order —
        *For any* multiset of (user_id, submitted_at) submissions,
        `podium_ranking` SHALL emit `min(3, distinct submitters)`
        entries with places 1..N in an order sorted by descending
        count, then ascending final submission timestamp, then
        ascending user id; each entry's count and final timestamp SHALL
        equal the submitter's true aggregate; and any permutation of
        the input SHALL produce the identical output.

        **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
        """
        submissions, permuted = case

        result = podium_ranking(list(submissions))

        # Shape: min(3, distinct submitters) entries, places 1..N.
        distinct = len({user_id for user_id, _ in submissions})
        assert len(result) == min(3, distinct)
        assert [entry["place"] for entry in result] == list(
            range(1, len(result) + 1))

        # Order: sorted by (-count, final_ts asc, user_id asc) —
        # spelled out on the emitted entries themselves.
        sort_keys = [
            (-entry["submitted"], entry["final_submitted_at"],
             entry["user_id"])
            for entry in result
        ]
        assert sort_keys == sorted(sort_keys)

        # Aggregates and full entry equality against the independent
        # oracle (covers Req 7.1 counts, 7.2 final-timestamp tie-break,
        # 7.3 user-id tie-break, 7.4 cap/places).
        assert result == _expected_podium(submissions)

        # Determinism under input permutation (Req 7.3's totality).
        assert podium_ranking(list(permuted)) == result


# ===========================================================================
# Property 9: The detail payload carries the podium exactly for
# Completed team jobs, emails joined per membership
# ===========================================================================

@pytest.fixture(scope="module")
def labeling(aws_stack):
    """The real labeling module imported inside the moto mock."""
    sys.modules.pop("labeling", None)
    import labeling

    return labeling


class PodiumEnv:
    """Per-example seeding + invocation facade with a fresh Use_Case id
    (the test_labeling_backend_switch.py LabelingEnv shape, reduced to
    what the job detail path needs)."""

    def __init__(self, stack, labeling):
        self.stack = stack
        self.labeling = labeling
        self.usecase_id = f"uc-{uuid.uuid4()}"
        user_id = f"admin-{uuid.uuid4()}"
        self.user = {
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "username": user_id,
            "role": "DataScientist",
        }

    # ------------------------------------------------------------ setup
    def put_job(self, **attrs):
        job_id = f"labeling-{uuid.uuid4().hex[:10]}"
        item = {
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": "podium property job",
            "created_at": 1,
        }
        item.update(attrs)
        self.stack.tables.labeling_jobs.put_item(Item=item)
        return job_id

    def put_task(self, job_id, index, assignee, status, **attrs):
        item = {
            "job_id": job_id,
            "task_id": f"task-{index:06d}",
            "usecase_id": self.usecase_id,
            "assignee_user_id": assignee,
            "status": status,
        }
        item.update(attrs)
        self.stack.tables.labeling_tasks.put_item(Item=item)

    def put_team(self, member_ids):
        """Team META + one MEMBER# item per current member; member
        emails follow the shipped `{user_id}@example.com` convention."""
        team_id = f"team-{uuid.uuid4().hex[:10]}"
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": team_id, "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Podium Team", "created_at": 1,
        })
        for user_id in member_ids:
            self.stack.tables.labeling_teams.put_item(Item={
                "team_id": team_id, "sk": f"MEMBER#{user_id}",
                "user_id": user_id,
                "email": f"{user_id}@example.com",
                "added_at": 1,
            })
        return team_id

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
# A scenario is a plain dict:
# - kind: 'team' (DDA team job), 'skip' (DDA Skip_Verification_Mode, no
#   team), or 'groundtruth' (SageMaker-managed, no team, no task items);
# - status: job status across InProgress / Completed / Failed / Stopped;
# - submissions: [(user_index 0..4, submitted_at)] -> one Submitted task
#   each, attributed via submitted_by/submitted_at ('team' kind only);
# - member_flags: current-membership flag per pool user 0..4 — a
#   submitter whose flag is False is a departed member (entry without
#   email); flags of non-submitters add zero-progress current members;
# - extra_members: 0..2 non-submitting current members (user-5, user-6);
# - other_tasks: [(status, assignee_index)] decoy non-Submitted tasks,
#   assignee_index -1 = UNASSIGNED — Inactive-only on Completed jobs
#   (a Completed job holds no unsubmitted active tasks);
# - auto_items: AUTO result items ('skip' kind only).

_JOB_STATUSES = ["InProgress", "Completed", "Failed", "Stopped"]


def _scenario(kind, status, submissions=(), member_flags=(False,) * 5,
              extra_members=0, other_tasks=(), auto_items=0):
    return {
        "kind": kind,
        "status": status,
        "submissions": list(submissions),
        "member_flags": list(member_flags),
        "extra_members": extra_members,
        "other_tasks": list(other_tasks),
        "auto_items": auto_items,
    }


@st.composite
def _detail_scenarios(draw):
    kind = draw(st.sampled_from(["team", "team", "team", "skip",
                                 "groundtruth"]))
    status = draw(st.sampled_from(_JOB_STATUSES))
    if kind == "groundtruth":
        return _scenario("groundtruth", status)
    if kind == "skip":
        return _scenario(
            "skip", status,
            auto_items=draw(st.integers(min_value=0, max_value=3)))
    # DDA team job. Small user pool + small timestamp range force
    # count ties and timestamp ties in the ranked population.
    submissions = draw(st.lists(
        st.tuples(st.integers(min_value=0, max_value=4),
                  st.integers(min_value=0, max_value=30)),
        min_size=0, max_size=12))
    member_flags = draw(st.lists(st.booleans(), min_size=5, max_size=5))
    extra_members = draw(st.integers(min_value=0, max_value=2))
    decoy_statuses = (
        ["Inactive"] if status == "Completed"
        else ["Assigned", "PresentationFailed", "Inactive"])
    other_tasks = draw(st.lists(
        st.tuples(st.sampled_from(decoy_statuses),
                  st.integers(min_value=-1, max_value=4)),
        min_size=0, max_size=4))
    return _scenario("team", status, submissions=submissions,
                     member_flags=member_flags,
                     extra_members=extra_members, other_tasks=other_tasks)


def _seed_scenario(env, scenario):
    """Materialize a scenario in the moto tables; returns the job id
    and the current-member user ids."""
    job_attrs = {"status": scenario["status"]}
    members = []

    if scenario["kind"] == "groundtruth":
        job_attrs["labeling_backend"] = "GroundTruth"
        job_attrs["image_count"] = 2
        return env.put_job(**job_attrs), members

    job_attrs["labeling_backend"] = "DDA"
    job_attrs["task_type"] = "Classification"

    if scenario["kind"] == "skip":
        job_attrs["skip_verification"] = True
        job_attrs["image_count"] = max(1, scenario["auto_items"])
        job_attrs["autolabel_completed_count"] = scenario["auto_items"]
        job_id = env.put_job(**job_attrs)
        for index in range(scenario["auto_items"]):
            env.put_task(job_id, index, "AUTO", "Assigned",
                         prelabel_status="Available")
        return job_id, members

    # DDA team job.
    members = [f"user-{i}" for i, flag in enumerate(scenario["member_flags"])
               if flag]
    members += [f"user-{5 + j}" for j in range(scenario["extra_members"])]
    job_attrs["team_id"] = env.put_team(members)
    job_attrs["image_count"] = (
        len(scenario["submissions"]) + len(scenario["other_tasks"]))
    job_id = env.put_job(**job_attrs)

    index = 0
    for user_index, submitted_at in scenario["submissions"]:
        user_id = f"user-{user_index}"
        env.put_task(job_id, index, user_id, "Submitted",
                     submitted_by=user_id, submitted_at=submitted_at)
        index += 1
    for decoy_status, assignee_index in scenario["other_tasks"]:
        assignee = ("UNASSIGNED" if assignee_index < 0
                    else f"user-{assignee_index}")
        env.put_task(job_id, index, assignee, decoy_status)
        index += 1
    return job_id, members


class TestProperty9DetailPayloadPodium:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(scenario=_detail_scenarios())
    @example(scenario=_scenario(  # the qualifying job: Completed team,
        # count ties + timestamp tie, a departed submitter (user-1),
        # a zero-progress current member (user-5), Inactive decoys
        "team", "Completed",
        submissions=[(0, 10), (0, 12), (1, 8), (1, 12), (2, 12), (3, 5)],
        member_flags=[True, False, True, True, False],
        extra_members=1,
        other_tasks=[("Inactive", 4), ("Inactive", -1)]))
    @example(scenario=_scenario(  # same population, job not Completed
        "team", "InProgress",
        submissions=[(0, 10), (1, 8), (2, 12)],
        member_flags=[True, True, True, False, False],
        other_tasks=[("Assigned", 3), ("PresentationFailed", -1)]))
    @example(scenario=_scenario(  # Completed team job, zero submitters
        "team", "Completed", member_flags=[True, False, False, False,
                                           False]))
    @example(scenario=_scenario(  # Completed Skip_Verification_Mode
        "skip", "Completed", auto_items=2))
    @example(scenario=_scenario(  # Completed Ground Truth
        "groundtruth", "Completed"))
    def test_property_detail_payload_podium(
            self, aws_stack, labeling, scenario):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 9: The detail payload carries the podium exactly for
        Completed team jobs, emails joined per membership — *For any*
        job state (status, team presence, backend, skip-verification)
        and any membership subset of the submitters, the DDA job detail
        payload SHALL carry a `podium` key exactly when the job is a
        Completed DDA team job — its entries equal to the shared oracle
        with `email` present on an entry exactly when that submitter is
        a current team member — and every non-qualifying job's payload
        SHALL carry no podium key.

        **Validates: Requirements 7.5, 8.2, 10.4**
        """
        env = PodiumEnv(aws_stack, labeling)
        job_id, members = _seed_scenario(env, scenario)

        status_code, response = env.get_job(job_id)
        assert status_code == 200, response
        job = response["job"]

        qualifies = (scenario["kind"] == "team"
                     and scenario["status"] == "Completed")
        if not qualifies:
            # Non-team, non-Completed, Ground Truth, and
            # Skip_Verification_Mode payloads carry no podium key —
            # the key is strictly additive (Req 8.2, 10.4).
            assert "podium" not in job
            return

        # Entries equal the shared oracle over the job's Submitted
        # (submitted_by, submitted_at) pairs, with the email joined
        # exactly for current members (Req 7.5, 8.2). podium_ranking's
        # own correctness is Property 8's subject.
        pairs = [(f"user-{user_index}", submitted_at)
                 for user_index, submitted_at in scenario["submissions"]]
        expected = podium_ranking(pairs)
        member_emails = {user_id: f"{user_id}@example.com"
                         for user_id in members}
        for entry in expected:
            email = member_emails.get(entry["user_id"])
            if email:
                entry["email"] = email

        assert job["podium"] == expected

        # Email presence iff current membership, spelled out entry by
        # entry (Req 7.5).
        for entry in job["podium"]:
            assert ("email" in entry) == (entry["user_id"] in member_emails)
