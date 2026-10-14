# Design Document

## Overview

Packaging reads Camera_Input_Nodes from the same graph `compile()` compiles. `package_workflow` passes `expand_unified_inputs(graph, catalog)`, the compiler's own pre-pass, to `gather_camera_input_nodes`. An Input Source set to a camera then gets exactly the binding point and `camera_input_nodes` record of the dedicated node it stands for, under its own node id.

The change is one call site in `edge-cv-portal/backend/functions/workflow_packaging.py`. The `workflow_core` layer, the deployment check, the frontend and the LocalServer do not change.

## Root Cause

`package_workflow`, from about line 2341:

```python
graph = parse_result.graph                           # the saved graph: node.type == 'unified_input'
...
result = compile_workflow(graph, arch, compile_context, simulation=False,
                          catalog=catalog)           # compile() expands unified_input on a copy
...
camera_nodes = gather_camera_input_nodes(
    graph, camera_backed_type_ids(resolved_items))   # reads the unexpanded graph
```

`compile()` expands a copy of the graph first, so the compiled document holds the effective node: an `aravis_camera_source` with its own `appsrc_{nodeId}`, a `csi_camera_source`, or an `icam_source` with its `v4l2src`. The gather reads the saved graph, where the node's type is still `unified_input`, and skips it. `bindingPoints`, `has_binding_points` and `camera_input_nodes` all derive from that list, so all three miss the node.

## Fix

```python
from workflow_core.compiler.compiler import expand_unified_inputs
...
    # Camera_Input_Nodes come from the graph compile() compiled:
    # expand_unified_inputs rewrites each unified_input into the source node
    # it stands for, under the same id, so an Input Source set to a camera
    # gets the dedicated node's binding point and camera_input_nodes record
    # (unified-input-camera-binding 2.1, 2.2).
    camera_nodes = gather_camera_input_nodes(
        expand_unified_inputs(graph, catalog),
        camera_backed_type_ids(resolved_items))
```

- **The compiler's rules, not a copy of them.** `expand_unified_inputs` is the pre-pass `compile()` runs, called here with the same merged catalog. The expanded node keeps the Input Source's id, position and data, takes the effective type, and keeps only that type's parameters. `build_binding_points` therefore renders the parameters with the effective type's descriptor, finds the binding hint by node id, and names the `appsrc_{nodeId}` the compiler emitted.
- **Import.** `workflow_core.compiler` does not re-export the function, so it is imported from `workflow_core.compiler.compiler`, as the layer's own tests do. The layer itself is unchanged, so there is no re-vendoring and no new layer version.
- **Only the camera gather changes.** `gather_python_source_nodes`, `gather_custom_python_nodes` and the compile calls keep reading the saved graph, and `binding_hints_from_definition` keeps reading the saved definition. No `source_kind` expands to a Python source.
- **Unchanged outside the bug condition.** Expansion returns every other node as an equal copy, in the same order. A folder-kind Input Source becomes a `folder_source`, which the gather skips. The gathered list is therefore value-equal to today's. It feeds only three outputs:
  - `bindingPoints`, in `compiled_pipeline.json` (both in the zip and in the Portal copy);
  - `has_binding_points`;
  - `camera_input_nodes`.

  Manifests, recipes and the packaged definition do not read it.
- **The pipeline itself is unchanged** (3.4). `compile()` already expanded the node, so the document only gains its `bindingPoints` section. With binding points, the packager serializes in the compiler's own format (sorted keys, 2-space indent, ASCII), so the text differs from today's only by that key.
- **No new failure mode.** An Input Source with a missing or unknown `source_kind` never reaches the gather, because `compile()` refuses that node first.

**Rejected alternatives:**
- **Resolving effective types inside `gather_camera_input_nodes`.** It would be a second copy of the expansion rules, including the parameter filtering, which could drift from the compiler.
- **Passing the expanded graph to `compile()` as well.** `compile()` expands anyway. Changing its input for every workflow buys nothing and widens what must be shown unchanged.
- **A device-side fallback that reads `workflow.json`.** It needs new LocalServer builds for every variant and still leaves the deployment camera check blind to the node.
- **Rewriting existing version items.** A version can be packaged again, with its component version bumped, and that picks up the fix.

