"""Session policies for calls into a ``cross`` use case's account
(workflow-telemetry-dashboards design §14.4; workflow-debug-mode design §9).

Every ``AssumeRole`` of ``DDAPortalAccessRole`` made by the telemetry and
debug code carries one of these policies, so the session holds only what its
purpose needs however broad the role is. The debug mode created this module
with the four purposes it uses (``ingest`` and ``media-retention-read`` as the
dashboards design defines them, plus ``debug-control`` and ``image-read``);
the dashboards added ``api-control``, ``media-read`` and ``live-expiry``
(tasks.md 2.2).

Each policy is well under the 2,048-character limit of a session policy
(the unit test serializes every purpose with the longest arguments and
asserts under 1,000).
"""
import json
import re
from typing import Any, Dict, Optional

__all__ = [
    "PURPOSES",
    "INGEST",
    "MEDIA_RETENTION_READ",
    "DEBUG_CONTROL",
    "IMAGE_READ",
    "API_CONTROL",
    "MEDIA_READ",
    "LIVE_EXPIRY",
    "UnknownPurpose",
    "build",
    "policy_text",
]

INGEST = "ingest"
MEDIA_RETENTION_READ = "media-retention-read"
DEBUG_CONTROL = "debug-control"
IMAGE_READ = "image-read"
# The dashboards' purposes (workflow-telemetry-dashboards design §14.4,
# tasks.md 2.2).
API_CONTROL = "api-control"
MEDIA_READ = "media-read"
LIVE_EXPIRY = "live-expiry"

PURPOSES = (INGEST, MEDIA_RETENTION_READ, DEBUG_CONTROL, IMAGE_READ, API_CONTROL, MEDIA_READ, LIVE_EXPIRY)

#: The S3 bucket-name grammar (lower-case letters, digits, dots and hyphens,
#: 3-63 characters), so a name can never carry a policy wildcard.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
#: A Portal use case id (a UUID, or the test fixtures' ``uc-<uuid>``).
_USECASE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: The debug images' prefix on a Results_Bucket (workflow-debug-mode §1.3).
DEBUG_IMAGE_PREFIX = "dda-telemetry/debug/"
#: The telemetry media root and the live-frame prefix (dashboards §12.1).
TELEMETRY_PREFIX = "dda-telemetry/"
LIVE_PREFIX = "dda-telemetry/live/"


class UnknownPurpose(ValueError):
    """``build`` was asked for a purpose this module does not define."""


def _usecase_id(usecase: Any) -> str:
    value = usecase.get("usecase_id") if isinstance(usecase, dict) else usecase
    if not isinstance(value, str) or not _USECASE_ID_RE.match(value):
        raise ValueError("session policy needs a valid use case id")
    return value


def _bucket(bucket: Optional[str]) -> str:
    if not isinstance(bucket, str) or not _BUCKET_RE.match(bucket):
        raise ValueError("session policy needs a valid bucket name")
    return bucket


