# Verification Notes — edgemlsdk-cmake-pin-failure

## Task 5.4 — Local validation checkpoint (Properties 1–4)

Date: 2026-08-08 (post-fix tree; tasks 1–4, 5.1–5.3 complete)

### Validation commands and results

1. Full docker security preservation suite (fixed tree, regenerated goldens):

   ```
   PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/security/preservation/ --noconftest
   ```

   **Result: 152 passed, 2 skipped** (31.6s, exit 0).

   - The 2 skips are pre-existing and intentional, unrelated to this spec:
     - `test_preservation_s3_out_of_scope_guard.py:101` — vendored
       `edgemlsdk/edgemlsdk/**` duplicates are gitignored build artifacts,
       excluded from the byte-for-byte out-of-scope guard.
     - `test_preservation_secrets_out_of_scope_guard.py:121` — same vendored
       `deploy.py` duplicate exclusion.
   - No pre-existing unrelated failures: the suite is fully green.
   - Masked-bytes mechanism intact (Req 3.4):
     `test_preservation_docker_masked_bytes.py` passes against the regenerated
     `docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt` — FROM-line masking
     and byte-for-byte comparison enforced unchanged.
   - Out-of-scope guard green (Req 2.5):
     `test_preservation_docker_out_of_scope.py` passes with the updated
     `src/edgemlsdk/Dockerfile` entry. Verified
     `sha256sum src/edgemlsdk/Dockerfile` =
     `5fe6186db15e31fa87b2924a4d014a729399561c8dcc110754c6bfbe95a7e355`,
     matching the single-entry update in
     `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
     (git diff confirms exactly one entry changed; all other entries untouched).
   - JP6 golden unchanged (Req 3.1):
     `git diff --stat test/backend-test/security/baselines/docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt`
     is empty; `git status --porcelain` on the baselines directory shows only
     the two intentionally regenerated goldens modified
     (`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt`,
     `docker_baseline_out_of_scope.json`).

2. Spec's own package (final tally):

   ```
   PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/edgemlsdk_cmake/ --noconftest
   ```

   **Result: 26 passed** (2.5s, exit 0).

### Property 1–4 traceability (all passing)

| Property | Test(s) | Requirement annotations |
| --- | --- | --- |
| Property 1 — Pinned Upstream Release-Binary CMake Install | `test/backend-test/edgemlsdk_cmake/test_bug_condition_exploration.py` (bionic/focal branches, jp5 step, `install_cmake`, jp6 ¬C anchor, repo-wide site scan, counterexample-inventory scoping) | Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4 (per-test docstrings) |
| Property 2 — Preservation: Non-CMake Lines and JP6 Unchanged | `test/backend-test/edgemlsdk_cmake/test_preservation_baseline.py` (jp6 sha256 golden, CMake-block-masked goldens b/c, `install_cmake`-masked golden d, mask-exactness, skip-path/else-branch verbatim, Hypothesis masking-helper properties) | Validates: Requirements 3.1, 3.2, 3.4, 3.5 (per-test docstrings) |
| Property 3 — No-4.x Pinned Version Range | `test/backend-test/edgemlsdk_cmake/test_version_range_properties.py` (Hypothesis version-range comparator, concrete `CMAKE_VER` assertions across all four files, arch-selection property) | Validates: Requirements 2.3, 3.3 |
| Property 4 — Baseline Regeneration: Goldens Track the Fixed Tree | `test/backend-test/security/preservation/test_preservation_docker_masked_bytes.py`, `test_preservation_docker_out_of_scope.py` (regenerated goldens, task 4) plus this full-suite run | Requirements 2.5, 3.1, 3.4 |

### No-Docker-build constraint (verified by inspection)

- The spec's `test/backend-test/edgemlsdk_cmake/` package parses the affected
  files as TEXT only (`_cmake_support.py` documents "no Docker builds — the
  spec's validation constraint"). No `docker`, `subprocess`, `Popen`,
  `os.system`, or `check_output` usage anywhere in the package.
- The docker preservation suite's only subprocess usage
  (`test_preservation_docker_profile.py`) runs a bash snippet and a nested
  pytest for docker-profile selection logic — it launches no Docker build.
- No automated test in this spec runs a Docker build.

### No-live-action contract

Confirmed for this checkpoint: no deploy, no push, no SSM command, no
instance start/stop/terminate, no artifact publication, no build dispatched.
Live verification proceeds only through tasks 6 (approval-gated push) and
7 (separately approval-gated AMD64 dedicated verification build).

Per the user-mandated completion criterion in `bugfix.md`, this spec is NOT
complete until a portal build reaches `succeeded` including artifact
publication (task 7).
