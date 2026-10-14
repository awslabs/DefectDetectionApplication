"""
Property and example tests for the dda_grounded_sam_worker pure logic:
``gsam_utils`` (caption building, phrase-span attribution, box clamping,
per-label NMS selection), the verbatim ``mask_utils.py`` copy, and the
handler's input validation.

These tests run without importing onnxruntime / numpy / Pillow /
tokenizers (the ``test_dda_sam_worker_mask_utils.py`` precedent): the
worker guards its heavy imports behind function bodies, and everything
exercised here is standard library only. The handler audit tests prove
it by blocking those imports outright while driving the validation
failure paths.

Module-loading note: both ``sam-worker/`` and ``grounded-sam-worker/``
ship a ``handler.py`` and a ``mask_utils.py``. To keep this file safe to
run in the same pytest session as ``test_dda_sam_worker_mask_utils.py``
(which imports the sam worker's modules by path insertion, in either
command-line order), the grounded-sam modules are loaded here from
explicit file paths via importlib, the worker directory is never put on
``sys.path``, and ``sys.modules['handler']`` is never populated. The
``mask_utils`` registration below is a ``setdefault`` and is safe by
construction: the drift guard in this file pins the two copies
byte-identical.

Feature: grounded-sam-autolabel, Task 1.4
Requirements: 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.10
"""
import importlib.util
import os
import sys
from contextlib import contextmanager

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, '..'))
_GSAM_WORKER_DIR = os.path.join(_BACKEND, 'grounded-sam-worker')
_SAM_WORKER_DIR = os.path.join(_BACKEND, 'sam-worker')
_SHARED_LAYER = os.path.join(_BACKEND, 'layers', 'shared', 'python')
if _SHARED_LAYER not in sys.path:
    sys.path.insert(0, _SHARED_LAYER)

import dda_manifest  # noqa: E402  (canonical RLE implementation)


