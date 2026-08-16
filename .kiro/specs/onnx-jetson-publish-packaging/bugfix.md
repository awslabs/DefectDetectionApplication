# Bugfix Requirements Document

## Introduction

ONNX vision models cannot reach any Jetson device. The ONNX export path works
(`compilation.py`'s `onnx` target runs a `torch.onnx.export` SageMaker training
job and records a completed `compilation_jobs` entry with a
`compiled_model_s3`), and the device side is ready (`OnnxRunner` in
`src/backend/dda_triton/resources_for_copy/inference_runtimes.py`;
`Dockerfile.jp7` enables GPU onnxruntime by default and JP7 has NO DLR support
at all). But the cloud-side chain between them — packaging, Greengrass publish,
workflow packaging, and deployment architecture resolution — cannot deliver an
ONNX component to a JetPack 5, 6, or 7 device. The user's concrete goal is to
run an ONNX vision model on a JetPack 7 (Jetson Thor) device, where ONNX is the
ONLY vision runtime (Neo cannot target CUDA 13 — its ceiling is 11.x, per the
`jetson-xavier-jp6` comment in `compilation.py` — so `COMPILATION_TARGETS`
deliberately has no `jetson-xavier-jp7` Neo target, a documented non-goal of
`.kiro/specs/onnx-compile-error-diagnostics/` that stays a non-goal here).

Four defects compound into this failure:

1. **The `onnx` publish target is unmapped, so publish fails closed (or, on a
   pre-`vllm-multi-arch-publish-conflict` tree, silently mis-stamps amd64).**
   `greengrass_publish.py`'s module-level `TARGET_TO_LOCAL_SERVER` and
   `TARGET_TO_PLATFORM` maps carry the Neo targets (now including
   `jetson-xavier-jp7`) but no entry that covers an ONNX-compiled artifact.
   The `vllm-multi-arch-publish-conflict` spec's fail-closed
   `resolve_target_platform` has LANDED on this tree (its tasks 1–3.9 are
   complete), so a `packaged_components` entry with `target: 'onnx'` now
   raises `PublishError` ("Unsupported compile target 'onnx'") and the target
   is recorded as failed. On a tree where that spec has not landed, the same
   input instead falls through `TARGET_TO_PLATFORM.get(target, 'amd64')` to
   platform `amd64` and an `aws.edgeml.dda.LocalServer.amd64` HARD dependency
   — the same silent-amd64 defect class as that spec's defect 4. Either way,
   the ONNX component never reaches a Jetson.

2. **The compiled-ONNX packaging path produces one arch-less, runtime-less
   entry.** `packaging.py`'s generic Phase 2 loop packages the completed
   `onnx` compilation job like a Neo artifact: one `packaged_components`
   entry with `target: 'onnx'`, and a manifest from `create_dda_manifest`
   that has NO top-level `runtime` field. The device selects its inference
   runner from that field (`lfv_model_template.py`
   `__load_runtime_config`: absent ⇒ `dlr`), so even if the component were
   published and deployed, the device would instantiate the DLR runner
   against an ONNX artifact — and JP7 ships no DLR at all. The BYO-import
   bypass (`package_onnx_component`) already gets both things right for
   imports — per-target entries and a `runtime: onnx` manifest with the
   artifact nested under `<stage_type>/` — but its default target list is
   `['jetson-xavier-jp5', 'jetson-xavier-jp6', 'x86_64-cpu']`, which omits
   `jetson-xavier-jp7`.

3. **The workflow packager can never select an ONNX component for JP7
   vision.** `workflow_packaging.py`'s `ARCH_TO_PUBLISH_TARGET` maps
   `ARCH_ARM64_JP7: 'jetson-xavier-jp7'` with a deliberate comment that no
   `published_components` entry can match this id, so a workflow selecting
   `arm64_jp7` with a vision `model_ref` fails closed with the
   uncovered-architecture error. That was correct when nothing could publish
   for JP7; with per-JetPack ONNX components it must resolve. (Its own
   `ARCH_TO_GG_PLATFORM['arm64_jp7'] = 'aarch64'` is already correct.)

