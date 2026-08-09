# Build Fleet Execution Failures Bugfix Design

## Overview

An authorized dedicated `AMD64` build can reach terminal SSM status `Failed` before the build agent reports a result, while the current consumer records only a generic error and the Build Log reads only one SSM stdout CloudWatch stream. The final command invocation—status details, response code, stdout, stderr, timestamps, command identity, and instance identity—is not retrieved or persisted. A second user-observed case ended as `failed` for exceeding maximum runtime, but current evidence cannot yet tell whether that build was actively progressing, stalled, queued behind other work, provisioning, or truly beyond a hard execution ceiling. This design does not assume that merely extending the timeout or merely queueing work is the correct remedy.

The correction introduces one evidence-driven reconciliation contract shared by EventBridge handling and the scheduled dispatcher tick. It retrieves terminal SSM evidence, waits a bounded interval for an in-flight agent result, redacts and bounds diagnostics before any application-controlled sink, classifies outcomes deterministically, and merges later evidence without resurrecting terminal state. A terminal-effects ledger makes status finalization, audit, dedicated allocation release or ephemeral cleanup, and oldest-eligible queue promotion retry-safe.

Runtime accounting is split into queue wait, provisioning, and active execution. Progress and heartbeat leases detect stalled or lost work; a target/mode-specific hard safety ceiling remains authoritative even while progress continues. Existing `max_runtime_hours` remains the compatibility fallback until evidence supports target-specific values. JP5/JP6 source builds known to take 1–2+ hours are therefore accommodated through snapshotted configurable budgets, not an unreviewed global constant change.

The Build Log API keeps `events` and `nextToken` unchanged and adds an optional structured diagnostic. The UI renders safe status details, response code, stdout/stderr excerpts, timing classification, and explicit unavailable/truncated markers instead of showing only `No log output was recorded for this build job.`

Static inspection proves a contract mismatch: `build_dispatcher.py` defaults `BUILD_REPO_DIR` to `/opt/dda/DefectDetectionApplication`, while dedicated fleet bootstrap clones to `/home/ubuntu/DefectDetectionApplication`. A preflight will detect this and the correction may align configuration, but the mismatch is not declared the historical cause unless retained evidence confirms it. RBAC, user-role matrices, route authorization, and navigation are out of scope.

**Amendment (third defect facet — ephemeral runner storage exhaustion):** Read-only evidence for ephemeral `JP6` job `bd91c5d8-ac7e-4125-becc-711860660f2e` confirms a distinct storage-exhaustion failure mode: a clean-lifecycle build on a 100 GB single-volume runner exhausted disk (`no space left on device`, ENOSPC) during concurrent final layer extraction of the two large JP6 docker images, and the agent's head-keeping error-tail truncation (`tail -n 5 | head -c 512`) dropped the trailing root cause from the durable record. The correction raises the ephemeral volume default from 100 GB to 200 GB (with optional per-target sizing where `JP6` resolves at least 200 GB), extends the deterministic classification table with a stable `RUNNER_DISK_FULL` code detected from ENOSPC evidence, changes durable-message derivation to tail-preserving truncation so the root-cause end of over-length lines survives, and adds disk-capacity recording to the dispatch preflight plus optional disk-usage evidence in failure diagnostics. Snapshot semantics are unchanged: `plan_runner` continues to read `config_snapshot.volume_size_gb`, and existing jobs keep their snapshotted sizes. No live build, no deployment, and no production setting mutation occurs during design or validation; the raised default takes effect only for jobs created after the deployment gate (task 12).

## Glossary

- **Bug_Condition (C)**: A dispatched job has command or runtime evidence requiring reconciliation, but the current path loses diagnostics, applies a generic/incorrect outcome, or consumes the wrong lifecycle interval as active runtime.
- **Property (P)**: The fixed system preserves complete safe evidence, selects the deterministic correct outcome, and completes terminal effects once.
- **Preservation**: Existing successful phase semantics, status values, artifacts, cancellation, serialization, queue ordering, target mappings, API pagination, and authorization behavior remain unchanged.
- **Invocation_Evidence**: The final `GetCommandInvocation` data for the persisted command and instance identity.
- **Agent_Evidence**: Correlated build-agent phase, heartbeat, progress, or terminal-result events.
- **Execution_Attempt**: A stable attempt identifier binding one job, target, mode, instance, and SSM command; stale evidence from another attempt cannot affect it.
- **Settlement_Window**: A bounded interval after terminal command observation in which a valid already-in-flight terminal agent result may arrive.
- **Execution_Diagnostic**: A versioned, bounded, redacted record persisted with the Build Job and returned by the Build Log API.
- **Queue_Wait**: Time from creation until allocation/dispatch eligibility; it is not active runtime.
- **Provisioning_Time**: Time spent creating or preparing compute before the agent begins executing; it is not active runtime.
- **Execution_Runtime**: Time from positively observed agent/SSM execution start until terminal completion.
- **Heartbeat_Lease**: A renewable liveness deadline based on correlated agent heartbeats.
- **Progress_Lease**: A renewable deadline based on meaningful progress such as phase/checkpoint advancement or output growth, distinct from mere process liveness.
- **Hard_Safety_Ceiling**: The snapshotted maximum execution duration that no heartbeat or progress event can extend.
- **Terminal_Effects**: Terminal transition, `ended_at`, one deduplicated audit, dedicated release or ephemeral cleanup, and protected queue promotion.
- **Effective_Dispatch**: One actual build execution; retries may repeat idempotent control operations but cannot run the build twice.
- **Volume_Size_Resolution**: New-job resolution of the ephemeral runner root volume size (`volume_size_gb`) at submission, snapshotted immutably in `config_snapshot` and read by `plan_runner` in `build_planner.py`.
- **ENOSPC_Evidence**: Disk-exhaustion patterns (`no space left on device`, ENOSPC) appearing in agent error messages, stderr, or invocation output, or an explicit agent-reported `error_kind=disk`.
- **Tail_Preserving_Truncation**: Bounding an over-length failure line by keeping its trailing bytes (the root-cause end) rather than its leading bytes, within existing size and redaction limits.
## Bug Details

### Bug Condition

The defect has three evidence-related forms. First, an SSM command becomes terminal before a terminal callback is durable, but the final invocation is not retrieved and its useful output is absent from Build Log. Second, a timeout decision is made without enough lifecycle/progress evidence to distinguish active execution from queueing, provisioning, or a stall. Third, an ephemeral build exhausts the runner's shared root volume (snap-docker layer storage, repository clone, buildkit cache, and `/tmp` on one default 100 GB volume), the failure is collapsed into a generic build failure, and head-keeping truncation of the durable error message drops the trailing `no space left on device` root cause.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input containing BuildJob job, optional SsmInvocation invocation,
         optional AgentEvidence agent, and timestamp now
  OUTPUT: boolean

  commandEvidenceLost :=
      job.ssm.command_id EXISTS
      AND invocation.status IN {Success, Failed, TimedOut, Cancelled}
      AND terminalAgentResultForAttempt(agent, job.execution_attempt_id) IS ABSENT
      AND (finalInvocationNotRetrieved(job)
           OR usefulDiagnosticsNotVisible(job)
           OR outcomeIsGenericOrMisclassified(job))

  runtimeEvidenceInsufficient :=
      job.status IN {queued, provisioning, building, publishing}
      AND currentSystemWouldFailForMaximumRuntime(job, now)
      AND (activeExecutionStartUnknown(job)
           OR queueOrProvisioningTimeIncluded(job)
           OR heartbeatOrProgressEvidenceIgnored(job)
           OR timeoutClassNotDistinguished(job))

  storageEvidenceLost :=
      job.execution_mode = ephemeral
      AND (diskExhaustionOccurredOrRisked(job)
           OR enospcEvidencePresent(job.agent_output, job.invocation_output))
      AND (job.config_snapshot.volume_size_gb = legacyUndersizedDefault(100)
           OR job.error_code = generic BUILD_FAILED despite ENOSPC evidence
           OR durableMessageDroppedTrailingCause(job)
           OR diskCapacityNeverRecorded(job))

  RETURN commandEvidenceLost OR runtimeEvidenceInsufficient OR storageEvidenceLost
