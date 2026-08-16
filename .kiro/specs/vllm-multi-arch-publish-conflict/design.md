# vLLM Multi-Arch Publish Conflict Bugfix Design

## Overview

Every vLLM publish fails with `ConflictException` because `publish_component`
loops over N packaged targets while holding ONE component name and ONE
component version for vLLM records. With `vllm_supported_architectures()` now
returning `['arm64_jp6', 'arm64_jp7']`, the second `create_component_version`
call always collides with the first. The atomicity rollback that should erase
the first target's component version is denied (`greengrass:DeleteComponent` is
not granted to the portal Lambda role), so an orphan survives, and
`next_vllm_component_version()` — which reads only the record's own (still
empty) publish history — recomputes `1.0.0` forever, wedging retries.

The fix has six moving parts, each reusing machinery that already exists in
this codebase rather than inventing new mechanisms:

1. **Per-JetPack component names.** vLLM records join the vision convention
   `f"{component_name}-{target_suffix}"`, so each `create_component_version`
   call carries a distinct identity. Each per-JetPack component advertises
   exactly one `Target_Architecture` and depends HARD on exactly that
   JetPack's `LocalServer` variant, making the architecture gate exact per
   component instead of record-wide.
2. **Authorized rollback.** `greengrass:DeleteComponent`, scoped to the
   Greengrass components resource ARN, is added to the portal Lambda policy —
   the same grant the cross-account `DDAPortalUseCaseAccountStack` role already
   carries. Rollback stays best-effort and non-throwing.
3. **Cloud-side version derivation.** `next_vllm_component_version()` stops
   reading record history and mirrors `workflow_packaging.py`'s
   `_existing_component_versions()` / `next_component_version()` pattern:
   major-only `N.0.0`, strictly above every version that actually exists in
   Greengrass for the component names being published.
4. **Suffix-aware resolution** on both sides of the deploy screen. The backend
   resolves a suffixed name back to its record (the GSI still holds the ONE
   unsuffixed base name) and resolves each component to its OWN architecture;
   the frontend does the same over the model detail API's `published_component`.
5. **Operational recovery.** The naming change alone makes the existing orphan
   non-blocking (the new names are different components), so deleting
   `model-vllm-qwen3-vl-8b-instruct:1.0.0` is optional hygiene requiring
   explicit user confirmation, not a prerequisite.
6. **JP7 target mappings, and no more silent amd64 defaulting.**
   `greengrass_publish.py`'s `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM`
   gain their missing `jetson-xavier-jp7` entries
   (`…LocalServer.arm64JP7` / `aarch64`), and the platform is resolved through
   an explicit lookup that fails closed instead of
   `TARGET_TO_PLATFORM.get(target, 'amd64')`, so no future target can be
   stamped amd64 by omission (2.17, 2.18, 2.19).

Part 6 is a **latent defect surfaced during design**, independent of part 1 and
one that would **survive a naming-only fix**. `jp7-vllm-enablement` task 6.2
updated `vllm_supported_architectures()` and `packaging.VLLM_ARCH_TO_TARGET` but
not these two maps, so today the JP7 target falls through to `platform='amd64'`
and `resolve_local_server_component` returns the **amd64** LocalServer: a JP7
component publishes cleanly and is then stamped with an aarch64-incompatible
manifest and the wrong HARD LocalServer dependency on an aarch64 Thor device.
Requirement 2.4 cannot be met without these entries, and 2.19 additionally
requires that the amd64 default itself be removed rather than merely
side-stepped for JP7.

## Glossary

- **Bug_Condition (C)**: The condition that triggers a defect. Four
  compounding conditions here: a vLLM record with more than one supported
  architecture (C1), a failed vLLM attempt that already created ≥1 component
  version (C2), a cloud-side version the record's history cannot see (C3), and a
  packaging target `packaging.VLLM_ARCH_TO_TARGET` can produce that is absent
  from `TARGET_TO_LOCAL_SERVER` or `TARGET_TO_PLATFORM` (C4).
  Formalized in `bugfix.md` as `isBugCondition_1/2/3/4`.
- **Property (P)**: The desired behavior under C — one `create_component_version`
  per distinct identity, an authorized rollback, and a derived version that
  dominates everything existing cloud-side.
- **Preservation**: Vision (non-vLLM) publish, legacy unsuffixed vLLM component
  resolution, the Triton device-side model identity, the fit-check/atomicity
  gates, and every other Greengrass IAM statement — all unchanged.
- **Base_Component_Name**: `derive_vllm_component_name(model_name)` =
  `model-vllm-{safe_model_name}`. Unchanged by this fix; it remains the record's
  top-level `component_name` GSI key and the display name.
- **Target_Suffix**: `target.replace('_', '-')` for a packaging target from
  `packaging.VLLM_ARCH_TO_TARGET` — the closed vocabulary
  `jetson-xavier-jp5` / `jetson-xavier-jp6` / `jetson-xavier-jp7`. The same
  transform vision components already use.
- **Per_JetPack_Component**: `f"{Base_Component_Name}-{Target_Suffix}"`, e.g.
  `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp7`.
- **publish_component**: The vLLM/vision publish handler in
  `edge-cv-portal/backend/functions/greengrass_publish.py`.
- **published_component**: The vLLM publish write-back map on the training-jobs
  record. Gains a `components` list (one entry per Per_JetPack_Component) while
  keeping its existing keys for legacy readers.
- **Arch_Gate**: `evaluate_vllm_arch_gate` in `deployments.py` and its pure TS
  twin `evaluateVllmArchGate` in `vllmArchGate.ts`. Unchanged by this fix —
  only the *supported set* fed into it becomes per-component.

## Bug Details

### Bug Condition

