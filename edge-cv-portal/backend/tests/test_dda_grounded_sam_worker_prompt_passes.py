"""
Handler tests for how the dda_grounded_sam_worker sends a Prompt_Map's
prompts to Grounding DINO.

The deployed Grounding DINO export (onnx-community grounding-dino-tiny)
builds the text attention mask and position ids with a loop over the
caption's special tokens, and that loop was unrolled for a one-phrase
caption when the model was traced. In a joined caption
('p1. p2. p3.') only the first phrase is encoded as a phrase: every
later token attends only to itself, at position 0, and never reaches the
Box_Threshold. These tests drive the real ``lambda_handler`` with a fake
tokenizer and a fake Grounding DINO session that emulates that export:
the session scores only the caption's first phrase, looked up by its
text in a per-test script, and every token of a later phrase gets logit
-1e4.

- Single-label preservation: a one-prompt invocation sends today's
  caption to exactly one forward and returns exactly today's response,
  and the caption alignment guard keeps its message. These pass on the
  handler before the fix and must keep passing after it.
- Several labels: the handler runs one forward per prompt, each on a
  one-phrase caption, over one preprocessed image, and every label gets
  its own regions; the merged candidates share one per-label NMS, one
  cap and one SAM pass. These fail on the handler before the fix, which
  returns only the first label.

The fake emits only the logits +1e4, 0 and -1e4, whose sigmoids are
exactly 1.0, 0.5 and 0.0 under any numpy, and boxes in binary
fractions, so the expected scores and pixel boxes are exact.

Module-loading note (the ``test_dda_grounded_sam_worker_utils.py`` and
``test_gsam_mask_offset_preservation.py`` precedent): the worker modules
are loaded from explicit file paths, ``gsam_utils`` and ``mask_utils``
are registered with ``setdefault``, the handler is loaded under a unique
module name with its environment scrubbed, the worker directory is never
put on ``sys.path`` and ``sys.modules['handler']`` is never populated.
The fakes are injected through the handler's ``_TOKENIZER`` and
``_DINO_SESSION`` globals, so neither onnxruntime nor tokenizers is
needed.
"""
import base64
import importlib.util
import io
import os
import re
import sys
from collections import namedtuple

import pytest

np = pytest.importorskip(
    'numpy', reason='the handler tests decode and preprocess a real image')
Image = pytest.importorskip(
    'PIL.Image', reason='the handler tests decode and preprocess a real image')

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, '..'))
_GSAM_WORKER_DIR = os.path.join(_BACKEND, 'grounded-sam-worker')


def _load_module_from(file_path, module_name):
    """Load a module instance from an explicit file path (no sys.path)."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The handler's `from gsam_utils import ...` / `from mask_utils import ...`
# must resolve; setdefault keeps this file compatible with the sibling
# test files registering their own (identical) instances first.
sys.modules.setdefault('gsam_utils', _load_module_from(
    os.path.join(_GSAM_WORKER_DIR, 'gsam_utils.py'), 'gsam_utils'))
sys.modules.setdefault('mask_utils', _load_module_from(
    os.path.join(_GSAM_WORKER_DIR, 'mask_utils.py'),
    'gsam_worker_mask_utils_prompt_passes'))
gsam_utils = sys.modules['gsam_utils']

# Environment variables the handler reads at import time.
_HANDLER_ENV_PREFIXES = ('GROUNDED_SAM_', 'GROUNDING_DINO_')
_HANDLER_ENV_EXACT = ('SAM_ENCODER_PATH', 'SAM_DECODER_PATH')


def _load_gsam_handler():
    """Load a fresh grounded-sam handler instance with a clean env."""
    saved = {}
    for key in list(os.environ):
        if key.startswith(_HANDLER_ENV_PREFIXES) or key in _HANDLER_ENV_EXACT:
            saved[key] = os.environ.pop(key)
    try:
        return _load_module_from(
            os.path.join(_GSAM_WORKER_DIR, 'handler.py'),
            'gsam_worker_handler_prompt_passes',
        )
    finally:
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# Fakes: BERT-style tokenizer and the deployed one-phrase DINO export
# ---------------------------------------------------------------------------

_PAD_ID, _CLS_ID, _SEP_ID, _DOT_ID = 0, 101, 102, 1012
_MARKER_IDS = frozenset((_PAD_ID, _CLS_ID, _SEP_ID, _DOT_ID))

# Logits with exact sigmoids: 1.0, 0.5 and 0.0
LOGIT_ONE, LOGIT_HALF, LOGIT_ZERO = 1e4, 0.0, -1e4
_NUM_QUERIES = 8


class _Encoding:
    def __init__(self, ids, tokens):
        self.ids = ids
        self.tokens = tokens
        self.attention_mask = [1] * len(ids)
        self.type_ids = [0] * len(ids)


class FakeTokenizer:
    """
    Word-and-punctuation tokenizer with BERT's marker ids: [PAD] 0,
    [CLS] 101, [SEP] 102 and '.' 1012. Every other token gets a stable
    id from 2000 upward, in order of first appearance.
    """

    def __init__(self):
        self._ids = {'[PAD]': _PAD_ID, '[CLS]': _CLS_ID,
                     '[SEP]': _SEP_ID, '.': _DOT_ID}
        self._tokens = {token_id: token for token, token_id in self._ids.items()}

    def _id(self, token):
        if token not in self._ids:
            token_id = 2000 + len(self._ids) - 4
            self._ids[token] = token_id
            self._tokens[token_id] = token
        return self._ids[token]

    def encode(self, text):
        tokens = ['[CLS]'] + re.findall(r'\w+|[^\w\s]', text) + ['[SEP]']
        return _Encoding([self._id(token) for token in tokens], tokens)

    def token_to_id(self, token):
        return self._ids.get(token)

    def id_to_token(self, token_id):
        return self._tokens[token_id]


_GraphArg = namedtuple('_GraphArg', 'name shape')


def _first_phrase_positions(ids):
    """Token positions of the caption's first phrase (a non-marker run)."""
    positions = []
    for index, token_id in enumerate(ids):
        if token_id in _MARKER_IDS:
            if positions:
                break
        else:
            positions.append(index)
    return positions


