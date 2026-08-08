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
Bootstrap completion gate — Property 6 and Property 7
(build-source-selection, task 4.3).

**Property 6: Bug Condition** — Bootstrap completion gates the agent
command (Req 6.1, 6.2, 6.3, 6.4).
**Property 7: Bug Condition** — Provisioning/bootstrap failures release
the runner (Req 6.5).

Where the earlier work sits, and what is deliberately NOT repeated here:

* task 4.1 added the pure decision
  (`build_planner.decide_runner_readiness`, `READINESS_*`,
  `BOOTSTRAP_MARKER_PATH` / `BOOTSTRAP_LOG_PATH`); its 17 unit tests live
  in `test_runner_readiness_unit.py` and are NOT duplicated here.
* task 4.2 wired the gate into the dispatcher
  (`BOOTSTRAP_PROBE_COMMANDS`, `probe_bootstrap_marker`, the gate inside
  `provision_ephemeral`, `fail_bootstrap_timeout`,
  `dedicated_bootstrap_gate`).

This file drives the WIRING: generated probe-output/time sequences are
pushed through `build_dispatcher.provision_ephemeral` tick by tick, over
mocked SSM and EC2, and the observable effects (SSM SendCommand, the
stored Build_Job record, EC2 terminations, Audit_Log entries) are checked
against a restated model of the expected behavior:

* no agent SendCommand is issued in ANY tick where the Bootstrap_Marker
  has not been observed — the live race (command requested 21:36:59Z,
  cloud-init finished 21:38:54Z, `Up 140.42 seconds`) is one generated
  point in that domain;
* the budget boundary observed through the dispatcher: still WAIT at
  `now == deadline` (job stays provisioning, nothing sent), TIMEOUT at
  `now > deadline` (job failed, `bootstrap` recorded as the failing
  stage, runner released);
* a marker observed ALONGSIDE inner-step failure text
  (`Failed: sudo chmod 666 /var/run/docker.sock`, `Failed to set Python
  3.11 as default`) still yields READY, and the bootstrap log location is
  on the stored job record BEFORE the agent command is sent — asserted by
  reading the record back from DynamoDB at SendCommand time, so the
  ordering is observed, not inferred;
* every recorded provisioning/bootstrap failure is accompanied by a
  termination request for that job's compute in the SAME pass, through
  both `fail_provisioning` (RunInstances failure, agent SendCommand
  failure) and `fail_bootstrap_timeout`.

Plus the one unit check task 4.3 asks for: the generated ephemeral
user-data writes the Bootstrap_Marker as its LAST statement. Task 5.2
made that write non-fatal (`|| true`) and the log redirect conditional,
so the assertion is on the marker being last (and written exactly once,
after the source sync and the build-environment setup), not on an exact
string.

SAFETY (absolute): no test in this file launches EC2 compute, sends a
real SSM command, calls real AWS, or starts a real build.
* Every AWS interaction runs against moto (`mock_aws` started at module
  scope before any handler import), with the same BuildJobs + GSI fixture
  as the sibling `test_source_selection_preservation.py`, so an item with
  a wrongly typed indexed key is a real DynamoDB `ValidationException`.
* `build_dispatcher.ssm` and `build_dispatcher.ec2` are replaced by
  recording stubs for the whole of every dispatcher call: SendCommand,
  RunInstances, DescribeInstances and TerminateInstances are recorded and
  answered in-process. Nothing reaches moto's EC2/SSM either, so no
  instance is ever created, not even a simulated one.
* Explicit AMI ids are configured, so `resolve_ami` never reads an SSM
  parameter.
* `runner_bootstrap_user_data` is only ever rendered to text; the text is
  parsed and syntax-checked with `bash -n`, which does not execute it.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import uuid
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3
# resources/clients and their table names at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "bootstrap-gate"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
_SETTINGS_TABLE = f"dda-portal-settings-{_SUFFIX}"
_AUDIT_TABLE = f"dda-portal-audit-log-{_SUFFIX}"

os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ["SETTINGS_TABLE"] = _SETTINGS_TABLE
os.environ["AUDIT_LOG_TABLE"] = _AUDIT_TABLE
# A configured repository so the generated user-data is real text rather
# than the pre-baked-AMI no-op; nothing is ever fetched from it.
os.environ["BUILD_REPO_URL"] = \
    "https://github.com/awslabs/DefectDetectionApplication"
# Explicit AMI ids: resolve_ami then never reads an SSM parameter.
os.environ["BUILD_ARM64_AMI_ID"] = "ami-0aaaaaaaaaaaaaaa1"
os.environ["BUILD_X86_64_AMI_ID"] = "ami-0aaaaaaaaaaaaaaa2"
os.environ["BUILD_EVENT_BUS"] = "dda-portal-builds-test"
os.environ["BUILD_LOG_GROUP"] = "/dda/portal-builds-test"
# Short synchronous-SSM window: the stubs answer immediately, so this only
# bounds a hypothetical hang.
os.environ["SSM_COMMAND_TIMEOUT_SECONDS"] = "5"
# Deliberately NOT set, so BUILD_REPO_DIR keeps its module default (the
# deployed Lambda sets no override either — design A1).
os.environ.pop("BUILD_REPO_DIR", None)

