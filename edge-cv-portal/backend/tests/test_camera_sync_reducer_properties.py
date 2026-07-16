"""
Property-based test for the Portal_Sync_Service reduce_report reducer.

**Feature: camera-registry-sync, Property 1: Sync reducer round trip with version guard**

*For any* registry state and any incoming device report, processing the
report through `reduce_report` yields registry entries that preserve
every declared Camera_Source field (id, name, type, params,
capabilities, origin, version, last-reported timestamp) and the
device's `usecase_id`, entries not referenced by the report are
unchanged, entries whose incoming version is lower than the recorded
version are discarded leaving the recorded entry intact, and the
device meta's last-report timestamp is stamped.

**Validates: Requirements 1.1, 1.2, 1.4, 3.2, 3.5**

Generators (per the design testing strategy): multi-source inventories,
version sequences including regressions, all origins and sync statuses,
whitespace/unicode names, and degenerate registries (empty,
never-synced).
"""
from hypothesis import given, settings
from hypothesis import strategies as st

import camera_sync as cs

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A small shared id pool so registry entries and report entries overlap,
# miss each other, and collide in interesting ways.
_CAMERA_IDS = [
    "cfg-a1b2", "cfg-c3d4", "cfg-e5f6",
    "disc-3fe9c0d21ab4", "disc-00aa11bb22cc", "portal-new-1",
]

_CHANGE_IDS = ["pc-1", "pc-2", "pc-3"]

_ORIGINS = ["edge-configured", "edge-discovered", "portal-created"]
_TYPES = ["Camera", "Folder", "ICam", "NvidiaCSI", "RTSP", "V4L2Discovered"]

# Whitespace/unicode-heavy names (Req 1.1 field fidelity for any name).
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


@st.composite
def _incoming_cameras(draw):
    """A camera entry in the shadow reported-document shape."""
    camera = {
        "version": draw(_versions),
        "name": draw(_names),
        "type": draw(st.sampled_from(_TYPES)),
        "origin": draw(st.sampled_from(_ORIGINS)),
        "params": draw(_params),
        "capabilities": draw(_capabilities),
        "absent": draw(st.booleans()),
    }
    if camera["absent"] and draw(st.booleans()):
        camera["absentSince"] = draw(st.integers(1, 2_000_000_000_000))
    if draw(st.booleans()):
        camera["ack"] = draw(st.sampled_from(_CHANGE_IDS))
    return camera


@st.composite
def _registry_entries(draw, camera_source_id, usecase_id):
    """A Camera_Registry entry per the design data model, any sync status."""
    entry = {
        "usecase_id": usecase_id,
        "camera_source_id": camera_source_id,
        "device_id": "thing-1",
        "name": draw(_names),
        "type": draw(st.sampled_from(_TYPES)),
        "params": draw(_params),
        "capabilities": draw(_capabilities),
        "origin": draw(st.sampled_from(_ORIGINS)),
        "version": draw(_versions),
        "last_reported_at": draw(st.integers(1, 1_700_000_000_000)),
        "sync_status": draw(st.sampled_from(
            [cs.SYNC_STATUS_SYNCED, cs.SYNC_STATUS_PENDING,
             cs.SYNC_STATUS_FAILED]
        )),
        "absent": draw(st.booleans()),
    }
    if entry["sync_status"] == cs.SYNC_STATUS_PENDING:
        entry["portal_change_id"] = draw(st.sampled_from(_CHANGE_IDS))
        entry["pending_content"] = {
            "op": draw(st.sampled_from(["create", "update", "delete"])),
            "name": draw(_names),
            "type": draw(st.sampled_from(_TYPES)),
            "params": draw(_params),
        }
    if entry["sync_status"] == cs.SYNC_STATUS_FAILED:
        entry["failure_reason"] = draw(_names)
    return entry


