"""Property test for secret non-disclosure (station-quick-setup task 5.14).

**Feature: station-quick-setup, Property 9: Secrets never appear at rest or in output**

For any execution of the registration, regeneration, bundle,
credential-exchange, and status-report flows, the raw Setup_Token secret, the
report secret, and the issued credential secret material appear in no persisted
DynamoDB item, no audit event payload, no log output, and no error response --
the only permitted occurrence is the Setup_Command in the
registration/regeneration response.

**Validates: Requirements 3.6, 8.3**

This drives all five *real* flows end to end against a moto-backed AWS stack:

  * ``device_registrations.create_registration`` (POST /device-registrations)
    mints the first Setup_Token and returns the Setup_Command.
  * ``device_registrations.regenerate_command`` (POST .../command) rotates the
    token and returns a fresh Setup_Command.
  * ``quick_setup`` POST /quick-setup/bundle returns the bundle manifest.
  * ``quick_setup`` POST /quick-setup/credentials exchanges the token for
    short-lived STS credentials plus a per-registration report secret.
  * ``quick_setup`` POST /quick-setup/status reports completion/failure,
    authenticated by the raw report secret.

After the flows run, the test harvests every "at rest / in output" channel the
property constrains -- every persisted item in the device-registrations table
and the shared audit-log table, every log record emitted by the handlers and
``shared_utils``, and the body of every *non-2xx* (error) response -- and
asserts that none of the sensitive values (the raw token secret, the full wire
token, the report secret, and the STS secret access key / session token) occur
in any of them. Only their one-way SHA-256 hashes are permitted at rest.

The 200 credential-exchange response body is deliberately *excluded* from the
scanned channels: delivering the credentials and report secret to the station
in that response is the whole point of the exchange, and the property forbids
secret material only at rest, in audit payloads, in logs, and in *error*
responses. The test additionally asserts, as a positive control, that the token
secret really does appear inside each returned Setup_Command -- so the scan is
exercising the correct secret rather than a value that never existed.

Every registration id / use case id is unique per example so the module-scoped
moto tables stay isolated across examples, and each request is presented from a
distinct source IP so the invalid-token rate limiter never trips and confounds
the flows.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Table / bucket names match the other quick-setup tests so the real
# shared_utils module (which reads table names at import time) resolves to the
# same moto-backed resources.
REGISTRATIONS_TABLE = "test-device-registrations"
USECASES_TABLE = "test-usecases"
USER_ROLES_TABLE = "test-user-roles"
AUDIT_LOG_TABLE = "test-audit-log"
ARTIFACTS_BUCKET = "test-portal-artifacts"

BUNDLE_KEY = "quick-setup/current/setup-bundle.tar.gz"
BUNDLE_BYTES = b"quick-setup bundle payload (opaque tarball bytes for the test)"
BOOTSTRAP_KEY = "quick-setup/current/bootstrap.sh"

# IoT Thing / Thing Group name alphabet, pattern [a-zA-Z0-9:_-]{1,128} (Req 1.2).
_IOT_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789:_-"
)
iot_names = st.text(alphabet=_IOT_NAME_ALPHABET, min_size=1, max_size=128)

# Roles holding Permission.MANAGE_DEVICES for a Use_Case (authorized user).
AUTHORIZED_ROLES = ["UseCaseAdmin", "Operator", "PortalAdmin"]

# A spread of plausible AWS regions so the flows reflect the Use_Case region.
aws_regions = st.sampled_from([
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1",
    "eu-central-1", "ap-southeast-1", "ap-northeast-1",
])

# Free-text error summaries the station might report on a failed run. Kept from
# containing the sensitive values (which is exactly what the property asserts
# the system never echoes back) -- the operator-supplied summary is arbitrary
# text, not a channel the secret would ever legitimately flow through.
error_summaries = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=200
)


@pytest.fixture(scope="module")
def qs_stack():
    """moto-backed AWS with the device-registrations, use-cases, user-roles,
    and audit tables plus the artifacts bucket (with the bundle object present),
    and the real device_registrations / quick_setup / token_service /
    rate_limiter / session_policy / shared_utils modules imported inside the
    mock so their module-level boto3 resources are intercepted by moto."""
    os.environ["REGISTRATIONS_TABLE"] = REGISTRATIONS_TABLE
    os.environ.setdefault("USECASES_TABLE", USECASES_TABLE)
    os.environ.setdefault("USER_ROLES_TABLE", USER_ROLES_TABLE)
    os.environ.setdefault("AUDIT_LOG_TABLE", AUDIT_LOG_TABLE)
    os.environ["PORTAL_ARTIFACTS_BUCKET"] = ARTIFACTS_BUCKET
    os.environ["QUICK_SETUP_BUNDLE_KEY"] = BUNDLE_KEY
    os.environ["QUICK_SETUP_BOOTSTRAP_KEY"] = BOOTSTRAP_KEY
    os.environ["QUICK_SETUP_BOOTSTRAP_SHA256"] = "a" * 64

    from moto import mock_aws

    with mock_aws():
        import hashlib

        import boto3

        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=REGISTRATIONS_TABLE,
            KeySchema=[{"AttributeName": "registration_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "registration_id", "AttributeType": "S"},
                {"AttributeName": "usecase_id", "AttributeType": "S"},
                {"AttributeName": "device_name", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "usecase-device-index",
                "KeySchema": [
                    {"AttributeName": "usecase_id", "KeyType": "HASH"},
                    {"AttributeName": "device_name", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=USECASES_TABLE,
            KeySchema=[{"AttributeName": "usecase_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "usecase_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=USER_ROLES_TABLE,
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
        ddb.create_table(
            TableName=AUDIT_LOG_TABLE,
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

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=ARTIFACTS_BUCKET)
        # The bundle object must exist so get_bundle_manifest's head_object
        # succeeds and a manifest is served (Req 4.10). Bake its true SHA-256
        # into the env so the served checksum matches the object.
        s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=BUNDLE_KEY, Body=BUNDLE_BYTES)
        s3.put_object(Bucket=ARTIFACTS_BUCKET, Key=BOOTSTRAP_KEY,
                      Body=b"#!/usr/bin/env bash\n")
        os.environ["QUICK_SETUP_BUNDLE_SHA256"] = hashlib.sha256(
            BUNDLE_BYTES).hexdigest()

        for module_name in ("shared_utils", "token_service", "rate_limiter",
                            "session_policy", "quick_setup",
                            "device_registrations"):
            sys.modules.pop(module_name, None)
        import shared_utils  # noqa: F401
        import token_service
        import rate_limiter  # noqa: F401
        import session_policy  # noqa: F401
        import quick_setup
        import device_registrations

        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {
            "dr": device_registrations,
            "qs": quick_setup,
            "ts": token_service,
            "registrations": resource.Table(REGISTRATIONS_TABLE),
            "usecases": resource.Table(USECASES_TABLE),
            "audit": resource.Table(AUDIT_LOG_TABLE),
        }


class _RecordCaptureHandler(logging.Handler):
    """Collects fully-formatted log records so the test can scan every byte a
    handler emitted (the message plus any interpolated args)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        try:
            self.messages.append(self.format(record))
        except Exception:  # pragma: no cover - never fail the flow on logging
            self.messages.append(str(record.__dict__))


