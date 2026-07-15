# Workflow Test Sandbox (task 11.3)

The container image and test harness behind the Workflow_Test_Runner's
RunSandbox state (Requirements 12.5, 12.6, 12.7, 12.10). The Fargate
infrastructure, state machine, and step Lambda live in
`../infrastructure/lib/test-runner-stack.ts` (task 11.2).

## What the harness does

`harness/` is a small Python package (`python3 -m harness` is the
container entrypoint) that:

1. Downloads the Compiled Pipeline Document (`COMPILED_DOCUMENT_S3_KEY`)
   and the selected Test_Dataset (`DATASET_S3_PREFIX`) from the portal
   artifacts bucket.
2. Stages the dataset as a sequential JPEG frame set and resolves the
   `{dataset_location}` placeholder the simulation compiler leaves in
   dataset-fed source elements (`multifilesrc`) — Requirement 12.5.
3. Renders the document into a `gst-launch`-style string exactly as
   LocalServer does — `" ! "` joins, `t0. ! queue ! ...` tee branches,
   `... ! f0.` funnel links (design section 5) — and executes it via
   `Gst.parse_launch`, mirroring `GstPipelineManager.run_pipeline`
   (bus watch, watchdog, `is_anomalous`/`confidence` tag parsing).
4. Executes simulation executor bindings: `recording_*` bindings record
   the would-be actuation (parameters + triggering inference metadata)
   instead of contacting any physical or device-local endpoint;
   `inference_filter` bindings are evaluated over the inference metadata
   and gate downstream recorders — Requirement 12.6.
5. Flushes per-node results `{nodeId, status, outputs, stubActivity,
   error}` to `RESULTS_S3_KEY` incrementally after every update, so a
   mid-run failure retains all prior results and identifies the failing
   node — Requirements 12.7, 12.10. Element-level bus errors map back to
   node ids via the document's `nodeId` tags.

Exit code 0 lets the CollectResults step finalize the run from the
flushed results; exit code 1 routes through the state machine's failure
recorder, which marks the run failed while the flushed partial results
are retained untouched.

### Environment contract

Set by the RunSandbox container overrides (test-runner-stack.ts):
`TEST_RUN_ID`, `WORKFLOW_ID`, `USECASE_ID`, `ARTIFACTS_BUCKET`,
`DATASET_S3_PREFIX`, `RESULTS_S3_KEY`, `COMPILED_DOCUMENT_S3_KEY`,
`SIMULATED_INFERENCE`, `STAGED_MODELS` (the model staging manifest —
see "Staging the DDA Triton conversion resources" below), plus
`TEST_RUNS_TABLE` / `WORKFLOWS_S3_PREFIX` from the task definition.
The task role only has portal-artifacts S3 and TestRuns table access —
no Greengrass or device permissions (Requirement 12.9).

## Simulate mode (`HARNESS_MODE=simulate`)

The same image also serves the Custom Node Designer's Plugin_Simulator
(custom-node-designer Requirements 7.2, 7.3, 7.6). Setting
`HARNESS_MODE=simulate` switches the entrypoint to `harness/simulate.py`,
which exercises exactly one custom-node plugin element instead of a
Compiled Pipeline Document:

1. Stages the plugin's x86_64 `.so` (`PLUGIN_S3_KEY`) into the task's
   plugin scan directory, prepended to `GST_PLUGIN_PATH` before
   GStreamer initializes.
2. Stages the sample input frames (`DATASET_S3_PREFIX`) with the same
   dataset staging as test runs and uploads each staged input frame
   under the run's `frames/` prefix.
3. Renders and executes the single-plugin pipeline
   `multifilesrc ! jpegparse ! jpegdec ! videoconvert !
   <element> <declared-params> ! videoconvert ! jpegenc ! appsink` via
   `Gst.parse_launch`. The appsink is the frame capture + metadata tap:
   each output frame is uploaded and its result record
   `{frameIndex, inputRef, outputRef, metadata}` is flushed to
   `RESULTS_S3_KEY` incrementally, so partial results survive a mid-run
   plugin failure.
4. Abnormal plugin termination stays contained to the task: bus errors
   and the plugin's captured stderr are recorded in the flushed results
   document; a hard native crash kills only the Fargate task and the
   simulator state machine's catch marks the run failed with the
   flushed partial results retained.

### Simulate-mode environment contract

