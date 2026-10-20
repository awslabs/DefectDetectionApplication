"""The series route's rules (workflow-telemetry-dashboards design §8.3, §18;
tasks.md 3.4).

- ``resolution_for(from_ms, to_ms)``: ``to - from <= 6 h`` -> minute items (at
  most 360 points per Device), ``<= 60 days`` -> hour items (1,440),
  ``<= 400 days`` -> day items (400), beyond -> ``RangeTooLong``; the
  1,440-points-per-Device cap is a consequence of the rule;
- ``validate(params, dashboard, template)``: ``from``/``to`` required epoch ms
  (``to > from``), an optional ``device_id`` of the Dashboard
  (``invalid_device``), optional ``measures`` (1-64 catalog ids all in the
  template, else ``invalid_measure`` naming the first offender; absent means
  every template measure);
- ``query_device``: one eventually consistent ``Query`` per Device over the
  chosen prefix with a ``ProjectionExpression`` of the requested measures and
  the ``expires_at`` filter; ``points`` renders ``{t, measures, events}``.
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr, Key

from telemetry import fold, schemas

__all__ = [
    "MINUTE", "HOUR", "DAY", "SIX_HOURS_MS", "SIXTY_DAYS_MS", "FOUR_HUNDRED_DAYS_MS", "POINT_CAPS", "RESOLUTION_PREFIX",
    "RangeTooLong", "InvalidQuery", "resolution_for", "bucket_start", "key_range", "validate", "query_device", "points",
    "refresh_seconds",
]

MINUTE, HOUR, DAY = "minute", "hour", "day"
SIX_HOURS_MS = 6 * fold.HOUR_MS
SIXTY_DAYS_MS = 60 * fold.DAY_MS
FOUR_HUNDRED_DAYS_MS = 400 * fold.DAY_MS
POINT_CAPS = {MINUTE: 360, HOUR: 1440, DAY: 400}
RESOLUTION_PREFIX = {MINUTE: "M#", HOUR: "H#", DAY: "D#"}
#: The frontend's refresh cadence by resolution (§8.3), served by /telemetry/schemas.
REFRESH_SECONDS = {MINUTE: 60, HOUR: 300, DAY: 900}
MAX_MEASURES = 64


class RangeTooLong(ValueError):
    """``to - from`` is over 400 days."""


class InvalidQuery(ValueError):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def resolution_for(from_ms: int, to_ms: int) -> str:
    span = int(to_ms) - int(from_ms)
    if span <= 0:
        raise InvalidQuery("invalid_range", "to must be after from")
    if span <= SIX_HOURS_MS:
        return MINUTE
    if span <= SIXTY_DAYS_MS:
        return HOUR
    if span <= FOUR_HUNDRED_DAYS_MS:
        return DAY
    raise RangeTooLong("the range may span at most 400 days")


def refresh_seconds(resolution: str) -> int:
    return REFRESH_SECONDS[resolution]


def bucket_start(resolution: str, ms: int) -> int:
    size = {MINUTE: fold.MINUTE_MS, HOUR: fold.HOUR_MS, DAY: fold.DAY_MS}[resolution]
    return (int(ms) // size) * size


def _key(resolution: str, ms: int) -> str:
    if resolution == MINUTE:
        return "M#" + fold.minute_key(ms)
    if resolution == HOUR:
        return "H#" + fold.hour_key(ms)
    return "D#" + fold.day_key(ms)


def key_range(resolution: str, from_ms: int, to_ms: int) -> Tuple[str, str]:
    """The ``sk BETWEEN`` bounds: the bucket containing ``from`` through the
    bucket containing ``to`` (its end exclusive: a ``to`` exactly on a bucket
    boundary does not fetch the next bucket)."""
    low = _key(resolution, bucket_start(resolution, from_ms))
    high_ms = bucket_start(resolution, int(to_ms) - 1)
    high = _key(resolution, max(high_ms, bucket_start(resolution, from_ms)))
    return low, high


def _bucket_of_key(sk: str) -> Optional[int]:
    prefix, _, key = str(sk).partition("#")
    try:
        if prefix == "M":
            return fold.minute_of_key(key)
        if prefix == "H":
            return fold.hour_of_key(key)
        if prefix == "D":
            return fold.day_of_key(key)
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------- validation
def _int_param(params: Dict[str, Any], name: str, required: bool) -> Optional[int]:
    raw = params.get(name)
    if raw in (None, ""):
        if required:
            raise InvalidQuery("invalid_range", "{0} is required".format(name), field=name)
        return None
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidQuery("invalid_range", "{0} must be an integer".format(name), field=name)
    if value < 0:
        raise InvalidQuery("invalid_range", "{0} must be positive".format(name), field=name)
    return value


def validate(params: Optional[Dict[str, Any]], dashboard: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    """``{from, to, resolution, device_ids, measures, all_measures}`` or raise
    ``InvalidQuery`` / ``RangeTooLong``."""
    params = params or {}
    from_ms = _int_param(params, "from", True)
    to_ms = _int_param(params, "to", True)
    resolution = resolution_for(from_ms, to_ms)
    device_ids = list(dashboard.get("device_ids") or [])
    device_id = params.get("device_id")
    if device_id not in (None, ""):
        if device_id not in device_ids:
            raise InvalidQuery("invalid_device", "device_id is not a device of this dashboard", device_id=str(device_id)[:128])
        device_ids = [device_id]
    template_measures = schemas.template_measures(template)
    raw = params.get("measures")
    if raw in (None, ""):
        measures = list(template_measures)
        all_measures = True
    else:
        measures = [m.strip() for m in str(raw).split(",") if m.strip()]
        if not 1 <= len(measures) <= MAX_MEASURES:
            raise InvalidQuery("invalid_measure", "measures must name 1 to {0} catalog ids".format(MAX_MEASURES))
        for measure in measures:
            if measure not in template_measures:
                raise InvalidQuery("invalid_measure", "measure is not in the dashboard's template", measure=measure[:64])
        all_measures = False
    return {"from": from_ms, "to": to_ms, "resolution": resolution, "device_ids": device_ids,
            "measures": measures, "all_measures": all_measures}


# --------------------------------------------------------------------------- reads
def query_device(table: Any, pk: str, resolution: str, from_ms: int, to_ms: int, measures: Optional[Iterable[str]],
                 now_s: float) -> List[Dict[str, Any]]:
    """One ``Query`` over the Pair's items at the resolution, projecting the
    requested measures (``None`` projects every measure)."""
    low, high = key_range(resolution, from_ms, to_ms)
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").between(low, high),
        "FilterExpression": Attr("expires_at").gt(int(now_s)),
    }
    names = {"#sk": "sk", "#ev": "events", "#gaps": "gaps", "#sf": "summariesFolded", "#ms": "measures", "#ex": "expires_at"}
    projection = ["#sk", "#ev", "#gaps", "#sf", "#ex"]
    if measures is None:
        projection.append("#ms")
    else:
        for index, measure in enumerate(measures):
            alias = "#m{0}".format(index)
            names[alias] = measure
            projection.append("#ms.{0}".format(alias))
    kwargs["ProjectionExpression"] = ", ".join(projection)
    kwargs["ExpressionAttributeNames"] = names
    found: List[Dict[str, Any]] = []
    while True:
        response = table.query(**kwargs)
        found.extend(response.get("Items", []) or [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    found.sort(key=lambda item: str(item.get("sk") or ""))
    return found


def _plain(value: Any) -> Any:
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
    except Exception:  # noqa: BLE001
        return value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def points(items: Iterable[Dict[str, Any]], resolution: str) -> List[Dict[str, Any]]:
    """``[{t, measures, events, gaps}]`` ordered by bucket start."""
    out = []
    for item in items:
        start = _bucket_of_key(str(item.get("sk") or ""))
        if start is None:
            continue
        out.append({"t": start, "measures": _plain(item.get("measures") or {}), "events": _plain(item.get("events") or {}),
                    "gaps": int(item.get("gaps") or 0), "summaries": int(item.get("summariesFolded") or 0)})
    out.sort(key=lambda p: p["t"])
    return out[:POINT_CAPS[resolution]]
