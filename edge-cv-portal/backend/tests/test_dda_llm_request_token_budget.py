"""
Example-based unit tests for the Token_Budget_Resolver
(layers/shared/python/dda_llm_request.py :: resolve_token_budget).

Spec: llm-model-token-and-image-sizing, task 2.5.
Requirements: 2.2, 2.3, 2.4, 2.5, 2.8, 2.9, 2.10

The function under test is pure (no boto3, no Pillow, no I/O), so these
tests need no moto fixtures — conftest.py already places the shared
layer on sys.path. The exhaustive any-type sweep lives in the sibling
property test (test_property_token_budget_resolution.py); these are the
named examples the design calls out, pinned so a regression names the
exact tier and boundary it broke.
"""
from decimal import Decimal

import pytest

from dda_llm_request import (
    MODEL_IMAGE_LIMIT_DEFAULT,
    MODEL_TOKEN_LIMIT_CEILING,
    MODEL_TOKEN_LIMIT_DEFAULT,
    resolve_model_image_limit,
    resolve_token_budget,
)

MODEL = 'us.amazon.nova-pro-v1:0'

# A mapping-tier value distinct from every boundary literal and from the
# default, so a test that falls through to it cannot be confused with
# one that returns the selection or the default.
CONFIGURED = 20000


def test_constants_pin_the_documented_values():
    assert MODEL_TOKEN_LIMIT_DEFAULT == 10000
    assert MODEL_TOKEN_LIMIT_CEILING == 128000


# ---------------------------------------------------------------------------
# Tier 1: the Token_Budget_Selection (Requirements 2.2, 2.9)
# ---------------------------------------------------------------------------

class TestSelectionTier:
    def test_selection_at_the_floor_is_returned_unchanged(self):
        assert resolve_token_budget(MODEL, 1, {MODEL: CONFIGURED}) == 1

    def test_selection_at_the_ceiling_is_returned_unchanged(self):
        assert resolve_token_budget(
            MODEL, 128000, {MODEL: CONFIGURED}) == 128000

    def test_selection_of_zero_falls_through_not_clamped_to_one(self):
        # 0 is below the range: invalid at this tier, continue to the
        # mapping — never clamped into range (Requirement 2.9).
        assert resolve_token_budget(MODEL, 0, {MODEL: CONFIGURED}) \
            == CONFIGURED

    def test_selection_past_the_ceiling_falls_through_not_clamped(self):
        assert resolve_token_budget(MODEL, 128001, {MODEL: CONFIGURED}) \
            == CONFIGURED


# ---------------------------------------------------------------------------
# Tier 2: the Model_Token_Limits entry (Requirements 2.3, 2.9)
# ---------------------------------------------------------------------------

class TestMappingTier:
    def test_entry_at_the_floor_is_returned_unchanged(self):
        assert resolve_token_budget(MODEL, None, {MODEL: 1}) == 1

    def test_entry_at_the_ceiling_is_returned_unchanged(self):
        assert resolve_token_budget(MODEL, None, {MODEL: 128000}) == 128000

    def test_entry_of_zero_falls_to_the_default(self):
        assert resolve_token_budget(MODEL, None, {MODEL: 0}) \
            == MODEL_TOKEN_LIMIT_DEFAULT

    def test_entry_past_the_ceiling_falls_to_the_default_not_clamped(self):
        # No clamping at this tier either: the result is the default,
        # never a value above the ceiling (Requirement 2.9).
        assert resolve_token_budget(MODEL, None, {MODEL: 128001}) \
            == MODEL_TOKEN_LIMIT_DEFAULT


# ---------------------------------------------------------------------------
# Tier 3: the default (Requirement 2.4)
# ---------------------------------------------------------------------------

class TestDefaultTier:
    def test_no_selection_and_no_entry_returns_the_default(self):
        assert resolve_token_budget(MODEL, None, {}) == 10000

    def test_out_of_range_at_both_tiers_returns_the_default(self):
        assert resolve_token_budget(MODEL, 0, {MODEL: 128001}) == 10000
        assert resolve_token_budget(MODEL, 128001, {MODEL: 0}) == 10000


# ---------------------------------------------------------------------------
# Booleans at both tiers (Requirement 2.5)
# ---------------------------------------------------------------------------

