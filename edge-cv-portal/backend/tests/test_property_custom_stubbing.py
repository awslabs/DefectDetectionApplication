"""Property test for custom-node test-run stubbing (task 13.2).

**Feature: custom-node-designer, Property 17: Test-run stubbing is exactly the unavailable custom nodes**

For all custom-node type sets with random x86_64 artifact availabilities
(succeeded / failed / building / missing s3Key / malformed / absent
entries), the test-run compile step's stub decision selects exactly the
Custom_Node_Types lacking a successfully built x86_64 Plugin_Artifact,
and apply_custom_stubs replaces exactly those descriptors with the
pass-through recording stub (identity element named via the
custom_stub_<nodeId> template, mapped for the target and ``sim``
architectures) while every other descriptor is left untouched.

**Validates: Requirements 12.2**

The functions under test (x86_64_artifact_available,
stubbed_custom_type_ids, apply_custom_stubs, stub_descriptor,
custom_stub_mapping) are pure over plain dicts and catalog descriptors,
so they are exercised directly with no AWS calls. The module is imported
through the shared moto-backed session fixture only so its module-level
boto3 clients bind the mock (workflow_test_steps has no shared_utils
dependency), mirroring test_workflow_testing_errors.py.
"""

from __future__ import annotations

import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from workflow_core.catalog.models import (
    ARCH_SIM,
    ARCHITECTURES,
    CATEGORIES,
    GstMapping,
    NodeTypeDescriptor,
    PortDescriptor,
    PORT_TYPES,
)


@pytest.fixture(scope="module")
def steps_module(aws_stack):
    """Import workflow_test_steps inside the moto mock so its module-level
    boto3 clients (and node_catalog_resolution's) are intercepted."""
    for name in ("workflow_test_steps", "node_catalog_resolution"):
        sys.modules.pop(name, None)
    import workflow_test_steps

    return workflow_test_steps


# ---------------------------------------------------------------------------
# Reference expectations, restated from Requirement 12.2 / the design
# (not derived from the implementation, so the test cannot silently
# agree with a wrong availability rule).
# ---------------------------------------------------------------------------

def artifact_is_usable(entry):
    """A usable x86_64 Plugin_Artifact entry: a successfully built
    artifact with a stored library object (buildStatus 'succeeded' and a
    nonempty s3Key)."""
    return (isinstance(entry, dict)
            and entry.get("buildStatus") == "succeeded"
            and bool(entry.get("s3Key")))


#: The identity-element name prefix the sandbox harness keys on (12.2:
#: "identity element named custom_stub_<nodeId>").
EXPECTED_STUB_PREFIX = "custom_stub_"


# ---------------------------------------------------------------------------
# Strategies: random custom type sets, artifact entry shapes, and
# descriptors.
# ---------------------------------------------------------------------------

_name_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_"
_names = st.text(alphabet=_name_alphabet, min_size=1, max_size=20)

_s3_keys = _names.map(lambda n: f"plugins/artifacts/{n}.so")


@st.composite
def artifact_entries(draw):
    """One per-type x86_64 artifact entry shape, usable or not:
    succeeded-with-key, succeeded-without-key, empty key, failed,
    building, malformed (statusless / empty dict), or None. The ABSENT
    sentinel (entry omitted from the dict entirely) is drawn separately.
    """
    shape = draw(st.sampled_from([
        "usable", "usable_extra_keys", "missing_s3_key", "empty_s3_key",
        "failed", "building", "no_status", "empty_dict", "none",
    ]))
    if shape == "usable":
        return {"buildStatus": "succeeded", "s3Key": draw(_s3_keys)}
    if shape == "usable_extra_keys":
        return {"buildStatus": "succeeded", "s3Key": draw(_s3_keys),
                "builtAt": draw(st.integers(min_value=1)),
                "sizeBytes": draw(st.integers(min_value=0))}
    if shape == "missing_s3_key":
        return {"buildStatus": "succeeded"}
    if shape == "empty_s3_key":
        return {"buildStatus": "succeeded", "s3Key": ""}
    if shape == "failed":
        return {"buildStatus": "failed", "s3Key": draw(_s3_keys),
                "error": "compile error"}
    if shape == "building":
        return {"buildStatus": "building"}
    if shape == "no_status":
        return {"s3Key": draw(_s3_keys)}
    if shape == "empty_dict":
        return {}
    return None


def _ports(draw, prefix):
    return [
        PortDescriptor(name=f"{prefix}{i}",
                       port_type=draw(st.sampled_from(PORT_TYPES)))
        for i in range(draw(st.integers(min_value=0, max_value=2)))
    ]


