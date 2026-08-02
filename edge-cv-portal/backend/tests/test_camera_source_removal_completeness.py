"""
Removal completeness for the generic ``camera_source`` node type
(csi-icam-input-nodes Property 4, Requirements 3.1, 3.2, 3.3, 8.3).

The generic ``camera_source`` node is removed outright and replaced by
``csi_camera_source`` and ``icam_source``. This asserts the removal is
complete across every seam that derives from the catalog: the catalog and
its lookup, ``BUILTIN_TYPE_IDS``, the served ``/workflows/node-catalog``
payload, the Component_Packager type-id constants, and the deploy-time
compatibility map.
"""
import json
import sys

import pytest


@pytest.fixture(scope="module")
def validation_module(aws_stack):
    sys.modules.pop("workflow_validation", None)
    import workflow_validation

    return workflow_validation


@pytest.fixture(scope="module")
def packaging(aws_stack):
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


@pytest.fixture(scope="module")
def deployments(aws_stack):
    for module_name in ("deployments", "workflow_guards"):
        sys.modules.pop(module_name, None)
    import deployments

    return deployments


def served_catalog(module):
    event = {
        "httpMethod": "GET",
        "resource": "/workflows/node-catalog",
        "path": "/workflows/node-catalog",
        "requestContext": {"authorizer": {"claims": {
            "sub": "user-1", "email": "u@example.com",
            "cognito:username": "user-1", "custom:role": "DataScientist"}}},
    }
    response = module.handler(event, None)
    assert response["statusCode"] == 200
    return json.loads(response["body"])


class TestCatalogRemoval:
    def test_camera_source_absent_from_catalog(self):
        from workflow_core.catalog import NODE_CATALOG, get_node_type
        ids = {d.type_id for d in NODE_CATALOG}
        assert "camera_source" not in ids
        assert get_node_type("camera_source") is None
        # The two typed replacements are present.
        assert {"csi_camera_source", "icam_source"} <= ids
        assert get_node_type("csi_camera_source") is not None
        assert get_node_type("icam_source") is not None

    def test_builtin_type_ids_reflect_the_swap(self):
        from custom_node_types import BUILTIN_TYPE_IDS
        assert "camera_source" not in BUILTIN_TYPE_IDS
        assert "csi_camera_source" in BUILTIN_TYPE_IDS
        assert "icam_source" in BUILTIN_TYPE_IDS

    def test_resolution_layer_builtin_ids_reflect_the_swap(self):
        from node_catalog_resolution import BUILTIN_TYPE_IDS
        assert "camera_source" not in BUILTIN_TYPE_IDS
        assert {"csi_camera_source", "icam_source"} <= BUILTIN_TYPE_IDS


class TestServedPayloadRemoval:
    def test_served_catalog_swaps_camera_source(self, validation_module):
        body = served_catalog(validation_module)
        by_id = {n["typeId"]: n for n in body["nodeTypes"]}
        assert "camera_source" not in by_id
        assert "csi_camera_source" in by_id
        assert "icam_source" in by_id
        # Both new types are served under the input category.
        assert by_id["csi_camera_source"]["category"] == "input"
        assert by_id["icam_source"]["category"] == "input"
        assert by_id["csi_camera_source"]["displayName"] == "CSI Camera Input"
        assert by_id["icam_source"]["displayName"] == "ICAM"


class TestPackagingRemoval:
    def test_packaging_type_id_constants_swapped(self, packaging):
        assert not hasattr(packaging, "CAMERA_SOURCE_TYPE_ID")
        assert packaging.CSI_CAMERA_SOURCE_TYPE_ID == "csi_camera_source"
        assert packaging.ICAM_SOURCE_TYPE_ID == "icam_source"


class TestDeploymentsRemoval:
    def test_compat_map_swapped(self, deployments):
        compat = deployments._CAMERA_COMPATIBLE_SOURCE_TYPES
        assert "camera_source" not in compat
        assert compat["icam_source"] == frozenset(
            {"ICam", "V4L2Discovered", "Camera"})
        assert compat["csi_camera_source"] == frozenset(
            {"NvidiaCSI", "Camera"})
        # aravis_camera_source is unchanged by this feature.
        assert compat["aravis_camera_source"] == frozenset(
            {"Camera", "AravisDiscovered"})
