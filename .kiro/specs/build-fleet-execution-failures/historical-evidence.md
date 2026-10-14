# Historical Evidence — build-fleet-execution-failures (Task 3)

Read-only investigation performed against retained AWS records (region
`us-east-1`). **No SSM command was sent, no record was updated, no
instance was changed, nothing was deployed or published, and no build
was launched.** Only `dynamodb get-item/scan`, `ssm list-commands/
get-command-invocation`, `logs describe-log-groups/get-log-events/
filter-log-events`, `ec2 describe-instances/describe-instance-attribute`,
and `lambda get-function-configuration/list-functions` were used.

All timestamps below are **UTC**. Sanitization: credentials, tokens,
signed query values, passwords, and raw authorization values were never
present in any output quoted here; the submitting user's identity id and
the AWS account number are intentionally omitted as unnecessary.

Evidence sources consulted and their availability:

| Source | Identity | Availability |
|---|---|---|
| BuildJobs table `dda-portal-build-jobs` (17 items total, TTL 180 d) | job records | **available** |
| BuildServers table `dda-portal-build-servers` | server record | **available** |
| Audit_Log table `dda-portal-audit-log` (5,875 items scanned) | build audit entries | **available** |
| SSM command history (`list-commands`, `get-command-invocation`) | both incident commands | **available** (still inside the ~30-day SSM retention window at investigation time) |
| CloudWatch `/dda/portal-builds` (retention 180 d) | build stdout streams | **available for the JP6 job; the AMD64 job's stream was never created** (see §1.4 — `unavailable`, not retention-expired) |
| CloudWatch Lambda logs `/aws/lambda/...BuildEventsHandler...` (retention: never expire) | event-consumer activity | **available** |
| EC2 instance + user data | `i-0865b0697fb050036` | **available** (instance still running) |
| Incident-day deployed Lambda code | repo `edge-cv-portal/infrastructure/cdk.out.bak-20260806T153822Z/asset.812a0957.../build_dispatcher.py` (assets synthesized 2026-08-06 15:38 UTC, before the 17:02 incident) | **available** |
| Incident-time dispatcher Lambda environment variables | historical env of `BuildDispatcherHandler` | **unavailable** (only the current configuration is readable; AWS retains no env history) |

---

## 1. Task 3.1 — Known AMD64 job `06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03`

### 1.1 Identity and configuration (BuildJobs record — available)

| Field | Value |
|---|---|
| build_job_id | `06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03` |
| build_target / required_arch | `AMD64` / `x86_64` |
| component_name | `aws.edgeml.dda.LocalServer.amd64` |
| execution_mode | `dedicated` |
| server_id | `srv-5b214096-91a9-41b7-9d62-cc03ba205c15` |
| request_id / order / predecessor | `2744bc1c-3d01-49a1-bd13-96aa378d85d1` / 0 / none |
| config_snapshot | `max_runtime_hours=4`, `volume_size_gb=100`, `x86_64_instance_type=m6i.4xlarge`, `source_ref=NULL` (repository **default branch**) |
| ssm.command_id | `300fd33a-9174-4a0b-aa21-d364f8d5daed` |
| log pointer | group `/dda/portal-builds`, stream `300fd33a-…/i-0865b0697fb050036/aws-runShellScript/stdout` |
| status / error | `failed` / `{"code":"AGENT_COMMAND_FAILED","message":"The build agent SSM command ended with status 'Failed' before reporting a build result."}` |

### 1.2 Timeline (all sources correlated — available)

