"""
Stream_Credentials storage for the Camera_Registry
(rtsp-rtmp-stream-cameras task 8.2 — Reqs 5.3, 5.4, 5.8, 6.6).

Portal-managed stream camera credentials never travel in the registry, in
the ``dda-camera-registry`` shadow, or in an audit event: they are written
to the Credential_Vault (AWS Secrets Manager) of the device's
Use_Case_Account, and only the Credential_Reference
(``{secretArn, versionId}``) is delivered to the device, which reads the
value itself with its own token-exchange credentials (Reqs 5.3, 5.5).

This module owns every Credential_Vault interaction of that flow, in the
order design component 7 gives:

``ensure_device_read_grant``
    Idempotently puts the inline policy ``DDAStreamCameraCredentialRead``
    on the device token-exchange role, granting
    ``secretsmanager:GetSecretValue`` on the secrets of *that thing only*
    through the ``${credentials-iot:ThingName}`` policy variable the AWS
    IoT credentials provider sets (Req 6.7). Modelled on the tuning grant
    in ``deployments.py``: an inline policy this feature owns exclusively,
    so it adds no other permission and can never disturb the device
    role's other policies.

``store_stream_credentials``
    ``CreateSecret`` for a camera with no secret yet, ``PutSecretValue``
    for one that already has one, tagged with the Use_Case, device, and
    Camera_Source (design "Credential_Vault secret"). It returns the
    ``{secretArn, versionId}`` the Credential_Reference carries, plus the
    two facts ``withdraw_stream_credentials`` needs to undo it.

``withdraw_stream_credentials``
    The Requirement 5.4 rollback, run when the desired-change write fails
    *after* the credentials were stored: a secret this request created is
    force-deleted, and a new version of an existing secret has
    ``AWSCURRENT`` moved back to the version that held it, so nothing
    references the new version. A secret the request restored from a
    scheduled deletion is scheduled for deletion again.

``schedule_secret_deletion``
    Requirement 5.8's scheduled deletion, for ``clearCredentials`` and for
    the deletion of a stream camera, run *after* the change is delivered
    so a delivery failure never destroys a secret the device still uses.
    Given ``secret_ids``, it schedules exactly those secrets.

``device_secret_id``, ``secret_scope`` and ``CameraIdCannotHoldCredentials``
    The camera's own secret (task 29, finding 22; design component 7,
    "Secret record"). The Camera_Registry records the ARN of the secret a
    camera got, because the device re-keys a Portal create to an id of
    its own, and resolves a camera's secrets from that record, the pending
    and the reported Credential_Reference, and the name of its id.
    ``device_secret_id`` accepts a candidate only when it names
    ``dda-portal/stream-camera-credentials/{device_id}/`` plus one path
    segment, in the use case's account and region (``secret_scope``), so
    no device can point the Portal at another device's secret. An update
    of a camera whose id cannot name a secret, and that has none,
    raises ``CameraIdCannotHoldCredentials`` with nothing written.

The Portal holds create/update/delete permission on these secrets but not
``GetSecretValue`` (Req 6.6), so nothing here ever reads a value back;
credential material only ever flows *into* this module, from the
write-only top-level ``credentials`` object of a create or update body.
No function in this module logs a credential value, or a message derived
from one — failures are logged with the secret *name* only.
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import ClientError

from shared_utils import get_usecase_client, get_usecase_region

logger = logging.getLogger()

# ---------------------------------------------------------------------------
# Credential_Vault layout (design "Credential_Vault secret")
# ---------------------------------------------------------------------------

#: Secret name prefix. One secret per (device, Camera_Source):
#: ``dda-portal/stream-camera-credentials/{device_id}/{camera_source_id}``.
#: The device-read grant is scoped to the thing's own segment of this
#: prefix, so the layout *is* the isolation boundary (Req 6.7).
SECRET_NAME_PREFIX = 'dda-portal/stream-camera-credentials'

#: The fields a Stream_Credentials value may carry, in the order the
#: design lists them. ``urlSecret`` is the RTMP URL secret suffix (a
#: stream key), kept out of the Stream_URL itself (Req 2.2).
CREDENTIAL_FIELDS = ('username', 'password', 'urlSecret')

#: Length bound on a single credential field. Secrets Manager accepts a
#: 64 KB value; this bound keeps a request from turning the vault into
#: general-purpose storage while being far above any real camera
#: credential.
MAX_CREDENTIAL_FIELD_LENGTH = 1024

#: Tags every secret carries, so a Use_Case's camera credentials can be
#: inventoried and cleaned up without reading any value.
TAG_USECASE_ID = 'dda-portal:usecase_id'
TAG_DEVICE_ID = 'dda-portal:device_id'
TAG_CAMERA_SOURCE_ID = 'dda-portal:camera_source_id'

#: Recovery window of a scheduled deletion (Req 5.8). A deletion is
#: recoverable for a week, so an operator who cleared or deleted a camera
#: by mistake can still restore it.
DELETION_RECOVERY_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# Device read grant (Reqs 6.6, 6.7)
# ---------------------------------------------------------------------------

#: The Greengrass token-exchange role devices assume, created by
#: station_install/setup_station.sh in the Use_Case account — the same
#: role (and the same environment override) the tuning grant uses.
DEVICE_ROLE_NAME = os.environ.get('DEVICE_TOKEN_EXCHANGE_ROLE',
                                  'GreengrassV2TokenExchangeRole')

#: Inline policy this feature owns on that role. Nothing else writes it,
#: so it can be replaced wholesale without touching any other device
#: permission.
DEVICE_POLICY_NAME = 'DDAStreamCameraCredentialRead'

#: The policy variable the AWS IoT credentials provider sets from the
#: thing-name header. If it is absent the grant fails closed — no device
#: can read any secret — which surfaces as a clear apply failure on the
#: device rather than as over-broad access (design component 7).
THING_NAME_VARIABLE = '${credentials-iot:ThingName}'

#: Error codes that mean "the Use_Case_Account does not grant the Portal
#: this capability" (Req 5.9). Anything else is a real failure.
ACCESS_DENIED_CODES = frozenset({
    'AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation',
    'AuthorizationError', 'UnrecognizedClientException',
})


class CredentialStorageUnavailable(Exception):
    """The Use_Case_Account does not grant the Portal the capability
    needed to manage Stream_Credentials (Req 5.9).

    Carries the missing capability by name so the route can name it in
    its rejection, and never carries credential material.
    """

    def __init__(self, capability: str, detail: Optional[str] = None):
        self.capability = capability
        self.detail = detail
        super().__init__(
            f"The use-case account does not grant the Portal {capability}"
            + (f": {detail}" if detail else ''))


class CameraIdCannotHoldCredentials(Exception):
    """An update would have to create a secret for a camera whose id cannot
    name one: ``secret_name(device_id, csid)`` is not the device's prefix
    plus one valid segment, and none of the camera's resolved secrets
    exists (task 29, design component 7 "Update with credentials").

    Raised before anything is written to the Credential_Vault. The message
    is fixed and names no id, so it can travel into a response or a log
    as it is; the route turns it into a 400 with ``field:
    camera_source_id``.
    """

    MESSAGE = 'this camera id cannot hold Portal-managed credentials'

    def __init__(self):
        super().__init__(self.MESSAGE)


# ---------------------------------------------------------------------------
# Naming and request-body helpers
# ---------------------------------------------------------------------------

def secret_name(device_id: str, csid: str) -> str:
    """The Credential_Vault secret name of one Camera_Source."""
    return f"{SECRET_NAME_PREFIX}/{device_id}/{csid}"


#: A complete Secrets Manager secret ARN: the region, the account, and the
#: name followed by the six-character suffix Secrets Manager appends
#: (task 29, design component 7 "Secret record"). Applied with
#: ``fullmatch`` only: ``$`` also matches before a final newline.
_SECRET_ARN = re.compile(
    r"arn:aws[a-z-]*:secretsmanager:(?P<region>[a-z0-9-]+):(?P<account>\d{12})"
    r":secret:(?P<name>[A-Za-z0-9/_+=.@-]+)-[A-Za-z0-9]{6}")
#: A secret name's characters, without '/': one path segment.
_NAME_SEGMENT = re.compile(r"[A-Za-z0-9_+=.@-]+")


def device_secret_id(candidate: Any, device_id: str,
                     account_id: Optional[str] = None,
                     region: Optional[str] = None, *,
                     derived: bool = False) -> Optional[str]:
    """The SecretId to call Secrets Manager with for one candidate of a
    camera's secrets, or None when the candidate is not one of this
    device's secrets (task 29, design component 7 "Secret record").

    Candidates 1-3 (the record, the pending and the reported
    Credential_Reference) count only as complete ARNs of ``account_id``
    and ``region``. Candidate 4 (``derived=True``) is the name
    ``secret_name()`` built, and reads no account or region. Either way
    the name must be ``dda-portal/stream-camera-credentials/{device_id}/``
    plus one segment, so a device can never point the Portal at another
    device's secret: the prefix ends in '/', so ``dev1`` never matches the
    secrets of ``dev1-b``.
    """
    if derived:                       # candidate 4; reads no account or region
        name = secret_id = candidate
    else:                             # candidates 1-3: complete ARNs only
        match = _SECRET_ARN.fullmatch(candidate) \
            if isinstance(candidate, str) else None
        if not match or match["account"] != account_id \
                or match["region"] != region:
            return None
        name, secret_id = match["name"], candidate
    prefix = f"{SECRET_NAME_PREFIX}/{device_id}/"
    rest = name[len(prefix):] if name.startswith(prefix) else ""
    return secret_id if _NAME_SEGMENT.fullmatch(rest) else None


def secret_id_name(secret_id: str) -> str:
    """The secret name a SecretId stands for: the name of a complete ARN,
    otherwise the id itself (a name)."""
    match = _SECRET_ARN.fullmatch(secret_id) \
        if isinstance(secret_id, str) else None
    return match["name"] if match else secret_id


def validate_credentials_request(body: Any) -> Optional[Tuple[str, str]]:
    """Validate the write-only ``credentials`` object and
    ``clearCredentials`` flag of a create/update body.

    Returns ``(field, message)`` for a rejection, or ``None`` when the
    body is acceptable. The message names the field and what is wrong
    with it, never the offending *value*, so a rejection can never echo
    credential material into a response or a log (Req 6.1). Keys are
    examined in sorted order, so the reported field is a function of the
    body rather than of dict ordering.

    Callers apply this to stream Camera_Source bodies only: a body of any
    other type keeps exactly the validation it had (Req 18.3).
    """
    if not isinstance(body, dict):
        return None  # the caller's own body-shape check owns this
    clear = body.get('clearCredentials')
    if 'clearCredentials' in body and not isinstance(clear, bool):
        return ('clearCredentials', 'clearCredentials must be a boolean')

    if 'credentials' not in body or body.get('credentials') is None:
        return None
    credentials = body['credentials']
    if not isinstance(credentials, dict):
        return ('credentials', 'credentials must be an object')

    supplied = 0
    for key in sorted(credentials, key=str):
        if key not in CREDENTIAL_FIELDS:
            return (f'credentials.{key}',
                    f"credentials.{key} is not a stream camera credential "
                    f"field; the accepted fields are "
                    f"{', '.join(CREDENTIAL_FIELDS)}")
        value = credentials[key]
        if value is None:
            continue
        if not isinstance(value, str):
            return (f'credentials.{key}',
                    f"credentials.{key} must be a string")
        if not value:
            return (f'credentials.{key}',
                    f"credentials.{key} must not be empty; omit it or send "
                    "null to leave it unset")
        if len(value) > MAX_CREDENTIAL_FIELD_LENGTH:
            return (f'credentials.{key}',
                    f"credentials.{key} must be at most "
                    f"{MAX_CREDENTIAL_FIELD_LENGTH} characters")
        supplied += 1
    if supplied == 0:
        return ('credentials',
                "credentials must carry at least one of "
                f"{', '.join(CREDENTIAL_FIELDS)}")
    if clear is True:
        return ('clearCredentials',
                'clearCredentials cannot be combined with credentials in the '
                'same request')
    return None


def credentials_from_body(body: Any) -> Optional[Dict[str, str]]:
    """The Stream_Credentials a body carries, or ``None``.

    Only the known fields, and only those with a value: the design's
    "with absent fields omitted". Assumes
    :func:`validate_credentials_request` already accepted the body.
    """
    if not isinstance(body, dict):
        return None
    credentials = body.get('credentials')
    if not isinstance(credentials, dict):
        return None
    material = {key: credentials[key] for key in CREDENTIAL_FIELDS
                if isinstance(credentials.get(key), str) and credentials[key]}
    return material or None


def clear_requested(body: Any) -> bool:
    """Whether the body asks for the camera's credentials to be cleared."""
    return isinstance(body, dict) and body.get('clearCredentials') is True


