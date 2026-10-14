"""
Property-based test for the Token_Budget_Resolver
(layers/shared/python/dda_llm_request.py :: resolve_token_budget).

Spec: llm-model-token-and-image-sizing, task 2.2.

**Feature: llm-model-token-and-image-sizing, Property 1: Output token budget resolution is total and safe**
**Validates: Requirements 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10**

The function under test is pure (no boto3, no Pillow, no I/O), so this
file needs no moto fixtures and no AWS at all — conftest.py already
places the shared layer on sys.path. Each property runs at 100 examples
via its own `@settings`, which takes precedence over the profile
default.

Per the design's test strategy:

- The Token_Budget_Selection and the Model_Token_Limits *entry* are
  drawn, independently, from the full any-type pool: None, booleans,
  integers (with `0`, `1`, `128000`, `128001` and negatives explicitly
  pinned via `@example` so the range boundaries and the no-clamping
  rule are hit on every run), floats including NaN and the infinities,
  arbitrary text, digit-only strings from `st.from_regex(r'\\d{1,6}')`,
  binary, lists and dictionaries.
- The mapping itself is drawn from None / dict-without-the-entry /
  dict-with-the-entry / non-mapping (list, text, integer, bytes).
- The identifier is drawn from text / None / integers / booleans /
  floats / tuples / bytes.
- Each example deep-copies its inputs, evaluates the resolver twice,
  and asserts: the returned range, the three-tier outcome recomputed
  independently in the test (with the literals 10000 and 128000, not
  the module constants), input equality after the calls, and equality
  of the two evaluations.
- The non-string-identifier tier passes a mapping subclass recording
  `get` / `__getitem__` calls and asserts zero lookups — while a valid
  Token_Budget_Selection is still returned, never discarded
  (Requirement 2.10's deliberate divergence from
  `resolve_model_image_limit`).
"""
import copy

from hypothesis import example, given, settings
from hypothesis import strategies as st

from dda_llm_request import (
    MODEL_TOKEN_LIMIT_CEILING,
    MODEL_TOKEN_LIMIT_DEFAULT,
    resolve_token_budget,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Integers spanning the range boundaries: below, at and just past both
# ends, plus negatives and far-out values (no clamping — Req 2.9).
_boundary_integers = st.sampled_from(
    (0, 1, 2, 9999, 10000, 10001, 127999, 128000, 128001,
     -1, -128000, 500000))

# The full any-type pool for a Token_Budget_Selection or a
# Model_Token_Limits entry (Req 2.1: "value of any type").
_any_value = st.one_of(
    st.none(),
    st.booleans(),
    _boundary_integers,
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=8),
    st.from_regex(r'\d{1,6}', fullmatch=True),   # digit-only strings (Req 2.8)
    st.binary(max_size=8),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)

# Model identifiers of any type (Req 2.1, 2.10). All branches are
# hashable so an entry can be planted under the identifier itself.
_string_identifiers = st.one_of(
    st.text(max_size=16),
    st.sampled_from((
        'us.amazon.nova-pro-v1:0',
        'anthropic.claude-3-5-sonnet-20241022-v2:0',
        '',
        ' us.amazon.nova-pro-v1:0 ',   # trimming would find the untrimmed key
        'US.AMAZON.NOVA-PRO-V1:0',     # case folding would find the lower key
    )),
)
_non_string_identifiers = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=8),
    st.tuples(st.text(max_size=4)),
)
_identifiers = st.one_of(_string_identifiers, _non_string_identifiers)

_non_mappings = st.one_of(
    st.lists(st.integers(), max_size=3),
    st.text(max_size=8),
    st.integers(),
    st.binary(max_size=4),
)


@st.composite
def _resolution_cases(draw):
    """(model_identifier, token_budget_selection, limits) covering every
    Model_Token_Limits configuration shape the property names: absent,
    non-mapping, mapping without the entry, mapping with the entry."""
    identifier = draw(_identifiers)
    selection = draw(_any_value)
    kind = draw(st.sampled_from(
        ('absent', 'non_mapping', 'missing_entry', 'with_entry')))
    if kind == 'absent':
        limits = None
    elif kind == 'non_mapping':
        limits = draw(_non_mappings)
    else:
        limits = draw(st.dictionaries(st.text(max_size=4), _any_value,
                                      max_size=3))
        if kind == 'missing_entry':
            limits = {key: value for key, value in limits.items()
                      if key != identifier}
        else:
            # Plant an entry under the identifier itself — for a
            # non-string identifier this is exactly the entry the
            # resolver must never look up (Req 2.10).
            limits[identifier] = draw(_any_value)
    return identifier, selection, limits


