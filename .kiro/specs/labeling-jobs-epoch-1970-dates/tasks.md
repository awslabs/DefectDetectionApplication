# Implementation Plan

## Overview

This plan fixes the labeling-jobs 1970-date bug using the exploratory bugfix workflow:
reproduce the bug first (Property 1: Bug Condition), capture existing behavior
(Property 2: Preservation), apply the minimal frontend-only fix (seconds → milliseconds
conversion at the four defective render sites in `Labeling.tsx` / `LabelingDetail.tsx`),
then validate, run the full frontend suite, and deploy + live-verify. The backend's
epoch-seconds unit is load-bearing and is NOT changed; the already-correct call sites
(`Labeling.tsx:596`, `PreLabeledDatasets.tsx:338`, `CreateTraining.tsx:188`) are NOT
touched.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Condition) fails, task 2 (Preservation) passes. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Implement the fix (3.1), then re-run task 1 (3.2) and task 2 (3.3). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - full frontend test suite green. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Frontend-only deploy and live verification on the CloudFront portal. Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE task 3 (tests written
  against unfixed code).
- Task 3 depends on 1 and 2. Sub-tasks 3.2 and 3.3 depend on 3.1.
- Task 4 depends on 3. Task 5 depends on 4.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Epoch-seconds labeling job timestamps rendered as milliseconds
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: The bug is deterministic, so scope the property to realistic epoch-seconds timestamps: for any `created_at` / `completed_at` / `stopped_at` drawn from the 2020–2040 range (`fc.integer({ min: 1577836800, max: 2208988800 })`, i.e. seconds), the rendered date string SHALL reflect the same calendar date as `new Date(seconds * 1000)` and never a 1970 date. Include the concrete failing case `created_at = 1789000000` (September 2026), which the unfixed cell renders as a `1/21/1970` date — matching the reported screenshot
  - Follow the existing frontend property-test conventions (`vitest` + `fast-check`, both already in `edge-cv-portal/frontend/package.json` devDependencies; setup at `src/test/setup.ts`; render-per-run walk with mocked `apiService` as in `CreateLabelingJob.groundedsam.property.test.tsx`); name the files per the `Page.topic.property.test.tsx` convention, e.g. `src/pages/Labeling.epochDates.property.test.tsx` and `src/pages/LabelingDetail.epochDates.property.test.tsx`
  - Jobs table (from Bug Condition site `jobsTableCreatedColumn`, `Labeling.tsx:524`): mount `Labeling` with a mocked jobs-list response whose `created_at` is generated epoch seconds; assert the Created cell text equals `new Date(created_at * 1000).toLocaleString()` and does not contain "1970"
  - DDA detail (site `ddaDetailKeyValuePairs`, `LabelingDetail.tsx:859-872`): mount `LabelingDetail` with a mocked DDA job (`labeling_backend: 'DDA'`) carrying generated `created_at` / `completed_at` / `stopped_at` seconds; assert each rendered value matches the `* 1000` oracle
  - Ground Truth detail (sites `groundTruthDetailKeyValuePairs` + `groundTruthDurationMath`, `LabelingDetail.tsx:1244-1256`): mount with a mocked Ground Truth job; assert Created / Completed match the `* 1000` oracle, a completed job's Duration equals `Math.round((completed_at - created_at) / 3600)` hours (e.g. a 2-hour job shows "2 hours", not 0), and an ongoing job's Duration is a realistic hour count (not ~496,000)
  - The test assertions encode Expected Behavior 2.1, 2.2, 2.3, 2.4 (the Fix Checking property from the Bug Condition and Property Specification in bugfix.md)
  - Run tests on UNFIXED code: `cd edge-cv-portal/frontend && npx vitest run src/pages/Labeling.epochDates.property.test.tsx src/pages/LabelingDetail.epochDates.property.test.tsx`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g. "`created_at = 1789000000` renders Created as `1/21/1970, ...`"; "ongoing Duration shows ~496,000 hours")
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Correct call sites, absent-timestamp placeholders, and non-timestamp rendering unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record actual outputs, then assert those outputs
  - Observe on UNFIXED code: the pre-labeled datasets table on the same Labeling page (`Labeling.tsx:596`) renders `new Date(created_at * 1000).toLocaleDateString()` — correct current-era dates from epoch seconds. Record this
  - Observe on UNFIXED code: on the detail page, a job with absent `completed_at` / `stopped_at` renders the `'-'` placeholder in the key-value pairs. Record this
  - Observe on UNFIXED code: non-timestamp content (job name, task type badge, progress bar, status indicator, label set, console link) renders from the payload values unchanged. Record this
  - Write property-based tests capturing the observed behavior: for any epoch-seconds dataset `created_at` (2020–2040 generator as in task 1), the datasets-table Created cell SHALL equal `new Date(created_at * 1000).toLocaleDateString()` (guards Requirement 3.1 through the fix); for any job payload with `completed_at` / `stopped_at` absent, the detail page SHALL render `'-'` for those rows (Requirement 3.4); for any generated job name / status / progress, those cells render unchanged (Requirement 3.5)
  - The untouched files `PreLabeledDatasets.tsx:338` and `CreateTraining.tsx:188` (Requirement 3.2) are guarded by task 3.1's zero-diff constraint (git diff must not touch them); add rendered-oracle coverage for them only if it drops in naturally with the same mocking pattern
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.4, 3.5_

