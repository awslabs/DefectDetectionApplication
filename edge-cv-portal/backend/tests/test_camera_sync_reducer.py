"""
Unit tests for the Portal_Sync_Service reduce_report pure reducer.

Feature: camera-registry-sync, task 5.1.

Covers: version-guarded staleness discard (Req 3.5), ack -> synced
(Req 5.3), failure -> failed with reason (Req 5.4), conflict
classification with edge-wins resolution and event contents
(Reqs 6.1, 6.2, 6.3), deletion-retained on reported deletion during a
pending portal update (Req 6.5), META stamping (Req 3.2), and
idempotency under duplicate delivery.

Validates: Requirements 3.2, 3.5, 5.3, 5.4, 6.1, 6.2, 6.3, 6.5
"""
import camera_sync as cs

NOW = 1_730_000_000_000


def edge_camera(**overrides):
    """A camera entry in the shadow reported-document shape."""
    camera = {
        "version": 7,
        "name": "Line 1 inspection cam",
        "type": "Camera",
        "origin": "edge-configured",
        "params": {"devicePath": "/dev/video0", "gain": 4},
        "capabilities": {"formats": [{"pixelFormat": "YUYV",
                                      "resolutions": [[1920, 1080]]}]},
        "absent": False,
    }
    camera.update(overrides)
    return camera


def registry_entry(**overrides):
    """A Camera_Registry DDB entry per the design data model."""
    entry = {
        "usecase_id": "uc-1",
        "camera_source_id": "cfg-a1b2",
        "name": "Line 1 inspection cam",
        "type": "Camera",
        "params": {"devicePath": "/dev/video0", "gain": 4},
        "capabilities": {},
        "origin": "edge-configured",
        "version": 7,
        "last_reported_at": NOW - 60_000,
        "sync_status": "synced",
        "absent": False,
    }
    entry.update(overrides)
    return entry


def pending_entry(**overrides):
    """A registry entry with an unacknowledged portal change."""
    defaults = {
        "sync_status": "pending",
        "portal_change_id": "pc-123",
        "pending_content": {
            "op": "update",
            "name": "Portal name",
            "type": "Camera",
            "params": {"devicePath": "/dev/video0", "gain": 8},
        },
    }
    defaults.update(overrides)
    return registry_entry(**defaults)


