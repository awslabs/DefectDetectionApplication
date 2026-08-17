"""
Property 2: Preservation - Credentials, Count, and Preview Behavior Unchanged.

Preservation property tests for the dataset-discovery-none-prefix bugfix
(spec: .kiro/specs/dataset-discovery-none-prefix). Written and run against the
UNFIXED code first (observation-first): these tests encode the baseline
behavior that the upcoming fix in functions/datasets.py must NOT change.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Pinned behaviors:
- count_images (POST /datasets) and get_image_preview (GET /datasets/preview)
  operate on exactly the caller-supplied prefix, independent of the prefix
  slot returned by get_data_bucket_and_credentials (Req 3.1, 3.2)
- Single-account use cases call
  assume_usecase_role(cross_account_role_arn, external_id, 'data-access') and
  pass the returned credential markers to boto3.client unchanged (Req 3.3);
  data-account use cases without data_account_external_id raise ValueError,
  surfaced as an error response (Req 3.4)
- list_datasets response body contains exactly the keys
  datasets / bucket / base_prefix (Req 3.5)
- discover_datasets semantics: recursion up to max_depth, image-extension
  filtering (.jpg/.jpeg/.png/.bmp/.tiff/.tif), sorting by image count
  descending then prefix (Req 3.6)

Deliberately NOT asserted here: the value of base_prefix in the response and
which prefix list_datasets scans - that is the buggy behavior about to change
(covered by test_property_dataset_discovery_none_prefix.py, Property 1).

Follows the test_captures.py pattern: a fake `shared_utils` module is injected
into sys.modules and functions/ is put on sys.path BEFORE importing
`datasets`; moto's mock_aws provides the S3 backend.

Run standalone:
    python3 -m pytest tests/test_property_dataset_discovery_preservation.py -v
"""
import contextlib
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
# BEFORE importing the module under test (mirrors test_captures.py).
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS_DIR = os.path.abspath(os.path.join(_HERE, "..", "functions"))
if _FUNCTIONS_DIR not in sys.path:
    sys.path.insert(0, _FUNCTIONS_DIR)


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
            "body": json.dumps(
                {"error": str(message_or_headers), "detail": str(error)}
            ),
        }

    def get_usecase(usecase_id):
        return {
            "usecase_id": usecase_id,
            "account_id": "123456789012",
            "cross_account_role_arn": "arn:aws:iam::123456789012:role/dda",
            "external_id": "ext-id",
            "s3_bucket": "test-data-bucket",
        }

    def assume_usecase_role(role_arn, external_id, session_name):
        return {
            "AccessKeyId": "testing",
            "SecretAccessKey": "testing",
            "SessionToken": "testing",
        }

    mod.create_response = create_response
    mod.handle_error = handle_error
    mod.get_usecase = get_usecase
    mod.assume_usecase_role = assume_usecase_role
    return mod


sys.modules.setdefault("shared_utils", _make_fake_shared_utils())

import datasets  # noqa: E402  (import after shim is installed)

# --------------------------------------------------------------------------- #
# Constants and deterministic stubs (per-test, patched onto the datasets
# module so this file never depends on which fake shared_utils won the
# sys.modules race in a combined run).
# --------------------------------------------------------------------------- #
REGION = "us-east-1"
BUCKET = "test-data-bucket"
DATA_BUCKET = "test-data-account-bucket"

CROSS_ACCOUNT_ROLE_ARN = "arn:aws:iam::123456789012:role/dda-data"
EXTERNAL_ID = "ext-123"
DATA_ROLE_ARN = "arn:aws:iam::210987654321:role/dda-data-account"
DATA_EXTERNAL_ID = "data-ext-456"

# Markers so tests can assert credentials flow to boto3.client unchanged.
# "testing" values are accepted by moto's mocked endpoints.
FAKE_CREDS = {
    "AccessKeyId": "testing",
    "SecretAccessKey": "testing",
    "SessionToken": "testing",
}

IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"]


def _create_response(status_code, body, headers=None):
    return {
        "statusCode": status_code,
        "headers": headers or {},
        "body": body if isinstance(body, str) else json.dumps(body),
    }


def _handle_error(error, message="Operation failed"):
    return {
        "statusCode": 500,
        "headers": {},
        "body": json.dumps({"error": str(message), "detail": str(error)}),
    }


@contextlib.contextmanager
def _patched_datasets(usecase, assume_calls=None, client_calls=None):
    """Patch the shared_utils bindings inside the datasets module.

    Configurable get_usecase, a recording assume_usecase_role that returns
    FAKE_CREDS markers, deterministic create_response/handle_error, and an
    optional boto3.client recorder (kwargs captured, then delegated to the
    real client so moto still intercepts the API calls).
    """
    names = ("get_usecase", "assume_usecase_role", "create_response",
             "handle_error")
    originals = {name: getattr(datasets, name) for name in names}
    real_client = boto3.client

    def fake_get_usecase(usecase_id):
        return dict(usecase)

    def fake_assume_usecase_role(role_arn, external_id, session_name):
        if assume_calls is not None:
            assume_calls.append((role_arn, external_id, session_name))
        return dict(FAKE_CREDS)

    datasets.get_usecase = fake_get_usecase
    datasets.assume_usecase_role = fake_assume_usecase_role
    datasets.create_response = _create_response
    datasets.handle_error = _handle_error

    if client_calls is not None:
        def recording_client(service, **kwargs):
            client_calls.append((service, kwargs))
            return real_client(service, **kwargs)
        boto3.client = recording_client

    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(datasets, name, original)
        boto3.client = real_client


def _seed_bucket(s3, bucket, keys):
    s3.create_bucket(Bucket=bucket)
    for key in keys:
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")


def _make_usecase(prefix_slot="absent"):
    """Single-account use case with a configurable s3_prefix slot.

    The prefix slot mirrors real DynamoDB items: absent, null, empty string,
    or set. count_images / get_image_preview must behave identically for all
    of them (they discard the prefix slot of get_data_bucket_and_credentials).
    """
    usecase = {
        "usecase_id": "uc-1",
        "account_id": "123456789012",
        "cross_account_role_arn": CROSS_ACCOUNT_ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "s3_bucket": BUCKET,
    }
    if prefix_slot == "null":
        usecase["s3_prefix"] = None
    elif prefix_slot == "empty":
        usecase["s3_prefix"] = ""
    elif prefix_slot == "set":
        usecase["s3_prefix"] = "configured-prefix/"
    return usecase


# --------------------------------------------------------------------------- #
# Hypothesis strategies
# --------------------------------------------------------------------------- #
_SEGMENT = st.text(alphabet="abcdefgh", min_size=1, max_size=8)
# Caller-supplied prefixes like "abc/" or "ab/cd/" - never starting with 'z',
# so the "zz-decoy/" keys can never fall under them.
_CALLER_PREFIX = st.lists(_SEGMENT, min_size=1, max_size=3).map(
    lambda segs: "/".join(segs) + "/"
)
_PREFIX_SLOT = st.sampled_from(["absent", "null", "empty", "set"])