END FUNCTION
```

### Correct Result Predicate

```
FUNCTION expectedBehavior(result)
  INPUT: reconciled BuildJob result
  OUTPUT: boolean

  RETURN result.outcome = classifyDeterministically(result.settledEvidence)
     AND result.execution_diagnostic IS bounded AND redacted
     AND everyAvailableInvocationFieldIsRepresentedOrMarkedUnavailable(result)
     AND buildLogShowsUsefulEvidenceWhenAnyExists(result)
     AND queueWaitDoesNotConsumeExecutionRuntime(result)
     AND provisioningDoesNotConsumeExecutionRuntime(result)
     AND freshProgressPreventsSoftStallTimeout(result)
     AND hardSafetyCeilingCannotBeExtended(result)
     AND terminalStateIsAbsorbing(result)
     AND terminalEffectsOccurExactlyOnce(result)
     AND serializationInvariantIsPreserved(result)
     AND newJobVolumeSizeResolvesRaisedDefault(result)      // 200 GB default; JP6 >= 200
     AND snapshottedVolumeSizesRemainImmutable(result)
     AND enospcEvidenceClassifiesAsRunnerDiskFull(result)
     AND durableMessagePreservesRootCauseTail(result)
     AND diskEvidenceRecordedOrMarkedUnavailable(result)
END FUNCTION
```

### Concrete Examples

- **Observed dedicated AMD64 failure**: job `06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03` was dispatched to `srv-5b214096-91a9-41b7-9d62-cc03ba205c15`, ran for approximately eight seconds, and received SSM `Failed` before a callback. Expected: retrieve the invocation, safely expose its response code/status details/stdout/stderr, and classify from that evidence. Actual: generic `AGENT_COMMAND_FAILED` plus an empty Build Log.
- **Failed invocation with stderr only**: SSM returns `Status=Failed`, `ResponseCode=127`, and stderr `.../portal-build-agent.sh: No such file or directory`, while the stdout stream is absent. Expected: `COMMAND_EXECUTION_FAILED` with a redacted stderr excerpt and path/preflight context. Actual: stderr is discarded and the UI reports no output.
- **Successful command with missing callback**: SSM returns `Success` but no terminal agent result arrives by the settlement deadline. Expected: `AGENT_RESULT_MISSING`, not success and not generic command failure.
- **Delayed callback race**: a correlated `succeeded` callback describing completion before the deadline arrives after the SSM `Success` event but within settlement. Expected: the valid agent result wins; command diagnostics merge without changing success or duplicating audit/cleanup.
- **User-observed maximum-runtime failure, active progress**: execution exceeds a soft inactivity lease while fresh correlated progress continues and remains below its target/mode hard ceiling. Expected: renew the soft lease and continue; do not fail merely because wall time since submission is large.
- **Stalled/hung execution**: the process remains nominally running but heartbeats stop, or heartbeats continue without meaningful progress beyond the configured progress-stall budget. Expected: classify the specific lease expiry, capture last heartbeat/progress and SSM/process evidence, stop safely, and finalize once.
- **Legitimately queued build**: a job waits six hours behind another dedicated build. Expected: record six hours of queue wait while active runtime remains zero; preserve oldest-eligible ordering and no-concurrent-build guarantees.
- **Provisioning delay**: an ephemeral runner takes 45 minutes to become SSM Online. Expected: charge provisioning time only to an explicitly configured provisioning budget, never to execution runtime.
- **Hard ceiling**: a JP5 source build continues emitting progress but crosses its snapshotted hard execution ceiling. Expected: `MAX_RUNTIME_EXCEEDED`, actionable timing diagnostics, safe stop, and one cleanup/promotion sequence. The ceiling value is target/mode configurable; this design does not assume the current observed job needs a longer value.
- **Observed ephemeral JP6 disk exhaustion**: job `bd91c5d8-ac7e-4125-becc-711860660f2e` ran roughly 100 minutes on a 100 GB runner, both JP6 image exports hit ENOSPC during final layer extraction, the agent's own log tee hit ENOSPC, and `gdk` exited 1. Expected: classify `RUNNER_DISK_FULL` with disk-usage evidence and a durable message ending in `no space left on device`. Actual: generic `BUILD_FAILED` with a durable message truncated mid-path at `write /var/snap/docker/common/`.
- **Over-length failure line**: a buildkit failure line exceeding the 512-byte bound carries its root cause at the end. Expected: the durable bounded message contains the line's trailing content within existing size/redaction limits. Actual: `head -c 512` keeps only the head and drops the cause.
- **Short failure line (preservation)**: a failure line already within the byte bound. Expected and actual: recorded unchanged; tail-preserving truncation changes only which end of over-length lines is retained.

### Evidence Precedence and Classification

For one `Execution_Attempt`, reconciliation orders evidence by semantic authority rather than delivery order:

1. A valid correlated agent terminal result whose reported completion time is at or before an already-decided hard deadline wins over SSM fallback and preserves result/partial-publish metadata.
2. An explicit user cancellation with confirmed stop yields existing `cancelled` semantics.
3. A hard-ceiling decision wins when no qualifying pre-deadline terminal result exists, even if later heartbeats arrive.
4. Infrastructure loss or spot interruption yields existing `interrupted` semantics with `INFRASTRUCTURE_LOST` diagnostics.
5. Terminal SSM `Failed`, `TimedOut`, or `Cancelled` without a terminal agent result is classified from invocation and lifecycle evidence.
6. SSM `Success` without a terminal agent result remains pending through settlement, then becomes `AGENT_RESULT_MISSING`.
7. Missing or delayed service evidence is represented as unavailable and retried within bounds; it is never fabricated.

Stable safe classifications are:

| Evidence | Status | Error code |
|---|---|---|
| `SendCommand` rejected before command ID | `failed` | `COMMAND_LAUNCH_FAILED` |
| Invocation `Failed` / non-zero response | `failed` | `COMMAND_EXECUTION_FAILED` |
| Invocation service status `TimedOut` | `failed` | `COMMAND_TIMED_OUT` |
| Unexpected invocation cancellation | `interrupted` | `COMMAND_CANCELLED` |
| Invocation `Success`, callback absent after settlement | `failed` | `AGENT_RESULT_MISSING` |
| Instance/server lifecycle lost | `interrupted` | `INFRASTRUCTURE_LOST` |
| Heartbeat lease expired | `failed` | `AGENT_HEARTBEAT_EXPIRED` |
| Heartbeats present but progress lease expired | `failed` | `BUILD_PROGRESS_STALLED` |
| Explicit provisioning budget exceeded | `failed` | `PROVISIONING_TIMEOUT` |
| Explicit queue-wait budget exceeded | `failed` | `QUEUE_WAIT_TIMEOUT` |
| Active execution crosses hard ceiling | `failed` | `MAX_RUNTIME_EXCEEDED` |
| ENOSPC evidence in agent/invocation output or agent-reported `error_kind=disk` | `failed` | `RUNNER_DISK_FULL` |

The UI message may add safe context, but these codes and their evidence-to-code mapping are deterministic. `RUNNER_DISK_FULL` detection lives in the same evidence-classification seam as the other rows (`build_reconciliation.py`): reconciliation matches ENOSPC patterns (`no space left on device`, ENOSPC) in the agent error message, stderr, or invocation output, and the agent may also short-circuit detection by reporting `error_kind=disk` directly. Outputs without disk-exhaustion evidence never classify as `RUNNER_DISK_FULL`.
## Expected Behavior

### Runtime Accounting Model

The job stores additive timing fields; existing top-level fields and statuses remain backward compatible:

- `created_at`: submission time.
- `dispatched_at`: allocation/provisioning initiation.
- `started_at`: existing public field, still set when the job enters `building`.
- `timing.queue_started_at`, `timing.queue_ended_at`, and derived `queue_wait_ms`.
- `timing.provisioning_started_at`, `timing.provisioning_ended_at`, and derived `provisioning_ms`.
- `timing.execution_started_at`: first positive evidence that the correlated command/agent began execution; this, not `created_at` or provisioning start, anchors execution runtime.
- `timing.last_heartbeat_at`, `timing.last_progress_at`, `timing.last_progress_kind`, and monotonic sequence values.
- `timing.execution_ended_at` and derived `execution_runtime_ms`.
- `timing.timeout_decided_at`, `timing.timeout_kind`, configured budget, observed duration, last activity, and target/mode.

The snapshotted configuration supports target/mode overrides with backward-compatible fallback:

```
effectiveBudget(job) :=
  job.config_snapshot.runtime_budgets[job.build_target][job.execution_mode]
  OR job.config_snapshot.runtime_budgets[job.build_target].default
  OR job.config_snapshot.max_runtime_hours
  OR 4 hours
