"""Request audit context and the MCP_Audit_Marker in `shared_utils`.

Spec: .kiro/specs/mcp-servers/ (design.md "3. Shared-layer and audit-log
changes", D25; tasks.md 2.1). Requirements 4.1, 4.2, 4.3, 4.5, 6.4.

Every audit entry written during an MCP-originated invocation is marked,
whether or not the writing handler passes `identity`/`event`; the binding to
`_X_AMZN_TRACE_ID` keeps a context left by an earlier invocation inert, and
entries written during browser invocations carry `channel='api'` and nothing
else new.
"""
import sys
import uuid

import pytest
from boto3.dynamodb.conditions import Attr

MCP_MARKER_FIELDS = ("channel", "mcp_tool", "mcp_server", "mcp_client",
                     "mcp_request_id")
ATTRIBUTION_FIELDS = ("username", "email", "source_ip", "user_agent",
                      "identity_source")


@pytest.fixture
def shared(aws_stack, monkeypatch):
    module = sys.modules["shared_utils"]
    monkeypatch.setattr(module, "_REQUEST_AUDIT_CONTEXT", None)
    monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
    return module


def _claims(user_id, email=None):
    claims = {"sub": user_id, "username": f"name-{user_id[:8]}",
              "client_id": "mcp-client", "token_use": "access"}
    if email is not None:
        claims["email"] = email
    return claims


def mcp_event(user_id, tool="portal_devices_list", identity_source="registry"):
    return {
        "httpMethod": "GET",
        "resource": "/devices",
        "path": "/devices",
        "requestContext": {
            "requestId": "req-mcp-1",
            "authorizer": {"claims": _claims(user_id)},
            "identity": {"sourceIp": "203.0.113.7",
                         "userAgent": "kiro/1.2.3 mcp"},
        },
        "mcp": {
            "channel": "mcp", "tool": tool,
            "server": "dda-portal-mcp/devices@1.0.0",
            "client": "kiro/1.2.3", "request_id": "req-mcp-1",
            "principal_email": "person@example.com",
            "identity_source": identity_source,
        },
    }


def browser_event(user_id):
    return {
        "httpMethod": "GET",
        "resource": "/devices",
        "path": "/devices",
        "requestContext": {
            "authorizer": {"claims": {
                "sub": user_id, "cognito:username": f"web-{user_id[:8]}",
                "email": "web@example.com"}},
            "identity": {"sourceIp": "198.51.100.4",
                         "userAgent": "Mozilla/5.0"},
        },
    }


def _write_and_read(shared, aws_stack, **kwargs):
    """Write one entry through `log_audit_event` and return the stored item."""
    writer = f"writer-{uuid.uuid4()}"
    shared.log_audit_event(writer, "list_devices", "collection", "devices",
                           "success", {"k": "v"}, **kwargs)
    items = aws_stack.tables.audit_log.scan(
        FilterExpression=Attr("user_id").eq(writer))["Items"]
    assert len(items) == 1
    return items[0]


# ------------------------------------------------------------ attribution_from

def test_attribution_from_without_block_is_unchanged(shared):
    user_id = f"user-{uuid.uuid4()}"
    result = shared.attribution_from(browser_event(user_id),
                                     identity_source="absent")
    assert tuple(result) == ATTRIBUTION_FIELDS
    assert result == {"username": f"web-{user_id[:8]}",
                      "email": "web@example.com", "source_ip": "198.51.100.4",
                      "user_agent": "Mozilla/5.0", "identity_source": "absent"}


def test_attribution_from_with_block_adds_marker(shared, monkeypatch):
    def no_registry_read(*_args, **_kwargs):
        raise AssertionError("the block's identity_source must be used")

    monkeypatch.setattr(shared, "_identity_source_for", no_registry_read)
    user_id = f"user-{uuid.uuid4()}"
    result = shared.attribution_from(mcp_event(user_id))
    assert set(result) == set(ATTRIBUTION_FIELDS) | set(MCP_MARKER_FIELDS)
    assert result["email"] == "person@example.com"
    assert result["identity_source"] == "registry"
    assert result["channel"] == "mcp"
    assert result["mcp_tool"] == "portal_devices_list"
    assert result["mcp_server"] == "dda-portal-mcp/devices@1.0.0"
    assert result["mcp_client"] == "kiro/1.2.3"
    assert result["mcp_request_id"] == "req-mcp-1"


def test_attribution_from_absent_block_values_record_unknown(shared):
    user_id = f"user-{uuid.uuid4()}"
    event = mcp_event(user_id)
    event["mcp"] = {"channel": "mcp", "identity_source": "absent"}
    result = shared.attribution_from(event)
    for field in MCP_MARKER_FIELDS[1:]:
        assert result[field] == "unknown"
    assert result["channel"] == "mcp"
    assert result["identity_source"] == "absent"


