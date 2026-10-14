"""
Work-stealing and labeler-pool property tests.

Spec: labeling-job-cleanup-work-stealing-and-podium, task 1.6.

Three properties over the real `steal_labeler_task` and
`get_labeler_job_pool` routes in dda_labeling.py — driven through the
module handler's router so the real @rbac_check, membership re-check,
and conditional-write paths run — against the moto-backed stack from
conftest.py (the LabelerEnv scaffolding of
test_dda_labeling_labeler_apis.py rebuilt per Hypothesis example:
uuid-isolated Use_Case + team + members + job + tasks). 100 examples
per property:

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 5:
A Steal_Request succeeds exactly when a Stealable_Task exists and
takes the Steal_Order minimum** — *For any* generated task population
(statuses across Assigned/Submitted/PresentationFailed/Inactive,
assignees across the caller, teammates, UNASSIGNED and AUTO, prelabel
states including Pending) and any job status, POST steal SHALL answer
200 exactly when the job is InProgress and at least one Stealable_Task
exists — transferring precisely the reference Steal_Order's first
candidate to the caller with stolen_from/stolen_at recorded and the
remaining count reported — and SHALL otherwise answer 409 with every
assignment unchanged.
**Validates: Requirements 5.1, 5.2, 5.5, 5.6**

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 6:
Steal races have one winner and never move protected tasks** — *For
any* task population and any injected concurrent mutation sequence
(submissions, competing steals, and reassignments landing between
candidate selection and the conditional write), the losing write SHALL
move to the next Steal_Order candidate, no task SHALL ever hold two
assignees (the caller gains exactly the one won task), and every
Submitted, PresentationFailed, Inactive, Pending-prelabel, and AUTO
task SHALL end byte-identical to its pre-race state.
**Validates: Requirements 5.3, 5.4**

**Feature: labeling-job-cleanup-work-stealing-and-podium, Property 7:
The Pool_Route reports the exact pool state** — *For any* generated
task population, GET pool SHALL answer `stealable_count` equal to the
reference Stealable_Task count (zero for non-InProgress jobs),
`job_complete` true exactly when all active tasks are Submitted with a
positive image_count, and a `podium` key present exactly when
`job_complete` holds — its entries equal to the shared
`podium_ranking` oracle's output with emails joined for current team
members only.
**Validates: Requirements 6.1**

Oracles
-------
- The Stealable_Task predicate and the Steal_Order are restated in
  this file, never imported from the code under test: status Assigned,
  assignee neither the caller nor AUTO, prelabel_status not Pending
  (UNASSIGNED tasks qualify); UNASSIGNED tasks first in ascending
  task_id, then Donors by (-stealable_count, user_id), ascending
  task_id within each Donor.
- The podium oracle is the shared `labeling_distribution.podium_ranking`
  itself (the design requires the Pool_Route to consume exactly that
  shared pure function), with the Req 7.5 email join restated over the
  membership this test seeded.

Races (Property 6): a proxy wrapped around the module's real tasks
table intercepts exactly the route's conditional steal writes and
flips each planned candidate (concurrent submission / competing steal
/ membership reassignment) immediately BEFORE delegating the write —
so the candidate's condition fails precisely the way a lost race
fails, and the walk's next-candidate behavior is observed end to end,
store included. The proxy also records the attempted candidate order,
pinning the walk to the reference Steal_Order.

Harness reuse (Hypothesis cannot consume function-scoped fixtures):
the module-scoped `dda` fixture follows
test_dda_labeling_labeler_apis.py; per-example environments are built
inside the test bodies.
"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import Key
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Pure shared-layer function (typing-only imports): safe at collection
# time, and the design names it as the Pool_Route's one ranking source.
from labeling_distribution import podium_ranking

REGION = "us-east-1"
DATASET_BUCKET = "test-steal-prop-dataset"  # name only; no S3 I/O here

UNASSIGNED = "UNASSIGNED"
AUTO = "AUTO"

PROTECTED_STATUSES = ("Submitted", "PresentationFailed", "Inactive")


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dda(aws_stack):
    """The real dda_labeling module imported inside the moto mock (the
    labeler-apis scaffolding). The steal and pool routes touch DynamoDB
    only — no fake Lambda or S3 seeding is needed."""
    sys.modules.pop("dda_labeling", None)
    import dda_labeling

    return SimpleNamespace(module=dda_labeling)


class StealPoolEnv:
    """Per-example seeding + invocation facade: a fresh Use_Case, one
    team, the caller plus 1-3 teammates as current members, one
    departed user (never a member), and the two Property 6 race actors
    (rival stealer, reassignment target) as members."""

    def __init__(self, stack, dda, teammate_count):
        self.stack = stack
        self.dda = dda
        self.usecase_id = f"uc-{uuid.uuid4()}"
        stack.tables.usecases.put_item(Item={
            "usecase_id": self.usecase_id,
            "name": "Work Stealing Property Test",
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:root",
            "s3_bucket": DATASET_BUCKET,
        })
        self.team_id = f"team-{uuid.uuid4()}"
        stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": "META",
            "usecase_id": self.usecase_id,
            "team_name": "Steal Property Team",
            "created_at": 1,
            "created_by": "admin",
        })
        caller_id = f"labeler-{uuid.uuid4().hex[:8]}"
        self.caller = {"user_id": caller_id,
                       "email": f"{caller_id}@example.com"}
        self.teammates = [f"tm{i}-{uuid.uuid4().hex[:8]}"
                          for i in range(teammate_count)]
        self.departed = f"departed-{uuid.uuid4().hex[:8]}"
        self.rival = f"rival-{uuid.uuid4().hex[:8]}"
        self.spare = f"spare-{uuid.uuid4().hex[:8]}"
        self.member_emails = {}
        for member in [caller_id, *self.teammates, self.rival, self.spare]:
            self.add_member(member)

    # ------------------------------------------------------------ setup
    def add_member(self, user_id):
        self.member_emails[user_id] = f"{user_id}@example.com"
        self.stack.tables.labeling_teams.put_item(Item={
            "team_id": self.team_id,
            "sk": f"MEMBER#{user_id}",
            "user_id": user_id,
            "email": f"{user_id}@example.com",
            "added_at": 1,
            "added_by": "admin",
        })

    def resolve(self, symbol):
        """Concrete assignee for a generated symbolic one."""
        if symbol == "caller":
            return self.caller["user_id"]
        if symbol.startswith("tm"):
            return self.teammates[int(symbol[2:])]
        if symbol == "departed":
            return self.departed
        return symbol  # UNASSIGNED / AUTO sentinels

    def seed_job(self, status, image_count):
        job_id = f"labeling-{uuid.uuid4().hex[:8]}"
        self.stack.tables.labeling_jobs.put_item(Item={
            "job_id": job_id,
            "usecase_id": self.usecase_id,
            "job_name": f"job-{job_id}",
            "labeling_backend": "DDA",
            "status": status,
            "task_type": "Classification",
            "label_set": ["normal", "anomaly"],
            "dataset_bucket": DATASET_BUCKET,
            "team_id": self.team_id,
            "image_count": image_count,
            "created_at": 1,
        })
        return job_id

    def seed_tasks(self, job_id, specs):
        """Write one task item per spec; returns the written plain-dict
        items (pre-Decimal), the Property 7 podium-pair source."""
        written = []
        for spec in specs:
            assignee = self.resolve(spec.assignee)
            image_key = f"images/{job_id}/{spec.task_id}.jpg"
            item = {
                "job_id": job_id,
                "task_id": spec.task_id,
                "image_s3_uri": f"s3://{DATASET_BUCKET}/{image_key}",
                "image_key": image_key,
                "usecase_id": self.usecase_id,
                "assignee_user_id": assignee,
                "status": spec.status,
                "created_at": 1,
            }
            if spec.prelabel != "absent":
                item["prelabel_status"] = spec.prelabel
            if spec.status == "Submitted":
                # A Submitted item always carries its submitter — the
                # submit route writes both, and skip-verification AUTO
                # result items record the sentinel.
                if spec.assignee == "AUTO":
                    item["submitted_by"] = AUTO
                elif spec.assignee == "UNASSIGNED":
                    item["submitted_by"] = self.departed
                else:
                    item["submitted_by"] = assignee
                item["submitted_at"] = spec.submitted_at
            self.stack.tables.labeling_tasks.put_item(Item=item)
            written.append(item)
        return written

    # ------------------------------------------------------------ invoke
    def _event(self, method, resource, path_params):
        path = resource
        for key, value in path_params.items():
            path = path.replace("{" + key + "}", value)
        return {
            "httpMethod": method,
            "resource": resource,
            "path": path,
            "pathParameters": path_params,
            "queryStringParameters": None,
            "body": None,
            "requestContext": {
                "authorizer": {
                    "claims": {
                        "sub": self.caller["user_id"],
                        "email": self.caller["email"],
                        "cognito:username": self.caller["user_id"],
                        "custom:role": "DataLabeler",
                    }
                }
            },
        }

    def steal(self, job_id):
        response = self.dda.module.handler(
            self._event("POST", "/labeler/jobs/{jobId}/steal",
                        {"jobId": job_id}), None)
        return response["statusCode"], json.loads(response["body"])

    def pool(self, job_id):
        response = self.dda.module.handler(
            self._event("GET", "/labeler/jobs/{jobId}/pool",
                        {"jobId": job_id}), None)
        return response["statusCode"], json.loads(response["body"])

    # ------------------------------------------------------------- store
    def tasks_by_id(self, job_id):
        items = self.stack.tables.labeling_tasks.query(
            KeyConditionExpression=Key("job_id").eq(job_id),
        ).get("Items", [])
        return {item["task_id"]: item for item in items}


class RacingTasksTable:
    """A proxy around the real tasks table that injects a concurrent
    mutation between the route's candidate selection and its
    conditional steal write: when the route's steal write arrives for a
    planned candidate, the plan's mutation is applied first (through
    the real table), so the delegated conditional write fails exactly
    the way a lost race fails. Every other call delegates untouched.
    `steal_attempts` records the candidate order the route walked."""

    _SABOTAGE_AT = 1_699_999_999

    def __init__(self, real_table, plan, rival, spare):
        self._real = real_table
        self._plan = plan          # task_id -> (kind, donor)
        self._rival = rival
        self._spare = spare
        self._applied = set()
        self.steal_attempts = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def update_item(self, **kwargs):
        values = kwargs.get("ExpressionAttributeValues") or {}
        update = kwargs.get("UpdateExpression") or ""
        if ":caller" in values and "stolen_from" in update:
            task_id = kwargs["Key"]["task_id"]
            self.steal_attempts.append(task_id)
            planned = self._plan.get(task_id)
            if planned and task_id not in self._applied:
                self._applied.add(task_id)
                self._flip(kwargs["Key"], *planned)
        return self._real.update_item(**kwargs)

    def _flip(self, key, kind, donor):
        if kind == "submit":       # a concurrent submission won
            self._real.update_item(
                Key=key,
                UpdateExpression="SET #status = :submitted, "
                                 "submitted_by = :by, "
                                 "submitted_at = :at, updated_at = :at",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":submitted": "Submitted",
                    ":by": donor,
                    ":at": self._SABOTAGE_AT,
                })
        elif kind == "steal":      # a competing stealer won
            self._real.update_item(
                Key=key,
                UpdateExpression="SET assignee_user_id = :rival, "
                                 "stolen_from = :donor, "
                                 "stolen_at = :at, updated_at = :at",
                ExpressionAttributeValues={
                    ":rival": self._rival,
                    ":donor": donor,
                    ":at": self._SABOTAGE_AT,
                })
        else:                      # a membership reassignment landed
            self._real.update_item(
                Key=key,
                UpdateExpression="SET assignee_user_id = :spare, "
                                 "updated_at = :at",
                ExpressionAttributeValues={
                    ":spare": self._spare,
                    ":at": self._SABOTAGE_AT,
                })


# ----------------------------------------------------------------- oracles

def _stealable_reference(tasks, caller):
    """The Stealable_Task predicate restated (Req 5.4): status
    Assigned, assignee neither the caller nor AUTO (UNASSIGNED
    qualifies), prelabel_status not Pending."""
    return [task for task in tasks
            if task.get("status") == "Assigned"
            and task.get("assignee_user_id")
            and task.get("assignee_user_id") not in (caller, AUTO)
            and task.get("prelabel_status") != "Pending"]


def _steal_order_reference(tasks, caller):
    """The Steal_Order restated (Req 5.2): UNASSIGNED tasks first in
    ascending task_id; then Donors by (-stealable_count, user_id),
    ascending task_id within each Donor."""
    stealable = _stealable_reference(tasks, caller)
    unassigned = sorted(
        (task for task in stealable
         if task["assignee_user_id"] == UNASSIGNED),
        key=lambda task: task["task_id"])
    donors = {}
    for task in stealable:
        if task["assignee_user_id"] == UNASSIGNED:
            continue
        donors.setdefault(task["assignee_user_id"], []).append(task)
    ordered = list(unassigned)
    for donor in sorted(donors, key=lambda d: (-len(donors[d]), d)):
        ordered.extend(sorted(donors[donor],
                              key=lambda task: task["task_id"]))
    return ordered


def _is_protected(item):
    """Req 5.4's never-reassign set: Submitted / PresentationFailed /
    Inactive status, a Pending prelabel, or the AUTO sentinel."""
    return (item.get("status") in PROTECTED_STATUSES
            or item.get("prelabel_status") == "Pending"
            or item.get("assignee_user_id") == AUTO)


# -------------------------------------------------------------- generators

_WIDE_TS = st.integers(min_value=1_700_000_000, max_value=1_700_000_400)
# Narrow range + small submitter pool force count and timestamp ties.
_NARROW_TS = st.integers(min_value=1_700_000_000, max_value=1_700_000_004)

_TASK_STATUS = st.sampled_from(
    ("Assigned", "Assigned", "Assigned", "Submitted", "Submitted",
     "PresentationFailed", "Inactive"))
_PRELABEL = st.sampled_from(
    ("absent", "absent", "absent", "Pending", "Available", "Failed"))


@st.composite
def _populations(draw, min_tasks=0, max_tasks=8, extra_stealable_max=0,
                 all_submitted=False, narrow_timestamps=False):
    """A task population over symbolic assignees (resolved against the
    per-example env at seeding time). `extra_stealable_max` > 0 appends
    1..N guaranteed-Stealable tasks; `all_submitted` builds the
    completed-team-job shape. task_ids are a drawn permutation so the
    Steal_Order's ascending-id rule is exercised across positions."""
    teammate_count = draw(st.integers(min_value=1, max_value=3))
    member_symbols = ["caller"] + [f"tm{i}" for i in range(teammate_count)]
    ts_strategy = _NARROW_TS if narrow_timestamps else _WIDE_TS
    specs = []
    for _ in range(draw(st.integers(min_value=min_tasks,
                                    max_value=max_tasks))):
        if all_submitted:
            status = "Submitted"
            assignee = draw(st.sampled_from(member_symbols + ["departed"]))
        else:
            status = draw(_TASK_STATUS)
            assignee = draw(st.sampled_from(
                member_symbols + [UNASSIGNED, AUTO, "departed"]))
        specs.append(SimpleNamespace(
            status=status,
            assignee=assignee,
            prelabel=draw(_PRELABEL),
            submitted_at=(draw(ts_strategy)
                          if status == "Submitted" else None)))
    if extra_stealable_max:
        for _ in range(draw(st.integers(min_value=1,
                                        max_value=extra_stealable_max))):
            specs.append(SimpleNamespace(
                status="Assigned",
                assignee=draw(st.sampled_from(
                    [f"tm{i}" for i in range(teammate_count)]
                    + [UNASSIGNED])),
                prelabel=draw(st.sampled_from(
                    ("absent", "Available", "Failed"))),
                submitted_at=None))
    if specs:
        ids = draw(st.permutations(range(len(specs))))
        for spec, number in zip(specs, ids):
            spec.task_id = f"task-{number:07d}"
    return SimpleNamespace(teammate_count=teammate_count, specs=specs)


