"""
Property-based test for Portal pin submission validation
(cloud-static-camera-provisioning task 2.2).

# Feature: cloud-static-camera-provisioning, Property 1: Submission validation acceptance

*For any* byte payload and injectable size limit, a Portal pin
submission is accepted exactly when the payload decodes as a
Supported_Image_Format (JPEG, PNG, BMP) and its size is at or below the
limit; every rejection names the supported formats (undecodable input)
or the size limit (oversize input), and a rejected submission records
zero Pin_Request items, zero Image_Transport writes, and zero
Sync_Channel writes.

**Validates: Requirements 1.1, 1.3, 1.4**

Payload generators: Pillow-generated real images across the supported
formats (varying dimensions/colors to vary byte length), decodable but
UNsupported formats (GIF, TIFF), and arbitrary undecodable bytes. The
size limit is injected per example (the module reads it at call time)
straddling each payload's actual length, so the boundary is exercised
without 50 MB payloads. Example counts come from the conftest hypothesis
profiles (portal-fast locally; the spec-minimum 100 with
HYPOTHESIS_PROFILE=ci) — never hardcoded here.
"""
import io

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, audit_events, create_usecase,
    image_bytes, make_user, pin_env, pin_request_items, register_device,
    submit_pin,
)

SUPPORTED = ("JPEG", "PNG", "BMP")


@pytest.fixture(scope="module")
def operator(pin_env):
    usecase_id = create_usecase(pin_env)
    return make_user("Operator"), usecase_id


def decodes_as_supported(payload):
    """The requirement's own oracle: the payload decodes (full load) as a
    Supported_Image_Format."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(payload)) as img:
            if img.format not in SUPPORTED:
                return False
            img.load()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

_dimensions = st.tuples(st.integers(min_value=1, max_value=48),
                        st.integers(min_value=1, max_value=48))
_colors = st.tuples(st.integers(0, 255), st.integers(0, 255),
                    st.integers(0, 255))

_valid_payloads = st.builds(
    image_bytes,
    image_format=st.sampled_from(SUPPORTED),
    size=_dimensions,
    color=_colors,
)
# Decodable, but not a Supported_Image_Format.
_unsupported_payloads = st.builds(
    image_bytes,
    image_format=st.sampled_from(["GIF", "TIFF"]),
    size=_dimensions,
    color=_colors,
)
_undecodable_payloads = st.binary(min_size=0, max_size=256)

_payloads = st.one_of(_valid_payloads, _unsupported_payloads,
                      _undecodable_payloads)

# Limit deltas straddling each payload's actual byte length; the huge
# delta stands in for the real headroom of the 50 MB default.
_limit_deltas = st.one_of(st.integers(min_value=-64, max_value=64),
                          st.just(10_000_000))


# Example count comes from the conftest hypothesis profile: 25 for fast
# local runs (portal-fast), 100 (the spec minimum) with HYPOTHESIS_PROFILE=ci.
@settings(deadline=None)
@given(payload=_payloads, limit_delta=_limit_deltas)
def test_pin_submission_validation_acceptance(pin_env, operator, payload,
                                              limit_delta):
    """Property 1: accepted iff decodable Supported_Image_Format within
    the size limit; rejections name the formats or the limit and record
    zero items, zero Image_Transport writes, zero Sync_Channel writes."""
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id, prefix="thing-prop1")

    limit = max(1, len(payload) + limit_delta)
    log = []
    fake_s3 = RecordingS3(pin_env.s3, log=log)
    fake_shadow = FakeIotDataClient(log=log)
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow

    original_limit = pin_env.module.MAX_PIN_IMAGE_BYTES
    pin_env.module.MAX_PIN_IMAGE_BYTES = limit
    try:
        status, body = submit_pin(pin_env, device_id, user, payload)
    finally:
        pin_env.module.MAX_PIN_IMAGE_BYTES = original_limit

    size_ok = len(payload) <= limit
    decodable = decodes_as_supported(payload)

    if size_ok and decodable:
        # Accepted exactly when decodable and within the limit (Req 1.1).
        assert status == 201, body
        assert body["status"] == "pending"
        assert len(fake_s3.copies) == 1
        assert len(fake_shadow.updates) == 1
    else:
        assert status == 400, body
        error = body["error"]
        if not size_ok:
            # Oversize rejection names the limit (Req 1.4).
            assert str(limit) in error
        else:
            # Undecodable rejection enumerates the formats (Req 1.3).
            for fmt in SUPPORTED:
                assert fmt in error
        # Zero Pin_Request items, zero Image_Transport canonical writes,
        # zero Sync_Channel writes.
        assert pin_request_items(pin_env, device_id) == []
        assert fake_s3.copies == []
        assert fake_shadow.updates == []
        # No acceptance audit event either.
        assert audit_events(pin_env, device_id, action="pin_static_image") \
            == []