The bug manifests whenever a vLLM record is published while more than one
architecture is supported (defect 1), whenever such a failed attempt tries to
roll back the versions it created (defect 2), and whenever a retry derives its
version from record history that cannot see the surviving orphan (defect 3).
`publish_component` holds one `(component_name, component_version)` pair for the
whole target loop, `greengrass.delete_component` is unauthorized on the portal
Lambda role, and `next_vllm_component_version` never queries Greengrass.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type PublishAttempt
  OUTPUT: boolean

  // C1 — duplicate component identity
  c1 := is_vllm_record(input.record)
        AND |vllm_supported_architectures()| > 1

  // C2 — rollback denied, orphan survives
  c2 := is_vllm_record(input.record)
        AND |input.createdArns| > 0
        AND anyTargetFailed(input)
        AND NOT iamPolicy(portalLambdaRole) ALLOWS greengrass:DeleteComponent

  // C3 — version derivation blind to cloud-side state
  c3 := is_vllm_record(input.record)
        AND EXISTS v IN existingCloudVersions(componentNameOf(input))
              WHERE major(v) >= historyDerivedMajor(input.record)

  // C4 — a producible packaging target unmapped in either module-level map,
  //       so its platform silently defaults to amd64
  c4 := EXISTS t IN targetsOf(input)
          WHERE t IN values(packaging.VLLM_ARCH_TO_TARGET)
            AND (t NOT IN keys(TARGET_TO_LOCAL_SERVER)
                 OR t NOT IN keys(TARGET_TO_PLATFORM))

  RETURN c1 OR c2 OR c3 OR c4
END FUNCTION
```

### Examples

- Publish `Qwen3-VL-8B-Instruct` (targets `jetson-xavier-jp6`,
  `jetson-xavier-jp7`). Expected: two DEPLOYABLE components, one per JetPack.
  Actual: `model-vllm-qwen3-vl-8b-instruct:1.0.0` created for JP6, then the
  identical name:version requested for JP7 → `ConflictException`, HTTP 502,
  `failed_step: greengrass_registration`.
- The same attempt's rollback: `delete_component(arn=…:1.0.0)` →
  AccessDenied, caught and logged as a warning. Expected: the version is
  removed. Actual: it survives with `dda-portal:managed=true` tags and no
  backing publish state.
- Retry the same record. Expected: a fresh publish succeeds. Actual:
  `next_vllm_component_version()` reads null history → `1.0.0` → conflicts with
  the orphan on the *first* target now.
- JP7 recipe generation (latent): `TARGET_TO_PLATFORM` has no
  `jetson-xavier-jp7`, so `platform` defaults to `amd64` and
  `resolve_local_server_component('jetson-xavier-jp7', 'amd64')` returns
  `aws.edgeml.dda.LocalServer.amd64`. Expected: platform `aarch64` and
  `aws.edgeml.dda.LocalServer.arm64JP7`.
- Deploy screen for thing `jetson-thor1` (`arm64_jp7`). Expected: the record's
  JP7 component selectable. Actual: `published_component` is null → supported
  set `[]` → fails closed → the model is hidden from every device.
- Edge case (expected behavior, not a bug): a model whose sanitized name is long
  enough that `base + '-' + suffix` exceeds the Greengrass 128-character
  component-name limit must fail closed with a `PublishError` for that target,
  never a create attempt.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Vision (non-vLLM) publish: `f"{component_name}-{target_suffix}"` naming, the
  caller-supplied version, a write-back of only `published_components` and
  `updated_at`, no atomicity gate, and fail-closed `PublishError` per target
  when a `LocalServer` variant cannot be resolved (3.1, 3.2).
- Device-side runtime identity: `vllm_model_prep.py` still receives
  `--model_name = _safe_model_name(model_name)`. `--component_name` is
  logging-only (its argparse help says "(logging)"; `prepare()` binds it to
  `component` and only logs it), so suffixing the component name has zero
  device-side effect (3.3).
- `derive_vllm_component_name(model_name)` keeps returning
  `model-vllm-{safe_model_name}` verbatim — the base name is what gets
  suffixed, and the transform-equality baseline in
  `test_property_llm_model_name_preservation.py` stays true.
- Legacy unsuffixed vLLM components keep resolving to their record and to the
  record-wide `supported_architectures`; the gate does not start failing closed
  on them, and existing deployments stay revisable (3.4, 3.5).
- Arch gate semantics: fail closed on a null device architecture and on an
  empty supported set, `arm64_jp4` → `JP4_UNSUPPORTED` with the JetPack-4
  message, exact-name matching with no fallback, one entry per
  (component, device) miss in the 409 `VLLM_ARCH_UNSUPPORTED` details,
  LLM-bearing workflow components still gated on `packaged_architectures`, and
  zero findings when no vLLM-bearing component is present (3.6–3.11).
- vLLM publish gates: preflight fit check (422 / `skip_fit_check` /
  `unverified`) before any component registration, all-or-nothing atomicity
  with no publish state written and a retryable 502 plus failure audit event,
  and the Models-table record + `published = True` + success audit event on
  success (3.12–3.14).
- Every other Greengrass IAM statement and resource scope in
  `compute-stack.ts`; no wildcard resource for the new action (3.15).

**Scope:**
All inputs that are not vLLM publishes or `model-vllm-*` resolutions are
completely unaffected:
- vision, ONNX-import, and workflow/plugin publish and packaging paths
- `dda.plugin.*` and `dda.workflow.*` deployment gating
- device-side model preparation, staging, and Triton model identity
- every non-Greengrass IAM statement, and every Greengrass action other than
  the added `DeleteComponent`

## Hypothesized Root Cause

Confirmed by reading the code — each defect has a single, located cause:

1. **One identity for N targets** (`greengrass_publish.py`, target loop):
   ```python
   target_suffix = target.replace('_', '-')
   if vllm_record:
       target_component_name = component_name      # ← fixed, no suffix
   else:
       target_component_name = f"{component_name}-{target_suffix}"
   ```
   plus a single `component_version` derived once before the loop. The comment
   above it explains the intent (the name was the deployment gate's
   discriminator and the GSI key) — an assumption that held only while vLLM had
   exactly one target. `vllm_archs` (the record-wide set) is also passed to
   every recipe, so a per-JetPack component would advertise architectures its
   HARD `LocalServer` dependency cannot satisfy.

2. **Missing IAM grant** (`compute-stack.ts`, the combined per-service
   statement): the Greengrass action list contains `CreateComponentVersion`,
   `DescribeComponent`, `GetComponent`, `ListComponents`,
   `ListComponentVersions`, … but not `DeleteComponent`. The cross-account
   `DDAPortalUseCaseAccountStack` role DOES grant it, which is why this only
   bites single-account setups — exactly the affected account (164152369890).

3. **History-only version derivation** (`next_vllm_component_version`): reads
   `published_components` / `published_component` from the record. The
   atomicity gate deliberately writes neither on failure, so the history is
   permanently null and the function is a constant `1.0.0`. Its docstring
   claims it accounts for "versions from failed attempts that may still exist
   cloud-side" — it cannot, because it never calls Greengrass.

4. **Missing JP7 target mappings** (`greengrass_publish.py`
   `TARGET_TO_LOCAL_SERVER` / `TARGET_TO_PLATFORM`): `jetson-xavier-jp7` is
   absent, so `TARGET_TO_PLATFORM.get(target, 'amd64')` yields `amd64` and the
   fail-closed resolver's amd64 branch hands back the amd64 LocalServer instead
   of raising. The fail-closed guarantee of `localserver-arch-naming` is
   silently bypassed for JP7 by the platform default.

**Why one shared component cannot work.** `generate_vllm_component_recipe`
places a HARD dependency on `resolve_local_server_component(target, platform)`
(`…LocalServer.arm64JP6` vs `…LocalServer.arm64JP7`). Greengrass
`ComponentDependencies` is a **top-level** recipe key, not per-manifest, so one
component version cannot carry a different dependency per platform manifest.
And both targets are platform `linux/aarch64`, so manifests cannot discriminate
them either. A shared component would place an unsatisfiable HARD dependency on
every device of the other JetPack. Per-JetPack components are the only shape
that keeps the dependency correct.

## Correctness Properties

Property 1: Bug Condition — One create per distinct per-JetPack identity

_For any_ vLLM publish where the bug condition holds (`isBugCondition_1`: a vLLM
record with more than one supported architecture), the fixed `publish_component`
SHALL issue exactly one `create_component_version` call per supported
architecture, all `(component_name, component_version)` pairs SHALL be distinct,
and the call for architecture `a` SHALL carry name
`derive_vllm_component_name(record) + "-" + suffix(a)`, a recipe whose
`DefaultConfiguration.supported_architectures` is exactly `[a]`, and a HARD
`ComponentDependencies` entry on `a`'s `LocalServer` variant with platform
`linux/aarch64`. (For `a = arm64_jp7` the LocalServer/platform half of this
assertion is only satisfiable once the JP7 target maps exist, hence 2.17/2.18.)

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.17, 2.18**

Property 2: Preservation — Non-bug inputs are behaviorally identical

_For any_ input where none of the bug conditions hold, the fixed code SHALL
produce the same result as the original code. Concretely: for any non-vLLM
record, `publish'(X) = publish(X)` (per-target suffixed names, caller-supplied
version, `published_components` + `updated_at` write-back only, no atomicity
gate, fail-closed `PublishError` per unresolvable target); for any
`model-vllm-*` name carrying no known target suffix,
`load_vllm_model_record'(name) = load_vllm_model_record(name)` and
`vllm_component_architectures'(record, name)` equals the record-wide
`vllm_component_architectures(record)`; and for any vLLM record,
`prepArgs'(X).model_name = _safe_model_name(X.record.model_name)`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.14, 3.15**

