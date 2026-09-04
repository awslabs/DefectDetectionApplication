"""
Example-based unit tests for the LLM guidance prompt builder and model
identifier validator (layers/shared/python/dda_llm_guidance.py):
validate_model_identifier and build_detection_prompt.

Spec: llm-auto-labeling, task 5.2.
Requirements: 1.5, 2.6, 3.1

The module under test is pure (no boto3, no I/O), so these tests need
no moto fixtures — conftest.py already places the shared layer on
sys.path.
"""
import pytest

from dda_llm_guidance import (
    MODEL_IDENTIFIER_MAX_LENGTH,
    build_detection_prompt,
    validate_model_identifier,
)

WIDTH = 640
HEIGHT = 480
LABEL_SET = ['scratch', 'dent', 'paint-chip']
MODALITIES = ('Segmentation', 'ObjectDetection', 'Classification')


def _prompt(modality='Segmentation', label_set=None,
            detection_prompt='Find every scratch.',
            width=WIDTH, height=HEIGHT, per_label_prompts=None):
    return build_detection_prompt(
        modality,
        LABEL_SET if label_set is None else label_set,
        detection_prompt,
        width,
        height,
        per_label_prompts,
    )


# ---------------------------------------------------------------------------
# validate_model_identifier (Requirement 1.5)
# ---------------------------------------------------------------------------

class TestValidateModelIdentifier:
    def test_empty_string_rejected(self):
        assert validate_model_identifier('') == 'model identifier is required'

    @pytest.mark.parametrize('value', [None, 42, 1.5, True, ['id'], {'id': 1}])
    def test_non_string_rejected(self, value):
        assert validate_model_identifier(value) == 'model identifier is required'

    def test_max_length_accepted(self):
        assert MODEL_IDENTIFIER_MAX_LENGTH == 256
        assert validate_model_identifier('a' * 256) is None

    def test_over_max_length_rejected(self):
        reason = validate_model_identifier('a' * 257)
        assert reason == 'model identifier must be at most 256 characters'

    @pytest.mark.parametrize('bad_char, label', [
        (' ', 'space'),
        ('\t', 'tab'),
        ('\n', 'newline'),
        ('\x00', 'NUL'),
        ('\x7f', 'DEL'),
    ])
    def test_whitespace_and_control_characters_rejected(self, bad_char, label):
        reason = validate_model_identifier(f'model{bad_char}id')
        assert reason == ('model identifier must not contain whitespace '
                          'or control characters')

    def test_realistic_identifier_with_colon_accepted(self):
        # Colons are legal in model identifiers.
        assert validate_model_identifier('us.amazon.nova-pro-v1:0') is None


# ---------------------------------------------------------------------------
# build_detection_prompt (Requirements 2.6, 3.1)
# ---------------------------------------------------------------------------

class TestBuildDetectionPrompt:
    def test_contains_width_and_height(self):
        prompt = _prompt(width=1234, height=987)
        assert '1234' in prompt
        assert '987' in prompt

    def test_contains_every_label_set_name(self):
        prompt = _prompt()
        for label in LABEL_SET:
            assert label in prompt

    def test_detection_prompt_inserted_byte_for_byte(self):
        # Leading/trailing whitespace, newlines, quotes, and braces all
        # survive verbatim (no trimming, no escaping; Requirement 2.6).
        detection_prompt = ('  \tFind "defects" like {cracks}\n'
                            'and \'chips\' -- even {nested {braces}}.\n  ')
        prompt = _prompt(detection_prompt=detection_prompt)
        assert detection_prompt in prompt

    def test_per_label_prompts_inserted_verbatim_when_supplied(self):
        per_label = {
            'scratch': '  a thin {linear} "mark"\n on the surface  ',
            'dent': 'a depression\twithout paint loss',
        }
        prompt = _prompt(per_label_prompts=per_label)
        for label, text in per_label.items():
            assert text in prompt
            assert label in prompt

    def test_no_per_label_sections_when_not_supplied(self):
        assert 'Guidance for label' not in _prompt(per_label_prompts=None)
        assert 'Guidance for label' not in _prompt(per_label_prompts={})

    def test_identical_geometry_instructions_across_modalities(self):
        # One prompt shape serves all three modalities — the whole
        # prompt, geometry instructions included, is identical.
        prompts = {modality: _prompt(modality=modality)
                   for modality in MODALITIES}
        assert len(set(prompts.values())) == 1
