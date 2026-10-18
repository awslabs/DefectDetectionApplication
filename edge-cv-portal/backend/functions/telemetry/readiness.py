"""The readiness checklist of ``GET /telemetry/usecases/{id}/settings``
(workflow-telemetry-dashboards design §10.1; tasks.md 3.4), each item cached
60 s per use case.

| Item | Ready when | Remedy |
|---|---|---|
| ingestion_resources | ``portal``: always; ``cross``: ``iot:GetTopicRule dda_tel_ingest`` through ``api-control`` names the alias ``acct-<account>`` | update the use-case account template |
| region | the use case's region is the Portal's | not supported outside the Portal's region |
| role_form (cross) | the role ARN matches ``^arn:aws:iam::<account_id>:role/`` | register the DDAPortalAccessRole ARN |
| device_records | every managed core device has a Device_Record naming the use case | the Device_Record_Remedy |
| telemetry_source | ``UC#<uc>/SEEN`` exists | deploy the LocalServer release with the agent |
| media_location | ``ListObjectsV2`` through ``media-read`` succeeds; the region from ``x-amz-bucket-region`` | create the bucket / the §12.3 limit |
| media_retention | the two lifecycle rules, versioning not Enabled | run ``scripts/telemetry_lifecycle.py`` |
| registry_enforcement (warning) | ``registry_enforcement_enabled()`` | turn enforcement on |
| device_shadows | every ``DEVSHADOW#`` is ``ok`` for the current hash | check that the devices are online |

The media-location probe lists under ``dda-telemetry/live/<usecase_id>/``:
the ``media-read`` session policy conditions ``s3:ListBucket`` on
``dda-telemetry/*/<usecase>/*``, which a bare ``dda-telemetry/`` prefix does
not satisfy in the ``cross`` topology (the same adjustment the debug mode made
for its image probe, decision P4).
"""
import logging
import re
import threading
import time
from collections import namedtuple
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

import shared_utils
from edge_telemetry_common import credentials
from edge_telemetry_common import devices as device_records
from edge_telemetry_common.topology import CROSS, telemetry_topology
from telemetry import configdb, constants, media, retention, shadow

__all__ = [
    "CACHE_SECONDS", "DEVICE_RECORD_REMEDY", "REMEDIES", "ENFORCEMENT_WARNING", "RegionProbe", "ITEM_IDS",
    "role_arn_valid", "probe_media_location", "checklist", "clear_cache",
]

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
REGION_HEADER = "x-amz-bucket-region"
MANAGED_TAG = "dda-portal:managed"
PORTAL_RULE_NAME = "dda_tel_ingest"

ITEM_IDS = ("ingestion_resources", "region", "role_form", "device_records", "telemetry_source", "media_location",
            "media_retention", "registry_enforcement", "device_shadows")

DEVICE_RECORD_REMEDY = (
    "A UseCaseAdmin or PortalAdmin sets the device's Target architecture on its device page (Devices > the device), "
    "which calls PUT /devices/{id} with {\"usecase_id\": \"<this use case>\", \"target_architecture\": \"<arch>\"}; "
    "{\"usecase_id\": ..., \"test_device\": false} also works; or re-run quick setup on the device. The route needs "
    "node_designer:manage, which Operators do not hold. Until then the device publishes nothing and cannot be added "
    "to a dashboard."
)
REMEDIES = {
    "ingestion_resources": "Update the use-case account template (DDAPortalUseCaseAccountStack) in the use case's "
                           "account to the version with telemetry.",
    "region": "Telemetry is not supported for use cases outside the Portal's region.",
    "role_form": "Register the use case with its account's DDAPortalAccessRole ARN.",
    "device_records": DEVICE_RECORD_REMEDY,
    "telemetry_source": "Deploy the LocalServer release that carries the Telemetry_Agent to a device of this use case; "
                        "summaries appear within a minute of enabling.",
    "media_location_missing": "Create the use case's inference-results bucket, or set its override.",
    "media_location_denied": "The Portal cannot read this bucket: an override bucket is read only when it is tagged "
                             "dda-portal:managed=true (cross-account) or once the data-plane grant is wired "
                             "(Portal account); the default bucket needs the use-case account template with telemetry.",
    "media_retention": "Run scripts/telemetry_lifecycle.py --bucket <name> --clip-days <n> --apply with operator "
                       "credentials.",
    "device_shadows": "The listed devices did not accept the telemetry shadow: check that they are online.",
}
ENFORCEMENT_WARNING = (
    "Registry enforcement is off: every signed-in user holds dashboards:read on this use case, so clip playback "
    "and live frames are refused until it is on (Q3)."
)
CROSS_REGION_WARNING = ("the bucket is in {bucket_region} while the use case is registered in {usecase_region}: "
                        "devices PUT across regions (transfer cost and latency)")
