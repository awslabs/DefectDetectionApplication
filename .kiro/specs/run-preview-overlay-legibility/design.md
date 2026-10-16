# Run Preview Overlay Legibility Bugfix Design

## Overview

The fix has four parts. They are committed together and ship in one LocalServer build; the catalog
text also reaches the Portal with its next backend deploy (D6).

- **The capture card shows the overlay (frontend).** In the run status graph, the capture node's
  card shows the run's overlay image by default when `/results` reports `hasOverlayImage`. A
  two-option control switches between "Overlay" and "Raw frame", and a caption under the image
  names which one is shown. A run without an overlay shows the raw frame, labelled as such.
- **The overlay scales with the image (marshal).** `_generate_detection_overlay` sizes the stroke,
  the font and the label padding from the image's short side, and colors each class from a fixed
  8-color palette with black or white label text. It draws all boxes before all labels. Which
  detections are drawn, their coordinates, the label text and the label anchor stay as they are.
- **The class id reaches the marshal (base model).** The YOLO and RF-DETR post-processors record the
  network's integer class on each result, and the base model adds it to the detection's payload
  entry as `class_id`. The marshal keys the palette on it. A named detector, such as the PPE demo
  model, otherwise sends only class names, and their hash collides (D4).
- **The catalog says where the files really go (item c).** The capture node's `output_path`
  description states that LocalServer does not use the folder and names the per-run folder. This is
  text only: the executor is unchanged.

Item (d), `{capture_id}` and `{execution_id}` in result payloads, is not a documentation gap; it is
a missing feature. It goes to a follow-up spec (D7).

Files that change:

| Area | File | Change |
|---|---|---|
| Marshal | `src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py` | Module-level style, color and layout helpers. `_generate_detection_overlay` draws in two passes. |
| Base model | `src/backend/lyra_science_processing_utils/utils/object_detection_result.py` | A keyword-only `class_id` field, `None` by default. `serialize()` and `deserialize()` do not change. |
| Base model | `src/backend/lyra_science_processing_utils/model_processors/yolo_detection_postprocessor.py`, `.../rf_detr_detection_postprocessor.py` | `_make_result` passes `class_id=int(cls_id)`. |
| Base model | `src/backend/dda_triton/resources_for_copy/lfv_model_template.py` | `__build_detection_tensors` appends `class_id` to the entry of a result that has an integer class id. |
| Frontend | `src/frontend/src/components/deployed-workflow/graph/previewModel.ts` | The capture branch carries the overlay URL, plus a pure `captureImageDisplay` helper and four captions. |
| Frontend | `.../graph/NodePreviewCard.tsx` | The `image` case renders the control, the image and the caption. Failed URLs are tracked as a set. |
| Frontend | `.../graph/RunStatusGraph.tsx` | The `/results` query is also enabled for a finished capture node. Its loading and error states reach the view-model. |
| Catalog | `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, copied to `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py` | The capture node's `output_path` description. |
| Catalog test data | `edge-cv-portal/backend/layers/workflow_core/tests/catalog_baseline.json`, `.../tests/test_bug_catalog_preservation.py` | The `capture` baseline entry is regenerated, and one maintenance note is added to the comment block. |
| Tests | New `test/backend-test/overlay_legibility/`, new frontend test files under `.../graph/`, and one new Portal layer test | See Testing Strategy. |

## Glossary

- **Capture card**: the preview card that `NodePreviewCard` renders when a `capture` node is
  selected on the run status graph (`/deployed-workflows/{registrationId}/executions/{executionId}/graph`).
  The **model card** is the same card for a `model_inference` node.
- **Raw frame**: `{capture_id}.jpg` in the run folder, served by
  `GET /workflows/executions/{id}/output-image`.
- **Overlay image**: `{capture_id}.overlay.jpg`, the marshal's `overlay` tensor, which the terminal
  `emlcapture` writes. It is served by `GET /workflows/executions/{id}/overlay-image`, and the
  `/results` output entry's `hasOverlayImage` says whether it exists (`api.py:441`). For a detector
  it is the frame with boxes; for a segmentation model, the frame blended with the mask.
- **Base model**: the per-model Triton python-backend model built from `lfv_model_template.py`. A
  model component's Startup copies the template to `base_<model>/1/model.py`
  (`model_convertor.py:533–604`, the copy at `:556–565`). It runs the model graph and sends its
  detections to the marshal as a JSON list in the reused `anomalies` tensor.
- **Marshal**: the per-model Triton python-backend model built from
  `marshal_for_capture_template.py`. The same Startup copies it to
  `/aws_dda/dda_triton/triton_model_repo/marshal_<model>/1/model.py` (`model_convertor.py:607–643`).
  Both templates come from `/aws_dda/resources_for_copy/`, which the backend re-syncs from its image
  whenever it starts (`triton_setup.py:164–179`).
- **Payload entry**: one detection in the base model's JSON list. Today it is `{bounding_box, class,
  confidence, confidence_threshold, class_label}` (`object_detection_result.py:55`,
  `lfv_model_template.py:333–336`); after the fix a YOLO or RF-DETR entry ends with `class_id`.
- **Class id**: the network's integer class, the index into the model's `detection.class_names`.
- **Production shape**: a payload entry as a named detector sends it. `class` and `class_label` are
  the class name, and after the fix `class_id` is the integer. The existing marshal tests use
  integer-string classes such as `"17"`, which only an unnamed detector sends.
- **Card scale**: min(320/w, 200/h). The card's `maxWidth: 320`, `maxHeight: 200` and
  `objectFit: contain` (`NodePreviewCard.tsx:49–53`) show a w×h image at this scale.
- **Box thickness t**: the `thickness` argument of `cv2.rectangle`. OpenCV paints a band
  2⌈t/2⌉+1 px wide, centered on the edge. Measured with the host's OpenCV: t = 2 → 3 px, 3 → 5,
  5 → 7, 9 → 11, 14 → 15.
- **s**: min(w, h), the image's short side.
- **Synthetic scene, card-visibility metric**: the test input and the measure defined under Testing
  Strategy.
- **F, F'**: the code at `1fa52f3a` (identical to `795ce528` outside `README.md`), and the fixed
  code.

## Bug Details

### Bug Condition

```
FUNCTION isCardBugCondition(node, run, results)
  RETURN node.type = "capture"
     AND node.status IN {success, warning}
     AND run.hasImageResults
     AND EXISTS image IN results.images : image.kind = "output" AND image.hasOverlayImage
END FUNCTION

FUNCTION isOverlayBugCondition(w, h, detections)
  valid := [d IN detections WHERE length(d.bounding_box) = 4]
  RETURN valid ≠ []
     AND (   min(w, h) ≥ 600                                  -- the style should grow
          OR the valid detections fall in ≥ 2 palette entries   -- the classes should differ (D4 key)
          OR EXISTS i < j : box(valid[j]) crosses band(valid[i]) )  -- a stroke over a label
END FUNCTION

FUNCTION isClassIdBugCondition(post_processor, class_names, entry)
  RETURN post_processor IN {yolo_object_detection, rf_detr_object_detection}
     AND class_names ≠ None
     AND "class_id" ∉ keys(entry)                            -- only the name reaches the marshal
END FUNCTION

FUNCTION isCatalogBugCondition(descriptor)
  RETURN descriptor.type_id = "capture"
     AND description(descriptor.output_path) claims images are written to output_path