@st.composite
def _steal_cases(draw):
    """Property 5: any job status × any population, weighted so the
    200 branch (InProgress with >=1 Stealable_Task) stays hot."""
    return SimpleNamespace(
        job_status=draw(st.sampled_from(
            ("InProgress", "InProgress", "InProgress",
             "Completed", "Stopped", "Failed", "Deleting"))),
        population=draw(_populations(
            extra_stealable_max=draw(st.sampled_from((3, 3, 0))))))


@st.composite
def _race_cases(draw):
    """Property 6: an InProgress job guaranteed >=1 Stealable_Task, a
    sabotage budget (or full exhaustion), and per-candidate mutation
    kinds."""
    return SimpleNamespace(
        population=draw(_populations(extra_stealable_max=4)),
        sabotage_all=draw(st.sampled_from((False, False, False, True))),
        budget=draw(st.integers(min_value=0, max_value=12)),
        kinds=draw(st.lists(
            st.sampled_from(("submit", "steal", "reassign")),
            min_size=12, max_size=12)))


@st.composite
def _pool_cases(draw):
    """Property 7: free populations plus a dedicated completed shape
    (all tasks Submitted by a small member pool over a narrow timestamp
    range — count ties, timestamp ties, >3 submitters)."""
    if draw(st.sampled_from(("free", "free", "complete"))) == "complete":
        population = draw(_populations(min_tasks=1, all_submitted=True,
                                       narrow_timestamps=True))
    else:
        population = draw(_populations())
    return SimpleNamespace(
        job_status=draw(st.sampled_from(
            ("InProgress", "InProgress", "Completed", "Stopped"))),
        population=population)


