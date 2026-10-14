# Feature: rtsp-rtmp-stream-cameras, Property 3: Redaction removes every secret and preserves secret-free text
"""Property test P3 — redaction removes every secret, and only secrets.

**Feature: rtsp-rtmp-stream-cameras, Property 3: Redaction removes every
secret and preserves secret-free text**

*For any* text and set of secret values:

- ``redact`` returns text that contains no secret value, no URL user
  information, and no Secret_Query_Parameter value.
- Applying ``redact`` twice equals applying it once.
- Text that contains none of these comes back unchanged.

**Validates: Requirements 6.1, 6.3**

``redact`` is the one masking rule the LocalServer's redaction filter
applies to every log record (Requirement 6.3), so its three kinds of
secret are exactly the three kinds Requirement 6.1 keeps out of logs:
URL user information, the value of a Secret_Query_Parameter, and every
value held in the Credential_Store (passed in as ``secrets``).

How each clause is checked:

1. *Nothing survives.* A structured generator plants known secrets —
   credentialed URLs, ``name=value`` Secret_Query_Parameters, and
   Credential_Store literals quoted in prose — in surroundings drawn
   from benign log lines and arbitrary text. Every planted value is
   marked with a distinctive prefix (``u5r~``, ``p4s~``, ``q7v~``,
   ``s3c~``) that nothing else in the corpus produces, so "the value is
   absent from the output" is a real statement about masking and cannot
   pass or fail by coincidence.
2. *Nothing is left behind.* ``_residual_secrets`` re-derives the three
   rules from the requirement text with plain string scanning — none of
   the module's regexes or helpers — and is applied to the **output**:
   an output may hold no ``scheme://user@`` authority other than the
   mask, no Secret_Query_Parameter assignment whose value is not the
   mask, and no Credential_Store literal. This catches secrets the
   generator did not plant deliberately, including ones created by the
   interaction of the three rules.
3. *Idempotence* is checked over the same corpus, plus a corpus built
   specifically to make the rules interact: a Credential_Store literal
   glued directly onto a Secret_Query_Parameter name, so that masking
   the literal is what exposes the assignment.
4. *Preservation* uses a separate corpus of secret-free text — benign
   log lines, credential-free stream URLs, arbitrary text — cross-checked
   against the same re-derived rules, and asserts the output is the input
   byte for byte.
5. *End to end*: the URL a Stream_Worker actually connects with
   (``compose_connect_url``, the only place credentials enter a URL) is
   passed through ``redact`` with the Credential_Store values, and must
   come back carrying neither the raw nor the percent-encoded
   credential.

Two deliberate scope limits of the corpus, both of them properties of the
rules rather than of the test:

- Generated Credential_Store literals never contain ``*``, since a
  literal that contains the mask is indistinguishable from an already
  masked output.
- Generated URL user information is restricted to characters RFC 3986
  allows there (no ``/``, ``?``, ``#`` or whitespace), because the rule
  is about URL user information; a ``/`` inside the user information
  ends the authority and there is no URL left to speak of.
"""

from __future__ import annotations

import string
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_core.stream_url import (
    SECRET_QUERY_PARAMETERS,
    compose_connect_url,
    redact,
)

#: The replacement every masking rule writes.
MASK = "***"

#: Shortest Credential_Store literal the rule masks; shorter values are
#: too common in ordinary text to mask without destroying the message.
MIN_SECRET_LENGTH = 4

#: A masked value, in each spelling the rule can produce (a quoted value
#: keeps its quotes, which is what makes the rule idempotent).
_MASKED_VALUES = frozenset({MASK, '"' + MASK + '"', "'" + MASK + "'"})

#: The characters that block a parameter name from being a whole token
#: (the rule's ``(?<![A-Za-z0-9_])`` boundary, re-derived).
_NAME_BOUNDARY_CHARS = frozenset(string.ascii_letters + string.digits + "_")

#: RFC 3986 scheme characters: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ).
_SCHEME_CHARS = frozenset(string.ascii_letters + string.digits + "+-.")

#: What ends a URL authority.
_AUTHORITY_STOP_CHARS = frozenset("/?#")

#: What ends an unquoted parameter value.
_VALUE_STOP_CHARS = frozenset("&;,)\"']}")

#: Secret_Query_Parameter names, longest first, which is the order the
#: rule resolves overlaps in (``stream_key`` before ``key``).
_NAMES_LONGEST_FIRST: Tuple[str, ...] = tuple(
    sorted(SECRET_QUERY_PARAMETERS, key=lambda name: (-len(name), name))
)


# ---------------------------------------------------------------------------
# Oracle: the three masking rules, re-derived from the requirement text
# ---------------------------------------------------------------------------