| UTC time | Event | Source |
|---|---|---|
| 17:00:47 | Server instance `i-0865b0697fb050036` (m6i.4xlarge) launched | EC2 `LaunchTime` |
| 17:00:45.797 / 17:00:54.578 | Server record created / last lifecycle change (`running`) | BuildServers |
| 17:02:53.525 | Job submitted (`created_at`); audit `build_requested` at 17:02:53.586 | BuildJobs, Audit_Log |
| 17:02:54.026 | `dispatched_at` = `started_at` (job entered `building` on dispatch) | BuildJobs |
| 17:02:55.436 | SSM command requested (`AWS-RunShellScript`) | `ssm list-commands` |
| 17:02:55.620 | Command execution start | `get-command-invocation` |
| 17:02:58.620 | Command execution end — elapsed **PT3.027S** | `get-command-invocation` |
| 17:02:59.866 | Dispatcher serialization check touched the job | BuildJobs `ssm.last_serialization_check_at` |
| 17:03:02.736 | Job `ended_at` (terminal `failed`) | BuildJobs |
| 17:03:02.854 | Audit `build_failed` written | Audit_Log |
| 17:03:03.092 | Event consumer log: `agent command 300fd33a-… ended 'Failed' -> job 'failed' (fallback)` | BuildEvents Lambda log |

(The bugfix's "12:02:53 PM … 12:03:02 PM" local-time report corresponds
to this 17:02:53–17:03:02 UTC window.)

### 1.3 Final SSM invocation — retrieved read-only (available)

`get-command-invocation --command-id 300fd33a-9174-4a0b-aa21-d364f8d5daed
--instance-id i-0865b0697fb050036`:

| Field | Value |
|---|---|
| Status / StatusDetails | `Failed` / `Failed` |
| **ResponseCode** | **127** |
| ExecutionStart / End / Elapsed | 17:02:55.620Z / 17:02:58.620Z / PT3.027S |
| StandardOutputContent | `""` — **available-empty** |
| StandardErrorContent | **available**, verbatim: `bash: /opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh: No such file or directory` + `failed to run commands: exit status 127` |
| CloudWatchOutputConfig | `/dda/portal-builds`, enabled |

No credentials, tokens, or secrets appeared in the invocation content.

### 1.4 Stdout stream, callback, and diagnostic-loss confirmation

- **CloudWatch stdout stream**: `get-log-events` on the job's recorded
  stream returns `ResourceNotFoundException` — the stream was **never
  created** (SSM creates it only when the plugin emits output; the
  command failed in ~3 s producing stderr only). The log group itself is
  retained (180 d). Status: `unavailable` (never existed), not
  retention-expired. The Build Log API therefore had nothing to page.
- **Agent callback**: `filter-log-events` over the BuildEvents Lambda
  log for 17:00–17:10 UTC filtered on the job id shows exactly **one**
  invocation — the SSM `Failed` fallback. **No agent phase/heartbeat/
  terminal callback ever arrived** (consistent with the agent script
  never starting).
- **Diagnostic loss (Req 1.1/1.2/1.3 confirmed historically)**: the
  decisive `ResponseCode=127` + stderr existed in SSM the entire time,
  yet the durable record holds only generic `AGENT_COMMAND_FAILED`, the
  audit `build_failed` entry holds only `command_id`/`command_status`/
  `error_code`, and the portal could show only "No log output was
  recorded for this build job."

### 1.5 Repository/script path — proven from retained evidence

- **Dispatched path (proximate cause, proven)**: the invocation stderr
  itself names the executed path
  `/opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh`
  and proves it **did not exist** on the server (`exit 127`, 3 s).
- **Actual clone path (proven)**: the server's EC2 user data (read via
  `describe-instance-attribute userData`) clones
  `https://github.com/awslabs/DefectDetectionApplication` to
  **`/home/ubuntu/DefectDetectionApplication`** and bootstraps there.
- **Why `/opt/dda` was used (proven for the code, unavailable for the
  env)**: the incident-day deployed dispatcher asset
  (`cdk.out.bak-20260806T153822Z`, synthesized 15:38 UTC that day)
  hardcodes `BUILD_REPO_DIR = os.environ.get('BUILD_REPO_DIR',
  '/opt/dda/DefectDetectionApplication')` and builds the agent command
  as `{BUILD_REPO_DIR}/scripts/portal-build-agent.sh`. The incident-time
  Lambda environment is not historically retained (`unavailable`), but
  the executed stderr path proves the effective value resolved to the
  `/opt/dda` default. (The *current* dispatcher Lambda has
  `BUILD_REPO_DIR` unset and current code resolves through
  `build_source.resolve_repo_dir`.)
