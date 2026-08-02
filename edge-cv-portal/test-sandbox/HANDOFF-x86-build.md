# Handoff: build & deploy the workflow test-sandbox image on x86

This branch carries a fix for cloud **workflow Test runs** (Workflow_Builder →
Test) that fail with `The sandbox container exited with code 1`. The code +
unit tests are complete and green; the only remaining step is to **build the
`dda-workflow-test-sandbox` container image on a real x86_64 host and push it
to ECR**. The originating environment is arm64 and can only build x86 under
qemu emulation, which is impractically slow (scipy/scikit-learn install ran
>17 min and cross-arch builds intermittently produced stale image digests).

Target account: **164152369890**, region **us-east-1**.
ECR repo: **`dda-workflow-test-sandbox`**.

---

## TL;DR for the x86 builder

```bash
# 0. Prereqs (see "Prerequisites" below): stage plugins + triton resources,
#    have AWS creds for account 164152369890, docker + amd64 support.
cd edge-cv-portal

AWS_REGION=us-east-1
ECR=164152369890.dkr.ecr.${AWS_REGION}.amazonaws.com
REPO=dda-workflow-test-sandbox
TAG=fix-decode-sklearn        # any unique tag

# 1. Build (native amd64 — should take a few minutes, not hours)
docker build --platform linux/amd64 -f test-sandbox/Dockerfile -t ${ECR}/${REPO}:${TAG} .

# 2. VERIFY the image actually contains the two fixes before pushing
docker run --rm --entrypoint grep ${ECR}/${REPO}:${TAG} -n \
  "failing_node in staged_by_node or failing_node is None" /opt/harness/harness/harness.py
docker run --rm --entrypoint grep ${ECR}/${REPO}:${TAG} -n \
  "_normalize_to_baseline_jpeg" /opt/harness/harness/dataset.py
docker run --rm --entrypoint python3 ${ECR}/${REPO}:${TAG} -c "import sklearn; print('sklearn', sklearn.__version__)"

# 3. Login + push
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR}
docker push ${ECR}/${REPO}:${TAG}

# 4. Point `latest` at the pushed image. The Fargate task definition pulls
#    `latest` at launch, so this is what makes the next Test run use it.
#    Prefer the server-side retag (reliable) over `docker push :latest`:
MANIFEST=$(aws ecr batch-get-image --repository-name ${REPO} \
  --image-ids imageTag=${TAG} --region ${AWS_REGION} \
  --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name ${REPO} --region ${AWS_REGION} \
  --image-tag latest --image-manifest "$MANIFEST"

# 5. Confirm
aws ecr describe-images --repository-name ${REPO} --image-ids imageTag=latest \
  --region ${AWS_REGION} --query 'imageDetails[0].{digest:imageDigest,tags:imageTags}'
```

Then re-run the **cookies folder** workflow Test in the portal and confirm it
passes. See "Verification" below.

---

## Prerequisites on the x86 builder

The image build needs two sets of artifacts that are **NOT in git** (see
`test-sandbox/README.md`). Stage them into the build context first, exactly as
on the original host:

1. **DDA GStreamer plugin `.so` set** into `edge-cv-portal/test-sandbox/plugins/`
   (the x86_64 edgemlsdk artifacts: `libgstemltriton.so`, `libgstemlcapture.so`,
   `libgstemexifextract.so`, ...). The build's verification gate requires
   `libgstemltriton.so` and `libgstemlcapture.so` to resolve their libs.
2. **DDA Triton conversion resources** into
   `edge-cv-portal/test-sandbox/dda_triton_resources/`
   (`lfv_model_template.py`, `inference_runtimes.py`,
   `marshal_for_capture_template.py`, `ensemble_model`, plus the `lyra_*`
   packages). Copy commands are in `test-sandbox/README.md`.

Also: Docker with linux/amd64, and AWS credentials that can push to ECR and
call `ecr:PutImage`/`BatchGetImage` in account 164152369890.

Optional sanity check before building — run the harness unit tests on x86:
```bash
cd edge-cv-portal/test-sandbox && python3 -m pytest -q -m "not integration"
# Expect all pass except the 2 known-environmental ones:
#   tests/integration/test_no_greengrass_static.py (docstring contains "greengrass")
#   tests/integration/test_gst_introspect_e2e.py (needs full GStreamer)
```

---

## What already IS deployed (do NOT redo)

These were deployed from the original host to account 164152369890/us-east-1:

- **Portal Lambda** (`EdgeCVPortalComputeStack`): `workflow_testing.py` +
  `workflow_model_staging.py` — model staging is now **best-effort**; a model
  that cannot be staged no longer fails the run (it is omitted from
  `STAGED_MODELS` and reported in a new `STAGING_FALLBACKS` state-machine
  input).
