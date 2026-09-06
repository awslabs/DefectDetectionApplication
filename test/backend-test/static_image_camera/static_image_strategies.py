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
"""Shared Hypothesis strategies and oracles for the static-image-camera
property suites (feature: static-image-camera-source).

Images are generated in memory with Pillow: random dimensions, random
pixel content (seed-derived so shrinking stays cheap), format drawn from
the Supported_Image_Formats {JPEG, PNG, BMP}. The decode oracles compute
the expected frame/metadata exactly as the design specifies the store
must (Pillow decode + EXIF transpose + packed 24-bit RGB), so equality
assertions are byte-for-byte against an independent decode of the same
submitted bytes."""
import io
import random

from hypothesis import strategies as st
from PIL import Image, ImageOps

FORMATS = ("JPEG", "PNG", "BMP")

# (width, height, pixel_seed, format): the full description of a generated
# valid image. Dimensions are kept small so 100-example runs stay fast;
# the store is dimension-agnostic.
image_specs = st.tuples(
    st.integers(min_value=1, max_value=48),
    st.integers(min_value=1, max_value=48),
    st.integers(min_value=0, max_value=2**32 - 1),
    st.sampled_from(FORMATS),
)

# Simple original-file-name strategy: the store echoes it verbatim.
file_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=24,
).map(lambda stem: stem + ".img")


def render_image_bytes(width, height, seed, img_format):
    """Encode a random-pixel RGB image of the given dimensions/format."""
    pixels = random.Random(seed).randbytes(width * height * 3)
    image = Image.frombytes("RGB", (width, height), pixels)
    buffer = io.BytesIO()
    image.save(buffer, format=img_format)
    return buffer.getvalue()


def expected_frame(data):
    """Oracle: the packed 24-bit RGB decode of the encoded bytes."""
    with Image.open(io.BytesIO(data)) as img:
        rgb = ImageOps.exif_transpose(img).convert("RGB")
        return {
            "data": rgb.tobytes(),
            "width": rgb.width,
            "height": rgb.height,
            "pixel_format": "RGB",
        }


def expected_metadata(data, file_name):
    """Oracle: the metadata fields the store must report (no timestamp)."""
    with Image.open(io.BytesIO(data)) as img:
        img_format = img.format
        transposed = ImageOps.exif_transpose(img)
        return {
            "fileName": file_name,
            "format": img_format,
            "width": transposed.width,
            "height": transposed.height,
            "fileSizeBytes": len(data),
        }


def metadata_without_timestamp(metadata):
    """Strip the pin timestamp for comparisons across distinct pin calls."""
    return {k: v for k, v in metadata.items() if k != "pinnedAtEpochMs"}


# ---------------------------------------------------------------------------
# Physical-camera doubles for the wired enumeration suites (tasks 4.2-4.4).
# ---------------------------------------------------------------------------

#: Realistic {Vendor}-{Serial} identifier shapes. By construction none of
#: these can ever equal the fixed static identifier "static-image-camera"
#: (capitalized vendor prefix + uppercase hex serial), so identifier
#: non-collision assertions cannot be vacuously broken by the generator.
_vendors = st.sampled_from(["Aravis", "Basler", "Lucid", "Allied", "Omron"])
_serials = st.text(alphabet="ABCDEF0123456789", min_size=4, max_size=10)


def _make_physical_camera(vendor, serial, octet):
    return {
        "id": "{}-{}".format(vendor, serial),
        "model": "{} Cam {}".format(vendor, octet),
        "address": "192.168.10.{}".format(octet),
        "physical_id": "{}-{}-0".format(vendor, serial),
        "protocol": "USB3Vision",
        "serial": serial,
        "vendor": vendor,
    }


#: A single physical camera's seven identity fields.
physical_camera_specs = st.builds(
    _make_physical_camera, _vendors, _serials, st.integers(1, 250)
)

#: Physical camera lists, including the empty list (Cloud_Environment).
physical_camera_lists = st.lists(
    physical_camera_specs, min_size=0, max_size=4, unique_by=lambda c: c["id"]
)

_CAMERA_FIELDS = (
    "id", "model", "address", "physical_id", "protocol", "serial", "vendor"
)


def camera_fields(camera):
    """The seven identity fields of a ``model.Camera`` as a tuple."""
    return tuple(getattr(camera, field) for field in _CAMERA_FIELDS)


def physical_fields(physical_spec):
    """The seven identity fields of a generated physical camera dict."""
    return tuple(physical_spec[field] for field in _CAMERA_FIELDS)


class FakeAravisBus:
    """Stands in for ``aravis_functions``' module-level ``Aravis`` binding.

    Implements exactly the enumeration entry points ``getCameras()`` /
    ``rescan_cameras()`` touch, backed by a generated physical camera list.
    """

    def __init__(self, physical):
        self.physical = list(physical)

    def enable_interface(self, name):
        pass

    def update_device_list(self):
        pass

    def shutdown(self):
        pass

    def get_n_devices(self):
        return len(self.physical)

    def get_device_id(self, i):
        return self.physical[i]["id"]

    def get_device_model(self, i):
        return self.physical[i]["model"]

    def get_device_address(self, i):
        return self.physical[i]["address"]

    def get_device_physical_id(self, i):
        return self.physical[i]["physical_id"]

    def get_device_protocol(self, i):
        return self.physical[i]["protocol"]

    def get_device_serial_nbr(self, i):
        return self.physical[i]["serial"]

    def get_device_vendor(self, i):
        return self.physical[i]["vendor"]
