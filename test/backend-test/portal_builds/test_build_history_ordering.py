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
Property test for the GET /builds history listing of
``edge-cv-portal/backend/functions/build_jobs.py``.

Spec: .kiro/specs/portal-build-fleet-and-workflow-gates (task 7.3)

**Validates: Requirements 4.7**

For any set of Build_Jobs with random creation times (inside and outside
the 90-day window) and random statuses, walking GET /builds page by page
through the opaque ``nextToken``:

- the listing contains exactly the Build_Jobs from the preceding 90 days
  (older jobs are excluded),
- entries are ordered most recent first — ``created_at`` descending with
  ``build_job_id`` as the deterministic tie-break (``history_sort_key``),
- pagination is exact: every page but the last carries exactly ``limit``
  entries, the pages are disjoint and complete, and the final page has no
  ``nextToken``,
- every succeeded job's entry includes its published artifact identifiers
  verbatim (the ``result`` payload recorded at publish time).

The handler runs against a moto-mocked BuildJobs DynamoDB table; the
shared_utils / rbac_middleware Lambda-layer collaborators are stubbed
(the standalone-suite pattern used across test/backend-test/), because
authorization behavior is covered by task 7.4 — this property is about
history ordering and content only.
"""
import json
import os
import sys
import time
import types
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment + fake layer modules BEFORE build_jobs is imported: the
# handler binds its boto3 resource/clients and table names at import time,
# and imports shared_utils / rbac_middleware from the Lambda layer (whose
# vendored runtime dependencies must not shadow host packages here).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_JOBS_TABLE_NAME = "build-jobs-p16"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE_NAME
os.environ["BUILD_SERVERS_TABLE"] = "build-servers-p16"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_FUNCTIONS_DIR = os.path.join(
    _REPO_ROOT, "edge-cv-portal", "backend", "functions")
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


def _decimal_default(obj):
    """The shared_utils JSON serializer for DynamoDB Decimals."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


_TEST_USER = {"user_id": "user-1", "email": "user-1@example.com",
              "username": "user-1", "role": "DataScientist"}


def _fake_shared_utils():
    """shared_utils stub faithful to the real create_response envelope."""
    module = types.ModuleType("shared_utils")

    def create_response(status_code, body, headers=None):
        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": (json.dumps(body, default=_decimal_default)
                     if not isinstance(body, str) else body),
        }

    module.create_response = create_response
    module.get_user_from_event = lambda event: dict(_TEST_USER)
    module.log_audit_event = lambda *args, **kwargs: None
    return module


def _fake_rbac_middleware():
    """rbac_middleware stub: pass-through decorators (authorization
    denial behavior is task 7.4's subject, not this property's)."""
    module = types.ModuleType("rbac_middleware")

    def _passthrough_factory(*factory_args, **factory_kwargs):
        def decorator(handler):
            return handler
        return decorator

    module.require_builds_submit = _passthrough_factory
    module.require_builds_cancel = _passthrough_factory
    module.require_builds_read = _passthrough_factory
    return module


