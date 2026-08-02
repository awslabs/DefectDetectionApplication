#!/usr/bin/env bats
#
# Shell tests for station_install/quick_setup/detect_arch.sh
# (device-arch-compatibility task 1.2).
#
# **Feature: device-arch-compatibility, Property 8: Detection range**
# **Validates: Requirements 1.1, 1.4**
#
# detect_target_architecture must print exactly one member of the fixed set
#   {x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, arm64_jp6}
# or nothing (empty) when undetermined, and must NEVER exit non-zero.
#
# Strategy: the function resolves its inputs from (a) overridable file/uname
# environment variables and (b) a small set of external commands (uname,
# dpkg-query, nvidia-smi, lspci) found on PATH. We therefore:
#   1. Point NV_TEGRA_RELEASE_FILE / NVIDIA_PROC_VERSION_FILE at fixture files
#      and set DETECT_ARCH_UNAME_M to fix the machine name.
#   2. Prepend a directory of stub executables so dpkg-query / nvidia-smi /
#      lspci behaviour is deterministic and offline. Each stub's behaviour is
#      controlled by env vars, and any command not stubbed is made to "not
#      exist" by pointing PATH exclusively at the stub dir plus coreutils.

setup() {
    STUB="$(mktemp -d)"
    FIX="$(mktemp -d)"

    DETECT_SRC="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)/detect_arch.sh"

    # Keep the real coreutils (sed/grep/head/printf...) reachable, then prepend
    # the stub dir so our fakes win for the probed commands.
    ORIG_PATH="$PATH"
    _write_stubs
    PATH="${STUB}:${PATH}"
    export PATH

    # Default: probes report "no NVIDIA" and dpkg has no l4t package; individual
    # tests override these.
    export STUB_NVIDIA_SMI_OK="0"     # nvidia-smi exit code sentinel: 0=absent
    export STUB_LSPCI_OUTPUT=""       # empty lspci listing
    export STUB_DPKG_L4T_VERSION=""   # no nvidia-l4t-core installed
    unset NV_TEGRA_RELEASE_FILE NVIDIA_PROC_VERSION_FILE DETECT_ARCH_UNAME_M
    # Point the NVIDIA proc-version file at a guaranteed-absent path so the
    # x86_64 GPU probe does not pick up the host's real /proc entry.
    export NVIDIA_PROC_VERSION_FILE="${FIX}/no-such-nvidia-version"
}

teardown() {
    [ -n "${STUB:-}" ] && rm -rf "$STUB"
    [ -n "${FIX:-}" ] && rm -rf "$FIX"
}

_write_stubs() {
    # nvidia-smi: present-but-failing vs present-and-succeeding vs absent.
    # STUB_NVIDIA_SMI_OK: "1" -> exists and succeeds; anything else -> the stub
    # exits non-zero so the probe treats the runtime as unusable.
    cat > "${STUB}/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
[ "${STUB_NVIDIA_SMI_OK:-0}" = "1" ] && exit 0
exit 1
EOF
    chmod +x "${STUB}/nvidia-smi"

    # lspci: prints the controllable listing (used to detect an NVIDIA VGA/3D
    # controller). Empty by default.
    cat > "${STUB}/lspci" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${STUB_LSPCI_OUTPUT:-}"
exit 0
EOF
    chmod +x "${STUB}/lspci"

    # dpkg-query: emulates `-W -f '${Version}' nvidia-l4t-core`. Prints the
    # controllable version and exits 0 when set; otherwise exits 1 (package not
    # installed), matching dpkg's behaviour.
    cat > "${STUB}/dpkg-query" <<'EOF'
#!/usr/bin/env bash
if [ -n "${STUB_DPKG_L4T_VERSION:-}" ]; then
    printf '%s' "${STUB_DPKG_L4T_VERSION}"
    exit 0
fi
exit 1
EOF
    chmod +x "${STUB}/dpkg-query"
}

# run_detect <expected> : source the helper and assert the printed value and a
# zero exit code (the function must never fail the caller).
_run_detect() {
    run bash -c ". '${DETECT_SRC}'; detect_target_architecture"
    [ "$status" -eq 0 ]
    [ "$output" = "$1" ]
}

# --- helpers to build fixtures ----------------------------------------------

_tegra_release() {  # <R-major-line-body>
    printf '%s\n' "$1" > "${FIX}/nv_tegra_release"
    export NV_TEGRA_RELEASE_FILE="${FIX}/nv_tegra_release"
}

# ============================================================================
# aarch64 via /etc/nv_tegra_release (Req 1.2)
# ============================================================================

@test "aarch64: nv_tegra_release R32 -> arm64_jp4" {
    export DETECT_ARCH_UNAME_M="aarch64"
    _tegra_release "# R32 (release), REVISION: 7.1, GCID: 12345, BOARD: t210ref"
    _run_detect "arm64_jp4"
}

@test "aarch64: nv_tegra_release R35 -> arm64_jp5" {
    export DETECT_ARCH_UNAME_M="aarch64"
    _tegra_release "# R35 (release), REVISION: 4.1, GCID: 12345, BOARD: t186ref"
    _run_detect "arm64_jp5"
}

