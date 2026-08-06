#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# portal-build-agent.sh — SSM-executed build agent for the portal build fleet.
#
# Executed on a Build_Server (ephemeral runner or dedicated server) via SSM
# SendCommand (AWS-RunShellScript with CloudWatchOutputConfig streaming stdout
# to /dda/portal-builds/{BUILD_JOB_ID}). Wraps the non-interactive
# portal-build.sh entry point and reports Build_Job phase transitions to the
# portal Build_Manager as EventBridge events (source dda.portal.builds,
# detail-type BuildPhaseChange).
#
# Parameters (environment variables, or KEY=VALUE command-line arguments):
#   BUILD_JOB_ID   (required)  Build_Job identifier, included in every event
#   BUILD_TARGET   (required)  One of: JP5, JP6, AMD64, AMD64_NVIDIA
#   EVENT_BUS      (optional)  EventBridge bus name for phase events; when
#                              unset, events are skipped (standalone/debug run)
#   SOURCE_REF     (optional)  Git ref to sync the repo clone to before the
#                              build; when unset, the checked-out tree is used
#
# Behavior (design §5 "Build agent"; Requirements 5.2, 5.3, 5.4, 7.1):
#   1. flock -n on /var/lock/dda-build.lock — on-server build mutual
#      exclusion (Req 7.1). Exits 75 (EX_TEMPFAIL) when the lock is held so
#      the dispatcher can defer and retry.
#   2. Sync the repo clone to SOURCE_REF (git fetch + checkout).
#   3. Emit phase=building.
#   4. Map BUILD_TARGET → portal-build.sh arguments and run the build+publish:
#        JP5          → aarch64 5       (aws.edgeml.dda.LocalServer.arm64JP5)
#        JP6          → aarch64 6       (aws.edgeml.dda.LocalServer.arm64JP6)
#        AMD64        → x86_64          (aws.edgeml.dda.LocalServer.amd64)
#        AMD64_NVIDIA → x86_64_nvidia   (aws.edgeml.dda.LocalServer.amd64Nvidia)
#      (portal-build.sh itself emits phase=publishing between build and
#      publish, Req 5.1/5.2.)
#   5. On success: emit phase=succeeded with the result metadata parsed from
#      the PORTAL_BUILD_RESULT line (component name, published version,
#      pushed image refs — Req 5.3).
#      On failure: emit phase=failed, distinguishing the build stage from the
#      publish stage — a publish-stage failure carries error_kind=publishing
#      plus per-artifact published/unpublished lists (Req 5.4).
#   6. The lock is released implicitly on exit.
#
# Exit codes:
#   0   build and publish succeeded
#   64  usage error (missing/invalid parameters or unsupported target)
#   75  build lock held by another build (dispatcher should defer, Req 7.5/7.6)
#   *   the portal-build.sh exit code on build/publish failure
# ─────────────────────────────────────────────────────────────────────────────
set -u
set -o pipefail

# ── Parameter resolution: env vars, overridable by KEY=VALUE args ───────────
for arg in "$@"; do
    case "$arg" in
        BUILD_JOB_ID=*)  BUILD_JOB_ID="${arg#BUILD_JOB_ID=}" ;;
        BUILD_TARGET=*)  BUILD_TARGET="${arg#BUILD_TARGET=}" ;;
        EVENT_BUS=*)     EVENT_BUS="${arg#EVENT_BUS=}" ;;
        SOURCE_REF=*)    SOURCE_REF="${arg#SOURCE_REF=}" ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            echo "Usage: $0 [BUILD_JOB_ID=<id>] [BUILD_TARGET=<JP5|JP6|AMD64|AMD64_NVIDIA>] [EVENT_BUS=<bus>] [SOURCE_REF=<ref>]" >&2
            exit 64
            ;;
    esac
done

BUILD_JOB_ID="${BUILD_JOB_ID:-}"
BUILD_TARGET="${BUILD_TARGET:-}"
EVENT_BUS="${EVENT_BUS:-}"
SOURCE_REF="${SOURCE_REF:-}"

if [ -z "$BUILD_JOB_ID" ] || [ -z "$BUILD_TARGET" ]; then
    echo "ERROR: BUILD_JOB_ID and BUILD_TARGET are required." >&2
    exit 64
fi

