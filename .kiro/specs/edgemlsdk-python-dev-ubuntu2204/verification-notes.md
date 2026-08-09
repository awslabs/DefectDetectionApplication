# Verification Notes — edgemlsdk-python-dev-ubuntu2204

## Task 5: Fix, preservation, and property validation checkpoint (local/static only)

Date: 2026-08-09 (post-fix tree; fix at `src/edgemlsdk/Dockerfile:286` =
`RUN apt-get install python-dev-is-python3 -y`; two goldens regenerated via
sanctioned paths — the `src/edgemlsdk/Dockerfile` entry in
`docker_baseline_out_of_scope.json` now `021a7f60…`, and the `edgemlsdk_cmake`
CMake-masked golden recaptured through `capture_or_assert_text`).

Pre-run state check: `git status --porcelain` over `src/edgemlsdk/`,
`test/backend-test/edgemlsdk_pythondev/`, `test/backend-test/edgemlsdk_cmake/`,
`test/backend-test/security/` shows exactly the expected diff scope —
`M src/edgemlsdk/Dockerfile`,
`M test/backend-test/edgemlsdk_cmake/baselines/edgemlsdk_Dockerfile_cmake_masked.txt`,
`M test/backend-test/security/baselines/docker_baseline_out_of_scope.json`,
`?? test/backend-test/edgemlsdk_pythondev/` (the new package). No test file in
the package was modified between tasks 1/2 and this checkpoint.

### Task 5.1 — Property 1: Expected Behavior (Reqs 2.1, 2.2, 2.3)

Command:

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/edgemlsdk_pythondev/test_bug_condition_exploration.py --noconftest
```

Result: **6 passed** in 0.21s (same tests as task 1, unmodified).

- No retired transitional Python package token in any apt install step of the
  fixed `src/edgemlsdk/Dockerfile` (the two cases that FAILED on the unfixed
  tree in task 1 now pass — fix confirmed for the C(X) site).
- Triton-section single-package step is exactly
  `RUN apt-get install python-dev-is-python3 -y` (token-boundary discipline
  held: the fixed token does not false-positive as `python-dev`).
- ¬C cases still pass: retired-token scan finds zero sites post-fix (scoping
  check's post-fix meaning), JP6 anchor requests `python3-dev`, JP5 retains
  its out-of-scope `python-dev` token, downstream `rm /usr/bin/python`
  (no `-f`) step present and after the install site.

PBT status: **passed**.

### Task 5.2 — Property 2: Preservation (Reqs 3.1, 3.2, 3.3)

Command:

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/edgemlsdk_pythondev/test_preservation_baseline.py --noconftest
```

Result: **10 passed** in 2.66s against the FROZEN task-2 goldens (no
recapture/rebaseline).

- Python-dev-line-masked view of `src/edgemlsdk/Dockerfile` byte-identical to
  the unfixed capture (CMake block, Python 3.11 source build,
  `rapidjson-dev libre2-dev` step, `rm /usr/bin/python` step verbatim).
- Mask-exactness: exactly one line differs between the raw file and the
  masked view.
- `Dockerfile.jp5` and `Dockerfile.jp6` full-file sha256 goldens bit-identical.
- Hypothesis helper properties green (masking preservation, retired-token
  classifier exactness incl. `python-dev-is-python3` prefix trap, apt-line
  tokenization totality).

Golden immutability verified after the run: sha256 of all three files in
`test/backend-test/edgemlsdk_pythondev/baselines/` identical before/after
(`sha256sum -c` OK) and mtimes unchanged (all 1786250694).

PBT status: **passed**.

### Task 5.3 — Property 3: Baseline Regeneration (Reqs 2.4, 3.4, 3.5)

Command 1 (full docker security preservation suite):

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/security/preservation/ --noconftest
```

Result: **152 passed, 2 skipped** in ~31s. The 2 skips are the known
pre-existing vendored-duplicate skips (unrelated to this fix):

- `test_preservation_s3_out_of_scope_guard.py:101` — vendored
  `edgemlsdk/edgemlsdk/**` duplicates are gitignored build artifacts
- `test_preservation_secrets_out_of_scope_guard.py:121` — vendored
  `edgemlsdk/edgemlsdk` `deploy.py` duplicate, same reason

Out-of-scope guard green with the regenerated `src/edgemlsdk/Dockerfile` hash;
jp5/jp6 masked goldens bit-identical; masked-bytes and hash-guard mechanisms
ran unchanged. Pre-existing warnings only (torch.load TorchScript UserWarning,
PyJWT algorithms DeprecationWarning) — both unrelated to this fix.

Command 2 (prior spec's package):

```
PYTHONPATH=src/backend:test/backend-test pytest \
  test/backend-test/edgemlsdk_cmake/ --noconftest
```

Result: **26 passed** in 2.57s. Its CMake-focused assertions unaffected; the
regenerated CMake-masked golden asserts green through the same
capture-or-assert mechanism.

Golden diff scoping (`git diff --stat` over `edgemlsdk_cmake/baselines/` and
`security/baselines/`): exactly two files changed, one line each —
`edgemlsdk_Dockerfile_cmake_masked.txt` and
`docker_baseline_out_of_scope.json`. The other three `edgemlsdk_cmake` goldens
(`edgemlsdk_Dockerfile.jp5_cmake_masked.txt`,
`edgemlsdk_Dockerfile.jp6.sha256.txt`,
`machine_setup.sh_install_cmake_masked.txt`) and the security masked goldens
(`docker_baseline_edgemlsdk_Dockerfile.jp5_masked.txt`,
`docker_baseline_edgemlsdk_Dockerfile.jp6_masked.txt`) are bit-identical
(no diff).

No-Docker-build contract verified by inspection: the only imports in
`test/backend-test/edgemlsdk_pythondev/*.py` are `hashlib`, `os`, `re`,
`hypothesis`, and the package's own support modules. Zero occurrences of
`subprocess`, `os.system`, `Popen`, `shell=`, `check_output`, `check_call`,
or any `docker` invocation in code (the words appear only in docstrings
documenting this constraint). All parsing is TEXT only.

Requirement traceability confirmed: every test carries a
`**Validates: Requirements …**` annotation.
- Property 1 (fix check) → exploration tests → Reqs 1.1–1.3, 2.1–2.3
- Property 2 (preservation) → frozen-golden + helper property tests → Reqs 3.1–3.3
- Property 3 (baseline regeneration) → security preservation suite +
  `edgemlsdk_cmake` package + diff scoping above → Reqs 2.4, 3.4, 3.5

PBT status: **passed**.

### Summary

| Suite | Result |
|-------|--------|
| edgemlsdk_pythondev exploration (Property 1) | 6 passed |
| edgemlsdk_pythondev preservation (Property 2) | 10 passed |
| security/preservation (Property 3) | 152 passed, 2 pre-existing skips |
| edgemlsdk_cmake (Property 3 / Req 3.5) | 26 passed |

Pre-existing unrelated failures: **none** (only the two documented
vendored-duplicate skips and two pre-existing dependency warnings).

**STOP honored**: no deploy, push, SSM command, instance action, artifact
publication, or build was performed in this checkpoint. Live verification
proceeds only through the separately approval-gated tasks 6 (commit + push)
and 7 (AMD64 dedicated live build; user-mandated completion criterion:
`succeeded` including artifact publication).