def _register_event(user_id, role, device_name, device_group, usecase_id):
    return {
        "httpMethod": "POST",
        "path": "/device-registrations",
        "body": json.dumps({
            "device_name": device_name,
            "device_group": device_group,
            "usecase_id": usecase_id,
        }),
        "requestContext": {
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "v1",
            "authorizer": {"claims": {
                "sub": user_id,
                "email": f"{user_id}@example.com",
                "cognito:username": user_id,
                "custom:role": role,
            }},
        },
    }


def _regenerate_event(user_id, role, registration_id):
    return {
        "httpMethod": "POST",
        "path": f"/device-registrations/{registration_id}/command",
        "pathParameters": {"id": registration_id},
        "body": json.dumps({}),
        "requestContext": {
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "stage": "v1",
            "authorizer": {"claims": {
                "sub": user_id,
                "email": f"{user_id}@example.com",
                "cognito:username": user_id,
                "custom:role": role,
            }},
        },
    }


def _quick_setup_event(path_suffix, body, source_ip):
    return {
        "httpMethod": "POST",
        "path": f"/v1/quick-setup/{path_suffix}",
        "body": json.dumps(body),
        "requestContext": {
            "domainName": "api.example.com",
            "stage": "v1",
            "identity": {"sourceIp": source_ip},
        },
    }


