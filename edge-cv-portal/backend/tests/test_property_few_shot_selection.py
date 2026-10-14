"""
Property-based tests for the shared LLM request module
(layers/shared/python/dda_llm_request.py): Few_Shot_Example selection,
the no-few-shot request shape, and Model_Image_Limit resolution.

Spec: llm-autolabel-prompt-tuning, tasks 1.2, 1.3, 1.4.

**Feature: llm-autolabel-prompt-tuning, Property 3: Few-shot selection is a deterministic, bounded, order-preserving prefix**
**Validates: Requirements 6.5, 7.2, 7.3, 7.4, 7.6**
**Feature: llm-autolabel-prompt-tuning, Property 4: A request without few-shot examples keeps the pre-feature shape**
**Validates: Requirements 10.2, 10.3**
**Feature: llm-autolabel-prompt-tuning, Property 13: Model_Image_Limit resolution is total and safe**
**Validates: Requirements 7.1**

The module under test is pure (no boto3, no I/O), so these tests need no
moto fixtures and no AWS credentials — conftest.py already places the
shared layer on sys.path and registers the hypothesis profile these
tests run under. Each property runs at 100 examples via its own
`@settings`, which takes precedence over the profile default.

Generator notes:

- Stored example sets are built as a permutation of at most 10 good and
  at most 10 bad references, with `position` assigned per designation in
  stored order — exactly the shape `create_dda_job` persists in
  `auto_label.few_shot.examples`. The expected candidate ordering (good
  in stored order followed by bad in stored order) is recomputed in the
  test from the generated list, never read back from the module.
- Property 4 covers the job-record compatibility contract by generating
  the whole `few_shot` sub-document space that must resolve to
  *disabled* — absent, `None`, non-dict, falsy `enabled`, and `enabled`
  with an empty or malformed `examples` value. The two-line resolver
  glue below mirrors what the Auto_Labeler does with that document
  (dict + `enabled is True` gate, then `select_few_shot_examples`); the
  request shape itself comes from the module under test.
- Prompt text and pixel dimensions are drawn from small alphabets and
  ranges: what matters here is the content *layout*, and the prompt body
  is pinned character-for-character against `build_detection_prompt`.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dda_llm_guidance import build_detection_prompt
from dda_llm_request import (
    FEW_SHOT_BAD,
    FEW_SHOT_GOOD,
    FEW_SHOT_HEADER,
    FEW_SHOT_TARGET_INTRO,
    MODEL_IMAGE_LIMIT_DEFAULT,
    build_llm_request,
    resolve_model_image_limit,
    select_few_shot_examples,
)

MODALITIES = ('Segmentation', 'ObjectDetection', 'Classification')

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_modalities = st.sampled_from(MODALITIES)
_dimensions = st.integers(min_value=1, max_value=4096)

_class_names = st.text(
    alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'),
                           whitelist_characters='_-', max_codepoint=0x24F),
    min_size=1, max_size=12,
)
_label_sets = st.lists(_class_names, min_size=1, max_size=6, unique=True)

# Detection_Prompt text: any printable text, inserted verbatim.
_prompts = st.text(min_size=0, max_size=200)

# Model_Image_Limit: at least 1, spanning below / at / above the number
# of examples a job can carry (up to 20).
_limits = st.integers(min_value=1, max_value=40)


@st.composite
def _stored_examples(draw):
    """A job's stored example references in stored order: at most 10 good
    and at most 10 bad, `position` per designation."""
    n_good = draw(st.integers(min_value=0, max_value=10))
    n_bad = draw(st.integers(min_value=0, max_value=10))
    order = draw(st.permutations([FEW_SHOT_GOOD] * n_good + [FEW_SHOT_BAD] * n_bad))
    counters = {FEW_SHOT_GOOD: 0, FEW_SHOT_BAD: 0}
    examples = []
    for designation in order:
        position = counters[designation]
        counters[designation] += 1
        examples.append({
            'ref': (f's3://uc-bucket/labeling-examples/job-1/'
                    f'{designation}/{position}-img.jpg'),
            'designation': designation,
            'position': position,
        })
    return examples


def _image_for(example):
    """The image dict the caller builds from a selected reference: bytes
    keyed by the ref so every block is identifiable, plus the
    designation that drives identification content."""
    return {
        'bytes': example['ref'].encode('utf-8'),
        'format': 'jpeg',
        'designation': example['designation'],
    }


TARGET_IMAGE = {'bytes': b'target-image-bytes', 'format': 'png'}


# ---------------------------------------------------------------------------
# Property 3 (task 1.2)
# ---------------------------------------------------------------------------

# Feature: llm-autolabel-prompt-tuning, Property 3: Few-shot selection is a
# deterministic, bounded, order-preserving prefix
@settings(max_examples=100)
@given(examples=_stored_examples(), limit=_limits,
       modality=_modalities, label_set=_label_sets, prompt=_prompts,
       width=_dimensions, height=_dimensions)
def test_property_few_shot_selection_is_deterministic_bounded_prefix(
        examples, limit, modality, label_set, prompt, width, height):
    """
    **Feature: llm-autolabel-prompt-tuning, Property 3: Few-shot
    selection is a deterministic, bounded, order-preserving prefix**

    For any stored example set (at most 10 good and 10 bad, in stored
    order) and any Model_Image_Limit of at least 1, the attached example
    list equals the first `limit - 1` entries of good-in-stored-order
    followed by bad-in-stored-order, the omitted list is exactly the
    remainder, the resulting request carries at least 1 and at most
    `limit` images, repeated evaluation yields the identical selection,
    and every attached example is immediately preceded by content
    identifying it as a good or a bad example.

    **Validates: Requirements 6.5, 7.2, 7.3, 7.4, 7.6**
    """
    expected_order = ([e for e in examples if e['designation'] == FEW_SHOT_GOOD]
                      + [e for e in examples if e['designation'] == FEW_SHOT_BAD])
    slots = max(0, limit - 1)

    attached, omitted = select_few_shot_examples(examples, limit)

    # Prefix of good-then-bad in stored order, remainder omitted.
    assert attached == expected_order[:slots]
    assert omitted == expected_order[slots:]
    assert attached + omitted == expected_order
    assert len(attached) + len(omitted) == len(examples)

    # Determinism: same input, identical selection (Requirement 7.6).
    again_attached, again_omitted = select_few_shot_examples(examples, limit)
    assert again_attached == attached
    assert again_omitted == omitted

    request = build_llm_request(modality, label_set, prompt, width, height,
                               None, TARGET_IMAGE,
                               [_image_for(e) for e in attached])
    content = request['messages'][0]['content']
    image_blocks = [block for block in content if 'image' in block]

    # Bounded: attached examples plus the target image (Requirement 7.2).
    assert 1 <= len(image_blocks) <= limit
    assert len(image_blocks) == len(attached) + 1

    # Every example image is immediately preceded by content identifying
    # it as a good or a bad example (Requirement 6.5).
    ordinals = {FEW_SHOT_GOOD: 0, FEW_SHOT_BAD: 0}
    for index, example in enumerate(attached):
        designation = example['designation']
        ordinals[designation] += 1
        label = 'Good example' if designation == FEW_SHOT_GOOD else 'Bad example'
        # Layout: [header, (text, image) per example, intro, target, prompt]
        text_block = content[1 + 2 * index]
        image_block = content[2 + 2 * index]
        assert text_block == {'text': f'{label} {ordinals[designation]}:'}
        assert image_block['image']['source']['bytes'] == \
            example['ref'].encode('utf-8')

    if attached:
        assert content[0] == {'text': FEW_SHOT_HEADER}
        assert content[-3] == {'text': FEW_SHOT_TARGET_INTRO}
    # The target image then the prompt always close the content list.
    assert content[-2]['image']['source']['bytes'] == TARGET_IMAGE['bytes']
    assert content[-1] == {'text': request['prompt']}


# ---------------------------------------------------------------------------
# Property 4 (task 1.3)
# ---------------------------------------------------------------------------

def _resolve_stored_few_shot(few_shot, limit):
    """The job-record compatibility gate (Requirement 10.3): only a dict
    with `enabled is True` can produce attachments; everything else is
    disabled. Selection itself is the module's."""
    if not isinstance(few_shot, dict) or few_shot.get('enabled') is not True:
        return []
    attached, _omitted = select_few_shot_examples(few_shot.get('examples'), limit)
    return [_image_for(e) for e in attached if isinstance(e, dict)
            and 'designation' in e and 'ref' in e]


