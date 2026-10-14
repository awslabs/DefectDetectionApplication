# Bugfix Requirements Document

## Introduction

The portal's "Deployed workflows" view (backed by the device backend's `GET /workflows/registrations`) lists every workflow version ever deployed to a device, not just the currently-deployed one. Verified live on the JP6 device: the Greengrass deployment contains only `dda.workflow.1f0b4c0c-...` component version 7.0.0, but the backend lists registrations for versions 2, 6, AND 7 — all with status `registered` — and `/aws_dda/workflows/1f0b4c0c-.../` holds directories `2/`, `6/`, and `7/`.

Two defects compound:

1. **Portal packaging defect**: the generated `dda.workflow.{workflowId}` Greengrass recipe has only a one-shot `Run` lifecycle (`mkdir -p` + `cp -r` into `/aws_dda/workflows/{id}/{version}`). There is no `Shutdown` cleanup, so when Greengrass replaces component version N with N+1 (or removes the component), version N's staged files remain on disk forever.
2. **Device engine defect**: the WorkflowWatcher registers every `{workflowId}/{version}` directory it finds as an active registration, and the listing endpoint returns every row. Nothing ever retires a version: the registration list only grows, and superseded versions remain indistinguishable from the deployed one.

Impact: operators cannot trust the deployed-workflows view (stale versions appear runnable and can be triggered), and stale staged artifacts accumulate on device disk indefinitely.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN Greengrass replaces a `dda.workflow.{workflowId}` component version with a newer one (or removes the component from the deployment) THEN the system leaves the outgoing version's staged files under `/aws_dda/workflows/{workflowId}/{version}` on disk indefinitely, because the generated recipe carries no Shutdown/cleanup lifecycle step

1.2 WHEN the device backend scans `/aws_dda/workflows/` and finds multiple version directories of the same workflow THEN the system registers every version directory with status `registered`, including versions that are no longer deployed via Greengrass

1.3 WHEN `GET /workflows/registrations` is called THEN the system returns every historical registration as active (e.g., versions 2, 6, and 7 all `registered` while only component version 7.0.0 is deployed), so the deployed-workflows view lists workflows that are not actually deployed and allows triggering them

1.4 WHEN a workflow version's artifact directory is removed from disk THEN the system marks its registration `invalid` (reason "Artifact directory was removed") but continues to include it in the deployed-workflows listing indefinitely, indistinguishable from a genuinely broken deployed artifact set

### Expected Behavior (Correct)

2.1 WHEN Greengrass replaces or removes a `dda.workflow.{workflowId}` component version THEN the generated recipe SHALL remove the outgoing version's staged directory `/aws_dda/workflows/{workflowId}/{version}` via a Shutdown lifecycle step (safe because the Run step re-copies the artifacts on every component (re)start)

2.2 WHEN a registration's artifact directory no longer exists on disk THEN the system SHALL mark that registration with a distinct non-active status (`removed`) while preserving the registration row and its execution history in the device database

2.3 WHEN multiple version directories of the same workflow exist on disk (legacy accumulation from recipes without cleanup) THEN the system SHALL treat only the highest numeric version as the deployed one and mark lower numeric versions with a distinct non-active status (`superseded`), preserving their rows and execution history

2.4 WHEN `GET /workflows/registrations` is called THEN the system SHALL by default return only active registrations (statuses `registered` and `invalid`); WHEN called with `includeInactive=true` THEN the system SHALL additionally return `removed` and `superseded` registrations so historical execution data remains reachable

2.5 WHEN a trigger is attempted on a `removed` or `superseded` registration THEN the system SHALL reject the trigger as non-runnable (HTTP 409), the same guard that already protects `invalid` registrations

2.6 WHEN a non-active registration's artifact directory becomes the highest version present again (e.g., a rollback redeployment re-copies it, or a re-copied directory reappears after a component restart) THEN the system SHALL flip that registration back to `registered` (or `invalid` per artifact validation) on the next scan

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a workflow version is the currently-deployed one (its directory is the highest version present and its artifacts validate) THEN the system SHALL CONTINUE TO list it as `registered` with the exact existing payload shape (registrationId, workflowId, name, version, arch, artifactPath, status, registeredAt), retain its execution history, and run it on trigger exactly as before

3.2 WHEN a workflow is packaged THEN the system SHALL CONTINUE TO emit every existing recipe field unchanged apart from the added Shutdown step: the one-shot Run copy script, ComponentDependencies (dda.plugin.* entries, model component entries, per-arch LocalServer floors), ComponentConfiguration, platform manifests/ordering/attributes, artifact URIs, and the packaged llm_inference modelName rewrite

3.3 WHEN a structurally malformed or incompatible artifact set is present on disk for a deployed (highest) version THEN the system SHALL CONTINUE TO register it as `invalid` with a reported reason, list it, and reject triggers against it

3.4 WHEN `GET /workflows/registrations/{registration_id}` is called for any known registration id (active or not) THEN the system SHALL CONTINUE TO return the registration with its executions history

3.5 WHEN a device has no workflow components (empty or absent `/aws_dda/workflows/`) THEN the system SHALL CONTINUE TO behave identically to today (empty listing, no side effects)

3.6 WHEN Greengrass (re)starts a workflow component THEN the system SHALL CONTINUE TO re-copy the artifacts via the Run step, and a directory that reappears SHALL CONTINUE TO flip its registration back to an active status on the next scan (existing invalid-to-registered flip-back behavior, extended to the new non-active statuses)