# Import boto3 (and thus botocore/urllib3) from the test environment
# BEFORE the Lambda layer directory joins sys.path: the layer vendors its
# own urllib3 build targeting the Lambda runtime.
import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (same shim as
# the sibling preservation suite).
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
                "build_domain", "build_planner", "build_source",
                "shared_utils"):
    sys.modules.pop(_module, None)

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the DEPLOYED schema, GSIs included (same fixture as the
# sibling test_source_selection_preservation.py).
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
_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_dispatcher  # noqa: E402
import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_source  # noqa: E402


# ---------------------------------------------------------------------------
# THE MODEL — the expected gate behavior, restated independently of the
# implementation (paths and codes spelled out, not imported).
# ---------------------------------------------------------------------------

MARKER_PATH = "/var/log/dda-build-server-bootstrap.done"
LOG_PATH = "/var/log/dda-build-server-bootstrap.log"
AGENT_SCRIPT_RELPATH = "scripts/portal-build-agent.sh"
SETUP_SCRIPT_RELPATH = "setup-build-server.sh"
MS_PER_MINUTE = 60 * 1000

#: Error codes a failed Build_Job carries (design "Error Handling").
ERROR_BOOTSTRAP_TIMEOUT = "BOOTSTRAP_TIMEOUT"
ERROR_PROVISIONING_FAILED = "PROVISIONING_FAILED"

#: Audit actions the two failure paths record.
AUDIT_BOOTSTRAP_TIMEOUT = "build_bootstrap_timeout"
AUDIT_PROVISIONING_FAILED = "build_provisioning_failed"

STATUS_QUEUED = "queued"
STATUS_PROVISIONING = "provisioning"
STATUS_FAILED = "failed"

#: The inner-step failures the live runner's bootstrap log carried while
#: cloud-init still finished successfully (Req 6.4).
INNER_FAILURES = (
    "Failed: sudo chmod 666 /var/run/docker.sock\n"
    "Failed to set Python 3.11 as default\n"
)

#: A bootstrap log path other than the documented default, so "the log
#: path recorded on the job came from the probe" is observable.
CUSTOM_LOG_PATH = "/var/log/dda-bootstrap-custom.log"

#: The generated probe-output domain, keyed by kind. `None` is the
#: unverifiable probe (`run_shell_sync` could not positively complete it).
PROBE_OUTPUTS = {
    "done": f"BOOTSTRAP_DONE=1\nBOOTSTRAP_LOG={LOG_PATH}\n",
    "done_with_inner_failures":
        INNER_FAILURES + f"BOOTSTRAP_DONE=1\nBOOTSTRAP_LOG={LOG_PATH}\n",
    "done_custom_log": f"BOOTSTRAP_DONE=1\nBOOTSTRAP_LOG={CUSTOM_LOG_PATH}\n",
    "absent": f"BOOTSTRAP_DONE=0\nBOOTSTRAP_LOG={LOG_PATH}\n",
    "absent_with_inner_failures":
        INNER_FAILURES + f"BOOTSTRAP_DONE=0\nBOOTSTRAP_LOG={LOG_PATH}\n",
    "contradictory": "BOOTSTRAP_DONE=1\nBOOTSTRAP_DONE=0\n",
    "garbage": "cannot open /var/log/dda-build-server-bootstrap.done\n",
    "empty": "",
    "unverifiable": None,
}

#: Kinds that POSITIVELY report the Bootstrap_Marker. Everything else
#: leaves the marker unobserved, so the gate must stay shut.
MARKER_OBSERVED_KINDS = frozenset(
    {"done", "done_with_inner_failures", "done_custom_log"})

#: The bootstrap log location each observed kind must put on the job.
EXPECTED_LOG_PATH = {
    "done": LOG_PATH,
    "done_with_inner_failures": LOG_PATH,
    "done_custom_log": CUSTOM_LOG_PATH,
}

#: Tick times, expressed relative to the bootstrap budget deadline so the
#: boundary convention is generated rather than hand-picked.
TIME_KINDS = ("inside_early", "inside_late", "at_deadline", "past_deadline")
_TIME_RANK = {kind: rank for rank, kind in enumerate(TIME_KINDS)}

BASE_TIME = 1_762_000_000_000

TARGETS = ("JP5", "JP6", "AMD64", "AMD64_NVIDIA")


# ---------------------------------------------------------------------------
# Recording stubs: nothing leaves the process.
# ---------------------------------------------------------------------------

def _client_error(code, message, operation):
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, operation)


