# edgemlsdk python-dev on Ubuntu 22.04 Bugfix Design

## Overview

AMD64 edgemlsdk Docker image builds die at `src/edgemlsdk/Dockerfile` line 286
(`RUN apt-get install python-dev -y`, Docker build step 61/83). The AMD64
build passes the host's Ubuntu release as the `OS` build-arg, and the
dedicated X86 build server runs Ubuntu 22.04, so the image base is
`public.ecr.aws/ubuntu/ubuntu:22.04` (jammy) — where the retired transitional
package `python-dev` has no installation candidate ("replaced by: python2-dev
python2 python-dev-is-python3"), apt exits 100, and the image build and the
portal build job fail. Verified live on portal build job
`08a1e2bd-45f9-4521-ac4a-b41b52222e2e` (AMD64, dedicated, source_ref
`feature/workflow-triggers`, commit `63ecb99`, settled `failed` /
`BUILD_FAILED` on 2026-08-09, ~13m35s in) — full evidence in
`.kiro/specs/edgemlsdk-cmake-pin-failure/verification-notes.md` (Task 7).

This is a pre-existing latent defect unmasked by the sibling spec
`edgemlsdk-cmake-pin-failure`: builds previously died ~49 Dockerfile steps
earlier at the CMake install step; that spec's fix held (the same job logs
`cmake version 3.31.6` at the fixed step), letting the build reach line 286
for the first time.

**The fix**: replace the single token `python-dev` with
**`python-dev-is-python3`** on line 286 — the modern transitional package that
apt itself names as the replacement. It pulls in `python3-dev` (the same
dev-headers package the proven `Dockerfile.jp6` anchor installs on its own
Ubuntu 22.04-generation base) AND `python-is-python3` (which provides
`/usr/bin/python`), preserving a critical downstream precondition analyzed in
Fix Implementation below. `Dockerfile.jp5`'s `python-dev` token is left
untouched (resolves on its pinned focal base; changing it adds risk and golden
churn for zero benefit).

Because full Docker image builds cannot run in automated tests (same
constraint as the sibling spec), fix and preservation checking are validated
by **static assertions and property tests over the Dockerfile text** in a new
`test/backend-test/edgemlsdk_pythondev/` package mirroring the established
`edgemlsdk_cmake` pattern, plus sanctioned regeneration of the two goldens
that embed the changed line (the `src/edgemlsdk/Dockerfile` sha256 entry in
the security out-of-scope guard, and the prior spec's CMake-masked golden).
Per the user-mandated completion criterion in bugfix.md: **the spec is not
complete until an actual portal build reaches `succeeded` including artifact
publication** — an approval-gated operational verification phase (commit+push
gate, then live-build gate, each a separate explicit approval) follows local
validation.

## Glossary

- **Bug_Condition (C)**: An apt install step X in `src/edgemlsdk/Dockerfile`
  requests a retired transitional Python package (concretely: `python-dev` at
  line 286) that has no installation candidate on the file's effective Ubuntu
  base (22.04 on the AMD64 build servers), so apt exits 100 and the image
  build fails.
- **Property (P)**: Every package requested by the step has an installation
  candidate on the current base (the step succeeds, no apt exit 100), and the
  Triton/edgemlsdk build's Python tooling needs remain satisfied (python3 dev
  headers present, consistent with the JP6 anchor; the downstream
  `/usr/bin/python` precondition preserved).
- **Preservation**: Every other line of `src/edgemlsdk/Dockerfile`
  (byte-for-byte — including the pinned upstream release-binary CMake block
  from spec `edgemlsdk-cmake-pin-failure` and the neighboring
  `rapidjson-dev libre2-dev` install), all of `Dockerfile.jp5` and
  `Dockerfile.jp6` (byte-for-byte untouched files), and the existing
  security-baseline mechanisms.
- **Retired transitional Python package**: A pre-Python-3-transition apt
  package name (`python-dev`, `python`, `python-pip`, ...) that Ubuntu retired
  during the Python 2 sunset; on jammy these names have no installation
  candidate and apt suggests modern replacements.
- **`python-dev-is-python3`**: The jammy/focal transitional package that
  formally replaces `python-dev`; Depends: `python3-dev`, `python-is-python3`.
  `python-is-python3` provides the `/usr/bin/python` → `python3` symlink.
- **JP6 anchor**: `src/edgemlsdk/Dockerfile.jp6` (l4t-jetpack r36.3.0, Ubuntu
  22.04 generation, proven green) installs `rapidjson-dev libre2-dev
  python3-dev` in its system-packages block — the in-repo proof of which
  Python dev-headers package the Triton dependency section needs on a jammy
  base.
