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
Build fleet execution failures — PRESERVATION baseline
(build-fleet-execution-failures, task 2).

**Property 2: Preservation** — Non-bug Build and Fleet behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.8, 3.9, 3.10,
3.11, 3.12**

Observation-first: every oracle in this file was RECORDED from the
CURRENT (unfixed) working tree — which already contains the completed
build-source-selection, build-fleet-rbac-visibility, custom-python-source
and dispatcher run-as-ubuntu changes; those ARE the baseline. These tests
MUST PASS now and MUST keep passing verbatim after the
build-fleet-execution-failures fix lands. This oracle is IMMUTABLE: it is
never rebaselined after implementation.

The input domain of this file is exactly the complement of the design's
`isBugCondition(input)`: normal agent phase deliveries, terminal
absorption, ordinary API reads/cancels/retries, target/component/mode
mappings, allocation/promotion/serialization decisions, and legacy
runtime-deadline arithmetic. Nothing here exercises a terminal SSM
command whose invocation evidence must be recovered, or a timeout whose
lifecycle evidence is insufficient — those are the exploration file's
domain (task 1) and MAY change with the fix.

What is pinned, and the additive-tolerance contract:

* **Req 3.1 — normal phase/result behavior.** The
  `build_events.apply_phase_event` pure oracle: building/publishing/
  succeeded/build-failed/publishing-failed transition chains along the
  build_domain state machine, `started_at`/`ended_at` recording, the
  succeeded event's `result` recorded VERBATIM, the publishing failure's
  distinct `PUBLISHING_FAILED` error kind with the per-artifact
  `publish_partial` lists exactly as reported, the recorded Audit_Log
  action names (`build_published`, `build_publishing_failed`,
  `build_failed`) and their detail meanings, and absorbing terminal
  states (any phase event on a terminal job is a no-op).
