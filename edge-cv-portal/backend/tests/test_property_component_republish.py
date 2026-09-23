"""
Property test for Plugin_Component republish detection and revision
numbering in plugin_components.py (custom-node-source-lifecycle task 3.2).

**Feature: custom-node-source-lifecycle, Property 8: Republish detection
and revision numbering** — Validates: Requirements 8.1, 8.2, 8.3, 8.6, 10.2
"""
import sys

import pytest
from hypothesis import given, strategies as st

from test_plugin_components import components_module  # noqa: F401

ARCHES = ["x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6", "arm64_jp7"]

_checksum = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)
_entry = st.fixed_dictionaries({
    "buildStatus": st.sampled_from(["succeeded", "failed", "building"]),
}, optional={"checksum": _checksum})
_artifacts = st.dictionaries(st.sampled_from(ARCHES), _entry, max_size=6)

_pointer = st.one_of(
    st.none(),
    # legacy registered pointer: architectures only
    st.fixed_dictionaries({
        "status": st.sampled_from(["registered", "failed", "packaging"]),
        "architectures": st.lists(st.sampled_from(ARCHES), max_size=6, unique=True),
    }, optional={"revision": st.integers(min_value=0, max_value=5)}),
    # full pointer with checksums
    st.fixed_dictionaries({
        "status": st.sampled_from(["registered", "failed", "packaging"]),
        "architectures": st.lists(st.sampled_from(ARCHES), max_size=6, unique=True),
        "artifact_checksums": st.dictionaries(st.sampled_from(ARCHES), _checksum, max_size=6),
        "revision": st.integers(min_value=0, max_value=5),
    }),
)


def succeeded_checksums(artifacts):
    return {a: e["checksum"] for a, e in artifacts.items()
            if e.get("buildStatus") == "succeeded" and e.get("checksum")}


@pytest.fixture(scope="module")
def workflow_packaging_module(aws_stack, components_module):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging
    return workflow_packaging


class TestRepublishDetection:
    @given(pointer=_pointer, artifacts=_artifacts)
    def test_needs_republish_iff_set_differs_or_not_registered(
            self, components_module, pointer, artifacts):
        item = {"artifacts": artifacts}
        if pointer is not None:
            item["component"] = pointer
        desired = succeeded_checksums(artifacts)

        if pointer is None or pointer.get("status") != "registered":
            expected = True
        elif "artifact_checksums" in pointer:
            expected = pointer["artifact_checksums"] != desired
        else:
            expected = sorted(pointer["architectures"]) != sorted(desired)
        assert components_module.needs_republish(item) == expected

    @given(pointer=_pointer, artifacts=_artifacts, plugin_version=st.integers(1, 50))
    def test_next_revision_and_version_string(self, components_module,
                                              workflow_packaging_module,
                                              pointer, artifacts, plugin_version):
        item = {"artifacts": artifacts}
        if pointer is not None:
            item["component"] = pointer
        revision = components_module.next_component_revision(item)
        recorded = int((pointer or {}).get("revision") or 0)
        if pointer is not None and pointer.get("status") == "registered":
            assert revision == recorded + 1
        else:
            # never registered (or a failed attempt): first publish is r0,
            # a failed attempt retries its own revision
            assert revision == recorded

        version = components_module.component_version_for(plugin_version, revision)
        assert version == f"{plugin_version}.0.{revision}"
        # Every produced version satisfies the workflow's pin range (8.6).
        requirement = workflow_packaging_module.plugin_version_requirement(plugin_version)
        assert requirement == f">={plugin_version}.0.0 <{plugin_version + 1}.0.0"
        major, minor, patch = (int(x) for x in version.split("."))
        assert (plugin_version, 0, 0) <= (major, minor, patch) < (plugin_version + 1, 0, 0)

    @given(artifacts=_artifacts)
    def test_first_registration_is_revision_zero(self, components_module, artifacts):
        assert components_module.next_component_revision({"artifacts": artifacts}) == 0
        assert components_module.needs_republish({"artifacts": artifacts}) is True
