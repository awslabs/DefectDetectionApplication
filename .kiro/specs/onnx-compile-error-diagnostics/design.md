# ONNX Compile Error Diagnostics Bugfix Design

## Overview

Compiling a vision model to the `onnx` target and having the portal show only
the bare word `ERROR` is not a reporting gap — it is state destruction. The
originating error from `_start_onnx_export_job` is captured correctly at start
time, then annihilated by the portal's own first status poll, because a
placeholder entry with no `export_format` key is routed to
`describe_compilation_job` with a job name that never existed, and the resulting
`ClientError` handler overwrites both `status` and `error` before writing the
record back to DynamoDB.

The fix is built around one invariant: **a recorded reason is write-once from
the poller's perspective.** Nothing a poll observes may overwrite what start
time recorded. Everything else follows from making that invariant enforceable:

1. Stop fabricating a `compilation_job_name` for a job that does not exist, and
   mark the entry as having no live SageMaker job, so the poller can recognize
   and skip it structurally rather than by string-matching a sentinel.
2. Make entry → describe-API classification a single total function
   (`classify_poll_kind`) that every poller shares, so no entry can ever be
   described with the wrong API and no entry can be written that a later poll
   cannot classify.
3. Make the `ClientError` handler additive: poll diagnostics land in their own
   fields, terminal statuses are never overwritten, and `error` /
   `failure_reason` are never touched.
4. Make `derive_compilation_status` total over the statuses the poller can
   actually emit, model `'ERROR'` explicitly as a *transient poll fault* rather
   than absorbing it in a catch-all that latches the record to `Failed`, and
   have `models.py` call that one implementation instead of re-implementing it.
5. Make both UI surfaces render the preserved reason and compare statuses
   case-insensitively, so `ERROR` and the uppercase `FAILED` the Neo path
   actually writes both surface their reason instead of a raw token.

The Neo compile path stays behaviorally byte-for-byte identical, and no
`jetson-xavier-jp7` compile target is added.

**Why this matters for JP7.** `.kiro/specs/jetpack7-support/design.md` records
that the JP6 CUDA 11.4 cudart + TensorRT 8 staging stages are not carried to
JP7 (their transitive L4T driver dependencies do not exist on Thor), so
"DLR-only models are not supported on JP7", while GPU onnxruntime is enabled by
default. SageMaker Neo cannot cover the gap either — its NVIDIA path rejects
`cuda-ver` above 11.x, documented in the `jetson-xavier-jp6` comment in
`COMPILATION_TARGETS`. The ONNX export path is therefore the designated route
for vision models on JP7, which turns an undiagnosable ONNX export failure from
an annoyance into a blocker. Implementing JP7 vision support is explicitly out
of scope.

## Glossary

- **Bug_Condition (C)**: the condition that triggers the bugs — an ONNX export
  that fails to start (or any poll that raises `ClientError`) followed by at
  least one status poll
- **Property (P)**: the desired behavior — the originating reason survives
  arbitrarily many polls and is surfaced to the user
- **Preservation**: the SageMaker Neo compile path for `jetson-xavier`,
  `jetson-xavier-jp5`, `jetson-xavier-jp6`, `x86_64-cpu`, `x86_64-cuda`,
  `arm64-cpu`, the ONNX export submission itself, and every request-level
  status code / audit event
- **Entry**: one element of a training record's `compilation_jobs` list
- **`start_compilation_job`**: the `POST /api/v1/training/{id}/compile` handler
  in `edge-cv-portal/backend/functions/compilation.py`
- **`get_compilation_status`**: the `GET /api/v1/training/{id}/compile` poller
  in the same module — **poller A**
- **`models.py::get_model`**: the model-detail handler's inline compilation sync
  — **poller B**, the one the reported URL exercises
- **`compilation_events.py`**: the EventBridge-driven writer for "SageMaker
  Compilation Job State Change" — **writer C**, name-matched, out of scope
- **`_start_onnx_export_job`**: launches the `torch.onnx.export` SageMaker
  *training* job (Neo cannot emit ONNX); its returned entry carries
  `export_format: 'onnx'`
- **Poll_Kind**: the classification of an entry into the describe API it needs —
  `none` (no live SageMaker job), `training` (ONNX export), `compilation` (Neo)
- **Originating_Reason**: the diagnostic captured at start time (`error`) or from
  a describe response (`failure_reason`) — write-once with respect to polling
- **Poll_Diagnostic**: a diagnostic produced by a failed poll (`poll_error`),
  stored separately so it can never displace an Originating_Reason
- **Transient_Status**: `'ERROR'` — a poll fault, not a compile outcome; a job in
  this state has an unknown true status and must be re-polled, not latched

## Bug Details

### Bug Condition — Defect 1 (placeholder entry polled with the wrong API)

`start_compilation_job`'s `onnx` branch catches any exception from
`_start_onnx_export_job` and appends an entry with a **fabricated** job name
(`f"{safe_model_name}-onnx-failed"`) and **no `export_format` key**. Poller A
branches on `job.get('export_format') == 'onnx'`, so the entry falls through to
`describe_compilation_job` for a name no SageMaker job ever had.

**Formal Specification:**
```
FUNCTION isBugCondition_1(entry)
  INPUT: entry of type CompilationJobEntry
  OUTPUT: boolean

  RETURN entry.target = 'onnx'
     AND NOT liveJob(entry)
     AND pollerWouldDescribe(entry)
END FUNCTION
```

### Bug Condition — Defect 2 (the poll destroys the originating reason)

