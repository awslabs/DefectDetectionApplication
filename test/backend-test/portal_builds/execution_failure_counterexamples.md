# Bug-Condition Exploration Counterexamples — build-fleet-execution-failures (Task 1)

**Property 1: Bug Condition** — Terminal SSM evidence recovery and
runtime-evidence sufficiency.

Source: `test/backend-test/portal_builds/test_execution_failure_exploration.py`,
run against UNFIXED code with

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
    test/backend-test/portal_builds/test_execution_failure_exploration.py \
    --noconftest -q
```

Result: **10 failed, 2 passed** — every bug-condition test failed as the
task requires; the two passing tests are the strict-boundary
preservation checks (`now == deadline` is not expired, Req 3.12), which
must already hold on unfixed code and do.

All test data is local/mocked (moto DynamoDB/CloudWatch, recording SSM
fake, temp filesystem). No live AWS resource was read or mutated, no SSM
command sent, no instance touched, no build started. All secret canaries
were unique synthetic per-test values and are REDACTED below.

---

## Facet A — `commandEvidenceLost` (Req 1.1, 1.2, 1.3, 1.4, 1.5, 1.10, 1.11)

### A.1 Incident-shaped deterministic case
`TestTerminalSsmEvidenceRecovery::test_incident_dedicated_amd64_failed_before_callback`

Fixture: dedicated AMD64 job `06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03` in
`building` on server `srv-5b214096-91a9-41b7-9d62-cc03ba205c15` (modeled
instance id), correlated `ssm.command_id` + `execution_attempt`, no
terminal agent callback. Scripted final `GetCommandInvocation`:
`Status=Failed`, `StatusDetails='Failed'`, `ResponseCode=127`,
stdout available-empty, stderr present naming
`bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh: No such file or directory`
plus [CANARY-REDACTED] secret values. Real SSM EventBridge failure shape
delivered through `build_events.handler`; CloudWatch stream missing for
`build_jobs.get_build_logs`.

Observed unfixed output (failed fixed-predicate clauses, verbatim):

- final GetCommandInvocation evidence was NEVER retrieved
  (build_events.py consumes only the event's command-id and status;
  SSM_GET_CALLS is empty for this command)
- no execution diagnostic was persisted on the Build_Job (terminal
  StatusDetails / ResponseCode / stdout / stderr and identities were
  discarded)
- outcome collapsed into generic/unstable code 'AGENT_COMMAND_FAILED'
  (expected one of ['COMMAND_EXECUTION_FAILED'])
- Build Log API returned ONLY an empty page
  (`{"events": [], "nextToken": null}`) although useful terminal
  evidence exists — the portal can render nothing but
  'No log output was recorded for this build job.'

Observed job error (unfixed):
`{"code": "AGENT_COMMAND_FAILED", "message": "The build agent SSM command ended with status 'Failed' before reporting a build result."}`
— exactly the incident's Build Details text.

Redaction note: no canary leaked into any surface on unfixed code —
because the evidence is discarded entirely, not because a redaction
contract exists (Req 1.4 remains unimplemented; the same assertions
must keep holding after the fix once evidence IS persisted).

### A.2 Hypothesis property — shrunk falsifying example
`TestTerminalSsmEvidenceRecovery::test_property_generated_invocation_evidence_is_preserved`

Generated: `response_code` 1–255, `StatusDetails` variants,
stdout/stderr each present / available-empty / unavailable, Unicode
noise, optional canary in status details; at least one useful field
always present. Shrunk falsifying example (Hypothesis):

```
response_code=1,
status_details='Failed',
stdout_kind='present',
stderr_kind='present',
output_noise='',
secret_in_details=False,
```

i.e. the MINIMAL case — any Failed invocation with any useful output —
loses all evidence: same four failed clauses as A.1. This proves the
loss is unconditional, not specific to exit 127 or missing streams.

## Facet A — Repository-path contract (Req 1.9)

### A.3 Deterministic contract case
`TestRepositoryPathContract::test_preflight_selects_registered_clone_and_finds_script`

Modeled in a temp filesystem: registered fleet clone
`<tmp>/home/ubuntu/DefectDetectionApplication` carrying
`scripts/portal-build-agent.sh` (executable), and the dispatcher's
historical default `<tmp>/opt/dda/DefectDetectionApplication` existing
but WITHOUT the script — the live-inspection shape behind the exit-127
evidence. Legacy server record with no persisted `repo_dir`; dispatcher
`BUILD_REPO_DIR` pinned to the `/opt/dda` path.

Observed unfixed mismatch (verbatim, temp prefix elided):

```
dispatcher effective repo dir : .../opt/dda/DefectDetectionApplication
effective agent script        : .../opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh
effective script exists       : False
registered clone (has script) : .../home/ubuntu/DefectDetectionApplication
registered script exists      : True
preflight seam in dispatcher  : False
```

No preflight exists in `build_dispatcher.py`; an invalid path/script
contract reaches the costly SSM command and fails only afterwards.
This proves the CONTRACT defect only — historical causation for the
2026-08-06 job is task 3's read-only evidence question.

## Facet B — `runtimeEvidenceInsufficient` (Req 1.13–1.18)

All cases drive `build_planner.decide_runtime_timeout(job, now)` with
explicitly snapshotted per-job budgets
(`runtime_budgets.AMD64.dedicated = {heartbeat_lease_minutes: 30,
progress_stall_minutes: 60, hard_runtime_hours: 8}`) plus legacy
`max_runtime_hours: 4`. No production timeout value is asserted or
changed.

| Case | Fixture | Fixed expectation | Observed unfixed decision |
|---|---|---|---|
| (a) active, fresh progress, below hard ceiling | building 5h, heartbeat 1min old, progress 2min old, hard 8h | CONTINUE | `timed_out=True`, `error='Build_Job exceeded its maximum runtime of 4 hours (timeout).'` — wall clock alone; progress/hard budget never read |
| (a-boundary, PRESERVATION) | `now == started_at + 4h` | not expired | not expired (PASSES unfixed, Req 3.12) |
| (b) heartbeat-fresh, progress-stalled | building 3h, heartbeat 1min old, progress 2h old (>60min budget) | `BUILD_PROGRESS_STALLED` | `timed_out=False`, `error=None` — stall invisible |
| (c) stale heartbeat | building 3h, heartbeat 2h old (>30min lease) | `AGENT_HEARTBEAT_EXPIRED` | `timed_out=False`, `error=None` — hung agent invisible until 4h wall clock |
| (d) queued 6h behind occupied server | status queued, created 6h ago | stay queued + evidence `{phase: queue_wait, queue_wait_ms: 21600000, execution_runtime_ms: 0}` | not failed (correct) but decision fields are only `(timed_out, build_job_id, status, error)` — NO phase/duration/budget evidence |
| (e) provisioning 45min, no explicit budget | status provisioning | no timeout + evidence `{phase: provisioning, provisioning_ms: 2700000, execution_runtime_ms: 0}` | not failed (correct) but zero provisioning accounting exposed |
| (f-boundary, PRESERVATION) | `now == execution start + hard budget` | not expired | not expired (PASSES unfixed, Req 3.12) |
| (f) hard-ceiling expiry +1ms | building, last activity 1h before deadline | `MAX_RUNTIME_EXCEEDED` + evidence (phase, budget+source, observed_ms, last heartbeat/progress, target/mode) | `timed_out=True` but only the prose string `'Build_Job exceeded its maximum runtime of 4 hours (timeout).'` — no stable code, no evidence fields |
| (f-anchor) exact-deadline counterexample | `started_at=T0`, `timing.execution_started_at=T0+30min`, `now=T0+4h+1ms` (active execution 3h30m, fresh progress) | NOT expired | `timed_out=True` — expiry anchored on `started_at` |

### Recorded anchor finding (Req 1.13, task bullet)

- `started_at` (recorded at the `building` transition) is the ONLY
  anchor `decide_runtime_timeout` uses, and it is INSUFFICIENT: it
  precedes positive execution-start evidence, so dispatch/setup gaps
  are charged to active runtime (case f-anchor fails a job 30 minutes
  early).
- `created_at` and `dispatched_at` are never consulted; queue wait and
  provisioning durations are not measured or exposed anywhere
  (cases d, e).
- No heartbeat, progress, soft/hard, or target/mode budget input exists
  in the decision (cases a, b, c, f).
- Consequence for the user-observed maximum-runtime failure: the
  retained decision output cannot distinguish active progress, stall,
  heartbeat loss, queue wait, provisioning, or hard-ceiling expiry —
  so no timeout extension or queueing remedy can be justified from the
  failure label alone (Req 2.13). No longer timeout is asserted by the
  exploration tests.

## Run summary (unfixed baseline)

```
FAILED ...::TestTerminalSsmEvidenceRecovery::test_incident_dedicated_amd64_failed_before_callback
FAILED ...::TestTerminalSsmEvidenceRecovery::test_property_generated_invocation_evidence_is_preserved
FAILED ...::TestRepositoryPathContract::test_preflight_selects_registered_clone_and_finds_script
FAILED ...::TestRuntimeEvidenceSufficiency::test_a_active_fresh_progress_below_hard_ceiling_continues
FAILED ...::TestRuntimeEvidenceSufficiency::test_b_progress_stall_is_classified
FAILED ...::TestRuntimeEvidenceSufficiency::test_c_stale_heartbeat_is_classified
FAILED ...::TestRuntimeEvidenceSufficiency::test_d_queue_wait_is_accounted_separately
FAILED ...::TestRuntimeEvidenceSufficiency::test_e_provisioning_time_is_isolated
FAILED ...::TestRuntimeEvidenceSufficiency::test_f_hard_ceiling_expiry_is_classified_with_evidence
FAILED ...::TestRuntimeEvidenceSufficiency::test_f_anchor_counterexample_execution_start_not_started_at
10 failed, 2 passed
```

These failures ARE the task-1 deliverable: they freeze the bug
condition. Do not weaken the assertions; task 10 re-runs this exact
file after the fixes land, where all 12 tests must pass.

---

# STORAGE AMENDMENT — Bug-Condition Exploration Counterexamples (Task 14)

**Property 17: Bug Condition** — ENOSPC evidence collapses into generic
failure with no disk evidence.
**Property 18: Bug Condition** — Head-keeping truncation drops the
trailing root cause.

Source: `test/backend-test/portal_builds/test_storage_exhaustion_exploration.py`,
run against UNFIXED code with

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest \
    test/backend-test/portal_builds/test_storage_exhaustion_exploration.py \
    --noconftest -q
```

