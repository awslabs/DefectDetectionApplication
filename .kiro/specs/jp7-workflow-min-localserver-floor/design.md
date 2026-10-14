# JP7 Workflow Min-LocalServer Floor Bugfix Design

## Overview

Workflow deployment `cb139a40` (workflow `dda.workflow.421f8233` v5.0.0 →
jetson-thor1, JP7) failed `FAILED_NO_STATE_CHANGE` because the packaged
component carries a HARD `aws.edgeml.dda.LocalServer.arm64JP7 >= 1.0.63`
recipe dependency — a number from the WRONG version lineage. LocalServer
variants version independently and their lineages are not comparable
(arm64JP7 latest = 1.0.5, arm64JP6 = 1.0.59, arm64JP5 = 1.0.39,
amd64 = 1.0.37, legacy bare arm64 = 1.0.45; the scalar
`DDA_LOCAL_SERVER_VERSION = '1.0.63'` is a legacy-lineage number).

Root cause (confirmed, not hypothesized): the CDK Lambda environment map
`WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` in
`edge-cv-portal/infrastructure/lib/compute-stack.ts` (~line 647) has
`arm64_jp4` / `arm64_jp5` / `arm64_jp6` keys but NO `arm64_jp7` key (and no
`x86_64` / `x86_64_nvidia` keys), while BOTH backend consumers —
`workflow_packaging.py::min_local_server_version_for` (packager: recipe
`ComponentDependencies` via `local_server_component_dependencies`, and
`manifest.json::minLocalServerVersion` via `build_manifest`) and
`deployments.py::check_local_server_compatibility` (pre-submit gate via the
module-level `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` map) — silently fall back
to the cross-lineage scalar for any arch absent from the map. The JP7 fan-out
(commit `c47f6ec`) added `arm64_jp7` to `ARCH_TO_LOCAL_SERVER_COMPONENT` but
missed the CDK env map; x86 was latently broken the same way from the start.

The fix is cloud-side only and three-part:

1. **Complete the map** (CDK): add `arm64_jp7: '1.0.0'`, `x86_64: '1.0.0'`,
   `x86_64_nvidia: '1.0.0'` to the `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS`
   literal, covering every key of `ARCH_TO_LOCAL_SERVER_COMPONENT`.
2. **Harden the fallback** (backend): an arch that is KNOWN to the consumer
   (present in `ARCH_TO_LOCAL_SERVER_COMPONENT`, or in the deployments-side
   arch vocabulary) but MISSING from a configured (non-empty) floor map never
   silently inherits the scalar again — it resolves to the safe per-lineage
   floor `'1.0.0'` with a loud warning (Decision 1 below). The scalar chain
   survives unchanged for the unconfigured-map case and for arch-undetermined
   devices (requirement 3.6).
3. **Pin the coverage** (test): a permanent backend test reads the actual
   `compute-stack.ts` env literal and asserts its keys equal
   `ARCH_TO_LOCAL_SERVER_COMPONENT`'s keys (and cover the deployments-side
   arch vocabulary), so a future JP8 fan-out that repeats the omission fails
   at test time, not in the field (Decision 3 below).

Nothing device-side changes; no component build is involved. Shipping the fix
requires a portal deploy, which per `.kiro/steering/builds.md` MUST NOT run
while a component build is running — a JP7 component build (job `998b6f42`)
is running right now, so the deploy is an orchestrator-sequenced USER ACTION.
Already-published workflow versions are immutable; the recovery path for the
incident package is re-packaging workflow `421f8233` (auto MAJOR bump via
`next_component_version`) and re-deploying to jetson-thor1 — the acceptance
criterion (requirements 2.5, 2.6), also a USER ACTION sequenced after the
portal deploy.

## Glossary

- **Bug_Condition (C)**: a workflow packaging or pre-submit-gate resolution
  in which an arch known to the consumer but absent from the configured
  per-arch floor map silently resolves to the cross-lineage scalar
  (`1.0.63`) — producing an unsatisfiable recipe constraint, a wrong
  manifest floor, or a spurious pre-submit rejection
- **Property (P)**: the desired behavior — every known arch resolves a
  per-lineage floor (its own map entry, or the safe `'1.0.0'` with a loud
  warning); NO cross-lineage constraint is ever emitted into a recipe,
  manifest, or pre-submit decision
- **Preservation**: jp4/jp5/jp6 resolution byte-identical; the multi-variant
  omission (Defect F), model/plugin dependency resolution, manifest schema,
  per-version override semantics, legacy component-name recognition, and the
  scalar env contract all unchanged
- **Floor map**: the `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` JSON env var
  (defined in `compute-stack.ts`, parsed independently by
  `workflow_packaging.py` and `deployments.py`) mapping workflow_core arch
  ids to that arch's minimum LocalServer version
- **Cross-lineage scalar**: `WORKFLOW_MIN_LOCAL_SERVER_VERSION` /
  `DDA_LOCAL_SERVER_VERSION` (`'1.0.63'` in prod) — a legacy-lineage version
  number meaningless in the JP5/JP6/JP7/amd64 lineages
