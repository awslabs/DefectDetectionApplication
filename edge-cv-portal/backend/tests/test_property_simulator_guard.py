"""Property test for the Plugin_Simulator start guard (task 8.3).

**Feature: custom-node-designer, Property 15: Simulator start guard equals x86_64 artifact presence**

For all Plugin_Record versions with random per-architecture artifact
sets (random arch subsets, random buildStatus values, present, empty,
or missing s3Key, and malformed entries), the Plugin_Simulator permits
starting a run if and only if a successfully built x86_64
Plugin_Artifact with a stored Plugin_Library key exists, and every
refusal describes the missing x86_64 build.

**Validates: Requirements 7.5**

The guard under test (`evaluate_simulation_guard`) is pure over the
Plugin_Record item dict, so it is exercised directly with no AWS
involvement. The module is imported through the shared moto-backed
session fixture only so the real `shared_utils` layer (not a test
fake) backs the import.
"""

from __future__ import annotations

import copy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="session")
def simulator(aws_stack):
    """The real plugin_simulator module, imported via the session stack."""
    return aws_stack.plugin_simulator


# ---------------------------------------------------------------------------
# Generators: arbitrary artifacts maps
# ---------------------------------------------------------------------------

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")

#: buildStatus values: the real lifecycle values plus arbitrary noise.
_build_status = st.one_of(
    st.sampled_from(("succeeded", "failed", "building")),
    st.text(max_size=12),
    st.none(),
)

#: s3Key values: a real Plugin_Library key, an empty string (recorded
#: but blank), or absent entirely (via the optional dict field below).
_s3_key = st.one_of(
    st.just("workflow-plugins/custom/uc-p15/x86_64/plugin.so"),
    st.just(""),
    st.none(),
)

#: A structurally well-formed per-arch artifact entry, with buildStatus
#: and s3Key each independently valid, wrong, or missing.
_well_formed_entry = st.fixed_dictionaries(
    {},
    optional={
        "buildStatus": _build_status,
        "s3Key": _s3_key,
        "checksum": st.just("ab" * 32),
        "signature": st.just("sig-bytes"),
        "logTail": st.text(max_size=10),
    },
)

#: Malformed entries: not a dict at all.
_malformed_entry = st.one_of(
    st.none(),
    st.text(max_size=10),
    st.integers(),
    st.lists(st.integers(), max_size=3),
    st.booleans(),
)

_entry = st.one_of(_well_formed_entry, _malformed_entry)

#: Artifacts maps over random arch subsets (unknown arch names mixed in
#: to confirm they never satisfy the guard).
_artifacts = st.dictionaries(
    keys=st.one_of(st.sampled_from(ARCHS), st.just("riscv64")),
    values=_entry,
    max_size=6,
)

#: Plugin_Record version items: the artifacts field present with a
#: random map, present but None (cleared), or absent entirely.
_item = st.fixed_dictionaries(
    {
        "plugin_id": st.just("plugin-p15"),
        "version": st.integers(min_value=1, max_value=99),
        "lifecycle_state": st.sampled_from(("dev", "test", "prod")),
    },
    optional={"artifacts": st.one_of(_artifacts, st.none())},
)


def _reference_guard_passes(item):
    """Requirement 7.5 restated: a run may start exactly when a
    successfully built x86_64 Plugin_Artifact with a stored key exists."""
    artifacts = item.get("artifacts") or {}
    entry = artifacts.get("x86_64")
    return (
        isinstance(entry, dict)
        and entry.get("buildStatus") == "succeeded"
        and bool(entry.get("s3Key"))
    )


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(item=_item)
def test_simulator_start_guard_equals_x86_64_artifact_presence(simulator, item):
    """**Feature: custom-node-designer, Property 15: Simulator start guard equals x86_64 artifact presence**

    For all Plugin_Record versions with random per-architecture
    artifact sets, the guard permits a run if and only if a
    successfully built x86_64 Plugin_Artifact exists, every refusal
    carries the identifying rejection describing the missing x86_64
    build, and guard evaluation never mutates the record.

    **Validates: Requirements 7.5**
    """
    before = copy.deepcopy(item)
    allowed, error = simulator.evaluate_simulation_guard(item)

    # Guard evaluation only decides; it never mutates the record.
    assert item == before

    if _reference_guard_passes(item):
        assert allowed is True
        assert error is None
    else:
        assert allowed is False
        # The refusal identifies itself and describes the missing build.
        assert error["code"] == "SIMULATION_REQUIRES_X86_64_BUILD"
        assert "x86_64" in error["message"]
        assert error["details"]["missing"] == "successful x86_64 Plugin_Artifact"
        assert error["details"]["plugin_id"] == item["plugin_id"]
        assert error["details"]["version"] == item["version"]