def _user_information_spans(text: str) -> List[str]:
    """The URL user information parts of ``text``, in order.

    A part is the text between ``<scheme>://`` and the last ``@`` of the
    authority that follows it. The authority ends at the first ``/``,
    ``?``, ``#`` or whitespace, so this never reaches into a path, a
    query or the next word. Scanning is left to right and
    non-overlapping, like the substitution it mirrors.
    """
    found: List[str] = []
    length = len(text)
    cursor = 0
    while True:
        marker = text.find("://", cursor)
        if marker == -1:
            return found
        scheme_start = marker
        while scheme_start > 0 and text[scheme_start - 1] in _SCHEME_CHARS:
            scheme_start -= 1
        scheme = text[scheme_start:marker]
        cursor = marker + 3
        if not any(character in string.ascii_letters for character in scheme):
            continue
        end = cursor
        while end < length and text[end] not in _AUTHORITY_STOP_CHARS and not text[end].isspace():
            end += 1
        authority = text[cursor:end]
        at_sign = authority.rfind("@")
        if at_sign < 1:
            continue
        found.append(authority[:at_sign])
        cursor = cursor + at_sign + 1


def _parse_assignment(text: str, start: int) -> Optional[Tuple[str, int]]:
    """Parse ``[ws] '=' [ws] value`` at ``start``; ``None`` when absent.

    A quoted value keeps its quotes and must be closed; an unquoted value
    runs to the first whitespace or delimiter and must be non-empty, so
    that an empty assignment (``password=``) carries no secret.
    """
    length = len(text)
    cursor = start
    while cursor < length and text[cursor].isspace():
        cursor += 1
    if cursor >= length or text[cursor] != "=":
        return None
    cursor += 1
    while cursor < length and text[cursor].isspace():
        cursor += 1
    if cursor >= length:
        return None
    quote_character = text[cursor]
    if quote_character in "\"'":
        closing = text.find(quote_character, cursor + 1)
        if closing == -1:
            return None
        return text[cursor : closing + 1], closing + 1
    end = cursor
    while end < length and text[end] not in _VALUE_STOP_CHARS and not text[end].isspace():
        end += 1
    if end == cursor:
        return None
    return text[cursor:end], end


def _secret_assignments(text: str) -> List[Tuple[str, str]]:
    """The ``(name, value)`` Secret_Query_Parameter assignments in ``text``.

    The name must be a whole token, compared case-insensitively, and the
    value must be non-empty. Overlapping names resolve longest first.
    """
    found: List[Tuple[str, str]] = []
    length = len(text)
    index = 0
    while index < length:
        if index > 0 and text[index - 1] in _NAME_BOUNDARY_CHARS:
            index += 1
            continue
        matched: Optional[Tuple[str, str, int]] = None
        for name in _NAMES_LONGEST_FIRST:
            if text[index : index + len(name)].lower() != name:
                continue
            parsed = _parse_assignment(text, index + len(name))
            if parsed is None:
                continue
            value, end = parsed
            matched = (text[index : index + len(name)], value, end)
            break
        if matched is None:
            index += 1
            continue
        found.append((matched[0], matched[1]))
        index = matched[2]
    return found


def _maskable_literals(secrets: Iterable[Any]) -> List[str]:
    """The literals the rule masks: strings of four characters or more."""
    return [
        secret
        for secret in (secrets or ())
        if isinstance(secret, str) and len(secret) >= MIN_SECRET_LENGTH and secret != MASK
    ]


def _residual_secrets(text: Any, secrets: Iterable[Any] = ()) -> List[str]:
    """Everything in ``text`` that Requirement 6.1 forbids, described.

    An empty list means the text carries no URL user information, no
    Secret_Query_Parameter value and no Credential_Store literal. Used
    on redaction output, where the mask itself is the one allowed
    remnant.
    """
    if not isinstance(text, str):
        return []
    problems: List[str] = []
    for user_information in _user_information_spans(text):
        if user_information != MASK:
            problems.append("user information {0!r}".format(user_information))
    for name, value in _secret_assignments(text):
        if value not in _MASKED_VALUES:
            problems.append("value of parameter {0!r}".format(name))
    for literal in _maskable_literals(secrets):
        if literal in text:
            problems.append("credential literal {0!r}".format(literal))
    return problems


def _carries_secret(text: Any, secrets: Iterable[Any] = ()) -> bool:
    """Whether ``text`` holds any of the three kinds of secret."""
    return bool(_residual_secrets(text, secrets))


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

