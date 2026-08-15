# ONNX Jetson Publish & Packaging Bugfix Design

## Overview

ONNX vision models cannot reach any Jetson device, and JetPack 7 (Jetson Thor)
has no other vision route at all: Neo's CUDA ceiling is 11.x (JP7 is CUDA 13),
JP7 ships no DLR, and GPU onnxruntime plus the device-side `OnnxRunner` are
already in place waiting for a component that never arrives. The cloud-side
chain breaks in four places between a completed `torch.onnx.export` job and a
deployable Greengrass component:

1. **Publish**: the compiled-ONNX `packaged_components` entry carries
   `target: 'onnx'`, which is a key of neither `TARGET_TO_LOCAL_SERVER` nor
   `TARGET_TO_PLATFORM` in `greengrass_publish.py`. On the current tree
   (with `vllm-multi-arch-publish-conflict` waves 1–3 landed) the fail-closed
   `resolve_target_platform` raises `PublishError` and the target is recorded
   failed; on a pre-sibling tree the old `TARGET_TO_PLATFORM.get(target,
   'amd64')` default silently stamps amd64. Either way, no Jetson component.
2. **Packaging**: `packaging.py`'s generic Phase 2 loop treats the completed
   `onnx` compilation entry like a Neo artifact — ONE arch-less entry, and a
   `create_dda_manifest` manifest with NO top-level `runtime` field, so the
   device's `__load_runtime_config` would default to the DLR runner against an
   ONNX artifact (and JP7 has no DLR). The BYO-import bypass
   (`package_onnx_component`) already produces the correct `runtime: onnx`
   payload but its default target list omits `jetson-xavier-jp7`.
3. **Workflow packaging**: `ARCH_TO_PUBLISH_TARGET[arm64_jp7] =
   'jetson-xavier-jp7'` deliberately fails closed for vision refs because
   nothing could publish that target — correct then, wrong once ONNX
   components exist for JP7.
4. **Deployment arch resolution**: the frontend `inferComponentTargetArchs`
   happens to resolve `...-onnx-jetson-xavier-jp7` to `arm64_jp7` via the
   generic JetPack-token regex, but nothing pins that; the backend
   `deployments.py` needs verification, not change.

**The fix approach is decided (bugfix.md, not re-litigated here): per-JetPack
ONNX components.** One compiled ONNX artifact yields one component per
supported JetPack (`model-{safe}-onnx-jetson-xavier-jp5/-jp6/-jp7`), each with
platform `aarch64` and a HARD dependency on exactly that JetPack's LocalServer
variant — the same shape (and for the same Greengrass top-level
`ComponentDependencies` reason) as the vLLM per-JetPack fix.

The design's central economy: **the publish pipeline needs no new mechanism.**
`greengrass_publish.py`'s vision branch is already fully generic over the
target vocabulary — per-target naming (`f"{component_name}-{target_suffix}"`),
fail-closed platform resolution, Greengrass name validation, per-target
`published_components` write-back all landed with the sibling specs. Adding
three ONNX packaging-target ids to the two module maps makes the entire
publish path work unchanged. The real new code is in `packaging.py` (fan the
one exported artifact out into per-JetPack entries with a `runtime: onnx`
manifest, mirroring the proven BYO layout) and a narrow resolution extension
in `workflow_packaging.py` (arm64_jp7 accepts the ONNX target id in addition
to the reserved Neo-shaped id).

The new packaging-target vocabulary is `onnx-jetson-xavier-jp5` /
`onnx-jetson-xavier-jp6` / `onnx-jetson-xavier-jp7` — chosen so the untouched
suffix transform derives exactly the component names bugfix.md 2.1 requires,
and so the ids can never collide with a Neo target of the same model (a model
with both Neo JP5 and ONNX artifacts publishes
`model-{safe}-jetson-xavier-jp5` AND `model-{safe}-onnx-jetson-xavier-jp5` as
distinct identities).

Scope guards, binding: JP7 is the primary deliverable; JP5/JP6 vision
resolution in `workflow_packaging.py` is NOT changed (Neo stays their primary
route — the ONNX JP5/JP6 targets do NOT satisfy arm64_jp5/jp6 workflow
coverage). The vLLM path is untouched. No `jetson-xavier-jp7` key ever joins
`COMPILATION_TARGETS` (guarded by `test_onnx_compile_diagnostics_exploration.py`
case 9, which must keep passing). No new IAM action is needed — packaging and
publish reuse the exact code paths and AWS calls already granted.

## Glossary

- **Bug_Condition (C)**: the four compounding conditions formalized in
  `bugfix.md` as `isBugCondition_1/2/3/4` — an ONNX publish target unmapped in
  either module map (C1), a compiled-ONNX packaging run that emits one
  arch-less/runtime-less entry or a BYO default list omitting JP7 (C2), an
  `arm64_jp7` workflow packaging with a vision ref whose model has a
  publishable JP7 ONNX component (C3), and a per-JetPack ONNX component name
  whose deployment arch resolution is unpinned (C4).
- **Property (P)**: the desired behavior under C — per-JetPack ONNX components
  published with correct platform/LocalServer stamping, per-JetPack packaged
  entries carrying a `runtime: onnx` manifest, `arm64_jp7` workflow resolution
  to the JP7 ONNX component, and exact singleton arch inference.
- **Preservation**: Neo vision publish byte-for-byte, the vLLM publish and
  packaging branches, Neo Phase 2 packaging (manifest still runtime-less —
  DLR is correct for Neo artifacts), BYO-import layout and explicit target
  lists, non-JP7 workflow resolution, device-side runtime selection, existing
  frontend arch inference, and the IAM baseline.
- **ONNX_Compiled_Target**: one of the new packaging-target ids
  `onnx-jetson-xavier-jp5` / `onnx-jetson-xavier-jp6` /
  `onnx-jetson-xavier-jp7`. Produced only by the compiled-ONNX packaging
  fan-out; consumed by publish (map keys) and by workflow packaging (JP7
  only).
- **ONNX_ARCH_TO_TARGET**: the new producer map in `packaging.py`,
  `{'arm64_jp5': 'onnx-jetson-xavier-jp5', 'arm64_jp6':
  'onnx-jetson-xavier-jp6', 'arm64_jp7': 'onnx-jetson-xavier-jp7'}` — the
  single source of the vocabulary, mirrored (with keep-in-sync comments) the
  same way `packaging.VLLM_ARCH_TO_TARGET` is mirrored today.
- **Per_JetPack_ONNX_Component**: `f"{base}-{target}"` where base is the
  record's `model-{safe}` component name and target is an
  ONNX_Compiled_Target (the ids contain no underscore, so the existing
  `target.replace('_', '-')` suffix transform is the identity on them), e.g.
  `model-yolo-test-onnx-jetson-xavier-jp7`.
