# Bug-Condition Exploration Counterexamples — build-agent-exit75-deferral (Task 1)

**Property 1: Bug Condition** — Exit-75 deferral is hard-failed
post-dispatch and the duplicate-send path can double-execute.

Source: `test/backend-test/portal_builds/test_exit75_deferral_exploration.py`,
run against UNFIXED code with

```
HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test \
    ~/.dda-test-venv/bin/python -m pytest \
    test/backend-test/portal_builds/test_exit75_deferral_exploration.py \
    --noconftest -q -p no:cacheprovider
```

Result: **6 failed, 0 passed** — every bug-condition test failed as the
task requires. Both defects of the 2026-09-09 incident (Build_Job
`851042a7-434f-4f8a-9fd4-79b25d100150`, runner `i-089a77f72bc558147`)
are reproduced; the root-cause analysis in bugfix.md is CONFIRMED, not
refuted.

All test data is local/mocked (moto DynamoDB, recording SSM fake over
`boto3.client`, `send_agent` stubbed at every would-be send). No live
AWS resource was read or mutated, no SSM command sent, no instance
touched, no build started, nothing deployed.

---

## Part A — classification (Defect A, Req 1.1)

### A.1 Verbatim incident case
`TestPartAClassification::test_incident_verbatim_exit75_invocation_is_not_hard_failed`

Fixture: the incident's second command `a13a0825` (attempt `6c6b5483`),
terminal invocation `Status='Failed'`, `ResponseCode=75`,
`agent_result=None`, stdout carrying the agent's deterministic marker
`'Build lock /var/lock/dda-build.lock is held by another build —
deferring (exit 75).'`, no preflight/ENOSPC evidence.

Observed unfixed classification (verbatim):

```
Classification(decided=True, status='failed',
               error_code='COMMAND_EXECUTION_FAILED', authority=5,
               reason='invocation Failed with non-zero response')
```

— the honest exit-75 deferral falls through `classify_attempt`'s
terminal-`Failed` branch (`build_reconciliation.py` lines 761–764) to a
decided hard failure. There is NO ResponseCode-75 case anywhere in the
classifier.

### A.2 Hypothesis property — shrunk falsifying example (verbatim)
`TestPartAClassification::test_property_rc75_terminal_failed_is_never_command_execution_failed`

Generated (ci profile, 100 examples): rc in {75, "75"}, stdout = noise +
lock marker + noise, arbitrary non-preflight/non-ENOSPC stderr /
StatusDetails text, current status building/provisioning. Shrunk
falsifying example (Hypothesis, verbatim):

```
Failing test case: test_property_rc75_terminal_failed_is_never_command_execution_failed(
    response_code=75,
    noise_before='',
    noise_after='',
    stderr='',
    status_details='Failed',
    current_status='building',
)
```

i.e. the MINIMAL case — a bare terminal `Failed` invocation with
`ResponseCode=75` and the lock marker — classifies as
`(decided=True, status='failed', error_code='COMMAND_EXECUTION_FAILED',
authority=5)`. The hard-fail is unconditional: no amount or shape of
surrounding text changes it, and the numeric-string `"75"` variant
behaves identically.

## Part B — settlement side effect (Defect A, Req 1.1/1.2)

### B.1 Ephemeral rc-75 settlement plans runner termination
`TestPartBSettlement::test_rc75_settlement_defers_and_never_plans_runner_termination`

Fixture: the incident job (EPHEMERAL, `building`, runner
`i-089a77f72bc558147`) with correlated `ssm.command_id=a13a0825` and
execution attempt `6c6b5483`; scripted final `GetCommandInvocation` =
`Failed`/rc 75/lock marker; one full dispatcher tick
(`build_dispatcher.run_tick`), which routes through
`reconcile_running_command`.

Observed unfixed output (violated fixed-predicate clauses, verbatim):

