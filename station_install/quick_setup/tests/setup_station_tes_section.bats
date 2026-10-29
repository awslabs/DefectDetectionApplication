#!/usr/bin/env bats
#
# setup_station.sh's "Updating GreengrassV2TokenExchangeRole" section under
# Quick Setup. The portal prepares the token exchange role and its policies
# itself (edge-cv-portal/backend/functions/token_exchange_role.py), the
# station's provisioning credentials carry no IAM write action, and run.sh
# passes DDA_TES_ROLE_PREPARED=1 when the portal reports success. The section
# must then make no IAM call and print no warning; without the flag (a manual
# install) it must run as before.
#
# The section is cut out of the real script between its banner and the next
# one, and sourced with a stub `aws` that records its calls and the helpers it
# needs from the top of setup_station.sh (add_warning, run_cmd).

setup() {
    WORK="$(mktemp -d)"
    SETUP_STATION="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)/setup_station.sh"
    local start end
    start=$(grep -n '^echo "▶ Updating GreengrassV2TokenExchangeRole"' "$SETUP_STATION" | cut -d: -f1)
    end=$(grep -n '^echo "▶ Configuring CSI camera exposure (opt-in)"' "$SETUP_STATION" | cut -d: -f1)
    [ -n "$start" ] && [ -n "$end" ] && [ "$start" -lt "$end" ]
    # From the line above the banner to the line above the next banner's rule.
    sed -n "$((start - 1)),$((end - 2))p" "$SETUP_STATION" > "$WORK/section.sh"

    mkdir -p "$WORK/bin"
    cat > "$WORK/bin/aws" <<'EOF'
#!/usr/bin/env bash
echo "aws $*" >> "$AWS_CALLS"
case "$1 $2" in
    "sts get-caller-identity") echo 123456789012 ;;
esac
exit 0
EOF
    chmod +x "$WORK/bin/aws"
    export AWS_CALLS="$WORK/aws-calls" LOG_FILE="$WORK/install.log"
    : > "$AWS_CALLS"
    unset DDA_TES_ROLE_PREPARED
}

teardown() {
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
}

# _run_section : source the section with the stub aws and print the number
# of warnings it added on the last line.
_run_section() {
    run env PATH="$WORK/bin:$PATH" bash -c '
        WARNINGS=()
        add_warning() { WARNINGS+=("$1"); echo "WARNING: $1"; }
        run_cmd() { eval "$1" >> "$LOG_FILE" 2>&1; }
        . "$1"
        echo "warnings=${#WARNINGS[@]}"
    ' _ "$WORK/section.sh"
}

@test "the section was found and is the role policy update" {
    grep -q "put-role-policy" "$WORK/section.sh"
    grep -q "DDA_TES_ROLE_PREPARED" "$WORK/section.sh"
}

@test "DDA_TES_ROLE_PREPARED=1: no IAM call, no warning, says the portal prepared the role" {
    export DDA_TES_ROLE_PREPARED=1
    _run_section
    [ "$status" -eq 0 ]
    [[ "$output" == *"GreengrassV2TokenExchangeRole was prepared by the Edge CV Portal; skipping role policy updates"* ]]
    [[ "$output" != *"updated successfully"* ]]
    [ "${lines[-1]}" = "warnings=0" ]
    ! grep -q '^aws iam' "$AWS_CALLS"
}

@test "without the flag (a manual install): the four inline policies and the managed policy attach run" {
    _run_section
    [ "$status" -eq 0 ]
    [[ "$output" == *"✓ GreengrassV2TokenExchangeRole updated successfully"* ]]
    [ "${lines[-1]}" = "warnings=0" ]
    [ "$(grep -c '^aws iam put-role-policy' "$AWS_CALLS")" -eq 4 ]
    for name in GreengrassComponentS3Access CloudWatchLogsPolicy ECRComponentAccess ShadowManagerSyncPolicy; do
        grep -q "^aws iam put-role-policy .*--policy-name ${name} " "$AWS_CALLS"
    done
    grep -q '^aws iam attach-role-policy --role-name GreengrassV2TokenExchangeRole --policy-arn arn:aws:iam::123456789012:policy/DDAPortalComponentAccessPolicy' "$AWS_CALLS"
}

@test "DDA_TES_ROLE_PREPARED=0 behaves like a manual install" {
    export DDA_TES_ROLE_PREPARED=0
    _run_section
    [ "$status" -eq 0 ]
    [ "$(grep -c '^aws iam put-role-policy' "$AWS_CALLS")" -eq 4 ]
}
