# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Debounced, thread-isolated reporter of the ``dda-model-status`` named
shadow (spec: model-gpu-fallback-visibility, design File 5 / Decisions 4-5).

The feature-config read path (``/feature-configurations`` and
``/feature-configurations/gpu-status``) hands :func:`report` the
device-GPU-status snapshot it just computed. The reporter:

- compares the canonical JSON of the snapshot against the last WRITTEN one
  and skips identical snapshots;
- debounces writes to at most one per :data:`DEBOUNCE_SECONDS` (~30 s,
  env-overridable via ``DDA_MODEL_STATUS_SHADOW_DEBOUNCE_SECONDS``);
- performs the actual shadow update on a short-lived daemon thread through
  the existing ``IoTShadowAccessor`` — a single write in flight at a time
  (lock + flag);
- swallows and logs EVERY exception: a shadow problem never affects the
  endpoint response (the ``get_features_vllm`` isolation precedent).

Reported state only — this shadow has no desired state and no delta
subscription (Decision 4: one-way telemetry). The camera-sync payload
convention applies: the accessor's ``update_thing_shadow_state_request``
wraps its payload in ``{"state": ...}`` itself, so callers pass
``{"reported": snapshot}`` (see ``camera_sync/agent.py``).

Accepted limitation (Decision 5, recorded honestly): on a device where
nothing polls the feature-config endpoints the shadow goes stale; the
device-side truth (logs, sidecar, local API) is unaffected and the shadow's
``updatedAt`` is visible in the portal.

Testability: ``_clock`` (monotonic time source), :data:`DEBOUNCE_SECONDS`,
and ``_accessor_override`` are module-level and patchable, so the debounce
and the accessor call are testable without real sleeps or Greengrass IPC.
The accessor is resolved LAZILY (import inside the worker) so this module
imports cleanly host-side without the device-only ``awsiot`` stack.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

#: Reported-only named shadow carrying the model GPU-fallback status
#: (keep in sync with the portal's deployments.py auto-include list).
MODEL_STATUS_SHADOW_NAME = "dda-model-status"

#: Minimum interval between shadow writes, seconds (design Decision 5).
DEBOUNCE_SECONDS = float(
    os.environ.get("DDA_MODEL_STATUS_SHADOW_DEBOUNCE_SECONDS", "30")
)

#: Monotonic time source; patchable in tests (no real sleeps needed).
_clock = time.monotonic

#: Test seam: when set, used instead of server_setup.iot_shadow_accessor.
_accessor_override = None

_lock = threading.Lock()
_last_written_canonical = None
_last_write_monotonic = None
_write_in_flight = False
#: Last spawned writer thread (tests join it to await the write).
_write_thread = None


def _get_accessor():
    """The shadow accessor: the test override when installed, otherwise the
    process-wide ``IoTShadowAccessor`` from ``server_setup`` — imported
    LAZILY because server_setup pulls the device-only Greengrass/awsiot
    stack (this module must import host-side)."""
    if _accessor_override is not None:
        return _accessor_override
    from utils import server_setup
    return server_setup.iot_shadow_accessor


def report(snapshot):
    """Report ``snapshot`` (the ``device_gpu_status`` shape) into the
    ``dda-model-status`` shadow — debounced, change-gated, asynchronous,
    and failure-isolated. Never raises.
    """
    global _last_written_canonical, _last_write_monotonic, \
        _write_in_flight, _write_thread
    try:
        thing_name = os.environ.get("AWS_IOT_THING_NAME")
        if not thing_name:
            # Host/dev context without a device identity: nothing to report
            # to (the camera-sync convention sources the thing name from
            # this variable too).
            log.debug(
                "model-status shadow report skipped: AWS_IOT_THING_NAME "
                "is not set"
            )
            return
        canonical = json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"), default=str
        )
        with _lock:
            if _write_in_flight:
                return  # single in-flight write (Decision 5)
            if canonical == _last_written_canonical:
                return  # unchanged since the last written snapshot
            now = _clock()
            if (_last_write_monotonic is not None
                    and now - _last_write_monotonic < DEBOUNCE_SECONDS):
                return  # within the debounce window
            _write_in_flight = True
            _last_write_monotonic = now
            _last_written_canonical = canonical
            thread = threading.Thread(
                target=_write_shadow,
                args=(thing_name, snapshot, canonical),
                name="dda-model-status-shadow-write",
                daemon=True,
            )
            _write_thread = thread
        thread.start()
    except Exception as e:  # never let a shadow problem touch the response
        log.warning(f"model-status shadow report failed: {e}")
        with _lock:
            _write_in_flight = False


def _write_shadow(thing_name, snapshot, canonical):
    """Worker: one shadow update through the accessor. Every exception is
    logged and swallowed; a failed write un-pins the last-written snapshot
    so a later (post-debounce) report retries."""
    global _last_written_canonical, _write_in_flight
    try:
        accessor = _get_accessor()
        # REAL accessor convention (dao/iotshadow/IoTShadowAccessor.py):
        # update_thing_shadow_state_request wraps the payload in
        # {"state": ...} itself — pass {"reported": snapshot}, exactly like
        # the camera sync (camera_sync/agent.py _report_current_document).
        accessor.update_thing_shadow_state_request(
            thing_name, MODEL_STATUS_SHADOW_NAME, {"reported": snapshot}
        )
    except Exception as e:
        log.warning(
            f"model-status shadow write to {MODEL_STATUS_SHADOW_NAME} "
            f"failed (endpoint response unaffected): {e}"
        )
        with _lock:
            if _last_written_canonical == canonical:
                _last_written_canonical = None
    finally:
        with _lock:
            _write_in_flight = False


def _reset_state():
    """Test seam: clear the debounce/change-detection state."""
    global _last_written_canonical, _last_write_monotonic, \
        _write_in_flight, _write_thread
    with _lock:
        _last_written_canonical = None
        _last_write_monotonic = None
        _write_in_flight = False
        _write_thread = None