# =========================================================================== #
# Property: count_images counts exactly the caller-supplied prefix,
# independent of the use case's configured prefix slot.
# **Validates: Requirements 3.1**
# =========================================================================== #
@given(
    prefix=_CALLER_PREFIX,
    n_images=st.integers(min_value=0, max_value=6),
    n_decoy=st.integers(min_value=0, max_value=3),
    n_nonimage=st.integers(min_value=0, max_value=2),
    prefix_slot=_PREFIX_SLOT,
)
@settings(max_examples=15, deadline=None)
def test_count_images_counts_exactly_the_supplied_prefix(
    prefix, n_images, n_decoy, n_nonimage, prefix_slot
):
    """count_images counts images under exactly the caller-supplied prefix.

    Decoy images outside the prefix and non-image files under it are never
    counted; the result does not depend on the use case's s3_prefix slot.
    **Validates: Requirements 3.1**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        keys = [
            f"{prefix}img{i}{IMAGE_EXTS[i % len(IMAGE_EXTS)]}"
            for i in range(n_images)
        ]
        keys += [f"zz-decoy/img{i}.jpg" for i in range(n_decoy)]
        keys += [f"{prefix}notes{i}.txt" for i in range(n_nonimage)]
        _seed_bucket(s3, BUCKET, keys)

        with _patched_datasets(_make_usecase(prefix_slot)):
            event = {"body": json.dumps({"usecase_id": "uc-1",
                                         "prefix": prefix})}
            response = datasets.count_images(event)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["image_count"] == n_images
    assert body["prefix"] == prefix
    assert body["bucket"] == BUCKET
    assert len(body["sample_images"]) == min(n_images, 5)
    assert all(s["key"].startswith(prefix) for s in body["sample_images"])


# =========================================================================== #
# Property: get_image_preview previews exactly the caller-supplied prefix,
# independent of the use case's configured prefix slot.
# **Validates: Requirements 3.2**
# =========================================================================== #
@given(
    prefix=_CALLER_PREFIX,
    n_images=st.integers(min_value=0, max_value=12),
    n_decoy=st.integers(min_value=0, max_value=3),
    prefix_slot=_PREFIX_SLOT,
)
@settings(max_examples=15, deadline=None)
def test_get_image_preview_previews_exactly_the_supplied_prefix(
    prefix, n_images, n_decoy, prefix_slot
):
    """get_image_preview returns presigned URLs only for images under the
    caller-supplied prefix, capped at the default limit of 8, independent of
    the use case's s3_prefix slot.
    **Validates: Requirements 3.2**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        keys = [
            f"{prefix}img{i}{IMAGE_EXTS[i % len(IMAGE_EXTS)]}"
            for i in range(n_images)
        ]
        keys += [f"zz-decoy/img{i}.jpg" for i in range(n_decoy)]
        keys += [f"{prefix}readme.txt"]
        _seed_bucket(s3, BUCKET, keys)

        with _patched_datasets(_make_usecase(prefix_slot)):
            event = {"queryStringParameters": {"usecase_id": "uc-1",
                                               "prefix": prefix}}
            response = datasets.get_image_preview(event)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["prefix"] == prefix
    assert body["bucket"] == BUCKET
    assert body["total_found"] == min(n_images, 8)  # default limit is 8
    assert len(body["images"]) == body["total_found"]
    assert all(img["key"].startswith(prefix) for img in body["images"])
    assert all(img["presigned_url"] for img in body["images"])
    assert body["expires_in_seconds"] == 1800


