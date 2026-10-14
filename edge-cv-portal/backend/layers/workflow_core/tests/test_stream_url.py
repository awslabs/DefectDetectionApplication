"""Unit tests for `workflow_core.stream_url` (Stream_URL rules).

Feature: rtsp-rtmp-stream-cameras, task 1.1. Deterministic companions to
the hypothesis properties of tasks 1.2 (Property 2) and 1.3 (Property 3):
they pin the constants, the problem codes, the message contract (name the
offending part, never echo its value), normalization, redaction and
`compose_connect_url`.

Requirements: 1.3, 2.1, 2.2, 6.1, 6.3, 10.6.
"""

import re

import pytest

from workflow_core.stream_url import (
    DEFAULT_PORTS,
    SCHEMES_BY_NODE_TYPE,
    SCHEMES_BY_SOURCE_TYPE,
    SECRET_QUERY_PARAMETERS,
    STREAM_URL_PATTERN,
    StreamUrlProblem,
    check_stream_url,
    compose_connect_url,
    normalize_stream_url,
    redact,
)

RTSP_SCHEMES = SCHEMES_BY_NODE_TYPE["rtsp_camera_source"]
RTMP_SCHEMES = SCHEMES_BY_NODE_TYPE["rtmp_stream_source"]

VALID_RTSP_URLS = (
    "rtsp://192.168.1.64:554/Streaming/Channels/101",
    "rtsp://cam.local/live",
    "rtsp://cam.local",
    "rtsps://cam.local:322/live?profile=high&channel=1",
    "rtsp://[2001:db8::1]:554/live",
    "rtsp://[::1]/live",
    "rtsp://cam/live?keyframe=1",
    "rtsp://cam/pa@th",
)

