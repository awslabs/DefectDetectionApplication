# Feature: rtsp-rtmp-stream-cameras, Property 23: Label keys are addressable and idempotent
"""Property test P23 — Label_Keys are addressable and idempotent.

**Feature: rtsp-rtmp-stream-cameras, Property 23: Label keys are
addressable and idempotent**

*For any* label, ``label_key`` SHALL return a string that:

- Matches ``[a-z0-9]+(_[a-z0-9]+)*``, or is empty for a label with no
  letters or digits.
- Equals ``label_key`` applied to itself.
- Is usable as a dotted-path segment in the Condition_Language.

**Validates: Requirements 13.3, 13.4**

A Label_Key is the only thing that stands between a detector's label — an
arbitrary string chosen by whoever trained the model — and a metadata key
an operator types into a condition or an output template
(``counter.<nodeId>.counts.<Label_Key>``, Requirements 13.3 and 13.4). It
therefore has to be *addressable* (a legal path segment, resolvable) and
*stable* (typing the key back gets the same key, so the operator's
expression keeps working).

How each clause is checked:

1. *Soundness.* ``_oracle_label_key`` re-derives the glossary sentence
   ("lowercased, each run of characters other than ASCII letters and
   digits is replaced by one underscore, and leading and trailing
   underscores are removed") with plain character classification and a
   hand-written run collapse — no regex, so it shares nothing with the
   module's ``[^a-z0-9]+`` substitution.
2. *Shape.* Every result either matches the pattern spelled out in the
   property or is empty, and it is empty exactly when the lowercased
   label holds no ASCII letter or digit. The lowercasing comes first in
   both the requirement and the implementation, which is observable: the
   Kelvin sign ``\u212a`` holds no ASCII letter yet lowercases to ``k``
   and so yields the key ``k``. ``LABEL_KEY_PATTERN`` itself is pinned
   against the same spelled-out shape, so the constant the validator and
   the docs point at cannot drift from the property.
3. *Idempotence*, on the key and on the key of the key.
4. *Addressability.* The round trip goes through the **real**
   Condition_Language: the device evaluator in
   ``src/backend/workflow_engine/output_bindings.py``, which is the
   implementation ``inference_filter``, ``conditional`` and
   ``digital_output`` conditions and output templates run on. For a
   generated Detection_List the counter metadata is merged as
   Requirement 13.3 specifies and then addressed by dotted path: the
   path must tokenize as one identifier, ``evaluate_condition`` must
   report the right verdict on the count and the total,
   ``resolve_field_path`` must find the original label behind the key,
   and ``render_template`` must substitute the count into an output
   template (Requirement 13.4).
5. *The empty key.* A label with no ASCII letters or digits has an empty
   Label_Key, which is not a legal path segment. The property pins the
   module's guard for that case: ``parse_label_list`` reports such a
   *configured* label as a problem (which is what V13 turns into an error
   finding), so an unaddressable key can never reach a condition through
   a node parameter.
6. *Totality*, over the non-string labels a detector or a JSON payload
   can put in ``label``.

Scope notes:

- The device evaluator is imported from the repository tree, like the
  vendored-mirror assertions elsewhere in this suite. It is the consumer
  under test here, not the module under test, so using it is a round trip
  rather than a tautology; ``_tokenize`` is private but is exactly the
  grammar clause 3 of the property talks about, so it is used directly
  instead of being restated.
- Generated labels deliberately include non-ASCII text, punctuation runs,
  leading and trailing separators, and already-normalized keys, because
  those are the labels that make the normalization observable. They never
  need to avoid anything: ``label_key`` is total.
"""

from __future__ import annotations

import re
import string
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_core.analytics.scene import (
    LABEL_KEY_PATTERN,
    MAX_CLASS_LIST_ITEMS,
    count_detections,
    label_key,
    parse_label_list,
    run_metadata,
)

# ---------------------------------------------------------------------------
# The real Condition_Language (the consumer of a Label_Key)
# ---------------------------------------------------------------------------

#: The device module that implements the Condition_Language: the
#: tokenizer and evaluator behind every ``condition`` parameter, the
#: dotted field-path resolver, and the output-template renderer.
_EVALUATOR_RELATIVE = Path("src/backend/workflow_engine/output_bindings.py")


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / _EVALUATOR_RELATIVE).is_file():
            return candidate
    raise AssertionError(
        "Could not locate the repository root containing "
        f"{_EVALUATOR_RELATIVE}"
    )


# Appended, never prepended: src/backend carries generically named
# packages (``utils``, ``model``, ``metrics``) that must not shadow the
# host interpreter's own packages for the rest of the session.
_DEVICE_BACKEND = str(_repo_root() / "src" / "backend")
if _DEVICE_BACKEND not in sys.path:
    sys.path.append(_DEVICE_BACKEND)

