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
"""Property test: edge configuration parsing and transitions.

**Feature: mcp-servers, Property 11: Edge configuration parsing and transitions**

Spec: .kiro/specs/mcp-servers/ (design §5 ``config.py`` and ``supervisor.py``,
§6 ``local_auth/config.py``, Correctness Property 11; tasks.md 10.6).
Validates: Requirements 12.1, 12.2, 10.7.

For any sequence of component-configuration reads (dicts with any subset of
the keys and any values, non-dict results, read errors) returned by the
injected shared reader ``local_auth.config.read_component_config_via_ipc``:

* ``McpConfig`` is enabled (and the mutating switch on) only for an explicit
  true representation, and a read error disables both;
* ``LocalLoginConfig`` observes exactly the ``LocalLoginEnabled`` values of
  the pre-refactor reader (``config.get('LocalLoginEnabled')`` parsed by
  ``parse_local_login_enabled``; any read error disabled);
* after each poll the supervisor's child matches ``(server_enabled,
  mutating_enabled)`` and a restart happens iff the server stays enabled and
  the mutating switch (or the allowed origins) changed.
"""
import itertools
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from local_auth import config as local_auth_config
from mcp_edge import config as mcp_config
from mcp_edge import supervisor as sup
from mcp_edge.config import McpConfig, McpState

KEYS = (mcp_config.SERVER_ENABLED_KEY, mcp_config.MUTATING_ENABLED_KEY,
        local_auth_config.CONFIG_KEY)

_FLAG = st.one_of(
    st.just(True), st.just(False), st.none(),
    st.sampled_from(["true", "TRUE", " True ", "false", "yes", "1", "", "on",
                     "true\n", "truex"]),
    st.integers(-1, 2), st.lists(st.just("true"), max_size=1),
    st.dictionaries(st.just("value"), st.just(True), max_size=1),
)
_ORIGIN = st.sampled_from(["https://ops.example.com", "http://127.0.0.1:8080",
                           "https://hmi.example.com:5443"])
_ORIGINS = st.one_of(
    st.lists(_ORIGIN, max_size=3).map(",".join),
    st.lists(_ORIGIN, max_size=2).map(lambda xs: " , ".join(xs) + " ,"),
    st.none(), st.integers(), st.lists(_ORIGIN, max_size=1),
)


@st.composite
def config_reads(draw):
    kind = draw(st.sampled_from(["dict", "dict", "dict", "error", "non_dict"]))
    if kind == "error":
        return RuntimeError("ipc unavailable")
    if kind == "non_dict":
        return draw(st.sampled_from([None, [], "config", 7]))
    result = {}
    for key in KEYS:
        if draw(st.booleans()):
            result[key] = draw(_FLAG)
    if draw(st.booleans()):
        result[mcp_config.ALLOWED_ORIGINS_KEY] = draw(_ORIGINS)
    return result


def explicit_true(value):
    return value is True or (isinstance(value, str)
                             and value.strip().lower() == "true")


def expected_origins(value):
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def expected_state(read):
    if isinstance(read, Exception) or not isinstance(read, dict):
        return McpState(False, False, ())
    return McpState(explicit_true(read.get(mcp_config.SERVER_ENABLED_KEY)),
                    explicit_true(read.get(mcp_config.MUTATING_ENABLED_KEY)),
                    expected_origins(read.get(mcp_config.ALLOWED_ORIGINS_KEY)))


def expected_local_login(read):
    """The pre-refactor ``_read_local_login_enabled_via_ipc`` + ``refresh``:
    the reader normalized non-dict results to ``{}``."""
    if isinstance(read, Exception):
        return False
    config = read if isinstance(read, dict) else {}
    return explicit_true(config.get(local_auth_config.CONFIG_KEY))


def scripted_shared_reader(reads):
    """The shared reader as production sees it: raises on an IPC error and
    returns ``{}`` for a non-dict result."""
    queue = iter(reads)

    def read_component_config_via_ipc():
        read = next(queue)
        if isinstance(read, Exception):
            raise read
        return read if isinstance(read, dict) else {}
    return read_component_config_via_ipc


class _Child:
    def __init__(self, argv):
        self.argv = argv
        self.returncode = None
        self.stdout = self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _Popen:
    def __init__(self):
        self.children = []

    def __call__(self, argv, **_kwargs):
        self.children.append(_Child(argv))
        return self.children[-1]


@settings(max_examples=100, deadline=None)
@given(reads=st.lists(config_reads(), min_size=1, max_size=12))
def test_config_parsing_local_login_and_supervisor_transitions(reads):
    mcp_reader = scripted_shared_reader(reads)
    login_reader = scripted_shared_reader(reads)
    clock = itertools.count(1000.0, 0.5)
    popen = _Popen()

    with mock.patch.object(local_auth_config, "read_component_config_via_ipc",
                           mcp_reader):
        config = McpConfig()  # the default reader: the shared one
    supervisor = sup.McpSupervisor(
        config, "https://127.0.0.1:5443", popen=popen, probe=lambda _port: True,
        clock=lambda: next(clock), sleep=lambda _s: None,
        start_relays=lambda out, err: [])
    login = local_auth_config.LocalLoginConfig()

    previous = McpState(False, False, ())
    for read in reads:
        with mock.patch.object(local_auth_config, "read_component_config_via_ipc",
                               mcp_reader):
            state = config.refresh()
        with mock.patch.object(local_auth_config, "read_component_config_via_ipc",
                               login_reader):
            login_enabled = login.refresh()

        assert state == expected_state(read)
        assert config.server_enabled is state.server_enabled
        assert config.mutating_enabled is state.mutating_enabled
        assert login_enabled is expected_local_login(read)

        spawned_before = len(popen.children)
        supervisor.step()
        running = [c for c in popen.children if c.returncode is None]
        if state.server_enabled:
            assert len(running) == 1
            argv = running[0].argv
            assert ("--mutating-enabled" in argv) is state.mutating_enabled
            if state.allowed_origins:
                assert argv[argv.index("--allowed-origins") + 1] == \
                    ",".join(state.allowed_origins)
            else:
                assert "--allowed-origins" not in argv
        else:
            assert running == []

        restart = (previous.server_enabled and state.server_enabled
                   and (previous.mutating_enabled != state.mutating_enabled
                        or previous.allowed_origins != state.allowed_origins))
        assert sup.restart_requested(previous, state) is restart
        started = len(popen.children) - spawned_before
        expected_start = state.server_enabled and (not previous.server_enabled
                                                   or restart)
        assert started == (1 if expected_start else 0)
        previous = state


@settings(max_examples=100, deadline=None)
@given(read=st.one_of(config_reads(), st.sampled_from([None, [], "x", 3])))
def test_parse_state_is_total(read):
    """``parse_state`` on the raw value (an injected non-shared reader may
    return anything): disabled for non-dicts, explicit-true only otherwise."""
    if isinstance(read, Exception):
        config = McpConfig(read_config=lambda: (_ for _ in ()).throw(read))
    else:
        config = McpConfig(read_config=lambda: read)
    assert config.refresh() == expected_state(read)
