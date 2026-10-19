# Bugfix Requirements Document

## Introduction

A JPEG Folder source cannot be decoded on LocalServer images built on
Ubuntu 22.04. Verified on hardware on 2026-10-09: on the lab Dell (amd64,
CPU only), `aws.edgeml.dda.LocalServer.amd64` 1.0.51 ran the workflow
`folder_source → custom_python → capture` and failed at `jpegdec` in 6 of 6
runs with `No valid frames decoded before end of stream`. The 1.0.50 image
decodes the same chain on the same device and the same file.

- 1.0.51 is Ubuntu 22.04.5, GStreamer 1.20.3, libjpeg-turbo 2.1.2. 1.0.50 is
  Ubuntu 20.04.6, GStreamer 1.16.3.
- `build-custom.sh` line 61 takes the generic image's Ubuntu release from the
  build host (`IMAGE_VER`). So 1.0.51 points to a 22.04 build host, which
  fits the owner's new dedicated Ubuntu 22.04 x86 build server. That link
  is inferred, not checked (dell/result.md); task 7 confirms the OS from
  the build log.
- It is not the custom Python node: on 1.0.51 a decode-free workflow
  (`custom_python_source → custom_python → capture`) passed 27 of 27 runs,
  with output equal to 255 − input.
- Failing pipeline (execution 43c913cf, abridged):
  `filesrc blocksize=-1 location=… ! emexifextract ! jpegparse ! jpegdec
  idct-method=2 ! videoconvert ! videoflip method=automatic ! appsink
  name=py_in_py1 … caps=video/x-raw,format=RGB …`

**Root cause (reproduced offline, see design.md).** On GStreamer 1.20.x,
`jpegdec` reads the formats its downstream peer offers. If the first one it
supports is RGB-family, it decodes through libjpeg-turbo's direct
colour-conversion output (`format_convert=1`). That happens behind
`videoconvert` for an RGB consumer (the custom Python bridge appsink,
model_inference's `capsfilter format=RGB`), and also for an unconstrained
downstream: `videoconvert ! fakesink` negotiates xBGR. On 1.20.3 that path
fails for most JPEGs (`Decode error #21: Improper call to JPEG library in
state 205`), so every frame is dropped. GStreamer 1.16.3 (JP5, 1.0.50)
never takes the path. On 1.24.2 (JP7) `jpegdec` emits only its native
formats, and no direct-conversion path was observed: thor1 passed check 3
on `LocalServer.arm64JP7` 1.0.56 on 2026-10-09. The failure is confirmed only
on Ubuntu 22.04 / GStreamer 1.20.3.

**Owner decision (2026-10-09; owner-decisions item 7).**
"YES to a separate bugfix that keeps Ubuntu 22.04 (decode to I420 before RGB
conversion). It does NOT block the `remediation` merge. amd64 1.0.51 stays
not cleared for station rollout until that fix is verified on the Dell."

- OS policy: JP5 stays on Ubuntu 20.04, and every other target is 22.04 or
  newer. Building amd64 on 20.04 is not an acceptable fix.
- Direction: constrain `jpegdec` to its native output formats (not I420
  unconditionally), so 4:2:2, 4:4:4 and grayscale JPEGs still negotiate, and
  let `videoconvert` convert to RGB.

**Owner requirement: mixed flows must not break (2026-10-09, about
17:55Z).** On hearing that the new capsfilter can abort documents that mix
a folder source with a camera, stream or Python-source feed, the owner said:
"It is important those flows do not break." The orchestrator relayed it as
a hard acceptance gate (the owner-requirements note in the lab notes,
items R1 to R5 and Q1):

- R1: a mixed flow is a JPEG file source (`folder_source`, or
  `csi_camera_source` where it renders a file chain) plus one frame-feed
  source of any validator-accepted type, in either segment order, from a
  pre-fix or a fixed-Portal package, bridged or not. None that works today
  may break on any target.
- R2: offline evidence in the JP5, JP6, arm64 CPU and JP7 LocalServer
  images, with the production parser and consumers: EOS, frames from both
  sources, no SIGABRT. Jetson renders stay byte-identical. All four images
  are required; a missing one is a stop for the owner, never a skipped row.
- R3: on-device mixed runs on the Dell for every feed type the lab can
  exercise, held for a sustained period.
- R4: one mixed flow on a Jetson, with a build that contains the fix,
  before the trunk merge.
- R5: the Portal deploy waits until every x86_64 and arm64_cpu station runs
  a LocalServer with the executor's feed-first rule, or the compile-side
  ordering has landed. The "owner accepts in writing" path is not
  available.
- Q1: state whether the pre-existing Jetson first-`caps=` cases are
  reachable and what they do today, and recommend. Don't implement design
  option (a) without approval.

This spec meets R5 by landing the compile-side ordering (design D10,
option c) in the same fix commit as everything else.

**Where the chain is decided (design.md, "Device-side vs packaged").** For
workflow-engine workflows, the Portal's Component_Packager compiles the
chain, and the order of the source segments, into each package's
`compiled_pipeline.json`. The device renders that document and never
recompiles it from its vendored catalog. So the Portal is the source of
truth for new packages, and already-deployed packages keep the old chain.
The legacy Pipeline_Configuration chain is built on the device at run time.
The fix therefore covers five places:
- the shared catalog `_jpeg_file_chain` (both byte-identical copies);
- the shared compiler's source-segment order (both copies), D10 option (c);
- a device-side normalization of already-packaged documents (C3);
- a device-side feed-first rule for them (C3b);
- the legacy builder (C4).

