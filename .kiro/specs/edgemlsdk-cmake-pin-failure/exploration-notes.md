# Exploration Notes — Task 1: Bug-Condition Exploration Static Tests (Unfixed Tree)

**Property 1: Bug Condition — Pinned Upstream Release-Binary CMake Install**

Date: run on the UNFIXED tree, before any production edit.

Command (finite, non-watch):

```
PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/edgemlsdk_cmake/ --noconftest
```

Result: **4 failed, 3 passed in 0.70s** — exactly the expected outcome.

| # | Design exploration case | Test | Result | Expected |
|---|------------------------|------|--------|----------|
| 1 | `Dockerfile` 18.04/bionic branch pin | `TestDockerfileAmd64::test_bionic_branch_uses_pinned_upstream_release_binary` | FAILED | FAIL ✓ |
| 2 | `Dockerfile` 20.04/focal branch pin | `TestDockerfileAmd64::test_focal_branch_uses_pinned_upstream_release_binary` | FAILED | FAIL ✓ |
| 3 | `Dockerfile.jp5` focal pin | `TestDockerfileJp5::test_jp5_cmake_step_uses_pinned_upstream_release_binary` | FAILED | FAIL ✓ |
| 4 | `machine_setup.sh` `install_cmake` 18.04 branch | `TestMachineSetupInstallCmake::test_install_cmake_uses_pinned_upstream_release_binary` | FAILED | FAIL ✓ |
| 5 | ¬C anchor: `Dockerfile.jp6` already fixed | `TestDockerfileJp6Anchor::test_jp6_already_satisfies_fixed_predicate` | PASSED | PASS ✓ |
| — | Scoping guard: no additional Kitware CMake sites | `TestRepoWideScanGuard::*` (2 tests) | PASSED | PASS ✓ |

## Counterexamples (exact Kitware apt lines and pins found)

### Case 1+2 — `src/edgemlsdk/Dockerfile` (AMD64, single if/else CMake RUN block)

```
Dockerfile:64:  wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /etc/apt/trusted.gpg.d/kitware.gpg >/dev/null && \
Dockerfile:65:  apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main" && \
Dockerfile:71:  apt-get install cmake=3.21.3-0kitware1ubuntu18.04.1 cmake-data=3.21.3-0kitware1ubuntu18.04.1 -y; \   (18.04 branch)
Dockerfile:75:  wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc ... && \
Dockerfile:76:  apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main" && \
Dockerfile:82:  apt-get install cmake=3.21.3-0kitware1ubuntu20.04.1 cmake-data=3.21.3-0kitware1ubuntu20.04.1 -y; \   (20.04 branch)
```

### Case 3 — `src/edgemlsdk/Dockerfile.jp5` ("── 2. CMake 3.21.3 from Kitware ──" step)

```
Dockerfile.jp5:36:  RUN wget -O /etc/apt/trusted.gpg.d/kitware.asc https://apt.kitware.com/keys/kitware-archive-latest.asc && \
Dockerfile.jp5:37:  echo "deb https://apt.kitware.com/ubuntu/ focal main" > /etc/apt/sources.list.d/kitware.list && \
Dockerfile.jp5:39:  apt-get install -y cmake-data=3.21.3-0kitware1ubuntu20.04.1 cmake=3.21.3-0kitware1ubuntu20.04.1 && \
```

### Case 4 — `src/edgemlsdk/src/utilities/machine_setup.sh` (`install_cmake`, 18.04 branch)

```
machine_setup.sh:65:  wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | tee /etc/apt/trusted.gpg.d/kitware.gpg >/dev/null;
machine_setup.sh:66:  apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main";
machine_setup.sh:73:  apt-get install cmake -y;          (UNPINNED — installs whatever Kitware serves today, i.e. 4.x)
```

## Cross-reference to live evidence

The focal pin (case 2) is the exact package the live failure hit: portal build
job `40b036fc-c379-4a70-b00e-e6d6b0d46ecb` (AMD64, dedicated, source_ref
`feature/portal-build-fleet-and-workflow-gates`, commit `646fb9d`) failed
2026-08-08 with `BUILD_FAILED` exit 1; the on-server gdk log tail shows **apt
exit code 100 on `cmake=3.21.3-0kitware1ubuntu20.04.1`** at the edgemlsdk
CMake install step (~step 7, ~3 minutes in). Recorded in
`.kiro/specs/build-fleet-execution-failures/verification-notes.md` (Task 13).

## Root cause confirmation

- All four C(X) install steps are present in the unfixed tree exactly as the
  bugfix/design documents describe — no refutation (no partial fixes, no
  differing pin strings).
- The repo-wide scan guard confirms NO additional Kitware CMake install sites
  exist under `src/edgemlsdk` beyond the three identified files.
- The ¬C anchor passes: `Dockerfile.jp6` already implements the target JP6
  pattern (pinned `CMAKE_VER=3.31.6`, GitHub-release installer, `uname -m`
  arch selection, `--skip-license --prefix=/usr/local`, in-build
  `cmake --version`).

Root cause analysis CONFIRMED — proceed to the fix (task 3). These tests are
frozen; they are re-run unchanged as the fix check in task 5.1 and must then
pass.