Set by the RunSandbox state of the node-designer simulator state
machine (`node-designer-stack.ts`, task 8.2): `SIMULATION_RUN_ID`,
`ARTIFACTS_BUCKET`, `DATASET_S3_PREFIX`, `RESULTS_S3_KEY`,
`PLUGIN_S3_KEY` (the plugin `.so` staged under the run's prefix),
`ELEMENT_FACTORY` (the plugin's element name), `ELEMENT_PARAMETERS`
(JSON `{parameter: value}`, optional), plus optional `PLUGIN_SCAN_DIR`
and `PIPELINE_TIMEOUT_SEC` (default 270 s — under the state machine's
5-minute limit so the harness flushes the timeout failure itself). The
task role is limited to the run's S3 prefix: no Plugin_Library write
path, no other Use_Case data (7.2).

## Image contents

- Ubuntu 22.04 (x86_64) with GStreamer 1.20 + Python GI bindings
- CPU Triton copied from the NVIDIA Triton server image at
  `/opt/tritonserver` (the `server-path` emltriton uses; override the
  release with `--build-arg TRITON_IMAGE=...`)
- The DDA GStreamer plugin set staged into `plugins/` (below)
- Vendored `workflow_core` (same package as the Lambda layer and
  LocalServer) and the harness under `/opt/harness`

### Staging the DDA plugins

The proprietary DDA GStreamer elements (`emltriton`, `emexifextract`,
`emlcapture`, `emoutputevent`, ...) are delivered as prebuilt x86_64
`.so` artifacts — the same edgemlsdk artifact set `src/backend/Dockerfile`
copies in at build time. Stage them before building:

```bash
cp /path/to/edgemlsdk/x86_64/libgst*.so edge-cv-portal/test-sandbox/plugins/
```

The image builds with `plugins/` empty (only workflows using DDA elements
then fail at parse time with a per-node error); full containerized
integration tests are task 11.8.

### Staging the DDA Triton conversion resources

Test runs execute model inference for real: the portal stages each
model_inference node's Greengrass model artifact zip under the run's
prefix (`models/<modelName>.zip`, manifest in the `STAGED_MODELS` env),
and `harness/model_staging.py` converts it into the Triton
python-backend repository layout the device-side
`src/backend/dda_triton/model_convertor.py` produces (the zip holds the
raw runtime artifact + `manifest.json`, not a ready model repo). The
conversion needs the python-backend templates and the app packages they
import; stage them before building, like the plugins:

```bash
cd <repo root>
cp src/backend/dda_triton/resources_for_copy/lfv_model_template.py \
   src/backend/dda_triton/resources_for_copy/inference_runtimes.py \
   src/backend/dda_triton/resources_for_copy/marshal_for_capture_template.py \
   src/backend/dda_triton/resources_for_copy/ensemble_model \
   edge-cv-portal/test-sandbox/dda_triton_resources/
cp -r src/backend/lyra_anomalies_mask_utils \
      src/backend/lyra_science_processing_utils \
      edge-cv-portal/test-sandbox/dda_triton_resources/
```

The image builds with the directory empty; staging a model then fails
at runtime with a clear per-node error naming the missing resources.

## Build and push

The ECR repository `dda-workflow-test-sandbox` is created by the
test-runner stack; the task definition references the tag from the CDK
context value `testSandboxImageTag` (default `latest`).

```bash
cd edge-cv-portal

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=us-east-1   # match the portal deployment region
ECR=${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com
TAG=latest             # must match the testSandboxImageTag CDK context

docker build --platform linux/amd64 \
  -f test-sandbox/Dockerfile \
  -t ${ECR}/dda-workflow-test-sandbox:${TAG} .

aws ecr get-login-password --region ${AWS_REGION} \
  | docker login --username AWS --password-stdin ${ECR}

docker push ${ECR}/dda-workflow-test-sandbox:${TAG}
```

To deploy against a different tag:

```bash
cd infrastructure
npx cdk deploy --context testSandboxImageTag=${TAG} ...
```

## Tests

Unit tests cover the harness's pure logic — launch-string rendering,
element-name → nodeId mapping, results assembly and incremental flush,
dataset staging plans, condition evaluation, and recording-stub
execution — and run without GStreamer or AWS:

```bash
cd edge-cv-portal/test-sandbox
python3 -m pytest tests -q
```

Containerized integration tests (task 11.8) live in `tests/integration/`
and are marked `@pytest.mark.integration`. They run the real harness
(`python3 -m harness`) inside the container image against a moto S3
server: an end-to-end sample workflow compiled by `workflow_core`
(`simulation=True`, x86_64) over a small generated Test_Dataset (12.5),
timeout behavior with `PIPELINE_TIMEOUT_SEC=2` (12.13), and
no-Greengrass assertions — behavioral (boto3 service recording during a
run) plus static source checks of the harness and the test-runner stack
(12.9). Each test skips cleanly with a reason when a prerequisite
(Docker daemon, `moto[server]`, Pillow) is missing.

By default the tests build a slim image variant — the Dockerfile's
runtime layers without the multi-gigabyte CPU Triton stage and without
the proprietary DDA plugin `.so` set, neither of which is available in
CI. Set `SANDBOX_IT_IMAGE` to a fully built `dda-workflow-test-sandbox`
image to run against the real image. Documented limitation: emltriton /
CPU-Triton inference is therefore not covered end-to-end; the sample
workflow uses stock GStreamer elements, which exercises the same 12.5
execution semantics (dataset staging, `{dataset_location}` resolution,
`Gst.parse_launch` execution, incremental results flushing).

```bash
cd edge-cv-portal/test-sandbox
python3 -m pytest tests/integration -q          # integration only
python3 -m pytest tests -q -m "not integration" # unit/property only
```
