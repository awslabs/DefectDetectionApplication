# Bugfix Requirements Document

## Introduction

On the portal's Data Labeling page (live at `d23v4ltibogb5x.cloudfront.net`, account
164152369890, us-east-1), every labeling job's Created column shows a January 1970 date
(e.g. `1/21/1970, 10:54:03 AM`) instead of the job's actual creation date. The screenshot
shows the jobs table (Job Name, Task Type, Progress, Status, Created) with every row on
1/21/1970. The labeling job detail page has the same defect on its Created / Completed /
Stopped values, and its Duration math is doubly wrong (completed jobs collapse to 0 hours;
ongoing jobs report ~496,000 hours).

Root cause (verified by direct code inspection): the backend persists labeling-job
timestamps as epoch SECONDS — `edge-cv-portal/backend/functions/dda_labeling.py` line 1989
`now = int(datetime.utcnow().timestamp())` (with the same pattern at lines 3029, 3258,
3635, 3719, and 3994 writing `created_at` / `updated_at` / `completed_at` / `stopped_at`),
and the Ground Truth backend `labeling.py` uses the identical convention — while the
frontend render sites pass those values straight to JavaScript's `new Date(...)`, which
expects epoch MILLISECONDS. A September 2026 timestamp (~1.789e9 seconds) interpreted as
milliseconds is ~20.7 days after the epoch: January 21, 1970, matching the screenshot
exactly.

The affected consumption sites are:
- `edge-cv-portal/frontend/src/pages/Labeling.tsx` line 524 (jobs table Created column):
  `cell: (item) => new Date(item.created_at).toLocaleString()`
- `edge-cv-portal/frontend/src/pages/LabelingDetail.tsx` lines 859–872 (DDA job detail:
  `rawJob.created_at` / `rawJob.completed_at` / `rawJob.stopped_at`, each rendered
  `new Date(x).toLocaleString()`)
- `edge-cv-portal/frontend/src/pages/LabelingDetail.tsx` lines 1244–1256 (Ground Truth job
  detail: `job.created_at` / `job.completed_at` rendered the same way, plus Duration math
  `Math.round((job.completed_at - job.created_at) / 3600000)` and
  `Math.round((Date.now() - job.created_at) / 3600000)` that assumes milliseconds)

The fix is applied at the frontend render/consumption sites (seconds → milliseconds
conversion), NOT the backend. The backend's epoch-seconds unit is load-bearing: stored
DynamoDB records are immutable history, backend arithmetic compares seconds, and three
existing frontend call sites already treat these fields as seconds correctly —
`Labeling.tsx` line 596 (the datasets table on the SAME page does
`new Date(item.created_at * 1000)`), `PreLabeledDatasets.tsx` line 338, and
`CreateTraining.tsx` line 188. Changing backend units would corrupt existing data
semantics and break those correct call sites. Zero backend changes.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the Data Labeling page's jobs table renders a job's Created column (`Labeling.tsx` line 524) THEN the system passes the epoch-seconds `created_at` directly to `new Date(...)`, which interprets it as milliseconds, and displays a January 1970 date (e.g. a job created 2026-09-08, `created_at` ≈ 1.789e9, renders as `1/21/1970, 10:54:03 AM`)

1.2 WHEN the labeling job detail page renders a DDA job's Created / Completed / Stopped values (`LabelingDetail.tsx` lines 859–872: `rawJob.created_at`, `rawJob.completed_at`, `rawJob.stopped_at`) THEN the system renders each present epoch-seconds timestamp via `new Date(x).toLocaleString()` and displays January 1970 dates

1.3 WHEN the labeling job detail page renders a Ground Truth job's Created / Completed values (`LabelingDetail.tsx` lines 1244–1249: `job.created_at`, `job.completed_at`) THEN the system renders each epoch-seconds timestamp via `new Date(x).toLocaleString()` and displays January 1970 dates

1.4 WHEN the labeling job detail page computes a Ground Truth job's Duration (`LabelingDetail.tsx` lines 1254–1256) THEN the system divides by 3,600,000 (milliseconds per hour) values that are actually seconds — a completed job's duration `Math.round((completed_at - created_at) / 3600000)` rounds to 0 hours, and the ongoing case `Math.round((Date.now() - created_at) / 3600000)` mixes `Date.now()` milliseconds with an epoch-seconds `created_at`, producing absurd counts around 496,000 hours

1.5 WHEN the backend creates or finalizes a labeling job record (`dda_labeling.py` line 1989 `now = int(datetime.utcnow().timestamp())`, likewise lines 3029, 3258, 3635, 3719, 3994; the Ground Truth backend `labeling.py` uses the same convention) THEN the system persists and returns `created_at` / `updated_at` / `completed_at` / `stopped_at` as epoch SECONDS, while the render sites in 1.1–1.4 interpret those values as epoch MILLISECONDS

### Expected Behavior (Correct)

2.1 WHEN the Data Labeling page's jobs table renders a job's Created column THEN the system SHALL convert the epoch-seconds `created_at` to milliseconds before constructing the Date (equivalent to `new Date(item.created_at * 1000).toLocaleString()`), so the displayed value reflects the job's actual creation date (a current-era date, never year 1970 for any realistic epoch-seconds timestamp)

