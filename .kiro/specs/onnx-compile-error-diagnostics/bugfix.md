# Bugfix Requirements Document

## Introduction

A user compiled a vision model to the `onnx` target through the portal
(`https://d23v4ltibogb5x.cloudfront.net/models/f182a10d-0da7-420a-943c-c370da7ee623`)
and the UI shows only the bare word `ERROR` with no diagnostic detail — no
reason, no failure message, no hint about what to do next. The reported
training_id is treated here as the reported *instance*, not as a reproducible
fixture: the defects below are structural and reproduce from any ONNX export
start failure.

The genuine reason the ONNX export failed to start is **not merely
unreported — it is destroyed**, by the portal's own code, on the first status
poll after the failure. Five compounding defects, all in the compilation
status path, turn a diagnosable start failure into an opaque token:

1. **The placeholder entry omits `export_format`, so it is polled with the
   wrong SageMaker API.** In `edge-cv-portal/backend/functions/compilation.py`,
   `start_compilation_job`'s `onnx` branch (~line 544-565) wraps
   `_start_onnx_export_job` in `try/except Exception` and on failure appends
   `{'target': 'onnx', 'compilation_job_name': f"{safe_model_name}-onnx-failed",
   'status': 'Failed', 'error': str(e)}`. That entry carries no `export_format`
   key. `get_compilation_status` branches on `if job.get('export_format') ==
   'onnx'` to choose `describe_training_job` over `describe_compilation_job`,
   so with the key absent the entry falls through to
   `describe_compilation_job(CompilationJobName='<model>-onnx-failed')`. That
   name is a sentinel — no such job was ever created — so botocore raises
   `ClientError` / `ValidationException`.

2. **The `ClientError` handler destroys the genuine root-cause error.** The
   handler in `get_compilation_status` does `job['status'] = 'ERROR'` and
   `job['error'] = str(e)`, overwriting both the `'Failed'` status and,
   critically, the real error string captured at creation time. `updated_jobs`
   is then written straight back to DynamoDB via `table.update_item`, so the
   actual reason the ONNX export failed to start is **permanently** destroyed
   on the FIRST status poll — and every subsequent poll re-destroys it. What
   the user is left with is a self-inflicted "compilation job not found" error,
   surfaced as the bare literal `ERROR`. The same handler behaves identically
   for a *transient* describe failure (throttling, expired assumed-role
   credentials) against a genuine, healthy job.

3. **`'ERROR'` is not in any layer's status vocabulary.**
   `derive_compilation_status` recognizes only `STARTING`/`INPROGRESS`/
   `IN_PROGRESS` (running) and `COMPLETED`; `'ERROR'` falls through the
   catch-all to overall `'Failed'`. Its docstring claims a codomain of
   `'InProgress' | 'Completed' | 'Failed'`, yet the per-job entries it reads
   can hold `'ERROR'`, a value no other layer models: `models.py::get_model`
   re-implements the same derivation inline and its `TERMINAL` set
   (`COMPLETED`/`FAILED`/`STOPPED`) does not contain `ERROR`, so an
   `ERROR`-latched job is re-polled on every model-detail load forever; and
   the frontend `CompilationJob['status']` union in
   `edge-cv-portal/frontend/src/types/index.ts` does not list it either. A
   transient poll error is therefore indistinguishable from a genuine terminal
   compile failure, and it permanently latches the record to `Failed` *and*
   clobbers state.

4. **Both UI surfaces render a bare status token and hide the reason.** On the
   reported page (`ModelDetail.tsx`), the fallback compilation table's status
   cell returns `<Badge>{item.status}</Badge>` for anything that is not exactly
   `COMPLETED`/`INPROGRESS`/`FAILED` — that Badge is the literal `ERROR` the
   user saw — and its inline `compilation_jobs` type declares only
   `{target, status, compiled_model_s3}`, so it has no `error` /
   `failure_reason` field to render even if one survived. `CompilationTab.tsx`
   does render both fields, but only inside a panel gated on
   `job.status === 'Failed'` — an exact, case-sensitive match that excludes
   `'ERROR'` *and* excludes the uppercase `'FAILED'` the Neo path actually
   writes, and its `getStatusIndicator` default arm likewise renders the bare
   token.

5. **`models.py::get_model` polls ONNX export jobs with the Neo API.** The
   model-detail sync loop calls `describe_compilation_job` for every non-terminal
   job with no `export_format` branch at all, so even a *successfully started*
   ONNX export training job is polled with the wrong API. The exception is
   caught and warned, so nothing is clobbered, but the job's status can never
   advance from the model detail page — which is the page the reported URL
   renders.

