# Task 1 — Live read-only evidence

Account **164152369890**, region **us-east-1**, caller
`arn:aws:sts::164152369890:assumed-role/admin-iam/i-06378d9cfa0907e3c`
(confirmed with `aws sts get-caller-identity`). Gathered 2026-09-10 via boto3
1.43.83 under `~/.dda-test-venv/bin/python` (the `aws ssm` CLI subcommand is
broken on this host; every call below is a `greengrassv2` read).

**Calls used, and only these:** `list-components`, `describe-component`,
`get-component --recipe-output-format JSON`, `list-component-versions`,
`list-core-devices`, `get-core-device`, `list-installed-components`,
`list-deployments`, `get-deployment`. No create/update/delete, no deployment
submitted, no device state changed. `list-core-devices` is additive to the set
named in tasks.md and is read-only (it enumerates the devices the other calls
are then made against).

Verdict summary:

| Subtask | Question | Verdict |
|---|---|---|
| 1.1 | Does `DescribeComponent.platforms[].attributes` reliably carry `variant`? | **SETTLED.** It carries exactly what the recipe carries — 187/187 agreement. But the resolver reads platforms from the **recipe**, because `describe-component` cannot supply `ComponentDependencies` and would be a second redundant call per node. |
| 1.2 | Is the root-vs-dependency split readable from the cloud pre-submit? | **SETTLED — partially yes.** `ListInstalledComponents` exposes it (`isRoot` field + `topologyFilter=ALL\|ROOT`). It describes the **previous** deployment's outcome, not the submission being validated, so it can corroborate but cannot decide. `GetDeployment.components` does NOT distinguish root. |
| 1.3 | How is the target device's platform reported? | **SETTLED.** `get-core-device` returns `platform`/`architecture`/`runtime`/`coreVersion` and **no `variant`**. JP5, JP6 and JP7 devices are byte-identical. The only authoritative variant source is the portal's own `DEVICES_TABLE.target_architecture`; absent ⇒ UNVERIFIED. |

Nothing is UNSETTLED. Four items could not be verified within the allowed
read-only set and are recorded as such in §4. Nine findings contradict or
qualify bugfix.md as written (§5).

---

## 1.1 — `DescribeComponent.platforms` vs the recipe's `Manifests[].Platform`

### Method

Every one of the 181 PRIVATE components' latest versions plus 7 AWS-managed
public components were fetched both ways and compared as **sets of attribute
maps** (key order is not semantic). 188 targets, 187 compared — one public
version turned out not to exist, see §4.2:

```
$ aws greengrassv2 describe-component --arn <arn>            # -> platforms[].attributes
$ aws greengrassv2 get-component --arn <arn> \
    --recipe-output-format JSON                              # -> Manifests[].Platform
```

### Result

```
components compared:            187
describe == recipe (as sets):   187
DISAGREEMENTS:                  0
describe_component with NO 'platforms' key: 0
recipe manifests with NO 'Platform' block:  2   (testmodel v1.0.0, alienmodel v1.0.0)
attribute keys seen across all recipe manifests: {'os': 228, 'architecture': 205, 'variant': 41, 'runtime': 4}
literal '*' attribute values seen: {'os=*': 4}
aarch64 manifests WITH variant:    41
aarch64 manifests WITHOUT variant: 107
```

**`describe-component` is reliable: it mirrors the recipe exactly**, including
the presence *and* the absence of `variant`. `components.py:127-158`'s
assumption holds for every component class present in the account, not just
`dda.plugin.*`.

### Per-class samples (verbatim, trimmed)

**Multi-arm workflow — Counterexample A, CONFIRMED first-hand.**
`dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe` v1.0.0:

```
describe-component platforms:
  [{"attributes": {"architecture": "aarch64", "os": "linux", "variant": "arm64_jp5"}},
   {"attributes": {"architecture": "aarch64", "os": "linux", "variant": "arm64_jp6"}}]
get-component Manifests[].Platform:
  [{"os": "linux", "variant": "arm64_jp5", "architecture": "aarch64"},
   {"os": "linux", "variant": "arm64_jp6", "architecture": "aarch64"}]
manifest_count: 2   ComponentDependencies: null
status: {"componentState": "DEPLOYABLE", "vendorGuidance": "ACTIVE"}
```

bugfix.md's claim that this version publishes **only** `arm64_jp5` and
`arm64_jp6` is confirmed. Two manifests, both variant-bearing, neither
`arm64_jp7`.

**Single-arm workflow — variant-less, as predicted by
`workflow_packaging.py:2135-2148`.** `dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8`
v12.0.0 (the Counterexample C depender):

