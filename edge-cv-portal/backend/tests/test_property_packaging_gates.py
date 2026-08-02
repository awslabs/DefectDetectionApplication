"""Property test for the custom-plugin packaging gates (task 10.3).

**Feature: custom-node-designer, Property 13: Packaging gates on lifecycle state and artifact presence**

For all workflows containing Custom_Node_Types and all combinations of
backing Plugin_Record lifecycle states and per-architecture artifact
availability, packaging is permitted (zero gate findings) if and only
if every compiled ``custom:`` dependency resolves to a backing
Plugin_Record that is in test or prod lifecycle state, carries a
complete Plugin_Artifact entry (succeeded build + s3Key + checksum +
signature) for every selected architecture the dependency was compiled
for, and has a registered Plugin_Component version; every
ineligibility produces exactly the finding identifying the
Custom_Node_Type and the missing architecture or the offending
lifecycle state.

**Validates: Requirements 11.1, 11.2, 11.3**

The gate logic under test (``custom_plugin_gate_findings``,
``artifact_entry_complete``, ``custom_dependency_index``) is pure over
plain dicts, so the property is exercised directly with no AWS calls.
The module is imported through the shared moto-backed session fixture
only so its module-level boto3 clients bind to the mock (same
re-import pattern as test_workflow_packaging_custom_plugins.py).
"""

from __future__ import annotations

