# Backend jammy Retired Packages Bugfix Design

## Overview

AMD64 docker-compose builds die in the backend image at
`src/backend/Dockerfile` line 70 (`RUN apt-get install libssl1.1 -y`, target
`backend_generic`, Docker build step 24/63). The AMD64 build passes the host's
Ubuntu release as the `OS` build-arg; the dedicated X86 build server runs
Ubuntu 22.04, so the base is `public.ecr.aws/ubuntu/ubuntu:22.04` (jammy) —
where the OpenSSL 1.1 runtime package `libssl1.1` has no installation
candidate (jammy ships OpenSSL 3; `libssl1.1` existed through focal). apt
exits 100 and the image build and the portal build job fail. Verified live on
portal build job `3d18ba88-9c17-490a-811b-8c21360216f4` (AMD64, dedicated,
source_ref `feature/workflow-triggers`, commit `4e1ce8c`, settled `failed` /
`BUILD_FAILED` on 2026-08-09, ~21m51s in) — full evidence in
`.kiro/specs/edgemlsdk-python-dev-ubuntu2204/verification-notes.md` (Task 7).

This is the third pre-existing latent defect in the same class, each unmasked
by fixing the previous blocker (CMake step → `python-dev` → `libssl1.1`), and
per the user mandate this spec closes the whole class in one pass: every apt
install step in the docker-compose AMD64 build path was enumerated in
bugfix.md, and the two live-unverified ¬C sites were re-verified for this
design against the real jammy package index (see Design Decision 2) —
**exactly one C(X) site exists: line 70.**

