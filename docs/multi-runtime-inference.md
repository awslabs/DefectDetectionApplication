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

### 5.4 ONNX execution-provider visibility (Active_Provider_Record)

(Spec: `model-gpu-fallback-visibility`.) ONNX Runtime's CUDA→CPU fallback is
by design and used to be **silent**: a model whose CUDA EP failed to
initialize served READY on CPU with no DDA-visible signal (the jetson-thor1
Aug 14–15 device-wide outage). The provider-selection **contract in §5.2 is
unchanged** — fallback still serves — but every `OnnxRunner` load is now
observable:

- **Active_Provider_Record.** After creating the session, the runner calls
  `session.get_providers()` and writes `dda_active_providers.json` into the
  **model version dir** (`base_<model>/<v>/`, the parent of the stage dir).
  The write is **atomic** (temp file + `os.replace` in the same dir — readers
  never see a torn file) and failure-isolated (a bookkeeping failure logs a
  warning and never fails the load). Shape: `modelId`, `runtime`, a
  **per-stage** map (`requestedProviders`, `activeProviders`, `gpuRequested`,
  `gpuActive` per stage), a **model-level aggregate** (`gpuRequested` = any
  stage requested a GPU provider; `gpuActive` = every GPU-requesting stage
  obtained one — a single fallen-back stage degrades the model; TRT-requested
  with CUDA-active counts as active), and `updatedAt`. Each load rewrites the
  record for the currently loaded instance.

  ```json
  {
    "modelId": "yolo_test",
    "runtime": "onnx",
    "stages": {
      "stage_model": {
        "requestedProviders": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "activeProviders": ["CPUExecutionProvider"],
        "gpuRequested": true,
        "gpuActive": false
      }
    },
    "gpuRequested": true,
    "gpuActive": false,
    "updatedAt": "2026-08-15T20:23:13Z"
  }
  ```

- **Logs.** The stub logs the ACTIVE providers at INFO on **every** load, and
  a prominent WARNING on GPU fallback (requested chain + active providers +
  "DEGRADED on CPU until the model is reloaded") only when a GPU provider was
  requested and none is active. CPU-by-design models (`device: "cpu"`) never
  warn and never count as degraded.

- **Status surface.** The backend (`dda_triton/provider_visibility.py`) reads
  the record (highest integer version dir) and merges it additively into each
  Triton entry of `GET /feature-configurations` as
  `defaultConfiguration.executionProviderInfo` (`requestedProviders`,
  `activeProviders`, `gpuRequested`, `gpuActive`, derived `gpuFallback`,
  `updatedAt`).

- **Device-level signal.** `GET /feature-configurations/gpu-status` aggregates
  the records: `gpuDegraded` is true iff at least one recorded GPU-chain model
  is loaded and **none** of them holds an active GPU provider, plus
  `gpuChainModels` / `gpuActiveModels` counts and a per-model map. Transitions
  log (WARNING on entering degraded, INFO on recovery).

- **Cloud mirror.** The same snapshot is reported (debounced, reported-state
  only, failure-isolated) into the `dda-model-status` **named shadow**
  (`utils/model_status_shadow.py`), which the portal reads to render per-model
  GPU / CPU-fallback / CPU badges and the device-level degraded alert on the
  device detail page.

- **Propagation note (absence means "no information").** The runner code is a
  **per-model copy** staged by `model_convertor.py`, so deploying a new
  LocalServer does NOT refresh already-deployed models: the record first
  appears after the model component's next restart (reboot, Nucleus restart,
  or model redeploy). Until then there is simply no
  `dda_active_providers.json`, and every consumer treats absence as "no
  information" — no `executionProviderInfo` field, no contribution to
  `gpu-status` in either direction, no badge in the portal. Absence is never a
  false signal.

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
  - **CPU images (x86_64, generic arm64 CPU): CPU only.** They install the
    onnxruntime CPU wheel. (JetPack 4, previously CPU-only here, is no longer
    supported.)
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

> **Amendment note** (see `.kiro/specs/onnx-compile-error-diagnostics/`): the
> compile step's *success* path was validated end to end as stated above, but
> its start-failure path destroyed its own diagnostics on the first status poll
> (the poller described a fabricated job name with the wrong API and overwrote
> the originating error). Fixed by the referenced spec, which establishes three
> contracts: (1) `error` / `failure_reason` are **write-once with respect to
> polling** — a poll never overwrites them, poll faults land in `poll_error`;
> (2) a failed ONNX start writes **no** `compilation_job_name` and carries
> `job_started: false`, so no describe call is ever issued for it; (3) every
> `compilation_jobs` entry is routed to its describe API (training vs. Neo
> compilation, or none) by the shared `classify_poll_kind` in the
> `compilation_status` layer module.