@st.composite
def descriptors(draw, type_id):
    """A random Custom_Node_Type descriptor with arbitrary realizations."""
    mappings = [
        GstMapping(
            arch=arch,
            element_chain=[{"factory": draw(_names),
                            "args_template": {"name": draw(_names)}}],
            plugin_dependencies=draw(
                st.lists(_names, max_size=2)),
        )
        for arch in draw(st.lists(st.sampled_from(ARCHITECTURES),
                                  unique=True, min_size=1, max_size=3))
    ]
    return NodeTypeDescriptor(
        type_id=type_id,
        category=draw(st.sampled_from(CATEGORIES)),
        display_name=draw(_names),
        inputs=_ports(draw, "in"),
        outputs=_ports(draw, "out"),
        parameters=[],
        mappings=mappings,
        hardware_dependent=draw(st.booleans()),
    )


@st.composite
def stubbing_cases(draw):
    """A random custom type set with per-type artifact availability.

    Each type's entry is either drawn from artifact_entries() or absent
    from the dict altogether; the dict may also carry entries for types
    outside the set (unused custom types of the same Use_Case)."""
    type_ids = draw(st.lists(_names, unique=True, min_size=0, max_size=6))
    entries = {}
    for type_id in type_ids:
        if draw(st.booleans()) or not type_ids:
            entries[type_id] = draw(artifact_entries())
        # else: absent entirely -> load failed closed, must be stubbed
    for extra_id in draw(st.lists(_names, unique=True, max_size=2)):
        if extra_id not in type_ids:
            entries[extra_id] = draw(artifact_entries())
    custom_descriptors = [draw(descriptors(type_id)) for type_id in type_ids]
    target_arch = draw(st.sampled_from(ARCHITECTURES))
    return type_ids, entries, custom_descriptors, target_arch


# ---------------------------------------------------------------------------
# Property 17
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(case=stubbing_cases())
def test_stubbing_is_exactly_the_unavailable_custom_nodes(steps_module,
                                                          case):
    """**Feature: custom-node-designer, Property 17: Test-run stubbing is exactly the unavailable custom nodes**

    For all custom type sets and artifact entry shapes, the stub set is
    exactly the types lacking a usable x86_64 Plugin_Artifact, and
    apply_custom_stubs substitutes the pass-through recording stub for
    exactly those descriptors (identity element with the
    custom_stub_<nodeId> name template, target + sim architectures)
    leaving every other descriptor untouched.

    **Validates: Requirements 12.2**
    """
    type_ids, entries, custom_descriptors, target_arch = case

    # -- stub decision: exactly the types without a usable x86_64 build --
    stub_ids = steps_module.stubbed_custom_type_ids(type_ids, entries)
    expected_stub_ids = frozenset(
        type_id for type_id in type_ids
        if not artifact_is_usable(entries.get(type_id)))
    assert stub_ids == expected_stub_ids

    # Availability of entries outside the used set never leaks in.
    assert stub_ids <= frozenset(type_ids)

    # x86_64_artifact_available agrees with the restated rule per entry.
    for type_id in type_ids:
        assert steps_module.x86_64_artifact_available(
            entries.get(type_id)) == artifact_is_usable(entries.get(type_id))

    # -- substitution: exactly the stubbed descriptors are replaced --
    result = steps_module.apply_custom_stubs(
        custom_descriptors, stub_ids, target_arch)

    assert len(result) == len(custom_descriptors)
    expected_archs = ([target_arch] if target_arch == ARCH_SIM
                      else [target_arch, ARCH_SIM])

    for original, resolved in zip(custom_descriptors, result):
        assert resolved.type_id == original.type_id
        if original.type_id not in stub_ids:
            # Untouched: the very same descriptor object passes through.
            assert resolved is original
            continue

        # Declaration is identical; only the realizations change.
        assert resolved.category == original.category
        assert resolved.display_name == original.display_name
        assert resolved.inputs == original.inputs
        assert resolved.outputs == original.outputs
        assert resolved.parameters == original.parameters
        assert resolved.hardware_dependent == original.hardware_dependent

        # Every realization is the pass-through recording stub, mapped
        # for the target arch and sim (identically stubbed under the
        # hardware-dependent sim-stub rule, 12.2).
        assert [m.arch for m in resolved.mappings] == expected_archs
        for mapping in resolved.mappings:
            assert mapping.executor_binding is None
            assert mapping.plugin_dependencies == []
            assert mapping.element_chain == [{
                "factory": "identity",
                "args_template": {"name": "{custom_stub_name}"},
            }]

    # The harness contract: stub instances are named custom_stub_<nodeId>.
    assert steps_module.CUSTOM_STUB_ELEMENT_PREFIX == EXPECTED_STUB_PREFIX
