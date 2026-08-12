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
Moto integration test for a MIXED-BATCH dispatcher tick — a JP5 and a
JP7 ephemeral Build_Job dispatched together (task 8.2 of
jp7-ephemeral-runner-provisioning).

**Validates: Requirements 2.1, 3.1, 3.3**

End-to-end ``run_tick`` executions over moto-mocked DynamoDB / EC2 / SSM,
following the ``test_dispatcher_tick_integration.py`` /
``test_jp7_dispatcher_tick_integration.py`` conventions:

- Each job in the batch is provisioned from its OWN release's AMI,
  resolved through its own release's SSM parameter seam: NO env AMI pins
  are set (``BUILD_ARM64_AMI_ID`` / ``BUILD_X86_64_AMI_ID`` /
  ``BUILD_ARM64_NOBLE_AMI_ID`` all absent), so the jammy (22.04)
  parameter is the only way JP5 can resolve and the noble (24.04)
  parameter the only way JP7 can (Req 2.1, 3.1). The two parameters are
  seeded with DISTINCT moto AMI ids; the RunInstances ImageIds prove
  which resolution answered which job.
- The JP5 job's plan fields (captured from the REAL planner via a
  non-mutating recorder), its resolved AMI, and its bootstrap user-data
  text are byte-preserved against the task 2 frozen oracle re-spelled
  in this file (Req 3.1, 3.3, 3.4). The JP7 job's user-data is the SAME
  frozen template (byte-identical modulo its own sync inputs) — the
  noble deltas travel in ``setup-build-server.sh``, never in user-data.
- Neither job's cache entry pollutes the other: after the tick,
  ``_AMI_CACHE`` holds exactly the jammy entry under the legacy
  ``'arm64'`` key and the noble entry under the distinct
  ``'24.04/arm64'`` key, with distinct values; and a SECOND tick's
  JP5+JP7 batch is served entirely from the cache (zero further SSM
  GetParameter reads) with each new runner still launching from its own
  release's AMI (Req 2.1, 3.1).

The frozen oracles below (plan matrix, bootstrap template, marker
statement, default repo dir) are deliberate RE-SPELLINGS of the task 2
observation baselines in
``test_jp7_ephemeral_preservation.py`` — never imported, never read
back from the modules under test.