```
describe-component platforms:   [{"attributes": {"architecture": "aarch64", "os": "linux"}}]
get-component Manifests[].Platform: [{"os": "linux", "architecture": "aarch64"}]
ComponentDependencies:
  {"model-vllm-qwen3-5-9b-jetson-xavier-jp7": {"VersionRequirement": ">=0.0.0",
                                               "DependencyType": "HARD"},
   "aws.edgeml.dda.LocalServer.arm64JP7":     {"VersionRequirement": ">=1.0.0",
                                               "DependencyType": "HARD"}}
```

The Counterexample C recipe content bugfix.md lists as
"not verifiable from this repo" is **confirmed verbatim**, including the
unpinned `>=0.0.0` HARD entry.

**A multi-arm workflow that DOES include jp7** (the positive control the
platform validator must not reject): `dda.workflow.bdfabc2a-d246-466f-a4ca-53bb40c9e119`
v8.0.0 → `variant: arm64_jp6` and `variant: arm64_jp7`. It is currently
installed and `FINISHED` on `jetson-thor1`.

**`model-*-jp7` — variant-less, matching `greengrass_publish.py:323`/`:654`.**
`model-yolo-test-jetson-xavier-jp7` v8.0.0:

```
both views: [{"os": "linux", "architecture": "aarch64"}]
ComponentDependencies: {"aws.edgeml.dda.LocalServer.arm64JP7":
                        {"VersionRequirement": ">=1.0.0 <2.0.0", "DependencyType": "HARD"}}
```

Same shape for `model-vllm-qwen3-5-9b-jetson-xavier-jp7` v1.0.0 and for the two
Counterexample B legacy models.

**`dda.plugin.*`.** `dda.plugin.7878501d-389d-4db5-832c-6ee2429fecb9` v3.0.0 —
three manifests, `variant` on the two aarch64 arms and absent on amd64; both
views identical:

```
[{"architecture": "aarch64", "os": "linux", "variant": "arm64_jp5"},
 {"architecture": "aarch64", "os": "linux", "variant": "arm64_jp6"},
 {"architecture": "amd64",   "os": "linux"}]
```

**LocalServer variants — all THREE are variant-less.**
`aws.edgeml.dda.LocalServer.arm64JP5` v1.0.44, `…arm64JP6` v1.0.67 and
`…arm64JP7` v1.0.26 each publish exactly
`[{"os": "linux", "architecture": "aarch64"}]` in both views. JetPack is
encoded in the component **name**, never in the manifest.

**AWS-managed public components.**

```
aws.greengrass.Nucleus 2.14.3     -> [{"os": "linux"}, {"os": "darwin"}, {"os": "windows"}]
aws.greengrass.Cli 2.14.3         -> [{"os": "linux"}, {"os": "darwin"}, {"os": "windows"}]
aws.greengrass.ShadowManager 2.3.9 -> [{"os": "*"}]
aws.greengrass.LogManager 2.3.10  -> [{"os": "*"}]
```

**Degenerate shape.** `testmodel` v1.0.0 and `alienmodel` v1.0.0 have a
manifest with no `Platform` block at all: the recipe reports `Platform: null`
and `describe-component` reports `{"attributes": {}}` — an unconstrained
manifest that matches every device.

### Verdict and the decision it forces

`describe-component` **is** reliable, so 1.1's stated failure mode does not
arise. The resolver nevertheless **reads platforms from the recipe and does not
call `describe-component` at all**, for a different and stronger reason:

`DescribeComponent`'s output shape is
`[arn, componentName, componentVersion, creationTimestamp, description,
platforms, publisher, status, tags]` — it carries **no
`ComponentDependencies`**. The closure walk must call
`get_component(recipeOutputFormat='JSON')` for every node anyway to get the
dependency edges, and that same response already carries
`Manifests[].Platform`. Calling `describe-component` would double the API
calls per node for information the resolver already holds, and would introduce
a second source that could skew. One memoized `get_component` per
`(name, version)` — the generalization of the existing read at
`deployments.py:437-450` — supplies both edges and platforms.

**Three matcher requirements this evidence adds, none of which bugfix.md states
as written** (see §5):

1. An **absent** attribute key is a wildcard, not just `variant`. Nucleus
   publishes `{"os": "linux"}` with no `architecture`; that manifest must match
   an aarch64 device. bugfix.md 2.8 only promises this for `variant`.
2. A **literal `"*"`** attribute value is a wildcard. `ShadowManager` and
   `LogManager` publish `{"os": "*"}`.
3. An **empty / null** `Platform` matches everything (`testmodel`,
   `alienmodel`).

Only these hold for the account's actual data; anything stricter would report
`aws.greengrass.Nucleus` and `aws.greengrass.ShadowManager` — auto-included on
every portal deployment — as platform-incompatible with every device and block
every submit.

Mapping is 1:1 and needs no translation: `plugin_components.platform_for`
(`plugin_components.py:213-220`) writes `platform['variant'] = arch` verbatim,
so the DDA `Target_Architecture` string **is** the manifest `variant` value
(`arm64_jp5` ⇒ `variant: arm64_jp5`). The 41 variant-bearing manifests observed
use exactly `arm64_jp4` / `arm64_jp5` / `arm64_jp6` / `arm64_jp7`.

