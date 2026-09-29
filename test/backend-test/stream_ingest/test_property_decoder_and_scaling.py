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
"""Property tests for the Stream_Worker's pure builders
(rtsp-rtmp-stream-cameras task 16.2).

**Feature: rtsp-rtmp-stream-cameras, Property 16: Decoder selection policy**
**Feature: rtsp-rtmp-stream-cameras, Property 17: Frame scaling fits, preserves aspect, and never upscales**
**Validates: Requirements 7.4, 7.5, 7.6**
"""
import os

os.environ.setdefault("COMPONENT_WORK_PATH", "/tmp")

import pytest  # noqa: E402
from hypothesis import given, settings, strategies as st  # noqa: E402

from stream_ingest import health  # noqa: E402
from stream_ingest.health import StreamError  # noqa: E402
from stream_ingest.pipeline import (  # noqa: E402
    DECODER_POLICIES,
    HARDWARE,
    SOFTWARE,
    RateLimiter,
    available_decoders,
    decoder_chain,
    fit_within,
    normalize_codec,
    select_decoder,
    tail_description,
)

HARDWARE_ELEMENTS = ("nvv4l2decoder", "nvh264dec", "nvh265dec")
SOFTWARE_ELEMENTS = ("avdec_h264", "avdec_h265")

codec_names = st.sampled_from(["h264", "H264", "avc", "h265", "H265", "hevc", "hvc1"])
unsupported_codecs = st.sampled_from(["MJPEG", "mpeg4", "vp8", "VP9", "av1", "", "JPEG"])


@st.composite
def capability_sets(draw):
    """Any mix of present and absent decoders, with occasional junk."""
    codecs = {}
    for codec in ("h264", "h265"):
        if draw(st.booleans()):
            continue  # the codec is missing entirely
        entry = {}
        entry["hardware"] = draw(st.one_of(st.none(), st.just(""), st.sampled_from(HARDWARE_ELEMENTS)))
        entry["software"] = draw(st.one_of(st.none(), st.just(""), st.sampled_from(SOFTWARE_ELEMENTS)))
        codecs[codec] = entry
    return {"codecs": codecs}


def _expected(policy, codec, capabilities, failed_hardware):
    """The policy table of Property 16, written independently of the code."""
    normalized = normalize_codec(codec)
    entry = capabilities["codecs"].get(normalized) or {}
    hardware = entry.get("hardware") or None
    software = entry.get("software") or None
    if policy == SOFTWARE:
        return (SOFTWARE, software) if software else None
    if policy == HARDWARE:
        return (HARDWARE, hardware) if hardware and not failed_hardware else None
    if hardware and not failed_hardware:
        return HARDWARE, hardware
    return (SOFTWARE, software) if software else None


