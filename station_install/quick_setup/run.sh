#!/bin/bash
#
# Station Quick Setup bundle orchestrator.
#
# Invoked by bootstrap.sh (via `exec bash run.sh`) after the bundle has been
# downloaded, checksum-verified, and extracted. bootstrap.sh exports the
# per-registration parameters as environment variables:
#   DDA_REGISTRATION_ID  the portal Device_Registration id (status reporting)
#   DDA_DEVICE_NAME      the IoT Thing / Greengrass core device name
#   DDA_THING_GROUP      the IoT Thing Group the core device joins
#   DDA_AWS_REGION       the Use_Case AWS region
#   DDA_QS_URL           this deployment's Quick_Setup_Endpoint base URL
#   DDA_SETUP_TOKEN      the single-use Setup_Token
#
# Responsibilities (see station-quick-setup design section 7 and Requirements
# 4.6, 4.7, 5.6, 5.8, 6.5, 6.8, 7.3, 7.4, 7.6, 7.7, 7.8):
#   - Create and announce the installation log; print step banners; fail fast.
#   - Exchange the Setup_Token for scoped Provisioning_Credentials, holding them
#     in memory ONLY (never written to a file) and redacting them from both
#     stdout and the log.
#   - Invoke setup_station.sh (the single source of provisioning truth) with the
#     Device_Group and the env credentials, so the core device is tagged
#     dda-portal:managed=true and appears in the portal device listing.
#   - Verify Device_Group membership and the Greengrass system service.
#   - Report completion/failure to the portal (up to 3 attempts).
#
# Note on step ordering: setup_station.sh performs ALL credential-free
# installation (apt packages, Python, Docker, GStreamer, Greengrass download)
# before its single credentialed provisioning sub-step, so credential-free work
# runs first within it. Credentials are exchanged just before invoking it (a
# single invocation) to keep setup_station.sh the sole provisioning source
# rather than splitting or re-running it.

set -u

# --- resolve paths -----------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
STATION_DIR="$(dirname "$SCRIPT_DIR")"           # bundle dir containing setup_station.sh
SETUP_STATION="${STATION_DIR}/setup_station.sh"

# Source the DDA Target_Architecture detection helper (device-arch-compatibility
# Req 1). It exposes the pure `detect_target_architecture` function and makes no
# system changes; a missing file simply leaves the architecture undetermined.
DETECT_ARCH_SH="${SCRIPT_DIR}/detect_arch.sh"
# shellcheck source=/dev/null
[ -f "$DETECT_ARCH_SH" ] && . "$DETECT_ARCH_SH"

# --- parameters from bootstrap (env) -----------------------------------------

REGISTRATION_ID="${DDA_REGISTRATION_ID:-}"
DEVICE_NAME="${DDA_DEVICE_NAME:-}"
THING_GROUP="${DDA_THING_GROUP:-}"
AWS_REGION_PARAM="${DDA_AWS_REGION:-}"
QS_URL="${DDA_QS_URL:-}"
QS_URL="${QS_URL%/}"                              # strip any trailing slash
SETUP_TOKEN="${DDA_SETUP_TOKEN:-}"

# Secrets held in memory only (never written to a file, Req 5.6).
REPORT_SECRET=""
ERROR_SUMMARY=""
STATUS_REPORTED=0
CURRENT_STEP="startup"
TMP_FILES=()

# The detected DDA Target_Architecture (device-arch-compatibility Req 1);
# empty until the detection step runs and undetermined architectures stay
# empty so they are simply not reported (Req 1.4).
DETECTED_ARCH=""

# --- installation log (Req 7.3) ----------------------------------------------

TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="/var/log/dda-quick-setup-${TS}.log"
if ! ( umask 077; : > "$LOG_FILE" ) 2>/dev/null; then
    LOG_FILE="/tmp/dda-quick-setup-${TS}.log"
    ( umask 077; : > "$LOG_FILE" ) 2>/dev/null || LOG_FILE="/dev/null"
