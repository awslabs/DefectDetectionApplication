# Dataset Discovery None Prefix Bugfix Design

## Overview

The `GET /datasets` endpoint returns an empty dataset list for every use case because `get_data_bucket_and_credentials` in `edge-cv-portal/backend/functions/datasets.py` returns `(bucket, None, credentials)` — the prefix slot was hardcoded to `None` in commit 1d42698 ("Support for single account setup"). The caller `list_datasets` composes `search_prefix = f"{base_prefix}{filter_prefix}".strip('/')`, and Python string interpolation turns `None` into the literal `"None"`, so discovery scans the nonexistent S3 prefix `"None/"` and silently returns nothing with HTTP 200.

The fix is surgical: restore prefix derivation inside `get_data_bucket_and_credentials` (from `data_s3_prefix`/`s3_prefix` for data-account setups, `s3_prefix` otherwise, always coercing missing/`None`/empty values to `''`), and make `list_datasets` defensively None-safe so the literal `"None"` can never recur. Credential handling, `count_images`, `get_image_preview`, and the response shape are untouched.

## Glossary

- **Bug_Condition (C)**: `base_prefix` is `None` when `list_datasets` composes `search_prefix` — currently true for every request, since `get_data_bucket_and_credentials` unconditionally returns `None` in the prefix slot
- **Property (P)**: The scanned S3 prefix equals the normalized composition of the use case's configured prefix and the caller's filter prefix, with no `"None"` literal, and discovery returns real image-bearing prefixes
- **Preservation**: Credential resolution (single-account default-credential markers, data-account external-id enforcement), `count_images`, `get_image_preview`, and the `{datasets, bucket, base_prefix}` response shape must remain byte-identical in behavior
- **get_data_bucket_and_credentials**: The function in `edge-cv-portal/backend/functions/datasets.py` that resolves the data bucket, base prefix, and STS credentials for a use case (data account if configured, otherwise the use case account)
- **list_datasets**: The `GET /datasets` handler that composes `search_prefix` and calls `discover_datasets`
- **data_s3_prefix / s3_prefix**: Use case configuration attributes in DynamoDB; the live table contains `null` and empty-string values for these fields, so derivation must use `or ''` semantics, never propagating `None`

## Bug Details

### Bug Condition

The bug manifests whenever `list_datasets` runs: `get_data_bucket_and_credentials` always returns `None` as the base prefix, so `f"{base_prefix}{filter_prefix}"` produces `"None"` or `"None{filter}"`, and discovery scans a prefix that does not exist in the bucket.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type DatasetListRequest (usecase, filter_prefix)
  OUTPUT: boolean

  base_prefix := get_data_bucket_and_credentials(input.usecase).prefix
  RETURN base_prefix IS None
         // currently always true: the function hardcodes None in the prefix slot,
         // so the composed search_prefix contains the literal "None"
END FUNCTION
```

### Examples

- Cookies use case, bucket `ryvan-cookies` containing `training-images/anomaly-*.jpg`, no filter: expected discovery of `training-images/` (scan from `''`); actual scan of `"None/"` returns `[]`
- Any use case with filter prefix `raw`: expected scan of `"raw/"`; actual scan of `"Noneraw/"` returns `[]`
- Data-account use case with `data_s3_prefix: "team-a/"`: expected scan of `"team-a/"`; actual scan of `"None/"` returns `[]`
- Response body: expected `base_prefix: ""` when unset; actual `base_prefix: null`

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `count_images` (POST /datasets) and `get_image_preview` (GET /datasets/preview) unpack the tuple but discard the prefix; they must count/preview under exactly the caller-supplied prefix, byte-identical to today (Req 3.1, 3.2)
- Single-account setups (root-ARN `cross_account_role_arn`): `assume_usecase_role` returns default-credential markers (`AccessKeyId: None` etc.) that flow into `boto3.client` and fall back to the Lambda execution role — unchanged (Req 3.3)
- Data-account setups: `data_account_external_id` remains required, assumed-role credentials used as before (Req 3.4)
- Response shape stays `{datasets, bucket, base_prefix}` (Req 3.5); discovery recursion, image-extension filtering, and sorting unchanged (Req 3.6)

**Scope:**
Only the value placed in the prefix slot of the returned tuple (and its None-safe consumption in `list_datasets`) changes. All credential paths, the other two endpoints, and `discover_datasets` are unaffected.

## Hypothesized Root Cause

Confirmed by code inspection and git history:

1. **Dropped prefix derivation in commit 1d42698**: The single-account refactor rewrote `get_data_bucket_and_credentials` and replaced the previously derived prefix with a hardcoded `None` in the return tuple. This is the root cause.

2. **Unsafe string interpolation in the caller**: `list_datasets` composes `f"{base_prefix}{filter_prefix}"` without guarding against `None`, converting the upstream defect into a silent wrong-prefix scan instead of an error.

3. **Silent failure mode**: `discover_datasets` finds no objects under `"None/"` and returns `[]`; the endpoint responds HTTP 200, so nothing surfaces in the UI or logs.

## Correctness Properties

Property 1: Bug Condition - Dataset Discovery Scans the Configured Prefix

_For any_ use case configuration (with `s3_prefix`/`data_s3_prefix` absent, `None`, empty, or set) and any filter prefix, when the bug condition holds (base prefix would be `None`), the fixed code SHALL derive a string base prefix (defaulting to `''`), compose the scanned S3 prefix as the normalized `"{configured_prefix}{filter_prefix}"` with no `"None"` literal, and discovery SHALL return the image-bearing prefixes actually present in the bucket. The response `base_prefix` SHALL be `''` (not `null`) when unset.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Credentials, Count, and Preview Behavior Unchanged

_For any_ input where the bug condition does NOT hold after the fix's derivation (a configured prefix is present), and for all calls to `count_images` and `get_image_preview` (which discard the prefix), the fixed code SHALL produce the same result as the original function: identical credential resolution for single-account and data-account setups, identical count/preview behavior under caller-supplied prefixes, and the unchanged `{datasets, bucket, base_prefix}` response shape.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/backend/functions/datasets.py`