class TestProperty16DecoderSelection:
    @settings(max_examples=25, deadline=None)
    @given(policy=st.sampled_from(DECODER_POLICIES), codec=codec_names,
           capabilities=capability_sets(), failed_hardware=st.booleans())
    def test_the_selection_follows_the_policy_table(self, policy, codec, capabilities, failed_hardware):
        expected = _expected(policy, codec, capabilities, failed_hardware)
        if expected is None:
            with pytest.raises(StreamError) as raised:
                select_decoder(policy, codec, capabilities, failed_hardware)
            assert raised.value.category == health.DECODER_UNAVAILABLE
            return
        selection = select_decoder(policy, codec, capabilities, failed_hardware)
        assert (selection.kind, selection.element) == expected
        assert selection.codec == normalize_codec(codec)
        # Never a decoder absent from the capabilities.
        hardware, software = available_decoders(capabilities, selection.codec)
        assert selection.element in (hardware, software)
        # decoderFallback exactly when auto chose software over a failed
        # hardware decoder (Requirement 7.5).
        assert selection.fallback == (policy == "auto" and selection.kind == SOFTWARE
                                      and bool(hardware) and failed_hardware)

    @settings(max_examples=25, deadline=None)
    @given(policy=st.sampled_from(DECODER_POLICIES), codec=unsupported_codecs,
           capabilities=capability_sets(), failed_hardware=st.booleans())
    def test_a_codec_other_than_h264_and_h265_is_unsupported_and_named(
            self, policy, codec, capabilities, failed_hardware):
        with pytest.raises(StreamError) as raised:
            select_decoder(policy, codec, capabilities, failed_hardware)
        assert raised.value.category == health.UNSUPPORTED_CODEC
        assert (codec or "unknown") in raised.value.message

    @settings(max_examples=25, deadline=None)
    @given(policy=st.sampled_from(DECODER_POLICIES), codec=codec_names,
           capabilities=capability_sets(), failed_hardware=st.booleans())
    def test_the_chain_uses_exactly_the_selected_decoder(self, policy, codec, capabilities, failed_hardware):
        try:
            selection = select_decoder(policy, codec, capabilities, failed_hardware)
        except StreamError:
            return
        description = tail_description(selection.codec, selection, (640, 360))
        elements = [token.split()[0] for token in description.split(" ! ")]
        decoders = [name for name in elements if name in HARDWARE_ELEMENTS + SOFTWARE_ELEMENTS]
        assert decoders == [selection.element]
        assert elements[-1] == "appsink"
        # The publish cap's element sits right after the decoder; never
        # videorate, which aborts on frames without a duration.
        assert "identity" in elements and elements.index("identity") == elements.index(selection.element) + 1
        assert "videorate" not in elements

    @pytest.mark.parametrize("codec", ["h264", "h265"])
    def test_auto_falls_back_to_software_after_a_hardware_failure(self, codec):
        capabilities = {"codecs": {codec: {"hardware": "nvv4l2decoder", "software": f"avdec_{codec}"}}}
        assert select_decoder("auto", codec, capabilities).kind == HARDWARE
        fallback = select_decoder("auto", codec, capabilities, failed_hardware=True)
        assert (fallback.kind, fallback.element, fallback.fallback) == (SOFTWARE, f"avdec_{codec}", True)
        software_only = {"codecs": {codec: {"software": f"avdec_{codec}"}}}
        assert select_decoder("auto", codec, software_only, failed_hardware=True).fallback is False
        with pytest.raises(StreamError) as raised:
            select_decoder("hardware", codec, capabilities, failed_hardware=True)
        assert raised.value.category == health.DECODER_UNAVAILABLE

    def test_malformed_capabilities_select_nothing(self):
        for capabilities in (None, {}, {"codecs": None}, {"codecs": {"h264": "avdec_h264"}},
                             {"codecs": {"h264": {"software": 7}}}):
            with pytest.raises(StreamError) as raised:
                select_decoder("auto", "h264", capabilities)
            assert raised.value.category == health.DECODER_UNAVAILABLE

    def test_an_unknown_policy_is_a_programming_error(self):
        with pytest.raises(ValueError):
            select_decoder("fastest", "h264", {"codecs": {"h264": {"software": "avdec_h264"}}})


dimensions = st.integers(min_value=2, max_value=8192)
maximums = st.integers(min_value=2, max_value=4096)


