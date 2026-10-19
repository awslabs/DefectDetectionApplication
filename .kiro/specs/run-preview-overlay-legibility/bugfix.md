# Bugfix Requirements Document

## Introduction

During the PPE demo on a JP5 MIC-730 (LocalServer `aws.edgeml.dda.LocalServer.arm64JP5` 1.0.54,
2026-10-15), the owner reported that "the output images for the ppe demo is just the 1st frame not
anything that gets matches". The investigation found no defect in the frame path: every run
captured its own frame at the right position of the demo video loop, and `/output-image` and
`/overlay-image` were distinct for every run. The report is still accurate as a perception, and
two product defects cause it. The LocalServer UI's run status graph shows the raw frame on the
capture node's card, and the model's overlay is drawn at a fixed size that all but disappears when
a 1920×1080 frame is shown in the 320×200 preview card. A third fact shapes the fix: a detector
with class names sends the marshal only the class name, so the network's class index, the natural
key for a stable class color, never reaches the code that draws the overlay.

**Root cause, verified against the worktree at `1fa52f3a`.** That commit is the branch base. It
differs from `795ce528`, where the investigation took its references, only in `README.md`, so
every code reference below holds at both.

| Location | What it does |
|---|---|
| `src/frontend/src/components/deployed-workflow/graph/previewModel.ts:229–233` | The capture branch of `previewViewModel` returns `{kind: "image", src: imageSrc}` whenever the run has image results. It never uses the overlay. |
| `src/frontend/src/components/deployed-workflow/graph/RunStatusGraph.tsx:110–114` | `imageSrc` is `workflowExecutionOutputImageUrl(...)`: `/output-image`, the raw `{capture_id}.jpg` (`src/backend/endpoints/download_file.py:164–191`, `src/backend/workflow_engine/run_artifacts.py:89–103`). |
| `RunStatusGraph.tsx:124`, `:143–162` | The `/results` query, and with it `hasOverlayImage` and the `/overlay-image` URL (`:157–162`), are enabled only for a selected `model_inference` node (`selectedIsModel`, `:124`, used at `:144`). |
| `src/frontend/src/components/deployed-workflow/graph/NodePreviewCard.tsx:158–171`, `:49–53` | The `image` case renders that one URL in a 320×200 thumbnail, with the alt text "Output of {nodeId}" and no visible label of what it shows. |
| `src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py:172–238` | `_generate_detection_overlay` draws every box with `cv2.rectangle(..., (0, 255, 0), 2)` (`:208`) and every label at `font_scale = 0.5`, `font_thickness = 1` (`:214–215`), with a 3 px pad (`:219`) on a filled green band with black text (`:225–237`). The values are the same for every class and every image size. |
| `src/backend/lyra_science_processing_utils/model_processors/yolo_detection_postprocessor.py:270–277`, `rf_detr_detection_postprocessor.py:212–219` | When the model has class names, `_make_result` replaces the network's integer class with its name, and the `ObjectDetectionResult` keeps only the name. |
| `src/backend/lyra_science_processing_utils/utils/object_detection_result.py:55`, `src/backend/dda_triton/resources_for_copy/lfv_model_template.py:333–336` | `serialize()` emits that name as `class`, and the base model forwards the entry with `class_label` added. The marshal reads `class` as a string (`marshal_for_capture_template.py:193`, `:453`). For the PPE demo model, whose manifest has `detection.class_names` `[helmet, human, no-helmet, vest]`, a capture record carries `"class_index": "helmet"`. No integer class reaches the marshal. |

At 1920×1080 the card shows the frame at 1/6 scale. OpenCV paints the thickness-2 stroke 3 px wide,
which is half a pixel in the card, and the 12 px label text is 2 px tall. Every class is the same
green, so the classes cannot be told apart at card size. The only legible view of the matches is
the Run results page, two clicks away.

**Related items the investigation listed, and the decision on each** (design.md D6 and D7):
- **(c) The capture node's `output_path` is ignored. In scope, as documentation.** The catalog
  describes it as the "Folder on the device file system where captured JPEG images are written"
  (`src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py:1527–1532`; the Portal layer
  copy is byte-identical). The executor replaces the compiled `file-target_{output_path}-jpg` target
  of every terminal capture element with the run's own folder (`pipeline_executor.py:1340`, `:1402`,
  `:1416`). That folder is `{capture_root}/{workflowId}/{executionId}` (`:1854–1856`), and the root
  is `/aws_dda/captures` (`:261`). Honoring the parameter is rejected, because the run API, run
  retention and the marshal's id derivation all rely on that layout (`run_retention.py:24–25`). The
  description is corrected instead.