### Counterexample A's submitted document, confirmed first-hand

```
$ aws greengrassv2 get-deployment --deployment-id 1982ad02-c3eb-4803-8b19-2b41f28bc391
  revisionId 6, targetArn arn:aws:iot:us-east-1:164152369890:thing/adlink-dlap-701
  created 2026-09-01T19:46:36.602Z, deploymentName ssh-tunnel-on-adlink-dlap-701
  deploymentPolicies: {componentUpdatePolicy: {NOTIFY_COMPONENTS, 60s},
                       failureHandlingPolicy: ROLLBACK}
  tags: {dda-portal:managed: true, dda-portal:usecase-id: 645504ce-…,
         dda-portal:workflow-id: 596c8577-…, dda-portal:workflow-version: 14}
  components (15) include:
    dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe  1.0.0   <- A offender
    model-cookies-segmentation-seghead-jetson-xavier   2.0.0   <- B offender
    model-yolo-test-jetson-xavier                      2.0.0   <- B offender
```

A JP7 device was sent a jp5/jp6-only workflow. `failureHandlingPolicy:
ROLLBACK` matches bugfix.md's blast-radius claim.

### Counterexample B, confirmed first-hand

`list-component-versions` in **both** namespaces:

```
aws.edgeml.dda.LocalServer.arm64JP4  account: []  aws: []      <- zero versions
aws.edgeml.dda.LocalServer.arm64     account: []  aws: []      <- zero versions
aws.edgeml.dda.LocalServer.arm64JP7  account: 27 versions (1.0.0 .. 1.0.26)  aws: []
aws.greengrass.Nucleus               account: []  aws: 53 versions
aws.greengrass.ShadowManager         account: []  aws: 30 versions
aws.greengrass.LogManager            account: []  aws: 29 versions
aws.greengrass.Cli                   account: []  aws: 54 versions
aws.greengrass.SecureTunneling       account: []  aws: 26 versions
aws.greengrass.TokenExchangeService  account: []  aws: 1 version  (2.0.3)
aws.greengrass.DockerApplicationManager account: []  aws: 18 versions
```

and the recipes that point at the two dead names:

```
model-cookies-segmentation-seghead-jetson-xavier v2.0.0 ComponentDependencies:
  {"aws.edgeml.dda.LocalServer.arm64JP4": {">=1.0.0 <2.0.0", HARD}}
model-yolo-test-jetson-xavier v2.0.0 ComponentDependencies:
  {"aws.edgeml.dda.LocalServer.arm64":    {">=1.0.0 <2.0.0", HARD}}
```

Revision 9's real id is **`4c0f8f84-32e5-4300-86ee-4ab557f6ec83`** (bugfix.md
abbreviates it `4c0f8f84-32e5…`; the id I first guessed from that prefix did not
exist). Its document contains both B offenders and **not** the A workflow —
exactly the "after the offending workflow was removed" narrative. The full
`adlink-dlap-701` history (17 revisions) shows the offenders entering at
revision 3–4 and leaving at revision 10, with revision 17 (`27299c2e-…`,
`dlap701-jp7-1.0.18-post-swap-reload`) the current `COMPLETED` one.

**Hard constraint this puts on the resolvability validator:**
`list-component-versions` against the *wrong* namespace returns an **empty
list, not an error**. `aws.greengrass.Nucleus` under the account ARN returns
`[]` with no exception. The resolver therefore cannot distinguish "wrong
namespace" from "no published versions" by exception handling; it must query
**both** namespaces and treat either non-empty result as resolved. The
tasks.md Notes' dual-namespace guard is confirmed necessary and its failure
mode is silent.

---

## 1.2 — Is the root-vs-dependency split readable from the cloud pre-submit?

### `ListInstalledComponents` exposes it — two ways

The API shape (from the botocore service model) is:

```
ListInstalledComponents INPUT : [coreDeviceThingName, maxResults, nextToken, topologyFilter]
                                 topologyFilter enum = ['ALL', 'ROOT']
installedComponents[] members : [componentName, componentVersion, isRoot,
                                 lastInstallationSource, lastReportedTimestamp,
                                 lastStatusChangeTimestamp, lifecycleState,
                                 lifecycleStateDetails, lifecycleStatusCodes]
```

Live, on `jetson-thor1`:

```
$ aws greengrassv2 list-installed-components --core-device-thing-name jetson-thor1 --topology-filter ALL
  count=21
$ aws greengrassv2 list-installed-components --core-device-thing-name jetson-thor1 --topology-filter ROOT
  count=14
  INSTALLED-BUT-NOT-ROOT (dependency-only): ['DeploymentService', 'FleetStatusService',
    'TelemetryAgent', 'UpdateSystemPolicyService', 'aws.greengrass.Cli',
    'aws.greengrass.DockerApplicationManager', 'aws.greengrass.TokenExchangeService']
  ROOT-BUT-NOT-IN-ALL: []
```

