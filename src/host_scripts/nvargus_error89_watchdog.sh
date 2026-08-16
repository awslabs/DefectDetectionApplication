#!/bin/bash
#
# nvargus Error(89) degraded-state watchdog (Mitigation 3)
#
# Spec: .kiro/specs/csi-nvargus-optional (design Files 4-6, Decision 2).
# Installed on ALL Jetson targets by install_nvidia_csi_service.sh and driven
# by nvargus-error89-watchdog.timer -> nvargus-error89-watchdog.service
# (oneshot, ~1 minute cadence).
#
# Each run scans the kernel journal INCREMENTALLY (journalctl cursor file:
# each line is counted exactly once across scans; the first run seeds the
# cursor). When the new-since-last-scan window contains >= SIG_THRESHOLD
# lines matching the NVRM signature (osCreateOsDescriptorFromFileHandle ...
# Error (89)) AND the dma-attachment signature (Can't map dma attachment)
# is present in the same window AND nvargus-daemon is active AND at least
# RESTART_MIN_INTERVAL seconds have elapsed since the last automatic
# restart, the watchdog restarts nvargus-daemon — the action that clears
# the device-wide CUDA-context-creation failure instantly (requirement
# 2.9). Restarts are rate-limited (2.10) and escalate to a persistent
# visible error when they recur (>= ESCALATION_COUNT automatic restarts
# within ESCALATION_WINDOW seconds -> restarts stop; manual intervention
# required). Every action — restart, suppression, escalation — is logged
# at warning-or-higher with the signature counts (2.11). A signature-free
# journal produces ZERO actions and ZERO log lines (requirement 3.8: a
# healthy device is untouched, no journal spam).
#
# State files (simple text, corruption-tolerant — unparseable content is
# treated as empty/zero) under $STATE_DIR:
#   cursor              journalctl --cursor-file state (managed by journalctl)
#   last_restart_epoch  epoch seconds of the last automatic restart
#   restart_history     one epoch per line, one per automatic restart
#
# All constants below are env-overridable (VAR="${VAR:-default}") so the
# host-side behavioral tests can drive the script with stub journalctl /
# systemctl / logger binaries and a temp STATE_DIR.

set -u

# --- Constants (all env-overridable for tests: design Files 4-6) ------------
SIG_NVRM="${SIG_NVRM:-osCreateOsDescriptorFromFileHandle.*Error (89)}"
_DEFAULT_SIG_DMA="Can't map dma attachment"
SIG_DMA="${SIG_DMA:-$_DEFAULT_SIG_DMA}"
SIG_THRESHOLD="${SIG_THRESHOLD:-3}"            # new NVRM lines per scan to trigger
RESTART_MIN_INTERVAL="${RESTART_MIN_INTERVAL:-600}"   # seconds between automatic restarts (2.10)
ESCALATION_WINDOW="${ESCALATION_WINDOW:-3600}" # if >= ESCALATION_COUNT restarts in this window,
ESCALATION_COUNT="${ESCALATION_COUNT:-3}"      #   suppress + log persistent visible error
STATE_DIR="${STATE_DIR:-/var/lib/dda/nvargus-watchdog}"   # cursor + restart history
LOG_TAG="${LOG_TAG:-nvargus-error89-watchdog}"

CURSOR_FILE="$STATE_DIR/cursor"
LAST_RESTART_FILE="$STATE_DIR/last_restart_epoch"
RESTART_HISTORY_FILE="$STATE_DIR/restart_history"

# Current epoch; WATCHDOG_NOW override keeps the time-window logic testable.
_now() {
    echo "${WATCHDOG_NOW:-$(date +%s)}"
}

# Last automatic-restart epoch; corruption-tolerant (non-numeric -> 0).
_read_last_restart_epoch() {
    local value=""
    if [ -f "$LAST_RESTART_FILE" ]; then
        value="$(head -n 1 "$LAST_RESTART_FILE" 2>/dev/null || true)"
    fi
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "$value"
    else
        echo 0
    fi
}

# Number of automatic restarts recorded within ESCALATION_WINDOW seconds of
# $1 (now). Non-numeric and future-dated history lines are ignored
# (corruption tolerance).
_recent_restart_count() {
    local now="$1" count=0 line
    if [ ! -f "$RESTART_HISTORY_FILE" ]; then
        echo 0
        return 0
    fi
    while IFS= read -r line; do
        [[ "$line" =~ ^[0-9]+$ ]] || continue
        [ "$line" -le "$now" ] || continue
        if [ $((now - line)) -le "$ESCALATION_WINDOW" ]; then
            count=$((count + 1))
        fi
    done < "$RESTART_HISTORY_FILE"
    echo "$count"
}

