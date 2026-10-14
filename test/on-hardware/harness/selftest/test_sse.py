"""Unit tests for harnesslib.sse (Req 5.3).

Covers ``data:``-framed chunking, multi-line events, comment/unknown-field
handling, byte/str input, and clean-termination detection (truncated streams
raise instead of silently looking complete).
"""

import pytest
from harnesslib.sse import SseStreamError, iter_data_events


def events(lines):
    return list(iter_data_events(lines))


class TestChunking:
    def test_single_event(self):
        assert events(['data: {"token": "a"}', ""]) == ['{"token": "a"}']

    def test_multiple_events_in_order(self):
        lines = [
            'data: {"token": "a"}',
            "",
            'data: {"token": "b"}',
            "",
            'data: {"done": true}',
            "",
        ]
        assert events(lines) == [
            '{"token": "a"}',
            '{"token": "b"}',
            '{"done": true}',
        ]

    def test_multi_line_data_joined_with_newline(self):
        assert events(["data: first", "data: second", ""]) == ["first\nsecond"]

    def test_bytes_input_decoded(self):
        assert events([b"data: hello", b""]) == ["hello"]

    def test_exactly_one_leading_space_stripped(self):
        assert events(["data:  padded", ""]) == [" padded"]

    def test_no_space_after_colon(self):
        assert events(["data:tight", ""]) == ["tight"]

    def test_empty_stream_yields_nothing(self):
        assert events([]) == []

    def test_extra_blank_lines_between_events_ignored(self):
        assert events(["", "data: x", "", "", "data: y", ""]) == ["x", "y"]


class TestNonDataLines:
    def test_comment_lines_ignored(self):
        assert events([": keep-alive", "data: x", ""]) == ["x"]

    def test_unknown_fields_ignored(self):
        lines = ["event: message", "id: 7", "retry: 100", "data: x", ""]
        assert events(lines) == ["x"]

    def test_field_name_without_colon_ignored(self):
        assert events(["garbage", "data: x", ""]) == ["x"]

    def test_bare_data_field_contributes_empty_payload(self):
        # "data" with no colon is a field with an empty value per the spec.
        assert events(["data", ""]) == [""]


class TestTermination:
    def test_clean_termination_after_final_blank_line(self):
        assert events(["data: x", ""]) == ["x"]

    def test_truncated_stream_raises(self):
        with pytest.raises(SseStreamError, match="mid-event"):
            events(["data: x", "", "data: cut-off"])

    def test_truncated_error_names_buffered_data(self):
        with pytest.raises(SseStreamError, match="cut-off"):
            events(["data: cut-off"])

    def test_trailing_comment_without_event_is_clean(self):
        assert events(["data: x", "", ": bye"]) == ["x"]