@test "aarch64: nv_tegra_release R36 -> arm64_jp6" {
    export DETECT_ARCH_UNAME_M="aarch64"
    _tegra_release "# R36 (release), REVISION: 2.0, GCID: 12345, BOARD: t234"
    _run_detect "arm64_jp6"
}

@test "arm64 machine name is treated like aarch64" {
    export DETECT_ARCH_UNAME_M="arm64"
    _tegra_release "# R35 (release), REVISION: 4.1"
    _run_detect "arm64_jp5"
}

@test "aarch64: unknown L4T major -> empty (undetermined)" {
    export DETECT_ARCH_UNAME_M="aarch64"
    _tegra_release "# R28 (release), REVISION: 2.1"
    _run_detect ""
}

# ============================================================================
# aarch64 fallback via nvidia-l4t-core dpkg version (Req 1.2)
# ============================================================================

@test "aarch64: dpkg nvidia-l4t-core 35.x fallback -> arm64_jp5" {
    export DETECT_ARCH_UNAME_M="aarch64"
    # No tegra release file present -> fall back to dpkg.
    export NV_TEGRA_RELEASE_FILE="${FIX}/absent-tegra-release"
    export STUB_DPKG_L4T_VERSION="35.4.1-20230801212816"
    _run_detect "arm64_jp5"
}

@test "aarch64: dpkg nvidia-l4t-core 36.x fallback -> arm64_jp6" {
    export DETECT_ARCH_UNAME_M="aarch64"
    export NV_TEGRA_RELEASE_FILE="${FIX}/absent-tegra-release"
    export STUB_DPKG_L4T_VERSION="36.2.0-20231201000000"
    _run_detect "arm64_jp6"
}

@test "aarch64: dpkg nvidia-l4t-core 32.x fallback -> arm64_jp4" {
    export DETECT_ARCH_UNAME_M="aarch64"
    export NV_TEGRA_RELEASE_FILE="${FIX}/absent-tegra-release"
    export STUB_DPKG_L4T_VERSION="32.7.1-20220219090344"
    _run_detect "arm64_jp4"
}

@test "aarch64: tegra release wins over dpkg fallback" {
    export DETECT_ARCH_UNAME_M="aarch64"
    _tegra_release "# R36 (release), REVISION: 2.0"
    export STUB_DPKG_L4T_VERSION="35.4.1-20230801212816"
    _run_detect "arm64_jp6"
}

@test "aarch64: no release file and no dpkg package -> empty" {
    export DETECT_ARCH_UNAME_M="aarch64"
    export NV_TEGRA_RELEASE_FILE="${FIX}/absent-tegra-release"
    export STUB_DPKG_L4T_VERSION=""
    _run_detect ""
}

# ============================================================================
# x86_64 GPU-runtime probes (Req 1.3)
# ============================================================================

@test "x86_64: no NVIDIA runtime -> x86_64" {
    export DETECT_ARCH_UNAME_M="x86_64"
    _run_detect "x86_64"
}

@test "x86_64: nvidia-smi succeeds -> x86_64_nvidia" {
    export DETECT_ARCH_UNAME_M="x86_64"
    export STUB_NVIDIA_SMI_OK="1"
    _run_detect "x86_64_nvidia"
}

@test "x86_64: /proc/driver/nvidia/version present -> x86_64_nvidia" {
    export DETECT_ARCH_UNAME_M="x86_64"
    printf 'NVRM version: NVIDIA UNIX x86_64 Kernel Module 535.x\n' \
        > "${FIX}/nvidia-version"
    export NVIDIA_PROC_VERSION_FILE="${FIX}/nvidia-version"
    _run_detect "x86_64_nvidia"
}

@test "x86_64: lspci shows an NVIDIA VGA controller -> x86_64_nvidia" {
    export DETECT_ARCH_UNAME_M="x86_64"
    export STUB_LSPCI_OUTPUT="01:00.0 VGA compatible controller: NVIDIA Corporation GA104 [GeForce RTX 3070]"
    _run_detect "x86_64_nvidia"
}

@test "x86_64: lspci shows a non-NVIDIA VGA controller -> x86_64" {
    export DETECT_ARCH_UNAME_M="x86_64"
    export STUB_LSPCI_OUTPUT="00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 630"
    _run_detect "x86_64"
}

@test "amd64 machine name is treated like x86_64" {
    export DETECT_ARCH_UNAME_M="amd64"
    export STUB_NVIDIA_SMI_OK="1"
    _run_detect "x86_64_nvidia"
}

# ============================================================================
# Unknown machine -> empty (undetermined) (Req 1.4)
# ============================================================================

@test "unknown machine name -> empty (undetermined)" {
    export DETECT_ARCH_UNAME_M="riscv64"
    _run_detect ""
}

@test "empty machine name -> empty (undetermined)" {
    export DETECT_ARCH_UNAME_M=""
    # With the override empty the helper calls uname; force a stub uname that
    # prints nothing so the machine is undetermined and the arch is empty.
    cat > "${STUB}/uname" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' ""
EOF
    chmod +x "${STUB}/uname"
    _run_detect ""
}