- **(d) `{capture_id}` and `{execution_id}` in `mqtt_publish` payloads. A follow-up, not part of
  this fix.** The catalog already documents both placeholders for `phase: capture` publishes, where
  they work (`nodes.py:1316–1333`, `pipeline_executor.py:2627–2652`). A completion-phase payload, the
  result message, renders against the run's tag values (`pipeline_executor.py:3941`,
  `output_bindings.py:3929–3932`, `render_template` at `:343–389`), and those have neither key. An
  unknown placeholder renders literally (`_LenientDict`, `output_bindings.py:335–340`). Run through
  the executor test harness on 2026-10-15, a completion-phase template with both placeholders
  published them literally, as `{capture_id}` and `{execution_id}`. Documenting them for result
  messages would therefore be wrong. Making them work is a behavior change, recorded under Out of
  Scope and Follow-ups.

**Who is affected.**
- Every deployed workflow whose capture card is opened for a run that has an overlay image. That
  covers detection models, and segmentation models with a mask.
- Every detection overlay on frames whose short side is 600 px or more.
- Every detector with more than one class. A one-class detector keeps today's green, because its
  class id is 0. On frames whose short side is under 600 px, a single detection's overlay stays
  byte-identical. With several detections, only the pixels where a later box crossed an earlier
  label change (defect 1.5).

The overlay JPEG is produced by the marshal, so the style change reaches every view of it:
- the model card and the Run results page on LocalServer;
- the Portal's results viewer, for a device that uploads its captures;
- the legacy Pipeline_Configuration path, which renders through the same marshal.

**Assumptions** (checked in the design review):
- A1. "Legible at card size" means that every box and its class color are clearly visible in the
  320×200 card. Label text cannot be legible at 1/6 scale: 8 px text in the card would need labels
  about 50 px tall on a 1080 px frame, which would cover the objects. Label text must be legible at
  full size, as on the Run results page.
- A2. The overlay is the default on the capture card for every run that has an overlay image,
  including a segmentation run, whose overlay is the mask drawn on the frame.
- A3. "Stable color" means that a class keeps its color within a model, on every run, frame and
  process. The color is keyed on the network's class id, the class's index in the model's class
  list. Two models that order their classes differently can give the same class name different
  colors. Distinct colors are guaranteed for class ids 0–7. A model with more than eight classes
  reuses colors (id mod 8), and its label text tells those classes apart.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a run's capture node has finished (`success` or `warning`) and the run's `/results` output
entry reports `hasOverlayImage` THEN the capture card shows only `/output-image`, the raw frame,
and does not say that it is the raw frame.

1.2 WHEN the user opens the capture card THEN the system offers no way to see the overlay there;
the overlay is reachable only through the model card or the Run results page.

1.3 WHEN the marshal draws a detection overlay on an image whose short side is 600 px or more THEN
the stroke thickness (2), the font scale (0.5) and the label padding (3 px) do not grow with the
image, so in the 320×200 card the boxes are sub-pixel and the labels are unreadable.

1.4 WHEN a detection overlay contains several classes THEN every box and label band is the same
green (0, 255, 0), so the classes cannot be told apart.

1.5 WHEN a box crosses an earlier detection's label THEN its stroke is drawn over that label,
because each detection's box and label are drawn in one pass, in detection order.

1.6 WHEN a workflow author sets the capture node's `output_path` THEN LocalServer writes nothing
there, while the catalog says the captured images are written to that folder.

1.7 WHEN a YOLO or RF-DETR detector has class names THEN the base model's detection payload carries
only the class name in `class`, and the network's integer class id is lost before the marshal. A
hash of the names is the only key left, and it collides: "helmet" and "no-helmet" both give
crc32 mod 8 = 6.

### Expected Behavior (Correct)

2.1 WHEN the capture card shows a finished run whose `/results` output entry reports
`hasOverlayImage` THEN the system SHALL:
- show the overlay image (`/overlay-image`) by default;
- name the image shown, in visible text and in the image's alt text;
- offer a two-option control, "Overlay" and "Raw frame", that switches the card to the raw frame
  and back.

