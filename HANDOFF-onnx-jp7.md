# Handoff: ONNX-on-JP7 workstream

**Branch:** `spec/jetpack7-support` (pushed to origin)
**Repo root:** `DefectDetectionApplication/`
**Date:** 2026-08-14
**Deployed portal:** https://d23v4ltibogb5x.cloudfront.net (account 164152369890, us-east-1)

## Goal

User wants to test ONNX vision models on JetPack 7 (Jetson Thor, `arm64_jp7`).
ONNX is the ONLY vision runtime on JP7: no DLR (the JP6 CUDA-11.4/TRT8 staging
stages were deliberately not carried to Thor), and SageMaker Neo cannot target
CUDA 13 (ceiling 11.x). GPU onnxruntime is enabled by default in
`src/backend/Dockerfile.jp7` (ORT 1.23.x, CUDA 13, sm_110), and the device-side
`OnnxRunner` exists (`src/backend/dda_triton/resources_for_copy/inference_runtimes.py`,
selected by manifest `runtime: onnx`).

## Original incident

Compiling model `f182a10d-0da7-420a-943c-c370da7ee623` to the `onnx` target
shows only the literal string `ERROR` in the portal UI. Root cause chain
(verified in `edge-cv-portal/backend/functions/compilation.py`):

1. `_start_onnx_export_job` raised at start; the except branch appends a
   placeholder entry with a fabricated job name `{safe}-onnx-failed` and NO
   `export_format` key.
2. `get_compilation_status` therefore polls it with `describe_compilation_job`
   (wrong API, nonexistent name) → `ClientError`.
3. The `except ClientError` handler OVERWRITES `job['status']='ERROR'` and
   `job['error']=str(e)`, then persists to DynamoDB — the originating error is
   destroyed on the FIRST poll. It cannot be recovered from the record.
4. `ModelDetail.tsx`'s fallback table renders the bare token (its inline type
   has no reason field); `CompilationTab.tsx`'s error panel filter
   `job.status === 'Failed'` never matches uppercase `FAILED` either, so Neo
   failure reasons have always been hidden too.
5. `models.py::get_model` is a second poller with no `export_format` branch —
   even a successful ONNX export can never advance from the model detail page.

**The record's original error is GONE.** To learn the real trigger, either
query CloudWatch on the compilation Lambda:
`filter @message like /Failed to start ONNX export job/`
or re-run a compile after the diagnostics fix deploys. Likely triggers (named,
deliberately not fixed): hardcoded
`arn:aws:iam::{account_id}:role/DDASageMakerExecutionRole` and the
region-pinned `ONNX_EXPORT_IMAGE` default
(`763104351884.dkr.ecr.us-east-1...pytorch-training:1.13.1-cpu-py39`).

## Specs created this session (both spec-only, no app code touched)

### 1. `.kiro/specs/onnx-compile-error-diagnostics/` — COMPLETE spec, ready to execute
- bugfix.md + design.md + tasks.md (9 waves).
- Five defects: wrong-API placeholder, destructive ClientError handler,
  `'ERROR'` outside every status vocabulary, both UI surfaces hiding reasons,
  models.py second-poller dispatch.
- Core invariant: `error`/`failure_reason` are write-once w.r.t. polling; poll
  faults go to separate `poll_error*` fields; `'ERROR'` redefined as an
  explicitly modeled transient status bounded by `POLL_ERROR_MAX_ATTEMPTS`.
- New shared module planned: `edge-cv-portal/backend/layers/shared/python/compilation_status.py`
  (`classify_poll_kind`, `derive_compilation_status`, status sets). Both
  handlers already mount that layer — no infra change, no IAM change.
- Wave 1 = exploration suite that MUST FAIL on unfixed code (case 9, the
  no-JP7-Neo-target guard, must PASS and stay passing).
- Ends with USER ACTION: portal deploy + fresh onnx compile to read the real error.

### 2. `.kiro/specs/onnx-jetson-publish-packaging/` — bugfix.md ONLY (requirements phase done)
- design.md and tasks.md NOT yet created. Next step: generate design, then tasks.
- Goal: deliver ONNX components to Jetson devices; JP7 is the deliverable.
- **User decision (do not re-litigate): per-JetPack ONNX components** — one
  component per JetPack variant (e.g. `model-{safe}-onnx-jetson-xavier-jp7`),
  platform `aarch64`, HARD dep on that JetPack's LocalServer. Single
  arch-agnostic component was rejected (Greengrass ComponentDependencies is
  top-level → unsatisfiable dep on other JetPacks).
