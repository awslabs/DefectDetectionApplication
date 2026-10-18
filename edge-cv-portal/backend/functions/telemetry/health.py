"""The Now_Panel and Health_Panel rules (workflow-telemetry-dashboards design
§9.1; tasks.md 3.4).

``flow`` is computed on the server's clock from the two pointers of the
``engine`` producer: ``flowing`` when ``LAST#engine.receivedMs`` is within
``2 x summaryIntervalS + 5 s``, ``idle`` when ``LASTHB#engine.receivedMs`` is
within ``2 x idleHeartbeatS``, else ``silent``. The panel's values come from
``LAST#`` (the newest non-Heartbeat Summary), shown with "last summary <age>"
and, separately, "last run <age of lastRunMs>" ("no run in 48 h" when
absent); a Heartbeat never blanks them.

``variant_hint`` fires only for a Device_Record whose ``target_architecture``
is ``x86_64_nvidia`` (the ``amd64Nvidia`` LocalServer variant whose recipe
lacks the publish statement) when ``publishFailures`` climb while ``flow`` is
``silent``.
"""
from typing import Any, Dict, Optional

from telemetry import constants

__all__ = ["FLOWING", "IDLE", "SILENT", "VARIANT_ARCHITECTURE", "VARIANT_HINT", "flow", "variant_hint", "ages",
           "flow_window_ms"]

FLOWING = "flowing"
IDLE = "idle"
SILENT = "silent"
VARIANT_ARCHITECTURE = "x86_64_nvidia"
VARIANT_HINT = (
    "This device runs the amd64Nvidia LocalServer variant, whose recipe does not carry the IPC publish statement "
    "(mqttproxy:2, aws.greengrass#PublishToIoTCore on *): its telemetry publishes are denied and counted as "
    "publishFailures. The variant is unsupported until its recipe gains the statement; nothing on the device or "
    "in the Portal settings changes this."
)
NO_RUN_WINDOW_MS = 48 * 3600 * 1000


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def flow_window_ms(settings: Optional[Dict[str, Any]]) -> Dict[str, int]:
    settings = settings or {}
    summary_s = _int(settings.get("summaryIntervalS")) or constants.DEFAULT_SUMMARY_INTERVAL_S
    heartbeat_s = _int(settings.get("idleHeartbeatS")) or constants.DEFAULT_IDLE_HEARTBEAT_S
    return {"flowing": (2 * summary_s + 5) * 1000, "idle": 2 * heartbeat_s * 1000}


def flow(last: Optional[Dict[str, Any]], lasthb: Optional[Dict[str, Any]], settings: Optional[Dict[str, Any]],
         now_ms: int) -> str:
    windows = flow_window_ms(settings)
    last_received = _int((last or {}).get("receivedMs"))
    if last_received is not None and now_ms - last_received <= windows["flowing"]:
        return FLOWING
    heartbeat_received = _int((lasthb or {}).get("receivedMs"))
    if heartbeat_received is not None and now_ms - heartbeat_received <= windows["idle"]:
        return IDLE
    return SILENT


def ages(last: Optional[Dict[str, Any]], lasthb: Optional[Dict[str, Any]], now_ms: int) -> Dict[str, Any]:
    """``lastSummaryAgeS``, ``lastRunAgeS`` (None when no run in 48 h),
    ``heartbeatAgeS``, ``noRunIn48h``."""
    last = last or {}
    last_received = _int(last.get("receivedMs"))
    last_run = _int(last.get("lastRunMs"))
    heartbeat_received = _int((lasthb or {}).get("receivedMs"))
    no_run = last_run is None or now_ms - last_run > NO_RUN_WINDOW_MS
    return {
        "lastSummaryAgeS": None if last_received is None else max(0, (now_ms - last_received) // 1000),
        "lastRunAgeS": None if no_run else max(0, (now_ms - last_run) // 1000),
        "heartbeatAgeS": None if heartbeat_received is None else max(0, (now_ms - heartbeat_received) // 1000),
        "noRunIn48h": no_run,
    }


def variant_hint(flow_state: str, publish_failures: Any, target_architecture: Optional[str]) -> Optional[str]:
    try:
        failures = int(publish_failures or 0)
    except (TypeError, ValueError):
        failures = 0
    if flow_state == SILENT and failures > 0 and target_architecture == VARIANT_ARCHITECTURE:
        return VARIANT_HINT
    return None
