"""Unit tests for the catalog data models, port-type compatibility rules,
and the per-arch LocalServer-bundled plugin manifest (task 1.2).

Catalog content coverage (presence and parameterization of every node
type) lands in tasks 1.3/1.4; these tests exercise the mechanisms this
task introduces.
"""

import dataclasses

import pytest

from workflow_core.catalog import (
    ARCHITECTURES,
    CATEGORIES,
    LOCALSERVER_BUNDLED_PLUGINS,
    NODE_CATALOG,
    PORT_TYPE_EVENT_SIGNAL,
    PORT_TYPE_INFERENCE_META,
    PORT_TYPE_VIDEO_FRAMES,
    GstMapping,
    NodeTypeDescriptor,
    ParameterDescriptor,
    PortDescriptor,
    are_port_types_compatible,
    bundled_plugins_for,
    get_node_type,
    incompatibility_reason,
    nodes_by_category,
)


class TestDataModels:
    def test_descriptors_are_frozen_dataclasses(self):
        port = PortDescriptor("in", PORT_TYPE_VIDEO_FRAMES)
        with pytest.raises(dataclasses.FrozenInstanceError):
            port.name = "other"

        param = ParameterDescriptor("gain", "int", required=False, default=4)
        with pytest.raises(dataclasses.FrozenInstanceError):
            param.default = 5

    def test_gst_mapping_defaults(self):
        mapping = GstMapping(arch="x86_64")
        assert mapping.element_chain == []
        assert mapping.executor_binding is None
        assert mapping.plugin_dependencies == []

    def test_mapping_for_returns_arch_specific_mapping(self):
        descriptor = get_node_type("folder_source")
        assert isinstance(descriptor, NodeTypeDescriptor)
        for arch in ARCHITECTURES:
            mapping = descriptor.mapping_for(arch)
            assert mapping is not None
            assert mapping.arch == arch
        assert descriptor.mapping_for("no_such_arch") is None


class TestPortCompatibility:
    def test_exact_match_is_compatible(self):
        for port_type in (PORT_TYPE_VIDEO_FRAMES, PORT_TYPE_INFERENCE_META,
                          PORT_TYPE_EVENT_SIGNAL):
            assert are_port_types_compatible(port_type, port_type)
            assert incompatibility_reason(port_type, port_type) is None

    def test_inference_meta_coerces_to_video_frames(self):
        # InferenceMeta rides the same buffer stream as VideoFrames.
        assert are_port_types_compatible(PORT_TYPE_INFERENCE_META,
                                         PORT_TYPE_VIDEO_FRAMES)
        assert incompatibility_reason(PORT_TYPE_INFERENCE_META,
                                      PORT_TYPE_VIDEO_FRAMES) is None

    def test_coercion_is_directional(self):
        assert not are_port_types_compatible(PORT_TYPE_VIDEO_FRAMES,
                                             PORT_TYPE_INFERENCE_META)

    def test_incompatible_pairs_get_descriptive_reason(self):
        reason = incompatibility_reason(PORT_TYPE_VIDEO_FRAMES,
                                        PORT_TYPE_EVENT_SIGNAL)
        assert reason is not None
        assert PORT_TYPE_VIDEO_FRAMES in reason
        assert PORT_TYPE_EVENT_SIGNAL in reason

    def test_unknown_port_types_are_rejected_with_reason(self):
        assert incompatibility_reason("Bogus", PORT_TYPE_VIDEO_FRAMES) is not None
        assert incompatibility_reason(PORT_TYPE_VIDEO_FRAMES, "Bogus") is not None


class TestBundledPluginManifest:
    def test_manifest_covers_every_architecture(self):
        assert set(LOCALSERVER_BUNDLED_PLUGINS) == set(ARCHITECTURES)

    def test_bundled_plugins_for_unknown_arch_raises(self):
        with pytest.raises(KeyError):
            bundled_plugins_for("riscv")

    def test_existing_dda_elements_are_bundled_everywhere(self):
        for arch in ARCHITECTURES:
            bundled = bundled_plugins_for(arch)
            assert {"emltriton", "emlcapture", "emoutputevent"} <= bundled


class TestCatalogAccess:
    def test_get_node_type_roundtrip(self):
        for descriptor in NODE_CATALOG:
            assert get_node_type(descriptor.type_id) is descriptor
        assert get_node_type("no_such_type") is None

    def test_nodes_by_category_uses_known_categories(self):
        grouped = nodes_by_category()
        assert set(grouped) <= set(CATEGORIES)
        assert sum(len(v) for v in grouped.values()) == len(NODE_CATALOG)