* **Req 3.6 — the Build Jobs API surface.** `GET /builds` envelope
  (`jobs`, `nextToken`, `total`), most-recent-first ordering, opaque
  offset pagination, the 90-day window; `GET /builds/{id}` (`job`) and
  the `{error: {code, message, details}}` 404 envelope;
  `GET /builds/{id}/logs` (`events`, `nextToken`) including the
  missing-stream empty page; cancel semantics (queued -> cancelled +
  audit; terminal/provisioning -> 409, job unchanged); retry semantics
  (interrupted-only, 201 with `retry_of`). Envelope assertions check the
  RECORDED keys and values as a SUBSET, so the design's optional additive
  `diagnostic`/`timing` fields cannot break this baseline while every
  recorded key keeps its recorded meaning ("excluded from equality only
  when absent in the original").
* **Req 3.7 — target/component/mode mappings.** `JP5`/`JP6 -> arm64`,
  `AMD64`/`AMD64_NVIDIA -> x86_64`, the exact component identities and
  recipes, unsupported-target rejection, the dedicated/ephemeral mode
  set, and the intentional dedicated arch-mismatch validation.
* **Req 3.2 / 3.4 / 3.11 — dispatch and terminal-effects contracts.**
  The DynamoDB single-slot allocation lock (`allocate_server` /
  `release_server` conditional writes, including stale-release
  protection), the pre-dispatch pgrep gate (`decide_predispatch`,
  `BUILD_PROCESS_PATTERNS`, the 5-minute re-verification interval), the
  on-server flock contract in `scripts/portal-build-agent.sh`
  (/var/lock/dda-build.lock, `flock -n`, exit 75), the serialization
  watchdog decisions, predecessor gates, original-submission ordering,
  and oldest-eligible queue promotion.
* **Req 3.3 — cancellation.** `build_domain.decide_cancellation` over
  every status x confirmation combination: queued cancels immediately
  with queue removal, running cancels iff the stop is pgrep-confirmed
  (fail-closed otherwise), terminal and provisioning are rejected
  unchanged.
* **Req 3.9 — ephemeral compute lifecycle.** `plan_runner` (one runner
  per job, snapshot-only sizing, provisioning status) and
  `decide_termination_retry` (10-minute cadence inside the 1-hour
  window, one orphaned-runner notification at exhaustion).
* **Req 3.8 / 3.12 — runtime compatibility.** For LEGACY-shaped jobs
  (only `started_at` + snapshotted `max_runtime_hours`, no timing/lease
  evidence): queue/predecessor wait remains queued and is never charged
  as runtime, `now == deadline` is NOT expired, `now > deadline` IS
  expired (strict boundary), a missing start time never times out
  (fail-safe), and the job's own snapshotted `max_runtime_hours`
  (default 4) remains the budget — never current configuration.

Oracles are recorded pure-function or API observations; no assertion
restates implementation internals beyond the frozen public contract.

SAFETY (absolute): no test in this file launches EC2 compute, sends a
real SSM command, calls real AWS, deploys, publishes an artifact, or
starts a real build. Every AWS interaction runs against moto
(`mock_aws` started at module scope before any handler import), and the
dispatcher invocation env var is unset so `invoke_dispatcher` is a no-op.
"""
import json
import os
import re
import sys
import time
import types
import uuid
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3
# resources/clients and table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "exec-failure-preserve"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"
_USER_ROLES_TABLE = f"dda-portal-user-roles-{_SUFFIX}"
_AUDIT_TABLE = f"dda-portal-audit-log-{_SUFFIX}"
_LOG_GROUP = f"/dda/portal-builds-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ["USER_ROLES_TABLE"] = _USER_ROLES_TABLE
os.environ["AUDIT_LOG_TABLE"] = _AUDIT_TABLE
os.environ["BUILD_LOG_GROUP"] = _LOG_GROUP
# Unset so invoke_dispatcher is a logged no-op: nothing can be dispatched.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
# No repository URL / no repo-dir override: module defaults are the
# recorded baseline.
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_REPO_DIR", None)

# Import boto3 (and thus botocore/urllib3) from the test environment
# BEFORE the Lambda layer directory joins sys.path: the layer vendors its
# own urllib3 build targeting the Lambda runtime.
import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_source_selection_preservation.py).
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
_BACKEND = os.path.join(_REPO_ROOT, "edge-cv-portal", "backend")

FUNCTIONS_DIR = os.environ.get(
    "DDA_BUILD_FN_DIR", os.path.join(_BACKEND, "functions"))
LAYER_DIR = os.environ.get(
    "DDA_BUILD_LAYER_DIR",
    os.path.join(_BACKEND, "layers", "shared", "python"))

for _p in (LAYER_DIR, FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fresh modules so the module-level boto3 handles are created under the
# moto mock started below, bound to this file's table names.
for _module in ("build_jobs", "build_dispatcher", "build_events",
                "build_fleet", "build_domain", "build_planner",
                "build_source", "rbac_middleware", "shared_utils"):
    sys.modules.pop(_module, None)

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

for _name, _key in ((_JOBS_TABLE, "build_job_id"),
                    (_SERVERS_TABLE, "server_id"),
                    (_SETTINGS_TABLE, "setting_key")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_DDB.create_table(
    TableName=_USER_ROLES_TABLE,
    KeySchema=[
        {"AttributeName": "user_id", "KeyType": "HASH"},
        {"AttributeName": "usecase_id", "KeyType": "RANGE"},
    ],
    AttributeDefinitions=[
        {"AttributeName": "user_id", "AttributeType": "S"},
        {"AttributeName": "usecase_id", "AttributeType": "S"},
    ],
    BillingMode="PAY_PER_REQUEST",
)
_DDB.create_table(
    TableName=_AUDIT_TABLE,
    KeySchema=[
        {"AttributeName": "event_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ],
    AttributeDefinitions=[
        {"AttributeName": "event_id", "AttributeType": "S"},
        {"AttributeName": "timestamp", "AttributeType": "N"},
    ],
    BillingMode="PAY_PER_REQUEST",
)

_LOGS = boto3.client("logs", region_name="us-east-1")
_LOGS.create_log_group(logGroupName=_LOG_GROUP)

_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)
_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_events  # noqa: E402
import build_jobs  # noqa: E402
import build_dispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# THE ORACLE — recorded from the CURRENT (unfixed) working tree.
# ---------------------------------------------------------------------------

#: Target -> (component identity, recipe, required architecture)
#: (Req 3.7): JP5/JP6/JP7 are arm64, AMD64/AMD64_NVIDIA are x86_64, and
#: the component identities are exactly these (JP7 added by the
#: jetpack7-support spec's target-matrix extension).
TARGET_ORACLE = {
    "JP5": ("aws.edgeml.dda.LocalServer.arm64JP5",
            "recipe-arm64-jp5.yaml", "arm64"),
    "JP6": ("aws.edgeml.dda.LocalServer.arm64JP6",
            "recipe-arm64-jp6.yaml", "arm64"),
    "JP7": ("aws.edgeml.dda.LocalServer.arm64JP7",
            "recipe-arm64-jp7.yaml", "arm64"),
    "AMD64": ("aws.edgeml.dda.LocalServer.amd64",
              "recipe-amd64.yaml", "x86_64"),
    "AMD64_NVIDIA": ("aws.edgeml.dda.LocalServer.amd64Nvidia",
                     "recipe-amd64-nvidia.yaml", "x86_64"),
}
TARGETS = tuple(TARGET_ORACLE)
EXECUTION_MODES = ("ephemeral", "dedicated")

#: Build_Job status vocabulary and its terminal (absorbing) subset
#: (Req 3.1, 3.6).
ALL_STATUSES = frozenset({
    "queued", "provisioning", "building", "publishing",
    "succeeded", "failed", "interrupted", "cancelled",
})
TERMINAL_STATUSES = frozenset({
    "succeeded", "failed", "interrupted", "cancelled",
})

#: The recorded state-machine edges for the NORMAL phase/result paths
#: (Req 3.1). Asserted as "these edges keep producing these outcomes";
#: the fix may only ADD events/edges, never change or drop these.
RECORDED_TRANSITIONS = {
    ("queued", "dispatch_ephemeral"): "provisioning",
    ("queued", "dispatch_dedicated"): "building",
    ("queued", "cancel"): "cancelled",
    ("queued", "server_lost"): "failed",
    ("provisioning", "runner_ready"): "building",
    ("provisioning", "provisioning_failed"): "failed",
    ("building", "build_succeeded"): "publishing",
    ("building", "build_failed"): "failed",
    ("building", "timeout"): "failed",
    ("building", "serialization_violation"): "failed",
    ("building", "interruption"): "interrupted",
    ("building", "cancel"): "cancelled",
    ("publishing", "publish_succeeded"): "succeeded",
    ("publishing", "publish_failed"): "failed",
    ("publishing", "timeout"): "failed",
    ("publishing", "serialization_violation"): "failed",
    ("publishing", "interruption"): "interrupted",
    ("publishing", "cancel"): "cancelled",
}
RECORDED_EVENTS = tuple(sorted({event for _, event in RECORDED_TRANSITIONS}))

#: Normal-path completion oracle (Req 3.1): audit action names, error
#: codes, and the publish-failure error kind.
AUDIT_BUILD_PUBLISHED = "build_published"
AUDIT_BUILD_PUBLISHING_FAILED = "build_publishing_failed"
AUDIT_BUILD_FAILED = "build_failed"
ERROR_BUILD_FAILED = "BUILD_FAILED"
ERROR_PUBLISHING_FAILED = "PUBLISHING_FAILED"
ERROR_KIND_PUBLISHING = "publishing"

#: API envelope key sets (Req 3.6). Asserted as SUBSET of the response
#: so the approved optional additive diagnostic/timing fields cannot
#: break the baseline while every recorded key keeps its meaning.
LIST_RESPONSE_KEYS = {"jobs", "nextToken", "total"}
DETAIL_RESPONSE_KEYS = {"job"}
LOGS_RESPONSE_KEYS = {"events", "nextToken"}
CANCEL_RESPONSE_KEYS = {"job"}
ERROR_ENVELOPE_KEYS = {"code", "message", "details"}
HISTORY_WINDOW_DAYS = 90

#: Dispatch / serialization contract (Req 3.2).
BUILD_PROCESS_PATTERNS = ("gdk component build", "build-custom.sh")
PREDISPATCH_RETRY_INTERVAL_MS = 5 * 60 * 1000
SERIALIZATION_CHECK_INTERVAL_MS = 5 * 60 * 1000
SERIALIZATION_VIOLATION_ERROR = "SERIALIZATION_VIOLATION"
AGENT_LOCK_FILE = "/var/lock/dda-build.lock"
AGENT_EXIT_LOCK_HELD = 75
AGENT_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "portal-build-agent.sh")
AGENT_SCRIPT_RELPATH = "scripts/portal-build-agent.sh"

#: Ephemeral cleanup cadence (Req 3.9).
TERMINATION_RETRY_INTERVAL_MS = 10 * 60 * 1000
TERMINATION_RETRY_WINDOW_MS = 60 * 60 * 1000

#: Legacy runtime compatibility (Req 3.8, 3.12).
DEFAULT_MAX_RUNTIME_HOURS = 4
MS_PER_HOUR = 60 * 60 * 1000
RUNNING_WATCHDOG_STATUSES = frozenset({"building", "publishing"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _observation(**fields):
    return json.dumps(fields, indent=2, default=str)


def _jwt_user(role_value="PortalAdmin"):
    user_id = str(uuid.uuid4())
    return {
        "user_id": user_id,
        "email": f"{role_value.lower()}-{user_id[:8]}@example.com",
        "username": f"{role_value.lower()}-{user_id[:8]}",
        "role": role_value,
    }


def _event(resource, method, user, body=None, job_id=None, query=None):
    event = {
        "resource": resource,
        "httpMethod": method,
        "path": resource.replace("{id}", job_id or ""),
        "pathParameters": {"id": job_id} if job_id is not None else None,
        "queryStringParameters": query,
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "authorizer": {
                "claims": {
                    "sub": user["user_id"],
                    "email": user["email"],
                    "cognito:username": user["username"],
                    "custom:role": user["role"],
                }
            },
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _scan_raw_jobs():
    items, kwargs = [], {}
    while True:
        page = _JOBS.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return {item["build_job_id"]: item for item in items}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _raw_job(job_id):
    return _JOBS.get_item(Key={"build_job_id": job_id}).get("Item")


def _audit_records():
    return [build_jobs.to_native(item) for item in
            _AUDIT.scan().get("Items", [])]


def _clear_tables():
    for job_id in _scan_raw_jobs():
        _JOBS.delete_item(Key={"build_job_id": job_id})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    for item in _AUDIT.scan().get("Items", []):
        _AUDIT.delete_item(Key={"event_id": item["event_id"],
                                "timestamp": item["timestamp"]})


def _now_ms():
    return int(time.time() * 1000)


def _seed_job(job_id=None, status="queued", created_at=None,
              execution_mode="ephemeral", server_id=None, target="AMD64",
              **extra):
    job_id = job_id or str(uuid.uuid4())
    item = {
        "build_job_id": job_id,
        "build_target": target,
        "component_name": TARGET_ORACLE[target][0],
        "required_arch": TARGET_ORACLE[target][2],
        "execution_mode": execution_mode,
        "status": status,
        "requested_by": "seed-user",
        "created_at": created_at if created_at is not None else _now_ms(),
    }
    if server_id:
        item["server_id"] = server_id
    item.update(extra)
    _JOBS.put_item(Item=build_jobs.to_dynamo_safe(item))
    return build_jobs.to_native(item)


def _body(response):
    return json.loads(response["body"])


def _assert_error_envelope(response, status, code):
    body = _body(response)
    observed = _observation(status=response["statusCode"], body=body)
    assert response["statusCode"] == status, observed
    assert "error" in body, observed
    error = body["error"]
    # Recorded keys are a SUBSET: optional additive fields tolerated.
    assert ERROR_ENVELOPE_KEYS <= set(error), observed
    assert error["code"] == code, observed
    assert isinstance(error["message"], str) and error["message"], observed
    assert isinstance(error["details"], dict), observed
    return body


def _assert_keys_subset(recorded, payload, **context):
    observed = _observation(recorded=sorted(recorded),
                            payload_keys=sorted(payload), **context)
    assert set(recorded) <= set(payload), observed


# ---------------------------------------------------------------------------
# Generators (pure-function property inputs)
# ---------------------------------------------------------------------------

statuses = st.sampled_from(sorted(ALL_STATUSES))
terminal_statuses = st.sampled_from(sorted(TERMINAL_STATUSES))
recorded_events = st.sampled_from(RECORDED_EVENTS)
phases = st.sampled_from(["building", "publishing", "succeeded", "failed"])

#: Agent result payloads: arbitrary JSON-ish dicts the succeeded event
#: may carry — recorded VERBATIM on the Build_Job (Req 3.1).
result_dicts = st.dictionaries(
    keys=st.text(min_size=1, max_size=12),
    values=st.one_of(
        st.text(max_size=24),
        st.integers(min_value=-10**6, max_value=10**6),
        st.lists(st.text(max_size=12), max_size=3),
    ),
    max_size=5,
)

artifact_lists = st.lists(st.text(min_size=1, max_size=20), max_size=4)


# ===========================================================================
# Req 3.1 — Normal phase/result behavior (pure apply_phase_event oracle)
# ===========================================================================

class TestNormalPhaseBehaviorBaseline:
    """**Property 2: Preservation** — normal building/publishing/
    succeeded/build-failed/publishing-failed behavior, recorded from
    `build_events.apply_phase_event` on unfixed code.

    **Validates: Requirements 3.1, 3.10**
    """

    NOW = 1_754_000_000_000

    def test_state_machine_recorded_edges(self):
        """Every recorded (status, event) edge keeps producing exactly
        its recorded next status (additive-only contract)."""
        for (status, event), expected in RECORDED_TRANSITIONS.items():
            actual = build_domain.next_status(status, event)
            assert actual == expected, _observation(
                status=status, event=event, expected=expected, actual=actual)

    @given(status=terminal_statuses, event=recorded_events)
    @settings(max_examples=60, deadline=None)
    def test_terminal_states_absorb_every_event(self, status, event):
        """Terminal statuses are absorbing for every recorded event."""
        assert build_domain.next_status(status, event) == status

    @given(status=terminal_statuses, phase=phases,
           extra=st.dictionaries(st.sampled_from(
               ["error_kind", "error_message", "source_commit"]),
               st.text(max_size=16), max_size=3))
    @settings(max_examples=60, deadline=None)
    def test_phase_event_on_terminal_job_is_noop(self, status, phase, extra):
        """Any phase event delivered to a terminal job is a no-op:
        no transition, no updates, no audit (duplicate/stale delivery)."""
        detail = {"phase": phase, "build_job_id": "job-x", **extra}
        application = build_events.apply_phase_event(status, detail, self.NOW)
        assert application.is_noop, _observation(
            status=status, detail=detail, application=application._asdict())
        assert application.updates == {}
        assert application.audit_action is None

    def test_building_phase_sets_started_at(self):
        """provisioning -> building on the agent's building event, with
        the start time recorded."""
        application = build_events.apply_phase_event(
            "provisioning", {"phase": "building"}, self.NOW)
        assert application.steps == (("provisioning", "building"),)
        assert application.updates.get("started_at") == self.NOW
        assert application.audit_action is None

    def test_building_phase_on_dedicated_building_job_is_noop(self):
        """A dedicated job is already `building` when the agent reports
        building: duplicate delivery is a no-op."""
        application = build_events.apply_phase_event(
            "building", {"phase": "building"}, self.NOW)
        assert application.is_noop

    def test_publishing_phase_transition(self):
        """building -> publishing on the publishing event (build step
        succeeded); no terminal fields, no audit."""
        application = build_events.apply_phase_event(
            "building", {"phase": "publishing"}, self.NOW)
        assert application.steps == (("building", "publishing"),)
        assert "ended_at" not in application.updates
        assert application.audit_action is None

    @given(result=result_dicts)
    @settings(max_examples=80, deadline=None)
    def test_succeeded_records_result_verbatim(self, result):
        """publishing -> succeeded records the agent-reported result
        VERBATIM, `ended_at`, and the `build_published` audit entry with
        its recorded detail meanings."""
        detail = {"phase": "succeeded", "result": result}
        application = build_events.apply_phase_event(
            "publishing", detail, self.NOW)
        observed = _observation(result=result,
                                application=application._asdict())
        assert application.steps == (("publishing", "succeeded"),), observed
        assert application.updates["result"] == result, observed
        assert application.updates["ended_at"] == self.NOW, observed
        assert application.audit_action == AUDIT_BUILD_PUBLISHED, observed
        details = application.audit_details
        assert details["component_name"] == result.get("component_name"), \
            observed
        assert details["component_version"] == (
            result.get("published_version")
            or result.get("component_version")), observed
        assert details["image_refs"] == (
            result.get("pushed_image_refs")
            or result.get("image_refs") or []), observed

    def test_succeeded_heals_lost_intermediates(self):
        """A succeeded event arriving while the job is still `building`
        chains building -> publishing -> succeeded."""
        application = build_events.apply_phase_event(
            "building", {"phase": "succeeded", "result": {}}, self.NOW)
        assert application.steps == (("building", "publishing"),
                                     ("publishing", "succeeded"))

    def test_non_dict_result_recorded_as_empty(self):
        """A non-dict agent result is recorded as {} (recorded oracle)."""
        application = build_events.apply_phase_event(
            "publishing", {"phase": "succeeded", "result": "oops"}, self.NOW)
        assert application.updates["result"] == {}

    def test_build_failure_records_build_failed(self):
        """building -> failed with the BUILD_FAILED error code, the
        agent's message, `ended_at`, and the `build_failed` audit."""
        detail = {"phase": "failed", "error_kind": "building",
                  "error_message": "gdk component build failed"}
        application = build_events.apply_phase_event(
            "building", detail, self.NOW)
        observed = _observation(application=application._asdict())
        assert application.steps == (("building", "failed"),), observed
        assert application.updates["error"] == {
            "code": ERROR_BUILD_FAILED,
            "message": "gdk component build failed"}, observed
        assert application.updates["ended_at"] == self.NOW, observed
        assert application.audit_action == AUDIT_BUILD_FAILED, observed
        assert application.audit_details["error_code"] == ERROR_BUILD_FAILED

    def test_build_failure_default_message(self):
        """A failed event without an error message records the recorded
        default 'The build failed.'"""
        application = build_events.apply_phase_event(
            "building", {"phase": "failed"}, self.NOW)
        assert application.updates["error"]["message"] == "The build failed."

    @given(published=artifact_lists, unpublished=artifact_lists)
    @settings(max_examples=80, deadline=None)
    def test_publishing_failure_distinct_kind_and_partial_lists(
            self, published, unpublished):
        """A publish-stage failure is DISTINCT from a build failure:
        PUBLISHING_FAILED code, the per-artifact published/unpublished
        lists EXACTLY as reported, and the publishing-failure audit."""
        detail = {
            "phase": "failed",
            "error_kind": ERROR_KIND_PUBLISHING,
            "error_message": "publish step failed",
            "published_artifacts": published,
            "unpublished_artifacts": unpublished,
        }
        application = build_events.apply_phase_event(
            "publishing", detail, self.NOW)
        observed = _observation(published=published, unpublished=unpublished,
                                application=application._asdict())
        assert application.steps == (("publishing", "failed"),), observed
        assert application.updates["error"]["code"] == \
            ERROR_PUBLISHING_FAILED, observed
        assert application.updates["publish_partial"] == {
            "published": published, "unpublished": unpublished}, observed
        assert application.audit_action == AUDIT_BUILD_PUBLISHING_FAILED, \
            observed
        assert application.audit_details["published"] == published, observed
        assert application.audit_details["unpublished"] == unpublished, \
            observed

    def test_stale_build_failure_while_publishing_is_noop(self):
        """A build-stage failure reported while the job is already
        `publishing` is out of order (stale): no-op."""
        application = build_events.apply_phase_event(
            "publishing", {"phase": "failed", "error_kind": "building"},
            self.NOW)
        assert application.is_noop

    def test_unknown_phase_is_noop(self):
        """An unknown phase yields the no-op application."""
        application = build_events.apply_phase_event(
            "building", {"phase": "verifying"}, self.NOW)
        assert application.is_noop


# ===========================================================================
# Req 3.1 / 3.8 — SSM-fallback SAFE preservation (non-bug domain only)
# ===========================================================================

class TestSsmFallbackSafePreservation:
    """**Property 2: Preservation** — the parts of the SSM command
    fallback that are OUTSIDE the bug condition and must survive the
    fix: terminal absorption (a terminal job's outcome is never
    resurrected by a late command notification) and the no-agent-outcome
    no-op for queued/provisioning jobs. The STATUS a running job lands
    in per command status is also preserved (failed / interrupted); the
    generic error CONTENT is deliberately NOT frozen — enriching it is
    the fix.

    **Validates: Requirements 3.1, 3.8, 3.10**
    """

    @given(status=terminal_statuses,
           command_status=st.sampled_from(["Failed", "TimedOut", "Cancelled"]))
    @settings(max_examples=60, deadline=None)
    def test_terminal_job_never_resurrected(self, status, command_status):
        fallback = build_events.decide_ssm_fallback(status, command_status)
        assert fallback.apply is False, _observation(
            status=status, command_status=command_status,
            fallback=fallback._asdict())

    @given(status=st.sampled_from(["queued", "provisioning"]),
           command_status=st.sampled_from(["Failed", "TimedOut", "Cancelled"]))
    @settings(max_examples=30, deadline=None)
    def test_no_agent_outcome_statuses_are_noops(self, status,
                                                 command_status):
        fallback = build_events.decide_ssm_fallback(status, command_status)
        assert fallback.apply is False

    @given(status=st.sampled_from(["building", "publishing"]))
    @settings(max_examples=10, deadline=None)
    def test_cancelled_command_interrupts_running_job(self, status):
        """A Cancelled agent command (torn down under the agent) keeps
        mapping the running job to `interrupted` (Req 3.8 status
        semantics preserved; added diagnostics are additive)."""
        fallback = build_events.decide_ssm_fallback(status, "Cancelled")
        assert fallback.apply is True
        assert fallback.next_status == "interrupted"

    @given(status=st.sampled_from(["building", "publishing"]),
           command_status=st.sampled_from(["Failed", "TimedOut"]))
    @settings(max_examples=20, deadline=None)
    def test_failed_command_fails_running_job(self, status, command_status):
        """A Failed/TimedOut agent command keeps mapping the running job
        to `failed` (status preserved; classification/diagnostic content
        is the fix's additive concern and is NOT asserted here)."""
        fallback = build_events.decide_ssm_fallback(status, command_status)
        assert fallback.apply is True
        assert fallback.next_status == "failed"


# ===========================================================================
# Req 3.6 — Build Jobs API surface (moto handler oracles)
# ===========================================================================

class TestBuildJobsApiEnvelopes:
    """**Property 2: Preservation** — the Build Jobs history/detail/log/
    cancel/retry response envelopes, error envelopes, status values,
    retention window, `events`/`nextToken`, and pagination, recorded on
    unfixed code. Recorded keys/values are asserted as a SUBSET, so
    optional additive diagnostic/timing fields cannot break this
    baseline (they are "excluded from equality only when absent in the
    original").

    **Validates: Requirements 3.6, 3.10**
    """

    def setup_method(self):
        _clear_tables()
        self.user = _jwt_user()

    # ------------------------------------------------------- GET /builds

    def test_history_envelope_ordering_and_pagination(self):
        now = _now_ms()
        ids = []
        for offset in range(5):
            job = _seed_job(created_at=now - offset * 1000)
            ids.append((job["created_at"], job["build_job_id"]))
        expected_order = [job_id for _, job_id in
                          sorted(ids, key=lambda p: (p[0], p[1]),
                                 reverse=True)]

        response = build_jobs.handler(
            _event("/builds", "GET", self.user, query={"limit": "3"}), None)
        assert response["statusCode"] == 200
        body = _body(response)
        _assert_keys_subset(LIST_RESPONSE_KEYS, body)
        assert [j["build_job_id"] for j in body["jobs"]] == \
            expected_order[:3], _observation(body=body)
        assert body["total"] == 5
        assert isinstance(body["nextToken"], str) and body["nextToken"]

        # The opaque token fetches exactly the remainder.
        response2 = build_jobs.handler(
            _event("/builds", "GET", self.user,
                   query={"limit": "3", "nextToken": body["nextToken"]}),
            None)
        body2 = _body(response2)
        assert [j["build_job_id"] for j in body2["jobs"]] == \
            expected_order[3:], _observation(body=body2)
        assert body2["nextToken"] is None

    def test_history_window_excludes_expired_jobs(self):
        now = _now_ms()
        _seed_job(job_id="recent-job", created_at=now - 1000)
        _seed_job(job_id="expired-job",
                  created_at=now - (HISTORY_WINDOW_DAYS + 1)
                  * 24 * 60 * 60 * 1000)
        body = _body(build_jobs.handler(
            _event("/builds", "GET", self.user), None))
        listed = {j["build_job_id"] for j in body["jobs"]}
        assert "recent-job" in listed
        assert "expired-job" not in listed
        assert body["total"] == 1

    def test_invalid_next_token_rejected(self):
        response = build_jobs.handler(
            _event("/builds", "GET", self.user,
                   query={"nextToken": "not-a-token"}), None)
        _assert_error_envelope(response, 400, "INVALID_PARAMETER")

    # -------------------------------------------------- GET /builds/{id}

    def test_detail_envelope(self):
        job = _seed_job(job_id="detail-job", status="succeeded",
                        result={"component_name": "aws.edgeml.dda."
                                                  "LocalServer.amd64",
                                "published_version": "1.2.3",
                                "pushed_image_refs": ["ecr/img:1.2.3"]})
        response = build_jobs.handler(
            _event("/builds/{id}", "GET", self.user, job_id="detail-job"),
            None)
        assert response["statusCode"] == 200
        body = _body(response)
        _assert_keys_subset(DETAIL_RESPONSE_KEYS, body)
        returned = body["job"]
        # Every seeded field keeps its recorded value; additive optional
        # fields are tolerated.
        for key in ("build_job_id", "build_target", "component_name",
                    "required_arch", "execution_mode", "status",
                    "requested_by", "created_at", "result"):
            assert returned[key] == job[key], _observation(
                key=key, returned=returned, seeded=job)

    def test_detail_not_found_envelope(self):
        response = build_jobs.handler(
            _event("/builds/{id}", "GET", self.user, job_id="missing"),
            None)
        _assert_error_envelope(response, 404, "BUILD_JOB_NOT_FOUND")

    # --------------------------------------------- GET /builds/{id}/logs

    def test_logs_envelope_with_existing_stream(self):
        stream = f"stream-{uuid.uuid4()}"
        _LOGS.create_log_stream(logGroupName=_LOG_GROUP,
                                logStreamName=stream)
        _LOGS.put_log_events(
            logGroupName=_LOG_GROUP, logStreamName=stream,
            logEvents=[{"timestamp": _now_ms() - 1000, "message": "line-1"},
                       {"timestamp": _now_ms(), "message": "line-2"}])
        _seed_job(job_id="logs-job", status="building",
                  log={"group": _LOG_GROUP, "stream": stream})

        response = build_jobs.handler(
            _event("/builds/{id}/logs", "GET", self.user, job_id="logs-job"),
            None)
        assert response["statusCode"] == 200
        body = _body(response)
        _assert_keys_subset(LOGS_RESPONSE_KEYS, body)
        assert [e["message"] for e in body["events"]] == ["line-1", "line-2"]
        for event in body["events"]:
            _assert_keys_subset({"timestamp", "message"}, event)
        assert isinstance(body["nextToken"], str) and body["nextToken"]

    def test_logs_missing_stream_is_empty_page(self):
        """A job whose log stream does not exist yields the recorded
        empty page — 200 with events [] and nextToken None (a queued or
        never-started job, i.e. the non-bug domain)."""
        _seed_job(job_id="no-stream-job", status="queued")
        response = build_jobs.handler(
            _event("/builds/{id}/logs", "GET", self.user,
                   job_id="no-stream-job"), None)
        assert response["statusCode"] == 200
        body = _body(response)
        _assert_keys_subset(LOGS_RESPONSE_KEYS, body)
        assert body["events"] == []
        assert body["nextToken"] is None

    def test_logs_not_found_envelope(self):
        response = build_jobs.handler(
            _event("/builds/{id}/logs", "GET", self.user, job_id="missing"),
            None)
        _assert_error_envelope(response, 404, "BUILD_JOB_NOT_FOUND")

    # ---------------------------------------------- POST /builds/{id}/cancel

    def test_cancel_queued_job(self):
        """Queued -> cancelled immediately: 200 {job}, ended_at set, the
        stored status moved, and the build_cancelled audit recorded
        (Req 3.3 API semantics)."""
        _seed_job(job_id="cancel-queued", status="queued")
        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", self.user,
                   job_id="cancel-queued"), None)
        assert response["statusCode"] == 200, _observation(
            body=_body(response))
        body = _body(response)
        _assert_keys_subset(CANCEL_RESPONSE_KEYS, body)
        assert body["job"]["status"] == "cancelled"
        assert body["job"]["ended_at"] is not None
        stored = build_jobs.to_native(_raw_job("cancel-queued"))
        assert stored["status"] == "cancelled"
        cancel_audits = [r for r in _audit_records()
                         if r.get("action") == "build_cancelled"]
        assert cancel_audits, _observation(audits=_audit_records())

    @pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
    def test_cancel_terminal_job_rejected_unchanged(self, status):
        """Terminal -> 409 CANCELLATION_REJECTED with the error envelope
        and the stored job byte-unchanged (Req 3.3)."""
        job_id = f"cancel-terminal-{status}"
        _seed_job(job_id=job_id, status=status, ended_at=_now_ms())
        before = _raw_job(job_id)
        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", self.user, job_id=job_id),
            None)
        body = _assert_error_envelope(response, 409, "CANCELLATION_REJECTED")
        assert body["error"]["details"]["status"] == status
        assert _raw_job(job_id) == before

    def test_cancel_provisioning_job_rejected_unchanged(self):
        _seed_job(job_id="cancel-provisioning", status="provisioning")
        before = _raw_job("cancel-provisioning")
        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", self.user,
                   job_id="cancel-provisioning"), None)
        _assert_error_envelope(response, 409, "CANCELLATION_REJECTED")
        assert _raw_job("cancel-provisioning") == before

    # ----------------------------------------------- POST /builds/{id}/retry

    def test_retry_interrupted_job(self):
        """Interrupted -> 201 {job}: a NEW queued job with the same
        target/mode/server, a retry_of reference, and its own id."""
        source = _seed_job(job_id="retry-source", status="interrupted",
                           execution_mode="dedicated",
                           server_id="srv-1", target="JP6")
        response = build_jobs.handler(
            _event("/builds/{id}/retry", "POST", self.user,
                   job_id="retry-source"), None)
        assert response["statusCode"] == 201, _observation(
            body=_body(response))
        body = _body(response)
        _assert_keys_subset({"job"}, body)
        clone = body["job"]
        assert clone["retry_of"] == "retry-source"
        assert clone["status"] == "queued"
        assert clone["build_target"] == source["build_target"]
        assert clone["component_name"] == source["component_name"]
        assert clone["execution_mode"] == "dedicated"
        assert clone["server_id"] == "srv-1"
        assert clone["build_job_id"] != "retry-source"
        assert _raw_job(clone["build_job_id"]) is not None

    @pytest.mark.parametrize("status",
                             ["queued", "building", "succeeded", "failed",
                              "cancelled"])
    def test_retry_only_for_interrupted(self, status):
        job_id = f"retry-{status}"
        _seed_job(job_id=job_id, status=status)
        response = build_jobs.handler(
            _event("/builds/{id}/retry", "POST", self.user, job_id=job_id),
            None)
        body = _assert_error_envelope(response, 409, "RETRY_NOT_AVAILABLE")
        assert body["error"]["details"]["status"] == status


