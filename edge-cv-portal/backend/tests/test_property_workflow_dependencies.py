"""Property test for Workflow_Component plugin dependencies (task 10.4).

**Feature: custom-node-designer, Property 21: Workflow_Component dependencies are exactly the custom plugins**

For all random compiled pluginDependencies lists (arbitrary mixes of
curated GStreamer plugin names, ``python:`` runtime packages, and
``custom:{usecase}/{name}`` Custom_Node_Type dependencies, duplicates
included) and random backing Plugin_Record maps:

- ``split_plugin_dependencies`` routes every dependency to exactly one
  bucket (curated / custom / python), preserving the multiset — no
  dependency is lost, duplicated across buckets, or misrouted;
- ``custom:`` dependencies NEVER appear in the inline curated plugin
  list (they are delivered by Plugin_Component dependency, not bundled
  inline — Requirement 11.1);
- ``plugin_component_dependencies`` yields exactly one HARD Greengrass
  dependency on ``dda.plugin.{pluginId}`` per distinct backing
  Plugin_Record, pinned with ``VersionRequirement``
  ``>={v}.0.0 <{v+1}.0.0`` — no curated/python leakage, no extras;
- ``build_recipe`` includes that block as ``ComponentDependencies``
  exactly when it is non-empty.

**Validates: Requirements 16.4, 11.1**

The functions under test are pure over plain values, so they are
exercised directly with no AWS involvement. The module is imported
through the shared moto-backed session fixture only so the real
``shared_utils`` layer (not a test fake) backs the import, mirroring
test_workflow_packaging_custom_plugins.py.
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
# Reference expectations, restated from Requirements 16.4 / 11.1 and the
# design (not imported from the implementation, so the test cannot
# silently agree with a wrong prefix or version scheme).
# ---------------------------------------------------------------------------

PYTHON_PREFIX = "python:"
CUSTOM_PREFIX = "custom:"
PLUGIN_COMPONENT_PREFIX = "dda.plugin."


def expected_version_requirement(version: int) -> str:
    return f">={version}.0.0 <{version + 1}.0.0"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# The colon is deliberately excluded so a curated name can never
# accidentally spell a "python:"/"custom:" prefix.
_name_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
_names = st.text(alphabet=_name_alphabet, min_size=1, max_size=24)

curated_deps = _names
python_deps = st.builds(lambda pkg: PYTHON_PREFIX + pkg, _names)
custom_deps = st.builds(
    lambda usecase, name: f"{CUSTOM_PREFIX}{usecase}/{name}", _names, _names)

# A compiled pluginDependencies list: any mix, duplicates allowed.
dependency_lists = st.lists(
    st.one_of(curated_deps, python_deps, custom_deps), max_size=20)

# A pool of distinct backing Plugin_Records (plugin_id -> version).
plugin_pools = st.dictionaries(
    _names, st.integers(min_value=1, max_value=9999), min_size=1, max_size=6)


@st.composite
def dependency_scenarios(draw):
    """A compiled dependency list plus a dep_records map assigning every
    distinct custom dependency a backing Plugin_Record from a shared
    pool (several Custom_Node_Types may share one plugin)."""
    deps = draw(dependency_lists)
    pool = draw(plugin_pools)
    plugin_ids = sorted(pool)
    dep_records = {}
    for dep in sorted({d for d in deps if d.startswith(CUSTOM_PREFIX)}):
        plugin_id = draw(st.sampled_from(plugin_ids))
        dep_records[dep] = {
            "plugin_id": plugin_id,
            "version": pool[plugin_id],
            "lifecycle_state": "test",
        }
    return deps, dep_records


# Valid Target_Architecture subsets for build_recipe's final_keys.
ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")
arch_sets = st.frozensets(st.sampled_from(ARCHS), min_size=1)


# ---------------------------------------------------------------------------
# Property 21
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(scenario=dependency_scenarios(), archs=arch_sets)
def test_dependencies_are_exactly_the_custom_plugins(packaging, scenario,
                                                     archs):
    """**Feature: custom-node-designer, Property 21: Workflow_Component dependencies are exactly the custom plugins**

    For all random compiled dependency lists and backing-record maps,
    split_plugin_dependencies partitions every dependency into exactly
    one bucket with custom plugins never in the inline curated list,
    plugin_component_dependencies declares exactly one pinned HARD
    dda.plugin.{pluginId} dependency per distinct backing Plugin_Record
    and nothing else, and build_recipe carries the block as
    ComponentDependencies iff it is non-empty.

    **Validates: Requirements 16.4, 11.1**
    """
    deps, dep_records = scenario

    gst, custom, python = packaging.split_plugin_dependencies(deps)

    # --- Partition: every dependency routed to exactly one bucket, the
    # multiset preserved (nothing lost, duplicated, or invented).
    reassembled = sorted(
        list(gst) + list(custom) + [PYTHON_PREFIX + p for p in python])
    assert reassembled == sorted(deps)

    # --- Routing is by prefix; custom deps NEVER inline (11.1, 16.4).
    assert all(not d.startswith((CUSTOM_PREFIX, PYTHON_PREFIX)) for d in gst)
    assert all(d.startswith(CUSTOM_PREFIX) for d in custom)
    assert all(not p.startswith(PYTHON_PREFIX) for p in python)
    assert set(custom) == {d for d in deps if d.startswith(CUSTOM_PREFIX)}
    assert not any(d.startswith(CUSTOM_PREFIX) for d in gst)

    # --- ComponentDependencies: exactly one pinned HARD entry per
    # distinct backing Plugin_Record — no curated/python leakage, no
    # extras (16.4).
    dependencies = packaging.plugin_component_dependencies(dep_records)

    expected = {
        f"{PLUGIN_COMPONENT_PREFIX}{record['plugin_id']}": {
            "VersionRequirement":
                expected_version_requirement(record["version"]),
            "DependencyType": "HARD",
        }
        for record in dep_records.values()
    }
    assert dependencies == expected

    # Only Plugin_Components of the workflow's custom plugins appear.
    backing_ids = {r["plugin_id"] for r in dep_records.values()}
    assert {name[len(PLUGIN_COMPONENT_PREFIX):] for name in dependencies} \
        == backing_ids
    assert all(name.startswith(PLUGIN_COMPONENT_PREFIX)
               for name in dependencies)

    # --- Recipe inclusion: ComponentDependencies present iff non-empty.
    final_keys = {
        arch: f"workflows/components/wf-1/1/{arch}/workflow-{arch}.zip"
        for arch in archs
    }
    recipe = packaging.build_recipe(
        "wf-1", 1, "bucket", final_keys,
        component_dependencies=dependencies)

    if dependencies:
        assert recipe["ComponentDependencies"] == dependencies
    else:
        assert "ComponentDependencies" not in recipe
