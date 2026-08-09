# Verification Notes — backend-jammy-retired-packages

Task 5 validation checkpoint (Properties 1-3), run on the fixed tree.
Local/static validation only: no deploy, push, SSM command, instance action,
artifact publication, or build was performed. Live verification remains gated
behind tasks 6 and 7 (separate explicit approvals).

Endpoint hygiene: this file contains NO live API Gateway URLs or other internal
endpoints (prior-chain lesson). Where an endpoint would otherwise be referenced,
use the placeholder `<REDACTED-INTERNAL-ENDPOINT>`.

## Tree state at checkpoint

- `git status` diff scope (spec-relevant): `src/backend/Dockerfile` (the fix,
  sha256 `ba076f2fae5de7819935daa08b71ed6c34ec2839ecbb519ba7dc5ffca3b0c655`),
  `test/backend-test/security/baselines/docker_baseline_out_of_scope.json`
  (exactly ONE entry regenerated — the `src/backend/Dockerfile` hash,
  `40f7c9e0...` → `ba076f2f...`; compose/edgemlsdk/frontend entries untouched,
  confirmed by `git diff`), and the new untracked package
  `test/backend-test/backend_jammy_pkgs/`.
- Test-file freeze: sha256 of both test modules and both support modules
  captured before and after all checkpoint runs — identical
  (`test_bug_condition_exploration.py` `88ae09ff...`,
  `test_preservation_baseline.py` `cbe4b76e...`,
  `_jammy_support.py` `17151e7c...`, `_jammy_preservation_support.py`
  `d36c6512...`). The task-3-era corrective parsing fix to exploration case 2
  (shared `normalized_apt_bodies` helper) predates this checkpoint and is part
  of the frozen baseline; assertions were not weakened (re-verified against
  the unfixed tree during that fix).

## Task 5.1 — Property 1 (Expected Behavior)

Command:
`PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/backend_jammy_pkgs/test_bug_condition_exploration.py --noconftest`

Result: **7 passed** in 0.21s (all seven design exploration cases).
- Cases 1, 2, 6 (the former bug-condition failures on the unfixed tree) now
  PASS: zero jammy-retired tokens in any AMD64-reachable apt step; the libssl
  step is the exact `/etc/os-release`-gated conditional with allowlist
  {"18.04", "20.04"} and body `apt-get install libssl1.1 -y`; the guarded step
  is 18.04/20.04-reachable and 22.04-unreachable under the reachability model.
- Cases 3, 4, 5, 7 still PASS: retired-token scan finds zero reachable sites
  (post-fix meaning); class-closure verdict inventory fully vetted; `ARG OS`
  only before `FROM`, no re-declaration after; frontend Dockerfile has zero
  apt steps.
- Tests re-run UNCHANGED from task 1 (hash-verified above). Fix confirmed;
  class closed. _Requirements: 2.1, 2.2, 2.3, 2.4._

## Task 5.2 — Property 2 (Preservation)

Command:
`PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/backend_jammy_pkgs/test_preservation_baseline.py --noconftest`

Result: **21 passed** in 3.70s against the FROZEN goldens (no recapture).
- Masked golden `backend_Dockerfile_libssl_masked.txt`: byte-identical — the
  shape-agnostic mask matched the fixed comment+conditional block, proving
  every other `src/backend/Dockerfile` line (Python 3.11 source build, awscrt
  workaround, apt-update lines, line 72's six-package install, inert $OS
  conditional, CVE block, COPY/script invocations) survives verbatim.
- Mask-exactness: exactly ONE target block differs from the raw file, and its
  shape is the admissible "fixed" form.
- All 8 full-file sha256 goldens (frontend Dockerfile, compose,
  jp5/jp6/x86_64_nvidia variants, three install scripts): bit-identical.
- Hypothesis properties (classifier token-boundary, masking preservation,
  apt tokenization totality, reachability model): all green.
- Golden immutability verified after the run: sha256 of all 9 golden files in
  `test/backend-test/backend_jammy_pkgs/baselines/` identical to the pre-run
  capture (e.g. masked golden `aba912ae...`). _Requirements: 3.1, 3.2, 3.3._

## Task 5.3 — Property 3 (Baseline Regeneration) + suite-wide checks

1. Full security preservation suite:
   `PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/security/preservation/ --noconftest`
   → **152 passed, 2 skipped** in 31.74s. The 2 skips are the known
   pre-existing vendored-duplicate skips
   (`test_preservation_s3_out_of_scope_guard.py`,
   `test_preservation_secrets_out_of_scope_guard.py`) — expected shape.
   Out-of-scope guard green with the regenerated `src/backend/Dockerfile`
   entry; masked-bytes mechanism, backend/edgemlsdk jp5+jp6 masked goldens,
   and default-refs guard all intact (no golden other than the one JSON entry
   modified — `git status` on `test/backend-test/security/baselines/` shows
   only `docker_baseline_out_of_scope.json`).

2. Sibling packages (run per-package; a single combined invocation hits a
   pytest module-basename collision between the two rootless packages'
   identically named test files — pre-existing layout property, no files
   touched):
   - `pytest test/backend-test/edgemlsdk_cmake/ --noconftest` → **26 passed**
   - `pytest test/backend-test/edgemlsdk_pythondev/ --noconftest` → **16 passed**
   Total 42/42. `git status` over both packages: clean — all sibling goldens
   bit-identical (`src/edgemlsdk/**` untouched, Req 3.5).

3. No-Docker-build contract: grep over
   `test/backend-test/backend_jammy_pkgs/*.py` — imports are only `hashlib`,
   `os`, `re`, `collections`, `pytest`, `hypothesis`, and the two local
   support modules; zero `docker`/`subprocess`/`os.system`/`Popen`/shell-out
   call sites (the only textual matches are docstrings stating the contract
   and file-path references). No automated test in this spec runs a Docker
   build.

4. Property traceability:
   - Property 1 → task 5.1 run (7/7), Requirements 2.1-2.4.
   - Property 2 → task 5.2 run (21/21), Requirements 3.1-3.3.
   - Property 3 → task 4 regeneration + this checkpoint's suite runs
     (152 passed/2 skipped + 42/42, exactly one golden entry regenerated,
     mechanisms intact), Requirements 2.5, 3.4, 3.5.

Pre-existing unrelated failures: none observed in any suite run.

## Status

Local validation checkpoint COMPLETE. Spec remains OPEN per the user-mandated
completion criterion (an actual portal build must reach `succeeded` including
artifact publication). Next gates: task 6 (explicit push approval) and task 7
(separate explicit live-build approval). Neither is authorized by this
checkpoint.