# ===========================================================================
# Req 3.7 — Target/component/mode mappings and intentional validation
# ===========================================================================

class TestTargetAndModeMappings:
    """**Property 2: Preservation** — `JP5`/`JP6`/`JP7 -> arm64`,
    `AMD64`/`AMD64_NVIDIA -> x86_64`, the exact component identities and
    recipes, the dedicated/ephemeral mode set, and the intentional
    dedicated arch-mismatch validation.

    **Validates: Requirements 3.7, 3.10**
    """

    def test_target_definitions_exact(self):
        assert set(build_domain.SUPPORTED_BUILD_TARGETS) == set(TARGETS)
        for target, (component, recipe, arch) in TARGET_ORACLE.items():
            definition = build_domain.target_definition(target)
            observed = _observation(target=target, definition=definition)
            assert definition["component_name"] == component, observed
            assert definition["recipe"] == recipe, observed
            assert definition["required_arch"] == arch, observed
            assert build_domain.required_arch_for_target(target) == arch

    def test_unsupported_target_rejected(self):
        assert not build_domain.is_supported_target("JP4")
        with pytest.raises(ValueError):
            build_domain.target_definition("JP4")

    def test_execution_mode_set(self):
        assert set(build_domain.EXECUTION_MODES) == set(EXECUTION_MODES)

    @given(targets=st.lists(st.sampled_from(TARGETS), min_size=1,
                            max_size=4),
           mode=st.sampled_from(EXECUTION_MODES))
    @settings(max_examples=80, deadline=None)
    def test_created_jobs_carry_identity_order_and_chaining(self, targets,
                                                            mode):
        """One queued job per target in request order, carrying the exact
        component identity and required architecture, predecessor-chained
        within the request with the original submission time (Req 3.4
        ordering inputs, Req 3.7 identities)."""
        job_ids = [f"job-{index}" for index in range(len(targets))]
        server_id = "srv-fixed" if mode == "dedicated" else None
        jobs = build_domain.create_build_jobs(
            targets=list(targets), execution_mode=mode, server_id=server_id,
            request_id="req-1", job_ids=job_ids, requested_by="user-1",
            created_at=1_754_000_000_000,
            config_snapshot={"max_runtime_hours": 4})
        assert [j["build_target"] for j in jobs] == list(targets)
        for order, job in enumerate(jobs):
            component, _, arch = TARGET_ORACLE[targets[order]]
            observed = _observation(order=order, job=job)
            assert job["component_name"] == component, observed
            assert job["required_arch"] == arch, observed
            assert job["status"] == "queued", observed
            assert job["request_order"] == order, observed
            assert job["predecessor_job_id"] == (
                job_ids[order - 1] if order else None), observed
            assert job["server_id"] == server_id, observed
            assert job["created_at"] == 1_754_000_000_000, observed

    def test_dedicated_arch_mismatch_intentionally_rejected(self):
        """An arm64 target on an x86_64 dedicated server keeps failing
        the intentional server_arch_mismatch validation."""
        servers = [{"server_id": "srv-x86", "lifecycle_state": "running",
                    "arch": "x86_64"}]
        result = build_domain.validate_build_request(
            {"targets": ["JP5"], "execution_mode": "dedicated",
             "server_id": "srv-x86"}, servers)
        assert not result.valid
        assert any(e["rule"] == "server_arch_mismatch"
                   for e in result.errors), _observation(
            errors=[dict(e) for e in result.errors])

    def test_supported_combination_accepted(self):
        """A matching dedicated combination and any ephemeral combination
        keep validating (both modes remain supported for every target)."""
        servers = [{"server_id": "srv-arm", "lifecycle_state": "running",
                    "arch": "arm64"}]
        assert build_domain.validate_build_request(
            {"targets": ["JP5", "JP6"], "execution_mode": "dedicated",
             "server_id": "srv-arm"}, servers).valid
        for target in TARGETS:
            assert build_domain.validate_build_request(
                {"targets": [target], "execution_mode": "ephemeral"},
                servers).valid

    def test_agent_script_target_argument_mapping(self):
        """The on-disk agent maps the targets to exactly the recorded
        portal-build.sh arguments (JP5 -> aarch64 5, JP6 -> aarch64 6,
        AMD64 -> x86_64, AMD64_NVIDIA -> x86_64_nvidia)."""
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            script = handle.read()
        assert "JP5)          BUILD_ARGS=(aarch64 5)" in script
        assert "JP6)          BUILD_ARGS=(aarch64 6)" in script
        assert "AMD64)        BUILD_ARGS=(x86_64)" in script
        assert "AMD64_NVIDIA) BUILD_ARGS=(x86_64_nvidia)" in script


