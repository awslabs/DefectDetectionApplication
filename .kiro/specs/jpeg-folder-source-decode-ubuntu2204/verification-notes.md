# Verification Notes — jpeg-folder-source-decode-ubuntu2204

**Status (2026-10-14):** design review 8 approved the design, and
implementation tasks 1 to 5 are done, offline only.
- "Implementation verification" below records them: the unfixed-tree
  failures and baselines, the fix, the offline matrix in the four R2
  images, and the repo suites. Task 6's two commits are made; the push of
  the feature branch is pending.
- Nothing is verified on a device yet. Owner-gated tasks 7 to 12 are
  pending and stay unchecked. Tasks 8 and 9 also carry the lab Dell's GPU
  upgrade as pending owner decisions.
- The owner's hard gate (R1 to R5, Q1) is unchanged since iteration 3.
  Offline, every R1 shape passes in the arm64 CPU image (1.20.3), whose
  documents equal x86_64's apart from `targetArch`. Every JP5, JP6 and JP7
  row keeps 48841a0's launch string and outcome. 12 of those Jetson rows
  are design D12's pre-existing shape; they go to task 12, not counted as
  passes.
- Design iteration 8 answered design review 7; its evidence is in
  "Design iteration 8" below.

Redaction policy: no device thing names, hostnames, LAN addresses, account
ids, registry URIs, bucket names, credentials, personal names, or internal
scanner or tool names appear here. Image references are redacted labels
(repository basename and tag, without any registry prefix) plus local
image IDs.

## Owner requirement carried as the hard gate (2026-10-09, about 17:55Z)

The owner, on the mixed folder + camera/stream/Python-source documents:
"It is important those flows do not break." The orchestrator relayed it
with items R1 to R5 and Q1 (the owner-requirements note in the lab notes).
Where each one lands:

| Item | bugfix.md | design.md | tasks.md |
|---|---|---|---|
| R1, every mixed shape | 3.11, AC 12 | D11, P8 | 1(i), 1(i2), 1(i3), 2 (matrix) |
| R2, offline in the JP5, JP6, arm64 CPU and JP7 images | AC 13 | D11, P8, Error Handling | 4 (Image rule), 4.4 |
| R3, Dell mixed runs per feed type, sustained | 2.7, AC 10 | D11, Testing Strategy | 8 (check 3c, soak) |
| R4, one Jetson run with the fix before the trunk merge | 2.11, AC 15 | D3, D11 | 7, 8b, 11 |
| R5, Portal deploy waits for C3b stations or the compile-side order | 3.12, AC 14 | D10 (C1b), D11 | 4.5, 10 |
| Q1, pre-existing Jetson shapes | 1.6, Out of Scope | D12 | 4.6, 12 |

R2 needs all four images. If one is missing or ambiguous, or reports the
wrong OS or GStreamer, the work stops for an owner decision. It is never
recorded as "not run" (tasks.md task 4, Image rule; design Error Handling).
On 2026-10-09 all four resolved (Design iteration 4). They resolved to
the same four image IDs twice on 2026-10-10 (Design iterations 5 and 6),
and again on 2026-10-12 and 2026-10-14, when each one's probe was re-run
as well (Design iterations 7 and 8).

The owner's words are the gate: "It is important those flows do not
break." A mixed workflow is a JPEG folder source, or `csi_camera_source`
where it renders a file chain, plus an Aravis, stream (RTSP) or
`custom_python_source` feed. None may break on any target.

## Design-phase offline evidence (2026-10-09)

**Environment.**
- aarch64 host with snap Docker. Every container ran with `--rm`.
- Stock images ran natively (arm64). `ubuntu:22.04` also ran as amd64
  under emulation (`--platform linux/amd64`).
- The scratch scripts and JPEGs lived outside the repo and were deleted
  afterwards.
- No repo file was written: test runs used `-p no:cacheprovider` and
  `PYTHONDONTWRITEBYTECODE=1`.

| Image | OS | GStreamer | plugins-good | libjpeg-turbo8 |
|---|---|---|---|---|
| `ubuntu:20.04` | 20.04.6 | 1.16.3 | 1.16.3-0ubuntu1.3 | 2.0.3-0ubuntu1.20.04.3 |
| `ubuntu:22.04`, arm64 and amd64 | 22.04.5 | 1.20.3 | 1.20.3-0ubuntu1.10 | 2.1.2-0ubuntu1 |
| `ubuntu:24.04` | 24.04.5 | 1.24.2 | 1.24.2-1ubuntu1.8 | 2.1.5-2ubuntu2 |

`jpegdec`'s SRC template is the same in all three:
`{ I420, RGB, BGR, RGBx, xRGB, BGRx, xBGR, GRAY8 }`.

**JPEG kinds.**
- From `jpegenc`, 640x480: g420, g422, g444, ggray. Also godd420, which is
  577x769 4:2:0.
- From `cjpeg` over a decoded g420: crgb (`-rgb`), cprog420
  (`-progressive`), c422 (`-sample 2x1`), c444 (`1x1`), c440 (`1x2`), c411
  (`4x1`) and cgray (`-grayscale`).

**Chains.** All share the head `filesrc blocksize=-1 location=F ! jpegparse !
jpegdec idct-method=2` and the tail `videoconvert ! videoflip
method=automatic ! video/x-raw,format=RGB ! fakesink`.
- **A**, unfixed: nothing in between.
- **B**: `format=I420` in between.
- **C**: `I420;Y42B;Y444;GRAY8` in between.
- **D**, chosen: the five-format `JPEGDEC_NATIVE_CAPS`.

Each cell is the exit code and `jpegdec`'s negotiated src format.

| Kind | 1.16.3 A | 1.16.3 D | 1.20.3 A | 1.20.3 D | 1.24.2 A | 1.24.2 D |
|---|---|---|---|---|---|---|
| g420 | 0 I420 | 0 I420 | 0 RGB | 0 I420 | 0 I420 | 0 I420 |
| g422 | 0 I420 | 0 I420 | 0 RGB | 0 I420 | 0 I420 | 0 I420 |
| g444 | 0 I420 | 0 I420 | **1** RGB | 0 I420 | 0 I420 | 0 I420 |
| ggray | 0 GRAY8 | 0 GRAY8 | **1** RGB | 0 GRAY8 | 0 GRAY8 | 0 GRAY8 |
| godd420 | 0 I420 | 0 I420 | **1** RGB | 0 I420 | 0 I420 | 0 I420 |
| crgb | 0 RGB | 0 RGB | **1** RGB | 0 RGB | 0 RGB | 0 RGB |
| cprog420 | 0 I420 | 0 I420 | 0 RGB | 0 I420 | 0 I420 | 0 I420 |
| c422 | 0 I420 | 0 I420 | 0 RGB | 0 I420 | 0 I420 | 0 I420 |
| c444 | 0 I420 | 0 I420 | **1** RGB | 0 I420 | 0 I420 | 0 I420 |
| c440 | 0 I420 | 0 I420 | **1** RGB | 0 I420 | 0 I420 | 0 I420 |
| c411 | 0 I420 | 0 I420 | **1** RGB | 0 I420 | 0 I420 | 0 I420 |
| cgray | 0 GRAY8 | 0 GRAY8 | **1** RGB | 0 GRAY8 | 0 GRAY8 | 0 GRAY8 |

- **B** fails ggray, cgray and crgb on all three versions.
- **C** fails crgb on all three versions.
- The emulated amd64 1.20.3 run is identical to arm64 in every cell, for
  A through D.

**Direct request** (`jpegdec ! video/x-raw,format=RGB|BGRx ! fakesink`, no
`videoconvert`):
- 1.20.3: only g420 passes. godd420, g444, ggray and crgb fail.
- 1.24.2: g420, godd420, g444 and ggray fail. crgb passes as RGB only.
  This run recorded exit codes, not the error kind. With thor1's 1.24.2
  probes (`jpegdec` offers only I420; a direct RGB request is
  `not-negotiated`), it reads as native-only output, not as a broken
  direct-conversion path. Iteration 3 read it the other way; design
  iteration 4 corrected that (review 3, finding 5).

**Debug excerpt** (1.20.3, g444, `GST_DEBUG=jpegdec:5,videodecoder:3`). The
same lines appear for chain A and for a direct `jpegdec !
video/x-raw,format=RGB`. The peer-caps line shown comes from the direct
case, because in chain A the log line is truncated before the format field.

```
gst_jpeg_turbo_parse_ext_fmt_convert: Received caps from peer: video/x-raw, … format=(string)RGB …
gst_jpeg_turbo_parse_ext_fmt_convert:<jpegdec0> format_convert=1
_gst_video_decoder_error:<jpegdec0> error: Decode error #21: Improper call to JPEG library in state 205
_gst_video_decoder_error:<jpegdec0> error: Failed to decode JPEG image
gst_video_decoder_sink_event_default:<jpegdec0> error: No valid frames decoded before end of stream
```

**LocalServer images with the real `emexifextract`.** The exact compiled
chain is
`filesrc ! emexifextract ! jpegparse ! jpegdec idct-method=2 ! [capsfilter] ! videoconvert ! videoflip method=automatic ! video/x-raw,format=RGB ! fakesink`.
Each run used `--network none` and `GST_PLUGIN_PATH=/usr/lib/panoramagst`.
U is unfixed and X is fixed (the D caps). Cells are U/X exit codes.

| Local image | OS / GStreamer | 576x768 4:2:0 | 320x240 4:2:0 | 640x480 gray |
|---|---|---|---|---|
| `flask-app:latest` (a JP5 build) | 20.04.6 / 1.16.3 | 0/0 | 0/0 | 0/0 |
| `flask-app-amd64:extracted` (amd64, emulated) | 20.04.6 / 1.16.3 | 0/0 | 0/0 | 0/0 |
| `flask-app:arm64-1.1.0` (generic arm64 CPU) | 22.04.5 / 1.20.3 | 0/0 | 0/0 | **1**/0 |
| `flask-app:arm64JP7-1.0.45` (JP7) | 24.04.3 / 1.24.2 | 0/0 | 0/0 | 0/0 |

**EXIF orientation.** The input was an orientation-6 JPEG stored at 640x480,
so the expected final caps are 480x640. Cells show the exit code and the
final sink caps.

| Image | 4:4:4 U | 4:4:4 X | 4:2:0 U | 4:2:0 X |
|---|---|---|---|---|
| JP5, 1.16.3 | 0, 480x640 | 0, 480x640 | 0, 480x640 | 0, 480x640 |
| arm64 CPU, 1.20.3 | **1** | 0, 480x640 | 0, 480x640 | 0, 480x640 |
| JP7, 1.24.2 | 0 | 0 | 0 | 0 |

The JP7 run recorded only exit codes and its first caps line, so the
rotated caps weren't captured there.

**Lab canary.** Corrected in design iteration 4 (review 3, finding 13).
- The 4:4:4 reading of iterations 1 to 3 matches thor1's canary
  (`test-normal-8.jpg`, sha256 `e9fe3f…`), the only canary the lab
  evidence bundle still holds. So it wasn't a measurement of the Dell's
  canary.
- The Dell canary is a byte copy of `/aws_dda/cookies/test-normal-1.jpg`
  (sha256 `9fabe604…`, 503050 bytes). It is byte-identical to the repo's
  `datasets/cookie-dataset/test-images/test-normal-1.jpg`.
- Measured offline from the repo copy: baseline (SOF0), 8-bit, 576x768,
  three components each sampled 1x1, so 4:4:4 (Pillow `get_sampling` 0).
  EXIF orientation 1, with JFIF, EXIF and ICC segments. Design iterations
  5 to 7 measured it again with the same result ("Design iteration 5" to
  "Design iteration 7").
- So the Dell canary is one of the kinds the 1.20.3 direct path fails on.
  dell/result.md's "baseline 4:2:0?" guess was wrong.

**Hardware.** These are references from the lab evidence and were not re-run
here:
- Dell, amd64 1.0.51 (22.04 / 1.20.3): check 3 FAIL, 6 of 6 runs.
- Dell, 1.0.50 (20.04 / 1.16.3): decodes.
- MIC-730, JP5 (1.16.3): direct `jpegdec` passed.
- thor1, JP7 1.0.56 (1.24.2): check 3 PASS on 2026-10-09.
- Orin, JP6: PNG-staged, so `jpegdec` never runs.

## Design iteration 2 evidence (2026-10-09)

Same host and rules as above: throwaway `--rm` containers with
`--network none`, the repo mounted read-only, scratch files outside the repo
and deleted afterwards, and no repo file written.

**Fed appsrc caps in a mixed document (review finding 1, design D10).**
A throwaway script rendered one document with the repo's
`workflow_engine.rendering`, in two segment orders. The segments:
- a JPEG file chain with the native-caps capsfilter, ending
  `videoconvert ! capsfilter caps=video/x-raw,format=RGB ! fakesink`;
- a fed appsrc, `appsrc name=appsrc caps=video/x-raw,format=GRAY8 !
  videoconvert ! f0.`;
- `funnel name=f0 ! fakesink`.

The `f0.` reference precedes `funnel name=f0` in both orders, so both
exercise a forward reference. For each order the script took the first
`caps=` clause, ran `Gst.parse_launch`, and called the production
`GstPipelineManager().create_buffer`, which imported in all three images.
It then pushed one 64x48 GRAY8 frame and waited for EOS. The input was a
640x480 4:4:4 JPEG from `jpegenc`, and `emexifextract` was present
(`GST_PLUGIN_PATH=/usr/lib/panoramagst`). Each order ran in its own
process.

| Local image | OS / GStreamer | Fed segment first (rule C3b) | File chain first (no C3b) |
|---|---|---|---|
| `flask-app:latest` (JP5 build) | 20.04.6 / 1.16.3 | caps `video/x-raw, format=GRAY8, width=64, height=48`; parse OK; **EOS** | first `caps=` is the quoted capsfilter, `from_string` → None (CRITICAL `gst_structure_validate_name`); push → `ERROR:gstappsrc.c:1264:gst_app_src_create: assertion failed: (GST_IS_BUFFER_LIST (obj))`, **exit 134** |
| `flask-app:arm64-1.1.0` (generic arm64 CPU) | 22.04.5 / 1.20.3 | same; **EOS** | same None; push → `GStreamer-App:ERROR:…/gstappsrc.c:1725:gst_app_src_create: code should not be reached`, **exit 134** |
| `flask-app:arm64JP7-1.0.45` (JP7) | 24.04.3 / 1.24.2 | same; **EOS** | same None; push → `GStreamer-App:ERROR:…/gstappsrc.c:1657:gst_app_src_create: code should not be reached`, **exit 134** |

So the hazard is a process abort (SIGABRT) on every shipped GStreamer
version, not only a failed run, and moving the fed segment first fixes it
everywhere. Workflow runs execute in the backend process, so on a device
the backend container would restart.

**Totality of the revised C3 and the new C3b (review finding 2).** A
throwaway copy of both functions' logic as in design C3 and C3b (logging
left out) ran over 20,007 JSON-shaped documents: 7 explicit cases, including
`{"segments": true}`, `1.5`, `5` and `"s"`, plus 20,000 random nested
dicts, lists and scalars. Each ran with 7 arch values: the 3 constrained
ids, `arm64_jp5`, `"unknown"`, `None` and `3`. Results:
- 0 exceptions;
- a second C3 call always returned 0;
- C3b moved a second-segment fed appsrc to index 0, and a second call
  returned False.

**Code facts re-checked for the review.**
- An exception from the C3 call site reaches `execute()`'s outer handler
  (`pipeline_executor.py` ~2376-2383). That handler records
  `Workflow executor failed unexpectedly; see logs` with no
  `failing_node_id`.
- Only continuous runs are reconciled at startup
  (`run_retention.reconcile_interrupted`).
- The lab harness `build_test_workflow.py` hard-codes `REPO` to the
  `remediation` worktree.
- The security prose golden `iam_baseline_readme_prose.md` pins
  `README_main.md` (`README_REL`), not `README.md`. Iteration 1 said
  otherwise; bugfix.md now has it right.
- More than one frame-feed binding point (Aravis, stream, Python source)
  is rejected before any pipeline is built (`python_source.py`).
- The compiler emits source segments in topological order and fan-in
  funnels after their feeders (`compiler._linearize`), so forward `f<N>.`
  references already occur in compiled documents.

## Design iteration 3 evidence (2026-10-09)

Same host and rules as above. Two scratch exports of 48841a0 (`git archive`
of `edge-cv-portal/backend`, `src/backend`, the backend test utilities and
`edge-cv-portal/infrastructure/lib`) lived under `$HOME/dda-tmp/` and were
deleted afterwards. One got design C1b only. The other got C1, C2 and C1b,
applied as the design specifies (the 10 call sites rewritten; both vendored
files copied and `cmp`-identical). No repo file was written. Every
container ran with `--rm --network none`.

**C1b blast radius (review 2, finding 2).**

| Run | Result |
|---|---|
| workflow_core suite, unpatched export | 1022 passed, 1 failed (the `__init__.py` mirror test), 1 skipped |
| The same with C1b only | 1022 passed, 1 failed (same id), 1 skipped |
| The same with C1, C2 and C1b | 1015 passed, 8 failed, 1 skipped |
| The 15 named Portal test files (tasks.md Overview), C1b only, in `dda-portal-test:py311` | 250 passed, 4 skipped, 4 errors. The errors were `test_stream_camera_feature_floor_coverage.py`'s, which couldn't find `infrastructure/lib/compute-stack.ts` in that mount (environmental) |
| The whole Portal `tests/` directory in the same image | Didn't finish within about 48 minutes; stopped, no result |

The 8 failures with C1, C2 and C1b:
- pre-existing: `test_catalog_content.py::TestCatalogMirrorEquality::test_portal_and_vendor_catalog_init_are_byte_identical`;
- exact-chain expectations (task 3.5):
  `test_catalog_content.py::TestCsiCameraSourceNodeType::test_mappings_read_the_csi_capture_file`
  and `test_compiler_compile.py::TestElementChains::test_chain_elements_tagged_with_node_id`;
- recorded outputs (tasks 3.6 and 3.7): the two
  `test_bug_catalog_preservation.py` descriptor tests,
  `test_catalog_stream_and_analytics.py::TestCatalogAppendIsAdditive::test_every_pre_existing_descriptor_content_is_byte_identical`,
  `test_property_zero_trigger_preservation.py::test_legacy_workflow_compilation_matches_committed_golden`
  and
  `test_property_no_new_trigger_preservation.py::test_zero_trigger_golden_comparison_unchanged`.

**Compile results with C1 and C1b.** Graphs: `folder_source → capture`
plus `<feed> → capture`, folder node first, for an Aravis,
`custom_python_source` and `rtsp_camera_source` feed. Segment heads:
- x86_64: `appsrc`, `filesrc`, with the capsfilter at index 4 of the file
  segment and the names `s1`, `s0` (unchanged);
- arm64_jp5, arm64_jp6 and arm64_jp7: `filesrc`, `appsrc`, and each
  document `cmp`-identical with and without C1b;
- `icam_source` plus Aravis: x86_64 `[s1 appsrc, s0 v4l2src]`, arm64_jp5
  `[s0 v4l2src, s1 appsrc]`.

`golden_zero_trigger_compilation.json` holds no `appsrc` (0 occurrences), so
C1b can't change it.

**Older LocalServers run fixed-tree packages (R5, P10).** The x86_64
documents above were run through each image's OWN baked backend code
(`PYTHONPATH=/`, `COMPONENT_WORK_PATH` set): its
`WorkflowExecutor._point_appsrc_at_frame_feed`,
`rendering.render_launch_string` and `GstPipelineManager().create_buffer`.
`emlcapture` was replaced by named `fakesink`s with buffer pad probes. The
input was a 640x480 4:4:4 JPEG, and each case ran in its own process. The
frames were 64x48: Aravis GRAY8 with no `format`, and stream and Python
source RGB with `format` "RGB".

| Image (own code) | OS / GStreamer | Feed first (C1b) | File first (no C1b) |
|---|---|---|---|
| `flask-app:arm64-1.1.0` (generic arm64 CPU) | 22.04.5 / 1.20.3 | Aravis, stream, Python source: EOS; one buffer at each sink; fed caps `video/x-raw, format=(string)GRAY8, width=(int)64, height=(int)48` (Aravis) or the same with `RGB` (stream, Python source) | All three: exit 134, `gstappsrc.c:1725:gst_app_src_create: code should not be reached` |
| `flask-app:latest` (JP5 build) | 20.04.6 / 1.16.3 | Same: EOS, one buffer at each sink, frame caps | All three: exit 134, `gstappsrc.c:1264:gst_app_src_create: assertion failed: (GST_IS_BUFFER_LIST (obj))` |

