#!/usr/bin/env bats
#
# Smoke test for edge-cv-portal/infrastructure/scripts/build-quick-setup-bundle.sh
#
# Feature: station-quick-setup, Task 8.5 (bundle-content smoke test)
#   - Unpack the built artifact and assert every station_install supporting
#     file (and quick_setup/) is present, so the station needs NO access to the
#     GitHub repository (Requirements 2.6, 4.3).
#   - Assert build/test cruft never ships in the bundle.
#
# Strategy: the packager is a self-contained shell script that tars a
# station_install/ tree into setup-bundle.tar.gz (+ sidecars + manifest.json).
# We run it exactly as CDK's asset bundler does — passing INPUT_DIR / OUTPUT_DIR
# explicitly — against the repository's real station_install/, then extract the
# tarball into a scratch dir and assert on the extracted tree. A second run
# against a copy of station_install/ salted with representative cruft
# (__pycache__, *.pyc, node_modules, .git, a tests/ dir, the bundler itself)
# proves the exclusion rules hold regardless of what the working tree contains.

setup() {
    BUILD_SH="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)/build-quick-setup-bundle.sh"
    # tests/ -> scripts/ -> infrastructure/ -> edge-cv-portal/ -> repo root.
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../../.." && pwd)"
    STATION_INSTALL="${REPO_ROOT}/station_install"

    OUTPUT_DIR="$(mktemp -d)"
    EXTRACT_DIR="$(mktemp -d)"
}

teardown() {
    [ -n "${OUTPUT_DIR:-}" ] && rm -rf "$OUTPUT_DIR"
    [ -n "${EXTRACT_DIR:-}" ] && rm -rf "$EXTRACT_DIR"
    [ -n "${SALTED_INPUT:-}" ] && rm -rf "$SALTED_INPUT"
    return 0
}

# --- helpers -----------------------------------------------------------------

# Build the bundle from $1 (input dir) into OUTPUT_DIR and extract the tarball
# into EXTRACT_DIR. Populates $status/$output from the build run.
_build_and_extract() {
    local input="$1"
    run bash "$BUILD_SH" "$input" "$OUTPUT_DIR"
    [ "$status" -eq 0 ]
    [ -f "${OUTPUT_DIR}/setup-bundle.tar.gz" ]
    tar -xzf "${OUTPUT_DIR}/setup-bundle.tar.gz" -C "$EXTRACT_DIR"
}

# ============================================================================
# Sanity: the fixture we build from is really station_install/
# ============================================================================

@test "fixture: the packager and station_install/ sources exist" {
    [ -f "$BUILD_SH" ]
    [ -d "$STATION_INSTALL" ]
    [ -f "${STATION_INSTALL}/setup_station.sh" ]
    [ -f "${STATION_INSTALL}/quick_setup/bootstrap.sh" ]
}

# ============================================================================
# Output artifacts (Req 4.3, 4.5): bundle + sidecars + manifest
# ============================================================================

@test "artifacts: bundle, sidecars and manifest are produced" {
    run bash "$BUILD_SH" "$STATION_INSTALL" "$OUTPUT_DIR"

    [ "$status" -eq 0 ]
    [ -f "${OUTPUT_DIR}/setup-bundle.tar.gz" ]
    [ -f "${OUTPUT_DIR}/setup-bundle.tar.gz.sha256" ]
    [ -f "${OUTPUT_DIR}/bootstrap.sh" ]
    [ -f "${OUTPUT_DIR}/bootstrap.sh.sha256" ]
    [ -f "${OUTPUT_DIR}/manifest.json" ]
}

@test "artifacts: sidecar checksum verifies against the exact bundle bytes (Req 4.5)" {
    run bash "$BUILD_SH" "$STATION_INSTALL" "$OUTPUT_DIR"
    [ "$status" -eq 0 ]

    # `sha256sum -c` must succeed from within OUTPUT_DIR (relative names).
    ( cd "$OUTPUT_DIR" && sha256sum -c setup-bundle.tar.gz.sha256 )
    ( cd "$OUTPUT_DIR" && sha256sum -c bootstrap.sh.sha256 )
}

# ============================================================================
# Bundle content (Req 2.6, 4.3): every supporting file is self-contained
# ============================================================================