2.2 WHEN the run has image results but no overlay image THEN the capture card SHALL show the raw
frame, labelled as the raw frame of a run that has no overlay image. WHEN the `/results` request
fails THEN the card SHALL show the raw frame, labelled as the raw frame, without claiming that no
overlay exists.

2.3 WHEN the overlay image fails to load THEN the capture card SHALL show the raw frame with a
warning-toned label saying the overlay could not be loaded, and SHALL hide the control. WHEN
neither image loads THEN the card SHALL show the existing "No preview is available for this node."
message.

2.4 WHEN the capture node has finished and the `/results` request is still in flight THEN the
capture card SHALL show the loading indicator instead of the raw frame.

2.5 WHEN the marshal draws a detection overlay on an image of width w and height h, with
s = min(w, h), THEN it SHALL use:
- box thickness t = max(2, ⌊s/240 + ½⌋);
- font scale max(0.5, 0.6·s/720);
- font thickness max(1, ⌊2·font scale + ½⌋);
- label padding max(3, t).

At 1920×1080 that is 5, 0.9, 2 and 5 px, and OpenCV paints the stroke 7 px wide.

2.6 WHEN the marshal draws a detection THEN it SHALL color the detection's box and label band with
entry k of a fixed 8-color palette. k is set by the first of these keys that applies:
1. the detection's `class_id`, when it is present, is not a boolean and `int()` accepts it:
   k = `int(class_id)` mod 8. The base model sends `class_id` only as an integer (2.10);
2. otherwise the string form of its `class`, `str(class)`, when `int()` accepts it, as for an
   integer string such as `"3"`: k = `int(str(class))` mod 8;
3. otherwise the crc32 of the UTF-8 (surrogatepass) encoding of the resolved class label, without
   the confidence: k = crc32 mod 8.

The label text SHALL be black or white, whichever contrasts more with the band, at 4.5:1 or more
for every palette entry. Entry 0 is today's green, with today's black text. For the PPE demo model
this gives helmet green (0), human magenta (1), no-helmet azure (2) and vest orange (3).

2.7 WHEN the marshal draws several detections THEN it SHALL draw every box first and every label
band and label text after, so that no box stroke crosses a label.

2.8 WHEN the synthetic test scene of design.md is drawn at 1920×1080 and at 1280×720 and shown at
card size THEN every box edge SHALL reach a visibility of at least 70, and the median edge at
least 140, on the metric design.md defines. Today's values are 43 and 82 at 1920×1080, and 64 and
123 at 1280×720.

2.9 WHEN a workflow author reads the capture node's `output_path` description THEN it SHALL say
that LocalServer does not write to that folder, and name the run folder
`/aws_dda/captures/{workflowId}/{executionId}/`.

2.10 WHEN the YOLO or the RF-DETR post-processor produces a detection THEN the base model SHALL add
the network's integer class to that detection's payload entry as `class_id`, after the existing
keys. A detection whose producer has no integer class id SHALL get no `class_id`; the supervised
multi-head detector, for example, already sends integer strings such as `"3"` in `class`.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the marshal draws a detection overlay THEN it SHALL CONTINUE TO:
- draw exactly the entries whose `bounding_box` has 4 values, in payload order;
- round and clamp the box coordinates as today;
- build each label from the same text (`f"{class_label} {float(conf) * 100:.0f}%"`, or the label
  alone when the confidence is not a number);
- anchor the label band at the box's upper-right corner, inside the box and clamped to the image;
- return an unannotated copy of the frame when no entry has a valid box, as for the zero-object
  sentinel.

3.2 WHEN a run is captured THEN the raw frame (`{capture_id}.jpg`, served by `/output-image`)
SHALL CONTINUE TO be byte-identical to today's. The marshal draws on a copy and never modifies the
input tensor.

3.3 WHEN the Run results page shows a run THEN its image, its overlay toggle ("Show bounding boxes"
or "Show overlay"), its mask-first precedence and its default SHALL CONTINUE TO behave as today.

3.4 WHEN any client calls the run API THEN `/results`, `/output-image`, `/overlay-image`,
`/metadata`, `/node-status` and `/workflows/node-catalog` SHALL CONTINUE TO return the same shapes,
fields and content types. Only the overlay JPEG's pixels change.

3.5 WHEN a model is not a detector THEN its outputs SHALL CONTINUE TO be byte-identical to today's:
the anomaly mask overlay (`_generate_overlay`), the mask, the base model's anomaly tensors, the
capture record and the run metadata. Its model card SHALL CONTINUE TO show the verdict fields.

