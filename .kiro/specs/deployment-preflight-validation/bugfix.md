# Bugfix Requirements Document

## Introduction

The portal's Create/Revise Deployment screen offers, and accepts, components that cannot be deployed to the chosen target device. Nothing on the submit path checks a selected component's published Greengrass recipe platform manifests against the target device's platform, and nothing anywhere resolves a selected component's transitive `ComponentDependencies` closure against what actually exists in the account. The operator therefore discovers the problem only after submit, as a Greengrass cloud-side version-negotiation failure, and Greengrass reports exactly ONE fault per negotiation — so a deployment carrying several faults must be fixed by blind serial iteration: resubmit, read one error, fix, resubmit, discover the next.

Two distinct faults were observed serially in one deployment during a JP7 (Thor) device session (device `adlink-dlap-701`, account 164152369890, us-east-1):

- **Counterexample A — platform-manifest mismatch.** Deployment `1982ad02-c3eb-4803-8b19-2b41f28bc391` revision 6 failed `FAILED_NO_STATE_CHANGE` with `errorStack ["DEPLOYMENT_FAILURE", "NO_AVAILABLE_COMPONENT_VERSION", "COMPONENT_VERSION_REQUIREMENTS_NOT_MET"]`, naming workflow component `dda.workflow.8784b33b-25a6-44c3-b62d-d47e8213eabe` (portal workflow `bedrock_test (copy)`, v1.0.0). That component publishes only `{"os":"linux","variant":"arm64_jp5","architecture":"aarch64"}` and `{"os":"linux","variant":"arm64_jp6","architecture":"aarch64"}`, so no manifest matches a JP7 device. The Greengrass message compounds the problem: its "Component … claimed platform: os linux, variant arm64_jp7" clause echoes the DEVICE's platform, not the component's claim, and it sent the first diagnosis pass in the wrong direction.
- **Counterexample B — dependency with zero published versions.** After the offending workflow was removed, the same deployment (`4c0f8f84-32e5…`, revision 9) failed again with the same `errorStack` but a reason that names NO component: "No local or cloud component version satisfies the requirements…". Walking the dependency closure of all 14 root components found two legacy model components whose HARD LocalServer dependency points at a component name with an empty version list in the account: `model-cookies-segmentation-seghead-jetson-xavier` v2.0.0 → `aws.edgeml.dda.LocalServer.arm64JP4 >=1.0.0 <2.0.0` (available versions `[]`), and `model-yolo-test-jetson-xavier` v2.0.0 → `aws.edgeml.dda.LocalServer.arm64 >=1.0.0 <2.0.0` (available versions `[]`). Their JP7 twins (`model-yolo-test-jetson-xavier-jp7` v8.0.0, `model-cookies-segmentation-onnx-jetson-xavier-jp7` v4.0.0) depend on `aws.edgeml.dda.LocalServer.arm64JP7` and resolve fine; `seghead` has no JP7 twin at all, so it cannot deploy to a JP7 device without re-registration/repackaging.

Blast radius is bounded and purely operator-facing. Both failures were `FAILED_NO_STATE_CHANGE` under `failureHandlingPolicy: ROLLBACK`, resolved cloud-side in ~30 ms with no artifact download and no device state change. This is an operator-velocity and observability defect, not a device-integrity one — which is why the fix is a pre-submit validation and messaging change with no device-side component.

The prior `device-arch-compatibility` spec does not cover this. Its client-side arch filter is scoped to `Gated_Component`s only: `classifyGatedComponent` in `edge-cv-portal/frontend/src/pages/deployments/archCompatibility.ts` returns a kind only for `model-vllm-*` and `dda.plugin.*`, and explicitly returns null for workflow components. On the backend, `collect_vllm_component_manifests` (`deployments.py`) contributes a manifest for a `dda.workflow.*` component only when its version item records `has_llm_inference`; `bedrock_test (copy)` records it false, so Counterexample A passed every gate. The compatibility source is also the wrong one: every existing gate reads portal-side records (`published_component.supported_architectures`, workflow `packaged_architectures`, plugin `architectures`) rather than the recipe manifests Greengrass negotiates against, so a component whose record and manifests disagree slips through. And nothing on either path has any notion of dependency resolvability — `check_plugin_deployment_gates` walks a "dependency closure" that is only the workflow's recorded `dda.plugin.*` components, never the Greengrass recipe closure. There is no `arm64_jp7` gap to report in the fixed architecture set: `arm64_jp7` is already present in `TARGET_ARCHITECTURES` in both `devices.py:33` and `quick_setup.py:82`.

