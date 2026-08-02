"""
Quick_Setup_Endpoint Lambda for Station Quick Setup (token-authenticated).

Serves the Station-facing, JWT-free routes of the feature:

    GET  /quick-setup/bootstrap     public static script (integrity anchored
                                    by the checksum embedded in the
                                    Setup_Command); no token pipeline.
    POST /quick-setup/bundle        Setup_Token in body -> bundle manifest.
    POST /quick-setup/credentials   Setup_Token in body -> Provisioning_Credentials.
    POST /quick-setup/status        registration_id + report_secret in body.

Every *non-bootstrap* request flows through one shared pipeline
(:func:`_token_authenticated_request`) so the security-critical ordering is
implemented exactly once:

    1. Invalid-token rate-limit admission check by source IP (429 if blocked,
       reject if the limiter state cannot be read) -- Req 3.9 / 3.10.
    2. A strict 'pending' audit entry written BEFORE any redemption effect;
       if it cannot be recorded the request is rejected and nothing is served
       (audit-before-effect) -- Req 8.4.
    3. TokenService.validate_token, mapping outcomes to a uniform
       ``invalid_token`` error and a distinct ``token_expired`` error, and
       rejecting outright on ``CHECK_FAILED`` -- Req 3.3, 3.5, 3.10.
       An expired token transitions a `pending` registration to `expired`
       (Req 3.3).
    4. The route action (bundle / credentials), only for a VALID token.
    5. Finalizing the audit entry to its terminal outcome -- Req 8.1, 8.2.

Requirements: 3.3, 3.8, 3.10, 8.1, 8.4

The bundle-manifest, credential-exchange, and status-report actions are
implemented in their own reserved tasks (5.4, 5.7, 5.11); this module wires
them into the pipeline and provides the shared pipeline and bootstrap route.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

import boto3
from botocore.exceptions import ClientError

import rate_limiter
import token_service
from session_policy import build_session_policy
from token_service import ValidationResult
from shared_utils import (
    create_response,
    record_audit_event_strict,
    finalize_audit_event,
    log_audit_event,
    get_usecase,
    get_usecase_region,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
sts = boto3.client("sts")

# The device-registrations table backs both the registration items and the
# RATELIMIT# counters (shared with token_service / rate_limiter).
REGISTRATIONS_TABLE = os.environ.get("REGISTRATIONS_TABLE")

# The portal Devices table (dda-portal-devices), keyed by device_id = IoT Thing
# name. On a successful quick-setup completion the reported DDA
# Target_Architecture is upserted here so the device is deployment-gate-ready
# without a manual admin step (device-arch-compatibility Req 2). The manual
# admin writer in devices.py is untouched (Req 2.7).
DEVICES_TABLE = os.environ.get("DEVICES_TABLE")

# DDA Target_Architectures a device can be recorded as — kept identical to
# devices.py TARGET_ARCHITECTURES. A reported value outside this fixed set is
# ignored and never written (device-arch-compatibility Req 2.2, 2.3).
TARGET_ARCHITECTURES = ("x86_64", "x86_64_nvidia",
                        "arm64_jp4", "arm64_jp5", "arm64_jp6")

# Portal artifacts bucket + the deploy-time key of the bootstrap script.
PORTAL_ARTIFACTS_BUCKET = os.environ.get("PORTAL_ARTIFACTS_BUCKET")
QUICK_SETUP_BOOTSTRAP_KEY = os.environ.get("QUICK_SETUP_BOOTSTRAP_KEY")

# The deploy-time key of the Setup_Bundle tarball and the SHA-256 computed over
# the exact bytes uploaded (Req 4.5). Both are baked into the Lambda
# environment per deployment, so only the most recently deployed bundle is ever
# served (Req 4.4).
QUICK_SETUP_BUNDLE_KEY = os.environ.get("QUICK_SETUP_BUNDLE_KEY")
QUICK_SETUP_BUNDLE_SHA256 = os.environ.get("QUICK_SETUP_BUNDLE_SHA256")

# Optional explicit endpoint override; otherwise the URL is derived from the
# incoming request so the manifest points at the serving deployment (Req 4.1).
QUICK_SETUP_API_URL = os.environ.get("QUICK_SETUP_API_URL")

# Dedicated station-provisioning role assumed for SAME-account use cases, whose
# `cross_account_role_arn` is the account root ARN (arn:aws:iam::ACCOUNT:root)
# and therefore not assumable. Injected by CDK (ComputeStack). It is trusted
# only by this Lambda's role and always assumed WITH the per-device session
# policy, so the station still receives least-privilege scoped credentials.
# Cross-account use cases assume their own use-case-account role instead.
QUICK_SETUP_PROVISIONING_ROLE_ARN = os.environ.get("QUICK_SETUP_PROVISIONING_ROLE_ARN")

# Presigned Setup_Bundle download URL lifetime (15 minutes).
BUNDLE_URL_TTL_SECONDS = 15 * 60

# Upper bound on the stored error summary of a failed provisioning run
# (Req 6.2): anything longer is truncated to this many characters.
ERROR_SUMMARY_MAX_CHARS = 1024

# Terminal status reported by the Setup_Bundle, and the two source states a
# report may be accepted from (Req 6.1, 6.2, 6.7). A report is accepted only
# from `in_progress` (first report) or `failed` (idempotent re-report after a
# failed run); every other current state — including the terminal `completed`
# — leaves the registration unchanged.
REPORT_STATUSES = ("completed", "failed")
REPORTABLE_FROM = ("in_progress", "failed")

# STS session-duration bounds. AssumeRole rejects any DurationSeconds below the
# 900s (15-minute) floor; role chaining (Lambda role -> use-case role) caps the
# ceiling at 3600s (1 hour). If the remaining Setup_Token lifetime is under the
# floor the exchange is refused rather than over-issuing (Req 5.3).
STS_MIN_DURATION_SECONDS = 900
STS_MAX_DURATION_SECONDS = 3600

# Audit taxonomy for quick-setup redemptions (Req 8.1, 8.2).
AUDIT_RESOURCE_TYPE = "quick_setup"
RESOURCE_BUNDLE = "bundle"
RESOURCE_CREDENTIALS = "credentials"

# Uniform token errors. The invalid-token body is byte-identical across every
# invalid case (unknown / consumed / invalidated / malformed) so a caller
# cannot distinguish them (Req 3.5). token_expired is deliberately distinct so
# the station can print a regenerate instruction (Req 3.3 / 7.5).
INVALID_TOKEN_ERROR = {
    "error": "invalid_token",
    "message": "The setup token is not valid. Generate a new setup command "
               "from the portal.",
}
TOKEN_EXPIRED_ERROR = {
    "error": "token_expired",
    "message": "The setup token has expired. Generate a new setup command "
               "from the portal.",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": (
        "Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token"
    ),
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Max-Age": "86400",
}


def _registrations_table():
    """The device-registrations DynamoDB table (read lazily for testability)."""
    if not REGISTRATIONS_TABLE:
        raise RuntimeError("REGISTRATIONS_TABLE environment variable is not set")
    return dynamodb.Table(REGISTRATIONS_TABLE)


def _source_ip(event):
    """Source IP of the request (Req 3.9 keying, Req 8.2 audit field)."""
    identity = (event.get("requestContext") or {}).get("identity") or {}
    return identity.get("sourceIp") or "unknown"


def _parse_body(event):
    """Best-effort JSON body parse; {} for missing/invalid bodies."""
    try:
        return json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return {}


def handler(event, context):
    """Route Quick_Setup_Endpoint requests.

    The bootstrap route is public and skips the token pipeline; every other
    route runs through :func:`_token_authenticated_request`.
    """
    try:
        method = event.get("httpMethod")
        path = event.get("path", "") or ""

        if method == "OPTIONS":
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

        if path.endswith("/quick-setup/bootstrap") and method == "GET":
            return get_bootstrap()

        if path.endswith("/quick-setup/bundle") and method == "POST":
            return _token_authenticated_request(
                event, RESOURCE_BUNDLE, get_bundle_manifest
            )

        if path.endswith("/quick-setup/credentials") and method == "POST":
            return _token_authenticated_request(
                event, RESOURCE_CREDENTIALS, exchange_credentials
            )

        if path.endswith("/quick-setup/status") and method == "POST":
            return report_status(event)

        return create_response(404, {"error": "Not found"})

    except Exception:  # pragma: no cover - defensive catch-all
        logger.error("Unhandled error in quick_setup handler", exc_info=True)
        return create_response(500, {"error": "Internal server error"})


def get_bootstrap():
    """Serve the bootstrap script bytes from the artifacts bucket.

    The object key is baked into ``QUICK_SETUP_BOOTSTRAP_KEY`` at deploy time
    so only the most recently deployed bootstrap is ever served. Returned as
    ``text/x-shellscript`` so the one-liner can pipe it to bash. No token is
    required: integrity is guaranteed by the checksum embedded in the
    Setup_Command that fetches it.
    """
    if not PORTAL_ARTIFACTS_BUCKET or not QUICK_SETUP_BOOTSTRAP_KEY:
        logger.error("Bootstrap artifact location is not configured")
        return create_response(500, {"error": "bootstrap_not_configured"})

    try:
        obj = s3.get_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET, Key=QUICK_SETUP_BOOTSTRAP_KEY
        )
        script = obj["Body"].read().decode("utf-8")
    except ClientError:
        logger.error("Bootstrap object could not be read", exc_info=True)
        return create_response(503, {"error": "bootstrap_unavailable"})

    return create_response(
        200, script, headers={"Content-Type": "text/x-shellscript"}
    )


def _token_authenticated_request(event, resource, action):
    """Shared pipeline for token-authenticated Quick_Setup_Endpoint requests.

    Args:
        event: the API Gateway proxy event.
        resource: the redeemed resource label for auditing (``bundle`` or
            ``credentials``), Req 8.2.
        action: ``action(registration, event, now) -> response`` invoked only
            once the presented Setup_Token validates. It returns an API
            Gateway response dict whose status code determines the terminal
            audit outcome.

    Returns:
        An API Gateway response dict.
    """
    now = int(time.time())
    source_ip = _source_ip(event)

    # 1. Invalid-token rate-limit admission check (Req 3.9). A storage failure
    #    means the limiter state cannot be evaluated, so reject rather than
    #    proceed (Req 3.10).
    try:
        allowed = rate_limiter.check(source_ip, now)
    except Exception:
        logger.error("Rate-limit state unavailable for %s", source_ip,
                     exc_info=True)
        return create_response(503, {"error": "rate_limit_unavailable"})
    if not allowed:
        return create_response(429, {
            "error": "rate_limited",
            "message": "Too many invalid setup attempts. Try again later.",
        })

    # Best-effort correlation id for the audit entry: the registration id
    # embedded in the (untrusted) token. Never trusted for authorization.
    body = _parse_body(event)
    token = body.get("token")
    parsed = token_service.parse_token(token) if token else None
    correlation_id = parsed[0] if parsed else "unknown"

    # 2. Strict 'pending' audit entry BEFORE any redemption effect. If it
    #    cannot be recorded, reject and serve nothing (Req 8.4).
    try:
        event_id = record_audit_event_strict(
            user_id=source_ip,
            action=f"redeem_{resource}",
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=correlation_id,
            result="pending",
            details={"resource": resource, "source_ip": source_ip},
        )
    except Exception:
        logger.error("Could not record pending audit entry", exc_info=True)
        return create_response(503, {"error": "audit_unavailable"})

    # 3. Token validation with uniform / distinct errors (Req 3.3, 3.5, 3.10).
    validation = token_service.validate_token(token, now)

    if validation.result == ValidationResult.CHECK_FAILED:
        # A security check could not be evaluated: reject rather than proceed
        # with an unverified token (Req 3.10). Not counted against the source
        # IP (it is not a client-attributable invalid token).
        _finalize_failure(event_id, "check_failed", source_ip, resource)
        return create_response(503, {"error": "token_check_unavailable"})

    if validation.result == ValidationResult.EXPIRED:
        # pending -> expired transition (Req 3.3), count the invalid attempt,
        # and return the distinct token_expired error.
        _expire_pending_registration(validation.registration, now)
        _count_invalid(source_ip, now)
        _finalize_failure(event_id, "token_expired", source_ip, resource,
                          registration=validation.registration)
        return create_response(403, TOKEN_EXPIRED_ERROR)

    if validation.result == ValidationResult.INVALID:
        _count_invalid(source_ip, now)
        _finalize_failure(event_id, "invalid_token", source_ip, resource)
        return create_response(403, INVALID_TOKEN_ERROR)

    # 4. VALID token: run the route action bound to its single registration
    #    (Req 3.7 binding is established by validate_token).
    registration = validation.registration
    response = action(registration, event, now)

    # 5. Finalize the audit entry to its terminal outcome (Req 8.1, 8.2).
    status_code = response.get("statusCode", 500)
    outcome = "success" if 200 <= status_code < 300 else "failure"
    _finalize_redemption(event_id, outcome, source_ip, resource, registration)
    return response


def _count_invalid(source_ip, now):
    """Count an invalid-token attempt against the source IP (Req 3.9).

    Persistence failures inside the limiter are swallowed there; a failure to
    even reach it must not crash the rejection path, so it is logged and
    ignored (the request is already being rejected).
    """
    try:
        rate_limiter.record_invalid(source_ip, now)
    except Exception:
        logger.error("Failed to record invalid-token attempt for %s",
                     source_ip, exc_info=True)


def _expire_pending_registration(registration, now):
    """Transition a `pending` registration to `expired` (Req 3.3).

    Conditional on the status still being `pending`, so a registration in any
    other state is left unchanged. Never fails the caller: the request is
    already being rejected with the expiration error.
    """
    if not registration:
        return
    registration_id = registration.get("registration_id")
    if not registration_id:
        return
    try:
        _registrations_table().update_item(
            Key={"registration_id": registration_id},
            UpdateExpression="SET #s = :expired, updated_at = :now",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":expired": "expired",
                ":pending": "pending",
                ":now": now,
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # Not pending (e.g. already expired / in_progress): leave as-is.
            return
        logger.error("Failed to expire registration %s", registration_id,
                     exc_info=True)


def _finalize_failure(event_id, reason, source_ip, resource, registration=None):
    """Finalize the audit entry for a rejected redemption (Req 3.8, 8.2).

    Records the failure reason, the source IP, and the requested resource.
    Device name / use case are added when a registration is available. Never
    raises: a finalize failure must not turn a clean rejection into a 500.
    """
    details = {
        "resource": resource,
        "source_ip": source_ip,
        "reason": reason,
        "outcome": "failure",
    }
    if registration:
        details["device_name"] = registration.get("device_name")
        details["usecase_id"] = registration.get("usecase_id")
        details["registration_id"] = registration.get("registration_id")
    try:
        finalize_audit_event(event_id, "failure", details=details)
    except Exception:
        logger.error("Failed to finalize failure audit entry %s", event_id,
                     exc_info=True)


def _finalize_redemption(event_id, outcome, source_ip, resource, registration):
    """Finalize the audit entry for a validated redemption (Req 8.1, 8.2).

    Includes device name, use case, requested resource, source IP, and
    outcome. Never raises.
    """
    details = {
        "resource": resource,
        "source_ip": source_ip,
        "outcome": outcome,
        "device_name": registration.get("device_name"),
        "usecase_id": registration.get("usecase_id"),
        "registration_id": registration.get("registration_id"),
    }
    result = "success" if outcome == "success" else "failure"
    try:
        finalize_audit_event(event_id, result, details=details)
    except Exception:
        logger.error("Failed to finalize redemption audit entry %s", event_id,
                     exc_info=True)


# --- Route actions (bodies implemented in their reserved tasks) ----------

def _quick_setup_url(event):
    """Resolve this deployment's Quick_Setup_Endpoint base URL (Req 4.1).

    Uses ``requestContext.domainName`` and ``requestContext.stage`` so the URL
    reflects the deployment that served the request, mirroring the derivation
    used when the Setup_Command is generated (no config circularity). An
    explicit ``QUICK_SETUP_API_URL`` override wins when set. Returns the
    ``…/quick-setup`` endpoint the Setup_Bundle posts back to for credential
    exchange and status reporting.
    """
    if QUICK_SETUP_API_URL:
        base = QUICK_SETUP_API_URL.rstrip("/")
    else:
        request_context = event.get("requestContext") or {}
        domain_name = request_context.get("domainName")
        stage = request_context.get("stage")
        if not domain_name:
            raise ValueError("Cannot resolve API endpoint from request context")
        base = f"https://{domain_name}"
        if stage:
            base = f"{base}/{stage}"

    if base.endswith("/quick-setup"):
        return base
    return f"{base}/quick-setup"


def get_bundle_manifest(registration, event, now):
    """Setup_Bundle manifest for a validated (not consumed) Setup_Token.

    The token has already been validated and bound to exactly this
    registration by the pipeline (Req 3.7); it is deliberately NOT consumed
    here because bundle download is retryable. The manifest carries everything
    the Setup_Bundle needs to run without any operator-supplied values
    (Req 4.1):

        * a 15-minute presigned GET URL for the bundle tarball,
        * ``bundle_sha256`` — the checksum computed at deploy time over the
          exact object served (Req 4.5), so the station can verify integrity
          before executing anything,
        * the per-registration parameters: registration id, device name,
          Device_Group, the Use_Case's AWS region, and this deployment's
          Quick_Setup_Endpoint URL.

    If the bundle object is not present in the artifacts bucket, the request is
    rejected with a 503 and no partial content is served (Req 4.10).
    """
    if not PORTAL_ARTIFACTS_BUCKET or not QUICK_SETUP_BUNDLE_KEY \
            or not QUICK_SETUP_BUNDLE_SHA256:
        logger.error("Bundle artifact location is not configured")
        return create_response(500, {"error": "bundle_not_configured"})

    # Verify the bundle object exists before issuing a presigned URL so we
    # never hand the station a link to a missing/partial artifact (Req 4.10).
    try:
        s3.head_object(
            Bucket=PORTAL_ARTIFACTS_BUCKET, Key=QUICK_SETUP_BUNDLE_KEY
        )
    except ClientError:
        logger.error("Setup_Bundle object is unavailable", exc_info=True)
        return create_response(503, {"error": "bundle_unavailable"})

    # Resolve the Use_Case region for the parameters block (Req 4.1). A lookup
    # failure means we cannot fully parameterize the bundle, so reject.
    try:
        usecase = get_usecase(registration.get("usecase_id"))
        aws_region = get_usecase_region(usecase)
    except Exception:
        logger.error("Could not resolve use case for bundle manifest",
                     exc_info=True)
        return create_response(503, {"error": "usecase_unavailable"})

    try:
        quick_setup_url = _quick_setup_url(event)
    except ValueError:
        logger.error("Could not resolve Quick_Setup_Endpoint URL",
                     exc_info=True)
        return create_response(500, {"error": "endpoint_unresolvable"})

    try:
        bundle_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": PORTAL_ARTIFACTS_BUCKET,
                "Key": QUICK_SETUP_BUNDLE_KEY,
            },
            ExpiresIn=BUNDLE_URL_TTL_SECONDS,
        )
    except ClientError:
        logger.error("Failed to presign Setup_Bundle URL", exc_info=True)
        return create_response(503, {"error": "bundle_unavailable"})

    return create_response(200, {
        "bundle_url": bundle_url,
        "bundle_sha256": QUICK_SETUP_BUNDLE_SHA256,
        "parameters": {
            "registration_id": registration.get("registration_id"),
            "device_name": registration.get("device_name"),
            "device_group": registration.get("device_group"),
            "aws_region": aws_region,
            "quick_setup_url": quick_setup_url,
        },
    })


def exchange_credentials(registration, event, now):
    """Exchange a validated Setup_Token for least-privilege Provisioning_Credentials.

    The token has already been validated and bound to exactly this
    registration by the pipeline (Req 3.7); its ``consumed_at`` is still 0
    (validate_token collapses any consumed token to INVALID). The ordering
    below is what makes the exchange both atomic and safely retryable (Req 5):

        1. Compute the remaining token lifetime. If it is below the 900s STS
           session-duration floor the token is effectively unusable for a
           scoped session, so refuse with the distinct ``token_expired`` error
           (treated as expired) and leave the token unconsumed — the operator
           regenerates and re-runs (Req 5.3).
        2. Mint short-lived credentials via ``sts.assume_role`` against the
           use-case cross-account role, passing the least-privilege session
           policy scoped to the single registered device and
           ``DurationSeconds=min(3600, remaining)`` so the credentials never
           outlive the token (Req 5.1, 5.2, 5.3). Any STS/lookup failure here
           returns an issuance error and leaves the token UNCONSUMED so the
           Setup_Bundle can retry within the remaining lifetime (Req 5.7).
        3. Mint a per-registration ``report_secret`` for status reporting.
        4. Consume the token atomically — the linearization point (Req 5.4,
           5.5): a single conditional ``UpdateItem`` requiring the token hash
           to still match, the token to be unconsumed and unexpired, and the
           status to still be ``pending``; it sets status ``in_progress``,
           stamps ``consumed_at``, and stores only the report-secret hash.
           A ConditionalCheckFailed means a concurrent exchange won the race
           (or the token was invalidated) — discard the minted credentials and
           return the uniform invalid-token error, so at most one issuance
           occurs per token (Req 5.4).

    Secret material (credentials, report_secret) is returned to the station but
    never logged or audited (Req 8.3).
    """
    registration_id = registration.get("registration_id")
    token_hash = registration.get("token_hash")
    expires_at = int(registration.get("token_expires_at", 0) or 0)

    # 1. Remaining lifetime vs the STS floor (Req 5.3).
    remaining = expires_at - int(now)
    if remaining < STS_MIN_DURATION_SECONDS:
        return create_response(403, TOKEN_EXPIRED_ERROR)

    duration = min(STS_MAX_DURATION_SECONDS, remaining)

    # 2. Resolve the use-case cross-account role and mint scoped credentials.
    #    Any failure here leaves the token unconsumed so the exchange can be
    #    retried within the remaining lifetime (Req 5.7).
    try:
        usecase = get_usecase(registration.get("usecase_id"))
        region = get_usecase_region(usecase)
        account_id = usecase.get("account_id")
        role_arn = usecase.get("cross_account_role_arn")
        external_id = usecase.get("external_id")
    except Exception:
        logger.error("Could not resolve use case for credential exchange",
                     exc_info=True)
        return create_response(503, {"error": "credential_issuance_failed"})

    if not role_arn or not account_id:
        logger.error("Use case is missing role/account configuration")
        return create_response(503, {"error": "credential_issuance_failed"})

    # Same-account use cases store the account root ARN as cross_account_role_arn
    # (arn:aws:iam::ACCOUNT:root), which cannot be assumed. Redirect to the
    # dedicated station-provisioning role in the portal account (trusted only by
    # this Lambda); the per-device session policy below still scopes the result.
    # The account root has no external id, so none is presented.
    if role_arn.rstrip("/").endswith(":root"):
        if not QUICK_SETUP_PROVISIONING_ROLE_ARN:
            logger.error(
                "Same-account use case but QUICK_SETUP_PROVISIONING_ROLE_ARN is "
                "not configured; cannot mint provisioning credentials"
            )
            return create_response(503, {"error": "credential_issuance_failed"})
        role_arn = QUICK_SETUP_PROVISIONING_ROLE_ARN
        external_id = None

    policy = build_session_policy(
        registration.get("device_name"),
        registration.get("device_group"),
        region,
        account_id,
    )

    assume_params = {
        "RoleArn": role_arn,
        "RoleSessionName": f"dda-qs-{registration_id}"[:64],
        "Policy": json.dumps(policy),
        "DurationSeconds": duration,
    }
    if external_id:
        assume_params["ExternalId"] = external_id

    try:
        assume_response = sts.assume_role(**assume_params)
        credentials = assume_response["Credentials"]
    except (ClientError, KeyError):
        logger.error("STS assume_role failed for credential exchange",
                     exc_info=True)
        return create_response(502, {"error": "credential_issuance_failed"})

    # 3. Mint the per-registration report secret (Req 6 status reporting).
    report_secret = secrets.token_urlsafe(32)
    report_secret_hash = hashlib.sha256(
        report_secret.encode("utf-8")
    ).hexdigest()

    # 4. Consume the token atomically — the linearization point (Req 5.4/5.5).
    try:
        _registrations_table().update_item(
            Key={"registration_id": registration_id},
            UpdateExpression=(
                "SET #s = :inprogress, consumed_at = :now, "
                "report_secret_hash = :rsh, updated_at = :now"
            ),
            ConditionExpression=(
                "token_hash = :th AND consumed_at = :never "
                "AND token_expires_at > :now AND #s = :pending"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":inprogress": "in_progress",
                ":pending": "pending",
                ":th": token_hash,
                ":never": 0,
                ":now": int(now),
                ":rsh": report_secret_hash,
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # A concurrent exchange consumed the token first, or it was
            # invalidated between validation and consume: discard the minted
            # credentials and return the uniform invalid-token error (Req 5.4).
            return create_response(403, INVALID_TOKEN_ERROR)
        # Any other storage failure means the token was NOT consumed; report an
        # issuance failure so the Setup_Bundle can retry (Req 5.7).
        logger.error("Failed to consume Setup_Token during credential exchange",
                     exc_info=True)
        return create_response(503, {"error": "credential_issuance_failed"})

    # 5. Return the credentials, report secret, and region. Expiration is a
    #    datetime from STS; serialize to ISO-8601 for the JSON body.
    expiration = credentials.get("Expiration")
    return create_response(200, {
        "credentials": {
            "access_key_id": credentials["AccessKeyId"],
            "secret_access_key": credentials["SecretAccessKey"],
            "session_token": credentials["SessionToken"],
            "expiration": expiration.isoformat() if expiration else None,
        },
        "report_secret": report_secret,
        "aws_region": region,
    })


def _record_target_architecture(device_name, usecase_id, target_architecture, now):
    """Best-effort upsert of the reported DDA Target_Architecture on the Devices
    table (device-arch-compatibility Req 2.2, 2.6).

    Called only AFTER the authenticated conditional transition to ``completed``
    has succeeded and only for a value already verified to be in the fixed set,
    so this write is never part of the registration authentication or transition
    (Req 2.4, 2.5). Keyed by ``device_id`` = IoT Thing name to match
    ``load_device_gate_info`` / the deployment architecture gate.

    Never raises: the registration is already completed, so a Devices-table
    failure is logged and swallowed and the completion still returns 200
    (Req 2 non-destructive). An audit event is recorded on a successful write
    (Req 2.6).
    """
    if not DEVICES_TABLE or not device_name:
        return
    try:
        dynamodb.Table(DEVICES_TABLE).update_item(
            Key={"device_id": device_name},
            UpdateExpression=(
                "SET target_architecture = :ta, usecase_id = :uid, "
                "updated_at = :now, updated_by = :by"
            ),
            ExpressionAttributeValues={
                ":ta": target_architecture,
                ":uid": usecase_id,
                ":now": now,
                ":by": "quick-setup",
            },
        )
    except Exception:
        # A Devices-table failure must not turn a successful completion into an
        # error; the device can still be corrected via the manual admin writer.
        logger.error(
            "Failed to record target_architecture for device %s", device_name,
            exc_info=True,
        )
        return

    # Audit the recording with the device name, the recorded value, and the
    # outcome (Req 2.6). Consistent with the feature's audit taxonomy; failures
    # here are logged and swallowed.
    try:
        log_audit_event(
            user_id="quick-setup",
            action="record_target_architecture",
            resource_type="device",
            resource_id=device_name,
            result="success",
            details={
                "target_architecture": target_architecture,
                "outcome": "success",
            },
        )
    except Exception:
        logger.error(
            "Failed to audit target_architecture recording for device %s",
            device_name, exc_info=True,
        )


def report_status(event):
    """Station status report authenticated by the per-registration report secret.

    Body: ``{"registration_id", "report_secret", "status", "error_summary"?}``
    where ``status`` is ``"completed"`` or ``"failed"``.

    The report secret is minted during the credential exchange
    (:func:`exchange_credentials`) and only its SHA-256 hash is stored, so the
    Setup_Bundle authenticates by presenting the raw secret which is compared
    against the stored hash in constant time (Req 6.7). A report is accepted
    only from an ``in_progress`` registration (the first report) or a
    ``failed`` one (an idempotent re-report after a failed run); it drives the
    registration to ``completed`` (Req 6.1) or ``failed`` (Req 6.2), storing
    the error summary truncated to at most 1024 characters on failure.

    Every rejection — an unknown registration id, a wrong/absent report
    secret, or a target whose status is already ``completed`` (or otherwise
    not reportable) — returns the single uniform error with the registration
    left unchanged (Req 6.7), so a caller cannot distinguish these cases. The
    conditional ``UpdateItem`` re-checks the secret hash and source state
    server-side, making the transition atomic against a concurrent report.
    """
    now = int(time.time())
    body = _parse_body(event)

    registration_id = body.get("registration_id")
    report_secret = body.get("report_secret")
    status = body.get("status")
    error_summary = body.get("error_summary")
    # Optional DDA Target_Architecture reported on a completed run
    # (device-arch-compatibility Req 2). Recorded only after the authenticated
    # transition succeeds and only when it is a member of the fixed set.
    target_architecture = body.get("target_architecture")

    # Contract validation: the reported status must be one of the two terminal
    # values. This is a malformed-request error, distinct from the uniform
    # authentication/state rejection below.
    if status not in REPORT_STATUSES:
        return create_response(400, {
            "error": "invalid_status",
            "message": "status must be 'completed' or 'failed'.",
        })

    # Authentication material must be present and well-typed; anything missing
    # collapses to the uniform rejection so absent fields are indistinguishable
    # from a wrong secret or unknown registration (Req 6.7).
    if not isinstance(registration_id, str) or not registration_id \
            or not isinstance(report_secret, str) or not report_secret:
        return create_response(403, INVALID_TOKEN_ERROR)

    # Load the target registration. A storage failure means we cannot verify
    # the report, so reject rather than mutate on an unverified basis.
    try:
        response = _registrations_table().get_item(
            Key={"registration_id": registration_id}
        )
        registration = response.get("Item")
    except ClientError:
        logger.error("Could not load registration for status report",
                     exc_info=True)
        return create_response(503, {"error": "status_report_unavailable"})

    # Unknown registration, or a registration with no report secret yet (its
    # credentials were never exchanged): uniform rejection, unchanged.
    stored_hash = registration.get("report_secret_hash") if registration else None
    if not stored_hash:
        return create_response(403, INVALID_TOKEN_ERROR)

    # Constant-time comparison of sha256(report_secret) against the stored hash
    # (Req 6.7). A mismatch is the uniform rejection.
    presented_hash = hashlib.sha256(
        report_secret.encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(presented_hash, str(stored_hash)):
        return create_response(403, INVALID_TOKEN_ERROR)

    # Only accept from an in_progress or failed registration. A completed (or
    # pending/expired) target is left unchanged (Req 6.7).
    current_status = registration.get("status")
    if current_status not in REPORTABLE_FROM:
        return create_response(403, INVALID_TOKEN_ERROR)

    # Atomic transition: re-check the secret hash and source state server-side
    # so a concurrent report (or a status change between our read and write)
    # cannot produce a lost update. The error summary is stored, truncated to
    # 1024 chars, only on a failure report (Req 6.2).
    values = {
        ":new": status,
        ":now": now,
        ":rsh": str(stored_hash),
        ":inprogress": "in_progress",
        ":failed": "failed",
    }
    update_expression = "SET #s = :new, updated_at = :now"
    if status == "failed":
        summary = error_summary if isinstance(error_summary, str) else ""
        values[":err"] = summary[:ERROR_SUMMARY_MAX_CHARS]
        update_expression += ", error_summary = :err"

    try:
        _registrations_table().update_item(
            Key={"registration_id": registration_id},
            UpdateExpression=update_expression,
            ConditionExpression=(
                "report_secret_hash = :rsh "
                "AND (#s = :inprogress OR #s = :failed)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # The secret no longer matches or the registration is no longer in
            # a reportable state (e.g. a concurrent report completed it):
            # uniform rejection, unchanged (Req 6.7).
            return create_response(403, INVALID_TOKEN_ERROR)
        logger.error("Failed to persist reported status", exc_info=True)
        return create_response(503, {"error": "status_report_unavailable"})

    # The authenticated transition succeeded. On a completed run carrying a
    # fixed-set Target_Architecture, best-effort record it on the Devices table
    # (Req 2.2). An out-of-set value is ignored — no write — and completion is
    # otherwise unchanged (Req 2.3); a missing value leaves the device's
    # architecture untouched (Req 2.4). The write never fails the completion.
    if status == "completed" and target_architecture in TARGET_ARCHITECTURES:
        _record_target_architecture(
            registration.get("device_name"),
            registration.get("usecase_id"),
            target_architecture,
            now,
        )

    return create_response(200, {
        "registration_id": registration_id,
        "status": status,
    })
