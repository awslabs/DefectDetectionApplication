#!/bin/bash
# prune-build-server-disk.sh — pre-build disk pruning + free-space threshold
# gate for the dedicated build server (build-server-disk-pruning, design
# File 1).
#
# Invoked by scripts/portal-build-agent.sh (Step 2.5 hook) after the source
# sync, inside /var/lock/dda-build.lock — so it can never race an
# in-progress build. Prunes ONLY provably-safe state:
#
#   1. Stale locally-tagged per-version ECR image generations whose exact
#      digest is CONFIRMED present in ECR (aws ecr describe-images with the
#      local RepoDigest). Deletion is `docker rmi <repo>:<tag>` — never
#      forced (-f), never by image ID: on a multi-tagged image this only
#      removes the tag; layers stay referenced by :latest.
#   2. Dangling images (docker image prune -f, dangling ONLY — NEVER
#      `docker image prune -a`, NEVER `docker builder prune`, NEVER
#      `docker system prune`: the BuildKit cache and the :latest lineage
#      carry the ~1-2 h onnxruntime GPU compile layers).
#   3. Stale custom-build/ workspace leftovers (build-custom.sh does the
#      same `rm -rf ./custom-build` at its own start — provably safe).
#   4. Old pattern-matched /tmp build logs past the retention window,
#      ALWAYS excluding the current job's own log.
#
# Then it re-measures free space on the docker storage volume and exits 3
# when it is below BUILD_MIN_FREE_DISK_GB — the agent fails the job with
# error_kind=disk BEFORE the expensive build.
#
# FAIL OPEN, LOUDLY: on ANY aws/docker error or uncertainty the affected
# candidate is RETAINED (reason logged: NOT_IN_ECR / NO_REPODIGEST /
# ECR_UNVERIFIABLE) and the script continues. Its own bugs must never
# abort a build — hence `set -u -o pipefail` but NOT `set -e`, and
# per-step error handling throughout.
#
# Exit codes:
#   0 — pruning done (or disabled), free space at/above the threshold
#       (or enforcement disabled / unmeasurable — fail open)
#   3 — free space still below BUILD_MIN_FREE_DISK_GB after pruning
#   4 — internal unexpected failure (the agent treats this as a prune
#       malfunction: loud warning, build proceeds — fail open)
#
# Arguments (agent KEY=VALUE convention):
#   BUILD_JOB_ID=<id>   current job id (its log is excluded from pruning)
#
# Environment knobs:
#   BUILD_MIN_FREE_DISK_GB   minimum free GB required AFTER pruning on the
#                            docker storage volume (default 60; 0 disables)
#   PRUNE_LOG_RETENTION_DAYS /tmp build-log retention in days (default 7)
#   DDA_PRUNE_DISABLE        1 = log and exit 0 immediately (escape hatch)
#   PRUNE_TMP_DIR            log-scan root (default /tmp; test override)
#
# All external binaries (docker, aws, df, find, stat) are resolved from
# PATH so the test suites can stub them.

set -u -o pipefail

# ── Arguments ─────────────────────────────────────────────────────────────
BUILD_JOB_ID=""
for arg in "$@"; do
    case "$arg" in
        BUILD_JOB_ID=*) BUILD_JOB_ID="${arg#BUILD_JOB_ID=}" ;;
        *) echo "prune: ignoring unknown argument: ${arg}" ;;
    esac
done

# ── Environment knobs (all overridable, all defaulted) ────────────────────
BUILD_MIN_FREE_DISK_GB="${BUILD_MIN_FREE_DISK_GB:-60}"
PRUNE_LOG_RETENTION_DAYS="${PRUNE_LOG_RETENTION_DAYS:-7}"
DDA_PRUNE_DISABLE="${DDA_PRUNE_DISABLE:-}"
PRUNE_TMP_DIR="${PRUNE_TMP_DIR:-/tmp}"

echo "=== DDA pre-build disk prune (job ${BUILD_JOB_ID:-<none>}) ==="

if [ "$DDA_PRUNE_DISABLE" = "1" ]; then
    echo "DDA_PRUNE_DISABLE=1 — pre-build disk prune disabled; skipping"
    exit 0
fi

# ── Repo dir resolution (the script lives at <repo>/scripts/) ────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || SCRIPT_DIR=""
if [ -z "$SCRIPT_DIR" ]; then
    echo "PRUNE-INTERNAL-ERROR: cannot resolve script directory"
    exit 4
