# Bugfix Requirements Document

## Introduction

Two interacting defects in the portal build fleet backend hard-failed a
healthy AMD64 ephemeral build and terminated the runner out from under the
build that was actually running (live production incident, 2026-09-09,
Build_Job `851042a7-434f-4f8a-9fd4-79b25d100150`).

**Defect A — the agent's exit-75 deferral contract is not honored
post-dispatch.** `scripts/portal-build-agent.sh` takes a non-blocking flock
on `/var/lock/dda-build.lock` (lines 239–244: `exec 9>"$LOCK_FILE"` /
`if ! flock -n 9; then ... exit 75`) and its header contract states "Exits
75 (EX_TEMPFAIL) when the lock is held so the dispatcher can defer and
retry" (header line 36) and "75  build lock held by another build
(dispatcher should defer, Req 7.5/7.6)" (header line 60). The dispatcher's
PRE-dispatch path honors that contract: pgrep verification →
`build_planner.decide_predispatch` → `PREDISPATCH_DEFER`
(`build_planner.py` line 364) → the job stays queued at the head of its
queue with the original `created_at` retained and `deferred_at` recorded
(`build_dispatcher.py` lines 1963–1971). But POST-dispatch, the
classification in
`edge-cv-portal/backend/functions/build_reconciliation.py`
`classify_attempt` (def at line 649) has NO case for ResponseCode 75: in
the terminal-`Failed` branch (line 730), anything that is not preflight
evidence and not ENOSPC evidence falls through to
`Classification(decided=True, status=STATUS_FAILED,
error_code=CODE_COMMAND_EXECUTION_FAILED, authority=5,
reason='invocation Failed with non-zero response')` (lines 761–764). The
job hard-fails; because the terminal finalization plans the ledger with
`cleanup_required=True` for ephemeral jobs (`build_dispatcher.py`
`reconcile_running_command`, lines 2540–2543), the termination watchdog
(line 2880) then TERMINATES the instance — killing the concurrently
running build that held the lock. The evidence needed to defer is already
in the classification inputs: `build_execution_diagnostic` captures
`response_code` from the invocation's `ResponseCode` field
(`build_reconciliation.py` lines 861–873), and the agent prints the
deterministic stdout marker "Build lock /var/lock/dda-build.lock is held
by another build — deferring (exit 75)."

**Defect B — ambiguous/duplicate-send handling can double-execute the
agent on one instance.** For job 851042a7, TWO agent SSM commands were
sent 4 s apart with DIFFERENT attempt IDs: command `1b538196` (12:54:03,
attempt `88dcd6b8`) marked by SSM 'Failed/Undeliverable', rc −1, no
recorded execution start; then command `a13a0825` (12:54:07, attempt
`6c6b5483`) which executed 12:54:08–12:54:12 and exited 75 with the lock
stdout marker. The instance was a fresh ephemeral runner
(`i-089a77f72bc558147`, launched 12:50:49) whose only other SSM commands
were innocuous bootstrap-marker polls
(`test -f /var/log/dda-build-server-bootstrap.done`) — so the
'Undeliverable' command actually executed and held the lock while SSM
reported it undeliverable (an SSM delivery-status race on a
just-bootstrapped instance). Verified code facts behind the double send:

- The dispatcher `handler` (`build_dispatcher.py` line 3179) runs the SAME
  full tick for both the 1-minute schedule and the async on-submit
  dispatch invocation, so two tick executions can overlap.
- The ephemeral send loop's only "already dispatched" gate is an
  in-memory-snapshot check `if (job.get('ssm') or {}).get('command_id'):
  continue` (line ~2200), and the execution-attempt claim recorded before
  SendCommand (lines 2240–2250) is written with `update_job_fields` —
  documented "Unconditional SET" (line 1248) — NOT a conditional claim.
  Two overlapping ticks that both scanned before either persisted a
  command_id both mint a fresh attempt (`new_execution_attempt`, fresh
  uuid) and both SendCommand: exactly the observed two commands with
  different attempt IDs 4 s apart. The second write clobbers the first
  attempt record, orphaning command `1b538196`.