# ---------------------------------------------------------------------------
# The oracle: the three tiers recomputed independently, with literals
# ---------------------------------------------------------------------------

def _is_valid_token_value(value):
    """In-range non-boolean integer, per the requirement's own words —
    literals, not the module constants (Req 2.2, 2.5, 2.8, 2.9)."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= 128000)


def _expected_budget(identifier, selection, limits):
    """Tier 1: valid selection. Tier 2: valid configured entry, only for
    a string identifier and a mapping. Tier 3: 10000 (Req 2.2-2.4,
    2.7, 2.10)."""
    if _is_valid_token_value(selection):
        return selection
    if isinstance(identifier, str) and isinstance(limits, dict):
        if _is_valid_token_value(limits.get(identifier)):
            return limits[identifier]
    return 10000


def _values_equal(a, b):
    """Deep equality that never confuses types (bool vs int) and treats
    a NaN as equal to itself, so input snapshots compare cleanly."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return (a != a and b != b) or a == b
    if isinstance(a, dict):
        return (len(a) == len(b)
                and all(_values_equal(ka, kb) and _values_equal(va, vb)
                        for (ka, va), (kb, vb)
                        in zip(a.items(), b.items())))
    if isinstance(a, (list, tuple)):
        return (len(a) == len(b)
                and all(_values_equal(x, y) for x, y in zip(a, b)))
    return a == b


# ---------------------------------------------------------------------------
# Property 1 — the resolution algebra over the full input space
# ---------------------------------------------------------------------------

# Feature: llm-model-token-and-image-sizing, Property 1: Output token
# budget resolution is total and safe
@settings(max_examples=100)
# Range boundaries and no-clamping (Req 2.2, 2.9):
@example(case=('m', 1, None))
@example(case=('m', 128000, None))
@example(case=('m', 0, {'m': 0}))
@example(case=('m', 128001, {'m': 128001}))
@example(case=('m', -1, {'m': -128000}))
# Booleans invalid at both tiers, never converted to 1/0 (Req 2.5):
@example(case=('m', True, {'m': True}))
@example(case=('m', False, {'m': False}))
# Digit-only strings and whole-valued floats, no conversion (Req 2.8):
@example(case=('m', '10000', {'m': '10000'}))
@example(case=('m', 10000.0, {'m': 10000.0}))
# Tier 2 wins only when tier 1 is invalid (Req 2.3):
@example(case=('m', None, {'m': 128000}))
@example(case=('m', None, {'m': 1}))
# Exact string comparison: no trimming, no case folding (Req 1.1):
@example(case=(' m ', None, {'m': 20000}))
@example(case=('M', None, {'m': 20000}))
# Everything invalid or absent -> the default (Req 1.2, 2.4, 2.7):
@example(case=(None, None, None))
@example(case=('m', None, {}))
@example(case=('m', None, 'not-a-mapping'))
# Non-string identifier keeps a valid selection (Req 2.10):
@example(case=(None, 42, {None: 99999}))
@example(case=(7, None, {7: 20000}))
@given(case=_resolution_cases())
def test_property_token_budget_resolution_is_total_and_safe(case):
    """
    **Feature: llm-model-token-and-image-sizing, Property 1: Output
    token budget resolution is total and safe**

    For any model identifier (including non-string values), any
    Token_Budget_Selection (absent, null, boolean, string, float,
    negative, zero, in-range integer, above-ceiling integer) and any
    Model_Token_Limits configuration (absent, non-mapping, missing
    entry, boolean entry, string entry, float entry, out-of-range
    entry, in-range entry), the Token_Budget_Resolver returns an
    integer between 1 and 128000 inclusive, returns the
    Token_Budget_Selection whenever that value is an in-range
    non-boolean integer, otherwise returns the configured entry
    whenever that entry is an in-range non-boolean integer, otherwise
    returns 10000, neither converts nor clamps any invalid value,
    leaves its inputs unmodified, and returns the identical value on
    repeated evaluation.

    **Validates: Requirements 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7,
    2.8, 2.9, 2.10**
    """
    identifier, selection, limits = case

    # Snapshot the inputs before any call (Req 2.6: left unmodified).
    identifier_snapshot = copy.deepcopy(identifier)
    selection_snapshot = copy.deepcopy(selection)
    limits_snapshot = copy.deepcopy(limits)

    # Total: both evaluations complete without raising (Req 2.1).
    first = resolve_token_budget(identifier, selection, limits)
    second = resolve_token_budget(identifier, selection, limits)

    # Range: a genuine integer in [1, 128000], never a bool (Req 2.1).
    assert isinstance(first, int) and not isinstance(first, bool)
    assert 1 <= first <= 128000

    # The three-tier outcome, recomputed independently. Equality here
    # is also what rules out conversion and clamping: a clamped 128001
    # or a converted '10000' / 10000.0 / True could not match the
    # oracle's fall-through (Req 1.2, 2.2, 2.3, 2.4, 2.5, 2.7, 2.8,
    # 2.9, 2.10).
    assert first == _expected_budget(identifier_snapshot,
                                     selection_snapshot,
                                     limits_snapshot)

    # Deterministic on repeated evaluation (Req 2.6).
    assert second == first

    # Inputs unmodified by either evaluation (Req 2.6).
    assert _values_equal(identifier, identifier_snapshot)
    assert _values_equal(selection, selection_snapshot)
    assert _values_equal(limits, limits_snapshot)


