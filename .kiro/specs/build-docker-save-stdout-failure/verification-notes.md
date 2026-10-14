# Verification Notes — build-docker-save-stdout-failure (Tasks 4 and 5)

Local/static validation checkpoint on the FIXED tree (after task 3's single
contiguous save-block replacement in `build-custom.sh`). All commands run
from the repo root. Endpoint redaction policy honored: no live API Gateway
URLs or other internal endpoints appear in this file or in
`exploration-notes.md` (re-verified at write time).

**Scope contract honored**: no deploy, no push, no SSM command, no
instance action, no artifact publication, and no build was performed in
tasks 4-5. Live verification proceeds only through the gated tasks 6-7.

## Task 4 — Zero-golden-change verification (Property 3)

Unique invariant of this spec: NO golden regeneration exists anywhere.
Every existing suite must pass with bit-identical goldens.

| # | Command | Result |
|---|---------|--------|
| 1 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/python_version_audit.py --noconftest -q` | 2 passed |
| 2 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/security/preservation/ --noconftest -q` | 152 passed, 2 skipped (the 2 skips are known pre-existing skips — expected shape) |
| 3 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/backend_jammy_pkgs/ --noconftest -q` | 28 passed |
| 4 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/edgemlsdk_cmake/ --noconftest -q` | 26 passed |
| 5 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/edgemlsdk_pythondev/ --noconftest -q` | 16 passed |

Sibling packages were run per-package (not in one invocation) to avoid the
known test-module-basename collision between packages under `--noconftest`.

Mechanical golden check:

```
git diff --stat -- 'test/backend-test/**/baselines/' 'test/backend-test/security/baselines/'
```

Output: EMPTY — zero tracked baseline/golden changes repo-wide.
`git status --porcelain -- test/backend-test/` shows exactly one entry:
`?? test/backend-test/build_save_pkgs/` — this spec's NEW (untracked)
package and its baselines. Nothing tracked was modified. Verdict: the fix
did not escape its declared scope (Req 3.3, 3.4). No rebaselining occurred.

## Task 5.1 — Fix check: exploration tests re-run unchanged (Property 1)

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/build_save_pkgs/test_bug_condition_exploration.py --noconftest -q
```

Result: **7 passed** (was 3 failed / 4 passed on the unfixed tree — see
`exploration-notes.md`). All seven design exploration cases now pass:
zero STDOUT_REDIRECT sites (case 1); exactly two call sites, both invoking
`save_image_tar`, destinations exactly the two `ZIP_MEMBERS` tar paths
(case 2); helper structure — `--output` to `"$dest.partial"`, atomic `mv`,
1048576 size threshold, `tar -tf` check, non-zero exits with diagnostics on
both failure paths (case 3); scan finds zero sites post-fix (case 4); four
neighbor scripts contain zero `docker save`/`docker export` sites (case 5);
`.tmp-*` cleanup + explicit `ZIP_MEMBERS` + `-x '*/.tmp-*'` exclusion
coexist and neither `.tmp-*` nor `*.partial` appears in `ZIP_MEMBERS`
(case 6); `echo "save docker images as tarvballs"` log anchor verbatim
(case 7). Requirements 2.1, 2.2, 2.3, 2.4, 2.5 — class closed in one pass.

Test-freeze proof (sha256 identical before and after the run — tests were
NOT modified since task 1/3):

```
15b4a3da3d353bb3cbcbf1b60b972fb727698b37f8a339b1dfcc4f8d63233584  test_bug_condition_exploration.py
fc730593683ef7fe2ebb3e39eb1af98e08998aeb215b886aff02b6ca8c200b6b  _save_support.py
```

