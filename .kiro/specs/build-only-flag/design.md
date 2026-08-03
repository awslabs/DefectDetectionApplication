# Design Document: build-only-flag

## Overview

Add a `SKIP_PUBLISH=1` build-only mode to `gdk-component-build-and-publish.sh`, symmetric with the existing `SKIP_BUILD=1` publish-only mode. In build-only mode the script runs the full clean + `gdk component build` + packaging path, then exits successfully without publishing, tagging, or offering the InferenceUploader build. The change is confined to one file — the build script itself. `build-custom.sh`, the recipes, and GDK config generation are untouched.

The mode is env-var driven (not a positional flag) to match the script's established `SKIP_BUILD=1` convention exactly. The existing positional argument parser (`arch`, `jetpack`) is a strict allowlist that errors on unknown args, so adding a `--build-only` alias there would work too, but the env-var is the convention users of this script already know; we keep a single mechanism for consistency.

## Design Decision: Credential Handling in Build-Only Mode

**Decision: relax the up-front AWS credential check to a non-fatal warning when `SKIP_PUBLISH=1`, and tolerate a missing region by substituting a placeholder in `gdk-config.json`.**

Rationale, from investigating the build path:

1. `.kiro/steering/builds.md` states explicitly: "Build only (no AWS creds needed): `gdk component build`."
2. `build-custom.sh` (the entire Build_Phase) makes no AWS API calls — it runs the version audit, docker-compose builds, in-image test gates, `docker save`, and `zip`. No `aws` CLI usage, no ECR login.
3. All Docker base images pull anonymously: the Jetson Dockerfiles default `BASE_REGISTRY=nvcr.io` (NGC public images, digest-pinned, no auth), and the x86/frontend/edgemlsdk Dockerfiles use `public.ecr.aws` (anonymous pulls). No ECR private-registry login is required for base image pulls during build.
4. The only pre-publish AWS touchpoints in the wrapper script are (a) the `sts get-caller-identity` pre-flight, (b) the `export-credentials` propagation for GDK, and (c) region resolution for the `publish` block of the generated `gdk-config.json` — a block that `gdk component build` never uses.

So credentials are genuinely unnecessary for build-only. However, the check is kept as a **warning** rather than removed, for two reasons: it costs nothing when creds are valid, and if the user intends to publish later (`SKIP_BUILD=1` re-run) an early heads-up about an expired session is still useful. The `export-credentials` propagation block is likewise allowed to no-op silently in build-only mode (it already tolerates failure).

For the region: `gdk-config.json` requires a `publish.region` value to be syntactically valid, but build never reads it. In build-only mode, if no region resolves, use `us-east-1` as a placeholder (with an informational note) instead of hard-failing. Outside build-only mode the existing hard failure stays.

## Architecture

Single-file change to `gdk-component-build-and-publish.sh`. The script's linear flow gains three guards:

```
┌─ conflict guard (new, first) ──────────────────────────────┐
│ SKIP_BUILD=1 && SKIP_PUBLISH=1 → error, exit 1             │
└────────────────────────────────────────────────────────────┘
┌─ credential pre-flight (modified) ─────────────────────────┐
│ sts get-caller-identity fails:                             │
│   SKIP_PUBLISH=1 → warn, continue                          │
│   otherwise      → error, exit 1 (unchanged)               │
└────────────────────────────────────────────────────────────┘
┌─ region resolution (modified) ─────────────────────────────┐
│ no region:                                                 │
│   SKIP_PUBLISH=1 → placeholder us-east-1, note, continue   │
│   otherwise      → error, exit 1 (unchanged)               │
└────────────────────────────────────────────────────────────┘
   clean → gdk component build → (unchanged)
┌─ publish/tag/uploader gate (new) ──────────────────────────┐
│ SKIP_PUBLISH=1 → print "build complete, publish skipped —  │
│   re-run with SKIP_BUILD=1 to publish", exit 0             │
│ otherwise → publish → tag → InferenceUploader (unchanged)  │
└────────────────────────────────────────────────────────────┘
```

## Components and Interfaces

### 1. Conflict guard (Requirement 3.1)

Placed immediately after the shebang/set lines, before the credential pre-flight, so a contradictory invocation fails in milliseconds with no side effects:

```bash
# SKIP_BUILD=1 skips the build (publish-only); SKIP_PUBLISH=1 skips the
# publish (build-only). Setting both would mean "do nothing" — reject it.
if [ "${SKIP_BUILD:-0}" = "1" ] && [ "${SKIP_PUBLISH:-0}" = "1" ]; then
    echo "❌ ERROR: SKIP_BUILD=1 and SKIP_PUBLISH=1 are mutually exclusive"
    echo "   (together they would skip both the build and the publish)."
    exit 1
fi
```

### 2. Credential pre-flight relaxation (Requirements 2.1, 2.2)

Wrap the existing hard-fail branch: when `SKIP_PUBLISH=1`, downgrade to a warning and skip the `exit 1`. The `aws configure export-credentials` propagation block already degrades gracefully and needs no change.

```bash
if ! CALLER_IDENTITY=$(aws sts get-caller-identity 2>&1); then
    if [ "${SKIP_PUBLISH:-0}" = "1" ]; then
        echo "⚠ AWS credentials are not valid — continuing anyway (SKIP_PUBLISH=1;"
        echo "  the build needs no AWS credentials; base images pull anonymously)."
        echo "  Re-authenticate before publishing with SKIP_BUILD=1."
    else
        # ... existing error block, exit 1 (unchanged)
    fi
fi
```