@st.composite
def _sync_cases(draw):
    """A registry state, an incoming multi-source report, meta, and a clock.

    Degenerate registries (empty, never-synced) arise naturally: the
    registry subset may be empty and meta may be None or never-synced.
    """
    usecase_id = draw(st.sampled_from(["uc-1", "uc-2"]))
    ids = draw(st.lists(st.sampled_from(_CAMERA_IDS),
                        unique=True, max_size=len(_CAMERA_IDS)))
    registry = {}
    report = {}
    for camera_id in ids:
        in_registry = draw(st.booleans())
        if in_registry:
            registry[camera_id] = draw(
                _registry_entries(camera_id, usecase_id))
        # Sources may be report-only, registry-only, or both.
        if draw(st.booleans()) or not in_registry:
            report[camera_id] = draw(_incoming_cameras())
    meta = draw(st.one_of(
        st.none(),
        st.fixed_dictionaries({
            "usecase_id": st.just(usecase_id),
            "never_synced": st.booleans(),
        }),
    ))
    now_ms = draw(st.integers(1_700_000_000_001, 1_900_000_000_000))
    return registry, report, meta, now_ms


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------

_DECLARED_FIELDS = ("name", "type", "params", "capabilities",
                    "origin", "version")


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_sync_cases())
def test_sync_reducer_round_trip_with_version_guard(case):
    """**Feature: camera-registry-sync, Property 1: Sync reducer round trip
    with version guard**

    **Validates: Requirements 1.1, 1.2, 1.4, 3.2, 3.5**
    """
    registry, report, meta, now_ms = case

    new_registry = dict(registry)
    for camera_id, incoming in report.items():
        prior = registry.get(camera_id)
        outcome = cs.reduce_report(prior, incoming, now_ms)

        if prior is not None and incoming["version"] < prior["version"]:
            # Version guard: stale state is discarded and the recorded
            # entry is retained intact (Req 3.5).
            assert outcome.action == cs.ACTION_DISCARD_STALE
            assert outcome.entry == prior
            assert outcome.conflict_event is None
            continue

        # Fresh state is applied — either a plain upsert or an upsert
        # with a conflict event (edge wins); each source is reduced
        # independently (Req 1.2).
        assert outcome.action in (cs.ACTION_UPSERT, cs.ACTION_CONFLICT)
        entry = outcome.entry
        assert entry is not None

        # Round trip: every declared Camera_Source field survives the
        # reduction unchanged (Req 1.1).
        for field in _DECLARED_FIELDS:
            assert entry[field] == incoming[field], field
        # Last-reported timestamp recorded from this report (Reqs 1.1, 3.2).
        assert entry["last_reported_at"] == now_ms
        assert entry["absent"] is incoming["absent"]
        if incoming["absent"] and "absentSince" in incoming:
            assert entry["absent_since"] == incoming["absentSince"]

        # Identity and Use_Case scoping carried from the existing
        # entry (Req 1.4).
        if prior is not None:
            assert entry["usecase_id"] == prior["usecase_id"]
            assert entry["camera_source_id"] == camera_id

        if outcome.action == cs.ACTION_CONFLICT:
            assert outcome.conflict_event is not None
        else:
            assert outcome.conflict_event is None

        # Idempotency under duplicate delivery: re-reducing the persisted
        # entry against the same incoming state reproduces it exactly.
        replay = cs.reduce_report(entry, incoming, now_ms)
        assert replay.entry == entry
        assert replay.conflict_event is None

        new_registry[camera_id] = entry

    # Entries not referenced by the report are unchanged (Reqs 1.2, 3.5).
    for camera_id, prior in registry.items():
        if camera_id not in report:
            assert new_registry[camera_id] == prior

    # Every processed report stamps the device META item (Req 3.2):
    # last-report timestamp set, never_synced cleared, scoping kept,
    # idempotently.
    stamped = cs.stamp_meta(meta, now_ms)
    assert stamped["last_report_at"] == now_ms
    assert stamped["never_synced"] is False
    if meta is not None:
        assert stamped["usecase_id"] == meta["usecase_id"]
    assert cs.stamp_meta(stamped, now_ms) == stamped