fi
REPO_DIR="$(cd "${SCRIPT_DIR}/.." 2>/dev/null && pwd)" || REPO_DIR=""
if [ -z "$REPO_DIR" ]; then
    echo "PRUNE-INTERNAL-ERROR: cannot resolve repository directory"
    exit 4
fi

# ── Step 1: docker-storage-path resolution + BEFORE measurement ──────────
# Mirrors the agent's record_disk_capacity: docker layer storage on
# snap-docker runners, else the repository volume.
free_gb_of() {
    local kb
    kb=$(df -k --output=avail "$1" 2>/dev/null | tail -1 \
        | awk '{print $NF}' | tr -dc '0-9')
    if [ -n "$kb" ]; then
        echo $(( kb / 1048576 ))
    else
        echo ""
    fi
}

DOCKER_STORAGE_PATH="/var/snap/docker/common"
[ -d "$DOCKER_STORAGE_PATH" ] || DOCKER_STORAGE_PATH="$REPO_DIR"

FREE_GB_BEFORE="$(free_gb_of "$DOCKER_STORAGE_PATH")"
echo "Free disk before pruning: ${FREE_GB_BEFORE:-unmeasurable} GB on ${DOCKER_STORAGE_PATH}"

# ── Step 2: stale locally-tagged per-version ECR generation pruning ──────
# Candidates: repository matches the ECR registry pattern for dda/* AND the
# tag is a real version (not latest, not <none> — dangling is step 3).
# Deletion is gated on FULL ECR confirmation: local RepoDigest present for
# that repo AND aws ecr describe-images succeeds for that exact digest.
echo "--- Pruning stale ECR-confirmed image generations ---"
IMAGE_ROWS="$(docker image ls --format '{{.Repository}}\t{{.Tag}}\t{{.Size}}' 2>/dev/null)"
if [ $? -ne 0 ]; then
    echo "WARNING: docker image ls failed — skipping generation pruning (fail open)"
    IMAGE_ROWS=""
fi