**Q1 reachability (D12).** The vendored compiler (with C1 and C1b), then
`_point_appsrc_at_frame_feed` and, for the bridged shape, `rewrite_document`.
The graphs were: the file source, then a middle node, then `capture`; plus
a feed → `capture`. The first rendered `caps=` clause:

| Middle node, feed | arm64_jp5, arm64_jp6, arm64_jp7 | x86_64 |
|---|---|---|
| `model_inference`, Aravis | `video/x-raw,format=RGB` | `video/x-raw,format=GRAY8` |
| `format_convert` BGR, `custom_python_source` | `video/x-raw,format=BGR` | `video/x-raw,format=GRAY8` |
| `custom_python` (VideoFrames ports), Aravis | `video/x-raw,format=RGB appsrc name=py_out_m is-live=true format=time b…` (runs on into the next segment) | `video/x-raw,format=GRAY8` |

**Q1 outcomes today (D12).** The repo's code was used, which is unchanged
for Jetsons. A two-branch string was built: a file branch ending
`capsfilter caps=video/x-raw,format=<first> ! fakesink`, then the fed
`appsrc name=appsrc caps=<frame caps> ! videoconvert ! fakesink`. It went
through `create_buffer` (non-bridged). The bridged shape replayed
`run_bridged_pipeline`'s fed-source steps (`_fed_frame_caps`,
`Gst.Caps.from_string`, `set_property`, strict `reconcile_to_caps_stride`,
push). Frames were 64x48.

| First caps, fed frame | `flask-app:latest` JP5 1.16.3 | `flask-app:1.0.46` JP6 1.20.3 (`pngdec` file branch) | `flask-app:arm64JP7-1.0.45` JP7 1.24.2 |
|---|---|---|---|
| RGB, Aravis GRAY8 | exit 2: `PipelineExecutionException` (cannot be reconciled) | same | same |
| I420, Aravis GRAY8 | EOS, one buffer at each sink; fed caps I420 64x48 over a 3072-byte buffer | same | same |
| BGR, Produced_Frame RGB | EOS; fed caps BGR (channels swapped) | same | same |
| RGB, Produced_Frame BGR | EOS; fed caps RGB (channels swapped) | same | same |
| RGB, Produced_Frame RGB (control) | EOS | same | same |
| Bridge appsink run-on, Aravis 1 byte/pixel | `from_string` → None; exit 2, `PipelineExecutionException` | same | same |
| Bridge appsink run-on, Aravis 3 bytes/pixel | `from_string` → None; **exit 134**, `gstappsrc.c:1264` assertion | **exit 134**, `gstappsrc.c:1725` | **exit 134**, `gstappsrc.c:1657` |

