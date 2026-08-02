# Requirements Document

## Introduction

DDA's edge runtime, the LocalServer, ships as independently-versioned per-architecture Greengrass components. Three of the four follow a consistent, explicit JetPack-tagged convention — `aws.edgeml.dda.LocalServer.arm64JP5`, `aws.edgeml.dda.LocalServer.arm64JP6`, `aws.edgeml.dda.LocalServer.amd64` — but the JetPack 4 variant is published under the bare, untagged name `aws.edgeml.dda.LocalServer.arm64`. Because that bare name reads as "generic aarch64", the portal's model-component publisher uses it as the aarch64 fallback: when a model's compile target is unknown or missing, `resolve_local_server_component` in `greengrass_publish.py` silently stamps the model's HARD LocalServer dependency as `aws.edgeml.dda.LocalServer.arm64` (the JetPack 4 server). On a JetPack 5 or 6 device that fallback drags the wrong LocalServer onto the device, where it collides with the correct variant (port 3443) and crash-loops to BROKEN, taking the whole core deployment UNHEALTHY. This exact failure was observed in production: a JP5 segmentation model published before the arch-specific mapping existed carried an `aws.edgeml.dda.LocalServer.arm64` dependency and broke a JP5 station.

This feature removes the ambiguity at its root. It renames the JetPack 4 LocalServer to the explicit `aws.edgeml.dda.LocalServer.arm64JP4`, retiring the bare `aws.edgeml.dda.LocalServer.arm64` as a produced-and-depended-upon component name, so all four variants follow one convention. It makes the portal's LocalServer dependency resolution fail closed — an aarch64 model whose target does not resolve to a known JetPack-tagged LocalServer is rejected at publish time rather than silently defaulting to JetPack 4. It threads the new name through every seam that recognizes LocalServer variant names (the build, the model publisher, the deployment-side variant-to-architecture parser, the deploy-screen compatibility inference, and the workflow-packaging per-variant minimum-version map), while keeping the legacy bare `arm64` name recognized on read so already-provisioned JetPack 4 devices continue to work during the transition. JetPack 4 remains a supported shipping target throughout.

## Glossary

- **Portal**: The edge-cv-portal cloud web application (React frontend, Lambda backend, DynamoDB storage) that manages DDA use cases, models, workflows, deployments, and devices.
- **LocalServer**: The Greengrass component running on an Edge_Device that executes DDA pipelines. It ships as independently-versioned per-architecture variants.
- **LocalServer_Variant**: One per-architecture LocalServer Greengrass component. The variants are the JetPack 4 variant, `aws.edgeml.dda.LocalServer.arm64JP5` (JetPack 5), `aws.edgeml.dda.LocalServer.arm64JP6` (JetPack 6), and `aws.edgeml.dda.LocalServer.amd64` (x86_64).
- **JP4_LocalServer_Name**: The Greengrass component name of the JetPack 4 LocalServer variant. Today the bare `aws.edgeml.dda.LocalServer.arm64`; this feature makes it the explicit `aws.edgeml.dda.LocalServer.arm64JP4`.
- **Legacy_JP4_Name**: The pre-feature JetPack 4 LocalServer component name `aws.edgeml.dda.LocalServer.arm64`, retained as a recognized-on-read alias for already-provisioned JetPack 4 devices.
- **Target_Architecture**: The DDA architecture identifier, one of `x86_64`, `x86_64_nvidia`, `arm64_jp4`, `arm64_jp5`, `arm64_jp6`.
- **Compile_Target**: The model compilation target string used at model publish time (e.g. `jetson-xavier` for JetPack 4, `jetson-xavier-jp5`, `jetson-xavier-jp6`, `x86_64-cpu`, `x86_64-cuda`, `arm64-cpu`).
- **Model_Component**: A Greengrass component the portal publishes for a trained/compiled model (name like `model-{name}-{target}`), whose recipe declares a HARD `ComponentDependencies` entry on a LocalServer_Variant.
- **Model_Publisher**: The portal Lambda (`greengrass_publish.py`) that generates Model_Component recipes, including the vLLM path, and stamps the LocalServer dependency via `resolve_local_server_component`.
- **LocalServer_Resolver**: The `resolve_local_server_component(target, platform)` function in the Model_Publisher that maps a Compile_Target (and coarse platform) to a LocalServer_Variant component name.
- **Variant_Arch_Parser**: The deployment-side function `deployments.local_server_component_arch(component_name)` that maps a LocalServer_Variant component name to a Target_Architecture (used by the LocalServer version-compatibility gate).
- **Deploy_Screen_Inference**: The Create/Revise Deployment screen logic (device-arch-compatibility feature) that infers a component's JetPack Target_Architecture from JetPack tokens in its component name (e.g. `arm64JP5`/`arm64JP6`).
- **Min_Version_Map**: The workflow-packaging per-variant minimum LocalServer version map (`workflow_packaging.py` `MIN_LOCAL_SERVER_VERSIONS`), keyed by Target_Architecture.
- **Build_System**: The GDK-based LocalServer build tooling (`gdk-config.json`, `run_jp_builds.sh`, `build-custom.sh`) that produces and names LocalServer_Variant components.