- hard-failed: status='failed' error={'code':
  'COMMAND_EXECUTION_FAILED', 'message': "The build agent SSM command
  ended with status 'Failed' before reporting a build result:
  invocation Failed with non-zero response. Retained command evidence
  was recorded in the execution diagnostic."} — the honest exit-75
  deferral became a terminal COMMAND_EXECUTION_FAILED
- cleanup_required ledger planned:
  `terminal_effects.compute_cleanup='pending'` — the ephemeral runner
  i-089a77f72bc558147 is termination-watchdog eligible while the
  lock-holder build (command 1b538196) is still running
- not requeued: status='failed' (expected 'queued' at the head of its
  queue, mirroring PREDISPATCH_DEFER)
- deferred_at was not recorded

(`created_at` retention was the only clause NOT violated — the failed
record keeps its timestamp, but the job is terminal so head-of-queue
position is moot.) This is exactly the incident path: hard failure →
`cleanup_required=True` ledger (`build_dispatcher.py` lines 2540–2543)
→ termination watchdog kills the instance under the running lock-holder
build.

## Part C — recovery lookup and single-live-command (Defect B, Req 1.3/1.4/1.5)

### C.1 Prior attempt's command is invisible to the recovery lookup
`TestPartCRecoveryAndSingleLiveCommand::test_find_command_by_comment_finds_prior_attempt_by_job_id`

Fixture: mocked `list_commands` containing the incident's first command
`1b538196` with comment `dda-build:851042a7-…:88dcd6b8`; lookup invoked
with the CURRENT attempt's comment `dda-build:851042a7-…:6c6b5483`.

Observed unfixed output (verbatim):

```
observed lookup result: None
```

— `find_command_by_comment` (lines 2307–2326) requires FULL comment
equality; the attempt id embedded in the comment guarantees a prior
attempt's command for the SAME job can never be found or attached, so
recovery proceeds as if no command exists.

### C.2 Overlapping ticks double-send (unconditional attempt claim)
`TestPartCRecoveryAndSingleLiveCommand::test_overlapping_tick_cannot_mint_a_second_live_command`

Fixture: tick A's claim already persisted (attempt `88dcd6b8`, command
`1b538196`, `ssm.command_id` set); tick B runs the ephemeral send loop
(`provision_ephemeral`) on the STALE pre-claim snapshot it scanned
before tick A's writes — the incident's 4-second overlap window
(scheduled tick + async on-submit invoke, `handler` line 3179).

Observed unfixed output (violated fixed-predicate clauses, verbatim):

- a SECOND agent SendCommand was issued for the same job on
  i-089a77f72bc558147 (1 send(s)) — the incident's commands
  1b538196/a13a0825 4 s apart; the losing tick must skip the send
- the first tick's attempt claim was clobbered by an unconditional
  write: persisted attempt_id='5e121f84-8ae6-4679-a108-6d00373b13d1'
  (expected the prior attempt '88dcd6b8'); command 1b538196 is now
  orphaned
- ssm.command_id was rewritten to 'a13a0825' (expected '1b538196')

(the fresh-uuid attempt id varies per run — `new_execution_attempt`
mints one per passing tick; the clobbering is the invariant.) The
in-memory `ssm.command_id` gate (~line 2200) plus the unconditional
`update_job_fields` SET (lines 2240–2250) let both ticks send: exactly
the observed two commands with different attempt IDs.

### C.3 Recovery resends while the prior command is live
`TestPartCRecoveryAndSingleLiveCommand::test_recovery_never_resends_while_prior_command_may_be_live`

Fixture: attempt `6c6b5483` stuck in `sending` past
`AMBIGUOUS_SEND_VISIBILITY_MS`; recent commands show the prior
attempt's `1b538196` as SSM-reported 'Failed/Undeliverable' while its
invocation carries an `ExecutionStartDateTime` (it actually executed
and holds the build lock — the incident's delivery-status race on a
just-bootstrapped instance).

Observed unfixed output (verbatim):

```
prior command '1b538196': reported 'Failed/Undeliverable' but actually
    executing (ExecutionStartDateTime present), cancelled=False, attached=False
resend issued: True (1 send(s), new command 'cmd-conditional-resend')
concurrently live agent commands for job
    851042a7-434f-4f8a-9fd4-79b25d100150:
    ['1b538196', 'cmd-conditional-resend'] (must be <= 1)
```

— `recover_ambiguous_send` (lines 2358–2440) cannot see the prior
command (C.1), never cancels it (`cancel_command` appears nowhere in
`edge-cv-portal/backend/functions/`), and gates nothing on its terminal
non-execution: two concurrently live agent commands for one job on one
instance.

## Run summary (unfixed baseline)

