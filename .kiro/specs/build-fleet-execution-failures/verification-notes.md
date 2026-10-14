# Verification Notes — build-fleet-execution-failures (Task 11 checkpoint)

Date: 2026-08-08T20:11Z (UTC). Local and mocked validation only. No deployment,
production timeout/volume-setting change, SSM command, instance action,
artifact publication, or live build was performed during this checkpoint.
Environment: Python 3.9.5, Node v20.20.2.

## 1. Evidenced cause(s)

From `.kiro/specs/build-fleet-execution-failures/historical-evidence.md`
(task 3, read-only investigation):

- **Exit-127 repository-path contract mismatch — CONFIRMED (proximate cause)**
  for AMD64 job `06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03`: the dispatched SSM
  command executed `/opt/dda/DefectDetectionApplication/scripts/portal-build-agent.sh`,
  which did not exist on the server (exit 127 in ~3 s); bootstrap had cloned to
  `/home/ubuntu/DefectDetectionApplication`. Caveat retained: fixing the path
  alone does not guarantee a successful build (agent-script presence on the
  then-default branch is unproven). Code comments describe the change as
  contract hardening plus preflight, consistent with the caveat.
- **Diagnostic loss — CONFIRMED historically**: the final invocation carried
  Status/StatusDetails/ResponseCode/stderr, yet the durable record collapsed
  to generic `AGENT_COMMAND_FAILED` with an empty Build Log (Req 1.1–1.3).
- **JP6 ephemeral ENOSPC — CONFIRMED (distinct failure mode)** for job
  `bd91c5d8-ac7e-4125-becc-711860660f2e`: disk exhaustion on the 100 GB volume
  at ~100 minutes (well under the 4 h budget); the agent's head-keeping
  truncation (`tail -n 5 | head -c 512`) dropped the trailing
  `no space left on device` root cause from the durable message.

## 2. Unknowns

- **Maximum-runtime job identity — UNKNOWN (blocked)**: exhaustive read-only
  scans of retained BuildJobs/Audit_Log found no job or audit entry carrying a
  maximum-runtime outcome; no unambiguous identifier was available and none
  was guessed. Consequence honored in code: the runtime work is an
  evidence-model fix (phase clocks, leases, snapshotted budgets), and
  `max_runtime_hours` still defaults to 4 — no timeout increase was encoded
  from the failure label alone (Req 2.13/2.19).
- Whether the path fix alone would have produced a successful historical build
  (see caveat above).

## 3. Validation commands and results (exact, finite/non-watch)

All commands run from the repo root (or the noted subdirectory) on
2026-08-08 during this checkpoint; property-based runs carried the
property-test warning.

