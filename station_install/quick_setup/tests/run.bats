#!/usr/bin/env bats
#
# Shell tests for station_install/quick_setup/run.sh (the bundle orchestrator).
#
# Feature: station-quick-setup, Task 7.6
#   - Log creation and step banners (Requirements 7.3, 7.4).
#   - Fail-fast ordering and success output (Requirements 7.6, 7.7).
#   - Report retry count (Requirement 6.8).
#   - Credential cleanup and log redaction with planted fake secrets
#     (Requirements 5.6, 5.8).
#   - Thing-group-join failure treated as a step failure (Requirement 4.7).
#
# Strategy: run.sh resolves its provisioning script relative to its own
# location (STATION_DIR=$(dirname script_dir)) and shells out to a small set of
# external commands (curl/wget for the portal, setup_station.sh for
# provisioning, aws for group verification, systemctl for the service check).
# We therefore:
#   1. Build a throwaway "bundle" directory laid out exactly like the real one
#      ($BUNDLE/quick_setup/run.sh + $BUNDLE/setup_station.sh) with a STUB
#      setup_station.sh whose completion summary and credential-leak behaviour
#      are controlled by env vars.
#   2. Prepend a directory of stub executables (curl, aws, systemctl, sleep) to
#      PATH so every side effect is deterministic and offline. The curl stub
#      implements the exact response contract run.sh's http_post relies on
#      (write body to the "-o" file, print the HTTP status to stdout) and routes
#      by URL so /credentials and /status can be shaped independently, recording
#      every call so retry counts are observable.
# The sleep stub is a no-op so the 3-attempt status retry (with its 3s backoff)
# runs instantly.

setup() {
    STUB="$(mktemp -d)"
    BUNDLE="$(mktemp -d)"

    RUN_SH_SRC="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)/run.sh"
    mkdir -p "${BUNDLE}/quick_setup"
    cp "$RUN_SH_SRC" "${BUNDLE}/quick_setup/run.sh"
    RUN="${BUNDLE}/quick_setup/run.sh"

    CRED_MARKER="${STUB}/cred_calls"
    STATUS_MARKER="${STUB}/status_calls"
    OTHER_MARKER="${STUB}/other_calls"
    SS_ENV_FILE="${STUB}/setup_station_env"

    ORIG_PATH="$PATH"
    _write_stubs
    PATH="${STUB}:${PATH}"
    export PATH

    # --- per-registration parameters (as bootstrap.sh would export them) -----
    export DDA_REGISTRATION_ID="reg-123"
    export DDA_DEVICE_NAME="station-42"
    export DDA_THING_GROUP="Line3_Group"
    export DDA_AWS_REGION="us-east-1"
    export DDA_QS_URL="https://example.test/quick-setup"
    export DDA_SETUP_TOKEN="dqs1.reg-123.tokensecret"

    # --- planted fake secrets (Req 5.6, 5.8) --------------------------------
    export FAKE_KEY="AKIAFAKE00000000TEST"
    export FAKE_SECRET="FAKESECRETVALUE1234567890abcdef"
    export FAKE_SESSION="FAKESESSIONTOKEN0987654321zyxwv"
    export FAKE_REPORT="FAKEREPORTSECRETuvwxyz9876"

    # --- default stub behaviours (overridden per-test) ----------------------
    export STUB_CRED_STATUS="200"
    export STUB_CRED_BODY="{\"credentials\": {\"access_key_id\":\"${FAKE_KEY}\",\"secret_access_key\":\"${FAKE_SECRET}\",\"session_token\":\"${FAKE_SESSION}\"}, \"report_secret\":\"${FAKE_REPORT}\", \"aws_region\":\"us-east-1\"}"
    export STUB_STATUS_STATUS="200"
    export STUB_SS_OUTPUT="Setup completed successfully!"
    export STUB_AWS_GROUPS="Line3_Group"
    export STUB_AWS_FAIL="0"
    export STUB_SYSTEMCTL_ACTIVE="1"
}

teardown() {
    [ -n "${STUB:-}" ] && rm -rf "$STUB"
    [ -n "${BUNDLE:-}" ] && rm -rf "$BUNDLE"
}

# --- stub factories ----------------------------------------------------------

_write_stubs() {
    # curl: implements http_post's contract and routes by URL (last argument).
    cat > "${STUB}/curl" <<EOF
#!/usr/bin/env bash
out=""
prev=""
url=""
for a in "\$@"; do
    if [ "\$prev" = "-o" ]; then out="\$a"; fi
    prev="\$a"
    url="\$a"
done
case "\$url" in
    *"/credentials"*)
        echo "\$url" >> "${CRED_MARKER}"
        [ -n "\$out" ] && printf '%s' "\${STUB_CRED_BODY:-}" > "\$out"
        printf '%s' "\${STUB_CRED_STATUS:-200}"
        ;;
    *"/status"*)
        echo "\$url" >> "${STATUS_MARKER}"
        [ -n "\$out" ] && : > "\$out"
        printf '%s' "\${STUB_STATUS_STATUS:-200}"
        ;;
    *)
        echo "\$url" >> "${OTHER_MARKER}"
        [ -n "\$out" ] && : > "\$out"
        printf '%s' "200"
        ;;