- **Out-of-scope sha256 guard**:
  `test/backend-test/security/preservation/test_preservation_docker_out_of_scope.py`,
  which pins the full-file sha256 of `src/edgemlsdk/Dockerfile` in
  `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
  (current entry: `5fe6186d…a7e355`).
- **Prior spec's CMake-masked golden**:
  `test/backend-test/edgemlsdk_cmake/baselines/edgemlsdk_Dockerfile_cmake_masked.txt`
  — a view of `src/edgemlsdk/Dockerfile` with only the CMake install block
  masked. It embeds line 286 (`RUN apt-get install python-dev -y`) verbatim,
  so this fix changes it; regeneration goes through that package's
  capture-on-absent path (`_cmake_preservation_support.capture_or_assert_text`).
- **Capture-on-absent / observation-first**: The golden methodology used by
  `edgemlsdk_cmake` and the security suite — a golden is captured from the
  UNFIXED tree on first run and asserted byte-for-byte thereafter.
- **Token-boundary matching**: `python-dev` is a proper prefix of
  `python-dev-is-python3`; all scans in this spec MUST match whole
  package-name tokens (split on whitespace/backslash-continuations), never
  substrings, or the fixed line would false-positive as still buggy.

## Bug Details

### Bug Condition

The bug manifests when the AMD64 edgemlsdk image build (base
`ubuntu:${OS}` with `OS=22.04` from the build host) reaches line 286. The
step requests `python-dev`, a transitional package retired on jammy; apt
reports "Package 'python-dev' has no installation candidate ... replaced
by: python2-dev python2 python-dev-is-python3" and exits with code 100,
killing the image build before any application code is copied in and before
any publish step.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type AptInstallStep
         (an apt-get install step in src/edgemlsdk/Dockerfile, identified by
          its file, line, and requested package tokens)
  OUTPUT: boolean

  RETURN input.file = src/edgemlsdk/Dockerfile
         AND EXISTS pkg IN input.requestedPackages :
               pkg IN RETIRED_TRANSITIONAL_PYTHON_PACKAGES   -- {"python-dev", ...}
               AND pkg has no installation candidate on the file's
                   effective Ubuntu base (22.04 on the AMD64 build servers)
         -- concretely today: line 286, pkg = "python-dev"
END FUNCTION
```

Non-buggy inputs ¬C(X) are every other line of `src/edgemlsdk/Dockerfile`
(including the CMake install block fixed by the prior spec and the
`rapidjson-dev libre2-dev` install directly above line 286), all of
`Dockerfile.jp5` and `Dockerfile.jp6` (whose `python-dev` occurrence /
`python3-dev` usage resolve on their pinned bases), and all other build
steps. Per the bugfix.md repo scan, line 286 is the only occurrence of a
retired transitional Python package in this file's apt installs.

### Examples

- **AMD64 live failure** (Req 1.1, 1.2): job `08a1e2bd` reached Docker step
  61/83 and logged `E: Package 'python-dev' has no installation candidate`,
  `exit code: 100`, `ERROR: edgemlsdk Docker build failed`; the job settled
  `BUILD_FAILED` ~13m35s in, no publish reached. Expected: the step installs
  a resolvable package set and the build proceeds.
- **Same section, neighboring step succeeds** (¬C anchor): step 60/83
  (`apt-get update && apt-get install rapidjson-dev libre2-dev -y`) succeeds
  on the same base — the defect is the retired package name, not the apt
  section.
- **JP6 on the same Ubuntu generation** (¬C anchor, Req 3.3):
  `Dockerfile.jp6` line 28 requests `rapidjson-dev libre2-dev python3-dev`
  and builds green — proving `python3-dev` is the resolvable dev-headers
  package for this dependency group on a 22.04-generation base.
- **JP5 on its pinned focal base** (¬C, Req 3.2): `Dockerfile.jp5` line 28
  includes the `python-dev` token inside its single system-packages block and
  that layer builds successfully on the digest-pinned `l4t-jetpack:r35.4.1`
  base (local logs `.gdk_build_jp5.log` / `.gdk_full_jp5.log`) — same token,
  no bug condition, because the base resolves it.