#: Alphabets deliberately exclude '*' (a literal containing the mask is
#: indistinguishable from masked output), whitespace, and the characters
#: that end an authority.
_CREDENTIAL_TAIL = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.%~é",
    min_size=0,
    max_size=10,
)

#: Marked secret material: a user name, a password, a Secret_Query_Parameter
#: value and a Credential_Store literal. The prefixes appear nowhere else
#: in the corpus, so "absent from the output" means "masked".
_USER_NAMES = _CREDENTIAL_TAIL.map(lambda tail: "u5r~" + tail)
_PASSWORDS = _CREDENTIAL_TAIL.map(lambda tail: "p4s~" + tail)
_QUERY_VALUES = _CREDENTIAL_TAIL.map(lambda tail: "q7v~" + tail)
_STORE_LITERALS = _CREDENTIAL_TAIL.map(lambda tail: "s3c~" + tail)

#: Secret_Query_Parameter names in the three cases an operator writes.
_SECRET_NAMES = tuple(
    sorted(SECRET_QUERY_PARAMETERS)
    + [name.upper() for name in sorted(SECRET_QUERY_PARAMETERS)]
    + [name.capitalize() for name in sorted(SECRET_QUERY_PARAMETERS)]
)

_SCHEMES = ("rtsp", "rtsps", "rtmp", "rtmps", "http", "https")

_HOSTS = (
    "cam.local",
    "cam.local:554",
    "192.168.1.64",
    "192.168.1.64:8554",
    "[fd00::1]",
    "[fd00::1]:322",
    "media.local:1935",
)

_PATHS = ("", "/live", "/Streaming/Channels/101", "/live/line1")

#: Log lines a LocalServer actually writes, none of which carries a
#: secret: no 'scheme://user@' authority and no Secret_Query_Parameter
#: assignment. 'keyframe interval=30' and 'passed the healthcheck' are
#: here on purpose: they contain the parameter names 'key' and 'pass'
#: without being assignments of them.
_BENIGN_FRAGMENTS = (
    "",
    "session opened",
    "reconnecting to camera",
    "keyframe interval=30",
    "state=connected",
    "user=operator",
    "GST_DEBUG=2",
    "stream health degraded, restarting the worker",
    "decoder=nvv4l2decoder",
    "passed the healthcheck",
    "rtsp://cam.local:554/Streaming/Channels/101",
    "rtmp://media.local:1935/live/line1?profile=main",
    "password=",
    "token requested for the session",
    "credentials rotated",
)

#: Arbitrary text, and text over a URL-ish alphabet so that the shapes
#: the rules look for turn up by chance as well as by construction.
_ARBITRARY_TEXT = st.one_of(
    st.text(max_size=16),
    st.text(alphabet="rtspmhRTSP:/@?#&=.[]0154 \t\nabcépasswordtokenkey'\"~;%-_*", max_size=32),
)


def _render_value(value: str, quote_character: str) -> str:
    if quote_character:
        return quote_character + value + quote_character
    return value


@st.composite
def _secret_bearing_texts(draw):
    """``(text, secrets, planted)``: text with known secrets in it.

    ``secrets`` is what the Credential_Store would hand the redaction
    filter; ``planted`` is every secret value that must not survive.
    """
    parts: List[str] = []
    secrets: List[str] = []
    planted: List[str] = []

    draw_url_credentials = draw(st.booleans())
    draw_query_secret = draw(st.booleans())
    literals = draw(st.lists(_STORE_LITERALS, max_size=2))
    # At least one kind of secret, so the corpus always has something to mask.
    if not (draw_url_credentials or draw_query_secret or literals):
        draw_url_credentials = True

    if draw_url_credentials:
        user = draw(_USER_NAMES)
        password = draw(st.one_of(st.none(), _PASSWORDS))
        credentials = user if password is None else user + ":" + password
        parts.append(
            "connecting to {0}://{1}@{2}{3}".format(
                draw(st.sampled_from(_SCHEMES)),
                credentials,
                draw(st.sampled_from(_HOSTS)),
                draw(st.sampled_from(_PATHS)),
            )
        )
        planted.append(user)
        if password is not None:
            planted.append(password)
            # The Credential_Store holds the password about half the time;
            # the user-information rule must not depend on it doing so.
            if draw(st.booleans()):
                secrets.append(password)

    if draw_query_secret:
        name = draw(st.sampled_from(_SECRET_NAMES))
        value = draw(_QUERY_VALUES)
        quote_character = draw(st.sampled_from(("", '"', "'")))
        separator = draw(st.sampled_from(("?", "&", " ", ", ", "; ")))
        spacing = draw(st.sampled_from(("=", " = ", "= ")))
        parts.append(
            "{0}://{1}{2}{3}{4}{5}{6}".format(
                draw(st.sampled_from(_SCHEMES)),
                draw(st.sampled_from(_HOSTS)),
                draw(st.sampled_from(_PATHS)),
                separator,
                name,
                spacing,
                _render_value(value, quote_character),
            )
        )
        planted.append(value)

    for literal in literals:
        secrets.append(literal)
        planted.append(literal)
        parts.append(
            draw(
                st.sampled_from(
                    (
                        "credential '{0}' loaded",
                        "using {0} for the session",
                        "{0}",
                        "store=[{0}]",
                    )
                )
            ).format(literal)
        )

    # Secrets the store holds that are not in the text at all, plus the
    # non-string entries a caller can pass.
    secrets.extend(draw(st.lists(_STORE_LITERALS.map(lambda value: "n0t~" + value), max_size=2)))
    if draw(st.booleans()):
        secrets.extend([None, 12, MASK, "ab"])

    noise = draw(st.lists(st.sampled_from(_BENIGN_FRAGMENTS), max_size=3))
    if draw(st.booleans()):
        noise.append(draw(_ARBITRARY_TEXT))
    parts.extend(noise)
    order = draw(st.permutations(list(range(len(parts)))))
    joiner = draw(st.sampled_from((" ", " | ", "\n", "; ")))
    text = joiner.join(parts[index] for index in order)
    return text, tuple(secrets), tuple(planted)


