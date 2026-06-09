# Pluggable Inference Runtimes (DLR + ONNX + PyTorch)

Status: Proposal / design (no implementation yet)
Branch: `byo-model-onnx`
Owners: DDA edge team

## 1. Goal

Allow a DDA model package to declare which inference engine it uses, so users
can migrate **per-model** off SageMaker Neo / DLR and onto ONNX Runtime (and,
optionally, native PyTorch) without a fleet-wide cutover.

Requirements:

- **DLR remains the default** — every existing model package keeps working with
  zero changes.
- A **config identifier in the model package** selects the runtime.
- **ONNX Runtime** support added in parallel with DLR.
- **Direct PyTorch** runtime support (TorchScript / `nn.Module`).
- **No Triton rebuild** — Triton stays compiled with the Python backend only;
  the engine swap happens *inside* the Python model (`model.py`). This keeps the
  change isolated to the backend image and avoids the long edgemlsdk/Triton build.

## 2. Where DLR/Neo lives today

The serving path is Python-backend Triton + DLR:

- Triton is built `--backend python` only (`src/edgemlsdk/Dockerfile*`); there is
  no native `onnxruntime`/`tensorrt` Triton backend.
- `dda_triton/resources_for_copy/lfv_model_template.py` is the `base_<model>`
  Triton Python model. Its `_InferenceRunner.__load_model` does
  `import dlr; dlr.DLRModel(model_path, device_type, device_id)` and
  `dlr_device_type()` loads the model-bundled `libdlr.so`.
- `dda_triton/model_convertor.py` builds the Triton model repository: copies
  `lfv_model_template.py` as `base_<model>/<v>/model.py`, symlinks the Neo
  artifact (`compiled.so` / `compiled.params` / `compiled.meta` / `libdlr.so`),
  and generates `config.pbtxt` for the `base` (python), `marshal` (python), and
  `ensemble` models.
- `edge-cv-portal/backend/functions/compilation.py` submits the SageMaker Neo
  job that produces the DLR artifact.
- Post-processing (`lyra_science_processing_utils.ModelGraphFactory`, the
  `marshal` model, anomaly mask utils) consumes the runner's raw output tensors.

Key insight: **DLR is just the inference engine behind a Python Triton model.**
Everything around it is engine-agnostic, so swapping the engine is a localized
change as long as the runner's input/output contract is preserved.

## 3. The runner contract (must be preserved)

`_InferenceRunner` today:

- Constructed from a model directory (the stage subdir, e.g. `base_<model>/<v>/`).
- Callable: `runner(input_np: np.ndarray) -> list[np.ndarray]`.

Any new runtime MUST honor the same contract: same number, order, shape, and
dtype of output tensors that `ModelGraphFactory` / the marshal stage expect.
If a given ONNX/PyTorch graph emits different raw outputs, the runner is
responsible for adapting them back to the DLR contract (see §7).

## 4. Manifest config identifier

Add optional fields to the model package `manifest.json` (read by
`model_convertor.py` at packaging time and `lfv_model_template.py` at load time):

```json
{
  "runtime": "dlr",
  "runtime_artifact": "compiled.so",
  "device": "gpu"
}
```

- `runtime`: `"dlr"` (default when absent) | `"onnx"` | `"pytorch"`.
- `runtime_artifact`: filename of the engine artifact within the stage dir
  (`compiled.so` for DLR via libpath, `model.onnx`, or `model.pt`). Optional;
  each runner has a sensible default.
- `device`: optional override (`"gpu"` | `"cpu"`); default is auto-detect.

Absent `runtime` ⇒ `dlr` ⇒ **full backward compatibility**.

Migration story: a user repackages a single model as ONNX, sets
`runtime: "onnx"`, and deploys it on the same device next to DLR models. No
device-wide change required.

## 5. Runtime abstraction

New file `dda_triton/resources_for_copy/inference_runtimes.py` (copied to the
device next to `model.py` by `model_convertor.py`):

```python
class BaseInferenceRunner(ABC):
    def __init__(self, model_dir: str, device_type: str, device_id: int = 0): ...
    @abstractmethod
    def __call__(self, input_np: np.ndarray) -> list[np.ndarray]: ...

class DlrRunner(BaseInferenceRunner):     # exact current DLR logic, moved verbatim
class OnnxRunner(BaseInferenceRunner):    # onnxruntime.InferenceSession
class TorchRunner(BaseInferenceRunner):   # torch.jit.load / torch.load + eval

def make_runner(runtime: str, model_dir, device_type, device_id) -> BaseInferenceRunner:
    ...  # enum dispatch; unknown -> clear ValueError
```