Poller A's `except ClientError` handler sets `job['status'] = 'ERROR'` and
`job['error'] = str(e)`, then the loop's `updated_jobs` is written back with
`table.update_item`. The Originating_Reason is destroyed on the FIRST poll and
re-destroyed on every subsequent one. The same handler fires for a genuine
transient fault (throttling, expired assumed-role credentials) against a healthy
job.

**Formal Specification:**
```
FUNCTION isBugCondition_2(entry, pollOutcome)
  INPUT: entry of type CompilationJobEntry, pollOutcome of type PollOutcome
  OUTPUT: boolean

  RETURN reason(entry) ≠ NULL
     AND pollOutcome IS ClientError
END FUNCTION
```

### Bug Condition — Defect 3 (`'ERROR'` outside every layer's vocabulary)

`derive_compilation_status` models only `STARTING`/`INPROGRESS`/`IN_PROGRESS`
and `COMPLETED`; everything else falls through a silent catch-all to `'Failed'`.
Its docstring claims a codomain of `'InProgress' | 'Completed' | 'Failed'` while
per-job entries can hold `'ERROR'`. `models.py::get_model` re-implements the same
rules inline, and its `TERMINAL` set (`COMPLETED`/`FAILED`/`STOPPED`) does not
contain `ERROR`, so an `ERROR`-latched entry is re-polled on every model-detail
load. The frontend `CompilationJob['status']` union does not list it either.

**Formal Specification:**
```
FUNCTION isBugCondition_3(statusSet)
  INPUT: statusSet of type Set<String>     // per-job statuses the poller emits
  OUTPUT: boolean

  RETURN EXISTS s IN statusSet
         WHERE s NOT IN modeledStatuses(derive_compilation_status)
END FUNCTION
```

### Bug Condition — Defect 4 (bare status token, hidden reason)

`ModelDetail.tsx`'s fallback table returns `<Badge>{item.status}</Badge>` for
anything other than exactly `COMPLETED`/`INPROGRESS`/`FAILED`, and its inline
`compilation_jobs` type declares only `{target, status, compiled_model_s3}` — no
reason field exists to render. `CompilationTab.tsx` does render `failure_reason`
and `error`, but only inside a panel gated on `job.status === 'Failed'`, an exact
case-sensitive match that excludes `'ERROR'` *and* the uppercase `'FAILED'` the
Neo path writes.

**Formal Specification:**
```
FUNCTION isBugCondition_4(surface, entry)
  INPUT: surface of type UISurface, entry of type CompilationJobEntry
  OUTPUT: boolean

  RETURN reason(entry) ≠ NULL
     AND NOT rendersReason(surface, entry)
END FUNCTION
```

### Bug Condition — Defect 5 (poller B uses the Neo API for ONNX jobs)

`models.py::get_model` calls `describe_compilation_job` for every non-terminal
entry with no `export_format` branch, so a successfully started ONNX export
training job is polled with the Neo API. The exception is caught and warned, so
nothing is clobbered, but the status can never advance from the model detail
page — the very page the reported URL renders.

**Formal Specification:**
```
FUNCTION isBugCondition_5(entry)
  INPUT: entry of type CompilationJobEntry
  OUTPUT: boolean

  RETURN NOT isTerminal(status(entry))
     AND (entry.export_format = 'onnx' OR NOT liveJob(entry))
END FUNCTION
```

### Examples

- `_start_onnx_export_job` raises `AccessDeniedException` for
  `arn:aws:iam::<acct>:role/DDASageMakerExecutionRole` → expected: the record
  keeps `status: Failed` and that exact reason forever, and the UI shows it;
  actual: after the first poll the record reads `status: ERROR`,
  `error: "ValidationException ... Could not find compilation job
  '<model>-onnx-failed'"`, and the UI shows `ERROR`.
- `ONNX_EXPORT_IMAGE` points at the us-east-1 DLC account from a non-us-east-1
  use-case account → same shape: the real image-pull/registry error is replaced
  on the first poll.
- A healthy Neo job for `jetson-xavier-jp6` is polled while the assumed-role
  credentials have expired → expected: the job keeps `INPROGRESS` and the poll
  fault is recorded separately; actual: `status: ERROR`, any previously captured
  `failure_reason` retained but the true status lost, and the overall status
  latched to `Failed`.
- A live ONNX export job is `InProgress` and the user opens the model detail
  page → expected: `describe_training_job` advances it; actual: poller B calls
  `describe_compilation_job`, fails, warns, and the status never moves.
- Edge case: a record whose only entry is the no-live-job ONNX failure →
  expected: zero describe calls, overall `Failed`, reason displayed. Actual: one
  doomed `describe_compilation_job`, overall `Failed` derived from a clobbered
  `ERROR`, reason gone.
- Edge case: a Neo job legitimately `FAILED` with a `FailureReason` → expected
  and required unchanged: `failure_reason` captured, overall `Failed`. The only
  change is that the UI now *shows* the reason instead of hiding it behind the
  case-sensitive filter.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The SageMaker Neo compile path for all six Neo targets: identical
  `create_compilation_job` arguments (job-name derivation and 63-character
  truncation, `OutputConfig` Os/Arch/Accelerator/CompilerOptions, `InputConfig`
  `S3Uri`/`DataInputConfig`/PYTORCH/1.8, the 3600 s `StoppingCondition`), and
  identical polling: raw uppercase `CompilationJobStatus`, `compiled_model_s3`
  on `COMPLETED`, `failure_reason = response.get('FailureReason', 'Unknown')` on
  `FAILED`
