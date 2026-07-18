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
"""The Local_Login_Configuration: the ``LocalLoginEnabled`` component
configuration flag that gates the whole local-auth subsystem
(Requirements 11.1, 11.2, 11.4; design decision D7).

- **Startup read (11.1)**: :meth:`LocalLoginConfig.start` performs an
  immediate read of ``LocalLoginEnabled`` from the LocalServer component
  configuration via Greengrass IPC ``GetConfiguration`` (the same pattern
  as ``defect_detection_config.py``), then

- **Re-poll (11.2)**: keeps re-reading it on a 30 s background daemon
  timer, so a ``UpdateConfiguration`` on a running device takes effect on
  subsequent requests well within the required 60 s, without a component
  restart or reinstall.

- **Fail-safe default (11.4)**: local login is enabled ONLY for an explicit
  true value (boolean ``true`` or the string ``"true"``, matched
  case-insensitively because Greengrass ``DefaultConfiguration`` values
  arrive as strings). A missing key, an IPC read error, or any other value
  means disabled.

State transitions (disabled -> enabled and back) are logged exactly once
per transition; a steady state is never re-logged by the 30 s poller.

The IPC reader is injectable so tests (and Property 23) never need the
Greengrass SDK; ``awsiot`` is imported lazily inside the default reader
for the same reason.
"""
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Component configuration key holding the Local_Login_Configuration (D7).
CONFIG_KEY = "LocalLoginEnabled"

#: Re-poll interval; two polls fit comfortably within the 60 s bound (11.2).
POLL_INTERVAL_SECONDS = 30.0

#: Sentinel distinguishing "key absent from the configuration" from a stored
#: ``None`` value — both parse to disabled (11.4), but the distinction keeps
#: the reader contract explicit.
MISSING = object()


def parse_local_login_enabled(value: Any) -> bool:
    """Parse a raw ``LocalLoginEnabled`` configuration value (11.4).

    Enabled ONLY for an explicit true representation: the boolean ``True``
    or a string equal to ``"true"`` ignoring case and surrounding
    whitespace (Greengrass string-typed configs). Every other input —
    missing key, ``None``, numbers, other strings, lists, mappings —
    parses to ``False`` (disabled).

    Pure function; targeted by Property 23.
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _read_local_login_enabled_via_ipc() -> Any:
    """Default reader: fetch ``LocalLoginEnabled`` from the LocalServer
    component configuration via Greengrass IPC ``GetConfiguration``.

    Returns the raw configured value, or :data:`MISSING` when the key is
    absent. Raises on IPC failure — the caller treats any error as
    disabled (11.4). Mirrors the ``DefectDetectionConfig`` pattern
    (fresh single-use operation per call); imports are lazy so this
    module stays importable without the Greengrass SDK.
    """
    import awsiot.greengrasscoreipc  # noqa: WPS433 — lazy, see docstring

    from defect_detection_config.defect_detection_config import (
        DefectDetectionConfig,
    )

    ipc_client = awsiot.greengrasscoreipc.connect()
    config_reader = DefectDetectionConfig(ipc_client)
    component_name = config_reader.get_local_server_component_name()
    # get_component_config is uncached, so every poll observes the live
    # configuration (unlike get_local_server_config, which caches).
    component_config = config_reader.get_component_config(component_name)
    if not isinstance(component_config, dict) or CONFIG_KEY not in component_config:
        return MISSING
    return component_config[CONFIG_KEY]


class LocalLoginConfig:
    """Live view of the Local_Login_Configuration state (11.1, 11.2, 11.4).

    ``read_value`` is injectable for tests; the on-device default reads the
    component configuration over Greengrass IPC. Local login starts
    disabled until the first successful read says otherwise.
    """

    def __init__(
        self,
        read_value: Callable[[], Any] = _read_local_login_enabled_via_ipc,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ):
        self._read_value = read_value
        self._poll_interval = poll_interval
        self._enabled = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._last_read_failed = False

    @property
    def enabled(self) -> bool:
        """The current Local_Login_Configuration state."""
        with self._lock:
            return self._enabled

    def refresh(self) -> bool:
        """Read the configuration once and apply it, returning the new state.

        Any reader exception means disabled (11.4); read failures are
        logged once per failure streak, state transitions once per
        transition.
        """
        try:
            raw_value = self._read_value()
        except Exception as error:  # noqa: BLE001 — any read error ⇒ disabled
            if not self._last_read_failed:
                logger.warning(
                    "Failed to read %s from the component configuration; "
                    "treating local login as disabled: %s",
                    CONFIG_KEY,
                    error,
                )
            self._last_read_failed = True
            new_state = False
        else:
            self._last_read_failed = False
            new_state = parse_local_login_enabled(raw_value)

        with self._lock:
            previous_state = self._enabled
            self._enabled = new_state
        if new_state != previous_state:
            logger.info(
                "Local login %s (component configuration %s changed)",
                "enabled" if new_state else "disabled",
                CONFIG_KEY,
            )
        return new_state

    def start(self) -> None:
        """Read the configuration now (11.1) and start the 30 s background
        re-poll (11.2). Idempotent: a second call only refreshes."""
        self.refresh()
        with self._lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="local-login-config-poller",
                daemon=True,
            )
            self._poll_thread.start()

    def stop(self) -> None:
        """Stop the background poller (used by tests and shutdown)."""
        self._stop_event.set()
        thread = self._poll_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._poll_interval + 1)
        self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self.refresh()
            except Exception:  # pragma: no cover — refresh already guards
                logger.exception("Unexpected error in the %s poller", CONFIG_KEY)


# Module-level singleton: the login endpoint, the authorization dependency,
# and server_setup share one live configuration view. server_setup starts
# the poller (task 10.3); until then the state is the fail-safe disabled.
_default_config = LocalLoginConfig()


def get_local_login_config() -> LocalLoginConfig:
    """The shared :class:`LocalLoginConfig` singleton."""
    return _default_config


def is_local_login_enabled() -> bool:
    """The current Local_Login_Configuration state of the singleton."""
    return _default_config.enabled