3.6 WHEN a `model_inference` node's card is shown THEN it SHALL CONTINUE TO list the run's
detections and show the overlay thumbnail exactly as today; only the overlay image's drawing style
changes.

3.7 WHEN any other node's card is shown THEN every other output node type (`llm_inference`,
`bedrock_inference`, `mqtt_publish`, `opcua_write`, `digital_output`) and every non-output node
SHALL CONTINUE TO render as today. The capture card SHALL CONTINUE TO show the in-progress
placeholder for a non-terminal node, the failure alert for a failed node, "No preview is available
for this node." for a run without image results, and the "View full results" link in every state.

3.8 WHEN the image's short side is under 600 px THEN the box thickness, font scale, font thickness
and padding SHALL CONTINUE TO be today's (2, 0.5, 1 and 3 px). A single detection whose palette
entry is 0, such as one with `class_id` 0 or an integer class 0, SHALL CONTINUE TO produce a
byte-identical overlay on such an image. That covers every one-class YOLO or RF-DETR detector,
named or not.

3.9 WHEN the catalog is read THEN every `catalog_baseline.json` entry other than `capture` SHALL
stay byte-identical, and the existing catalog preservation tests (`test_bug_catalog_preservation.py`,
`test_catalog_stream_and_analytics.py`, `test_catalog_modbus_write.py`) SHALL CONTINUE TO pass. The
`capture` entry changes only in its `output_path` description: that parameter's name, type,
required flag, default, constraints and examples, the capture node's other parameters and its
mappings stay as they are. The executor's handling of `output_path` SHALL CONTINUE TO be today's:
it is not used.

3.10 WHEN the vendored catalog mirror is checked THEN `nodes.py` SHALL CONTINUE TO be
byte-identical between `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/`
and `src/backend/workflow_engine/vendor/workflow_core/catalog/`.

3.11 WHEN an `mqtt_publish` binding renders its payload THEN the descriptor and the rendering SHALL
CONTINUE TO be today's, for both phases.

3.12 WHEN the base model serializes a detection THEN:
- every existing key of the payload entry (`bounding_box`, `class`, `confidence`,
  `confidence_threshold`, `class_label`) SHALL CONTINUE TO carry today's value, in today's order;
- an entry without a class id, the zero-object sentinel and every other output tensor SHALL
  CONTINUE TO be byte-identical to today's;
- `ObjectDetectionResult.serialize()` and `deserialize()` SHALL CONTINUE TO produce and accept
  today's four keys;
- `class_id` SHALL NOT reach the capture record, the detections sidecar, the run metadata or any
  API response.

## Out of Scope and Follow-ups

- **Completion-phase run identifiers (item d).** This needs a follow-up spec, for example
  `completion-phase-run-identifiers`:
  - make `{capture_id}`, `{execution_id}` and `{workflow_id}` resolve in completion-phase
    `payload_template` and `value_template` placeholders;
  - keep them out of `{inference_json}` and the persisted run metadata, so existing payloads stay
    byte-identical;
  - document the placeholders in those descriptions.

  A template that contains these placeholders today, which publishes them literally, would start
  publishing real values; the follow-up must call that out. A dashboard that links a result message
  to the run's images needs this change.
- **Label collision avoidance.** Nested boxes, such as a helmet inside a person, can still overlap
  their labels; the later label covers the earlier one, as today.
- **Semantic or configurable class colors**, such as a violation class in red, and a color legend in
  the card. The palette is keyed on the class id and knows nothing about what a class means.
- **A detection count or object list on the capture card.** The model card already lists the
  detections.
- **Honoring `output_path`.** It is rejected (design.md D6), not deferred.
- **Re-staging the Portal test sandbox's copies** (`edge-cv-portal/test-sandbox/dda_triton_resources/`)
  of the five files this fix changes on the device side: the marshal, the base model template,
  `object_detection_result.py` and the YOLO and RF-DETR post-processors. They are staged by hand
  when the sandbox image is rebuilt, and three of the five (the marshal, the base model template and
  the YOLO post-processor) already differ from the source.
- **Class ids from other detection producers.** A future detector post-processor with class names
  should pass `class_id` the same way. Until it does, its classes get hashed colors, which can
  collide.
- **Run-artifact growth on the device and its cleanup.** These are separate work.