`lfv_model_template.py._InferenceRunner.__load_model` becomes a thin wrapper that
reads `runtime` from the manifest and calls `make_runner(...)`. The existing DLR
code (`load_lib`, `dlr_device_type`, `DLRModel`) moves into `DlrRunner` unchanged.

Each engine is **lazily imported inside its runner**, so a DLR-only device never
imports `onnxruntime`/`torch`, and a missing optional dependency only fails
models that actually request that runtime.

### 5.1 DlrRunner
Unchanged behavior. Keeps bundled-`libdlr.so` loading, `dlr_device_type`,
`DLRModel(model_path, device_type, device_id)`, `.run(inp)`.

### 5.2 OnnxRunner
`onnxruntime.InferenceSession(model.onnx, providers=[...])`.
- Provider order: `TensorrtExecutionProvider` -> `CUDAExecutionProvider` ->
  `CPUExecutionProvider`, falling back gracefully.
- Map the single positional input DLR used into a named feed
  (`sess.get_inputs()[0].name`); handle NCHW vs NHWC and input dtype from the
  graph. Return `sess.run(None, feed)` (already a `list[np.ndarray]`).
- Side benefit: no `libdlr.so` ⇒ avoids the libjpeg/cudart-version collision
  class of bugs entirely.

### 5.3 TorchRunner
`torch.jit.load(model.pt)` (TorchScript preferred) or `torch.load` for a pickled
`nn.Module`.
- `model.eval()` + `torch.no_grad()`, move to CUDA if available; convert input
  ndarray -> tensor with correct layout; return `[t.cpu().numpy() for t in out]`.
- Requires the NVIDIA Jetson PyTorch wheel matching the JetPack — heaviest
  dependency; gate behind a build arg so DLR/ONNX-only images stay smaller.

## 6. Packaging changes (`model_convertor.py`)

- Read `manifest["runtime"]` (default `dlr`).
- Always copy `lfv_model_template.py` **and** `inference_runtimes.py` into the
  base model dir.
- DLR: symlink the Neo artifact as today (unchanged).
- ONNX: place `model.onnx` into `base_<model>/<v>/`.
- PyTorch: place `model.pt`.
- `config.pbtxt` stays `backend: "python"` for all three. `marshal` and
  `ensemble` configs are unchanged. This is what allows the runtimes to coexist
  with **no Triton rebuild**.

## 7. Output-contract adaptation (highest risk)

DLR models emit a specific output set the marshal stage relies on. ONNX/PyTorch
graphs may emit raw logits / different tensor names/orders. Each non-DLR runner
must normalize its raw outputs to the DLR contract, OR we add a small per-model
output-adapter config in the manifest (e.g. index/name mapping + any
softmax/argmax the DLR graph used to bake in). Pin this down with one real ONNX
sample before implementing.

## 8. Dependencies

- `onnxruntime` — installed in the backend container build (Dockerfile.jp5 /
  Dockerfile.jp6), NOT in setup_station.sh: the OnnxRunner runs in-container
  under python3.9, while the station scripts only provision the host python that
  runs the packaging-only model_convertor.py. Two modes via build-args:
  - **CPU (default):** prebuilt wheel `onnxruntime==1.16.3` (last release with a
    cp39 aarch64 manylinux_2_17 wheel; works on JP5 glibc 2.31 and JP6 2.35).
  - **GPU (`ONNXRUNTIME_GPU=1`, default on for JP5/JP6 in build-custom.sh):**
    `onnxruntime-gpu` built from source with the CUDA + TensorRT execution
    providers by `edge_ml1_p_camera_management/install_onnxruntime_gpu.sh`,
    against the l4t-jetpack base image's CUDA/TRT. JP5 → ORT v1.16.3, CUDA archs
    `72;87`; JP6 → ORT v1.17.1, arch `87`. This is needed because NVIDIA's
    prebuilt Jetson GPU wheels target each JetPack's *native* python (3.8 on
    r35, 3.10 on r36), not the 3.9 the container uses, and PyPI's
    onnxruntime-gpu is x86_64-only.
  - **JetPack 4: CPU only.** Its native python is 3.6 (EOL) and there is no
    compatible cp39 GPU build path; the portal compile UI notes this.
  OnnxRunner auto-selects TensorRT → CUDA → CPU providers, so the same code path
  works for either wheel.