- `COMPILATION_TARGETS` content: exactly seven targets, byte-identical
  compiler options (including the JP5/JP6 `cuda-ver` 11.4 / `trt-ver` 8.5.2 /
  `gpu-code` sm_72 triples), and **no** `jetson-xavier-jp7` target
- The ONNX export submission: identical `create_training_job` request, and the
  success entry still carrying `export_format: 'onnx'` and `status: 'InProgress'`
- Request-level behavior: 400 on invalid targets, 403 on insufficient role, 400
  on a non-`Completed` training job, 404 on no compilation jobs, the
  `ValidationException` / `AccessDenied` / `ResourceLimitExceeded` mappings, the
  `start_compilation` audit event, and the DynamoDB update expression
- The imported-BYO-ONNX bypass (`_is_onnx_import` → `compilation_skipped`, 200
  with an empty job list)
- `derive_compilation_status` results for every already-modeled status set:
  `None` on empty, `'InProgress'` when any job runs, `'Completed'` when all are
  `COMPLETED`, `'Failed'` on genuine terminal failure, and case-insensitive
  normalization
- `compilation_events.py` (writer C) and `training_events.py`: name-matched,
  untouched
- `CompilationTab.tsx`'s 15 s polling, target-selection modal, package/publish
  actions, version derivation and validation, and published-components panel
- `packaging.py`, `workflow_packaging.py`, `greengrass_publish.py`, the
  deployment gates, and every recipe
- The IAM policy: no change, therefore no drift in
  `iam_baseline_EdgeCVPortalComputeStack.template.json`

**Scope:**
All inputs that do NOT involve an ONNX start failure, a failed status poll, or a
status value outside the modeled vocabulary are completely unaffected. This
includes:
- Every Neo target compile and poll that succeeds or fails through SageMaker
- Every ONNX export that starts successfully and is polled successfully
- Every imported BYO ONNX model
- Every already-terminal record (which issues no describe calls at all)

## Hypothesized Root Cause

Based on the code read, the causes are:

1. **An unmarked, unrepresentable state.** The placeholder entry encodes "there
   is no job" by writing a job *name* that looks exactly like a real one. The
   data model has no way to say "no live SageMaker job", so every consumer has
   to guess, and poller A guesses wrong.
   - `compilation_job_name` is the only key any consumer keys off
   - `export_format` is the only discriminator poller A uses, and the
     placeholder omits it
   - poller B uses no discriminator at all

2. **A destructive error handler on a persisted record.** `job['error'] =
   str(e)` in an `except ClientError` block, followed by `table.update_item`,
   makes a poll a *writer* of the field that start time owns. There is no
   separation between "what happened when we tried to start" and "what happened
   when we tried to look".

3. **A silent catch-all standing in for a total function.**
   `derive_compilation_status`'s final `return 'Failed'` absorbs every value it
   does not enumerate, so adding a status value anywhere in the poller silently
   changes the aggregate semantics with no failure and no log. The
   re-implementation of the same rules inside `models.py::get_model` doubles the
   surface on which this can drift.

4. **Exact-match status comparisons against values written in another case.**
   The frontend compares `'Failed'` / `'COMPLETED'` / `'INPROGRESS'` as literals
   while the backend writes SageMaker's uppercase forms for Neo and mixed case
   for the portal-synthesized entries. Wherever the comparison misses, rendering
   falls to a default arm that prints the raw token.

5. **Contributing / likely-trigger causes for the initial failure (to
   investigate and name, not to fix here).** `_start_onnx_export_job` hardcodes
   `role_arn = f"arn:aws:iam::{account_id}:role/DDASageMakerExecutionRole"`, and
   `ONNX_EXPORT_IMAGE` defaults to the region-pinned
   `763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:1.13.1-cpu-py39`.
   Either raises from `create_training_job`. IAM is **not** the cause of the
   *poll* failure: `compute-stack.ts` grants `DescribeTrainingJob` /
   `DescribeCompilationJob` on unscoped `arn:aws:sagemaker:*:*:training-job/*`
   and `compilation-job/*`, so the poll `ClientError` is a `ValidationException`
   on a non-existent name, not a denial.

## Correctness Properties

Property 1: Bug Condition - Originating Reason Survives Arbitrarily Many Polls

_For any_ error string `e` and _any_ poll count N ≥ 1, a record created through
the ONNX export start-failure path with originating reason `e` and then polled N
times SHALL still report `e` as that entry's recorded reason, and the fixed
poller SHALL issue no describe call at all for an entry with no live SageMaker
job, leaving its recorded status and reason byte-for-byte unchanged.

**Validates: Requirements 2.1, 2.2, 2.4, 2.5, 2.6**

Property 2: Preservation - Non-Bug Inputs Are Behaviorally Identical

_For any_ input where none of `isBugCondition_1` … `isBugCondition_5` holds, the
fixed code SHALL produce the same result as the original code: identical
`create_compilation_job` and `create_training_job` arguments, identical
`COMPILATION_TARGETS` content with no `jetson-xavier-jp7` target, identical Neo
polling including the `FAILED` → `failure_reason` capture, identical
`derive_compilation_status` results over already-modeled status sets, identical
request-level status codes and audit events, and identical rendering of statuses
that already match today.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19, 3.20, 3.21, 3.22, 3.23, 3.24**

Property 3: Fix Checking - Poll_Kind Classification Is Total and Round-Trips

