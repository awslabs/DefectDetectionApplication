"""
The Device_Access_Document: the inline policy the portal writes on the
devices' token-exchange role so devices can read a use case's buckets that the
template's name patterns do not cover (portal-authorization-and-iam-scoping,
design I6 "Design"; Requirements 8.1, 8.2).

One inline policy, ``DDAUseCaseDeviceAccess``, on the Device_Role holds one
read statement per use case (``Sid: UseCase<usecase_id without hyphens>``)
whose ``Resource`` lists the bucket and object ARNs of every bucket in the use
case's ``device_read_buckets`` (its ``s3_bucket`` and ``additional_s3_buckets``
outside ``dda-*`` / ``*-dda-*``; never ``data_s3_bucket``), and one
results-write statement (``Sid: UseCase<id>Results``) for an
``inference_uploader_s3_bucket`` outside ``dda-inference-results-*``. A use
case whose buckets all match the patterns has no statement.

The builders are pure. The two writers, ``grant_device_access`` and
``revoke_device_access``, take the IAM client as an argument and create none:
``deployments.py`` (deployment create, on the Deployments role) and
``usecases.py`` (``PUT`` and ``DELETE /usecases/{id}``, on the UseCases role)
pass ``get_usecase_client('iam', usecase)`` - the calling handler's execution
role for a single-account use case, the assumed ``DDAPortalAccessRole``
otherwise. Both are best-effort: every failure comes back as a status, none is
raised.

The shape constants are mirrored in ``scripts/portal-role-drift-check.py``; a
unit test asserts that the two copies are equal.
"""
import fnmatch
import json
import logging
from urllib.parse import unquote

# The device role's name is defined once, beside the other portal-written
# inline policy on that role (DDAWorkflowTuningSampleAccess).
from tuning_settings import DEVICE_ROLE_NAME

logger = logging.getLogger(__name__)

DEVICE_ACCESS_POLICY_NAME = 'DDAUseCaseDeviceAccess'
# IAM allows 10,240 characters for all inline policies of a role together and
# the device role already carries five; the document takes at most this much.
DEVICE_ACCESS_POLICY_MAX_BYTES = 6_000
# The DDAPortalComponentAccessPolicy resource patterns (usecase-account-stack.ts):
# buckets matching them are already readable by devices.
TEMPLATE_BUCKET_PATTERNS = ('dda-*', '*-dda-*')
TEMPLATE_RESULTS_PATTERN = 'dda-inference-results-*'
SID_RE = r'^UseCase[0-9a-f]{32}(Results)?$'
READ_ACTIONS = ('s3:GetObject', 's3:GetObjectVersion', 's3:GetBucketLocation')
RESULTS_ACTIONS = ('s3:PutObject', 's3:PutObjectTagging', 's3:GetObject', 's3:GetBucketLocation')
RESULTS_SID_SUFFIX = 'Results'
POLICY_VERSION = '2012-10-17'

REMEDY_POLICY_SIZE_LIMIT = (
    f"The {DEVICE_ACCESS_POLICY_NAME} policy would exceed {DEVICE_ACCESS_POLICY_MAX_BYTES} bytes: "
    "name the bucket within the dda-* pattern, remove allowed buckets the use case no longer "
    "needs, or consolidate use cases.")
# IAM stores no inline policy without a statement and the portal's grant on
# the device role is GetRolePolicy / PutRolePolicy only, so a removal that
# would leave the document empty is reported with the manual command.
REMEDY_EMPTY_DOCUMENT = (
    f"Removing the statement would leave {DEVICE_ACCESS_POLICY_NAME} without statements; delete "
    f"the policy instead: aws iam delete-role-policy --role-name {DEVICE_ROLE_NAME} "
    f"--policy-name {DEVICE_ACCESS_POLICY_NAME}")


# ------------------------------------------------------------------ builders

def usecase_sid(usecase_id) -> str:
    """The read statement's Sid (Sids are alphanumeric; use-case ids are UUIDs)."""
    return 'UseCase' + str(usecase_id).replace('-', '')


