# Bugfix Requirements Document

## Introduction

During the PPE demo on a JP5 MIC-730 (LocalServer `aws.edgeml.dda.LocalServer.arm64JP5` 1.0.54,
2026-10-15), the demo workflow was triggered over MQTT with `concurrency_policy: drop`. A run takes
about 250 ms (p50 250–258 ms), yet completed triggered runs never exceeded about 1.65 runs/s at any
trigger rate from 1.67 Hz to 5 Hz, and at 1.8 Hz about 1 in 10 triggers was discarded. At every rate
the gaps between runs were exact multiples of the trigger period.

**Root cause, verified against the code at `1fa52f3a` (`795ce528` plus a README-only commit).** The
trigger dispatcher runs one activation at a time per workflow and keeps the trigger node in flight
for as long as the run starter is running. The production run starter hands the run to the executor
hook and learns that the run has ended only by re-reading the execution row every 0.5 s:

| Location | What it does |
|---|---|
| `src/backend/workflow_engine/trigger_runtime.py:104–106` | `DEFAULT_RUN_POLL_INTERVAL_SECONDS = 0.5`, "how often the production run_starter re-reads the execution row" |
| `trigger_runtime.py:507–562` (`default_run_starter`) | inserts the pending row, calls `executor.dispatch(execution_id)` (`:555`), then `_wait_for_terminal_status(...)` (`:560`) |
| `trigger_runtime.py:565–585` (`_wait_for_terminal_status`) | reads the row and, while it is `pending` or `running`, calls `time.sleep(poll_interval)` (`:585`) |
| `trigger_runtime.py:465–483` (`ActivationDispatcher.run_activation`) | marks the trigger node in flight for the whole run-starter call and clears it in `finally` |
| `trigger_runtime.py:326–337` (`TriggerGate.fire`, `drop`) | discards a firing while the node has a pending activation or `is_in_flight` (`:456–463`) is true |
| `src/backend/workflow_engine/executor.py:57–82` (`dispatch`) | starts the executor on a daemon thread and returns at once; nothing tells the caller when the run ends |

A run of about 250 ms therefore keeps its node in flight until the re-read, about 0.51–0.56 s after
its trigger arrived. Every `drop` firing in between is discarded, and the next run starts at the
first trigger after the re-read. The trigger-activation-runtime design allowed either mechanism
("polls the execution row / completion callback"); the implementation only polls.

**Today's numbers.** These were measured on the MIC-730 in 70 s windows, one full loop of the demo
video each. MQTT triggers were published from a host on a fixed schedule, each message carrying a
sequence number and the host's publish time. Over the 707 runs, the receive latency (device receive
time minus host publish time) was 35 ms p50, 149 ms p95, 282 ms p99 and 460 ms max. The run duration
is the trigger node's whole-run `durationMs` from node status. Dropped means a published sequence
number with no run. Completed runs/s is (n − 1) / (last receive − first receive) over the window's
completed runs.

| Trigger rate | Triggers | Completed runs | Failed | Dropped | Completed runs/s | Inter-run gap p50 / max | Run p50 / p95 / max |
|---|---|---|---|---|---|---|---|
| 1.67 Hz | 117 | 109 | 0 | 8 | 1.553 | 0.601 / 1.120 s | 253 / 280 / 302 ms |
| **1.8 Hz** | 126 | 114 | 0 | 12 | **1.628** | 0.556 / 1.114 s | 258 / 281 / 473 ms |
| 1.9 Hz | 133 | 101 | 0 | 32 | 1.440 | 0.531 / 1.544 s | 256 / 281 / 324 ms |
| 2 Hz | 140 | 70 | 0 | 70 | 1.001 | 1.000 / 1.505 s | 254 / 298 / 340 ms |
| 3 Hz | 210 | 103 | 0 | 107 | 1.472 | 0.666 / 1.286 s | 250 / 274 / 321 ms |
| 4 Hz | 280 | 94 | 0 | 186 | 1.345 | 0.749 / 1.467 s | 255 / 298 / 406 ms |
| **5 Hz** | 350 | 116 | 0 | 234 | **1.649** | 0.600 / 0.830 s | 257 / 284 / 342 ms |

The 1.8 Hz and 5 Hz rows are the reference for the on-device verification. The baseline did not
read the run logs, so design.md re-measures both rates, run logs included, on the build that is
current when the fix is verified, before deploying it.