# =========================================================================== #
# Credential paths
# **Validates: Requirements 3.3, 3.4**
# =========================================================================== #
def test_single_account_credential_path_unchanged():
    """Single-account use cases call
    assume_usecase_role(cross_account_role_arn, external_id, 'data-access')
    and pass the returned credential markers to boto3.client unchanged.
    **Validates: Requirements 3.3**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        _seed_bucket(s3, BUCKET, ["training-images/anomaly-1.jpg"])

        assume_calls = []
        client_calls = []
        with _patched_datasets(_make_usecase("absent"),
                               assume_calls=assume_calls,
                               client_calls=client_calls):
            event = {"body": json.dumps({"usecase_id": "uc-1",
                                         "prefix": "training-images/"})}
            response = datasets.count_images(event)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["image_count"] == 1

    # Exactly one role assumption, with the single-account arguments.
    assert assume_calls == [
        (CROSS_ACCOUNT_ROLE_ARN, EXTERNAL_ID, "data-access")
    ]

    # The returned credential markers flow into boto3.client unchanged.
    s3_client_kwargs = [kwargs for service, kwargs in client_calls
                        if service == "s3" and kwargs]
    assert s3_client_kwargs == [{
        "aws_access_key_id": FAKE_CREDS["AccessKeyId"],
        "aws_secret_access_key": FAKE_CREDS["SecretAccessKey"],
        "aws_session_token": FAKE_CREDS["SessionToken"],
    }]


def test_data_account_credential_path_unchanged():
    """Data-account use cases assume the data account role with the
    data_account_external_id and read from the data account bucket.
    **Validates: Requirements 3.4**
    """
    usecase = {
        "usecase_id": "uc-1",
        "account_id": "123456789012",
        "cross_account_role_arn": CROSS_ACCOUNT_ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "s3_bucket": BUCKET,
        "data_account_role_arn": DATA_ROLE_ARN,
        "data_account_external_id": DATA_EXTERNAL_ID,
        "data_s3_bucket": DATA_BUCKET,
    }
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        _seed_bucket(s3, DATA_BUCKET, ["team-a/img-1.jpg", "team-a/img-2.png"])

        assume_calls = []
        with _patched_datasets(usecase, assume_calls=assume_calls):
            event = {"body": json.dumps({"usecase_id": "uc-1",
                                         "prefix": "team-a/"})}
            response = datasets.count_images(event)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["bucket"] == DATA_BUCKET
    assert body["image_count"] == 2
    assert assume_calls == [(DATA_ROLE_ARN, DATA_EXTERNAL_ID, "data-access")]


def test_data_account_without_external_id_raises_value_error():
    """A data-account use case without data_account_external_id raises
    ValueError, which surfaces as an error response from the endpoints.
    **Validates: Requirements 3.4**
    """
    usecase = {
        "usecase_id": "uc-1",
        "account_id": "123456789012",
        "cross_account_role_arn": CROSS_ACCOUNT_ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "s3_bucket": BUCKET,
        "data_account_role_arn": DATA_ROLE_ARN,
        # data_account_external_id deliberately missing
        "data_s3_bucket": DATA_BUCKET,
    }
    assume_calls = []
    with _patched_datasets(usecase, assume_calls=assume_calls):
        # Direct call: the helper itself raises ValueError.
        with pytest.raises(ValueError, match="data_account_external_id"):
            datasets.get_data_bucket_and_credentials(usecase)

        # Endpoint call: the ValueError surfaces as an error response.
        event = {"body": json.dumps({"usecase_id": "uc-1",
                                     "prefix": "team-a/"})}
        response = datasets.count_images(event)

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert "data_account_external_id" in body["detail"]
    # No role was assumed before the error was raised.
    assert assume_calls == []


# =========================================================================== #
# Property: list_datasets response body shape.
# **Validates: Requirements 3.5**
# =========================================================================== #
@pytest.mark.parametrize("prefix_slot", ["absent", "null", "empty", "set"])
@pytest.mark.parametrize("filter_prefix", ["", "training"])
def test_list_datasets_response_shape(prefix_slot, filter_prefix):
    """The list_datasets response body contains exactly the keys
    datasets / bucket / base_prefix, for every prefix-slot configuration and
    filter prefix. (The VALUES of base_prefix and datasets are deliberately
    not asserted - they are about to change with the fix.)
    **Validates: Requirements 3.5**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        _seed_bucket(s3, BUCKET, [
            "training-images/anomaly-1.jpg",
            "training-images/anomaly-2.jpg",
        ])

        with _patched_datasets(_make_usecase(prefix_slot)):
            event = {"queryStringParameters": {"usecase_id": "uc-1",
                                               "prefix": filter_prefix}}
            response = datasets.list_datasets(event)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert set(body.keys()) == {"datasets", "bucket", "base_prefix"}
    assert isinstance(body["datasets"], list)
    assert body["bucket"] == BUCKET


