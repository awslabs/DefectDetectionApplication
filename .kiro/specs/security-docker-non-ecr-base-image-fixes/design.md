# Docker Non-ECR Base Image Pinning & Parameterization (Group 6) Bugfix Design

## Overview

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan whose findings carry rubric rule id
`bosco/non-ecr-docker-image` — surfaced six HIGH-severity **Non-ECR Docker Base
Image** findings. This spec is the sixth remediation group (stacked after
`security-s3-bucket-squatting-fixes`); it is scoped **strictly** to findings
**D1–D6** across the four maintained Jetson Dockerfiles, plus the repo-audit
gate (D6).

The defect, across five `FROM` lines, is one shape: a container base image is
pulled from an **external, non-ECR registry** (`nvcr.io`, NVIDIA's NGC registry)
using a **mutable tag** with **no immutable `@sha256` digest pin** and **no
registry parameterization**. A mutable tag such as `r35.4.1` or `11.4.19-runtime`
is not content-addressed, so a compromised / MITM'd registry can repoint it to
tampered bytes that every build silently pulls; and because `nvcr.io` is
hardcoded in each `FROM`, an internal / air-gapped / ECR-mirrored build has no
seam to redirect the pull without editing the Dockerfile.

Per the user's **Option A** decision, the fix is **registry parameterization via
a build `ARG` + immutable digest pinning**, **NOT** a relocation of the images to
ECR. The in-scope base images are NVIDIA-proprietary Jetson (L4T / JetPack)
images with no public-ECR equivalent that DDA does not own and cannot re-host;
this is the Jetson-image analogue of the sibling S3 spec's "AWS-managed buckets
that DDA does not own are NOT renamed" decision — the registry `ARG` supplies the
override seam and the digest pin supplies tamper-resistance, rather than
relocating an asset DDA does not own (Option C rejected). Option B (digest pin
only, no registry seam) was rejected because it leaves the external registry
hardcoded with no override for internal / air-gapped builds.

For each in-scope `FROM` referencing `nvcr.io`, the minimal Option A change is:

1. **Registry `ARG`** — a single shared build arg `ARG BASE_REGISTRY=nvcr.io`,
   declared **before the first `FROM`** in each Dockerfile, so an internal build
   can `--build-arg BASE_REGISTRY=<internal-ecr-mirror>` without editing the
   file. The default `nvcr.io` preserves the current registry for the default
   build.
2. **Immutable digest pin** — pin each base image to its `@sha256` digest while
   **keeping** the human-readable tag, e.g.
   `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:<digest>`. The
   digests are exactly what the current mutable tags resolve to, so pinning is
   behavior-preserving (the same image bytes are pulled).

### The verified digests (multi-arch manifest-list digests)

The three digests below are the **manifest-list** digests the current mutable
tags resolve to on `nvcr.io`, so per-arch selection (the aarch64 Jetson manifest
is chosen at pull time exactly as today) is preserved:

| Image | Tag | Immutable digest |
|-------|-----|------------------|
| `l4t-jetpack` | `r35.4.1` | `sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e` |
| `l4t-jetpack` | `r36.3.0` | `sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa` |
| `l4t-cuda` | `11.4.19-runtime` | `sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78` |

**Re-verification step (implementation-time, REQUIRED before landing).** Before
each Dockerfile fix lands, the digest MUST be re-verified against `nvcr.io` so
that pinning is genuinely behavior-preserving, e.g.:

```
docker buildx imagetools inspect nvcr.io/nvidia/l4t-jetpack:r35.4.1
docker manifest inspect --verbose nvcr.io/nvidia/l4t-jetpack:r35.4.1
# (or: crane digest / skopeo inspect)
```

The pin MUST target the **same manifest-list digest** the current tag resolves to
(not a single per-arch child digest), so multi-arch selection is unchanged. If a
resolved digest differs from the value recorded above, the corrected digest is
recorded here before the fix lands (per the bugfix.md rollback commitment).

### The findings and their real source locations

| # | File | Line | `FROM` (current) | Fix |
|---|------|------|------------------|-----|
| D1 | `src/backend/Dockerfile.jp5` | 4 | `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1` | `ARG BASE_REGISTRY=nvcr.io` added among the existing lines 1–3 ARGs; `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971…b2a0e` |
| D2 | `src/edgemlsdk/Dockerfile.jp5` | 1 | `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1 AS builder` | insert `ARG BASE_REGISTRY=nvcr.io` as a NEW first line before the FROM; `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971…b2a0e AS builder` |
| D3 | `src/backend/Dockerfile.jp6` | 18 | `FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114` | `ARG BASE_REGISTRY=nvcr.io` added among lines 1–3 ARGs (before the comment block); `FROM ${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff08…8da78 AS cuda114` |
| D4 | `src/backend/Dockerfile.jp6` | 19 | `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0` | `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3…e2fa` — SHARES the single `BASE_REGISTRY` ARG declared before the first FROM (no re-declaration) |
| D5 | `src/edgemlsdk/Dockerfile.jp6` | 1 | `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0 AS builder` | insert `ARG BASE_REGISTRY=nvcr.io` as a NEW first line before the FROM; `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3…e2fa AS builder` |
| D6 | (in-scope Dockerfiles only) | — | No runnable check exists that asserts D1–D5 stay fixed | Add `test/backend-test/security/docker_base_image_audit.py` (two-layer gate) + wire into `build-custom.sh` as the fifth security gate |

### Docker ARG-scoping subtlety (CRITICAL — call out to the implementer)

Docker scopes an `ARG` declared **before the first `FROM`** to the `FROM`
instructions only — it is available for interpolation in **every** subsequent
`FROM` line **without re-declaration**, but it is NOT in scope inside a build
stage body (a `RUN`/`ENV`/etc. that referenced it would need a post-`FROM`
re-declaration).

In all four in-scope Dockerfiles, `BASE_REGISTRY` is used **only** in `FROM`
instructions, never in a stage body. Therefore:

- **A single `ARG BASE_REGISTRY=nvcr.io` before the first `FROM` is sufficient**
  for every `FROM` in the file. In `Dockerfile.jp6` this one declaration covers
  **both** D3 (line 18) and D4 (line 19).
- **No post-`FROM` re-declaration of `BASE_REGISTRY` is added.** The existing
  `ARG PYTHON_VERSION` re-declarations after the `FROM` (present in
  `Dockerfile.jp5` and `Dockerfile.jp6`) are for `PYTHON_VERSION`, which IS used
  in stage bodies — those stay exactly as they are. Adding a spurious
  `BASE_REGISTRY` re-declaration would be an unnecessary line change that widens
  the diff and weakens the preservation argument, so the implementer MUST NOT add
  one.

### Scope of change per file (emphasis)

**ONLY the `FROM` lines change, plus exactly one `ARG BASE_REGISTRY=nvcr.io` line
added per file (D3 and D4 share one, so `Dockerfile.jp6` gets one added ARG line
covering both its `FROM`s).** Every other line in every in-scope Dockerfile is
**byte-for-byte unchanged**.

### Out of scope

- The vendored / generated duplicate `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5|jp6`
  — regenerated from `src/edgemlsdk/...` by `build-custom.sh`; contains identical
  `nvcr.io` `FROM` lines but is NOT hand-maintained and is overwritten each build.
  Only D1–D5 (the maintained source paths) are fixed.
- The already-compliant `public.ecr.aws/*` Dockerfiles — `src/backend/Dockerfile`,
  `src/frontend/Dockerfile`, `src/edgemlsdk/Dockerfile` — already pull from AWS
  ECR Public; digest-pinning them is possible future hardening but is out of scope
  for the *non-ECR* finding class.
- Relocating the Jetson images to ECR (Option C) — deliberately not done.
- `docker-compose.yaml` / build-invocation changes — `ARG BASE_REGISTRY=nvcr.io`
  defaults, so no compose change is required for the default build.
- The already-remediated batches (injection #1–#8, secrets S1–S9, IAM I1–I17, S3
  B1–B7) — all their fixes MUST remain byte-for-byte unchanged.

## Glossary

- **Bug_Condition (C)**: A base-image `FROM` line in an in-scope Dockerfile that
  references a non-ECR registry (`nvcr.io`) and is NOT both registry-parameterized
  (via a `${BASE_REGISTRY...}` ARG) AND digest-pinned (`@sha256:`).
- **Property (P) / Fix Checking**: After the fix, every in-scope `FROM` is
  registry-parameterized via `${BASE_REGISTRY}` (with `ARG BASE_REGISTRY=nvcr.io`
  before the first `FROM`) AND digest-pinned with `@sha256:`, retaining its tag
  and any `AS <stage>` name.
- **Preservation**: For every input that does NOT trigger the bug condition (the
  default build with `BASE_REGISTRY` unset/`nvcr.io`, every non-`FROM` line, the
  multi-stage structure, the ECR Dockerfiles, and the vendored duplicate), the
  fixed code behaves identically to the original — `F(X) = F'(X)`.
- **Non-ECR base image**: a base image pulled from a registry other than AWS ECR
  (public or private). Here the external registry is `nvcr.io` (NVIDIA NGC).
- **Mutable tag**: a human-readable image reference (e.g. `r35.4.1`) that is NOT
  content-addressed and can be repointed by the registry to different bytes.
- **Immutable digest (`@sha256:...`)**: a content-addressed reference; the same
  digest always resolves to exactly the same bytes or the pull fails.
- **Manifest-list digest**: the multi-arch index digest a tag resolves to; pinning
  to it (not a per-arch child) preserves the per-architecture selection.
- **Registry parameterization / seam**: the `ARG BASE_REGISTRY` that lets a build
  redirect the registry portion of a `FROM` to a trusted mirror without editing
  the Dockerfile.
- **ARG scoping (before-first-FROM)**: an `ARG` declared before the first `FROM`
  is usable for interpolation in ALL subsequent `FROM` lines without
  re-declaration, but is NOT in scope inside stage bodies. `BASE_REGISTRY` is used
  only in `FROM`s, so one before-first-`FROM` declaration suffices per file.
- **In-scope Dockerfile**: one of the four maintained Jetson Dockerfiles —
  `src/backend/Dockerfile.jp5`, `src/edgemlsdk/Dockerfile.jp5`,
  `src/backend/Dockerfile.jp6`, `src/edgemlsdk/Dockerfile.jp6`.
- **F / F'**: the original (unfixed) Dockerfile, where the `FROM` references
  `nvcr.io` directly with a mutable tag, no digest, no `ARG`; and the fixed
  Dockerfile, where every in-scope `FROM` is `${BASE_REGISTRY}`-parameterized
  (default `nvcr.io`) and `@sha256`-pinned with the tag and any `AS <stage>` name
  retained.

## Bug Details

### Bug Condition

The bug manifests on any base-image `FROM` line in an in-scope Dockerfile that
pulls from the external, non-ECR registry `nvcr.io` and is either not
registry-parameterized (the registry is a literal, not `${BASE_REGISTRY...}`) or
not pinned to an immutable `@sha256` digest. The five sites are the `FROM` lines
D1–D5 (see the findings table): `Dockerfile.jp5:4`, `Dockerfile.jp5:1`
(edgemlsdk), `Dockerfile.jp6:18` + `:19` (backend), and `Dockerfile.jp6:1`
(edgemlsdk).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type FromInstruction   // a FROM line in an in-scope Dockerfile
                                     // (src/backend/Dockerfile.jp5|jp6,
                                     //  src/edgemlsdk/Dockerfile.jp5|jp6)
  OUTPUT: boolean

  // True when a base image is pulled from an external, non-ECR registry
  // AND is not registry-parameterized via a ${BASE_REGISTRY...} ARG
  // AND/OR is not pinned to an immutable @sha256 digest:
  RETURN nonEcrRegistry(X)               // references nvcr.io, not public.ecr.aws
                                         // / *.dkr.ecr.*.amazonaws.com
      AND ( NOT registryParameterized(X) // registry is a literal, not ${BASE_REGISTRY...}
            OR NOT digestPinned(X) )      // no @sha256:<digest> pin
END FUNCTION
```

For the current tree `isBugCondition` is TRUE for D1–D5 (each references `nvcr.io`
literally, with a mutable tag and no digest).

**Expected behavior for buggy inputs (Fix Checking):**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := F'(X)
  ASSERT registryParameterized(result)
     // registry portion is ${BASE_REGISTRY} with ARG BASE_REGISTRY=nvcr.io
     // declared before the first FROM, so a build can redirect to an ECR mirror
     // without editing the file.
  ASSERT digestPinned(result)
     // the reference carries an immutable @sha256:<digest> pin so a repointed
     // mutable tag cannot silently change the pulled bytes.
  ASSERT tagRetained(result) AND stageNamePreserved(result)
     // the tag (r35.4.1 / r36.3.0 / 11.4.19-runtime) is kept alongside the
     // digest, and any AS <stage> name (builder / cuda114) is unchanged so
     // multi-stage COPY --from references still resolve.
END FOR
```

### Examples

Bug manifestation on unfixed code (verified against the actual file contents):

- **D1** — `src/backend/Dockerfile.jp5` line 4 is `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1`,
  preceded by `ARG OS` / `ARG PLATFORM` / `ARG PYTHON_VERSION` on lines 1–3 and
  followed by a `# Re-declare after FROM` + `ARG PYTHON_VERSION`. Literal `nvcr.io`,
  mutable tag, no digest, no registry ARG. **Expected after fix:** an added
  `ARG BASE_REGISTRY=nvcr.io` among lines 1–3 and
  `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e`.
- **D2** — `src/edgemlsdk/Dockerfile.jp5` line 1 is the FIRST line,
  `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1 AS builder` (the `ARG OS`/`PLATFORM`/…
  come AFTER it). **Expected after fix:** a NEW first line `ARG BASE_REGISTRY=nvcr.io`
  inserted BEFORE the FROM, then
  `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971…b2a0e AS builder`
  (the `AS builder` name preserved).
- **D3** — `src/backend/Dockerfile.jp6` line 18 is
  `FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114`, at the end of the
  CUDA-11.4-provider comment block; lines 1–3 are `ARG OS`/`PLATFORM`/`PYTHON_VERSION`.
  **Expected after fix:** `ARG BASE_REGISTRY=nvcr.io` added among lines 1–3, and
  `FROM ${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78 AS cuda114`
  (the `AS cuda114` name preserved so the later `COPY --from=cuda114` resolves).
- **D4** — `src/backend/Dockerfile.jp6` line 19 is `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0`
  (the final runtime stage, immediately after D3). **Expected after fix:**
  `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa`
  — sharing the single `BASE_REGISTRY` ARG declared before the first `FROM`
  (line 18), with no re-declaration.
- **D5** — `src/edgemlsdk/Dockerfile.jp6` line 1 (FIRST line) is
  `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0 AS builder`. **Expected after fix:** a
  NEW first line `ARG BASE_REGISTRY=nvcr.io` before the FROM, then
  `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3…e2fa AS builder`.

Edge cases (preserved, NOT buggy):

- The `public.ecr.aws/ubuntu/ubuntu:${OS}` / `public.ecr.aws/docker/library/node:18-alpine`
  / `public.ecr.aws/nginx/nginx:stable-alpine` `FROM`s in `src/backend/Dockerfile`,
  `src/frontend/Dockerfile`, `src/edgemlsdk/Dockerfile` — already ECR; `isBugCondition`
  is false (`nonEcrRegistry` is false). Unchanged.
- The vendored `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5|jp6` `FROM` lines —
  identical text but out of scope (regenerated); the gate does not scan them.
- The `ARG PYTHON_VERSION` re-declarations after the `FROM` in jp5/jp6 backend —
  for `PYTHON_VERSION` (used in stage bodies), not `BASE_REGISTRY`; unchanged.
- Any `FROM` already carrying `${BASE_REGISTRY...}` + `@sha256:` (post-fix) —
  cleared.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The default build (with `BASE_REGISTRY` unset or `nvcr.io`) pulls the SAME image
  bytes as before — the pinned `@sha256` digest is exactly the manifest-list
  digest each current mutable tag resolves to, so `nvcr.io/nvidia/<image>:<tag>@sha256:<digest>`
  is equivalent in effect to the current `nvcr.io/nvidia/<image>:<tag>`, with
  per-arch selection unchanged.
- Every non-`FROM` line in all four Dockerfiles is byte-for-byte identical: the
  `ARG OS`/`PLATFORM`/`PYTHON_VERSION` declarations, the post-`FROM`
  `ARG PYTHON_VERSION` re-declarations, the py3compile disable/restore, the apt
  purge/install phases, the toolchain PPA + gcc-11/g++-11 setup, the
  Python-from-source (jp5) / deadsnakes 3.11 (jp6) install, the awscrt `0.14.7` /
  aws-lc linker workaround, the ONNX runtime build args, the aravis build, the
  `grpc_tools.protoc` step, the CUDA 11.4 `COPY --from=cuda114` staging, the
  `COPY app.py …` / `COPY` app steps, `CMD`, the edgemlsdk install, and `USER root`.
- The multi-stage structure is preserved: the `AS builder` names (D2, D5) and the
  `AS cuda114` name (D3), so `COPY --from=cuda114 /usr/local/cuda-11.4/…` still
  resolves exactly as before.
- An internal build with `--build-arg BASE_REGISTRY=<mirror>` that hosts the same
  digest-pinned images produces an equivalent build, pulling identical bytes (same
  `@sha256` digest) from the mirror.
- No `docker-compose.yaml` or build-invocation change is required for the default
  build (`ARG BASE_REGISTRY=nvcr.io` supplies the default registry).

**Scope:**
All inputs that do NOT trigger the bug condition must be completely unaffected.
This explicitly includes:
- The default build (`BASE_REGISTRY` unset/`nvcr.io`) of all four Dockerfiles.
- The already-compliant `public.ecr.aws/*` Dockerfiles (`src/backend/Dockerfile`,
  `src/frontend/Dockerfile`, `src/edgemlsdk/Dockerfile`).
- The vendored `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5|jp6` duplicate.
- Every finding from the sibling remediation batches (#1–#8, S1–S9, I1–I17,
  B1–B7), already remediated on separate branches.

**Note:** the expected correct behavior for buggy inputs is defined in the
Correctness Properties section (Property 1); this section focuses on what must
NOT change.

## Hypothesized Root Cause

The Jetson Dockerfiles were written for a build context where `nvcr.io` was the
canonical, reachable source of the NVIDIA L4T / JetPack base images, and "pull the
tag that works" was sufficient. Concretely:

1. **Registry hardcoded into each `FROM`.** `nvcr.io` is embedded directly in
   every Jetson `FROM`, with no notion that an internal / air-gapped / ECR-mirrored
   build would need to redirect the registry — so there is no `ARG` seam.

2. **Mutable tag assumed stable.** The base images were referenced by their
   human-readable tag (`r35.4.1`, `r36.3.0`, `11.4.19-runtime`) on the assumption
   the tag is a stable identifier. A tag is not content-addressed, so a repointed /
   tampered upstream tag would be pulled silently; the author did not pin a digest.

3. **Multi-stage `FROM`s share no arg.** In `Dockerfile.jp6` the two `FROM`s (the
   `cuda114` provider stage and the final runtime stage) were each written as
   standalone literals; there was no shared registry variable, so both hardcode
   `nvcr.io`.

4. **No regression gate (D6).** Nothing asserts the base-image references stay
   parameterized + pinned, so a future edit (or a `git checkout` reverting a fix)
   could reintroduce a non-ECR, unparameterized, unpinned `FROM` with no automated
   detection.

## Correctness Properties

Property 1: Bug Condition — Every in-scope non-ECR base image is registry-parameterized AND digest-pinned

_For any_ base-image `FROM` in an in-scope Dockerfile where the bug condition holds
(`isBugCondition` returns true — a `FROM` referencing the non-ECR registry
`nvcr.io` that is not both `${BASE_REGISTRY...}`-parameterized and `@sha256`-pinned),
the fixed Dockerfile SHALL make that `FROM` registry-parameterized AND
digest-pinned:

- The registry portion SHALL be `${BASE_REGISTRY}` with `ARG BASE_REGISTRY=nvcr.io`
  declared before the first `FROM` (declared once per file; in `Dockerfile.jp6` the
  single declaration is shared by both `FROM`s per Docker's before-first-`FROM` ARG
  scoping — no re-declaration is added).
- The reference SHALL carry an immutable `@sha256:<digest>` pin equal to the
  manifest-list digest the current mutable tag resolves to.
- The human-readable tag (`r35.4.1` / `r36.3.0` / `11.4.19-runtime`) SHALL be
  retained alongside the digest, and any `AS <stage>` name (`builder` / `cuda114`)
  SHALL be preserved so multi-stage `COPY --from` references still resolve.

A full-repo audit over the four in-scope Dockerfiles finds no remaining disallowed
`FROM` — no `FROM` referencing a non-ECR literal registry that is not both
parameterized and digest-pinned — other than lines carrying a documented `# nosec`
exception.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

Property 2: Preservation — No behavior change for the default build

_For any_ `FROM` (or Dockerfile line) where the bug condition does NOT hold
(`isBugCondition` returns false — the default build with `BASE_REGISTRY`
unset/`nvcr.io`, every already-compliant ECR `FROM`, and every non-`FROM` line),
the fixed code SHALL produce the same result as the original code (`F(X) = F'(X)`),
preserving: the default build pulling identical image bytes (the pinned digest is
exactly the digest the current tag resolves to, so the same manifest-list is pulled
and per-arch selection is unchanged); every non-`FROM` line in all four Dockerfiles
byte-for-byte identical (ARG declarations including the post-`FROM`
`ARG PYTHON_VERSION`, apt phases, Python/toolchain builds, awscrt/aws-lc workaround,
ONNX/aravis/Triton builds, the `COPY --from=cuda114` cudart staging, the `COPY` app
steps, `CMD`, `USER root`); the multi-stage `AS builder` / `AS cuda114` names and
the `COPY --from=cuda114` reference still resolving; no `docker-compose.yaml` change
required; and the already-compliant `public.ecr.aws/*` Dockerfiles plus the vendored
`src/backend/edgemlsdk/edgemlsdk/…` duplicate remaining unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, each in-scope Dockerfile gets the
minimal change that makes `isBugCondition` false for its `FROM`(s) while preserving
`F(X) = F'(X)`. In every case: **only the `FROM` line(s) change, plus exactly one
`ARG BASE_REGISTRY=nvcr.io` line is added** (shared by both `FROM`s in
`Dockerfile.jp6`). Every other line is byte-for-byte unchanged, and **no post-`FROM`
`BASE_REGISTRY` re-declaration is added** (`BASE_REGISTRY` is used only in `FROM`s).

Before landing each file, **re-verify the digest** against `nvcr.io`
(`docker buildx imagetools inspect` / `docker manifest inspect --verbose`) and
confirm it is the multi-arch manifest-list digest (§ Overview → re-verification).

#### D1 — `src/backend/Dockerfile.jp5` (Req 2.1)

**Current (lines 1–5):**
```dockerfile
ARG OS
ARG PLATFORM
ARG PYTHON_VERSION
FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1
# Re-declare after FROM so the build arg is available in the build stage.
```

**Fixed:** add `ARG BASE_REGISTRY=nvcr.io` among the existing top ARGs (before the
first `FROM`) and parameterize + pin the `FROM`:
```dockerfile
ARG OS
ARG PLATFORM
ARG PYTHON_VERSION
ARG BASE_REGISTRY=nvcr.io
FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e
# Re-declare after FROM so the build arg is available in the build stage.
```
The subsequent `ARG PYTHON_VERSION` re-declaration and everything below are
unchanged.

#### D2 — `src/edgemlsdk/Dockerfile.jp5` (Req 2.2)

**Current (line 1 is the FIRST line):**
```dockerfile
FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1 AS builder
ARG OS
ARG PLATFORM
```

**Fixed:** insert `ARG BASE_REGISTRY=nvcr.io` as a NEW first line before the
`FROM`, preserving the `AS builder` stage name:
```dockerfile
ARG BASE_REGISTRY=nvcr.io
FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e AS builder
ARG OS
ARG PLATFORM
```
The `ARG OS`/`PLATFORM`/`PWSH_ARCH`/`PYTHON_VERSION` block that already follows the
`FROM` and everything below are unchanged.

#### D3 + D4 — `src/backend/Dockerfile.jp6` (Req 2.3, 2.4) — one shared ARG, two FROMs

**Current (lines 1–3 top ARGs; lines 18–19 the two FROMs, after the comment block):**
```dockerfile
ARG OS
ARG PLATFORM
ARG PYTHON_VERSION
# ── CUDA 11.4 cudart provider (for the Neo/DLR model runtime) ──────────────
# … (comment block) …
FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114
FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0
# Re-declare after FROM so the build arg is available in the build stage.
ARG PYTHON_VERSION
```

**Fixed:** add a SINGLE `ARG BASE_REGISTRY=nvcr.io` among the top ARGs (before the
first `FROM`, before the comment block). Per Docker's before-first-`FROM` ARG
scoping, this one declaration is in scope for BOTH `FROM`s — **no re-declaration**.
Parameterize + pin both, preserving the `AS cuda114` name:
```dockerfile
ARG OS
ARG PLATFORM
ARG PYTHON_VERSION
ARG BASE_REGISTRY=nvcr.io
# ── CUDA 11.4 cudart provider (for the Neo/DLR model runtime) ──────────────
# … (comment block, byte-for-byte unchanged) …
FROM ${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78 AS cuda114
FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa
# Re-declare after FROM so the build arg is available in the build stage.
ARG PYTHON_VERSION
```
The `AS cuda114` name is preserved so the later
`COPY --from=cuda114 /usr/local/cuda-11.4/targets/aarch64-linux/lib/ …` cudart
staging and the `ldconfig` / `libcudart.so.11` verification block resolve exactly
as before. The post-`FROM` `ARG PYTHON_VERSION` re-declaration (for
`PYTHON_VERSION`, used in the stage body) is untouched. **Do not** add a
`BASE_REGISTRY` re-declaration.

#### D5 — `src/edgemlsdk/Dockerfile.jp6` (Req 2.5)

**Current (line 1 is the FIRST line):**
```dockerfile
FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0 AS builder
ARG OS
ARG PLATFORM
```

**Fixed:** insert `ARG BASE_REGISTRY=nvcr.io` as a NEW first line before the
`FROM`, preserving `AS builder`:
```dockerfile
ARG BASE_REGISTRY=nvcr.io
FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa AS builder
ARG OS
ARG PLATFORM
```
Everything below is unchanged.

#### D6 — Repo audit gate (Req 2.6) — `test/backend-test/security/docker_base_image_audit.py`

Add a companion audit module mirroring the sibling `s3_squat_audit.py` /
`iam_audit.py` two-layer shape, and wire it into `build-custom.sh` as a fifth gate.
Details in Testing Strategy → Repo-audit design.

### Ordering and risk (least blast radius first)

Each in-scope Dockerfile is a **separate task**, so a break can be reverted in
isolation (per the bugfix.md rollback commitment). Suggested order:

1. **jp5 edgemlsdk (D2)** and **jp6 edgemlsdk (D5)** — single-`FROM` builder-stage
   files; the simplest, lowest-coupling change (one added ARG line + one FROM).
2. **jp5 backend (D1)** — single `FROM`, ARG added to the existing top block.
3. **jp6 backend (D3 + D4)** — highest-coupling: two `FROM`s sharing one ARG and a
   `COPY --from=cuda114` cross-stage dependency. Land last so the multi-stage
   resolution can be verified after the simpler files are green.

**Highest-risk areas to watch:**
- **Digest correctness / manifest-list.** A wrong or per-arch (non-manifest-list)
  digest would either break the pull or change per-arch selection. Re-verify each
  digest against `nvcr.io` immediately before landing.
- **`AS cuda114` preservation (D3).** If the stage name is dropped or changed, the
  later `COPY --from=cuda114` fails to resolve and the CUDA 11.4 cudart staging
  breaks. Preserve it exactly.
- **No spurious `BASE_REGISTRY` re-declaration.** Adding a post-`FROM`
  re-declaration would widen the diff and weaken the byte-for-byte preservation
  argument; `BASE_REGISTRY` is used only in `FROM`s, so none is needed.
- **Internal-mirror availability.** If an operator sets
  `--build-arg BASE_REGISTRY=<mirror>` that lacks the digest-pinned image, the pull
  fails closed — recoverable by unsetting the arg (default `nvcr.io`).

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate the non-ECR /
unparameterized / unpinned `FROM` pattern on the **unfixed** tree (repo audit +
the exploration test), then verify the fix **parameterizes + pins** every buggy
`FROM` (Fix Checking) and **preserves** behavior for every non-buggy input
(Preservation Checking, `F(X) = F'(X)`). Property-based testing (Hypothesis — the
repo already vendors `.hypothesis/`) is emphasized where the input domain is
generatable: `BASE_REGISTRY` resolution (unset/default vs. internal-mirror
override), and arbitrary `FROM` lines classified by `disallowed_hits()`.

### Repo-audit design (Req 2.6 / D6)

**Decision: add a companion `test/backend-test/security/docker_base_image_audit.py`**
rather than extend the sibling gates (`repo_audit.py`, `secrets_audit.py`,
`iam_audit.py`, `s3_squat_audit.py`). Rationale (least-duplicative, mirrors the S3
gate's rationale): the gates own different patterns and in-scope file sets; editing
an existing gate would entangle five specs' assertions and risk regressing green
gates. To avoid duplication, `docker_base_image_audit.py` **reuses the sibling
low-level primitives** — `REPO_ROOT`, `EXCLUDED_PATH_SUBSTRING`, `Hit`, `_has_nosem`
— via the SAME `try/except` fallback re-implementation pattern the siblings use when
`repo_audit` is not importable, and defines only its OWN constants and precise
`disallowed_hits()` logic. It mirrors the siblings' two-layer shape: a raw
`run_audit()` broad enumeration (non-empty on the unfixed tree, used by the
exploration test) and a precise `disallowed_hits()` gate (zero after fix, minus
documented `# nosec` exceptions).

**`IN_SCOPE_FILES`** (relative to `REPO_ROOT`) — the four maintained Jetson
Dockerfiles, excluding the vendored duplicate and non-Dockerfiles:
```python
BACKEND_JP5_REL   = os.path.join("src", "backend",   "Dockerfile.jp5")
EDGEMLSDK_JP5_REL = os.path.join("src", "edgemlsdk", "Dockerfile.jp5")
BACKEND_JP6_REL   = os.path.join("src", "backend",   "Dockerfile.jp6")
EDGEMLSDK_JP6_REL = os.path.join("src", "edgemlsdk", "Dockerfile.jp6")
IN_SCOPE_FILES = frozenset(os.path.normpath(p) for p in (
    BACKEND_JP5_REL, EDGEMLSDK_JP5_REL, BACKEND_JP6_REL, EDGEMLSDK_JP6_REL,
))
```

**`VENDORED_DUP_SUBSTRING`** (defensive exclusion of the regenerated duplicate,
mirroring the S3 gate):
```python
VENDORED_DUP_SUBSTRING = os.path.join("edgemlsdk", "edgemlsdk")
```

**`ALLOWED_REGISTRY` markers** — a `FROM` registry is allowed if it is an ECR host
OR the parameterized `${BASE_REGISTRY...}` seam:
```python
# ECR hosts: public.ecr.aws and *.dkr.ecr.<region>.amazonaws.com
_ECR_HOST_RE = re.compile(
    r"(?:^|/)(?:public\.ecr\.aws|[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com)/")
# The registry-parameterization seam (default value may legitimately be nvcr.io).
_PARAMETERIZED_REGISTRY_RE = re.compile(r"\$\{?BASE_REGISTRY[:}]?")
# An @sha256 immutable digest pin.
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")
```

**RAW enumeration** (`run_audit()`): scan every in-scope file for every
non-empty, non-comment `FROM` line and emit a `Hit` per `FROM` (a
`FROM <ref> [AS <stage>]` occurrence). This is deliberately broad — it lists all
five in-scope `FROM`s (D1–D5) so the exploration test can enumerate the
counterexamples. It applies NO scoping/precision filtering beyond `IN_SCOPE_FILES`
and never scans the vendored duplicate or non-Dockerfiles.

**Precise gate** (`disallowed_hits()`): a `FROM` is *disallowed* only when it is in
`IN_SCOPE_FILES`, is not comment-only, carries no `# nosec` marker, and is a
**non-compliant non-ECR base image**, i.e.:

```
FUNCTION isDisallowedFrom(fromLine)
  ref := the image reference portion of the FROM (after FROM, before AS)
  IF _has_nosem(fromLine): RETURN False               # documented exception
  IF _ECR_HOST_RE matches ref: RETURN False           # ECR / public.ecr.aws → cleared
  IF _PARAMETERIZED_REGISTRY_RE matches ref
     AND _DIGEST_RE matches ref: RETURN False          # ${BASE_REGISTRY}+@sha256 → cleared
  # Anything else that references a non-ECR literal registry OR lacks a digest:
  RETURN referencesNonEcrRegistry(ref)
     AND ( NOT _PARAMETERIZED_REGISTRY_RE matches ref
           OR NOT _DIGEST_RE matches ref )
END FUNCTION
```

**Critical parsing subtlety — the `${BASE_REGISTRY}` + `@sha256` case must be
recognized as compliant even though its default still transitively contains
`nvcr.io`.** The point of Option A is the ARG **seam** plus the **digest**, not the
absence of the string `nvcr.io` — `ARG BASE_REGISTRY=nvcr.io` legitimately keeps
`nvcr.io` as the default value. So the gate MUST clear a `FROM` of the form
`FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:…` (parameterized registry
+ digest) and MUST NOT naively flag it just because `nvcr.io` appears elsewhere in
the file as the ARG default. The gate keys on the `FROM` **reference string**
containing `${BASE_REGISTRY` (parameterized) AND `@sha256:` (pinned); a literal
`nvcr.io/…` in the `FROM` ref with no `${BASE_REGISTRY}` (or no `@sha256:`) is
disallowed.

**Negative-fixture concept (proves the gate isn't a file-global check).** A test
constructs a fixture by taking a fixed in-scope Dockerfile and reverting ONE `FROM`
to the literal-`nvcr.io`, no-digest form (leaving the `ARG BASE_REGISTRY=nvcr.io`
line in place) and asserts `disallowed_hits()` re-flags exactly that `FROM`. This
proves the gate parses each `FROM` reference individually — the mere presence of
`ARG BASE_REGISTRY=nvcr.io` (or of a compliant `FROM` elsewhere) does NOT satisfy
the gate for a reverted `FROM`.

**Two-layer API** (matching sibling gates):
- `run_audit()` — raw broad enumeration of every in-scope `FROM`; non-empty on the
  unfixed tree (exploration test). Never scans the vendored duplicate or
  non-Dockerfiles.
- `disallowed_hits()` — precise post-fix gate; applies `IN_SCOPE_FILES` scoping,
  `_has_nosem` exceptions, and the per-`FROM` ECR / `${BASE_REGISTRY}`+`@sha256`
  classification. Returns `[]` after the fix; the `__main__` block exits non-zero if
  any element remains.

**CI wiring**: add the new gate as a **fifth** block in `build-custom.sh`,
immediately after the S3 bucket-squatting gate (the `Security S3 bucket-squatting
audit gate` region, ~line 274–279), under the same `set -e`-guarded backend-test
block so a non-zero exit fails the build:
```sh
echo "Running security Docker non-ECR base image audit gate..."
python${PYTHON_VERSION} test/backend-test/security/docker_base_image_audit.py
python${PYTHON_VERSION} -m pytest \
  test/backend-test/security/test_docker_base_image_bug_condition_exploration.py \
  test/backend-test/security/test_docker_audit_gate_negative_fixture.py -v
echo "Security Docker non-ECR base image audit gate passed."
```
(The shared `security/preservation` suite is already run by the Group-1 gate; the
Docker preservation tests below live under it as `test_preservation_docker_*`.)

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the non-ECR / unparameterized /
unpinned `FROM` pattern BEFORE the fix, and confirm/refute the root-cause analysis.
If refuted, re-hypothesize.

**Test Plan**: Write
`test/backend-test/security/test_docker_base_image_bug_condition_exploration.py` to
observe the UNFIXED counterexample shape — each in-scope `FROM` is a literal
`nvcr.io` reference with no `${BASE_REGISTRY}` ARG and no `@sha256:` digest. Written
as invariants that **PASS on the unfixed tree** and are **FLIPPED to the secure
invariants after the fix** (the same task-1/task-7 pattern as the S3 batch's
`test_s3_squat_bug_condition_exploration.py`), plus a direct gate assertion.

**Test Cases**:
1. **D1 jp5 backend**: assert the `FROM` on `src/backend/Dockerfile.jp5:4` is a
   literal `nvcr.io/nvidia/l4t-jetpack:r35.4.1` with no `${BASE_REGISTRY}` / no
   `@sha256:` (passes on unfixed). Flip after fix: asserts `${BASE_REGISTRY}` +
   `@sha256:` + `r35.4.1` retained.
2. **D2 jp5 edgemlsdk**: same shape on `src/edgemlsdk/Dockerfile.jp5:1`, `AS builder`
   present. Flip: parameterized + pinned + `AS builder` preserved.
3. **D3 jp6 backend cuda114**: `FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114`
   on `:18`. Flip: parameterized + pinned + `AS cuda114` preserved.
4. **D4 jp6 backend runtime**: `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0` on `:19`.
   Flip: parameterized + pinned; and assert exactly ONE `ARG BASE_REGISTRY` in the
   file (shared by both `FROM`s, no re-declaration).
5. **D5 jp6 edgemlsdk**: `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0 AS builder` on `:1`.
   Flip: parameterized + pinned + `AS builder` preserved.
6. **`test_docker_audit_returns_no_disallowed_hits`**: assert
   `docker_base_image_audit.disallowed_hits() == []`. FAILS on the unfixed tree
   (five disallowed `FROM`s) and is GREEN after the fix. Also assert
   `run_audit()` is non-empty (enumerates the five in-scope `FROM`s) and excludes
   the vendored duplicate.
7. **Negative-fixture test** (`test_docker_audit_gate_negative_fixture.py`): revert
   one file's fix in an in-memory fixture (literal `nvcr.io`, no digest, ARG line
   still present) and assert `disallowed_hits()` re-flags exactly that `FROM` —
   proving the gate is per-`FROM`, not file-global.

**Expected Counterexamples**:
- `run_audit()` enumerates five in-scope `FROM`s; `disallowed_hits()` flags all five
  (each a literal `nvcr.io` reference, no `${BASE_REGISTRY}`, no `@sha256:`).
- Confirmation of the root cause: registry hardcoded (no ARG seam) + mutable tag (no
  digest) on every Jetson `FROM`; the ECR `public.ecr.aws/*` `FROM`s are NOT flagged.

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed code
parameterizes + pins.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT registryParameterized(result)
     AND digestPinned(result)
     AND tagRetained(result) AND stageNamePreserved(result)
END FOR
```

Concretely:
- **D1**: `src/backend/Dockerfile.jp5` declares `ARG BASE_REGISTRY=nvcr.io` before
  the first `FROM` and the `FROM` is
  `${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971…b2a0e` (Req 2.1).
- **D2**: `src/edgemlsdk/Dockerfile.jp5` has `ARG BASE_REGISTRY=nvcr.io` as the new
  first line and a parameterized + pinned `FROM … AS builder` (Req 2.2).
- **D3**: `src/backend/Dockerfile.jp6` `FROM … l4t-cuda:11.4.19-runtime@sha256:fb22ff08…8da78 AS cuda114`
  parameterized + pinned, `AS cuda114` preserved (Req 2.3).
- **D4**: `src/backend/Dockerfile.jp6` `FROM … l4t-jetpack:r36.3.0@sha256:b3bbd7e3…e2fa`
  parameterized + pinned, sharing the single `BASE_REGISTRY` ARG (Req 2.4).
- **D5**: `src/edgemlsdk/Dockerfile.jp6` `ARG BASE_REGISTRY=nvcr.io` + parameterized
  + pinned `FROM … AS builder` (Req 2.5).
- **D6**: `docker_base_image_audit.disallowed_hits() == []` (Req 2.6).

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed code
produces the same result as the original — `F(X) = F'(X)`.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: Capture the baseline on the **unfixed** tree first, then
assert the fixed tree matches. The core preservation argument is that with
`BASE_REGISTRY` unset/`nvcr.io` the resolved reference is equivalent in effect to
the current tag (the pinned digest IS the manifest-list digest the tag resolves to),
and every non-`FROM` line is byte-for-byte identical. Property-based testing
(Hypothesis) is used where the domain is generatable. Preservation tests live under
`test/backend-test/security/preservation/` as `test_preservation_docker_*`, picked
up by the shared security/preservation suite.

**Property-based test plans:**
- **PBT 1 — `BASE_REGISTRY` resolution (D1–D5)**: model the `FROM` reference
  resolution as `resolve(base_registry, image, tag, digest) -> f"{registry}/nvidia/{image}:{tag}@sha256:{digest}"`
  where `registry = base_registry or "nvcr.io"`. Generate `base_registry ∈ {unset,
  "nvcr.io", "123456789012.dkr.ecr.us-west-2.amazonaws.com", "my-mirror.internal",
  random host-like strings}` with the tag and digest **fixed** per image.
  Invariants: (a) when unset/`nvcr.io`, the resolved reference equals the current
  `nvcr.io/nvidia/<image>:<tag>@sha256:<digest>` (byte-identical to `F`'s effective
  pull); (b) for any override, the resolved reference equals
  `<registry>/nvidia/<image>:<tag>@sha256:<digest>` with the tag and digest unchanged
  (only the registry prefix differs — the intended new behavior). The DIFFERENCE for
  the unset case is empty (perfect preservation).
- **PBT 2 — arbitrary `FROM` line → `disallowed_hits()` classification (D6)**:
  generate arbitrary `FROM` lines from a grammar over {ECR host | `nvcr.io` literal |
  `${BASE_REGISTRY}` seam} × {digest present | absent} × {`AS <stage>` present |
  absent} × {`# nosec` present | absent}. Invariant: `disallowed_hits()` flags a
  `FROM` iff it references a non-ECR literal registry AND (is not `${BASE_REGISTRY}`-
  parameterized OR lacks `@sha256:`), AND is not `# nosec`-annotated. In particular
  a `${BASE_REGISTRY}`+`@sha256` line whose ARG default is `nvcr.io` is NOT flagged;
  an ECR host is NOT flagged; a literal `nvcr.io` without digest IS flagged.

**Example-based preservation cases:**
1. **Non-`FROM` bytes unchanged (golden)**: for each of the four Dockerfiles, take a
   golden of the file with the `FROM` and `ARG BASE_REGISTRY` lines masked/removed
   and assert every OTHER line is byte-for-byte identical between the unfixed
   baseline and the fixed file. This is the primary preservation guarantee.
2. **Multi-stage structure golden**: assert the fixed files retain the `AS builder`
   names (D2, D5), the `AS cuda114` name (D3), and the
   `COPY --from=cuda114 /usr/local/cuda-11.4/targets/aarch64-linux/lib/ …` line
   unchanged (jp6 backend), so cross-stage copies still resolve.
3. **Default-registry resolution**: assert that with `BASE_REGISTRY` unset the
   effective pull reference for each `FROM` equals
   `nvcr.io/nvidia/<image>:<tag>@sha256:<digest>` with the recorded manifest-list
   digest (equivalent to the current tag pull).
4. **ECR Dockerfiles + vendored duplicate untouched (Req 3.7)**: assert
   `src/backend/Dockerfile`, `src/frontend/Dockerfile`, `src/edgemlsdk/Dockerfile`,
   and `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5|jp6` are byte-for-byte
   unchanged.
5. **No compose change (Req 3.6)**: assert `docker-compose.yaml` is unchanged.

### Unit Tests

- **D1–D5**: parse each in-scope Dockerfile; assert exactly one
  `ARG BASE_REGISTRY=nvcr.io` before the first `FROM`; assert each in-scope `FROM`
  matches `${BASE_REGISTRY}/nvidia/<image>:<tag>@sha256:<64-hex>` with the correct
  tag and digest; assert `AS builder` (D2, D5) / `AS cuda114` (D3) preserved; for
  jp6 backend assert a single shared `ARG BASE_REGISTRY` (no re-declaration) covers
  both `FROM`s.
- **Digest values**: assert the pinned digests equal the recorded manifest-list
  digests (and, at implementation time, a manually-run re-verification against
  `nvcr.io`; the automated test asserts the committed value).
- **D6 gate**: `docker_base_image_audit.disallowed_hits() == []`; `run_audit()`
  enumerates the five in-scope `FROM`s and excludes the vendored duplicate and
  non-Dockerfiles; `_has_nosem` clears an annotated line; the `${BASE_REGISTRY}` +
  `@sha256` case is classified compliant.

### Property-Based Tests

- PBT 1 (`BASE_REGISTRY` resolution) — invariant: unset/`nvcr.io` resolves to the
  current byte-identical reference; any override changes only the registry prefix,
  tag + digest fixed.
- PBT 2 (arbitrary `FROM` → classification) — invariant: `disallowed_hits()` flags
  exactly the non-ECR-literal AND (unparameterized OR unpinned) `FROM`s, clearing
  ECR hosts, `${BASE_REGISTRY}`+`@sha256` lines, and `# nosec` lines.

### Integration Tests

- **Default build (gated)**: build each in-scope image with no `--build-arg` and
  assert the build resolves the pinned digest and produces an equivalent image to
  the pre-fix baseline (same manifest-list digest pulled). For jp6 backend, assert
  the `COPY --from=cuda114` cudart staging and the `libcudart.so.11` verification
  step still succeed.
- **Internal-mirror override**: build with
  `--build-arg BASE_REGISTRY=<mirror hosting the same digest>` and assert an
  equivalent image (identical bytes by digest) is produced from the mirror.
- **Audit gate in CI**: run `docker_base_image_audit.py` in `build-custom.sh` — it
  fails the build if any in-scope `FROM` reverts to a non-ECR, unparameterized, or
  unpinned reference.

**Rollback plan** (per the bugfix.md commitments): each in-scope Dockerfile is a
separate task (jp5 backend / jp5 edgemlsdk / jp6 backend covering D3+D4 / jp6
edgemlsdk). If a build breaks after a fix (stale/wrong digest, or an internal mirror
lacking the image), the specific Dockerfile change is reverted independently —
isolated to one file's diff — without touching the other Dockerfiles, the audit
gate, or the sibling remediation branches.
