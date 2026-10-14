# Design Document

## Overview

The `DeploymentsHandler` Lambda attaches the ComputeStack's existing `WorkflowCoreLayer`, alongside `sharedLayer`:

```ts
layers: [sharedLayer, workflowCoreLayer],
```

This is the layer the Workflow Manager functions already attach (workflows, validation, packaging, generator, tuning). With it, the override check in `validate_camera_bindings` can import `workflow_core`, so a valid override deploys and an invalid one gets the intended 409.

No Python code changes. `deployments.py` keeps its lazy imports, so it still loads without the layer wherever it is imported.

## Root Cause

`_camera_node_descriptor` and `_override_errors` in `backend/functions/deployments.py` import `workflow_core.catalog.get_node_type` and `workflow_core.validator.check_parameter_value` inside the functions. The comment there says that only override validation needs the layer. The ComputeStack gave the function only `sharedLayer`, so in Lambda that import raises `ModuleNotFoundError`. The error escapes `validate_camera_bindings` and is answered as a generic 500.

The unit tests never saw it, because `tests/conftest.py` puts `backend/layers/workflow_core/python` on `sys.path` for every module.

## Fix

- **One line in `edge-cv-portal/infrastructure/lib/compute-stack.ts`:** add `workflowCoreLayer` to the `DeploymentsHandler`'s `layers`. The layer construct is defined earlier in the same stack, so no new resource is created.
- **Size.** `WorkflowPackagingHandler` already runs the same code asset with the same two layers, well inside Lambda's 250 MiB unzipped limit.
- **IAM.** No change. Same-account layers need no permission.

**Rejected alternatives:**
- **Catch the ImportError and fail closed.** Override deployments would still be impossible, just with a different error. The code already treats "no descriptor" as fail-closed; the missing layer is a wiring defect, not a data condition.
- **Copy the parameter descriptors into `deployments.py`.** A second copy of the catalog constraints could drift from the one the compiler and the device use.
- **Also attach the layer to `DevicesHandler`.** It imports only `get_device_local_server` and `local_server_component_arch` from `deployments.py`, which never reach `workflow_core`. Attaching it would change a second function for no reachable benefit.

## Testing Strategy

**Infra test:** `edge-cv-portal/infrastructure/test/deployments-workflow-core-layer.test.ts` (jest, CDK assertions), synthesizing the ComputeStack the way the existing infra tests do.

**Property 1, the bug condition.**
- The function whose handler is `deployments.handler` attaches `sharedLayer` and the same `WorkflowCoreLayer` Ref as `workflow_packaging.handler`.
- A general guard: for every ComputeStack function whose handler module in `backend/functions` contains a `workflow_core` import (at module level or inside a function), the function attaches a `WorkflowCoreLayer`.
- **Expected on the unfixed code: FAIL.** `deployments.handler` has only `SharedLayer`, and it is the only offender.

**Property 2, preservation.**
- The ComputeStack template is synthesized before and after the change, and the two templates are compared. The only difference allowed is the `Layers` property of `DeploymentsHandler`.
- The existing infra jest suite and the IAM synth gate must pass unchanged.
- The Python override tests, such as `test_camera_binding_validation.py` and `test_camera_binding_submission.py`, already cover the override logic with the layer on the path. They must keep passing.

**In the deployed Portal**, with a temporary principal and a test workflow on a lab device:
- **Before the deploy:** an override deployment answers 500. This was already observed on 2026-09-29.
- **After the deploy:**
  - an invalid override (`gain: 500`) answers 409 `CAMERA_OVERRIDE_INVALID`;
  - a valid override answers 201, and the device's `dda-camera-bindings` shadow carries the override;
  - on the device, the workflow registers and a Run grabs the overridden camera.
- **Preservation:** a `cameraSourceId` deployment still succeeds.
- **Afterwards:** everything is cleaned up as in `unified-input-camera-binding`.