# Fresh modules (other suites in this session may have installed their own
# real or fake copies), then the fakes.
for _module in ("build_jobs", "build_domain", "shared_utils",
                "rbac_middleware"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()
sys.modules["rbac_middleware"] = _fake_rbac_middleware()

# The flask-app verification container's python3.9 is built without the
# _bz2 C extension, and moto's request path imports moto.s3 -> bz2 on
# every call (moto.core.authorization -> moto.iam.access_control ->
# moto.s3.models). bz2 is only used for S3-Select payload decompression,
# which this DynamoDB-only suite never exercises, so a minimal
# stdlib-shaped stub keeps the import chain intact where _bz2 is absent.
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

# Module-scope moto: active for build_jobs' import-time boto3 handles and
# for every request in the property below.
_MOCK = mock_aws()
_MOCK.start()

import boto3  # noqa: E402

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
_DDB.create_table(
    TableName=_JOBS_TABLE_NAME,
    KeySchema=[{"AttributeName": "build_job_id", "KeyType": "HASH"}],
    AttributeDefinitions=[{"AttributeName": "build_job_id",
                           "AttributeType": "S"}],
    BillingMode="PAY_PER_REQUEST",
)
_TABLE = _DDB.Table(_JOBS_TABLE_NAME)

import build_domain  # noqa: E402
import build_jobs  # noqa: E402

_DAY_MS = 24 * 60 * 60 * 1000

# Creation-time ages (ms before "now"). A one-day guard band on each side
# of the 90-day cutoff keeps the property deterministic against the clock
# advancing between item generation and the handler's own now_ms() call.
_FRESH_AGES = st.integers(min_value=0, max_value=89 * _DAY_MS)
_STALE_AGES = st.integers(min_value=91 * _DAY_MS, max_value=200 * _DAY_MS)

_STATUSES = sorted(build_domain.ALL_STATUSES)


@st.composite
def _job_specs(draw):
    """A random set of Build_Job specs: (in_window, age_ms, status,
    result-or-None). Ages are drawn from a small per-example pool so
    created_at ties occur and exercise the build_job_id tie-break."""
    count = draw(st.integers(min_value=0, max_value=12))
    fresh_pool = draw(st.lists(_FRESH_AGES, min_size=1, max_size=4))
    stale_pool = draw(st.lists(_STALE_AGES, min_size=1, max_size=4))
    specs = []
    for index in range(count):
        in_window = draw(st.booleans())
        age = draw(st.sampled_from(fresh_pool if in_window else stale_pool))
        status = draw(st.sampled_from(_STATUSES))
        result = None
        if status == build_domain.STATUS_SUCCEEDED:
            # Published artifact identifiers recorded on success (Req 4.7).
            result = {
                "component_name": f"aws.edgeml.dda.LocalServer.t{index}",
                "component_version": draw(st.from_regex(
                    r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}", fullmatch=True)),
                "published_image_refs": [
                    f"1234.dkr.ecr.us-east-1.amazonaws.com/dda:{index}"],
            }
        specs.append({"in_window": in_window, "age_ms": age,
                      "status": status, "result": result})
    return specs


def _clear_table():
    scan = _TABLE.scan()
    for item in scan.get("Items", []):
        _TABLE.delete_item(Key={"build_job_id": item["build_job_id"]})


def _put_jobs(specs, now_ms):
    """Materialize the specs as BuildJobs items; returns them keyed by id."""
    jobs = {}
    for index, spec in enumerate(specs):
        job = {
            "build_job_id": f"job-{index:04d}",
            "created_at": now_ms - spec["age_ms"],
            "status": spec["status"],
            "build_target": "JP5",
            "execution_mode": "ephemeral",
            "requested_by": _TEST_USER["user_id"],
        }
        if spec["result"] is not None:
            job["result"] = spec["result"]
        _TABLE.put_item(Item=job)
        jobs[job["build_job_id"]] = job
    return jobs


def _walk_history(limit):
    """Page through GET /builds with the opaque nextToken; returns the
    concatenated entries, the per-page sizes, and the reported total."""
    entries, page_sizes, next_token, total = [], [], None, None
    while True:
        params = {"limit": str(limit)}
        if next_token is not None:
            params["nextToken"] = next_token
        response = build_jobs.list_builds(
            {"httpMethod": "GET", "resource": "/builds",
             "queryStringParameters": params}, None)
        assert response["statusCode"] == 200, response["body"]
        body = json.loads(response["body"])
        entries.extend(body["jobs"])
        page_sizes.append(len(body["jobs"]))
        total = body["total"]
        next_token = body.get("nextToken")
        if next_token is None:
            return entries, page_sizes, total
        assert len(page_sizes) <= 64, "nextToken chain did not terminate"


# Feature: portal-build-fleet-and-workflow-gates, Property 16: Build history ordering and content
# Validates: Requirements 4.7
@settings(max_examples=100, deadline=None)
@given(specs=_job_specs(), limit=st.integers(min_value=1, max_value=5))
def test_build_history_ordering_and_content(specs, limit):
    """For any set of Build_Jobs with random creation times and statuses,
    GET /builds lists exactly the 90-day window most recent first
    (created_at desc, build_job_id tie-break), pages exactly by the
    nextToken, and carries every succeeded job's published artifact
    identifiers (Req 4.7)."""
    _clear_table()
    now_ms = int(time.time() * 1000)
    jobs = _put_jobs(specs, now_ms)

    entries, page_sizes, total = _walk_history(limit)

    expected = sorted(
        (job for job in jobs.values()
         if job["created_at"] >= now_ms - 90 * _DAY_MS),
        key=lambda job: (job["created_at"], job["build_job_id"]),
        reverse=True)
    expected_ids = [job["build_job_id"] for job in expected]

    # Exactly the 90-day window, most recent first with the deterministic
    # tie-break — pages concatenated are complete, disjoint, and ordered.
    assert [entry["build_job_id"] for entry in entries] == expected_ids

    # The reported total is the full history size, and pagination is
    # exact: every page except the last holds exactly `limit` entries.
    assert total == len(expected_ids)
    for size in page_sizes[:-1]:
        assert size == limit
    if page_sizes:
        assert 0 <= page_sizes[-1] <= limit

    # Every succeeded job's entry carries its published artifact
    # identifiers verbatim; and entries reflect the stored jobs.
    by_id = {entry["build_job_id"]: entry for entry in entries}
    for job in expected:
        entry = by_id[job["build_job_id"]]
        assert entry["status"] == job["status"]
        assert entry["created_at"] == job["created_at"]
        if job["status"] == build_domain.STATUS_SUCCEEDED:
            assert entry.get("result") == job["result"], (
                "succeeded job entry is missing its published artifact "
                "identifiers")
