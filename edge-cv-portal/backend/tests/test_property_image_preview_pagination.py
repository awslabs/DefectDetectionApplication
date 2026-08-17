"""
Bug condition exploration + preservation tests for the source-image picker
preview pagination bug.

Spec: source-image-picker-pagination, Tasks 1 and 2.

**Property 1: Bug Condition - Full Reachability via Pagination**

_For any_ dataset prefix where the bug condition holds (isBugCondition:
`imageCount(prefix) > 20`), fetching successive pages (offset = 0, 12, 24, ...
until `has_more` is false) SHALL yield a union of image keys exactly equal to
the set of all image-extension keys under the prefix, and each response SHALL
report `total_found` equal to the true total image count.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

EXPECTED TO FAIL ON UNFIXED CODE: `get_image_preview` clamps
`limit = min(int(params.get('limit', '8')), 20)`, breaks out of key
collection at the clamp, ignores any `offset` parameter, and returns no
`has_more`/`offset`/`limit` metadata — so images beyond the first 20
lexicographic keys are unreachable and `total_found` is the truncated
returned count. The failures here are the counterexamples that confirm the
bug exists.

**Property 2: Preservation - Legacy Callers and Small Prefixes Unchanged**

_For any_ input where the bug condition does NOT hold (prefixes with no more
images than one page, and legacy no-offset call shapes such as
ImagePreview.tsx's `limit: 12`), the system SHALL keep returning a valid
preview response: `images` (first `limit` lexicographic image keys),
`total_found`, `expires_in_seconds: 1800`, identical extension filtering,
and identical `count_images` / `list_datasets` behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6**

EXPECTED TO PASS ON UNFIXED CODE (observation-first baseline). NOTE: these
tests deliberately do NOT pin the unfixed `total_found` semantics for
prefixes larger than one page — the fix changes `total_found` from
"returned count" to "true total under the prefix" by design, so the legacy
shape test only requires `total_found` to be an int >= len(images).

Follows the established pattern (see
test_property_dataset_discovery_none_prefix.py): a fake `shared_utils`
module is injected into `sys.modules` and the `functions/` dir is put on
`sys.path` BEFORE importing `datasets`; moto's mocked S3 provides the
bucket contents; synthetic API Gateway events invoke the handlers directly.
"""
import json
import os
import sys
import types

import boto3
import pytest
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

# Mutable holder so each test can configure the use case dict returned by the
# stubbed get_usecase.
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
            "body": json.dumps({"error": str(message_or_headers), "detail": str(error)}),
        }

    def get_usecase(usecase_id):
        usecase = dict(_USECASE_HOLDER["usecase"])
        usecase.setdefault("usecase_id", usecase_id)
        return usecase

    def assume_usecase_role(role_arn, external_id, session_name):
        # Fake static credentials - moto accepts any credentials.
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


# Force our fake so `datasets` binds to it even if a real shared_utils was
# imported earlier in the session; re-import datasets against the fake.
sys.modules["shared_utils"] = _make_fake_shared_utils()
sys.modules.pop("datasets", None)

import datasets  # noqa: E402  (import after shim is installed)

BUCKET = "test-image-preview-pagination-bucket"
REGION = "us-east-1"