**Function**: `get_data_bucket_and_credentials`

**Specific Changes**:
1. **Restore prefix derivation (data-account branch)**: derive
   `prefix = usecase.get('data_s3_prefix') or usecase.get('s3_prefix', '') or ''`
   — DynamoDB items contain `null` and empty-string values, so `or ''` semantics are mandatory; the function must never return `None` in the prefix slot
2. **Restore prefix derivation (use-case branch)**: derive
   `prefix = usecase.get('s3_prefix') or ''`
3. **Return the derived prefix**: change `return bucket, None, credentials` to `return bucket, prefix, credentials`

**Function**: `list_datasets`

**Specific Changes**:
4. **Defensive None-safety**: compose the search prefix with `base_prefix = base_prefix or ''` (and use it for the response `base_prefix` field) so the literal `"None"` can never recur even if a future regression reintroduces `None`

No changes to `count_images`, `get_image_preview`, `discover_datasets`, or any credential logic.

## Testing Strategy

### Validation Approach

Two-phase: first run an exploration test on the UNFIXED code to surface the `"None/"` counterexample, then verify the fix (Property 1) and preservation (Property 2). Tests live in `edge-cv-portal/backend/tests/` using pytest + moto, following the established `test_captures.py` pattern: inject a fake `shared_utils` module into `sys.modules` (stubbing `get_usecase` and `assume_usecase_role`) before importing `datasets`, and use moto's mocked S3 for bucket contents.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Create a moto S3 bucket containing image keys under a real prefix (e.g., `training-images/anomaly-1.jpg`), stub `get_usecase` to return a use case with no configured prefix, invoke `list_datasets`, and assert datasets are discovered. Run on the UNFIXED code to observe the empty-list failure.

**Test Cases**:
1. **Unset Prefix Discovery Test**: bucket has `training-images/*.jpg`, use case has no `s3_prefix`; assert `datasets` is non-empty and includes `training-images/` (will fail on unfixed code)
2. **Filter Prefix Test**: supply `prefix=training` filter; assert scan targets `training-images/`-compatible keys, not `Nonetraining/` (will fail on unfixed code)
3. **Response Field Test**: assert `base_prefix` in the response body is `''`, not `null` (will fail on unfixed code)

**Expected Counterexamples**:
- `list_datasets` returns `datasets: []` and `base_prefix: null` even though the bucket contains image-bearing prefixes
- Cause: hardcoded `None` prefix interpolated as the literal `"None"` into the S3 scan prefix

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL (usecase_config, filter_prefix, bucket_contents) WHERE isBugCondition(input) DO
  response := list_datasets_fixed(input)
  ASSERT scanned_prefix = normalize(configured_prefix + filter_prefix)  // no "None" literal
  ASSERT response.datasets = image_bearing_prefixes(bucket_contents, scanned_prefix)
  ASSERT response.base_prefix = configured_prefix or ''
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT datasets_original(input) = datasets_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis) is recommended for preservation checking because:
- It generates varied use case configurations (prefix set/unset/`None`/empty, data-account vs single-account) automatically
- It catches edge cases like DynamoDB `null` values that manual unit tests might miss
- It provides strong guarantees that credential paths and the count/preview endpoints are unchanged

**Test Plan**: Observe behavior on UNFIXED code for `count_images`, `get_image_preview`, and credential resolution, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Count/Preview Preservation**: for random caller-supplied prefixes and bucket contents, `count_images` and `get_image_preview` return identical results before and after the fix (they discard the prefix slot)
2. **Credential Path Preservation**: single-account use cases still pass default-credential markers to `boto3.client`; data-account use cases still require `data_account_external_id` and raise `ValueError` without it
3. **Configured Prefix Preservation**: for use cases with a configured prefix, discovery matches the pre-1d42698 behavior (scan `"{prefix}{filter}"` normalized)
4. **Response Shape Preservation**: response always contains exactly `datasets`, `bucket`, `base_prefix`

### Unit Tests

- Prefix derivation for both branches: `data_s3_prefix` set, `data_s3_prefix: None` falling back to `s3_prefix`, both unset → `''`
- `search_prefix` composition: empty base + empty filter → `''` (bucket-root scan); trailing-slash normalization
- `ValueError` still raised when data account configured without external ID

### Property-Based Tests

- Property 1 (exploration/fix): generate use case configs (prefix absent/`None`/empty/set) and filter prefixes; assert the scanned prefix never contains `"None"` and discovery finds seeded image prefixes
- Property 2 (preservation): generate non-bug inputs and count/preview calls; assert behavior identical to the original implementation

### Integration Tests

- Full `handler` round-trip via moto: `GET /datasets` for the cookies-style layout (`training-images/` with images) returns the dataset and `base_prefix: ''`
- `POST /datasets` and `GET /datasets/preview` round-trips unchanged after the fix