**Contributing / likely-trigger causes (to investigate, not the primary fix).**
`_start_onnx_export_job` hardcodes
`role_arn = f"arn:aws:iam::{account_id}:role/DDASageMakerExecutionRole"`, and
`ONNX_EXPORT_IMAGE` defaults to a region-pinned
`763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:1.13.1-cpu-py39`.
Either can make `_start_onnx_export_job` raise, which is the trigger for
Defect 1. They are in scope to *investigate and name*, not to fix here: the
primary fix is that whatever the cause, the failure must remain diagnosable.

**IAM is not the cause of the poll failure.** `compute-stack.ts` grants
`DescribeTrainingJob` / `DescribeCompilationJob` on unscoped
`arn:aws:sagemaker:*:*:training-job/*` and `compilation-job/*`, so the
`ClientError` is a `ValidationException` on a non-existent job name, not an
authorization denial.

**Why this matters for JP7.** The ONNX export path is the designated route for
vision models on JetPack 7, which raises the severity of these diagnostic
defects from cosmetic to blocking. `.kiro/specs/jetpack7-support/design.md`
records that the JP6 CUDA 11.4 cudart + TensorRT 8 staging stages are **not**
carried to JP7 — their transitive L4T driver dependencies do not exist on Thor
— so "DLR-only models are not supported on JP7", while GPU onnxruntime is
enabled by default. SageMaker Neo cannot close that gap either: its NVIDIA path
rejects `cuda-ver` 12.x and higher (ceiling 11.x), as documented in the
`jetson-xavier-jp6` comment in `COMPILATION_TARGETS`. A vision model reaching a
JP7 device therefore has to go through `onnx`, and an undiagnosable ONNX export
failure blocks that route with no path forward for the operator.

**Non-goals.** There is deliberately NO `jetson-xavier-jp7` compile target and
this spec MUST NOT add one (verified absent from `COMPILATION_TARGETS`; the
`jetson-xavier-jp7` string exists only as a *packaging* target in
`packaging.py` / `workflow_packaging.py`). This spec does not implement JP7
vision support, does not change the Neo compile path for any target, does not
change what `_start_onnx_export_job` submits to SageMaker, and does not attempt
to make the reported training_id reproduce.

> **Amendment note** (see `.kiro/specs/onnx-jetson-publish-packaging/`): the
> JP7 vision route deferred here is now delivered by that sibling spec, which
> changes `packaging.py`, `workflow_packaging.py`, and `greengrass_publish.py`
> (per-JetPack compiled-ONNX components). This spec's diagnostics contracts —
> the write-once `error` / `failure_reason` invariant, `classify_poll_kind`
> routing, and case 9's no-JP7-Neo-compile-target guard — are preserved
> untouched: `COMPILATION_TARGETS` still gains no `jetson-xavier-jp7` entry.

## Bug Analysis

### Current Behavior (Defect)

**Defect 1 — placeholder entry omits `export_format`, so it is polled with the wrong API**

1.1 WHEN `_start_onnx_export_job` raises during `start_compilation_job` THEN
the system appends a placeholder `compilation_jobs` entry carrying `target`,
`compilation_job_name`, `status: 'Failed'`, and `error`, but no
`export_format` key

1.2 WHEN `get_compilation_status` polls that placeholder entry THEN the system
evaluates `job.get('export_format') == 'onnx'` as false and takes the
`describe_compilation_job` branch, the SageMaker Neo API, for what was never a
Neo job

1.3 WHEN `describe_compilation_job` is called with the fabricated sentinel name
`f"{safe_model_name}-onnx-failed"` THEN the system raises `ClientError` /
`ValidationException`, because no compilation job by that name was ever created

1.4 WHEN the sentinel name is persisted to DynamoDB THEN the system stores it in
the same `compilation_job_name` field a real job name occupies, so no layer can
tell a record with no live SageMaker job from one that has one

**Defect 2 — the `ClientError` handler destroys the genuine root-cause error**

1.5 WHEN any `describe_*` call inside `get_compilation_status` raises
`ClientError` THEN the system sets `job['status'] = 'ERROR'` and
`job['error'] = str(e)`, overwriting the `'Failed'` status and the real error
string captured at creation time