def _create_usecase(qs_stack, region):
    """A fresh use case with a (non-root) cross-account role so both the
    registration IoT uniqueness probe (assume + describe_thing against the
    empty moto account) and the credential-exchange sts.assume_role succeed."""
    usecase_id = f"uc-{uuid.uuid4()}"
    qs_stack["usecases"].put_item(Item={
        "usecase_id": usecase_id,
        "name": "Test Use Case",
        "account_id": ACCOUNT_ID,
        "region": region,
        "cross_account_role_arn":
            f"arn:aws:iam::{ACCOUNT_ID}:role/DDAPortalAccessRole",
        "external_id": f"ext-{uuid.uuid4()}",
    })
    return usecase_id


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    device_name=iot_names,
    device_group=iot_names,
    user_id=st.from_regex(r"[a-zA-Z0-9_-]{1,40}", fullmatch=True),
    role=st.sampled_from(AUTHORIZED_ROLES),
    region=aws_regions,
    report_status=st.sampled_from(["completed", "failed"]),
    error_summary=error_summaries,
)
def test_secrets_never_appear_at_rest_or_in_output(
    qs_stack, device_name, device_group, user_id, role, region,
    report_status, error_summary,
):
    """**Feature: station-quick-setup, Property 9: Secrets never appear at rest or in output**

    **Validates: Requirements 3.6, 8.3**
    """
    dr = qs_stack["dr"]
    qs = qs_stack["qs"]
    ts = qs_stack["ts"]

    usecase_id = _create_usecase(qs_stack, region)

    # Capture every log record the handlers / shared_utils emit during the
    # flows so "no log output" can be checked against the actual bytes logged.
    # The handler is attached to the root logger (which the handler modules log
    # through) WITHOUT lowering its level: the application code logs at
    # INFO/ERROR, so the effective level stays there and the AWS SDK's own
    # DEBUG wire-logging (which would echo the STS response body) is filtered
    # at its source rather than polluting the captured application log output.
    capture = _RecordCaptureHandler()
    capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)

    # Bodies of the *error* (non-2xx) responses, the only responses the
    # property constrains. The 200 credential response is intentionally kept
    # out of this list (it legitimately carries the credentials + report
    # secret to the station).
    error_response_bodies: list[str] = []
    # Success-response bodies that MAY contain the Setup_Command (the sole
    # permitted occurrence of the token) -- used only as a positive control.
    setup_commands: list[str] = []

    try:
        # --- Flow 1: registration -> first Setup_Token + Setup_Command.
        resp = dr.handler(
            _register_event(user_id, role, device_name, device_group,
                            usecase_id),
            None,
        )
        assert resp["statusCode"] == 201, resp["body"]
        reg_body = json.loads(resp["body"])
        registration_id = reg_body["registration"]["registration_id"]
        first_command = reg_body["setup_command"]
        setup_commands.append(first_command)
        first_parsed = ts.parse_token(_token_from_command(first_command))
        assert first_parsed is not None
        first_secret = first_parsed[1]

        # --- Flow 2: regeneration -> rotated Setup_Token + Setup_Command.
        resp = dr.handler(
            _regenerate_event(user_id, role, registration_id), None)
        assert resp["statusCode"] == 200, resp["body"]
        regen_body = json.loads(resp["body"])
        current_command = regen_body["setup_command"]
        setup_commands.append(current_command)
        current_token = _token_from_command(current_command)
        current_parsed = ts.parse_token(current_token)
        assert current_parsed is not None
        current_secret = current_parsed[1]

        # --- Flow 3: bundle manifest for the current token (not consumed).
        resp = qs.handler(
            _quick_setup_event("bundle", {"token": current_token},
                               f"ip-{uuid.uuid4()}"),
            None,
        )
        assert resp["statusCode"] == 200, resp["body"]
        # The manifest carries no secret material; if it were an error it would
        # be scanned below, but a 200 here is expected.

        # --- Flow 4: credential exchange -> credentials + report secret.
        resp = qs.handler(
            _quick_setup_event("credentials", {"token": current_token},
                               f"ip-{uuid.uuid4()}"),
            None,
        )
        assert resp["statusCode"] == 200, resp["body"]
        cred_body = json.loads(resp["body"])
        credentials = cred_body["credentials"]
        secret_access_key = credentials["secret_access_key"]
        session_token = credentials["session_token"]
        report_secret = cred_body["report_secret"]

        # --- Flow 5: status report authenticated by the raw report secret.
        report_body = {
            "registration_id": registration_id,
            "report_secret": report_secret,
            "status": report_status,
        }
        if report_status == "failed":
            report_body["error_summary"] = error_summary
        resp = qs.handler(
            _quick_setup_event("status", report_body, f"ip-{uuid.uuid4()}"),
            None,
        )
        assert resp["statusCode"] == 200, resp["body"]

        # --- Extra error paths: re-present the now-consumed token so an error
        #     response is produced and scanned for leakage too.
        for suffix in ("bundle", "credentials"):
            err = qs.handler(
                _quick_setup_event(suffix, {"token": current_token},
                                   f"ip-{uuid.uuid4()}"),
                None,
            )
            if not (200 <= err["statusCode"] < 300):
                error_response_bodies.append(err["body"])
    finally:
        root_logger.removeHandler(capture)

    # The sensitive values that must never appear at rest / in the scanned
    # output channels. The full wire tokens are included so neither the raw
    # secret nor the composed token leaks.
    sensitive_values = [
        first_secret,
        current_secret,
        _token_from_command(first_command),
        current_token,
        report_secret,
        secret_access_key,
        session_token,
    ]
    # Guard against a degenerate empty value slipping a vacuous "in" check.
    sensitive_values = [v for v in sensitive_values if v]

    # Positive control: the token secret really does live in the Setup_Command
    # (so the scans below are checking a value that genuinely exists).
    assert first_secret and first_secret in first_command
    assert current_secret and current_secret in current_command

    # --- Channel 1 + 2: every persisted DynamoDB item (device-registrations
    #     table AND the shared audit-log table, which also carries the audit
    #     event payloads). Only one-way hashes may persist -- never the raw
    #     secret material.
    registration_items = _scan_all(qs_stack["registrations"])
    audit_items = _scan_all(qs_stack["audit"])
    at_rest_blob = json.dumps(registration_items, default=str) + \
        json.dumps(audit_items, default=str)

    # --- Channel 3: log output emitted during the flows.
    log_blob = "\n".join(capture.messages)

    # --- Channel 4: error (non-2xx) response bodies.
    error_blob = "\n".join(error_response_bodies)

    for channel_name, blob in (
        ("dynamodb item / audit payload", at_rest_blob),
        ("log output", log_blob),
        ("error response", error_blob),
    ):
        for value in sensitive_values:
            assert value not in blob, (
                f"secret material leaked into {channel_name}: "
                f"registration_id={registration_id!r}"
            )

    # The persisted registration keeps only hashes of the token and report
    # secret -- their presence confirms the hash-only-at-rest contract (Req
    # 3.6) while the raw values are absent per the scan above.
    stored = qs_stack["registrations"].get_item(
        Key={"registration_id": registration_id}
    )["Item"]
    assert stored.get("token_hash")
    assert stored.get("report_secret_hash")


def _token_from_command(command: str) -> str:
    """Extract the Setup_Token embedded as the trailing ``--token <token>``
    argument of the one-line Setup_Command."""
    marker = "--token "
    idx = command.rfind(marker)
    assert idx != -1, command
    return command[idx + len(marker):].strip()


def _scan_all(table):
    """Return every item in a DynamoDB table (paginated scan)."""
    items = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items