@test "content: every station_install supporting file is present in the bundle (Req 4.3)" {
    _build_and_extract "$STATION_INSTALL"

    # Core provisioning entrypoint + its supporting files. The station must be
    # able to provision from these bytes alone, with no repository checkout.
    [ -f "${EXTRACT_DIR}/setup_station.sh" ]
    [ -f "${EXTRACT_DIR}/patch_docker_host_prereqs.sh" ]
    [ -f "${EXTRACT_DIR}/launch-edge-device.sh" ]
    [ -f "${EXTRACT_DIR}/create-edge-device-iam-role.sh" ]
    [ -f "${EXTRACT_DIR}/edge_manager_agent_config.json" ]
    [ -f "${EXTRACT_DIR}/edge-device-iam-policy.json" ]
}

@test "content: the quick_setup/ directory (bootstrap.sh + run.sh) ships in the bundle" {
    _build_and_extract "$STATION_INSTALL"

    [ -d "${EXTRACT_DIR}/quick_setup" ]
    [ -f "${EXTRACT_DIR}/quick_setup/bootstrap.sh" ]
    [ -f "${EXTRACT_DIR}/quick_setup/run.sh" ]
}

@test "content: the bundle carries no external references (every non-cruft source file is shipped)" {
    _build_and_extract "$STATION_INSTALL"

    # Every regular file directly under station_install/ (the top-level
    # supporting files) must appear in the extracted tree, so the station never
    # needs to reach back to the repository for a missing file (Req 2.6, 4.3).
    while IFS= read -r src; do
        rel="${src#${STATION_INSTALL}/}"
        [ -f "${EXTRACT_DIR}/${rel}" ] || {
            echo "missing from bundle: ${rel}"
            false
        }
    done < <(find "$STATION_INSTALL" -maxdepth 1 -type f)
}

# ============================================================================
# Cruft exclusion: build/test artifacts must never reach the station
# ============================================================================

@test "exclusion: quick_setup/tests is pruned from the shipped bundle" {
    _build_and_extract "$STATION_INSTALL"

    [ ! -e "${EXTRACT_DIR}/quick_setup/tests" ]
}

@test "exclusion: the bundler script itself is never shipped" {
    _build_and_extract "$STATION_INSTALL"

    run find "$EXTRACT_DIR" -name 'build-quick-setup-bundle.sh'
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "exclusion: representative build/test cruft is stripped from the bundle" {
    # Copy station_install/ and salt it with cruft the packager promises to drop.
    SALTED_INPUT="$(mktemp -d)"
    cp -R "${STATION_INSTALL}/." "${SALTED_INPUT}/"

    mkdir -p "${SALTED_INPUT}/__pycache__"
    echo "cache" > "${SALTED_INPUT}/__pycache__/x.cpython-310.pyc"
    echo "compiled" > "${SALTED_INPUT}/stale.pyc"
    mkdir -p "${SALTED_INPUT}/.git"
    echo "gitdir" > "${SALTED_INPUT}/.git/config"
    mkdir -p "${SALTED_INPUT}/node_modules/dep"
    echo "dep" > "${SALTED_INPUT}/node_modules/dep/index.js"
    mkdir -p "${SALTED_INPUT}/.pytest_cache"
    echo "pc" > "${SALTED_INPUT}/.pytest_cache/CACHEDIR.TAG"
    mkdir -p "${SALTED_INPUT}/tests"
    echo "toptest" > "${SALTED_INPUT}/tests/test_top.py"
    cp "$BUILD_SH" "${SALTED_INPUT}/build-quick-setup-bundle.sh"

    _build_and_extract "$SALTED_INPUT"

    # Cruft directories/files must be absent from the extracted tree.
    [ ! -e "${EXTRACT_DIR}/__pycache__" ]
    [ ! -e "${EXTRACT_DIR}/stale.pyc" ]
    [ ! -e "${EXTRACT_DIR}/.git" ]
    [ ! -e "${EXTRACT_DIR}/node_modules" ]
    [ ! -e "${EXTRACT_DIR}/.pytest_cache" ]
    [ ! -e "${EXTRACT_DIR}/tests" ]
    [ ! -e "${EXTRACT_DIR}/build-quick-setup-bundle.sh" ]
    run find "$EXTRACT_DIR" -name '*.pyc'
    [ -z "$output" ]

    # ...but the real supporting files still shipped from the salted copy.
    [ -f "${EXTRACT_DIR}/setup_station.sh" ]
    [ -f "${EXTRACT_DIR}/quick_setup/bootstrap.sh" ]
    [ -f "${EXTRACT_DIR}/quick_setup/run.sh" ]
}