- The ambiguous-send recovery (`recover_ambiguous_send`, lines 2358–2440)
  can NEVER attach a prior attempt's command:
  `find_command_by_comment` (lines 2307–2326) requires
  `command.get('Comment') == comment` where the deterministic comment is
  `dda-build:<job-id>:<attempt-id>`
  (`build_reconciliation.command_comment`, line 1027) — the attempt id in
  the comment guarantees a different attempt's command never matches. And
  `claim_resend` (line 2329) conditions only on the CURRENT attempt's
  identity; nothing in the resend path cancels the prior command
  (`cancel_command` appears nowhere in
  `edge-cv-portal/backend/functions/`) or gates the new send on the prior
  command's terminal non-execution.

The two defects compound: Defect B put a genuine lock-holder on the
instance, Defect A classified the second command's honest exit-75 deferral
as a hard COMMAND_EXECUTION_FAILED, and the ephemeral cleanup then
terminated the instance while the (orphaned but live) first command's
build was running.

### Not this bug (prior history, do not confuse)

Job `01b18948` was a DIFFERENT exit-75 bug: a root-owned
`/var/lock/dda-build.lock` made `exec 9>` fail with 'Bad file descriptor',
producing an exit-75-forever loop with NO real build running; it is healed
by `build_dispatcher.lock_ownership_heal_command`. This spec's bug is a
GENUINE lock hold (a real concurrent build) plus wrong post-dispatch
classification and a duplicate send. The heal is out of scope and must
keep working unchanged.

### Explicitly out of scope

- InsufficientInstanceCapacity AZ/instance-type fallback (separate
  transient-capacity concern with a manual config workaround).
- Any change to the per-server lock model itself
  (`/var/lock/dda-build.lock`, `flock -n`, exit 75 in
  `scripts/portal-build-agent.sh`) — the agent side of the contract is
  correct and `test_source_selection_preservation.py` pins the script
  text.
- Weakening genuine `COMMAND_EXECUTION_FAILED` classification for
  non-75 response codes, or any change to `STABLE_ERROR_CODES` semantics
  for existing codes.

### Rollout