# ── JSON helpers (dependency-free; error text may contain quotes/newlines) ──
# Escape a string for embedding inside a JSON string value: backslashes,
# double quotes, and control characters (newlines/tabs become spaces).
json_escape() {
    printf '%s' "$1" \
        | tr '\n\r\t' '   ' \
        | tr -d '\000-\010\013\014\016-\037' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# Emit a BuildPhaseChange event carrying the given detail JSON object to the
# EventBridge bus named by EVENT_BUS. Retries a few times: phase events are how
# the portal tracks this Build_Job, so delivery matters more than in
# portal-build.sh — but a persistent failure must not mask the build result,
# so it degrades to a warning (the SSM command status is the backstop).
emit_event() {
    local detail="$1"
    if [ -z "$EVENT_BUS" ]; then
        echo "ℹ EVENT_BUS not set — skipping event: $detail"
        return 0
    fi
    local escaped entries attempt
    escaped=$(json_escape "$detail")
    entries=$(printf '[{"Source":"dda.portal.builds","DetailType":"BuildPhaseChange","Detail":"%s","EventBusName":"%s"}]' \
        "$escaped" "$EVENT_BUS")
    for attempt in 1 2 3; do
        if aws events put-events --entries "$entries" \
            --query 'FailedEntryCount' --output text 2>/dev/null | grep -q '^0$'; then
            echo "✓ Emitted event to bus ${EVENT_BUS}: $detail"
            return 0
        fi
        echo "⚠ put-events attempt ${attempt}/3 failed for bus ${EVENT_BUS}"
        sleep 5
    done
    echo "⚠ Warning: failed to emit event after 3 attempts (detail: $detail)"
    return 0
}

emit_failed() {
    local error_kind="$1" error_message="$2" extra="${3:-}"
    local msg detail
    msg=$(json_escape "$error_message")
    detail=$(printf '{"build_job_id":"%s","phase":"failed","build_target":"%s","error_kind":"%s","error_message":"%s"%s}' \
        "$BUILD_JOB_ID" "$BUILD_TARGET" "$error_kind" "$msg" "$extra")
    emit_event "$detail"
}

# ── Locate the repo clone (this script lives in <repo>/scripts/) ────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || {
    echo "ERROR: cannot cd to repo dir '${REPO_DIR}'" >&2
    exit 64
}

# ── Step 1: on-server build mutual exclusion (Req 7.1) ──────────────────────
# Defense in depth under the dispatcher's DynamoDB server-allocation lock and
# pre-dispatch pgrep verification (.kiro/steering/builds.md: never run two
# component builds at the same time). flock is held for the lifetime of this
# process via FD 9 and released implicitly on exit.
LOCK_FILE="/var/lock/dda-build.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Build lock ${LOCK_FILE} is held by another build — deferring (exit 75)."
    exit 75
fi
echo "✓ Acquired build lock ${LOCK_FILE}"

# ── Validate the Build_Target and map it to portal-build.sh arguments ───────
case "$BUILD_TARGET" in
    JP5)          BUILD_ARGS=(aarch64 5) ;;
    JP6)          BUILD_ARGS=(aarch64 6) ;;
    AMD64)        BUILD_ARGS=(x86_64) ;;
    AMD64_NVIDIA) BUILD_ARGS=(x86_64_nvidia) ;;
    *)
        echo "ERROR: unsupported BUILD_TARGET '${BUILD_TARGET}' (supported: JP5, JP6, AMD64, AMD64_NVIDIA)" >&2
        emit_failed "building" "Unsupported BUILD_TARGET '${BUILD_TARGET}' (supported: JP5, JP6, AMD64, AMD64_NVIDIA)"
        exit 64
        ;;
esac

# ── Step 2: sync the source tree to SOURCE_REF ───────────────────────────────
if [ -n "$SOURCE_REF" ]; then
    echo "Syncing source to ref '${SOURCE_REF}'..."
    if ! git fetch --prune origin 2>&1; then
        emit_failed "building" "git fetch failed while syncing to source ref '${SOURCE_REF}'"
        exit 1
    fi
    if git rev-parse --verify --quiet "refs/remotes/origin/${SOURCE_REF}" >/dev/null; then
        # Branch: (re)create the local branch at the remote tip so the tree
        # matches the requested ref exactly.
        if ! git checkout --force -B "$SOURCE_REF" "origin/${SOURCE_REF}" 2>&1; then
            emit_failed "building" "git checkout of branch '${SOURCE_REF}' failed"
            exit 1
        fi
    else
        # Tag or commit SHA.
        if ! git checkout --force "$SOURCE_REF" 2>&1; then
            emit_failed "building" "git checkout of ref '${SOURCE_REF}' failed (ref not found?)"
            exit 1
        fi
    fi
    echo "✓ Source synced to $(git rev-parse --short HEAD) (${SOURCE_REF})"
else
    echo "ℹ SOURCE_REF not set — building the currently checked-out tree ($(git rev-parse --short HEAD 2>/dev/null || echo 'unknown'))"
