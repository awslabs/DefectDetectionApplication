# Bugfix Requirements Document

## Introduction

The Node Designer plugin import feature records an advisory per-platform compatibility check on imported plugins. When an imported source requires a newer GStreamer than a target platform's build image provides (e.g. gst-plugins-good `main` requires >= 1.19.0, but arm64 JetPack 4 ships 1.14 and arm64 JetPack 5 ships 1.16), the plugin detail page shows a warning under the affected build entry with a suggested revision: "Import revision 1.14 for this platform instead."

The bug is that this advice is a dead end. Per-architecture source revisions (`arch_revisions`) can only be set at import time on POST /plugins/import. Once a plugin is imported, there is no way to apply a different source revision to specific platforms: the detail page's "Retry build" simply re-submits the same already-fetched source tree, which fails again for the same reason. The user's only recourse is to delete the record and re-import from scratch with the per-architecture overrides they now know they need.

The fix gives the user a way to act on the warning: adjust the imported plugin's source revision for the affected platform(s) directly from the plugin detail page (defaulting to the recorded `suggestedRevision`), fetch the adjusted revision's source tree, and re-run the affected builds so they can succeed.

## Bug Analysis

### Current Behavior (Defect)

When an imported plugin has platforms whose compatibility check failed with a known suggested revision, the system offers no action to apply that suggestion:

1.1 WHEN an imported plugin's build entry shows an incompatible-platform warning with a suggested revision THEN the system displays the advice but provides no control to apply the suggested revision to that platform

1.2 WHEN the user retries a failed build for a platform whose warning suggests a different revision THEN the system re-submits the build from the same incompatible source tree and the build fails again for the same reason

1.3 WHEN the user attempts to change an imported plugin's per-platform source revision after import THEN the system provides no API or UI path to do so (per-architecture revisions are accepted only at import time on POST /plugins/import)

1.4 WHEN a source needs different revisions per platform and the user did not set per-architecture overrides at import time THEN the system leaves the affected platforms permanently unable to build, forcing the user to delete the record and re-import from scratch

### Expected Behavior (Correct)

2.1 WHEN an imported plugin's build entry shows an incompatible-platform warning with a suggested revision THEN the system SHALL offer an action on the plugin detail page to apply a revision override for that platform, pre-filled with the recorded suggested revision and editable by the user

2.2 WHEN the user applies a per-platform revision override to an imported plugin THEN the system SHALL fetch the overridden revision's source tree (reusing an already-fetched tree when the same revision is already recorded in the fetches map) and record the platform's arch_revisions mapping so its builds resolve to the adjusted tree

2.3 WHEN the adjusted revision's source fetch succeeds THEN the system SHALL re-run the affected platform's build from the adjusted source tree so the build can succeed

2.4 WHEN the adjusted revision's source fetch fails (unreachable repository or nonexistent revision) THEN the system SHALL surface the fetch failure on the affected platform's entry without altering the plugin's other platforms or its existing successful builds

2.5 WHEN the user applies a revision override THEN the system SHALL require the same permission as build submission (node-designer:manage) and reject the request for records that are not imports or whose import has not settled

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a plugin is imported with per-architecture revision overrides at import time THEN the system SHALL CONTINUE TO fetch each distinct revision once, map architectures to their revision slugs, and build each architecture from its own source tree

3.2 WHEN a platform's compatibility entry is compatible (or carries no suggested revision) THEN the system SHALL CONTINUE TO display the build entry (and any warning) as it does today, without altering its revision or source tree

3.3 WHEN the user retries a failed build without applying a revision override THEN the system SHALL CONTINUE TO re-submit the build from the platform's currently recorded source tree

3.4 WHEN a single-revision import has no per-architecture overrides THEN the system SHALL CONTINUE TO use the flat source_s3_prefix layout for builds, source inspection, and record display

3.5 WHEN builds for other platforms are running or have succeeded THEN the system SHALL CONTINUE TO leave their build status, artifacts, checksums, and signatures untouched by another platform's revision adjustment

3.6 WHEN component auto-packaging is triggered after all requested builds settle THEN the system SHALL CONTINUE TO trigger it exactly once per build round, including rounds completed by a revision-adjusted retry