# ===========================================================================
# Req 3.2 / 3.4 / 3.11 — Locks, pgrep gate, flock, serialization,
# predecessor gates, ordering, promotion
# ===========================================================================

class TestDispatchAndSerializationContracts:
    """**Property 2: Preservation** — the DynamoDB single-slot lock,
    pre-dispatch pgrep gate, on-server flock command contract,
    serialization watchdog, stale-release protection, predecessor gates,
    original submission ordering, and oldest-eligible queue promotion.

    **Validates: Requirements 3.2, 3.4, 3.10, 3.11**
    """

    def setup_method(self):
        _clear_tables()

    # --------------------------------------- DynamoDB single-slot lock

    def test_single_slot_allocation_lock(self):
        """The server's single running slot: first allocation wins, a
        concurrent second allocation is refused, release requires the
        owning job (stale-release protection), and release frees the
        slot for the next allocation."""
        _SERVERS.put_item(Item={"server_id": "srv-lock"})
        assert build_dispatcher.allocate_server("srv-lock", "job-a") is True
        assert build_dispatcher.allocate_server("srv-lock", "job-b") is False
        # Stale release by a non-owner can never free the slot.
        assert build_dispatcher.release_server("srv-lock", "job-b") is False
        held = _SERVERS.get_item(Key={"server_id": "srv-lock"})["Item"]
        assert held.get("running_build_job_id") == "job-a"
        # The owner's release frees the slot exactly once.
        assert build_dispatcher.release_server("srv-lock", "job-a") is True
        freed = _SERVERS.get_item(Key={"server_id": "srv-lock"})["Item"]
        assert "running_build_job_id" not in freed
        assert build_dispatcher.release_server("srv-lock", "job-a") is False
        assert build_dispatcher.allocate_server("srv-lock", "job-b") is True

    def test_conditional_transition_protects_terminal_state(self):
        """`transition_job` is conditional on the expected status: a
        stale writer cannot resurrect a moved job (Req 3.11)."""
        _seed_job(job_id="transition-job", status="building")
        assert build_dispatcher.transition_job(
            "transition-job", "building", "failed",
            extra={"ended_at": _now_ms()}) is True
        # The stale duplicate loses the conditional write.
        assert build_dispatcher.transition_job(
            "transition-job", "building", "interrupted") is False
        stored = build_jobs.to_native(_raw_job("transition-job"))
        assert stored["status"] == "failed"

    # ---------------------------------------------- pre-dispatch pgrep

    def test_build_process_patterns_frozen(self):
        assert tuple(build_planner.BUILD_PROCESS_PATTERNS) == \
            BUILD_PROCESS_PATTERNS

    def test_predispatch_defers_on_running_build(self):
        """A detected build process defers the job back to its queue
        with the ORIGINAL created_at (submission order preserved) and
        records the verification time."""
        job = {"build_job_id": "gate-job", "status": "queued",
               "created_at": 1111}
        now = 999_000
        decision = build_planner.decide_predispatch(
            job, "4242 gdk component build --name X\n", now)
        observed = _observation(decision=decision._asdict())
        assert decision.action == "defer", observed
        assert decision.status == "queued", observed
        assert decision.created_at == 1111, observed
        assert decision.deferred_at == now, observed
        assert decision.build_processes, observed

    def test_predispatch_starts_when_clean(self):
        decision = build_planner.decide_predispatch(
            {"build_job_id": "gate-job", "status": "queued",
             "created_at": 1111},
            "7 unrelated-daemon --flag\n", 999_000)
        assert decision.action == "start"
        assert decision.build_processes == ()

    @given(elapsed=st.integers(min_value=0, max_value=20 * 60 * 1000))
    @settings(max_examples=60, deadline=None)
    def test_reverification_five_minute_cadence(self, elapsed):
        """Re-verification is due iff at least the 5-minute interval
        elapsed since the last attempt (never verified -> immediately)."""
        base = 1_754_000_000_000
        due = build_planner.is_reverification_due(base, base + elapsed)
        assert due == (elapsed >= PREDISPATCH_RETRY_INTERVAL_MS)
        assert build_planner.is_reverification_due(None, base) is True

    # -------------------------------------------- on-server flock contract

    def test_agent_flock_contract(self):
        """The on-server agent still takes the non-blocking flock on
        /var/lock/dda-build.lock and defers with exit 75 when held."""
        with open(AGENT_SCRIPT, encoding="utf-8") as handle:
            script = handle.read()
        assert f'LOCK_FILE="{AGENT_LOCK_FILE}"' in script
        assert "flock -n 9" in script
        assert f"exit {AGENT_EXIT_LOCK_HELD}" in script

    def test_agent_command_invokes_agent_with_recorded_arguments(self):
        """The dispatcher's command text still executes
        scripts/portal-build-agent.sh with the BUILD_JOB_ID /
        BUILD_TARGET / EVENT_BUS argument contract and propagates the
        agent's exit status."""
        job = {"build_job_id": "cmd-job", "build_target": "AMD64",
               "execution_mode": "dedicated", "server_id": "srv-1",
               "config_snapshot": {"max_runtime_hours": 4}}
        command = build_dispatcher.agent_command(job)
        observed = _observation(command=command)
        assert AGENT_SCRIPT_RELPATH in command, observed
        assert "BUILD_JOB_ID=cmd-job" in command, observed
        assert "BUILD_TARGET=AMD64" in command, observed
        assert re.search(r"EVENT_BUS=\S+", command), observed
        assert command.rstrip().endswith('exit "$AGENT_RUN_STATUS"') or \
            re.search(r'exit "\$\w+"\s*$', command), observed

    # ------------------------------------------- serialization watchdog

    @given(elapsed=st.integers(min_value=0, max_value=20 * 60 * 1000))
    @settings(max_examples=60, deadline=None)
    def test_serialization_check_cadence(self, elapsed):
        base = 1_754_000_000_000
        due = build_planner.is_serialization_check_due(base, base + elapsed)
        assert due == (elapsed >= SERIALIZATION_CHECK_INTERVAL_MS)
        assert build_planner.is_serialization_check_due(None, base) is True

    @given(count=st.integers(min_value=0, max_value=6),
           job_ids=st.lists(st.uuids().map(str), max_size=4))
    @settings(max_examples=80, deadline=None)
    def test_serialization_violation_decision(self, count, job_ids):
        """Two or more detected build processes -> stop-all/fail-all of
        every associated job with the SERIALIZATION_VIOLATION error;
        zero or one -> no violation, nothing stopped, nothing failed."""
        decision = build_planner.decide_serialization_violation(
            "srv-1", count, job_ids)
        observed = _observation(count=count, job_ids=job_ids,
                                decision=decision._asdict())
        if count >= 2:
            assert decision.violation and decision.stop_all, observed
            assert decision.failed_job_ids == tuple(job_ids), observed
            assert decision.error == SERIALIZATION_VIOLATION_ERROR, observed
        else:
            assert not decision.violation and not decision.stop_all, observed
            assert decision.failed_job_ids == (), observed
            assert decision.error is None, observed

    # ------------------------- predecessor gates, ordering, promotion

    def test_predecessor_gate(self):
        """A queued job with a predecessor is dispatchable iff the
        predecessor is terminal; unknown predecessor state is NOT
        eligible (positively established only)."""
        job = {"build_job_id": "j2", "status": "queued",
               "predecessor_job_id": "j1"}
        assert build_planner.is_dispatch_eligible(job, "building") is False
        assert build_planner.is_dispatch_eligible(job, None) is False
        for terminal in sorted(TERMINAL_STATUSES):
            assert build_planner.is_dispatch_eligible(job, terminal) is True
        first = {"build_job_id": "j1", "status": "queued",
                 "predecessor_job_id": None}
        assert build_planner.is_dispatch_eligible(first) is True
        assert build_planner.is_dispatch_eligible(
            {"build_job_id": "j1", "status": "building",
             "predecessor_job_id": None}) is False

    @given(created=st.lists(st.integers(min_value=0, max_value=10**6),
                            min_size=2, max_size=6, unique=True))
    @settings(max_examples=60, deadline=None)
    def test_original_submission_ordering(self, created):
        """Eligible queued jobs keep ascending original created_at
        ordering (Req 3.4)."""
        jobs = [{"build_job_id": f"job-{index}", "status": "queued",
                 "predecessor_job_id": None, "created_at": created_at}
                for index, created_at in enumerate(created)]
        ordered = build_planner.eligible_queued_jobs(jobs)
        assert [j["created_at"] for j in ordered] == sorted(created)

    @given(created=st.lists(st.integers(min_value=0, max_value=10**6),
                            min_size=1, max_size=6, unique=True))
    @settings(max_examples=60, deadline=None)
    def test_oldest_eligible_queue_promotion(self, created):
        """promote_next selects exactly the OLDEST queued job of the
        server's queue (Req 3.4, 3.11)."""
        jobs = [{"build_job_id": f"job-{index}", "status": "queued",
                 "server_id": "srv-1", "created_at": created_at}
                for index, created_at in enumerate(created)]
        # Noise: a running job and another server's queue never win.
        jobs.append({"build_job_id": "running", "status": "building",
                     "server_id": "srv-1", "created_at": -1})
        jobs.append({"build_job_id": "other", "status": "queued",
                     "server_id": "srv-2", "created_at": -1})
        selected = build_planner.promote_next("srv-1", jobs)
        assert selected is not None
        assert selected["created_at"] == min(created)
        assert build_planner.promote_next("srv-empty", jobs) is None

    def test_promotion_only_on_terminal_status(self):
        for status in sorted(ALL_STATUSES):
            assert build_planner.should_promote(status) == \
                (status in TERMINAL_STATUSES)

    def test_single_start_per_server_per_plan(self):
        """Within one plan, at most one job starts per dedicated server:
        the oldest eligible takes the slot, every other job targeting
        that server queues (Req 3.2)."""
        jobs = [
            {"build_job_id": "j-old", "status": "queued",
             "predecessor_job_id": None, "created_at": 100,
             "execution_mode": "dedicated", "server_id": "srv-1"},
            {"build_job_id": "j-new", "status": "queued",
             "predecessor_job_id": None, "created_at": 200,
             "execution_mode": "dedicated", "server_id": "srv-1"},
        ]
        servers = [{"server_id": "srv-1"}]
        decisions = build_planner.plan_dedicated_dispatch(jobs, servers)
        by_job = {d.build_job_id: d for d in decisions}
        assert by_job["j-old"].action == "start"
        assert by_job["j-new"].action == "queue"
        assert by_job["j-new"].status == "queued"
        # Occupied server: nothing starts.
        occupied = [{"server_id": "srv-1",
                     "running_build_job_id": "job-running"}]
        decisions = build_planner.plan_dedicated_dispatch(jobs, occupied)
        assert all(d.action == "queue" for d in decisions)

    def test_dead_server_sweep_semantics(self):
        """Queued jobs are swept (failed) iff the server is stopped or
        terminated; transitional states sweep nothing (Req 3.8)."""
        jobs = [{"build_job_id": "q1", "status": "queued",
                 "server_id": "srv-1", "created_at": 2},
                {"build_job_id": "q0", "status": "queued",
                 "server_id": "srv-1", "created_at": 1},
                {"build_job_id": "running", "status": "building",
                 "server_id": "srv-1", "created_at": 0}]
        swept = build_planner.sweep_dead_server(
            {"server_id": "srv-1", "lifecycle_state": "stopped"}, jobs)
        assert swept.sweep is True
        assert swept.failed_job_ids == ("q0", "q1")
        assert swept.error
        for state in ("running", "stopping", "shutting-down", "pending"):
            decision = build_planner.sweep_dead_server(
                {"server_id": "srv-1", "lifecycle_state": state}, jobs)
            assert decision.sweep is False
            assert decision.failed_job_ids == ()


