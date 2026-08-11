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
#   BUILD_TARGET   (required)  One of: JP5, JP6, JP7, AMD64, AMD64_NVIDIA
#   EVENT_BUS      (optional)  EventBridge bus name for phase events; when
#                              unset, events are skipped (standalone/debug run)
#   SOURCE_REF     (optional)  Git ref to sync the repo clone to before the
#                              build; when unset, the checked-out tree is used
#   ATTEMPT_ID     (optional)  Execution-attempt identity claimed by the
#                              dispatcher (build-fleet-execution-failures
#                              task 7.2). When set, the agent additionally
#                              runs a dispatch preflight before any costly
#                              work, emits a correlated execution-start
#                              event with disk-capacity evidence, periodic
#                              heartbeats, output-growth progress events
#                              (unique event ids, monotonic sequences, no
#                              raw environment or build output), and stamps
#                              terminal events with the completion time.
#                              When unset, behavior and event field sets
#                              are byte-compatible with the legacy agent.
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
#        JP7          → aarch64 7       (aws.edgeml.dda.LocalServer.arm64JP7)
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
        ATTEMPT_ID=*)    ATTEMPT_ID="${arg#ATTEMPT_ID=}" ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            echo "Usage: $0 [BUILD_JOB_ID=<id>] [BUILD_TARGET=<JP5|JP6|JP7|AMD64|AMD64_NVIDIA>] [EVENT_BUS=<bus>] [SOURCE_REF=<ref>] [ATTEMPT_ID=<id>]" >&2
            exit 64
            ;;
    esac
done

BUILD_JOB_ID="${BUILD_JOB_ID:-}"
BUILD_TARGET="${BUILD_TARGET:-}"
EVENT_BUS="${EVENT_BUS:-}"
SOURCE_REF="${SOURCE_REF:-}"
ATTEMPT_ID="${ATTEMPT_ID:-}"

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
    detail=$(printf '{"build_job_id":"%s","phase":"failed","build_target":"%s","error_kind":"%s","error_message":"%s"%s%s}' \
        "$BUILD_JOB_ID" "$BUILD_TARGET" "$error_kind" "$msg" "$extra" \
        "$(attempt_terminal_fields)")
    emit_event "$detail"
}

# ── Correlated attempt evidence (task 7.2) ──────────────────────────────────
# All the helpers below are exact no-ops when ATTEMPT_ID is unset, keeping
# legacy invocations and their event field sets byte-compatible.

epoch_ms() { date +%s%3N; }

# A unique event id for every emitted attempt event.
agent_event_id() {
    uuidgen 2>/dev/null || echo "${BUILD_JOB_ID}-$$-$(date +%s%N)"
}

# Additive attempt-correlation fields (leading comma) for a phase event.
attempt_event_fields() {
    [ -z "$ATTEMPT_ID" ] && return 0
    printf ',"attempt_id":"%s","event_id":"%s","observed_at":%s' \
        "$(json_escape "$ATTEMPT_ID")" "$(agent_event_id)" "$(epoch_ms)"
}

# Terminal events additionally carry the completion time (task 7.2).
attempt_terminal_fields() {
    [ -z "$ATTEMPT_ID" ] && return 0
    attempt_event_fields
    printf ',"completed_at":%s' "$(epoch_ms)"
}

# Emit one correlated activity event (execution_start / heartbeat /
# progress). Fire-and-forget with a SINGLE put-events attempt and no
# output: activity events are advisory liveness evidence — the terminal
# result and the SSM command status remain the authoritative backstops —
# and they never print the "skipping event" line legacy consumers parse.
emit_activity_event() {
    local detail="$1"
    [ -n "$EVENT_BUS" ] || return 0
    local escaped entries
    escaped=$(json_escape "$detail")
    entries=$(printf '[{"Source":"dda.portal.builds","DetailType":"BuildPhaseChange","Detail":"%s","EventBusName":"%s"}]' \
        "$escaped" "$EVENT_BUS")
    aws events put-events --entries "$entries" >/dev/null 2>&1 || true
    return 0
}

# ── Heartbeat / progress monitor (task 7.2) ─────────────────────────────────
# Runs in a background subshell while the wrapper and the protected build
# process are alive: a heartbeat every HEARTBEAT_INTERVAL_SECONDS with a
# monotonic sequence, and a progress event (kind output_growth, byte
# count) whenever the local build log grew. Heartbeats carry identities,
# sequences and byte counts ONLY — never raw environment or build output.
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-60}"
HEARTBEAT_MONITOR_PID=""

