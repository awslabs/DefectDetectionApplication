# Implementation Plan

## Overview

This plan fixes the source-image picker's 20-image preview cap using the exploratory bugfix
workflow: surface the bug on UNFIXED code first (Property 1: Bug Condition — prefixes with more
than 20 images have unreachable, unselectable images), capture existing behavior that must not
change (Property 2: Preservation — legacy `limit: 12` callers, small prefixes, extension
filtering, presign expiry, untouched `count_images`/`list_datasets`), then apply the fix and
validate. The fix adds offset/limit pagination to `get_image_preview` (collect all keys, slice,
presign only the page, true `total_found`, `offset`/`limit`/`has_more` metadata, cap 50, defaults
preserved), a typed `offset` surface in `api.ts`, and a paged 12-per-page thumbnail grid in
`SyntheticData.tsx` (deterministic 6×2 CSS grid, Prev/Next, "Showing A–B of TOTAL", per-page
loading, page reset on dataset change only, selection persisting across pages). Frontend wizard
tests are extended and the checkpoint gates on the backend suites (run standalone due to known
moto state leakage in full sweeps), the vitest wizard suite, and the `tsc` production type check
(Req 3.7).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Condition exploration) FAILS; task 2 (Preservation) PASSES. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the fix (3.1 backend pagination, 3.2 api.ts typed surface, 3.3 wizard paged grid), then re-run task 1 (3.4) and task 2 (3.5). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Extend the frontend wizard tests for pagination behavior. Depends on wave 2 (tests the fixed wizard)."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Checkpoint - run backend suites standalone, the vitest wizard suite, and the tsc build gate; ensure all pass. Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE any fix (tests written against unfixed code).
- Task 3 depends on wave 1; sub-tasks 3.4 and 3.5 depend on 3.1–3.3.
- Task 4 depends on task 3 (frontend tests exercise the fixed paged grid).
- Task 5 depends on tasks 3 and 4.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Full Reachability via Pagination
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples demonstrating that images beyond the first 20 lexicographic keys are unreachable (isBugCondition: `imageCount(prefix) > 20`)
  - Create `edge-cv-portal/backend/tests/test_property_image_preview_pagination.py` using the established pytest + moto + hypothesis pattern with a `test_captures.py`-style `shared_utils` stub (moto-backed S3, synthetic API Gateway events invoking the `get_image_preview` handler)
  - **Scoped PBT Approach**: scope the property to the concrete 63-image counterexample plus a bounded hypothesis sweep
  - Test case 1 - Counterexample Reproduction: seed 63 images (32 `anomaly-*.jpg` + 31 `normal-*.jpg`) under `training-images/`; fetch successive pages (offset 0, 12, 24, ... until `has_more` is false) and assert the union of page keys equals all 63 keys including every `normal-*.jpg`
  - Test case 2 - True Total: assert every page response reports `total_found == 63` (unfixed code returns the truncated count, 20)
  - Test case 3 - Property Sweep (hypothesis): generated image counts N in [21, 120] with mixed key names; assert union-of-pages == all image keys and `total_found == N`
  - Test case 4 - Cap Probe: request `limit=50` on a 25-image prefix; assert more than 20 keys are reachable (unfixed code clamps to 20)
  - Assertions match the Expected Behavior Properties from design (union-of-pages completeness, true total, paging metadata)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g. "63-image prefix: only 20 anomaly keys ever returned, no paging metadata, total_found=20")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Legacy Callers and Small Prefixes Unchanged
  - **IMPORTANT**: Follow observation-first methodology - observe behavior on UNFIXED code for non-buggy inputs (isBugCondition false: prefixes with ≤ 20 images and legacy no-offset call shapes), then encode it as property tests
  - Add to `edge-cv-portal/backend/tests/test_property_image_preview_pagination.py` (or a sibling file in the same pattern)
  - Legacy caller shape: `limit=12`, no `offset` (the `ImagePreview.tsx` DatasetBrowser shape) — response contains `images` (first 12 lexicographic image keys), `total_found`, and `expires_in_seconds: 1800`
  - Small prefix property (hypothesis): generated N in [0, 20] images — all N keys returned on the first page, same reachable set as unfixed code
  - Extension filtering: mixed keys (images plus `.txt`, `.manifest`, extensionless) — only `{'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}` appear in results
  - Expiry preservation: response keeps `expires_in_seconds: 1800`
  - Untouched operations: assert `count_images` and `list_datasets` behavior via direct calls on the same seeded bucket (results consistent with the seeded key set)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6_

