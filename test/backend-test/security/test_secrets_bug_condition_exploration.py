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
"""Bug-condition exploration test (Task 1) for
security-secrets-credentials-jwt-fixes.

Property 1: Bug Condition -- a secret value reaches a log / command sink, a
real-looking credential sits in copy-pasteable text, or a scanner-flagged
pattern lacks a documented exception, across the in-scope application-code sites.

**These tests are written to assert the SECURE (post-fix) behavior, so they are
EXPECTED TO FAIL on the UNFIXED tree.** Each failure surfaces the counterexample
that confirms the bug exists:

  * S1 -- the JWT authorizer invocation log still contains the bearer token,
  * S2 -- deploy.py still f-string-interpolates the AWS access/secret keys into
    the ``AWS-RunShellScript`` SSM command strings,
  * S5 -- the unverified ``verify_signature=False`` pre-parse line still carries
    no documented ``# nosem``,
  * the repo audit still finds disallowed bug-condition hits (non-empty).

The SAME tests are re-run in task 7 against the fixed tree, where they must PASS
(log free of the token, command strings free of the keys, the pre-parse line
documented, audit clean).

Hypothesis (vendored under .hypothesis/) is used where the input domain is
generatable (secret-bearing authorizer tokens), scoped to concrete failing
shapes for reproducibility.

Validates: Requirements 1.1, 1.2, 1.5, 1.10
"""
import importlib.util
import logging
import os
import sys
from argparse import Namespace

import pytest
from hypothesis import given, settings, HealthCheck, example
from hypothesis import strategies as st

import secrets_audit

REPO_ROOT = secrets_audit.REPO_ROOT


def _load_module_from_path(mod_name, rel_path, injected_modules=None):
    """Load a single source file as a module WITHOUT importing the heavy backend
    package graph. ``injected_modules`` lets us stub the module's imports so we
    exercise the REAL target code in isolation."""
    injected = injected_modules or {}
    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        path = os.path.join(REPO_ROOT, rel_path)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod  # register before exec for self-references
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


# ---------------------------------------------------------------------------
# Repo audit (S10 / Req 2.10) -- this is the gate re-run in task 7.
# ---------------------------------------------------------------------------

def test_secrets_audit_returns_no_disallowed_hits():
    """The secrets audit must return ZERO disallowed bug-condition hits in the
    in-scope tree (``cdk.out/asset.*`` and the vendored ``edgemlsdk/edgemlsdk/``
    duplicate excluded), other than occurrences carrying a documented
    ``# nosem``/``# nosec`` exception.

    UNFIXED-TREE EXPECTATION: this FAILS -- the disallowed hits it lists ARE the
    S1/S2/S5 counterexamples. Validates Req 1.1, 1.2, 1.5, 1.10 (enumeration),
    and is the pattern gate re-run in task 7.
    """
    all_hits = secrets_audit.run_audit()

    # None of the generated CDK artifacts may leak into the audit result.
    leaked = [h for h in all_hits if secrets_audit.EXCLUDED_PATH_SUBSTRING in h.path]
    assert not leaked, f"cdk.out/asset.* copies must be excluded, got: {leaked}"

    # Per-site counterexample summary for the failure message.
    per_site = {}
    for label, frag in secrets_audit.IN_SCOPE_SITES.items():
        per_site[label] = [
            f"{os.path.relpath(h.path, REPO_ROOT)}:{h.lineno} [{h.category}] {h.text.strip()}"
            for h in secrets_audit.hits_for(frag, all_hits)
        ]

    disallowed = secrets_audit.disallowed_hits()
    detail_lines = []
    for label, lines in per_site.items():
        detail_lines.append(f"  {label}: {len(lines)} raw hit(s)")
        detail_lines.extend(f"      {ln}" for ln in lines)
    detail = "\n".join(detail_lines)

    assert not disallowed, (
        f"Secrets audit found {len(disallowed)} disallowed bug-condition hit(s) "
        f"across the in-scope tree (counterexamples confirming the bug):\n"
        + "\n".join(
            f"  [{h.category}] {os.path.relpath(h.path, REPO_ROOT)}:{h.lineno}: {h.text.strip()}"
            for h in disallowed
        )
        + f"\n\nRaw per-site enumeration:\n{detail}"
    )


# ---------------------------------------------------------------------------
# S1 -- jwt_authorizer.handler must NOT log the bearer token
# ---------------------------------------------------------------------------

_JWT_AUTHORIZER_REL = os.path.join(
    "edge-cv-portal", "backend", "functions", "jwt_authorizer.py"
)


