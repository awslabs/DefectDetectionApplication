# Feature: rtsp-rtmp-stream-cameras, Property 26: Event gate automaton
"""Property test P26 — the event gate agrees with a reference automaton.

**Feature: rtsp-rtmp-stream-cameras, Property 26: Event gate automaton**

*For any* sequence of condition outcomes (true, false, or unevaluable),
timestamps, and parameters, ``step_event_gate`` SHALL match a reference
automaton:

- The gate activates after exactly ``activate_after`` consecutive trues.
- The gate clears after exactly ``clear_after`` consecutive falses, with
  unevaluable outcomes counting as false.
- ``passed`` is true on exactly the runs the emit rule selects.

**Validates: Requirements 15.2, 15.3**

``step_event_gate`` is the whole of an ``event_gate`` node: its
``passed`` decides whether a run reaches the gate's downstream nodes
(Requirement 15.3) and its state becomes ``event.<nodeId>.state``,
``.transition``, ``.active_since`` and the consecutive counts in the run
metadata (Requirement 15.4). The same function runs on the device and in
the Portal's cloud test sandbox (Requirement 15.6), so the values *and*
the metadata document must agree exactly.

How each clause is checked:

1. *The automaton* is judged by :func:`_oracle_trace`, an independent
   formulation of Requirement 15.2. Where the module carries incremental
   counters, the oracle decides each run by **slicing the verdict
   sequence**: the gate activates on a run when it is inactive and the
   last ``activate_after`` verdicts are all true, and clears when it is
   active and the last ``clear_after`` verdicts are all false. The two
   are exactly equivalent — a consecutive counter *is* the length of the
   prefix's trailing run of the current verdict, which the oracle
   recomputes from scratch with :func:`_trailing_run` — and they share no
   code.
2. *Unevaluable outcomes* are pinned twice: the oracle maps ``None`` to
   false before it starts, and
   :func:`test_event_gate_counts_an_unevaluable_outcome_as_false` shows
   that rewriting every ``None`` in a sequence to ``False`` leaves the
   whole trace untouched.
3. *The emit rule* is checked against the oracle for all three modes and
   then, per mode, against a direct reading of Requirement 15.3:
   ``on_activate`` passes exactly the activating runs, ``on_change``
   exactly the activating and clearing runs, ``while_active`` exactly the
   runs the gate is active — and, when ``repeat_interval_ms`` is greater
   than 0, at most once per window, which
   :func:`test_event_gate_while_active_passes_at_most_once_per_repeat_interval`
   states as "consecutive passes within one activation are at least a
   window apart, the first run of every activation passes, and a
   suppressed run is inside the window".

Beyond the oracle equality, the properties pin what a reference automaton
cannot state on its own: activation and clearing at the *exact* run for a
generated threshold, the emit mode never influencing the automaton, the
automaton depending on the verdicts alone (timestamps only move
``active_since``/``last_emit``), invariance under a shift of the clock,
resumability from the carried state and the inactive restart of
Requirement 15.5, the Requirement 15.4 metadata shape, determinism,
totality on malformed parameters, and the documented fallbacks for a
malformed threshold.

Scope notes, all of them deliberate:

- The oracle reproduces the ``last_emit_ms`` **field** with the module's
  own assignment order (reset on a clear, then set on a pass), because a
  state comparison has to reproduce it exactly; the emit *decision* it is
  used for is formulated independently, as the timestamp of the most
  recent pass within the current activation
  (:func:`_oracle_trace`'s ``episode_passes``). Under ``while_active``
  the two coincide — a gate only leaves the active state through a clear,
  which forgets the last pass — and a divergence would fail the oracle
  equality.
- Traces start from a fresh :class:`EventGateState`, the state a new
  registration and a restarted backend begin from (Requirement 15.5).
  Arbitrary *carried* states are covered by
  :func:`test_event_gate_resumes_from_the_carried_state`, which splits a
  sequence and resumes, so every state the module can actually produce is
  exercised as a starting state.
- The malformed-input corpus replaces the ``state`` argument wholesale
  (``None``, text, a mapping — what a broken store hands back) but never
  fabricates an :class:`EventGateState` with ill-typed *fields*: those
  values are only ever produced by this module, which keeps them ints or
  ``None``.
- Timestamps are generated non-decreasing for the properties that speak
  about a window, and with backwards jumps for
  :func:`test_event_gate_matches_the_reference_automaton_when_the_clock_jumps_backwards`,
  since a clock that jumps backwards may delay an emit (the module's
  documented choice) and the oracle must agree with it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from workflow_core.analytics.scene import (
    DEFAULT_ACTIVATE_AFTER,
    DEFAULT_CLEAR_AFTER,
    DEFAULT_EMIT,
    EMIT_MODES,
    EMIT_ON_ACTIVATE,
    EMIT_ON_CHANGE,
    EMIT_WHILE_ACTIVE,
    GATE_STATE_ACTIVE,
    GATE_STATE_INACTIVE,
    TRANSITION_ACTIVATED,
    TRANSITION_CLEARED,
    TRANSITION_NONE,
    EventGateState,
    event_gate_metadata,
    step_event_gate,
)

Record = Tuple[EventGateState, bool, str]


# ---------------------------------------------------------------------------
# The reference automaton (shares no code with the module)
# ---------------------------------------------------------------------------


def _verdicts(outcomes: Sequence[Any]) -> List[bool]:
    """Requirement 15.2: an outcome that cannot be evaluated is false."""
    return [False if outcome is None else bool(outcome) for outcome in outcomes]


def _trailing_run(verdicts: Sequence[bool], index: int, value: bool) -> int:
    """How many runs up to and including ``index`` ended in ``value``.

    The module's ``consecutive_true``/``consecutive_false`` counters, as a
    property of the sequence rather than as carried arithmetic.
    """
    count = 0
    position = index
    while position >= 0 and verdicts[position] == value:
        count += 1
        position -= 1
    return count


def _last_all_true(verdicts: Sequence[bool], index: int, window: int) -> bool:
    """Whether the ``window`` verdicts ending at ``index`` are all true."""
    return index + 1 >= window and all(verdicts[index + 1 - window : index + 1])


def _last_all_false(verdicts: Sequence[bool], index: int, window: int) -> bool:
    """Whether the ``window`` verdicts ending at ``index`` are all false."""
    return index + 1 >= window and not any(verdicts[index + 1 - window : index + 1])


def _oracle_trace(
    outcomes: Sequence[Any],
    timestamps: Sequence[int],
    *,
    activate_after: int,
    clear_after: int,
    emit: str,
    repeat_interval_ms: int,
) -> List[Record]:
    """The reference automaton of Requirement 15.2 and 15.3.

    Decides each run by slicing the verdict sequence instead of carrying
    counters, and decides a ``while_active`` emit from the timestamps of
    the passes since the current activation.
    """
    verdicts = _verdicts(outcomes)
    records: List[Record] = []
    active = False
    active_since: Optional[int] = None
    last_emit: Optional[int] = None
    episode_passes: List[int] = []

    for index, verdict in enumerate(verdicts):
        now = timestamps[index]
        transition = TRANSITION_NONE

        if not active and _last_all_true(verdicts, index, activate_after):
            active = True
            active_since = now
            transition = TRANSITION_ACTIVATED
        elif active and _last_all_false(verdicts, index, clear_after):
            active = False
            active_since = None
            last_emit = None
            episode_passes = []
            transition = TRANSITION_CLEARED

        if emit == EMIT_ON_ACTIVATE:
            passed = transition == TRANSITION_ACTIVATED
        elif emit == EMIT_ON_CHANGE:
            passed = transition in (TRANSITION_ACTIVATED, TRANSITION_CLEARED)
        elif not active:
            passed = False
        elif repeat_interval_ms == 0 or not episode_passes:
            passed = True
        else:
            passed = (now - episode_passes[-1]) >= repeat_interval_ms

        if passed:
            last_emit = now
            episode_passes.append(now)

        records.append(
            (
                EventGateState(
                    active=active,
                    consecutive_true=_trailing_run(verdicts, index, True),
                    consecutive_false=_trailing_run(verdicts, index, False),
                    active_since_ms=active_since,
                    last_emit_ms=last_emit,
                ),
                passed,
                transition,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Driving the module
# ---------------------------------------------------------------------------


def _module_trace(
    outcomes: Sequence[Any],
    timestamps: Sequence[int],
    *,
    activate_after: int,
    clear_after: int,
    emit: str,
    repeat_interval_ms: int,
    state: Any = None,
) -> List[Record]:
    """Step one gate through a sequence, collecting every run's record."""
    carried = EventGateState() if state is None else state
    records: List[Record] = []
    for outcome, now in zip(outcomes, timestamps):
        carried, passed, transition = step_event_gate(
            carried,
            outcome,
            activate_after=activate_after,
            clear_after=clear_after,
            emit=emit,
            repeat_interval_ms=repeat_interval_ms,
            now_ms=now,
        )
        records.append((carried, passed, transition))
    return records