- **Safe per-lineage floor (`'1.0.0'`)**: satisfiable in EVERY LocalServer
  lineage (all variants version from 1.0.x and workflow support ships in
  current field builds); the value every existing map entry already uses
- **`ARCH_TO_LOCAL_SERVER_COMPONENT`**: `workflow_packaging.py`'s fail-closed
  arch → LocalServer-variant map (6 keys: arm64_jp4/jp5/jp6/jp7, x86_64,
  x86_64_nvidia; both x86 flavors → the one `.amd64` variant)
- **Deployments arch vocabulary**: the possible non-None returns of
  `deployments.py::local_server_component_arch` — {arm64_jp4, arm64_jp5,
  arm64_jp6, arm64_jp7, x86_64} (installed amd64 components read as
  `x86_64`; `x86_64_nvidia` never appears on the read side).
  `deployments.py` CANNOT import `workflow_packaging` (Lambda bundling —
  documented at deployments.py ~line 70), so it needs its own constant,
  pinned in lockstep by the coverage test
- **`min_local_server_version_for(arch)`**: `workflow_packaging.py` ~line
  178 — the packager's floor resolution (map entry, else scalar today)
- **`local_server_component_dependencies(archs)`**: `workflow_packaging.py`
  ~line 1457 — emits the HARD LocalServer `ComponentDependencies` entry
  (`'>=' + floor`) when the selected archs collapse to one variant; omits it
  entirely (with warning) for multi-variant selections (Defect F)
- **`build_manifest`**: `workflow_packaging.py` ~line 1630 — writes
  `minLocalServerVersion` (arch-scoped scalar field) and
  `minLocalServerVersions` (the full parsed map) into `manifest.json`
- **`check_local_server_compatibility`**: `deployments.py` ~line 2484 — the
  pre-submit gate; per device, `effective_min = by_arch.get(arch, fallback)`
- **Per-version override**: `min_local_server_version` on a WorkflowVersions
  item; when present the caller passes `by_arch={}` and the override applies
  uniformly to every device (requirement 3.5 — must stay bypassing the map)
- **`next_component_version`**: `workflow_packaging.py` ~line 514 — resolves
  the next free component version for re-packaging (auto MAJOR bump); the
  recovery path for the immutable bad package
- **Incident package**: `dda.workflow.421f8233` v5.0.0, carrying
  `aws.edgeml.dda.LocalServer.arm64JP7 >= 1.0.63` forever (immutable)

## Bug Details

### Bug Condition

The bug manifests whenever the floor resolution runs for an arch that the
consumer knows (it has a LocalServer variant) but the configured floor map
does not list. Today that is `arm64_jp7` (incident), `x86_64`, and
`x86_64_nvidia` (latent); tomorrow it is any future arch (JP8) whose fan-out
repeats the omission. Both consumers bite: the packager bakes the scalar
into an immutable recipe + manifest, and the pre-submit gate rejects
deployable devices.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X of type FloorResolution
         { consumer: packager | presubmitGate,
           arch: ArchId,                  // workflow_core arch id
           floorMap: Map[ArchId, Version], // parsed WORKFLOW_MIN_LOCAL_SERVER_VERSIONS
           scalar: Version }              // cross-lineage scalar (1.0.63 in prod)
  OUTPUT: boolean

  RETURN X.arch IS KNOWN to X.consumer
           // packager: X.arch IN keys(ARCH_TO_LOCAL_SERVER_COMPONENT)
           // gate:     X.arch IN deployments arch vocabulary
         AND X.floorMap IS non-empty        // a map IS configured
         AND X.arch NOT IN keys(X.floorMap) // ... but misses this arch
         AND resolvedFloor(X) = X.scalar    // silent cross-lineage fallback