## What Changes for Users

- **Versions packaged after the fix.** A camera-kind Input Source appears in the CreateDeployment camera step. It must be bound, or given a manual override, on every target device, exactly like the dedicated node. Nothing is pre-selected: the builder's camera picker records a binding hint only for `icam_source` and `aravis_camera_source` nodes (`isCameraReferenceParameter`), and the picker for an Input Source is out of scope.
- **Versions packaged before the fix** keep their recorded fields. They deploy without a camera step, and an Aravis one still gets no frame on the device. Packaging the version again fixes it.
- **Devices.** Nothing changes (3.5). When no binding is delivered, the device grabs from the camera id typed into the node, which the binding point carries in its parameters, as it does for the dedicated node.

## Effect on multi-source-workflows

That design leaves this gap to this bugfix, so that its single-source byte-identity holds. Once this fix is merged:
- its "Pre-existing gap" section becomes a dependency on this fix;
- Decision 4's binding bullet is no longer multi-source only, because every package already binds a camera-kind Input Source by effective type, and `gather_frame_feed_source_nodes` can build on the expanded graph;
- the `unified_input(aravis_camera)` golden in its task 3.1 records the fixed output, since its base commit includes this fix.

Task 8 makes those edits.

## Testing Strategy

The tests are in `edge-cv-portal/backend/tests/test_unified_input_camera_binding.py` and use Hypothesis with the repo profiles: 25 examples by default, 100 with `HYPOTHESIS_PROFILE=ci`. The module imports `workflow_packaging` and `deployments` inside module-scoped fixtures that depend on `aws_stack`, never at module level, following the workspace rule for these suites.

**Through the real handler.** The bug is in the handler's call sequence, so the tests package through `workflow_packaging.handler` (`POST /workflows/{id}/package`). A test of the pure helpers alone would miss it.
- The harness follows `test_workflow_packaging_binding_points.py`: one use case with its S3 bucket, a UseCaseAdmin user, and the use-case clients patched (S3 to moto, Greengrass to a DEPLOYABLE stub).
- Each packaging creates a fresh workflow through `workflows.handler` and marks its version validated.
- The harness is module-scoped, so Hypothesis can drive it.

**Property 1, fix checking (the exploration test).**
- The generator builds 1 to 3 source-to-capture chains:
  - at least one chain starts with an Input Source of kind `aravis_camera`, `csi_camera` or `icam`. It carries that kind's parameters, parameters that belong only to other kinds (such as `location` or `device` on an Aravis input), and an optional `cameraBindingHint`;
  - the other chains start with dedicated camera nodes or folder sources;
  - there is at most one Aravis source in total, because V7 allows one.
- E is the same workflow with each camera-kind Input Source written as the dedicated node, with the same id and data and only the kind's parameters. The generator builds E itself; it does not call `expand_unified_inputs`.
- W and E are packaged for every device architecture. The test asserts:
  - per architecture, `bindingPoints(W) = bindingPoints(E)`;
  - the version items' `camera_input_nodes` and `has_binding_points` are equal;
  - W's compiled document without `bindingPoints` equals the plain compiler output for W, and its text is that output plus `bindingPoints` in the packager's serialization;
  - for an Aravis kind, the device's own `plan_aravis_feeds` plans exactly one feed, for the Input Source's id, with its typed camera id. The planner is loaded by path from `src/backend/workflow_engine/aravis_feed.py`, as `test_video_loop_parity_properties.py` loads device code, and the check is skipped if the device tree is absent.
- **Expected on the unfixed code: FAIL.** W's documents have no `bindingPoints`, and its `camera_input_nodes` is empty.

