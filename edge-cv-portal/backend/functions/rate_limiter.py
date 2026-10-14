"""
Invalid-token rate limiter for the Quick_Setup_Endpoint.

Implements Requirement 3.9 of the station-quick-setup feature:

    IF more than 10 requests with invalid Setup_Tokens are received from the
    same source IP within a 5-minute window, THEN THE Quick_Setup_Endpoint
    SHALL reject subsequent requests from that source IP for at least 5
    minutes.

The module is split into two layers:

  * A **pure decision core** -- :func:`evaluate` -- that carries all of the
    security-critical logic as a side-effect-free function of
    ``(previous state, clock, whether this request presented an invalid
    token)`` and returns ``(next state, allowed?)``. Being pure it is
    trivially property-testable against a reference model (Property 11).

  * A thin **DynamoDB persistence layer** that loads and stores the per
    source-IP counters as ``RATELIMIT#{source_ip}`` items in the
    device-registrations table, carrying a ``ttl`` attribute so stale
    counters auto-expire (DynamoDB TTL). Registration items never carry the
    ``ttl`` attribute, so the two record kinds share the table without
    colliding.
"""
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()

# --- Requirement 3.9 constants -------------------------------------------
# Sliding-window length, the invalid-token allowance inside a window, and the
# minimum block duration once the allowance is exceeded. All in seconds.
WINDOW = 300        # 5-minute window
MAX_INVALID = 10    # block iff *more than* 10 invalid tokens in the window
BLOCK = 300         # block lasts at least 5 minutes

# Partition-key prefix that distinguishes rate-limit items from registration
# items in the shared device-registrations table.
RATELIMIT_PREFIX = "RATELIMIT#"

# How long a rate-limit item should linger before DynamoDB TTL reaps it. It
# must outlive both the active window and any block started at the very end of
# that window, so cleanup never races an in-force block.
_TTL_SECONDS = WINDOW + BLOCK

dynamodb = boto3.resource("dynamodb")


class RateState:
    """Immutable per-source-IP rate-limit counters.

    ``window_start``  epoch second the current window opened.
    ``invalid_count`` invalid-token requests counted in the current window.
    ``blocked_until`` epoch second the block lifts (0 == not blocked).
    """

    __slots__ = ("window_start", "invalid_count", "blocked_until")

    def __init__(self, window_start, invalid_count, blocked_until):
        object.__setattr__(self, "window_start", int(window_start))
        object.__setattr__(self, "invalid_count", int(invalid_count))
        object.__setattr__(self, "blocked_until", int(blocked_until))

    def __setattr__(self, *_args):  # frozen
        raise AttributeError("RateState is immutable")

    def __delattr__(self, *_args):  # frozen
        raise AttributeError("RateState is immutable")

    def __eq__(self, other):
        return (
            isinstance(other, RateState)
            and self.window_start == other.window_start
            and self.invalid_count == other.invalid_count
            and self.blocked_until == other.blocked_until
        )

    def __hash__(self):
        return hash((self.window_start, self.invalid_count, self.blocked_until))

    def __repr__(self):
        return (
            f"RateState(window_start={self.window_start}, "
            f"invalid_count={self.invalid_count}, "
            f"blocked_until={self.blocked_until})"
        )


def evaluate(state, now, invalid_attempt):
    """Pure decision core for the invalid-token rate limiter.

    Args:
        state: the previously persisted :class:`RateState`, or ``None`` when
            no counter exists yet for this source IP.
        now: the current time in epoch seconds.
        invalid_attempt: ``True`` when the request being evaluated presented an
            invalid Setup_Token, ``False`` for an otherwise-countable request
            (e.g. a valid token or a pre-validation admission check).

    Returns:
        ``(next_state, allowed)`` where ``next_state`` is the state to persist
        and ``allowed`` is ``False`` when the request must be rejected because
        the source IP is (or has just become) blocked.
    """
    # An in-force block rejects every request without disturbing the counters,
    # so the block always lasts at least BLOCK seconds from when it started.
    if state is not None and state.blocked_until > now:
        return state, False

    # No prior state, or the previous window has elapsed (a block that has
    # since expired implies its window elapsed too): open a fresh window.
    if state is None or (now - state.window_start) >= WINDOW:
        window_start = now
        invalid_count = 0
    else:
        window_start = state.window_start
        invalid_count = state.invalid_count

    blocked_until = 0
    if invalid_attempt:
        invalid_count += 1
        # Strictly *more than* MAX_INVALID invalid tokens trips the block.
        if invalid_count > MAX_INVALID:
            blocked_until = now + BLOCK

    next_state = RateState(
        window_start=window_start,
        invalid_count=invalid_count,
        blocked_until=blocked_until,
    )
    # The tripping request is itself rejected, so the very next request is the
    # first "subsequent" one and the block covers the full BLOCK window.
    allowed = blocked_until <= now
    return next_state, allowed


# --- DynamoDB persistence layer ------------------------------------------

def _table():
    """The device-registrations table (read lazily so tests can set the env)."""
    table_name = os.environ.get("REGISTRATIONS_TABLE")
    if not table_name:
        raise RuntimeError("REGISTRATIONS_TABLE environment variable is not set")
    return dynamodb.Table(table_name)


def _pk(source_ip):
    return f"{RATELIMIT_PREFIX}{source_ip}"


def load_state(source_ip):
    """Load the persisted :class:`RateState` for a source IP, or ``None``."""
    response = _table().get_item(Key={"registration_id": _pk(source_ip)})
    item = response.get("Item")
    if not item:
        return None
    return RateState(
        window_start=item.get("window_start", 0),
        invalid_count=item.get("invalid_count", 0),
        blocked_until=item.get("blocked_until", 0),
    )


def save_state(source_ip, state, now):
    """Persist ``state`` for a source IP with a refreshed TTL for auto-cleanup."""
    _table().put_item(
        Item={
            "registration_id": _pk(source_ip),
            "window_start": state.window_start,
            "invalid_count": state.invalid_count,
            "blocked_until": state.blocked_until,
            "ttl": now + _TTL_SECONDS,
        }
    )


def check(source_ip, now):
    """Return whether a request from ``source_ip`` is currently allowed.

    A read-only admission check for the start of the request pipeline: it does
    not count the request. Returns ``False`` only while a block is in force.
    Any storage failure is surfaced to the caller so the pipeline can reject
    rather than proceed with an unverified limiter state (Req 3.10).
    """
    state = load_state(source_ip)
    _next, allowed = evaluate(state, now, invalid_attempt=False)
    return allowed


def record_invalid(source_ip, now):
    """Count an invalid-token request from ``source_ip`` and persist the result.

    Returns the ``allowed`` flag for this request: ``False`` when this request
    trips the block (or arrives while already blocked).
    """
    state = load_state(source_ip)
    next_state, allowed = evaluate(state, now, invalid_attempt=True)
    try:
        save_state(source_ip, next_state, now)
    except ClientError:
        # Best-effort persistence: a failed counter write must not crash the
        # request path. The admission check on the next request re-reads state.
        logger.exception("Failed to persist rate-limit state for %s", source_ip)
    return allowed