esac
exit 0
EOF
    chmod +x "${STUB}/curl"

    # aws: only iot list-thing-groups-for-thing is exercised by run.sh.
    cat > "${STUB}/aws" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "iot" ] && [ "$2" = "list-thing-groups-for-thing" ]; then
    [ "${STUB_AWS_FAIL:-0}" = "1" ] && exit 1
    printf '{"thingGroups": ['
    first=1
    for g in ${STUB_AWS_GROUPS:-}; do
        [ "$first" -eq 0 ] && printf ','
        printf '{"groupName": "%s"}' "$g"
        first=0
    done
    printf ']}\n'
    exit 0
fi
exit 0
EOF
    chmod +x "${STUB}/aws"

    # systemctl: emulate `is-active [--quiet] greengrass`.
    cat > "${STUB}/systemctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "is-active" ]; then
    if [ "${STUB_SYSTEMCTL_ACTIVE:-1}" = "1" ]; then
        case "$*" in *--quiet*) exit 0 ;; esac
        echo "active"; exit 0
    else
        case "$*" in *--quiet*) exit 3 ;; esac
        echo "inactive"; exit 3
    fi
fi
exit 0
EOF
    chmod +x "${STUB}/systemctl"

    # sleep: no-op so the status-report backoff does not slow the tests.
    cat > "${STUB}/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "${STUB}/sleep"

    # setup_station.sh stub inside the bundle: records the credentials it
    # receives via the environment (proving in-memory-only passing), emulates a
    # provisioning tool that echoes the credentials to stdout (so redaction can
    # be verified), then prints the controllable completion summary.
    cat > "${BUNDLE}/setup_station.sh" <<EOF
#!/usr/bin/env bash
{
    echo "AWS_ACCESS_KEY_ID=\${AWS_ACCESS_KEY_ID:-}"
    echo "AWS_SECRET_ACCESS_KEY=\${AWS_SECRET_ACCESS_KEY:-}"
    echo "AWS_SESSION_TOKEN=\${AWS_SESSION_TOKEN:-}"
    echo "DDA_THING_GROUP=\${DDA_THING_GROUP:-}"
    echo "ARGS=\$*"
} > "${SS_ENV_FILE}"
echo "provisioning with key \${AWS_ACCESS_KEY_ID} secret \${AWS_SECRET_ACCESS_KEY} token \${AWS_SESSION_TOKEN}"
printf '%s\n' "\${STUB_SS_OUTPUT}"
exit 0
EOF
    chmod +x "${BUNDLE}/setup_station.sh"
}

# --- helpers -----------------------------------------------------------------

# Extract the announced installation-log path from the captured output.
_log_path() {
    printf '%s\n' "$output" \
        | grep -oE '/(var/log|tmp)/dda-quick-setup-[0-9-]+\.log' \
        | head -1
}

# ============================================================================
# Log creation and step banners (Req 7.3, 7.4)
# ============================================================================

@test "log: creates and announces the installation log file" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Installation log:"* ]]
    local log
    log="$(_log_path)"
    [ -n "$log" ]
    [ -f "$log" ]
}

@test "banners: each provisioning step prints its name (Req 7.4)" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    [[ "$output" == *"▶ STEP: Exchange provisioning credentials"* ]]
    [[ "$output" == *"▶ STEP: Provision station"* ]]
    [[ "$output" == *"▶ STEP: Verify Device_Group membership"* ]]
    [[ "$output" == *"▶ STEP: Verify Greengrass service"* ]]
    [[ "$output" == *"▶ STEP: Report completion to the portal"* ]]
}

# ============================================================================
# Success output (Req 7.6)
# ============================================================================

@test "success: prints device name, group, region and log path, exits 0 (Req 7.6)" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    [[ "$output" == *"completed successfully"* ]]
    [[ "$output" == *"station-42"* ]]
    [[ "$output" == *"Line3_Group"* ]]
    [[ "$output" == *"us-east-1"* ]]
    [[ "$output" == *"Installation log:"* ]]
}

# ============================================================================
# Fail-fast ordering (Req 7.7)
# ============================================================================

@test "fail-fast: a failed provisioning step stops before later steps (Req 7.7)" {
    export STUB_SS_OUTPUT=$'\xe2\x9d\x8c apt dependency install failed\nSetup completed with ERRORS'

    run bash "$RUN"

    [ "$status" -ne 0 ]
    [[ "$output" == *"Provisioning failed at step: Provision station"* ]]
    [[ "$output" == *"Installation log:"* ]]
    # Fail-fast: the steps AFTER the failed one must never begin.
    [[ "$output" != *"▶ STEP: Verify Device_Group membership"* ]]
    [[ "$output" != *"▶ STEP: Verify Greengrass service"* ]]
}