fi

log()    { printf '%s\n' "$*" | tee -a "$LOG_FILE"; }
logerr() { printf '%s\n' "$*" | tee -a "$LOG_FILE" >&2; }

banner() {
    CURRENT_STEP="$1"
    log ""
    log "=================================================="
    log "▶ STEP: $1"
    log "=================================================="
}

# --- secret redaction (Req 5.8) ----------------------------------------------
# Build a sed program that replaces the literal secret values with a placeholder
# so they never reach stdout or the log, even when a downstream tool (e.g. the
# Greengrass provisioner invocation in setup_station.sh) echoes them.

REDACT_SED=""
add_redact() {
    local v="$1" e
    [ -n "$v" ] || return 0
    e=$(printf '%s' "$v" | sed 's/[][\.*^$/]/\\&/g')
    REDACT_SED="${REDACT_SED}s|${e}|***REDACTED***|g;"
}
redact() {
    if [ -n "$REDACT_SED" ]; then sed "$REDACT_SED"; else cat; fi
}

# The Setup_Token is itself a secret; redact it from all captured output.
add_redact "$SETUP_TOKEN"

# --- cleanup trap (Req 5.6) --------------------------------------------------
# On ANY exit: drop the AWS credentials from the environment and remove the
# temp files this script created, so no credential material survives.

cleanup() {
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
          AWS_CREDENTIAL_EXPIRATION 2>/dev/null || true
    local f
    for f in "${TMP_FILES[@]:-}"; do
        [ -n "$f" ] && rm -f "$f" 2>/dev/null || true
    done
}
trap cleanup EXIT

# --- HTTP helpers (curl preferred, wget fallback) ----------------------------

HTTP_TOOL=""
if command -v curl >/dev/null 2>&1; then
    HTTP_TOOL="curl"
elif command -v wget >/dev/null 2>&1; then
    HTTP_TOOL="wget"
fi

# http_post <url> <json-body> <out-body-file> -> echoes the HTTP status code.
http_post() {
    local url="$1" body="$2" out="$3" code=""
    if [ "$HTTP_TOOL" = "curl" ]; then
        code=$(curl -sS -o "$out" -w '%{http_code}' \
            -X POST -H 'Content-Type: application/json' \
            --data "$body" "$url" 2>/dev/null)
        [ -z "$code" ] && code="000"
        echo "$code"
    elif [ "$HTTP_TOOL" = "wget" ]; then
        local hdrs
        hdrs=$(wget -q -O "$out" -S --header='Content-Type: application/json' \
            --post-data="$body" "$url" 2>&1)
        local rc=$?
        code=$(printf '%s\n' "$hdrs" | grep -oE 'HTTP/[0-9.]+ [0-9]{3}' | tail -1 | awk '{print $2}')
        [ -z "$code" ] && { [ $rc -eq 0 ] && code="200" || code="000"; }
        echo "$code"
    else
        echo "000"
    fi
}

# --- minimal JSON string-field extraction ------------------------------------
# The endpoint returns small, server-controlled JSON with flat string fields
# (credential fields are nested under "credentials" but still match by key), so
# a targeted extractor avoids a hard dependency on jq/python.
json_string() {
    local file="$1" key="$2"
    grep -oE "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$file" \
        | head -1 \
        | sed -E "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/"
}