- **Compiled_ONNX_Manifest**: the `manifest.json` of a compiled-ONNX component
  ZIP — the Phase 1 `create_dda_manifest` output's `model_graph` + `dataset`,
  plus top-level `runtime: 'onnx'` and `runtime_artifact: 'model.onnx'`,
  minus `compilable_models` (which describes the .pt input that is not in
  this ZIP; the device reader never consults it).
- **BYO_Import_Path**: `package_onnx_component` + the `is_onnx_import` bypass
  in `package_components` — already correct except the JP7 omission in its
  default target list. It keeps its existing Neo-shaped target ids
  (`jetson-xavier-jp5/jp6`, `x86_64-cpu`, now `+ jetson-xavier-jp7`) because
  published BYO components already exist under those ids and workflow
  packaging already matches them.
- **resolve_target_platform / TARGET_TO_LOCAL_SERVER / TARGET_TO_PLATFORM**:
  the landed fail-closed publish resolution in `greengrass_publish.py`
  (lines ~77/88/180) this spec composes with.
- **resolve_model_components**: the vision/vLLM published-shape reader in
  `workflow_packaging.py` (line ~1262) whose coverage check this spec extends
  for `arm64_jp7` only.
- **OnnxRunner contract**: the device loads
  `<version_dir>/<stage_type>/<runtime_artifact or 'model.onnx'>` when the
  manifest carries `runtime: onnx` (`lfv_model_template.py`
  `__load_runtime_config` → `inference_runtimes.make_runner`); absent
  `runtime` ⇒ DLR. No device-side change in this spec.

## Bug Details

### Bug Condition

The full formal specifications live in `bugfix.md` (Bug Conditions and
Properties). Summarized against the current tree:

**Defect 1 — `onnx` publish target unmapped** (`isBugCondition_1`): a publish
whose `packaged_components` contain a packaged ONNX entry whose `target` is a
key of neither `TARGET_TO_LOCAL_SERVER` nor `TARGET_TO_PLATFORM`. On this
tree `resolve_target_platform('onnx')` raises `PublishError` ("Unsupported
compile target 'onnx'…") and the target is recorded failed — confirmed by
reading `greengrass_publish.py` lines 180–203 and the target loop's
`except PublishError` arm (~line 1075).

**Defect 2 — compiled-ONNX packaging arch-less and runtime-less**
(`isBugCondition_2`): a training record with a completed
`compilation_jobs` entry `{target: 'onnx', export_format: 'onnx', status:
'Completed', compiled_model_s3: <model.tar.gz>}` enters the generic Phase 2
loop (`packaging.py` line ~834) and yields ONE entry `{target: 'onnx', …}`
whose ZIP holds the `create_dda_manifest` manifest — `model_graph`,
`compilable_models`, `dataset`, and NO `runtime` key (lines 155–170). Also:
the BYO default list `['jetson-xavier-jp5', 'jetson-xavier-jp6',
'x86_64-cpu']` (line 757) omits `jetson-xavier-jp7`.

**Defect 3 — workflow packaging fails closed for `arm64_jp7` vision refs**
(`isBugCondition_3`): `resolve_model_components` maps every selected arch
through `ARCH_TO_PUBLISH_TARGET` (line ~1348), computes coverage against the
published entries' `target` set, and raises the uncovered-architecture
`PackagingError` for `arm64_jp7` — exactly as the map's comment (lines
269–276) says it deliberately must while nothing can publish that id.

**Defect 4 — deployment arch resolution unpinned for ONNX names**
(`isBugCondition_4`): `inferComponentTargetArchs` (`archCompatibility.ts`)
matches `/(?:jp|jetpack)(4|5|6|7)(?![0-9])/g` over the whole name, so
`model-x-onnx-jetson-xavier-jp7` resolves `{arm64_jp7}` — by accident of the
generic token, pinned by no test. `CreateDeployment.tsx` filters non-gated
components through that inference (lines ~667 and ~790) with no ONNX-name
coverage. `deployments.py` gates only `model-vllm-*` and workflow components
by architecture, and LocalServer minimum-version gating keys off the
installed LocalServer component name (`local_server_component_arch`), not
model names — expected safe, unverified.

### Examples

- Package + publish `yolo_test` v7 (training `6a43ff2b-50ec-4441-b5f0-d84ccfef1400`)
  after a completed `onnx` export. Expected: three DEPLOYABLE components
  `model-yolo-test-onnx-jetson-xavier-jp5/-jp6/-jp7`, each aarch64 with a HARD
  dep on its own `…LocalServer.arm64JP{N}`. Actual today: packaging writes one
  `target: 'onnx'` entry; publish records it failed with
  `PublishError: Unsupported compile target 'onnx'`.
- Even if that entry were force-published: its manifest has no `runtime`
  field, so a device would instantiate the DLR runner against `model.onnx`
  — and a JP7 device has no DLR at all.
- Import a BYO ONNX model and package with no explicit targets. Expected:
  JP5, JP6, JP7, and x86 entries. Actual: the default list stops at JP6 —
  JP7 unreachable even on the path that already gets the manifest right.
- Package a workflow that references `rf-detr` v7 (training
  `ede4bd32-e12c-4e88-8139-75d622e53ad0`) for `arm64_jp7`. Expected (post-fix,
  with the JP7 ONNX component published): resolution succeeds and the workflow
  component carries the model dependency. Actual: `PackagingError: Model
  'rf-detr' has no published Greengrass component for the selected
  architecture(s) arm64_jp7 (target jetson-xavier-jp7)…`.
- Deploy screen for thing `jetson-thor1` (`arm64_jp7`). Expected: the JP7
  ONNX component offered, the JP5/JP6 ONNX siblings labeled incompatible.
  Today there is no component to show; post-fix this behavior must be pinned,
  not assumed.
- Edge case (expected, not a bug): a model whose sanitized name pushes
  `model-{safe}-onnx-jetson-xavier-jp7` past 128 characters must fail closed
  per target with `PublishError` from the landed
  `validate_greengrass_component_name` — never a create attempt.
- Edge case (expected, not a bug): a workflow selecting `arm64_jp7` whose
  vision model has NO published JP7 ONNX component keeps failing closed with
  the accurate uncovered-architecture error naming the model and arch.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Neo vision publish byte-for-byte for every currently mapped target
  (`jetson-xavier`, `jetson-xavier-jp5/-jp6/-jp7`, `arm64-cpu`, `x86_64-cpu`,
  `x86_64-cuda`): same names, platforms, LocalServer variants, recipes (3.1);
  a genuinely unknown target still fails closed through
  `resolve_target_platform` (3.2) — the new entries narrow the unmapped set,
  never reintroduce a default.
- The vLLM publish path exactly as `vllm-multi-arch-publish-conflict` landed
  it: per-JetPack naming, cloud-side version derivation, atomicity gate,
  write-back shape (3.3); the vLLM packaging branch (3.6).