- `torch` — Jetson wheel, **only if PyTorch runtime ships in v1**; gate behind a
  Docker build arg.
- `dlr==1.10.0` stays as-is (pure-python `py3-none-any` wrapper over the
  model-bundled libdlr.so; version-agnostic).
- Lazy imports keep optional engines out of images/devices that don't use them.

## 9. Portal / packaging side (phased)

- Phase 1: hand-supplied `model.onnx` / `model.pt` via the model import path;
  `greengrass_publish.py` / `model_import.py` write `runtime` into the manifest.
- Phase 2: runtime dropdown in the import UI (DLR/Neo | ONNX | PyTorch);
  `compilation.py` gains an ONNX-export path (replacing Neo for those models).

## 10. Validation

- Per runtime: assert output tensor count/order/shape/dtype match the DLR
  contract that `ModelGraphFactory` / marshal expect.
- Numerical parity: same image through DLR vs ONNX vs PyTorch -> same anomaly
  label/score within tolerance.
- Device tests on **both** JP5 and JP6 (different ORT/torch builds).

## 11. Effort / risk

| Workstream | Effort | Risk | Triton rebuild |
|---|---|---|---|
| Runtime abstraction + DlrRunner refactor | S | Low | No |
| OnnxRunner + onnxruntime-gpu dep | M | Med | No |
| TorchRunner + torch dep | M | Med-High | No |
| manifest `runtime` + convertor packaging | S | Low | No |
| Portal import / runtime selection | M | Low | No |
| Parity validation (JP5 + JP6) | M | Med | No |

## 12. Open questions

1. Do ONNX/PyTorch models emit the same output contract the marshal stage needs,
   or is a per-runtime output adapter required? (Need one sample ONNX model.)
2. Is PyTorch in scope for v1, or staged after ONNX (torch Jetson-wheel weight)?
3. Where do ONNX/PyTorch artifacts come from — hand-supplied via import, or an
   export step in the portal?

## 13. Files expected to change (implementation phase)

- `src/backend/dda_triton/resources_for_copy/inference_runtimes.py` (new)
- `src/backend/dda_triton/resources_for_copy/lfv_model_template.py`
  (delegate `_InferenceRunner` to `make_runner`)
- `src/backend/dda_triton/model_convertor.py` (runtime-aware packaging)
- `src/backend/requirements.txt` and `src/backend/Dockerfile*`
  (conditional `onnxruntime-gpu` / `torch`)
- `edge-cv-portal/backend/functions/model_import.py`,
  `greengrass_publish.py` (manifest `runtime`)
- `edge-cv-portal/backend/functions/compilation.py` (Phase 2: ONNX export)
- Portal frontend import pages (Phase 2: runtime dropdown)
- Tests under `test/backend-test/dda_triton/`

---

# Object-detection task type (YOLO / bounding boxes)

Status: Design + initial implementation (single-stage ONNX YOLO decode).
This realizes the §7 "output-contract adapter" for the concrete case of a
bring-your-own ONNX object-detection model (e.g. YOLOv8) whose raw output is a
detection tensor, not the anomaly schema the existing pipeline assumes.

## 14. Why a new task type

The whole on-device serving path is currently hardwired to **anomaly
classification**:

- `model_convertor.py` emits a fixed Triton output contract in every
  `config.pbtxt`: `output` (is_anomalous, uint8), `output_confidence`,
  `output_score`, `mask` (= input shape), `anomalies`. The ensemble
  `input_map`/`output_map` wiring between the `base`, `marshal`, and `ensemble`
  models is fixed to those names.
- `lfv_model_template.py.execute()` reads only
  `inference_output.objects[0].anomaly` and packs the anomaly tensors. It never
  reads `.bboxes`.
- The marshal/overlay stage renders an anomaly mask/overlay.

Object detection is a different **output shape and decode**, so it needs an
explicit selector rather than overloading the anomaly path. The manifest already
grew a `runtime` field (dlr|onnx|pytorch) that says *how* to run the engine; we
add an orthogonal **`task`** field that says *what the output means and how to
decode/emit it*:

```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "task": "object_detection",
  "detection": {
    "layout": "yolo",          // decoder family
    "num_classes": 80,
    "score_threshold": 0.25,
    "iou_threshold": 0.45,
    "class_names": ["person", "bicycle", ...]   // optional, for labels
  }
}
```

Absent `task` ⇒ `anomaly` ⇒ **full backward compatibility** with every existing
model package.

## 15. What already exists (reused, not rebuilt)

The result-level schema for boxes is already present and serialized end to end:

- `ObjectDetectionResult` = `[x_min,y_min,x_max,y_max]` + `obj_class` +
  `confidence` + `threshold`, JSON-serializable.
- `AnomalyResult.bboxes: List[ObjectDetectionResult]` is serialized into the
  inference result JSON and round-trips via `deserialize`.
- `SingleStageModelGraph` already accepts a post-processor that returns a
  `list[ObjectDetectionResult]` and wraps them into
  `InferenceData`/`SingleObjectInferenceData`.

So the detection *result* contract is solved. The work is the decode (raw YOLO
tensor → `ObjectDetectionResult`s) and emitting boxes through Triton/GStreamer.

## 16. What changes

1. **YOLO decode post-processor (new):** `yolo_detection_postprocessor.py`.
   Input: raw ONNX output (YOLOv8 `[1, 84, 8400]` = 4 box coords + num_classes
   scores per anchor; also handles the transposed `[1, 8400, 84]` layout).
   Steps: transpose to per-anchor rows, split boxes/class-scores, take max class
   score as confidence, filter by `score_threshold`, class-wise NMS by
   `iou_threshold`, convert xywh(center)→xyxy, scale from the network input size
   back to the source image. Output: `list[ObjectDetectionResult]`. Pure
   numpy — no torch dependency on the hot path. Slots straight into
   `SingleStageModelGraph` (which already handles a list result).

2. **Triton output contract — `model_convertor.py`:** branch on `task`. For
   `object_detection`, emit a detection `config.pbtxt` whose `base` model
   outputs a variable-length `detections` tensor (serialized JSON bytes,
   `TYPE_UINT8 dims:[-1]`, mirroring how `anomalies` is already carried) instead
   of the anomaly mask/score tensors, and wire the ensemble/marshal maps to it.
   `backend: "python"` stays — **no Triton rebuild**.

3. **`execute()` in `lfv_model_template.py`:** branch on `task`. For detection,
   pull `ObjectDetectionResult`s out of the graph result and pack them into the
   `detections` JSON tensor; skip the anomaly-specific mask/score packing.

4. **GStreamer pipeline:** the streaming graph (decode → caps → `emltriton`) is
   unchanged — it is agnostic to what the model computes. The only
   detection-aware rendering is the **marshal/overlay** stage if boxes are to be
   drawn on the output image (draw rectangles + labels from the detections
   tensor). Core flow untouched.

5. **Frontend / workflow type:** surface detection models so the UI can render
   boxes and the results viewer interprets the `bboxes` field (already present
   in the result JSON). `FeatureConfigurationType` (currently
   `LFVModel | TritonModel`) and the workflow model type gain an
   object-detection task flavor.

## 17. Implementation phasing

- **Phase A (in progress):** the YOLO decode post-processor as a standalone,
  unit-tested numpy module validated against the real YOLOv8n ONNX output
  (`[1,84,8400]`). No device-contract changes yet — lowest risk, immediately
  testable off-device.
- **Phase B:** `task`-aware `model_convertor.py` detection config + `execute()`
  emit path; manifest `task` plumbing.
- **Phase C:** marshal overlay rendering of boxes; frontend detection type +
  results rendering.

## 18. Files expected to change (object-detection)

- `src/backend/lyra_science_processing_utils/model_processors/yolo_detection_postprocessor.py` (new)
- `src/backend/dda_triton/model_convertor.py` (task-aware detection config)
- `src/backend/dda_triton/resources_for_copy/lfv_model_template.py` (task-aware execute)
- `src/backend/dda_triton/constants.py` (task manifest keys)
- `src/frontend/src/components/workflow/types.ts` and model views (detection type)
- `test/backend-test/...` (YOLO decode unit tests)

## 19. Phase B — task-aware serving (implemented)

The on-device serving path now honors the `task` field end to end **without a
Triton rebuild and without changing the Triton output contract**:

- `lfv_model_template.py` reads `task` from the manifest in `initialize()` and
  branches `execute()`:
  - `anomaly` (default): unchanged anomaly tensor emit.
  - `object_detection`: collects `ObjectDetectionResult`s from the graph result
    and emits them as a serialized JSON list through the **existing
    variable-length `anomalies` tensor**; `output` = 1 if any detection,
    `output_score`/`output_confidence` = top detection confidence, `mask` empty.
  Reusing the `anomalies` channel means the `base`/`marshal`/`ensemble`
  `config.pbtxt` and their input/output maps are untouched.
- `model_convertor.py` reads `manifest["dataset"]` defensively (`.get`) so a BYO
  detection package without an anomaly-style `dataset` block packages cleanly.
- No new graph or preprocessor: a detection model uses
  `single_stage_model_graph` with stage type `yolo_object_detection`.
  `SingleStageModelGraph` already passes the raw runner output to the
  post-processor and wraps a returned `list[ObjectDetectionResult]` into
  `InferenceData`. `BasicPreProcessor` already emits `(1,3,H,W)` float32, which
  is the YOLO ONNX input — set `image_range_scale: true`, `normalize: false`.

### Detection model package — manifest shape

```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "task": "object_detection",
  "detection": {
    "num_classes": 80,
    "score_threshold": 0.25,
    "iou_threshold": 0.45,
    "network_input": 640,
    "class_names": ["person", "bicycle", ...]
  },
  "model_graph": {
    "model_graph_type": "single_stage_model_graph",
    "stages": [{
      "stage_type": "yolo_object_detection",
      "threshold": 0.25,
      "image_width": 640,
      "image_height": 640,
      "image_range_scale": true,
      "normalize": false,
      "input_shape": [1, 3, 640, 640]
    }]
  }
}
```

### Phase C (implemented)

- **Source-image box scaling:** `SingleStageModelGraph.predict()` now passes the
  source image size `(width, height)` to the post-processor as `src_img_size`
  (harmless to anomaly post-processors — they accept `**kwargs`). The YOLO
  post-processor uses it to scale network-space boxes (0..network_input) back to
  source-image pixels.
- **Marshal overlay box rendering:** `marshal_for_capture_template.py` detects
  the (reused `anomalies`) payload as a detection list and draws the boxes +
  `class conf` labels onto the input image to produce the `overlay` JPEG, and
  emits a `detections` block in the capture metadata
  (`deviceFleetAuxiliaryOutputs`). Because boxes are rendered server-side into
  the overlay image, the existing frontend image display shows them with no
  change.
- **Frontend type:** `InferenceResultHistory` gains an optional `detections`
  field (`DetectionResult[]`) carrying the structured list for textual display;
  the results API mapping to populate it from the metadata block is a follow-up.

## 20. Portal packaging for ONNX / detection models

The portal can compile, package, and publish an ONNX model end to end:

1. **Compile to ONNX** (`compilation.py`, `target=onnx`): runs a SageMaker
   training job that `torch.onnx.export`s a trained TorchScript model to
   `model.onnx` (Neo cannot emit ONNX). Validated end-to-end earlier.
2. **Package** (`model_converter.py`, Smart Import): `generate_dda_package` now
   takes `export_format` ('pytorch' default | 'onnx') and, for ONNX, writes a
   **device-correct manifest** for the pluggable engine:
   `runtime: "onnx"`, `runtime_artifact: "model.onnx"`,
   `model_graph.model_graph_type: "single_stage_model_graph"`, and for
   `model_type=object_detection` the stage `type: "yolo_object_detection"` with
   `image_range_scale: true`, `normalize: false`, `threshold`, plus a top-level
   `task: "object_detection"` and a `detection` block (num_classes,
   score/iou thresholds, network_input, class_names). Bundles `model.onnx`
   (not a `.pt`).
3. **Publish** (`greengrass_publish.py`): unchanged — publishes the component.

Frontend: Smart Import (`SmartImport.tsx`) gains a "Runtime / export format"
selector (PyTorch/Neo vs ONNX Runtime); `api.ts convertModel` carries
`export_format` / `score_threshold` / `iou_threshold`.

Validated off-device: the generated ONNX-detection manifest matches exactly what
the device serving code (Phases B/C) reads. The portal frontend type-checks.

Caveat: "compile to ONNX" and "package" are currently two Smart-Import steps
(export produces model.onnx in S3; package converts it). Auto-chaining the ONNX
export output directly into packaging is a follow-up. On-device validation
pending.
