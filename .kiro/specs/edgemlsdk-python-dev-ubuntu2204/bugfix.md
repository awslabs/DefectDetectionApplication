# Bugfix Requirements Document

## Introduction

AMD64 edgemlsdk Docker image builds fail at `src/edgemlsdk/Dockerfile` line 286
(`RUN apt-get install python-dev -y`, Docker build step 61/83). The AMD64 build
passes the host's Ubuntu release as the `OS` build-arg (the dedicated X86 build
server runs Ubuntu 22.04), so the image base is `public.ecr.aws/ubuntu/ubuntu:22.04`.
On Ubuntu 22.04 (jammy) the retired transitional package `python-dev` has no
installation candidate — apt reports "replaced by: python2-dev python2
python-dev-is-python3" and exits with code 100, killing the image build and the
portal build job.

Verified live: portal build job `08a1e2bd-45f9-4521-ac4a-b41b52222e2e` (AMD64,
dedicated X86 server, source_ref `feature/workflow-triggers`, commit `63ecb99`)
settled `failed` with `BUILD_FAILED` (exit 1) on 2026-08-09 after ~13m35s, at
exactly this step. Full evidence:
`.kiro/specs/edgemlsdk-cmake-pin-failure/verification-notes.md` (Task 7 section).

This is a pre-existing latent defect that was previously unreachable: every
build died ~49 Dockerfile steps earlier at the CMake install step, which spec
`edgemlsdk-cmake-pin-failure` fixed (the same job logs `cmake version 3.31.6`
at the fixed step). The CMake fix unmasked this defect. That spec remains open
pending a `succeeded` build; this follow-on spec removes the next blocker.

**Scan results** (repo scan for other `python-dev`-style retired-package
install sites, per the bug-condition scoping):

- `src/edgemlsdk/Dockerfile:286` — the confirmed live failure (22.04 base has
  no `python-dev` candidate). The only occurrence of a retired transitional
  Python package in this file's apt installs; the file's Python tooling is
  otherwise provided by the Python 3.11 built from source earlier in the file.
- `src/edgemlsdk/Dockerfile.jp5` (line 29, inside the single system-packages
  `apt-get install` block) — contains the same `python-dev` token, but its base
  is pinned by digest to `l4t-jetpack:r35.4.1` (Ubuntu 20.04/focal), where the
  name still resolves (local JP5 build logs `.gdk_build_jp5.log` /
  `.gdk_full_jp5.log` show that layer built successfully). It does not trigger
  the failure on its pinned base; whether to modernize it proactively is a
  design-phase decision, not a requirement of this fix.
- `src/edgemlsdk/Dockerfile.jp6` — already uses `python3-dev`; no defect.
- No other occurrences in shell scripts, compose files, or requirements files.
  Test goldens (`test/backend-test/security/baselines/...jp5_masked.txt`,
  `test/backend-test/edgemlsdk_cmake/baselines/*_cmake_masked.txt`) embed the
  affected lines and track the tree; they change only via sanctioned
  regeneration if their source lines change.

**Bug condition C(X):** an apt install step X in `src/edgemlsdk/Dockerfile`
requests a retired transitional Python package (e.g. `python-dev`) that has no
installation candidate on the file's effective Ubuntu base (22.04 on the AMD64
build servers), so apt exits with code 100 and the image build fails. Concretely
today: line 286's `apt-get install python-dev -y`. Non-buggy inputs ¬C(X) are
every other line of `src/edgemlsdk/Dockerfile` (including the CMake install
block fixed by the prior spec), all of `Dockerfile.jp5` and `Dockerfile.jp6`
(whose occurrence/absence of `python-dev` resolves on their pinned bases), and
all other build steps.