Verification status of the claims below. Verified in-repo: the deploy screen's coarse filter reads only os/architecture-shaped fields and ignores a manifest's `variant` attribute (`getComponentArchitectures`/`isCompatibleWithDevice`, `CreateDeployment.tsx:181-238`); the gated classification excludes workflow components (`archCompatibility.ts`); `create_deployment` and `create_workflow_deployment` run only the plugin lifecycle/arch gates, the vLLM arch gate, the `INCOMPATIBLE_LOCAL_SERVER` floor and camera-binding validation before `greengrass_client.create_deployment` (`deployments.py:1136-1260`, `3630-3700`); no code anywhere resolves recipe `ComponentDependencies` for validation (the only production reads are the Nucleus version requirement at `deployments.py:445` and the LocalServer-variant contradiction guard in `workflow_packaging.py:1648`); a workflow packaged for more than one arm JetPack gets a `variant` attribute per manifest while a single-arm packaging stays variant-less (`workflow_packaging.py:2135-2148`); model components published by `greengrass_publish.py` carry a variant-less `{"os":"linux","architecture":<platform>}` manifest (`greengrass_publish.py:323`, `654`); `TARGET_TO_LOCAL_SERVER` maps the legacy `jetson-xavier` target to `aws.edgeml.dda.LocalServer.arm64JP4` and documents the bare `…LocalServer.arm64` name as RETIRED as a produced name, which is what leaves both Counterexample B dependencies pointing at names with no published versions; the portal surfaces the Greengrass `reason`/`statusDetails` verbatim on deployment detail (`deployments.py:891-955`) and no code anywhere translates `NO_AVAILABLE_COMPONENT_VERSION` or the "does not claim platform" wording. Reported by the operator and NOT verifiable from this repo: the deployment ids, revisions, timestamps, per-component available-version lists, the two devices' identical `platform=linux architecture=aarch64` report to Greengrass, and that the `variant` attribute originates from Nucleus platform attributes rather than the kernel arch (the repo asserts the same in comments at `plugin_components.py:213-216` and `workflow_packaging.py:2136-2138`, which corroborates but does not verify it). Also unverified in-repo: whether Greengrass `DescribeComponent`'s `platforms[].attributes` reliably carries `variant` for every listed component — `components.py:127-158` assumes it does for plugin components.

## Bug Analysis

### Current Behavior (Defect)

No pre-submit check compares a selected component's published platform manifests with the target device's platform, and no check resolves the selected components' transitive dependency closure, so Greengrass rejects the deployment after submit, one fault at a time, with messages that misattribute or omit the offending component:

1.1 WHEN a selected component's published recipe manifests all carry a `variant` attribute naming a JetPack other than the target device's THEN the system offers the component as compatible on the Create/Revise Deployment screen, because the screen's platform filter reduces every `aarch64` manifest to the coarse `arm64` bucket and ignores the `variant` attribute entirely

1.2 WHEN such a component is a `dda.workflow.*` component whose version item records `has_llm_inference` false THEN the system applies no architecture gate to it at all — the client-side classification returns "not gated" for every workflow component and the backend vLLM gate collects no manifest for it — and the submission is accepted

1.3 WHEN a selected component (or any component in its transitive `ComponentDependencies` closure) requires a component name that has no published version in the account satisfying its `VersionRequirement` THEN the system submits the deployment and the deployment fails cloud-side, because no portal code path resolves a recipe dependency closure or checks that a dependency has any published version

1.4 WHEN a submitted deployment contains more than one such fault THEN the system reports only the first fault Greengrass encounters, so the operator must remove or repair one component, resubmit, and rediscover the next fault, repeating until the deployment negotiates

1.5 WHEN a platform-mismatch failure reason is surfaced on the deployment detail THEN the system presents Greengrass's wording verbatim, including the clause that reports the DEVICE's platform as the component's claimed platform, which misdirects diagnosis toward the component's packaging instead of the mismatch

1.6 WHEN a dependency-resolvability failure reason is surfaced on the deployment detail THEN the system presents a reason that identifies no component, no version requirement and no remediation, leaving the operator to reconstruct the dependency closure by hand

1.7 WHEN a component's portal-side backing record disagrees with the platform manifests published on its Greengrass component version THEN the system judges compatibility from the portal record only, so a component that Greengrass will reject can still be offered and accepted

### Expected Behavior (Correct)

The portal validates every selected component and its full transitive dependency closure against the account and the target device BEFORE submitting, reports every fault it finds in one pass, and attributes each fault to a named component with a remediation:

2.1 WHEN a deployment is submitted with target devices selected THEN the system SHALL validate every selected component and every component in its transitive `ComponentDependencies` closure before submitting to Greengrass, regardless of whether the component belongs to an existing gated class

2.2 WHEN validating platform compatibility THEN the system SHALL judge each component version against the platform manifests published on that component version and the target device's platform (os, architecture and the platform attributes the device reports, including `variant`), rather than against the portal's own backing records