2.2 WHEN the labeling job detail page renders a DDA job's Created / Completed / Stopped values THEN the system SHALL convert each present epoch-seconds timestamp (`rawJob.created_at`, `rawJob.completed_at`, `rawJob.stopped_at`) to milliseconds before rendering, displaying the actual calendar dates

2.3 WHEN the labeling job detail page renders a Ground Truth job's Created / Completed values THEN the system SHALL convert each present epoch-seconds timestamp (`job.created_at`, `job.completed_at`) to milliseconds before rendering, displaying the actual calendar dates

2.4 WHEN the labeling job detail page computes a Ground Truth job's Duration THEN the system SHALL compute hours from consistent units — completed: `(completed_at - created_at)` seconds divided by 3,600; ongoing: `(Date.now() / 1000 - created_at)` divided by 3,600 (equivalently, convert both operands to milliseconds and divide by 3,600,000) — yielding realistic hour counts (a 2-hour job reports 2 hours, not 0; an ongoing job created hours ago reports single-digit hours, not ~496,000)

2.5 WHEN the fix is applied THEN the system SHALL change only the frontend consumption sites in `Labeling.tsx` and `LabelingDetail.tsx` — zero backend changes; epoch seconds remain the persisted storage unit and the API contract for labeling-job timestamps

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the pre-labeled datasets table on the same Data Labeling page renders a dataset's Created column (`Labeling.tsx` line 596, already `new Date(item.created_at * 1000).toLocaleDateString()`) THEN the system SHALL CONTINUE TO render it exactly as before — this already-correct call site is not modified

3.2 WHEN `PreLabeledDatasets.tsx` (line 338) and `CreateTraining.tsx` (line 188) render labeling-derived `created_at` values THEN the system SHALL CONTINUE TO render `new Date(created_at * 1000)` exactly as before — these already-correct call sites are not modified

3.3 WHEN the backend creates, updates, or finalizes labeling job records THEN the system SHALL CONTINUE TO persist and return `created_at` / `updated_at` / `completed_at` / `stopped_at` as epoch seconds — stored DynamoDB records are immutable history, backend arithmetic compares seconds, and existing consumers already treat these fields as seconds

3.4 WHEN a job's `completed_at` or `stopped_at` is absent THEN the system SHALL CONTINUE TO render the `'-'` placeholder in the detail page's key-value pairs (the existing falsy guards are unchanged)

3.5 WHEN the jobs table and the labeling job detail page render non-timestamp content (job name, task type, progress bar, status indicator, label set, console / worker-portal / output links) THEN the system SHALL CONTINUE TO render it unchanged

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type LabelingJobTimestampRender
  OUTPUT: boolean

  // X = (field, value, site): a labeling-job timestamp consumption on the
  // labeling pages. The bug condition holds for every PRESENT epoch-seconds
  // timestamp consumed by one of the four defective render sites, which
  // interpret the value as epoch milliseconds.
  RETURN X.value IS PRESENT
     AND X.field IN [created_at, completed_at, stopped_at]
     AND X.site IN [jobsTableCreatedColumn,            // Labeling.tsx:524
                    ddaDetailKeyValuePairs,            // LabelingDetail.tsx:859-872
                    groundTruthDetailKeyValuePairs,    // LabelingDetail.tsx:1244-1249
                    groundTruthDurationMath]           // LabelingDetail.tsx:1254-1256
END FUNCTION
```

### Property: Fix Checking

```pascal
// For every consumption where the bug condition holds, the fixed render
// treats the stored value as epoch seconds.
FOR ALL X WHERE isBugCondition(X) DO
  rendered ← render'(X)
  IF X.site IN [jobsTableCreatedColumn, ddaDetailKeyValuePairs,
                groundTruthDetailKeyValuePairs] THEN
    ASSERT rendered = new Date(X.value * 1000).toLocaleString()
    // For any realistic epoch-seconds value (2020-2040 range),
    // the rendered year is never 1970.
  ELSE  // groundTruthDurationMath
    ASSERT completedHours = round((completed_at - created_at) / 3600)
    ASSERT ongoingHours   = round((Date.now() / 1000 - created_at) / 3600)
  END IF
END FOR
```

### Property: Preservation Checking

```pascal
// For all non-buggy inputs, the fixed frontend behaves identically to the
// original: the already-correct call sites, the '-' placeholders for absent
// timestamps, all non-timestamp rendering, and the backend's epoch-seconds
// records are untouched.
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT render(X) = render'(X)
END FOR
```

**Key Definitions:**
- **F**: the labeling pages' timestamp rendering as it exists before the fix
  (`Labeling.tsx` / `LabelingDetail.tsx` passing epoch seconds to `new Date(...)`)
- **F'**: the same rendering after the seconds → milliseconds conversion is applied at
  the four defective sites
- **Counterexample**: a job with `created_at = 1789000000` (September 2026) whose jobs-table
  Created cell renders as a `1/21/1970` date, and whose ongoing Duration reports
  ~496,000 hours