- [x] 3. Fix source-image picker pagination

  - [x] 3.1 Implement backend offset/limit pagination in get_image_preview
    - In `edge-cv-portal/backend/functions/datasets.py`, parse `offset = max(int(params.get('offset', '0')), 0)` and `limit = min(max(int(params.get('limit', '8')), 1), 50)` (cap raised from 20 to 50)
    - Remove the early `break`s so the existing `list_objects_v2` paginator collects ALL image-extension keys under the prefix (stable lexicographic order), then slice `page_keys = image_keys[offset : offset + limit]`
    - Presign only `page_keys` (keep `ExpiresIn=1800`)
    - Return `total_found = len(image_keys)` (true total), plus `offset`, `limit`, and `has_more = offset + limit < len(image_keys)`; keep `prefix`, `bucket`, `images`, `expires_in_seconds: 1800` unchanged; defaults (`offset=0`, `limit=8`) preserve legacy caller behavior
    - Do NOT modify `count_images` or `list_datasets`
    - _Bug_Condition: isBugCondition(X) — imageCount(X.prefix) > 20, from design_
    - _Expected_Behavior: union of all pages equals all image keys under prefix, total_found is the true total, has_more/offset/limit metadata correct — expectedBehavior from design_
    - _Preservation: legacy limit=12 no-offset shape, extension filtering, ExpiresIn=1800, count_images/list_datasets untouched — Preservation Requirements from design_
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Add typed offset param and response fields to frontend API client
    - In `edge-cv-portal/frontend/src/services/api.ts` `getImagePreview`: add optional `offset?: number` param, serialized into the query string only when provided (mirroring `limit`)
    - Add `offset: number`, `limit: number`, `has_more: boolean` to the typed response
    - _Expected_Behavior: paginated backend reachable from frontend with a typed surface, from design_
    - _Requirements: 2.1, 3.7_

  - [x] 3.3 Implement paged thumbnail grid in SyntheticData.tsx
    - In `edge-cv-portal/frontend/src/pages/synthetic/SyntheticData.tsx`, export `const SOURCE_PICKER_PAGE_SIZE = 12` (2 rows × 6 columns)
    - Add `pageOffset` and `totalImages` state; thumbnail effect requests `getImagePreview({usecase_id, prefix, offset: pageOffset, limit: SOURCE_PICKER_PAGE_SIZE})` and stores `total_found` in `totalImages`; `thumbsLoading` becomes the per-page loading state
    - Replace the wrapping flex container with a deterministic CSS grid (`display: grid; gridTemplateColumns: repeat(6, 96px); gap: 8px`)
    - Add Previous/Next controls (Previous disabled at `pageOffset === 0`, Next disabled when `pageOffset + SOURCE_PICKER_PAGE_SIZE >= totalImages`) and a "Showing A–B of TOTAL" indicator (`A = pageOffset + 1`, `B = min(pageOffset + SOURCE_PICKER_PAGE_SIZE, totalImages)`)
    - Dataset change resets `pageOffset` to 0 and clears `selectedKeys` (existing behavior); page change does NOT touch `selectedKeys`, so selection persists across pages and the selected-count text reflects all pages
    - `handleCreate` already maps `Array.from(selectedKeys)` into `source_images`, so multi-page selections flow into the created session unchanged
    - _Expected_Behavior: every image reachable/selectable via pages, selection persists across navigation, submission includes keys from all pages — from design_
    - _Preservation: single-page prefixes render fully with disabled controls, at-least-one-Source_Image validation unchanged, selectedKeys cleared only on dataset change — from design_
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 3.5, 3.6_

  - [x] 3.4 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Full Reachability via Pagination
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior; when it passes, the expected behavior is satisfied
    - Run `edge-cv-portal/backend/tests/test_property_image_preview_pagination.py` exploration tests
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed - all 63 images reachable, true total reported)
    - _Requirements: 2.1, 2.2, 2.5_

  - [x] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Legacy Callers and Small Prefixes Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions for legacy limit=12 shape, small prefixes, extension filtering, expiry, count_images/list_datasets)
    - Confirm all tests still pass after fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6_

- [x] 4. Extend frontend wizard tests for pagination
  - Extend `edge-cv-portal/frontend/src/pages/synthetic/SyntheticData.test.tsx` (vitest, mocked `apiService`)
  - Multi-page navigation: mock 63-image responses page by page; assert Next/Previous fetch with the expected `offset` values (0, 12, 24, ...) and render each page's thumbnails
  - Selection persistence: select on page 1, navigate to page 2 and back — selection intact and the selected-count text reflects off-page selections
  - Indicator text: "Showing 1–12 of 63", then "Showing 13–24 of 63" after Next
  - Disabled states: Previous disabled on page 1, Next disabled on the last page
  - Multi-page submission: select keys on two pages, submit, assert `createSyntheticSession` receives `source_images` containing keys from both pages
  - Dataset-change reset: page resets to 0 and `selectedKeys` cleared on dataset change (and only then)
  - No-source validation: submission with no source images still blocked with the existing message
  - _Requirements: 2.2, 2.3, 2.4, 2.5, 3.5, 3.6_

- [x] 5. Checkpoint - Ensure all tests pass
  - Backend (run standalone due to known moto state leakage in full-suite sweeps), from `edge-cv-portal/backend`:
    - `pytest tests/test_property_image_preview_pagination.py -v` (the new exploration + preservation file)
    - `pytest tests/test_property_dataset_discovery_preservation.py tests/test_dataset_discovery_none_prefix.py -v` (dataset-discovery pair, own invocation)
    - the synthetic test family standalone
  - Frontend, from `edge-cv-portal/frontend`:
    - `npx vitest run src/pages/synthetic/SyntheticData.test.tsx`
    - `npx tsc --noEmit` (production build gate, Req 3.7)
  - Ensure all tests pass, ask the user if questions arise
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

## Notes

- Tasks 1 and 2 MUST be completed and run against UNFIXED code before task 3 begins; task 1 failing and task 2 passing together confirm the bug diagnosis and the preservation baseline.
- Property 1 (Bug Condition) and Property 2 (Preservation) are the same tests re-run in 3.4 and 3.5 — no new tests are written during verification.
- Backend test suites in this repo are run standalone per file/family because of known moto state leakage across full-suite sweeps.
- Requirement 3.7 (frontend `tsc` type check) is enforced by the build gate in task 5 rather than a runtime property.
- No changes are made to `ImagePreview.tsx`, `count_images`, or `list_datasets`; `total_found` semantics change from "returned count" to "true total", which `ImagePreview.tsx` only displays (a latent display bug this fix also corrects).