- **Edge case — dormant 18.04 branch**: with `OS=18.04` (bionic base),
  `python-dev` resolves (Python 2 dev headers) and the bug condition does not
  hold; no current build host runs 18.04 (see Fix Implementation, Decision 3).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Every `src/edgemlsdk/Dockerfile` line other than line 286 executes
  byte-for-byte unchanged — including the pinned upstream release-binary
  CMake install block from spec `edgemlsdk-cmake-pin-failure`, the Python
  3.11 source build, the `rapidjson-dev libre2-dev` install at the step
  directly above, and the later `rm /usr/bin/python && ln -s ...` step
  (Req 3.1).
- `src/edgemlsdk/Dockerfile.jp5` remains byte-for-byte untouched; its single
  system-packages apt block continues to resolve on its pinned focal base as
  it does today (Req 3.2, see Design Decision 2).
- `src/edgemlsdk/Dockerfile.jp6` remains byte-for-byte untouched, including
  its existing `python3-dev` install (Req 3.3).
- The docker security-baseline preservation mechanisms — masked-bytes goldens
  (jp5/jp6), the out-of-scope sha256 guard, byte-for-byte comparison
  semantics — continue to be enforced, against the one regenerated
  out-of-scope entry (Req 3.4).
- The prior spec's `test/backend-test/edgemlsdk_cmake/` package continues to
  pass against the fixed tree: its CMake-focused assertions are unaffected,
  and its one golden embedding line 286 is updated only through that
  package's sanctioned capture-on-absent regeneration path (Req 3.5).

**Scope:**

All inputs that do NOT involve line 286's retired `python-dev` request are
completely unaffected by this fix. This includes:

- All other apt install steps of `src/edgemlsdk/Dockerfile` (they resolve
  today on the 22.04 base up to step 61, per the live build log)
- The entire `Dockerfile.jp5` and `Dockerfile.jp6` files
- All shell scripts, compose files, and requirements files (bugfix.md repo
  scan: no other retired transitional Python package install sites)
- The security-baseline suite's mechanisms and all its non-regenerated
  goldens

The actual expected correct behavior is defined in the Correctness Properties
section (Property 1).

## Hypothesized Root Cause

The root cause is externally confirmed by the live build log, not merely
hypothesized — this is Ubuntu package-name retirement, not a logic bug in our
code:

1. **Retired transitional package name**: `python-dev` was the Python 2 era
   dev-headers metapackage. Ubuntu retired it during the Python 2 sunset;
   on jammy (22.04) it has no installation candidate and apt names the
   replacements (`python2-dev python2 python-dev-is-python3`) and exits 100.
   Confirmed verbatim in the job `08a1e2bd` CloudWatch log.

2. **Environment drift unmasked the line**: the `OS` build-arg tracks the
   build host's Ubuntu release. When the Dockerfile was written, hosts ran
   18.04/20.04-era bases where `python-dev` resolved. The current dedicated
   X86 server runs 22.04. The line was additionally shadowed by the CMake
   failure ~49 steps earlier until spec `edgemlsdk-cmake-pin-failure` fixed
   that step.

3. **The package's actual role in this file**: the line sits in the Triton
   dependency section (upstream Triton docs list `python3-dev rapidjson-dev`
   as build deps; the line appears to be a legacy transcription from the
   Python 2 era). The build's Python interpreter and headers actually used by
   Triton are the Python 3.11 built from source earlier in the file
   (`/usr/local/include/python3.11`, passed explicitly to `build.py`). The
   apt package's surviving *load-bearing* contribution on old bases was
   side-effectual: `python-dev` → `python` (Python 2) → provides
   `/usr/bin/python`, which the later
   `RUN apt-get install libnuma-dev -y && rm /usr/bin/python && ln -s ...`
   step deletes **without `-f`** — so `rm` fails with exit 1 if
   `/usr/bin/python` does not exist. Any fix that stops providing
   `/usr/bin/python` merely relocates the build failure to that later,
   preserved-byte-for-byte step.

4. **Why JP6 didn't need this**: `Dockerfile.jp6` installs `python3-dev`
   directly AND its own python-symlink step uses `rm -f` — it never depended
   on an apt package providing `/usr/bin/python`. The AMD64 Dockerfile's
   `rm` (no `-f`) is the structural difference that constrains the
   replacement choice here.

If the exploratory static tests refute any of this (e.g. the line has already
been changed, or another retired-package site exists), we re-hypothesize
before fixing.

## Correctness Properties

Property 1: Bug Condition - Line 286 Resolves on the 22.04 Base

