#!/usr/bin/env bats
#
# Shell tests for station_install/quick_setup/bootstrap.sh
#
# Feature: station-quick-setup, Task 7.4
#   - Unmet-prerequisite fixtures make NO system changes and exit non-zero
#     (Requirements 7.1, 7.2).
#   - Token rejection prints the regenerate instruction and exits non-zero
#     (Requirement 7.5).
#
# Strategy: bootstrap.sh shells out to a handful of external commands to probe
# the station (id, uname, lsb_release, df) and to talk to the Quick_Setup
# endpoint (curl). We put a directory of stub executables at the front of PATH
# so every probe is fully controlled and deterministic, independent of the host
# actually running the tests. The curl stub records every invocation to a
# MARKER file, which lets us assert that a failing prerequisite run never
# reaches the network (a strong proxy for "made no system changes").

setup() {
    STUB="$(mktemp -d)"
    MARKER="${STUB}/curl_called"
    BOOTSTRAP="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)/bootstrap.sh"

    # Remember the original PATH so stubs can chain to the real tools.
    ORIG_PATH="$PATH"

    _write_good_stubs

    PATH="${STUB}:${PATH}"
    export PATH

    # Defaults for the curl stub (overridden per-test as needed).
    export STUB_CURL_STATUS="200"
    export STUB_CURL_BODY=""
}

teardown() {
    [ -n "${STUB:-}" ] && rm -rf "$STUB"
}

# --- stub factories ----------------------------------------------------------

# A curl stub that (a) records that it was invoked, and (b) emulates the
# response contract bootstrap's http_post relies on: write the body to the
# file named by "-o", print the HTTP status code (from -w '%{http_code}') to
# stdout, and exit 0 for transport success. Body/status come from env vars so
# individual tests can shape the response without rewriting the stub.
_write_curl_stub() {
    cat > "${STUB}/curl" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "${MARKER}"
out=""
prev=""
for a in "\$@"; do
    if [ "\$prev" = "-o" ]; then out="\$a"; fi
    prev="\$a"
done
if [ -n "\$out" ]; then
    printf '%s' "\${STUB_CURL_BODY:-}" > "\$out"
fi
printf '%s' "\${STUB_CURL_STATUS:-200}"
exit 0
EOF
    chmod +x "${STUB}/curl"
}

# All prerequisites satisfied: root, supported arch, supported Ubuntu, ample
# disk, plus a present curl. Individual tests overwrite one stub to force a
# single failing prerequisite.
_write_good_stubs() {
    cat > "${STUB}/id" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
EOF
    chmod +x "${STUB}/id"

    cat > "${STUB}/uname" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-m" ]; then echo "x86_64"; else echo "Linux"; fi
EOF
    chmod +x "${STUB}/uname"

    cat > "${STUB}/lsb_release" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    -is) echo "Ubuntu" ;;
    -rs) echo "20.04" ;;
    *)   echo "" ;;
esac
EOF
    chmod +x "${STUB}/lsb_release"

    # df -Pk / : line 2, field 4 is available KB. ~95 GB free.
    cat > "${STUB}/df" <<'EOF'
#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted-on"
echo "/dev/root 104857600 10485760 99614720 10% /"
EOF
    chmod +x "${STUB}/df"

    _write_curl_stub
}

# ============================================================================
# Prerequisite failures: NO system changes, non-zero exit (Req 7.1, 7.2)
# ============================================================================

@test "prereq: non-root exits non-zero, prints the unmet item, never calls curl" {
    cat > "${STUB}/id" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-u" ]; then echo 1000; else exec /usr/bin/id "$@"; fi
EOF
    chmod +x "${STUB}/id"

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Root privileges required"* ]]
    [[ "$output" == *"No changes were made to this system"* ]]
    [ ! -f "$MARKER" ]
}

@test "prereq: unsupported architecture exits non-zero and makes no system changes" {
    cat > "${STUB}/uname" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-m" ]; then echo "mips64"; else echo "Linux"; fi
EOF
    chmod +x "${STUB}/uname"

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Unsupported CPU architecture: 'mips64'"* ]]
    [ ! -f "$MARKER" ]
}

@test "prereq: non-Ubuntu OS exits non-zero and makes no system changes" {
    cat > "${STUB}/lsb_release" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    -is) echo "Debian" ;;
    -rs) echo "12" ;;
    *)   echo "" ;;
esac
EOF
    chmod +x "${STUB}/lsb_release"

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Unsupported OS: 'Debian'"* ]]
    [ ! -f "$MARKER" ]
}

@test "prereq: unsupported Ubuntu release exits non-zero and makes no system changes" {
    cat > "${STUB}/lsb_release" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    -is) echo "Ubuntu" ;;
    -rs) echo "16.04" ;;
    *)   echo "" ;;