Property 3: Fix Checking — Rollback is authorized and attempted for every ARN

_For any_ failed vLLM attempt where the bug condition holds
(`isBugCondition_2`: ≥1 component version created and ≥1 target failed), the
synthesized portal Lambda policy SHALL allow `greengrass:DeleteComponent` on the
Greengrass components resource ARN (and on no wider resource), a
`delete_component` call SHALL be attempted for every ARN created during the
attempt, and the rollback SHALL raise nothing — the reported error remains the
publish failure.

**Validates: Requirements 2.7, 2.8, 3.13, 3.15**

Property 4: Fix Checking — Derived version dominates every existing version

_For any_ component name and any set of versions existing in Greengrass for it
(`isBugCondition_3`: a cloud-side version the record history cannot see), the
derived next version SHALL match `^\d+\.0\.0$` and its major SHALL be strictly
greater than the major of every existing version, independent of the record's
`published_components` / `published_component` history.

**Validates: Requirements 2.9, 2.10**

Property 5: Fix Checking — Suffixed names round-trip to record and architecture

_For any_ published vLLM record and any supported architecture `a`, the
per-JetPack name `derive_vllm_component_name(record) + "-" + suffix(a)` SHALL
resolve through `load_vllm_model_record'` back to that record, and
`vllm_component_architectures'(record, name)` SHALL return exactly `[a]` —
matching the write-back the publish produced for that component.

**Validates: Requirements 2.5, 2.11, 2.12**

Property 6: Fix Checking — The per-JetPack arch gate is exact and fail-closed

_For any_ per-JetPack component whose supported set is `[a]` and any device
architecture `b`, the gate SHALL return no findings when `b = a`, at least one
finding when `b ≠ a` (including `arm64_jp4`, with reason `JP4_UNSUPPORTED` and
the JetPack-4 message), at least one finding when the device architecture is
null, and at least one finding when the component's supported set is empty. The
same verdicts SHALL hold in the frontend twin for the same inputs.

**Validates: Requirements 2.13, 2.14, 3.6, 3.7, 3.8, 3.9**

Property 7: Fix Checking — Derived names satisfy every downstream consumer

_For any_ model name and any supported architecture, the derived per-JetPack
component name SHALL start with `model-` (backend publish validation), start
with `model-vllm-` (`isVllmModelComponent` / `VLLM_MODEL_COMPONENT_PREFIX`),
contain a JetPack token matching `/(?:jp|jetpack)(4|5|6|7)(?![0-9])/` whose
major is that architecture's major (`inferComponentTargetArchs`), and match the
Greengrass component-name charset `^[a-zA-Z0-9._-]+$`; when the name would
exceed the 128-character Greengrass limit the publish SHALL fail closed for that
target with a `PublishError` and SHALL NOT call `create_component_version`.

**Validates: Requirements 2.6**

Property 8: Fix Checking — Target maps are total and unmapped targets fail closed

_For any_ vLLM record and any supported architecture `a`, the recipe generated
for `target(a)` SHALL carry a manifest platform of `platformFor(a)` (`aarch64`
for every Jetson target) AND a HARD `ComponentDependencies` entry on exactly
`localServerVariant(a)` (the `arm64JP{N}` variant for every Jetson target), so
platform and dependency always correspond to the same architecture. _For any_
target `t` in `values(packaging.VLLM_ARCH_TO_TARGET)`, `t` SHALL be present in
BOTH `TARGET_TO_LOCAL_SERVER` and `TARGET_TO_PLATFORM` (map totality in both
directions against the packaging map). And _for any_ target where the bug
condition holds (`isBugCondition_4`: absent from either map), recipe generation
SHALL raise `PublishError` and `create_component_version` SHALL NOT be called
for that target — the platform SHALL NOT default to `amd64` and the amd64
`LocalServer` SHALL NOT be selected. Already-mapped targets SHALL resolve to
exactly the LocalServer variant and platform they resolve to today, and a
genuinely unknown target SHALL still fail closed.