class _RecordingSSM:
    """`build_dispatcher.ssm` replacement.

    Answers DescribeInstanceInformation, SendCommand and
    GetCommandInvocation in-process, classifies every SendCommand as the
    marker PROBE or the AGENT invocation, and — for an agent send — reads
    the Build_Job record back from DynamoDB AT SEND TIME, so "the
    bootstrap log path was recorded on the job BEFORE the agent command"
    is observed rather than inferred.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.sends = []                  # every SendCommand, in order
        self.invocations = {}            # command_id -> canned invocation
        self.probe_output = None         # output the next probe returns
        self.probe_completes = True      # False -> unverifiable probe
        self.online = True               # DescribeInstanceInformation ping
        self.agent_send_error = None     # ClientError to raise on the agent
        self._counter = 0

    # -- introspection -----------------------------------------------------
    def sends_of(self, kind):
        return [s for s in self.sends if s["kind"] == kind]

    @property
    def agent_sends(self):
        return self.sends_of("agent")

    @property
    def probe_sends(self):
        return self.sends_of("probe")

    # -- the client surface the dispatcher uses ---------------------------
    def describe_instance_information(self, Filters=None, **kwargs):
        ids = []
        for f in Filters or []:
            if f.get("Key") == "InstanceIds":
                ids.extend(f.get("Values") or [])
        return {"InstanceInformationList": [
            {"InstanceId": i,
             "PingStatus": "Online" if self.online else "ConnectionLost"}
            for i in ids]}

    def send_command(self, **kwargs):
        commands = kwargs["Parameters"]["commands"]
        text = "\n".join(commands)
        kind = "other"
        if AGENT_SCRIPT_RELPATH in text:
            kind = "agent"
        elif "BOOTSTRAP_DONE" in text:
            kind = "probe"
        job_id = None
        match = re.search(r"BUILD_JOB_ID=([^\s'\"]+)", text)
        if match:
            job_id = match.group(1)
        record = {
            "kind": kind,
            "instance_id": kwargs["InstanceIds"][0],
            "commands": list(commands),
            "text": text,
            "build_job_id": job_id,
            "cloudwatch": "CloudWatchOutputConfig" in kwargs,
            # The stored record as it stands at SendCommand time.
            "job_at_send": _raw_job(job_id) if job_id else None,
        }
        if kind == "agent" and self.agent_send_error is not None:
            record["raised"] = True
            self.sends.append(record)
            raise self.agent_send_error
        self._counter += 1
        command_id = f"cmd-{self._counter:04d}"
        record["command_id"] = command_id
        self.sends.append(record)
        if kind == "probe":
            self.invocations[command_id] = {
                "Status": "Success" if self.probe_completes else "Failed",
                "StandardOutputContent": self.probe_output or "",
            }
        else:
            self.invocations[command_id] = {
                "Status": "Success", "StandardOutputContent": ""}
        return {"Command": {"CommandId": command_id}}

    def get_command_invocation(self, CommandId, InstanceId, **kwargs):
        return dict(self.invocations[CommandId], CommandId=CommandId,
                    InstanceId=InstanceId)

    def get_parameter(self, Name, **kwargs):  # pragma: no cover - guard
        raise AssertionError(
            f"resolve_ami must use the configured AMI ids, not SSM ({Name})")


class _RecordingEC2:
    """`build_dispatcher.ec2` replacement.

    RunInstances creates only a bookkeeping entry (no instance anywhere),
    DescribeInstances answers the `tag:dda-build:job-id` lookup
    `terminate_partial_compute` performs, and TerminateInstances is
    recorded. `run_error` makes RunInstances fail; `partial_compute`
    additionally registers compute that the failed launch left behind.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []                  # ordered call log
        self.instances = {}              # instance_id -> {job_id, state}
        self.run_error = None
        self.partial_compute = 0         # instances a failed launch leaves
        self._counter = 0

    # -- introspection -----------------------------------------------------
    def calls_of(self, name):
        return [c for c in self.calls if c["op"] == name]

    def instances_for(self, job_id):
        return sorted(i for i, v in self.instances.items()
                      if v["job_id"] == job_id)

    def live_instances_for(self, job_id):
        return sorted(i for i, v in self.instances.items()
                      if v["job_id"] == job_id and v["state"] != "terminated")

    def _new_instance(self, job_id):
        self._counter += 1
        instance_id = f"i-0{self._counter:015d}"
        self.instances[instance_id] = {"job_id": job_id, "state": "running"}
        return instance_id

    # -- the client surface the dispatcher uses ---------------------------
    def run_instances(self, **kwargs):
        job_id = None
        for spec in kwargs.get("TagSpecifications") or []:
            for tag in spec.get("Tags") or []:
                if tag["Key"] == build_dispatcher.TAG_JOB_ID:
                    job_id = tag["Value"]
        self.calls.append({"op": "run_instances", "job_id": job_id,
                           "user_data": kwargs.get("UserData"),
                           "image_id": kwargs.get("ImageId")})
        if self.run_error is not None:
            for _ in range(self.partial_compute):
                self._new_instance(job_id)
            raise self.run_error
        instance_id = self._new_instance(job_id)
        return {"Instances": [{"InstanceId": instance_id}]}

    def describe_instances(self, Filters=None, **kwargs):
        job_id, states = None, None
        for f in Filters or []:
            if f.get("Name") == f"tag:{build_dispatcher.TAG_JOB_ID}":
                job_id = (f.get("Values") or [None])[0]
            if f.get("Name") == "instance-state-name":
                states = set(f.get("Values") or [])
        self.calls.append({"op": "describe_instances", "job_id": job_id})
        matched = [
            {"InstanceId": i}
            for i, v in sorted(self.instances.items())
            if v["job_id"] == job_id
            and (states is None or v["state"] in states)
        ]
        return {"Reservations": [{"Instances": matched}] if matched else []}

    def terminate_instances(self, InstanceIds, **kwargs):
        self.calls.append({"op": "terminate_instances",
                           "instance_ids": list(InstanceIds)})
        for instance_id in InstanceIds:
            if instance_id in self.instances:
                self.instances[instance_id]["state"] = "terminated"
        return {"TerminatingInstances": [
            {"InstanceId": i} for i in InstanceIds]}