# ---------------------------------------------------------------------------
# Property 1, the non-string-identifier tier: zero mapping lookups
# ---------------------------------------------------------------------------

class _RecordingLimits(dict):
    """A Model_Token_Limits mapping that records every `get` /
    `__getitem__` call, so the non-string-identifier tier can assert
    the lookup never happens (Req 2.10)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lookups = []

    def get(self, key, default=None):
        self.lookups.append(('get', key))
        return super().get(key, default)

    def __getitem__(self, key):
        self.lookups.append(('getitem', key))
        return super().__getitem__(key)


# Feature: llm-model-token-and-image-sizing, Property 1: Output token
# budget resolution is total and safe
@settings(max_examples=100)
@example(identifier=None, selection=42, entry=99999)
@example(identifier=7, selection=None, entry=20000)
@example(identifier=True, selection=128001, entry=1)
@example(identifier=b'us.amazon.nova-pro-v1:0', selection=None, entry=128000)
@given(identifier=_non_string_identifiers, selection=_any_value,
       entry=st.integers(min_value=1, max_value=128000))
def test_property_non_string_identifier_performs_no_lookup(
        identifier, selection, entry):
    """
    **Feature: llm-model-token-and-image-sizing, Property 1: Output
    token budget resolution is total and safe**

    For any non-string model identifier, the Token_Budget_Resolver
    performs no Model_Token_Limits lookup at all — asserted with a
    mapping subclass recording `get` / `__getitem__` — and still
    returns a valid Token_Budget_Selection rather than discarding it,
    falling back to 10000 otherwise, even though the mapping holds a
    valid entry under that very identifier. This is the deliberate
    divergence from `resolve_model_image_limit`.

    **Validates: Requirements 2.1, 2.6, 2.10**
    """
    # A valid entry planted under the identifier itself, plus noise:
    # the only way to return it would be the forbidden lookup.
    limits = _RecordingLimits({identifier: entry, 'noise-key': 11111})
    limits_snapshot = dict(limits)
    # Snapshot construction must not count as resolver lookups.
    limits.lookups.clear()

    first = resolve_token_budget(identifier, selection, limits)
    second = resolve_token_budget(identifier, selection, limits)

    # Zero lookups on the mapping for a non-string identifier (Req 2.10).
    assert limits.lookups == []

    # A valid selection is returned, not discarded; otherwise the
    # default — never the planted entry (Req 2.10).
    if _is_valid_token_value(selection):
        assert first == selection
    else:
        assert first == 10000

    assert second == first
    assert isinstance(first, int) and not isinstance(first, bool)
    assert 1 <= first <= 128000
    # The mapping's contents are untouched (Req 2.6).
    assert _values_equal(dict(limits), limits_snapshot)


# ---------------------------------------------------------------------------
# The published constants themselves (Req 1.2, 2.1)
# ---------------------------------------------------------------------------

def test_module_constants_match_the_specified_default_and_ceiling():
    """Model_Token_Limit_Default is 10000 and Model_Token_Limit_Ceiling
    is 128000 — the literals every tier above was checked against.

    **Validates: Requirements 1.2, 2.1**
    """
    assert MODEL_TOKEN_LIMIT_DEFAULT == 10000
    assert MODEL_TOKEN_LIMIT_CEILING == 128000
