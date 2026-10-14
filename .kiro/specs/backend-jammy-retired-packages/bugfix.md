# Bugfix Requirements Document

## Introduction

AMD64 docker-compose builds fail in the backend image at `src/backend/Dockerfile`
line 70 (`RUN apt-get install libssl1.1 -y`, target `backend_generic`, Docker
build step 24/63). The AMD64 build passes the host's Ubuntu release as the `OS`
build-arg (the dedicated X86 build server runs Ubuntu 22.04), so the image base
is `public.ecr.aws/ubuntu/ubuntu:22.04`. On Ubuntu 22.04 (jammy) the OpenSSL 1.1
runtime package `libssl1.1` has no installation candidate — jammy ships OpenSSL 3;
`libssl1.1` existed through focal (20.04). apt reports "Unable to locate package
libssl1.1" and exits with code 100, killing the image build and the portal
build job.

Verified live: portal build job `3d18ba88-9c17-490a-811b-8c21360216f4` (AMD64,
dedicated X86 server, source_ref `feature/workflow-triggers`, commit `4e1ce8c`)
settled `failed` with `BUILD_FAILED` (exit 1) on 2026-08-09 after ~21m51s, at
exactly this step. Full evidence:
`.kiro/specs/edgemlsdk-python-dev-ubuntu2204/verification-notes.md` (Task 7
section). In that same job, the edgemlsdk image built completely (83/83 steps,
exported) for the first time on AMD64, and the frontend `react-webapp` image
exported successfully — the build then died in the backend image.

This is the third pre-existing latent defect in the same class, each unmasked
by fixing the previous blocker: spec `edgemlsdk-cmake-pin-failure` fixed the
CMake install step, spec `edgemlsdk-python-dev-ubuntu2204` fixed the retired
`python-dev` package, and now the build reaches the backend image and dies on
the retired `libssl1.1` package. Both sibling specs remain open pending a
`succeeded` build with artifact publication; this follow-on spec removes the
next blocker.

**Broadened scope (user-mandated):** this spec must fix the whole defect class
in one pass, not just `libssl1.1` — each single-package fix costs a ~20-minute
live build to discover the next one. The bug condition therefore covers EVERY
apt install step in the docker-compose AMD64 build path that requests a package
with no installation candidate on the effective Ubuntu 22.04 base.

**Scan results** (full enumeration of the AMD64 compose build path, performed
for this requirements phase):

Images built by `src/docker-compose.yaml` in the AMD64 path
(`build-custom.sh aws.edgeml.dda.LocalServer.amd64`): `backend_generic`
(`src/backend/Dockerfile`, OS=22.04 effective) and `frontend`
(`src/frontend/Dockerfile`). The `backend_generic_nvidia` service is
runtime-only (no build; its `Dockerfile.x86_64_nvidia` belongs to the separate
amd64Nvidia target) and `backend_tegra_gpu_enabled` is the arm64 profile —
neither is built in this path. Dockerfile.jp5/.jp6 are selected only via
`BACKEND_DOCKERFILE` on Jetson targets.

Every apt install step in `src/backend/Dockerfile`, evaluated against jammy
availability:

| Site | Packages | Jammy verdict |
|------|----------|---------------|
| line 4 | software-properties-common wget build-essential cmake libffi-dev zlib1g-dev libssl-dev libsqlite3-dev gdb | ¬C — all resolve; ran successfully in live build 3d18ba88 (before the failure point) |
| lines 16-18 | ppa:ubuntu-toolchain-r/test + gcc-11 g++-11 | ¬C — gcc-11 is jammy's native toolchain; ran live ✓ |
| lines 24-25 | `--only-upgrade` ncurses-base, ncurses-bin | ¬C — ran live ✓ |
| line 26 | libb64-0d | ¬C — jammy universe; ran live ✓ |
| line 49 (`prereqs_install.sh`) | pkgconf libcairo2-dev libgirepository1.0-dev libgl1-mesa-glx libsm6 libxext6 | ¬C — all resolve on jammy (`libgl1-mesa-glx` is transitional on jammy but still a valid candidate; it was retired later, in 23.04+); ran live ✓ |
| line 67 (`install_aravis.sh`) | wget build-essential ninja-build; gstreamer1.0-plugins-bad libxml2-dev libglib2.0-dev libusb-1.0-0-dev gobject-introspection libgtk-3-dev gtk-doc-tools xsltproc libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-plugins-good libgirepository1.0-dev gettext pkg-config libcairo2-dev | ¬C — all resolve; ran live ✓ |
| **line 70** | **libssl1.1** | **C(X) — no installation candidate on jammy. The confirmed live failure.** |
| line 72 | libexif12 libcurl4 libarchive13 gstreamer1.0-tools gstreamer1.0-libav ffmpeg | ¬C — all have jammy candidates, but this step has never executed live on 22.04 (it is past the failure point); flagged for design-phase verification |
| lines 73-75 | libavcodec-extra57i (guarded: `if [ "$OS" = "18.04" ]`) | ¬C for this build path — unreachable when OS=22.04. (The token looks like a typo for `libavcodec-extra57`, but it only executes on the 18.04 base; out of scope here) |
| lines 127-133 (CVE block) | build-essential libgnutls28-dev libuv1 | ¬C — all have jammy candidates; never executed live on 22.04; flagged for design-phase verification |
| line 139 (`install_edgemlsdk.sh`) | (no apt installs in that script) | n/a |