1.6 WHEN the loop completes THEN the system writes `updated_jobs` straight back
to DynamoDB via `table.update_item`, so the overwrite is permanent — the
originating error is destroyed on the FIRST status poll and cannot be recovered
from the record

1.7 WHEN the user reads the record after that first poll THEN the system reports
a self-inflicted "compilation job not found" error in place of the reason the
ONNX export actually failed to start

1.8 WHEN a poll fails transiently (throttling, expired assumed-role credentials,
a network fault) against a genuine, healthy job THEN the system applies the same
overwrite, discarding that job's real status and any previously captured
`failure_reason`

1.9 WHEN the record is polled repeatedly THEN the system re-destroys state on
every poll, so no number of retries or refreshes can ever recover the
originating reason

**Defect 3 — `'ERROR'` is not in any layer's status vocabulary**

1.10 WHEN `derive_compilation_status` receives a job whose status is `'ERROR'`
THEN the system matches neither the running set (`STARTING`/`INPROGRESS`/
`IN_PROGRESS`) nor `COMPLETED`, and falls through the silent catch-all to an
overall `'Failed'`

1.11 WHEN `derive_compilation_status`'s docstring claims a codomain of
`'InProgress' | 'Completed' | 'Failed'` THEN the system nonetheless persists
per-job status values (`'ERROR'`) outside the vocabulary the docstring
enumerates, with no validation at the write boundary

1.12 WHEN `models.py::get_model` derives the overall status THEN the system
re-implements `derive_compilation_status`'s rules inline rather than calling it,
so the two derivations can drift and both must be fixed for any status value to
be handled consistently

1.13 WHEN `models.py::get_model` selects jobs to sync THEN the system tests
`str(job['status']).upper() not in {'COMPLETED', 'FAILED', 'STOPPED'}`, which
`'ERROR'` satisfies, so an `ERROR`-latched job is re-polled on every model-detail
load indefinitely

1.14 WHEN the frontend types a compilation job THEN the system declares
`CompilationJob['status']` as a closed union of eight values that does not
include `'ERROR'`, so the value the poller actually writes is untyped at the
client boundary

1.15 WHEN a transient describe failure occurs among otherwise-running jobs THEN
the system produces an overall `'Failed'` and latches the record there, making a
recoverable poll fault indistinguishable from a genuine terminal compile failure

**Defect 4 — both UI surfaces render a bare status token and hide the reason**

1.16 WHEN `ModelDetail.tsx` renders its fallback compilation-jobs table for a
job whose status is not exactly `COMPLETED`, `INPROGRESS`, or `FAILED` THEN the
system renders `<Badge>{item.status}</Badge>` — the bare word `ERROR` the user
reported

1.17 WHEN `ModelDetail.tsx` types its inline `compilation_jobs` array THEN the
system declares only `{target, status, compiled_model_s3}`, so that surface has
no `error` or `failure_reason` field to render even when one is present on the
record

1.18 WHEN `CompilationTab.tsx` renders its "Compilation Errors" panel THEN the
system filters on `job.status === 'Failed'`, an exact case-sensitive match, so a
job whose status is `'ERROR'` never renders its `error` or `failure_reason`
despite the panel supporting both fields

1.19 WHEN the Neo path writes the uppercase `'FAILED'` that
`describe_compilation_job` returns THEN the system's same exact-match filter
excludes it too, so Neo failures also render no reason, and
`getStatusIndicator`'s default arm renders the bare token `FAILED`

**Defect 5 — `models.py::get_model` polls ONNX export jobs with the Neo API**

1.20 WHEN `models.py::get_model` syncs a non-terminal job THEN the system calls
`describe_compilation_job` unconditionally, with no `export_format` branch, so
even a successfully started ONNX export *training* job is polled with the Neo
API

1.21 WHEN that call fails THEN the system catches the exception and logs a
warning without changing the job, so the ONNX export's status can never advance
from the model detail page — the page the reported URL renders

### Expected Behavior (Correct)

**Fix 1 — a record with no live SageMaker job is recognizable and is not described**

2.1 WHEN `_start_onnx_export_job` fails to start the export THEN the system SHALL
record the failure without fabricating a `compilation_job_name` for a job that
does not exist, marking the entry explicitly as having no live SageMaker job
(e.g. a `job_started: false` / no-live-job marker) so the poller can recognize it
without string matching on a sentinel

2.2 WHEN the poller encounters an entry with no live SageMaker job THEN the system
SHALL skip it — issuing no `describe_compilation_job` and no
`describe_training_job` call — and SHALL leave its recorded status and reason
byte-for-byte unchanged