# `few_shot` documents that must all resolve to disabled: absent
# (sentinel), null, non-dict, falsy `enabled`, and enabled-but-empty or
# malformed `examples`.
_disabled_few_shot_docs = st.one_of(
    st.just('__absent__'),
    st.none(),
    st.sampled_from([[], 0, 1, '', 'enabled', 3.5, True]),
    st.fixed_dictionaries({'enabled': st.sampled_from(
        [False, None, 0, '', 'true', [], {}])}),
    st.builds(lambda examples: {'enabled': True, 'examples': examples},
              st.sampled_from([None, [], {}, '', 0, 'good'])),
)


# Feature: llm-autolabel-prompt-tuning, Property 4: A request without few-shot
# examples keeps the pre-feature shape
@settings(max_examples=100)
@given(few_shot=_disabled_few_shot_docs, limit=_limits,
       modality=_modalities, label_set=_label_sets, prompt=_prompts,
       width=_dimensions, height=_dimensions,
       per_label=st.one_of(st.none(), st.just({})))
def test_property_request_without_few_shot_keeps_pre_feature_shape(
        few_shot, limit, modality, label_set, prompt, width, height, per_label):
    """
    **Feature: llm-autolabel-prompt-tuning, Property 4: A request without
    few-shot examples keeps the pre-feature shape**

    For any `llm:` job configuration in which the Few_Shot_Option is
    disabled, absent, `null`, or malformed in the job record, the model
    request content is exactly the target image block followed by the
    text block `build_detection_prompt` produces from the
    Detection_Prompt character-for-character, the Label_Set and the
    image's pixel dimensions — no example image blocks, no example
    identification content — and no failure is attributable to the
    few-shot configuration.

    **Validates: Requirements 10.2, 10.3**
    """
    # Resolution never raises for any of these documents (Req 10.3).
    few_shot_images = _resolve_stored_few_shot(few_shot, limit)
    assert few_shot_images == []

    request = build_llm_request(modality, label_set, prompt, width, height,
                               per_label, TARGET_IMAGE, few_shot_images)

    expected_prompt = build_detection_prompt(modality, label_set, prompt,
                                            width, height, per_label)
    assert request['prompt'] == expected_prompt

    # The pre-feature content list, exactly: [image(target), text(prompt)].
    assert request['messages'] == [{
        'role': 'user',
        'content': [
            {'image': {'format': 'png',
                       'source': {'bytes': TARGET_IMAGE['bytes']}}},
            {'text': expected_prompt},
        ],
    }]

    content = request['messages'][0]['content']
    assert len([block for block in content if 'image' in block]) == 1
    # The prompt is the only text block: no header, no target intro, no
    # per-example identification content.
    assert [block['text'] for block in content if 'text' in block] == [expected_prompt]


