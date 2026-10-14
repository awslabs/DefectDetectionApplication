"""
Use_Case Sample_Export settings for VLM/LLM Anomaly Tuning
(spec: quality-prompt-tuning, Requirements 2.1, 2.8, 2.9, 11.3).

One place for the two Use_Case settings this feature adds and for the
artefacts derived from them, so the writer (``usecases.py`` validating an
update), the deliverer (``deployments.py`` merging the LocalServer
component configuration and granting the device role) and the reader (the
tuning Lambda's ``sampleExportEnabled`` and Sample_Store location) can
never disagree:

- ``tuning_sample_export``        — bool, default false. WHERE enabled, every
  deployment of the Use_Case's devices carries the ``workflowTuning``
  LocalServer component configuration and the device's token-exchange role
  is granted put/get on the Sample_Store prefix (Requirements 2.1, 2.8).
- ``tuning_sample_retention_days`` — int in [7, 365], default 30. The S3
  lifecycle expiry applied to ``workflow-tuning/samples/`` (Requirement 2.9).

The module is pure: no boto3, no I/O, no environment reads beyond the
device role name override, so it imports cleanly into every Lambda that
bundles ``backend/functions``.
"""
import os
from decimal import Decimal

# --------------------------------------------------------------------------
# Use_Case item fields
# --------------------------------------------------------------------------

#: Enablement flag (Requirement 2.1). Absent ⇒ disabled: a Use_Case that
#: never opted in is byte-identical to pre-feature (Requirement 11.3).
SAMPLE_EXPORT_FIELD = 'tuning_sample_export'

#: Sample_Store retention in days (Requirement 2.9).
SAMPLE_RETENTION_FIELD = 'tuning_sample_retention_days'

SAMPLE_RETENTION_DEFAULT_DAYS = 30
SAMPLE_RETENTION_MIN_DAYS = 7
SAMPLE_RETENTION_MAX_DAYS = 365

# --------------------------------------------------------------------------
# S3 layout (must match the device: src/backend/workflow_engine/tuning/)
# --------------------------------------------------------------------------

#: Everything this feature writes lives under this prefix of the Use_Case's
#: inference results bucket (Requirement 9.4).
TUNING_ROOT_PREFIX = 'workflow-tuning/'

#: The Sample_Store: where devices upload Tuning_Samples (Requirement 2.3).
#: The trailing slash is part of the contract — the device disables export
#: for a prefix that does not end in '/' (design, LocalServer component
#: configuration).
SAMPLE_STORE_PREFIX = TUNING_ROOT_PREFIX + 'samples/'

#: Device_Score_Job manifests the Portal writes and the device reads
#: (design, "Device_Score_Job (shadow + manifest)").
JOB_STORE_PREFIX = TUNING_ROOT_PREFIX + 'jobs/'

#: Score_Run outcome batches a Device_Score_Job appends
#: (``{sessions/}{session}/runs/{run}/outcomes-{n}.json``, Requirement 9.6).
SESSION_STORE_PREFIX = TUNING_ROOT_PREFIX + 'sessions/'

#: The LocalServer component configuration key carrying the export config.
COMPONENT_CONFIG_KEY = 'workflowTuning'

# --------------------------------------------------------------------------
# Device role grant (Requirement 2.8)
# --------------------------------------------------------------------------

#: The Greengrass token-exchange role devices assume, created by
#: station_install/setup_station.sh in the Use_Case account.
DEVICE_ROLE_NAME = os.environ.get('DEVICE_TOKEN_EXCHANGE_ROLE',
                                  'GreengrassV2TokenExchangeRole')

#: Inline policy this feature owns on that role. Nothing else writes it, so
#: it can be replaced (and removed) wholesale without touching any other
#: device permission — "no other new permission" (Requirement 2.8).
DEVICE_POLICY_NAME = 'DDAWorkflowTuningSampleAccess'


class SettingError(ValueError):
    """An invalid Use_Case setting value, carrying the message the API
    returns to the caller (400)."""


# --------------------------------------------------------------------------
# Writing: validation of an update
# --------------------------------------------------------------------------