# The wizard's fixed page size (SOURCE_PICKER_PAGE_SIZE from design).
PAGE_SIZE = 12
# Safety bound for the paging loop: enough for 120 images at 12/page,
# with headroom, so a broken has_more can never loop forever.
MAX_PAGES = 32


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_bucket(keys):
    """Create the moto bucket and seed it with the given object keys."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for key in keys:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"fake-image-bytes")
    return s3


def _set_usecase(**overrides):
    """Single-account use case (no separate data account)."""
    usecase = {
        "usecase_id": "uc-picker",
        "account_id": "123456789012",
        "cross_account_role_arn": "arn:aws:iam::123456789012:root",
        "external_id": "ext-id",
        "s3_bucket": BUCKET,
    }
    usecase.update(overrides)
    _USECASE_HOLDER["usecase"] = usecase


def _invoke_preview(prefix, limit=None, offset=None):
    """Invoke get_image_preview with a synthetic API Gateway event."""
    params = {"usecase_id": "uc-picker", "prefix": prefix}
    if limit is not None:
        params["limit"] = str(limit)
    if offset is not None:
        params["offset"] = str(offset)
    event = {"queryStringParameters": params}
    response = datasets.get_image_preview(event)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def _fetch_all_pages(prefix, page_size=PAGE_SIZE):
    """Fetch successive pages (offset 0, page_size, 2*page_size, ...) until
    has_more is false. On unfixed code has_more is absent, so exactly one
    page is fetched and the truncated union becomes the counterexample."""
    pages = []
    offset = 0
    for _ in range(MAX_PAGES):
        body = _invoke_preview(prefix, limit=page_size, offset=offset)
        pages.append(body)
        if not body.get("has_more", False):
            break
        offset += page_size
    return pages


def _union_keys(pages):
    return {img["key"] for page in pages for img in page["images"]}


def _sixty_three_image_keys():
    """The reported counterexample: 32 anomaly-*.jpg sorting lexicographically
    before 31 normal-*.jpg under training-images/."""
    anomaly = [f"training-images/anomaly-{i:03d}.jpg" for i in range(32)]
    normal = [f"training-images/normal-{i:03d}.jpg" for i in range(31)]
    return anomaly + normal


# =========================================================================== #
# Task 1 — Property 1: Bug Condition exploration tests
# EXPECTED TO FAIL ON UNFIXED CODE (failure confirms the bug exists).
# =========================================================================== #

@mock_aws
def test_bug_counterexample_63_images_all_reachable_via_pages():
    """Test case 1 - Counterexample Reproduction: 63 images (32 anomaly + 31
    normal) under training-images/; fetching pages (offset 0, 12, 24, ...
    until has_more is false) yields a union equal to all 63 keys, including
    every normal-*.jpg.

    **Validates: Requirements 2.1, 2.2**
    """
    all_keys = _sixty_three_image_keys()
    _seed_bucket(all_keys)
    _set_usecase()

    pages = _fetch_all_pages("training-images/")
    union = _union_keys(pages)

    # On unfixed code: a single page of at most 20 anomaly keys, no paging
    # metadata, so the union misses every normal-*.jpg.
    assert union == set(all_keys), (
        f"Union of all pages must equal all 63 image keys; got {len(union)} "
        f"keys across {len(pages)} page(s). Missing: "
        f"{sorted(set(all_keys) - union)[:5]}... "
        f"(first page keys: {sorted(union)[:5]})"
    )
    normal_keys = {k for k in all_keys if "normal-" in k}
    assert normal_keys <= union, (
        f"Every normal-*.jpg must be reachable by paging; "
        f"{len(normal_keys - union)} of {len(normal_keys)} are unreachable"
    )


@mock_aws
def test_bug_true_total_reported_on_every_page():
    """Test case 2 - True Total: every page response reports
    total_found == 63 (unfixed code returns the truncated returned count).

    **Validates: Requirements 2.1, 2.4**
    """
    all_keys = _sixty_three_image_keys()
    _seed_bucket(all_keys)
    _set_usecase()

    pages = _fetch_all_pages("training-images/")

    for i, page in enumerate(pages):
        assert page["total_found"] == 63, (
            f"Page {i} (offset {i * PAGE_SIZE}) must report the true total "
            f"63, got total_found={page['total_found']!r}"
        )


@st.composite
def _image_key_sets(draw):
    """Generated image counts N in [21, 120] with mixed key names (two stems
    so lexicographic ordering interleaves classes like the real dataset)."""
    n = draw(st.integers(min_value=21, max_value=120))
    n_anomaly = draw(st.integers(min_value=0, max_value=n))
    keys = [f"sweep-images/anomaly-{i:04d}.jpg" for i in range(n_anomaly)]
    keys += [f"sweep-images/normal-{i:04d}.png" for i in range(n - n_anomaly)]
    return keys


@settings(max_examples=10, deadline=None)
@given(keys=_image_key_sets())
def test_bug_property_sweep_union_of_pages_and_true_total(keys):
    """Test case 3 - Property Sweep (hypothesis): for generated image counts
    N in [21, 120], union-of-pages == all image keys and total_found == N on
    every page.

    **Validates: Requirements 2.1, 2.2**
    """
    with mock_aws():
        _seed_bucket(keys)
        _set_usecase()

        pages = _fetch_all_pages("sweep-images/")
        union = _union_keys(pages)

        assert union == set(keys), (
            f"N={len(keys)}: union of pages has {len(union)} keys, expected "
            f"all {len(keys)} (missing {len(set(keys) - union)})"
        )
        for page in pages:
            assert page["total_found"] == len(keys), (
                f"N={len(keys)}: total_found={page['total_found']!r}, "
                f"expected the true total {len(keys)}"
            )


@mock_aws
def test_bug_cap_probe_limit_50_on_25_image_prefix():
    """Test case 4 - Cap Probe: request limit=50 on a 25-image prefix; more
    than 20 keys must be reachable in one page (unfixed code clamps to 20).

    **Validates: Requirements 2.1, 2.2**
    """
    keys = [f"cap-probe/img-{i:03d}.jpg" for i in range(25)]
    _seed_bucket(keys)
    _set_usecase()

    body = _invoke_preview("cap-probe/", limit=50)

    returned = {img["key"] for img in body["images"]}
    assert len(returned) > 20, (
        f"limit=50 on a 25-image prefix must reach more than 20 keys; "
        f"got only {len(returned)} (cap clamp counterexample)"
    )


# =========================================================================== #
# Task 2 — Property 2: Preservation tests
# EXPECTED TO PASS ON UNFIXED CODE (observation-first baseline).
# =========================================================================== #

@mock_aws
def test_preservation_legacy_limit12_no_offset_shape():
    """Legacy caller shape (ImagePreview.tsx via DatasetBrowser): limit=12,
    no offset — response contains the first 12 lexicographic image keys,
    a total_found int, and expires_in_seconds: 1800.

    Note: total_found's exact value for >12-image prefixes is NOT pinned
    (the fix changes it from truncated count to true total by design); it
    only needs to be an int >= len(images).

    **Validates: Requirements 3.1, 3.2**
    """
    keys = [f"legacy-shape/img-{i:03d}.jpg" for i in range(15)]
    _seed_bucket(keys)
    _set_usecase()

    body = _invoke_preview("legacy-shape/", limit=12)

    returned = [img["key"] for img in body["images"]]
    assert returned == sorted(keys)[:12], (
        f"limit=12 no-offset must return the first 12 lexicographic keys; "
        f"got {returned!r}"
    )
    assert isinstance(body["total_found"], int)
    assert body["total_found"] >= len(body["images"])
    assert body["expires_in_seconds"] == 1800
    for img in body["images"]:
        assert img["presigned_url"], f"missing presigned_url for {img['key']}"
        assert img["filename"] == os.path.basename(img["key"])


@settings(max_examples=12, deadline=None)
@given(n=st.integers(min_value=0, max_value=20))
def test_preservation_small_prefix_all_keys_returned(n):
    """Small prefix property (hypothesis): for N in [0, 20] images with
    limit=20, all N keys are returned on the first page and total_found == N
    (isBugCondition false — behavior identical before and after the fix).

    **Validates: Requirements 3.6**
    """
    keys = [f"small-prefix/img-{i:03d}.jpg" for i in range(n)]
    with mock_aws():
        _seed_bucket(keys)
        _set_usecase()

        body = _invoke_preview("small-prefix/", limit=20)

        returned = {img["key"] for img in body["images"]}
        assert returned == set(keys), (
            f"N={n}: expected all {n} keys returned, got {len(returned)}"
        )
        assert body["total_found"] == n
        assert body["expires_in_seconds"] == 1800


@mock_aws
def test_preservation_extension_filtering():
    """Extension filtering: mixed keys (images plus .txt, .manifest,
    extensionless) — only the six recognized image extensions appear.

    **Validates: Requirements 3.4**
    """
    image_keys = [
        "mixed/a.jpg", "mixed/b.jpeg", "mixed/c.png",
        "mixed/d.bmp", "mixed/e.tiff", "mixed/f.tif",
    ]
    non_image_keys = [
        "mixed/notes.txt", "mixed/dataset.manifest", "mixed/README",
        "mixed/archive.zip",
    ]
    _seed_bucket(image_keys + non_image_keys)
    _set_usecase()

    body = _invoke_preview("mixed/", limit=20)

    returned = {img["key"] for img in body["images"]}
    assert returned == set(image_keys), (
        f"Only image-extension keys must appear; got {sorted(returned)!r}"
    )
    assert body["total_found"] == len(image_keys)


@mock_aws
def test_preservation_count_images_direct_call():
    """Untouched operation: count_images counts exactly the image keys under
    the supplied prefix and returns at most 5 sample images.

    **Validates: Requirements 3.3**
    """
    keys = [f"count-me/img-{i:03d}.jpg" for i in range(9)]
    decoys = ["count-me/notes.txt", "elsewhere/img-000.jpg"]
    _seed_bucket(keys + decoys)
    _set_usecase()

    event = {"body": json.dumps({"usecase_id": "uc-picker",
                                 "prefix": "count-me/"})}
    response = datasets.count_images(event)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    assert body["prefix"] == "count-me/"
    assert body["image_count"] == 9
    assert len(body["sample_images"]) == 5
    sample_keys = {s["key"] for s in body["sample_images"]}
    assert sample_keys <= set(keys)


@mock_aws
def test_preservation_list_datasets_direct_call():
    """Untouched operation: list_datasets discovers the image-bearing prefix
    consistent with the seeded key set.

    **Validates: Requirements 3.3**
    """
    keys = [f"training-images/img-{i:03d}.jpg" for i in range(3)]
    _seed_bucket(keys)
    _set_usecase()

    event = {"queryStringParameters": {"usecase_id": "uc-picker"}}
    response = datasets.list_datasets(event)
    assert response["statusCode"] == 200, response["body"]
    body = json.loads(response["body"])

    discovered = {d["prefix"]: d for d in body["datasets"]}
    assert "training-images/" in discovered, (
        f"Expected 'training-images/' among discovered prefixes, "
        f"got {sorted(discovered)!r}"
    )
    assert discovered["training-images/"]["image_count"] == 3
    assert body["bucket"] == BUCKET