# ---------------------------------------------------------------------------
# Property 13 (task 1.4)
# ---------------------------------------------------------------------------

_model_identifiers = st.sampled_from([
    'us.amazon.nova-pro-v1:0',
    'anthropic.claude-3-5-sonnet-20241022-v2:0',
    'some.model-with-tighter-bound',
    '',
])

# Any configured value, valid or not: integers below / at / above 1,
# bools, floats, numeric strings, None, and containers.
_configured_values = st.one_of(
    st.integers(min_value=-50, max_value=50),
    st.booleans(),
    st.floats(min_value=-10, max_value=50, allow_nan=False, allow_infinity=False),
    st.text(max_size=4),
    st.none(),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(st.text(max_size=2), st.integers(), max_size=1),
)

_limit_configs = st.one_of(
    st.none(),
    st.just({}),
    st.sampled_from([[], 'not-a-mapping', 7]),
    st.dictionaries(_model_identifiers, _configured_values, max_size=4),
)


# Feature: llm-autolabel-prompt-tuning, Property 13: Model_Image_Limit
# resolution is total and safe
@settings(max_examples=100)
@given(model_identifier=_model_identifiers, limits=_limit_configs)
def test_property_model_image_limit_resolution_is_total_and_safe(
        model_identifier, limits):
    """
    **Feature: llm-autolabel-prompt-tuning, Property 13:
    Model_Image_Limit resolution is total and safe**

    For any model identifier and any limit configuration — missing
    entry, non-integer value, value below 1, or a valid value — the
    resolved Model_Image_Limit is an integer of at least 1, and is 20
    whenever no valid configured value exists for that identifier.

    **Validates: Requirements 7.1**
    """
    resolved = resolve_model_image_limit(model_identifier, limits)

    # Total: an integer of at least 1 for every input.
    assert isinstance(resolved, int) and not isinstance(resolved, bool)
    assert resolved >= 1

    configured = limits.get(model_identifier) if isinstance(limits, dict) else None
    valid = (isinstance(configured, int) and not isinstance(configured, bool)
             and configured >= 1)
    if valid:
        assert resolved == configured
    else:
        # No valid configured value exists: the default of 20.
        assert resolved == MODEL_IMAGE_LIMIT_DEFAULT == 20

    # Safe downstream: the bound always leaves the target image a slot,
    # so a resolved limit can never produce a zero-image request.
    attached, _omitted = select_few_shot_examples(
        [{'ref': 's3://b/k.jpg', 'designation': FEW_SHOT_GOOD, 'position': 0}],
        resolved)
    assert len(attached) + 1 <= resolved