END FUNCTION
```

On the unfixed tree with the prod environment (`floorMap` =
`{arm64_jp4: 1.0.0, arm64_jp5: 1.0.0, arm64_jp6: 1.0.0}`, `scalar` =
`1.0.63`), `isBugCondition` is true for `arm64_jp7`, `x86_64`, and
`x86_64_nvidia` in the packager and for `arm64_jp7` and `x86_64` in the
pre-submit gate.

### Examples

- **Incident (defects 1.1–1.3)**: packaging `421f8233` for `arm64_jp7` →
  `min_local_server_version_for('arm64_jp7')` returns `1.0.63` → recipe gets
  HARD `aws.edgeml.dda.LocalServer.arm64JP7 >= 1.0.63` and manifest gets
  `minLocalServerVersion: 1.0.63` → deployment `cb139a40` to jetson-thor1
  (thing pin `=1.0.5`, model constraint `>=1.0.0 <2.0.0`) fails
  `FAILED_NO_STATE_CHANGE`. Expected: floor `1.0.0`, constraint
  `>= 1.0.0`, satisfiable by 1.0.5.
- **Second bite (defect 1.4)**: portal deploy of a JP7 workflow to
  jetson-thor1 → gate computes `by_arch.get('arm64_jp7', '1.0.63')` →
  rejects pre-submit: "Installed LocalServer version 1.0.5 is older than the
  required minimum 1.0.63". Expected: effective min `1.0.0`, device passes.
- **Latent x86 (defect 1.5)**: packaging for `x86_64` (or `x86_64_nvidia`)
  → `aws.edgeml.dda.LocalServer.amd64 >= 1.0.63` baked (amd64 lineage latest
  = 1.0.37); gate blocks amd64 devices identically. Undetected only because
  no x86 workflow deploy has been attempted.
- **Future recurrence (defect 1.6)**: a JP8 fan-out adds `arm64_jp8` to
  `ARCH_TO_LOCAL_SERVER_COMPONENT` but not to the CDK map → same silent
  scalar today; after the fix, the coverage test fails at build/test time
  AND the runtime hardening resolves `1.0.0` with a loud warning.
- **Edge case — immutability (defect 1.7)**: `421f8233` v5.0.0 carries
  `>=1.0.63` forever; fixing the env alone repairs nothing already
  published. Recovery: re-package (auto MAJOR bump to the next free `N.0.0`)
  and re-deploy — the acceptance path.
- **Edge case — must NOT change (3.1)**: packaging for `arm64_jp6` resolves
  `1.0.0` from its own map entry, exactly as today, byte-identical
  constraint and manifest.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- **jp4/jp5/jp6 resolution (3.1)**: byte-identical recipe
  `ComponentDependencies` (`'>=1.0.0'`) and `manifest.json`
  `minLocalServerVersion` for `arm64_jp4`/`arm64_jp5`/`arm64_jp6`; JP5/JP6
  portal deploys pass the gate exactly as today.
- **Multi-variant omission (3.2)**: archs resolving to more than one
  distinct LocalServer variant still omit the LocalServer dependency
  entirely with the existing warning (Defect F) — `local_server_component_
  dependencies`'s multi-variant branch is untouched.
- **Model/plugin dependencies (3.3)**: model components `>=0.0.0` HARD,
  `dda.plugin.*` pinning — no change; this fix touches only LocalServer
  floor resolution.
- **Manifest schema (3.4)**: same fields, same types. Only VALUES for
  previously-missing archs change (`minLocalServerVersion` for JP7/x86
  packages); the embedded `minLocalServerVersions` map gains the three new
  keys additively (from the CDK env fix); jp4/jp5/jp6 values byte-identical.
- **Per-version override (3.5)**: `min_local_server_version` on a
  WorkflowVersions item still applies uniformly with the map bypassed — the
  caller keeps passing `by_arch={}`; `check_local_server_compatibility`'s
  signature and semantics are untouched (Decision 2 hardens the map
  DERIVATION, not the check).
- **Legacy recognition and the scalar chain (3.6)**:
  `local_server_component_arch` unchanged (bare `arm64`/`aarch64` →
  `arm64_jp4`); the write side still never emits the retired bare `.arm64`
  name; arch-undetermined devices still fall to the scalar fallback and are
  reported/blocked as today; `DDA_LOCAL_SERVER_VERSION` /
  `WORKFLOW_MIN_LOCAL_SERVER_VERSION` continue to exist as the last-resort
  default (map unconfigured/empty ⇒ scalar chain exactly as today) — not
  removed, not renamed.
- **Security preservation suite (3.7)**: the IAM CDK-synth guard compares
  the IAM statement multiset only — an env-var-only CDK change does not
  alter it; the existing LocalServer packaging preservation tests clear all
  floor env vars and assert the `1.0.0` scalar default, so they are
  environment-independent and pass unmodified. The `cdk.out` drift guard is
  respected OPERATIONALLY: the portal deploy is sequenced strictly outside
  any component build window, with `cdk.out` moved aside before the next
  build.
- **Non-workflow flows (3.8)**: LocalServer/model/plugin component publish
  and deploy paths untouched; nothing device-side changes.

**Scope:**
All inputs that do NOT involve floor resolution for a known-but-unmapped
arch are completely unaffected: every jp4/jp5/jp6 packaging and deploy,
every multi-arch packaging, every per-version-override deploy, every
non-workflow component flow, and — for test environments with no floor map
configured — the scalar default chain, byte-for-byte.

**Conscious exceptions (intended test updates, NOT silent regressions):**
two existing unit tests in
`edge-cv-portal/backend/tests/test_workflow_packaging_variant_min_version.py`
pin the DEFECTIVE fallback itself and must be updated by the fix, per
requirement 2.3 ("silence is the defect") and 3.6 ("no legitimate arch
consumer of the scalar floor remains"):
- `test_falls_back_to_scalar_for_unmapped_arch` asserts
  `min_local_server_version_for('x86_64') == '1.0.63'` under a configured
  map missing x86_64 — after hardening this returns `'1.0.0'`. The
  `min_local_server_version_for(None) == scalar` half of that test remains
  true and is kept.
- `test_arm64_jp4_falls_back_to_scalar_when_unmapped` asserts jp4 →
  `'1.0.63'` under a configured map missing jp4 — after hardening `'1.0.0'`.
These are updated in the same task as the packaging hardening, with comments
citing this design. No other existing test pins the defective path
(verified: the localserver-preservation and vllm-resolution-preservation
suites clear ALL floor env vars, so the map is empty there and the scalar
chain — which is preserved — is what they exercise).

## Hypothesized Root Cause

> Not a hypothesis: the investigation completed during requirements
> (bugfix.md Introduction). Section header kept per the bugfix design
> format. Stated for the record:

1. **Missing CDK map keys (primary)**: commit `2308311` introduced
   `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` with jp4/jp5/jp6 keys; the JP7
   fan-out (commit `c47f6ec`) extended `ARCH_TO_LOCAL_SERVER_COMPONENT` and
   `ARCH_TO_GG_PLATFORM` with `arm64_jp7` but not the CDK env map. x86_64 /
   x86_64_nvidia were never in the map at all.
2. **Silent scalar fallback (systemic)**: both consumers treat "arch not in
   map" identically to "no map configured" and substitute the cross-lineage
   scalar without any signal — `min_local_server_version_for`'s
   `return MIN_LOCAL_SERVER_VERSION` and the gate's
   `by_arch.get(arch, min_local_server_version)`. The map's own CDK comment
   documents the cross-lineage hazard, then the code walks into it for any
   unmapped arch.
3. **No coverage pin (systemic)**: nothing asserts the floor map covers the
   arch vocabulary. The two maps live in different languages
   (TypeScript literal / Python dict) with no test tying them together, so
   the omission was invisible until a field deployment failed.

## Design Decisions

### Decision 1 — Hardening semantics: safe per-lineage floor `'1.0.0'` + loud warning (NOT fail-closed)

Requirement 2.3 left the choice open: fail packaging closed vs. default to
`'1.0.0'` with a loud warning. **Decision: default to `'1.0.0'` with a loud
warning, in both consumers.**

**Rationale:**
- `'1.0.0'` is satisfiable in EVERY lineage (all variants version from
  1.0.x; workflow support ships in current field builds — the same reasoning
  behind every existing map entry). The 2.3 invariant — no cross-lineage
  constraint ever emitted — holds absolutely: the hardened path physically
  cannot produce an unsatisfiable constraint.
- Failure asymmetry: over-constraining bakes an unsatisfiable floor into an
  IMMUTABLE published component (unrecoverable; the incident). A `'1.0.0'`
  floor at worst under-constrains — deployable everywhere, recoverable, and
  identical to the value the map would have carried anyway under the
  jp4/jp5/jp6 convention.
- Fail-closed would convert a config omission into a packaging AND
  pre-submit outage for the affected arch, and would need awkward carve-outs
  in the gate (arch-undetermined devices must keep the scalar fallback per
  3.6 — a raise there would break legitimate paths).
- The runtime hardening is defense-in-depth, not the primary guard: the
  coverage test (Decision 3) fails the build the moment a future arch is
  added without a map key, so the hardened path should never execute in a
  correctly-tested deployment. When it does execute, the warning makes it
  visible in CloudWatch instead of silent.

The warning text names the arch, the missing-key condition, the substituted
floor, and the coverage test that should have caught it.

### Decision 2 — Hardening seams: inside `min_local_server_version_for` (packaging); at map DERIVATION in deployments (gate untouched)

**Packaging** (`workflow_packaging.py`): the guard goes inside
`min_local_server_version_for`:

```
IF arch in MIN_LOCAL_SERVER_VERSIONS:            return map entry   (unchanged)
IF map non-empty AND arch in ARCH_TO_LOCAL_SERVER_COMPONENT:
                                                 warn loudly; return '1.0.0'  (NEW)