```

Each effective budget may define `heartbeat_lease_minutes`, `progress_stall_minutes`, `hard_runtime_hours`, and optional separate `queue_wait_hours` and `provisioning_minutes`. Queue/provisioning limits are disabled unless explicitly configured. Existing jobs lacking the new shape use their snapshotted `max_runtime_hours` as the hard runtime ceiling and do not retroactively adopt current settings.

At the exact lease or ceiling boundary the job remains eligible to run; expiry uses strict `now > deadline`, preserving the existing strict timeout boundary. A fresh heartbeat renews liveness only. Meaningful progress renews both liveness and progress. Neither can extend `execution_started_at + hard_runtime_hours`.

### Ephemeral Volume Sizing Model

`DEFAULT_VOLUME_SIZE_GB` in the build configuration defaults (`edge-cv-portal/backend/functions/build_config.py`) rises from 100 to 200. Resolution mirrors the runtime-budget pattern and is performed once at submission:

```
effectiveVolumeSizeGb(request) :=
  request.volume_size_gb                                   // explicit user value, validated as today
  OR config.volume_size_gb_by_target[request.build_target] // optional per-target map
  OR config.volume_size_gb                                 // global default, now 200
```

The optional `volume_size_gb_by_target` map (target -> volume GB) follows the same validation and snapshot discipline as the runtime-budget map: validated in `build_domain.py`/`build_config.py`, resolved at submission, and snapshotted immutably into `config_snapshot.volume_size_gb`. Any configured or resolved `JP6` value must be at least 200 GB. Snapshot semantics are unchanged: `plan_runner` in `build_planner.py` continues to read `config_snapshot.volume_size_gb`, and previously created jobs keep their snapshotted sizes without retroactive adoption. The frontend `BuildInfrastructureSettings.tsx` continues to expose `volume_size_gb` and gains the optional per-target sizing without removing the global field. The 200 GB default takes effect only for jobs created after deployment (task 12 gate); no production setting is mutated during design or validation.

### Preservation Requirements

**Unchanged Behaviors:**
- Normal `building`, `publishing`, `succeeded`, build-failure, and publishing-failure callbacks retain current state-machine semantics, result metadata, partial-publish lists, and absorbing terminal statuses.
- Dedicated dispatch retains the DynamoDB single-slot allocation, pre-dispatch `pgrep`, on-server `flock`, serialization watchdog, deferral cadence, and prohibition on concurrent builds.
- Cancellation retains queued removal, SSM stop, `pgrep` confirmation, conflict handling, and fail-closed behavior.
- Predecessor gates, original submission ordering, and oldest-eligible dedicated queue promotion remain unchanged.
- `JP5`/`JP6` remain arm64; `AMD64`/`AMD64_NVIDIA` remain x86_64; existing component identities and intentional mode restrictions remain unchanged.
- Successful dedicated and ephemeral builds continue to stream logs, publish artifacts, clean compute, and promote queued work.
- Existing Build Job APIs keep response envelopes, status values, field meanings, pagination, and retention; diagnostics and timing are optional additions.
- Existing role checks, permission matrices, navigation, and authorization envelopes are not modified.
- Existing jobs honor their snapshotted `volume_size_gb`, instance type, and spot/on-demand choices; new-job instance-type and spot resolution behave as before except for the raised volume-size default and optional per-target sizing.
- Successful builds within available disk capacity retain current log streaming, callbacks, artifact publication, compute cleanup, and result recording; the disk preflight recording and disk diagnostics are additive observations only.
- Failures unrelated to disk exhaustion keep their existing durable error messages, bounded sizes, and redaction behavior; tail-preserving truncation changes only which end of over-length lines is retained, and lines within the bound are unchanged.

**Scope:**
Inputs outside `isBugCondition` must behave as before. This includes successful callbacks, non-agent SSM commands, unrelated EventBridge events, jobs without a command ID, ordinary CloudWatch pagination, existing retries/cancellation, and all supported target/mode combinations. Infrastructure changes are limited to operational wiring needed for read-only command reconciliation, schedule/event delivery, and repository-path configuration; they do not change user RBAC.

## Hypothesized Root Cause

The following are hypotheses ranked by code evidence; retained invocation data must decide which applies to the incident:

1. **Repository path contract mismatch**: dedicated bootstrap clones to `/home/ubuntu/DefectDetectionApplication`, while dispatcher default command path is `/opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh`.
   - This is a proven static mismatch and plausibly explains an approximately eight-second exit with code 127.
   - It is not historical proof until stderr/status details or equivalent retained evidence confirms it.

2. **Invocation evidence discarded**: `build_events.py` consumes only EventBridge command ID/status, has no SSM client, and does not call `GetCommandInvocation`.
   - The event Lambda currently receives no dedicated read-only invocation permission.
   - Terminal diagnostics and response code cannot reach DynamoDB, API, UI, or audit.

3. **Build Log source is incomplete**: the job points to the SSM stdout stream only.
   - Missing, delayed, or empty stdout is treated as an empty page.
   - Invocation stderr and status details are neither merged nor shown.

4. **Terminal fallback is premature and generic**: the first failure notification immediately absorbs the job.
   - SSM `Success` is not routed, so missing-result-after-success cannot be classified.
   - Late evidence cannot enrich a terminal record through the current transition-only path.

5. **No scheduled command reconciliation**: the one-minute dispatcher tick does not inspect `ssm.command_id`.
   - Lost EventBridge notifications leave command evidence unobserved.

6. **Timeout model lacks evidence dimensions**: `decide_runtime_timeout` checks only running status, `started_at`, and a single `max_runtime_hours`.
   - It has no heartbeat, progress, hard-vs-soft distinction, timeout subtype, or target/mode override.
   - Queue and provisioning statuses are currently excluded, which is correct for active runtime, but their elapsed times and independent limits are not visible.
   - The user-observed timeout therefore cannot yet justify extending the limit or changing queue behavior.

7. **Terminal side effects are distributed**: status transitions, audit, server release, runner termination, and subsequent dispatch occur in separate handlers/ticks.
   - Conditional status writes prevent resurrection but do not by themselves prove exactly-once diagnostic merge, audit, cleanup, or promotion under all races.

8. **Dispatch command assumptions are not preflighted**: path, script mode, shell tools, target mapping, component identity, callback bus, source ref, architecture, and AWS environment are assumed rather than recorded as evidence.

9. **Undersized shared root volume (evidence-confirmed for the storage facet)**: the 100 GB default gp3 root volume hosts snap-docker layer storage (`/var/snap/docker/common`), the repository clone, buildkit cache, and `/tmp` simultaneously, while the JP6 target exports two large multi-stage CUDA/TensorRT/Triton images concurrently via docker-compose.
   - Confirmed by the DynamoDB record, SSM invocation `2068c6dc-e0a0-4abd-a66c-e410c81950f2`, and the CloudWatch log for job `bd91c5d8-ac7e-4125-becc-711860660f2e`: both final layer extractions failed with ENOSPC after roughly 100 minutes.
   - Unlike hypotheses 1–8, this cause is established from retained evidence, not inference.

10. **Head-keeping error-tail truncation (evidence-confirmed)**: `scripts/portal-build-agent.sh` derives the durable error message via `tail -n 5 "$BUILD_LOG" | head -c 512`, keeping the front of very long buildkit failure lines and dropping the trailing root cause, so the durable record ended at `write /var/snap/docker/common/` and concealed `no space left on device`.

11. **No disk evidence in preflight or diagnostics**: dispatch preflight records no available disk capacity for the build/docker storage path, and failure diagnostics carry no disk-usage fields, so exhaustion cannot be confirmed or excluded from the durable record alone.

### Appended Runtime Requirements Traceability

The appended runtime criteria refine the existing design rather than changing its approach:

- Requirements 1.13–1.18 are addressed by the second form of `isBugCondition`, the runtime concrete examples, root-cause hypothesis 6, and the evidence fields in the Runtime Accounting Model. Together they capture insufficient evidence for phase, queue/provisioning isolation, heartbeat/progress state, target/mode ceilings, and terminal timeout diagnostics.
- Requirements 2.13 and 2.18 are satisfied by Historical Evidence Retrieval, the timeout decision diagnostic, Property 13, and Property 14: a timeout label alone cannot select a remedy, and available lifecycle/activity/log evidence is reported while unavailable evidence is identified.
- Requirements 2.14 and 2.15 are satisfied by the Runtime Accounting Model, `decideTimeout`, Property 9, and the queue/provisioning integration cases: each phase is measured independently, active runtime starts only on positive execution evidence, and occupied-server/predecessor wait remains queued in oldest-eligible order.
- Requirements 2.16 and 2.17 are satisfied by heartbeat/progress leases, the non-extendable snapshotted target/mode hard ceiling, Properties 7, 8, and 11, and their boundary/matrix tests. JP5/JP6 duration evidence informs configuration but does not mandate an unevidenced value.
- Requirement 2.19 is satisfied by Property 15 and Approval-Gated Live Verification: mocked and property-based checks precede any costly action, and production timeout changes or build launches require explicit approval.
- Requirement 3.11 is preserved by the terminal-effects ledger, verified stop-before-release ordering, and Properties 6 and 10 for exactly-once cleanup, audit, oldest-eligible promotion, duplicate-dispatch prevention, and serialization.
- Requirement 3.12 is preserved by the strict `now > deadline` comparison, immutable snapshots with `max_runtime_hours` fallback, and Properties 7 and 10.

### Appended Storage Requirements Traceability

The appended storage criteria extend the same evidence-driven contract:

- Requirements 1.19–1.22 are captured by the third form of `isBugCondition` (`storageEvidenceLost`), the disk-exhaustion concrete examples, and root-cause entries 9–11, which are confirmed by retained evidence for job `bd91c5d8-ac7e-4125-becc-711860660f2e`.
- Requirement 2.20 is satisfied by the Ephemeral Volume Sizing Model and Property 16: the default rises to 200 GB, optional per-target sizing resolves `JP6` to at least 200 GB, resolution occurs at submission, and snapshots remain immutable.
- Requirement 2.21 is satisfied by the `RUNNER_DISK_FULL` classification row, its detection seam in `build_reconciliation.py`, the optional agent `error_kind=disk` shortcut, and Property 17.
- Requirement 2.22 is satisfied by Tail-Preserving Durable Message Derivation in the agent, the head+tail-preserving backend bounding primitives (task 4.1), and Property 18.
- Requirement 2.23 is satisfied by the disk-capacity recording check added to the preflight contract (task 7.1) and the optional disk-usage fields in the failure diagnostic, with unavailable measurements identified rather than fabricated.
- Requirements 3.13–3.15 are preserved by the immutable-snapshot discipline, the additive-only nature of disk recording/diagnostics, and the scoping of tail-preserving truncation to over-length lines only (Properties 2, 16, and 18).

## Correctness Properties

Property 1: Bug Condition - Terminal SSM Evidence Recovery

_For any_ dispatched job whose correlated SSM command becomes terminal before a terminal agent result is durable, reconciliation SHALL retrieve the final invocation when available, persist a bounded redacted diagnostic, and expose useful available evidence instead of only a generic error or empty-log message.

**Validates: Requirements 2.1, 2.2, 2.3, 2.11**

Property 2: Preservation - Non-Bug Build Behavior

_For any_ input where `isBugCondition` returns false, the fixed system SHALL produce the same status/result, artifact metadata, cancellation behavior, queue order, target mapping, API envelope, and effective dispatch behavior as the original system, except for backward-compatible optional diagnostic/timing fields.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.9**

Property 3: Diagnostic Redaction and Bounds

_For any_ invocation, callback, progress, or exception payload containing secret-shaped values and arbitrary Unicode/size, every persisted or user-visible diagnostic SHALL omit the original secret values, remain valid UTF-8/JSON, fit configured per-field and total byte limits, and identify truncation or unavailable fields without fabricating content.

**Validates: Requirements 2.2, 2.10, 2.11**

Property 4: Deterministic Classification and Ordering

_For any_ correlated set of agent, SSM, timeout, cancellation, and lifecycle evidence, every permutation and duplication of deliveries SHALL converge to the same precedence-defined status, safe error code, agent result, and merged diagnostic; terminal state SHALL remain absorbing while later evidence may only increase diagnostic completeness.

**Validates: Requirements 2.4, 2.6, 2.11**

Property 5: Event and Scheduled Reconciliation Convergence

_For any_ persisted command ID, whether a terminal EventBridge notification is delivered zero, one, or multiple times, the EventBridge path plus scheduled reconciliation SHALL converge within the configured bound; a genuinely nonterminal invocation SHALL remain nonterminal.

**Validates: Requirements 2.1, 2.5, 2.6**

Property 6: Exactly-Once Terminal Effects and Promotion

_For any_ terminal cause and any retries/races among callback, command event, timeout watchdog, cancellation, and scheduled tick, there SHALL be one terminal outcome, one deduplicated audit effect, one effective dedicated release or ephemeral cleanup, one oldest-eligible queue promotion, and at most one effective build execution for each attempt.

**Validates: Requirements 2.6, 2.7, 2.11, 3.11**

Property 7: Timeout Boundary and Hard Ceiling

_For any_ active execution and snapshotted target/mode hard budget, the system SHALL not time out at `now == hard_deadline`, SHALL classify `MAX_RUNTIME_EXCEEDED` at `now > hard_deadline` when no qualifying pre-deadline terminal result exists, and SHALL not allow heartbeat or progress events to extend that hard deadline.

**Validates: Requirements 2.4, 2.7, 2.11, 2.17, 3.8, 3.12**

Property 8: Heartbeat and Progress Leases

_For any_ active execution below its hard ceiling, fresh correlated progress SHALL renew both heartbeat and progress leases, a heartbeat without progress SHALL renew only liveness, missing heartbeats beyond the heartbeat lease SHALL classify `AGENT_HEARTBEAT_EXPIRED`, and continued liveness without progress beyond the progress lease SHALL classify `BUILD_PROGRESS_STALLED` with the last observed evidence.

**Validates: Requirements 2.4, 2.5, 2.11, 2.16, 2.18**

Property 9: Queue and Provisioning Time Isolation

_For any_ queue delay or provisioning delay, active execution runtime SHALL remain zero until positive execution-start evidence; queue or provisioning SHALL time out only under their own explicitly configured snapshotted budgets, and waiting behind another job SHALL preserve serialization and oldest-eligible ordering.

**Validates: Requirements 2.4, 2.7, 2.11, 2.14, 2.15, 3.2, 3.4**

Property 10: Timeout Retry and Race Convergence

_For any_ timeout decision concurrent with a terminal callback, SSM status event, cleanup retry, or queue-promotion tick, a valid result completed before the applicable deadline SHALL retain precedence, otherwise the deterministic timeout class SHALL remain terminal, and all repeated effects SHALL converge without duplicate dispatch or cleanup.

**Validates: Requirements 2.6, 2.7, 2.11, 3.11, 3.12**

Property 11: Target and Mode Matrix

_For any_ supported target and valid execution mode, preflight, runtime budgeting, reconciliation, diagnostics, and terminal effects SHALL preserve `JP5`/`JP6 -> arm64`, `AMD64`/`AMD64_NVIDIA -> x86_64`, current component identities, and intentional architecture-specific differences; invalid combinations SHALL continue to fail existing validation.

**Validates: Requirements 2.8, 2.9, 2.11, 2.17, 3.7**

Property 12: Preflight Fails Before Costly Work

_For any_ missing or invalid repository path, agent executable, required tool, architecture mapping, component identity, callback bus, source ref, quoting contract, or required AWS environment, preflight SHALL emit a redacted actionable diagnostic and prevent the build/publish step; for any valid contract it SHALL not change the resulting command semantics.

**Validates: Requirements 2.8, 2.9, 3.9**

Property 13: Build Log and Timeout Diagnostic Availability

_For any_ terminal job with useful invocation or timeout diagnostics and an absent, empty, delayed, or incomplete CloudWatch stream, the existing log event page SHALL remain backward compatible and the optional diagnostic payload/UI SHALL show the available timing phase, budget and source, observed duration, last heartbeat/progress, target/mode, and safely redacted evidence; only unavailable fields may be explicitly marked unavailable.

**Validates: Requirements 2.2, 2.3, 2.18, 3.6**

Property 14: Evidence-Gated Timeout Diagnosis

_For any_ reported maximum-runtime failure, the selected classification or remedy SHALL be derived from separately measured queue, provisioning, active-execution, heartbeat, progress, hard-ceiling, and retained-log evidence, and SHALL NOT infer a timeout extension or queueing remedy from the failure label alone.

**Validates: Requirements 2.13, 2.18**

Property 15: Approval-Gated Timeout Validation

_For any_ timeout correction validation, safe mocked and property-based checks SHALL complete before a costly live build, and no production timeout configuration change or dedicated/ephemeral build launch SHALL occur without explicit user approval.

**Validates: Requirements 2.19**

Property 16: Volume-Size Default and Per-Target Resolution

_For any_ new ephemeral Build Job submission, volume-size resolution SHALL yield the raised 200 GB default when no explicit or per-target value applies, SHALL resolve any `JP6` per-target value to at least 200 GB, SHALL snapshot the resolved size immutably into `config_snapshot.volume_size_gb`, and _for any_ previously created job SHALL leave the snapshotted volume size, instance type, and spot semantics unchanged.

**Validates: Requirements 2.20, 3.13**

Property 17: ENOSPC Classification

_For any_ agent error message, stderr, or invocation output containing disk-exhaustion patterns (`no space left on device`, ENOSPC) or an agent-reported `error_kind=disk`, classification SHALL yield the stable `RUNNER_DISK_FULL` error code and never generic `BUILD_FAILED`, and _for any_ output containing no disk-exhaustion evidence, classification SHALL never yield `RUNNER_DISK_FULL`.

**Validates: Requirements 2.21, 3.15**

Property 18: Tail-Preserving Truncation

_For any_ failure line exceeding the durable-message byte bound, the derived bounded redacted message SHALL contain the line's trailing content (the root-cause end), and _for any_ line within the bound, the derived message SHALL be unchanged from current behavior, preserving existing bounded sizes and redaction.

**Validates: Requirements 2.22, 3.15**
## Fix Implementation

### Data Model

Add optional fields to each Build Job; no status or existing field is removed:

```json
{
  "execution_attempt": {
    "attempt_id": "uuid",
    "dispatch_state": "claimed|sending|sent|reconciling|terminal",
    "instance_id": "i-...",
    "command_id": "cmd-...",
    "command_comment": "dda-build:<job-id>:<attempt-id>",
    "claimed_at": 0,
    "sent_at": 0
  },
  "timing": {
    "queue_started_at": 0,
    "queue_ended_at": 0,
    "provisioning_started_at": 0,
    "provisioning_ended_at": 0,
    "execution_started_at": 0,
    "last_heartbeat_at": 0,
    "last_progress_at": 0,
    "last_progress_kind": "phase|checkpoint|output_growth",
    "heartbeat_sequence": 0,
    "progress_sequence": 0,
    "execution_ended_at": 0,
    "timeout_decided_at": 0,
    "timeout_kind": "..."
  },
  "execution_diagnostic": {
    "schema_version": 1,
    "attempt_id": "uuid",
    "command_id": "cmd-...",
    "instance_id": "i-...",
    "source": ["eventbridge", "scheduled_reconciliation"],
    "status": "Failed",
    "status_details": "...",
    "response_code": 127,
    "execution_start": "...",
    "execution_end": "...",
    "stdout": {"text": "...", "available": true, "truncated": false},
    "stderr": {"text": "...", "available": true, "truncated": false},
    "classification": "COMMAND_EXECUTION_FAILED",
    "observed_at": 0,
    "complete": true
  },
  "terminal_effects": {
    "effect_id": "<job-id>:<attempt-id>:terminal",
    "audit": "pending|done",
    "compute_cleanup": "pending|done|not_applicable",
    "allocation_release": "pending|done|not_applicable",
    "promotion_wakeup": "pending|done"
  }
}
```

Use byte-based limits after redaction: at most 16 KiB each for stdout/stderr, 4 KiB for status/detail/message fields, and 48 KiB total diagnostic JSON. Preserve useful head and tail with an inserted truncation marker and original byte count. A missing provider field is `{available:false}`; an empty but available field is `{available:true,text:""}`.

### Shared Diagnostic Sanitizer

Create a pure helper module used by events, dispatcher, jobs API, and tests. It recursively normalizes supported scalar/list/map values, redacts before logging or persistence, then bounds output. Redaction covers at minimum AWS access-key IDs, secret/session values, bearer/basic/authorization values, password/token/secret assignments, repository credentials, signed-URL credential/signature/token parameters, and configured organization patterns. Key names are retained where safe; values become `[REDACTED]`.

Raw invocation content exists only in local function memory long enough to sanitize. Code must not interpolate raw service responses into Lambda logs, exceptions, Audit_Log details, or failed-job messages. Tests inject unique canary secrets and search every captured sink.

### Reconciliation Flow

Implement a pure `reconcile_attempt(job, invocation, agent_evidence, lifecycle, now)` decision and thin I/O adapters:

1. Resolve and validate `attempt_id`, command ID, and instance ID; reject stale/mismatched evidence.
2. `GetCommandInvocation` when command identity is known. Treat `InvocationDoesNotExist` as eventual consistency until the bounded lookup deadline, not as command failure.
3. Sanitize and bound the invocation immediately.
4. Merge diagnostics by field completeness using a conditional diagnostic version update, even when status is already terminal.
5. If invocation is nonterminal, update observed timing and leave the job nonterminal.
6. If terminal agent evidence exists, apply the existing phase result and merge command diagnostics.
7. Otherwise set/retain `settlement_deadline` and wait a configurable short interval.
8. At deadline, apply the classification table and create the terminal-effects ledger through conditional finalization.

EventBridge becomes the low-latency path and routes `Success` as well as `Failed`, `TimedOut`, and `Cancelled`. The one-minute scheduled tick scans command-bearing nonterminal jobs, jobs awaiting settlement, dispatch attempts in an ambiguous `sending` state, and terminal jobs with incomplete diagnostics/effects. Both paths call the same reconciliation function, making a missing event a latency issue rather than a correctness issue.

### Ordering, Idempotency, Cleanup, and Promotion

- Every agent event includes `attempt_id`, a unique event ID, monotonic sequence, and source timestamp. Duplicate IDs and non-increasing progress sequences are no-ops.
- Terminal finalization conditionally writes the status, error/result, `ended_at`, evidence digest, and stable `effect_id`. A DynamoDB transaction also releases a dedicated allocation only if that job still owns it and cleanup is already confirmed.
- Timeout/cancellation/interruption first records cleanup pending, sends the stop operation, and confirms no protected build process remains. A dedicated slot is not released and no follower is promoted while process state is unknown, preserving no-concurrent-build guarantees.
- Ephemeral `TerminateInstances` is naturally retryable; `InvalidInstanceID.NotFound` is successful cleanup. The observed terminal instance state sets cleanup done.
- Audit writes use `effect_id` as a uniqueness key. A retry can complete a pending audit but cannot create a second logical record.
- Allocation release wakes the dispatcher, but the scheduled tick remains the fallback. Existing oldest-eligible planning plus the conditional server lock selects one follower. A promotion records a dispatch claim before any command is sent.
- `SendCommand` carries a deterministic command comment containing job/attempt identity. If execution stops after recording `sending` but before persisting `command_id`, reconciliation searches recent commands for that marker and attaches the existing ID. It does not blindly resend. Only after the visibility/reconciliation bound proves no command exists may a conditional new attempt be sent.
- The agent's local `flock` and attempt marker remain defense in depth: even an ambiguous provider retry cannot execute the build concurrently or execute the same attempt twice.
- Later diagnostics update `execution_diagnostic` only; they cannot rewrite an absorbed status, valid agent result, `ended_at`, or completed effects.

### Runtime and Progress Reconciliation

The agent receives `ATTEMPT_ID` and emits:

- an execution-start event after preflight and lock acquisition;
- periodic heartbeat events while the command wrapper and protected build process are alive;
- progress events when phase/checkpoint advances or the local build log grows, with monotonic sequence and byte count;
- a terminal result carrying completion time and the existing result/failure metadata.

A trap stops the heartbeat monitor when the agent exits. Heartbeats contain no raw build output or environment. Scheduled reconciliation also uses SSM invocation state and safe process checks as secondary liveness evidence when events are delayed.

Timeout evaluation is a pure state machine:

```
FUNCTION decideTimeout(job, now)
  IF job.status = queued THEN
    RETURN expireOnlyIfConfigured(queue_wait_deadline, QUEUE_WAIT_TIMEOUT)
  IF job.status = provisioning THEN
    RETURN expireOnlyIfConfigured(provisioning_deadline, PROVISIONING_TIMEOUT)
  IF execution_started_at IS ABSENT THEN
    RETURN WAIT_FOR_EXECUTION_EVIDENCE
  IF now > execution_started_at + hard_runtime_budget THEN
    RETURN MAX_RUNTIME_EXCEEDED
  IF now > last_heartbeat_at + heartbeat_lease THEN
    RETURN AGENT_HEARTBEAT_EXPIRED
  IF now > last_progress_at + progress_stall_budget THEN
    RETURN BUILD_PROGRESS_STALLED
  RETURN CONTINUE
