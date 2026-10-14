# Requirements Document

## Introduction

The portal's deployment architecture gates (vllm-triton-inference Req 3, custom-node-designer Req 16) reject a deployment when a target device's recorded DDA `Target_Architecture` is not in the supported set of a gated component (a vLLM model component, an LLM-bearing workflow component, or a Node Designer Plugin_Component). The gates fail closed: a device with no recorded `Target_Architecture` is treated as unsupported.

Two gaps degrade this experience today:

1. **Onboarding gap.** A Station provisioned through Station Quick Setup never gets a `Target_Architecture` recorded on the Devices table. The station-quick-setup flow (and `setup_station.sh` before it) records no architecture, and the only writer of `Target_Architecture` is the manual admin `PUT /api/v1/devices/{id}`. Greengrass reports only `aarch64` (identical across JetPack 5 and 6), so nothing distinguishes `arm64_jp5` from `arm64_jp6` automatically. As a result every quick-setup device lands with a null architecture and its first gated deployment (for example a vLLM model) is rejected until an admin manually sets the architecture — a confusing failure with no obvious cause.

2. **Deploy-screen confusion.** The Create/Revise Deployment screen filters the component catalog only by a coarse `arm64` vs `amd64` read of Greengrass recipe platform metadata. It does not use the device's DDA `Target_Architecture`, and gated components (vLLM model components, LLM-bearing workflow components, Plugin_Components) that the backend will reject are still shown and selectable. The user only discovers the incompatibility as a rejection on submit.

This feature closes both gaps. During quick-setup provisioning the Station determines its own DDA `Target_Architecture` and the portal records it on the Devices table, so quick-setup devices are gate-ready without a manual step. The Create/Revise Deployment screen is upgraded to filter the component catalog by the same DDA `Target_Architecture` compatibility contract the backend gates enforce, so components a target device cannot run are hidden or clearly separated before submit. The backend gates remain the authoritative enforcement layer; the screen filter is a client-side aid that mirrors them.

## Glossary