def _trace(case: Dict[str, Any], **overrides: Any) -> List[Record]:
    """The module's trace for a generated case."""
    parameters = dict(case)
    parameters.update(overrides)
    return _module_trace(
        parameters["outcomes"],
        parameters["timestamps"],
        activate_after=parameters["activate_after"],
        clear_after=parameters["clear_after"],
        emit=parameters["emit"],
        repeat_interval_ms=parameters["repeat_interval_ms"],
        state=parameters.get("state"),
    )


def _expected(case: Dict[str, Any], **overrides: Any) -> List[Record]:
    """The reference automaton's trace for a generated case."""
    parameters = dict(case)
    parameters.update(overrides)
    return _oracle_trace(
        parameters["outcomes"],
        parameters["timestamps"],
        activate_after=parameters["activate_after"],
        clear_after=parameters["clear_after"],
        emit=parameters["emit"],
        repeat_interval_ms=parameters["repeat_interval_ms"],
    )


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

#: The three condition outcomes of Requirement 15.2.
_OUTCOMES = (True, False, None)

#: Thresholds inside the catalog's declared 1 to 1000 range
#: (Requirement 15.1). Small values so a generated sequence actually
#: reaches them; 1000 is the maximum, which no sequence here reaches.
_THRESHOLDS = st.one_of(
    st.integers(min_value=1, max_value=6),
    st.sampled_from((1, 2, 3, 1000)),
)