END FUNCTION
```

### Examples

- **The PPE demo, 1920×1080, 8 detections of 4 classes.** In the capture card, F shows the raw frame
  with no label. In the model card, F's thickness-2 green strokes (3 px painted) are half a card
  pixel wide, and the 12 px labels are 2 px tall. F' shows the overlay in the capture card, captioned
  "Overlay: …". Its strokes are 7 px (about 1.2 card px) and the label bands about 39 px tall at
  full size, with 0.9-scale text. The model's `detection.class_names` is
  `[helmet, human, no-helmet, vest]`, read from the demo model's component manifest during the
  investigation; the repo's PPE checkpoint fixture lists the same order
  (`edge-cv-portal/backend/tests/fixtures/detector_probes.py:25`). So the class ids give four
  colors: helmet green (0), human magenta (1), no-helmet azure (2) and vest orange (3).
- **The same model with only the marshal updated**, for example by a hot-patch of the marshal
  alone. No `class_id` arrives and the label hash decides: helmet and no-helmet both red (6), human
  violet (5), vest aquamarine (7). O1 and O3 catch this on the device.
- **An empty-corridor run (a sentinel, no objects).** F' shows the overlay, an unannotated copy of
  the frame, captioned as the overlay; the model card says "No objects were detected.".
- **A capture-only workflow (no model).** There is no overlay image, so F' shows the raw frame,
  captioned "Raw frame: this run has no overlay image.".
- **A segmentation run with a mask.** F' shows the server-rendered mask overlay in the capture card.
  The Run results page keeps its client-side mask composite.
- **A 640×480 one-class detector, named or not.** The style is unchanged (2, 0.5, 1, 3 px) and class
  id 0 selects today's green, so a single detection's overlay is byte-identical. With several
  detections, only the pixels where a later box crossed an earlier label change (defect 1.5).

## Expected Behavior

bugfix.md 2.1–2.10 lists the required behavior and 3.1–3.12 the behavior that must not change. The
Correctness Properties below restate them as testable properties.

## Root Cause

1. **The capture card never asks for the overlay.** `previewViewModel`'s capture branch
   (`previewModel.ts:229–233`) returns `{kind: "image", src: imageSrc}`, and `imageSrc` is the
   raw-frame URL (`RunStatusGraph.tsx:110–114`). The `/results` query that knows about the overlay
   is enabled for `model_inference` only (`:124`, `:143–146`). The card's alt text is "Output of
   {nodeId}", and nothing visible says that it is the raw frame.
2. **The overlay style is fixed in pixels.** `_generate_detection_overlay` hard-codes thickness 2,
   font scale 0.5, font thickness 1 and padding 3 (`marshal_for_capture_template.py:208`,
   `:214–219`), and the green (0, 255, 0) for every class (`:208`, `:225–227`). None of them depends
   on the image size.
3. **Each detection is drawn in one pass** (`:183–237`), so a later box can cross an earlier label.
4. **The catalog describes a behavior that the executor overrides.** `nodes.py:1527–1532` describes
   `output_path` as the folder where images are written. `_route_capture_outputs`
   (`pipeline_executor.py:1340`) replaces the compiled `buffer-message-id` with the run folder
   (`:1402`, `:1416`), because the run API, run retention and the marshal's id derivation rely on
   the `{root}/{workflowId}/{executionId}` layout.
5. **A named detector drops its class id before the marshal.** The YOLO and RF-DETR `_make_result`
   replace the integer class with its name (`yolo_detection_postprocessor.py:270–277`,
   `rf_detr_detection_postprocessor.py:212–219`). `serialize()` emits only the name as `class`
   (`object_detection_result.py:55`), and the base model forwards that
   (`lfv_model_template.py:333–336`). A palette keyed on an integer class therefore never sees one
   for such a model.

## Design Decisions

### D1. The capture card shows the overlay by default, with a two-option switch (chosen)

The capture card behaves like the model card: when the run has an overlay image, it shows it. The
card renders a Cloudscape `SegmentedControl`, labelled "Image shown", with the options "Overlay"
and "Raw frame", above the image. Below the image a visible caption names and explains what is
shown. The image's alt text names it too ("Output of capture_1, overlay"), without repeating the
caption, so a screen reader does not read the same sentence twice. The control appears only when
both images can be shown.

The capture card does not use the Run results page's toggle. That `Toggle` ("Show bounding boxes" /
"Show overlay") names an action. The card must name the image it shows, and a two-option control
does both: its selected option is the label. The Run results page keeps its toggle unchanged
(bugfix 3.3). Captioning the raw frame without offering the overlay, the investigation's
alternative, was rejected because it leaves the matches two clicks away.

The logic is in a pure function, `captureImageDisplay` in `previewModel.ts`. It takes the raw URL,
the overlay URL (if any), whether the overlay's availability is unknown, the chosen mode and the
set of URLs that failed to load. It returns what to show, its caption, the caption's tone and
whether to show the control. `NodePreviewCard` holds only two pieces of state:
- the set of URLs whose `<img>` fired `onError`;
- the overlay URL for which the user chose the raw frame.

Because that choice is keyed by the overlay URL, it resets to the overlay for another run, and it
persists across the capture nodes of one run, which show the same two images.

`RunStatusGraph` enables the existing `/results` query (`["getWorkflowExecutionResults",
executionId]`) for a finished capture node, as it does for a model node. The query cache is
shared, so the model card, the capture card and the Run results page (`RunResults.tsx:312`) fetch
`/results` at most once per run.

The overlay is the default for every run with an overlay image (assumption A2), including a run
with no detections (the overlay is then an unannotated copy) and a segmentation run (the overlay is
the mask blend). The caption is accurate in all of these cases. The model card's detection list is
where the user sees that nothing was detected.

### D2. Loading and failure states of the capture card

| State | Card shows | Caption |
|---|---|---|
| Node not terminal | In-progress placeholder (unchanged) | – |
| Node `failure` | Failure alert (unchanged) | – |
| `hasImageResults` false | "No preview is available for this node." (unchanged) | – |
| `/results` in flight | Spinner (`kind: "loading"`) | – |
| Overlay available, both images load | The selected image; the control | "Overlay: the model's results drawn on the captured frame." or "Raw frame: the captured image, without the overlay." |
| No overlay image | Raw frame; no control | "Raw frame: this run has no overlay image." |
| `/results` failed or did not complete | Raw frame; no control | "Raw frame: the captured image, without the overlay." (no claim about the overlay) |
| Overlay image failed to load | Raw frame; no control | "Raw frame: the overlay could not be loaded." (warning tone) |
| Raw frame failed, overlay loads | Overlay; no control | The overlay caption |
| Both failed | "No preview is available for this node." | – |

Showing the spinner while `/results` is in flight avoids flashing the raw frame, which is the very
view the user misread, before the overlay. The request goes to the device itself and is cached for
the run (`staleTime: Infinity`, `components/react-query.ts`). On a failing `/results`,
react-query's default three retries keep the spinner for about 7 s before the raw frame appears.
That is the same query, with the same retries, that the model card already uses.

### D3. Stroke and font scale with the image's short side

With s = min(w, h):

| Parameter | Formula | 640×480 | 1280×720 | 1920×1080 | 3840×2160 |
|---|---|---|---|---|---|
| Box thickness t | max(2, ⌊s/240 + ½⌋) | 2 (3 px painted) | 3 (5 px) | 5 (7 px) | 9 (11 px) |
| Font scale | max(0.5, 0.6·s/720) | 0.5 | 0.6 | 0.9 | 1.8 |
| Font thickness | max(1, ⌊2·font scale + ½⌋) | 1 | 1 | 2 | 4 |
| Label padding | max(3, t) | 3 | 3 | 5 | 9 |
| Band for "no-helmet 64%" | text + baseline + 2·padding | 132×23 | 156×26 | 236×39 | 470×76 |

The rounding is round-half-up (`math.floor(x + 0.5)`), not Python's `round`, which rounds halves to
even; 4.5 must give 5. For s < 600 every parameter equals today's (bugfix 3.8). The label bands stay
at about 3.5 % of the image height from 720 px up.

**Why s/240 and not the investigation's s/360.** The investigation proposed
`max(2, round(min(w, h)/360))`. At 1920×1080 that is t = 3, a 5 px stroke, which is 0.83 px in the
card; at 1280×720 it is t = 2, today's 3 px. During the design, the synthetic scene of the golden
test was measured with today's marshal and with a reference implementation of D3–D5 (worst box edge
and median, card-visibility metric):

| Size | F | s/360 with the palette | s/240 with the palette (chosen) |
|---|---|---|---|
| 1920×1080 | 43 / 82 | 64 / 118 | 85 / 157 |
| 1280×720 | 64 / 123 | 64 / 118 | 96 / 176 |

These numbers are for boxes only; with labels drawn, s/240 measures 85 / 163 and 96 / 184. They
were re-measured with the scene in production shape (Testing Strategy) and did not change.

s/360 leaves 720p no better than today, and at 1080p it gets half of s/240's gain. On two 1920×1080
PPE demo frames, the worst edge went from 58–60 today to 105–118 with s/240. Those frames were drawn
with the colors the D4 key gives the demo model (helmet 0, human 1, no-helmet 2, vest 3, its
`detection.class_names` order), so they show what F' draws. At full size a 7 px stroke is 0.65 % of
the short side; it stays well inside small objects such as helmets.

Font: `font_scale ≈ w/1280 · 0.6` is the same as 0.6·s/720 for 16:9 frames. Using s keeps portrait
and very wide frames in proportion, and the 0.5 floor keeps small images at today's size. Label
*text* is not readable at card scale at any reasonable size (assumption A1). At card size the
legibility comes from the strokes and the colored label bands; the text is for full size.

### D4. An 8-color palette keyed on the network class id, with black or white text

`DETECTION_PALETTE_RGB` (RGB order, with the text color in parentheses):

| Index | Color | RGB | Text | Contrast |
|---|---|---|---|---|
| 0 | green (today's) | (0, 255, 0) | black | 15.3:1 |
| 1 | magenta | (255, 0, 255) | black | 6.7:1 |
| 2 | azure | (0, 160, 255) | black | 7.5:1 |
| 3 | orange | (255, 140, 0) | black | 9.0:1 |
| 4 | yellow | (255, 255, 0) | black | 19.6:1 |
| 5 | violet | (160, 32, 255) | white | 5.1:1 |
| 6 | red | (255, 40, 40) | black | 5.6:1 |
| 7 | aquamarine | (0, 255, 200) | black | 16.1:1 |

The contrast is the WCAG 2 contrast ratio from relative luminance. The smallest CIE76 distance
between two entries is 31 (magenta and violet); adjacent indices differ in hue.

**The key.** The entry is chosen by the first key that applies (bugfix 2.6):
1. `class_id`, when it is present, is not a boolean and `int()` accepts it: `int(class_id) mod 8`.
   The base model now sends it, as an `int`, for YOLO and RF-DETR detections;
2. otherwise `str(class)`, when `int()` accepts it: `int(str(class)) mod 8`. An unnamed YOLO or
   RF-DETR detector sends its index as a string (`str(cls_id)`, `yolo_detection_postprocessor.py:273`),
   and so does the supervised multi-head detector (`supervised_bbox_multihead_postprocessor.py:104`).
   This key decides for the multi-head detector, which sends no `class_id`, and for an unnamed
   detector on an old base model; on such a detector it gives the same entry as key 1;
3. otherwise the crc32 of the UTF-8 (surrogatepass) encoding of the resolved class label, without
   the confidence: `crc32 mod 8`.

A boolean is never a key; `True` would otherwise count as 1. The mapping is a pure function of the
payload: it never uses Python's `hash()`, which changes per process, and it gives the same color in
every run, frame and process (assumption A3). Class ids 0–7 get eight different colors; past 8
classes, colors repeat and the label text tells them apart.

**Why the class id must travel.** A named detector sends only names (Root Cause 5), and the hash of
a small name set collides often. For the PPE demo model the label hash gives helmet 6, no-helmet 6,
human 5 and vest 7: three colors, with the compliant class and the violating class both red. The
class ids give 0, 1, 2 and 3: four colors.

**How it travels (chosen: an additive `class_id` on the payload entry).**
- `ObjectDetectionResult` gains a keyword-only constructor field, `class_id: Optional[int] = None`.
  `serialize()` and `deserialize()` do not change, so every other serializer of results, such as
  `InferenceData.serialize()` (`inference_data.py:58`), and every reader keeps today's four keys.
- The YOLO and RF-DETR `_make_result` pass `class_id=int(cls_id)`. `cls_id` is the argmax column
  for YOLO (`yolo_detection_postprocessor.py:171`) and the label id after `col_to_label` for RF-DETR
  (`rf_detr_detection_postprocessor.py:149`): the same index that the class name is looked up by.
- The base model's `__build_detection_tensors` appends `entry["class_id"] = int(class_id)` after
  `class_label`, when the result's class id is an integer (`int` or a NumPy integer) and not a
  boolean.
- The marshal reads `det.get("class_id")` for the palette only. Its capture record, its detections
  block and its sidecar copy four named keys (`marshal_for_capture_template.py:449–467`), so
  `class_id` reaches no file, record or API (bugfix 3.12). Nothing else consumes the base model's
  tensors: the ensemble maps them to internal `inference_*` tensors that only the marshal reads
  (`model_convertor.py:496–517`).

A declared constructor field is used, not an attribute set after construction. That documents the
field on the class, gives every instance the attribute, and leaves `serialize()` visibly unchanged.

**Mixed versions are safe.** The post-processors and `object_detection_result.py` load from the
backend image, and the model's Startup republishes both `model.py` files together (Build and
Rollout).
- An old marshal ignores `class_id`: every box is green, as today.
- A new marshal with an old base model gets no `class_id`. Named classes then get hashed colors,
  which collide for the PPE model.
- An old post-processor with a new base model cannot happen in practice, because both come from the
  same image. It would only mean that no `class_id` is sent.

None of these fails a run. O1 checks all four files on the device, and O3 checks the colors.

**Rejected:**
- **Hashing the label alone** (the review's option B). It collides on the demo model: helmet and
  no-helmet are both red. Accepting that would need the owner's sign-off against the brief's "each
  class gets its own stable color".
- **Resolving names to ids in the marshal.** The marshal is a separate Triton model. It does not see
  the base model's manifest, and it would have to repeat the class-name lookup of two
  post-processors.
- **Colors in first-seen order.** Not stable across frames, runs or processes (A3).
- **Emitting `class_id` from `serialize()`.** It would add the key wherever results are serialized,
  not only on the way to the marshal.
- **The Okabe-Ito color-blind-safe palette.** It is less saturated. On the demo frames at 1920×1080
  and 1280×720 it measured 0.60–0.99 of today's worst edge with s/360, and 0.89–1.33 with s/240.
- **Semantic colors, such as violations in red.** The marshal does not know which classes are
  violations; this is a follow-up.

For color-blind users the label text carries the class at full size, so color is never the only
cue. Index 0 keeping today's green means that a one-class detector, whose class id is 0, looks as
it does today.

### D5. Layout and drawing are separate, and boxes are drawn before labels

`_layout_detection_overlay(width, height, detections)` returns the style and one item per drawn
detection. It is pure geometry: it calls only `cv2.getTextSize`, and it carries today's exact
expressions for:
- the valid-box filter;
- `int(round(v))` and the clamps;
- label resolution and label text;
- the band anchor (`band_x2 = min(w, x2)`, `band_x1 = max(0, band_x2 - text_w - 2*pad)`,
  `band_y1 = max(0, y1)`, `band_y2 = min(h, band_y1 + band_h)`) and the text origin
  `(band_x1 + pad, band_y1 + pad + text_h)`.

Only the sizes and colors come from D3 and D4; the geometry does not depend on the class or the
class id. `_generate_detection_overlay` swaps the channels as today, draws every item's box, then
every item's band and text. Labels are therefore on top of all strokes (bugfix 2.7), and later
labels still cover earlier ones where labels overlap (a follow-up).

The split makes the geometry testable without rasterizing (Property 6). It gives the golden test the
band and text rectangles to mask, and the on-device checker the geometry of every box.

### D6. `output_path` (item c): documented, not honored

**Honoring it was rejected**, in each of the three forms it could take:
- **As the run folder itself.** Every run of the workflow would land in one folder, and the marshal
  would derive a wrong id from that folder's name (`basename(disk_path)`,
  `marshal_for_capture_template.py:599–601`). Run retention keeps the
  `{root}/{workflow_id}/{execution_id}` layout for exactly that reason (`run_retention.py:24–25`).
- **As the root of the run folders.** Runs would be scattered across folders chosen by authors.
  Retention and continuous-run staging work on the one capture root (`run_retention.py:232` returns
  `pipeline_executor._WORKFLOW_CAPTURE_ROOT`). The backend container may also not mount, or be able
  to write, an arbitrary host path.
- **As a second copy.** The disk would grow outside any retention.

**The description is corrected instead.** This is in scope because:
- it is text only, with no runtime effect;
- it removes one of the five causes the investigation found ("anyone checking `/tmp` on the device
  finds nothing");
- it rides in the same commit.

The text is:

> Not used on LocalServer: a deployed workflow writes each run's frame, overlay and metadata to the
> run's own folder, /aws_dda/captures/{workflowId}/{executionId}/ (a continuous workflow may stage
> runs in memory instead). A value is still required, e.g. /aws_dda/captures.

The name, type, `required=True`, `default=None`, `constraints={"min_length": 1}` and
`examples=["/aws_dda/captures"]` stay, so existing workflows validate and compile byte-identically.

The Portal layer is the single source of truth (`src/backend/workflow_engine/vendor/README.md`). The
change is made there and copied with `cp` to the vendored file, as `test_vendored_catalog_mirror.py`
instructs (`:78–80`, `:189–192`): a wholesale `re_vendor.sh` would also copy the Portal-only
`catalog/platforms.py`. The `capture` entry of `catalog_baseline.json` is regenerated by the
documented maintenance path, and a note goes into `test_bug_catalog_preservation.py`, as the
security-scan-remediation-high change did for one description.

The device gets the text with the LocalServer build, where it is inert. Workflow authors see it in
the Portal's Workflow Builder after the next Portal backend deploy (the `WorkflowCoreLayer`). This
fix needs no Portal deploy.

### D7. `{capture_id}` / `{execution_id}` in result payloads (item d): a follow-up

The brief asked to document that `payload_template` can include these placeholders. That is true
only for `phase: capture`, and the catalog already says so (`nodes.py:1316–1333`), backed by
`_capture_phase_metadata` (`pipeline_executor.py:2627–2652`). A completion-phase payload, the
result message the brief means, renders over the run's tag values (`pipeline_executor.py:3941`;
`output_bindings.py:3929–3932`, `render_template` at `:343–389`), which carry neither key. An
unknown placeholder is left as written (`_LenientDict`, `output_bindings.py:335–340`). With the
executor test harness on 2026-10-15, the completion template `{{"c":"{capture_id}","e":"{execution_id}"}}`
published the placeholders literally, and `{inference_json}` held only the run metadata.

Documenting them for result messages would be wrong, and making them work is a behavior change for
every completion-phase template. bugfix.md, "Out of Scope and Follow-ups", sketches the follow-up
spec.

### Alternatives considered

| Alternative | Why not |
|---|---|
| Caption the raw frame only (the investigation's minimum) | The matches stay two clicks away; the brief asks for the overlay. |
| Reuse the Run results page's `Toggle` | It names an action, not the image shown; the card would need a separate label anyway. |
| Draw the boxes in the browser over the raw frame | It would duplicate the marshal's drawing and the mask logic in a second renderer, and it would not help the Run results page or the Portal views. |
| Enlarge the card's thumbnail | Outside the brief, and a larger card still needs a scaled overlay at 1/4–1/3 scale. |
| s/360 for the stroke (the investigation's proposal) | Measured: 720p no better than today, half the gain at 1080p (D3). |
| Color by `hash(label)` or crc32 of the label alone | `hash()` changes per process; crc32 collides within the PPE classes, helmet and no-helmet both red (D4). |
| Resolve class names to ids in the marshal | The marshal has no access to the base model's manifest and would repeat two post-processors' lookup (D4). |
| Label collision avoidance | Not asked for; a separate layout feature, recorded as a follow-up. |
| Honor `output_path` | It breaks the run API, retention and the marshal (D6). |

## Technology Stack

Locked by this design; no new dependency is added.
- **Frontend:** the LocalServer UI's React 18, TypeScript 4.9 and Cloudscape 3 (`SegmentedControl`,
  `Box`, `SpaceBetween`), with react-query v4. The lock file pins Cloudscape 3.0.363 and
  react-query 4.32.6.
- **Frontend tests:** Jest through `react-scripts` 5, React Testing Library, and fast-check 3.23.2,
  all already in `src/frontend/package.json`.
- **Marshal and base model:** Python, run by the Triton python-backend stub (3.11 on every target),
  with OpenCV (`cv2`), NumPy and the standard library (`math`, `zlib`, `collections`, `typing`).
- **Backend tests:** pytest and Hypothesis, as the existing marshal tests use them.
- **Catalog:** the shared `workflow_core` package (Python), with its tests under
  `edge-cv-portal/backend/layers/workflow_core/tests`.

## Fix Implementation

### `src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py`

Add `import math`, `import zlib` and `from collections import namedtuple` to the imports at the top
of the file, and after the `resolve_class_label` block:

```python
#: Detection overlay palette (RGB), indexed by the D4 key mod 8
#: (run-preview-overlay-legibility). Entry 0 is the one green every class
#: used before, so a one-class detector (class id 0) keeps its look.
DETECTION_PALETTE_RGB = (
    (0, 255, 0), (255, 0, 255), (0, 160, 255), (255, 140, 0),
    (255, 255, 0), (160, 32, 255), (255, 40, 40), (0, 255, 200),
)
#: Label text colour per palette entry: black or white, whichever has the
#: higher WCAG 2 contrast ratio with the band (every entry >= 4.5:1).
DETECTION_LABEL_TEXT_RGB = (
    (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0),
    (0, 0, 0), (255, 255, 255), (0, 0, 0), (0, 0, 0),
)

