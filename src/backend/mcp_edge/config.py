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
"""The Edge_MCP_Server component configuration (backend process).

Keys of the LocalServer component configuration (design §5 ``config.py``,
Data Models "Edge component configuration"):

- ``McpServerEnabled``: start the child and serve ``/mcp``;
- ``McpMutatingToolsEnabled``: list and accept Mutating_Tools;
- ``McpAllowedOrigins`` (optional, in no recipe's ``DefaultConfiguration``):
  CSV of ``scheme://host[:port]`` values the proxy and the child accept as
  ``Origin``; absent or empty means none.

Built on the ``local_auth.config`` pattern: the values are read through the
process's one shared reader, ``local_auth.config.read_component_config_via_ipc``,
and parsed with ``parse_local_login_enabled`` (enabled only for an explicit
true value, Requirement 12.1). An immediate read at ``start()``, then a 30 s
daemon re-poll; a read failure means both switches are off (12.2); state
transitions are logged once. ``on_change`` callbacks run after a refresh
that changed any of the three values.
"""
import logging
import threading
from typing import Any, Callable, List, NamedTuple, Optional, Tuple

from local_auth import config as local_login_config

logger = logging.getLogger(__name__)

SERVER_ENABLED_KEY = "McpServerEnabled"
MUTATING_ENABLED_KEY = "McpMutatingToolsEnabled"
ALLOWED_ORIGINS_KEY = "McpAllowedOrigins"

POLL_INTERVAL_SECONDS = 30.0


class McpState(NamedTuple):
    """One observation of the MCP configuration."""

    server_enabled: bool
    mutating_enabled: bool
    allowed_origins: Tuple[str, ...]


DISABLED_STATE = McpState(False, False, ())


def parse_allowed_origins(value: Any) -> Tuple[str, ...]:
    """The CSV ``McpAllowedOrigins`` value as a tuple; anything that is not
    a string, and empty entries, contribute nothing. The entries' format is
    checked by the child's argument parser."""
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_state(component_config: Any) -> McpState:
    """The MCP state of one component-configuration mapping. Pure."""
    if not isinstance(component_config, dict):
        return DISABLED_STATE
    parse = local_login_config.parse_local_login_enabled
    return McpState(
        server_enabled=parse(component_config.get(SERVER_ENABLED_KEY)),
        mutating_enabled=parse(component_config.get(MUTATING_ENABLED_KEY)),
        allowed_origins=parse_allowed_origins(
            component_config.get(ALLOWED_ORIGINS_KEY)),
    )


def _read_via_shared_reader() -> dict:
    return local_login_config.read_component_config_via_ipc()


class McpConfig:
    """Live view of the MCP component configuration.

    ``read_config`` is injectable (Property 11); the default reads through
    the process's shared IPC reader. Starts disabled until the first
    successful read says otherwise.
    """

    def __init__(
        self,
        read_config: Callable[[], Any] = _read_via_shared_reader,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ):
        self._read_config = read_config
        self._poll_interval = poll_interval
        self._state = DISABLED_STATE
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._last_read_failed = False
        self._callbacks: List[Callable[[McpState, McpState], None]] = []

    # ---------------------------------------------------------- state

    @property
    def state(self) -> McpState:
        with self._lock:
            return self._state

    @property
    def server_enabled(self) -> bool:
        return self.state.server_enabled

    @property
    def mutating_enabled(self) -> bool:
        return self.state.mutating_enabled

    @property
    def allowed_origins(self) -> List[str]:
        return list(self.state.allowed_origins)

    def on_change(self, callback: Callable[[McpState, McpState], None]) -> None:
        """Call ``callback(previous, current)`` after every refresh that
        changed the state."""
        self._callbacks.append(callback)

    # -------------------------------------------------------- polling

    def refresh(self) -> McpState:
        """Read the configuration once and apply it; never raises."""
        try:
            new_state = parse_state(self._read_config())
        except Exception as error:  # noqa: BLE001 — any read error ⇒ disabled
            if not self._last_read_failed:
                logger.warning(
                    "Failed to read the MCP keys from the component "
                    "configuration; treating the MCP server as disabled: %s",
                    error,
                )
            self._last_read_failed = True
            new_state = DISABLED_STATE
        else:
            self._last_read_failed = False

        with self._lock:
            previous = self._state
            self._state = new_state
        if new_state != previous:
            logger.info(
                "MCP configuration changed: server %s, mutating tools %s, "
                "%d allowed origin(s)",
                "enabled" if new_state.server_enabled else "disabled",
                "enabled" if new_state.mutating_enabled else "disabled",
                len(new_state.allowed_origins),
            )
            for callback in list(self._callbacks):
                try:
                    callback(previous, new_state)
                except Exception:  # noqa: BLE001 — a callback never stops the poller
                    logger.exception("MCP configuration change callback failed")
        return new_state

    def start(self) -> None:
        """Read now and start the 30 s re-poll. Idempotent."""
        self.refresh()
        with self._lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="mcp-config-poller", daemon=True
            )
            self._poll_thread.start()

    def stop(self) -> None:
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
                logger.exception("Unexpected error in the MCP configuration poller")
