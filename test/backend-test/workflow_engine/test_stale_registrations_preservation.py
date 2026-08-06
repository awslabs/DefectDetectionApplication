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
"""Preservation property tests (Task 2, device half) for
stale-workflow-registrations.

**Property 2: Preservation — Deployed Version and Recipe Contract
Unchanged (device engine half)**

**Validates: Requirements 3.1, 3.3, 3.4, 3.5, 3.6**

Observation-first: the assertions below record the behavior OBSERVED on
the UNFIXED watcher/API for inputs where the bug condition does NOT hold
(single-version-per-workflow layouts, no vanished directories except in
the remove-then-readd sequence). They must PASS on the unfixed tree
(baseline) and KEEP passing after the fix (tasks 3/4), because none of
these inputs involve supersession or the default-listing filter's
non-active statuses:

- a single valid version registers as ``registered`` with the exact
  payload keys/values (registrationId, workflowId, name, version, arch,
  artifactPath, status, registeredAt) and is listed (3.1);
- malformed/incompatible artifact sets for the (only, hence deployed)
  version register as ``invalid`` with the observed reason, still appear
  in the default listing, and reject triggers 409 without creating an
  execution (3.3);
- the detail route returns any known registration id with its
  executions (3.4);
- an empty or absent ``/aws_dda/workflows/`` root is a byte-identical
  no-op: no rows, empty listing (3.5);
- remove-then-readd: the registration row and its execution history are
  never deleted while the directory is gone, the registration is
  non-runnable (409) while gone, and the reappearing directory flips it
  back to ``registered`` with the exact active payload on the next scan
  (3.6).

DELIBERATELY NOT PINNED: the *specific status value* a vanished
directory's registration carries while gone, and whether it appears in
the default listing during that window. On the unfixed tree
``_invalidate_removed`` marks it ``invalid`` and the unfiltered listing
keeps returning it — behavior the fix INTENTIONALLY changes to
``removed`` + filtered (bugfix requirements 2.2/2.4, exercised by the
task-1 exploration test). Only what must survive per the Preservation
requirements is asserted here.

Harness: workflow_engine_test_utils (make_session_factory, make_watcher,
write_artifact_set) plus the standalone-FastAPI-app pattern from
test_workflow_engine_api.py, mirroring
test_stale_registrations_exploration.py.
"""
import shutil
import tempfile
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from workflow_engine_test_utils import (
    DEVICE_ARCH,
    VALID_MANIFEST,
    make_session_factory,
    make_watcher,
    write_artifact_set,
)

from workflow_engine import api as workflow_engine_api
from workflow_engine.models import WorkflowExecution, WorkflowRegistration

#: The full observed payload key set for an active (``registered``)
#: listing item on the unfixed tree; invalid items additionally carry
#: ``invalidReason``.
ACTIVE_PAYLOAD_KEYS = {
    "registrationId", "workflowId", "name", "version", "arch",
    "artifactPath", "status", "registeredAt",
}

WRONG_ARCH = "arm64_jp5"

#: Layout variants for one single-version workflow, with the
#: independently-recorded classification the unfixed
#: discovery/watcher produces for each (status, arch, exact reason or
#: prefix, listed name).
VARIANTS = ("valid", "valid_named", "missing_compiled",
            "broken_manifest", "wrong_arch")


# ---------------------------------------------------------------------------
# Harness (test_workflow_engine_api.py / exploration-test pattern)
# ---------------------------------------------------------------------------


def make_client(session_factory):
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


def api_call(session_factory, watcher, method, path):
    """One request against the standalone app, with the watcher's reasons
    map wired through ``runtime.invalid_reason`` (as production wiring
    does), so invalid payloads surface their reasons."""
    with patch(
        "workflow_engine.runtime.invalid_reason",
        side_effect=watcher.invalid_reason,
    ):
        with make_client(session_factory) as client:
            return getattr(client, method)(path)


def default_listing(session_factory, watcher):
    response = api_call(session_factory, watcher, "get",
                        "/workflows/registrations")
    assert response.status_code == 200
    return response.json()


def db_rows(session_factory):
    session = session_factory()
    try:
        return {
            row.id: {
                "workflow_id": row.workflow_id,
                "version": row.version,
                "arch": row.arch,
                "artifact_path": row.artifact_path,
                "status": row.status,
                "registered_at": row.registered_at,
            }
            for row in session.query(WorkflowRegistration).all()
        }
    finally:
        session.close()


