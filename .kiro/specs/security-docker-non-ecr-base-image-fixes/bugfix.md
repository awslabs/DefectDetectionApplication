# Bugfix Requirements Document

## Introduction

A code security review of the DefectDetectionApplication (DDA) — the AWS "DDA
Code Review" scan whose findings carry rubric rule id
`bosco/non-ecr-docker-image` — surfaced 6 HIGH-severity **Non-ECR Docker Base
Image** findings that are the subject of this spec. Every finding is a
container base image pulled from an **external, non-ECR registry**
(`nvcr.io`, NVIDIA's NGC registry) using a **mutable tag** with **no immutable
`@sha256` digest pin** and **no registry parameterization**. This is a
supply-chain / image-squatting-and-tamper exposure:

1. **Mutable tags can be repointed.** A tag such as `r35.4.1` or
   `11.4.19-runtime` is not content-addressed; the upstream registry (or an
   attacker who compromises it, or a man-in-the-middle on an unauthenticated
   pull) can repoint the tag to a different, tampered image without the
   Dockerfile changing. Every build silently pulls whatever the tag currently
   resolves to.
2. **The external registry is hardcoded with no seam.** The Dockerfiles embed
   `nvcr.io` directly in each `FROM`, so there is no way for an internal /
   air-gapped / ECR-mirrored build to redirect the pull to a trusted mirror
   **without editing the Dockerfile**. A build that cannot reach `nvcr.io`, or
   that must pull only from an approved internal registry, has no override
   point.

This spec ("Docker Non-ECR Base Image Pinning & Parameterization" — the fifth
remediation group in the AWS code-review sequence, stacked after
`security-s3-bucket-squatting-fixes`) is scoped **strictly** to the 6 findings
enumerated below (labelled D1–D6) — 5 in-scope `FROM` lines (D1–D5) plus a
repository audit gate (D6). It uses the SAME bug-condition methodology, EARS
format, and Property 1 (Fix Checking) / Property 2 (Preservation) framing as
the sibling specs `security-injection-deserialization-fixes` (findings #1–#8),
`security-secrets-credentials-jwt-fixes` (findings S1–S9),
`security-iam-authorization-fixes` (findings I1–I17), and
`security-s3-bucket-squatting-fixes` (findings B1–B7).

### The user's remediation choice: Option A (registry ARG + immutable digest pinning)

The in-scope base images are **NVIDIA-proprietary Jetson (L4T / JetPack)
images** that have **no public-ECR equivalent**. This drives the mitigation.
Three options were considered:

- **Option A (CHOSEN)** — parameterize the registry via a build `ARG` (default
  preserves `nvcr.io`) **and** pin each base image to its immutable `@sha256`
  digest while keeping the human-readable tag. The `ARG` provides the seam to
  redirect to an internal / ECR mirror without editing the Dockerfile; the
  digest pin provides tamper-resistance so a repointed mutable tag cannot
  silently change the pulled bytes.
- **Option B (rejected)** — digest pin only, no registry parameterization.
  Rejected because it leaves the external registry hardcoded with no override
  seam for internal / air-gapped builds.
- **Option C (rejected)** — move the images to AWS ECR. Rejected because these
  are NVIDIA-proprietary Jetson images with no public-ECR equivalent; DDA
  cannot lawfully re-host them, and there is no AWS-managed mirror. This is the
  Jetson-image analogue of the sibling S3 spec's "AWS-managed buckets that DDA
  does not own are NOT renamed" decision: the registry `ARG` provides the seam
  and the digest pin provides tamper-resistance, rather than relocating an
  asset DDA does not own.

For each in-scope `FROM` referencing `nvcr.io`, Option A applies:

1. **Registry `ARG`** — introduce a single shared build arg
   `ARG BASE_REGISTRY=nvcr.io`, declared **before the first `FROM`** in each
   Dockerfile, and **re-declared after a `FROM`** only where a later stage's
   `FROM` needs it (Docker scopes an `ARG` declared before the first `FROM` to
   the `FROM` lines only; a stage body that referenced it would need a
   re-declaration, but here the arg is used only in the `FROM` instructions).
   An internal build repoints the registry with
   `--build-arg BASE_REGISTRY=<internal-ecr-mirror>` — no Dockerfile edit.
2. **Immutable digest pin** — pin each base image to its `@sha256` digest while
   KEEPING the human-readable tag, i.e.
   `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:<digest>`. The
   digests below were resolved from `nvcr.io` and are exactly what the current
   mutable tags point to, so pinning is **behavior-preserving** (the same image
   bytes are pulled):

   | Image | Tag | Immutable digest |
   |-------|-----|------------------|
   | `l4t-jetpack` | `r35.4.1` | `sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e` |
   | `l4t-jetpack` | `r36.3.0` | `sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa` |
   | `l4t-cuda` | `11.4.19-runtime` | `sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78` |

### Glossary

- **Non-ECR base image** — a container base image pulled from a registry other
  than AWS ECR (public or private). Here the external registry is `nvcr.io`
  (NVIDIA NGC).
- **Mutable tag** — a human-readable image reference (e.g. `r35.4.1`) that is
  NOT content-addressed and can be repointed by the registry to different
  bytes at any time.
- **Immutable digest (`@sha256:...`)** — a content-addressed image reference:
  the same digest always resolves to exactly the same image bytes, or the pull
  fails. Pinning to a digest makes a repointed tag detectable / harmless.
- **Image squatting / tamper** — an attacker (or a compromised / MITM'd
  registry) serves a tampered image for a mutable tag, which an unpinned build
  pulls and executes.
- **Registry parameterization / seam** — a build `ARG` (`BASE_REGISTRY`) that
  lets a build redirect the registry portion of a `FROM` to a trusted internal
  / ECR mirror without editing the Dockerfile.
- **In-scope Dockerfile** — one of the four maintained Jetson Dockerfiles:
  `src/backend/Dockerfile.jp5`, `src/edgemlsdk/Dockerfile.jp5`,
  `src/backend/Dockerfile.jp6`, `src/edgemlsdk/Dockerfile.jp6`.
- **F** — the original (unfixed) Dockerfile, where the `FROM` references
  `nvcr.io` directly with a mutable tag, no digest pin, and no registry `ARG`.
- **F'** — the fixed Dockerfile, where every in-scope `FROM` is
  registry-parameterized via `${BASE_REGISTRY}` (default `nvcr.io`) and pinned
  to its immutable `@sha256` digest with the tag retained.

### The findings and their real source locations

All 5 image findings are in the four maintained Jetson Dockerfiles. The finding
`file_path`s are basenames; the same `FROM` lines exist byte-for-byte in the
vendored duplicate (out of scope, see below). Line numbers match both copies.

| # | File | Line | `FROM` (current) | Fix |
|---|------|------|------------------|-----|
| D1 | `src/backend/Dockerfile.jp5` | 4 | `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1` | `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971...b2a0e` with `ARG BASE_REGISTRY=nvcr.io` declared before the first `FROM` |
| D2 | `src/edgemlsdk/Dockerfile.jp5` | 1 | `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1 AS builder` | `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971...b2a0e AS builder` with `ARG BASE_REGISTRY=nvcr.io` declared before the first `FROM`; the `AS builder` stage name preserved |
| D3 | `src/backend/Dockerfile.jp6` | 18 | `FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114` | `FROM ${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff08...8da78 AS cuda114`; the `AS cuda114` stage name preserved so the later `COPY --from=cuda114` still resolves |
| D4 | `src/backend/Dockerfile.jp6` | 19 | `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0` | `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3...e2fa` (final stage), sharing the `BASE_REGISTRY` arg declared before the first `FROM` (line 18) |
| D5 | `src/edgemlsdk/Dockerfile.jp6` | 1 | `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0 AS builder` | `FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3...e2fa AS builder` with `ARG BASE_REGISTRY=nvcr.io` declared before the first `FROM`; the `AS builder` stage name preserved |

**Repository audit gate**

| # | File | Loc | Finding | Fix |
|---|------|-----|---------|-----|
| D6 | (in-scope Dockerfiles only) | — | No runnable check exists that asserts the fixes above are in place; a regression (a new / edited `FROM`, or a `git checkout` reverting a fix) could re-introduce a non-ECR, unparameterized, or unpinned base image in an in-scope Dockerfile with no automated detection. | Add a runnable two-layer audit `test/backend-test/security/docker_base_image_audit.py`: (1) a raw enumeration layer that lists every `FROM` line in the four in-scope Dockerfiles, and (2) a precise `disallowed_hits()` layer that flags any in-scope `FROM` referencing a non-ECR registry that is NOT both registry-parameterized (via a `${BASE_REGISTRY...}` ARG) AND digest-pinned (`@sha256:`). Scoped to the four in-scope Dockerfiles; excludes the vendored `src/backend/edgemlsdk/edgemlsdk/...` duplicate. Wired into `build-custom.sh` as the fifth security gate (after the S3 gate). |

### Explicitly out of scope (handled elsewhere, or fundamentally out of scope)

- **The vendored / generated duplicate Dockerfiles** —
  `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5` and
  `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp6` are a vendored/generated copy
  of the edgemlsdk Dockerfiles, regenerated from `src/edgemlsdk/...` by
  `build-custom.sh` (the `build-custom.sh` copy step). They contain byte-for-byte
  identical `nvcr.io` `FROM` lines, but they are NOT maintained by hand and are
  overwritten on every build. Only the maintained source paths listed as D1–D5
  are fixed; the vendored copy is NOT touched. This mirrors the sibling batches'
  exclusion of the vendored `src/backend/edgemlsdk/edgemlsdk/...` duplicate and
  `cdk.out`.
- **The already-compliant `public.ecr.aws/*` base images** —
  `src/backend/Dockerfile` (`public.ecr.aws/ubuntu/ubuntu:${OS}`),
  `src/frontend/Dockerfile` (`public.ecr.aws/docker/library/node:18-alpine`,
  `public.ecr.aws/nginx/nginx:stable-alpine`), and `src/edgemlsdk/Dockerfile`
  (`public.ecr.aws/ubuntu/ubuntu:${OS}`) already pull from AWS ECR Public and
  are NOT in scope for this batch. (Digest-pinning those ECR images is a
  possible future hardening but is out of scope here — the finding class is
  specifically *non-ECR* base images.)
- **Relocating the Jetson images to ECR (Option C)** — deliberately NOT done;
  see the remediation-choice section. These are NVIDIA-proprietary images with
  no public-ECR equivalent that DDA does not own and cannot re-host.
- **`docker-compose.yaml` and build invocation changes** — the `BASE_REGISTRY`
  ARG has a default of `nvcr.io`, so no compose / build-arg change is REQUIRED
  for the default build. Wiring an internal-mirror override into the build
  tooling is optional operator configuration, not part of this fix.
- **The already-remediated batches** — injection / unsafe-deserialization
  (`security-injection-deserialization-fixes`, #1–#8); secrets, credentials &
  JWT (`security-secrets-credentials-jwt-fixes`, S1–S9); IAM & Authorization
  (`security-iam-authorization-fixes`, I1–I17); and S3 bucket squatting
  (`security-s3-bucket-squatting-fixes`, B1–B7). All of their fixes MUST remain
  byte-for-byte unchanged.
- **Any other finding class from the review** not carrying rule id
  `bosco/non-ecr-docker-image` on the five `FROM` lines above.

### Testability + Rollback commitments

- **Digest resolution (implementation-time check).** The three digests in the
  table above were resolved from `nvcr.io` and are asserted to be exactly what
  the current mutable tags point to. At implementation time the digests MUST be
  re-verified against `nvcr.io` (e.g. via `docker buildx imagetools inspect` /
  `crane digest` / `skopeo inspect`) so that pinning is genuinely
  behavior-preserving. For the multi-arch `l4t-*` manifests, the pin MUST target
  the same manifest (manifest-list digest) the current tag resolves to so the
  per-arch selection is unchanged. If a resolved digest differs from the value
  recorded here, the design phase records the corrected digest before the fix
  lands.
- **Fix verification** is a static parse of each in-scope Dockerfile: assert
  every in-scope `FROM` (a) references the registry via `${BASE_REGISTRY...}`
  rather than a literal `nvcr.io`, (b) carries an `@sha256:` digest, and (c)
  retains its original tag and, where present, its `AS <stage>` name. A
  `BASE_REGISTRY` ARG default of `nvcr.io` is asserted so the default resolved
  reference is unchanged.
- **Preservation verification** captures that every non-`FROM` line in all four
  Dockerfiles is byte-for-byte unchanged, and that the resolved default image
  reference (`BASE_REGISTRY` unset → `nvcr.io`) plus the pinned digest equals the
  image the current tag resolves to. Preservation tests live under
  `test/backend-test/security/preservation/` (`test_preservation_docker_*`).
- **Property-based testing** is emphasized in the design phase where the input
  domain is generatable: generate `BASE_REGISTRY` values (unset/default vs. an
  internal-mirror override) and assert the resolved reference equals
  `<registry>/nvidia/<image>:<tag>@sha256:<digest>` with the tag and digest
  fixed; generate arbitrary in-scope-Dockerfile `FROM` lines and assert
  `disallowed_hits()` flags exactly those that are non-ECR AND
  (unparameterized OR unpinned).
- **Rollback plan:** each in-scope Dockerfile is a **separate task** in this
  spec's breakdown (jp5 backend, jp5 edgemlsdk, jp6 backend covering D3+D4, jp6
  edgemlsdk). If a build breaks after a fix (e.g. a stale / wrong digest, or an
  internal mirror lacking the image), the specific Dockerfile change can be
  reverted independently — isolated to one file's diff — without touching the
  other Dockerfiles, the audit gate, or the sibling remediation branches.

### Bug Condition and Properties

The bug-condition methodology frames this fix as follows.

**Bug Condition `C(X)`** — identifies the inputs/code paths that trigger the
defect. Here the "input" is any base-image `FROM` line in an in-scope
Dockerfile:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type FromInstruction   // a FROM line in an in-scope Dockerfile
                                     // (src/backend/Dockerfile.jp5|jp6,
                                     //  src/edgemlsdk/Dockerfile.jp5|jp6)
  OUTPUT: boolean

  // True when a base image is pulled from an external, non-ECR registry
  // AND is not registry-parameterized via a ${BASE_REGISTRY...} ARG
  // AND/OR is not pinned to an immutable @sha256 digest:
  RETURN nonEcrRegistry(X)          // references nvcr.io (not public.ecr.aws / an ECR host)
      AND ( NOT registryParameterized(X)   // registry is a literal, not ${BASE_REGISTRY...}
            OR NOT digestPinned(X) )        // no @sha256:<digest> pin
END FUNCTION
```

For the current tree, `isBugCondition` is TRUE for D1–D5 (each references
`nvcr.io` literally, with a mutable tag and no digest).

**Fix Property `P` (Property 1 — Fix Checking)** — desired behavior for all
buggy inputs after the fix `F'`:

```pascal
// Property 1: Fix Checking - every in-scope non-ECR base image is
// registry-parameterized AND digest-pinned
FOR ALL X WHERE isBugCondition(X) DO
  result <- F'(X)
  ASSERT registryParameterized(result)
     // the registry portion is ${BASE_REGISTRY} (or ${BASE_REGISTRY:-nvcr.io}),
     // with ARG BASE_REGISTRY=nvcr.io declared before the first FROM, so an
     // internal build can redirect to an ECR mirror without editing the file.
  ASSERT digestPinned(result)
     // the reference carries an immutable @sha256:<digest> pin so a repointed
     // mutable tag cannot silently change the pulled bytes.
  ASSERT tagRetained(result) AND stageNamePreserved(result)
     // the human-readable tag (r35.4.1 / r36.3.0 / 11.4.19-runtime) is kept
     // alongside the digest, and any AS <stage> name (builder / cuda114) is
     // unchanged so multi-stage COPY --from references still resolve.
END FOR
```

**Preservation Property (Property 2 — Preservation Checking)** — for every
input that does NOT trigger the bug condition (every already-compliant `FROM`,
every non-`FROM` line, and every default build), the fixed code behaves
identically to the original code `F`:

```pascal
// Property 2: Preservation Checking - no behavior change for the default build
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
     // With BASE_REGISTRY unset/defaulted to nvcr.io, the resolved image
     // reference is equivalent IN EFFECT to the current tag: the pinned digest
     // is exactly the digest the current tag resolves to, so the SAME image
     // bytes are pulled.
     // Every OTHER line in all four Dockerfiles is byte-for-byte unchanged:
     // the ARG OS/PLATFORM/PYTHON_VERSION declarations, the apt phases, the
     // Python-from-source / deadsnakes install, the awscrt / aws-lc linker
     // workaround, the ONNX runtime build args, aravis, the Triton build, the
     // CUDA 11.4 cudart staging COPY --from=cuda114, the artifact-collection
     // RUN blocks, the COPY app steps, CMD, and USER root.
     // The multi-stage AS builder / AS cuda114 stage names and the
     // COPY --from=cuda114 reference still work.
     // No docker-compose.yaml change is required (the ARG defaults).
     // The already-compliant public.ecr.aws/* Dockerfiles
     // (src/backend/Dockerfile, src/frontend/Dockerfile,
     //  src/edgemlsdk/Dockerfile) and the vendored
     // src/backend/edgemlsdk/edgemlsdk/... duplicate remain unchanged.
END FOR
```

- **F**: the original (unfixed) Dockerfile, where the `FROM` references
  `nvcr.io` directly with a mutable tag, no `@sha256` digest, and no registry
  `ARG`.
- **F'**: the fixed Dockerfile, where every in-scope `FROM` is
  registry-parameterized via `${BASE_REGISTRY}` (default `nvcr.io`) and pinned
  to its immutable `@sha256` digest with the tag and any `AS <stage>` name
  retained.

## Bug Analysis

### Current Behavior (Defect)

The four maintained Jetson Dockerfiles pull their base images from the external,
non-ECR registry `nvcr.io` using mutable tags, with no immutable `@sha256`
digest pin and no registry parameterization — so a repointed / tampered upstream
tag would be pulled silently, and an internal / air-gapped / ECR-mirrored build
has no seam to redirect the registry without editing the Dockerfile.

1.1 WHEN `src/backend/Dockerfile.jp5` (line 4) is built THEN the system executes
`FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1`, pulling the base image from the
external non-ECR registry `nvcr.io` via a mutable tag with **no** `@sha256`
digest pin and **no** `${BASE_REGISTRY}` parameterization (D1).

1.2 WHEN `src/edgemlsdk/Dockerfile.jp5` (line 1) is built THEN the system
executes `FROM nvcr.io/nvidia/l4t-jetpack:r35.4.1 AS builder`, pulling the
builder-stage base image from the external non-ECR registry `nvcr.io` via a
mutable tag with **no** `@sha256` digest pin and **no** `${BASE_REGISTRY}`
parameterization (D2).

1.3 WHEN `src/backend/Dockerfile.jp6` (line 18) is built THEN the system
executes `FROM nvcr.io/nvidia/l4t-cuda:11.4.19-runtime AS cuda114`, pulling the
CUDA 11.4 cudart provider stage from the external non-ECR registry `nvcr.io` via
a mutable tag with **no** `@sha256` digest pin and **no** `${BASE_REGISTRY}`
parameterization (D3).

1.4 WHEN `src/backend/Dockerfile.jp6` (line 19) is built THEN the system
executes `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0` (the final runtime stage),
pulling the base image from the external non-ECR registry `nvcr.io` via a
mutable tag with **no** `@sha256` digest pin and **no** `${BASE_REGISTRY}`
parameterization (D4).

1.5 WHEN `src/edgemlsdk/Dockerfile.jp6` (line 1) is built THEN the system
executes `FROM nvcr.io/nvidia/l4t-jetpack:r36.3.0 AS builder`, pulling the
builder-stage base image from the external non-ECR registry `nvcr.io` via a
mutable tag with **no** `@sha256` digest pin and **no** `${BASE_REGISTRY}`
parameterization (D5).

1.6 WHEN the repository is audited for the bug-condition pattern in the in-scope
Dockerfiles (D1–D5) — a `FROM` referencing a non-ECR registry that is NOT both
registry-parameterized via a `${BASE_REGISTRY...}` ARG AND digest-pinned with
`@sha256:` — THEN the unfixed tree contains the five disallowed occurrences
above with no runnable check to detect a regression, and no documented,
justified exception (D6).

### Expected Behavior (Correct)

After the fix, every in-scope base image is registry-parameterized via a
`BASE_REGISTRY` build ARG (defaulting to `nvcr.io`) AND pinned to its immutable
`@sha256` digest with the human-readable tag and any `AS <stage>` name retained,
and a runnable audit gate detects any regression.

2.1 WHEN `src/backend/Dockerfile.jp5` is built THEN the system SHALL declare
`ARG BASE_REGISTRY=nvcr.io` before the first `FROM` and SHALL execute
`FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e`,
so the registry is redirectable via `--build-arg BASE_REGISTRY=<mirror>` and the
image is pinned to its immutable digest with the `r35.4.1` tag retained (D1).

2.2 WHEN `src/edgemlsdk/Dockerfile.jp5` is built THEN the system SHALL declare
`ARG BASE_REGISTRY=nvcr.io` before the first `FROM` and SHALL execute
`FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r35.4.1@sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e AS builder`,
preserving the `AS builder` stage name (D2).

2.3 WHEN `src/backend/Dockerfile.jp6` is built THEN the system SHALL declare
`ARG BASE_REGISTRY=nvcr.io` before the first `FROM` (line 18) and SHALL execute
`FROM ${BASE_REGISTRY}/nvidia/l4t-cuda:11.4.19-runtime@sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78 AS cuda114`,
preserving the `AS cuda114` stage name so the later `COPY --from=cuda114` still
resolves (D3).

2.4 WHEN `src/backend/Dockerfile.jp6` is built THEN the final runtime stage
SHALL execute
`FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa`,
sharing the `BASE_REGISTRY` ARG declared before the first `FROM`, pinned to its
immutable digest with the `r36.3.0` tag retained (D4).

2.5 WHEN `src/edgemlsdk/Dockerfile.jp6` is built THEN the system SHALL declare
`ARG BASE_REGISTRY=nvcr.io` before the first `FROM` and SHALL execute
`FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:r36.3.0@sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa AS builder`,
preserving the `AS builder` stage name (D5).

2.6 WHEN the repository is audited for the bug-condition pattern in the in-scope
Dockerfiles (D1–D5) THEN the system SHALL provide a runnable two-layer audit
`test/backend-test/security/docker_base_image_audit.py` — (1) a raw enumeration
of every `FROM` line in the four in-scope Dockerfiles, and (2) a precise
`disallowed_hits()` that flags any in-scope `FROM` referencing a non-ECR
registry that is NOT both registry-parameterized (`${BASE_REGISTRY...}`) AND
digest-pinned (`@sha256:`) — that SHALL assert zero disallowed hits, SHALL be
scoped to the four in-scope Dockerfiles, SHALL exclude the vendored
`src/backend/edgemlsdk/edgemlsdk/...` duplicate, and SHALL be wired into
`build-custom.sh` as the fifth security gate (after the S3 gate). Preservation
tests under `test/backend-test/security/preservation/`
(`test_preservation_docker_*`) SHALL capture that all non-`FROM` content is
byte-for-byte unchanged and that the resolved default image reference equals the
current digest (D6).

### Unchanged Behavior (Regression Prevention)

The default build (with `BASE_REGISTRY` unset / defaulted to `nvcr.io`) must
produce exactly the same images as before, because the pinned digest is exactly
the digest each current tag resolves to. Every non-`FROM` line in all four
Dockerfiles, the multi-stage structure, the already-compliant
`public.ecr.aws/*` Dockerfiles, and the vendored duplicate must remain
byte-for-byte identical.

3.1 WHEN any in-scope Dockerfile is built with `BASE_REGISTRY` unset (or set to
`nvcr.io`) THEN the system SHALL CONTINUE TO pull the same base image bytes as
before — the pinned `@sha256` digest is exactly the digest the current mutable
tag resolves to, so the default resolved reference
(`nvcr.io/nvidia/<image>:<tag>@sha256:<digest>`) is equivalent in effect to the
current `nvcr.io/nvidia/<image>:<tag>`.

3.2 WHEN `src/backend/Dockerfile.jp5` and `src/backend/Dockerfile.jp6` are built
THEN the system SHALL CONTINUE TO execute every non-`FROM` instruction
unchanged — the `ARG OS`/`PLATFORM`/`PYTHON_VERSION` declarations, the
py3compile disable/restore, the apt purge/install phases, the toolchain PPA and
gcc-11/g++-11 setup, the Python-from-source (jp5) / deadsnakes 3.11 (jp6)
install, the awscrt `0.14.7` / aws-lc linker workaround, the ONNX runtime build
args, the aravis build, the grpc_tools protoc step, the `COPY app.py ...` steps,
`CMD ["python3.11", "app.py"]`, the edgemlsdk install, and `USER root` — all
byte-for-byte identical.

3.3 WHEN `src/backend/Dockerfile.jp6` is built THEN the system SHALL CONTINUE TO
stage the CUDA 11.4 cudart libs via `COPY --from=cuda114
/usr/local/cuda-11.4/targets/aarch64-linux/lib/ ...` — the `AS cuda114` stage
name on the digest-pinned, parameterized `FROM` (D3) is preserved so the
`COPY --from=cuda114` reference resolves exactly as before, and the subsequent
`ldconfig` / `libcudart.so.11` verification RUN block is unchanged.

3.4 WHEN `src/edgemlsdk/Dockerfile.jp5` and `src/edgemlsdk/Dockerfile.jp6` are
built THEN the system SHALL CONTINUE TO run the full builder stage unchanged —
the build-env setup, system packages, CMake install, Python 3.11 setup, Python
packages, AWS CLI / PowerShell / OpenSSL / aws-crt-cpp / aws-sdk-cpp / aws-c-iot
/ aravis / aws-iot-device-sdk-cpp-v2 / Boost / Triton / c-periphery builds, the
EdgeML SDK build, and the artifact-collection steps — with only the `FROM`
`AS builder` line changed to the parameterized, digest-pinned form.

3.5 WHEN a build sets `--build-arg BASE_REGISTRY=<internal-ecr-mirror>` that
hosts the same digest-pinned images THEN the system SHALL CONTINUE TO produce an
equivalent build, pulling the identical image bytes (identified by the same
`@sha256` digest) from the mirror instead of `nvcr.io`, with no other change.

3.6 WHEN the build runs THEN the system SHALL CONTINUE TO require no
`docker-compose.yaml` or build-invocation change for the default build, because
`ARG BASE_REGISTRY=nvcr.io` supplies the default registry.

3.7 WHEN the review's out-of-scope items are considered — the vendored /
generated duplicate `src/backend/edgemlsdk/edgemlsdk/Dockerfile.jp5|jp6`
(regenerated from `src/edgemlsdk/...` by `build-custom.sh`); the already-compliant
`public.ecr.aws/*` Dockerfiles (`src/backend/Dockerfile`,
`src/frontend/Dockerfile`, `src/edgemlsdk/Dockerfile`); the deliberate decision
NOT to relocate the NVIDIA-proprietary Jetson images to ECR (Option C rejected);
and the already-remediated batches
(`security-injection-deserialization-fixes` #1–#8,
`security-secrets-credentials-jwt-fixes` S1–S9,
`security-iam-authorization-fixes` I1–I17, and
`security-s3-bucket-squatting-fixes` B1–B7) — THEN this spec SHALL CONTINUE TO
leave them unchanged.
