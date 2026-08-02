#!/bin/bash
#
# detect_arch.sh — Station DDA Target_Architecture detection for Quick Setup.
#
# Exposes one pure function, detect_target_architecture, that prints exactly
# one of:
#     x86_64 | x86_64_nvidia | arm64_jp4 | arm64_jp5 | arm64_jp6
# or nothing (empty output) when the architecture cannot be resolved to the
# fixed set. It is read-only (no system changes) and NEVER exits non-zero, so
# the caller can capture its output without risking the provisioning run
# (device-arch-compatibility Requirements 1.1-1.5).
#
# Source it and call the function:
#     . detect_arch.sh
#     arch="$(detect_target_architecture)"
#
# On aarch64/arm64 the JetPack major is derived from the installed L4T release
# (which distinguishes JetPack 4/5/6 — the kernel CPU arch does not); on
# x86_64/amd64 the value is x86_64_nvidia when an NVIDIA GPU runtime is
# detectable, else x86_64.
#
# Detection sources are overridable via environment variables so the function
# is unit-testable with fixtures:
#     NV_TEGRA_RELEASE_FILE     path to the L4T release file
#                               (default /etc/nv_tegra_release)
#     NVIDIA_PROC_VERSION_FILE  path to the NVIDIA kernel-module version file
#                               (default /proc/driver/nvidia/version)
#     DETECT_ARCH_UNAME_M       overrides the `uname -m` machine name
# The dpkg-query / nvidia-smi / lspci probes resolve through PATH so they can
# be stubbed in tests.

# _detect_arch_machine -> the CPU machine name (uname -m), honoring an override.
_detect_arch_machine() {
    if [ -n "${DETECT_ARCH_UNAME_M:-}" ]; then
        printf '%s' "$DETECT_ARCH_UNAME_M"
    else
        uname -m 2>/dev/null || true
    fi
}

# _detect_jetpack_arch_from_major <major> -> the arch token for a known L4T
# major (32/35/36), else nothing.
_detect_jetpack_arch_from_major() {
    case "$1" in
        32) printf 'arm64_jp4' ;;
        35) printf 'arm64_jp5' ;;
        36) printf 'arm64_jp6' ;;
        *)  : ;;   # unknown / empty -> undetermined
    esac
}

# _detect_jetpack_from_tegra_release <file> -> arm64_jp{4,5,6} or nothing.
# The first line of nv_tegra_release looks like:
#   # R35 (release), REVISION: 4.1, GCID: ..., BOARD: ...
_detect_jetpack_from_tegra_release() {
    local file="$1" major
    [ -n "$file" ] && [ -r "$file" ] || return 0
    major=$(sed -nE 's/^#[[:space:]]*R([0-9]+).*/\1/p' "$file" 2>/dev/null | head -1)
    _detect_jetpack_arch_from_major "$major"
}

# _detect_jetpack_from_dpkg -> arm64_jp{4,5,6} or nothing.
# Fallback source: the leading major of the nvidia-l4t-core package version,
# e.g. "35.4.1-20230..." -> 35.
_detect_jetpack_from_dpkg() {
    local version major
    command -v dpkg-query >/dev/null 2>&1 || return 0
    version=$(dpkg-query -W -f '${Version}' nvidia-l4t-core 2>/dev/null) || return 0
    major=$(printf '%s' "$version" | sed -nE 's/^([0-9]+).*/\1/p')
    _detect_jetpack_arch_from_major "$major"
}

# _detect_has_nvidia_gpu -> 0 (true) when an NVIDIA GPU runtime is detectable,
# else 1. Read-only probes only.
_detect_has_nvidia_gpu() {
    local proc_file="${NVIDIA_PROC_VERSION_FILE:-/proc/driver/nvidia/version}"
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        return 0
    fi
    if [ -e "$proc_file" ]; then
        return 0
    fi
    if command -v lspci >/dev/null 2>&1 \
            && lspci 2>/dev/null | grep -iE '(VGA|3D)' | grep -qi 'nvidia'; then
        return 0
    fi
    return 1
}

# detect_target_architecture -> prints one fixed-set value or nothing.
# Never exits non-zero.
detect_target_architecture() {
    local machine arch=""
    machine="$(_detect_arch_machine)"
    case "$machine" in
        aarch64|arm64)
            arch="$(_detect_jetpack_from_tegra_release \
                "${NV_TEGRA_RELEASE_FILE:-/etc/nv_tegra_release}")"
            [ -n "$arch" ] || arch="$(_detect_jetpack_from_dpkg)"
            ;;
        x86_64|amd64)
            if _detect_has_nvidia_gpu; then
                arch="x86_64_nvidia"
            else
                arch="x86_64"
            fi
            ;;
        *)
            arch=""
            ;;
    esac
    printf '%s' "$arch"
    return 0
}
