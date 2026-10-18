"""
Workflow telemetry roll-up (workflow-telemetry-dashboards design §7.3;
tasks.md 3.3): fed by the DynamoDB streams of the summaries and events tables
(batch 100, window 60 s, ``ReportBatchItemFailures``).

It folds only run records: a stream record is considered when its ``pk``
begins with ``P#`` and its ``sk`` with ``S#`` (a Summary) or ``E#`` (an
Event) and its event is ``INSERT`` or ``MODIFY``; the pointer items, the
Clip_Index items and ``REMOVE`` events are skipped without a read. For each
affected ``(pk, minute)`` (a Summary's minute is the minute of ``eventMs -
1``, an Event's the minute of ``eventMs``):

- ``Query`` the Summary_Store for the Pair over the receive-time keys within
  ±5 minutes of the minute (the tolerance the ingest's ``eventMs`` rule
  allows), keep the items whose ``eventMs - 1`` falls in the minute,
  de-duplicate on (producer, instance, seq), ``fold_minute`` them with the
  Pair's Events of the minute (``E#<minute>`` keys), compute the authoritative
  ``gaps`` from the whole window (``count_gaps``), ``PutItem`` the minute item
  (7-day TTL);
- recompute the hour from its ``M#`` items (400 days) and the UTC day from
  its ``H#`` items (400 days).

Every read filters ``expires_at > :now``. Recompute-and-overwrite makes a
retried or bisected batch idempotent at all three levels; a failed
``(pk, minute)`` reports every record that mapped to it.
"""
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer

from telemetry import fold, records

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MINUTE_TTL_SECONDS = 7 * 24 * 3600
HOUR_TTL_SECONDS = 400 * 24 * 3600
DAY_TTL_SECONDS = 400 * 24 * 3600

_deserializer = TypeDeserializer()
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


def _summaries():
    return _dynamodb().Table(os.environ["TELEMETRY_SUMMARIES_TABLE"])


def _events():
    return _dynamodb().Table(os.environ["TELEMETRY_EVENTS_TABLE"])


def _aggregates():
    return _dynamodb().Table(os.environ["TELEMETRY_AGGREGATES_TABLE"])


# --------------------------------------------------------------------------- stream records
def _plain(value: Any) -> Any:
    try:
        return _deserializer.deserialize(value)
    except (TypeError, AttributeError, KeyError):
        return None