| # | Command | Result |
|---|---------|--------|
| 1 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/portal_builds/test_execution_failure_exploration.py test/backend-test/portal_builds/test_storage_exhaustion_exploration.py test/backend-test/portal_builds/test_execution_failure_preservation.py --noconftest -q` | **91 passed** — task 1 counterexamples now pass, task 14 storage counterexamples now pass, task 2 frozen preservation oracle passes unchanged |
| 2 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/portal_builds/ --noconftest -q` | **927 passed, 0 failed** (152 s) — full unchanged portal_builds suite green |
| 3 | `cd edge-cv-portal/frontend && npx tsc --noEmit` | clean, exit 0 |
| 4 | `cd edge-cv-portal/frontend && npx vitest run src/pages/builds/` | 3 files, **21 passed** |
| 5 | `cd edge-cv-portal/frontend && npx vitest run src/pages/builds/BuildDetail.test.tsx src/components/BuildInfrastructureSettings.test.tsx` | 2 files, **23 passed** |
| 6 | `cd edge-cv-portal/infrastructure && npx tsc --noEmit` | clean, exit 0 |
| 7 | `cd edge-cv-portal/infrastructure && npx jest build-fleet-stack.test.ts` | **22 passed** (incl. 1 snapshot) — least privilege, SSM `Success` event coverage, unchanged one-minute schedule |
| 8 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/portal_builds/test_build_reconciliation_properties.py test/backend-test/portal_builds/test_build_diagnostic_api.py --noconftest -q` | **25 passed** — canary/redaction/bounds/marker review (see §4) |

Properties 1–18 all map to passing property/unit/integration tests (verified
in task 10.8/10.9 and re-confirmed by runs 1–2 above): Properties 1/2 by the
frozen exploration/preservation files, 3–5 by
`test_build_reconciliation_properties.py`, 6/10 by
`test_terminal_effects_properties.py`, 7–9/14 by
`test_runtime_accounting_properties.py`, 11/12/15 by
`test_preflight_target_matrix_properties.py` and
`test_no_live_validation_contract.py`, 13 by `test_build_diagnostic_api.py`
and `BuildDetail.test.tsx`, 16 by `test_runtime_budgets_and_volume_sizing_unit.py`
/ `test_runtime_accounting_properties.py`, 17 by
`test_enospc_classification_properties.py`, 18 by
`test_agent_tail_truncation_properties.py`.

## 4. Canary/secret and bounds review

Evidence from run 8 (25 tests passing) and code inspection of
`edge-cv-portal/backend/functions/build_reconciliation.py`:

- Canary secrets (AWS-key-shaped, org-pattern, and bare values under
  secret-shaped map keys) are asserted absent from every captured sink:
  Audit_Log entries, persisted diagnostics, API serializations, and nested
  evidence trees (`test_diagnostic_is_valid_bounded_redacted_and_truthful`,
  `test_nested_evidence_trees_are_redacted_and_json_safe`,
  `test_secret_shaped_map_keys_are_redacted`).
- Byte limits enforced post-redaction: 16 KiB stdout/stderr
  (`STDOUT_STDERR_LIMIT_BYTES`), 4 KiB status/detail/message
  (`DETAIL_FIELD_LIMIT_BYTES`), 48 KiB total diagnostic JSON
  (`TOTAL_DIAGNOSTIC_LIMIT_BYTES`); `test_bound_text_is_truthful_at_every_limit`
  asserts output never exceeds the limit, remains valid UTF-8, and that
  `truncated`/`original_bytes` markers are truthful.
- Unavailable vs available-empty provider fields are distinguished and never
  fabricated (`{"available": false}` vs empty-with-`original_bytes: 0`).
- `test_build_diagnostic_api.py` confirms the same markers and redaction
  survive through the Build Log/detail API projection, including the
  missing-CloudWatch-stream case.

## 5. Diff review (scoped to this spec's files)

Scoped files reviewed: `build_events.py`, `build_dispatcher.py`,
`build_planner.py`, `build_domain.py`, `build_config.py`, `build_jobs.py`,
`build_reconciliation.py` (new), `build_fleet.py`,
`scripts/portal-build-agent.sh`, `portal-build.sh`,
`build-fleet-stack.ts` + test, frontend builds pages/types/`BuildDetail`/
`BuildInfrastructureSettings`, and the new portal_builds test files
(5,744 insertions / 251 deletions across the 18 tracked files plus new files).
The working tree also carries completed changes from earlier specs
(build-source-selection, build-fleet-rbac-visibility, custom-python-source,
deployment fixes); those were excluded from this review.

- **(a) No portal-user RBAC/navigation change**: the only IAM changes in
  `build-fleet-stack.ts` are per-handler Lambda service execution roles and a
  least-privilege `grantSsmReconciliationReads` helper, explicitly commented
  as "service execution wiring, not user RBAC"; asserted by the 22 stack
  tests. No route authorization or navigation change in the scoped frontend
  files (`App.tsx`/`Layout.tsx`/`RequireRole` diffs belong to the earlier
  build-fleet-rbac-visibility spec).
- **(b) No unsupported historical-cause claim**: grep of the scoped diffs
  found no unevidenced causation language; runtime-reconciliation comments
  explicitly mark the max-runtime cause as UNKNOWN and describe the work as
  hardening. The path fix cites the CONFIRMED exit-127 evidence with its
  caveat.
- **(c) No unevidenced hardcoded timeout increase**: `max_runtime_hours`
  still defaults to **4** in `DEFAULT_BUILD_CONFIG`; legacy jobs keep their
  snapshotted values. The `volume_size_gb` 100→200 change is the evidenced
  and approved storage amendment (job bd91c5d8, Req 2.20/3.13).
  `DEFAULT_BOOTSTRAP_TIMEOUT_MINUTES = 20` predates this spec
  (build-source-selection).
- **(d) No production configuration mutation**: all changes are code defaults
  and validation; resolution happens at submission time into immutable
  `config_snapshot` fields, so existing jobs are untouched and the raised
  volume default takes effect only for jobs created after the task 12
  deployment. `test_no_live_validation_contract.py` proves the suite performs
  no deployment, production setting write, SSM send, instance action,
  artifact publication, or live build.

## 6. Compatibility results

- Full unchanged `portal_builds` preservation suite green (927/927),
  including the frozen task 2 oracle: phase transitions, API envelopes
  (`events`/`nextToken`/pagination), target/component/mode mappings,
  single-slot lock/`pgrep`/`flock`/watchdog, cancellation fail-closed,
  queue ordering, strict `now > deadline` boundary, and legacy
  `max_runtime_hours` fallback.
- Diagnostics are additive/optional: legacy API responses and frontend
  clients that omit the fields are unchanged (run 8 legacy cases;
  `BuildDetail.test.tsx` legacy/running/success views).
- Existing jobs keep snapshotted `volume_size_gb`, instance type, and spot
  semantics; new-job resolution order is explicit request → per-target map →
  global default.

## 7. Rollback considerations

- No production resources were touched by tasks 1–11/14; rollback of this
  checkpoint is a pure code revert (git) of the scoped files.
- After a future task 12 deployment: rollback = redeploy the previous stack
  revision. Points to note for that gate:
  - New handler IAM roles and the SSM `Success` event-rule addition are
    infrastructure-stack scoped and revert with the stack.
  - `config_snapshot` values are written at submission time and immutable, so
    jobs created under the new defaults keep 200 GB volumes even after a code
    rollback; jobs created before keep their old snapshots either way. No
    data migration exists in either direction.
  - Optional `diagnostic` API fields are additive; older frontends ignore
    them and a rolled-back backend simply stops emitting them.
- Agent script changes (`portal-build-agent.sh`, tail-preserving truncation,
  heartbeats) ship with the deployment artifact and revert with it.

## 8. Residual risks

- The exit-127 path fix carries the recorded caveat: contract hardening plus
  preflight is evidenced, but a successful end-to-end dedicated AMD64 build is
  unproven until the separately approved task 13 live build.
- The maximum-runtime facet is validated only against the evidence model
  (mocked); with the historical job identity unknown, the real distribution
  of queue/provisioning/stall causes remains unmeasured until post-deployment
  observation.
- Mocked SSM/EventBridge fidelity: eventual-consistency windows
  (`InvocationDoesNotExist` retry bounds, settlement timing) are modeled, not
  measured against live service latencies.
- 200 GB may still be insufficient for pathological concurrent JP6 exports;
  the new disk preflight evidence and `RUNNER_DISK_FULL` classification make
  any recurrence diagnosable from the durable record.
- Python 3.9 runtime deprecation warning (boto3 support ends 2026-04-29) is
  pre-existing and out of scope.

## 9. STOP conditions

Honored. This checkpoint deployed nothing, changed no production timeout or
setting, sent no SSM command, performed no instance action, published no
artifact, and launched no build. Tasks 12 (portal deployment) and 13 (live
dedicated AMD64 build) remain separate explicit user-approval gates.

## Task 12 deployment evidence

- Date/time: 2026-08-08 ~20:44 UTC (backend) / ~20:56–21:05 UTC (frontend + smoke checks, after credential refresh)
- Account/region: 164152369890 / us-east-1 (verified via `aws sts get-caller-identity` before each phase)
- Status: COMPLETE — backend deployed, frontend deployed, all smoke checks passed.

### Backend/infrastructure deploy (COMPLETE)
- Command: `npx cdk deploy EdgeCVPortalBuildFleetStack --require-approval never -c cloudFrontDomain=d23v4ltibogb5x.cloudfront.net` (from `edge-cv-portal/infrastructure`, credentials exported via `aws configure export-credentials`)
- EdgeCVPortalComputeStack: deployed first as dependency — UPDATE_COMPLETE (deployment time 134s). Outputs unchanged (ApiUrl https://yqvyoowugk.execute-api.us-east-1.amazonaws.com/v1/).
- EdgeCVPortalBuildFleetStack: UPDATE_COMPLETE (deployment time 72s), 10/48 resources changed:
  - Lambda functions updated: BuildFleetHandler, BuildConfigHandler, BuildEventsHandler, BuildDispatcherHandler, BuildJobsHandler
  - IAM policies updated: BuildEventsRole/DefaultPolicy, BuildDispatcherRole/DefaultPolicy (per-handler roles)
  - Events rule updated: BuildSsmCommandStatusRule (SSM Success event rule)
  - Stack ARN: arn:aws:cloudformation:us-east-1:164152369890:stack/EdgeCVPortalBuildFleetStack/b73ecc90-91aa-11f1-a9aa-0e8f068eab59

### Frontend deploy (COMPLETE)
- First attempt failed on AWS session expiry; retried after credential refresh (identity re-verified, account 164152369890).
- Command: `./deploy-frontend.sh` (from `edge-cv-portal`, credentials exported via `aws configure export-credentials`). Exit 0.
- config.json regenerated from stack outputs (API URL https://yqvyoowugk.execute-api.us-east-1.amazonaws.com/v1), `npm ci` + `npm run build`, S3 sync with `--delete`, index.html/config.json uploaded no-cache, CloudFront invalidation issued.
- Script's expected Step 6 ran: EdgeCVPortalComputeStack redeployed with `-c cloudFrontDomain=d23v4ltibogb5x.cloudfront.net` for auto-CORS — UPDATE_COMPLETE (deployment time 58s), outputs unchanged.
- Bundle rotation confirmed: entry chunk `assets/index-CU_8Cysc.js` (pre-deploy) → `assets/index-CSCN5TlS.js` (post-deploy).

### Smoke checks (ALL PASSED, read-only — no SSM send, no instance action, no build launch, no artifact publication)
- **Event/schedule wiring**: `dda-portal-build-dispatcher-tick` ENABLED, `rate(1 minute)`, target = BuildDispatcherHandler Lambda. `dda-portal-build-ssm-command-status` ENABLED, pattern covers `aws.ssm` Command + Command Invocation status changes with statuses Success/Failed/TimedOut/Cancelled (the new `Success` coverage), target = BuildEventsHandler Lambda. (`aws events list-rules` / `list-targets-by-rule`)
- **Lambda wiring**: all five handlers LastModified 2026-08-08T20:44Z (post-deploy today): BuildFleetHandler 20:44:20, BuildConfigHandler 20:44:20, BuildDispatcherHandler 20:44:38, BuildEventsHandler 20:44:38, BuildJobsHandler 20:44:48.
- **API compatibility** (authenticated via Cognito user-pool client `38qo3h1dbpkrjj5m0f4la12suo`, USER_PASSWORD_AUTH ID token; no credentials stored):
  - `GET /builds` → 200, envelope parses with `jobs`/`nextToken`/`total`, 17 jobs.
  - `GET /builds/06c9a7ac-6b65-49ee-acdd-db8bf6d0cc03` → 200; status `failed`, error code `AGENT_COMMAND_FAILED` preserved; legacy `config_snapshot.volume_size_gb` = 100 unchanged; `diagnostic` present as additive field (764 bytes, schema_version 1) carrying the historical exit-127 evidence (response_code 127, stderr "No such file or directory", truthful `available`/`truncated`/`original_bytes` markers, `disk: {available: false}`).
  - `GET /builds/bd91c5d8-ac7e-4125-becc-711860660f2e` → 200; status `failed`, error code `BUILD_FAILED` preserved; legacy volume snapshot 100 GB unchanged; diagnostic present (19,036 bytes, within the 48 KiB bound).
- **Redaction**: regex scan of both full detail responses for secret-shaped content (AKIA/ASIA keys, ghp_/xox tokens, JWTs, private-key headers, `aws_secret_access_key`) — clean, zero hits.
- **Frontend serving**: CloudFront root → HTTP 200 serving the new bundle (`index-CSCN5TlS.js`, changed from pre-deploy hash).

### Post-deploy stack times (`aws cloudformation describe-stacks`)
- EdgeCVPortalBuildFleetStack: UPDATE_COMPLETE, LastUpdatedTime 2026-08-08T20:44:13Z
- EdgeCVPortalComputeStack: UPDATE_COMPLETE, LastUpdatedTime 2026-08-08T21:00:11Z (Step 6 CORS redeploy)
- EdgeCVPortalFrontendStack: UPDATE_COMPLETE, LastUpdatedTime 2026-06-05T17:59:25Z — unchanged, expected: frontend deploy is S3 sync + CloudFront invalidation, not a stack update

### Rollback reference (pre-deploy state, captured before deploy)
- EdgeCVPortalBuildFleetStack: previous LastUpdatedTime 2026-08-08T12:53:44Z, UPDATE_COMPLETE
- EdgeCVPortalComputeStack: previous LastUpdatedTime 2026-08-08T12:50:29Z, UPDATE_COMPLETE
- EdgeCVPortalFrontendStack: LastUpdatedTime 2026-06-05T17:59:25Z (stack untouched this session; frontend content rollback = re-sync previous build to S3 + invalidate)
- Rollback = redeploy previous revision (git checkout of prior commit + same cdk deploy commands)

### Anomalies
- None affecting the deployment. Note: both historical jobs return `diagnostic` fields (source `scheduled_reconciliation`, observed before this deployment) rather than the also-acceptable no-diagnostic legacy shape; the field is additive/optional and all legacy envelope fields (status, error code, config snapshot) are unchanged.

## Task 13 live build evidence

Date: 2026-08-08 (~21:09–21:14 UTC). AWS account 164152369890, us-east-1, portal API `https://yqvyoowugk.execute-api.us-east-1.amazonaws.com/v1`.