- **CDK** (`EdgeCVPortalTestRunnerStack`): the RunSandbox container override now
  passes the `STAGING_FALLBACKS` env var.
- **Sandbox image** currently on ECR `latest` = digest
  `sha256:60bbc876d7064b6e9359ba1430e583e58bc880acbb0fd68b81615e32d581e963`
  (also tagged `fix-fallback`). That image has the **attribution + model-load
  fallback** fixes but **NOT** the two fixes on this branch (dataset baseline-JPEG
  normalization and scikit-learn). Your new build supersedes it.

So the x86 builder only needs to **build + push the image** and repoint `latest`.
No CDK/Lambda/frontend deploy is required.

---

## What this branch changes (and why)

Confirmed from live CloudWatch logs (`/dda-portal/workflow-test-sandbox`), the
cookies run now gets past the earlier bugs and fails on two *new*, real issues.
Both are fixed here:

### A) Dataset frames crash stock `jpegdec` (the current blocker)
Log: `jpegdec: Failed to decode JPEG image. Decode error #21: Improper call to
JPEG library in state 205`. The sim source chain
`multifilesrc ! jpegparse ! jpegdec` only decodes **baseline** JPEGs; the
dataset contained non-baseline (progressive/CMYK/EXIF/etc.) images, which the
old staging copied through verbatim.

Fix — `test-sandbox/harness/dataset.py`: staging now normalizes **every** frame
through Pillow to a clean baseline RGB JPEG (`convert("RGB")`,
`progressive=False`, no EXIF), not just PNGs. Unreadable images are skipped with
a warning; an all-unreadable dataset raises `ValueError`. Tests updated in
`tests/test_dataset.py` (+ `test_custom_plugins.py` seed images).

### B) Staged model can't load — `ModuleNotFoundError: No module named 'sklearn'`
The DDA model python backend (`lyra_science_processing_utils` score calibrator)
imports scikit-learn, which the image lacked, so `emltriton`/Triton couldn't
load the model and the pipeline never reached PLAYING.

Fix — `test-sandbox/Dockerfile`: added `scikit-learn==1.3.2` to the runtime pip
install. **Caveat:** the calibrator may have been pickled with a different
sklearn minor; if you see a version-mismatch warning or a pickle load error in
the logs, pin the version the device/edgemlsdk uses instead of 1.3.2. Even if
real inference still can't load, it is **non-fatal** — the harness falls back to
the injected simulated outcome (best-effort inference) and the run should still
pass.

### Already-fixed on the prior image (context)
- `harness.py::run_gst_pipeline`: on a synchronous `set_state(PLAYING)==FAILURE`
  it drains the bus for the real element error instead of the generic message.
- `harness.py::execute`: failures attribute to the owning node, else a run-level
  error — never defaulted onto the source node.
- Model-load fallback: a staged model that fails to reach PLAYING (including the
  unattributable `element=None` case) reverts to the sim stub, re-runs once, and
  injects the simulated outcome. `results.py` records `inferenceMode`
  (`real`/`simulated`) + `fallbackReason`; the frontend TestPanel surfaces it.

---

## Verification (after push + retag `latest`)

1. In the portal, open the **cookies folder binary** workflow and click
   **Run test** with a dataset selected.
2. Expected: the run **succeeds**. The model inference node shows
   `Inference simulated` with a fallback reason if sklearn/the model still can't
   load on CPU, or `Inference: real model` if it loads. The Folder Source is no
   longer marked failed.
3. If it still fails, read the run's results + logs:
   ```bash
   # newest sandbox log stream
   SG=/dda-portal/workflow-test-sandbox
   S=$(aws logs describe-log-streams --log-group-name $SG --region us-east-1 \
       --order-by LastEventTime --descending --max-items 1 \
       --query 'logStreams[0].logStreamName' --output text)
   aws logs get-log-events --log-group-name $SG --log-stream-name "$S" \
       --region us-east-1 --start-from-head --query 'events[].message' --output text
   ```
   With these fixes the log will name the specific failing element/module
   rather than the generic "failed to change state to PLAYING".

---

## Known gotchas observed on the arm64 host (should not occur on native x86)

- Cross-arch (`--platform linux/amd64` on arm64) `docker build` sometimes reused
  a stale harness COPY layer and emitted an **identical image digest** despite
  source changes. Always run the step-2 `grep`/`import` **verification** before
  pushing.