Result: **6 failed, 0 passed** — every storage-facet bug-condition test
failed as task 14 requires. The frozen task-1 and task-2 baselines were
NOT modified. All test data is local/mocked (moto DynamoDB, recording
SSM fake, temp-dir fixture logs, subprocess bash for the agent's
error-tail pipeline). No live AWS resource, SSM command, instance
action, deployment, or build. No fixture contains a secret-shaped
value; nothing here required redaction.

Incident modeled: ephemeral JP6 job `bd91c5d8-ac7e-4125-becc-711860660f2e`
(historical-evidence.md §2.3): 100 GB single-volume snap-docker runner,
both JP6 image exports hit ENOSPC during final layer extraction, agent
log tee hit ENOSPC, `gdk` exited 1, normal failed callback; durable
error = generic `BUILD_FAILED` cut mid-path at
`write /var/snap/docker/common/`.

## Facet (a) — ENOSPC misclassification (Req 1.19, 1.20, 1.22)

### 14.a1 Incident-shaped agent callback
`TestEnospcMisclassification::test_a1_incident_agent_callback_enospc_collapses_to_generic`

Fixture: job `bd91c5d8-…` in `building`; agent failed callback whose
`error_message` carries the over-length buildkit ENOSPC line plus the
retained agent-tee ENOSPC line, delivered through
`build_events.handler` (phase path, `apply_phase_event`).

