# Trigger Dispatch Completion Wake-up Bugfix Design

## Overview

The trigger dispatcher runs a workflow's activations one at a time. It keeps the trigger node in
flight for as long as the run starter runs. The production run starter, `default_run_starter`,
hands the run to the executor hook and then re-reads the execution row every 0.5 s until the run
settles. A run of about 250 ms therefore keeps its node in flight for about 0.5 s, the `drop` policy
discards every firing in the extra window, and the run rate is tied to the poll instead of the run.

The fix makes the end of the run observable:
- `executor.dispatch` gains an optional, keyword-only `on_complete` callback. The hook calls it
  exactly once, on the run's thread, after the executor callback has returned or raised.
- `default_run_starter` creates one `threading.Event` per run, passes `on_complete=event.set`, and
  waits on the event instead of sleeping.
- The execution row stays the release condition. The starter still returns only after a read shows
  the row has left `pending`/`running` (or no longer exists). The existing 0.5 s re-read remains as a
  fallback, so the worst case is today's behavior.

Nothing else changes: the policy gates, the queue, the dispatcher loop, the manual-run path, the
continuous runners and the transports.

**Technology stack (locked).**
- Python 3 standard library only (`threading.Event`); no new dependency.
- The existing SQLAlchemy session factory.
- pytest with Hypothesis. The workflow_engine conftest loads the `engine-fast` profile (25 examples,
  Hypothesis's default 200 ms deadline), or `ci` (100 examples) with `HYPOTHESIS_PROFILE=ci`
  (`test/backend-test/workflow_engine/conftest.py:33–35`). Every property that runs in real time
  therefore sets `@settings(max_examples=10, deadline=None)` itself.

The change is device-side only, in `src/backend/workflow_engine/executor.py` and
`src/backend/workflow_engine/trigger_runtime.py`. No Portal, frontend, catalog, recipe, Dockerfile or
requirements change.

## Glossary

- **Run starter.** The synchronous `run_starter(activation)` the dispatcher calls
  (`trigger_runtime.py:471`). The production one is `default_run_starter` (`:507–562`).
- **In-flight window.** From `run_activation` setting `_in_flight_node` (`:469`) to the starter's
  return (`:481–483`). `drop` discards firings inside it.
- **Settled row.** The execution row's status is neither `pending` nor `running`, or the row no longer
  exists. This is the condition `_wait_for_terminal_status` returns on today (`:579–582`).
- **Completion signal.** The new per-run `threading.Event`. The executor hook sets it when the run's
  thread ends.
- **Release lag.** Time from the end of the run's executor thread to the starter's return.
- **ε.** 50 ms, the design target for the release lag on the device (bugfix.md 2.1–2.3). V2's drop
  attribution at 1.8 Hz checks it trigger by trigger. V4 bounds the lag only to the next trigger
  boundary.
- **Fallback interval.** `DEFAULT_RUN_POLL_INTERVAL_SECONDS` (0.5 s), the longest time between two row
  reads while the starter waits.
- **Cycle (c).** Time from a trigger's arrival on the device until the dispatcher is free again. The
  device cannot observe it directly.
- **Measured cycle (c_k).** For run k, last_k − receive_k: from the trigger's arrival (the
  Trigger_Context `timestamp`) to the run's last `run.log` record. On F' it is within a few tens of ms
  of c. On F it is not: the dispatcher is free about 0.25 s after the last record (on the PPE demo,
  c ≈ 0.53 s and c_k ≈ 0.28 s, estimated).
- **Idle time (idle_k).** receive_{k+1} − last_k, from a run's last log record to the next run's
  trigger.
- **Predicted idle (e_k).** ⌈(c_k + ε)·f⌉/f − c_k at trigger rate f: the idle time when the dispatcher
  is free ε after the run's last log record and the next `drop` trigger after that is accepted.
- **Publish time (ts_n).** The host's `time.time()` when trigger n was published. The message
  carries it. For a dropped trigger, it comes from the publisher's log or from the schedule (step 3).
- **Dispatcher drop.** A dropped 1.8 Hz trigger that arrived more than ε after the last log record
  of the run in flight, by the step-5 bound, or that had no earlier run in its window. 2.2 forbids
  such a discard. A discard caused by a receive-latency spike or a long run is not one. A trigger
  lost in transit cannot be told apart and counts as one (the 1.8 Hz rule).
- **F / F'.** The code before and after the fix.

## Bug Details

### Bug Condition

```
FUNCTION isBugCondition(X)
  INPUT:  X = one activation dispatched by default_run_starter with an executor registered;
              t_end = when its executor thread ends (the row is settled by then);
              p = the fallback interval
  OUTPUT: boolean

  RETURN releaseLag(F, X) > ε
    -- F reads the row only at about dispatch + k·p, so it returns at the first read at or after
    -- the terminal commit. That commit precedes the end of the thread by the run's tail (D3), so
    -- releaseLag(F, X) ∈ [−tail, p − tail): at most p, and slightly negative when a read lands
    -- inside the tail. On the PPE demo (runs end roughly 0.26–0.30 s after dispatch, estimated;
    -- p = 0.5 s) it is about 0.2–0.3 s on every run.
    -- ε = 50 ms on the device; the tests use bounds far below their injected fallback interval.
END FUNCTION
```

### Examples

- **PPE demo at 5 Hz, today.** A trigger arrives at 0 s. The run ends at roughly 0.26–0.30 s
  (estimated). The row is read right after dispatch (pending), the starter sleeps 0.5 s, and the next
  read, at about 0.51–0.56 s, sees `completed`. The triggers at 0.2 s and 0.4 s are discarded and the
  one at 0.6 s runs: gaps of 0.6 s, 1.649 runs/s, 234 of 350 triggers discarded.
- **PPE demo at 1.8 Hz, today.** The release at about 0.51–0.56 s straddles the 0.556 s trigger
  period. A trigger that arrives early (receive-latency jitter) lands before the release and is
  discarded: 12 of 126 triggers, leaving 1.1 s holes.
- **`queue` backlog (from the code).** N pending activations of 250 ms runs drain at one per about
  0.5 s.
- **Not the bug.** These are unchanged by the fix:
  - no executor registered (the starter returns at once);
  - a manual run (no dispatcher involved);
  - a continuous run (the runner calls the executor synchronously);
  - a run whose row settles exactly at a re-read.

### Rate model, checked against today's numbers

With `drop`, trigger period T = 1/f and cycle c, the next accepted trigger is the first one that
arrives after the dispatcher is free. Ignoring receive jitter, the inter-run gap is ⌈c/T⌉·T and the
run rate is f/⌈c·f⌉. Today the dispatcher is free at c ≈ 0.53 s, set by the 0.5 s re-read
(c_k ≈ 0.28 s, estimated). The model reproduces the rows where c is clear of a multiple of T. It
only brackets the rows where c is within receive jitter of T, because there a jittered trigger can
land on either side of the release:

| Trigger rate | T | ⌈c·f⌉ at c = 0.53 s | Predicted runs/s | Measured runs/s |
|---|---|---|---|---|
| 1.67 Hz | 0.600 s | 1, but c is within jitter of T: brackets | 0.83–1.67 | 1.553 |
| 1.8 Hz | 0.556 s | 1, but c is within jitter of T: brackets | 0.90–1.80 | 1.628 |
| 1.9 Hz | 0.526 s | 1 or 2, c straddles T: brackets | 0.95–1.90 | 1.440 |
| 2 Hz | 0.500 s | 2 | 1.00 | 1.001 |
| 3 Hz | 0.333 s | 2 | 1.50 | 1.472 |
| 4 Hz | 0.250 s | 3 | 1.33 | 1.345 |
| 5 Hz | 0.200 s | 3 | 1.67 | 1.649 |

After the fix the cycle consists of these parts:
- the row insert;
- pre-run preparation;
- the run (p50 250–258 ms measured);
- post-terminal persistence;
- one re-read.

Only the run is measured today, so c ≈ 0.27–0.35 s is an estimate. The predictions are:
- **1.8 Hz:** ⌈c·f⌉ = 1, so about 1.8 runs/s. The slack against receive-latency spikes grows from
  about 0–0.05 s today to about 0.2–0.29 s.
