# Edge Test Harness

A pytest suite that validates a **live edge device** through its Backend_API
(default port 5000) from any host with HTTP access to it — a workstation, a
build server, or a CI job. It exercises what is **already deployed** on the
device (health, vision models, vLLM text generation, workflows, coexistence);
it never registers, packages, publishes, or deploys anything, and it restores
every model/workflow it started to its pre-run state, on success and failure
alike.

The harness replaces the verify-side stages of the manual runbook
`test/on-hardware/jp6_vllm_validation.md` (see the banner notes in that file);
the deploy-side portal steps remain manual.

```
harness/
├── README.md               # this file
├── devices.yaml.example    # sample configuration — copy to devices.yaml
├── conftest.py             # session wiring: config, client, gating, budget
├── pytest.ini              # markers + junitxml addopts
├── harnesslib/             # config, HTTP client, SSE parser, restoration, results
├── stages/                 # the on-device test stages (test_00 … test_40)
└── selftest/               # host-only unit + fake-device tests (no device needed)
```

Dependencies: `pytest`, `requests`, `pyyaml` — all already in the repo's test
tooling.

---

## Quick start

```bash
# 1. Describe your device once
cp test/on-hardware/harness/devices.yaml.example test/on-hardware/harness/devices.yaml
$EDITOR test/on-hardware/harness/devices.yaml

# 2. Run the suite against it
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages
```

