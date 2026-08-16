# Bugfix Requirements Document

## Introduction

Deploying workflow `dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8` v5.0.0 to the
JP7 device jetson-thor1 (deployment `cb139a40`, usecase `645504ce`) failed
`FAILED_NO_STATE_CHANGE` with an unsatisfiable version-constraint conflict on
`aws.edgeml.dda.LocalServer.arm64JP7`:

```
dda.workflow.421f8233 requires >=1.0.63,
model-rf-detr-seg-nano-jetson-xavier-jp7 requires >=1.0.0 <2.0.0,
thing/jetson-thor1 requires =1.0.5
```

Root cause (confirmed in code): the portal Lambda environment in
`edge-cv-portal/infrastructure/lib/compute-stack.ts` (~line 647) defines the
per-arch workflow LocalServer floor map
`WORKFLOW_MIN_LOCAL_SERVER_VERSIONS = {arm64_jp4: '1.0.0', arm64_jp5: '1.0.0',
arm64_jp6: '1.0.0'}` with NO `arm64_jp7` entry, alongside the scalar fallback
`DDA_LOCAL_SERVER_VERSION: '1.0.63'`. That scalar is a legacy-lineage number:
LocalServer variants are independently versioned and their lineages are NOT
comparable (arm64JP7 latest = 1.0.5, arm64JP6 = 1.0.59, arm64JP5 = 1.0.39,
amd64 = 1.0.37, bare legacy arm64 = 1.0.45 — the map's own comment, added with the
map in commit `2308311` for jp4/jp5/jp6, documents exactly this cross-lineage
hazard). `workflow_packaging.py::min_local_server_version_for(arch)` (~line 178)
silently falls back to the scalar for any arch absent from the map, so packaging a
JP7 workflow bakes `>=1.0.63` as a HARD `ComponentDependencies` entry into the
immutable Greengrass recipe (via `local_server_component_dependencies`, ~line 1457)
AND `minLocalServerVersion: 1.0.63` into the artifact `manifest.json` (~line 1665).
The JP7 fan-out (commit `c47f6ec`, jetpack7-support spec) added
`ARCH_ARM64_JP7 -> aws.edgeml.dda.LocalServer.arm64JP7` to
`ARCH_TO_LOCAL_SERVER_COMPONENT` but missed the CDK env map.

Second bite: `deployments.py`'s pre-submit compatibility gate
(`check_local_server_compatibility`, ~line 2472; error return at ~line 3411) reads
the same env map with the same scalar fallback, so portal-initiated workflow
deploys to JP7 devices are also blocked pre-submit (installed 1.0.5 < "required"
1.0.63). The x86 archs are LATENTLY broken the same way: `x86_64` /
`x86_64_nvidia` are present in `ARCH_TO_LOCAL_SERVER_COMPONENT` (both map to the
`.amd64` variant, latest 1.0.37) but absent from the floor map, so an x86 workflow
package would bake the same unsatisfiable `>=1.0.63`. JP5/JP6 are verified
unaffected (map entries exist at '1.0.0'). Multi-arch workflows are unaffected by
construction — packaging for multiple distinct LocalServer variants omits the
LocalServer dependency entirely (edge-deploy-reliability Defect F).

The systemic defect is the silent cross-lineage scalar fallback itself: the exact
same failure will recur on JP8 (and any future arch) unless an arch known to
`ARCH_TO_LOCAL_SERVER_COMPONENT` but missing from the floor map stops silently
inheriting the scalar, and a build-time coverage test pins the two maps together.

Scope guardrails: this fix is CLOUD-SIDE ONLY — the CDK Lambda environment
(`compute-stack.ts`; `compute-stack.js` is a gitignored build artifact regenerated
from it), backend fallback hardening in `workflow_packaging.py` /
`deployments.py`, and tests. NO LocalServer component build and NO device-side
change is required. Shipping it requires a portal deploy
(deploy-portal/deploy-infrastructure), which per `.kiro/steering/builds.md` MUST
NOT run while a component build is running (a `csi-nvargus-optional` JP7 fleet
build may be queued soon — sequence: portal deploy fully finishes, move `cdk.out`
aside, then start the build). Already-published workflow component versions are
immutable and cannot be repaired in place; the recovery path is re-packaging
(which auto-bumps the component MAJOR to the next free `N.0.0`,
`next_component_version`). End-to-end verification = re-package workflow
`421f8233` after the fix and deploy it to jetson-thor1 successfully.

## Bug Analysis