@st.composite
def _adjacent_literal_texts(draw):
    """A Credential_Store literal glued onto a parameter name.

    Masking the literal is what turns the rest into a
    Secret_Query_Parameter assignment, so this is the corpus where the
    three rules interact: ``s3c~xtoken=q7v~y`` holds no assignment of
    ``token`` (the name is ``s3c~xtoken``), but ``***token=q7v~y`` does.
    Nothing is claimed to be planted here beyond the literal itself; the
    point is that the output must still be a fixed point.
    """
    literal = draw(_STORE_LITERALS)
    name = draw(st.sampled_from(_SECRET_NAMES))
    value = draw(_QUERY_VALUES)
    prefix = draw(st.sampled_from(("", "worker ", "rtsp://cam.local/live?")))
    text = "{0}{1}{2}={3}".format(prefix, literal, name, value)
    return text, (literal,), (literal,)


@st.composite
def _arbitrary_texts(draw):
    """Arbitrary text with an arbitrary set of store literals."""
    text = draw(_ARBITRARY_TEXT)
    secrets = draw(
        st.lists(
            st.one_of(
                _STORE_LITERALS,
                st.sampled_from(("password", "token", "abcd", "ab", MASK, "@", "rtsp://")),
                st.text(max_size=8),
            ),
            max_size=3,
        )
    )
    return text, tuple(secrets), ()


def _wide_texts():
    """Every corpus, as ``(text, secrets, planted)`` triples."""
    return st.one_of(_secret_bearing_texts(), _adjacent_literal_texts(), _arbitrary_texts())


@st.composite
def _secret_free_texts(draw):
    """Text that carries none of the three kinds of secret.

    Built from benign log lines, credential-free stream URLs and
    arbitrary text, then filtered through the re-derived rules so that
    "secret-free" is an independent judgement rather than a promise of
    the generator.
    """
    parts = draw(st.lists(st.sampled_from(_BENIGN_FRAGMENTS), min_size=1, max_size=4))
    if draw(st.booleans()):
        parts.append(
            "{0}://{1}{2}".format(
                draw(st.sampled_from(_SCHEMES)),
                draw(st.sampled_from(_HOSTS)),
                draw(st.sampled_from(_PATHS)),
            )
        )
    if draw(st.booleans()):
        parts.append(draw(_ARBITRARY_TEXT))
    text = draw(st.sampled_from((" ", " | ", "\n", "; "))).join(parts)
    # Store literals that are not in the text.
    secrets = tuple(
        "n0t~" + tail for tail in draw(st.lists(_CREDENTIAL_TAIL, max_size=2))
    )
    assume(not _carries_secret(text, secrets))
    return text, secrets


# ---------------------------------------------------------------------------
# Clause 1: no secret survives
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_secret_bearing_texts())
def test_planted_secrets_never_survive_redaction(case):
    """**Property 3** (clause 1, Requirements 6.1, 6.3).

    Every secret planted in the text — URL user information, a
    Secret_Query_Parameter value, a Credential_Store literal — is absent
    from the redacted text, and the redacted text holds no residual
    secret of any of the three kinds.
    """
    text, secrets, planted = case
    output = redact(text, secrets)
    for value in planted:
        assert value not in output, (value, text, output)
    assert _residual_secrets(output, secrets) == [], (text, output)