_SSM = _RecordingSSM()
_EC2 = _RecordingEC2()


class _MockedAws:
    """Replace the dispatcher's SSM and EC2 clients for one scenario."""

    def __enter__(self):
        _SSM.reset()
        _EC2.reset()
        self._patches = [
            mock.patch.object(build_dispatcher, "ssm", _SSM),
            mock.patch.object(build_dispatcher, "ec2", _EC2),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_job(job_id):
    return _JOBS.get_item(Key={"build_job_id": job_id}).get("Item")


def _job(job_id):
    """The stored Build_Job in native types."""
    item = _raw_job(job_id)
    return None if item is None else build_dispatcher.to_native(item)


def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _AUDIT.scan().get("Items", []):
        _AUDIT.delete_item(Key={"event_id": item["event_id"],
                                "timestamp": item["timestamp"]})


def _audits_for(job_id, action=None):
    return [
        build_dispatcher.to_native(item)
        for item in _AUDIT.scan().get("Items", [])
        if item.get("resource_id") == job_id
        and (action is None or item.get("action") == action)
    ]


def _observation(**fields):
    fields.setdefault("agent_sends", [
        {k: v for k, v in s.items() if k != "job_at_send"}
        for s in _SSM.agent_sends])
    fields.setdefault("probe_sends", len(_SSM.probe_sends))
    fields.setdefault("ec2_calls", _EC2.calls)
    return json.dumps(fields, indent=2, default=str)


def _seed_queued_ephemeral_job(job_id, target="JP6", budget_minutes=20,
                               source_ref=None, created_at=BASE_TIME):
    """A queued ephemeral Build_Job, ready for the provisioning pass."""
    snapshot = {
        "arm64_instance_type": "m6g.4xlarge",
        "x86_64_instance_type": "m6i.4xlarge",
        "volume_size_gb": 100,
        "region": "us-east-1",
        "max_runtime_hours": 4,
        "use_spot_for_ephemeral": False,
        "source_ref": source_ref,
        "bootstrap_timeout_minutes": budget_minutes,
    }
    _JOBS.put_item(Item={
        "build_job_id": job_id,
        "request_id": f"req-{job_id}",
        "request_order": 0,
        "predecessor_job_id": None,
        "build_target": target,
        "component_name": f"dda-{target.lower()}",
        "required_arch": build_domain.required_arch_for_target(target),
        "execution_mode": "ephemeral",
        "status": STATUS_QUEUED,
        "requested_by": "gate-test",
        "created_at": created_at,
        "config_snapshot": snapshot,
    })


def _tick(now):
    """One dispatcher provisioning pass over the CURRENT stored jobs."""
    jobs = build_dispatcher.scan_all(build_dispatcher.jobs_table())
    build_dispatcher.provision_ephemeral(jobs, now)


def _run_provisioning_tick(job_id, now):
    """The tick that provisions the runner, with the assertions that make
    the later readiness ticks meaningful."""
    _SSM.probe_output = None
    _tick(now)
    job = _job(job_id)
    observed = _observation(job=job, now=now)
    assert job["status"] == STATUS_PROVISIONING, observed
    assert job["dispatched_at"] == now, observed
    assert job["runner"]["instance_id"], observed
    # The provisioning pass itself never probes and never sends the agent:
    # the runner does not exist yet.
    assert _SSM.agent_sends == [], observed
    assert _SSM.probe_sends == [], observed
    return job


def _tick_time(time_kind, dispatched_at, budget_minutes, nudge=0):
    deadline = dispatched_at + budget_minutes * MS_PER_MINUTE
    if time_kind == "inside_early":
        return dispatched_at + 140_000  # the observed live ~140 s
    if time_kind == "inside_late":
        return deadline - 1
    if time_kind == "at_deadline":
        return deadline
    return deadline + 1 + nudge


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

probe_kinds = st.sampled_from(sorted(PROBE_OUTPUTS))
time_kinds = st.sampled_from(TIME_KINDS)
#: >= 3 minutes so the observed live probe time (~140 s) is genuinely
#: inside the budget for every generated job.
budget_minutes = st.integers(min_value=3, max_value=8)
targets = st.sampled_from(TARGETS)
source_refs = st.sampled_from([
    None, "main", "feature/portal-build-fleet-and-workflow-gates",
    "v1.2.3", "a" * 40])

#: A dispatcher tick: what the marker probe reports, and when the tick
#: happens relative to the bootstrap budget deadline.
ticks = st.lists(st.tuples(probe_kinds, time_kinds), min_size=1, max_size=5)


def _ordered(tick_list):
    """Ticks in time order (a dispatcher tick sequence advances)."""
    return sorted(tick_list, key=lambda t: _TIME_RANK[t[1]])


# ---------------------------------------------------------------------------
# Property 6 — Bootstrap completion gates the agent command
# (Req 6.1, 6.2, 6.3, 6.4)
# ---------------------------------------------------------------------------

class TestProperty6BootstrapGatesTheAgentCommand:
    """Generated probe-output/time sequences driven through
    `build_dispatcher.provision_ephemeral` over mocked SSM/EC2."""

    @given(tick_list=ticks, budget=budget_minutes, target=targets,
           source_ref=source_refs)
    @settings(max_examples=150, deadline=None,
              suppress_health_check=[HealthCheck.too_slow,
                                     HealthCheck.function_scoped_fixture])
    def test_no_agent_command_before_an_observed_marker(
            self, tick_list, budget, target, source_ref):
        """For ANY generated sequence of ticks over a provisioning runner:

        * no agent SendCommand in any tick where the Bootstrap_Marker has
          not been observed (Req 6.1, 6.2);
        * still WAIT at `now == deadline`, TIMEOUT strictly past it, with
          `bootstrap` recorded as the failing stage (Req 6.3);
        * a marker observed alongside inner-step failure text is READY,
          with the bootstrap log location already on the job record when
          the agent command is sent (Req 6.4).
        """
        _clear_tables()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        _seed_queued_ephemeral_job(job_id, target=target,
                                   budget_minutes=budget,
                                   source_ref=source_ref)
        with _MockedAws():
            provisioned = _run_provisioning_tick(job_id, BASE_TIME)
            deadline = BASE_TIME + budget * MS_PER_MINUTE
            instance_id = provisioned["runner"]["instance_id"]

            sent = False
            failed = False
            for index, (probe_kind, time_kind) in enumerate(
                    _ordered(tick_list)):
                now = _tick_time(time_kind, BASE_TIME, budget, nudge=index)
                marker_observed = probe_kind in MARKER_OBSERVED_KINDS
                _SSM.probe_output = PROBE_OUTPUTS[probe_kind]
                _SSM.probe_completes = probe_kind != "unverifiable"
                agents_before = len(_SSM.agent_sends)
                probes_before = len(_SSM.probe_sends)

                _tick(now)

                job = _job(job_id)
                new_agent_sends = _SSM.agent_sends[agents_before:]
                new_probe_sends = _SSM.probe_sends[probes_before:]
                observed = _observation(
                    tick=index, probe_kind=probe_kind, time_kind=time_kind,
                    now=now, deadline=deadline, budget=budget,
                    marker_observed=marker_observed, job=job,
                    sent_before=sent, failed_before=failed)

                if sent or failed:
                    # A dispatched (or failed) job is out of the gate's
                    # hands: nothing is probed and nothing is sent again.
                    assert new_agent_sends == [], observed
                    assert new_probe_sends == [], observed
                    continue

                # Still gated: the marker is probed exactly once per tick.
                assert len(new_probe_sends) == 1, observed
                assert new_probe_sends[0]["instance_id"] == instance_id, \
                    observed

                if marker_observed:
                    # READY — and ONLY here may a command be sent.
                    assert len(new_agent_sends) == 1, observed
                    send = new_agent_sends[0]
                    assert send["instance_id"] == instance_id, observed
                    assert send["build_job_id"] == job_id, observed
                    assert send["cloudwatch"] is True, observed
                    # Req 6.4: the readiness evidence and the bootstrap log
                    # location were on the record BEFORE the command left.
                    at_send = build_dispatcher.to_native(send["job_at_send"])
                    bootstrap = (at_send or {}).get("bootstrap") or {}
                    assert bootstrap.get("log_path") == \
                        EXPECTED_LOG_PATH[probe_kind], observed
                    assert bootstrap.get("marker_at") == now, observed
                    # The job stays provisioning; the send is recorded.
                    assert job["status"] == STATUS_PROVISIONING, observed
                    assert job["ssm"]["command_id"] == send["command_id"], \
                        observed
                    assert job["log"]["stream"].startswith(
                        send["command_id"]), observed
                    sent = True
                    continue

                # Marker NOT observed: no command, ever.
                assert new_agent_sends == [], observed
                if now > deadline:
                    # TIMEOUT: failed at the bootstrap stage (Req 6.3).
                    assert job["status"] == STATUS_FAILED, observed
                    assert job["error"]["code"] == \
                        ERROR_BOOTSTRAP_TIMEOUT, observed
                    assert f"{budget} minutes" in job["error"]["message"], \
                        observed
                    assert LOG_PATH in job["error"]["message"], observed
                    audits = _audits_for(job_id, AUDIT_BOOTSTRAP_TIMEOUT)
                    assert len(audits) == 1, observed
                    assert audits[0]["details"]["stage"] == "bootstrap", \
                        observed
                    failed = True
                else:
                    # WAIT — including exactly AT the deadline: nothing
                    # happens this tick and the job keeps waiting.
                    assert job["status"] == STATUS_PROVISIONING, observed
                    assert (job.get("ssm") or {}).get("command_id") is None, \
                        observed
                    assert "error" not in job, observed
                    assert _audits_for(job_id) == [], observed

            # At most one agent command per Build_Job, ever.
            assert len(_SSM.agent_sends) <= 1, _observation(
                budget=budget, job=_job(job_id))

    def test_the_observed_live_race_sends_nothing(self):
        """The counterexample this gate exists for: the runner's SSM agent
        pings Online and the command was requested at 21:36:59Z while
        cloud-init did not finish until 21:38:54Z (`Up 140.42 seconds`).
        With the marker absent 140 s in, the tick must send nothing."""
        _clear_tables()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        _seed_queued_ephemeral_job(job_id, target="JP5", budget_minutes=20)
        with _MockedAws():
            _run_provisioning_tick(job_id, BASE_TIME)
            _SSM.probe_output = PROBE_OUTPUTS["absent"]
            _tick(BASE_TIME + 140_000)
            job = _job(job_id)
            observed = _observation(job=job)
            assert _SSM.agent_sends == [], observed
            assert job["status"] == STATUS_PROVISIONING, observed
            assert (job.get("ssm") or {}).get("command_id") is None, observed
            # ... and once the marker appears, the same job goes.
            _SSM.probe_output = PROBE_OUTPUTS["done_with_inner_failures"]
            _tick(BASE_TIME + 141_000)
            job = _job(job_id)
            observed = _observation(job=job)
            assert len(_SSM.agent_sends) == 1, observed
            assert job["bootstrap"]["log_path"] == LOG_PATH, observed
            assert AGENT_SCRIPT_RELPATH in _SSM.agent_sends[0]["text"], \
                observed

    def test_an_offline_runner_is_never_probed_or_sent_to(self):
        """SSM Online is necessary before the gate is even consulted, so an
        instance that is not yet SSM-managed costs no probe and gets no
        command."""
        _clear_tables()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        _seed_queued_ephemeral_job(job_id, budget_minutes=3)
        with _MockedAws():
            _run_provisioning_tick(job_id, BASE_TIME)
            _SSM.online = False
            _SSM.probe_output = PROBE_OUTPUTS["done"]
            _tick(BASE_TIME + 1000)
            observed = _observation(job=_job(job_id))
            assert _SSM.probe_sends == [], observed
            assert _SSM.agent_sends == [], observed
            assert _job(job_id)["status"] == STATUS_PROVISIONING, observed


# ---------------------------------------------------------------------------
# Property 7 — Provisioning/bootstrap failures release the runner (Req 6.5)
# ---------------------------------------------------------------------------

#: The failure causes an ephemeral Build_Job can take before it ever
#: reaches `building`, and the error code each records.
FAILURE_CAUSES = {
    # RunInstances rejected the launch (capacity, quota, permissions).
    "run_instances_client_error": ERROR_PROVISIONING_FAILED,
    # An unplannable launch (bad sizing/target) raising ValueError.
    "run_instances_value_error": ERROR_PROVISIONING_FAILED,
    # The bootstrap never signalled completion inside its budget.
    "bootstrap_timeout": ERROR_BOOTSTRAP_TIMEOUT,
    # The runner was ready but the agent SendCommand itself failed.
    "agent_send_failure": ERROR_PROVISIONING_FAILED,
}

AUDIT_ACTION_FOR_CAUSE = {
    "run_instances_client_error": AUDIT_PROVISIONING_FAILED,
    "run_instances_value_error": AUDIT_PROVISIONING_FAILED,
    "bootstrap_timeout": AUDIT_BOOTSTRAP_TIMEOUT,
    "agent_send_failure": AUDIT_PROVISIONING_FAILED,
}

failure_causes = st.sampled_from(sorted(FAILURE_CAUSES))
#: Compute a failed launch may leave behind (0 = the launch created none).
partial_compute_counts = st.integers(min_value=0, max_value=2)


class TestProperty7FailuresReleaseTheRunner:
    """Every recorded provisioning/bootstrap failure is accompanied by a
    termination request for that job's compute in the SAME pass — through
    both `fail_provisioning` and `fail_bootstrap_timeout`."""

    @given(cause=failure_causes, partial=partial_compute_counts,
           target=targets, budget=budget_minutes)
    @settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_every_failure_terminates_that_jobs_compute_in_the_same_pass(
            self, cause, partial, target, budget):
        _clear_tables()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        _seed_queued_ephemeral_job(job_id, target=target,
                                   budget_minutes=budget)
        with _MockedAws():
            if cause == "run_instances_client_error":
                _EC2.run_error = _client_error(
                    "InsufficientInstanceCapacity",
                    "There is no Spot capacity available",
                    "RunInstances")
                _EC2.partial_compute = partial
                failing_tick_at = len(_EC2.calls)
                _tick(BASE_TIME)
            elif cause == "run_instances_value_error":
                _EC2.run_error = ValueError(
                    "the runner sizing is not plannable")
                _EC2.partial_compute = partial
                failing_tick_at = len(_EC2.calls)
                _tick(BASE_TIME)
            elif cause == "bootstrap_timeout":
                _run_provisioning_tick(job_id, BASE_TIME)
                _SSM.probe_output = PROBE_OUTPUTS["absent"]
                failing_tick_at = len(_EC2.calls)
                _tick(BASE_TIME + budget * MS_PER_MINUTE + 1)
            else:  # agent_send_failure
                _run_provisioning_tick(job_id, BASE_TIME)
                _SSM.probe_output = PROBE_OUTPUTS["done"]
                _SSM.agent_send_error = _client_error(
                    "InvalidInstanceId",
                    "Instances not in a valid state for SendCommand",
                    "SendCommand")
                failing_tick_at = len(_EC2.calls)
                _tick(BASE_TIME + 1000)

            job = _job(job_id)
            pass_calls = _EC2.calls[failing_tick_at:]
            tagged = _EC2.instances_for(job_id)
            terminated = [i for call in pass_calls
                          if call["op"] == "terminate_instances"
                          for i in call["instance_ids"]]
            observed = _observation(cause=cause, partial=partial, job=job,
                                    pass_calls=pass_calls, tagged=tagged,
                                    audits=_audits_for(job_id))

            # The failure is recorded.
            assert job["status"] == STATUS_FAILED, observed
            assert job["error"]["code"] == FAILURE_CAUSES[cause], observed
            assert job["ended_at"] is not None, observed
            audits = _audits_for(job_id, AUDIT_ACTION_FOR_CAUSE[cause])
            assert len(audits) == 1, observed
            assert audits[0]["result"] == "failure", observed

            # ... and the SAME pass requested termination of this job's
            # compute: the tagged lookup happened, every instance tagged
            # for the job was terminated, and none is left running.
            lookups = [c for c in pass_calls
                       if c["op"] == "describe_instances"]
            assert [c["job_id"] for c in lookups] == [job_id], observed
            assert sorted(terminated) == tagged, observed
            assert _EC2.live_instances_for(job_id) == [], observed
            # The audit entry names exactly what was released, so the
            # release is evidenced on the record too (Req 6.5).
            assert sorted(
                audits[0]["details"]["terminated_partial_compute"]) == \
                tagged, observed

            if cause == "bootstrap_timeout":
                # Bootstrap stage, with the log location to look in.
                assert audits[0]["details"]["stage"] == "bootstrap", observed
                assert audits[0]["details"]["bootstrap_log"] == LOG_PATH, \
                    observed
                assert _SSM.agent_sends == [], observed
            if cause == "agent_send_failure":
                # The send was ATTEMPTED and raised; the job did not stay
                # in provisioning behind a command that never landed.
                assert len(_SSM.agent_sends) == 1, observed
                assert _SSM.agent_sends[0].get("raised") is True, observed
                assert (job.get("ssm") or {}).get("command_id") is None, \
                    observed

    def test_a_failed_job_is_not_reprocessed_on_later_ticks(self):
        """Terminal is terminal: a job failed at the bootstrap stage is not
        probed, not re-terminated, and never dispatched by a later tick."""
        _clear_tables()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        _seed_queued_ephemeral_job(job_id, budget_minutes=3)
        with _MockedAws():
            _run_provisioning_tick(job_id, BASE_TIME)
            _SSM.probe_output = PROBE_OUTPUTS["absent"]
            _tick(BASE_TIME + 3 * MS_PER_MINUTE + 1)
            assert _job(job_id)["status"] == STATUS_FAILED
            calls_after_failure = len(_EC2.calls)
            probes_after_failure = len(_SSM.probe_sends)
            _SSM.probe_output = PROBE_OUTPUTS["done"]
            _tick(BASE_TIME + 4 * MS_PER_MINUTE)
            observed = _observation(job=_job(job_id))
            assert _SSM.agent_sends == [], observed
            assert _SSM.probe_sends[probes_after_failure:] == [], observed
            assert _EC2.calls[calls_after_failure:] == [], observed
            assert len(_audits_for(job_id, AUDIT_BOOTSTRAP_TIMEOUT)) == 1, \
                observed


# ---------------------------------------------------------------------------
# Unit: the generated ephemeral user-data writes the marker LAST (Req 6.2)
# ---------------------------------------------------------------------------

_BASH = "/bin/bash"


def _statements(user_data):
    """The non-blank statement lines of a generated bootstrap script."""
    return [line for line in user_data.splitlines() if line.strip()]


def _marker_indexes(statements):
    return [i for i, line in enumerate(statements) if MARKER_PATH in line]


class TestEphemeralUserDataWritesTheMarkerLast:
    """`build_dispatcher.runner_bootstrap_user_data` must end with the
    Bootstrap_Marker write, because the whole gate reads that marker as
    "bootstrap ran to completion".

    Asserted structurally, not as an exact string: task 5.2 made the write
    non-fatal (`|| true`) and the log redirect conditional, so what matters
    is that the marker is written exactly once, after the source sync and
    the build-environment setup, and that nothing follows it.
    """

    @given(source_ref=source_refs, target=targets,
           repo_dir=st.sampled_from([
               None, "/home/ubuntu/DefectDetectionApplication",
               "/opt/dda/DefectDetectionApplication",
               "/var/lib/dda/tree with spaces"]))
    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_the_marker_write_is_the_last_statement(self, source_ref, target,
                                                    repo_dir):
        job = {
            "build_job_id": "job-user-data",
            "build_target": target,
            "execution_mode": "ephemeral",
            "config_snapshot": {"source_ref": source_ref},
        }
        user_data = build_dispatcher.runner_bootstrap_user_data(
            job, repo_dir=repo_dir)
        statements = _statements(user_data)
        indexes = _marker_indexes(statements)
        observed = json.dumps({"source_ref": source_ref,
                               "repo_dir": repo_dir,
                               "statements": statements}, indent=2)

        # Written exactly once, and it is the LAST statement.
        assert len(indexes) == 1, observed
        assert indexes[0] == len(statements) - 1, observed

        marker_statement = statements[-1]
        tokens = marker_statement.split()
        assert tokens[0] == "touch", observed
        # The touched path is the marker itself (quoting tolerated), and
        # anything after it is only the non-fatal guard task 5.2 added.
        assert MARKER_PATH in marker_statement, observed
        trailing = marker_statement.split(MARKER_PATH, 1)[1]
        assert trailing.strip().strip("'\"") in ("", "|| true"), observed

        # It comes AFTER the source sync and the build-environment setup:
        # the marker means the whole bootstrap ran, not just its start.
        setup_indexes = [i for i, line in enumerate(statements)
                         if SETUP_SCRIPT_RELPATH in line]
        assert setup_indexes, observed
        assert max(setup_indexes) < indexes[0], observed
        clone_indexes = [i for i, line in enumerate(statements)
                         if "git clone" in line]
        assert clone_indexes, observed
        assert max(clone_indexes) < indexes[0], observed
        if source_ref:
            checkout_indexes = [i for i, line in enumerate(statements)
                                if "git checkout" in line]
            assert checkout_indexes, observed
            assert max(checkout_indexes) < indexes[0], observed

        # The bootstrap log the gate records is set up before the body, so
        # a bootstrap that never reaches the marker is still diagnosable.
        log_indexes = [i for i, line in enumerate(statements)
                       if LOG_PATH in line]
        assert log_indexes, observed
        assert min(log_indexes) < indexes[0], observed

    @pytest.mark.skipif(not os.path.exists(_BASH), reason="bash unavailable")
    @given(source_ref=source_refs)
    @settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    def test_the_generated_bootstrap_parses(self, source_ref):
        """`bash -n` (parse only — nothing is executed): the conditional
        redirect and the tolerated marker write must still be a valid
        script, or the marker would never be reached on a real runner."""
        job = {
            "build_job_id": "job-user-data",
            "build_target": "JP6",
            "execution_mode": "ephemeral",
            "config_snapshot": {"source_ref": source_ref},
        }
        user_data = build_dispatcher.runner_bootstrap_user_data(job)
        with tempfile.NamedTemporaryFile("w", suffix=".sh",
                                         delete=False) as handle:
            handle.write(user_data)
            path = handle.name
        try:
            result = subprocess.run([_BASH, "-n", path],
                                    capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(path)

    def test_the_marker_path_is_read_from_its_one_definition(self):
        """The dispatcher does not re-spell the marker or the log: the
        generated user-data and the probe both use build_planner's paths,
        so the writer and the reader cannot drift."""
        assert build_planner.BOOTSTRAP_MARKER_PATH == MARKER_PATH
        assert build_planner.BOOTSTRAP_LOG_PATH == LOG_PATH
        with open(os.path.join(FUNCTIONS_DIR, "build_dispatcher.py")) as fh:
            source = fh.read()
        assert "dda-build-server-bootstrap" not in source

    def test_the_probe_reads_exactly_what_the_bootstrap_writes(self):
        """Round trip: the probe text tests the marker the user-data writes
        and emits the `KEY=value` lines the readiness decision parses."""
        probe_text = "\n".join(build_dispatcher.BOOTSTRAP_PROBE_COMMANDS)
        assert MARKER_PATH in probe_text
        assert LOG_PATH in probe_text
        # What the probe reports on a completed bootstrap, parsed by the
        # decision, is READY with the log path surfaced.
        job = {"build_job_id": "job-probe", "dispatched_at": BASE_TIME,
               "config_snapshot": {"bootstrap_timeout_minutes": 20}}
        decision = build_planner.decide_runner_readiness(
            job, f"BOOTSTRAP_DONE=1\nBOOTSTRAP_LOG={LOG_PATH}",
            BASE_TIME + 140_000)
        assert decision.readiness == build_planner.READINESS_READY
        assert decision.log_path == LOG_PATH
