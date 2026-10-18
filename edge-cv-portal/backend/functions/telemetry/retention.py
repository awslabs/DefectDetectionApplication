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
check; the dashboards' "Media retention" item (``media_retention_ready``,
dashboards tasks.md 2.2) requires the first two ids and ignores the third.
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
    "media_retention_ready",
    "MEDIA_RETENTION_UNSET",
    "MEDIA_VERSIONED",
    "clip_rule_days",
]

#: The reasons ``media_retention_ready`` reports (the live-lease refusal
#: ``live_unavailable`` carries one of them, dashboards design §9.3).
MEDIA_RETENTION_UNSET = "media_retention_unset"
MEDIA_VERSIONED = "media_versioned"

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


def _enabled_rule_on_prefix(rules: Iterable[Any], rule_id: str, prefix: str) -> bool:
    for rule in rules or []:
        if not isinstance(rule, dict) or rule.get("ID") != rule_id:
            continue
        if str(rule.get("Status") or "") != "Enabled":
            return False
        if _rule_prefix(rule) != prefix:
            return False
        expiration = rule.get("Expiration")
        return isinstance(expiration, dict) and isinstance(expiration.get("Days"), int) and expiration["Days"] >= 1
    return False


def media_retention_ready(rules: Optional[Iterable[Any]], versioning_status: Any,
                          *, clip_days: Optional[int] = None) -> Tuple[bool, Optional[str]]:
    """``(ready, reason)`` of the dashboards' "Media retention" readiness item
    (design §10.1, §12.4): the bucket must carry enabled ``DDATelemetryClips``
    and ``DDATelemetryLive`` rules on their prefixes with an expiration in
    days (``media_retention_unset`` otherwise), and its versioning must not be
    ``Enabled`` (``media_versioned``: the hourly sweep would leave noncurrent
    frames behind). ``Suspended`` and never-enabled (``None`` or an empty
    string) versioning are both acceptable. The third id,
    ``DDATelemetryDebugImages``, is the debug mode's and is ignored here.

    ``clip_days`` is informational: when given and the clips rule expires on
    another number of days the item is still ready and the reason is
    ``None``; callers that want to show the mismatch compare
    ``clip_rule_days(rules)`` themselves."""
    rule_list = list(rules or [])
    if not (_enabled_rule_on_prefix(rule_list, CLIPS_RULE_ID, CLIPS_PREFIX)
            and _enabled_rule_on_prefix(rule_list, LIVE_RULE_ID, LIVE_PREFIX)):
        return False, MEDIA_RETENTION_UNSET
    if str(versioning_status or "") == "Enabled":
        return False, MEDIA_VERSIONED
    return True, None


def clip_rule_days(rules: Optional[Iterable[Any]]) -> Optional[int]:
    """The ``Expiration.Days`` of the bucket's ``DDATelemetryClips`` rule, or
    ``None`` when the rule is absent (the checklist reports a mismatch with
    ``telemetryClipRetentionDays`` as information, never as not-ready)."""
    for rule in rules or []:
        if isinstance(rule, dict) and rule.get("ID") == CLIPS_RULE_ID:
            expiration = rule.get("Expiration")
            if isinstance(expiration, dict) and isinstance(expiration.get("Days"), int):
                return expiration["Days"]
            return None
    return None