@settings(max_examples=100)
@given(_wide_texts())
def test_redaction_leaves_no_residual_secret(case):
    """**Property 3** (clause 1 over the whole corpus, Requirement 6.1).

    Whatever the input — planted secrets, secrets that only appear once
    another rule has fired, or arbitrary text — the output carries no
    URL user information other than the mask, no Secret_Query_Parameter
    value other than the mask, and no Credential_Store literal.
    """
    text, secrets, _planted = case
    output = redact(text, secrets)
    assert _residual_secrets(output, secrets) == [], (text, output)


# ---------------------------------------------------------------------------
# Clause 2: idempotence
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_wide_texts())
def test_redaction_is_idempotent(case):
    """**Property 3** (clause 2, Requirement 6.3).

    Applying ``redact`` twice equals applying it once, so a log record
    that passes through the filter more than once is not mangled further.
    """
    text, secrets, _planted = case
    once = redact(text, secrets)
    assert redact(once, secrets) == once, (text, once)


@settings(max_examples=100)
@given(_wide_texts())
def test_redaction_of_masked_text_is_stable_without_the_store(case):
    """**Property 3** (clause 2, without the Credential_Store).

    Redacted text is a fixed point of the two structural rules on their
    own, so a downstream handler that no longer has the store's values
    cannot change it either.
    """
    text, secrets, _planted = case
    once = redact(text, secrets)
    assert redact(once) == once, (text, once)


# ---------------------------------------------------------------------------
# Clause 3: secret-free text is preserved
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_secret_free_texts())
def test_secret_free_text_is_returned_unchanged(case):
    """**Property 3** (clause 3, Requirement 6.3).

    Text with no URL user information, no Secret_Query_Parameter
    assignment and no Credential_Store literal comes back byte for byte,
    so the filter never destroys a diagnosable log line.
    """
    text, secrets = case
    assert redact(text, secrets) == text, text


@settings(max_examples=100)
@given(
    st.sampled_from(_BENIGN_FRAGMENTS),
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=MIN_SECRET_LENGTH - 1
    ),
)
def test_literals_shorter_than_the_threshold_are_left_alone(fragment, short_secret):
    """**Property 3** (clause 3, the length threshold).

    A Credential_Store value shorter than four characters is *not*
    masked. That is the module's documented decision: such values occur
    too often in ordinary text to mask without destroying the message.
    This pins the decision so that it cannot drift silently.
    """
    text = fragment + " " + short_secret + " end"
    assume(not _carries_secret(text))
    assert redact(text, (short_secret,)) == text, text


@settings(max_examples=100)
@given(st.one_of(st.none(), st.integers(), st.booleans(), st.lists(st.text(), max_size=2)))
def test_non_string_records_pass_through(value):
    """**Property 3** (totality, Requirement 6.3).

    The filter hands ``redact`` arbitrary log-record arguments, so a
    non-string is returned unchanged rather than stringified or raising.
    """
    assert redact(value, ("s3c~secret",)) is value


# ---------------------------------------------------------------------------
# End to end: the URL a Stream_Worker connects with
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    st.sampled_from(("RTSP", "RTMP")),
    _USER_NAMES,
    _PASSWORDS,
    st.one_of(st.none(), _QUERY_VALUES),
    st.sampled_from(_SECRET_NAMES + ("sign", "hmac")),
    st.sampled_from(_HOSTS),
    st.sampled_from(_PATHS),
)
def test_worker_connect_url_credentials_never_survive_redaction(
    protocol, user, password, suffix_value, suffix_name, host, path
):
    """**Property 3** (end to end, Requirements 6.1, 6.3).

    ``compose_connect_url`` is the only place credentials enter a URL.
    Should such a URL ever reach a log record, the filter — holding the
    Credential_Store values, as Requirement 6.3 specifies — must leave
    neither the raw nor the percent-encoded credential in it.
    """
    from urllib.parse import quote

    scheme = "rtsp" if protocol == "RTSP" else "rtmp"
    url = "{0}://{1}{2}".format(scheme, host, path)
    suffix = None if suffix_value is None else "?{0}={1}".format(suffix_name, suffix_value)
    secrets = [password] + ([suffix_value] if suffix_value is not None else [])

    connect_url = compose_connect_url(url, suffix, user, password, protocol)
    output = redact(connect_url, secrets)

    assert password not in output, (connect_url, output)
    assert quote(password, safe="") not in output, (connect_url, output)
    if suffix_value is not None:
        assert suffix_value not in output, (connect_url, output)
    assert _residual_secrets(output, secrets) == [], (connect_url, output)
