# Folder Source Image Consumption Bugfix Design

## Overview

Deployed workflows with a `folder_source` input re-process the same oldest image forever because the new workflow engine (`src/backend/workflow_engine/pipeline_executor.py`) resolves the frame but never consumes it. The legacy path (`src/backend/gstreamer/gst_pipeline_executor.py`) drains the folder: `_cleanup_file_after_processing` deletes the processed input (and the original JPEG behind a JP6 staged PNG) after a successful pipeline run, and `_move_bad_folder_image_source` relocates failing images to a `failed/` subfolder so a bad image can't wedge the folder.

The fix records the (original JPEG, staged PNG) pairs that `_stage_frame_sources` resolves from directory locations, then mirrors the legacy post-pipeline semantics inside `execute()`: on pipeline success, delete the pair (regardless of downstream Bedrock/LLM/output-binding outcomes); on pipeline failure, relocate the original to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`; on decode/stage failure, relocate the bad image before failing the run. Single-file (non-directory) locations and all non-file sources are untouched.

This is a device-side change riding a LocalServer build. A JP6 build for other fixes is currently in flight; this fix targets the NEXT build.

## Glossary

- **Bug_Condition (C)**: A workflow run whose compiled document contains a `filesrc` element whose `location` is a directory — i.e. `_stage_frame_sources` resolved the frame via `_oldest_image_in_folder`
- **Property (P)**: After the run's pipeline processing completes, the resolved source JPEG (and any staged `.dda_decoded.png`) is removed from the folder (success) or relocated to `failed/` (failure), so the next run selects the next-oldest image
- **Preservation**: Non-folder sources (camera, CSI, ICAM, Aravis, single-file `filesrc`), frame selection order, run outputs/metadata/status, and output bindings behave identically to the unfixed code
- **Folder_Frame**: A `(original_jpeg, staged_png_or_None, node_id)` triple recorded when `_stage_frame_sources` resolves a directory location
- **`_stage_frame_sources`**: Static method (~line 561) that mutates the per-run document, resolving directory locations to the oldest JPEG and Pillow-staging a PNG when the compiled chain expects `pngdec` (JP6)
- **`_oldest_image_in_folder`**: Helper (~line 285) selecting the oldest `.jpg`/`.jpeg` by mtime — mirrors `captured_images_utils.get_oldest_image_file_path`
- **`_stage_decoded_png`**: Helper (~line 314) writing `<file>.dda_decoded.png` via Pillow (JP6 libdlr/libjpeg collision workaround)
- **`_cleanup_file_after_processing`**: Legacy cleanup in `gst_pipeline_executor.py` (~line 138): deletes the pipeline input via `captured_images_utils.delete_image`, and when the input ends with `.dda_decoded.png`, also deletes the original source JPEG
- **`_move_bad_folder_image_source`**: Legacy relocation: `os.rename` into `{constants.INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` (directory created via `dda_user_management_utils.create_dda_user_directory`)

## Bug Details

### Bug Condition

The bug manifests on every workflow run whose document contains a `filesrc` with a directory `location`. `_stage_frame_sources` resolves the frame and mutates `args["location"]` in place, but nothing records what was resolved and no code path after the pipeline run deletes or relocates the source image. The same oldest JPEG is re-selected by mtime on every execution.

**Formal Specification:**
```
FUNCTION isBugCondition(run)
  INPUT: run of type WorkflowExecutionRun
  OUTPUT: boolean

  RETURN EXISTS element IN run.document.segments[*].elements
         WHERE element.factory = "filesrc"
           AND os.path.isdir(element.args.location)
  // i.e. the run resolved at least one folder-source frame
END FUNCTION
```

### Examples

- Device run: `/aws_dda/yolotest` holds `zidane.jpg` (oldest), `bus.jpg`, `cat.jpg`. Every execution of the vlm-smoketest workflow resolved `zidane.jpg` (staged as `zidane.jpg.dda_decoded.png`). Expected: run 1 processes and deletes `zidane.jpg` + its staged PNG; run 2 processes `bus.jpg`; run 3 processes `cat.jpg`; run 4 fails with "No .jpg/.jpeg image files found"
- JP6 staging accumulation: after N runs the folder holds `zidane.jpg` plus a repeatedly-rewritten `zidane.jpg.dda_decoded.png`. Expected: no leftover staging files after a run
- Corrupt image: a truncated JPEG as the oldest file fails `_stage_decoded_png` on every run — the workflow is permanently wedged. Expected: the bad file is relocated to `failed/` and the next run proceeds to the next image
- Non-bug case: `location` is a single file (`/aws_dda/fixed/frame.jpg`) — legacy FILE-type sources are never consumed; repeated runs against the same fixed file must keep working

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Single-file `filesrc` locations (not a directory) are never deleted or relocated; repeated runs against a fixed file keep working
- Camera, CSI, ICAM, and Aravis sources are completely unaffected (no Folder_Frames are recorded for them)
- Frame selection order (oldest `.jpg`/`.jpeg` by mtime via `_oldest_image_in_folder`) is unchanged
- The run's captured outputs, run metadata JSON, execution-row status/`failing_node_id`, node status persistence, and output-binding behavior are unchanged
- All existing failure modes (preflight, document load, Aravis feed, CSI prep, model resolution, rendering, bridge errors) record the same status and errors as before

**Scope:**
All runs where no `filesrc` element has a directory `location` should be completely unaffected by this fix. This includes:
- Documents with no `filesrc` at all (camera/CSI/ICAM/Aravis workflows)
- Documents whose `filesrc` points at a single file
- Documents with no frame sources (e.g. pure binding documents in tests)

**Note:** The expected correct behavior for buggy inputs is defined in Correctness Properties (Property 1).

## Hypothesized Root Cause

The root cause is verified, not hypothesized — this is a missing feature in the port from the legacy executor:

1. **No consumption path exists**: `workflow_engine/pipeline_executor.py` has no equivalent of `_cleanup_file_after_processing` or `_move_bad_folder_image_source`. `_stage_frame_sources` mutates the document and returns `None`, so `execute()` doesn't even know which files were resolved.

2. **Legacy condition (verified by reading `execute_workflow_pipeline`)**: cleanup runs in the `else` branch of the try/except around `_run_pipeline` — i.e. on pipeline-processing success, before/independent of any downstream output handling. The `except` branch relocates the folder image to `failed/` on ANY pipeline failure. Both are gated on `ImageSourceType.FOLDER`; FILE sources are never consumed. The new engine must mirror: consume after pipeline success even if Bedrock/LLM/output bindings later fail the run; relocate on pipeline failure.

3. **Bad-image path (verified)**: legacy calls `_move_bad_folder_image_source` in two places — the pre-run zero-byte check and the pipeline-failure `except` branch. In the new engine the analogous "bad image" signal is a `FrameSourceError` from `_stage_decoded_png` for a folder-resolved JPEG (decode failure), plus the pipeline-failure path. Both must relocate.

## Correctness Properties

Property 1: Bug Condition - Folder Source Frames Are Consumed After the Run

_For any_ run where the bug condition holds (at least one `filesrc` location is a directory), the fixed executor SHALL, when pipeline processing succeeds, delete the resolved source JPEG and any staged `.dda_decoded.png` (even when downstream output bindings fail); when pipeline processing fails, relocate the resolved JPEG to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`; and when the frame fails to decode/stage, relocate the bad JPEG to `failed/` before failing the run — so repeated executions drain the folder in mtime order and never re-process or wedge on the same image.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Non-Folder Runs and Run Semantics Unchanged

_For any_ run where the bug condition does NOT hold (no `filesrc` directory location: camera/CSI/ICAM/Aravis sources, single-file locations, or no frame source), the fixed executor SHALL produce the same result as the original — no files deleted or relocated, same document mutations, same frame selection for folder resolution order, same execution status, metadata, and output-binding behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

**File**: `src/backend/workflow_engine/pipeline_executor.py`

**Function**: `_stage_frame_sources`, `execute`, plus two new helpers

**Specific Changes**:

1. **Record resolved Folder_Frames**: Change `_stage_frame_sources(document)` to return `List[FolderFrame]` where `FolderFrame` is a small dataclass `(original: str, staged_png: Optional[str], node_id: Optional[str])`. Append an entry ONLY when `os.path.isdir(location)` was true (directory-resolved). A single-file location that gets a staged PNG is NOT recorded (never consumed), though its staged PNG may be best-effort cleaned if desired — out of scope; legacy leaves fixed-file behavior alone. When `_stage_decoded_png` raises for a directory-resolved JPEG, attach the resolved original path to the `FrameSourceError` (e.g. `e.source_image = resolved`) so the caller can relocate it.

2. **Consume on pipeline success** (new helper `_consume_folder_frames(folder_frames)`): for each Folder_Frame, delete the staged PNG (if any) and the original JPEG via `captured_images_utils.delete_image` (same permission handling as legacy; import lazily like other DAO/gstreamer imports if needed for test importability). Best-effort per file: a delete failure is logged and never fails the run (mirrors the containment discipline of `execute()`; legacy would raise, but a completed run must not be marked failed by cleanup).

3. **Relocate on failure** (new helper `_relocate_failed_folder_frames(workflow_id, folder_frames)`): mirror `_move_bad_folder_image_source` — ensure `{constants.INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` exists (via `dda_user_management_utils.create_dda_user_directory`, lazy import) and `os.rename` each original JPEG into it; best-effort remove any staged PNG. Logged, best-effort, contained.

4. **Hook into `execute()`**:
   - Capture `folder_frames = self._stage_frame_sources(document)` at the existing call site (~line 1020).
   - In the `except FrameSourceError` handler at that site: if the error carries a resolved source image, relocate it to `failed/` before `_finish_failed` (Requirement 2.4).
   - In the `except` handler around the pipeline run (`manager.run_pipeline` / `_run_bridged`): relocate all recorded Folder_Frames (Requirement 2.5), then proceed with the existing failure handling unchanged.
   - Immediately AFTER the pipeline run returns successfully (right before `_repair_capture_artifacts`): call `_consume_folder_frames(folder_frames)` (Requirements 2.1, 2.2, 2.3). This placement guarantees consumption happens regardless of Bedrock/LLM/output-binding outcomes, mirroring the legacy `else` branch.

5. **No behavior change when `folder_frames` is empty**: every new code path is a no-op for an empty list, so non-folder runs take the exact pre-fix path.

## Testing Strategy

### Validation Approach

Two-phase: first surface counterexamples demonstrating the missing consumption on UNFIXED code, then verify the fix drains folders correctly and preserves all non-folder behavior. Tests are pure/unit-testable: `tmp_path` folders with fake images and a stubbed pipeline run (injected `pipeline_manager_factory`), following the patterns in `test/backend-test/workflow_engine/` and `test/backend-test/output_bindings_fixes/` (see `executor_harness.py` and `workflow_engine_test_utils.py`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. The root cause is verified in code; the exploration test encodes the expected consumption behavior and MUST FAIL on unfixed code.

**Test Plan**: Build a `tmp_path` folder with 3 fake JPEGs at distinct mtimes, a compiled document whose `filesrc` location is the folder, and a stubbed pipeline manager that records the launch string and returns empty tags. Run `execute()` repeatedly on UNFIXED code and assert consumption/drain behavior.

**Test Cases**:
1. **Folder Drains On Success**: after a successful run, the resolved oldest JPEG is deleted; a second run resolves the next-oldest (will fail on unfixed code — the file remains and is re-selected)
2. **Staged PNG Cleanup**: with a `pngdec` chain (JP6 path), the staged `.dda_decoded.png` is deleted along with the original (will fail on unfixed code)
3. **Consumption Despite Output-Binding Failure**: pipeline succeeds, post-run handler raises `OutputBindingError`; the frame is still consumed (will fail on unfixed code)
4. **Bad Image Relocation**: oldest file is a corrupt JPEG in a `pngdec` chain; it is relocated to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` so the next run picks the next image (will fail on unfixed code)
5. **Pipeline Failure Relocation**: stubbed pipeline raises; the resolved JPEG is relocated to `failed/` (will fail on unfixed code)

**Expected Counterexamples**:
- The same oldest JPEG remains in the folder after every run and is re-resolved
- Possible causes (verified): no recording of resolved frames, no post-run consumption path, no bad-image relocation

### Fix Checking

**Goal**: Verify that for all runs where the bug condition holds, the fixed executor consumes or relocates the resolved frame.

**Pseudocode:**
```
FOR ALL run WHERE isBugCondition(run) DO
  result := execute_fixed(run)
  IF pipeline_succeeded(run) THEN
    ASSERT resolved_jpeg NOT IN folder AND staged_png NOT IN folder
  ELSE
    ASSERT resolved_jpeg IN failed_dir(workflow_id)
  END IF
END FOR
```

### Preservation Checking

**Goal**: Verify that for all runs where the bug condition does NOT hold, the fixed executor produces the same result as the original.

**Pseudocode:**
```
FOR ALL run WHERE NOT isBugCondition(run) DO
  ASSERT execute_original(run) = execute_fixed(run)
  ASSERT filesystem_after_original(run) = filesystem_after_fixed(run)
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis, per repo convention) is recommended for preservation checking because it generates many document shapes and folder states automatically, catches edge cases (empty folders, mixed extensions, single-file locations with/without staged PNGs), and gives strong guarantees that non-folder behavior is unchanged.

**Test Plan**: Observe behavior on UNFIXED code first for single-file sources, camera-style documents, and folder resolution order, then write property-based tests capturing that behavior. These must PASS on unfixed code.

**Test Cases**:
1. **Single-File Source Preservation**: for any single-file `filesrc` location, the file (and any staged PNG the run created) is never deleted or relocated; repeated runs keep working — passes on unfixed code
2. **Selection Order Preservation**: for any folder of JPEGs with random mtimes, `_stage_frame_sources` resolves the oldest by mtime, identical to unfixed behavior
3. **Non-Filesrc Document Preservation**: for documents with no `filesrc` (or no frame source), `execute()` produces the same status, metadata, and document mutations, and touches no files
4. **Failure-Mode Preservation**: existing failure paths (empty folder, render failure, pipeline exception status recording) record the same status/error/`failing_node_id` as before

### Unit Tests

- `_stage_frame_sources` return value: directory location → recorded Folder_Frame (with staged PNG on `pngdec` chains); single-file location → not recorded
- `_consume_folder_frames`: deletes original + staged PNG; delete failure is logged, not raised
- `_relocate_failed_folder_frames`: creates `failed/`, renames original, best-effort removes staged PNG
- `FrameSourceError` from `_stage_decoded_png` on a directory-resolved image carries the resolved source path

### Property-Based Tests

- Property 1 (fix checking): generate folders of N fake JPEGs with random mtimes; run the executor N times with a stubbed pipeline; assert the folder drains in mtime order and each run's launch string references the expected file
- Property 2 (preservation): generate non-bug-condition documents/locations; assert filesystem and execution-row outcomes are identical to observed unfixed behavior

### Integration Tests

- Full `execute()` flow through the harness: folder source → stubbed pipeline → output bindings, asserting consumption plus unchanged status/metadata/binding behavior
- On-hardware (user-gated, JP6, NEXT LocalServer build): folder with 3+ images, run the deployed workflow repeatedly, verify the folder drains in mtime order and `failed/` receives corrupt images
