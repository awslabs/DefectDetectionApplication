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
Property test for Source_Selection snapshot fidelity
(build-source-selection, task 9).

**Property 9: Expected Behavior** — Source_Selection snapshot fidelity

**Validates: Requirements 1.2, 1.3, 1.4, 1.6, 2.4, 2.5, 2.7**

For any submission x effective configuration:

- an ACCEPTED submission creates one queued Build_Job per target whose
  ``config_snapshot`` carries the resolved source exactly:
  ``repository`` is the normalized submitted URL when one was supplied
  (Req 1.3, 1.6) and the configured ``default_repository`` when omitted
  (Req 1.2, the zero-effort path); ``source_ref`` is the submitted ref
  when one was supplied (Req 2.5) and the configured value when omitted
  (Req 2.4, ``None`` meaning the repository's default branch);
- tags and 40-hex commit SHAs survive into the snapshot UNCHANGED
  (Req 2.7);
- the fallback default comes from the ``build_infrastructure_config``
  setting (the stored operator value), not a constant;
- the ``build_requested`` audit details carry the resolved ``repository``
  and ``source_ref``;
- a REJECTED submission (invalid repository or invalid ref) returns the
  existing BUILD_REQUEST_INVALID envelope with each error naming the
  offending field, creates ZERO Build_Jobs, and records ZERO
  build_requested audit entries (Req 1.4).

The full real path runs: ``build_jobs.handler`` -> rbac_middleware ->
``build_domain.validate_build_request`` -> ``build_domain
.create_build_jobs`` -> the moto-mocked BuildJobs table (deployed GSI
schema included). ``invoke_dispatcher`` is a no-op; nothing here launches
compute, sends SSM commands, or calls real AWS.
"""
import json
import os
import sys
import types
import uuid
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import: the handlers bind their boto3 handles and
# table names at import time (sibling pattern:
# test_source_selection_preservation.py).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "source-snapshot"
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

# Import boto3 from the test environment BEFORE the Lambda layer directory
# joins sys.path (the layer vendors its own urllib3 build).
import boto3  # noqa: E402

# Some verification containers ship a python build without the _bz2 C
# extension while moto's request path imports moto.s3 -> bz2 (sibling shim).
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
_LAYER_DIR = os.path.join(_BACKEND, "layers", "shared", "python")
_FUNCTIONS_DIR = os.path.join(_BACKEND, "functions")
for _p in (_LAYER_DIR, _FUNCTIONS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fresh modules so the module-level boto3 handles are created under the
# moto mock started below, bound to this file's table names.
for _module in ("build_jobs", "build_domain", "build_source",
                "rbac_middleware", "shared_utils"):
    sys.modules.pop(_module, None)

_MOCK = mock_aws()
_MOCK.start()

_DDB = boto3.resource("dynamodb", region_name="us-east-1")

# BuildJobs with the DEPLOYED schema, GSIs included, so persisting a job
# exercises the real index-key constraints.
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
_SETTINGS = _DDB.Table(_SETTINGS_TABLE)
_AUDIT = _DDB.Table(_AUDIT_TABLE)

import build_domain  # noqa: E402
import build_jobs  # noqa: E402
import build_source  # noqa: E402


# ---------------------------------------------------------------------------
# Expected semantics, restated independently of the implementation
# ---------------------------------------------------------------------------

TARGETS = (build_domain.TARGET_JP5, build_domain.TARGET_JP6,
           build_domain.TARGET_AMD64, build_domain.TARGET_AMD64_NVIDIA)

#: The setting the fallback default MUST come from (Req 1.2/1.5), and the
#: documented default when nothing is stored.
SETTING_KEY = "build_infrastructure_config"
DOCUMENTED_DEFAULT_REPOSITORY = \
    "https://github.com/awslabs/DefectDetectionApplication"


def _expected_effective_config(stored):
    """The documented defaults with stored non-None values merged over
    them — the read semantics jobs snapshot (restated oracle)."""
    config = dict(build_domain.DEFAULT_BUILD_CONFIG)
    for key, value in (stored or {}).items():
        if key in config and value is not None:
            config[key] = value
    return config


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: GitHub owner/repo segments. The repo alphabet deliberately avoids '.'
#: so the plain form IS the normalized form (no '.git'-stripping ambiguity)
#: and the expected snapshot value can be computed independently.
_owners = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9-]{0,10}", fullmatch=True)
_repos = st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,15}", fullmatch=True)


@st.composite
def _valid_repository(draw):
    """(submitted_value, expected_normalized): the plain form plus its
    '.git' / trailing-slash variants, all normalizing to the plain form."""
    owner = draw(_owners)
    repo = draw(_repos)
    plain = f"https://github.com/{owner}/{repo}"
    submitted = draw(st.sampled_from(
        [plain, f"{plain}.git", f"{plain}/", f"  {plain} "]))
    return submitted, plain


#: Branch names (letters/digits/_/- components joined by '/'): no leading
#: '-', no '..', no whitespace, no component starting with '.'.
_branches = st.from_regex(
    r"[A-Za-z][A-Za-z0-9_-]{0,8}(/[A-Za-z0-9_-]{1,8}){0,2}", fullmatch=True)
#: Tags (Req 2.7: non-branch refs are accepted).
_tags = st.from_regex(r"v[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}", fullmatch=True)
#: 40-hex commit SHAs (Req 2.7), accepted verbatim.
_shas = st.text(alphabet="0123456789abcdefABCDEF", min_size=40, max_size=40)

_valid_refs = st.one_of(_branches, _tags, _shas)

#: Repositories Req 1.4 must reject (each rejection names the field).
_INVALID_REPOSITORIES = (
    "",                                          # empty
    "http://github.com/owner/repo",              # not https
    "git@github.com:owner/repo.git",             # scp-style remote
    "https://gitlab.com/owner/repo",             # non-allowlisted host
    "https://github.com/owner",                  # missing repository
    "https://github.com/owner/repo/tree/main",   # extra path segments
    "https://user@github.com/owner/repo",        # userinfo
    "https://github.com:8443/owner/repo",        # port
    "https://github.com/owner/repo?ref=main",    # query
    "https://github.com/owner/repo#frag",        # fragment
    "not a url",
    42,                                          # non-string JSON value
)

#: Refs the validator must reject (Req 2.7 accepts branches/tags/SHAs; these
#: are the git-invalid / shell-hostile forms).
_INVALID_REFS = (
    "-option",        # leading '-'
    "a..b",           # '..'
    "has space",      # whitespace
    "a\tb",           # control/whitespace
    "/leading",       # leading '/'
    "trailing/",      # trailing '/'
    "a//b",           # empty component
    "ref~1",          # outside the accepted character set
    "ref^",
    "ref:name",
    ".hidden",        # component starting with '.'
    "name.lock",      # '.lock' suffix
    7,                # non-string JSON value
)


@st.composite
def _submission_case(draw):
    """One submission x configuration case over the full Property 9 domain:
    repository omitted/valid/invalid x ref omitted/blank/valid/invalid x a
    stored configuration with and without operator overrides."""
    targets = draw(st.lists(st.sampled_from(TARGETS), min_size=1, max_size=3))

    # Stored operator configuration (the build_infrastructure_config
    # setting): sometimes absent entirely, sometimes carrying a configured
    # default repository (an operator fork) and/or a configured global ref.
    stored = draw(st.one_of(
        st.none(),
        st.fixed_dictionaries({}, optional={
            "default_repository": st.sampled_from([
                "https://github.com/operator/fork",
                "https://github.com/awslabs/DefectDetectionApplication",
            ]),
            "source_ref": st.sampled_from(
                ["develop", "v9.9.9", "c" * 40]),
            "volume_size_gb": st.integers(min_value=20, max_value=500),
        }),
    ))

    repo_kind = draw(st.sampled_from(["omitted", "valid", "invalid"]))
    if repo_kind == "valid":
        submitted_repository, normalized_repository = \
            draw(_valid_repository())
    elif repo_kind == "invalid":
        submitted_repository = draw(st.sampled_from(_INVALID_REPOSITORIES))
        normalized_repository = None
    else:
        submitted_repository = None
        normalized_repository = None

    ref_kind = draw(st.sampled_from(
        ["omitted", "blank", "valid", "invalid"]))
    if ref_kind == "valid":
        submitted_ref = draw(_valid_refs)
    elif ref_kind == "blank":
        submitted_ref = draw(st.sampled_from(["", "   "]))
    elif ref_kind == "invalid":
        submitted_ref = draw(st.sampled_from(_INVALID_REFS))
    else:
        submitted_ref = None

    return (targets, stored, repo_kind, submitted_repository,
            normalized_repository, ref_kind, submitted_ref)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER = {"user_id": "prop9-user", "email": "prop9@example.com",
         "username": "prop9-user", "role": "PortalAdmin"}


def _event(body):
    return {
        "resource": "/builds",
        "httpMethod": "POST",
        "path": "/builds",
        "pathParameters": None,
        "queryStringParameters": None,
        "body": json.dumps(body),
        "requestContext": {
            "requestId": str(uuid.uuid4()),
            "authorizer": {
                "claims": {
                    "sub": _USER["user_id"],
                    "email": _USER["email"],
                    "cognito:username": _USER["username"],
                    "custom:role": _USER["role"],
                }
            },
        },
    }


def _scan_jobs():
    items, kwargs = [], {}
    while True:
        page = _JOBS.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _audit_records():
    return _AUDIT.scan().get("Items", [])


def _reset_stores(stored_config):
    for item in _scan_jobs():
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _audit_records():
        _AUDIT.delete_item(Key={"event_id": item["event_id"],
                                "timestamp": item["timestamp"]})
    _SETTINGS.delete_item(Key={"setting_key": SETTING_KEY})
    if stored_config is not None:
        _SETTINGS.put_item(Item={"setting_key": SETTING_KEY,
                                 "value": stored_config})


# Feature: build-source-selection, Property 9: Source_Selection snapshot fidelity
# **Validates: Requirements 1.2, 1.3, 1.4, 1.6, 2.4, 2.5, 2.7**
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(case=_submission_case())
def test_source_selection_snapshot_fidelity(case):
    """For any submission x configuration: every created Build_Job's
    config_snapshot carries the resolved repository and source_ref exactly
    (submitted values when supplied, configured values when omitted, tags
    and 40-hex SHAs unchanged, the fallback read from the
    build_infrastructure_config setting), the build_requested audit details
    carry both, and a rejected submission names the offending field and
    creates zero Build_Jobs."""
    (targets, stored, repo_kind, submitted_repository,
     normalized_repository, ref_kind, submitted_ref) = case

    _reset_stores(stored)

    body = {"targets": targets, "execution_mode": "ephemeral"}
    if repo_kind != "omitted":
        body["repository"] = submitted_repository
    if ref_kind != "omitted":
        body["source_ref"] = submitted_ref

    # The audit assertions record the handler's log_audit_event calls
    # directly: the shared helper keys audit items on a millisecond
    # timestamp, so two jobs audited in the same millisecond would
    # overwrite each other in a table scan.
    audit_calls = []

    def _record_audit(**kwargs):
        audit_calls.append(kwargs)

    with mock.patch.object(build_jobs, "invoke_dispatcher",
                           side_effect=lambda ids: None), \
            mock.patch.object(build_jobs, "log_audit_event",
                              side_effect=_record_audit):
        response = build_jobs.handler(_event(body), None)
    payload = json.loads(response["body"])

    effective = _expected_effective_config(stored)
    should_reject = repo_kind == "invalid" or ref_kind == "invalid"

    if should_reject:
        # Req 1.4: the standard validation envelope names the offending
        # field, and NO Build_Job is created.
        assert response["statusCode"] == 400, payload
        assert payload["error"]["code"] == "BUILD_REQUEST_INVALID", payload
        errors = payload["error"]["details"]["errors"]
        fields = {e.get("field") for e in errors}
        if repo_kind == "invalid":
            assert any(
                e.get("rule") == build_domain.RULE_REPOSITORY_INVALID
                and e.get("field") == "repository" for e in errors), errors
        if ref_kind == "invalid":
            assert any(
                e.get("rule") == build_domain.RULE_SOURCE_REF_INVALID
                and e.get("field") == "source_ref" for e in errors), errors
        assert fields <= {"repository", "source_ref"}, errors
        assert _scan_jobs() == [], (
            "a rejected submission persisted Build_Jobs")
        assert not any(c.get("action") == "build_requested"
                       for c in audit_calls), (
            "a rejected submission recorded build_requested audit entries")
        return

    # Accepted: one queued Build_Job per target (Req 1.2 zero-effort path
    # included), each snapshotting the RESOLVED source.
    assert response["statusCode"] == 201, payload
    jobs = payload["jobs"]
    assert len(jobs) == len(targets), payload

    if repo_kind == "valid":
        expected_repository = normalized_repository      # Req 1.3, 1.6
    else:
        # Omitted: the configured default from the settings item, i.e.
        # the operator-controlled value, not a constant (Req 1.2).
        expected_repository = effective["default_repository"]
    if ref_kind == "valid":
        expected_source_ref = submitted_ref              # Req 2.5, 2.7
    else:
        # Omitted or blank: the configured value; None means the
        # repository's default branch (Req 2.4).
        expected_source_ref = effective["source_ref"]

    stored_items = {item["build_job_id"]: item for item in _scan_jobs()}
    audit_by_job = {
        c["resource_id"]: c for c in audit_calls
        if c.get("action") == "build_requested"}

    for job in jobs:
        for record_name, snapshot in (
                ("response", job["config_snapshot"]),
                ("stored",
                 stored_items[job["build_job_id"]]["config_snapshot"])):
            observed = (record_name, snapshot, stored, body)
            assert snapshot["repository"] == expected_repository, observed
            assert snapshot["source_ref"] == expected_source_ref, observed
        # Additive keys only: the pre-existing snapshot keys still carry
        # the effective configuration values.
        assert job["config_snapshot"]["volume_size_gb"] == \
            effective["volume_size_gb"]
        # The build_requested audit details carry the resolved pair.
        audit = audit_by_job.get(job["build_job_id"])
        assert audit is not None, (
            f"no build_requested audit entry for {job['build_job_id']}")
        details = audit["details"]
        assert details["repository"] == expected_repository, details
        assert details["source_ref"] == expected_source_ref, details