- Neo Phase 2 packaging: same layout, same runtime-less `create_dda_manifest`
  manifest — the DLR default is CORRECT for Neo/DLR artifacts (3.4).
- BYO-import packaging: same ZIP layout (`manifest.json` at the root with
  `runtime: 'onnx'` and the merged `dataset` block, artifact nested under
  `<stage_type>/`), and explicit caller-requested target lists honored
  verbatim (3.5) — only the DEFAULT list gains JP7.
- Workflow packaging for `arm64_jp4/jp5/jp6`, `x86_64`, `x86_64_nvidia`:
  today's `ARCH_TO_PUBLISH_TARGET` resolution against Neo-published targets,
  same coverage semantics, same uncovered-architecture error — JP5/JP6 vision
  resolution is NOT changed to prefer ONNX (3.7);
  `ARCH_TO_LOCAL_SERVER_COMPONENT` and `ARCH_TO_GG_PLATFORM` untouched (3.8).
- `COMPILATION_TARGETS`: still exactly seven targets, still no
  `jetson-xavier-jp7` Neo target; the `onnx` export target definition and
  `_start_onnx_export_job` untouched (3.9). The landed
  `onnx-compile-error-diagnostics` contracts (write-once error fields,
  `classify_poll_kind`, no fabricated job names) untouched (3.10).
- Device-side runtime selection: absent/`dlr` ⇒ DLR runner, `onnx` ⇒
  `OnnxRunner` with TensorRT → CUDA → CPU preference — no device code in this
  spec (3.11).
- `CompilationTab.tsx`'s `onnx` compile target entry and the SmartImport ONNX
  flow offered exactly as today (copy updates describing JP7 allowed, no
  target additions/removals) (3.12); `inferComponentTargetArchs` resolves
  every existing (non-ONNX) name exactly as today (3.13).
- IAM: no new action, no baseline drift. Packaging reuses the packaging
  Lambda's existing S3/DynamoDB calls; publish reuses the publish Lambda's
  existing Greengrass calls. If execution uncovers an unavoidable new action
  it must be flagged and rebaselined only through the security gate's
  documented protocol — never silently (3.14).

**Scope:**
All inputs that do not involve an ONNX artifact reaching a Jetson are
completely unaffected: every Neo compile/package/publish, every vLLM
package/publish, every workflow packaging without `arm64_jp7` vision refs,
every deployment of existing component names, and every IAM statement.

## Hypothesized Root Cause

Confirmed by reading the current tree (sibling waves have landed since
bugfix.md was written; line references re-verified):

1. **A target id outside the publish vocabulary**
   (`greengrass_publish.py` lines 77–96): `TARGET_TO_LOCAL_SERVER` /
   `TARGET_TO_PLATFORM` enumerate the Neo targets. `'onnx'` was never a
   deployable target id — it names an export FORMAT, not a device class. The
   landed fail-closed `resolve_target_platform` (line 180) correctly refuses
   it; the fix is not to weaken that but to introduce real per-JetPack target
   ids and map them.
2. **Format-shaped packaging output** (`packaging.py` lines 797–875): the
   generic Phase 2 loop assumes every completed compilation entry is a
   per-device Neo artifact, so the `onnx` entry inherits both wrong shapes:
   one entry keyed by the format id, and the Neo manifest whose missing
   `runtime` key is only correct for DLR artifacts. The BYO bypass proves the
   correct payload shape (`runtime: onnx`, artifact under `<stage_type>/`)
   on-device on JP5/JP6 today; its JP7 omission (line 757) predates the JP7
   LocalServer/publish mappings, which now exist.
3. **A deliberately unreachable map entry** (`workflow_packaging.py` lines
   265–277): `ARCH_ARM64_JP7: 'jetson-xavier-jp7'` was reserved with an
   explicit fail-closed comment. Correct until something can publish for JP7;
   the comment itself instructs the successor.
4. **Convention-by-coincidence in the frontend**: the JetPack-token regex is
   generic enough to cover the new names, so no production change is needed —
   but an untested resolution is not a designed one. The risk is drift: a
   future tightening of the regex or of `CreateDeployment` filtering could
   silently strand ONNX components; tests pin the contract.

**Why per-JetPack components (recorded, decided in bugfix.md).** Greengrass
`ComponentDependencies` is top-level, not per-manifest, so one shared
component cannot carry a different LocalServer HARD dependency per JetPack —
it would place an unsatisfiable dependency on every device of the other
JetPacks. Identical reasoning to the vLLM per-JetPack fix.

## Correctness Properties

Property 1: Bug Condition — A compiled ONNX artifact reaches per-JetPack Jetson components

_For any_ training record with a completed `onnx` compilation entry (the bug
condition `isBugCondition_2`, composing into `isBugCondition_1` at publish),
the fixed packaging SHALL emit one `packaged_components` entry per
ONNX_Compiled_Target (JP5, JP6, JP7 — no `target: 'onnx'` entry), each ZIP
carrying a Compiled_ONNX_Manifest (`runtime: 'onnx'`,
`runtime_artifact: 'model.onnx'`, artifact at `<stage_type>/model.onnx`), and
the fixed publish SHALL resolve every such target through both module maps to
platform `aarch64` and a HARD dependency on exactly that JetPack's
LocalServer variant, publishing distinct component names
`model-{safe}-onnx-jetson-xavier-jp{N}` with per-target
`published_components` entries of status `published`.

**Validates: Requirements 2.1, 2.2, 2.3, 2.5, 2.6, 2.7**

Property 2: Preservation — Non-bug inputs are behaviorally identical

_For any_ input where none of `isBugCondition_1…4` holds, the fixed code
SHALL produce the same result as the original code. Concretely: every
currently mapped publish target resolves to exactly today's
`(LocalServer variant, platform)` pair and recipe; a genuinely unknown target
still raises `PublishError` with no create; Neo Phase 2 packaging output and
its runtime-less manifest are unchanged; BYO-import packaging with an
explicit target list honors it verbatim with the same ZIP layout; the vLLM
packaging and publish branches are untouched; workflow packaging for every
architecture other than `arm64_jp7` resolves exactly as today;
`inferComponentTargetArchs` returns exactly today's set for every existing
component name; and the synthesized IAM policy is unchanged.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14**

Property 3: Fix Checking — ONNX target maps are total and compose fail-closed

_For any_ target in `values(packaging.ONNX_ARCH_TO_TARGET)`, the target SHALL
be a key of BOTH `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM`,
`resolve_target_platform` SHALL return `aarch64` without raising, and
`resolve_local_server_component` SHALL return the `arm64JP{N}` variant whose
JetPack major equals the target's; _for any_ target absent from either map,
`resolve_target_platform` SHALL still raise `PublishError` with no
`create_component_version` call. The `vllm-multi-arch-publish-conflict`
map-totality discipline (every value of `packaging.VLLM_ARCH_TO_TARGET` in
both maps) SHALL hold with the ONNX entries present.

