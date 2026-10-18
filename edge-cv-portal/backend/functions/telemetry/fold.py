"""The aggregate fold of the workflow telemetry (workflow-telemetry-dashboards
design §7.3, §8.1; tasks.md 3.2).

- ``SUMMARY_EVENT_TIME_TOLERANCE_MS``: the one constant the ingest's
  ``eventMs`` rule (``records.event_time``) and the roll-up's receive-time
  query window share, so every stored Summary of a minute lies inside the
  query (§4.4, §7.3);
- ``bucket_minute(kind, event_ms)``: a Summary buckets by the minute of
  ``eventMs - 1`` (an ``intervalEnd`` exactly on the minute belongs to the
  minute it covers), an Event by the minute of ``eventMs``;
- ``fold_minute(summaries, events, gaps)``: the minute item from the raw
  Summary_Store items of a Pair and its Events, driven by the catalog's
  folds (sum, max, mean, p50mean, p95max, worst, last, count; ``perKey``
  measures per map key; the owner-filtered ``ext.ppe`` measures), with
  in-memory de-duplication on (producer, instance, seq);
- ``fold_items(items)``: the hour item from minute items and the day item
  from hour items, by the same rule (sums and maxima are exact at every
  level; means are weighted by the folded counts; ``last`` follows the
  latest item; ``gaps`` and the event counts sum);
- ``count_gaps(window_items, minute)``: the authoritative gap count of a
  minute over the whole ±5-minute window (out-of-order arrivals never count).
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telemetry import schemas

__all__ = [
    "SUMMARY_EVENT_TIME_TOLERANCE_MS", "MINUTE_MS", "HOUR_MS", "DAY_MS",
    "bucket_minute", "minute_key", "hour_key", "day_key", "minute_of_key", "hour_of_key", "day_of_key",
    "dedupe", "summary_measures", "fold_minute", "fold_items", "count_gaps", "measure_specs",
]

SUMMARY_EVENT_TIME_TOLERANCE_MS = 300_000
MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


# --------------------------------------------------------------------------- buckets and keys
def bucket_minute(kind: str, event_ms: int) -> int:
    """The start (epoch ms) of the minute a message folds into."""
    event_ms = int(event_ms)
    if kind == "summary":
        event_ms -= 1
    return (event_ms // MINUTE_MS) * MINUTE_MS


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)


def minute_key(minute_ms: int) -> str:
    """``yyyyMMddHHmm`` of a minute start."""
    return _utc(minute_ms).strftime("%Y%m%d%H%M")


def hour_key(ms: int) -> str:
    """``yyyyMMddHH`` of the hour containing ``ms``."""
    return _utc(ms).strftime("%Y%m%d%H")


def day_key(ms: int) -> str:
    """``yyyyMMdd`` of the UTC day containing ``ms``."""
    return _utc(ms).strftime("%Y%m%d")


def _parse(key: str, fmt: str) -> int:
    return int(datetime.strptime(key, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def minute_of_key(key: str) -> int:
    return _parse(key, "%Y%m%d%H%M")


def hour_of_key(key: str) -> int:
    return _parse(key, "%Y%m%d%H")


def day_of_key(key: str) -> int:
    return _parse(key, "%Y%m%d")


# --------------------------------------------------------------------------- the catalog view
def measure_specs() -> List[Dict[str, Any]]:
    return schemas.catalog_measures()


def _summary_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    fields = item.get("summary")
    return fields if isinstance(fields, dict) else {}


def _producer_name(item: Dict[str, Any]) -> str:
    producer = item.get("producer")
    if isinstance(producer, dict):
        return str(producer.get("name") or "")
    if producer is None:
        producer = _summary_fields(item).get("producer")
        if isinstance(producer, dict):
            return str(producer.get("name") or "")
    return str(producer or "")


def _instance(item: Dict[str, Any]) -> str:
    instance = item.get("instance")
    if instance is None:
        producer = _summary_fields(item).get("producer")
        if isinstance(producer, dict):
            instance = producer.get("instance")
    return str(instance or "")


def _seq(item: Dict[str, Any]) -> Optional[int]:
    seq = item.get("seq")
    if seq is None:
        seq = _summary_fields(item).get("seq")
    try:
        return int(seq) if seq is not None else None
    except (TypeError, ValueError):
        return None


def _event_ms(item: Dict[str, Any]) -> int:
    value = item.get("eventMs")
    if value is None:
        value = _summary_fields(item).get("intervalEnd", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_idle(item: Dict[str, Any]) -> bool:
    idle = item.get("idle")
    if idle is None:
        idle = _summary_fields(item).get("idle", False)
    return bool(idle)


def dedupe(summaries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One item per (producer, instance, seq): a device retransmission or an
    IoT double delivery is stored twice and folded once. The copy with the
    earliest ``receivedMs`` is kept; items without a seq are all kept."""
    kept: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    unkeyed: List[Dict[str, Any]] = []
    for item in summaries:
        seq = _seq(item)
        if seq is None:
            unkeyed.append(item)
            continue
        key = (_producer_name(item), _instance(item), seq)
        current = kept.get(key)
        if current is None or int(item.get("receivedMs") or 0) < int(current.get("receivedMs") or 0):
            kept[key] = item
    ordered = sorted(kept.values(), key=lambda i: (_event_ms(i), _producer_name(i), _instance(i), _seq(i) or 0))
    return ordered + unkeyed