start_heartbeat_monitor() {
    [ -n "$ATTEMPT_ID" ] && [ -n "$EVENT_BUS" ] || return 0
    (
        # The monitor must not hold the build lock's descriptor.
        exec 9>&- 2>/dev/null || true
        hb_sequence=0
        progress_sequence=0
        last_log_bytes=0
        while :; do
            sleep "$HEARTBEAT_INTERVAL_SECONDS"
            hb_sequence=$((hb_sequence + 1))
            emit_activity_event "$(printf '{"build_job_id":"%s","phase":"heartbeat","build_target":"%s","attempt_id":"%s","event_id":"%s","sequence":%s,"observed_at":%s}' \
                "$BUILD_JOB_ID" "$BUILD_TARGET" \
                "$(json_escape "$ATTEMPT_ID")" "$(agent_event_id)" \
                "$hb_sequence" "$(epoch_ms)")"
            log_bytes=$(stat -c %s "$BUILD_LOG" 2>/dev/null || echo 0)
            if [ "$log_bytes" -gt "$last_log_bytes" ]; then
                progress_sequence=$((progress_sequence + 1))
                emit_activity_event "$(printf '{"build_job_id":"%s","phase":"progress","build_target":"%s","attempt_id":"%s","event_id":"%s","sequence":%s,"observed_at":%s,"progress_kind":"output_growth","log_bytes":%s}' \
                    "$BUILD_JOB_ID" "$BUILD_TARGET" \
                    "$(json_escape "$ATTEMPT_ID")" "$(agent_event_id)" \
                    "$progress_sequence" "$(epoch_ms)" "$log_bytes")"
                last_log_bytes=$log_bytes
            fi
        done
    ) &
    HEARTBEAT_MONITOR_PID=$!
}

# Stopped reliably on EVERY shell exit path via the EXIT trap below.
stop_heartbeat_monitor() {
    if [ -n "$HEARTBEAT_MONITOR_PID" ]; then
        kill "$HEARTBEAT_MONITOR_PID" 2>/dev/null || true
        wait "$HEARTBEAT_MONITOR_PID" 2>/dev/null || true
        HEARTBEAT_MONITOR_PID=""
    fi
}
trap stop_heartbeat_monitor EXIT

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
    JP7)          BUILD_ARGS=(aarch64 7) ;;
    AMD64)        BUILD_ARGS=(x86_64) ;;
    AMD64_NVIDIA) BUILD_ARGS=(x86_64_nvidia) ;;
    *)
        echo "ERROR: unsupported BUILD_TARGET '${BUILD_TARGET}' (supported: JP5, JP6, JP7, AMD64, AMD64_NVIDIA)" >&2
        emit_failed "building" "Unsupported BUILD_TARGET '${BUILD_TARGET}' (supported: JP5, JP6, JP7, AMD64, AMD64_NVIDIA)"
        exit 64
        ;;
esac

# ── Step 1.5: dispatch preflight (build-fleet-execution-failures task 7.1;
# evidence-gate rows 1 and 8, both CONFIRMED: the 2026-08-06 AMD64 dispatch
# reached a live server with zero pre-checks). Runs ONLY when the dispatcher
# claimed an execution attempt (ATTEMPT_ID set) so legacy/standalone runs
# behave byte-identically. Validates the machine-side startup contract and
# RECORDS disk capacity (row 9, task 7.5) BEFORE any costly build/publish
# work; a violation reports a bounded preflight failure (the backend
# classifies it COMMAND_PREFLIGHT_FAILED) and exits 78 without building.
BUILD_LOG="/tmp/portal-build-agent-${BUILD_JOB_ID}.log"
PREFLIGHT_DISK_JSON='{"available":false}'
PREFLIGHT_REPO_AVAIL_GB=""
PREFLIGHT_TMP_AVAIL_GB=""

