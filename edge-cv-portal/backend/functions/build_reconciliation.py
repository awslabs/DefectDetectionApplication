"""
Build reconciliation pure logic (Build_Manager)

Pure shared contract for the build-fleet-execution-failures bugfix
(tasks 4.1, 4.2, 4.3). This module deliberately has NO AWS clients, NO
boto3 import, and NO side effects of any kind: it defines the evidence
normalization/redaction/bounding primitives, the deterministic evidence
classification and diagnostic-merge contract, and the timing /
execution-attempt / terminal-effects planning records used by the build
handlers (build_events.py, build_dispatcher.py, build_planner.py,
build_jobs.py) and by tests.

Evidence gate (historical-evidence.md, task 3.3): this module is
authorized by hypothesis-table rows 2 (invocation evidence discarded —
CONFIRMED), 3 (Build Log source incomplete — CONFIRMED), 4 (terminal
fallback premature/generic — CONFIRMED), and 9 (JP6 ephemeral disk
exhaustion + head-keeping tail truncation — CONFIRMED). Row 9
specifically requires head+tail-preserving byte bounding so that
trailing root causes such as ``no space left on device`` survive into
every durable diagnostic, and a distinct stable disk-exhaustion
classification (``RUNNER_DISK_FULL``).

Contract highlights (design.md):
- Raw provider payloads must never reach an application-controlled sink
  (logs, exceptions, Audit_Log details, failed-job messages, DynamoDB,
  API models). Callers sanitize with :func:`sanitize_provider_field` /
  :func:`build_execution_diagnostic` FIRST, and only ever persist or
  log the sanitized result.
- A missing provider field is ``{"available": False}``; an empty but
  available field is ``{"available": True, "text": ""}`` — unavailable
  is never conflated with available-empty and content is never
  fabricated.
- Post-redaction byte bounds: 16 KiB each stdout/stderr, 4 KiB for
  status/detail/message fields, 48 KiB total diagnostic JSON, always
  preserving useful head AND tail with a truncation marker carrying the
  original byte count.
- Classification is deterministic and precedence-ordered; later
  evidence may only increase diagnostic completeness — it can never
  resurrect or overwrite a terminal status, a valid agent result, or
  ``ended_at``. Duplicates and non-increasing evidence are no-ops.
- Deadline boundaries are strict: ``now == deadline`` is NOT expired,
  ``now > deadline`` is expired. Hard ceilings are non-extendable.

Spec: .kiro/specs/build-fleet-execution-failures
Requirements: 2.1, 2.2, 2.4, 2.6, 2.7, 2.10, 2.11, 2.14, 2.16, 2.17,
2.18, 2.21, 2.22, 3.1, 3.6, 3.8, 3.11, 3.12
"""
import json
import re
from decimal import Decimal
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import build_domain

# ===========================================================================
# Task 4.1 — Redaction, normalization, and byte-bounding primitives
# ===========================================================================

# ---------------------------------------------------------------------------
# Byte bounds (design "Data Model"; post-redaction limits)
# ---------------------------------------------------------------------------

#: Post-redaction byte limit for each of stdout / stderr.
STDOUT_STDERR_LIMIT_BYTES = 16 * 1024
#: Post-redaction byte limit for status/detail/message fields.
DETAIL_FIELD_LIMIT_BYTES = 4 * 1024
#: Total serialized diagnostic JSON byte limit.
TOTAL_DIAGNOSTIC_LIMIT_BYTES = 48 * 1024

#: Replacement for every redacted secret value; key names are retained.
REDACTED = '[REDACTED]'

# ---------------------------------------------------------------------------
# Redaction patterns (Req 2.10). Values are replaced with [REDACTED];
# key/context names are retained where safe so diagnostics stay useful.
# ---------------------------------------------------------------------------

# Key names whose assigned values are always secrets.
_SECRET_KEY_FRAGMENT = (
    r"[\w\-\.]*(?:secret|password|passwd|pwd|token|credential|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|session[_-]?key|"
    r"client[_-]?key|auth)[\w\-\.]*"
)

#: A map KEY whose full name matches this is a secret assignment: its
#: VALUE (scalar or whole subtree) is redacted while the key name is
#: retained (design "Shared Diagnostic Sanitizer": key names are
#: retained where safe; values become [REDACTED]).
_SECRET_KEY_NAME_RE = re.compile(
    r"(?i)^" + _SECRET_KEY_FRAGMENT + r"$")

_REDACTION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # AWS access key IDs (access, temporary/session credentials).
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)"
                r"[0-9A-Z]{16}\b"),
     REDACTED),
    # PEM private-key blocks.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
                r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                re.DOTALL),
     REDACTED),
    # Authorization headers / values: Bearer, Basic, raw Authorization.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=_\-\.~]+"),
     r"\1 " + REDACTED),
    (re.compile(r"(?i)\b(authorization)\b(\s*[:=]\s*)\S+"),
     r"\1\2" + REDACTED),
    # Repository / URL userinfo credentials: scheme://user:pass@host
    (re.compile(r"(://)[^/@:\s]+:[^@/\s]+@"),
     r"\1" + REDACTED + ":" + REDACTED + "@"),
    # Signed-URL credential/signature/token query parameters.
    (re.compile(r"(?i)([?&](?:X-Amz-Signature|X-Amz-Credential|"
                r"X-Amz-Security-Token|AWSAccessKeyId|Signature|Token|"
                r"X-Goog-Signature|sig)=)[^&\s\"']+"),
     r"\1" + REDACTED),
    # Well-known token literal formats (GitHub/GitLab/Slack PATs, JWTs).
    (re.compile(r"\bghp_[A-Za-z0-9]{4,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{4,}\b"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9\-_]{4,}\b"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{4,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"
                r"\.[A-Za-z0-9_\-]+\b"), REDACTED),
    # Assignment-style secrets: KEY=value, KEY: value, KEY value,
    # quoted or bare. The key name is retained; the value is redacted.
    # The first lookahead keeps this pass from re-consuming values an
    # earlier pattern already redacted (e.g. signed-URL parameters).
    # The second lookahead keeps a bare secret-key WORD (e.g. a lone
    # "secret" on the previous line) from consuming a FOLLOWING key's
    # name as its "value": without it, "secret\nPASSWORD: <val>" matched
    # key="secret", separator="\n", value="PASSWORD:", leaving <val>
    # unredacted (found by the Property 3 redaction canary test). A
    # candidate value that is itself a secret key introducing its own
    # assignment is refused here so the engine re-anchors on that inner
    # key and redacts its real value instead. Two refusal shapes:
    #   1. the candidate introduces an explicit ":"/"=" assignment
    #      ("secret\nPASSWORD: v"), or
    #   2. the candidate is followed by a SAME-LINE bare-space token
    #      ("secret\nPASSWORD v") — horizontal whitespace only, so a
    #      genuine value that merely LOOKS secret-ish and ends its line
    #      (e.g. "KEY=verysecretvalue\n...") is still redacted, not
    #      mistaken for an inner key. A broader refusal (any value
    #      containing a secret-ish word) would itself cause leaks.
    (re.compile(r"(?i)\b(" + _SECRET_KEY_FRAGMENT + r")"
                r"(\s*[:=]\s*|\s+)"
                r"(?!\[REDACTED\])"
                r"(?!(?:" + _SECRET_KEY_FRAGMENT + r")\s*[:=])"
                r"(?!(?:" + _SECRET_KEY_FRAGMENT + r")[^\S\n]+\S)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1\2" + REDACTED),
]


def redact_text(text: str,
                extra_patterns: Optional[List[str]] = None) -> str:
    """Redact secret VALUES from one text while retaining safe context
    (Req 2.10). ``extra_patterns`` are configured organization-specific
    regular expressions whose full match is replaced with [REDACTED].

    This must run before ANY application-controlled sink: DynamoDB, API
    responses, portal output, Lambda/CloudWatch logs, Audit_Log details,
    exceptions, and failed-job messages.
    """
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    for raw in (extra_patterns or []):
        try:
            text = re.sub(raw, REDACTED, text)
        except re.error:
            # A misconfigured org pattern must fail CLOSED for that
            # pattern's literal text, not crash sanitization.
            text = text.replace(raw, REDACTED)
    return text