**Validates: Requirements 2.17, 2.18, 2.19, 3.18, 3.19**

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/backend/functions/greengrass_publish.py`

1. **JP7 target mappings and fail-closed platform resolution** (2.17, 2.18,
   2.19; prerequisite for 2.4): add
   `'jetson-xavier-jp7': 'aws.edgeml.dda.LocalServer.arm64JP7'` to
   `TARGET_TO_LOCAL_SERVER` and `'jetson-xavier-jp7': 'aarch64'` to
   `TARGET_TO_PLATFORM`, and add the target to the `PublishError` message's list
   of supported compile targets. Without this JP7 silently resolves to the amd64
   LocalServer via the `platform == 'amd64'` default, and the recipe manifest is
   stamped `amd64` (2.17, 2.18).

   The entries alone only fix JP7; 2.19 requires the *defaulting mechanism*
   itself to go, so the next target added to `packaging.VLLM_ARCH_TO_TARGET`
   without map entries cannot repeat the defect. Replace the silent default
   ```python
   platform = TARGET_TO_PLATFORM.get(target, 'amd64')
   ```
   with an explicit lookup that raises when the target is absent from **either**
   map:
   ```python
   def resolve_target_platform(target: str) -> str:
       """Fail closed: an unmapped target must never default to amd64."""
       if target not in TARGET_TO_PLATFORM or target not in TARGET_TO_LOCAL_SERVER:
           raise PublishError(
               f"Unsupported compile target '{target}': not mapped in "
               f"TARGET_TO_PLATFORM/TARGET_TO_LOCAL_SERVER "
               f"(supported: {sorted(set(TARGET_TO_PLATFORM) & set(TARGET_TO_LOCAL_SERVER))})"
           )
       return TARGET_TO_PLATFORM[target]
   ```
   Called wherever the platform is derived, so an unmapped target becomes a
   recorded failed target with a clear message before recipe generation and
   never reaches `create_component_version`. `resolve_local_server_component`
   keeps its existing fail-closed body unchanged — it simply stops being handed
   a bogus `'amd64'` platform. Every currently mapped target keeps resolving to
   exactly today's LocalServer variant and platform (3.18), and a genuinely
   unknown target still raises `PublishError` (3.19).

2. **Per-target vLLM naming** in the target loop: delete the vLLM special case
   so both branches use the vision convention:
   ```python
   target_suffix = target.replace('_', '-')
   target_component_name = f"{component_name}-{target_suffix}"
   ```
   `component_name` remains the Base_Component_Name from
   `derive_vllm_component_name` (unchanged function), so the record-level name,
   the GSI key, and the `model-` / `model-vllm-` prefixes are all preserved.

3. **Per-component supported architecture set**: pass
   `supported_architectures=[arch_for_target]` to
   `generate_vllm_component_recipe` instead of the record-wide `vllm_archs`,
   where `arch_for_target` comes from a module-level reverse map
   `VLLM_TARGET_TO_ARCH` mirroring `packaging.VLLM_ARCH_TO_TARGET` (the same
   "mirrored pure helper" convention this module already uses for
   `vllm_supported_architectures` / `_safe_model_name`, because the functions
   asset is bundled with the shared layer only). A target with no arch mapping
   fails closed with `PublishError` rather than advertising a guess.

4. **Component-name validation** (2.6): add
   ```python
   GREENGRASS_COMPONENT_NAME_MAX = 128
   GREENGRASS_COMPONENT_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')
   def validate_greengrass_component_name(name: str) -> None:  # raises PublishError
   ```
   called for the derived per-target name before recipe generation, so an
   over-long or malformed name is a recorded failed target with a clear message
   instead of an opaque API error. Applies to both branches (vision names are
   already valid, so this is a no-op there).

5. **Cloud-side version derivation** (2.9, 2.10): replace
   `next_vllm_component_version(training_job)` with the
   `workflow_packaging.py` pattern:
   ```python
   def existing_component_versions(greengrass, component_name) -> set   # mirrors _existing_component_versions
   def next_major_from_versions(versions) -> str                        # pure: f"{1 + max major}.0.0"
   def next_vllm_component_version(greengrass, component_names) -> str  # max over the names
   ```
   `existing_component_versions` resolves the component ARN via
   `list_components(scope='PRIVATE')` paging, then pages
   `list_component_versions`, warning and returning `set()` on `ClientError`
   (identical degradation to the workflow packager). The publish derives ONE
   shared `N.0.0` = 1 + the highest existing major across all per-JetPack names,
   so every name gets a version strictly above everything that exists for it,
   while the record keeps a single `component_version` (preserving `model_id =
   f"{training_id}-{component_version}"` and the response shape). The record's
   own history is no longer consulted at all.

6. **Call ordering**: move the `get_usecase_client('greengrassv2', …)` creation
   ABOVE the preflight fit-check block (it needs only `usecase`, already
   fetched) so the derived name list and version are available to the 422 / skip
   / unverified branches exactly as today. No `create_component_version` call
   moves — the fit gate remains the first fail-closed point before any
   registration (3.12).

7. **Write-back shape for N components** (2.5): the `published_component` map
   keeps every key it has today and gains a `components` list:
   ```python
   published_component = {
       'component_name': component_name,            # Base_Component_Name (GSI key, display)
       'component_version': component_version,      # shared N.0.0
       'supported_architectures': vllm_archs,       # record-wide union (legacy readers)
       'runtime': 'vllm',
       'component_arns': {target: arn, …},          # unchanged
       'components': [                              # NEW — one entry per Per_JetPack_Component
           {'component_name': f'{base}-{suffix}',
            'component_version': component_version,
            'target': target,
            'architecture': arch,
            'supported_architectures': [arch],
            'component_arn': arn},
           …
       ],
       'published_at': timestamp,
   }
   ```
   Each `published_components` entry additionally carries
   `'supported_architectures': [arch]` alongside its already-per-target
   `component_name`. The top-level `component_name` attribute stays the
   unsuffixed base name, so the `component_name-index` GSI keeps resolving ONE
   record from ONE string and no index change is needed.

8. **Rollback logging** (2.8): keep the try/except-warning shape; include the
   component name and version parsed from the ARN in the warning so a surviving
   version is identifiable from logs. No behavior change beyond message detail.

**File**: `edge-cv-portal/backend/functions/deployments.py`

9. **Closed suffix vocabulary**: module-level
   `VLLM_TARGET_SUFFIX_TO_ARCH = {'jetson-xavier-jp5': 'arm64_jp5',
   'jetson-xavier-jp6': 'arm64_jp6', 'jetson-xavier-jp7': 'arm64_jp7'}` with a
   "keep in sync with `packaging.VLLM_ARCH_TO_TARGET`" comment (same mirroring
   convention as the publish module), plus a pure
   `split_vllm_component_name(name) -> (base_name, arch | None)`.

10. **Suffix-aware record resolution** (2.11): `load_vllm_model_record(name)`
    queries the GSI with the exact name first (the legacy path — one query,
    byte-identical behavior for unsuffixed names), and only when that misses AND
    the name carries a known suffix does it re-query with the stripped base
    name. Everything else (ClientError warning, newest-`created_at` tiebreak,
    `_decimal_to_native`) is untouched.

11. **Per-component architecture resolution** (2.12):
    `vllm_component_architectures(record, component_name=None)` — the new
    parameter is optional and keyword-compatible with existing callers:
    1. if `published_component.components` contains an entry whose
       `component_name` equals `component_name` → that entry's
       `supported_architectures`;
    2. elif `component_name` carries a known target suffix → `[arch]` if that
       arch is in the record-wide set, else `[]` (fail closed on an
       out-of-set suffix);
    3. else (no name given, or an unsuffixed legacy name) → the record-wide
       `published_component.supported_architectures` /
       `record.supported_architectures`, exactly as today (3.4).
    `collect_vllm_component_manifests` passes the component name through.
    `evaluate_vllm_arch_gate` itself is NOT touched.

**File**: `edge-cv-portal/frontend/src/pages/deployments/vllmArchGate.ts`

12. Add the pure TS twin of the resolution rules: `VLLM_TARGET_SUFFIX_TO_ARCH`,
    `splitVllmComponentName(name)`, and
    `vllmArchsForComponent(componentName, publishedComponent)` implementing the
    same three-rule order. Kept UI-free so fast-check can exercise it. The gate
    function `evaluateVllmArchGate` and the 409 parsing are unchanged.

**File**: `edge-cv-portal/frontend/src/pages/deployments/archCompatibility.ts`

13. No production change is required — `componentSupportedArchs` already keys
    `vllmArchs` by the exact component name, and `inferComponentTargetArchs`
    already matches the `jp6`/`jp7` token a suffixed name carries. The change is
    documentation: state in the module comment that vLLM entries are now keyed by
    the suffixed per-JetPack name and resolved via `vllmArchsForComponent`. The
    property test gains suffixed-name cases (2.13).

**File**: `edge-cv-portal/frontend/src/pages/CreateDeployment.tsx`

14. In the vLLM arch-resolution effect, replace
    `resp.model.published_component?.supported_architectures || []` with
    `vllmArchsForComponent(name, resp.model.published_component)`. The
    component→record join is unchanged (the listing's `training_job_id`, which
    every per-JetPack component version carries via the
    `dda-portal:training-id` tag), as are the still-resolving (`undefined`) and
    fail-closed (`[]`) semantics. `models.py` already returns
    `published_component` verbatim, so the new `components` list reaches the
    client with no API change.

**File**: `edge-cv-portal/infrastructure/lib/compute-stack.ts`

15. Add `'greengrass:DeleteComponent'` to the existing combined per-service
    statement's action list (the one already scoped to
    `arn:aws:greengrass:*:*:components:*`, `…coreDevices:*`, `…deployments:*`).
    No new statement, no resource change, no other action added.
    **Security justification**: rollback only ever deletes component versions
    the portal itself created seconds earlier in the same failed publish; the
    action is confined to the Greengrass components resource ARN (never
    `*`), and the equivalent cross-account role in
    `DDAPortalUseCaseAccountStack` already grants exactly this action — this
    closes an inconsistency rather than widening the trust boundary.

**Files**: `test/backend-test/security/baselines/…`

16. Rebaseline per the gate's documented protocol
    (`test/backend-test/security/preservation/README_iam.md`,
    `.kiro/steering/builds.md`): move `edge-cv-portal/infrastructure/cdk.out`
    aside first (the cdk.out drift guard), re-synthesize, and update
    `iam_baseline_EdgeCVPortalComputeStack.template.json` so
    `test_synth_matches_fixed_baseline` passes. Because
    `test_baseline_drift_confined_to_I*` asserts the symmetric difference between
    the fixed and `.unfixed` templates equals the recorded I1–I4 change set, the
    matching statement string in
    `iam_baseline_cdk_i_changes.json` (`EdgeCVPortalComputeStack` → `added`)
    must be updated in the same commit; the `.unfixed.template.json` capture is
    the historical pre-fix `F(X)` record and is only touched if the
    drift-confinement test demands it (verify by running the suite). Record in
    the commit message which artifact was rebaselined and that the sole drift is
    the intentional `greengrass:DeleteComponent` grant. Do NOT weaken or delete
    the gate. The 4 known-acceptable local-only `cdk.out` drift failures stay
    untouched (3.17).

## Cross-Spec Documentation Consistency

All work lands on the existing branch `spec/jetpack7-support` — **no new
branch**. Per-JetPack vLLM component naming changes assumptions that six
completed sibling specs on this branch describe as current behavior. Each needs
a short **amendment note** appended (referencing
`.kiro/specs/vllm-multi-arch-publish-conflict/`), not a rewrite; these are
deliverables in the task list, not silent drift.

| Sibling spec | What this fix supersedes | Amendment note to add |
| --- | --- | --- |
| `.kiro/specs/jp7-vllm-enablement/` (10/10 complete) | `design.md`'s manual-validation row "Publish → component `model-vllm-*` published, architectures include `arm64_jp7`" and Requirement 4.4's "the published component's supported architectures SHALL include `arm64_jp7`" — there is no longer ONE component advertising both JetPacks. Task 6.2 also added `arm64_jp7` to `vllm_supported_architectures()` and `packaging.VLLM_ARCH_TO_TARGET` but not to `greengrass_publish.py`'s `TARGET_TO_LOCAL_SERVER` / `TARGET_TO_PLATFORM`, which is what let JP7 resolve to the amd64 LocalServer. | Note that JP7 support is now delivered as its own component `model-vllm-{safe}-jetson-xavier-jp7` advertising exactly `['arm64_jp7']` with a HARD dependency on `…LocalServer.arm64JP7`, and that the two missing target maps were completed here. Requirement 4.4 is satisfied per-component rather than record-wide. |
| `.kiro/specs/vllm-model-name-mismatch/` | Nothing functional. That spec deliberately fixed the component name to `model-vllm-{safe_model_name}`; per-target suffixing appends to that base name. | Note explicitly why the intent is not regressed: the Triton identity travels on `--model_name` (`_safe_model_name(model_name)`, unchanged), while `--component_name` is logging-only in `src/backend/dda_triton/vllm_model_prep.py` (argparse help "(logging)"; `prepare()` binds it to `component` and only logs it). `derive_vllm_component_name` still returns `model-vllm-{safe_model_name}` verbatim, and that spec's transform-equality property test passes unmodified. |
| `.kiro/specs/vllm-triton-inference/` | The recipe's `DefaultConfiguration.supported_architectures` and the record's `published_component.supported_architectures` were record-wide; the gate's supported set is now resolved per component. | Note that `evaluate_vllm_arch_gate`, the 409 `VLLM_ARCH_UNSUPPORTED` contract, the fail-closed rules and the JP4 reason are all unchanged; only the *source* of a `model-vllm-*` component's supported set changed (per-component entry, with the record-wide set retained for legacy unsuffixed components). |
| `.kiro/specs/device-arch-compatibility/` | `vllmComponentArchs` resolution in `archCompatibility.ts` / `vllmArchGate.ts` / `CreateDeployment.tsx`: entries are now keyed by the exact suffixed component name and resolved with `vllmArchsForComponent`. | Note that the exact-name, no-fallback matching contract and the fail-closed rules (null device arch, empty supported set, still-resolving `undefined`) are unchanged; the map is now keyed per per-JetPack component, so one record can contribute several keys with disjoint single-arch sets. |
| `.kiro/specs/jetpack7-support/` (umbrella) | The JP7 rollout narrative: a vLLM model on JP7 now means a JP7-specific model component, not a shared one. | One-paragraph pointer to this spec in the JP7 vLLM section. |
| `.kiro/specs/localserver-arch-naming/` | `resolve_local_server_component`'s fail-closed variant resolution is intact, but the `platform == 'amd64'` fallback silently covered the unmapped `jetson-xavier-jp7` target because `TARGET_TO_PLATFORM.get(target, 'amd64')` defaults to amd64. | Note the added `jetson-xavier-jp7` → `…LocalServer.arm64JP7` / `aarch64` entries, and that any future aarch64 target must be added to BOTH maps or the amd64 default defeats fail-closed resolution. |

## Operational Recovery and Deployment

### The orphan: two paths, and which is preferred

The orphan is `model-vllm-qwen3-vl-8b-instruct:1.0.0` in account
`164152369890` (`us-east-1`), tagged `dda-portal:managed=true` with training-id
`1e05eb99-ca55-4325-9be0-15874979e6a3`, with no backing publish state.

- **Path A (preferred): let the code fix make it non-blocking.** After the fix
  the record publishes as `model-vllm-qwen3-vl-8b-instruct-jetson-xavier-jp6`
  and `…-jetson-xavier-jp7` — different component names, whose existing-version
  sets are empty, so both start at `1.0.0` and the orphan can never collide
  again. Cloud-side version derivation then guarantees the same for any FUTURE
  failed attempt on those names. Preferred because it needs no live AWS
  mutation, is idempotent, and fixes the class of problem rather than this one
  instance (Requirement 2.15's "or otherwise made non-blocking").
- **Path B (optional hygiene): delete the orphan.** Its only residual effect is
  cosmetic-plus-confusing: it keeps appearing in managed-component listings, and
  because no record resolves it, the deploy screen fails closed and hides it.
  Deleting it is a **live AWS mutation and REQUIRES explicit user confirmation**
  before execution (`aws greengrassv2 delete-component --arn
  arn:aws:greengrass:us-east-1:164152369890:components:model-vllm-qwen3-vl-8b-instruct:versions:1.0.0`).
  Do it only after Path A is deployed, so the delete is cleanup rather than a
  prerequisite, and only with confirmation.

### Required deploy before anything takes effect (2.16)

Nothing in this fix is active in the account until the portal is deployed —
state this as an explicit step, do not assume it:

1. Lambda code: `greengrass_publish.py` and `deployments.py` (functions asset).
2. Infrastructure: the `compute-stack.ts` IAM change (the
   `greengrass:DeleteComponent` grant).
3. Frontend: `CreateDeployment.tsx` / `vllmArchGate.ts` /
   `archCompatibility.ts`.

Use the repo's portal deploy path (`deploy-portal.sh`, or
`deploy-infrastructure.sh` + `deploy-frontend.sh`). Per
`.kiro/steering/builds.md`, do **not** run a portal deploy while a component
build is in flight, and move `edge-cv-portal/infrastructure/cdk.out` aside
before running the security guard suite (a portal deploy regenerates it and is
the classic cause of drift-guard failures). Verification after deploy: publish
the affected record and confirm two DEPLOYABLE components, then confirm the JP7
component is selectable for thing `jetson-thor1` and the JP6 component is shown
incompatible, on `https://d23v4ltibogb5x.cloudfront.net`.

