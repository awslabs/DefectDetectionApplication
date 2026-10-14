# edgemlsdk CMake Pin Failure Bugfix Design

## Overview

Every edgemlsdk Docker image build for AMD64 and JP5 dies at the CMake install
step because it resolves CMake through the Kitware apt repository
(`apt.kitware.com`): the pinned `cmake=3.21.3-0kitware1ubuntu{18,20}.04.1`
package versions are no longer served (apt exit 100), and an unpinned install
would yield the repo's current CMake 4.x, which breaks Triton's transitively
fetched dependencies (`cmake_minimum_required(VERSION < 3.5)` support was
removed in 4.0). Verified live on portal build job
`40b036fc-c379-4a70-b00e-e6d6b0d46ecb` (AMD64, 2026-08-08, `BUILD_FAILED`
exit 1 ~3 minutes in).

The fix pattern is already proven in-repo: `src/edgemlsdk/Dockerfile.jp6`
installs a pinned CMake 3.31.6 from the official Kitware **GitHub release
binary** — a self-contained installer script downloaded from
`github.com/Kitware/CMake/releases`, arch-selected via `uname -m`
(x86_64/aarch64), installed under `/usr/local`, verified in-build with
`cmake --version`, with Kitware apt residue cleanup retained in the later
Python step. This design replicates that pattern faithfully in:

1. `src/edgemlsdk/Dockerfile` — both the 18.04/bionic and 20.04/focal branches
   of its CMake `RUN` block (AMD64 builds)
2. `src/edgemlsdk/Dockerfile.jp5` — its single CMake `RUN` block (JP5/arm64)
3. `src/edgemlsdk/src/utilities/machine_setup.sh` — `install_cmake`'s
   18.04 Kitware-apt branch, adapted for a host script (not Docker)

Because full Docker image builds cannot run in tests, fix and preservation
checking are validated by **static assertions and property tests over the
Dockerfile/script text**, plus regeneration of the affected docker
security-baseline goldens through their sanctioned capture paths. Per the
user-mandated completion criterion in bugfix.md: **the spec is not complete
until an actual portal build succeeds and publishes** — an approval-gated
operational verification phase follows local validation.

## Glossary

- **Bug_Condition (C)**: A CMake installation step X (in an edgemlsdk
  Dockerfile or setup script) resolves CMake through the Kitware apt
  repository, making the outcome dependent on what that repo currently serves
  — dead 3.21.3 pin (apt exit 100) or unpinned 4.x (Triton-incompatible).
- **Property (P)**: The install step installs a pinned CMake 3.x
  (≥ 3.21, < 4.0) from the official Kitware GitHub release binary with no apt
  resolution of the `cmake` package, deterministically, with in-build version
  verification.
- **Preservation**: Every non-CMake-install line of the affected files, the
  entire already-fixed `Dockerfile.jp6`, and all other `machine_setup.sh`
  behavior must remain byte-for-byte / behaviorally unchanged.
- **JP6 pattern**: The proven `RUN` block in `src/edgemlsdk/Dockerfile.jp6`:
  `CMAKE_VER=3.31.6` → `uname -m` arch selection → `wget` the
  `cmake-${CMAKE_VER}-linux-${CM_ARCH}.sh` installer from GitHub releases →
  `sh /tmp/cmake.sh --skip-license --prefix=/usr/local` → cleanup →
  `cmake --version`.
- **Masked-bytes golden mechanism**: The docker security-baseline preservation
  tests (`test/backend-test/security/preservation/
  test_preservation_docker_masked_bytes.py`) that compare each in-scope
  Dockerfile's non-`FROM` / non-`ARG BASE_REGISTRY` lines byte-for-byte
  against goldens in `test/backend-test/security/baselines/`, via the
  `capture_or_assert_text` capture-on-absent primitive.
- **Out-of-scope sha256 guard**: `test_preservation_docker_out_of_scope.py`,
  which pins the full-file sha256 of `src/edgemlsdk/Dockerfile` (among others)
  in `docker_baseline_out_of_scope.json`.
- **install_cmake**: The function in
  `src/edgemlsdk/src/utilities/machine_setup.sh` that installs CMake on build
  hosts; its 18.04 branch adds the Kitware apt repo and installs an unpinned
  `cmake`.

## Bug Details

### Bug Condition

