"""
Bug condition exploration test for the dataset discovery "None/" prefix bug.

Spec: dataset-discovery-none-prefix, Task 1.

**Property 1: Bug Condition - Dataset Discovery Scans the Configured Prefix**

_For any_ use case configuration (with `s3_prefix`/`data_s3_prefix` absent,
`None`, empty, or set) and any filter prefix, when the bug condition holds
(base prefix would be `None`), the code SHALL derive a string base prefix
(defaulting to ''), compose the scanned S3 prefix as the normalized
`"{configured_prefix}{filter_prefix}"` with no "None" literal, and discovery
SHALL return the image-bearing prefixes actually present in the bucket.
The response `base_prefix` SHALL be '' (not null) when unset.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5**

EXPECTED TO FAIL ON UNFIXED CODE: `get_data_bucket_and_credentials` hardcodes
`None` in the prefix slot, so `list_datasets` composes the literal S3 prefix
"None/" (or "None{filter}/"), scans a nonexistent location, and returns
`datasets: []` with `base_prefix: null`. The failures here are the
counterexamples that confirm the bug exists.

The bug is deterministic (every request hits it), so the property is scoped
to concrete configurations covering prefix absent / None / '' and both
branches of `get_data_bucket_and_credentials`, per the design's Scoped PBT
Approach.

Follows the `test_captures.py` pattern: a fake `shared_utils` module is
injected into `sys.modules` and the `functions/` dir is put on `sys.path`
BEFORE importing `datasets`; moto's mocked S3 provides the bucket contents.
"""
import json
import os
import sys
import types

import boto3
import pytest
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
        # Fake static credentials - moto accepts any credentials, so this
        # covers both the single-account and data-account branches.
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