def credential_reference(stored: Optional[Dict[str, Any]]
                         ) -> Optional[Dict[str, str]]:
    """The Credential_Reference of a :func:`store_stream_credentials`
    result: exactly ``{secretArn, versionId}`` and nothing else, so the
    rollback bookkeeping never leaks into the registry item, the desired
    change, or the device's inventory."""
    if not stored:
        return None
    return {'secretArn': stored['secretArn'], 'versionId': stored['versionId']}


def device_policy_document(region: str, account_id: str) -> Dict[str, Any]:
    """The inline device-role policy of Requirement 6.7: read access to
    the Credential_Vault secrets stored for *this thing* and nothing
    else.

    The trailing wildcard covers both the Camera_Source segment of the
    name and the six-character suffix Secrets Manager appends to every
    secret ARN.
    """
    return {
        'Version': '2012-10-17',
        'Statement': [{
            'Sid': DEVICE_POLICY_NAME,
            'Effect': 'Allow',
            'Action': ['secretsmanager:GetSecretValue'],
            'Resource': (f"arn:aws:secretsmanager:{region}:{account_id}"
                         f":secret:{SECRET_NAME_PREFIX}/"
                         f"{THING_NAME_VARIABLE}/*"),
        }],
    }


# ---------------------------------------------------------------------------
# Clients and error classification
# ---------------------------------------------------------------------------