# ===========================================================================
# Req 3.3 — Cancellation decision semantics (pure oracle)
# ===========================================================================

class TestCancellationDecisionBaseline:
    """**Property 2: Preservation** — verified cancellation and
    fail-closed behavior over every status x confirmation combination.

    **Validates: Requirements 3.3, 3.10**
    """

    @given(status=statuses,
           stop_confirmed=st.sampled_from([None, False, True]))
    @settings(max_examples=120, deadline=None)
    def test_cancellation_decision_matrix(self, status, stop_confirmed):
        decision = build_domain.decide_cancellation(
            status, stop_confirmed=stop_confirmed, server_id="srv-name")
        observed = _observation(status=status, stop_confirmed=stop_confirmed,
                                decision=decision._asdict())
        if status in TERMINAL_STATUSES:
            # Terminal: rejected unchanged (Req 4.8 semantics preserved).
            assert not decision.cancelled, observed
            assert decision.next_status == status, observed
            assert decision.errors and decision.errors[0]["rule"] == \
                "cancel_terminal_status", observed
        elif status == "queued":
            # Queued: cancelled immediately and removed from the queue.
            assert decision.cancelled, observed
            assert decision.next_status == "cancelled", observed
            assert decision.remove_from_queue is True, observed
        elif status in ("building", "publishing"):
            # Running: cancelled iff the stop is pgrep-confirmed;
            # False or unknown is fail-closed.
            if stop_confirmed is True:
                assert decision.cancelled, observed
                assert decision.next_status == "cancelled", observed
                assert decision.remove_from_queue is False, observed
            else:
                assert not decision.cancelled, observed
                assert decision.next_status == status, observed
                assert decision.errors and decision.errors[0]["rule"] == \
                    "cancel_stop_not_confirmed", observed
                assert "srv-name" in decision.errors[0]["message"], observed
        else:  # provisioning
            assert not decision.cancelled, observed
            assert decision.next_status == status, observed
            assert decision.errors and decision.errors[0]["rule"] == \
                "cancel_not_cancellable", observed

    def test_fail_closed_verification_parsing(self):
        """Unparseable pgrep output is never treated as confirmation."""
        assert build_jobs.parse_build_process_count(
            "BUILD_PROCESS_COUNT=0") == 0
        assert build_jobs.parse_build_process_count(
            "BUILD_PROCESS_COUNT=2") == 2
        assert build_jobs.parse_build_process_count(
            "BUILD_PROCESS_COUNT=zero") is None
        assert build_jobs.parse_build_process_count("no marker") is None
        assert build_jobs.parse_build_process_count("") is None


