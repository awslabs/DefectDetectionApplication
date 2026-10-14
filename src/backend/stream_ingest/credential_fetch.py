#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Fetching Portal-managed Stream_Credentials (rtsp-rtmp-stream-cameras
Requirements 5.5, 5.6, 6.7; design components 11 and 12).

A Portal change to a stream camera carries a Credential_Reference,
``{secretArn, versionId}``, never the credentials. :func:`fetch` reads that
exact secret version with the device's own AWS credentials (the Greengrass
token exchange service, the ``camera_sync/pin_worker.py`` pattern), which
can read only the secrets stored for this device's thing name.

A failure raises :class:`CredentialFetchError` whose message is
``credential retrieval failed: <AWS error code>``: it never holds a secret
value, a secret name beyond the reference the Portal already knows, or a
request payload, so it can travel as the change's failure reason.

A denied fetch (:data:`RETRYABLE_REASONS`) is what a device read grant that
has not propagated yet returns, so the Edge_Sync_Agent retries it before the
change fails (``CredentialFetchError.retryable``; Requirement 5.6, finding 21).
"""
import json
import re
from typing import Any, Callable, Dict, Mapping, Optional

from model import stream_source

#: botocore timeouts: the agent applies changes on its own thread, and a
#: hung endpoint must fail the change rather than stall the next one.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 10

_SECRET_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:secretsmanager:(?P<region>[a-z0-9-]+):\d{12}:secret:[A-Za-z0-9/_+=.@-]+$")
_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

#: The error codes of a denied fetch. The Portal writes the device read grant
#: about a second before the device fetches, and IAM can take longer than
#: that to apply a new policy, so these are retried; every other failure is
#: final (design component 12, **Denied credential fetch**).
RETRYABLE_REASONS = frozenset({"AccessDeniedException", "AccessDenied"})


class CredentialFetchError(Exception):
    """The credentials could not be retrieved; the message holds no secret."""

    def __init__(self, reason: str):
        super().__init__(f"credential retrieval failed: {reason}")
        self.reason = reason

    @property
    def retryable(self) -> bool:
        """Whether the fetch was denied, and is worth retrying."""
        return self.reason in RETRYABLE_REASONS


def parse_reference(credential_ref: Any) -> Dict[str, str]:
    """The ``{secretArn, versionId, region}`` of a Credential_Reference."""
    if not isinstance(credential_ref, Mapping):
        raise CredentialFetchError("InvalidReference")
    arn, version = credential_ref.get("secretArn"), credential_ref.get("versionId")
    match = _SECRET_ARN_RE.match(arn) if isinstance(arn, str) else None
    if match is None or not (isinstance(version, str) and _VERSION_ID_RE.match(version)):
        raise CredentialFetchError("InvalidReference")
    return {"secretArn": arn, "versionId": version, "region": match.group("region")}


def _default_client(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("secretsmanager", region_name=region, config=Config(
        connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S, retries={"max_attempts": 2}))


def _error_code(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str) and re.match(r"^[A-Za-z0-9.]{1,64}$", code):
            return code
    return type(error).__name__


def fetch(credential_ref: Any, client_factory: Optional[Callable[[str], Any]] = None) -> Dict[str, str]:
    """The Stream_Credentials the reference names: ``username``,
    ``password`` and ``urlSecret``, whichever the secret holds."""
    reference = parse_reference(credential_ref)
    try:
        client = (client_factory or _default_client)(reference["region"])
        response = client.get_secret_value(SecretId=reference["secretArn"], VersionId=reference["versionId"])
    except Exception as error:  # noqa: BLE001 - reduced to the error code
        raise CredentialFetchError(_error_code(error)) from None
    secret = response.get("SecretString") if isinstance(response, Mapping) else None
    try:
        document = json.loads(secret) if isinstance(secret, str) else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise CredentialFetchError("MalformedSecret")
    try:
        credentials = stream_source.validate_credentials(document)
    except stream_source.StreamSourceError:
        raise CredentialFetchError("MalformedSecret") from None
    if not credentials:
        raise CredentialFetchError("EmptySecret")
    return credentials
