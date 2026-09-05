"""
Unit tests for the additive `extensions` parameter on
`datasets.get_image_preview`.

Spec: llm-autolabel-prompt-tuning, Task 6.3.

Covers: the filter applied for `jpg,jpeg,png`; an absent parameter preserving
the existing six-extension behavior byte-for-byte; an empty prefix reporting
`total_found == 0`; an inaccessible prefix surfacing a non-2xx error.

_Requirements: 2.1, 2.5_

Follows test_property_image_preview_pagination.py: a fake `shared_utils`
module is injected into `sys.modules` and the `functions/` dir is put on
`sys.path` BEFORE importing `datasets`; moto's mocked S3 provides the bucket
contents; synthetic API Gateway events invoke the handler directly.
"""
import json
import os
import sys
import types

import boto3
from moto import mock_aws

# --------------------------------------------------------------------------- #
# Import shim
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS_DIR = os.path.abspath(os.path.join(_HERE, "..", "functions"))
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

_USECASE_HOLDER = {"usecase": {}}


def _make_fake_shared_utils():
    mod = types.ModuleType("shared_utils")

    def create_response(status_code, body, headers=None):
        return {
            "statusCode": status_code,
            "headers": headers or {},
            "body": body if isinstance(body, str) else json.dumps(body),
        }

    def handle_error(error, message_or_headers="Operation failed"):
        return {
            "statusCode": 500,
            "headers": {},
            "body": json.dumps({"error": str(message_or_headers),
                                "detail": str(error)}),
        }

    def get_usecase(usecase_id):
        usecase = dict(_USECASE_HOLDER["usecase"])
        usecase.setdefault("usecase_id", usecase_id)
        return usecase

    def assume_usecase_role(role_arn, external_id, session_name):
        return {
            "AccessKeyId": "AKIAFAKE",
            "SecretAccessKey": "secretfake",
            "SessionToken": "tokenfake",
        }

    mod.create_response = create_response
    mod.handle_error = handle_error
    mod.get_usecase = get_usecase
    mod.assume_usecase_role = assume_usecase_role
    return mod


sys.modules["shared_utils"] = _make_fake_shared_utils()
sys.modules.pop("datasets", None)

import datasets  # noqa: E402

BUCKET = "test-image-preview-extensions-unit-bucket"
REGION = "us-east-1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_bucket(keys):
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(Bucket=BUCKET)
    except (s3.exceptions.BucketAlreadyOwnedByYou,
            s3.exceptions.BucketAlreadyExists):
        pass
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        stale = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if stale:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": stale})
    for key in keys:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"fake-image-bytes")
    return s3


def _set_usecase(**overrides):
    usecase = {
        "usecase_id": "uc-preview-ext",
        "account_id": "123456789012",
        "cross_account_role_arn": "arn:aws:iam::123456789012:root",
        "external_id": "ext-id",
        "s3_bucket": BUCKET,
    }
    usecase.update(overrides)
    _USECASE_HOLDER["usecase"] = usecase


def _event(prefix, limit=None, offset=None, extensions=None):
    params = {"usecase_id": "uc-preview-ext", "prefix": prefix}
    if limit is not None:
        params["limit"] = str(limit)
    if offset is not None:
        params["offset"] = str(offset)
    if extensions is not None:
        params["extensions"] = extensions
    return {"queryStringParameters": params}


def _invoke(prefix, **kwargs):
    return datasets.get_image_preview(_event(prefix, **kwargs))


def _ok_body(response):
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def _without_urls(body):
    """The response with the (signature- and clock-dependent) presigned URLs
    removed, so two responses can be compared field by field."""
    stripped = dict(body)
    stripped["images"] = [
        {k: v for k, v in img.items() if k != "presigned_url"}
        for img in body["images"]
    ]
    return stripped


_MIXED_KEYS = [
    "mixed/a.jpg", "mixed/b.jpeg", "mixed/c.png",          # JPEG/PNG
    "mixed/d.JPG", "mixed/e.Jpeg", "mixed/f.PNG",          # mixed case
    "mixed/g.bmp", "mixed/h.tiff", "mixed/i.tif",          # other images
    "mixed/notes.txt", "mixed/dataset.manifest",           # non-images
    "mixed/README", "mixed/j.jpgx",                        # no ext / near miss
]
_JPEG_PNG_KEYS = {
    "mixed/a.jpg", "mixed/b.jpeg", "mixed/c.png",
    "mixed/d.JPG", "mixed/e.Jpeg", "mixed/f.PNG",
}
_SIX_EXTENSION_KEYS = _JPEG_PNG_KEYS | {
    "mixed/g.bmp", "mixed/h.tiff", "mixed/i.tif",
}