4. **Deployment-side architecture resolution has no pinned behavior for the
   new component names.** The frontend `inferComponentTargetArchs`
   (`archCompatibility.ts`) matches the JetPack token via
   `/(?:jp|jetpack)(4|5|6|7)(?![0-9])/`, so a name ending
   `-onnx-jetson-xavier-jp7` would already resolve to `arm64_jp7` — but
   nothing tests or pins that for ONNX component names, and
   `CreateDeployment.tsx` compatibility filtering has never seen one. The
   backend `deployments.py` gates only `model-vllm-*` and workflow components
   by architecture (vision model components are arch-gated in the frontend),
   so it needs verification, not necessarily change.

**Chosen fix approach (decided, not to be re-litigated):** per-JetPack ONNX
components. One compiled ONNX artifact yields one Greengrass component per
supported JetPack variant, each named with a per-target suffix following the
existing vision/vLLM per-target convention (e.g.
`model-{safe}-onnx-jetson-xavier-jp5/-jp6/-jp7`), each with platform
`aarch64` and a HARD dependency on that JetPack's LocalServer
(`aws.edgeml.dda.LocalServer.arm64JP5/JP6/JP7`). The alternative — a single
arch-agnostic component with a deployment-resolved LocalServer — was
explicitly rejected: Greengrass `ComponentDependencies` is top-level, so a
shared component would place an unsatisfiable HARD dependency on every device
of the other JetPacks (the same reason the vLLM fix went per-JetPack).
Scope note: JP7 is the primary deliverable; JP5/JP6 keep their Neo targets as
the primary vision route and their resolution in `workflow_packaging.py` is
NOT changed by this spec.

**Sibling-spec coordination** (encoded in the Unchanged Behavior section):
`.kiro/specs/vllm-multi-arch-publish-conflict/` (landed code waves; its
property/test waves 4–8 are still open) established the conventions this spec
reuses — Target_Suffix transform, per-component `supported_architectures`,
fail-closed `resolve_target_platform`, Greengrass name validation — and its
map-totality property tests must keep passing with this spec's new entries in
either landing order. `.kiro/specs/onnx-compile-error-diagnostics/` (not yet
implemented) fixes the opaque ONNX export failure the user currently hits; it
is a practical prerequisite for exercising this spec end-to-end but shares no
code contract beyond the ones preserved in 3.10. `.kiro/specs/jetpack7-support/`
and `docs/multi-runtime-inference.md` gain amendments recording ONNX as the
delivered JP7 vision route.

## Bug Analysis

### Current Behavior (Defect)

**Defect 1 — the `onnx` publish target is unmapped in `greengrass_publish.py`**

1.1 WHEN a model with a packaged ONNX artifact (a `packaged_components` entry
whose `target` is `'onnx'`) is published on the current tree (with
`vllm-multi-arch-publish-conflict` waves 1–3 landed) THEN the system's
`resolve_target_platform('onnx')` raises `PublishError` because `'onnx'` is a
key of neither `TARGET_TO_LOCAL_SERVER` nor `TARGET_TO_PLATFORM`, the target is
recorded as failed, and no component version is created — the ONNX model cannot
be published for any device.

1.2 WHEN the same publish runs on a tree where `vllm-multi-arch-publish-conflict`
has NOT landed THEN the system falls through
`TARGET_TO_PLATFORM.get('onnx', 'amd64')` to platform `amd64`,
`resolve_local_server_component('onnx', 'amd64')` returns
`aws.edgeml.dda.LocalServer.amd64` via its amd64 branch, and the ONNX component
is silently published with an amd64 platform manifest and an amd64 LocalServer
HARD dependency — undeployable to any Jetson (JP5/JP6/JP7) and mis-advertised
to every amd64 device.

1.3 WHEN an ONNX publish is attempted for a Jetson-bound model under either
ordering THEN the system delivers no component that an `arm64_jp5`,
`arm64_jp6`, or `arm64_jp7` device can deploy.

**Defect 2 — compiled-ONNX packaging emits one arch-less, runtime-less entry**

1.4 WHEN `packaging.py` packages a training job whose completed
`compilation_jobs` entry has `target: 'onnx'` (the `torch.onnx.export` path for
a trained model) THEN the system routes it through the generic Neo Phase 2 loop
and writes exactly ONE `packaged_components` entry with `target: 'onnx'`,
rather than per-JetPack entries publish could map to platforms and LocalServer
variants.