- `docker push …:latest` returned a **stale cached manifest** (RepoDigests
  empty) and did not upload the new image. Pushing under a fresh tag worked;
  hence the ECR **server-side retag** (`batch-get-image` → `put-image`) for
  `latest` in the TL;DR. On a clean native x86 Docker this is likely
  unnecessary, but it's the reliable path.

---

## Spec references
`/.kiro/specs/workflow-manager/` — Requirements **12.14–12.18**, the
Workflow_Test_Runner design bullets (failure attribution, best-effort staged-
model CPU inference with stub fallback, per-node inference-mode reporting), and
tasks **11.9–11.14**.

---

## Continue from here (troubleshooting loop) — 2026-07-31

The x86 builder already built + pushed the fix and repointed `latest`.
Confirmed on ECR:

- `latest` = digest `sha256:e70e85e1e2ee6c00de47f367a4708e5a04333b632228b716055bd5848393da67`
  (also tagged `fix-decode-sklearn`), pushed 2026-07-31T02:54 UTC.

Latest cookies Test run **`c0804f10`** (02:57 UTC, against this new image) was
verified against `results.json` + the `/dda-portal/workflow-test-sandbox` log
stream. Status: **the two fixes on this branch worked, but the run still fails
for a new downstream reason.** Details below so the x86 builder can keep
iterating.

### Confirmed FIXED by the `e70e85e1` image
- **JPEG decode** — no more `jpegdec: Decode error #21 ... state 205`. The
  Pillow baseline-JPEG normalization in `dataset.py` resolved it.
- **sklearn** — no more `ModuleNotFoundError: No module named 'sklearn'`. The
  `scikit-learn==1.3.2` add to the Dockerfile resolved it.