```
FAILED ...::TestPartAClassification::test_incident_verbatim_exit75_invocation_is_not_hard_failed
FAILED ...::TestPartAClassification::test_property_rc75_terminal_failed_is_never_command_execution_failed
FAILED ...::TestPartBSettlement::test_rc75_settlement_defers_and_never_plans_runner_termination
FAILED ...::TestPartCRecoveryAndSingleLiveCommand::test_find_command_by_comment_finds_prior_attempt_by_job_id
FAILED ...::TestPartCRecoveryAndSingleLiveCommand::test_overlapping_tick_cannot_mint_a_second_live_command
FAILED ...::TestPartCRecoveryAndSingleLiveCommand::test_recovery_never_resends_while_prior_command_may_be_live
6 failed in 6.14s
```

These failures ARE the task-1 deliverable: they freeze both bug
conditions (`isBugCondition(X)` and `isDoubleSendCondition(Y)` from
bugfix.md). Do not weaken the assertions; task 3.5 re-runs this exact
file unchanged after the fixes land, where all 6 tests must then PASS.

---

# Post-Fix Outcome (Task 3.5 / 3.6)

The SAME suite, UNMODIFIED (md5 `187d13a0f002d8d7c6c8b3f67953a415`),
re-run after tasks 3.1–3.4 landed:

```
HYPOTHESIS_PROFILE=ci PYTHONPATH=src/backend:test/backend-test \
    ~/.dda-test-venv/bin/python -m pytest \
    test/backend-test/portal_builds/test_exit75_deferral_exploration.py \
    --noconftest -q -p no:cacheprovider
6 passed in 1.91s
```

**6 passed, 0 failed** — every counterexample above is now resolved:
Parts A, B and C all hold. The preservation oracle was re-run unchanged
(md5 `dbde68aed67d747cfb8358914978a8f7`) and is still green:

```
test/backend-test/portal_builds/test_exit75_deferral_preservation.py
34 passed in 1.52s
```

## What each counterexample now yields