**Validates: Requirements 2.2, 2.3, 3.2**

Property 4: Fix Checking — Compiled-ONNX fan-out and manifest; BYO default gains JP7

_For any_ completed `onnx` compilation entry, the fixed `package_components`
SHALL produce exactly the ONNX_Compiled_Target entry set from ONE uploaded
artifact, whose `manifest.json` at the ZIP root carries `runtime: 'onnx'`,
`runtime_artifact` naming the `.onnx` file, the Phase 1 `model_graph` and
`dataset` blocks, and whose `.onnx` artifact sits at `<stage_type>/model.onnx`
(the path `OnnxRunner` resolves). _For any_ BYO ONNX import packaged with no
explicit target list, the defaulted list SHALL be exactly
`['jetson-xavier-jp5', 'jetson-xavier-jp6', 'jetson-xavier-jp7',
'x86_64-cpu']`; _for any_ explicit list, it SHALL be honored verbatim.

**Validates: Requirements 2.6, 2.7, 2.8, 3.5**

Property 5: Fix Checking — Derived ONNX component names satisfy every gate

_For any_ model name and any ONNX_Compiled_Target, the derived
Per_JetPack_ONNX_Component name SHALL equal
`{base}-onnx-jetson-xavier-jp{N}`, start with `model-`, match the Greengrass
charset `^[a-zA-Z0-9._-]+$`, carry a JetPack token matching
`/(?:jp|jetpack)(4|5|6|7)(?![0-9])/` whose major equals the target's JetPack,
and NOT start with `model-vllm-` for any base derived from
`model-{safe}` naming; a name exceeding 128 characters SHALL fail closed for
that target with `PublishError` and no create.

**Validates: Requirements 2.1, 2.4**

Property 6: Fix Checking — arm64_jp7 workflow resolution is exact and stays fail-closed

_For any_ workflow with a vision `model_ref` packaged for `arm64_jp7` where
the model's `published_components` contain a published entry whose target is
`onnx-jetson-xavier-jp7` (or the BYO `jetson-xavier-jp7`), resolution SHALL
return that entry's component name and raise no uncovered-architecture error;
_for any_ such workflow where the model has NO published JP7-accepted entry,
resolution SHALL raise `PackagingError` naming the model and `arm64_jp7`; and
_for any_ selected architecture other than `arm64_jp7`, the accepted target
set SHALL be exactly today's singleton — an `onnx-jetson-xavier-jp5/jp6`
entry SHALL NOT satisfy `arm64_jp5`/`arm64_jp6` coverage.

**Validates: Requirements 2.9, 2.10, 3.7**

Property 7: Fix Checking — Frontend arch inference and deploy filtering are pinned

_For any_ Per_JetPack_ONNX_Component name,
`inferComponentTargetArchs` SHALL return exactly the singleton set of the
name's JetPack architecture (`…-onnx-jetson-xavier-jp7` → `['arm64_jp7']`),
and the Create/Revise Deployment filtering SHALL offer the component for a
device of exactly that architecture and label it incompatible for a device of
any other recorded architecture; _for any_ existing (non-ONNX) name, the
inference SHALL be unchanged.

> **Correction (task 5.3 verification):** the deploy-screen clause is scoped
> to devices with a RECORDED architecture, per Requirement 2.12. For a device
> with no recorded architecture the screen fails OPEN for ALL name-inferred
> (non-gated) components — ONNX and Neo jp5/jp6 alike — and shows the
> "Selected device(s) have no recorded architecture" warning as the
> user-facing guard (pre-existing, intentional `archIncompatReason` behavior
> in `CreateDeployment.tsx`). The pinned contract for the null-arch case is
> warning + fail-open, not filtering.

**Validates: Requirements 2.11, 2.12, 3.13**

Property 8: Fix Checking — Backend deployment gates ignore ONNX model names

_For any_ Per_JetPack_ONNX_Component name, `deployments.py` SHALL apply no
vLLM architecture gate to it (the name does not start with `model-vllm-`, so
`collect_vllm_component_manifests` produces no manifest for it), and
LocalServer minimum-version gating SHALL continue to key off the installed
LocalServer component name only — verified by test; production change ONLY if
verification shows a gap.

**Validates: Requirements 2.13**

## Fix Implementation

### Changes Required

Step order matters: step 1 (publish maps) has no prerequisites and unblocks
everything else; steps 2–4 (packaging) produce the entries step 1 consumes;
step 5 (workflow packaging) consumes step 1's published shape; steps 6–7 are
frontend/docs.

**File**: `edge-cv-portal/backend/functions/packaging.py`

1. **The vocabulary, at its producer** (2.1, 2.6): add a module-level map
   next to `VLLM_ARCH_TO_TARGET` (same mirrored-pure-constant convention):

   ```python
   # Target_Architecture -> compiled-ONNX packaging target id. One compiled
   # ONNX artifact packages one entry per supported JetPack (per-JetPack
   # components: Greengrass ComponentDependencies is top-level, so each
   # JetPack needs its own component with its own LocalServer HARD dep).
   # KEEP IN SYNC with greengrass_publish.py TARGET_TO_LOCAL_SERVER /
   # TARGET_TO_PLATFORM and workflow_packaging.py's arm64_jp7 acceptance.
   ONNX_ARCH_TO_TARGET = {
       'arm64_jp5': 'onnx-jetson-xavier-jp5',
       'arm64_jp6': 'onnx-jetson-xavier-jp6',
       'arm64_jp7': 'onnx-jetson-xavier-jp7',
   }
   ONNX_COMPILED_TARGETS = list(ONNX_ARCH_TO_TARGET.values())
   ```

   The ids contain no underscore, so publish's `target.replace('_', '-')`
   suffix transform is the identity and the derived component name is
   `model-{safe}-onnx-jetson-xavier-jp{N}` with zero publish-side naming code.

2. **`package_compiled_onnx_component(compiled_model_s3, dda_manifest,
   s3_client, usecase) -> str`** (2.7): new function mirroring
   `package_onnx_component`'s proven payload shape, fed by the export job's
   artifact instead of an import package:
   - download `compiled_model_s3` (the export training job's
     `model.tar.gz`; the export script writes `model.onnx` into
     `/opt/ml/model`, so the archive holds it at the root), extract, and
     locate the `.onnx` file by recursive scan (same tolerance as the BYO
     path);
   - build the Compiled_ONNX_Manifest from the Phase 1 `dda_manifest`:
     keep `model_graph` and `dataset`, DROP `compilable_models` (it names the
     `.pt` input that is not in this ZIP; the device reader never consults
     it), add `runtime: 'onnx'` and `runtime_artifact: <onnx filename>`;
   - assemble `payload/manifest.json` at the root and the artifact at
     `payload/<stage_type>/<onnx filename>` where `stage_type =
     dda_manifest['model_graph']['stages'][0]['type']` — exactly the layout
     `model_convertor.py` symlinks and `OnnxRunner` resolves
     (`<version_dir>/<stage_type>/model.onnx`), byte-compatible with the BYO
     layout already working on JP5/JP6 devices;
   - zip and upload to
     `model_artifacts/model-{uuid}/{uuid}_greengrass_model_component.zip`
     (same key scheme and existing S3 permissions).

