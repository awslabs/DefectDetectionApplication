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

- `onnxruntime-gpu` — Jetson build, **per JetPack** (JP5 CUDA 11.4 vs JP6
  CUDA 12.2). Mirrors the existing per-JetPack lib provisioning pattern
  (cf. the libcudart.so.11 staging for JP6).
- `torch` — Jetson wheel, **only if PyTorch runtime ships in v1**; gate behind a
  Docker build arg.
- `dlr==1.10.0` stays as-is.
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