def execution_count(session_factory):
    session = session_factory()
    try:
        return session.query(WorkflowExecution).count()
    finally:
        session.close()


def write_variant(root, workflow_id, version, variant):
    """One single-version artifact set of the given variant; returns
    (version_dir, expected) where ``expected`` is the classification
    observed on the unfixed tree."""
    name = "Display Name {0}".format(workflow_id)
    if variant == "valid":
        version_dir = write_artifact_set(
            root, workflow_id=workflow_id, version=version)
        expected = {"status": "registered", "arch": DEVICE_ARCH,
                    "reason_prefix": None, "name": None}
    elif variant == "valid_named":
        manifest = dict(VALID_MANIFEST, workflowName=name)
        version_dir = write_artifact_set(
            root, workflow_id=workflow_id, version=version,
            manifest=manifest)
        expected = {"status": "registered", "arch": DEVICE_ARCH,
                    "reason_prefix": None, "name": name}
    elif variant == "missing_compiled":
        version_dir = write_artifact_set(
            root, workflow_id=workflow_id, version=version,
            omit=("compiled_pipeline.json",))
        expected = {
            "status": "invalid", "arch": "unknown",
            "reason_prefix": ("Missing required artifact file: "
                              "compiled_pipeline.json"),
            "name": None,
        }
    elif variant == "broken_manifest":
        version_dir = write_artifact_set(
            root, workflow_id=workflow_id, version=version,
            raw_manifest="{ this is not json")
        expected = {"status": "invalid", "arch": "unknown",
                    "reason_prefix": "Malformed manifest.json:",
                    "name": None}
    elif variant == "wrong_arch":
        manifest = dict(VALID_MANIFEST, targetArch=WRONG_ARCH)
        version_dir = write_artifact_set(
            root, workflow_id=workflow_id, version=version,
            manifest=manifest)
        expected = {
            "status": "invalid", "arch": WRONG_ARCH,
            "reason_prefix": (
                "Artifact architecture '{0}' does not match this device's "
                "architecture '{1}'".format(WRONG_ARCH, DEVICE_ARCH)),
            "name": None,
        }
    else:  # pragma: no cover - strategy is closed over VARIANTS
        raise AssertionError(variant)
    return version_dir, expected


# ---------------------------------------------------------------------------
# Strategies: single-version-per-workflow layouts (the bug condition
# never holds: one version directory per workflow, all present)
# ---------------------------------------------------------------------------

_layouts = st.lists(
    st.tuples(st.integers(min_value=1, max_value=99),
              st.sampled_from(VARIANTS)),
    min_size=1, max_size=4,
)


# ---------------------------------------------------------------------------
# Property: single-version registration/listing identity (3.1, 3.3, 3.4)
# ---------------------------------------------------------------------------