## Task 5.2 — Preservation check: frozen goldens re-run unchanged (Property 2)

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/build_save_pkgs/test_preservation_baseline.py --noconftest -q
```

Result: **14 passed** (same count as the unfixed-tree capture run in
task 2; capture-or-assert helpers took the ASSERT path — goldens present).
The shape-agnostic masked golden asserts the fixed tree matches the
unfixed capture outside the one save block; mask-exactness holds (exactly
one contiguous block differs, now the comment+helper+call-sites shape);
ZIP_MEMBERS-intact holds; all four neighbor-script sha256 goldens
bit-identical. Requirements 3.1, 3.2, 3.5.

Golden-freeze proof (sha256 identical before and after the run — no
recapture/rebaseline):

```
509667baf74a091d55cb86bcfd19c1bfb7844351317d610f973993600a13ea0c  baselines/build_custom_save_masked.txt
e1d3c6c2506b076a17872d566401ac875cd6c7e8573598b4188e08581e0a62e3  baselines/com.dda.InferenceUploader_build-and-publish.sh.sha256.txt
01c371bfb084d874bc0ca3e023e986b67c8ee3906361c2842f2c793077c8bbd3  baselines/publish-ecr-only.sh.sha256.txt
27ec67e029fdefb7b5cd18e2788c6dda421702035e38c181ce206dfe2b427cdc  baselines/scripts_portal-build-agent.sh.sha256.txt
bc5c8b70322da524013e1d03a3688a78a8df6840fbf73a7c53d79f904db2cdfa  baselines/src_edgemlsdk_build.sh.sha256.txt
3b7bd0a1f53db98407a67a6d55ac5964caf0eefa0cb86494943a362817b20512  test_preservation_baseline.py
b926b073677b6cb99d3bb41dcef90cf42af83ef1d72c775fac85e20264126578  _save_preservation_support.py
```

## Task 5.3 — Suite-wide checkpoint re-run (Property 3 re-confirmed)

Complete validation set re-run at the checkpoint (fresh runs, after 5.1/5.2):

| # | Command | Result |
|---|---------|--------|
| 1 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/python_version_audit.py --noconftest -q` | 2 passed |
| 2 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/security/preservation/ --noconftest -q` | 152 passed, 2 skipped |
| 3 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/backend_jammy_pkgs/ --noconftest -q` | 28 passed |
| 4 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/edgemlsdk_cmake/ --noconftest -q` | 26 passed |
| 5 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/edgemlsdk_pythondev/ --noconftest -q` | 16 passed |
| 6 | `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/build_save_pkgs/ --noconftest -q` | 21 passed (7 exploration + 14 preservation) |

`git diff --stat` over all baselines directories re-run after all suites:
still EMPTY. Task 4's zero-golden-change verdict holds at the checkpoint.

Pre-existing unrelated failures: NONE observed. The only non-pass outcomes
anywhere were the 2 known pre-existing skips in the security preservation
suite.

## Property traceability (Properties 1-3, all validated)

| Property | Test(s) | Requirements | Status |
|----------|---------|--------------|--------|
| Property 1 — Bug Condition / Expected Behavior: both save sites use the output+rename form with an integrity guard | `test_bug_condition_exploration.py` (7 cases) + classifier Hypothesis property in `test_preservation_baseline.py` | 1.1, 1.2, 1.3, 1.4 (exploration, unfixed tree); 2.1, 2.2, 2.3, 2.4, 2.5 (fix check, fixed tree) | PASS (task 5.1) |
| Property 2 — Preservation: all other lines, tar paths, and neighbor scripts unchanged | `test_preservation_baseline.py` (masked golden, mask-exactness, ZIP_MEMBERS-intact, 4 neighbor sha256 goldens, masking + tokenization-totality Hypothesis properties) | 3.1, 3.2, 3.5 | PASS (task 5.2, frozen goldens) |
| Property 3 — Guard and suite preservation: zero golden changes, all audits green | python-version audit + security preservation suite + 3 sibling packages + mechanical `git diff --stat` over baselines | 3.3, 3.4 | PASS (tasks 4 and 5.3) |

## No-live-action / no-shell-out verification (bugfix.md validation constraint)

```
grep -rnE 'import subprocess|from subprocess|os\.system|os\.popen|import docker|from docker|shutil\.which|Popen|check_call|check_output|pty\.' test/backend-test/build_save_pkgs/*.py
```

Output: NO MATCHES (exit 1). Full import inventory of the package:
`os`, `re`, `hashlib`, `collections.namedtuple`, `hypothesis`, and the
package's own `_save_support`/`_save_preservation_support` modules — plus
pytest as the runner. The package parses `build-custom.sh` as TEXT only;
no automated test in this spec runs Docker, a subprocess, or any live
command.

## Status

- Tasks 4, 5.1, 5.2, 5.3 complete. Properties 1-3 validated with exact
  requirement traceability. Zero golden changes repo-wide.
- **Spec NOT complete**: per the user-mandated completion criterion, this
  spec closes only when an actual portal build reaches `succeeded`
  including artifact publication (tasks 6-7, separately gated and
  acknowledged; task 6 push precedes task 7 live build).