and on `adlink-dlap-701`: ALL=19, ROOT=12, the same seven dependency-only
entries. One verbatim entry of each kind:

```
{"componentName": "model-yolo-test-jetson-xavier-jp7", "componentVersion": "8.0.0",
 "isRoot": true, "lastInstallationSource": "0f9e72bd-4e39-48d0-8403-14bea51abc7d",
 "lastReportedTimestamp": "2026-09-10 00:05:55.646000+00:00",
 "lastStatusChangeTimestamp": "2026-09-09 00:30:56.972000+00:00",
 "lifecycleState": "RUNNING", "lifecycleStatusCodes": []}

{"componentName": "aws.greengrass.Cli", "componentVersion": "2.18.3",
 "isRoot": false, "lastInstallationSource": "0f9e72bd-4e39-48d0-8403-14bea51abc7d",
 "lastReportedTimestamp": "2026-09-10 00:05:55.646000+00:00",
 "lastStatusChangeTimestamp": "2026-09-09 00:30:56.972000+00:00",
 "lifecycleState": "RUNNING", "lifecycleStatusCodes": []}
```

`lifecycleStateDetails` is absent when empty. `lastInstallationSource` is the
deployment id that installed the component — a usable attribution field.

**The default `topologyFilter` is `ROOT`.** Called without the parameter,
`jetson-thor1` returns 14 entries, all `isRoot: true`; `adlink-dlap-701`
returns 12. The portal's existing call at `devices.py:290` passes no
`topologyFilter`, so it has only ever seen the ROOT view and the
dependency-only set is invisible to it today. Seeing the split requires
explicitly asking for `ALL`.

### `GetDeployment` does NOT expose it

```
ComponentDeploymentSpecification keys: ['componentVersion', 'configurationUpdate', 'runWith']
UNION of component entry keys observed: ['componentVersion', 'configurationUpdate']
any 'isRoot'-like key? []
```

Every entry in a deployment document **is** a root by construction, so the
document cannot distinguish them. `jetson-thor1`'s current revision 86
(`0f9e72bd-…`, `COMPLETED`) lists 14 components; the device reports 21
installed. The 7-component delta is the dependency closure Greengrass resolved,
and it appears in no deployment document.

`ListDeployments --history-filter LATEST_ONLY` returns
`[creationTimestamp, deploymentId, deploymentName, deploymentStatus,
isLatestForTarget, revisionId, targetArn]` — no component list, no root
information. `GetCoreDevice` carries no component information at all.

### Verdict and the decision it forces

**The split is readable, but it answers the wrong question.**
`ListInstalledComponents(topologyFilter=ALL)` reports what the device last
told Fleet Status about the **outcome of the previous deployment**. It is a
device-reported, staleness-prone observation (`lastReportedTimestamp`, here up
to a day old), and it says nothing about the submission being validated. The
question 2.11 asks — *will this de-selection be a no-op?* — is about a
deployment that does not exist yet, and only the recipe
`ComponentDependencies` of the components in the **submitted** set can answer
it.

So for task 4.4:

- **Source of truth: the recipe closure** the resolver already fetches for
  2.1. The de-selected-but-still-required finding is derived from
  `ComponentDependencies` edges of the *selected* components against the
  set-difference versus the target's previous deployment
  (`GetDeployment.components` on the latest revision). This is not a silent
  assumption any more — the cloud has no pre-submit alternative.
