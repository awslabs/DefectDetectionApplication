# Exploration Notes — edgemlsdk-python-dev-ubuntu2204 (Task 1)

Bug-condition exploration static tests run on the UNFIXED tree, per the
bugfix workflow: the retired-token and fixed-line cases MUST FAIL on unfixed
code (failure confirms bug condition C(X)); the scoping check and ¬C cases
MUST PASS.

## Run

- Command: `PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/edgemlsdk_pythondev/ --noconftest`
- Result: **2 failed, 4 passed** — exactly the expected outcome.
- Package: `test/backend-test/edgemlsdk_pythondev/` (`test_bug_condition_exploration.py`
  + `_pythondev_support.py`), mirroring the `edgemlsdk_cmake` import-light
  pattern. TEXT parsing only — no `docker`, `subprocess`, or shell-out
  anywhere in the package. Logical RUN reconstruction across backslash
  continuations, comment lines skipped, package-token extraction excluding
  flags, strict whole-token matching (`python-dev` never substring-matches
  `python-dev-is-python3` or `python3-dev`).

## Counterexamples (bug condition C(X) confirmed)

### Case 1 — retired-token scan (FAILED as expected)

```
src/edgemlsdk/Dockerfile:286: token 'python-dev' in step: RUN apt-get install python-dev -y
assert not [(286, 'python-dev', 'RUN apt-get install python-dev -y')]
```

Exactly ONE retired transitional Python package site in the file's apt
install steps — line 286, token `python-dev` — positioned immediately after
the Triton-section `RUN apt-get update && apt-get install rapidjson-dev
libre2-dev -y` step (line 284-285), as identified by `isBugCondition` in
design.

### Case 2 — Triton-section fixed-line assertion (FAILED as expected)

```
src/edgemlsdk/Dockerfile:286: the Triton-section single-package install step is
  'RUN apt-get install python-dev -y'
expected exactly
  'RUN apt-get install python-dev-is-python3 -y'
```

## Cross-reference to live evidence

The counterexample matches portal build job
`08a1e2bd-45f9-4521-ac4a-b41b52222e2e` (AMD64, dedicated X86 server,
source_ref `feature/workflow-triggers`, commit `63ecb99`, 2026-08-09):
Docker build step **61/83** logged
`E: Package 'python-dev' has no installation candidate ... replaced by:
python2-dev python2 python-dev-is-python3`, apt **exit code 100**,
`ERROR: edgemlsdk Docker build failed`; the job settled `BUILD_FAILED`
(exit 1) ~13m35s in. Full evidence:
`.kiro/specs/edgemlsdk-cmake-pin-failure/verification-notes.md` (Task 7).

## Root cause analysis confirmed (no refutation)

- Case 3 (scoping, PASSED): the retired-token scan finds at most one site
  and the one site found is exactly line 286's `python-dev` — confirming the
  bugfix.md repo scan and the single-line fix scope. Written to hold on both
  trees (post-fix: zero sites).
- Case 4 (¬C JP6 anchor, PASSED): `Dockerfile.jp6`'s system-packages block
  requests `python3-dev` (whole token, alongside `rapidjson-dev libre2-dev`)
  and the file has no retired token — anchoring the target dev-headers
  package on a 22.04-generation base.
- Case 5 (¬C JP5 out-of-scope, PASSED): `Dockerfile.jp5` contains exactly
  one retired-token site, the `python-dev` token inside its single
  system-packages block (resolves on the digest-pinned focal base — design
  Decision 2 leaves the file untouched; the test asserts presence, guarding
  against accidental modification). Passes pre- and post-fix.
- Case 6 (¬C downstream precondition, PASSED): the `rm /usr/bin/python`
  command exists (logical RUN starting at line 308, `rm` at line 309), uses
  no `-f`, and follows line 286 — pinning the structural fact that forces
  design Decision 1's `python-dev-is-python3` choice (a drop or bare
  `python3-dev` would relocate the failure to this later, preserved step).
  Passes pre- and post-fix.

No refutation observed: line 286 is unchanged, no additional retired sites
exist, and the JP6 anchor uses `python3-dev`. Proceed with the fix as
designed.

These tests encode the expected behavior and are re-run unchanged as the fix
check in task 5.1. Baseline frozen — no assertion may be weakened.