- **Server record**: the retained BuildServers item for
  `srv-5b214096…` has **no `repo_dir` attribute** (legacy record created
  17:00 that day, before per-server repo_dir persistence) — so nothing
  on the record could have redirected the dispatcher.

**Conclusion (evidence-based, not static inference):** the
`/opt/dda/DefectDetectionApplication` vs
`/home/ubuntu/DefectDetectionApplication` mismatch is **CONFIRMED as the
proximate cause of this job's SSM `Failed`**: the dispatcher invoked a
script path that did not exist while the fleet bootstrap had cloned the
repository elsewhere.

**Bounded caveat:** it is *not* proven that the build would have
succeeded had the correct clone path been used. Job
`3a9e3a50-2965-449f-8227-e74284ec0927` (2026-08-07, JP5 ephemeral)
failed with `AGENT_ABSENT_ON_DEFAULT_BRANCH` ("agent script absent from
upstream default branch (exit 127)"), and this AMD64 job's
`source_ref` was NULL (default branch). Whether
`/home/ubuntu/DefectDetectionApplication/scripts/portal-build-agent.sh`
existed in that clone at 17:02 UTC on 2026-08-06 is `unavailable`
(no retained listing; issuing a new SSM command to look is prohibited).

### 1.6 Lifecycle state

Server lifecycle: `running` before, during, and after the job (record
`last_state_change_at` 17:00:54; EC2 state still `running` at
investigation time; `terminated_at` NULL). No infrastructure or
lifecycle loss contributed. Same-day repeats of the identical signature:
`8618148c-…` (JP5 dedicated, 17:06) and `a25bb078-…` (AMD64 dedicated,
20:50) both ended `AGENT_COMMAND_FAILED` nine seconds after submission,
consistent with the same path defect.

---

## 2. Task 3.2 — Reported maximum-runtime job

### 2.1 Identity search (read-only, exhaustive over retained records)

- **Full BuildJobs scan** (all 17 retained items, 2026-08-06 → 2026-08-08):
  **no job** carries an error message containing "maximum runtime", and
  no error code resembling `MAX_RUNTIME`/timeout exists. The planner's
  wall-clock message ("Build_Job exceeded its maximum runtime of N hours
  (timeout).") appears on **zero** retained jobs.
- **Full Audit_Log scan** (5,875 items): no action containing `runtime`
  or `max_`. The only timeout-family entry is
  `build_bootstrap_timeout` for job `f13904b1-a0ce-4a1a-b870-58c07d7522c0`
  (ephemeral bootstrap exceeded its 20-minute provisioning budget —
  already distinctly coded `BOOTSTRAP_TIMEOUT`, a provisioning-phase
  outcome, not an execution maximum-runtime outcome).

**Result: the investigation of the reported maximum-runtime failure is
BLOCKED BY UNAVAILABLE IDENTITY.** No retained BuildJobs, Audit_Log,
SSM, or CloudWatch record identifies a job failed for exceeding maximum
runtime, and this document will not guess one.

### 2.2 What can and cannot be concluded

- Queue wait, predecessor/server occupancy, provisioning duration,
  positive execution start, runtime budget source, heartbeat/progress/
  log growth, hard-deadline evaluation, cleanup, and terminal effects
  for the reported job **cannot be reconstructed** — `unavailable`
  (no identity, no records).
- Independently of that job, the retained schema shows the *system*
  records only `created_at`, `dispatched_at`, `started_at`, `ended_at`,
  and a single snapshotted `max_runtime_hours` (4 h on every retained
  job); there are no heartbeat/progress/phase-duration fields anywhere
  in the retained data. So even if the job's record existed, the current
  evidence model could not distinguish active progress, stall, heartbeat
  loss, queue wait, provisioning delay, or hard-ceiling expiry
  (Req 1.13–1.18 confirmed as an evidence-model insufficiency).
- **No timeout increase is recommended or justified** by anything in
  this section. A remedy for the reported failure requires first landing
  the runtime-evidence model (task 8) so a future occurrence produces
  classifiable evidence (Req 2.13, 2.19).

### 2.3 Distinct evidenced failure mode: JP6 ephemeral runner disk exhaustion + tail truncation

Job `bd91c5d8-ac7e-4125-becc-711860660f2e` (JP6, ephemeral,
`aws.edgeml.dda.LocalServer.arm64JP6`, ref
`feature/portal-build-fleet-and-workflow-gates`) — all facts re-verified
read-only in this investigation:

| Evidence | Source (available) | Observation |
|---|---|---|
| Timeline | BuildJobs | created 2026-08-08 02:51:25.576; bootstrap marker 02:53:59.672; `started_at` 02:54:17.771; `ended_at` 04:34:25.176; runner `i-0dae725c73950757f` terminated 04:34:59.617 |
| Runtime vs budget | BuildJobs + SSM | execution elapsed **PT1H40M11.686S** (~100 min), snapshotted `max_runtime_hours=4` → failed **well under** its runtime budget; **not** a timeout |
| Terminal invocation | SSM `2068c6dc-e0a0-4abd-a66c-e410c81950f2` | `Status=Failed`, `StatusDetails=Failed`, **`ResponseCode=1`**; stdout 22,930 B retained; stderr 193 B ending `tee: /tmp/portal-build-agent-bd91c5d8-….log: No space left on device` |
| Root cause (full text only in CloudWatch) | `/dda/portal-builds` stream `2068c6dc-…/i-0dae725c73950757f/aws-runShellScript/stdout` | `#109 ERROR: failed to extract layer sha256:265a67…: write /var/snap/docker/common/var-lib-docker/containerd/…/snapshots/384/fs/…` (**ENOSPC** during docker layer extraction), plus `tee: /tmp/gdk-build-1786157659.log: No space left on device` |
| Disk provisioned | BuildJobs `config_snapshot` | `volume_size_gb=100` — the JP6 build exhausted its 100 GB runner volume |
| Observability defect | BuildJobs `error.message` + `scripts/portal-build-agent.sh` line 224 | durable error is cut **mid-path** at `…write /var/snap/docker/common/` — the agent builds it via `ERROR_TAIL=$(tail -n 5 "$BUILD_LOG" \| head -c 512)`, so the decisive "no space left on device" text was truncated out of every durable/portal surface; the cause is recoverable only by reading CloudWatch manually |

This is a **confirmed, distinct failure mode**: ephemeral-runner disk
exhaustion misreported as a bare `BUILD_FAILED` with a truncated,
cause-free durable message. It is direct input to task 4.2
(classification should recognize disk-exhaustion evidence), task 7.1
(preflight should check free-disk against the workload), and task 9.1
(diagnostics must preserve useful head **and tail**, with truncation
markers, instead of a blind first-512-bytes cut). It does **not**
evidence any timeout change.

---

## 3. Task 3.3 — Evidence gate (hypothesis table)

Implementation tasks 7 and 8 MUST cite the rows below before any path
correction or runtime-budget default change. Where the verdict is
**unknown**, only observability/preflight/runtime-model corrections are
permitted — no claim about historical cause and no production timeout
increase.

| # | Hypothesis (design §Hypothesized Root Cause) | Evidence source | Observation | Verdict | Permitted code correction | Prohibited inference |
|---|---|---|---|---|---|---|
| 1 | Repository path contract mismatch caused the 2026-08-06 AMD64 failure | SSM invocation stderr (§1.3); EC2 user data (§1.5); incident-day deployed asset (§1.5) | Command executed `/opt/dda/…/portal-build-agent.sh`, which did not exist (exit 127, 3 s); bootstrap had cloned to `/home/ubuntu/DefectDetectionApplication`; deployed default was `/opt/dda/…` | **CONFIRMED** (proximate cause of the SSM `Failed`) | Task 7.1: resolve dispatch path from registered/actual clone; preflight script existence/executability | That fixing the path alone guarantees a successful build — the agent script's presence on the default branch at that time is unproven (§1.5 caveat, job `3a9e3a50`) |
| 2 | Final invocation evidence is discarded by the event consumer | Retained SSM invocation vs BuildJobs `error`, Audit_Log entry, BuildEvents Lambda log (§1.3–1.4) | Decisive `ResponseCode=127` + stderr were retained in SSM but appear nowhere durable; consumer logged a one-line generic fallback | **CONFIRMED** | Tasks 4.1/4.2/5.1: retrieve, redact, bound, persist invocation diagnostics | None — direct historical confirmation |
| 3 | Build Log source is incomplete (stdout stream only) | CloudWatch `ResourceNotFoundException` on the job's recorded stream (§1.4) | Stream never created (stderr-only, 3 s failure); Build Log API had nothing to serve | **CONFIRMED** | Task 9.1: diagnostics independent of stream existence; merge stderr/status details | None |
| 4 | Terminal fallback is premature and generic | BuildEvents Lambda log; job `error`; audit details (§1.2, §1.4) | Single event → immediate `AGENT_COMMAND_FAILED` absorption; audit carries only command id/status | **CONFIRMED** | Tasks 4.2/5.1: deterministic classification (`COMMAND_EXECUTION_FAILED` for this shape), settlement | None |
| 5 | Missing scheduled command reconciliation caused a historical loss | BuildEvents log window (§1.4) | The incident's EventBridge notification WAS delivered; no retained case of a lost/undelivered terminal event | **UNKNOWN** historically (static gap frozen by task 1/A.1) | Task 5.2: tick reconciliation as hardening | Claiming any historical incident was caused by a lost event |
| 6 | The reported maximum-runtime failure was a hard-ceiling / stall / queue-charging defect | Full BuildJobs + Audit_Log scans (§2.1) | No retained job or audit entry carries a maximum-runtime outcome; identity unavailable | **UNKNOWN** (blocked by unavailable identity) | Task 8.x: phase clocks, leases, snapshotted target/mode ceilings, evidence-rich decisions — as an evidence-model fix | Any historical-cause claim for the reported job; any production timeout increase from the label alone (Req 2.13/2.19) |
| 7 | Distributed terminal effects duplicate or split state | Audit_Log + BuildJobs for both incidents (§1.2, §2.3) | Exactly one audit and one terminal transition observed in each retained case; no duplicate/lost effect in retained history | **UNKNOWN** (no historical counterexample; race window remains a static concern) | Task 6.x: idempotent ledger as hardening | Claiming a historical duplicate-effect incident occurred |
| 8 | No preflight allows invalid contracts to reach costly execution | §1.3 (3-second exit 127 on a live m6i.4xlarge); task 1 exploration A.3 | An invalid path/script contract reached a dispatched SSM command with zero pre-checks | **CONFIRMED** | Task 7.1: full preflight before build/publish | That preflight alone addresses every failure mode |
| 9 | Ephemeral runner disk exhaustion + tail-truncation observability defect (JP6) | BuildJobs, SSM invocation, CloudWatch stream, `portal-build-agent.sh` (§2.3) | ENOSPC on the 100 GB volume at ~100 min (under the 4 h budget); durable error truncated before the cause by `tail -n 5 \| head -c 512` | **CONFIRMED** (distinct failure mode) | Task 4.2: classify disk-exhaustion evidence; task 7.1: free-disk preflight; task 9.1: head+tail bounded diagnostics with truncation markers | That 100 GB is always insufficient or that a specific volume size is mandated; that this job was any kind of timeout |

**Gate statement:** Task 7.1's path correction is authorized by row 1
(confirmed) and must carry row 1's caveat; task 7.1's preflight is
authorized by rows 1, 8, 9. Task 8's runtime model is authorized by row
6 **as an observability/evidence-model fix only** — no default hard
ceiling above the existing snapshotted `max_runtime_hours=4` fallback may
be encoded as a production value from this evidence, and no claim may be
made about what the reported maximum-runtime job actually experienced.
Task 4.2 and 9.1 diagnostics changes are authorized by rows 2, 3, 4, 9.