The likely fix direction is replacing `python-dev` with its modern equivalent
or dropping it if unneeded (the build's Python is 3.11 built from source);
that choice is deliberately left to the design phase. The requirement here is
only that the apt install steps resolve successfully on the current base and
that the Triton/edgemlsdk build's Python tooling needs remain satisfied.

**Validation constraint** (same as the prior spec): full Docker image builds
cannot run in automated tests. Fix and preservation checking are validated via
static/property tests over the Dockerfile text, plus the docker
security-baseline suite with sanctioned golden regeneration. Live verification
is gated: builds sync from origin, so a commit+push gate precedes the live
build gate, and each is a separate explicit user approval.

**Completion criterion (user-mandated, same as the prior spec):** this spec is
complete only when an actual portal build reaches `succeeded` including
artifact publication.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the AMD64 edgemlsdk image build (`src/edgemlsdk/Dockerfile` on the Ubuntu 22.04 base) reaches line 286 (`RUN apt-get install python-dev -y`, step 61/83) THEN the system fails with apt exit code 100 ("Package 'python-dev' has no installation candidate ... replaced by: python2-dev python2 python-dev-is-python3") and the image build dies

1.2 WHEN a portal build job targets AMD64 THEN the system terminates the job as `BUILD_FAILED` (exit 1) at the `python-dev` install step, ~13-14 minutes in, before building any application code and before any publish step (verified: job `08a1e2bd-45f9-4521-ac4a-b41b52222e2e`, 2026-08-09)

1.3 WHEN the edgemlsdk image build reaches the Triton dependency install section THEN the system attempts to satisfy its Python tooling need via the retired transitional `python-dev` apt package instead of a package name that resolves on the current Ubuntu base

### Expected Behavior (Correct)

2.1 WHEN the AMD64 edgemlsdk image build (`src/edgemlsdk/Dockerfile` on the Ubuntu 22.04 base) reaches the step at line 286 THEN the system SHALL run an apt install whose every requested package has an installation candidate on the current base, so the step succeeds (no apt exit 100)

2.2 WHEN the edgemlsdk image build completes the Triton dependency install section THEN the system SHALL still satisfy the Python tooling needs of the Triton/edgemlsdk build (headers/dev support consistent with the Python 3.11 toolchain built from source earlier in the Dockerfile), whether by a modern replacement package or by dropping the obsolete package if it is unneeded

2.3 WHEN a portal build job targets AMD64 THEN the system SHALL proceed past Dockerfile step 61/83 without a `python-dev` resolution failure

2.4 WHEN the fix changes lines in `src/edgemlsdk/Dockerfile` THEN the system SHALL regenerate the affected docker security-baseline entries (e.g. the `src/edgemlsdk/Dockerfile` entry in `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`) and any other goldens embedding the changed lines through each mechanism's sanctioned capture path, so the preservation suites pass against the fixed tree

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the AMD64 edgemlsdk image is built THEN the system SHALL CONTINUE TO execute every `src/edgemlsdk/Dockerfile` line other than the fixed `python-dev` install byte-for-byte unchanged — including the pinned upstream release-binary CMake install block from spec `edgemlsdk-cmake-pin-failure`, the Python 3.11 source build, and the neighboring `rapidjson-dev libre2-dev` install at line 285-286

3.2 WHEN the JP5 edgemlsdk image is built (`src/edgemlsdk/Dockerfile.jp5`, pinned `l4t-jetpack:r35.4.1`/focal base) THEN the system SHALL CONTINUE TO resolve its single system-packages apt install block successfully as it does today, with the file untouched unless the design phase determines its `python-dev` occurrence is the same defect requiring the same fix

3.3 WHEN the JP6 edgemlsdk image is built (`src/edgemlsdk/Dockerfile.jp6`) THEN the system SHALL CONTINUE TO use its existing `python3-dev` install unchanged, with the file byte-for-byte untouched

3.4 WHEN the docker security-baseline preservation tests run THEN the system SHALL CONTINUE TO enforce their existing mechanisms (masked-bytes goldens, out-of-scope hash guard, byte-for-byte comparison semantics) against the regenerated goldens

3.5 WHEN the prior spec's `edgemlsdk_cmake` test package runs against the fixed tree THEN the system SHALL CONTINUE TO pass, with its CMake-focused assertions unaffected and any of its goldens that embed the changed line updated only through that package's sanctioned regeneration path