RETURN scalar                                    (unchanged: empty map, None,
                                                  or unknown arch)
```

This hardens every packaging consumer at once (`local_server_component_
dependencies` and `build_manifest::minLocalServerVersion`) through the one
resolution function, stays monkeypatch-testable in the existing test style,
and leaves the scalar chain intact for the unconfigured case (3.6) and for
unknown archs (which `local_server_component_dependencies` already fails
closed on earlier, so the scalar branch is unreachable for them in the
recipe path anyway).

**Deployments** (`deployments.py`): `check_local_server_compatibility` is
NOT modified — its `by_arch.get(arch, fallback)` semantics are load-bearing
for the per-version override (3.5, caller passes `{}`) and the
arch-undetermined device path (3.6). Instead the hardening happens where the
module map is derived: `_parse_min_versions_map()`'s result is
completed — when non-empty, every arch id in the deployments arch vocabulary
(`LOCAL_SERVER_ARCH_IDS = ('arm64_jp4', 'arm64_jp5', 'arm64_jp6',
'arm64_jp7', 'x86_64')`, a new module constant mirroring
`local_server_component_arch`'s codomain) missing from the map is filled
with `'1.0.0'` and a loud warning. The gate then sees a complete map; the
override path and undetermined-arch fallback behave byte-identically to
today. `deployments.py` cannot import `workflow_packaging` (Lambda bundling
constraint documented in the module), so the constant is local and the
coverage test pins it in lockstep.

**Rejected alternative — normalize the packaging map at import too:** it
would keep `min_local_server_version_for` untouched and make the manifest's
embedded `minLocalServerVersions` complete even under misconfiguration, but
`ARCH_TO_LOCAL_SERVER_COMPONENT` is defined ~70 lines after the map parse
(reordering churn), and the existing packaging tests monkeypatch
`MIN_LOCAL_SERVER_VERSIONS` directly — import-time normalization is
invisible to them, making the hardening untestable in the established
style. In prod the embedded map is complete anyway once the CDK fix lands.

### Decision 3 — Coverage test placement: a backend Python test that parses the `compute-stack.ts` literal (NOT a jest/CDK-synth test)

**Investigated:** `edge-cv-portal/infrastructure` has jest 29.7 + ts-jest
(`npm test`) with six existing stack test files — but NONE instantiates
`ComputeStack` (it requires the full table/bucket/pool prop graph; no
harness exists), and a synth-based test could not see the two Python-side
maps anyway.

**Decision:** the requirement-2.4 coverage test is a permanent backend
pytest, `edge-cv-portal/backend/tests/test_workflow_min_localserver_floor_
coverage.py`, that:
1. Reads `edge-cv-portal/infrastructure/lib/compute-stack.ts` (path resolved
   relative to the repo tree), extracts the object literal inside
   `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS: JSON.stringify({ ... })`, and parses
   it into `{arch_id: version}` (tolerant of TS trailing commas/quotes;
   fails the test loudly if the anchor cannot be found — the anchor
   disappearing IS a coverage-relevant change someone must look at).
2. Asserts `set(literal.keys()) == set(ARCH_TO_LOCAL_SERVER_COMPONENT)` —
   the CDK env map and the packager's arch vocabulary in lockstep.
3. Asserts `set(deployments.LOCAL_SERVER_ARCH_IDS) ⊆ set(literal.keys())`
   and pins `LOCAL_SERVER_ARCH_IDS` against `local_server_component_arch`'s
   actual codomain (drive the real function with each
   `ARCH_TO_LOCAL_SERVER_COMPONENT` component name and the legacy
   `.arm64`/`.aarch64` names) — the three vocabularies cannot drift apart.
4. Asserts every literal value parses as `N.N.N` (a well-formed floor).

One test, one language, pins all three maps against the file that is
actually deployed. A jest snapshot could only ever see the TS side.

### Decision 4 — CDK values: `'1.0.0'` for all three new keys, per the jp4/jp5/jp6 convention

`arm64_jp7: '1.0.0'` (requirement 2.1: workflow support ships in current JP7
field builds; thor1's 1.0.5 satisfies it), `x86_64: '1.0.0'` and
`x86_64_nvidia: '1.0.0'` (requirement 2.2: both collapse to the `.amd64`
variant, latest 1.0.37, via the existing max-of-floors logic — equal floors,
so the collapse is trivially unchanged). The stale CDK comment ("archs
absent here fall back to DDA_LOCAL_SERVER_VERSION") is updated to state the
hardened contract: the map must cover every `ARCH_TO_LOCAL_SERVER_COMPONENT`
key, the coverage test enforces it, and missing keys resolve to the safe
`'1.0.0'` floor with a warning rather than the scalar.

`compute-stack.js` is a gitignored build artifact — only the `.ts` is
edited; `npx tsc --noEmit` (or `npm run build`) validates it compiles.

## Correctness Properties

Property 1: Bug Condition - Known Archs Resolve Per-Lineage Floors From The Deployed Map

_For any_ floor resolution where the bug condition holds (a known arch —
`arm64_jp7`, `x86_64`, `x86_64_nvidia` in the packager; `arm64_jp7`,
`x86_64` in the pre-submit gate — resolved against the actual
`compute-stack.ts` environment), the fixed tree SHALL resolve the arch's own
per-lineage floor `'1.0.0'` from an explicit map key: the CDK literal's key
set SHALL equal `ARCH_TO_LOCAL_SERVER_COMPONENT`'s key set;
`min_local_server_version_for` SHALL return `'1.0.0'`;
`local_server_component_dependencies([arch])` SHALL emit
`'>=1.0.0'` (satisfiable by arm64JP7 1.0.5 and amd64 1.0.37);
`build_manifest` SHALL record `minLocalServerVersion: '1.0.0'`; and the
pre-submit gate SHALL pass a JP7 device running 1.0.5 and an amd64 device
running 1.0.37.

**Validates: Requirements 2.1, 2.2, 2.4, 2.6**

Property 2: Preservation - Everything Outside The Missing-Key Resolutions Is Unchanged

_For any_ input where the bug condition does NOT hold — jp4/jp5/jp6
packaging and gating, multi-variant arch selections, model/plugin dependency
resolution, manifest schema, per-version overrides (uniform, map bypassed),
arch-undetermined devices, legacy bare `arm64`/`aarch64` component-name
recognition, and empty/unconfigured floor maps (scalar chain) — the fixed
tree SHALL produce the same result as the original tree, byte-identical
where output is compared (recipe entries, manifest fields for jp4/jp5/jp6,
gate decisions and reasons).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.8**

Property 3: Fix Checking - A Configured Map Never Silently Falls Back To The Scalar For A Known Arch

_For any_ non-empty floor map missing one or more known archs, and any
scalar value, the hardened resolution SHALL return the safe per-lineage
floor `'1.0.0'` (never the scalar) for every missing KNOWN arch, SHALL log a
warning naming the arch, SHALL leave mapped archs resolving their own
entries, SHALL leave unknown archs and `None` on the scalar chain, and the
deployments-side map completion SHALL fill exactly the missing
`LOCAL_SERVER_ARCH_IDS` keys with `'1.0.0'` while never overwriting present
entries — so no recipe constraint, manifest floor, or pre-submit minimum can
ever carry a cross-lineage value for a known arch.

**Validates: Requirements 2.3, 3.6**

Property 4: Fix Checking - The Coverage Test Pins All Three Vocabularies To The Deployed Literal

_For any_ state of the tree, the coverage test SHALL fail unless: the
`compute-stack.ts` `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` literal parses, its
key set equals `ARCH_TO_LOCAL_SERVER_COMPONENT`'s key set, it covers
`deployments.LOCAL_SERVER_ARCH_IDS`, `LOCAL_SERVER_ARCH_IDS` matches
`local_server_component_arch`'s observable codomain, and every value is a
well-formed version — so a future arch fan-out that omits the floor-map key
fails at test time instead of in the field.

**Validates: Requirements 2.4**

> Requirement 2.5 (re-package `421f8233` → new MAJOR version → deploy to
> jetson-thor1 successfully) is end-to-end acceptance on live AWS + the
> device; it is validated by the USER ACTION tasks, not by an automated
> property.

## Fix Implementation

### Changes Required

**File 1 — `edge-cv-portal/infrastructure/lib/compute-stack.ts` (~line 647)**

In `lambdaEnvironment.WORKFLOW_MIN_LOCAL_SERVER_VERSIONS`, extend the
`JSON.stringify({...})` literal:

```typescript
WORKFLOW_MIN_LOCAL_SERVER_VERSIONS: JSON.stringify({
  arm64_jp4: '1.0.0',
  arm64_jp5: '1.0.0',
  arm64_jp6: '1.0.0',
  arm64_jp7: '1.0.0',
  x86_64: '1.0.0',
  x86_64_nvidia: '1.0.0',
}),
```

and rewrite the preceding comment per Decision 4 (coverage contract +
hardened fallback; drop the "archs absent here fall back to
DDA_LOCAL_SERVER_VERSION" sentence). Validate with `npx tsc --noEmit` from
`edge-cv-portal/infrastructure`. NO other CDK change; `compute-stack.js` is
regenerated by the deploy tooling.

**File 2 — `edge-cv-portal/backend/functions/workflow_packaging.py`**

1. New module constant near the floor config (~line 150):
   `SAFE_LINEAGE_FLOOR = '1.0.0'` with a comment explaining satisfiability
   in every lineage.
2. Harden `min_local_server_version_for` (~line 178) per Decision 2:
   configured-but-missing KNOWN arch → `logging.warning(...)` naming the
   arch, the missing key, the substituted `SAFE_LINEAGE_FLOOR`, and the
   coverage test; return `SAFE_LINEAGE_FLOOR`. Empty map / `None` / unknown
   arch → scalar, unchanged. (`ARCH_TO_LOCAL_SERVER_COMPONENT` is defined
   below the function — module-level name resolution at CALL time makes the
   reference valid; no reordering needed.)
3. Update the module docstring/comment block (~lines 140–155): "archs absent
   from the map fall back to the scalar default" → the hardened contract.

**File 3 — `edge-cv-portal/backend/functions/deployments.py`**

1. New module constant near `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` (~line
   158): `LOCAL_SERVER_ARCH_IDS = ('arm64_jp4', 'arm64_jp5', 'arm64_jp6',
   'arm64_jp7', 'x86_64')` and `SAFE_LINEAGE_FLOOR = '1.0.0'`, with a
   comment stating it mirrors `local_server_component_arch`'s codomain and
   is pinned by the coverage test (cannot import `workflow_packaging`).
2. New helper `_fill_missing_arch_floors(by_arch)`: empty map → returned
   as-is (scalar chain preserved, 3.6); non-empty → a copy with every
   `LOCAL_SERVER_ARCH_IDS` key missing from it set to `SAFE_LINEAGE_FLOOR`,
   one loud `logger.warning` naming the filled archs.
3. Apply at module map derivation:
   `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS =
   _fill_missing_arch_floors(_parse_min_versions_map())`.
   `check_local_server_compatibility` and the ~line 3414 override logic are
   NOT modified.

**File 4 — `edge-cv-portal/backend/tests/test_workflow_packaging_variant_min_version.py`**

Update the two defect-pinning tests per the "Conscious exceptions" section:
`test_falls_back_to_scalar_for_unmapped_arch` (x86_64 under a configured map
→ now `'1.0.0'`; keep the `None` → scalar assertion) and
`test_arm64_jp4_falls_back_to_scalar_when_unmapped` (jp4 under a configured
map → now `'1.0.0'`; keep the mapped-jp6 assertion). Add comments citing
this design and requirement 2.3.

**File 5 — NEW `edge-cv-portal/backend/tests/test_workflow_min_localserver_floor_coverage.py`**

The permanent Decision 3 coverage test (Property 4).

**Files 6–7 — NEW test suites** (exploration
`test_jp7_localserver_floor_exploration.py`, preservation + hardening PBTs
`test_property_jp7_localserver_floor.py` — see Testing Strategy; the
preservation file name carries the `test_property_` prefix per the repo's
Hypothesis conventions).

**Explicit non-goals:** no change to `check_local_server_compatibility`, no
change to `local_server_component_dependencies` / `build_manifest` /
`next_component_version` bodies, no recipe/publish/device-side change, no
IAM change, no frontend change, no removal/rename of any env var.

## Testing Strategy

### Validation Approach

Two-phase per the bugfix methodology: first surface the counterexamples on
the UNFIXED tree with the REAL deployed environment (the parsed
`compute-stack.ts` literal — not a synthetic map), then verify the fix
resolves per-lineage floors and the hardening + coverage test close the
recurrence class, while the preservation suite holds jp4/jp5/jp6 and every
adjacent behavior byte-identical.

All backend suites run from `edge-cv-portal/backend` in the portal venv
(`/home/ubuntu/.venvs/dda-portal-tests`) WITH conftest (moto `aws_stack`
fixture where needed; Hypothesis profiles `portal-fast`/`ci` are
conftest-registered — do NOT hardcode `max_examples`):
`python3 -m pytest tests/<file> -q -p no:cacheprovider`.
Property tests carry `# Validates: Requirements X.Y` comments.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating the bug BEFORE the fix,
against the actual deployed configuration. Confirm the (already confirmed)
root cause mechanically.