def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summary_measures(fields: Dict[str, Any], producer_name: str) -> Dict[str, Any]:
    """The per-Summary measure values of the catalog's summary measures,
    owner-filtered (blocks a producer does not own are ignored). ``perKey``
    measures yield ``{key: value}``; ``worst`` yields the state string;
    ``last`` yields the raw value; derived measures are absent."""
    owned, _dropped = schemas.drop_unowned_blocks(fields, producer_name)
    values: Dict[str, Any] = {}
    for measure in measure_specs():
        if measure["source"] != "summary" or measure.get("derived"):
            continue
        value = schemas.resolve_path(owned, measure["path"])
        if value is None:
            continue
        if measure["perKey"]:
            if isinstance(value, dict) and value:
                numbers = {k: _to_number(v) for k, v in value.items()}
                values[measure["id"]] = {k: v for k, v in numbers.items() if v is not None}
            continue
        if measure["fold"] == "worst":
            values[measure["id"]] = value if isinstance(value, str) else None
            if values[measure["id"]] is None:
                del values[measure["id"]]
            continue
        if measure["fold"] == "last":
            values[measure["id"]] = value
            continue
        number = _to_number(value)
        if number is not None:
            values[measure["id"]] = number
    return values


# --------------------------------------------------------------------------- the folds
class _Accumulator:
    """Folds one measure's samples by the catalog rule."""

    def __init__(self, fold: str) -> None:
        self.fold = fold
        self.total = 0.0
        self.weight = 0.0
        self.maximum: Optional[float] = None
        self.count = 0
        self.last: Any = None
        self.last_order: Optional[Tuple[Any, ...]] = None
        self.states: List[str] = []

    def add(self, value: Any, order: Tuple[Any, ...], weight: float = 1.0) -> None:
        if self.fold == "worst":
            if isinstance(value, str):
                self.states.append(value)
            return
        if self.fold == "last":
            if self.last_order is None or order >= self.last_order:
                self.last, self.last_order = value, order
            return
        number = _to_number(value)
        if number is None:
            return
        self.count += 1
        if self.fold in ("sum", "count"):
            self.total += number
        elif self.fold in ("max", "p95max"):
            self.maximum = number if self.maximum is None else max(self.maximum, number)
        elif self.fold in ("mean", "p50mean"):
            if weight <= 0:
                weight = 1.0
            self.total += number * weight
            self.weight += weight

    def result(self) -> Any:
        if self.fold == "worst":
            return schemas.worst_state(self.states)
        if self.fold == "last":
            return self.last
        if self.count == 0:
            return None
        if self.fold in ("sum", "count"):
            return _tidy(self.total)
        if self.fold in ("max", "p95max"):
            return _tidy(self.maximum)
        return _tidy(self.total / self.weight) if self.weight else None


def _tidy(number: Optional[float]) -> Any:
    if number is None:
        return None
    if float(number).is_integer():
        return int(number)
    return round(float(number), 6)


def _weight_of(measure: Dict[str, Any], fields: Dict[str, Any]) -> float:
    path = measure.get("weight")
    if not path:
        return 1.0
    number = _to_number(schemas.resolve_path(fields, path))
    return number if number is not None and number > 0 else 1.0


def fold_minute(summaries: Iterable[Dict[str, Any]], events: Iterable[Dict[str, Any]],
                gaps: int = 0) -> Dict[str, Any]:
    """The minute item of a Pair: ``{measures, weights, events, summariesFolded, gaps}``.

    ``summaries`` are Summary_Store items (``summary`` map, ``producer``,
    ``instance``, ``seq``, ``eventMs``, ``idle``); Heartbeats contribute the
    ``telemetry`` counters they carry and are not counted in
    ``summariesFolded``. ``events`` are Event_Store items (``type`` at the
    top level or inside ``event``). ``gaps`` is the authoritative count of
    :func:`count_gaps`, folded by sum at every level."""
    items = dedupe(summaries)
    accumulators: Dict[str, _Accumulator] = {}
    per_key: Dict[str, Dict[str, _Accumulator]] = {}
    weights: Dict[str, float] = {}
    folded = 0
    specs = {m["id"]: m for m in measure_specs()}
    for item in items:
        fields = _summary_fields(item)
        producer = _producer_name(item)
        order = (_event_ms(item), _seq(item) or 0)
        if not _is_idle(item):
            folded += 1
        for measure_id, value in summary_measures(fields, producer).items():
            measure = specs[measure_id]
            weight = _weight_of(measure, fields)
            if measure["perKey"]:
                bucket = per_key.setdefault(measure_id, {})
                for key, number in value.items():
                    bucket.setdefault(key, _Accumulator(measure["fold"])).add(number, order, weight)
                continue
            accumulators.setdefault(measure_id, _Accumulator(measure["fold"])).add(value, order, weight)
            if measure["fold"] == "p50mean":
                weights[measure_id] = weights.get(measure_id, 0.0) + weight
    measures: Dict[str, Any] = {}
    for measure_id, accumulator in accumulators.items():
        value = accumulator.result()
        if value is not None:
            measures[measure_id] = value
    for measure_id, keyed in per_key.items():
        values = {key: acc.result() for key, acc in keyed.items()}
        values = {key: value for key, value in values.items() if value is not None}
        if values:
            measures[measure_id] = values
    event_counts: Dict[str, int] = {}
    for event in events:
        event_type = event.get("type")
        if event_type is None and isinstance(event.get("event"), dict):
            event_type = event["event"].get("type")
        if event_type:
            event_counts[str(event_type)] = event_counts.get(str(event_type), 0) + 1
    measures["summaries"] = folded
    measures["gaps"] = int(gaps)
    for measure in specs.values():
        if measure["source"] == "event":
            count = event_counts.get(measure["eventType"], 0)
            if count:
                measures[measure["id"]] = count
    return {
        "measures": measures,
        "weights": {k: _tidy(v) for k, v in weights.items()},
        "events": event_counts,
        "summariesFolded": folded,
        "gaps": int(gaps),
    }


