# Bugfix Requirements Document

## Introduction

**A workflow deployment that sets a camera binding by manual override fails with HTTP 500.** The Portal answers "Failed to create workflow deployment", and nothing is deployed. The deployments Lambda's log shows:

```
Error creating workflow deployment: No module named 'workflow_core'
  File "/var/task/deployments.py", line 4240, in create_workflow_deployment
    camera_errors, camera_warnings = validate_camera_bindings(
```

**Found** on 2026-09-29 while verifying `unified-input-camera-binding` on the Orin AGX. `POST /deployments` with `camera_bindings: {thing: {node: {"override": {"camera_id": "Fake_1"}}}}` returned 500.

**What the code and the deployed stack show:**
- `validate_camera_bindings` checks each override against the node type's declared parameters, in `_override_errors`.
  - That function and `_camera_node_descriptor` import `workflow_core.validator.check_parameter_value` and `workflow_core.catalog.get_node_type`. The imports are inside the functions, so the rest of `deployments.py` loads without the layer.
- The `DeploymentsHandler` Lambda (`deployments.handler`, ComputeStack) attaches only `sharedLayer`. In production it runs with `SharedLayer27DFABF0:89` alone.
- The `ImportError` is not caught inside `validate_camera_bindings`. It reaches `create_workflow_deployment`'s generic handler, which answers 500 `INTERNAL_ERROR`.
- The unit tests pass, because the test conftest puts the `workflow_core` layer on `sys.path`.

**Who is affected.** Anyone who binds a camera by manual override instead of choosing a registered Camera_Source.
- That includes every deployment of a camera workflow to a device that has never synced its camera registry. For such a device, override is the only binding the Portal allows (camera-registry-sync 8.8), so those deployments cannot be made at all.
- Bindings by `cameraSourceId`, and workflows with no camera node, are not affected.

**Scope of the audit.** Every Portal Lambda whose handler can reach a `workflow_core` import was checked against its deployed layers.
- Only two lack the layer: `DeploymentsHandler`, and `DevicesHandler`.
- `DevicesHandler` reaches `deployments.py` only through a lazy import of `get_device_local_server` and `local_server_component_arch`, neither of which imports `workflow_core`, so it cannot hit this error.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a workflow deployment request carries a camera binding of the form `{"override": {...}}` for any Camera_Input_Node, THEN the Portal answers HTTP 500 `INTERNAL_ERROR` "Failed to create workflow deployment", and no deployment is created.

1.2 WHEN the override would be invalid (an undeclared parameter, or a value outside the declared constraints), THEN the Portal also answers 500 instead of the 409 `CAMERA_BINDINGS_INVALID` / `CAMERA_OVERRIDE_INVALID` the code intends.

1.3 WHEN the target device has never synced its camera registry, THEN a camera workflow cannot be deployed to it at all, because override is the only binding allowed for such a device.

### Expected Behavior (Correct)

2.1 WHEN a workflow deployment carries a valid override binding, THEN the Portal SHALL validate it against the node type's declared parameters and create the deployment. It SHALL deliver the override in the device's `dda-camera-bindings` shadow, exactly as it delivers a `cameraSourceId` binding.

2.2 WHEN an override is invalid, THEN the Portal SHALL answer 409 `CAMERA_BINDINGS_INVALID`, with one `CAMERA_OVERRIDE_INVALID` error per violation naming the device, the node and the parameter.

2.3 THE `DeploymentsHandler` Lambda SHALL have the `workflow_core` package available at run time: the same `WorkflowCoreLayer` the Workflow Manager functions attach.

### Unchanged Behavior (Regression Prevention)

3.1 Deployments with `cameraSourceId` bindings, or with no camera nodes, SHALL behave exactly as today.

3.2 Every other Lambda's code, layers, environment and IAM SHALL be unchanged. No IAM statement is added or removed.

3.3 `deployments.py` SHALL keep loading without `workflow_core` on its path, so other importers such as `devices.py` are unaffected.

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(R)
  INPUT: R of type WorkflowDeploymentRequest
  OUTPUT: boolean

  RETURN EXISTS (thing, node, binding) IN R.camera_bindings :
           binding HAS KEY 'override'
           AND NOT binding HAS NON-EMPTY 'cameraSourceId'
END FUNCTION
```

### Property 1: Fix Checking

```pascal
FOR ALL R WHERE isBugCondition(R) DO
  response := deploy'(R)
  ASSERT response.status IN {201, 409}          // never 500
  ASSERT response.status = 409 IFF some override violates its node type's declared parameters
END FOR
```

### Property 2: Preservation Checking

```pascal
FOR ALL R WHERE NOT isBugCondition(R) DO
  ASSERT deploy'(R) = deploy(R)
END FOR
```
