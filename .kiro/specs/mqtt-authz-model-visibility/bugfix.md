# Bugfix Requirements Document

## Introduction

Two defects observed on the JP6 device (thing `ryanorinagxdevkithomelabjp622`, `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.45, account 164152369890, us-east-1):

**Defect 1 — MQTT publish output binding fails with UnauthorizedError.** Workflow execution `85bf7a61` failed in the `mqtt_publish` output binding. The workflow engine (inside the backend flask-app container) publishes through Greengrass IPC `PublishToIoTCore` (`output_bindings.py` `_default_greengrass_publisher`), using the LocalServer component's identity. All LocalServer recipe variants authorize `aws.greengrass.ipc.mqttproxy` operations (`SubscribeToIoTCore` + `PublishToIoTCore`) only on the shadow topic resource `$aws/things/*/shadow/name/*`. The `mqtt_publish` node's `topic` parameter is free-form and user-configured (e.g. `factory/line1/inspection`), so publishes to any workflow topic fall outside the authorized resources and Greengrass denies them with `awsiot.greengrasscoreipc.model.UnauthorizedError`. Verified on-device: the deployed 1.0.45 recipe carries exactly one mqttproxy policy with resource `$aws/things/*/shadow/name/*`.

**Defect 2 — vLLM model missing from the Deployed models page.** The component `model-vllm-opt125m-smoke` 2.0.0 is deployed and RUNNING, and the backend `/feature-configurations` endpoint returns its entry (verified on-device: `{"type":"VllmModel","modelName":"opt125m-smoke","status":"LOADING",...}`). The local frontend Deployed models page (`src/frontend/src/components/model/DeployedModels.tsx`) fetches its rows through `listModels()` in `src/frontend/src/api/FeatureConfigurationAPI.ts`, which filters the response with `isAssignableModel` — a filter introduced by the edge-vlm-workflow-fixes spec to keep vLLM models out of *legacy workflow* model assignment. That filter drops every `VllmModel` entry, so the page never shows vLLM models even though the device reports them.

Not a defect: the same device log shows a `Text_Generation_API` 409 for `opt125m-smoke` in state `loading` — the model was mid-load right after a component restart, and retry semantics already handle this transient.

## Bug Analysis

### Current Behavior (Defect)

**Defect 1 — Greengrass IPC publish authorization**

1.1 WHEN a deployed workflow runs an `mqtt_publish` output binding with `greengrass` enabled and a user-configured topic that does not match `$aws/things/*/shadow/name/*` THEN the system fails the publish with `awsiot.greengrasscoreipc.model.UnauthorizedError` from the Greengrass IPC `PublishToIoTCore` call, and the output binding does not deliver the message

1.2 WHEN any of the four LocalServer recipe variants (`recipe-arm64-jp6.yaml`, `recipe-arm64-jp5.yaml`, `recipe-arm64.yaml`, `recipe-amd64.yaml`) declares `aws.greengrass.ipc.mqttproxy` access control THEN the system authorizes `PublishToIoTCore` only on the resource `$aws/things/*/shadow/name/*`, leaving every workflow topic unauthorized

**Defect 2 — Deployed models page vLLM visibility**

1.3 WHEN the backend `/feature-configurations` response contains a `VllmModel` entry (e.g. `opt125m-smoke` for the deployed, RUNNING component `model-vllm-opt125m-smoke`) THEN the system omits that model from the Deployed models page, because the page's data source `listModels()` filters the response with `isAssignableModel`, which excludes every `VllmModel` entry

### Expected Behavior (Correct)

**Defect 1 — Greengrass IPC publish authorization**

2.1 WHEN a deployed workflow runs an `mqtt_publish` output binding with `greengrass` enabled and any user-configured topic THEN the system SHALL complete the Greengrass IPC `PublishToIoTCore` call without `UnauthorizedError`, delivering the message to AWS IoT Core

2.2 WHEN any of the four LocalServer recipe variants declares `aws.greengrass.ipc.mqttproxy` access control THEN the system SHALL include a policy entry authorizing the `PublishToIoTCore` operation for the free-form workflow topics the `mqtt_publish` node accepts (publish-only; the topic parameter is unconstrained user input, so the resource scope must cover arbitrary topics and the recipe SHALL document why)

**Defect 2 — Deployed models page vLLM visibility**

2.3 WHEN the backend `/feature-configurations` response contains a `VllmModel` entry THEN the Deployed models page SHALL list that model with its name, status, and a model type label, alongside the LFV and Triton models

### Unchanged Behavior (Regression Prevention)

**Defect 1**

3.1 WHEN the LocalServer accesses shadow pubsub topics THEN the system SHALL CONTINUE TO authorize `SubscribeToIoTCore` and `PublishToIoTCore` on `$aws/things/*/shadow/name/*` via the existing `mqttproxy:1` policy entry, unchanged

3.2 WHEN the recipe variants are parsed THEN the system SHALL CONTINUE TO carry the edge-deploy-reliability lifecycle changes (Install/Startup/Shutdown) and every other recipe section unchanged; only the `aws.greengrass.ipc.mqttproxy` access-control block gains the new publish policy (the deploy_reliability structure goldens are regenerated to reflect exactly this reviewed change)

3.3 WHEN the new publish policy is added THEN the system SHALL NOT broaden `SubscribeToIoTCore` beyond the existing shadow-topic resource (the new policy entry is publish-only)

3.4 WHEN an `mqtt_publish` node uses the plain-broker or `aws_iot` (mutual TLS) publishing paths THEN the system SHALL CONTINUE TO publish exactly as before (those paths do not use Greengrass IPC and are untouched)

**Defect 2**

3.5 WHEN the legacy workflow editor (`EditWorkflow`) builds its model assignment options THEN the system SHALL CONTINUE TO exclude `VllmModel` entries via `isAssignableModel` (legacy workflows cannot run vLLM models)

3.6 WHEN `listModels()` is called by legacy-workflow consumers THEN the system SHALL CONTINUE TO return only assignable (`LFVModel`/`TritonModel`) entries

3.7 WHEN the backend `/feature-configurations` response contains `LFVModel` or `TritonModel` entries THEN the Deployed models page SHALL CONTINUE TO list them with the same name, status, type label, and input shape rendering as before
