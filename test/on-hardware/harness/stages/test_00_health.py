"""Stage 00 — backend health, readiness, and device identity (Reqs 3.1–3.3).

Runs first (module naming ``test_00_*``) to establish the device baseline, so
downstream stage failures are attributable to features rather than a dead
backend (Req 3.1). The stage:

* asserts the Backend_API answers its health/readiness surface
  (``/system-health`` and ``/dda-component-status``);
* captures the Target_Device identity — the LocalServer component version as
  reported by the device — into the session ``device_identity`` fixture and
  the Results_Bundle (Req 3.2);
* under ``auth_enabled``, verifies the authenticated surface (Req 3.3). The
  fail-fast part of the requirement lives in the session ``edge_client``
  fixture: a failing login handshake aborts the run before any test executes,
  and credential values never reach a message (the client redacts the
  ``Authorization`` header; only the credential *reference* is ever named).

With no device configured the stage collects and skips cleanly through the
``harness_target`` fixture (Req 1.5).
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

pytestmark = pytest.mark.stage("health")

#: Healthy status reported by ``/dda-component-status`` (Backend_API:
#: ``utils/constants.GET_DDA_COMPONENT_STATUS_HEALTHY``).
COMPONENT_STATUS_HEALTHY = "HEALTHY"

#: Keys under which a device payload may carry the LocalServer version. The
#: real device reports it as ``localServerVersion`` in ``/system-health``;
#: the component-status surface is probed first per the stage design.
_VERSION_KEYS = ("localServerVersion", "local_server_version", "version")


def _reported_version(payload: Any) -> Optional[str]:
    """The LocalServer version carried by ``payload``, if any."""
    if not isinstance(payload, dict):
        return None
    for key in _VERSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def test_system_health_answers(edge_client):
    """The Backend_API answers its health surface with a well-formed payload
    (Req 3.1). A non-2xx answer raises ``DeviceApiError`` with the bounded
    response body, which is the failure diagnostic (Req 8.2)."""
    health = edge_client.system_health()
    assert isinstance(
        health, dict
    ), f"/system-health answered with a non-object payload: {health!r}"


def test_dda_components_healthy(edge_client):
    """The device reports its DDA components healthy — the readiness baseline
    downstream stages build on (Req 3.1)."""
    status = edge_client.component_status()
    assert isinstance(
        status, dict
    ), f"/dda-component-status answered with a non-object payload: {status!r}"
    reported = status.get("status")
    assert reported == COMPONENT_STATUS_HEALTHY, (
        f"device reports DDA component status {reported!r} (expected "
        f"{COMPONENT_STATUS_HEALTHY!r}); full payload: {status!r}"
    )


def test_device_identity_recorded(edge_client, device_identity, results_plugin):
    """The Target_Device identity — LocalServer version as reported by the
    device — is captured into the Results_Bundle (Req 3.2).

    The component-status surface is probed first; the ``/system-health``
    payload (which the real device stamps with ``localServerVersion``) is the
    fallback. Later stages depend on ``device_identity``, which keeps this
    stage first (Req 3.1).
    """
    version = _reported_version(edge_client.component_status())
    if version is None:
        version = _reported_version(edge_client.system_health())
    assert version is not None, (
        "device reported no LocalServer version on /dda-component-status or "
        "/system-health; the Results_Bundle requires the device identity "
        "(Req 3.2)"
    )
    device_identity["local_server_version"] = version
    if results_plugin is not None:
        results_plugin.set_local_server_version(version)


@pytest.mark.capability("auth_enabled")
def test_authenticated_surface(harness_target, edge_client):
    """Under ``auth_enabled`` the device agrees local auth is on and an
    authenticated call succeeds (Req 3.3).

    The session ``edge_client`` has already performed the login handshake and
    carries the bearer token; bad credentials failed the run fast before this
    test. No credential value can appear here — only the profile claim and
    the device observation are named.
    """
    status = edge_client.auth_status()
    enabled = status.get("localLoginEnabled")
    assert enabled, (
        f"device profile {harness_target.name!r} grants 'auth_enabled' but "
        f"the device reports localLoginEnabled={enabled!r} — profile claim "
        "and device observation disagree"
    )
    # The session token authenticates a protected surface.
    edge_client.feature_configurations()