# =========================================================================== #
# discover_datasets semantics (exercised directly against moto S3; the fix
# does not touch this function).
# **Validates: Requirements 3.6**
# =========================================================================== #
@given(
    folders=st.dictionaries(
        keys=st.text(alphabet="abcdefgh", min_size=1, max_size=6),
        values=st.tuples(
            st.integers(min_value=0, max_value=4),   # image count
            st.integers(min_value=0, max_value=2),   # non-image count
        ),
        min_size=1,
        max_size=5,
    ),
)
@settings(max_examples=15, deadline=None)
def test_discover_datasets_filtering_and_sorting(folders):
    """discover_datasets returns exactly the image-bearing prefixes, with
    correct image counts (non-image files ignored), sorted by image count
    descending then prefix ascending.
    **Validates: Requirements 3.6**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        keys = []
        for folder, (n_images, n_nonimages) in folders.items():
            keys += [
                f"{folder}/img{i}{IMAGE_EXTS[i % len(IMAGE_EXTS)]}"
                for i in range(n_images)
            ]
            keys += [f"{folder}/doc{i}.txt" for i in range(n_nonimages)]
        _seed_bucket(s3, BUCKET, keys)

        result = datasets.discover_datasets(s3, BUCKET, "", 3)

    expected = sorted(
        ((f"{folder}/", counts[0])
         for folder, counts in folders.items() if counts[0] > 0),
        key=lambda item: (-item[1], item[0]),
    )
    assert [(d["prefix"], d["image_count"]) for d in result] == expected
    assert all(d["has_subdirectories"] is False for d in result)
    assert all(d["last_modified"] is not None for d in result)


def test_discover_datasets_counts_all_image_extensions():
    """All six image extensions are counted; other extensions are not.
    **Validates: Requirements 3.6**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        keys = [f"folder/image-{i}{ext}" for i, ext in enumerate(IMAGE_EXTS)]
        keys += ["folder/manifest.json", "folder/readme.txt",
                 "folder/notes.md"]
        _seed_bucket(s3, BUCKET, keys)

        result = datasets.discover_datasets(s3, BUCKET, "", 3)

    assert [(d["prefix"], d["image_count"]) for d in result] == [
        ("folder/", len(IMAGE_EXTS))
    ]


@pytest.mark.parametrize("max_depth,expected_prefixes", [
    (0, [""]),
    (1, ["", "l1/"]),
    (2, ["", "l1/", "l1/l2/"]),
    (3, ["", "l1/", "l1/l2/", "l1/l2/l3/"]),
])
def test_discover_datasets_recursion_respects_max_depth(max_depth,
                                                        expected_prefixes):
    """Recursion visits prefixes down to max_depth levels and no deeper.
    **Validates: Requirements 3.6**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        _seed_bucket(s3, BUCKET, [
            "root.jpg",
            "l1/i.jpg",
            "l1/l2/i.jpg",
            "l1/l2/l3/i.jpg",
            "l1/l2/l3/l4/i.jpg",
        ])

        result = datasets.discover_datasets(s3, BUCKET, "", max_depth)

    # Every level holds exactly one image, so the count-descending sort
    # falls through to the prefix sort.
    assert [d["prefix"] for d in result] == expected_prefixes
    assert all(d["image_count"] == 1 for d in result)


def test_discover_datasets_scans_a_configured_prefix_subtree():
    """Scanning a configured prefix (e.g. a derived base_prefix like
    'team-a/' after the fix) finds only the image-bearing prefixes under it.
    Recorded via a direct discover_datasets call, which the fix does not
    touch.
    **Validates: Requirements 3.6**
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        _seed_bucket(s3, BUCKET, [
            "team-a/training-images/anomaly-1.jpg",
            "team-a/training-images/anomaly-2.jpg",
            "team-b/other-images/x.jpg",
            "unrelated/y.png",
        ])

        result = datasets.discover_datasets(s3, BUCKET, "team-a/", 3)

    assert [(d["prefix"], d["image_count"]) for d in result] == [
        ("team-a/training-images/", 2)
    ]