## Requirements

### Requirement 1: Explicit JetPack 4 LocalServer Component Name

**User Story:** As a platform maintainer, I want the JetPack 4 LocalServer published under an explicit `arm64JP4` name, so that all LocalServer variants follow one JetPack-tagged convention and no variant owns the ambiguous bare `arm64` name.

#### Acceptance Criteria

1. THE Build_System SHALL produce the JetPack 4 LocalServer variant under the component name `aws.edgeml.dda.LocalServer.arm64JP4`.
2. THE Build_System SHALL support building the JetPack 4 variant through the same per-target build path it uses for the JetPack 5 and JetPack 6 variants, selectable by a JetPack 4 build target.
3. THE JP4_LocalServer_Name SHALL be `aws.edgeml.dda.LocalServer.arm64JP4` everywhere the portal produces or references a LocalServer dependency name for a newly published artifact.
4. THE feature SHALL NOT change the JetPack 5, JetPack 6, or x86_64 LocalServer variant component names.

### Requirement 2: Fail-Closed Model LocalServer Dependency Resolution

**User Story:** As an operator, I want a model whose architecture cannot be resolved to a known LocalServer variant to be rejected at publish time, so that a mis-resolved model can never silently depend on the JetPack 4 server and break a JetPack 5 or 6 device.

#### Acceptance Criteria

1. WHEN the Model_Publisher stamps a Model_Component's LocalServer dependency, THE LocalServer_Resolver SHALL map each known Compile_Target to its explicit LocalServer_Variant: `jetson-xavier` to `aws.edgeml.dda.LocalServer.arm64JP4`, `jetson-xavier-jp5` to `aws.edgeml.dda.LocalServer.arm64JP5`, `jetson-xavier-jp6` to `aws.edgeml.dda.LocalServer.arm64JP6`, and the x86_64 targets to `aws.edgeml.dda.LocalServer.amd64`.
2. IF a model's Compile_Target resolves to the aarch64 platform but is not a known JetPack-tagged Compile_Target, THEN THE LocalServer_Resolver SHALL fail the publish with an error identifying the unresolved target, rather than defaulting to any bare or generic LocalServer name.
3. THE LocalServer_Resolver SHALL NOT return `aws.edgeml.dda.LocalServer.arm64` or any untagged aarch64 LocalServer name for any input.
4. WHEN the Model_Publisher publishes an x86_64 model, THE LocalServer_Resolver SHALL resolve `aws.edgeml.dda.LocalServer.amd64` exactly as before this feature.
5. WHEN the vLLM Model_Publisher path stamps a LocalServer dependency, THE resolution SHALL use the same fail-closed LocalServer_Resolver, so vLLM components are subject to the identical guarantee.

### Requirement 3: Deployment-Side and Deploy-Screen Recognition of the New Name

**User Story:** As an operator, I want deploy-time architecture gating and the deploy screen to recognize the explicit `arm64JP4` name, so that JetPack 4 components are gated and displayed as JetPack 4 exactly as JP5 and JP6 are.

#### Acceptance Criteria