**Scope.** The device-side workflow engine (`src/backend/workflow_engine/`). The same wait governs
every trigger-driven workflow on every LocalServer variant and under every concurrency policy. With
`queue`, the cost is a floor of about 0.5 s per backlog item instead of a discard.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a run started by the trigger dispatcher ends THEN the system keeps its trigger node in flight
until the run starter's next 0.5 s re-read of the execution row, which on the PPE demo comes roughly
0.2–0.3 s after every run has ended (derived from the release at 0.51–0.56 s and the 250 ms run)

1.2 WHEN a trigger with `concurrency_policy` `drop` fires in that window THEN the system discards the
firing although no run of the workflow is executing; at 1.8 Hz this discarded 12 of 126 triggers
(9.5 %) and left 1.1 s gaps

1.3 WHEN `drop` triggers arrive more often than once per 0.5 s THEN the system starts the next run only
at the first trigger after the re-read, so completed runs stay at or below about 1.65 runs/s at every
measured rate from 1.67 Hz to 5 Hz (1.0 runs/s at 2 Hz), although a run takes about 250 ms

1.4 WHEN activations are pending (a `queue` backlog, or a debounced activation waiting behind a run)
THEN the system starts each one only at the first 0.5 s re-read after the previous run ended, so a
backlog of 250 ms runs drains at about 2 runs/s at most (derived from the code, not measured)

### Expected Behavior (Correct)

In 2.1–2.3, ε is 50 ms: the design target for the time the release itself may take on the device,
from the end of the run's executor thread through the completion signal and one re-read of the row
to the dispatcher being free. On the device, 2.2 is checked trigger by trigger at 1.8 Hz (2.4, first
bullet). The host tests use looser bounds that hold on a loaded CI host.

2.1 WHEN the executor thread of a run started by the trigger dispatcher ends, and the run's execution
row has left `pending`/`running`, THEN the system SHALL return from the run starter within ε, without
waiting for a fallback re-read, so the trigger node stops being in flight within ε of the run's end

2.2 WHEN a `drop` trigger fires more than ε after the previous activation's executor thread ended, and
that activation's execution row has left `pending`/`running`, THEN the system SHALL accept the firing

2.3 WHEN activations are pending, and the previous activation's executor thread has ended with its
execution row no longer `pending`/`running`, THEN the system SHALL start the next one within ε of that
thread ending

2.4 WHEN the PPE demo workflow is triggered on the MIC-730 THEN the system SHALL meet the on-device
criteria in design.md, among them:
- at 1.8 Hz, no trigger is discarded that arrived more than ε after the last log record of the run
  in flight (a trigger lost in transit cannot be told apart from such a discard; design.md V2
  tolerates one per session when a clean window follows it). Counted from the fourth trigger of a
  window, at most 3 % of triggers are discarded and at least 1.75 completed runs/s are reached, in
  one of up to three windows (today 8.9 % and 1.638 runs/s; 9.5 % and 1.628 over the whole
  window). Receive-latency spikes and the slower first run after a pause can still discard a
  trigger. design.md tells those discards apart from the dispatcher's, and explains the repeats
  and the fourth-trigger start;
- at 5 Hz: with the fix, a run is expected to start on the first trigger that arrives more than ε
  after the previous run's last log record. The criterion is: at p50, the idle time from a run's
  last log record to the next run's trigger is at most 0.05 s longer than that model predicts for
  the run's measured cycle; today it is about one trigger period (0.2 s) longer. The PPE demo's
  measured cycle is estimated at about 0.26–0.29 s (p50), so the fix should give an idle time of
  about 0.11–0.14 s, against about 0.3 s today;