# decide_action: the count/decide logic, sourceable in isolation for tests.
# Input:  the NEW (since last cursor) kernel journal text on stdin.
# Output: one line — "<decision> <nvrm_count> <dma_count>", where decision is
#   escalated    >= ESCALATION_COUNT restarts within ESCALATION_WINDOW —
#                automatic restarts suppressed, persistent error every scan
#   no-trigger   signature-free or below-threshold window (incl. near-misses
#                and one-signature-only streams) — take no action, stay silent
#   inactive     trigger threshold met but nvargus-daemon is not active —
#                nothing to restart (a stopped daemon holds no poisoned state)
#   rate-limited trigger threshold met inside RESTART_MIN_INTERVAL of the
#                last automatic restart — suppress
#   restart      trigger threshold met, daemon active, interval elapsed —
#                restart nvargus-daemon
# Performs NO state changes: the only external probe is the read-only
# `systemctl is-active` guard, and it is only reached when the threshold is
# met (a signature-free stream probes nothing).
decide_action() {
    local text nvrm_count dma_count now last
    text="$(cat)"
    nvrm_count="$(printf '%s\n' "$text" | grep -c -e "$SIG_NVRM" || true)"
    dma_count="$(printf '%s\n' "$text" | grep -c -e "$SIG_DMA" || true)"
    now="$(_now)"

    if [ "$(_recent_restart_count "$now")" -ge "$ESCALATION_COUNT" ]; then
        echo "escalated $nvrm_count $dma_count"
        return 0
    fi
    if [ "$nvrm_count" -lt "$SIG_THRESHOLD" ] || [ "$dma_count" -eq 0 ]; then
        echo "no-trigger $nvrm_count $dma_count"
        return 0
    fi
    if ! systemctl is-active --quiet nvargus-daemon; then
        echo "inactive $nvrm_count $dma_count"
        return 0
    fi
    last="$(_read_last_restart_epoch)"
    if [ "$last" -gt 0 ] && [ $((now - last)) -lt "$RESTART_MIN_INTERVAL" ]; then
        echo "rate-limited $nvrm_count $dma_count"
        return 0
    fi
    echo "restart $nvrm_count $dma_count"
}

# Record an automatic restart at epoch $1: last-restart marker plus history
# (history pruned to the escalation window to stay small).
_record_restart() {
    local now="$1" line kept=""
    echo "$now" > "$LAST_RESTART_FILE"
    if [ -f "$RESTART_HISTORY_FILE" ]; then
        while IFS= read -r line; do
            [[ "$line" =~ ^[0-9]+$ ]] || continue
            [ "$line" -le "$now" ] || continue
            if [ $((now - line)) -le "$ESCALATION_WINDOW" ]; then
                kept="${kept}${line}"$'\n'
            fi
        done < "$RESTART_HISTORY_FILE"
    fi
    printf '%s%s\n' "$kept" "$now" > "$RESTART_HISTORY_FILE"
}

# act_on_decision <decision> <nvrm_count> <dma_count>: perform the action and
# the loud logging the decision requires. A no-trigger (healthy /
# below-threshold) scan logs NOTHING (3.8).
act_on_decision() {
    local decision="$1" nvrm_count="$2" dma_count="$3" now
    case "$decision" in
        no-trigger)
            # Healthy / below-threshold stream: exit silently — zero
            # actions, zero journal spam (requirement 3.8).
            :
            ;;
        escalated)
            logger -t "$LOG_TAG" -p daemon.err \
                "hard driver fault suspected — ${ESCALATION_COUNT}+ automatic nvargus-daemon restarts within ${ESCALATION_WINDOW}s; automatic restarts suppressed; manual intervention required (this scan: ${nvrm_count} new Error(89) lines, ${dma_count} dma-attachment lines)"
            ;;
        inactive)
            logger -t "$LOG_TAG" -p daemon.warning \
                "degraded-state signature detected (${nvrm_count} new Error(89) lines, ${dma_count} dma-attachment lines) but nvargus-daemon is not active — no restart attempted"
            ;;
        rate-limited)
            logger -t "$LOG_TAG" -p daemon.warning \
                "restart suppressed (rate-limit): ${nvrm_count} new Error(89) lines, ${dma_count} dma-attachment lines within ${RESTART_MIN_INTERVAL}s of the last automatic restart — no restart performed"
            ;;
        restart)
            now="$(_now)"
            logger -t "$LOG_TAG" -p daemon.err \
                "detected degraded-state signature: ${nvrm_count} new Error(89) lines, ${dma_count} dma-attachment lines — restarting nvargus-daemon"
            systemctl restart nvargus-daemon
            _record_restart "$now"
            ;;
    esac
    return 0
}

main() {
    mkdir -p "$STATE_DIR"
    local new_text decision_line decision nvrm_count dma_count
    # Incremental kernel-journal scan: journalctl maintains the cursor file,
    # so each line is seen exactly once across scans (the first run seeds
    # the cursor).
    new_text="$(journalctl -k --cursor-file "$CURSOR_FILE" 2>/dev/null || true)"
    decision_line="$(printf '%s\n' "$new_text" | decide_action)"
    read -r decision nvrm_count dma_count <<< "$decision_line"
    act_on_decision "$decision" "$nvrm_count" "$dma_count"
    return 0
}

# Sourceable for tests (drive decide_action / act_on_decision directly);
# executes the full scan only when run as a script.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