class TestProperty17FrameScaling:
    @settings(max_examples=25, deadline=None)
    @given(width=dimensions, height=dimensions, max_dim=maximums)
    def test_the_result_fits_is_even_and_never_upscales(self, width, height, max_dim):
        new_width, new_height = fit_within(width, height, max_dim)
        assert new_width % 2 == 0 and new_height % 2 == 0
        assert new_width >= 2 and new_height >= 2
        assert max(new_width, new_height) <= max(2, max_dim)
        assert new_width <= max(2, width) and new_height <= max(2, height)

    @settings(max_examples=25, deadline=None)
    @given(width=dimensions, height=dimensions, max_dim=maximums)
    def test_the_aspect_ratio_is_kept_within_rounding(self, width, height, max_dim):
        new_width, new_height = fit_within(width, height, max_dim)
        if width >= height:
            ideal = height * new_width / width
            actual = new_height
        else:
            ideal = width * new_height / height
            actual = new_width
        # Within one pixel of the exact value; a shorter edge that would be
        # under 2 pixels is held at the 2-pixel minimum.
        assert abs(actual - ideal) <= 1 or (actual == 2 and ideal < 2)

    @settings(max_examples=25, deadline=None)
    @given(width=dimensions, height=dimensions, max_dim=maximums)
    def test_the_longer_edge_is_as_large_as_allowed(self, width, height, max_dim):
        new_width, new_height = fit_within(width, height, max_dim)
        bound = min(max(width, height), max_dim)
        assert max(new_width, new_height) in (bound, bound - 1)

    def test_examples(self):
        assert fit_within(3840, 2160, 1920) == (1920, 1080)
        assert fit_within(1920, 1080, 1920) == (1920, 1080)
        assert fit_within(1280, 720, 1920) == (1280, 720)
        assert fit_within(1080, 1920, 1280) == (720, 1280)
        assert fit_within(1921, 1081, 4096) == (1920, 1080)
        assert fit_within(704, 576, 320) == (320, 262)

    @pytest.mark.parametrize("arguments", [(1, 100, 100), (100, 0, 100), (100, 100, 1),
                                           (True, 100, 100), (100.0, 100, 100)])
    def test_degenerate_inputs_are_rejected(self, arguments):
        with pytest.raises(ValueError):
            fit_within(*arguments)


class TestChains:
    def test_the_jetson_chain_scales_on_the_vic(self):
        selection = select_decoder("auto", "h265", {"codecs": {"h265": {"hardware": "nvv4l2decoder",
                                                                       "software": "avdec_h265"}}})
        chain = decoder_chain("h265", selection, (1280, 720))
        assert chain.startswith("nvv4l2decoder name=decoder ! identity name=rate silent=true ! nvvidconv")
        assert "nvvidconv ! capsfilter name=scale caps=video/x-raw,format=RGBA,width=1280,height=720" in chain
        assert chain.endswith("video/x-raw,format=RGB")

    def test_the_software_chain_scales_before_converting(self):
        selection = select_decoder("software", "h264", {"codecs": {"h264": {"software": "avdec_h264"}}})
        chain = decoder_chain("h264", selection, None, publish_fps=5)
        assert chain == ("avdec_h264 name=decoder ! identity name=rate silent=true ! "
                         "videoscale ! videoconvert ! capsfilter name=scale caps=video/x-raw,format=RGB")