# json_escape <string> -> a value safe to embed in a JSON string literal.
json_escape() {
    printf '%s' "$1" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# --- status reporting (Req 6.8) ----------------------------------------------
# POST the outcome to the portal, retrying up to 3 attempts total. If every
# attempt fails, print an undeliverable-status message WITHOUT altering the
# provisioning outcome (the caller's exit code is unchanged).
report_status() {
    local status="$1"
    [ "$STATUS_REPORTED" = "1" ] && return 0

    if [ -z "$QS_URL" ] || [ -z "$REGISTRATION_ID" ] || [ -z "$REPORT_SECRET" ]; then
        logerr "⚠️  Cannot report setup status to the portal (no endpoint, registration id, or report secret available)."
        return 0
    fi

    local summary_json=""
    local arch_json=""
    if [ "$status" = "failed" ]; then
        local trimmed
        trimmed=$(printf '%s' "$ERROR_SUMMARY" | head -c 1024)
        summary_json=",\"error_summary\":\"$(json_escape "$trimmed")\""
    elif [ "$status" = "completed" ] && [ -n "${DETECTED_ARCH:-}" ]; then
        # Report the detected DDA Target_Architecture on success only, and only
        # when one was determined (device-arch-compatibility Req 2.1). The value
        # is a fixed-set token by construction; escape it for consistency.
        arch_json=",\"target_architecture\":\"$(json_escape "$DETECTED_ARCH")\""
    fi
    # NOTE: this body contains report_secret; it is never logged.
    local body="{\"registration_id\":\"${REGISTRATION_ID}\",\"report_secret\":\"${REPORT_SECRET}\",\"status\":\"${status}\"${summary_json}${arch_json}}"

    local attempt out code
    for attempt in 1 2 3; do
        out="$(mktemp)"; TMP_FILES+=("$out")
        code=$(http_post "${QS_URL}/status" "$body" "$out")
        rm -f "$out" 2>/dev/null || true
        if [ "$code" = "200" ]; then
            STATUS_REPORTED=1
            log "✓ Reported setup status '${status}' to the Edge CV Portal."
            return 0
        fi
        [ "$attempt" -lt 3 ] && sleep 3
    done

    logerr ""
    logerr "⚠️  The setup status could not be reported to the Edge CV Portal after 3 attempts."
    logerr "    The provisioning outcome on this station is unchanged."
    return 0
}

# fail_step <detail> — print the failed step and log location, report failure,
# and exit non-zero without attempting subsequent steps (Req 7.7).
fail_step() {
    local detail="${1:-}"
    if [ -n "$detail" ]; then
        ERROR_SUMMARY="Step '${CURRENT_STEP}' failed: ${detail}"
    else
        ERROR_SUMMARY="Step '${CURRENT_STEP}' failed."
    fi
    logerr ""
    logerr "❌ Provisioning failed at step: ${CURRENT_STEP}"
    [ -n "$detail" ] && logerr "   ${detail}"
    logerr "   Installation log: ${LOG_FILE}"
    report_status "failed"
    exit 1
}

# --- header (announce log location, Req 7.3) ---------------------------------

log "=================================================="
log "  DDA Station Quick Setup"
log "=================================================="
log "Device name : ${DEVICE_NAME}"
log "Device group: ${THING_GROUP}"
log "AWS region  : ${AWS_REGION_PARAM}"
log "Installation log: ${LOG_FILE}"

# --- preconditions -----------------------------------------------------------

if [ ! -f "$SETUP_STATION" ]; then
    CURRENT_STEP="Locate provisioning script"
    fail_step "setup_station.sh not found at ${SETUP_STATION}"
fi

MISSING=""
[ -z "$DEVICE_NAME" ]      && MISSING="${MISSING} device_name"
[ -z "$AWS_REGION_PARAM" ] && MISSING="${MISSING} aws_region"
[ -z "$QS_URL" ]           && MISSING="${MISSING} quick_setup_url"
[ -z "$SETUP_TOKEN" ]      && MISSING="${MISSING} setup_token"
if [ -n "$MISSING" ]; then
    CURRENT_STEP="Validate parameters"
    fail_step "missing required parameter(s):${MISSING}"
fi

# --- STEP: exchange provisioning credentials (Req 5.6, 5.8, 7.5) -------------

banner "Exchange provisioning credentials"
CRED_FILE="$(mktemp)"; TMP_FILES+=("$CRED_FILE")
CODE=$(http_post "${QS_URL}/credentials" "{\"token\": \"${SETUP_TOKEN}\"}" "$CRED_FILE")

if [ "$CODE" != "200" ]; then
    ERR_CODE=$(json_string "$CRED_FILE" "error")
    rm -f "$CRED_FILE" 2>/dev/null || true
    case "$ERR_CODE" in
        invalid_token|token_expired)
            logerr "❌ The setup token is no longer valid (portal reported: ${ERR_CODE})."
            logerr "   Generate a new setup command from the Edge CV Portal and run it again."
            logerr "   Installation log: ${LOG_FILE}"
            exit 1
            ;;
        *)
            fail_step "credential exchange failed (HTTP ${CODE})"
            ;;
    esac
