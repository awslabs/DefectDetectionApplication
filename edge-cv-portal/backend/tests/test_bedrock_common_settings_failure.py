"""
Unit test: a raising settings-table read inside
`bedrock_common.get_bedrock_configuration` logs a warning and falls back
to the workflow-generation defaults; no exception escapes.

Task 1.5 (spec: custom-node-code-assist).

The DynamoDB resource on the module is replaced with a stub whose
`Table(...).get_item` raises botocore ClientError, so the failure path is
exercised deterministically without moto.

_Requirements: 4.5_
"""
import logging

import pytest
from botocore.exceptions import ClientError

import bedrock_common


class _RaisingTable:
    """Stub DynamoDB Table whose get_item always raises ClientError."""

    def get_item(self, **kwargs):
        raise ClientError(
            error_response={
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "Throughput exceeded",
                }
            },
            operation_name="GetItem",
        )


class _RaisingDynamoResource:
    def Table(self, name):
        return _RaisingTable()


@pytest.fixture
def failing_settings_read(monkeypatch):
    """Point bedrock_common at a settings table whose read raises."""
    monkeypatch.setattr(bedrock_common, "SETTINGS_TABLE", "test-settings-raising")
    monkeypatch.setattr(bedrock_common, "dynamodb", _RaisingDynamoResource())


def test_settings_read_failure_falls_back_to_defaults(
    failing_settings_read, caplog
):
    """ClientError from the settings read yields the defaults, a warning,
    and no escaping exception (Requirement 4.5)."""
    with caplog.at_level(logging.WARNING):
        config = bedrock_common.get_bedrock_configuration()  # must not raise

    # The returned configuration is exactly the workflow-generation
    # defaults, with the timeout already an int clamped to [1, 240].
    expected = dict(bedrock_common.DEFAULT_BEDROCK_CONFIG)
    expected["timeout_seconds"] = int(expected["timeout_seconds"])
    assert config == expected

    timeout = config["timeout_seconds"]
    assert isinstance(timeout, int) and not isinstance(timeout, bool)
    assert 1 <= timeout <= bedrock_common.MAX_TIMEOUT_SECONDS

    # A warning about the failed read was logged.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "Could not read Bedrock configuration" in r.getMessage()
        for r in warnings
    )


def test_settings_read_failure_result_matches_no_table_configured(
    failing_settings_read, monkeypatch
):
    """The failure fallback is semantically identical to having no
    settings table configured at all (the pure-defaults path)."""
    failing_config = bedrock_common.get_bedrock_configuration()

    monkeypatch.setattr(bedrock_common, "SETTINGS_TABLE", None)
    default_config = bedrock_common.get_bedrock_configuration()

    assert failing_config == default_config
