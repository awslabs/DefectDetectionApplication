# Build Docker Save Stdout Failure Bugfix Design

## Overview

AMD64 portal builds die at the artifact-packaging step in `build-custom.sh`:
line 366's `docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar`
fails immediately with `write /dev/stdout: bad file descriptor` (EBADF) under
the snap Docker + SSM RunShellScript execution context, leaving a 0-byte tar;
`set -e` (line 2) aborts the script, the job settles `failed` (error_kind
`building`, exit 1), and no publish step is reached. Line 367 (react-webapp)
is the identical pattern, unreached only because line 366 aborts first.
Verified live on portal build job `d844a5fb-81d5-4294-956d-d6d6ae1f000e`
(AMD64, dedicated X86 server, source_ref `feature/workflow-triggers`, commit
`ab900d9`, settled `failed` 2026-08-09, ~9m32s) — full evidence in
`.kiro/specs/backend-jammy-retired-packages/verification-notes.md` (Task 7
re-execution). This is the first NON-apt blocker in the chain, unmasked by
the three sibling fixes: that job was the first AMD64 build ever to build all
three images and pass every in-build gate.

The bind is that BOTH known save forms are broken under snap Docker: the
script's own comment (lines 361-365) documents that the stdout-redirect form
was itself the workaround for the `--output` form's transient
`.tmp-<name><rand>` temp-file behavior, which raced the old recursive
packaging `zip` to exit 18 — and the redirect form now fails under the SSM
stdout context.

**The fix**: replace both save sites with `docker save --output` writing to a
`.partial`-suffixed name in the staging dir, followed by an atomic `mv` into
the final tar name, wrapped in a small helper with explicit failure
diagnostics and a post-save integrity guard (non-empty size threshold + full
`tar -tf` structural check). This form involves docker's stdout in no way at
all — zero assumptions about snap stdout plumbing under SSM — and its one
documented hazard (the snap `.tmp-*` temp file) is neutralized three times
over: the `.partial` suffix means the final tar name only ever appears
complete via atomic rename; the packaging step's pre-zip `rm -f .tmp-*`
cleanup and its explicit `ZIP_MEMBERS` list with `-x '*/.tmp-*'` exclusion
(both already in the script, lines 385-421) guard any residual. Critically,
`--output` has direct historical evidence of producing COMPLETE tars under
snap Docker on this exact build path — the documented failure mode was the
zip race, never the save itself (see Design Decision 1). The integrity guard
additionally ensures a silent 0-byte or truncated tar can never reach the zip
again, whatever the failure mode.

Automated tests cannot run `docker save` against real multi-gigabyte images,
and there is **no live way to test the chosen form before the verification
build** — hence the fewest-assumptions decision constraint. Fix and
preservation checking are validated by static assertions and property tests
over the `build-custom.sh` text in a new `test/backend-test/build_save_pkgs/`
package mirroring the proven sibling pattern (`backend_jammy_pkgs`,
`edgemlsdk_pythondev`). Per the user-mandated completion criterion shared
with all three open siblings: **the spec is complete only when an actual
portal build reaches `succeeded` including artifact publication** — an
approval-gated operational verification phase (commit+push gate, then
live-build gate) follows local validation. A single `succeeded` AMD64 build
closes FOUR specs at once (this one plus `edgemlsdk-cmake-pin-failure`,
`edgemlsdk-python-dev-ubuntu2204`, `backend-jammy-retired-packages`).

## Glossary

- **Bug_Condition (C)**: A `docker save` invocation site X in
  `build-custom.sh` that streams the image tar through the snap-confined
  docker CLI's redirected stdout (`docker save <image> > <file>`), failing
  with `write /dev/stdout: bad file descriptor` under the SSM RunShellScript
  execution context, leaving a 0-byte tar and aborting the build. Concretely
  today: lines 366 (flask-app) and 367 (react-webapp).
- **Property (P)**: Both image-save sites produce complete, non-empty,
  structurally valid tars at the exact `ZIP_MEMBERS` paths, under both the
  SSM and interactive execution contexts, without reintroducing the snap
  `--output` temp-file hazard — and an integrity guard makes any residual
  save failure loud, never a silent 0-byte tar.
- **Preservation**: Every other line of `build-custom.sh` byte-for-byte (the
  interpreter-version audit guard, edgemlsdk build + deb extraction,
  docker-compose builds, in-image backend test / security gate block,
  staging-dir population, explicit-member-list zip with `.tmp-*` exclusion,
  `zip -T` integrity check, copy to `greengrass-build`), plus every other
  script in the repo.
- **Snap Docker**: The build server's docker CLI is the snap package. Strict
  confinement restricts its file access (private `/tmp`, `home` interface
  for `/home/*`) and broke its stdout plumbing under SSM: the shell created
  the redirect target (the 0-byte tar) but the confined CLI got EBADF
  writing fd 1 (`/dev/stdout` in the Go error message).
- **SSM RunShellScript context**: Portal builds run `build-custom.sh` via
  the SSM agent's RunShellScript document; the script's stdout/stderr are
  the agent's command pipes, not a terminal. The EBADF arises only in this
  context — the same redirect worked when stdout was a terminal.
- **Snap `--output` temp-file hazard**: Under snap Docker,
  `docker save --output <file>` writes a transient `.tmp-<name><rand>` file
  in the destination directory and renames it into place. Documented at
  lines 361-365 as the reason the redirect form was adopted: the temp file
  raced the old recursive packaging `zip` to exit 18.
- **Save block**: The changed region — the explanatory comment block (lines
  361-365) plus the two `docker save` lines (366-367). The section comment
  (line 359) and the `echo "save docker images as tarvballs"` (line 360)
  stay byte-for-byte (the echo is the log anchor the live evidence greps
  for).
