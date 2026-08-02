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

## Provisioning on a new system (one-time)

The sandbox image is an **environment-level artifact**: CDK creates the
ECR repository but does **not** build or push the image, so a freshly
deployed portal has no image for the Fargate task to pull and every
Workflow_Test run fails at task launch until the image is provisioned.
Provisioning is a manual, per-environment bootstrap done once (and
repeated only when the harness, Dockerfile, or vendored resources
change). The full sequence:

1. **Deploy the infrastructure.** `deploy-infrastructure.sh` (CDK)
   provisions the `TestRunnerStack`: the empty ECR repository
   `dda-workflow-test-sandbox`, the ECS/Fargate cluster and task
   definition, and the `Validate → Compile → RunSandbox →
   CollectResults` Step Functions state machine. The task definition
   pulls the tag from the `testSandboxImageTag` CDK context (default
   `latest`).
2. **Stage the DDA plugin `.so` set** into `test-sandbox/plugins/`
   (see "Staging the DDA plugins" above).
3. **Stage the DDA Triton conversion resources** into
   `test-sandbox/dda_triton_resources/` (see "Staging the DDA Triton
   conversion resources" above).
4. **Build, verify, push, and point `latest` at the image**
   (see "Build and push" below).
5. **Confirm** by running a workflow Test from the portal (Workflow
   Builder → Run test with a dataset selected).

After this one-time bootstrap, end users never create containers
directly: each Test run starts the state machine, whose `RunSandbox`
state launches an ephemeral Fargate task from the pushed image with
per-run environment overrides, then tears it down when the run finishes.
Because the task definition tracks a tag (not a digest), rolling out a
new image is just "push + repoint the tag" — no CDK or Lambda redeploy
(step 4 below).

## Build and push

The ECR repository `dda-workflow-test-sandbox` is created by the
test-runner stack; the task definition references the tag from the CDK
context value `testSandboxImageTag` (default `latest`).

**Build host requirements:** a native **x86_64** host with Docker and
AWS credentials that can push to ECR and call `ecr:PutImage` /
`ecr:BatchGetImage` in the portal's account. Building x86 under qemu
emulation on arm64 works but is impractically slow (multi-hour pip
installs). Ensure adequate free disk (the image is ~7 GB and the Triton
base stage adds several more) — `docker system prune` first if needed.

```bash
cd edge-cv-portal

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=us-east-1   # match the portal deployment region
ECR=${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com
REPO=dda-workflow-test-sandbox
TAG=latest             # must match the testSandboxImageTag CDK context

# 1. Build (native amd64)
docker build --platform linux/amd64 \
  -f test-sandbox/Dockerfile \
  -t ${ECR}/${REPO}:${TAG} .

# 2. Verify the image contains the harness + runtime deps before pushing
docker run --rm --entrypoint python3 ${ECR}/${REPO}:${TAG} \
  -c "import sklearn; print('sklearn', sklearn.__version__)"

# 3. Login + push
aws ecr get-login-password --region ${AWS_REGION} \
  | docker login --username AWS --password-stdin ${ECR}
docker push ${ECR}/${REPO}:${TAG}
```

### Rolling out a new image without a CDK redeploy

Push under a unique tag, then repoint `latest` (the tag the Fargate
task pulls) at that image with a **server-side retag**. Prefer this over
`docker push :latest`, which can upload a stale cached manifest:

```bash
UNIQUE_TAG=fix-decode-sklearn        # any unique tag
docker build --platform linux/amd64 -f test-sandbox/Dockerfile \
  -t ${ECR}/${REPO}:${UNIQUE_TAG} .
docker push ${ECR}/${REPO}:${UNIQUE_TAG}

MANIFEST=$(aws ecr batch-get-image --repository-name ${REPO} \
  --image-ids imageTag=${UNIQUE_TAG} --region ${AWS_REGION} \
  --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name ${REPO} --region ${AWS_REGION} \
  --image-tag latest --image-manifest "$MANIFEST"

# Confirm latest resolves to the new digest
aws ecr describe-images --repository-name ${REPO} --image-ids imageTag=latest \
  --region ${AWS_REGION} --query 'imageDetails[0].{digest:imageDigest,tags:imageTags}'
```

To deploy against a different tag instead of repointing `latest`:

```bash
cd infrastructure
npx cdk deploy --context testSandboxImageTag=${TAG} ...
```

### Build gotchas

- **`copy_file_range: no space left on device` at the
  `COPY --from=triton` step with plenty of free disk.** A BuildKit +
  overlay2 kernel bug on some hosts (notably snap-packaged Docker), not
  a real capacity problem. Build with a containerized BuildKit worker,
  whose own snapshotter avoids the host copy path:
  ```bash
  docker buildx create --name x86sandbox --driver docker-container --use
  docker buildx build --builder x86sandbox --platform linux/amd64 --load \
    -f test-sandbox/Dockerfile -t ${ECR}/${REPO}:${TAG} .
  ```
  The legacy builder (`DOCKER_BUILDKIT=0`) is **not** a workaround: it
  clears the Triton copy but cannot handle this Dockerfile's wildcard
  `COPY --from=<stage> ...glob*` lines and fails with "no source files
  were specified".
- Always run the step-2 verification before repointing `latest`; a
  stale cross-arch layer can otherwise ship silently.

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