fi

AWS_ACCESS_KEY_ID=$(json_string "$CRED_FILE" "access_key_id")
AWS_SECRET_ACCESS_KEY=$(json_string "$CRED_FILE" "secret_access_key")
AWS_SESSION_TOKEN=$(json_string "$CRED_FILE" "session_token")
REPORT_SECRET=$(json_string "$CRED_FILE" "report_secret")
CRED_REGION=$(json_string "$CRED_FILE" "aws_region")
# Shred the response file immediately; credentials live only in shell variables.
rm -f "$CRED_FILE" 2>/dev/null || true

# Redact all secret material from every subsequent captured stream.
add_redact "$AWS_ACCESS_KEY_ID"
add_redact "$AWS_SECRET_ACCESS_KEY"
add_redact "$AWS_SESSION_TOKEN"
add_redact "$REPORT_SECRET"

if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] || [ -z "$AWS_SESSION_TOKEN" ]; then
    fail_step "the provisioning credentials returned by the portal were incomplete"
fi

export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
[ -z "$AWS_REGION_PARAM" ] && [ -n "$CRED_REGION" ] && AWS_REGION_PARAM="$CRED_REGION"
log "✓ Provisioning credentials obtained (values redacted from output and log)."

# --- STEP: provision the station (setup_station.sh, Req 4.6, 6.5) ------------

banner "Provision station (setup_station.sh)"
SS_OUT="$(mktemp)"; TMP_FILES+=("$SS_OUT")

if ! cd "$STATION_DIR"; then
    fail_step "could not enter the provisioning directory ${STATION_DIR}"
fi

# Detect the DDA Target_Architecture up front (read-only, never fatal) so it can
# seed the Greengrass Nucleus platform `variant` override during provisioning:
# LocalServer's per-JetPack aarch64 variants (arm64_jp4/jp5/jp6) all report
# architecture "aarch64", so a multi-arm-variant Workflow_Component only deploys
# to a device that declares its `variant` (device-arch-compatibility). Passing
# the detected token as DDA_PLATFORM_VARIANT makes setup_station.sh write the
# override; an empty/undetermined value lets setup_station.sh self-detect, and a
# non-arm token is a no-op there. The same value is reused for the completion
# report below (a single detection keeps the applied override and the reported
# architecture consistent).
if command -v detect_target_architecture >/dev/null 2>&1; then
    DETECTED_ARCH="$(detect_target_architecture 2>/dev/null || true)"
fi

# setup_station.sh's EXIT-trap does not propagate a non-zero exit code, so its
# outcome is determined from its final summary line rather than $?. All output
# is redacted (Req 5.8) and captured to both the log and SS_OUT for inspection.
DDA_THING_GROUP="$THING_GROUP" \
DDA_PLATFORM_VARIANT="$DETECTED_ARCH" \
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_SESSION_TOKEN="$AWS_SESSION_TOKEN" \
    bash "$SETUP_STATION" "$AWS_REGION_PARAM" "$DEVICE_NAME" 2>&1 \
    | redact | tee -a "$LOG_FILE" | tee "$SS_OUT" >/dev/null