3. **Fan-out in the generic path** (2.6): in `package_components`, after the
   `completed_jobs` filtering (line ~803) and Phase 1, partition the jobs:

   ```python
   onnx_export_jobs = [j for j in completed_jobs
                       if j.get('target') == 'onnx'
                       or j.get('export_format') == 'onnx']
   neo_jobs = [j for j in completed_jobs if j not in onnx_export_jobs]
   ```

   The Neo loop runs over `neo_jobs` unchanged (3.4). For the ONNX export
   entry (at most one per record — `start_compilation_job` writes one entry
   per requested target and `COMPILATION_TARGETS` has one `onnx` key), call
   `package_compiled_onnx_component` ONCE and append one entry per
   ONNX_Compiled_Target:

   ```python
   packaged_components += [
       {'target': t, 'component_package_s3': component_s3_uri,
        'status': 'packaged'}
       for t in ONNX_COMPILED_TARGETS
   ]
   ```

   A packaging failure appends per-target `{'target': t, 'status': 'failed',
   'error': …}` entries, mirroring the Neo loop's per-target failure shape.
   The `requested_targets` filter keeps its existing semantics — the user
   requests the `onnx` id (the compile-target id the UI knows) and the
   fan-out expands it; `packaged_components` is still written wholesale with
   the same update expression, and the audit event's `targets` list now
   carries the fanned-out ids.

4. **BYO default list gains JP7** (2.8): line 757 becomes

   ```python
   onnx_targets = requested_targets or [
       'jetson-xavier-jp5', 'jetson-xavier-jp6', 'jetson-xavier-jp7',
       'x86_64-cpu']
   ```

   No other BYO change: `jetson-xavier-jp7` is fully mapped in publish since
   the sibling spec landed (aarch64 + `…LocalServer.arm64JP7`), and
   `ARCH_TO_PUBLISH_TARGET[arm64_jp7]` already matches this exact id, so BYO
   JP7 workflow resolution works with no workflow_packaging change. Explicit
   caller lists are honored verbatim as today (3.5).

**File**: `edge-cv-portal/backend/functions/greengrass_publish.py`

5. **Map the ONNX targets — the entire publish fix** (2.2, 2.3):
   - `TARGET_TO_LOCAL_SERVER` (line ~77) gains
     `'onnx-jetson-xavier-jp5': 'aws.edgeml.dda.LocalServer.arm64JP5'`,
     `'onnx-jetson-xavier-jp6': 'aws.edgeml.dda.LocalServer.arm64JP6'`,
     `'onnx-jetson-xavier-jp7': 'aws.edgeml.dda.LocalServer.arm64JP7'`
     with a comment: compiled-ONNX per-JetPack targets, KEEP IN SYNC with
     `packaging.ONNX_ARCH_TO_TARGET`.
   - `TARGET_TO_PLATFORM` (line ~88) gains the same three keys → `'aarch64'`.
   - `resolve_local_server_component`'s fail-closed message (line ~171)
     appends the three new ids to its supported-target list (message-only;
     the raising path is unreachable for mapped targets).
   - NOTHING else: `resolve_target_platform` resolves the new keys without
     code change; the vision branch derives
     `model-{safe}-onnx-jetson-xavier-jp{N}`, validates it via the landed
     `validate_greengrass_component_name`, generates the recipe through
     `generate_component_recipe` (aarch64 manifest platform, HARD
     `arm64JP{N}` LocalServer dep), and writes the per-target
     `published_components` entries (2.5) — all existing generic code.

**File**: `edge-cv-portal/backend/functions/workflow_packaging.py`