DynamoDB, EC2, and SSM are real moto; ``instance_ssm_online`` is stubbed
False on the second tick so the first tick's provisioning jobs stay
parked at readiness while the second batch provisions. shared_utils is
replaced by a minimal fake (the sibling standalone-suite pattern).
"""
import base64
import os
import shlex
import sys
import types
from unittest import mock

# ---------------------------------------------------------------------------
# Environment BEFORE any import: build_dispatcher binds its boto3
# resources/clients and env-derived settings at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_JOBS_TABLE = "build-jobs-jp7t82"
_SERVERS_TABLE = "build-servers-jp7t82"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE

#: The configured repository: WITH a repo URL the runner user-data
#: bootstrap is generated, which is what the byte-preservation oracle
#: compares (Req 3.3).
_REPO_URL = "https://github.com/dda-test/DefectDetectionApplication"
os.environ["BUILD_REPO_URL"] = _REPO_URL
os.environ.pop("BUILD_REPO_DIR", None)  # -> the authoritative default dir
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

# NO AMI pins of either scope: resolution can only go env override ->
# cache -> SSM parameter, and with the overrides absent the parameter
# seams (and then the caches) are the only sources (Req 2.1, 3.1).
os.environ.pop("BUILD_ARM64_AMI_ID", None)
os.environ.pop("BUILD_X86_64_AMI_ID", None)
os.environ.pop("BUILD_ARM64_NOBLE_AMI_ID", None)

# The canonical parameter paths live under /aws/..., which moto's
# put_parameter rejects, so both env-overridable parameter NAMES (both
# pre-existing deployment knobs) are pointed at moto-writable paths.
# The jammy parameter answers JP5; the noble parameter answers JP7.
#: Suite-unique path prefix: sibling modules' module-scope moto mocks
#: stay active for the whole pytest session, so parameter names must not
#: collide across files.
_JAMMY_AMI_PARAMETER = "/dda-test/jp7t82/canonical/ubuntu/jammy/arm64/ami-id"
_NOBLE_AMI_PARAMETER = "/dda-test/jp7t82/canonical/ubuntu/noble/arm64/ami-id"
os.environ["ARM64_AMI_SSM_PARAMETER"] = _JAMMY_AMI_PARAMETER
os.environ["ARM64_NOBLE_AMI_SSM_PARAMETER"] = _NOBLE_AMI_PARAMETER
os.environ.pop("X86_64_AMI_SSM_PARAMETER", None)

# Import boto3 (and thus botocore/urllib3) from the test environment BEFORE
# the Lambda function directory joins sys.path.
import boto3  # noqa: E402

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call (sibling-suite shim; S3-Select is never exercised here).
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
# Fake shared_utils (build_dispatcher imports only log_audit_event from the
# layer). This suite asserts no audit content, so the fake is a sink.
# ---------------------------------------------------------------------------


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        pass

    module.log_audit_event = log_audit_event
    return module


# Fresh modules so build_dispatcher's module-level boto3 handles and env
# bindings are created under the moto mock started below (sibling pattern).
for _module in ("build_dispatcher", "build_planner", "build_domain",
                "build_source", "shared_utils"):
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

_EC2 = boto3.client("ec2", region_name="us-east-1")
_SSM = boto3.client("ssm", region_name="us-east-1")


def _two_distinct_ami_ids():
    """Two DISTINCT moto AMI ids: one seeds the jammy parameter, the
    other the noble parameter. Their distinctness is what lets the test
    prove which release's resolution produced each runner's ImageId."""
    images = _EC2.describe_images(Owners=["amazon"]).get("Images", [])
    ids = sorted({image["ImageId"] for image in images})
    while len(ids) < 2:  # pragma: no cover - moto version dependent
        ids.append(_EC2.register_image(
            Name=f"dda-test-ami-{len(ids)}", RootDeviceName="/dev/sda1",
            VirtualizationType="hvm")["ImageId"])
    return ids[0], ids[1]


_JAMMY_AMI_ID, _NOBLE_AMI_ID = _two_distinct_ami_ids()
assert _JAMMY_AMI_ID != _NOBLE_AMI_ID

_SSM.put_parameter(Name=_JAMMY_AMI_PARAMETER, Type="String",
                   Value=_JAMMY_AMI_ID)
_SSM.put_parameter(Name=_NOBLE_AMI_PARAMETER, Type="String",
                   Value=_NOBLE_AMI_ID)

import build_domain  # noqa: E402
import build_planner  # noqa: E402
import build_dispatcher  # noqa: E402

# The env knobs above are all bound by the imports; drop the ones this
# file customized so later-collected sibling modules importing fresh
# copies see a clean environment.
for _var in ("BUILD_REPO_URL", "ARM64_AMI_SSM_PARAMETER",
             "ARM64_NOBLE_AMI_SSM_PARAMETER"):
    os.environ.pop(_var, None)


# ---------------------------------------------------------------------------
# THE FROZEN ORACLE — re-spelled from the task 2 preservation baseline
# (test_jp7_ephemeral_preservation.py), which recorded it from UNFIXED
# code. Nothing here is read back from the modules under test.
# ---------------------------------------------------------------------------

#: Non-JP7 ephemeral planning oracle for JP5 (Req 3.4 subset used here):
#: arch, snapshot instance-type key, default instance type, defaults.
FROZEN_JP5_ARCH = "arm64"
FROZEN_JP5_TYPE_KEY = "arm64_instance_type"
FROZEN_JP5_DEFAULT_TYPE = "m6g.4xlarge"
FROZEN_DEFAULT_VOLUME_GB = 100
FROZEN_PLAN_STATUS = "provisioning"

#: The authoritative default repository directory (Req 5.3 default) —
#: the directory the bootstrap clones into when nothing overrides it.
FROZEN_DEFAULT_REPO_DIR = "/home/ubuntu/DefectDetectionApplication"

#: The root-written Bootstrap_Marker statement the ephemeral bootstrap
#: text ends with (recorded verbatim).
FROZEN_MARKER_STATEMENT = \
    "touch /var/log/dda-build-server-bootstrap.done || true"


def _sync_guard(kind, exit_code):
    """The recorded Source_Sync failure guard text (no event emission —
    the ephemeral seam passes no event bus)."""
    return ('{ echo "PORTAL_SOURCE_SYNC_FAILED kind=%s '
            'repository=$REPO_URL ref=$SOURCE_REF"; exit %d; }'
            % (kind, exit_code))


_GUARD_UNREACHABLE = _sync_guard("repository_unreachable", 65)
_GUARD_REF = _sync_guard("ref_not_found", 66)


def _frozen_sync_lines(repo_url, repo_dir, source_ref):
    """The recorded Source_Sync block: the three shlex-quoted
    assignments, clone-if-absent, cd, and — only when a ref was
    selected — fetch + checkout."""
    lines = [
        "REPO_DIR=" + shlex.quote(repo_dir),
        "REPO_URL=" + shlex.quote(repo_url),
        "SOURCE_REF=" + shlex.quote(source_ref),
        'git config --global --add safe.directory "$REPO_DIR" '
        "2>/dev/null || true",
        'mkdir -p "$(dirname "$REPO_DIR")"',
        'if [ ! -d "$REPO_DIR/.git" ]; then',
        '  git clone "$REPO_URL" "$REPO_DIR" || ' + _GUARD_UNREACHABLE,
        "fi",
        'cd "$REPO_DIR" || ' + _GUARD_UNREACHABLE,
    ]
    if source_ref:
        lines += [
            "git fetch --prune origin || " + _GUARD_UNREACHABLE,
            'if git rev-parse --verify --quiet '
            '"refs/remotes/origin/$SOURCE_REF" >/dev/null 2>&1; then',
            '  git checkout --force -B "$SOURCE_REF" "origin/$SOURCE_REF" '
            "|| " + _GUARD_REF,
            "else",
            '  git checkout --force "$SOURCE_REF" || ' + _GUARD_REF,
            "fi",
        ]
    return lines


def frozen_runner_bootstrap(repo_url, repo_dir, source_ref, region):
    """FROZEN byte-level oracle for `runner_bootstrap_user_data`,
    re-spelled from the output recorded on unfixed code (the task 2
    observation run). Inputs mirror the real inputs: the configured
    repository URL, the resolved repo dir, the job's selected ref (''
    when none), and the dispatch region."""
    qdir = shlex.quote(repo_dir)
    body = ['export HOME="${HOME:-/home/ubuntu}"']
    if region:
        qregion = shlex.quote(region)
        body += ["export AWS_DEFAULT_REGION=" + qregion,
                 "export AWS_REGION=" + qregion]
    body += ['export PATH="${HOME:-/home/ubuntu}/.local/bin:$PATH"']
    body += _frozen_sync_lines(repo_url, repo_dir, source_ref)
    body += ["bash ./setup-build-server.sh"]
    return "\n".join([
        "#!/bin/bash",
        "set -uo pipefail",
        "BOOTSTRAP_LOG=/var/log/dda-build-server-bootstrap.log",
        'if : > "$BOOTSTRAP_LOG" 2>/dev/null; then',
        '  exec >> "$BOOTSTRAP_LOG" 2>&1',
        "fi",
        'export HOME="${HOME:-/root}"',
        "export DEBIAN_FRONTEND=noninteractive",
        "apt-get update -y && apt-get install -y git",
        'mkdir -p "$(dirname %s)"' % qdir,
        'chown ubuntu:ubuntu "$(dirname %s)" 2>/dev/null || true' % qdir,
        "if [ -d %s ]; then chown -R ubuntu:ubuntu %s 2>/dev/null || true; fi"
        % (qdir, qdir),
        'PORTAL_RUN_SCRIPT="$(mktemp /tmp/portal-build-run.XXXXXX)" '
        "|| exit 1",
        "cat > \"$PORTAL_RUN_SCRIPT\" <<'PORTAL_RUN_EOF'",
        "\n".join(body),
        "PORTAL_RUN_EOF",
        'chmod 644 "$PORTAL_RUN_SCRIPT"',
        'if [ "$(id -u)" = "0" ] && id ubuntu >/dev/null 2>&1; then',
        '  sudo -H -u ubuntu bash "$PORTAL_RUN_SCRIPT"',
        "else",
        '  bash "$PORTAL_RUN_SCRIPT"',
        "fi",
        'PORTAL_RUN_STATUS="$?"',
        'rm -f "$PORTAL_RUN_SCRIPT"',
        'case "$PORTAL_RUN_STATUS" in',
        '  65|66) exit "$PORTAL_RUN_STATUS";;',
        "esac",
        FROZEN_MARKER_STATEMENT,
        "",
    ])


# ---------------------------------------------------------------------------
# Helpers (sibling conventions)
# ---------------------------------------------------------------------------

NOW = 1_700_000_000_000  # ms epoch anchor for deterministic tick times
_MINUTE_MS = 60 * 1000


def _clear_tables():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_job(job_id, build_target, created_at=NOW, config_snapshot=None):
    item = {
        "build_job_id": job_id,
        "build_target": build_target,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": build_domain.STATUS_QUEUED,
        "requested_by": "operator-1",
        "created_at": created_at,
        "config_snapshot": config_snapshot if config_snapshot is not None
        else {"max_runtime_hours": 4},
    }
    _JOBS.put_item(Item=item)
    return item


def _runner_instances_for_job(job_id):
    """Instances tagged dda-build:job-id = job_id (any state)."""
    response = _EC2.describe_instances(Filters=[
        {"Name": f"tag:{build_dispatcher.TAG_JOB_ID}", "Values": [job_id]},
    ])
    return [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]


def _instance_user_data(instance_id):
    """The decoded launch user-data text of a moto instance."""
    attribute = _EC2.describe_instance_attribute(
        InstanceId=instance_id, Attribute="userData")
    encoded = (attribute.get("UserData") or {}).get("Value", "")
    return base64.b64decode(encoded).decode("utf-8") if encoded else ""


def _root_volume_size(instance):
    """The size (GiB) of the instance's /dev/sda1 EBS volume."""
    volume_ids = [
        mapping["Ebs"]["VolumeId"]
        for mapping in instance.get("BlockDeviceMappings", [])
        if mapping.get("DeviceName") == "/dev/sda1" and mapping.get("Ebs")
    ]
    assert len(volume_ids) == 1
    volumes = _EC2.describe_volumes(VolumeIds=volume_ids)["Volumes"]
    return volumes[0]["Size"]