2.3 WHEN an entry for the `onnx` target IS backed by a live SageMaker training
job THEN the system SHALL carry `export_format: 'onnx'` so it is polled with
`describe_training_job`, and every entry the poller writes back SHALL carry
whichever of these markers its own branching requires (no entry may be written
that the poller cannot subsequently classify)

**Fix 2 — the originating reason survives arbitrarily many polls**

2.4 WHEN a status poll fails for an entry that already carries a recorded reason
THEN the system SHALL preserve that reason, writing any poll-time diagnostic to a
distinct field rather than overwriting `error`

2.5 WHEN a record created via the ONNX start-failure path is polled N ≥ 1 times
THEN the system SHALL still report the originating error string after the Nth
poll, for any N and any error string

2.6 WHEN a poll fails for an entry whose recorded status is already terminal
(`Failed`) THEN the system SHALL NOT overwrite that terminal status with a
poll-time status

2.7 WHEN a poll failure is recorded THEN the system SHALL distinguish a transient
describe failure (throttling, expired credentials, a network fault) from a
genuine terminal compile failure, and SHALL NOT latch the record to a terminal
state on a transient one

**Fix 3 — total status handling, one derivation, no silent catch-all**

2.8 WHEN `derive_compilation_status` receives any per-job status value the poller
can actually write THEN the system SHALL map it explicitly, with no silent
catch-all that maps an unmodeled value to `Failed`, and SHALL return a value
inside its documented codomain for every input

2.9 WHEN a status value outside the modeled vocabulary reaches
`derive_compilation_status` THEN the system SHALL surface it as an explicit,
named outcome (logged, and distinguishable from a genuine terminal failure)
rather than silently collapsing it to `Failed`

2.10 WHEN a single transient describe failure occurs among otherwise-running jobs
THEN the system SHALL NOT return an overall `'Failed'`

2.11 WHEN the overall compilation status is derived anywhere in the backend THEN
the system SHALL use one shared implementation — `models.py` SHALL call
`derive_compilation_status` rather than re-implementing its rules inline

2.12 WHEN `models.py::get_model` selects jobs to sync THEN the system SHALL treat
every terminal value the poller can write as terminal, so a terminally failed
job is not re-polled on every model-detail load

2.13 WHEN the frontend types a compilation job status THEN the system SHALL model
every value the poller can write, so no value the backend persists is untyped at
the client boundary

**Fix 4 — the UI surfaces the preserved reason, not a bare status token**

2.14 WHEN either UI surface renders a compilation job that carries a reason
(`error` or `failure_reason`) THEN the system SHALL display that reason, never a
bare status token alone

2.15 WHEN `ModelDetail.tsx` renders its fallback compilation-jobs table THEN its
inline `compilation_jobs` type SHALL include the `error` and `failure_reason`
fields and the table SHALL render them

2.16 WHEN either surface compares a job status THEN the system SHALL compare
case-insensitively, so the uppercase values the backend actually writes
(`FAILED`, `COMPLETED`, `INPROGRESS`) and any newly modeled value are matched by
the same rules

2.17 WHEN a job's status is an unmodeled or diagnostic value THEN the system
SHALL render it with an explanatory label and the preserved reason, rather than
the raw token

**Fix 5 — the model-detail poller uses the right API per entry**

2.18 WHEN `models.py::get_model` syncs a job THEN the system SHALL select the
describe API from the entry's own markers — `describe_training_job` for an
`export_format: 'onnx'` entry, `describe_compilation_job` for a Neo entry — and
SHALL skip an entry with no live SageMaker job entirely

2.19 WHEN `models.py::get_model` cannot advance a job's status THEN the system
SHALL leave the recorded reason intact, preserving the non-destructive
warn-and-continue behavior it has today

**Fix 6 — name the likely trigger**

2.20 WHEN the ONNX export fails to start because of the hardcoded
`DDASageMakerExecutionRole` ARN or the region-pinned `ONNX_EXPORT_IMAGE` default
THEN the system SHALL make the preserved reason sufficient to identify which of
the two it was, so the operator can act on it without console archaeology.
Investigating and naming these is in scope; changing the role-resolution or
image-resolution logic is NOT.

### Unchanged Behavior (Regression Prevention)

**The Neo (non-ONNX) compile path**