import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (DynamoDB / S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


# ---------------------------------------------------------------------------
# Reference model: eligibility restated from the requirements (11.1,
# 11.2, 11.3) rather than imported, so the test cannot silently agree
# with a wrong implementation.
# ---------------------------------------------------------------------------

PACKAGEABLE_STATES = ("test", "prod")

# Gate codes as the API contract documents them (11.2, 11.3, 16.4).
CODE_RECORD_MISSING = "PLUGIN_RECORD_NOT_FOUND"
CODE_LIFECYCLE = "PLUGIN_LIFECYCLE_VIOLATION"
CODE_ARTIFACT_MISSING = "PLUGIN_ARTIFACT_MISSING"
CODE_COMPONENT_MISSING = "PLUGIN_COMPONENT_MISSING"


def entry_complete_model(entry):
    """A usable per-arch Plugin_Artifact entry: succeeded build with the
    recorded key, checksum, and signature (11.2, 10.4)."""
    return bool(
        isinstance(entry, dict)
        and entry.get("buildStatus") == "succeeded"
        and entry.get("s3Key")
        and entry.get("checksum")
        and entry.get("signature")
    )


def expected_findings_model(dep_specs):
    """The set of findings the requirements demand, keyed for comparison.

    Per dependency, in gate order: an unresolvable backing Plugin_Record
    fails closed; a lifecycle state outside test/prod rejects identifying
    the Custom_Node_Type and state (11.3, and only then); otherwise every
    compiled architecture lacking a complete Plugin_Artifact rejects
    identifying the Custom_Node_Type and architecture (11.2), and a
    missing registered Plugin_Component version rejects (16.4/11.1).
    """
    expected = set()
    for spec in dep_specs:
        dep = spec["dep"]
        if not spec["in_index"] or spec["record"] is None:
            expected.add((CODE_RECORD_MISSING, dep, None, None))
            continue
        node_type_id = spec["node_type_id"]
        record = spec["record"]
        state = record.get("lifecycle_state")
        if state not in PACKAGEABLE_STATES:
            expected.add((CODE_LIFECYCLE, dep, node_type_id, state))
            continue
        artifacts = record.get("artifacts") or {}
        for arch in spec["archs"]:
            if not entry_complete_model(artifacts.get(arch)):
                expected.add((CODE_ARTIFACT_MISSING, dep, node_type_id, arch))
        component = record.get("component") or {}
        if component.get("status") != "registered":
            expected.add((CODE_COMPONENT_MISSING, dep, node_type_id, None))
    return expected


def finding_key(finding):
    """Project an implementation finding onto the model key space."""
    code = finding["code"]
    dep = finding["dependency"]
    if code == CODE_RECORD_MISSING:
        return (code, dep, None, None)
    if code == CODE_LIFECYCLE:
        return (code, dep, finding["node_type_id"], finding["lifecycle_state"])
    if code == CODE_ARTIFACT_MISSING:
        return (code, dep, finding["node_type_id"], finding["arch"])
    if code == CODE_COMPONENT_MISSING:
        return (code, dep, finding["node_type_id"], None)
    raise AssertionError(f"unknown gate finding code: {code}")


# ---------------------------------------------------------------------------
# Random dep/record worlds
# ---------------------------------------------------------------------------

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")

# Random lifecycle states: the packageable pair, dev, plus unknown /
# absent values that must fail closed (11.3).
LIFECYCLE_STATES = ("dev", "test", "prod", "archived", "", None)

USECASE_ID = "uc-p13"

_complete_entry = st.fixed_dictionaries({
    "buildStatus": st.just("succeeded"),
    "s3Key": st.just(f"workflow-plugins/custom/{USECASE_ID}/a/p.so"),
    "checksum": st.just("ab" * 32),
    "signature": st.just("c2lnLWJ5dGVz"),
})

# Entries with fields randomly present/missing/empty or a wrong build
# status — complete only by coincidence (then genuinely eligible).
_random_entry = st.one_of(
    st.none(),
    st.fixed_dictionaries({}, optional={
        "buildStatus": st.sampled_from(["succeeded", "failed", "running", ""]),
        "s3Key": st.sampled_from(["", f"workflow-plugins/custom/{USECASE_ID}/a/p.so"]),
        "checksum": st.sampled_from(["", "ab" * 32]),
        "signature": st.sampled_from(["", "c2lnLWJ5dGVz"]),
    }),
)

_component = st.one_of(
    st.none(),
    st.just({}),
    st.fixed_dictionaries({"status": st.sampled_from(
        ["registered", "pending", "failed", ""])}),
)


@st.composite
def worlds(draw):
    """A random packaging world: selected architectures, custom plugin
    dependencies compiled per arch, their Custom_Node_Type declarations,
    and backing Plugin_Records in random shapes.

    Each dependency draws an ``eligible`` bias flag; eligible deps are
    built fully packageable so the empty-findings branch of the iff is
    exercised as often as the rejection branch.
    """
    selected_archs = draw(st.lists(
        st.sampled_from(ARCHS), min_size=1, max_size=len(ARCHS), unique=True))
    n_deps = draw(st.integers(min_value=1, max_value=4))

    dep_specs = []
    node_type_items = []
    dep_records = {}
    arch_custom_deps = {arch: [] for arch in selected_archs}

    for i in range(n_deps):
        dep = f"custom:{USECASE_ID}/plugin-{i}"
        node_type_id = f"custom.plugin-{i}"
        plugin_id = f"plg-{i}"
        # The archs this dependency was compiled for: a nonempty subset
        # of the selected architectures.
        dep_archs = sorted(draw(st.lists(
            st.sampled_from(selected_archs), min_size=1,
            max_size=len(selected_archs), unique=True)))

        eligible = draw(st.booleans())
        if eligible:
            in_index = True
            record = {
                "plugin_id": plugin_id,
                "version": draw(st.integers(min_value=1, max_value=9)),
                "lifecycle_state": draw(st.sampled_from(PACKAGEABLE_STATES)),
                "artifacts": {arch: draw(_complete_entry) for arch in dep_archs},
                "component": {"status": "registered"},
            }
        else:
            in_index = draw(st.booleans())
            if draw(st.booleans()):
                record = None  # backing Plugin_Record missing entirely
            else:
                artifact_archs = draw(st.lists(
                    st.sampled_from(ARCHS), max_size=len(ARCHS), unique=True))
                record = {
                    "plugin_id": plugin_id,
                    "version": draw(st.integers(min_value=1, max_value=9)),
                    "lifecycle_state": draw(st.sampled_from(LIFECYCLE_STATES)),
                    "artifacts": {arch: draw(_random_entry)
                                  for arch in artifact_archs},
                    "component": draw(_component),
                }

        if in_index:
            # A CustomNodeTypes item whose declaration mappings carry the
            # custom: dependency, as registration records it (8.6) — the
            # dep_index is derived through custom_dependency_index.
            node_type_items.append({
                "node_type_id": node_type_id,
                "version": 1,
                "plugin_id": plugin_id,
                "plugin_version": record["version"] if record else 1,
                "declaration": {
                    "typeId": node_type_id,
                    "mappings": [{
                        "arch": arch,
                        "elementChain": [{"factory": f"plugin{i}"}],
                        "pluginDependencies": [dep],
                    } for arch in dep_archs],
                },
            })

        dep_records[dep] = record
        for arch in dep_archs:
            arch_custom_deps[arch].append(dep)
        dep_specs.append({
            "dep": dep,
            "node_type_id": node_type_id,
            "in_index": in_index,
            "record": record,
            "archs": dep_archs,
        })

    return dep_specs, node_type_items, arch_custom_deps, dep_records


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(world=worlds())
def test_packaging_gates_on_lifecycle_and_artifacts(packaging, world):
    """**Feature: custom-node-designer, Property 13: Packaging gates on lifecycle state and artifact presence**

    For all combinations of backing Plugin_Record lifecycle states,
    per-architecture artifact availability, component registration, and
    record resolvability, ``custom_plugin_gate_findings`` returns zero
    findings if and only if every dependency is fully packageable, and
    each ineligibility produces exactly the finding identifying the
    Custom_Node_Type and the offending architecture or lifecycle state.

    **Validates: Requirements 11.1, 11.2, 11.3**
    """
    dep_specs, node_type_items, arch_custom_deps, dep_records = world

    dep_index = packaging.custom_dependency_index(node_type_items)
    findings = packaging.custom_plugin_gate_findings(
        arch_custom_deps, dep_index, dep_records)

    expected = expected_findings_model(dep_specs)

    # 11.1: packaging may proceed (no findings) iff every dependency is
    # eligible — records resolvable, lifecycle test/prod, a complete
    # artifact for every compiled arch, component registered.
    assert (findings == []) == (expected == set())

    # Each ineligibility produces exactly the identifying finding: same
    # multiset (no duplicates) and same identification (11.2, 11.3).
    assert len(findings) == len(expected)
    assert {finding_key(f) for f in findings} == expected

    # Findings identify the Custom_Node_Type and arch/state in the
    # human-readable message too (11.2, 11.3).
    for finding in findings:
        if finding["code"] == CODE_LIFECYCLE:
            assert finding["node_type_id"] in finding["message"]
            assert f"'{finding['lifecycle_state']}'" in finding["message"]
        elif finding["code"] == CODE_ARTIFACT_MISSING:
            assert finding["node_type_id"] in finding["message"]
            assert f"'{finding['arch']}'" in finding["message"]
        elif finding["code"] == CODE_COMPONENT_MISSING:
            assert finding["node_type_id"] in finding["message"]
        elif finding["code"] == CODE_RECORD_MISSING:
            assert finding["dependency"] in finding["message"]

    # artifact_entry_complete agrees with the requirements' notion of a
    # usable per-arch Plugin_Artifact entry across all generated shapes.
    for spec in dep_specs:
        record = spec["record"]
        if not record:
            continue
        for entry in (record.get("artifacts") or {}).values():
            assert packaging.artifact_entry_complete(entry) \
                == entry_complete_model(entry)
