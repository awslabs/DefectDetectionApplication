
"""Property test for Plugin_Artifact sign-then-verify (task 10.2).

**Feature: custom-node-designer, Property 8: Sign-then-verify round trip with tamper detection**

For all artifact byte strings: storing the artifact through the
Plugin_Build_Service signing path (SHA-256 checksum + KMS ECDSA_SHA_256
signature over the digest, plugin_builds.store_signed_artifact) and then
verifying it through the Component_Packager verification path
(workflow_packaging.verify_custom_plugin_artifact: stream the bytes,
recompute the SHA-256, KMS-Verify the recorded signature) succeeds on
the unmodified bytes and returns the recorded checksum.

And for all tampered variants of the stored bytes (single byte flips,
truncations, appends, and wholesale replacement with different bytes),
verification fails via the existing PackagingError path identifying the
checksum failure; a recorded signature computed over different bytes
likewise fails verification identifying the signature failure.

**Validates: Requirements 3.3, 3.6, 10.4**

Runs against the moto-backed session stack from conftest.py: the
signing key is a real ECC_NIST_P256 KMS key (PLUGIN_SIGNING_KEY_ARN),
so signatures and verifications are genuine ECDSA operations.
"""

from __future__ import annotations

import hashlib
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import TEST_ENV

ARCHS = ("x86_64", "x86_64_nvidia", "arm64_jp4", "arm64_jp5", "arm64_jp6")
BUCKET = TEST_ENV["PORTAL_ARTIFACTS_BUCKET"]


@pytest.fixture(scope="module")
def builds(aws_stack):
    """The real plugin_builds module (signing path), from the session stack."""
    return aws_stack.plugin_builds


@pytest.fixture(scope="module")
def packaging(aws_stack):
    """Import workflow_packaging inside the moto mock so its module-level
    boto3 clients (S3 / KMS) are intercepted."""
    sys.modules.pop("workflow_packaging", None)
    import workflow_packaging

    return workflow_packaging


# ---------------------------------------------------------------------------
# Generators: artifact bytes and a guaranteed-different tampered variant
# ---------------------------------------------------------------------------

@st.composite
def artifact_cases(draw):
    """(data, tampered, arch): artifact bytes plus a tampered variant
    produced by a byte flip, truncation, append, or replacement with
    different bytes — always with tampered != data."""
    data = draw(st.binary(min_size=1, max_size=2048))
    kind = draw(st.sampled_from(("flip", "truncate", "append", "replace")))
    if kind == "flip":
        index = draw(st.integers(min_value=0, max_value=len(data) - 1))
        delta = draw(st.integers(min_value=1, max_value=255))
        tampered = (data[:index]
                    + bytes([data[index] ^ delta])
                    + data[index + 1:])
    elif kind == "truncate":
        cut = draw(st.integers(min_value=1, max_value=len(data)))
        tampered = data[:len(data) - cut]
    elif kind == "append":
        tampered = data + draw(st.binary(min_size=1, max_size=64))
    else:  # replace
        tampered = draw(
            st.binary(min_size=0, max_size=2048).filter(lambda b: b != data))
    assert tampered != data
    arch = draw(st.sampled_from(ARCHS))
    return data, tampered, arch


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(case=artifact_cases())
def test_sign_then_verify_round_trip_with_tamper_detection(
        aws_stack, builds, packaging, case):
    """**Feature: custom-node-designer, Property 8: Sign-then-verify round trip with tamper detection**

    For all artifact byte strings, the signing path (SHA-256 checksum +
    KMS signature, Requirements 3.3/3.6) followed by the packaging
    verification path succeeds on the unmodified bytes; for all
    tampered variants the checksum recompute fails via PackagingError,
    and a signature recorded over different bytes fails the KMS
    signature verification via PackagingError (Requirement 10.4).

    **Validates: Requirements 3.3, 3.6, 10.4**
    """
    data, tampered, arch = case
    s3 = aws_stack.s3

    # Deterministic per-example namespace so examples never collide.
    usecase_id = f"uc-p8-{hashlib.sha256(data + tampered).hexdigest()[:12]}"
    plugin_id = "plg-p8"
    plugin_name = "sign-verify-plugin"
    dependency = f"custom:{usecase_id}/{plugin_name}"
    node_type_id = "custom.sign-verify"

    # --- signing path (3.3 / 3.6): checksum, KMS-sign, store .so + .sig
    # at the immutable per-version Plugin_Library key (defect 8).
    entry = builds.store_signed_artifact(usecase_id, arch, plugin_name, data,
                                         plugin_id, 1)
    record = {
        "plugin_id": plugin_id,
        "version": 1,
        "usecase_id": usecase_id,
        "name": plugin_name,
        "artifacts": {arch: {**entry, "buildStatus": "succeeded"}},
    }

    # The recorded checksum is the SHA-256 of exactly the stored bytes,
    # and the detached signature object sits alongside the artifact.
    assert entry["checksum"] == hashlib.sha256(data).hexdigest()
    stored = s3.get_object(Bucket=BUCKET, Key=entry["s3Key"])["Body"].read()
    assert stored == data
    s3.head_object(Bucket=BUCKET, Key=entry["s3Key"] + ".sig")

    # --- round trip (10.4): unmodified bytes verify successfully
    manifest_key, checksum = packaging.verify_custom_plugin_artifact(
        dependency, node_type_id, record, arch)
    assert checksum == entry["checksum"]
    assert manifest_key.endswith(f"/{plugin_name}.so")

    # --- tamper detection (10.4): modified stored bytes fail the
    # checksum recompute through the PackagingError path
    s3.put_object(Bucket=BUCKET, Key=entry["s3Key"], Body=tampered)
    with pytest.raises(packaging.PackagingError) as checksum_failure:
        packaging.verify_custom_plugin_artifact(
            dependency, node_type_id, record, arch)
    assert "checksum" in checksum_failure.value.message
    assert checksum_failure.value.artifact == (
        f"custom-plugins/{arch}/{plugin_name}.so")

    # --- signature over different bytes (10.4): restore the original
    # artifact so the checksum passes, but record a signature computed
    # over the tampered bytes — KMS verification must reject it
    s3.put_object(Bucket=BUCKET, Key=entry["s3Key"], Body=data)
    wrong_signature = builds.sign_digest(hashlib.sha256(tampered).digest())
    forged_record = {
        **record,
        "artifacts": {arch: {**entry, "buildStatus": "succeeded",
                             "signature": wrong_signature}},
    }
    with pytest.raises(packaging.PackagingError) as signature_failure:
        packaging.verify_custom_plugin_artifact(
            dependency, node_type_id, forged_record, arch)
    assert "signature" in signature_failure.value.message
    assert signature_failure.value.artifact == (
        f"custom-plugins/{arch}/{plugin_name}.so")
