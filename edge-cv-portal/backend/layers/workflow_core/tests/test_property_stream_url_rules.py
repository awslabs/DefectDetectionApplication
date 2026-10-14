# Feature: rtsp-rtmp-stream-cameras, Property 2: Stream_URL rules are sound and complete
"""Property test P2 — the Stream_URL rules are sound and complete.

**Feature: rtsp-rtmp-stream-cameras, Property 2: Stream_URL rules are sound and complete**

*For any* string and node type:

- ``check_stream_url`` accepts the string exactly when all of these hold:
  it parses, its scheme is allowed for the node type, its host is
  non-empty, it has no user information, and it has no
  Secret_Query_Parameter.
- The catalog regex accepts every URL ``check_stream_url`` accepts.
- The TypeScript port returns the same verdict and problem code. That
  clause belongs to the frontend mirror and is covered by task 11.2,
  over a fixture corpus generated from this module; this file pins the
  Python side, including the JavaScript-equivalent reading of the
  catalog regex (see ``test_catalog_regex_means_exactly_the_stream_url_shape``).

**Validates: Requirements 1.3, 2.1, 2.2, 4.2, 5.2, 9.4**

How the verdict is checked:

1. *Soundness.* ``_oracle_code`` is a second, independent expression of
   the requirement text — plain string operations, none of the module's
   regexes or helpers — that returns the expected problem code (or
   ``None``) for an arbitrary input. It encodes the precedence the
   design fixes: the specific codes (``scheme_not_allowed``,
   ``user_info``, ``no_host``, ``secret_query_parameter``) are reported
   in preference to the generic ``invalid_url``, so that an operator is
   told what to fix. Every generated input, structured or arbitrary, is
   compared against it.
2. *Completeness.* A separate strategy builds URLs only out of clean
   components (a lowercase stream scheme, a well-formed host, a numeric
   or absent port, a credential-free path, non-secret query names). The
   ground truth there is the generator's own intent, independent of both
   the oracle and the module: such a URL must be accepted for its own
   node type and rejected with ``scheme_not_allowed`` for the other one.
3. *Cross-validation.* Every URL accepted out of clean components is
   also, to ``urllib.parse``, a URL with the expected scheme, a
   non-empty host, no user information and no Secret_Query_Parameter.
   The stdlib parser is stricter than a Stream_URL needs to be about
   some authorities (bracketed hosts, NFKC-sensitive unicode); when it
   raises, it carries no information and the case is skipped.

The corpus spans the four stream schemes and foreign ones, mixed-case
schemes, host names, IPv4, bracketed IPv6, unicode hosts, default and
non-default and malformed ports, user information, secret and non-secret
query names in mixed case, unicode paths and values, fragments and
whitespace.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_core.stream_url import (
    SCHEMES_BY_NODE_TYPE,
    SCHEMES_BY_SOURCE_TYPE,
    SECRET_QUERY_PARAMETERS,
    STREAM_URL_PATTERN,
    check_stream_url,
)

CODE_INVALID_URL = "invalid_url"
CODE_SCHEME_NOT_ALLOWED = "scheme_not_allowed"
CODE_NO_HOST = "no_host"
CODE_USER_INFO = "user_info"
CODE_SECRET_QUERY_PARAMETER = "secret_query_parameter"

#: The four schemes a Stream_URL may use (Requirement 1.3, glossary).
STREAM_SCHEMES = ("rtsp", "rtsps", "rtmp", "rtmps")

#: Every key a caller passes as "the node type": the two
#: Stream_Camera_Source_Node types and the two Camera_Source types.
SCHEMES_BY_KEY = dict(SCHEMES_BY_NODE_TYPE)
SCHEMES_BY_KEY.update(SCHEMES_BY_SOURCE_TYPE)

#: The other key of the same family, used for the cross-type rejection.
OTHER_KEY = {
    "rtsp_camera_source": "rtmp_stream_source",
    "rtmp_stream_source": "rtsp_camera_source",
    "RTSP": "RTMP",
    "RTMP": "RTSP",
}

#: An RFC 3986 scheme: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ).
_ORACLE_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://")


# ---------------------------------------------------------------------------
# Oracle: the requirement text, re-derived
# ---------------------------------------------------------------------------


def _oracle_accepted_schemes(allowed_schemes) -> Tuple[str, ...]:
    """The accepted schemes, lowercased and de-duplicated."""
    if isinstance(allowed_schemes, str):
        candidates: Iterable = (allowed_schemes,)
    else:
        candidates = allowed_schemes
    accepted: List[str] = []
    for scheme in candidates:
        if isinstance(scheme, str) and scheme.strip().lower():
            lowered = scheme.strip().lower()
            if lowered not in accepted:
                accepted.append(lowered)
    return tuple(accepted)


def _oracle_host_and_port(authority: str) -> Tuple[str, Optional[str]]:
    """Split an authority that carries no user information into host and port.

    A Stream_URL has "a host, and an optional port". The bracketed IPv6
    form (``[::1]:554``) keeps its brackets; an authority that opens a
    bracket it never closes is taken as one host, so that nothing inside
    it is mistaken for a port.
    """
    if authority.startswith("["):
        closing = authority.find("]")
        if closing == -1:
            return authority, None
        host = authority[: closing + 1]
        tail = authority[closing + 1 :]
        if tail.startswith(":"):
            return host, tail[1:]
        return authority, None
    if ":" in authority:
        host, _, port = authority.rpartition(":")
        return host, port
    return authority, None


def _oracle_secret_query_name(query: str) -> Optional[str]:
    """The first Secret_Query_Parameter name in ``query``, if any."""
    for pair in re.split(r"[&;]", query):
        name = pair.split("=", 1)[0].strip()
        if name and name.lower() in SECRET_QUERY_PARAMETERS:
            return name
    return None


def _is_stream_url_shape(url) -> bool:
    """Whether ``url`` has the Stream_URL shape, spelled out.

    A lowercase stream scheme, ``://``, a non-empty authority that holds
    no whitespace and none of ``/``, ``@``, ``?`` or ``#``, then either
    nothing or a path/query that starts with ``/`` or ``?`` and holds no
    ``#`` and no whitespace.

    This is what ``STREAM_URL_PATTERN`` says in words; a test below pins
    the regex to it.
    """
    if not isinstance(url, str):
        return False
    match = _ORACLE_SCHEME_RE.match(url)
    if match is None:
        return False
    if match.group(1) not in STREAM_SCHEMES:  # lowercase is implied
        return False
    rest = url[match.end() :]
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not authority or "@" in authority or re.search(r"\s", authority):
        return False
    remainder = rest[len(authority) :]
    if remainder == "":
        return True
    if remainder[0] not in "/?":
        return False
    return "#" not in remainder and re.search(r"\s", remainder) is None


def _oracle_code(url, allowed_schemes) -> Optional[str]:
    """The expected ``check_stream_url`` problem code, or ``None``.

    The five rejection reasons of Requirement 1.3 / 2.1 / 2.2, in the
    order the design reports them.
    """
    accepted = _oracle_accepted_schemes(allowed_schemes)

    if not isinstance(url, str) or not url.strip():
        return CODE_INVALID_URL

    match = _ORACLE_SCHEME_RE.match(url)
    if match is None:
        return CODE_INVALID_URL
    scheme = match.group(1)
    rest = url[match.end() :]

    if scheme.lower() not in accepted:
        return CODE_SCHEME_NOT_ALLOWED

    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in authority:
        return CODE_USER_INFO

    host, port = _oracle_host_and_port(authority)
    if not host:
        return CODE_NO_HOST

    remainder = rest[len(authority) :]
    query = remainder.partition("?")[2].partition("#")[0] if "?" in remainder else ""
    if _oracle_secret_query_name(query) is not None:
        return CODE_SECRET_QUERY_PARAMETER

    if not _is_stream_url_shape(url):
        return CODE_INVALID_URL
    # ASCII digits only ('²' and '٣' are str.isdigit() but are no port).
    if port not in (None, "") and not all("0" <= c <= "9" for c in port):
        return CODE_INVALID_URL
    return None


def _code_of(problem) -> Optional[str]:
    return None if problem is None else problem.code


def _catalog_regex_accepts(url: str) -> bool:
    """Exactly what the catalog constraint does with ``regex``.

    ``workflow_core.validator.parameters`` applies a string parameter's
    ``regex`` constraint with ``re.search``; the pattern carries its own
    anchors.
    """
    return re.search(STREAM_URL_PATTERN, url) is not None


def _javascript_regex_accepts(url: str) -> bool:
    """What the frontend mirror's ``new RegExp(pattern).test(value)`` does.

    JavaScript's ``$`` matches only at the end of the input, while
    Python's also matches just before a trailing newline; ``fullmatch``
    is the Python spelling of the JavaScript verdict.
    """
    return re.fullmatch(STREAM_URL_PATTERN, url) is not None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_CLEAN_HOSTS = (
    "cam.local",
    "CAM.Local",
    "192.168.1.64",
    "10.0.0.5",
    "media-1.plant.example.com",
    "cam",
    "[2001:db8::1]",
    "[::1]",
    "[fe80::1]",
    "café.local",
    "摄像机.local",
)

_ODD_AUTHORITIES = (
    "",
    ":",
    "[::1",
    "cam:554:extra",
    "@",
    "cam@",
    "us er",
    "::1",
)

_PORTS = (None, "", "554", "322", "1935", "443", "8554", "0")
_ODD_PORTS = ("abc", "554x", ":", "-1")

_CLEAN_PATHS = (
    "",
    "/",
    "/live",
    "/Streaming/Channels/101",
    "/ünicode/路径",
    "/pa@th",
    "/live/",
    "/a%20b",
)

#: Query names that merely resemble a Secret_Query_Parameter, plus
#: ordinary stream parameters. None is a whole-token match, so none of
#: them is secret.
_PLAIN_QUERY_NAMES = (
    "profile",
    "channel",
    "keyframe",
    "passthrough",
    "authorization",
    "stream_id",
    "subtype",
    "transport",
    "x",
)

_SECRET_QUERY_NAMES = tuple(
    sorted(SECRET_QUERY_PARAMETERS)
    + [name.upper() for name in sorted(SECRET_QUERY_PARAMETERS)]
    + [name.capitalize() for name in sorted(SECRET_QUERY_PARAMETERS)]
)

#: Values are prefixed with '~', a character no problem message contains,
#: so that "the message never echoes the value" can be asserted without
#: a generated value coincidentally being a word of the message.
_VALUES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_é路",
    min_size=0,
    max_size=12,
).map(lambda token: "~" + token)

_SUFFIXES = ("", "#frag", " ", "\n", "\t", "#")

_KEYS = tuple(SCHEMES_BY_KEY)


def _render_query(pairs) -> str:
    if not pairs:
        return ""
    return "?" + "&".join(
        name if value is None else "{0}={1}".format(name, value) for name, value in pairs
    )


@st.composite
def _clean_stream_urls(draw):
    """A URL built only out of clean components, with its node type.

    Returns ``(key, url)``, where ``key`` names the node type or
    Camera_Source type whose accepted schemes the URL's scheme belongs
    to. By construction the URL parses, has a host, carries no user
    information and has no Secret_Query_Parameter, so it must be
    accepted for ``key``.
    """
    key = draw(st.sampled_from(_KEYS))
    scheme = draw(st.sampled_from(SCHEMES_BY_KEY[key]))
    host = draw(st.sampled_from(_CLEAN_HOSTS))
    port = draw(st.sampled_from(_PORTS))
    path = draw(st.sampled_from(_CLEAN_PATHS))
    pairs = draw(
        st.lists(
            st.tuples(st.sampled_from(_PLAIN_QUERY_NAMES), st.one_of(st.none(), _VALUES)),
            max_size=3,
        )
    )
    url = "{0}://{1}{2}{3}{4}".format(
        scheme,
        host,
        "" if port is None else ":" + port,
        path,
        _render_query(pairs),
    )
    return key, url


@st.composite
def _wide_stream_urls(draw):
    """A URL assembled from every component pool, clean or not."""
    scheme = draw(
        st.sampled_from(
            STREAM_SCHEMES
            + ("http", "https", "srt", "file", "rtspu", "rt+sp")
            + ("RTSP", "Rtsp", "RTMP", "rtMps", "RTSPS")
        )
    )
    user_info = draw(
        st.one_of(
            st.none(),
            st.builds(
                lambda user, password: user if password is None else user + ":" + password,
                st.sampled_from(("~admin", "~oper", "")),
                st.one_of(st.none(), _VALUES),
            ),
        )
    )
    authority = draw(
        st.one_of(
            st.builds(
                lambda host, port: host if port is None else host + ":" + port,
                st.sampled_from(_CLEAN_HOSTS),
                st.sampled_from(_PORTS + _ODD_PORTS),
            ),
            st.sampled_from(_ODD_AUTHORITIES),
        )
    )
    path = draw(st.sampled_from(_CLEAN_PATHS))
    pairs = draw(
        st.lists(
            st.tuples(
                st.sampled_from(_PLAIN_QUERY_NAMES + _SECRET_QUERY_NAMES),
                st.one_of(st.none(), _VALUES),
            ),
            max_size=3,
        )
    )
    suffix = draw(st.sampled_from(_SUFFIXES))
    url = "{0}://{1}{2}{3}{4}{5}".format(
        scheme,
        "" if user_info is None else user_info + "@",
        authority,
        path,
        _render_query(pairs),
        suffix,
    )
    return url


#: Arbitrary text over a URL-ish alphabet, plus a stream-scheme prefix
#: half of the time, plus the non-string values an API caller can pass.
_ARBITRARY_INPUTS = st.one_of(
    st.none(),
    st.integers(),
    st.booleans(),
    st.text(max_size=12),
    st.text(alphabet="rtspmRTSPM:/@?#&=.[]0154 \tabcé~;%-_", max_size=24),
    st.builds(
        lambda prefix, tail: prefix + tail,
        st.sampled_from(("rtsp://", "rtsps://", "rtmp://", "RTSP://", "x://", "rtsp:/", "//")),
        st.text(alphabet="camlive:/@?#&=.[]0154 \té~;%", max_size=16),
    ),
)

_ALLOWED_SCHEME_SETS = st.one_of(
    st.sampled_from([SCHEMES_BY_KEY[key] for key in _KEYS]),
    st.just(()),
    st.just("rtsp"),
    st.just(("RTSP", "rtsps")),
)


# ---------------------------------------------------------------------------
# Completeness: a URL of clean components is accepted for its node type
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_clean_stream_urls())
def test_clean_stream_urls_are_accepted_for_their_node_type(case):
    """**Property 2** (completeness, ground truth by construction).

    A URL whose scheme, host, port, path and query are all clean is a
    Stream_URL: ``check_stream_url`` accepts it for the node type whose
    accepted schemes contain its scheme, and both readings of the
    catalog regex accept it (Requirements 1.3, 2.1, 2.2).
    """
    key, url = case
    assert check_stream_url(url, SCHEMES_BY_KEY[key]) is None, url
    assert _catalog_regex_accepts(url), url
    assert _javascript_regex_accepts(url), url


@settings(max_examples=100)
@given(_clean_stream_urls())
def test_clean_stream_urls_are_rejected_for_the_other_node_type(case):
    """**Property 2** (scheme rule, Requirement 2.1).

    The same URL offered to the other type of the family is rejected as
    ``scheme_not_allowed``, and the message names the accepted schemes
    of that type.
    """
    key, url = case
    other = OTHER_KEY[key]
    problem = check_stream_url(url, SCHEMES_BY_KEY[other])
    assert problem is not None and problem.code == CODE_SCHEME_NOT_ALLOWED, url
    for scheme in SCHEMES_BY_KEY[other]:
        assert scheme in problem.message


@settings(max_examples=100)
@given(_clean_stream_urls())
def test_accepted_urls_agree_with_the_stdlib_parser(case):
    """**Property 2** (cross-validation with ``urllib.parse``).

    An accepted URL is, to the standard library, a URL with the expected
    scheme, a non-empty host, no user information and no
    Secret_Query_Parameter. ``urllib.parse`` is stricter than a
    Stream_URL needs to be about some authorities; when it raises it
    carries no information and the case is skipped.
    """
    key, url = case
    assume(check_stream_url(url, SCHEMES_BY_KEY[key]) is None)
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        username = parts.username
    except ValueError:
        assume(False)
        return
    assert parts.scheme in SCHEMES_BY_KEY[key]
    assert hostname, url
    assert username is None, url
    for name, _value in parse_qsl(parts.query, keep_blank_values=True):
        assert name.lower() not in SECRET_QUERY_PARAMETERS, url


# ---------------------------------------------------------------------------
# Soundness: every verdict matches the oracle
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_wide_stream_urls(), _ALLOWED_SCHEME_SETS)
def test_verdict_matches_the_oracle_over_assembled_urls(url, allowed_schemes):
    """**Property 2** (soundness over the assembled corpus).

    For every URL assembled from the component pools and every set of
    accepted schemes, the problem code equals the oracle's
    (Requirements 1.3, 2.1, 2.2, 4.2, 5.2, 9.4).
    """
    assert _code_of(check_stream_url(url, allowed_schemes)) == _oracle_code(url, allowed_schemes), (
        url,
        allowed_schemes,
    )


@settings(max_examples=100)
@given(_ARBITRARY_INPUTS, _ALLOWED_SCHEME_SETS)
def test_verdict_matches_the_oracle_over_arbitrary_input(url, allowed_schemes):
    """**Property 2** (soundness over arbitrary input).

    The same holds for arbitrary text and for the non-string values an
    API caller can pass.
    """
    assert _code_of(check_stream_url(url, allowed_schemes)) == _oracle_code(url, allowed_schemes), (
        url,
        allowed_schemes,
    )


@settings(max_examples=100)
@given(
    st.one_of(_wide_stream_urls(), _ARBITRARY_INPUTS, _clean_stream_urls().map(lambda case: case[1])),
    _ALLOWED_SCHEME_SETS,
)
def test_the_catalog_regex_accepts_every_accepted_url(url, allowed_schemes):
    """**Property 2** (the checker is never looser than the catalog).

    Whatever ``check_stream_url`` accepts, the catalog ``url`` constraint
    accepts — under the catalog's own ``re.search`` application and under
    the JavaScript reading the frontend mirror uses (Requirement 1.3).
    """
    if check_stream_url(url, allowed_schemes) is not None:
        return
    assert isinstance(url, str)
    assert _catalog_regex_accepts(url), url
    assert _javascript_regex_accepts(url), url


@settings(max_examples=100)
@given(st.one_of(_wide_stream_urls(), _ARBITRARY_INPUTS.filter(lambda v: isinstance(v, str))))
def test_catalog_regex_means_exactly_the_stream_url_shape(url):
    """**Property 2** (the shared pattern says what the design says).

    ``STREAM_URL_PATTERN`` accepts exactly the strings with the
    Stream_URL shape: a lowercase stream scheme, a non-empty
    credential-free authority, then an optional path or query with no
    fragment and no whitespace. The JavaScript reading is the exact one;
    Python's ``re.search`` differs only in tolerating a single trailing
    newline, which the shape rule rejects.
    """
    assert _javascript_regex_accepts(url) == _is_stream_url_shape(url), url
    if _catalog_regex_accepts(url) and not _javascript_regex_accepts(url):
        assert url.endswith("\n") and _javascript_regex_accepts(url[:-1]), url


# ---------------------------------------------------------------------------
# Confidentiality: a problem message names the part, never the value
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    st.sampled_from(_KEYS),
    st.sampled_from(_CLEAN_HOSTS),
    st.sampled_from(_CLEAN_PATHS),
    st.sampled_from(_SECRET_QUERY_NAMES),
    _VALUES,
    st.one_of(st.none(), _VALUES),
    st.sampled_from(("~admin", "~oper")),
)
def test_problem_messages_name_the_part_but_never_its_value(
    key, host, path, secret_name, secret_value, password, user
):
    """**Property 2** (Requirements 2.2, 5.2: never echo a secret).

    A URL carrying a Secret_Query_Parameter, or user information, or
    both, is rejected with a message that names the offending parameter
    but contains neither its value nor the user information. Generated
    values start with '~', which no message contains, so the assertion
    cannot fail on a coincidental word of the message.
    """
    scheme = SCHEMES_BY_KEY[key][0]
    credentials = user if password is None else user + ":" + password
    for embed_user_info in (False, True):
        url = "{0}://{1}{2}{3}?{4}={5}".format(
            scheme,
            credentials + "@" if embed_user_info else "",
            host,
            path,
            secret_name,
            secret_value,
        )
        problem = check_stream_url(url, SCHEMES_BY_KEY[key])
        assert problem is not None
        assert secret_value not in problem.message
        assert user not in problem.message
        if password is not None:
            assert password not in problem.message
        if embed_user_info:
            assert problem.code == CODE_USER_INFO
        else:
            assert problem.code == CODE_SECRET_QUERY_PARAMETER
            assert secret_name in problem.message