OverlayStyle = namedtuple(
    "OverlayStyle", "box_thickness font_scale font_thickness label_padding")
OverlayItem = namedtuple(
    "OverlayItem", "box color_bgr text_bgr label band text_origin text_box")


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def _detection_overlay_style(width, height):
    """Stroke, font and padding for a w x h overlay (D3)."""
    side = max(1, min(int(width), int(height)))
    box_thickness = max(2, _round_half_up(side / 240.0))
    font_scale = max(0.5, 0.6 * side / 720.0)
    font_thickness = max(1, _round_half_up(2.0 * font_scale))
    return OverlayStyle(box_thickness, font_scale, font_thickness,
                        max(3, box_thickness))


def _detection_color_index(class_id, class_index, class_label):
    """Stable palette index of a detection (D4).

    The first key that applies: ``class_id``, the network class id the
    base model sends; ``class_index``, the payload's ``str(class)`` (an
    unnamed detector's index, such as "3"); else the crc32 of the resolved
    class label. A key applies when it is not None, not a boolean, and
    int() accepts it. Pure and total: never raises.
    """
    for key in (class_id, class_index):
        if key is None or isinstance(key, bool):
            continue
        try:
            return int(key) % len(DETECTION_PALETTE_RGB)
        except (TypeError, ValueError, OverflowError):
            continue
    crc = zlib.crc32(str(class_label).encode("utf-8", "surrogatepass"))
    return crc % len(DETECTION_PALETTE_RGB)


def _bgr(rgb):
    return (rgb[2], rgb[1], rgb[0])


def _layout_detection_overlay(width, height, detections):
    """(style, [OverlayItem]) for the valid detections, in payload order (D5)."""
```

The body of `_layout_detection_overlay`:
- Skip every entry whose `bounding_box` does not have 4 values (`:184–186`).
- Compute `x1, y1, x2, y2` with today's `int(round(v))` and clamps (`:187–192`).
- Resolve `class_index`, `class_label` and `label` exactly as today (`:193–206`). The one difference:
  the fallback label is `str(class_label)`, so a non-string label is drawable.
- Compute `(text_w, text_h), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
  style.font_scale, style.font_thickness)`, and the band with today's expressions (`:220–224`), using
  `pad = style.label_padding`.
- Set `text_origin = (tx, ty) = (band_x1 + pad, band_y1 + pad + text_h)` and
  `text_box = (tx, ty - text_h, tx + text_w, ty + baseline)`.
- Choose the colors through `_detection_color_index(det.get("class_id"), class_index,
  class_label)`, where `class_index` is today's `str(det.get("class", ""))`, and convert them with
  `_bgr`.

**Constraint:** the module level must touch only `math`, `zlib` and `collections`, and read no `cv2`
attribute. Five metadata test modules import the template with an empty `cv2` stub when OpenCV is
missing, so any module-level `cv2.` access would break them on such a runner.

`_generate_detection_overlay(self, image, detections)` keeps its signature and its channel swap. It
also keeps its return value: a new BGR array of the image's shape.

```python
overlay = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
h, w = overlay.shape[0], overlay.shape[1]
style, items = _layout_detection_overlay(w, h, detections)
for item in items:                      # pass 1: every box
    cv2.rectangle(overlay, item.box[:2], item.box[2:], item.color_bgr,
                  style.box_thickness)
for item in items:                      # pass 2: every label, on top
    cv2.rectangle(overlay, item.band[:2], item.band[2:], item.color_bgr, -1)
    cv2.putText(overlay, item.label, item.text_origin,
                cv2.FONT_HERSHEY_SIMPLEX, style.font_scale, item.text_bgr,
                style.font_thickness, cv2.LINE_AA)
return overlay
```

Its docstring and the comment at `:174–180` change. The colors are now defined in RGB and reversed
for drawing, because the drawing array is in BGR order (the old comment relied on green and black
being symmetric under the swap). Nothing else in the file changes: `_generate_overlay`,
`_encode_overlay`, `_generate_capture_meta_data`, the detections sidecar and `execute()` stay as
they are. In particular the record and sidecar builders keep copying only `class_index`,
`class_label`, `bounding_box` and `confidence` (`:449–467`).

### The base model: the class id (four files)

`src/backend/lyra_science_processing_utils/utils/object_detection_result.py`: import `Optional` from
`typing`, and add a keyword-only field after `threshold`:

```python
def __init__(self, bounding_box: List, obj_class: str = 'default', confidence: float = 1.0,
             threshold: float = 0.5, *, class_id: Optional[int] = None):
    """
    ...
    :param class_id: The network's integer class index, when the post-processor knows it.
        Not part of serialize(): the base model passes it on for the marshal's overlay
        palette only (run-preview-overlay-legibility D4).
    """
    ...
    self.class_id = class_id
```

`serialize()` and `deserialize()` stay exactly as they are.

`yolo_detection_postprocessor.py` (`_make_result`, `:275–277`) and
`rf_detr_detection_postprocessor.py` (`_make_result`, `:217–219`) both become:

```python
return ObjectDetectionResult(
    [x_min, y_min, x_max, y_max], label, score, self.score_threshold,
    class_id=int(cls_id),
)
```

Nothing else in the two post-processors changes: the decode, the thresholds, the NMS, the scaling
and the label lookup stay as they are.

`src/backend/dda_triton/resources_for_copy/lfv_model_template.py`, `__build_detection_tensors`
(`:328–336`):

```python
serialized = []
for d in detections:
    entry = d.serialize()
    entry["class_label"] = resolve_class_label(d.obj_class, self.__class_names)
    class_id = getattr(d, "class_id", None)
    if isinstance(class_id, (int, np.integer)) and not isinstance(class_id, bool):
        # The network class id, read only by the marshal's overlay palette
        # (run-preview-overlay-legibility D4). No record or API carries it.
        entry["class_id"] = int(class_id)
    serialized.append(entry)
