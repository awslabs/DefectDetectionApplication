# JP7 vLLM On-Hardware Validation Procedure

Feature: **jp7-vllm-enablement** — manual validation of the default-enabled
from-source vLLM path on a physical **Jetson Thor developer kit
(JetPack 7.1 / 7.2, 128 GB unified memory)**.
Covers Requirements 2.8 and 6.1–6.7 of the spec
(`.kiro/specs/jp7-vllm-enablement/requirements.md`).

Two models are exercised through the complete pipeline
(register → package → publish → deploy → READY → generate → stream →
workflow → coexistence), plus in-container GPU checks
(`torch.cuda.is_available()`, engine load reaching loaded status):

| | Smoke_Model | Realistic_Model |
|---|---|---|
| Hugging Face model ID | `facebook/opt-125m` | `Qwen/Qwen2.5-7B-Instruct` |
| Purpose | Fast end-to-end smoke of the full pipeline | Realistic mid-size workload on the 128 GB Thor |
| `gpu_memory_utilization` | `0.3` | `0.5` |
| `max_model_len` | `2048` | `8192` |
| Other engine settings | defaults (`dtype=auto`, `tensor_parallel_size=1`, `enforce_eager=true`) | defaults (`dtype=auto`, `tensor_parallel_size=1`, `enforce_eager=true`) |
| Approx. weights download | ~250 MB | ~15 GB (FP16/BF16) |

These are the engine configurations **recorded in the design**
(`.kiro/specs/jp7-vllm-enablement/design.md`, Components §8), sized against
the portal fit check's `arm64_jp7` device memory profile of
**120 GiB (`128849018880` bytes)**: the Smoke_Model's `0.3` fraction budgets
0.3 × 120 GiB = **36 GiB** and deliberately leaves GPU headroom for the
vision-coexistence stage; the Realistic_Model's `0.5` fraction budgets
0.5 × 120 GiB = **60 GiB** against ~15 GiB of weights plus KV cache —
comfortably sized for the Thor target and consistent with the fit-check
profile.

Record the outcome of every stage in the results table at the end of this
document.

> **Automation status — Edge_Test_Harness.** The verify-side stages of this
> runbook (L, G, S, C, and the run/verify half of W) can be automated by the
> pytest harness in `test/on-hardware/harness/` — see its README.
> `harness/devices.yaml.example` ships no JP7 profile yet: add a `jp7-thor`
> entry to your `devices.yaml` mirroring `jp6-orinagx` with
> `architecture: arm64_jp7`, capabilities `[vllm, onnx_models, workflows]`
> (no `dlr_models` — DLR is unsupported on JP7), and expected vLLM model
> `opt125m-smoke`. Once the deploy-side stages below (0–4: build, register,
> package/publish, deploy) have been completed through the portal, one
> command replaces the manual curl/portal verification:
>
> ```bash
> DDA_HARNESS_DEVICE=jp7-thor pytest test/on-hardware/harness/stages
> ```
>
> The manual instructions in the automated stages are kept below for triage
> and for devices without harness coverage.
>
> | Runbook stage | Harness coverage |
> |---|---|
> | 0–4 (prereqs, build, register, package/publish, deploy) | **Manual** — portal/build-server steps, out of harness scope |
> | 1.4 In-container GPU checks | **Manual** — `docker exec` on the device (Req 2.8, 6.7) |
> | 5 Stage L — READY propagation | Automated: `stages/test_20_vllm_textgen.py` (expected models → READY within `vllm_ready_s`) |
> | 6 Stage G — non-streaming generate | Automated: `stages/test_20_vllm_textgen.py` (non-empty text + latency/token metrics) |
> | 7 Stage S — SSE streaming | Automated: `stages/test_20_vllm_textgen.py` (incremental tokens + `done` termination) |
> | 8 Stage W — `llm_inference` workflow | Partially automated: `stages/test_30_workflows.py` runs a deployed workflow and asserts the `llm.<nodeId>.generated_text` metadata; workflow authoring/packaging/deployment stays manual (steps 1–3) |
> | 9 Stage C — vision coexistence | Automated: `stages/test_40_coexistence.py` (both READY through a completed generate) plus `stages/test_10_vision_models.py` (vision lifecycle) |

---

## 0. Prerequisites

- **Ubuntu 24.04 arm64 build server** (aarch64, docker + the compose v2
  plugin + GDK installed; see the README's
  "JetPack 7 (JP7) Build Server" section and `setup-build-server.sh`).
  Builds must run **one at a time** (see `.kiro/steering/builds.md`).