1.5 WHEN that Phase 2 loop assembles the component ZIP THEN the system writes
the `create_dda_manifest` output as `manifest.json` — a manifest with
`model_graph`, `compilable_models` (framework `PYTORCH`), and `dataset` but NO
top-level `runtime` field — so the on-device `lfv_model_template.py`
`__load_runtime_config` defaults the runner to `dlr` and the device would
instantiate the DLR runner against an ONNX artifact; on JP7 no DLR exists at
all.

1.6 WHEN `packaging.py` packages an imported BYO ONNX model (the
`is_onnx_import` bypass, which does produce a correct `runtime: onnx` manifest
with the artifact nested under `<stage_type>/`) THEN the system defaults
`onnx_targets` to `['jetson-xavier-jp5', 'jetson-xavier-jp6', 'x86_64-cpu']`,
omitting `jetson-xavier-jp7`, so even the working import path never packages
for JP7.

**Defect 3 — workflow packaging fails closed for `arm64_jp7` vision refs**

1.7 WHEN a workflow whose `model_graph` contains a vision `model_ref` is
packaged for the `arm64_jp7` architecture THEN the system maps `arm64_jp7` to
publish target `jetson-xavier-jp7` via `ARCH_TO_PUBLISH_TARGET`, finds no
`published_components` entry with that target (nothing can publish one — see
defects 1 and 2), and raises the uncovered-architecture `PackagingError`
("no published Greengrass component for the selected architecture(s)
arm64_jp7"), exactly as the map's comment says it deliberately must — so no
vision workflow can ever be packaged for a JP7 device.

**Defect 4 — deployment arch resolution is unpinned for ONNX component names**

1.8 WHEN the frontend `inferComponentTargetArchs` (`archCompatibility.ts`)
receives a per-JetPack ONNX component name (e.g.
`model-{safe}-onnx-jetson-xavier-jp7`) THEN the system happens to resolve
`arm64_jp7` via the generic JetPack-token regex, but no test pins this for
ONNX names, and `CreateDeployment.tsx` compatibility filtering has no
verified behavior for them — the resolution is untested, not designed.

1.9 WHEN the backend `deployments.py` encounters a per-JetPack ONNX component
name THEN the system applies no architecture gate to it (vision model
components are only arch-gated client-side); this is unverified for the new
names rather than known-correct.

### Expected Behavior (Correct)

**Fix 1 — per-JetPack ONNX publish targets, mapped and fail-closed-compatible**

2.1 WHEN a model with packaged ONNX components is published THEN the system
SHALL publish one Greengrass component per supported JetPack variant, each
named with the existing per-target suffix convention
(`f"{component_name}-{target_suffix}"`, suffix derived from the per-JetPack
ONNX packaging target id), so that the JP5, JP6, and JP7 ONNX components are
distinct component identities (e.g. `model-{safe}-onnx-jetson-xavier-jp7`, with
the exact target-id vocabulary fixed in design and kept consistent with
`vllm-multi-arch-publish-conflict`'s Target_Suffix transform).

2.2 WHEN each per-JetPack ONNX component's recipe is generated THEN the system
SHALL stamp its manifest platform `aarch64` (`{os: linux, architecture:
aarch64}`) and a HARD `ComponentDependencies` entry on exactly that JetPack's
LocalServer variant (`aws.edgeml.dda.LocalServer.arm64JP5` / `.arm64JP6` /
`.arm64JP7`), resolved through the mapped entries — never through any amd64
default.

2.3 WHEN the per-JetPack ONNX packaging targets are added to
`greengrass_publish.py` THEN every such target SHALL be a key of BOTH
`TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM`, so
`resolve_target_platform` resolves them without raising and the
`vllm-multi-arch-publish-conflict` map-totality discipline (every producible
target mapped in both maps, unmapped targets fail closed) holds with the new
entries present in either spec-landing order.

2.4 WHEN a per-JetPack ONNX component name is derived THEN it SHALL satisfy the
existing name gates: `validate_greengrass_component_name` (length ≤ 128,
charset `^[a-zA-Z0-9._-]+$`), the backend `model-` prefix convention, and a
JetPack token matching the frontend inference regex
`/(?:jp|jetpack)(4|5|6|7)(?![0-9])/` whose major equals the component's
JetPack.

2.5 WHEN the publish records its results THEN each per-JetPack ONNX component
SHALL get its own `published_components` entry carrying its own
`component_name`, its per-JetPack `target` id, and `status: 'published'`, in
the same per-target shape vision publishes already write.

**Fix 2 — packaging yields per-JetPack entries with a `runtime: onnx` manifest**

2.6 WHEN `packaging.py` packages a training job whose completed compilation
entry has `target: 'onnx'` THEN the system SHALL produce, from that ONE
exported artifact, one `packaged_components` entry per supported JetPack
variant (each with its per-JetPack target id and a component package S3 URI),
instead of the single `target: 'onnx'` entry.

2.7 WHEN the compiled-ONNX component payload is assembled THEN its
`manifest.json` SHALL carry top-level `runtime: 'onnx'` (and the
`runtime_artifact` naming the `.onnx` file when the design requires it), with
the ONNX artifact placed where the on-device `OnnxRunner` resolves it
(`<version_dir>/<stage_type>/<artifact>` per `lfv_model_template.py` and
`model_convertor.py` expectations), so the device selects `OnnxRunner`, not the
DLR default.

2.8 WHEN `packaging.py` packages an imported BYO ONNX model THEN the
`onnx_targets` default SHALL include the JetPack 7 target alongside the
existing JP5/JP6/x86 targets, so imports can also reach JP7 (and each target in
that list SHALL be publishable per Fix 1).

**Fix 3 — workflow packaging resolves `arm64_jp7` vision refs to the ONNX
component**

2.9 WHEN a workflow with a vision `model_ref` is packaged for `arm64_jp7` and
the referenced model has a published per-JetPack ONNX component for JP7 THEN
the system SHALL resolve `arm64_jp7` to that component (matching its
`published_components` entry by the JP7 ONNX target id through
`ARCH_TO_PUBLISH_TARGET` or its designed successor) instead of raising the
uncovered-architecture error, and the map's "no entry can match this id"
comment SHALL be updated to reflect that ONNX now covers JP7.

2.10 WHEN a workflow is packaged for `arm64_jp7` with a vision `model_ref`
whose model has NO published JP7 ONNX component THEN the system SHALL CONTINUE
TO fail closed with the accurate uncovered-architecture `PackagingError` naming
the model and the uncovered architecture.

**Fix 4 — deployment arch resolution pinned for the new names**

2.11 WHEN the frontend `inferComponentTargetArchs` receives a per-JetPack ONNX
component name THEN it SHALL resolve exactly the singleton architecture set of
that name's JetPack token (`...-onnx-jetson-xavier-jp7` → `{arm64_jp7}`), and
this SHALL be pinned by tests in `archCompatibility.property.test.ts` (or a
sibling suite) whether or not production code needs changing.

2.12 WHEN `CreateDeployment.tsx` compatibility filtering evaluates a
per-JetPack ONNX component THEN it SHALL show the JP7 ONNX component as
deployable to an `arm64_jp7` thing and as incompatible with things of any other
recorded architecture.

2.13 WHEN the backend `deployments.py` processes a deployment containing a
per-JetPack ONNX component THEN its behavior SHALL be verified for the new
names (and resolution added ONLY if verification shows a gap): vision model
components stay outside the vLLM arch gate, and LocalServer minimum-version
gating keys off the LocalServer name, not the model component name.

**Fix 5 — documentation amendments**

2.14 WHEN this fix lands THEN `.kiro/specs/jetpack7-support/` and
`docs/multi-runtime-inference.md` SHALL be amended to record that per-JetPack
ONNX components are the delivered vision route for JP7, and a sibling-spec
amendment note SHALL be added for `vllm-multi-arch-publish-conflict` recording
that the ONNX targets joined its target maps under its totality discipline.

### Unchanged Behavior (Regression Prevention)

**Neo vision publish — byte-for-byte**

3.1 WHEN any of the currently mapped targets (`jetson-xavier`, `jetson-xavier-jp5`,
`jetson-xavier-jp6`, `jetson-xavier-jp7`, `arm64-cpu`, `x86_64-cpu`,
`x86_64-cuda` — the mapped vocabulary as it exists on the current tree) is
published THEN the system SHALL CONTINUE TO produce exactly today's component
names, platforms, LocalServer dependencies, and recipes for those targets —
byte-for-byte unchanged.

3.2 WHEN a genuinely unknown packaging target is published THEN
`resolve_target_platform` SHALL CONTINUE TO fail closed with `PublishError`,
recording a failed target with no component version created — the new ONNX
entries narrow the unmapped set but never reintroduce a default.

**vLLM publish path untouched**

3.3 WHEN a vLLM model record is published THEN the system SHALL CONTINUE TO
follow the `vllm-multi-arch-publish-conflict` behavior exactly (per-JetPack
vLLM naming, cloud-side version derivation, atomicity gate, write-back shape) —
this spec touches no vLLM branch.

**Compiled Neo packaging and BYO-import packaging**

3.4 WHEN `packaging.py` packages completed Neo compilation jobs THEN the
generic Phase 2 loop SHALL CONTINUE TO package each Neo target exactly as
today (same layout, same `create_dda_manifest` manifest with no `runtime`
field — the DLR default is the correct runtime for Neo/DLR artifacts).

3.5 WHEN `packaging.py` packages an imported BYO ONNX model THEN
`package_onnx_component` SHALL CONTINUE TO produce the same ZIP layout
(`manifest.json` at the root with `runtime: 'onnx'` and the merged `dataset`
block, artifact nested under `<stage_type>/`), and explicit caller-requested
target lists SHALL CONTINUE TO be honored verbatim.

3.6 WHEN a vLLM record is packaged THEN the vLLM packaging branch SHALL
CONTINUE TO behave exactly as today (this spec's packaging changes are confined
to the ONNX paths).

**Workflow packaging for non-JP7 architectures**

3.7 WHEN a workflow with a vision `model_ref` is packaged for `arm64_jp4`,
`arm64_jp5`, `arm64_jp6`, `x86_64`, or `x86_64_nvidia` THEN the system SHALL
CONTINUE TO resolve those architectures through today's
`ARCH_TO_PUBLISH_TARGET` entries against the Neo-published targets, with the
same coverage semantics and the same uncovered-architecture error on a miss —
JP5/JP6 vision resolution is NOT changed to prefer ONNX by this spec.

3.8 WHEN `workflow_packaging.py` resolves LocalServer variants and Greengrass
platforms THEN `ARCH_TO_LOCAL_SERVER_COMPONENT` and `ARCH_TO_GG_PLATFORM`
SHALL CONTINUE TO resolve exactly as today for every architecture.

**Compilation targets and export path**

3.9 WHEN `COMPILATION_TARGETS` is consulted THEN it SHALL CONTINUE TO contain
no `jetson-xavier-jp7` Neo compile target (Neo's CUDA ceiling is 11.x; JP7 is
CUDA 13 — the documented non-goal of `onnx-compile-error-diagnostics` stays a
non-goal), and the `onnx` export target definition and
`_start_onnx_export_job` SHALL CONTINUE TO behave exactly as today.

3.10 WHEN the `onnx-compile-error-diagnostics` spec's contracts are exercised
THEN they SHALL CONTINUE TO hold untouched: write-once error fields on
compilation job entries, `classify_poll_kind` (or its specified dispatch
seam), and no fabrication of a `compilation_job_name` for a job that was never
created.

**Device-side runtime selection**

3.11 WHEN a deployed model component's manifest has no `runtime` field or
`runtime: 'dlr'` THEN the device SHALL CONTINUE TO select the DLR runner; WHEN
it has `runtime: 'onnx'` THEN the device SHALL CONTINUE TO select `OnnxRunner`
with its existing TensorRT → CUDA → CPU provider preference — no device-side
code changes in this spec.

**Frontend import and compile UI**

3.12 WHEN the compile-targets list is rendered THEN `CompilationTab.tsx`'s
`onnx` target entry and the SmartImport ONNX flow SHALL CONTINUE TO be offered
exactly as today (copy updates describing JP7 support are allowed; no target
additions or removals).

3.13 WHEN `inferComponentTargetArchs` receives any existing (non-ONNX)
component name THEN it SHALL CONTINUE TO resolve exactly today's architecture
set (LocalServer variants, vLLM per-JetPack names, Neo vision names, and
names with no JetPack token yielding the empty set).

**IAM and security baseline**

3.14 WHEN the portal infrastructure is synthesized THEN the Lambda IAM policies
SHALL CONTINUE TO grant exactly today's actions — this fix prefers NO new
Greengrass/SageMaker action; if design uncovers an unavoidable new action, it
SHALL be flagged explicitly and the security baselines under
`test/backend-test/security/baselines/iam_baseline_*.json` SHALL be
re-recorded only through that gate's documented, reviewed protocol — never
silently.

### Bug Conditions and Properties

**Key definitions.** `F` is the current (unfixed) code; `F'` is the fixed code.
`onnxTargets` is the per-JetPack ONNX packaging-target vocabulary this spec
introduces (fixed in design); `jetpackOf(t)` maps such a target to its
JetPack architecture id (`arm64_jp5` | `arm64_jp6` | `arm64_jp7`);
`localServerVariant(a)` and `platformFor(a)` are as in
`vllm-multi-arch-publish-conflict` (`platformFor(arm64_jp5/6/7) = 'aarch64'`).

#### Defect 1 — `onnx` publish target unmapped

```pascal
FUNCTION isBugCondition_1(X)
  INPUT: X of type PublishRequest
  OUTPUT: boolean

  // A publish whose packaged targets include the ONNX artifact's target,
  // which is a key of neither module-level map in greengrass_publish.py.
  RETURN EXISTS c IN X.record.packaged_components
           WHERE c.status = 'packaged'
             AND isOnnxTarget(c.target)
             AND (c.target NOT IN keys(TARGET_TO_LOCAL_SERVER)
                  OR c.target NOT IN keys(TARGET_TO_PLATFORM))
END FUNCTION
```

```pascal
// Property: Fix Checking - every per-JetPack ONNX target is mapped and its
// recipe is stamped for its own JetPack
FOR ALL X WHERE isBugCondition_1(X) DO
  FOR ALL t IN onnxTargets(X) DO
    ASSERT t IN keys(TARGET_TO_LOCAL_SERVER')
    ASSERT t IN keys(TARGET_TO_PLATFORM')
    recipe ← recipeFor(publish'(X), t)
    ASSERT recipe.Manifests[0].Platform.architecture = platformFor(jetpackOf(t))
    ASSERT recipe.ComponentDependencies CONTAINS localServerVariant(jetpackOf(t))
    ASSERT dependencyType(recipe.ComponentDependencies,
                          localServerVariant(jetpackOf(t))) = HARD
    ASSERT recipe.ComponentDependencies CONTAINS NO amd64 LocalServer
    name ← componentNameFor(publish'(X), t)
    ASSERT isValidGreengrassComponentName(name)
    ASSERT jetpackTokenMajor(name) = major(jetpackOf(t))
  END FOR
  // Composition with the sibling fix, either landing order:
  ASSERT resolve_target_platform'(t) RAISES nothing FOR ALL t IN onnxTargets(X)
  ASSERT resolve_target_platform'(u) RAISES PublishError
           FOR ALL u NOT IN keys(TARGET_TO_PLATFORM')
END FOR
```

#### Defect 2 — compiled-ONNX packaging arch-less and runtime-less

```pascal
FUNCTION isBugCondition_2(X)
  INPUT: X of type PackagingRequest
  OUTPUT: boolean

  // A trained model with a completed torch.onnx.export compilation entry
  // (the generic Phase 2 loop mishandles it), or a BYO ONNX import whose
  // defaulted target list omits JP7.
  RETURN (EXISTS j IN X.record.compilation_jobs
            WHERE j.target = 'onnx' AND upper(j.status) = 'COMPLETED')
      OR (is_onnx_import(X.record)
          AND X.requested_targets = NULL)   // defaulted list omits JP7
END FUNCTION
```

```pascal
// Property: Fix Checking - one artifact, per-JetPack entries, onnx manifest
FOR ALL X WHERE isBugCondition_2(X) DO
  entries ← packagedComponents(package'(X))
  ASSERT { e.target FOR e IN entries } ⊇ requiredOnnxTargets(X)  // JP7 included
  ASSERT NO e IN entries WHERE e.target = 'onnx'                 // arch-less id gone
  FOR ALL e IN entries DO
    manifest ← manifestOf(e.component_package_s3)
    ASSERT manifest.runtime = 'onnx'
    ASSERT artifactPath(e) = '<stage_type>/' + onnxArtifactName(e)
  END FOR
END FOR
```

#### Defect 3 — workflow packaging fails closed for arm64_jp7 vision refs

```pascal
FUNCTION isBugCondition_3(X)
  INPUT: X of type WorkflowPackagingRequest
  OUTPUT: boolean

  RETURN 'arm64_jp7' IN X.selected_architectures
     AND EXISTS ref IN visionModelRefs(X.workflow)
           WHERE hasPublishedJp7OnnxComponent'(ref)   // publishable post-fix
END FUNCTION
```

```pascal
// Property: Fix Checking - arm64_jp7 resolves to the JP7 ONNX component
FOR ALL X WHERE isBugCondition_3(X) DO
  resolved ← resolveModelComponents'(X)
  FOR ALL ref IN visionModelRefs(X.workflow) DO
    ASSERT jp7OnnxComponentName(ref) IN resolved[ref]
  END FOR
  ASSERT package'(X) RAISES NO uncovered-architecture PackagingError
END FOR

// Fail-closed retained when no JP7 ONNX component exists
FOR ALL X WHERE 'arm64_jp7' IN X.selected_architectures
            AND EXISTS ref WHERE NOT hasPublishedJp7OnnxComponent'(ref) DO
  ASSERT package'(X) RAISES PackagingError
           NAMING ref AND 'arm64_jp7'
END FOR
```

#### Defect 4 — deployment arch resolution unpinned for ONNX names

```pascal
FUNCTION isBugCondition_4(X)
  INPUT: X of type ComponentName
  OUTPUT: boolean

  RETURN isPerJetpackOnnxComponentName(X)
END FUNCTION
```

```pascal
// Property: Fix Checking - exact singleton arch inference, exact gate
FOR ALL X WHERE isBugCondition_4(X) DO
  ASSERT inferComponentTargetArchs'(X) = { jetpackArchOf(X) }
  FOR ALL deviceArch IN allArchitectures DO
    ASSERT compatible'(X, deviceArch) IFF deviceArch = jetpackArchOf(X)
  END FOR
  ASSERT compatible'(X, NULL) = false        // fail closed, unchanged rule
END FOR
```

#### Preservation

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT (isBugCondition_1(X) OR isBugCondition_2(X)
                     OR isBugCondition_3(X) OR isBugCondition_4(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

Concretely, preservation covers:

```pascal
// Neo vision publish byte-for-byte (3.1, 3.2)
FOR ALL X WHERE targetsOf(X) ⊆ currentMappedTargets DO
  ASSERT publish(X) = publish'(X)
END FOR
FOR ALL t IN currentMappedTargets DO
  ASSERT TARGET_TO_LOCAL_SERVER'(t) = TARGET_TO_LOCAL_SERVER(t)
  ASSERT TARGET_TO_PLATFORM'(t)     = TARGET_TO_PLATFORM(t)
END FOR
FOR ALL u WHERE u NOT IN keys(TARGET_TO_PLATFORM') DO
  ASSERT resolve_target_platform'(u) RAISES PublishError
END FOR

// vLLM publish untouched (3.3)
FOR ALL X WHERE is_vllm_record(X.record) DO
  ASSERT publish(X) = publish'(X)
END FOR

// Neo Phase 2 packaging and BYO import layout unchanged (3.4, 3.5)
FOR ALL X WHERE isNeoCompiledJob(X) DO
  ASSERT package(X) = package'(X)
END FOR
FOR ALL X WHERE is_onnx_import(X.record) AND X.requested_targets ≠ NULL DO
  ASSERT targetsOf(package'(X)) = X.requested_targets
  ASSERT zipLayout(package'(X)) = zipLayout(package(X))
END FOR

// Non-JP7 workflow vision resolution unchanged (3.7, 3.8)
FOR ALL X WHERE X.selected_architectures ∩ {'arm64_jp7'} = ∅ DO
  ASSERT resolveModelComponents(X) = resolveModelComponents'(X)
END FOR

// No JP7 Neo compile target; export path unchanged (3.9)
ASSERT 'jetson-xavier-jp7' NOT IN keys(COMPILATION_TARGETS')
ASSERT COMPILATION_TARGETS'('onnx') = COMPILATION_TARGETS('onnx')

// Device runtime selection unchanged (3.11)
FOR ALL m WHERE m.runtime IN {ABSENT, 'dlr'} DO
  ASSERT runnerFor'(m) = DlrRunner
END FOR
FOR ALL m WHERE m.runtime = 'onnx' DO
  ASSERT runnerFor'(m) = OnnxRunner
END FOR

// Existing-name arch inference unchanged (3.13)
FOR ALL name WHERE NOT isPerJetpackOnnxComponentName(name) DO
  ASSERT inferComponentTargetArchs'(name) = inferComponentTargetArchs(name)
END FOR

// IAM policy unchanged (3.14)
ASSERT iamPolicy'(portalLambdaRoles) = iamPolicy(portalLambdaRoles)
```
