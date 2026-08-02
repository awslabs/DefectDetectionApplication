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
"""Unit tests for local_auth.config.

Feature: portal-user-manager (Requirements 11.1, 11.2, 11.4).
"""
import logging
import time

import pytest

from local_auth.config import (
    CONFIG_KEY,
    MISSING,
    POLL_INTERVAL_SECONDS,
    LocalLoginConfig,
    get_local_login_config,
    parse_local_login_enabled,
)


class TestParseLocalLoginEnabled:
    """Requirement 11.4: enabled only for explicit true representations."""

    @pytest.mark.parametrize(
        "value", [True, "true", "True", "TRUE", "  true  "]
    )
    def test_explicit_true_values_enable(self, value):
        assert parse_local_login_enabled(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            MISSING,          # key absent from the configuration
            None,
            False,
            "false",
            "",
            "yes",
            "1",
            "truthy",
            1,
            0,
            ["true"],
            {"enabled": True},
        ],
    )
    def test_everything_else_disables(self, value):
        assert parse_local_login_enabled(value) is False


class TestRefresh:
    def test_startup_read_applies_configured_value(self):
        # Requirement 11.1: the state comes from the component configuration.
        config = LocalLoginConfig(read_value=lambda: "true")
        assert config.enabled is False  # fail-safe before the first read
        assert config.refresh() is True
        assert config.enabled is True

    def test_missing_key_is_disabled(self):
        config = LocalLoginConfig(read_value=lambda: MISSING)
        config.refresh()
        assert config.enabled is False

    def test_read_error_is_disabled(self):
        # Requirement 11.4: unreadable configuration ⇒ disabled.
        def boom():
            raise RuntimeError("ipc unavailable")

        config = LocalLoginConfig(read_value=boom)
        # Force enabled first so the error visibly transitions to disabled.
        config._read_value = lambda: True
        config.refresh()
        assert config.enabled is True
        config._read_value = boom
        config.refresh()
        assert config.enabled is False

    def test_runtime_change_applies_on_next_refresh(self):
        # Requirement 11.2: a changed value takes effect without restart.
        values = iter(["false", "true", "false"])
        config = LocalLoginConfig(read_value=lambda: next(values))
        assert config.refresh() is False
        assert config.refresh() is True
        assert config.refresh() is False


class _RecordingHandler(logging.Handler):
    """Collects log records (the suite conftest repurposes ``caplog``)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def config_log_records():
    config_logger = logging.getLogger("local_auth.config")
    handler = _RecordingHandler()
    previous_level = config_logger.level
    config_logger.addHandler(handler)
    config_logger.setLevel(logging.DEBUG)
    yield handler.records
    config_logger.removeHandler(handler)
    config_logger.setLevel(previous_level)


class TestTransitionLogging:
    def test_transition_logged_once_and_steady_state_silent(
        self, config_log_records
    ):
        values = iter(["false", "true", "true", "true"])
        config = LocalLoginConfig(read_value=lambda: next(values))
        config.refresh()  # disabled -> disabled: no transition
        config.refresh()  # disabled -> enabled: logged
        config.refresh()  # steady enabled: silent
        config.refresh()  # steady enabled: silent
        transitions = [
            record
            for record in config_log_records
            if record.levelno == logging.INFO
            and CONFIG_KEY in record.getMessage()
        ]
        assert len(transitions) == 1
        assert "enabled" in transitions[0].getMessage()

    def test_read_failure_logged_once_per_streak(self, config_log_records):
        def boom():
            raise RuntimeError("ipc unavailable")

        config = LocalLoginConfig(read_value=boom)
        config.refresh()
        config.refresh()
        config.refresh()
        failures = [
            record
            for record in config_log_records
            if "Failed to read" in record.getMessage()
        ]
        assert len(failures) == 1


class TestPoller:
    def test_start_reads_immediately_and_repolls(self):
        values = {"value": "false"}
        config = LocalLoginConfig(
            read_value=lambda: values["value"], poll_interval=0.05
        )
        config.start()
        try:
            assert config.enabled is False
            values["value"] = "true"
            deadline = time.monotonic() + 2
            while not config.enabled and time.monotonic() < deadline:
                time.sleep(0.02)
            assert config.enabled is True
        finally:
            config.stop()

    def test_start_is_idempotent(self):
        config = LocalLoginConfig(read_value=lambda: "false", poll_interval=0.05)
        config.start()
        try:
            first_thread = config._poll_thread
            config.start()
            assert config._poll_thread is first_thread
        finally:
            config.stop()


class TestSingleton:
    def test_shared_singleton(self):
        assert get_local_login_config() is get_local_login_config()
        assert isinstance(get_local_login_config(), LocalLoginConfig)

    def test_default_poll_interval_within_60s_bound(self):
        # Requirement 11.2: two polls fit within the 60 s application bound.
        assert POLL_INTERVAL_SECONDS <= 30.0
