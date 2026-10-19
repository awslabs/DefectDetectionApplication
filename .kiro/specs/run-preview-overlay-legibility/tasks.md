# Implementation Plan

## Overview

This plan implements design.md D1–D6. D7 (item d) is a follow-up and has no task here.
- **The capture card shows the overlay.** It shows the run's overlay by default, with an
  "Overlay" / "Raw frame" control and a caption naming the image shown (`previewModel.ts`,
  `NodePreviewCard.tsx`, `RunStatusGraph.tsx`).
- **The overlay scales with the image.** The marshal's detection overlay scales its stroke and font
  with the image, colors classes from an 8-color palette keyed on the class id, and draws boxes
  before labels (`marshal_for_capture_template.py`).
- **The class id reaches the marshal.** The YOLO and RF-DETR post-processors record the network
  class on each result, and the base model sends it as `class_id` (`object_detection_result.py`,
  the two post-processors, `lfv_model_template.py`).
- **The catalog is corrected.** The capture node's `output_path` description says that LocalServer
  does not write there: the Portal layer `nodes.py`, its vendored copy, and one regenerated baseline
  entry.

The plan follows the repo's exploratory bugfix method:
1. Write exploration tests that must FAIL on the unfixed code (F).
2. Write preservation tests, capture the F goldens, and record a baseline that must PASS on F.
3. Apply the fix, then re-run both.
4. Run a checkpoint.
5. Build and verify on devices; these steps are owner-gated.

**On-device rule (builds.md).** The marshal, the base model and the LocalServer UI run on the edge
device. The change is not done until it is built, deployed and verified on real hardware:
1. the MIC-730 first (JP5);
2. then a JP6 device and a JP7 device;
3. then x86, on hardware only if an x86 LocalServer device is available.

Nothing is committed before the MIC-730 verification passes.

**Sequencing.** The owner schedules the builds after the `triton-model-cleanup-wedge` builds,
because only one component build may run at a time.

**Suite invocations.** All run from the worktree root unless a directory is given. F is the code at
`1fa52f3a`, which differs from `795ce528` only in `README.md`.

| Suite | Command | Baseline at `1fa52f3a` |
|---|---|---|
| Marshal (host) | `PYTHONPATH=src/backend python3 -m pytest -q -p no:cacheprovider test/backend-test/test_marshal_*.py test/backend-test/test_overlay_path_consistency.py` | 46 passed |
| Base model tensor set (host) | `PYTHONPATH=src/backend python3 -m pytest -q -p no:cacheprovider test/backend-test/test_lfv_detection_tensor_set.py` | 3 passed |
| Overlay suite (host, new) | `PYTHONPATH=src/backend python3 -m pytest -q -p no:cacheprovider test/backend-test/overlay_legibility/` | – |
| Detector post-processors (image only) | the image command below, with `test/backend-test/test_yolo_detection_postprocessor.py test/backend-test/test_rf_detr_detection_postprocessor.py` | recorded in task 2. On the host both fail to collect: `model_processors/__init__.py` reaches `sklearn` |
| Catalog files (host) | from `edge-cv-portal/backend/layers/workflow_core`: `PYTHONPATH=python python3 -m pytest -q -p no:cacheprovider tests/test_bug_catalog_preservation.py` | 13 passed |
| Mirror guard (host) | `PYTHONPATH=src/backend python3 -m pytest -q -p no:cacheprovider test/backend-test/workflow_engine/test_vendored_catalog_mirror.py` | 8 passed |
| CSI pins (host) | `PYTHONPATH=src/backend python3 -m pytest -q -p no:cacheprovider test/backend-test/csi_nvargus_optional/test_preservation_csi_surface.py -k backend_csi_consumer_file_untouched` | 3 passed, 1 pre-existing failure (the vendored `nodes.py` pin, 35bc1b2b against e9cc8f43) |
| Portal layer, full | the `python:3.11-slim` container command in design.md, "Test environment" | recorded in task 2 |
| Frontend | from `src/frontend`: `npm ci` once (the worktree has no `node_modules`), then `CI=true npx react-scripts test --watchAll=false --testPathPattern=components/deployed-workflow` | recorded in task 2 |

**The image command** (the builds.md pattern; `pip` installs only into the throwaway container).
Every run in a flask-app image uses it, naming the freshly built image where one exists:

```
docker run --rm -v "$(pwd)":/repo -w /repo -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/repo/src/backend -e DDA_APP_ROOT=/repo/src/backend flask-app:latest bash -lc \
  'export LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}; \
   PY=${PY:-$(command -v python3.11 || command -v python3.10)}; \
   $PY -m pip install --no-cache-dir --quiet pytest sarge hypothesis pydantic==2.6.4; \
   $PY -m pytest -q -p no:cacheprovider <paths>'
```

