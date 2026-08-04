#!/usr/bin/env bash
# compose_lifecycle.sh — compose lifecycle helper (edge-deploy-reliability,
# Defect E: synchronous teardown + adoption-proof Startup).
#
# Shipped into every LocalServer component artifact by build-custom.sh's
# `cp -r src/host_scripts` step. NOT security-baseline-tracked. `docker` is
# resolved via PATH, so both subcommands are pure functions of docker CLI
# output and are testable with a stubbed docker.
#
# Usage:
#   compose_lifecycle.sh wait-empty <timeout-seconds> -- <docker compose args...>
#       Poll `docker compose <args> ps -aq` every ${POLL_INTERVAL_SECONDS}s
#       until the project reports zero containers. Exit 0 when empty
#       (immediately if already empty — the fast-teardown/cold-start case);
#       exit 1 naming the surviving container IDs when the bound elapses.
#       Used by recipe Shutdown (best-effort) so Shutdown does not return
#       while a slow-dying container (~24s post-SIGKILL GPU/Triton teardown)
#       from this incarnation still exists.
#
#   compose_lifecycle.sh verify-fresh <since-epoch> -- <docker compose args...>
#       For every `docker compose <args> ps -q` container, parse
#       `docker inspect -f '{{.State.StartedAt}}'` to an epoch and require
#       it to be >= <since-epoch>. Zero containers -> exit 0. Any stale
#       container, or malformed/unparsable inspect output -> exit 1 (fail
#       closed). Used by recipe Startup (NOT best-effort) so Greengrass
#       never reports RUNNING over a previous incarnation's container.

set -u

POLL_INTERVAL_SECONDS=2

die() {
    echo "compose_lifecycle.sh: $*" >&2
    exit 1
}

usage() {
    die "usage: compose_lifecycle.sh {wait-empty <timeout-seconds> | verify-fresh <since-epoch>} -- <docker compose args...>"
}

# ISO8601 (docker inspect .State.StartedAt, e.g.
# 2025-01-02T03:04:05.123456789Z) -> unix epoch. Fractional seconds are
# stripped first: Jetson Ubuntu's `date -d` handles ISO8601 reliably only
# without them. Prints nothing (rc 1) on unparsable input — callers fail
# closed.
started_at_to_epoch() {
    iso="$1"
    stripped="$(printf '%s' "$iso" | sed -E 's/\.[0-9]+//')"
    [ -n "$stripped" ] || return 1
    epoch="$(date -d "$stripped" +%s 2>/dev/null)" || return 1
    case "$epoch" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$epoch"
}

wait_empty() {
    bound="$1"
    shift
    deadline=$(( $(date +%s) + bound ))
    while :; do
        remaining="$(docker compose "$@" ps -aq)" \
            || die "wait-empty: 'docker compose ps -aq' failed"
        if [ -z "$remaining" ]; then
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "compose_lifecycle.sh: wait-empty: project still reports containers after ${bound}s: $(printf '%s' "$remaining" | tr '\n' ' ')" >&2
            return 1
        fi
        sleep "$POLL_INTERVAL_SECONDS"
    done
}

verify_fresh() {
    since_epoch="$1"
    shift
    ids="$(docker compose "$@" ps -q)" \
        || die "verify-fresh: 'docker compose ps -q' failed"
    # Zero project containers: nothing to check (cold-start no-op).
    [ -z "$ids" ] && return 0
    while IFS= read -r id; do
        [ -z "$id" ] && continue
        started_at="$(docker inspect -f '{{.State.StartedAt}}' "$id")" \
            || die "verify-fresh: 'docker inspect' failed for container $id (fail closed)"
        epoch="$(started_at_to_epoch "$started_at")" \
            || die "verify-fresh: container $id has malformed StartedAt '${started_at}' (fail closed)"
        if [ "$epoch" -lt "$since_epoch" ]; then
            echo "compose_lifecycle.sh: verify-fresh: container $id is STALE: StartedAt $started_at (epoch $epoch) predates reference epoch $since_epoch — a previous incarnation's container" >&2
            return 1
        fi
    done <<EOF
$ids
EOF
    return 0
}

[ "$#" -ge 3 ] || usage
subcommand="$1"
numeric_arg="$2"
[ "$3" = "--" ] || usage
shift 3

case "$numeric_arg" in
    ''|*[!0-9]*) usage ;;
esac

case "$subcommand" in
    wait-empty)
        wait_empty "$numeric_arg" "$@"
        ;;
    verify-fresh)
        verify_fresh "$numeric_arg" "$@"
        ;;
    *)
        usage
        ;;
esac