3.1 WHEN `start_compilation_job` is invoked for any of `jetson-xavier`,
`jetson-xavier-jp5`, `jetson-xavier-jp6`, `x86_64-cpu`, `x86_64-cuda`, or
`arm64-cpu` THEN the system SHALL CONTINUE TO produce byte-for-byte identical
`create_compilation_job` arguments — job name derivation and 63-character
truncation, `OutputConfig` (`S3OutputLocation`, `TargetPlatform` Os/Arch,
`Accelerator`, `CompilerOptions`), `InputConfig` (`S3Uri`, `DataInputConfig`,
`Framework` PYTORCH, `FrameworkVersion` 1.8), and the 3600-second
`StoppingCondition`

3.2 WHEN a Neo target's compilation job is polled THEN the system SHALL CONTINUE
TO call `describe_compilation_job`, record the raw uppercase
`CompilationJobStatus`, set `compiled_model_s3` from
`ModelArtifacts.S3ModelArtifacts` on `COMPLETED`, and capture
`failure_reason = response.get('FailureReason', 'Unknown')` on `FAILED`

3.3 WHEN every `COMPILATION_TARGETS` entry is read THEN the system SHALL
CONTINUE TO expose exactly the seven targets it exposes today with identical
`os` / `arch` / `accelerator` / `compiler_options` values — in particular the
`jetson-xavier-jp5` and `jetson-xavier-jp6` `cuda-ver` 11.4 / `trt-ver` 8.5.2 /
`gpu-code` sm_72 triples — and SHALL NOT gain a `jetson-xavier-jp7` target

3.4 WHEN an invalid target is requested THEN the system SHALL CONTINUE TO return
400 naming the invalid targets and the valid target list

**The ONNX export start path**

3.5 WHEN `_start_onnx_export_job` succeeds THEN the system SHALL CONTINUE TO
submit an identical `create_training_job` request — job name derivation,
`ONNX_EXPORT_IMAGE`, `sagemaker_program` / `sagemaker_submit_directory`
hyperparameters, `INPUT_SHAPE` / `ONNX_OPSET` in both HyperParameters and
Environment, the `model` input channel, `OutputDataConfig`, `ml.m5.large`
ResourceConfig, and the 1800-second `StoppingCondition` — and SHALL CONTINUE TO
return an entry carrying `export_format: 'onnx'` and `status: 'InProgress'`

3.6 WHEN a live ONNX export training job is polled by `get_compilation_status`
THEN the system SHALL CONTINUE TO call `describe_training_job`, record
`TrainingJobStatus` verbatim, set `compiled_model_s3` from
`ModelArtifacts.S3ModelArtifacts` on `Completed`, and capture `failure_reason`
on `Failed`

3.7 WHEN a target fails to start THEN the system SHALL CONTINUE TO record that
target's failure and CONTINUE processing the remaining targets — one target's
failure never aborts the others, and the request still returns 200 with the
`compilation_jobs` list

**Imported ONNX bypass and request-level behavior**

3.8 WHEN `_is_onnx_import` identifies an imported BYO ONNX model THEN the system
SHALL CONTINUE TO skip Neo compilation, set `compilation_skipped = True`, and
return 200 with an empty `compilation_jobs` list and the existing "proceed to
packaging" message

3.9 WHEN a caller lacks the `DataScientist` role on a non-auto-triggered request
THEN the system SHALL CONTINUE TO return 403, and WHEN the training job is not
`Completed` the system SHALL CONTINUE TO return 400 with the current status in
the message

3.10 WHEN `start_compilation_job` raises a SageMaker `ClientError` THEN the
system SHALL CONTINUE TO map it to today's exact status codes and messages: the
`CompilationJobName` length hint, the `Member` → `Field` rewrites, 403 on
`AccessDenied`, 429 on `ResourceLimitExceeded`, and 500 otherwise

3.11 WHEN a compilation start or status poll completes THEN the system SHALL
CONTINUE TO write the same DynamoDB attributes (`compilation_jobs`,
`compilation_status`, `updated_at`) with the same update expression, and SHALL
CONTINUE TO log the `start_compilation` audit event with the same fields

3.12 WHEN `get_compilation_status` runs with no compilation jobs on the record
THEN the system SHALL CONTINUE TO return 404 with "No compilation jobs found for
this training", and SHALL CONTINUE TO return 403 when the caller lacks use-case
access

**Derivation semantics for values already modeled**

3.13 WHEN `derive_compilation_status` receives an empty or absent job list THEN
the system SHALL CONTINUE TO return `None`

