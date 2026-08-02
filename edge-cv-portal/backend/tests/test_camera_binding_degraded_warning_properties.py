"""
Property-based test for deploy-time degraded-source warning gating —
validate_camera_bindings (functions/deployments.py).

**Feature: camera-registry-sync, Property 15: Degraded-source warnings gate
submission on confirmation**

*For any* binding whose referenced Camera_Source is stale, marked absent,
or has sync status pending or failed, and for any target device that has
never synced, validation emits a warning identifying the condition, and
the deployment is accepted if and only if every emitted warning id
appears in the submitted confirmations (with never-synced devices
additionally restricted to manual overrides).

**Validates: Requirements 8.8, 9.3**

Task 11.4 (spec: camera-registry-sync). The example-based unit tests over
the same function live in test_camera_binding_validation.py (task 11.1);
the completeness property (task 11.2) lives in
test_camera_binding_completeness_properties.py and the existence property
(task 11.3) in test_camera_binding_existence_properties.py.
"""
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def deployments(aws_stack):
    """Import deployments inside the moto mock so its module-level boto3
    clients are intercepted."""
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_NODE_IDS = ["n1", "n2", "n3"]
_DEVICES = ["line-a", "line-b", "line-c"]

#: The Requirement 9.3 degraded conditions a bound registry entry may be
#: in, drawn independently so any combination (including none) occurs.
_SYNC_STATUSES = ["synced", "pending", "failed"]


@st.composite
def _degraded_cases(draw):
    """A version with binding points, targets, per-device registries, and
    bindings with a known set of expected warnings.

    Each target is either

    * **never synced with an empty registry** (8.8) — absent from the
      snapshot entirely or present with ``never_synced`` and no cameras;
      its nodes are bound by manual override (permitted) or by a
      registered-source reference (restricted: the empty registry makes
      it a missing-source error), or
    * **synced** — every node bound to an entry present in the registry
      whose degraded flags (absent / stale / sync status) are drawn
      independently, so bindings range from fully healthy (no warning)
      to degraded in every condition at once.

    Also draws the subset of expected warnings to confirm in the
    partial-confirmation pass.
    """
    node_ids = draw(st.lists(st.sampled_from(_NODE_IDS),
                             unique=True, min_size=1, max_size=3))
    node_types = {
        node_id: draw(st.sampled_from(["icam_source", "acme.cam_input"]))
        for node_id in node_ids
    }
    targets = draw(st.lists(st.sampled_from(_DEVICES),
                            unique=True, min_size=1, max_size=3))

    registry_snapshot = {}
    bindings = {}
    # (device, node_id, camera_source_id) -> frozenset of conditions
    expected_degraded = {}
    expected_never_synced = set()
    # Source references on never-synced devices: restricted to manual
    # override, so these must surface as missing-source errors (8.8).
    expected_missing = set()
    confirm_keys = set()

    for device in targets:
        device_bindings = {}
        if draw(st.booleans()):
            # Never synced, no registered Camera_Sources (8.8).
            expected_never_synced.add(device)
            if draw(st.booleans()):
                confirm_keys.add(("never-synced", device))
            if draw(st.booleans()):
                # Present in the snapshot as never synced and empty; the
                # other branch leaves it out entirely (fail-safe rule).
                registry_snapshot[device] = {"never_synced": True,
                                             "cameras": {}}
            for node_id in node_ids:
                override_allowed = node_types[node_id] == "icam_source"
                if override_allowed and draw(st.booleans()):
                    device_bindings[node_id] = {
                        "override": {"device": "/dev/video1"}}
                else:
                    camera_source_id = f"cfg-{device}-{node_id}"
                    device_bindings[node_id] = {
                        "cameraSourceId": camera_source_id}
                    expected_missing.add(
                        (device, node_id, camera_source_id))
        else:
            cameras = {}
            for node_id in node_ids:
                camera_source_id = f"cfg-{device}-{node_id}"
                entry = {
                    # Camera is compatible with both node types, so type
                    # mismatches never intrude on the warning signal.
                    "type": "Camera",
                    "params": {"devicePath": "/dev/video0"},
                    "sync_status": draw(st.sampled_from(_SYNC_STATUSES)),
                    "absent": draw(st.booleans()),
                    "stale": draw(st.booleans()),
                }
                cameras[camera_source_id] = entry
                device_bindings[node_id] = {
                    "cameraSourceId": camera_source_id}
                conditions = set()
                if entry["absent"]:
                    conditions.add("absent")
                if entry["stale"]:
                    conditions.add("stale")
                if entry["sync_status"] in ("pending", "failed"):
                    conditions.add(entry["sync_status"])
                if conditions:
                    key = (device, node_id, camera_source_id)
                    expected_degraded[key] = frozenset(conditions)
                    if draw(st.booleans()):
                        confirm_keys.add(("degraded",) + key)
            registry_snapshot[device] = {"never_synced": False,
                                         "cameras": cameras}
        bindings[device] = device_bindings

    version = {
        "has_binding_points": True,
        "camera_input_nodes": [
            {"node_id": node_id,
             "node_type": node_types[node_id],
             "compiled_device_paths": {"x86_64": "/dev/video0"}}
            for node_id in node_ids
        ],
    }
    return (version, targets, registry_snapshot, bindings,
            expected_degraded, expected_never_synced, expected_missing,
            confirm_keys)


