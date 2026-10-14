"""Stream_URL rules: the shared RTSP/RTMP URL contract.

One pure module (standard library only, no I/O and no logging) holding
every rule about a Stream_URL, so that the Portal, the Node_Catalog, the
validator, the LocalServer Image_Sources, the Stream_Workers and the log
redaction filter all agree
(rtsp-rtmp-stream-cameras Requirements 1.3, 2.1, 2.2, 6.1, 6.3, 10.6).

Consumers:

- the catalog constraint on ``rtsp_camera_source.url`` /
  ``rtmp_stream_source.url`` (``STREAM_URL_PATTERN``),
- the workflow validator (V11),
- the Portal camera registry and deployment service,
- the LocalServer Image_Source schema,
- the Stream_Worker (``compose_connect_url``),
- the LocalServer redaction filter (``redact``).

A Stream_URL is *credential-free*: it has a scheme (``rtsp``, ``rtsps``,
``rtmp`` or ``rtmps``), a host, and an optional port, path and query. It
carries no user information and no Secret_Query_Parameter. Secret
material travels separately, as Stream_Credentials.

Two invariants this module guarantees, and that the property tests pin:

- **Soundness / completeness.** Every URL ``check_stream_url`` accepts
  matches ``STREAM_URL_PATTERN``; that is, the checker is never more
  permissive than the catalog regex, so a value the builder accepts is a
  value the catalog accepts.
- **Confidentiality.** No function in this module ever echoes a secret
  value. Problem messages name the offending part (the scheme, the
  query parameter) and never its value.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import quote

__all__ = [
    "STREAM_URL_PATTERN",
    "SCHEMES_BY_NODE_TYPE",
    "SCHEMES_BY_SOURCE_TYPE",
    "SECRET_QUERY_PARAMETERS",
    "DEFAULT_PORTS",
    "StreamUrlProblem",
    "check_stream_url",
    "normalize_stream_url",
    "redact",
    "compose_connect_url",
]

# ---------------------------------------------------------------------------
# Constants (Requirements 1.2, 1.3, 2.1, 2.2)
# ---------------------------------------------------------------------------

#: Catalog regex for Stream_Camera_Source_Node.url; valid in both Python
#: and JavaScript (no named groups, no lookaround, no unicode classes),
#: because the Portal frontend mirrors it verbatim.
#: Lowercase scheme, a non-empty authority containing no '@', then an
#: optional path or query.
STREAM_URL_PATTERN = r"^(rtsps?|rtmps?)://[^\s/@?#]+([/?][^\s#]*)?$"

#: Accepted schemes per Stream_Camera_Source_Node type (Requirement 2.1).
SCHEMES_BY_NODE_TYPE: Dict[str, Tuple[str, ...]] = {
    "rtsp_camera_source": ("rtsp", "rtsps"),
    "rtmp_stream_source": ("rtmp", "rtmps"),
}

#: Accepted schemes per Image_Source / Camera_Source type.
SCHEMES_BY_SOURCE_TYPE: Dict[str, Tuple[str, ...]] = {
    "RTSP": ("rtsp", "rtsps"),
    "RTMP": ("rtmp", "rtmps"),
}

#: Query parameter names that carry secret material. Compared
#: case-insensitively (Requirement 2.2, glossary Secret_Query_Parameter).
SECRET_QUERY_PARAMETERS: FrozenSet[str] = frozenset(
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

#: Default port per scheme, dropped by :func:`normalize_stream_url`.
DEFAULT_PORTS: Dict[str, int] = {"rtsp": 554, "rtsps": 322, "rtmp": 1935, "rtmps": 443}

#: The replacement every masking rule in :func:`redact` writes.
_MASK = "***"

#: Shortest literal :func:`redact` masks. Shorter values are too common
#: in ordinary text to mask without destroying the message.
_MIN_SECRET_LENGTH = 4

#: The catalog constraint, compiled. Applied with ``fullmatch``: the
#: pattern carries its own anchors, but Python's ``$`` also matches just
#: before a trailing newline, while the JavaScript mirror's
#: ``RegExp.test`` anchors at the very end. ``fullmatch`` is the Python
#: spelling of the JavaScript verdict, so a URL with a trailing newline
#: is rejected here exactly as the Workflow_Builder rejects it.
_STREAM_URL_RE = re.compile(STREAM_URL_PATTERN)

#: ``<scheme>://<rest>``, with an RFC 3986 scheme (any case, so that a
#: non-lowercase scheme can be diagnosed rather than swallowed).
_SCHEME_SPLIT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", re.DOTALL)

#: Everything after the authority: the first '/', '?' or '#'.
_AUTHORITY_END_RE = re.compile(r"[/?#]")


# ---------------------------------------------------------------------------
# Problem reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamUrlProblem:
    """Why a URL is not a valid Stream_URL.

    ``code`` is one of ``invalid_url``, ``scheme_not_allowed``,
    ``no_host``, ``user_info`` or ``secret_query_parameter``.
    ``message`` names the offending part and never echoes its value.
    """

    code: str
    message: str


CODE_INVALID_URL = "invalid_url"
CODE_SCHEME_NOT_ALLOWED = "scheme_not_allowed"
CODE_NO_HOST = "no_host"
CODE_USER_INFO = "user_info"
CODE_SECRET_QUERY_PARAMETER = "secret_query_parameter"


# ---------------------------------------------------------------------------
# Parsing helpers (private)
# ---------------------------------------------------------------------------


def _split_scheme(url: str) -> Optional[Tuple[str, str]]:
    """Split ``scheme://rest``; ``None`` when there is no such prefix."""
    match = _SCHEME_SPLIT_RE.match(url)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _split_authority(rest: str) -> Tuple[str, str]:
    """Split the part after ``scheme://`` into (authority, remainder).

    The remainder keeps its leading ``/``, ``?`` or ``#`` so that callers
    can reassemble the URL byte for byte.
    """
    match = _AUTHORITY_END_RE.search(rest)
    if match is None:
        return rest, ""
    return rest[: match.start()], rest[match.start() :]