3.14 WHEN every job is `COMPLETED` THEN the system SHALL CONTINUE TO return
`'Completed'`; WHEN any job is `STARTING` / `INPROGRESS` / `IN_PROGRESS` the
system SHALL CONTINUE TO return `'InProgress'`; WHEN jobs are terminal and at
least one is genuinely `FAILED` or `STOPPED` the system SHALL CONTINUE TO return
`'Failed'`

3.15 WHEN status values arrive in either case (`INPROGRESS` from SageMaker or
`InProgress` from the portal) THEN the system SHALL CONTINUE TO treat them
identically through the existing `.upper()` normalization

**Frontend behavior outside the diagnostics change**

3.16 WHEN `CompilationTab.tsx` polls THEN the system SHALL CONTINUE TO refresh
on mount, poll every 15 s while any job is non-terminal by its existing
case-insensitive `isNonTerminal` test, and stop once all jobs are terminal

3.17 WHEN `CompilationTab.tsx` renders the target-selection modal, the
package/publish actions, the version derivation and validation
(`isValidPublishVersion`, `bumpMajor`, the per-variant `startsWith` component
match), and the published-components panel THEN the system SHALL CONTINUE TO
behave exactly as today

3.18 WHEN either surface renders a `COMPLETED`, `INPROGRESS`, or successful job
THEN the system SHALL CONTINUE TO render the same indicator, artifact link, and
duration as today; the case-insensitive comparison of 2.16 SHALL change only
which *additional* statuses match, never reclassify one that matches today

3.19 WHEN the model detail page loads a model whose jobs are all terminal THEN
the system SHALL CONTINUE TO issue no SageMaker describe calls at all

**Scope containment**

3.20 WHEN this fix lands THEN `packaging.py`, `workflow_packaging.py`,
`greengrass_publish.py`, the deployment gates, and every recipe SHALL CONTINUE
TO behave identically — no packaging or publish path is touched

3.21 WHEN the SageMaker IAM grants are synthesized THEN `compute-stack.ts` SHALL
CONTINUE TO grant exactly the `DescribeTrainingJob` / `DescribeCompilationJob`
actions and resource scopes it grants today; this fix requires no IAM change and
SHALL introduce no drift in
`test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json`

3.22 WHEN `_start_onnx_export_job` resolves its execution role and training
image THEN the system SHALL CONTINUE TO use the existing hardcoded
`DDASageMakerExecutionRole` ARN and the existing `ONNX_EXPORT_IMAGE`
default/override — these are named as likely triggers, not fixed here (2.20)

3.23 WHEN the `dda-portal-compilation-state-change` EventBridge rule delivers a
"SageMaker Compilation Job State Change" event to `compilation_events.py` THEN
the system SHALL CONTINUE TO match the record's `compilation_jobs` entry by
`compilation_job_name`, normalize the status to uppercase, capture
`failure_reason`, chain packaging on `COMPLETED`, publish the failure alert on
`FAILED`, and write only `compilation_jobs` + `updated_at` — removing the
fabricated `{safe_model_name}-onnx-failed` name cannot regress this handler
because no such SageMaker job ever emitted an event for it to match

3.24 WHEN an ONNX export training job changes state THEN the system SHALL
CONTINUE TO deliver that event to `training_events.py`, whose name scan finds no
record with a matching `training_job_name`, so the ONNX export job SHALL CONTINUE
TO have no effect on any training record through that path

### Bug Conditions and Properties

**Key definitions.** `F` is the current (unfixed) code; `F'` is the fixed code.
`entry` is one element of a record's `compilation_jobs` list. `poll(record)` is
one invocation of `get_compilation_status`; `poll^N` is N successive
invocations. `reason(entry)` is the diagnostic string the record carries for
that entry (`error` or `failure_reason`). `liveJob(entry)` is true when a
SageMaker job (compilation or training) actually exists for that entry.

#### Defect 1 — placeholder entry polled with the wrong API

```pascal
FUNCTION isBugCondition_1(X)
  INPUT: X of type CompilationJobEntry
  OUTPUT: boolean

  // An entry the poller will describe even though no SageMaker job exists,
  // classically the '{safe_model_name}-onnx-failed' sentinel written when
  // _start_onnx_export_job raises: no export_format key, so the poller takes
  // the describe_compilation_job branch for a name that was never created.
  RETURN X.target = 'onnx'
     AND NOT liveJob(X)
     AND pollerWouldDescribe(X)
END FUNCTION
```