def _warning_key(deployments, warning):
    """A warning's identity independent of its id string."""
    if warning["code"] == deployments.CAMERA_WARNING_NEVER_SYNCED:
        return ("never-synced", warning["device"])
    return ("degraded", warning["device"], warning["nodeId"],
            warning["cameraSourceId"])


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------

# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(_degraded_cases())
def test_degraded_warnings_gate_submission_on_confirmation(
        deployments, case):
    """**Feature: camera-registry-sync, Property 15: Degraded-source
    warnings gate submission on confirmation**

    **Validates: Requirements 8.8, 9.3**

    Warnings are emitted exactly for degraded bindings (identifying each
    condition) and never-synced targets (restricted to manual override:
    registered-source references on them are missing-source errors), and
    each warning is marked confirmed iff its id was submitted —
    submitting all warning ids from a first pass yields a second pass in
    which every warning is confirmed.
    """
    (version, targets, registry_snapshot, bindings,
     expected_degraded, expected_never_synced, expected_missing,
     confirm_keys) = case

    # -------------------------------------------------- unconfirmed pass
    errors, warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, [])

    degraded = [w for w in warnings
                if w["code"] == deployments.CAMERA_WARNING_SOURCE_DEGRADED]
    never_synced = [w for w in warnings
                    if w["code"] == deployments.CAMERA_WARNING_NEVER_SYNCED]

    # Exactly one degraded warning per degraded binding, identifying the
    # Camera_Source and every condition it is in (9.3).
    assert {(w["device"], w["nodeId"], w["cameraSourceId"]):
            frozenset(w["conditions"])
            for w in degraded} == expected_degraded
    assert len(degraded) == len(expected_degraded)
    for warning in degraded:
        assert warning["cameraSourceId"] in warning["message"]
        assert warning["device"] in warning["message"]
        for condition in warning["conditions"]:
            assert condition in warning["message"]

    # Exactly one never-synced warning per never-synced target (8.8).
    assert {w["device"] for w in never_synced} == expected_never_synced
    assert len(never_synced) == len(expected_never_synced)
    for warning in never_synced:
        assert warning["device"] in warning["message"]

    assert len(warnings) == len(degraded) + len(never_synced)

    # Warning ids are distinct, so confirming one never confirms another.
    ids = [w["id"] for w in warnings]
    assert len(set(ids)) == len(ids)

    # Nothing is confirmed when nothing was submitted.
    assert all(not w["confirmed"] for w in warnings)

    # Never-synced targets permit binding only through manual override:
    # the only errors are the missing-source rejections of their
    # registered-source references (8.8).
    assert {(e["device"], e["nodeId"], e["cameraSourceId"])
            for e in errors} == expected_missing
    assert len(errors) == len(expected_missing)
    assert all(e["code"] == deployments.CAMERA_ERROR_SOURCE_MISSING
               for e in errors)

    # ------------------------------------------- partial-confirmation pass
    submitted = [w["id"] for w in warnings
                 if _warning_key(deployments, w) in confirm_keys]
    partial_errors, partial_warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings,
        submitted + ["not-a-real-warning-id"])

    assert [w["id"] for w in partial_warnings] == ids
    assert partial_errors == errors
    # Each warning is confirmed iff its id was submitted, so the caller's
    # every-warning-confirmed acceptance rule passes exactly when the
    # submission covered all warnings.
    for warning in partial_warnings:
        assert warning["confirmed"] == (
            _warning_key(deployments, warning) in confirm_keys)

    # ---------------------------------------------- full-confirmation pass
    full_errors, full_warnings = deployments.validate_camera_bindings(
        version, targets, registry_snapshot, bindings, ids)

    assert [w["id"] for w in full_warnings] == ids
    assert full_errors == errors
    assert all(w["confirmed"] for w in full_warnings)