Observed unfixed output (failed fixed-predicate clauses, verbatim):

- ENOSPC-bearing agent output collapsed into generic `'BUILD_FAILED'`
  instead of the stable `'RUNNER_DISK_FULL'` classification
  (`enospcEvidenceClassifiesAsRunnerDiskFull`)
- no disk evidence was recorded (or marked unavailable) anywhere on the
  durable job record (`diskCapacityNeverRecorded` /
  `diskEvidenceRecordedOrMarkedUnavailable`)

### 14.a2 Agent `error_kind=disk` shortcut
`TestEnospcMisclassification::test_a2_agent_error_kind_disk_shortcut_is_not_honored`

A failed callback reporting `error_kind='disk'` (the task-7.4 local
detection shortcut task-4.4 classification must honor) falls into the
generic else-branch: observed `'BUILD_FAILED'`, expected
`'RUNNER_DISK_FULL'`.

### 14.a3 SSM fallback discards ENOSPC invocation evidence
`TestEnospcMisclassification::test_a3_ssm_fallback_discards_enospc_invocation_evidence`

Fixture: `building` job with correlated `ssm.command_id`
`2068c6dc-e0a0-4abd-a66c-e410c81950f2`, no callback; scripted final
`GetCommandInvocation` (`Status=Failed`, `ResponseCode=1`, stdout with
the buildkit ENOSPC line + `tee: /tmp/gdk-build-….log: No space left on
device`, stderr = the retained agent-tee ENOSPC line). SSM EventBridge
terminal-`Failed` shape delivered through `build_events.handler`
(fallback path, `decide_ssm_fallback`).