class TestSingleVersionLayoutBaseline:
    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(layout=_layouts)
    def test_rows_listing_reasons_and_trigger_guard_match_baseline(
            self, layout):
        """**Property 2: Preservation (device half)** — for ANY
        single-version-per-workflow layout (valid, named, malformed,
        wrong-arch), one scan produces exactly the observed unfixed rows
        and listing: valid sets ``registered`` with the exact payload,
        malformed sets ``invalid`` with the observed reason and STILL
        listed, triggers 200 on registered / 409 (no execution) on
        invalid, and the detail route returns executions for any known
        id.

        **Validates: Requirements 3.1, 3.3, 3.4**
        """
        root = tempfile.mkdtemp(prefix="stale_regs_preserve_")
        try:
            session_factory = make_session_factory()
            expectations = {}
            for index, (version, variant) in enumerate(layout):
                workflow_id = "wf-{0:02d}".format(index)
                version_dir, expected = write_variant(
                    root, workflow_id, str(version), variant)
                expected["version"] = str(version)
                expected["workflow_id"] = workflow_id
                expected["artifact_path"] = version_dir
                expectations["{0}:{1}".format(workflow_id, version)] = expected

            watcher = make_watcher(root, session_factory)
            watcher.sync_once()

            # --- Registration rows (3.1 / 3.3) ---
            rows = db_rows(session_factory)
            assert set(rows) == set(expectations), (
                "PRESERVATION REGRESSION (Req 3.1): row id set changed")
            for reg_id, expected in expectations.items():
                row = rows[reg_id]
                assert row["status"] == expected["status"], (
                    "PRESERVATION REGRESSION (Req 3.1/3.3): {0} status "
                    "{1!r} != baseline {2!r}".format(
                        reg_id, row["status"], expected["status"]))
                assert row["workflow_id"] == expected["workflow_id"]
                assert row["version"] == expected["version"]
                assert row["arch"] == expected["arch"]
                assert row["artifact_path"] == expected["artifact_path"]
                assert isinstance(row["registered_at"], int)
                assert row["registered_at"] > 0

            # --- Invalid reasons (3.3) ---
            for reg_id, expected in expectations.items():
                reason = watcher.invalid_reason(reg_id)
                if expected["status"] == "registered":
                    assert reason is None
                else:
                    assert reason is not None and reason.startswith(
                        expected["reason_prefix"]), (
                        "PRESERVATION REGRESSION (Req 3.3): {0} reason "
                        "{1!r} does not match baseline prefix {2!r}".format(
                            reg_id, reason, expected["reason_prefix"]))

            # --- Default listing payloads (3.1 / 3.3): every
            # single-version registration is listed — registered AND
            # invalid — ordered by (workflow_id, version), exact keys ---
            listing = default_listing(session_factory, watcher)
            assert [item["registrationId"] for item in listing] == sorted(
                expectations), (
                "PRESERVATION REGRESSION (Req 3.1/3.3): listing ids/order "
                "changed")
            for item in listing:
                expected = expectations[item["registrationId"]]
                if expected["status"] == "registered":
                    assert set(item) == ACTIVE_PAYLOAD_KEYS, (
                        "PRESERVATION REGRESSION (Req 3.1): active payload "
                        "keys changed: {0!r}".format(sorted(item)))
                else:
                    assert set(item) == ACTIVE_PAYLOAD_KEYS | {
                        "invalidReason"}
                    assert item["invalidReason"].startswith(
                        expected["reason_prefix"])
                assert item["workflowId"] == expected["workflow_id"]
                assert item["name"] == expected["name"]
                assert item["version"] == expected["version"]
                assert item["arch"] == expected["arch"]
                assert item["artifactPath"] == expected["artifact_path"]
                assert item["status"] == expected["status"]

            # --- Trigger guard (3.1 / 3.3) and detail route (3.4) ---
            for reg_id, expected in expectations.items():
                executions_before = execution_count(session_factory)
                response = api_call(
                    session_factory, watcher, "post",
                    "/workflows/registrations/{0}/trigger".format(reg_id))
                if expected["status"] == "registered":
                    assert response.status_code == 200, (
                        "PRESERVATION REGRESSION (Req 3.1): trigger on the "
                        "deployed registration {0} returned {1}".format(
                            reg_id, response.status_code))
                    assert response.json()["status"] == "pending"
                    assert execution_count(session_factory) == (
                        executions_before + 1)
                else:
                    assert response.status_code == 409, (
                        "PRESERVATION REGRESSION (Req 3.3): trigger on the "
                        "invalid registration {0} returned {1}".format(
                            reg_id, response.status_code))
                    assert execution_count(session_factory) == (
                        executions_before)

                detail = api_call(
                    session_factory, watcher, "get",
                    "/workflows/registrations/{0}".format(reg_id))
                assert detail.status_code == 200, (
                    "PRESERVATION REGRESSION (Req 3.4): detail route for "
                    "known id {0} returned {1}".format(
                        reg_id, detail.status_code))
                body = detail.json()
                expected_executions = (
                    1 if expected["status"] == "registered" else 0)
                assert len(body["executions"]) == expected_executions
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property: remove-then-readd — rows/history survive, flip-back to
# registered on reappearance (3.4, 3.6)
# ---------------------------------------------------------------------------