**Scan results (every `jpegdec` site, plus the code the fix interacts
with).**
- Catalog JPEG file chain `_jpeg_file_chain` in
  `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`
  and its byte-identical vendored copy
  `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`.
  `folder_source` and `csi_camera_source` use it on x86_64, x86_64_nvidia,
  arm64_cpu, arm64_jp5 and arm64_jp7. `unified_input` compiles through the
  `folder_source` mapping. **In scope** for the three 22.04 targets.
- The compiler `workflow_core/compiler/compiler.py` (both copies;
  byte-identical today). `_build_segments` emits source segments in
  topological order seeded by graph node order, so a file source placed
  first in the graph renders first. **In scope** (option c).
- Legacy builder `GstPipelineBuilder._add_file_image_source`
  (`src/backend/gstreamer/pipeline_builder.py`, non-JP6 branch). FOLDER,
  FILE and NVIDIA CSI sources use it, and its model chain ends in
  `capsfilter caps=video/x-raw,format=RGB`. **In scope.** Two more callers
  inherit the capsfilter on constrained arches, harmlessly: the image-source
  capture and preview path (`build()` without a workflow), and the
  live-view `GStreamerBackend` for `NvidiaCSI`, which is Jetson-only.
- The Frame_Feed caps consumers `GstPipelineManager.create_buffer`
  (`src/backend/gstreamer/gst_pipeline.py`) and `python_bridge._fed_frame_caps`.
  Both take the FIRST `caps=` clause of the launch string, up to the next
  `!`. `create_buffer` always does; `_fed_frame_caps` does only for frames
  without an explicit `format`, which means Aravis grabs (stream frames and
  Produced_Frames carry one). They are not changed.
- Already-packaged `compiled_pipeline.json` documents on devices. **In
  scope**, through C3 and C3b.
- The `sim` mapping `_dataset_fed_sim_source` and the cloud test sandbox's
  `simulate.py`, the `README.md` and `README_main.md` manual pipelines
  (`README_main.md` is pinned by the security prose golden
  `iam_baseline_readme_prose.md`), `run_emexif.sh` and the test fixtures.
  **Out of scope** (backlogged or unaffected I420 consumers).
- JP6's PNG-staged chain has no `jpegdec`. It stays unchanged (3.2, D9).

**Bug condition C(X).** X is a JPEG file chain that runs on a LocalServer
image with GStreamer 1.20.x, where `jpegdec` is followed by `videoconvert`
with no constraint on `jpegdec`'s output caps, and the first
`jpegdec`-supported format the downstream offers is RGB-family.
- That holds for an RGB/BGR consumer (the bridge appsink, model_inference,
  `format_convert` RGB or BGR), and for an unconstrained downstream:
  `videoconvert ! fakesink` negotiates xBGR. The Dell's diag-narrowing
  cases 3 and 10 (`jpegdec ! videoconvert ! fakesink`, no RGB request) fail
  that way on 1.0.51, for the canary and for a generated JPEG.
- It does not hold when the downstream prefers I420: a `jpegenc` capture,
  or the bedrock I420 capture chain.
- In this codebase: the catalog chain on x86_64 / x86_64_nvidia /
  arm64_cpu (compiled now or already packaged), and the legacy non-JP6
  file chain on those targets, whenever their downstream isn't I420-first.
  The fix constrains those chains unconditionally.

¬C(X) covers every other chain: JP5 (1.16.3), JP7 (1.24.2), JP6 (pngdec),
downstreams that prefer I420, and every non-JPEG source.

**Hazard the fix introduces, C2(X).** The capsfilter gives the file chain a
`caps=` clause of its own. When that chain renders before the fed appsrc,
both consumers hand the appsrc the capsfilter's quoted caps, and the push
aborts the process (1.5). Without D10 this covers a fixed-Portal package
whose file source comes first in graph order, on any LocalServer, and a
pre-fix package once C3 has inserted the capsfilter. D10 closes it in two
layers: the compiler emits the feed segment first in every new package,
which also protects LocalServers that predate this fix, and the executor
moves it first in pre-fix packages.

**Pre-existing defect P(X), fixed here on the constrained arches only.**
Today, on every arch, a bridged document whose file source → custom_python
segment comes before an Aravis feed hands the fed appsrc the bridge
appsink's `caps=` clause. That clause runs on into the next segment's text
(`video/x-raw,format=RGB appsrc name=py_out_… block=true`), and
`Gst.Caps.from_string` rejects it. A 1-byte/pixel grab then fails the run,
contained. A 3-byte/pixel grab (an RGB camera, or the static image camera)
aborts the process (1.6, measured in the JP5, JP6 and JP7 images, design
D12). The feed-first rules fix it on x86_64, x86_64_nvidia and arm64_cpu as
a side effect. On JP5, JP6 and JP7 it stays as it is: that is an explicit
owner decision (Q1, task 12).

**Validation constraint.** Repo tests cannot run the device's GStreamer.
So the fix is validated in three layers:
- static, unit and property tests over the catalog, the compiler, the
  compiled documents, the normalizer, the feed-first rules, the mixed-flow
  matrix and the legacy string;