- **`.partial` suffix + atomic `mv`**: The fixed form saves to
  `<dest>.tar.partial` and renames to `<dest>.tar` with `mv` in the same
  directory (same filesystem → atomic rename). The final tar name only ever
  exists complete; the snap temp for a `.partial` destination is
  `.tmp-<name>.tar.partial<rand>`, still matched by the existing `.tmp-*`
  cleanup and zip exclusion.
- **Integrity guard**: Post-save assertion that the tar is non-trivial
  (size ≥ 1 MiB — the images are 4.78 GB and hundreds of MB; any header-only
  or empty file fails) and structurally valid (`tar -tf` full listing to
  `/dev/null` exits 0). Runs for both images; failure is loud with
  diagnostics and exit 1.
- **`ZIP_MEMBERS`**: The explicit packaging member list (lines 404-412)
  referencing `custom-build/$COMPONENT_NAME/flask-app.tar` and
  `custom-build/$COMPONENT_NAME/react-webapp.tar` — the tar destination
  paths the fix must keep landing on exactly (Req 3.2).
- **Capture-on-absent / observation-first**: The golden methodology used by
  the sibling packages — a golden is captured from the UNFIXED tree on first
  run and asserted byte-for-byte thereafter, never rebaselined by this spec.
- **Token-boundary matching**: Scans for save forms classify whole shell
  tokens, never substrings — `docker save` inside a comment or string must
  not classify as an invocation site; `--output` must not match a
  hypothetical `--output-foo`.

## Bug Details

### Bug Condition

The bug manifests when `build-custom.sh`, running under the snap Docker + SSM
RunShellScript context, reaches an image-save site of the form
`docker save <image> > <file>`. The shell creates `<file>` (hence the 0-byte
tar), the confined docker CLI fails its first write to redirected stdout with
EBADF, `set -e` aborts, and the job settles `failed` before zip packaging and
before any publish step. Both known forms are defective under snap Docker:
the redirect form fails as above; the plain `--output` form has the
documented `.tmp-*`/zip race.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type ImageSaveSite
         (a docker save invocation site in build-custom.sh, identified by
          the invoked image, the tar-producing form, and its destination)
  OUTPUT: boolean

  RETURN input.form = STDOUT_REDIRECT
           -- `docker save <image> > <file>`: the image tar streams through
           -- the snap-confined CLI's redirected stdout
         AND executionContext IN {SSM_RunShellScript}
           -- under which the redirect fd is unusable by the confined CLI
           -- (EBADF), leaving a 0-byte <file> and aborting via set -e
  -- concretely today: build-custom.sh lines 366 (flask-app) and
  -- 367 (react-webapp) — the complete class per the repo-wide scan
  -- (no other docker save/export site exists anywhere in the repo)