# ===========================================================================
# Req 3.9 — Ephemeral runner lifecycle (pure oracles)
# ===========================================================================

class TestEphemeralLifecycleBaseline:
    """**Property 2: Preservation** — one runner per dispatched job with
    snapshot-only sizing, and the termination-retry / orphaned-runner
    cleanup cadence.

    **Validates: Requirements 3.9, 3.10, 3.11**
    """

    @given(target=st.sampled_from(TARGETS))
    @settings(max_examples=20, deadline=None)
    def test_runner_plan_snapshot_sizing(self, target):
        _, _, arch = TARGET_ORACLE[target]
        snapshot = {"arm64_instance_type": "m6g.8xlarge",
                    "x86_64_instance_type": "m6i.8xlarge",
                    "volume_size_gb": 250,
                    "use_spot_for_ephemeral": True}
        plan = build_planner.plan_runner({
            "build_job_id": "runner-job", "build_target": target,
            "execution_mode": "ephemeral", "config_snapshot": snapshot})
        observed = _observation(target=target, plan=plan._asdict())
        assert plan.build_job_id == "runner-job", observed
        assert plan.arch == arch, observed
        assert plan.instance_type == ("m6g.8xlarge" if arch == "arm64"
                                      else "m6i.8xlarge"), observed
        assert plan.volume_size_gb == 250, observed
        assert plan.spot is True, observed
        assert plan.status == "provisioning", observed

    def test_runner_plan_rejects_dedicated(self):
        with pytest.raises(ValueError):
            build_planner.plan_runner({
                "build_job_id": "x", "build_target": "AMD64",
                "execution_mode": "dedicated"})

    def test_one_runner_per_eligible_ephemeral_job(self):
        jobs = [
            {"build_job_id": "e1", "status": "queued",
             "predecessor_job_id": None, "created_at": 1,
             "execution_mode": "ephemeral", "build_target": "JP5"},
            {"build_job_id": "e2", "status": "building",
             "predecessor_job_id": None, "created_at": 2,
             "execution_mode": "ephemeral", "build_target": "JP5"},
            {"build_job_id": "d1", "status": "queued",
             "predecessor_job_id": None, "created_at": 3,
             "execution_mode": "dedicated", "server_id": "srv-1",
             "build_target": "AMD64"},
        ]
        plans = build_planner.plan_ephemeral_provisioning(jobs)
        assert [p.build_job_id for p in plans] == ["e1"]

    @given(now_offset=st.integers(min_value=0, max_value=2 * 60 * 60 * 1000),
           last_offset=st.one_of(
               st.none(),
               st.integers(min_value=0, max_value=60 * 60 * 1000)),
           already_notified=st.booleans())
    @settings(max_examples=120, deadline=None)
    def test_termination_retry_cadence(self, now_offset, last_offset,
                                       already_notified):
        """Retry iff strictly inside the 1-hour window AND at least
        10 minutes since the last attempt (immediately when none);
        the orphaned-runner notification fires exactly once at window
        exhaustion."""
        first_failed_at = 1_754_000_000_000
        now = first_failed_at + now_offset
        last_attempt_at = (None if last_offset is None
                           else first_failed_at + last_offset)
        decision = build_planner.decide_termination_retry(
            first_failed_at, last_attempt_at, now, already_notified)
        observed = _observation(now_offset=now_offset,
                                last_offset=last_offset,
                                already_notified=already_notified,
                                decision=decision._asdict())
        if now_offset < TERMINATION_RETRY_WINDOW_MS:
            expected_retry = (last_attempt_at is None or
                              (now - last_attempt_at)
                              >= TERMINATION_RETRY_INTERVAL_MS)
            assert decision.retry == expected_retry, observed
            assert decision.notify_orphaned is False, observed
        else:
            assert decision.retry is False, observed
            assert decision.notify_orphaned == (not already_notified), \
                observed


