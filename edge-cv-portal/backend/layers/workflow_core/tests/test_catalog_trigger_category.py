"""Unit tests for the CATEGORY_TRIGGER category constant
(triggers-stage-and-unified-input task 1.2).

Asserts the constant's value, its membership and trigger-first position
in the CATEGORIES tuple, and that the relative order of the five
pre-existing categories is unchanged.

Validates: Requirements 1.1, 1.2
"""

from workflow_core.catalog import (
    CATEGORIES,
    CATEGORY_INFERENCE,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_POST_PROCESSING,
    CATEGORY_PREPROCESSING,
    CATEGORY_TRIGGER,
)


class TestCategoryTriggerConstant:
    def test_value_is_trigger(self):
        # Requirement 1.1: the constant carries the literal "trigger".
        assert CATEGORY_TRIGGER == "trigger"

    def test_member_of_categories(self):
        # Requirement 1.2: CATEGORY_TRIGGER is part of the CATEGORIES tuple.
        assert CATEGORY_TRIGGER in CATEGORIES

    def test_trigger_appears_before_input(self):
        # Requirement 1.2: consumers iterating CATEGORIES present Triggers
        # ahead of Inputs.
        assert CATEGORIES.index(CATEGORY_TRIGGER) < CATEGORIES.index(CATEGORY_INPUT)

    def test_trigger_is_first(self):
        # Requirement 1.2: trigger-first ordering — CATEGORY_TRIGGER leads
        # the tuple.
        assert CATEGORIES[0] == CATEGORY_TRIGGER


class TestPreExistingCategoryOrderUnchanged:
    def test_relative_order_of_original_five_categories(self):
        # Requirement 1.2: widening CATEGORIES must not reorder the five
        # pre-existing categories (input, preprocessing, inference,
        # post_processing, output).
        original = [
            CATEGORY_INPUT,
            CATEGORY_PREPROCESSING,
            CATEGORY_INFERENCE,
            CATEGORY_POST_PROCESSING,
            CATEGORY_OUTPUT,
        ]
        preserved = [c for c in CATEGORIES if c in original]
        assert preserved == original

    def test_original_category_values_unchanged(self):
        assert CATEGORY_INPUT == "input"
        assert CATEGORY_PREPROCESSING == "preprocessing"
        assert CATEGORY_INFERENCE == "inference"
        assert CATEGORY_POST_PROCESSING == "post_processing"
        assert CATEGORY_OUTPUT == "output"