**JP6 image identification.** `flask-app:1.0.46` reports Ubuntu 22.04.3 LTS
and GStreamer 1.20.3. It has `/usr/local/cuda-11.4` and `cuda-12.6`, VPI
3.2.4 (`libnvvpi3`, `python3.10-vpi3`), and `gi` only for python3.10
(python3.11 can't import it). That matches `Dockerfile.jp6`: an l4t-jetpack
r36.4.0 base plus an l4t-cuda 11.4 runtime stage.

**Code facts re-checked for review 2.**
- `workflow_packaging.py` derives `bindingPoints` from `compiled.to_dict()`
  after `compile_workflow` (lines ~2659 and ~2708). `binding_point_slots`
  records `segment` by index. Unexpected exceptions end at the handler's
  `INTERNAL_ERROR` 500 (line ~3021).
- `workflow_test_steps.py` compiles for `x86_64` with `simulation` True by
  default. All four frame-feed types are `hardware_dependent`, so they
  compile to `multifilesrc` stubs there.
- `SOURCE_KIND_TO_SOURCE_TYPE` expands `unified_input` to the Aravis, RTSP
  and RTMP sources as well as the file sources.
- `ContinuousRunnerManager` starts a runner for every `registered`
  continuous stream registration, and the operator pause persists across
  restarts. The backend runs `network_mode: host` with `restart: always`.
- `_fed_frame_caps` uses the frame's `format` when present. Stream frames
  carry "RGB", and Produced_Frames carry the producer's format.
- The lab harness writes only `pythonSourceBinding` points.
- The amd64 `Dockerfile` installs `gstreamer1.0-libav` and `ffmpeg`, so
  H.264 RTSP decodes.
- `test/backend-test/` directories that use the executor:
  `workflow_engine/` (42 files), `output_bindings_fixes/` (8),
  `gstreamer/` (4), `static_image_camera/` (3), `utils/`, `stream_ingest/`
  and `api-endpoints/` (2 each), and `resources/` and
  `csi_nvargus_optional/` (1 each).

## Design iteration 4 evidence (2026-10-09)

Same host and rules as above:
- scratch files lived under `$HOME/dda-tmp/` and were deleted afterwards;
- containers were throwaway (`--rm --pull never --network none`) and ran
  by image ID;
- no image was pulled, deleted or retagged;
- no repo file was written: `git status --porcelain` afterwards showed
  only the untracked spec directory;
- no device was contacted.

**Image labels resolved to local image IDs (review 3, finding 3).** Each
label was resolved with tasks.md task 4's Image rule against the local
image list, then run by ID to read what it reports.

| Role | Label | Image ID | Label matches | Reports (OS / GStreamer / `gi` Python) |
|---|---|---|---|---|
| JP5 (R2) | `flask-app:latest` | `f2e221c3e2d1` | 1 | Ubuntu 20.04.6 / 1.16.3 / python3.11 |
| JP6 (R2) | `flask-app:1.0.46` | `1fa46843533f` | 1 | Ubuntu 22.04.3 / 1.20.3 / python3.10 |
| arm64 CPU (R2) | `flask-app:arm64-1.1.0` | `6d5ce25b9e2d` | 1 | Ubuntu 22.04.5 / 1.20.3 / python3.11 |
| JP7 (R2) | `flask-app:arm64JP7-1.0.45` | `b365ed521490` | 1 | Ubuntu 24.04.3 / 1.24.2 / python3.11 |
| amd64, emulated | `flask-app-amd64:extracted` | `f34e42c9acad` | 1 | Ubuntu 20.04.6 / 1.16.3 / python3.11 (x86_64) |
| Portal tests | `dda-portal-test:py311` | `14250238405b` | 1 | not run here |
| Stock | `ubuntu:22.04` | two IDs | 2 | an arm64 and an amd64 image both match the label rule |

- Three of the four R2 labels exist locally only under a
  registry-prefixed repository name, so only the rule finds them. No R2
  image is missing, so no stop was needed.
- The `ubuntu:22.04` row is why the rule requires exactly one image ID
  instead of the review's `head -1`. The local `ubuntu:20.04`,
  `ubuntu:22.04` and `ubuntu:24.04` are arm64.
- Corrected in design iteration 5 (review 4, finding 2). Iteration 4 said
  here that Docker resolves the plain stock names unambiguously, so task
  4.1 could run every stock row by name. That holds for the native rows
  only. The plain name `ubuntu:22.04` is the arm64 image, and the amd64
  one is reachable only by its ID or another name. So 4.1's emulated row
  runs by that ID, and no stock tag is ever pulled or retagged.

**Recorded-output commands (review 3, findings 1 and 2).** Two `git
archive` exports of 48841a0's workflow_core layer were used: a pristine one,
and one with design C1 applied (the helper and its 10 call sites, rewritten
by a scratch script).

| Step | Result |
|---|---|
| The README's step 4 form (`ensure_ascii` default) on the committed baseline | Doesn't round-trip. The baseline holds 15 literal U+2014 em dashes: `bedrock_inference` 7, `custom_python_source` 2, `llm_inference` 2, `mqtt_subscribe` 2, `model_inference` 1, `opcua_subscribe` 1 |
| Task 3.6's snippet (`ensure_ascii=False`, UTF-8) | The round-trip assertion holds. Diff: 36 insertions, 0 deletions, 6 capsfilter blocks, all in `folder_source` and `csi_camera_source` |
| The docstring command, `PYTHONPATH=python python3 -c "… _write_golden()"` | `ModuleNotFoundError: No module named 'rpds.rpds'`, raised from the layer's bundled `rpds` |
| Task 3.7's command (layer root first, `python` appended) | Diff: 21 insertions, one 7-line capsfilter block in each of the `arm64_cpu`, `x86_64` and `x86_64_nvidia` `folder_capture` documents |
| `test_bug_catalog_preservation.py`, `test_catalog_stream_and_analytics.py` and the two zero-trigger files, after both refreshes | 49 passed |

**Dell canary (finding 13).** See "Lab canary" above. A header parse (the
SOF and APPn markers) and Pillow agree: `test-normal-1.jpg` and thor1's
`test-normal-8.jpg` are both baseline 576x768 4:4:4 JPEGs with EXIF
orientation 1.

**Build gate body (finding 10).** Task 5's extraction of `build-custom.sh`'s
in-image gate body was run on the worktree. It gives 141 lines, from
`set -e` to the dependency audit gate's "passed" echo, with no escaped
quote outside a comment, and `bash -n` passes. It wasn't run in a
container: it pip-installs its test packages (`pytest` isn't in the JP5
image), which needs network. Review 4 (finding 1) then showed that under
its `set -e` the body stops early. Tasks 2 and 5 now run a derived copy
("Design iteration 5"). Review 5 (finding 1) showed that it stops at the
first block in the JP5 image, not at the stream gate, and that the
read-only mount this paragraph planned breaks the conftest-loading suites,
so the copy now runs against a writable copy of the tree ("Design
iteration 6").

**C1b stop-at-first loop (finding 6).** A throwaway copy of the revised
loop ran over fake segment lists:
- a file-first list moves the feed segment to index 0, and the others keep
  their order and identity;
- when the first frame-feed segment is already at index 0, the same list
  object comes back, even with a second feed segment later;
- with frame-feed segments at index 1 and 3, only the first one moves;
- a non-feed appsrc head, and every other arch id (JP5, JP6, JP7, `sim`,
  unknown), return the input list.

**Other facts checked.**
- thor1 1.0.56's `pipeline-string.txt` starts with `starting pipeline: `.
  Its only run-specific values are `location=` (the canary path) and
  `buffer-message-id=` (the capture id), and its node ids are `n1`, `py1`
  and `n3` (finding 11).
- thor1's 1.24.2 probes: a direct `jpegdec ! video/x-raw,format=…` request
  is `not-negotiated` for the 4:4:4 canary (RGB, BGR, RGBx) and for a
  generated 4:2:0 JPEG (RGB, BGR), with `jpegdec`'s src caps I420 each
  time; the I420 request passes (finding 5).
- The Dell's diag-narrowing cases 3 and 10 (`jpegdec ! videoconvert !
  fakesink`) fail on 1.0.51, for the canary and for a generated JPEG
  (finding 4).
- `test_vendored_catalog_mirror.py` has three tuples. The vendor README's
  "Current scope" paragraph names only the first two (finding 7).
- Lab Dell GPU upgrade (tasks 8 and 9):
  - `detect_arch.sh` returns `x86_64_nvidia` on x86 when `nvidia-smi` runs,
    `/proc/driver/nvidia/version` exists, or `lspci` lists an NVIDIA VGA or
    3D controller;
  - `recipe-amd64.yaml` runs `get_nvidia_libs_versions.sh`, which picks the
    `generic` Docker profile on every x86 host;
  - `Dockerfile.x86_64_nvidia` is
    `nvidia/cuda:12.6.3-cudnn-devel-ubuntu22.04`;
  - Volta (sm_70) support in that image's GPU libraries was not
    investigated (task 9's open item).

## Design iteration 5 evidence (2026-10-10)

Same host and rules as above:
- scratch files lived under `$HOME/dda-tmp/` and were deleted afterwards;
- no container ran. Docker was used read-only (`docker images`, `docker
  image inspect`), and nothing was pulled, loaded, deleted or retagged;
- no repo file was written outside this spec directory: `git status
  --porcelain` afterwards showed only the untracked spec directory;
- no device was contacted, and no AWS call was made.

**Image labels (review 4, findings 2 and 3).** The Image rule gave the
same IDs as iteration 4.

| Label | Matches | Image ID | Architecture |
|---|---|---|---|
| `flask-app:latest` (JP5, R2) | 1 | `f2e221c3e2d1` | arm64 |
| `flask-app:1.0.46` (JP6, R2) | 1 | `1fa46843533f` | arm64 |
| `flask-app:arm64-1.1.0` (arm64 CPU, R2) | 1 | `6d5ce25b9e2d` | arm64 |
| `flask-app:arm64JP7-1.0.45` (JP7, R2) | 1 | `b365ed521490` | arm64 |
| `flask-app-amd64:extracted` | 1 | `f34e42c9acad` | amd64 |
| `dda-portal-test:py311` | 1 | `14250238405b` | arm64 |
| `ubuntu:20.04` | 1 | `674fd86895a6` | arm64 |
| `ubuntu:22.04` | 2 | `b8e6b596a324`, `e7a996328f5c` | amd64, arm64 |
| `ubuntu:24.04` | 1 | `95d16dfcd4ab` | arm64 |

- An image ID is a content hash. The same IDs mean the same images, with
  the OS, GStreamer and `gi` Python that iteration 4 recorded, so the
  probes weren't re-run. No R2 image is missing.
- The plain names `ubuntu:20.04`, `ubuntu:22.04` and `ubuntu:24.04` give
  `674fd86895a6`, `e7a996328f5c` and `95d16dfcd4ab`, all arm64. So task
  4.1's emulated row needs the amd64 image's ID (`b8e6b596a324` today).
- No build was running (`pgrep`), and `flask-app:latest` was still the
  JP5 image.
- Each Docker CLI call took about 25 s on this host.

**Gate body, derived copy (review 4, finding 1).** Task 5 item 5's
extraction and derivation ran on the worktree's `build-custom.sh`. Both
bodies then ran on the host, in a clean environment, with
`python${PYTHON_VERSION}` resolving to a stub (`PYTHON_VERSION=-stub`). So
no real test and no pip install ran. The stub printed its arguments. It
failed the backend unit tests' pytest (rc 1), the stream gate (rc 1) and
`repo_audit.py` (rc 3), and passed everything else.

| Check | Result |
|---|---|
| Verbatim extraction | 141 lines. Line 1 is `set -e`, the last line is the dependency gate's "passed" echo, and `bash -n` passes |
| `Running …` echoes in the body | 7, at body lines 22, 38, 57, 75, 95, 115 and 136: the stream gate, then the six audit gates. The pip install and the five backend unit test files come before the first one |
| `\|\|`, `&&` or `exit` outside comments | none |
| Derived copy | 142 lines. Line 2 is `set +e`, `bash -n` passes, and lines 3 onward equal the verbatim lines 2 onward |
| Derived copy, stubbed run | Ran to the last line, exit 0. Log order: `GATE-CMD-FAILED rc=1 line=9` (the unit tests' pytest); the stream gate's `Running` line, then `rc=1 line=24`; the injection gate's `Running` line, then `rc=3 line=40` (`repo_audit.py`); then the five other `Running` lines with no failure |
| Verbatim body, same stub | Stopped at its first failing command, exit 1, before any `Running` line |

What this shows:
- `set +e` plus the trap runs every gate.
- `line=` names the derived copy's line where a failing multi-line
  command starts.
- The exit status is 0 despite three failures, so only the log can be
  judged.
- A failure in the first block lands before the first `Running` line, and
  review 4's rule ("between its `Running` line and the next") assigns it
  to no gate. Task 5 item 5's pass rule now judges that block on its own.

**Dell canary, measured again (review 3, finding 13).** On 2026-10-09 the
Dell's `/aws_dda/cookies/test-normal-1.jpg` was confirmed to have sha256
`9fabe604d7a235f7d7ae8ca5e1f96f3438e8314d1afe4cd247d014078639a743`, the
same as the repo file. The repo copy was read on the host with a
dependency-free walk of its JPEG markers and with Pillow 11.3.0:

| Item | `datasets/cookie-dataset/test-images/test-normal-1.jpg` |
|---|---|
| Tracked, size, sha256 | yes; 503050 bytes; `9fabe604…a743` |
| Segments before SOS | APP0 JFIF, APP1 Exif (2744 bytes), APP2 ICC_PROFILE (552 bytes); no Adobe APP14 |
| Frame | SOF0 baseline, 8-bit, 576x768, 3 components |
| Components (id: sampling, quant table) | 1: 1x1, 0. 2: 1x1, 1. 3: 1x1, 1 |
| Pillow | mode RGB, `get_sampling` 0, not progressive |
| EXIF orientation | 1 |

So the canary's chroma subsampling is measured, not inferred: 4:4:4.

**Recorded-output commands, control re-run (review 3, findings 1 and
2).** Task 3.6's snippet and task 3.7's command were extracted verbatim
from tasks.md and run on a pristine `git archive` export of 48841a0's
workflow_core layer, without C1. Both exited 0. `catalog_baseline.json`
and `golden_zero_trigger_compilation.json` came out byte-identical to the
committed files, so the commands add no spurious delta. Review 4 measured
the C1 deltas: 36 and 21 insertions.

**Other facts checked.**
- `build-custom.sh` saves the image with `save_image_tar flask-app
  ./custom-build/$COMPONENT_NAME/flask-app.tar`, and zips it as
  `custom-build/<component>/flask-app.tar` inside `<component>-<arch>.zip`.
  So a `docker load` of the artifact applies `flask-app:latest` (finding
  3).
- `detect_arch.sh` reports `x86_64_nvidia` on an x86 host when
  `nvidia-smi` runs, `/proc/driver/nvidia/version` exists, or `lspci`
  lists an NVIDIA VGA or 3D controller. That is why task 8 must not re-run
  station quick setup after the GPU upgrade.

## Design iteration 6 evidence (2026-10-10)

Same host and rules as above:
- scratch files lived under `$HOME/dda-tmp/` and were deleted afterwards;
- no container ran. Docker was used read-only (`docker images`, `docker
  image inspect`), and nothing was pulled, loaded, deleted or retagged;
- no repo file was written outside this spec directory: `git status
  --porcelain` afterwards showed only the untracked spec directory;
- no device was contacted (the lab Dell is powered off for its GPU
  upgrade), and no AWS call was made.

**Image labels (review 3, finding 3).** The Image rule, applied to one
`docker images` listing, gave the same IDs as iterations 4 and 5. Every R2
image is present, so no stop was needed.

| Label | Matches | Image ID | Architecture |
|---|---|---|---|
| `flask-app:latest` (JP5, R2) | 1 | `f2e221c3e2d1` | arm64 |
| `flask-app:1.0.46` (JP6, R2) | 1 | `1fa46843533f` | arm64 |
| `flask-app:arm64-1.1.0` (arm64 CPU, R2) | 1 | `6d5ce25b9e2d` | arm64 |
| `flask-app:arm64JP7-1.0.45` (JP7, R2) | 1 | `b365ed521490` | arm64 |
| `flask-app-amd64:extracted` | 1 | `f34e42c9acad` | amd64 |
| `dda-portal-test:py311` | 1 | `14250238405b` | arm64 |
| `ubuntu:20.04` | 1 | `674fd86895a6` | arm64 |
| `ubuntu:22.04` | 2 | `b8e6b596a324`, `e7a996328f5c` | amd64, arm64 |
| `ubuntu:24.04` | 1 | `95d16dfcd4ab` | arm64 |

The plain name `flask-app:latest` still gives the JP5 image, and no build
was running (`pgrep`).

**Gate body against a writable copy of the tree (review 5, findings 1 and
2).** Review 5 ran the derived copy in the JP5 image (`f2e221c3e2d1`) with
`--network none` and its pip packages supplied offline. These are its
counts. Task 5 item 5's pass rule quotes them; this iteration didn't re-run
them.

| Block | Read-only worktree mount (V1) | Writable export of 48841a0 (V2) |
|---|---|---|
| First block (5 backend unit test files) | 63 errors at setup | 57 failed, 6 passed: `ImportError … is_stream_source_type …` through the image's `/app.py` |
| Stream camera components gate | 2 failed, 13 passed, 7 errors | The same |
| Injection: `repo_audit.py`; exploration; preservation | Pass; 11 errors; 142 passed, 10 skipped | Pass; 10 passed, 1 skipped; 142 passed, 10 skipped |
| Secrets: script; exploration | Pass; 8 errors | Pass; 5 passed, 3 skipped |
| IAM: script; exploration | Pass; 24 errors | Pass; 24 passed |
| S3: script; exploration and negative fixture | Pass; 20 errors, 3 skipped | Pass; 20 passed, 3 skipped |
| Docker: script; exploration and negative fixture | Pass; 19 errors | Pass; 19 passed |
| Dependency: script; exploration and negative fixture | Pass; 17 errors | Pass; 17 passed |

- The V1 errors all come from the backend conftest's session fixture:
  `OSError: [Errno 30] Read-only file system`.
- V2's trap lines were at derived lines 3, 9 and 24. Line 3 is the pip
  install, and it failed only because of review 5's offline stand-in.

What this iteration checked on the host:

| Check | Result |
|---|---|
| Task 5 item 5's extraction and derivation, verbatim, on the worktree | 141 and 142 lines. The derived line 2 is `set +e`, and `bash -n` passes on both |
| Derived-copy line numbers | The pip install is line 3 and the first block's pytest line 9. The `Running …` echoes are lines 23, 39, 58, 76, 96, 116 and 137 (body lines 22 to 136, as in iteration 5). The stream gate's pytest starts at line 24 |
| The copy line (`git ls-files -z --cached --others --exclude-standard \| tar …`), run from the worktree root into a scratch directory | 4562 files and no symlinks: the 4557 tracked files plus the 5 untracked spec files. No `.git`, 325 MiB. `src/backend` and `test/backend-test` equal the worktree's (`diff -rq`, `__pycache__` excluded). The copy holds the conftest's copy source and the JSON that `setUp` rewrites |
| Task 5 item 5's count line on that copy | Both sides 4562, and no `.git`: passes |
| Task 5 item 5's grep pattern on a synthetic pytest log | Keeps the `Running`, trap, `FAILED`/`ERROR` and summary lines (`… in 12.34s`, `… in 125.02s (0:02:05)`); drops the session header, the "short test summary info" header and the verbose per-test lines |
| `git status --porcelain` after the copy | Only the untracked spec directory |
| `test/backend-test/conftest.py` | The session autouse fixture `setup_teardown_actions` runs `shutil.copytree` from `<cwd>/test/backend-test/utils/inference_out_for_test/` to `<cwd>/test/backend-test/utils/tmp/` |
| `test/backend-test/local_server_base_test_case.py` | `setUp` opens `test/backend-test/utils/em-agent-id-testListImages.json` with `"w"` and rewrites it, then runs `from app import app` |
| `build-custom.sh`'s gate block | The build runs the body with `-v "$REPO_ROOT":/repo -w /repo`, a read-write mount of its own checkout. The body sets `PYTHONPATH=/repo/src/backend` |
| Pytest calls in the body | 9, so the grep's `^=+ .* in [0-9.]+s` term records 9 summary lines |

Not traced in this iteration (review 5's U2): why the first block's `from
app import app` resolves to the image's `/app.py` and not to
`/repo/src/backend/app.py`, which exists. No root `pytest.ini`,
`pyproject.toml` or `setup.cfg` pytest section changes `sys.path`.
**Traced later** (review 6, finding 1; re-measured in "Design iteration
7"):
- The repo root holds a tracked `__init__.py`, so pytest collects `/repo`
  as a package.
- While the first test sets up, pytest's `Package.setup` puts the
  package's parent, `/`, first on `sys.path`. That is where the image
  bakes its backend.
- From then on, backend packages not yet imported resolve to the image's
  copies. The first block therefore imports a mix: the tree's `utils`, and
  the image's `app`, `gstreamer` and `model`.
- The pass rule is unaffected. The same mechanism also broke the
  legacy-builder single-file runs, which now mount the tree at `/w/repo`.

**Dell canary, measured again (review 3, finding 13).** The tracked repo
copy `datasets/cookie-dataset/test-images/test-normal-1.jpg` was read on
the host with a dependency-free walk of its JPEG markers and with Pillow
11.3.0:

| Item | Result |
|---|---|
| Size and sha256 | 503050 bytes; `9fabe604d7a235f7d7ae8ca5e1f96f3438e8314d1afe4cd247d014078639a743`, the value confirmed for the Dell's `/aws_dda/cookies/test-normal-1.jpg` on 2026-10-09 |
| Segments before SOS | APP0 JFIF, APP1 Exif (2744 bytes), APP2 ICC_PROFILE (552 bytes) |
| Frame | SOF0 baseline, 8-bit, 576x768, 3 components |
| Components (id: sampling, quant table) | 1: 1x1, 0. 2: 1x1, 1. 3: 1x1, 1 |
| Pillow | Mode RGB, `get_sampling` 0, not progressive |
| EXIF orientation | 1 |

The measured chroma subsampling is 4:4:4, as in iterations 4 and 5.

**Other facts checked.**
- `recipe-amd64.yaml` line 136 names the artifact with a placeholder URI
  that ends in `aws.edgeml.dda.LocalServer.amd64-x86_64.zip`. Its install
  step loads `…/custom-build/aws.edgeml.dda.LocalServer.amd64/flask-app.tar`
  from the unarchived zip. So task 7 step 2 reads the URI from the
  published version's recipe and unzips only that tar (review 5, finding
  3).
- The lab Dell's GPU upgrade (tasks 8 and 9), re-read in the code:
  - `detect_arch.sh`'s `_detect_has_nvidia_gpu` returns true when
    `nvidia-smi` runs, `/proc/driver/nvidia/version` exists, or `lspci`
    lists an NVIDIA VGA or 3D controller;
  - `src/host_scripts/get_nvidia_libs_versions.sh` writes
    `DOCKER_PROFILE='tegra'` only on aarch64 with CUDA present, and
    `generic` otherwise;
  - `src/backend/Dockerfile.x86_64_nvidia` line 21 is `FROM
    nvidia/cuda:12.6.3-cudnn-devel-ubuntu22.04`;
  - Volta (sm_70) support in that image stays task 9's open item, not
    investigated.
- MediaMTX's RTP and RTCP listeners (task 8, 3c-stream) were not checked
  offline: the image isn't local, and nothing may be pulled in this run.
  Task 8 sets both addresses to 127.0.0.1 and checks every listener with
  `ss` on the Dell before the stream is used.

## Design iteration 7 evidence (2026-10-12)

Same host and rules as above:
- Scratch files lived under `$HOME/dda-tmp/` and were deleted afterwards.
  They included a `git archive` export of 48841a0: `src/backend`,
  `test/backend-test`, the root `__init__.py`, `pyproject.toml`,
  `setup.cfg`, and the workflow_core layer.
- Every container was an existing local image, run by ID with `--rm
  --pull never --network none`. Nothing was pulled, loaded, deleted or
  retagged.
- The test packages came from a read-only offline stand-in on
  `PYTHONPATH`, in place of the commands' `pip install`: the host's
  pure-Python pytest 8.4.2, pluggy 1.6.0, hypothesis 6.141.1, sarge 0.1.8,
  testfixtures 8.3.0 and their dependencies.
- No repo file was written outside this spec directory. `git status
  --porcelain` afterwards showed only the untracked spec directory.
- No device was contacted (the lab Dell is powered off for its GPU
  upgrade), and no AWS call was made. No build was running (`pgrep`).

**Image labels (review 3, finding 3).** Task 4's Image rule was applied to
one `docker images` listing, then its probe was run by ID:

| Label | Matches | Image ID | Reports (by ID, `--network none`) |
|---|---|---|---|
| `flask-app:latest` (JP5, R2) | 1 | `f2e221c3e2d1` | Ubuntu 20.04.6 / GStreamer 1.16.3 / aarch64 |
| `flask-app:1.0.46` (JP6, R2) | 1 | `1fa46843533f` | Ubuntu 22.04.3 / 1.20.3 / aarch64 |
| `flask-app:arm64-1.1.0` (arm64 CPU, R2) | 1 | `6d5ce25b9e2d` | Ubuntu 22.04.5 / 1.20.3 / aarch64 |
| `flask-app:arm64JP7-1.0.45` (JP7, R2) | 1 | `b365ed521490` | Ubuntu 24.04.3 / 1.24.2 / aarch64 |
| `flask-app-amd64:extracted` | 1 | `f34e42c9acad` | not probed |
| `dda-portal-test:py311` | 1 | `14250238405b` | not probed |
| `ubuntu:20.04`, `ubuntu:24.04` | 1 each | `674fd86895a6`, `95d16dfcd4ab` | not probed |
| `ubuntu:22.04` | 2 | `b8e6b596a324`, `e7a996328f5c` | not probed |

These are the same IDs as in iterations 4 to 6. The plain name
`flask-app:latest` still gives the JP5 image. Every R2 image is present
and reports its row's versions, so no STOP was needed.

**Tasks 3.6 and 3.7 (review 3, findings 1 and 2), verbatim.** A script
extracted task 3.6's snippet and task 3.7's command from tasks.md. Both
ran on a pristine `git archive` export of 48841a0's workflow_core layer,
on this aarch64 host (Python 3.9.5):

| Procedure | Result |
|---|---|
| Task 3.6's snippet (18 lines, on stdin, from the layer directory) | exit 0. The round-trip assertion holds, and `catalog_baseline.json` is byte-identical to 48841a0's afterwards. It holds 15 literal em dashes |
| Task 3.7's command, with the golden deleted first | exit 0, no `ModuleNotFoundError`. The re-created `golden_zero_trigger_compilation.json` is byte-identical to 48841a0's and holds 0 `appsrc` |

The C1-applied diffs weren't re-run here. They are reviews 4 to 6's
measurements: 36 insertions, 0 deletions and 6 capsfilter blocks for 3.6,
and 21 insertions in the three constrained `folder_capture` documents for
3.7.

**Legacy-builder mount (review 6, finding 1).** These runs used the JP5
image (`f2e221c3e2d1`) on the export, with the Overview command's shape;
the offline stand-in replaced its `pip install`. A small scratch pytest
plugin reported where four modules were loaded from: `gstreamer`,
`gstreamer.pipeline_builder`, `model.image_source` and `utils.constants`.
It also logged a stack whenever `/` entered `sys.path`.

| Run | Result | `gstreamer.pipeline_builder` from | `model.image_source`, `utils.constants` from |
|---|---|---|---|
| A: `test_pipeline_builder.py` alone, tree at `/repo` | 12 passed | `/gstreamer/pipeline_builder.py` (the image's) | `/repo/src/backend/…` |
| B: the same, tree at `/w/repo` | 12 passed | `/w/repo/src/backend/gstreamer/pipeline_builder.py` | `/w/repo/src/backend/…` |
| C: a throwaway guard case (1(h)'s assertion, in the file's class shape), `/repo` | 1 failed: "the legacy cases must test the tree's builder: /gstreamer/pipeline_builder.py" | `/gstreamer/pipeline_builder.py` | `/repo/src/backend/…` |
| D: the guard case, `/w/repo` | 1 passed | the tree's | the tree's |

- The spy caught `insert(0, '/')` in run A's first setup phase. The stack:
  `_pytest/runner.py:164 pytest_runtest_setup` → `runner.py:514 setup` →
  `_pytest/python.py:668 setup` → `python.py:498 importtestmodule` →
  `_pytest/pathlib.py:583 import_path`.
- The image's `/gstreamer/pipeline_builder.py` is sha256 `8f0f7113…`,
  20107 bytes. The tree's is `bf7221c7…`, 20504 bytes. So run A's "12
  passed" tested the baked file. It passes only because both copies of
  `_add_file_image_source` are still identical (review 6).
- The repo root's `__init__.py` is tracked (588 bytes, added in d88c5b5).
  `pyproject.toml` and `setup.cfg` have no pytest section.
- The JP5 image holds `/app.py`, `/gstreamer`, `/model`, `/utils` and
  `/workflow_engine`.
- The whole `gstreamer/` directory wasn't re-run here. Review 6 measured
  it at 36 failed, 77 passed under both mounts, with the same ids.

**Provenance outside pytest (an extension of review 6's finding 1).** The
configured working directory of each of the five LocalServer images is
empty or `/` (`docker image inspect`). These runs used the JP5 image with
`PYTHONPATH=/repo/src/backend`, `COMPONENT_WORK_PATH` set, and the export
at `/repo`:

| Invocation | `workflow_engine.pipeline_executor` and `gstreamer.gst_pipeline` loaded from |
|---|---|
| `python3.11 -c …`, default working directory | `/workflow_engine/pipeline_executor.py`, `/gstreamer/gst_pipeline.py` (the image's) |
| `python3.11 /work/where.py` (by file path) | `/repo/src/backend/…` (the tree's) |
| `python3.11 -c …` with `-w /work` | `/repo/src/backend/…` (the tree's) |

`-c` puts the working directory first on `sys.path`, ahead of
`PYTHONPATH` (measured). Python documents the same for `-m` and stdin. A
script run by path puts its own directory there instead. Hence task 4's
Provenance rule.

**Task 5 item 5's revised block (review 6, finding 2).** Parts 1a, 1b and
3 ran as written from the worktree root. Part 2, the gate container, was
not run.
- Part 1a printed `scratch OK`.
- Part 1b printed the verbatim body's expected first and last two lines,
  141 and 142 lines, `derived copy OK`, and `tree copy OK: 4562 files`.
- Each STOP path was forced once:
  - a second part 1a with `$G` present printed its STOP line;
  - a copy with line 2 changed to `set -e` printed `STOP: derived copy`;
  - one extra file in the tree copy printed `STOP: tree copy 4563, git
    lists 4562`.
- Part 3 removed everything, and `git status --porcelain` showed only the
  untracked spec directory.

**Task 8's `ss` rule (review 6, finding 3).** This host's `ss -Hlntu`
prints six fields per line: the local address in field 5, and the peer
(`0.0.0.0:*`) in field 6. The rule ran verbatim, apart from the file path,
on synthetic `mediamtx` and `ffmpeg` lines in that format:

| Case | Verdict |
|---|---|
| tcp 127.0.0.1:8554, udp 127.0.0.1:8000 and udp 127.0.0.1:8001, plus an unrelated sshd line that the grep drops | `localhost only` |
| RTP on `0.0.0.0:8000` | STOP |
| An ffmpeg UDP socket on `0.0.0.0` | STOP |
| An IPv6 wildcard RTSP listener (`[::]:8554`) | STOP |
| Only the RTSP listener | STOP |
| No MediaMTX or ffmpeg line | STOP |

Not checked: how MediaMTX 1.9.3 and ffmpeg really open their sockets. The
image isn't local, and nothing may be pulled.

**Dell canary (review 3, finding 13), measured again.** The tracked repo
copy `datasets/cookie-dataset/test-images/test-normal-1.jpg` is
unmodified (last changed in bb3f564). It was read with a dependency-free
marker walk and with Pillow 11.3.0:

| Item | Result |
|---|---|
| Size and sha256 | 503050 bytes; `9fabe604d7a235f7d7ae8ca5e1f96f3438e8314d1afe4cd247d014078639a743`, the value confirmed for the Dell's `/aws_dda/cookies/test-normal-1.jpg` on 2026-10-09 |
| Segments before SOS | APP0 JFIF, APP1 Exif (2744 bytes), APP2 ICC_PROFILE (552 bytes), two DQT, SOF0, four DHT |
| Frame | SOF0 baseline, 8-bit, 576x768, 3 components |
| Components (id: sampling, quant table) | 1: 1x1, 0. 2: 1x1, 1. 3: 1x1, 1 |
| Pillow | Mode RGB, `get_sampling` 0, not progressive |
| EXIF orientation | 1 |

The measured chroma subsampling is 4:4:4, that is, no chroma subsampling,
as in iterations 4 to 6.

**The lab Dell's GPU upgrade (tasks 8 and 9), re-read in the code.**
- `station_install/quick_setup/detect_arch.sh`, `_detect_has_nvidia_gpu`
  (lines 88 to 100), returns true in three cases: `nvidia-smi` runs,
  `/proc/driver/nvidia/version` exists, or `lspci` lists an NVIDIA VGA or
  3D controller. On `x86_64` or `amd64` that gives `x86_64_nvidia` (lines
  119 to 123).
- `src/host_scripts/get_nvidia_libs_versions.sh` (lines 188 to 210)
  writes `DOCKER_PROFILE='tegra'` only on aarch64, and `generic`
  otherwise. `recipe-amd64.yaml` line 108 runs it.
- `src/backend/Dockerfile.x86_64_nvidia` line 21 is `FROM
  nvidia/cuda:12.6.3-cudnn-devel-ubuntu22.04`.
- Not investigated: whether that image's GPU libraries still support
  sm_70. That is task 9's open item. The fact that R580 is the last
  driver branch with Volta support was relayed with the upgrade plan, and
  wasn't checked against an NVIDIA source.

## Design iteration 8 evidence (2026-10-14)

Same host and rules as above:
- Scratch files lived under `$HOME/dda-tmp/` and were deleted afterwards.
  They included stand-in scripts, a small scratch tree for the mount
  checks, and a `git archive` export of 48841a0's workflow_core layer.
- Every container was an existing local image, run by ID with `--rm
  --pull never --network none`. Nothing was pulled, loaded, deleted or
  retagged, and no container was left behind. One container in the
  Created state predates these reviews and was left alone.
- No repo file was written outside this spec directory. `git status
  --porcelain` afterwards showed only the untracked spec directory.
- No device was contacted (the lab Dell is powered off for its GPU
  upgrade), and no AWS call was made. No build was running (`pgrep`).

**Image labels (review 3, finding 3).** Task 4's Image rule was applied to
one `docker images` listing, then its probe was run by ID:

| Label | Matches | Image ID | Reports (by ID, `--network none`) |
|---|---|---|---|
| `flask-app:latest` (JP5, R2) | 1 | `f2e221c3e2d1` | Ubuntu 20.04.6 / GStreamer 1.16.3 / aarch64 |
| `flask-app:1.0.46` (JP6, R2) | 1 | `1fa46843533f` | Ubuntu 22.04.3 / 1.20.3 / aarch64 |
| `flask-app:arm64-1.1.0` (arm64 CPU, R2) | 1 | `6d5ce25b9e2d` | Ubuntu 22.04.5 / 1.20.3 / aarch64 |
| `flask-app:arm64JP7-1.0.45` (JP7, R2) | 1 | `b365ed521490` | Ubuntu 24.04.3 / 1.24.2 / aarch64 |
| `flask-app-amd64:extracted` | 1 | `f34e42c9acad` | not probed; its backend is below |

These are the same IDs as in iterations 4 to 7. Every R2 image is present
and reports its row's versions, so no STOP was needed.

**Tasks 3.6 and 3.7 (review 3, findings 1 and 2), verbatim.** A script
extracted task 3.6's snippet and task 3.7's command from tasks.md. Both
ran on a pristine `git archive` export of 48841a0's workflow_core layer,
on this aarch64 host (Python 3.9.5):

| Procedure | Result |
|---|---|
| Task 3.6's snippet (18 lines, on stdin, from the layer directory) | Exit 0. The round-trip assertion holds, and `catalog_baseline.json` is byte-identical to 48841a0's afterwards: 15 literal em dashes, no `\u2014` escape |
| Task 3.7's command, with the golden deleted first | Exit 0, no `ModuleNotFoundError`. The re-created `golden_zero_trigger_compilation.json` is byte-identical to 48841a0's and holds 0 `appsrc` |

The C1-applied diffs weren't re-run here. They are review 7's re-run:
36 insertions, 0 deletions and 6 capsfilter lines for 3.6, and 21
insertions in the three constrained `folder_capture` documents for 3.7.

**Dell canary (review 3, finding 13), measured again offline.** The
tracked repo copy `datasets/cookie-dataset/test-images/test-normal-1.jpg`
is unmodified (last changed in bb3f564). It was read with a
dependency-free marker walk and with Pillow 11.3.0:

| Item | Result |
|---|---|
| Size and sha256 | 503050 bytes; `9fabe604d7a235f7d7ae8ca5e1f96f3438e8314d1afe4cd247d014078639a743`, the value confirmed for the Dell's `/aws_dda/cookies/test-normal-1.jpg` on 2026-10-09 |
| Segments before SOS | APP0 JFIF, APP1 Exif (2744 bytes), APP2 ICC_PROFILE (552 bytes), two DQT, SOF0, four DHT |
| Frame | SOF0 baseline, 8-bit, 576x768, 3 components |
| Components (id: sampling, quant table) | 1: 1x1, 0. 2: 1x1, 1. 3: 1x1, 1 |
| Pillow | Mode RGB, 576x768, `get_sampling` 0, not progressive |
| EXIF orientation | 1 |

The measured chroma subsampling is 4:4:4, that is, no chroma subsampling,
as in iterations 4 to 7.

**The legacy golden capture (review 7, finding 1).** A scratch tree held
a stand-in for the capture-or-assert helper under
`test/backend-test/gstreamer/`. The stand-in resolves
`goldens/jpegdec_native_caps/` from `__file__` and calls
`os.makedirs(..., exist_ok=True)`, as the cited helper does. It writes the
golden when absent and asserts it when present, then tries one write
outside the goldens directory. Each run used the JP5 image
(`f2e221c3e2d1`) with the scratch tree mounted read-only at `/w/repo`:

| Run | Result | Owners on the host afterwards |
|---|---|---|
| Plus the goldens-only read-write mount, without the host `mkdir` | Captured; the outside write failed with `[Errno 30] Read-only file system`; exit 0 | `goldens/`, `jpegdec_native_caps/` and the golden: all 0:0. Docker created the missing source directory itself, as root |
| The same with the host `mkdir -p` first (task 2's shape) | Captured; outside write `Read-only file system`; exit 0 | `goldens/` 1000:1000; `jpegdec_native_caps/` and the golden 0:0 |
| Task 2's `chown` container | Exit 0 | All 1000:1000; `find ! -user` lists 0 entries |
| The plain read-only mount | Asserted; outside write `Read-only file system`; exit 0 | Unchanged |

So the review's reason for the `mkdir` ("the mount point must exist")
doesn't hold on this host. Task 2 keeps the `mkdir`, so that the directory
belongs to the host user from the start, and its `chown` covers what the
run creates inside it. The real pytest capture wasn't run, because it
needs the network for `pip`.

**A row that aborts (review 7, finding 3).** A stand-in row prints one
provenance line with a plain `print` and one line to stderr. It then
unsets the caps of a PLAYING appsrc and pushes a buffer: the production
abort path when `Gst.Caps.from_string` returns None in `create_buffer`.
In the amd64 image it calls `os.abort()` instead, which avoids starting
GStreamer under emulation.

| Image | Command | Exit | Provenance line |
|---|---|---|---|
| JP5 `f2e221c3e2d1` | iteration 7's row shape (Python is the container's PID 1) | **133** | Lost. stdout held only GLib's `Bail out! … gstappsrc.c:1264:gst_app_src_create: assertion failed` line |
| JP5 | the row command (`--init -e PYTHONUNBUFFERED=1`) | **134** | Kept. Python ran as PID 7 |
| arm64 CPU `6d5ce25b9e2d` | the row command | **134** | Kept. `gstappsrc.c:1725:gst_app_src_create: code should not be reached` |
| amd64 `f34e42c9acad` (emulated), `os.abort()` | the row command with `--platform linux/amd64` | **134** | Kept. qemu printed `uncaught target signal 6 (Aborted)`, and the container ended |

The PID-1 shape under emulation wasn't re-run, because review 7 measured
that it hangs.

**The local amd64 image's backend (review 7, finding 2).** In
`f34e42c9acad` (emulated), `/app.py`, `/gstreamer`, `/model` and `/utils`
are present and `/workflow_engine` is absent. `find / -xdev` finds no
`pipeline_executor.py`, `python_bridge.py`, `rendering.py` or
`workflow_engine` anywhere. The JP5 and arm64 CPU stand-in rows above
reported `/workflow_engine` present.

**Workspace removal (review 7, NIT 6).** A container wrote a nested
directory and two files into a scratch workspace, so they were owned by
uid 0. The host user's `rm -rf` then failed with `Permission denied` and
left 4 entries. Task 4's container `find /g -mindepth 1 -delete`,
followed by `rmdir`, removed the workspace, and the check printed
`workspace removed`.

**Task 8's `ss` rule (review 7, NIT 4).** Three blocks ran on synthetic
`mediamtx`, `ffmpeg` and `sshd` lines in this host's `ss -H` format, each
through the block's own `grep`: the iteration-7 block, review 7's proposed
block, and iteration 8's block (now in tasks.md).

| Case | Iteration 7 | Review 7 | Iteration 8 |
|---|---|---|---|
| The expected three, plus an `sshd` line the grep drops | localhost only | localhost only | localhost only |
| RTP on `0.0.0.0:8000` | STOP | STOP | STOP |
| An extra ffmpeg UDP socket on `0.0.0.0` | STOP | STOP | STOP |
| An IPv6 wildcard RTSP listener | STOP | STOP | STOP |
| Only the RTSP listener | STOP | STOP | STOP |
| No `mediamtx` or `ffmpeg` line | STOP | STOP | STOP |
| An extra ffmpeg UDP socket on 127.0.0.1 | **localhost only** | STOP | STOP |
| An extra MediaMTX listener on 127.0.0.1:9997 | **localhost only** | STOP | STOP |
| The RTSP line duplicated | STOP | STOP | STOP |
| RTSP twice, RTCP missing | **localhost only** | **localhost only** | STOP |
| RTP twice, RTSP missing | **localhost only** | **localhost only** | STOP |

Not checked: how MediaMTX 1.9.3 and ffmpeg really open their sockets. The
image isn't local, and nothing may be pulled.

**The lab Dell's GPU upgrade (tasks 8 and 9).** Unchanged since
iteration 7. Tasks 8 and 9 carry it as pending owner decisions, in the
recommended order:
- task 8 runs first, with the Dell deployed as an x86_64 CPU station on
  the amd64 LocalServer, without re-running station quick setup;
- then the Dell can optionally move to amd64Nvidia for an on-device task
  9.

The decision inputs are recorded there: R580 as the last driver branch
with Volta support (relayed, not checked against an NVIDIA source) and
the CUDA 12.6 base. Whether that image's GPU libraries support sm_70 is
task 9's open item, not investigated here.

## Pre-fix repo baselines (2026-10-09, unfixed tree at 48841a0)

These design-phase counts cover only a subset of task 5's commands (review
1, finding 7). Task 2 records the authoritative baseline for every task 5
command from a pristine 48841a0 worktree. Review 2 (finding 7) showed that
the whole `test/backend-test/workflow_engine/` directory needs
`--continue-on-collection-errors` on the host, because
`test_mqtt_publish_retain.py` fails to collect (`awsiot`); the per-file runs
below were not affected.

The host runs Python 3.9.5. Commands follow tasks.md's Overview.

| Suite | Result |
|---|---|
| `layers/workflow_core/tests/` | 1022 passed, **1 failed**, 1 skipped (116.75 s) |
| `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py` | 8 passed |
| `test/backend-test/csi_nvargus_optional/test_preservation_csi_surface.py` | 19 passed, **1 failed** |
| `test_workflow_pipeline_executor.py`, `test_folder_source_consumption_preservation.py`, `test_workflow_run_log.py` | 28 passed |
| `test/backend-test/gstreamer/test_pipeline_builder.py`, in `flask-app:latest` with python3.11 | 12 passed. This used the `/repo` mount, which loaded the image's baked builder (review 6, finding 1). Iteration 7 measured 12 passed against the tree's builder, mounted at `/w/repo` |
| The same file on the host | Collection error: the host's `gi` import fails through `aravis_functions`, so this file is container-only |

Both failures are pre-existing:
- `TestCatalogMirrorEquality::test_portal_and_vendor_catalog_init_are_byte_identical`
  fails because the vendor tree deliberately omits `catalog/platforms.py`
  (design D6).
- `test_backend_csi_consumer_file_untouched[…/vendor/workflow_core/catalog/nodes.py]`
  fails because that golden entry has been stale since 6bda4de. Task 3.8
  rebaselines it.

## Implementation verification (tasks 1 to 6), 2026-10-14

Tasks 1 to 5 ran on 2026-10-14 and are recorded below. Task 6's two
commits are made; the push is the release step after this run (Task 6).
Owner tasks 7 to 12 stay unchecked. Nothing here was verified on a device.

Rules for every run below:
- The host is aarch64 with Python 3.9.5, pytest 8.4.2, Hypothesis 6.141.1
  and Docker 29.6.1. Commands ran from the worktree root unless a `cd` is
  shown.
- Containers used existing local images only, by image ID, with `--rm
  --pull never`. Nothing was pulled, loaded, retagged or deleted, and no
  container was left behind (one container in the Created state predates
  this work and was left alone).
- Network: tasks 1, 2 and 4.1 ran their in-container `pip install` (the
  Overview command and the gate body) and 4.1's `apt-get` with the
  network, as their commands say. Every task 4 row and every task 5
  container ran with `--network none`; task 5's pip installs came from a
  local wheelhouse instead (Task 5).
- No device was contacted (the lab Dell is powered off), and no AWS call
  was made.
- Logs, row outputs and harness scripts are in the uncommitted lab
  evidence bundle, not in the repo.

**Images (task 4's Image rule).** Resolved at 02:03Z and again at 04:29Z,
with the same IDs; each R2 image was probed by ID with `--network none`.
No STOP was needed.

| Label | Matches | Image ID | Reports |
|---|---|---|---|
| `flask-app:latest` (JP5, R2) | 1 | `f2e221c3e2d1` | Ubuntu 20.04.6 / GStreamer 1.16.3 / aarch64; `gi` for python3.11 |
| `flask-app:1.0.46` (JP6, R2) | 1 | `1fa46843533f` | 22.04.3 / 1.20.3 / aarch64; `gi` for python3.10 |
| `flask-app:arm64-1.1.0` (arm64 CPU, R2) | 1 | `6d5ce25b9e2d` | 22.04.5 / 1.20.3 / aarch64; `gi` for python3.11 and python3.10 |
| `flask-app:arm64JP7-1.0.45` (JP7, R2) | 1 | `b365ed521490` | 24.04.3 / 1.24.2 / aarch64; `gi` for python3.11 |
| `flask-app-amd64:extracted` | 1 | `f34e42c9acad` | 20.04.6 / 1.16.3 / x86_64, emulated (4.2) |
| `dda-portal-test:py311` | 1 | `14250238405b` | the Portal function tests |
| `ubuntu:20.04`, `ubuntu:24.04` | 1 each | `674fd86895a6`, `95d16dfcd4ab` | arm64 (4.1, by name) |
| `ubuntu:22.04` | 2 | `e7a996328f5c` (arm64), `b8e6b596a324` (amd64) | 4.1: the native row by name, the emulated row by the amd64 ID |

### Task 1: exploration on the unfixed tree

The new test files were written and run before the first production edit:
test files 02:10Z to 02:21Z, goldens captured 02:23Z to 02:34Z, task 1's
offline rows 02:40Z to 02:42Z, first source edit 02:48Z (file
modification times).

| Run | Command | Result on the unfixed tree |
|---|---|---|
| Portal, new file | `cd edge-cv-portal/backend && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest layers/workflow_core/tests/test_jpegdec_native_caps.py -q -p no:cacheprovider -rf` | Run 1 (captures task 2's two Portal goldens): 60 failed, 207 passed. Run 2 (asserts them): the same 60 ids, 207 passed |
| Portal function file (c4) | the Overview's `dda-portal-test:py311` command with `tests/test_feed_segment_first_packaging.py` | 1 failed, 3 passed: `test_icam_slot_segment_follows_the_feed_first_order[x86_64-1]`, `assert 0 == 1` |
| Backend, new files | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_jpegdec_native_caps.py test/backend-test/workflow_engine/test_jpegdec_mixed_feed_matrix.py test/backend-test/workflow_engine/test_vendored_catalog_mirror.py --noconftest -q -p no:cacheprovider -rfE` | Run 1 (captures the backend goldens): 130 failed, 194 passed. Run 2: the same 130 ids, 194 passed |
| Legacy (h), JP5 image, tree at `/w/repo` | task 2's capture command, then the plain read-only Overview command (Task 2) | 2 failed, 14 passed in both. Both 1(h) cases fail at `the vendored JPEGDEC_NATIVE_CAPS is missing`, after their `inspect.getsourcefile` assertion passed |

The failing ids, by case. A stands for {`x86_64`, `x86_64_nvidia`,
`arm64_cpu`}; braces list the parameter values.
- Portal `test_jpegdec_native_caps.py`, 60:
  - (a), 4: `TestJpegdecNativeCapsConstant::test_constant_is_the_exact_design_string`,
    `…::test_constant_survives_args_template_resolution`,
    `…::test_arch_tuple_is_the_three_constrained_arches` and
    `…::test_compiler_shares_the_catalog_arch_tuple`;
  - (b), 6: `TestConstrainedCatalogChains::test_chain_pins_jpegdec_with_the_native_caps[{folder_source,csi_camera_source}-A]`;
  - (c), 1: `TestCompiledSourceChain::test_capsfilter_follows_jpegdec_with_the_source_node_id`;
  - (c2), 48: `TestFeedSegmentFirstCompile::test_feed_segment_moves_to_index_zero[{folder_source,csi_camera_source}+{aravis_camera_source,custom_python_source,rtmp_stream_source,rtsp_camera_source}+{split,fan_in}-A]`;
  - (c3), 1: `test_feed_segment_first_property`.
- Portal function file (c4), 1: the x86_64 case above.
- Backend `test_jpegdec_native_caps.py`, 5: (d)
  `test_constrain_jpegdec_output_properties`, (d2)
  `test_feed_segment_first_properties`, (e)
  `test_vendored_compile_renders_the_pinned_chain`, (f)
  `test_execute_pins_a_pre_fix_x86_64_chain` and (g)
  `test_executor_constants_are_the_vendored_ones`.
- Backend `test_jpegdec_mixed_feed_matrix.py`, 125:
  - (i), 25: `test_bug_row_consumer_yields_the_frame_caps[A-folder-{aravis1,aravis3}-file_first-{fixed-plain,fixed-bridged,prefix-bridged}]`,
    `[A-folder-{python,stream}-file_first-fixed-plain]` and
    `[x86_64-csi-aravis1-file_first-fixed-plain]`;
  - (i2), 50: `test_constrained_file_first_row_starts_with_the_fed_appsrc[A-folder-{aravis1,aravis3,python,stream}-file_first-{fixed,prefix}-{plain,bridged}]`
    and `[x86_64-csi-aravis1-file_first-{fixed,prefix}-plain]`;
  - (i3), 50: `test_pre_fix_shape_row_pins_each_file_chain_once[A-folder-{aravis1,aravis3,python,stream}-{file_first,feed_first}-prefix-{plain,bridged}]`
    and `[x86_64-csi-aravis1-{file_first,feed_first}-prefix-plain]`.
- Legacy (h), 2: `TestPipelineBuilder::test_folder_input_pins_jpegdec_on_x86_64`
  and `…::test_folder_input_pins_jpegdec_on_arm64_cpu`.

Every matrix row also asserts that its execution row is `completed`, so
these are consumer failures, not fixture failures.

Offline counterexamples, in throwaway containers under task 4's Workspace
and Image rules:
- **Stock `ubuntu:22.04`** (the native arm64 image, by name, `--pull
  never`), task 4.1's script in unfixed mode: aarch64, Ubuntu 22.04.5,
  GStreamer 1.20.3, plugins-good 1.20.3-0ubuntu1.10, libjpeg-turbo8
  2.1.2-0ubuntu1. `jpegdec`'s SRC template lists I420, RGB, BGR, RGBx,
  xRGB, BGRx, xBGR and GRAY8.
  - The unfixed chain U failed 8 of the 12 kinds: the 4:4:4 kinds
    (`g444`, `c1x1`), both grayscale kinds (`ggray`, `cgray`), the
    RGB-colourspace kind (`crgb`), the 577x769 4:2:0 kind (`godd420`),
    and `c1x2` and `c4x1`. It passed `g420`, `g422`, `cprog` and `c2x1`.
    `jpegdec` negotiated RGB in every U run.
  - U0 failed the same 8 kinds, negotiating xBGR.
  - With `GST_DEBUG=jpegdec:5` the 4:4:4 U run logged `format_convert=1`
    and `No valid frames decoded before end of stream`. Adding
    `videodecoder:4` showed `Decode error #21: Improper call to JPEG
    library in state 205`.
- **The bridged run-on shape of bugfix 1.6**, arm64 CPU image
  `6d5ce25b9e2d`, the unfixed tree at `/w/repo`, the row command. The
  provenance lines named `/w/repo/src/backend/workflow_engine/pipeline_executor.py`,
  `/w/repo/src/backend/workflow_engine/python_bridge.py` and
  `/w/repo/src/backend/gstreamer/gst_pipeline.py`. `_fed_frame_caps`
  returned the run-on clause `video/x-raw,format=RGB appsrc
  name=py_out_py1 is-live=true format=time block=true,width=64,height=48`.
  - 3 bytes/pixel: exit **134**, `Bail out!
    GStreamer-App:ERROR:../gst-libs/gst/app/gstappsrc.c:1725:gst_app_src_create:
    code should not be reached`.
  - 1 byte/pixel: exit 1, `PipelineExecutionException: Frame buffer
    cannot be reconciled with the caps it is pushed under`.

### Task 2: preservation and baselines on the unfixed tree

**New preservation tests** (the same runs as task 1: the first captured,
the second asserted):
- Portal `test_jpegdec_native_caps.py`, 207 passed: the unconstrained
  mappings golden, its `catalog_baseline.json` guard, the unconstrained
  mixed-compiles golden, and 204 cases where a feed-first or single-source
  graph compiles the same with `_feed_segment_first` patched to identity.
- Portal function file: `test_icam_slot_addresses_the_v4l2src_device_argument[x86_64]`
  and `[arm64_jp5]` passed, as did c4's `arm64_jp5` case.
- Backend, 194 passed: the 6 single-source launch-string goldens; the 54
  Jetson mixed-row goldens (`mixed/`); the 75 preserved constrained rows
  (P8); the 50 fixed-shape capsfilter-count rows (AC 12); and the mirror
  test's 9 cases, including the new
  `test_vendored_shared_module_is_byte_identical_to_portal_copy[compiler/compiler.py]`.
- Legacy goldens, JP5 image `f2e221c3e2d1`, tree at `/w/repo`: task 2's
  capture command as written (the host `mkdir -p`, then the Overview
  command plus a read-write mount of only
  `test/backend-test/gstreamer/goldens/`). The two golden cases passed and
  wrote `folder_input_arm64_jp5.txt` and `folder_input_jp6_png.txt`; 1(h)'s
  two cases failed, as task 1 expects. The `chown` container exited 0, and
  the `find` line printed `goldens owned by the host user`. The plain
  read-only run then asserted both goldens: 14 passed, the same 2 failed.

**Frozen sha256** of the six new or updated test files and the 64 goldens,
taken before the first production edit. Task 5 compares against these.

```text
7633b4fb44e7cdd57078468383c499197ffc3f7107df03afebc4fda532db4dd8  edge-cv-portal/backend/layers/workflow_core/tests/test_jpegdec_native_caps.py
fbc54fbd7fd1c286fa54d78a78fc3f2af379a9597fc24fdcea873ec41db7acde  edge-cv-portal/backend/tests/test_feed_segment_first_packaging.py
85dc9a362698996c9386c3025f06d69453d1042b1dea0a4b273586e998b06a74  test/backend-test/workflow_engine/test_jpegdec_native_caps.py
5678cb4f8463232ee7ec0b67b375911a0b5420476f6ed94b66656d7d9f5d7f77  test/backend-test/workflow_engine/test_jpegdec_mixed_feed_matrix.py
6b75b929683a14bf5b228a92bd2cef4eb3544e67b75c9fe5205c71d03687599f  test/backend-test/workflow_engine/test_vendored_catalog_mirror.py
962bb61ec3187f06b59d6686f3c9822cc1bfee68a928154b2466742be6a4aa38  test/backend-test/gstreamer/test_pipeline_builder.py
2afc8539d05422e272c26d5877f5438f6585b0ef8a99df159e1676dd6f01a7c8  edge-cv-portal/backend/layers/workflow_core/tests/goldens/jpegdec_native_caps/unconstrained_mappings.json
c20556d1bc3d54b3105878cf27cae417225236ad683bfaa393bace7e8618f8da  edge-cv-portal/backend/layers/workflow_core/tests/goldens/jpegdec_native_caps/unconstrained_mixed_compiles.json
137d88435e88d8506671c0d24680ae9d094b53e2b58fc2ddb6fbcf2260830b25  test/backend-test/gstreamer/goldens/jpegdec_native_caps/folder_input_arm64_jp5.txt
559e20c21106382e42e9d501276cf8672abea2684d401a9129f336ee8be1a409  test/backend-test/gstreamer/goldens/jpegdec_native_caps/folder_input_jp6_png.txt
85feb3b8d87bcac3a35c9e08accd446ce1b7f437d216c598621a2745d0a091e3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/arm64_jp5_catalog_chain.txt
5db6c480bac13588ac05e327bdebe78455048168542b6c373c47d7f603c0f384  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/arm64_jp6_pngdec_chain.txt
85feb3b8d87bcac3a35c9e08accd446ce1b7f437d216c598621a2745d0a091e3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/arm64_jp7_catalog_chain.txt
b4f1c7d9c3ecd8625646a375a9c85ad2e14158ab3ad7989adc093dbd1a0d5153  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-csi-aravis1-feed_first-prefix-plain.txt
600709e10f2493819b0cbe6693922006d262b0e6f55de3faedf3e1d67fd547a2  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-csi-aravis1-file_first-prefix-plain.txt
f1eff0ae01975a469576bb7329691a54e489977bf524c8438c5971b038d4a657  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis1-feed_first-prefix-bridged.txt
65d5eed09ddc1b11805ee2cf9b2b1da4eed64c671530dbfd8651fd9db4ed594e  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis1-feed_first-prefix-plain.txt
d0e8a1839709f3c22386c47d6b3715217a046cd1c2042f6c6c40dced23922b55  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis1-file_first-prefix-bridged.txt
24731bc6d15aff3b437bb391e465109fd0bacb65996150344917f621eea0bed4  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis1-file_first-prefix-plain.txt
23516b9b3c8d985bded96fba487f5b5cb4f238e34744adae27b81bad000946f3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis3-feed_first-prefix-bridged.txt
0b72702c3723fa5aa3c30b6ff6be9ce5c0c066a44a9650937f7072994e32c05a  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis3-feed_first-prefix-plain.txt
ac69036a8f3a495474571d9c64c295c8a66ba5dd547f93b60942939d5e351a92  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis3-file_first-prefix-bridged.txt
64b8573aad44cafde180246a79dba5e3d6fbd996072fe54286505d709a9bda58  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-aravis3-file_first-prefix-plain.txt
23516b9b3c8d985bded96fba487f5b5cb4f238e34744adae27b81bad000946f3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-python-feed_first-prefix-bridged.txt
0b72702c3723fa5aa3c30b6ff6be9ce5c0c066a44a9650937f7072994e32c05a  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-python-feed_first-prefix-plain.txt
ac69036a8f3a495474571d9c64c295c8a66ba5dd547f93b60942939d5e351a92  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-python-file_first-prefix-bridged.txt
64b8573aad44cafde180246a79dba5e3d6fbd996072fe54286505d709a9bda58  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-python-file_first-prefix-plain.txt
23516b9b3c8d985bded96fba487f5b5cb4f238e34744adae27b81bad000946f3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-stream-feed_first-prefix-bridged.txt
0b72702c3723fa5aa3c30b6ff6be9ce5c0c066a44a9650937f7072994e32c05a  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-stream-feed_first-prefix-plain.txt
ac69036a8f3a495474571d9c64c295c8a66ba5dd547f93b60942939d5e351a92  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-stream-file_first-prefix-bridged.txt
64b8573aad44cafde180246a79dba5e3d6fbd996072fe54286505d709a9bda58  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp5-folder-stream-file_first-prefix-plain.txt
d34c446cd06c787c142907390f38f2bce3eb12e6ddbc116a7b9eaaaa4ff2ddde  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-csi-aravis1-feed_first-prefix-plain.txt
8acc4cf5497821541d78f5a076d90382dcc7795a86e9106cce8f5792342e904c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-csi-aravis1-file_first-prefix-plain.txt
659a8ee9804c000d6c7a471bfdc4be40d42f8a47f032198d0122678119fd0db7  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis1-feed_first-prefix-bridged.txt
bf84585813bbd7152696b211885878afad234073a5c77e0478a1ee97aff6ac43  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis1-feed_first-prefix-plain.txt
12899fa41e73c0294344e2e5764f1ce6675ec426af6ac4f5da95e6c35eed0165  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis1-file_first-prefix-bridged.txt
ded4b32cd5a4457b54e7d1f5a4cc7d44ab91118867981637e6425aa93a35b949  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis1-file_first-prefix-plain.txt
5d5caf363d336f4532ac3a5cb56560f46636d30038bffc3ff3756ffb2aae0d8c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis3-feed_first-prefix-bridged.txt
71e9da8abd2c1dd939dee1fabc36500fc2ac0df354447124f056e56a5054b1c4  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis3-feed_first-prefix-plain.txt
2e50ba85515211f87706acc543c3c95dd5185e63d462ad1cd0564a3a9d6ffc9c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis3-file_first-prefix-bridged.txt
f2761e11b0479185751075b5799a5549446345eecf0fdd8991244c4776153d22  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-aravis3-file_first-prefix-plain.txt
5d5caf363d336f4532ac3a5cb56560f46636d30038bffc3ff3756ffb2aae0d8c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-python-feed_first-prefix-bridged.txt
71e9da8abd2c1dd939dee1fabc36500fc2ac0df354447124f056e56a5054b1c4  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-python-feed_first-prefix-plain.txt
2e50ba85515211f87706acc543c3c95dd5185e63d462ad1cd0564a3a9d6ffc9c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-python-file_first-prefix-bridged.txt
f2761e11b0479185751075b5799a5549446345eecf0fdd8991244c4776153d22  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-python-file_first-prefix-plain.txt
5d5caf363d336f4532ac3a5cb56560f46636d30038bffc3ff3756ffb2aae0d8c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-stream-feed_first-prefix-bridged.txt
71e9da8abd2c1dd939dee1fabc36500fc2ac0df354447124f056e56a5054b1c4  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-stream-feed_first-prefix-plain.txt
2e50ba85515211f87706acc543c3c95dd5185e63d462ad1cd0564a3a9d6ffc9c  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-stream-file_first-prefix-bridged.txt
f2761e11b0479185751075b5799a5549446345eecf0fdd8991244c4776153d22  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp6-folder-stream-file_first-prefix-plain.txt
b4f1c7d9c3ecd8625646a375a9c85ad2e14158ab3ad7989adc093dbd1a0d5153  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-csi-aravis1-feed_first-prefix-plain.txt
600709e10f2493819b0cbe6693922006d262b0e6f55de3faedf3e1d67fd547a2  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-csi-aravis1-file_first-prefix-plain.txt
f1eff0ae01975a469576bb7329691a54e489977bf524c8438c5971b038d4a657  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis1-feed_first-prefix-bridged.txt
65d5eed09ddc1b11805ee2cf9b2b1da4eed64c671530dbfd8651fd9db4ed594e  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis1-feed_first-prefix-plain.txt
d0e8a1839709f3c22386c47d6b3715217a046cd1c2042f6c6c40dced23922b55  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis1-file_first-prefix-bridged.txt
24731bc6d15aff3b437bb391e465109fd0bacb65996150344917f621eea0bed4  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis1-file_first-prefix-plain.txt
23516b9b3c8d985bded96fba487f5b5cb4f238e34744adae27b81bad000946f3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis3-feed_first-prefix-bridged.txt
0b72702c3723fa5aa3c30b6ff6be9ce5c0c066a44a9650937f7072994e32c05a  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis3-feed_first-prefix-plain.txt
ac69036a8f3a495474571d9c64c295c8a66ba5dd547f93b60942939d5e351a92  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis3-file_first-prefix-bridged.txt
64b8573aad44cafde180246a79dba5e3d6fbd996072fe54286505d709a9bda58  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-aravis3-file_first-prefix-plain.txt
23516b9b3c8d985bded96fba487f5b5cb4f238e34744adae27b81bad000946f3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-python-feed_first-prefix-bridged.txt
0b72702c3723fa5aa3c30b6ff6be9ce5c0c066a44a9650937f7072994e32c05a  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-python-feed_first-prefix-plain.txt
ac69036a8f3a495474571d9c64c295c8a66ba5dd547f93b60942939d5e351a92  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-python-file_first-prefix-bridged.txt
64b8573aad44cafde180246a79dba5e3d6fbd996072fe54286505d709a9bda58  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-python-file_first-prefix-plain.txt
23516b9b3c8d985bded96fba487f5b5cb4f238e34744adae27b81bad000946f3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-stream-feed_first-prefix-bridged.txt
0b72702c3723fa5aa3c30b6ff6be9ce5c0c066a44a9650937f7072994e32c05a  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-stream-feed_first-prefix-plain.txt
ac69036a8f3a495474571d9c64c295c8a66ba5dd547f93b60942939d5e351a92  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-stream-file_first-prefix-bridged.txt
64b8573aad44cafde180246a79dba5e3d6fbd996072fe54286505d709a9bda58  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/arm64_jp7-folder-stream-file_first-prefix-plain.txt
3bb4e58ca3d51e13c5691b8de5c5c7475d0f53d7892e7bdf5ab9a1685102a9d2  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/x86_64_already_pinned_chain.txt
68df23dc2b63dd61980fac8dec08adfc56067f488c5f2cf0b0b793e49c8b2db3  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/x86_64_aravis_single_source.txt
7f1f25cec7030a94a0767f2dc1c6f03045469e3f003e1e428b3001b25933a7d7  test/backend-test/workflow_engine/goldens/jpegdec_native_caps/x86_64_filesrc_jpegdec_fakesink.txt
```

Task 3.5 then changed `test_pipeline_builder.py` by its one allowed hunk;
Task 5 shows that this hunk is the only difference.

**Pristine-worktree baseline (48841a0).** `git -C
/home/ubuntu/github/jp5/main/jpeg-folder-decode worktree add --detach
"$HOME/dda-tmp/jpeg-baseline-48841a0" 48841a0` printed `HEAD is now at
48841a0d`. Every task 5 command then ran there as written, 02:46Z to
02:56Z. The JP5 runs mounted that worktree read-only at `/w/repo`; a
session-finish print recorded where `gstreamer.pipeline_builder` came
from.

| Command (task 5 item) | Result at 48841a0 |
|---|---|
| workflow_core suite (1) | 1 failed, 1022 passed, 1 skipped |
| `workflow_engine/` (2) | 2 failed, 1898 passed, 10 skipped, 5 errors |
| `static_image_camera/` (2) | 17 failed, 23 passed, 6 errors |
| `stream_ingest/` (2) | 231 passed, 23 skipped, 2 errors |
| `output_bindings_fixes/` (2) | 25 passed, 7 errors |
| `api-endpoints/` (2) | 33 passed, 78 errors |
| `utils/` (2) | 64 passed, 1 skipped, 118 errors |
| `resources/` (2) | 69 errors |
| `csi_nvargus_optional/` (2) | 1 failed, 65 passed, 13 skipped |
| JP5 `gstreamer/` (3) | 36 failed, 77 passed |
| JP5 `test_pipeline_builder.py` alone (3) | 12 passed; builder loaded from `/w/repo/src/backend/gstreamer/pipeline_builder.py` |
| Portal named files, 16 of them (4) | 348 passed, 4 skipped |
| Steering guard suites (5) | 4 passed, 3 skipped |
| `build-custom.sh` gate body (5) | below |
| `test/python_version_audit.py` (6) | 0 counterexamples |

The host-side failures and errors come from the host environment: `gi`
(`cannot import name '_gi'`), the missing `awsiot`, `panorama`, `grpc` and
`mock_camera_backend` modules, `workflow_engine_test_utils` not on
`output_bindings_fixes/`' path, one `caplog` attribute error, and one
Hypothesis counterexample in `test_grab_determinism_and_config_invariance`.
The CSI failure is the stale `nodes.py` entry that task 3.8 rebaselines.
The JP5 `gstreamer/` ids are the 36 that review 6 measured.

**The gate body at 48841a0.** Task 5 item 5's procedure: the derived copy
(`set +e` plus the ERR trap, so every gate runs) in the JP5 image
`f2e221c3e2d1`, against a writable throwaway copy of the pristine
worktree, never the worktree itself (the backend conftest writes into the
tree). Its pip install had the network.
- Part 1: `scratch OK`, `derived copy OK` (141 and 142 lines), `tree copy
  OK: 4557 files`.
- Part 2's grep, in order: the first block `57 failed, 6 passed`, then
  `GATE-CMD-FAILED rc=1 line=9`; `Running stream camera components
  gate...`, `2 failed, 13 passed, 7 errors`, `GATE-CMD-FAILED rc=1
  line=24`; then the six audit gates with no `GATE-CMD-FAILED` line:
  injection/deserialization `10 passed, 1 skipped` and the
  `security/preservation/` suite `142 passed, 10 skipped`;
  secrets/credentials/JWT `5 passed, 3 skipped`; IAM `24 passed`; S3
  bucket-squatting `20 passed, 3 skipped`; Docker base image `19 passed`;
  dependency `17 passed`.
- Verdicts: the first block and the stream gate fail, for the
  image-content reasons of the pass rule; all six audit gates pass. Part
  3 removed the scratch.

**Every failing or erroring id at 48841a0**, per command, collection errors
included (generated from the `-rfE` logs and the gate grep):

- **workflow_core** (1 id)
  - FAILED `layers/workflow_core/tests/test_catalog_content.py` (1): `TestCatalogMirrorEquality::test_portal_and_vendor_catalog_init_are_byte_identical`
- **workflow_engine/** (7 ids)
  - FAILED `test/backend-test/workflow_engine/test_opcua_binding_preservation.py` (1): `TestAllSuccessLogging::test_all_success_run_logs_completed_message`
  - FAILED `test/backend-test/workflow_engine/test_property_python_bridge_process_frame.py` (1): `test_property_4_process_frame_contract_round_trip`
  - ERROR `test/backend-test/workflow_engine/test_mqtt_publish_retain.py` (collection)
  - ERROR `test/backend-test/workflow_engine/test_component_log_bounds.py` (4): `TestPipelineRunnerLines::test_a_run_logs_its_progress_at_info_and_nothing_at_warning`, `TestPipelineRunnerLines::test_a_continuous_run_writes_none_of_them_to_the_component_log`, `TestPipelineRunnerLines::test_a_triggered_run_still_writes_them_to_the_component_log`, `TestPipelineRunnerLines::test_inference_result_lines_are_info`
- **static_image_camera/** (23 ids)
  - FAILED `test/backend-test/static_image_camera/test_camera_manager_short_circuit.py` (12): `test_static_grab_touches_no_camera_machinery`, `test_static_grab_without_pin_raises_naming_camera`, `test_pin_replace_unpin_disturb_no_physical_camera`, `test_connect_camera_pinned_true_without_camera_construction`, `test_connect_camera_unpinned_raises_naming_camera`, `test_disconnect_camera_is_noop_true`, `test_get_camera_status_pinned_connected`, `test_get_camera_status_unpinned_disconnected`, `test_get_camera_feature_bounds_is_empty_never_connects[pinned_store]`, `test_get_camera_feature_bounds_is_empty_never_connects[empty_store]`, `test_apply_camera_features_is_empty_never_connects[pinned_store]`, `test_apply_camera_features_is_empty_never_connects[empty_store]`
  - FAILED `test/backend-test/static_image_camera/test_property_grab_determinism.py` (1): `test_grab_determinism_and_config_invariance`
  - FAILED `test/backend-test/static_image_camera/test_workflow_feed.py` (4): `TestExecutorStaticFrameFeed::test_run_feeds_pinned_image_and_completes`, `TestExecutorStaticFrameFeed::test_grab_config_is_accepted_and_frame_unchanged`, `TestExecutorStaticGrabFailure::test_unpinned_grab_fails_run_with_node_and_camera_name`, `TestExecutorStaticGrabFailure::test_pin_after_failure_lets_the_next_run_complete`
  - ERROR `test/backend-test/static_image_camera/test_image_source_wiring.py` (collection)
  - ERROR `test/backend-test/static_image_camera/test_property_enumeration.py` (collection)
  - ERROR `test/backend-test/static_image_camera/test_property_enumeration_resilience.py` (collection)
  - ERROR `test/backend-test/static_image_camera/test_property_identifier.py` (collection)
  - ERROR `test/backend-test/static_image_camera/test_property_static_camera_pixel_format.py` (collection)
  - ERROR `test/backend-test/static_image_camera/test_property_static_camera_pixel_format_preservation.py` (collection)
- **stream_ingest/** (2 ids)
  - ERROR `test/backend-test/stream_ingest/test_stream_broadcaster_and_routes.py` (collection)
  - ERROR `test/backend-test/stream_ingest/test_stream_image_source_api.py` (collection)
- **output_bindings_fixes/** (7 ids)
  - ERROR `test/backend-test/output_bindings_fixes/test_capture_artifact_repair_exploration.py` (collection)
  - ERROR `test/backend-test/output_bindings_fixes/test_greengrass_denial_diagnostics_exploration.py` (collection)
  - ERROR `test/backend-test/output_bindings_fixes/test_llm_containment_preservation.py` (collection)
  - ERROR `test/backend-test/output_bindings_fixes/test_llm_node_status_exploration.py` (collection)
  - ERROR `test/backend-test/output_bindings_fixes/test_llm_output_persistence_exploration.py` (collection)
  - ERROR `test/backend-test/output_bindings_fixes/test_triton_routing_preservation.py` (collection)
  - ERROR `test/backend-test/output_bindings_fixes/test_tritonless_routing_preservation.py` (collection)
- **api-endpoints/** (78 ids)
  - ERROR `test/backend-test/api-endpoints/test_feature_configuration_api.py` (collection)
  - ERROR `test/backend-test/api-endpoints/test_workflows_api.py` (collection)
  - ERROR `test/backend-test/api-endpoints/test_auth_info_api.py` (4): `TestAuthInfoApi::test_auth_disabled_returns_enabled_false_and_no_settings`, `TestAuthInfoApi::test_auth_enabled_returns_public_settings`, `TestAuthInfoApi::test_optional_logout_endpoint_defaults_to_null`, `TestAuthInfoApi::test_secret_is_never_exposed`
  - ERROR `test/backend-test/api-endpoints/test_camera_api.py` (2): `TestCamera::test_connect_camera_endpoint`, `TestCamera::test_disconnect_camera_endpoint`
  - ERROR `test/backend-test/api-endpoints/test_captured_images_api.py` (6): `TestCapturedImages::test_delete_captured_images`, `TestCapturedImages::test_delete_captured_images_invalid_path`, `TestCapturedImages::test_delete_captured_images_non_exists_file`, `TestCapturedImages::test_list_captured_images`, `TestCapturedImages::test_list_captured_images_invalid_path`, `TestCapturedImages::test_list_captured_images_non_exist_path`
  - ERROR `test/backend-test/api-endpoints/test_image_source_api.py` (11): `TestImageSource::test_add_image_source`, `TestImageSource::test_capture`, `TestImageSource::test_capture_folder`, `TestImageSource::test_capture_image_failed`, `TestImageSource::test_capture_invalid_type`, `TestImageSource::test_delete_image_source`, `TestImageSource::test_get_image_source`, `TestImageSource::test_list_image_source`, `TestImageSource::test_preview_image_without_data`, `TestImageSource::test_update_image_source`, `TestImageSource::test_update_image_source_with_crop`
  - ERROR `test/backend-test/api-endpoints/test_image_source_configuration_api.py` (1): `TestImageSourceConfigurations::test_list_image_source_configs`
  - ERROR `test/backend-test/api-endpoints/test_inference_result_api.py` (16): `TestInferenceResult::test_bulk_update_inference_results`, `TestInferenceResult::test_bulk_update_inference_results_invalid_human_classification`, `TestInferenceResult::test_bulk_update_inference_results_invalid_text_note`, `TestInferenceResult::test_bulk_update_inference_results_no_capture_id`, `TestInferenceResult::test_bulk_update_inference_results_with_human_classification_and_text_note`, `TestInferenceResult::test_delete_inference_result`, `TestInferenceResult::test_get_inference_result_by_capture_id`, `TestInferenceResult::test_get_inference_result_by_capture_id_capture_id_not_found`, `TestInferenceResult::test_get_inference_result_by_capture_id_workflow_not_found`, `TestInferenceResult::test_get_inference_result_summary`, `TestInferenceResult::test_list_inference_input_images_for_retrain_extends_max_download_limit`, `TestInferenceResult::test_list_inference_input_images_for_retrain_happy_path`, `TestInferenceResult::test_list_inference_input_images_for_retrain_no_file`, `TestInferenceResult::test_list_inference_results`, `TestInferenceResult::test_list_inference_results_invalid_input`, `TestInferenceResult::test_reset_inference_result_summary_start_time`
  - ERROR `test/backend-test/api-endpoints/test_input_configuration_api.py` (1): `TestInputConfigurations::test_list_input_configs`
  - ERROR `test/backend-test/api-endpoints/test_list_images_api.py` (6): `TestListImages::test_list_images_invalid_maxresults_exceeded`, `TestListImages::test_list_images_invalid_maxresults_startingpoint_letter`, `TestListImages::test_list_images_invalid_sort`, `TestListImages::test_list_images_invalid_startingpoint_negative`, `TestListImages::test_list_images_invalid_workflow_id`, `TestListImages::test_list_images_minimum_options_happy_path`
  - ERROR `test/backend-test/api-endpoints/test_local_server_version.py` (9): `TestGetLocalServerComponentVersion::test_amd64_path_resolves_amd64_variant`, `TestGetLocalServerComponentVersion::test_exact_match_is_order_independent`, `TestGetLocalServerComponentVersion::test_falls_back_to_first_prefix_match_when_name_undeterminable`, `TestGetLocalServerComponentVersion::test_falls_back_to_ipc_when_path_has_no_version`, `TestGetLocalServerComponentVersion::test_returns_not_found_on_ipc_error`, `TestGetLocalServerComponentVersion::test_returns_not_found_when_no_localserver_component`, `TestGetLocalServerComponentVersion::test_returns_running_variant_version_when_multiple_variants_present`, `TestGetLocalServerComponentVersion::test_version_from_path_does_not_call_ipc`, `TestGetLocalServerComponentVersion::test_version_read_from_artifact_path_ignores_leftover_component`
  - ERROR `test/backend-test/api-endpoints/test_output_configuration_api.py` (1): `TestOutputConfigurations::test_list_output_configs`
  - ERROR `test/backend-test/api-endpoints/test_station_logo_api.py` (2): `TestFeatureConfigurations::test_get_station_logo`, `TestFeatureConfigurations::test_get_station_logo_no_logo`
  - ERROR `test/backend-test/api-endpoints/test_streams_api.py` (16): `TestStreamsApi::test_frame_disconnected_returns_503`, `TestStreamsApi::test_frame_no_frame_returns_204`, `TestStreamsApi::test_frame_ok_returns_200_bytes_with_ok_header`, `TestStreamsApi::test_frame_requires_viewer_id_query_param`, `TestStreamsApi::test_frame_stale_returns_200_bytes_with_stale_header`, `TestStreamsApi::test_heartbeat_refreshed_returns_200`, `TestStreamsApi::test_heartbeat_unknown_viewer_returns_404`, `TestStreamsApi::test_settings_accepted_returns_200_dict`, `TestStreamsApi::test_settings_apply_error_returns_422_naming_control`, `TestStreamsApi::test_settings_no_active_session_returns_409`, `TestStreamsApi::test_subscribe_accepted_returns_200_with_viewer_id_and_count`, `TestStreamsApi::test_subscribe_camera_unavailable_returns_503`, `TestStreamsApi::test_subscribe_viewer_limit_boundary_8th_accepted_9th_rejected`, `TestStreamsApi::test_subscribe_viewer_limit_returns_429`, `TestStreamsApi::test_unsubscribe_returns_200_with_viewer_count`, `TestStreamsApi::test_viewers_returns_200_with_viewer_count`
  - ERROR `test/backend-test/api-endpoints/test_system_health_api.py` (1): `TestSystemHealth::test_system_health`
- **utils/** (118 ids)
  - ERROR `test/backend-test/utils/streaming/integration/test_inference_timing.py` (collection)
  - ERROR `test/backend-test/utils/streaming/integration/test_latency_bounds.py` (collection)
  - ERROR `test/backend-test/utils/test_create_model_repo.py` (collection)
  - ERROR `test/backend-test/utils/test_edgemlsdk_trace_listener.py` (collection)
  - ERROR `test/backend-test/utils/test_f2325_callback_thread.py` (collection)
  - ERROR `test/backend-test/utils/test_f2325_ipc_bug_conditions.py` (collection)
  - ERROR `test/backend-test/utils/test_f2325_ipc_preservation.py` (collection)
  - ERROR `test/backend-test/utils/test_f2325_ipc_recovery.py` (collection)
  - ERROR `test/backend-test/utils/test_f2325_resubscribe_backoff.py` (collection)
  - ERROR `test/backend-test/utils/test_f2325_subscription_queue.py` (collection)
  - ERROR `test/backend-test/utils/test_feature_configs_utils.py` (collection)
  - ERROR `test/backend-test/utils/test_inference_results_utils.py` (collection)
  - ERROR `test/backend-test/utils/test_ipc_client.py` (collection)
  - ERROR `test/backend-test/utils/test_list_gg_components.py` (collection)
  - ERROR `test/backend-test/utils/test_property_detection_result_persistence.py` (collection)
  - ERROR `test/backend-test/utils/test_property_detection_result_preservation.py` (collection)
  - ERROR `test/backend-test/utils/test_property_f2325_ipc_reconnect.py` (collection)
  - ERROR `test/backend-test/utils/test_restart_component.py` (collection)
  - ERROR `test/backend-test/utils/test_save_is_triton_value_to_file.py` (collection)
  - ERROR `test/backend-test/utils/test_stop_running_component.py` (collection)
  - ERROR `test/backend-test/utils/test_auth.py` (9): `TestValidateToken::test_empty_introspection_response_raises_401`, `TestValidateToken::test_inactive_token_raises_401_access_denied`, `TestValidateToken::test_no_token_with_auth_enabled_raises_401`, `TestValidateToken::test_non_200_introspection_raises_401_access_denied`, `TestValidateToken::test_valid_active_token_passes`, `TestValidateToken::test_validate_token_forwards_settings_to_validate_remotely`, `TestValidateRemotely::test_builds_basic_auth_header_and_posts_to_endpoint`, `TestValidateRemotely::test_raises_500_when_post_fails`, `TestValidateRemotely::test_returns_response_on_success`
  - ERROR `test/backend-test/utils/test_camera_manager.py` (11): `TestCameraManager::test_connect_camera`, `TestCameraManager::test_connect_camera_failed`, `TestCameraManager::test_disconnect_camera`, `TestCameraManager::test_disconnect_nonexistent_camera`, `TestCameraManager::test_get_all_camera_statuses`, `TestCameraManager::test_get_camera_status`, `TestCameraManager::test_get_nonexistent_camera_status`, `TestGetCameraFrameCompatibility::test_connects_once_when_not_cached`, `TestGetCameraFrameCompatibility::test_raises_when_no_frame`, `TestGetCameraFrameCompatibility::test_reuses_cached_camera_without_reconnecting`, `TestGetCameraFrameCompatibility::test_signature_is_unchanged`
  - ERROR `test/backend-test/utils/test_captured_images_utils.py` (7): `TestCapturedImagesUtils::test_convert_captured_data_to_save_in_db`, `TestCapturedImagesUtils::test_delete_image`, `TestCapturedImagesUtils::test_get_images`, `TestCapturedImagesUtils::test_get_images_empty_path`, `TestCapturedImagesUtils::test_get_oldest_image_file_path_corrupted`, `TestCapturedImagesUtils::test_get_oldest_image_file_path_no_jpg`, `TestCapturedImagesUtils::test_get_oldest_image_file_path_success`
  - ERROR `test/backend-test/utils/test_dda_user_management_utils.py` (17): `TestUpdateDdaUserFilePermissions::test_chmod_failure_raises`, `TestUpdateDdaUserFilePermissions::test_chown_failure_raises_and_skips_chmod`, `TestUpdateDdaUserFilePermissions::test_chown_then_chmod_with_defaults`, `TestUpdateDdaUserFilePermissions::test_custom_permissions_passed_through`, `TestSetupDdaUsersAndGroups::test_deletes_then_creates_both_users`, `TestSetupDdaUsersAndGroups::test_raises_when_create_fails`, `TestGetAllParentDirectories::test_returns_root_to_leaf_chain`, `TestCreateDdaUserDirectory::test_creates_dir_and_sets_perms_excluding_root_and_slash`, `TestCreateDdaUserDirectory::test_makedirs_oserror_propagates`, `TestCreateDdaUserDirectory::test_skips_makedirs_when_exists`, `TestConfinedDdaPermissionWalk::test_missing_path_keeps_the_type_error`, `TestConfinedDdaPermissionWalk::test_returned_path_is_the_given_string`, `TestConfinedDdaPermissionWalk::test_symlink_below_the_root_that_leads_out_is_refused`, `TestConfinedDdaPermissionWalk::test_unconfined_paths_are_refused_before_any_change`, `TestConfinedDdaPermissionWalk::test_update_permissions_refuses_an_unconfined_path`, `TestConfinedDdaPermissionWalk::test_valid_nested_path_gets_todays_walk`, `TestPropertyConfinedWalk::test_property_the_walk_stays_inside_its_area`
  - ERROR `test/backend-test/utils/test_digital_input_process_manager.py` (6): `TestDigitalInputProcess::test_check_health_reporting`, `TestDigitalInputProcess::test_init`, `TestDigitalInputProcess::test_monitor_digital_input_events_workflow_not_called_based_on_init_digital_level`, `TestDigitalInputProcess::test_monitor_digital_input_events_workflow_ready_mode`, `TestDigitalInputProcess::test_monitor_digital_input_events_workflow_trigger_called`, `TestDigitalInputProcess::test_monitor_digital_input_events_workflow_trigger_not_ready`
  - ERROR `test/backend-test/utils/test_digital_input_thread.py` (6): `TestDigitalInputThread::test_check_health_reporting`, `TestDigitalInputThread::test_init`, `TestDigitalInputThread::test_monitor_digital_input_events_workflow_not_called_based_on_init_digital_level`, `TestDigitalInputThread::test_monitor_digital_input_events_workflow_ready_mode`, `TestDigitalInputThread::test_monitor_digital_input_events_workflow_trigger_called`, `TestDigitalInputThread::test_monitor_digital_input_events_workflow_trigger_not_ready`
  - ERROR `test/backend-test/utils/test_user_group_management_utils.py` (27): `TestUserGroupExistenceChecks::test_get_userid_from_name_missing_returns_none`, `TestUserGroupExistenceChecks::test_get_username_from_id_missing_returns_none`, `TestUserGroupExistenceChecks::test_is_group_exists_false`, `TestUserGroupExistenceChecks::test_is_group_exists_true`, `TestUserGroupExistenceChecks::test_is_user_exists_false`, `TestUserGroupExistenceChecks::test_is_user_exists_true`, `TestUserGroupCommandConstruction::test_add_user_to_group_command`, `TestUserGroupCommandConstruction::test_create_group_with_gid`, `TestUserGroupCommandConstruction::test_create_user_minimal_command`, `TestUserGroupCommandConstruction::test_create_user_with_uid_and_group`, `TestUserGroupCommandConstruction::test_delete_group_command`, `TestUserGroupCommandConstruction::test_delete_user_command`, `TestUserGroupCommandConstruction::test_remove_user_from_group_command`, `TestIdempotentHelpers::test_create_group_if_not_exists_short_circuits`, `TestIdempotentHelpers::test_create_user_if_not_exists_creates_when_absent`, `TestIdempotentHelpers::test_create_user_if_not_exists_short_circuits`, `TestIdempotentHelpers::test_delete_group_if_exists_short_circuits_when_absent`, `TestIdempotentHelpers::test_delete_user_if_exists_short_circuits_when_absent`, `TestCreateDeleteUserAndGroup::test_create_user_and_group_aborts_if_group_fails`, `TestCreateDeleteUserAndGroup::test_create_user_and_group_success_path`, `TestCreateDeleteUserAndGroup::test_delete_user_and_group_aborts_if_user_delete_fails`, `TestNumericIdValidation::test_decimal_ids_keep_todays_argv`, `TestNumericIdValidation::test_malformed_gid_is_rejected_before_run_command`, `TestNumericIdValidation::test_malformed_uid_is_rejected_before_run_command`, `TestNumericIdValidation::test_unset_ids_add_no_option`, `TestGroupMembership::test_is_user_in_group_false`, `TestGroupMembership::test_is_user_in_group_true`
  - ERROR `test/backend-test/utils/test_utils.py` (15): `TestUtils::test_create_em_agent_config_aarch64`, `TestUtils::test_create_em_agent_config_other`, `TestUtils::test_gen_uuid`, `TestUtils::test_generate_capture_id`, `TestUtils::test_get_dio_script_path`, `TestUtils::test_get_gst_plugins_path_aarch64`, `TestUtils::test_get_gst_plugins_path_other`, `TestUtils::test_get_opcua_config_happy_path`, `TestUtils::test_get_opcua_config_non_opcua_type`, `TestUtils::test_get_station_logo_no_logo_folder`, `TestUtils::test_get_station_logo_returns_logo`, `TestUtils::test_remove_prefix`, `TestUtils::test_remove_prefix_invalid`, `TestUtils::test_split_file_name_and_path`, `TestUtils::test_split_file_name_and_path_invalid`
- **resources/** (69 ids)
  - ERROR `test/backend-test/resources/test_workflow_accessor.py` (collection)
  - ERROR `test/backend-test/resources/test_image_source_accessor.py` (21): `TestImageSourceAccessor::test_create_folder_source_inside_the_dda_area_is_accepted`, `TestImageSourceAccessor::test_create_folder_source_outside_the_dda_area_returns_400`, `TestImageSourceAccessor::test_create_image_source_camera_happy_path`, `TestImageSourceAccessor::test_create_image_source_folder_happy_path`, `TestImageSourceAccessor::test_create_image_source_invalid_data_camera_missing_params`, `TestImageSourceAccessor::test_create_image_source_invalid_data_folder_missing_location`, `TestImageSourceAccessor::test_create_image_source_missing_attr`, `TestImageSourceAccessor::test_delete_image_source`, `TestImageSourceAccessor::test_get_image_source_by_id`, `TestImageSourceAccessor::test_get_image_source_by_id_non_exist`, `TestImageSourceAccessor::test_list_image_source_camera_happy_path`, `TestImageSourceAccessor::test_list_image_source_camera_happy_path_without_type`, `TestImageSourceAccessor::test_update_all_image_sources_with_camera_status`, `TestImageSourceAccessor::test_update_folder_source_outside_the_dda_area_returns_400`, `TestImageSourceAccessor::test_update_image_source_happy_path`, `TestImageSourceAccessor::test_update_image_source_invalid_data`, `TestImageSourceAccessor::test_update_image_source_non_exist`, `TestImageSourceAccessor::test_update_image_source_with_camera_status`, `TestImageSourceAccessor::test_update_image_source_with_cfg`, `TestImageSourceAccessor::test_update_image_source_with_location`, `TestImageSourceAccessor::test_update_image_source_with_relative_path_location`
  - ERROR `test/backend-test/resources/test_image_source_cfg_accessor.py` (5): `TestImageSourceConfigurationAccessor::test_create_invalid_image_source_configuration`, `TestImageSourceConfigurationAccessor::test_create_invalid_image_source_configuration_with_crop`, `TestImageSourceConfigurationAccessor::test_create_valid_image_source_configuration`, `TestImageSourceConfigurationAccessor::test_create_valid_image_source_configuration_with_crop`, `TestImageSourceConfigurationAccessor::test_list_image_source_configurations`
  - ERROR `test/backend-test/resources/test_inference_result_accessor.py` (18): `TestInferenceResultAccessor::test_delete_inference_result`, `TestInferenceResultAccessor::test_get_inference_res_happy_path`, `TestInferenceResultAccessor::test_get_inference_res_happy_path_with_human_classification_and_text_note`, `TestInferenceResultAccessor::test_get_inference_result_summary`, `TestInferenceResultAccessor::test_input_images_path_from_capture_id_list_happy_path`, `TestInferenceResultAccessor::test_input_images_path_from_capture_id_list_non_exist`, `TestInferenceResultAccessor::test_list_inference_result_data_by_capture_id_prefix`, `TestInferenceResultAccessor::test_list_inference_results`, `TestInferenceResultAccessor::test_list_input_images_path_for_retrain_get_most_confusing_inference`, `TestInferenceResultAccessor::test_list_input_images_path_for_retrain_prediction_filter`, `TestInferenceResultAccessor::test_store_inference_res_happy_path`, `TestInferenceResultAccessor::test_store_inference_res_happy_path_with_human_classification_and_text_note`, `TestInferenceResultAccessor::test_store_inference_res_invalid_human_classification`, `TestInferenceResultAccessor::test_store_inference_res_invalid_prediction`, `TestInferenceResultAccessor::test_store_inference_res_invalid_text_note`, `TestInferenceResultAccessor::test_store_inference_res_missing_attr`, `TestInferenceResultAccessor::test_update_inference_results_happy_path`, `TestInferenceResultAccessor::test_update_inference_results_no_primary_key`
  - ERROR `test/backend-test/resources/test_input_configuration_accessor.py` (4): `TestInputConfigurationAccessor::test_create_input_cfg_happy_path`, `TestInputConfigurationAccessor::test_create_input_cfg_invalid_param`, `TestInputConfigurationAccessor::test_create_input_cfg_missing_attr`, `TestInputConfigurationAccessor::test_list_input_configurations`
  - ERROR `test/backend-test/resources/test_latency_time_accessor.py` (5): `TestLatencyTimeAccessor::test_get_latency_time`, `TestLatencyTimeAccessor::test_store_latency_time_happy_path`, `TestLatencyTimeAccessor::test_store_latency_time_invalid_param`, `TestLatencyTimeAccessor::test_store_latency_time_missing_attr`, `TestLatencyTimeAccessor::test_store_latency_times_happy_path`
  - ERROR `test/backend-test/resources/test_output_configuration_accessor.py` (4): `TestOutputConfigurationAccessor::test_create_output_cfg_happy_path`, `TestOutputConfigurationAccessor::test_create_output_cfg_invalid_param`, `TestOutputConfigurationAccessor::test_create_output_cfg_missing_attr`, `TestOutputConfigurationAccessor::test_list_output_configurations`
  - ERROR `test/backend-test/resources/test_stream_source_classic_rejection.py` (5): `TestStreamSourcesInClassicWorkflows::test_a_rejected_rebind_keeps_the_previous_source`, `TestStreamSourcesInClassicWorkflows::test_binding_a_classic_workflow_to_a_stream_source_is_rejected`, `TestStreamSourcesInClassicWorkflows::test_digital_input_capture_refuses_a_stream_source`, `TestStreamSourcesInClassicWorkflows::test_the_run_path_refuses_a_stream_source`, `TestStreamSourcesInClassicWorkflows::test_the_run_path_still_runs_a_folder_source`
  - ERROR `test/backend-test/resources/test_workflow_metadata_accessor.py` (6): `TestWorkflowMetadataAccessor::test_create_workflow_metadata`, `TestWorkflowMetadataAccessor::test_create_workflow_metadata_invalid_param`, `TestWorkflowMetadataAccessor::test_create_workflow_metadata_missing_attr`, `TestWorkflowMetadataAccessor::test_get_workflow_metadata`, `TestWorkflowMetadataAccessor::test_list_workflow_metadatas`, `TestWorkflowMetadataAccessor::test_update_workflow_metadata`
- **csi_nvargus_optional/** (1 id)
  - FAILED `test/backend-test/csi_nvargus_optional/test_preservation_csi_surface.py` (1): `test_backend_csi_consumer_file_untouched[src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py]`
- **gstreamer/ (JP5)** (36 ids)
  - FAILED `test/backend-test/gstreamer/test_gst_pipeline_executor.py` (16): `TestPipelineExecutor::test_execute_image_source_pipeline_bad_file`, `TestPipelineExecutor::test_execute_image_source_pipeline_camera_no_overrides`, `TestPipelineExecutor::test_execute_image_source_pipeline_camera_with_overrides`, `TestPipelineExecutor::test_execute_image_source_pipeline_folder_no_overrides`, `TestPipelineExecutor::test_execute_image_source_pipeline_folder_with_overrides`, `TestPipelineExecutor::test_execute_image_source_pipeline_icam_no_overrides`, `TestPipelineExecutor::test_execute_image_source_pipeline_icam_with_overrides`, `TestPipelineExecutor::test_execute_image_source_pipeline_invalid_type`, `TestPipelineExecutor::test_execute_workflow_pipeline_camera`, `TestPipelineExecutor::test_execute_workflow_pipeline_camera_runtime_failure`, `TestPipelineExecutor::test_execute_workflow_pipeline_folder`, `TestPipelineExecutor::test_execute_workflow_pipeline_folder_bad_file`, `TestPipelineExecutor::test_execute_workflow_pipeline_folder_runtime_failure`, `TestPipelineExecutor::test_execute_workflow_pipeline_folder_triton`, `TestPipelineExecutor::test_execute_workflow_pipeline_icam`, `TestPipelineExecutor::test_execute_workflow_pipeline_icam_runtime_failure`
  - FAILED `test/backend-test/gstreamer/test_property_appsrc_frame_stride.py` (5): `test_expected_stride_and_size_are_gstreamers_own_numbers`, `test_device_log_lines_are_gstreamers_arithmetic`, `test_wrapped_buffer_size_equals_the_size_its_caps_imply`, `test_bridged_fed_appsrc_wraps_to_the_caps_it_declares`, `test_deployed_workflow_frame_feed_shares_the_exposure`
  - FAILED `test/backend-test/gstreamer/test_property_appsrc_frame_stride_preservation.py` (15): `test_real_gstreamer_bindings_are_in_use`, `TestAlignedWidthsAreByteIdenticalAndCopyFree::test_wrapped_size_equals_tight_and_caps_implied_size`, `TestAlignedWidthsAreByteIdenticalAndCopyFree::test_generated_aligned_renders_stay_faithful`, `TestInherentlyAlignedFormats::test_four_byte_formats_are_aligned_for_every_width`, `TestInherentlyAlignedFormats::test_gray8_aligned_widths_are_byte_identical`, `TestInherentlyAlignedFormats::test_gray8_unaligned_widths_gain_exactly_the_reported_padding`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key0-recorded0]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key1-recorded1]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key2-recorded2]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key3-recorded3]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key4-recorded4]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key5-recorded5]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key6-recorded6]`, `TestInherentlyAlignedFormats::test_recorded_video_info_numbers_are_unchanged[key7-recorded7]`, `TestFrameDictIsNeverMutated::test_the_workflow_feed_pinned_image_is_an_unaligned_case`
- **gate body** (66 ids)
  - FAILED `test/backend-test/utils/test_auth.py` (9): `TestValidateToken::test_empty_introspection_response_raises_401`, `TestValidateToken::test_inactive_token_raises_401_access_denied`, `TestValidateToken::test_no_token_with_auth_enabled_raises_401`, `TestValidateToken::test_non_200_introspection_raises_401_access_denied`, `TestValidateToken::test_valid_active_token_passes`, `TestValidateToken::test_validate_token_forwards_settings_to_validate_remotely`, `TestValidateRemotely::test_builds_basic_auth_header_and_posts_to_endpoint`, `TestValidateRemotely::test_raises_500_when_post_fails`, `TestValidateRemotely::test_returns_response_on_success`
  - FAILED `test/backend-test/api-endpoints/test_auth_info_api.py` (4): `TestAuthInfoApi::test_auth_disabled_returns_enabled_false_and_no_settings`, `TestAuthInfoApi::test_auth_enabled_returns_public_settings`, `TestAuthInfoApi::test_optional_logout_endpoint_defaults_to_null`, `TestAuthInfoApi::test_secret_is_never_exposed`
  - FAILED `test/backend-test/utils/test_user_group_management_utils.py` (27): `TestUserGroupExistenceChecks::test_get_userid_from_name_missing_returns_none`, `TestUserGroupExistenceChecks::test_get_username_from_id_missing_returns_none`, `TestUserGroupExistenceChecks::test_is_group_exists_false`, `TestUserGroupExistenceChecks::test_is_group_exists_true`, `TestUserGroupExistenceChecks::test_is_user_exists_false`, `TestUserGroupExistenceChecks::test_is_user_exists_true`, `TestUserGroupCommandConstruction::test_add_user_to_group_command`, `TestUserGroupCommandConstruction::test_create_group_with_gid`, `TestUserGroupCommandConstruction::test_create_user_minimal_command`, `TestUserGroupCommandConstruction::test_create_user_with_uid_and_group`, `TestUserGroupCommandConstruction::test_delete_group_command`, `TestUserGroupCommandConstruction::test_delete_user_command`, `TestUserGroupCommandConstruction::test_remove_user_from_group_command`, `TestIdempotentHelpers::test_create_group_if_not_exists_short_circuits`, `TestIdempotentHelpers::test_create_user_if_not_exists_creates_when_absent`, `TestIdempotentHelpers::test_create_user_if_not_exists_short_circuits`, `TestIdempotentHelpers::test_delete_group_if_exists_short_circuits_when_absent`, `TestIdempotentHelpers::test_delete_user_if_exists_short_circuits_when_absent`, `TestCreateDeleteUserAndGroup::test_create_user_and_group_aborts_if_group_fails`, `TestCreateDeleteUserAndGroup::test_create_user_and_group_success_path`, `TestCreateDeleteUserAndGroup::test_delete_user_and_group_aborts_if_user_delete_fails`, `TestNumericIdValidation::test_decimal_ids_keep_todays_argv`, `TestNumericIdValidation::test_malformed_gid_is_rejected_before_run_command`, `TestNumericIdValidation::test_malformed_uid_is_rejected_before_run_command`, `TestNumericIdValidation::test_unset_ids_add_no_option`, `TestGroupMembership::test_is_user_in_group_false`, `TestGroupMembership::test_is_user_in_group_true`
  - FAILED `test/backend-test/utils/test_dda_user_management_utils.py` (17): `TestUpdateDdaUserFilePermissions::test_chmod_failure_raises`, `TestUpdateDdaUserFilePermissions::test_chown_failure_raises_and_skips_chmod`, `TestUpdateDdaUserFilePermissions::test_chown_then_chmod_with_defaults`, `TestUpdateDdaUserFilePermissions::test_custom_permissions_passed_through`, `TestSetupDdaUsersAndGroups::test_deletes_then_creates_both_users`, `TestSetupDdaUsersAndGroups::test_raises_when_create_fails`, `TestGetAllParentDirectories::test_returns_root_to_leaf_chain`, `TestCreateDdaUserDirectory::test_creates_dir_and_sets_perms_excluding_root_and_slash`, `TestCreateDdaUserDirectory::test_makedirs_oserror_propagates`, `TestCreateDdaUserDirectory::test_skips_makedirs_when_exists`, `TestConfinedDdaPermissionWalk::test_missing_path_keeps_the_type_error`, `TestConfinedDdaPermissionWalk::test_returned_path_is_the_given_string`, `TestConfinedDdaPermissionWalk::test_symlink_below_the_root_that_leads_out_is_refused`, `TestConfinedDdaPermissionWalk::test_unconfined_paths_are_refused_before_any_change`, `TestConfinedDdaPermissionWalk::test_update_permissions_refuses_an_unconfined_path`, `TestConfinedDdaPermissionWalk::test_valid_nested_path_gets_todays_walk`, `TestPropertyConfinedWalk::test_property_the_walk_stays_inside_its_area`
  - FAILED `test/backend-test/stream_ingest/test_image_stream_components.py` (2): `test_every_backend_package_is_in_the_image`, `test_the_image_imports_the_stream_ingest_service_from_its_own_root`
  - ERROR `test/backend-test/stream_ingest/test_image_stream_components.py` (7): `test_pyav_bundles_ffmpeg_6_1_or_later`, `test_pyav_has_the_flv_demuxer`, `test_ffmpeg_has_the_rtmp_protocol`, `test_software_decoding[h264]`, `test_software_decoding[h265]`, `test_rtmp_head_demuxes_and_decodes[h264]`, `test_rtmp_head_demuxes_and_decodes[h265]`

**Removal.** `git -C /home/ubuntu/github/jp5/main/jpeg-folder-decode
worktree remove --force "$HOME/dda-tmp/jpeg-baseline-48841a0"` exited 0,
and `git worktree list` no longer showed it. Task 4 re-created it at
03:12Z for 4.4's pre-fix compiles; Task 4 records its removal.

### Task 3: the fix

Source edits ran 02:48Z to 02:53Z, the recorded outputs until 02:56Z; no
source file changed afterwards. Lines added and removed: the two
`nodes.py` copies +43/−14 each, the two `compiler/compiler.py` copies
+41/−2 each, `pipeline_executor.py` +93, `pipeline_builder.py` +11.
- **3.1 to 3.4.** C1, C1b, C3, C3b and C4 as the design writes them.
  `gst_pipeline.py` and `python_bridge.py` are unchanged.
- **3.2.** `cmp` finds both pairs byte-identical (`nodes.py` sha256
  `2b0d9b8c…`, `compiler.py` `3097588b…`). The vendor README's `diff -r … -x
  '__pycache__'` lists only `catalog/__init__.py` (differs) and
  `catalog/platforms.py` (Portal only). `git status` shows exactly two
  modified files under `src/backend/workflow_engine/vendor/`.
  `test_vendored_catalog_mirror.py`: 9 passed.
- **3.5.** `test_catalog_content.py` and `test_compiler_compile.py`: 95
  passed, 1 failed (the pre-existing
  `TestCatalogMirrorEquality::test_portal_and_vendor_catalog_init_are_byte_identical`).
  `test_pipeline_builder.py` in the JP5 image with the tree at `/w/repo`:
  16 passed, the builder loaded from
  `/w/repo/src/backend/gstreamer/pipeline_builder.py`.
- **3.6.** The snippet exited 0, so its round-trip assertion held.
  `catalog_baseline.json`: 36 insertions, 0 deletions, 6 inserted
  capsfilter blocks. Only `folder_source` and `csi_camera_source` changed,
  each in its `x86_64`, `x86_64_nvidia` and `arm64_cpu` mappings, whose
  chains are now `filesrc, emexifextract, jpegparse, jpegdec, capsfilter,
  videoconvert, videoflip`. Their other fields and the type-id set are
  unchanged. The maintenance-log paragraph is appended above
  `_LLM_PRESERVED_PARAMETERS`.
- **3.7.** The command exited 0. `golden_zero_trigger_compilation.json`:
  21 insertions, 0 deletions, one capsfilter block in each of the
  `x86_64`, `x86_64_nvidia` and `arm64_cpu` `folder_capture` documents
  (`segments/0/elements` 8 → 9). The golden holds no `appsrc`. The four
  consumer files of 3.6 and 3.7: 49 passed.
- **3.8.** `pipeline_builder.py` `bf7221c7…` → `4b27ee43…`,
  `pipeline_executor.py` `7b80c297…` → `103bc023…`, vendored `nodes.py`
  `35bc1b2b…` → `2b0d9b8c…`; `csi_capture.py` unchanged. The `nodes.py`
  entry had been stale since 6bda4de: at 48841a0 that file hashes
  `e9cc8f43…`, hence task 2's pre-existing
  `test_backend_csi_consumer_file_untouched[…/catalog/nodes.py]` failure.
  This rebaseline records the post-fix value. `csi_nvargus_optional/`: 66
  passed, 13 skipped.
- **3.9** (03:01Z to 03:09Z, every Overview suite): no failing or erroring
  id that task 2 didn't record, and `csi_nvargus_optional/`'s stale-entry
  failure is gone. workflow_core 1 failed, 1289 passed, 1 skipped;
  `workflow_engine/` 2 failed, 2214 passed, 10 skipped, 5 errors; the other
  directories' counts equal task 2's.

### Task 4: offline matrix

**Harness.** Workspace `$HOME/dda-tmp/jpeg-folder-decode-matrix/`. The
images are the Image rule's IDs above. Every row container used the row
command's flags: `--rm --init --pull never --network none -e
PYTHONUNBUFFERED=1`, the fixed tree at `/w/repo:ro`, the pristine 48841a0
worktree at `/w/base:ro` for basecode rows, the workspace at `/work`, and
`-w /work`. A small driver ran each row by file path as its own Python
process (a child of Docker's init) and recorded its exit code.
- Each row first prints four provenance lines (`pipeline_executor`,
  `python_bridge`, `gst_pipeline`, `rendering`). Every row's lines carry
  its kind's prefix: `/w/repo/src/backend/` (fixed code),
  `/w/base/src/backend/` (48841a0's code), or the image's
  `/workflow_engine/` and `/gstreamer/` (4.5). No row exited 3 or 133 or
  lacked its lines, so there were no harness errors.
- The row script points the `filesrc` at a generated 64x48 4:4:4 JPEG and
  replaces each capture sink with `multifilesink
  location=<row>/sink<N>_%05d.bin`.
- It then applies the rewrites in `execute()` order:
  `_point_appsrc_at_frame_feed`, `python_bridge.rewrite_document` for
  bridged rows, `_stage_frame_sources`, C3, C3b, `_ensure_terminal_sink`
  and `_ensure_writable_inference_branch`. A rewrite the code lacks is
  logged as absent: 48841a0's code and the images' own code have no C3 or
  C3b.
- It renders with `rendering.render_launch_string` and runs the
  production consumers. Non-bridged rows: `Gst.parse_launch`, then
  `GstPipelineManager().create_buffer`, PLAYING, push, end-of-stream, and
  a 10 s wait for EOS or ERROR. Bridged rows:
  `python_bridge.run_bridged_pipeline` with a pass-through `process_frame`
  handler.

**4.1 Stock matrix** (03:15Z, `apt-get` with the network; the emulated
row ran by the amd64 image's ID with `--platform linux/amd64`):

| Image | `uname -m` | OS / GStreamer / plugins-good / libjpeg-turbo8 | U | X | U0 | X0 |
|---|---|---|---|---|---|---|
| `ubuntu:20.04` | aarch64 | 20.04.6 / 1.16.3 / 1.16.3-0ubuntu1.3 / 2.0.3-0ubuntu1.20.04.3 | 12/12 | 12/12 | 12/12 | 12/12 |
| `ubuntu:22.04` | aarch64 | 22.04.5 / 1.20.3 / 1.20.3-0ubuntu1.10 / 2.1.2-0ubuntu1 | 4/12 | 12/12 | 4/12 | 12/12 |
| `ubuntu:22.04` (`b8e6b596a324`) | x86_64 | 22.04.5 / 1.20.3 / 1.20.3-0ubuntu1.10 / 2.1.2-0ubuntu1 | 4/12 | 12/12 | 4/12 | 12/12 |
| `ubuntu:24.04` | aarch64 | 24.04.5 / 1.24.2 / 1.24.2-1ubuntu1.8 / 2.1.5-2ubuntu2 | 12/12 | 12/12 | 12/12 | 12/12 |

- On 1.20.3, U and U0 failed the same 8 kinds as in task 1 (`g444`,
  `ggray`, `godd420`, `crgb`, `c1x1`, `c1x2`, `c4x1`, `cgray`). U
  negotiated RGB and U0 xBGR, and the 4:4:4 debug run again logged
  `format_convert=1`.
- X and X0 passed all 12 kinds in every image. `jpegdec` emitted I420 for
  the 9 YCbCr kinds, GRAY8 for the 2 grayscale kinds and RGB for `crgb`.
- On 1.16.3 and 1.24.2, U and U0 passed all 12 with the same native
  formats (U0 recorded as observed; it is a counterexample row).

**4.2 LocalServer images, real `emexifextract`** (`gst-launch-1.0` only,
`--init --network none`). Kinds: 576x768 4:2:0, 320x240 4:2:0, 640x480
grayscale, and EXIF orientation 6 at 4:4:4 and at 4:2:0.

| Image | GStreamer | U | X |
|---|---|---|---|
| JP5 `f2e221c3e2d1` | 1.16.3 | 5/5 | 5/5 |
| arm64 CPU `6d5ce25b9e2d` | 1.20.3 | 3/5: grayscale and 4:4:4 orientation 6 fail (`jpegdec` negotiates RGB) | 5/5 |
| JP7 `b365ed521490` | 1.24.2 | 5/5 | 5/5 |
| amd64 `f34e42c9acad` (emulated) | 1.16.3 | 5/5 | 5/5 |

Every X run ended at the expected size, 480x640 for both orientation-6
kinds, with `jpegdec` emitting I420 or GRAY8.

**4.3 Rendered check 3 string.** `folder_source → custom_python → capture`,
compiled for `x86_64` by the fixed tree's vendored compiler, rendered
`filesrc … ! emexifextract ! jpegparse ! jpegdec idct-method=2 ! capsfilter
caps="video/x-raw,format=I420;…;video/x-raw,format=RGB" ! videoconvert !
videoflip method=automatic ! emlpython …`. Its source segment up to
`videoflip method=automatic`, plus `! video/x-raw,format=RGB ! fakesink`,
ran in the arm64 CPU image on a 576x768 4:4:4 JPEG: exit 0, `jpegdec` src
I420.

**4.4 Mixed-flow matrix.**
- Graphs: `folder_source f0 → capture c_file` plus `<feed> n1 → capture
  c_feed`, for `aravis_camera_source`, `custom_python_source` and
  `rtsp_camera_source`. The order is file first or feed first, by graph
  order. The bridged variant puts a pass-through `custom_python py1`
  (VideoFrames in and out) between `f0` and `c_file`.
- Three compilers ran on the host. Fixed: the fixed tree's vendored
  compiler (76 documents, with 4.6's graphs). Pre-fix: the pristine
  48841a0 worktree's vendored compiler (60). Counterexample: the fixed
  compiler with `compiler._feed_segment_first` patched to identity (60).
- For `arm64_jp5`, `arm64_jp6` and `arm64_jp7`, the fixed and pre-fix
  documents of every graph are identical (asserted while the rows were
  generated).
- The `x86_64` documents equal the `arm64_cpu` ones apart from
  `targetArch` for every graph, under each of the three compilers (12 of
  12), so the arm64 CPU rows stand for x86_64 too.
- Frames: Aravis 64x48 at 1 and at 3 bytes/pixel without `format`; a
  stream frame with `format` RGB; Produced_Frames with `format` RGB and
  BGR.

arm64 CPU image, 40 rows, fixed code:

| Frame | Order | Pre-fix, plain | Pre-fix, bridged | Fixed, plain | Fixed, bridged |
|---|---|---|---|---|---|
| Aravis, 1 byte/pixel | file first | PASS | PASS | PASS | PASS |
| Aravis, 1 byte/pixel | feed first | PASS | PASS | PASS | PASS |
| Aravis, 3 bytes/pixel | file first | PASS | PASS | PASS | PASS |
| Aravis, 3 bytes/pixel | feed first | PASS | PASS | PASS | PASS |
| Stream (RGB) | file first | PASS | PASS | PASS | PASS |
| Stream (RGB) | feed first | PASS | PASS | PASS | PASS |
| Produced_Frame RGB | file first | PASS | PASS | PASS | PASS |
| Produced_Frame RGB | feed first | PASS | PASS | PASS | PASS |
| Produced_Frame BGR | file first | PASS | PASS | PASS | PASS |
| Produced_Frame BGR | feed first | PASS | PASS | PASS | PASS |

- PASS means 4.4's pass rule: EOS, one file in each sink, exit 0, and the
  fed caps equal to `_frame_caps(frame)` plus `,width=64,height=48`.
- All 40 launch strings start with `appsrc name=appsrc caps=` and hold the
  design caps string exactly once.
- The pre-fix rows logged C3 inserting one capsfilter. The file-first
  pre-fix rows also logged C3b moving the feed segment (`c3b=True`), the
  feed-first ones not. The fixed rows logged `c3=0 c3b=False`: the package
  already had both.

JP5, JP6 and JP7 images, 40 rows each. Every graph and frame ran twice,
with 48841a0's code (`/w/base`) and the fixed code (`/w/repo`). Both rows
of a pair share one row directory, so their launch strings compare byte
for byte with nothing masked. Each cell holds for both codes in all three
images:

| Frame | Order | Plain | Bridged |
|---|---|---|---|
| Aravis, 1 byte/pixel | file first | PASS | **D12**: contained exception, exit 1 |
| Aravis, 1 byte/pixel | feed first | PASS | PASS |
| Aravis, 3 bytes/pixel | file first | PASS | **D12**: SIGABRT, exit 134 |
| Aravis, 3 bytes/pixel | feed first | PASS | PASS |
| Stream (RGB) | file first and feed first | PASS | PASS |
| Produced_Frame RGB | file first and feed first | PASS | PASS |
| Produced_Frame BGR | file first and feed first | PASS | PASS |

- Jetson byte identity: in each image, all 20 pairs render identical
  launch strings and give identical exits and outcomes.
- 36 of the 40 rows pass in each image. The other 4 per image, 12 in all,
  are design D12's pre-existing bridge-appsink shape (below).
- This Jetson run (03:20Z) supersedes a first one (03:14Z) that compared
  launch strings with the row path masked. Both runs gave the same exit
  and verdict for all 120 rows.

**The D12 rows (code review 1, finding 3).** In these rows the bridge
appsink on the file branch renders `caps=video/x-raw,format=RGB` ahead of
the fed appsrc. For an Aravis frame, which has no `format`,
`_fed_frame_caps` then returns the run-on clause `video/x-raw,format=RGB
appsrc name=py_out_py1 is-live=true format=time block=true,width=64,height=48`,
with either code. Base and fixed outcomes side by side:

| Image | Aravis frame | 48841a0's code | Fixed code |
|---|---|---|---|
| JP5 `f2e221c3e2d1` | 1 byte/pixel | exit 1: `PipelineExecutionException` (the frame buffer can't be reconciled with the caps), no buffer in either sink | the same |
| JP5 | 3 bytes/pixel | exit 134: `ERROR:gstappsrc.c:1264:gst_app_src_create: assertion failed: (GST_IS_BUFFER_LIST (obj))` | the same |
| JP6 `1fa46843533f` | 1 byte/pixel | exit 1: `PipelineExecutionException`, no buffer | the same |
| JP6 | 3 bytes/pixel | exit 134: `gstappsrc.c:1725:gst_app_src_create: code should not be reached` | the same |
| JP7 `b365ed521490` | 1 byte/pixel | exit 1: `PipelineExecutionException`, no buffer | the same |
| JP7 | 3 bytes/pixel | exit 134: `gstappsrc.c:1657:gst_app_src_create: code should not be reached` | the same |

- None of these 12 rows passes, and none is counted as a pass.
- They fail the same way before and after the fix, with byte-identical
  launch strings. That meets R1 (no flow that works today breaks) and R2's
  Jetson byte identity, under AC 13's and 4.4's D12 rule.
- They go to the owner's Q1 decision (task 12), where the owner is asked
  to acknowledge them. On `arm64_cpu` the same graphs pass in both shapes
  (table above), because C1b and C3b put the fed appsrc first there.

**Counterexample** (C1 without C1b, the rewrites without C3b; arm64 CPU
image, fixed code): the non-bridged Aravis graph, folder node first. Both
rows (1 and 3 bytes/pixel) exited 134 with `gstappsrc.c:1725:gst_app_src_create:
code should not be reached`, after their provenance lines.

**Extra shapes (code review 1, NIT 7).** Run in this iteration (04:36Z)
with the same harness, the code review's graph script (`x86_64` added) and
4.4's five frames, in both orders:
- feed-branch bridge: a pass-through `custom_python` on the feed branch
  (`n1 → py1 → c_feed`), with `f0 → c_file`. Here `rewrite_document`
  splits the fed segment.
- fan-in: `f0` and `n1` into one `capture`, through a funnel.
- arm64 CPU ran pre-fix and fixed documents with the fixed code. The
  Jetsons ran 48841a0's and the fixed code. `x86_64` again equals
  `arm64_cpu` apart from `targetArch` (12 of 12, both compilers).

| Shape | arm64 CPU (10 pre-fix + 10 fixed rows) | JP5, JP6 and JP7 (20 rows each) |
|---|---|---|
| Feed-branch bridge | 20 of 20 pass; every launch string starts with the fed appsrc and pins once | 20 of 20 pass in each image; all 10 pairs identical in each |
| Fan-in into one capture | 20 of 20 pass; the same two checks | 20 of 20 pass in each image; all 10 pairs identical in each |

- Fan-in rows are judged by EOS, exit 0, the fed caps, and both buffers in
  the one shared sink (`SINKS [2]`). The row script's own
  one-file-per-sink verdict doesn't fit a shared sink.
- On `arm64_cpu` the fan-in's feed segment moves ahead of `funnel name=f0`,
  so the launch string references `f0.` before the funnel is declared
  (`appsrc name=appsrc caps=… ! videoconvert ! f0. filesrc … ! f0. funnel
  name=f0 ! …`). `Gst.parse_launch` accepts it on 1.20.3.

**4.5 Older LocalServers, each image's own code** (`PYTHONPATH=/`;
provenance `/workflow_engine/…` and `/gstreamer/…`; no C3 or C3b there).
Each cell gives the fixed package (feed first, capsfilter present), then
the same package compiled without C1b (file first):

| Frame | arm64 CPU image, `arm64_cpu` package | arm64 CPU image, `x86_64` package | JP5 image, `arm64_cpu` package | JP5 image, `x86_64` package |
|---|---|---|---|---|
| Aravis, 1 byte/pixel | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 |
| Aravis, 3 bytes/pixel | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 |
| Stream (RGB) | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 |
| Produced_Frame RGB | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 |
| Produced_Frame BGR | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 | PASS / exit 134 |

So 10 of 10 fixed packages reach EOS with one buffer per sink and the fed
caps in each image. 10 of 10 packages without C1b abort: `gstappsrc.c:1725`
in the arm64 CPU image, the `gstappsrc.c:1264` assertion in JP5.

The local amd64 image (`flask-app-amd64:extracted`, `f34e42c9acad`) is out
of 4.5's scope (review 7, finding 2). Its `/` holds `app.py`, `gstreamer`,
`model` and `utils` but no `workflow_engine`. 4.2's probe of the whole
image found no `pipeline_executor.py`, `python_bridge.py` or
`rendering.py`, so it can't run a workflow package.

**Frame-feed consumers per image (design review 8, NIT).** The sha256
(first 16 hex digits) of each function's source text
(`ast.get_source_segment`), measured inside each image during every row
run:

| Function | 48841a0 (`/w/base`) and fixed tree (`/w/repo`) | arm64 CPU `6d5ce25b9e2d` | JP5 `f2e221c3e2d1` | JP7 `b365ed521490` | JP6 `1fa46843533f` |
|---|---|---|---|---|---|
| `gst_pipeline.py` `create_buffer` | `55f65c34138b17ad` | same | same | same | `13738072a22f454b` |
| `python_bridge.py` `_fed_frame_caps` | `b9b463c996e07dc4` | same | same | same | absent |
| `pipeline_executor.py` `_point_appsrc_at_frame_feed` | `69a398190fbaf47b` | same | same | same | `685b4d4112912852` |
| `pipeline_executor.py` `_frame_caps` | `d4813dce7df33e71` | same | same | same | `ab0008011e54e994` |
| `rendering.py` `render_launch_string` | `1a6056bef0bea54c` | same | same | same | same |

The arm64 CPU and JP5 images' own consumers equal 48841a0's, so 4.5 covers
LocalServers built from that code. The JP6 1.0.46 image has an older
generation without `_fed_frame_caps`. Design review 8 read it: it still
takes the launch string's first `caps=` and gives the renamed appsrc its
own caps, which is all C1b relies on. 1.0.50's own code isn't local and
isn't checked. The fix doesn't change any of the five functions.

**4.6 D12 outcome table** (JP5, JP6 and JP7, fixed tree on the path; the
Jetson code is unchanged by the fix). The three images gave the same
outcome in every row:

| Graph (file source first) | Fed frame | First `caps=` | Outcome |
|---|---|---|---|
| `model_inference` → `mqtt_publish` (`emltriton` swapped for `identity`) | Aravis, 1 byte/pixel (GRAY8) | `video/x-raw,format=RGB` | exit 1: contained `PipelineExecutionException`, no buffer |
| `bedrock_inference`, whose feeder's I420 capture chain ends in `jpegenc ! multifilesink` | Aravis, 1 byte/pixel (GRAY8) | `video/x-raw,format=I420` | EOS, exit 0. The fed appsrc took I420 caps for a 3072-byte GRAY8 buffer (I420 needs 4608), and its sink decodes to a wrong 64x48 frame (mean RGB 0, 135, 0) |
| `format_convert` BGR | Produced_Frame RGB, all red | `video/x-raw,format=BGR` | EOS; the frame arrives blue (mean RGB 0, 0, 255): channels swapped |
| `format_convert` RGB | Produced_Frame BGR, blue | `video/x-raw,format=RGB` | EOS; the frame arrives red (mean RGB 255, 0, 0): channels swapped |
| Bridge appsink (4.4's D12 rows) | Aravis, 1 and 3 bytes/pixel | the run-on clause | contained exception, and SIGABRT (above) |

These rows have no pass condition. They match design D12's table and feed
task 12.

**Cleanup.** The workspace's records (row specs, logs, documents, the 4.1
and 4.2 outputs) were archived into the lab evidence bundle. The
workspace was then emptied with the Workspace rule's container (`find /g
-mindepth 1 -delete`, JP5 image) and removed: `workspace removed`. The
pristine worktree held no entry the host user didn't own. `git worktree
remove --force "$HOME/dda-tmp/jpeg-baseline-48841a0"` exited 0, and `git
worktree list` no longer shows it.

### Task 5: repo suites and checkpoint (fixed tree)

Run 04:34Z to 04:41Z on the working tree: 48841a0 plus the fix, before
the commits, with nothing changed since. The commands are the Overview's
and task 5's.
- Every container ran with `--network none`.
- The two in-container pip installs resolved from a local wheelhouse
  mounted read-only, through pip's `PIP_NO_INDEX=1` and
  `PIP_FIND_LINKS=/wheels` environment variables. That covers the
  Overview command's install and the gate body's own (derived line 3), and
  leaves the command text unchanged.
- The wheelhouse holds the versions task 2's networked run installed:
  pytest 9.1.1, pytest-cov 7.1.0, Hypothesis 6.169.1, sarge 0.1.8 and
  testfixtures 12.4.0. The gate log's headers show the same `pytest-9.1.1`,
  `cov-7.1.0` and `hypothesis-6.169.1` as task 2's.

| Item | Suite | Task 2, 48841a0 | Task 5, fixed tree | New failing ids |
|---|---|---|---|---|
| 1 | workflow_core | 1 failed, 1022 passed, 1 skipped | 2 failed, 1288 passed, 1 skipped | 1, pre-existing (below) |
| 2 | `workflow_engine/` | 2 failed, 1898 passed, 10 skipped, 5 errors | 3 failed, 2213 passed, 10 skipped, 5 errors. Re-run on a quiet host: 2 failed, 2214 passed, 10 skipped, 5 errors | 1 in the first run, a pre-existing timing failure (below); 0 in the re-run |
| 2 | `static_image_camera/` | 17 failed, 23 passed, 6 errors | the same | 0 |
| 2 | `stream_ingest/` | 231 passed, 23 skipped, 2 errors | the same | 0 |
| 2 | `output_bindings_fixes/` | 25 passed, 7 errors | the same | 0 |
| 2 | `api-endpoints/` | 33 passed, 78 errors | the same | 0 |
| 2 | `utils/` | 64 passed, 1 skipped, 118 errors | the same | 0 |
| 2 | `resources/` | 69 errors | the same | 0 |
| 2 | `csi_nvargus_optional/` | 1 failed, 65 passed, 13 skipped | 66 passed, 13 skipped | 0; the stale entry is fixed (3.8) |
| 3 | JP5 `gstreamer/` | 36 failed, 77 passed | 36 failed, 81 passed | 0; the same 36 ids |
| 3 | JP5 `test_pipeline_builder.py` alone | 12 passed | 16 passed | 0 |
| 4 | Portal named files | 348 passed, 4 skipped (16 files) | 352 passed, 4 skipped (17 files) | 0 |
| 5 | steering guard suites | 4 passed, 3 skipped | the same | 0 |
| 5 | `build-custom.sh` gate body | below | below | 0 |
| 6 | `test/python_version_audit.py` | 0 counterexamples | 0 counterexamples | none |

- Item 3's single-file run loaded `gstreamer.pipeline_builder` from
  `/w/repo/src/backend/gstreamer/pipeline_builder.py`, and 1(h)'s
  `inspect.getsourcefile` assertions passed.
- The tests of tasks 1 and 2 pass unchanged: the Portal file 267 passed;
  the backend files 324 passed (11, 304 and 9); the Portal function file 4
  passed; the legacy file 16 passed.

**The two ids task 2's run didn't show.** Both fail on 48841a0 too, so
neither is caused by the fix. The tests are left unchanged.
- `layers/workflow_core/tests/test_property_stream_url_redaction.py::test_planted_secrets_never_survive_redaction`
  (code review 1, finding 5).
  - Hypothesis found a text in which a planted Secret_Query_Parameter
    value, written with spaces around the `=` after an RTSP URL, survives
    `redact()`.
  - `stream_url.py` is byte-identical in 48841a0's two copies and the
    fixed tree's two (sha256 `63d05566e3422de3…`; `git diff 48841a0` is
    empty for both paths), and all four give the same output on the
    recorded example, with the planted value still in it.
  - The test, run alone with `--hypothesis-seed` 0 to 39, each run in a
    fresh working directory so no stored example replays: seed 25 fails
    on both trees, with a falsifying example of the same shape, and the
    other 39 seeds pass on both.
  - The draw is random, so task 2's and task 3.9's runs passed it, while
    the code review's run and this one found it.
  - It is a gap in pre-existing redaction code, outside this fix, and is
    raised with the owner as a separate issue. The full example is kept
    in the lab evidence bundle, not here.
- `test/backend-test/workflow_engine/test_opcua_binding_preservation.py::test_all_success_output_bindings_finalize_completed`.
  - Hypothesis raised `FlakyFailure`: the first example took 272 ms
    against the 200 ms deadline (`DeadlineExceeded`), and its retry took
    195 ms. The host was busy with task 5's container suites and the
    extra 4.4 rows at the time.
  - Run alone, the file fails this test the same way on both trees, 3
    runs of 3 each: the first example takes about 410 ms, the retry about
    118 ms. Each of those runs also shows the known
    `TestAllSuccessLogging` failure: 2 failed, 5 passed.
  - In the full directory the earlier tests warm the imports, so it
    usually passes: in task 2's run, in 3.9, in the code review's run, and
    in this re-run on a quiet host, whose failing and erroring ids equal
    task 2's exactly.

**The gate body (item 5).** The derived copy ran in the JP5 image
`f2e221c3e2d1` against a writable copy of the fixed tree, never the
worktree itself, for task 5 item 5's reasons: the backend conftest copies
files into the tree, and `LocalServerBaseTestCase.setUp` rewrites a
tracked file.
- Part 1: `scratch OK`; `derived copy OK`, 141 and 142 lines, with the
  same head and tail lines as task 2's; `tree copy OK: 4630 files`. That is
  the 4557 tracked files plus the 73 untracked ones: 68 new test and
  golden files and the 5 spec files.
- Part 2's grep, against task 2's:
  - the first block: `57 failed, 6 passed`, then `GATE-CMD-FAILED rc=1
    line=9`, with the same 57 ids;
  - the stream camera components gate: `2 failed, 13 passed, 7 errors`,
    then `GATE-CMD-FAILED rc=1 line=24`, with the same 9 ids;
  - the six audit gates, with no `GATE-CMD-FAILED` line: `10 passed, 1
    skipped` and `142 passed, 10 skipped`; `5 passed, 3 skipped`; `24
    passed`; `20 passed, 3 skipped`; `19 passed`; `17 passed`. All are
    identical to task 2's.
- Verdict under the pass rule: pass. Only the two expected image-content
  blocks fail, with task 2's ids, and all six audit gates pass. Part 3
  printed `gate scratch removed`.

`git diff --stat -- test/backend-test/security/baselines/` is empty.

**sha256 before and after the runs.** 69 of task 2's 70 values match, both
times. The 70th file, `test_pipeline_builder.py`, is now `4e056a5e…`, and
task 3.5's hunk is its only change. Reverting exactly that hunk, by
restoring 48841a0's lines for the `device_arch` patch line, the signature
and the expected string, gives `962bb61e…`, task 2's value.

**Scope** (`git status --porcelain`, before the commits):
- modified: the two `catalog/nodes.py` copies, the two
  `compiler/compiler.py` copies, `src/backend/workflow_engine/pipeline_executor.py`,
  `src/backend/gstreamer/pipeline_builder.py`, `catalog_baseline.json`,
  `golden_zero_trigger_compilation.json`, `test_bug_catalog_preservation.py`
  (the log paragraph), `test_catalog_content.py`,
  `test_compiler_compile.py`, `backend_csi_consumers_sha256.json`,
  `test/backend-test/gstreamer/test_pipeline_builder.py` and
  `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py` (one
  tuple entry and one comment bullet);
- new: `edge-cv-portal/backend/layers/workflow_core/tests/test_jpegdec_native_caps.py`
  and its `goldens/` (2 files), `edge-cv-portal/backend/tests/test_feed_segment_first_packaging.py`,
  `test/backend-test/workflow_engine/test_jpegdec_native_caps.py` and
  `test_jpegdec_mixed_feed_matrix.py` with their `goldens/` (6 files plus
  54 in `mixed/`), `test/backend-test/gstreamer/goldens/` (2 files), and
  this spec directory.
- Nothing else: `gst_pipeline.py`, `python_bridge.py`, the vendor
  README, `re_vendor.sh` and the security baselines are untouched. The
  gitignored `edge-cv-portal/backend/.hypothesis/` is the only ignored
  file the runs left in the tree.

**Hygiene scan.** It covered everything the two commits hold: the 444
added lines of the tracked diff, the 73 untracked files (the new tests,
goldens and this spec directory) and both commit messages.
- Pattern classes: 12-digit account ids (outside hex strings); container
  registry hosts; ARNs, `s3://` URIs and the word "bucket"; hostnames
  (EC2-style names, internal DNS suffixes, and the lab devices' host-name
  forms); every IPv4 address; credentials (the lab devices' login
  secrets, AWS access key ids, secret key names, private-key headers and
  API key assignments); personal names and home directory paths;
  Greengrass thing-name forms; scanner and internal tool names; inline
  suppression markers.
- The literal pattern list stays in the lab evidence bundle, because
  several patterns are the very strings that must not appear here.
- Result: no account id, registry host, bucket name, hostname, LAN
  address, credential, thing name, scanner or tool name, or suppression
  marker. Every hit is benign: the redaction-policy sentences and the
  bucket-squatting gate's name; "Jetson-only" in prose; `127.0.0.1`,
  `0.0.0.0` and package version strings; the repo's absolute path in the
  task commands, as in the approved tasks.md; and task 10's
  `--core-device-thing-name <thing>` placeholder.
- `security-scan-remediation-high/requirements.md` is unchanged.

### Task 6: commits on the feature branch

- **Gate.** The diff scope and the hygiene result are in Task 5.
- **Build source.** This run's instructions, relayed from the owner's
  session, approve commits on `spec/jpeg-folder-source-decode`. The owner
  builds from the pushed feature branch, and the trunk merge waits for
  on-device verification (`.kiro/steering/builds.md`). The owner's own
  words on the build source aren't quoted in this run. If the owner
  directs `remediation` instead, record those words here.
- **Commits.** Named files only, no hook skipped:
  - `b6d1e1a0` `fix(workflow): pin jpegdec to native formats and keep
    frame feeds first on Ubuntu 22.04 targets`. Its 82 files are the code,
    tests, goldens and recorded outputs of tasks 1 to 3, and nothing else
    (Task 5's scope list). C1, C1b, C3 and C3b ship together in it
    (design D10). Its body says what was verified offline and that
    on-device verification is pending.
  - The `docs(spec)` commit right after it holds this spec directory's
    five files.
- **Remote** (`git ls-remote`, read-only): `integration/all-specs` and
  `remediation` are both still at 48841a0, and `spec/jpeg-folder-source-decode`
  doesn't exist on `origin` yet. So no merge is needed before the push.
- **Push: pending.** It is the release step after this run. The local
  branch tracks `origin/integration/all-specs`, so the push must use the
  explicit `git push -u origin spec/jpeg-folder-source-decode`, never a
  bare `git push` (code review 1, NIT 9). Nothing was merged into or
  pushed to `remediation`, `integration/all-specs` or `main`, and no build
  was dispatched.

### Raised with the owner by this run

- Task 12: acknowledge that task 4.4's 12 Jetson D12 rows are recorded as
  pre-existing, not as passes (Task 4).
- A separate issue, outside this fix: `redact()` lets a planted
  Secret_Query_Parameter value through when it is written with spaces
  around `=` (Task 5).
- For information: `test_all_success_output_bindings_finalize_completed`
  misses its 200 ms Hypothesis deadline on a cold or busy host, on 48841a0
  as on the fix (Task 5).

## Owner-gated (tasks 7 to 12): pending, not executed in this run

- The rebuilds (amd64, amd64Nvidia; arm64 CPU when shipped), the arm64JP7
  verification build for thor1, and the first versions that contain C3b.
- Task 7's offline check of each built image. Record it as `flask-app:<component
  version>` plus the loaded image ID. Also record the IDs of the tags the
  artifact's tar applied, and confirm they were restored: `flask-app:latest`
  must be back on the JP5 image before anything else runs. The artifact
  comes from the published component version (its recipe's artifact URI,
  copied with `aws s3 cp`); record only the component name and version,
  never the URI, the ARN or the account id.
- **Pending owner decisions: the lab Dell's GPU upgrade.** The owner is
  installing two NVIDIA V100 GPUs in the lab Dell (powered off for it on
  2026-10-09).
  - Recommended order: run task 8 with the Dell still deployed as an
    x86_64 CPU station (the amd64 LocalServer; check 3a redeploys the
    run-2 `x86_64` package unchanged). Then, optionally, switch it to the
    amd64Nvidia LocalServer and turn task 9 into an on-device check.
  - Task 8 must not re-run station quick setup, which would auto-detect
    `x86_64_nvidia`.
  - NVIDIA's R580 driver branch is the last with Volta support.
  - The amd64Nvidia image is CUDA 12.6 based. Whether its GPU libraries
    still support sm_70 is UNVERIFIED and is task 9's open item.
- Dell checks 3a and 3b under the numeric rule of bugfix 2.7; the check 3c
  matrix (3c-python, 3c-stream-t, 3c-stream-c, 3c-aravis,
  3c-aravis-bridged; P and F packages) with the proof of both frames; the
  temporary legacy workflow; the 30-minute soak with every 3c workflow;
  the digest of the `bluenviron/mediamtx:1.9.3-ffmpeg` image it ran by,
  the `ss` verdict with its recorded protocol and local-address pairs
  (field 5 only; exactly the three 127.0.0.1 listeners, each once; no
  other `mediamtx` or `ffmpeg` socket), and the container's removal.
- thor1 (task 8b): the check 3 comparison with 1.0.56 under bugfix 2.11's
  normalization, the mixed flow, the legacy workflow if safe, the soak and
  the restore.
- The amd64Nvidia record: built only, or the on-device check on the
  upgraded Dell if the owner decides so (task 9).
- The station inventory (counts and versions only), then the Portal
  ComputeStack deploy after its precondition is confirmed.
- The trunk merge.
- The owner's Q1 decision (task 12).
- amd64 stays not cleared for station rollout until task 8 passes.
