# Copyright 2026 Amazon Web Services, Inc.
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
"""Property test for the LLM_Binding's image downscaling geometry (task 6.2).

# Feature: vllm-workflow-latency-optimization, Property 9: Downscaling geometry (iff around the threshold)

*For any* decodable captured image and *for any* configured maximum pixel
dimension >= 1: when the image's longer edge exceeds the maximum, the bytes
sent SHALL decode to an image whose longer edge equals the maximum with the
aspect ratio preserved (within 1-pixel rounding); when the longer edge is
less than or equal to the maximum, the bytes sent SHALL be byte-identical to
the captured bytes (never upscaled).

**Validates: Requirements 5.3, 5.7**

The test builds real PIL images (hypothesis-generated dimensions in 1..512,
JPEG or PNG encoding, arbitrary fill color), encodes them to bytes, and
drives :func:`workflow_engine.output_bindings.downscale_image_bytes` with a
hypothesis-generated ``max_dim`` in 1..600 — so the generated cases land on
both sides of (and exactly on) the threshold.
"""
import io

from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from workflow_engine.output_bindings import downscale_image_bytes


def encoded_bytes(width, height, color, image_format):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=image_format)
    return buffer.getvalue()


def decoded_size(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size


color_component = st.integers(min_value=0, max_value=255)


@settings(max_examples=100, deadline=None)
@given(
    width=st.integers(min_value=1, max_value=512),
    height=st.integers(min_value=1, max_value=512),
    color=st.tuples(color_component, color_component, color_component),
    image_format=st.sampled_from(["JPEG", "PNG"]),
    max_dim=st.integers(min_value=1, max_value=600),
)
def test_downscaling_geometry_iff_around_threshold(
    width, height, color, image_format, max_dim
):
    data = encoded_bytes(width, height, color, image_format)
    result = downscale_image_bytes(data, max_dim)

    longer = max(width, height)
    shorter = min(width, height)

    if longer <= max_dim:
        # R5.7: at or below the threshold the captured bytes ride
        # byte-identical — never upscaled, never re-encoded.
        assert result == data
    else:
        # R5.3: above the threshold the sent bytes decode to an image
        # whose longer edge equals the configured maximum, aspect ratio
        # preserved within 1-pixel rounding, shorter edge >= 1.
        out_width, out_height = decoded_size(result)
        assert max(out_width, out_height) == max_dim
        out_shorter = min(out_width, out_height)
        expected_shorter = shorter * max_dim / float(longer)
        assert out_shorter >= 1
        assert abs(out_shorter - expected_shorter) <= 1
        # Orientation is preserved: the longer input edge stays the
        # longer output edge (equal edges yield a square either way).
        if width > height:
            assert out_width == max_dim
        elif height > width:
            assert out_height == max_dim