def _split_host_port(authority: str) -> Tuple[str, Optional[str]]:
    """Split an authority (no user information) into (host, port).

    Handles the bracketed IPv6 form, ``[::1]:554``. ``port`` is the raw
    text between the host and the end, or ``None`` when absent.
    """
    if authority.startswith("["):
        closing = authority.find("]")
        if closing != -1:
            host = authority[: closing + 1]
            tail = authority[closing + 1 :]
            if tail.startswith(":"):
                return host, tail[1:]
            # Anything else after ']' is malformed; keep it on the host so
            # normalization stays byte-faithful.
            return authority, None
        return authority, None
    if ":" in authority:
        host, _, port = authority.rpartition(":")
        return host, port
    return authority, None


def _is_port_number(port: str) -> bool:
    """Whether ``port`` is a non-empty run of ASCII digits."""
    return port.isascii() and port.isdigit()


def _query_of(remainder: str) -> str:
    """The query string of a URL remainder, without its '?' or fragment."""
    if "?" not in remainder:
        return ""
    query = remainder.split("?", 1)[1]
    return query.split("#", 1)[0]


def _secret_query_parameter(query: str) -> Optional[str]:
    """The first Secret_Query_Parameter name in ``query``, if any.

    The name is returned as written, so that the message can name it;
    matching is case-insensitive.
    """
    if not query:
        return None
    for pair in re.split(r"[&;]", query):
        if not pair:
            continue
        name = pair.split("=", 1)[0].strip()
        if name and name.lower() in SECRET_QUERY_PARAMETERS:
            return name
    return None


# ---------------------------------------------------------------------------
# check_stream_url (Requirements 1.3, 2.1, 2.2)
# ---------------------------------------------------------------------------