# ---------------------------------------------------------------------------
# Normalization (Req 2.2): nested scalar/list/map evidence to plain
# JSON-safe Python values, preserving valid UTF-8/JSON.
# ---------------------------------------------------------------------------

def normalize_evidence(value: Any) -> Any:
    """Recursively normalize nested scalar/list/map evidence into plain
    JSON-serializable values.

    - ``Decimal`` (DynamoDB) becomes int when integral, else float.
    - ``bytes`` are decoded as UTF-8 with invalid sequences replaced, so
      the result is always valid UTF-8.
    - lists/tuples/sets become lists; maps stay maps with str keys.
    - Unknown objects become their ``str()`` representation.
    - ``None``, bool, int, float, str pass through unchanged.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', errors='replace')
    if isinstance(value, dict):
        return {str(key): normalize_evidence(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return [normalize_evidence(item) for item in items]
    return str(value)


# ---------------------------------------------------------------------------
# Byte bounding (Req 2.2, evidence-gate row 9): head AND tail preserved.
# ---------------------------------------------------------------------------

def _utf8_head(raw: bytes, limit: int) -> str:
    """Longest valid-UTF-8 prefix of ``raw`` within ``limit`` bytes."""
    if limit <= 0:
        return ''
    return raw[:limit].decode('utf-8', errors='ignore')


def _utf8_tail(raw: bytes, limit: int) -> str:
    """Longest valid-UTF-8 suffix of ``raw`` within ``limit`` bytes."""
    if limit <= 0:
        return ''
    return raw[-limit:].decode('utf-8', errors='ignore')


def truncation_marker(original_bytes: int) -> str:
    """The marker inserted between preserved head and tail."""
    return ("\n[TRUNCATED: original {n} bytes; head and tail preserved]\n"
            .format(n=original_bytes))


class BoundedText(NamedTuple):
    """Result of byte-bounding one text field.

    - ``text``: the bounded (possibly truncated) valid-UTF-8 text
    - ``truncated``: True iff content was removed
    - ``original_bytes``: UTF-8 byte length before bounding
    """
    text: str
    truncated: bool
    original_bytes: int


def bound_text(text: str, limit_bytes: int) -> BoundedText:
    """Bound one text to ``limit_bytes`` UTF-8 bytes, preserving useful
    head AND tail around an inserted truncation marker with the original
    byte count (Req 2.2, 2.22).

    Head+tail preservation is deliberate (evidence-gate row 9): the JP6
    job ``bd91c5d8``'s durable error was cut mid-path by
    ``tail -n 5 | head -c 512``, dropping the trailing
    ``no space left on device`` root cause. The tail of over-length
    content must survive.
    """
    if not isinstance(text, str):
        text = str(text)
    raw = text.encode('utf-8')
    original = len(raw)
    if original <= limit_bytes:
        return BoundedText(text=text, truncated=False,
                           original_bytes=original)
    marker = truncation_marker(original)
    marker_bytes = len(marker.encode('utf-8'))
    budget = limit_bytes - marker_bytes
    if budget <= 0:
        # Degenerate limit: keep only the tail (the root-cause end).
        return BoundedText(text=_utf8_tail(raw, max(limit_bytes, 0)),
                           truncated=True, original_bytes=original)
    head_budget = budget // 2
    tail_budget = budget - head_budget
    bounded = (_utf8_head(raw, head_budget) + marker
               + _utf8_tail(raw, tail_budget))
    return BoundedText(text=bounded, truncated=True,
                       original_bytes=original)


def bound_tail_text(text: str, limit_bytes: int) -> BoundedText:
    """Bound one text keeping ONLY its trailing bytes (Req 2.22,
    tail-preserving truncation for durable error-message derivation).
    Lines already within the bound are unchanged (Req 3.15)."""
    if not isinstance(text, str):
        text = str(text)
    raw = text.encode('utf-8')
    original = len(raw)
    if original <= limit_bytes:
        return BoundedText(text=text, truncated=False,
                           original_bytes=original)
    return BoundedText(text=_utf8_tail(raw, limit_bytes),
                       truncated=True, original_bytes=original)


# ---------------------------------------------------------------------------
# Provider-field representation (Req 2.2): unavailable vs available-empty.
# ---------------------------------------------------------------------------

#: Sentinel distinguishing "the provider did not return this field at
#: all" from any real (possibly empty) value.
FIELD_UNAVAILABLE = object()


def sanitize_provider_field(
        value: Any,
        limit_bytes: int = STDOUT_STDERR_LIMIT_BYTES,
        extra_patterns: Optional[List[str]] = None) -> Dict[str, Any]:
    """Sanitize one provider text field into the design's field record.

    - missing field (``FIELD_UNAVAILABLE`` or ``None``) ->
      ``{"available": False}`` — identified as unavailable, never
      fabricated (Req 2.2)
    - present field (even empty) -> ``{"available": True, "text": ...,
      "truncated": ..., "original_bytes": ...}`` where text is
      normalized, redacted, then byte-bounded (in that order, so limits
      apply POST-redaction).
    """
    if value is FIELD_UNAVAILABLE or value is None:
        return {'available': False}
    normalized = normalize_evidence(value)
    if not isinstance(normalized, str):
        normalized = json.dumps(normalized, sort_keys=True, default=str)
    redacted = redact_text(normalized, extra_patterns)
    bounded = bound_text(redacted, limit_bytes)
    return {
        'available': True,
        'text': bounded.text,
        'truncated': bounded.truncated,
        'original_bytes': bounded.original_bytes,
    }


def provider_field(payload: Optional[Dict[str, Any]], key: str) -> Any:
    """Read one field from a provider payload, distinguishing an absent
    field (``FIELD_UNAVAILABLE``) from any present (even empty) value."""
    if not isinstance(payload, dict) or key not in payload:
        return FIELD_UNAVAILABLE
    return payload[key]


def sanitize_evidence_tree(value: Any,
                           extra_patterns: Optional[List[str]] = None,
                           limit_bytes: int = DETAIL_FIELD_LIMIT_BYTES
                           ) -> Any:
    """Normalize then redact (and bound string leaves of) an arbitrary
    nested evidence structure. Map keys are retained; every string leaf
    is redacted and bounded. A value assigned to a secret-shaped map
    key (password/token/secret/credential/...) is a secret by
    definition: the whole assigned value — scalar, list, or subtree —
    becomes [REDACTED] while the key name is retained (Req 2.10)."""
    normalized = normalize_evidence(value)

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            return bound_text(redact_text(node, extra_patterns),
                              limit_bytes).text
        if isinstance(node, dict):
            result: Dict[str, Any] = {}
            for key, item in node.items():
                if isinstance(key, str) and \
                        _SECRET_KEY_NAME_RE.match(key):
                    result[key] = REDACTED
                else:
                    result[key] = _walk(item)
            return result
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(normalized)


def diagnostic_json_bytes(diagnostic: Dict[str, Any]) -> int:
    """Serialized JSON byte size of a diagnostic (bounding metric)."""
    return len(json.dumps(diagnostic, sort_keys=True,
                          default=str).encode('utf-8'))


def bound_diagnostic_total(
        diagnostic: Dict[str, Any],
        total_limit_bytes: int = TOTAL_DIAGNOSTIC_LIMIT_BYTES
        ) -> Dict[str, Any]:
    """Enforce the total serialized-JSON bound on a diagnostic by
    progressively re-bounding its largest text fields (stdout, stderr,
    then detail fields), preserving head+tail each time. Structure and
    availability markers are never dropped."""
    result = json.loads(json.dumps(diagnostic, default=str))
    if diagnostic_json_bytes(result) <= total_limit_bytes:
        return result

    def _shrink(field_names: List[str], floor_bytes: int) -> bool:
        for name in field_names:
            field = result.get(name)
            if not isinstance(field, dict) or not field.get('available'):
                continue
            text = field.get('text')
            if not isinstance(text, str):
                continue
            current = len(text.encode('utf-8'))
            while current > floor_bytes and \
                    diagnostic_json_bytes(result) > total_limit_bytes:
                target = max(floor_bytes, current // 2)
                original = field.get('original_bytes', current)
                bounded = bound_text(text, target)
                field['text'] = bounded.text
                field['truncated'] = True
                field['original_bytes'] = original
                text = bounded.text
                current = len(text.encode('utf-8'))
            if diagnostic_json_bytes(result) <= total_limit_bytes:
                return True
        return diagnostic_json_bytes(result) <= total_limit_bytes

    if _shrink(['stdout', 'stderr'], 1024):
        return result
    _shrink(['status_details', 'message'], 256)
    return result


# ===========================================================================
# Task 4.2 — Deterministic evidence classification, precedence, and
# diagnostic merge
# ===========================================================================

# ---------------------------------------------------------------------------
# Stable safe error codes (design classification table, Req 2.4/2.21)
# ---------------------------------------------------------------------------

CODE_COMMAND_LAUNCH_FAILED = 'COMMAND_LAUNCH_FAILED'
CODE_COMMAND_EXECUTION_FAILED = 'COMMAND_EXECUTION_FAILED'
CODE_COMMAND_TIMED_OUT = 'COMMAND_TIMED_OUT'
CODE_COMMAND_CANCELLED = 'COMMAND_CANCELLED'
CODE_AGENT_RESULT_MISSING = 'AGENT_RESULT_MISSING'
CODE_INFRASTRUCTURE_LOST = 'INFRASTRUCTURE_LOST'
CODE_AGENT_HEARTBEAT_EXPIRED = 'AGENT_HEARTBEAT_EXPIRED'
CODE_BUILD_PROGRESS_STALLED = 'BUILD_PROGRESS_STALLED'
CODE_PROVISIONING_TIMEOUT = 'PROVISIONING_TIMEOUT'
CODE_QUEUE_WAIT_TIMEOUT = 'QUEUE_WAIT_TIMEOUT'
CODE_MAX_RUNTIME_EXCEEDED = 'MAX_RUNTIME_EXCEEDED'
CODE_RUNNER_DISK_FULL = 'RUNNER_DISK_FULL'
CODE_COMMAND_PREFLIGHT_FAILED = 'COMMAND_PREFLIGHT_FAILED'

STABLE_ERROR_CODES = frozenset({
    CODE_COMMAND_LAUNCH_FAILED,
    CODE_COMMAND_EXECUTION_FAILED,
    CODE_COMMAND_TIMED_OUT,
    CODE_COMMAND_CANCELLED,
    CODE_AGENT_RESULT_MISSING,
    CODE_INFRASTRUCTURE_LOST,
    CODE_AGENT_HEARTBEAT_EXPIRED,
    CODE_BUILD_PROGRESS_STALLED,
    CODE_PROVISIONING_TIMEOUT,
    CODE_QUEUE_WAIT_TIMEOUT,
    CODE_MAX_RUNTIME_EXCEEDED,
    CODE_RUNNER_DISK_FULL,
    CODE_COMMAND_PREFLIGHT_FAILED,
})

#: Terminal SSM invocation statuses (provider vocabulary).
SSM_TERMINAL_STATUSES = frozenset(
    {'Success', 'Failed', 'TimedOut', 'Cancelled'})

# ---------------------------------------------------------------------------
# Disk-exhaustion (ENOSPC) evidence detection (Req 2.21, evidence-gate
# row 9). Pure and deterministic: outputs with no disk-exhaustion
# evidence never classify as RUNNER_DISK_FULL.
# ---------------------------------------------------------------------------

_ENOSPC_PATTERN = re.compile(
    r"(?i)(no space left on device|enospc)")

#: Agent terminal callbacks may short-circuit detection by reporting
#: this error kind directly (design "ENOSPC classification").
AGENT_ERROR_KIND_DISK = 'disk'

#: Agent terminal callbacks reporting a failed dispatch preflight (task
#: 7.1: an invalid startup contract must fail BEFORE build/publish with
#: the stable COMMAND_PREFLIGHT_FAILED code).
AGENT_ERROR_KIND_PREFLIGHT = 'preflight'

#: Marker the generated preflight guard (build_dispatcher
#: .preflight_guard_commands) and the agent's own preflight write to
#: stderr/stdout when an invalid startup contract is detected before any
#: costly work. Reconciliation maps invocation output carrying this
#: marker to CODE_COMMAND_PREFLIGHT_FAILED (evidence-gate rows 1 and 8,
#: both CONFIRMED: the 2026-08-06 AMD64 dispatch reached a live
#: m6i.4xlarge with zero pre-checks and failed on a script path that did
#: not exist).
PREFLIGHT_FAILURE_MARKER = 'DDA_PREFLIGHT_FAILED'


def is_preflight_failure_evidence(*texts: Any,
                                  agent_error_kind: Optional[str] = None
                                  ) -> bool:
    """True iff any provided text carries the preflight failure marker,
    or the agent explicitly reported ``error_kind=preflight``."""
    if agent_error_kind == AGENT_ERROR_KIND_PREFLIGHT:
        return True
    for text in texts:
        if isinstance(text, str) and PREFLIGHT_FAILURE_MARKER in text:
            return True
    return False


def is_disk_exhaustion_evidence(*texts: Any,
                                agent_error_kind: Optional[str] = None
                                ) -> bool:
    """True iff any provided text carries an ENOSPC/disk-exhaustion
    pattern, or the agent explicitly reported ``error_kind=disk``."""
    if agent_error_kind == AGENT_ERROR_KIND_DISK:
        return True
    for text in texts:
        if isinstance(text, str) and _ENOSPC_PATTERN.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# Attempt correlation and stale-evidence rejection (Req 2.6)
# ---------------------------------------------------------------------------

def evidence_matches_attempt(attempt: Optional[Dict[str, Any]],
                             evidence: Optional[Dict[str, Any]]) -> bool:
    """True iff a piece of evidence belongs to the job's current
    execution attempt. Evidence carrying a DIFFERENT attempt_id,
    command_id, or instance_id than the attempt record is stale or
    mismatched and MUST be rejected (it can never affect this attempt).

    A field absent from either side does not mismatch (legacy events);
    an explicit conflicting value does.
    """
    if not isinstance(evidence, dict):
        return False
    attempt = attempt or {}
    for key in ('attempt_id', 'command_id', 'instance_id'):
        ours = attempt.get(key)
        theirs = evidence.get(key)
        if ours is not None and theirs is not None and ours != theirs:
            return False
    return True


# ---------------------------------------------------------------------------
# Invocation lookup representation (Req 2.5/2.6): service unavailability
# and eventual consistency are represented, never fabricated as failure.
# ---------------------------------------------------------------------------

#: Lookup states for the final GetCommandInvocation evidence.
LOOKUP_RETRIEVED = 'retrieved'          # final invocation in hand
LOOKUP_PENDING = 'pending'              # InvocationDoesNotExist /
#                                       # service unavailable, within the
#                                       # bounded lookup window: retry
LOOKUP_UNAVAILABLE = 'unavailable'      # bounded window exhausted; the
#                                       # evidence is identified as
#                                       # unavailable (not fabricated)


class InvocationLookup(NamedTuple):
    """Pure decision about one invocation-retrieval observation.

    - ``state``: LOOKUP_RETRIEVED | LOOKUP_PENDING | LOOKUP_UNAVAILABLE
    - ``retry_after_ms``: when PENDING, the caller should look again on
      its next tick (informational; scheduling is the caller's I/O)
    """
    state: str
    retry_after_ms: Optional[int]


def decide_invocation_lookup(invocation: Optional[Dict[str, Any]],
                             first_observed_at: int,
                             now: int,
                             lookup_window_ms: int,
                             retry_interval_ms: int = 60 * 1000
                             ) -> InvocationLookup:
    """Represent invocation service unavailability / eventual
    consistency (``InvocationDoesNotExist``) without fabricating a
    command failure (Req 2.5, design reconciliation flow step 2).

    Strict boundary: the lookup window is exhausted only when
    ``now > first_observed_at + lookup_window_ms``.
    """
    if isinstance(invocation, dict) and invocation:
        return InvocationLookup(state=LOOKUP_RETRIEVED,
                                retry_after_ms=None)
    if now > first_observed_at + lookup_window_ms:
        return InvocationLookup(state=LOOKUP_UNAVAILABLE,
                                retry_after_ms=None)
    return InvocationLookup(state=LOOKUP_PENDING,
                            retry_after_ms=retry_interval_ms)


# ---------------------------------------------------------------------------
# Settlement planning (Req 2.5/2.6): a bounded interval after terminal
# command observation in which an already-in-flight terminal agent
# result may still arrive.
# ---------------------------------------------------------------------------

#: Default settlement window after a terminal command observation.
DEFAULT_SETTLEMENT_WINDOW_MS = 2 * 60 * 1000


def settlement_deadline(observed_terminal_at: int,
                        settlement_window_ms: int =
                        DEFAULT_SETTLEMENT_WINDOW_MS) -> int:
    """The settlement deadline for one terminal command observation."""
    return observed_terminal_at + settlement_window_ms


def settlement_expired(deadline: int, now: int) -> bool:
    """Strict boundary: settled only when ``now > deadline``."""
    return now > deadline


# ---------------------------------------------------------------------------
# Deterministic classification (design precedence table, Req 2.4/2.6)
# ---------------------------------------------------------------------------

class Classification(NamedTuple):
    """One precedence-defined reconciliation outcome for an attempt.

    - ``decided``: False means no terminal decision yet (keep waiting /
      keep the job nonterminal); the remaining fields describe why
    - ``status``: the Build_Job terminal status implied (failed /
      interrupted / cancelled / succeeded), or the current nonterminal
      status when not decided
    - ``error_code``: one of STABLE_ERROR_CODES (None for success or
      agent-authoritative results)
    - ``authority``: which precedence rule decided (1-7, matching the
      design's evidence-precedence list)
    - ``reason``: short safe human-readable summary (already safe: it
      is built only from stable vocabulary, never raw provider text)
    """
    decided: bool
    status: Optional[str]
    error_code: Optional[str]
    authority: Optional[int]
    reason: str


def _invocation_text_fields(invocation: Optional[Dict[str, Any]]
                            ) -> List[str]:
    fields = []
    for key in ('StandardErrorContent', 'StandardOutputContent',
                'StatusDetails'):
        value = (invocation or {}).get(key)
        if isinstance(value, str):
            fields.append(value)
    return fields


def classify_attempt(
        current_status: str,
        invocation: Optional[Dict[str, Any]] = None,
        agent_result: Optional[Dict[str, Any]] = None,
        user_cancellation_confirmed: bool = False,
        hard_deadline_ms: Optional[int] = None,
        infrastructure_lost: bool = False,
        send_command_rejected: bool = False,
        settlement_deadline_ms: Optional[int] = None,
        now: Optional[int] = None) -> Classification:
    """Deterministically classify one execution attempt's settled
    evidence by semantic authority, independent of delivery order
    (design "Evidence Precedence and Classification").

    ``agent_result`` is a CORRELATED terminal agent result (callers
    reject stale evidence with :func:`evidence_matches_attempt` first)
    of shape ``{"phase": "succeeded"|"failed"|..., "completed_at": ms,
    "error_kind": optional, "message": optional}``.
    """
    # 1. Valid correlated agent terminal result at/before an already-
    #    decided hard deadline wins and preserves result metadata.
    if isinstance(agent_result, dict) and agent_result:
        completed_at = agent_result.get('completed_at')
        qualifies = (hard_deadline_ms is None or completed_at is None
                     or completed_at <= hard_deadline_ms)
        if qualifies:
            phase = agent_result.get('phase')
            if phase == 'succeeded':
                return Classification(
                    decided=True, status=build_domain.STATUS_SUCCEEDED,
                    error_code=None, authority=1,
                    reason='terminal agent result: succeeded')
            # Agent-reported failure keeps agent authority; ENOSPC
            # evidence in the agent's own message/kind maps to the
            # distinct stable disk-exhaustion code (Req 2.21).
            if is_disk_exhaustion_evidence(
                    agent_result.get('message'),
                    agent_error_kind=agent_result.get('error_kind')):
                return Classification(
                    decided=True, status=build_domain.STATUS_FAILED,
                    error_code=CODE_RUNNER_DISK_FULL, authority=1,
                    reason='terminal agent result: disk exhaustion '
                           '(ENOSPC evidence)')
            return Classification(
                decided=True, status=build_domain.STATUS_FAILED,
                error_code=None, authority=1,
                reason='terminal agent result: failed')

    # 2. Explicit user cancellation with confirmed stop.
    if user_cancellation_confirmed:
        return Classification(
            decided=True, status=build_domain.STATUS_CANCELLED,
            error_code=None, authority=2,
            reason='user cancellation with confirmed stop')

    # 3. Hard-ceiling decision (no qualifying pre-deadline result).
    if hard_deadline_ms is not None and now is not None \
            and now > hard_deadline_ms:
        return Classification(
            decided=True, status=build_domain.STATUS_FAILED,
            error_code=CODE_MAX_RUNTIME_EXCEEDED, authority=3,
            reason='active execution crossed its non-extendable hard '
                   'safety ceiling')

    # 4. Infrastructure loss / spot interruption.
    if infrastructure_lost:
        return Classification(
            decided=True, status=build_domain.STATUS_INTERRUPTED,
            error_code=CODE_INFRASTRUCTURE_LOST, authority=4,
            reason='instance/server lifecycle lost')

    # SendCommand rejected before a command ID existed.
    if send_command_rejected:
        return Classification(
            decided=True, status=build_domain.STATUS_FAILED,
            error_code=CODE_COMMAND_LAUNCH_FAILED, authority=5,
            reason='SendCommand rejected before a command ID')

    invocation_status = (invocation or {}).get('Status')

    # 5. Terminal SSM Failed/TimedOut/Cancelled without a terminal
    #    agent result: classify from invocation + lifecycle evidence.
    if invocation_status in ('Failed', 'TimedOut', 'Cancelled'):
        if invocation_status == 'TimedOut':
            return Classification(
                decided=True, status=build_domain.STATUS_FAILED,
                error_code=CODE_COMMAND_TIMED_OUT, authority=5,
                reason='invocation service status TimedOut')
        if invocation_status == 'Cancelled':
            return Classification(
                decided=True, status=build_domain.STATUS_INTERRUPTED,
                error_code=CODE_COMMAND_CANCELLED, authority=5,
                reason='unexpected invocation cancellation')
        # Failed preflight guard (task 7.1): the generated guard /
        # agent preflight writes PREFLIGHT_FAILURE_MARKER before any
        # costly work; the stable code is COMMAND_PREFLIGHT_FAILED.
        if is_preflight_failure_evidence(
                *_invocation_text_fields(invocation)):
            return Classification(
                decided=True, status=build_domain.STATUS_FAILED,
                error_code=CODE_COMMAND_PREFLIGHT_FAILED, authority=5,
                reason='dispatch preflight failed before build/publish '
                       '(invalid startup contract)')
        # Failed / non-zero response code. ENOSPC evidence in the
        # invocation output is the distinct disk-exhaustion code.
        if is_disk_exhaustion_evidence(
                *_invocation_text_fields(invocation)):
            return Classification(
                decided=True, status=build_domain.STATUS_FAILED,
                error_code=CODE_RUNNER_DISK_FULL, authority=5,
                reason='invocation output carries disk-exhaustion '
                       '(ENOSPC) evidence')
        return Classification(
            decided=True, status=build_domain.STATUS_FAILED,
            error_code=CODE_COMMAND_EXECUTION_FAILED, authority=5,
            reason='invocation Failed with non-zero response')

    # 6. SSM Success without a terminal agent result: pending through
    #    settlement, then AGENT_RESULT_MISSING.
    if invocation_status == 'Success':
        if settlement_deadline_ms is not None and now is not None \
                and settlement_expired(settlement_deadline_ms, now):
            return Classification(
                decided=True, status=build_domain.STATUS_FAILED,
                error_code=CODE_AGENT_RESULT_MISSING, authority=6,
                reason='invocation Success but no terminal agent '
                       'result by the settlement deadline')
        return Classification(
            decided=False, status=current_status, error_code=None,
            authority=6,
            reason='invocation Success; awaiting the in-flight agent '
                   'result within the settlement window')

    # 7. Missing/delayed service evidence: represented as unavailable
    #    and retried within bounds; never fabricated.
    return Classification(
        decided=False, status=current_status, error_code=None,
        authority=7,
        reason='no terminal evidence available yet; retry within the '
               'bounded lookup window')


# ---------------------------------------------------------------------------
# Execution diagnostic construction (Req 2.2) and field-completeness
# merge independent of delivery order (Req 2.6)
# ---------------------------------------------------------------------------

DIAGNOSTIC_SCHEMA_VERSION = 1

#: The disk-capacity fields the optional ``execution_diagnostic.disk``
#: block may carry (task 7.5, Req 2.23). Everything else is dropped by
#: the sanitizer so an arbitrary payload can never widen the record.
_DISK_EVIDENCE_FIELDS = ('docker_storage_path', 'available_gb',
                         'used_gb', 'total_gb', 'measured_at')

#: The truthful "no measurement was taken" disk block (Req 2.23:
#: unavailable measurements are identified, never fabricated).
DISK_EVIDENCE_UNAVAILABLE: Dict[str, Any] = {'available': False}


def sanitize_disk_evidence(raw: Any,
                           extra_patterns: Optional[List[str]] = None
                           ) -> Dict[str, Any]:
    """The optional ``execution_diagnostic.disk`` block from a raw
    disk-capacity measurement (task 7.5, Req 2.23): the known fields
    only, normalized/redacted/bounded through the task 4.1 primitives.
    A missing or unusable measurement is ``{"available": False}`` —
    identified as unavailable rather than fabricated."""
    if not isinstance(raw, dict) or not raw:
        return dict(DISK_EVIDENCE_UNAVAILABLE)
    if raw.get('available') is False:
        return dict(DISK_EVIDENCE_UNAVAILABLE)
    block: Dict[str, Any] = {}
    for field in _DISK_EVIDENCE_FIELDS:
        if field not in raw:
            continue
        value = normalize_evidence(raw[field])
        if isinstance(value, str):
            value = bound_text(redact_text(value, extra_patterns),
                               DETAIL_FIELD_LIMIT_BYTES).text
        block[field] = value
    if not block:
        return dict(DISK_EVIDENCE_UNAVAILABLE)
    block['available'] = True
    return block


def build_execution_diagnostic(
        attempt: Optional[Dict[str, Any]],
        invocation: Optional[Dict[str, Any]],
        classification: Optional[str],
        source: str,
        observed_at: int,
        extra_patterns: Optional[List[str]] = None,
        disk: Any = None) -> Dict[str, Any]:
    """Build one bounded, redacted, truthful ``execution_diagnostic``
    from a (raw) final invocation. The raw invocation must exist only in
    local memory long enough to pass through this function; callers
    persist/log ONLY the returned sanitized structure.
    """
    attempt = attempt or {}
    invocation = invocation or {}

    def _detail(key: str) -> Any:
        value = provider_field(invocation, key)
        if value is FIELD_UNAVAILABLE:
            return None
        text = normalize_evidence(value)
        if not isinstance(text, str):
            text = json.dumps(text, sort_keys=True, default=str)
        return bound_text(redact_text(text, extra_patterns),
                          DETAIL_FIELD_LIMIT_BYTES).text

    response_code = provider_field(invocation, 'ResponseCode')
    diagnostic = {
        'schema_version': DIAGNOSTIC_SCHEMA_VERSION,
        'attempt_id': attempt.get('attempt_id'),
        'command_id': (attempt.get('command_id')
                       or invocation.get('CommandId')),
        'instance_id': (attempt.get('instance_id')
                        or invocation.get('InstanceId')),
        'source': [source],
        'status': _detail('Status'),
        'status_details': _detail('StatusDetails'),
        'response_code': (None if response_code is FIELD_UNAVAILABLE
                          else normalize_evidence(response_code)),
        'execution_start': _detail('ExecutionStartDateTime'),
        'execution_end': _detail('ExecutionEndDateTime'),
        'stdout': sanitize_provider_field(
            provider_field(invocation, 'StandardOutputContent'),
            STDOUT_STDERR_LIMIT_BYTES, extra_patterns),
        'stderr': sanitize_provider_field(
            provider_field(invocation, 'StandardErrorContent'),
            STDOUT_STDERR_LIMIT_BYTES, extra_patterns),
        'classification': classification,
        # Optional disk-capacity evidence (task 7.5, Req 2.23): the
        # preflight measurement when one was recorded, else the
        # truthful {"available": False} marker.
        'disk': sanitize_disk_evidence(disk, extra_patterns),
        'observed_at': observed_at,
        'complete': bool(invocation),
    }
    return bound_diagnostic_total(diagnostic)


def _field_completeness(value: Any) -> int:
    """Completeness rank of one diagnostic field value: available text
    beats available-empty beats unavailable beats absent."""
    if isinstance(value, dict) and 'available' in value:
        if not value.get('available'):
            return 1
        return 3 if value.get('text') else 2
    if value is None:
        return 0
    return 3


def merge_diagnostics(existing: Optional[Dict[str, Any]],
                      incoming: Optional[Dict[str, Any]]
                      ) -> Tuple[Dict[str, Any], bool]:
    """Merge two execution diagnostics by FIELD COMPLETENESS so the
    merged record is independent of delivery order (Req 2.6).

    Returns ``(merged, changed)``. ``changed`` False means the incoming
    evidence was a duplicate or non-increasing — a no-op the caller must
    not persist (idempotency). Later evidence may only INCREASE
    completeness: an available field never regresses to unavailable,
    text never regresses to empty, and identity fields are first-writer
    (they are correlated, so any duplicate carries the same values).
    """
    if not incoming:
        return (dict(existing) if existing else {}, False)
    if not existing:
        return dict(incoming), True

    merged = dict(existing)
    changed = False
    for key, new_value in incoming.items():
        if key == 'source':
            sources = list(merged.get('source') or [])
            for entry in (new_value or []):
                if entry not in sources:
                    sources.append(entry)
                    changed = True
            merged['source'] = sources
            continue
        if key == 'observed_at':
            old = merged.get('observed_at')
            if old is None or (new_value is not None
                               and new_value < old):
                # keep the EARLIEST observation time for determinism
                if merged.get('observed_at') != new_value:
                    merged['observed_at'] = new_value
                    changed = True
            continue
        if key == 'complete':
            if new_value and not merged.get('complete'):
                merged['complete'] = True
                changed = True
            continue
        old_value = merged.get(key)
        if _field_completeness(new_value) > _field_completeness(old_value):
            merged[key] = new_value
            changed = True
    return merged, changed


# ---------------------------------------------------------------------------
# Terminal absorption (Req 2.6/3.1): later evidence can enrich the
# diagnostic but can never resurrect or overwrite terminal state.
# ---------------------------------------------------------------------------

class EvidenceApplication(NamedTuple):
    """Pure decision about applying later evidence to a job view.

    - ``update_diagnostic``: the merged diagnostic to persist (None
      when nothing changed — the observation is a no-op)
    - ``update_status`` / ``update_error_code`` / ``update_ended_at``:
      terminal fields to write, or None. All three are ALWAYS None when
      the job is already terminal (absorption).
    """
    update_diagnostic: Optional[Dict[str, Any]]
    update_status: Optional[str]
    update_error_code: Optional[str]
    update_ended_at: Optional[int]


def apply_evidence(job_status: str,
                   existing_diagnostic: Optional[Dict[str, Any]],
                   incoming_diagnostic: Optional[Dict[str, Any]],
                   classification: Classification,
                   now: int) -> EvidenceApplication:
    """Decide what later evidence may change (Req 2.6):

    - diagnostic completeness may INCREASE at any time, even after
      terminal status;
    - status, error code, and ``ended_at`` are written only when the
      job is NOT already terminal and the classification decided;
    - duplicates / non-increasing evidence produce an all-None no-op.
    """
    merged, changed = merge_diagnostics(existing_diagnostic,
                                        incoming_diagnostic)
    update_diagnostic = merged if changed else None

    if build_domain.is_terminal(job_status) or not classification.decided:
        return EvidenceApplication(
            update_diagnostic=update_diagnostic,
            update_status=None, update_error_code=None,
            update_ended_at=None)
    return EvidenceApplication(
        update_diagnostic=update_diagnostic,
        update_status=classification.status,
        update_error_code=classification.error_code,
        update_ended_at=now)


# ===========================================================================
# Task 4.3 — Timing, execution-attempt, and terminal-effects planning
# ===========================================================================

_MS_PER_MINUTE = 60 * 1000
_MS_PER_HOUR = 60 * _MS_PER_MINUTE

#: Documented compatibility default (mirrors
#: build_planner.DEFAULT_MAX_RUNTIME_HOURS; Req 3.12).
DEFAULT_HARD_RUNTIME_HOURS = 4

# ---------------------------------------------------------------------------
# Stable execution-attempt identity and deterministic command comment
# ---------------------------------------------------------------------------

#: Dispatch states of one execution attempt (design data model).
DISPATCH_CLAIMED = 'claimed'
DISPATCH_SENDING = 'sending'
DISPATCH_SENT = 'sent'
DISPATCH_RECONCILING = 'reconciling'
DISPATCH_TERMINAL = 'terminal'


def command_comment(build_job_id: str, attempt_id: str) -> str:
    """The deterministic SSM command comment binding one command to one
    job/attempt: ``dda-build:<job-id>:<attempt-id>``. Ambiguous-send
    recovery searches recent commands for exactly this marker before any
    resend (design "Ordering, Idempotency")."""
    return 'dda-build:{job}:{attempt}'.format(job=build_job_id,
                                              attempt=attempt_id)


def parse_command_comment(comment: Any) -> Optional[Tuple[str, str]]:
    """Inverse of :func:`command_comment`: ``(job_id, attempt_id)`` or
    None when the comment is not a dda-build marker."""
    if not isinstance(comment, str):
        return None
    parts = comment.split(':')
    if len(parts) != 3 or parts[0] != 'dda-build' \
            or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def new_execution_attempt(build_job_id: str,
                          attempt_id: str,
                          instance_id: Optional[str],
                          claimed_at: int) -> Dict[str, Any]:
    """A stable execution-attempt record binding one job, instance, and
    (later) SSM command. ``attempt_id`` is supplied by the caller (I/O
    layer generates the uuid) so this module stays pure/deterministic."""
    return {
        'attempt_id': attempt_id,
        'dispatch_state': DISPATCH_CLAIMED,
        'instance_id': instance_id,
        'command_id': None,
        'command_comment': command_comment(build_job_id, attempt_id),
        'claimed_at': claimed_at,
        'sent_at': None,
    }


# ---------------------------------------------------------------------------
# Snapshotted runtime budgets (design "Runtime Accounting Model")
# ---------------------------------------------------------------------------

class EffectiveBudget(NamedTuple):
    """The resolved snapshotted runtime budget for one job.

    - ``hard_runtime_ms``: the non-extendable hard safety ceiling
    - ``heartbeat_lease_ms`` / ``progress_stall_ms``: soft leases (None
      when not configured — soft leases are opt-in)
    - ``queue_wait_ms`` / ``provisioning_ms``: independent optional
      phase budgets (None when not configured — disabled)
    - ``source``: which configuration level supplied the hard ceiling:
      'target_mode_override' | 'target_default' |
      'snapshot_max_runtime_hours' | 'compatibility_default'
    """
    hard_runtime_ms: float
    heartbeat_lease_ms: Optional[float]
    progress_stall_ms: Optional[float]
    queue_wait_ms: Optional[float]
    provisioning_ms: Optional[float]
    source: str


def _budget_ms(entry: Dict[str, Any], key: str,
               per: float) -> Optional[float]:
    value = entry.get(key)
    if value is None:
        return None
    try:
        return float(value) * per
    except (TypeError, ValueError):
        return None


def effective_budget(job: Dict[str, Any]) -> EffectiveBudget:
    """Resolve the job's snapshotted runtime budget in design order:
    target/mode override, target default, snapshotted
    ``max_runtime_hours``, then the documented compatibility default.
    Only the job's OWN ``config_snapshot`` is consulted — never current
    configuration. Existing jobs lacking the new shape keep using their
    snapshotted ``max_runtime_hours`` (Req 2.17, 3.12).
    """
    snapshot = job.get('config_snapshot') or {}
    budgets = snapshot.get('runtime_budgets') or {}
    target_map = budgets.get(job.get('build_target')) or {}

    entry = None
    source = None
    mode_entry = target_map.get(job.get('execution_mode'))
    if isinstance(mode_entry, dict):
        entry, source = mode_entry, 'target_mode_override'
    elif isinstance(target_map.get('default'), dict):
        entry, source = target_map['default'], 'target_default'

    heartbeat_lease_ms = progress_stall_ms = None
    queue_wait_ms = provisioning_ms = None
    hard_runtime_ms = None
    if entry is not None:
        hard_runtime_ms = _budget_ms(entry, 'hard_runtime_hours',
                                     _MS_PER_HOUR)
        heartbeat_lease_ms = _budget_ms(entry, 'heartbeat_lease_minutes',
                                        _MS_PER_MINUTE)
        progress_stall_ms = _budget_ms(entry, 'progress_stall_minutes',
                                       _MS_PER_MINUTE)
        queue_wait_ms = _budget_ms(entry, 'queue_wait_hours',
                                   _MS_PER_HOUR)
        provisioning_ms = _budget_ms(entry, 'provisioning_minutes',
                                     _MS_PER_MINUTE)

    if hard_runtime_ms is None:
        max_hours = snapshot.get('max_runtime_hours')
        if max_hours is not None:
            hard_runtime_ms = float(max_hours) * _MS_PER_HOUR
            source = 'snapshot_max_runtime_hours'
        else:
            hard_runtime_ms = DEFAULT_HARD_RUNTIME_HOURS * _MS_PER_HOUR
            source = 'compatibility_default'
    return EffectiveBudget(
        hard_runtime_ms=hard_runtime_ms,
        heartbeat_lease_ms=heartbeat_lease_ms,
        progress_stall_ms=progress_stall_ms,
        queue_wait_ms=queue_wait_ms,
        provisioning_ms=provisioning_ms,
        source=source,
    )


# ---------------------------------------------------------------------------
# Phase clocks (Req 2.14): queue wait, provisioning, and active
# execution are measured separately; queue/provisioning time never
# consumes execution runtime.
# ---------------------------------------------------------------------------

def _timing(job: Dict[str, Any]) -> Dict[str, Any]:
    timing = job.get('timing')
    return timing if isinstance(timing, dict) else {}


def queue_wait_ms(job: Dict[str, Any], now: int) -> Optional[int]:
    """Elapsed queue wait: from ``timing.queue_started_at`` (fallback
    ``created_at``) to ``timing.queue_ended_at`` (fallback
    ``dispatched_at``, else ``now`` while still queued)."""
    timing = _timing(job)
    start = timing.get('queue_started_at')
    if start is None:
        start = job.get('created_at')
    if start is None:
        return None
    end = timing.get('queue_ended_at')
    if end is None:
        end = job.get('dispatched_at')
    if end is None:
        end = now
    return max(0, int(end) - int(start))


def provisioning_ms(job: Dict[str, Any], now: int) -> Optional[int]:
    """Elapsed provisioning time: from
    ``timing.provisioning_started_at`` (fallback ``dispatched_at``) to
    ``timing.provisioning_ended_at`` (fallback ``now`` while still
    provisioning)."""
    timing = _timing(job)
    start = timing.get('provisioning_started_at')
    if start is None:
        start = job.get('dispatched_at')
    if start is None:
        return None
    end = timing.get('provisioning_ended_at')
    if end is None:
        end = now
    return max(0, int(end) - int(start))


def execution_runtime_ms(job: Dict[str, Any], now: int) -> int:
    """Active execution runtime, anchored ONLY on positive execution-
    start evidence (``timing.execution_started_at``). Zero until such
    evidence exists: queue wait and provisioning are never charged to
    execution (Req 2.14/2.15)."""
    timing = _timing(job)
    start = timing.get('execution_started_at')
    if start is None:
        return 0
    end = timing.get('execution_ended_at')
    if end is None:
        end = now
    return max(0, int(end) - int(start))


# ---------------------------------------------------------------------------
# Heartbeat / progress sequences (Req 2.16): monotonic, duplicate and
# non-increasing observations are no-ops.
# ---------------------------------------------------------------------------

PROGRESS_KIND_PHASE = 'phase'
PROGRESS_KIND_CHECKPOINT = 'checkpoint'
PROGRESS_KIND_OUTPUT_GROWTH = 'output_growth'


class ActivityUpdate(NamedTuple):
    """Pure result of observing one heartbeat/progress event.

    - ``accepted``: False means duplicate / non-increasing sequence /
      stale attempt — a no-op the caller must not persist
    - ``timing``: the updated timing map to persist when accepted
    """
    accepted: bool
    timing: Dict[str, Any]


def observe_heartbeat(job: Dict[str, Any],
                      attempt_evidence: Dict[str, Any],
                      sequence: int,
                      observed_at: int) -> ActivityUpdate:
    """Apply one correlated heartbeat. Renews LIVENESS only (never the
    progress lease, never the hard deadline). Non-increasing sequences
    and stale attempts are no-ops."""
    timing = dict(_timing(job))
    if not evidence_matches_attempt(job.get('execution_attempt'),
                                    attempt_evidence):
        return ActivityUpdate(accepted=False, timing=timing)
    last_sequence = timing.get('heartbeat_sequence')
    if last_sequence is not None and sequence <= last_sequence:
        return ActivityUpdate(accepted=False, timing=timing)
    timing['heartbeat_sequence'] = sequence
    timing['last_heartbeat_at'] = observed_at
    return ActivityUpdate(accepted=True, timing=timing)


def observe_progress(job: Dict[str, Any],
                     attempt_evidence: Dict[str, Any],
                     sequence: int,
                     observed_at: int,
                     kind: str = PROGRESS_KIND_OUTPUT_GROWTH
                     ) -> ActivityUpdate:
    """Apply one correlated meaningful-progress event. Renews BOTH the
    progress lease and liveness (design: meaningful progress implies the
    process is alive), but never the hard deadline. Non-increasing
    sequences and stale attempts are no-ops."""
    timing = dict(_timing(job))
    if not evidence_matches_attempt(job.get('execution_attempt'),
                                    attempt_evidence):
        return ActivityUpdate(accepted=False, timing=timing)
    last_sequence = timing.get('progress_sequence')
    if last_sequence is not None and sequence <= last_sequence:
        return ActivityUpdate(accepted=False, timing=timing)
    timing['progress_sequence'] = sequence
    timing['last_progress_at'] = observed_at
    timing['last_progress_kind'] = kind
    heartbeat = timing.get('last_heartbeat_at')
    if heartbeat is None or observed_at > heartbeat:
        timing['last_heartbeat_at'] = observed_at
    return ActivityUpdate(accepted=True, timing=timing)


def observe_execution_start(job: Dict[str, Any],
                            attempt_evidence: Dict[str, Any],
                            observed_at: int) -> ActivityUpdate:
    """Record positive execution-start evidence. First writer wins (a
    duplicate start is a no-op); execution runtime is anchored here and
    ONLY here (Req 2.14)."""
    timing = dict(_timing(job))
    if not evidence_matches_attempt(job.get('execution_attempt'),
                                    attempt_evidence):
        return ActivityUpdate(accepted=False, timing=timing)
    if timing.get('execution_started_at') is not None:
        return ActivityUpdate(accepted=False, timing=timing)
    timing['execution_started_at'] = observed_at
    if timing.get('last_heartbeat_at') is None:
        timing['last_heartbeat_at'] = observed_at
    if timing.get('last_progress_at') is None:
        timing['last_progress_at'] = observed_at
    return ActivityUpdate(accepted=True, timing=timing)


# ---------------------------------------------------------------------------
# Timeout decision (design decideTimeout pseudocode; Req 2.16/2.18)
# ---------------------------------------------------------------------------

TIMEOUT_CONTINUE = 'CONTINUE'
TIMEOUT_WAIT_FOR_EXECUTION_EVIDENCE = 'WAIT_FOR_EXECUTION_EVIDENCE'


class TimingDecision(NamedTuple):
    """Evidence-rich pure timing decision for one job at ``now``.

    - ``timed_out``: True iff a strict deadline was crossed
    - ``classification``: a stable code (QUEUE_WAIT_TIMEOUT /
      PROVISIONING_TIMEOUT / MAX_RUNTIME_EXCEEDED /
      AGENT_HEARTBEAT_EXPIRED / BUILD_PROGRESS_STALLED) when timed out,
      else CONTINUE or WAIT_FOR_EXECUTION_EVIDENCE
    - ``evidence``: phase, observed durations, applicable budget +
      value + source, target/mode, and last-activity fields; fields
      whose evidence does not exist are present with value None
      (identified as unavailable, never fabricated — Req 2.18)
    """
    timed_out: bool
    classification: str
    evidence: Dict[str, Any]


def decide_timing(job: Dict[str, Any], now: int) -> TimingDecision:
    """The design's pure ``decideTimeout`` state machine with strict
    ``now > deadline`` boundaries throughout (Req 3.12) and hard
    ceilings that no heartbeat/progress event can extend (Req 2.16).

    Evaluation precedence (design): explicit queue/provisioning budget
    where applicable, execution hard ceiling, heartbeat lease, then
    progress lease.
    """
    status = job.get('status')
    budget = effective_budget(job)
    timing = _timing(job)
    evidence: Dict[str, Any] = {
        'phase': None,
        'observed_ms': None,
        'queue_wait_ms': queue_wait_ms(job, now),
        'provisioning_ms': provisioning_ms(job, now),
        'execution_runtime_ms': execution_runtime_ms(job, now),
        'budget_ms': None,
        'budget_source': budget.source,
        'hard_runtime_ms': budget.hard_runtime_ms,
        'last_heartbeat_at': timing.get('last_heartbeat_at'),
        'last_progress_at': timing.get('last_progress_at'),
        'last_progress_kind': timing.get('last_progress_kind'),
        'execution_started_at': timing.get('execution_started_at'),
        'build_target': job.get('build_target'),
        'execution_mode': job.get('execution_mode'),
        'evaluated_at': now,
    }

    def _decision(timed_out: bool, classification: str,
                  phase: str, observed: Optional[int],
                  budget_ms: Optional[float]) -> TimingDecision:
        evidence['phase'] = phase
        evidence['observed_ms'] = observed
        evidence['budget_ms'] = budget_ms
        return TimingDecision(timed_out=timed_out,
                              classification=classification,
                              evidence=evidence)

    # Queue wait: expire ONLY under an explicitly snapshotted budget.
    if status == build_domain.STATUS_QUEUED:
        waited = queue_wait_ms(job, now)
        if budget.queue_wait_ms is not None and waited is not None \
                and waited > budget.queue_wait_ms:
            return _decision(True, CODE_QUEUE_WAIT_TIMEOUT,
                             'queue_wait', waited, budget.queue_wait_ms)
        return _decision(False, TIMEOUT_CONTINUE, 'queue_wait', waited,
                         budget.queue_wait_ms)

    # Provisioning: expire ONLY under an explicitly snapshotted budget.
    if status == build_domain.STATUS_PROVISIONING:
        provisioned = provisioning_ms(job, now)
        if budget.provisioning_ms is not None \
                and provisioned is not None \
                and provisioned > budget.provisioning_ms:
            return _decision(True, CODE_PROVISIONING_TIMEOUT,
                             'provisioning', provisioned,
                             budget.provisioning_ms)
        return _decision(False, TIMEOUT_CONTINUE, 'provisioning',
                         provisioned, budget.provisioning_ms)

    if status not in (build_domain.STATUS_BUILDING,
                      build_domain.STATUS_PUBLISHING):
        # Terminal or unknown status: nothing to time out here.
        return _decision(False, TIMEOUT_CONTINUE, 'terminal'
                         if build_domain.is_terminal(status or '')
                         else 'unknown', None, None)

    execution_started_at = timing.get('execution_started_at')
    if execution_started_at is None:
        # No positive execution-start evidence: never charge active
        # runtime; fail-safe WAIT (Req 2.14).
        return _decision(False, TIMEOUT_WAIT_FOR_EXECUTION_EVIDENCE,
                         'execution', 0, budget.hard_runtime_ms)

    observed = execution_runtime_ms(job, now)

    # Hard safety ceiling: strictly non-extendable (Req 2.16/2.17).
    hard_deadline = execution_started_at + budget.hard_runtime_ms
    if now > hard_deadline:
        return _decision(True, CODE_MAX_RUNTIME_EXCEEDED, 'execution',
                         observed, budget.hard_runtime_ms)

    # Heartbeat lease (opt-in): liveness lost.
    last_heartbeat = timing.get('last_heartbeat_at')
    if budget.heartbeat_lease_ms is not None \
            and last_heartbeat is not None \
            and now > last_heartbeat + budget.heartbeat_lease_ms:
        return _decision(True, CODE_AGENT_HEARTBEAT_EXPIRED,
                         'execution', observed,
                         budget.heartbeat_lease_ms)

    # Progress lease (opt-in): liveness without meaningful progress.
    last_progress = timing.get('last_progress_at')
    if budget.progress_stall_ms is not None \
            and last_progress is not None \
            and now > last_progress + budget.progress_stall_ms:
        return _decision(True, CODE_BUILD_PROGRESS_STALLED,
                         'execution', observed,
                         budget.progress_stall_ms)

    return _decision(False, TIMEOUT_CONTINUE, 'execution', observed,
                     budget.hard_runtime_ms)


def hard_deadline_ms(job: Dict[str, Any]) -> Optional[float]:
    """The job's non-extendable hard execution deadline, or None until
    positive execution-start evidence exists."""
    execution_started_at = _timing(job).get('execution_started_at')
    if execution_started_at is None:
        return None
    return execution_started_at + effective_budget(job).hard_runtime_ms


def timeout_evidence_record(decision: TimingDecision,
                            decided_at: int) -> Dict[str, Any]:
    """The persisted safe timing diagnostic for one terminal timeout
    decision (Req 2.18). Pure projection: no raw provider text enters
    this record, so it is safe for every sink."""
    record = dict(decision.evidence)
    record['timeout_kind'] = (decision.classification
                              if decision.timed_out else None)
    record['timeout_decided_at'] = decided_at if decision.timed_out \
        else None
    return record


# ---------------------------------------------------------------------------
# Terminal-effects ledger (Req 2.7/3.11): one stable effect_id, effects
# planned as retryable pure records. NO I/O here — adapters execute.
# ---------------------------------------------------------------------------

EFFECT_PENDING = 'pending'
EFFECT_DONE = 'done'
EFFECT_NOT_APPLICABLE = 'not_applicable'

#: The ledger's effect keys, in the required completion order:
#: verified compute stop/cleanup precedes allocation release, which
#: precedes promotion (stop-before-release, Req 3.11).
EFFECT_AUDIT = 'audit'
EFFECT_COMPUTE_CLEANUP = 'compute_cleanup'
EFFECT_ALLOCATION_RELEASE = 'allocation_release'
EFFECT_PROMOTION_WAKEUP = 'promotion_wakeup'

_LEDGER_EFFECTS = (EFFECT_AUDIT, EFFECT_COMPUTE_CLEANUP,
                   EFFECT_ALLOCATION_RELEASE, EFFECT_PROMOTION_WAKEUP)


def terminal_effect_id(build_job_id: str, attempt_id: str) -> str:
    """The single stable effect identity for one attempt's terminal
    effects: ``<job-id>:<attempt-id>:terminal``. Audit deduplication and
    retryable effect completion key on exactly this value."""
    return '{job}:{attempt}:terminal'.format(job=build_job_id,
                                             attempt=attempt_id)


def plan_terminal_effects(build_job_id: str,
                          attempt_id: str,
                          execution_mode: str,
                          cleanup_required: bool = True
                          ) -> Dict[str, Any]:
    """Plan the terminal-effects ledger for one terminal outcome as
    retryable effects under ONE stable ``effect_id`` (Req 2.7).

    - dedicated mode: compute stop/cleanup must be VERIFIED before the
      allocation release becomes executable; release precedes promotion
    - ephemeral mode: runner cleanup replaces allocation release
    - ``cleanup_required`` False (e.g. a job that never dispatched
      compute) marks cleanup not_applicable
    """
    dedicated = execution_mode == build_domain.EXECUTION_MODE_DEDICATED
    return {
        'effect_id': terminal_effect_id(build_job_id, attempt_id),
        EFFECT_AUDIT: EFFECT_PENDING,
        EFFECT_COMPUTE_CLEANUP: (EFFECT_PENDING if cleanup_required
                                 else EFFECT_NOT_APPLICABLE),
        EFFECT_ALLOCATION_RELEASE: (EFFECT_PENDING if dedicated
                                    else EFFECT_NOT_APPLICABLE),
        EFFECT_PROMOTION_WAKEUP: EFFECT_PENDING,
    }


class EffectAdvance(NamedTuple):
    """Pure decision about completing one ledger effect.

    - ``allowed``: False means the completion is out of order (e.g.
      releasing the allocation before verified cleanup) or a duplicate;
      the ledger is returned unchanged
    - ``ledger``: the (possibly updated) ledger to persist
    - ``reason``: why a completion was refused (safe text)
    """
    allowed: bool
    ledger: Dict[str, Any]
    reason: Optional[str]


def advance_effect(ledger: Dict[str, Any], effect: str) -> EffectAdvance:
    """Mark one effect done, enforcing ordering and idempotency:

    - an unknown effect or a not_applicable effect cannot be completed;
    - completing an effect twice is a refused duplicate (retries may
      complete a PENDING effect but cannot create a second logical
      effect — audit deduplication, Req 2.7);
    - ``allocation_release`` requires ``compute_cleanup`` to be done or
      not_applicable first (verified stop before release, Req 3.11);
    - ``promotion_wakeup`` requires ``allocation_release`` to be done
      or not_applicable first.
    """
    current = dict(ledger)
    state = current.get(effect)
    if effect not in _LEDGER_EFFECTS:
        return EffectAdvance(False, current,
                             'unknown effect {0!r}'.format(effect))
    if state == EFFECT_NOT_APPLICABLE:
        return EffectAdvance(False, current,
                             '{0} is not applicable'.format(effect))
    if state == EFFECT_DONE:
        return EffectAdvance(False, current,
                             '{0} already done (duplicate)'.format(effect))
    if effect == EFFECT_ALLOCATION_RELEASE and \
            current.get(EFFECT_COMPUTE_CLEANUP) == EFFECT_PENDING:
        return EffectAdvance(
            False, current,
            'allocation release requires verified compute cleanup '
            'first (stop-before-release)')
    if effect == EFFECT_PROMOTION_WAKEUP and \
            current.get(EFFECT_ALLOCATION_RELEASE) == EFFECT_PENDING:
        return EffectAdvance(
            False, current,
            'promotion requires the allocation release first')
    current[effect] = EFFECT_DONE
    return EffectAdvance(True, current, None)


def pending_effects(ledger: Dict[str, Any]) -> List[str]:
    """The effects still pending, in required completion order. Retries
    re-drive exactly these; an empty list means the terminal outcome is
    fully settled."""
    return [effect for effect in _LEDGER_EFFECTS
            if ledger.get(effect) == EFFECT_PENDING]