6. **arm64_jp7 accepts the ONNX target id** (2.9, 2.10, 3.7):
   - add, next to `ARCH_TO_PUBLISH_TARGET`:

     ```python
     # Additional publish-target ids accepted per arch when resolving VISION
     # published_components. arm64_jp7's vision route is ONNX (Neo cannot
     # target CUDA 13): compiled-ONNX publishes 'onnx-jetson-xavier-jp7'
     # (packaging.ONNX_ARCH_TO_TARGET — keep in sync) and BYO ONNX imports
     # publish 'jetson-xavier-jp7' (already the primary id above). JP5/JP6
     # deliberately get NO onnx acceptance here: Neo remains their primary
     # vision route and their coverage semantics are unchanged.
     ARCH_TO_EXTRA_PUBLISH_TARGETS = {
         ARCH_ARM64_JP7: ('onnx-jetson-xavier-jp7',),
     }

     def publish_targets_for_arch(arch):
         """Accepted published_components target ids for one arch, or ()
         when the arch has no known publish target (caller fails closed)."""
         primary = ARCH_TO_PUBLISH_TARGET.get(arch)
         if not primary:
             return ()
         return (primary,) + ARCH_TO_EXTRA_PUBLISH_TARGETS.get(arch, ())
     ```

   - in `resolve_model_components` (lines ~1346–1381): build
     `targets_of_arch[arch] = publish_targets_for_arch(arch)` (empty tuple →
     the existing no-known-target `PackagingError`, message unchanged);
     coverage becomes "any accepted id present in `published_targets`"; the
     uncovered-architecture message renders the singleton case byte-identical
     to today (`(target jetson-xavier-jp5)`) and the multi-id case as
     `(targets jetson-xavier-jp7 or onnx-jetson-xavier-jp7)`; the resolved
     `names` set selects entries whose target is in the union of accepted
     ids. All other semantics (vLLM singular shape first, fail-closed on no
     record / no entries, multi-name divergence omission in
     `model_component_dependencies`) untouched.
   - rewrite the `ARCH_ARM64_JP7` comment in `ARCH_TO_PUBLISH_TARGET`
     (lines 269–276): `jetson-xavier-jp7` is now producible (BYO ONNX import
     publish), and compiled-ONNX coverage arrives via
     `ARCH_TO_EXTRA_PUBLISH_TARGETS` — ONNX is the delivered JP7 vision
     route (2.9's comment-update clause).

**File**: `edge-cv-portal/frontend/src/pages/deployments/archCompatibility.ts`

7. **No production change — pin it** (2.11, 2.12): extend the module comment
   to state that per-JetPack ONNX model components
   (`model-{safe}-onnx-jetson-xavier-jp{N}`) are non-gated components resolved
   by the JetPack-token inference to their singleton arch. The contract is
   pinned by new fast-check property cases over generated ONNX names in the
   `archCompatibility` property suite and by extending
   `CreateDeployment.archFilter.test.tsx` with a JP7 ONNX component offered
   for an `arm64_jp7` thing and filtered/labeled for `arm64_jp6`.

**File**: `edge-cv-portal/frontend/src/components/CompilationTab.tsx`

8. **Copy only** (3.12): the BYO ONNX info alert "One package deploys to all
   targets (JetPack 5/6, x86)" gains JetPack 7; optionally the `onnx` compile
   target description notes it is the JP7 vision route. No target additions
   or removals, no behavior change.

**File**: `edge-cv-portal/backend/functions/deployments.py`

9. **Verification only** (2.13): no production change expected. A unit test
   pins that a Per_JetPack_ONNX_Component name activates no vLLM gate
   (`collect_vllm_component_manifests` yields no manifest for it; the 409
   `VLLM_ARCH_UNSUPPORTED` path cannot fire on it) and that
   `local_server_component_arch` ignores model component names. If the test
   surfaces a gap, resolution is added in a follow-up task gated on that
   evidence — not speculatively.

**IAM**: no change (3.14). Step 2 uses the packaging Lambda's existing
`s3:GetObject`/`PutObject` on the use-case bucket (same keys scheme); step 5
changes only module dicts. No security-baseline rebaseline is expected; if
one becomes necessary it follows the gate's documented protocol and is
flagged explicitly.

## Cross-Spec Documentation Consistency

All work lands on the existing branch `spec/jetpack7-support` — no new
branch. Amendment notes (short appended notes referencing
`.kiro/specs/onnx-jetson-publish-packaging/`, not rewrites):

| Sibling spec / doc | Affected claim | Amendment note to add |
| --- | --- | --- |
| `.kiro/specs/vllm-multi-arch-publish-conflict/` | Its design treats `packaging.VLLM_ARCH_TO_TARGET` as the producer vocabulary feeding the two publish maps; its map-totality tests assert subset relations over the maps. | Note that the compiled-ONNX targets (`onnx-jetson-xavier-jp5/6/7`, producer `packaging.ONNX_ARCH_TO_TARGET`) joined `TARGET_TO_LOCAL_SERVER` / `TARGET_TO_PLATFORM` under the same totality discipline (mapped in BOTH maps; unmapped targets still fail closed through `resolve_target_platform`), composing in either landing order. |
| `.kiro/specs/onnx-compile-error-diagnostics/` (implemented & deployed) | "Implementing JP7 vision support is explicitly out of scope" and its preservation list ("`packaging.py`, `workflow_packaging.py`, `greengrass_publish.py` … untouched" — true for THAT spec's changes). | Note that this sibling spec now delivers the JP7 vision route those documents deferred, changing those three modules; the diagnostics contracts (write-once reasons, `classify_poll_kind`, case 9's no-JP7-Neo-target guard) are preserved untouched (3.10). |
| `.kiro/specs/jetpack7-support/` (umbrella) | The JP7 rollout narrative lacks a delivered vision route ("DLR-only models are not supported on JP7"). | One paragraph: per-JetPack ONNX components (`model-{safe}-onnx-jetson-xavier-jp7`, platform aarch64, HARD dep `…LocalServer.arm64JP7`) are the delivered JP7 vision route, packaged from the `onnx` export target and resolved by workflow packaging for `arm64_jp7`. |
| `docs/multi-runtime-inference.md` | Describes the ONNX runtime and BYO import flow; compiled-ONNX delivery to Jetson was undelivered. | Record that compiled ONNX exports now package per-JetPack (JP5/JP6/JP7) with `runtime: onnx` manifests, that BYO imports default to JP5/JP6/JP7/x86, and that ONNX is the only JP7 vision runtime. |
| `.kiro/specs/localserver-arch-naming/` | Its target list of `TARGET_TO_LOCAL_SERVER` keys predates the ONNX ids. | Note the three `onnx-jetson-xavier-jp{N}` → `arm64JP{N}` entries, added under the both-maps-or-fail-closed rule its amendment for the vLLM spec already records. |

## Deployment and On-Hardware Verification

Nothing takes effect until the portal is deployed — an explicit user-action
step, never assumed:

1. Lambda code: `packaging.py`, `greengrass_publish.py`,
   `workflow_packaging.py` (functions asset). No infrastructure change.
2. Frontend: `archCompatibility.ts` comment / `CompilationTab.tsx` copy (and
   the test-only additions do not ship, but the frontend bundle redeploys
   with the copy change).

Use the repo's portal deploy path (`deploy-portal.sh`, or
`deploy-infrastructure.sh` + `deploy-frontend.sh`). Per
`.kiro/steering/builds.md`: never portal-deploy while a component build is in
flight, and move `edge-cv-portal/infrastructure/cdk.out` aside before running
the security guard suite.

Post-deploy verification on `https://d23v4ltibogb5x.cloudfront.net`
(account 164152369890, us-east-1), with the user's concrete models:

- **Package → publish**: for `yolo_test` v7 (training
  `6a43ff2b-50ec-4441-b5f0-d84ccfef1400`) and `rf-detr` v7 (training
  `ede4bd32-e12c-4e88-8139-75d622e53ad0`), run the `onnx` compile (now
  diagnosable end-to-end thanks to the deployed diagnostics spec), package,
  and publish; confirm three DEPLOYABLE components per model with the
  `-onnx-jetson-xavier-jp5/-jp6/-jp7` names, aarch64 platforms, and correct
  LocalServer dependencies.
- **Deploy to JP7**: revise/create a deployment for an `arm64_jp7` thing
  (e.g. `jetson-thor1`); confirm the JP7 ONNX component is offered and the
  JP5/JP6 siblings are labeled incompatible; deploy and confirm the device
  reaches DEPLOYED with the model converted and `OnnxRunner` selected
  (`runtime 'onnx'` in the model log line).
- **Workflow inference**: package a workflow referencing the model for
  `arm64_jp7`, deploy it, and run inference end-to-end on the Thor device.
- **JP6 regression**: publish/deploy an existing Neo JP6 model and confirm
  names, recipes, and deployment behavior are unchanged.

Per `.kiro/steering/builds.md`, on-device verification on real hardware is
REQUIRED before this spec's work is called done.

## Testing Strategy

### Validation Approach

Two phases. First, an exploration suite on the UNFIXED tree that surfaces
counterexamples for all four defects and confirms the located root causes
(each has a read-and-confirmed cause; refutation is unlikely but must be
observed, not assumed). Then fix checking against Correctness Properties 1
and 3–8 and preservation checking over Property 2, with the existing suites
as regression gates.