The bug manifests whenever an edgemlsdk build (or host setup) reaches a CMake
install step that resolves the `cmake` package through the Kitware apt
repository. The outcome then depends on the current contents of
`apt.kitware.com` rather than on anything pinned in this repo: the pinned
3.21.3 package versions are no longer published (apt exits 100 and the image
build dies before any project code is copied in), and the unpinned host-script
path installs whatever the repo currently serves (CMake 4.x, which Triton's
dependency tree cannot build with).

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type CMakeInstallStep
         (a CMake installation step in an edgemlsdk Dockerfile or setup script,
          identified by its file and text)
  OUTPUT: boolean

  RETURN input.file IN [src/edgemlsdk/Dockerfile (18.04 branch),
                        src/edgemlsdk/Dockerfile (20.04 branch),
                        src/edgemlsdk/Dockerfile.jp5,
                        src/edgemlsdk/src/utilities/machine_setup.sh:install_cmake]
         AND input resolves the `cmake` package via apt from apt.kitware.com
         AND (input pins a package version no longer served     -- apt exit 100
              OR input is unpinned                              -- yields 4.x)
END FUNCTION
```

Non-buggy inputs ¬C(X) are install steps that already use a pinned upstream
release binary (the JP6 pattern in `Dockerfile.jp6`) and every other build /
setup step in these files.

### Examples

- **AMD64, focal branch** (bugfix Req 1.1): `src/edgemlsdk/Dockerfile` runs
  `apt-get install cmake=3.21.3-0kitware1ubuntu20.04.1
  cmake-data=3.21.3-0kitware1ubuntu20.04.1 -y` against the Kitware repo.
  Expected: a CMake 3.x is installed. Actual: apt exit 100 ("version not
  found"), image build fails. Verified live in portal build job
  `40b036fc-c379-4a70-b00e-e6d6b0d46ecb` (~step 7, ~3 minutes in).
- **AMD64, bionic branch** (Req 1.2): same Dockerfile, 18.04 branch, pins
  `...0kitware1ubuntu18.04.1` — fails identically.
- **JP5** (Req 1.3): `src/edgemlsdk/Dockerfile.jp5` step 2 pins
  `cmake-data=3.21.3-0kitware1ubuntu20.04.1 cmake=3.21.3-0kitware1ubuntu20.04.1`
  — apt exit 100.
- **Host script, milder form** (Req 1.5): `machine_setup.sh install_cmake` on
  an 18.04 host adds the Kitware apt repo and runs `apt-get install cmake -y`
  **unpinned** — installs whatever Kitware currently serves (4.x where
  available, Triton-incompatible) or fails outright on retired distributions.
- **Edge case — already fixed (¬C)**: `src/edgemlsdk/Dockerfile.jp6` installs
  pinned 3.31.6 from the GitHub release binary; it does not depend on the
  Kitware apt repo and must not be touched.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- `src/edgemlsdk/Dockerfile.jp6` remains byte-for-byte unchanged, including
  its CMake 3.31.6 install block (Req 3.1).
- Every build step other than the CMake install in the affected files —
  base image lines, LLVM/clang setup, gcc-11 setup, OpenSSL build (18.04),
  Python 3.11 source build, the Kitware repo-residue cleanup lines in the
  Python step of `Dockerfile.jp5`, and all subsequent steps — executes exactly
  as before; only the CMake install lines change (Req 3.2).
- Triton and its transitively fetched dependencies continue to build against
  a CMake 3.x toolchain (≥ 3.21, < 4.0) (Req 3.3).
- The docker security-baseline masked-bytes golden mechanism (FROM-line
  masking semantics, byte-for-byte comparison of non-masked lines) continues
  to be enforced, against regenerated goldens (Req 3.4).
- `machine_setup.sh` continues to skip the CMake install when CMake is
  already present, and performs all its other setup functions
  (`install_powershell`, package installs, etc.) unchanged (Req 3.5).

**Scope:**

All inputs that do NOT involve the four identified CMake install steps are
completely unaffected by this fix. This includes:

- The JP6 Dockerfile and every JP6 build step
- All non-CMake lines/steps of `src/edgemlsdk/Dockerfile` and
  `Dockerfile.jp5`
- All non-`install_cmake` functions of `machine_setup.sh`, and
  `install_cmake`'s "already installed" skip path and its non-18.04
  (`check_and_install_package cmake` — Ubuntu archive, not Kitware) branch

The actual expected correct behavior is defined in the Correctness Properties
section (Property 1).

## Hypothesized Root Cause

The root cause is externally confirmed, not merely hypothesized — this is
repository drift at Kitware, not a logic bug in our code:

1. **Dead pinned apt package versions**: Kitware's apt repository no longer
   publishes the `3.21.3-0kitware1ubuntu{18,20}.04.1` package versions our
   Dockerfiles pin, so `apt-get install cmake=<that version>` exits 100.
   Confirmed live by the on-server gdk log for build job
   `40b036fc-c379-4a70-b00e-e6d6b0d46ecb` (recorded in
   `.kiro/specs/build-fleet-execution-failures/verification-notes.md`,
   Task 13).

2. **Kitware apt now serves CMake 4.x as current**: unpinning is not an
   option because 4.x removed `cmake_minimum_required(VERSION < 3.5)` support
   and breaks Triton's transitively fetched dependencies. Re-pinning to
   another apt 3.x fails with `cmake : Breaks: cmake-data (< 4.3)` dependency
   conflicts (documented in the Dockerfile.jp6 fix comment).

3. **Structural fragility**: any install path that resolves CMake through a
   third-party apt repo we do not control is non-deterministic over time.
   The deterministic alternative — a pinned, self-contained upstream release
   binary from `github.com/Kitware/CMake/releases` — is already proven by the
   working `Dockerfile.jp6` (and by the analogous PowerShell tarball install
   in `machine_setup.sh`'s `install_powershell`).

4. **Host-script variant**: `install_cmake`'s 18.04 branch has the same
   exposure in unpinned form; it predates the drift and was never migrated.

If the exploratory static tests refute any of this (e.g. an affected file has
already been changed), we re-hypothesize before fixing.

## Correctness Properties

Property 1: Bug Condition - Pinned Upstream Release-Binary CMake Install

_For any_ CMake install step where the bug condition holds (isBugCondition
returns true — the step resolves CMake via the Kitware apt repository), the
fixed file SHALL install a pinned CMake 3.x from the official Kitware GitHub
release binary instead: no `apt.kitware.com` CMake resolution remains anywhere
in the affected files; each CMake install step downloads
`github.com/Kitware/CMake/releases/download/v<ver>/cmake-<ver>-linux-<arch>.sh`
with `uname -m`-based arch selection (x86_64/aarch64) and installs via the
self-contained installer script; and each Dockerfile CMake step ends with an
in-build `cmake --version` verification.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Non-CMake Lines and JP6 Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns
false), the fixed files SHALL produce the same result as the original files:
`src/edgemlsdk/Dockerfile.jp6` is byte-for-byte identical to its pre-fix
state, every non-CMake-install line of `src/edgemlsdk/Dockerfile`,
`src/edgemlsdk/Dockerfile.jp5`, and `machine_setup.sh` is byte-for-byte
identical (asserted by diff-scoping: masking out only the CMake install
block/function lines and comparing the remainder against goldens captured on
the unfixed tree), and the docker security-baseline masked-bytes mechanism
remains intact with its goldens regenerated through the sanctioned capture
path.

**Validates: Requirements 3.1, 3.2, 3.4, 3.5**

Property 3: No-4.x - Pinned Version Range

_For any_ pinned CMake version string parsed from the CMake install steps of
the fixed files (including the untouched `Dockerfile.jp6`), the version v
SHALL satisfy 3.21 ≤ v < 4.0, preserving the Triton-compatible major/minor
range the original 3.21.3 pin provided and guaranteeing no 4.x can be
installed.

**Validates: Requirements 2.3, 3.3**

Property 4: Baseline Regeneration - Goldens Track the Fixed Tree

_For any_ docker security-baseline golden affected by the fix
(`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` for the changed
non-FROM lines of Dockerfile.jp5; the `src/edgemlsdk/Dockerfile` sha256 entry
in `docker_baseline_out_of_scope.json`), the golden SHALL be regenerated
through the baseline mechanism's sanctioned capture path so the preservation
suite passes against the fixed tree — while
`docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt` SHALL remain unchanged
(JP6 untouched).

**Validates: Requirements 2.5, 3.1, 3.4**

### Properties Summary Table

| # | Property | Kind | Validation approach |
|---|----------|------|---------------------|
| 1 | Pinned upstream release-binary install, no Kitware apt resolution, in-build `cmake --version` | Fix check | Static assertions over the fixed Dockerfile/script text: regex scan proving zero `apt.kitware.com` / `cmake=3.21.3-0kitware*` occurrences in the affected files; presence of the GitHub-release installer URL, arch selection, `--skip-license --prefix=/usr/local`, and `cmake --version` in each Dockerfile CMake step |
| 2 | Non-CMake lines and JP6 unchanged | Preservation | Diff-scoped goldens captured on the UNFIXED tree: full-file sha256 of `Dockerfile.jp6`; CMake-block-masked views of `Dockerfile`, `Dockerfile.jp5`, and `machine_setup.sh` (install_cmake body masked) compared byte-for-byte after the fix |
| 3 | Every pinned version v satisfies 3.21 ≤ v < 4.0 | Fix check (property test) | Parser extracts every `CMAKE_VER=<x.y.z>` (and any residual apt `cmake=` pin) from the affected files + Dockerfile.jp6; Hypothesis property test over the version-comparison helper plus a direct assertion on each parsed version |
| 4 | Security-baseline goldens regenerated, mechanism intact | Preservation | Re-run `test_preservation_docker_masked_bytes.py` (and the out-of-scope guard) against the fixed tree after sanctioned regeneration; assert the jp6 golden file is bit-identical to pre-fix |

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct, all four changes replicate the
JP6 pattern (read from the actual `Dockerfile.jp6` working-tree
implementation, mirrored faithfully). The pinned version is **3.31.6** in all
locations, matching JP6 exactly.

#### Change 1 — `src/edgemlsdk/Dockerfile` (AMD64, both OS branches)

**Location**: The single `RUN if [ "$OS" = "18.04" ]; then ... else ... fi`
CMake block (following the "Install the latest versions of CMake" comment).

Replace the entire if/else Kitware-apt block with one branch-free JP6-pattern
`RUN` block — the upstream installer is distro-agnostic, so the OS branching
is no longer needed for CMake:

```dockerfile
# Install pinned CMake 3.x from the official upstream release binary (see
# Dockerfile.jp6): the Kitware apt repo no longer serves the pinned 3.21.3
# packages and its current cmake is 4.x, which breaks Triton's transitively
# fetched dependencies. The upstream installer is self-contained (no apt
# resolution) and gives a clean, pinned 3.x.
RUN CMAKE_VER=3.31.6 && \
    ARCH_TAG=$(uname -m) && \
    if [ "$ARCH_TAG" = "x86_64" ]; then CM_ARCH=x86_64; else CM_ARCH=aarch64; fi && \
    wget -q "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VER}/cmake-${CMAKE_VER}-linux-${CM_ARCH}.sh" -O /tmp/cmake.sh && \
    sh /tmp/cmake.sh --skip-license --prefix=/usr/local && \
    rm -f /tmp/cmake.sh && \
    cmake --version