- an offline matrix in throwaway containers: stock GStreamer 1.16, 1.20 and
  1.24, plus the JP5, JP6, arm64 CPU, JP7 and amd64 LocalServer images,
  including runs through the arm64 CPU and JP5 images' own pre-fix executor
  code (the local amd64 image predates the workflow engine);
- the gated on-device checks on the lab Dell and thor1, which are the real
  test.

Per `.kiro/steering/builds.md`, on-device code reaches the trunk only after
that hardware verification. The authority for committing and pushing this
device fix before hardware verification is the owner's instruction in this
session: "you will need to push the device fixes to remediation so I can
build them on the build system for the devices". The build system builds
only pushed refs. This run pushes the feature branch
`spec/jpeg-folder-source-decode` rather than `remediation`, because:
- `remediation` now equals `integration/all-specs` (48841a0), so a push
  there puts unverified device code on the trunk path;
- build-source-selection lets the owner build any `source_ref`.

Task 6's gate asks the owner to confirm that build source. If the owner
directs a push to `remediation` instead, that direction is recorded and
followed. The steering's "verify before committing" rule then governs the
trunk merge (task 11).

**Completion criterion.** This spec closes only when the rebuilt amd64
LocalServer passes check 3 and the check 3c mixed-flow matrix on the Dell,
followed by a 30-minute soak (task 8), and the thor1 non-regression step
passes (task 8b). amd64 1.0.51 stays not cleared for station rollout until
task 8 passes.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a workflow-engine document compiled from `folder_source` (or
`csi_camera_source`) for x86_64, x86_64_nvidia or arm64_cpu runs on a
GStreamer 1.20.x LocalServer, and the first `jpegdec`-supported format its
downstream offers is RGB-family (an RGB consumer such as the custom Python
bridge appsink `format=RGB` or model_inference's `capsfilter format=RGB`,
or an unconstrained downstream such as `videoconvert ! fakesink`, which
negotiates xBGR) THEN `jpegdec` negotiates that RGB-family format, decodes
through libjpeg-turbo's direct-output path, fails each frame with
`Improper call to JPEG library in state 205`, and the run fails at
`jpegdec` with `No valid frames decoded before end of stream`. Verified:
Dell, 1.0.51, 6 of 6 runs. The Dell's diag-narrowing cases 3 and 10
(`jpegdec ! videoconvert ! fakesink`, no RGB request) fail the same way.