**Test Plan**: `tests/test_jp7_localserver_floor_exploration.py` parses the
`WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` literal and the
`DDA_LOCAL_SERVER_VERSION` scalar out of `compute-stack.ts` (same extractor
the coverage test uses), loads them into the modules (monkeypatch
`packaging.MIN_LOCAL_SERVER_VERSIONS` / `MIN_LOCAL_SERVER_VERSION`, and the
deployments map/scalar), then asserts the EXPECTED (fixed) behavior. Run on
the UNFIXED tree → failures reproduce the incident numbers exactly.

**Test Cases**:
1. **Coverage of the literal**: literal keys ⊇
   `{arm64_jp7, x86_64, x86_64_nvidia}` (will fail on unfixed code — this is
   the CDK-fix pin; the hardening alone cannot make it pass)
2. **Packager floor (incident, defect 1.1)**:
   `min_local_server_version_for('arm64_jp7') == '1.0.0'` and
   `local_server_component_dependencies(['arm64_jp7'])` emits
   `aws.edgeml.dda.LocalServer.arm64JP7` with `'>=1.0.0'` (will fail:
   unfixed returns `'>=1.0.63'` — the cb139a40 constraint)
3. **Manifest floor (defect 1.2)**: `build_manifest(..., arch='arm64_jp7')`
   carries `minLocalServerVersion == '1.0.0'` (will fail: `'1.0.63'`)