from workflow_engine.output_bindings import (  # noqa: E402  (path set above)
    _tokenize,
    evaluate_condition,
    render_template,
    resolve_field_path,
)

#: The node id used in the metadata paths. Any node id the Portal issues
#: is a legal leading identifier; the Label_Key is the segment under test.
NODE_ID = "counter_1"

#: The metadata prefix of Requirement 13.3.
COUNTER_PREFIX = "counter.{0}".format(NODE_ID)


# ---------------------------------------------------------------------------
# Oracle: the glossary's Label_Key sentence, re-derived without regexes
# ---------------------------------------------------------------------------

#: The characters a Label_Key may contain. Classification happens after
#: lowercasing, so uppercase ASCII cannot reach it.
_KEY_CHARACTERS = frozenset(string.ascii_lowercase + string.digits)


def _oracle_label_key(label: Any) -> str:
    """The Label_Key of ``label``, straight from the glossary sentence.

    Lowercase the label; replace each run of characters other than ASCII
    letters and digits by one underscore; remove leading and trailing
    underscores. Written with character classification and an explicit
    run collapse so it shares no machinery with the implementation.
    """
    if label is None:
        return ""
    text = label if isinstance(label, str) else str(label)
    pieces: List[str] = []
    in_run = False
    for character in text.lower():
        if character in _KEY_CHARACTERS:
            pieces.append(character)
            in_run = False
        else:
            if not in_run:
                pieces.append("_")
            in_run = True
    key = "".join(pieces)
    while key.startswith("_"):
        key = key[1:]
    while key.endswith("_"):
        key = key[:-1]
    return key


