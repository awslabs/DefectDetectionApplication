# Source Image Picker Pagination Bugfix Design

## Overview

The Synthetic Data wizard's source-image picker can only ever show the first 20
lexicographic image keys of a dataset prefix: the backend preview endpoint
(`get_image_preview` in `edge-cv-portal/backend/functions/datasets.py`) clamps
every request to 20 presigned URLs and offers no way to request subsequent
images, and the wizard (`SyntheticData.tsx`) renders whatever it receives as a
single unpaged grid. In the reported dataset (63 images: 32 `anomaly-*.jpg`
sorting before 31 `normal-*.jpg`), all `normal-*.jpg` images are unreachable
and unselectable, blocking the inpainting path that requires normal source
images.

The fix adds **offset/limit pagination** to the preview endpoint and a **paged
thumbnail grid** to the wizard:

- Backend: `get_image_preview` gains optional `offset` and `limit` query
  parameters. It collects *all* image keys under the prefix (the function
  already paginates `list_objects_v2`; it just stops early today), then slices
  `[offset : offset + limit]` and presigns only that page. The response gains
  paging metadata (`offset`, `limit`, `has_more`) and `total_found` becomes the
  TRUE total image count under the prefix. The per-page cap is raised from 20
  to 50 — bounded per page, but every image is reachable by paging.
- Frontend API (`api.ts` `getImagePreview`): optional `offset` param and the
  new typed response fields.
- Wizard (`SyntheticData.tsx`): fixed page size of 12 thumbnails (2 rows × 6
  columns) rendered as a deterministic CSS grid, Prev/Next controls, a
  "Showing A–B of TOTAL" indicator, per-page loading state, and selection
  (`selectedKeys: Set<string>`, already held at wizard level) preserved across
  page changes.

**Why offset paging instead of an S3 continuation-token cursor:** S3 lists keys
in stable lexicographic order, so offset paging is deterministic and
repeatable. Image sets here are modest (tens to low hundreds of keys), so
collecting all matching keys per request is cheap, and the function already
walks all `list_objects_v2` pages. Offset paging is stateless (no opaque token
to thread through the frontend), supports jumping to any page, and makes
`total_found` exact with no extra listing pass. A continuation-token cursor
would only pay off for very large prefixes and would complicate both the API
surface and Prev navigation.

A plain cap bump is explicitly out of scope per the requirements — pagination
must cover any image count.

## Glossary

- **Bug_Condition (C)**: A preview request for a dataset prefix containing
  more than 20 images — images beyond the first 20 lexicographic keys are
  unreachable and unselectable.
- **Property (P)**: With pagination, the union of all pages equals the full
  image set under the prefix, `total_found` is the true total, and selections
  persist across page navigation.
- **Preservation**: Behavior for small prefixes (≤ one page), the
  DatasetBrowser `ImagePreview.tsx` caller (`limit: 12`, no offset), extension
  filtering, presigned-URL expiry, and the untouched `count_images` /
  `list_datasets` operations must remain unchanged.
- **get_image_preview**: The function in
  `edge-cv-portal/backend/functions/datasets.py` that lists image keys under a
  prefix and returns presigned URLs for preview.
- **selectedKeys**: The `Set<string>` of selected Source_Image S3 keys held in
  wizard state in `SyntheticData.tsx`; today cleared only on dataset change.
- **Page**: A fixed-size window of the lexicographically ordered image keys
  under the prefix, addressed by `offset` (0-based index of the first image).
- **PAGE_SIZE**: The wizard's fixed images-per-page constant, 12 (2 rows × 6
  columns of 96px thumbnails).

## Bug Details

### Bug Condition

The bug manifests when the source-image picker previews a dataset prefix
containing more than 20 images. `get_image_preview` clamps
`limit = min(int(params.get('limit', '8')), 20)`, stops collecting keys at the
clamp, and returns no mechanism (offset, cursor, or otherwise) to fetch the
remaining images; the wizard renders the single truncated response as an
unpaged grid.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type PreviewRequest  // (usecase_id, prefix) plus the images under prefix
  OUTPUT: boolean

  // Images beyond the first 20 lexicographic keys are unreachable/unselectable.
  RETURN imageCount(input.prefix) > 20
