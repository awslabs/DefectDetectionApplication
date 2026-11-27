# Car assembly check: sample workflows

A workflow that checks whether the three pieces of a toy car (`front_fender`, `rear_fender`, `roof`) sit in their pockets. An RF-DETR-Seg model finds each piece's mask; the Layout Check node (`layout_check`) scores each mask against the piece's Expected_Region and gives a verdict per piece (`ok`, `misplaced`, `missing`, `duplicate`) and for the whole frame (`pass`).

| File | What it is |
|---|---|
| `workflow.json` | The production form for this spec: Static_Image_Camera → Grayscale_Node → model → one capture, with the Layout Check and the pass/fail MQTT topics. |
| `workflow-batch.json` | The same workflow with a `folder_source`, for the batch runs of `batch_run.py`. |
| `workflow-color.json` | The Colour_Variant: the same workflow without the Grayscale_Node and with the colour model. |
| `grayscale_handler.py` | The Grayscale_Node's `code`, pasted as is (never retyped). |
| `batch_run.py` | The device batch driver (runs on the device host, Python 3.8+ standard library only). |

The three JSON files are `definition` bodies. The Portal has no JSON import: create the workflow with `POST /workflows` and the body `{"usecase_id": "<use case>", "name": "<name>", "definition": <file contents>, "description": "<optional>"}`, sent by a user who holds `WORKFLOW_CREATE`, or build it by hand in the Workflow Builder from the node tables below.

## Placeholders to fill

| Node | Parameter | Placeholder | Where the value comes from |
|---|---|---|---|
| `layout` | `expected_regions` | `<REPLACE: expected_regions JSON from layout_reference.py (6.2)>` | `datasets/layout_reference.py --manifest … --source-ref <golden sample> --triage placement-triage.csv --out expected_regions.json`: paste the file's JSON text. |
| `layout` | `min_iou` | `<REPLACE: calibrated min_iou (6.2)>` | `datasets/layout_reference.py --calibrate …`: the printed `recommended_min_iou`, later the predicted-mask value of `--calibrate --results`. |

`source.camera_id` is `static-image-camera`, the Static_Image_Camera of LocalServer, bound at deploy time and pinned through `POST /static-image-camera/pin` to one colour original of the Known_Good_Set (never a grayscale JPEG). `modelName` names the packaged model components `car-assembly-seg-gray` and `car-assembly-seg-color`; change them if your components are named differently.

## Node tables

### `workflow.json`

| id | type | parameters | connections |
|---|---|---|---|
| `source` | `aravis_camera_source` | `camera_id = static-image-camera` | `source.out → gray.in` |
| `gray` | `custom_python_preprocess` | `code` = the text of `grayscale_handler.py`, `requirements` empty, `wall_clock_limit_s = 10` | `gray.out → model.in` |
| `model` | `model_inference` | `modelName = car-assembly-seg-gray`, `detection_sort_order = left_to_right` | `model.out → capture.in`, `model.out → layout.in` |
| `capture` | `capture` | `output_path = /aws_dda/captures`, `interval = 0`, `quality = 100` | sink |
| `layout` | `layout_check` | `expected_regions` = placeholder, `min_iou` = placeholder, `min_confidence = 0.5`, `min_outline_overlap = 0.1` | `layout.out → verdict.in` |
| `verdict` | `conditional` | `condition = layout.layout.pass == true` | `verdict.true → accept.in`, `verdict.false → reject.in` |
| `accept` | `mqtt_publish` | `topic = dda/car-assembly/pass`, `greengrass = true`, `payload_format = json`, `payload_template` = T | sink |
| `reject` | `mqtt_publish` | `topic = dda/car-assembly/fail`, `greengrass = true`, `payload_format = json`, `payload_template` = T | sink |

Template T, identical on both MQTT nodes:

```json
{"pass": "{layout.layout.pass}", "front_fender": "{layout.layout.pieces.front_fender.status}", "rear_fender": "{layout.layout.pieces.rear_fender.status}", "roof": "{layout.layout.pieces.roof.status}", "flagged": "{layout.layout.flagged}", "sent_at": "{now_iso}"}
```

Every placeholder is a quoted JSON string. A string that is exactly one placeholder keeps the value's own type, so `pass` arrives as a boolean and `flagged` as a list, for example `{"pass": false, "front_fender": "ok", "rear_fender": "misplaced", "roof": "missing", "flagged": ["b07d44aa"], "sent_at": "2026-11-24T10:15:30.123Z"}`.

### `workflow-batch.json`

Identical to `workflow.json` except `source`:

| id | type | parameters | connections |
|---|---|---|---|
| `source` | `folder_source` | `location = /aws_dda/batch-input/`, `file_pattern = *.jpg` | `source.out → gray.in` |

`batch_run.py` copies one image at a time into `/aws_dda/batch-input/` and triggers one run per image.

### `workflow-color.json`

Identical to `workflow.json` except: no `gray` node, `source.out → model.in`, and `model.modelName = car-assembly-seg-color`. Its batch version is the same `folder_source` substitution as `workflow-batch.json`.

## Why the workflow is shaped this way