```

The comment at `:328–331`, which speaks of "the retained numeric `class` index", is corrected: when
the model has class names, `class` is the name. `getattr` keeps duck-typed results, such as test
fakes, working. The tensor set, `output`, the scores, the empty mask, the zero-object sentinel and
`__build_anomaly_tensors` do not change. `test/backend-test/dda_triton/test_mock_model/mock_model_template.py`
asks to be kept in sync with this file, but it has no detection-tensor builder, so the note does not
apply to this change.

### `src/frontend/src/components/deployed-workflow/graph/previewModel.ts`

- **The `image` variant:**
  `{ kind: "image"; src: string; overlaySrc?: string; overlayUnknown?: true }`.
- **`previewViewModel` args** gain `resultsLoading?: boolean` and `resultsError?: boolean`, both
  `false` by default.
- **The capture branch** (`:229–233`), evaluated in order:
  1. `!hasImageResults` → `unavailable`;
  2. `resultsLoading` → `loading`;
  3. `overlayImageSrc !== undefined` → `{kind: "image", src: imageSrc, overlaySrc: overlayImageSrc}`;
  4. `resultsError` (the results were not successfully loaded: an error, or a fetch that never
     completed) → `{kind: "image", src: imageSrc, overlayUnknown: true}`;
  5. otherwise (the results loaded and report no overlay image) → `{kind: "image", src: imageSrc}`.

  The precedence comment above `previewViewModel` (`:166–185`) is updated. Steps 1–3 of the
  existing precedence (not an output node, not terminal, failure) still come first.
- **New exports:**
  - `CaptureImageMode` and `CaptureImageDisplay`, a discriminated union on `shown`, so that `src`
    and `caption` are `string` wherever an image is shown (`tsconfig.json` sets `strict: true`):

    ```ts
    export type CaptureImageMode = "overlay" | "raw";
    export type CaptureImageDisplay =
      | { shown: "none"; captionTone: "secondary"; showModeControl: false }
      | {
          shown: CaptureImageMode;
          src: string;
          caption: string;
          captionTone: "secondary" | "warning";
          showModeControl: boolean;
        };
    ```
  - `CAPTURE_OVERLAY_CAPTION` ("Overlay: the model's results drawn on the captured frame."),
    `CAPTURE_RAW_CAPTION` ("Raw frame: the captured image, without the overlay."),
    `CAPTURE_NO_OVERLAY_CAPTION` ("Raw frame: this run has no overlay image.") and
    `CAPTURE_OVERLAY_FAILED_CAPTION` ("Raw frame: the overlay could not be loaded.");
  - `captureImageDisplay({src, overlaySrc, overlayUnknown, mode, failedSrcs})`.
- **What `captureImageDisplay` returns**, with `overlayOk = overlaySrc defined and not failed` and
  `rawOk = src not failed`:
  - **both ok:** the image named by `mode`, `showModeControl: true`;
  - **only the overlay:** the overlay, no control;
  - **only the raw frame:** the raw frame, no control. The caption is
    `CAPTURE_OVERLAY_FAILED_CAPTION` (tone `warning`) when `overlaySrc` is defined,
    `CAPTURE_RAW_CAPTION` when `overlayUnknown`, and `CAPTURE_NO_OVERLAY_CAPTION` otherwise;
  - **neither:** `{shown: "none", captionTone: "secondary", showModeControl: false}`.

  The tone is `secondary` in every case except the failed overlay.

### `.../graph/NodePreviewCard.tsx`

- **State.** Replace `failedImageSrc` (`:133`) with
  `failedSrcs: ReadonlySet<string>` (`useState(() => new Set())`) and a `markFailed(src)` that adds
  to a copy. Add `rawChosenFor: string | null`. Both hooks stay above the `kind === "none"` early
  return (`:137`).
- **The `detections` case** uses `failedSrcs.has(thumbnailSrc)` in place of the equality, with the
  same behavior.
- **The `image` case** (`:158–171`) becomes a block that computes `captureImageDisplay` and
  narrows on `shown` before it reads `src`:

  ```tsx
  case "image": {
    const overlaySrc = viewModel.overlaySrc;
    const display = captureImageDisplay({
      src: viewModel.src,
      overlaySrc,
      overlayUnknown: viewModel.overlayUnknown === true,
      mode: overlaySrc !== undefined && rawChosenFor === overlaySrc ? "raw" : "overlay",
      failedSrcs,
    });
    if (display.shown === "none") {
      body = <Unavailable />;
      break;
    }
    // Narrowed: display.src and display.caption are strings from here on.
    const shownSrc = display.src;
    body = (
      <SpaceBetween size="xs">
        {display.showModeControl && (
          <SegmentedControl
            data-testid="preview-image-mode"
            label="Image shown"
            selectedId={display.shown}
            onChange={({ detail }): void =>
              setRawChosenFor(detail.selectedId === "raw" ? overlaySrc ?? null : null)
            }
            options={[
              { id: "overlay", text: "Overlay" },
              { id: "raw", text: "Raw frame" },
            ]}
          />
        )}
        <img
          data-testid="preview-thumbnail"
          data-shown={display.shown}
          src={shownSrc}
          alt={`Output of ${nodeId}, ${display.shown === "overlay" ? "overlay" : "raw frame"}`}
          style={THUMBNAIL_STYLE}
          onError={(): void => markFailed(shownSrc)}
        />
        <Box
          data-testid="preview-image-caption"
          variant="small"
          color={display.captionTone === "warning" ? "text-status-warning" : "text-body-secondary"}
        >
          {display.caption}
        </Box>
      </SpaceBetween>
    );
    break;
  }
  ```
- Import `SegmentedControl` from `@cloudscape-design/components` with the existing imports
  (`:20–28`). The footer link and every other case are unchanged.

### `.../graph/RunStatusGraph.tsx`

- Add `const selectedIsCapture = selectedNode?.type === "capture";` next to `selectedIsModel`
  (`:124`).
- `resultsEnabled` (`:143–146`) becomes
  `(selectedIsModel || selectedIsCapture) && (success || warning) && hasImageResults`.
- Update the comment at `:139–142`.
- In the `previewViewModel` call (`:406–418`), add:

  ```ts
  resultsLoading: resultsEnabled && resultsQuery.isInitialLoading,
  // Unknown covers an error and a fetch that never completed (e.g. paused offline).
  resultsError: resultsEnabled && !resultsQuery.isSuccess,
  ```

  `resultsError` keys on "not successfully loaded" rather than on `isError`: in react-query 4.32.6
  a fetch that starts while `navigator.onLine` is false waits with `fetchStatus: 'paused'` (the
  default `networkMode: 'online'`; `components/react-query.ts` sets only `staleTime`), where
  `isInitialLoading` and `isError` are both false and `data` is undefined. With `isError` the card
  would then claim "this run has no overlay image", a fact it does not know (bugfix 2.2). Step 2
  of the capture branch still catches the initial load first, and step 3 still wins when earlier
  data reported an overlay. A disabled query reports `isLoading` in react-query v4, so
  `resultsLoading` mirrors the metadata guard at `:415`.

`hasOverlayImage` and `overlayImageSrc` (`:152–162`) are unchanged and are already passed for every
node type. The model branch ignores the two new arguments.

### Catalog

- In `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`, replace the
  capture `output_path` description (`:1529–1531`) with the D6 text, split over string literals the
  way the neighbouring descriptions are. Then
  `cp edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py
  src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`.
- Regenerate only the `capture` entry of `catalog_baseline.json`, from
  `edge-cv-portal/backend/layers/workflow_core` with `PYTHONPATH=python`:
  1. load the file;
  2. set `baseline["capture"] = dataclasses.asdict(get_node_type("capture"))`;
  3. write it back with `json.dump(..., indent=2, sort_keys=True, ensure_ascii=False)` plus a
     trailing newline.

  These settings reproduce today's file byte-for-byte; the design review checked this.
  `git diff --stat` must then show one changed line in the JSON.
- Append to the maintenance comment block in `test_bug_catalog_preservation.py` (the `#:` lines
  from `:84`): "The run-preview-overlay-legibility spec rewrote the ``capture`` ``output_path``
  description to say LocalServer does not write there. Only that entry was regenerated per the same
  maintenance path; ONLY that description moved."

### Not changed

- `RunResults.tsx`, `WorkflowRegistrationAPI.ts`, and every other LocalServer UI file.
- The backend API and artifact code: `api.py`, `download_file.py`, `run_artifacts.py`,
  `detections.py`, `pipeline_executor.py` (`output_path` is still not used), `output_bindings.py`,
  `run_retention.py`.
- In the base model's code path: `inference_data.py`, the model graphs,
  `supervised_bbox_multihead_postprocessor.py` (it already sends integer strings), every other
  post-processor, `model_convertor.py`, `triton_setup.py`, and every other file under
  `resources_for_copy/`.
- `test/backend-test/dda_triton/test_mock_model/mock_model_template.py` (no detection-tensor builder).
- The copies under `edge-cv-portal/test-sandbox/dda_triton_resources/` of the marshal, the base model
  template, `object_detection_result.py` and the two post-processors. They are staged by hand when
  the sandbox image is rebuilt, and three of them already differ from the source.
- The Portal frontend and its other backend code; the HMI, which shows `/output-image` only; and
  `docs/multi-runtime-inference.md`, which describes the overlay without its style or the payload's
  keys.
- Recipes, Dockerfiles, `docker-compose.yaml`, `requirements.txt` and `build-custom.sh`.

### Invariants and where they are enforced

| Invariant | Enforced by | Why there |
|---|---|---|
| Which detections are drawn, their coordinates, the label text and the anchor are today's (3.1) | `_layout_detection_overlay`, the only place the geometry is computed | One function, pinned by Property 6 against an independent re-implementation of today's expressions. |
| A class keeps its color (2.6) | `_detection_color_index`, a pure function of the payload | No process state, no `hash()`. |
| The palette key is the network class id when the detector has one (2.6, 2.10) | The two `_make_result` (set it), `__build_detection_tensors` (send it) and `_detection_color_index` (key order) | The post-processor is the only place that knows the integer; Properties 5 and 11. |
| `class_id` never leaves the ensemble (3.12) | The marshal's record, block and sidecar builders, which copy four named keys | Unchanged code; P10 and U8 assert it. |
| `serialize()` keeps today's four keys (3.12) | `ObjectDetectionResult.serialize()`, unchanged | Every other serializer of results stays byte-identical; U8. |
| Label text contrast ≥ 4.5:1 (2.6) | The constant table, checked by a unit test that recomputes the WCAG ratio | A table cannot drift at run time; the test catches a palette edit. |
| The raw frame is never modified (3.2) | `_generate_detection_overlay` draws on the `cvtColor` copy; the raw JPEG comes from the pipeline buffer through `emlcapture` | Unchanged; Property 9 asserts the input array is untouched. |
| The capture card always says which image it shows (2.1–2.3) | `captureImageDisplay`, which returns a caption whenever it returns an image | The only place the card decides what to show; Property 2. |
| The card never re-shows a URL that failed | `captureImageDisplay` (it never returns a failed URL) and `NodePreviewCard`'s `failedSrcs` | The state stays in the presentational component, as today. |
| The catalog mirror is byte-identical (3.10) | `test_vendored_catalog_mirror.py` | Existing guard. |
| Only the `output_path` description moved (3.9) | `catalog_baseline.json` (regenerated entry), the existing preservation tests, and a new pin test on the parameter's other fields | The baseline proves the other node types; the pin proves the other fields of this one. |

### Error handling

| Operation | Failure | Recoverable? | What the caller receives | Logged |
|---|---|---|---|---|
| `/results` for a capture card | Network error, 4xx or 5xx | Yes, on a page reload. react-query first retries three times, its default (`components/react-query.ts` sets only `staleTime`), so the spinner shows for about 7 s | The raw frame, captioned `CAPTURE_RAW_CAPTION`, no control | react-query's default console logging only (unchanged) |
| Overlay `<img>` load | 404, empty file, undecodable | Yes, on reload | The raw frame with the warning caption, no control | None (unchanged) |
| Raw `<img>` load | 404 (for example, a node-image-only run) | – | The overlay if it loads, else "No preview is available" | None (unchanged) |
| Both `<img>` loads | Both fail | – | "No preview is available for this node." | None |
| Color index | `class_id` is missing, a boolean or not an integer | Handled | The next key decides: an integer `class`, else the label hash | None: a log line would repeat on every frame |
| Color index | `class` is not an integer | Handled | The crc32 fallback entry | None, for the same reason |
| Color index | The label holds lone surrogates | Handled | `surrogatepass` encodes them | None |
| Base model, `class_id` | The result has no integer class id (another producer, a boolean, or `None`) | Handled | The entry has today's keys only | None |
| Mixed versions on a device | A new marshal runs with an old base model, for example after a hot-patch of the marshal alone | Yes, at the model's next Startup, which republishes both | Hashed colors for named classes, which can collide; the run succeeds | None in the marshal; O1 and O3 detect it on the device |
| Label text | The label is not a string and the confidence is not a number | Handled | `str(class_label)`; today this crashed in `putText` | None |
| Box coordinates | NaN or infinite | No; unchanged from today | `int(round(v))` raises; the Triton request and the run fail through the existing error path | The existing Triton and backend error lines |
| Overlay encode | `cv2.imencode` fails | Yes, per frame (unchanged) | An empty `overlay` tensor, so no `.overlay.jpg`; the capture card shows "no overlay image" | ERROR "Unable to encode overlay for output" (existing) |
| Baseline regeneration | The diff touches more than the capture description line | Developer time | The task stops; nothing is committed | – |

The new code adds no failure mode for any input today's code accepts. The style function is total
for w, h ≥ 1, the color function is total for any JSON value, the drawing calls are the same four
per detection, and the base model's addition only reads an attribute and tests its type.

### Inputs and validation

- **Detection payload** (JSON, from the base model through the reused `anomalies` tensor). Every rule
  is unchanged except those marked new.
  - `bounding_box`: drawn only when its length is 4; values rounded and clamped.
  - `class_id` (new, optional): the base model sends it as an `int`, never a boolean. Any value that
    is not a boolean and that `int()` accepts picks the palette entry (a float 2.9 gives 2, a
    string "3" gives 3); any other value is ignored and the next key decides. It is never copied
    into a record.
  - `class`: any value, as `str()`. When there is no usable `class_id`, `str(class)` picks the
    palette entry when `int()` accepts it (new), for example "3", " 3 " or "+3". Anything else,
    such as a name, a boolean (`"True"`) or a float (`"2.0"`), falls through to the label hash
    (new). This is bugfix 2.6's rule, stated once for both documents.
  - `class_label`: any value; when empty, it falls back to `resolve_class_label`, then to the class
    string.
  - `confidence`: a number gives `NN%`; anything else gives the label alone.
- **`ObjectDetectionResult(class_id=...)`**: keyword-only, optional, `None` by default. The two
  post-processors pass a Python `int`; the class does not validate it, and the base model sends it
  only when it is an integer.
- **Image**: an H×W×3 uint8 RGB tensor with H, W ≥ 1 (unchanged); the style uses min(H, W).
- **`/results` payload**: `images[]`. `hasOverlayImage` is optional and read with `!!` as today, so a
  missing field means no overlay.
- **URLs**: built only by `workflowExecutionOutputImageUrl` and `workflowExecutionOverlayImageUrl`
  from the route's execution id and the auth token; nothing comes from the user.
- **The control**: `selectedId` is "overlay" or "raw"; any other value is treated as "overlay".