> **Amendment note** (see `.kiro/specs/onnx-jetson-publish-packaging/`):
> compiled ONNX exports (`target=onnx`) now package **per-JetPack** — one
> upload fans out to `onnx-jetson-xavier-jp5/-jp6/-jp7` entries, each ZIP
> carrying a `runtime: "onnx"` + `runtime_artifact` manifest — and publish as
> per-JetPack components with `aarch64` recipes and the matching
> `LocalServer.arm64JP{N}` HARD dependency, so the compile→package→publish
> chain above is closed for Jetson (the auto-chaining caveat no longer blocks
> Jetson delivery). BYO ONNX imports with no explicit target list now default
> to JetPack 5/6/**7** plus x86. ONNX is the **only** vision runtime on JP7
> (DLR/Neo is not carried there).

## 21. RF-DETR detection architecture (DETR-family decoder)

YOLO is not the only object-detection export a user may bring. RF-DETR (and the
broader DETR family) has a fundamentally different ONNX output shape and decode,
so the detection path is now **architecture-pluggable** via a
`detection.layout` / `detection_arch` selector rather than assuming YOLO.

### 21.1 How RF-DETR differs from YOLO

| | YOLO (v5/v8) | RF-DETR (DETR-family) |
|---|---|---|
| ONNX outputs | 1 tensor `[1, 4+C, N]` (or transposed) | 2 tensors: boxes `[1, Q, 4]` + logits `[1, Q, C]` |
| Box encoding | xywh (center), **network-pixel** scale | cxcywh, **normalized 0..1** |
| Scoring | max class score per anchor | per-query sigmoid (or softmax) over logits |
| Filtering | score threshold + **NMS** (IoU) | **NMS-free** — flattened top-k over query×class |
| Queries/anchors | ~8400 grid anchors | fixed query slots (e.g. 300) |

Because RF-DETR is set-based, there is **no NMS**; duplicate suppression is
handled by the model's Hungarian-matched training, so decode is a top-k over the
flattened (query × class) score matrix followed by a per-detection threshold.

### 21.2 On-device decoder

`src/backend/lyra_science_processing_utils/model_processors/rf_detr_detection_postprocessor.py`
(`RfDetrDetectionPostProcessor`, registered as `rf_detr_object_detection` /
codename "canele"):

- **Tensor identification by shape**, not order: the 3-D tensor whose last dim
  is 4 is boxes; the other is logits. This tolerates exporters that swap output
  order or name the tensors differently.
- **Scoring:** sigmoid per (query, class) by default; `use_softmax: true`
  switches to softmax-over-classes (with an optional `background_class` index to
  drop). 
- **Top-k:** flatten the `[Q, C]` score matrix, take the top `top_k` (default
  300), then apply `score_threshold`. Query index → box, class index → label.
- **Boxes:** cxcywh → xyxy, then scale **normalized (0..1) → source-image
  pixels** using `src_img_size` passed by `SingleStageModelGraph` (falls back to
  `network_input` if absent).
- **Output:** `list[ObjectDetectionResult]` — the exact same result contract as
  YOLO, so Phases B/C (reused `anomalies` tensor emit, marshal overlay, frontend
  `detections`) are unchanged.

Config keys (manifest `detection` block): `layout: "rf_detr"`, `num_classes`,
`score_threshold`, `top_k`, `use_softmax`, `background_class`, `network_input`,
`class_names`. Pure numpy — no torch on the hot path.

Unit tests: `test/backend-test/test_rf_detr_detection_postprocessor.py` (8/8
pass): shape-based tensor ID, sigmoid vs softmax scoring, top-k truncation,
threshold filtering, cxcywh→xyxy conversion, normalized→source scaling,
background-class drop, and empty-result handling.

### 21.3 Packaging (`model_converter.py`)

`generate_dda_package` gained a `detection_arch` param (`'yolo'` default |
`'rf_detr'`). It maps the architecture to the on-device stage type /
post-processor (`yolo_object_detection` vs `rf_detr_object_detection`) and writes
the `detection.layout` accordingly. NMS (`iou_threshold`) is written for YOLO;
`top_k` is written for RF-DETR. The handler parses `detection_arch` from the
request body and forwards it.

### 21.4 Frontend

Smart Import (`SmartImport.tsx`) shows a **"Detection architecture"**
selector (YOLO | RF-DETR) only when Model Type = Object Detection;
`api.ts convertModel` carries `detection_arch`.

### 21.5 Detection model package — RF-DETR manifest shape

```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "task": "object_detection",
  "detection": {
    "layout": "rf_detr",
    "num_classes": 90,
    "score_threshold": 0.5,
    "top_k": 300,
    "network_input": 560,
    "class_names": ["person", "bicycle", ...]
  },
  "model_graph": {
    "model_graph_type": "single_stage_model_graph",
    "stages": [{
      "stage_type": "rf_detr_object_detection",
      "threshold": 0.5,
      "image_width": 560,
      "image_height": 560,
      "image_range_scale": true,
      "normalize": false,
      "input_shape": [1, 3, 560, 560]
    }]
  }
}
```

Caveat: the RF-DETR decoder is verified against synthetic two-tensor outputs
(unit tests). Exact output tensor **names/order** on a real RF-DETR ONNX export
vary by exporter — the shape-based tensor ID handles order, but a real
end-to-end export/device run is still pending. RF-DETR normalized-cxcywh and
sigmoid scoring are the documented DETR-family conventions.

## 22. vLLM Hugging Face weight cache (persistence & disk cleanup)

HF-sourced vLLM model components cache their downloaded weights on the
device under `HF_HOME=/aws_dda/hf_cache` (set in the `environment` of all
three backend services in `src/docker-compose.yaml`). Because `/aws_dda`
is a persistent read-write bind mount, the cache survives backend
container recreation: after the first download, the post-restart reload
(driven by the backend's vLLM reconciler, §23 — the cache by itself never
re-issues a load) reads the weights from the local cache, with no
re-download and no dependency on huggingface.co being reachable.

### 22.1 Caches accumulate — undeployment does NOT remove them

Each HF model's weights live under:

```
/aws_dda/hf_cache/hub/models--{org}--{name}
```

(e.g. `/aws_dda/hf_cache/hub/models--Qwen--Qwen2.5-VL-7B-Instruct-AWQ`,
~6 GB for a 7B AWQ model).

Component undeployment does **not** delete these caches. The model
component's Shutdown script (`vllm_model_prep.py --cleanup`) removes only
the staged Triton model repository under `/aws_dda/dda_triton/`, never
the HF cache — deliberately, so a redeploy of the same model does not pay
the full re-download again, and because safe automatic eviction would
require cross-component reference counting (another deployed component
may use the same HF model ID). Caches from undeployed models therefore
accumulate under `/aws_dda/hf_cache/hub/` until removed manually.

### 22.2 Manual cleanup (small-disk devices)

To reclaim disk space, remove a model's cache tree **while that model is
not deployed** (and no other deployed vLLM component references the same
HF model ID):

```bash
rm -rf /aws_dda/hf_cache/hub/models--{org}--{name}
```

For example:

```bash
rm -rf "/aws_dda/hf_cache/hub/models--Qwen--Qwen2.5-VL-7B-Instruct-AWQ"
```

If the model is deployed again later, the first load simply re-downloads
the weights (requires huggingface.co reachability, same as any first
download). To see what is using space: `du -sh /aws_dda/hf_cache/hub/*`.

## 23. vLLM post-restart lifecycle (reconciler + unload tombstone)

(Spec: `vllm-model-reload-after-backend-restart`.) The vLLM load used to be
a **one-shot** action: the model component's Startup
(`vllm_model_prep.py`) stages the Triton-style repository under
`VLLM_MODEL_DIR` and POSTs one load to the backend's runtime server, but
the loaded engine lives **in the backend process** — recreated empty on
every backend start. A backend restart (crash, `docker restart`, the
awscrt abort class) therefore used to orphan every loaded model: staged on
disk, component RUNNING, yet `models: {}` forever, every generate 409,
status stuck "LOADING", until a human restarted the model component. That
is fixed by a reconciler in the backend itself.

### 23.1 The `Vllm_Reconciler`

`src/backend/vllm_runtime/reconciler.py` (`VllmReconciler`), started by
`app.py::start_vllm_runtime()` after the runtime server is up, as one
daemon thread named `vllm-reconciler` (vLLM-free images — JP5 default,
x86 — never import or construct it; their startup sequence is unchanged):

- **One-shot scan.** One snapshot of the model repository at backend
  start: models in state STAGED (explicitly unloaded / tombstoned models
  are excluded — see §23.2), sorted by name for a deterministic order. No
  periodic re-scan; every backend start runs a fresh one.
- **Sequential loopback re-drive.** Each candidate load is issued through
  the SAME loopback model-control endpoint the component Startup uses —
  `POST http://127.0.0.1:{VLLM_RUNTIME_PORT}/v2/repository/models/{m}/load`
  — never `manager.load()` directly, so engine construction happens on the
  runtime server's event loop and concurrent loads (e.g. a component
  Startup racing the reconciler on a fresh deploy) serialize to exactly
  ONE engine construction. The state is re-checked right before each POST
  and LOADING/READY models are skipped.
- **Bounded retries.** `RECONCILE_RETRY_BACKOFF_SECONDS = (30, 120, 480)`
  — 4 attempts total, ~10.5 min worst case, never a retry storm. Each
  failed attempt applies the same KV-cache-OOM single unload→reload
  recovery the component Startup uses. On exhaustion the model stays
  FAILED with the retained failure reason and one prominent ERROR log
  names the model, the reason, and that automatic retries are exhausted.
  One model's failure never stops the scan for the others.

This also makes the `vllm_model_prep.py` `LOAD_UNREACHABLE` diagnostic's
promise — "the model **stays staged for the next LocalServer start**" —
actually true: the staged repository always survived on disk, but before
the reconciler nothing ever scanned it and re-issued the load. The
reconciler is what redeems that promise (the prep script itself is
unchanged).

### 23.2 The `Unload_Tombstone` (`.dda_explicit_unload`)

Reconciliation must distinguish "desired but orphaned by a restart" from
"explicitly stopped". An explicit unload (model-control
`POST .../unload`) writes a marker file
`VLLM_MODEL_DIR/{model}/.dda_explicit_unload` (content is triage-only
JSON with a UTC timestamp; its mere existence is what counts):

- **Unload suppresses.** A staged-but-tombstoned model is skipped by the
  reconciler and stays unloaded across restarts. The unload itself always
  succeeds and frees the engine — a marker write failure is logged and
  never fails the unload.
- **Load / re-stage re-arms.** An explicit load clears the marker as its
  first action, and the component Startup's atomic re-stage
  (`shutil.rmtree` + `os.rename` directory replace) removes it wholesale
  with the old directory — a redeploy re-arms reconciliation with zero
  `vllm_model_prep.py` changes.
- **`--cleanup` removes everything.** The component Shutdown's
  `vllm_model_prep.py --cleanup` removes the staged directory, marker
  included — nothing left staged, nothing scanned, no litter, never
  resurrected.

### 23.3 Truthful status vocabulary

The status surfaces (feature-config API and the text-generation 409
category) now tell the truth about the post-restart lifecycle:

- **UNLOADED → "STOPPED".** A staged-but-tombstoned model reports the new
  reporting-only `UNLOADED` state, mapped to the existing device status
  vocabulary as `STOPPED` in feature-config and to the `unloaded` 409
  category in text-generation — a generate against an explicitly unloaded
  model fails fast instead of burning the workflow binding's 240 s poll
  budget (the binding polls only on "loading").
- **STAGED → "LOADING" is now guaranteed.** This mapping used to assume "a
  load request is on the way" with no mechanism behind it after a restart.
  It is retained because the reconciler makes it true by construction:
  first deploy — the component Startup's load; after a restart — the
  reconciler's re-drive; bounded time in both cases.
- **FAILED with retained reason after exhaustion.** When the retry
  schedule is exhausted, the model reports FAILED with the retained
  backend failure reason — never an indefinite "LOADING" with no load in
  flight.

## 24. RF-DETR-Seg (instance and semantic modes)

(Spec: `rfdetr-seg-support`.) RF-DETR-Seg is the segmentation member of the
RF-DETR family of §21: the same DINOv2 backbone and set-based decoder, plus a
mask head. One on-device decoder,
`src/backend/lyra_science_processing_utils/model_processors/rf_detr_seg_postprocessor.py`
(`RfDetrSegPostProcessor`), serves it in two **Output_Modes** selected per
model when it is trained, imported or packaged:

- **instance** — one mask per detected object beside its box and class: the
  detection result contract of §21 extended with `mask_rle` / `mask_area`
  per entry, plus a Composite_Mask image (§24.5);
- **semantic** — every instance merged into one class-indexed mask: the
  record is exactly the LFV segmentation (anomaly) record (§24.11).

The published checkpoints come in six sizes (nano 312, small 384, medium 432,
large 504, xlarge 624, 2xlarge 768 — native square resolution in pixels),
all Apache 2.0 in rfdetr 1.10.1 (owner decision D4); training, `.pth`
conversion and ONNX Smart Import are described in the README and the trainer
README. This section is the **device contract**.

### 24.1 Output contract (Seg_Output_Contract)

An RF-DETR-Seg ONNX export has one input `input [1, 3, R, R]` and exactly
three outputs, in graph order:

| name | shape | meaning |
|---|---|---|
| `dets` | `[1, Q, 4]` | boxes, cxcywh, **normalised 0..1** |
| `labels` | `[1, Q, C + 1]` | per-query class logits; the trailing slot is rfdetr's never-positive background slot |
| `masks` | `[1, Q, R/4, R/4]` | per-query mask logits on the `R/4` grid (rfdetr `mask_downsample_ratio`) |

`Q` is the size's query count (nano / small 100, medium / large 200, xlarge /
2xlarge 300) and `C` the number of classes. rfdetr 1.10.1 declares the four
`masks` dims symbolically in the protobuf although every runtime produces
`[1, Q, R/4, R/4]`; the trainer and the conversion job stamp the static dims
after export (spike S2), and the device never relies on the declaration:

- **Identification is by run-time shape, not order or name.** After
  squeezing a leading batch dim of 1 the decoder takes the `[Q, 4]` tensor
  as boxes, the `[Q, S]` tensor as logits and the `[Q, Hm, Wm]` tensor as
  masks, in any order. Fewer than three usable tensors, or a `Q` mismatch
  between them, yields the mode's **empty result** with one warning per
  processor instance — never a raise, never a failed capture.
- **The `C + 1 == 4` tie-break.** A three-class model has `labels [Q, 4]`,
  the same shape as `dets`. The manifest's `detection.output_names` (the
  graph-order names `["dets", "labels", "masks"]` the packager copies from
  the trainer's / inspector's `onnx_output_names`) decides which rank-2
  tensor is which, because the ONNX runner returns outputs in graph order.
  Without `output_names` the decoder takes the runner's order as
  `[boxes, logits]` and warns once. The same tie-break was added to the
  detection decoder of §21 (`rf_detr_object_detection`), which has the
  identical ambiguity for a three-class detector.

### 24.2 Stage types and `detection.output_mode`

| stage type | codename | mode | returns |
|---|---|---|---|
| `rf_detr_instance_segmentation` | `macaron` | instance | `list[InstanceSegmentationResult]` |
| `rf_detr_semantic_segmentation` | `financier` | semantic | `AnomalyResult` (score + index mask) |

Both map to `RfDetrSegPostProcessor` in `MODEL_NAME_MAP`; the **stage type
decides the mode**. `detection.output_mode` (`"instance"` / `"semantic"`)
declares the same choice for readers of the manifest; a contradiction
between the two is logged once as an error and the stage type wins. A
manifest written before this contract (stage type
`rf_detr_semantic_segmentation`, a `detection` block without `output_mode`,
`top_k` or `output_names`) decodes in semantic mode with the defaults below.
`rf_detr_semantic_segmentation_postprocessor.py` is now an import shim onto
the one decoder.

### 24.3 Manifest shape, both modes

The Portal's shared builder (`detection_training.build_detection_device_manifest`)
writes the manifest for trained, converted and imported Seg models alike. Both
modes use the **top-level** `detection` block (the Triton base model merges it
into the stage with `setdefault`): the RF-DETR block (`layout: "rf_detr"` names
the decoder family, `top_k` = the size's `Q`, no `iou_threshold`) plus
`mask_threshold`, `output_mode`, `output_names` and, last, `class_names`.
`model_graph.pixel_level_classes.names` is `["background"] + class_names`
(exactly `num_classes + 1` names; `class_<i>` placeholders where the trainer
had no names) in **both** modes, because both write a Composite_Mask through
the anomaly-mask utilities. Preprocessing is RF-DETR's: square resize
(`preserve_aspect: false`) and `normalize: true` (ImageNet mean/std after
0..1 scaling). Instance mode keeps `task: "object_detection"`; semantic mode
omits `task` (the anomaly default) — the only other difference.

The two examples below are generated from the builder and checked against it
by `edge-cv-portal/backend/tests/test_docs_manifest_examples.py` (the HTML
comment names the arguments; the builder's `dataset` block is omitted here).
`output_shape` is the builder's nominal value: the packager replaces it with
the exported logits shape and adds `output_shapes` (`[dets, labels, masks]`
as exported). The device reads neither.

Instance mode, nano, two classes:

<!-- manifest: rf_detr_seg instance nano -->
```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "model_graph": {
    "model_graph_type": "single_stage_model_graph",
    "stages": [{
      "type": "rf_detr_instance_segmentation",
      "input_shape": [1, 3, 312, 312],
      "output_shape": [1, 6, 8400],
      "image_width": 312,
      "image_height": 312,
      "image_range_scale": true,
      "normalize": true,
      "threshold": 0.5,
      "num_classes": 2
    }],
    "pixel_level_classes": {
      "names": ["background", "plate", "scratch"],
      "normal_ids": [0]
    }
  },
  "input_shape": [1, 3, 312, 312],
  "preprocessing": {"resize": [312, 312], "channel_order": "RGB"},
  "task": "object_detection",
  "detection": {
    "layout": "rf_detr",
    "num_classes": 2,
    "score_threshold": 0.5,
    "network_input": 312,
    "preserve_aspect": false,
    "top_k": 100,
    "mask_threshold": 0.5,
    "output_mode": "instance",
    "output_names": ["dets", "labels", "masks"],
    "class_names": ["plate", "scratch"]
  }
}
```

Semantic mode, same model — the stage type, `output_mode` and the absent
`task` are the differences:

<!-- manifest: rf_detr_seg semantic nano -->
```json
{
  "runtime": "onnx",
  "runtime_artifact": "model.onnx",
  "model_graph": {
    "model_graph_type": "single_stage_model_graph",
    "stages": [{
      "type": "rf_detr_semantic_segmentation",
      "input_shape": [1, 3, 312, 312],
      "output_shape": [1, 6, 8400],
      "image_width": 312,
      "image_height": 312,
      "image_range_scale": true,
      "normalize": true,
      "threshold": 0.5,
      "num_classes": 2
    }],
    "pixel_level_classes": {
      "names": ["background", "plate", "scratch"],
      "normal_ids": [0]
    }
  },
  "input_shape": [1, 3, 312, 312],
  "preprocessing": {"resize": [312, 312], "channel_order": "RGB"},
  "detection": {
    "layout": "rf_detr",
    "num_classes": 2,
    "score_threshold": 0.5,
    "network_input": 312,
    "preserve_aspect": false,
    "top_k": 100,
    "mask_threshold": 0.5,
    "output_mode": "semantic",
    "output_names": ["dets", "labels", "masks"],
    "class_names": ["plate", "scratch"]
  }
}
```

### 24.4 Decode rules

The decoder is pure numpy + cv2, no torch on the hot path:

1. **Scores.** `sigmoid(labels[:, :C])` with `C = min(num_classes, S)`: the
   first `C` slots are classes, the background slot is never scored. Without
   `num_classes` every slot is scored (a foreign graph); `num_classes > S`
   logs an error once and uses `S`.
2. **Flattened top-k.** Over the `Q × C` (query, class) pairs, keep the
   `k = min(top_k, Q × C)` highest, then drop pairs below `score_threshold`
   — exactly the detection decoder's selection, so no NMS. A query kept for
   two classes yields two instances. `top_k` defaults from
   `detection.network_input` when the manifest omits it: 312 / 384 → 100,
   432 / 504 → 200, 624 / 768 → 300, anything else 300.
3. **Masks, bilinear then threshold.** For each kept pair in descending
   score order: `sigmoid(masks[q])` on the `R/4` grid, resized with
   `cv2.INTER_LINEAR` to the **source frame** the graph received
   (`src_img_size`), then `> mask_threshold` (default 0.5; rfdetr's reference
   order). `mask_area` is the foreground pixel count at source resolution.
   **Zero foreground drops the pair** — a box without a mask is never
   emitted.
4. **Boxes.** cxcywh → xyxy, scaled from 0..1 to source pixels with the
   square-resize geometry (no letterbox), as in §21.
5. **Cap and order.** At most `top_k` instances, score descending. Zero
   instances is an empty list (instance mode) or an all-background mask
   (semantic mode).

Semantic mode paints the kept instances into one index mask
(`paint_composite`, also used by the Triton base model in instance mode):
instances are painted in **ascending** confidence so the highest score wins an
overlap, index `= min(class_index + 1, 103)`, index 0 = background. The
result is `AnomalyResult(score = highest kept confidence, mask = that index
mask)`. Compared with the pre-Seg semantic decoder two rules changed on
purpose: bilinear instead of nearest-neighbour upsampling (smoother edges) and
flattened top-k instead of a per-query argmax (a query can contribute two
classes).

**Palette limit.** The anomaly-mask palette has 104 rows, so a Composite_Mask
distinguishes at most **103 classes**; a model with more logs one warning at
load and classes ≥ 103 share the last colour in the composite. The instance
records are unaffected — their `class_index` and `mask_rle` are exact; only
the composite's colours saturate.

### 24.5 Instance result record (the Detection_List)

In instance mode every entry of the detection list carries, beside the §21
keys (`bounding_box` in source pixels, `class`, `class_label`, `confidence`,
`confidence_threshold`):

| key | value |
|---|---|
| `class_index` | integer class id (`class_names[class_index]` is the label) |
| `mask_rle` | the mask as **Canonical_RLE at source resolution**: space-separated run lengths of the `height × width` binary mask flattened in column-major order, the first run counting background pixels (a mask whose first pixel is foreground starts with `0`), summing to `width × height` — COCO's uncompressed-RLE order, so the counts drop into `{"counts": [...], "size": [h, w]}` unchanged (`lyra_science_processing_utils/utils/mask_rle.py`, pinned to the Portal encoder by a shared vector file) |
| `mask_area` | foreground pixels, `int` |
| `mask_truncated` | `true` only when the ceiling below removed this entry's `mask_rle` |

**The 4 MiB ceiling.** The base model serialises the list and, while it
exceeds `DETECTION_LIST_MAX_BYTES` (4 MiB), removes `mask_rle` from the
**lowest-confidence** entry that still has one, marks it
`mask_truncated: true` and keeps its box and `mask_area`; one warning per
capture names the count. A box is never dropped and a capture never fails on
size. The ceiling applies to every serialisation of the list (capture record,
sidecar, live result), not to the Composite_Mask PNG.

**Capture record** (`marshal_for_capture_template.py`). The `detections`
block of the label is the map `{"0": {...}, "1": {...}}` as for detection,
each entry copying `mask_rle`, `mask_area` and `mask_truncated` when present;
the block gains `detections_truncated: true` when any entry was truncated
(only then — a YOLO / RF-DETR capture is byte-identical to before) and, when
a mask was written, `mask_color_map` (`{"0": {background share}, "1": {per
class}}` with the class's hex colour and pixel share, the same map the label's
`internal-color-map` carries). The same `detections` block is written to the
detections sidecar. The overlay image draws the boxes as today and fills each
mask with its class colour at alpha 0.4 under them; `mask.png` beside it is
the Composite_Mask (the colourised index mask), written whenever
`pixel_level_classes` is non-empty. Zero instances produce the zero-object
sentinel, an empty mask and `output = 0`, exactly as a zero-box detection.

Colours: the class colour of `class_index` is palette row `class_index + 1`
in both the composite and the overlay fill.

### 24.6 In-process mask memory bound (DD3) and measured cost

Between decode and serialisation up to `top_k` (300) source-resolution masks
are alive inside the Triton Python stub; dense `bool` arrays at 1920×1080
would be ~2 MB each, ~600 MB in the worst case. The decoder therefore keeps
each kept mask only in **packed-bit form** (`np.packbits`, 259,200 B per
1080p instance) and releases the dense arrays before the next pair;
`paint_composite` and `serialize()` unpack one mask at a time. The cost is
the extra pack / unpack per instance; measured in the spike on an x86_64
Build Fleet server in the Export_Image container (16 vCPU, CPU only,
1920×1080 frames):

| instances | decode total | resize + threshold + packbits | unpack + paint composite | RLE encode | `mask_rle` text |
|---|---|---|---|---|---|
| 10 | 49 ms | 20 ms | 6 ms | 22 ms | 46 KB |
| 100 | 447 ms | 177 ms | 58 ms | 212 ms | 458 KB |

The Jetson figure (the lab JP6 Orin devkit) is taken during the on-device
verification of the feature and recorded here with it. These numbers are the
post-processing cost per frame on top of the ONNX run, and scale with the
number of kept instances, not with `top_k`.

### 24.7 Where the record goes on the device

- **Live result** (`inference_results_utils.save_image_object`; the HMI live
  card and the device API / WebSocket): `detections` stays the **map** with the
  new per-entry keys passing through; `detections_truncated` is copied only
  when the block carries it; when `mask.png` exists the result also carries
  `mask_image`, `mask_background` and `anomalies` (the per-class coverage
  from `mask_color_map`), read exactly as for a segmentation result.
- **Result history DB**: the `inference_result` table gained a `detections`
  JSON column (Alembic revision `fe9a273bbe2c`) holding the list form of the
  block — `[entry_0, entry_1, …]` in key order, `mask_rle` included — written
  for every capture whose block is a non-empty map; `GET
  /workflows/{id}/results` and `/results/{capture_id}` return it as
  `detections`.
- **Run preview** (`GET /workflows/executions/{execution_id}/overlay`): the
  body has **four keys**, `{maskImage, maskBackground, instances,
  detectionsTruncated}`. `instances` is the run's list (sidecar first, record
  second, entries verbatim including `mask_rle`) and `detectionsTruncated`
  the block marker; both are `null` for a run without instance results.
- **Run metadata / conditions** (`workflow_engine/detections.py`): entries
  gain `mask_area` only when the raw entry carries a finite number; `mask_rle`
  is never copied into telemetry. The debug-bundle recorder projects only
  `label`, `confidence`, `box` and `id`, so bundles are unchanged.
- **HMI**: run results and the result-history detail draw the masks on a
  canvas over the image (`InstanceMaskLayer`, toggle **Masks + boxes** /
  **Boxes**); the detected-objects table shows a **Mask area (px)** column
  when any row has one; the live card lists **Mask coverage** per class under
  the objects when the result carries the coverage map.

### 24.8 Instance-manifest export

`GET /workflows/{workflow_id}/results/export` takes
`manifest_format=classification` (the default: today's manifest,
byte-identical) or `manifest_format=instance`. The instance form writes the
Portal's **Instance_Manifest** — one line per stored result whose
`detections` carry at least one `mask_rle`: `source-ref` (the image file
beside the manifest in the zip), `instance-segmentation` (image size, one
annotation per mask-carrying entry with `class_id`, the box as left / top /
width / height and the entry's `rle` verbatim) and
`instance-segmentation-metadata` (confidences, the export-wide `class-map`,
`human-annotated: "no"`). Results without a mask-carrying entry, and
truncated entries, are skipped and counted in the log. Any other value is a
400. In the HMI the workflow's **Download result images** dialog has a
**Manifest format** select (Classification / Instance segmentation), and the result
history's download of selected rows offers **Download with
instance-segmentation manifest** when a selected row stores an instance
mask; both append `&manifest_format=instance` to this URL.

### 24.9 Edge MCP

One rule for every edge tool whose result carries the Detection_List: boxes
and `mask_area` are returned, **`mask_rle` is always dropped** (a few 1080p
masks as text would exceed the 200 KB object-result cap and the tool would
answer `result_too_large` instead), and the mask image stays dropped or
extracted exactly as before — the Composite_Mask reaches the caller as the
extracted image where a tool extracts one. The drop paths per tool are listed
in `docs/edge-mcp-server.md`; the live `inferenceResult.detections` is a map,
so the path grammar gained the `{}` segment (every value of an object) beside
`[]` (every element of a list).

### 24.10 Feature floor

A LocalServer built before this contract has no `rf_detr_instance_segmentation`
stage type (instance mode fails at model load) and runs the pre-Seg decoder in
semantic mode. The Portal therefore keeps a per-architecture **Seg feature
floor** — the first LocalServer version carrying this section — in one CDK
literal (`WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS`, read by the
workflow packager and the deployment gate). A workflow that references an
RF-DETR-Seg model is packaged with its LocalServer floor raised to the maximum
of the lineage, stream and Seg floors, and an architecture without an entry is
refused: `409 RFDETR_SEG_UNSUPPORTED_ARCH` at packaging, the same reason at
the deployment gate. Workflows without a Seg model are untouched. The entries
are filled per architecture once its build is verified; an architecture whose
hardware verification is pending is listed as unverified in the Portal tests
instead.

### 24.11 Semantic mode is the LFV segmentation record

In semantic mode nothing above §24.4 applies: the decoder returns an
`AnomalyResult`, the base model emits the anomaly tensors (score, the
colourised mask from `pixel_level_classes`), the marshal writes the anomaly
capture record (`mask.png`, `internal-color-map`, `anomaly-mask-ref-detected`,
no `detections` block), and every reader — live result, DB row, run preview,
HMI, Portal captures, export — handles it as it handles an LFV segmentation
model. `mask_rle`, `mask_area`, the `detections` column and the instance
export do not occur. Switching a model between the modes is a packaging
choice (**Output** on the model's page), not a retraining.