`src/frontend/Dockerfile`: alpine-based (node:18-alpine build stage,
nginx:stable-alpine runtime), zero apt steps, npm only — and the react-webapp
image exported successfully in live build 3d18ba88. No defect, as expected.

**Net scan result: exactly ONE C(X) site exists in the AMD64 compose build
path today — `src/backend/Dockerfile:70` (`libssl1.1`).** The two flagged
live-unverified ¬C sites (lines 72 and 127-133) must be re-verified against
jammy package indexes during the design phase so the class is closed in one
pass.

**Bug condition C(X):** an apt install step X in the docker-compose AMD64
build path (unconditional, or reachable when the effective `OS` build-arg is
22.04) requests a package that has no installation candidate on the Ubuntu
22.04 base, so apt exits with code 100 and the image build fails. Concretely
today: `src/backend/Dockerfile:70`'s `apt-get install libssl1.1 -y`. Non-buggy
inputs ¬C(X) are every other apt install step enumerated above (all resolve on
jammy), every non-apt line of `src/backend/Dockerfile`, all of
`src/frontend/Dockerfile` and `src/docker-compose.yaml`, the
`Dockerfile.jp5`/`.jp6`/`.x86_64_nvidia` variants (not built in this path),
and the OS=18.04-guarded conditional (unreachable on jammy).

**OpenSSL context for the fix direction (design decides):** unlike
`src/edgemlsdk/Dockerfile` (which builds OpenSSL 3.x from source),
`src/backend/Dockerfile` does NOT build OpenSSL from source — its Python 3.11
source build links against the base's `libssl-dev` (OpenSSL 3 on jammy, which
Python 3.11 fully supports). The `libssl1.1` install most plausibly served
prebuilt artifacts on the older bases (the same Dockerfile serves JP4/OS=18.04
and defaults to OS=20.04, where `libssl1.1` resolves; the edgemlsdk artifacts
are installed at line 139, after line 70). The fix direction is a jammy
equivalent (`libssl3` is the jammy OpenSSL runtime, though typically already
present in the base) or removal if nothing on the 22.04 path needs the 1.1
runtime. If the design phase finds a genuine OpenSSL 1.1 dependency in a
prebuilt artifact used on the 22.04 path, that is a design problem to solve
properly, not something to paper over.