class FakeDinoSession:
    """
    Stands in for the deployed Grounding DINO export: only the caption's
    first phrase is scored. Its text is looked up in `script`, which
    maps a phrase to the queries it fires, each as
    ``(target, logit, (cx, cy, w, h))``; target 'phrase' puts the logit
    on the phrase's tokens and 'dot' on the '.' that ends it. Every other
    token, a later phrase's included, gets logit -1e4.
    """

    def __init__(self, tokenizer, script):
        self._tokenizer = tokenizer
        self._script = script
        self.calls = []

    def get_inputs(self):
        return [
            _GraphArg('pixel_values', [1, 3, 800, 800]),
            _GraphArg('input_ids', ['batch_size', 'sequence_length']),
            _GraphArg('attention_mask', ['batch_size', 'sequence_length']),
            _GraphArg('token_type_ids', ['batch_size', 'sequence_length']),
        ]

    def get_outputs(self):
        return [
            _GraphArg('logits', ['batch_size', 'num_queries', 'sequence_length']),
            _GraphArg('pred_boxes', ['batch_size', 'num_queries', 4]),
        ]

    def run(self, output_names, feeds):
        self.calls.append(
            {name: np.array(value, copy=True) for name, value in feeds.items()})
        ids = [int(token_id) for token_id in feeds['input_ids'][0]]
        logits = np.full((1, _NUM_QUERIES, len(ids)), LOGIT_ZERO, dtype=np.float32)
        boxes = np.zeros((1, _NUM_QUERIES, 4), dtype=np.float32)
        phrase = _first_phrase_positions(ids)
        if phrase:
            text = ' '.join(self._tokenizer.id_to_token(ids[p]) for p in phrase)
            for query, (target, logit, box) in enumerate(self._script.get(text, ())):
                if target == 'phrase':
                    logits[0, query, phrase] = logit
                else:
                    logits[0, query, phrase[-1] + 1] = logit
                boxes[0, query] = box
        return [logits, boxes]


class _SegmentRecorder:
    """Replaces `_segment_masks`: records the detections, returns fake RLEs."""

    def __init__(self):
        self.calls = []

    def __call__(self, image_rgb, detections):
        self.calls.append(list(detections))
        return [(detection, f'rle-{index}')
                for index, detection in enumerate(detections)]


_WIDTH, _HEIGHT = 64, 48