Portal-only (build fleet lambdas). No component build, no on-device
verification. Deploy via `edge-cv-portal/deploy-infrastructure.sh`
honoring the `.kiro/steering/builds.md` gates: never during a component
build, run the security guard suite first, move
`edge-cv-portal/infrastructure/cdk.out` aside after the deploy.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a dispatched agent SSM command reaches terminal invocation status
'Failed' with ResponseCode 75 (the agent's documented lock-held deferral)
and no terminal agent result THEN `classify_attempt` falls through its
terminal-`Failed` branch to `Classification(decided=True,
status=STATUS_FAILED, error_code=CODE_COMMAND_EXECUTION_FAILED,
authority=5)` (`build_reconciliation.py` lines 761–764) and the Build_Job
hard-fails, even though the agent contract says the dispatcher should
defer and retry

1.2 WHEN such an exit-75 classification settles an EPHEMERAL Build_Job
THEN `reconcile_running_command` plans the terminal ledger with
`cleanup_required=True` (`build_dispatcher.py` lines 2540–2543) and the
termination watchdog terminates the runner instance — killing the
concurrently running build that legitimately held
`/var/lock/dda-build.lock` (incident: `i-089a77f72bc558147` terminated
while command `1b538196`'s build was running)

1.3 WHEN two dispatcher tick executions overlap (the 1-minute schedule
plus the async on-submit invoke, both running the same full tick via
`handler`, line 3179) and both scan a provisioning ephemeral job before
either has persisted `ssm.command_id` THEN both pass the in-memory
`(job.get('ssm') or {}).get('command_id')` gate (~line 2200), both record
an execution attempt with an unconditional `update_job_fields` write
(lines 2240–2250), and both SendCommand — two agent executions for one job
on one instance (incident: commands `1b538196` and `a13a0825`, 4 s apart,
different attempt IDs)

1.4 WHEN the ambiguous-send recovery looks up a possibly-lost prior send
THEN `find_command_by_comment` (lines 2307–2326) matches only the FULL
deterministic comment `dda-build:<job-id>:<attempt-id>` of the CURRENT
attempt, so a prior attempt's command (different attempt id) can never be
found or attached, and the recovery proceeds as if no command exists

1.5 WHEN a conditional resend is issued after the visibility bound
(`recover_ambiguous_send`, lines 2398–2440) THEN the new SendCommand is
neither preceded by cancellation of the prior attempt's command nor gated
on evidence of that command's terminal non-execution — an
'Undeliverable'-reported command that actually executed keeps running
concurrently with the resend

### Expected Behavior (Correct)

2.1 WHEN a dispatched agent SSM command reaches terminal invocation status
'Failed' with ResponseCode 75 and no terminal agent result THEN
`classify_attempt` SHALL NOT decide a hard failure: the classification
SHALL identify the lock-held deferral (the ResponseCode-75 evidence is
already available to the diagnostic builder, lines 861–873) and route the
job back to the pre-dispatch deferral semantics — status returns to
queued at the head of its queue, the ORIGINAL `created_at` retained,
`deferred_at` recorded, and the server allocation kept for dedicated
servers (mirroring the `PREDISPATCH_DEFER` handling at
`build_dispatcher.py` lines 1963–1971)

2.2 WHEN an exit-75 deferral is recognized for an EPHEMERAL Build_Job THEN
the runner instance SHALL NOT be terminated while the lock-holder build
may be running: no `cleanup_required=True` terminal ledger is planned, no
termination-watchdog eligibility is created, and the next tick's
reconciliation/recovery re-verifies — attaching to the surviving command
or re-running the agent once the lock frees

2.3 WHEN a dispatcher tick is about to SendCommand the agent for a job
THEN the execution-attempt claim SHALL be a CONDITIONAL write that exactly
one concurrent tick execution can win (the loser does not send), so at
most one agent command per job dispatch can ever be in flight from the
dispatch loop

2.4 WHEN the ambiguous-send recovery inspects recent commands for a job
THEN the lookup SHALL be able to find a prior attempt's command by job id
(parsing the `dda-build:<job-id>:<attempt-id>` comment prefix rather than
requiring full-comment equality), and an existing command for the SAME job
SHALL be attached rather than treated as never-executed

2.5 WHEN a resend for a job would be issued while a prior command for that
job is not provably terminal-without-execution THEN the resend SHALL be
gated: either the prior command is cancelled first (and the cancellation
confirmed) or the resend waits until the prior command's evidence is
terminal — never two concurrently live agent commands for one job on one
instance

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a terminal 'Failed' invocation carries a non-75 response code and
no preflight or ENOSPC evidence THEN `classify_attempt` SHALL CONTINUE TO
return decided `STATUS_FAILED` / `CODE_COMMAND_EXECUTION_FAILED` at
authority 5 — genuine command-execution failures still hard-fail

3.2 WHEN a terminal invocation is 'TimedOut' or 'Cancelled', or carries
preflight-failure or ENOSPC evidence, THEN `classify_attempt` SHALL
CONTINUE TO produce `CODE_COMMAND_TIMED_OUT` /
`CODE_COMMAND_CANCELLED` / `CODE_COMMAND_PREFLIGHT_FAILED` /
`CODE_RUNNER_DISK_FULL` exactly as today, and the authority ordering 1–7
of the precedence table SHALL be preserved (agent result > user
cancellation > hard ceiling > infrastructure loss > invocation evidence >
settlement > unavailable)

3.3 WHEN a correlated terminal agent result exists (succeeded or failed)
THEN it SHALL CONTINUE TO win at authority 1 regardless of the
invocation's response code — an agent-reported real failure on a job
whose invocation also shows rc 75 is still agent-authoritative

3.4 WHEN the PRE-dispatch pgrep verification finds a build process on a
dedicated server THEN the `PREDISPATCH_DEFER` path SHALL CONTINUE TO
requeue at the head of the queue with the original `created_at`,
`deferred_at` recorded, and the allocation kept (lines 1963–1971)

3.5 WHEN an ambiguous send is recovered and the CURRENT attempt's command
is found by its deterministic comment THEN it SHALL CONTINUE TO be
attached without a resend, and `claim_resend`'s conditional
one-writer-wins semantics (line 2329) SHALL CONTINUE TO reject every
concurrent or retried claimant

3.6 WHEN a genuinely terminal ephemeral Build_Job settles (real failure,
success, interruption) THEN the termination watchdog SHALL CONTINUE TO
terminate the runner within its 10-minute target with the existing retry
and orphan-notification behavior (Req 3.2/3.9 of
build-fleet-execution-failures)

3.7 WHEN the agent script runs THEN `scripts/portal-build-agent.sh` SHALL
CONTINUE TO acquire `/var/lock/dda-build.lock` with `flock -n` on FD 9 and
exit 75 with the deterministic stdout marker when held — the script text
pinned by `test_source_selection_preservation.py` is not modified

3.8 WHEN a root-owned lock file produces the 'Bad file descriptor'
exit-75-forever loop (the job 01b18948 pattern) THEN
`build_dispatcher.lock_ownership_heal_command` SHALL CONTINUE TO heal it —
the deferral handling for a genuine lock hold must not mask or break the
ownership heal

3.9 WHEN `STABLE_ERROR_CODES` consumers (UI error rendering, audit
entries, diagnostics) read a settled job THEN every EXISTING code SHALL
CONTINUE TO carry its current meaning; any new deferral outcome is
additive and never re-uses an existing code with changed semantics

3.10 WHEN evidence arrives in any order or duplicated (EventBridge vs
scheduled reconciliation) THEN classification SHALL CONTINUE TO be
deterministic and idempotent per `apply_evidence` — the fix must not make
outcomes order-dependent

### Deriving the Bug Condition

The input is the settled evidence for one dispatched execution attempt:

```pascal
RECORD AttemptEvidence
  invocation_status   : string   // SSM: Failed | TimedOut | Cancelled | Success | ...
  response_code       : integer  // SSM invocation ResponseCode
  agent_result        : record?  // correlated terminal agent result, or NIL
  execution_mode      : string   // ephemeral | dedicated
  preflight_evidence  : boolean  // PREFLIGHT_FAILURE_MARKER in invocation text
  enospc_evidence     : boolean  // ENOSPC evidence in invocation text
END RECORD

FUNCTION isBugCondition(X)
  INPUT: X of type AttemptEvidence
  OUTPUT: boolean

  // Defect A: an honest agent lock-held deferral reaching the
  // terminal-Failed classification branch with no agent authority.
  RETURN X.invocation_status = 'Failed'
     AND X.response_code = 75
     AND X.agent_result = NIL
     AND NOT X.preflight_evidence
     AND NOT X.enospc_evidence
END FUNCTION
```

**Fix Checking** — for all buggy inputs the fixed classifier defers:

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  result ← classify_attempt'(X)
  ASSERT NOT (result.decided AND result.status = STATUS_FAILED)
  ASSERT result.error_code ≠ CODE_COMMAND_EXECUTION_FAILED
  // and the settlement path consequently:
  ASSERT job_requeued_at_head(X) AND deferred_at_recorded(X)
  ASSERT X.execution_mode = 'ephemeral' IMPLIES NOT instance_terminated(X)
END FOR
```

**Secondary bug condition (Defect B)** — duplicate-send exposure:

```pascal
FUNCTION isDoubleSendCondition(Y)
  INPUT: Y of type DispatchState
  OUTPUT: boolean

  // A prior attempt exists in sending/sent whose command is
  // Undeliverable-but-possibly-executed, and the recovery/dispatch
  // path issues a NEW command without finding, cancelling, or
  // terminally excluding the prior one.
  RETURN Y.prior_attempt_exists
     AND Y.prior_command_status IN ('Undeliverable', 'Failed-no-execution-evidence')
     AND Y.new_send_issued
     AND NOT (Y.prior_command_found_by_job_id
              OR Y.prior_command_cancelled
              OR Y.prior_command_proven_never_executed)
END FUNCTION

FOR ALL Y WHERE isDoubleSendCondition possible DO
  ASSERT find_command_by_job'(Y) finds the prior attempt's command  // 2.4
  ASSERT concurrent_live_agent_commands(Y) ≤ 1                      // 2.3, 2.5
END FOR
```

**Preservation** — for all non-buggy inputs the fixed code is identical:

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT classify_attempt(X) = classify_attempt'(X)   // decided, status,
                                                      // error_code, authority
END FOR
```