class TestUpsert:
    def test_new_source_upserts_all_declared_fields(self):
        incoming = edge_camera()
        outcome = cs.reduce_report(None, incoming, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.conflict_event is None
        entry = outcome.entry
        assert entry["name"] == incoming["name"]
        assert entry["type"] == incoming["type"]
        assert entry["params"] == incoming["params"]
        assert entry["capabilities"] == incoming["capabilities"]
        assert entry["origin"] == incoming["origin"]
        assert entry["version"] == incoming["version"]
        assert entry["last_reported_at"] == NOW
        assert entry["sync_status"] == "synced"
        assert entry["absent"] is False

    def test_upsert_preserves_usecase_scoping(self):
        outcome = cs.reduce_report(registry_entry(), edge_camera(version=8), NOW)
        assert outcome.entry["usecase_id"] == "uc-1"
        assert outcome.entry["camera_source_id"] == "cfg-a1b2"

    def test_absent_source_carries_absent_since(self):
        incoming = edge_camera(absent=True, absentSince=NOW - 5_000)
        outcome = cs.reduce_report(registry_entry(), incoming, NOW)
        assert outcome.entry["absent"] is True
        assert outcome.entry["absent_since"] == NOW - 5_000


class TestVersionGuard:
    def test_older_version_is_discarded_and_entry_retained(self):
        entry = registry_entry(version=7)
        outcome = cs.reduce_report(entry, edge_camera(version=6), NOW)
        assert outcome.action == cs.ACTION_DISCARD_STALE
        assert outcome.entry == entry
        assert outcome.conflict_event is None

    def test_equal_version_redelivery_is_idempotent(self):
        incoming = edge_camera(version=7)
        first = cs.reduce_report(registry_entry(), incoming, NOW)
        second = cs.reduce_report(first.entry, incoming, NOW)
        assert second.action == cs.ACTION_UPSERT
        assert second.entry == first.entry


class TestAckAndFailure:
    def test_matching_ack_marks_synced_and_clears_pending(self):
        entry = pending_entry()
        incoming = edge_camera(version=8, ack="pc-123",
                               params={"devicePath": "/dev/video0", "gain": 8},
                               name="Portal name")
        outcome = cs.reduce_report(entry, incoming, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.conflict_event is None
        assert outcome.entry["sync_status"] == "synced"
        assert "portal_change_id" not in outcome.entry
        assert "pending_content" not in outcome.entry

    def test_failure_entry_marks_failed_with_reason(self):
        entry = pending_entry()
        failure = {"reason": "location is required", "portalChangeId": "pc-123"}
        outcome = cs.reduce_report(entry, failure, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.entry["sync_status"] == "failed"
        assert outcome.entry["failure_reason"] == "location is required"

    def test_failure_for_superseded_change_is_discarded(self):
        entry = pending_entry(portal_change_id="pc-456")
        failure = {"reason": "stale", "portalChangeId": "pc-123"}
        outcome = cs.reduce_report(entry, failure, NOW)
        assert outcome.action == cs.ACTION_DISCARD_STALE
        assert outcome.entry == entry

    def test_failure_for_unknown_source_is_discarded(self):
        outcome = cs.reduce_report(None, {"reason": "x", "portalChangeId": "p"}, NOW)
        assert outcome.action == cs.ACTION_DISCARD_STALE
        assert outcome.entry is None


class TestConflict:
    def test_unacked_diverging_content_classifies_conflict_edge_wins(self):
        entry = pending_entry()
        incoming = edge_camera(version=8, name="Edge name")
        outcome = cs.reduce_report(entry, incoming, NOW)
        assert outcome.action == cs.ACTION_CONFLICT
        # Edge wins: the retained entry is the edge state (Req 6.2).
        assert outcome.entry["name"] == "Edge name"
        assert outcome.entry["sync_status"] == "synced"
        # Event carries both versions, resolution, timestamp (Req 6.3).
        event = outcome.conflict_event
        assert event.camera_source_id == "cfg-a1b2"
        assert event.edge_version["name"] == "Edge name"
        assert event.portal_version["name"] == "Portal name"
        assert event.resolution == cs.RESOLUTION_EDGE_RETAINED
        assert event.created_at == NOW

    def test_unacked_identical_content_is_not_a_conflict(self):
        entry = pending_entry()
        incoming = edge_camera(
            version=8,
            name="Portal name",
            params={"devicePath": "/dev/video0", "gain": 8},
        )
        outcome = cs.reduce_report(entry, incoming, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.conflict_event is None

    def test_conflict_replay_after_resolution_is_plain_upsert(self):
        entry = pending_entry()
        incoming = edge_camera(version=8, name="Edge name")
        first = cs.reduce_report(entry, incoming, NOW)
        replay = cs.reduce_report(first.entry, incoming, NOW)
        assert replay.action == cs.ACTION_UPSERT
        assert replay.conflict_event is None
        assert replay.entry == first.entry


class TestDeletion:
    def test_deletion_with_pending_update_is_deletion_retained_conflict(self):
        entry = pending_entry()
        outcome = cs.reduce_report(entry, None, NOW)
        assert outcome.action == cs.ACTION_CONFLICT
        assert outcome.entry is None
        event = outcome.conflict_event
        assert event.resolution == cs.RESOLUTION_DELETION_RETAINED
        assert event.edge_version is None
        assert event.portal_version == entry["pending_content"]
        assert event.created_at == NOW

    def test_deletion_marker_dict_is_equivalent(self):
        outcome = cs.reduce_report(pending_entry(), {"deleted": True}, NOW)
        assert outcome.action == cs.ACTION_CONFLICT
        assert outcome.entry is None

    def test_deletion_matching_pending_delete_is_agreement(self):
        entry = pending_entry(pending_content={"op": "delete"})
        outcome = cs.reduce_report(entry, None, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.entry is None
        assert outcome.conflict_event is None

    def test_deletion_without_pending_change_deletes(self):
        outcome = cs.reduce_report(registry_entry(), None, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.entry is None
        assert outcome.conflict_event is None

    def test_deletion_of_unknown_source_is_idempotent_noop(self):
        outcome = cs.reduce_report(None, None, NOW)
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.entry is None
        assert outcome.conflict_event is None


class TestMetaStamp:
    def test_stamps_last_report_at_and_clears_never_synced(self):
        meta = cs.stamp_meta({"usecase_id": "uc-1", "never_synced": True}, NOW)
        assert meta == {
            "usecase_id": "uc-1",
            "never_synced": False,
            "last_report_at": NOW,
        }

    def test_missing_meta_is_created(self):
        meta = cs.stamp_meta(None, NOW)
        assert meta["last_report_at"] == NOW
        assert meta["never_synced"] is False

    def test_stamp_is_idempotent(self):
        once = cs.stamp_meta({"never_synced": True}, NOW)
        assert cs.stamp_meta(once, NOW) == once