4. **Pre-submit gate (defect 1.4), scoped PBT**: _for any_ installed JP7
   version in the real lineage range (1.0.0–1.0.5, Hypothesis-generated) a
   JP7 device passes `check_local_server_compatibility` with the prod map
   (will fail: unfixed effective min 1.0.63 rejects 1.0.5 with the exact
   observed reason string)
5. **Latent x86 (defect 1.5)**: same assertions for `x86_64` /
   `x86_64_nvidia` (floor `'1.0.0'`, `.amd64` constraint satisfiable by
   1.0.37; gate passes an amd64 device at 1.0.37) (will fail)
6. **Recurrence shape (defect 1.6)**: a hypothetical `arm64_jp8` added to a
   COPY of `ARCH_TO_LOCAL_SERVER_COMPONENT` with the prod map resolves a
   non-scalar floor (will fail on unfixed: silent `1.0.63`)

**Expected Counterexamples**:
- `min_local_server_version_for('arm64_jp7') == '1.0.63'`; recipe entry
  `>=1.0.63` vs thor1's `=1.0.5` — the deployment `cb139a40` conflict
- Gate reason: "Installed LocalServer version 1.0.5 is older than the
  required minimum 1.0.63"
- Literal key set `{arm64_jp4, arm64_jp5, arm64_jp6}` — three known archs
  uncovered

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the
fixed resolution produces the expected per-lineage behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition-class input (known arch, configured map) DO
  floor := resolve'(X)                      // fixed resolution
  ASSERT floor is per-lineage:
         X.arch IN keys(map') ⇒ floor = map'[X.arch]   // CDK fix: always, in prod
         X.arch NOT IN keys(map) ⇒ floor = '1.0.0' AND warning logged  // hardening
  ASSERT floor ≠ cross-lineage scalar
END FOR
```

Concretely: the exploration suite re-run green (tasks mirror it), plus
hardening PBTs (Property 3) in
`tests/test_property_jp7_localserver_floor.py`: _for any_
Hypothesis-generated non-empty floor map over arbitrary subsets of known
archs (plus junk keys), any scalar, and any known arch — the packaging
resolution and the deployments `_fill_missing_arch_floors` output never
yield the scalar for a known arch, warn on fills, keep mapped entries
verbatim, and keep `None`/unknown archs and empty maps on the scalar chain.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold,
the fixed functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

**Testing Approach**: Property-based testing for the resolution and gate
surfaces (many generated maps/archs/device versions catch edge cases manual
cases miss), plus observed-baseline example tests for the exact prod values,
plus re-running the existing suites that already pin adjacent behavior.

**Test Plan**: Observe on the UNFIXED tree first, then encode in
`tests/test_property_jp7_localserver_floor.py` (Property 2 section, PASS
required on unfixed):

**Test Cases**:
1. **jp4/jp5/jp6 identity**: with the prod literal loaded, floor/recipe
   entry/manifest field for each of jp4/jp5/jp6 equal the observed unfixed
   values (`'1.0.0'`, `'>=1.0.0'`) — and a PBT: _for any_ map containing the
   arch, resolution returns exactly the map entry (fixed == unfixed by
   construction on this branch)
2. **Scalar-chain identity (empty map)**: _for any_ scalar value and any
   arch (known, unknown, `None`), an EMPTY map resolves the scalar — the
   test-environment contract the existing preservation suites rely on
3. **Multi-variant omission (Defect F)**: _for any_ arch subset resolving to
   >1 distinct variant, `local_server_component_dependencies` returns `{}`
   (unchanged); the x86 pair still collapses to one `.amd64` entry with the
   max floor
4. **Override uniformity (3.5)**: gate called with `by_arch={}` and an
   override value gates every generated device against the override,
   regardless of variant
5. **Legacy recognition (3.6)**: `local_server_component_arch` over the full
   name vocabulary (JP-tagged, bare arm64/aarch64, amd64/x86, junk) —
   observed mapping pinned
6. **Manifest schema (3.4)**: `build_manifest` key set and types unchanged
   against the observed unfixed key set
7. **Existing-suite baselines**: record green runs (with counts) of
   `test_workflow_packaging_localserver_preservation.py`,
   `test_workflow_localserver_variant_compat.py`,
   `test_workflow_packaging_variant_min_version.py` (noting the two tests
   that will be consciously updated),
   `test_workflow_packaging_deployment_integration.py`

### Unit Tests

- Coverage test (Property 4 / requirement 2.4): literal extraction, key-set
  equality with `ARCH_TO_LOCAL_SERVER_COMPONENT`, `LOCAL_SERVER_ARCH_IDS`
  containment + codomain pin, value well-formedness
- Hardening warning content (arch named, `'1.0.0'` substituted)
- Updated `test_workflow_packaging_variant_min_version.py` cases (File 4)
- `npx tsc --noEmit` as the CDK compile check

### Property-Based Tests

- Exploration case 4 (gate passes JP7 devices across the real lineage range)
  — scoped to the bug condition
- Property 3 hardening PBTs (generated maps/scalars/archs; both consumers)
- Property 2 preservation PBTs (scalar-chain identity, map-entry identity,
  Defect F omission, override uniformity)
- All Hypothesis: `test_property_*.py` naming, conftest profiles (no
  hardcoded `max_examples`), `# Validates: Requirements X.Y` comments

### Integration Tests

- `test_workflow_packaging_deployment_integration.py` re-run green
  (packaging → manifest → gate wiring with the moto stack)
- Security guard pair + IAM synth preservation test re-run green host-side
  (3.7; env-only CDK change must not move the IAM multiset)
- End-to-end (USER ACTION, live AWS): portal deploy → re-package `421f8233`
  (auto MAJOR bump) → verify the new recipe carries `>=1.0.0` for
  `.arm64JP7` and manifest `minLocalServerVersion: 1.0.0` → deploy to
  jetson-thor1 → deployment SUCCEEDS (requirements 2.5, 2.6)

## Deployment and Sequencing (binding, per `.kiro/steering/builds.md`)

- A JP7 LocalServer component build (job `998b6f42`) is RUNNING. The portal
  deploy that ships this fix regenerates `cdk.out` and MUST NOT run while
  any component build is running (the build's security gate fails on the
  cdk.out drift guard after the ~1h compile). Sequence: JP7 build fully
  finishes → portal deploy (USER ACTION / orchestrator-sequenced) → move
  `cdk.out` aside → only then any next build.
- The re-package + thor1 re-deploy acceptance depends on the deployed portal
  (the Lambdas must be running with the new env), so it is sequenced after
  the portal deploy — also a USER ACTION.
- Nothing in this spec builds or deploys any Greengrass component itself;
  the workflow re-package publishes a new `dda.workflow.421f8233` component
  version through the existing portal flow.