#: ``repeat_interval_ms`` values inside the catalog's 0 to 86400000 range.
_INTERVALS = st.sampled_from((0, 0, 1, 100, 250, 1000, 86400000))

_MAX_RUNS = 26


@st.composite
def _outcome_sequences(draw: Any, min_size: int = 0) -> List[Any]:
    """Outcome sequences, as runs of one verdict and as free sequences.

    Runs dominate the corpus: a threshold of three is only reached by a
    sequence that repeats a verdict, and the interesting behaviour is at
    the run boundaries. A "false" run mixes ``False`` and the unevaluable
    ``None`` so that the two are never separated in practice.
    """
    style = draw(st.sampled_from(("runs", "runs", "free")))
    if style == "free":
        return draw(
            st.lists(
                st.sampled_from(_OUTCOMES), min_size=min_size, max_size=_MAX_RUNS
            )
        )
    sequence: List[Any] = []
    blocks = draw(
        st.lists(
            st.tuples(st.booleans(), st.integers(min_value=1, max_value=5)),
            min_size=1,
            max_size=8,
        )
    )
    for truthy, length in blocks:
        if truthy:
            sequence.extend([True] * length)
        else:
            sequence.extend(
                draw(
                    st.lists(
                        st.sampled_from((False, None)),
                        min_size=length,
                        max_size=length,
                    )
                )
            )
    if len(sequence) < min_size:
        sequence.extend([True] * (min_size - len(sequence)))
    return sequence[:_MAX_RUNS]


@st.composite
def _timestamps(draw: Any, count: int, *, monotone: bool = True) -> List[int]:
    """``now_ms`` per run: non-decreasing, or with backwards jumps."""
    start = draw(st.integers(min_value=0, max_value=10_000))
    step = (
        st.integers(min_value=0, max_value=400)
        if monotone
        else st.integers(min_value=-400, max_value=400)
    )
    stamps = [start]
    for _ in range(max(0, count - 1)):
        stamps.append(stamps[-1] + draw(step))
    return stamps[:count]


@st.composite
def _gate_cases(
    draw: Any,
    *,
    emit: Optional[Any] = None,
    interval: Optional[Any] = None,
    monotone: bool = True,
    min_size: int = 0,
) -> Dict[str, Any]:
    """A sequence of outcomes with timestamps and gate parameters."""
    outcomes = draw(_outcome_sequences(min_size=min_size))
    return {
        "outcomes": outcomes,
        "timestamps": draw(_timestamps(len(outcomes), monotone=monotone)),
        "activate_after": draw(_THRESHOLDS),
        "clear_after": draw(_THRESHOLDS),
        "emit": draw(st.sampled_from(EMIT_MODES) if emit is None else emit),
        "repeat_interval_ms": draw(_INTERVALS if interval is None else interval),
    }


def _previous_active(records: Sequence[Record], index: int) -> bool:
    """Whether the gate was active before run ``index``.

    Traces start from a fresh gate, which is inactive (Requirement 15.5).
    """
    return records[index - 1][0].active if index else False


# ---------------------------------------------------------------------------
# Clause: the whole automaton against the reference
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_matches_the_reference_automaton(case: Dict[str, Any]) -> None:
    """Every run's state, ``passed`` and ``transition`` match the oracle.

    The comparison is over the whole trace, so a divergence anywhere in
    the sequence — a transition on the wrong run, a counter that does not
    reset, a stale ``active_since`` — fails here.
    """
    assert _trace(case) == _expected(case), case