def _document(*statements: Dict[str, Any]) -> Dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def build(purpose: str, usecase: Any = None, bucket: Optional[str] = None) -> Dict[str, Any]:
    """The session policy document for ``purpose``.

    - ``ingest``: ``iot:ListPrincipalThings`` on ``arn:aws:iot:*:*:cert/*``;
    - ``media-retention-read`` (needs ``bucket``): ``s3:GetLifecycleConfiguration``
      and ``s3:GetBucketVersioning`` on the bucket ARN;
    - ``debug-control``: ``iot:UpdateThingShadow`` and ``iot:GetThingShadow`` on
      ``thing/*``; ``iot:GetTopicRule`` on ``rule/dda_dbg_ingest``;
    - ``image-read`` (needs ``bucket`` and ``usecase``): ``s3:GetObject`` on
      ``<bucket>/dda-telemetry/debug/<usecase>/*`` and ``s3:ListBucket`` on the
      bucket with ``s3:prefix`` like ``dda-telemetry/debug/<usecase>/*``;
    - ``api-control``: ``iot:UpdateThingShadow`` and ``iot:GetThingShadow`` on
      ``thing/*``; ``greengrass:GetCoreDevice``, ``greengrass:ListCoreDevices``
      and ``greengrass:ListTagsForResource`` on ``*``; ``iot:GetTopicRule`` on
      ``rule/dda_tel_ingest`` and ``rule/dda_dbg_ingest``;
    - ``media-read`` (needs ``bucket`` and ``usecase``): ``s3:GetObject`` on
      ``<bucket>/dda-telemetry/*/<usecase>/*`` and ``s3:ListBucket`` with
      ``s3:prefix`` like ``dda-telemetry/*/<usecase>/*``;
    - ``live-expiry`` (needs ``bucket`` and ``usecase``): ``s3:ListBucket`` with
      ``s3:prefix`` like ``dda-telemetry/live/<usecase>/*`` and
      ``s3:DeleteObject`` on ``<bucket>/dda-telemetry/live/<usecase>/*``.

    Raises :class:`UnknownPurpose` for any other purpose and ``ValueError``
    when a required argument is missing or malformed.
    """
    if purpose == INGEST:
        return _document({
            "Effect": "Allow",
            "Action": "iot:ListPrincipalThings",
            "Resource": "arn:aws:iot:*:*:cert/*",
        })
    if purpose == MEDIA_RETENTION_READ:
        return _document({
            "Effect": "Allow",
            "Action": ["s3:GetLifecycleConfiguration", "s3:GetBucketVersioning"],
            "Resource": "arn:aws:s3:::{0}".format(_bucket(bucket)),
        })
    if purpose == DEBUG_CONTROL:
        return _document(
            {
                "Effect": "Allow",
                "Action": ["iot:UpdateThingShadow", "iot:GetThingShadow"],
                "Resource": "arn:aws:iot:*:*:thing/*",
            },
            {
                "Effect": "Allow",
                "Action": "iot:GetTopicRule",
                "Resource": "arn:aws:iot:*:*:rule/dda_dbg_ingest",
            },
        )
    if purpose == IMAGE_READ:
        name = _bucket(bucket)
        prefix = DEBUG_IMAGE_PREFIX + _usecase_id(usecase) + "/*"
        return _document(
            {
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::{0}/{1}".format(name, prefix),
            },
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": "arn:aws:s3:::{0}".format(name),
                "Condition": {"StringLike": {"s3:prefix": prefix}},
            },
        )
    if purpose == API_CONTROL:
        return _document(
            {
                "Effect": "Allow",
                "Action": ["iot:UpdateThingShadow", "iot:GetThingShadow"],
                "Resource": "arn:aws:iot:*:*:thing/*",
            },
            {
                "Effect": "Allow",
                "Action": ["greengrass:GetCoreDevice", "greengrass:ListCoreDevices",
                           "greengrass:ListTagsForResource"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": "iot:GetTopicRule",
                "Resource": ["arn:aws:iot:*:*:rule/dda_tel_ingest", "arn:aws:iot:*:*:rule/dda_dbg_ingest"],
            },
        )
    if purpose == MEDIA_READ:
        name = _bucket(bucket)
        prefix = TELEMETRY_PREFIX + "*/" + _usecase_id(usecase) + "/*"
        return _document(
            {
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::{0}/{1}".format(name, prefix),
            },
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": "arn:aws:s3:::{0}".format(name),
                "Condition": {"StringLike": {"s3:prefix": prefix}},
            },
        )
    if purpose == LIVE_EXPIRY:
        name = _bucket(bucket)
        prefix = LIVE_PREFIX + _usecase_id(usecase) + "/*"
        return _document(
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": "arn:aws:s3:::{0}".format(name),
                "Condition": {"StringLike": {"s3:prefix": prefix}},
            },
            {
                "Effect": "Allow",
                "Action": "s3:DeleteObject",
                "Resource": "arn:aws:s3:::{0}/{1}".format(name, prefix),
            },
        )
    raise UnknownPurpose("unknown session policy purpose: {0!r}".format(purpose))


def policy_text(policy: Dict[str, Any]) -> str:
    """The canonical serialization of a policy: the session cache key of
    ``credentials.py`` and the size the unit test measures."""
    return json.dumps(policy, sort_keys=True, separators=(",", ":"))