```pascal
// Property: Fix Checking - an entry with no live job is never described
FOR ALL X WHERE isBugCondition_1(X) DO
  calls ← describeCalls(poll'(recordWith(X)))
  ASSERT NOT EXISTS c IN calls WHERE c.targets(X)
  ASSERT statusOf'(X) = statusOf(X)
  ASSERT reason'(X)   = reason(X)
END FOR

// Property: Fix Checking - every entry the poller writes, the poller can read
FOR ALL X WHERE X IN entriesWrittenBy(poll'(record) OR start'(request)) DO
  ASSERT classifiable'(X)          // no-live-job | onnx-training | neo-compilation
  ASSERT poll'(recordWith(X)) RAISES nothing
END FOR
```

#### Defect 2 — the poll destroys the originating reason

```pascal
FUNCTION isBugCondition_2(X)
  INPUT: X of type (entry, pollOutcome)
  OUTPUT: boolean

  // Any entry carrying a recorded reason whose poll raises ClientError: the
  // handler overwrites status AND error, then update_item persists it.
  RETURN reason(X.entry) ≠ NULL
     AND X.pollOutcome IS ClientError
END FUNCTION
```

```pascal
// Property: Fix Checking - the originating reason survives arbitrarily many polls
FOR ALL e IN ErrorStrings, FOR ALL N >= 1 DO
  record ← startWithOnnxFailure'(e)          // _start_onnx_export_job raises e
  ASSERT reason'(onnxEntryOf(poll'^N(record))) CONTAINS e
END FOR

// Property: Fix Checking - a terminal status is never overwritten by a poll
FOR ALL X WHERE isBugCondition_2(X) DO
  ASSERT reason'(afterPoll'(X.entry)) = reason(X.entry)
  ASSERT isTerminal(statusOf(X.entry))
     IMPLIES statusOf'(afterPoll'(X.entry)) = statusOf(X.entry)
  ASSERT pollDiagnostic'(afterPoll'(X.entry)) ≠ NULL   // written elsewhere
  ASSERT transient(X.pollOutcome)
     IMPLIES NOT latchedTerminal'(afterPoll'(X.entry))
END FOR
```

#### Defect 3 — `'ERROR'` outside every layer's vocabulary

```pascal
FUNCTION isBugCondition_3(X)
  INPUT: X of type StatusValueSet          // per-job statuses the poller can emit
  OUTPUT: boolean

  // Some emitted value is not explicitly handled by the derivation and is
  // absorbed by the silent catch-all. Classically 'ERROR'.
  RETURN EXISTS s IN X WHERE s NOT IN modeledStatuses(derive_compilation_status)
END FUNCTION
```

```pascal
// Property: Fix Checking - derivation is total over what the poller can emit
FOR ALL X IN PowerSet(emittableStatuses') DO
  r ← derive_compilation_status'(X)
  ASSERT r IN {NULL, 'InProgress', 'Completed', 'Failed'} ∪ documentedCodomain'
  ASSERT EXISTS s IN X WHERE s NOT IN modeledStatuses'
     IMPLIES explicitlyNamed'(r)            // no silent collapse to 'Failed'
END FOR

// Property: Fix Checking - one transient failure among running jobs is not Failed
FOR ALL X WHERE (EXISTS s IN X WHERE s IN runningStatuses)
             AND |{s IN X WHERE transientStatus(s)}| = 1 DO
  ASSERT derive_compilation_status'(X) ≠ 'Failed'
END FOR

// Property: Fix Checking - one derivation, shared, and terminal means terminal
ASSERT models.deriveOverall' IS derive_compilation_status'
FOR ALL s IN emittableStatuses' WHERE isTerminal(s) DO
  ASSERT s IN terminalSet'(models.get_model)     // never re-polled forever
  ASSERT s IN typeUnion'(frontend.CompilationJob.status)
END FOR
```

#### Defect 4 — the UI shows a bare token and hides the reason

```pascal
FUNCTION isBugCondition_4(X)
  INPUT: X of type RenderedJob              // (surface, entry)
  OUTPUT: boolean

  // The entry carries a reason but the surface renders only a status token,
  // either because the status misses the exact-match arms or because the
  // surface's type has no reason field at all.
  RETURN reason(X.entry) ≠ NULL
     AND NOT rendersReason(X.surface, X.entry)
END FUNCTION
```

