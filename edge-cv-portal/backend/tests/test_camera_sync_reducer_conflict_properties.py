"""
Property-based test for reduce_report conflict classification.

**Feature: camera-registry-sync, Property 8: Conflict classification with edge-wins resolution**

*For any* registry entry holding a pending portal change and any
incoming edge report for the same Camera_Source that does not
acknowledge that change, the reducer classifies a Conflict exactly
when the edge content differs from the pending portal content; on
every conflict the retained effective state equals the edge state
(including edge deletion winning over portal modification), and the
emitted conflict event contains both conflicting versions, the
resolution applied, and a timestamp.

**Validates: Requirements 6.1, 6.2, 6.3, 6.5**

Generators (per the design testing strategy): pending entries with
portal_change_id/pending_content across all ops, edge reports whose
content converges with or diverges from the pending content, matching
and non-matching acks, edge deletions, and whitespace/unicode names.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

import camera_sync as cs

# ---------------------------------------------------------------------------
# Generators (shared id pools, mirroring the Property 1 conventions)
# ---------------------------------------------------------------------------

_CAMERA_IDS = [
    "cfg-a1b2", "cfg-c3d4", "cfg-e5f6",
    "disc-3fe9c0d21ab4", "disc-00aa11bb22cc", "portal-new-1",
]

_CHANGE_IDS = ["pc-1", "pc-2", "pc-3"]

_ORIGINS = ["edge-configured", "edge-discovered", "portal-created"]
_TYPES = ["Camera", "Folder", "ICam", "NvidiaCSI", "RTSP", "V4L2Discovered"]

# Whitespace/unicode-heavy names.
_names = st.text(
    alphabet=st.characters(
        codec="utf-8", categories=("L", "N", "P", "Zs", "S")
    ),
    min_size=0,
    max_size=24,
)

_versions = st.integers(min_value=1, max_value=40)

_params = st.dictionaries(
    st.sampled_from(["devicePath", "cameraId", "url", "gain", "exposure"]),
    st.one_of(st.text(max_size=16), st.integers(0, 10_000_000)),
    max_size=4,
)

_capabilities = st.one_of(
    st.just({}),
    st.fixed_dictionaries({
        "formats": st.lists(
            st.fixed_dictionaries({
                "pixelFormat": st.sampled_from(["YUYV", "MJPG", "NV12"]),
                "resolutions": st.lists(
                    st.tuples(st.integers(1, 4096), st.integers(1, 4096))
                    .map(list),
                    max_size=3,
                ),
            }),
            max_size=2,
        ),
    }),
)


def _content_of(source):
    """Project onto the comparable content fields (mirrors the design)."""
    if not source:
        return {}
    return {field: source.get(field) for field in ("name", "type", "params")}


@st.composite
def _conflict_cases(draw):
    """A pending registry entry plus an incoming edge state.

    The incoming state is a camera report (converging with or diverging
    from the pending content, with a matching, non-matching, or absent
    ack) or an edge deletion. Incoming versions never regress below the
    recorded version so the pending-change classification path — not the
    staleness guard (Property 1) — is exercised.
    """
    camera_id = draw(st.sampled_from(_CAMERA_IDS))
    change_id = draw(st.sampled_from(_CHANGE_IDS))
    pending_content = {
        "op": draw(st.sampled_from(["create", "update", "delete"])),
        "name": draw(_names),
        "type": draw(st.sampled_from(_TYPES)),
        "params": draw(_params),
    }
    entry_version = draw(_versions)
    registry_entry = {
        "usecase_id": draw(st.sampled_from(["uc-1", "uc-2"])),
        "camera_source_id": camera_id,
        "device_id": "thing-1",
        "name": draw(_names),
        "type": draw(st.sampled_from(_TYPES)),
        "params": draw(_params),
        "capabilities": draw(_capabilities),
        "origin": draw(st.sampled_from(_ORIGINS)),
        "version": entry_version,
        "last_reported_at": draw(st.integers(1, 1_700_000_000_000)),
        "sync_status": cs.SYNC_STATUS_PENDING,
        "portal_change_id": change_id,
        "pending_content": pending_content,
        "absent": False,
    }

    if draw(st.booleans()):
        # Edge deletion: the source vanished from the full report (Req 6.5).
        incoming = draw(st.sampled_from([None, {"deleted": True}]))
    else:
        incoming = {
            "version": draw(
                st.integers(entry_version, entry_version + 10)),
            "origin": draw(st.sampled_from(_ORIGINS)),
            "capabilities": draw(_capabilities),
            "absent": False,
        }
        if draw(st.booleans()):
            # Edge content converged onto the pending portal content.
            incoming["name"] = pending_content["name"]
            incoming["type"] = pending_content["type"]
            incoming["params"] = dict(pending_content["params"])
        else:
            incoming["name"] = draw(_names)
            incoming["type"] = draw(st.sampled_from(_TYPES))
            incoming["params"] = draw(_params)
        ack = draw(st.sampled_from(
            [None, change_id] + [c for c in _CHANGE_IDS if c != change_id]))
        if ack is not None:
            incoming["ack"] = ack

    now_ms = draw(st.integers(1_700_000_000_001, 1_900_000_000_000))
    return registry_entry, incoming, now_ms


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

_DECLARED_FIELDS = ("name", "type", "params", "capabilities",
                    "origin", "version")


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_conflict_cases())
def test_conflict_classification_with_edge_wins_resolution(case):
    """**Feature: camera-registry-sync, Property 8: Conflict classification
    with edge-wins resolution**

    **Validates: Requirements 6.1, 6.2, 6.3, 6.5**
    """
    registry_entry, incoming, now_ms = case
    pending_content = registry_entry["pending_content"]
    change_id = registry_entry["portal_change_id"]

    outcome = cs.reduce_report(registry_entry, incoming, now_ms)

    if incoming is None or incoming.get("deleted") is True:
        # Edge deletion while a portal change is pending (Req 6.5):
        # the deletion is the effective state either way.
        assert outcome.entry is None
        if pending_content["op"] == "delete":
            # Portal wanted the deletion too: agreement, not a Conflict.
            assert outcome.action == cs.ACTION_UPSERT
            assert outcome.conflict_event is None
        else:
            # Deletion wins over the pending portal modification, and a
            # conflict event is recorded (Reqs 6.3, 6.5).
            assert outcome.action == cs.ACTION_CONFLICT
            event = outcome.conflict_event
            assert event is not None
            assert event.camera_source_id == registry_entry["camera_source_id"]
            assert event.edge_version is None
            assert event.portal_version == pending_content
            assert event.resolution == cs.RESOLUTION_DELETION_RETAINED
            assert event.created_at == now_ms
        return

    # A camera report for the pending source. It acknowledges the pending
    # change exactly when its ack carries that change's id.
    acknowledged = incoming.get("ack") == change_id
    content_differs = _content_of(incoming) != _content_of(pending_content)

    # Conflict exactly when the change is unacknowledged and the edge
    # content differs from the pending portal content (Req 6.1).
    if acknowledged or not content_differs:
        assert outcome.action == cs.ACTION_UPSERT
        assert outcome.conflict_event is None
    else:
        assert outcome.action == cs.ACTION_CONFLICT
        event = outcome.conflict_event
        assert event is not None
        # The event contains both conflicting versions, the resolution
        # applied, and the timestamp (Req 6.3).
        assert event.camera_source_id == registry_entry["camera_source_id"]
        assert event.edge_version == _content_of(incoming)
        assert event.portal_version == pending_content
        assert event.resolution == cs.RESOLUTION_EDGE_RETAINED
        assert event.created_at == now_ms

    # The retained effective state equals the edge state — edge wins on
    # conflict, and the applied/converged state on upsert (Req 6.2).
    entry = outcome.entry
    assert entry is not None
    for field in _DECLARED_FIELDS:
        assert entry[field] == incoming[field], field
    assert entry["last_reported_at"] == now_ms
