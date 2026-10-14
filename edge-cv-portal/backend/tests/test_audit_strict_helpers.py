"""
Unit tests for the strict two-phase audit helpers in shared_utils
(spec: portal-user-manager, task 1.1).

record_audit_event_strict / finalize_audit_event implement the
audit-before-effect protocol: a 'pending' entry is written (raising on
failure) before the guarded operation, then finalized to
success/failure/rejected. Details are sanitized against the
password/verifier/hash/temp* denylist before any write.

_Requirements: 6.1, 6.3, 6.4, 6.5_
"""
import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def shared(aws_stack):
    """The real shared_utils module imported inside the moto mock."""
    import shared_utils
    return shared_utils


@pytest.fixture
def audit_table(aws_stack):
    """The moto-backed audit log table the strict helpers write to."""
    return aws_stack.tables.audit_log


def get_entry(audit_table, event_id):
    from boto3.dynamodb.conditions import Key
    items = audit_table.query(
        KeyConditionExpression=Key("event_id").eq(event_id)
    )["Items"]
    assert len(items) == 1
    return items[0]


class TestRecordAuditEventStrict:
    def test_writes_pending_entry_with_all_fields(self, shared, audit_table):
        event_id = shared.record_audit_event_strict(
            user_id="admin-1",
            action="role_change",
            resource_type="user_account",
            resource_id="operator1",
            details={"previous_role": "Viewer", "new_role": "Operator"},
        )

        entry = get_entry(audit_table, event_id)
        assert entry["user_id"] == "admin-1"
        assert entry["action"] == "role_change"
        assert entry["resource_type"] == "user_account"
        assert entry["resource_id"] == "operator1"
        assert entry["result"] == "pending"
        assert entry["details"] == {"previous_role": "Viewer",
                                    "new_role": "Operator"}
        assert entry["timestamp"] > 0

    def test_supported_action_types_and_results(self, shared, audit_table):
        assert shared.USER_ACCOUNT_RESOURCE_TYPE == "user_account"
        assert set(shared.USER_ACCOUNT_AUDIT_ACTIONS) == {
            "password_change", "forgot_password", "role_change",
            "account_create", "account_disable",
            "account_enable", "account_delete"}
        assert set(shared.AUDIT_RESULTS) == {
            "pending", "success", "failure", "rejected"}
        for action in shared.USER_ACCOUNT_AUDIT_ACTIONS:
            event_id = shared.record_audit_event_strict(
                "admin-1", action, shared.USER_ACCOUNT_RESOURCE_TYPE, "u")
            assert get_entry(audit_table, event_id)["action"] == action

    def test_unique_event_ids_within_same_millisecond(self, shared):
        ids = {shared.record_audit_event_strict(
            "admin-1", "password_change", "user_account", "u")
            for _ in range(5)}
        assert len(ids) == 5

    def test_invalid_result_raises(self, shared):
        with pytest.raises(ValueError):
            shared.record_audit_event_strict(
                "admin-1", "role_change", "user_account", "u",
                result="not-a-result")

    def test_raises_when_put_item_fails(self, shared, monkeypatch):
        """Req 6.4: an unrecordable audit entry must surface as an
        exception so the caller aborts the action."""
        monkeypatch.setattr(shared, "AUDIT_LOG_TABLE", "does-not-exist")
        with pytest.raises(ClientError):
            shared.record_audit_event_strict(
                "admin-1", "password_change", "user_account", "u")


class TestDetailsSanitization:
    def test_denylisted_keys_dropped(self, shared, audit_table):
        """Req 6.3: passwords, hashes, verifiers, and temp* values never
        reach the audit log."""
        event_id = shared.record_audit_event_strict(
            "admin-1", "password_change", "user_account", "u",
            details={
                "password": "s3cret!",
                "new_password": "s3cret!",
                "password_hash": "abc",
                "verifier": {"salt": "x", "hash": "y"},
                "credential_verifier": "abc",
                "temp_password": "s3cret!",
                "temporaryPassword": "s3cret!",
                "permanent": True,
                "nested": {"tempPass": "s3cret!", "reason": "ok",
                           "list": [{"hash": "z", "kept": 1}]},
            })

        details = get_entry(audit_table, event_id)["details"]
        assert details == {
            "permanent": True,
            "nested": {"reason": "ok", "list": [{"kept": 1}]},
        }

    def test_sanitize_is_prefix_match_for_temp_only(self, shared):
        # 'attempt' contains 'temp' but is not temp-prefixed: kept.
        assert shared.sanitize_audit_details(
            {"attempt_count": 3, "template": "x"}
        ) == {"attempt_count": 3}


class TestFinalizeAuditEvent:
    def test_finalizes_to_success_with_merged_details(self, shared, audit_table):
        event_id = shared.record_audit_event_strict(
            "admin-1", "role_change", "user_account", "operator1",
            details={"previous_role": "Viewer"})

        shared.finalize_audit_event(event_id, "success",
                                    {"new_role": "Operator",
                                     "temp_password": "leak!"})

        entry = get_entry(audit_table, event_id)
        assert entry["result"] == "success"
        assert entry["details"] == {"previous_role": "Viewer",
                                    "new_role": "Operator"}
        assert entry["completed_at"] >= entry["timestamp"]
        # identity fields preserved (Req 6.1)
        assert entry["user_id"] == "admin-1"
        assert entry["resource_id"] == "operator1"

    @pytest.mark.parametrize("result", ["failure", "rejected"])
    def test_terminal_results(self, shared, audit_table, result):
        event_id = shared.record_audit_event_strict(
            "admin-1", "forgot_password", "user_account", "u")
        shared.finalize_audit_event(event_id, result, {"reason": "why"})
        entry = get_entry(audit_table, event_id)
        assert entry["result"] == result
        assert entry["details"]["reason"] == "why"

    def test_invalid_result_raises(self, shared):
        event_id = shared.record_audit_event_strict(
            "admin-1", "role_change", "user_account", "u")
        with pytest.raises(ValueError):
            shared.finalize_audit_event(event_id, "pending")

    def test_unknown_event_raises(self, shared):
        with pytest.raises(ValueError):
            shared.finalize_audit_event("no-such-event", "success")