esac
EOF
    chmod +x "${STUB}/lsb_release"

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Unsupported Ubuntu release: '16.04'"* ]]
    [ ! -f "$MARKER" ]
}

@test "prereq: insufficient disk space exits non-zero and makes no system changes" {
    cat > "${STUB}/df" <<'EOF'
#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted-on"
echo "/dev/root 104857600 104856576 1024 99% /"
EOF
    chmod +x "${STUB}/df"

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Insufficient free disk space on /"* ]]
    [ ! -f "$MARKER" ]
}

@test "prereq: missing curl and wget is reported as an unmet prerequisite" {
    # Build an isolated PATH that provides every tool bootstrap needs EXCEPT an
    # HTTP client, so command -v curl / wget both fail.
    ISOBIN="${STUB}/isobin"
    mkdir -p "$ISOBIN"
    for t in bash sh awk gawk mawk grep sed head tail mktemp rm find chmod tar \
             sha256sum mkdir dirname basename cut tr sort printf env sleep; do
        p="$(PATH="$ORIG_PATH" command -v "$t" 2>/dev/null || true)"
        [ -n "$p" ] && ln -sf "$p" "$ISOBIN/$t"
    done
    # Keep the good probe stubs (id/uname/lsb_release/df) but drop curl entirely.
    rm -f "${STUB}/curl"
    ln -sf "${STUB}/id" "$ISOBIN/id"
    ln -sf "${STUB}/uname" "$ISOBIN/uname"
    ln -sf "${STUB}/lsb_release" "$ISOBIN/lsb_release"
    ln -sf "${STUB}/df" "$ISOBIN/df"

    # Restrict PATH to the isolated bin for the run only, so teardown still has
    # a normal PATH available.
    PATH="${ISOBIN}" run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"curl or wget"* ]]
    [[ "$output" == *"No changes were made to this system"* ]]
}

@test "prereq: multiple unmet prerequisites are each printed" {
    cat > "${STUB}/id" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-u" ]; then echo 1000; else exec /usr/bin/id "$@"; fi
EOF
    chmod +x "${STUB}/id"
    cat > "${STUB}/uname" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-m" ]; then echo "riscv64"; else echo "Linux"; fi
EOF
    chmod +x "${STUB}/uname"
    cat > "${STUB}/df" <<'EOF'
#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted-on"
echo "/dev/root 104857600 104856576 1024 99% /"
EOF
    chmod +x "${STUB}/df"

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Root privileges required"* ]]
    [[ "$output" == *"Unsupported CPU architecture: 'riscv64'"* ]]
    [[ "$output" == *"Insufficient free disk space on /"* ]]
    [ ! -f "$MARKER" ]
}

# ============================================================================
# Token rejection: print regenerate instruction, non-zero exit (Req 7.5)
# ============================================================================

@test "token: invalid_token response prints the regenerate instruction and exits non-zero" {
    export STUB_CURL_STATUS="403"
    export STUB_CURL_BODY='{"error": "invalid_token", "message": "nope"}'

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.badsecret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Generate a new setup command from the Edge CV Portal"* ]]
    # The endpoint WAS contacted (curl called) but nothing was provisioned.
    [ -f "$MARKER" ]
}

@test "token: token_expired response prints the regenerate instruction and exits non-zero" {
    export STUB_CURL_STATUS="403"
    export STUB_CURL_BODY='{"error": "token_expired", "message": "expired"}'

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.expiredsecret

    [ "$status" -ne 0 ]
    [[ "$output" == *"expired"* ]] || [[ "$output" == *"not valid"* ]]
    [[ "$output" == *"Generate a new setup command from the Edge CV Portal"* ]]
    [ -f "$MARKER" ]
}

@test "token: generic non-200 rejection still prints the regenerate instruction and exits non-zero" {
    export STUB_CURL_STATUS="500"
    export STUB_CURL_BODY='{"error": "internal"}'

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    [[ "$output" == *"Generate a new setup command from the Edge CV Portal"* ]]
}

@test "token: trailing slash on endpoint is normalized before contacting /bundle" {
    export STUB_CURL_STATUS="403"
    export STUB_CURL_BODY='{"error": "invalid_token"}'

    run bash "$BOOTSTRAP" --endpoint https://example.test/quick-setup/ --token dqs1.reg.secret

    [ "$status" -ne 0 ]
    # Recorded curl invocation should target .../quick-setup/bundle (single slash).
    grep -q "https://example.test/quick-setup/bundle" "$MARKER"
    ! grep -q "quick-setup//bundle" "$MARKER"
}