VALID_RTMP_URLS = (
    "rtmp://media.local/live/line1",
    "rtmps://media.local:443/live/line1",
    "rtmp://10.0.0.5:1935/app",
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_scheme_maps_cover_both_node_and_source_types():
    assert SCHEMES_BY_NODE_TYPE == {
        "rtsp_camera_source": ("rtsp", "rtsps"),
        "rtmp_stream_source": ("rtmp", "rtmps"),
    }
    assert SCHEMES_BY_SOURCE_TYPE == {"RTSP": ("rtsp", "rtsps"), "RTMP": ("rtmp", "rtmps")}
    assert set(DEFAULT_PORTS) == {"rtsp", "rtsps", "rtmp", "rtmps"}
    assert DEFAULT_PORTS == {"rtsp": 554, "rtsps": 322, "rtmp": 1935, "rtmps": 443}


def test_secret_query_parameters_are_the_glossary_set():
    assert SECRET_QUERY_PARAMETERS == frozenset(
        {
            "password",
            "passwd",
            "pwd",
            "pass",
            "secret",
            "token",
            "key",
            "apikey",
            "api_key",
            "auth",
            "signature",
            "sig",
            "streamkey",
            "stream_key",
        }
    )
    assert all(name == name.lower() for name in SECRET_QUERY_PARAMETERS)


def test_stream_url_pattern_is_javascript_portable():
    # No named groups, lookaround, or unicode property classes: the
    # Portal frontend mirrors this pattern verbatim (Requirement 1.6).
    for unsupported in ("(?P<", "(?=", "(?!", "(?<", r"\p{", r"\A", r"\Z"):
        assert unsupported not in STREAM_URL_PATTERN
    re.compile(STREAM_URL_PATTERN)


# ---------------------------------------------------------------------------
# check_stream_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", VALID_RTSP_URLS)
def test_valid_rtsp_urls_are_accepted(url):
    assert check_stream_url(url, RTSP_SCHEMES) is None


@pytest.mark.parametrize("url", VALID_RTMP_URLS)
def test_valid_rtmp_urls_are_accepted(url):
    assert check_stream_url(url, RTMP_SCHEMES) is None


@pytest.mark.parametrize("url", VALID_RTSP_URLS + VALID_RTMP_URLS)
def test_every_accepted_url_matches_the_catalog_pattern(url):
    """Completeness: the checker is never looser than the catalog regex."""
    allowed = RTSP_SCHEMES if url.startswith("rtsp") else RTMP_SCHEMES
    assert check_stream_url(url, allowed) is None
    assert re.match(STREAM_URL_PATTERN, url) is not None


@pytest.mark.parametrize(
    "url,allowed,code",
    [
        ("rtmp://media.local/live/line1", RTSP_SCHEMES, "scheme_not_allowed"),
        ("rtsp://cam.local/live", RTMP_SCHEMES, "scheme_not_allowed"),
        ("http://cam.local/live", RTSP_SCHEMES, "scheme_not_allowed"),
        ("rtsp://user:pass@cam.local/live", RTSP_SCHEMES, "user_info"),
        ("rtmp://user@media.local/live", RTMP_SCHEMES, "user_info"),
        ("rtsp://cam.local/live?password=hunter2", RTSP_SCHEMES, "secret_query_parameter"),
        ("rtsp://cam.local/live?Token=abc", RTSP_SCHEMES, "secret_query_parameter"),
        ("rtmp://media.local/live?streamkey=abc", RTMP_SCHEMES, "secret_query_parameter"),
        ("rtsp://:554/live", RTSP_SCHEMES, "no_host"),
        ("rtsp://", RTSP_SCHEMES, "no_host"),
        ("RTSP://cam.local/live", RTSP_SCHEMES, "invalid_url"),
        ("rtsp://cam.local/live#frag", RTSP_SCHEMES, "invalid_url"),
        ("rtsp://cam.local/li ve", RTSP_SCHEMES, "invalid_url"),
        ("cam.local/live", RTSP_SCHEMES, "invalid_url"),
        ("", RTSP_SCHEMES, "invalid_url"),
        ("   ", RTSP_SCHEMES, "invalid_url"),
        (None, RTSP_SCHEMES, "invalid_url"),
        (12, RTSP_SCHEMES, "invalid_url"),
    ],
)
def test_rejected_urls_report_the_expected_code(url, allowed, code):
    problem = check_stream_url(url, allowed)
    assert isinstance(problem, StreamUrlProblem)
    assert problem.code == code
    assert problem.message


def test_scheme_problem_names_the_accepted_schemes():
    problem = check_stream_url("http://cam.local/live", RTSP_SCHEMES)
    assert "rtsp" in problem.message and "rtsps" in problem.message


def test_secret_query_problem_names_the_parameter_but_not_its_value():
    problem = check_stream_url("rtsp://cam.local/live?Token=sup3rs3cret", RTSP_SCHEMES)
    assert problem.code == "secret_query_parameter"
    assert "Token" in problem.message
    assert "sup3rs3cret" not in problem.message


def test_user_info_problem_never_echoes_the_credentials():
    problem = check_stream_url("rtsp://admin:sup3rs3cret@cam.local/live", RTSP_SCHEMES)
    assert problem.code == "user_info"
    assert "sup3rs3cret" not in problem.message
    assert "admin" not in problem.message


def test_secret_parameter_names_match_case_insensitively_as_whole_tokens():
    for name in sorted(SECRET_QUERY_PARAMETERS):
        url = "rtsp://cam.local/live?{0}=v".format(name.upper())
        assert check_stream_url(url, RTSP_SCHEMES).code == "secret_query_parameter"
    # A parameter that merely contains a secret name is not secret.
    assert check_stream_url("rtsp://cam.local/live?keyframe=1", RTSP_SCHEMES) is None
    assert check_stream_url("rtsp://cam.local/live?passthrough=1", RTSP_SCHEMES) is None


def test_allowed_schemes_accepts_any_iterable_case():
    assert check_stream_url("rtsp://cam/live", ["RTSP"]) is None
    assert check_stream_url("rtsp://cam/live", "rtsp") is None
    assert check_stream_url("rtsp://cam/live", ()).code == "scheme_not_allowed"


# ---------------------------------------------------------------------------
# normalize_stream_url (Requirement 10.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("RTSP://CAM.Local:554/Live?a=B", "rtsp://cam.local/Live?a=B"),
        ("rtsp://cam:554/x", "rtsp://cam/x"),
        ("rtsps://cam:322/x", "rtsps://cam/x"),
        ("rtmp://Cam:1935/x", "rtmp://cam/x"),
        ("rtmps://cam:443/x", "rtmps://cam/x"),
        ("rtsp://cam:1935/x", "rtsp://cam:1935/x"),
        ("rtsp://[2001:DB8::1]:554/x", "rtsp://[2001:db8::1]/x"),
        ("rtsp://cam:/x", "rtsp://cam/x"),
        ("  rtsp://cam/x  ", "rtsp://cam/x"),
        ("not a url", "not a url"),
    ],
)
def test_normalization_lowercases_and_drops_default_ports(url, expected):
    assert normalize_stream_url(url) == expected


def test_normalization_keeps_path_and_query_byte_for_byte():
    url = "rtsp://cam/Streaming/Channels/101?Profile=High&x=%2F"
    assert normalize_stream_url(url) == url


def test_normalization_is_idempotent_and_total():
    for url in VALID_RTSP_URLS + VALID_RTMP_URLS + ("nope", "", "rtsp://"):
        once = normalize_stream_url(url)
        assert normalize_stream_url(once) == once