- **`ListInstalledComponents(ALL)` is available as optional corroboration**
  and nothing more. It can enrich a finding ("this component is currently
  installed on the device as a non-root dependency, `isRoot: false`,
  `lifecycleState: RUNNING`, installed by deployment X") but it must never
  gate: a stale or unavailable report must not change the classification, per
  2.9. It also cannot be required, because it does not exist for a device
  that has never reported.
- Nothing in either API distinguishes a HARD from a SOFT dependency edge —
  that too comes only from the recipe.

### Counterexample C's cloud-side half, confirmed first-hand

`jetson-thor1` has 86 revisions (1..86). Walking every one and asking whether
`model-vllm-qwen3-5-9b-jetson-xavier-jp7` (M) and
`dda.workflow.421f8233-f1d9-495a-b7b2-f26b1d24d0d8` (W) are in the document:

```
rev | created                          | status   | name                              | M | W(version)
 18 | 2026-08-26 19:50:28.035Z         | INACTIVE | ssh-tunnel-on-jetson-thor1        | Y | 12.0.0
 19 | 2026-08-26 20:38:47.545Z         | INACTIVE | ssh-tunnel-on-jetson-thor1        | Y | 12.0.0
 20 | 2026-08-26 20:38:50.673Z         | INACTIVE | ssh-tunnel-on-jetson-thor1        | Y | 12.0.0
 21 | 2026-08-26 20:39:36.933Z         | INACTIVE | ssh-tunnel-on-jetson-thor1        | - | 12.0.0   <- removal
 22 | 2026-08-26 20:39:39.201Z         | INACTIVE | ssh-tunnel-on-jetson-thor1        | - | 12.0.0
 …  (23-33 all M=-, W=12.0.0)
 34 | 2026-08-27 03:24:40.522Z         | INACTIVE | yolo-world-blue-plate-jp7-thor1   | - | -        <- W absent
 35 | 2026-08-27 03:39:09.186Z         | INACTIVE | thor1-localserver-1.0.12-merged   | - | 12.0.0
 36 | 2026-08-27 04:15:56.902Z         | INACTIVE | yolo-world-blue-plate-v2-jp7-thor1| - | -        <- W absent
 37 | 2026-08-27 04:27:17.287Z         | INACTIVE | yolo-world-blue-plate-v3-thor1    | - | -        <- W absent
 38 | 2026-08-27 17:41:31.520Z         | INACTIVE | ssh-tunnel-on-jetson-thor1        | - | -        <- W absent
 39 | 2026-08-30 13:54:33.338Z         | INACTIVE | thor1-localserver-1.0.14-…        | - | 12.0.0
 …  (40-52 all M=-, W=12.0.0)
 53 | 2026-09-01 16:15:32.170Z         | INACTIVE | thor1-remove-qwen-folder-test     | - | -        <- both removed
 54..86  M=-, W=-   (86 = COMPLETED, current)
```

Revision 21's id is **`5fa4482b-c4d5-4f6b-b35e-3adc9ef6c585`**, created
**2026-08-26T20:39:36.933Z** — the exact timestamp bugfix.md states. Its
document drops M while keeping W v12.0.0, whose published recipe HARD-requires
M at `>=0.0.0` (§1.1 above). Revision 53's id is
**`a9086c7d-ae9e-4131-8e83-7efc4fd549b4`** (one of tasks.md 3.1's two
reference deployments), created **2026-09-01T16:15:32.170Z**, and it removes
both.

The mechanism is also observable in the present tense, independent of the
incident: `aws.greengrass.Cli`, `aws.greengrass.DockerApplicationManager` and
`aws.greengrass.TokenExchangeService` are installed and `RUNNING` on both JP7
devices with `isRoot: false` and appear in **no** deployment document. Greengrass
resolves and keeps non-root dependencies, and reports the fact.

M is no longer installed on `jetson-thor1` today, so the retention itself
cannot be re-observed live — it is historical.

---

## 1.3 — How the target device's platform is actually reported

### `get-core-device`, live, for JP5 / JP6 / JP7

```
$ aws greengrassv2 get-core-device --core-device-thing-name jetson-thor1          # JP7
{"coreDeviceThingName": "jetson-thor1", "coreVersion": "2.12.0",
 "platform": "linux", "architecture": "aarch64", "runtime": "aws_nucleus_classic",
 "status": "HEALTHY", "lastStatusUpdateTimestamp": "2026-09-10 00:05:55.646000+00:00",
 "tags": {"dda-portal:managed": "true"}}

$ aws greengrassv2 get-core-device --core-device-thing-name adlink-dlap-701       # JP7
{… "platform": "linux", "architecture": "aarch64", "runtime": "aws_nucleus_classic",
 "coreVersion": "2.12.0", "status": "HEALTHY", "tags": {"dda-portal:managed": "true"}}

$ aws greengrassv2 get-core-device --core-device-thing-name jp5730ai-164v2        # JP5
{… "platform": "linux", "architecture": "aarch64", "runtime": "aws_nucleus_classic",
 "coreVersion": "2.12.0", "status": "HEALTHY", "tags": {"dda-portal:managed": "true"}}

$ aws greengrassv2 get-core-device --core-device-thing-name mic730jp513-ryvanlabhome  # JP5
{… identical platform/architecture/runtime/coreVersion …}

$ aws greengrassv2 get-core-device --core-device-thing-name ryanorinagxdevkithomelabjp622  # JP6
{… identical platform/architecture/runtime/coreVersion …}
```

Output shape (botocore service model):
`[architecture, coreDeviceThingName, coreVersion, lastStatusUpdateTimestamp,
platform, runtime, status, tags]`.

**There is no `variant` field, and no field from which one could be derived.**
`coreVersion` is the Nucleus version (2.12.0 on all five — identical across
JetPacks). `runtime` is `aws_nucleus_classic` on all 45 core devices in the
account. `platform=linux architecture=aarch64` is what every Jetson reports
regardless of JetPack: across all 45 devices the only values seen are
`linux/aarch64` (36), `linux/amd64` (8), `linux/arm` (1) and
`windows/amd64` (1).

**bugfix.md's operator-reported claim is CONFIRMED first-hand, and is broader
than stated.** It is not merely that the two JP7 devices report identically —
JP5, JP6 and JP7 devices are all indistinguishable through this API. The
`variant` attribute lives in the device's Nucleus platform overrides
(consistent with the comments at `plugin_components.py:213-216` and
`workflow_packaging.py:2136-2138`); Greengrass uses it during negotiation but
does not surface it on any read API in the allowed set.

### An installed-component proxy was tried and REJECTED

The one cloud-readable candidate was the installed
`aws.edgeml.dda.LocalServer.arm64JP*` component name. Checked across all 45
core devices (36 of them aarch64); it is **not reliable**:

```
mic730jp513-ryvanlabhome   ['aws.edgeml.dda.LocalServer.arm64JP6@1.0.66 (NOT-ROOT)',
                            'aws.edgeml.dda.LocalServer.arm64JP5@1.0.43']   <- BOTH, ambiguous
jp513-730aiv2              ['aws.edgeml.dda.LocalServer.arm64@1.0.94 (NOT-ROOT)']  <- names no JetPack
jp4mic730ai-ryanlabhome    ['aws.edgeml.dda.LocalServer.arm64@1.0.124']            <- names no JetPack
DDA_mic730ai_tr            ['aws.edgeml.dda.LocalServer@1.0.0']                    <- names no JetPack
bd-autopoint, dda_thing_autopoint, dlap401, DLAP211JNXThing, JetsonAGX-GGV2-GA,
JetsonNanoTurbine1, L4VJetsonNano, L4VJetsonNanoJFk14, l4vJetsonXavierNx,
l4vKioskDemoThing, reinvent2021-nano, ryvan-mic730ai-refurb2   -> no LocalServer at all
```

Of the 36 aarch64 core devices: **7** name a JetPack unambiguously
(`jetson-thor1`, `adlink-dlap-701`, `Neon-2000-ONO`, `jp5730ai-164v2`,
`jp6-orinagx`, `ryan-orin-nano`, `ryanorinagxdevkithomelabjp622`), **1**
reports two conflicting variant-named LocalServers
(`mic730jp513-ryvanlabhome`: JP6 non-root *and* JP5 root), **16** carry a
LocalServer name that identifies no JetPack, and **12** carry none at all.
A heuristic covering 7 of 36 devices, ambiguous on one — rejected as a source
of truth.

### What the portal's own record carries

Read from the repo (no DynamoDB call; `dynamodb` is outside the allowed
read-only set):

- `devices.py:33` and `quick_setup.py:82` — identical closed sets:
  `TARGET_ARCHITECTURES = ('x86_64', 'x86_64_nvidia', 'arm64_jp4', 'arm64_jp5',
  'arm64_jp6', 'arm64_jp7')`.
- `load_device_gate_info` (`deployments.py:2116-2135`) reads
  `DEVICES_TABLE[device_id].target_architecture`, returning `None` when there
  is no record or the read raises. Its docstring: *"Devices without a record
  fail closed (not a Test_Device; no recorded architecture)."*
- `devices.py:275-286` reads `platform` and `architecture` from
  `get_core_device` for display only.
- `deployments.py:295-310` reads `get_core_device` **only** for `coreVersion`.

**The map to a manifest `variant` is the identity.**
`plugin_components.platform_for` (`plugin_components.py:213-220`) writes
`platform['variant'] = arch` for every aarch64 target, and
`workflow_packaging.py:2138-2145` writes the same when more than one arm arch
is packaged. So `target_architecture == 'arm64_jp7'` is satisfied by a manifest
carrying `variant: arm64_jp7`, with no translation table. Verified against the
live data: all 41 variant-bearing manifests in the account use exactly these
strings.

### Verdict — the device-platform source the validator uses

| Attribute | Source | When absent |
|---|---|---|
| `os` | `get_core_device.platform` | UNVERIFIED |
| `architecture` | `get_core_device.architecture` | UNVERIFIED |
| `variant` | `DEVICES_TABLE.target_architecture` (identity map) | **UNVERIFIED — never incompatible** |
| `runtime` | `DEVICES_TABLE.target_architecture == 'x86_64_nvidia'` ⇒ `nvidia` | treat as unconstrained |

Fail-open rules, all derived from what the live data actually contains:

1. Device with **no** `DEVICES_TABLE.target_architecture` ⇒ its `variant` is
   unknown ⇒ any variant-bearing manifest is **UNVERIFIED**, never
   incompatible (2.9). How many devices are in that position was not measured
   (DynamoDB is outside the allowed call set — §4.3), but the case is
   certainly reachable: `load_device_gate_info` returns `None` for any thing
   name with no Devices-table record, and only **12 of the account's 45 core
   devices carry the `dda-portal:managed` tag** at all (the other 33, of which
   26 are aarch64, are not portal-provisioned — `get-core-device` reports no
   such tag for them). The tag is not the Devices-table record, so this bounds
   rather than measures the gap; it does establish that "no recorded
   architecture" is the common case, not an edge case.
2. `get_core_device` raising, or `platform`/`architecture` empty ⇒ the whole
   device's platform judgement is UNVERIFIED. `devices.py:275-286` already
   swallows `ClientError` here and leaves both `''`.
3. A **variant-less** manifest is satisfied by any device of that architecture
   (2.8, and 107 of 148 aarch64 manifests in the account are variant-less).
   An **absent `architecture`** and a literal `"*"` value are wildcards too
   (§1.1) — Nucleus and ShadowManager depend on it.
4. Thing-group target ⇒ per 3.8, unresolvable member platforms hide nothing
   and block nothing.

**A precedent conflict the design must handle explicitly.** The existing
plugin architecture gate does the opposite:
`evaluate_plugin_arch_gate` (`deployments.py:1987-2020`) documents *"A device
with no recorded Target_Architecture fails closed"* and implements it as
`if device_arch not in supported` — `None` is never in `supported`, so the gate
**blocks**. The new preflight must fail **open** (2.9) while 3.4 requires the
plugin gate keep byte-identical semantics. Both behaviours have to coexist:
the plugin gate stays fail-closed and untouched, the new validator is
fail-open, and the preservation oracle (task 3) must pin the plugin gate's
fail-closed behaviour so the new code cannot soften it.

---

## 4. What could NOT be verified within the allowed read-only set

Recorded honestly rather than assumed:

1. **The `FAILED_NO_STATE_CHANGE` status and the
   `errorStack ["DEPLOYMENT_FAILURE", "NO_AVAILABLE_COMPONENT_VERSION",
   "COMPONENT_VERSION_REQUIREMENTS_NOT_MET"]`** of Counterexample A/B.
   `GetDeployment` has no `reason` or `statusDetails` member
   (`[components, creationTimestamp, deploymentId, deploymentName,
   deploymentPolicies, deploymentStatus, iotJobArn, iotJobConfiguration,
   iotJobId, isLatestForTarget, parentTargetArn, revisionId, tags, targetArn]`),
   and every superseded revision now reports `deploymentStatus: INACTIVE`, not
   `FAILED`. The failure reason lives in the IoT job execution
   (`iot describe-job-execution`), which is outside the allowed call set. What
   IS verified is the submitted document, the component versions, the recipes'
   platforms and dependencies, the empty version lists, and
   `failureHandlingPolicy: ROLLBACK` — i.e. every precondition of the failure.
   The exploration test (task 2) must therefore assert on the pre-submit
   refusal, not on a reproduced Greengrass error string.
2. **`aws.greengrass.SecureTunneling` v1.0.20** returned
   `ResourceNotFoundException: Public component (aws.greengrass.SecureTunneling:1.0.20)
   does not exist` for `describe-component` — I picked a version that is not
   published. `list-component-versions` shows 26 versions
   (`2.0.1, 2.0.0, 1.1.3 … 1.0.0`); `1.0.19` and `2.0.1` do exist. This is a
   bad version guess on my part, not an API limitation, and it is the reason
   187 rather than 188 components were compared. It does incidentally confirm
   the resolver's fail-open contract has a real trigger:
   `describe_component`/`get_component` on a plausible-but-unpublished version
   raises rather than returning empty.
3. **`DEVICES_TABLE.target_architecture`'s live values.** DynamoDB is outside
   the allowed call set, so §1.3's portal-record half is read from the code,
   not from the table. The *shape* and the identity map are verified in-repo;
   which devices actually have a recorded architecture is not.
4. **Device-side facts of Counterexample C** — the six-day retention as
   observed on the device, the `resolve-all-group-dependencies-finish` /
   `ComponentManager` / `merge-config` log lines, the
   `GroupToRootComponents`/`ComponentToGroups` split in the device's own
   bookkeeping, the 119 ms Startup-after-Shutdown interval, the 19 correct
   cleanup executions. These are device evidence and remain
   operator-reported. The cloud half (documents, recipe, timestamps, ids) is
   now confirmed.

Per task 1's rule, nothing here forces a guess; where a source is absent the
resolver's answer is UNVERIFIED (fail-open), never a fault.

---

## 5. Findings that contradict or qualify bugfix.md as written

1. **2.8 is too narrow.** It promises universality only for a manifest that
   "constrains only os and architecture and carries no variant attribute". The
   account also contains `{"os": "linux"}` with **no architecture** (Nucleus,
   Cli), `{"os": "*"}` (ShadowManager, LogManager) and `Platform: null`
   (`testmodel`, `alienmodel`). All four are auto-included or deployable today.
   The matcher rule must be *absent key ⇒ wildcard, literal `"*"` ⇒ wildcard,
   empty Platform ⇒ matches everything*, not a variant-specific exception.
   Otherwise the validator blocks every deployment on Nucleus.

2. **1.1's premise does not hold, in the reassuring direction.**
   `describe-component` is perfectly reliable (187/187). The reason to read
   platforms from the recipe is not distrust but that `describe-component`
   cannot supply `ComponentDependencies`, so it is a redundant second call.
   Recording this because 1.1 was framed as "if `describe-component` is not
   reliable…", and the honest answer is that it is, and the resolver still
   should not use it.

3. **1.2's "not established either way" is now established, and the answer is
   more nuanced than the question allows.** Greengrass DOES expose the split
   (`isRoot`, `topologyFilter=ALL|ROOT`) — but only as the previous
   deployment's device-reported outcome, so it cannot decide the pre-submit
   question. The recipe closure remains the source of truth; the API is
   corroboration only. The design should say this rather than "no readable
   split".

4. **Counterexample C's retention window was not uninterrupted.** bugfix.md
   says the component "stayed installed and running on the device for six more
   days, across revisions 22-52 (~30 deployments)". The cloud record shows the
   depending workflow `dda.workflow.421f8233-…` is **absent from revisions 34,
   36, 37 and 38** (2026-08-27 03:24Z through 17:41Z). In those four windows
   nothing selected required the model, so Greengrass should have removed it.
   This is consistent with — and may be the cloud-side explanation for — the
   device-side observation that the staged repository "kept being re-created
   119 ms after each Shutdown cleanup removed it", but it means the retention
   was not monotonic. The claim should read "across revisions 21-52, with the
   depending workflow briefly absent at revisions 34 and 36-38" and the
   exploration test must not encode an unbroken retention.

5. **Revision 53's timestamp differs between cloud and device by ~3 minutes.**
   bugfix.md gives 2026-09-01T16:18:30.543Z (the device's `merge-config` log
   line); the cloud `creationTimestamp` is **16:15:32.170Z**. Not a
   contradiction — submit time versus device-apply time — but the two should be
   labelled distinctly.

6. **`list-component-versions` fails silently across namespaces.** Wrong
   namespace ⇒ `[]`, not an exception. The dual-namespace guard in tasks.md's
   Notes is therefore load-bearing and cannot be replaced by
   exception-handling.

7. **The existing plugin arch gate fails CLOSED on an unrecorded device
   architecture** (`deployments.py:2000`, `:2012`), which is the direct
   opposite of 2.9's fail-open contract. bugfix.md does not mention the
   tension. 3.4 pins the plugin gate, so both must coexist; the design should
   state the asymmetry deliberately instead of letting a future reader
   "harmonize" them.

8. **The portal's own `list_installed_components` call sees ROOT only.**
   `devices.py:290` passes no `topologyFilter` and the API default is `ROOT`
   (verified: 14 vs 21 on `jetson-thor1`). Any corroboration in 4.4 must pass
   `topologyFilter='ALL'` explicitly.

9. **Repeated near-identical revisions seconds apart are normal on both
   incident devices** (thor1 19/20, 21/22, 23/24, 25-28, 30-33, 43-47, 48-52;
   dlap701 4/5/6, 7/8/9, 10/11/12). A validator that refuses a submit will be
   hit on each of those, and the automated store-limit remediation path
   (`_submit_store_remediation`, `_resume_original_deployment`) is among the
   producers — which is exactly why tasks.md excludes it from gating. The
   evidence supports that exclusion.

---

## 6. Reproduction

Scripts used, under `/tmp/dda_evidence/` (read-only, outside the repo, not
committed; listed so any claim above can be re-derived). Each prints the
equivalent `aws greengrassv2 …` command line beside its output.

| Script | What it does |
|---|---|
| `list_components.py` | `list-components --scope PRIVATE`, 181 names |
| `t11_platforms.py` | 15 per-class `describe-component` vs `get-component` pairs |
| `t11_bulk.py` | the 187-component agreement check, attribute-key census |
| `t11_edge.py` | `Platform: null`, `os: "*"`, jp6+jp7 workflow |
| `t11_ceA.py` | Counterexample A/B documents, `adlink-dlap-701` 17 revisions |
| `t1_versions.py` | `list-component-versions` in both namespaces, 13 names |
| `t12_rootsplit.py` | `topologyFilter` ALL vs ROOT, `GetDeployment` shape |
| `t12_history.py`, `t12_hist2.py` | `jetson-thor1` 86-revision M/W timeline |
| `t13_devices.py` | `list-core-devices` + `get-core-device` for all 45 |
| `t13_variant_proxy.py` | the rejected installed-LocalServer-name proxy |