fi

# ── Step 3: report the build start (queued/provisioning → building) ─────────
emit_event "$(printf '{"build_job_id":"%s","phase":"building","build_target":"%s","source_ref":"%s"}' \
    "$BUILD_JOB_ID" "$BUILD_TARGET" "$(json_escape "$SOURCE_REF")")"

# ── Step 4: run the non-interactive build + publish ─────────────────────────
# portal-build.sh reads BUILD_JOB_ID/EVENT_BUS to emit the phase=publishing
# event between the build and publish steps (Req 5.1). Output goes to stdout
# (streamed to CloudWatch Logs by SSM) and to a local log used for result and
# failure-stage parsing.
export BUILD_JOB_ID EVENT_BUS
BUILD_LOG="/tmp/portal-build-agent-${BUILD_JOB_ID}.log"
echo "Starting portal-build.sh ${BUILD_ARGS[*]} (target ${BUILD_TARGET}, log ${BUILD_LOG})"

bash ./portal-build.sh "${BUILD_ARGS[@]}" 2>&1 | tee "$BUILD_LOG"
BUILD_EXIT_CODE=${PIPESTATUS[0]}

# ── Step 5: report the result ────────────────────────────────────────────────
if [ "$BUILD_EXIT_CODE" -eq 0 ]; then
    # Parse the machine-readable result line printed by portal-build.sh:
    #   PORTAL_BUILD_RESULT {"component_name":...,"published_version":...,"pushed_image_refs":[...]}
    RESULT_JSON=$(grep -a '^PORTAL_BUILD_RESULT ' "$BUILD_LOG" | tail -1 | sed 's/^PORTAL_BUILD_RESULT //')
    case "$RESULT_JSON" in
        \{*) : ;;  # looks like a JSON object — use it verbatim
        *)
            echo "⚠ Warning: PORTAL_BUILD_RESULT line missing or malformed; reporting empty result metadata"
            RESULT_JSON="{}"
            ;;
    esac
    emit_event "$(printf '{"build_job_id":"%s","phase":"succeeded","build_target":"%s","result":%s}' \
        "$BUILD_JOB_ID" "$BUILD_TARGET" "$RESULT_JSON")"
    echo "✅ Build_Job ${BUILD_JOB_ID} (${BUILD_TARGET}) succeeded."
    exit 0
fi

# Failure: distinguish the build stage from the publish stage (Req 5.4).
# portal-build.sh prints the "Publishing LocalServer component" step banner
# only after the build step has succeeded, so its presence in the log means
# the failure happened during publishing.
ERROR_TAIL=$(tail -n 5 "$BUILD_LOG" 2>/dev/null | head -c 512)

if grep -aq 'Publishing LocalServer component' "$BUILD_LOG"; then
    # Publish-stage failure: reconstruct which artifacts were published before
    # the failure and which were not (Req 5.4). portal-build.sh runs its
    # publish steps sequentially under `set -e`, so each step's start marker
    # proves the preceding step completed.
    PUBLISHED='' UNPUBLISHED=''
    append() {  # append "item" to the named list variable (comma-separated)
        local -n list_ref="$1"
        list_ref="${list_ref:+${list_ref},}\"$2\""
    }
    if grep -aq 'Pushing flask-app to ECR' "$BUILD_LOG"; then
        # ECR + S3 path: two image pushes, then the component version.
        if grep -aq 'Pushing react-webapp to ECR' "$BUILD_LOG"; then
            append PUBLISHED "image:dda/flask-app"
        else
            append UNPUBLISHED "image:dda/flask-app"
        fi
        if grep -aq 'Creating component version via API' "$BUILD_LOG"; then
            append PUBLISHED "image:dda/react-webapp"
        else
            append UNPUBLISHED "image:dda/react-webapp"
        fi
    fi
    if grep -aq '✓ Component published successfully' "$BUILD_LOG"; then
        append PUBLISHED "greengrass_component"
    else
        append UNPUBLISHED "greengrass_component"
    fi
    emit_failed "publishing" \
        "Publishing failed after a successful build (exit ${BUILD_EXIT_CODE}): ${ERROR_TAIL}" \
        ",\"published_artifacts\":[${PUBLISHED}],\"unpublished_artifacts\":[${UNPUBLISHED}]"
else
    emit_failed "building" \
        "Build failed (exit ${BUILD_EXIT_CODE}): ${ERROR_TAIL}"
fi

echo "✗ Build_Job ${BUILD_JOB_ID} (${BUILD_TARGET}) failed (exit ${BUILD_EXIT_CODE})."
exit "$BUILD_EXIT_CODE"