class _RecordingSsm:
    """Delegating proxy over the REAL (moto) SSM client that records the
    parameter Name of every GetParameter read. Resolution behavior is
    entirely the real client's; only observation is added."""

    def __init__(self, real):
        self._real = real
        self.parameter_reads = []

    def get_parameter(self, **kwargs):
        self.parameter_reads.append(kwargs.get("Name"))
        return self._real.get_parameter(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_tick_recording(now):
    """One real tick with (a) a non-mutating recorder around the REAL
    planner, capturing the plans the dispatcher executed, and (b) the
    recording SSM proxy, capturing every parameter read. Returns
    (captured_plans, parameter_reads)."""
    real_plan_provisioning = build_planner.plan_ephemeral_provisioning
    captured = []

    def recording_planner(jobs):
        plans = real_plan_provisioning(jobs)
        captured.extend(plans)
        return plans

    proxy = _RecordingSsm(build_dispatcher.ssm)
    with mock.patch.object(build_planner, "plan_ephemeral_provisioning",
                           side_effect=recording_planner), \
            mock.patch.object(build_dispatcher, "ssm", proxy):
        build_dispatcher.run_tick(now=now)
    return captured, proxy.parameter_reads


# ---------------------------------------------------------------------------
# Mixed-batch tick: JP5 + JP7 in one tick (Req 2.1, 3.1, 3.3)
# ---------------------------------------------------------------------------

class TestMixedBatchTick:

    def setup_method(self):
        _clear_tables()
        build_dispatcher._AMI_CACHE.clear()

    def test_mixed_batch_provisions_each_release_and_preserves_jp5(self):
        """One tick over a JP5 + JP7 ephemeral batch: each job is
        provisioned from its own release's AMI through its own release's
        SSM parameter (jammy for JP5, noble for JP7; no env pins exist),
        the JP5 job's plan fields, resolved AMI, and bootstrap user-data
        text are byte-preserved against the task 2 frozen oracle, and
        the two resolutions land in DISTINCT cache entries (Req 2.1,
        3.1, 3.3)."""
        _seed_job("job-jp5-mixed", build_domain.TARGET_JP5,
                  created_at=NOW - _MINUTE_MS,
                  config_snapshot={
                      "arm64_instance_type": "c7g.8xlarge",
                      "volume_size_gb": 150,
                      "source_ref": "release/2.4",
                      "max_runtime_hours": 4,
                  })
        _seed_job("job-jp7-mixed", build_domain.TARGET_JP7,
                  config_snapshot={
                      "arm64_instance_type": "m6g.2xlarge",
                      "volume_size_gb": 120,
                      "max_runtime_hours": 4,
                  })

        plans, parameter_reads = _run_tick_recording(NOW)

        # --- The JP5 plan is byte-preserved against the task 2 frozen
        # --- planning oracle (fields asserted BY NAME, Req 3.1/3.4
        # --- preservation): snapshot-driven instance type and volume,
        # --- spot default False, status 'provisioning', and the
        # --- os_release field equal to the '22.04' default.
        plans_by_job = {plan.build_job_id: plan for plan in plans}
        assert set(plans_by_job) == {"job-jp5-mixed", "job-jp7-mixed"}
        jp5_plan = plans_by_job["job-jp5-mixed"]
        assert jp5_plan.arch == FROZEN_JP5_ARCH
        assert jp5_plan.instance_type == "c7g.8xlarge"
        assert jp5_plan.volume_size_gb == 150
        assert jp5_plan.spot is False
        assert jp5_plan.status == FROZEN_PLAN_STATUS
        assert jp5_plan.os_release == build_domain.OS_RELEASE_JAMMY
        jp7_plan = plans_by_job["job-jp7-mixed"]
        assert jp7_plan.arch == build_domain.ARCH_ARM64
        assert jp7_plan.os_release == build_domain.OS_RELEASE_NOBLE

        # --- Each job's runner launched from its OWN release's AMI, and
        # --- each release was resolved through its OWN parameter,
        # --- exactly once (Req 2.1, 3.1).
        jp5_instances = _runner_instances_for_job("job-jp5-mixed")
        jp7_instances = _runner_instances_for_job("job-jp7-mixed")
        assert len(jp5_instances) == 1
        assert len(jp7_instances) == 1
        jp5_instance, jp7_instance = jp5_instances[0], jp7_instances[0]
        assert jp5_instance["ImageId"] == _JAMMY_AMI_ID
        assert jp7_instance["ImageId"] == _NOBLE_AMI_ID
        assert jp5_instance["ImageId"] != jp7_instance["ImageId"]
        assert parameter_reads.count(_JAMMY_AMI_PARAMETER) == 1
        assert parameter_reads.count(_NOBLE_AMI_PARAMETER) == 1
        assert set(parameter_reads) == \
            {_JAMMY_AMI_PARAMETER, _NOBLE_AMI_PARAMETER}

        # --- Plan -> RunInstances plumbing carried the JP5 snapshot
        # --- sizing unchanged.
        assert jp5_instance["InstanceType"] == "c7g.8xlarge"
        assert _root_volume_size(jp5_instance) == 150
        assert jp7_instance["InstanceType"] == "m6g.2xlarge"

        # --- Runner records: each job carries its own release; the JP5
        # --- record's fields are today's shapes (Req 3.1).
        jp5_job = _get_job("job-jp5-mixed")
        jp7_job = _get_job("job-jp7-mixed")
        assert jp5_job["status"] == build_domain.STATUS_PROVISIONING
        assert jp7_job["status"] == build_domain.STATUS_PROVISIONING
        jp5_runner = jp5_job["runner"]
        assert jp5_runner["arch"] == FROZEN_JP5_ARCH
        assert jp5_runner["instance_type"] == "c7g.8xlarge"
        assert jp5_runner["os_release"] == build_domain.OS_RELEASE_JAMMY
        assert jp5_runner["repo_dir"] == FROZEN_DEFAULT_REPO_DIR
        assert jp7_job["runner"]["os_release"] == \
            build_domain.OS_RELEASE_NOBLE

        # --- The JP5 bootstrap user-data is BYTE-IDENTICAL to the task 2
        # --- frozen oracle for its inputs (Req 3.3), still ending with
        # --- the root-written Bootstrap_Marker statement.
        jp5_text = _instance_user_data(jp5_instance["InstanceId"])
        assert jp5_text == frozen_runner_bootstrap(
            _REPO_URL, FROZEN_DEFAULT_REPO_DIR, "release/2.4", "us-east-1")
        assert jp5_text.rstrip("\n").endswith(FROZEN_MARKER_STATEMENT)

        # --- The JP7 bootstrap is the SAME frozen template over its own
        # --- sync inputs (no ref selected): the noble deltas travel in
        # --- setup-build-server.sh, so neither job's user-data carries
        # --- any delta text (Req 3.3).
        jp7_text = _instance_user_data(jp7_instance["InstanceId"])
        assert jp7_text == frozen_runner_bootstrap(
            _REPO_URL, FROZEN_DEFAULT_REPO_DIR, "", "us-east-1")
        assert "--break-system-packages" not in jp5_text
        assert "--break-system-packages" not in jp7_text

        # --- Neither resolution polluted the other's cache entry: the
        # --- jammy id under the legacy 'arm64' key, the noble id under
        # --- the distinct '24.04/arm64' key, nothing else (Req 3.1).
        assert build_dispatcher._AMI_CACHE == {
            "arm64": _JAMMY_AMI_ID,
            "24.04/arm64": _NOBLE_AMI_ID,
        }

    def test_cache_entries_stay_release_scoped_across_ticks(self):
        """A second tick's JP5 + JP7 batch is served entirely from the
        per-release cache entries the first mixed batch populated — zero
        further SSM GetParameter reads — and each new runner STILL
        launches from its own release's AMI: the jammy entry never
        answers a noble resolution and vice versa (Req 2.1, 3.1)."""
        _seed_job("job-jp5-t1", build_domain.TARGET_JP5,
                  created_at=NOW - _MINUTE_MS)
        _seed_job("job-jp7-t1", build_domain.TARGET_JP7)
        _, first_reads = _run_tick_recording(NOW)
        assert set(first_reads) == \
            {_JAMMY_AMI_PARAMETER, _NOBLE_AMI_PARAMETER}

        _seed_job("job-jp5-t2", build_domain.TARGET_JP5,
                  created_at=NOW + _MINUTE_MS)
        _seed_job("job-jp7-t2", build_domain.TARGET_JP7,
                  created_at=NOW + _MINUTE_MS)

        # The first tick's provisioning jobs stay parked at readiness
        # (SSM not online yet) while the second batch provisions.
        with mock.patch.object(build_dispatcher, "instance_ssm_online",
                               return_value=False):
            _, second_reads = _run_tick_recording(NOW + 2 * _MINUTE_MS)

        # Cache hits only: no further parameter reads on either release.
        assert second_reads == []

        jp5_instances = _runner_instances_for_job("job-jp5-t2")
        jp7_instances = _runner_instances_for_job("job-jp7-t2")
        assert len(jp5_instances) == 1
        assert len(jp7_instances) == 1
        assert jp5_instances[0]["ImageId"] == _JAMMY_AMI_ID
        assert jp7_instances[0]["ImageId"] == _NOBLE_AMI_ID

        assert _get_job("job-jp5-t2")["runner"]["os_release"] == \
            build_domain.OS_RELEASE_JAMMY
        assert _get_job("job-jp7-t2")["runner"]["os_release"] == \
            build_domain.OS_RELEASE_NOBLE