```

Notes:
- `wget` is already installed earlier in this Dockerfile (the clang step runs
  `apt-get install gpg wget -y`), and `ca-certificates` is present on the
  18.04 path via the OpenSSL step; no new package installs are needed.
- The replaced block's `apt-get install -y software-properties-common
  lsb-release` is redundant — both are installed in the first apt step of the
  file — so dropping it with the block changes nothing else.
- No Kitware apt list/key is ever added on the fixed path, so no residue
  cleanup is needed in this file (it had none before either).

#### Change 2 — `src/edgemlsdk/Dockerfile.jp5` (JP5/arm64)

**Location**: The "── 2. CMake 3.21.3 from Kitware ──" `RUN` block.

Replace it with the same JP6-pattern block as Change 1 (identical text to the
`Dockerfile.jp6` step 2 block, including its comment style updated to say
"JP5 / Ubuntu 20.04 base"). On JP5 hardware `uname -m` yields `aarch64`, so
the arch selection picks the aarch64 installer.

**Explicitly unchanged**: the later "── 3. Python 3.11 ──" step's Kitware
residue cleanup lines (`rm -f /etc/apt/sources.list.d/*kitware*` and
`sed -i '/kitware/d' /etc/apt/sources.list`) stay exactly as they are — they
are harmless defense-in-depth, they mirror JP6, and Req 3.2 explicitly
preserves them. `wget` is already used in this block pre-fix, so it is
available.

#### Change 3 — `src/edgemlsdk/src/utilities/machine_setup.sh` (host script)

**Function**: `install_cmake`

Replace only the 18.04 Kitware-apt branch body with a host-adapted JP6
pattern; keep the function's structure, its "already installed" skip path,
and the non-18.04 `check_and_install_package cmake` branch (Ubuntu archive,
not Kitware — not part of the bug condition):

```bash
install_cmake()
{
    if ! dpkg -s "cmake" >/dev/null 2>&1 && ! command -v cmake >/dev/null 2>&1; then
        . /etc/os-release
        if [ $VERSION_ID = "18.04" ]; then
            # Pinned upstream release binary (see src/edgemlsdk/Dockerfile.jp6):
            # the Kitware apt repo no longer serves pinned 3.x packages and its
            # current cmake is 4.x (Triton-incompatible).
            CMAKE_VER=3.31.6
            if [ $(uname -m) = "x86_64" ]; then CM_ARCH=x86_64; else CM_ARCH=aarch64; fi
            wget -q "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VER}/cmake-${CMAKE_VER}-linux-${CM_ARCH}.sh" -O /tmp/cmake.sh
            $do_sudo sh /tmp/cmake.sh --skip-license --prefix=/usr/local
            rm -f /tmp/cmake.sh
            cmake --version
        else
            check_and_install_package cmake;
        fi
    else
        echo "CMake is already installed"
    fi
}
```

Host-script adaptations (vs the Docker pattern):
- `$do_sudo` on the privileged install step, mirroring the existing
  `install_powershell` convention in the same file (arch selection also
  mirrors `install_powershell`).
- The guard gains `command -v cmake`: the installer-based CMake does not
  register with dpkg, so without it a re-run would reinstall. `dpkg -s` is
  kept so hosts with apt-installed CMake still skip — preserving Req 3.5's
  skip semantics in both worlds.
- The old branch's gpg/apt-key/kitware-keyring choreography is deleted with
  the branch; nothing else in the script referenced it.

#### Change 4 — Security-baseline golden regeneration (sanctioned capture paths)

Two goldens are affected by intentional edits and must be regenerated; one
must be proven untouched:

| Golden | Why it changes | Sanctioned regeneration path |
|--------|----------------|------------------------------|
| `test/backend-test/security/baselines/docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` | The CMake `RUN` lines of `Dockerfile.jp5` are non-`FROM` lines, so the masked view changes | Delete the golden, re-run `test_preservation_docker_masked_bytes.py` — `capture_or_assert_text` re-captures on absence from the fixed tree; then re-run to assert green |
| `test/backend-test/security/baselines/docker_baseline_out_of_scope.json` — the `src/edgemlsdk/Dockerfile` entry | That file's full sha256 is pinned by the out-of-scope guard (from the security-docker-non-ecr-base-image-fixes spec) and the CMake block edit changes it | Per `.kiro/steering/builds.md`: `sha256sum src/edgemlsdk/Dockerfile`, edit **just that entry** in the JSON golden, re-run `test_preservation_docker_out_of_scope.py` |
| `docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt` | Must NOT change | No action; a test asserts it is bit-identical pre/post fix (Property 4 / Req 3.1) |

`machine_setup.sh` is not covered by any existing baseline or guard (verified
by repo search), so no additional golden is involved. The vendored
`src/backend/edgemlsdk/edgemlsdk/**` duplicates are gitignored build
artifacts regenerated by `build-custom.sh` from the fixed source; no action.
Per steering, the baseline updates ship in the same commit as the fix.

## Testing Strategy

### Validation Approach

Full Docker image builds cannot run in tests, so validation is layered:

1. **Static/property tests** over the Dockerfile/script text (new tests under
   `test/backend-test/edgemlsdk_cmake/`) — exploratory on the unfixed tree,
   fix + preservation checks on the fixed tree.
2. **Existing security-baseline suite** re-run against regenerated goldens.
3. **Approval-gated operational verification**: a real portal build that must
   reach `succeeded` **including artifact publication** — the user-mandated
   completion criterion.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix. Confirm or refute the root cause analysis. If refuted, we
re-hypothesize.

**Test Plan**: Static exploration tests that parse the UNFIXED
`src/edgemlsdk/Dockerfile`, `Dockerfile.jp5`, and `machine_setup.sh` and
assert the bug condition is present (these tests document C(X); they are
expected to FAIL once the fix lands and will be inverted/retired then).

**Test Cases**:
1. **AMD64 bionic pin present**: `Dockerfile` 18.04 branch contains
   `apt.kitware.com` and `cmake=3.21.3-0kitware1ubuntu18.04.1` (will confirm
   bug on unfixed code)
2. **AMD64 focal pin present**: `Dockerfile` 20.04 branch contains
   `cmake=3.21.3-0kitware1ubuntu20.04.1` (will confirm bug on unfixed code)
3. **JP5 pin present**: `Dockerfile.jp5` contains the same focal pin (will
   confirm bug on unfixed code)
4. **Host script unpinned Kitware install**: `install_cmake` 18.04 branch
   adds `apt.kitware.com` and installs unpinned `cmake` (will confirm bug on
   unfixed code)
5. **JP6 already fixed (¬C sanity)**: `Dockerfile.jp6` contains the
   GitHub-release installer and no Kitware apt CMake resolution (passes on
   unfixed code — anchors the target pattern)

**Expected Counterexamples**:
- The three Dockerfile CMake steps and the host-script branch all resolve
  CMake via `apt.kitware.com` — matching the live build failure evidence
  (apt exit 100 on job `40b036fc`)
- Possible refutations: the files were already partially fixed, the pin
  strings differ from the bugfix document, or additional CMake install sites
  exist (a repo-wide scan test guards this)

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
files produce the expected behavior.

**Pseudocode:**

```
FOR ALL step IN cmakeInstallSteps(affectedFilesFixed) DO
  ASSERT NOT resolvesViaKitwareApt(step)           -- no apt.kitware.com cmake
  ASSERT usesGithubReleaseInstaller(step)          -- pinned self-contained .sh
  ASSERT hasArchSelection(step)                    -- x86_64 / aarch64
  IF step.file IS Dockerfile THEN
    ASSERT hasInBuildVerification(step)            -- cmake --version
  END IF
  v := parsePinnedVersion(step)
  ASSERT 3.21 <= v AND v < 4.0                     -- Property 3
END FOR

-- repo-wide sweep (no residual bug condition anywhere in scope):
ASSERT grep("apt.kitware.com", affectedFilesFixed) = EMPTY
ASSERT grep("cmake=3.21.3-0kitware", affectedFilesFixed) = EMPTY
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed files produce the same result as the original files.

**Pseudocode:**

```
FOR ALL file IN affectedFiles DO
  ASSERT maskCMakeInstallBlock(file_fixed) = maskCMakeInstallBlock(file_original)
END FOR
ASSERT sha256(Dockerfile.jp6_fixed) = sha256(Dockerfile.jp6_original)
ASSERT nonInstallCmakeFunctions(machine_setup_fixed)
       = nonInstallCmakeFunctions(machine_setup_original)
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because:
- It generates many test cases automatically across the input domain (here:
  version strings for the range property, line-classification inputs for the
  masking helper)
- It catches edge cases that manual unit tests might miss (e.g. version
  strings like `3.9`, `3.21.0`, `4.0.0-rc1` in the range comparator)
- It provides strong guarantees that behavior is unchanged for all non-buggy
  inputs

**Test Plan**: Observe the UNFIXED files first — capture diff-scoping goldens
(sha256 of `Dockerfile.jp6`; CMake-block-masked text views of `Dockerfile`,
`Dockerfile.jp5`, and `machine_setup.sh`) via a capture-or-assert helper
mirroring `_docker_preservation_support.capture_or_assert_text`. After the
fix, the same tests assert the masked views are byte-for-byte identical —
proving only the CMake install lines changed.

**Test Cases**:
1. **Non-CMake bytes of `Dockerfile`**: capture masked view on unfixed tree,
   assert identical after fix
2. **Non-CMake bytes of `Dockerfile.jp5`**: same (also indirectly enforced by
   the regenerated security masked-bytes golden)
3. **Non-`install_cmake` bytes of `machine_setup.sh`**: capture the file with
   the `install_cmake` function body masked, assert identical after fix;
   additionally assert the "already installed" skip path and the
   `check_and_install_package cmake` else-branch survive verbatim
4. **JP6 untouched**: sha256 of `Dockerfile.jp6` and its masked security
   golden are bit-identical pre/post fix

### Unit Tests

- Parse each fixed CMake install step: installer URL shape, `CMAKE_VER`
  value, arch-selection conditional, `--skip-license --prefix=/usr/local`,
  `cmake --version` presence (Dockerfiles), `$do_sudo` on the host-script
  install step
- Guard semantics of the fixed `install_cmake`: skip when `dpkg -s cmake` OR
  `command -v cmake` succeeds; 18.04 branch selected only for
  `VERSION_ID = "18.04"`
- Negative sweeps: no `apt.kitware.com`, no `kitware.gpg`/`kitware.list`
  additions, no `cmake=3.21.3-0kitware*` anywhere in the three fixed files

### Property-Based Tests

- **Version range property (Property 3)**: Hypothesis-generated version
  strings validate the 3.21 ≤ v < 4.0 comparator; every concrete
  `CMAKE_VER` parsed from the four files (three fixed + jp6) must satisfy it
- **Masking preservation property (Property 2)**: for generated line
  sequences, the masking helper removes exactly the CMake-install block lines
  and nothing else (mirrors the sanity test pattern of
  `test_masking_removed_the_from_lines`)
- **Arch-selection property (Property 1)**: for any `uname -m` value, the
  modeled arch selection yields `x86_64` for `x86_64` and `aarch64` otherwise
  — matching JP6's behavior exactly

### Integration Tests

Automated integration is limited by the no-Docker-builds constraint; the
security suite serves as the in-repo integration layer, and the live build is
the true integration test:

- Re-run the full docker security preservation suite
  (`test/backend-test/security/preservation/`) against the fixed tree with
  regenerated goldens — masked-bytes mechanism intact (Req 3.4), out-of-scope
  guard green with the updated `src/edgemlsdk/Dockerfile` hash, jp6 goldens
  unchanged
- Pre-build guard run per `.kiro/steering/builds.md` (out-of-scope guard +
  secrets guard) before dispatching the verification build

### Gated Live Verification (User-Mandated Completion Criterion)

Per the bugfix Introduction: **"The build must succeed and publish to be
successful and complete."** Local/static validation alone does NOT complete
this spec. After all local validation passes, an approval-gated operational
verification phase runs:

1. **Push precondition**: the fix only affects real builds once pushed to
   origin's `feature/portal-build-fleet-and-workflow-gates` branch — build
   servers sync from origin, so local commits are invisible to them. The
   verification build's `source_ref` is that feature branch (post-push).
2. **Approval gate**: dispatching a portal build is an operational action on
   shared infrastructure — request explicit user approval before dispatch.
3. **Preflight first**: run the portal build preflight before dispatching,
   per the established build-fleet workflow.
4. **Cheapest target first**: an AMD64 dedicated build on the existing x86
   server (the same shape as the failing evidence job `40b036fc`) — it
   exercises `src/edgemlsdk/Dockerfile`'s focal branch, the exact front-line
   failure.
5. **Monitoring**: track the job via the Build Log diagnostics deployed
   today; confirm the edgemlsdk CMake step now logs the GitHub-release
   installer and `cmake version 3.31.6`, not apt exit 100.
6. **Success criterion**: the job must reach `succeeded` **including artifact
   publication** — not merely fail later than before. A build that fails
   differently is progress evidence but does not complete the spec.
7. **New-failure handling**: any follow-on failure beyond the CMake step
   (e.g. a later compile or publish error) is **new evidence outside this
   spec's fix scope** — record it, open/route a new spec or task for it, and
   keep this spec open (not complete) until a build succeeds and publishes.
8. **JP5 follow-on**: after AMD64 succeeds, the JP5 (arm64) path
   (`Dockerfile.jp5`) should be verified by the next JP5 build opportunity;
   builds run strictly one at a time per steering.

## Rollback Considerations

The fix is a **pure git revert**:

- All changes are text edits to three tracked source files plus two golden
  files under `test/backend-test/security/baselines/` — no schema, data, or
  infrastructure migration. Reverting the fix commit restores the pre-fix
  Dockerfiles/script AND the pre-fix goldens atomically, so the preservation
  suite stays consistent on either side of the revert (the goldens revert
  with the code they describe).
- The new tests under `test/backend-test/edgemlsdk_cmake/` revert with the
  same commit.
- No runtime state depends on the change: images are rebuilt from the
  Dockerfiles on each build, and `machine_setup.sh` is idempotent (the
  `command -v` guard means a host that already got installer-based CMake
  simply skips on re-run, even after a script revert).
- Note the asymmetry: rolling back restores a known-broken state (the Kitware
  apt pins fail against today's repo contents), so rollback is only a safety
  valve against a defective fix, not a viable operating state.
