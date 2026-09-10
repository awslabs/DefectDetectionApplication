# Implementation Plan

## Overview

Fix the two verified defects from the 2026-09-09 incident (Build_Job
`851042a7-434f-4f8a-9fd4-79b25d100150`, runner `i-089a77f72bc558147`):

1. **Defect A — honor the agent's exit-75 deferral contract
   post-dispatch** (bugfix.md 1.1/1.2 → 2.1/2.2): teach
   `build_reconciliation.classify_attempt` (def line 649) a
   ResponseCode-75 case ahead of the `CODE_COMMAND_EXECUTION_FAILED`
   fallthrough (lines 761–764), and route the deferral in
   `build_dispatcher.reconcile_running_command` to the same semantics the
   pre-dispatch `PREDISPATCH_DEFER` path already implements (lines
   1963–1971): queued at the head of the queue, original `created_at`
   retained, `deferred_at` recorded, allocation kept for dedicated, and —
   critically — NO `cleanup_required=True` ledger and NO
   termination-watchdog eligibility for ephemeral runners while the
   lock-holder build may be running.
2. **Defect B — stop the double-execute exposure** (bugfix.md 1.3–1.5 →
   2.3–2.5): make the pre-SendCommand attempt claim a CONDITIONAL
   one-writer-wins write (the ephemeral send loop's only gate today is an
   in-memory `ssm.command_id` check at ~line 2200 plus an unconditional
   `update_job_fields` SET), fix the ambiguous-send recovery lookup so it
   can find a PRIOR attempt's command by job id
   (`find_command_by_comment`, lines 2307–2326, today requires
   full-comment equality on `dda-build:<job-id>:<attempt-id>` and so can
   never match a different attempt), and gate any resend on cancelling
   the prior command or proving its terminal non-execution (no
   `cancel_command` exists anywhere in the backend functions today).

**Authority-ordering guard.** The new rc-75 case lives INSIDE the
authority-5 terminal-`Failed` branch and must sit AFTER the preflight and
ENOSPC evidence checks and BEFORE the generic fallthrough — never above
agent results (authority 1), user cancellation (2), hard ceiling (3), or
infrastructure loss (4). Do not touch the meaning of any existing
`STABLE_ERROR_CODES` member (bugfix.md 3.1/3.2/3.9); if a new stable code
is introduced for observability (e.g. a non-terminal deferral marker) it
is additive.

**Do-not-confuse guard.** Job `01b18948`'s exit-75-forever loop (root-owned
lock file, 'Bad file descriptor', NO real build running) is a DIFFERENT
bug healed by `build_dispatcher.lock_ownership_heal_command` and pinned by
`test_run_as_ubuntu_unit.py`; that heal must stay intact (bugfix.md 3.8).
This spec's deferral handles a GENUINE lock hold. A deferral loop safety
valve (bounded re-verification, as the pre-dispatch path already has via
`is_reverification_due`) keeps a wedged lock from deferring forever.

**Scope guards.** `scripts/portal-build-agent.sh` is NOT modified
(`test_source_selection_preservation.py` pins its text, bugfix.md 3.7).
No preservation-tracked file (docker-compose, Dockerfiles,
requirements.txt, recipes, setup_station.sh) is touched → no
security-baseline rebaselines expected. Portal-only rollout: no component
build, no on-device verification. Out of scope: InsufficientInstanceCapacity
fallback, any change to the per-server flock model.

Test commands (sibling-suite conventions):
- Exploration/preservation suites live in `test/backend-test/portal_builds/`
  and follow the recorded convention of
  `execution_failure_counterexamples.md`:
  `HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test ~/.dda-test-venv/bin/python -m pytest test/backend-test/portal_builds/<file> --noconftest -q -p no:cacheprovider`
  (run from the repo root; the suites self-insert
  `edge-cv-portal/backend/functions` into `sys.path` exactly as
  `test_build_reconciliation_unit.py` lines 37–41 do). Because
  `--noconftest` skips the conftest's profile registration, each new suite
  registers the `ci` profile itself (`settings.register_profile("ci",
  max_examples=100)` + `load_profile(os.environ.get("HYPOTHESIS_PROFILE",
  "ci"))`) so property tests run ≥100 examples.