- **Portal**: The Edge CV Portal (edge-cv-portal) web application: React frontend, Lambda-backed REST API, and CDK-managed infrastructure.
- **Portal_Backend**: The Lambda functions and API Gateway endpoints serving the Portal's REST API.
- **Devices_Table**: The DynamoDB `dda-portal-devices` table whose per-device item records portal-managed device attributes, keyed by `device_id` (the IoT Thing name).
- **Target_Architecture**: The device's recorded DDA architecture attribute on the Devices_Table item, one of the fixed set `{x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, arm64_jp6}`, matched by exact name by the deployment architecture gates. Null when unrecorded.
- **Arch_Gate**: The Portal_Backend pre-submit deployment gates that reject a deployment when a target device's `Target_Architecture` is not in the supported set of a gated component: the vLLM architecture gate (`check_vllm_deployment_gate`, vllm-triton-inference Req 3.3-3.7) and the Plugin_Component architecture gate (`check_plugin_deployment_gates`, custom-node-designer Req 16.6). Both fail closed on a null `Target_Architecture`.
- **Gated_Component**: A component whose deployment is subject to the Arch_Gate: a vLLM model component (`model-vllm-*`), an LLM-bearing workflow component (a `dda.workflow.*` version item with `has_llm_inference`), or a Node Designer Plugin_Component (`dda.plugin.*`).
- **Supported_Architecture_Set**: The set of `Target_Architecture` values a Gated_Component version supports, resolved from its backing record (a vLLM_Model_Record's `published_component.supported_architectures`, a workflow version item's `packaged_architectures`, or a Plugin_Component's recorded `architectures`); empty when unresolvable, so the gate fails closed.
- **Station**: An edge device (Jetson or x86 host) running the DDA Greengrass stack after provisioning.
- **Quick_Setup**: The Station Quick Setup feature (station-quick-setup): a portal-driven one-line-command provisioning flow. Its device-side installer is the Setup_Bundle; it reports provisioning status to the Quick_Setup_Endpoint.
- **Setup_Bundle**: The self-contained device-side installer served by Quick_Setup, derived from `station_install`, that provisions the Station and reports Setup_Status.
- **Quick_Setup_Endpoint**: The Portal_Backend HTTPS endpoints that serve Quick_Setup Stations presenting a Setup_Token, including the status-report endpoint.
- **Detected_Architecture**: The DDA `Target_Architecture` value the Setup_Bundle determines for the Station from the Station's own platform during provisioning.
- **Create_Revise_Deployment_Screen**: The portal frontend Create Deployment page (`CreateDeployment.tsx`), used for both creating a new deployment and revising the existing deployment of a target.
- **Deployments_List_Page**: The portal frontend page that lists existing deployments.
- **Created_Date**: The creation timestamp recorded on each deployment.

## Requirements

### Requirement 1: Station Determines Its DDA Target_Architecture During Quick Setup

**User Story:** As a station operator using Quick Setup, I want the installer to determine my device's DDA architecture automatically, so that I do not have to know or enter `arm64_jp6` versus `arm64_jp5` myself.

#### Acceptance Criteria

1. WHEN the Setup_Bundle provisions a Station, THE Setup_Bundle SHALL determine the Station's Detected_Architecture as exactly one value from the fixed set `{x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, arm64_jp6}`, distinguishing JetPack major versions on aarch64 hosts from a source other than the CPU architecture reported by the kernel (which is identical across JetPack releases).
2. WHERE the Station is a Jetson device, THE Setup_Bundle SHALL derive the JetPack-major portion of the Detected_Architecture (`arm64_jp4`, `arm64_jp5`, or `arm64_jp6`) from the installed L4T/JetPack release information on the Station.
3. WHERE the Station is an x86_64 host, THE Setup_Bundle SHALL derive the Detected_Architecture as `x86_64_nvidia` when an NVIDIA GPU runtime is present and `x86_64` otherwise.
4. IF the Setup_Bundle cannot determine a Detected_Architecture from the fixed set, THEN THE Setup_Bundle SHALL treat the architecture as undetermined, SHALL NOT fail provisioning for that reason alone, and SHALL continue to a successful completion when all other provisioning steps succeed.
5. THE determination of the Detected_Architecture SHALL make no change to the Station's provisioned end state beyond reporting the value, so that a Station provisioned with this feature reaches the same end state as station-quick-setup Requirement 4.2 otherwise defines.

### Requirement 2: Portal Records the Quick-Setup Device's Target_Architecture

**User Story:** As a portal user, I want a device I onboard through Quick Setup to already have its DDA architecture recorded, so that its first architecture-gated deployment is not rejected for a missing architecture.

#### Acceptance Criteria

1. WHEN the Setup_Bundle reports successful completion to the Quick_Setup_Endpoint and a Detected_Architecture was determined, THE Setup_Bundle SHALL include the Detected_Architecture in the completion report.
2. WHEN the Quick_Setup_Endpoint accepts a completion report that includes a Detected_Architecture in the fixed set `{x86_64, x86_64_nvidia, arm64_jp4, arm64_jp5, arm64_jp6}`, THE Portal_Backend SHALL record that value as the `Target_Architecture` on the Devices_Table item for the provisioned device name.
3. IF a completion report includes an architecture value outside the fixed set, THEN THE Portal_Backend SHALL reject the reported architecture value, SHALL NOT write it to the Devices_Table, and SHALL leave the completion handling of station-quick-setup Requirement 6.1 otherwise unchanged.
4. IF a completion report includes no Detected_Architecture, THEN THE Portal_Backend SHALL leave the device's `Target_Architecture` unchanged and SHALL still set the Setup_Status to `completed` per station-quick-setup Requirement 6.1.
5. WHEN the Portal_Backend records a `Target_Architecture` from a Quick_Setup completion report, THE recording SHALL be authenticated as originating from the Setup_Bundle of the targeted Device_Registration by the same authentication that station-quick-setup Requirement 6.7 applies to status reports, and an unauthenticated or mismatched report SHALL NOT write a `Target_Architecture`.
6. WHEN the Portal_Backend records a `Target_Architecture` from a Quick_Setup completion report, THE Portal_Backend SHALL record an audit event containing the device name, the recorded architecture value, and the outcome, consistent with station-quick-setup Requirement 8.
7. THE recording of a `Target_Architecture` from Quick_Setup SHALL NOT alter the manual admin `PUT /api/v1/devices/{id}` architecture writer (custom-node-designer Req 16), so that an administrator can still view and override the recorded value after onboarding.

### Requirement 3: Deploy Screen Filters Gated Components by Device Target_Architecture

**User Story:** As a portal user creating or revising a deployment, I want components my target devices cannot run to be filtered out, so that I am not offered choices the backend will reject.

#### Acceptance Criteria

1. WHEN one or more target devices are selected on the Create_Revise_Deployment_Screen, THE screen SHALL determine each selected device's recorded `Target_Architecture` from the device listing and SHALL evaluate each Gated_Component's compatibility using the same rule the Arch_Gate applies: the component is compatible with a device only when the device's `Target_Architecture` is a member of the component's Supported_Architecture_Set by exact name.
2. WHEN multiple target devices are selected, THE screen SHALL treat a Gated_Component as compatible only when it is compatible with every selected target device.
3. WHEN a Gated_Component is not compatible with the selected target device(s), THE screen SHALL exclude that component from the selectable component choices offered to the user for addition to the deployment.
4. WHEN the screen excludes Gated_Components for incompatibility, THE screen SHALL make the excluded components discoverable in a clearly labeled incompatible grouping that states, per component, the device `Target_Architecture`(s) that caused the exclusion and the component's Supported_Architecture_Set, so that the exclusion is explainable rather than silent.
5. IF a selected target device has no recorded `Target_Architecture`, THEN THE screen SHALL treat every Gated_Component as incompatible for that device (failing closed, consistent with the Arch_Gate) and SHALL surface a message identifying the device(s) with no recorded architecture and indicating that the architecture must be recorded before gated components can be deployed.
6. THE screen's `Target_Architecture` compatibility filtering SHALL apply only to Gated_Components; non-gated components SHALL continue to be offered according to the screen's existing platform behavior and SHALL NOT be hidden by this filtering.
7. WHERE a target is an IoT Thing Group rather than an explicit device selection, THE screen SHALL apply the compatibility filtering according to the recorded `Target_Architecture` of the group's member devices resolvable to the screen, and WHERE member device architectures are not resolvable, THE screen SHALL not silently hide Gated_Components on the basis of architecture.
8. THE screen's compatibility filtering SHALL be a client-side aid only and SHALL NOT replace the Portal_Backend Arch_Gate; a deployment submission SHALL remain subject to the authoritative backend gates regardless of the screen's filtering.
9. WHERE a component is not a Gated_Component but encodes a JetPack major target in its component name by the DDA naming convention (for example a `-jp5`/`-jp6` suffix or an `arm64JP5`/`arm64JP6` LocalServer variant), THE screen SHALL infer that JetPack Target_Architecture from the component name and SHALL exclude the component (with the same explainable reason as criterion 4) when it does not match a selected device's recorded `Target_Architecture`, so that, for example, a `jp5` build is not offered for an `arm64_jp6` device even though both report the coarse `arm64` platform.
10. WHERE a non-Gated_Component's component name carries no JetPack token, THE screen SHALL NOT hide it on the basis of the JetPack inference (it retains the screen's existing coarse platform behavior), and WHERE a selected device has no recorded `Target_Architecture`, THE screen SHALL NOT hide a name-inferred component for that device on the basis of the JetPack inference.