### Edge cases

- **Zero detections or the sentinel.** The overlay is the unannotated copy, captioned as the overlay.
  The sentinel entry has no `class_id`.
- **A segmentation run.** The capture card shows the mask overlay. The Run results page keeps its
  client-side mask composite (3.3).
- **A box past the image edge, or with x1 > x2.** Clamped as today, and the band anchored at
  `min(w, x2)` as today.
- **A label wider than its box.** The band extends left and is clamped at 0. The text can then run
  past the band's right edge, as today; the golden mask covers the text box.
- **Nested boxes.** Strokes never cross labels. Two labels can still overlap; the later one wins.
- **More than 8 classes, or hashed labels.** Colors repeat; the labels tell them apart.
- **Class names that look like numbers**, such as `["3", "1"]`. `class_id` comes first, so the
  network index decides, not the name.
- **A named detector on a stale base model.** Hashed colors until the next Startup republishes
  `base_<model>/1/model.py` (D4, mixed versions).
- **Very large frames** (for example 4608×3288): t = 14 (15 px painted), font 2.74.
- **Very small images** (the tests use 2×2 to 40×40): today's style; the bands are clamped as today.
- **A portrait image.** The style uses the short side.
- **Several capture nodes in one run.** They show the same two images, and the choice of raw frame
  carries across them; another run starts on the overlay.
- **Auth enabled.** Both URLs carry the token in the query string, as today.
- **The run still active when the capture node has finished.** `/results` is fetched once, as for the
  model card. The overlay is written in the pipeline, before the node turns terminal; the executor
  reads the run's capture record right after the pipeline returns (`pipeline_executor.py:2179`).
- **A stale marshal on the device.** If a model was loaded from the old `model.py` before its Startup
  republished, the overlay keeps the old style until the next load. On the device, O1 checks the files
  and O2 checks that the loaded marshal draws with them.

## Correctness Properties

Property 1: Bug Condition - The capture card shows the overlay

_For any_ selected capture node with status `success` or `warning` in a run with image results
whose `/results` output entry has `hasOverlayImage`, the fixed graph SHALL render the overlay URL
in `preview-thumbnail` with `data-shown="overlay"`, the caption `CAPTURE_OVERLAY_CAPTION` and the
"Overlay" / "Raw frame" control. Choosing "Raw frame" SHALL render the raw URL with
`CAPTURE_RAW_CAPTION`. F renders the raw URL with no caption and no control.
**Validates: 2.1, 2.4**

Property 2: Capture display invariants

_For any_ raw URL r, optional distinct overlay URL o, `overlayUnknown` flag, mode m and failed set
S ⊆ {r, o}, `captureImageDisplay` SHALL:
- never return a URL in S;
- return `shown: "overlay"` only with `src = o`, and `shown: "raw"` only with `src = r`;
- set `showModeControl` exactly when both o and r are available, and then return `shown = m`;
- return a caption exactly when `shown ≠ "none"`, starting with "Overlay:" exactly when the overlay
  is shown;
- use the warning tone exactly when o is defined and failed and r is shown;
- return `"none"` exactly when no URL is available.
**Validates: 2.1, 2.2, 2.3**

Property 3: Preview preservation

_For any_ node type other than `capture`, `previewViewModel'` SHALL return the same value as F's for
the same arguments, whatever `resultsLoading` and `resultsError` are. For `capture`, a missing or
non-terminal status SHALL give `pending`, `failure` SHALL give `failure`, and `hasImageResults`
false SHALL give `unavailable`, exactly as F.
**Validates: 3.6, 3.7**

Property 4: Style

_For all_ w, h ≥ 1, `_detection_overlay_style(w, h)` SHALL equal the D3 formulas. It SHALL be
non-decreasing in min(w, h), and equal (2, 0.5, 1, 3) whenever min(w, h) < 600. The painted stroke
of a box SHALL measure 2⌈t/2⌉+1 px across its left edge.
**Validates: 2.5, 3.8**

Property 5: Color stability and contrast

_For any_ class id value i, class value c and label l, `_detection_color_index(i, c, l)` SHALL be
deterministic and SHALL never raise. It SHALL equal:
- int(i) mod 8 when i is not a boolean and `int(i)` succeeds;
- otherwise int(c) mod 8 when c is not a boolean and `int(c)` succeeds;
- otherwise crc32(the UTF-8 surrogatepass encoding of str(l)) mod 8.

`_layout_detection_overlay` calls it with i = the payload's `class_id`, c = `str(class)` and l =
the resolved class label, so for a payload this is exactly bugfix 2.6's rule.
Any 8 consecutive integer keys SHALL map to 8 distinct entries. The text color of every entry SHALL
have the higher WCAG contrast of black and white, at 4.5:1 or more.
**Validates: 2.6**

Property 6: Geometry preservation

_For any_ image size and any detection list (valid and invalid boxes, unsorted and out-of-range
coordinates, any class, class id, label and confidence values), `_layout_detection_overlay` SHALL:
- return one item per entry with a 4-value box, in payload order;
- give each item the box `clamp(int(round(v)))` that today's expressions give;
- give each item today's label text, with `str()` applied to the fallback label (identical for
  string labels, which every production payload has);
- place the band and text origin with today's anchor expressions, evaluated with the D3 sizes.

These are compared with an independent re-implementation in the test. No geometry depends on the
class or the class id.
**Validates: 3.1**

Property 7: Labels on top

_For any_ two detections where the second one's box crosses the first one's band, the second one's
band and text box do not intersect the first one's band, and the first one's text box lies inside
its band (the band is not clamped at the left edge, Edge cases), every band pixel of the first
detection outside its text box (grown by font thickness + 1 px) SHALL hold the first detection's
color after drawing, and the first detection's text pixels SHALL be the same as when it is drawn
alone. Where two labels do intersect, the later one covers the earlier one (D5).
**Validates: 2.7**

Property 8: Card visibility

On the synthetic scene at 1920×1080 and 1280×720, the fixed overlay's card-visibility worst edge
SHALL be ≥ 70 and its median ≥ 140. F measures 43 / 82 and 64 / 123.
**Validates: 2.8**

Property 9: Raw-frame and non-detector preservation

_For any_ input, `_generate_detection_overlay` SHALL leave the input array unchanged.
`_generate_overlay` (anomaly) SHALL produce output byte-identical to the golden captured on F. A
single detection whose palette entry is 0, given as an integer class 0 or in production shape with
`class_id` 0, on a 640×480 or 480×360 image SHALL produce output byte-identical to the golden
captured on F. The base model's anomaly tensors SHALL be byte-identical to F's.
**Validates: 3.2, 3.5, 3.8**

Property 10: Catalog

The capture `output_path` description SHALL contain "Not used on LocalServer" and
"/aws_dda/captures/{workflowId}/{executionId}/". The parameter's other fields SHALL equal their F
values, every `catalog_baseline.json` entry other than `capture` SHALL stay byte-identical, the
existing catalog preservation tests (`test_bug_catalog_preservation.py`,
`test_catalog_stream_and_analytics.py`, `test_catalog_modbus_write.py`) SHALL CONTINUE TO pass, and
the two `nodes.py` copies SHALL be byte-identical. (The `mqtt_publish` baseline entry is
deliberately a pre-Bug-2 recording that the live serialization differs from, so "every other
descriptor equals its baseline entry" would be false on F already; the existing tests handle it.)
**Validates: 2.9, 3.9, 3.10**

Property 11: Class id carriage

_For any_ YOLO or RF-DETR output, with or without class names, each result SHALL carry `class_id`
equal to the network class index that its class name is looked up by, and `serialize()` SHALL
return exactly today's four keys. _For any_ list of results, `__build_detection_tensors` SHALL
emit each entry as today's entry followed by `"class_id": int(i)` exactly when the result's class
id i is an integer and not a boolean. It SHALL emit every other tensor, and the zero-object
sentinel, byte-identically to F. On F, a named detector's entry has no `class_id`.
**Validates: 2.10, 3.12**

## Testing Strategy

### Test environment

- **Backend, on the host**, from the repository root:
  `PYTHONPATH=src/backend python3 -m pytest -q -p no:cacheprovider <paths>`. The existing marshal
  tests run with real OpenCV and NumPy: at `1fa52f3a`, `test_marshal_*.py` and
  `test_overlay_path_consistency.py` give 46 passed, and `test_lfv_detection_tensor_set.py` gives 3
  passed.
- **The detector post-processor suites need an image.** `test_yolo_detection_postprocessor.py` and
  `test_rf_detr_detection_postprocessor.py` cannot be collected on the host: `model_processors/__init__.py`
  imports every post-processor, and one of them imports `sklearn`, which the host's Python 3.9
  lacks (checked at `1fa52f3a`). They run in the flask-app image, with the image command below;
  task 2 records their baseline there. The new class-id tests do not have this problem:
  `_overlay_support.load_postprocessors()` imports the two modules normally, and only when that
  fails loads them by file path behind a stand-in `lyra_science_processing_utils.model_processors`
  package. That is the pattern of
  `edge-cv-portal/backend/tests/test_model_converter_preserve_aspect.py:85–115`.
- **Backend, in each built image**: the overlay suites also run inside the flask-app image under
  python3.11, the interpreter of the Triton python-backend stub that runs the marshal and the base
  model. That checks the golden tolerance against each image's OpenCV (task 7).
- **The image command, and why it sets `DDA_APP_ROOT`.** Every run in a flask-app image in this
  spec (tasks 2, 3.10, 4, 5 and 7, and N1) uses this command, naming the freshly built image where
  one exists:
  ```
  docker run --rm -v "$(pwd)":/repo -w /repo -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONPATH=/repo/src/backend -e DDA_APP_ROOT=/repo/src/backend flask-app:latest bash -lc \
    'export LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}; \
     PY=${PY:-$(command -v python3.11 || command -v python3.10)}; \
     $PY -m pip install --no-cache-dir --quiet pytest sarge hypothesis pydantic==2.6.4; \
     $PY -m pytest -q -p no:cacheprovider <paths>'
  ```
  Both templates put `os.environ.get("DDA_APP_ROOT", "/")` first on `sys.path` before they import
  the lyra packages (`marshal_for_capture_template.py:42–44`, then `:55`;
  `lfv_model_template.py:39–41`, then `:53–64`), and the flask-app image ships its own copies of
  both packages at `/` (`Dockerfile.jp5:234–235`, with no WORKDIR, `:197`). Without
  `DDA_APP_ROOT=/repo/src/backend`, a session in which a template loads first imports the image's
  packages instead of the branch's. The existing marshal suites load the template during
  collection (`test_marshal_overlay_drawing_examples.py:88`, five sibling `test_marshal_*.py` files
  and `test_overlay_path_consistency.py:122`), so this depends on collection order. In a combined
  run, list `test/backend-test/overlay_legibility/` first as well: its support module then pins
  both packages before any template loads. The command installs `sarge`, as builds.md's pattern
  does, because `test/backend-test/conftest.py` imports it at module level (`:25`) and
  `src/backend/requirements.txt` does not list it. The same conftest imports `pydantic` (`:20`).
  On JP6 the image installs the app requirements for the DDA interpreter, 3.10, only, and
  python3.11 there carries just the Triton stub's set (`Dockerfile.jp6:143`: numpy, opencv-python,
  scikit-learn, dlr, Pillow and dill, plus onnxruntime). The command therefore installs
  `pydantic==2.6.4`, the `requirements.txt` pin, which pip leaves alone where it is already present;
  without it, every module in the JP6 image's python3.11 run fails at conftest import. `PY` is taken
  from the environment when set, so the interpreter can be chosen with `-e PY=...` before the image
  name. On JP6, run the command once as written (python3.11, the stub interpreter), and once with
  `-e PY=python3.10` added before the image name.
- **Portal layer**, two invocations:
  - **The catalog files on the host.** From `edge-cv-portal/backend/layers/workflow_core`, run
    `PYTHONPATH=python python3 -m pytest -q -p no:cacheprovider tests/test_bug_catalog_preservation.py
    tests/test_catalog_capture_output_path.py`. These import only `workflow_core.catalog`. At
    `1fa52f3a` the first file gives 13 passed. The mirror guard,
    `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py`, gives 8 passed.
  - **The full suite, in a Python 3.11 container.** The layer vendors `rpds` as a compiled
    cpython-311 x86_64 module, which the host's interpreter cannot load: all 51 test modules that
    reach `jsonschema` fail to collect on the host. Run:
    ```
    docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$(pwd)":/repo \
      -w /repo/edge-cv-portal/backend/layers/workflow_core python:3.11-slim \
      bash -c 'pip install --quiet jsonschema==4.26.0 pytest==8.4.2 hypothesis==6.141.1 \
      && python -m pytest tests -q -p no:cacheprovider'
    ```
    The conftest appends `python/` after site-packages, so the installed packages win.
    `PYTHONDONTWRITEBYTECODE` keeps the container from leaving root-owned caches in the worktree.
    The baseline was not measured during the design; task 2 records it.