END FUNCTION
```

### Examples

- Prefix `training-images/` with 63 images (32 `anomaly-*.jpg` before 31
  `normal-*.jpg`): wizard requests `limit: 50`, backend clamps to 20 and
  returns the first 20 `anomaly-*.jpg` keys. Expected: all 63 images reachable
  via pages; actual: no `normal-*.jpg` is ever viewable or selectable, so the
  inpainting path is blocked.
- Prefix with 25 images: expected all 25 reachable; actual only the first 20
  lexicographic keys are shown, the last 5 silently missing with no page
  controls and no indication more exist.
- `total_found` today is `len(image_keys)` after the early break, i.e. the
  *returned* count (≤ 20), not the true total — the UI cannot even tell the
  user that images were omitted.
- Edge case (non-buggy): prefix with 12 images — all are shown and selectable
  today; this must keep working identically (single page).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `ImagePreview.tsx` (used by DatasetBrowser) calls
  `getImagePreview({usecase_id, prefix, limit: 12})` with no offset and expects
  `images`, `total_found`, and `expires_in_seconds`. It must keep receiving a
  valid response and rendering correctly. Note on `total_found` semantics: it
  changes from "returned count" to "true total under the prefix".
  `ImagePreview.tsx` only *displays* it ("Showing {images.length} of
  {totalFound}" and a "Only showing first N images" note when
  `totalFound > images.length`) — it never indexes or slices by it — so the
  true total is safe and actually fixes a latent display bug (today the modal
  claims "Showing 12 of 12" even when more images exist).
- Presigned URL generation keeps `ExpiresIn=1800` and the response keeps
  `expires_in_seconds: 1800`.
- `count_images` and `list_datasets` are not modified in any way.
- Extension filtering keeps the exact set
  `{'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}`.
- The wizard's at-least-one-Source_Image validation and its message are
  unchanged.
- `selectedKeys` is still cleared on dataset change (and only then).

**Scope:**
All inputs that do NOT involve a prefix with more than 20 images should be
completely unaffected by this fix. This includes:
- Small-prefix previews (≤ 12 images in the wizard, ≤ 20 anywhere): every
  image still shown and selectable, single page, Prev/Next disabled or
  equivalent no-op.
- The DatasetBrowser preview modal flow (`ImagePreview.tsx`).
- Error paths (missing `usecase_id`/`prefix` → 400) and all other endpoints in
  `datasets.py`.

## Hypothesized Root Cause

This bug is a designed-in limitation rather than a coding slip; the "root
cause" is fully known from reading the code:

1. **Hard cap with early break in `get_image_preview`**:
   `limit = min(int(params.get('limit', '8')), 20)` clamps every request, and
   the key-collection loop breaks as soon as `len(image_keys) >= limit`. No
   offset, cursor, or page parameter exists, so keys after the cap are
   unreachable by any request.
   - The `list_objects_v2` paginator already iterates all pages — only the
     early break prevents full collection.
2. **`total_found` reports the truncated count**: it is set to
   `len(image_keys)` after the break, so callers cannot detect truncation or
   compute page counts.
3. **Unpaged wizard grid**: `SyntheticData.tsx` fires one
   `getImagePreview({limit: 50})` call per prefix and renders every returned
   thumbnail in a wrapping flex container — no page state, no controls, no
   position indicator.
4. **API client surface**: `api.ts#getImagePreview` accepts only `limit`, so
   even a paginated backend would be unreachable from the frontend without a
   typed `offset` parameter.

## Correctness Properties

Property 1: Bug Condition - Full Reachability via Pagination

_For any_ dataset prefix where the bug condition holds (isBugCondition returns
true, i.e. the prefix contains more than 20 images), the fixed system SHALL
make every image reachable: fetching successive pages (offset = 0, PAGE_SIZE,
2×PAGE_SIZE, … until has_more is false) SHALL yield a union of image keys
exactly equal to the set of all image-extension keys under the prefix, each
response SHALL report total_found equal to the true total image count, and
S3 keys selected on one page SHALL remain selected (and counted) after
navigating to other pages and back, with wizard submission including selected
keys from all pages.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Legacy Callers and Small Prefixes Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns
false — prefixes with no more images than one page — as well as the legacy
no-offset call shapes such as ImagePreview.tsx's `limit: 12`), the fixed
system SHALL produce the same observable result as the original system: the
same set of reachable/selectable images, a valid preview response containing
`images`, `total_found`, and `expires_in_seconds: 1800`, identical extension
filtering, identical `count_images` and `list_datasets` behavior, and the
unchanged at-least-one-Source_Image validation.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Note: Requirement 3.7 (frontend `tsc` type check) is enforced by the build
gate rather than a runtime property — see Testing Strategy.

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/backend/functions/datasets.py`

**Function**: `get_image_preview`

**Specific Changes**:
1. **Add pagination parameters**: parse `offset = max(int(params.get('offset', '0')), 0)`
   and `limit = min(max(int(params.get('limit', '8')), 1), 50)` — the per-page
   cap rises from 20 to 50 (bounded per page; pages cover everything).
2. **Collect all keys, then slice**: remove the early `break`s so the existing
   `list_objects_v2` paginator collects *every* image-extension key under the
   prefix (S3 returns them in stable lexicographic order), then take
   `page_keys = image_keys[offset : offset + limit]`.
3. **Presign only the page**: generate presigned URLs (`ExpiresIn=1800`,
   unchanged) for `page_keys` only.
4. **Response metadata**: return
   `total_found = len(image_keys)` (true total), plus `offset`, `limit`, and
   `has_more = offset + limit < len(image_keys)`; keep `prefix`, `bucket`,
   `images`, and `expires_in_seconds: 1800` exactly as before. Calls without
   `offset` default to `offset=0`, so legacy callers (`limit: 12`) get the
   first 12 images as today.

**File**: `edge-cv-portal/frontend/src/services/api.ts`

**Function**: `getImagePreview`

**Specific Changes**:
5. **Typed pagination surface**: add optional `offset?: number` to the params
   (serialized into the query string only when provided, mirroring `limit`)
   and add `offset: number`, `limit: number`, `has_more: boolean` to the typed
   response.

**File**: `edge-cv-portal/frontend/src/pages/synthetic/SyntheticData.tsx`

**Specific Changes**:
6. **Fixed page size**: export `const SOURCE_PICKER_PAGE_SIZE = 12` (2 rows ×
   6 columns; within the required 10–16 range).
7. **Page state + paged fetch**: add `pageOffset` and `totalImages` state; the
   thumbnail effect requests
   `getImagePreview({usecase_id, prefix, offset: pageOffset, limit: SOURCE_PICKER_PAGE_SIZE})`
   and stores `total_found` in `totalImages`. `thumbsLoading` becomes the
   per-page loading state (spinner while a page loads). Dataset change resets
   `pageOffset` to 0 and clears `selectedKeys` (existing behavior); page
   change does NOT touch `selectedKeys`, so selection persists across pages —
   the selected-count text already reads `selectedKeys.size` and therefore
   reflects selections on all pages.
8. **Deterministic grid**: replace the wrapping flex container with a CSS grid
   (`display: grid; gridTemplateColumns: repeat(6, 96px); gap: 8px`) so a page
   is always 6 columns × 2 rows regardless of container width.
9. **Page controls**: Previous/Next buttons (Previous disabled at
   `pageOffset === 0`, Next disabled when `pageOffset + SOURCE_PICKER_PAGE_SIZE >= totalImages`)
   and a "Showing A–B of TOTAL" indicator where
   `A = pageOffset + 1`, `B = min(pageOffset + SOURCE_PICKER_PAGE_SIZE, totalImages)`.
   With a single page the indicator still renders and both buttons are
   disabled (no behavioral regression for small prefixes).
10. **Submission unchanged**: `handleCreate` already maps
    `Array.from(selectedKeys)` into `source_images`, so keys picked on
    multiple pages flow into the created session with no further change.

No changes to `ImagePreview.tsx`, `count_images`, or `list_datasets`.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface
counterexamples that demonstrate the bug on unfixed code, then verify the fix
works correctly and preserves existing behavior. Backend tests follow the
established pytest + moto pattern in `edge-cv-portal/backend/tests/` (moto-backed
S3/DynamoDB, real `shared_utils` layer, synthetic API Gateway events, as in
`test_property_dataset_discovery_preservation.py`). Frontend tests extend
`edge-cv-portal/frontend/src/pages/synthetic/SyntheticData.test.tsx` (vitest,
mocked `apiService`). All frontend changes must pass the `tsc` production
build gate (Req 3.7).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix. Confirm or refute the root cause analysis. If we refute, we will need
to re-hypothesize.

**Test Plan**: In a new backend test file
`edge-cv-portal/backend/tests/test_property_image_preview_pagination.py`, seed
a moto S3 bucket with more than 20 image keys under a prefix (including the
reported 32 `anomaly-*.jpg` + 31 `normal-*.jpg` shape), invoke
`get_image_preview` through the handler, and assert the pagination property
(union of all pages == all image keys; true total). Run on the UNFIXED code
to observe failures.

**Test Cases**:
1. **Counterexample Reproduction**: 63 images (32 anomaly + 31 normal) under
   `training-images/`; request pages until `has_more` is false and assert the
   union covers all 63 keys including every `normal-*.jpg` (will fail on
   unfixed code: only 20 anomaly keys are ever returned and no paging
   metadata exists)
2. **True Total**: assert `total_found == 63` for the same prefix (will fail
   on unfixed code: `total_found` is the truncated returned count, 20)
3. **Property Sweep**: hypothesis-generated image counts N in [21, 120] and
   mixed key names; union-of-pages == all keys and `total_found == N` (will
   fail on unfixed code for every N > 20)
4. **Cap Probe (edge)**: request `limit=50` on a 25-image prefix; assert more
   than 20 keys are reachable (will fail on unfixed code: clamped to 20)

**Expected Counterexamples**:
- Any prefix with more than 20 images: page-2 images are unreachable and
  `total_found` under-reports.
- Possible causes: the `min(..., 20)` clamp, the early `break` in key
  collection, the absence of any offset/cursor parameter.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
function produces the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  pages := fetchAllPages'(X)            // offset = 0, 12, 24, ... until NOT has_more
  ASSERT union(pages.images).keys = allImageKeys(X.prefix)
  ASSERT pages[i].total_found = imageCount(X.prefix) FOR ALL i
  ASSERT pages are disjoint AND in lexicographic key order
END FOR
```

The exploration tests above become the fix-checking tests once the fix lands:
they must pass on fixed code. The frontend side of Property 1 (selection
persistence, indicator, multi-page submission) is covered by the vitest cases
below.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT reachableImages(F(X)) = reachableImages(F'(X))
END FOR

FOR ALL X IN legacyCallers DO           // ImagePreview.tsx shape: limit=12, no offset
  ASSERT F'(X) contains images, total_found, expires_in_seconds = 1800
  ASSERT F'(X).images = first 12 lexicographic image keys   // same as F(X)
END FOR

ASSERT count_images' = count_images
ASSERT list_datasets' = list_datasets
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because:
- It generates many test cases automatically across the input domain (image
  counts, key names, extensions)
- It catches edge cases that manual unit tests might miss (0 images, exactly
  12, exactly 20, non-image keys mixed in)
- It provides strong guarantees that behavior is unchanged for all non-buggy
  inputs

**Test Plan**: Observe behavior on UNFIXED code first for small prefixes,
legacy `limit: 12` calls, extension filtering, and expiry, then write
property-based tests (same new backend file) capturing that behavior so it
provably survives the fix.

**Test Cases**:
1. **Legacy Caller Preservation**: `limit=12`, no `offset`, on prefixes with
   ≤ 12 and with > 12 images — response has `images` (first 12 lexicographic
   keys), `total_found`, `expires_in_seconds=1800`; verify identical reachable
   set before/after fix for the ≤ 12 case
2. **Small Prefix Preservation (hypothesis)**: N in [0, 20] images — all N
   keys returned on the first page, `has_more` false, same set as unfixed code
3. **Extension Filtering Preservation**: mixed keys (images plus `.txt`,
   `.manifest`, extensionless) — only the six recognized image extensions
   appear, before and after
4. **Untouched Operations**: `count_images` and `list_datasets` responses
   byte-for-byte equivalent on the same seeded bucket before and after the fix
5. **Expiry Preservation**: presign parameters keep `ExpiresIn=1800` and the
   response keeps `expires_in_seconds: 1800`

### Unit Tests

- Backend: offset/limit parsing and clamping (negative offset → 0, limit
  clamped to [1, 50], defaults `offset=0`/`limit=8`), `has_more` correctness at
  boundaries (offset+limit == total, > total), offset beyond total → empty
  `images` with correct `total_found`, missing `usecase_id`/`prefix` → 400
- Frontend (`SyntheticData.test.tsx`, mocked `apiService`):
  - Multi-page navigation: mock 63-image responses page by page; Next/Previous
    fetch with the expected `offset` and render the page's thumbnails
  - Selection persistence: select on page 1, go to page 2 and back — selection
    and the selected-count text (reflecting off-page selections) are intact
  - Indicator: "Showing 1–12 of 63", then "Showing 13–24 of 63" after Next;
    Previous disabled on page 1, Next disabled on the last page
  - Multi-page submission: select keys on two pages, submit, assert
    `createSyntheticSession` receives `source_images` containing keys from
    both pages
  - Per-page loading state and dataset-change reset (page back to 0,
    `selectedKeys` cleared — existing behavior)
  - No-source validation message still blocks submission (Req 3.5)

### Property-Based Tests

- Backend exploration/fix property (hypothesis + moto): for generated image
  counts and key names, union of all pages equals the full key set,
  `total_found` is exact, pages are disjoint and ordered
- Backend preservation property: for generated small prefixes (≤ one page) and
  legacy call shapes, the reachable image set and response shape match unfixed
  behavior
- Extension-filtering property: generated mixes of image and non-image keys —
  filtering invariant under pagination

### Integration Tests

- Backend handler-level flow (moto): seed the 63-image counterexample bucket,
  walk the full wizard fetch sequence (offset 0 → 12 → … → 60) through the
  Lambda handler and confirm every `normal-*.jpg` becomes reachable
- Frontend wizard flow (vitest): full create-session run selecting sources
  from multiple pages, including the inpainting-relevant case of selecting
  `normal-*.jpg` images that only exist on later pages
- Build gate: frontend production `tsc` type check passes with the new API
  types and wizard state (Req 3.7)