def _load_module_from(file_path, module_name):
    """Load a module instance from an explicit file path (no sys.path)."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The worker's pure prompt/detection logic (unique module name, safe to
# register so the handler's own `from gsam_utils import ...` resolves).
gsam_utils = _load_module_from(
    os.path.join(_GSAM_WORKER_DIR, 'gsam_utils.py'), 'gsam_utils')
sys.modules['gsam_utils'] = gsam_utils

# The worker's mask_utils copy, loaded from its own file so Property 8
# provably tests the grounded-sam copy. Registered under 'mask_utils'
# only when nothing else (the sam-worker test) got there first — the two
# copies are byte-identical (drift guard below), so either registration
# satisfies both test files.
gsam_mask_utils = _load_module_from(
    os.path.join(_GSAM_WORKER_DIR, 'mask_utils.py'), 'gsam_worker_mask_utils')
sys.modules.setdefault('mask_utils', gsam_mask_utils)

# Environment variables the handler reads at import time.
_HANDLER_ENV_PREFIXES = ('GROUNDED_SAM_', 'GROUNDING_DINO_')
_HANDLER_ENV_EXACT = ('SAM_ENCODER_PATH', 'SAM_DECODER_PATH')


def _load_gsam_handler(env=None):
    """
    Load a fresh grounded-sam-worker handler module instance with a
    scrubbed (optionally overridden) environment, without registering it
    in sys.modules — `import handler` elsewhere keeps resolving to the
    sam worker's module.
    """
    saved = {}
    for key in list(os.environ):
        if key.startswith(_HANDLER_ENV_PREFIXES) or key in _HANDLER_ENV_EXACT:
            saved[key] = os.environ.pop(key)
    os.environ.update(env or {})
    try:
        return _load_module_from(
            os.path.join(_GSAM_WORKER_DIR, 'handler.py'),
            'gsam_worker_handler_under_test',
        )
    finally:
        for key in (env or {}):
            os.environ.pop(key, None)
        os.environ.update(saved)


_HEAVY_MODULES = ('numpy', 'onnxruntime', 'PIL', 'tokenizers')


@contextmanager
def _heavy_imports_blocked():
    """
    Make any import of the worker's heavy dependencies raise ImportError
    for the duration of the block (sys.modules sentinel), restoring the
    interpreter state afterwards. Proves the handler's validation
    failure paths run before any model import (req 3.8).
    """
    saved = {}
    for name in list(sys.modules):
        if name.split('.')[0] in _HEAVY_MODULES:
            saved[name] = sys.modules.pop(name)
    for name in _HEAVY_MODULES:
        sys.modules[name] = None  # `import <name>` now raises ImportError
    try:
        yield
    finally:
        for name in _HEAVY_MODULES:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Phrase cores: arbitrary unicode (inner whitespace welcome) except '.' —
# build_caption keeps inner dots, which the handler then rejects as a span
# misalignment (see TestBuildCaptionExamples), so property inputs stay in
# the accepted domain. Cores must survive whitespace collapse non-empty.
_PHRASE_CORE = st.text(
    alphabet=st.characters(exclude_characters='.', exclude_categories=('Cs',)),
    min_size=1,
    max_size=12,
).filter(lambda text: ' '.join(text.split()) != '')

# Synthetic vocabulary: phrase token ids disjoint from the marker ids.
_CLS_ID, _SEP_ID, _PAD_ID, _DOT_ID = 1, 2, 0, 3
_TOKEN_ID = st.integers(min_value=10, max_value=9999)


@st.composite
def _caption_span_cases(draw):
    """
    A phrase list (decorated with leading/inner/trailing whitespace and
    trailing dots), one synthetic token run per phrase, trailing padding,
    a per-token score vector, and thresholds.
    """
    cores = draw(st.lists(_PHRASE_CORE, min_size=1, max_size=6))
    phrases_in = []
    for core in cores:
        lead = draw(st.sampled_from(['', ' ', '\t ']))
        mid = draw(st.sampled_from(['', ' ', ' \n ']))
        dots = '.' * draw(st.integers(min_value=0, max_value=3))
        phrases_in.append(lead + core + mid + dots)
    runs = [draw(st.lists(_TOKEN_ID, min_size=1, max_size=4)) for _ in cores]
    pad_count = draw(st.integers(min_value=0, max_value=3))
    stream_length = 1 + sum(len(run) + 1 for run in runs) + 1 + pad_count
    scores = draw(st.lists(
        st.floats(min_value=0.0, max_value=1.0),
        min_size=stream_length, max_size=stream_length,
    ))
    box_threshold = draw(st.floats(min_value=0.0, max_value=1.0))
    text_threshold = draw(st.floats(min_value=0.0, max_value=1.0))
    return phrases_in, runs, pad_count, scores, box_threshold, text_threshold


# Normalized cxcywh coordinates: in-range, out-of-range, degenerate, and
# non-finite values (the conversion must be total).
_COORD = st.one_of(
    st.floats(min_value=-3.0, max_value=3.0),
    st.sampled_from([
        float('nan'), float('inf'), float('-inf'), 0.0, -0.0, 1e308, -1e308,
    ]),
)
_RAW_BOX = st.one_of(
    st.tuples(_COORD, _COORD, _COORD, _COORD),
    # Malformed shapes the conversion must also survive (returning None)
    st.just(None),
    st.just((0.25,)),
    st.just('not-a-box'),
)
_RAW_CANDIDATE = st.fixed_dictionaries({
    'raw_box': _RAW_BOX,
    'score': st.floats(min_value=0.0, max_value=1.0),
    'label_index': st.integers(min_value=0, max_value=3),
})


@st.composite
def _mask_cases(draw):
    width = draw(st.integers(min_value=1, max_value=16))
    height = draw(st.integers(min_value=1, max_value=16))
    mask = draw(st.lists(
        st.integers(min_value=0, max_value=1),
        min_size=width * height, max_size=width * height,
    ))
    return width, height, mask


_VALID_PROMPT_ENTRY = st.fixed_dictionaries(
    {'label': st.text(min_size=1, max_size=8).filter(lambda s: s.strip())},
    optional={'prompt': st.one_of(st.none(), st.text(max_size=8))},
)
_NON_LIST_PROMPTS = st.one_of(
    st.none(),
    st.text(max_size=5),
    st.integers(),
    st.floats(allow_nan=False),
    st.booleans(),
    st.dictionaries(st.text(max_size=3), st.text(max_size=3), max_size=2),
    st.tuples(st.text(max_size=3)),
)
_BAD_PROMPT_ENTRY = st.one_of(
    # Non-dict entries
    st.none(),
    st.text(max_size=5),
    st.integers(),
    st.lists(st.text(max_size=3), max_size=2),
    # Missing label
    st.just({}),
    st.fixed_dictionaries({'prompt': st.text(max_size=5)}),
    # Non-string label
    st.fixed_dictionaries({'label': st.one_of(
        st.integers(), st.none(), st.booleans(), st.floats(allow_nan=False))}),
    # Blank label
    st.fixed_dictionaries({'label': st.sampled_from(['', ' ', '\t', '  \n '])}),
    # Non-string, non-None prompt on an otherwise valid entry
    st.fixed_dictionaries({
        'label': st.text(min_size=1, max_size=8).filter(lambda s: s.strip()),
        'prompt': st.one_of(
            st.integers(), st.booleans(),
            st.lists(st.text(max_size=3), max_size=1),
            st.dictionaries(st.text(max_size=3), st.text(max_size=3), max_size=1)),
    }),
)


@st.composite
def _malformed_prompts(draw):
    kind = draw(st.sampled_from(['non_list', 'empty_list', 'bad_entry']))
    if kind == 'non_list':
        return draw(_NON_LIST_PROMPTS)
    if kind == 'empty_list':
        return []
    valid = draw(st.lists(_VALID_PROMPT_ENTRY, max_size=3))
    bad = draw(_BAD_PROMPT_ENTRY)
    position = draw(st.integers(min_value=0, max_value=len(valid)))
    return valid[:position] + [bad] + valid[position:]


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

class TestProperty6CaptionSpansAndAttribution:
    @settings(max_examples=100, deadline=None)
    @given(case=_caption_span_cases())
    def test_caption_spans_partition_prompts_and_attribution_maps_onto_them(self, case):
        """
        Feature: grounded-sam-autolabel, Property 6: Caption spans
        partition the prompts and attribution is a function onto them

        **Validates: Requirements 3.3**
        """
        phrases_in, runs, pad_count, scores, box_threshold, text_threshold = case

        # -- Caption building normalizes without losing or reordering phrases
        caption, phrases = gsam_utils.build_caption(phrases_in)
        assert len(phrases) == len(phrases_in)
        for phrase in phrases:
            assert phrase  # never empty
            assert phrase == phrase.lower()
            assert phrase == ' '.join(phrase.split())  # inner whitespace collapsed
            assert not phrase.endswith('.') and not phrase.endswith(' ')
            assert '.' not in phrase  # inputs carry no inner dots by construction
        assert caption == '. '.join(phrases) + '.'  # canonical multi-phrase form

        # -- Synthetic tokenization: [CLS] run1 . run2 . ... . [SEP] [PAD]*
        token_ids = [_CLS_ID]
        expected_spans = []
        for run in runs:
            start = len(token_ids)
            token_ids.extend(run)
            expected_spans.append((start, len(token_ids)))
            token_ids.append(_DOT_ID)
        token_ids.append(_SEP_ID)
        token_ids.extend([_PAD_ID] * pad_count)

        spans = gsam_utils.phrase_token_spans(
            token_ids, {_DOT_ID}, {_CLS_ID, _SEP_ID, _PAD_ID})

        # One span per phrase, disjoint, ordered, covering exactly its tokens
        assert spans == expected_spans
        assert len(spans) == len(phrases)
        for (s0, e0), (s1, e1) in zip(spans, spans[1:]):
            assert s0 < e0 <= s1 < e1
        for (start, end), run in zip(spans, runs):
            assert token_ids[start:end] == run

        # -- Attribution: None, or exactly one phrase index meeting both
        #    thresholds with the winning span's max as the score
        result = gsam_utils.attribute_detection(
            scores, spans, box_threshold, text_threshold)
        span_maxima = [max(scores[start:end]) for start, end in spans]
        best = max(span_maxima)
        if best >= box_threshold and best >= text_threshold:
            assert result is not None
        else:
            assert result is None
        if result is not None:
            phrase_index, score = result
            assert 0 <= phrase_index < len(phrases)  # a supplied prompt label
            assert score >= box_threshold
            assert score >= text_threshold
            assert score == best == span_maxima[phrase_index]
            assert phrase_index == span_maxima.index(best)  # first wins ties


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

class TestProperty7DetectionSelectionPipeline:
    @settings(max_examples=100, deadline=None)
    @given(
        raw_candidates=st.lists(_RAW_CANDIDATE, max_size=12),
        width=st.integers(min_value=1, max_value=4000),
        height=st.integers(min_value=1, max_value=4000),
        max_detections=st.integers(min_value=1, max_value=5),
        iou_threshold=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_selection_is_bounded_deduplicated_capped_and_ordered(
            self, raw_candidates, width, height, max_detections, iou_threshold):
        """
        Feature: grounded-sam-autolabel, Property 7: The detection
        selection pipeline yields bounded, thresholded, deduplicated,
        capped detections

        (Score thresholding is the attribution stage's job and is
        asserted by Property 6; this property covers the clamp, the
        per-label NMS, the cap, and the ordering.)

        **Validates: Requirements 3.4, 3.6, 3.10**
        """
        candidates = []
        for raw in raw_candidates:
            box = gsam_utils.cxcywh_to_pixel_box(raw['raw_box'], width, height)
            if box is None:
                continue  # dropped: no positive clamped area (or malformed)
            # Clamped in-bounds with positive area (req 3.6); the tiny
            # tolerance absorbs the left+width float rounding.
            assert box['left'] >= 0.0 and box['top'] >= 0.0
            assert box['width'] > 0.0 and box['height'] > 0.0
            assert box['left'] + box['width'] <= width + 1e-6
            assert box['top'] + box['height'] <= height + 1e-6
            candidates.append({
                'label_index': raw['label_index'],
                'score': raw['score'],
                'box': box,
            })

        kept = gsam_utils.select_detections(
            candidates, max_detections=max_detections, iou_threshold=iou_threshold)

        # Capped, keeping at least the top-scoring candidate; empty in,
        # empty out — never an error (req 3.10)
        assert len(kept) <= max_detections
        if candidates:
            assert kept
        else:
            assert kept == []
        # A subset of the candidates (the originals, not copies)
        for detection in kept:
            assert any(detection is candidate for candidate in candidates)
        # Descending score order
        scores = [detection['score'] for detection in kept]
        assert scores == sorted(scores, reverse=True)
        # No same-label pair overlaps at or above the NMS threshold (req 3.4)
        for i in range(len(kept)):
            for j in range(i + 1, len(kept)):
                if kept[i]['label_index'] == kept[j]['label_index']:
                    iou = gsam_utils.box_iou(kept[i]['box'], kept[j]['box'])
                    assert iou < iou_threshold


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------

class TestProperty8RleIsCanonical:
    @settings(max_examples=100, deadline=None)
    @given(case=_mask_cases())
    def test_worker_rle_matches_shared_layer_and_round_trips(self, case):
        """
        Feature: grounded-sam-autolabel, Property 8: The worker's RLE is
        the canonical encoding

        **Validates: Requirements 3.7**
        """
        width, height, mask = case
        encoded = gsam_mask_utils.rle_encode(mask, width, height)
        assert encoded == dda_manifest.rle_encode(mask, width, height)
        assert list(dda_manifest.rle_decode(encoded, width, height)) == mask


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

class TestProperty9MalformedPromptsRejected:
    @settings(max_examples=100, deadline=None)
    @given(prompts=_malformed_prompts())
    def test_normalize_prompts_raises_on_malformed_input(self, prompts):
        """
        Feature: grounded-sam-autolabel, Property 9: Malformed prompt
        inputs are rejected at the worker boundary

        **Validates: Requirements 3.8**
        """
        with pytest.raises(ValueError):
            gsam_utils.normalize_prompts(prompts)


# ---------------------------------------------------------------------------
# Examples / smoke tests beside the properties
# ---------------------------------------------------------------------------

class TestMaskUtilsDriftGuard:
    def test_worker_copy_is_byte_identical_to_sam_workers(self):
        """The verbatim-copy invariant behind Property 8 (req 3.7)."""
        with open(os.path.join(_GSAM_WORKER_DIR, 'mask_utils.py'), 'rb') as f:
            grounded_copy = f.read()
        with open(os.path.join(_SAM_WORKER_DIR, 'mask_utils.py'), 'rb') as f:
            sam_original = f.read()
        assert grounded_copy == sam_original


class TestDefaultConstantsAndEnvParsing:
    def test_gsam_utils_default_constants(self):
        assert gsam_utils.DEFAULT_BOX_THRESHOLD == 0.35
        assert gsam_utils.DEFAULT_TEXT_THRESHOLD == 0.25
        assert gsam_utils.DEFAULT_BOX_NMS_IOU == 0.8
        assert gsam_utils.DEFAULT_MAX_DETECTIONS == 20

    def test_handler_defaults_with_clean_environment(self):
        """Req 3.5 smoke: the shipped Detection_Thresholds defaults."""
        handler = _load_gsam_handler(env={})
        assert handler.BOX_THRESHOLD == 0.35
        assert handler.TEXT_THRESHOLD == 0.25
        assert handler.NMS_IOU_THRESHOLD == 0.8
        assert handler.MAX_DETECTIONS == 20
        assert handler.MASK_LOGIT_THRESHOLD == 0.0
        assert handler.DINO_SIZE == 800
        assert handler.URL_FETCH_TIMEOUT_SECONDS == 30
        assert handler.GROUNDED_SAM_MODEL_PATH == '/opt/models'

    def test_handler_env_overrides_are_parsed(self):
        """Req 3.5: every threshold knob reads from its env var."""
        handler = _load_gsam_handler(env={
            'GROUNDED_SAM_BOX_THRESHOLD': '0.5',
            'GROUNDED_SAM_TEXT_THRESHOLD': '0.4',
            'GROUNDED_SAM_NMS_IOU_THRESHOLD': '0.65',
            'GROUNDED_SAM_MAX_DETECTIONS': '7',
            'GROUNDED_SAM_MASK_THRESHOLD': '0.25',
            'GROUNDED_SAM_DINO_SIZE': '640',
            'GROUNDED_SAM_URL_FETCH_TIMEOUT': '5',
            'GROUNDED_SAM_MODEL_PATH': '/tmp/models',
        })
        assert handler.BOX_THRESHOLD == 0.5
        assert handler.TEXT_THRESHOLD == 0.4
        assert handler.NMS_IOU_THRESHOLD == 0.65
        assert handler.MAX_DETECTIONS == 7
        assert handler.MASK_LOGIT_THRESHOLD == 0.25
        assert handler.DINO_SIZE == 640
        assert handler.URL_FETCH_TIMEOUT_SECONDS == 5
        assert handler.GROUNDED_SAM_MODEL_PATH == '/tmp/models'


class TestHandlerRejectsBeforeHeavyImports:
    """
    lambda_handler must raise ValueError on malformed events before any
    heavy import (req 3.8): each case runs with numpy / onnxruntime /
    PIL / tokenizers blocked, so any heavy import attempt would surface
    as ImportError instead of the expected ValueError.
    """

    def test_missing_image_source(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            with pytest.raises(ValueError, match='image_bytes_base64'):
                handler.lambda_handler({})

    def test_non_dict_event(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            with pytest.raises(ValueError, match='JSON object'):
                handler.lambda_handler([])

    def test_non_https_presigned_url(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            with pytest.raises(ValueError, match='https'):
                handler.lambda_handler(
                    {'image_s3_presigned_url': 'http://example.com/img.png'})

    def test_invalid_base64_image(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            with pytest.raises(ValueError, match='image_bytes_base64'):
                handler.lambda_handler({'image_bytes_base64': '!!not-base64!!'})

    def test_malformed_prompts(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            event = {
                'image_s3_presigned_url': 'https://example.com/img.png',
                'prompts': 'dent',
                'modality': 'Segmentation',
            }
            with pytest.raises(ValueError, match='prompts'):
                handler.lambda_handler(event)

    def test_unknown_modality(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            event = {
                'image_s3_presigned_url': 'https://example.com/img.png',
                'prompts': [{'label': 'dent'}],
                'modality': 'Classification',
            }
            with pytest.raises(ValueError, match='modality'):
                handler.lambda_handler(event)

    def test_invalid_max_detections(self):
        with _heavy_imports_blocked():
            handler = _load_gsam_handler(env={})
            event = {
                'image_s3_presigned_url': 'https://example.com/img.png',
                'prompts': [{'label': 'dent'}],
                'modality': 'Segmentation',
            }
            with pytest.raises(ValueError, match='max_detections'):
                handler.lambda_handler(dict(event, max_detections=0))
            with pytest.raises(ValueError, match='max_detections'):
                handler.lambda_handler(dict(event, max_detections='lots'))


class TestBuildCaptionExamples:
    def test_normalizes_and_joins_phrases(self):
        caption, phrases = gsam_utils.build_caption(
            ['  Small   Surface DENT..', 'scratch. '])
        assert caption == 'small surface dent. scratch.'
        assert phrases == ['small surface dent', 'scratch']

    def test_rejects_phrase_normalizing_to_empty(self):
        with pytest.raises(ValueError, match='empty phrase'):
            gsam_utils.build_caption(['...'])
        with pytest.raises(ValueError, match='empty phrase'):
            gsam_utils.build_caption(['dent', ' . . '])

    def test_rejects_non_sequence_and_non_string_inputs(self):
        with pytest.raises(ValueError):
            gsam_utils.build_caption('dent')
        with pytest.raises(ValueError):
            gsam_utils.build_caption(None)
        with pytest.raises(ValueError):
            gsam_utils.build_caption([])
        with pytest.raises(ValueError):
            gsam_utils.build_caption(['dent', 42])

    def test_inner_dot_phrase_breaks_span_alignment(self):
        """
        build_caption keeps inner dots, but the resulting caption then
        yields more token spans than phrases — the misalignment the
        handler rejects at runtime instead of mislabeling detections.
        """
        caption, phrases = gsam_utils.build_caption(['a.b', 'c'])
        assert caption == 'a.b. c.'
        assert phrases == ['a.b', 'c']
        # Synthetic tokenization: the inner '.' tokenizes as a separator,
        # splitting the first phrase into two spans.
        token_ids = [_CLS_ID, 10, _DOT_ID, 11, _DOT_ID, 12, _DOT_ID, _SEP_ID]
        spans = gsam_utils.phrase_token_spans(
            token_ids, {_DOT_ID}, {_CLS_ID, _SEP_ID, _PAD_ID})
        assert len(spans) == 3
        assert len(phrases) == 2


class TestNormalizePromptsExamples:
    def test_blank_or_absent_prompt_falls_back_to_label(self):
        assert gsam_utils.normalize_prompts([{'label': 'Dent', 'prompt': '  '}]) == [
            {'label': 'Dent', 'prompt': 'Dent'}]
        assert gsam_utils.normalize_prompts([{'label': 'dent'}]) == [
            {'label': 'dent', 'prompt': 'dent'}]

    def test_overrides_kept_verbatim_in_order(self):
        prompts = [
            {'label': 'dent', 'prompt': 'small surface dent'},
            {'label': 'scratch', 'prompt': None},
        ]
        assert gsam_utils.normalize_prompts(prompts) == [
            {'label': 'dent', 'prompt': 'small surface dent'},
            {'label': 'scratch', 'prompt': 'scratch'},
        ]


class TestSelectDetectionsExamples:
    def test_empty_input_yields_empty_selection(self):
        assert gsam_utils.select_detections([]) == []

    def test_rejects_non_positive_max_detections(self):
        with pytest.raises(ValueError, match='max_detections'):
            gsam_utils.select_detections([], max_detections=0)
