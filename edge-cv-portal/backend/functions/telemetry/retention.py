"""The ``dda-telemetry/`` lifecycle rules of a Results_Bucket
(workflow-telemetry-dashboards design §12.4; workflow-debug-mode design §1.3,
tasks.md 3.3).

Three rule ids are owned here and applied by the operator through
``edge-cv-portal/scripts/telemetry_lifecycle.py`` (the Portal holds no
``s3:PutLifecycleConfiguration`` grant): ``DDATelemetryClips`` (clip days,
default 90), ``DDATelemetryLive`` (1 day) and ``DDATelemetryDebugImages``
(debug days, default 7, range 1-30). ``merge_telemetry_rules`` replaces the
three ids in place, appends those absent and keeps every other rule verbatim,
exactly as ``tuning_settings.merge_lifecycle_rules`` treats its own ids, so a
bucket carrying the workflow-tuning rules or an operator's rules is never
disturbed. ``debug_images_rule_present`` is the debug mode's images readiness
check; the dashboards' "Media retention" item reads the first two ids only.
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "TELEMETRY_RULE_IDS",
    "CLIPS_RULE_ID",
    "LIVE_RULE_ID",
    "DEBUG_IMAGES_RULE_ID",
    "TELEMETRY_PREFIX",
    "DEBUG_IMAGES_PREFIX",
    "DEFAULT_CLIP_DAYS",
    "DEFAULT_DEBUG_DAYS",
    "DEBUG_DAYS_RANGE",
    "telemetry_rules",
    "merge_telemetry_rules",
    "debug_images_rule_present",
]

CLIPS_RULE_ID = "DDATelemetryClips"
LIVE_RULE_ID = "DDATelemetryLive"
DEBUG_IMAGES_RULE_ID = "DDATelemetryDebugImages"
TELEMETRY_RULE_IDS = (CLIPS_RULE_ID, LIVE_RULE_ID, DEBUG_IMAGES_RULE_ID)

TELEMETRY_PREFIX = "dda-telemetry/"
CLIPS_PREFIX = TELEMETRY_PREFIX + "clips/"
LIVE_PREFIX = TELEMETRY_PREFIX + "live/"
DEBUG_IMAGES_PREFIX = TELEMETRY_PREFIX + "debug/"

DEFAULT_CLIP_DAYS = 90
LIVE_DAYS = 1
DEFAULT_DEBUG_DAYS = 7
DEBUG_DAYS_RANGE = (1, 30)


def _rule(rule_id: str, prefix: str, days: int) -> Dict[str, Any]:
    return {
        "ID": rule_id,
        "Filter": {"Prefix": prefix},
        "Status": "Enabled",
        "Expiration": {"Days": int(days)},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
    }


def telemetry_rules(*, clip_days: int = DEFAULT_CLIP_DAYS,
                    debug_days: int = DEFAULT_DEBUG_DAYS) -> List[Dict[str, Any]]:
    """The three rules as the specifications write them, in id order."""
    if not isinstance(clip_days, int) or clip_days < 1:
        raise ValueError("clip_days must be a positive integer")
    low, high = DEBUG_DAYS_RANGE
    if not isinstance(debug_days, int) or not low <= debug_days <= high:
        raise ValueError("debug_days must be an integer between {0} and {1}".format(low, high))
    return [
        _rule(CLIPS_RULE_ID, CLIPS_PREFIX, clip_days),
        _rule(LIVE_RULE_ID, LIVE_PREFIX, LIVE_DAYS),
        _rule(DEBUG_IMAGES_RULE_ID, DEBUG_IMAGES_PREFIX, debug_days),
    ]


def merge_telemetry_rules(existing_rules: Optional[Iterable[Any]], *,
                          clip_days: int = DEFAULT_CLIP_DAYS,
                          debug_days: int = DEFAULT_DEBUG_DAYS) -> Tuple[List[Any], bool]:
    """``(rules, changed)``: the bucket's lifecycle rules with the three
    telemetry rules in force.

    Every rule the bucket already carries is preserved verbatim and in order
    except the three ids owned here, which are replaced in place (or appended
    when absent; a duplicate of one of ours is dropped, one rule per id).
    ``changed`` is False when the bucket already carries exactly these rules,
    so the caller can skip the write and stay idempotent."""
    desired = {rule["ID"]: rule for rule in telemetry_rules(clip_days=clip_days, debug_days=debug_days)}
    merged: List[Any] = []
    seen: List[str] = []
    changed = False
    for rule in list(existing_rules or []):
        rule_id = rule.get("ID") if isinstance(rule, dict) else None
        if rule_id in desired:
            if rule_id in seen:
                changed = True
                continue
            seen.append(rule_id)
            if rule != desired[rule_id]:
                changed = True
            merged.append(desired[rule_id])
        else:
            merged.append(rule)
    for rule_id in TELEMETRY_RULE_IDS:
        if rule_id not in seen:
            merged.append(desired[rule_id])
            changed = True
    return merged, changed


def _rule_prefix(rule: Dict[str, Any]) -> Optional[str]:
    """The prefix a lifecycle rule applies to, in any of the shapes S3 returns."""
    if "Prefix" in rule and isinstance(rule.get("Prefix"), str):
        return rule["Prefix"]
    rule_filter = rule.get("Filter")
    if isinstance(rule_filter, dict):
        if isinstance(rule_filter.get("Prefix"), str):
            return rule_filter["Prefix"]
        combined = rule_filter.get("And")
        if isinstance(combined, dict) and isinstance(combined.get("Prefix"), str):
            return combined["Prefix"]
    return None


def debug_images_rule_present(rules: Optional[Iterable[Any]]) -> bool:
    """True when the bucket carries an enabled ``DDATelemetryDebugImages``
    rule on the ``dda-telemetry/debug/`` prefix with an ``Expiration`` in
    days (the debug mode's images readiness check, workflow-debug-mode §7.2)."""
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("ID") != DEBUG_IMAGES_RULE_ID:
            continue
        if str(rule.get("Status") or "") != "Enabled":
            return False
        if _rule_prefix(rule) != DEBUG_IMAGES_PREFIX:
            return False
        expiration = rule.get("Expiration")
        return isinstance(expiration, dict) and isinstance(expiration.get("Days"), int) and expiration["Days"] >= 1
    return False