while IFS=$'\t' read -r img_repo img_tag img_size; do
    [ -n "$img_repo" ] || continue
    case "$img_repo" in
        [0-9]*.dkr.ecr.*.amazonaws.com/dda/*) : ;;
        *) continue ;;
    esac
    case "$img_tag" in
        latest|"<none>"|"") continue ;;
    esac
    ref="${img_repo}:${img_tag}"
    ecr_repo_name="${img_repo#*.amazonaws.com/}"   # e.g. dda/flask-app

    # Local RepoDigest for THIS repository — proof a push of these exact
    # bytes completed. Absent/unreadable => retain.
    digest_lines="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" 2>/dev/null)"
    if [ $? -ne 0 ]; then
        echo "RETAINED image ${ref} reason=NO_REPODIGEST (docker image inspect failed)"
        continue
    fi
    local_digest=""
    while IFS= read -r digest_line; do
        case "$digest_line" in
            "${img_repo}@sha256:"*) local_digest="${digest_line#*@}"; break ;;
        esac
    done <<EOF
$digest_lines
EOF
    if [ -z "$local_digest" ]; then
        echo "RETAINED image ${ref} reason=NO_REPODIGEST"
        continue
    fi

    # ECR confirmation: the exact digest is retrievable from ECR right now.
    aws_stderr="$(aws ecr describe-images \
        --repository-name "$ecr_repo_name" \
        --image-ids imageDigest="$local_digest" 2>&1 >/dev/null)"
    aws_rc=$?
    if [ "$aws_rc" -ne 0 ]; then
        if printf '%s' "$aws_stderr" | grep -qiE 'ImageNotFound|RepositoryNotFound'; then
            echo "RETAINED image ${ref} reason=NOT_IN_ECR"
        else
            echo "RETAINED image ${ref} reason=ECR_UNVERIFIABLE (aws exit ${aws_rc})"
        fi
        continue
    fi

    # FULL confirmation — untag by <repo>:<tag> (tag-only removal; the
    # layers stay referenced by any other tag, e.g. :latest).
    if docker rmi "$ref" >/dev/null 2>&1; then
        echo "PRUNED image ${ref} (${img_size}, digest ${local_digest})"
    else
        echo "WARNING: docker rmi ${ref} failed — image retained (fail open)"
    fi
done <<EOF
$IMAGE_ROWS
EOF

# ── Step 3: dangling images (dangling ONLY — see header) ─────────────────
echo "--- Pruning dangling images ---"
DANGLING_REPORT="$(docker image prune -f 2>&1)"
if [ $? -eq 0 ]; then
    echo "$DANGLING_REPORT"
else
    echo "WARNING: dangling-image prune failed — continuing (fail open): ${DANGLING_REPORT}"
fi

# ── Step 4: stale custom-build/ workspace leftovers ──────────────────────
# build-custom.sh does the same removal at its own start; doing it here
# makes the threshold measurement honest.
echo "--- Pruning workspace leftovers ---"
WORKSPACE_DIR="${REPO_DIR}/custom-build"
if [ -d "$WORKSPACE_DIR" ]; then
    ws_size="$(du -sh "$WORKSPACE_DIR" 2>/dev/null | awk '{print $1}')"
    echo "Removing stale workspace ${WORKSPACE_DIR}/ (size ${ws_size:-unknown})"
    if rm -rf "$WORKSPACE_DIR" 2>/dev/null; then
        echo "PRUNED workspace ${WORKSPACE_DIR}/ (${ws_size:-unknown})"
    else
        echo "WARNING: failed to remove ${WORKSPACE_DIR}/ — continuing (fail open)"
    fi
else
    echo "No workspace leftovers at ${WORKSPACE_DIR}/ — skipping"
fi

# ── Step 5: old /tmp build logs (pattern-scoped, age-gated) ───────────────
# Exactly the four timestamped/job-suffixed build-log patterns; the current
# job's own log is ALWAYS excluded, whatever its age.
echo "--- Pruning build logs older than ${PRUNE_LOG_RETENTION_DAYS} days in ${PRUNE_TMP_DIR} ---"
CURRENT_JOB_LOG_NAME=""
if [ -n "$BUILD_JOB_ID" ]; then
    CURRENT_JOB_LOG_NAME="portal-build-agent-${BUILD_JOB_ID}.log"
fi
for log_pattern in 'gdk-build-*.log' 'gdk-publish-*.log' \
        'portal-build-agent-*.log' 'inference-uploader-build-*.log'; do
    while IFS= read -r log_file; do
        [ -n "$log_file" ] || continue
        log_base="$(basename "$log_file")"
        if [ -n "$CURRENT_JOB_LOG_NAME" ] \
                && [ "$log_base" = "$CURRENT_JOB_LOG_NAME" ]; then
            echo "RETAINED log ${log_file} (current job's log — never pruned)"
            continue
        fi
        log_size="$(stat -c '%s' "$log_file" 2>/dev/null)"
        if rm -f "$log_file" 2>/dev/null; then
            echo "PRUNED log ${log_file} (${log_size:-unknown} bytes)"
        else
            echo "WARNING: failed to delete ${log_file} — continuing (fail open)"
        fi
    done <<EOF
$(find "$PRUNE_TMP_DIR" -maxdepth 1 -name "$log_pattern" -mtime +"$PRUNE_LOG_RETENTION_DAYS" 2>/dev/null)
EOF
done

# ── Step 6: AFTER measurement + threshold gate ────────────────────────────
FREE_GB_AFTER="$(free_gb_of "$DOCKER_STORAGE_PATH")"
echo "Free disk after pruning: ${FREE_GB_AFTER:-unmeasurable} GB on ${DOCKER_STORAGE_PATH}"
if [ -n "$FREE_GB_BEFORE" ] && [ -n "$FREE_GB_AFTER" ]; then
    echo "Total freed: $(( FREE_GB_AFTER - FREE_GB_BEFORE )) GB"
else
    echo "Total freed: unmeasurable (before=${FREE_GB_BEFORE:-?} after=${FREE_GB_AFTER:-?})"
fi

case "$BUILD_MIN_FREE_DISK_GB" in
    ''|*[!0-9]*)
        echo "WARNING: BUILD_MIN_FREE_DISK_GB='${BUILD_MIN_FREE_DISK_GB}' is not a number — enforcement skipped (fail open)"
        exit 0
        ;;
esac
if [ "$BUILD_MIN_FREE_DISK_GB" -gt 0 ]; then
    if [ -z "$FREE_GB_AFTER" ]; then
        echo "WARNING: free space unmeasurable — cannot enforce BUILD_MIN_FREE_DISK_GB=${BUILD_MIN_FREE_DISK_GB}; proceeding (fail open)"
    elif [ "$FREE_GB_AFTER" -lt "$BUILD_MIN_FREE_DISK_GB" ]; then
        echo "PRUNE-DISK-INSUFFICIENT free=${FREE_GB_AFTER}GB required=${BUILD_MIN_FREE_DISK_GB}GB"
        exit 3
    fi
fi
exit 0
