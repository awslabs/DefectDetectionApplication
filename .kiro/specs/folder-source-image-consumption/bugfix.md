# Bugfix Requirements Document

## Introduction

A deployed workflow using a `folder_source` input never progresses past the first image in the folder: every run re-processes the same (oldest by mtime) image. The legacy Pipeline_Configuration path (`gst_pipeline_executor.execute_workflow_pipeline`) consumes the processed folder image after a successful pipeline run (`_cleanup_file_after_processing`) and relocates the image to a `failed/` subfolder when the pipeline run fails (`_move_bad_folder_image_source`), so the folder always drains. The new workflow engine (`workflow_engine/pipeline_executor.py`) resolves a directory `location` to the oldest JPEG (`_oldest_image_in_folder`) and Pillow-stages a `.dda_decoded.png` on JP6 (`_stage_decoded_png`), but has NO post-run consumption path at all. Observed on device: repeated runs of the vlm-smoketest workflow against `/aws_dda/yolotest` always resolved the same `zidane.jpg` (staged as `zidane.jpg.dda_decoded.png`), and staged PNGs accumulated.

## Bug Analysis

### Current Behavior (Defect)

The workflow engine's `execute()` run flow never deletes, relocates, or otherwise marks folder-resolved source images as processed.

1.1 WHEN a workflow run with a directory `folder_source` location completes successfully THEN the system leaves the resolved source JPEG in the folder, so every subsequent run re-selects and re-processes the same oldest image (the folder never drains)

1.2 WHEN the JP6 staged-PNG path creates a `<file>.jpg.dda_decoded.png` for the resolved JPEG THEN the system never deletes the staged PNG, so staging files accumulate in the source folder

1.3 WHEN a folder-resolved image fails to decode/stage (bad or corrupt image) THEN the system fails the run but leaves the bad image in place, so the same bad image wedges every subsequent run of that workflow

1.4 WHEN the pipeline run itself fails for a folder-resolved frame THEN the system leaves the resolved image in place (the legacy path relocates it to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` so the folder still drains)

### Expected Behavior (Correct)

Mirror the legacy `execute_workflow_pipeline` folder-source semantics: consume on pipeline success, relocate on failure, never touch non-folder sources.

2.1 WHEN a run's pipeline processing succeeds for a folder-resolved frame THEN the system SHALL delete the resolved source JPEG (via `captured_images_utils.delete_image` or equivalent), mirroring `_cleanup_file_after_processing`

2.2 WHEN a staged `.dda_decoded.png` was created for the resolved JPEG THEN the system SHALL delete the staged PNG along with the original JPEG

2.3 WHEN the pipeline run succeeds but a downstream step fails (Bedrock/LLM processing, output bindings) THEN the system SHALL still consume the folder-resolved frame — legacy cleans up after pipeline processing regardless of downstream outcomes (cleanup lives in the `else` of the pipeline try/except; verified in `execute_workflow_pipeline`)

2.4 WHEN a folder-resolved image fails to decode/stage (FrameSourceError from `_stage_decoded_png`) THEN the system SHALL relocate the bad source JPEG to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` before failing the run, mirroring `_move_bad_folder_image_source`

2.5 WHEN the pipeline run itself fails for a folder-resolved frame THEN the system SHALL relocate the resolved source JPEG to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` (mirroring the legacy `except` branch) and best-effort remove any staged PNG

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the workflow's frame sources are non-folder (camera, CSI, ICAM, Aravis, or a single-file `location` that is not a directory) THEN the system SHALL CONTINUE TO run exactly as before — single-file sources are never consumed (legacy gates consumption on `ImageSourceType.FOLDER`; the resolution code distinguishes `os.path.isdir(location)`)

3.2 WHEN a directory location holds multiple images THEN the system SHALL CONTINUE TO select the oldest `.jpg`/`.jpeg` by mtime (`_oldest_image_in_folder` selection order unchanged)

3.3 WHEN a run completes THEN the system SHALL CONTINUE TO produce the same captured outputs, run metadata, execution-row status, and output-binding behavior as before the fix

3.4 WHEN a run fails in any existing failure mode THEN the system SHALL CONTINUE TO record the same `failed` status, error message, and `failing_node_id` attribution as before the fix