1. WHEN the Variant_Arch_Parser is given `aws.edgeml.dda.LocalServer.arm64JP4`, THE Variant_Arch_Parser SHALL return Target_Architecture `arm64_jp4`.
2. THE Variant_Arch_Parser SHALL continue to map `aws.edgeml.dda.LocalServer.arm64JP5` to `arm64_jp5`, `aws.edgeml.dda.LocalServer.arm64JP6` to `arm64_jp6`, and the x86_64/amd64 names to `x86_64`, unchanged.
3. WHEN the Variant_Arch_Parser is given the Legacy_JP4_Name `aws.edgeml.dda.LocalServer.arm64` (or `aws.edgeml.dda.LocalServer.aarch64`), THE Variant_Arch_Parser SHALL continue to return `arm64_jp4`, so already-provisioned JetPack 4 devices remain recognized.
4. WHERE the Deploy_Screen_Inference infers a JetPack Target_Architecture from a component name's JetPack token, THE inference SHALL recognize the `arm64JP4` token as `arm64_jp4` alongside the existing `arm64JP5`/`arm64JP6` tokens.
5. THE recognition changes SHALL NOT alter the parser's or inference's behavior for any non-LocalServer component name or for the JP5/JP6/x86 variants.

### Requirement 4: Workflow Packaging Minimum-Version Map Alignment

**User Story:** As an operator, I want the workflow-packaging per-variant minimum LocalServer version gate to treat JetPack 4 by its explicit variant, so that JetPack 4 workflow components are gated against the JetPack 4 LocalServer version lineage.

#### Acceptance Criteria

1. WHEN the workflow packager resolves the minimum LocalServer version for the `arm64_jp4` Target_Architecture, THE Min_Version_Map SHALL key that lineage the same way it keys `arm64_jp5` and `arm64_jp6` (per-arch floor with scalar fallback), independent of the other variant lineages.
2. THE Min_Version_Map alignment SHALL NOT change the resolved minimum version for the `arm64_jp5`, `arm64_jp6`, or x86_64 lineages.

### Requirement 5: Migration and Backward Compatibility

**User Story:** As an operator with JetPack 4 devices already in the field, I want the rename rolled out without breaking currently-provisioned JetPack 4 stations, so that JetPack 4 keeps working through its remaining supported life.

#### Acceptance Criteria

1. THE feature SHALL define a migration path in which the JetPack 4 LocalServer is (re)published as `aws.edgeml.dda.LocalServer.arm64JP4` and JetPack 4 Model_Components are republished to depend on `aws.edgeml.dda.LocalServer.arm64JP4`.
2. WHILE both names may exist during the transition, THE Variant_Arch_Parser and Deploy_Screen_Inference SHALL recognize both the Legacy_JP4_Name and the JP4_LocalServer_Name as `arm64_jp4` (Requirement 3.3, 3.4).
3. WHEN a JetPack 5 or JetPack 6 model is published after this feature, THE resulting Model_Component SHALL depend on its own JetPack-tagged LocalServer_Variant and SHALL NOT depend on any JetPack 4 LocalServer name, eliminating the cross-variant conflict that motivated this feature.
4. WHEN a model, workflow, or deployment that does not involve JetPack 4 is processed after this feature, THE Portal SHALL behave identically to before this feature.

### Requirement 6: Regression Coverage for the Dependency Mapping

**User Story:** As a maintainer, I want automated coverage of the target-to-LocalServer mapping and the fail-closed behavior, so that a future target string or code path cannot silently reintroduce the wrong-LocalServer bug.

#### Acceptance Criteria

1. THE feature SHALL include tests asserting the LocalServer_Resolver maps every known Compile_Target to the expected LocalServer_Variant, including `jetson-xavier` to `aws.edgeml.dda.LocalServer.arm64JP4` and the JP5/JP6 targets to their variants.
2. THE feature SHALL include a test asserting that an aarch64 target not resolvable to a known JetPack-tagged variant fails closed (raises/rejects) rather than returning a bare or generic LocalServer name.
3. THE feature SHALL include tests asserting the Variant_Arch_Parser maps `arm64JP4` and the Legacy_JP4_Name both to `arm64_jp4`, and the JP5/JP6/x86 names to their architectures.
4. THE feature SHALL include a test asserting no published Model_Component recipe (vision or vLLM) can be generated with a bare `aws.edgeml.dda.LocalServer.arm64` dependency.