Observed unfixed output:

- ENOSPC-bearing invocation output collapsed into generic
  `'AGENT_COMMAND_FAILED'` instead of `'RUNNER_DISK_FULL'` — the
  fallback never retrieves or consults the invocation at all
  (`decide_ssm_fallback(status, command_status)` has no evidence input)
- no disk evidence recorded anywhere on the durable job record

### 14.a4 Property 17 — shrunk falsifying example (Hypothesis)
`TestEnospcMisclassification::test_property17_bug_condition_enospc_evidence_classification`

Generated: pattern in {`no space left on device`, `No space left on
device`, `ENOSPC`} x channel in {agent_message, agent_error_kind_disk,
invocation_stdout, invocation_stderr} x surrounding noise. Shrunk
falsifying example:

```
pattern='no space left on device',
channel='agent_message',
noise_before='',
noise_after='',
```

i.e. the MINIMAL case — the bare disk-exhaustion phrase in a plain agent
error message — classifies as generic `BUILD_FAILED`. The collapse is
unconditional across every evidence channel and pattern.

## Facet (b) — Head-keeping truncation loss (Req 1.21)

### 14.b1 Incident-shaped over-length buildkit line
`TestHeadKeepingTruncationLoss::test_b1_incident_overlength_buildkit_line_drops_trailing_cause`

The durable message is derived by executing the agent's ACTUAL pipeline,
extracted verbatim from `scripts/portal-build-agent.sh` line 224:

```
tail -n 5 "$BUILD_LOG" 2>/dev/null | head -c 512
```

against a fixture log whose last five lines reproduce the retained
shape: a 524-byte buildkit layer-extraction failure line ending in
`no space left on device`, positioned so byte 512 of the tail-5 stream
falls exactly after `write /var/snap/docker/common/`.

Observed unfixed derived durable message (verbatim tail):

```
…failed to extract layer sha256:265a67ff…ff: write /var/snap/docker/common/
```

— cut mid-path EXACTLY like the retained `bd91c5d8-…` DynamoDB
`error.message`; the trailing `no space left on device` root cause is
dropped (`durableMessageDroppedTrailingCause`).

### 14.b2 Property 18 — shrunk falsifying example (Hypothesis)
`TestHeadKeepingTruncationLoss::test_property18_bug_condition_trailing_cause_survives`

Generated: over-length lines (bound overshoot 1–1600 bytes, varied pad
characters, 0–4 preceding log lines) with the root cause at the END.
Shrunk falsifying example:

```
overshoot=1,   # 1 byte past the 512-byte bound suffices
pad_char='a',
previous_lines=[],
```

(Hypothesis: "The test always failed when commented parts were varied
together" — EVERY generated over-length line loses its trailing cause;
the derived message ends with pad bytes and never contains
`no space left on device`.)

## Run summary (unfixed storage baseline)

```
FAILED ...::TestEnospcMisclassification::test_a1_incident_agent_callback_enospc_collapses_to_generic
FAILED ...::TestEnospcMisclassification::test_a2_agent_error_kind_disk_shortcut_is_not_honored
FAILED ...::TestEnospcMisclassification::test_a3_ssm_fallback_discards_enospc_invocation_evidence
FAILED ...::TestEnospcMisclassification::test_property17_bug_condition_enospc_evidence_classification
FAILED ...::TestHeadKeepingTruncationLoss::test_b1_incident_overlength_buildkit_line_drops_trailing_cause
FAILED ...::TestHeadKeepingTruncationLoss::test_property18_bug_condition_trailing_cause_survives
6 failed
```

These failures ARE the task-14 deliverable: they freeze the
`storageEvidenceLost` bug condition. Do not weaken the assertions; the
storage fixes (tasks 4.4, 7.4, 7.5, 8.5) may now begin, and task 10.9
re-runs this exact file unchanged, where all 6 tests must then PASS.