- **Jetson Thor developer kit (128 GB), JetPack 7.1 or 7.2
  (Jetson Linux r38.x)**, provisioned as a Greengrass core device whose
  thing records Target_Architecture `arm64_jp7`, with:
  - the JP7 host prerequisites from the README's
    "JetPack 7 (JP7) Devices" section applied (NVIDIA Container Runtime
    registered with Docker, compose v2 plugin, r38.x driver stack);
  - internet reachability to `huggingface.co` (HF-sourced weights download
    on device) and to the component artifact bucket;
  - ≥ 30 GB free disk for the Qwen weights + staging;
  - shell access (SSH) for log triage.
- **Portal stack deployed** (with this feature's `arm64_jp7` gating changes)
  and reachable; a Use_Case exists with the target device registered; portal
  user with the `DataScientist` role (registration) and deployment
  permissions.
- A **previously supported vision model** (ONNX or Triton-served — **not**
  DLR/Neo-compiled, which JP7 does not support) already validated on JP7,
  available in the same Use_Case for the coexistence stage.
- Device API access for verification commands: the LocalServer backend
  listens on the device at **port 5000** (plaintext, station auth disabled)
  or **port 5443** (TLS, station auth enabled). The curl examples below use
  `http://<device>:5000`; substitute `https://<device>:5443 -k` plus a
  session token if station authorization is enabled on the device.

Note on `test/on-hardware/register_vllm_models.py`: that seed script
registers the **JP6** engine configurations — its Smoke_Model values
(`0.3` / `2048`) match this procedure, but its Realistic_Model uses the JP6
figure `gpu_memory_utilization=0.55`, not the JP7-recorded `0.5`. For JP7,
register the Realistic_Model through the manual form entry in Stage 2 (or
adjust the script payload to `0.5` before running it).

---

## 1. Build the JP7 image (Ubuntu 24.04 arm64 build server)

### 1.1 Default build (vLLM enabled)

On the arm64 build server, from the repo root, with **default build args**
(no `VLLM_*` overrides):

```bash
# ensure no other build is running
pgrep -af "gdk component build"; pgrep -af "build-custom.sh"

# build + publish the JP7 LocalServer component
./gdk-component-build-and-publish.sh aarch64 7 2>&1 | tee .gdk_build_jp7.log
```

(Equivalently: set `gdk-config.json` to `aws.edgeml.dda.LocalServer.arm64JP7`
and run `gdk component build`, which invokes
`bash build-custom.sh aws.edgeml.dda.LocalServer.arm64JP7 NEXT_PATCH`, then
`gdk component publish`.) Expect the ~1–2 h `ONNXRUNTIME_GPU=1` source build
**plus the multi-hour from-source vLLM compile** (the measured duration
range is recorded in the README's JP7 build section; record the figure from
this run if it is still the placeholder).

**Expected outcomes** (all visible in the build log):

1. Interpreter audit guard passes: `TOTAL counterexamples: 0`.
2. `JetPack 7: 1` and `Backend python: 3.11 (edgemlsdk/tooling python: 3.11)`
   — JP7 has a single cp311 interpreter, no JP6-style split.
3. The backend image builds `FROM nvcr.io/nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:…`
   (CUDA 13.0.2 base, digest-pinned).
4. The **torch layer** installs `torch==2.9.0+cu130 torchvision==0.24.0
   torchaudio==2.9.0 triton==3.5.0` from
   `https://download.pytorch.org/whl/cu130`, and its import gate prints
   `torch 2.9.0+cu130 cuda 13.0` (a CUDA 13.x build — `torch.cuda.is_available()`
   is deliberately **not** asserted at build time; the build server has no
   Thor GPU).
5. The **vLLM layer** runs `install_vllm_gpu.sh`: prerequisite checks pass,
   the ccache presence/absence message is logged, vLLM is cloned at tag
   `v0.11.2`, `use_existing_torch.py` runs, the Classic-API compatibility
   patch is applied after its shim-shape guard, and the `sm_110` kernel
   compile runs with at most 6 parallel jobs (`MAX_JOBS`).
6. The built wheel (cp311-compatible tags, `aarch64` platform) is staged to
   `/opt/vllm-wheels` **before** the build-tree cleanup, then installed with
   the `numpy>=1.24,<2` co-constraint.
7. The script's post-install verification passes: `import vllm` first, then
   the per-symbol checks (`AsyncEngineArgs`, `SamplingParams`,
   `AsyncLLMEngine.from_engine_args` / `.generate` /
   `.shutdown_background_loop`), and the torch-unchanged check (installed
   torch still `2.9.0+cu130`).
8. The extended import gate passes each named check (`vllm`, `torch`, and
   every Classic_Engine_API symbol including `errored`); the
   dependency-consistency gate prints `app deps OK: numpy 1.24.3` and
   `pip check` reports no broken requirements.
9. In-image backend tests and the six security audit gates run under the
   image's DDA interpreter (python3.11) and pass.
10. `gdk component publish` registers a new
    `aws.edgeml.dda.LocalServer.arm64JP7` component version.

**Failure triage**

| Symptom | Triage |
|---|---|
| Script exits before the compile naming a missing prerequisite (CUDA toolkit/nvcc, torch, git/pip/headers) | The fail-fast checks fired by design (Req 1.8). If torch is named: the torch layer above it failed or was skipped — check the torch layer's own gate output and that `VLLM_ENABLE` was not partially overridden. |
| Shim-shape guard miss naming `vllm/engine/async_llm_engine.py` | A `VLLM_VERSION` override checked out a tag whose shim no longer matches `AsyncLLMEngine = AsyncLLM`; the compat patch refuses to apply blind. Build with the default `v0.11.2` or update the script for the new tag. |
| Compile error / no `dist/vllm-*.whl` / wheel tag check fails | The build is loud by design — no partial artifact is installed (Req 1.11). Check `TORCH_CUDA_ARCH_LIST=11.0` reached CMake, and nvcc is CUDA 13.x. An OOM-killed compiler (dmesg) → lower `VLLM_BUILD_JOBS`. |
| Torch-unchanged check fails naming two versions | The vLLM install displaced torch — `use_existing_torch.py` did not strip the torch pins (wrong tag?) or an extra index leaked in. Must be fixed; do not override. |
| `app deps OK` or `pip check` fails | vLLM's transitive installs clobbered an app dependency (most likely numpy). Inspect the pip log for what was upgraded; the `numpy>=1.24,<2` co-constraint must have held. |
| ccache "not found" message | **Not an error** (Req 1.12) — the build proceeds without compiler caching; expect a longer compile. |
| Rebuild recompiles everything despite ccache | Confirm ccache is on PATH inside the build and the cache dir persists between builds; vLLM's setup.py auto-wires ccache when present. |
| Two builds ran concurrently | Model versioning is corrupted — stop both, clean `greengrass-build/` + `custom-build/`, rebuild one at a time. |

### 1.2 `VLLM_ENABLE=0` variant (expectation only — optional to execute)

A build with the layers disabled:

```bash
cd src
BACKEND_DOCKERFILE=Dockerfile.jp7 docker-compose --profile tegra -f docker-compose.yaml build \
  --build-arg OS=24.04 --build-arg PYTHON_VERSION=3.11 \
  --build-arg VLLM_ENABLE=0 backend_tegra_gpu_enabled
```

**Expected**: the torch layer prints `torch layer skipped (VLLM_ENABLE=0)`,
the vLLM layer prints `vLLM layer skipped (VLLM_ENABLE=0)`, and the vLLM
import gate and dependency-consistency gate print their skip messages —
nothing vLLM-related (neither torch nor the wheel) is installed. At runtime
the capability probe (`importlib.util.find_spec("vllm")`) finds no wheel,
so the image runs the **pre-feature startup sequence**: no
`vLLM runtime manager started.` log line, no `/text-generation/*` routes
(404), no `VllmModel` feature-config entries — vision behavior unchanged.
Do **not** deploy this variant image for the remaining stages.

### 1.3 Deploy the new LocalServer component version

Deploy the freshly published `aws.edgeml.dda.LocalServer.arm64JP7` version
to the target Thor (portal Deployments page or your standard fleet flow) and
wait for the Greengrass deployment to complete.

**Expected on-device** (via SSH):

```bash
# container up and backend on cp311
docker exec <localserver-container> python3 --version   # Python 3.11.x
docker exec <localserver-container> python3 -c "import vllm; print(vllm.__version__)"   # 0.11.2...
docker exec <localserver-container> python3 -c "import torch; print(torch.__version__, torch.version.cuda)"   # 2.9.0+cu130 13.0
docker exec <localserver-container> ls /opt/vllm-wheels   # the staged vllm-*.whl
# startup log
docker logs <localserver-container> 2>&1 | grep -i "vllm"
#   -> "vLLM runtime manager started."
```

**Triage**: `import vllm` fails at runtime but passed at build → the
container is running a stale image; confirm the deployed component version.
`[VLLM RUNTIME STARTUP FAILED]` in the log → the app continues serving
vision workloads by design; capture the traceback (manager/loopback-server
startup) before proceeding.

### 1.4 In-container GPU checks (Req 2.8, 6.7)

Inside the deployed container, under the DDA interpreter:

```bash
docker exec <localserver-container> python3 -c \
  "import torch; print('cuda available:', torch.cuda.is_available(), '| device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'n/a')"
```

**Expected outcomes**

- `cuda available: True` with a Thor GPU device name — the on-hardware half
  of the build-time CUDA-13 gate (Requirement 2.8).
- The second in-container check — a vLLM engine load completing with the
  model reaching loaded status on the Thor GPU — is verified in **Stage L**
  below (the model's serving state reaching `ready`).

**Failure triage** — distinguish **container-image** problems from **host
driver-stack** problems:

| Symptom | Triage |
|---|---|
| `torch.cuda.is_available()` is `False` inside the container, but `nvidia-smi` (or `tegrastats`) works **on the host** | **Container-side**: the NVIDIA Container Runtime is not injecting the driver interfaces. Verify `docker info \| grep -i nvidia` shows the runtime registered (re-run `station_install/patch_docker_host_prereqs.sh` if not) and that the container was started by the component recipe (which requests the nvidia runtime). |
| `torch.cuda.is_available()` is `False` **and** the host GPU tools also fail | **Host driver stack**: the device is not flashed with JetPack 7.1/7.2 (r38.x) or the driver install is broken. An r35/r36 (JP5/JP6) host cannot run the JP7 image — reflash/use the matching component. Not an image defect. |
| `import torch` itself fails, or `torch.version.cuda` is not `13.x` | **Container-image**: the image predates this feature or was built with `VLLM_ENABLE=0`. Check the deployed component version and the build log's torch gate output. |
| `cuda available: True` here but the engine load in Stage L fails with a CUDA error | Mixed: capture the engine traceback. Kernel-image errors (`no kernel image is available`) → the wheel was compiled without `sm_110` (**image**: check `TORCH_CUDA_ARCH_LIST` in the build log). OOM → sizing (**configuration**: Stage L triage). |

---

## 2. Stage R — Register the LLMs (portal, Register_LLM_Flow)

Repeat for **both** models (Requirement 6.3).

1. In the portal, open the **Models** page and choose the **Register LLM**
   action (form header: *Register LLM (vLLM)*).
2. **Use Case**: select the target use case.
3. **Model Name**: `opt125m-smoke` (Smoke_Model) / `qwen25-7b-instruct`
   (Realistic_Model). **Model Version**: `1.0`.
4. **Model Source**: keep the *Hugging Face model ID* radio selected and
   enter the model ID exactly:
   - Smoke_Model: `facebook/opt-125m`
   - Realistic_Model: `Qwen/Qwen2.5-7B-Instruct`
5. Expand **Engine settings (optional)** (pre-filled with the documented
   defaults from `GET /api/v1/models/vllm/engine-spec`) and set exactly:
   - Smoke_Model: `gpu memory utilization` = `0.3`,
     `max model len` = `2048`; leave `dtype=auto`,
     `tensor parallel size=1`, `enforce eager` checked (true).
   - Realistic_Model: `gpu memory utilization` = `0.5`,
     `max model len` = `8192`; leave the other settings at their defaults.
6. Click **Register LLM**.

**Expected outcomes**

- Success alert: *"LLM registered successfully and is eligible for publish.
  Model ID: {training_id}"*; the API returned
  `201 {training_id, publish_eligible: true, labeling_steps: 0,
  training_steps: 0}` — no labeling, no training.
- The Models list shows the record with the **`LLM (vLLM)`** type badge and
  the `Registered` source badge.
- The model detail view shows Type `LLM (vLLM)` and a **Supported
  Architectures** section that initially reads *"Supported architectures are
  recorded when the model component is packaged and published."* (it
  populates in Stage P below and must then **include `arm64_jp7`**).

**Failure triage**

| Symptom | Triage |
|---|---|
| 400 with per-field findings | The form surfaces each `{field, value, reason}` finding inline. Fix the flagged field (typical: malformed HF ID — must be `{organization}/{model_name}`; `gpu_memory_utilization` outside `(0.0, 1.0]`; `max_model_len` < 1; unknown engine key). |
| Engine settings section fails to load | `GET /api/v1/models/vllm/engine-spec` failed; registration still applies documented defaults for omitted settings, but re-check portal API health before proceeding. |
| 403 | Portal user lacks the `DataScientist` role on the use case. |

---

## 3. Stage P — Package and publish (portal API)

The portal UI drives packaging/publish for trained/imported vision models;
for vLLM records use the same training APIs directly (the vLLM dispatch is
`packaging.py::is_vllm_record` → `package_vllm_component`, chaining into
`greengrass_publish.py`'s vLLM branch when `auto_triggered` is set). Repeat
per model, using the `training_id` returned at registration:

```bash
# package + auto-chain component creation (publish)
curl -s -X POST "$PORTAL_API/api/v1/training/$TRAINING_ID/package" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"auto_triggered": true}'
```

**Expected outcomes**

- `200` with `packaged_components` containing an entry per vLLM-supported
  architecture — including **`target: "jetson-xavier-jp7"`,
  `status: "packaged"`** alongside the pre-existing `jetson-xavier-jp6`
  entry — with `supported_architectures` including **both `arm64_jp6` and
  `arm64_jp7`**, and `component_package_s3` URIs under
  `s3://…/model_artifacts/model-{uuid}/…zip` in the Use_Case bucket
  (JP5 stays absent: `JP5_VLLM_ENABLED` is `False`; `arm64_jp4` never
  appears).
- Component creation is triggered (`component_creation_triggered: true`);
  shortly after, the record's `published_component` map carries
  `component_name: "model-vllm-{safe_model_name}"` (e.g.
  `model-vllm-opt125m-smoke`), `component_version: "1.0.0"`,
  `supported_architectures` **including `arm64_jp7`**, `runtime: "vllm"`,
  and a `component_arns` entry for `jetson-xavier-jp7`.
- The model detail view's **Supported Architectures** section now lists
  `arm64_jp7` (publish write-back preferred over packaged entries).
- The Greengrass component `model-vllm-…` version `1.0.0` exists in the
  use-case account.

**Failure triage**

| Symptom | Triage |
|---|---|
| `supported_architectures` lacks `arm64_jp7` | The deployed portal predates this feature's gating change — confirm the portal backend build includes the `vllm_supported_architectures()` / `VLLM_ARCH_TO_TARGET` / catalog `VLLM_ARCHITECTURES` updates. |
| Packaging 4xx/5xx naming a failing artifact/step | The vLLM packaging path is atomic and retryable: nothing was uploaded and the record state is unchanged. Fix the named step (usually S3 permissions on the use-case bucket) and re-POST. |
| `component_creation_triggered` true but no `published_component` | Publish is asynchronous and best-effort from packaging. Check the greengrass-publish Lambda logs; re-drive publish explicitly via `POST /api/v1/training/{id}/publish` with `component_name=model-vllm-{safe_name}`, `component_version=1.0.0`, `targets=["jetson-xavier-jp7"]`. |
| Publish rejects the component name/version | Names must keep the `model-` prefix and `N.0.0` versions are monotonic per record; a re-publish of the same record derives the next `N.0.0`. |

---

## 4. Stage D — Deploy to the Jetson Thor

Repeat per model (Smoke_Model first).

1. Portal → **Deployments** → create a deployment for the use case.
2. Target the Thor device (thing) — its recorded architecture must be
   `arm64_jp7`.
3. Add the component `model-vllm-{safe_model_name}` version `1.0.0`
   (the LocalServer dependency `aws.edgeml.dda.LocalServer.arm64JP7` is a
   HARD dependency of the component's recipe — the version deployed in
   Stage 1.3 must be present/compatible).
4. Submit.

**Expected outcomes**

- No client-side incompatibility warning (the CreateDeployment page runs a
  TS twin of the architecture gate, which now passes `arm64_jp7`); the
  backend gate passes and the deployment is submitted.
- Greengrass runs the component's startup script on the device: it unpacks
  the Triton_vLLM_Repository, downloads/stages weights, then
  `vllm_model_prep.py` validates the repository layout
  (`{model_name}/1/model.json` + `{model_name}/config.pbtxt` with
  `backend: "vllm"`), stages it atomically into
  `/aws_dda/dda_triton/vllm_model_repo/{model_name}`, and requests the load
  through the companion runtime's model-control endpoint — **no LocalServer
  restart**.
- Negative checks (optional): targeting a JP4/JP5 device instead is
  rejected pre-submit with `409 VLLM_ARCH_UNSUPPORTED` listing the offending
  (component, device) pairs — jp4 with reason `JP4_UNSUPPORTED`. Targeting
  the Thor with a vLLM component published **before** this feature (whose
  supported set lacks `arm64_jp7`) is likewise rejected with reason
  `ARCH_UNSUPPORTED` carrying the component's supported set.

**Failure triage**

| Symptom | Triage |
|---|---|
| `409 VLLM_ARCH_UNSUPPORTED` against the Thor | The device's recorded Target_Architecture is missing or not `arm64_jp7` (gate fails closed), or the record's `published_component.supported_architectures` lacks `arm64_jp7` (component published pre-feature — re-run Stage P against the updated portal) — re-check Stage P write-back and the device registration. |
| Greengrass deployment errors in the component lifecycle | `sudo tail -f /aws_dda/greengrass/v2/logs/model-vllm-*.log` — repository validation defects (layout/backend/model.json) name the defect and offending path and exit non-zero. |
| Weights download slow/fails (Qwen, ~15 GB) | Confirm device internet access to huggingface.co and free disk; the download budget dominates the deployment time for the Realistic_Model. |

---

## 5. Stage L — Load and READY propagation

> **Automated** by `harness/stages/test_20_vllm_textgen.py` (with a JP7
> device profile): expected vLLM models are brought to READY within
> `timeouts.vllm_ready_s`, with FAILED-with-reason surfaced verbatim. Manual
> steps below remain for triage.

After deployment, watch the model come up (per model). Reaching `ready`
here **is** the "engine load completes with the model reaching loaded
status on the Thor GPU" check of Requirement 6.7 (second half of
Stage 1.4's in-container checks):

```bash
# device — serving states known to the companion runtime
curl -s http://<device>:5000/text-generation/models
# -> [{"model_name": "...", "state": "loading"}]  then  "ready"

# device — feature-config status merge (existing model-status mechanism)
curl -s http://<device>:5000/feature-configurations | python3 -m json.tool
# -> an entry with "type": "VllmModel", the model name, "status": "READY"
```

**Expected outcomes**

- Smoke_Model: READY within a few minutes of the component completing
  (tiny weights; engine warm-up dominates).
- Realistic_Model: READY within the download + load budget (~15 GB weights;
  loading a 7B model with `gpu_memory_utilization=0.5` — a 60 GiB budget —
  on the 128 GB Thor). STAGED/LOADING states report as `LOADING` in the
  feature-config merge.
- READY propagates through the existing device model-status mechanisms
  (feature-config API, shadow sync) within 30 s of the runtime state change,
  so the portal-side device model status shows the `VllmModel` entry READY.

**Failure triage**

| Symptom | Triage |
|---|---|
| State `FAILED` with a CUDA OOM reason | Lower the memory/context settings: re-register (or re-deploy) with a smaller `gpu_memory_utilization` and/or `max_model_len` (e.g. Qwen at 0.4 / 4096). Failure is isolated per model — vision Triton and other vLLM models are untouched. |
| State `FAILED` with an import/engine construction error | **Container-image side**: check the wheel stack inside the container: `docker exec <c> python3 -c "import vllm, torch; print(vllm.__version__, torch.__version__, torch.version.cuda)"` (expect `0.11.2… / 2.9.0+cu130 / 13.0`). Note: JP7 sets **no** `VLLM_USE_V1` (unlike JP6) — v0.11.2 removed the V0 engine and the env var; the classic surface comes from the build script's `AsyncLLMEngine(AsyncLLM)` compatibility subclass. An `AttributeError` on a classic symbol (`shutdown_background_loop`, `from_engine_args`, `errored`) means the compat patch is missing from the wheel — a build-gate escape; rebuild and file it. |
| State `FAILED` with `no kernel image is available for execution on the device` | The wheel lacks `sm_110` kernels (**container-image**): the build's `TORCH_CUDA_ARCH_LIST`/`CUDA_ARCHITECTURES` did not reach the compile. Verify the build log and rebuild. |
| State `FAILED` with a driver/`cudaErrorInitializationError`-class failure and Stage 1.4's `torch.cuda.is_available()` also fails | **Host driver-stack side** — go back to Stage 1.4's triage (JetPack 7.1/7.2 flash, NVIDIA Container Runtime registration). |
| Stuck `LOADING`, runtime log silent | The load request may have raced the runtime start: `vllm_model_prep.py` load requests are best-effort; the model stays staged, and the backend's vLLM reconciler (`vllm-reconciler` thread, spec `vllm-model-reload-after-backend-restart`) re-drives every staged, not-explicitly-unloaded model on the next backend start with bounded retries (30/120/480 s backoff; exhaustion → `FAILED` with the retained reason, never LOADING forever). To resolve immediately instead of waiting for a restart, re-trigger the load: `curl -X POST http://localhost:<runtime_port>/v2/repository/models/<model>/load` from inside the device. |
| READY on-device but not visible portal-side | READY stall → status-merge logs: `docker logs <c> 2>&1 | grep -i "Failed to list vLLM models"` (the merge isolates vLLM failures from the vision feed); then check the shadow-sync path and portal device-status refresh. |
| `unknown` state for the model name | Name mismatch: the text-generation model name is the **sanitized** model name (the repository directory) — list `GET /text-generation/models` and use exactly the reported name in every later stage. |

---

## 6. Stage G — Non-streaming generate round trip

> **Automated** by `harness/stages/test_20_vllm_textgen.py`: a non-streaming
> generate against a READY model asserting non-empty `generated_text`, with
> latency/token metrics recorded in the results bundle. The error-contract
> spot-checks (422/409) below are manual-only.

Per model (READY required):

```bash
curl -s -X POST "http://<device>:5000/text-generation/<model_name>/generate" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "The three primary colors are", "max_tokens": 64, "temperature": 0.7, "top_p": 1.0}'
```

For the Realistic_Model use a realistic instruction prompt, e.g.
`"Summarize in two sentences why edge devices run quantized language models."`
with `max_tokens: 256`.

**Expected outcomes**

- `200 {"model_name": "<model_name>", "generated_text": "…"}` with non-empty
  text. opt-125m output will be low-quality (tiny model) — any coherent
  continuation counts; Qwen output should be an on-topic
  instruction-following answer.
- Omitted parameters get documented defaults
  (`max_tokens=256`, `temperature=0.7`, `top_p=1.0`).
- Spot-check the error contract: an empty prompt returns
  `422 {"findings": […]}` without invoking generation; a bogus model name
  returns `409` with state `unknown`.

**Failure triage**

| Symptom | Triage |
|---|---|
| `409 {state: loading/failed/unknown}` | The model left READY — go back to Stage L triage (the body carries the failure reason for `failed`). |
| `502 {model_name, reason}` | Backend generate failure after the transient-retry budget (default 2 retries). The reason is the vLLM engine error — OOM mid-generation → reduce `max_tokens`/`max_model_len` or memory settings. |
| `504 {model_name, timeout_seconds}` | Wall-clock timeout (default 120 s, env `TEXT_GEN_TIMEOUT_SECONDS`). Expected only for very long Qwen generations — raise the env or lower `max_tokens`. |
| `503` | The router is installed but no runtime — the capability probe/manager didn't start; re-check Stage 1.3. |

---

## 7. Stage S — SSE streaming session

> **Automated** by `harness/stages/test_20_vllm_textgen.py`: the SSE stream
> must deliver incremental `data: {"token": …}` chunks and terminate with a
> single `data: {"done": true}` event.

Per model:

```bash
curl -sN -X POST "http://<device>:5000/text-generation/<model_name>/generate-stream" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Count from one to ten:", "max_tokens": 64}'
```

**Expected outcomes**

- `Content-Type: text/event-stream`; a sequence of
  `data: {"token": "…"}` events arriving **incrementally in generation
  order** (visibly token-by-token for Qwen), terminated by exactly one
  `data: {"done": true}` event.
- Validation and READY-state failures surface as 422/409 **before** the
  stream starts (same contracts as Stage G).

**Failure triage**

| Symptom | Triage |
|---|---|
| Stream stops with a single `data: {"error": {"reason": …}}` event | Mid-stream engine failure: delivery stops, no retry, delivered tokens are not retracted (by design). Triage the reason like a Stage G `502`. |
| All tokens arrive at once | Ensure `-N` (no curl buffering) and no buffering proxy between the client and the device. |

---

## 8. Stage W — `llm_inference` workflow node (metadata verification)

> **Partially automated** by `harness/stages/test_30_workflows.py`: with the
> workflow already deployed, the harness enumerates workflows, runs one to
> observable output, and asserts the `llm.<nodeId>.generated_text` metadata
> when the definition carries an `llm_inference` node. Workflow authoring,
> packaging, and deployment (steps 1–3 below) remain manual portal steps.

Run against the **Realistic_Model** (a realistic prompt exercises template
rendering); optionally repeat with the Smoke_Model.

1. Portal → **Workflows** → create/edit a workflow for the use case; add an
   **LLM Inference** node (inference palette). In the node config panel the
   model select lists **only** vLLM records — pick the Qwen record. (A
   use case without vLLM records shows *"No vLLM models are registered for
   this use case"*.)
2. Set the node's `prompt_template` to reference upstream inference
   metadata, e.g.
   `"An inspection produced label {class_label}. Explain in one sentence what the operator should do."`
   and generation parameters (e.g. `max_tokens=128`).
3. Package the workflow for **`arm64_jp7`** — packaging validation must
   accept it with **zero `V6_LLM_ARCH_UNSUPPORTED` findings** (compiling
   for a non-vLLM architecture such as `arm64_jp4` must still error naming
   the node and architecture), deploy the `workflow-*` component to the
   Thor (the LLM-bearing workflow passes the same architecture gate), and
   trigger an execution that produces the upstream metadata the template
   references.
4. Inspect the run's inference metadata (device UI run/results view, or
   `GET http://<device>:5000/workflows/executions`).

**Expected outcomes**

- The run's metadata contains `llm.<nodeId>.generated_text` with non-empty
  generated text merged under the node's outcome
  (`metadata['llm'][nodeId] = {"generated_text": …}`) — Requirement 6.4.
- The LocalServer log shows
  `LLM inference binding (node <nodeId>) processed`.
- Downstream nodes (filters/conditionals/outputs/custom Python) can
  reference `{llm.<nodeId>.generated_text}`.

**Failure triage**

| Symptom | Triage |
|---|---|
| Packaging for `arm64_jp7` yields `V6_LLM_ARCH_UNSUPPORTED` | The portal's workflow_core catalog (layer or vendored copy) predates this feature — `VLLM_ARCHITECTURES` must include `ARCH_ARM64_JP7` so the node resolves its executor binding. Confirm the deployed portal build. |
| Node outcome is `{"error": "unresolved placeholder {name}"}` | The prompt template references a metadata field the run didn't produce; no API call was made (by design). Fix the template or the upstream node. |
| Node outcome is `{"error": "Text_Generation_API returned 404 …"}` | The node's HTTP invoker (`workflow_engine/output_bindings.py::TEXT_GENERATION_URL`) posts to `http://localhost:5000/text-generation/...`, matching the backend's registered route (no `/api` prefix). A 404 means the route was not registered (vLLM probe failed — recheck Stage G and the LocalServer startup log) or a stale image. |
| Node outcome error carries a 409/502/504 payload | Same triage as Stages G/L: model not READY, backend failure, or timeout (the node's client timeout is 130 s ≥ the API's 120 s so the API's own error arrives as the recorded reason). |
| Other bindings/nodes affected | Must not happen: a binding failure is recorded, never raised — remaining bindings and independent nodes continue. If the run itself fails, capture logs; that is a workflow-engine regression, not an LLM issue. |

---

## 9. Stage C — Vision-model coexistence

> **Automated** by `harness/stages/test_40_coexistence.py` (with vision
> lifecycle covered by `harness/stages/test_10_vision_models.py`): one vision
> model and one vLLM model held READY simultaneously through a completed
> generate, failing with both device-reported states on any departure. The
> `tegrastats` memory-pressure observation below is manual-only.

With the **Smoke_Model READY** (0.3 GPU fraction — a 36 GiB budget — leaves
ample headroom on the 128 GB Thor) and, if Stage L succeeded, the
Realistic_Model too:

1. Deploy (or confirm deployed) a previously supported **vision model**
   (ONNX or Triton-served — JP7 does not support DLR/Neo-compiled models)
   to the same device via the standard model component flow.
2. Verify both serve **simultaneously** — no unloading of either:

```bash
curl -s http://<device>:5000/feature-configurations | python3 -m json.tool
# -> the vision model entry AND the VllmModel entr(y/ies), all READY
```

3. Run a vision inference (execute a vision workflow or trigger the standard
   capture/inference path) **and** a Stage G generate call back-to-back.

**Expected outcomes**

- Both model types serve inference with neither unloaded: the vision model's
  inference request and the vLLM generate request each return a successful
  response while both models remain loaded (Requirement 6.5).
- The embedded vision Triton never sees the vLLM repository (it lives in the
  sibling `vllm_model_repo` directory, not `triton_model_repo`).
- Repeat the check with Qwen loaded (0.5 GPU fraction, 60 GiB budget) — on
  the 128 GB Thor both fit comfortably; watch `tegrastats` for memory
  pressure.

**Failure triage**

| Symptom | Triage |
|---|---|
| Vision model degrades/fails only while a vLLM model is loaded | GPU memory pressure: lower the vLLM `gpu_memory_utilization` (this is exactly why the Smoke_Model runs at 0.3). Capture `tegrastats` output for the report. |
| Vision model fails identically with `VLLM_ENABLE=0`-style conditions (no vLLM loaded) | Not a coexistence issue — a JP7 vision regression; escalate with the model type and logs (and check the model is not DLR-only, which JP7 does not support). |

---

## 10. Results record

For the harness-automated stages (5–9), a harness run's results bundle
(`harness-results/<device>-<timestamp>/results.json` + `junit.xml`) is the
machine-readable record — reference its path in the Notes column instead of
re-checking boxes by hand.

| # | Stage | Smoke_Model (`facebook/opt-125m`) | Realistic_Model (`Qwen/Qwen2.5-7B-Instruct`) | Notes |
|---|---|---|---|---|
| 1 | Image build (default args) | ☐ pass ☐ fail | — | build log ref + measured vLLM build duration (for the README) |
| 1.2 | `VLLM_ENABLE=0` variant (optional) | ☐ pass ☐ fail ☐ skipped | — | |
| 1.3 | LocalServer deploy + probe | ☐ pass ☐ fail | — | component version |
| 1.4 | In-container `torch.cuda.is_available()` | ☐ pass ☐ fail | — | |
| 2 | Register (badge + record) | ☐ pass ☐ fail | ☐ pass ☐ fail | training_ids |
| 3 | Package + publish (incl. `arm64_jp7`) | ☐ pass ☐ fail | ☐ pass ☐ fail | component names/versions |
| 4 | Deploy to Thor | ☐ pass ☐ fail | ☐ pass ☐ fail | deployment ids |
| 5 | READY propagation (engine load → loaded) | ☐ pass ☐ fail | ☐ pass ☐ fail | time-to-READY |
| 6 | Non-streaming generate | ☐ pass ☐ fail | ☐ pass ☐ fail | |
| 7 | SSE streaming | ☐ pass ☐ fail | ☐ pass ☐ fail | |
| 8 | Workflow `llm_inference` node | ☐ optional | ☐ pass ☐ fail | execution id |
| 9 | Vision coexistence | ☐ pass ☐ fail | ☐ pass ☐ fail | vision model used |

Tester: ____________  Device (thing name / JetPack): ____________
Image / component version: ____________  Date: ____________