# =========================================================================== #
# Property 5
# =========================================================================== #

class TestProperty5StealTakesTheStealOrderMinimum:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_steal_cases())
    def test_property_steal_succeeds_iff_stealable_and_takes_minimum(
            self, aws_stack, dda, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 5: A Steal_Request succeeds exactly when a
        Stealable_Task exists and takes the Steal_Order minimum — *For
        any* generated task population and any job status, POST steal
        SHALL answer 200 exactly when the job is InProgress and at
        least one Stealable_Task exists, transferring precisely the
        reference Steal_Order's first candidate to the caller with
        stolen_from/stolen_at recorded and the remaining count
        reported; otherwise 409 with every assignment unchanged.

        **Validates: Requirements 5.1, 5.2, 5.5, 5.6**
        """
        env = StealPoolEnv(aws_stack, dda, case.population.teammate_count)
        caller = env.caller["user_id"]
        active = [spec for spec in case.population.specs
                  if spec.status != "Inactive"]
        job_id = env.seed_job(case.job_status, image_count=len(active))
        env.seed_tasks(job_id, case.population.specs)

        pre = env.tasks_by_id(job_id)
        order = _steal_order_reference(pre.values(), caller)

        status, body = env.steal(job_id)
        post = env.tasks_by_id(job_id)

        if case.job_status == "InProgress" and order:
            # ---- 200 exactly when InProgress with a Stealable_Task --
            assert status == 200, (
                f"steal rejected ({status}) with candidates "
                f"{[t['task_id'] for t in order]}: {body!r}")
            winner = order[0]
            assert body["task_id"] == winner["task_id"]
            assert body["job_id"] == job_id
            assert body["stolen_from"] == winner["assignee_user_id"]
            # Remaining count after the transfer (Req 5.1).
            assert body["stealable_count"] == len(order) - 1

            # The store moved exactly the Steal_Order minimum, with
            # provenance recorded in the same write (Req 5.2, 5.8's
            # attributes as observable transfer evidence).
            won = post[winner["task_id"]]
            assert won["assignee_user_id"] == caller
            assert won["stolen_from"] == winner["assignee_user_id"]
            assert int(won["stolen_at"]) > 0
            assert won["stolen_at"] == won["updated_at"]
            assert won["status"] == "Assigned"
            provenance = ("assignee_user_id", "stolen_from", "stolen_at",
                          "updated_at")
            assert ({key: value for key, value in won.items()
                     if key not in provenance}
                    == {key: value
                        for key, value in pre[winner["task_id"]].items()
                        if key not in provenance})

            # Exactly one assignment changed: every other task item is
            # byte-identical.
            for task_id, item in post.items():
                if task_id != winner["task_id"]:
                    assert item == pre[task_id], (
                        f"untouched task {task_id} changed")
        else:
            # ---- otherwise 409 with every assignment unchanged ------
            assert status == 409, (
                f"expected 409 (status={case.job_status}, "
                f"candidates={len(order)}), got {status}: {body!r}")
            if case.job_status != "InProgress":
                # Req 5.6: the answer names the job's status.
                assert body["status"] == case.job_status
                assert case.job_status in body["error"]
            else:
                # Req 5.5: zero Stealable_Tasks remain.
                assert body["error"] == "No stealable tasks remain"
                assert body["stealable_count"] == 0
            assert post == pre, "a 409 answer mutated an assignment"


# =========================================================================== #
# Property 6
# =========================================================================== #

class TestProperty6StealRacesHaveOneWinner:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_race_cases())
    def test_property_races_one_winner_protected_tasks_unmoved(
            self, aws_stack, dda, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 6: Steal races have one winner and never move
        protected tasks — *For any* task population and any injected
        concurrent mutation sequence (submissions, competing steals,
        reassignments landing between candidate selection and the
        conditional write), the losing write SHALL move to the next
        Steal_Order candidate, no task SHALL ever hold two assignees
        (the caller gains exactly the one won task), and every
        Submitted, PresentationFailed, Inactive, Pending-prelabel, and
        AUTO task SHALL end byte-identical to its pre-race state.

        **Validates: Requirements 5.3, 5.4**
        """
        env = StealPoolEnv(aws_stack, dda, case.population.teammate_count)
        caller = env.caller["user_id"]
        active = [spec for spec in case.population.specs
                  if spec.status != "Inactive"]
        job_id = env.seed_job("InProgress", image_count=len(active))
        env.seed_tasks(job_id, case.population.specs)

        pre = env.tasks_by_id(job_id)
        order = _steal_order_reference(pre.values(), caller)
        assert order, "race generator must yield >=1 Stealable_Task"

        # Sabotage the first K Steal_Order candidates; an unowned
        # (UNASSIGNED) candidate cannot be concurrently submitted, so
        # its mutation falls back to a competing steal.
        k = len(order) if case.sabotage_all else min(case.budget,
                                                     len(order))
        plan = {}
        for index in range(k):
            candidate = order[index]
            kind = case.kinds[index]
            if (candidate["assignee_user_id"] == UNASSIGNED
                    and kind == "submit"):
                kind = "steal"
            plan[candidate["task_id"]] = (kind,
                                          candidate["assignee_user_id"])
        protected_ids = {task_id for task_id, item in pre.items()
                         if _is_protected(item)}
        assert not set(plan) & protected_ids  # candidates never protected

        original = dda.module.labeling_tasks_table
        proxy = RacingTasksTable(original, plan, env.rival, env.spare)
        dda.module.labeling_tasks_table = proxy
        try:
            status, body = env.steal(job_id)
        finally:
            dda.module.labeling_tasks_table = original
        post = env.tasks_by_id(job_id)

        if k < len(order):
            # ---- the losing writes moved to the next candidate ------
            winner = order[k]
            assert status == 200, (
                f"expected the walk to win candidate {k}: {body!r}")
            assert body["task_id"] == winner["task_id"]
            assert body["stolen_from"] == winner["assignee_user_id"]
            assert body["stealable_count"] == len(order) - k - 1
            won = post[winner["task_id"]]
            assert won["assignee_user_id"] == caller
            assert won["stolen_from"] == winner["assignee_user_id"]
            assert won["status"] == "Assigned"
            won_ids = {winner["task_id"]}
            attempted = [task["task_id"] for task in order[:k + 1]]
        else:
            # Every candidate lost its race: 409, nothing acquired.
            assert status == 409, f"expected exhaustion 409: {body!r}"
            assert body["error"] == "No stealable tasks remain"
            assert body["stealable_count"] == 0
            won_ids = set()
            attempted = [task["task_id"] for task in order]

        # The walk followed the reference Steal_Order exactly, one
        # conditional write per candidate (Req 5.3).
        assert proxy.steal_attempts == attempted

        # One winner: the caller gained exactly the won task; two
        # writers never both acquire one task.
        gained = sorted(task_id for task_id, item in post.items()
                        if item.get("assignee_user_id") == caller
                        and pre[task_id].get("assignee_user_id") != caller)
        assert gained == sorted(won_ids)

        for task_id, item in post.items():
            if task_id in won_ids:
                continue
            if task_id in plan:
                # A lost candidate holds exactly its concurrent
                # mutation — never the caller.
                kind, donor = plan[task_id]
                assert item["assignee_user_id"] != caller
                if kind == "submit":
                    assert item["status"] == "Submitted"
                    assert item["assignee_user_id"] == donor
                elif kind == "steal":
                    assert item["status"] == "Assigned"
                    assert item["assignee_user_id"] == env.rival
                else:
                    assert item["status"] == "Assigned"
                    assert item["assignee_user_id"] == env.spare
                continue
            assert item == pre[task_id], (
                f"task {task_id} untouched by the race was mutated")

        # Req 5.4 restated: every pre-race protected task ended
        # byte-identical.
        for task_id in protected_ids:
            assert post[task_id] == pre[task_id], (
                f"protected task {task_id} was moved")


# =========================================================================== #
# Property 7
# =========================================================================== #

class TestProperty7PoolRouteReportsExactState:
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(case=_pool_cases())
    def test_property_pool_reports_exact_state(
            self, aws_stack, dda, case):
        """Feature: labeling-job-cleanup-work-stealing-and-podium,
        Property 7: The Pool_Route reports the exact pool state — *For
        any* generated task population, GET pool SHALL answer
        `stealable_count` equal to the reference Stealable_Task count
        (zero for non-InProgress jobs), `job_complete` true exactly
        when all active tasks are Submitted with a positive
        image_count, and a `podium` key present exactly when
        `job_complete` holds — its entries equal to the shared
        `podium_ranking` oracle's output, emails joined for current
        team members only.

        **Validates: Requirements 6.1**
        """
        env = StealPoolEnv(aws_stack, dda, case.population.teammate_count)
        caller = env.caller["user_id"]
        active = [spec for spec in case.population.specs
                  if spec.status != "Inactive"]
        # The distribute invariant: one task per image, so image_count
        # is the active task count (0 for an empty population).
        job_id = env.seed_job(case.job_status, image_count=len(active))
        written = env.seed_tasks(job_id, case.population.specs)

        status, body = env.pool(job_id)
        assert status == 200, f"pool rejected ({status}): {body!r}"

        expected_stealable = (
            len(_stealable_reference(written, caller))
            if case.job_status == "InProgress" else 0)
        expected_complete = bool(active) and all(
            spec.status == "Submitted" for spec in active)

        assert body["job_id"] == job_id
        assert body["stealable_count"] == expected_stealable
        assert body["job_complete"] is expected_complete

        if expected_complete:
            # The podium key rides along exactly when Job_Complete,
            # computed by the one shared ranking (the oracle import)
            # over the Submitted tasks' (submitted_by, submitted_at)
            # pairs, with emails for current members only (Req 7.5).
            expected_podium = podium_ranking(
                (item["submitted_by"], item["submitted_at"])
                for item in written
                if item["status"] == "Submitted")
            for entry in expected_podium:
                email = env.member_emails.get(entry["user_id"])
                if email:
                    entry["email"] = email
            assert body["podium"] == expected_podium
            assert set(body) == {"job_id", "stealable_count",
                                 "job_complete", "podium"}
        else:
            assert set(body) == {"job_id", "stealable_count",
                                 "job_complete"}
