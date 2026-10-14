"""
Vendored SDK pins of the shared Lambda layer (portal-usecase-bucket-config).

`layers/shared/python` ships boto3/botocore 1.43.101 (design §6.2) so the
S3 bucket ABAC and tagging operations the use-case bucket configuration
relies on are present whatever the Lambda-managed runtime bundles. The
probe below runs in a fresh interpreter with the layer directory as
`sys.path[0]` — exactly how the layer shadows the runtime's copy on Lambda
— and reports the version and operation names; an un-pin or a downgrade
fails the lane.

Validates: Requirements 1.5, 9.13
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LAYER = os.path.abspath(
    os.path.join(_HERE, "..", "layers", "shared", "python"))

EXPECTED_SDK_VERSION = "1.43.101"

_PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import boto3, botocore, s3transfer, jmespath, dateutil, six
from botocore.loaders import Loader
from botocore.model import ServiceModel
loader = Loader()
def model(name):
    return ServiceModel(loader.load_service_model(name, 'service-2'), service_name=name)
s3 = model('s3')
print(json.dumps({
    'files': {m.__name__: m.__file__ for m in (boto3, botocore, s3transfer, jmespath, dateutil, six)},
    'versions': {m.__name__: m.__version__ for m in (boto3, botocore, s3transfer, jmespath, dateutil, six)},
    's3_operations': sorted(s3.operation_names),
    's3control_operations': sorted(model('s3control').operation_names),
    'create_bucket_configuration_members': sorted(s3.shape_for('CreateBucketConfiguration').members),
}))
"""


@pytest.fixture(scope="module")
def layer_sdk():
    """What a fresh interpreter sees with the layer directory first on sys.path."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, _SHARED_LAYER],
        check=True, capture_output=True, text=True, env=env, timeout=120)
    return json.loads(out.stdout)


def test_sdk_modules_resolve_to_the_vendored_layer_copies(layer_sdk):
    for name, path in layer_sdk["files"].items():
        assert path.startswith(_SHARED_LAYER + os.sep), (
            f"{name} resolved to {path}, not the vendored layer copy")


def test_vendored_boto3_and_botocore_versions_are_pinned(layer_sdk):
    assert layer_sdk["versions"]["botocore"] == EXPECTED_SDK_VERSION
    assert layer_sdk["versions"]["boto3"] == EXPECTED_SDK_VERSION


def test_vendored_dependency_versions_are_pinned(layer_sdk):
    assert layer_sdk["versions"]["s3transfer"] == "0.19.2"
    assert layer_sdk["versions"]["jmespath"] == "1.1.0"
    assert layer_sdk["versions"]["dateutil"] == "2.9.0.post0"
    assert layer_sdk["versions"]["six"] == "1.17.0"


def test_s3_model_has_bucket_abac_operations(layer_sdk):
    assert {"PutBucketAbac", "GetBucketAbac"} <= set(layer_sdk["s3_operations"])


def test_s3control_model_has_bucket_tagging_operations(layer_sdk):
    assert {"TagResource", "UntagResource", "ListTagsForResource"} <= set(
        layer_sdk["s3control_operations"])


def test_s3_create_bucket_configuration_accepts_tags(layer_sdk):
    assert "Tags" in layer_sdk["create_bucket_configuration_members"]
