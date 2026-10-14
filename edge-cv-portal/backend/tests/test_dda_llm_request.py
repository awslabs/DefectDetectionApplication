"""
Example-based unit tests for the shared LLM request module
(layers/shared/python/dda_llm_request.py): build_llm_request,
select_few_shot_examples, image_format_for_key and
resolve_model_image_limit.

Spec: llm-autolabel-prompt-tuning, task 1.5.
Requirements: 3.1, 3.4, 6.5, 7.4

The module under test is pure (no boto3, no I/O), so these tests need no
moto fixtures — conftest.py already places the shared layer on sys.path.
"""
import pytest

from dda_llm_guidance import build_detection_prompt
from dda_llm_request import (
    FEW_SHOT_BAD,
    FEW_SHOT_GOOD,
    FEW_SHOT_HEADER,
    FEW_SHOT_TARGET_INTRO,
    MODEL_IMAGE_LIMIT_DEFAULT,
    build_llm_request,
    few_shot_identification_text,
    image_format_for_key,
    resolve_model_image_limit,
    select_few_shot_examples,
)

WIDTH = 640
HEIGHT = 480
LABEL_SET = ['scratch', 'dent', 'paint-chip']
DETECTION_PROMPT = 'Locate every scratch on the housing.'
MODALITIES = ('Segmentation', 'ObjectDetection', 'Classification')

TARGET = {'bytes': b'target-bytes', 'format': 'png'}


def _example(designation, index, position=0):
    """A stored example reference plus the image dict built from it."""
    return {
        'ref': (f's3://uc-bucket/labeling-examples/job-1/'
                f'{designation}/{position}-{index}.jpg'),
        'designation': designation,
        'position': position,
        'bytes': f'{designation}-{index}-bytes'.encode('utf-8'),
        'format': 'jpeg',
    }


def _request(few_shot_images=None, modality='ObjectDetection',
             per_label_prompts=None, detection_prompt=DETECTION_PROMPT):
    return build_llm_request(modality, LABEL_SET, detection_prompt,
                             WIDTH, HEIGHT, per_label_prompts, TARGET,
                             few_shot_images)


def _content(**kwargs):
    return _request(**kwargs)['messages'][0]['content']


# ---------------------------------------------------------------------------
# Prompt text (Requirement 3.1)
# ---------------------------------------------------------------------------

class TestPromptText:
    @pytest.mark.parametrize('modality', MODALITIES)
    def test_prompt_equals_build_detection_prompt_verbatim(self, modality):
        expected = build_detection_prompt(modality, LABEL_SET,
                                          DETECTION_PROMPT, WIDTH, HEIGHT,
                                          None)
        request = _request(modality=modality)
        assert request['prompt'] == expected
        assert request['messages'][0]['content'][-1] == {'text': expected}

    def test_prompt_identical_with_and_without_few_shot(self):
        without = _request()['prompt']
        with_examples = _request(
            few_shot_images=[_example(FEW_SHOT_GOOD, 0)])['prompt']
        assert with_examples == without

    def test_per_label_prompts_inserted_verbatim(self):
        per_label = {'scratch': 'Only hairline scratches. Keep "quotes" \\ as-is.'}
        request = _request(per_label_prompts=per_label)
        assert request['prompt'] == build_detection_prompt(
            'ObjectDetection', LABEL_SET, DETECTION_PROMPT, WIDTH, HEIGHT,
            per_label)
        assert "Guidance for label 'scratch': " + per_label['scratch'] \
            in request['prompt']

    def test_detection_prompt_not_trimmed_or_escaped(self):
        raw = '  Find <scratches> & "dents"\n\ttabbed  '
        assert raw in _request(detection_prompt=raw)['prompt']


# ---------------------------------------------------------------------------
# Content layout (Requirements 6.5, 10.2)
# ---------------------------------------------------------------------------