def _error_code(error: Exception) -> Optional[str]:
    if isinstance(error, ClientError):
        return (error.response.get('Error') or {}).get('Code')
    return None


def _is_access_denied(error: Exception) -> bool:
    return _error_code(error) in ACCESS_DENIED_CODES


def _client(service: str, usecase: Dict[str, Any],
            region: Optional[str] = None, session_name: Optional[str] = None):
    """A client for the device's Use_Case_Account: the assumed
    cross-account role in a multi-account setup, the Lambda's own
    credentials in a single-account one (the shared
    ``get_usecase_client`` contract every other delivery path uses)."""
    usecase = usecase or {}
    return get_usecase_client(service, usecase, session_name=session_name,
                              region=region or get_usecase_region(usecase))


def _usecase_account_id(usecase: Dict[str, Any],
                        region: Optional[str] = None) -> str:
    """The Use_Case_Account id the secret ARNs are built from.

    The recorded ``account_id`` of the Use_Case, falling back to the
    identity of the credentials the Portal would use to write the secret
    — which is the Use_Case_Account in both setups (the assumed role in a
    multi-account one, the Portal's own account in a single-account one,
    where they are the same account).
    """
    account_id = (usecase or {}).get('account_id')
    if account_id:
        return str(account_id)
    return _client('sts', usecase, region=region).get_caller_identity()[
        'Account']


