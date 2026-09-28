# Bugfix Requirements Document

## Introduction

**A workflow whose Input Source node (`unified_input`) is set to a camera is packaged without that camera's binding.** Packaging looks for camera nodes by their raw node type, before the Unified Input is expanded into the camera node it stands for. The node therefore gets:

- no binding point in the compiled document;
- no entry in the version item's `camera_input_nodes`.

For `source_kind: aravis_camera`, nothing on the device plans a frame grab for the node. Its `appsrc_{nodeId}` is never fed, so the Run would wait out the 120-second pipeline watchdog and fail. The deployment camera check never sees the node either, so a missing or wrong camera is not caught before deployment.

**Found by reading the code** while designing `multi-source-workflows` (2026-09-27). It has not been reproduced on a device. The first task reproduces it.

**What the code shows:**

- **Packaging uses the unexpanded graph.**
  - `workflow_packaging.py` builds binding points from `parse_definition(...).graph` (around line 2345).
  - `gather_camera_input_nodes` keeps only nodes whose `node.type` is `csi_camera_source`, `icam_source`, `aravis_camera_source`, or a camera-backed custom type.
  - `build_binding_points` and `camera_input_nodes_record` receive that list.
- **Expansion happens only in the compiler.** `expand_unified_inputs` runs only inside `workflow_core.compiler.compile()`. No Portal function calls it before packaging.
  - The compiled pipeline itself is correct: the node becomes `aravis_camera_source` with its own `appsrc_{nodeId}`.
  - The binding metadata is what is missing.
- **The device keys on the marker.** `aravis_feed.plan_aravis_feeds` plans a grab only for binding points marked `aravisBinding: true`. With none, it plans zero feeds, and `run_pipeline` runs without a frame.
- **Other camera kinds are affected too.** `SOURCE_KIND_TO_SOURCE_TYPE` maps `aravis_camera`, `csi_camera` and `icam` to camera sources.
  - For `csi_camera` and `icam`, the in-pipeline source element still renders.
  - The node loses its `csiSensorBinding` point, or its binding slots, and the per-device camera binding and the deploy-time check. It runs on whatever parameters were typed into the node.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a workflow contains a `unified_input` whose `source_kind` maps to a camera source (`aravis_camera`, `csi_camera` or `icam`), THEN packaging emits no binding point for that node.

1.2 WHEN such a workflow is packaged, THEN the version item's `camera_input_nodes` omits the node. `validate_camera_bindings` then neither asks for a camera for it nor checks one on any target device.

1.3 WHEN the `source_kind` is `aravis_camera` and the package runs on a device, THEN no frame is grabbed for the node's `appsrc_{nodeId}`. The Run would fail at the 120-second pipeline watchdog instead of capturing. This is inferred from the code and is confirmed by task 1.

1.4 WHEN the `source_kind` is `csi_camera` or `icam`, THEN the device runs the node on its rendered parameters, with no per-device camera binding.

### Expected Behavior (Correct)

2.1 WHEN a `unified_input` maps to a camera source, THEN packaging SHALL emit the same binding point it emits for the equivalent dedicated node:
- `aravisBinding: true`, `csiSensorBinding: true`, or the binding slots;
- `nodeType` set to the effective type;
- parameters rendered with the effective type's descriptor;
- the node's binding hint.

2.2 WHEN such a workflow is packaged, THEN `camera_input_nodes` SHALL include the node with its effective type, so that the deployment camera check covers it exactly as it covers the dedicated node.

2.3 WHEN a workflow with a `unified_input` set to `aravis_camera` runs on a device, THEN the device SHALL grab the bound camera's frame for the node, as it does for `aravis_camera_source`. This needs no device change.

### Unchanged Behavior (Regression Prevention)

3.1 Packages for workflows without a camera-kind `unified_input` SHALL be byte-identical to today's, excluding `packagedAt`: compiled documents, binding points, manifests, recipes and version items.

3.2 A `unified_input` with `source_kind: folder` SHALL package exactly as today.

3.3 Workflows that use the dedicated `aravis_camera_source`, `csi_camera_source` and `icam_source` nodes SHALL package exactly as today.

3.4 The compiled pipeline document for a camera-kind `unified_input` SHALL be unchanged. Only the binding metadata is added.

3.5 The LocalServer SHALL need no change. Existing packages SHALL keep running unchanged.

## Out of Scope

- **The builder's camera picker for a Unified Input.** Today the camera id is typed into the node. It is not needed for this fix, because per-device binding happens at deployment. It could be a follow-up.
- **Validator counting by effective type.** The validator counts frame-feed sources by raw type, so a Unified Input plus an Aravis node passes validation and fails at compile. `multi-source-workflows` Decision 1 covers this.

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(W)
  INPUT: W of type WorkflowDefinition
  OUTPUT: boolean

  // A workflow with at least one Unified Input standing for a camera.
  RETURN EXISTS n IN W.nodes :
           n.type = 'unified_input'
           AND SOURCE_KIND_TO_SOURCE_TYPE[n.parameters.source_kind OR 'folder']
               IN { 'aravis_camera_source', 'csi_camera_source', 'icam_source' }
END FUNCTION
```

### Property 1: Fix Checking

```pascal
FOR ALL W WHERE isBugCondition(W) DO
  E := W with every camera-kind unified_input rewritten into its effective node type
  ASSERT bindingPoints(package'(W)) = bindingPoints(package(E))
  ASSERT camera_input_nodes(package'(W)) = camera_input_nodes(package(E))
  ASSERT compiledPipeline(package'(W)) = compiledPipeline(package(W))
END FOR
```

### Property 2: Preservation Checking

```pascal
FOR ALL W WHERE NOT isBugCondition(W) DO
  ASSERT package'(W) = package(W)      // byte-identical, excluding packagedAt
END FOR
```
