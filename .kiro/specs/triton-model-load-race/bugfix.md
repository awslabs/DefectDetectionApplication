# Bugfix: Triton model-load race on concurrent model/LocalServer deploys

## Summary

When new model versions are (re)packaged, republished, and deployed to an edge
device, models can get permanently stuck in `LOADING` state in the LocalServer
UI even though the model artifacts are valid (a standalone `tritonserver
--model-repository ...` run loads them fine). The only recovery today is a
manual LocalServer restart.

## Observed behavior (jp5 device, real incident)

- After deploying LocalServer v1.0.32 + repackaged models, the LocalServer UI
  showed all ensemble models stuck `LOADING`.
- App log (`dda_triton.triton_edge_client`) reported: `marshal_model-cookies` =
  `READY`, every other base/marshal/ensemble model = `LOADING`, unchanged for
  13+ minutes.
- Triton's Python backend emitted at boot:
  `pb_stub.cc:2081 Failed to preinitialize Python stub: Python model file not
  found in '.../base_model-cookies-binary-jetson-xavier-jp5/10/model.py'`.
- File timestamps: `base_model-cookies/10/model.py` was written at
  `04:36:32.505`, but the stub tried to read it at `04:36:32.147` — the load
  beat the file copy by ~358 ms.
- A clean LocalServer restart (all files already present) loaded every model
  successfully, confirming the artifacts are valid and the failure is purely a
  startup-timing race.
- Memory/GPU were healthy (11 GiB free, no OOM), ruling out resource pressure.

## Root cause

1. **Non-atomic model-repo assembly.** In
   `src/backend/dda_triton/model_convertor.py`, `_create_base_model_structure`
   (and the marshal/ensemble variants) create the model version directory and
   write `config.pbtxt` *before* copying `model.py` / `inference_runtimes.py`
   and creating the artifact symlinks. Triton recognizes a model as loadable
   from `config.pbtxt` + version dir, so there is a window in which Triton (or a
   concurrent `/start`) observes a model dir whose backend `model.py` is not yet
   present. Model components run `model_convertor.py` concurrently with each
   other and with LocalServer startup (all restarted by a single Greengrass
   deployment), so this window is hit intermittently.

2. **Non-resilient load queue (compounding).** A single Python-stub
   preinitialize failure wedges the entire edgemlsdk `TritonModelLoadJobQueue`,
   leaving unrelated sibling models stuck `LOADING` too (marshal rf-detr / yolo
   had already loaded seconds earlier yet still hung). One model's failure
   should not block sibling loads, and a transient missing-file at load time
   should be retryable.

## Scope

- In scope: durable fix so the model version directory is only ever visible to
  Triton in a complete state; hardening so a single model's load failure does
  not wedge sibling model loads and transient failures are retried.
- Out of scope: changing the edgemlsdk C++ `TritonModelLoadJobQueue` internals
  (mitigated from the Python side instead); Portal-side deployment gating (a
  partial mitigation only — the race also occurs on multi-model co-deploys with
  no LocalServer update).

## Impact if unfixed

Intermittent, hard-to-diagnose "models stuck LOADING" after any deploy that
delivers model updates; requires manual device intervention (LocalServer
restart) to recover. Undermines confidence in Portal-driven deploys.