## Testing Strategy

### Validation Approach

Two phases. First, exploratory tests on the UNFIXED tree that surface
counterexamples and confirm the located root causes (each defect has a
one-line cause, so refutation is unlikely but must be observed, not assumed).
Then fix checking against the Correctness Properties and preservation checking
over the untouched behaviors, with the existing suites as regression gates.

Backend suites MUST run separately (module-name collisions when combined):

```
PYTHONPATH=src/backend:test/backend-test python3 -m pytest <suite> -q -p no:cacheprovider --noconftest
```

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix, and confirm or refute the four hypothesized causes. If refuted,
re-hypothesize before writing any fix.

**Test Plan**: A new exploration suite
(`edge-cv-portal/backend/tests/test_vllm_multi_arch_publish_exploration.py`)
seeds a vLLM record with BOTH JP6 and JP7 packaged targets and drives
`publish_component` against a fake Greengrass client that behaves like the
service: `create_component_version` raises `ConflictException` on a repeated
`(ComponentName, ComponentVersion)`, and `delete_component` raises
`AccessDeniedException` (mirroring the unauthorized portal role). A source-level
assertion covers the IAM and target-map causes without AWS.

**Test Cases**:
1. **Duplicate identity**: publish with two packaged targets → 502 with
   `failed_step: greengrass_registration` and two creates carrying the identical
   name:version (will fail on unfixed code once the fix lands; on unfixed code it
   *demonstrates* the conflict).