2.3 WHEN a component version publishes no manifest whose platform is satisfied by the target device's platform THEN the system SHALL reject the submission before it reaches Greengrass and SHALL report the component name, the component version, the platform(s) that component version actually claims, the target device(s) and the platform each of them reports, and a remediation

2.4 WHEN validating dependency resolvability THEN the system SHALL confirm, for every dependency in the closure, that the depended-on component name exists in the account with at least one published version satisfying the dependency's `VersionRequirement`

2.5 WHEN a dependency in the closure has no published version satisfying its `VersionRequirement` THEN the system SHALL reject the submission before it reaches Greengrass and SHALL report the depended-on component name, its `VersionRequirement`, the selected component and version that requires it, whether the name has zero published versions or only non-satisfying ones, and a remediation — including, where the selected component has no deployable equivalent for the target platform, that the component must be re-registered or repackaged for that platform

2.6 WHEN more than one fault is found across the selected components and their closure THEN the system SHALL report every fault it found in a single response, so that the operator can resolve all of them before the next submission rather than discovering them one per submit

2.7 WHEN reporting any fault THEN the system SHALL name the offending component in every case and SHALL attribute platforms correctly, presenting the component's claimed platform(s) and the device's reported platform as distinct facts, and SHALL NOT reproduce the Greengrass wording that presents the device's platform as the component's claim

2.8 WHEN a component version's manifest constrains only os and architecture and carries no variant attribute THEN the system SHALL treat that manifest as satisfied by every device of that architecture, so a variant-less `{"os":"linux","architecture":"aarch64"}` component is never reported as incompatible with any Jetson variant

2.9 WHEN the validation cannot be completed because a required account read fails or a component's manifests or versions cannot be resolved THEN the system SHALL report the unresolved component as unverified rather than as a fault, and SHALL NOT block a submission on the basis of a check it could not perform

2.10 WHEN the validation finds no fault THEN the system SHALL submit the deployment to Greengrass unchanged, leaving Greengrass as the authoritative enforcement layer for what it accepts

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a deployment's selected components and their transitive closure resolve against the target device today THEN the system SHALL CONTINUE TO submit it with the same Greengrass deployment document and no additional operator step (the two `jetson-thor1` deployments `7092ec91-b404-4738-a586-a022ddc15157` and `a9086c7d…` are the reference cases)

3.2 WHEN a selected component version publishes a variant-less aarch64 manifest THEN the system SHALL CONTINUE TO offer it for, and deploy it to, any Jetson device regardless of the device's JetPack variant

3.3 WHEN a selected model component's JetPack-matched twin is chosen for a matching device (for example a `-jp7` model component whose HARD dependency is `aws.edgeml.dda.LocalServer.arm64JP7`, deployed to a JP7 device) THEN the system SHALL CONTINUE TO accept and submit the deployment

3.4 WHEN an existing pre-submit gate applies THEN the system SHALL CONTINUE TO evaluate it with identical semantics, error codes and payloads: the vLLM architecture gate (`VLLM_ARCH_UNSUPPORTED`), the Plugin_Component lifecycle and architecture gates (`PLUGIN_LIFECYCLE_VIOLATION`, `PLUGIN_ARCH_UNSUPPORTED`), the workflow LocalServer floor (`INCOMPATIBLE_LOCAL_SERVER`) and deploy-time camera-binding validation

3.5 WHEN the Create/Revise Deployment screen filters or groups the component catalog THEN the system SHALL CONTINUE TO apply the existing coarse arm64/amd64 platform filter, the gated `Target_Architecture` filter and the name-inferred JetPack filter with their current outcomes, including the hidden-count notice and the explainable incompatible grouping

3.6 WHEN the screen is in revise mode with a pre-loaded component that is flagged incompatible THEN the system SHALL CONTINUE TO show it rather than drop it, and SHALL CONTINUE TO allow the operator to remove it and proceed

3.7 WHEN no target device is selected THEN the system SHALL CONTINUE TO leave the component catalog fully discoverable, applying no device-derived validation or filtering

3.8 WHERE the deployment target is an IoT Thing Group whose member device platforms are not resolvable THEN the system SHALL CONTINUE TO neither hide components nor block submission on the basis of platform or dependency validation

3.9 WHEN Greengrass rejects a deployment that the pre-submit validation allowed THEN the system SHALL CONTINUE TO surface the Greengrass `reason` and `statusDetails` on the deployment detail as it does today, so that no cloud-side failure detail is lost

3.10 WHEN this fix is delivered THEN the system SHALL CONTINUE TO run every device unchanged: no component artifact, recipe, LocalServer build, published component version or on-device behavior is altered, and a device whose deployment is not resubmitted stays exactly as it is
