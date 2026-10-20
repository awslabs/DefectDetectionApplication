"""Constants of the workflow telemetry Portal functions
(workflow-telemetry-dashboards design §5.4, §15.2).

``PERSON_CLASSES`` is the person-class list the Portal writes into every
``dda-workflow-telemetry`` shadow document: a constant in this phase, not a
setting (``PUT .../settings`` refuses a ``personClasses`` key), so a later
configurable value would already satisfy the agent's validation.
"""

__all__ = [
    "PERSON_CLASSES",
    "SHADOW_NAME",
    "LIVE_LEASE_SECONDS",
    "LIVE_LEASE_DEFAULTS",
    "DEFAULT_SUMMARY_INTERVAL_S",
    "DEFAULT_IDLE_HEARTBEAT_S",
    "SUMMARY_INTERVAL_RANGE",
    "IDLE_HEARTBEAT_RANGE",
    "DEVICE_LIVE_LIMIT",
    "MAX_DEVICES_PER_DASHBOARD",
    "MAX_DASHBOARDS_PER_USECASE",
]

PERSON_CLASSES = ["person", "human"]

#: The Control_Shadow the agent reads (design §5.4).
SHADOW_NAME = "dda-workflow-telemetry"

#: A live lease lasts 60 s and is renewed every 20 s (design §9.3).
LIVE_LEASE_SECONDS = 60
#: The lease entry the Portal writes beside expiresAt, bucket, region, prefix.
LIVE_LEASE_DEFAULTS = {"maxFps": 1, "maxWidth": 1280, "maxHeight": 720, "maxBytes": 307200}

DEFAULT_SUMMARY_INTERVAL_S = 10
DEFAULT_IDLE_HEARTBEAT_S = 60
SUMMARY_INTERVAL_RANGE = (5, 60)
IDLE_HEARTBEAT_RANGE = (30, 300)

#: Concurrently leased Workflows per Device (design §9.3, §15.4).
DEVICE_LIVE_LIMIT = 8
MAX_DEVICES_PER_DASHBOARD = 20
MAX_DASHBOARDS_PER_USECASE = 100
