# Exploration Notes — backend-jammy-retired-packages (Task 1)

Bug-condition exploration static tests, run on the UNFIXED tree.

## Run

```
PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/backend_jammy_pkgs/ --noconftest
```

Result: **3 failed, 4 passed** — exactly the expected shape. New package
`test/backend-test/backend_jammy_pkgs/` (`_jammy_support.py` +
`test_bug_condition_exploration.py`), mirroring the `edgemlsdk_pythondev`
pattern: TEXT-only parsing (no `docker`, `subprocess`, or shell-out),
import-light, `--noconftest`, content matching (no line-number anchors).

## Counterexample (bug condition C(X) confirmed)

The retired-token scan over the AMD64 compose build path
(`src/backend/Dockerfile` + `prereqs_install.sh`, `install_aravis.sh`,
`install_edgemlsdk.sh` under `src/backend/edge_ml1_p_camera_management/`)
found exactly ONE 22.04-reachable site:

```
src/backend/Dockerfile:70: token 'libssl1.1' in step: RUN apt-get install libssl1.1 -y
```

- The step sits between the two `apt update -y` lines (69 and 71), exactly
  as the bugfix.md scan recorded.
- It is UNCONDITIONAL (`allowlist=None` under the reachability model), so it
  is reachable on every base — including Ubuntu 22.04 (jammy), where
  `libssl1.1` has no installation candidate.
- Cross-reference to live evidence: portal build job
  `3d18ba88-9c17-490a-811b-8c21360216f4` (AMD64, dedicated X86 server,
  source_ref `feature/workflow-triggers`, commit `4e1ce8c`) died at backend
  Docker step 24/63 with apt exit 100
  (`E: Unable to locate package libssl1.1`), settling `BUILD_FAILED` on
  2026-08-09 at ~21m51s. The static counterexample is the same step the
  live build failed on.

## Per-case outcomes

| Case | Test | Expected (unfixed) | Actual |
|------|------|--------------------|--------|
| 1 | `TestNoRetiredTokensInReachableAptSteps` — no jammy-retired token in any 22.04-reachable apt step | FAIL | FAIL ✓ (counterexample above) |
| 2 | `TestLibsslStepFixedForm` — libssl step is the exact `/etc/os-release`-gated conditional, allowlist {"18.04","20.04"}, body `apt-get install libssl1.1 -y` | FAIL | FAIL ✓ (`allowlist None != frozenset({'18.04','20.04'})` — the step is the unconditional form) |
| 3 | `TestCounterexampleInventoryScoping` — retired scan finds exactly the one line-70 site | PASS | PASS ✓ (single-step fix scope confirmed; post-fix meaning: zero reachable sites) |
| 4 | `TestClassClosureVerdictInventory` — every AMD64-reachable apt token in the design-verified jammy-resolvable inventory (Decision 2 table + bugfix.md scan); retired-set members excluded (case 1 isolates them) | PASS | PASS ✓ (no unvetted tokens; class closed in one pass) |
| 5 | `TestArgScopingStructuralPin` — `ARG OS` only before FROM, no re-declaration after FROM | PASS | PASS ✓ (Decision 1's structural basis holds: `$OS` out of scope in RUN; lines 73-75 stay inert) |
| 6 | `TestOldBaseAllowlistReachability` — libssl step reachable on 18.04/20.04, unreachable on 22.04 | FAIL | FAIL ✓ (`is_reachable(step, "22.04") == True` — the unconditional step is reachable everywhere) |
| 7 | `TestFrontendZeroAptSanity` — `src/frontend/Dockerfile` has zero apt steps | PASS | PASS ✓ (alpine/npm only; Req 2.4 scan boundary anchored) |

## Root-cause confirmation

None of the refutation triggers fired:

- Line 70 has not already been changed (case 2's counterexample shows the
  unconditional form verbatim).
- No additional jammy-retired sites exist in the AMD64 path (case 3: exactly
  one site).
- No second `ARG OS` declaration exists after FROM (case 5).
- No install script contains an unvetted apt package (case 4; the three
  scripts' apt steps all resolve on jammy per the bugfix.md scan and design
  Decision 2, and `install_edgemlsdk.sh` has no apt installs at all —
  dpkg/pip only).

The hypothesized root cause stands: proceed to task 2 (preservation
baselines) and then the task 3 fix (`/etc/os-release`-gated conditional).

These tests are frozen from this point: task 5.1 re-runs them unchanged as
the fix check (Property 1).