def secret_scope(usecase: Dict[str, Any]) -> Tuple[str, str]:
    """``(account_id, region)`` of the Use_Case_Account, which every
    recorded, pending or reported secret ARN must name (task 29, design
    component 7 "Secret record").

    Resolved lazily, once per request, and only by a request that needs a
    camera's secrets. ``AccessDenied*`` raises
    :class:`CredentialStorageUnavailable`, the route's existing 409; any
    other error propagates.
    """
    region = get_usecase_region(usecase or {})
    try:
        account_id = _usecase_account_id(usecase, region=region)
    except Exception as e:  # noqa: BLE001 — classified here
        if _is_access_denied(e):
            raise CredentialStorageUnavailable(
                'permission to store stream camera credentials',
                str(_error_code(e))) from e
        raise
    return account_id, region


# ---------------------------------------------------------------------------
# Step 2: the device read grant
# ---------------------------------------------------------------------------

def ensure_device_read_grant(usecase: Dict[str, Any],
                             region: Optional[str] = None,
                             session_name: Optional[str] = None
                             ) -> Dict[str, Any]:
    """Idempotently grant the device token-exchange role read access to
    its own stream camera secrets (Reqs 6.6, 6.7).

    Reads the existing inline policy first and rewrites it only when it
    differs, so a steady-state create or update makes one read call and no
    write. Returns ``{'status': 'unchanged' | 'granted' | 'failed', ...}``.

    Raises :class:`CredentialStorageUnavailable` when the Use_Case_Account
    denies the Portal the IAM calls, so the route can reject the request
    before any credential is stored (Req 5.9). Every *other* failure —
    most importantly a token-exchange role that does not exist yet — is
    logged and reported as ``failed`` without raising: the grant is
    repaired on the next credentialed write (or by hand), and a device
    that cannot read a reference reports the change as failed with a
    secret-free reason (Req 5.6), which is a far better outcome than
    refusing to store credentials at all.
    """
    resolved_region = region or get_usecase_region(usecase or {})
    try:
        account_id = _usecase_account_id(usecase, region=resolved_region)
        document = device_policy_document(resolved_region, account_id)
        iam_client = _client('iam', usecase, region=resolved_region,
                             session_name=session_name)
    except Exception as e:  # noqa: BLE001 — classified below
        if _is_access_denied(e):
            raise CredentialStorageUnavailable(
                'permission to grant devices read access to stream camera '
                'credentials', str(_error_code(e))) from e
        logger.warning(
            f"Could not prepare the {DEVICE_POLICY_NAME} grant on "
            f"{DEVICE_ROLE_NAME}: {e}")
        return {'status': 'failed', 'role_name': DEVICE_ROLE_NAME,
                'policy_name': DEVICE_POLICY_NAME, 'error': str(e)}

    try:
        try:
            existing = iam_client.get_role_policy(
                RoleName=DEVICE_ROLE_NAME, PolicyName=DEVICE_POLICY_NAME)
            current = existing.get('PolicyDocument')
            if isinstance(current, str):
                current = json.loads(current)
            if current == document:
                return {'status': 'unchanged', 'role_name': DEVICE_ROLE_NAME,
                        'policy_name': DEVICE_POLICY_NAME}
        except ClientError as e:
            if _is_access_denied(e):
                raise
            # No such policy (or an unreadable one): write ours below.
        except (TypeError, ValueError):
            pass
        iam_client.put_role_policy(
            RoleName=DEVICE_ROLE_NAME, PolicyName=DEVICE_POLICY_NAME,
            PolicyDocument=json.dumps(document))
        logger.info(
            f"Granted {DEVICE_POLICY_NAME} "
            f"(secretsmanager:GetSecretValue on {SECRET_NAME_PREFIX}/"
            f"<thing>/*) to {DEVICE_ROLE_NAME}")
        return {'status': 'granted', 'role_name': DEVICE_ROLE_NAME,
                'policy_name': DEVICE_POLICY_NAME}
    except Exception as e:  # noqa: BLE001 — classified above
        if _is_access_denied(e):
            raise CredentialStorageUnavailable(
                'permission to grant devices read access to stream camera '
                'credentials', str(_error_code(e))) from e
        logger.warning(
            f"Could not write {DEVICE_POLICY_NAME} on {DEVICE_ROLE_NAME}: {e}")
        return {'status': 'failed', 'role_name': DEVICE_ROLE_NAME,
                'policy_name': DEVICE_POLICY_NAME, 'error': str(e)}