- **A pre-existing failure, left as it is.**
  `test/backend-test/csi_nvargus_optional/test_preservation_csi_surface.py::test_backend_csi_consumer_file_untouched[...vendor/workflow_core/catalog/nodes.py]`
  already fails at `1fa52f3a`: the pin is 35bc1b2b and the file is e9cc8f43. It keeps failing with a
  new hash after D6.
  - Re-pinning it here would also absorb the earlier, unrelated drift, so it is recorded instead.
  - Its three sibling pins pass; `pipeline_executor.py` is not touched.
- **Frontend**: the worktree has no `node_modules`, so run `npm ci` in `src/frontend` first. Then
  `CI=true npx react-scripts test --watchAll=false --testPathPattern=components/deployed-workflow`,
  and `CI=true npm run build` for the type check and lint.

### The shared support module

`test/backend-test/overlay_legibility/_overlay_support.py` is used by the exploration, preservation,
property and golden tests, and by the on-device checker.

**At import, before any loader runs,** it imports `lyra_science_processing_utils` and
`lyra_anomalies_mask_utils` and checks that both come from this worktree's `src/backend`. Both are
regular packages with an `__init__.py`, so `__file__` is set:

```python
import os

import lyra_anomalies_mask_utils
import lyra_science_processing_utils

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SRC_BACKEND = os.path.realpath(os.path.join(_REPO_ROOT, "src", "backend"))
for _pkg in (lyra_science_processing_utils, lyra_anomalies_mask_utils):
    if not os.path.realpath(_pkg.__file__).startswith(_SRC_BACKEND + os.sep):
        raise RuntimeError(
            f"{_pkg.__name__} resolved to {_pkg.__file__}, not this worktree's src/backend; "
            "in a flask-app container set DDA_APP_ROOT=/repo/src/backend")
```

Importing both packages first also pins every later submodule import, the templates' included, to
the branch's copies, because a package's submodules resolve from its own `__path__`. If an
earlier module in the session already imported the image's copies, the check fails at collection
and names the remedy, instead of letting E8, U8 or P11 test the wrong code. On the host both
packages come from `src/backend` through `PYTHONPATH`, so the check passes.

It provides:
- `load_marshal(path=None)`: stubs only `triton_python_backend_utils` (with `Tensor` and
  `triton_string_to_numpy`) and imports the marshal template with the real `cv2`, as
  `test_marshal_overlay_drawing_examples.py` does. It loads `path` when given, else the worktree's
  `marshal_for_capture_template.py`, under a new module name on each call, so that the on-device
  checker can hold F's marshal next to the branch's;
- `load_lfv()`: imports `lfv_model_template.py` as `test_lfv_detection_tensor_set.py` does. It stubs
  `triton_python_backend_utils` with a `Tensor` that records the name and the array, and stubs
  `lyra_science_processing_utils.model_graph_factory` only when importing the real one fails;
- `load_postprocessors()`: the YOLO and RF-DETR post-processor classes (see Test environment);
- `synthetic_scene(w, h)`, `scene_detections(w, h)`, `clamped_boxes(w, h, detections)` and
  `card_visibility(raw_bgr, overlay_bgr, boxes)`, as below.

None of these imports a function that exists only on F'.

### The synthetic scene and the card-visibility metric

The scene has no people, so it can be committed.

- **The scene** is RGB, like the input tensor: four vertical bands of equal width, with
  x ∈ [⌊i·w/4⌋, ⌊(i+1)·w/4⌋) and RGB (245, 245, 245), (200, 200, 200), (128, 128, 128) and
  (20, 20, 20).
- **The detections** are in production shape: each has `class` = `class_label`, as a named detector
  sends it, and the `class_id` below, with `bounding_box = [a·w, b·h, c·w, d·h]` as floats:

  | class_label (and class) | class_id | confidence | (a, b, c, d) | Exercises |
  |---|---|---|---|---|
  | human | 1 | 0.91 | (0.10, 0.20, 0.45, 0.98) | a large box over two bands |
  | helmet | 0 | 0.97 | (0.20, 0.21, 0.30, 0.33) | nesting |
  | vest | 3 | 0.59 | (0.15, 0.40, 0.40, 0.75) | nesting |
  | no-helmet | 2 | 0.64 | (0.55, 0.30, 0.70, 0.55) | mid gray and dark |
  | human | 1 | 0.88 | (0.52, 0.25, 0.90, 1.05) | the bottom clamp |
  | widget | – | 0.50 | (0.00, 0.00, 0.08, 0.12) | no `class_id`: the label hash (entry 0), the corner, a clamped band |
  | tool | 4 | "n/a" | (0.78, 0.10, 0.95, 0.22) | the label without a percentage |

  The ids follow the PPE demo model's order. F ignores `class_id`. On F' this payload draws exactly
  what integer classes with the same ids drew during the design, so the D3 numbers hold; they were
  re-measured in this shape.
- **`card_visibility(raw_bgr, overlay_bgr, boxes)`**, where `raw_bgr` is the channel-swapped input
  and `boxes` are the clamped boxes:
  1. Compute the card size: scale = min(320/w, 200/h), cw = max(1, round(w·scale)),
     ch = max(1, round(h·scale)).
  2. Resize both images to (cw, ch) with `cv2.INTER_AREA`, and compute D = per-pixel max over
     channels of |overlay − raw| as int16.
  3. For each box, with f = coordinate·scale:
     - rows = [⌊fy1⌋+2, max(⌊fy1⌋+3, ⌊fy2⌋−1)) and cols = [⌊fx1⌋+2, max(⌊fx1⌋+3, ⌊fx2⌋−1)),
       each intersected with the image;
     - the left and right edges take, per row, the max of D over the columns {⌊fx⌋−1, ⌊fx⌋, ⌊fx⌋+1}
       and average over the rows;
     - the bottom edge does the same over the rows {⌊fy2⌋−1, ⌊fy2⌋, ⌊fy2⌋+1}, per column;
     - the top edge is skipped, because the label band sits on it;
     - an edge whose rows, columns or window are empty after the intersection is skipped too,
       which no box of the synthetic scene hits; on device frames it keeps the metric defined for a
       box at the border.
  4. Return the minimum ("worst") and the median over all edges.

  The metric is alignment-robust: the 3-pixel window catches a line wherever the resize puts it.

### Exploration tests (must FAIL on F)