- Every property test carries the traceability tag comment:
  `# Feature: build-agent-exit75-deferral, Property N: <title>`.

New files this plan creates:
- `test/backend-test/portal_builds/test_exit75_deferral_exploration.py`
- `test/backend-test/portal_builds/test_exit75_deferral_preservation.py`
- `test/backend-test/portal_builds/exit75_deferral_counterexamples.md`

## Tasks

- [x] 1. Write bug condition exploration test (BEFORE implementing the fix)
  - **Property 1: Bug Condition** - Exit-75 deferral is hard-failed post-dispatch and the duplicate-send path can double-execute
  - **CRITICAL**: these tests MUST FAIL on the unfixed tree — failure confirms the bug exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: these tests encode the expected behavior — they validate the fix when they pass after implementation
  - **GOAL**: surface counterexamples demonstrating both defects; confirm or refute the root-cause analysis (if refuted, re-hypothesize before task 3)
  - Create `test/backend-test/portal_builds/test_exit75_deferral_exploration.py` with the tag `# Feature: build-agent-exit75-deferral, Property 1: exit-75 deferral and single-live-command`
  - **Part A — classification (Defect A, scoped PBT on the deterministic bug condition)**: property over invocations built from the incident shape — `{'Status': 'Failed', 'ResponseCode': 75, 'StandardOutputContent': 'Build lock /var/lock/dda-build.lock is held by another build — deferring (exit 75).', ...}` with hypothesis-generated non-preflight/non-ENOSPC text fields and `agent_result=None` — asserting `classify_attempt` does NOT return `(decided=True, status=STATUS_FAILED, error_code=CODE_COMMAND_EXECUTION_FAILED)` (bugfix.md `isBugCondition` / Fix Checking pseudocode). Include the verbatim incident case (command `a13a0825`, attempt `6c6b5483`) as a concrete example
  - **Part B — settlement side effect (Defect A)**: unit-style case around `reconcile_running_command` (mocked SSM/DynamoDB per the sibling dispatcher-suite mocking patterns in `test_dispatcher_command_reconciliation.py`) proving an ephemeral job settling an rc-75 'Failed' invocation today plans `cleanup_required=True` and transitions to failed — i.e. the runner becomes termination-watchdog eligible while the lock-holder runs
  - **Part C — recovery lookup (Defect B)**: assert `find_command_by_comment` CAN find a prior attempt's command given only the job id — construct a mocked `list_commands` response containing a command whose comment is `dda-build:<job>:<prior-attempt>` while the current attempt's comment carries a different attempt id (the incident's `88dcd6b8` vs `6c6b5483` pair); today's full-equality match returns None. Also assert the dispatch/recovery path cannot produce two concurrently live agent commands for one job (today: no `cancel_command`, unconditional attempt claim — the assertion fails)
  - Run on the UNFIXED tree with the recorded command convention
  - **EXPECTED OUTCOME**: Parts A, B and C FAIL (proving both defects); record the exact counterexamples in `test/backend-test/portal_builds/exit75_deferral_counterexamples.md` (the `execution_failure_counterexamples.md` precedent), including the hypothesis falsifying examples verbatim
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Non-75 classification, precedence ordering, dispatch and recovery behavior are byte-identical
  - **IMPORTANT**: observation-first methodology — run the UNFIXED code on non-bug-condition inputs, record actual outputs, encode them as properties that PASS on the unfixed tree and must keep passing verbatim after the fix. This oracle is IMMUTABLE — never rebaselined after implementation
  - Create `test/backend-test/portal_builds/test_exit75_deferral_preservation.py` with the tag `# Feature: build-agent-exit75-deferral, Property 2: non-bug-condition behavior preserved` (self-registered `ci` profile, ≥100 examples)
  - Property: for all terminal 'Failed' invocations whose `ResponseCode ≠ 75` (hypothesis-generated codes over `{-1, 1, 2, 64, 74, 76, 100, 137, 255, ...}` minus 75, arbitrary non-preflight/non-ENOSPC text), `classify_attempt` returns exactly today's `(decided=True, STATUS_FAILED, CODE_COMMAND_EXECUTION_FAILED, authority=5)` (bugfix.md 3.1)
  - Property: 'TimedOut'/'Cancelled'/preflight-evidence/ENOSPC-evidence inputs keep their exact current codes and authorities; agent results (succeeded/failed, ENOSPC and not) keep authority 1 even when the invocation also carries rc 75 (bugfix.md 3.2, 3.3); user cancellation, hard ceiling, infrastructure loss, Success-settlement and evidence-unavailable branches keep their exact current `Classification` tuples — enumerate the precedence table like `test_build_reconciliation_unit.py` does
  - Property: `command_comment`/`parse_command_comment` round-trip unchanged; `claim_resend` conditional one-writer-wins semantics unchanged (mocked conditional-write success/`ConditionalCheckFailedException` both directions, bugfix.md 3.5); `recover_ambiguous_send` still attaches the CURRENT attempt's found command without resend
  - Unit oracle: the `PREDISPATCH_DEFER` requeue shape (`decide_predispatch` on pgrep output showing a build process → status queued, `deferred_at` from the decision) recorded from the unfixed `build_planner` (bugfix.md 3.4); the termination watchdog still terminates a GENUINELY terminal ephemeral job's runner (bugfix.md 3.6); `lock_ownership_heal_command` content untouched (bugfix.md 3.8)
  - Run on the UNFIXED tree
  - **EXPECTED OUTCOME**: all tests PASS (baseline confirmed); collaterally re-run the existing pinned suites `test_execution_failure_preservation.py`, `test_build_reconciliation_unit.py`, `test_build_reconciliation_properties.py`, `test_run_as_ubuntu_unit.py`, `test_source_selection_preservation.py` and record their green counts
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [x] 3. Fix: post-dispatch exit-75 deferral + single-live-command dispatch

  - [x] 3.1 Classify ResponseCode 75 as a lock-held deferral, not a hard failure
    - In `build_reconciliation.classify_attempt` terminal-`Failed` branch (after the preflight check ~line 746 and ENOSPC check ~line 753, BEFORE the fallthrough at lines 761–764): when the invocation's `ResponseCode` is 75 and there is no qualifying agent result, return a NON-hard-failure classification (a distinct deferral outcome — e.g. `decided=False` with a deferral marker the caller can act on, or a new additive stable code — design choice recorded in the code comment; it must NOT be `STATUS_FAILED`/`CODE_COMMAND_EXECUTION_FAILED`)
    - Read `ResponseCode` defensively (int or numeric string, as `provider_field`/diagnostic construction at lines 861–873 already tolerates)
    - Keep the generic fallthrough byte-identical for every other code
    - _Bug_Condition: isBugCondition(X) from bugfix.md (Status='Failed' AND response_code=75 AND no agent result AND no preflight/ENOSPC evidence)_
    - _Expected_Behavior: Fix Checking pseudocode from bugfix.md — never decided-failed, never COMMAND_EXECUTION_FAILED_
    - _Preservation: bugfix.md 3.1–3.3 — non-75 codes, other statuses, agent authority unchanged_
    - _Requirements: 2.1, 3.1, 3.2, 3.3, 3.9_

  - [x] 3.2 Route the deferral in the settlement path (requeue at head, no ephemeral termination)
    - In `build_dispatcher.reconcile_running_command` (line 2449): on the deferral classification, mirror the pre-dispatch `PREDISPATCH_DEFER` handling (lines 1963–1971) — conditional transition back to queued (add the state-machine edge in `build_domain` deliberately if `next_status` lacks it), ORIGINAL `created_at` retained (head-of-queue), `deferred_at` recorded via `update_job_fields`, allocation kept for dedicated servers
    - Do NOT plan a `cleanup_required=True` ledger (lines 2540–2543 must not run for a deferral) and do NOT create termination-watchdog eligibility for the ephemeral runner (bugfix.md 2.2); keep the `runner`/instance binding on the job so the next tick re-verifies on the SAME runner — attach to a surviving command (task 3.4's job-id lookup) or re-dispatch once the lock frees
    - Clear the settled attempt's command bookkeeping so the send gate can re-dispatch, but ONLY via the conditional claim from task 3.3
    - Add a bounded deferral safety valve (re-verification interval + a cap or the existing queue-wait watchdog) so a wedged lock cannot defer forever — must NOT mask the `lock_ownership_heal_command` path (bugfix.md 3.8)
    - Audit the deferral (best-effort `audit` entry, e.g. `build_deferred_lock_held`) so the incident timeline is visible without log archaeology
    - _Bug_Condition: isBugCondition(X) with execution_mode=ephemeral — incident: i-089a77f72bc558147 terminated while the lock-holder ran_
    - _Expected_Behavior: job requeued at head + deferred_at + NOT instance_terminated (bugfix.md Fix Checking)_
    - _Preservation: bugfix.md 3.4, 3.6 — pre-dispatch defer shape and genuine-terminal watchdog behavior unchanged_
    - _Requirements: 2.1, 2.2, 3.4, 3.6, 3.8_

  - [x] 3.3 Make the pre-SendCommand attempt claim conditional (one writer wins)
    - Both send sites — dedicated (~lines 2021–2037) and ephemeral (~lines 2240–2254) — currently record the attempt with an unconditional `update_job_fields` SET; replace with a conditional write (attribute_not_exists on the live attempt / expected prior attempt state, the `claim_resend` precedent at line 2329) so overlapping tick executions (scheduled + async on-submit, `handler` line 3179) cannot both SendCommand for one job
    - The losing writer skips the send silently (next tick reconciles)
    - _Bug_Condition: isDoubleSendCondition(Y) from bugfix.md — incident: commands 1b538196/a13a0825, attempts 88dcd6b8/6c6b5483, 4 s apart_
    - _Expected_Behavior: at most one agent command per job dispatch in flight (bugfix.md 2.3)_
    - _Preservation: bugfix.md 3.5 — claim_resend conditional semantics unchanged; normal single-tick dispatch unchanged_
    - _Requirements: 2.3, 3.5_

  - [x] 3.4 Ambiguous-send recovery: find prior attempts by job id, gate resends on cancel-or-proven-dead
    - `find_command_by_comment` (lines 2307–2326): match commands whose comment PARSES to the same job id via `parse_command_comment` (line 1036) instead of requiring full-comment equality — a prior attempt's `dda-build:<job>:<other-attempt>` command is now findable and attachable (bugfix.md 2.4); prefer the most recent non-terminal match, adopt its command_id/attempt binding
    - `recover_ambiguous_send` (lines 2358–2440): before any conditional resend, either (a) `ssm.cancel_command` the prior command and confirm cancellation, or (b) require its invocation evidence to be terminal-without-execution (no ExecutionStartDateTime AND a terminal delivery status observed stably) — an 'Undeliverable'-reported command is NOT proof of non-execution (the incident's 1b538196 executed and held the lock)
    - Keep `AMBIGUOUS_SEND_VISIBILITY_MS` bound and the never-blindly-resend contract intact
    - _Bug_Condition: isDoubleSendCondition(Y) — Undeliverable-but-executed prior command treated as never-executed_
    - _Expected_Behavior: prior command found by job id; resend only after cancel-confirmed or terminal non-execution (bugfix.md 2.4, 2.5)_
    - _Preservation: bugfix.md 3.5 — current-attempt attach-without-resend unchanged_
    - _Requirements: 2.4, 2.5, 3.5_

  - [x] 3.5 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Exit-75 deferral and single-live-command
    - **IMPORTANT**: re-run the SAME suite from task 1 — do NOT write a new test
    - `HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test ~/.dda-test-venv/bin/python -m pytest test/backend-test/portal_builds/test_exit75_deferral_exploration.py --noconftest -q -p no:cacheprovider`
    - **EXPECTED OUTCOME**: Parts A, B, C all PASS (bug fixed); append the outcome to `exit75_deferral_counterexamples.md`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 3.6 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-bug-condition behavior preserved
    - **IMPORTANT**: re-run the SAME suite from task 2 UNMODIFIED — do NOT rebaseline; a preservation failure means the fix leaked outside the bug condition and the fix (not the test) is wrong
    - Also re-run the collateral pinned suites from task 2 (`test_execution_failure_preservation.py`, `test_build_reconciliation_unit.py`, `test_build_reconciliation_properties.py`, `test_run_as_ubuntu_unit.py`, `test_source_selection_preservation.py`) and confirm counts match the task-2 baseline
    - **EXPECTED OUTCOME**: all PASS (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [x] 4. Checkpoint - Ensure all tests pass
  - Full portal-builds sweep: `HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test ~/.dda-test-venv/bin/python -m pytest test/backend-test/portal_builds --noconftest -q -p no:cacheprovider` — everything green
  - Confirm no preservation-tracked file changed (`git status`/`git diff` against `src/docker-compose.yaml`, the backend/frontend/edgemlsdk Dockerfiles, `src/backend/requirements.txt`, recipe variants, `station_install/setup_station.sh`) — expected: none touched, no baseline rebaselines
  - Security guard pair (seconds, host-side): `python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`
  - Ask the user if anything is ambiguous before proceeding to rollout

- [x] 5. USER ACTION: portal deploy + live re-verification (no component build)
  - Sequence per `.kiro/steering/builds.md`: confirm no component build is running (`pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"` both empty), deploy via `edge-cv-portal/deploy-infrastructure.sh`, then move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) so the next build's drift guard stays green
  - Live verification of the deferral path (the honest claim only the real account can make): submit two builds racing for one runner/server, or reproduce a held `/var/lock/dda-build.lock`, and confirm the second job DEFERS (queued at head, `deferred_at` set, `build_deferred_lock_held` audit) instead of failing with COMMAND_EXECUTION_FAILED, the runner is NOT terminated while the first build runs, and the deferred job completes after the lock frees
  - Commit only after this verification, stating what was verified and which paths were exercised
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

## Rollout and live verification record (tasks 4, 5 — COMPLETE)

**Correction round (design review).** A design-level review of the task-3
implementation (`semantic-review/2026-09-09-222949-pr-local.md`) found 13
issues, 3 blocking, all confirmed in code and all fixed before deploy:
(1) the deferral was UNREACHABLE for a `provisioning` job — the agent
exits 75 in Step 1 before emitting `phase=building`, so the common
ephemeral shape would have wedged with a live billing runner (fixed by
`reconcile_provisioning_lock_held`, deliberately narrow: it acts only on
the lock-held classification); (2) the re-dispatch had no lock-holder
verification, and the agent command's preamble runs `chown -R` +
`git fetch` + `git checkout --force -B` BEFORE the script reaches
`flock` while `portal-build.sh` writes the tracked `gdk-config.json`, so
a retry would have rewritten the tree the holder was building in — the
corruption mode `builds.md` prohibits (fixed by gating
`redispatch_lock_deferred` on the same pgrep verification the dedicated
path uses, fail-closed on an unverifiable check); (3)
`prior_command_proven_never_executed` returned True for the incident's
own evidence shape (terminal `Failed`/`Undeliverable` with no
`ExecutionStartDateTime` — command `1b538196`, which was executing and
holding the lock), authorizing a resend with no cancellation (now
INDETERMINATE). Also fixed: unreadable invocations no longer read as
absence (`read_invocation` state vocabulary); settled deferral commands
are excluded from the job-id lookup; the valve bounds deferral PRESSURE
using `effective_budget` rather than wall-clock on `max_runtime_ms`; a
`lock_deferral_watchdog` backstop bounds the requeued state itself; no
deferral exit terminates a runner before `lock_holder_build_absent`
pgrep-confirms no build remains; the claim condition rejects an attempt
map lacking `attempt_id` and reports WHY it refused; prior-command
adoption is recorded in `execution_attempt.adopted_command` instead of
overwriting identity in place. Issue 10 was adopted in narrowed form —
the marker text sizes the deferral budget (uncorroborated rc 75 gets one
cycle) but never gates the classification, so a genuine deferral whose
stdout was truncated or re-encoded can never be re-hard-failed.

**Test state at deploy.** Exploration 6/6 pass (was 6/6 FAIL pre-fix),
preservation 34/34 pass UNMODIFIED, new `test_exit75_deferral_regressions.py`
45/45 pass, collateral suites all at their task-2 baselines
(`test_execution_failure_preservation` 73, `test_build_reconciliation_unit`
89, `test_build_reconciliation_properties` 9, `test_run_as_ubuntu_unit` 26,
`test_source_selection_preservation` 35, `test_no_live_validation_contract`
10); whole `test/backend-test/portal_builds` 1220 passed with only the 8
known pre-existing failures (4 `test_build_diagnostic_api.py`, 4
`test_ref_aware_bootstrap_property.py`, both verified pre-existing in a
clean HEAD worktree). No preservation-tracked file touched, so no
security-baseline rebaseline; guard pair green (4 passed, 3 skipped).

**Rollout.** Committed as `d87a8d5` on `integration/all-specs` (pushed),
deployed with `deploy-infrastructure.sh` after confirming no component
build was running: EdgeCVPortalComputeStack and EdgeCVPortalBuildFleetStack
both UPDATE_COMPLETE (01:14:30Z / 01:19:07Z), BuildDispatcher and
BuildEvents lambdas modified 01:19:29Z. The deployed dispatcher package
was downloaded and confirmed to contain `CODE_BUILD_LOCK_HELD`,
`defer_lock_held_job`, `reconcile_provisioning_lock_held`,
`lock_deferral_watchdog`, `claim_execution_attempt`,
`cancel_command_confirmed` and `lock_holder_build_absent`. Regenerated
`cdk.out` moved aside; guard pair re-run green.

**Live verification (the deliberate lock race).** On the dedicated JP7
server `srv-af3e3e08` / `i-0345420de4f9cd214`, `/var/lock/dda-build.lock`
was held by a foreground, self-releasing SSM command (`flock ... sleep
900`, executionTimeout 1200) with NO build process running — so the
pre-dispatch pgrep verification passes and the agent genuinely reaches
its Step 1 `flock -n` and exits 75. Lock possession was confirmed by a
separate check that distinguishes contention from a permission error
(`LOCK=held_by_another`; an earlier detached attempt produced a false
`held` reading that was actually a permission error, and was discarded).

Build_Job `933a21f7-6432-4fea-b3cb-d2cfcaf2eea3` (JP7, dedicated,
`integration/all-specs`) then showed, live:

* **cycle 1** — dispatched to `building` (command
  `bf5e10ae-4988-4a21-ba13-eefdb7020176`), agent exited 75, and within
  ~30 s the dispatcher DEFERRED it: `status='queued'`, `deferred_at` set,
  `lock_deferral.count=1`, `marker_corroborated=true`, `cycle_budget=72`
  (the job's own 6 h ceiling / the 5-minute interval), `created_at`
  RETAINED (head of queue), `ssm.command_id` cleared, attempt settled
  `terminal` with `settled_as='lock_held_deferral'`,
  `execution_diagnostic.response_code=75` /
  `classification='BUILD_LOCK_HELD'`. **`error` was None and
  `terminal_effects` was absent** — no hard failure, no cleanup ledger,
  so no termination-watchdog eligibility. The dedicated **server
  allocation was KEPT** (Req 2.1's dedicated clause). Dispatcher log:
  "agent exit-75 lock-held deferral for command bf5e10ae... — requeued
  at the head of its queue"; the `build_deferred_lock_held` audit hook
  fired with no write failure.
* **cycle 2** — at the 5-minute re-verification the job re-dispatched to
  `building` (command `e316bc72-b533-4c2d-8af2-bc5bd053fd86`), hit the
  still-held lock, and deferred again: `count=2` of budget 72,
  `settled_command_ids` accumulating both commands (so neither can be
  re-attached by the ambiguous-send lookup), still no error, allocation
  still held. This is the bounded-retry behavior the agent's contract
  always specified.

Before this fix the SAME scenario produced a decided
`COMMAND_EXECUTION_FAILED` plus (for ephemeral) runner termination under
the live lock holder — the 2026-09-09 incident.

**Cleanup.** The verification job was cancelled while still `queued`
(so it could never turn into a real 1–2 h build), then the lock was
released by cancelling the holding SSM command. Post-release check on the
server: `NO_HOLDER`, `NO_GDK_BUILD`, `NO_CUSTOM_BUILD`, `LOCK=free`;
final job `status='cancelled'` with `error=None`, server allocation
released (`running_build_job_id=None`) and the server `running` and
available for real builds.

**Not covered live** (unchanged from the review's honest list): the
ephemeral `provisioning`-status route and the valve/stall exits were
exercised only in tests — the live race used a dedicated server, where
the dispatcher transitions to `building` before the send. The
`attribute_type(#att, 'NULL')` claim condition is validated against moto
here and against real DynamoDB only by the first ephemeral dispatch after
deploy (a rejected expression would surface as a `ValidationException`
and make the claim refuse rather than double-send, so it fails safe).
