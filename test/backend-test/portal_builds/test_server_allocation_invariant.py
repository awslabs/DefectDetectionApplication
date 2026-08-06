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
Property test for the server allocation invariant in
``edge-cv-portal/backend/functions/build_planner.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates

**Validates: Requirements 7.1, 7.2, 2.2**

The expected allocation semantics are restated here independently of the
implementation: for any sequence of dispatch ticks and job completions
over any set of dedicated Build_Jobs and Dedicated_Build_Servers, at
every step each server has at most one Build_Job in a running state
(building/publishing) allocated to it (Req 7.1); a job dispatched to a
server that already holds an allocation is placed in that server's queue
with the ``queued`` status instead of starting (Req 7.2); and every
dedicated dispatch decision targets exactly the Dedicated_Build_Server
selected in the job's build request — never a substitute (Req 2.2).
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Import the pure planner/domain modules from the portal Lambda bundle.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

import build_domain  # noqa: E402
import build_planner  # noqa: E402

# Statuses that mean "running on a server" for the allocation invariant.
_RUNNING_STATUSES = frozenset({
    build_domain.STATUS_BUILDING,
    build_domain.STATUS_PUBLISHING,
})

_TERMINAL = st.sampled_from(sorted(build_domain.TERMINAL_STATUSES))


@st.composite
def _fleet_and_jobs(draw):
    """Generate a fleet of servers, a set of dedicated Build_Jobs, and a
    sequence of operations to replay against the planner.

    Some servers start pre-occupied by a running job (the seed state is
    consistent: the occupying job exists with a running status and the
    server's ``running_build_job_id`` points at it). Every other job
    starts queued and targets an arbitrary server of the fleet — a few
    target a server id absent from the fleet to cover the unknown-server
    case. Operations are dispatch ticks and completions of a running job
    with an arbitrary terminal status.
    """
    n_servers = draw(st.integers(min_value=1, max_value=4))
    server_ids = [f"server-{i}" for i in range(n_servers)]
    servers = [{"server_id": sid, "running_build_job_id": None} for sid in server_ids]

    jobs = []
    counter = 0

    # Seed: some servers already hold a running job.
    for server in servers:
        if draw(st.booleans()):
            job_id = f"seed-job-{counter}"
            jobs.append({
                "build_job_id": job_id,
                "predecessor_job_id": None,
                "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
                "server_id": server["server_id"],
                "status": draw(st.sampled_from(sorted(_RUNNING_STATUSES))),
                "created_at": counter,
            })
            server["running_build_job_id"] = job_id
            counter += 1

    # Queued dedicated jobs targeting fleet servers (or, rarely, an
    # unknown server id the planner must skip).
    n_jobs = draw(st.integers(min_value=0, max_value=10))
    for _ in range(n_jobs):
        target = draw(st.sampled_from(server_ids + ["server-unknown"]))
        jobs.append({
            "build_job_id": f"job-{counter}",
            "predecessor_job_id": None,
            "execution_mode": build_domain.EXECUTION_MODE_DEDICATED,
            "server_id": target,
            "status": build_domain.STATUS_QUEUED,
            "created_at": counter,
        })
        counter += 1

    # Operation sequence: dispatch ticks interleaved with completions.
    # A completion carries an index selector (resolved modulo the running
    # set at replay time) and a terminal status.
    ops = draw(st.lists(
        st.one_of(
            st.just(("tick",)),
            st.tuples(st.just("complete"), st.integers(min_value=0), _TERMINAL),
        ),
        min_size=1,
        max_size=12,
    ))
    return servers, jobs, ops


def _assert_invariant(servers, jobs):
    """Req 7.1: at most one running Build_Job allocated per server, and
    each server's ``running_build_job_id`` is consistent with job state."""
    running_by_server = {}
    for job in jobs:
        if job["status"] in _RUNNING_STATUSES and job.get("server_id"):
            running_by_server.setdefault(job["server_id"], []).append(
                job["build_job_id"]
            )
    for server in servers:
        allocated = running_by_server.get(server["server_id"], [])
        assert len(allocated) <= 1, (
            f"server {server['server_id']} has {len(allocated)} running "
            f"jobs allocated: {allocated}"
        )
        if allocated:
            assert server["running_build_job_id"] == allocated[0]
        else:
            assert server["running_build_job_id"] is None


