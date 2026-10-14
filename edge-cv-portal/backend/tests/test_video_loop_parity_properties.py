"""
Property test for Video_Validation parity between the device and the
Portal (static-camera-video-loop task 7.2).

# Feature: static-camera-video-loop, Property 11: Sniff parity (device ⇔ Portal) and totality

*For any* byte string and the shared signature vectors, the device and
Portal ``sniff_video_container`` agree. They return a container name
exactly for the documented signatures and ``None`` for every JPEG/PNG/BMP
encoding. The two ``video_loop.py`` files are byte-identical.

**Validates: Requirements 1.3, 8.2**

The device module is loaded by path from ``src/backend/utils`` under a
distinct module name, so both copies are live side by side. The shared
vectors are the device suite's golden file. Example counts come from the
conftest hypothesis profiles.
"""
import hashlib
import importlib.util
import io
import json
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
DEVICE_MODULE = os.path.join(_REPO, "src", "backend", "utils", "video_loop.py")
PORTAL_MODULE = os.path.join(_HERE, "..", "functions", "video_loop.py")
VECTORS = os.path.join(_REPO, "test", "backend-test", "static_video_camera",
                       "goldens", "video_sniff_vectors.json")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def modules():
    if not os.path.isfile(DEVICE_MODULE):
        pytest.skip("device source tree not present next to the portal")
    device = _load(DEVICE_MODULE, "device_video_loop_under_test")
    import video_loop as portal  # the Lambda asset's copy (functions/)

    assert os.path.samefile(portal.__file__, PORTAL_MODULE)
    return device, portal


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def test_vendored_copy_is_byte_identical(modules):
    assert _sha256(PORTAL_MODULE) == _sha256(DEVICE_MODULE), (
        "edge-cv-portal/backend/functions/video_loop.py must be a verbatim "
        "copy of src/backend/utils/video_loop.py; copy the device module "
        "over after editing it")


def test_shared_vectors_sniff_the_same_on_both_sides(modules):
    device, portal = modules
    with open(VECTORS, "r", encoding="utf-8") as handle:
        vectors = json.load(handle)["vectors"]
    assert vectors
    for vector in vectors:
        head = bytes.fromhex(vector["head_hex"])
        assert device.sniff_video_container(head) == vector["expected"], \
            vector["name"]
        assert portal.sniff_video_container(head) == vector["expected"], \
            vector["name"]


@settings(deadline=None)
@given(data=st.binary(min_size=0, max_size=128))
def test_arbitrary_bytes_sniff_the_same_on_both_sides(modules, data):
    device, portal = modules
    result = portal.sniff_video_container(data)
    assert result == device.sniff_video_container(data)
    assert result is None or result in portal.SUPPORTED_VIDEO_CONTAINERS


@settings(deadline=None)
@given(
    image_format=st.sampled_from(["JPEG", "PNG", "BMP"]),
    size=st.tuples(st.integers(min_value=1, max_value=64),
                   st.integers(min_value=1, max_value=64)),
    color=st.tuples(st.integers(0, 255), st.integers(0, 255),
                    st.integers(0, 255)),
)
def test_still_images_are_never_videos(modules, image_format, size, color):
    from PIL import Image

    device, portal = modules
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=image_format)
    head = buffer.getvalue()
    assert portal.sniff_video_container(head) is None
    assert device.sniff_video_container(head) is None
