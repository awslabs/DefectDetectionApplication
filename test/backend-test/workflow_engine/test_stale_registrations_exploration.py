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
"""Bug-condition exploration tests (Task 1, device half) for
stale-workflow-registrations.

Property 1: Bug Condition — Stale Versions Are Retired and Cleaned
(device engine half, Property 1(b)/(c)/(d)).

**These tests assert the FIXED (post-fix) watcher/API behavior, so they
are EXPECTED TO FAIL on the UNFIXED tree.** Each failure is the
counterexample confirming the bug: ``WorkflowWatcher.sync_once`` registers
EVERY ``{workflowId}/{version}`` directory it finds as an active
``registered`` row with no concept of supersession,
``_invalidate_removed`` marks vanished directories ``invalid`` (still
listed) instead of ``removed``, and ``GET /workflows/registrations``
returns every row unconditionally — so the deployed-workflows view lists
every version ever deployed and lets stale versions be triggered.

Expected counterexamples on the UNFIXED tree (the verified JP6 case):
    - dirs 2/, 6/, 7/ for one workflow -> rows wf-1:2, wf-1:6, wf-1:7 all
      status='registered'; the default listing has length 3;
    - a deleted version directory -> status 'invalid' (not 'removed') and
      still present in the default listing;
    - a trigger against a stale (superseded) version returns 200 and
      creates an execution instead of being rejected 409.

The SAME tests are re-run in task 5.1 against the fixed watcher/API
(lower numeric versions -> 'superseded', vanished dirs -> 'removed',
default listing filtered to active statuses, 409 on stale triggers),
where they must PASS.

Harness: workflow_engine_test_utils (make_session_factory, make_watcher,
write_artifact_set) plus the standalone-FastAPI-app api pattern from
test_workflow_engine_api.py.

**Validates: Requirements 1.2, 1.3, 1.4** (expected behavior 2.2, 2.3,
2.4, 2.5)
"""
import shutil
import tempfile
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine import api as workflow_engine_api
from workflow_engine.models import WorkflowExecution, WorkflowRegistration

WORKFLOW_ID = "wf-1"

#: The statuses that count as "active" (deployed-version) registrations
#: per expected behavior 2.4 — everything else must be filtered from the
#: default listing.
ACTIVE_STATUSES = ("registered", "invalid")

#: The new non-active statuses the fix introduces (expected behavior
#: 2.2 / 2.3).
STATUS_REMOVED = "removed"
STATUS_SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_client(session_factory, watcher=None):
    """A standalone FastAPI app holding only the workflow engine router,
    bound to the test session factory (test_workflow_engine_api.py
    pattern). When a watcher is supplied, ``runtime.invalid_reason`` is
    patched through to it so non-active statuses surface their reasons."""
    app = FastAPI()
    app.include_router(workflow_engine_api.router)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[workflow_engine_api.get_db] = override_get_db
    return TestClient(app)


def sync_versions(root, session_factory, versions):
    """Write one artifact set per version under ``root`` and run one
    watcher scan. Returns the watcher (for reasons / later rescans)."""
    for version in versions:
        write_artifact_set(root, workflow_id=WORKFLOW_ID, version=str(version))
    watcher = make_watcher(root, session_factory)
    watcher.sync_once()
    return watcher


def rows_by_id(session_factory):
    session = session_factory()
    try:
        return {
            row.id: row.status
            for row in session.query(WorkflowRegistration).all()
        }
    finally:
        session.close()


def default_listing(session_factory, watcher):
    with patch(
        "workflow_engine.runtime.invalid_reason",
        side_effect=watcher.invalid_reason,
    ):
        with make_client(session_factory) as client:
            response = client.get("/workflows/registrations")
    assert response.status_code == 200
    return response.json()


def reg_id(version):
    return "{0}:{1}".format(WORKFLOW_ID, version)


# ---------------------------------------------------------------------------
# Case 1: the verified JP6 failing case — dirs 2/, 6/, 7/ for one workflow
# (Requirements 1.2, 1.3 / expected behavior 2.3, 2.4)
# ---------------------------------------------------------------------------