HEADER_ABSENT_WARNING = "region taken from the use case (header absent); verify the bucket's region"

RegionProbe = namedtuple("RegionProbe", ["ok", "region", "region_source", "error", "warning"])

_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::(\d{12}):role/.+$")

_cache_lock = threading.Lock()
_cache: Dict[Any, Any] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached(key: Any, now: float, loader: Callable[[], Any]) -> Any:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now + CACHE_SECONDS, value)
    return value


def role_arn_valid(usecase: Dict[str, Any]) -> bool:
    """The ``cross`` role ARN form of §2.3: ``arn:aws:iam::<account_id>:role/...``."""
    match = _ROLE_ARN_RE.match(str(usecase.get("cross_account_role_arn") or ""))
    return bool(match and match.group(1) == str(usecase.get("account_id") or ""))


def _item(item_id: str, ready: bool, *, remedy: Optional[str] = None, warning: Optional[str] = None,
          **detail: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {"id": item_id, "ready": bool(ready)}
    if remedy and not ready:
        item["remedy"] = remedy
    if warning:
        item["warning"] = warning
    item.update(detail)
    return item


def _code(error: ClientError) -> str:
    return str(((error.response or {}).get("Error") or {}).get("Code") or "")


# --------------------------------------------------------------------------- items
def _ingestion_resources(usecase: Dict[str, Any]) -> Dict[str, Any]:
    if telemetry_topology(usecase) != CROSS:
        return _item("ingestion_resources", True, topology="portal")
    expected_suffix = ":function:dda-portal-telemetry-ingest:acct-{0}".format(usecase.get("account_id"))
    try:
        iot = credentials.client("iot", usecase, "api-control")
        rule = iot.get_topic_rule(ruleName=PORTAL_RULE_NAME)
    except ClientError as error:
        return _item("ingestion_resources", False, remedy=REMEDIES["ingestion_resources"], topology="cross",
                     error=_code(error))
    except (BotoCoreError, KeyError, ValueError) as error:
        return _item("ingestion_resources", False, remedy=REMEDIES["ingestion_resources"], topology="cross",
                     error=error.__class__.__name__)
    actions = ((rule.get("rule") or {}).get("actions") or [])
    names_alias = any(str(((action.get("lambda") or {}).get("functionArn")) or "").endswith(expected_suffix)
                      for action in actions)
    return _item("ingestion_resources", names_alias, remedy=REMEDIES["ingestion_resources"], topology="cross",
                 error=None if names_alias else "rule_action_mismatch")


def _region(usecase: Dict[str, Any], portal_region: str) -> Dict[str, Any]:
    region = shared_utils.get_usecase_region(usecase)
    return _item("region", region == portal_region, remedy=REMEDIES["region"], usecase_region=region,
                 portal_region=portal_region)


def _role_form(usecase: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if telemetry_topology(usecase) != CROSS:
        return None
    return _item("role_form", role_arn_valid(usecase), remedy=REMEDIES["role_form"])


def _managed_core_devices(usecase: Dict[str, Any]) -> List[str]:
    """Thing names of the managed core devices (the two calls devices.list_devices makes)."""
    gg = credentials.client("greengrassv2", usecase, "api-control")
    names: List[str] = []
    kwargs: Dict[str, Any] = {"maxResults": 100}
    while True:
        response = gg.list_core_devices(**kwargs)
        for device in response.get("coreDevices") or []:
            name = device.get("coreDeviceThingName")
            if not name:
                continue
            arn = "arn:aws:greengrass:{0}:{1}:coreDevices:{2}".format(
                shared_utils.get_usecase_region(usecase), usecase.get("account_id"), name)
            try:
                tags = (gg.list_tags_for_resource(resourceArn=arn).get("tags") or {})
            except ClientError:
                tags = {}
            if str(tags.get(MANAGED_TAG, "")).lower() == "true":
                names.append(str(name))
        token = response.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return sorted(set(names))


def _device_records(usecase: Dict[str, Any], devices_table: Any) -> Dict[str, Any]:
    recorded = set(device_records.usecase_devices(usecase))
    try:
        managed = _managed_core_devices(usecase)
    except ClientError as error:
        return _item("device_records", False, remedy=REMEDIES["device_records"], error=_code(error),
                     unrecorded=[], other_usecase={})
    except (BotoCoreError, KeyError, ValueError) as error:
        return _item("device_records", False, remedy=REMEDIES["device_records"], error=error.__class__.__name__,
                     unrecorded=[], other_usecase={})
    unrecorded = []
    other: Dict[str, str] = {}
    for name in managed:
        if name in recorded:
            continue
        unrecorded.append(name)
        if devices_table is not None:
            try:
                record = devices_table.get_item(Key={"device_id": name}).get("Item") or {}
            except ClientError:
                record = {}
            if record.get("usecase_id") and str(record["usecase_id"]) != str(usecase["usecase_id"]):
                other[name] = str(record["usecase_id"])
    return _item("device_records", not unrecorded, remedy=REMEDIES["device_records"], unrecorded=unrecorded,
                 other_usecase=other, recorded=sorted(recorded), managed=managed)


def _telemetry_source(usecase: Dict[str, Any], config_table: Any, now_s: float) -> Dict[str, Any]:
    item = config_table.get_item(Key={"pk": configdb.usecase_pk(str(usecase["usecase_id"])), "sk": configdb.SEEN_SK}).get("Item")
    ready = configdb.is_unexpired(item, now_s)
    return _item("telemetry_source", ready, remedy=REMEDIES["telemetry_source"],
                 lastSeenMs=int(item["receivedMs"]) if ready and item.get("receivedMs") is not None else None)


def _header_region(response: Any) -> Optional[str]:
    headers = ((response or {}).get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    for key, value in headers.items():
        if str(key).lower() == REGION_HEADER and value:
            return str(value)
    return None


def probe_media_location(usecase: Dict[str, Any], bucket: str, *, client: Any = None) -> RegionProbe:
    """``ListObjectsV2(Prefix='dda-telemetry/live/<uc>/', MaxKeys=1)`` through
    ``media-read``; the bucket's region from ``x-amz-bucket-region`` first
    (also from a redirect error's headers), the use case's region as the
    fallback with ``region_source: usecase``."""
    usecase_region = shared_utils.get_usecase_region(usecase)
    prefix = media.LIVE_PREFIX + str(usecase["usecase_id"]) + "/"
    error = None
    region = None
    try:
        s3 = client if client is not None else credentials.client("s3", usecase, "media-read", bucket=bucket)
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        region = _header_region(response)
    except ClientError as caught:
        region = _header_region(caught.response)
        code = _code(caught)
        if code in ("PermanentRedirect", "301") and region:
            error = None
        else:
            error = code or "ClientError"
    except (BotoCoreError, KeyError, ValueError) as caught:
        error = caught.__class__.__name__
    if error:
        return RegionProbe(False, None, None, error, None)
    if region:
        warning = CROSS_REGION_WARNING.format(bucket_region=region, usecase_region=usecase_region) \
            if region != usecase_region else None
        return RegionProbe(True, region, "header", None, warning)
    return RegionProbe(True, usecase_region, "usecase", None, HEADER_ABSENT_WARNING)


def _media_location(usecase: Dict[str, Any]) -> Dict[str, Any]:
    bucket = media.results_bucket(usecase)
    probe = probe_media_location(usecase, bucket)
    location = media.media_location(usecase)
    if not probe.ok:
        remedy = REMEDIES["media_location_missing"] if probe.error == "NoSuchBucket" else REMEDIES["media_location_denied"]
        return _item("media_location", False, remedy=remedy, error=probe.error, media_location=location)
    location.update({"region": probe.region, "region_source": probe.region_source})
    return _item("media_location", True, warning=probe.warning, media_location=location)


def _media_retention(usecase: Dict[str, Any], clip_days: Optional[int]) -> Dict[str, Any]:
    bucket = media.results_bucket(usecase)
    try:
        s3 = credentials.client("s3", usecase, "media-retention-read", bucket=bucket)
        try:
            rules = list(s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules") or [])
        except ClientError as error:
            if _code(error) != "NoSuchLifecycleConfiguration":
                raise
            rules = []
        versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    except ClientError as error:
        return _item("media_retention", False, remedy=REMEDIES["media_retention"], reason="lifecycle_unreadable",
                     error=_code(error))
    except (BotoCoreError, KeyError, ValueError) as error:
        return _item("media_retention", False, remedy=REMEDIES["media_retention"], reason="lifecycle_unreadable",
                     error=error.__class__.__name__)
    ready, reason = retention.media_retention_ready(rules, versioning, clip_days=clip_days)
    rule_days = retention.clip_rule_days(rules)
    warning = None
    if ready and clip_days is not None and rule_days is not None and rule_days != clip_days:
        warning = "the DDATelemetryClips rule expires after {0} days while telemetryClipRetentionDays is {1}".format(
            rule_days, clip_days)
    return _item("media_retention", ready, remedy=REMEDIES["media_retention"], reason=reason, warning=warning,
                 versioning=versioning or "never", clipRuleDays=rule_days)


def _registry_enforcement() -> Dict[str, Any]:
    enabled = shared_utils.registry_enforcement_enabled()
    return _item("registry_enforcement", True, warning=None if enabled else ENFORCEMENT_WARNING, enforced=enabled)


def _device_shadows(usecase: Dict[str, Any], settings: Dict[str, Any], config_table: Any) -> Dict[str, Any]:
    usecase_id = str(usecase["usecase_id"])
    expected = shadow.desired_hash(shadow.desired_document(usecase_id, settings, 0))
    recorded = device_records.usecase_devices(usecase)
    rows = configdb.query_all(config_table, KeyConditionExpression=(
        Key("pk").eq(configdb.usecase_pk(usecase_id)) & Key("sk").begins_with("DEVSHADOW#")))
    by_device = {str(row.get("device_id") or str(row["sk"])[10:]): row for row in rows}
    pending: List[str] = []
    failed: Dict[str, str] = {}
    for device_id in recorded:
        row = by_device.get(device_id)
        if row is None or str(row.get("desiredHash") or "") != expected:
            pending.append(device_id)
        elif row.get("result") != shadow.RESULT_OK:
            failed[device_id] = str(row.get("error") or "failed")
    ready = not pending and not failed
    return _item("device_shadows", ready, remedy=REMEDIES["device_shadows"], pending=pending, failed=failed,
                 devices=len(recorded))


# --------------------------------------------------------------------------- the checklist
def checklist(usecase: Dict[str, Any], settings: Optional[Dict[str, Any]], *, config_table: Any,
              devices_table: Any = None, portal_region: str, clip_days: Optional[int] = None,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The §10.1 items in order, each cached 60 s per use case."""
    now = now if now is not None else time.time()
    usecase_id = str(usecase["usecase_id"])
    settings = settings or {}
    items: List[Optional[Dict[str, Any]]] = [
        _cached((usecase_id, "ingestion_resources"), now, lambda: _ingestion_resources(usecase)),
        _cached((usecase_id, "region"), now, lambda: _region(usecase, portal_region)),
        _cached((usecase_id, "role_form"), now, lambda: _role_form(usecase)),
        _cached((usecase_id, "device_records"), now, lambda: _device_records(usecase, devices_table)),
        _cached((usecase_id, "telemetry_source"), now, lambda: _telemetry_source(usecase, config_table, now)),
        _cached((usecase_id, "media_location"), now, lambda: _media_location(usecase)),
        _cached((usecase_id, "media_retention"), now, lambda: _media_retention(usecase, clip_days)),
        _registry_enforcement(),
        _cached((usecase_id, "device_shadows", shadow.desired_hash(shadow.desired_document(usecase_id, settings, 0))),
                now, lambda: _device_shadows(usecase, settings, config_table)),
    ]
    return [item for item in items if item is not None]