def test_claims_email_wins_over_principal_email(shared):
    user_id = f"user-{uuid.uuid4()}"
    event = mcp_event(user_id)
    event["requestContext"]["authorizer"]["claims"]["email"] = "claim@example.com"
    assert shared.attribution_from(event)["email"] == "claim@example.com"


# --------------------------------------------------- request audit context

def test_marked_entry_without_identity_or_event(shared, aws_stack, monkeypatch):
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-a")
    shared.get_user_from_event(mcp_event(f"user-{uuid.uuid4()}"))
    item = _write_and_read(shared, aws_stack)
    assert item["channel"] == "mcp"
    assert item["mcp_tool"] == "portal_devices_list"
    assert item["mcp_request_id"] == "req-mcp-1"
    assert item["email"] == "person@example.com"
    assert item["identity_source"] == "registry"
    assert item["source_ip"] == "203.0.113.7"


def test_trace_id_binding_makes_stale_context_inert(shared, aws_stack,
                                                    monkeypatch):
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-a")
    shared.get_user_from_event(mcp_event(f"user-{uuid.uuid4()}"))
    # A later invocation that never calls get_user_from_event.
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-b")
    item = _write_and_read(shared, aws_stack)
    assert item["channel"] == "api"
    for field in MCP_MARKER_FIELDS[1:]:
        assert field not in item
    for field in ATTRIBUTION_FIELDS:
        assert item[field] == "unknown"


def test_in_process_synthesized_event_keeps_the_mark(shared, aws_stack,
                                                    monkeypatch):
    # synthetic_data.retrain_session builds a marker-less event and passes it
    # to training.create_training_job within the same invocation.
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-a")
    user_id = f"user-{uuid.uuid4()}"
    shared.get_user_from_event(mcp_event(user_id))
    shared.get_user_from_event(browser_event(user_id))
    item = _write_and_read(shared, aws_stack)
    assert item["channel"] == "mcp"
    assert item["mcp_tool"] == "portal_devices_list"


def test_browser_invocation_after_mcp_one_writes_api(shared, aws_stack,
                                                     monkeypatch):
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-a")
    shared.get_user_from_event(mcp_event(f"user-{uuid.uuid4()}"))
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-b")
    user_id = f"user-{uuid.uuid4()}"
    event = browser_event(user_id)
    shared.get_user_from_event(event)
    assert _write_and_read(shared, aws_stack)["channel"] == "api"
    item = _write_and_read(shared, aws_stack, event=event)
    assert item["channel"] == "api"
    assert item["email"] == "web@example.com"
    for field in MCP_MARKER_FIELDS[1:]:
        assert field not in item


def test_context_honoured_without_trace_id(shared, aws_stack):
    # pytest has no Lambda runtime: the variable is absent on both sides.
    shared.get_user_from_event(mcp_event(f"user-{uuid.uuid4()}"))
    assert _write_and_read(shared, aws_stack)["channel"] == "mcp"


def test_explicit_identity_wins_over_context(shared, aws_stack, monkeypatch):
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-trace-a")
    shared.get_user_from_event(mcp_event(f"user-{uuid.uuid4()}"))
    identity = {"username": "u", "email": "e@example.com", "source_ip": "s",
                "user_agent": "a", "identity_source": "absent"}
    item = _write_and_read(shared, aws_stack, identity=identity)
    assert item["channel"] == "api"
    assert item["username"] == "u"


def test_explicit_mcp_event_marks_strict_and_finalize(shared, aws_stack):
    user_id = f"user-{uuid.uuid4()}"
    event = mcp_event(user_id, tool="portal_admin_whoami")
    event_id = shared.record_audit_event_strict(
        user_id, "role_change", "user_account", "target", event=event)
    shared.finalize_audit_event(event_id, "success", event=event)
    items = aws_stack.tables.audit_log.scan(
        FilterExpression=Attr("event_id").eq(event_id))["Items"]
    assert len(items) == 1
    assert items[0]["channel"] == "mcp"
    assert items[0]["mcp_tool"] == "portal_admin_whoami"
    assert items[0]["result"] == "success"


def test_signatures_unchanged(shared):
    import inspect
    assert list(inspect.signature(shared.log_audit_event).parameters) == [
        "user_id", "action", "resource_type", "resource_id", "result",
        "details", "identity", "event"]
    assert list(inspect.signature(shared.record_audit_event_strict).parameters) == [
        "user_id", "action", "resource_type", "resource_id", "result",
        "details", "identity", "event"]
    assert list(inspect.signature(shared.finalize_audit_event).parameters) == [
        "event_id", "result", "details", "identity", "event"]
    assert shared.ATTRIBUTION_FIELDS == ATTRIBUTION_FIELDS
    assert shared.MCP_MARKER_FIELDS == MCP_MARKER_FIELDS
