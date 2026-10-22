"""`GET /audit-logs` channel and MCP-tool filters.

Spec: .kiro/specs/mcp-servers/ (design.md "3. Shared-layer and audit-log
changes", the `functions/audit_logs.py:get_audit_logs` bullet; tasks.md 2.3).
Requirement 4.6.

`channel=api` keeps entries written before the MCP_Audit_Marker existed
(no `channel` attribute) visible; `channel=mcp` selects marked entries;
`mcp_tool=<name>` selects one tool's entries. No table or GSI change.
"""
import json
import sys
import uuid

import boto3
import pytest

REGION = "us-east-1"
# A table of this module's own, so the scan's Limit never meets entries the
# session-shared audit table accumulates from other modules.
TABLE = "test-audit-log-channel-filter"
UC_A = "uc-channel-a"
UC_B = "uc-channel-b"

ENTRIES = (
    # (event id, usecase, channel or None, mcp_tool or None)
    ("pre-upgrade-a", UC_A, None, None),
    ("api-a", UC_A, "api", None),
    ("mcp-list-a", UC_A, "mcp", "portal_devices_list"),
    ("mcp-get-b", UC_B, "mcp", "portal_devices_get"),
    ("pre-upgrade-b", UC_B, None, None),
)


@pytest.fixture(scope="module")
def audit_logs(aws_stack):
    client = boto3.client("dynamodb", region_name=REGION)
    try:
        client.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"},
                       {"AttributeName": "timestamp", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "N"}],
            BillingMode="PAY_PER_REQUEST")
    except client.exceptions.ResourceInUseException:
        pass
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    for index, (event_id, usecase_id, channel, tool) in enumerate(ENTRIES):
        item = {"event_id": event_id, "timestamp": 1_000 + index,
                "user_id": "writer", "action": "list_devices",
                "resource_type": "collection", "resource_id": "devices",
                "result": "success", "usecase_id": usecase_id}
        if channel is not None:
            item["channel"] = channel
        if tool is not None:
            item.update(mcp_tool=tool, mcp_server="dda-portal-mcp/devices@1.0.0",
                        mcp_client="kiro/1.2.3", mcp_request_id=f"r-{index}")
        table.put_item(Item=item)
    sys.modules.pop("audit_logs", None)
    import audit_logs as module
    original = module.AUDIT_LOG_TABLE
    module.AUDIT_LOG_TABLE = TABLE
    yield module
    module.AUDIT_LOG_TABLE = original


@pytest.fixture
def as_admin(audit_logs, monkeypatch):
    monkeypatch.setattr(audit_logs.rbac_manager, "is_portal_admin",
                        lambda *_a, **_k: True)
    return audit_logs


@pytest.fixture
def as_member_of_a(audit_logs, monkeypatch):
    monkeypatch.setattr(audit_logs.rbac_manager, "is_portal_admin",
                        lambda *_a, **_k: False)
    monkeypatch.setattr(audit_logs.rbac_manager, "get_accessible_usecases",
                        lambda *_a, **_k: [UC_A])
    return audit_logs


def call(module, **query):
    event = {
        "httpMethod": "GET", "path": "/audit-logs", "resource": "/audit-logs",
        "queryStringParameters": query or None,
        "requestContext": {"authorizer": {"claims": {
            "sub": f"user-{uuid.uuid4()}", "email": "viewer@example.com",
            "cognito:username": "viewer"}}},
    }
    response = module.handler(event, None)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])
    return body, sorted(log["event_id"] for log in body["logs"])


def test_no_channel_filter_returns_everything(as_admin):
    _, ids = call(as_admin)
    assert ids == sorted(entry[0] for entry in ENTRIES)


def test_api_filter_keeps_pre_upgrade_entries(as_admin):
    _, ids = call(as_admin, channel="api")
    assert ids == ["api-a", "pre-upgrade-a", "pre-upgrade-b"]


def test_mcp_filter_selects_marked_entries(as_admin):
    _, ids = call(as_admin, channel="mcp")
    assert ids == ["mcp-get-b", "mcp-list-a"]


def test_mcp_tool_filter(as_admin):
    _, ids = call(as_admin, mcp_tool="portal_devices_list")
    assert ids == ["mcp-list-a"]
    _, ids = call(as_admin, channel="mcp", mcp_tool="portal_devices_get")
    assert ids == ["mcp-get-b"]
    _, ids = call(as_admin, channel="api", mcp_tool="portal_devices_get")
    assert ids == []


def test_unrecognised_channel_value_adds_no_filter(as_admin):
    _, ids = call(as_admin, channel="other")
    assert ids == sorted(entry[0] for entry in ENTRIES)


def test_api_clause_is_parenthesised_with_the_usecase_scope(as_member_of_a):
    # The non-admin use-case clause and the api clause are ANDed; the OR of
    # the api clause must not escape its parentheses.
    _, ids = call(as_member_of_a, channel="api")
    assert ids == ["api-a", "pre-upgrade-a"]
    _, ids = call(as_member_of_a, channel="mcp")
    assert ids == ["mcp-list-a"]


def test_filter_expression_text(as_admin, monkeypatch):
    captured = {}

    class Recorder:
        def scan(self, **kwargs):
            captured.update(kwargs)
            return {"Items": [], "ScannedCount": 0}

    monkeypatch.setattr(as_admin.dynamodb, "Table", lambda _name: Recorder())
    call(as_admin, channel="api", mcp_tool="portal_devices_list")
    assert captured["FilterExpression"] == (
        "(attribute_not_exists(#channel) OR #channel = :channel)"
        " AND #mcp_tool = :mcp_tool")
    assert captured["ExpressionAttributeNames"] == {
        "#channel": "channel", "#mcp_tool": "mcp_tool"}
    assert captured["ExpressionAttributeValues"] == {
        ":channel": "api", ":mcp_tool": "portal_devices_list"}
    captured.clear()
    call(as_admin, channel="mcp")
    assert captured["FilterExpression"] == "#channel = :channel"
    assert captured["ExpressionAttributeValues"] == {":channel": "mcp"}


def test_available_actions_name_the_mcp_actions(as_admin):
    body, _ = call(as_admin)
    for action in ("mcp_tool_call", "authentication_failed",
                   "authorization_unavailable"):
        assert action in body["available_actions"]
