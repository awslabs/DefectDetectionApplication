"""Property test for the invalid-token rate limiter (station-quick-setup task 2.4).

**Feature: station-quick-setup, Property 11: The invalid-token rate limiter matches its specification**

For any sequence of timestamped requests from arbitrary source IPs with
arbitrary valid/invalid outcomes, the rate limiter blocks a source IP if and
only if it exceeded 10 invalid-token requests within the current 5-minute
window, every block lasts at least 5 minutes from its imposition, and requests
from other IPs are never affected.

**Validates: Requirements 3.9**

Pure-core test: only ``rate_limiter.evaluate`` (a side-effect-free decision
function) is exercised, so no AWS is required. The implementation is driven
per source IP and compared, event by event, against an independently written
reference model of the specified behavior. Three named invariants are also
asserted directly: block-iff-exceeded, minimum block duration, and per-IP
isolation.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rate_limiter import BLOCK, MAX_INVALID, WINDOW, RateState, evaluate

# A small pool of distinct source IPs so streams realistically interleave
# several IPs and exercise the "other IPs are never affected" claim.
IPS = ["203.0.113.1", "203.0.113.2", "198.51.100.7", "192.0.2.42"]

BASE_TIME = 1_700_000_000


# ---------------------------------------------------------------------------
# Independent reference model of the specified behavior.
#
# Written from the plain-language spec rather than mirroring the implementation:
# it tracks the counted-invalid timestamps of the current window in a list
# (length == count) and an explicit block deadline, deciding admission from
# those directly.
# ---------------------------------------------------------------------------
class ReferenceLimiter:
    """Reference decision model for a single source IP (Req 3.9)."""

    def __init__(self):
        self.window_start = None          # epoch the current window opened
        self.invalid_in_window = []       # counted invalid attempts this window
        self.blocked_until = 0            # epoch the block lifts (0 == open)

    def step(self, now: int, invalid: bool) -> bool:
        # An in-force block rejects every request and disturbs no counters, so
        # the block spans its full duration from imposition.
        if self.blocked_until > now:
            return False

        # A fresh window opens when none exists yet or the 5-minute window has
        # elapsed since it opened.
        if self.window_start is None or (now - self.window_start) >= WINDOW:
            self.window_start = now
            self.invalid_in_window = []

        if invalid:
            self.invalid_in_window.append(now)
            # Strictly more than the allowance within the window trips a block.
            if len(self.invalid_in_window) > MAX_INVALID:
                self.blocked_until = now + BLOCK

        return self.blocked_until <= now


# ---------------------------------------------------------------------------
# Strategies: streams of (ip, time-delta, invalid) events. Cumulative,
# non-negative deltas keep timestamps monotonic (as real request arrivals
# are); the delta range straddles both the window and block boundaries so the
# stream reliably crosses resets and block expiries.
# ---------------------------------------------------------------------------
_events = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=len(IPS) - 1),   # source IP index
        st.integers(min_value=0, max_value=WINDOW + BLOCK + 30),  # time delta
        st.booleans(),                                       # invalid token?
    ),
    min_size=0,
    max_size=120,
)


@settings(max_examples=100, deadline=None)
@given(events=_events)
def test_invalid_token_rate_limiter_matches_specification(events):
    """**Feature: station-quick-setup, Property 11: The invalid-token rate limiter matches its specification**

    **Validates: Requirements 3.9**
    """
    impl_states: dict[str, RateState | None] = {}
    references: dict[str, ReferenceLimiter] = {}

    # Per-IP record of (now, invalid, allowed) as seen in the interleaved run,
    # used afterwards to prove decisions depend on that IP's stream alone.
    per_ip_stream: dict[str, list[tuple[int, bool, bool]]] = {}

    now = BASE_TIME
    for ip_index, delta, invalid in events:
        now += delta
        ip = IPS[ip_index]

        prev = impl_states.get(ip)
        next_state, allowed = evaluate(prev, now, invalid)
        impl_states[ip] = next_state

        # 1. Implementation agrees with the reference model event by event.
        ref_allowed = references.setdefault(ip, ReferenceLimiter()).step(now, invalid)
        assert allowed == ref_allowed, (
            f"decision mismatch for {ip} at t={now} invalid={invalid}: "
            f"impl={allowed} ref={ref_allowed}"
        )

        # 2. Minimum block duration: a block newly imposed on this event must
        #    last at least BLOCK seconds and reject the tripping request itself.
        was_blocking = prev is not None and prev.blocked_until > now
        newly_blocked = (not was_blocking) and next_state.blocked_until > now
        if newly_blocked:
            assert next_state.blocked_until - now >= BLOCK
            assert allowed is False

        # 3. While a block imposed earlier is still in force, every request is
        #    rejected without altering the counters (block persists >= 5 min).
        if was_blocking:
            assert allowed is False
            assert next_state == prev

        per_ip_stream.setdefault(ip, []).append((now, invalid, allowed))

    # 4. Per-IP isolation: replaying each IP's own subsequence in isolation
    #    reproduces exactly the decisions it received in the interleaved run,
    #    so events from other IPs never affected it.
    for ip, stream in per_ip_stream.items():
        isolated_state: RateState | None = None
        for event_time, invalid, expected_allowed in stream:
            isolated_state, isolated_allowed = evaluate(
                isolated_state, event_time, invalid
            )
            assert isolated_allowed == expected_allowed, (
                f"isolation violated for {ip} at t={event_time}"
            )


def test_reference_and_impl_agree_on_a_known_trip_sequence():
    """Anchor example: the 11th invalid token in a window trips a >=5-minute
    block, and the block rejects requests for the full window (Req 3.9)."""
    now = BASE_TIME
    state = None
    # First MAX_INVALID (10) invalid tokens stay allowed (10 is not > 10).
    for _ in range(MAX_INVALID):
        state, allowed = evaluate(state, now, invalid_attempt=True)
        assert allowed is True
    # The 11th invalid token within the window trips the block and is itself
    # rejected.
    state, allowed = evaluate(state, now, invalid_attempt=True)
    assert allowed is False
    assert state.blocked_until == now + BLOCK
    # Any request just before the block lifts is rejected; the count is frozen.
    _next, allowed = evaluate(state, now + BLOCK - 1, invalid_attempt=False)
    assert allowed is False
    # At/after the block deadline a fresh window opens and requests are allowed.
    _next, allowed = evaluate(state, now + BLOCK, invalid_attempt=False)
    assert allowed is True
