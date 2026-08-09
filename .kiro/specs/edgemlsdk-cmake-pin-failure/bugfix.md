# Bugfix Requirements Document

## Introduction

Every edgemlsdk Docker image build for the AMD64 and JP5 (arm64) targets fails
at the CMake installation step. `src/edgemlsdk/Dockerfile` (used by AMD64
builds, both its 18.04/bionic and 20.04/focal branches) and
`src/edgemlsdk/Dockerfile.jp5` pin CMake to
`cmake=3.21.3-0kitware1ubuntu18.04.1` / `cmake=3.21.3-0kitware1ubuntu20.04.1`
from the Kitware apt repository (`apt.kitware.com`). The Kitware repo no longer
serves that 3.21.3 package version, so `apt-get install` exits with code 100
and the image build dies early — before any project code is even copied in.

Verified live: portal build job `40b036fc-c379-4a70-b00e-e6d6b0d46ecb` (AMD64,
dedicated, source_ref `feature/portal-build-fleet-and-workflow-gates`, commit
`646fb9d`) failed on 2026-08-08 with `BUILD_FAILED` exit 1; the on-server gdk
log tail shows apt exit code 100 on `cmake=3.21.3-0kitware1ubuntu20.04.1` at
the edgemlsdk cmake install step (~step 7, ~3 minutes in). Recorded in
`.kiro/specs/build-fleet-execution-failures/verification-notes.md` (Task 13).
This cmake pin is now the front-line blocker for any successful build.

The underlying repo drift: Kitware's apt repo now serves CMake 4.x as its
current package. CMake 4.x removed support for
`cmake_minimum_required(VERSION < 3.5)` and breaks Triton's transitively
fetched dependencies, and pinning the apt packages back to a 3.x version fails
because those package versions are no longer published. `Dockerfile.jp6` has
already been fixed in the working tree by installing a pinned CMake 3.x from
the official Kitware GitHub release binary (self-contained installer script,
no apt resolution) — that is the proven pattern to replicate.

`src/edgemlsdk/src/utilities/machine_setup.sh` has the same exposure in a
milder form: its `install_cmake` adds the Kitware apt repo on 18.04 hosts and
installs an **unpinned** `cmake`, so whatever the repo currently serves (4.x,
Triton-incompatible) gets installed — or the install fails outright on
retired distributions.

**Bug condition C(X):** a CMake installation step X (in an edgemlsdk
Dockerfile or setup script) resolves CMake through the Kitware apt repository,
so that the outcome depends on what that repo currently serves — either a
pinned 3.x package version that is no longer published (apt exit 100, build
failure) or an unpinned install that now yields Triton-incompatible CMake 4.x.
Non-buggy inputs ¬C(X) are install steps that already use a pinned upstream
release binary (the JP6 pattern) and every other build step in these files.

**Validation constraint:** full Docker image builds cannot be run in tests.
Fix and preservation checking are validated via static Dockerfile/script
assertions and property tests, plus the existing docker security-baseline
mechanism (whose masked-bytes goldens for the changed files must be
regenerated as an intended consequence of the fix). An actual verification
build through the portal is a separately approved operational task, not part
of this spec's automated validation.
The build must succeed and publish to be successful and complete.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the AMD64 edgemlsdk image build (`src/edgemlsdk/Dockerfile`, 20.04/focal branch) reaches the CMake install step THEN the system runs `apt-get install cmake=3.21.3-0kitware1ubuntu20.04.1 cmake-data=3.21.3-0kitware1ubuntu20.04.1` against the Kitware apt repo, apt exits with code 100 because that package version is no longer served, and the image build fails

1.2 WHEN the AMD64 edgemlsdk image build (`src/edgemlsdk/Dockerfile`, 18.04/bionic branch) reaches the CMake install step THEN the system runs `apt-get install cmake=3.21.3-0kitware1ubuntu18.04.1 ...` against the Kitware apt repo and fails the same way (apt exit 100)

1.3 WHEN the JP5 edgemlsdk image build (`src/edgemlsdk/Dockerfile.jp5`) reaches its CMake install step THEN the system runs `apt-get install cmake-data=3.21.3-0kitware1ubuntu20.04.1 cmake=3.21.3-0kitware1ubuntu20.04.1` against the Kitware apt repo and fails with apt exit 100

1.4 WHEN a portal build job targets AMD64 or JP5 THEN the system terminates the job as `BUILD_FAILED` (exit 1) roughly 3 minutes in, at the edgemlsdk cmake install step, before building any application code (verified: job `40b036fc-c379-4a70-b00e-e6d6b0d46ecb`, 2026-08-08)

1.5 WHEN `machine_setup.sh`'s `install_cmake` runs on an 18.04 host without CMake installed THEN the system adds the Kitware apt repo and installs an unpinned `cmake`, so the installed version is whatever the repo currently serves — CMake 4.x where available (Triton-incompatible) or an outright failure on retired distributions

### Expected Behavior (Correct)

2.1 WHEN the AMD64 edgemlsdk image build (`src/edgemlsdk/Dockerfile`, both OS branches) reaches the CMake install step THEN the system SHALL install a pinned CMake 3.x from the official Kitware GitHub release binary (self-contained installer from `github.com/Kitware/CMake/releases`, arch-selected x86_64/aarch64) without resolving CMake through the Kitware apt repository, and the step SHALL succeed deterministically regardless of what that apt repo currently serves

2.2 WHEN the JP5 edgemlsdk image build (`src/edgemlsdk/Dockerfile.jp5`) reaches its CMake install step THEN the system SHALL install a pinned CMake 3.x via the same upstream release-binary pattern, without resolving CMake through the Kitware apt repository

2.3 WHEN the CMake install step completes in either Dockerfile THEN the system SHALL verify in-build (e.g. `cmake --version`) that the installed CMake is the pinned 3.x version — a Triton-compatible 3.x (≥ 3.21, < 4.0), never 4.x

2.4 WHEN `machine_setup.sh` installs CMake on a host where it is absent THEN the system SHALL install a CMake 3.x deterministically without depending on the current contents of the Kitware apt repository

2.5 WHEN the fixed Dockerfiles change the CMake install lines THEN the system SHALL regenerate the affected docker security-baseline goldens (e.g. `test/backend-test/security/baselines/docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt`) through the baseline mechanism's sanctioned capture path, so the masked-bytes preservation tests pass against the fixed tree

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the JP6 edgemlsdk image build (`src/edgemlsdk/Dockerfile.jp6`) reaches its CMake install step THEN the system SHALL CONTINUE TO use its existing, already-fixed upstream release-binary install (CMake 3.31.6) unchanged

3.2 WHEN any edgemlsdk image is built THEN the system SHALL CONTINUE TO execute every build step other than the CMake install (base image, LLVM/clang setup, Python 3.11 source build, Kitware repo-residue cleanup in the Python step, and all subsequent steps) exactly as before — only the CMake install lines change

3.3 WHEN Triton (and its transitively fetched dependencies) is built inside the edgemlsdk images THEN the system SHALL CONTINUE TO build against a CMake 3.x toolchain (≥ 3.21, < 4.0), preserving the major/minor compatibility the pinned 3.21.3 provided

3.4 WHEN the docker security-baseline preservation tests run THEN the system SHALL CONTINUE TO enforce the masked-bytes golden mechanism (FROM-line masking semantics, byte-for-byte comparison of non-masked lines) against the regenerated goldens

3.5 WHEN `machine_setup.sh` runs on a host where CMake is already installed, or installs its other packages THEN the system SHALL CONTINUE TO skip the CMake install and perform all other setup steps unchanged