### 3. Region placeholder (Requirement 2.3)

In the existing `GDK_REGION` resolution, replace the hard failure with a placeholder only when `SKIP_PUBLISH=1` (the generated `gdk-config.json` publish block is unused by `gdk component build`):

```bash
if [ -z "$GDK_REGION" ]; then
    if [ "${SKIP_PUBLISH:-0}" = "1" ]; then
        GDK_REGION="us-east-1"
        echo "ℹ No AWS region configured — using placeholder '${GDK_REGION}' in"
        echo "  gdk-config.json (unused during build-only)."
    else
        # ... existing error block, exit 1 (unchanged)
    fi
fi
```

### 4. Publish gate (Requirements 1.1, 1.2, 1.3, 1.4)

Insert immediately before `print_step "Publishing LocalServer component"`. Everything from the publish step through the InferenceUploader prompt is skipped by an early, successful exit — the least invasive structure (no re-indenting of ~200 lines of publish/tag logic):

```bash
if [ "${SKIP_PUBLISH:-0}" = "1" ]; then
    print_step "Skipping publish (SKIP_PUBLISH=1)"
    echo "⏭  SKIP_PUBLISH=1 — build artifacts are in greengrass-build/artifacts/"
    echo "   To publish this build later without rebuilding, run:"
    echo "     SKIP_BUILD=1 $0 $*"
    END_TIME=$(date +%s)
    echo "✅ Build complete (publish skipped). Total time: $((END_TIME - START_TIME))s"
    exit 0
fi
```

Note: `STEP`/`TOTAL_STEPS` counters — the early exit means fewer steps print in build-only mode; `TOTAL_STEPS` stays 8 (the header is cosmetic and SKIP_BUILD already under-runs it). Not worth dynamic computation.

### 5. Usage/help text (Requirement 4.1)

Extend the usage comment block near the argument docs:

```bash
# Environment variables:
#   SKIP_BUILD=1    Publish-only: re-publish existing greengrass-build/ artifacts
#                   without rebuilding (e.g. after a transient publish failure).
#   SKIP_PUBLISH=1  Build-only: build + package the component, then exit without
#                   publishing/tagging (no valid AWS credentials required).
#                   Mutually exclusive with SKIP_BUILD=1.
#
# Examples:
#   SKIP_PUBLISH=1 ./gdk-component-build-and-publish.sh aarch64 6   # build only
#   SKIP_BUILD=1   ./gdk-component-build-and-publish.sh aarch64 6   # publish only
```

## Data Models

None — this change introduces no new data structures. The only new state is the `SKIP_PUBLISH` environment variable, read with the same `"${VAR:-0}" = "1"` idiom as `SKIP_BUILD`.

## Error Handling

| Condition | Mode | Behavior |
|---|---|---|
| `SKIP_BUILD=1` + `SKIP_PUBLISH=1` | any | Error + exit 1 before any action (Req 3.1) |
| Invalid/expired AWS credentials | build-only | Warning, continue (Req 2.1) |
| Invalid/expired AWS credentials | default / publish-only | Error + exit 1, unchanged (Req 2.2) |
| No AWS region resolvable | build-only | Placeholder `us-east-1` + note, continue (Req 2.3) |
| No AWS region resolvable | default / publish-only | Error + exit 1, unchanged |
| Build failure (`gdk component build`) | any | Unchanged: tail log, exit 1 |

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — a formal statement about what the system should do.*

The prework analysis classified nearly every acceptance criterion as SMOKE or EXAMPLE: the change is control flow in a long-running, side-effecting shell script, so most criteria cannot be exercised repeatedly. One criterion is safely universally quantifiable, because the conflict guard runs before argument parsing, credential checks, and any side effect, and exits in milliseconds:

### Property 1: Conflicting modes always rejected before any action

For any command-line argument vector (valid or invalid arch/JetPack arguments, in any order), invoking the Build_Script with both `SKIP_BUILD=1` and `SKIP_PUBLISH=1` set SHALL exit with a non-zero code, print the mutual-exclusion error, and perform no build or publish side effect (no directories removed, no files created or modified).

**Validates: Requirements 3.1**

## Testing Strategy

Property-based testing is largely inapplicable: the script is a long-running (~1–2h), side-effecting orchestration of Docker/GDK/AWS. The single exception is Property 1 (the conflict guard), which fires before any side effect and completes in milliseconds, so it can be exercised across arbitrary argument vectors. Everything else is verified by syntax check, fast-failing example checks, and code review.

Verification approach:

1. **Syntax check**: `bash -n gdk-component-build-and-publish.sh` after editing.
2. **Fast-failing example checks** (no build triggered, each completes in seconds):
   - Conflict guard: `SKIP_BUILD=1 SKIP_PUBLISH=1 ./gdk-component-build-and-publish.sh` → non-zero exit, conflict message, no files touched.
   - Default-path preservation: with deliberately broken credentials (`AWS_ACCESS_KEY_ID=bad AWS_SECRET_ACCESS_KEY=bad AWS_SESSION_TOKEN=bad`, no SKIP vars), the script still exits 1 at the pre-flight (Req 2.2) — this fails before any build starts, so it is safe to run even per the concurrent-build constraint.
3. **Code review** of the guard placements (publish gate must precede the credential re-resolution and artifact-size measurement; conflict guard must precede everything).
4. Full end-to-end build-only run is deferred to normal usage — a real build takes 1–2h, must not run concurrently with another build (builds.md), and one is currently running.

No existing tests in `test/backend-test` reference this script (verified by grep), so there are no test files to update.
