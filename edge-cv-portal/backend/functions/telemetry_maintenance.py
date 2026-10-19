"""
Workflow telemetry maintenance (workflow-telemetry-dashboards design §2.2,
§9.3, §10.3, §12.4; tasks.md 3.6), run every 5 minutes by EventBridge
Scheduler. One schedule, three cadences kept by marker items in the config
table:

- **every run**: lease expiry and the ``null`` tidy-up of lapsed ``live``
  entries (a Pair whose slots have all lapsed gets ``desired.live.<wf>: null``
  through ``shadow.write_lease`` and its slot items are deleted); the use-case
  deletion cascade (an orphaned ``SETTINGS`` item -> ``desired.enabled: false``
  best effort, then the Dashboards, leases, markers and settings are deleted);
  the completion of the 202 shadow writes (``shadowSyncPending``);
- **when ``UC#<uc>/RESYNC`` is older than 15 minutes**: for an enabled use case,
  rewrite the shadow of every Device_Record whose ``DEVSHADOW#`` is missing,
  failed or carries another settings hash; rewrite the marker;
- **when ``MAINT/SWEEP`` is older than 60 minutes**: the live-frame sweep
  (``ListObjectsV2`` under ``dda-telemetry/live/<uc>/``, ``DeleteObjects`` of
  objects older than 23 h, through ``live-expiry`` credentials); rewrite the
  marker.

Use cases are found through the config table's ``by-type`` index (every
``SETTINGS`` item). One use case's failure never stops the others.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

import shared_utils
from edge_telemetry_common import credentials
from edge_telemetry_common import devices as device_records
from telemetry import configdb, leases, media, shadow

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RESYNC_AFTER_MS = 15 * 60 * 1000
SWEEP_AFTER_MS = 60 * 60 * 1000
LIVE_FRAME_MAX_AGE_S = 23 * 3600
DELETE_BATCH = 1000

_lock = threading.Lock()
_dynamodb_resource = None


def _dynamodb():
    global _dynamodb_resource
    with _lock:
        if _dynamodb_resource is None:
            _dynamodb_resource = boto3.resource("dynamodb")
        return _dynamodb_resource


def reset_clients() -> None:
    global _dynamodb_resource
    with _lock:
        _dynamodb_resource = None
    device_records.clear_cache()


def _config():
    return _dynamodb().Table(os.environ["TELEMETRY_CONFIG_TABLE"])


# --------------------------------------------------------------------------- reads
def settings_items() -> List[Dict[str, Any]]:
    """Every use case's full ``SETTINGS`` item: listed through the ``by-type``
    index (which projects only ``enabled``, ``account_id`` and ``region``) and
    then read in full."""
    listed = configdb.query_all(_config(), IndexName="by-type",
                                KeyConditionExpression=Key("gsi2pk").eq(configdb.BY_TYPE_USECASE))
    items: List[Dict[str, Any]] = []
    for entry in listed:
        full = _config().get_item(Key={"pk": entry["pk"], "sk": entry["sk"]}).get("Item")
        items.append(full or entry)
    return items


def _usecase(usecase_id: str) -> Optional[Dict[str, Any]]:
    try:
        return shared_utils.get_usecase(usecase_id)
    except ValueError:
        return None


def _partition(pk: str) -> List[Dict[str, Any]]:
    return configdb.query_all(_config(), KeyConditionExpression=Key("pk").eq(pk))


def _marker_age_ms(pk: str, sk: str, now_ms: int) -> Optional[int]:
    item = _config().get_item(Key={"pk": pk, "sk": sk}).get("Item")
    if not item or item.get("at") is None:
        return None
    try:
        return now_ms - int(item["at"])
    except (TypeError, ValueError):
        return None


def _write_marker(pk: str, sk: str, now_ms: int, **detail: Any) -> None:
    item = {"pk": pk, "sk": sk, "at": int(now_ms)}
    item.update(detail)
    _config().put_item(Item=item)


# --------------------------------------------------------------------------- lease expiry
def expire_leases(usecase: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    """Write ``null`` for every Pair whose slots have all lapsed, delete the
    lapsed slots and the expired holder items."""
    usecase_id = str(usecase["usecase_id"])
    table = _config()
    items = leases.read_partition(table, usecase_id)
    slots = leases.slots_of(items)
    lapsed = leases.lapsed_pairs(slots, now_ms)
    results: Dict[str, Any] = {}
    if lapsed:
        try:
            written = shadow.write_all_leases(usecase, [(device_id, workflow_id, None) for device_id, workflow_id, _ in lapsed])
        except Exception as error:  # noqa: BLE001 - best effort; the slots still go
            logger.warning("lease tidy-up writes failed for use case %s: %s", usecase_id, error.__class__.__name__)
            written = {}
        for device_id, workflow_id, indexes in lapsed:
            outcome = written.get((device_id, workflow_id)) or {}
            results["{0}/{1}".format(device_id, workflow_id)] = outcome.get("result", shadow.RESULT_FAILED)
            leases.tidy_pair(table, usecase_id, indexes, now_ms)
    expired_holders = 0
    for item in items:
        if str(item.get("sk") or "").startswith("HOLDER#") and int(item.get("leaseExpiresMs") or 0) <= now_ms:
            try:
                table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]},
                                  ConditionExpression="leaseExpiresMs <= :now", ExpressionAttributeValues={":now": now_ms})
                expired_holders += 1
            except ClientError:
                continue
    return {"tidied": results, "expired_holders": expired_holders}


# --------------------------------------------------------------------------- the deletion cascade
def cascade_delete(settings: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    """The use case no longer exists: switch its devices off best effort and
    delete every item the telemetry kept for it."""
    usecase_id = str(settings.get("usecase_id") or str(settings["pk"])[3:])
    table = _config()
    ghost = {"usecase_id": usecase_id, "account_id": settings.get("account_id"), "region": settings.get("region"),
             "cross_account_role_arn": settings.get("cross_account_role_arn")}
    partition = _partition(configdb.usecase_pk(usecase_id))
    devices = [str(i.get("device_id") or str(i["sk"])[10:]) for i in partition if str(i.get("sk") or "").startswith("DEVSHADOW#")]
    switched_off = 0
    if devices and ghost.get("account_id"):
        try:
            results = shadow.write_all_desired(ghost, devices, {"enabled": False}, now_ms=now_ms)
            switched_off = sum(1 for r in results.values() if r.get("result") == shadow.RESULT_OK)
        except Exception as error:  # noqa: BLE001 - the role may already be gone
            logger.warning("devices of deleted use case %s not switched off: %s", usecase_id, error.__class__.__name__)
    deleted = 0
    dashboards = configdb.query_all(table, IndexName="by-usecase", KeyConditionExpression=Key("gsi1pk").eq(configdb.usecase_pk(usecase_id)))
    for dashboard in dashboards:
        table.delete_item(Key={"pk": dashboard["pk"], "sk": dashboard["sk"]})
        deleted += 1
    for item in _partition(configdb.lease_pk(usecase_id)) + partition:
        table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
        deleted += 1
    logger.info("use case %s no longer exists: %d telemetry items deleted, %d devices switched off", usecase_id, deleted, switched_off)
    return {"usecase_id": usecase_id, "deleted": deleted, "switched_off": switched_off, "dashboards": len(dashboards)}


# --------------------------------------------------------------------------- the re-sync
def resync_shadows(usecase: Dict[str, Any], settings: Dict[str, Any], now_ms: int, *, force: bool = False) -> Dict[str, Any]:
    """Rewrite the shadow of every recorded device whose ``DEVSHADOW#`` is
    missing, failed or stale; rewrite the ``RESYNC`` marker."""
    usecase_id = str(usecase["usecase_id"])
    table = _config()
    plain = {k: v for k, v in settings.items()}
    expected = shadow.desired_hash(shadow.desired_document(usecase_id, plain, 0))
    recorded = device_records.usecase_devices(usecase)
    rows = {str(i.get("device_id") or str(i["sk"])[10:]): i for i in _partition(configdb.usecase_pk(usecase_id))
            if str(i.get("sk") or "").startswith("DEVSHADOW#")}
    stale = []
    for device_id in recorded:
        row = rows.get(device_id)
        if force or row is None or str(row.get("desiredHash") or "") != expected or row.get("result") != shadow.RESULT_OK:
            stale.append(device_id)
    results = shadow.write_all_desired(usecase, stale, plain, table=table, now_ms=now_ms) if stale else {}
    _write_marker(configdb.usecase_pk(usecase_id), configdb.RESYNC_SK, now_ms, devices=len(recorded), rewritten=len(stale))
    ok = sum(1 for r in results.values() if r.get("result") == shadow.RESULT_OK)
    if settings.get("shadowSyncPending"):
        table.update_item(Key=configdb.settings_key(usecase_id), UpdateExpression="SET shadowSyncPending = :f",
                          ExpressionAttributeValues={":f": False})
    logger.info("use case %s re-sync: %d of %d devices rewritten, %d ok", usecase_id, len(stale), len(recorded), ok)
    return {"usecase_id": usecase_id, "devices": len(recorded), "rewritten": stale, "ok": ok}


# --------------------------------------------------------------------------- the sweep
def _to_epoch(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def sweep_live_frames(usecase: Dict[str, Any], now_s: float) -> Dict[str, Any]:
    """Delete the use case's live frames older than 23 h (``live-expiry``
    credentials), only under ``dda-telemetry/live/<uc>/``."""
    usecase_id = str(usecase["usecase_id"])
    bucket = media.results_bucket(usecase)
    prefix = media.LIVE_PREFIX + usecase_id + "/"
    s3 = credentials.client("s3", usecase, "live-expiry", bucket=bucket)
    old: List[str] = []
    listed = 0
    kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        response = s3.list_objects_v2(**kwargs)
        for obj in response.get("Contents") or []:
            key = str(obj.get("Key") or "")
            if not key.startswith(prefix):
                continue
            listed += 1
            modified = _to_epoch(obj.get("LastModified"))
            if modified is not None and now_s - modified > LIVE_FRAME_MAX_AGE_S:
                old.append(key)
        if not response.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = response.get("NextContinuationToken")
    deleted = 0
    for start in range(0, len(old), DELETE_BATCH):
        batch = old[start:start + DELETE_BATCH]
        answer = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
        deleted += len(batch) - len(answer.get("Errors") or [])
    return {"usecase_id": usecase_id, "listed": listed, "deleted": deleted}


# --------------------------------------------------------------------------- entry point
def run(now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    now_s = now_ms / 1000.0
    summary: Dict[str, Any] = {"usecases": 0, "expired": {}, "cascaded": [], "resynced": [], "swept": [], "errors": 0}
    items = settings_items()
    sweep_due = (_marker_age_ms(configdb.MAINT_PK, configdb.SWEEP_SK, now_ms) or SWEEP_AFTER_MS + 1) > SWEEP_AFTER_MS
    swept_any = False
    for settings in items:
        usecase_id = str(settings.get("usecase_id") or str(settings["pk"])[3:])
        summary["usecases"] += 1
        try:
            usecase = _usecase(usecase_id)
            if usecase is None:
                summary["cascaded"].append(cascade_delete(settings, now_ms))
                continue
            expiry = expire_leases(usecase, now_ms)
            if expiry["tidied"] or expiry["expired_holders"]:
                summary["expired"][usecase_id] = expiry
            if settings.get("enabled") is True:
                age = _marker_age_ms(configdb.usecase_pk(usecase_id), configdb.RESYNC_SK, now_ms)
                if settings.get("shadowSyncPending") or age is None or age > RESYNC_AFTER_MS:
                    summary["resynced"].append(resync_shadows(usecase, settings, now_ms))
                if sweep_due:
                    try:
                        summary["swept"].append(sweep_live_frames(usecase, now_s))
                        swept_any = True
                    except (ClientError, BotoCoreError) as error:
                        summary["errors"] += 1
                        logger.error("live-frame sweep failed for use case %s: %s", usecase_id, error.__class__.__name__)
        except Exception as error:  # noqa: BLE001 - one use case's failure never stops the others
            summary["errors"] += 1
            logger.error("maintenance step failed for use case %s: %s", usecase_id, error.__class__.__name__)
    if sweep_due:
        _write_marker(configdb.MAINT_PK, configdb.SWEEP_SK, now_ms, usecases=len(summary["swept"]))
        summary["sweep_marker_written"] = True
    return summary


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    return run()