### Approved scope (restated)
Exactly ONE live build: target AMD64, execution mode dedicated, source_ref `feature/portal-build-fleet-and-workflow-gates` (mandatory — agent script absent from upstream main). No queued follower, no JP5/JP6 builds, no timeout-setting changes, no extra artifact publication, no git commits/pushes, no instance start/terminate by the operator. All respected.

### Server / instance
- Server: `srv-5b214096-91a9-41b7-9d62-cc03ba205c15` ("X86 build server"), dedicated, cpu_architecture x86_64, lifecycle_state running.
- Instance: `i-0865b0697fb050036`, m6i.4xlarge, EC2 state `running`, SSM PingStatus `Online` (verified before submit).

### Job
- build_job_id: `40b036fc-c379-4a70-b00e-e6d6b0d46ecb` (request_id `62876904-ebe1-4229-8878-1bd8675f7542`, request_order 0, no predecessor — single job, as approved).
- Submitted 2026-08-08T21:09:24Z via POST /builds → HTTP 201, status `queued`.
- Snapshotted budgets (config_snapshot at creation): max_runtime_hours 4, runtime_budgets null, volume_size_gb 200 (global default; dedicated mode uses the server's existing disk), x86_64_instance_type m6i.4xlarge, region us-east-1, use_spot_for_ephemeral false, repository `https://github.com/awslabs/DefectDetectionApplication`, source_ref `feature/portal-build-fleet-and-workflow-gates`.

### Preflight (deployed dispatcher, runs first)
PASSED at 21:09:25.671Z. checks: execution_mode dedicated, build_target AMD64, required_arch x86_64, component_name aws.edgeml.dda.LocalServer.amd64, repo_dir /home/ubuntu/DefectDetectionApplication, callback_bus default, callback_region us-east-1, quoting_round_trip true. failures: [].

### Per-phase timing (epoch ms → UTC)
- created_at 1786223365125 (21:09:25.125)
- dispatched_at/started_at 1786223365671 (21:09:25.671) — queue 546 ms (matches diagnostic timing.queueMs 546)
- SSM RequestedDateTime 21:09:27.479; execution_attempt sent_at 1786223367410
- SSM invocation Failed with ResponseCode 1; job diagnostic observed the failure by ~21:13 poll
- ended_at 1786223566780 (21:12:46.780) — total ~3 min 22 s from submit to settlement
- Terminal transition observed once in monitor log (building → failed, single CHANGE line, no flapping).

### SSM evidence (execution_attempt / exactly-once claim)
- attempt_id `f1179601-78a3-4eb6-b352-3f877d4aadbe`, dispatch_state `sent`, instance i-0865b0697fb050036.
- CommandId `8cef4532-9dce-4c7b-9f6e-efa8ba566299`, Comment `dda-build:40b036fc-...:f1179601-...` (job:attempt binding intact).
- Read-only cross-check `aws ssm list-command-invocations --details`: Status Failed, plugin ResponseCode 1 — consistent with the job's error `BUILD_FAILED` "Build failed (exit 1)". evidence_digest recorded on the job (`c0db437c…26a8d7b`).
- Agent synced source to commit `646fb9d` on the mandated feature branch before building (SSM stdout evidence).

### Build Log API diagnostics
- GET /builds/{id}/logs returned 148 log events plus a `diagnostic` block: schemaVersion 1, classification COMMAND_EXECUTION_FAILED, status/statusDetails Failed, responseCode 1, complete true, timing {queueMs 546, provisioningMs null, executionMs null}, disk.available false.
- stdout: available true, 15,316 bytes, truncated true (truthful marker). stderr: available true, truncated false ("failed to run commands: exit status 1").
- Redaction check over the full 420 KB response: 0 AKIA/ASIA access-key shapes, 0 JWT shapes, 0 private-key blocks; 11 [REDACTED] markers present (credential-adjacent phrases redacted). One 40-hex-char string is a public apt keyserver fingerprint from Docker build output, not a credential. Redaction-clean.

### Terminal effects (exactly-once)
`terminal_effects` on the job: effect_id `{job}:{attempt}:terminal`, allocation_release done, audit done, compute_cleanup not_applicable (dedicated — backend correctly did not touch the server's compute), promotion_wakeup done. Exactly one terminal effect set; exactly one terminal transition observed.

### Cleanup / lease release
Full DynamoDB server record for srv-5b214096 byte-identical before submit and after terminal state (lifecycle_state running, no busy/current-job residue) — slot/lease released, server back to available.

### Agent-script caveat (IMPORTANT)
Agent-side improvements (heartbeats/progress events, tail-preserving truncation, agent-side preflight) exist only in the LOCAL working tree and are NOT pushed to origin. Servers sync the agent from origin's feature branch, so the OLD agent executed this build. Consequences observed and expected: no new heartbeat/progress events, and stdout truncation is head-preserving (the failure tail was cut from the SSM output; recovered read-only from the on-server gdk log). The new dispatcher's ATTEMPT_ID env injection was harmless to the old agent. This build verifies the deployed BACKEND contract only.

### Final outcome
Job terminal status: FAILED (`BUILD_FAILED`, exit 1). Root cause (from on-server `/tmp/gdk-build-1786223371.log` tail, read-only): the edgemlsdk Docker image build fails installing pinned `cmake=3.21.3-0kitware1ubuntu20.04.1` from the Kitware apt repository (apt exit code 100) — a source/build-environment issue on the feature branch, NOT a backend defect. The deployed backend contract behaved as designed end-to-end: preflight gate → single claimed attempt → SSM evidence capture → settlement → redacted bounded diagnostics → exactly-once terminal effects → lease release. Valid task outcome per plan (a genuine build failure exercised the failure path faithfully).

### Anomalies
None in the backend contract. The build failure itself is a pre-existing branch/toolchain issue (Kitware cmake pin) unrelated to this spec.
