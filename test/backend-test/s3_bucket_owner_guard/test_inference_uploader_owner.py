# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""S3 bucket-squatting regression tests for the InferenceUploader component.

The Portal configures the component with the use case's results bucket,
whose default name is derived from the use case's account id
(``dda-inference-results-<account>``) and which nothing creates for the
operator. Another account can pre-create that exact name and receive every
inference image and metadata file the device uploads. Every upload therefore
asserts ``ExpectedBucketOwner``: the account a derived name encodes, or the
explicit ``s3BucketOwner`` configuration; S3 refuses the upload (403) when
another account owns the bucket, and the component records a failed upload
instead of a success.

Both copies of the component (``inference-uploader/`` and the legacy
``com.dda.InferenceUploader/``) are loaded straight from their artifact files
and exercised with a fake S3 client that models S3's refusal.

Run on the host with ``--noconftest`` (the build_script lane's convention).
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
)
UPLOADERS = {
    "current": os.path.join(REPO_ROOT, "inference-uploader", "artifacts", "inference_uploader.py"),
    "legacy": os.path.join(REPO_ROOT, "com.dda.InferenceUploader", "artifacts", "inference_uploader.py"),
}
ACCOUNT = "123456789012"
DERIVED = "dda-inference-results-" + ACCOUNT


class SquattableS3:
    """Owned by `owner`; a managed upload whose ExpectedBucketOwner names
    another account is refused (403), exactly as S3 does."""

    def __init__(self, owner=ACCOUNT):
        self.owner = owner
        self.uploads = []
        self.refused = []

    def upload_file(self, local_path, bucket, key, ExtraArgs=None):
        extra = dict(ExtraArgs or {})
        expected = extra.get("ExpectedBucketOwner")
        if expected is not None and expected != self.owner:
            self.refused.append((bucket, key, expected))
            raise RuntimeError("An error occurred (AccessDenied) when calling the PutObject operation: Access Denied")
        self.uploads.append((bucket, key, extra))


@pytest.fixture(params=sorted(UPLOADERS))
def uploader_module(request, monkeypatch):
    """The component module loaded from its artifact (boto3's client factory
    is stubbed so no network client is built)."""
    path = UPLOADERS[request.param]
    spec = importlib.util.spec_from_file_location("inference_uploader_" + request.param, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: SquattableS3())
    yield module
    sys.modules.pop(spec.name, None)


def _uploader(module, tmp_path, bucket, owner=None, s3=None):
    config = {
        "inferenceResultsPath": str(tmp_path / "inference-results"),
        "uploadIntervalSeconds": 10, "batchSize": 10, "localRetentionDays": 7,
        "uploadImages": True, "uploadMetadata": True,
        "s3Bucket": bucket, "s3Prefix": "uc/dev", "awsRegion": "us-east-1",
    }
    if owner is not None:
        config["s3BucketOwner"] = owner
    (tmp_path / "inference-results").mkdir(exist_ok=True)
    instance = module.InferenceUploader(config)
    if s3 is not None:
        instance.s3_client = s3
    return instance


@pytest.mark.parametrize("bucket, configured, expected", [
    (DERIVED, "", ACCOUNT),                         # derived: the account in the name
    (DERIVED, None, ACCOUNT),
    ("customer-results", "", ""),                   # override: nothing to assert
    ("customer-results", "444455556666", "444455556666"),  # explicit owner wins
    (DERIVED, "444455556666", "444455556666"),
    ("dda-inference-results-12345", "", ""),        # not an account id
    ("dda-inference-results-123456789012-x", "", ""),
])
def test_expected_bucket_owner_rule(uploader_module, bucket, configured, expected):
    assert uploader_module.expected_bucket_owner(bucket, configured) == expected


def test_derived_bucket_upload_asserts_the_account_in_its_name(uploader_module, tmp_path):
    s3 = SquattableS3(owner=ACCOUNT)
    up = _uploader(uploader_module, tmp_path, DERIVED, s3=s3)
    local = tmp_path / "frame.jpg"
    local.write_bytes(b"\xff\xd8jpeg")
    assert up.upload_file(str(local), "uc/dev/frame.jpg") is True
    bucket, key, extra = s3.uploads[0]
    assert bucket == DERIVED and key == "uc/dev/frame.jpg"
    assert extra["ExpectedBucketOwner"] == ACCOUNT
    assert extra["ContentType"] == "image/jpeg"


def test_squatted_derived_bucket_fails_the_upload(uploader_module, tmp_path):
    """The derived name exists under another account: S3 refuses the
    owner-asserted upload and the component reports the upload failed
    (it stays local, never in the attacker's bucket)."""
    s3 = SquattableS3(owner="999999999999")
    up = _uploader(uploader_module, tmp_path, DERIVED, s3=s3)
    local = tmp_path / "meta.jsonl"
    local.write_text("{}\n")
    assert up.upload_file(str(local), "uc/dev/meta.jsonl") is False
    assert s3.uploads == []
    assert s3.refused == [(DERIVED, "uc/dev/meta.jsonl", ACCOUNT)]


def test_override_bucket_is_not_asserted_unless_configured(uploader_module, tmp_path):
    s3 = SquattableS3(owner="999999999999")
    up = _uploader(uploader_module, tmp_path, "customer-results", s3=s3)
    local = tmp_path / "frame.png"
    local.write_bytes(b"\x89PNG")
    assert up.upload_file(str(local), "uc/dev/frame.png") is True
    assert "ExpectedBucketOwner" not in s3.uploads[0][2]

    s3 = SquattableS3(owner="999999999999")
    up = _uploader(uploader_module, tmp_path, "customer-results", owner="444455556666", s3=s3)
    assert up.upload_file(str(local), "uc/dev/frame.png") is False
    assert s3.refused == [("customer-results", "uc/dev/frame.png", "444455556666")]


def test_configuration_exposes_s3_bucket_owner(uploader_module, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", DERIVED)
    monkeypatch.setenv("S3_BUCKET_OWNER", "444455556666")
    monkeypatch.setenv("GG_CONFIG_FILE", "/nonexistent/config.json")
    config = uploader_module.load_configuration()
    assert config["s3Bucket"] == DERIVED
    assert config["s3BucketOwner"] == "444455556666"
    monkeypatch.delenv("S3_BUCKET_OWNER")
    assert uploader_module.load_configuration()["s3BucketOwner"] == ""


def test_recipes_declare_the_owner_setting():
    for recipe in ("inference-uploader/recipe.yaml", "com.dda.InferenceUploader/recipe.yaml"):
        text = open(os.path.join(REPO_ROOT, recipe), encoding="utf-8").read()
        assert "s3BucketOwner: ''" in text, recipe
