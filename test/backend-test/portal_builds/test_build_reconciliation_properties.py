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
Diagnostic / classification / reconciliation property tests
(build-fleet-execution-failures task 10.3).

**Property 3: Diagnostic Redaction and Bounds** — arbitrary diagnostic
payloads remain safe and bounded.
**Property 4: Deterministic Classification and Ordering** —
permutations and duplicates converge.
**Property 5: Event and Scheduled Reconciliation Convergence** —
missing events affect latency only.

**Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.6, 2.10, 2.11**

Properties 3 and 4 are PURE (``build_reconciliation.py``): generators
produce arbitrary Unicode/size payloads carrying nested secret canaries
(AWS keys, bearer/basic authorization, password/token/credential
assignments, repository userinfo credentials, signed-URL parameters,
configured organization patterns), field missingness, all SSM statuses,
callback/lifecycle evidence, duplicates, and delivery permutations.

Property 5 uses the moto + recording-SSM-fake integration pattern from
``test_command_event_reconciliation.py`` /
``test_dispatcher_command_reconciliation.py``: EventBridge terminal
notifications are delivered zero, one, or multiple times (optionally
before the invocation is visible — ``InvocationDoesNotExist`` eventual
consistency) and the scheduled dispatcher tick must converge to the
same precedence-defined terminal outcome/diagnostic; genuinely
nonterminal invocations stay nonterminal. No live AWS is touched.

Run ONLY this file, from the repository root:

    PYTHONPATH=src/backend:test/backend-test python3 -m pytest \\
        test/backend-test/portal_builds/test_build_reconciliation_properties.py \\
        --noconftest -q