def test_same_camera_iff_equal_normal_forms():
    assert normalize_stream_url("RTSP://Cam.Local:554/live") == normalize_stream_url(
        "rtsp://cam.local/live"
    )
    assert normalize_stream_url("rtsp://cam.local/live") != normalize_stream_url(
        "rtsp://cam.local/live2"
    )


# ---------------------------------------------------------------------------
# redact (Requirements 6.1, 6.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "connecting to rtsp://admin:s3cr3tpw@cam.local:554/live",
            "connecting to rtsp://***@cam.local:554/live",
        ),
        ("rtmp://user@media/live", "rtmp://***@media/live"),
        ("rtsp://a@b@host/live", "rtsp://***@host/live"),
        ("rtmp://media/live?streamkey=abcdef&x=1", "rtmp://media/live?streamkey=***&x=1"),
        ("password=hunter2", "password=***"),
        ("TOKEN=abc", "TOKEN=***"),
        ('password="hunter2"', 'password="***"'),
        # Secret-free text is preserved byte for byte.
        ("password=", "password="),
        ("rtsp://host/live and mail user@example.com", "rtsp://host/live and mail user@example.com"),
        ("keyframe=1 passthrough=2", "keyframe=1 passthrough=2"),
        ("plain text with no secrets at all", "plain text with no secrets at all"),
    ],
)
def test_redaction_masks_url_and_query_secrets(text, expected):
    assert redact(text) == expected


def test_redaction_masks_credential_literals_longest_first():
    secrets = ["s3cr3tpw", "s3cr3t"]
    assert redact("user s3cr3tpw and s3cr3t", secrets) == "user *** and ***"


def test_redaction_ignores_short_literals():
    assert redact("the cat sat", ["cat"]) == "the cat sat"


def test_redaction_is_idempotent():
    secrets = ["s3cr3tpw", "abcdef1234"]
    for text in (
        "rtsp://admin:s3cr3tpw@cam/live?token=abcdef1234",
        'password="s3cr3tpw" pwd=abcdef1234',
        "nothing to see here",
    ):
        once = redact(text, secrets)
        assert redact(once, secrets) == once
        assert "s3cr3tpw" not in once
        assert "abcdef1234" not in once


def test_redaction_is_total():
    assert redact("") == ""
    assert redact(None) is None
    assert redact(7) == 7


# ---------------------------------------------------------------------------
# compose_connect_url (Stream_Worker only)
# ---------------------------------------------------------------------------


def test_rtsp_connect_url_is_the_stream_url_unchanged():
    assert compose_connect_url("rtsp://cam/live", None, "admin", "pw", "RTSP") == "rtsp://cam/live"


def test_rtsp_connect_url_appends_the_secret_suffix():
    assert (
        compose_connect_url("rtsp://cam/live", "?auth=tok", "admin", "pw", "RTSP")
        == "rtsp://cam/live?auth=tok"
    )


def test_rtmp_connect_url_inserts_encoded_user_info():
    assert (
        compose_connect_url("rtmp://media/live/l1", None, "user name", "p@ss/word", "RTMP")
        == "rtmp://user%20name:p%40ss%2Fword@media/live/l1"
    )
    assert (
        compose_connect_url("rtmp://media/live/l1", None, "user", None, "RTMP")
        == "rtmp://user@media/live/l1"
    )
    assert compose_connect_url("rtmp://media/live/l1", None, None, None, "RTMP") == (
        "rtmp://media/live/l1"
    )


def test_rtmp_connect_url_appends_the_secret_suffix_as_one_query():
    assert (
        compose_connect_url("rtmp://media/live/l1?x=1", "?token=abc", None, None, "rtmp")
        == "rtmp://media/live/l1?x=1&token=abc"
    )
    assert (
        compose_connect_url("rtmp://media/live/l1", "/streamkey", None, None, "RTMP")
        == "rtmp://media/live/l1/streamkey"
    )


def test_compose_connect_url_rejects_unknown_protocol_and_malformed_url():
    with pytest.raises(ValueError):
        compose_connect_url("rtmp://media/x", None, None, None, "SRT")
    with pytest.raises(ValueError):
        compose_connect_url("not-a-url", None, None, None, "RTMP")


@pytest.mark.parametrize("port", ["\u00b2", "\u0663", "\uff15\uff15\uff14", "5\u00b2"])
def test_a_port_of_non_ascii_digits_is_rejected(port):
    """``str.isdigit`` is true for '²', '٣' and fullwidth digits, which no
    stream client takes as a port; only ASCII digits are a port."""
    url = "rtsp://10.0.0.5:{0}/live".format(port)
    problem = check_stream_url(url, ("rtsp",))
    assert problem is not None and problem.code == "invalid_url"
    # normalize_stream_url stays total on such input (int('²') raises).
    assert normalize_stream_url(url) == url