record_disk_capacity() {
    # Docker layer storage on snap-docker runners (evidence row 9), and
    # the repository / /tmp volume. Evidence only: recording never fails
    # the preflight unless PREFLIGHT_MIN_DISK_GB is configured below.
    local docker_path="/var/snap/docker/common"
    [ -d "$docker_path" ] || docker_path="$REPO_DIR"
    local line
    line=$(df -k --output=size,used,avail "$docker_path" 2>/dev/null | tail -1)
    if [ -n "$line" ]; then
        # shellcheck disable=SC2086
        set -- $line
        PREFLIGHT_DISK_JSON=$(printf '{"docker_storage_path":"%s","total_gb":%s,"used_gb":%s,"available_gb":%s,"measured_at":%s,"available":true}' \
            "$(json_escape "$docker_path")" \
            "$(( ${1:-0} / 1048576 ))" "$(( ${2:-0} / 1048576 ))" \
            "$(( ${3:-0} / 1048576 ))" "$(epoch_ms)")
    fi
    PREFLIGHT_REPO_AVAIL_GB=$(avail_gb_of "$REPO_DIR")
    PREFLIGHT_TMP_AVAIL_GB=$(avail_gb_of /tmp)
    return 0
}

# Available gigabytes on the volume backing a path ('' when unmeasurable —
# identified as unavailable, never fabricated).
avail_gb_of() {
    local kb
    kb=$(df -k --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -n "$kb" ]; then
        echo $(( kb / 1048576 ))
    else
        echo ""
    fi
}

if [ -n "$ATTEMPT_ID" ]; then
    PREFLIGHT_FAILURES=""
    # Agent entry points exist, are readable, and can be invoked.
    [ -r "./portal-build.sh" ] || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} portal_build_script"
    # Required tools (common to every target — no cross-target
    # assumptions are imposed on JP5/JP6 vs AMD64, task 7.3).
    for required_tool in bash flock git aws docker python3; do
        command -v "$required_tool" >/dev/null 2>&1 \
            || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} tool_${required_tool}"
    done
    command -v gdk >/dev/null 2>&1 || [ -x "${HOME:-/home/ubuntu}/.local/bin/gdk" ] \
        || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} tool_gdk"
    # Machine architecture matches the target mapping (JP5/JP6/JP7 -> arm64,
    # AMD64/AMD64_NVIDIA -> x86_64; task 7.3 preserves the matrix).
    MACHINE_ARCH="$(uname -m)"
    case "$BUILD_TARGET" in
        JP5|JP6|JP7)
            case "$MACHINE_ARCH" in
                aarch64|arm64) : ;;
                *) PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} arch_mismatch" ;;
            esac ;;
        AMD64|AMD64_NVIDIA)
            [ "$MACHINE_ARCH" = "x86_64" ] \
                || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} arch_mismatch" ;;
    esac
    # Writable lock and log locations.
    ( : >>"$LOCK_FILE" ) 2>/dev/null \
        || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} lock_writable"
    ( : >"/tmp/.dda-preflight-$$" && rm -f "/tmp/.dda-preflight-$$" ) 2>/dev/null \
        || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} log_writable"
    # Callback region and safe AWS identity presence (no credential is
    # ever printed).
    PREFLIGHT_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
    [ -n "$PREFLIGHT_REGION" ] \
        || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} region"
    aws sts get-caller-identity >/dev/null 2>&1 \
        || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} aws_identity"
    # Source ref resolvable WITHOUT mutating the checkout.
    if [ -n "$SOURCE_REF" ]; then
        git rev-parse --verify --quiet "refs/remotes/origin/${SOURCE_REF}" >/dev/null 2>&1 \
            || git rev-parse --verify --quiet "${SOURCE_REF}^{commit}" >/dev/null 2>&1 \
            || git ls-remote --exit-code origin "$SOURCE_REF" >/dev/null 2>&1 \
            || PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} source_ref"
    fi
    # Disk-capacity recording (task 7.5, evidence row 9): evidence only,
    # unless a separately configured minimum is violated.
    record_disk_capacity
    PREFLIGHT_MIN_DISK_GB="${PREFLIGHT_MIN_DISK_GB:-}"
    if [ -n "$PREFLIGHT_MIN_DISK_GB" ]; then
        PREFLIGHT_AVAIL_GB=$(printf '%s' "$PREFLIGHT_DISK_JSON" \
            | grep -o '"available_gb":[0-9]*' | grep -o '[0-9]*' || true)
        if [ -n "$PREFLIGHT_AVAIL_GB" ] \
                && [ "$PREFLIGHT_AVAIL_GB" -lt "$PREFLIGHT_MIN_DISK_GB" ]; then
            PREFLIGHT_FAILURES="${PREFLIGHT_FAILURES} disk_minimum"
        fi
    fi

    if [ -n "$PREFLIGHT_FAILURES" ]; then
        echo "DDA_PREFLIGHT_FAILED checks:${PREFLIGHT_FAILURES}" >&2
        emit_failed "preflight" \
            "Dispatch preflight failed before any build/publish work: DDA_PREFLIGHT_FAILED${PREFLIGHT_FAILURES}"
        exit 78
    fi
    echo "✓ Dispatch preflight passed (arch ${MACHINE_ARCH}, disk ${PREFLIGHT_DISK_JSON})"

    # Correlated execution start (after preflight and lock acquisition,
    # task 7.2), carrying the preflight summary and disk evidence.
    emit_activity_event "$(printf '{"build_job_id":"%s","phase":"execution_start","build_target":"%s","attempt_id":"%s","event_id":"%s","observed_at":%s,"disk":%s,"preflight":{"passed":true,"repo_avail_gb":"%s","tmp_avail_gb":"%s"}}' \
        "$BUILD_JOB_ID" "$BUILD_TARGET" "$(json_escape "$ATTEMPT_ID")" \
        "$(agent_event_id)" "$(epoch_ms)" "$PREFLIGHT_DISK_JSON" \
        "$PREFLIGHT_REPO_AVAIL_GB" "$PREFLIGHT_TMP_AVAIL_GB")"
    start_heartbeat_monitor