def _usecase_sids(usecase_id) -> set:
    return {usecase_sid(usecase_id), usecase_sid(usecase_id) + RESULTS_SID_SUFFIX}


def _names(value) -> list:
    """The bucket names a record field holds: a list, a DynamoDB string set
    (read back as a Python set) or a single string."""
    if value is None:
        return []
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _in_patterns(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def device_read_buckets(usecase) -> list:
    """The use case's out-of-pattern Device_Read_Buckets: ``s3_bucket`` and
    every ``additional_s3_buckets`` entry matching no TEMPLATE_BUCKET_PATTERNS
    entry, de-duplicated and sorted. ``data_s3_bucket`` is never included: no
    device code path reads training data, and it may live in a separate data
    account where an identity-policy grant is inert."""
    usecase = usecase if isinstance(usecase, dict) else {}
    names = [usecase.get('s3_bucket')] + _names(usecase.get('additional_s3_buckets'))
    return sorted({name for name in names
                   if isinstance(name, str) and name.strip()
                   and not _in_patterns(name, TEMPLATE_BUCKET_PATTERNS)})


def results_override(usecase):
    """The use case's Results_Bucket_Override when it is outside
    TEMPLATE_RESULTS_PATTERN, else None (the template already grants the
    results upload on dda-inference-results-*)."""
    usecase = usecase if isinstance(usecase, dict) else {}
    value = usecase.get('inference_uploader_s3_bucket')
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    return None if fnmatch.fnmatchcase(name, TEMPLATE_RESULTS_PATTERN) else name


def bucket_arns(bucket: str) -> list:
    """The bucket ARN and its objects' ARN. IAM ignores the object actions on
    the bucket ARN and the bucket action on the object ARN, so one statement
    serves both."""
    return [f'arn:aws:s3:::{bucket}', f'arn:aws:s3:::{bucket}/*']


def usecase_statements(usecase) -> list:
    """This use case's statements in the Device_Access_Document: one read
    statement listing two ARNs per bucket of ``device_read_buckets``, and one
    results-write statement for an out-of-pattern override; ``[]`` when the
    use case needs neither."""
    usecase = usecase if isinstance(usecase, dict) else {}
    usecase_id = usecase.get('usecase_id')
    if not usecase_id:
        return []
    statements = []
    buckets = device_read_buckets(usecase)
    if buckets:
        statements.append({
            'Sid': usecase_sid(usecase_id),
            'Effect': 'Allow',
            'Action': list(READ_ACTIONS),
            'Resource': [arn for bucket in buckets for arn in bucket_arns(bucket)],
        })
    override = results_override(usecase)
    if override:
        statements.append({
            'Sid': usecase_sid(usecase_id) + RESULTS_SID_SUFFIX,
            'Effect': 'Allow',
            'Action': list(RESULTS_ACTIONS),
            'Resource': bucket_arns(override),
        })
    return statements


def _statements_of(document) -> list:
    if not isinstance(document, dict):
        return []
    statements = document.get('Statement')
    if isinstance(statements, dict):
        return [statements]
    return list(statements) if isinstance(statements, list) else []


def merge_document(existing, usecase_id, statements):
    """``(document, changed)``: ``existing`` with this use case's statements
    replaced by ``statements`` and every other statement kept as it is.

    The replacement takes the place of the use case's first existing
    statement (appended when it had none), so re-merging the statements a
    document already holds changes nothing and causes no write."""
    sids = _usecase_sids(usecase_id)
    current = _statements_of(existing)
    merged = []
    placed = False
    for statement in current:
        if isinstance(statement, dict) and statement.get('Sid') in sids:
            if not placed:
                merged.extend(statements)
                placed = True
            continue
        merged.append(statement)
    if not placed:
        merged.extend(statements)
    return {'Version': POLICY_VERSION, 'Statement': merged}, merged != current


def document_size(document) -> int:
    """The serialized size IAM counts against its limit (no whitespace)."""
    return len(json.dumps(document, separators=(',', ':')))


# ------------------------------------------------------- the IAM writing pair

def _error_code(error) -> str:
    response = getattr(error, 'response', None)
    if isinstance(response, dict):
        return str((response.get('Error') or {}).get('Code') or '')
    return ''


def _describe(error) -> str:
    code = _error_code(error)
    if code:
        message = (getattr(error, 'response', {}).get('Error') or {}).get('Message') or ''
        return f"{code}: {message}" if message else code
    return str(error) or type(error).__name__


def _result(status: str, **fields) -> dict:
    return {'status': status, 'role_name': DEVICE_ROLE_NAME,
            'policy_name': DEVICE_ACCESS_POLICY_NAME, **fields}


def failed_result(error) -> dict:
    """The ``failed`` status for an error raised outside the two writers (the
    caller could not obtain the IAM client)."""
    return _result('failed', error=_describe(error))


def _read_document(iam_client):
    """The current document, or None when the policy does not exist."""
    try:
        response = iam_client.get_role_policy(
            RoleName=DEVICE_ROLE_NAME, PolicyName=DEVICE_ACCESS_POLICY_NAME)
    except Exception as e:
        if _error_code(e) == 'NoSuchEntity':
            return None
        raise
    document = response.get('PolicyDocument')
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            document = json.loads(unquote(document))
    return document


def _write(usecase_id, statements, iam_client) -> dict:
    """Read, merge, and write only when the document changed and stays
    within the cap."""
    try:
        document, changed = merge_document(_read_document(iam_client), usecase_id, statements)
        if not changed:
            return _result('unchanged')
        if not document['Statement']:
            return _result('failed', reason='document_would_be_empty',
                           error='the document would have no statements',
                           remedy=REMEDY_EMPTY_DOCUMENT)
        size = document_size(document)
        if size > DEVICE_ACCESS_POLICY_MAX_BYTES:
            return _result('failed', reason='policy_size_limit',
                           error=f'the document would be {size} bytes, over the '
                                 f'{DEVICE_ACCESS_POLICY_MAX_BYTES}-byte cap',
                           remedy=REMEDY_POLICY_SIZE_LIMIT, size=size)
        iam_client.put_role_policy(
            RoleName=DEVICE_ROLE_NAME, PolicyName=DEVICE_ACCESS_POLICY_NAME,
            PolicyDocument=json.dumps(document, separators=(',', ':')))
        return _result('granted' if statements else 'removed', size=size)
    except Exception as e:
        if _error_code(e) == 'LimitExceeded':
            return _result('failed', reason='policy_size_limit', error=_describe(e),
                           remedy=REMEDY_POLICY_SIZE_LIMIT)
        return _result('failed', error=_describe(e))


def grant_device_access(usecase, *, iam_client, region=None) -> dict:
    """Write this use case's statements into the Device_Access_Document
    (removing them when it no longer needs any). Status ``granted`` |
    ``removed`` | ``unchanged`` | ``failed`` (with ``error``, and ``reason`` /
    ``remedy`` for ``policy_size_limit`` and ``document_would_be_empty``)."""
    usecase = usecase if isinstance(usecase, dict) else {}
    result = _write(usecase.get('usecase_id'), usecase_statements(usecase), iam_client)
    log = logger.warning if result['status'] == 'failed' else logger.info
    log(f"{DEVICE_ACCESS_POLICY_NAME} on {DEVICE_ROLE_NAME} for use case "
        f"{usecase.get('usecase_id')} (region {region or 'default'}): {result}")
    return result


def revoke_device_access(usecase, *, iam_client) -> dict:
    """Remove this use case's statements from the Device_Access_Document.
    Status ``removed`` | ``unchanged`` | ``failed``."""
    usecase = usecase if isinstance(usecase, dict) else {}
    result = _write(usecase.get('usecase_id'), [], iam_client)
    log = logger.warning if result['status'] == 'failed' else logger.info
    log(f"{DEVICE_ACCESS_POLICY_NAME} on {DEVICE_ROLE_NAME}: revoke for use case "
        f"{usecase.get('usecase_id')}: {result}")
    return result