Test commands (repo conventions):
- Backend: from `edge-cv-portal/backend/tests`, WITH the conftest (moto
  `aws_stack` fixture; Hypothesis profiles `portal-fast`/`ci` are
  conftest-registered — do NOT hardcode `max_examples`), venv
  `/home/ubuntu/.venvs/dda-portal-tests`:
  `python3 -m pytest <suite> -q -p no:cacheprovider`
- Frontend: from `edge-cv-portal/frontend`, with
  `PATH="$HOME/.local/node/bin:$PATH"`: `npx vitest run <file>`; fast-check
  for property tests.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs BEFORE
implementing the fix, and confirm or refute the hypothesized causes. If any
is refuted, re-hypothesize before writing a fix.

**Test Plan**: a new exploration suite
`edge-cv-portal/backend/tests/test_onnx_jetson_publish_packaging_exploration.py`
(moto-backed conftest stack, fake Greengrass client following
`test_vllm_multi_arch_publish_exploration.py`) plus a frontend exploration
case. Seed a trained-model record with a completed `onnx` compilation entry
and drive packaging → publish → workflow packaging on the UNFIXED tree.

**Test Cases**:
1. **Compiled-ONNX packaging shape** (`isBugCondition_2`): package a record
   with a completed `{target: 'onnx', compiled_model_s3: …}` entry (S3 seeded
   with a synthetic model.tar.gz holding model.onnx, and a trained artifact
   holding config.yaml + export_artifacts). Assert per-JetPack entries with a
   `runtime: 'onnx'` manifest and `<stage_type>/model.onnx` layout; on
   unfixed code observe ONE `target: 'onnx'` entry whose manifest has NO
   `runtime` key (will fail on unfixed code).
2. **Publish fails closed on `'onnx'`** (`isBugCondition_1`): publish that
   record. Assert three per-JetPack components with aarch64 platforms and
   `arm64JP{N}` HARD deps; on unfixed code observe `resolve_target_platform`
   raising and a `published_components` entry with `status: 'failed'` and the
   "Unsupported compile target 'onnx'" message — also assert directly that
   `isBugCondition_1` holds on the unfixed maps (`'onnx'`, and each
   `onnx-jetson-xavier-jp{N}` id, absent from both maps) so the condition is
   recorded (will fail on unfixed code).
3. **BYO default omits JP7** (`isBugCondition_2`, import arm): package a BYO
   ONNX import with no explicit targets; assert the JP7 target in the
   defaulted entry set; on unfixed code observe `['jetson-xavier-jp5',
   'jetson-xavier-jp6', 'x86_64-cpu']` (will fail on unfixed code).
4. **arm64_jp7 workflow resolution** (`isBugCondition_3`): with a record
   seeded to carry a published `onnx-jetson-xavier-jp7` entry, resolve model
   components for `archs=['arm64_jp7']`; assert the JP7 ONNX component name
   resolves; on unfixed code observe the uncovered-architecture
   `PackagingError` naming the model and `arm64_jp7` (will fail on unfixed
   code).
5. **Fail-closed retained without a JP7 component** (edge case, encodes
   2.10): the same workflow resolution against a record with only Neo JP5/JP6
   entries must raise the uncovered-architecture error naming the model and
   arch — expected to PASS on unfixed code and REQUIRED to keep passing after
   the fix (do NOT invert).
6. **Frontend inference unpinned** (`isBugCondition_4`): new cases in the
   frontend suite asserting
   `inferComponentTargetArchs('model-x-onnx-jetson-xavier-jp7') ===
   ['arm64_jp7']` etc. — these PASS on unfixed code (the regex already
   covers the names) and document that the fix here is pinning, not code;
   the deploy-screen filtering case in `CreateDeployment.archFilter.test.tsx`
   is added with them.

**Expected Counterexamples**:
- One `target: 'onnx'` packaged entry; manifest without `runtime`.
- `PublishError: Unsupported compile target 'onnx'` and a failed target with
  no component version.
- The three-entry BYO default list.
- `PackagingError … no published Greengrass component for the selected
  architecture(s) arm64_jp7 (target jetson-xavier-jp7)`.
- Possible causes if refuted: a different Phase 2 dispatch than read, a
  publish branch special-casing 'onnx', or workflow coverage matching on
  something other than the target id — each would force re-hypothesis.

### Fix Checking

**Goal**: for all inputs where a bug condition holds, the fixed code produces
the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition_1(X) OR isBugCondition_2(X)
             OR isBugCondition_3(X) OR isBugCondition_4(X) DO
  result := pipeline'(X)     // package' / publish' / resolve' / infer'
  ASSERT expectedBehavior(result)   // Properties 1, 3, 4, 5, 6, 7, 8