END FUNCTION
```

The decision stores observed queue, provisioning, active runtime, last heartbeat/progress ages, budget source, target, and mode in the diagnostic. That evidence lets operators decide whether a specific budget needs adjustment, work was actually queued, provisioning was slow, or execution stalled. No automatic timeout extension follows from the newly reported case.

### AMD64 Command Preflight and Path Hypothesis

Run a short, separately recorded preflight after dedicated allocation/`pgrep` verification or ephemeral SSM readiness, but before the costly agent command. It checks:

1. configured repository directory exists and is the intended clone;
2. `scripts/portal-build-agent.sh` and `portal-build.sh` exist, are readable, and can be invoked;
3. `bash`, `flock`, `git`, `aws`, Docker, GDK, Python, and target-specific tools are available;
4. current machine architecture matches the target (`AMD64* -> x86_64`, `JP5/JP6 -> arm64`);
5. target maps to the existing portal-build arguments and expected component identity;
6. source ref is resolvable without mutating the checkout during preflight;
7. callback bus/region/account identity is available and command quoting round-trips job/attempt/target values;
8. required directories/lock/log locations are writable by the build user;
9. required AWS identity is present without printing credentials;
10. available disk capacity is recorded (`df`) for the build/docker storage path — for snap-docker runners `/var/snap/docker/common` — and for the repository/`tmp` volume; this check records evidence and does not itself fail the preflight unless a separately configured minimum is violated.

Align dedicated configuration with the fleet clone location or pass the actual clone path explicitly from fleet registration; do not rely on divergent defaults. Persist the chosen path and preflight checks in redacted diagnostics. A failed preflight is `COMMAND_PREFLIGHT_FAILED`, performs no build/publish, and follows the same exactly-once terminal cleanup/promotion flow.

### Build Log Persistence, API, and UI

`GET /builds/{id}/logs` retains:

```json
{"events": [{"timestamp": 0, "message": "..."}], "nextToken": "..."}
```

and optionally adds:

```json
{
  "diagnostic": {
    "schemaVersion": 1,
    "classification": "COMMAND_EXECUTION_FAILED",
    "status": "Failed",
    "statusDetails": "...",
    "responseCode": 127,
    "stdout": {"text": "...", "available": true, "truncated": false},
    "stderr": {"text": "...", "available": true, "truncated": false},
    "timing": {"queueMs": 0, "provisioningMs": 0, "executionMs": 8000},
    "observedAt": 0,
    "complete": true
  }
}
```

The diagnostic is returned independently of CloudWatch stream existence and may be repeated across pages as immutable metadata. Existing clients ignore it. `GET /builds/{id}` may expose the same optional persisted structure.

`BuildDetail.tsx` renders an Execution diagnostics panel with classification, response/status details, timing breakdown, timeout reason/budget, and stdout/stderr excerpts. If CloudWatch events are empty but diagnostic output exists, the textarea/panel shows that output and never the generic terminal empty-log message. If neither source is available, it states which evidence was unavailable or expired. Existing polling and tokens remain unchanged.

### Historical Evidence Retrieval

Before choosing the incident correction or timeout values, perform a read-only investigation for the known AMD64 job and the newly observed runtime-failed job:

1. Read the BuildJobs item and Audit_Log entries to obtain command ID, instance/server identity, timestamps, config snapshot, status transitions, and any existing error.
2. Query final command/invocation details by recorded command and instance IDs, including status details, response code, stdout/stderr, and execution times.
3. Read both SSM CloudWatch stdout and stderr streams plus relevant event-consumer/dispatcher logs around the job window.
4. Read EC2/fleet lifecycle history sufficient to identify provisioning, stop, termination, or infrastructure loss.
5. Reconstruct queue wait, provisioning, execution, last observed activity, configured max runtime, and whether another job/process held the dedicated slot.
6. Sanitize evidence before placing it in investigation notes. Record retention-expired or unavailable sources explicitly.

This procedure performs no updates, command sends, instance actions, deployment, artifact publication, or live build. If retained evidence shows response code 127/path-not-found, the path hypothesis is confirmed; otherwise the implemented correction must follow the evidence found. For the runtime case, adjust budgets only after the timing reconstruction distinguishes active progress, stall, queue wait, provisioning, and hard-ceiling exhaustion.

### Ephemeral Volume Sizing, Disk Diagnostics, and Tail-Preserving Messages

**Volume sizing (2.20)**: raise `DEFAULT_VOLUME_SIZE_GB` from 100 to 200 in `build_config.py`; optionally add the validated `volume_size_gb_by_target` map (target -> volume GB, `JP6` resolving to at least 200) following the runtime-budget pattern. Resolution happens at submission and is snapshotted; `plan_runner` in `build_planner.py` keeps reading `config_snapshot.volume_size_gb` unchanged. `BuildInfrastructureSettings.tsx` continues to expose `volume_size_gb` and adds the optional per-target entries with help text. No production configuration is mutated in design or validation; the new default applies to jobs created after the task 12 deployment gate.

**ENOSPC classification (2.21)**: extend the deterministic classification table in `build_reconciliation.py` (the task 4.2 seam) with `RUNNER_DISK_FULL`, detected from ENOSPC patterns (`no space left on device`, ENOSPC) in agent error messages, stderr, or invocation output. The agent may also report `error_kind=disk` directly in its terminal callback, which classification honors without pattern matching. Detection is pure and testable alongside the existing rows.

**Tail-preserving truncation (2.22)**: `scripts/portal-build-agent.sh` changes its error-tail derivation from `tail -n 5 "$BUILD_LOG" | head -c 512` to tail-preserving semantics (`tail -n 5 "$BUILD_LOG" | tail -c 512`), keeping the END of over-length content so causes like `no space left on device` survive. The backend byte-bounding primitives (task 4.1) already preserve useful head and tail with a truncation marker; durable message derivation through those primitives must preserve the root-cause end of each bounded line. Lines within the bound are unchanged (3.15), and all existing size and redaction limits still apply.

**Disk preflight and diagnostics (2.23)**: the preflight contract (task 7.1 seam) gains the disk-capacity recording check above, and `execution_diagnostic` gains optional disk-usage fields (for example `disk: {docker_storage_path, available_gb, used_gb, total_gb, measured_at, available: true|false}`), marked `{available: false}` when not measured rather than fabricated.

### Changes Required

Assuming exploration confirms the decision seams above, implementation is limited to:

- **`edge-cv-portal/backend/functions/build_reconciliation.py` (new pure module)**: evidence normalization, redaction/bounding, precedence, diagnostic merge, timeout decisions, and terminal-effect planning; the classification table includes `RUNNER_DISK_FULL` detected from ENOSPC patterns or agent-reported `error_kind=disk`.
- **`edge-cv-portal/backend/functions/build_events.py`**: add read-only invocation retrieval, route all terminal SSM statuses including `Success`, persist/merge diagnostics, and delegate terminal decisions instead of immediate generic fallback.
- **`edge-cv-portal/backend/functions/build_dispatcher.py`**: scheduled command/settlement/effect reconciliation, preflight, attempt claims, ambiguous-send recovery, lease/hard-ceiling watchdog, verified cleanup, and promotion wakeup. Preserve current allocation/serialization code paths.
- **`edge-cv-portal/backend/functions/build_planner.py`**: replace single elapsed-runtime decision with pure lifecycle budget/lease decisions while retaining `max_runtime_hours` fallback and strict boundary behavior; `plan_runner` continues to read `config_snapshot.volume_size_gb` unchanged.
- **`edge-cv-portal/backend/functions/build_domain.py` and `build_config.py`**: validate optional target/mode budget maps and snapshot them per job; preserve existing defaults and accepted requests. Raise `DEFAULT_VOLUME_SIZE_GB` from 100 to 200 and validate the optional per-target `volume_size_gb_by_target` map (`JP6` >= 200 GB), resolved at submission and snapshotted.
- **`edge-cv-portal/backend/functions/build_jobs.py`**: return optional diagnostic data with Build Log pages and detail, preserving `events`, `nextToken`, limits, and error envelopes.
- **`scripts/portal-build-agent.sh` and `portal-build.sh`**: accept attempt identity, emit safe start/heartbeat/progress/terminal timestamps and sequences, and cleanly stop heartbeat monitoring. Preserve target arguments and artifact result format. Change error-tail derivation to tail-preserving truncation (keep the END of over-length lines) and optionally report `error_kind=disk` when ENOSPC is detected locally.
- **`edge-cv-portal/backend/functions/build_fleet.py` / fleet registration**: report or persist the actual clone path so dispatch does not depend on a conflicting default.
- **`edge-cv-portal/infrastructure/lib/build-fleet-stack.ts`**: configure one consistent repository path, allow the event consumer only the operational read action required for invocation retrieval, include `Success` in the SSM event rule, and retain the one-minute scheduled reconciliation. This is service execution wiring, not user RBAC.
- **`edge-cv-portal/frontend/src/pages/builds/types.ts`, `BuildDetail.tsx`, and tests**: add optional diagnostic/timing types and actionable rendering while preserving existing pages.
- **`BuildInfrastructureSettings.tsx` and config tests**: expose validated optional target/mode budgets without removing the global compatibility field; help text explains queue, provisioning, stall, and hard-runtime semantics. Keep exposing `volume_size_gb` (default now 200) and add the optional per-target volume-size entries.
- **Preflight seam (task 7.1, dispatched via `build_dispatcher.py` and executed by the agent)**: record available disk capacity for the docker storage path and repository/`tmp` volume; persist the measurement in redacted diagnostics with unavailable measurements marked, not fabricated.

No production code is changed during this design phase, and no `tasks.md` is created.

## Testing Strategy

### Validation Approach

Use a two-phase bugfix strategy: first add focused exploration tests that fail on the current implementation and capture retained evidence if available; only then implement and run fix checking plus preservation checking. All clocks, service responses, delivery permutations, and process outcomes are injected. No test requires live AWS compute.

### Exploratory Bug Condition Checking

**Goal**: prove the current loss/misclassification and determine whether the path mismatch and timeout hypothesis match evidence.

**Failing Exploration Test**:

1. Seed a `building` dedicated AMD64 job with a persisted command/instance ID and no terminal callback.
2. Stub final invocation as `Failed`, `StatusDetails=Failed`, `ResponseCode=127`, empty stdout, and nonempty stderr naming a missing agent path; include secret canaries.
3. Deliver the real SSM EventBridge failure shape through `build_events.handler`.
4. Call `get_build_logs` with the CloudWatch stream missing.
5. Assert the job/API contain a redacted structured diagnostic, stderr, response code, path-specific classification, and no secret canary.

This test fails on current code because `build_events.py` has no SSM retrieval, persists only generic `AGENT_COMMAND_FAILED`, and `get_build_logs` returns an empty page.

**Additional Exploration Cases**:

1. SSM `Success` with no callback: current EventBridge rule/consumer does not reconcile it to `AGENT_RESULT_MISSING`.
2. Failure event followed by a valid terminal callback: current terminal absorption prevents result recovery and diagnostic merge.
3. Missing SSM event followed by scheduled tick: current dispatcher does not inspect the command ID.
4. Active build beyond a soft interval with fresh progress and below hard ceiling: current single `started_at`/max-runtime model cannot represent the distinction and fails once the global limit is crossed.
5. Queued-behind-job and slow-provisioning records: verify they do not currently consume active runtime, while demonstrating that their separate durations/reasons are not recorded sufficiently for diagnosis.
6. Read-only historical reconstruction: compare observed path, response code, timing, queue occupancy, provisioning state, and log growth to the hypotheses. If evidence refutes a hypothesis, revise the correction before implementation.
7. ENOSPC misclassification: feed agent/invocation output containing `no space left on device` through current classification and assert it currently collapses to generic `BUILD_FAILED` with no disk evidence recorded.
8. Head-keeping truncation loss: derive the durable message from a fixture reproducing the observed over-length buildkit failure line via the current `tail -n 5 | head -c 512` semantics and assert the trailing `no space left on device` cause is currently dropped (matching the retained record ending at `write /var/snap/docker/common/`).

**Expected Counterexamples**:
- available stderr/status details are absent from both job and Build Log response;
- `Success` without callback remains unresolved or is later misclassified by an unrelated watchdog;
- delivery order changes diagnostic completeness;
- active progress cannot renew a lease, and the timeout message names only the global duration;
- queue/provisioning durations cannot be separated from the user-visible timeline;
- ENOSPC failures classify as generic `BUILD_FAILED` and the durable message ends mid-path, hiding the root cause that survives only in CloudWatch.

### Fix Checking

**Goal**: for all bug-condition inputs, prove the reconciled result satisfies the expected predicate.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := reconcileAndFinalize_fixed(input)
  ASSERT expectedBehavior(result)
END FOR
```