def _load_jwt_authorizer():
    # jwt_authorizer.py imports PyJWT (`import jwt`), a cloud-portal Lambda dep
    # not installed in the edge runtime flask-app image (present on JP6 but not
    # JP5). Skip the jwt_authorizer-dependent tests cleanly when PyJWT is
    # unavailable; secrets_audit.py still statically guards this source.
    pytest.importorskip("jwt")
    return _load_module_from_path("jwt_authorizer_under_test", _JWT_AUTHORIZER_REL)


class _CapturingHandler(logging.Handler):
    """Collects fully-formatted log lines emitted by the module's logger."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _invoke_handler_capture(mod, event, claims=None):
    """Invoke ``handler`` with logging captured. ``validate_jwt_token`` is
    stubbed so the decode is deterministic and the handler runs to completion;
    the invocation log line (S1) is emitted first regardless."""
    handler_log = _CapturingHandler()
    handler_log.setLevel(logging.DEBUG)
    mod.logger.addHandler(handler_log)
    mod.logger.setLevel(logging.DEBUG)

    original_validate = mod.validate_jwt_token
    mod.validate_jwt_token = lambda token: (claims or {
        "sub": "user-123", "email": "user@example.com", "iss": "https://issuer",
    })
    try:
        policy = mod.handler(event, None)
    finally:
        mod.validate_jwt_token = original_validate
        mod.logger.removeHandler(handler_log)
    return policy, "\n".join(handler_log.lines)


_TOKEN = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9."
    "eyJzdWIiOiJ1c2VyLTEyMyIsImlzcyI6Imh0dHBzOi8vaXNzdWVyIn0."
    "S1gnATuReSeCReTvALue0123456789"
)


def _authorizer_event(token):
    bearer = f"Bearer {token}"
    return {
        "type": "TOKEN",
        "authorizationToken": bearer,
        "headers": {"Authorization": bearer},
        "methodArn": "arn:aws:execute-api:us-east-1:123456789012:abc/prod/GET/x",
        "requestContext": {"requestId": "req-abc-123"},
    }


def test_handler_log_does_not_contain_bearer_token():
    """S1 (Req 1.1): the invocation log line must NOT contain the bearer token
    or any headers value. UNFIXED-TREE EXPECTATION: FAILS -- the handler dumps
    the whole event (``json.dumps(event)``), so the token lands in the log."""
    mod = _load_jwt_authorizer()
    event = _authorizer_event(_TOKEN)
    _, log_output = _invoke_handler_capture(mod, event)

    print(f"\n[S1 counterexample] handler invocation log line(s):\n{log_output}")

    # nosem-mode note: after the S1 fix the redacted log must contain methodArn
    # (and requestId) but NEVER the token substring.
    assert _TOKEN not in log_output, (
        "COUNTEREXAMPLE (S1): the bearer token appears verbatim in the JWT "
        f"authorizer invocation log line (persisted to CloudWatch Logs):\n{log_output}"
    )


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(secret=st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
                      min_size=8, max_size=40))
@example(secret="SuperSecretTokenValue123")
def test_handler_never_logs_secret_token_property(secret):
    """S1 (property, Req 1.1): for any secret-bearing token, the invocation log
    line must not contain it. UNFIXED-TREE EXPECTATION: FAILS (the whole event,
    token included, is dumped to the log)."""
    mod = _load_jwt_authorizer()
    token = f"eyJhbGciOiJSUzI1NiJ9.payload.{secret}"
    event = _authorizer_event(token)
    _, log_output = _invoke_handler_capture(mod, event)
    assert secret not in log_output, (
        f"COUNTEREXAMPLE (S1): secret token fragment {secret!r} reached the "
        f"invocation log line: {log_output!r}"
    )


def test_handler_still_returns_allow_policy_for_valid_token():
    """Sanity anchor (preservation preview, Req 3.1): with a stubbed decode the
    handler returns an Allow policy whose principalId is the subject id. This
    holds on both the unfixed and fixed trees -- only the log content changes."""
    mod = _load_jwt_authorizer()
    event = _authorizer_event(_TOKEN)
    policy, _ = _invoke_handler_capture(mod, event, claims={"sub": "user-123"})
    stmt = policy["policyDocument"]["Statement"][0]
    assert policy["principalId"] == "user-123"
    assert stmt["Effect"] == "Allow"
    assert stmt["Resource"] == event["methodArn"]


# ---------------------------------------------------------------------------
# S2 -- deploy.py must NOT interpolate AWS keys into the SSM command strings
# ---------------------------------------------------------------------------

# NOTE (S2 re-pin, Req 1.2): Task 1's original implementation of this helper was
# a STATIC replica of the (then-unfixed) ``deploy.py`` source lines. After
# task 5 removed the two ``export AWS_*`` entries and the ``-a/-s`` mqtt
# fragment from the real ``deploy.py``, the static replica no longer reflects
# the source under test. The helper has been reworked to run the REAL
# ``deploy.py`` main() with boto3 stubbed and capture the ``commands`` lists it
# hands to SSM — mirroring the ``_capture_ssm_commands`` pattern used in
# ``test/backend-test/security/preservation/test_preservation_deploy_ssm.py``.
# The two tests below therefore assert on the actual constructed strings: post-
# fix they contain neither the fake credentials nor the ``-a KEY -s SECRET``
# fragment, and would FAIL if those fragments were reintroduced.


_FAKE_ACCESS_KEY = "AKIAFAKEACCESSKEY123"
_FAKE_SECRET_KEY = "SECRETFAKEabcdef0123456789+/EXAMPLEKEY"
_FAKE_TOKEN = "FQoTOKENexample"


def _make_deploy_boto3_stub():
    """Deterministic boto3 stub whose ``get_credentials()`` returns the fake
    access/secret keys defined above, so the captured command strings are
    reproducible."""
    import json
    import types

    boto3 = types.ModuleType("boto3")

    class _Creds:
        access_key = _FAKE_ACCESS_KEY
        secret_key = _FAKE_SECRET_KEY
        token = _FAKE_TOKEN

    class _SecretsClient:
        def get_secret_value(self, SecretId=None):
            return {
                "SecretString": json.dumps(
                    {"AWS_ACCESS_KEY_ID": _FAKE_ACCESS_KEY,
                     "AWS_SECRET_ACCESS_KEY": _FAKE_SECRET_KEY}
                )
            }

    class _Session:
        region_name = "us-west-2"

        def client(self, *a, **k):
            service = k.get("service_name") or (a[0] if a else None)
            if service == "secretsmanager":
                return _SecretsClient()
            return types.SimpleNamespace(upload_file=lambda *a, **k: None)

        def get_credentials(self):
            return _Creds()

    boto3.Session = lambda *a, **k: _Session()
    boto3.client = lambda *a, **k: types.SimpleNamespace()

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        pass

    exc.ClientError = ClientError
    botocore.exceptions = exc
    return {"boto3": boto3, "botocore": botocore, "botocore.exceptions": exc}


def _capture_deploy_ssm_commands(args):
    """Run the REAL ``deploy.main(args)`` with boto3 stubbed and the AWS side
    effects patched out; return the list of ``commands`` lists handed to SSM
    (download list, then mqtt list if args.mqtt). Mirrors the preservation
    suite's ``_capture_ssm_commands`` approach so these exploration tests
    exercise the actual source under test."""
    mod = _load_module_from_path(
        "deploy_exploration",
        os.path.join("src", "edgemlsdk", "src", "test", "longevity", "deploy.py"),
        injected_modules=_make_deploy_boto3_stub(),
    )
    captured = []
    mod.set_aws_access_keys_from_secrets_manager = lambda: None
    mod.upload_folder_to_s3 = lambda *a, **k: None
    mod.upload_file_to_s3 = lambda *a, **k: None
    mod.DeployLongevity.create_instance = lambda self, *a, **k: "i-abc"
    mod.DeployLongevity.run_commands_via_ssm_with_retry = (
        lambda self, iid, mr, commands, creds: captured.append(list(commands))
    )
    mod.DeployLongevity.close_ssm = lambda self: None
    mod.main(args)
    return captured


def _canonical_deploy_args():
    # artifacts_bucket_owner / longevity_bucket_owner were added to deploy.main
    # by the S3 bucket-squatting batch (B1). Supply explicit values so
    # resolution short-circuits (the stubbed boto3 session has no real `sts`
    # client). The resulting `head-bucket --expected-bucket-owner <acct>`
    # preflight entries carry no credentials, so these secrets assertions still
    # hold.
    return Namespace(
        mqtt="mqtt", platform="aarch64", ubuntu_version="22.04",
        python_version="3.11", region="us-west-2",
        mqtt_endpoint="a.iot.us-west-2.amazonaws.com", release_date="20230918",
        longevity_hours=72, payload_size=50,
        artifacts_bucket_owner="123456789012",
        longevity_bucket_owner="123456789012",
    )


def test_deploy_download_command_has_no_credentials():
    """S2 (Req 1.2): the ``download_edgemlsdk_release_artifacts`` SSM command
    strings constructed by the REAL ``deploy.py`` must contain NEITHER the fake
    access key NOR the fake secret key value. Post-fix (task 5) this holds; the
    test would FAIL if the two ``export AWS_*`` entries were reintroduced."""
    captured = _capture_deploy_ssm_commands(_canonical_deploy_args())
    assert len(captured) >= 1, "expected at least the download commands list"
    download = captured[0]
    joined = "\n".join(download)
    print(f"\n[S2] captured download command list (no credential values):\n{joined}")

    assert _FAKE_ACCESS_KEY not in joined and _FAKE_SECRET_KEY not in joined, (
        "COUNTEREXAMPLE (S2): the fake AWS access/secret keys appear verbatim in "
        "the download_edgemlsdk_release_artifacts SSM command strings "
        "(would be persisted to CloudWatch Logs / SSM command history):\n"
        f"{joined}"
    )
    # The two ``export AWS_*`` entries must be gone from the constructed list.
    assert not any(c.startswith("export AWS_ACCESS_KEY_ID=") for c in download), (
        "COUNTEREXAMPLE (S2): an 'export AWS_ACCESS_KEY_ID=...' entry is still "
        f"present in the constructed download command list:\n{joined}"
    )
    assert not any(c.startswith("export AWS_SECRET_ACCESS_KEY=") for c in download), (
        "COUNTEREXAMPLE (S2): an 'export AWS_SECRET_ACCESS_KEY=...' entry is "
        f"still present in the constructed download command list:\n{joined}"
    )


def test_deploy_mqtt_command_has_no_credentials():
    """S2 (Req 1.2): the ``run_mqtt_longevity`` SSM command string constructed
    by the REAL ``deploy.py`` must contain NEITHER the fake access/secret key
    value NOR the ``-a KEY -s SECRET`` fragment. Post-fix (task 5) this holds;
    the test would FAIL if the trailing credential fragment were reintroduced."""
    captured = _capture_deploy_ssm_commands(_canonical_deploy_args())
    assert len(captured) == 2, "expected download list + mqtt list"
    run_mqtt = captured[1]
    joined = "\n".join(run_mqtt)
    print(f"\n[S2] captured run_mqtt_longevity command (no credential values):\n{joined}")

    assert _FAKE_ACCESS_KEY not in joined and _FAKE_SECRET_KEY not in joined, (
        "COUNTEREXAMPLE (S2): the fake AWS access/secret keys appear verbatim in "
        f"the run_mqtt_longevity SSM command string:\n{joined}"
    )
    assert f"-a {_FAKE_ACCESS_KEY} -s {_FAKE_SECRET_KEY}" not in joined, (
        "COUNTEREXAMPLE (S2): the '-a <key> -s <secret>' credential fragment is "
        f"still present in the mqtt run command:\n{joined}"
    )


def test_deploy_source_interpolates_credentials():
    """S2 (Req 1.2, source cross-check): the real deploy.py source must not
    interpolate ``credentials.access_key``/``secret_key`` into a command string.
    UNFIXED-TREE EXPECTATION: FAILS -- the source contains
    ``export AWS_ACCESS_KEY_ID={credentials.access_key}`` and the ``-a``/``-s``
    fragment. This cross-checks the verbatim replication above."""
    src_path = os.path.join(
        REPO_ROOT, "src", "edgemlsdk", "src", "test", "longevity", "deploy.py"
    )
    with open(src_path) as f:
        src = f.read()

    interpolated = (
        "{credentials.access_key}" in src or "{credentials.secret_key}" in src
    )
    assert not interpolated, (
        "COUNTEREXAMPLE (S2): deploy.py f-string-interpolates "
        "credentials.access_key / credentials.secret_key into the "
        "AWS-RunShellScript command strings (the export entries and the "
        "'-a'/'-s' mqtt fragment)."
    )


# ---------------------------------------------------------------------------
# S5 -- the unverified verify_signature=False pre-parse must carry a # nosem
# ---------------------------------------------------------------------------

def test_unverified_decode_line_has_documented_marker():
    """S5 (Req 1.5): the ``jwt.decode(token, options={"verify_signature":
    False})`` pre-parse line (jwt_authorizer.py:131) must carry a documented
    ``# nosem``. UNFIXED-TREE EXPECTATION: FAILS -- the line has no marker."""
    src_path = os.path.join(REPO_ROOT, _JWT_AUTHORIZER_REL)
    with open(src_path) as f:
        lines = f.readlines()

    matches = [
        (i + 1, ln.rstrip("\n"))
        for i, ln in enumerate(lines)
        if "verify_signature" in ln and "False" in ln and not ln.lstrip().startswith("#")
    ]
    print(f"\n[S5 counterexample] unverified-decode line(s): {matches}")
    assert matches, "expected the verify_signature=False pre-parse line to exist"

    undocumented = [(no, txt) for no, txt in matches
                    if "nosem" not in txt.lower() and "nosec" not in txt.lower()]
    assert not undocumented, (
        "COUNTEREXAMPLE (S5): the unverified verify_signature=False pre-parse "
        f"line carries no documented # nosem/# nosec marker: {undocumented}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