END FOR
```

### Preservation Checking

**Goal**: for all inputs where no bug condition holds, the fixed code
produces the same result as the original.

**Pseudocode:**
```
FOR ALL X WHERE NOT (isBugCondition_1(X) OR isBugCondition_2(X)
                     OR isBugCondition_3(X) OR isBugCondition_4(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Testing Approach**: property-based testing (Hypothesis backend, fast-check
frontend) — the preservation surface is broad and mechanical (arbitrary Neo
target subsets, arbitrary explicit BYO target lists, arbitrary component
names, arbitrary non-JP7 architecture selections), which generated cases
cover far better than hand-picked examples, and it is the convention every
sibling suite already follows.

**Test Plan**: observe UNFIXED behavior first, encode it as properties that
must still hold after the fix.

**Test Cases**:
1. **Publish target-map baseline**: record the exact
   `(LocalServer variant, platform)` pair every currently mapped target
   resolves to, and that a genuinely unknown target raises `PublishError`
   with no create — assert unchanged after the ONNX entries land (3.1, 3.2).
2. **Neo Phase 2 packaging**: over generated Neo-only completed-job sets,
   the packaged entry set, ZIP layout, and runtime-less manifest are
   byte-identical (3.4).
3. **BYO explicit lists**: over generated explicit target lists, the entry
   set equals the request verbatim and the ZIP layout is unchanged (3.5).
4. **vLLM branches**: the vLLM packaging dispatch and publish behavior are
   untouched — re-run the existing vLLM suites as the gate (3.3, 3.6).
5. **Non-JP7 workflow resolution**: over generated published shapes and
   architecture selections excluding `arm64_jp7`, resolution and error
   messages are identical; ONNX JP5/JP6 entries never satisfy
   arm64_jp5/jp6 coverage (3.7, 3.8).
6. **Frontend inference**: over generated existing-name shapes (LocalServer
   variants, vLLM suffixed names, Neo vision names, token-less names),
   `inferComponentTargetArchs` is unchanged (3.13).
7. **Compile targets**: `COMPILATION_TARGETS` still has exactly seven
   targets and no `jetson-xavier-jp7` — the existing diagnostics case 9
   guard is the gate; do NOT duplicate it, re-run it (3.9, 3.10).

### Unit Tests

- `ONNX_ARCH_TO_TARGET` / `ONNX_COMPILED_TARGETS`: closed vocabulary, ids
  contain the `-onnx-` token and a JetPack token, suffix transform identity.
- Map totality both ways for the ONNX ids: every value of
  `ONNX_ARCH_TO_TARGET` in BOTH publish maps; `resolve_target_platform`
  returns `aarch64`; `resolve_local_server_component` returns the matching
  `arm64JP{N}` variant (extends `test_greengrass_publish_localserver.py`'s
  matrix).
- `package_compiled_onnx_component`: manifest content (`runtime`,
  `runtime_artifact`, `model_graph`, `dataset`, no `compilable_models`),
  ZIP layout (`manifest.json` root, `<stage_type>/model.onnx`), and error
  propagation.
- Fan-out: completed `onnx` job → exactly the three-target entry set from one
  upload; packaging failure → three failed entries; mixed Neo+ONNX jobs →
  Neo entries unchanged alongside the fan-out.
- BYO default list content; explicit list verbatim.
- `publish_targets_for_arch`: singleton for every non-JP7 arch, the pair for
  `arm64_jp7`, `()` for unknown archs (caller raises today's message).
- `resolve_model_components`: JP7 coverage via either accepted id; resolved
  names from the union; uncovered error naming both accepted ids; singleton
  error text byte-identical to today for non-JP7 archs.
- `deployments.py` verification: ONNX names produce no vLLM gate manifest;
  `local_server_component_arch` untouched by model names (Property 8).

### Property-Based Tests

Backend (Hypothesis), new suite
`edge-cv-portal/backend/tests/test_onnx_jetson_publish_packaging_properties.py`,
one test per Correctness Property with `# Validates: Requirements …`
comments, conftest-profile settings (no hardcoded `max_examples`):

- **Property 1**: over generated model names and stage types — the full
  package'→publish' pipeline yields per-JetPack entries, `runtime: onnx`
  manifests, and correctly stamped recipes with distinct names.
- **Property 3**: over the ONNX id set plus generated unmapped target names —
  totality in both maps, `aarch64` + `arm64JP{N}` correspondence, unmapped
  targets still raise with no create, and the vLLM totality assertions hold
  with the new entries.
- **Property 4**: over generated artifact/manifest shapes — fan-out entry
  sets, manifest fields, layout; over generated explicit/absent BYO lists —
  default exactly the four-target list, explicit verbatim.
- **Property 5**: over generated model names × ONNX targets — name
  composition, `model-` prefix, charset, JetPack-token major, never
  `model-vllm-`, fail-closed above 128 chars with no create.
- **Property 6**: over generated published shapes × architecture selections —
  JP7 resolution via either id, fail-closed retention, non-JP7 singleton
  semantics unchanged.
- **Property 2 (preservation)**: the observed baselines from Preservation
  Checking encoded over generated non-bug inputs.

Frontend (fast-check), new
`edge-cv-portal/frontend/src/pages/deployments/onnxComponentArch.property.test.ts`
(following `archCompatibility.property.test.ts`):

- **Property 7**: over generated safe model names and JetPack majors —
  `inferComponentTargetArchs` returns exactly the singleton arch for
  `model-{safe}-onnx-jetson-xavier-jp{N}`; compatibility verdicts are exact
  (own arch compatible, every other arch and null fail closed); existing-name
  inference unchanged (Property 2 twin).

### Integration Tests

- Backend end-to-end (moto + fake Greengrass): seed a trained record with a
  completed `onnx` export entry and its S3 artifacts → package → publish →
  three DEPLOYABLE components with correct recipes and per-target
  `published_components` → workflow packaging for `arm64_jp7` resolves the
  JP7 component into the workflow's dependencies.
- BYO end-to-end: import-shaped record → default packaging now covers JP7 →
  publish `jetson-xavier-jp7` → workflow packaging for `arm64_jp7` resolves
  it through the EXISTING map entry (no `ARCH_TO_EXTRA_PUBLISH_TARGETS`
  involvement).
- Frontend: `CreateDeployment.archFilter.test.tsx` extended — the JP7 ONNX
  component is offered for an `arm64_jp7` thing and filtered/labeled for
  `arm64_jp6` and for a thing with no recorded architecture.
  *(Correction, task 5.3: for a thing with NO recorded architecture the
  screen fails open for all name-inferred components and shows the
  no-recorded-architecture warning instead — Requirement 2.12 governs
  (recorded architectures only); the test encodes warning + fail-open.)*
- Manual, post-deploy (user action): the on-hardware plan in "Deployment and
  On-Hardware Verification" — yolo_test v7 and rf-detr v7 on `jetson-thor1`,
  plus the JP6 regression check.

### Preservation Gates To Re-run

Backend (each separately, from `edge-cv-portal/backend/tests` with the
conftest):

- `test_greengrass_publish_localserver.py` — the resolver matrix and
  no-bare-arm64 invariants must pass with the new entries (the
  values-iteration tests cover them automatically).
- `test_vllm_multi_arch_publish_exploration.py` and
  `test_vllm_multi_arch_publish_properties.py` — the sibling's map-totality
  and baseline-resolution assertions must pass with the ONNX entries present
  (either-landing-order requirement).
- `test_vllm_packaging_dispatch.py`, `test_vllm_publish_fit_gate.py`,
  `test_vllm_publish_writeback.py` — vLLM branches untouched (3.3, 3.6).
- `test_vision_model_packaging_preservation.py`,
  `test_vision_model_packaging_exploration.py` — Neo packaging shape (3.4).
- `test_workflow_packaging_vision_resolution_exploration.py`,
  `test_workflow_packaging_vllm_resolution_preservation.py`,
  `test_workflow_packaging_multi_variant_exploration.py`,
  `test_workflow_localserver_variant_compat.py` — workflow resolution
  semantics for the untouched architectures (3.7, 3.8).
- `test_onnx_compile_diagnostics_exploration.py` (case 9 MUST keep passing —
  no JP7 Neo target, exactly seven targets),
  `test_onnx_compile_diagnostics_properties.py`,
  `test_onnx_compile_diagnostics_units.py`,
  `test_onnx_compile_diagnostics_integration.py` — the deployed diagnostics
  contracts (3.9, 3.10).
- `test_deployment_vllm_gate.py`, `test_deployment_plugin_gates.py` —
  deployment gates unchanged (Property 8's scope).

Frontend (vitest, single runs from `edge-cv-portal/frontend`):

- `src/pages/deployments/archCompatibility.property.test.ts`
- `src/pages/deployments/vllmSuffixArch.property.test.ts` (if landed by the
  sibling's open waves at execution time)
- `src/pages/CreateDeployment.archFilter.test.tsx`