def _is_spelled_out_label_key(text: str) -> bool:
    """Whether ``text`` matches ``[a-z0-9]+(_[a-z0-9]+)*`` by hand.

    The property spells the shape out as "underscore-separated, non-empty
    runs of lowercase ASCII letters and digits"; this reads it that way
    rather than compiling it, so the constant can be compared against the
    property instead of against itself.
    """
    if not isinstance(text, str) or text == "":
        return False
    parts = text.split("_")
    return all(
        part != "" and all(character in _KEY_CHARACTERS for character in part)
        for part in parts
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Separator characters, i.e. everything a run of which becomes one
#: underscore: ASCII punctuation and whitespace.
_SEPARATOR_CHARACTERS = "-_ ./,:;+*&%$#@!?()[]{}<>|\\\"'~`^=\t\n"

#: Non-ASCII fragments that make the lowercase-then-replace order, and
#: the "no ASCII letters or digits" case, observable.
_UNICODE_FRAGMENTS = (
    "\u00fc",  # u with diaeresis: not ASCII, becomes a separator
    "\u00c9",  # E acute
    "P\u00e9rson",  # the mixed case the deterministic suite pins
    "\u65e5\u672c",  # CJK
    "\U0001f642",  # emoji
    "\u212a",  # Kelvin sign: lowercases to ASCII "k"
    "\u0130",  # I with dot above: lowercases to "i" + combining mark
    "\u00df",  # sharp s
    "\u00b2",  # superscript two: a digit, but not an ASCII digit
    "\uff46\uff55\uff4c\uff4c",  # fullwidth letters
)

#: Labels that already are Label_Keys, plus the ones the normalization
#: turns into nothing. Kept explicit so every run sees them.
_CURATED_LABELS = (
    "person",
    "Person",
    "PERSON",
    "hard_hat",
    "Hard Hat",
    "hard-hat",
    "  hard   hat  ",
    "safety_vest",
    "class 3",
    "3M",
    "3m_mask",
    "true",
    "false",
    "42",
    "0.5",
    "a__b",
    "_a_",
    "---",
    "",
    " ",
    "\n",
    "person.helmet",
    "person::helmet",
)


@st.composite
def _structured_labels(draw: Any) -> str:
    """A label assembled from words, separator runs and unicode."""
    fragments = draw(
        st.lists(
            st.one_of(
                st.text(
                    alphabet=string.ascii_letters + string.digits,
                    min_size=1,
                    max_size=6,
                ),
                st.text(alphabet=_SEPARATOR_CHARACTERS, min_size=1, max_size=3),
                st.sampled_from(_UNICODE_FRAGMENTS),
            ),
            min_size=0,
            max_size=6,
        )
    )
    return "".join(fragments)


def _labels() -> st.SearchStrategy[str]:
    """Every kind of label a detector, or an operator, can produce."""
    return st.one_of(
        st.sampled_from(_CURATED_LABELS),
        _structured_labels(),
        st.text(max_size=24),
        st.text(alphabet=_SEPARATOR_CHARACTERS, max_size=6),
    )


def _non_string_labels() -> st.SearchStrategy[Any]:
    """The non-strings a ``label`` field can hold after JSON parsing."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False, width=16),
        st.lists(st.integers(min_value=0, max_value=9), max_size=3),
    )


@st.composite
def _addressable_labels(draw: Any) -> str:
    """Labels whose Label_Key is non-empty, hence addressable.

    Built by repair rather than by filtering, so the distribution keeps
    the whole corpus (including the labels that are *nearly* empty) and
    no example is thrown away: a label that would normalize to nothing
    gets one ASCII word appended.
    """
    label = draw(_labels())
    if _oracle_label_key(label) != "":
        return label
    word = draw(
        st.text(
            alphabet=string.ascii_letters + string.digits, min_size=1, max_size=6
        )
    )
    return label + word


def _detections(labels: List[str]) -> List[Dict[str, Any]]:
    """A Detection_List over ``labels``: one high-confidence box each.

    The boxes are identical and no Zone is configured, so the counter's
    filtering plays no part here — Property 24 is what pins that.
    """
    return [
        {
            "id": "d{0}".format(index),
            "label": label,
            "confidence": 1.0,
            "x_min": 0.0,
            "y_min": 0.0,
            "x_max": 10.0,
            "y_max": 10.0,
        }
        for index, label in enumerate(labels)
    ]


def _counter_metadata(labels: List[str], classes: str = "") -> Dict[str, Any]:
    """The run metadata of a ``detection_counter`` node (Requirement 13.3)."""
    result = count_detections(_detections(labels), classes=classes)
    return {"counter": {NODE_ID: run_metadata(result)}}


# ---------------------------------------------------------------------------
# Clause 1: the normalization is the glossary's
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_labels())
def test_label_key_equals_the_glossary_oracle(label: str) -> None:
    assert label_key(label) == _oracle_label_key(label), label


# ---------------------------------------------------------------------------
# Clause 1 (shape): the result matches the spelled-out pattern
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_labels())
def test_label_key_matches_the_spelled_out_shape(label: str) -> None:
    key = label_key(label)
    if key == "":
        return
    assert _is_spelled_out_label_key(key), (label, key)
    # No leading, trailing or doubled underscore survives.
    assert not key.startswith("_") and not key.endswith("_"), key
    assert "__" not in key, key


@settings(max_examples=100)
@given(_labels())
def test_label_key_is_empty_exactly_without_ascii_letters_or_digits(
    label: str,
) -> None:
    """Empty is reserved for a label that carries nothing addressable.

    Judged on the *lowercased* label, which is the order the requirement
    states: the Kelvin sign holds no ASCII letter but lowercases to one.
    """
    lowered = label.lower()
    has_addressable_character = any(
        character in _KEY_CHARACTERS for character in lowered
    )
    assert (label_key(label) != "") is has_addressable_character, label


@settings(max_examples=100)
@given(st.text(alphabet=string.ascii_lowercase + string.digits + "_", max_size=16))
def test_label_key_pattern_constant_is_the_spelled_out_shape(text: str) -> None:
    """``LABEL_KEY_PATTERN`` accepts exactly the property's shape.

    The constant is what the validator, the catalog documentation and the
    frontend mirror point at, so it is checked against the hand-read
    shape rather than being trusted.
    """
    matched = re.fullmatch(LABEL_KEY_PATTERN, text) is not None
    assert matched is _is_spelled_out_label_key(text), text


# ---------------------------------------------------------------------------
# Clause 2: idempotence
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_labels())
def test_label_key_is_idempotent(label: str) -> None:
    key = label_key(label)
    assert label_key(key) == key, (label, key)
    assert label_key(label_key(key)) == key, (label, key)


# ---------------------------------------------------------------------------
# Clause 3: addressable from the Condition_Language and from templates
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_addressable_labels())
def test_label_key_is_one_condition_language_identifier(label: str) -> None:
    """``counter.<nodeId>.counts.<Label_Key>`` is a single identifier.

    A key that needed quoting, or that broke into several tokens, would
    not be addressable however the rest of the expression is written.
    """
    key = label_key(label)
    path = "{0}.counts.{1}".format(COUNTER_PREFIX, key)
    assert _tokenize(path) == [path], (label, key)
    assert _tokenize("{0} >= 1".format(path)) == [path, ">=", "1"], key


@settings(max_examples=100)
@given(st.lists(_labels(), min_size=1, max_size=6))
def test_counter_counts_are_addressable_by_label_key(labels: List[str]) -> None:
    """The round trip: label -> Label_Key -> dotted path -> the count.

    Every non-empty key in the merged metadata resolves, compares
    correctly in a condition, and carries its original label behind
    ``labels.<Label_Key>``.
    """
    metadata = _counter_metadata(labels)
    counts = metadata["counter"][NODE_ID]["counts"]
    expected = Counter(_oracle_label_key(label) for label in labels)

    assert dict(counts) == dict(expected), labels

    for key, count in counts.items():
        if key == "":
            # Not addressable, and never configurable: see
            # test_unaddressable_labels_are_reported_at_configuration_time.
            continue
        path = "{0}.counts.{1}".format(COUNTER_PREFIX, key)
        found, value = resolve_field_path(metadata, path)
        assert found and value == count, (key, path)
        assert evaluate_condition("{0} == {1}".format(path, count), metadata)
        assert evaluate_condition("{0} >= {1}".format(path, count), metadata)
        assert not evaluate_condition("{0} > {1}".format(path, count), metadata)
        assert evaluate_condition(
            "{0} > {1} && {2}.total >= {3}".format(
                path, count - 1, COUNTER_PREFIX, count
            ),
            metadata,
        )
        # labels.<Label_Key> holds the label as the detector wrote it.
        found, original = resolve_field_path(
            metadata, "{0}.labels.{1}".format(COUNTER_PREFIX, key)
        )
        assert found, key
        assert _oracle_label_key(original) == key, (original, key)

    total_path = "{0}.total".format(COUNTER_PREFIX)
    assert evaluate_condition(
        "{0} == {1}".format(total_path, sum(counts.values())), metadata
    )


@settings(max_examples=100)
@given(st.lists(_addressable_labels(), min_size=1, max_size=4))
def test_zero_filled_classes_are_addressable_by_label_key(
    labels: List[str],
) -> None:
    """A class listed but never seen is still addressable, at zero.

    The ``classes`` parameter is written as the operator spells it, so
    this also exercises label -> Label_Key on the configuration side
    (Requirement 13.3, "with every label in ``classes`` present and zero
    when unseen").
    """
    configured = [label for label in labels if "," not in label]
    assume(configured)
    metadata = _counter_metadata([], classes=", ".join(configured))
    counts = metadata["counter"][NODE_ID]["counts"]
    for label in configured:
        key = _oracle_label_key(label)
        assert counts[key] == 0, (label, key)
        path = "{0}.counts.{1}".format(COUNTER_PREFIX, key)
        assert evaluate_condition("{0} == 0".format(path), metadata)
        assert not evaluate_condition("{0} >= 1".format(path), metadata)


@settings(max_examples=100)
@given(_addressable_labels(), st.integers(min_value=1, max_value=4))
def test_label_key_paths_render_in_output_templates(
    label: str, repeats: int
) -> None:
    """Requirement 13.4's second half: output templates resolve the path."""
    key = label_key(label)
    metadata = _counter_metadata([label] * repeats)
    path = "{0}.counts.{1}".format(COUNTER_PREFIX, key)
    # A template that is exactly the placeholder keeps the native type.
    assert render_template("{" + path + "}", metadata) == repeats
    # Inside a larger template it substitutes as text.
    rendered = render_template("seen {" + path + "} of " + key, metadata)
    assert rendered == "seen {0} of {1}".format(repeats, key)


# ---------------------------------------------------------------------------
# Clause 3 (the empty key): unaddressable labels are refused at config time
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_labels())
def test_unaddressable_labels_are_reported_at_configuration_time(
    label: str,
) -> None:
    """A configured label whose Label_Key is empty is a reported problem.

    ``parse_label_list`` is the shared parser V13 reports through, so a
    key that could not be addressed never reaches a condition through a
    node parameter. A blank value is "not configured" rather than
    malformed, and is excluded here.
    """
    assume("," not in label)
    assume(label.strip() != "")
    keys, problems = parse_label_list(label, MAX_CLASS_LIST_ITEMS)
    key = label_key(label)
    if key == "":
        assert keys == [] and problems, label
    else:
        assert keys == [key] and problems == [], label


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_non_string_labels())
def test_label_key_is_total_for_non_string_labels(value: Any) -> None:
    key = label_key(value)
    assert isinstance(key, str)
    assert key == _oracle_label_key(value), value
    assert key == "" or _is_spelled_out_label_key(key), (value, key)
    assert label_key(key) == key, value


@settings(max_examples=100)
@given(st.lists(_non_string_labels(), min_size=1, max_size=4))
def test_non_string_detection_labels_stay_addressable(
    values: List[Any],
) -> None:
    """A detection whose label came back as a number is still countable."""
    metadata = _counter_metadata(values)
    counts = metadata["counter"][NODE_ID]["counts"]
    assert dict(counts) == dict(
        Counter(_oracle_label_key(value) for value in values)
    ), values
    for key, count in counts.items():
        if key == "":
            continue
        path = "{0}.counts.{1}".format(COUNTER_PREFIX, key)
        assert _tokenize(path) == [path], key
        assert evaluate_condition("{0} == {1}".format(path, count), metadata)