class TestRateLimiter:
    """The publish cap (found on hardware, task 25.3: ``videorate``
    drop-only aborted the worker for a camera whose frames carry no
    duration, and a PTS-timed cap passed 17-21 frames per second of a
    30 fps camera that set no PTS on a third of its frames). It is timed by
    arrival only."""

    @staticmethod
    def kept(source_fps, cap, seconds=10.0, jitter_ns=0):
        limiter = RateLimiter(cap)
        period = 1_000_000_000 / source_fps
        count = int(source_fps * seconds)
        stamps = [int(index * period) + (jitter_ns if index % 2 else -jitter_ns) for index in range(count)]
        return sum(limiter.keep(max(0, stamp)) for stamp in stamps) / seconds

    @pytest.mark.parametrize("source_fps,cap,expected", [
        (15, 10, 10.0), (30, 10, 10.0), (25, 10, 10.0), (10, 10, 10.0), (7, 10, 7.0),
        (60, 5, 5.0), (29.97, 10, 10.0), (100, 1, 1.0)])
    def test_a_source_is_thinned_to_the_cap_or_kept_whole(self, source_fps, cap, expected):
        assert abs(self.kept(source_fps, cap) - expected) <= 0.2, self.kept(source_fps, cap)

    @pytest.mark.parametrize("source_fps,kept_per_three", [(15, 2), (30, 1)])
    def test_a_faster_source_is_thinned_evenly(self, source_fps, kept_per_three):
        """After the initial burst, every three consecutive frames keep the
        same number, and no gap between kept frames exceeds an interval
        plus a source period."""
        limiter = RateLimiter(10)
        period = 1_000_000_000 / source_fps
        pattern = [limiter.keep(int(index * period)) for index in range(300)]
        assert all(sum(pattern[index:index + 3]) == kept_per_three for index in range(10, 297))
        kept_at = [index * period for index, kept in enumerate(pattern) if kept]
        assert max(b - a for a, b in zip(kept_at, kept_at[1:])) <= 1.01 * (period + limiter.interval_ns)

    def test_arrival_jitter_at_the_cap_keeps_every_frame(self):
        assert self.kept(10, 10, jitter_ns=5_000_000) == 10.0
        assert self.kept(10, 10, jitter_ns=40_000_000) == 10.0

    @settings(max_examples=100, deadline=None)
    @given(cap=st.integers(min_value=1, max_value=30), data=st.data())
    def test_a_source_at_or_below_the_cap_keeps_every_frame_despite_jitter(self, cap, data):
        """Arrivals that jitter by less than half an interval never lose a
        frame: an early frame spends what a late one left."""
        source_fps = data.draw(st.integers(min_value=1, max_value=cap))
        interval = 1_000_000_000 // cap
        jitters = data.draw(st.lists(st.integers(min_value=-(interval * 45 // 100), max_value=interval * 45 // 100),
                                     min_size=2, max_size=120))
        period = 1_000_000_000 / source_fps
        stamps = [int(index * period) + jitter + interval for index, jitter in enumerate(jitters)]
        limiter = RateLimiter(cap)
        assert all(limiter.keep(stamp) for stamp in stamps)

    @settings(max_examples=100, deadline=None)
    @given(cap=st.integers(min_value=1, max_value=30),
           gaps=st.lists(st.integers(min_value=0, max_value=3_000_000_000), min_size=1, max_size=200))
    def test_the_cap_is_never_exceeded(self, cap, gaps):
        """Between any two kept frames, the frames kept number at most the
        burst plus the intervals elapsed: never more than the cap on
        average, whatever the arrival times."""
        limiter = RateLimiter(cap)
        timestamp, kept = 0, []
        for gap in gaps:
            timestamp += gap
            if limiter.keep(timestamp):
                kept.append(timestamp)
        assert kept[0] == gaps[0], "the first frame is always kept"
        for first in range(len(kept)):
            for last in range(first, len(kept)):
                elapsed = (kept[last] - kept[first]) / limiter.interval_ns
                assert last - first + 1 <= RateLimiter.BURST + elapsed + 1e-6

    @settings(max_examples=50, deadline=None)
    @given(cap=st.integers(min_value=1, max_value=30), source_fps=st.integers(min_value=1, max_value=60))
    def test_a_regular_source_keeps_the_lesser_of_its_rate_and_the_cap(self, cap, source_fps):
        assert abs(self.kept(source_fps, cap, seconds=20.0) - min(cap, source_fps)) <= 0.15

    def test_a_stall_lets_at_most_the_burst_through_and_earlier_times_add_nothing(self):
        limiter = RateLimiter(10)
        assert limiter.keep(0) and limiter.keep(50_000_000)          # the burst of two
        assert not limiter.keep(60_000_000)
        assert limiter.keep(5_000_000_000) and limiter.keep(5_010_000_000)   # after a 5 s stall
        assert not limiter.keep(5_020_000_000)
        assert limiter.keep(5_105_000_000)
        assert not limiter.keep(4_000_000_000)                       # earlier: nothing accrues
        assert not limiter.keep(5_150_000_000) and limiter.keep(5_210_000_000)
