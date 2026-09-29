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
      architecture: arm64_jp6             # x86_64 | arm64_cpu | arm64_jp5 | arm64_jp6 | arm64_jp7
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
      continuous_window_s: 30  # stream stage: continuous workflow sampling window
```

Field reference:

| Field | Meaning |
| --- | --- |
| `base_url` | The device Backend_API root, e.g. `http://192.168.1.42:5000` or a tunnel-forwarded `http://localhost:5000`. Required. |
| `profile.architecture` | One of `x86_64`, `arm64_cpu`, `arm64_jp5`, `arm64_jp6`, `arm64_jp7`. Unknown values are rejected (fail closed). Required. |
| `profile.capabilities` | Any of `vllm`, `dlr_models`, `onnx_models`, `workflows`, `auth_enabled`, `stream_cameras`. Unknown names are rejected. Stages gate on these (see below). |
| `credentials` | A credential **reference** (never a value) — see [Credentials](#credentials). Required only with `auth_enabled`. |
| `expected.vision_models` | Vision model names the device must report (asserted present). Empty list = enumerate-only. |
| `expected.vllm_models` | vLLM model names the device must report and bring to READY. Empty list = exercise whatever `VllmModel` entries the device reports. |
| `expected.workflows` | Workflow names the device must report. Empty list = enumerate-only. |
| `expected.stream_*`, `expected.continuous_workflow` | Optional inputs of the stream camera stage — see [Stream cameras stage](#stream-cameras-stage). |
| `timeouts.*` | Per-stage bounds in seconds; defaults shown above. `run_budget_s` bounds the whole run — once exceeded, remaining tests fail with a budget-exceeded message instead of stalling on a hung device. |

Capability → stage gating:

| Capability | Gates |
| --- | --- |
| `vllm` | vLLM lifecycle + text generation (`test_20`) and coexistence (`test_40`). Grant on `arm64_jp6` (and JP5 targets where vLLM is enabled). |
| `dlr_models` | DLR/Neo vision-model assertions inside `test_10`. Do **not** grant on JP6 (TensorRT 10, no TRT8 `libnvinfer.so.8`) — those checks then skip with a recorded reason instead of failing. |
| `workflows` | Workflow execution stage (`test_30`). |
| `stream_cameras` | RTSP/RTMP stream camera stage (`test_35`). Grant on devices whose LocalServer has the stream camera feature; a granted device answering 404 on `/streams/capabilities` fails the stage with `CapabilityMismatchError`. |
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
| `DDA_HARNESS_MODEL_READY_S`, `DDA_HARNESS_VLLM_READY_S`, `DDA_HARNESS_GENERATE_S`, `DDA_HARNESS_WORKFLOW_OUTPUT_S`, `DDA_HARNESS_RUN_BUDGET_S`, `DDA_HARNESS_CONTINUOUS_WINDOW_S` | The matching `timeouts.*` entry |
| `DDA_HARNESS_EXPECTED_VISION_MODELS`, `DDA_HARNESS_EXPECTED_VLLM_MODELS`, `DDA_HARNESS_EXPECTED_WORKFLOWS`, `DDA_HARNESS_EXPECTED_STREAM_URLS` | The matching `expected.*` list (comma-separated) |
| `DDA_HARNESS_EXPECTED_STREAM_SECURE_URL`, `DDA_HARNESS_EXPECTED_STREAM_CREDENTIALS`, `DDA_HARNESS_EXPECTED_STREAM_WORKFLOW`, `DDA_HARNESS_EXPECTED_CONTINUOUS_WORKFLOW` | The matching single-valued `expected.*` entry (an empty value unsets it) |

`expected.stream_failures` is a mapping and is **file-only**: setting
`DDA_HARNESS_EXPECTED_STREAM_FAILURES` is rejected.

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
| `test_35_stream_cameras.py` | `stream_cameras` | `stream_cameras` (+ each check skips when its `expected.*` input is unset) | stream capabilities, RTSP/RTMP connection test + preview + health, credentials, failure categories, triggered and continuous stream workflows |
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
  request/response diagnostics with the `Authorization` header redacted
  (and stream camera passwords scrubbed; request bodies are never captured);
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

## Stream cameras stage

`test_35_stream_cameras.py` checks the RTSP/RTMP stream camera feature end to
end through the Backend_API. It runs only when the profile grants
`stream_cameras`, and each check skips, naming the missing key, when its input
is not configured:

| Check | Input | Passes when |
| --- | --- | --- |
| Capabilities | — | `/streams/capabilities` reports RTSP and RTMP ingest and a software decoder for H.264 and H.265. Each codec's hardware decoder and the PyAV, FFmpeg and GStreamer versions are recorded as metrics. |
| Stream URLs | `stream_urls` | Each URL, created as an Image_Source with the `auto` decoder policy, passes the connection test, previews an image and reports `streaming`. Codec, resolution, source rate and decoder are recorded per URL (`stream_source[<url>]`). All failing URLs are reported in one message. |
| Credentials | `stream_secure_url`, `stream_credentials` | The credentialed source connects; no connection-test, GET or stream-health response carries the password or URL user information; after a wrong password is PATCHed, the connection test fails with `authentication_failed`. |
| Failure categories | `stream_failures` | Each URL's connection test answers `ok: false` with exactly its category. All mismatches are reported in one message. |
| Triggered run | `stream_workflow` | A trigger of its highest registered version completes within `timeouts.workflow_output_s`, and the run metadata has a `stream` entry with `seq` and `acquiredAtMs`. |
| Continuous run | `continuous_workflow` | Over `timeouts.continuous_window_s` the workflow stays `running` with `effectiveFps > 0` and a rising `counters.completed`; a pause reports `paused` and no run starts or completes; a resume reports `running`. A workflow found paused is left alone and both checks skip. |

The inputs, all optional and under `expected`:

| Key | Meaning |
| --- | --- |
| `stream_urls` | Credential-free `rtsp://`, `rtsps://`, `rtmp://` or `rtmps://` URLs that must connect. The Image_Source type comes from the scheme. |
| `stream_secure_url` | A URL that needs credentials. |
| `stream_credentials` | A credential reference (`env:VAR` or `file:path`) resolving to `username:password` for `stream_secure_url`. |
| `stream_failures` | File-only mapping of connection-test failure category to a URL or a list of URLs. Categories are checked against the device vocabulary (`not_found`, `unsupported_codec`, `tls_verification_failed`, `authentication_failed`, `decoder_unavailable`, `timeout`, `network_error`, …). |
| `stream_workflow` | workflowId of an installed on_trigger stream workflow. |
| `continuous_workflow` | workflowId of an installed continuous stream workflow. |

A configured URL with user information (`user:pass@`) or a secret query
parameter (`pass=`, `token=`, …) is rejected at load time, and the error does
not echo it. Use a distinctive test password of 8 or more characters that is
not part of any URL or name the device returns (not `secure`, say): the leak
check matches it as a substring anywhere in the responses. The password is
scrubbed from failure diagnostics and never written to results.

Example against a MediaMTX test server (RTSP on 8554, RTSPS on 8322 with a
self-signed certificate, RTMP on 1935, RTMPS on 1936; `nosuchpath` is
readable but never published):

```yaml
devices:
  jp7-thor:
    base_url: http://localhost:5000
    profile:
      architecture: arm64_jp7
      capabilities: [onnx_models, workflows, stream_cameras]
    expected:
      stream_urls:
        - rtsp://192.168.88.237:8554/h264
        - rtsp://192.168.88.237:8554/h265
        - rtmp://192.168.88.237:1935/live/h264
        - rtmp://192.168.88.237:1935/live/h265      # Enhanced RTMP
      stream_secure_url: rtsp://192.168.88.237:8554/secure
      stream_credentials: env:DDA_HARNESS_STREAM_SECRET  # "username:password"
      stream_failures:
        not_found: rtsp://192.168.88.237:8554/nosuchpath
        unsupported_codec: rtsp://192.168.88.237:8554/vp9
        tls_verification_failed:                    # self-signed certificates
          - rtsps://192.168.88.237:8322/h264
          - rtmps://192.168.88.237:1936/live/h264
      stream_workflow: <workflowId of an on_trigger stream workflow>
      continuous_workflow: <workflowId of a continuous stream workflow>
    timeouts:
      continuous_window_s: 30
```

State_Restoration: every Image_Source the stage creates is deleted when its
check finishes, and by restoration at teardown if the check was cut short.
The continuous pause is recorded before it is issued, so it is resumed however
the check ends. Triggered runs leave their registration untouched.

Out of scope: the soak sampling of task 25.3 (backend and worker RSS every
minute, `docker inspect` RestartCount) needs shell access to the device, which
this HTTP-only harness does not have. Sample those on the device alongside a
long run.

## Harness selftests (no device required)

The harness's own correctness is guarded by host-side tests — unit tests plus
an in-process fake device that the real stages run against:

```bash
pytest test/on-hardware/harness/selftest
```

These run in ordinary repo CI and need no hardware.