END FUNCTION
```

Non-buggy inputs ¬C(X) are every other line of `build-custom.sh` (the
interpreter-version audit guard, the edgemlsdk build and deb extraction, the
docker-compose builds, the in-image backend test / security gate block, the
staging-dir population, the explicit-member-list `zip` packaging with its
`.tmp-*` exclusion, the `zip -T` integrity check, and the copy to
`greengrass-build`), plus every other script in the repo
(`scripts/portal-build-agent.sh`, `publish-ecr-only.sh`,
`com.dda.InferenceUploader/build-and-publish.sh`, `src/edgemlsdk/build.sh`, …
— all verified free of `docker save`/`docker export` by the bugfix.md scan).

### Examples

- **AMD64 live failure** (Req 1.1, 1.2): job `d844a5fb` logged
  `save docker images as tarvballs` then `write /dev/stdout: bad file
  descriptor`; the 0-byte
  `./custom-build/aws.edgeml.dda.LocalServer.amd64/flask-app.tar` was
  confirmed by read-only SSM inspection; disk was not the cause (29 GB free,
  flask-app image 4.78 GB). Expected: a complete ~4.78 GB tar at that exact
  path and the script proceeding to the react-webapp save.
- **Line 367 same class** (Req 1.3): `docker save react-webapp > …` is the
  byte-identical pattern; it has never executed on AMD64 only because line
  366 aborts first under `set -e`. Expected: fixed in the same pass.
- **Neither known form works** (Req 1.4): the `--output` form's snap
  `.tmp-*` rename raced the packaging zip (exit 18, documented at lines
  361-365 — the reason the redirect form was adopted); the redirect form
  fails under SSM. Expected: a form that avoids docker stdout entirely AND
  neutralizes the temp-file hazard.
- **Interactive shell** (¬C anchor, Req 2.4): the redirect form worked when
  stdout was a terminal (developers run this script locally); the fix must
  not regress the local path. `--output` is context-independent — it never
  touches stdout — so it works identically in both contexts.
- **Edge case — silent truncation**: any failure mode that leaves a 0-byte
  or partial tar without a non-zero exit would ship a corrupt artifact
  through `zip` (which happily archives a 0-byte member) all the way to
  deployment. Expected: the integrity guard makes this impossible — size
  threshold + `tar -tf` structural check, loud failure.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- Every `build-custom.sh` line other than the save block executes
  byte-for-byte unchanged — including the interpreter-version audit guard,
  the edgemlsdk build and deb extraction, the docker-compose builds with
  their profile/build-arg selection, the in-image backend test and security
  gate block, the staging-dir population, the explicit-member-list zip
  packaging with diagnostics, the `zip -T` integrity check, and the copy to
  `greengrass-build` (Req 3.1). The section comment (line 359) and the
  `echo "save docker images as tarvballs"` log anchor (line 360) also stay
  byte-for-byte; only the comment block (361-365) and the two save lines
  (366-367) change, with the comment rewritten to stay truthful.
- The packaging zip continues to reference the exact tar destination paths
  (`custom-build/$COMPONENT_NAME/flask-app.tar`,
  `custom-build/$COMPONENT_NAME/react-webapp.tar`) in its explicit
  `ZIP_MEMBERS` list, and the saved tars land at those exact paths (Req 3.2)
  — the `.partial` suffix exists only transiently before the `mv`.
- `test/python_version_audit.py` continues to pass with `build-custom.sh` as
  a scoped artifact — the fixed save block introduces no end-of-life Python
  interpreter references (Req 3.3).
- The security preservation suite and the three sibling test packages
  (`backend_jammy_pkgs`, `edgemlsdk_cmake`, `edgemlsdk_pythondev`) continue
  to pass with ZERO golden changes — no existing golden or baseline embeds
  `build-custom.sh` bytes (verified by the bugfix.md scan), and the fix
  touches no file they cover (Req 3.4).
- JP5, JP6, and x86 NVIDIA builds continue to package their artifacts
  through the same shared save/zip path: the fixed save form is
  target-agnostic (`docker save --output` produces byte-identical tar
  content to a successful streamed save) and does not alter tar contents,
  naming, or the archive layout consumed by deployment (Req 3.5).

**Scope:**

All inputs that do NOT involve the two `docker save` stdout-redirect sites
are completely unaffected by this fix. This includes:

- Every other line of `build-custom.sh` (asserted mechanically by the
  save-block-masked golden)
- Every other shell script in the repo — none contains a `docker save` or
  `docker export` invocation (bugfix.md scan; pinned by sha256 goldens for
  the four scanned neighbor scripts)
- All Dockerfiles, compose files, requirements files, application code, and
  every existing golden/baseline repo-wide

The actual expected correct behavior is defined in the Correctness
Properties section (Property 1).

## Hypothesized Root Cause

The failure mechanism is externally evidenced by the live job log and the
on-server inspection; the residual uncertainty is only in the kernel-level
detail, which the chosen fix sidesteps entirely:

1. **Snap confinement breaks the SSM-context stdout redirect**: the docker
   CLI is snap-packaged; under SSM RunShellScript the parent shell's stdout
   is the SSM agent's command pipe. When the shell performs
   `> <file>`, it opens the target and dups it onto the CLI's fd 1 — and the
   confined CLI's write to fd 1 fails EBADF (Go reports it as
   `write /dev/stdout: bad file descriptor`). The shell-side open succeeded
   (the 0-byte tar exists), so the denial is on the snap side of the fd, in
   a way specific to this execution context — the same redirect form worked
   historically when stdout was a terminal.

2. **Not disk, not the image, not apt**: 29 GB free post-run; the flask-app
   image (4.78 GB) had just been built and exported successfully; every
   in-build gate had passed. The failure is isolated to the save form.

3. **The `--output` form was abandoned for a different, now-guarded reason**:
   lines 361-365 document that `--output` under snap writes a transient
   `.tmp-<name><rand>` in the destination dir and renames it, and that this
   temp file raced the then-recursive packaging `zip` to exit 18. Two things
   have changed since: the zip is now an explicit member list with a
   `-x '*/.tmp-*'` exclusion and a pre-zip `rm -f .tmp-*` cleanup (lines
   385-421, added by `build-fleet-execution-failures`), and this fix adds
   the `.partial`+`mv` discipline on top. The original reason to avoid
   `--output` no longer holds.

4. **Why the redirect's EBADF does not implicate `--output` or pipes**: with
   `--output`, the confined CLI opens the destination file itself through
   its own (snap-mediated) file access — no inherited fd is involved; and
   the repo dir is snap-accessible (proven: the `--output` era produced the
   temp files and completed the renames in this very staging dir). A
   shell-created pipe (option a) would likewise be a fresh kernel object,
   but that path has no direct on-server evidence, whereas `--output` does.

If the exploratory static tests refute any of this (e.g. the save lines have
already been changed, another `docker save` site exists, or the zip guard
lines are not as documented), we re-hypothesize before fixing. If the live
verification build fails at the fixed step in a NEW way (e.g. snap denies the
`--output` open in the SSM context — contrary to the historical evidence),
the integrity guard and failure diagnostics are designed to make that
conclusive in one job's logs (see Testing Strategy).

## Correctness Properties

Property 1: Bug Condition - Both Save Sites Use the Output+Rename Form with an Integrity Guard

_For any_ image-save site in `build-custom.sh` where the bug condition holds
(isBugCondition returns true — today, exactly the flask-app and react-webapp
sites), the fixed script SHALL save via `docker save --output` to a
`.partial`-suffixed destination in the staging dir and atomically `mv` it to
the exact final tar path, followed by an integrity guard (size ≥ 1 MiB and
`tar -tf` structural validity) that exits non-zero with diagnostics on any
failure — so the save never streams through docker's stdout (no EBADF
exposure in any execution context, SSM or interactive), the final tar name
only ever appears complete (no reintroduced snap temp-file hazard: the
transient `.tmp-*` and `.partial` names remain covered by the existing
pre-zip cleanup and zip exclusion), and a silent 0-byte or truncated tar can
never reach the zip; and class-wide, NO bare `docker save <image> > <file>`
stdout-redirect invocation SHALL remain anywhere in `build-custom.sh` — both
C(X) sites fixed identically in one pass, closing the class.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - All Other Lines, Tar Paths, and Neighbor Scripts Unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns
false), the fixed script SHALL produce the same result as the original:
every line of `build-custom.sh` outside the save block (the comment block at
361-365 plus the two save lines at 366-367) is byte-for-byte identical to its
pre-fix state (asserted by diff-scoping: masking out only the save block and
comparing the remainder against a golden captured on the unfixed tree — the
masked view thereby also proving the audit guard, the compose builds, the
gate block, the staging-dir population, the `ZIP_MEMBERS` list with its
exact tar paths `custom-build/$COMPONENT_NAME/flask-app.tar` and
`custom-build/$COMPONENT_NAME/react-webapp.tar`, the `.tmp-*` cleanup and
exclusion, the `zip -T` check, and the `greengrass-build` copy survive
verbatim); the fixed save block still lands the tars at those exact
`ZIP_MEMBERS` paths; and the four scanned neighbor scripts
(`scripts/portal-build-agent.sh`, `publish-ecr-only.sh`,
`com.dda.InferenceUploader/build-and-publish.sh`, `src/edgemlsdk/build.sh`)
are byte-for-byte identical to their pre-fix states (full-file sha256
goldens) — the target-agnostic save path shared by JP5/JP6/x86 NVIDIA builds
is structurally unchanged.

**Validates: Requirements 3.1, 3.2, 3.5**

Property 3: Guard and Suite Preservation - Zero Golden Changes, All Audits Green

_For any_ existing automated guard or suite touching the fixed tree, the fix
SHALL leave it green with zero golden changes: `test/python_version_audit.py`
continues to pass with `build-custom.sh` in scope (the fixed block introduces
no disallowed interpreter references), and the security preservation suite
plus the three sibling test packages (`backend_jammy_pkgs`,
`edgemlsdk_cmake`, `edgemlsdk_pythondev`) continue to pass with every golden
bit-identical — no existing golden or baseline embeds `build-custom.sh`
bytes, so unlike the sibling specs this fix requires NO golden regeneration
anywhere.

**Validates: Requirements 3.3, 3.4**

### Properties Summary Table

| # | Property | Kind | Validation approach |
|---|----------|------|---------------------|
| 1 | Both save sites use `--output` → `.partial` → atomic `mv` with integrity guard; zero bare stdout-redirect saves remain; temp-file hazard not reintroduced | Fix check | Static assertions over the fixed script text: save-form classifier proving both sites are the fixed form and no STDOUT_REDIRECT site remains; exact-form assertions for the helper (`--output`, `.partial` staging-dir destination, `mv`, size threshold, `tar -tf`, non-zero exit); guard-coexistence assertions (`.tmp-*` cleanup + zip exclusion still present and the `.partial`/`.tmp-*` names covered) |
| 2 | All other script lines, `ZIP_MEMBERS` tar paths, and 4 neighbor scripts unchanged | Preservation | Diff-scoped goldens captured on the UNFIXED tree: save-block-masked view of `build-custom.sh`; full-file sha256 of the four neighbor scripts; compared byte-for-byte after the fix; explicit assertion that the `ZIP_MEMBERS` tar paths appear verbatim in the masked (unchanged) region |
| 3 | Zero golden changes repo-wide; python-version audit and all sibling suites green | Preservation | Re-run `test/python_version_audit.py`, the docker security preservation suite, and the three sibling packages against the fixed tree; assert every golden bit-identical pre/post fix (no sanctioned regeneration exists for this spec) |

## Fix Implementation

### Design Decisions

**Decision 1 — save form: `docker save --output <dest>.partial` + atomic
`mv` (option b), not the pipe form, not an inner-redirect wrapper.**

The decision constraint is explicit: there is no live way to test until the
verification build, so the chosen form must carry the fewest assumptions
about snap Docker's behavior in the SSM context. Candidates evaluated:

| Option | Avoids the SSM stdout EBADF? | Avoids the snap `.tmp-*`/zip race? | Works interactively (Req 2.4)? | Untested snap assumptions |
|--------|------------------------------|-------------------------------------|--------------------------------|---------------------------|
| (a) `docker save <img> \| cat > <file>` | Probably — docker's fd 1 becomes a fresh shell-created pipe, not the SSM-context redirect fd; `cat` (unconfined) writes the file. `set -o pipefail` (line 3) already propagates docker's failure, so no silent-truncation loophole | YES (no `--output`) | YES | **One material assumption**: that the confined CLI can write a pipe in the SSM context. Plausible (fresh kernel object, historically streamed fine when stdout was a terminal/pipe) but with ZERO direct on-server evidence — the EBADF mechanism is not understood at the kernel level, so "a pipe is different from the redirect fd" is reasoning, not observation |
| (b) **`docker save --output <dest>.partial` + `mv` (chosen)** | YES — by construction: docker's stdout is not involved at all, in any context | YES — three independent layers: `.partial` suffix means the final tar name only appears via atomic same-dir rename; the pre-zip `rm -f .tmp-*` cleanup; the explicit `ZIP_MEMBERS` list with `-x '*/.tmp-*'` exclusion (and the saves complete before zip starts — the original race needed the old recursive `zip -r <dir>` scan) | YES — context-independent | **None with respect to stdout, and the file-write path is EVIDENCED**: the `--output` era demonstrably wrote complete tars into this exact staging dir under snap on this build path (the documented failure was the zip race, never the save) — the strongest evidence available for any option |
| (c) `sh -c 'docker save <img> > <file>'` (inner redirect) | LIKELY NOT — the inner shell performs the same open+dup onto the confined CLI's fd 1; without a kernel-level understanding of the EBADF there is no reason to believe an inner redirect differs from the outer one | YES | YES | Rests entirely on the unexplained detail of WHERE the fd breaks — rejected |

Option (b) is the only candidate whose critical path is backed by direct
on-server observation rather than inference: snap docker has already proven
it can open and write image tars in this staging dir via `--output` (that is
precisely how the `.tmp-*` temp files and renames documented at lines
361-365 came to be observed). Its one known hazard is the reason the
redirect form existed, and that hazard is now guarded three ways — two of
which (`rm -f .tmp-*`, explicit member list + exclusion) already shipped
with `build-fleet-execution-failures`, plus this fix's `.partial`+`mv`
discipline ensuring the final name never exists in a partial state
regardless of snap's rename timing. Option (a) is a good fallback candidate
if the live build refutes (b), but choosing it first would trade observed
behavior for reasoned behavior. Option (c) is rejected outright.

Belt-and-braces per the decision constraint: the fixed form is wrapped in a
helper with explicit failure diagnostics (docker version, staging-dir
listing, disk free) so that IF the live build fails at this step in a new
way, one job's logs are conclusive for the re-design — no second exploratory
build needed.

**Decision 2 — `--output` destination: the staging dir itself, NOT `/tmp`,
NOT the repo root.** The `mv` must be an atomic same-filesystem rename, so
the `.partial` file belongs next to its final name. `/tmp` is rejected
outright: snap strict confinement gives the docker snap a PRIVATE `/tmp`
(a per-snap tmpfs namespace) — `--output /tmp/...` would write inside the
snap's namespace and the script's `mv` would find nothing; this is the
highest-assumption option, not the lowest. The repo root would work (same
`home`-interface accessibility as the staging dir, and the staging dir is
where snap demonstrably wrote before) but adds a cross-directory move and a
second location to clean up for zero benefit. The staging dir keeps the
transient names inside the directory the existing `.tmp-*` cleanup and zip
exclusion already police.

**Decision 3 — integrity guard on every save (new, both sites)**: after the
`mv`, assert the tar is non-trivial (`stat -c%s` ≥ 1 MiB — flask-app is
4.78 GB, react-webapp hundreds of MB; the threshold only needs to reject
degenerate 0-byte/header-only files, and 1 MiB is three orders of magnitude
below any real image tar) and structurally valid (`tar -tf "$dest"
> /dev/null` — a full-archive walk of the uncompressed docker-save tar; pure
sequential read, seconds-scale on the build server's volume, negligible
against a ~9-minute build). This is cheap, catches ANY residual failure mode
(including ones not yet imagined) before the zip, and directly discharges
the "silent 0-byte tar" hazard that made job `d844a5fb`'s failure so
expensive to diagnose. The guard is deliberately form-independent: it stays
correct even if the save form is ever changed again.

**Decision 4 — both sites fixed identically via one helper function defined
inside the changed region.** The helper (`save_image_tar <image> <dest>`)
keeps the two call sites trivially identical (Req 2.2's same-pass class
closure) and keeps the entire diff inside the save block: Req 3.1 demands
every OTHER line byte-for-byte, and the masked-golden mechanism is simplest
when the changed region is one contiguous block. The comment block (361-365)
is rewritten inside the same region to stay truthful (Req 3.1's explicit
carve-out): it now documents why NEITHER historical form is used and how the
temp-file hazard is layered against.

**Decision 5 — nothing else changes; zero golden regeneration.** No existing
golden or baseline embeds `build-custom.sh` bytes (bugfix.md scan), so —
unlike all three siblings — this fix regenerates NOTHING. The new test
package pins this as an invariant: the security suite, the sibling packages,
and `python_version_audit.py` must pass with bit-identical goldens. The four
scanned neighbor scripts are pinned byte-for-byte by sha256 goldens so the
"no other docker save site" scan verdict stays enforced mechanically.

### Changes Required

**File**: `build-custom.sh`

**Location**: The save block — comment lines 361-365 and save lines 366-367,
between the `echo "save docker images as tarvballs"` log anchor (line 360,
unchanged) and the `cp src/docker-compose.yaml` staging line (line 369,
unchanged).

**Specific Changes**:

1. **Replace the save block (the entire code fix)**:

   ```bash
   # before (lines 361-367)
   # Use stdout redirection rather than `docker save --output`. Under snap Docker,
   # `--output` writes a transient `.tmp-<name><rand>` file in the destination dir
   # and renames it; that temp file would briefly appear in the staging dir and
   # break the packaging `zip` (exit 18 "could not open for reading"). Redirecting
   # stdout lets the shell create the final file directly — no snap temp file.
   docker save flask-app > ./custom-build/$COMPONENT_NAME/flask-app.tar
   docker save react-webapp > ./custom-build/$COMPONENT_NAME/react-webapp.tar

   # after — docker's stdout is not involved at all, and the final tar name
   # only ever appears complete:
   # Save via `docker save --output` to a .partial name, then atomically mv
   # into place. NEITHER historical form works bare under snap Docker:
   #  - `docker save <img> > <file>` fails under the SSM RunShellScript
   #    context with "write /dev/stdout: bad file descriptor" (EBADF),
   #    leaving a 0-byte tar (portal job d844a5fb, 2026-08-09).
   #  - bare `--output <final-name>` writes a transient `.tmp-<name><rand>`
   #    in the destination dir and renames it, which raced the old recursive
   #    packaging zip (exit 18).
   # The .partial suffix + atomic same-dir mv means the final tar name only
   # ever exists complete; transient `.tmp-*`/`.partial` names are policed by
   # the pre-zip `rm -f .tmp-*` cleanup and the explicit ZIP_MEMBERS list
   # with its `-x '*/.tmp-*'` exclusion below. The integrity guard makes any
   # residual save failure loud — a 0-byte or truncated tar can never reach
   # the zip silently.
   save_image_tar() {
     local image=$1 dest=$2
     rm -f "$dest" "$dest.partial"
     if ! docker save --output "$dest.partial" "$image"; then
       echo "ERROR: docker save $image failed."
       echo "  docker: $(docker --version 2>/dev/null || echo 'version unavailable')"
       echo "  Staging dir:"
       ls -lh "$(dirname "$dest")" || true
       echo "  Disk free:"
       df -h "$(dirname "$dest")" || true
       exit 1
     fi
     mv "$dest.partial" "$dest"
     local size
     size=$(stat -c%s "$dest")
     if [ "$size" -lt 1048576 ] || ! tar -tf "$dest" > /dev/null; then
       echo "ERROR: saved image tar $dest failed integrity check (size ${size} bytes)."
       exit 1
     fi
     echo "Saved $image -> $dest ($size bytes, tar structure OK)"
   }
   save_image_tar flask-app ./custom-build/$COMPONENT_NAME/flask-app.tar
   save_image_tar react-webapp ./custom-build/$COMPONENT_NAME/react-webapp.tar
   ```

   The final destinations are the exact `ZIP_MEMBERS` paths (Req 3.2).
   Later line numbers shift by the added lines; all textual anchors in
   tests use content matching, never line numbers (sibling precedent).

2. **Nothing else in the file changes**: line 360's echo anchor, the staging
   `cp`/`mkdir` lines, the `rm -f .tmp-*` cleanup, the diagnostics block,
   the `ZIP_MEMBERS` list, the zip invocation with exclusion, the `zip -T`
   check, and the `greengrass-build` copy are all byte-for-byte preserved
   (enforced by the masked golden).

3. **No golden regeneration anywhere** (Decision 5): the commit contains the
   `build-custom.sh` edit and the new test package only.

4. **Same-commit rule**: the script edit and the new
   `test/backend-test/build_save_pkgs/` package ship in one commit, so the
   tree is self-consistent on either side of it (pure-git-revert rollback,
   matching sibling precedent).

5. **Untouched by design**: the four neighbor scripts, all Dockerfiles and
   compose files, `test/python_version_audit.py`, the security suite and its
   baselines, the three sibling test packages and their goldens, and all
   application code.

## Testing Strategy

### Validation Approach

Automated tests cannot run `docker save` against real multi-gigabyte images
(and the build server context cannot be reproduced locally), so validation is
layered, mirroring the proven sibling structure:

1. **Static/property tests** over the `build-custom.sh` text — a new
   `test/backend-test/build_save_pkgs/` package (exploration tests fail on
   the unfixed tree; observation-first preservation goldens captured
   pre-fix; Hypothesis property tests for the helpers). Import-light, runs
   under `pytest --noconftest` with
   `PYTHONPATH=src/backend:test/backend-test`, and parses the script as TEXT
   only — no `docker`, `subprocess`, or shell-out anywhere in the package.
2. **Existing suites re-run**: `test/python_version_audit.py`, the full
   docker security preservation suite, and the three sibling packages
   against the fixed tree — all green with zero golden changes.
3. **Approval-gated operational verification**: commit+push gate, then a
   live AMD64 dedicated portal build that must reach `succeeded` **including
   artifact publication** — the user-mandated completion criterion shared by
   all FOUR open specs in this chain.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix. Confirm or refute the root cause analysis. If we
refute, we re-hypothesize.

**Test Plan**: Write tests in
`test/backend-test/build_save_pkgs/test_bug_condition_exploration.py` that
parse `build-custom.sh` (logical line reconstruction, comment/string-aware
tokenization, save-form classification: STDOUT_REDIRECT vs OUTPUT_PARTIAL_MV
vs OTHER) and assert the expected CORRECT state — no bare stdout-redirect
`docker save` site, both sites in the fixed form. Run these tests on the
UNFIXED tree to observe the failures and pin the counterexamples to exactly
lines 366-367.

**Test Cases**:
1. **No stdout-redirect save site remains**: classify every `docker save`
   invocation site in `build-custom.sh`; assert zero sites of form
   STDOUT_REDIRECT (will FAIL on unfixed code — counterexamples: the
   flask-app and react-webapp sites, matching the live evidence)
2. **Both sites in the fixed form**: assert exactly two image-save call
   sites exist (flask-app, react-webapp), both invoking the shared helper,
   with final destinations exactly the two `ZIP_MEMBERS` tar paths (will
   FAIL on unfixed code)
3. **Fixed-form structure**: assert the helper contains, in order:
   `docker save --output` to a `"$dest.partial"` destination, an `mv` from
   `.partial` to the final name, a size guard with threshold 1048576, a
   `tar -tf` structural check, and non-zero exits on both failure paths
   (will FAIL on unfixed code, which has no helper)
4. **Counterexample inventory scoping**: assert the STDOUT_REDIRECT scan
   over the UNFIXED tree finds exactly TWO sites (lines 366-367's images) —
   confirming the bugfix.md scan and that the fix scope is exactly the save
   block (passes pre-fix as a scoping check; post-fix meaning: zero sites)
5. **Class boundary — no other docker save/export anywhere**: assert the
   four neighbor scripts contain zero `docker save`/`docker export`
   invocation sites (passes pre/post fix; anchors the Req 2.2 class
   closure)
6. **Temp-hazard guards coexist (Req 2.3)**: assert the pre-zip
   `rm -f ./custom-build/$COMPONENT_NAME/.tmp-*` cleanup, the explicit
   `ZIP_MEMBERS` array with both tar paths, and the `-x '*/.tmp-*'` zip
   exclusion are all present, and that the transient names the fixed form
   can produce (`.tmp-*`, `*.partial`) never appear in `ZIP_MEMBERS`
   (cleanup/exclusion assertions pass pre/post fix; the `.partial`
   non-membership assertion is trivially true pre-fix)
7. **Log anchor preserved**: assert the
   `echo "save docker images as tarvballs"` line survives verbatim — the
   live-log grep anchor for the verification build (passes pre/post fix)

**Expected Counterexamples**:
- Exactly two failing sites: the flask-app and react-webapp
  stdout-redirect saves — matching the live evidence (job `d844a5fb`'s
  EBADF at the flask-app site; the react-webapp site unreached under
  `set -e`)
- Possible refutations: the save lines were already changed, additional
  `docker save`/`docker export` sites exist, the zip-side guards are not as
  documented, or the comment block no longer matches its documented content
  — any of which sends us back to re-hypothesize

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the
fixed script produces the expected behavior.

**Pseudocode:**

```
FOR ALL site IN dockerSaveSites(buildCustomSh_fixed) DO
  ASSERT form(site) ≠ STDOUT_REDIRECT                        -- Req 2.1, 2.2
  ASSERT form(site) = OUTPUT_PARTIAL_MV                      -- the chosen form