class TestVerifiedJp6MultiVersionCase:
    def test_only_the_highest_numeric_version_is_active(self):
        """Dirs 2/, 6/, 7/ for one workflow: after one scan only wf-1:7
        may hold an active status; wf-1:2 and wf-1:6 must be 'superseded'
        (rows preserved, never deleted).

        EXPECTED FAILURE on the unfixed tree: all three rows are
        status='registered' — the watcher registers every on-disk version
        as active with no concept of supersession (the live JP6 state:
        component v7.0.0 deployed, versions 2/6/7 all 'registered').

        Validates: Requirements 1.2 (expected behavior 2.3)
        """
        root = tempfile.mkdtemp(prefix="stale_regs_jp6_")
        try:
            session_factory = make_session_factory()
            sync_versions(root, session_factory, [2, 6, 7])

            statuses = rows_by_id(session_factory)
            # Rows are preserved for all three versions (never deleted).
            assert set(statuses) == {reg_id(2), reg_id(6), reg_id(7)}

            assert statuses[reg_id(7)] == "registered"
            for stale in (reg_id(2), reg_id(6)):
                assert statuses[stale] == STATUS_SUPERSEDED, (
                    "COUNTEREXAMPLE (Req 1.2): {0} has status {1!r} — the "
                    "watcher registered a superseded on-disk version as "
                    "active; full rows: {2!r} (the verified JP6 state: "
                    "wf:2/6/7 all 'registered' while only v7 is "
                    "deployed)".format(stale, statuses[stale], statuses))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_default_listing_omits_stale_versions(self):
        """GET /workflows/registrations must by default return only the
        active registration (version 7), omitting superseded versions.

        EXPECTED FAILURE on the unfixed tree: the listing endpoint has no
        status filter, so all three versions come back (listing length 3)
        and the deployed-workflows view lists workflows that are not
        actually deployed.

        Validates: Requirements 1.3 (expected behavior 2.4)
        """
        root = tempfile.mkdtemp(prefix="stale_regs_listing_")
        try:
            session_factory = make_session_factory()
            watcher = sync_versions(root, session_factory, [2, 6, 7])

            body = default_listing(session_factory, watcher)
            listed = [item["registrationId"] for item in body]
            assert listed == [reg_id(7)], (
                "COUNTEREXAMPLE (Req 1.3): the default listing returned "
                "{0!r} (length {1}) — stale versions 2 and 6 appear "
                "alongside the deployed version 7 (expected exactly "
                "['wf-1:7'])".format(listed, len(listed)))
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case 2: Hypothesis-generated numeric version sets
# (Requirements 1.2, 1.3 / expected behavior 2.3, 2.4)
# ---------------------------------------------------------------------------


class TestMultiVersionSupersessionProperty:
    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(versions=st.sets(st.integers(min_value=1, max_value=99),
                            min_size=2, max_size=5))
    def test_exactly_the_highest_version_is_active_and_listed(self, versions):
        """For ANY set of >= 2 numeric version directories of one workflow,
        after one scan exactly the highest numeric version holds an active
        status, every lower version is 'superseded', and the default
        listing contains only the active registration.

        EXPECTED FAILURE on the unfixed tree: every version registers as
        'registered' and the unfiltered listing returns them all.

        Validates: Requirements 1.2, 1.3 (expected behavior 2.3, 2.4)
        """
        root = tempfile.mkdtemp(prefix="stale_regs_prop_")
        try:
            session_factory = make_session_factory()
            watcher = sync_versions(root, session_factory, sorted(versions))
            highest = max(versions)

            statuses = rows_by_id(session_factory)
            assert set(statuses) == {reg_id(v) for v in versions}

            active = sorted(
                rid for rid, status in statuses.items()
                if status in ACTIVE_STATUSES)
            assert active == [reg_id(highest)], (
                "COUNTEREXAMPLE (Req 1.2): versions {0!r} on disk -> active "
                "registrations {1!r}; expected only the highest numeric "
                "version {2!r} to be active (full statuses: {3!r})".format(
                    sorted(versions), active, reg_id(highest), statuses))
            for version in versions - {highest}:
                assert statuses[reg_id(version)] == STATUS_SUPERSEDED, (
                    "COUNTEREXAMPLE (Req 1.2): {0} has status {1!r}, "
                    "expected 'superseded'".format(
                        reg_id(version), statuses[reg_id(version)]))

            listed = [item["registrationId"]
                      for item in default_listing(session_factory, watcher)]
            assert listed == [reg_id(highest)], (
                "COUNTEREXAMPLE (Req 1.3): versions {0!r} on disk -> the "
                "default listing returned {1!r}; expected only "
                "{2!r}".format(sorted(versions), listed, reg_id(highest)))
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case 3: a deleted artifact directory retires the registration as
# 'removed' and drops it from the default listing
# (Requirement 1.4 / expected behavior 2.2, 2.4)
# ---------------------------------------------------------------------------