**Affected goldens (enumerated):** exactly one — the `src/backend/Dockerfile`
hash entry in
`test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
(currently `40f7c9e00e53096fed4aa5be311f393c231440f241503a3527ac33e5a1741b87`),
which requires sanctioned regeneration when the file changes. No masked-bytes
golden exists for the plain `src/backend/Dockerfile` (the security suite's
masked goldens cover only the `.jp5`/`.jp6` backend and edgemlsdk variants),
and `docker_baseline_default_refs.json` tracks no entry in this file. The
sibling specs' test packages (`edgemlsdk_cmake`, `edgemlsdk_pythondev`) embed
no `src/backend/Dockerfile` content.

**Validation constraint** (same as both sibling specs): full Docker image
builds cannot run in automated tests. Fix and preservation checking are
validated via static/property tests over the Dockerfile text, plus the docker
security-baseline suite with sanctioned golden regeneration. Live verification
is gated: builds sync from origin, so a commit+push gate precedes the live
build gate, and each is a separate explicit user approval (the user has
pre-authorized commit and one deployment-test build for this chain, but the
tasks must still document both gates).

**Completion criterion (user-mandated, shared with both open siblings):** this
spec is complete only when an actual portal build reaches `succeeded`
including artifact publication.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the AMD64 backend image build (`src/backend/Dockerfile`, target `backend_generic`, effective Ubuntu 22.04 base) reaches line 70 (`RUN apt-get install libssl1.1 -y`, step 24/63) THEN the system fails with apt exit code 100 ("Unable to locate package libssl1.1 ... Couldn't find any package by glob 'libssl1.1'") and the image build dies

1.2 WHEN a portal build job targets AMD64 THEN the system terminates the job as `BUILD_FAILED` (exit 1) at the `libssl1.1` install step, ~21-22 minutes in — after the edgemlsdk image (83/83) and the frontend image have built and exported — before building the rest of the backend image and before any publish step (verified: job `3d18ba88-9c17-490a-811b-8c21360216f4`, 2026-08-09)

1.3 WHEN the backend image build attempts to provide an OpenSSL 1.1-era runtime library THEN the system requests the retired `libssl1.1` package name, which resolves only on Ubuntu 20.04 and earlier bases, instead of a name (or approach) that works on the current Ubuntu 22.04 base

1.4 WHEN retired/EOL package defects on the jammy base are discovered and fixed one at a time THEN the system burns a ~20-minute live build per single-package defect to unmask the next one (pattern confirmed three times: CMake step, `python-dev`, now `libssl1.1`)

### Expected Behavior (Correct)

2.1 WHEN the AMD64 backend image build reaches the step at line 70 THEN the system SHALL run an apt install whose every requested package has an installation candidate on the effective Ubuntu 22.04 base, so the step succeeds (no apt exit 100)

2.2 WHEN the backend image build completes on the 22.04 base THEN the system SHALL still satisfy whatever runtime library need the `libssl1.1` install served — via the jammy OpenSSL runtime (`libssl3`), via removal if nothing on the 22.04 path needs the OpenSSL 1.1 runtime, or via a properly designed solution if the design phase confirms a genuine OpenSSL 1.1 dependency in a prebuilt artifact used on this path

2.3 WHEN a portal build job targets AMD64 THEN the system SHALL proceed past `src/backend/Dockerfile` step 24/63 without a `libssl1.1` resolution failure

2.4 WHEN the whole-class scan verdicts are confirmed in the design phase THEN the system SHALL have NO apt install step in the docker-compose AMD64 build path (unconditional or reachable at OS=22.04, in `src/backend/Dockerfile`, its invoked install scripts, and `src/frontend/Dockerfile`) that requests a package with no installation candidate on the Ubuntu 22.04 base — closing the class in one pass, including the two flagged live-unverified sites (lines 72 and 127-133), which SHALL be verified against jammy package availability and fixed in the same pass if any package there lacks a candidate

2.5 WHEN the fix changes lines in `src/backend/Dockerfile` THEN the system SHALL regenerate the affected golden — the `src/backend/Dockerfile` entry in `test/backend-test/security/baselines/docker_baseline_out_of_scope.json` — through the security suite's sanctioned capture path, so the preservation suites pass against the fixed tree

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the AMD64 backend image is built THEN the system SHALL CONTINUE TO execute every `src/backend/Dockerfile` line not identified as C(X) byte-for-byte unchanged — including the Python 3.11 source build, the awscrt vendored-link workaround, the prereqs/aravis/edgemlsdk install script invocations, the neighboring `apt update -y` lines 69/71, and the ¬C apt install steps enumerated in the scan (unless design-phase verification of the two flagged sites reclassifies one as C(X), in which case only that site changes)

3.2 WHEN the same `src/backend/Dockerfile` is built with OS=18.04 (JP4) or OS=20.04 (the default) THEN the system SHALL CONTINUE TO satisfy those bases' runtime library needs — `libssl1.1` currently resolves and installs there, so the fix SHALL NOT remove the OpenSSL 1.1 runtime from paths that still need it (a conditional install, base-appropriate package selection, or a design-verified removal are all acceptable; breaking the JP4/tegra paths is not)

3.3 WHEN the AMD64 compose build runs THEN the system SHALL CONTINUE TO build the frontend image from `src/frontend/Dockerfile` and orchestrate services via `src/docker-compose.yaml` with both files byte-for-byte untouched, and SHALL CONTINUE TO leave `src/backend/Dockerfile.jp5`, `Dockerfile.jp6`, and `Dockerfile.x86_64_nvidia` byte-for-byte untouched

3.4 WHEN the docker security-baseline preservation tests run THEN the system SHALL CONTINUE TO enforce their existing mechanisms (masked-bytes goldens, out-of-scope hash guard, default-refs guard, byte-for-byte comparison semantics) against the regenerated golden, with the jp5/jp6 masked goldens and `docker_baseline_default_refs.json` bit-identical (no entry in them covers the plain `src/backend/Dockerfile`)

3.5 WHEN the sibling specs' test packages (`test/backend-test/edgemlsdk_cmake/`, `test/backend-test/edgemlsdk_pythondev/`) run against the fixed tree THEN the system SHALL CONTINUE TO pass them with their goldens bit-identical, since `src/edgemlsdk/**` is untouched by this fix