# ---------------------------------------------------------------------------
# Step 3: storing the credentials
# ---------------------------------------------------------------------------

def _secret_string(credentials: Dict[str, str]) -> str:
    """The secret value: the supplied fields only, in the design's order,
    so an unchanged credential set produces a byte-identical value."""
    return json.dumps({key: credentials[key] for key in CREDENTIAL_FIELDS
                       if key in credentials})


def _secret_tags(usecase_id: Optional[str], device_id: str,
                 csid: str) -> list:
    return [{'Key': TAG_USECASE_ID, 'Value': str(usecase_id or '')},
            {'Key': TAG_DEVICE_ID, 'Value': device_id},
            {'Key': TAG_CAMERA_SOURCE_ID, 'Value': csid}]


def _current_version_id(description: Dict[str, Any]) -> Optional[str]:
    """The version ``AWSCURRENT`` points at, from a DescribeSecret."""
    stages = description.get('VersionIdsToStages') or {}
    for version_id, version_stages in stages.items():
        if 'AWSCURRENT' in (version_stages or []):
            return version_id
    return None


def store_stream_credentials(usecase: Dict[str, Any], device_id: str,
                             csid: str, credentials: Dict[str, str],
                             secret_ids: Sequence[str] = (), *,
                             create: bool,
                             region: Optional[str] = None,
                             session_name: Optional[str] = None
                             ) -> Dict[str, Any]:
    """Write one Camera_Source's Stream_Credentials to the
    Credential_Vault of the device's Use_Case_Account (Reqs 5.3, 5.8).

    ``CreateSecret`` for a camera with no secret yet, ``PutSecretValue``
    for one that has (Req 5.8's "write a new secret version"); a secret
    left pending deletion by an earlier ``clearCredentials`` or delete is
    restored first, so re-adding credentials to a camera works inside the
    recovery window.

    Which secret is the camera's (task 29, design component 7 "Update
    with credentials"):

    - ``create=True``, a create: ``secret_name(device_id, csid)`` alone
      is described, written into when it exists, and created otherwise.
      ``secret_ids`` is not used.
    - ``create=False``, an update: ``secret_ids`` are the camera's
      resolved SecretIds, in order (``camera_registry
      .credential_secret_ids``), which end with the derived name whenever
      ``device_secret_id`` accepts it. They are described in order, and
      the first that exists is restored, written into and re-tagged. When
      none exists, ``secret_name(device_id, csid)`` is created, but only
      when ``device_secret_id(..., derived=True)`` accepts it; otherwise
      :class:`CameraIdCannotHoldCredentials` is raised before anything is
      written.

    Either way the secret written is re-tagged with the given ``csid``, so
    ``dda-portal:camera_source_id`` names the registry entry while the
    secret keeps the name it was created under.

    Returns ``{'secretArn', 'versionId', 'created', 'previousVersionId',
    'restoredFromDeletion'}``: the first two are the Credential_Reference
    the change delivers (see :func:`credential_reference`), the other
    three are what :func:`withdraw_stream_credentials` needs to undo this
    write, including the deletion the restore cancelled.

    Raises :class:`CredentialStorageUnavailable` when the Use_Case_Account
    denies the Portal the Secrets Manager calls (Req 5.9). Any other
    failure propagates as-is, and never with a message derived from a
    credential value.
    """
    resolved_region = region or get_usecase_region(usecase or {})
    name = secret_name(device_id, csid)
    candidates = (name,) if create else tuple(secret_ids or ())
    try:
        client = _client('secretsmanager', usecase, region=resolved_region,
                         session_name=session_name)
        description: Optional[Dict[str, Any]] = None
        target: Optional[str] = None
        for secret_id in candidates:
            try:
                description = client.describe_secret(SecretId=secret_id)
            except ClientError as e:
                if _error_code(e) != 'ResourceNotFoundException':
                    raise
                continue
            target = secret_id
            break

        if target is None:
            if not create and device_secret_id(
                    name, device_id, derived=True) is None:
                raise CameraIdCannotHoldCredentials()
            response = client.create_secret(
                Name=name,
                Description=('DDA Portal-managed stream camera credentials '
                             f"for {device_id}/{csid}"),
                SecretString=_secret_string(credentials),
                Tags=_secret_tags((usecase or {}).get('usecase_id'),
                                  device_id, csid),
            )
            logger.info(f"Created Credential_Vault secret {name}")
            return {'secretArn': response['ARN'],
                    'versionId': response['VersionId'],
                    'created': True,
                    'previousVersionId': None,
                    'restoredFromDeletion': False}

        # The camera's existing secret: the name for a create, the first
        # resolved id that exists for an update (task 29). Logged by name.
        label = description.get('Name') or secret_id_name(target)
        restored_from_deletion = False
        if description.get('DeletedDate') is not None:
            # Scheduled for deletion by an earlier clear/delete: restore
            # it rather than failing, and rewrite the value below.
            client.restore_secret(SecretId=target)
            restored_from_deletion = True
            logger.info(
                f"Restored Credential_Vault secret {label} from scheduled "
                "deletion before writing a new version")

        try:
            if restored_from_deletion:
                description = client.describe_secret(SecretId=target)
            previous_version_id = _current_version_id(description)
            response = client.put_secret_value(
                SecretId=target, SecretString=_secret_string(credentials))
        except Exception:
            # Nothing was written, but the restore above cancelled the
            # deletion an earlier clear/delete scheduled: schedule it
            # again, so a request that stores nothing leaves the vault
            # as it found it (Reqs 5.8, 5.9).
            if restored_from_deletion:
                _reschedule_deletion(client, target)
            raise
        try:
            # Re-tagged with the camera's current id (task 29): after a
            # re-key the tag names the cfg- entry, while the name keeps
            # the id the secret was created under.
            client.tag_resource(
                SecretId=target,
                Tags=_secret_tags((usecase or {}).get('usecase_id'),
                                  device_id, csid))
        except Exception as e:  # noqa: BLE001 — tags are metadata only
            logger.warning(f"Could not refresh the tags of {label}: {e}")
        logger.info(
            f"Wrote a new version of Credential_Vault secret {label}")
        return {'secretArn': response['ARN'],
                'versionId': response['VersionId'],
                'created': False,
                'previousVersionId': previous_version_id,
                'restoredFromDeletion': restored_from_deletion}
    except (CredentialStorageUnavailable, CameraIdCannotHoldCredentials):
        raise
    except Exception as e:  # noqa: BLE001 — classified here
        if _is_access_denied(e):
            raise CredentialStorageUnavailable(
                'permission to store stream camera credentials',
                str(_error_code(e))) from e
        raise