_For any_ entry the system can write — from `start_compilation_job` (Neo
success, ONNX success, ONNX start failure, skipped-name target) or from either
poller's write-back — `classify_poll_kind` SHALL return exactly one of `none`,
`training`, or `compilation`, the poller SHALL issue exactly the describe call
that kind prescribes (and none for `none`), and re-polling any entry the poller
just wrote SHALL raise nothing.

**Validates: Requirements 2.1, 2.2, 2.3, 2.18**

Property 4: Fix Checking - Poll Diagnostics Are Additive, Never Destructive

_For any_ entry carrying an originating reason and _any_ `ClientError` outcome,
the fixed poller SHALL leave `error` and `failure_reason` unchanged, SHALL record
the poll fault in a distinct field, SHALL NOT overwrite an already-terminal
status, and SHALL NOT latch the record to a terminal state on a transient fault.

**Validates: Requirements 2.4, 2.6, 2.7**

Property 5: Fix Checking - derive_compilation_status Is Total, Shared, and Non-Latching

_For any_ subset of the statuses the poller can emit (including `'ERROR'`),
`derive_compilation_status` SHALL return a value inside its documented codomain,
SHALL name any unmodeled value explicitly rather than collapsing it silently to
`Failed`, SHALL NOT return `'Failed'` when the only non-running status is a
transient poll fault, SHALL be the single implementation `models.py` also calls,
and every terminal value it models SHALL be treated as terminal by poller B's
sync filter and SHALL be present in the frontend `CompilationJob['status']`
union.

**Validates: Requirements 2.8, 2.9, 2.10, 2.11, 2.12, 2.13**

Property 6: Fix Checking - The UI Surfaces the Preserved Reason

_For any_ entry carrying a reason, both `ModelDetail.tsx` and
`CompilationTab.tsx` SHALL render that reason and SHALL NOT render a bare status
token alone; and _for any_ status the poller can emit, each surface's
classification SHALL be identical for the value, its uppercase form, and its
lowercase form.

**Validates: Requirements 2.14, 2.15, 2.16, 2.17, 2.20**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct:

**Step 1 — new shared module `edge-cv-portal/backend/layers/shared/python/compilation_status.py`**

The shared Lambda layer already hosts cross-handler modules (`rbac_utils.py`,
`s3_browse_utils.py`, `manifest_transformer.py`, `user_roles_dao.py`), and both
`CompilationHandler` and `ModelsHandler` mount it, so this is where the single
source of truth belongs. Pure functions only — no boto3, no I/O:

```python
POLL_KIND_NONE = 'none'
POLL_KIND_TRAINING = 'training'
POLL_KIND_COMPILATION = 'compilation'

RUNNING_STATUSES   = {'STARTING', 'INPROGRESS', 'IN_PROGRESS'}
COMPLETED_STATUSES = {'COMPLETED'}
FAILED_STATUSES    = {'FAILED', 'STOPPING', 'STOPPED'}
TRANSIENT_STATUSES = {'ERROR'}          # a poll fault, not a compile outcome
TERMINAL_STATUSES  = COMPLETED_STATUSES | FAILED_STATUSES

STATUS_POLL_ERROR = 'ERROR'             # kept as-is: legacy records carry it

POLL_ERROR_MAX_ATTEMPTS = 5             # bound on "unknown, keep polling"

def normalize_status(value) -> str          # str(value or '').upper()
def is_terminal_status(value) -> bool       # normalize_status in TERMINAL_STATUSES
def is_transient_status(value) -> bool      # normalize_status in TRANSIENT_STATUSES
def classify_poll_kind(job: dict) -> str
def entry_reason(job: dict) -> str | None   # failure_reason or error, in that order
def derive_compilation_status(jobs) -> str | None
```

`classify_poll_kind` is total, with no exception path:

```
FUNCTION classify_poll_kind(job)
  IF job.get('job_started') IS False        THEN RETURN POLL_KIND_NONE
  IF NOT job.get('compilation_job_name')    THEN RETURN POLL_KIND_NONE
  IF job.get('export_format') = 'onnx'      THEN RETURN POLL_KIND_TRAINING
  RETURN POLL_KIND_COMPILATION
END FUNCTION
```

`derive_compilation_status` keeps today's precedence exactly so every modeled
input is preserved, and adds explicit arms after it:

```
FUNCTION derive_compilation_status(jobs)
  IF jobs IS EMPTY                          THEN RETURN NULL          // 3.13
  statuses ← { normalize_status(j.status) FOR j IN jobs }
  IF statuses ∩ RUNNING_STATUSES ≠ ∅        THEN RETURN 'InProgress'  // 3.14
  IF statuses ⊆ COMPLETED_STATUSES          THEN RETURN 'Completed'   // 3.14
  IF statuses ∩ FAILED_STATUSES ≠ ∅         THEN RETURN 'Failed'      // 3.14
  IF statuses ⊆ (COMPLETED_STATUSES ∪ TRANSIENT_STATUSES)
                                            THEN RETURN 'InProgress'  // 2.10
  LOG WARNING naming the unmodeled values   // 2.9 - never silent
  RETURN 'InProgress'                                                 // 2.9
END FUNCTION
```

The two new arms are reachable only when a transient or unmodeled value is
present — i.e. only under `isBugCondition_3` — so every already-modeled status
set yields exactly today's answer (Property 2). Keep and correct the docstring:
enumerate the codomain, enumerate the per-job vocabulary, and state that a
transient poll fault yields `'InProgress'` because the job's true status is
unknown and must be re-polled.