| ID | File | Asserts | On F it fails because |
|---|---|---|---|
| E1 | `test/backend-test/overlay_legibility/test_overlay_legibility_exploration.py` | One class-1 box (400, 300, 1500, 800) on a uniform RGB (128, 128, 128) 1920×1080 image: in row 550, the run of pixels that differ from the background around x = 400 is 7 px wide | It is 3 px |
| E2 | same | Classes 1 and 2, non-overlapping, at (100, 100, 400, 600) and (700, 100, 1000, 600) on a uniform gray 1280×720 image: the left-edge midpoints `overlay[350, 100]` and `overlay[350, 700]` hold BGR (255, 0, 255) and (255, 160, 0) | Both are green |
| E2b | same | E2's boxes in production shape: `{"class": "helmet", "class_label": "helmet", "class_id": 0}` and `{"class": "no-helmet", "class_label": "no-helmet", "class_id": 2}`. The midpoints hold BGR (0, 255, 0) and (255, 160, 0) | Both are green (measured) |
| E3 | same | Property 8 on the synthetic scene | 43 / 82 and 64 / 123 |
| E7 | same | Labels on top (defect 1.5). U5's A, then B, on uniform gray 1280×720. R is the set of pixels of overlay([A]) inside A's box whose max channel is ≤ 60: A's label text, the only dark pixels there. overlay([A, B]) equals overlay([A]) on R | B's stroke overwrites 4 of the 144 text pixels (measured; 0 of 202 on the reference F') |
| E8 | `test/backend-test/overlay_legibility/test_class_id_exploration.py` | A synthetic YOLO output, `[1, 8, 8400]` with class names `[helmet, human, no-helmet, vest]` and two anchors scoring in columns 0 and 2, through `YoloDetectionPostProcessor.__call__` and then the base model's `__build_detection_tensors`. Each decoded `anomalies` entry has `class` the name and `class_id` its column (0 and 2) | The entries carry no `class_id` |
| E4 | `edge-cv-portal/backend/layers/workflow_core/tests/test_catalog_capture_output_path.py` | The description contains "Not used on LocalServer" and "/aws_dda/captures/{workflowId}/{executionId}/" | It says images are written there |
| E5 | `src/frontend/src/components/deployed-workflow/graph/RunStatusGraph.captureOverlay.exploration.test.tsx` | Property 1, with the API mocked as in `RunStatusGraph.preview.test.tsx` and `/results` returning `[{kind: "output", hasOverlay: true, hasOverlayImage: true}]`: the overlay URL, the caption and "Raw frame" switching | It shows `/output-image/exec-1` with no caption |
| E6 | same | With `/results` pending forever, the capture card shows `preview-loading` | It shows the raw frame |
| E6b | same | `getWorkflowExecutionResults` rejects (the harness's QueryClient has `retry: false`, `RunStatusGraph.preview.test.tsx:90–92`): the capture card shows `preview-thumbnail` with `src` `/output-image/exec-1`, the caption `CAPTURE_RAW_CAPTION`, and no toolbar named "Image shown". This drives a rejected `/results` through the `resultsError` wiring of `RunStatusGraph.tsx`, which the pure-function tests cannot reach | It shows the raw frame with no caption |

E1–E3 and E7 call only `_generate_detection_overlay`, and E8 only the post-processor's `__call__`
and `__build_detection_tensors`; all exist on F. Each test must fail on an assertion, not on an
import or harness error.

### Preservation tests (must PASS on F and on F')

| ID | File | Asserts |
|---|---|---|
| P1 | `test_overlay_legibility_preservation.py` (Hypothesis, at most 25 examples, the budget of the `fast` profile in `test/backend-test/conftest.py:40–49`) | Sizes w, h from 64 to 2160 px on a uniform image, with one detection inside it. Take t', the font scale and thickness, and the padding from the D3 formulas (each is at least F's value); let m = ⌈t'/2⌉ + 1, and band_w', band_h' the label's band at those sizes (`cv2.getTextSize`). The box is drawn with width ≥ band_w' + 2m and height ≥ band_h' + 2m + 1. Every pixel outside the box grown by m, and every pixel inside the rectangle (x1 + m, y1 + band_h' + m)–(x2 − m, y2 − m), equals the channel-swapped input |
| P2 | same | Boxes of length 0, 1, 2, 3 and 5, and the sentinel, give an overlay equal to the swapped input |
| P3 | same | One detection (label "part", confidence 0.87, box (0.2w, 0.2h, 0.7w, 0.8h)) on the synthetic scene at 640×480 and 480×360, in two shapes: `{"class": 0, "class_label": "part"}` and `{"class": "part", "class_label": "part", "class_id": 0}`. Both equal `goldens/preserved_class0_{w}x{h}.png`, captured on F (measured byte-identical with the reference F') |
| P4 | same | `_generate_overlay` on a fixed 256×192 image with a two-color mask equals `goldens/preserved_anomaly_256x192.png`, captured on F |
| P5 | same | The input array equals its pre-call copy after `_generate_detection_overlay` (the synthetic scene at 640×480) |
| P6 | existing | The marshal suites: 46 tests, including the RGB/BGR round trip, the sentinel copy and the overlay-ref property |
| P7 | `RunStatusGraph.captureOverlay.preservation.test.tsx` | The model card's detections and overlay thumbnail; the llm, bedrock and mqtt previews; the non-output detail; the capture card's pending, failure and unavailable states; a capture card without an overlay showing `/output-image`. `/results` is not requested for llm, bedrock, mqtt, failed or running nodes |
| P8 | existing | `RunResults.test.tsx`, `RunResults.analytics.test.tsx`, `RunStatusGraph.test.tsx`, `RunStatusGraph.preview.test.tsx`, `previewModel.test.ts` and `NodePreviewCard.test.tsx`, run unmodified on F. After the fix, only the arbitraries of `NodePreviewCard.test.tsx` gain the new `image` fields |
| P9 | existing, plus E4's file | `test_bug_catalog_preservation.py`, `test_vendored_catalog_mirror.py`, and a pin of `output_path`'s other fields: name, `param_type "string"`, `required True`, `default None`, `constraints {"min_length": 1}`, `examples ["/aws_dda/captures"]`, `multiline False`, `depends_on None` |
| P10 | `test_overlay_legibility_preservation.py` | The base model's tensors, through `_overlay_support.load_lfv()`. Every case uses `input_np = np.zeros((8, 8, 3), dtype=np.uint8)` and an instance with the dtypes of `_make_detection_model_instance` (`test_lfv_detection_tensor_set.py:124–136`: uint8 `output`, `mask` and `anomalies`, float32 confidence and score). The detection path, and the anomaly path without a mask, emit `mask` as `np.zeros(input_np.shape)` (`lfv_model_template.py:364`, `:415`), and the golden stores every tensor's bytes, so a 640×480 frame would add about 1.2 MB of base64 per case; with 8×8×3 the JSON stays under 20 KB. Inputs: results built with today's positional constructor (a named class, an integer string, float coordinates); one of them also given a `class_id` attribute with `setattr`, which F ignores; the zero-object case; and `__build_anomaly_tensors` without a mask and with a 4×4 index mask of two pixel classes (a fake graph config). `--capture` records every tensor's name, dtype, shape and bytes in `goldens/preserved_lfv_tensors.json` on F. On F and F': the anomaly tensors and the sentinel are byte-identical; every detection tensor other than `anomalies` is byte-identical; `anomalies` is byte-identical when no input has a class id; with one, the decoded list equals the golden after removing `class_id`, key order included |
| P11 | existing | `test_lfv_detection_tensor_set.py` on the host (3 passed), and `test_yolo_detection_postprocessor.py` and `test_rf_detr_detection_postprocessor.py` in the flask-app image, run unmodified |

The P3, P4 and P10 goldens are captured on F with
`python3 test/backend-test/overlay_legibility/test_overlay_legibility_preservation.py --capture`
(with `PYTHONPATH=src/backend`). The command refuses to overwrite an existing golden unless
`--force` is given, so they cannot be recaptured on F' by accident. A missing golden fails the test,
with the command in the message.

### Unit, property and golden tests for the new code

**Backend**, in `test_overlay_style_properties.py`:
- **U1 (Property 4).** The D3 table at 2×2, 40×30, 599×599, 600×600, 640×480, 1280×720, 1500×1500,
  1920×1080, 1080×1920, 3840×2160 and 4608×3288. Three of them pin the round-half-up, where
  Python's `round` would give one less:
  - 600×600: s/240 = 2.5 → t = 3;
  - 1500×1500: 2 × 1.25 = 2.5 → font thickness 3;
  - 1920×1080: 4.5 → t = 5.

  Then Hypothesis for monotonicity and for the s < 600 identity.
- **U2 (Property 5).** Recompute the WCAG ratio for each entry, and check the text table's choice and
  the 4.5:1 bound.
- **U3 (Property 5).** Hypothesis over the three arguments. `class_id` and `class` each range over:
  absent (`None`), booleans, integers (including negatives), integer strings (with whitespace),
  non-numeric strings, floats as strings, NaN and infinity. Labels include lone surrogates. The
  test checks:
  - the key order;
  - that ids 0–7 are distinct;
  - that the index is the same across two calls;
  - that a boolean never decides;
  - that a subprocess with a different `PYTHONHASHSEED` gives the same index for "widget";
  - through `_layout_detection_overlay`, that each item's color is the entry that bugfix 2.6's
    rule gives for the payload's `class_id` and `class`, with `class` taken as `str(class)`. So a
    float `class` such as 2.0 falls through to the label hash, while a float `class_id` such as
    2.9 gives entry 2.
- **U4 (Property 6).** Hypothesis, with generators modelled on those of
  `test_marshal_overlay_dimensions.py` (written out in the new file, not imported). They are extended
  with float coordinates, negative and out-of-range values, `class_id` values, and sizes up to 2160.
  The reference re-implements today's expressions in the test.
- **U5 (Property 7).** At 1280×720 on uniform gray: detection A (class 0, box (200, 150, 600, 500))
  and then detection B (class 1, box (520, 100, 900, 600)). B's left edge at x = 520 runs through A's
  band, (482, 150)–(600, 176). B's own band, (781, 100)–(900, 126), lies above and to the right of
  it, so the pair meets Property 7's condition. Every pixel of A's band outside A's text box, grown
  by font thickness + 1 px, must hold A's color; the reference implementation gave 0 such pixels
  off-color. U5 runs on
  F' only: on F the same count measures F's smaller band (A alone gives 141, A with B 138), not the
  crossing. E7 covers the crossing on F.
- **U6 (Property 4).** The measured stroke width across a box's left edge: 3 px at 640×480, 5 at
  1280×720, 7 at 1920×1080 and 11 at 3840×2160.
- **U7.** After `_encode_overlay` and `cv2.imdecode`, a stroke-center pixel of each palette entry,
  drawn at 1920×1080, is within ±40 per channel of the entry. JPEG is lossy, so the tolerance is
  wide.
- **U9 (Property 5).** The four PPE classes in production shape, with `class_id` 0–3, on
  separate boxes at 1920×1080 on uniform gray. Their left edges hold four distinct palette colors,
  entries 0, 1, 2 and 3. The same payload without `class_id` gives entries 6, 5, 6 and 7, the
  collision that D4 avoids.

**Backend**, in `test_detection_class_id.py` (**U8**, Property 11), on the host:
- **YOLO.** With and without class names, letterboxed and not: each result's `class_id` equals its
  column, `obj_class` is the name or `str(column)`, and `serialize()` has exactly the keys
  `bounding_box`, `class`, `confidence` and `confidence_threshold`.
- **RF-DETR.** With sigmoid scoring, and with softmax and a background class: `class_id` equals the
  label id from `col_to_label`, not the raw column.
- **`ObjectDetectionResult`.** `class_id` defaults to `None`; a fifth positional argument raises
  `TypeError`; positional construction and `deserialize(serialize(r))` are unchanged.
- **The base model.** Hypothesis over lists of results whose `class_id` is `None`, a Python `int`, a
  NumPy `int64`, a boolean or a string. The entry gets `"class_id": int(i)` as its last key exactly
  for a non-boolean integer; otherwise its JSON equals today's entry. The sentinel is unchanged.

**The goldens**, in `test_overlay_goldens.py`: the synthetic scene at 1920×1080, 1280×720 and
640×480 against `goldens/detection_overlay_{w}x{h}.png`. The PNGs are lossless, and measured at 34,
18 and 12 KB with the reference implementation.
- The mask is the union over items of the band rectangle and the text box, grown by
  font thickness + 2 px.
- Outside the mask, pixel-exact.
- Inside each masked rectangle, a mean absolute difference ≤ 6.0 over its pixels and channels.
  That tolerates OpenCV's anti-aliasing differences between builds, but catches a font, scale,
  thickness or color change.
- Regenerate only for an intended, reviewed style change, with
  `python3 test/backend-test/overlay_legibility/test_overlay_goldens.py --regenerate`, after
  looking at the PNGs.

**Frontend:**
- **`previewModel.capture.property.test.ts`** (fast-check, 100 runs): the five-step capture
  precedence, Property 2, and Property 3. Property 3 is tested as invariance: for any non-capture
  node type and any other arguments, every combination of `resultsLoading` and `resultsError` gives
  the same value as leaving both out. Together with the existing `previewModel.test.ts` properties,
  which pass unmodified on F and F', that pins the other node types.
- **`NodePreviewCard.capture.test.tsx`**:
  - the four captions and tones;
  - the control's presence;
  - "Raw frame", then "Overlay", switching `src` and `data-shown`;
  - `fireEvent.error` on the overlay → raw frame with the warning caption and no control;
  - errors on both images → `preview-unavailable`;
  - a changed `overlaySrc` resets the card to the overlay;
  - the alt text names the image shown ("…, overlay" or "…, raw frame").
- **Querying the control.** Cloudscape 3.0.363's `SegmentedControl` renders both a segment toolbar
  and a `Select` for narrow containers, and jsdom applies no CSS, so both are in the accessibility
  tree. E5 and `NodePreviewCard.capture.test.tsx` therefore click within the toolbar:
  `within(screen.getByRole("toolbar", { name: "Image shown" })).getByRole("button", { name: "Raw frame" })`.
- **`NodePreviewCard.test.tsx`**: `viewModelArb`'s `image` arbitrary gains optional `overlaySrc` and
  `overlayUnknown`, and Property 4 (the results link in every state) keeps passing.

### Unit and integration split

| Level | What | Tests |
|---|---|---|
| Pure unit | Style, color and layout functions | U1–U4, Properties 4–6 |
| Raster unit (real OpenCV) | Drawing order, stroke width, colors, encode round trip, goldens, card visibility | U5–U7, U9, the goldens, E1–E3, E2b, E7, P1–P5 |
| Unit (base model) | Class id carriage through the post-processors and the base model's tensors | E8, U8, P10, P11 |
| Pure unit (TypeScript) | `previewViewModel`, `captureImageDisplay` | Properties 2–3 |
| Component | `NodePreviewCard`; `RunStatusGraph` with the API mocked at the module boundary | E5, E6, E6b, P7, `NodePreviewCard.capture.test.tsx` |
| Catalog | The descriptor text and pins, the mirror, the baseline | E4, P9 |
| Device | The template and package refresh, the real overlay and its colors, the UI, health | O1–O10 |

### On-device verification (MIC-730, JP5)

builds.md requires on-device verification before the implementation is committed. The procedure:

1. **Before deploying, on the current build.**
   - Record the LocalServer version, the backend container's `RestartCount` and the free space on
     `/aws_dda`.
   - Read the PPE model's `detection.class_names` from the deployed model's `manifest.json`, which
     `base_<ppe model>/1/` links to (read only). The expected value is
     `[helmet, human, no-helmet, vest]`.
   - Run the PPE demo workflow at least 40 times over at least one full loop of its video: MQTT
     triggers at the demo's usual rate, or manual runs (`POST /workflows/registrations/{id}/trigger`).
   - For three runs with at least four detections of at least two classes, and one run with none,
     save `/output-image`, `/overlay-image` and `/metadata` on the host, outside the repository.
   - Compute `card_visibility` with the boxes from `/metadata` (`x_min`…`y_max`), recorded as
     context for O4, and record the model node's `durationMs` p50 from `/node-status`.
2. **Deploy safely.** Revise only the LocalServer component version, following the MIC-730
   deployment safety steps:
   - stop every trigger publisher before the deployment;
   - trigger no run until the deployment is COMPLETED and every model component's Startup has
     finished (READY, or "gave up" in its log);
   - kick the PPE model with one manual run and wait for READY.

   If a model stays LOADING for more than 10 minutes and the backend log shows
   `Python model file not found`, capture the evidence and stop. A backend restart needs the
   owner's go-ahead.
3. **Repeat step 1's runs and downloads** on the new build.
4. **Check** in a browser on the LocalServer UI: the run status graph of a run with detections, and of
   the empty run, then the Run results page. If the implementer has no browser on the device's
   network, the owner does this check; record who did it.

#### The host-side checker

The checker is a script kept with the downloaded images, outside the repository, and it is not
committed. It runs on the host from the branch worktree, with `PYTHONPATH` set to `src/backend`
and `test/backend-test/overlay_legibility`. It loads the branch's marshal through
`_overlay_support.load_marshal()` and uses its `_layout_detection_overlay` and
`DETECTION_PALETTE_RGB`. For O4 it also loads F's marshal and uses
`_overlay_support.card_visibility` (below).

`/metadata` gives no class or class id per box: its detections are `{id, label, confidence, x_min,
y_min, x_max, y_max}` (`detections.py:262`, `:360–367`). The checker therefore takes the geometry
from the layout, which does not depend on the class, and the color from the overlay. For each
`/metadata` detection of a run, with the decoded overlay and raw frame (BGR):
1. Build `{"bounding_box": [x_min, y_min, x_max, y_max], "class": label, "class_label": label,
   "confidence": confidence}` and lay it out with `_layout_detection_overlay(w, h, …)`. This gives
   the clamped box (x1, y1, x2, y2), the band height band_h and the style's t; let
   m = ⌈t/2⌉ + 1.
2. Take the edge column x1 when x1 ≥ m, else x2 when x2 ≤ w − 1 − m. When neither holds, skip the
   box as "both edges at the border".
3. Take the rows [y1 + band_h + m, y2 − m]. Skip the box when there are fewer than 10 rows.
4. **c_b**, the box's drawn color, is the per-channel median of the overlay along that column over
   those rows.
5. **The entry** is the palette entry (in BGR) nearest c_b by max-channel difference; **dist** is
   that difference.
6. **The width** is the median, over 5 evenly spaced rows of the range, of the run of contiguous
   pixels around the column whose max-channel difference from c_b is at most 60.
7. **The raw rate** is the fraction of the raw frame's pixels on that column and those rows that lie
   within ±30 of c_b on every channel.

Over the three sampled runs, at least 8 boxes of at least 2 labels must be checkable; the skipped
boxes are counted and recorded. During the design a reference checker was run after a JPEG round
trip:
- on the synthetic scene and on a 1920×1080 PPE demo frame drawn by the reference F', dist was at
  most 3 and the width 6–7 px for every box, with entries helmet 0, human 1, no-helmet 2 and vest 3;
- drawn by F, every box was entry 0 with a 3 px width;
- with the class id missing, as on a stale base model, the entries were 6, 5, 6 and 7.

**F's overlay on the same frames, for O4.** Visibility depends on the frame, so O4 compares the
device's overlay with F's overlay of the same raw frame, not with other frames of the loop:
1. From the worktree root, write F's marshal to a temporary directory outside the repository:
   `TMP_F=$(mktemp -d)`, then
   `git show 1fa52f3a:src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py > "$TMP_F/marshal_f.py"`.
   Load it with `_overlay_support.load_marshal("$TMP_F/marshal_f.py")`, and make an instance with
   `TritonPythonModel.__new__`, as `test_marshal_overlay_drawing_examples.py:99` does.
2. For each sampled post-deploy run with detections, decode its `/output-image` (BGR) and convert
   it to RGB, the marshal's input order. Draw F's overlay with `_generate_detection_overlay`,
   using that run's `/metadata` detections built as in step 1 of the per-box procedure.
3. Round-trip F's overlay through `cv2.imencode(".jpg", …)` and `cv2.imdecode`, as the device's
   overlay went through `_encode_overlay` (`marshal_for_capture_template.py:540–546`) before it
   was downloaded.
4. Compute `card_visibility(raw, device_overlay, boxes)` and `card_visibility(raw, F_overlay,
   boxes)` with the same raw frame and the same clamped boxes. The run's ratio is the device's
   worst edge divided by F's. A run whose F worst edge is 0 is skipped and recorded.
5. Delete `$TMP_F` afterwards.

On the design's two demo frames, this ratio was about 1.75–2.0 (D3: 58–60 for F, 105–118 for the
reference F').

Acceptance criteria:

| # | Check | Pass |
|---|---|---|
| O1 | The code is refreshed | The sha256 of `/aws_dda/dda_triton/triton_model_repo/marshal_<ppe model>/1/model.py` equals that of the branch's `marshal_for_capture_template.py`. The sha256 of `base_<ppe model>/1/model.py` equals that of the branch's `lfv_model_template.py`. In the backend container (read-only `sha256sum`), `/lyra_science_processing_utils/utils/object_detection_result.py` and the post-processor that the deployed detector uses equal the branch's files: `yolo_detection_postprocessor.py` for `yolo_object_detection` (the PPE demo model), `rf_detr_detection_postprocessor.py` for `rf_detr_object_detection` (the registry entry that the model's manifest selects, `model_processors/__init__.py:62–63`). If any differs, or if O2 or O3 then shows the old style or hashed colors (a model loaded before the republish): stop and ask the owner; do not copy files, reload models or restart by hand. |
| O2 | Style at 1920×1080 | Every checkable box has dist ≤ 40 and a width of 7 ± 2 px; at another resolution, 2⌈t/2⌉+1 ± 2 with t from D3. The bands are filled, and the text reads at 1:1. |
| O3 | Colors | Within a run, boxes with different labels map to different entries. Across the sampled runs, each label maps to one entry. Each label's entry is its index in the deployed model's `detection.class_names` mod 8: helmet 0, human 1, no-helmet 2 and vest 3 for the demo model. |
| O4 | Card visibility | The median, over the three sampled post-deploy runs with detections, of the device-to-F worst-edge ratio on the same raw frame (see "F's overlay on the same frames") is at least 1.5. The pre-deploy medians are recorded as context only. |
| O5 | Geometry | For every `/metadata` box, the overlay differs from the raw frame along all four edge centerlines (mean max-channel difference ≥ 60), outside the label bands, which the checker takes from `_layout_detection_overlay`. The empty run's overlay is within a mean absolute difference of 3 of its raw frame. |
| O6 | Raw frames | For each label, the post-deploy raw rate is at most the pre-deploy raw rate plus 5 percentage points, both measured with the same c_b (that label's median c_b over the post-deploy overlays). The raw frame is 1920×1080, as before. A hi-vis vest close to its palette color raises both rates equally. |
| O7 | Capture card | The overlay shows by default, with its caption and control. "Raw frame" and "Overlay" switch the image and the caption. The empty run shows the overlay caption. |
| O8 | Unchanged views | The model card lists the detections as before, with the new style in its thumbnail. The Run results page's "Show bounding boxes" toggle switches overlay and raw as before. |
| O9 | Cost | The model node's `durationMs` p50 is within +5 ms, or +5 %, whichever is larger, of the pre-deploy value. |
| O10 | Health | Over at least 30 minutes of demo runs: no backend container restart or crash loop, `RestartCount` unchanged, the PPE model READY throughout, and no "Unable to encode overlay" in the backend log. |

**On a failed check.** O1 has its own rule, in its row. If any of O2–O10 fails:
- stop, and record the evidence (the downloads, the checker's output and the relevant log lines);
- leave the trigger publishers stopped and the demo workflow unchanged;
- ask the owner whether to roll back by redeploying the previous LocalServer version (Build and
  Rollout, "Rollback"). The device stays on the new build until the owner decides;
- do not commit.

### JP6, JP7 and x86 non-regression

Run this after the MIC-730 passes, building one target at a time (builds.md):

- **N1, in every built image.** Run `test/backend-test/overlay_legibility/` (listed first), the
  existing marshal suites (P6) and the base model and post-processor suites (P11) inside the
  freshly built flask-app image under python3.11, the stub interpreter. Use the image command of
  "Test environment", which follows the builds.md `docker run` pattern, sets
  `DDA_APP_ROOT=/repo/src/backend` and installs `pydantic==2.6.4` for the conftest. On JP6, run the
  command once as written (python3.11, the stub interpreter), and once with `-e PY=python3.10`
  added before the image name. A golden that exceeds its tolerance under an image's OpenCV is a
  failure to fix, not to regenerate around.
- **N2, on a JP6 Jetson AGX Orin and a JP7 Jetson Thor.** Deploy with the deployment safety steps
  and run a detection workflow. Over a 15-minute soak, check:
  - O1, with the post-processor that device's detector uses (an RF-DETR model such as `canele`
    maps to `rf_detr_object_detection`, `model_processors/__init__.py:62–63`);
  - O2 at that camera's resolution, using the D3 table;
  - O3 against that device's detection model's `detection.class_names`;
  - O5, O7, O8 and O10.

  If the device has a segmentation or anomaly workflow, check that its model card and Run results
  page are unchanged, and that the capture card shows its mask overlay. A failed check follows the
  MIC-730's rule ("On a failed check").
- **N3, x86** (`aws.edgeml.dda.LocalServer.amd64`). Build and run N1. Run N2 only if an x86
  LocalServer device with a detection model is available; otherwise record "not verified on x86
  hardware" in `verification-notes.md`.

## Build and Rollout

- **Scheduling.** Start only after the `triton-model-cleanup-wedge` builds have finished; the owner
  schedules it. Never run two component builds at once (`pgrep -af "gdk component build"`,
  `pgrep -af "build-custom.sh"`), and never run a Portal deploy during a build. Move
  `edge-cv-portal/infrastructure/cdk.out` aside and get the guard pair green first (builds.md).
- **Order.**
  1. JP5 (`aws.edgeml.dda.LocalServer.arm64JP5`), then the MIC-730 verification;
  2. JP6 (`aws.edgeml.dda.LocalServer.arm64JP6`);
  3. JP7 (`aws.edgeml.dda.LocalServer.arm64JP7`);
  4. x86 (`aws.edgeml.dda.LocalServer.amd64`).

  Restore `gdk-config.json` and `recipe.yaml` after each build.
- **Preservation-tracked files.** None is expected to change. At `1fa52f3a`, no file this spec edits
  has an entry under `test/backend-test/security/baselines/`. The JP5, JP6 and JP7 masked Dockerfile
  baselines mention `resources_for_copy` and the two templates only in comments. None of the edited
  files is in `repo_audit.py`'s in-scope list, and the build gate (`build-custom.sh:247–381`) runs
  none of the suites this spec touches. Re-check before the build.
- **How the code reaches a model.**
  - The new backend image carries `object_detection_result.py` and the post-processors at
    `/lyra_science_processing_utils/`: the Dockerfiles copy them to the image root
    (`Dockerfile.jp5:197`, `:233–235`, and the same lines in the JP6, JP7 and x86 Dockerfiles). The
    Triton python-backend stub imports them from there (`lfv_model_template.py:31–41`) when it loads
    the base model.
  - The backend re-syncs `/aws_dda/resources_for_copy/`, with both templates, when it starts
    (`triton_setup.py:164–179`).
  - Each model component restarts through its HARD dependency on LocalServer
    (`edge-cv-portal/backend/functions/greengrass_publish.py:316–320`). Its Startup republishes
    `base_<model>/` and `marshal_<model>/` (`model_convertor.py:752–762`) and loads them.
  - If `triton-model-cleanup-wedge` has landed, its content-aware publish sees the changed
    `model.py` files and swaps the directories.
  - Either way, O1 confirms the files and O2 and O3 the loaded code before the other checks. The
    mixed states of D4 are safe but visible.
- **Portal.** No deploy is needed. The `output_path` text reaches the Workflow Builder with the next
  Portal backend deploy, which this spec does not schedule.
- **Rollback.** Redeploy the previous LocalServer version. The previous templates come back with the
  models' next Startup, and the previous post-processors with the previous image. The change keeps
  no state and alters no schema, API or configuration.

## Risks and Limits

- **OpenCV differences between images.** The anti-aliased label text can differ by a few levels
  between OpenCV builds. The golden masks the text and band areas with a tolerance, and N1 runs the
  goldens in every image.
- **A stale marshal or base model.** A device where a model loaded before its Startup republished
  keeps the old style, or hashed colors, until the next load. O1 and O3 catch it; the fix itself
  does not reload models.
- **Other detection producers.** A future detector post-processor with class names must pass
  `class_id` the same way, or its classes get hashed colors, which can collide. The field's
  docstring says so.
- **Overlapping labels** on nested boxes remain (a follow-up).
- **Thicker strokes cover more pixels.** At 1080p, 3 px of the stroke fall inside the box. Objects of
  a few pixels are rare at that resolution, and the Run results page's toggle still shows the raw
  frame.
- **Colors are not semantic.** The violation class of the demo, no-helmet, is azure, and in another
  model a violation class can be green. Semantic colors are a follow-up.
- **Not optimized for color vision deficiency.** Saliency was chosen over the Okabe-Ito palette. The
  label text carries the class at full size, and the caption and control in the card are text.
  Whether this meets WCAG 1.4.1 for every user needs manual testing with assistive technologies and
  an accessibility review; this spec does not claim it.
- **The capture card adds one `/results` request per run.** It is shared with the model card and the
  Run results page through the query cache.
- **A pre-existing failing pin.** The CSI suite's pin for the vendored `nodes.py` stays failing, as it
  does at `1fa52f3a`. Re-pinning it is left to a separate change.

## Design-gate disposition

The design-review loop reached its iteration cap with no HIGH finding open. Its last review (the
third revision) left 1 MEDIUM and 4 NIT findings, all on the test harness and the verification
plan rather than on the fix (D1–D7 are unchanged). Under the standing design-gate policy the loop
ended there, and every open finding was applied in this revision; none is carried as an open task:
- MEDIUM, the in-image test command selected python3.11 on JP6, which lacks `pydantic`: the
  command now honors `PY` from the environment and installs `pydantic==2.6.4`, the Test environment
  documents the JP6 interpreter split, and N1 and task 7 run twice on JP6 (Testing Strategy; tasks.md
  Overview and task 7).
- NIT, a `/results` fetch that never completes was captioned as "no overlay image": `resultsError`
  keys on `!isSuccess`, D2's row reads "failed or did not complete", and E6b drives a rejected
  `/results` through the graph wiring (Fix Implementation, D2, Exploration tests; tasks 1 and 3.5).
- NIT, bugfix 3.9 and Property 10 overstated baseline equality (false for `mqtt_publish` on F):
  reworded to byte-identity of the other baseline entries plus the existing preservation tests.
- NIT, O1 checked only the YOLO post-processor: O1, N2 and tasks 6–7 check the post-processor the
  deployed detector uses.
- NIT, Property 6 promised today's label text where today's code has none: the fallback label is
  compared with `str()` applied.
The responses to the earlier review rounds are not part of the spec and are removed from this
document; the review artifacts are not part of the documents-only commit.