- [x] 3. Fix for epoch-seconds labeling job timestamps rendered as 1970 dates

  - [x] 3.1 Implement the seconds-to-milliseconds conversion at the four defective render sites
    - `Labeling.tsx:524` (jobs table Created column): render `new Date(item.created_at * 1000).toLocaleString()` — mirroring the datasets table's existing convention at line 596
    - `LabelingDetail.tsx:859-872` (DDA detail): convert `rawJob.created_at`, `rawJob.completed_at`, `rawJob.stopped_at` with `* 1000` inside the existing truthiness ternaries (keep the `'-'` fallbacks untouched)
    - `LabelingDetail.tsx:1244-1249` (Ground Truth detail): convert `job.created_at`, `job.completed_at` with `* 1000` (keep the `'-'` fallback untouched)
    - `LabelingDetail.tsx:1254-1256` (Duration): compute from seconds — completed: `Math.round((job.completed_at - job.created_at) / 3600)`; ongoing: `Math.round((Date.now() / 1000 - job.created_at) / 3600)`
    - A small file-local helper (e.g. `const secondsToDate = (s: number) => new Date(s * 1000)`) MAY be introduced per file if it reads naturally; do NOT create a shared module or otherwise over-engineer
    - Do NOT touch the already-correct call sites: `Labeling.tsx:596`, `PreLabeledDatasets.tsx:338`, `CreateTraining.tsx:188`
    - Zero backend changes: `git diff` must show only `Labeling.tsx`, `LabelingDetail.tsx`, and the new test files — no `edge-cv-portal/backend/` files
    - _Bug_Condition: isBugCondition(X) where X is a present created_at/completed_at/stopped_at epoch-seconds value consumed by jobsTableCreatedColumn, ddaDetailKeyValuePairs, groundTruthDetailKeyValuePairs, or groundTruthDurationMath_
    - _Expected_Behavior: rendered = new Date(X.value * 1000).toLocaleString(); completedHours = round(Δseconds/3600); ongoingHours = round((Date.now()/1000 − created_at)/3600) — Fix Checking property from bugfix.md_
    - _Preservation: already-correct *1000 call sites, '-' placeholders, non-timestamp rendering, and backend epoch-seconds records unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Labeling job timestamps render actual calendar dates and realistic durations
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm the jobs table, detail key-value pairs, and duration math all treat the stored values as epoch seconds
    - Run the bug condition exploration tests from task 1
    - **EXPECTED OUTCOME**: Tests PASS (confirms the bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Correct call sites, absent-timestamp placeholders, and non-timestamp rendering unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2
    - Also confirm `git diff` touches no backend files and none of `Labeling.tsx:596` / `PreLabeledDatasets.tsx` / `CreateTraining.tsx` (Requirements 3.2, 3.3)
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full frontend suite: `cd edge-cv-portal/frontend && npx vitest run`
  - Ensure all tests pass (the new epoch-date tests plus the entire existing suite); ask the user if questions arise

- [x] 5. Deploy frontend and verify live
  - **Frontend-only deploy** via `./deploy-frontend.sh` from `edge-cv-portal/` — safe end-to-end after the portal-deploy-flag-hardening spec (it passes no worker flag and the default is now flag-on); zero backend/infrastructure changes to deploy
  - Follow the `.kiro/steering/builds.md` gates first: confirm no component build is running (`pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` must both return nothing) — do NOT run a portal deploy while a component build is in progress
  - Live verification on `d23v4ltibogb5x.cloudfront.net` (account 164152369890, us-east-1): open the Data Labeling page for the cookies use case — the jobs list (list endpoint returns `created_at` in seconds) must show current-year dates for the existing jobs (e.g. `sdfsdasdfadfs`, `tttttasdf`, created 2026-09-08 ~10:5x per the screenshot's times), not `1/21/1970`
  - Open one job's detail page: Created / Completed / Stopped values show current-era dates and the Duration is a realistic hour count
  - Spot-check the datasets table on the same page still shows correct dates (Requirement 3.1 live)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.3_

## Notes

- **Test-first ordering is mandatory**: Task 1 (bug condition) must FAIL and task 2
  (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify
  `Labeling.tsx` or `LabelingDetail.tsx` until both are written and their expected
  outcomes documented.
- **Property references**: Property 1 (Bug Condition / Fix Checking) validates
  Requirements 2.1, 2.2, 2.3, 2.4; Property 2 (Preservation) validates Requirements 3.1,
  3.2, 3.4, 3.5. Requirement 3.3 (backend unchanged) and 2.5 (frontend-only fix) are
  enforced by task 3.1's zero-diff constraint and re-checked in 3.3 and 5.
- **Scope guard**: fix ONLY the four defective consumption sites; the backend's
  epoch-seconds unit is load-bearing (immutable DynamoDB history, seconds arithmetic,
  three correct `* 1000` call sites) and must not change.
- **Primary fix locations**: `edge-cv-portal/frontend/src/pages/Labeling.tsx` (line 524)
  and `edge-cv-portal/frontend/src/pages/LabelingDetail.tsx` (lines 859–872, 1244–1256).
- **Deploy (task 5) is frontend-only and bounded** (~minutes: npm build + S3 sync +
  CloudFront invalidation), but respect the builds.md sequencing gates — never overlap
  with a running component build.
