# Bugfix Requirements Document

## Introduction

The "adjust revision for this platform" action (delivered by the imported-plugin-revision-adjustment-fix spec) lets a user apply a compatible source revision to an imported plugin's platform and re-run its build. In practice, the FIRST build started right after applying the adjustment ALWAYS compiles the ORIGINAL revision's source tree and fails (e.g. meson requires `gstreamer-1.0 >= 1.19.0` while the build image ships 1.16.3); manually re-running the build then compiles the adjusted revision's tree and succeeds.

The root cause is a read-after-write race. The adjustment paths write the record's revision mapping (`arch_revisions` / `fetches`) with an `update_item`, then immediately auto-start the queued builds. That auto-start re-reads the record with an eventually-consistent DynamoDB `get_item` (no `ConsistentRead`), which can return the pre-write item. With the stale item, the build's source prefix resolution finds no adjusted mapping and falls back to the original revision's tree, so the CodeBuild `sourceLocationOverride` points at the wrong source. The race exists on both adjustment paths: the fetch-success result handler and the reuse path that maps an architecture to an already-fetched revision.

The fix must ensure a build auto-started immediately after an adjustment write always resolves its source from the post-write record state, without changing any other read path or build flow.

## Bug Analysis

### Current Behavior (Defect)

A queued per-arch build is auto-started within the same invocation immediately after a write that changed the record's revision mapping, and the auto-start's eventually-consistent re-read returns the pre-write item:

1.1 WHEN an adjustment fetch succeeds and the auto-started build's re-read of the record returns the pre-write item THEN the system resolves the architecture's source prefix without the just-written arch_revisions mapping and submits the build from the original revision's source tree, which fails

1.2 WHEN an adjustment reuses an already-fetched revision and the auto-started build's re-read of the record returns the pre-write item THEN the system resolves the architecture's source prefix without the just-written arch_revisions mapping and submits the build from the original revision's source tree, which fails

1.3 WHEN the first build after an adjustment fails this way and the user manually retries THEN the system reads fresh state, resolves the adjusted revision's source prefix, and the build succeeds — masking the defect as a transient failure

### Expected Behavior (Correct)

2.1 WHEN an adjustment fetch succeeds and the queued builds are auto-started THEN the system SHALL resolve each pending architecture's source prefix from the post-write record state so the build compiles the adjusted revision's source tree

2.2 WHEN an adjustment reuses an already-fetched revision and the queued build is auto-started THEN the system SHALL resolve the architecture's source prefix from the post-write record state so the build compiles the adjusted revision's source tree

2.3 WHEN a build is auto-started immediately after any write that changed the record's revision mapping THEN the system SHALL submit the CodeBuild sourceLocationOverride with a prefix that reflects the just-written mapping, regardless of DynamoDB read consistency timing

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a build is auto-started for an architecture whose revision mapping was not changed by the adjustment THEN the system SHALL CONTINUE TO resolve its source prefix exactly as today (per-arch mapping if present, otherwise the flat source_s3_prefix)

3.2 WHEN a single-revision record has no per-architecture revision mappings THEN the system SHALL CONTINUE TO build from the flat source_s3_prefix layout

3.3 WHEN queued builds are auto-started after an import-time fetch settles (the original import flow, no adjustment involved) THEN the system SHALL CONTINUE TO start them with the same idempotency guarantees: already-started architectures are left alone, unconfigured architectures stay queued, and a StartBuild failure is recorded without raising to the caller

3.4 WHEN the user manually retries a failed build THEN the system SHALL CONTINUE TO re-submit it from the platform's currently recorded source tree

3.5 WHEN other callers read the plugin record (detail page, listing, source inspection, build-result handling) THEN the system SHALL CONTINUE TO behave as today, unaffected by the fix

3.6 WHEN an adjustment fetch fails THEN the system SHALL CONTINUE TO surface the failure on the affected platform's entry without starting builds or altering other platforms
