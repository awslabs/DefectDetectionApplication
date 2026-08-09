# Exploration Notes — build-docker-save-stdout-failure (Task 1)

Bug-condition exploration static tests written and run on the UNFIXED tree.
Package: `test/backend-test/build_save_pkgs/` (`_save_support.py` +
`test_bug_condition_exploration.py`), mirroring the sibling pattern
(`backend_jammy_pkgs`, `edgemlsdk_pythondev`): TEXT-only parsing,
import-light, `--noconftest`, content anchors (no line numbers except the
deliberate line-2/3 structural pins that precede the fix region). No
`docker`, `subprocess`, or shell-out anywhere in the package.

## Command

```
PYTHONPATH=src/backend:test/backend-test pytest test/backend-test/build_save_pkgs/ --noconftest
```

## Result: 3 failed, 4 passed — exactly the expected shape

| Case | Test | Expected on unfixed tree | Observed |
|------|------|--------------------------|----------|
| 1 | `TestNoStdoutRedirectSaveSiteRemains` | FAIL | FAIL ✔ |
| 2 | `TestBothSitesInFixedForm` | FAIL | FAIL ✔ |
| 3 | `TestFixedFormHelperStructure` | FAIL | FAIL ✔ |
| 4 | `TestCounterexampleInventoryScoping` | PASS | PASS ✔ |
| 5 | `TestClassBoundaryNeighborScripts` | PASS | PASS ✔ |
| 6 | `TestTempHazardGuardsCoexist` | PASS | PASS ✔ |
| 7 | `TestLogAnchorPreserved` | PASS | PASS ✔ |

## Counterexamples (bug condition C(X) confirmed)

The comment/string-aware save-form classifier found exactly TWO
STDOUT_REDIRECT `docker save` invocation sites in `build-custom.sh` — no
more, no fewer — positioned between the unchanged line-360
`echo "save docker images as tarvballs"` log anchor and the unchanged
compose-`cp` staging line (`cp src/docker-compose.yaml ...`), immediately
after the 5-line comment block (lines 361-365) that documents why the
redirect form was adopted (the snap `--output` `.tmp-*`/zip race):

```
build-custom.sh:366: form=STDOUT_REDIRECT image='flask-app'
  dest='./custom-build/$COMPONENT_NAME/flask-app.tar'
  in: docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar

build-custom.sh:367: form=STDOUT_REDIRECT image='react-webapp'
  dest='./custom-build/$COMPONENT_NAME/react-webapp.tar'
  in: docker save react-webapp > ./custom-build/$COMPONENT_NAME/react-webapp.tar
```

Case 2's counterexample: zero `save_image_tar` helper call sites exist
(expected two). Case 3's counterexample: no `save_image_tar()` helper is
defined at all — no `--output`/`.partial`/`mv` discipline and no integrity
guard, so a silent 0-byte tar can reach the zip.

## Cross-reference to live evidence

The line-366 site matches the live failure exactly: portal build job
`d844a5fb-81d5-4294-956d-d6d6ae1f000e` (AMD64, dedicated X86 build server,
source_ref `feature/workflow-triggers`, commit `ab900d9`, settled `failed`
2026-08-09 at ~9m32s, error_kind `building`, exit 1) logged
`save docker images as tarvballs` followed immediately by
`write /dev/stdout: bad file descriptor` (EBADF). The 0-byte
`flask-app.tar` was confirmed on-server by read-only SSM inspection; disk
was not the cause (29 GB free; the flask-app image is 4.78 GB). The
line-367 site (react-webapp) is the byte-identical pattern, unreached only
because line 366 aborts first under `set -e` (line 2 — pinned by case 7).

## Root cause analysis: CONFIRMED, no refutation

- The save lines are exactly as documented in bugfix.md (lines 366-367,
  stdout-redirect form) — not already changed.
- No additional `docker save`/`docker export` sites exist: the whole-file
  scan found only the two sites (case 4), and the four neighbor scripts
  (`scripts/portal-build-agent.sh`, `publish-ecr-only.sh`,
  `com.dda.InferenceUploader/build-and-publish.sh`,
  `src/edgemlsdk/build.sh`) contain zero sites (case 5). The class is
  exactly the two sites — one-pass closure scope confirmed.
- The zip-side guards are as documented at lines 385-421 (case 6): the
  pre-zip `rm -f ./custom-build/$COMPONENT_NAME/.tmp-*` cleanup, the
  explicit `ZIP_MEMBERS` array containing both tar paths verbatim
  (`custom-build/$COMPONENT_NAME/flask-app.tar`,
  `custom-build/$COMPONENT_NAME/react-webapp.tar`), the `-x '*/.tmp-*'`
  exclusion, and the explicit `"${ZIP_MEMBERS[@]}"` (non-recursive) zip
  invocation. No transient name (`.tmp-*`, `*.partial`) appears in
  `ZIP_MEMBERS`.
- The comment block matches its documented content (the classifier's
  comment-awareness was exercised for real: the block's literal
  `docker save --output` text did NOT classify as an invocation site).
- The log anchor and `set -e`/`set -o pipefail` structural pins hold
  (case 7).

Proceed to task 2 (preservation baseline capture) and then the fix
(task 3, design Change 1). These tests are frozen: they re-run unchanged as
the fix check in task 5.1, where all seven cases must pass.