# ---------------------------------------------------------------------------
# Step 5's rollback: withdrawing a version nothing references (Req 5.4)
# ---------------------------------------------------------------------------

def withdraw_stream_credentials(usecase: Dict[str, Any],
                                stored: Dict[str, Any],
                                region: Optional[str] = None,
                                session_name: Optional[str] = None
                                ) -> Dict[str, Any]:
    """Undo a :func:`store_stream_credentials` whose desired-change write
    then failed, so that no stored version is left referenced (Req 5.4).

    - A secret this request *created* is removed with
      ``ForceDeleteWithoutRecovery``: it never existed before the request,
      and leaving a recoverable shell would leave a credential nobody
      reads.
    - A *new version* of an existing secret has ``AWSCURRENT`` moved back
      to the version that held it, and removed from the new one, so the
      secret's current value after the failed request equals its value
      before it. Secrets Manager moves ``AWSPREVIOUS`` onto the version
      ``AWSCURRENT`` leaves, so the new version keeps at most that label;
      nothing references it.
    - A secret the request *restored* from a scheduled deletion (an
      earlier ``clearCredentials`` or delete) is scheduled for deletion
      again, after the stage move, since Secrets Manager refuses stage
      changes on a secret pending deletion. Without this, a failed
      re-add would keep credentials the operator cleared forever.

    Best-effort by design: the route must still return its existing
    delivery-failure response, so every failure here is logged and
    reported in the returned status rather than raised. Returns
    ``{'status': 'deleted' | 'reverted' | 'skipped' | 'failed', ...}``,
    plus ``deletion_rescheduled`` for a restored secret.
    """
    if not stored:
        return {'status': 'skipped', 'reason': 'nothing stored'}
    secret_arn = stored.get('secretArn')
    try:
        client = _client('secretsmanager', usecase,
                         region=region or get_usecase_region(usecase or {}),
                         session_name=session_name)
    except Exception as e:  # noqa: BLE001 — the route's 502 still wins
        logger.error(
            f"Failed to withdraw the stored credential version of "
            f"{secret_arn}: {e}")
        return {'status': 'failed', 'secret_arn': secret_arn,
                'error': str(e)}

    if stored.get('created'):
        try:
            client.delete_secret(SecretId=secret_arn,
                                 ForceDeleteWithoutRecovery=True)
        except Exception as e:  # noqa: BLE001 — the route's 502 still wins
            logger.error(
                f"Failed to withdraw the Credential_Vault secret created "
                f"for this request ({secret_arn}): {e}")
            return {'status': 'failed', 'secret_arn': secret_arn,
                    'error': str(e)}
        logger.info(
            f"Withdrew the Credential_Vault secret created for this "
            f"request ({secret_arn}) after the delivery failure")
        return {'status': 'deleted', 'secret_arn': secret_arn}

    previous_version_id = stored.get('previousVersionId')
    if not previous_version_id:
        # An existing secret with no AWSCURRENT version to restore:
        # destroying it would be more destructive than the failure it
        # is recovering from, so the version is left in place and the
        # registry stays untouched (nothing references it).
        logger.warning(
            f"Could not withdraw the new version of {secret_arn}: the "
            "secret had no current version to restore")
        result = {'status': 'failed', 'secret_arn': secret_arn,
                  'error': 'no previous version'}
    else:
        try:
            client.update_secret_version_stage(
                SecretId=secret_arn, VersionStage='AWSCURRENT',
                MoveToVersionId=previous_version_id,
                RemoveFromVersionId=stored.get('versionId'))
        except Exception as e:  # noqa: BLE001 — the route's 502 still wins
            logger.error(
                f"Failed to withdraw the stored credential version of "
                f"{secret_arn}: {e}")
            result = {'status': 'failed', 'secret_arn': secret_arn,
                      'error': str(e)}
        else:
            logger.info(
                f"Moved AWSCURRENT of {secret_arn} back to the version held "
                "before this request after the delivery failure")
            result = {'status': 'reverted', 'secret_arn': secret_arn,
                      'version_id': previous_version_id}

    if stored.get('restoredFromDeletion'):
        result['deletion_rescheduled'] = _reschedule_deletion(
            client, secret_arn)
    return result