def fold_items(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """An hour item from minute items, or a day item from hour items, by the
    catalog rule: sums and maxima exactly; means weighted by the items'
    folded counts (``p50mean`` by the recorded weights); ``worst`` over the
    items; ``last`` from the latest item (by its ``sk``, else input order);
    the event counts, ``summariesFolded`` and ``gaps`` summed."""
    specs = {m["id"]: m for m in measure_specs()}
    accumulators: Dict[str, _Accumulator] = {}
    per_key: Dict[str, Dict[str, _Accumulator]] = {}
    weights: Dict[str, float] = {}
    event_counts: Dict[str, int] = {}
    folded = 0
    gaps = 0
    ordered = list(items)
    for index, item in enumerate(ordered):
        order = (str(item.get("sk") or ""), index)
        item_folded = int(item.get("summariesFolded") or 0)
        folded += item_folded
        gaps += int(item.get("gaps") or 0)
        for event_type, count in (item.get("events") or {}).items():
            event_counts[str(event_type)] = event_counts.get(str(event_type), 0) + int(count)
        item_weights = item.get("weights") or {}
        for measure_id, value in (item.get("measures") or {}).items():
            measure = specs.get(measure_id)
            if measure is None or measure_id in ("summaries", "gaps") or measure["source"] == "event":
                continue
            fold = measure["fold"]
            if fold == "p50mean":
                weight = float(item_weights.get(measure_id) or item_folded or 1)
                weights[measure_id] = weights.get(measure_id, 0.0) + weight
            elif fold == "mean":
                weight = float(item_folded or 1)
            else:
                weight = 1.0
            if measure["perKey"]:
                if isinstance(value, dict):
                    bucket = per_key.setdefault(measure_id, {})
                    for key, number in value.items():
                        bucket.setdefault(key, _Accumulator(fold)).add(number, order, weight)
                continue
            accumulators.setdefault(measure_id, _Accumulator(fold)).add(value, order, weight)
    measures: Dict[str, Any] = {}
    for measure_id, accumulator in accumulators.items():
        value = accumulator.result()
        if value is not None:
            measures[measure_id] = value
    for measure_id, keyed in per_key.items():
        values = {key: acc.result() for key, acc in keyed.items()}
        values = {key: value for key, value in values.items() if value is not None}
        if values:
            measures[measure_id] = values
    measures["summaries"] = folded
    measures["gaps"] = gaps
    for measure in specs.values():
        if measure["source"] == "event":
            count = event_counts.get(measure["eventType"], 0)
            if count:
                measures[measure["id"]] = count
    return {
        "measures": measures,
        "weights": {k: _tidy(v) for k, v in weights.items()},
        "events": event_counts,
        "summariesFolded": folded,
        "gaps": gaps,
    }


def count_gaps(window_items: Iterable[Dict[str, Any]], minute: int) -> int:
    """The authoritative ``gaps`` of ``minute`` (its start, epoch ms): for each
    (producer, instance) sort the distinct stored ``seq`` values of the whole
    window and, for each consecutive pair whose later member's ``eventMs``
    falls in the minute, add ``later.seq - earlier.seq - 1``. Messages that
    arrived out of order yield no gap; a retransmission is one seq."""
    by_producer: Dict[Tuple[str, str], Dict[int, int]] = {}
    for item in window_items:
        seq = _seq(item)
        if seq is None:
            continue
        key = (_producer_name(item), _instance(item))
        seqs = by_producer.setdefault(key, {})
        event_ms = _event_ms(item)
        if seq not in seqs or event_ms < seqs[seq]:
            seqs[seq] = event_ms
    gaps = 0
    for seqs in by_producer.values():
        ordered = sorted(seqs)
        for earlier, later in zip(ordered, ordered[1:]):
            if bucket_minute("summary", seqs[later]) == minute:
                gaps += later - earlier - 1
    return gaps