- **Exactly one `capture`, unconditional, on the frame path.** Each run records one image (`{capture_id}.jpg`) and its run metadata, whatever the verdict. A second capture would record the same frame twice; a conditional capture would leave runs without a record.
- **The verdict reaches only gated executor bindings.** The compiled document is one GStreamer segment (`source → gray → model → capture`) plus four executor bindings (`layout`, `verdict`, `accept`, `reject`). The `conditional` gates `accept` and `reject`, so exactly one of the two topics receives a message for a run with a verdict. Neither does when `layout.layout.pass` is absent (an unevaluable condition closes both ports), which fails closed on a LocalServer without the Layout Check.
- **The outline rule (`min_outline_overlap = 0.1`, written explicitly).** A detected piece with less than 10 % of its mask inside the car outline (the union of the three Expected_Regions) is ignored: it is listed under `layout.layout.ignored`, never counted and never flagged. A spare piece lying beside the tray therefore does not fail a correct build. A piece in a wrong pocket, or one pushed partly out of its pocket, is still counted and judged by its score, and an extra piece lying over the car still makes its class `duplicate`. `0` counts every detection (the behaviour before this rule); the upper bound is `0.5`.
- **No run id in the payload.** The MQTT nodes publish in the completion phase, whose payloads carry no `{execution_id}` or `{capture_id}` (those placeholders exist only in the capture phase). To correlate a verdict with its image, add a third `mqtt_publish` with `phase = capture` that publishes `{capture_id}` on its own topic.
- **Production alternatives for the fail branch.** A `digital_output` node (`signal_type = pulse`, `condition = layout.layout.pass == false`) drives a reject signal; an `s3_upload` node on `verdict.false` uploads only the failed runs.

## The Grayscale_Node

The Grayscale_Model was trained on grayscale images (Pillow mode `L`, ITU-R 601-2 weights), so every workflow that feeds it carries the Grayscale_Node immediately upstream of `model_inference`, downstream of any `rotate`. The node is a `custom_python_preprocess` whose `code` is `grayscale_handler.py` pasted unchanged, `requirements` empty and `wall_clock_limit_s` 10. The bridge delivers `RGB` frames, so only the handler's `RGB` branch runs on a device. The Colour_Variant's workflow is the same definition without the node; that is the only difference between the two variants on the device.

## Production camera (not deployed in this spec)

The Verification_Device `mic-730ai` has no camera for this use case (owner decision O16 (a)); the three definitions use the Static_Image_Camera or a folder. For a later production setup:

1. Replace `source` with the camera node: `aravis_camera_source` for the Basler Capture_Camera or another GenICam camera, `icam_source` for a V4L2 camera.
2. When the camera is mounted like the Capture_Camera on thor2, whose frames are upside down relative to the training images, insert a `rotate` node with `method = rotate-180` between the camera and `gray` (`source.out → rotate.in`, `rotate.out → gray.in`; in the colour form `rotate.out → model.in`). The `capture` then records the rotated frame, so the masks and the Reference_Layout share one coordinate system. The three checked-in definitions carry no `rotate` node: a pinned `assembly-photos/` original is already in the training orientation, and a `rotate-180` there would be the error.
3. **Symptom of a wrong orientation:** every piece `misplaced` with similar low scores. A moved camera or fixture shows the same signature; check the orientation first.
4. **Fitness check before any verdict from the camera is recorded:** place one known-good physical assembly and make one run. The run preview's `instances` must show exactly one instance of each piece class, each with the class matching the piece's shape. If a piece is missing or a class is wrong, the camera's optics are outside what the model was trained on; stop and decide with the owner (new captures, a further labeling round and retraining, or another camera).
5. **Keep or recapture the Reference_Layout:** if that run passes with every IoU within the Known_Good predicted-mask range, the manifest-derived reference stands. Otherwise recapture it from that run (`datasets/layout_reference.py --overlay <saved overlay body> --metadata <saved metadata body> --out expected_regions.json`) and deploy a new workflow version with it.
6. **Capture-only form.** None is checked in. When a production camera is mounted, write one for the verification captures: the camera node → `rotate` as the mounting requires → one `capture`, with no model and no executor bindings. Each trigger then records one rotated colour frame as `{capture_id}.jpg`. The grayscale production form cannot serve this purpose: its `capture` sits downstream of `gray` and records an already converted frame.

## When to recapture the reference

The Reference_Layout assumes the camera framing of the images it was captured from. When the camera or the fixture moves, recapture it (`layout_reference.py` capture, then `--calibrate`) and deploy a new workflow version. A whole-frame downscale at the same field of view needs no new reference, but it does need a new predicted-mask calibration (a batch run and `--calibrate --results`) at the new resolution before any result is recorded.

## Switching variants (checklist)

| | Grayscale form (`workflow.json`) | Colour form (`workflow-color.json`) |
|---|---|---|
| Grayscale_Node | present, immediately upstream of `model` | absent |
| Model | `car-assembly-seg-gray` | `car-assembly-seg-color` |
| Training/inference parity | the handler's conversion matches the training conversion | no conversion |
| Camera (later production setup) | colour or mono | must deliver colour frames |
| `min_iou` | the grayscale variant's own calibrated value | the colour variant's own calibrated value (its batch run doubles as its predicted-mask calibration) |
| Reference_Layout | unchanged | unchanged (independent of the variant; not recaptured for a switch) |
| `rotate` | only for a camera mounted like thor2's | the same rule |

## Batch runs on the device

`batch_run.py` runs on the device host, where `/aws_dda` is the folder the LocalServer container reads (there is no remote mode). Copy the colour originals to the host and run, for example:

```bash
python3 batch_run.py --registration-id <id> --workflow-id <id> \
  --folder /aws_dda/batch-input --images ./originals \
  --triage placement-triage.csv --expect-local-server-version <version> --out gray.json
```

The script reads `GET /system-health` before the first run and after the last, refuses to start when `--expect-local-server-version` does not match, copies one image at a time into the empty `--folder`, triggers one run per image and writes the Result_Export that `datasets/layout_reference.py --calibrate --results` and `datasets/compare_variants.py` read. `--token env:VAR` reads a bearer token from the environment variable `VAR`; never pass a token on the command line.