END FOR

ASSERT COUNT(imageSaveCallSites(buildCustomSh_fixed)) = 2
ASSERT images(callSites) = {flask-app, react-webapp}          -- Req 2.2
ASSERT finalDest(flask-app site)  = "custom-build/$COMPONENT_NAME/flask-app.tar"
ASSERT finalDest(react-webapp site) = "custom-build/$COMPONENT_NAME/react-webapp.tar"
                                                              -- Req 3.2 landing paths

helper := saveHelper(buildCustomSh_fixed)
ASSERT usesFlag(helper, "--output")                           -- no stdout in any
ASSERT outputDest(helper) = "$dest.partial"                   -- context (Req 2.1, 2.4)
ASSERT atomicRename(helper) = mv("$dest.partial", "$dest")    -- Req 2.3
ASSERT sizeGuard(helper) = (size >= 1048576)                  -- Req 2.1, 2.5
ASSERT structureGuard(helper) = tarListingCheck("$dest")
ASSERT allFailurePaths(helper) exit non-zero WITH diagnostics -- belt-and-braces

-- temp-file hazard not reintroduced (Req 2.3):
ASSERT preZipCleanup(buildCustomSh_fixed) covers ".tmp-*"
ASSERT zipExclusion(buildCustomSh_fixed) = "*/.tmp-*"
ASSERT ZIP_MEMBERS ∩ {".tmp-*", "*.partial"} = EMPTY
ASSERT zipInvocation uses explicit ZIP_MEMBERS (no recursive dir scan)