def _reschedule_deletion(client: Any, secret_id: str) -> bool:
    """Schedule a secret this request restored for deletion again, with
    the usual recovery window. Best-effort: logged, never raised."""
    try:
        client.delete_secret(
            SecretId=secret_id,
            RecoveryWindowInDays=DELETION_RECOVERY_WINDOW_DAYS)
    except Exception as e:  # noqa: BLE001 — callers report, never raise
        logger.error(
            f"Could not schedule Credential_Vault secret {secret_id} for "
            f"deletion again after restoring it: {e}")
        return False
    logger.info(
        f"Scheduled Credential_Vault secret {secret_id} for deletion again "
        f"({DELETION_RECOVERY_WINDOW_DAYS}-day recovery window)")
    return True


# ---------------------------------------------------------------------------
# Requirement 5.8's scheduled deletion (clearCredentials and delete)
# ---------------------------------------------------------------------------

def schedule_secret_deletion(usecase: Dict[str, Any], device_id: str,
                             csid: str, region: Optional[str] = None,
                             session_name: Optional[str] = None, *,
                             secret_ids: Optional[Sequence[str]] = None):
    """Schedule deletion of a Camera_Source's Credential_Vault secret
    (Req 5.8), with the recoverable window of
    :data:`DELETION_RECOVERY_WINDOW_DAYS`.

    Called *after* the desired change has been delivered, so a delivery
    failure never destroys credentials the device is still using. A camera
    that never had credentials has no secret, which is reported as
    ``absent`` rather than as an error.

    ``secret_ids`` (task 29, design component 7 "Clear and delete"):
    exactly those SecretIds are scheduled, in order, and the result is a
    list with one ``{'status', 'secret_id', ...}`` per id. An empty
    sequence schedules nothing and makes no call. Only ``secret_ids is
    None`` keeps the by-name behavior below, which schedules
    ``secret_name(device_id, csid)`` and returns one dict: the test is
    ``is None``, never the sequence's truth value, so an empty list can
    never schedule a name another camera may use.

    Best-effort: the change is already delivered, so a failure here is
    logged and reported, never raised. Each status is ``'scheduled'``,
    ``'absent'`` (no such secret, or one already scheduled) or
    ``'failed'``.
    """
    if secret_ids is not None:
        return _schedule_secret_ids(usecase, list(secret_ids),
                                    region=region, session_name=session_name)
    name = secret_name(device_id, csid)
    try:
        client = _client('secretsmanager', usecase,
                         region=region or get_usecase_region(usecase or {}),
                         session_name=session_name)
        client.delete_secret(
            SecretId=name,
            RecoveryWindowInDays=DELETION_RECOVERY_WINDOW_DAYS)
        logger.info(
            f"Scheduled Credential_Vault secret {name} for deletion in "
            f"{DELETION_RECOVERY_WINDOW_DAYS} days")
        return {'status': 'scheduled', 'secret_name': name,
                'recovery_window_days': DELETION_RECOVERY_WINDOW_DAYS}
    except Exception as e:  # noqa: BLE001 — the change is already delivered
        if _error_code(e) in ('ResourceNotFoundException',
                              'InvalidRequestException'):
            # No secret, or one already scheduled for deletion.
            return {'status': 'absent', 'secret_name': name}
        logger.warning(
            f"Could not schedule deletion of Credential_Vault secret "
            f"{name}: {e}")
        return {'status': 'failed', 'secret_name': name, 'error': str(e)}