```pascal
// Property: Fix Checking - a preserved reason is always surfaced
FOR ALL X WHERE reason(X.entry) ≠ NULL DO
  FOR ALL surface IN {ModelDetail, CompilationTab} DO
    ASSERT rendersReason'(surface, X.entry)
    ASSERT NOT rendersBareTokenOnly'(surface, X.entry)
  END FOR
END FOR

// Property: Fix Checking - status matching is case-insensitive
FOR ALL s IN emittableStatuses', FOR ALL surface IN {ModelDetail, CompilationTab} DO
  ASSERT classify'(surface, s) = classify'(surface, upper(s))
                              = classify'(surface, lower(s))
END FOR
```

#### Defect 5 — the model-detail poller uses the Neo API for ONNX jobs

```pascal
FUNCTION isBugCondition_5(X)
  INPUT: X of type CompilationJobEntry
  OUTPUT: boolean

  // models.py::get_model describes every non-terminal entry with
  // describe_compilation_job, with no export_format branch.
  RETURN NOT isTerminal(statusOf(X))
     AND (X.export_format = 'onnx' OR NOT liveJob(X))
END FUNCTION
```

```pascal
// Property: Fix Checking - the API is selected from the entry's own markers
FOR ALL X WHERE NOT isTerminal(statusOf(X)) DO
  calls ← describeCalls(get_model'(recordWith(X)))
  ASSERT X.export_format = 'onnx'
     IMPLIES calls = [describe_training_job(X.compilation_job_name)]
  ASSERT liveJob(X) AND X.export_format ≠ 'onnx'
     IMPLIES calls = [describe_compilation_job(X.compilation_job_name)]
  ASSERT NOT liveJob(X) IMPLIES calls = []
  ASSERT reason'(afterSync'(X)) = reason(X)      // still non-destructive
END FOR
```

#### Preservation

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT (isBugCondition_1(X) OR isBugCondition_2(X)
                     OR isBugCondition_3(X) OR isBugCondition_4(X)
                     OR isBugCondition_5(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

Concretely, preservation covers:

```pascal
// The Neo compile path is byte-for-byte unchanged
FOR ALL t IN {'jetson-xavier', 'jetson-xavier-jp5', 'jetson-xavier-jp6',
              'x86_64-cpu', 'x86_64-cuda', 'arm64-cpu'} DO
  ASSERT createCompilationJobArgs'(t) = createCompilationJobArgs(t)
  ASSERT COMPILATION_TARGETS'[t]      = COMPILATION_TARGETS[t]
END FOR
ASSERT 'jetson-xavier-jp7' NOT IN keys(COMPILATION_TARGETS')

// Neo polling, including the FAILED -> failure_reason capture
FOR ALL X WHERE liveJob(X) AND X.export_format ≠ 'onnx' DO
  ASSERT poll'(X) = poll(X)
  ASSERT describeStatus(X) = 'FAILED'
     IMPLIES failure_reason'(X) = describeResponse(X).FailureReason OR 'Unknown'
END FOR

// The ONNX export submission is unchanged
FOR ALL X WHERE onnxStartSucceeds(X) DO
  ASSERT createTrainingJobArgs'(X) = createTrainingJobArgs(X)
  ASSERT startedEntry'(X).export_format = 'onnx'
  ASSERT startedEntry'(X).status        = 'InProgress'
END FOR

// Already-modeled derivation outcomes are unchanged
FOR ALL X IN PowerSet(modeledStatuses) DO
  ASSERT derive_compilation_status'(X) = derive_compilation_status(X)
END FOR
ASSERT derive_compilation_status'([]) = NULL

// Request-level behavior and the imported-ONNX bypass are unchanged
FOR ALL request DO
  ASSERT statusCode'(start(request))  = statusCode(start(request))
  ASSERT auditEvent'(start(request))  = auditEvent(start(request))
END FOR
FOR ALL X WHERE _is_onnx_import(X) DO
  ASSERT start'(X) = start(X)
END FOR

// Rendering of statuses that already match today is unchanged
FOR ALL X WHERE statusOf(X) IN {'COMPLETED', 'INPROGRESS'} DO
  ASSERT render'(surface, X) = render(surface, X)
END FOR

// Nothing outside the diagnostics path moves
ASSERT iamStatements'(EdgeCVPortalComputeStack) = iamStatements(EdgeCVPortalComputeStack)
ASSERT packagingBehavior' = packagingBehavior
ASSERT publishBehavior'   = publishBehavior
```