**Step 2 — `compilation.py`: stop fabricating a job name (design step 1 of the fix order)**

In `start_compilation_job`'s `onnx` except branch (~line 556-565), replace the
placeholder with:

```python
compilation_jobs.append({
    'target': 'onnx',
    'export_format': 'onnx',
    'status': 'Failed',
    'job_started': False,
    'error': str(e),
    'failed_step': 'start_onnx_export_job',
})
```

No `compilation_job_name`. Two call sites currently assume that key exists and
must be made tolerant in the same change:
- the `start_compilation` audit event's
  `[j['compilation_job_name'] for j in compilation_jobs]` → use
  `j.get('compilation_job_name') or f"{j.get('target')}:not-started"` so the
  audit event keeps its shape without raising (3.11)
- poller A's `ClientError` log line `f"... for {job['compilation_job_name']}"`
  → `job.get('compilation_job_name')`

Writer C (`compilation_events.py`) matches entries by `compilation_job_name`, so
removing the fabricated name cannot regress it — no SageMaker job ever emitted
an event bearing that name (3.23). Poller B already filters on
`if j.get('compilation_job_name')`, so it skips the entry for free.

**Step 3 — `compilation.py`: poller A routes by Poll_Kind and never destroys**

Replace the `export_format` conditional in `get_compilation_status` with the
shared classifier, and make the `ClientError` handler additive:

```python
for job in compilation_jobs:
    kind = classify_poll_kind(job)
    if kind == POLL_KIND_NONE:
        updated_jobs.append(job)          # no describe call, no mutation
        continue
    try:
        if kind == POLL_KIND_TRAINING:
            ...describe_training_job...   # body unchanged
        else:
            ...describe_compilation_job...# body unchanged
        job.pop('poll_error', None)       # a successful poll clears the fault
        job.pop('poll_error_count', None)
        updated_jobs.append(job)
    except ClientError as e:
        logger.error(...)                 # .get() for the job name
        job['poll_error'] = str(e)
        job['poll_error_at'] = timestamp
        job['poll_error_count'] = int(job.get('poll_error_count', 0)) + 1
        if not is_terminal_status(job.get('status')):
            if job['poll_error_count'] >= POLL_ERROR_MAX_ATTEMPTS:
                job['status'] = 'FAILED'
                job.setdefault('failure_reason',
                               f"status could not be retrieved after "
                               f"{job['poll_error_count']} attempts: {e}")
            else:
                job['status'] = STATUS_POLL_ERROR
        updated_jobs.append(job)
```

Invariants this establishes: `error` and `failure_reason` are never assigned in
the `except` block; a terminal status is never overwritten; a transient fault
maps to the explicitly modeled `'ERROR'` (so the aggregate stays `InProgress`
and the record is not latched); and the "unknown, keep polling" window is bounded
by `POLL_ERROR_MAX_ATTEMPTS`, after which the entry becomes genuinely terminal
with the *poll* reason promoted into `failure_reason` only if no
Originating_Reason exists (`setdefault`).

**Step 4 — `models.py`: poller B shares the classifier and the derivation**

In `get_model`'s sync block (~line 234-290):
- import `classify_poll_kind`, `is_terminal_status`, `derive_compilation_status`,
  `POLL_KIND_*` from `compilation_status`
- build `jobs_to_sync` with `not is_terminal_status(j.get('status'))` and
  `classify_poll_kind(j) != POLL_KIND_NONE`, so terminal entries (including
  `'FAILED'` and the no-live-job entry) are never re-polled and `'ERROR'` is
  handled by the same rule everywhere (2.12)
- dispatch per entry: `describe_training_job` for `POLL_KIND_TRAINING`,
  `describe_compilation_job` for `POLL_KIND_COMPILATION`, mirroring poller A's
  field capture (`TrainingJobStatus` verbatim, `compiled_model_s3` on completion,
  `failure_reason` on failure) (2.18)
- delete the inline duplicated derivation and call
  `derive_compilation_status(compilation_jobs)` (2.11)
- keep the `except Exception: logger.warning(...)` warn-and-continue shape
  verbatim, and do not touch `error` / `failure_reason` there (2.19, 3.19)

**Step 5 — `frontend/src/types/index.ts`: model every emitted status**

Add `'ERROR'` to the `CompilationJob['status']` union with a comment stating it
is a transient poll fault (the job's true status is unknown, it will be
re-polled), and add `job_started?: boolean`, `poll_error?: string`, and
`failed_step?: string`. `failure_reason` and `error` already exist (2.13).

**Step 6 — `CompilationTab.tsx`: case-insensitive classification, reason always shown**

- add a local `const classify = (s?: string) => String(s || '').toUpperCase()`
  and route `getStatusIndicator` through it, so `Completed`/`COMPLETED`,
  `InProgress`/`INPROGRESS`, and `Failed`/`FAILED` all hit their intended arm
  (2.16); add an explicit `ERROR` arm rendering `type="in-progress"` with the
  label "Status unavailable — retrying" plus the poll error, and keep the default
  arm for anything else but annotate it rather than printing a raw token (2.17)
- change the "Compilation Errors" panel's filter from `job.status === 'Failed'`
  to a diagnostic predicate — any job whose normalized status is `FAILED` /
  `STOPPED` / `ERROR`, or that carries `failure_reason` / `error` / `poll_error`
  — so the reason is rendered for the ONNX no-live-job entry, for Neo `FAILED`
  jobs (which the exact-match filter has always excluded), and for a transient
  poll fault (2.14)