### Requirement 4: Revise Mode Surfaces Now-Incompatible Installed Components

**User Story:** As a portal user revising an existing deployment, I want to see when a component already installed on the device is no longer compatible with the device's recorded architecture, so that a revision does not silently hide or drop it without my awareness.

#### Acceptance Criteria

1. WHEN the Create_Revise_Deployment_Screen enters revise mode for a target and pre-loads the existing deployment's components, IF a pre-loaded component is a Gated_Component that is not compatible with the target device's recorded `Target_Architecture`, THEN THE screen SHALL display the pre-loaded component with an indication that it is incompatible with the target's recorded architecture rather than omit it from the pre-loaded set.
2. WHEN a pre-loaded component is displayed as incompatible in revise mode, THE screen SHALL state the reason using the same device `Target_Architecture` and Supported_Architecture_Set detail as Requirement 3.4.
3. THE surfacing of a now-incompatible pre-loaded component SHALL NOT by itself block the user from removing that component or from proceeding, so that the user retains control over the revision.

### Requirement 5: Compatibility Determination Is Consistent and Fails Closed

**User Story:** As a platform owner, I want the deploy-screen compatibility logic to match the backend gate semantics exactly, so that the screen never offers a component the backend will reject and never hides a component the backend would accept.

#### Acceptance Criteria

1. THE screen's Gated_Component compatibility predicate SHALL match the Portal_Backend Arch_Gate predicate for the same inputs: exact-name membership of a device's `Target_Architecture` in a component's Supported_Architecture_Set, with an absent device `Target_Architecture` and an empty Supported_Architecture_Set both treated as incompatible.
2. IF the screen cannot resolve a Gated_Component's Supported_Architecture_Set, THEN THE screen SHALL treat that component as incompatible (fail closed) rather than offer it as compatible.
3. THE screen SHALL NOT classify a non-Gated_Component as incompatible on the basis of the `Target_Architecture` gate, so that infrastructure and non-gated components remain available regardless of device architecture.
4. WHEN no target device is selected, THE screen SHALL NOT apply `Target_Architecture` compatibility filtering to Gated_Components, so that the full catalog remains discoverable until a target defines the applicable architecture(s).

### Requirement 6: Deployments List Defaults to Newest-First

**User Story:** As a portal user, I want the deployments page to show the most recently created deployments first by default, so that the deployments I most likely care about are at the top without manual sorting.

#### Acceptance Criteria

1. WHEN the Deployments_List_Page loads or refreshes, THE Deployments_List_Page SHALL order the listed deployments by their Created_Date in descending order (newest first) by default.
2. WHERE two or more deployments share the same Created_Date, THE Deployments_List_Page SHALL apply a deterministic tie-breaker so that the listed order is stable across loads.
3. IF a listed deployment has no resolvable Created_Date, THEN THE Deployments_List_Page SHALL order that deployment after all deployments with a resolvable Created_Date rather than omit it from the list.
4. WHERE the Deployments_List_Page allows the user to sort by a column, THE default descending Created_Date ordering SHALL apply until the user chooses a different sort, and choosing a different sort SHALL override the default for the duration of that interaction.