class TestRemovedDirectoryRetirement:
    def test_deleted_directory_yields_removed_status_and_is_omitted(self):
        """Register one version, delete its directory (what the fixed
        recipe's Shutdown produces on replace/remove), rescan: the row
        must flip to status 'removed' — a distinct non-active status —
        and vanish from the default listing (row preserved in the DB).

        EXPECTED FAILURE on the unfixed tree: ``_invalidate_removed``
        marks the row 'invalid' ("Artifact directory was removed") and
        the unfiltered listing keeps returning it indefinitely,
        indistinguishable from a genuinely broken deployed artifact set.

        Validates: Requirements 1.4 (expected behavior 2.2, 2.4)
        """
        root = tempfile.mkdtemp(prefix="stale_regs_removed_")
        try:
            session_factory = make_session_factory()
            version_dir = write_artifact_set(
                root, workflow_id=WORKFLOW_ID, version="3")
            watcher = make_watcher(root, session_factory)
            watcher.sync_once()
            assert rows_by_id(session_factory) == {reg_id(3): "registered"}

            shutil.rmtree(version_dir)
            watcher.sync_once()

            statuses = rows_by_id(session_factory)
            # The row itself is preserved (execution history retention).
            assert reg_id(3) in statuses
            assert statuses[reg_id(3)] == STATUS_REMOVED, (
                "COUNTEREXAMPLE (Req 1.4): after its artifact directory "
                "was deleted, {0} has status {1!r} — expected the distinct "
                "non-active status 'removed', not 'invalid'".format(
                    reg_id(3), statuses[reg_id(3)]))

            listed = [item["registrationId"]
                      for item in default_listing(session_factory, watcher)]
            assert listed == [], (
                "COUNTEREXAMPLE (Req 1.4): the default listing still "
                "returns {0!r} after the artifact directory was removed — "
                "retired registrations must be omitted by default".format(
                    listed))
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case 4: triggering a stale (superseded) version is rejected 409
# (Requirement 1.3 / expected behavior 2.5)
# ---------------------------------------------------------------------------


class TestStaleTriggerRejected:
    def test_trigger_on_superseded_version_is_rejected_409(self):
        """With dirs 2/ and 7/ on disk, a trigger against wf-1:2 (stale)
        must be rejected 409 — the same non-runnable guard that protects
        'invalid' registrations — and create no execution.

        EXPECTED FAILURE on the unfixed tree: wf-1:2 is 'registered', so
        the trigger returns 200 and creates a pending execution — an
        operator can run a workflow version that is not actually deployed.

        Validates: Requirements 1.3 (expected behavior 2.5)
        """
        root = tempfile.mkdtemp(prefix="stale_regs_trigger_")
        try:
            session_factory = make_session_factory()
            watcher = sync_versions(root, session_factory, [2, 7])

            with patch(
                "workflow_engine.runtime.invalid_reason",
                side_effect=watcher.invalid_reason,
            ):
                with make_client(session_factory) as client:
                    response = client.post(
                        "/workflows/registrations/{0}/trigger".format(
                            reg_id(2)))

            assert response.status_code == 409, (
                "COUNTEREXAMPLE (Req 1.3 / expected 2.5): triggering the "
                "stale registration {0} returned {1} ({2!r}) — expected "
                "409; stale versions are runnable on the unfixed "
                "tree".format(reg_id(2), response.status_code,
                              response.json()))

            session = session_factory()
            try:
                assert session.query(WorkflowExecution).count() == 0, (
                    "COUNTEREXAMPLE (Req 1.3 / expected 2.5): the stale "
                    "trigger created an execution row")
            finally:
                session.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)
