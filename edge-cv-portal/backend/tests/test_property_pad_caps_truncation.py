"""Property test for build-time caps truncation (task 4.2).

**Feature: port-guidance-and-pad-prepopulation, Property 9: Caps truncation is bounded and marked**

For any string, `truncate_caps` returns a prefix of the input of at most
4096 characters together with a flag that is true iff the input exceeds
4096 characters; when the flag is true the returned string is exactly
4096 characters long.

**Validates: Requirements 3.4**

The function under test lives in the executable build-image script
`plugin-build-images/dda-gst-introspect` (no `.py` extension). Its
module-top-level code is deliberately GI-free (the `gi` imports stay
inside `scan()`), so the script is loaded here via a SourceFileLoader
and `truncate_caps` is exercised directly as a pure function.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Load the introspection script as a module (top level is GI-free).
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / 'plugin-build-images' / 'dda-gst-introspect'
)


def _load_introspect_module():
    loader = importlib.machinery.SourceFileLoader(
        'dda_gst_introspect', str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_loader('dda_gst_introspect', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_introspect = _load_introspect_module()
truncate_caps = _introspect.truncate_caps
MAX_CAPS_LEN = _introspect.MAX_CAPS_LEN


# ---------------------------------------------------------------------------
# Generators: caps strings concentrated around the truncation boundary.
# ---------------------------------------------------------------------------

# Short everyday caps strings, exact-boundary lengths (MAX_CAPS_LEN - 1,
# MAX_CAPS_LEN, MAX_CAPS_LEN + 1), and clearly oversized inputs.
_boundary_lengths = st.integers(
    min_value=MAX_CAPS_LEN - 2, max_value=MAX_CAPS_LEN + 2)
_oversized_lengths = st.integers(
    min_value=MAX_CAPS_LEN + 1, max_value=MAX_CAPS_LEN + 512)

_caps_strings = st.one_of(
    st.text(max_size=80),
    _boundary_lengths.flatmap(
        lambda n: st.text(min_size=n, max_size=n)),
    _oversized_lengths.flatmap(
        lambda n: st.text(min_size=n, max_size=n)),
)


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None)
@given(caps=_caps_strings)
def test_caps_truncation_is_bounded_and_marked(caps):
    """**Feature: port-guidance-and-pad-prepopulation, Property 9: Caps truncation is bounded and marked**

    For any string, `truncate_caps` returns a prefix of the input of at
    most MAX_CAPS_LEN (4096) characters, the truncation flag is true iff
    the input exceeds MAX_CAPS_LEN characters, and when the flag is true
    the returned string is exactly MAX_CAPS_LEN characters long
    (Requirement 3.4).

    **Validates: Requirements 3.4**
    """
    result, truncated = truncate_caps(caps)

    # The result is a prefix of the input of at most MAX_CAPS_LEN chars.
    assert isinstance(result, str)
    assert len(result) <= MAX_CAPS_LEN
    assert caps.startswith(result)

    # The flag is true iff the input exceeds MAX_CAPS_LEN characters.
    assert isinstance(truncated, bool)
    assert truncated == (len(caps) > MAX_CAPS_LEN)

    # When truncation occurred, exactly MAX_CAPS_LEN characters remain;
    # otherwise the input is returned unchanged.
    if truncated:
        assert len(result) == MAX_CAPS_LEN
        assert result == caps[:MAX_CAPS_LEN]
    else:
        assert result == caps