(This run contains property-based tests and may generate/shrink
counterexamples.)
"""
import json
import os
import sys
import types
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st

# ---------------------------------------------------------------------------
# Environment BEFORE any import (the handlers bind boto3 at import time).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

_SUFFIX = "reconcile-properties"
_JOBS_TABLE = f"dda-portal-build-jobs-{_SUFFIX}"
_SERVERS_TABLE = f"dda-portal-build-servers-{_SUFFIX}"
os.environ["BUILD_JOBS_TABLE"] = _JOBS_TABLE
os.environ["BUILD_SERVERS_TABLE"] = _SERVERS_TABLE
os.environ.pop("BUILD_REPO_URL", None)
os.environ.pop("BUILD_ALERT_TOPIC_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_ARN", None)
os.environ.pop("BUILD_INSTANCE_PROFILE_NAME", None)
os.environ.pop("BUILD_SECURITY_GROUP_ID", None)
os.environ.pop("BUILD_SUBNET_ID", None)

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

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
# Fake shared_utils capturing Audit_Log entries (a canary sink).
# ---------------------------------------------------------------------------
AUDIT_EVENTS = []


def _fake_shared_utils():
    module = types.ModuleType("shared_utils")

    def log_audit_event(**kwargs):
        AUDIT_EVENTS.append(kwargs)

    module.log_audit_event = log_audit_event
    return module


for _module in ("build_events", "build_dispatcher", "build_planner",
                "build_domain", "build_reconciliation", "build_source",
                "shared_utils"):
    sys.modules.pop(_module, None)
sys.modules["shared_utils"] = _fake_shared_utils()

_MOCK = mock_aws()
_MOCK.start()

# ---------------------------------------------------------------------------
# Recording fake SSM installed over boto3.client BEFORE the handler
# imports: scripted final GetCommandInvocation evidence + retrieval
# observability. Unknown operations delegate to moto (never real AWS).
# ---------------------------------------------------------------------------
SSM_INVOCATIONS = {}
SSM_GET_CALLS = []
SSM_SEND_CALLS = []

_REAL_BOTO3_CLIENT = boto3.client


class _FakeSsm:
    def __init__(self, inner):
        self._inner = inner

    def get_command_invocation(self, **kwargs):
        SSM_GET_CALLS.append(dict(kwargs))
        invocation = SSM_INVOCATIONS.get(kwargs.get("CommandId"))
        if invocation is not None:
            return dict(invocation)
        raise ClientError(
            {"Error": {"Code": "InvocationDoesNotExist",
                       "Message": "no such invocation"}},
            "GetCommandInvocation")

    def list_commands(self, **kwargs):
        return {"Commands": []}

    def send_command(self, **kwargs):
        SSM_SEND_CALLS.append(dict(kwargs))
        return self._inner.send_command(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _intercepting_client(service_name, *args, **kwargs):
    inner = _REAL_BOTO3_CLIENT(service_name, *args, **kwargs)
    if service_name == "ssm":
        return _FakeSsm(inner)
    return inner


boto3.client = _intercepting_client

_DDB = boto3.resource("dynamodb", region_name="us-east-1")
for _name, _key in ((_JOBS_TABLE, "build_job_id"),
                    (_SERVERS_TABLE, "server_id")):
    _DDB.create_table(
        TableName=_name,
        KeySchema=[{"AttributeName": _key, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": _key, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
_JOBS = _DDB.Table(_JOBS_TABLE)
_SERVERS = _DDB.Table(_SERVERS_TABLE)

_EC2 = boto3.client("ec2", region_name="us-east-1")


def _default_ami_id():
    images = _EC2.describe_images(Owners=["amazon"]).get("Images", [])
    if images:
        return images[0]["ImageId"]
    return _EC2.register_image(  # pragma: no cover
        Name="dda-test-ami", RootDeviceName="/dev/sda1",
        VirtualizationType="hvm")["ImageId"]


_AMI_ID = _default_ami_id()
os.environ["BUILD_ARM64_AMI_ID"] = _AMI_ID
os.environ["BUILD_X86_64_AMI_ID"] = _AMI_ID

import build_domain  # noqa: E402
import build_reconciliation as br  # noqa: E402
import build_events  # noqa: E402
import build_dispatcher  # noqa: E402

# The handlers above captured their (fake-wrapped) clients at import;
# restore the real factory so OTHER test modules collected in the same
# pytest process get untouched moto clients.
boto3.client = _REAL_BOTO3_CLIENT

_MINUTE_MS = 60 * 1000


# ===========================================================================
# Shared generators — secret canaries and arbitrary Unicode payloads
# ===========================================================================

#: Configured organization pattern (Req 2.10 "configured organization
#: patterns") the tests hand to redaction as ``extra_patterns``.
ORG_PATTERN = r"ORG-INTERNAL-[0-9a-f]{12}"

_HEX = "0123456789abcdef"

#: Arbitrary Unicode noise. It must never contain a canary core by
#: accident, so canary substring assertions stay sound.
noise_text = st.text(max_size=200).filter(
    lambda s: "CANARY" not in s and "ORG-INTERNAL-" not in s)

_ASSIGNMENT_KEYS = (
    "PASSWORD", "DB_PASSWORD", "passwd", "pwd",
    "GIT_TOKEN", "api_token", "SECRET_TOKEN", "npm_token",
    "AWS_SECRET_ACCESS_KEY", "aws_session_token",
    "api_key", "ACCESS_KEY", "repo_credential", "client_secret",
)
_ASSIGNMENT_SEPS = ("=", ": ", " = ", " ")


@st.composite
def secret_line(draw):
    """One self-contained secret-bearing line plus the canary value(s)
    that MUST NOT survive redaction (Req 2.10): AWS keys, bearer/basic
    authorization, password/token/credential assignments, repository
    userinfo credentials, signed-URL parameters, org patterns."""
    value = "CANARY" + draw(st.text(alphabet=_HEX, min_size=12,
                                    max_size=20))
    kind = draw(st.sampled_from(
        ["aws_key", "bearer", "basic", "assignment", "repo_userinfo",
         "signed_url", "org_pattern"]))
    if kind == "aws_key":
        canary = "AKIA" + draw(st.text(
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
            min_size=16, max_size=16))
        return "using credential " + canary, [canary]
    if kind == "bearer":
        return "Authorization: Bearer " + value, [value]
    if kind == "basic":
        return "authorization: Basic " + value, [value]
    if kind == "assignment":
        key = draw(st.sampled_from(_ASSIGNMENT_KEYS))
        sep = draw(st.sampled_from(_ASSIGNMENT_SEPS))
        return key + sep + value, [value]
    if kind == "repo_userinfo":
        return ("fatal: could not read from "
                "https://builder:" + value +
                "@github.com/example-org/private-repo.git"), [value]
    if kind == "signed_url":
        second = "CANARY" + draw(st.text(alphabet=_HEX, min_size=12,
                                         max_size=20))
        return ("GET https://bucket.s3.amazonaws.com/artifact.tgz"
                "?X-Amz-Credential=" + value +
                "&X-Amz-Signature=" + second), [value, second]
    canary = "ORG-INTERNAL-" + draw(st.text(alphabet=_HEX, min_size=12,
                                            max_size=12))
    return "artifact tag " + canary, [canary]


@st.composite
def secret_payload(draw):
    """Arbitrary-Unicode, arbitrary-size text embedding one or more
    newline-delimited secret lines (as real agent/provider output
    delimits them) plus optional bulk padding that forces the byte
    bounds to truncate. Returns ``(text, canaries)``."""
    parts = []
    canaries = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        parts.append(draw(noise_text))
        line, values = draw(secret_line())
        parts.append(line)
        canaries.extend(values)
    parts.append(draw(noise_text))
    pad = draw(st.integers(min_value=0, max_value=40_000))
    text = "\n".join(parts) + "\n" + ("x" * pad)
    return text, canaries


#: Field-missingness flavors for provider fields (Req 2.2): the
#: provider omitted the field entirely, returned it empty, or returned
#: real content.
field_flavor = st.sampled_from(["missing", "empty", "payload"])


# ===========================================================================
# Property 3: Diagnostic Redaction and Bounds
# ===========================================================================

class TestProperty3DiagnosticRedactionAndBounds:
    """**Property 3: Diagnostic Redaction and Bounds**

    **Validates: Requirements 2.2, 2.10, 2.11**
    """

    @settings(max_examples=120, deadline=None,
              suppress_health_check=[HealthCheck.filter_too_much,
                                     HealthCheck.data_too_large])
    @given(data=st.data())
    def test_diagnostic_is_valid_bounded_redacted_and_truthful(
            self, data):
        """For any invocation payload with secret-shaped values,
        arbitrary Unicode/size, and field missingness, the built
        ``execution_diagnostic`` is valid UTF-8/JSON, fits the 16 KiB
        stream / 4 KiB detail / 48 KiB total byte limits, marks
        unavailable/empty/truncated fields truthfully, and carries no
        canary in any captured sink (Req 2.2, 2.10, 2.11)."""
        canaries = []
        invocation = {}

        def _draw_field(key, flavor):
            if flavor == "missing":
                return
            if flavor == "empty":
                invocation[key] = ""
                return
            text, values = data.draw(secret_payload(), label=key)
            invocation[key] = text
            canaries.extend(values)

        _draw_field("StandardOutputContent",
                    data.draw(field_flavor, label="stdout_flavor"))
        _draw_field("StandardErrorContent",
                    data.draw(field_flavor, label="stderr_flavor"))
        _draw_field("StatusDetails",
                    data.draw(field_flavor, label="details_flavor"))
        if data.draw(st.booleans(), label="has_status"):
            invocation["Status"] = data.draw(
                st.sampled_from(sorted(br.SSM_TERMINAL_STATUSES)
                                + ["InProgress", "Pending"]),
                label="status")
        if data.draw(st.booleans(), label="has_response_code"):
            invocation["ResponseCode"] = data.draw(
                st.integers(min_value=-1, max_value=255), label="rc")
        if data.draw(st.booleans(), label="has_dates"):
            invocation["ExecutionStartDateTime"] = "2026-08-06T17:02:55Z"
            invocation["ExecutionEndDateTime"] = "2026-08-06T17:03:03Z"

        diagnostic = br.build_execution_diagnostic(
            attempt={"attempt_id": "attempt-1", "command_id": "cmd-1",
                     "instance_id": "i-1"},
            invocation=invocation or None,
            classification=br.CODE_COMMAND_EXECUTION_FAILED,
            source="event_bridge",
            observed_at=1_786_017_773_000,
            extra_patterns=[ORG_PATTERN])

        # Valid UTF-8/JSON (Req 2.2).
        serialized = json.dumps(diagnostic, sort_keys=True, default=str)
        serialized.encode("utf-8")
        json.loads(serialized)

        # Total serialized bound (Req 2.2: 48 KiB total).
        assert br.diagnostic_json_bytes(diagnostic) <= \
            br.TOTAL_DIAGNOSTIC_LIMIT_BYTES

        # Stream fields: 16 KiB bound plus truthful availability and
        # truncation markers (Req 2.2).
        for key, source_key in (("stdout", "StandardOutputContent"),
                                ("stderr", "StandardErrorContent")):
            field = diagnostic[key]
            if source_key not in invocation:
                assert field == {"available": False}, \
                    f"{key}: an absent provider field must be marked " \
                    f"unavailable, never fabricated"
                continue
            assert field["available"] is True
            text = field["text"]
            assert len(text.encode("utf-8")) <= \
                br.STDOUT_STDERR_LIMIT_BYTES
            if invocation[source_key] == "":
                assert field == {"available": True, "text": "",
                                 "truncated": False, "original_bytes": 0}
            if field["truncated"]:
                # Truthful: content really was removed, and the marker
                # carries a byte count.
                assert field["original_bytes"] > \
                    len(text.encode("utf-8"))
                assert "[TRUNCATED:" in text
            else:
                assert field["original_bytes"] == \
                    len(text.encode("utf-8"))

        # Detail fields: 4 KiB bound; absent provider fields are None
        # (never fabricated).
        for key, source_key in (("status", "Status"),
                                ("status_details", "StatusDetails"),
                                ("execution_start",
                                 "ExecutionStartDateTime"),
                                ("execution_end",
                                 "ExecutionEndDateTime")):
            value = diagnostic[key]
            if source_key not in invocation:
                assert value is None
            else:
                assert isinstance(value, str)
                assert len(value.encode("utf-8")) <= \
                    br.DETAIL_FIELD_LIMIT_BYTES
        if "ResponseCode" not in invocation:
            assert diagnostic["response_code"] is None
        else:
            assert diagnostic["response_code"] == \
                invocation["ResponseCode"]

        # No measurement was taken: the disk block is truthfully
        # unavailable, not fabricated (Req 2.2).
        assert diagnostic["disk"] == {"available": False}

        # Canary absence from the whole captured sink (Req 2.10).
        for canary in canaries:
            assert canary not in serialized, \
                f"secret canary leaked into the diagnostic: {canary!r}"

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.filter_too_much,
                                     HealthCheck.data_too_large])
    @given(data=st.data())
    def test_nested_evidence_trees_are_redacted_and_json_safe(
            self, data):
        """For any NESTED scalar/list/map evidence carrying secret
        lines at arbitrary depth, ``sanitize_evidence_tree`` keeps the
        structure JSON-safe and omits every canary (Req 2.2, 2.10)."""
        canaries = []

        def _leaf():
            flavor = data.draw(st.sampled_from(
                ["noise", "secret", "int", "bytes", "none"]),
                label="leaf_flavor")
            if flavor == "noise":
                return data.draw(noise_text, label="leaf_noise")
            if flavor == "secret":
                line, values = data.draw(secret_line(),
                                         label="leaf_secret")
                canaries.extend(values)
                prefix = data.draw(noise_text, label="leaf_prefix")
                return prefix + "\n" + line
            if flavor == "int":
                return data.draw(st.integers(), label="leaf_int")
            if flavor == "bytes":
                return b"\xff\xfe raw provider bytes"
            return None

        def _tree(depth):
            if depth <= 0:
                return _leaf()
            shape = data.draw(st.sampled_from(["dict", "list", "leaf"]),
                              label=f"shape_{depth}")
            if shape == "dict":
                return {
                    data.draw(st.sampled_from(
                        ["env", "output", "detail", "context", "nested"]),
                        label=f"key_{depth}_{i}") + str(i):
                    _tree(depth - 1)
                    for i in range(data.draw(
                        st.integers(min_value=1, max_value=3),
                        label=f"width_{depth}"))
                }
            if shape == "list":
                return [_tree(depth - 1) for _ in range(data.draw(
                    st.integers(min_value=1, max_value=3),
                    label=f"len_{depth}"))]
            return _leaf()

        raw = _tree(data.draw(st.integers(min_value=1, max_value=3),
                              label="depth"))
        # Ensure at least one secret is present in the tree.
        line, values = data.draw(secret_line(), label="guaranteed")
        canaries.extend(values)
        raw = {"payload": raw, "trailer": "context\n" + line}

        sanitized = br.sanitize_evidence_tree(
            raw, extra_patterns=[ORG_PATTERN])

        serialized = json.dumps(sanitized, sort_keys=True)
        serialized.encode("utf-8")
        for canary in canaries:
            assert canary not in serialized, \
                f"secret canary leaked from the nested tree: {canary!r}"

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.filter_too_much,
                                     HealthCheck.data_too_large])
    @given(data=st.data(),
           secret_key=st.sampled_from(
               ["password", "PASSWORD", "secret", "aws_secret_access_key",
                "api_key", "repo_token", "authorization", "session_key",
                "db_credential", "client_secret"]),
           shape=st.sampled_from(["scalar", "list", "subtree"]),
           wrap=st.booleans())
    def test_secret_shaped_map_keys_are_redacted(self, data, secret_key,
                                                 shape, wrap):
        """A value assigned to a secret-shaped map KEY is a secret by
        definition ("password/token/secret assignments", Req 2.10):
        even a BARE canary value carried under such a key — as a
        scalar, inside a list, or inside a whole subtree — must not
        survive ``sanitize_evidence_tree`` into any sink, while the key
        name itself is retained and safe sibling values are untouched.

        Found by this property on the initial 10.3 run (shrunk
        counterexample: ``{"nested": {"password": "<canary>"}}``
        survived sanitization verbatim) and fixed additively in
        ``build_reconciliation.sanitize_evidence_tree``."""
        canary = "CANARY" + data.draw(
            st.text(alphabet=_HEX, min_size=12, max_size=20),
            label="canary")
        if shape == "scalar":
            value = canary
        elif shape == "list":
            value = [canary, data.draw(noise_text, label="extra")]
        else:
            value = {"inner": canary}
        sibling = data.draw(noise_text, label="sibling")
        tree = {"detail": sibling, "nested": {secret_key: value}}
        if wrap:
            tree = {"wrapper": [tree]}

        sanitized = br.sanitize_evidence_tree(tree)

        serialized = json.dumps(sanitized, sort_keys=True)
        serialized.encode("utf-8")
        assert canary not in serialized, \
            f"bare secret value under key {secret_key!r} leaked"
        assert secret_key in serialized, \
            "the key name itself must be retained"
        assert br.REDACTED in serialized

    @settings(max_examples=150, deadline=None,
              suppress_health_check=[HealthCheck.data_too_large])
    @given(payload=secret_payload(),
           limit=st.sampled_from([64, 256, 1024,
                                  br.DETAIL_FIELD_LIMIT_BYTES,
                                  br.STDOUT_STDERR_LIMIT_BYTES]))
    def test_bound_text_is_truthful_at_every_limit(self, payload, limit):
        """Byte bounding never exceeds its limit, stays valid UTF-8,
        and reports truncation truthfully (marker + original byte
        count) — content is removed, never fabricated (Req 2.2)."""
        text, _ = payload
        bounded = br.bound_text(text, limit)
        raw = bounded.text.encode("utf-8")
        assert len(raw) <= limit
        assert bounded.original_bytes == len(text.encode("utf-8"))
        if bounded.truncated:
            assert bounded.original_bytes > limit
        else:
            assert bounded.text == text


# ===========================================================================
# Property 4: Deterministic Classification and Ordering
# ===========================================================================

#: Fixed, oracle-checkable invocation text flavors. (Arbitrary Unicode
#: content is Property 3's business; here texts must stay exactly
#: classifiable so the design-table oracle is exact.)
_STDERR_FLAVORS = {
    "plain": "bash: line 12: gdk: command exited 1",
    "enospc": ("failed to register layer: write /var/snap/docker/"
               "common/x: no space left on device"),
    "preflight": br.PREFLIGHT_FAILURE_MARKER + ": repository missing",
}

_NOW = 1_786_017_773_000

_AGENT_MESSAGES = {
    "plain": "component build failed during publish",
    "enospc": "gdk component build failed: no space left on device",
}


@st.composite
def classification_evidence(draw):
    """One correlated, settled evidence set spanning agent callbacks,
    all SSM statuses, cancellation, lifecycle loss, launch rejection,
    hard-deadline and settlement clocks (Req 2.4, 2.6)."""
    evidence = {
        "current_status": draw(st.sampled_from(
            [build_domain.STATUS_BUILDING,
             build_domain.STATUS_PUBLISHING])),
        "now": _NOW,
        "user_cancellation_confirmed": draw(st.booleans()),
        "infrastructure_lost": draw(st.booleans()),
        "send_command_rejected": draw(st.booleans()),
    }

    hard = draw(st.sampled_from(["none", "future", "boundary", "past"]))
    evidence["hard_deadline_ms"] = {
        "none": None, "future": _NOW + 60_000,
        "boundary": _NOW, "past": _NOW - 60_000,
    }[hard]

    settle = draw(st.sampled_from(["none", "future", "boundary", "past"]))
    evidence["settlement_deadline_ms"] = {
        "none": None, "future": _NOW + 30_000,
        "boundary": _NOW, "past": _NOW - 30_000,
    }[settle]

    if draw(st.booleans()):
        phase = draw(st.sampled_from(["succeeded", "failed"]))
        flavor = draw(st.sampled_from(["plain", "enospc"]))
        agent = {"phase": phase, "message": _AGENT_MESSAGES[flavor]}
        completed = draw(st.sampled_from(
            ["none", "before", "at", "after"]))
        deadline = evidence["hard_deadline_ms"]
        if completed != "none" and deadline is not None:
            agent["completed_at"] = {
                "before": deadline - 1_000, "at": deadline,
                "after": deadline + 1_000}[completed]
        elif completed != "none":
            agent["completed_at"] = _NOW - 5_000
        if draw(st.booleans()):
            agent["error_kind"] = draw(st.sampled_from(
                ["build", br.AGENT_ERROR_KIND_DISK]))
        evidence["agent_result"] = agent
    else:
        evidence["agent_result"] = None

    inv_status = draw(st.sampled_from(
        ["absent", "Pending", "InProgress", "Delayed", "Cancelling",
         "Success", "Failed", "TimedOut", "Cancelled"]))
    if inv_status == "absent":
        evidence["invocation"] = None
    else:
        evidence["invocation"] = {
            "Status": inv_status,
            "ResponseCode": draw(st.sampled_from([0, 1, 127])),
            "StatusDetails": inv_status,
            "StandardErrorContent": _STDERR_FLAVORS[
                draw(st.sampled_from(sorted(_STDERR_FLAVORS)))],
        }
    return evidence


def _design_table_oracle(evidence):
    """The design's "Evidence Precedence and Classification" list,
    written independently from the spec (NOT from the implementation):
    returns ``(decided, status, error_code)``."""
    now = evidence["now"]
    hard = evidence["hard_deadline_ms"]
    agent = evidence["agent_result"]

    # 1. Valid correlated agent terminal result at/before an
    #    already-decided hard deadline.
    if agent is not None:
        completed = agent.get("completed_at")
        if hard is None or completed is None or completed <= hard:
            if agent["phase"] == "succeeded":
                return (True, build_domain.STATUS_SUCCEEDED, None)
            disk = (agent.get("error_kind") == br.AGENT_ERROR_KIND_DISK
                    or "no space left on device" in agent["message"])
            return (True, build_domain.STATUS_FAILED,
                    br.CODE_RUNNER_DISK_FULL if disk else None)

    # 2. Explicit user cancellation with confirmed stop.
    if evidence["user_cancellation_confirmed"]:
        return (True, build_domain.STATUS_CANCELLED, None)

    # 3. Hard-ceiling decision (strict now > deadline).
    if hard is not None and now > hard:
        return (True, build_domain.STATUS_FAILED,
                br.CODE_MAX_RUNTIME_EXCEEDED)

    # 4. Infrastructure loss / spot interruption.
    if evidence["infrastructure_lost"]:
        return (True, build_domain.STATUS_INTERRUPTED,
                br.CODE_INFRASTRUCTURE_LOST)

    # SendCommand rejected before a command ID.
    if evidence["send_command_rejected"]:
        return (True, build_domain.STATUS_FAILED,
                br.CODE_COMMAND_LAUNCH_FAILED)

    invocation = evidence["invocation"] or {}
    status = invocation.get("Status")

    # 5. Terminal SSM Failed/TimedOut/Cancelled without agent result.
    if status == "TimedOut":
        return (True, build_domain.STATUS_FAILED,
                br.CODE_COMMAND_TIMED_OUT)
    if status == "Cancelled":
        return (True, build_domain.STATUS_INTERRUPTED,
                br.CODE_COMMAND_CANCELLED)
    if status == "Failed":
        stderr = invocation.get("StandardErrorContent", "")
        if br.PREFLIGHT_FAILURE_MARKER in stderr:
            return (True, build_domain.STATUS_FAILED,
                    br.CODE_COMMAND_PREFLIGHT_FAILED)
        if "no space left on device" in stderr:
            return (True, build_domain.STATUS_FAILED,
                    br.CODE_RUNNER_DISK_FULL)
        return (True, build_domain.STATUS_FAILED,
                br.CODE_COMMAND_EXECUTION_FAILED)

    # 6. Success without agent result: settlement, then missing result.
    if status == "Success":
        settle = evidence["settlement_deadline_ms"]
        if settle is not None and now > settle:
            return (True, build_domain.STATUS_FAILED,
                    br.CODE_AGENT_RESULT_MISSING)
        return (False, evidence["current_status"], None)

    # 7. Missing/delayed evidence: never fabricated.
    return (False, evidence["current_status"], None)


def _classify(evidence):
    return br.classify_attempt(
        current_status=evidence["current_status"],
        invocation=evidence["invocation"],
        agent_result=evidence["agent_result"],
        user_cancellation_confirmed=evidence[
            "user_cancellation_confirmed"],
        hard_deadline_ms=evidence["hard_deadline_ms"],
        infrastructure_lost=evidence["infrastructure_lost"],
        send_command_rejected=evidence["send_command_rejected"],
        settlement_deadline_ms=evidence["settlement_deadline_ms"],
        now=evidence["now"])


#: The diagnostic keys a degraded fragment may lose (identity, detail,
#: and stream fields).
_DEGRADABLE_SCALARS = ("status", "status_details", "response_code",
                       "execution_start", "execution_end",
                       "classification", "attempt_id", "command_id",
                       "instance_id")
_DEGRADABLE_STREAMS = ("stdout", "stderr")


@st.composite
def diagnostic_fragments(draw):
    """A full base diagnostic plus 2–4 degraded fragments of it (fields
    replaced with unavailable/None as partial deliveries would carry),
    with distinct sources and observation times (Req 2.6).

    Fragments keep the FULL key set: real incoming diagnostics are
    always produced by ``build_execution_diagnostic``, which emits every
    key and represents a missing provider field as ``None`` /
    ``{"available": False}`` — it never drops keys."""
    base = br.build_execution_diagnostic(
        attempt={"attempt_id": "attempt-9", "command_id": "cmd-9",
                 "instance_id": "i-9"},
        invocation={
            "CommandId": "cmd-9", "InstanceId": "i-9",
            "Status": "Failed", "StatusDetails": "Failed",
            "ResponseCode": 127,
            "ExecutionStartDateTime": "2026-08-06T17:02:55Z",
            "ExecutionEndDateTime": "2026-08-06T17:03:03Z",
            "StandardOutputContent": "phase: build starting",
            "StandardErrorContent": "portal-build-agent.sh: not found",
        },
        classification=br.CODE_COMMAND_EXECUTION_FAILED,
        source="event_bridge",
        observed_at=_NOW)

    fragments = []
    for index in range(draw(st.integers(min_value=2, max_value=4))):
        fragment = json.loads(json.dumps(base))
        for key in _DEGRADABLE_SCALARS:
            if draw(st.booleans()):
                fragment[key] = None
        for key in _DEGRADABLE_STREAMS:
            choice = draw(st.sampled_from(
                ["full", "unavailable", "empty"]))
            if choice == "unavailable":
                fragment[key] = {"available": False}
            elif choice == "empty":
                fragment[key] = {"available": True, "text": "",
                                 "truncated": False, "original_bytes": 0}
        fragment["source"] = [draw(st.sampled_from(
            ["event_bridge", "scheduled_reconciliation", "callback"]))]
        fragment["observed_at"] = _NOW + draw(
            st.integers(min_value=0, max_value=300_000))
        fragment["complete"] = draw(st.booleans())
        fragments.append(fragment)
    return base, fragments


def _fold_merge(fragments):
    merged = None
    for fragment in fragments:
        merged, _ = br.merge_diagnostics(merged, fragment)
    return merged


def _normalized(diagnostic):
    result = dict(diagnostic)
    result["source"] = sorted(result.get("source") or [])
    return result


class TestProperty4DeterministicClassificationAndOrdering:
    """**Property 4: Deterministic Classification and Ordering**

    **Validates: Requirements 2.4, 2.6, 2.11**
    """

    @settings(max_examples=300, deadline=None)
    @given(evidence=classification_evidence())
    def test_classification_matches_the_design_precedence_table(
            self, evidence):
        """For any correlated evidence set, classification is the ONE
        precedence-defined outcome of the design table, uses only
        stable safe error codes, and is deterministic across repeated
        (duplicate) evaluation (Req 2.4, 2.11)."""
        outcome = _classify(evidence)
        expected = _design_table_oracle(evidence)

        assert (outcome.decided, outcome.status, outcome.error_code) \
            == expected
        assert outcome.error_code is None \
            or outcome.error_code in br.STABLE_ERROR_CODES
        if outcome.decided:
            assert build_domain.is_terminal(outcome.status)
        # Duplicate delivery of the same settled evidence converges to
        # the identical outcome (Req 2.6).
        assert _classify(evidence) == outcome

    @settings(max_examples=100, deadline=None)
    @given(data=st.data())
    def test_merge_is_order_independent_and_monotonic(self, data):
        """Every permutation and duplication of partial diagnostic
        deliveries converges to the same merged record; completeness
        only ever increases; re-delivering any fragment afterwards is a
        no-op (Req 2.6)."""
        base, fragments = data.draw(diagnostic_fragments(),
                                    label="fragments")
        indexes = list(range(len(fragments)))
        order_a = data.draw(st.permutations(indexes), label="order_a")
        order_b = data.draw(st.permutations(indexes), label="order_b")

        merged_a = _fold_merge([fragments[i] for i in order_a])
        merged_b = _fold_merge([fragments[i] for i in order_b])
        assert _normalized(merged_a) == _normalized(merged_b), \
            "delivery order changed the merged diagnostic"

        # Monotonic completeness along one delivery order.
        merged = None
        previous_ranks = {}
        for index in order_a:
            merged, _ = br.merge_diagnostics(merged, fragments[index])
            for key, value in merged.items():
                if key in ("source", "observed_at", "complete"):
                    continue
                rank = br._field_completeness(value)
                assert rank >= previous_ranks.get(key, 0), \
                    f"field {key} lost completeness during merge"
                previous_ranks[key] = rank

        # Duplicates after convergence are no-ops.
        for fragment in fragments:
            remerged, changed = br.merge_diagnostics(merged, fragment)
            assert changed is False, \
                "re-delivered fragment reported a change"
            assert _normalized(remerged) == _normalized(merged)

    @settings(max_examples=200, deadline=None)
    @given(evidence=classification_evidence(),
           terminal_status=st.sampled_from(
               sorted(build_domain.TERMINAL_STATUSES)),
           has_existing=st.booleans())
    def test_terminal_state_is_absorbing_for_any_later_evidence(
            self, evidence, terminal_status, has_existing):
        """Once terminal, NO later evidence may resurrect or overwrite
        status/error/``ended_at`` — later evidence may only increase
        diagnostic completeness (Req 2.6, 2.11)."""
        classification = _classify(evidence)
        incoming = br.build_execution_diagnostic(
            attempt={"attempt_id": "attempt-2", "command_id": "cmd-2",
                     "instance_id": "i-2"},
            invocation=evidence["invocation"],
            classification=classification.error_code,
            source="scheduled_reconciliation",
            observed_at=_NOW)
        existing = incoming if has_existing else None

        application = br.apply_evidence(
            terminal_status, existing, incoming, classification, _NOW)

        assert application.update_status is None
        assert application.update_error_code is None
        assert application.update_ended_at is None
        if has_existing:
            # The identical diagnostic is a duplicate: a no-op.
            assert application.update_diagnostic is None

        # And on a NONTERMINAL job a decided classification is applied
        # exactly as classified.
        nonterminal = br.apply_evidence(
            build_domain.STATUS_BUILDING, None, incoming,
            classification, _NOW)
        if classification.decided:
            assert nonterminal.update_status == classification.status
            assert nonterminal.update_error_code == \
                classification.error_code
            assert nonterminal.update_ended_at == _NOW
        else:
            assert nonterminal.update_status is None
            assert nonterminal.update_ended_at is None


# ===========================================================================
# Property 5: Event and Scheduled Reconciliation Convergence
# (moto + recording-SSM-fake integration)
# ===========================================================================

def _clear_state():
    for item in _JOBS.scan().get("Items", []):
        _JOBS.delete_item(Key={"build_job_id": item["build_job_id"]})
    for item in _SERVERS.scan().get("Items", []):
        _SERVERS.delete_item(Key={"server_id": item["server_id"]})
    del AUDIT_EVENTS[:]
    del SSM_GET_CALLS[:]
    del SSM_SEND_CALLS[:]
    SSM_INVOCATIONS.clear()


def _get_job(job_id):
    return build_dispatcher.to_native(
        _JOBS.get_item(Key={"build_job_id": job_id}).get("Item"))


def _seed_running_job(job_id, command_id, instance_id, base_now):
    attempt_id = str(uuid.uuid4())
    job = {
        "build_job_id": job_id,
        "build_target": build_domain.TARGET_AMD64,
        "execution_mode": build_domain.EXECUTION_MODE_EPHEMERAL,
        "status": build_domain.STATUS_BUILDING,
        "requested_by": "operator-1",
        "created_at": base_now - 10_000,
        "started_at": base_now - 9_000,
        "config_snapshot": {"max_runtime_hours": 4},
        "runner": {"instance_id": instance_id},
        "ssm": {"command_id": command_id, "instance_id": instance_id},
        "execution_attempt": {
            "attempt_id": attempt_id,
            "command_id": command_id,
            "instance_id": instance_id,
        },
    }
    _JOBS.put_item(Item=job)
    return job


def _script_invocation(command_id, instance_id, status, stderr):
    SSM_INVOCATIONS[command_id] = {
        "CommandId": command_id,
        "InstanceId": instance_id,
        "Status": status,
        "StatusDetails": status,
        "ResponseCode": 0 if status == "Success" else 1,
        "StandardOutputContent": "",
        "StandardErrorContent": stderr,
    }


def _deliver_event(command_id, instance_id, status):
    build_events.handler({
        "detail-type": "EC2 Command Status-change Notification",
        "source": "aws.ssm",
        "detail": {
            "command-id": command_id,
            "instance-id": instance_id,
            "status": status,
        },
    }, None)


def _tick(now):
    from unittest import mock
    with mock.patch.object(build_dispatcher, "run_shell_sync",
                           return_value=None):
        build_dispatcher.run_tick(now=now)


#: The precedence-defined convergence target for one terminal command
#: status when no terminal agent result ever arrives (design table):
#: (job status, error code or None).
_EXPECTED_OUTCOME = {
    "Failed": (build_domain.STATUS_FAILED,
               br.CODE_COMMAND_EXECUTION_FAILED),
    "TimedOut": (build_domain.STATUS_FAILED,
                 br.CODE_COMMAND_TIMED_OUT),
    "Cancelled": (build_domain.STATUS_INTERRUPTED,
                  br.CODE_COMMAND_CANCELLED),
    "Success": (build_domain.STATUS_FAILED,
                br.CODE_AGENT_RESULT_MISSING),
}


class TestProperty5EventAndScheduledReconciliationConvergence:
    """**Property 5: Event and Scheduled Reconciliation Convergence**

    **Validates: Requirements 2.1, 2.5, 2.6**
    """

    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(terminal_status=st.sampled_from(
               ["Failed", "TimedOut", "Cancelled", "Success"]),
           deliveries=st.integers(min_value=0, max_value=3),
           invocation_delayed=st.booleans())
    def test_zero_one_or_many_deliveries_converge_to_one_outcome(
            self, terminal_status, deliveries, invocation_delayed):
        """Whether the terminal EventBridge notification is delivered
        zero, one, or multiple times — and whether the final invocation
        is immediately visible or only eventually consistent
        (``InvocationDoesNotExist``) — the event path plus the
        scheduled tick converge within the configured bound to the SAME
        precedence-defined terminal outcome, diagnostic classification,
        and single terminal audit. A missing event costs latency,
        never correctness (Req 2.1, 2.5, 2.6)."""
        _clear_state()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        command_id = str(uuid.uuid4())
        instance_id = f"i-{uuid.uuid4().hex[:12]}"
        canary = "SECRETCANARY" + uuid.uuid4().hex
        stderr = (f"AWS_SECRET_ACCESS_KEY={canary}\n"
                  f"process exited with status 1")

        base_now = build_dispatcher.now_ms() + _MINUTE_MS
        _seed_running_job(job_id, command_id, instance_id, base_now)

        if not invocation_delayed:
            _script_invocation(command_id, instance_id,
                               terminal_status, stderr)
        for _ in range(deliveries):
            _deliver_event(command_id, instance_id, terminal_status)
        if invocation_delayed:
            # Eventual consistency never fabricates a command failure:
            # every delivery so far saw InvocationDoesNotExist, so the
            # job MUST still be nonterminal (Req 2.5).
            if deliveries:
                intermediate = _get_job(job_id)
                assert intermediate["status"] == \
                    build_domain.STATUS_BUILDING
                assert intermediate["reconciliation"]["lookup_state"] \
                    == br.LOOKUP_PENDING
                assert AUDIT_EVENTS == []
            _script_invocation(command_id, instance_id,
                               terminal_status, stderr)

        # Scheduled reconciliation ticks: one inside and one strictly
        # past the settlement window (Req 2.5 bounded convergence).
        tick_1 = base_now + _MINUTE_MS
        tick_2 = tick_1 + build_dispatcher.SETTLEMENT_WINDOW_MS \
            + 2 * _MINUTE_MS
        _tick(tick_1)
        _tick(tick_2)

        expected_status, expected_code = \
            _EXPECTED_OUTCOME[terminal_status]
        job = _get_job(job_id)
        assert job["status"] == expected_status
        assert job["ended_at"]
        if expected_status == build_domain.STATUS_FAILED:
            assert job["error"]["code"] == expected_code
        else:
            # Interrupted keeps its existing shape (no error record);
            # the code lives in the diagnostic.
            assert "error" not in job

        diagnostic = job["execution_diagnostic"]
        assert diagnostic["classification"] == expected_code
        assert diagnostic["command_id"] == command_id
        assert diagnostic["stderr"]["available"] is True

        # Exactly one terminal audit despite duplicates + extra ticks.
        terminal_audits = [a for a in AUDIT_EVENTS if a["action"] in
                           ("build_failed", "build_interrupted")]
        assert len(terminal_audits) == 1

        # No canary in any captured sink (Req 2.10 wiring).
        for name, text in (
                ("job item", json.dumps(job, default=str)),
                ("audit log", json.dumps(AUDIT_EVENTS, default=str))):
            assert canary not in text, f"canary leaked into {name}"

        # Convergence is stable: one more duplicate event and one more
        # tick change nothing (Req 2.6).
        ended_at = job["ended_at"]
        _deliver_event(command_id, instance_id, terminal_status)
        _tick(tick_2 + _MINUTE_MS)
        after = _get_job(job_id)
        assert after["status"] == expected_status
        assert after["ended_at"] == ended_at
        assert len([a for a in AUDIT_EVENTS if a["action"] in
                    ("build_failed", "build_interrupted")]) == 1

    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(ticks=st.integers(min_value=1, max_value=3),
           nonterminal_status=st.sampled_from(
               ["Pending", "InProgress", "Delayed"]))
    def test_genuinely_nonterminal_invocation_stays_nonterminal(
            self, ticks, nonterminal_status):
        """For any number of scheduled ticks, a genuinely nonterminal
        invocation keeps the job nonterminal: no terminal transition,
        no error, no audit is ever fabricated (Req 2.5)."""
        _clear_state()
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        command_id = str(uuid.uuid4())
        instance_id = f"i-{uuid.uuid4().hex[:12]}"
        base_now = build_dispatcher.now_ms() + _MINUTE_MS
        _seed_running_job(job_id, command_id, instance_id, base_now)
        _script_invocation(command_id, instance_id, nonterminal_status,
                           "")

        for index in range(ticks):
            _tick(base_now + (index + 1) * _MINUTE_MS)

        job = _get_job(job_id)
        assert job["status"] == build_domain.STATUS_BUILDING
        assert "error" not in job
        assert "ended_at" not in job
        assert AUDIT_EVENTS == []