# Feature: portal-build-fleet-and-workflow-gates, Property 4: Server allocation never exceeds one job per server
@settings(max_examples=200)
@given(state=_fleet_and_jobs())
def test_server_allocation_never_exceeds_one_job_per_server(state):
    """For any sequence of dispatch ticks and completions over any set of
    dedicated Build_Jobs and Dedicated_Build_Servers, each server holds at
    most one running job at every step (Req 7.1); dispatch against an
    occupied server queues the job with the queued status (Req 7.2); and
    every decision targets exactly the server selected in the job's
    request (Req 2.2)."""
    servers, jobs, ops = state
    jobs_by_id = {job["build_job_id"]: job for job in jobs}
    servers_by_id = {server["server_id"]: server for server in servers}

    _assert_invariant(servers, jobs)

    for op in ops:
        if op[0] == "tick":
            # Snapshot occupancy before the plan to check each decision.
            occupied_before = {
                s["server_id"]: s["running_build_job_id"] for s in servers
            }
            decisions = build_planner.plan_dedicated_dispatch(jobs, servers)

            started_servers = set()
            for decision in decisions:
                job = jobs_by_id[decision.build_job_id]

                # Req 2.2: the decision targets exactly the server the
                # job's build request selected.
                assert decision.server_id == job["server_id"], (
                    f"job {job['build_job_id']} selected "
                    f"{job['server_id']} but was planned onto "
                    f"{decision.server_id}"
                )
                # Only known fleet servers may be planned.
                assert decision.server_id in servers_by_id

                if decision.action == build_planner.ALLOCATION_START:
                    # Req 7.1: the slot must have been free, and no other
                    # start in this plan may have taken it.
                    assert occupied_before[decision.server_id] is None, (
                        f"job {decision.build_job_id} started on occupied "
                        f"server {decision.server_id}"
                    )
                    assert decision.server_id not in started_servers, (
                        f"two jobs started on server {decision.server_id} "
                        f"in one plan"
                    )
                    started_servers.add(decision.server_id)
                    # Apply: the job starts running on its server.
                    job["status"] = build_domain.STATUS_BUILDING
                    servers_by_id[decision.server_id][
                        "running_build_job_id"
                    ] = decision.build_job_id
                else:
                    # Req 7.2: occupied server -> the job waits in that
                    # server's queue with the queued status.
                    assert decision.action == build_planner.ALLOCATION_QUEUE
                    assert (
                        occupied_before[decision.server_id] is not None
                        or decision.server_id in started_servers
                    ), (
                        f"job {decision.build_job_id} queued against free "
                        f"server {decision.server_id}"
                    )
                    assert decision.status == build_domain.STATUS_QUEUED
                    assert job["status"] == build_domain.STATUS_QUEUED

        else:
            # Complete a running job with an arbitrary terminal status.
            _, selector, terminal_status = op
            running = sorted(
                (j for j in jobs if j["status"] in _RUNNING_STATUSES),
                key=lambda j: j["build_job_id"],
            )
            if not running:
                continue
            job = running[selector % len(running)]
            job["status"] = terminal_status
            server = servers_by_id.get(job.get("server_id"))
            if server is not None and (
                server["running_build_job_id"] == job["build_job_id"]
            ):
                server["running_build_job_id"] = None
                # Promotion (Req 7.3 machinery): the freed slot may be
                # handed to the oldest queued job for that server.
                if build_planner.should_promote(terminal_status):
                    promoted = build_planner.promote_next(
                        server["server_id"], jobs
                    )
                    if promoted is not None:
                        assert promoted["status"] == build_domain.STATUS_QUEUED
                        assert promoted["server_id"] == server["server_id"]
                        promoted["status"] = build_domain.STATUS_BUILDING
                        server["running_build_job_id"] = promoted["build_job_id"]

        # Req 7.1: the invariant holds after every applied step.
        _assert_invariant(servers, jobs)