Use the exact failing exploration fixture as the first regression. Then cover every classification row, missing/partial invocation fields, empty/delayed CloudWatch, settlement expiry, late valid result, historical field unavailability, progress/stall/hard-ceiling distinctions, and cleanup/promotion completion.

### Preservation Checking

**Goal**: prove inputs outside the bug condition retain original behavior.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT observableBehavior(original(input))
       = observableBehavior(fixed(input))
END FOR
```

`observableBehavior` excludes only new optional diagnostic/timing fields and includes status/result/error, artifacts, existing API fields/tokens, audit meaning, dispatch count, server lock, process count, runner lifecycle, queue ordering, and cancellation response.

**Preservation Tests**:

1. Run existing phase idempotence/audit tests unchanged for building, publishing, success, build failure, and partial publish failure.
2. Run existing dedicated allocation, pre-dispatch deferral, serialization watchdog, cancellation, ephemeral one-to-one, runner cleanup, request validation, config snapshot, and history/log pagination tests unchanged.
3. For each valid target/mode pair, compare generated command target mapping, component identity, architecture, and result handling before/after.
4. Verify existing UI rendering and API clients work when `diagnostic` and new timing fields are absent.
5. Verify unrelated SSM commands/events and terminal jobs without missing diagnostics are no-ops.
6. Run existing RBAC tests unchanged; add no role or navigation expectations.
7. Verify disk-unrelated failures with short or in-bound error lines keep their existing durable messages, bounds, and redaction; verify existing jobs' snapshotted `volume_size_gb`, instance type, and spot semantics are honored unchanged; verify successful builds are unaffected by the additive disk recording.

### Unit Tests

- Redaction grammar, recursive normalization, Unicode byte limits, head/tail truncation, and unavailable-vs-empty representation.
- Complete classification table and precedence at settlement/hard-deadline boundaries.
- Runtime accounting for queued, provisioning, active, heartbeat-only, progress, stall, and hard ceiling.
- Target/mode budget fallback and immutable snapshot behavior.
- Preflight checks and command quoting, especially dedicated AMD64 path and `AMD64 -> x86_64` mapping.
- Diagnostic merge after terminal status without outcome/result mutation.
- Build Log response compatibility and UI rendering for stdout-only, stderr-only, status-only, empty, truncated, and unavailable evidence.
- Volume-size resolution: raised 200 GB default, per-target map validation, `JP6 >= 200` enforcement, immutable snapshots, and unchanged `plan_runner` consumption.
- ENOSPC pattern detection and `error_kind=disk` shortcut against representative buildkit/docker/agent output, including non-matching output.
- Tail-preserving derivation for over-length, exactly-at-bound, and short lines, including redaction interaction.
- Disk preflight recording and unavailable-measurement marking in the diagnostic.

### Property-Based Tests

- **Property 1/3**: generate terminal invocation fields, missingness, arbitrary output, and secret canaries; assert useful bounded redacted diagnostics.
- **Property 4/5**: generate callback/SSM/lifecycle evidence, all delivery permutations, duplicates, and omitted EventBridge events; assert convergence.
- **Property 6/10**: generate concurrent terminal writers, conditional-write losses, cleanup retries, and promotion ticks; assert one effect ID, one effective cleanup/promotion, and no duplicate execution.
- **Property 7**: generate times immediately below, at, and above every hard deadline for target/mode budgets.
- **Property 8**: generate heartbeat/progress sequences including duplicate, stale, missing, and increasing sequences; assert lease semantics.
- **Property 9**: generate arbitrary queue/provisioning delays and optional independent budgets; assert zero active runtime before execution start and preserved serialization/order.
- **Property 11**: generate the target/mode matrix, valid/invalid server architectures, callback order, and terminal causes; assert mappings and intentional restrictions.
- **Property 2**: differential-test non-bug inputs against captured original behavior.
- **Property 16**: generate submission requests with and without explicit/per-target volume sizes plus pre-existing snapshotted jobs; assert 200 GB default resolution, `JP6 >= 200`, snapshot immutability, and unchanged existing-job sizes.
- **Property 17**: generate outputs with embedded ENOSPC patterns at arbitrary positions, `error_kind=disk` callbacks, and disk-free outputs; assert `RUNNER_DISK_FULL` exactly when disk evidence exists and never otherwise.
- **Property 18**: generate failure lines of arbitrary content and length around the byte bound; assert over-length lines retain their trailing content in the durable bounded redacted message and in-bound lines are unchanged.

Each PBT is tagged with its `Property N` text and exact requirement references from the Correctness Properties section.

### Integration Tests

- Moto/fake-service flow from dedicated dispatch through preflight, command event, invocation retrieval, Build Job persistence, Build Log API, terminal effects, server release, and oldest follower dispatch.
- Equivalent ephemeral flow including provisioning timing and idempotent termination.
- EventBridge suppressed flow resolved by scheduled reconciliation within the injected bound.
- SSM `Success` then delayed callback inside/outside settlement, and failure/callback races in both orders.
- Timeout flows for active progress, heartbeat-only stall, no heartbeat, queue wait, provisioning, and hard ceiling; confirm stop before release and follower promotion only after cleanup verification.
- Frontend tests render actionable diagnostics and timing while preserving existing empty/running/success views.
- Full target/mode matrix through preflight and cleanup, without executing real builds.
- Ephemeral disk-exhaustion flow: submission resolves and snapshots the 200 GB volume, preflight records disk capacity, an injected ENOSPC failure classifies `RUNNER_DISK_FULL` with disk evidence and a tail-preserved durable message, and terminal cleanup/promotion behave exactly once; mocked services only, no real volumes or builds.

### Approval-Gated Live Verification

Verification order is mandatory:

1. static diagnostics and unchanged existing suites;
2. failing exploration test on unfixed behavior;
3. fixed unit/property/integration tests with mocked services;
4. read-only retained historical evidence inspection;
5. only after explicit user approval: deploy to an approved environment and run one narrowly scoped build.

The approval request must state target, mode, server/instance, estimated duration/cost, artifact publication effects, timeout budget, cleanup plan, and rollback. Without explicit approval, do not deploy, send SSM commands, start/stop instances, publish artifacts, or launch a build. The same gate covers the storage facet: design and validation mutate no production setting, and the raised 200 GB volume default becomes effective for new jobs only after the approved task 12 deployment. If approved, first run preflight, then monitor queue/provisioning/execution timers, heartbeat/progress, SSM invocation, Build Log diagnostics, cleanup, and one queued follower. Redact captured evidence and stop after the approved matrix; broader JP5/JP6 1–2+ hour source-build verification requires separate explicit approval.