### Current Behavior (Defect)

The floor map is missing entries for archs that the packager and the pre-submit
gate both know about, and both silently substitute the incomparable legacy-lineage
scalar:

1.1 WHEN a workflow is packaged for `arm64_jp7` THEN
`min_local_server_version_for('arm64_jp7')` finds no `arm64_jp7` key in
`WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` and silently returns the scalar `1.0.63`, and
the packager bakes a HARD `aws.edgeml.dda.LocalServer.arm64JP7 >= 1.0.63`
`ComponentDependencies` entry into the immutable Greengrass recipe — unsatisfiable
forever, since the arm64JP7 lineage is at 1.0.5 (incident package:
`dda.workflow.421f8233` v5.0.0)

1.2 WHEN that same packaging runs THEN the artifact `manifest.json` records
`minLocalServerVersion: 1.0.63` for the JP7 package (and the
`minLocalServerVersions` map it embeds carries no `arm64_jp7` key), so the
on-device compatibility surface also sees the cross-lineage number

1.3 WHEN the resulting component is deployed to a JP7 device THEN Greengrass
fails the deployment `FAILED_NO_STATE_CHANGE` with an unresolvable constraint
conflict (`>=1.0.63` vs `thing/jetson-thor1 requires =1.0.5` vs the model
component's `>=1.0.0 <2.0.0`) — observed on deployment `cb139a40`

1.4 WHEN a portal workflow deployment to a JP7 device is submitted THEN the
pre-submit gate (`deployments.py::check_local_server_compatibility`) resolves the
same missing-key fallback (`by_arch.get('arm64_jp7', 1.0.63)`) and rejects the
request before submission with an incompatible-devices error ("Installed
LocalServer version 1.0.5 is older than the required minimum 1.0.63") — the
second bite of the same defect

1.5 WHEN a workflow is packaged for `x86_64` or `x86_64_nvidia` THEN the identical
missing-key fallback bakes `aws.edgeml.dda.LocalServer.amd64 >= 1.0.63` (amd64
lineage latest = 1.0.37) into the recipe and manifest, and the pre-submit gate
blocks amd64 devices the same way — latently broken today, undetected only because
no x86 workflow deploy has been attempted

1.6 WHEN any FUTURE arch (e.g. JP8) is added to `ARCH_TO_LOCAL_SERVER_COMPONENT`
without a matching floor-map entry THEN the system silently emits a
cross-lineage constraint again — nothing fails loudly at packaging time, no test
pins the floor map's coverage against `ARCH_TO_LOCAL_SERVER_COMPONENT`, and the
defect is only discovered when a field deployment fails

1.7 WHEN a workflow version has already been packaged with the bad constraint
(e.g. `dda.workflow.421f8233` v5.0.0) THEN the published Greengrass component
version is immutable and carries `>=1.0.63` forever — fixing the environment alone
does not repair existing packages

### Expected Behavior (Correct)

2.1 WHEN a workflow is packaged for `arm64_jp7` THEN the system SHALL resolve the
JP7 floor from a per-lineage `arm64_jp7` floor-map entry (`'1.0.0'`, per the
jp4/jp5/jp6 convention — workflow support ships in current JP7 field builds) and
SHALL emit a satisfiable recipe constraint
(`aws.edgeml.dda.LocalServer.arm64JP7 >= 1.0.0`) and matching
`manifest.json` `minLocalServerVersion` value

2.2 WHEN a workflow is packaged for `x86_64` or `x86_64_nvidia` THEN the system
SHALL resolve those archs' floors from their own floor-map entries (`'1.0.0'`)
and SHALL emit a satisfiable `aws.edgeml.dda.LocalServer.amd64` constraint (when
both x86 flavors are selected they collapse to the one amd64 variant with the max
of their floors, as today)

2.3 WHEN an arch is present in `ARCH_TO_LOCAL_SERVER_COMPONENT` but absent from
the configured floor map THEN the system SHALL NOT silently inherit the
cross-lineage scalar — the invariant is that NO cross-lineage version constraint
may ever be emitted into a recipe, manifest, or pre-submit decision (whether the
hardened path fails packaging closed or defaults to `'1.0.0'` with a loud warning
is decided in design; silence is the defect)

2.4 WHEN the backend test suite runs THEN a coverage test SHALL pin that every
key of `ARCH_TO_LOCAL_SERVER_COMPONENT` has an explicit floor-map entry in the
deployed environment configuration (CDK env map and backend expectations kept in
lockstep), so a future JP8 fan-out that repeats the omission fails at build/test
time instead of in the field

2.5 WHEN an affected already-packaged workflow (e.g. `421f8233` v5.0.0) is
re-packaged after the fix THEN the packager SHALL produce a new component version
(auto MAJOR bump to the next free `N.0.0` via `next_component_version` — verified
present) whose recipe and manifest carry the corrected per-lineage floor, and that
new version SHALL be deployable to a JP7 device (acceptance: re-package `421f8233`
and deploy to jetson-thor1 successfully)

2.6 WHEN a portal workflow deployment targets a JP7 (or amd64) device THEN the
pre-submit gate SHALL resolve the same corrected per-arch floors (it reads the
same env map) and SHALL pass a JP7 device running arm64JP7 1.0.5 (and an amd64
device running 1.0.37) instead of rejecting on the cross-lineage scalar

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a workflow is packaged for `arm64_jp4`, `arm64_jp5`, or `arm64_jp6` THEN
the system SHALL CONTINUE TO emit byte-identical LocalServer constraints
(`>= 1.0.0` per the existing map entries) in both the recipe
`ComponentDependencies` and `manifest.json` `minLocalServerVersion`, and JP5/JP6
portal deploys SHALL CONTINUE TO pass the pre-submit gate exactly as today

3.2 WHEN a workflow is packaged for multiple architectures that resolve to more
than one distinct LocalServer variant THEN the system SHALL CONTINUE TO omit the
LocalServer `ComponentDependencies` entry entirely with the existing warning
(edge-deploy-reliability Defect F: a recipe-global multi-variant closure is
undeployable) — multi-arch workflows have no LocalServer dependency by design and
are unaffected by this fix

3.3 WHEN a workflow package resolves model and plugin dependencies THEN the
system SHALL CONTINUE TO emit them unchanged (model components `>=0.0.0` HARD,
`dda.plugin.*` pinning) — this fix touches only the LocalServer floor resolution

3.4 WHEN `manifest.json` is generated THEN its schema SHALL CONTINUE TO be
unchanged — same fields, same types; only the resolved VALUES for
previously-missing archs are corrected (`minLocalServerVersion` for JP7/x86
packages, and the embedded `minLocalServerVersions` map gains the new per-arch
keys additively; existing jp4/jp5/jp6 values are byte-identical)

3.5 WHEN a WorkflowVersions item carries a per-version `min_local_server_version`
override THEN the deployment gate SHALL CONTINUE TO apply that override uniformly
to every target device with the per-arch map bypassed, exactly as today

3.6 WHEN the pre-submit gate reads an installed LocalServer component name THEN
the read-side legacy recognition SHALL CONTINUE TO work unchanged: bare
`arm64`/`aarch64` suffixes map to `arm64_jp4` (whose floor entry exists), the
write side SHALL CONTINUE TO never emit the retired bare `.arm64` name
(verified: no arch in `ARCH_TO_LOCAL_SERVER_COMPONENT` maps to it), and a device
whose variant cannot be determined SHALL CONTINUE TO be reported/blocked exactly
as today; the scalar `DDA_LOCAL_SERVER_VERSION` env and its
`WORKFLOW_MIN_LOCAL_SERVER_VERSION` override SHALL CONTINUE TO exist as the
last-resort default in the resolution chain (no legitimate arch consumer of the
scalar floor remains — every mapped arch gets a per-lineage entry — but the
fallback chain and env contract are not removed or renamed)

3.7 WHEN the security preservation suite runs THEN it SHALL CONTINUE TO pass:
the IAM CDK-synth guard (`test_preservation_iam_cdk_synth.py`) compares the IAM
statement multiset only, which an env-var-only change does not alter (verified in
the test's design); the existing LocalServer packaging preservation tests
(`test_workflow_packaging_localserver_preservation.py` etc.) clear all floor env
vars and assert against the `1.0.0` default, so they are environment-independent
and SHALL CONTINUE TO pass unmodified; and the `cdk.out` drift guard SHALL be
respected operationally — the portal deploy that ships this fix regenerates
`cdk.out`, so per `.kiro/steering/builds.md` it MUST be sequenced strictly outside
any component build window and `cdk.out` moved aside (or rebaselined) before the
next build

3.8 WHEN devices deploy non-workflow components (LocalServer itself, model
components, plugins) THEN those flows SHALL CONTINUE TO behave exactly as today —
this fix changes no component recipes, no publish paths, and nothing device-side