- Four defects encoded:
  1. `onnx` unmapped in `greengrass_publish.py` `TARGET_TO_LOCAL_SERVER` /
     `TARGET_TO_PLATFORM` → on current tree `resolve_target_platform` raises
     PublishError; on a pre-sibling tree it silently stamps amd64.
  2. `packaging.py` generic Phase 2 loop packages compiled-onnx as ONE
     `target:'onnx'` entry with a manifest LACKING `runtime` (device defaults
     to dlr → wrong runner; JP7 has no DLR). BYO-import path
     (`package_onnx_component`) is already correct but its default target list
     omits `jetson-xavier-jp7`.
  3. `workflow_packaging.py` `ARCH_TO_PUBLISH_TARGET[arm64_jp7]='jetson-xavier-jp7'`
     deliberately fails closed for vision refs — must resolve to the JP7 ONNX
     component (keep fail-closed when none published).
  4. Frontend `inferComponentTargetArchs` regex `(?:jp|jetpack)(4|5|6|7)(?![0-9])`
     happens to handle `-onnx-jetson-xavier-jp7` — pin with tests either way.
- Scope guards: JP5/JP6 vision resolution NOT changed (Neo stays primary);
  vLLM path untouched; no `jetson-xavier-jp7` key in `COMPILATION_TARGETS`
  ever; prefer NO new IAM actions (flag + reviewed rebaseline if unavoidable).

## Critical cross-spec state

`.kiro/specs/vllm-multi-arch-publish-conflict/` code waves 1–3.9 are LANDED in
this branch's working tree (committed with this push): `jetson-xavier-jp7` is
in both publish target maps, fail-closed `resolve_target_platform` exists,
per-target vLLM naming, frontend suffix-arch resolution. Its later waves
(remaining property suites / user-action verification) may still be open —
check its tasks.md checkboxes. The publish-packaging spec was written to
compose with it in either order; its map-totality tests must keep passing when
the new onnx targets are added.

## Suggested execution order

1. Execute `onnx-compile-error-diagnostics` tasks (waves 1→9; wave 1 must FAIL
   pre-fix, that's expected and correct).
2. Deploy portal; re-run an onnx compile; capture the real start-failure
   reason. If it's the role ARN or region-pinned image, open a follow-up spec.
3. Generate design.md then tasks.md for `onnx-jetson-publish-packaging`
   (bugfix workflow, design phase next), then execute.
4. On-hardware: compile → publish → deploy to an `arm64_jp7` thing (e.g.
   `jetson-thor1`) → run workflow inference; JP6 regression check.

## Repo conventions the next session needs

- Backend portal tests: `cd edge-cv-portal/backend/tests && python3 -m pytest <suite> -q -p no:cacheprovider`
  (needs its conftest for the moto `aws_stack` fixture — do NOT use --noconftest
  for these; do NOT hardcode Hypothesis `max_examples`, profiles `portal-fast`/`ci`
  are conftest-registered).
- Frontend: `npx vitest run <file>` from `edge-cv-portal/frontend`; fast-check
  for property tests (see `src/components/vllm-publish/publishState.gating.property.test.ts`).
- `.kiro/steering/builds.md`: move `edge-cv-portal/infrastructure/cdk.out`
  aside before the IAM security guard suite; never portal-deploy while a
  component build is in flight. 4 known-acceptable local-only cdk.out drift
  failures under `test/backend-test/security/` are pre-existing.
- Spec docs follow the `vllm-multi-arch-publish-conflict` house style:
  EARS clauses (1.x defect / 2.x expected / 3.x SHALL CONTINUE TO),
  `isBugCondition_N(X)` pascal formalisms, waves JSON + mermaid in tasks.md,
  sibling-spec amendment tables, exploration-fails-first methodology.

## Untracked junk left out of the commit (intentionally)

Deploy logs (`edge-cv-portal/deploy-*.out`, `.gdk_*`), `cdk.out.bak-*` dirs,
`tmp/`, `.defect8-build-*.json`, `.kiro/tmp_modbus_sim.py`. Do not commit.