- **`DDA_APP_ROOT` is required.** Both templates put `DDA_APP_ROOT`, default `/`, first on
  `sys.path`, and the image ships its own lyra packages at `/`. Without the variable, a session in
  which a template loads first, as the existing marshal suites do during collection, tests the
  image's packages instead of the branch's (design.md, Testing Strategy, "Test environment").
- **`sarge` is installed** because `test/backend-test/conftest.py` imports it, and
  `src/backend/requirements.txt` does not list it.
- **`pydantic==2.6.4` is installed** because the same conftest imports `pydantic` (`:20`). On JP6
  the image installs the app requirements for the DDA interpreter, 3.10, only, and python3.11
  there carries just the Triton stub's set (`Dockerfile.jp6:143`), so a python3.11 run in the JP6
  image cannot collect without it. The pin is the `requirements.txt` one, and pip leaves it alone
  where it is already present.
- **`PY` is taken from the environment when set.** On JP6, run the command once as written
  (python3.11, the stub interpreter), and once with `-e PY=python3.10` added before the image name.
- **In a combined run, list `test/backend-test/overlay_legibility/` first.** Its support module
  then pins both lyra packages to the branch before any template loads.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2"], "description": "Against UNFIXED code: exploration tests (MUST FAIL) and preservation tests plus the F goldens and baselines (MUST PASS). The two are independent, and both complete before any fix code is written." },
    { "wave": 2, "tasks": ["3"], "description": "The fix in the marshal, the base model and its post-processors, the three frontend files and the catalog; the tests for the new code; then exploration (now PASS) and preservation (still PASS). Depends on wave 1." },
    { "wave": 3, "tasks": ["4"], "description": "Checkpoint: every suite, the image suites, the frontend build, the security baseline grep, the guard pair and the hygiene greps. Depends on wave 2." },
    { "wave": 4, "tasks": ["5"], "description": "OWNER-GATED: pre-build checks and the JP5 LocalServer build, after the triton-model-cleanup-wedge builds. Depends on wave 3." },
    { "wave": 5, "tasks": ["6"], "description": "OWNER-GATED: MIC-730 deployment and verification (O1-O10, with the host-side checker). Depends on wave 4." },
    { "wave": 6, "tasks": ["7"], "description": "OWNER-GATED: JP6, JP7 and x86 builds, one at a time, and the non-regression checks (N1-N3). Depends on wave 5." },
    { "wave": 7, "tasks": ["8"], "description": "verification-notes.md and the local commit, with the owner's go-ahead. Depends on wave 6." }
  ]
}
```

## Tasks

- [ ] 1. Write the bug condition exploration tests (BEFORE the fix)
  - **Property 1: Bug Condition.** The capture card shows the overlay. **Property 4: Bug
    Condition.** The style scales. **Property 5: Bug Condition.** The classes differ, also in
    production shape. **Property 7: Bug Condition.** Labels on top. **Property 8: Bug Condition.**
    Card visibility. **Property 10: Bug Condition.** The catalog text. **Property 11: Bug
    Condition.** The class id reaches the payload.
  - **CRITICAL:** these tests MUST FAIL on F, on an assertion, not on an import or harness error.
    The failure confirms the defect. Do not fix the code or weaken a test when it fails.
  - **The class ids.** Record the PPE demo model's class ids in this task: its
    `detection.class_names` is `[helmet, human, no-helmet, vest]`, so helmet 0, human 1, no-helmet 2
    and vest 3. This order was read from the demo model's component manifest during the
    investigation, and the repo's PPE checkpoint fixture lists the same one
    (`edge-cv-portal/backend/tests/fixtures/detector_probes.py:25`). Task 6 re-reads it on the
    device.
  - **The support module.** Create `test/backend-test/overlay_legibility/_overlay_support.py` as in
    design.md, "The shared support module". At import, before any loader, it imports
    `lyra_science_processing_utils` and `lyra_anomalies_mask_utils`, and raises `RuntimeError` when
    either resolves outside this worktree's `src/backend`, naming the
    `DDA_APP_ROOT=/repo/src/backend` remedy. It provides:
    - `load_marshal(path=None)`, which loads `path` when given and the worktree's marshal template
      otherwise, under a new module name on each call;
    - `load_lfv()`, which stubs `lyra_science_processing_utils.model_graph_factory` only when the real
      import fails;
    - `load_postprocessors()`, which falls back to the stand-in package only when the real import
      fails;
    - `synthetic_scene(w, h)` and `scene_detections(w, h)`, exactly the design's production-shape
      scene;
    - `clamped_boxes(w, h, detections)`, today's round-and-clamp, written out in the module;
    - `card_visibility(raw_bgr, overlay_bgr, boxes) -> (worst, median)`, exactly the design's metric.

    The module must not import any function that exists only on F'.
  - **The marshal tests.** Create `test/backend-test/overlay_legibility/test_overlay_legibility_exploration.py`
    with the header `# Feature: run-preview-overlay-legibility, Property N: ...`. Only
    `_generate_detection_overlay` is called.
    - **E1.** One class-1 box (400, 300, 1500, 800) on a uniform RGB (128, 128, 128) 1920×1080
      image. In row 550, the run of pixels differing from the background around x = 400 is 7 px.
      Expected on F: 3 px.
    - **E2.** Classes 1 and 2 at (100, 100, 400, 600) and (700, 100, 1000, 600) on a uniform gray
      1280×720 image. The left-edge pixels `overlay[350, 100]` and `overlay[350, 700]` (row,
      column) are BGR (255, 0, 255) and (255, 160, 0). Expected on F: (0, 255, 0) for both.
    - **E2b.** E2's boxes in production shape: `{"class": "helmet", "class_label": "helmet",
      "class_id": 0}` and `{"class": "no-helmet", "class_label": "no-helmet", "class_id": 2}`. The
      same pixels are BGR (0, 255, 0) and (255, 160, 0). Expected on F: (0, 255, 0) for both.
    - **E3.** On the synthetic scene at 1920×1080 and 1280×720: worst ≥ 70 and median ≥ 140.
      Expected on F: 43 / 82 and 64 / 123.
    - **E7.** U5's A, box (200, 150, 600, 500) with class 0, label "helmet" and confidence 0.97.
      Then B, box (520, 100, 900, 600) with class 1, label "human" and confidence 0.91. Both on a
      uniform gray 1280×720 image. Let R be the pixels of overlay([A]) inside A's box whose max
      channel is ≤ 60. Assert that overlay([A, B]) equals overlay([A]) on R. Expected on F: 4 of
      the 144 text pixels differ.
  - **The class-id test (E8).** Create `test/backend-test/overlay_legibility/test_class_id_exploration.py`.
    - Build a YOLO output `[1, 8, 8400]` that scores two separate anchors in columns 0 and 2, and
      run it through `YoloDetectionPostProcessor({"detection": {"class_names": ["helmet", "human",
      "no-helmet", "vest"], "network_input": 640}})`.
    - Pass the results, wrapped as the model graph wraps them, to the base model's
      `__build_detection_tensors` from `load_lfv()`.
    - Decode the `anomalies` tensor. Assert that the entries have `class` "helmet" and "no-helmet",
      and `class_id` 0 and 2. Expected on F: no `class_id` key.
  - **The catalog test (E4).** Create `edge-cv-portal/backend/layers/workflow_core/tests/test_catalog_capture_output_path.py`.
    Assert that the capture `output_path` description contains "Not used on LocalServer" and
    "/aws_dda/captures/{workflowId}/{executionId}/". In the same file, add the P9 pin of the other
    fields (task 2), which must PASS on F.
  - **The frontend tests (E5, E6).** Create `src/frontend/src/components/deployed-workflow/graph/RunStatusGraph.captureOverlay.exploration.test.tsx`.
    Copy the harness of `RunStatusGraph.preview.test.tsx`: `jest.mock("api/WorkflowRegistrationAPI")`,
    the `useAuth` mock, `renderGraph`, `mockAPIs`, and the URL mocks `/output-image/{id}` and
    `/overlay-image/{id}`.
    - **E5.** `/results` returns `[{kind: "output", hasOverlay: true, hasOverlayImage: true}]`,
      and `cap1` has status `success`. After clicking `node-cap1`:
      - `preview-thumbnail` has `src` `/overlay-image/exec-1` and `data-shown="overlay"`;
      - `preview-image-caption` reads `CAPTURE_OVERLAY_CAPTION`.

      Click "Raw frame" within the control's toolbar:
      `within(screen.getByRole("toolbar", { name: "Image shown" })).getByRole("button", { name: "Raw frame" })`.
      The `SegmentedControl` also renders a `Select`, and jsdom shows both, so an unscoped query
      matches twice. The card then shows `/output-image/exec-1` with `CAPTURE_RAW_CAPTION`, and
      "Overlay", clicked the same way, switches back.

      Assert on the literal caption strings from design.md, D2, and import no new export, so that
      the file also type-checks on F.
    - **E6.** `getWorkflowExecutionResults` never resolves. The capture card shows `preview-loading`
      and no `preview-thumbnail`. Expected on F: the raw thumbnail.
    - **E6b.** `getWorkflowExecutionResults` rejects (the harness's QueryClient has `retry: false`,
      `RunStatusGraph.preview.test.tsx:90–92`). The capture card shows `preview-thumbnail` with
      `src` `/output-image/exec-1`, the caption `CAPTURE_RAW_CAPTION`, and no toolbar named
      "Image shown". This is the only test that drives a rejected `/results` through the
      `resultsError` wiring of 3.5. Expected on F: the raw thumbnail with no caption.
  - **Run** the suites: the backend overlay suite, the catalog file, and the frontend pattern
    `captureOverlay.exploration`. Record each counterexample in this task:
    - the measured widths, colors and visibility values;
    - E7's count of overwritten text pixels;
    - E8's decoded entries;
    - the description text;
    - the rendered `src`.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 2.1, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10_

- [ ] 2. Write the preservation tests, capture the F goldens, and record the baseline (BEFORE the fix)
  - **Preservation, checked on F and on F':**
    - the raster form of Property 6, the geometry (P1, P2);
    - Property 9, raw frames, non-detectors and the base model's anomaly tensors (P3–P5, P10);
    - the base model's detection payload, the preservation half of Property 11 (P10, P11);
    - the previews behind Property 3 (P7);
    - the catalog half of Property 10 (P9).

    Properties 3 and 6 are also checked directly after the fix, in 3.7 and 3.8, because their
    functions exist only on F'.
  - **CRITICAL:** these tests MUST PASS on F. Observe the values on F first, then encode them.
  - **The backend tests.** Create `test/backend-test/overlay_legibility/test_overlay_legibility_preservation.py`
    with P1–P5 and P10 as design.md specifies:
    - **P1:** Hypothesis, at most 25 examples (the conftest's `fast` budget), with bounds from the
      D3 formulas computed inside the test;
    - **P2:** invalid boxes and the sentinel;
    - **P3:** one detection (label "part", confidence 0.87, box (0.2w, 0.2h, 0.7w, 0.8h)) on the
      synthetic scene at 640×480 and 480×360. It runs as `{"class": 0, "class_label": "part"}` and
      as `{"class": "part", "class_label": "part", "class_id": 0}`, both against
      `goldens/preserved_class0_{w}x{h}.png`;
    - **P4:** `_generate_overlay` on a 256×192 RGB gradient with a two-color palette mask (one
      rectangle of (255, 0, 0) and one of (0, 0, 255) on white (255, 255, 255)), against
      `goldens/preserved_anomaly_256x192.png`;
    - **P5:** the input is unchanged;
    - **P10:** the base model's tensors through `load_lfv()`, against
      `goldens/preserved_lfv_tensors.json`. The inputs are those of design.md. Every case uses
      `input_np = np.zeros((8, 8, 3), dtype=np.uint8)` and the dtypes of
      `_make_detection_model_instance` (`test_lfv_detection_tensor_set.py:124–136`), because the
      golden stores the input-sized `mask` tensor's bytes. A `class_id` is set on one result with
      `setattr`, never through the constructor, so the test runs on F.
  - **The capture command.** Add
    `python3 test/backend-test/overlay_legibility/test_overlay_legibility_preservation.py --capture`
    (with `PYTHONPATH=src/backend`):
    - it writes the P3 and P4 goldens with `cv2.imwrite` (PNG), and the P10 golden as JSON: per
      case, every tensor's name, dtype, shape and base64 bytes;
    - it refuses to overwrite an existing file without `--force`;
    - a missing golden fails its test, with the command in the message.

    Run it now, on F, and commit nothing yet. Check that the PNGs are a few kilobytes each and show
    what is expected, and that the JSON holds the anomaly cases, the detection cases and the
    sentinel, and is under 20 KB.
  - **The catalog pin (P9).** In `test_catalog_capture_output_path.py`, pin the fields of
    `output_path` other than its description: name, `param_type "string"`, `required True`,
    `default None`, `constraints {"min_length": 1}`, `examples ["/aws_dda/captures"]`,
    `multiline False`, `depends_on None`.
  - **The frontend tests (P7).** Create `.../graph/RunStatusGraph.captureOverlay.preservation.test.tsx`
    with the checks of design.md, P7:
    - the model card's detection list and its overlay thumbnail `src`;
    - the llm, bedrock and mqtt previews, with `/results` not requested;
    - the non-output detail;
    - the capture card while `running` (no `/results`), after `failure` (alert, no `/results`), and
      with `hasImageResults: false` (`preview-unavailable`);
    - the capture card without an overlay (`images: []`), with `preview-thumbnail` `src`
      `/output-image/exec-1`.
  - **Record the baselines** in this task:
    - the marshal suite (46 at `1fa52f3a`) and the base model tensor set (3);
    - the overlay suite (P1–P5 and P10 pass; E1–E3, E2b and E7 fail);
    - the class-id exploration file (E8 fails);
    - the detector post-processor suites in the `flask-app:latest` image (P11), with the image
      command of the Overview;
    - the catalog files (13 plus E4's file: E4 fails, P9 passes);
    - the mirror guard (8);
    - the CSI pins (3 pass, 1 pre-existing failure; record its hashes);
    - the full Portal layer suite in the `python:3.11-slim` container;
    - the frontend `components/deployed-workflow` suite (`npm ci` first);
    - `git status --porcelain` before any fix.
  - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.12_

- [ ] 3. Apply the fix

- [ ] 3.1 The marshal: `src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py`
  - Add `math`, `zlib` and `collections.namedtuple` to the top imports.
  - Add `DETECTION_PALETTE_RGB`, `DETECTION_LABEL_TEXT_RGB`, `OverlayStyle`, `OverlayItem`,
    `_round_half_up`, `_detection_overlay_style`, `_detection_color_index`, `_bgr` and
    `_layout_detection_overlay`, exactly as in design.md, "Fix Implementation". The color index
    takes `det.get("class_id")`, today's `str(det.get("class", ""))` and the resolved class label,
    in that order.
  - At module level touch only `math`, `zlib` and `collections`, and read no `cv2` attribute. Five
    metadata test modules import the template with an empty `cv2` stub when OpenCV is missing.
  - Rewrite the body of `_generate_detection_overlay`: the channel swap, the layout, then pass 1
    (boxes) and pass 2 (bands and text). Keep its signature and return shape. Update its docstring
    and the comment at `:174–180`.
  - Change nothing else in the file. In particular, the record and sidecar builders keep copying
    only their four named keys.
  - _Requirements: 2.5, 2.6, 2.7, 3.1, 3.2, 3.5, 3.8, 3.12_

- [ ] 3.2 The base model: the class id
  - `src/backend/lyra_science_processing_utils/utils/object_detection_result.py`: import
    `Optional`, and add the keyword-only `class_id: Optional[int] = None` after `threshold`, with
    its docstring, stored as `self.class_id`. Do not change `serialize()` or `deserialize()`.
  - `.../model_processors/yolo_detection_postprocessor.py` and
    `.../model_processors/rf_detr_detection_postprocessor.py`: in `_make_result`, pass
    `class_id=int(cls_id)` to `ObjectDetectionResult`. Change nothing else in either file.
  - `src/backend/dda_triton/resources_for_copy/lfv_model_template.py`, `__build_detection_tensors`:
    append `entry["class_id"] = int(class_id)` after `class_label`, only when
    `getattr(d, "class_id", None)` is an `int` or a NumPy integer and not a `bool`. Correct the
    comment at `:328–331`. Change nothing else: not the tensor set, the scores, the empty mask, the
    sentinel or `__build_anomaly_tensors`.
  - Do not touch `supervised_bbox_multihead_postprocessor.py`, the test sandbox's copies or
    `mock_model_template.py`, which has no detection-tensor builder.
  - _Requirements: 2.6, 2.10, 3.5, 3.12_

- [ ] 3.3 The view-model: `src/frontend/src/components/deployed-workflow/graph/previewModel.ts`
  - Add the `image` variant's optional `overlaySrc` and `overlayUnknown`, and the
    `resultsLoading` / `resultsError` arguments.
  - Implement the five-step capture branch.
  - Add `CaptureImageMode`, `CaptureImageDisplay`, the four caption constants and
    `captureImageDisplay`, exactly as in design.md. `CaptureImageDisplay` is the discriminated
    union on `shown`: the `"none"` member has no `src` or `caption`, and the other member has both
    as `string`.
  - Update the precedence comment.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.7_

- [ ] 3.4 The card: `.../graph/NodePreviewCard.tsx`
  - Add the `failedSrcs` set and `markFailed`, and `rawChosenFor`. Both hooks stay above the
    `kind === "none"` return.
  - The `detections` case uses `failedSrcs.has`.
  - Render the new `image` case as the design's TSX: a block that returns `<Unavailable />` when
    `display.shown === "none"` and only then reads `display.src`, so it type-checks under
    `strict`. Then the `SegmentedControl`, the `<img>` with `data-shown` and an alt text naming the
    image shown, and the caption `Box`.
  - Import `SegmentedControl` from `@cloudscape-design/components`. Add no dependency.
  - _Requirements: 2.1, 2.2, 2.3, 3.6, 3.7_

- [ ] 3.5 The graph: `.../graph/RunStatusGraph.tsx`
  - Add `selectedIsCapture`, widen `resultsEnabled`, and pass
    `resultsLoading: resultsEnabled && resultsQuery.isInitialLoading` and
    `resultsError: resultsEnabled && !resultsQuery.isSuccess` to `previewViewModel`. The second keys
    on "not successfully loaded", not on `isError`, so that a fetch that never completed (for
    example one paused offline) also counts as unknown (design.md, `RunStatusGraph.tsx`). Update the
    comment at `:139–142`.
  - Touch nothing else, in particular not the metadata query, the polling or the graph rendering.
  - _Requirements: 2.1, 2.2, 2.4, 3.6, 3.7_

- [ ] 3.6 The catalog
  - In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, replace
    the capture `output_path` description with the text in design.md, D6. Then
    `cp edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`.
    Do not run `re_vendor.sh`.
  - Regenerate the `capture` entry of `edge-cv-portal/backend/layers/workflow_core/tests/catalog_baseline.json`
    with the one-off procedure in design.md, "Catalog". Confirm that `git diff --stat` shows a
    single changed line in the JSON, and that the diff is the description only.
  - Append the maintenance note to the comment block in `test_bug_catalog_preservation.py`.
  - _Requirements: 2.9, 3.9, 3.10_

- [ ] 3.7 Write the backend tests for the new code
  - Create `test/backend-test/overlay_legibility/test_overlay_style_properties.py` with U1–U7 and
    U9, as design.md specifies:
    - the style table, including 599×599, 600×600, 1500×1500 and 1920×1080 for the round-half-up;
    - the WCAG recomputation;
    - the color index: the key order of bugfix 2.6 (`class_id`, then `str(class)`, each when it is
      not a boolean and `int()` accepts it, then the label hash), checked on the function and
      through `_layout_detection_overlay`; booleans never deciding; stability; and a
      `PYTHONHASHSEED` subprocess;
    - the layout against an independent re-implementation of today's expressions, with `class_id`
      among the generated fields;
    - labels on top: A at (200, 150, 600, 500) and B at (520, 100, 900, 600), at 1280×720, on F'
      only;
    - the measured stroke widths 3, 5, 7 and 11 px;
    - the JPEG round trip within ±40;
    - U9: the four PPE classes in production shape give entries 0–3, and without `class_id`
      6, 5, 6 and 7.
  - Create `test/backend-test/overlay_legibility/test_detection_class_id.py` with U8: the YOLO and
    RF-DETR results' `class_id`, `ObjectDetectionResult`'s keyword-only default and unchanged
    `serialize()` / `deserialize()`, and the base model's emission rule over Hypothesis lists. It
    loads the post-processors through `load_postprocessors()`, so it runs on the host.
  - Create `test/backend-test/overlay_legibility/test_overlay_goldens.py` for the synthetic scene at
    1920×1080, 1280×720 and 640×480:
    - the mask is the band rectangles united with the text boxes, grown by font thickness + 2 px;
    - outside the mask, pixel-exact; inside, a mean absolute difference ≤ 6.0;
    - `--regenerate` writes `goldens/detection_overlay_{w}x{h}.png`, and a missing golden fails
      with that command in the message.
  - Generate the three goldens on F' and inspect them as images before keeping them:
    - one color per class, with helmet green, human magenta, no-helmet azure, vest orange and
      tool yellow;
    - the 7 px strokes at 1920×1080;
    - filled bands, with black text;
    - the "widget" band, green by the label hash, clamped at the left edge;
    - the "tool" label without a percentage.

    Each is expected to be about 10–35 KB.
  - _Requirements: 2.5, 2.6, 2.7, 2.8, 2.10, 3.1, 3.12_

- [ ] 3.8 Write the frontend tests for the new code
  - Create `.../graph/previewModel.capture.property.test.ts` (fast-check, 100 runs):
    - the five-step capture precedence;
    - Property 2 over `captureImageDisplay`, with distinct generated URLs, a failed subset, both
      modes and both `overlayUnknown` values;
    - Property 3 for non-capture node types, as invariance under `resultsLoading` and
      `resultsError` (design.md).
  - Create `.../graph/NodePreviewCard.capture.test.tsx` with the cases in design.md: the captions
    and tones, the control, switching, overlay failure, double failure, reset on a new
    `overlaySrc`, and the alt text naming the image shown. Click the options within the toolbar
    named "Image shown", as in E5.
  - In `NodePreviewCard.test.tsx`, extend only `viewModelArb`'s `image` arbitrary with optional
    `overlaySrc` and `overlayUnknown`. Property 4 must keep passing.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.7_

- [ ] 3.9 Verify the fix: re-run the exploration tests (they MUST now PASS)
  - Re-run E1–E8 unchanged. Record the values:
    - E1: 7 px;
    - E2: the two palette colors; E2b: green and azure;
    - E3: about 85 / 163 and 96 / 184;
    - E7: 0 text pixels changed;
    - E8: `class_id` 0 and 2.
  - _Requirements: 2.1, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10_

- [ ] 3.10 Verify preservation: re-run task 2 (it MUST still PASS, unchanged)
  - Re-run P1–P11 and the existing suites from task 2, including the detector post-processor suites
    in the image, with the image command of the Overview. The counts must be at or above the
    baseline.
  - No test may be modified or skipped. The two exceptions are the `viewModelArb` extension (3.8),
    and the `catalog_baseline.json` `capture` entry and its maintenance comment in
    `test_bug_catalog_preservation.py` (3.6).
  - The CSI pin for the vendored `nodes.py` still fails, now against the new hash; the other three
    pass. Record both hashes. Re-pinning is out of scope (design.md, Risks).
  - _Requirements: 3.1–3.12_

- [ ] 4. Checkpoint
  - **Every suite** in the Overview table: the host suites, the full Portal layer suite in the
    container, and the frontend suite. Also `CI=true npm run build` in `src/frontend`.
  - **The flask-app image.** With the image command of the Overview, which sets `DDA_APP_ROOT`, run
    in the existing `flask-app:latest` image, in this order: the overlay suite, then the marshal
    suites, `test_lfv_detection_tensor_set.py` and the two detector post-processor suites. They
    then also run under the image's OpenCV and with `sklearn`.
    - A `RuntimeError` from `_overlay_support` that names `DDA_APP_ROOT` means that the session
      imported the image's own lyra packages. Fix the command, not the code.
    - If the golden tolerance fails there, fix the comparison or the code; do not regenerate the
      goldens to match one image.
  - **The security baselines.** Grep `test/backend-test/security/baselines/` for every edited path.
    At `1fa52f3a` none is hash-tracked. If that has changed, rebaseline only what is pinned, and name
    this task in the note.
  - **The guard pair** from builds.md must be green.
  - **Public-repo hygiene of the diff** (code, tests, comments, goldens): no device thing names,
    account ids, hostnames, bucket names, IoT endpoints, e-mail addresses or local paths outside the
    repository. The goldens are synthetic: no people and no device images.
  - _Requirements: 2.1–2.10, 3.1–3.12_

- [ ] 5. OWNER-GATED: pre-build checks and the JP5 build
  - **Do not start without the owner's go-ahead.** The build runs after the
    `triton-model-cleanup-wedge` builds; never start two component builds at once.
  - Check that no build is running: `pgrep -af "gdk component build"` and
    `pgrep -af "build-custom.sh"` both return nothing.
  - Check that no Portal deploy is in progress. Move `edge-cv-portal/infrastructure/cdk.out` aside
    and re-run the guard pair green.
  - Build `aws.edgeml.dda.LocalServer.arm64JP5` alone, logging to `.gdk_build_jp5.log`, and publish
    the next patch version. Restore `gdk-config.json` and `recipe.yaml` afterwards.
  - Run N1 in the freshly built JP5 flask-app image, with the image command of the Overview.
  - _Requirements: 2.5, 2.6, 2.7, 2.10_

- [ ] 6. OWNER-GATED: MIC-730 deployment and verification (design.md, "On-device verification")
  - **The checker.** Write the host-side checker of design.md, "The host-side checker", as a script
    next to the downloaded images, outside the repository. It is not committed. It loads the
    branch's marshal through `_overlay_support.load_marshal()` and takes the geometry from
    `_layout_detection_overlay` and the colors from the overlay.
    - For O4 it also loads F's marshal. From the worktree root, run `TMP_F=$(mktemp -d)` and
      `git show 1fa52f3a:src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py > "$TMP_F/marshal_f.py"`,
      then call `load_marshal("$TMP_F/marshal_f.py")`.
    - It draws F's overlay on each sampled post-deploy raw frame with that run's `/metadata` boxes,
      round-trips it through `cv2.imencode(".jpg")`, and compares the two overlays' worst edges on
      the same frame. Delete `$TMP_F` afterwards.
  - **Before deploying, on the current build:**
    - record the LocalServer version, the backend `RestartCount` and the free space on `/aws_dda`;
    - read the PPE model's `detection.class_names` from the deployed model's `manifest.json`, which
      `base_<ppe model>/1/` links to (read only), and check it against task 1's record;
    - run the PPE demo workflow at least 40 times over at least one video loop;
    - save `/output-image`, `/overlay-image` and `/metadata` of three runs with at least four
      detections of at least two classes, and of one run with none, outside the repository;
    - compute `card_visibility` (context for O4), the raw rates for O6, and the model node's
      `durationMs` p50.
  - **Deploy** by revising only the LocalServer version, following the MIC-730 deployment safety
    steps:
    - stop every trigger publisher first;
    - trigger no run until the deployment is COMPLETED and every model component's Startup has
      finished (READY or "gave up");
    - kick the PPE model with one manual run and wait for READY.

    If a model stays LOADING for more than 10 minutes and `Python model file not found` appears in
    the backend log, capture the evidence and stop. A backend restart needs the owner's go-ahead.
  - **Check O1 first**: the sha256 of `marshal_<ppe model>/1/model.py` and `base_<ppe model>/1/model.py`,
    and of the backend container's `object_detection_result.py` and the post-processor that the
    deployed detector uses (`yolo_detection_postprocessor.py` for `yolo_object_detection`, the PPE
    demo model; `rf_detr_detection_postprocessor.py` for `rf_detr_object_detection`), against the
    branch's files. Then repeat the runs and downloads.
  - **Check O2–O10** against design.md's table:
    - at least 8 checkable boxes of at least 2 labels; record the skipped ones;
    - O2: dist ≤ 40 and the 7 ± 2 px width;
    - O3: one entry per label, different labels on different entries, and helmet 0, human 1,
      no-helmet 2 and vest 3;
    - O4: the median, over the three sampled post-deploy runs with detections, of the device-to-F
      worst-edge ratio on the same raw frame is at least 1.5;
    - O5: geometry and the empty run;
    - O6: each label's raw rate at most 5 points above its pre-deploy rate, and the 1920×1080 raw
      frame;
    - O7: the capture card, checked in a browser on the LocalServer UI; record who checked it;
    - O8: the unchanged views;
    - O9: `durationMs` p50 within +5 ms or +5 %;
    - O10: 30 minutes healthy.

    If O3 shows entries 6, 5, 6 and 7, the base model is stale: stop and ask the owner, as O1 says.
  - **If any other check of O2–O10 fails:** stop, record the evidence, leave the triggers stopped,
    and ask the owner whether to roll back by redeploying the previous LocalServer version. Do not
    commit. The device stays on the new build until the owner decides (design.md, "On a failed
    check").
  - Record every number next to its pre-deploy value in `verification-notes.md`. Leave the device
    as it was found: no trigger publisher running and the demo workflow unchanged.
  - _Requirements: 2.1, 2.2, 2.5, 2.6, 2.7, 2.10, 3.1, 3.2, 3.3, 3.6, 3.12_

- [ ] 7. OWNER-GATED: JP6, JP7 and x86 builds and non-regression (N1–N3)
  - Build `aws.edgeml.dda.LocalServer.arm64JP6`, then `aws.edgeml.dda.LocalServer.arm64JP7`, then
    `aws.edgeml.dda.LocalServer.amd64`, one at a time, each with the pre-build checks of task 5.
    The x86 build runs on an x86_64 build host.
  - **N1** in each built image, with the image command of the Overview (it sets `DDA_APP_ROOT` and
    installs `pydantic==2.6.4`): the overlay suite first, then the marshal, base model and detector
    post-processor suites, under python3.11. On JP6, run the command once as written (python3.11,
    the stub interpreter), and once with `-e PY=python3.10` added before the image name.
  - **N2** on a JP6 Jetson AGX Orin and on a JP7 Jetson Thor:
    - deploy with the deployment safety steps;
    - over a 15-minute soak, check O1 (with the post-processor that device's detector uses:
      `rf_detr_detection_postprocessor.py` for an RF-DETR model, `model_processors/__init__.py:62–63`),
      O2 at the device camera's resolution, O3 against that model's `detection.class_names`, O5,
      O7, O8 and O10;
    - if the device has a segmentation or anomaly workflow, check that its views are unchanged and
      that its capture card shows the mask overlay.

    Deploying a minimal detection workflow, where none is registered, needs the owner's approval.
    A failed check follows task 6's rule: stop, record, leave the triggers stopped, ask the owner
    about a rollback, and do not commit.
  - **N3**: x86 hardware checks only if an x86 LocalServer device with a detection model is
    available. Otherwise record "not verified on x86 hardware".
  - _Requirements: 2.1, 2.5, 2.6, 2.10, 3.2, 3.3, 3.5_

- [ ] 8. Write verification-notes.md and commit, with the owner's go-ahead
  - Write `.kiro/specs/run-preview-overlay-legibility/verification-notes.md`, with:
    - the host, container and image suite counts;
    - the exploration counterexamples and the after values;
    - the golden sizes;
    - the before-and-after tables for the MIC-730, the JP6 and JP7 devices, and x86, with the
      checker's per-label entries;
    - the pre-existing CSI pin.

    Use neutral device wording: the MIC-730, a JP6 Jetson AGX Orin, a JP7 Jetson Thor, an x86
    device.
  - Commit on `fix/run-preview-overlay-legibility`, staging files by name. State what was verified
    on which device and the LocalServer versions. No push: the repo is public and the push is
    owner-gated.
  - Before committing, run the public-repo hygiene checks over the added lines and the message.
  - Tell the owner that the `output_path` text reaches the Portal's Workflow Builder only with the
    next Portal backend deploy. This spec does not schedule one.

## Notes

- Tasks 1 and 2 run against F. Task 1 must fail for the right reason and task 2 must pass, both
  before any line of task 3 is written. The F goldens of task 2 (P3, P4, P10) must exist before
  task 3.1 starts.
- The exploration and preservation tests call only functions that exist on F. The new helpers are
  tested in 3.7 and 3.8. The preservation tests set a class id only with `setattr`, which works on F.
- Property tests use Hypothesis (backend) and fast-check (frontend). The rasterizing Hypothesis
  tests cap their examples at 25, the `fast` profile budget of `test/backend-test/conftest.py`,
  because each one draws real images.
- Out of scope, as listed in bugfix.md:
  - completion-phase `{capture_id}` / `{execution_id}` (the follow-up spec);
  - label collision avoidance;
  - semantic colors and a legend;
  - a detection count on the capture card;
  - honoring `output_path`;
  - the test sandbox's copies;
  - class ids from other detection producers;
  - run-artifact cleanup.
