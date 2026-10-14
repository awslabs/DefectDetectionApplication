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
Unit tests for the build events consumer
(``edge-cv-portal/backend/functions/build_events.py``, task 8.5 of
portal-build-fleet-and-workflow-gates).

**Validates: Requirements 4.1, 5.4, 5.5**

- Duplicate EventBridge delivery is a no-op (Req 4.1 idempotence): the
  handler applies every status transition as a DynamoDB conditional
  update computed by the build_domain state machine with terminal
  absorption, so redelivering a phase event (intermediate or terminal)
  leaves the Build_Job record byte-identical and records no second
  Audit_Log entry.
- ``build_published`` audit content (Req 5.5): a succeeded phase event
  records the agent-reported result metadata verbatim on the Build_Job
  and produces a ``build_published`` Audit_Log entry carrying the
  component name, published version, and pushed image references.
- Publishing failure (Req 5.4): a ``phase=failed`` event with
  ``error_kind=publishing`` marks the job failed with the
  PUBLISHING_FAILED error kind — distinct from a build failure's
  BUILD_FAILED — preserves the per-artifact published/unpublished lists
  exactly as reported (``publish_partial``), and records the
  ``build_publishing_failed`` Audit_Log entry.

DynamoDB is real moto (module-scope mock, sibling
test_dispatcher_tick_integration.py pattern); shared_utils is replaced
by a minimal fake capturing Audit_Log entries. Events are delivered
through the Lambda ``handler`` entry point (source
``dda.portal.builds``, detail-type ``BuildPhaseChange``) so routing is
exercised too.
"""
import os
import sys
import types

# ---------------------------------------------------------------------------
# Environment BEFORE any import: build_events binds its boto3 resources
# and env-derived table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_JOBS_TABLE = "build-jobs-t85"
_SERVERS_TABLE = "build-servers-t85"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call. bz2 is only used for S3-Select payload decompression, which
# this suite never exercises, so a minimal stdlib-shaped stub keeps the
# import chain intact where _bz2 is absent (sibling shim).
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

# ---------------------------------------------------------------------------
# Fake shared_utils capturing Audit_Log entries (build_events imports only
# log_audit_event from the layer).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    module.log_audit_event = log_audit_event
    return module


# Fresh modules so build_events' module-level boto3 handles are created
# under the moto mock started below (sibling pattern).
for _module in ("build_events", "build_domain", "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()

# Module-scope moto: active for every import below and for the whole run.
_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name in (_JOBS_TABLE, _SERVERS_TABLE):
    _key = "build_job_id" if _name == _JOBS_TABLE else "server_id"
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

import build_domain  # noqa: E402
import build_events  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = 1_700_000_000_000  # ms epoch anchor


def _clear():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]


def _get_job(job_id):
    return build_events.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_job(job_id, status, **extra):
    item = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_JP5,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": status,
        "requested_by": "operator-1",
        "created_at": NOW - 60_000,
    }
    item.update(extra)
    _JOBS.put_item(Item=item)
    return item


def _phase_event(detail):
    """EventBridge envelope for a custom build agent phase event
    (source dda.portal.builds, detail-type BuildPhaseChange)."""
    return {
        "source": build_events.PHASE_EVENT_SOURCE,
        "detail-type": build_events.PHASE_EVENT_DETAIL_TYPE,
        "detail": detail,
    }


def _deliver(detail):
    return build_events.handler(_phase_event(detail), None)


def _audits(action):
    return [entry for entry in AUDIT_EVENTS if entry["action"] == action]


_SUCCESS_RESULT = {
    "component_name": "aws.edgeml.dda.LocalServer.arm64JP5",
    "published_version": "2.1.7",
    "pushed_image_refs": [
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/flask-app:2.1.7",
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/react-webapp:2.1.7",
    ],
}


# ---------------------------------------------------------------------------
# Duplicate EventBridge delivery is a no-op (Req 4.1)
# ---------------------------------------------------------------------------

class TestDuplicateDeliveryIsNoOp:

    def setup_method(self):
        _clear()

    def test_duplicate_succeeded_event_leaves_job_and_audit_unchanged(self):
        """Redelivering the terminal succeeded phase event is a no-op:
        the Build_Job record is byte-identical (status, result, ended_at
        untouched) and no second build_published Audit_Log entry is
        recorded (Req 4.1)."""
        _seed_job("job-1", build_domain.STATUS_PUBLISHING,
                  started_at=NOW - 30_000)
        detail = {"build_job_id": "job-1", "phase": "succeeded",
                  "result": _SUCCESS_RESULT}

        _deliver(detail)
        first = _get_job("job-1")
        assert first["status"] == build_domain.STATUS_SUCCEEDED
        assert len(_audits(build_events.AUDIT_BUILD_PUBLISHED)) == 1

        # Duplicate delivery (EventBridge at-least-once): no-op.
        response = _deliver(detail)
        assert response["statusCode"] == 200
        assert _get_job("job-1") == first, \
            "duplicate delivery must not modify the Build_Job record"
        assert len(_audits(build_events.AUDIT_BUILD_PUBLISHED)) == 1, \
            "duplicate delivery must not record a second audit entry"

    def test_duplicate_intermediate_phase_event_is_noop(self):
        """Redelivering an intermediate (publishing) phase event while
        the job already reached that status changes nothing (Req 4.1):
        the conditional transition finds no defined edge to apply."""
        _seed_job("job-2", build_domain.STATUS_BUILDING,
                  started_at=NOW - 30_000)
        detail = {"build_job_id": "job-2", "phase": "publishing"}

        _deliver(detail)
        first = _get_job("job-2")
        assert first["status"] == build_domain.STATUS_PUBLISHING

        _deliver(detail)
        assert _get_job("job-2") == first
        assert AUDIT_EVENTS == [], \
            "an intermediate phase transition records no audit entry"

    def test_duplicate_publishing_failure_event_is_noop(self):
        """Redelivering the publishing-failure event leaves the failed
        job's error record and publish_partial lists untouched and
        records no second audit entry (Req 4.1): a terminal Build_Job is
        never resurrected or rewritten."""
        _seed_job("job-3", build_domain.STATUS_PUBLISHING,
                  started_at=NOW - 30_000)
        detail = {
            "build_job_id": "job-3",
            "phase": "failed",
            "error_kind": build_events.ERROR_KIND_PUBLISHING,
            "error_message": "docker push denied",
            "published_artifacts": ["flask-app:2.1.7"],
            "unpublished_artifacts": ["react-webapp:2.1.7",
                                      "greengrass-component"],
        }

        _deliver(detail)
        first = _get_job("job-3")
        assert first["status"] == build_domain.STATUS_FAILED
        assert len(_audits(build_events.AUDIT_BUILD_PUBLISHING_FAILED)) == 1

        _deliver(detail)
        assert _get_job("job-3") == first
        assert len(_audits(build_events.AUDIT_BUILD_PUBLISHING_FAILED)) == 1

    def test_stale_event_after_terminal_status_is_noop(self):
        """A stale non-terminal phase event delivered after the job
        reached a terminal status is a no-op (terminal absorption,
        Req 4.1)."""
        _seed_job("job-4", build_domain.STATUS_SUCCEEDED,
                  started_at=NOW - 30_000, ended_at=NOW - 1_000,
                  result=_SUCCESS_RESULT)
        before = _get_job("job-4")

        for phase in ("building", "publishing", "failed"):
            _deliver({"build_job_id": "job-4", "phase": phase})

        assert _get_job("job-4") == before
        assert AUDIT_EVENTS == []


# ---------------------------------------------------------------------------
# build_published audit content (Req 5.5) + verbatim result (Req 5.3)
# ---------------------------------------------------------------------------

class TestBuildPublishedAudit:

    def setup_method(self):
        _clear()

    def test_succeeded_event_records_result_and_build_published_audit(self):
        """A succeeded phase event records the agent-reported result
        metadata verbatim on the Build_Job and produces exactly one
        ``build_published`` Audit_Log entry carrying the component name,
        published version, and pushed image references (Req 5.5)."""
        _seed_job("job-5", build_domain.STATUS_PUBLISHING,
                  started_at=NOW - 30_000)

        response = _deliver({"build_job_id": "job-5", "phase": "succeeded",
                             "result": _SUCCESS_RESULT})
        assert response["statusCode"] == 200

        job = _get_job("job-5")
        assert job["status"] == build_domain.STATUS_SUCCEEDED
        assert job["result"] == _SUCCESS_RESULT, \
            "result metadata must be recorded verbatim as reported"
        assert job["ended_at"], "a terminal status records the end time"

        audits = _audits(build_events.AUDIT_BUILD_PUBLISHED)
        assert len(audits) == 1
        entry = audits[0]
        assert entry["resource_type"] == "build_job"
        assert entry["resource_id"] == "job-5"
        assert entry["result"] == "success"
        details = entry["details"]
        assert details["component_name"] == \
            "aws.edgeml.dda.LocalServer.arm64JP5"
        assert details["component_version"] == "2.1.7"
        assert details["image_refs"] == _SUCCESS_RESULT["pushed_image_refs"]


# ---------------------------------------------------------------------------
# Publishing failure recording (Req 5.4)
# ---------------------------------------------------------------------------

class TestPublishingFailureRecording:

    def setup_method(self):
        _clear()

    def test_publishing_failure_records_lists_and_distinct_error_kind(self):
        """A phase=failed event with error_kind=publishing marks the job
        failed with the PUBLISHING_FAILED error code, preserves the
        per-artifact published/unpublished lists exactly as reported,
        and records the build_publishing_failed audit entry (Req 5.4)."""
        _seed_job("job-6", build_domain.STATUS_PUBLISHING,
                  started_at=NOW - 30_000)
        published = ["flask-app:2.1.7"]
        unpublished = ["react-webapp:2.1.7", "greengrass-component"]

        _deliver({
            "build_job_id": "job-6",
            "phase": "failed",
            "error_kind": build_events.ERROR_KIND_PUBLISHING,
            "error_message": "ECR push denied for react-webapp",
            "published_artifacts": published,
            "unpublished_artifacts": unpublished,
        })

        job = _get_job("job-6")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == build_events.ERROR_PUBLISHING_FAILED
        assert job["error"]["message"] == "ECR push denied for react-webapp"
        assert job["ended_at"]
        # Per-artifact lists preserved exactly as reported (Req 5.4).
        assert job["publish_partial"]["published"] == published
        assert job["publish_partial"]["unpublished"] == unpublished

        audits = _audits(build_events.AUDIT_BUILD_PUBLISHING_FAILED)
        assert len(audits) == 1
        entry = audits[0]
        assert entry["resource_id"] == "job-6"
        assert entry["result"] == "failure"
        details = entry["details"]
        assert details["error_kind"] == build_events.ERROR_KIND_PUBLISHING
        assert details["error_code"] == build_events.ERROR_PUBLISHING_FAILED
        assert details["published"] == published
        assert details["unpublished"] == unpublished

    def test_publishing_failure_code_is_distinct_from_build_failure(self):
        """The publishing failure's error kind is DISTINCT from a build
        failure's (Req 5.4): a plain phase=failed on a building job
        records BUILD_FAILED with a build_failed audit, never
        PUBLISHING_FAILED and never publish_partial lists."""
        assert build_events.ERROR_PUBLISHING_FAILED != \
            build_events.ERROR_BUILD_FAILED

        _seed_job("job-7", build_domain.STATUS_BUILDING,
                  started_at=NOW - 30_000)
        _deliver({
            "build_job_id": "job-7",
            "phase": "failed",
            "error_kind": "building",
            "error_message": "onnxruntime compilation failed",
        })

        job = _get_job("job-7")
        assert job["status"] == build_domain.STATUS_FAILED
        assert job["error"]["code"] == build_events.ERROR_BUILD_FAILED
        assert "publish_partial" not in job

        assert _audits(build_events.AUDIT_BUILD_PUBLISHING_FAILED) == []
        build_audits = _audits(build_events.AUDIT_BUILD_FAILED)
        assert len(build_audits) == 1
        assert build_audits[0]["details"]["error_code"] == \
            build_events.ERROR_BUILD_FAILED