- inside each alert, render `failure_reason` and `error` as today, add
  `poll_error` under a distinct "Status lookup error" label so the two are never
  conflated, and render `failed_step` when present; when
  `compilation_job_name` is absent, render "not started" instead of an empty
  cell in the job-name column
- leave the polling effect, the modals, the version logic, and the
  published-components panel untouched (3.16, 3.17)

**Step 7 — `ModelDetail.tsx`: the fallback table gains the reason**

- extend the inline `compilation_jobs` type with `failure_reason?: string`,
  `error?: string`, `poll_error?: string`, `job_started?: boolean` (2.15)
- normalize the status cell's comparisons to uppercase and add an `ERROR` arm
  (2.16, 2.17)
- add a "Reason" column rendering `failure_reason || error`, with `poll_error`
  shown as secondary text, so the reported page can never again show only a
  status token (2.14)
- leave the `trained`/`imported` → `loadTrainingJob` → `CompilationTab` branch
  and every other section untouched

**Step 8 — documentation**

- correct `derive_compilation_status`'s docstring (step 1)
- add a comment on the ONNX except branch stating why no
  `compilation_job_name` is written and which consumers depend on that
- add a comment on the `ClientError` handler stating the write-once invariant for
  `error` / `failure_reason`, so a future change does not silently reintroduce
  Defect 2
- name the two likely triggers (hardcoded `DDASageMakerExecutionRole`,
  region-pinned `ONNX_EXPORT_IMAGE`) in the ONNX branch comment so the preserved
  reason is actionable (2.20), and state that changing their resolution is out of
  scope

## Sibling spec amendments

Documentation-consistency amendments — a short note appended to each affected
document referencing `.kiro/specs/onnx-compile-error-diagnostics/`, not a
rewrite.

| Document | Affected claim | Amendment |
|---|---|---|
| `.kiro/specs/jetpack7-support/design.md` (line ~35) and `tasks.md` (task on JP7 known limitations) | "DLR-only models are not supported on JP7" makes ONNX export the designated vision route for JP7, but that route's failure mode was undiagnosable | Note that the ONNX export path is the JP7 vision route and that its start-failure diagnostics are hardened here; state that no `jetson-xavier-jp7` **compile** target exists or is added, and that `jetson-xavier-jp7` remains a packaging-target identifier only |
| `.kiro/specs/vllm-package-publish-gui/design.md` (line ~408) and `requirements.md` (Req 5.5 / clause 95) | "existing `CompilationTab` behavior is otherwise … not modified" and "SHALL present the existing CompilationTab package and publish controls unchanged" | Note that this spec changes `CompilationTab`'s **status classification and error rendering** only; the package/publish controls, request contracts, polling, version logic, and the `trained`/`imported` → `CompilationTab` routing are all untouched, so the vision-record requirement still holds as written |
| `docs/multi-runtime-inference.md` §20 | "**Compile to ONNX** (`compilation.py`, `target=onnx`) … Validated end-to-end earlier" | Note that the *success* path was validated but the start-failure path destroyed its own diagnostics on the first status poll; record the write-once invariant for `error` / `failure_reason`, that a failed ONNX start writes no `compilation_job_name`, and that entries are routed by `classify_poll_kind` |

## Testing Strategy

### Validation Approach

Two phases. First, surface counterexamples on the UNFIXED tree: one exploration
suite whose properties FAIL because the reason is destroyed and the wrong API is
called, and one preservation suite that PASSES and records the Neo baseline that
must survive. Then apply the fix and re-run both, followed by the fix-checking
property suites.

Test commands (portal backend suites run from the tests directory, which is how
they resolve `conftest.py` and the layer paths):

```
cd edge-cv-portal/backend/tests && python3 -m pytest <suite> -q -p no:cacheprovider
```

Frontend, from `edge-cv-portal/frontend`: `npx vitest run <file>`.

Hypothesis is used for every property; the portal conftest registers the
`portal-fast` (25 examples) and `ci` (100 examples) profiles, so suites MUST NOT
hardcode `max_examples`.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE implementing
the fix, and confirm or refute the root-cause analysis. If any case is refuted,
re-hypothesize before writing a fix.

**Test Plan**: Create
`edge-cv-portal/backend/tests/test_onnx_compile_diagnostics_exploration.py`
following `test_vllm_packaging_dispatch.py`'s pattern — a module-scoped fixture
on the moto-backed `aws_stack`, its own training-jobs table, and
`compilation.py` loaded inside the mock so its module-level boto3 clients are
intercepted. Stub `sagemaker_usecase` so `create_training_job` raises a chosen
`ClientError` and `describe_compilation_job` raises `ValidationException` for
any unknown name, exactly as the service does. Run against the UNFIXED tree.

**Test Cases**:
1. **Reason destroyed on the first poll** (`isBugCondition_2`, the core bug):
   drive `start_compilation_job` with `targets=['onnx']` and a raising
   `create_training_job`, assert the record carries the originating error, then
   run `get_compilation_status` once and assert the error is STILL there (will
   fail on unfixed code — it reads the `ValidationException` instead)
2. **Reason survives N polls** (property form of case 1): for any error string
   and any N ≥ 1, the reason after N polls still contains the original string
   (will fail on unfixed code at N = 1)
3. **No-live-job entry is never described** (`isBugCondition_1`): assert zero
   `describe_compilation_job` and zero `describe_training_job` calls for the
   ONNX failure entry (will fail on unfixed code — one doomed
   `describe_compilation_job` for `{safe_model_name}-onnx-failed`)