2. **Rollback denied**: the same attempt → `delete_component` was attempted and
   the exception was swallowed; the created version survives in the fake's state
   (will fail on unfixed code).
3. **Wedged retry**: re-invoke publish with the orphan version present in the
   fake → derived version is `1.0.0` again and conflicts on the FIRST target
   (will fail on unfixed code).
4. **JP7 recipe mis-stamping** (`isBugCondition_4`, defect 4): with
   `target = 'jetson-xavier-jp7'` — which satisfies `isBugCondition_4` on the
   unfixed tree because it is in `values(packaging.VLLM_ARCH_TO_TARGET)` but in
   neither `TARGET_TO_LOCAL_SERVER` nor `TARGET_TO_PLATFORM` — assert the JP7
   recipe's manifest platform and HARD `ComponentDependencies` entry are
   `aarch64` / `…LocalServer.arm64JP7`. On unfixed code they are `amd64` /
   `…LocalServer.amd64` and the publish SUCCEEDS rather than failing, so the
   counterexample is the mis-stamped recipe, not an exception (will fail on
   unfixed code). Also assert directly that `isBugCondition_4` holds for
   `jetson-xavier-jp7` on the unfixed maps, so the condition itself is recorded.
5. **IAM absence**: assert `greengrass:DeleteComponent` is absent from the
   `compute-stack.ts` Greengrass action list (passes on unfixed code, documents
   `F(X)`; inverted after the fix).