1.2 WHEN the legacy builder's non-JP6 file chain (`… jpegdec idct-method=2 !
videoconvert ! videoflip ! capsfilter caps=video/x-raw,format=RGB !
emltriton|eminfer`) runs on such an image THEN the same failure occurs.
This is inferred from the identical decode chain. It was not run on the
Dell, to protect the owner's `/aws_dda/cookies` images.

1.3 WHEN the image runs GStreamer 1.20.3 THEN which JPEGs fail depends on
subsampling, geometry and CPU, so no subset of JPEGs is safe. Offline, the
direct path failed for 4:4:4, 4:4:0, 4:1:1, grayscale, RGB-colourspace and
odd-size 4:2:0 JPEGs. The Dell canary is a baseline 576x768 4:4:4 JPEG.
That was measured, not inferred, from its byte-identical repo copy
`datasets/cookie-dataset/test-images/test-normal-1.jpg` (sha256
`9fabe604…`): its SOF0 header gives all three components 1x1 sampling,
and Pillow's `get_sampling` returns 0 (design iterations 4 to 7). On the
Dell the path failed even for a generated 320x240 4:2:0 JPEG.

1.4 WHEN a workflow was packaged before the fix THEN its
`compiled_pipeline.json` carries the unconstrained chain. A fixed catalog
alone doesn't repair it: it keeps failing on a 22.04 LocalServer until it is
repackaged.

1.5 WHEN a document holds a JPEG file chain that carries the native-caps
capsfilter and a frame-feed source, the file source's segment comes first,
and the document runs on a LocalServer without a feed-first rule THEN the
fed appsrc receives the capsfilter's quoted caps, `Gst.Caps.from_string`
returns None, and pushing the frame aborts the process (SIGABRT, exit 134).
Measured with the repo's `create_buffer` in the JP5 (1.16.3), arm64 CPU
(1.20.3) and JP7 (1.24.2) images (design iteration 2), and with the images'
own pre-fix executor code in the arm64 CPU and JP5 images, for Aravis, stream
and Python-source frames (iteration 3). Workflow runs execute in the backend
process, so:
- a triggered registration aborts the backend on every trigger;
- a continuous stream registration (`rtsp_camera_source` defaults to
  `processing_mode` continuous) aborts it at the first tick after every
  start while the stream is up. With `restart: always` that is a crash
  loop, until the registration is removed or paused.
The fix itself introduces this hazard: today no file chain renders `caps=`.

1.6 WHEN a bridged document (a `custom_python` node on the file branch)
renders its file source segment ahead of an Aravis feed, on any arch, today
THEN `_fed_frame_caps` takes the bridge appsink's clause
`video/x-raw,format=RGB appsrc name=py_out_<node> is-live=true format=time
block=true`, `Gst.Caps.from_string` returns None, and:
- a 1-byte/pixel grab fails the run with `Frame buffer cannot be reconciled
  with the caps it is pushed under` (contained);
- a 3-byte/pixel grab passes the size check, and the push aborts the
  process (SIGABRT).
Measured in the JP5 (1.16.3), JP6 (1.20.3) and JP7 (1.24.2) images (D12).
Pre-existing, not reported, reachable from the Portal.

### Expected Behavior (Correct)

2.1 WHEN `folder_source` or `csi_camera_source` is compiled for x86_64,
x86_64_nvidia or arm64_cpu THEN the chain SHALL be `filesrc ! emexifextract !
jpegparse ! jpegdec idct-method=2 ! capsfilter caps=<JPEGDEC_NATIVE_CAPS> !
videoconvert ! videoflip method=automatic`. `JPEGDEC_NATIVE_CAPS` is
`video/x-raw,format=I420;video/x-raw,format=Y42B;video/x-raw,format=Y444;video/x-raw,format=GRAY8;video/x-raw,format=RGB`,
with I420 first.

2.2 WHEN such a chain runs with an RGB consumer downstream THEN `jpegdec`
SHALL output I420 for every YCbCr JPEG (4:2:0, 4:2:2, 4:4:4, 4:4:0, 4:1:1,
progressive, odd sizes), GRAY8 for grayscale and RGB for RGB-colourspace
JPEGs, and SHALL NOT use the direct-conversion path. `videoconvert` SHALL
then convert to the consumer's format. This SHALL hold on GStreamer 1.16,
1.20 and 1.24.

2.3 WHEN the legacy builder builds the non-JP6 file chain on a device whose
`environment.device_arch()` is x86_64 or arm64_cpu THEN it SHALL insert the
same capsfilter, built from the same constant, right after `jpegdec`.

2.4 WHEN the workflow executor runs a document whose `registration.arch` is
x86_64, x86_64_nvidia or arm64_cpu, and a segment holds the unconstrained
catalog sequence `jpegparse, jpegdec, videoconvert` THEN it SHALL insert
the capsfilter between `jpegdec` and `videoconvert` in the per-run
document before rendering. It SHALL tag the capsfilter with `jpegdec`'s
`nodeId`, log one INFO line with the count, and SHALL NOT insert a second
capsfilter where one is already present.

2.5 WHEN the Portal catalog or compiler changes THEN the vendored
`catalog/nodes.py` and `compiler/compiler.py` SHALL be re-synced byte for
byte by per-file copy (design D6). `test_vendored_catalog_mirror.py` SHALL
pass, with `"compiler/compiler.py"` appended to its
`MIRRORED_SHARED_MODULE_RELPATHS`, so the existing guard also checks the
two compiler copies byte for byte.

2.6 WHEN the change alters recorded catalog outputs THEN each record SHALL
be updated through its documented maintenance path, and the reviewed diff
SHALL show only the inserted element:
- `catalog_baseline.json`: the scoped refresh of exactly `folder_source`
  and `csi_camera_source`, plus a maintenance-log entry in
  `test_bug_catalog_preservation.py`. Its two consumers,
  `test_bug_catalog_preservation.py` and `test_catalog_stream_and_analytics.py`,
  SHALL pass afterwards;
- `golden_zero_trigger_compilation.json`: `_write_golden()`;
- the CSI consumers sha256 golden: per-entry rebaseline.

2.7 WHEN the rebuilt amd64 LocalServer runs on the Dell (owner-gated) THEN:
- check 3 SHALL complete for a pre-fix package (3a) and a package compiled
  from the fixed tree (3b). `check3.py`'s inversion proof SHALL show an
  output size equal to the EXIF-oriented source size, Pearson
  r(output, 255 − source) ≥ 0.99, r(output, source) ≤ −0.99, and mean
  |output − (255 − source)| ≤ 3. The JPEG decode, the re-encode and I420
  chroma keep the output from equalling 255 − source exactly: thor1
  measured r = 0.9996 and a mean of 1.12 on the working JP7 chain;
- every check 3c mixed run SHALL complete, for each feed type the lab can
  exercise (`custom_python_source`; an RTSP stream from a throwaway local
  source, on_trigger and continuous; the Aravis Fake camera or the static
  image camera, non-bridged and bridged), each from a pre-fix-shape package
  and a fixed-shape package. Each run SHALL show the proof of both frames
  defined in task 8;
- a temporary legacy Folder-source model workflow SHALL decode;
- in a 30-minute soak that includes every 3c workflow, every run SHALL
  complete, and the backend's StartedAt and RestartCount SHALL NOT change.

2.8 WHEN the executor runs a document on a constrained arch whose fed
appsrc (the element `_point_appsrc_at_frame_feed` names `appsrc`) heads a
segment other than the first THEN it SHALL move that segment to index 0
before rendering. Every other segment SHALL keep its relative order, and
one INFO line SHALL be logged. It SHALL do nothing when the fed appsrc's
segment is already first, when there is no fed appsrc, and on any other
arch.

2.9 WHEN the compiler compiles a graph for x86_64, x86_64_nvidia or
arm64_cpu with `simulation=False`, and the appsrc-headed segment of a
frame-feed source (`aravis_camera_source`, `custom_python_source`,
`rtsp_camera_source`, `rtmp_stream_source`) is not at index 0 THEN it SHALL
move that segment to index 0 inside `compile()`. Every other segment SHALL
keep its relative order, and every segment, tee and funnel name SHALL stay
unchanged. The packager's `bindingPoints` slots SHALL be derived from the
reordered document. Every other arch and every simulation compile SHALL be
unchanged.

2.10 WHEN a mixed document runs on a constrained arch, from either package
shape THEN the fed appsrc SHALL be the first element of the rendered launch
string. So 1.5 and 1.6 SHALL NOT occur there.

2.11 WHEN a build of the feature branch runs on thor1 (owner-gated, task
8b) THEN check 3's logged launch string SHALL equal LocalServer.arm64JP7
1.0.56's after one normalization of both strings: keep the text after
`starting pipeline: `, then apply
`re.sub(r'\b(location|buffer-message-id)=\S+', r'\1=<P>', s)` (those two
values carry the run's canary path and capture id). Neither the "Pinned"
nor the "Moved" INFO line SHALL appear, and one non-bridged mixed flow
SHALL complete with its segment order unchanged.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a chain is compiled for, built on or run on arm64_jp5 or arm64_jp7
THEN the system SHALL CONTINUE TO produce the current 6-element chain and
the current segment order byte for byte. That covers catalog mappings,
compiled documents (mixed ones included), legacy builder strings and
executor documents. JP5 is pinned to 20.04 / GStreamer 1.16.3, and JP7
passed check 3 on 1.24.2 hardware, so neither needs a release rebuild for
this bug. thor1 runs one verification build (task 8b).

3.2 WHEN the target is arm64_jp6 THEN the system SHALL CONTINUE TO use the
PNG-staged chain and the current segment order unchanged: the catalog
`_jp6_png_staged_chain`, the executor's `_stage_frame_sources` and
`_prepare_csi_captures`, and the legacy builder's `_stage_decoded_png`.

3.3 WHEN a `sim` mapping is compiled, a graph is compiled with
`simulation=True`, or the cloud test sandbox runs THEN the system SHALL
CONTINUE TO produce today's documents, including `multifilesrc ! jpegparse
! jpegdec ! videoconvert`, unchanged.

3.4 WHEN the fixed chain is built THEN every element other than the inserted
capsfilter SHALL CONTINUE TO be unchanged in factory, order and args:
`filesrc blocksize=-1 location=…`, `emexifextract`, `jpegparse`,
`jpegdec idct-method=2`, `videoconvert` and `videoflip method=automatic`.
`plugin_dependencies` SHALL CONTINUE TO be unchanged (`capsfilter` is in
`coreelements`, which is already listed). EXIF orientation SHALL CONTINUE TO
apply: an orientation-6 JPEG still ends rotated to 480x640.

3.5 WHEN `test_bug_catalog_preservation.py` runs THEN every descriptor other
than `folder_source` and `csi_camera_source` SHALL CONTINUE TO match
`catalog_baseline.json` byte for byte, including the recorded pre-change
`mqtt_publish` values.

3.6 WHEN the executor runs a document for an arch outside the constrained
set, or on a constrained arch a document with neither the exact
`jpegparse, jpegdec, videoconvert` sequence nor a fed appsrc outside the
first segment THEN the rendered launch string SHALL CONTINUE TO be byte
identical to today's. Examples: test fixtures `filesrc ! jpegdec !
fakesink`, JP6 `pngdec` chains, custom chains, documents that already
carry the capsfilter with the feed first, and single-source Frame_Feed
documents. The order of the other run-time normalizations SHALL CONTINUE TO
be unchanged.

3.7 WHEN a folder or file source runs THEN the following SHALL CONTINUE TO
behave as today: directory resolution to the oldest JPEG, consumption on
success, relocation to `failed/` on failure, and
`get_input_file_from_pipeline` returning the legacy string's file.

3.8 WHEN an element of the source chain fails THEN failing-node attribution
SHALL CONTINUE TO name the source node. The inserted capsfilter carries
`jpegdec`'s `nodeId`, and the name map is built from the final document.

3.9 WHEN the fix ships THEN the following SHALL CONTINUE TO be unchanged:
Ubuntu 22.04 bases, `build-custom.sh`, Dockerfiles, recipes,
`requirements.txt`, the security preservation baselines, and every other
dependency.

3.10 WHEN a document holds a JPEG file chain and a frame-feed source and
runs on x86_64, x86_64_nvidia or arm64_cpu THEN the fed appsrc SHALL
CONTINUE TO receive `_frame_caps(frame_data)` plus the frame's width and
height, whatever the segment order, package shape or bridging, through
both `GstPipelineManager.create_buffer` and `python_bridge._fed_frame_caps`.

3.11 (Owner gate, "It is important those flows do not break.") WHEN a
document holds a JPEG file source (`folder_source`, or `csi_camera_source`
where it renders a file chain) and one frame-feed source of any type the
validator accepts (`aravis_camera_source`, `rtsp_camera_source`,
`rtmp_stream_source`, `custom_python_source`), in either segment order,
from a pre-fix or a fixed-Portal package, bridged or not THEN it SHALL
CONTINUE TO work on every target where it works today, and SHALL work on
x86_64, x86_64_nvidia and arm64_cpu. "Works" means the run reaches EOS with
one frame delivered from each source, and the process doesn't abort.

3.12 WHEN a package compiled by the fixed Portal runs on a LocalServer that
predates this fix THEN its mixed documents SHALL CONTINUE TO hand the fed
appsrc its own caps, as pre-fix packages do today. Its single-source
documents SHALL decode as before, or better on 1.20.x.

## Acceptance Criteria

1. In stock Ubuntu 20.04, 22.04 and 24.04 containers (GStreamer 1.16.3, 1.20.3
   and 1.24.2), the fixed chain decodes all 12 test JPEG kinds (design,
   "Offline matrix") with exit 0, both with the RGB tail (X) and with the
   unconstrained `videoconvert ! fakesink` tail (X0), and `jpegdec` outputs
   I420, GRAY8 or RGB as expected. The unfixed chain fails on 1.20.3 for at
   least the 4:4:4, grayscale and RGB-colourspace kinds, and the
   unconstrained unfixed chain (U0) for at least 4:4:4.
2. The fixed chain, with the real `emexifextract`, decodes in every
   available LocalServer image that runs it: JP5 (1.16.3), arm64 CPU
   (1.20.3), JP7 (1.24.2), amd64 20.04 (1.16.3), and the branch-built amd64
   22.04 image once it exists. The JP6 image (label `flask-app:1.0.46`,
   1.20.3) never runs this chain; it joins the mixed-flow matrix (AC 13)
   with its own `pngdec` chain. Image names in this spec are redacted
   labels, resolved to local image IDs by tasks.md task 4's Image rule.
3. Catalog tests: the folder_source and csi_camera_source chains for x86_64,
   x86_64_nvidia and arm64_cpu equal the 7-element chain in 2.1, with
   `args_template == {"caps": JPEGDEC_NATIVE_CAPS}`. The arm64_jp5,
   arm64_jp7, arm64_jp6 and `sim` mappings equal the frozen unfixed-tree
   golden `goldens/jpegdec_native_caps/unconstrained_mappings.json`
   (task 2), and also their `catalog_baseline.json` entries.
4. Compiling `folder_source → capture` for x86_64 renders
   `jpegdec idct-method=2 ! capsfilter caps="video/x-raw,format=I420;…;video/x-raw,format=RGB" ! videoconvert`.
   The arm64_jp5 and arm64_jp7 renders are unchanged.

5. The unit and property tests of the executor normalizer and the
   feed-segment rule pass. The normalizer inserts once per matching
   sequence. Both are idempotent and gated by arch, leave non-matching
   documents unchanged, and never raise on JSON-shaped input, including
   `{"segments": true}`, `{"segments": 1.5}` and `{"segments": 5}`.
6. The legacy builder tests pass in the `flask-app` container: the x86_64 and
   arm64_cpu strings carry the capsfilter, the JP5 string is unchanged, and
   the JP6 PNG path is unchanged. They run with the tree mounted at
   `/w/repo` (tasks.md Overview), on their own as well as with the whole
   `gstreamer/` directory. Each new case first asserts that the builder it
   tests is the tree's `src/backend/gstreamer/pipeline_builder.py`. Under a
   `/repo` mount, pytest's package handling of the repo root's
   `__init__.py` puts `/` first on `sys.path`, and the single-file run then
   tests the image's baked builder instead (review 6, finding 1). Their
   goldens live in `test/backend-test/gstreamer/goldens/jpegdec_native_caps/`.
   Task 2 captures them once, with a read-write mount of only the goldens
   directory, and hands it back to the host user. Every other run is
   read-only (review 7, finding 1).
7. `test_vendored_catalog_mirror.py` passes, including its new
   `compiler/compiler.py` case. The two `nodes.py` copies and the two
   `compiler/compiler.py` copies are byte-identical (`cmp`, and the mirror
   test), and the vendor README's `diff -r` shows only the two pre-existing
   differences (`catalog/__init__.py`, `catalog/platforms.py`).
8. The `catalog_baseline.json` diff touches only `folder_source` and
   `csi_camera_source`, with one element in each of 3 mappings: 36
   inserted lines and no deletion. The zero-trigger golden diff touches
   only the x86_64, x86_64_nvidia and arm64_cpu `folder_capture` documents:
   21 inserted lines. Both were measured on a scratch export in design
   iteration 4.
9. These suites are green apart from the pre-existing failures task 2
   records (same test ids before and after): the workflow_core suite; the
   backend directories `workflow_engine/`, `static_image_camera/`,
   `stream_ingest/`, `output_bindings_fixes/`, `api-endpoints/`, `utils/`,
   `resources/` and `csi_nvargus_optional/`; `gstreamer/` in the
   `flask-app` container, with the tree at `/w/repo` (AC 6); the named
   Portal packaging and binding-point files in the
   `dda-portal-test:py311` container; the security guard
   suites; and, in the JP5 `flask-app` container against a writable
   throwaway copy of the tree under test, the full security preservation
   suite and the rest of `build-custom.sh`'s in-image gate body (the gate a
   component build runs before packaging). No security baseline changes.
   - The gate body runs as a derived copy that keeps going past a failing
     gate (`set +e` plus an ERR trap that logs each failing command;
     tasks.md task 5 item 5), so every gate runs.
   - It never runs against the worktree itself. The backend conftest
     copies files into the tree, so under a read-only mount every test
     that loads it errors at setup, the six audit gates' exploration and
     negative-fixture suites included. A read-write mount rewrites a
     tracked test file and leaves root-owned caches (review 5, finding 1).
   - Pass rule, the same in tasks.md task 5 item 5 and the design's Error
     Handling. In the JP5 image, two blocks fail for image-content
     reasons this fix doesn't touch. One is the first block, the five
     backend unit test files. It runs verbatim at `/repo`, so its tests
     import a mix of the tree's `utils` and the image's baked `app`,
     `gstreamer` and `model`, by AC 6's mechanism. The other is the
     stream camera components gate: no local image has `/stream_ingest`
     or PyAV.
     Task 2 records their ids as pre-existing. The six audit gates must
     pass in both runs. The work stops on any failing command inside the
     six audit gates, and on any failing command or test id in the first
     block or the stream gate that task 2's run didn't show.
10. On the Dell (owner-gated, unchecked here): checks 3a and 3b pass under
    the numeric rule in 2.7. Every check 3c mixed run passes with the proof
    of both frames. A temporary legacy Folder-source model workflow
    decodes. The 30-minute soak, which includes every 3c workflow, ends
    with every run completed and the backend's StartedAt and RestartCount
    unchanged.
11. Compile-side order (2.9): compiling a mixed graph (a `folder_source` or
    `csi_camera_source`, then each of the four frame-feed types, in graph
    order) for x86_64, x86_64_nvidia and arm64_cpu puts the feed segment at
    index 0 with every name unchanged. For an `icam_source` plus Aravis
    graph, the packager's `bindingPoints` slot still addresses the
    `v4l2src` `device` argument. The arm64_jp5, arm64_jp6, arm64_jp7 and
    simulation compiles of the same graphs equal the frozen unfixed-tree
    golden `goldens/jpegdec_native_caps/unconstrained_mixed_compiles.json`.
12. Mixed-flow matrix, unit (3.10, 3.11; P8): `execute()` over feed {Aravis
    1 byte/pixel, Aravis 3 bytes/pixel, stream, Python source} × order
    {file first, feed first} × shape {pre-fix, fixed} × run {non-bridged,
    bridged}, on all six device arches. Constrained rows: the launch string
    starts with the fed appsrc; its first `caps=` clause is one token equal
    to `_frame_caps(frame)`; `_fed_frame_caps` returns that plus the
    dimensions; each file chain renders exactly one capsfilter. Jetson rows
    (their real shape only) render byte-identically to their unfixed-tree
    goldens.
13. Mixed-flow matrix, offline (R2): the same rows run in the JP5
    (1.16.3), JP6 (label `flask-app:1.0.46`, 1.20.3), arm64 CPU (1.20.3) and
    JP7 (1.24.2) images. They use the production `Gst.parse_launch`,
    `GstPipelineManager.create_buffer` (non-bridged rows) and
    `python_bridge.run_bridged_pipeline` with a pass-through handler
    (bridged rows). Each row reaches EOS with exactly one buffer at each
    branch's sink pad and exits 0, and the fed appsrc carries the frame
    caps. Jetson launch strings are byte-identical before and after the fix.
    One exception: on JP5, JP6 and JP7 the bridged file-first rows with an
    Aravis frame are design D12's pre-existing bridge-appsink shapes. They
    fail today with 48841a0's code, as a contained
    `PipelineExecutionException` at 1 byte/pixel and a SIGABRT at 3
    bytes/pixel. For those rows the criterion is that the fixed code
    renders the same launch string and gives the same outcome as 48841a0's
    (R1: no flow that works today may break). They are recorded with both
    outcomes side by side, never counted as passes, and go to the owner's
    Q1 decision (tasks.md task 12).
    All four images are required: an R2 image that is missing, ambiguous or
    reports the wrong OS or GStreamer stops the work for an owner decision
    (tasks.md task 4, Image rule). No R2 row may be recorded as "not run".
14. Older LocalServers (R5, 3.12): packages compiled by the fixed tree run
    through the arm64 CPU and JP5 images' own baked executor, rendering
    and `create_buffer` code reach EOS with one buffer per sink, for
    Aravis, stream and Python-source frames. The file-first counterexample
    aborts (exit 134 under tasks.md task 4's row command, which runs with
    `--init`). Design iteration 3 measured both in those two images; task
    4 repeats it in the arm64 CPU and JP5 images. The local amd64 image
    predates the workflow engine (no `/workflow_engine`), so it can't run
    a workflow package and is out of scope (review 7, finding 2). Both
    images' frame-feed consumers (`create_buffer`,
    `_point_appsrc_at_frame_feed`, `_frame_caps`, `_fed_frame_caps`,
    `render_launch_string`) are byte-identical to 48841a0's, so this covers
    LocalServers built from that code. Older builds can carry older
    consumers (the JP6 1.0.46 image does), but each generation checked
    reads the launch string's first `caps=` and gives the renamed appsrc
    its own caps, which is all C1b relies on. 1.0.50's own code isn't local
    and isn't checked.
15. Jetson non-regression (owner-gated, R4): task 8b passes on thor1.

## Non-Functional Requirements

- No new dependency, file I/O, Pillow decode or staged file. The fix is one
  in-pipeline element, one pure compile-time reorder, and two pure document
  rewrites on the device.
- No new process-abort path on any LocalServer, including LocalServers that
  predate this fix: no launch string rendered from a package the fixed
  Portal compiles, or from a pre-fix package on the fixed LocalServer, may
  hand the fed appsrc caps that `Gst.Caps.from_string` rejects.
- Version-agnostic. The same chain must work on GStreamer 1.16, 1.20 and
  1.24, because an x86_64 image's OS follows its build host.
- Public repository: no device thing names, hostnames, LAN addresses,
  account ids, registry URIs, bucket names, credentials, personal names or
  internal scanner or tool names go into any committed file. Devices are
  named only "the lab Dell", "thor1", "MIC-730" and "Orin".

## Out of Scope

- Building amd64 on Ubuntu 20.04 (rejected by the owner), or patching or
  upgrading GStreamer in the images.
- Retiring JP6's PNG staging. Design D9 finds that it very likely works
  around this same defect. Backlogged: it needs Orin verification.
- The `sim` mappings and the cloud test sandbox's `simulate.py` (same class,
  separate deployable). Backlogged.
- The `README.md` and `README_main.md` manual test pipelines. The
  `README_main.md` copy is pinned by a security prose golden. Backlogged.
- The pre-existing first-`caps=` shapes on JP5, JP6 and JP7 (design D12),
  including the bridged abort of 1.6. Fixing them there means widening the
  feed-first rules to the Jetson arches, or option (a), and needs MIC-730,
  Orin and thor1 verification. It is an explicit owner decision (task 12)
  and is not implemented here.
- Anchoring the consumers' lookup to the fed appsrc's own `caps=` (design
  D10, option a). Backlogged.
- Two terminal `emlcapture` elements in one run writing the same
  `{output_dir}/{capture_id}.jpg` (`_route_capture_outputs`; pre-existing).
  Task 8's proof of both frames works around it.
- User-supplied camera `processingPipeline` strings that contain `jpegdec`.
- The pre-existing attribution quirk where later runs in one process report
  `jpegdec1…` with "node unidentified" (dell/result.md, other observations).
- Pre-existing test failures this spec doesn't cause. They are recorded in
  verification-notes.md and not fixed here.
- Merging into `remediation`, `integration/all-specs` or `main`, the Portal
  ComputeStack deploy, and the station rollout. These are later
  owner-gated steps (tasks.md 7 to 12).

## Assumptions

- amd64 NVIDIA devices register only `x86_64` packages.
  `environment.device_arch()` returns `x86_64` on every x86 host, and
  discovery requires `targetArch == device_arch`, so an `x86_64_nvidia`
  package registers as invalid there (pre-existing). Both ids are in the
  constrained set, so the device path and the catalog output are
  constrained either way.
- The generic arm64 CPU LocalServer is a shipped target. `build-custom.sh`
  forces it to Ubuntu 22.04, and its 22.04 image fails unfixed on grayscale
  JPEGs offline. It has no lab device, so it is recorded as built, not
  verified on device, like amd64Nvidia (owner-decisions item 4). The owner
  is adding two NVIDIA V100 GPUs to the lab Dell, which could give
  amd64Nvidia an on-device check after task 8. That is a pending owner
  decision (tasks.md tasks 8 and 9), not an acceptance criterion here.
- The Dell run-2 check 3 package (compiled before the fix, kept in the lab
  evidence bundle under `dell/work/twf-r2/pkg-a/`) can be redeployed
  unchanged, and it proves the device-side path. The run-2 RESTORE deleted
  its component version and its S3 zip. Task 8 re-uploads the zip and
  re-creates the component version from `pkg-a/recipe.json`. If Greengrass
  refuses to re-create that version, task 8 bumps only the recipe's
  `ComponentVersion` (for example to 1.0.1) and keeps the zip and its
  `compiled_pipeline.json` byte-identical.
- The Dell's Aravis Fake camera (its registry id is in the lab evidence)
  and its static image camera grab through the Aravis feed path on the
  rebuilt LocalServer. If neither grabs, check 3c-aravis records N/A with
  the error and points to the offline Aravis rows.
- The `bluenviron/mediamtx:1.9.3-ffmpeg` image can be pulled on the Dell
  and run by its recorded digest. If that tag can't be pulled, task 8 stops
  and asks the owner for a replacement tag. Its RTSP, RTP and RTCP
  addresses are set to 127.0.0.1. Its ffmpeg publisher uses
  `-rtsp_transport tcp`, so ffmpeg should need no UDP sockets of its own.
  Neither behaviour was checked offline. With both settings, it should
  listen on localhost only. Task 8 checks that with `ss`
  before using it. It judges only each socket's local address (the peer
  column reads `0.0.0.0:*`, or `[::]:*` for IPv6), and it requires
  exactly the three listeners, each once, and no other socket of the two
  processes (review 6, finding 3; review 7, NIT 4). The backend uses host networking, so
  `rtsp://127.0.0.1:8554/…` is reachable from it, and the amd64 image ships
  `gstreamer1.0-libav` for H.264.
- The image labelled `flask-app:1.0.46` is a JP6 build: Ubuntu 22.04.3,
  GStreamer 1.20.3, CUDA 11.4 and 12.6, VPI 3.2.4, with `gi` for
  python3.10 only. That matches `Dockerfile.jp6` (an l4t-jetpack r36.4.0
  base plus a cuda-11.4 stage). Checked in design iteration 3; its image ID
  and reported versions were re-checked in iteration 4. The same image ID
  resolved again in iterations 5 to 8, and iterations 7 and 8 re-ran its
  probe (Ubuntu 22.04.3, GStreamer 1.20.3).