-- token discipline (scans must not substring/comment-match):
ASSERT classify("# docker save flask-app > x.tar", in_comment) = NOT_A_SITE
ASSERT classify("docker save --output f.partial img") = OUTPUT_FORM
ASSERT classify("docker save img > f.tar") = STDOUT_REDIRECT
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed script produces the same result as the original.

**Pseudocode:**

```
ASSERT maskSaveBlock(buildCustomSh_fixed)
     = maskSaveBlock(buildCustomSh_original)                  -- Req 3.1
FOR ALL file IN [scripts/portal-build-agent.sh, publish-ecr-only.sh,
                 com.dda.InferenceUploader/build-and-publish.sh,
                 src/edgemlsdk/build.sh] DO
  ASSERT sha256(file_fixed) = sha256(file_original)           -- class boundary
END FOR
ASSERT ZIP_MEMBERS tar paths appear verbatim IN maskedView    -- Req 3.2
ASSERT pythonVersionAudit(fixedTree) = GREEN                  -- Req 3.3
ASSERT allExistingGoldens(fixedTree) bit-identical            -- Req 3.4
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because:
- It generates many test cases automatically across the input domain (here:
  synthetic shell-line sequences for the masking helper, synthetic
  `docker save` invocation variants for the save-form classifier)
- It catches edge cases that manual unit tests might miss (e.g. `docker
  save` text inside comments or strings, flag/argument orderings —
  `--output f img` vs `-o f img` vs `img --output f`, redirects with and
  without spacing, `.partial` vs `.tmp-*` name discipline)
- It provides strong guarantees that behavior is unchanged for all
  non-buggy inputs

**Test Plan**: Observe the UNFIXED tree first — capture goldens via a
capture-on-absent helper mirroring the sibling pattern into
`test/backend-test/build_save_pkgs/baselines/`:
`build_custom_save_masked.txt` (`build-custom.sh` with ONLY the save block —
the 361-365 comment block plus the two save lines — masked) and full-file
sha256 goldens for the four neighbor scripts. Goldens are FROZEN after
capture — never rebaselined by this spec. After the fix, the same tests
assert the masked view and all four sha256es are byte-for-byte identical —
proving exactly one contiguous block changed and no other script was
touched.

**Test Cases**:
1. **Non-save-block bytes of build-custom.sh**: capture masked view on the
   unfixed tree; assert identical after fix (this view contains the audit
   guard, edgemlsdk build, compose builds, gate block, staging-dir
   population, `.tmp-*` cleanup, diagnostics, `ZIP_MEMBERS`, zip + `zip -T`,
   and the greengrass copy — all thereby proven verbatim; Req 3.1, 3.5)
2. **Neighbor scripts untouched**: full-file sha256 goldens for the four
   scanned scripts identical pre/post fix (class boundary enforced
   mechanically)
3. **ZIP_MEMBERS tar paths intact**: assert both tar paths appear verbatim
   inside the masked (unchanged) region AND as the fixed sites' final
   destinations — the producer and consumer agree (Req 3.2)
4. **Mask exactness**: the masked view differs from the raw file by exactly
   the one contiguous save block (pre-fix: 5 comment lines + 2 save lines;
   post-fix: the new comment block + helper + 2 call lines), with the
   block's boundaries anchored on the unchanged line-360 echo above and the
   unchanged compose-`cp` line below — the mask cannot hide collateral
   edits

### Unit Tests

- Save-form classifier: STDOUT_REDIRECT vs OUTPUT_PARTIAL_MV vs OTHER;
  comment and string content never classifies as a site; both `--output`
  and `-o` spellings recognized; `docker save` with no redirect and no
  output flag classifies OTHER (never silently passes)
- Helper-structure parser: flag extraction, `.partial` destination
  discipline, `mv` source/target pairing, size-threshold literal, `tar -tf`
  presence, non-zero exit on both failure paths
- Masking helper: block-boundary anchoring on the echo line above and the
  compose-`cp` line below; masked region length sanity pre/post fix
- Zip-guard assertions: `ZIP_MEMBERS` parse, exclusion flag parse, cleanup
  glob parse
- Structural pins: `set -e` and `set -o pipefail` still at lines 2-3; the
  echo log anchor verbatim; no `docker export` anywhere

### Property-Based Tests

- **Save-form classifier property (Property 1)**: Hypothesis-generated
  `docker save` invocation variants (flag orderings, spacing, image names,
  redirect targets, comment/string wrapping) — the classifier returns
  STDOUT_REDIRECT iff the invocation is a real (non-comment, non-string)
  `docker save` whose image tar goes through a shell stdout redirect, and
  OUTPUT_PARTIAL_MV iff it is the fixed form (token discipline)
- **Masking preservation property (Property 2)**: for generated shell-line
  sequences containing zero or more marked save blocks between the two
  anchors, the masking helper removes exactly the block(s) and nothing else
  (mirrors the sibling masking-helper property pattern)
- **Tokenization totality property (Properties 1-2)**: for generated shell
  lines with random comments, strings, redirects, and continuations,
  classification is total and never throws — unknown constructs classify
  OTHER, never crash and never silently classify as the fixed form

### Integration Tests

Automated integration is limited by the no-docker-in-tests constraint; the
existing suites serve as the in-repo integration layer, and the live build
is the true integration test:

- Re-run `test/python_version_audit.py` against the fixed tree: green with
  `build-custom.sh` in scope (Req 3.3)
- Re-run the full docker security preservation suite
  (`test/backend-test/security/preservation/`, `--noconftest`) against the
  fixed tree: all goldens bit-identical — nothing this fix touches is
  covered by any baseline (Req 3.4)
- Re-run the three sibling packages (`backend_jammy_pkgs`,
  `edgemlsdk_cmake`, `edgemlsdk_pythondev`, `--noconftest`) against the
  fixed tree: all green, all goldens bit-identical (Req 3.4)
- Pre-build guard run per `.kiro/steering/builds.md` (out-of-scope guard +
  secrets guard) before dispatching the verification build

### Gated Live Verification (User-Mandated Completion Criterion)

Per bugfix.md: **the spec is complete only when an actual portal build
reaches `succeeded` including artifact publication.** Local/static validation
alone does NOT complete this spec — and for this spec in particular, the
chosen save form's behavior under snap+SSM is only fully provable live. Two
separately approval-gated steps, same shape as the siblings' tasks 6-7 (both
gates pre-authorized by the user for this chain but documented and
acknowledged separately):

1. **Gate 1 — commit + push**: builds sync from origin, so the fix is
   invisible to build servers until pushed. Target branch:
   `feature/workflow-triggers` (the user's standing branch decision from the
   sibling chain, where evidence job `d844a5fb`'s source_ref already
   points). Explicit acknowledgment before pushing.
2. **Gate 2 — live build**: exactly ONE AMD64 **dedicated** build on the
   existing X86 build server (the same shape as evidence job `d844a5fb`),
   source_ref `feature/workflow-triggers`, dispatched only after separate
   explicit acknowledgment, with the full steering preflight first (no
   concurrent build, no preservation-tracked drift, guard tests green,
   fleet/instance health, one-at-a-time).
3. **Monitoring**: track via the Build Log API / CloudWatch
   `/dda/portal-builds`. Confirm the job passes the former failure point:
   after `save docker images as tarvballs`, both `Saved <image> -> <dest>
   (<size> bytes, tar structure OK)` lines appear with plausible sizes
   (flask-app ~4.78 GB) and NO `bad file descriptor`; then the packaging
   diagnostics, `zip`, `zip -T`, and greengrass copy run green; and the
   three siblings' fixed steps still log clean (CMake 3.31.6 CACHED,
   `python-dev-is-python3` CACHED, the guarded libssl conditional skipping
   in 0.2s).
4. **Success criterion**: the job reaches `succeeded` **including artifact
   publication**. A build that fails later than the save/packaging steps is
   progress evidence, not completion.
5. **New-failure handling**: any follow-on failure past the fixed step is
   new evidence outside this spec's fix scope — record it in this spec's
   verification notes, route it to a follow-on spec (as this spec was
   itself routed from `backend-jammy-retired-packages`), and keep this spec
   open. A failure AT the fixed step (e.g. snap denying the `--output` open
   under SSM, contradicting the historical evidence) is a fix
   insufficiency: the helper's diagnostics are designed to make one job's
   logs conclusive, and the documented fallback is option (a)
   (`docker save <img> | cat > <file>`, safe under the script's existing
   `pipefail`) plus the SAME integrity guard — a deliberate re-design
   requiring user agreement, not an automatic retry.
6. **Shared completion — FOUR specs**: `edgemlsdk-cmake-pin-failure`,
   `edgemlsdk-python-dev-ubuntu2204`, and `backend-jammy-retired-packages`
   all remain open on the same criterion; a single `succeeded` AMD64 build
   with artifact publication satisfies all four specs' completion criteria
   simultaneously.

## Rollback Considerations

The fix is a **pure git revert**:

- All changes are text edits: one contiguous block in `build-custom.sh` plus
  the new test package under `test/backend-test/build_save_pkgs/`. No golden
  regeneration, no schema, data, or infrastructure migration. Reverting the
  fix commit restores the pre-fix script atomically; since no existing
  golden embeds the script, the preservation suites are consistent on
  either side of the revert.
- No runtime state depends on the change: `build-custom.sh` runs fresh from
  the origin checkout on each portal build; no deployed artifact embeds the
  fix until a build succeeds and publishes.
- If the live build refutes the chosen form (snap denies the `--output`
  file open in the SSM context), the fallback is NOT an automatic retry:
  per the new-failure handling above, the pipe form (option a) with the
  same integrity guard is the documented next candidate — a deliberate,
  user-agreed re-design with the failed job's diagnostics as evidence. The
  integrity guard and diagnostics survive any such re-design unchanged.
