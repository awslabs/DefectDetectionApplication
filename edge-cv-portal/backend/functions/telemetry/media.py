"""Media locations of the workflow telemetry (workflow-telemetry-dashboards
design §12.1-§12.3; tasks.md 3.3, 3.4).

Layout in the use case's Results_Bucket (``inference_uploader_s3_bucket``
override, else ``dda-inference-results-<account>``, as ``captures.py``
resolves it)::

    dda-telemetry/clips/<usecase_id>/<device>/<workflow_id>/<yyyy-mm-dd>/<clipId>.mp4   (+ .json)
    dda-telemetry/live/<usecase_id>/<device>/<workflow_id>/<slot>.jpg

Binding before any credential (§12.2): a reference is bound by
:func:`bind_key` (the bucket must be the Results_Bucket, else
``bucket_not_allowed`` with reason ``portal_bucket`` for a Portal_Owned_Bucket;
the key must lie under the record's Pair prefix, else ``key_outside_prefix``)
or :func:`bind` for an ``s3://`` URI (``invalid_s3_uri`` first). The ingest
binds with ``audit=False`` (a membership test, no audit row); the API binds
with ``audit=True`` and only then builds a client through
``credentials.client('s3', U, 'media-read', bucket=...)``.
"""
import logging
from collections import namedtuple
from typing import Any, Dict, Optional

import shared_utils

__all__ = [
    "TELEMETRY_PREFIX", "CLIPS_PREFIX", "LIVE_PREFIX", "Binding", "results_bucket", "pair_prefix",
    "bind_key", "bind", "media_location",
]

logger = logging.getLogger(__name__)

TELEMETRY_PREFIX = "dda-telemetry/"
CLIPS_PREFIX = TELEMETRY_PREFIX + "clips/"
LIVE_PREFIX = TELEMETRY_PREFIX + "live/"

#: ``ok`` with ``bucket``/``key``, or ``reason`` in ``invalid_s3_uri``,
#: ``bucket_not_allowed`` (``detail`` ``portal_bucket`` or ``not_results_bucket``)
#: and ``key_outside_prefix``.
Binding = namedtuple("Binding", ["ok", "reason", "detail", "bucket", "key"])


def results_bucket(usecase: Dict[str, Any]) -> str:
    """The use case's Results_Bucket as ``captures.py`` resolves it."""
    account_id = str(usecase.get("account_id") or "")
    override = usecase.get("inference_uploader_s3_bucket")
    return str(override) if override else "dda-inference-results-{0}".format(account_id)


def pair_prefix(kind: str, usecase_id: str, device_id: str, workflow_id: str) -> str:
    """``dda-telemetry/<clips|live>/<uc>/<device>/<wf>/``."""
    root = CLIPS_PREFIX if kind == "clips" else LIVE_PREFIX
    return "{0}{1}/{2}/{3}/".format(root, usecase_id, device_id, workflow_id)


def bind_key(usecase: Dict[str, Any], kind: str, usecase_id: str, device_id: str, workflow_id: str,
             bucket: Any, key: Any) -> Binding:
    """Bind a (bucket, key) reference to the record's Pair (§12.2), without
    any credential or network call."""
    if not shared_utils.is_membership_name(bucket) or not isinstance(key, str) or not key:
        return Binding(False, "invalid_s3_uri", None, None, None)
    if bucket != results_bucket(usecase):
        detail = "portal_bucket" if bucket in shared_utils.portal_owned_buckets() else "not_results_bucket"
        return Binding(False, "bucket_not_allowed", detail, bucket, None)
    prefix = pair_prefix(kind, usecase_id, device_id, workflow_id)
    if not key.startswith(prefix) or len(key) <= len(prefix) or "/../" in key or key.endswith("/"):
        return Binding(False, "key_outside_prefix", None, bucket, None)
    return Binding(True, None, None, bucket, key)


def bind(usecase: Dict[str, Any], record: Dict[str, Any], uri: Any, *, kind: str = "clips",
         audit: bool = False, actor: Optional[Dict[str, Any]] = None, event: Optional[Dict[str, Any]] = None) -> Binding:
    """Bind an ``s3://bucket/key`` URI to ``record`` (``{usecase_id, device_id,
    workflow_id}``). With ``audit`` the refusal of a bucket that is in the
    use case's Allowed_Bucket_Set goes through ``assert_bucket_allowed`` (an
    audit row); the Results_Bucket itself skips it (§12.1, N14)."""
    try:
        bucket, key = shared_utils.parse_s3_uri(uri)
    except shared_utils.InvalidS3Uri:
        return Binding(False, "invalid_s3_uri", None, None, None)
    binding = bind_key(usecase, kind, str(record["usecase_id"]), str(record["device_id"]),
                       str(record["workflow_id"]), bucket, key)
    if audit and binding.reason == "bucket_not_allowed" and bucket in shared_utils.allowed_s3_buckets(usecase):
        try:
            shared_utils.assert_bucket_allowed(usecase, bucket, purpose="telemetry-media", actor=actor, event=event)
        except shared_utils.BucketConfigError:
            pass
    return binding


def media_location(usecase: Dict[str, Any]) -> Dict[str, Any]:
    """The read-only ``media_location`` of the settings answer (§12.1)."""
    override = usecase.get("inference_uploader_s3_bucket")
    return {"bucket": results_bucket(usecase), "prefix": TELEMETRY_PREFIX,
            "source": "override" if override else "default"}
