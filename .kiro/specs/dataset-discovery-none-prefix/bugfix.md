# Bugfix Requirements Document

## Introduction

The dataset discovery endpoint (`GET /datasets`, handled by `edge-cv-portal/backend/functions/datasets.py`) returns an empty dataset list for every use case, causing the "Dataset" dropdown in the Synthetic Data wizard to show "No datasets found" even when the use case's data bucket contains qualifying image prefixes (e.g., `training-images/` with `anomaly-*.jpg` in the `ryvan-cookies` bucket for the cookies use case).

The root cause is that commit 1d42698 ("Support for single account setup") changed `get_data_bucket_and_credentials(usecase)` to return `(bucket, None, credentials)`, dropping the prefix that was previously derived from the use case's `s3_prefix` (or `data_s3_prefix` for data-account setups). The caller `list_datasets` composes `search_prefix = f"{base_prefix}{filter_prefix}".strip('/')` — with `base_prefix = None`, Python string interpolation yields the literal string `"None"`, which becomes `"None/"` after the trailing-slash append. Dataset discovery therefore scans the nonexistent S3 prefix `"None/"` (or `"None{filter}/"` when a filter is supplied), finds nothing, and returns an empty list with HTTP 200 — a silent failure with no error surfaced to the UI.

The same endpoint feeds three UI surfaces: the Synthetic Data wizard dataset picker (`SyntheticData.tsx`), the Dataset Browser page (`DatasetBrowser.tsx`), and dataset selection in training creation. All three are broken by this defect. `count_images` and `get_image_preview` in the same file also call `get_data_bucket_and_credentials` but ignore the prefix return value (they use caller-supplied prefixes), so they are unaffected and must remain unchanged.

## Bug Analysis

### Current Behavior (Defect)

When dataset discovery runs, the `None` base prefix is interpolated into the S3 search prefix as the literal string "None", so discovery scans a prefix that does not exist in the bucket.

1.1 WHEN GET /datasets is called for any use case THEN the system builds the S3 search prefix from a `None` base prefix, producing the literal string "None/" and scanning a nonexistent S3 location

1.2 WHEN GET /datasets is called with a user-supplied filter prefix THEN the system scans the literal prefix "None{filter}/" instead of "{filter}/"

1.3 WHEN the "None/"-prefixed scan finds no objects THEN the system returns an empty dataset list with HTTP 200, and the UI displays "No datasets found" with no error indication, even when the bucket contains image-bearing prefixes such as `training-images/`

1.4 WHEN GET /datasets responds THEN the system returns `base_prefix` as `null` in the response body

### Expected Behavior (Correct)

The search prefix must be derived from the use case's configured prefix (defaulting to the empty string when unset), so discovery scans the real bucket contents while still honoring the user-supplied filter prefix.

2.1 WHEN GET /datasets is called for a use case without a separate data account THEN the system SHALL derive the base prefix from the use case's `s3_prefix` configuration, defaulting to '' when unset

2.2 WHEN GET /datasets is called for a use case with a separate data account configured THEN the system SHALL derive the base prefix from the use case's `data_s3_prefix` configuration (falling back to `s3_prefix`), defaulting to '' when unset

2.3 WHEN GET /datasets is called with a user-supplied filter prefix THEN the system SHALL compose the search prefix as base prefix followed by filter prefix, with no "None" literal appearing in the scanned S3 prefix

2.4 WHEN the use case has no configured prefix and no filter is supplied THEN the system SHALL scan from the bucket root ('') and discover image-bearing prefixes such as `training-images/`

2.5 WHEN GET /datasets responds and no base prefix is configured THEN the system SHALL return `base_prefix` as '' (empty string) rather than `null`

### Unchanged Behavior (Regression Prevention)

The fix must be confined to search-prefix composition for dataset listing; credentials handling, the other two endpoints in the file, and the response shape must be preserved.

3.1 WHEN POST /datasets (count_images) is called with a caller-supplied prefix THEN the system SHALL CONTINUE TO count images under exactly that prefix, unaffected by the prefix returned from `get_data_bucket_and_credentials`

3.2 WHEN GET /datasets/preview (get_image_preview) is called with a caller-supplied prefix THEN the system SHALL CONTINUE TO generate presigned preview URLs for images under exactly that prefix

3.3 WHEN the use case is a single-account setup (cross_account_role_arn is the account root ARN) THEN the system SHALL CONTINUE TO receive default-credential markers (`AccessKeyId: None` etc.) from `assume_usecase_role` and fall back to the Lambda execution role for S3 access

3.4 WHEN the use case is a multi-account or data-account setup THEN the system SHALL CONTINUE TO assume the configured role (requiring `data_account_external_id` for data accounts) and use the assumed credentials for S3 access

3.5 WHEN GET /datasets responds THEN the system SHALL CONTINUE TO return the existing response shape with `datasets`, `bucket`, and `base_prefix` fields

3.6 WHEN dataset discovery scans a valid prefix THEN the system SHALL CONTINUE TO recurse up to `max_depth`, count only image files (.jpg/.jpeg/.png/.bmp/.tiff/.tif), and sort results by image count descending then prefix
