"""Property test for Setup_Token expiration bound (station-quick-setup task 2.2).

**Feature: station-quick-setup, Property 6: Token expiration is bounded**

For any token issuance (creation or regeneration) at any clock time, the
stored expiration is at most 90 minutes after the issuance time.

**Validates: Requirements 3.1**

`TokenService.generate_token` is the single issuance path used by both
`device_registrations.py` (initial creation) and its regeneration route, so
exercising it across arbitrary registration ids and arbitrary clock times
covers both issuance cases. The function is pure with respect to its `now`
argument, so no AWS is required.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import token_service as ts

# The Requirement 3.1 upper bound, stated independently of the module's own
# constant so the test pins the 90-minute contract rather than tautologically
# echoing TOKEN_TTL_SECONDS.
NINETY_MINUTES = 90 * 60

# Registration ids are uuid4 strings in production; the token wire format only
# requires a non-empty field with no "." separator. Generate a representative
# range of such ids.
registration_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-",
    min_size=1,
    max_size=64,
)

# Issuance clock times, from the epoch to well beyond the year 2200, covering
# small and very large timestamps.
issuance_times = st.integers(min_value=0, max_value=7_258_118_400)


@settings(max_examples=200, deadline=None)
@given(registration_id=registration_ids, now=issuance_times)
def test_token_expiration_is_bounded(registration_id, now):
    """**Feature: station-quick-setup, Property 6: Token expiration is bounded**

    For any registration id and any issuance time, the expiration returned by
    `generate_token` is at most 90 minutes after that issuance time.

    **Validates: Requirements 3.1**
    """
    _token, _token_hash, expires_at = ts.generate_token(registration_id, now=now)

    # Core property: expiration never exceeds the 90-minute bound (Req 3.1).
    assert expires_at <= now + NINETY_MINUTES
    # It is also strictly in the future of issuance (a usable, non-degenerate
    # lifetime), so the bound is not satisfied by a zero/negative TTL.
    assert expires_at > now
