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
Build source selection PRESERVATION baselines
(build-source-selection, task 2).

**Property 2: Preservation** — Legacy dedicated servers need no
re-bootstrap (Req 5.3).
**Property 14: Preservation** — Everything outside the selected source is
unchanged (Req 7.1, 7.3, 7.4, 7.5, 7.6).

Observation-first: every oracle in this file was RECORDED from the
CURRENT (unfixed) code, before any Increment A change
(`build_source.py` does not exist yet; `build_dispatcher.BUILD_REPO_DIR`
still defaults to `/opt/dda/DefectDetectionApplication`). These tests
MUST PASS now and MUST keep passing verbatim after the Increment A fixes
(task 6 re-runs this file unchanged).

What is pinned here:

* **Req 7.1 — the zero-effort submission path.** The exact `POST /builds`
  request body a no-selection submission sends (`targets`,
  `execution_mode`, plus `server_id` for dedicated mode and nothing
  else), the `201` response envelope (`request_id`, `jobs`), and the
  created Build_Job records: one queued job per target in request order,
  shared `request_id`, 0-based `request_order`, `predecessor_job_id`
  chaining, `requested_by`, `created_at`, and the full record key set.
* **Req 7.5 — `config_snapshot`.** The key set and value types
  `effective_build_config()` produces and `build_domain.create_build_jobs`
  snapshots, including `source_ref: None` ("None -> the repo default
  branch"), and the per-job deep-copy isolation. Asserted as a SUPERSET
  so Increment B's additive keys (`repository`, `default_repository`)
  cannot break the baseline, while every recorded key keeps its recorded
  name, value and type — exactly the "additive keys only" contract.
* **Req 7.3 — the `server-index` None-key omission.** For an ephemeral
  job with `server_id = None` the STORED item omits `server_id` while
  `predecessor_job_id: None` is retained as NULL, and the returned
  in-memory record still carries `server_id: None` (the API contract).
  This pins the fix that already landed
  (`without_null_index_keys` / `INDEXED_KEY_ATTRIBUTES` in
  `build_jobs.py`); the BuildJobs table here carries the deployed
  `status-index` / `server-index` / `request-index` GSIs, so a
  regression is a real DynamoDB `ValidationException`, not a soft
  assertion.
* **Req 7.4 — the build API surface.** `GET /builds` shape, 90-day
  window, most-recent-first ordering, opaque pagination token semantics,
  `GET /builds/{id}`, `GET /builds/{id}/logs`, cancel semantics
  (queued -> cancelled + audit; terminal -> 409 unchanged) and the
  `{"error": {code, message, details}}` envelope.
* **Req 7.6 — the `portal-build-agent.sh` argument contract.** exit 64
  on a missing or unknown argument, exit 75 on a held build lock, and
  the `building` / `succeeded` / `failed` phase-event detail field sets
  (including the publish-stage extras).
* **Req 5.3 / Property 2 — legacy dedicated servers.** Server records
  carrying NO recorded repository directory (every server bootstrapped
  before this change) are the input domain: the directory those servers'
  bootstrap actually used is `/home/ubuntu/DefectDetectionApplication`,
  resolution for such a record yields the module's configured repository
  directory (no server field is consulted), and the dedicated dispatch
  path is otherwise identical for the field being absent, `None`, or
  empty.

Property 2 is expressed against CURRENT behavior on purpose: the shared
resolver does not exist yet (task 3.1 creates
`edge-cv-portal/backend/functions/build_source.py`). Nothing here imports
that module unconditionally — the invariants are stated so that they hold
today AND after the resolver lands, and the resolver is additionally
asserted opportunistically once it exists
(`_resolve_repo_dir` / `_default_repo_dir` below).

SAFETY (absolute): no test in this file launches EC2 compute, sends a
real SSM command, calls real AWS, or starts a real build.
* Every AWS interaction runs against moto (`mock_aws` started at module
  scope before any handler import).
* `invoke_dispatcher` is a no-op (its function-name env var is unset) and
  is additionally patched on the submit paths.
* The dedicated dispatch oracle patches `build_dispatcher.ssm` with a
  recording stub and `run_shell_sync` with a canned clean pgrep output,
  so no SendCommand of any kind leaves the process.
* The real `scripts/portal-build-agent.sh` is executed ONLY on paths that
  exit before Step 4 (`bash ./portal-build.sh`): the exit-64 argument
  errors (which precede the lock entirely) and the exit-75 held-lock path
  (driven with a deliberately UNSUPPORTED `BUILD_TARGET`, so even if the
  lock were unexpectedly free the script exits 64 at target validation
  and can never reach a build).
* The phase-event detail sets are recorded by running a byte-identical
  copy (sha256 asserted) of the real script inside a temporary sandbox
  repository whose `portal-build.sh` is a stub, with `EVENT_BUS` unset so
  `emit_event` prints the detail instead of calling `aws events`.
"""
import contextlib
import fcntl
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import uuid
from decimal import Decimal
from unittest import mock

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

_SUFFIX = "source-preserve"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"
_USER_ROLES_TABLE = f"dda-portal-user-roles-{_SUFFIX}"
_AUDIT_TABLE = f"dda-portal-audit-log-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ["USER_ROLES_TABLE"] = _USER_ROLES_TABLE
os.environ["AUDIT_LOG_TABLE"] = _AUDIT_TABLE
# Unset so invoke_dispatcher is a logged no-op: nothing can be dispatched.
os.environ.pop("BUILD_DISPATCHER_FUNCTION_NAME", None)
# No repository URL: runner_bootstrap_user_data() must stay inert here.
os.environ.pop("BUILD_REPO_URL", None)
# Deliberately NOT set, so BUILD_REPO_DIR keeps its module default (the
# deployed Lambda sets no override either — design A1).
os.environ.pop("BUILD_REPO_DIR", None)

# Import boto3 (and thus botocore/urllib3) from the test environment
# BEFORE the Lambda layer directory joins sys.path: the layer vendors its
# own urllib3 build targeting the Lambda runtime.
import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling
# shim in test_build_authorization_preservation.py).
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
for _module in ("build_jobs", "build_dispatcher", "build_fleet",
                "build_domain", "build_planner", "rbac_middleware",
                "shared_utils"):
    sys.modules.pop(_module, None)

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the DEPLOYED schema, GSIs included (same fixture as the
# sibling test_build_authorization_preservation.py): an item whose
# indexed key attribute is present with the wrong type is rejected by
# DynamoDB, which is what makes the Req 7.3 baseline real.
_DDB.create_table(
    TableName=_JOBS_TABLE,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[
        {"AttributeName": "build_job_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "created_at", "AttributeType": "N"},
        {"AttributeName": "server_id", "AttributeType": "S"},
        {"AttributeName": "request_id", "AttributeType": "S"},
        {"AttributeName": "request_order", "AttributeType": "N"},
    ],
    GlobalSecondaryIndexes=[
        {
            "IndexName": "status-index",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "server-index",
            "KeySchema": [
                {"AttributeName": "server_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "request-index",
            "KeySchema": [
                {"AttributeName": "request_id", "KeyType": "HASH"},
                {"AttributeName": "request_order", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
    BillingMode="PAY_PER_REQUEST",
)
for _name, _key in ((_SERVERS_TABLE, "server_id"),
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

_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)
_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_dispatcher  # noqa: E402
import build_domain  # noqa: E402
import build_fleet  # noqa: E402
import build_jobs  # noqa: E402


# ---------------------------------------------------------------------------
# THE ORACLE — recorded from the CURRENT (unfixed) code.
# ---------------------------------------------------------------------------

TARGETS = (build_domain.TARGET_JP5, build_domain.TARGET_JP6,
           build_domain.TARGET_AMD64, build_domain.TARGET_AMD64_NVIDIA)
MODE_EPHEMERAL = build_domain.EXECUTION_MODE_EPHEMERAL
MODE_DEDICATED = build_domain.EXECUTION_MODE_DEDICATED

#: The EXACT body a zero-effort (no repository, no branch) submission
#: sends today: nothing but the targets and the execution mode, plus
#: `server_id` in dedicated mode (Req 7.1). Increment B may ADD optional
#: `repository` / `source_ref` fields; a submission that selects neither
#: must keep sending exactly these keys.
NO_SELECTION_BODY_KEYS_EPHEMERAL = {"targets", "execution_mode"}
NO_SELECTION_BODY_KEYS_DEDICATED = {"targets", "execution_mode", "server_id"}

#: `POST /builds` success envelope (Req 7.1).
SUBMIT_STATUS = 201
SUBMIT_RESPONSE_KEYS = {"request_id", "jobs"}

#: Full key set of a created/persisted Build_Job record: the 13 keys
#: `build_domain.create_build_jobs` produces plus the two
#: `build_jobs.put_new_job` adds (`log`, `ttl`) — Req 7.1.
JOB_RECORD_KEYS = {
    "build_job_id", "request_id", "request_order", "predecessor_job_id",
    "build_target", "component_name", "required_arch", "execution_mode",
    "server_id", "status", "requested_by", "created_at", "config_snapshot",
    "log", "ttl",
}

#: `config_snapshot` key set with the recorded default value and value
#: type of each key (Req 7.5). `source_ref: None` is the existing
#: "None -> the repo default branch" key that Increment B reuses.
CONFIG_SNAPSHOT_ORACLE = {
    "arm64_instance_type": ("m6g.4xlarge", str),
    "x86_64_instance_type": ("m6i.4xlarge", str),
    # 100 -> 200: build-fleet-execution-failures storage amendment
    # (Req 2.20) raised the documented ephemeral volume default.
    "volume_size_gb": (200, int),
    "region": ("us-east-1", str),
    "max_runtime_hours": (4, int),
    "use_spot_for_ephemeral": (False, bool),
    "source_ref": (None, type(None)),
}

#: Attributes that are a key of the BuildJobs table or of one of its GSIs
#: and are therefore OMITTED from the stored item when None (Req 7.3).
INDEXED_KEY_ATTRIBUTES_ORACLE = {
    "build_job_id", "status", "created_at",
    "server_id", "request_id", "request_order",
}

#: `GET /builds` / `GET /builds/{id}` / `GET /builds/{id}/logs` and the
#: non-authorization error envelope (Req 7.4).
LIST_RESPONSE_KEYS = {"jobs", "nextToken", "total"}
DETAIL_RESPONSE_KEYS = {"job"}
LOGS_RESPONSE_KEYS = {"events", "nextToken"}
CANCEL_RESPONSE_KEYS = {"job"}
ERROR_ENVELOPE_KEYS = {"code", "message", "details"}

#: The location every dedicated server bootstrapped before this change
#: actually cloned the repository into — `build_fleet.USER_DATA_TEMPLATE`
#: (Req 5.3, Property 2). Those servers carry no recorded directory, so
#: resolution must land here after task 3.1 too.
LEGACY_BOOTSTRAP_REPO_DIR = "/home/ubuntu/DefectDetectionApplication"

#: Bootstrap completion marker the dedicated template already writes.
BOOTSTRAP_MARKER = "/var/log/dda-build-server-bootstrap.done"
BOOTSTRAP_LOG = "/var/log/dda-build-server-bootstrap.log"

#: `scripts/portal-build-agent.sh` argument contract (Req 7.6).
AGENT_SCRIPT_RELPATH = "scripts/portal-build-agent.sh"
AGENT_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "portal-build-agent.sh")
AGENT_ARG_KEYS = ("BUILD_JOB_ID", "BUILD_TARGET", "EVENT_BUS", "SOURCE_REF")
AGENT_REQUIRED_ARG_KEYS = ("BUILD_JOB_ID", "BUILD_TARGET")
AGENT_EXIT_USAGE = 64
AGENT_EXIT_LOCK_HELD = 75
AGENT_LOCK_FILE = "/var/lock/dda-build.lock"

#: Phase-event detail field sets the agent emits (Req 7.6).
BUILDING_DETAIL_KEYS = {"build_job_id", "phase", "build_target", "source_ref"}
#: Task 13 reconciliation — the ONLY extra keys the `building` detail may
#: carry beyond the recorded oracle: the spec-sanctioned ADDITIVE
#: `source_commit` field task 12 introduced (Req 4.5). Req 7.6 allows the
#: emissions to differ from the oracle "only by additive keys"; every
#: recorded key, value and type assertion is unchanged, and no other
#: detail set (`succeeded`, `failed`) admits any extra key.
BUILDING_DETAIL_ADDITIVE_KEYS = {"source_commit"}
SUCCEEDED_DETAIL_KEYS = {"build_job_id", "phase", "build_target", "result"}
FAILED_DETAIL_KEYS = {"build_job_id", "phase", "build_target", "error_kind",
                      "error_message"}
FAILED_PUBLISHING_DETAIL_KEYS = FAILED_DETAIL_KEYS | {
    "published_artifacts", "unpublished_artifacts"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _artifact_fingerprint():
    return {
        "build_jobs": getattr(build_jobs, "__file__", "?"),
        "build_domain": getattr(build_domain, "__file__", "?"),
        "build_dispatcher": getattr(build_dispatcher, "__file__", "?"),
        "build_fleet": getattr(build_fleet, "__file__", "?"),
        "build_repo_dir": build_dispatcher.BUILD_REPO_DIR,
        "build_source_present": _build_source() is not None,
    }


def _observation(**fields):
    fields["artifact"] = _artifact_fingerprint()
    return json.dumps(fields, indent=2, default=str)


def _build_source():
    """`build_source.py` once task 3.1 creates it, else None. Nothing in
    this file REQUIRES it: the Property 2 invariants are stated against
    current behavior, and the resolver is asserted opportunistically so
    the same assertions gain teeth after the fix lands."""
    try:
        import build_source  # noqa: F401 (created by task 3.1)
    except ImportError:
        return None
    return sys.modules["build_source"]


def _default_repo_dir():
    """The authoritative default clone directory: `BUILD_REPO_DIR` today,
    `build_source.DEFAULT_REPO_DIR` once it exists (they are the same
    value after task 3.2 sets the default from the resolver)."""
    module = _build_source()
    if module is not None and hasattr(module, "DEFAULT_REPO_DIR"):
        return module.DEFAULT_REPO_DIR
    return build_dispatcher.BUILD_REPO_DIR


def _resolve_repo_dir(job, server=None):
    """Repo_Dir for a (job, server) pair, through the shared resolver when
    it exists and through the module constant while it does not."""
    module = _build_source()
    if module is not None and hasattr(module, "resolve_repo_dir"):
        return module.resolve_repo_dir(
            job, server, env_default=build_dispatcher.BUILD_REPO_DIR)
    return build_dispatcher.BUILD_REPO_DIR


def _agent_command(job, repo_dir=None):
    """`build_dispatcher.agent_command`, tolerant of the task 3.2
    signature change from `agent_command(job)` to
    `agent_command(job, repo_dir)`."""
    fn = build_dispatcher.agent_command
    positional = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 2:
        return fn(job, repo_dir if repo_dir is not None
                  else _resolve_repo_dir(job))
    return fn(job)


_ARG_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*=")


def _agent_invocation(command_text):
    """The agent invocation inside a dispatcher command: the script path
    and the KEY=VALUE arguments that follow it.

    Extracted rather than compared as a whole string on purpose — task
    5.2 prepends a source-sync preamble to the same command, and the
    argument contract (Req 7.6) is what must not change."""
    tokens = shlex.split(command_text)
    for index, token in enumerate(tokens):
        if token.endswith(AGENT_SCRIPT_RELPATH):
            args = []
            for candidate in tokens[index + 1:]:
                if not _ARG_TOKEN.match(candidate):
                    break
                args.append(candidate)
            return {
                "script": token,
                "args": args,
                "arg_keys": [a.split("=", 1)[0] for a in args],
                "arg_map": dict(a.split("=", 1) for a in args),
                "interpreter": tokens[index - 1] if index else None,
            }
    return None


def _fleet_bootstrap_clone_dir():
    """The directory `build_fleet`'s dedicated bootstrap clones into —
    the location legacy servers actually used (Property 2, Req 5.3).

    Reads it out of the bootstrap text so the assertion survives task
    3.3 replacing the literal with a `{repo_dir}` placeholder fed from
    `build_source.DEFAULT_REPO_DIR`."""
    template = build_fleet.USER_DATA_TEMPLATE
    match = re.search(r"git clone\s+(\S+)\s+(\S+)", template)
    assert match, _observation(
        template=template, note="the fleet bootstrap no longer clones")
    directory = match.group(2)
    if directory.startswith("{") and directory.endswith("}"):
        # Formatted from the shared resolver (post task 3.3).
        return _default_repo_dir()
    return directory


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
    """Raw stored items (Decimals and all), keyed by build_job_id."""
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
    return _AUDIT.scan().get("Items", [])


def _clear_tables():
    for job_id in _scan_raw_jobs():
        _JOBS.delete_item(Key={"build_job_id": job_id})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    for item in _audit_records():
        _AUDIT.delete_item(Key={"event_id": item["event_id"],
                                "timestamp": item["timestamp"]})


def _seed_server(arch, repo_dir_field="absent", server_id=None):
    """A running dedicated server. `repo_dir_field` chooses the Property 2
    input: "absent" (every server bootstrapped before this change),
    None, or "" (recorded but empty)."""
    server_id = server_id or f"srv-{uuid.uuid4()}"
    item = {
        "server_id": server_id,
        "name": f"legacy-{server_id[-8:]}",
        "instance_id": "i-0123456789abcdef0",
        "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
        "cpu_architecture": arch,
    }
    if repo_dir_field != "absent":
        item["repo_dir"] = repo_dir_field
    _SERVERS.put_item(Item=item)
    return build_jobs.to_native(item)


def _seed_job(job_id, status, created_at, execution_mode=MODE_EPHEMERAL,
              server_id=None, target=build_domain.TARGET_JP5):
    item = {
        "build_job_id": job_id,
        "build_target": target,
        "execution_mode": execution_mode,
        "status": status,
        "requested_by": "seed-user",
        "created_at": created_at,
    }
    if server_id:
        item["server_id"] = server_id
    _JOBS.put_item(Item=item)
    return build_jobs.to_native(item)


class _NoDispatch:
    """`invoke_dispatcher` is already a no-op (its env var is unset); this
    guarantees it for every submit case."""

    def __enter__(self):
        self._patch = mock.patch.object(build_jobs, "invoke_dispatcher",
                                        side_effect=lambda ids: None)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def _assert_error_envelope(response, status, code):
    body = json.loads(response["body"])
    observed = _observation(status=response["statusCode"], body=body)
    assert response["statusCode"] == status, observed
    assert set(body) == {"error"}, observed
    assert set(body["error"]) == ERROR_ENVELOPE_KEYS, observed
    assert body["error"]["code"] == code, observed
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict), observed
    return body


def _assert_config_snapshot(snapshot, expected_values=None):
    """Every recorded `config_snapshot` key keeps its name, type and
    value; additive keys are allowed (Req 7.5).

    Without `expected_values` the recorded DEFAULTS are asserted. With
    `expected_values` (a generated effective configuration) the snapshot
    must carry those values in their own type — `source_ref` is the one
    key whose documented domain is None-or-ref-string."""
    observed = _observation(snapshot=snapshot,
                            oracle=list(CONFIG_SNAPSHOT_ORACLE),
                            expected_values=expected_values)
    assert set(snapshot) >= set(CONFIG_SNAPSHOT_ORACLE), observed
    for key, (default_value, default_type) in CONFIG_SNAPSHOT_ORACLE.items():
        overridden = expected_values is not None and key in expected_values
        expected = expected_values[key] if overridden else default_value
        value_type = type(expected) if overridden else default_type
        value = snapshot[key]
        assert isinstance(value, value_type) or (
            value_type is int and isinstance(value, Decimal)), observed
        assert value == expected, observed


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_ARM64_TARGETS = tuple(t for t in TARGETS
                       if build_domain.required_arch_for_target(t)
                       == build_domain.ARCH_ARM64)
_X86_TARGETS = tuple(t for t in TARGETS
                     if build_domain.required_arch_for_target(t)
                     == build_domain.ARCH_X86_64)

#: Non-empty ordered target selections (duplicates are legal today: one
#: Build_Job per selected target, in request order).
target_lists = st.lists(st.sampled_from(TARGETS), min_size=1, max_size=4)

#: Dedicated selections must be arch-homogeneous for the chosen server.
arch_homogeneous_targets = st.one_of(
    st.tuples(st.just(build_domain.ARCH_ARM64),
              st.lists(st.sampled_from(_ARM64_TARGETS),
                       min_size=1, max_size=3)),
    st.tuples(st.just(build_domain.ARCH_X86_64),
              st.lists(st.sampled_from(_X86_TARGETS),
                       min_size=1, max_size=3)),
)

#: Effective build configurations: the recorded key set with values in
#: the recorded types, plus an optional additive key (Increment B extends
#: the snapshot additively, so the snapshot machinery must be agnostic).
build_configs = st.fixed_dictionaries({
    "arm64_instance_type": st.sampled_from(["m6g.4xlarge", "m6g.8xlarge"]),
    "x86_64_instance_type": st.sampled_from(["m6i.4xlarge", "m6i.8xlarge"]),
    "volume_size_gb": st.integers(min_value=20, max_value=500),
    "region": st.sampled_from(["us-east-1", "eu-west-1"]),
    "max_runtime_hours": st.integers(min_value=1, max_value=12),
    "use_spot_for_ephemeral": st.booleans(),
    "source_ref": st.one_of(st.none(),
                            st.sampled_from(["main", "v1.2.3", "a" * 40])),
})

#: The Property 2 input domain: a Build_Server record with NO recorded
#: repository directory — absent (every server bootstrapped before this
#: change), explicitly None, or recorded empty.
legacy_repo_dir_fields = st.sampled_from(["absent", None, ""])


# ---------------------------------------------------------------------------
# Property 14 (a): the zero-effort submission path (Req 7.1, 7.3, 7.5).
# ---------------------------------------------------------------------------

class TestNoSelectionSubmissionBaseline:
    """`POST /builds` with neither repository nor branch selected: the
    request body, the 201 envelope, and the created Build_Job records,
    recorded on unfixed code."""

    @given(targets=target_lists)
    @settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_ephemeral_no_selection_submission(self, targets):
        _clear_tables()
        user = _jwt_user()
        body = {"targets": targets, "execution_mode": MODE_EPHEMERAL}
        assert set(body) == NO_SELECTION_BODY_KEYS_EPHEMERAL

        with _NoDispatch():
            response = build_jobs.handler(
                _event("/builds", "POST", user, body=body), None)

        jobs = self._assert_accepted(response, targets, MODE_EPHEMERAL,
                                    user, None)
        # Req 7.3: the stored item omits `server_id` (None) while
        # `predecessor_job_id: None` is retained as NULL.
        for order, job in enumerate(jobs):
            stored = _raw_job(job["build_job_id"])
            observed = _observation(order=order, stored=stored, job=job)
            assert job["server_id"] is None, observed
            assert "server_id" not in stored, observed
            assert "predecessor_job_id" in stored, observed
            if order == 0:
                assert stored["predecessor_job_id"] is None, observed
            else:
                assert stored["predecessor_job_id"] == \
                    jobs[order - 1]["build_job_id"], observed

    @given(selection=arch_homogeneous_targets)
    @settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_dedicated_no_selection_submission(self, selection):
        arch, targets = selection
        _clear_tables()
        user = _jwt_user()
        server = _seed_server(arch)
        body = {"targets": targets, "execution_mode": MODE_DEDICATED,
                "server_id": server["server_id"]}
        assert set(body) == NO_SELECTION_BODY_KEYS_DEDICATED

        with _NoDispatch():
            response = build_jobs.handler(
                _event("/builds", "POST", user, body=body), None)

        jobs = self._assert_accepted(response, targets, MODE_DEDICATED,
                                    user, server["server_id"])
        for job in jobs:
            stored = _raw_job(job["build_job_id"])
            assert stored["server_id"] == server["server_id"], _observation(
                stored=stored)

    def _assert_accepted(self, response, targets, execution_mode, user,
                         server_id):
        body = json.loads(response["body"])
        observed = _observation(status=response["statusCode"], body=body,
                               targets=targets, mode=execution_mode)

        assert response["statusCode"] == SUBMIT_STATUS, observed
        assert set(body) == SUBMIT_RESPONSE_KEYS, observed
        jobs = body["jobs"]
        assert len(jobs) == len(targets), observed
        assert len(_scan_raw_jobs()) == len(targets), observed
        assert [job["build_target"] for job in jobs] == list(targets), observed

        request_ids = {job["request_id"] for job in jobs}
        assert request_ids == {body["request_id"]}, observed

        for order, job in enumerate(jobs):
            assert set(job) == JOB_RECORD_KEYS, observed
            assert job["status"] == build_domain.STATUS_QUEUED, observed
            assert job["request_order"] == order, observed
            assert job["execution_mode"] == execution_mode, observed
            assert job["server_id"] == server_id, observed
            assert job["requested_by"] == user["user_id"], observed
            assert job["predecessor_job_id"] == (
                None if order == 0 else jobs[order - 1]["build_job_id"]), \
                observed
            definition = build_domain.target_definition(job["build_target"])
            assert job["component_name"] == definition["component_name"], observed
            assert job["required_arch"] == definition["required_arch"], observed
            assert job["log"] == {"group": build_jobs.BUILD_LOG_GROUP,
                                  "stream": job["build_job_id"]}, observed
            assert job["ttl"] == int(job["created_at"] / 1000) + \
                build_jobs.JOB_TTL_DAYS * 24 * 60 * 60, observed
            _assert_config_snapshot(job["config_snapshot"])

        # One build_requested audit record per created Build_Job (Req 7.1).
        requested = [r for r in _audit_records()
                     if r["action"] == "build_requested"]
        assert len(requested) == len(targets), observed
        assert {r["result"] for r in requested} == {"success"}, observed
        return jobs

    def test_rejected_submission_creates_no_job_and_keeps_the_envelope(self):
        """The rejection envelope for an invalid no-selection body, and
        the zero-persistence guarantee (Req 7.1, 7.4)."""
        _clear_tables()
        user = _jwt_user()
        with _NoDispatch():
            response = build_jobs.handler(
                _event("/builds", "POST", user,
                       body={"targets": [], "execution_mode": "ephemeral"}),
                None)
        body = _assert_error_envelope(response, 400, "BUILD_REQUEST_INVALID")
        assert body["error"]["details"]["errors"], "rejection names no rule"
        assert _scan_raw_jobs() == {}


# ---------------------------------------------------------------------------
# Property 14 (b): the config_snapshot key set, types and semantics
# (Req 7.5).
# ---------------------------------------------------------------------------

class TestConfigSnapshotBaseline:

    def test_effective_config_key_set_and_types(self):
        """`effective_build_config()` with no stored settings item: the
        recorded key set with the recorded default values and types."""
        config = build_jobs.effective_build_config()
        _assert_config_snapshot(config)
        # Recorded exactly today (Increment B may only ADD keys).
        assert set(config) == set(CONFIG_SNAPSHOT_ORACLE) or \
            set(config) > set(CONFIG_SNAPSHOT_ORACLE), _observation(
                config=config)

    @given(config=build_configs, targets=target_lists,
           requested_by=st.text(min_size=1, max_size=12),
           created_at=st.integers(min_value=1, max_value=2 ** 40))
    @settings(max_examples=150, deadline=None)
    def test_create_build_jobs_snapshots_the_config_verbatim(
            self, config, targets, requested_by, created_at):
        """Every job carries the effective configuration verbatim, as its
        own deep copy: mutating one snapshot affects neither another job's
        snapshot nor the caller's configuration (Req 7.5)."""
        job_ids = [str(uuid.uuid4()) for _ in targets]
        original = json.loads(json.dumps(config))
        jobs = build_domain.create_build_jobs(
            targets=targets, execution_mode=MODE_EPHEMERAL, server_id=None,
            request_id=str(uuid.uuid4()), job_ids=job_ids,
            requested_by=requested_by, created_at=created_at,
            config_snapshot=config)

        for job in jobs:
            observed = _observation(snapshot=job["config_snapshot"],
                                    config=config)
            assert job["config_snapshot"] == config, observed
            assert set(job["config_snapshot"]) >= set(CONFIG_SNAPSHOT_ORACLE), \
                observed
            _assert_config_snapshot(job["config_snapshot"],
                                    expected_values=config)
            assert job["config_snapshot"] is not config, observed

        jobs[0]["config_snapshot"]["volume_size_gb"] = 999999
        assert config == original, "mutating a snapshot leaked into the config"
        for job in jobs[1:]:
            assert job["config_snapshot"]["volume_size_gb"] == \
                config["volume_size_gb"]

    @given(config=build_configs, targets=target_lists)
    @settings(max_examples=100, deadline=None)
    def test_snapshot_survives_persistence_unchanged(self, config, targets):
        """The snapshot round-trips through DynamoDB with its recorded key
        set intact (Req 7.5)."""
        job_ids = [str(uuid.uuid4()) for _ in targets]
        jobs = build_domain.create_build_jobs(
            targets=targets, execution_mode=MODE_EPHEMERAL, server_id=None,
            request_id=str(uuid.uuid4()), job_ids=job_ids,
            requested_by="snapshot-user", created_at=build_jobs.now_ms(),
            config_snapshot=config)
        for job in jobs:
            build_jobs.put_new_job(job)
        for job_id in job_ids:
            stored = build_jobs.to_native(_raw_job(job_id))
            assert stored["config_snapshot"] == config, _observation(
                stored=stored["config_snapshot"], config=config)


# ---------------------------------------------------------------------------
# Property 14 (c): the stored item still omits None-valued indexed key
# attributes — the `server-index` fix that made ephemeral submission
# possible (Req 7.3).
# ---------------------------------------------------------------------------

class TestNullIndexedKeyOmissionBaseline:
    """Against the DEPLOYED BuildJobs schema (GSIs created above), so a
    regression here is a real `ValidationException: Type mismatch for
    Index Key server_id`, not a soft assertion."""

    def test_indexed_key_attribute_set_is_unchanged(self):
        assert set(build_jobs.INDEXED_KEY_ATTRIBUTES) == \
            INDEXED_KEY_ATTRIBUTES_ORACLE, _observation(
                observed=list(build_jobs.INDEXED_KEY_ATTRIBUTES),
                oracle=sorted(INDEXED_KEY_ATTRIBUTES_ORACLE))

    @given(targets=target_lists,
           extra_none_fields=st.lists(
               st.sampled_from(["error", "ended_at", "started_at",
                                "dispatched_at", "result"]),
               max_size=3, unique=True),
           config=build_configs)
    @settings(max_examples=150, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_ephemeral_jobs_store_without_none_indexed_keys(
            self, targets, extra_none_fields, config):
        """For any generated ephemeral job (`server_id = None`): the
        stored item omits `server_id`, keeps `predecessor_job_id: None`,
        and keeps every other None-valued attribute as NULL."""
        job_ids = [str(uuid.uuid4()) for _ in targets]
        jobs = build_domain.create_build_jobs(
            targets=targets, execution_mode=MODE_EPHEMERAL, server_id=None,
            request_id=str(uuid.uuid4()), job_ids=job_ids,
            requested_by="null-key-user", created_at=build_jobs.now_ms(),
            config_snapshot=config)

        for job in jobs:
            for field in extra_none_fields:
                job[field] = None
            returned = build_jobs.put_new_job(job)
            stored = _raw_job(job["build_job_id"])
            expected_stored_keys = {
                key for key, value in returned.items()
                if value is not None
                or key not in INDEXED_KEY_ATTRIBUTES_ORACLE}
            observed = _observation(returned=returned, stored=stored,
                                    extra_none_fields=extra_none_fields)

            # The returned (API) record keeps server_id: None.
            assert returned["server_id"] is None, observed
            assert set(returned) == JOB_RECORD_KEYS | set(extra_none_fields), \
                observed
            # The stored item omits ONLY the None-valued indexed keys.
            assert set(stored) == expected_stored_keys, observed
            assert "server_id" not in stored, observed
            for field in extra_none_fields:
                assert field in stored and stored[field] is None, observed

    @given(order=st.integers(min_value=0, max_value=5),
           status=st.sampled_from(sorted(build_domain.ALL_STATUSES)))
    @settings(max_examples=100, deadline=None)
    def test_first_job_of_a_request_keeps_a_null_predecessor(
            self, order, status):
        """`predecessor_job_id` is not an indexed key, so None is stored
        as NULL — for any request order and any status (Req 7.3)."""
        job_id = str(uuid.uuid4())
        job = {
            "build_job_id": job_id,
            "request_id": str(uuid.uuid4()),
            "request_order": order,
            "predecessor_job_id": None,
            "build_target": build_domain.TARGET_JP5,
            "component_name": "aws.edgeml.dda.LocalServer.arm64JP5",
            "required_arch": build_domain.ARCH_ARM64,
            "execution_mode": MODE_EPHEMERAL,
            "server_id": None,
            "status": status,
            "requested_by": "null-predecessor-user",
            "created_at": build_jobs.now_ms(),
            "config_snapshot": dict(build_jobs.DEFAULT_BUILD_CONFIG),
        }
        build_jobs.put_new_job(job)
        stored = _raw_job(job_id)
        observed = _observation(stored=stored)
        assert "server_id" not in stored, observed
        assert stored["predecessor_job_id"] is None, observed
        assert stored["config_snapshot"]["source_ref"] is None, observed


# ---------------------------------------------------------------------------
# Property 14 (d): the build API surface — list/detail/logs/cancel shapes,
# pagination token, ordering, error envelopes (Req 7.4).
# ---------------------------------------------------------------------------

class TestBuildApiSurfaceBaseline:

    @given(offsets=st.lists(st.integers(min_value=0, max_value=10_000_000),
                            min_size=1, max_size=7, unique=True),
           limit=st.integers(min_value=1, max_value=4))
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_list_shape_ordering_and_pagination(self, offsets, limit):
        """`GET /builds`: `{jobs, nextToken, total}`, most recent first
        (created_at desc, build_job_id as the tie-break), and an opaque
        token that walks the history exactly once (Req 7.4)."""
        _clear_tables()
        user = _jwt_user()
        now = build_jobs.now_ms()
        seeded = []
        for index, offset in enumerate(sorted(offsets)):
            job_id = f"bj-{index}-{uuid.uuid4()}"
            seeded.append(_seed_job(job_id, build_domain.STATUS_QUEUED,
                                    now - offset))
        expected_order = [
            job["build_job_id"] for job in sorted(
                seeded,
                key=lambda j: (int(j["created_at"]), j["build_job_id"]),
                reverse=True)]

        response = build_jobs.handler(_event("/builds", "GET", user), None)
        body = json.loads(response["body"])
        observed = _observation(status=response["statusCode"], body=body,
                                expected_order=expected_order)
        assert response["statusCode"] == 200, observed
        assert set(body) == LIST_RESPONSE_KEYS, observed
        assert body["total"] == len(seeded), observed
        assert body["nextToken"] is None, observed
        assert [job["build_job_id"] for job in body["jobs"]] == \
            expected_order, observed

        # Paginated walk: same order, no duplicates, no gaps, and the
        # token is absent exactly on the last page.
        collected, token, pages = [], None, 0
        while True:
            query = {"limit": str(limit)}
            if token:
                query["nextToken"] = token
            page_response = build_jobs.handler(
                _event("/builds", "GET", user, query=query), None)
            page = json.loads(page_response["body"])
            assert page_response["statusCode"] == 200, _observation(page=page)
            assert set(page) == LIST_RESPONSE_KEYS, _observation(page=page)
            assert page["total"] == len(seeded), _observation(page=page)
            assert len(page["jobs"]) <= limit, _observation(page=page)
            collected.extend(job["build_job_id"] for job in page["jobs"])
            token = page["nextToken"]
            pages += 1
            if token is None:
                break
            assert pages <= len(seeded) + 1, "pagination did not terminate"
        assert collected == expected_order, _observation(
            collected=collected, expected=expected_order, limit=limit)

    def test_detail_logs_and_error_envelopes(self):
        _clear_tables()
        user = _jwt_user()
        now = build_jobs.now_ms()
        job = _seed_job(f"bj-{uuid.uuid4()}", build_domain.STATUS_BUILDING,
                        now)

        detail = build_jobs.handler(
            _event("/builds/{id}", "GET", user,
                   job_id=job["build_job_id"]), None)
        detail_body = json.loads(detail["body"])
        observed = _observation(status=detail["statusCode"], body=detail_body)
        assert detail["statusCode"] == 200, observed
        assert set(detail_body) == DETAIL_RESPONSE_KEYS, observed
        assert detail_body["job"]["build_job_id"] == job["build_job_id"], observed

        logs = build_jobs.handler(
            _event("/builds/{id}/logs", "GET", user,
                   job_id=job["build_job_id"]), None)
        logs_body = json.loads(logs["body"])
        observed = _observation(status=logs["statusCode"], body=logs_body)
        assert logs["statusCode"] == 200, observed
        assert set(logs_body) == LOGS_RESPONSE_KEYS, observed
        assert logs_body == {"events": [], "nextToken": None}, observed

        _assert_error_envelope(
            build_jobs.handler(_event("/builds/{id}", "GET", user,
                                      job_id="bj-missing"), None),
            404, "BUILD_JOB_NOT_FOUND")
        _assert_error_envelope(
            build_jobs.handler(_event("/builds/{id}/logs", "GET", user,
                                      job_id="bj-missing"), None),
            404, "BUILD_JOB_NOT_FOUND")
        _assert_error_envelope(
            build_jobs.handler(_event("/builds", "GET", user,
                                      query={"nextToken": "not-a-token"}),
                               None),
            400, "INVALID_PARAMETER")

    @given(target=st.sampled_from(TARGETS),
           execution_mode=st.sampled_from([MODE_EPHEMERAL, MODE_DEDICATED]))
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_queued_cancel_semantics(self, target, execution_mode):
        """Cancelling a queued Build_Job: 200 `{job}` with the job
        cancelled and `ended_at` set, plus one `build_cancelled` audit
        record (Req 7.4). No SSM is involved on this path."""
        _clear_tables()
        user = _jwt_user()
        job_id = f"bj-{uuid.uuid4()}"
        _seed_job(job_id, build_domain.STATUS_QUEUED, build_jobs.now_ms(),
                  execution_mode=execution_mode, target=target,
                  server_id=("srv-x" if execution_mode == MODE_DEDICATED
                             else None))

        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", user, job_id=job_id), None)
        body = json.loads(response["body"])
        observed = _observation(status=response["statusCode"], body=body)
        assert response["statusCode"] == 200, observed
        assert set(body) == CANCEL_RESPONSE_KEYS, observed
        assert body["job"]["status"] == build_domain.STATUS_CANCELLED, observed
        assert body["job"]["ended_at"], observed

        records = [r for r in _audit_records()
                   if r["action"] == "build_cancelled"]
        assert len(records) == 1, observed
        assert records[0]["result"] == "success", observed
        assert records[0]["details"]["status_at_request"] == \
            build_domain.STATUS_QUEUED, observed
        assert records[0]["details"]["removed_from_queue"] is True, observed

    @given(status=st.sampled_from([build_domain.STATUS_SUCCEEDED,
                                   build_domain.STATUS_FAILED,
                                   build_domain.STATUS_CANCELLED]))
    @settings(max_examples=60, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_terminal_cancel_rejected_unchanged(self, status):
        _clear_tables()
        user = _jwt_user()
        job_id = f"bj-{uuid.uuid4()}"
        _seed_job(job_id, status, build_jobs.now_ms())
        before = _raw_job(job_id)

        response = build_jobs.handler(
            _event("/builds/{id}/cancel", "POST", user, job_id=job_id), None)
        body = _assert_error_envelope(response, 409, "CANCELLATION_REJECTED")
        assert body["error"]["details"]["status"] == status
        assert body["error"]["details"]["errors"], "rejection names no rule"
        assert _raw_job(job_id) == before, "a rejected cancel mutated the job"

    def test_cancel_missing_job_envelope(self):
        _clear_tables()
        user = _jwt_user()
        _assert_error_envelope(
            build_jobs.handler(
                _event("/builds/{id}/cancel", "POST", user,
                       job_id="bj-missing"), None),
            404, "BUILD_JOB_NOT_FOUND")


# ---------------------------------------------------------------------------
# Property 2: legacy dedicated servers need no re-bootstrap (Req 5.3).
#
# Input domain: Build_Server records carrying NO recorded repository
# directory — the field absent (every server bootstrapped before this
# change), explicitly None, or recorded empty.
# ---------------------------------------------------------------------------

class TestLegacyDedicatedServerBaseline:

    def test_legacy_bootstrap_cloned_into_home_ubuntu(self):
        """The location legacy dedicated servers actually used. Recorded
        from `build_fleet.USER_DATA_TEMPLATE` on unfixed code
        (`build_fleet.py:179`); after task 3.3 the same directory must
        come out of the shared resolver, which is why resolution for a
        server with no recorded directory has to yield exactly this."""
        clone_dir = _fleet_bootstrap_clone_dir()
        assert clone_dir == LEGACY_BOOTSTRAP_REPO_DIR, _observation(
            clone_dir=clone_dir, oracle=LEGACY_BOOTSTRAP_REPO_DIR,
            note="legacy dedicated servers would need a re-bootstrap")
        # The dedicated bootstrap already signals completion and logs
        # where the design's ephemeral gate will look (Req 6.2, 6.4).
        assert BOOTSTRAP_MARKER in build_fleet.USER_DATA_TEMPLATE
        assert BOOTSTRAP_LOG in build_fleet.USER_DATA_TEMPLATE

        module = _build_source()
        if module is not None and hasattr(module, "DEFAULT_REPO_DIR"):
            # Once the resolver exists, its default IS this directory.
            assert module.DEFAULT_REPO_DIR == LEGACY_BOOTSTRAP_REPO_DIR

    @given(repo_dir_field=legacy_repo_dir_fields,
           target=st.sampled_from(TARGETS),
           source_ref=st.one_of(st.none(),
                                st.sampled_from(["main", "v1.2.3", "b" * 40])))
    @settings(max_examples=150, deadline=None)
    def test_agent_invocation_for_a_legacy_server_is_the_recorded_contract(
            self, repo_dir_field, target, source_ref):
        """For a server record with no recorded repository directory,
        resolution yields the module's configured directory and the agent
        invocation is exactly the recorded contract (Req 5.3, 7.6)."""
        server = {
            "server_id": f"srv-{uuid.uuid4()}",
            "instance_id": "i-0123456789abcdef0",
            "lifecycle_state": build_domain.SERVER_STATE_RUNNING,
            "cpu_architecture": build_domain.required_arch_for_target(target),
        }
        if repo_dir_field != "absent":
            server["repo_dir"] = repo_dir_field
        job = {
            "build_job_id": str(uuid.uuid4()),
            "build_target": target,
            "execution_mode": MODE_DEDICATED,
            "server_id": server["server_id"],
            "status": build_domain.STATUS_QUEUED,
            "config_snapshot": dict(build_jobs.DEFAULT_BUILD_CONFIG,
                                    source_ref=source_ref),
        }

        resolved = _resolve_repo_dir(job, server)
        command = _agent_command(job, resolved)
        invocation = _agent_invocation(command)
        observed = _observation(repo_dir_field=repo_dir_field,
                                resolved=resolved, command=command,
                                invocation=invocation)

        # No recorded directory => the configured/authoritative default.
        assert resolved == build_dispatcher.BUILD_REPO_DIR, observed
        assert invocation is not None, observed
        assert invocation["interpreter"] == "bash", observed
        assert invocation["script"] == \
            f"{resolved}/{AGENT_SCRIPT_RELPATH}", observed

        expected_keys = ["BUILD_JOB_ID", "BUILD_TARGET", "EVENT_BUS"]
        if source_ref:
            expected_keys.append("SOURCE_REF")
        assert invocation["arg_keys"] == expected_keys, observed
        assert invocation["arg_map"]["BUILD_JOB_ID"] == job["build_job_id"], observed
        assert invocation["arg_map"]["BUILD_TARGET"] == target, observed
        assert invocation["arg_map"]["EVENT_BUS"] == \
            build_dispatcher.BUILD_EVENT_BUS, observed
        if source_ref:
            assert invocation["arg_map"]["SOURCE_REF"] == source_ref, observed

        module = _build_source()
        if module is not None:
            # Once the resolver lands (task 3.1/3.2), "no recorded
            # directory" must resolve to the location legacy servers
            # actually used — that is Req 5.3 in one line.
            assert resolved == LEGACY_BOOTSTRAP_REPO_DIR, observed
            if hasattr(module, "agent_script_path"):
                assert module.agent_script_path(resolved) == \
                    invocation["script"], observed

    @given(target=st.sampled_from(TARGETS))
    @settings(max_examples=60, deadline=None)
    def test_agent_invocation_is_identical_across_legacy_field_variants(
            self, target):
        """Absent / None / empty recorded directory are the same input as
        far as dispatch is concerned: the agent invocation is byte-equal
        for all three (Req 5.3)."""
        job_id = str(uuid.uuid4())
        job = {
            "build_job_id": job_id,
            "build_target": target,
            "execution_mode": MODE_DEDICATED,
            "server_id": "srv-legacy",
            "status": build_domain.STATUS_QUEUED,
            "config_snapshot": dict(build_jobs.DEFAULT_BUILD_CONFIG),
        }
        invocations = []
        for field in ("absent", None, ""):
            server = {"server_id": "srv-legacy",
                      "instance_id": "i-0123456789abcdef0",
                      "lifecycle_state": build_domain.SERVER_STATE_RUNNING}
            if field != "absent":
                server["repo_dir"] = field
            invocations.append(
                _agent_invocation(_agent_command(job,
                                                 _resolve_repo_dir(job, server))))
        assert invocations[0] == invocations[1] == invocations[2], \
            _observation(invocations=invocations)

    @given(repo_dir_field=legacy_repo_dir_fields,
           target=st.sampled_from(TARGETS))
    @settings(max_examples=40, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_dedicated_dispatch_effects_match_the_oracle(
            self, repo_dir_field, target):
        """The whole dedicated dispatch pass for a legacy server: clean
        pre-dispatch verification -> queued->building transition, exactly
        one agent SendCommand with the recorded SSM parameters, and the
        command/log bookkeeping on the job (Req 5.3, 7.4).

        SAFETY: `build_dispatcher.ssm` is a recording stub and
        `run_shell_sync` is canned, so no SSM call and no build occur."""
        _clear_tables()
        arch = build_domain.required_arch_for_target(target)
        server = _seed_server(arch, repo_dir_field=repo_dir_field)
        now = build_jobs.now_ms()
        job_id = str(uuid.uuid4())
        job = {
            "build_job_id": job_id,
            "request_id": str(uuid.uuid4()),
            "request_order": 0,
            "predecessor_job_id": None,
            "build_target": target,
            "component_name": build_domain.target_definition(
                target)["component_name"],
            "required_arch": arch,
            "execution_mode": MODE_DEDICATED,
            "server_id": server["server_id"],
            "status": build_domain.STATUS_QUEUED,
            "requested_by": "legacy-dispatch-user",
            "created_at": now,
            "config_snapshot": dict(build_jobs.DEFAULT_BUILD_CONFIG),
        }
        build_jobs.put_new_job(job)

        ssm_stub = mock.MagicMock()
        ssm_stub.send_command.return_value = {
            "Command": {"CommandId": "cmd-legacy-oracle"}}
        with mock.patch.object(build_dispatcher, "ssm", ssm_stub), \
                mock.patch.object(build_dispatcher, "run_shell_sync",
                                  return_value=""):
            build_dispatcher.verify_and_start_dedicated(job, server, now)

        assert ssm_stub.send_command.call_count == 1, _observation(
            calls=ssm_stub.send_command.call_args_list)
        kwargs = ssm_stub.send_command.call_args.kwargs
        commands = kwargs["Parameters"]["commands"]
        invocation = _agent_invocation(commands[0])
        stored = build_jobs.to_native(_raw_job(job_id))
        resolved = _resolve_repo_dir(job, server)
        observed = _observation(kwargs=kwargs, stored=stored,
                                resolved=resolved, invocation=invocation)

        assert kwargs["InstanceIds"] == [server["instance_id"]], observed
        assert kwargs["DocumentName"] == "AWS-RunShellScript", observed
        assert len(commands) == 1, observed
        assert kwargs["Parameters"]["executionTimeout"] == [
            str(build_dispatcher.agent_execution_timeout_seconds(job))], observed
        assert kwargs["CloudWatchOutputConfig"] == {
            "CloudWatchLogGroupName": build_dispatcher.BUILD_LOG_GROUP,
            "CloudWatchOutputEnabled": True}, observed

        assert invocation is not None, observed
        assert invocation["script"] == \
            f"{resolved}/{AGENT_SCRIPT_RELPATH}", observed
        assert invocation["arg_keys"] == ["BUILD_JOB_ID", "BUILD_TARGET",
                                         "EVENT_BUS"], observed
        assert invocation["arg_map"]["BUILD_JOB_ID"] == job_id, observed

        assert stored["status"] == build_domain.STATUS_BUILDING, observed
        assert stored["dispatched_at"] == now, observed
        assert stored["started_at"] == now, observed
        assert stored["ssm"]["command_id"] == "cmd-legacy-oracle", observed
        assert stored["log"] == {
            "group": build_dispatcher.BUILD_LOG_GROUP,
            "stream": build_dispatcher.ssm_log_stream(
                "cmd-legacy-oracle", server["instance_id"])}, observed


# ---------------------------------------------------------------------------
# Property 14 (e): the portal-build-agent.sh argument contract and phase
# event detail field sets (Req 7.6).
#
# SAFETY: see the module docstring. The REAL script is executed only on
# paths that exit before `bash ./portal-build.sh`; the detail field sets
# come from a byte-identical copy running in a sandbox repository whose
# `portal-build.sh` is a stub.
# ---------------------------------------------------------------------------

def _agent_env(home):
    """A minimal environment with none of the agent's parameters set, so
    only the command-line arguments under test drive the run. EVENT_BUS
    is deliberately absent: `emit_event` then PRINTS the detail instead of
    calling `aws events put-events` (no AWS, by construction)."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.path.join(home, "gitconfig-absent"),
    }


def _run_agent(script, args, home):
    return subprocess.run(
        ["bash", script] + list(args), cwd=home, env=_agent_env(home),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def _emitted_details(stdout):
    """The phase-event details the agent emitted, parsed from the
    `EVENT_BUS not set — skipping event: <detail>` lines."""
    details = []
    for line in stdout.splitlines():
        marker = "skipping event: "
        if marker in line:
            details.append(json.loads(line.split(marker, 1)[1]))
    return details


@contextlib.contextmanager
def _hold_build_lock():
    """Hold the agent's build lock so the script's `flock -n` fails.

    The held-lock case is driven with an UNSUPPORTED BUILD_TARGET, so even
    if the lock were unexpectedly free the script would exit 64 at target
    validation and could never reach a build."""
    fd = os.open(AGENT_LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        pytest.skip(f"{AGENT_LOCK_FILE} is already held (a build may be "
                    f"running); not disturbing it")
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _require_free_build_lock():
    """Refuse to run a sandbox agent pass while the on-server build lock
    is held: the copy takes the same real lock, and a held lock means a
    build may be in progress."""
    fd = os.open(AGENT_LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pytest.skip(f"{AGENT_LOCK_FILE} is held (a build may be running)")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _sandbox_repo(build_exit=0, publishing=False, result_line=True):
    """A temporary repository whose `scripts/portal-build-agent.sh` is a
    BYTE-IDENTICAL copy of the real script and whose `portal-build.sh` is
    a stub. Returns (repo_dir, agent_script)."""
    root = tempfile.mkdtemp(prefix="dda-agent-sandbox-")
    repo = os.path.join(root, "DefectDetectionApplication")
    os.makedirs(os.path.join(repo, "scripts"))
    agent = os.path.join(repo, "scripts", "portal-build-agent.sh")
    shutil.copyfile(AGENT_SCRIPT, agent)
    os.chmod(agent, os.stat(agent).st_mode | stat.S_IXUSR)
    with open(AGENT_SCRIPT, "rb") as real, open(agent, "rb") as copy:
        assert hashlib.sha256(real.read()).hexdigest() == \
            hashlib.sha256(copy.read()).hexdigest(), \
            "the sandbox agent is not byte-identical to the real script"

    lines = ["#!/bin/bash", 'echo "STUB portal-build.sh $*"']
    if publishing:
        lines.append('echo "Publishing LocalServer component"')
        lines.append('echo "Pushing flask-app to ECR"')
    if result_line:
        lines.append(
            "echo 'PORTAL_BUILD_RESULT {\"component_name\":"
            "\"aws.edgeml.dda.LocalServer.arm64JP5\",\"published_version\":"
            "\"1.0.0\",\"pushed_image_refs\":[]}'")
    lines.append(f"exit {build_exit}")
    stub = os.path.join(repo, "portal-build.sh")
    with open(stub, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(stub, 0o755)
    return repo, agent


class TestAgentArgumentContractBaseline:
    """The REAL `scripts/portal-build-agent.sh`, driven only through its
    argument-parsing and lock paths."""

    def test_the_real_script_exists_and_is_the_documented_entry_point(self):
        assert os.path.isfile(AGENT_SCRIPT), AGENT_SCRIPT
        with open(AGENT_SCRIPT) as handle:
            text = handle.read()
        for key in AGENT_ARG_KEYS:
            assert f"{key}=*)" in text, f"{key} is no longer parsed"
        assert 'LOCK_FILE="/var/lock/dda-build.lock"' in text
        assert "exit 64" in text and "exit 75" in text

    @given(unknown=st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),
            whitelist_characters="-_./"),
        min_size=1, max_size=16))
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_unknown_argument_exits_64(self, unknown):
        """Any argument that is not one of the four documented KEY=VALUE
        parameters is a usage error (exit 64) — even alongside valid
        required parameters, and before the build lock is touched."""
        if any(unknown.startswith(f"{key}=") for key in AGENT_ARG_KEYS):
            return  # a documented parameter, not an unknown argument
        home = tempfile.mkdtemp(prefix="dda-agent-usage-")
        try:
            result = _run_agent(
                AGENT_SCRIPT,
                [f"BUILD_JOB_ID=bj-{uuid.uuid4()}", "BUILD_TARGET=JP5",
                 unknown],
                home)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        stderr = result.stderr.decode("utf-8", "replace")
        observed = _observation(argument=unknown, exit_code=result.returncode,
                                stderr=stderr)
        assert result.returncode == AGENT_EXIT_USAGE, observed
        assert "unknown argument" in stderr, observed
        assert "Usage:" in stderr, observed

    @pytest.mark.parametrize("args", [
        [],
        ["BUILD_JOB_ID=bj-only"],
        ["BUILD_TARGET=JP5"],
        ["EVENT_BUS=some-bus"],
        ["BUILD_JOB_ID=", "BUILD_TARGET=JP5"],
        ["BUILD_JOB_ID=bj-only", "BUILD_TARGET="],
    ])
    def test_missing_required_arguments_exit_64(self, args):
        home = tempfile.mkdtemp(prefix="dda-agent-required-")
        try:
            result = _run_agent(AGENT_SCRIPT, args, home)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        stderr = result.stderr.decode("utf-8", "replace")
        observed = _observation(args=args, exit_code=result.returncode,
                                stderr=stderr)
        assert result.returncode == AGENT_EXIT_USAGE, observed
        assert "BUILD_JOB_ID and BUILD_TARGET are required" in stderr, observed

    def test_held_lock_exits_75(self):
        """A held `/var/lock/dda-build.lock` defers the run with exit 75
        (EX_TEMPFAIL) before anything else happens (Req 7.6)."""
        home = tempfile.mkdtemp(prefix="dda-agent-lock-")
        try:
            with _hold_build_lock():
                result = _run_agent(
                    AGENT_SCRIPT,
                    [f"BUILD_JOB_ID=bj-{uuid.uuid4()}",
                     # Deliberately unsupported: an unexpectedly free lock
                     # exits 64 here instead of ever reaching a build.
                     "BUILD_TARGET=__NOT_A_TARGET__"],
                    home)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        stdout = result.stdout.decode("utf-8", "replace")
        observed = _observation(exit_code=result.returncode, stdout=stdout,
                               stderr=result.stderr.decode("utf-8", "replace"))
        assert result.returncode == AGENT_EXIT_LOCK_HELD, observed
        assert "is held by another build" in stdout, observed
        assert AGENT_LOCK_FILE in stdout, observed


class TestAgentPhaseEventDetailBaseline:
    """The `building` / `succeeded` / `failed` detail field sets, recorded
    by running a byte-identical copy of the agent in a sandbox repository
    (`portal-build.sh` stubbed, `EVENT_BUS` unset).

    Task 13 reconciliation: task 12 (Req 4.5) added the spec-sanctioned
    ADDITIVE `source_commit` field to the `building` detail. The Req 7.6
    preservation contract allows emissions to differ from the oracle
    "only by additive keys", so the `building` exact-key-set assertion is
    relaxed to: the oracle's recorded keys are a subset, and any extra
    key must be in `BUILDING_DETAIL_ADDITIVE_KEYS` ({'source_commit'}).
    NOTHING else changed — no oracle value was re-recorded, every value
    assertion is intact, and the `succeeded` / `failed` detail sets are
    still pinned exactly."""

    def _run(self, target="JP5", build_exit=0, publishing=False,
             result_line=True, args=None):
        _require_free_build_lock()
        repo, agent = _sandbox_repo(build_exit=build_exit,
                                    publishing=publishing,
                                    result_line=result_line)
        job_id = f"bj-{uuid.uuid4()}"
        try:
            result = _run_agent(
                agent, args if args is not None
                else [f"BUILD_JOB_ID={job_id}", f"BUILD_TARGET={target}"],
                repo)
        finally:
            shutil.rmtree(os.path.dirname(repo), ignore_errors=True)
            for leftover in (f"/tmp/portal-build-agent-{job_id}.log",):
                if os.path.exists(leftover):
                    os.remove(leftover)
        return job_id, result, _emitted_details(
            result.stdout.decode("utf-8", "replace"))

    @pytest.mark.parametrize("target", list(TARGETS))
    def test_building_and_succeeded_detail_field_sets(self, target):
        job_id, result, details = self._run(target=target, build_exit=0)
        observed = _observation(target=target, exit_code=result.returncode,
                                details=details,
                                stdout=result.stdout.decode("utf-8", "replace"))
        assert result.returncode == 0, observed
        phases = [detail["phase"] for detail in details]
        assert phases == ["building", "succeeded"], observed

        building, succeeded = details
        # Task 13 reconciliation (Req 4.5 / 7.6): task 12 added the
        # spec-sanctioned ADDITIVE `source_commit` field to the building
        # detail. The oracle's recorded keys must all still be present
        # with their recorded values; extra keys are allowed ONLY for the
        # sanctioned additive set. No oracle value was re-recorded.
        assert set(building) >= BUILDING_DETAIL_KEYS, observed
        assert set(building) - BUILDING_DETAIL_KEYS <= \
            BUILDING_DETAIL_ADDITIVE_KEYS, observed
        assert building["build_job_id"] == job_id, observed
        assert building["build_target"] == target, observed
        assert building["source_ref"] == "", observed

        assert set(succeeded) == SUCCEEDED_DETAIL_KEYS, observed
        assert succeeded["build_job_id"] == job_id, observed
        assert succeeded["build_target"] == target, observed
        assert succeeded["result"]["published_version"] == "1.0.0", observed

    def test_missing_result_line_still_succeeds_with_empty_result(self):
        _job_id, result, details = self._run(build_exit=0, result_line=False)
        observed = _observation(details=details, exit_code=result.returncode)
        assert result.returncode == 0, observed
        assert set(details[-1]) == SUCCEEDED_DETAIL_KEYS, observed
        assert details[-1]["result"] == {}, observed

    def test_build_stage_failure_detail_field_set(self):
        job_id, result, details = self._run(build_exit=3)
        observed = _observation(details=details, exit_code=result.returncode)
        assert result.returncode == 3, observed
        assert [d["phase"] for d in details] == ["building", "failed"], observed
        failed = details[-1]
        assert set(failed) == FAILED_DETAIL_KEYS, observed
        assert failed["build_job_id"] == job_id, observed
        assert failed["error_kind"] == "building", observed
        assert "exit 3" in failed["error_message"], observed

    def test_publish_stage_failure_detail_field_set(self):
        _job_id, result, details = self._run(build_exit=4, publishing=True)
        observed = _observation(details=details, exit_code=result.returncode)
        assert result.returncode == 4, observed
        failed = details[-1]
        assert set(failed) == FAILED_PUBLISHING_DETAIL_KEYS, observed
        assert failed["error_kind"] == "publishing", observed
        assert isinstance(failed["published_artifacts"], list), observed
        assert isinstance(failed["unpublished_artifacts"], list), observed

    def test_unsupported_target_exits_64_and_emits_a_building_failure(self):
        """The unsupported-target usage error: exit 64 with a `failed`
        event whose detail set is the recorded build-stage one (Req 7.6)."""
        job_id, result, details = self._run(
            args=[f"BUILD_JOB_ID=bj-unsupported-{uuid.uuid4()}",
                  "BUILD_TARGET=NOT_A_TARGET"])
        del job_id
        stderr = result.stderr.decode("utf-8", "replace")
        observed = _observation(details=details, exit_code=result.returncode,
                                stderr=stderr)
        assert result.returncode == AGENT_EXIT_USAGE, observed
        assert "unsupported BUILD_TARGET" in stderr, observed
        assert len(details) == 1, observed
        assert set(details[0]) == FAILED_DETAIL_KEYS, observed
        assert details[0]["error_kind"] == "building", observed
        assert details[0]["build_target"] == "NOT_A_TARGET", observed