class TestBooleanRejection:
    """bool is an int subclass; it must be rejected before the int
    check, never converted to 1 or 0."""

    def test_true_selection_is_not_treated_as_one(self):
        # Were True converted to 1, the selection tier would return 1;
        # instead it falls through to the mapping.
        assert resolve_token_budget(MODEL, True, {MODEL: CONFIGURED}) \
            == CONFIGURED

    def test_false_selection_is_not_treated_as_zero(self):
        assert resolve_token_budget(MODEL, False, {MODEL: CONFIGURED}) \
            == CONFIGURED

    def test_true_entry_is_not_treated_as_one(self):
        assert resolve_token_budget(MODEL, None, {MODEL: True}) \
            == MODEL_TOKEN_LIMIT_DEFAULT

    def test_false_entry_is_not_treated_as_zero(self):
        assert resolve_token_budget(MODEL, None, {MODEL: False}) \
            == MODEL_TOKEN_LIMIT_DEFAULT


# ---------------------------------------------------------------------------
# Non-integer types at both tiers (Requirement 2.8)
# ---------------------------------------------------------------------------

class TestNonIntegerRejection:
    """Strings (including digit-only), floats (including whole-valued)
    and Decimals are invalid with no numeric conversion attempted."""

    @pytest.mark.parametrize('value', ['20000', 20000.0, Decimal('20000')],
                             ids=['digit-string', 'whole-float', 'decimal'])
    def test_rejected_as_a_selection(self, value):
        assert resolve_token_budget(MODEL, value, {MODEL: CONFIGURED}) \
            == CONFIGURED

    @pytest.mark.parametrize('value', ['20000', 20000.0, Decimal('20000')],
                             ids=['digit-string', 'whole-float', 'decimal'])
    def test_rejected_as_an_entry(self, value):
        assert resolve_token_budget(MODEL, None, {MODEL: value}) \
            == MODEL_TOKEN_LIMIT_DEFAULT

    def test_decimal_entry_documents_why_the_loader_converts(self):
        """DynamoDB's resource API deserializes every number to Decimal,
        and Decimal is not an int subclass, so a persisted entry read
        straight off the settings item would silently resolve to the
        default. That is exactly why data_accounts' token-limits loader
        converts Decimal to native int before the resolver sees any
        value — pinned here so the contract is visible where it bites.
        """
        persisted_shape = {MODEL: Decimal('20000')}
        assert resolve_token_budget(MODEL, None, persisted_shape) \
            == MODEL_TOKEN_LIMIT_DEFAULT

        converted = {MODEL: int(persisted_shape[MODEL])}
        assert resolve_token_budget(MODEL, None, converted) == 20000


# ---------------------------------------------------------------------------
# Non-string model identifier (Requirement 2.10)
# ---------------------------------------------------------------------------

NON_STRING_IDENTIFIERS = (None, 42, b'us.amazon.nova-pro-v1:0', ('llm',))
_IDS = ['none', 'int', 'bytes', 'tuple']


class TestNonStringIdentifierDivergence:
    """The deliberate divergence from resolve_model_image_limit: a
    non-string identifier skips the Model_Token_Limits lookup but does
    NOT discard a valid Token_Budget_Selection, because the selection
    tier does not depend on the identifier. This asymmetry is intended;
    do not "correct" either resolver to match the other."""

    @pytest.mark.parametrize('identifier', NON_STRING_IDENTIFIERS, ids=_IDS)
    def test_valid_selection_survives_a_non_string_identifier(
            self, identifier):
        # The entry is planted under the identifier itself (every
        # identifier here is hashable), yet the selection still wins.
        assert resolve_token_budget(identifier, 500,
                                    {identifier: CONFIGURED}) == 500

    @pytest.mark.parametrize('identifier', NON_STRING_IDENTIFIERS, ids=_IDS)
    def test_no_lookup_happens_without_a_valid_selection(self, identifier):
        # Even a plantable entry keyed by the identifier itself is never
        # consulted: the resolver goes straight to the default.
        assert resolve_token_budget(identifier, None,
                                    {identifier: CONFIGURED}) \
            == MODEL_TOKEN_LIMIT_DEFAULT

    def test_divergence_from_the_image_limit_resolver_side_by_side(self):
        identifier = 42

        # resolve_model_image_limit: a non-string identifier yields the
        # default unconditionally — the mapping entry is ignored.
        assert resolve_model_image_limit(identifier, {identifier: 7}) \
            == MODEL_IMAGE_LIMIT_DEFAULT

        # resolve_token_budget: the same non-string identifier skips the
        # lookup exactly like the image resolver...
        assert resolve_token_budget(identifier, None,
                                    {identifier: CONFIGURED}) \
            == MODEL_TOKEN_LIMIT_DEFAULT

        # ...but still returns a valid selection rather than discarding
        # it (the divergence Requirement 2.10 mandates).
        assert resolve_token_budget(identifier, 500,
                                    {identifier: CONFIGURED}) == 500
