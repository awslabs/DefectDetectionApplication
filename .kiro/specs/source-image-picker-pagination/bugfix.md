# Bugfix Requirements Document

## Introduction

In the Synthetic Data wizard's source-image picker, only a subset of the selected dataset's images is shown and there is no way to reach the rest. The frontend requests up to 50 preview images, but the backend preview endpoint hard-caps the result at 20 presigned URLs with no pagination. Because S3 lists keys lexicographically, a dataset whose `training-images/` prefix contains 32 `anomaly-*.jpg` keys before 31 `normal-*.jpg` keys shows only anomaly images — the normal images are unreachable and unselectable, blocking the inpainting path that requires normal source images.

The fix must provide pagination of the source-image picker across the entire image set of the selected dataset prefix (63 images in the reported case, any count generally), with selection persisting across pages and a page size corresponding to roughly two rows of 96px thumbnails. A plain cap bump is explicitly not acceptable.

Affected code:
- Backend: `edge-cv-portal/backend/functions/datasets.py` (`get_image_preview`) — clamps `limit = min(int(params.get('limit', '8')), 20)` and returns at most 20 presigned URLs with no way to request subsequent images.
- Frontend: `edge-cv-portal/frontend/src/pages/synthetic/SyntheticData.tsx` — calls `apiService.getImagePreview({usecase_id, prefix, limit: 50})` and renders all returned thumbnails in a single unpaged grid.
- Other caller to preserve: `edge-cv-portal/frontend/src/components/ImagePreview.tsx` (used by `DatasetBrowser.tsx`) calls `getImagePreview` with `limit: 12`.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the source-image picker requests previews for a dataset prefix containing more than 20 images THEN the system returns at most 20 presigned URLs (backend clamps any requested limit to 20) and provides no mechanism to fetch the remaining images

1.2 WHEN a dataset prefix's images sort lexicographically such that one class fills the first 20 keys (e.g. 32 `anomaly-*.jpg` before 31 `normal-*.jpg`) THEN the system shows only that class in the picker, making the later-sorting images (the `normal-*.jpg` files) unreachable and unselectable

1.3 WHEN the user needs to select normal source images for the inpainting generation path in such a dataset THEN the system provides no way to view or select them, blocking that path entirely

1.4 WHEN the picker renders the returned previews THEN the system displays them as a single unpaged grid with no page controls and no indication that further images exist beyond those shown

### Expected Behavior (Correct)

2.1 WHEN the source-image picker requests previews for a dataset prefix THEN the system SHALL support pagination over all image keys under that prefix, returning presigned URLs for the requested page together with the total image count and a cursor (or equivalent) for fetching the next page

2.2 WHEN a dataset prefix contains more images than one page THEN the system SHALL make every image under the prefix reachable and selectable by navigating pages, regardless of lexicographic key order or image count

2.3 WHEN the user navigates between pages in the picker THEN the system SHALL preserve the selected S3 keys across page changes (selections made on page 1 remain when navigating to page 2 and back), and the displayed selected count SHALL reflect selections across all pages, including images not on the current page

2.4 WHEN the picker displays a page of thumbnails THEN the system SHALL show a fixed page size corresponding to roughly two rows of 96px thumbnails (a concrete images-per-page in the range of about 10-16, chosen and documented in the design), together with previous/next controls and a position indicator (e.g. "page X of Y" or "showing A-B of TOTAL")

2.5 WHEN the user submits the wizard with source images selected across multiple pages THEN the system SHALL include all selected S3 keys in the created session, not only those visible on the current page

### Unchanged Behavior (Regression Prevention)

3.1 WHEN an existing caller invokes the preview endpoint without pagination parameters (e.g. `ImagePreview.tsx` via DatasetBrowser with `limit: 12`) THEN the system SHALL CONTINUE TO return a valid preview response so that the DatasetBrowser image-preview modal keeps working (either untouched via backward-compatible defaults or updated consistently)

3.2 WHEN presigned URLs are generated for preview images THEN the system SHALL CONTINUE TO use the existing expiry behavior (30-minute `ExpiresIn=1800`, `expires_in_seconds` in the response)

3.3 WHEN the `count_images` and `list_datasets` operations are invoked THEN the system SHALL CONTINUE TO behave exactly as before (no changes to dataset discovery or image counting)

3.4 WHEN the preview endpoint filters keys under the prefix THEN the system SHALL CONTINUE TO include only files with recognized image extensions (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`, `.tif`)

3.5 WHEN the wizard is submitted with no source images selected THEN the system SHALL CONTINUE TO block creation with the at-least-one-Source_Image validation message

3.6 WHEN a dataset prefix contains no more images than one picker page THEN the system SHALL CONTINUE TO let the user view and select all of them as today (single page, no behavioral regression)

3.7 WHEN the frontend production build runs THEN the system SHALL CONTINUE TO pass the `tsc` type check (all frontend changes are type-correct)

## Bug Condition

The bug condition identifies the inputs for which the defect manifests:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type PreviewRequest  // (usecase_id, prefix) with the images under prefix
  OUTPUT: boolean

  // The prefix holds more images than the preview cap, so images beyond
  // the first 20 lexicographic keys are unreachable and unselectable.
  RETURN imageCount(X.prefix) > 20
END FUNCTION
```

## Fix Property

For all buggy inputs, the fixed system must make every image reachable:

```pascal
// Property: Fix Checking - Full reachability via pagination
FOR ALL X WHERE isBugCondition(X) DO
  pages ← fetchAllPages'(X)          // F': paginated preview endpoint
  ASSERT union(pages.images).keys = allImageKeys(X.prefix)   // every image reachable
  ASSERT pages.total = imageCount(X.prefix)                  // accurate total count
  ASSERT selectionsPersistAcrossPages(pages)                 // Set of S3 keys survives navigation
END FOR
```

## Preservation Goal

For all non-buggy inputs and unchanged operations, behavior is identical:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT reachableImages(F(X)) = reachableImages(F'(X))  // small prefixes: all images still shown/selectable
END FOR

// Existing callers without pagination parameters keep working
FOR ALL X IN legacyCallers DO   // e.g. DatasetBrowser ImagePreview (limit: 12)
  ASSERT F'(X) is a valid preview response rendering as before
END FOR

// Untouched operations
ASSERT count_images' = count_images
ASSERT list_datasets' = list_datasets
ASSERT presignedUrlExpiry' = 1800 seconds
```

## Counterexample

Concrete example demonstrating the bug: a use case whose dataset prefix `training-images/` contains 63 images — 32 `anomaly-*.jpg` followed lexicographically by 31 `normal-*.jpg`. The wizard requests `getImagePreview(limit=50)`; the backend clamps to 20 and returns the first 20 lexicographic keys, all `anomaly-*.jpg`. No `normal-*.jpg` image can be viewed or selected, so the inpainting path (which requires normal sources) cannot be used.