def check_stream_url(url: Any, allowed_schemes: Iterable[str]) -> Optional[StreamUrlProblem]:
    """Check ``url`` as a Stream_URL; ``None`` when it is valid.

    A URL is rejected when it cannot be parsed, its scheme is outside
    ``allowed_schemes``, its host is empty, it carries user information,
    or its query has a Secret_Query_Parameter.

    Every accepted URL matches :data:`STREAM_URL_PATTERN`, so the checker
    is never more permissive than the catalog constraint. The specific
    codes are reported in preference to the generic ``invalid_url`` even
    when the pattern would also reject the value, so that the operator
    is told what to fix.
    """
    accepted = _accepted_schemes(allowed_schemes)
    accepted_text = ", ".join(accepted) if accepted else "(none)"

    if not isinstance(url, str) or not url.strip():
        return StreamUrlProblem(
            CODE_INVALID_URL,
            "Stream URL is required and must be a non-empty string of the form "
            "{0}://host[:port][/path].".format(accepted[0] if accepted else "rtsp"),
        )

    split = _split_scheme(url)
    if split is None:
        return StreamUrlProblem(
            CODE_INVALID_URL,
            "Stream URL is not a valid URL: it must start with one of {0}:// "
            "followed by a host.".format(accepted_text),
        )
    scheme, rest = split

    if scheme.lower() not in accepted:
        return StreamUrlProblem(
            CODE_SCHEME_NOT_ALLOWED,
            "Stream URL scheme '{0}' is not allowed here; accepted schemes are "
            "{1}.".format(scheme.lower(), accepted_text),
        )

    authority, remainder = _split_authority(rest)

    if "@" in authority:
        return StreamUrlProblem(
            CODE_USER_INFO,
            "Stream URL must not contain embedded user information (the "
            "'user:password@' part before the host); credentials belong in the "
            "camera's configuration, not in the URL.",
        )

    host, port = _split_host_port(authority)
    if not host:
        return StreamUrlProblem(
            CODE_NO_HOST,
            "Stream URL must contain a host, for example "
            "{0}://192.168.1.64/path.".format(scheme.lower()),
        )

    secret_name = _secret_query_parameter(_query_of(remainder))
    if secret_name is not None:
        return StreamUrlProblem(
            CODE_SECRET_QUERY_PARAMETER,
            "Stream URL query parameter '{0}' carries credentials; credentials "
            "belong in the camera's configuration, not in the URL.".format(secret_name),
        )

    if _STREAM_URL_RE.fullmatch(url) is None:
        if scheme != scheme.lower():
            return StreamUrlProblem(
                CODE_INVALID_URL,
                "Stream URL scheme must be lowercase; write '{0}' instead of "
                "'{1}'.".format(scheme.lower(), scheme),
            )
        if "#" in url:
            return StreamUrlProblem(
                CODE_INVALID_URL,
                "Stream URL must not contain a fragment ('#').",
            )
        if re.search(r"\s", url):
            return StreamUrlProblem(
                CODE_INVALID_URL,
                "Stream URL must not contain whitespace.",
            )
        return StreamUrlProblem(
            CODE_INVALID_URL,
            "Stream URL is not a valid URL: it must be of the form "
            "{0}://host[:port][/path][?query].".format(scheme.lower()),
        )

    # The pattern admits any non-space characters after the host, so the
    # port rule is explicit: ASCII digits only. ``str.isdigit`` alone would
    # accept Unicode digits such as '²' or '٣', which no stream client
    # takes as a port (and which ``int()`` cannot even parse).
    if port is not None and port != "" and not _is_port_number(port):
        return StreamUrlProblem(
            CODE_INVALID_URL,
            "Stream URL port must be numeric.",
        )

    return None


def _accepted_schemes(allowed_schemes: Iterable[str]) -> Tuple[str, ...]:
    """Normalize ``allowed_schemes`` to a lowercase, de-duplicated tuple."""
    if isinstance(allowed_schemes, str):
        candidates: Iterable[str] = (allowed_schemes,)
    else:
        candidates = allowed_schemes
    seen: List[str] = []
    for scheme in candidates:
        if not isinstance(scheme, str):
            continue
        lowered = scheme.strip().lower()
        if lowered and lowered not in seen:
            seen.append(lowered)
    return tuple(seen)


