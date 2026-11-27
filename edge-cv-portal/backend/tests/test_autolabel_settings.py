"""
autolabel_settings.py — Images per auto-label worker run
(Settings -> Auto-Labeling).

Covers the shared module the Settings API (data_accounts.py) and the
auto-label worker (dda_autolabel_worker.py) both import:

- the settings key, the bounds 1 to 5, the default 5 and the error text;
- validate_images_per_run: None and a whole number from 1 to 5 may be
  saved; everything else, bools, floats such as 3.0 and strings
  included, gets the one error text;
- parse_stored_images_per_run: a stored whole number from 1 to 5, as an
  int or as the Decimal DynamoDB returns, is used; anything else counts
  as nothing saved (None);
- a Hypothesis property over integers tying both rules to the bounds.

Pure functions: no AWS, no moto.
"""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import autolabel_settings
from autolabel_settings import (
    DEFAULT_IMAGES_PER_RUN,
    IMAGES_PER_RUN_ERROR,
    IMAGES_PER_RUN_SETTING_KEY,
    MAX_IMAGES_PER_RUN,
    MIN_IMAGES_PER_RUN,
    parse_stored_images_per_run,
    validate_images_per_run,
)

EXPECTED_ERROR = ('images_per_run must be a whole number from 1 to 5, '
                  'or null to use the default (5)')


class TestConstants:

    def test_exact_values(self):
        assert IMAGES_PER_RUN_SETTING_KEY == 'autolabel.images_per_run'
        assert MIN_IMAGES_PER_RUN == 1
        assert MAX_IMAGES_PER_RUN == 5
        assert DEFAULT_IMAGES_PER_RUN == 5
        assert IMAGES_PER_RUN_ERROR == EXPECTED_ERROR

    def test_module_imports_no_boto3(self):
        """Importing the module creates no AWS clients."""
        assert not hasattr(autolabel_settings, 'boto3')


class TestValidate:

    @pytest.mark.parametrize('value', [None, 1, 2, 3, 4, 5])
    def test_valid_values(self, value):
        assert validate_images_per_run(value) == []

    @pytest.mark.parametrize('value', [
        0, 6, -1, 2.5, 3.0, '3', True, False, [3], {}])
    def test_invalid_values_get_the_exact_error(self, value):
        assert validate_images_per_run(value) == [EXPECTED_ERROR]


class TestParseStored:

    @pytest.mark.parametrize('value', [1, 2, 3, 4, 5])
    def test_stored_ints(self, value):
        parsed = parse_stored_images_per_run(value)
        assert parsed == value
        assert type(parsed) is int

    @pytest.mark.parametrize('value', [1, 2, 3, 4, 5])
    def test_stored_decimals_as_dynamodb_returns_them(self, value):
        parsed = parse_stored_images_per_run(Decimal(str(value)))
        assert parsed == value
        assert type(parsed) is int

    @pytest.mark.parametrize('value', [
        None, 0, 6, Decimal('2.5'), Decimal('NaN'), Decimal('Infinity'),
        '3', True, 3.0])
    def test_other_values_count_as_nothing_saved(self, value):
        assert parse_stored_images_per_run(value) is None


@given(st.integers(min_value=-1000, max_value=1000))
def test_both_rules_follow_the_bounds(value):
    """validate accepts exactly the integers from 1 to 5, and a stored
    Decimal of an integer is used exactly when it is in that range."""
    in_range = 1 <= value <= 5
    assert (validate_images_per_run(value) == []) is in_range
    expected = value if in_range else None
    assert parse_stored_images_per_run(Decimal(value)) == expected