class TestRemoveThenReaddBaseline:
    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(version=st.integers(min_value=1, max_value=99))
    def test_row_and_history_survive_and_flip_back_on_reappearance(
            self, version):
        """**Property 2: Preservation (device half)** — register one
        valid version, run it once, delete its directory, rescan, then
        re-add the directory (what a Greengrass component restart's Run
        re-copy produces) and rescan again:

        - while gone: the row and its execution history are never
          deleted, the detail route still returns them (3.4), and the
          registration is non-runnable (409, no new execution) —
          asserted WITHOUT pinning the specific non-active status value
          or its listing visibility, which the fix intentionally changes
          (invalid -> removed, listed -> filtered);
        - after reappearance: the registration flips back to
          ``registered`` with the exact active payload, is listed again,
          and triggers run (3.6), with prior history intact.

        **Validates: Requirements 3.4, 3.6**
        """
        root = tempfile.mkdtemp(prefix="stale_regs_readd_")
        try:
            session_factory = make_session_factory()
            reg_id = "wf-1:{0}".format(version)
            version_dir = write_artifact_set(
                root, workflow_id="wf-1", version=str(version))
            watcher = make_watcher(root, session_factory)
            watcher.sync_once()
            assert db_rows(session_factory)[reg_id]["status"] == "registered"

            # One run on the deployed version (history to preserve).
            trigger = api_call(
                session_factory, watcher, "post",
                "/workflows/registrations/{0}/trigger".format(reg_id))
            assert trigger.status_code == 200
            execution_id = trigger.json()["executionId"]

            # --- Directory vanishes; rescan ---
            shutil.rmtree(version_dir)
            watcher.sync_once()

            rows = db_rows(session_factory)
            assert reg_id in rows, (
                "PRESERVATION REGRESSION (Req 3.4): the registration row "
                "was deleted after its artifact directory vanished")
            # Non-runnable while gone (unfixed: status 'invalid'; fixed:
            # 'removed' — both reject; the exact status is NOT pinned).
            assert rows[reg_id]["status"] != "registered"
            response = api_call(
                session_factory, watcher, "post",
                "/workflows/registrations/{0}/trigger".format(reg_id))
            assert response.status_code == 409
            assert execution_count(session_factory) == 1

            detail = api_call(
                session_factory, watcher, "get",
                "/workflows/registrations/{0}".format(reg_id))
            assert detail.status_code == 200, (
                "PRESERVATION REGRESSION (Req 3.4): detail route stopped "
                "returning the retired registration")
            executions = detail.json()["executions"]
            assert [e["executionId"] for e in executions] == [execution_id], (
                "PRESERVATION REGRESSION (Req 3.4): execution history was "
                "not preserved across directory removal")

            # --- Directory reappears (Run re-copy); rescan ---
            write_artifact_set(root, workflow_id="wf-1", version=str(version))
            watcher.sync_once()

            rows = db_rows(session_factory)
            assert rows[reg_id]["status"] == "registered", (
                "PRESERVATION REGRESSION (Req 3.6): a reappearing artifact "
                "directory did not flip the registration back to "
                "'registered' (got {0!r})".format(rows[reg_id]["status"]))
            assert watcher.invalid_reason(reg_id) is None

            listing = default_listing(session_factory, watcher)
            assert [item["registrationId"] for item in listing] == [reg_id]
            item = listing[0]
            assert set(item) == ACTIVE_PAYLOAD_KEYS
            assert item["status"] == "registered"
            assert item["version"] == str(version)
            assert item["arch"] == DEVICE_ARCH

            # Runnable again; prior history intact, new run appended.
            response = api_call(
                session_factory, watcher, "post",
                "/workflows/registrations/{0}/trigger".format(reg_id))
            assert response.status_code == 200
            detail = api_call(
                session_factory, watcher, "get",
                "/workflows/registrations/{0}".format(reg_id))
            execution_ids = {
                e["executionId"] for e in detail.json()["executions"]}
            assert execution_id in execution_ids
            assert len(execution_ids) == 2
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Empty / absent root: byte-identical no-op (3.5)
# ---------------------------------------------------------------------------


class TestEmptyRootNoOp:
    def test_empty_root_registers_nothing_and_lists_empty(self):
        """An existing-but-empty workflows root: one scan touches
        nothing, registers nothing, and the listing is empty.

        **Validates: Requirements 3.5**
        """
        root = tempfile.mkdtemp(prefix="stale_regs_empty_")
        try:
            session_factory = make_session_factory()
            watcher = make_watcher(root, session_factory)
            assert watcher.sync_once() == []
            assert db_rows(session_factory) == {}
            assert default_listing(session_factory, watcher) == []
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_absent_root_registers_nothing_and_lists_empty(self):
        """A device that never received a Workflow_Component (root
        directory absent): identical no-op.

        **Validates: Requirements 3.5**
        """
        root = tempfile.mkdtemp(prefix="stale_regs_absent_")
        shutil.rmtree(root)  # the root path does not exist at scan time
        session_factory = make_session_factory()
        watcher = make_watcher(root, session_factory)
        assert watcher.sync_once() == []
        assert db_rows(session_factory) == {}
        assert default_listing(session_factory, watcher) == []