6. **Edge case**: a record whose base name plus suffix exceeds 128 characters —
   observe that unfixed code calls `create_component_version` anyway.

**Expected Counterexamples**:
- Two creates with identical `(ComponentName, ComponentVersion)`; the second
  raises `ConflictException`.
- A surviving component version after a rollback that logged only a warning.
- `1.0.0` re-derived on every retry regardless of cloud-side state.
- Possible causes: fixed vLLM name in the target loop, single pre-loop version,
  missing IAM grant, history-only version derivation, missing JP7 target maps.

### Fix Checking

**Goal**: For all inputs where a bug condition holds, the fixed code produces the
expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  result := publish'(X)
  ASSERT expectedBehavior(result)   // Properties 1, 3, 4, 5, 6, 7, 8
END FOR
```

### Preservation Checking

**Goal**: For all inputs where no bug condition holds, the fixed code produces
the same result as the original.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT publish(X) = publish'(X)
END FOR
```

**Testing Approach**: Property-based testing is the right instrument here
because the preservation surface is large and mostly mechanical (arbitrary model
names, arbitrary architecture sets, arbitrary legacy vs suffixed component
names): generated cases cover the space that hand-written examples would miss,
and the existing suites already follow this convention (Hypothesis for Python,
fast-check for TS).

**Test Plan**: Observe the UNFIXED behavior for vision publish, legacy
unsuffixed vLLM resolution, and the Triton prep arguments first, then encode
that behavior as properties that must still hold after the fix.

**Test Cases**:
1. **Vision publish preservation**: observe per-target suffixed names,
   caller-supplied version, `published_components` + `updated_at` only, no
   atomicity gate, fail-closed `PublishError` per unresolvable target — then
   assert unchanged (3.1, 3.2).
2. **Legacy unsuffixed resolution**: observe that an unsuffixed
   `model-vllm-*` name resolves to its record and to the record-wide supported
   set with a single GSI query — then assert unchanged (3.4, 3.5).
3. **Triton identity**: observe `--model_name = _safe_model_name(model_name)` in
   the generated recipe's Startup/Shutdown scripts — then assert unchanged under
   suffixed component names (3.3).
4. **Gate semantics**: observe null-arch, empty-set, and `arm64_jp4` verdicts —
   then assert unchanged (3.6–3.9).
5. **IAM statements**: observe every synthesized statement, then assert the only
   drift is the added `DeleteComponent` action (3.15, 3.16).
6. **Target-map resolution preservation**: observe, on unfixed code, the
   LocalServer variant and platform each already-mapped target resolves to
   (`x86_64-cpu` / `x86_64-cuda` → `…LocalServer.amd64` + `amd64`;
   `jetson-xavier`, `jetson-xavier-jp5`, `jetson-xavier-jp6`, `arm64-cpu` → their
   current aarch64 variants) — then assert every one of those pairs is
   byte-identical after the fix, i.e. adding the JP7 entries and removing the
   amd64 default changed nothing for them (3.18). Also observe that a genuinely
   unknown target raises `PublishError` and assert it still does (3.19).

### Unit Tests

- `derive_vllm_component_name` unchanged; per-target name composition for each
  target in the closed suffix vocabulary.
- `validate_greengrass_component_name`: accepts valid names, raises
  `PublishError` on over-length and on illegal characters.
- `VLLM_TARGET_TO_ARCH` / `VLLM_TARGET_SUFFIX_TO_ARCH` mirror
  `packaging.VLLM_ARCH_TO_TARGET` exactly (totality both ways).
- **Target-map totality both ways** (2.17, 2.19): every value of
  `packaging.VLLM_ARCH_TO_TARGET` is a key of BOTH `TARGET_TO_LOCAL_SERVER` and
  `TARGET_TO_PLATFORM`, and every Jetson key of those maps that the packaging map
  can produce maps to platform `aarch64` and an `arm64JP{N}` LocalServer variant.
  This is the test that would have caught defect 4 at the time task 6.2 landed.
