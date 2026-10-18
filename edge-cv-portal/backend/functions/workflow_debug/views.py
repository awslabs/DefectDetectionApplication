"""Presentation helpers of the debug pages (workflow-debug-mode design §5.6).

The ``amd64Nvidia`` LocalServer variant (``recipe-amd64-nvidia.yaml``) lacks
the ``mqttproxy:2`` statement that publishing on ``dda/debug/...`` needs: the
shadow read and the ``reported`` writes work, so the Portal sees the session
``active``, but every record publish is denied and the stream stays empty.
The Devices-table value for that hardware is ``target_architecture ==
"x86_64_nvidia"`` (``amd64Nvidia`` is the component suffix and never a
Device_Record value), so the viewer's empty-stream notice names this cause
exactly when the record says ``x86_64_nvidia`` and the device's ``reported``
counters show publish failures.
"""
from typing import Any, Optional

__all__ = ["UNSUPPORTED_TARGET_ARCHITECTURE", "VARIANT_NOTICE", "variant_notice"]

UNSUPPORTED_TARGET_ARCHITECTURE = "x86_64_nvidia"

VARIANT_NOTICE = (
    "This device is recorded as x86_64_nvidia, the LocalServer variant whose recipe "
    "(recipe-amd64-nvidia.yaml) lacks the mqttproxy:2 statement debug publishing needs: "
    "the session is active on the device but every record publish is denied by Greengrass "
    "IPC, which is why the stream stays empty while publishFailures climb. Debug mode is "
    "unsupported on this variant until its recipe gains mqttproxy:2."
)


def variant_notice(target_architecture: Optional[str], publish_failures: Any) -> Optional[str]:
    """The notice text when the Device_Record says ``x86_64_nvidia`` and
    ``publish_failures`` is above zero; ``None`` otherwise (another
    architecture, a missing record, no failures)."""
    if target_architecture != UNSUPPORTED_TARGET_ARCHITECTURE:
        return None
    try:
        failures = int(publish_failures or 0)
    except (TypeError, ValueError):
        return None
    return VARIANT_NOTICE if failures > 0 else None