- at 5 Hz, at least 2.3 completed runs/s while the p95 of the measured cycle (trigger arrival to the
  run's last log record) is at most 0.38 s (today 1.649 runs/s);
- no failed run in any window or in the soak

These thresholds assume a run of about 258 ms (p50), as on 1.0.54. design.md re-measures the run on
the build that is current at verification time, and a build whose run is more than 20 ms slower goes
to the owner before the fix is deployed.

2.5 WHEN a run's executor thread has ended but its execution row still reads `pending` or `running`
THEN the system SHALL keep the trigger node in flight, re-read the row at the 0.5 s fallback interval
until it settles, and log one WARNING naming the execution id and the status it still reads

**What 2.4 means for throughput.** With `drop`, a run can start only when a trigger arrives, so the
gain at a given trigger rate depends on how many trigger periods one cycle spans. The cycle runs from
trigger arrival until the dispatcher is free, and is estimated at about 0.27–0.35 s after the fix.
That gives about 1.8 runs/s at 1.8 Hz (every trigger) and about 2.5 runs/s at 5 Hz (every second
trigger). The investigation's estimate of about 3.5–4 runs/s is the back-to-back ceiling, reached
with a `queue` backlog or a much higher trigger rate. It is an estimate: the parts of the cycle
outside the measured 250 ms have not been measured.

The device can only measure the cycle up to the run's last log record. After the fix, the dispatcher
is free within a few tens of ms of that record. Today it is free about 0.25 s later, at the re-read.
design.md derives these numbers.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a triggered run is pending or running THEN the system SHALL CONTINUE TO hold its trigger node
in flight and start no other activation of that workflow, and the run starter SHALL CONTINUE TO return
only once the execution row has left `pending`/`running` or no longer exists (trigger-activation-runtime
Requirement 7.6)

3.2 WHEN a trigger fires THEN the system SHALL CONTINUE TO apply each concurrency policy exactly as
defined in Requirements 7.2–7.4:
- `queue`: bounded by `queue_depth`, with the discard logged naming the node;
- `drop`: discard while an activation of the node is pending or in flight;
- `debounce`: one activation per trailing `debounce_ms` window, carrying the most recent context.

Only the in-flight window gets shorter, because it now ends when the run ends

3.3 WHEN several activations are pending at a dequeue THEN the system SHALL CONTINUE TO dispatch them
by `priority`, then firing order (Requirement 7.5). A dispatcher that is free sooner can find a
different set of activations pending, which the requirement already allows

3.4 WHEN an MQTT or OPC UA subscription delivers a message or a data change THEN the system SHALL
CONTINUE TO build and persist the Trigger_Context and to report Trigger_Health and reconnect exactly
as today, and a failing run SHALL CONTINUE TO be contained to its own execution row (Requirement 7.7)

3.5 WHEN a run is started manually (`POST /workflows/registrations/{id}/trigger`, `api.py:295`) THEN
`executor.dispatch(execution_id)` SHALL CONTINUE TO run the executor on a daemon thread named
`workflow-execution-<id>`, return `True` or `False` as today, and contain executor exceptions. The
manual path passes no completion callback

3.6 WHEN a continuous (stream camera) workflow runs THEN its runner SHALL CONTINUE TO call the executor
synchronously on its own thread (`continuous_runner.py:443`), untouched by this change

3.7 WHEN no trigger fires THEN the system SHALL CONTINUE TO spend no CPU on dispatch beyond today's idle
loop, which is the dispatcher's 0.25 s wait on an empty queue (`trigger_runtime.py:110`, `:485–490`).
The fix adds no thread, timer or database read while idle

3.8 WHEN no executor is registered THEN the run starter SHALL CONTINUE TO leave the new row `pending`
and return at once

3.9 WHEN a run is in flight THEN the run starter SHALL CONTINUE TO re-read the execution row no more
than once per 0.5 s while it waits, plus one read when the completion signal arrives, so a typical run
still costs about two reads

3.10 WHEN a trigger group stops (re-registration, removal or shutdown) THEN the dispatcher SHALL
CONTINUE TO let an in-flight run finish; stopping never aborts a run

## Out of Scope and Follow-ups

- The run's own duration (about 250 ms) and the MQTT receive latency. Latency spikes of up to 0.46 s
  can still cause an occasional `drop` discard when the trigger period is close to the cycle;
  design.md tells these discards apart from the dispatcher's.
- The extra time of the first run after a pause. In the baseline it kept that run active past the
  first 0.5 s re-read in at least 3 of 7 windows, while its `durationMs` was close to the p50. It
  lies in the run, not in the dispatcher's wait.
- The PPE demo's trigger rate and policy. With the fix, 1.8 Hz should run every trigger. A `queue`
  policy or a higher trigger rate gets closer to the back-to-back ceiling.
- Run-artifact growth. More runs per second write artifacts faster (about 1.8 MB per run on the PPE
  demo), and LocalServer's run retention removes only continuous runs. Retention and cleanup are
  separate work. The verification session records the runs it created and leaves their clean-up to
  the owner.
- Pre-existing behaviors, unchanged here and recorded for follow-up:
  - An execution row left `pending`/`running` after its executor thread has ended keeps the trigger
    node in flight indefinitely. 2.5 makes this visible but does not change it.
  - A database error while the run starter reads the row frees the trigger node before the run ends.
  - Run-log capture is per logger, so overlapping runs of two different workflows can write into
    each other's `run.log`.