- **Unmapped aarch64 target fails closed** (2.19): a synthetic target injected
  into `packaging.VLLM_ARCH_TO_TARGET` but absent from either module map makes
  `resolve_target_platform` (and therefore recipe generation) raise
  `PublishError`, and no `create_component_version` call is made for it — it does
  NOT default to `amd64`.
- `existing_component_versions`: paging, no-such-component → `set()`,
  `ClientError` → warn + `set()`.
- `next_vllm_component_version`: `1.0.0` when nothing exists; `N+1.0.0` over the
  highest major across all names; unaffected by record history.
- New JP7 target maps: `resolve_local_server_component('jetson-xavier-jp7',
  'aarch64') == 'aws.edgeml.dda.LocalServer.arm64JP7'` and platform `aarch64`.
- `split_vllm_component_name` / `splitVllmComponentName`: suffixed, unsuffixed,
  and unknown-suffix names.
- `vllm_component_architectures`: per-component entry hit, suffix fallback,
  out-of-set suffix → `[]`, legacy record-wide path.
- Publish write-back: `components` list shape, one entry per architecture,
  top-level `component_name` still the base name, `published_components` entries
  carrying `[arch]`.
- Rollback: `delete_component` attempted per created ARN; a raising delete does
  not propagate.

### Property-Based Tests

Backend (Hypothesis), new suite
`edge-cv-portal/backend/tests/test_vllm_multi_arch_publish_properties.py`, one
test per Correctness Property with `Validates: Requirements …` comments:

- **Property 1**: over model names × supported-arch subsets — one create per
  arch, all identities distinct, per-component `supported_architectures == [a]`,
  HARD dependency on `a`'s LocalServer, platform `aarch64`.
- **Property 4**: over arbitrary sets of existing version strings — derived
  version matches `^\d+\.0\.0$` and strictly dominates every existing major.
- **Property 5**: over model names × arch subsets — suffixed name round-trips to
  the record (moto-backed GSI) and to exactly `[a]`.
- **Property 6**: over (component arch, device arch) pairs including null and
  empty sets — exact gate verdicts and preserved JP4 reason.
- **Property 7**: over arbitrary model names — `model-` and `model-vllm-`
  prefixes, JetPack-token major matches the arch, Greengrass charset, and
  fail-closed above 128 characters.
- **Property 2 (preservation)**: over non-vLLM records and legacy unsuffixed
  names — `F(X) = F'(X)` for vision publish, legacy resolution, and Triton prep
  arguments.
- **Property 3**: source-level over the synthesized/loaded policy — the action
  is granted on the components ARN and on nothing wider; rollback attempts every
  ARN and raises nothing.
- **Property 8** (`# Validates: Requirements 2.17, 2.18, 2.19, 3.18, 3.19`): over
  the full set of producible targets plus generated unmapped target names — every
  `packaging.VLLM_ARCH_TO_TARGET` value is a key of both module maps, each
  generated recipe's manifest platform and HARD LocalServer dependency correspond
  to the same architecture (`aarch64` + `arm64JP{N}` for every Jetson target),
  every already-mapped target resolves to exactly today's variant and platform,
  and any target absent from either map raises `PublishError` with no
  `create_component_version` call.

Frontend (fast-check), new
`edge-cv-portal/frontend/src/pages/deployments/vllmSuffixArch.property.test.ts`:

- `vllmArchsForComponent` is the exact twin of the backend resolution (same
  three-rule order) over generated `published_component` maps.
- A per-JetPack component is compatible with its own arch and with no other,
  including null-arch and empty-set fail-closed cases (Property 6 twin).
- Legacy unsuffixed names still resolve to the record-wide set (Property 2 twin).

### Integration Tests

- Backend end-to-end publish (moto + fake Greengrass): a two-target vLLM record
  publishes two DEPLOYABLE components, returns 200 with two
  `published_components` entries, and writes the `components` list; a forced
  failure on the second target rolls both back, writes no publish state, and
  returns the retryable 502.
- Deploy-gate round trip: publish, then run `check_vllm_deployment_gate` for a
  `arm64_jp7` device against both component names — the JP7 component passes,
  the JP6 component is rejected with `ARCH_UNSUPPORTED`.
- Frontend: extend `CreateDeployment.archFilter.test.tsx` so the mocked record
  returns a `components` list and the suffixed JP7 component is offered while
  the JP6 one is filtered out for a `arm64_jp7` device.
- Manual, post-deploy: publish the affected record, revise the `jetson-thor1`
  deployment, confirm the JP7 component is selectable and the JP6 component is
  labeled incompatible.

### Preservation Gates To Re-run

Backend (each separately, with the command above):

- `edge-cv-portal/backend/tests/test_vllm_publish_fit_gate.py` — expected to need
  its created-`ComponentName` assertion updated to the suffixed name and its
  fake Greengrass client extended with `get_paginator`; every status-code, fit
  status, and record-state assertion must pass unchanged.
- `edge-cv-portal/backend/tests/test_vllm_publish_writeback.py` — expected to
  need the per-target `component_name` / ARN assertions updated to suffixed names
  and new `components`-list assertions; record-level `component_name`,
  `published_component.component_name`, version, `published`, models-table item,
  and rollback assertions must pass unchanged.
- `edge-cv-portal/backend/tests/test_deployment_plugin_gates.py` — unchanged.
- `edge-cv-portal/backend/tests/test_deployment_vllm_gate.py` — unchanged
  (legacy unsuffixed seeding exercises the preserved path).
- `edge-cv-portal/backend/tests/test_vision_model_packaging_preservation.py` —
  unchanged (Property 2).
- `edge-cv-portal/backend/tests/test_property_llm_model_name_preservation.py` —
  unchanged (base-name transform equality).
- `test/backend-test/security/preservation/` — after the rebaseline; the only
  permitted drift is the `DeleteComponent` grant. The 4 known-acceptable
  local-only `cdk.out` drift failures are pre-existing and left alone.

Frontend (vitest, single run):

- `src/pages/deployments/archCompatibility.property.test.ts`
- `src/components/vllm-publish/*.property.test.ts`
- `src/pages/CreateDeployment.archFilter.test.tsx`
- `src/pages/ModelDetail.vllmPublish.integration.test.tsx` (the record-wide
  supported-architecture badge is preserved by the retained union field)