class TestContentLayout:
    @pytest.mark.parametrize('few_shot', [None, []])
    def test_empty_few_shot_is_target_then_prompt(self, few_shot):
        request = _request(few_shot_images=few_shot)
        assert request['messages'] == [{
            'role': 'user',
            'content': [
                {'image': {'format': 'png',
                           'source': {'bytes': b'target-bytes'}}},
                {'text': request['prompt']},
            ],
        }]

    def test_good_only_set(self):
        examples = [_example(FEW_SHOT_GOOD, i, i) for i in range(2)]
        content = _content(few_shot_images=examples)
        assert content[0] == {'text': FEW_SHOT_HEADER}
        assert content[1] == {'text': 'Good example 1:'}
        assert content[2]['image']['source']['bytes'] == examples[0]['bytes']
        assert content[3] == {'text': 'Good example 2:'}
        assert content[4]['image']['source']['bytes'] == examples[1]['bytes']
        assert content[5] == {'text': FEW_SHOT_TARGET_INTRO}
        assert content[6]['image']['source']['bytes'] == b'target-bytes'
        assert 'text' in content[7]
        assert len(content) == 8

    def test_bad_only_set(self):
        examples = [_example(FEW_SHOT_BAD, i, i) for i in range(2)]
        content = _content(few_shot_images=examples)
        assert [block.get('text') for block in content[:6]] == [
            FEW_SHOT_HEADER, 'Bad example 1:', None,
            'Bad example 2:', None, FEW_SHOT_TARGET_INTRO,
        ]

    def test_mixed_set_numbers_ordinals_per_designation(self):
        examples = [_example(FEW_SHOT_GOOD, 0, 0), _example(FEW_SHOT_GOOD, 1, 1),
                    _example(FEW_SHOT_BAD, 0, 0)]
        content = _content(few_shot_images=examples)
        identification = [block['text'] for block in content
                          if 'text' in block
                          and block['text'].startswith(('Good example',
                                                        'Bad example'))]
        assert identification == ['Good example 1:', 'Good example 2:',
                                  'Bad example 1:']
        # Every example image immediately follows its identification text.
        for index, example in enumerate(examples):
            assert 'text' in content[1 + 2 * index]
            assert content[2 + 2 * index]['image']['source']['bytes'] == \
                example['bytes']

    def test_target_image_and_prompt_always_close_the_content(self):
        for few_shot in (None, [_example(FEW_SHOT_BAD, 0)]):
            request = _request(few_shot_images=few_shot)
            content = request['messages'][0]['content']
            assert content[-2] == {'image': {'format': 'png',
                                             'source': {'bytes': b'target-bytes'}}}
            assert content[-1] == {'text': request['prompt']}

    def test_missing_format_defaults_to_jpeg(self):
        content = _content(few_shot_images=[{'bytes': b'x',
                                            'designation': FEW_SHOT_GOOD}])
        assert content[2]['image']['format'] == 'jpeg'

    def test_unknown_designation_identified_as_bad_example(self):
        content = _content(few_shot_images=[{'bytes': b'x', 'format': 'jpeg',
                                            'designation': 'weird'}])
        assert content[1] == {'text': 'Bad example 1:'}

    @pytest.mark.parametrize('designation, ordinal, expected', [
        (FEW_SHOT_GOOD, 1, 'Good example 1:'),
        (FEW_SHOT_BAD, 3, 'Bad example 3:'),
        (None, 2, 'Bad example 2:'),
    ])
    def test_few_shot_identification_text(self, designation, ordinal, expected):
        assert few_shot_identification_text(designation, ordinal) == expected


# ---------------------------------------------------------------------------
# Content restriction (Requirement 3.4)
# ---------------------------------------------------------------------------

class TestContentCarriesOnlyImagesAndDerivedText:
    def test_every_block_is_an_image_or_a_derived_text_block(self):
        examples = [_example(FEW_SHOT_GOOD, 0), _example(FEW_SHOT_BAD, 0)]
        request = _request(few_shot_images=examples)
        content = request['messages'][0]['content']
        allowed_texts = {
            FEW_SHOT_HEADER, FEW_SHOT_TARGET_INTRO, request['prompt'],
            'Good example 1:', 'Bad example 1:',
        }
        for block in content:
            assert set(block) in ({'image'}, {'text'})
            if 'text' in block:
                assert block['text'] in allowed_texts
            else:
                assert set(block['image']) == {'format', 'source'}
                assert set(block['image']['source']) == {'bytes'}
        assert set(request['messages'][0]) == {'role', 'content'}
        assert set(request) == {'messages', 'prompt'}

    def test_no_credentials_urls_or_arns_reach_the_request(self):
        # The example references are s3:// URIs; none of them may appear
        # in any content block, and no ARN / credential material either.
        examples = [_example(FEW_SHOT_GOOD, 0), _example(FEW_SHOT_BAD, 0)]
        request = _request(few_shot_images=examples)
        texts = ' '.join(block['text'] for block
                         in request['messages'][0]['content'] if 'text' in block)
        for needle in ('s3://', 'https://', 'arn:aws:', 'AKIA', 'uc-bucket',
                       'X-Amz-Signature', 'SecretAccessKey', 'SessionToken'):
            assert needle not in texts
        for example in examples:
            assert example['ref'] not in texts