- **5 Hz:** ⌈c·f⌉ = 2 while c < 0.4 s, so about 2.5 runs/s with gaps of 0.4 s.
- **Back-to-back ceiling** (a `queue` backlog, or T ≪ c): 1/c. That is about 3.5–4 runs/s if the
  cycle were the measured run alone (250–284 ms, p50 to p95; the investigation's estimate), and about
  2.9 runs/s at c = 0.35 s.

## Expected Behavior

```
// Property: Fix Checking
FOR ALL X WHERE isBugCondition(X) DO
  result ← run default_run_starter'(X)
  ASSERT releaseLag(F', X) ≤ ε
  ASSERT the row is settled when the starter returns
END FOR

// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  -- no executor, manual runs, continuous runs, gate decisions, ordering, idle dispatchers,
  -- stops during a run, rows that settle only after the executor thread ended
  ASSERT F(X) = F'(X)   -- except the single WARNING of D5 for a row still active after the signal
END FOR
```

## Root Cause

Confirmed by reading the code and by the measured gaps:
1. The starter's return is the only thing that ends the in-flight window (`trigger_runtime.py:481–483`).
2. The starter observes the end of the run only by polling the row (`:565–585`), every 0.5 s (`:106`).
3. The executor hook returns as soon as it has started the run thread and offers no completion
   notification (`executor.py:57–82`).
4. The trigger-activation-runtime design named both options ("polls the execution row / completion
   callback"). The implementation took the poll, and nothing measured its cost until the PPE demo.

## Design Decisions

### D1. Wake on a completion signal from the executor hook (chosen)

`executor.dispatch(execution_id, *, on_complete=None)` calls `on_complete()` once, in the `finally`
of the run thread's body, after the executor callback has returned or raised.
`default_run_starter` passes `on_complete=completed.set` for a fresh `threading.Event` and waits on
it.

Why the hook:
- Every triggered run passes through it (`trigger_runtime.py:555`).
- It already owns the run thread and its exception containment.
- Its `finally` covers every way `WorkflowExecutor.execute` (`pipeline_executor.py:1525`) can end:
  completed, the `_finish_failed` paths, `_mark_failed_best_effort`, the early returns for a missing
  or non-pending row, and an unexpected exception.

Why `threading.Event`:
- It is level-triggered, so a run that ends before the starter starts waiting is not missed.
- With one event per run, nothing is shared between runs or workflows and nothing needs cleanup.

### D2. The row stays the release condition

The starter returns only when a read shows a settled row. The signal ends the wait early; it never
releases on its own. This keeps the meaning of Requirement 7.6 (the run has left pending/running) and
the source of truth that the API status and the run list use.

### D3. Signal at the end of the thread, not at the terminal write

`execute` commits `completed` at `pipeline_executor.py:2358–2360` and then still runs a tail:
- it persists node status, node frames and run metadata (`:2361–2370`);
- it emits the timing logs, stops the run-log capture and removes the work dir (`:2383–2393`).

Run-log capture adds a handler to the whole `workflow_engine` logger (`run_log.py:56`, `:128`), not
to one thread. A next run that started inside that tail would write its first lines into the
previous run's `run.log`. The integration helper `join_execution_thread` also joins the run thread
(`test_workflow_engine_integration.py:119–132`). Its docstring predates the current ordering (output
bindings now run before the terminal status, `pipeline_executor.py:2292`); the tail at `:2361–2393`
is the reason that remains.

With the signal at the end of the thread, a dispatched run never overlaps the previous run's tail;
P8 pins this on the host. Today's poll can occasionally release inside the tail. The fallback still
can, but only when the terminal commit lands just before a fallback re-read and the tail outlasts
it, so the fix is never worse than today. V7 on the device allows for exactly this case.

### D4. The fallback interval stays 0.5 s

The fallback bounds the wait when the signal is late or missing. With the signal, a typical run costs
two row reads, one right after dispatch and one after the signal, the same as today. A shorter
interval would add reads, with no benefit on the normal path, against the SQLite database that the
executor writes to during the run. That database uses the default rollback journal
(`dao/sqlite_db/sqlite_db_operations.py:38–40`).

### D5. The signal has arrived, but the row is still active

The executor thread can end while the row still reads `pending`/`running`, for example when
`_mark_failed_best_effort` could not write. In that case the starter:
- logs one WARNING naming the execution id and the status;
- continues today's fixed-interval re-reads until the row settles.

It does not release early (D2). It does not spin: once the signal is set it sleeps one fallback
interval between reads. Today the same situation polls silently forever. Changing that is a
follow-up (bugfix.md, Out of Scope).

The starter samples the signal before each read, not after it. The hook sets the signal only after
every executor write has committed. So a read that started after the signal and still shows an
active row is the genuine case. If the order were reversed, a run that finished between the read and
the check (for example, right after a fallback timeout) would log a false WARNING and pay an extra
0.5 s.

### Alternatives considered

- **Poll every 20–50 ms.** The investigation named this as the fallback option. It is a one-line
  change, but:
  - it keeps a release lag of up to one interval (10–25 ms on average, plus jitter);
  - it multiplies row reads per run by 5–12 against the shared SQLite file;
  - it still quantizes the cycle.

  Rejected, because the signal is practical with a single call site. The implementer does not switch
  approaches; if the signal turned out impractical, the design would be revised first.
- **Signal at each terminal write inside `WorkflowExecutor.execute`.** It needs a notification on every
  terminal path, misses the early returns, and releases inside the post-terminal tail (D3). Rejected.
- **Run the executor synchronously on the dispatcher thread**, as continuous runners do. The wait would
  be simplest, but triggered runs would leave the `executor.dispatch` path that Requirement 7.6 pins
  ("exactly as the manual trigger endpoint does"). They would also lose the `workflow-execution-<id>`
  thread that tests rely on. Rejected.
- **One module-level condition variable notified on every completion.** It wakes every waiting
  dispatcher of every workflow and needs a registry of finished ids to avoid lost wake-ups. Rejected in
  favor of one Event per run.
- **Find the run thread by name and `join` it.** This couples the starter to a naming convention and
  races with a thread that has already ended. Rejected.
- **Return a handle from `dispatch`.** This breaks the bool contract that callers and tests assert
  (`test_workflow_engine_api.py:208`, `:221`). Rejected.

## Fix Implementation

### `src/backend/workflow_engine/executor.py`

Extend the `ExecutorCallback` comment (`:36–39`) to state the contract the signal relies on: the call
is synchronous and returns once the run has reached a terminal state. Then change `dispatch`:

```python
def dispatch(
    execution_id: str,
    *,
    on_complete: Optional[Callable[[], None]] = None,
) -> bool:
    """Hand a pending execution to the registered executor, if any.

    Returns True when an executor picked the run up. The executor is invoked on
    its own daemon thread; exceptions are contained and logged (Requirement 13.7).

    ``on_complete``, when given, is called exactly once on that thread after the
    executor returned or raised, so a caller can wait for the run without polling
    (trigger-dispatch-completion-wakeup). It is never called when no executor is
    registered. An exception it raises is contained and logged.
    """
    if on_complete is not None and not callable(on_complete):
        raise TypeError("on_complete must be callable or None")
    executor = get_executor()
    if executor is None:
        logger.info(
            "No workflow executor registered; execution %s stays pending",
            execution_id,
        )
        return False

    def _run() -> None:
        try:
            executor(execution_id)
        except Exception:  # noqa: BLE001 - workflow failures must be contained
            logger.exception("Workflow execution %s raised", execution_id)
        finally:
            if on_complete is not None:
                try:
                    on_complete()
                except Exception:  # noqa: BLE001 - never affects the run
                    logger.exception(
                        "Completion callback for workflow execution %s raised",
                        execution_id,
                    )

    thread = threading.Thread(
        target=_run, name=f"workflow-execution-{execution_id}", daemon=True
    )
    thread.start()
    return True
```

### `src/backend/workflow_engine/trigger_runtime.py`

Constant comment (`:104–106`). The value is unchanged:

```python
#: Fallback re-read interval while the production run_starter waits for a run.
#: The wait normally ends on the executor hook's completion signal
#: (trigger-dispatch-completion-wakeup); this interval bounds it when the signal
#: is late, missing, or arrives before the row has settled.
DEFAULT_RUN_POLL_INTERVAL_SECONDS = 0.5
```

In `default_run_starter`, the row insert is unchanged; only the dispatch and the wait change. Keep
the existing annotated signatures, which the snippets below show in full. The module already imports
`threading`, `time`, `Callable` and `Optional`, and the change adds no import:

```python
def default_run_starter(
    registration_id: str,
    session_factory: Optional[Callable] = None,
    poll_interval: float = DEFAULT_RUN_POLL_INTERVAL_SECONDS,
) -> Callable[[RunActivation], None]:
    """..."""  # the existing docstring, updated as described below
    # A non-positive, NaN or too large interval would make the wait spin or
    # overflow Event.wait (any timeout above threading.TIMEOUT_MAX raises
    # OverflowError); fail at construction (group start), not mid-run.
    if not 0 < poll_interval <= threading.TIMEOUT_MAX:
        raise ValueError(
            "poll_interval must be a positive number of seconds no greater "
            "than threading.TIMEOUT_MAX, got {0!r}".format(poll_interval)
        )

    def run_starter(activation: RunActivation) -> None:
        ...  # unchanged: the deferred imports and the pending-row insert
        # One Event per run, set by the executor hook when the run's thread
        # ends. Level-triggered, so a run that ends before the wait starts is
        # not missed.
        completed = threading.Event()
        if not executor.dispatch(execution_id, on_complete=completed.set):
            return  # no executor: the row stays pending, as today
        _wait_for_terminal_status(
            factory, execution_id, poll_interval, completed=completed
        )

    return run_starter
```

`_wait_for_terminal_status` gains two optional parameters, both with defaults that keep today's
behavior:

```python
def _wait_for_terminal_status(
    session_factory: Callable,
    execution_id: str,
    poll_interval: float,
    completed: Optional[threading.Event] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """..."""  # the existing docstring, updated as described below
    from workflow_engine.models import WorkflowExecution

    warned = False
    while True:
        # Sample the signal BEFORE reading the row. The hook sets it only after
        # every executor write has committed, so "signalled before this read" and
        # "the read still shows pending/running" together mean the executor really
        # ended without settling the row. Sampling after the read would misreport
        # a run that settled between the read and the check.
        signalled = completed is not None and completed.is_set()
        session = session_factory()
        try:
            execution = session.get(WorkflowExecution, execution_id)
            status = None if execution is None else execution.status
        finally:
            session.close()
        if status not in ACTIVE_EXECUTION_STATUSES:
            return  # settled, or the row vanished (None)
        if completed is not None and not signalled:
            # Normal path: wake when the run's thread ends (at once if that
            # already happened), or after one fallback interval; then re-read.
            completed.wait(poll_interval)
            continue
        if completed is not None and not warned:
            warned = True
            logger.warning(
                "Workflow execution %s: its executor finished but the execution "
                "row still reads '%s'; keeping the trigger in flight and "
                "re-reading every %.1f s until it settles",
                execution_id, status, poll_interval,
            )
        sleep(poll_interval)
```

Also update the docstrings that describe the wait:
- the module docstring's run-starter paragraph (`:46–55`);
- `default_run_starter` (`:512–525`);
- `_wait_for_terminal_status` (`:568–572`).

Each should say that the wait ends on the completion signal, with the 0.5 s re-read as a fallback.

### Not changed

- `TriggerGate`, `ActivationQueue`.
- `ActivationDispatcher`: `_loop`, `run_activation`, `is_in_flight` and `stop`.
- `WorkflowActivationCore`, `WorkflowTriggerGroup`, `TriggerSubscriptionManager`, `ReconnectEngine`.
- Every transport.
- `pipeline_executor.py`, `api.py`, `continuous_runner.py`.
- The catalog and the Portal.
- `DEFAULT_DISPATCH_POLL_TIMEOUT_SECONDS` (0.25 s, the idle loop).

### Invariants and where they are enforced

| Invariant | Enforced by | Why there |
|---|---|---|
| At most one in-flight run per workflow (Requirement 7.6) | `ActivationDispatcher` (one thread, synchronous `run_activation`) together with `_wait_for_terminal_status`, which returns only on a settled row | The owner is unchanged. The row is the source of truth shared with the API and the UI. |
| The completion signal fires exactly once, after the executor callback returned or raised | `executor.dispatch`: the `finally` of `_run` | This is the only wrapper around every triggered run's thread, including unexpected exceptions. |
| The wait never spins | `_wait_for_terminal_status`: every iteration that does not return blocks once for the fallback interval (on the Event until it is set, then with `sleep`) | The loop is the only place that waits. The construction-time check rules out a zero, NaN or too large interval. |
| A stop never aborts a run | `ActivationDispatcher.stop` sets the stop event and joins; the wait does not watch the stop event | Unchanged by the fix. P7 pins it. |
| Policy semantics and dispatch order | `TriggerGate`, `ActivationQueue` | Untouched by the fix. |

### Error handling

| Operation | Failure | Recoverable? | What the caller receives | Logged |
|---|---|---|---|---|
| Insert the execution row | Database error | Yes, at the next activation | The exception leaves the run starter; `run_activation` contains it | ERROR with traceback (existing) |
| `executor.dispatch` | No executor registered | Yes | `False`; the starter returns and the row stays `pending` | INFO (existing) |
| `executor.dispatch` | `on_complete` is not callable | No, a programming error | `TypeError` before any thread starts; `run_activation` contains it; the row the starter inserted stays `pending` | ERROR with traceback (existing `run_activation` line) |
| `executor.dispatch` | The thread cannot start | Yes | `RuntimeError` from `thread.start()`; `run_activation` contains it; the row stays `pending` | ERROR with traceback (existing) |
| Executor callback | Raises | Yes | Contained in `_run`; the completion is still signalled | ERROR with traceback, "Workflow execution %s raised" (existing) |
| `on_complete` | Raises | Yes | Contained in `_run`; the starter falls back to the 0.5 s re-read | ERROR with traceback, "Completion callback for workflow execution %s raised" (new) |
| Row read during the wait | Database error | Yes | The exception propagates; `run_activation` contains it and frees the node (unchanged, recorded as a follow-up) | ERROR with traceback (existing) |
| Signal set, row still active | The executor ended without settling the row | Yes, once the row settles | The starter keeps waiting and re-reads every 0.5 s | WARNING once per execution (new) |
| The executor never returns | A hung run | As today | The starter re-reads every 0.5 s until the row settles | Nothing new |
| `default_run_starter` construction | `poll_interval` is ≤ 0, NaN or above `threading.TIMEOUT_MAX` (`ValueError`), or not a number (`TypeError` from the comparison) | No, a programming error | The exception when the group starts; `on_registrations_changed` contains it (`trigger_runtime.py:1342–1348`) and that group does not start | ERROR with traceback, "Trigger group for registration %s failed to start" (existing) |

### Inputs and validation

No external input changes: trigger messages, the Trigger_Context and the policy parameters keep their
catalog ranges and handling. The new parameters are internal:
- **`on_complete`** (`dispatch`): optional and keyword-only; `None` or a callable. A non-callable
  raises `TypeError` before any thread starts. A callable that cannot be called with no arguments
  fails when called, and `_run` contains and logs it like any other callback error.
- **`poll_interval`** (`default_run_starter`): seconds as a number, with
  `0 < poll_interval <= threading.TIMEOUT_MAX` (about 9.2e9 s; CPython's `Event.wait` raises
  `OverflowError` for any timeout above it, not only for infinity). A number outside that range
  (including NaN and infinity) raises `ValueError` at construction. A non-numeric value,
  such as `None` or `"0.5"`, raises `TypeError` from the comparison `0 < poll_interval`; no separate
  type check is added. The production value is always the 0.5 s default, because
  `_default_run_starter_factory` (`:1381–1386`) passes none.
- **`completed`** (`_wait_for_terminal_status`): optional; annotated `Optional[threading.Event]`, and
  used only through `is_set()` and `wait(timeout)`, so Property 5's fake with those two methods
  works. With `None`, the function polls exactly as today.
- **`sleep`** (`_wait_for_terminal_status`): a callable taking seconds; the default is `time.sleep`.
  It is a test seam.

### Edge cases

- **The run ends before the starter waits.** The Event is already set, so `wait` returns at once, and
  the re-read sees the settled row.
- **The run ends between a read and the decision.** The read showed `running` and the signal was
  sampled unset before it, so the starter calls `wait`. That returns at once, and the next read sees
  the settled row: no WARNING and no extra fallback interval (D5).
- **A run longer than the fallback interval.** `wait` times out every 0.5 s and the row is re-read,
  as today. After the signal, one more read releases.
- **The run ends in under a millisecond.** The first read, right after dispatch, may already see the
  row settled; the starter returns without waiting.
- **The terminal commit lands just before a fallback re-read.** That re-read sees the settled row and
  releases, possibly inside the tail, as today (D3). The later signal is harmless, because nothing
  waits on that Event any more.
- **The executor raises.** `_run` logs it, then signals. `execute`'s own containment has marked the
  row `failed`, so the re-read releases.
- **The row is deleted while the starter waits.** The read returns `None` and the starter returns, as
  today.
- **The group stops while a run is in flight.** The stop sets the stop event and joins for up to 5 s,
  as today. The wait does not watch the stop event, so the run is not aborted. The join now completes
  sooner, because the starter returns right after the run. P7 tests this.
- **A manual run during a triggered run.** The manual path passes no callback and is not gated by the
  dispatcher, as today. Each Event belongs to one execution id, so the two runs cannot wake each
  other.
- **Several workflows.** Each has its own dispatcher thread and its own per-run Events; nothing is
  shared.
- **Wall-clock changes.** `time.sleep` takes a relative timeout. `Event.wait` times out on the
  monotonic clock on Python 3.11 (JP5, x86) built against glibc 2.30 or later. On 3.10 (JP6) and
  3.9 (host tests), its timeout follows the system clock, so a wall-clock step can shorten or
  lengthen one fallback wait. The completion signal is unaffected: it ends the wait at once. A
  shortened wait costs one extra read, and a lengthened one still ends on the signal.
- **Interpreters.** This is plain `threading` code: Python 3.11 in the JP5 and x86 images, 3.10 in the
  JP6 image, 3.9 for the host tests.

## Correctness Properties

Property 1: Bug Condition - The run starter returns right after the run ends

_For any_ fake run duration d between 10 and 300 ms and any outcome (`completed`, `failed`), with the
fallback interval set to 3 s, `default_run_starter'` SHALL return no later than 0.5 s after the fake
executor's thread ended (t_return − t_end ≤ 0.5 s, a one-sided bound), and the row SHALL be settled
at that moment. F returns at the first 3 s re-read after the end. The host bound is 0.5 s rather than ε so that it holds on a loaded CI host; F's
release lag is at least 2.7 s, more than five times the bound.

**Validates: Requirements 2.1**

Property 2: Bug Condition - `drop` accepts a firing that arrives after the run ended

_For any_ fake run duration d between 10 and 60 ms and any delay m between 100 and 150 ms, a
`WorkflowActivationCore` with a `drop` trigger and the production `default_run_starter` (0.5 s
fallback) SHALL accept a second firing delivered m after the first run's executor thread ended, and
SHALL run it. F discards it, because the node stays in flight until the 0.5 s re-read.

**Validates: Requirements 2.2**

Property 3: Bug Condition - A backlog drains at the run rate

Five `queue` firings delivered at once, with 30 ms fake runs and the production fallback, SHALL all
reach `completed` within 1.0 s of the first firing. F needs about 2.0 s: each of the first four
activations holds the dispatcher until a 0.5 s re-read, and the fifth completes about 30 ms after its
dispatch.

**Validates: Requirements 2.3**

Property 4: Fix Contract - The completion signal is delivered exactly once

_For any_ executor behavior (returns, raises) and duration, `dispatch(id, on_complete=cb)` SHALL:
- return `True`;
- call `cb` exactly once, on the `workflow-execution-<id>` thread, after the executor callback
  returned or raised;
- contain and log an exception raised by `cb`.

With no executor registered it SHALL return `False` and never call `cb`. A non-callable `cb` SHALL
raise `TypeError` before any thread starts.

**Validates: Requirements 2.1, 3.8**

Property 5: Fix Contract and Preservation - The wait releases only on a settled row

_For any_ scripted sequence of row reads (`pending`, `running`, `completed`, `failed`, missing) and
any point at which the signal is set, `_wait_for_terminal_status` SHALL:
- return right after the first read that shows a settled row, and never after a read that shows an
  active one;
- block exactly once per iteration that does not return, for exactly the fallback interval. The
  blocking call is `wait` on the Event when the signal was unset before that iteration's read, and
  `sleep` otherwise;
- log exactly one WARNING if a read that started after the signal was set showed an active row, and
  none otherwise. The WARNING names the execution id and the status that read showed. A signal that
  arrives between a read and the decision causes no WARNING.

With `completed=None` it SHALL behave as F: read, then sleep for the fallback interval.

**Validates: Requirements 2.5, 3.1, 3.9**

Property 6: Preservation - The starter never returns while the run is active

_For any_ run that holds its row in `running` for h between 0 and 700 ms before its terminal write, the
starter SHALL return no earlier than that write. An executor that ends but leaves the row `running`
SHALL keep the starter waiting until the row is settled by other means. When the terminal write
comes after the starter's first read and the fallback interval is longer than the whole run, its
tail included, the starter SHALL also return no earlier than the end of the run's tail, so that the
next run never starts inside it (D3). This holds on F and on F'.

**Validates: Requirements 2.5, 3.1**

Property 7: Preservation - The other paths are unchanged

On F and on F':
- `dispatch` without `on_complete` SHALL keep its thread name, daemon flag, return values and
  exception containment;
- with no executor, the row SHALL stay `pending`;
- an idle, started dispatcher SHALL call the session factory zero times and start no run thread;
- a stop of the activation core while a run is in flight SHALL return only after the run's row has
  settled, SHALL leave the run to complete, and SHALL leave no dispatcher thread running;
- the existing suites SHALL pass unchanged: trigger-activation-runtime Properties 9, 10, 11 and 13,
  and the trigger lifecycle, executor-hook, engine API, engine integration and continuous-runner
  suites.

**Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.10**

## Testing Strategy

### Test environment

The host command runs Python 3.9 with no GStreamer, Greengrass or device. Run it from the
repository root: the root conftest copies `test/backend-test/utils/inference_out_for_test/` relative
to the current directory (`test/backend-test/conftest.py:83–91`), and every test errors when the
suite starts elsewhere.

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/ -q --continue-on-collection-errors
```

- On the build host, `test_mqtt_publish_retain.py` fails to collect, because the `awsiot` SDK is not
  installed. That error is pre-existing. At `795ce528`, and again at `1fa52f3a` (which adds only a
  README change), the suite gives 1904 passed, 10 skipped and that 1 error, in about 3.5 minutes.
- The same suites also run in the flask-app container, following the builds.md pattern (python3.11
  for JP5, python3.10 for JP6).
- Timing bounds are at least 4× the value expected on F' (P7: 3.5 s against about 0.8 s). In the
  exploration tests they also lie well below F's value (E3: 1.0 s against about 2.0 s). The
  preservation tests pass on F as well, so their bounds also cover F's value (P7: about 1.0 s).
- Hypothesis settings: E1, E2, P1 and Property 4 run in real time, so each sets
  `@settings(max_examples=10, deadline=None)`; under `engine-fast` they would otherwise get 25
  examples and a 200 ms deadline. Property 5 has no real time and uses the profile.
- Log assertions inside `@given`: `caplog` is function-scoped, and Hypothesis rejects function-scoped
  fixtures inside `@given` (`HealthCheck.function_scoped_fixture`). Property 4 and Property 5 capture
  records with a handler installed in the test body, as `captured_logs` does in
  `test_property_tuning_sample_export.py:272–295`. The plain tests (P4, P6) use `caplog`.
- Databases inside `@given`: E1 and P1 call `make_session_factory()`, which needs no fixture. One
  file-backed database is shared across examples and rows are isolated by a unique registration id,
  as `test_property_trigger_context.py:429–438` does.

### Exploration tests (must FAIL on F)

File: `test/backend-test/workflow_engine/test_dispatch_wakeup_exploration.py`.

- **E1, Property 1 (Hypothesis, 10 examples, no deadline).** Use a real SQLite session factory
  (`make_session_factory`) and a fake executor registered with `executor.set_executor`. The fake
  marks the row `running`, sleeps d, writes `completed` or `failed`, and records when its thread ends.
  Call `default_run_starter(reg, session_factory=sf, poll_interval=3.0)`. Assert that
  t_return − t_end ≤ 0.5 s (one-sided: on F', at small d, a first read that lands between the fake's
  terminal commit and its end timestamp returns a few µs before the end) and that the row is settled
  at that moment. Draw d from 10–300 ms: at d near 0 the fake can commit its terminal status before
  the starter's first read, so that example passes on F too, and a shrinking replay can make
  Hypothesis report `Flaky` instead of the late return. The run that ends before the first read is
  the "under a millisecond" edge case, which Property 5 covers.
- **E2, Property 2 (Hypothesis, 10 examples, no deadline).** Use a `WorkflowActivationCore` with
  `TriggerPolicy(concurrency_policy="drop")` and the production starter. Draw d from 10–60 ms, for
  the same reason as E1. Fire, wait on an Event set by the fake executor's last line, sleep m, fire
  again. Assert that the second firing returns `True` and that its row reaches `completed`.
- **E3, Property 3.** Use `queue` with `queue_depth` 10 and the production 0.5 s fallback, fire five
  times at once, and assert that all five rows are `completed` within 1.0 s. F takes about 2.0 s.
- **E4, integration.** Use the real `WorkflowExecutor`, registered with `register_workflow_executor`
  and a scripted pipeline manager (the `ScriptedPipelineManager` shape from
  `test_workflow_engine_integration.py`), with `pipeline_executor._WORKFLOW_CAPTURE_ROOT` patched to
  `tmp_path`. Add a `TriggerSubscriptionManager` with the `StubTransportFactory` from
  `test_trigger_lifecycle.py`, the compiled document
  `trigger_compiled(parameters={"concurrency_policy": "drop"})`, and
  `run_starter_factory=lambda rid: default_run_starter(rid, session_factory=sf, poll_interval=3.0)`.
  Deliver #1, join its `workflow-execution-<id>` thread, sleep 0.1 s, and deliver #2. Assert that
  `on_delivery` returns `True` and that both rows are `completed`.

Record each counterexample in tasks.md.

### Preservation tests (must PASS on F and on F')

File: `test/backend-test/workflow_engine/test_dispatch_wakeup_preservation.py`. Values are observed on
F first.

- **P1, Property 6 (Hypothesis over h and the outcome, 10 examples, no deadline).** The fake executor
  takes `t_write = time.monotonic()` immediately before the terminal `session.commit()`, so a fallback
  read can never land between the commit and the timestamp. The starter returns no earlier than
  `t_write`, and the row is settled.
- **P2, Property 6.** The fake executor sets `running` and returns, with `poll_interval=0.2`. The
  starter is still blocked after 1.2 s. The test then writes `failed`, and the starter returns within
  1.0 s.
- **P3, Property 7.** With no executor, the row stays `pending` and the starter returns within 0.5 s.
- **P4, Property 7.** `dispatch(id)` with no callback: the thread is named `workflow-execution-<id>`,
  it is a daemon, `True` is returned, and an executor exception is logged as "Workflow execution %s
  raised". With no executor, `False` is returned and the INFO line is logged.
- **P5, Property 7.** Snapshot the names of the live threads, then start a `WorkflowActivationCore`
  over a counting session factory and fire nothing for 1.0 s. Assert zero session-factory calls and
  no `workflow-execution-*` thread that was not in the snapshot.
- **P6.** The fake executor writes `failed` and then raises. The starter returns, and the hook logs
  the exception. Wait up to 1 s for the "Workflow execution %s raised" record (the existing
  `wait_for` helper over `caplog.records`) before asserting it: on F, a first read that lands
  between the fake's `failed` commit and its raise returns before the hook has logged, so a
  `caplog` check made right after the return can miss the record.
- **P7, Property 7.** Use a `WorkflowActivationCore` with a `drop` trigger and
  `default_run_starter(reg, session_factory=sf)` (production 0.5 s fallback). The fake executor marks
  the row `running`, sleeps 0.8 s and writes `completed`. Fire once and wait until the row reads
  `running`. Find the `workflow-activation-dispatcher-<reg>` thread in `threading.enumerate()`, then
  call `core.stop(timeout=5.0)` and time it. Assert that:
  - `stop` returns within 3.5 s (about 1.0 s on F and about 0.8 s on F'). That is under the 5 s
    join timeout, so a hung join still fails;
  - the row reads `completed` when `stop` returns;
  - the dispatcher thread is no longer alive.
- **P8, Property 6 (D3).** The fake executor marks the row `running` and holds it for 300 ms, so
  the starter's first read, right after the dispatch, sees `running` even on a slow CI host (the
  test assumes that the first read comes within 300 ms of the dispatch; it is the one condition
  under which P8 fails on F and on F' alike). It then commits `completed`, sleeps 200 ms (the tail)
  and takes `t_tail = time.monotonic()` as its last statement. With `poll_interval=3.0`, the
  starter returns no earlier than `t_tail`, and the row is `completed`. Expected: about 3 s on F
  (the first re-read) and about 0.5 s on F'. A signal sent at the terminal write instead of the end
  of the thread (the rejected alternative) fails it.
- **Existing suites, unchanged:**
  - `test_property_concurrency_policies.py` (Property 9);
  - `test_property_dispatch_ordering.py` (Property 10);
  - `test_property_or_semantics.py` (Property 11);
  - `test_property_trigger_context.py` (Property 13);
  - `test_trigger_lifecycle.py`;
  - `test_workflow_engine_api.py`, including `TestExecutorHook`;
  - `test_workflow_engine_integration.py`;
  - `test_workflow_continuous_runner.py`.

### Unit and property tests for the new code

File: `test/backend-test/workflow_engine/test_property_dispatch_completion_signal.py`. These tests are
written with the fix, because F has no `on_complete` or `completed`.

- **Property 4 (Hypothesis, 10 examples, no deadline).** Generate the executor behavior (returns,
  raises `RuntimeError`), the callback behavior (records, raises) and a 0–20 ms duration. Order the
  events through a shared list and assert the callback's thread name. Capture the log records with a
  handler installed in the test body. Separate tests cover `TypeError` for a non-callable callback
  and no call without an executor.
- **Property 5 (Hypothesis, no threads, no sleeping).**
  - A fake session factory replays a scripted list of statuses, with `None` for a vanished row.
  - A fake Event flips to set at a scripted read index and records each `wait(timeout)`.
  - A recording `sleep` captures the fallback sleeps.
  - The script includes a signal that is set between a read and the decision.
  - Assert:
    - the return index;
    - that each non-returning iteration makes exactly one blocking call with
      `timeout == poll_interval`, and which call that is (`wait` when the signal was unset before
      the read, `sleep` otherwise);
    - the WARNING count, from a handler installed in the test body, and that the WARNING's message
      names the execution id and the status that read showed.
- Construction checks: `default_run_starter` raises `ValueError` for 0, a negative value, NaN,
  infinity and a finite value above `threading.TIMEOUT_MAX` (1e10), and `TypeError` for `None`.

### Unit and integration split

| Level | What | Where |
|---|---|---|
| Pure unit (no threads) | Wait-loop release condition, blocking calls, WARNING | Property 5 |
| Unit with short-lived threads | Hook signal contract | Property 4, P4 |
| Unit with a real SQLite file and a fake executor | Starter release lag, release only on a settled row and not inside the run's tail, no executor | E1, P1–P3, P6, P8 |
| Activation core with a fake executor | `drop` acceptance, backlog drain, idle, stop during a run | E2, E3, P5, P7 |
| Integration with the real `WorkflowExecutor` and a scripted pipeline | The completion signal reaching the starter through the real executor path | E4 |
| Device | Throughput, drops, release timing, overlap, idle CPU, health | On-device verification below |

### On-device verification (MIC-730, JP5)

builds.md requires on-device verification before the implementation is committed.

**Background activity (every device, every window).** The backend container and its database are
shared with other activity on the device. On the PPE demo, the clipper and the live view run inside
the backend container and read the device API, and the demo prunes its old runs on a schedule. A
difference in that activity between the before and the after windows can move V8 by more than its
tolerance, and a prune that holds the database's locks can show up in V2, V6 or V7. So every window
runs against a fixed background:
- **Idle-CPU windows** (steps 1 and 6, V8) start at least 120 s after the last run of any earlier
  window or demo activity, and after the helpers have finished the work that run gave them. On the
  PPE demo, the clipper polls the device API every 0.25 s until 60 s after the last run and every
  2 s after that, and it closes, encodes and publishes its last clip a few seconds after the last
  hit; its log shows the publish. The before and the after window both follow this rule.
- **Live view.** Every window, before and after alike, runs with the same live-view clients
  connected, preferably none.
- **Continuous workflows.** A continuous workflow, where one runs, is paused for each idle-CPU window
  (`POST /workflows/registrations/{id}/continuous/pause`, `api.py:329`) and resumed right after it
  (`.../continuous/resume`, `api.py:340`), before and after alike, because its runs would dominate
  the idle CPU. A pause persists across backend restarts, so none may be left in place over a
  deployment. If the workflow has not resumed, with its counters advancing, within 60 s, stop and
  report to the owner.
- **Periodic jobs.** Before the first window of step 1, record the schedule of every periodic job
  outside LocalServer that writes the backend database or the artifact volume, such as an artifact
  prune. On the PPE demo, the clipper prunes one interval (an hour by default) after it starts and
  every interval after that, and a daily scheduled job prunes too. Derive each job's next run from
  its schedule or its log. A helper restarted in step 2 restarts its own schedule.
  - Start no window and no soak that a scheduled run could overlap, allowing for the job's longest
    run in its log. When a window or the soak cannot be placed clear of a run, wait until that run
    has finished.
  - Check each job's log after every window and after the soak ("Invalid windows", step 5).
  - LocalServer's own housekeeping of continuous runs, every 60 s once a continuous workflow has
    started (`run_retention.py:74`, `continuous_runner.py:766–767`), is the same on both builds and
    is part of the background.
- **Collection.** Collect and derive each window (steps 4–5) before the next window starts. During
  a window, nothing else reads the device API apart from the helpers that were running in step 1.

The procedure:

1. **Before deploying, on the current build.**
   - Record the LocalServer version, the commit it was built from (if known) and the backend
     container's `RestartCount`.
   - Record free space on the artifact volume (`df -h /aws_dda`). At least 10 GB must be free.
     Otherwise stop and report to the owner; delete nothing.
   - Record the periodic jobs and their next runs (background rule). If a demo trigger loop is
     running, record its rate and options, then stop it; step 8 says when it is restarted.
     Stopping a demo trigger loop can run one prune (on the PPE demo, the stop command waits for
     the open clip and then prunes runs and clips once, which writes the database and the artifact
     volume); start no window until its summary line is in the prune log.
   - Measure idle CPU over 300 s under the background rule, with no trigger publisher running: read
     the backend container's cumulative CPU time from its cgroup at the start and end of the window
     (`cpuacct.usage` under cgroup v1 on JP5, `cpu.stat` `usage_usec` under cgroup v2).
   - Measure the 1.8 Hz and 5 Hz windows (steps 3–5). The baseline did not read the run logs, so
     these windows give the Today values in the table below, and the 5 Hz window decides the
     separation rule.
   - Compare the run p50 (node status) of both windows with the baseline's 258 ms. If either is more
     than 20 ms higher, report it to the owner before deploying: V3 and V5 assume the baseline's run
     duration.
2. **Deploy safely.** Revise only the LocalServer component version. The MIC-730 has a known Triton
   model-load wedge, and the deployment restarts or recreates the backend container, so:
   - stop every trigger publisher before the deployment. Also stop cleanly the device-side helpers
     that run inside the backend container, which on the MIC-730 are the demo's clipper and live view;
   - trigger no run until the deployment is COMPLETED and every model component's Startup has
     finished (READY, or "gave up" in its log);
   - record the backend container's id and `RestartCount` again; V9 compares against these values;
   - run the in-image check (Build and Rollout): the sha256 of `workflow_engine/executor.py` and
     `workflow_engine/trigger_runtime.py` inside the backend container must equal the snapshot's.
     A mismatch stops the session with a report to the owner;
   - kick the PPE model with one manual run (`POST /workflows/registrations/{id}/trigger`) and wait
     for READY;
   - start one more manual run the same way; it must complete. This is the MIC-730 check of 3.5
     (V11);
   - restart the helpers, so that steps 3–7 run under the same load as step 1, and record when.
     A helper's own schedule, such as the clipper's prune, restarts with it (background rule).

   If a model stays LOADING for more than 10 minutes, whether or not the backend log shows
   `Python model file not found`, capture the evidence and stop. A backend restart needs the owner's
   go-ahead.
3. **Measure the windows**, 70 s each:
   - 1.8 Hz (gated, and repeated under the 1.8 Hz rule below when it calls for it);
   - 5 Hz (gated);
   - f_x, only when the separation rule calls for it (gated);
   - optionally 20 Hz, for information only: the ceiling, where completed runs/s ≈ 1/(c + 0.025 s).

   Each trigger message is `{"seq": n, "ts": t}`, where t is the host's `time.time()` at publish.
   Publish message n (n = 1, 2, …) at t0 + (n − 1)/f, an absolute schedule that does not drift, and
   log every message's seq and ts on the host. The trigger publisher that produced the baseline
   sends both fields on such a schedule but logs only the seq. Add ts to its log line, or
   reconstruct ts_j = ts_1 + (j − 1)/f from the schedule, which that publisher held within 1 ms. If
   it is not available, use an equivalent. Neither clock may be stepped during a window: a step
   shows as a lasting shift in the floor of receive_k − ts_k, and makes the window invalid (step 5).
   Leave at least 10 s with no publisher running between windows, so the step-4 margins never take
   in another window's runs. The MQTT transports stamp the Trigger_Context `timestamp` with the
   device's `time.time()` on delivery (`trigger_runtime.py:1850`, `:2152`), so receive times are on
   the device clock. ts serves only to attribute drops (step 5).
4. **Collect each window** from the device API. The collector that produced the baseline lists runs
   this way but does not read the run logs; extend it, or use an equivalent that follows this step.
   - List the runs from `GET /workflows/registrations/{id}`, whose `executions` array is unbounded
     (`api.py:174–193`). Keep the rows whose `startedAt` (whole seconds, device clock) lies in
     [window start − 1 s, window end + 5 s]. Take the window start and end on the device clock, or on
     a host clock checked to be within 0.5 s of it.
   - Do not use `GET /workflows/registrations/{id}/executions`. It returns at most the newest 50
     runs (`api.py:201`, `:227–229`), and a 70 s window holds about 115–230.
   - Take each run's status from that list, then read:
     - `GET /workflows/executions/{executionId}/node-status`: the trigger node's `durationMs` spans
       the whole run;
     - `GET /workflows/executions/{executionId}/metadata`: the trigger's `payload_json` (seq and
       ts_k) and its device receive timestamp (receive_k);
     - `GET /workflows/executions/{executionId}/log`: the first and last record times (first_k,
       last_k). Convert them with the backend container's time zone; both clocks are the device
       clock.
   - Dropped means a sequence number published in the window with no run among the window's runs.
     Its ts comes from the publisher's log or the schedule (step 3).
   - For the soak, the list alone is enough: V1 needs only each run's status.
5. **Derive**, over the window's completed runs in receive order:
   - c_k = last_k − receive_k;
   - gap_k = receive_{k+1} − receive_k;
   - idle_k = receive_{k+1} − last_k;
   - e_k = ⌈(c_k + 0.05 s)·f⌉/f − c_k, at the window's trigger rate f;
   - completed runs/s = (n − 1) / (receive_last − receive_first), the baseline's formula (1.8 Hz
     today: 113 / 69.405 s = 1.628).

   At 1.8 Hz, also:
   - **attribute each drop.**
     - L = min over the window's runs of (receive_k − ts_k): the smallest receive latency plus the
       offset between the two clocks, which L absorbs.
     - For each dropped seq j, k is the window's run with the largest seq below j, and a_j = ts_j + L
       is the earliest device time at which j can have arrived. The 0.556 s period exceeds the
       baseline's 0.43 s spread of receive latency, so triggers arrive in seq order and run k was the
       one in flight. A window whose spread of receive_k − ts_k exceeds the 0.556 s trigger period
       is invalid (Invalid windows).
     - j is a **dispatcher drop** when a_j > last_k + ε (0.05 s), or when no run of the window has a
       seq below j.
   - **count V3 from seq 4.** Both the dropped share and completed runs/s count only the triggers
     from seq 4 on and their runs (1.8 Hz today: 11 of 123 dropped, and 111 / 67.777 s = 1.638
     runs/s). "Why V3 counts from seq 4" below gives the reason. V2 still covers every trigger.

   **Invalid windows.** A 70 s window is invalid when:
   - a clock was stepped during it;
   - one of its runs has no readable log record;
   - at 1.8 Hz, its spread of receive_k − ts_k exceeds the trigger period;
   - a periodic job recorded under the background rule (such as an artifact prune) ran during it.

   An idle-CPU window or a soak is invalid when such a job ran during it. Record why and repeat the
   window or the soak once. An invalid window does not count toward the 1.8 Hz rule. If the repeat
   is invalid too, stop and report to the owner.
6. **Repeat the idle-CPU window** of step 1 on the new build, under the background rule: it starts
   at least 120 s after the last run of step 3's windows.
7. **Soak.** Start it clear of every scheduled job (background rule). Run 10 minutes at 1.8 Hz
   (about 1,080 runs, about 2 GB of artifacts), then 20 minutes idle. Then:
   - check V1 over the soak's runs;
   - re-check the free space, the backend container's id and its `RestartCount`;
   - read V10 from `docker logs --since <deployment time>` of the container recorded in step 2. That
     log keeps at most 3 × 50 MB (`src/docker-compose.yaml:35–39`, the same on every backend
     service). If it no longer reaches back to the deployment, read the missing span from the
     backend's `application.log` (`src/backend/dda_logging/custom_logging.py:147–148`), which
     receives the same records.
8. **After the session.**
   - Stop every trigger publisher.
   - Record in `verification-notes.md` how many runs the session created and their total size. The
     session's runs are the registration's runs whose `startedAt` lies between the start of step 1
     and the end of the soak, about 1,600–2,300 on the MIC-730. Their size is the sum of `du -sb`
     over their run directories, `/aws_dda/captures/<workflowId>/<executionId>/`
     (`pipeline_executor.py:261`), roughly 3–4 GB. Delete nothing. The clean-up is left to the owner,
     because run-artifact retention is out of scope.
   - If every gated check passed, the new LocalServer stays deployed.
   - If V3 is the only gate not met after the 1.8 Hz rule's windows, report to the owner as that
     rule says, with no rollback. The new LocalServer stays deployed until the owner decides.
   - If any other gated check failed, redeploy the previous version (Rollback) with the owner's
     go-ahead, following the same deployment safety steps.
   - Leave the demo workflow and its helpers as they were found. A demo trigger loop that step 1
     stopped is restarted with the options recorded there once the session has ended with a
     LocalServer deployed and healthy: the new one kept, or the previous one restored. After a
     stop-and-report, it stays stopped, and the report says so.

Acceptance criteria:

| # | Check | Window | Pass | Today |
|---|---|---|---|---|
| V1 | Failed runs | Every window, and the 10-minute soak | 0 | 0 |
| V2 | Dispatcher drops (step 5) | 1.8 Hz, every window run | At most one over all the windows, and none in the last one (1.8 Hz rule) | 10–11 of the 12 drops on 1.0.54 (estimated, no run logs); measured in step 1 |
| V3 | Dropped share and completed runs/s, from seq 4 (step 5) | 1.8 Hz | ≤ 3 % dropped and ≥ 1.75 completed runs/s in at least one window (1.8 Hz rule) | 11 of 123 (8.9 %) and 1.638 on 1.0.54 (12 of 126 and 1.628 over the whole window); measured in step 1 |
| V4 | Release on completion: p50 of idle_k − e_k over consecutive pairs | 5 Hz | ≤ 0.05 s | About 0.2 s (one period), measured in step 1 |
| V4x | Release on completion, separation window; only when the separation rule calls for it | f_x | ≤ 10 % of triggers dropped | ≥ 30 % dropped, measured in step 1 (required for V4x to apply) |
| V5 | Completed runs/s (step 5) | 5 Hz | ≥ 2.3 while c_k p95 ≤ 0.38 s. Otherwise record c_k: the run itself then limits 5 Hz, and V4 or V4x decides | 1.649 on 1.0.54; measured in step 1 |
| V6 | Run duration p50 (node status) | 1.8 Hz, 5 Hz | ≤ the same rate's step-1 p50 + 40 ms (about 300 ms on 1.0.54) | 258 / 257 ms on 1.0.54; measured in step 1 |
| V7 | No overlap: first_{k+1} ≥ last_k | 1.8 Hz, 5 Hz | Every consecutive pair whose run k has c_k ≤ 0.5 s. A pair whose run k has c_k > 0.5 s may overlap through a fallback release (D3); record any such overlap with its c_k | Measured in step 1 |
| V8 | Idle CPU of the backend container | 300 s, no triggers, under the background rule | At most the pre-deploy value plus 10 % of it or plus 0.5 % of one core, whichever is larger. If it misses, repeat the after window once, starting at least 120 s after the first one ended; V8 passes when the repeat meets it | Measured in step 1 |
| V9 | Backend health | Whole session and soak | No container restart and no crash loop: the container id and `RestartCount` recorded after the deployment (step 2) are unchanged at the end | – |
| V10 | Wait WARNING | From the deployment to the end of the soak | Zero "executor finished but the execution row still reads" lines in the log of the backend container recorded in step 2 (step 7 says how to read it) | – |
| V11 | Manual run (3.5) | After the deployment, every device | The manual run through `POST /workflows/registrations/{id}/trigger` completes | – |
| V12 | Continuous workflow (3.6), where one runs | Each pause of the background rule, and the deployment | It resumes, with its counters advancing, within 60 s after each pause and after the deployment | – |

**Why V2 attributes the drops.** With the fix, a 1.8 Hz trigger is discarded only while the
previous trigger's run is still going. Apart from a long run, that happens only when the previous
trigger arrived late: lat_k − lat_{k+1} > T − (c_k + δ), where T = 0.556 s and δ is the release
lag; the right-hand side is about 0.2–0.29 s. The baseline's receive latency (707 runs: 35 ms p50,
149 ms p95, 282 ms p99, 460 ms max) reaches that for about 1–2 % of triggers, so a raw drop count
cannot tell a correct fix from a late release. The attribution can:
- a discard caused by a latency spike or a long run has a_j ≤ last_k + ε, because j arrived while
  run k was still going;
- a trigger that arrived later, by that bound, and was still discarded is the defect itself (2.2);
- a_j is a lower bound on j's arrival, up to the few ms by which j's own latency can be below the
  window's minimum (31–33 ms in every baseline window). So the check practically never blames the
  dispatcher for a late trigger; otherwise it can only under-count.

Applied to the baseline's 1.8 Hz window, with c_k estimated from `durationMs` because the baseline
has no run logs, the attribution gives 10–11 of the 12 drops to the dispatcher. The other one
followed a trigger whose delivery took 257 ms longer than the window's fastest. A model that
resamples the baseline's receive latencies and run durations gives the current code about eight
dispatcher drops per window, and a correct fix none in 2,000 windows.

A trigger lost between the publisher and the device looks like a dispatcher drop, because the
device logs a `drop` discard only at DEBUG (`trigger_runtime.py:330`). The baseline publishes with
MQTT QoS 0, so a rare loss is possible, although none is evident in the baseline's 1.67–1.9 Hz
windows. The 1.8 Hz rule therefore tolerates one dispatcher drop per session, provided that a clean
window follows it. The current code produces several in every window.

**Why V3 counts from seq 4.** In 3 of the 7 baseline windows (1.9, 2 and 3 Hz), the window's first
run, which follows the pause before the window, was still active at the first 0.5 s re-read. A
trigger that arrived 0.95–1.01 s after the first run's own trigger was still dropped, although that
re-read comes about 0.51 s after it. Two more windows (1.8 and 5 Hz) are consistent with that. The
poll delays only the release, not the run, so the first run's extra time, which lies outside its
`durationMs` (only 20–70 ms above the window's p50), remains with the fix. Such a run then still
discards seq 2 at 1.8 Hz, and seq 3 too if it lasts more than 1.1 s. These are not dispatcher
drops, but they would use up most of V3's 3 % budget.
Counting from seq 4 removes them from V3 and keeps the comparison with today's numbers like for
like. In the model, a first run of 0.45–1.05 s, which is consistent with these windows, raises a
correct fix's V3 miss rate at a measured cycle of about 0.32 s from about 6 % to 17 % per window.
Counting from seq 4 brings it back to about 6 %.

**The 1.8 Hz rule.** In the model, a correct fix still misses V3 in about 0.5 % of windows at a
measured cycle of about 0.28 s, 6–7 % at 0.32 s and 15–20 % at 0.35 s, the top of the estimate,
because of the latency tail alone. So:
- After the deployment, run the first 1.8 Hz window. Run another, at least 10 s after the last, when
  the last window had exactly one dispatcher drop or missed V3; at most three 1.8 Hz windows in all.
  With three windows, fewer than 1 % of sessions end without a V3 pass in the model.
- V2 fails as soon as the windows hold two dispatcher drops in all. It passes when they hold at
  most one and the last window run holds none. Record a single dispatcher drop with the values
  below.
- V3 passes when at least one window meets it.
- If V2 passes and no window meets V3, record each drop's j and k, run k's excess latency
  (receive_k − ts_k − L), c_k and a_j − last_k, and report them to the owner instead of rolling
  back. Receive latency is out of scope (bugfix.md).
- If step 1 shows no dispatcher drop at 1.8 Hz, V2 cannot show the fix on this device. It still
  gates, and V4 (or V4x) carries the fix.

**Why V7's exception uses c_k.** On F', a run is released before its thread ends only by a read that
the signal did not wake. That is either a fallback re-read, which comes at least 0.5 s after the
trigger arrived, because the first read follows the dispatch and each wait lasts the full 0.5 s; or
the first read right after the dispatch, which sees a settled row only when the run has ended within
about a millisecond. The next run's first record comes after the release. So the next run can
overlap run k only when run k's last record came more than 0.5 s after its trigger, and a run with
c_k ≤ 0.5 s never can. The trigger node's `durationMs` cannot place a run against the re-reads: it
starts at `mark_running_all` (`pipeline_executor.py:1952`), after the readiness wait, so its offset
from the dispatch is unknown. On the MIC-730, c_k is about 0.26–0.29 s at p50, so V7 gates nearly
every pair there.

**Why V4 is relative to the cycle.** With the fix, the dispatcher is free within ε of a run's last
log record, and the next `drop` trigger that arrives after that is accepted. So idle_k is about e_k:
on F', idle_k − e_k is about 0 (or −1/f, when the release came well inside ε) at any cycle length. An
absolute bound would not work. idle_k ranges over a whole trigger period, depending on where c_k
falls within one: at 5 Hz, a correct fix with c_k of 0.2–0.25 s idles 0.15–0.2 s, and one with c_k
of 0.26–0.29 s idles 0.11–0.14 s. On F the dispatcher is free only at the re-read, about 0.53 s after
the trigger on the PPE demo. So idle_k − e_k is at least one period whenever
⌈(c_k + 0.05 s)·f⌉ < ⌈0.53 s·f⌉. At 5 Hz that holds for c_k up to 0.35 s, which covers the PPE demo.
V4 bounds the release lag only to the next trigger boundary: at 5 Hz with c_k ≈ 0.28 s, any lag
below about 0.12 s still gives idle_k = e_k. ε is the design target, and V2's attribution at 1.8 Hz
is the per-trigger device check of 2.2.

**Separation rule (every device).** Step 1 measures V4's quantity on the current build at 5 Hz.
- **At least 0.15 s.** The 5 Hz V4 separates F from F' on this device, and V4 gates. The MIC-730
  should give about 0.2 s.
- **Below 0.15 s.** The device's cycle sits where today's re-read costs no extra trigger at 5 Hz, for
  example c_k p50 above 0.35 s. V4 still gates F', because it cannot fail a correct fix, but it
  cannot show the fix. Set T_x = c_k p95 + 0.1 s from the step-1 window and f_x = 1/T_x, and run a
  70 s window at f_x on the current build too.
  - If at least 30 % of those triggers are dropped, the f_x window separates F from F', and V4x gates
    after the deploy. F' is free by c_k + ε, which is less than T_x for at least 95 % of runs, so it
    accepts nearly every trigger. F holds the node until a re-read and discards a large share.
  - If fewer than 30 % are dropped, the `drop` windows in this plan cannot show the fix on this
    device. Record c_k and both windows and report them to the owner. That device's checks are then
    non-regression only, and the host tests (E1–E4) carry the wake-up path.

### JP6 and x86 non-regression

Run this after the MIC-730 passes, on one JP6 device and one x86 device. There is no baseline table
for these devices, so the current-build windows provide it:

- Record the device's LocalServer component name, version and build commit, and apply the Build
  base rule in both directions. The x86 build uses the same variant and the same Ubuntu release
  as the device's current image (Build and Rollout, "x86 image base").
- Use a trigger-driven workflow with an MQTT trigger and `drop`. If none is registered, deploy a
  minimal one, with the owner's approval.
- The background rule above applies on these devices too, including its periodic-job record and the
  pause of a continuous workflow for each idle-CPU window.
- Before deploying, on the current build: the free-space check and the idle-CPU window of step 1,
  and the 5 Hz window, collected and derived as in steps 4–5. The 5 Hz window decides the
  separation rule on this device.
- Build from the snapshot commit, deploy and follow the deployment safety steps above wherever
  Triton models are deployed. On every device, record the backend container's id and
  `RestartCount` after the deployment, as in step 2, for V9, and run the in-image check before any
  window.
- Run the 5 Hz window, and the f_x window if the separation rule calls for it. Check V1, V4 (or V4x)
  and V7, and that completed runs/s at 5 Hz is at least 95 % of the current build's.
- Start one manual run with `POST /workflows/registrations/{id}/trigger`; it must complete (V11).
- If the device has a continuous workflow, it must keep running, with its counters advancing
  (V12).
- Check V8: the idle-CPU window on the new build, under the background rule, with V8's one repeat.
- Soak: start it clear of every scheduled job; 10 minutes at 5 Hz, with V1 over its runs, then
  30 minutes idle. Check V9 over both, and V10 as in step 7.
- After the session, as step 8, including the record of the runs and the artifact volume the session
  created.

## Build and Rollout

- Start only after the `triton-model-cleanup-wedge` builds have finished (owner-scheduled).
- **Build base.** For each test device, record the commit its current LocalServer was built
  from, and list the device-side differences in both directions:
  `git log --oneline <device-commit>..HEAD -- src/` (changes the deployment would add) and
  `git log --oneline HEAD..<device-commit> -- src/` (changes it would remove). If either list is
  non-empty, report both to the owner before the build. The owner either picks another build
  base, or accepts the difference. If the owner accepts it, step 1 still measures the current
  build, and verification-notes.md lists the extra commits next to that device's
  before-and-after table. This spec does not rebase the branch.
- **Build snapshot.** Before the first build, snapshot the uncommitted tree as a local commit on
  a separate branch, `wip/trigger-dispatch-completion-wakeup-verify`, and build every target from
  that commit. Record its id. It is a build snapshot, not the implementation commit, and it is not
  pushed. To build on the x86_64 host, carry it over with `git bundle create` and `git fetch` from
  the bundle, and check that `git rev-parse HEAD` there prints the same id. The task 8 commit
  must have the same `src/` tree as the snapshot (`git diff --quiet <snapshot> HEAD -- src/`).
- **In-image check.** After each deployment, and before any window, compare the sha256 of the
  image's `workflow_engine/executor.py` and `workflow_engine/trigger_runtime.py` inside the backend
  container (`docker exec <backend> sha256sum …`) with the snapshot's files. A mismatch stops that
  device's session with a report to the owner.
- **Pre-build checks**, on each build host and in each checkout (builds.md):
  - no build running (`pgrep -af "gdk component build"`, `pgrep -af "build-custom.sh"`);
  - no Portal deploy in progress, and none during the build;
  - `edge-cv-portal/infrastructure/cdk.out` moved aside;
  - the guard pair green.
- **Commands.** Build and publish each target with `portal-build.sh`, which runs `gdk component build`
  and `gdk component publish`. It writes `gdk-config.json` (`portal-build.sh:248`) and copies the
  target recipe over the tracked `recipe.yaml` (`:229`). Capture each run's output to the log named
  below, and restore both files afterwards with `git checkout -- gdk-config.json recipe.yaml`.

| Order | Target | Build host | Command | Log |
|---|---|---|---|---|
| 1 | `aws.edgeml.dda.LocalServer.arm64JP5`, then the MIC-730 verification | aarch64 | `./portal-build.sh aarch64 5` | `.gdk_build_jp5.log` |
| 2 | `aws.edgeml.dda.LocalServer.arm64JP6`, then the JP6 checks | aarch64 | `./portal-build.sh aarch64 6` | `.gdk_build_jp6.log` |
| 3 | The x86 test device's variant, then the x86 checks: `aws.edgeml.dda.LocalServer.amd64` (CPU-only) or `aws.edgeml.dda.LocalServer.amd64Nvidia` (NVIDIA GPU) | x86_64 | `./portal-build.sh x86_64` for `amd64`, `./portal-build.sh x86_64_nvidia` for `amd64Nvidia` | `.gdk_build_amd64.log` |

- **The x86 build needs an x86_64 host.** `build-custom.sh` takes its compose profile from the build
  host's `uname -m` (`build-custom.sh:25`, `:204`), so the aarch64 host cannot produce an amd64 image.
  builds.md documents only the JP5, JP6 and JP7 builds; the commands above follow the same
  pre-build checks. Build from the snapshot commit, carried over as the Build snapshot bullet
  says; a checkout of the branch alone would hold the unfixed code, because the fix stays
  uncommitted until task 8.
- **x86 image base.** The x86 image takes the build host's Ubuntu release (build-custom.sh:61).
  Record the release of the x86 test device's current image (`/etc/os-release` inside its
  backend container) and build on an x86_64 host with the same release. If no such host is
  available, report to the owner before building. While the branch lacks the 22.04 JPEG decode
  fix, the minimal x86 test workflow uses no JPEG Folder source.
- **One build at a time per build host.** builds.md's rule protects the host's shared working
  directories and image tags. Each later build on a host starts only after the previous one there
  has finished. Whether the x86 build may overlap an arm64 build on the other host is the owner's
  call.
- No preservation-tracked file is expected to change. At `1fa52f3a`, neither `trigger_runtime.py`
  nor `executor.py` has a hash entry under `test/backend-test/security/baselines/`. Re-check before
  the build.
- JP7 is not part of this spec's verification plan. Its next LocalServer build carries the change and
  should run the same non-regression check.
- **Rollback.** Redeploy the previous LocalServer version. The change keeps no state and alters no
  schema or configuration.

## Risks and Limits

- **More load and more artifacts.** More runs per second means proportionally more GPU and CPU load,
  and faster artifact growth: about 1.8 MB per run on the PPE demo, so about 11–16 GB per hour at
  1.8–2.5 runs/s. The soak checks free space. Retention is out of scope.
- **The signal assumes a synchronous executor.** `WorkflowExecutor.execute` is synchronous: the model
  readiness wait (`pipeline_executor.py:1843`) and the pipeline both run on the run thread. A future
  asynchronous executor would fall back to the 0.5 s re-read with a WARNING on every run, which is
  visible rather than silent.
- **Receive-latency spikes remain.** Spikes of up to 0.46 s on the MQTT path can still cause an
  occasional `drop` discard when the trigger period is close to the cycle. V2's attribution and the
  1.8 Hz rule keep them from failing a correct fix, at the cost of up to two more 70 s windows.
- **The first run after a pause stays slow.** Its extra time lies outside the dispatcher and is not
  changed here. V3 counts from seq 4 for that reason.
- **The ceiling is an estimate.** The 3.5–4 runs/s figure needs the optional 20 Hz window to confirm.
- **Some devices may not show the gain with `drop`.** Where the cycle sits just under a multiple of
  the 0.5 s re-read, today's code loses little, and the separation rule reports that device as
  non-regression only.
- **The test devices are shared.** Other activity in the backend container, such as the demo's
  helpers, its prune or a continuous workflow, can move the device checks. The background rule
  fixes it for every window, invalid windows are repeated once, and V8 has one repeat. A session
  can therefore take longer than its windows alone.

## Design-gate disposition

The design-review loop reached its iteration cap with no HIGH finding open. Its last round left
2 MEDIUM and 5 NIT findings, all on the owner-gated build and device-verification plan rather than
on the fix (D1–D5 and the code are unchanged since round 3). Under the standing design-gate policy
the loop ended there, and every open finding was applied in this revision; none is carried as an
open task:
- MEDIUM, builds not guaranteed to carry the fix: the Build snapshot and In-image check bullets
  (Build and Rollout, step 2, the JP6 and x86 section, tasks 5–8).
- MEDIUM, before-and-after comparison not isolated to the fix: the two-direction Build base rule
  and the x86 image base bullet (Build and Rollout, the JP6 and x86 section, tasks 5 and 7).
- NIT, bugfix.md 2.2 and 2.3 omitted the settled-row condition: reworded.
- NIT, validation rules looser than stated: `poll_interval` bounded by `threading.TIMEOUT_MAX`, the
  `on_complete` rule and the error-table row (Fix Implementation, Inputs and validation, Error
  handling, tasks 3.2 and 3.3).
- NIT, unstated timing margins in E1, E2, P6 and P8: ranges, the one-sided bound, the log wait and
  the 300 ms hold (Testing Strategy, tasks 1 and 2).
- NIT, gaps in the on-device procedure: V11 and V12, the prune rule in step 1, the clip condition
  in task 6, and the step-5 spread wording.
- NIT, review artifacts: the review-response section is removed, and the review files are not part
  of the documents-only commit.