_For any_ apt install step in `src/edgemlsdk/Dockerfile` where the bug
condition holds (isBugCondition returns true — today, exactly line 286's
`python-dev` request), the fixed file SHALL request only package names with
an installation candidate on the Ubuntu 22.04 base: the step becomes
`RUN apt-get install python-dev-is-python3 -y`; no retired transitional
Python package token (`python-dev` as a whole token, `python`, `python-pip`)
remains in any apt install step of the file; the replacement transitively
provides python3 dev headers (`python3-dev`, matching the JP6 anchor) and the
`/usr/bin/python` path (`python-is-python3`) required by the downstream
`rm /usr/bin/python` step — so the Triton/edgemlsdk build's Python tooling
needs remain satisfied and the build proceeds past step 61/83.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - All Other Lines and Sibling Dockerfiles Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns
false), the fixed files SHALL produce the same result as the original files:
every line of `src/edgemlsdk/Dockerfile` other than line 286 is byte-for-byte
identical to its pre-fix state (asserted by diff-scoping: masking out only
the `python-dev` install line and comparing the remainder against a golden
captured on the unfixed tree — the masked view thereby also proving the
prior spec's CMake block, the Python 3.11 source build, the neighboring
`rapidjson-dev libre2-dev` install, and the `rm /usr/bin/python` step
survive verbatim), and `src/edgemlsdk/Dockerfile.jp5` and
`src/edgemlsdk/Dockerfile.jp6` are byte-for-byte identical to their pre-fix
states (full-file sha256 goldens), preserving JP5's working focal-base
resolution and JP6's existing `python3-dev` install.

**Validates: Requirements 3.1, 3.2, 3.3**

Property 3: Baseline Regeneration - Goldens Track the Fixed Tree, Mechanisms Intact

_For any_ golden affected by the fix (the `src/edgemlsdk/Dockerfile` sha256
entry in `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`;
the prior spec's
`test/backend-test/edgemlsdk_cmake/baselines/edgemlsdk_Dockerfile_cmake_masked.txt`,
which masks only the CMake block and therefore embeds line 286), the golden
SHALL be regenerated through its mechanism's sanctioned capture path in the
same commit as the fix, so both preservation suites pass against the fixed
tree — while every golden NOT embedding the changed line SHALL remain
bit-identical (security masked goldens
`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` and
`docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt`; the `edgemlsdk_cmake`
package's `edgemlsdk_Dockerfile.jp5_cmake_masked.txt`,
`edgemlsdk_Dockerfile.jp6.sha256.txt`, and
`machine_setup.sh_install_cmake_masked.txt`), and the enforcing mechanisms
(masked-bytes comparison, out-of-scope hash guard, capture-on-absent
semantics) continue to run unchanged.

**Validates: Requirements 2.4, 3.4, 3.5**

### Properties Summary Table

| # | Property | Kind | Validation approach |
|---|----------|------|---------------------|
| 1 | Line 286 requests only jammy-resolvable packages; replacement is `python-dev-is-python3`; python3 dev headers + `/usr/bin/python` provided | Fix check | Static assertions over the fixed Dockerfile text: token-boundary scan proving zero retired transitional Python package tokens in any apt install step; exact fixed-line assertion; replacement-package dependency contract documented and asserted as the requested token |
| 2 | All other `Dockerfile` lines and both sibling Dockerfiles unchanged | Preservation | Diff-scoped goldens captured on the UNFIXED tree: python-dev-line-masked view of `src/edgemlsdk/Dockerfile`; full-file sha256 of `Dockerfile.jp5` and `Dockerfile.jp6`; compared byte-for-byte after the fix |
| 3 | Exactly two goldens regenerated via sanctioned paths; all others bit-identical; mechanisms intact | Preservation | Re-run the full docker security preservation suite and the `edgemlsdk_cmake` package against the fixed tree after sanctioned regeneration; assert non-affected goldens are bit-identical pre/post fix |

## Fix Implementation

### Design Decisions

**Decision 1 — replacement choice for line 286: `python-dev-is-python3`
(option b), not drop (a), not bare `python3-dev` (c).**

Three candidate fixes were evaluated against the file's actual downstream
dependencies:

| Option | apt resolves on 22.04? | python3 headers (JP6 anchor)? | Provides `/usr/bin/python`? | Later `rm /usr/bin/python` (no `-f`) survives? |
|--------|------------------------|-------------------------------|-----------------------------|------------------------------------------------|
| (a) drop the line | n/a (no step) | via source-built 3.11 only | **NO** | **FAILS** — rm exits 1, build dies at the libnuma-dev step |
| (b) `python-dev-is-python3` | YES | YES (Depends: python3-dev) | YES (Depends: python-is-python3) | YES |
| (c) `python3-dev` | YES | YES | **NO** | **FAILS** — same relocation of the failure |