# ---------------------------------------------------------------------------
# select_few_shot_examples (Requirement 7.4)
# ---------------------------------------------------------------------------

class TestSelectFewShotExamples:
    def test_limit_of_one_attaches_nothing(self):
        examples = [_example(FEW_SHOT_GOOD, 0), _example(FEW_SHOT_BAD, 0)]
        attached, omitted = select_few_shot_examples(examples, 1)
        assert attached == []
        assert omitted == examples

    def test_limit_of_one_yields_the_pre_feature_content(self):
        attached, _omitted = select_few_shot_examples(
            [_example(FEW_SHOT_GOOD, 0)], 1)
        content = _content(few_shot_images=attached)
        assert len(content) == 2
        assert 'image' in content[0] and 'text' in content[1]

    def test_good_examples_precede_bad_in_stored_order(self):
        stored = [_example(FEW_SHOT_BAD, 0, 0), _example(FEW_SHOT_GOOD, 0, 0),
                  _example(FEW_SHOT_BAD, 1, 1), _example(FEW_SHOT_GOOD, 1, 1)]
        attached, omitted = select_few_shot_examples(stored, 4)
        assert [e['ref'] for e in attached] == [stored[1]['ref'], stored[3]['ref'],
                                               stored[0]['ref']]
        assert [e['ref'] for e in omitted] == [stored[2]['ref']]

    def test_all_examples_fit_under_a_generous_limit(self):
        stored = [_example(FEW_SHOT_GOOD, i, i) for i in range(3)]
        attached, omitted = select_few_shot_examples(stored, 20)
        assert attached == stored
        assert omitted == []

    @pytest.mark.parametrize('examples', [None, 'good', 7, {}])
    def test_non_list_examples_attach_nothing(self, examples):
        assert select_few_shot_examples(examples, 20) == ([], [])


# ---------------------------------------------------------------------------
# image_format_for_key
# ---------------------------------------------------------------------------

class TestImageFormatForKey:
    @pytest.mark.parametrize('key, expected', [
        ('training-images/a.PNG', 'png'),
        ('training-images/a.png', 'png'),
        ('s3://bucket/dir.png/b.PNG', 'png'),
        ('training-images/a.jpeg', 'jpeg'),
        ('training-images/a.JPG', 'jpeg'),
        ('training-images/a.jpg', 'jpeg'),
        ('training-images/no-extension', 'jpeg'),
        ('', 'jpeg'),
        ('training-images/a.png.jpg', 'jpeg'),
    ])
    def test_format_rule(self, key, expected):
        assert image_format_for_key(key) == expected

    @pytest.mark.parametrize('key', [None, 42, b'a.png'])
    def test_non_string_keys_default_to_jpeg(self, key):
        assert image_format_for_key(key) == 'jpeg'


# ---------------------------------------------------------------------------
# resolve_model_image_limit
# ---------------------------------------------------------------------------

class TestResolveModelImageLimit:
    def test_default_is_twenty(self):
        assert MODEL_IMAGE_LIMIT_DEFAULT == 20

    def test_configured_value_used(self):
        assert resolve_model_image_limit('m', {'m': 4}) == 4

    @pytest.mark.parametrize('limits', [None, {}, {'other': 4}, [], 'nope'])
    def test_missing_entry_falls_back_to_default(self, limits):
        assert resolve_model_image_limit('m', limits) == MODEL_IMAGE_LIMIT_DEFAULT

    @pytest.mark.parametrize('configured', [0, -1, '4', 4.0, True, False,
                                            None, [4], {'v': 4}])
    def test_invalid_entry_falls_back_to_default(self, configured):
        assert resolve_model_image_limit('m', {'m': configured}) == \
            MODEL_IMAGE_LIMIT_DEFAULT

    def test_limit_of_one_is_honored(self):
        assert resolve_model_image_limit('m', {'m': 1}) == 1