BUCKET = "test-dataset-discovery-bucket"
REGION = "us-east-1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_bucket(keys):
    """Create the moto bucket and seed it with the given image keys."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for key in keys:
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"fake-image-bytes")
    return s3


def _single_account_usecase(**overrides):
    """Cookies-style use case: no separate data account."""
    usecase = {
        "usecase_id": "uc-cookies",
        "account_id": "123456789012",
        "cross_account_role_arn": "arn:aws:iam::123456789012:root",
        "external_id": "ext-id",
        "s3_bucket": BUCKET,
        # no s3_prefix by default (overridable per test case)
    }
    usecase.update(overrides)
    return usecase


def _set_usecase(usecase):
    _USECASE_HOLDER["usecase"] = usecase


def _invoke_list_datasets(filter_prefix=None):
    params = {"usecase_id": _USECASE_HOLDER["usecase"]["usecase_id"]}
    if filter_prefix is not None:
        params["prefix"] = filter_prefix
    event = {"queryStringParameters": params}
    response = datasets.list_datasets(event)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


# --------------------------------------------------------------------------- #
# Test case 1: unset prefix discovery (Req 2.1, 2.4)
#
# Bug condition (isBugCondition from design): the base prefix slot of
# get_data_bucket_and_credentials IS None. On unfixed code the scan targets
# the literal "None/" and returns [].
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "prefix_config",
    [
        pytest.param({}, id="s3_prefix-absent"),
        pytest.param({"s3_prefix": None}, id="s3_prefix-None"),
        pytest.param({"s3_prefix": ""}, id="s3_prefix-empty"),
    ],
)
@mock_aws
def test_unset_prefix_discovers_image_bearing_prefixes(prefix_config):
    """Use case with no configured prefix, no filter: discovery scans from the
    bucket root ('') and finds training-images/.

    **Validates: Requirements 2.1, 2.4** (counterexample for 1.1, 1.3)
    """
    _seed_bucket([
        "training-images/anomaly-1.jpg",
        "training-images/anomaly-2.jpg",
    ])
    _set_usecase(_single_account_usecase(**prefix_config))

    body = _invoke_list_datasets()

    # On unfixed code: datasets == [] because the scan targeted "None/".
    assert body["datasets"], (
        "Expected non-empty datasets (bucket contains "
        "training-images/anomaly-*.jpg) but got: "
        f"datasets={body['datasets']!r}, base_prefix={body['base_prefix']!r}"
    )
    discovered = {d["prefix"] for d in body["datasets"]}
    assert "training-images/" in discovered, (
        f"Expected 'training-images/' among discovered prefixes, got {discovered!r}"
    )


# --------------------------------------------------------------------------- #
# Test case 2: filter prefix composition (Req 1.2, 2.3)
# --------------------------------------------------------------------------- #
@mock_aws
def test_filter_prefix_composes_without_none_literal(monkeypatch):
    """Filter prefix 'training': the scanned S3 prefix is the normalized
    '{configured_prefix}{filter_prefix}' ('training/') with no "None" literal,
    and discovery finds the images seeded under training/ while excluding keys
    outside it.

    **Validates: Requirements 1.2, 2.3**
    """
    _seed_bucket([
        "training/anomaly-1.jpg",
        "training/anomaly-2.jpg",
        # Decoy outside the filter prefix: must NOT be discovered.
        "other/keep-out.jpg",
    ])
    _set_usecase(_single_account_usecase())

    # Spy on discover_datasets to capture the scanned prefix.
    scanned_prefixes = []
    real_discover = datasets.discover_datasets

    def spy_discover(s3_client, bucket, prefix, max_depth):
        scanned_prefixes.append(prefix)
        return real_discover(s3_client, bucket, prefix, max_depth)

    monkeypatch.setattr(datasets, "discover_datasets", spy_discover)

    body = _invoke_list_datasets(filter_prefix="training")

    assert scanned_prefixes, "discover_datasets was never invoked"
    scanned = scanned_prefixes[0]
    # On unfixed code: scanned == "Nonetraining/" (the "None" literal).
    assert "None" not in scanned, (
        f"Scanned S3 prefix contains the 'None' literal: {scanned!r}"
    )
    assert scanned == "training/", (
        f"Expected normalized scan prefix 'training/', got {scanned!r}"
    )
    # Discovery works: the scan under 'training/' finds the seeded images and
    # excludes the decoy outside the filter prefix. On unfixed code the scan
    # targets 'Nonetraining/' and discovery is empty.
    discovered = {d["prefix"] for d in body["datasets"]}
    assert "training/" in discovered, (
        f"Expected 'training/' among discovered prefixes, got {discovered!r}"
    )
    assert "other/" not in discovered, (
        f"Decoy prefix 'other/' must not be discovered, got {discovered!r}"
    )


# --------------------------------------------------------------------------- #
# Test case 3: response base_prefix field (Req 1.4, 2.5)
# --------------------------------------------------------------------------- #
@mock_aws
def test_response_base_prefix_is_empty_string_not_null():
    """When no base prefix is configured, the response `base_prefix` is ''
    (empty string), not null.

    **Validates: Requirements 1.4, 2.5**
    """
    _seed_bucket(["training-images/anomaly-1.jpg"])
    _set_usecase(_single_account_usecase())

    body = _invoke_list_datasets()

    # On unfixed code: base_prefix is None (serialized as null).
    assert body["base_prefix"] == "", (
        f"Expected base_prefix '' (empty string), got {body['base_prefix']!r}"
    )


# --------------------------------------------------------------------------- #
# Test case 4: data-account configured prefix (Req 2.2)
# --------------------------------------------------------------------------- #
@mock_aws
def test_data_account_prefix_scans_configured_data_s3_prefix():
    """Data-account use case with data_s3_prefix 'team-a/': discovery scans
    team-a/ and finds the images seeded there.

    **Validates: Requirements 2.2**
    """
    _seed_bucket([
        "team-a/widgets/anomaly-1.jpg",
        "team-a/widgets/anomaly-2.jpg",
        # Keys outside the configured prefix must not be required for discovery.
        "other-team/stuff/anomaly-9.jpg",
    ])
    _set_usecase(_single_account_usecase(
        data_account_role_arn="arn:aws:iam::999999999999:role/data-access",
        data_account_external_id="data-ext-id",
        data_s3_bucket=BUCKET,
        data_s3_prefix="team-a/",
    ))

    body = _invoke_list_datasets()

    # On unfixed code: the scan targets "None/" instead of "team-a/" -> [].
    assert body["datasets"], (
        "Expected non-empty datasets (bucket contains team-a/widgets/*.jpg) "
        f"but got: datasets={body['datasets']!r}, "
        f"base_prefix={body['base_prefix']!r}"
    )
    discovered = {d["prefix"] for d in body["datasets"]}
    assert any(p.startswith("team-a/") for p in discovered), (
        f"Expected prefixes under 'team-a/', got {discovered!r}"
    )
    assert body["base_prefix"] == "team-a/", (
        f"Expected base_prefix 'team-a/', got {body['base_prefix']!r}"
    )