if grep -q "Setup completed with ERRORS" "$SS_OUT"; then
    ERR_LINE=$(grep "^❌" "$SS_OUT" | tail -1)
    fail_step "setup_station.sh reported errors: ${ERR_LINE:-see installation log}"
elif grep -qE "Setup completed successfully|Setup completed with warnings" "$SS_OUT"; then
    log "✓ Station provisioning completed."
else
    fail_step "setup_station.sh did not complete (no completion summary found)"
fi

# --- STEP: verify Device_Group membership (Req 4.6, 4.7) ---------------------

banner "Verify Device_Group membership"
if [ -z "$THING_GROUP" ]; then
    log "⚠️  No Device_Group specified; skipping membership verification."
elif ! command -v aws >/dev/null 2>&1; then
    log "⚠️  AWS CLI not available; skipping Device_Group membership verification."
else
    GRP_OUT="$(mktemp)"; TMP_FILES+=("$GRP_OUT")
    if aws iot list-thing-groups-for-thing \
            --thing-name "$DEVICE_NAME" --region "$AWS_REGION_PARAM" \
            > "$GRP_OUT" 2>/dev/null; then
        if grep -qF "\"${THING_GROUP}\"" "$GRP_OUT"; then
            log "✓ Device is a member of Device_Group '${THING_GROUP}'."
        else
            rm -f "$GRP_OUT" 2>/dev/null || true
            fail_step "device '${DEVICE_NAME}' is not a member of Device_Group '${THING_GROUP}' after provisioning"
        fi
    else
        # The scoped provisioning credentials may not permit
        # iot:ListThingGroupsForThing, so a query failure cannot be treated as a
        # join failure; rely on the provisioning result above instead.
        log "⚠️  Could not query Device_Group membership (insufficient permission or API error); relying on the provisioning result."
    fi
    rm -f "$GRP_OUT" 2>/dev/null || true
fi

# --- STEP: verify Greengrass service -----------------------------------------

banner "Verify Greengrass service"
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet greengrass; then
        log "✓ Greengrass system service is active."
    else
        STATE=$(systemctl is-active greengrass 2>/dev/null || echo "unknown")
        fail_step "the Greengrass system service is not active (state: ${STATE})"
    fi
else
    log "⚠️  systemctl not available; skipping Greengrass service verification."
fi

# --- STEP: detect device architecture (Req 1) --------------------------------
# Non-fatal: determine the Station's DDA Target_Architecture for reporting. Any
# failure logs a warning and leaves DETECTED_ARCH empty; it never calls
# fail_step and never changes the provisioned end state (Req 1.4, 1.5).

banner "Detect device architecture"
# DETECTED_ARCH is normally already resolved before provisioning (it seeds the
# Nucleus platform variant override); re-detect only if that earlier attempt
# came back empty so the completion report still carries a value when possible.
if [ -z "${DETECTED_ARCH:-}" ] && command -v detect_target_architecture >/dev/null 2>&1; then
    DETECTED_ARCH="$(detect_target_architecture 2>/dev/null || true)"
fi
if [ -n "${DETECTED_ARCH:-}" ]; then
    log "✓ Detected device architecture: ${DETECTED_ARCH}"
elif command -v detect_target_architecture >/dev/null 2>&1; then
    log "⚠️  Could not determine the device architecture; it will be left unrecorded."
else
    log "⚠️  Architecture-detection helper unavailable; skipping architecture detection."
fi

# --- STEP: report completion (Req 6.1 via 6.8) -------------------------------

banner "Report completion to the portal"
report_status "completed"

# --- success (Req 7.6) -------------------------------------------------------

log ""
log "=================================================="
log "✅ Station Quick Setup completed successfully"
log "=================================================="
log "Device name : ${DEVICE_NAME}"
log "Device group: ${THING_GROUP}"
log "AWS region  : ${AWS_REGION_PARAM}"
log "Installation log: ${LOG_FILE}"

exit 0