# ===========================================================================
# Req 3.8 / 3.12 — Legacy runtime deadline compatibility (pure oracle)
# ===========================================================================

class TestRuntimeDeadlineCompatibility:
    """**Property 2: Preservation** — for LEGACY-shaped jobs (only
    `started_at` plus a snapshotted `max_runtime_hours`), the runtime
    watchdog keeps its strict boundary and snapshot fallback: queue and
    predecessor wait remain queued and untimed, `now == deadline` is not
    expired, `now > deadline` is expired, a missing start time never
    times out, and the job's OWN snapshot (default 4 hours) is the
    budget.

    **Validates: Requirements 3.8, 3.10, 3.12**
    """

    STARTED_AT = 1_754_000_000_000

    def _job(self, status, hours=None, started_at="default"):
        job = {"build_job_id": "runtime-job", "status": status}
        if started_at == "default":
            job["started_at"] = self.STARTED_AT
        elif started_at is not None:
            job["started_at"] = started_at
        if hours is not None:
            job["config_snapshot"] = {"max_runtime_hours": hours}
        return job

    @given(status=st.sampled_from(["queued", "provisioning"]),
           elapsed_hours=st.integers(min_value=0, max_value=100))
    @settings(max_examples=60, deadline=None)
    def test_queue_and_provisioning_wait_never_times_out(self, status,
                                                         elapsed_hours):
        """A job waiting behind an occupied server or an unfinished
        predecessor remains queued no matter how long: the runtime
        watchdog only ever applies to building/publishing."""
        job = self._job(status, hours=1)
        now = self.STARTED_AT + elapsed_hours * MS_PER_HOUR
        decision = build_planner.decide_runtime_timeout(job, now)
        observed = _observation(status=status, elapsed_hours=elapsed_hours,
                                decision=decision._asdict())
        assert decision.timed_out is False, observed
        assert decision.status == status, observed
        assert decision.error is None, observed

    @given(status=st.sampled_from(["building", "publishing"]),
           hours=st.integers(min_value=1, max_value=12))
    @settings(max_examples=60, deadline=None)
    def test_exact_deadline_is_not_expired(self, status, hours):
        """`now == deadline` is NOT expired (strict boundary, Req 3.12)."""
        job = self._job(status, hours=hours)
        deadline = self.STARTED_AT + hours * MS_PER_HOUR
        decision = build_planner.decide_runtime_timeout(job, deadline)
        assert decision.timed_out is False, _observation(
            status=status, hours=hours, decision=decision._asdict())
        assert decision.status == status
        assert decision.error is None

    @given(status=st.sampled_from(["building", "publishing"]),
           hours=st.integers(min_value=1, max_value=12),
           overrun=st.integers(min_value=1, max_value=MS_PER_HOUR))
    @settings(max_examples=80, deadline=None)
    def test_past_deadline_is_expired(self, status, hours, overrun):
        """`now > deadline` IS expired: the job fails with a recorded
        timeout error (content may gain diagnostics; presence and the
        failed status are frozen)."""
        job = self._job(status, hours=hours)
        now = self.STARTED_AT + hours * MS_PER_HOUR + overrun
        decision = build_planner.decide_runtime_timeout(job, now)
        observed = _observation(status=status, hours=hours, overrun=overrun,
                                decision=decision._asdict())
        assert decision.timed_out is True, observed
        assert decision.status == "failed", observed
        assert decision.error, observed

    def test_snapshot_fallback_default_four_hours(self):
        """A legacy job with no snapshotted max_runtime_hours keeps the
        documented 4-hour fallback (Req 3.12)."""
        job = self._job("building")  # no config_snapshot at all
        boundary = self.STARTED_AT + DEFAULT_MAX_RUNTIME_HOURS * MS_PER_HOUR
        assert build_planner.decide_runtime_timeout(
            job, boundary).timed_out is False
        assert build_planner.decide_runtime_timeout(
            job, boundary + 1).timed_out is True
        assert build_planner.max_runtime_ms(None) == \
            DEFAULT_MAX_RUNTIME_HOURS * MS_PER_HOUR

    @given(hours=st.integers(min_value=1, max_value=12))
    @settings(max_examples=30, deadline=None)
    def test_snapshotted_hours_remain_the_budget(self, hours):
        """The job's OWN snapshotted max_runtime_hours is the budget —
        never current configuration (legacy compatibility, Req 3.12)."""
        assert build_planner.max_runtime_ms(
            {"max_runtime_hours": hours}) == hours * MS_PER_HOUR

    def test_unknown_start_time_never_times_out(self):
        """A running job without a recorded start time is never timed
        out (fail-safe arithmetic)."""
        job = self._job("building", hours=1, started_at=None)
        job.pop("started_at", None)
        decision = build_planner.decide_runtime_timeout(
            job, self.STARTED_AT + 100 * MS_PER_HOUR)
        assert decision.timed_out is False
        assert decision.error is None