def _png_base64():
    """A 64x48 RGB gradient PNG, base64-encoded."""
    rows = np.arange(_HEIGHT, dtype=np.uint8)[:, None]
    cols = np.arange(_WIDTH, dtype=np.uint8)[None, :]
    rgb = np.stack(np.broadcast_arrays(rows * 4, cols * 3, rows + cols), axis=-1)
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8)).save(
        buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def _event(prompts, modality, **extra):
    return dict({'image_bytes_base64': _png_base64(), 'prompts': prompts,
                 'modality': modality}, **extra)


@pytest.fixture
def gsam():
    """Factory: a fresh handler with the fake tokenizer and DINO session."""
    def make(script):
        handler = _load_gsam_handler()
        tokenizer = FakeTokenizer()
        session = FakeDinoSession(tokenizer, script)
        handler._TOKENIZER = tokenizer
        handler._DINO_SESSION = session
        return handler, tokenizer, session
    return make


# Normalized (cx, cy, w, h) boxes and their pixel boxes on the 64x48 image
_BOX_A = (0.25, 0.5, 0.25, 0.5)
_PIXEL_A = {'left': 8.0, 'top': 12.0, 'width': 16.0, 'height': 24.0}
_BOX_B = (0.75, 0.5, 0.25, 0.5)
_PIXEL_B = {'left': 40.0, 'top': 12.0, 'width': 16.0, 'height': 24.0}

# 'rounded nose piece' as the first phrase of a caption
_NOSE_SCRIPT = {'rounded nose piece': [
    ('phrase', LOGIT_ONE, _BOX_A),                   # q0: kept
    ('phrase', LOGIT_HALF, (0.25, 0.5, 0.25, 0.4375)),  # q1: IoU 0.875 with q0, NMS drops it
    ('phrase', LOGIT_HALF, _BOX_B),                  # q2: kept
    ('phrase', LOGIT_ONE, (0.5, 0.5, 0.0, 0.5)),     # q3: zero width, the clamp drops it
    ('dot', LOGIT_ONE, (0.5, 0.5, 0.5, 0.5)),        # q4: outside the span, attribution drops it
]}
_NOSE_PROMPT = [{'label': 'front_fender', 'prompt': 'Rounded  Nose Piece'}]


# ---------------------------------------------------------------------------
# Single-label preservation (passes before and after the fix)
# ---------------------------------------------------------------------------

def test_single_label_object_detection_output_unchanged(gsam):
    handler, tokenizer, session = gsam(_NOSE_SCRIPT)

    response = handler.lambda_handler(_event(_NOSE_PROMPT, 'ObjectDetection'))

    assert response == {
        'regions': [
            {'class': 'front_fender', 'score': 1.0, 'box': _PIXEL_A},
            {'class': 'front_fender', 'score': 0.5, 'box': _PIXEL_B},
        ],
        'image_width': _WIDTH,
        'image_height': _HEIGHT,
    }
    caption = gsam_utils.build_caption(['Rounded  Nose Piece'])[0]
    assert caption == 'rounded nose piece.'
    assert len(session.calls) == 1
    assert session.calls[0]['input_ids'].tolist() == [tokenizer.encode(caption).ids]


def test_single_label_segmentation_output_unchanged(gsam, monkeypatch):
    handler, _tokenizer, session = gsam(_NOSE_SCRIPT)
    recorder = _SegmentRecorder()
    monkeypatch.setattr(handler, '_segment_masks', recorder)

    response = handler.lambda_handler(_event(_NOSE_PROMPT, 'Segmentation'))

    assert response == {
        'regions': [
            {'class': 'front_fender', 'score': 1.0, 'rle': 'rle-0'},
            {'class': 'front_fender', 'score': 0.5, 'rle': 'rle-1'},
        ],
        'image_width': _WIDTH,
        'image_height': _HEIGHT,
    }
    assert recorder.calls == [[
        {'label_index': 0, 'score': 1.0, 'box': _PIXEL_A},
        {'label_index': 0, 'score': 0.5, 'box': _PIXEL_B},
    ]]
    assert len(session.calls) == 1


def test_alignment_guard_message_unchanged(gsam):
    handler, _tokenizer, session = gsam({})

    with pytest.raises(ValueError) as single:
        handler.lambda_handler(_event(
            [{'label': 'gap', 'prompt': 'a.b'}], 'ObjectDetection'))
    assert str(single.value) == (
        'caption token spans (2) do not align with the 1 prompts; '
        'a prompt likely contains inner sentence punctuation')

    with pytest.raises(ValueError) as several:
        handler.lambda_handler(_event(
            [{'label': 'dent', 'prompt': 'dent'},
             {'label': 'gap', 'prompt': 'a.b'},
             {'label': 'chip', 'prompt': 'chip'}],
            'ObjectDetection'))
    assert str(several.value) == (
        'caption token spans (4) do not align with the 3 prompts; '
        'a prompt likely contains inner sentence punctuation')

    assert session.calls == []


# ---------------------------------------------------------------------------
# Several labels: one single-phrase pass per prompt (fails before the fix)
# ---------------------------------------------------------------------------

_SHAPE_PROMPTS = [
    {'label': 'front_fender', 'prompt': 'rounded nose piece'},
    {'label': 'rear_fender', 'prompt': 'square tab piece'},
    {'label': 'roof', 'prompt': 'half disc'},
]
_SHAPE_PHRASES = ['rounded nose piece', 'square tab piece', 'half disc']
_BOX_C = (0.5, 0.25, 0.5, 0.25)
_PIXEL_C = {'left': 16.0, 'top': 6.0, 'width': 32.0, 'height': 12.0}


def _shape_script(nose=LOGIT_ONE, tab=LOGIT_ONE, disc=LOGIT_ONE, tab_box=_BOX_B):
    """One detection per phrase, with its own box, when it is first in a caption."""
    return {
        'rounded nose piece': [('phrase', nose, _BOX_A)],
        'square tab piece': [('phrase', tab, tab_box)],
        'half disc': [('phrase', disc, _BOX_C)],
    }


def test_one_single_phrase_pass_per_label(gsam, monkeypatch):
    handler, tokenizer, session = gsam(_shape_script())
    preprocessed = []
    real_preprocess = handler._dino_preprocess

    def counting_preprocess(image_rgb):
        preprocessed.append(image_rgb.shape)
        return real_preprocess(image_rgb)

    monkeypatch.setattr(handler, '_dino_preprocess', counting_preprocess)

    handler.lambda_handler(_event(_SHAPE_PROMPTS, 'ObjectDetection'))

    assert len(session.calls) == 3
    for call, phrase in zip(session.calls, _SHAPE_PHRASES):
        expected_ids = tokenizer.encode(f'{phrase}.').ids
        assert call['input_ids'].tolist() == [expected_ids]
        assert expected_ids.count(_DOT_ID) == 1
    assert preprocessed == [(_HEIGHT, _WIDTH, 3)]
    for call in session.calls[1:]:
        assert np.array_equal(call['pixel_values'], session.calls[0]['pixel_values'])


def test_every_label_gets_its_regions(gsam):
    handler, _tokenizer, _session = gsam(_shape_script())

    response = handler.lambda_handler(_event(_SHAPE_PROMPTS, 'ObjectDetection'))

    assert response['regions'] == [
        {'class': 'front_fender', 'score': 1.0, 'box': _PIXEL_A},
        {'class': 'rear_fender', 'score': 1.0, 'box': _PIXEL_B},
        {'class': 'roof', 'score': 1.0, 'box': _PIXEL_C},
    ]


def test_segmentation_runs_one_sam_pass_for_all_labels(gsam, monkeypatch):
    handler, _tokenizer, _session = gsam(_shape_script())
    recorder = _SegmentRecorder()
    monkeypatch.setattr(handler, '_segment_masks', recorder)

    response = handler.lambda_handler(_event(_SHAPE_PROMPTS, 'Segmentation'))

    assert len(recorder.calls) == 1
    assert [detection['label_index'] for detection in recorder.calls[0]] == [0, 1, 2]
    assert [(region['class'], region['rle']) for region in response['regions']] == [
        ('front_fender', 'rle-0'), ('rear_fender', 'rle-1'), ('roof', 'rle-2')]


def test_cap_applies_to_the_merged_passes(gsam):
    handler, _tokenizer, _session = gsam(_shape_script(tab=LOGIT_HALF))

    response = handler.lambda_handler(
        _event(_SHAPE_PROMPTS, 'ObjectDetection', max_detections=2))

    assert [(region['class'], region['score']) for region in response['regions']] == [
        ('front_fender', 1.0), ('roof', 1.0)]


def test_same_box_under_two_labels_is_kept_for_both(gsam):
    handler, _tokenizer, _session = gsam(_shape_script(tab_box=_BOX_A))

    response = handler.lambda_handler(_event(_SHAPE_PROMPTS[:2], 'ObjectDetection'))

    assert response['regions'] == [
        {'class': 'front_fender', 'score': 1.0, 'box': _PIXEL_A},
        {'class': 'rear_fender', 'score': 1.0, 'box': _PIXEL_A},
    ]