@settings(max_examples=100)
@given(_gate_cases(monotone=False))
def test_event_gate_matches_the_reference_automaton_when_the_clock_jumps_backwards(
    case: Dict[str, Any],
) -> None:
    """A non-monotonic clock is handled as the module documents.

    ``repeat_interval_ms`` is measured on ``now_ms`` as given, so a
    backwards jump can delay an emit rather than trigger one. The oracle
    computes the same difference, so this pins the documented choice.
    """
    assert _trace(case) == _expected(case), case


# ---------------------------------------------------------------------------
# Clause: the gate activates after exactly activate_after consecutive trues
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    st.integers(min_value=1, max_value=6),
    st.integers(min_value=0, max_value=7),
    st.lists(st.sampled_from((False, None)), max_size=4),
    st.sampled_from(EMIT_MODES),
    _THRESHOLDS,
)
def test_event_gate_activates_after_exactly_activate_after_trues(
    activate_after: int,
    trues: int,
    prefix: List[Any],
    emit: str,
    clear_after: int,
) -> None:
    """Requirement 15.2, at the exact run.

    A fresh gate fed anything false and then ``trues`` consecutive trues
    activates on the ``activate_after``-th true and on no other run, and
    does not activate at all when the run of trues is shorter.
    """
    outcomes = list(prefix) + [True] * trues
    timestamps = list(range(len(outcomes)))
    records = _module_trace(
        outcomes,
        timestamps,
        activate_after=activate_after,
        clear_after=clear_after,
        emit=emit,
        repeat_interval_ms=0,
    )
    activations = [
        index
        for index, record in enumerate(records)
        if record[2] == TRANSITION_ACTIVATED
    ]
    if trues >= activate_after:
        expected_index = len(prefix) + activate_after - 1
        assert activations == [expected_index], (outcomes, activations)
        assert records[expected_index][0].active_since_ms == expected_index
        assert all(not record[0].active for record in records[:expected_index])
        assert all(record[0].active for record in records[expected_index:])
    else:
        assert activations == [], (outcomes, activations)
        assert all(not record[0].active for record in records)


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_activates_only_on_a_run_of_activate_after_trues(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.2 and 15.4, as an invariant of the whole trace.

    Every activating run has the required run of trues behind it and was
    preceded by an inactive gate; the gate never becomes active on any
    other run; and ``active_since`` is the activating run's timestamp,
    unchanged for as long as the gate stays active.
    """
    records = _trace(case)
    verdicts = _verdicts(case["outcomes"])
    window = case["activate_after"]

    for index, (state, _passed, transition) in enumerate(records):
        was_active = _previous_active(records, index)
        if transition == TRANSITION_ACTIVATED:
            assert not was_active, (index, case)
            assert _last_all_true(verdicts, index, window), (index, case)
            assert state.active, (index, case)
            assert state.active_since_ms == case["timestamps"][index], (index, case)
        if state.active and not was_active:
            assert transition == TRANSITION_ACTIVATED, (index, case)
        if state.active and was_active:
            assert state.active_since_ms == records[index - 1][0].active_since_ms
        if not state.active:
            assert state.active_since_ms is None, (index, case)


# ---------------------------------------------------------------------------
# Clause: the gate clears after exactly clear_after consecutive falses
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    st.integers(min_value=1, max_value=6),
    st.integers(min_value=1, max_value=6),
    st.lists(st.sampled_from((False, None)), min_size=0, max_size=8),
    st.sampled_from(EMIT_MODES),
)
def test_event_gate_clears_after_exactly_clear_after_falses(
    activate_after: int,
    clear_after: int,
    tail: List[Any],
    emit: str,
) -> None:
    """Requirement 15.2, at the exact run, with unevaluable outcomes.

    The gate is first activated by a run of trues, then fed ``tail``,
    a mixture of ``False`` and the unevaluable ``None``: it clears on the
    ``clear_after``-th of them and on no other run.
    """
    outcomes: List[Any] = [True] * activate_after + list(tail)
    timestamps = [index * 10 for index in range(len(outcomes))]
    records = _module_trace(
        outcomes,
        timestamps,
        activate_after=activate_after,
        clear_after=clear_after,
        emit=emit,
        repeat_interval_ms=0,
    )
    clears = [
        index for index, record in enumerate(records) if record[2] == TRANSITION_CLEARED
    ]
    if len(tail) >= clear_after:
        expected_index = activate_after + clear_after - 1
        assert clears == [expected_index], (outcomes, clears)
        assert records[expected_index][0].active is False
        assert records[expected_index][0].active_since_ms is None
        assert records[expected_index][0].consecutive_false == clear_after
    else:
        assert clears == [], (outcomes, clears)
        assert records[-1][0].active is True


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_clears_only_on_a_run_of_clear_after_falses(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.2, as an invariant of the whole trace.

    Every clearing run has the required run of falses behind it and was
    preceded by an active gate; the gate never becomes inactive on any
    other run. A single true in the window resets it, which is what makes
    the run of falses "consecutive".
    """
    records = _trace(case)
    verdicts = _verdicts(case["outcomes"])
    window = case["clear_after"]

    for index, (state, _passed, transition) in enumerate(records):
        was_active = _previous_active(records, index)
        if transition == TRANSITION_CLEARED:
            assert was_active, (index, case)
            assert _last_all_false(verdicts, index, window), (index, case)
            assert not state.active, (index, case)
        if was_active and not state.active:
            assert transition == TRANSITION_CLEARED, (index, case)
        assert state.consecutive_true == _trailing_run(verdicts, index, True)
        assert state.consecutive_false == _trailing_run(verdicts, index, False)
        assert state.consecutive_true == 0 or state.consecutive_false == 0


# ---------------------------------------------------------------------------
# Clause: an unevaluable outcome counts as false
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_counts_an_unevaluable_outcome_as_false(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.2: ``None`` is indistinguishable from ``False``.

    Rewriting every unevaluable outcome in a sequence to ``False`` leaves
    the whole trace — states, passes and transitions — byte for byte the
    same. (The *recording* of an unevaluable condition on the node is the
    binding's job, not the gate's.)
    """
    rewritten = [False if outcome is None else outcome for outcome in case["outcomes"]]
    assert _trace(case) == _trace(case, outcomes=rewritten), case


@settings(max_examples=100)
@given(
    _gate_cases(),
    st.lists(
        st.sampled_from((1, 7, "x", [0], {"a": 1}, (0,))),
        min_size=_MAX_RUNS,
        max_size=_MAX_RUNS,
    ),
    st.lists(
        st.sampled_from((0, 0.0, "", [], {}, False)),
        min_size=_MAX_RUNS,
        max_size=_MAX_RUNS,
    ),
)
def test_event_gate_reads_a_non_boolean_outcome_as_its_truth_value(
    case: Dict[str, Any],
    truthy: List[Any],
    falsy: List[Any],
) -> None:
    """A condition evaluator may hand the gate any value but ``None``.

    Replacing each ``True`` with an arbitrary truthy value and each
    ``False`` with an arbitrary falsy one leaves the trace unchanged, so
    the gate reads a value's truth and nothing else.
    """
    rewritten = [
        truthy[index] if outcome is True else falsy[index] if outcome is False else None
        for index, outcome in enumerate(case["outcomes"])
    ]
    assert _trace(case) == _trace(case, outcomes=rewritten), case


# ---------------------------------------------------------------------------
# Clause: passed is true on exactly the runs the emit rule selects
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_gate_cases(emit=st.just(EMIT_ON_ACTIVATE)))
def test_event_gate_on_activate_passes_exactly_the_activating_runs(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.3, ``on_activate``.

    The interval is irrelevant in this mode, which the second trace pins:
    the passes do not move when the window changes.
    """
    records = _trace(case)
    for state, passed, transition in records:
        assert passed == (transition == TRANSITION_ACTIVATED), (state, transition)
        if passed:
            assert state.active, state
    assert [record[1] for record in _trace(case, repeat_interval_ms=86400000)] == [
        record[1] for record in records
    ]


@settings(max_examples=100)
@given(_gate_cases(emit=st.just(EMIT_ON_CHANGE)))
def test_event_gate_on_change_passes_exactly_the_activations_and_clears(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.3, ``on_change``."""
    for state, passed, transition in _trace(case):
        assert passed == (
            transition in (TRANSITION_ACTIVATED, TRANSITION_CLEARED)
        ), (state, transition)
        assert passed != (transition == TRANSITION_NONE), (state, transition)


@settings(max_examples=100)
@given(_gate_cases(emit=st.just(EMIT_WHILE_ACTIVE), interval=st.just(0)))
def test_event_gate_while_active_passes_exactly_the_active_runs(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.3, ``while_active`` without a repeat interval.

    Every run the gate is active on passes, including the activating run
    and including the falses before a clear; the clearing run itself does
    not, because the gate is no longer active on it.
    """
    for state, passed, transition in _trace(case):
        assert passed == state.active, (state, transition)
        assert not (passed and transition == TRANSITION_CLEARED)


@settings(max_examples=100)
@given(
    _gate_cases(
        emit=st.just(EMIT_WHILE_ACTIVE),
        interval=st.sampled_from((1, 100, 250, 1000)),
    )
)
def test_event_gate_while_active_passes_at_most_once_per_repeat_interval(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.3: ``while_active`` rate-limited to one pass per window.

    Read directly off the trace, with a non-decreasing clock: two passes
    within one activation are at least a window apart, the first run of
    every activation passes (a clear forgets the last pass, so a
    re-activation always emits), and an active run that does not pass is
    inside the window of the last one.
    """
    records = _trace(case)
    interval = case["repeat_interval_ms"]
    last_pass: Optional[int] = None

    for index, (state, passed, transition) in enumerate(records):
        now = case["timestamps"][index]
        if transition == TRANSITION_ACTIVATED:
            assert passed, (index, case)
        if transition == TRANSITION_CLEARED:
            assert not passed, (index, case)
            last_pass = None
        if passed:
            assert state.active, (index, case)
            if last_pass is not None:
                assert now - last_pass >= interval, (index, case)
            last_pass = now
        elif state.active:
            assert last_pass is not None, (index, case)
            assert now - last_pass < interval, (index, case)


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_only_an_active_or_clearing_run_can_pass(
    case: Dict[str, Any],
) -> None:
    """No emit mode ever lets a run through while the gate is idle.

    A pass means the gate is active on that run, or the run is the one
    that cleared it (``on_change``). A run with no transition and an
    inactive gate never passes, in any mode.
    """
    for state, passed, transition in _trace(case):
        if passed:
            assert state.active or transition == TRANSITION_CLEARED, (state, transition)
        if not state.active and transition == TRANSITION_NONE:
            assert not passed, (state, transition)


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_emit_mode_does_not_change_the_automaton(
    case: Dict[str, Any],
) -> None:
    """The emit rule selects runs; it never moves the automaton.

    Requirement 15.2 (the state machine) and Requirement 15.3 (what the
    gate lets through) are independent: across all three modes the active
    flag, both counters, ``active_since`` and the transitions are
    identical, and only ``passed`` — and with it the internal
    ``last_emit_ms`` — differs.
    """
    baseline = _trace(case, emit=EMIT_ON_ACTIVATE)

    def automaton(records: Sequence[Record]) -> List[Any]:
        return [
            (
                state.active,
                state.consecutive_true,
                state.consecutive_false,
                state.active_since_ms,
                transition,
            )
            for state, _passed, transition in records
        ]

    for mode in EMIT_MODES:
        assert automaton(_trace(case, emit=mode)) == automaton(baseline), (mode, case)


# ---------------------------------------------------------------------------
# The clock: the automaton depends on the verdicts alone
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_gate_cases(), st.integers(min_value=-5_000, max_value=5_000))
def test_event_gate_is_invariant_under_a_shift_of_the_clock(
    case: Dict[str, Any],
    delta: int,
) -> None:
    """Only differences of ``now_ms`` matter.

    Shifting every timestamp by the same amount shifts ``active_since``
    and ``last_emit_ms`` by it and leaves every pass and transition
    exactly where it was, which is what makes a ``repeat_interval_ms``
    window a duration rather than a wall-clock schedule.
    """
    shifted = [stamp + delta for stamp in case["timestamps"]]
    original = _trace(case)
    moved = _trace(case, timestamps=shifted)

    assert [record[1:] for record in moved] == [record[1:] for record in original]
    for before, after in zip(original, moved):
        assert after[0].active == before[0].active
        assert after[0].consecutive_true == before[0].consecutive_true
        assert after[0].consecutive_false == before[0].consecutive_false
        for field in ("active_since_ms", "last_emit_ms"):
            was = getattr(before[0], field)
            now = getattr(after[0], field)
            assert now == (None if was is None else was + delta), (field, case)


@settings(max_examples=100)
@given(
    _gate_cases(emit=st.sampled_from((EMIT_ON_ACTIVATE, EMIT_ON_CHANGE))),
    st.integers(min_value=0, max_value=50),
)
def test_event_gate_transitions_do_not_depend_on_the_timestamps(
    case: Dict[str, Any],
    stride: int,
) -> None:
    """Requirement 15.2 is a function of the outcome sequence.

    Re-running the same outcomes on a completely different clock leaves
    the active flag, the counters and the transitions untouched — and, in
    the two modes that do not consult the clock, the passes too.
    """
    other = [index * stride for index in range(len(case["outcomes"]))]
    original = _trace(case)
    rescheduled = _trace(case, timestamps=other)
    assert [
        (state.active, state.consecutive_true, state.consecutive_false, passed, transition)
        for state, passed, transition in rescheduled
    ] == [
        (state.active, state.consecutive_true, state.consecutive_false, passed, transition)
        for state, passed, transition in original
    ], case


# ---------------------------------------------------------------------------
# State handling: carried state, restarts and determinism
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(_gate_cases(min_size=1), st.integers(min_value=0, max_value=25))
def test_event_gate_resumes_from_the_carried_state(
    case: Dict[str, Any],
    split: int,
) -> None:
    """The gate is a pure step function of its state.

    Stepping a sequence in one pass and stepping it in two, resuming from
    the state the first half returned, give the same trace. This is what
    lets the LocalServer keep Event_Gate_State in a store keyed by
    registration and node (Requirement 15.2) — and it exercises every
    state the module can actually produce as a starting state.
    """
    boundary = min(split, len(case["outcomes"]))
    whole = _trace(case)
    head = _trace(
        case,
        outcomes=case["outcomes"][:boundary],
        timestamps=case["timestamps"][:boundary],
    )
    carried = head[-1][0] if head else EventGateState()
    tail = _trace(
        case,
        outcomes=case["outcomes"][boundary:],
        timestamps=case["timestamps"][boundary:],
        state=carried,
    )
    assert head + tail == whole, (boundary, case)

    # The input state is never mutated: it is a frozen value.
    assert carried == (head[-1][0] if head else EventGateState())


@settings(max_examples=100)
@given(_gate_cases(min_size=1))
def test_event_gate_starts_inactive_after_a_restart(case: Dict[str, Any]) -> None:
    """Requirement 15.5: a fresh gate is inactive and remembers nothing.

    A default :class:`EventGateState` is the state a new registration and
    a restarted backend begin from, and a first run can only pass by
    activating — which needs ``activate_after`` to be 1.
    """
    assert EventGateState() == EventGateState(
        active=False,
        consecutive_true=0,
        consecutive_false=0,
        active_since_ms=None,
        last_emit_ms=None,
    )
    records = _trace(case)
    first_state, first_passed, first_transition = records[0]
    assert first_transition != TRANSITION_CLEARED, case
    if first_transition == TRANSITION_ACTIVATED:
        assert case["activate_after"] == 1 and _verdicts(case["outcomes"])[0]
    if first_passed:
        assert first_transition == TRANSITION_ACTIVATED, records[0]
        assert first_state.active, records[0]


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_is_deterministic_for_identical_input(
    case: Dict[str, Any],
) -> None:
    """Identical input gives an identical trace and identical metadata.

    The device and the sandbox run this same function, and the metadata
    they merge has to be the same document (Requirement 15.6), so the
    comparison is on the serialized metadata as well as on the states.
    """
    first = _trace(case)
    second = _trace(case)
    assert first == second, case
    assert json.dumps(
        [event_gate_metadata(state, transition) for state, _p, transition in first]
    ) == json.dumps(
        [event_gate_metadata(state, transition) for state, _p, transition in second]
    )


# ---------------------------------------------------------------------------
# Requirement 15.4: the merged run metadata
# ---------------------------------------------------------------------------

_METADATA_KEYS = (
    "state",
    "transition",
    "active_since",
    "consecutive_true",
    "consecutive_false",
)


@settings(max_examples=100)
@given(_gate_cases())
def test_event_gate_metadata_reports_the_state_of_every_run(
    case: Dict[str, Any],
) -> None:
    """Requirement 15.4: ``event.<nodeId>`` carries exactly these keys.

    Values are plain JSON — a string state, a string transition, an
    integer or ``None`` ``active_since``, and the two integer counts — so
    the document is identical on the device and in the sandbox.
    """
    for state, _passed, transition in _trace(case):
        metadata = event_gate_metadata(state, transition)
        assert tuple(metadata) == _METADATA_KEYS, metadata
        assert metadata["state"] == (
            GATE_STATE_ACTIVE if state.active else GATE_STATE_INACTIVE
        )
        assert metadata["transition"] in (
            TRANSITION_ACTIVATED,
            TRANSITION_CLEARED,
            TRANSITION_NONE,
        )
        assert metadata["transition"] == transition
        assert metadata["active_since"] is None or isinstance(
            metadata["active_since"], int
        )
        assert (metadata["active_since"] is None) == (not state.active)
        assert isinstance(metadata["consecutive_true"], int)
        assert isinstance(metadata["consecutive_false"], int)
        assert json.loads(json.dumps(metadata)) == metadata


# ---------------------------------------------------------------------------
# Totality and the documented parameter fallbacks
# ---------------------------------------------------------------------------


def _junk() -> st.SearchStrategy[Any]:
    """Whatever a caller may actually hand a parameter."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-4, max_value=4),
        st.floats(allow_nan=True, allow_infinity=True, width=32),
        st.text(max_size=6),
        st.lists(st.integers(min_value=-2, max_value=2), max_size=3),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
    )


@settings(max_examples=100)
@given(
    st.lists(st.one_of(st.sampled_from(_OUTCOMES), _junk()), max_size=6),
    st.lists(st.one_of(st.integers(min_value=-10, max_value=10), _junk()), max_size=6),
    _junk(),
    _junk(),
    _junk(),
    _junk(),
    st.one_of(st.none(), _junk()),
)
def test_event_gate_is_total_for_malformed_input(
    outcomes: List[Any],
    stamps: List[Any],
    activate_after: Any,
    clear_after: Any,
    emit: Any,
    repeat_interval_ms: Any,
    state: Any,
) -> None:
    """Nothing raises, whatever the run and the node's parameters hold.

    The gate runs inside a workflow run that must not fail on a
    configuration mistake, so every argument — including ``now_ms``,
    which a caller can leave unset or hand a string — degrades instead of
    raising, and the state it returns stays well typed so that
    Requirement 15.4's metadata is always a JSON document.
    """
    carried: Any = state
    for index in range(len(outcomes)):
        now = stamps[index] if index < len(stamps) else None
        carried, passed, transition = step_event_gate(
            carried,
            outcomes[index],
            activate_after=activate_after,
            clear_after=clear_after,
            emit=emit,
            repeat_interval_ms=repeat_interval_ms,
            now_ms=now,
        )
        assert isinstance(carried, EventGateState)
        assert isinstance(passed, bool)
        assert transition in (
            TRANSITION_ACTIVATED,
            TRANSITION_CLEARED,
            TRANSITION_NONE,
        )
        assert isinstance(carried.active, bool)
        assert isinstance(carried.consecutive_true, int)
        assert isinstance(carried.consecutive_false, int)
        for field in ("active_since_ms", "last_emit_ms"):
            value = getattr(carried, field)
            assert value is None or isinstance(value, int), (field, value)
        metadata = event_gate_metadata(carried, transition)
        assert json.loads(json.dumps(metadata)) == metadata


#: A malformed threshold and the value the module documents it falls back
#: to: a whole number below 1 is read as 1, and anything that is not a
#: number at all as the catalog default.
_MALFORMED_THRESHOLDS = (
    (0, 1),
    (-7, 1),
    (1.9, 1),
    ("2", 2),
    ("  3  ", 3),
    ("x", None),
    (None, None),
    (True, None),
    ([], None),
)


@settings(max_examples=100)
@given(
    st.sampled_from(_MALFORMED_THRESHOLDS),
    st.integers(min_value=1, max_value=5),
    st.sampled_from(EMIT_MODES),
)
def test_event_gate_reads_a_malformed_threshold_as_the_documented_fallback(
    threshold: Tuple[Any, Optional[int]],
    trues: int,
    emit: str,
) -> None:
    """A malformed ``activate_after`` degrades instead of breaking the gate.

    The effective threshold is the documented one — a value below 1 is
    read as 1, a non-number as the catalog default of 3 — and the gate
    then activates after exactly that many trues.
    """
    value, expected = threshold
    effective = DEFAULT_ACTIVATE_AFTER if expected is None else expected
    records = _module_trace(
        [True] * trues,
        list(range(trues)),
        activate_after=value,
        clear_after=DEFAULT_CLEAR_AFTER,
        emit=emit,
        repeat_interval_ms=0,
    )
    activations = [
        index
        for index, record in enumerate(records)
        if record[2] == TRANSITION_ACTIVATED
    ]
    assert activations == ([effective - 1] if trues >= effective else []), (
        value,
        trues,
        activations,
    )


@settings(max_examples=100)
@given(
    st.sampled_from(_MALFORMED_THRESHOLDS),
    st.integers(min_value=1, max_value=5),
)
def test_event_gate_reads_a_malformed_clear_threshold_as_the_documented_fallback(
    threshold: Tuple[Any, Optional[int]],
    falses: int,
) -> None:
    """The same fallback for ``clear_after``, from an active gate."""
    value, expected = threshold
    effective = DEFAULT_CLEAR_AFTER if expected is None else expected
    outcomes: List[Any] = [True] + [None if index % 2 else False for index in range(falses)]
    records = _module_trace(
        outcomes,
        list(range(len(outcomes))),
        activate_after=1,
        clear_after=value,
        emit=DEFAULT_EMIT,
        repeat_interval_ms=0,
    )
    clears = [
        index for index, record in enumerate(records) if record[2] == TRANSITION_CLEARED
    ]
    assert clears == ([effective] if falses >= effective else []), (
        value,
        falses,
        clears,
    )


@settings(max_examples=100)
@given(_gate_cases(), st.sampled_from(("nonsense", "", "ON_ACTIVATE", None, 3)))
def test_event_gate_reads_an_unknown_emit_mode_as_the_default(
    case: Dict[str, Any],
    emit: Any,
) -> None:
    """An unknown ``emit`` falls back to the catalog default.

    An operator cannot reach this through the descriptor's enum, but a
    hand-edited document can, and a run must not fail because of it.
    """
    assume(emit not in EMIT_MODES)
    assert _trace(case, emit=emit) == _trace(case, emit=DEFAULT_EMIT), case