@test "fail-fast: credential-exchange failure stops before provisioning" {
    export STUB_CRED_STATUS="500"
    export STUB_CRED_BODY='{"error": "internal"}'

    run bash "$RUN"

    [ "$status" -ne 0 ]
    [[ "$output" == *"Provisioning failed at step: Exchange provisioning credentials"* ]]
    [[ "$output" != *"▶ STEP: Provision station"* ]]
    # No provisioning was attempted.
    [ ! -f "$SS_ENV_FILE" ]
}

@test "token: invalid_token from /credentials prints regenerate instruction, exits non-zero" {
    export STUB_CRED_STATUS="403"
    export STUB_CRED_BODY='{"error": "invalid_token", "message": "nope"}'

    run bash "$RUN"

    [ "$status" -ne 0 ]
    [[ "$output" == *"Generate a new setup command from the Edge CV Portal"* ]]
    [[ "$output" != *"▶ STEP: Provision station"* ]]
}

# ============================================================================
# Report retry count (Req 6.8)
# ============================================================================

@test "report: status delivery is retried up to 3 attempts then reported undeliverable (Req 6.8)" {
    export STUB_STATUS_STATUS="500"

    run bash "$RUN"

    # Provisioning itself succeeded, so the run still exits 0; only the report
    # could not be delivered.
    [ "$status" -eq 0 ]
    [[ "$output" == *"could not be reported to the Edge CV Portal after 3 attempts"* ]]
    [[ "$output" == *"provisioning outcome on this station is unchanged"* ]]
    # Exactly 3 POST /status attempts were made.
    [ -f "$STATUS_MARKER" ]
    [ "$(wc -l < "$STATUS_MARKER")" -eq 3 ]
}

@test "report: a 200 status response is not retried" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    [[ "$output" == *"Reported setup status 'completed'"* ]]
    [ "$(wc -l < "$STATUS_MARKER")" -eq 1 ]
}

# ============================================================================
# Credential cleanup and log redaction (Req 5.6, 5.8)
# ============================================================================

@test "redaction: planted secrets never appear in stdout (Req 5.8)" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    [[ "$output" != *"$FAKE_KEY"* ]]
    [[ "$output" != *"$FAKE_SECRET"* ]]
    [[ "$output" != *"$FAKE_SESSION"* ]]
    [[ "$output" != *"$FAKE_REPORT"* ]]
    [[ "$output" != *"$DDA_SETUP_TOKEN"* ]]
    [[ "$output" == *"redacted from output and log"* ]]
}

@test "redaction: planted secrets are redacted from the installation log (Req 5.8)" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    local log
    log="$(_log_path)"
    [ -f "$log" ]
    # The provisioning tool echoed the credentials; the log must not contain them.
    ! grep -qF "$FAKE_KEY" "$log"
    ! grep -qF "$FAKE_SECRET" "$log"
    ! grep -qF "$FAKE_SESSION" "$log"
    # The redaction placeholder is present where the leak was scrubbed.
    grep -qF "***REDACTED***" "$log"
}

@test "cleanup: credentials are passed in-memory via env, not written to a persistent file (Req 5.6)" {
    run bash "$RUN"

    [ "$status" -eq 0 ]
    # setup_station.sh received the credentials through the environment.
    [ -f "$SS_ENV_FILE" ]
    grep -qF "AWS_ACCESS_KEY_ID=${FAKE_KEY}" "$SS_ENV_FILE"
    grep -qF "AWS_SECRET_ACCESS_KEY=${FAKE_SECRET}" "$SS_ENV_FILE"
    grep -qF "AWS_SESSION_TOKEN=${FAKE_SESSION}" "$SS_ENV_FILE"
    # The Device_Group was forwarded so the core device joins the right group.
    grep -qF "DDA_THING_GROUP=${DDA_THING_GROUP}" "$SS_ENV_FILE"
    # No leftover file under the bundle workdir holds the secret material.
    ! grep -rqF "$FAKE_SECRET" "$BUNDLE"
    ! grep -rqF "$FAKE_SESSION" "$BUNDLE"
}

# ============================================================================
# Thing-group-join failure treated as a step failure (Req 4.7)
# ============================================================================

@test "group: missing Device_Group membership fails the run (Req 4.7)" {
    export STUB_AWS_GROUPS="Some_Other_Group"

    run bash "$RUN"

    [ "$status" -ne 0 ]
    [[ "$output" == *"Provisioning failed at step: Verify Device_Group membership"* ]]
    [[ "$output" == *"is not a member of Device_Group 'Line3_Group'"* ]]
    # Fail-fast: the Greengrass service check is not reached.
    [[ "$output" != *"▶ STEP: Verify Greengrass service"* ]]
}

@test "group: an inconclusive membership query does not fail the run" {
    # A permission/API error querying group membership must NOT be treated as a
    # join failure (the scoped credentials may lack ListThingGroupsForThing).
    export STUB_AWS_FAIL="1"

    run bash "$RUN"

    [ "$status" -eq 0 ]
    [[ "$output" == *"relying on the provisioning result"* ]]
    [[ "$output" == *"completed successfully"* ]]
}
