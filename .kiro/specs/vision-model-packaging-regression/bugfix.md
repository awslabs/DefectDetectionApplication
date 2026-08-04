# Bugfix Requirements Document

## Introduction

Regression introduced by the edge-deploy-reliability Defect C change: `workflow_packaging.py::resolve_model_components` fails closed when a referenced model's registry record has no `published_component` (singular dict) — but only vLLM publishes write that attribute. Vision/ONNX publishes (greengrass_publish.py) write `published_components` (plural, per-target LIST of `{component_name, target, component_version, status}` entries, e.g. `model-yolo-test-jetson-xavier-jp5`). Packaging any workflow referencing a published vision model now fails with "Model '<name>' referenced by the workflow has no published Greengrass component; publish the model before packaging workflows that use it" (verified live: workflow 6075bf76 v3, model yolo_test, Lambda log 04:38 UTC; the DynamoDB record has `published_component: null` but `published_components` with a `status: published` jp5 entry).

Vision component names are also PER-TARGET, so the model dependency emission must follow the Defect F single-variant discipline: emit a HARD entry only when the selected architectures resolve to exactly one published component name for that model; omit that model's entry (log it) when targets/entries diverge.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a workflow references a vision/ONNX model whose training record carries `published_components` (plural list) but no singular `published_component` THEN `resolve_model_components` raises PackagingError claiming the model is unpublished, and packaging fails — a regression for previously packageable workflows

1.2 WHEN the registry snapshot record is inspected THEN only the vLLM publish shape (singular `published_component` dict) is consulted; the vision publish shape written by greengrass_publish.py is never read

### Expected Behavior (Correct)

2.1 WHEN a referenced model's record carries a singular `published_component` with a `component_name` (vLLM shape) THEN the system SHALL resolve it exactly as today

2.2 WHEN a referenced model's record carries `published_components` (plural) THEN the system SHALL consider entries whose `status` is `published` and resolve the component name(s) relevant to the workflow's selected architectures (arch→target per greengrass_publish.TARGET_TO_LOCAL_SERVER discipline: arm64_jp5→jetson-xavier-jp5, arm64_jp6→jetson-xavier-jp6, x86_64/x86_64_nvidia→the x86 target naming greengrass_publish uses)

2.3 WHEN the selected architectures resolve to exactly ONE published component name for the model THEN the system SHALL emit that single HARD dependency entry (unpinned, as today)

2.4 WHEN the selected architectures resolve to MULTIPLE distinct component names for the model (per-target names diverge) THEN the system SHALL omit that model's dependency entries and log a warning naming the model and the divergent components (Defect F discipline — deployability over the ordering edge)

2.5 WHEN a model has NO published component in either shape (no singular dict, no plural entry with status published) THEN the system SHALL CONTINUE TO fail closed with the existing PackagingError message

2.6 WHEN a selected architecture has no matching published target entry (even though other targets ARE published) THEN the system SHALL FAIL CLOSED with a PackagingError naming the model AND the uncovered architecture/target, and listing the targets the model IS published for — an accurate coverage error the user can act on by re-publishing for the missing target. (Supersession note: reconciled with edge-deploy-reliability Defect G 2.19 — fail-closed accuracy wins over warn-and-proceed; the deployment-time arch gates cannot help a workflow packaged for an arch its model was never published for.)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN vLLM-referencing workflows are packaged THEN resolution and emitted dependencies SHALL CONTINUE TO be byte-identical to today

3.2 WHEN a workflow references no models THEN packaging SHALL CONTINUE TO emit no model entries; plugin and LocalServer dependency emission (including the Defect F single-variant rule) SHALL CONTINUE TO behave identically

3.3 WHEN TRAINING_JOBS_TABLE is not configured THEN model dependencies SHALL CONTINUE TO be skipped with the existing warning

3.4 WHEN a referenced model has no registry record at all THEN the existing "no record in the Use_Case model registry" PackagingError SHALL CONTINUE TO apply