def coerce_sample_export(value):
    """The boolean an update's ``tuning_sample_export`` carries.

    A real bool, or the exact strings ``"true"``/``"false"`` (trimmed,
    case-insensitive) — the same two shapes the device accepts for the
    delivered ``workflowTuning.enabled`` flag, so the Portal setting and
    the device's parse can never read the same value differently. Anything
    else is a SettingError, so a typo can never silently leave export off
    (or on)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == 'true':
            return True
        if text == 'false':
            return False
    raise SettingError(
        f"{SAMPLE_EXPORT_FIELD} must be a boolean")


def coerce_sample_retention_days(value):
    """The retention an update's ``tuning_sample_retention_days`` carries:
    a whole number of days within [7, 365] (Requirement 2.9)."""
    if isinstance(value, bool):
        raise SettingError(
            f"{SAMPLE_RETENTION_FIELD} must be an integer number of days "
            f"between {SAMPLE_RETENTION_MIN_DAYS} and "
            f"{SAMPLE_RETENTION_MAX_DAYS}")
    days = None
    if isinstance(value, int):
        days = value
    elif isinstance(value, (float, Decimal)):
        if value == int(value):
            days = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            days = int(text)
        except ValueError:
            days = None
    if days is None:
        raise SettingError(
            f"{SAMPLE_RETENTION_FIELD} must be an integer number of days "
            f"between {SAMPLE_RETENTION_MIN_DAYS} and "
            f"{SAMPLE_RETENTION_MAX_DAYS}")
    if not SAMPLE_RETENTION_MIN_DAYS <= days <= SAMPLE_RETENTION_MAX_DAYS:
        raise SettingError(
            f"{SAMPLE_RETENTION_FIELD} must be between "
            f"{SAMPLE_RETENTION_MIN_DAYS} and {SAMPLE_RETENTION_MAX_DAYS} "
            f"days")
    return days


def validate_settings(body):
    """Normalize the two settings in an update body in place.

    Returns the error message for the first invalid value (the caller
    answers 400 with it), or None when the body carries no tuning setting
    or only valid ones. Absent keys are left absent — an update that does
    not mention the settings never writes them."""
    for field, coerce in ((SAMPLE_EXPORT_FIELD, coerce_sample_export),
                          (SAMPLE_RETENTION_FIELD,
                           coerce_sample_retention_days)):
        if field in body:
            try:
                body[field] = coerce(body[field])
            except SettingError as e:
                return str(e)
    return None


# --------------------------------------------------------------------------
# Reading: the settings of a Use_Case item
# --------------------------------------------------------------------------

def sample_export_enabled(usecase):
    """Whether the Use_Case has Sample_Export enabled. Anything other than
    a recognizable true value — absent, null, false, a stray string — is
    disabled (Requirements 2.6, 11.3)."""
    if not isinstance(usecase, dict):
        return False
    if SAMPLE_EXPORT_FIELD not in usecase:
        return False
    try:
        return coerce_sample_export(usecase[SAMPLE_EXPORT_FIELD])
    except SettingError:
        return False


def sample_retention_days(usecase):
    """The Use_Case's Sample_Store retention in days: the configured value
    when it is valid, else the 30-day default (Requirement 2.9)."""
    if isinstance(usecase, dict) and SAMPLE_RETENTION_FIELD in usecase:
        try:
            return coerce_sample_retention_days(
                usecase[SAMPLE_RETENTION_FIELD])
        except SettingError:
            return SAMPLE_RETENTION_DEFAULT_DAYS
    return SAMPLE_RETENTION_DEFAULT_DAYS


def sample_store_bucket(usecase, account_id=None):
    """The bucket holding the Sample_Store: the Use_Case's inference results
    bucket, resolved exactly as the InferenceUploader configuration resolves
    it (``inference_uploader_s3_bucket``, else
    ``dda-inference-results-{account_id}``). Empty string when neither is
    available — the caller then delivers no configuration."""
    usecase = usecase if isinstance(usecase, dict) else {}
    configured = usecase.get('inference_uploader_s3_bucket')
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    account = account_id or usecase.get('account_id') or ''
    account = str(account).strip()
    if not account:
        return ''
    return f"dda-inference-results-{account}"


# --------------------------------------------------------------------------
# Delivery: derived artefacts
# --------------------------------------------------------------------------

def component_configuration(usecase, account_id=None):
    """The ``workflowTuning`` LocalServer component configuration for the
    Use_Case, or None when export is disabled or no bucket resolves
    (Requirements 2.1, 11.3).

    Shape (design, "LocalServer component configuration"):
    ``{"enabled": true, "bucket": "...", "prefix": "workflow-tuning/samples/"}``
    """
    if not sample_export_enabled(usecase):
        return None
    bucket = sample_store_bucket(usecase, account_id)
    if not bucket:
        return None
    return {'enabled': True, 'bucket': bucket, 'prefix': SAMPLE_STORE_PREFIX}


def device_policy_document(bucket):
    """The inline device-role policy granting put/get on the Use_Case
    bucket's ``workflow-tuning/*`` objects and nothing else
    (Requirement 2.8). The device puts Tuning_Samples and Device_Score_Job
    outcome batches and gets sample bytes and job manifests, all under that
    one prefix."""
    return {
        'Version': '2012-10-17',
        'Statement': [{
            'Sid': 'DDAWorkflowTuningSampleAccess',
            'Effect': 'Allow',
            'Action': ['s3:PutObject', 's3:GetObject'],
            'Resource': f"arn:aws:s3:::{bucket}/{TUNING_ROOT_PREFIX}*",
        }],
    }


# --------------------------------------------------------------------------
# Delivery: Sample_Store lifecycle (Requirement 2.9)
# --------------------------------------------------------------------------

#: Rule ids this feature owns on the Use_Case bucket's lifecycle
#: configuration. Nothing else writes them, so the merge below can replace
#: them wholesale while leaving every foreign rule untouched.
SAMPLES_LIFECYCLE_RULE_ID = 'DDAWorkflowTuningSamples'
JOBS_LIFECYCLE_RULE_ID = 'DDAWorkflowTuningJobs'
SESSIONS_LIFECYCLE_RULE_ID = 'DDAWorkflowTuningSessions'

LIFECYCLE_RULE_IDS = (SAMPLES_LIFECYCLE_RULE_ID, JOBS_LIFECYCLE_RULE_ID,
                      SESSIONS_LIFECYCLE_RULE_ID)

#: Device_Score_Job manifests and Score_Run outcome batches are consumed
#: within one run; 30 days is the design's fixed retention for both (the
#: Use_Case retention governs the Sample_Store only).
RUN_ARTIFACT_RETENTION_DAYS = 30


def lifecycle_rules(retention_days=None):
    """The S3 lifecycle rules this feature owns, in a stable order
    (Requirement 2.9):

    - ``workflow-tuning/samples/``  expires after the Use_Case's retention
      (``retention_days``, default 30, bounded 7..365);
    - ``workflow-tuning/jobs/``     and
    - ``workflow-tuning/sessions/`` expire after 30 days.

    Nothing outside ``workflow-tuning/`` is touched: each rule is filtered
    on its own prefix, so the Use_Case bucket's inference results are
    unaffected. ``NoncurrentVersionExpiration`` is set alongside the
    expiry so a versioned bucket actually frees the objects.
    """
    days = SAMPLE_RETENTION_DEFAULT_DAYS
    if retention_days is not None:
        days = coerce_sample_retention_days(retention_days)

    def rule(rule_id, prefix, expire_days):
        return {
            'ID': rule_id,
            'Filter': {'Prefix': prefix},
            'Status': 'Enabled',
            'Expiration': {'Days': expire_days},
            'NoncurrentVersionExpiration': {'NoncurrentDays': expire_days},
        }

    return [
        rule(SAMPLES_LIFECYCLE_RULE_ID, SAMPLE_STORE_PREFIX, days),
        rule(JOBS_LIFECYCLE_RULE_ID, JOB_STORE_PREFIX,
             RUN_ARTIFACT_RETENTION_DAYS),
        rule(SESSIONS_LIFECYCLE_RULE_ID, SESSION_STORE_PREFIX,
             RUN_ARTIFACT_RETENTION_DAYS),
    ]


def merge_lifecycle_rules(existing_rules, retention_days=None):
    """``(rules, changed)`` — the bucket's lifecycle rules with this
    feature's three rules in force.

    Every rule the bucket already carries is preserved verbatim and in
    order except the three ids this feature owns, which are replaced in
    place (or appended when absent). ``changed`` is False when the bucket
    already carries exactly these rules, so the caller can skip the write
    and stay idempotent across deployments."""
    desired = {rule['ID']: rule for rule in lifecycle_rules(retention_days)}
    merged = []
    seen = []
    changed = False
    for rule in list(existing_rules or []):
        rule_id = rule.get('ID') if isinstance(rule, dict) else None
        if rule_id in desired:
            if rule_id in seen:
                # A duplicate of one of ours: drop it, one rule per id.
                changed = True
                continue
            seen.append(rule_id)
            if rule != desired[rule_id]:
                changed = True
            merged.append(desired[rule_id])
        else:
            merged.append(rule)
    for rule_id in LIFECYCLE_RULE_IDS:
        if rule_id not in seen:
            merged.append(desired[rule_id])
            changed = True
    return merged, changed