### Current failure (the new blocker)
1. The real staged model still does **not** reach PLAYING — but now for a
   *different* reason than sklearn. Startup logs show `Error: Failed to
   initialize NVML` (no GPU on CPU-only Fargate) plus loader warnings for
   `libgstemlvideocapture.so` / `libavcodec.so.57`. This is **expected /
   non-fatal**: the model-load fallback fires correctly and reverts to the
   `sim_inference` stub. Working as designed per the UX contract ("the model is
   not executed in cloud tests; the configured outcome is injected").
2. **The actual blocker is the fallback re-run**, which dies with:
   ```
   Pipeline ERROR - multifilesrc1 : Internal data stream error.
   ... gst_base_src_loop(): /GstPipeline:pipeline1/GstMultiFileSrc:multifilesrc1:
       streaming stopped, reason not-linked (-1)
   ```
   The sim re-run launch string is:
   ```
   multifilesrc ... ! jpegparse ! jpegdec idct-method=2 ! videoconvert !
   capsfilter caps=video/x-raw,format=RGB ! identity name=sim_inference_n2 !
   jpegenc idct-method=2 quality=100 !
   emlcapture buffer-message-id=file-target_/aws_dda/captures-jpg interval=0 meta=""
   ```
   `reason not-linked` at `multifilesrc` means a **downstream** element failed to
   link/initialize, leaving its segment unlinked. Prime suspect: the
   **`emlcapture`** ("Capture to File System") sink failing to init on CPU-only
   Fargate (same NVML/plugin environment that blocked the model), which leaves
   its pad unlinked so `multifilesrc` reports `not-linked`.

### Iteration loop (edit → build → verify → push → retag → test → read)
```bash
cd edge-cv-portal
AWS_REGION=us-east-1
ECR=164152369890.dkr.ecr.${AWS_REGION}.amazonaws.com
REPO=dda-workflow-test-sandbox
TAG=fix-capture-$(date +%H%M)     # unique per iteration

# build native amd64
docker build --platform linux/amd64 -f test-sandbox/Dockerfile -t ${ECR}/${REPO}:${TAG} .

# VERIFY your change is in the image before pushing (arm-host lesson; cheap on x86)
docker run --rm --entrypoint grep ${ECR}/${REPO}:${TAG} -n "<a-string-from-your-edit>" /opt/harness/harness/harness.py

# push + repoint latest (server-side retag is the reliable path)
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR}
docker push ${ECR}/${REPO}:${TAG}
MANIFEST=$(aws ecr batch-get-image --repository-name ${REPO} --image-ids imageTag=${TAG} \
  --region ${AWS_REGION} --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name ${REPO} --region ${AWS_REGION} \
  --image-tag latest --image-manifest "$MANIFEST"

# trigger the cookies Test in the portal, then read the newest run
SG=/dda-portal/workflow-test-sandbox
S=$(aws logs describe-log-streams --log-group-name $SG --region ${AWS_REGION} \
    --order-by LastEventTime --descending --max-items 1 \
    --query 'logStreams[0].logStreamName' --output text)
aws logs get-log-events --log-group-name $SG --log-stream-name "$S" \
    --region ${AWS_REGION} --start-from-head --query 'events[].message' --output text
```

### Concrete next hypotheses to try (in order)
1. **Isolate `emlcapture`.** Add a temporary debug pipeline to the harness (or a
   one-off `docker run` inside the image) that runs
   `multifilesrc ... ! jpegparse ! jpegdec ! videoconvert ! fakesink` on the
   staged frames. If that reaches PLAYING, the source/decode chain is fine and
   the fault is definitively the `emlcapture` sink — proceed to (2)/(3).
2. **Simulation-stub the capture node.** `emlcapture` is a hardware/edge sink
   that likely can't init on CPU-only Fargate. In the catalog
   (`edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`)
   the `capture` node type is currently `hardware_dependent=False`, so the
   Compile step renders a **real** `emlcapture` element even in sim. Options:
   - Mark the capture node so the sim path substitutes a benign sink
     (e.g. `filesink`/`multifilesink` writing to a temp dir, or `fakesink`),
     mirroring how other hardware sinks are stubbed. This is a
     **workflow_core catalog change → requires redeploying the Compile Lambda**
     (`EdgeCVPortalComputeStack`), not just the sandbox image.
   - OR have the sandbox harness rewrite/replace `emlcapture` with a benign sink
     at pipeline-build time when running in sim mode (image-only change, no
     Lambda redeploy — preferable for fast iteration).
3. **If `emlcapture` must stay**, check what it needs at init (writable
   `/aws_dda/captures-jpg` path, the `libgstemlcapture.so` deps like
   `libavcodec.so.57`). The build's plugin verification gate requires
   `libgstemlcapture.so` to resolve; confirm it actually loads at runtime with
   `gst-inspect-1.0 emlcapture` inside the image. Missing `libavcodec.so.57`
   would explain a link/init failure.

### Notes / environment facts
- No GPU on Fargate → `Failed to initialize NVML` is expected; don't chase it.
- The fallback + attribution logic lives in
  `edge-cv-portal/test-sandbox/harness/harness.py` (`execute()` model-load
  fallback ~line 780–830; the trigger is
  `if failing_node in staged_by_node or failing_node is None:`).
- Fargate pulls `:latest` at task launch, so the server-side retag is what makes
  the next Test run use a new image.
- The catalog / Compile-Lambda option in (2) is the only path that needs a CDK
  redeploy; the harness-rewrite option keeps you in the image-only fast loop.

---

## RESOLVED — capture-sink blocker fixed and verified — 2026-07-31

The `multifilesrc ... not-linked` / `Internal data stream error` blocker is
fixed. Root cause confirmed: the `capture` node is `hardware_dependent=False`
in the catalog (`nodes.py`), so the simulation compile renders a **real**
`emlcapture` sink, which cannot initialize on CPU-only Fargate (device libs /
writable device path). Its pad stays unlinked, so `multifilesrc` reports
`not-linked` and the whole run fails.

**Fix (image-only, no Compile-Lambda redeploy):** the harness rewrites every
hardware-only sink to a benign `fakesink` before rendering —
`renderer.stub_hardware_sinks(document)` (maps `emlcapture` →
`fakesink sync=false name=sim_capture_<nodeId>`, drops the device-specific
args, preserves the nodeId for attribution). Wired into `harness.execute()`
right after `{dataset_location}` resolution and reported as `capture_sink_stub`
stub activity so the node shows as simulated. Covered by
`tests/test_renderer.py::test_stub_hardware_sinks_*`; full harness unit suite
green (193 passed).

**Built + deployed:** image rebuilt on native x86 via the containerized BuildKit
worker (`docker buildx --driver docker-container`, which avoids the snap-Docker
`copy_file_range` ENOSPC bug — see gotchas), pushed as tag `fix-capture-v2`
(digest `sha256:4e92962ddbadf60145b7ffaef09d1c1ad86f036da7512ce0cf39f8133653de2a`)
and `latest` repointed to it via the server-side retag.

**Verified:** state-machine run `verify-capture-0731034226` (cookies workflow,
same input as the failed `c0804f10`) **SUCCEEDED** — `collection.status =
completed`, `node_count = 3`. The staged model still falls back to the injected
simulated outcome (expected NVML-on-CPU), and the capture node now runs through
the benign sink so the pipeline reaches PLAYING and the run passes.

Source for the fix is committed on `cloud-test-sandbox-cpu-fallback-decode-fix`
(renderer function, harness wiring, and tests). Build-cache note: bump to a
**fresh unique image tag** per iteration — a repeated tag can make Docker reuse
a finished build without re-running, and always re-run the grep verification
before pushing.
