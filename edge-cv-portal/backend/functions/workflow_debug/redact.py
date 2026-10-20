#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Redaction of every string of a debug record (workflow-debug-mode design
§5.5), applied on the device before a record leaves and again in the
Portal's ingest with the same module.

This file exists twice and must stay byte-identical: the device copy at
``src/backend/workflow_engine/telemetry/debug/redact.py`` and the Portal copy
at ``edge-cv-portal/backend/functions/workflow_debug/redact.py``
(``test_workflow_debug_redact_two_copies.py`` compares them). It is therefore
environment-neutral: the base ``redact(text, secrets)`` of ``stream_url`` is
imported from the Portal's ``workflow_core`` layer when present and from the
device's vendored copy otherwise, nothing of ``dda_logging`` is imported, and
the caller injects the Credential_Store secrets (the device agent passes
``dda_logging.redaction.credential_store_secrets()``; the ingest passes
``frozenset()``).

Masking rules, in order (every mask is ``***``):

1. ``stream_url.redact``: URL user information (``scheme://user:pass@host``
   -> ``scheme://***@host``), ``name=value`` assignments of a
   Secret_Query_Parameter (``password``, ``passwd``, ``pwd``, ``pass``,
   ``secret``, ``token``, ``key``, ``apikey``, ``api_key``, ``auth``,
   ``signature``, ``sig``, ``streamkey``, ``stream_key``) in a URL query or
   in prose, and every injected secret of at least 4 characters;
2. the additional assignment names ``X-Amz-Signature``, ``X-Amz-Credential``
   and ``X-Amz-Security-Token`` (``name=value`` or ``name: value``);
3. the header values of ``Authorization``, ``Proxy-Authorization``,
   ``X-Api-Key``, ``Cookie`` and ``Set-Cookie`` in the ``Header: value`` and
   ``"Header": "value"`` forms;
4. connection-string keys ``AccountKey=``, ``SharedAccessKey=``,
   ``password=`` and ``pwd=`` inside ``;``-separated strings;
5. AWS access key ids ``(AKIA|ASIA)[0-9A-Z]{16}`` and the values of
   ``aws_secret_access_key``, ``SecretAccessKey`` and ``SessionToken``
   (assignment, colon and JSON forms).

The rules are applied until the text stops changing (at most eight passes),
so the result is a fixed point: redacting it again changes nothing.
``redact_structure`` never raises; a value it cannot handle becomes the
string ``"[withheld]"``.
"""

import re
from typing import Any, Iterable

try:  # the Portal's layer exposes the package name...
    from workflow_core.stream_url import redact as _base_redact
except ImportError:  # ...the device its vendored copy
    from workflow_engine.vendor.workflow_core.stream_url import redact as _base_redact

__all__ = ["redact_text", "redact_structure", "WITHHELD", "MASK"]

MASK = "***"
WITHHELD = "[withheld]"

_MAX_PASSES = 8

#: An optionally quoted value: a double- or single-quoted string, or a run of
#: characters up to a delimiter. A quoted value keeps its quotes.
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s&;,)\"'\]}]+)"

#: Rule 2: the additional assignment names (hyphenated, so outside the base
#: rule's word tokens), as a query parameter, an assignment or a colon pair.
_EXTRA_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)"
    r"(\s*[=:]\s*)" + _VALUE,
    re.IGNORECASE,
)

#: Rule 3: header values, ``Header: value`` (to the end of the line or a
#: closing quote/brace) and ``"Header": "value"``.
_HEADER_NAMES = r"(Authorization|Proxy-Authorization|X-Api-Key|Cookie|Set-Cookie)"
_HEADER_JSON_RE = re.compile(
    r"([\"']" + _HEADER_NAMES + r"[\"']\s*:\s*)([\"'])(?:(?!\3).)*\3",
    re.IGNORECASE,
)
_HEADER_LINE_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(" + _HEADER_NAMES + r")(\s*:\s*)([^\r\n\"'}\]]+)",
    re.IGNORECASE,
)

#: Rule 4: connection-string keys whose values may carry ``=`` padding.
_CONNECTION_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(AccountKey|SharedAccessKey|password|pwd)(\s*=\s*)([^;\s\"']+)",
    re.IGNORECASE,
)

#: Rule 5: AWS access key ids and the secret/session values.
_AWS_KEY_ID_RE = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")
_AWS_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])([\"']?)(aws_secret_access_key|SecretAccessKey|SessionToken|"
    r"aws_session_token|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)(\1)(\s*[=:]\s*)" + _VALUE,
    re.IGNORECASE,
)


def _masked_value(value: str) -> str:
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[0] + MASK + value[0]
    return MASK


def _mask_assignment(match: "re.Match") -> str:
    return match.group(1) + match.group(2) + _masked_value(match.group(3))


def _mask_header_json(match: "re.Match") -> str:
    quote = match.group(3)
    return match.group(1) + quote + MASK + quote


def _mask_header_line(match: "re.Match") -> str:
    return match.group(1) + match.group(3) + MASK


def _mask_connection(match: "re.Match") -> str:
    return match.group(1) + match.group(2) + MASK


def _mask_aws_value(match: "re.Match") -> str:
    return (match.group(1) + match.group(2) + match.group(3) + match.group(4)
            + _masked_value(match.group(5)))


def _redact_once(text: str, secrets: Iterable[str]) -> str:
    redacted = _base_redact(text, secrets)
    if not isinstance(redacted, str):
        redacted = text
    redacted = _EXTRA_ASSIGNMENT_RE.sub(_mask_assignment, redacted)
    redacted = _HEADER_JSON_RE.sub(_mask_header_json, redacted)
    redacted = _HEADER_LINE_RE.sub(_mask_header_line, redacted)
    redacted = _CONNECTION_RE.sub(_mask_connection, redacted)
    redacted = _AWS_KEY_ID_RE.sub(MASK, redacted)
    redacted = _AWS_VALUE_RE.sub(_mask_aws_value, redacted)
    return redacted


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask every secret of ``text`` (the rules of the module docstring)
    until a fixed point; a non-string is returned unchanged."""
    if not isinstance(text, str) or not text:
        return text
    literals = tuple(secrets or ())
    redacted = text
    for _ in range(_MAX_PASSES):
        masked = _redact_once(redacted, literals)
        if masked == redacted:
            break
        redacted = masked
    return redacted


def redact_structure(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Walk dicts, lists, tuples and strings, masking every string with
    :func:`redact_text`. Numbers, booleans and ``None`` pass through;
    anything that cannot be redacted becomes ``"[withheld]"``. Never
    raises."""
    try:
        literals = tuple(s for s in (secrets or ()) if isinstance(s, str))
    except Exception:  # noqa: BLE001 - an unusable secrets iterable
        literals = ()
    return _walk(value, literals, 0)


def _walk(value: Any, literals: tuple, depth: int) -> Any:
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return redact_text(value, literals)
        if depth > 64:
            return WITHHELD
        if isinstance(value, dict):
            return {
                (redact_text(k, literals) if isinstance(k, str) else k): _walk(v, literals, depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_walk(v, literals, depth + 1) for v in value]
        if isinstance(value, (bytes, bytearray)):
            return WITHHELD
        return redact_text(str(value), literals)
    except Exception:  # noqa: BLE001 - never raises
        return WITHHELD