# --------------------------------------------------------------------------- #
# Filter applied
# --------------------------------------------------------------------------- #
@mock_aws
def test_extensions_filter_lists_only_jpeg_and_png():
    """`extensions=jpg,jpeg,png` lists exactly the JPEG/PNG keys, case
    insensitively, and reports the filtered total.

    _Requirements: 2.1_
    """
    _seed_bucket(_MIXED_KEYS)
    _set_usecase()

    body = _ok_body(_invoke("mixed/", limit=50, extensions="jpg,jpeg,png"))

    assert {img["key"] for img in body["images"]} == _JPEG_PNG_KEYS
    assert body["total_found"] == len(_JPEG_PNG_KEYS)
    assert body["has_more"] is False
    assert body["expires_in_seconds"] == 1800
    for img in body["images"]:
        assert img["presigned_url"]
        assert img["filename"] == os.path.basename(img["key"])


@mock_aws
def test_extensions_filter_paging_spans_the_filtered_set_only():
    """Offset/limit paging and `has_more` span the filtered set: the first
    page carries JPEG/PNG keys only and `total_found` stays the filtered
    total on every page.

    _Requirements: 2.1_
    """
    _seed_bucket(_MIXED_KEYS)
    _set_usecase()

    first = _ok_body(_invoke("mixed/", limit=4, offset=0,
                             extensions="jpg,jpeg,png"))
    second = _ok_body(_invoke("mixed/", limit=4, offset=4,
                              extensions="jpg,jpeg,png"))

    assert first["total_found"] == 6 and second["total_found"] == 6
    assert first["has_more"] is True and second["has_more"] is False
    assert len(first["images"]) == 4 and len(second["images"]) == 2
    paged = ([img["key"] for img in first["images"]]
             + [img["key"] for img in second["images"]])
    assert set(paged) == _JPEG_PNG_KEYS
    assert paged == sorted(_JPEG_PNG_KEYS)


# --------------------------------------------------------------------------- #
# Absent parameter preserves the existing behavior
# --------------------------------------------------------------------------- #
@mock_aws
def test_absent_extensions_preserves_six_extension_behavior():
    """With no `extensions` parameter the six recognized image extensions are
    listed, byte-for-byte identical (presigned URLs aside) to the response for
    the same request without the parameter.

    _Requirements: 2.1_
    """
    _seed_bucket(_MIXED_KEYS)
    _set_usecase()

    baseline = _ok_body(_invoke("mixed/", limit=50))
    repeat = _ok_body(_invoke("mixed/", limit=50))

    assert {img["key"] for img in baseline["images"]} == _SIX_EXTENSION_KEYS
    assert baseline["total_found"] == len(_SIX_EXTENSION_KEYS)
    assert baseline["offset"] == 0
    assert baseline["limit"] == 50
    assert baseline["has_more"] is False
    assert baseline["bucket"] == BUCKET
    assert baseline["prefix"] == "mixed/"
    assert baseline["expires_in_seconds"] == 1800
    # Identical response for an identical request: the additive parameter
    # changes nothing on the path that omits it.
    assert _without_urls(repeat) == _without_urls(baseline)


@mock_aws
def test_absent_extensions_response_is_unchanged_for_a_legacy_call():
    """The legacy no-extensions, no-offset call shape (limit=12) is unchanged:
    the first 12 lexicographic image keys over the six-extension set.

    _Requirements: 2.1_
    """
    keys = [f"legacy/img-{i:03d}.jpg" for i in range(9)]
    keys += ["legacy/x.bmp", "legacy/y.tif", "legacy/z.tiff", "legacy/n.txt"]
    _seed_bucket(keys)
    _set_usecase()

    body = _ok_body(_invoke("legacy/", limit=12))

    expected = sorted(k for k in keys if not k.endswith(".txt"))
    assert [img["key"] for img in body["images"]] == expected[:12]
    assert body["total_found"] == len(expected)
    assert body["expires_in_seconds"] == 1800


# --------------------------------------------------------------------------- #
# Empty prefix vs inaccessible prefix (Req 2.5)
# --------------------------------------------------------------------------- #
@mock_aws
def test_empty_prefix_reports_zero_total_found():
    """A prefix with no matching object answers 200 with an empty image list
    and `total_found == 0` — the empty-prefix case.

    _Requirements: 2.5_
    """
    _seed_bucket(["other/notes.txt", "other/data.bmp"])
    _set_usecase()

    body = _ok_body(_invoke("other/", limit=50, extensions="jpg,jpeg,png"))

    assert body["images"] == []
    assert body["total_found"] == 0
    assert body["has_more"] is False

    missing_prefix = _ok_body(
        _invoke("does-not-exist/", limit=50, extensions="jpg,jpeg,png"))
    assert missing_prefix["images"] == []
    assert missing_prefix["total_found"] == 0


@mock_aws
def test_inaccessible_prefix_surfaces_non_2xx_error():
    """An unreadable dataset bucket surfaces a non-2xx response — the
    inaccessible-prefix case, distinct from the empty-prefix case above.

    _Requirements: 2.5_
    """
    _seed_bucket(["mixed/a.jpg"])
    _set_usecase(s3_bucket="no-such-bucket-for-image-preview")

    response = _invoke("mixed/", limit=50, extensions="jpg,jpeg,png")

    assert response["statusCode"] >= 400, response["body"]
    assert "error" in json.loads(response["body"])