4. **Terminal status not overwritten**: assert the entry's status is still
   `Failed` (not `ERROR`) after a poll (will fail on unfixed code)
5. **Transient fault on a healthy Neo job**: seed an `INPROGRESS`
   `jetson-xavier-jp6` entry, make `describe_compilation_job` raise
   `ThrottlingException`, and assert the recorded status is not a terminal
   `Failed`, the previously captured `failure_reason` is intact, and the overall
   status is not `'Failed'` (will fail on unfixed code)
6. **`derive_compilation_status` totality** (`isBugCondition_3`): over generated
   subsets of the emittable statuses including `'ERROR'`, assert the result is in
   the documented codomain and that a single transient value among otherwise-
   running jobs does not yield `'Failed'` (the second half will fail on unfixed
   code for the transient-only sets)
7. **Round-trip**: for every entry `start_compilation_job` or
   `get_compilation_status` can write, a subsequent poll classifies it and raises
   nothing (will fail on unfixed code — the sentinel entry raises inside the
   describe branch, and after Step 2 lands the audit-event comprehension would
   `KeyError` without the `.get()` change)
8. **Poller B uses the Neo API for an ONNX job** (`isBugCondition_5`): seed an
   `InProgress` `export_format: 'onnx'` entry, invoke `models.get_model`, and
   assert `describe_training_job` was called (will fail on unfixed code —
   `describe_compilation_job` is called, fails, and is warned away)
9. **Source-level assertion, PASSES on unfixed code and documents `F(X)`**:
   `'jetson-xavier-jp7' not in COMPILATION_TARGETS` and exactly seven targets
   are defined — this must keep passing after the fix (non-goal guard)

**Expected Counterexamples**:
- The record's `error` after one poll is a `ValidationException` naming
  `{safe_model_name}-onnx-failed`, not the reason `create_training_job` raised
- `status` flipped from `'Failed'` to `'ERROR'`
- One `describe_compilation_job` call issued for a job that never existed
- `derive_compilation_status({'ERROR'}) == 'Failed'` — a transient fault latching
  the record
- Possible causes to confirm: the missing `export_format` on the placeholder,
  the destructive `except ClientError` handler, the silent catch-all in the
  derivation, poller B's missing dispatch

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
code produces the expected behavior.

**Pseudocode:**
```
FOR ALL e IN ErrorStrings, FOR ALL N >= 1 DO
  record ← start_compilation_job'(targets = ['onnx'], startRaises = e)
  ASSERT entry_reason(onnxEntryOf(poll'^N(record))) CONTAINS e
  ASSERT describeCalls(poll'^N(record)) = []
END FOR

FOR ALL entry WHERE isBugCondition_1(entry) OR isBugCondition_5(entry) DO
  ASSERT describeCalls(poll'(entry)) = prescribedBy(classify_poll_kind(entry))
END FOR

FOR ALL statusSet IN PowerSet(emittableStatuses') DO
  ASSERT derive_compilation_status'(statusSet) IN documentedCodomain
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed code produces the same result as the original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT (isBugCondition_1(input) OR isBugCondition_2(input)
                      OR isBugCondition_3(input) OR isBugCondition_4(input)
                      OR isBugCondition_5(input)) DO
  ASSERT F(input) = F'(input)
END FOR
```

**Testing Approach**: Property-based testing is the right instrument here
because preservation is a universal claim over the whole Neo target × status ×
request space, it generates the mixed-case and mixed-status combinations a
hand-written table would miss, and it gives a strong guarantee that the Neo path
did not move.

**Test Plan**: Observe the UNFIXED behavior first, record it, then encode it as
properties in
`edge-cv-portal/backend/tests/test_onnx_compile_diagnostics_properties.py`.
Verify the suite passes on the UNFIXED tree before implementing anything.

**Test Cases**:
1. **Neo submission identity**: observe the exact `create_compilation_job`
   kwargs for each of the six Neo targets on unfixed code, freeze them as a
   golden, and assert equality after the fix
2. **Neo polling identity**: observe that `COMPLETED` sets `compiled_model_s3`
   and `FAILED` sets `failure_reason = FailureReason or 'Unknown'`, and that the
   raw uppercase status is stored verbatim; assert unchanged
3. **`COMPILATION_TARGETS` identity**: freeze all seven entries including the
   JP5/JP6 compiler-option triples; assert no `jetson-xavier-jp7` key appears
4. **ONNX submission identity**: observe the `create_training_job` kwargs on the
   success path and the returned entry's `export_format` / `status`; assert
   unchanged
5. **Derivation identity over modeled statuses**: over generated subsets of
   `{STARTING, INPROGRESS, IN_PROGRESS, COMPLETED, FAILED, STOPPING, STOPPED}`
   in mixed case, assert `derive_compilation_status'` equals the recorded unfixed
   result, and `[] → None`
6. **Request-level identity**: over generated requests, assert the status codes
   and messages for invalid targets, insufficient role, non-`Completed` training,
   the `ClientError` mappings, and the empty-job-list 404 are unchanged, and
   that the `start_compilation` audit event keeps its field shape
7. **Imported-ONNX bypass identity**: over generated records satisfying
   `_is_onnx_import`, assert the 200 / `compilation_skipped` / empty-list
   response is unchanged
8. **Terminal-record identity**: a record whose jobs are all terminal issues
   zero describe calls from either poller

### Unit Tests