def _schedule_secret_ids(usecase: Dict[str, Any], secret_ids: List[str],
                         region: Optional[str] = None,
                         session_name: Optional[str] = None
                         ) -> List[Dict[str, Any]]:
    """The ``secret_ids`` form of :func:`schedule_secret_deletion`: one
    result per id, in order, and no call at all for an empty list. Each
    secret is logged by name. Best-effort, never raising."""
    if not secret_ids:
        return []
    try:
        client = _client('secretsmanager', usecase,
                         region=region or get_usecase_region(usecase or {}),
                         session_name=session_name)
    except Exception as e:  # noqa: BLE001 — the change is already delivered
        logger.warning(
            f"Could not schedule deletion of {len(secret_ids)} "
            f"Credential_Vault secret(s): {e}")
        return [{'status': 'failed', 'secret_id': secret_id,
                 'error': str(e)} for secret_id in secret_ids]
    results: List[Dict[str, Any]] = []
    for secret_id in secret_ids:
        label = secret_id_name(secret_id)
        try:
            client.delete_secret(
                SecretId=secret_id,
                RecoveryWindowInDays=DELETION_RECOVERY_WINDOW_DAYS)
        except Exception as e:  # noqa: BLE001 — the change is delivered
            if _error_code(e) in ('ResourceNotFoundException',
                                  'InvalidRequestException'):
                # No secret, or one already scheduled for deletion (a
                # secret both an ARN and its name resolved, task 29).
                results.append({'status': 'absent', 'secret_id': secret_id})
                continue
            logger.warning(
                f"Could not schedule deletion of Credential_Vault secret "
                f"{label}: {e}")
            results.append({'status': 'failed', 'secret_id': secret_id,
                            'error': str(e)})
            continue
        logger.info(
            f"Scheduled Credential_Vault secret {label} for deletion in "
            f"{DELETION_RECOVERY_WINDOW_DAYS} days")
        results.append({'status': 'scheduled', 'secret_id': secret_id,
                        'recovery_window_days':
                            DELETION_RECOVERY_WINDOW_DAYS})
    return results