fi

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
# The resolved commit SHA that will be built (build-source-selection
# Req 4.5): recorded on the phase=building event so the Build_Job is
# traceable to an exact source state even if the branch moves. Additive
# only — the existing detail fields are untouched.
SOURCE_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo '')"
emit_event "$(printf '{"build_job_id":"%s","phase":"building","build_target":"%s","source_ref":"%s","source_commit":"%s"}' \
    "$BUILD_JOB_ID" "$BUILD_TARGET" "$(json_escape "$SOURCE_REF")" "$SOURCE_COMMIT")"

# ── Step 4: run the non-interactive build + publish ─────────────────────────
# portal-build.sh reads BUILD_JOB_ID/EVENT_BUS to emit the phase=publishing
# event between the build and publish steps (Req 5.1). Output goes to stdout
# (streamed to CloudWatch Logs by SSM) and to a local log used for result and
# failure-stage parsing.
export BUILD_JOB_ID EVENT_BUS ATTEMPT_ID
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
    emit_event "$(printf '{"build_job_id":"%s","phase":"succeeded","build_target":"%s","result":%s%s}' \
        "$BUILD_JOB_ID" "$BUILD_TARGET" "$RESULT_JSON" \
        "$(attempt_terminal_fields)")"
    echo "✅ Build_Job ${BUILD_JOB_ID} (${BUILD_TARGET}) succeeded."
    exit 0
fi

# Failure: distinguish the build stage from the publish stage (Req 5.4).
# portal-build.sh prints the "Publishing LocalServer component" step banner
# only after the build step has succeeded, so its presence in the log means
# the failure happened during publishing.
#
# TAIL-PRESERVING truncation (build-fleet-execution-failures task 7.4,
# Req 2.22; evidence-gate row 9 — CONFIRMED): the former
# `tail -n 5 | head -c 512` kept the HEAD of over-length buildkit failure
# lines and dropped the trailing root cause — JP6 job bd91c5d8's durable
# error was cut mid-path at `write /var/snap/docker/common/`, hiding
# `no space left on device`. `tail -c 512` keeps the root-cause END of
# over-length content; lines already within the bound are unchanged
# (Req 3.15), and all existing bounded sizes and redaction still apply.
ERROR_TAIL=$(tail -n 5 "$BUILD_LOG" 2>/dev/null | tail -c 512)

# Optional local ENOSPC detection (task 7.4, Req 2.21): when the captured
# failure output carries disk-exhaustion evidence, report error_kind=disk
# in the terminal callback so classification honors it directly without
# pattern matching. Disk-free failures keep the existing error kinds.
BUILD_FAILURE_KIND="building"
if tail -n 200 "$BUILD_LOG" 2>/dev/null | grep -aqiE 'no space left on device|enospc'; then
    BUILD_FAILURE_KIND="disk"
fi

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
    emit_failed "$BUILD_FAILURE_KIND" \
        "Build failed (exit ${BUILD_EXIT_CODE}): ${ERROR_TAIL}"
fi

echo "✗ Build_Job ${BUILD_JOB_ID} (${BUILD_TARGET}) failed (exit ${BUILD_EXIT_CODE})."
exit "$BUILD_EXIT_CODE"