**Example tests**, one per kind, so that a failure reads plainly. They also fail on the unfixed code.
- Aravis: `aravisBinding: true`, empty slots, `nodeType: aravis_camera_source`, and the rendered `camera_id`, `gain` and `exposure`. `plan_aravis_feeds` plans one feed.
- CSI: `csiSensorBinding: true`, empty slots, and the rendered `gain` and `exposure`.
- ICAM: one slot addressing the `v4l2src` `device` argument, and `compiled_device_paths` for every architecture.
- Deployment check: `validate_camera_bindings` on the packaged version item, for one target with no binding, returns one unbound-camera error naming the Input Source. With a compatible Camera_Source bound, it returns no error.

**Property 2, preservation.**
- The generator builds 1 to 3 chains outside the bug condition, with optional hints:
  - dedicated `icam_source`, `csi_camera_source` and `aravis_camera_source` nodes, with at most one Aravis;
  - `folder_source` nodes;
  - folder-kind Input Sources carrying camera parameters.
- The oracle is today's handler path, rebuilt from the unchanged pure helpers over the stored graph: `gather_camera_input_nodes`, `build_binding_points`, `compiled_document_json` and `camera_input_nodes_record`, with `has_binding_points = bool(camera_nodes)`. This is the pattern of `test_property_aravis_free_packaging_identity.py`. The stored graph is the serializer's canonical form that the workflows handler saves, which can reorder nodes, so every oracle compiles it rather than the generated definition.
- The test asserts that each architecture's packaged `compiled_pipeline.json` text equals the oracle's byte for byte, and that the version item's fields equal the oracle's.
- **Expected on the unfixed code: PASS.**

**Existing suites** that must pass unedited:
- `test_workflow_packaging_binding_points.py`, `test_property_aravis_binding_points.py` and `test_property_aravis_free_packaging_identity.py`;
- the camera-binding suites;
- the layer's `test_unified_input_expansion.py`;
- then every backend module that imports `workflow_packaging`, and the full Portal backend suite, run in shards and compared with a base worktree.

**On the Portal and devices.** The code change is Portal-only, but the symptom (1.3) and the result (2.3) are on the device, and the deployment check (2.2) runs in the deployed Portal. The checks use the Orin AGX (JP6, with the Aravis Fake camera `Fake_1`) and thor1 (JP7, with its Basler). The Dell was planned first, but it has no Portal use case, so the Portal cannot deploy to it; the Orin is in thor1's use case.
- **Before the Portal deploy**, on today's Portal:
  - Workflow `uicb-repro` is an Input Source set to `aravis_camera` with camera id `Fake_1`, feeding a capture. One copy per device is packaged for that device's architecture only, because a multi-architecture package pulls every listed LocalServer variant onto the device. A Run is triggered on each through the engine API (`POST /workflows/registrations/{id}/trigger`).
  - Expected: no "Aravis frame feed planned" line, and the Run fails at the pipeline watchdog after about 120 s.
  - Two controls, a dedicated `aravis_camera_source` workflow and a folder workflow, are packaged, and their compiled documents are kept.
- **After the Portal deploy:**
  - Both controls are packaged again. Their compiled documents are byte-identical and their version items unchanged (3.1 to 3.3).
  - `uicb-repro` is packaged again. Its version item records the Input Source as `aravis_camera_source`, and the binding-context endpoint lists it.
  - A deployment with no camera binding is refused, naming the node.
  - A deployment with a binding succeeds. The Orin binds its `Fake_1` camera source. thor1 binds its Basler while the node's typed id stays `Fake_1`, so the log shows that the binding, not the typed id, was used.
  - The Runs complete with a capture, the executor logs the feed planned for the bound camera, and the backend stays healthy with no restart.
- **Not run on hardware:** CSI and ICAM Input Sources. They need a CSI sensor or a V4L2 smart camera on a lab device. Their packaging and deployment check are covered by the tests above, and on the device they use the same binding markers the dedicated nodes already use.
- **Afterwards:** the device deployments are restored. The test workflows, their components, S3 artifacts, camera-binding shadow keys and device workflow directories are removed, and so is the temporary Portal principal.
