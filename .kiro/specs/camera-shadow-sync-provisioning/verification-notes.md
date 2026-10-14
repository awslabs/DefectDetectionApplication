# Verification Notes — camera-shadow-sync-provisioning

## Task 2 — Preservation baseline recorded on the UNFIXED tree

Observation date: recorded during task 2 execution, before any fix (tasks 3+)
was applied. Working tree clean (no tracked modifications); the only additions
are the task 1/2 test files themselves.

Note: the counts written into tasks.md at design time (infrastructure jest 30
passing; portal backend 883 passing; LocalServer 204 passed + 3 skipped) are
stale — the tree has grown since the spec was written. The counts below are
the actual observed baseline that tasks 3.10 and 4 must compare against.

### 1. Infrastructure jest + tsc (`edge-cv-portal/infrastructure`)

- `npx jest` (full run): **6 suites, 82 tests — 80 passed, 2 failed.**
  The only 2 failures are the task-1 exploration tests in
  `test/camera-shadow-sync-provisioning.test.ts` (EXPECTED to fail on the
  unfixed tree — they are the Gap 2 counterexamples). All 5 pre-existing
  suites (77 tests) pass; the new suite's 3 preservation tests pass.
- `npx jest test/camera-shadow-sync-provisioning.test.ts`: 3 passed
  (preservation describe, Property 6) + 2 failed (exploration describe,
  expected).
- `npx tsc --noEmit`: **clean** (exit 0).

### 2. Security preservation suite (`test/backend-test/security/`)

- `PYTHONPATH=src/backend:test/backend-test python3 -m pytest
  test/backend-test/security/`: **254 passed, 2 skipped** — green.
- `test_preservation_dependency_setup_station.py` alone: **2 passed** against
  the current (pre-fix) golden
  `dependency_baseline_setup_station.txt`.

### 3. Portal backend suite (`edge-cv-portal/backend/tests`)

- `python3 -m pytest tests -q --continue-on-collection-errors` (from
  `edge-cv-portal/backend`): **1851 passed, 41 errors.**
- The 41 errors are a PRE-EXISTING collection-order conflict unrelated to
  this spec: `tests/test_captures.py` installs a fake `shared_utils` into
  `sys.modules` at collection time and later-collected modules that import
  handler modules at module scope (e.g.
  `test_property_setup_command_wellformed.py` → `device_registrations`) hit
  `ImportError: cannot import name 'Permission' from 'shared_utils'`.
  All affected files pass when run directly (verified:
  `test_property_setup_command_wellformed.py`,
  `test_sts_failure_token_preservation.py`,
  `test_reject_unverifiable_audit_before_effect.py` → 13 passed).
  Nothing under `edge-cv-portal/backend` is touched by this spec.
- `tests/test_camera_shadow_sync_integration.py` (documents the rule SQL
  contract, Req 3.9): **3 passed.**

### 4. LocalServer suite (`test/backend-test`)

- `PYTHONPATH=src/backend:test/backend-test python3 -m pytest
  test/backend-test -q --continue-on-collection-errors`:
  **2563 passed, 8 skipped, 28 failed, 269 errors.**
- Of the 28 failures, exactly **3 are the task-1 exploration tests** in
  `test/backend-test/camera_shadow_sync/test_gap1_exploration.py`
  (EXPECTED to fail on the unfixed tree — Gap 1 counterexamples).
- The remaining 25 failures and all 269 errors are PRE-EXISTING
  environment/tree issues on the clean committed tree, unrelated to this
  spec (examples: `ModuleNotFoundError: No module named 'panorama'` for
  `api-endpoints`/`utils` collection errors; GStreamer/aravis end-to-end
  streaming integration; sqlite migration; jwt build submission; docker
  golden drift in `deploy_reliability`). None involve camera shadow sync.
- Spec-scoped directory
  (`python3 -m pytest test/backend-test/camera_shadow_sync -v`):
  **3 failed (expected exploration) + 5 passed** — the 5 passing are the
  task-1 sanity test plus the 4 new preservation content-anchor tests in
  `test_setup_station_preservation.py`.

### Task 2 test artifacts

- `test/backend-test/camera_shadow_sync/test_setup_station_preservation.py`
  (Property 5, Req 3.2): 4 content-anchor tests — installer invocation
  (`--thing-policy-name GreengrassV2IoTThingPolicy`), step 3.5 ECR heredoc
  block, step 3.6 `put-role-policy` + `ShadowManagerSyncPolicy` document
  (D8), step 4 verification block. **4 passed on the unfixed tree**; must
  keep passing byte-identical after the fix.
- Preservation describe in
  `edge-cv-portal/infrastructure/test/camera-shadow-sync-provisioning.test.ts`
  (Property 6, Req 3.3/3.4): cross-account UseCaseAccountStack camera shadow
  role/policy/rule pre-fix values (fixed names, `SendCameraShadowReports`
  statement scoped to the portal queue ARN, exact SQL, queue ARN/URL target,
  unconditional `CameraShadowReportQueueArn` output);
  `UserAccountsShadowRule` (name, SQL, ack-queue target, `SendAccountSyncAcks`
  statement); camera shadow report queue/DLQ/queue-policy properties.
  **3 passed on the unfixed tree**; must pass on BOTH trees.
- Property 7 (Req 3.5/3.6/3.9) needs no test: enforced by the task 3 diff
  (no file under `src/backend/camera_sync/`, `camera_registry.py`,
  `camera_sync.py` touched) and re-checked at 3.10 via the suites above.