# ---------------------------------------------------------------------------
# normalize_stream_url (Requirement 10.6)
# ---------------------------------------------------------------------------


def normalize_stream_url(url: Any) -> str:
    """Return the canonical form of ``url`` for camera identity.

    Lowercases the scheme and the host and drops an explicit default
    port for the scheme. The path and query are kept byte for byte, and
    user information (which a Stream_URL never has) is left untouched.

    Two URLs identify the same camera exactly when their normalized
    forms are equal; this drives the override and unbound URL match
    (Requirement 10.6) and the keys of anonymous sessions. The function
    is total and idempotent: a value it cannot parse is returned
    unchanged.
    """
    if not isinstance(url, str):
        return url
    text = url.strip()
    split = _split_scheme(text)
    if split is None:
        return text
    scheme, rest = split
    scheme = scheme.lower()

    authority, remainder = _split_authority(rest)
    user_info = ""
    if "@" in authority:
        user_info, _, authority = authority.rpartition("@")
        user_info += "@"

    host, port = _split_host_port(authority)
    host = host.lower()

    if port is None or port == "":
        normalized_authority = host
    elif _is_port_number(port) and int(port) == DEFAULT_PORTS.get(scheme):
        normalized_authority = host
    else:
        normalized_authority = "{0}:{1}".format(host, port)

    return "{0}://{1}{2}{3}".format(scheme, user_info, normalized_authority, remainder)


# ---------------------------------------------------------------------------
# redact (Requirements 6.1, 6.3)
# ---------------------------------------------------------------------------

#: ``scheme://user:pass@`` -> ``scheme://***@``. Greedy up to the last
#: '@' of the authority, which cannot contain '/', '?', '#' or
#: whitespace, so the rule never reaches into a path, a query or the
#: following word. An empty user information part is secret-free and is
#: left alone.
_USER_INFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)([^\s/?#]+@)")

#: ``<secret-name>=<value>``: the assignment form a Secret_Query_Parameter
#: takes, in a URL query or in a log line that quotes one. The name must
#: be a whole token (so ``stream_key`` is not matched as ``key``), matches
#: case-insensitively, and the value must be non-empty, so secret-free
#: text such as ``password=`` is preserved byte for byte. An optionally
#: quoted value keeps its quotes, which makes the rule idempotent.
_SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])({0})(\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s&;,)\"'\]}}]+)".format(
        "|".join(
            sorted(
                (re.escape(name) for name in SECRET_QUERY_PARAMETERS),
                key=lambda name: (-len(name), name),
            )
        )
    ),
    re.IGNORECASE,
)


def _mask_secret_value(match: "re.Match") -> str:
    value = match.group(3)
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        masked = value[0] + _MASK + value[0]
    else:
        masked = _MASK
    return match.group(1) + match.group(2) + masked


#: How many times the three rules are applied before giving up on a fixed
#: point. One rule can expose what another masks: masking the credential
#: ``abc`` in ``abctoken=s3cret`` leaves ``***token=s3cret``, where
#: ``token`` has become a whole token and its value must now be masked
#: too. For credentials that do not themselves contain the mask a fixed
#: point is reached in at most three passes; the bound only keeps a
#: pathological credential from looping.
_MAX_REDACTION_PASSES = 8


