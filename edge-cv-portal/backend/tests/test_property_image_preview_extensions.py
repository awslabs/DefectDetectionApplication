"""
Property test for the Sample_Image listing with the `extensions` filter.

Spec: llm-autolabel-prompt-tuning, Task 6.2.

Feature: llm-autolabel-prompt-tuning, Property 19: Only JPEG and PNG objects
are listed, and every one is reachable — *For any* set of objects under the
dataset prefix, the Sample_Image listing SHALL contain exactly the objects
whose keys end in `.jpg`, `.jpeg` or `.png` case-insensitively, the union of
all pages SHALL equal that set with each page containing at most 100 images,
and the reported total SHALL equal the size of that set.

**Validates: Requirements 2.1, 2.2, 2.7**

Follows the approach of test_property_image_preview_pagination.py: a fake
`shared_utils` module is injected into `sys.modules` and the `functions/` dir
is put on `sys.path` BEFORE importing `datasets`; moto's mocked S3 provides
the bucket contents; synthetic API Gateway events invoke the handler directly.
"""
import json
import os
import sys
import types

import boto3
from hypothesis import given, settings, strategies as st
from moto import mock_aws

# --------------------------------------------------------------------------- #
# Import shim: inject a fake `shared_utils` and expose functions/ on the path
# BEFORE importing the module under test.
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS_DIR = os.path.abspath(os.path.join(_HERE, "..", "functions"))
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)

_USECASE_HOLDER = {"usecase": {}}


def _make_fake_shared_utils():
    """Build a stand-in for the shared Lambda layer's `shared_utils` module."""
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

import datasets  # noqa: E402  (import after shim is installed)

BUCKET = "test-image-preview-extensions-bucket"
REGION = "us-east-1"
PREFIX = "sample-images/"

# The wizard's Sample_Image filter (Req 2.1).
EXTENSIONS_PARAM = "jpg,jpeg,png"
JPEG_PNG_SUFFIXES = (".jpg", ".jpeg", ".png")

# Req 2.7: a page carries at most 100 images.
MAX_PAGE_SIZE = 100
# Safety bound for the paging loop so a broken has_more cannot loop forever.
MAX_PAGES = 40


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_bucket(keys):
    """Create the moto bucket and seed it with exactly the given object keys.

    The bucket is purged first: when an outer (session-scoped) moto mock is
    already active, nested per-example ``mock_aws()`` contexts share that
    backend and objects from earlier examples would otherwise leak in.
    """
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
    """Single-account use case (no separate data account)."""
    usecase = {
        "usecase_id": "uc-preview-ext",
        "account_id": "123456789012",
        "cross_account_role_arn": "arn:aws:iam::123456789012:root",
        "external_id": "ext-id",
        "s3_bucket": BUCKET,
    }
    usecase.update(overrides)
    _USECASE_HOLDER["usecase"] = usecase


def _invoke_preview(prefix, limit=None, offset=None, extensions=None):
    """Invoke get_image_preview with a synthetic API Gateway event."""
    params = {"usecase_id": "uc-preview-ext", "prefix": prefix}
    if limit is not None:
        params["limit"] = str(limit)
    if offset is not None:
        params["offset"] = str(offset)
    if extensions is not None:
        params["extensions"] = extensions
    response = datasets.get_image_preview({"queryStringParameters": params})
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def _fetch_all_pages(prefix, page_size, extensions=EXTENSIONS_PARAM):
    """Fetch successive pages until has_more is false."""
    pages = []
    offset = 0
    for _ in range(MAX_PAGES):
        body = _invoke_preview(prefix, limit=page_size, offset=offset,
                               extensions=extensions)
        pages.append(body)
        if not body.get("has_more", False):
            break
        offset += page_size
    return pages


def _expected_jpeg_png(keys):
    """The objects a JPEG/PNG listing must contain, case-insensitively."""
    return {k for k in keys if k.lower().endswith(JPEG_PNG_SUFFIXES)}


# Extension pool: JPEG/PNG in mixed case, other recognized image extensions
# that the filter must exclude, non-image extensions, near-miss suffixes and
# extensionless keys.
_EXT_POOL = [
    ".jpg", ".JPG", ".Jpg", ".jpeg", ".JPEG", ".png", ".PNG", ".pNg",
    ".bmp", ".tiff", ".tif", ".TIFF",
    ".txt", ".manifest", ".json", ".zip", "",
    ".jpgx", ".pngg", ".jpg.txt", ".png.bak",
]


@st.composite
def _object_layouts(draw):
    """A mixed object set under the prefix, decoys outside it, and a page size.

    Page sizes are drawn across the endpoint's per-page cap so the union of
    pages is exercised from single-image pages up to a full page.
    """
    n = draw(st.integers(min_value=0, max_value=18))
    exts = draw(st.lists(st.sampled_from(_EXT_POOL), min_size=n, max_size=n))
    keys = [f"{PREFIX}img-{i:03d}{ext}" for i, ext in enumerate(exts)]
    n_decoy = draw(st.integers(min_value=0, max_value=3))
    decoys = [f"other-images/decoy-{i:03d}.jpg" for i in range(n_decoy)]
    page_size = draw(st.integers(min_value=1, max_value=50))
    return keys, decoys, page_size


# =========================================================================== #
# Property 19
# =========================================================================== #

@settings(max_examples=100, deadline=None)
@given(layout=_object_layouts())
def test_property_only_jpeg_png_listed_and_all_reachable(layout):
    """Feature: llm-autolabel-prompt-tuning, Property 19: Only JPEG and PNG
    objects are listed, and every one is reachable — *For any* set of objects
    under the dataset prefix, the Sample_Image listing SHALL contain exactly
    the objects whose keys end in `.jpg`, `.jpeg` or `.png` case-insensitively,
    the union of all pages SHALL equal that set with each page containing at
    most 100 images, and the reported total SHALL equal the size of that set.

    **Validates: Requirements 2.1, 2.2, 2.7**
    """
    keys, decoys, page_size = layout
    expected = _expected_jpeg_png(keys)

    with mock_aws():
        _seed_bucket(keys + decoys)
        _set_usecase()

        pages = _fetch_all_pages(PREFIX, page_size)

        listed = [img["key"] for page in pages for img in page["images"]]
        union = set(listed)

        # Exactly the JPEG/PNG objects under the prefix, nothing else.
        assert union == expected, (
            f"page_size={page_size}: listing must equal the JPEG/PNG set; "
            f"unexpected={sorted(union - expected)!r} "
            f"missing={sorted(expected - union)!r}"
        )
        # Every one reachable exactly once across the pages.
        assert len(listed) == len(union), (
            f"page_size={page_size}: keys repeated across pages: {listed!r}"
        )
        for i, page in enumerate(pages):
            # Reported total is the size of the filtered set (Req 2.2).
            assert page["total_found"] == len(expected), (
                f"page {i} (offset {i * page_size}): total_found="
                f"{page['total_found']!r}, expected {len(expected)}"
            )
            # At most 100 images per page (Req 2.7).
            assert len(page["images"]) <= MAX_PAGE_SIZE, (
                f"page {i} carried {len(page['images'])} images, "
                f"which exceeds the {MAX_PAGE_SIZE}-image bound"
            )