That is the whole invocation. The run is non-interactive end to end; results
land in `harness-results/<device>-<UTC timestamp>/` (see
[Results bundle](#results-bundle)).

Exit behavior worth knowing:

- **Device unreachable** at its base URL → the run aborts during setup with
  exit code **2** and a single diagnostic naming the URL and the connection
  error (no per-test failure spam).
- **Capability not granted** by the device profile → those stages are
  **skipped** with a recorded reason naming the capability and the device.
- **Capability granted but absent on the device** (e.g. `vllm` declared but
  the device reports no `VllmModel` entries) → the stage **fails** with a
  `CapabilityMismatchError` contrasting the profile claim with the device
  observation, so a profile typo cannot silently reduce coverage.

---

## Configuration: `devices.yaml`

The harness reads `devices.yaml` next to this README (or the file named by
`DDA_HARNESS_CONFIG`). `DDA_HARNESS_DEVICE=<name>` selects an entry; with
exactly one device in the file the selection is implicit.

```yaml
devices:
  jp6-orinagx:
    base_url: http://localhost:5000       # tunnel-forwarded or LAN address
    profile:
      architecture: arm64_jp6             # x86_64 | arm64_jp4 | arm64_jp5 | arm64_jp6
      capabilities: [vllm, onnx_models, workflows]   # no dlr_models on JP6 (TRT10)
    credentials: env:DDA_HARNESS_TOKEN    # omit when local auth is disabled
    expected:
      vision_models:
        - model-rf-detr-seg-nano-jetson-xavier-jp6
        - model-yolo-test-jetson-xavier-jp6
      vllm_models:
        - opt125m-smoke
      workflows: []                        # empty = enumerate-only, no presence assertion
    timeouts:
      model_ready_s: 300
      vllm_ready_s: 900        # engine warmup + possible HF download
      generate_s: 120
      workflow_output_s: 180
      run_budget_s: 2400
```

Field reference:

| Field | Meaning |
| --- | --- |
| `base_url` | The device Backend_API root, e.g. `http://192.168.1.42:5000` or a tunnel-forwarded `http://localhost:5000`. Required. |
| `profile.architecture` | One of `x86_64`, `arm64_jp4`, `arm64_jp5`, `arm64_jp6`. Unknown values are rejected (fail closed). Required. |
| `profile.capabilities` | Any of `vllm`, `dlr_models`, `onnx_models`, `workflows`, `auth_enabled`. Unknown names are rejected. Stages gate on these (see below). |
| `credentials` | A credential **reference** (never a value) — see [Credentials](#credentials). Required only with `auth_enabled`. |
| `expected.vision_models` | Vision model names the device must report (asserted present). Empty list = enumerate-only. |
| `expected.vllm_models` | vLLM model names the device must report and bring to READY. Empty list = exercise whatever `VllmModel` entries the device reports. |
| `expected.workflows` | Workflow names the device must report. Empty list = enumerate-only. |
| `timeouts.*` | Per-stage bounds in seconds; defaults shown above. `run_budget_s` bounds the whole run — once exceeded, remaining tests fail with a budget-exceeded message instead of stalling on a hung device. |

Capability → stage gating:

| Capability | Gates |
| --- | --- |
| `vllm` | vLLM lifecycle + text generation (`test_20`) and coexistence (`test_40`). Grant on `arm64_jp6` (and JP5 targets where vLLM is enabled). |
| `dlr_models` | DLR/Neo vision-model assertions inside `test_10`. Do **not** grant on JP6 (TensorRT 10, no TRT8 `libnvinfer.so.8`) — those checks then skip with a recorded reason instead of failing. |
| `workflows` | Workflow execution stage (`test_30`). |
| `auth_enabled` | The login handshake at session start and the authenticated-surface check in `test_00`. |
| `onnx_models` | Declarative profile information (ONNX-backed vision expectations). |

### Environment overrides

Every field can be overridden per run — highest precedence wins
(environment > devices.yaml > built-in defaults):

| Variable | Overrides |
| --- | --- |
| `DDA_HARNESS_CONFIG` | Path to an alternate `devices.yaml` |
| `DDA_HARNESS_DEVICE` | Device entry selection |
| `DDA_HARNESS_BASE_URL` | `base_url` |
| `DDA_HARNESS_ARCHITECTURE` | `profile.architecture` |
| `DDA_HARNESS_CAPABILITIES` | `profile.capabilities` (comma-separated) |
| `DDA_HARNESS_CREDENTIALS` | `credentials` reference |
| `DDA_HARNESS_MODEL_READY_S`, `DDA_HARNESS_VLLM_READY_S`, `DDA_HARNESS_GENERATE_S`, `DDA_HARNESS_WORKFLOW_OUTPUT_S`, `DDA_HARNESS_RUN_BUDGET_S` | The matching `timeouts.*` entry |
| `DDA_HARNESS_EXPECTED_VISION_MODELS`, `DDA_HARNESS_EXPECTED_VLLM_MODELS`, `DDA_HARNESS_EXPECTED_WORKFLOWS` | The matching `expected.*` list (comma-separated) |

A device can be defined **entirely from the environment** (no file at all):

```bash
DDA_HARNESS_DEVICE=adhoc \
DDA_HARNESS_BASE_URL=http://192.168.1.42:5000 \
DDA_HARNESS_ARCHITECTURE=arm64_jp5 \
DDA_HARNESS_CAPABILITIES=dlr_models,onnx_models,workflows \
pytest test/on-hardware/harness/stages
```

With **no device configured at all**, the stages still collect and skip
cleanly (the configuration error becomes the skip reason) — this is what
keeps the harness safe to include in host-side CI collection.

## Credentials

Credentials are declared as **references, never values**, so no secret can
appear in configuration reprs, logs, or the results bundle:

- `env:VAR_NAME` — resolve from an environment variable at use time
- `file:~/path/to/token` — resolve from a file (`~` expanded) at use time

The resolved value may be either of:

- `username:password` — the harness performs the `/local-auth/login` flow and
  attaches the issued bearer token to the session;
- a ready-made **bearer token** (no colon) — attached directly.

Resolution happens only when the profile grants `auth_enabled`. A failing
handshake aborts the run fast with a diagnostic that names the credential
*reference*, never its value; the `Authorization` header is redacted from all
failure diagnostics.

## Reaching a remote device (SSH tunnel)

Reaching the device is the operator's concern — the harness only needs a
reachable `base_url`. For a device that is not on your LAN, forward its
backend port over SSH and point `base_url` at localhost:

```bash
# terminal 1: forward local port 5000 to the device's backend
ssh -N -L 5000:localhost:5000 <user>@<device-host>

# terminal 2: run against the forwarded port
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages
# (jp6-orinagx's base_url is http://localhost:5000 in devices.yaml.example)
```

If local port 5000 is taken, forward any free port (`-L 15000:localhost:5000`)
and set `DDA_HARNESS_BASE_URL=http://localhost:15000` for the run. A jump
host works the same way (`ssh -J bastion <user>@<device-host> …`).

## Stage and marker selection

The stages, in run order (module naming keeps health first):

| Module | `stage` marker | Capability gate | Validates |
| --- | --- | --- | --- |
| `test_00_health.py` | `health` | — (`auth_enabled` for the auth check) | `/system-health`, `/dda-component-status`, device identity, auth surface |
| `test_10_vision_models.py` | `vision_models` | DLR entries on `dlr_models` | expected vision models present, start → READY, restoration |
| `test_20_vllm_textgen.py` | `vllm_textgen` | `vllm` | expected vLLM models READY, non-streaming generate, SSE streaming, metrics |
| `test_25_vlm_image_generate.py` | `vlm_image_generate` | `vllm` (+ skips unless a Qwen VL / multimodal model is deployed) | image-carrying generate → `image_used: true` + non-empty answer; text-only generate unchanged |
| `test_30_workflows.py` | `workflows` | `workflows` | expected workflows present, run → observable output, `llm_inference` metadata |
| `test_40_coexistence.py` | `coexistence` | `vllm` | vision + vLLM READY simultaneously through a completed generate |

Selection uses standard pytest mechanisms — no test-code edits:

```bash
# one stage, by module
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages/test_00_health.py

# everything except capability-gated stages
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages -m "not capability"

# keyword selection (matches test/module names)
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages -k "vllm"

# health + vision only
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages -k "health or vision"
```

Registered markers (see `pytest.ini`): `capability(name)` — the test requires
that Capability_Flag; `stage(name)` — results-bundle grouping. Note pytest's
`-m` expressions match marker *names*, not arguments — use module paths or
`-k` to select an individual stage.

## Results bundle

Every run writes a comparable bundle to `--harness-output-dir` (default:
`harness-results/<device>-<UTC timestamp>/`, relative to the invocation
directory):

```
harness-results/jp6-orinagx-20250115-142530/
├── results.json    # schema_version 1 — see below
├── junit.xml       # standard JUnit XML (relocated from the pytest addopts path)
└── failures/       # present only on failure: one JSON capture per failing test
    └── 00-<test-nodeid>.json
```

`results.json` (schema_version **1**):

```json
{
  "schema_version": 1,
  "device": "jp6-orinagx",
  "profile": {"architecture": "arm64_jp6", "capabilities": ["onnx_models", "vllm", "workflows"]},
  "local_server_version": "…",
  "started_at": "…",
  "duration_s": 0.0,
  "exit_status": 0,
  "outcome": "passed",
  "stages": {
    "test_20_vllm_textgen": {
      "passed": 4, "failed": 0, "skipped": 0,
      "skip_reasons": [], "failures": []
    }
  },
  "metrics": {"vllm_generate_latency_s": 0.0, "vllm_stream_token_count": 0},
  "restoration_warnings": []
}
```

- **skip reasons** (missing capability, no device configured) flow into both
  `results.json` and the JUnit XML;
- **metrics** are informational (generate latency, token counts) — no
  thresholds asserted;
- **failure captures** under `failures/` carry the bounded (≤ 8 KB) failing
  request/response diagnostics with the `Authorization` header redacted;
- **restoration_warnings** records any teardown stop that failed — the device
  state to double-check by hand.

Custom output directory:

```bash
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages \
  --harness-output-dir=/tmp/orin-run-42
```

## Reference smoke run: jp6-orinagx

The reference example — the automated replacement for the verify-side stages
of `test/on-hardware/jp6_vllm_validation.md` — targets a 64 GB AGX Orin
(JetPack 6) with the smoke stack deployed:

- **Profile**: `arm64_jp6`, capabilities `[vllm, onnx_models, workflows]`
  (no `dlr_models` on JP6 — TRT10 devices skip DLR assertions with a recorded
  reason instead of failing).
- **Expected vision models**: `model-rf-detr-seg-nano-jetson-xavier-jp6`,
  `model-yolo-test-jetson-xavier-jp6`.
- **Expected vLLM models**: `opt125m-smoke` (the `facebook/opt-125m`
  Smoke_Model registered at `gpu_memory_utilization=0.3`, which deliberately
  leaves GPU headroom for the coexistence stage).
- **Timeouts**: `vllm_ready_s: 900` — engine warm-up plus a possible
  Hugging Face weights download dominate the first READY.

```bash
# one-time: tunnel to the Orin (or use its LAN address in devices.yaml)
ssh -N -L 5000:localhost:5000 <user>@<orin-host> &

# the smoke run
DDA_HARNESS_DEVICE=jp6-orinagx pytest test/on-hardware/harness/stages
```

A green run means: backend healthy and identity recorded; both vision models
present and READY; `opt125m-smoke` READY, answering a non-streaming generate
with non-empty text and a token-by-token SSE stream terminated by a `done`
event; deployed workflows enumerated (and any expected ones executed to
observable output); vision + vLLM READY simultaneously through a completed
generate. Everything the harness started is stopped again on the way out.

## Harness selftests (no device required)

The harness's own correctness is guarded by host-side tests — unit tests plus
an in-process fake device that the real stages run against:

```bash
pytest test/on-hardware/harness/selftest
```

These run in ordinary repo CI and need no hardware.