**The fix**: make the `libssl1.1` install conditional on the base image's
actual release, read from `/etc/os-release` — installed only when
`VERSION_ID` is `18.04` or `20.04` (the bases where it resolves and may be
needed by prebuilt-era artifacts), skipped on `22.04`, whose OpenSSL 3 runtime
(`libssl3`) is already guaranteed present (preinstalled in the base image and
a hard dependency of line 4's `libssl-dev`), and whose edgemlsdk artifacts now
ship their own source-built OpenSSL 3.x (`openssl.deb`, installed by
`install_edgemlsdk.sh`). Critically, the conditional does NOT use the `OS`
build-arg: investigation for this design found that `ARG OS` is declared only
before `FROM` (line 1) and is therefore **out of scope in every RUN**
instruction — the existing `if [ "$OS" = "18.04" ]` conditional at lines
73-75 is inert today, and re-declaring `ARG OS` after `FROM` would activate
that dormant branch on JP4 and attempt to install the typo'd
`libavcodec-extra57i`, breaking a working path (see Design Decision 1).

Because full Docker image builds cannot run in automated tests (same
constraint as both sibling specs), fix and preservation checking are
validated by **static assertions and property tests over the Dockerfile and
install-script text** in a new `test/backend-test/backend_jammy_pkgs/`
package mirroring the proven `edgemlsdk_pythondev` pattern, plus sanctioned
regeneration of the single affected golden (the `src/backend/Dockerfile`
sha256 entry in the security out-of-scope guard). Per the user-mandated
completion criterion shared with both open siblings: **the spec is not
complete until an actual portal build reaches `succeeded` including artifact
publication** — an approval-gated operational verification phase (commit+push
gate, then live-build gate, each a separate explicit approval) follows local
validation. A single `succeeded` AMD64 build closes THREE specs at once.

## Glossary

- **Bug_Condition (C)**: An apt install step X in the docker-compose AMD64
  build path (unconditional, or reachable when the effective base is Ubuntu
  22.04) requests a package with no installation candidate on the jammy base,
  so apt exits 100 and the image build fails. Concretely today:
  `src/backend/Dockerfile:70`'s `libssl1.1`.
- **Property (P)**: Every package requested by a 22.04-reachable apt step has
  an installation candidate on the jammy base (no apt exit 100), and the
  runtime library need the `libssl1.1` install served remains satisfied on
  every base the file supports (OpenSSL 1.1 runtime still installed on
  18.04/20.04; OpenSSL 3 runtime guaranteed on 22.04).
- **Preservation**: Every other line of `src/backend/Dockerfile`
  (byte-for-byte), all of `src/frontend/Dockerfile`,
  `src/docker-compose.yaml`, `src/backend/Dockerfile.jp5`, `.jp6`, and
  `.x86_64_nvidia` (byte-for-byte untouched files), the three install scripts
  invoked by the Dockerfile, and the existing security-baseline mechanisms.
- **jammy**: Ubuntu 22.04 LTS. Ships OpenSSL 3 (`libssl3`); the OpenSSL 1.1
  runtime package `libssl1.1` was retired after focal (20.04) and has no
  installation candidate on jammy.
- **Jammy-retired package**: An apt package name with no installation
  candidate on Ubuntu 22.04 (for this spec's retired set, concretely:
  `libssl1.1`).
- **AMD64-reachable apt step**: An apt install step in `src/backend/Dockerfile`
  or its invoked install scripts (`prereqs_install.sh`, `install_aravis.sh`,
  `install_edgemlsdk.sh`) that executes when the effective base is 22.04 —
  i.e. unconditional steps plus steps inside a release conditional whose
  allowlist includes 22.04. Steps guarded to 18.04/20.04 only (the fixed
  libssl1.1 step; the inert lines 73-75 branch) are NOT AMD64-reachable.
- **`OS` build-arg scoping trap**: `ARG OS` (line 1) precedes `FROM` and is
  consumed only by the `FROM` instruction; per Dockerfile ARG scoping it is
  undefined in all subsequent `RUN` instructions unless re-declared after
  `FROM` — which this file never does (`grep -nE 'ARG '` finds only line 1
  and line 65's `ONNXRUNTIME_SPEC`). Consequence: `$OS` expands empty in every
  RUN, and the lines 73-75 conditional (`if [ "$OS" = "18.04" ]`) never
  fires on any base.
- **`/etc/os-release`**: Standard shell-sourceable release identification
  file present in every Ubuntu base image; `VERSION_ID` is `"18.04"`,
  `"20.04"`, or `"22.04"`. The fix gates on this, not on the `$OS` arg,
  reading the base image's ground truth without touching ARG scoping.
- **`libssl3`**: The jammy OpenSSL 3 runtime package. Verified preinstalled
  in `public.ecr.aws/ubuntu/ubuntu:22.04` (dpkg status `ii`, 3.0.2) and a
  hard dependency of `libssl-dev`, which line 4 installs — so the 22.04 path
  never lacks an OpenSSL runtime even with the libssl1.1 step skipped.
- **edgemlsdk `openssl.deb`**: The edgemlsdk image (fixed by the sibling
  specs) builds OpenSSL 3.x from source and packages it; the backend's
  `install_edgemlsdk.sh` (line 139) installs it via
  `dpkg -i edgemlsdk/openssl.deb`, so the prebuilt edgemlsdk artifacts
  (aws-sdk-cpp, triton-core, ...) bring their own OpenSSL on the 22.04 path.
- **Out-of-scope sha256 guard**:
  `test/backend-test/security/preservation/test_preservation_docker_out_of_scope.py`,
  which pins the full-file sha256 of `src/backend/Dockerfile` in
  `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
  (current entry: `40f7c9e0…741b87` — verified equal to the unfixed tree's
  hash during this design phase).
- **Capture-on-absent / observation-first**: The golden methodology used by
  the sibling packages and the security suite — a golden is captured from the
  UNFIXED tree on first run and asserted byte-for-byte thereafter.
- **Token-boundary matching**: All scans MUST match whole package-name
  tokens (split on whitespace/backslash-continuations), never substrings —
  `libssl1.1` must not match a hypothetical `libssl1.1-dbg`, and `libssl-dev`
  (line 4) must never classify as retired.

## Bug Details

### Bug Condition

The bug manifests when the AMD64 backend image build (base
`ubuntu:${OS}` with `OS=22.04` from the build host) reaches line 70. The step
requests `libssl1.1`, which has no installation candidate on jammy; apt
reports "Unable to locate package libssl1.1 ... Couldn't find any package by
glob 'libssl1.1'" and exits with code 100, killing the image build ~21-22
minutes into the job — after the edgemlsdk image (83/83 steps) and the
frontend image have built and exported — and before any publish step.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type AptInstallStep
         (an apt/apt-get install step in the docker-compose AMD64 build path:
          src/backend/Dockerfile or an install script it invokes, identified
          by file, line, requested package tokens, and guard condition)
  OUTPUT: boolean

  RETURN input.isReachableWhenBaseIs("22.04")
           -- unconditional, or inside a release conditional whose
           -- allowlist includes 22.04
         AND EXISTS pkg IN input.requestedPackages :
               pkg has no installation candidate on Ubuntu 22.04 (jammy)
               -- membership in RETIRED_JAMMY_PACKAGES, today {"libssl1.1"}
  -- concretely today: src/backend/Dockerfile line 70, pkg = "libssl1.1"
END FUNCTION
```

Non-buggy inputs ¬C(X) are every other apt install step enumerated in the
bugfix.md scan (all verified jammy-resolvable — the previously flagged sites
at line 72 and lines 127-133 were re-verified for this design, see Design
Decision 2), every non-apt line of `src/backend/Dockerfile`, all of
`src/frontend/Dockerfile` (alpine, zero apt steps) and
`src/docker-compose.yaml`, the `Dockerfile.jp5`/`.jp6`/`.x86_64_nvidia`
variants (not built in this path), and the lines 73-75 conditional
(unreachable on jammy — and in fact inert on every base, per the `OS`
build-arg scoping trap).

### Examples

- **AMD64 live failure** (Req 1.1, 1.2): job `3d18ba88` reached backend
  Docker step 24/63 and logged `E: Unable to locate package libssl1.1`,
  exit code 100, `BUILD_FAILED`; ~21m51s in, no publish reached. Expected:
  the step set installs only resolvable packages on this base and the build
  proceeds.
- **Neighboring steps succeed on the same base** (¬C anchor): step 23
  (line 69's `apt update -y`) succeeded in the same live build, and line 4's
  nine-package install (including `libssl-dev`, which pulls `libssl3` on
  jammy) ran green much earlier — the defect is the retired package name,
  not the apt section.
- **Same line on 20.04/18.04** (¬C, Req 3.2): with the compose default
  `OS: ${OS:-20.04}` or JP4's 18.04, `libssl1.1` resolves natively (it is
  those releases' own OpenSSL runtime) and the step succeeds — same token,
  no bug condition, because the base resolves it.
- **Design-phase jammy index verification** (closes the class, Req 2.4): in
  a throwaway container on the exact base image
  `public.ecr.aws/ubuntu/ubuntu:22.04`, `apt-cache policy` after
  `apt-get update` shows candidates for all six line-72 packages (libexif12
  0.6.24-1ubuntu0.22.04.1, libcurl4 7.81.0, libarchive13 3.6.0,
  gstreamer1.0-tools 1.20.3, gstreamer1.0-libav 1.20.3, ffmpeg 7:4.4.2) and
  all three CVE-block packages (build-essential 12.9ubuntu3, libgnutls28-dev
  3.7.3, libuv1 1.43.0) — and NO stanza for `libssl1.1` (no candidate).
- **Edge case — the inert 18.04 conditional** (¬C, structural): lines 73-75
  (`if [ "$OS" = "18.04" ] ; then apt install libavcodec-extra57i -y; fi`)
  never execute on ANY base because `$OS` is empty in RUN scope; the typo'd
  package name inside it has therefore never failed a build. The fix must
  not change this (see Design Decision 1).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Every `src/backend/Dockerfile` line other than line 70 executes
  byte-for-byte unchanged — including the Python 3.11 source build, the
  awscrt vendored-link workaround, the prereqs/aravis/edgemlsdk install
  script invocations, the neighboring `apt update -y` lines 69/71, the ¬C
  apt install steps at line 4, lines 16-25, line 26, line 72, and the CVE
  block, and the lines 73-75 conditional with its current (inert) behavior
  (Req 3.1).
- Builds with an 18.04 or 20.04 base continue to install `libssl1.1` exactly
  as before — the conditional keeps the OpenSSL 1.1 runtime on the bases
  that resolve and may need it (Req 3.2).
- `src/frontend/Dockerfile`, `src/docker-compose.yaml`,
  `src/backend/Dockerfile.jp5`, `Dockerfile.jp6`, and
  `Dockerfile.x86_64_nvidia` remain byte-for-byte untouched (Req 3.3).
- The three install scripts (`prereqs_install.sh`, `install_aravis.sh`,
  `install_edgemlsdk.sh`) remain byte-for-byte untouched (their apt steps
  are all ¬C).
- The docker security-baseline preservation mechanisms — masked-bytes
  goldens (backend jp5/jp6, edgemlsdk jp5/jp6), the out-of-scope sha256
  guard, the default-refs guard, byte-for-byte comparison semantics —
  continue to be enforced, against the one regenerated out-of-scope entry
  (Req 3.4).
- Both sibling test packages (`test/backend-test/edgemlsdk_cmake/`,
  `test/backend-test/edgemlsdk_pythondev/`) continue to pass with their
  goldens bit-identical, since `src/edgemlsdk/**` is untouched (Req 3.5).

**Scope:**

All inputs that do NOT involve line 70's retired `libssl1.1` request are
completely unaffected by this fix. This includes:

- All other apt install steps of the AMD64 compose build path (all verified
  jammy-resolvable; those before line 70 additionally ran green in live
  build `3d18ba88`)
- The entire frontend Dockerfile, compose file, and the three backend
  Dockerfile variants not built in this path
- All install scripts, requirements files, and application code
- The security-baseline suite's mechanisms and all its non-regenerated
  goldens

The actual expected correct behavior is defined in the Correctness Properties
section (Property 1).

## Hypothesized Root Cause

The root cause is externally confirmed by the live build log — Ubuntu package
retirement unmasked by environment drift, not a logic bug in our code:

1. **Retired package name**: `libssl1.1` is the OpenSSL 1.1 runtime package,
   native to bionic (18.04) and focal (20.04). Jammy moved to OpenSSL 3
   (`libssl3`) and ships no `libssl1.1` at all; apt reports "Unable to locate
   package" and exits 100. Confirmed verbatim in the job `3d18ba88`
   CloudWatch log and re-confirmed against the jammy index in the
   design-phase container check.

2. **Environment drift unmasked the line**: the `OS` build-arg tracks the
   build host's release; the compose default is 20.04 and JP4 uses 18.04 —
   both resolve `libssl1.1`. The dedicated X86 server runs 22.04. The line
   was additionally shadowed by two earlier blockers (the edgemlsdk CMake
   step, then `python-dev`) until the sibling specs fixed them and the build
   reached the backend image for the first time.

3. **The package's plausible role in this file**: `src/backend/Dockerfile`
   does NOT build OpenSSL from source (unlike `src/edgemlsdk/Dockerfile`);
   its Python 3.11 source build links against the base's `libssl-dev`
   (line 4 — OpenSSL 1.1 on old bases, OpenSSL 3 on jammy; Python 3.11
   supports both). On the old bases `libssl-dev` itself depends on the
   matching `libssl1.1`, so line 70 was largely redundant even there; its
   plausible purpose was belt-and-braces provision of the OpenSSL 1.1
   runtime for prebuilt binary artifacts of that era — chiefly the edgemlsdk
   debs installed later at line 139. On the 22.04 path those artifacts now
   bring their own OpenSSL: the edgemlsdk image builds OpenSSL 3.x from
   source and ships `openssl.deb`, which `install_edgemlsdk.sh` installs;
   and the base's `libssl3` is present regardless (preinstalled, and a hard
   dependency of line 4's `libssl-dev`). No consumer of OpenSSL 1.1 exists
   on the 22.04 path.

4. **Why a naive conditional would backfire**: the obvious fix — mirror the
   lines 73-75 pattern with `if [ "$OS" != "22.04" ]` — silently does the
   WRONG thing: `$OS` is empty in RUN scope (ARG declared only before FROM),
   so the guard would evaluate `"" != "22.04"` → true on every base and
   change nothing. Fixing THAT by re-declaring `ARG OS` after `FROM` would
   activate the dormant lines 73-75 branch on real 18.04 builds and attempt
   the typo'd `libavcodec-extra57i` install, breaking JP4. Reading
   `/etc/os-release` sidesteps the ARG scoping entirely and leaves lines
   73-75 exactly as inert as they are today.

If the exploratory static tests refute any of this (e.g. the line has already
been changed, another jammy-retired package site exists in the AMD64 path, or
a second `ARG OS` declaration exists after FROM), we re-hypothesize before
fixing.

## Correctness Properties

Property 1: Bug Condition - No Jammy-Unresolvable Package in Any AMD64-Reachable Apt Step

_For any_ apt install step in the docker-compose AMD64 build path where the
bug condition holds (isBugCondition returns true — today, exactly line 70's
`libssl1.1` request), the fixed tree SHALL make the step either request only
packages with an installation candidate on the Ubuntu 22.04 base or be
unreachable when the base is 22.04: line 70 becomes a release conditional
gated on `/etc/os-release` `VERSION_ID` with allowlist {18.04, 20.04}, so the
`libssl1.1` install is skipped on jammy (whose OpenSSL runtime need is
satisfied by the base's preinstalled `libssl3`, line 4's `libssl-dev`
dependency on it, and the edgemlsdk artifacts' own `openssl.deb`); and
class-wide, NO AMD64-reachable apt install step in `src/backend/Dockerfile`
or its invoked install scripts SHALL request a jammy-retired package token —
closing the class in one pass, with the two previously flagged sites
(line 72; the CVE block) confirmed jammy-resolvable by the design-phase
index verification.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - All Other Lines, Old-Base Behavior, and Sibling Files Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns
false), the fixed files SHALL produce the same result as the original files:
every line of `src/backend/Dockerfile` other than the line-70 step is
byte-for-byte identical to its pre-fix state (asserted by diff-scoping:
masking out only the libssl install step and comparing the remainder against
a golden captured on the unfixed tree — the masked view thereby also proving
the Python 3.11 build, the awscrt workaround, lines 69/71/72, the inert
lines 73-75 conditional, the CVE block, and the script invocations survive
verbatim); the fixed step still installs `libssl1.1` when `VERSION_ID` is
18.04 or 20.04 (old-base behavior preserved, no `ARG OS` re-declaration
introduced); and `src/frontend/Dockerfile`, `src/docker-compose.yaml`,
`src/backend/Dockerfile.jp5`, `Dockerfile.jp6`, `Dockerfile.x86_64_nvidia`,
and the three install scripts are byte-for-byte identical to their pre-fix
states (full-file sha256 goldens).

**Validates: Requirements 3.1, 3.2, 3.3**

Property 3: Baseline Regeneration - Exactly One Golden Regenerated, Mechanisms Intact

_For any_ golden affected by the fix (exactly one: the
`src/backend/Dockerfile` sha256 entry in
`test/backend-test/security/baselines/docker_baseline_out_of_scope.json`),
the golden SHALL be regenerated through the security suite's sanctioned
capture path in the same commit as the fix, so the preservation suites pass
against the fixed tree — while every golden NOT embedding the changed line
SHALL remain bit-identical (the backend jp5/jp6 and edgemlsdk jp5/jp6
masked-bytes goldens, `docker_baseline_default_refs.json` — which tracks no
entry in this file — the other three out-of-scope entries, and both sibling
packages' goldens in `edgemlsdk_cmake/` and `edgemlsdk_pythondev/`), and the
enforcing mechanisms (masked-bytes comparison, out-of-scope hash guard,
default-refs guard, capture-on-absent semantics) continue to run unchanged.

**Validates: Requirements 2.5, 3.4, 3.5**

### Properties Summary Table

| # | Property | Kind | Validation approach |
|---|----------|------|---------------------|
| 1 | No jammy-unresolvable package in any AMD64-reachable apt step; line 70 gated to 18.04/20.04; class closed with verified verdicts for line 72 + CVE block | Fix check | Static assertions over the fixed Dockerfile/script text: reachability-aware token-boundary scan proving zero jammy-retired tokens in AMD64-reachable apt steps; exact fixed-step assertion; embedded verdict inventory pinning every AMD64-reachable apt package to the design-verified jammy-resolvable set |
| 2 | All other Dockerfile lines, old-base libssl1.1 behavior, and 8 sibling files unchanged | Preservation | Diff-scoped goldens captured on the UNFIXED tree: libssl-step-masked view of `src/backend/Dockerfile`; full-file sha256 of frontend Dockerfile, compose, jp5/jp6/x86_64_nvidia variants, and the three install scripts; compared byte-for-byte after the fix; guard asserting `ARG OS` still appears only before FROM |
| 3 | Exactly one golden regenerated via the sanctioned path; all others bit-identical; mechanisms intact | Preservation | Re-run the full docker security preservation suite and both sibling packages against the fixed tree after sanctioned regeneration; assert non-affected goldens are bit-identical pre/post fix |

## Fix Implementation

### Design Decisions

**Decision 1 — line 70 fix: `/etc/os-release`-gated conditional install
(allowlist 18.04/20.04), not unconditional `libssl3`, not removal, not an
`$OS`-arg conditional.**

Candidate fixes evaluated against the file's three supported bases and the
verified facts:

| Option | 22.04 resolves? | 18.04/20.04 behavior preserved (Req 3.2)? | Structural risk |
|--------|-----------------|-------------------------------------------|-----------------|
| (a1) conditional on `$OS` build-arg | YES (guard skips) | **NO — silently inert**: `$OS` is empty in RUN scope, guard never fires, libssl1.1 no longer installed anywhere | Behaves like removal without saying so |
| (a2) conditional on `$OS` + re-declare `ARG OS` after FROM | YES | libssl1.1 preserved, BUT | **Activates dormant lines 73-75 branch on 18.04 → typo'd `libavcodec-extra57i` install breaks JP4**; also changes more than the C(X) line (Req 3.1) |
| (a3) **conditional on `/etc/os-release` VERSION_ID (chosen)** | YES (guard skips on 22.04) | YES — installs libssl1.1 exactly as today on 18.04/20.04 | None: reads the base's ground truth; lines 73-75 stay inert; single-step diff |
| (b) unconditional `libssl3` | YES | **NO — `libssl3` does not exist on 18.04/20.04** (it is jammy's package); apt exit 100 moves to the JP4/default paths | Breaks two working paths to fix one |
| (c) remove the line | YES (no step) | Probably — on old bases line 4's `libssl-dev` already depends on the matching `libssl1.1`, so the runtime is present anyway | "Probably" is not proof: a prebuilt-era artifact dependency on an explicitly-installed 1.1 runtime cannot be ruled out from text alone, and Req 3.2 explicitly forbids breaking JP4/20.04 |

Option (a3) is the only candidate that provably preserves old-base behavior
(the exact same `apt-get install libssl1.1 -y` runs there), provably skips
the unresolvable request on jammy, adds nothing on jammy (justified, not
papered over: `libssl3` 3.0.2 is verified preinstalled in the exact base
image `public.ecr.aws/ubuntu/ubuntu:22.04`, is a hard dependency of line 4's
`libssl-dev`, and the edgemlsdk debs — the only plausible OpenSSL 1.1-era
consumers, installed at line 139 — now carry their own source-built OpenSSL
3.x via `openssl.deb` per `install_edgemlsdk.sh`), and future-proofs the
class (a hypothetical 24.04 base also lacks `libssl1.1`; an allowlist skips
it there too, unlike a `!= "22.04"` denylist). The allowlist gate visually
mirrors the file's existing conditional style at lines 73-75 while avoiding
its scoping trap.

**Decision 2 — the two flagged live-unverified sites: verified ¬C, no change
(closes Req 2.4).** Verified for this design against the real jammy index, in
a container on the exact base image the failing build uses
(`public.ecr.aws/ubuntu/ubuntu:22.04`, `apt-get update` then
`apt-cache policy`):

| Site | Packages | Jammy candidate (verified) |
|------|----------|----------------------------|
| line 72 | libexif12 | 0.6.24-1ubuntu0.22.04.1 ✓ |
| line 72 | libcurl4 | 7.81.0-1ubuntu1.25 ✓ |
| line 72 | libarchive13 | 3.6.0-1ubuntu1.8 ✓ |
| line 72 | gstreamer1.0-tools | 1.20.3-0ubuntu1.1 ✓ |
| line 72 | gstreamer1.0-libav | 1.20.3-0ubuntu1 ✓ |
| line 72 | ffmpeg | 7:4.4.2-0ubuntu0.22.04.1 ✓ |
| lines 127-133 | build-essential | 12.9ubuntu3 ✓ |
| lines 127-133 | libgnutls28-dev | 3.7.3-4ubuntu1.9 ✓ |
| lines 127-133 | libuv1 | 1.43.0-1ubuntu0.1 ✓ |
| line 70 (control) | libssl1.1 | **no stanza — no candidate ✗ (the C(X) site)** |

Both flagged sites are confirmed ¬C; combined with bugfix.md's enumeration of
every other apt step (all ran green in live build `3d18ba88` before the
failure point, or were verified there), **line 70 is the only C(X) site and
the class is closed in one pass.** The verdict inventory is embedded in the
new test package so any future package addition to these steps must be
re-vetted (the test fails on unknown tokens).

**Decision 3 — install scripts: OUT OF SCOPE, untouched.**
`prereqs_install.sh` and `install_aravis.sh` request only verified
jammy-resolvable packages (and ran green in the live build);
`install_edgemlsdk.sh` has no apt installs (dpkg/pip only). All three are
pinned byte-for-byte by sha256 goldens in the new package.

**Decision 4 — the inert lines 73-75 conditional and its typo'd
`libavcodec-extra57i`: OUT OF SCOPE, untouched, behavior preserved.** It is
unreachable at OS=22.04 by its own guard and inert on every base by the ARG
scoping trap; bugfix.md explicitly scopes it out. The fix introduces no `ARG`
re-declaration, so its (non-)behavior is bit-preserved. The new package pins
the structural facts (`ARG OS` only before FROM; the conditional's exact
bytes inside the masked view) so any future change that would activate it is
caught deliberately, not accidentally.

**Decision 5 — no compensating install on jammy.** No `libssl3` install line
is added for 22.04: it is already guaranteed present twice over (base image +
line 4's `libssl-dev` dependency), and Req 2.2 is satisfied by demonstrating
the need is already met, keeping the diff to exactly one step. The live
verification build is the arbiter of any residual doubt — if anything on the
22.04 path genuinely needed OpenSSL 1.1 specifically, the build or its
artifacts would surface it, and that would be a design problem to solve
properly per bugfix.md, not paper over.

### Changes Required

**File**: `src/backend/Dockerfile`

**Location**: Line 70, between the `apt update -y` lines 69 and 71.

**Specific Changes**:

1. **Single-step replacement (the entire code fix)**:

   ```dockerfile
   # before (line 70)
   RUN apt-get install libssl1.1 -y
   # after — libssl1.1 (OpenSSL 1.1 runtime) exists only through focal; on
   # jammy the base already ships libssl3 and the edgemlsdk debs carry their
   # own OpenSSL 3.x (openssl.deb). Gate on the base's own /etc/os-release:
   # the OS build-arg is out of scope in RUN (declared only before FROM).
   RUN . /etc/os-release && if [ "$VERSION_ID" = "18.04" ] || [ "$VERSION_ID" = "20.04" ] ; then \
       apt-get install libssl1.1 -y; \
       fi
   ```

   Nothing else in the file changes. Line 69's `apt update -y` still
   precedes it, so the index is fresh when the guard fires on old bases.
   The comment block travels with the step (both are inside the single
   masked region, keeping the masked-golden mechanism simple). Later line
   numbers shift by the added lines (e.g. the CVE block moves from 127-133);
   all textual anchors in tests use content matching, not line numbers.

2. **Golden regeneration — security out-of-scope guard (sanctioned path)**:
   the full-file sha256 of `src/backend/Dockerfile` changes. Per
   `.kiro/steering/builds.md` and both siblings' precedent: run
   `sha256sum src/backend/Dockerfile` on the fixed tree and update **just
   that one entry** in
   `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
   (currently `40f7c9e0…741b87`); re-run
   `test_preservation_docker_out_of_scope.py` to green. The other three
   entries (compose, edgemlsdk Dockerfile, frontend Dockerfile) are
   untouched. Note: the security suite has **no masked-bytes golden** for
   the plain `src/backend/Dockerfile` (only backend jp5/jp6 and edgemlsdk
   jp5/jp6 masked goldens exist — verified by listing
   `test/backend-test/security/baselines/`), and
   `docker_baseline_default_refs.json` tracks no entry in this file — so the
   out-of-scope entry is the ONLY golden affected repo-wide (bugfix.md's
   enumeration, re-verified).

3. **Same-commit rule**: the Dockerfile edit, the golden regeneration, and
   the new test package ship in one commit, so the tree is self-consistent
   on either side of it (pure-git-revert rollback, matching both siblings).

4. **Untouched by design**: `src/frontend/Dockerfile`,
   `src/docker-compose.yaml`, `src/backend/Dockerfile.jp5`, `.jp6`,
   `.x86_64_nvidia`, the three install scripts, all other
   `src/backend/Dockerfile` lines, the security suite's masked goldens and
   default-refs baseline, all other out-of-scope entries, both sibling test
   packages, and all of `src/edgemlsdk/**`.

## Testing Strategy

### Validation Approach

Full Docker image builds cannot run in tests, so validation is layered
(mirroring the proven `edgemlsdk_cmake` / `edgemlsdk_pythondev` structure):

1. **Static/property tests** over the Dockerfile and install-script text — a
   new `test/backend-test/backend_jammy_pkgs/` package (exploration tests
   fail on the unfixed tree; observation-first preservation goldens captured
   pre-fix; Hypothesis property tests for the helpers). Import-light, runs
   under `pytest --noconftest` with
   `PYTHONPATH=src/backend:test/backend-test`, and parses files as TEXT only
   — no `docker`, `subprocess`, or shell-out anywhere in the package (the
   no-Docker-builds constraint, verified by inspection as in both siblings).
2. **Existing suites re-run**: the full docker security preservation suite
   and both sibling packages, against the fixed tree with the one sanctioned
   golden regeneration.
3. **Approval-gated operational verification**: commit+push gate, then a
   live AMD64 dedicated portal build that must reach `succeeded` **including
   artifact publication** — the user-mandated completion criterion shared by
   all three open specs in this chain.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix. Confirm or refute the root cause analysis. If we
refute, we re-hypothesize.

**Test Plan**: Write tests in
`test/backend-test/backend_jammy_pkgs/test_bug_condition_exploration.py`
that parse the AMD64 build path's apt install steps (logical RUN
reconstruction across backslash continuations in the Dockerfile; apt lines
in the three install scripts; token-boundary package extraction;
release-conditional reachability modeling) and assert the expected CORRECT
state — no jammy-retired package token in any AMD64-reachable apt step. Run
these tests on the UNFIXED tree to observe the failures and pin the
counterexample to exactly line 70.

**Test Cases**:
1. **No jammy-retired tokens in any AMD64-reachable apt step**: scan every
   apt/apt-get install step's package tokens in `src/backend/Dockerfile` +
   the three install scripts against `RETIRED_JAMMY_PACKAGES`
   (`{"libssl1.1"}`); assert empty intersection among 22.04-reachable steps
   (will FAIL on unfixed code — counterexample: line 70's unconditional
   `libssl1.1`)
2. **Fixed-step exact form**: assert the libssl install step is the
   `/etc/os-release`-gated conditional with allowlist exactly
   {"18.04", "20.04"} and body `apt-get install libssl1.1 -y` (will FAIL on
   unfixed code, which has the unconditional form)
3. **Counterexample inventory scoping**: assert the retired-token scan over
   the UNFIXED tree finds exactly ONE site (the line-70 step) — confirming
   the bugfix.md scan and that the fix scope is a single step (passes
   pre-fix as a scoping check; post-fix meaning: zero reachable sites)
4. **Class-closure verdict inventory (¬C)**: assert every package token
   requested by AMD64-reachable apt steps in the Dockerfile and scripts is a
   member of the design-verified jammy-resolvable inventory (the Decision 2
   table plus the bugfix.md-enumerated ¬C sites) — any unknown token fails
   the test until vetted (passes pre-fix for all sites except line 70's
   token, which test 1 already isolates; passes post-fix completely)
5. **ARG scoping trap pinned (structural)**: assert `ARG OS` appears only
   before `FROM` and no `ARG OS` re-declaration exists after `FROM` — the
   structural fact that forces the `/etc/os-release` gate and keeps lines
   73-75 inert (passes pre/post fix)
6. **Old-base allowlist reachability (Req 3.2)**: assert the fixed step's
   guard makes `libssl1.1` reachable when the base is 18.04 or 20.04 and
   unreachable when 22.04, under the reachability model (will FAIL pre-fix:
   the unconditional step is reachable on all three)
7. **Frontend/compose sanity (¬C)**: assert `src/frontend/Dockerfile`
   contains zero apt install steps (alpine/npm only) — anchors the scan
   boundary of Req 2.4 (passes pre/post fix)

**Expected Counterexamples**:
- Exactly one failing site: line 70's `libssl1.1` token in an unconditional
  (hence 22.04-reachable) step — matching the live evidence (apt exit 100 in
  job `3d18ba88` at backend step 24/63)
- Possible refutations: the line was already changed, additional
  jammy-retired sites exist in the AMD64 path, a second `ARG OS` declaration
  exists after FROM, or an install script contains an unvetted apt package —
  any of which sends us back to re-hypothesize

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
tree produces the expected behavior.

**Pseudocode:**

```
FOR ALL step IN aptInstallSteps(backendDockerfile_fixed ∪ installScripts) DO
  IF reachableOnBase(step, "22.04") THEN
    ASSERT tokens(step) ∩ RETIRED_JAMMY_PACKAGES = EMPTY          -- Req 2.1, 2.4
    ASSERT tokens(step) ⊆ VERIFIED_JAMMY_RESOLVABLE_INVENTORY     -- class closure
  END IF
END FOR

libsslStep := theLibsslInstallStep(backendDockerfile_fixed)
ASSERT guard(libsslStep) = osReleaseAllowlist({"18.04", "20.04"})  -- Req 2.2
ASSERT body(libsslStep) = "apt-get install libssl1.1 -y"
ASSERT NOT reachableOnBase(libsslStep, "22.04")                    -- Req 2.3
ASSERT reachableOnBase(libsslStep, "18.04")
ASSERT reachableOnBase(libsslStep, "20.04")                        -- Req 3.2

-- jammy OpenSSL-runtime need already met without the step (Req 2.2, documented
-- facts asserted as text anchors):
ASSERT "libssl-dev" IN tokens(line4Step)     -- → depends on libssl3 on jammy
ASSERT "openssl.deb" IN dpkgInstalls(install_edgemlsdk.sh)

-- token-boundary discipline (scans must not substring-match):
ASSERT classify("libssl-dev") = NOT_RETIRED
ASSERT classify("libssl1.1") = RETIRED
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed files produce the same result as the original files.

**Pseudocode:**

```
ASSERT maskLibsslInstallStep(backendDockerfile_fixed)
     = maskLibsslInstallStep(backendDockerfile_original)
FOR ALL file IN [src/frontend/Dockerfile, src/docker-compose.yaml,
                 src/backend/Dockerfile.jp5, src/backend/Dockerfile.jp6,
                 src/backend/Dockerfile.x86_64_nvidia,
                 prereqs_install.sh, install_aravis.sh,
                 install_edgemlsdk.sh] DO
  ASSERT sha256(file_fixed) = sha256(file_original)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because:
- It generates many test cases automatically across the input domain (here:
  synthetic Dockerfile line sequences for the masking helper, package-token
  lists for the retired-set classifier, synthetic guarded/unguarded apt
  steps for the reachability model)
- It catches edge cases that manual unit tests might miss (e.g. the
  `libssl1.1` / `libssl-dev` token-boundary discipline, backslash-continued
  apt lines, flags adjacent to package tokens, allowlist vs denylist guard
  semantics)
- It provides strong guarantees that behavior is unchanged for all non-buggy
  inputs

**Test Plan**: Observe the UNFIXED tree first — capture goldens via a
capture-on-absent helper mirroring the `edgemlsdk_pythondev` pattern into
`test/backend-test/backend_jammy_pkgs/baselines/`:
`backend_Dockerfile_libssl_masked.txt` (the backend Dockerfile with ONLY the
libssl install step masked) and full-file sha256 goldens for the eight
untouched sibling files listed above. Goldens are FROZEN after capture —
never rebaselined by this spec. After the fix, the same tests assert the
masked view and all eight sha256es are byte-for-byte identical — proving
exactly one step changed and every sibling file is untouched.

**Test Cases**:
1. **Non-libssl bytes of the backend Dockerfile**: capture masked view on
   the unfixed tree; assert identical after fix (this view contains the
   Python 3.11 build, the awscrt workaround, lines 69/71/72, the inert
   73-75 conditional, the CVE block, and all COPY/script lines — all thereby
   proven verbatim)
2. **Frontend/compose/jp5/jp6/x86_64_nvidia untouched**: full-file sha256
   goldens identical pre/post fix (Req 3.3 enforced mechanically; jp5/jp6
   also independently enforced by the security suite's backend masked
   goldens)
3. **Install scripts untouched**: full-file sha256 goldens for the three
   scripts identical pre/post fix (Decision 3 enforced mechanically)
4. **Mask exactness**: the masked view differs from the raw file by exactly
   the one target step (pre-fix: one physical line; post-fix: the
   comment+conditional block), with the step count and content asserted —
   the mask cannot hide collateral edits

### Unit Tests

- Apt-step parser: logical RUN reconstruction across backslash
  continuations; package-token extraction excludes flags (`-y`,
  `--no-install-recommends`, `--only-upgrade`) and shell operators; both
  `apt` and `apt-get` forms recognized; dpkg/pip lines never classify as apt
  installs
- Reachability model: unconditional steps reachable on every base; the
  `/etc/os-release` allowlist guard reachable exactly on its listed
  releases; the lines 73-75 `$OS` guard modeled as 18.04-only (hence not
  22.04-reachable) with its inertness documented
- Retired-set classifier: token-boundary matching (`libssl1.1` flagged;
  `libssl-dev`, `libssl3`, `zlib1g-dev` not; adversarial near-misses like
  `libssl1.1-foo` not flagged as the exact token)
- Fixed-step assertions: exact text of the fixed conditional; its position
  between the two `apt update -y` lines; `libssl1.1` appears in the file
  only inside the guarded step
- Structural pins: `ARG OS` only before FROM; frontend Dockerfile has zero
  apt steps; `install_edgemlsdk.sh` installs `openssl.deb` via dpkg

### Property-Based Tests

- **Retired-token classifier property (Property 1)**: Hypothesis-generated
  package-name tokens (including adversarial prefixes/suffixes around
  `libssl1.1` and `libssl-dev`) — the classifier flags a token iff it is
  exactly a member of the retired set (token-boundary discipline)
- **Masking preservation property (Property 2)**: for generated Dockerfile
  line sequences containing zero or more marked target steps, the masking
  helper removes exactly the target step(s) and nothing else (mirrors the
  `edgemlsdk_pythondev` masking-helper property pattern)
- **Apt-line tokenization property (Properties 1-2)**: for generated apt
  install lines with random flag/package orderings and backslash
  continuations, tokenization is total and flags never classify as packages
- **Reachability model property (Property 1)**: for generated release
  allowlists and base versions, a guarded step is reachable iff the base is
  in the allowlist; an unconditional step is always reachable — so the
  fixed step is 22.04-unreachable and 18.04/20.04-reachable by construction

### Integration Tests

Automated integration is limited by the no-Docker-builds constraint; the
existing suites serve as the in-repo integration layer, and the live build is
the true integration test:

- Re-run the full docker security preservation suite
  (`test/backend-test/security/preservation/`, `--noconftest`) against the
  fixed tree: out-of-scope guard green with the single updated
  `src/backend/Dockerfile` hash; backend and edgemlsdk jp5/jp6 masked
  goldens bit-identical; default-refs guard bit-identical; mechanisms intact
  (Req 2.5, 3.4)
- Re-run both sibling packages (`test/backend-test/edgemlsdk_cmake/`,
  `test/backend-test/edgemlsdk_pythondev/`, `--noconftest`) against the
  fixed tree: all green, all goldens bit-identical — `src/edgemlsdk/**` is
  untouched (Req 3.5)
- Pre-build guard run per `.kiro/steering/builds.md` (out-of-scope guard +
  secrets guard) before dispatching the verification build

### Gated Live Verification (User-Mandated Completion Criterion)

Per bugfix.md: **the spec is complete only when an actual portal build
reaches `succeeded` including artifact publication.** Local/static validation
alone does NOT complete this spec. Two separately approval-gated steps, same
shape as both siblings' tasks 6-7:

1. **Gate 1 — commit + push**: builds sync from origin, so the fix is
   invisible to build servers until pushed. Target branch:
   `feature/workflow-triggers` (the user's standing branch decision from the
   sibling chain, where the failing evidence job's source_ref already
   points). Explicit user approval required before pushing.
2. **Gate 2 — live build**: exactly ONE AMD64 **dedicated** build on the
   existing X86 build server (the same shape as evidence job `3d18ba88`),
   source_ref `feature/workflow-triggers`, dispatched only after separate
   explicit user approval, with the full steering preflight first (no
   concurrent build, no preservation-tracked drift, guard tests green,
   fleet/instance health, one-at-a-time).
3. **Monitoring**: track via the Build Log API / CloudWatch
   `/dda/portal-builds`. Confirm the backend build proceeds past the former
   step 24/63 with no `libssl1.1` resolution failure (the guarded step skips
   on jammy), that line 72's six-package install and the CVE block install
   run green (closing the flagged ¬C verdicts live), and that both siblings'
   fixed steps still log clean (CMake 3.31.6; `python-dev-is-python3`).
4. **Success criterion**: the job reaches `succeeded` **including artifact
   publication**. A build that fails later than the backend libssl step is
   progress evidence, not completion.
5. **New-failure handling**: any follow-on failure past the fixed step is
   new evidence outside this spec's fix scope — record it in this spec's
   verification notes, route it to a follow-on spec (as this spec was itself
   routed from `edgemlsdk-python-dev-ubuntu2204`), and keep this spec open.
6. **Shared completion — THREE specs**: `edgemlsdk-cmake-pin-failure` and
   `edgemlsdk-python-dev-ubuntu2204` both remain open on the same criterion;
   a single `succeeded` AMD64 build with artifact publication satisfies all
   three specs' completion criteria simultaneously.

## Rollback Considerations

The fix is a **pure git revert**:

- All changes are text edits: one Dockerfile step, one JSON golden entry,
  plus the new test package under `test/backend-test/backend_jammy_pkgs/`.
  No schema, data, or infrastructure migration. Reverting the fix commit
  restores the pre-fix Dockerfile AND the pre-fix golden atomically, so the
  preservation suites stay consistent on either side of the revert.
- No runtime state depends on the change: images are rebuilt from the
  Dockerfile on each portal build; no deployed artifact embeds the fix until
  a build succeeds and publishes.
- If the live build surfaces a genuine OpenSSL 1.1 dependency on the 22.04
  path (contradicting Decision 5's analysis), the fallback is NOT to paper
  over it with a compat shim: per bugfix.md that is a design problem —
  identify the consuming artifact, and solve it properly (most likely in the
  artifact's own build, as the edgemlsdk image already did with its
  source-built OpenSSL 3.x) — a deliberate scope expansion requiring user
  agreement before implementation.