def work_item(record: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """``(pk, minute)`` for a stream record that produces work, else None."""
    if str(record.get("eventName") or "") not in ("INSERT", "MODIFY"):
        return None
    body = record.get("dynamodb") or {}
    keys = body.get("Keys") or {}
    pk = _plain(keys.get("pk")) if "pk" in keys else None
    sk = _plain(keys.get("sk")) if "sk" in keys else None
    if not isinstance(pk, str) or not isinstance(sk, str) or not pk.startswith("P#"):
        return None
    if sk.startswith("S#"):
        kind = "summary"
    elif sk.startswith("E#"):
        kind = "event"
    else:
        return None
    image = body.get("NewImage") or {}
    event_ms = _plain(image.get("eventMs")) if "eventMs" in image else None
    if event_ms is None:
        try:
            event_ms = int(sk.split("#")[1])
        except (IndexError, ValueError):
            return None
    try:
        return pk, fold.bucket_minute(kind, int(event_ms))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- reads
def _query_all(table: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    while True:
        response = table.query(**kwargs)
        found.extend(response.get("Items", []) or [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return found
        kwargs["ExclusiveStartKey"] = last_key


def summary_window(pk: str, minute: int, now_s: float) -> List[Dict[str, Any]]:
    """The Summary_Store items of the Pair whose receive-time keys lie within
    ±5 minutes of the minute (the whole window; the caller filters)."""
    start = minute - fold.SUMMARY_EVENT_TIME_TOLERANCE_MS
    end = minute + fold.MINUTE_MS + fold.SUMMARY_EVENT_TIME_TOLERANCE_MS
    return _query_all(
        _summaries(),
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(
            records.summary_sk(start, "00000000"), "S#{0:013d}#~".format(end)),
        FilterExpression=Attr("expires_at").gt(int(now_s)))


def events_of_minute(pk: str, minute: int, now_s: float) -> List[Dict[str, Any]]:
    return _query_all(
        _events(),
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(
            "E#{0:013d}#".format(minute), "E#{0:013d}#~".format(minute + fold.MINUTE_MS - 1)),
        FilterExpression=Attr("expires_at").gt(int(now_s)))


def _aggregate_range(pk: str, low: str, high: str, now_s: float) -> List[Dict[str, Any]]:
    return _query_all(
        _aggregates(),
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(low, high),
        FilterExpression=Attr("expires_at").gt(int(now_s)))


# --------------------------------------------------------------------------- the three levels
def _put(pk: str, sk: str, folded: Dict[str, Any], ttl_seconds: int, now_s: float, computed_ms: int) -> Dict[str, Any]:
    item = {"pk": pk, "sk": sk, "measures": folded["measures"], "weights": folded.get("weights") or {},
            "events": folded["events"], "summariesFolded": int(folded["summariesFolded"]), "gaps": int(folded["gaps"]),
            "computedAt": int(computed_ms), "expires_at": int(now_s) + int(ttl_seconds)}
    _aggregates().put_item(Item=item)
    return item


def recompute_minute(pk: str, minute: int, now_s: float) -> Dict[str, Any]:
    window = summary_window(pk, minute, now_s)
    in_minute = [item for item in window if fold.bucket_minute("summary", int(item.get("eventMs") or 0)) == minute]
    gaps = fold.count_gaps(window, minute)
    events = events_of_minute(pk, minute, now_s)
    folded = fold.fold_minute(in_minute, events, gaps=gaps)
    return _put(pk, "M#" + fold.minute_key(minute), folded, MINUTE_TTL_SECONDS, now_s, int(now_s * 1000))


def recompute_hour(pk: str, hour_ms: int, now_s: float) -> Dict[str, Any]:
    key = fold.hour_key(hour_ms)
    minutes = _aggregate_range(pk, "M#{0}00".format(key), "M#{0}59".format(key), now_s)
    minutes.sort(key=lambda i: str(i.get("sk")))
    return _put(pk, "H#" + key, fold.fold_items(minutes), HOUR_TTL_SECONDS, now_s, int(now_s * 1000))


def recompute_day(pk: str, day_ms: int, now_s: float) -> Dict[str, Any]:
    key = fold.day_key(day_ms)
    hours = _aggregate_range(pk, "H#{0}00".format(key), "H#{0}23".format(key), now_s)
    hours.sort(key=lambda i: str(i.get("sk")))
    return _put(pk, "D#" + key, fold.fold_items(hours), DAY_TTL_SECONDS, now_s, int(now_s * 1000))


def recompute(pk: str, minute: int, now_s: float) -> Dict[str, Any]:
    """The minute, then its hour, then its UTC day."""
    minute_item = recompute_minute(pk, minute, now_s)
    hour_item = recompute_hour(pk, minute, now_s)
    day_item = recompute_day(pk, minute, now_s)
    return {"minute": minute_item["sk"], "hour": hour_item["sk"], "day": day_item["sk"],
            "summariesFolded": minute_item["summariesFolded"], "gaps": minute_item["gaps"]}


# --------------------------------------------------------------------------- the batch
def process(event: Dict[str, Any], *, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Collect the affected ``(pk, minute)`` set of a stream batch, recompute
    each, and report the records of every failed pair."""
    now_s = clock()
    affected: Dict[Tuple[str, int], List[str]] = {}
    skipped = 0
    for record in event.get("Records") or []:
        item = work_item(record)
        sequence = str(((record.get("dynamodb") or {}).get("SequenceNumber")) or record.get("eventID") or "")
        if item is None:
            skipped += 1
            continue
        affected.setdefault(item, []).append(sequence)
    failures: List[Dict[str, str]] = []
    done: List[Dict[str, Any]] = []
    for (pk, minute), sequences in sorted(affected.items()):
        try:
            done.append(recompute(pk, minute, now_s))
        except Exception as error:  # noqa: BLE001 - one pair's failure is reported, the others proceed
            logger.error("roll-up failed for %s minute %s: %s", pk, fold.minute_key(minute), error.__class__.__name__)
            failures.extend({"itemIdentifier": sequence} for sequence in sequences if sequence)
    logger.info("roll-up: %d pair-minutes recomputed, %d records skipped, %d failed", len(done), skipped, len(failures))
    return {"batchItemFailures": failures, "recomputed": done, "skipped": skipped}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    result = process(event)
    return {"batchItemFailures": result["batchItemFailures"]}