The decisive constraint is the later preserved step
`RUN apt-get install libnuma-dev -y && rm /usr/bin/python && ln -s
/usr/bin/python3.11 /usr/bin/python`: `rm` without `-f` requires
`/usr/bin/python` to exist. On the unfixed old bases, `python-dev` satisfied
that via its Python 2 dependency chain; on the 22.04 base, nothing else in
the file creates `/usr/bin/python` before that step. Options (a) and (c)
would fix step 61 only to fail at that later step — which Req 3.1 requires
byte-for-byte unchanged. Option (b) is apt's own named replacement, is a
single-token diff, delivers the JP6 anchor's `python3-dev` transitively, and
uniquely preserves the `/usr/bin/python` precondition. (JP6 itself can use
bare `python3-dev` because its symlink step uses `rm -f` — a structural
difference, not an inconsistency.) The resulting `/usr/bin/python` →
`/usr/bin/python3.11` link is dangling on 22.04 exactly as it was on the
historical bases (python3.11 lives in `/usr/local/bin`); that pre-existing
behavior is preserved, not worsened, and the live verification build is the
arbiter of any residual doubt.

**Decision 2 — `Dockerfile.jp5`'s `python-dev` token: OUT OF SCOPE,
untouched.** Its base is digest-pinned to `l4t-jetpack:r35.4.1` (focal),
where the token resolves — local JP5 build logs show the layer building
successfully. Changing it would add risk to a working build path, force
regeneration of two more goldens
(`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` and the
`edgemlsdk_cmake` package's `edgemlsdk_Dockerfile.jp5_cmake_masked.txt`),
and deliver no behavior improvement. The bug condition C(X) is defined over
the *effective base*, and JP5's base resolves the token: ¬C. If JP5's base is
ever re-pinned to a jammy-generation l4t image, that migration owns the token
update. Recorded here as the design-phase decision bugfix.md deferred
(Req 3.2's "unless the design phase determines" clause: it does not).

**Decision 3 — dormant 18.04 branch: accept the narrowing, document it.**
`python-dev-is-python3` does not exist on bionic (18.04), so a hypothetical
`OS=18.04` build of this Dockerfile would fail at the fixed line. This is
accepted because: no current build host runs 18.04 (the fleet's dedicated
X86 server is 22.04, and `OS` is always the host's release); the 18.04 path
was already effectively dormant; and on 18.04 the *original* line resolved,
so the 18.04-only purge block later in the file (`apt-get remove --purge
python python3.6 ...`) interacted only with the old Python 2 chain — with
the fix, `OS != 18.04` on every real build and the purge block never runs.
No OS-conditional is added: it would complicate the file to protect a branch
no host can trigger.

### Changes Required

**File**: `src/edgemlsdk/Dockerfile`

**Location**: Line 286, in the Triton dependency section ("Install Triton
Server and it's dependencies"), immediately after the
`rapidjson-dev libre2-dev` install step.

**Specific Changes**:

1. **Single-token replacement (the entire code fix)**:

   ```dockerfile
   # before (line 286)
   RUN apt-get install python-dev -y
   # after
   RUN apt-get install python-dev-is-python3 -y
   ```

   Nothing else in the file changes. The `apt-get update` on the step above
   (285) still precedes it, so the package index is fresh. No comment block
   is added or removed (keeping the diff to exactly one line keeps both
   masked-golden mechanisms simple).

2. **Golden regeneration — security out-of-scope guard (sanctioned path)**:
   the full-file sha256 of `src/edgemlsdk/Dockerfile` changes. Per
   `.kiro/steering/builds.md` and the prior spec's precedent: run
   `sha256sum src/edgemlsdk/Dockerfile` on the fixed tree and update **just
   that one entry** in
   `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
   (currently `5fe6186d…a7e355`); re-run
   `test_preservation_docker_out_of_scope.py` to green. All other entries
   untouched. Note: the security suite has **no masked-bytes golden** for
   `src/edgemlsdk/Dockerfile` (only jp5/jp6 masked goldens exist — verified
   by listing `test/backend-test/security/baselines/`), so the out-of-scope
   entry is the only security-suite artifact affected.

3. **Golden regeneration — prior spec's CMake-masked golden (sanctioned
   path)**:
   `test/backend-test/edgemlsdk_cmake/baselines/edgemlsdk_Dockerfile_cmake_masked.txt`
   masks only the CMake install block and therefore embeds line 286 verbatim
   (verified: the golden contains `RUN apt-get install python-dev -y`). After
   the fix its assertion fails by design. Regenerate through that package's
   capture-on-absent path: delete the golden, re-run
   `test/backend-test/edgemlsdk_cmake/test_preservation_baseline.py` (which
   re-captures from the fixed tree via
   `_cmake_preservation_support.capture_or_assert_text`), then re-run to
   assert green. The package's other goldens
   (`edgemlsdk_Dockerfile.jp5_cmake_masked.txt`,
   `edgemlsdk_Dockerfile.jp6.sha256.txt`,
   `machine_setup.sh_install_cmake_masked.txt`) embed no changed line and
   must be bit-identical pre/post fix — asserted, not regenerated.

4. **Same-commit rule**: the Dockerfile edit, both golden regenerations, and
   the new test package ship in one commit, so the tree is self-consistent on
   either side of it (pure-git-revert rollback, matching the prior spec).

5. **Untouched by design**: `Dockerfile.jp5`, `Dockerfile.jp6`, all other
   `src/edgemlsdk/Dockerfile` lines, `machine_setup.sh`, the security suite's
   masked goldens, all other out-of-scope entries. The gitignored vendored
   `src/backend/edgemlsdk/edgemlsdk/**` duplicates are build artifacts
   regenerated by `build-custom.sh`; no action (prior-spec precedent).

## Testing Strategy

### Validation Approach

Full Docker image builds cannot run in tests, so validation is layered
(mirroring the proven `edgemlsdk-cmake-pin-failure` structure):

1. **Static/property tests** over the Dockerfile text — a new
   `test/backend-test/edgemlsdk_pythondev/` package (exploration tests fail
   on the unfixed tree; observation-first preservation goldens captured
   pre-fix; Hypothesis property tests for the helpers). Import-light, runs
   under `pytest --noconftest` with `PYTHONPATH=src/backend:test/backend-test`,
   and parses files as TEXT only — no `docker`, `subprocess`, or shell-out
   anywhere in the package (the no-Docker-builds constraint, verified by
   inspection as in the prior spec).
2. **Existing suites re-run**: the full docker security preservation suite
   and the prior spec's `edgemlsdk_cmake` package, against the fixed tree
   with the two sanctioned golden regenerations.
3. **Approval-gated operational verification**: commit+push gate, then a
   live AMD64 dedicated portal build that must reach `succeeded` **including
   artifact publication** — the user-mandated completion criterion.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix. Confirm or refute the root cause analysis. If we
refute, we re-hypothesize.

**Test Plan**: Write tests in
`test/backend-test/edgemlsdk_pythondev/test_bug_condition_exploration.py`
that parse the Dockerfile's apt install steps (logical RUN instructions,
token-boundary package extraction) and assert the expected CORRECT state —
no retired transitional Python package tokens in any apt install step of
`src/edgemlsdk/Dockerfile`. Run these tests on the UNFIXED tree to observe
the failures and pin the counterexample to exactly line 286.

**Test Cases**:
1. **No retired tokens in any apt step**: scan every apt-get/apt install
   step's package tokens against the retired-set
   (`python-dev`, `python`, `python-pip`, `python-setuptools`); assert empty
   intersection (will FAIL on unfixed code — counterexample: line 286's
   `python-dev`)
2. **Line 286 exact form**: assert the Triton-section single-package install
   step is `RUN apt-get install python-dev-is-python3 -y` (will FAIL on
   unfixed code, which has `python-dev`)
3. **Counterexample inventory scoping**: assert the retired-token scan over
   the UNFIXED file finds exactly ONE site (line 286) — confirming the
   bugfix.md scan and that the fix scope is a single line (passes pre-fix as
   a scoping check; inverted meaning post-fix: zero sites)
4. **JP6 anchor sanity (¬C)**: `Dockerfile.jp6`'s system-packages block
   requests `python3-dev` (token-boundary) and no retired token (passes on
   unfixed code — anchors the target dev-headers package)
5. **JP5 out-of-scope sanity (¬C)**: `Dockerfile.jp5` contains the
   `python-dev` token in its single system-packages block (passes on unfixed
   code AND after the fix — documents Decision 2's untouched scope; this
   test asserts presence, guarding against accidental modification)
6. **Downstream precondition documented**: assert the
   `rm /usr/bin/python` step exists, uses no `-f`, and follows line 286 —
   pinning the structural fact that forces Decision 1 (passes pre/post fix)

**Expected Counterexamples**:
- Exactly one failing site: line 286's `python-dev` token — matching the
  live evidence (apt exit 100 in job `08a1e2bd` at step 61/83)
- Possible refutations: the line was already changed, additional retired
  Python package sites exist in the file, or the JP6 anchor does not use
  `python3-dev` — any of which sends us back to re-hypothesize

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
file produces the expected behavior.

**Pseudocode:**

```
FOR ALL step IN aptInstallSteps(Dockerfile_fixed) DO
  ASSERT tokens(step) ∩ RETIRED_TRANSITIONAL_PYTHON_PACKAGES = EMPTY
END FOR

tritonPythonStep := theSinglePackageInstallStepInTritonSection(Dockerfile_fixed)
ASSERT tritonPythonStep = "RUN apt-get install python-dev-is-python3 -y"
-- replacement contract (documented dependency facts asserted as tokens):
ASSERT "python-dev-is-python3" IN tokens(tritonPythonStep)   -- → python3-dev
                                                             -- → python-is-python3
                                                             --   (provides /usr/bin/python)

-- token-boundary discipline (Property 1 scans must not substring-match):
ASSERT NOT substringScanUsed  -- "python-dev" ∉ tokens, though it IS a
                              -- substring of the fixed line's token
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed files produce the same result as the original files.

**Pseudocode:**

```
FOR ALL file IN [src/edgemlsdk/Dockerfile] DO
  ASSERT maskPythonDevInstallLine(file_fixed) = maskPythonDevInstallLine(file_original)
END FOR
ASSERT sha256(Dockerfile.jp5_fixed) = sha256(Dockerfile.jp5_original)
ASSERT sha256(Dockerfile.jp6_fixed) = sha256(Dockerfile.jp6_original)
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because:
- It generates many test cases automatically across the input domain (here:
  synthetic Dockerfile line sequences for the masking helper, package-token
  lists for the retired-set classifier)
- It catches edge cases that manual unit tests might miss (e.g. the
  `python-dev` / `python-dev-is-python3` prefix trap, backslash-continued
  apt lines, tokens adjacent to `-y` flags)
- It provides strong guarantees that behavior is unchanged for all non-buggy
  inputs

**Test Plan**: Observe the UNFIXED tree first — capture goldens via a
capture-on-absent helper mirroring
`_cmake_preservation_support.capture_or_assert_text` into
`test/backend-test/edgemlsdk_pythondev/baselines/`:
`edgemlsdk_Dockerfile_pythondev_masked.txt` (the Dockerfile with ONLY the
retired-Python-package install line masked),
`edgemlsdk_Dockerfile.jp5.sha256.txt`, and
`edgemlsdk_Dockerfile.jp6.sha256.txt`. Goldens are FROZEN after capture —
never rebaselined by this spec. After the fix, the same tests assert the
masked view and both sha256es are byte-for-byte identical — proving exactly
one line changed and both sibling files are untouched.

**Test Cases**:
1. **Non-python-dev bytes of `Dockerfile`**: capture masked view on unfixed
   tree; assert identical after fix (this view contains the CMake block, the
   Python 3.11 build, the rapidjson/re2 step, and the `rm /usr/bin/python`
   step — all thereby proven verbatim)
2. **`Dockerfile.jp5` untouched**: full-file sha256 golden identical
   pre/post fix (Decision 2 enforced mechanically)
3. **`Dockerfile.jp6` untouched**: full-file sha256 golden identical
   pre/post fix (also independently enforced by the security suite's jp6
   masked golden and the `edgemlsdk_cmake` package's jp6 sha256 golden)
4. **Mask exactness**: the masked view differs from the raw file by exactly
   the one target line (count and content asserted) — the mask cannot hide
   collateral edits

### Unit Tests

- Apt-step parser: logical RUN reconstruction across backslash continuations;
  package-token extraction excludes flags (`-y`, `--fix-missing`) and shell
  operators; token-boundary matching (`python-dev` does NOT match
  `python-dev-is-python3`, `python3-dev` does NOT match the retired set)
- Fixed-line assertions: exact text of the fixed step; its position in the
  Triton dependency section (after the `rapidjson-dev libre2-dev` step)
- Negative sweeps over the fixed file: zero retired transitional Python
  package tokens in any apt install step; `python-dev-is-python3` appears
  exactly once
- Structural pins: `rm /usr/bin/python` (no `-f`) step present and ordered
  after the fixed line; JP6 anchor's `python3-dev` token present in
  `Dockerfile.jp6`

### Property-Based Tests

- **Retired-token classifier property (Property 1)**: Hypothesis-generated
  package-name tokens (including adversarial prefixes/suffixes like
  `python-dev-is-python3`, `libpython-dev-foo`, `python-devtools`) — the
  classifier flags a token iff it is exactly a member of the retired set
- **Masking preservation property (Property 2)**: for generated Dockerfile
  line sequences containing zero or more marked target lines, the masking
  helper removes exactly the target line(s) and nothing else (mirrors the
  `edgemlsdk_cmake` masking-helper property pattern)
- **Apt-line tokenization property (Properties 1–2)**: for generated apt
  install lines with random flag/package orderings and continuations,
  tokenization is total and flags never classify as packages

### Integration Tests

Automated integration is limited by the no-Docker-builds constraint; the
existing suites serve as the in-repo integration layer, and the live build is
the true integration test:

- Re-run the full docker security preservation suite
  (`test/backend-test/security/preservation/`, `--noconftest`) against the
  fixed tree: out-of-scope guard green with the single updated
  `src/edgemlsdk/Dockerfile` hash; jp5/jp6 masked goldens bit-identical;
  mechanisms intact (Req 2.4, 3.4). Expected shape: fully green apart from
  the two known pre-existing vendored-duplicate skips.
- Re-run the prior spec's package (`test/backend-test/edgemlsdk_cmake/`,
  `--noconftest`) against the fixed tree after the sanctioned regeneration of
  its one affected golden: all tests green, its other three goldens
  bit-identical (Req 3.5)
- Pre-build guard run per `.kiro/steering/builds.md` (out-of-scope guard +
  secrets guard) before dispatching the verification build

### Gated Live Verification (User-Mandated Completion Criterion)

Per bugfix.md: **the spec is complete only when an actual portal build
reaches `succeeded` including artifact publication.** Local/static validation
alone does NOT complete this spec. Two separately approval-gated steps, same
shape as the prior spec's tasks 6–7:

1. **Gate 1 — commit + push**: builds sync from origin, so the fix is
   invisible to build servers until pushed. Target branch:
   `feature/workflow-triggers` (the user's standing branch decision from the
   prior spec's task 6, where the failing evidence job's source_ref already
   points). Explicit user approval required before pushing.
2. **Gate 2 — live build**: exactly one AMD64 **dedicated** build on the
   existing X86 build server (the same shape as evidence job `08a1e2bd`),
   source_ref `feature/workflow-triggers`, dispatched only after separate
   explicit user approval, with the full steering preflight first (no
   concurrent build, no preservation-tracked drift, guard tests green,
   fleet/instance health, one-at-a-time).
3. **Monitoring**: track via the Build Log API / CloudWatch
   `/dda/portal-builds`. Confirm step 61/83 now logs a successful
   `python-dev-is-python3` install (no apt exit 100), and that the previously
   fixed CMake step still logs `cmake version 3.31.6`.
4. **Success criterion**: the job reaches `succeeded` **including artifact
   publication**. A build that fails later than step 61 is progress evidence,
   not completion.
5. **New-failure handling**: any follow-on failure past step 61 is new
   evidence outside this spec's fix scope — record it in this spec's
   verification notes, route it to a follow-on spec (as this spec was itself
   routed from the prior one), and keep this spec open.
6. **Shared completion**: the sibling spec `edgemlsdk-cmake-pin-failure`
   remains open on the same criterion; a `succeeded` AMD64 build with
   artifact publication satisfies both specs' completion criteria
   simultaneously.

## Rollback Considerations

The fix is a **pure git revert**:

- All changes are text edits: one Dockerfile line, one JSON golden entry, one
  regenerated masked-golden text file, plus the new test package under
  `test/backend-test/edgemlsdk_pythondev/`. No schema, data, or
  infrastructure migration. Reverting the fix commit restores the pre-fix
  Dockerfile AND the pre-fix goldens atomically, so both preservation suites
  stay consistent on either side of the revert.
- No runtime state depends on the change: images are rebuilt from the
  Dockerfile on each portal build; no deployed artifact embeds the fix until
  a build succeeds and publishes.
- If the live build surfaces a problem with `python-dev-is-python3` itself
  (e.g. an unexpected interaction between `python-is-python3`'s symlink and a
  later step), the fallback is option (c) `python3-dev` **plus** a
  minimally-scoped `-f` on the downstream `rm` — a deliberate scope expansion
  that would require revisiting Req 3.1 with the user before implementation.