def _redact_once(text: str, literals: List[str]) -> str:
    """Apply the three masking rules once, in order.

    User information is masked first: its authority may contain an
    ``@`` inside what would otherwise look like a parameter value, so
    masking values first could strip the ``@`` and leave the user
    information in place.
    """
    redacted = _USER_INFO_RE.sub(lambda m: m.group(1) + _MASK + "@", text)
    redacted = _SECRET_VALUE_RE.sub(_mask_secret_value, redacted)
    for literal in literals:
        redacted = redacted.replace(literal, _MASK)
    return redacted


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """Mask every secret in ``text``.

    Three kinds of secret are masked with ``***``:

    - URL user information, so ``scheme://user:pass@host`` becomes
      ``scheme://***@host``,
    - the value of a Secret_Query_Parameter in its ``name=value`` form
      (``password=hunter2`` becomes ``password=***``), whether it sits in
      a URL query or in surrounding prose; an empty value is left alone
      and a quoted value keeps its quotes,
    - every literal in ``secrets`` of length 4 or more, longest first.

    ``redact`` is idempotent, preserves secret-free text, and is total:
    a non-string is returned unchanged so that a logging filter can pass
    arbitrary record arguments through it.

    The rules are applied until the text stops changing, because masking
    one kind of secret can expose another (a credential glued onto a
    Secret_Query_Parameter name is what makes that name a whole token).
    Without that, the output would neither be secret-free nor a fixed
    point.
    """
    if not isinstance(text, str) or not text:
        return text

    literals = sorted(
        {
            secret
            for secret in (secrets or ())
            if isinstance(secret, str) and len(secret) >= _MIN_SECRET_LENGTH
            if secret != _MASK
        },
        key=lambda value: (-len(value), value),
    )

    redacted = text
    for _pass in range(_MAX_REDACTION_PASSES):
        masked = _redact_once(redacted, literals)
        if masked == redacted:
            break
        redacted = masked
    return redacted


# ---------------------------------------------------------------------------
# compose_connect_url (Stream_Worker only)
# ---------------------------------------------------------------------------


def compose_connect_url(
    url: str,
    url_secret_suffix: Optional[str],
    username: Optional[str],
    password: Optional[str],
    protocol: str,
) -> str:
    """Build the URL a Stream_Worker connects with.

    Runs only inside a Stream_Worker, never in the Portal and never on a
    path that logs or persists its result (Requirement 6.1).

    - **RTSP**: the Stream_URL is returned unchanged, because credentials
      go to the ``rtspsrc`` ``user-id`` / ``user-pw`` properties and the
      URL secret suffix, when present, is appended.
    - **RTMP**: URL-encoded user information is inserted before the host
      and the URL secret suffix is appended.

    A suffix that starts with ``?`` is joined with ``&`` when the URL
    already carries a query, so the result stays a single query string.
    """
    if not isinstance(url, str) or _split_scheme(url) is None:
        raise ValueError("stream URL is not well formed")

    normalized_protocol = protocol.strip().upper() if isinstance(protocol, str) else ""
    if normalized_protocol.startswith("RTSP"):
        normalized_protocol = "RTSP"
    elif normalized_protocol.startswith("RTMP"):
        normalized_protocol = "RTMP"
    else:
        raise ValueError(
            "unknown stream protocol; expected one of {0}".format(
                ", ".join(sorted(SCHEMES_BY_SOURCE_TYPE))
            )
        )

    composed = url
    if normalized_protocol == "RTMP":
        composed = _with_user_info(composed, username, password)
    return _append_secret_suffix(composed, url_secret_suffix)


def _with_user_info(url: str, username: Optional[str], password: Optional[str]) -> str:
    """Insert URL-encoded user information before the host."""
    has_user = isinstance(username, str) and username != ""
    has_password = isinstance(password, str) and password != ""
    if not has_user and not has_password:
        return url

    split = _split_scheme(url)
    if split is None:  # pragma: no cover - guarded by the caller
        raise ValueError("stream URL is not well formed")
    scheme, rest = split
    authority, remainder = _split_authority(rest)
    if "@" in authority:
        # Already carries user information; leave it as the caller built it.
        return url

    user_info = quote(username, safe="") if has_user else ""
    if has_password:
        user_info += ":" + quote(password, safe="")
    return "{0}://{1}@{2}{3}".format(scheme, user_info, authority, remainder)


def _append_secret_suffix(url: str, url_secret_suffix: Optional[str]) -> str:
    """Append the URL secret suffix, keeping one query string."""
    if not isinstance(url_secret_suffix, str) or url_secret_suffix == "":
        return url
    suffix = url_secret_suffix
    if suffix.startswith("?") and "?" in url:
        suffix = "&" + suffix[1:]
    return url + suffix