| Counterexample | Fixed behavior |
| --- | --- |
| A.1 / A.2 (rc 75 or `"75"` hard-failed) | `classify_attempt` returns `Classification(decided=False, status=<unchanged>, error_code='BUILD_LOCK_HELD', authority=5, reason='invocation Failed with the agent lock-held deferral code (exit 75); dispatcher defers and retries')` — never decided-failed, never `COMMAND_EXECUTION_FAILED`. The case sits AFTER the preflight and ENOSPC evidence checks and BEFORE the generic fallthrough, and keys on the ResponseCode ONLY (the lock stdout marker with any other code still hard-fails). |
| B.1 (ephemeral settlement planned `cleanup_required` + terminal failure) | `reconcile_running_command` routes the deferral to `defer_lock_held_job`: `status='queued'`, ORIGINAL `created_at` retained, `deferred_at` recorded, `lock_deferral` counter written, `ssm.command_id` cleared, attempt settled `terminal`. NO `plan_job_ledger`, NO `terminal_effects`, NO `fail_job` — so `terminal_effects.compute_cleanup` is absent and the termination watchdog (which only sees terminal ephemeral jobs) can never terminate the runner while the lock-holder build runs. A `build_deferred_lock_held` audit entry records the deferral. |
| C.1 (prior attempt's command invisible) | `find_command_by_comment` now matches every command whose comment PARSES to the same JOB id via `parse_command_comment`, preferring the current attempt's exact match, then the most recent non-terminal match, then the most recent match; it returns a `CommandMatch` carrying the found command's id, attempt id, comment and ListCommands status. The incident's `1b538196` (`dda-build:851042a7-…:88dcd6b8`) is found from the current attempt's `…:6c6b5483` comment. |
| C.2 (overlapping ticks both send) | The ephemeral pre-SendCommand claim is now the conditional `claim_execution_attempt` (`attribute_not_exists(execution_attempt.attempt_id)`, or equality on a SETTLED prior attempt) — the stale-snapshot tick loses and skips the send silently; attempt `88dcd6b8` and command `1b538196` survive unclobbered. The dedicated site records its claim INSIDE the conditional `queued -> building` transition, which is already the one-writer-wins arbiter there. |
| C.3 (resend over a live prior command) | Past `AMBIGUOUS_SEND_VISIBILITY_MS`, a prior attempt's delivery-terminal command is resend-eligible only when `prior_command_proven_never_executed` is True (terminal delivery status on BOTH reads AND no `ExecutionStartDateTime`) or `cancel_command_confirmed` succeeded. The incident's `1b538196` (reported `Failed/Undeliverable`, `ExecutionStartDateTime` present) is ATTACHED instead — zero sends, one live command. |

## Collateral suites (task-2 baseline, unchanged)

| Suite | Baseline | Post-fix |
| --- | --- | --- |
| `test_execution_failure_preservation.py` | 73 | 73 passed |
| `test_build_reconciliation_unit.py` | 89 | 89 passed |
| `test_build_reconciliation_properties.py` | 9 | 9 passed |
| `test_run_as_ubuntu_unit.py` | 26 | 26 passed |
| `test_source_selection_preservation.py` | 35 | 35 passed |

No collateral test was modified.

Whole-directory sweep (`test/backend-test/portal_builds`):
**8 failed, 1175 passed, 1 skipped**. The 8 failures are the
PRE-EXISTING ones on the unfixed tree — verified by running the same
sweep in a clean `HEAD` worktree: **8 failed, 1135 passed, 1 skipped**
(the same 4 `test_build_diagnostic_api.py` and 4
`test_ref_aware_bootstrap_property.py` tests; the +40 passes are this
spec's two new suites). Nothing in this fix changed that set.

## Design-review correction round (semantic-review/2026-09-09-222949-pr-local.md)

The design review of the task-3 implementation found 13 issues (3
blocking). Twelve are fixed in `build_dispatcher.py` /
`build_reconciliation.py`; issue 10 was adopted in a narrowed form.
The two suites above are UNCHANGED (6 and 34 passed). The new
`test_exit75_deferral_regressions.py` (45 passed) covers the paths they
never reached.

| Review issue | Resolution |
| --- | --- |
| 1 (blocking) — deferral unreachable in `provisioning` | `command_reconciliation` gains a NARROW `provisioning`+ephemeral route (`reconcile_provisioning_lock_held`) that acts ONLY on the lock-held classification. The agent exits 75 in Step 1, before `phase=building`, so this is the common shape; it used to wedge with `ssm.command_id` set and no watchdog. |
| 2 (blocking) — re-dispatch could corrupt the holder's tree | `redispatch_lock_deferred` runs `VERIFY_BUILD_PROCESS_COMMANDS` through `decide_predispatch` (plus the root-side lock-ownership heal) before any send, and fails closed on an unverifiable check. The command preamble's `git checkout --force` runs before the agent reaches `flock`. |
| 3 (blocking) — "proven never executed" cleared the incident shape | A terminal invocation with no `ExecutionStartDateTime` is now INDETERMINATE (`None`) → cancel-and-confirm. Only a positively ABSENT invocation plus a delivery-terminal ListCommands status returns True. |
| 4 — resend gate failed open on SSM errors | New `read_invocation` distinguishes RETRIEVED / ABSENT / UNREADABLE; `cancel_command_confirmed` waits on UNREADABLE instead of authorizing a send. |
| 5 — stale prior-command attach | `lock_deferral.settled_command_ids` (+ `last_command_id`) now feed `find_command_by_comment(exclude_command_ids=...)`. |
| 6 — no external backstop | New tick step 3.5 `lock_deferral_watchdog`: a deadline on the requeued state's own activity clock (`LOCK_DEFERRAL_STALL_MS`, 30 min). |
| 7 — wrong budget and clock | `lock_deferral_window_ms` uses `build_reconciliation.effective_budget(job).hard_runtime_ms`; the valve bounds deferral CYCLES (`lock_deferral_cycle_budget`), not wall-clock. |
| 8 — valve exit re-created the harm | Every exit from the deferral passes `lock_holder_build_absent` (instance state + pgrep, fail closed) before a terminal write; otherwise `hold_lock_deferral_exit` keeps the job nonterminal. |
| 9 — `terminated_at` treated as liveness | `runner_instance_state` / `runner_instance_alive`; a gone runner falls through to replacement compute. |
| 10 — marker alongside rc 75 | Adopted as a PRESSURE bound, not a classification gate: `is_lock_held_marker_evidence` corroboration sizes the budget (1 cycle when uncorroborated). Classification still keys on rc 75 alone, so a genuine deferral with truncated/re-encoded stdout is never re-hard-failed. |
| 11 — claim condition edge | `attribute_not_exists(#att) OR attribute_type(#att, NULL)`; the claim returns CLAIM_WON / CLAIM_LIVE_ATTEMPT / CLAIM_LOST so the log names the real reason. |
| 12 — attach rewrote identity in place | `execution_attempt.adopted_command` records the adoption (and the superseded dispatch's clocks); the live `claimed_at`/`sending_at` no longer describe a different dispatch. |