- `classify_poll_kind`: totality over every entry shape the system writes (Neo
  success, ONNX success, ONNX start failure, absent name, absent
  `export_format`, `job_started: False` with a name present), each mapping to
  exactly one kind
- `normalize_status` / `is_terminal_status` / `is_transient_status`: mixed case,
  `None`, empty string, unknown values
- `entry_reason`: `failure_reason` precedence over `error`, `None` when neither
  is present
- `derive_compilation_status`: empty → `None`; all `COMPLETED` → `Completed`;
  any running → `InProgress`; genuine `FAILED`/`STOPPED` → `Failed`;
  transient-only → `InProgress`; `{FAILED, ERROR}` → `Failed` (a genuine failure
  still dominates); an unmodeled value logs a warning and returns a non-latching
  value — the test that would have caught Defect 3
- The ONNX except branch writes no `compilation_job_name` and does write
  `job_started: False`, `export_format: 'onnx'`, and `error`
- The audit event's job-name list tolerates an entry with no
  `compilation_job_name`
- The `ClientError` handler: `error` / `failure_reason` untouched; `poll_error`,
  `poll_error_at`, `poll_error_count` set; a terminal status not overwritten; a
  successful poll clearing the fault fields; `POLL_ERROR_MAX_ATTEMPTS` promoting
  to a terminal `FAILED` with `failure_reason` set only by `setdefault`
- `models.py::get_model`: `jobs_to_sync` excludes terminal and `POLL_KIND_NONE`
  entries; per-kind dispatch; the shared derivation is called rather than an
  inline copy; warn-and-continue preserved

### Property-Based Tests

- Property 1 (Hypothesis): originating reason survives N polls, over generated
  error strings and N
- Property 3 (Hypothesis): `classify_poll_kind` totality and describe-call
  correspondence over generated entry shapes; round-trip over every entry the
  system can write
- Property 4 (Hypothesis): additive poll diagnostics over generated
  (entry, `ClientError`) pairs
- Property 5 (Hypothesis): derivation totality, codomain membership, and the
  non-latching transient rule over generated status subsets; plus the
  cross-layer assertions (one shared implementation, terminal-set membership,
  frontend union membership asserted at source level against
  `frontend/src/types/index.ts`)
- Property 2 (Hypothesis): the preservation suite above
- Property 6 (fast-check) in
  `edge-cv-portal/frontend/src/components/compilationStatus.property.test.ts`,
  following `src/components/vllm-publish/publishState.gating.property.test.ts`:
  over generated jobs, the status classifier is case-insensitive and the
  diagnostic predicate is true whenever any reason field is present

### Integration Tests

- End-to-end on moto: `start_compilation_job` with `targets=['onnx']` and a
  raising `create_training_job`, then three successive `get_compilation_status`
  calls — the originating reason is byte-identical after each, no describe call
  is issued, and `compilation_status` is `Failed` throughout
- Mixed request: `targets=['jetson-xavier-jp6', 'onnx']` where the Neo target
  starts and the ONNX target fails — the Neo entry polls normally through
  `describe_compilation_job`, the ONNX entry is skipped, and the overall status
  follows the Neo job
- Transient-recovery flow: a Neo job whose describe raises `ThrottlingException`
  on polls 1-2 and succeeds on poll 3 — the record is never latched to `Failed`,
  `poll_error` is cleared on success, and the true status is recorded
- Poller B: a live ONNX export entry advances from `InProgress` to `Completed`
  through `models.get_model`, with `compiled_model_s3` set
- Frontend: `CompilationTab.tsx` has **no** existing test suite, so create
  `edge-cv-portal/frontend/src/components/CompilationTab.diagnostics.test.tsx` —
  a job with status `ERROR` and a preserved `error` renders the reason and not a
  bare token; a Neo job with uppercase `FAILED` and a `failure_reason` renders
  that reason (previously hidden by the exact-match filter); a job with no
  `compilation_job_name` renders "not started"
- Frontend: extend `src/pages/ModelDetail.vllmPublish.integration.test.tsx`'s
  fixtures (or add a sibling test) so the fallback compilation table's new Reason
  column is exercised for a vision record whose training job did not load

### Preservation Gates To Re-run

- `cd edge-cv-portal/backend/tests && python3 -m pytest test_vllm_packaging_dispatch.py -q -p no:cacheprovider`
  — MUST pass unchanged (shares the training-jobs-table fixture pattern)
- `cd edge-cv-portal/backend/tests && python3 -m pytest test_property_llm_free_compilation_identity.py -q -p no:cacheprovider`
  — MUST pass unchanged
- `cd edge-cv-portal/backend/tests && python3 -m pytest test_vision_model_packaging_preservation.py -q -p no:cacheprovider`
  — MUST pass unchanged (no packaging path is touched, 3.20)
- `python3 -m pytest test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py -q -p no:cacheprovider --noconftest`
  — MUST pass with NO rebaseline: this fix makes no IAM change (3.21). Move
  `edge-cv-portal/infrastructure/cdk.out` aside first per
  `.kiro/steering/builds.md`; the 4 known-acceptable local-only `cdk.out` drift
  failures under `test/backend-test/security/` are pre-existing and out of scope
- From `edge-cv-portal/frontend`:
  `npx vitest run src/pages/ModelDetail.engineConfig.test.tsx` and
  `npx vitest run src/pages/ModelDetail.vllmPublish.integration.test.tsx` —
  MUST pass unchanged (the `trained`/`imported` → `CompilationTab` routing and
  the vLLM section are untouched); plus `npx vitest run src/components/vllm-publish`
  and a clean `npm run build`
