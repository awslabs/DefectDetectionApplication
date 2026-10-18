# JPEG Folder Source Decode on Ubuntu 22.04 — Bugfix Design

## Overview

On GStreamer 1.20.x, `jpegdec` follows the formats its downstream peer
offers. When the first one it supports is RGB-family, it decodes with
libjpeg-turbo's direct colour-conversion output. That happens behind
`videoconvert` for an RGB consumer (the custom Python bridge appsink,
model_inference's `capsfilter format=RGB`), and also for an unconstrained
downstream: `videoconvert ! fakesink` negotiates xBGR (the Dell's
diag-narrowing cases 3 and 10; review 3's probe). It doesn't happen when
the downstream prefers I420, as a `jpegenc` capture does. On 1.20.3 that
path fails for most JPEGs (`Improper call to JPEG library in state 205`),
so the Ubuntu 22.04 amd64 LocalServer can't run a JPEG Folder source into
an RGB consumer.

**The fix** pins `jpegdec` to the formats it produces natively by putting
one `capsfilter` right after it:

```
video/x-raw,format=I420;video/x-raw,format=Y42B;video/x-raw,format=Y444;video/x-raw,format=GRAY8;video/x-raw,format=RGB
```

I420 comes first, so `jpegdec` never sees an RGB-family first format and
never takes the direct path. `videoconvert`, which already follows, does the
RGB conversion. This is the 1.16.3 decode path that 1.0.50 used. It refines
the owner's "decode to I420 before RGB conversion": YCbCr JPEGs of every
subsampling decode to I420, grayscale JPEGs keep GRAY8, and RGB-colourspace
JPEGs keep their native RGB. Forcing I420 alone breaks the last two on every
GStreamer version (see "Offline matrix").

The capsfilter goes into the three places that build this chain, only for
`x86_64`, `x86_64_nvidia` and `arm64_cpu` (the jpegdec targets that ship
GStreamer 1.20.x):

1. The shared catalog `_jpeg_file_chain`: the Portal source of truth plus its
   byte-identical vendored copy (C1, C2).
2. A run-time normalization in the LocalServer workflow executor, which
   inserts it into documents packaged before the fix, so a LocalServer
   rebuild alone repairs every deployed workflow (C3).
3. The legacy Pipeline_Configuration builder's file chain (C4).

**Mixed flows must not break (owner requirement, a hard acceptance gate).**
The owner's words: "It is important those flows do not break." A workflow
that mixes a JPEG folder source with an Aravis, stream (RTSP) or
`custom_python_source` feed must not break on any target. Items R1 to R5
and Q1 (bugfix.md) make that gate concrete.

Both Frame_Feed consumers read the launch string's first `caps=` clause as
the fed appsrc's caps, and the capsfilter gives the file chain a `caps=`
clause. A document that renders a folder source ahead of a camera, stream
or Python-source feed would abort the backend process (bugfix 1.5). So, on
the same three arches, the fed segment is put first in two layers (D10):
- the shared compiler emits the frame-feed segment first in every new
  package (C1b), which also protects LocalServers that predate this fix;
- the executor moves it first in packages compiled before the fix (C3b).

Both also fix a pre-existing abort on those arches (bugfix 1.6). D11 maps
the owner's gate (R1 to R5) onto a unit, offline and on-device matrix. D12
answers Q1 for JP5, JP6 and JP7 with measurements and a recommendation.

JP5 (1.16.3), JP6 (PNG-staged) and JP7 (1.24.2, check 3 PASS on thor1)
stay byte-identical, mixed documents included.

**Technology stack (locked):** Python 3 in the existing modules only; the
GStreamer core `capsfilter` element (plugin `coreelements`, already a
declared dependency); pytest with Hypothesis (the repo's existing test
stack); throwaway Docker containers for the offline matrix, using the local
LocalServer images and the local `dda-portal-test:py311` image for the
Portal function tests. No new dependency, no OS or GStreamer change, no
Dockerfile, recipe or build script change.

## Glossary

- **Direct-output path**: GStreamer 1.20.x `jpegdec` with libjpeg-turbo sets
  `out_color_space` to a `JCS_EXT_*` format and lets libjpeg-turbo convert
  (debug line `gst_jpeg_turbo_parse_ext_fmt_convert … format_convert=1`).
  It is taken when the first format the peer offers that `jpegdec`
  supports is RGB-family. It was not observed on 1.24.2, where `jpegdec`
  emits only native formats (Root Cause).
- **Native output**: what `jpegdec` emits without that path. I420 for every
  YCbCr JPEG (it resamples 4:2:2, 4:4:4, 4:4:0 and 4:1:1 chroma
  internally), GRAY8 for grayscale, RGB for RGB-colourspace (Adobe
  transform 0) JPEGs. The src template is
  `{ I420, RGB, BGR, RGBx, xRGB, BGRx, xBGR, GRAY8 }` on 1.16.3, 1.20.3 and
  1.24.2.
- **`JPEGDEC_NATIVE_CAPS`**: the caps string above, a new constant in
  `workflow_core/catalog/nodes.py`.
- **`JPEGDEC_NATIVE_CAPS_ARCHS`**: the new tuple
  `(ARCH_X86_64, ARCH_X86_64_NVIDIA, ARCH_ARM64_CPU)`. A **constrained
  arch** is an arch id in it.
- **Catalog JPEG sequence**: three consecutive elements `jpegparse`,
  `jpegdec`, `videoconvert` in one segment. Every document compiled from
  `_jpeg_file_chain` since 8daa021 (2026-07-13) has it.
- **Pre-fix package** (P): a workflow component whose
  `compiled_pipeline.json` was compiled by a Portal without this fix: no
  capsfilter, source segments in graph order. **Fixed-shape package** (F):
  compiled by the fixed tree: capsfilter present and, on constrained arches,
  the feed segment first.
- **Frame-feed source**: `aravis_camera_source`, `custom_python_source`,
  `rtsp_camera_source` or `rtmp_stream_source` (the validator's
  `FRAME_FEED_SOURCE_TYPES`). Each compiles to `appsrc
  name=appsrc_<nodeId> ! videoconvert`.
- **Fed appsrc**: the one appsrc a Frame_Feed run pushes its frame into
  (Aravis grab, stream frame or Produced_Frame). `_point_appsrc_at_frame_feed`
  renames it `appsrc` and gives it its own `caps=` argument.
- **Mixed document**: a document holding a JPEG file source and a
  frame-feed source. It is "file-first" when the file source's segment
  renders ahead of the fed appsrc's segment.
- **Feed-first rules**: the compile-side reorder (C1b, option c) and the
  executor's reorder (C3b, option b).
- **Older LocalServer**: any LocalServer build without C3, C3b and C4, for
  example amd64 1.0.50, the only cleared amd64 build.

## Root Cause (confirmed)

**Mechanism.** With `GST_DEBUG=jpegdec:5` on stock Ubuntu 22.04 (GStreamer
1.20.3, libjpeg-turbo 2.1.2), the unfixed chain logs these lines in order:
- `gst_jpeg_turbo_parse_ext_fmt_convert: Received caps from peer: …` then
  `format_convert=1`;
- `Decode error #21: Improper call to JPEG library in state 205`;
- `No valid frames decoded before end of stream`.

The second line is libjpeg's `JERR_BAD_STATE`. Our reading, which is
consistent with libjpeg's state constants but wasn't traced in the plugin
source: a raw-data read is issued while the decompressor is in the scanline
state (`DSTATE_SCANNING` = 205), and raw reads need `DSTATE_RAW_OK` = 206.
Either way the error happens inside `jpegdec` in a stock image, so no DDA
element, model or library is involved.

**Behavior by GStreamer version.** Full data is in verification-notes.md.
The chain under test is
`jpegdec ! [X] ! videoconvert ! videoflip method=automatic ! video/x-raw,format=RGB`,
over 12 JPEG kinds:
- 4:2:0, 4:2:2, 4:4:4 and grayscale from `jpegenc`;
- odd-size 577x769 4:2:0;
- from `cjpeg`: progressive, 2x1, 1x1, 1x2 and 4x1 sampling, grayscale, and
  `-rgb`.

| X | 1.16.3 (20.04, JP5) | 1.20.3 (22.04, amd64/arm64) | 1.24.2 (24.04, JP7) |
|---|---|---|---|
| none (today) | 12/12 pass, native formats | RGB negotiated for all 12; **8/12 fail** (4:4:4, 1x2, 4x1, odd 4:2:0, both grayscale, RGB-colourspace) | 12/12 pass, `jpegdec` picks I420 |
| `format=I420` | 9/12, grayscale ×2 and RGB-colourspace fail | 9/12, same | 9/12, same |
| I420;Y42B;Y444;GRAY8 | 11/12, RGB-colourspace fails | 11/12, same | 11/12, same |
| **chosen 5-format list** | **12/12**, same formats as today | **12/12** (I420 ×9, GRAY8 ×2, RGB ×1) | **12/12**, same formats as today |

Other results:
- 1.20.3 gives identical results on arm64 and on amd64 (emulated).
- On 1.24.2 `jpegdec` emits only native formats. A direct RGB request
  after `jpegdec` (no `videoconvert`) is `not-negotiated` for YCbCr JPEGs:
  thor1's on-device probes show it for the canary and a generated 4:2:0
  JPEG, with `jpegdec` offering only I420. The stock 24.04 runs fit that:
  g420, godd420, g444 and ggray fail, and crgb passes as native RGB. No
  direct-conversion path was observed on 1.24.2, and no catalog or legacy
  chain makes a direct request anyway. (Iteration 3 read the stock result
  as "1.24 still has the broken path"; review 3, finding 5, corrected it.)
- The unconstrained tail `videoconvert ! fakesink` (no RGB request) also
  takes the direct path on 1.20.3: `jpegdec` negotiates xBGR and fails a
  4:4:4 JPEG (review 3's probe, in the generic arm64 CPU image). The Dell's
  diag-narrowing cases 3 and 10 show the same on 1.0.51. Only a downstream
  that prefers I420, such as `jpegenc`, avoids it. Task 4.1 adds this pair
  as U0/X0.
- Which JPEGs fail depends on the CPU's libjpeg-turbo SIMD path as well as
  the JPEG: the Dell failed a 320x240 4:2:0 JPEG that passes in the emulated
  container. The only safe rule is to never let `jpegdec` take the
  direct-output path.

**LocalServer images with the real `emexifextract`.** The exact compiled
chain was run unfixed and then fixed, on 576x768 4:2:0, 320x240 4:2:0 and
640x480 grayscale JPEGs:

| Image | Unfixed | Fixed |
|---|---|---|
| JP5, 20.04 / 1.16.3 | pass ×3 | pass ×3 |
| amd64, 20.04 / 1.16.3 | pass ×3 | pass ×3 |
| arm64 CPU, 22.04 / 1.20.3 | grayscale fails | pass ×3 |
| JP7, 24.04 / 1.24.2 | pass ×3 | pass ×3 |

An EXIF orientation-6 640x480 JPEG ends as 480x640 with the capsfilter in
place (JP5 and arm64 CPU images), so `emexifextract` and `videoflip
method=automatic` still rotate.

**Hardware.**
- Dell, 1.0.51 (22.04 / 1.20.3): fails 6/6. The Dell canary is a byte copy
  of `/aws_dda/cookies/test-normal-1.jpg`, which is byte-identical to the
  repo's `datasets/cookie-dataset/test-images/test-normal-1.jpg` (sha256
  `9fabe604…`). Measured offline from the repo copy, not inferred (design
  iterations 4 to 7): SOF0 baseline, 8-bit, 576x768, three components
  each sampled 1x1, so 4:4:4 (Pillow `get_sampling` 0), EXIF orientation
  1. So it is one of the failing kinds.
  thor1's canary, `test-normal-8.jpg` from the same set, measures the
  same.
- Dell, 1.0.50 (20.04 / 1.16.3): passes.
- MIC-730, JP5 (1.16.3): passes.
- thor1, JP7 (1.24.2), LocalServer.arm64JP7 1.0.56: check 3 passed on
  2026-10-09 through the direct `_jpeg_file_chain`. Its read-only probes
  show `jpegdec` offering only I420 there, and `not-negotiated` for RGB
  straight after `jpegdec`.
- Orin, JP6: PNG staging, so `jpegdec` never runs.

The failure is therefore confirmed only on Ubuntu 22.04 / GStreamer 1.20.3.

**Link to JP6's PNG staging.** Commit d3ef268 (2026-06-09) added the JP6
Pillow → PNG → `pngdec` path for "Improper call to JPEG library in state
205" once a model is loaded, and blamed a libjpeg collision with libdlr.so.
The identical error reproduces in a stock 22.04 container with no libdlr.so.
"Once a model is loaded" matches the model branch's `capsfilter
format=RGB`, which is the request that triggers the direct path. The same
commit added `jpegparse` against "intermittent no valid frames" on 1.20,
and the varying failure subset looks intermittent. So the staging is very
likely a workaround for this same defect. It works on JP6 and stays
unchanged (D9).

## Device-side vs Packaged Pipeline (determination)

**Workflow-engine workflows: the chain, and the order of the source
segments, are baked into the package by the Portal.** The code shows it:

- Portal: `edge-cv-portal/backend/functions/workflow_packaging.py` is the
  Component_Packager Lambda. For each selected arch it calls
  `compile_workflow(graph, arch, …)` (line ~2659), using the WorkflowCoreLayer
  copy of `workflow_core`. It then derives `bindingPoints` from the compiled
  dict (`build_binding_points`, line ~2708; slots address segments by index)
  and writes `compiled_pipeline.json` into the per-arch artifact zip
  (`zf.writestr('compiled_pipeline.json', …)`, line ~2236).
- The WorkflowCoreLayer is `lambda.Code.fromAsset(…/backend/layers/workflow_core)`
  (`compute-stack.ts` lines 182-183). Every ComputeStack deploy publishes
  whatever tree it is deployed from.
- Device: `workflow_engine/discovery.py` validates the artifact's
  `compiled_pipeline.json` (`COMPILED_PIPELINE_FILE`, line 58) and records
  `registration.arch` from the manifest's `targetArch`. Discovery rejects a
  package whose arch doesn't match `device_arch` (lines 186-198).
- Device: `WorkflowExecutor.execute()` (`pipeline_executor.py`) loads the
  document per run (`_load_compiled_document`, line ~3827), substitutes
  Camera_Binding slots (`camera_binding._substitute_slots`, by segment
  index), points the fed appsrc at the frame (`_point_appsrc_at_frame_feed`),
  and applies the run-time normalizations: `_stage_frame_sources`,
  `python_bridge.rewrite_document`, `_ensure_terminal_sink` (line 1129,
  called at 1918) and `_ensure_writable_inference_branch` (line 1161). It
  then renders with `rendering.render_launch_string` (line 1925), which
  emits segments in list order. This fix adds C3 and C3b right before
  `_ensure_terminal_sink`.
- No device module calls the compiler. The vendored `workflow_core` is
  imported on the device only for arch ids (`environment.py`), parameter
  checks (`camera_binding.py`), stream URL helpers and
  `anomaly_invocation`.
- The lab harness (`build_test_workflow.py`, in the uncommitted evidence
  bundle) compiles test packages with the vendored copy of the tree its
  `REPO` constant names, today the `remediation` worktree. It emits only
  `pythonSourceBinding` points. Task 8 uses a copy with a required `--repo`
  argument and the Aravis and stream binding points as well.

**Legacy Pipeline_Configuration workflows: the chain is built on the device
at run time.** `GstPipelineBuilder` builds it in
`src/backend/gstreamer/pipeline_builder.py`, `_add_file_image_source`, line
112. A LocalServer rebuild fixes these directly. A Pipeline_Configuration
has one image source, so no legacy string mixes a file source with an
appsrc.

**Consequences.**

1. The Portal copies of `catalog/nodes.py` and `compiler/compiler.py` are
   the real fix for packages compiled from now on. A ComputeStack deploy is
   needed before the Portal emits them. It is owner-gated and happens after
   the trunk merge (task 10).
2. Pre-fix packages, including every workflow deployed today, keep the
   unconstrained chain and graph-order segments. Without a device-side
   change they would keep failing on any 22.04 LocalServer until someone
   repackages and redeploys them, and the Portal deploy would have to come
   before the LocalServer rollout. That ordering trap is easy to miss.
3. The vendored copies must still change and stay byte-identical. The
   mirror test enforces it for both files (`compiler.py` through the
   `MIRRORED_SHARED_MODULE_RELPATHS` entry this fix adds), and the device
   imports the new constants.
4. Packages from the fixed Portal also reach older LocalServers. A
   single-source package is fine there: the capsfilter is harmless on
   1.16.3 and fixes 1.20.3. A mixed package is fine there too, because the
   compiler already put its feed segment first (C1b). Design iteration 3
   ran such packages through the arm64 CPU and JP5 images' own pre-fix
   executor code: EOS for Aravis, stream and Python-source frames. Without
   C1b the same packages abort (exit 134), and a continuous stream
   registration would crash-loop (bugfix 1.5). So C1 never ships without
   C1b, and the Portal deploy needs no station precondition (R5 is met by
   C1b). Task 10 still records a station inventory.

**Paths covered (all of them).**
- (a) The catalog source and its mirror, for new packages (C1, C2).
- (b) The compile-side feed-first order and its mirror, for new mixed
  packages on every LocalServer, older ones included (C1b, C2).
- (c) The executor normalization (C3, D4), for pre-fix packages, so a
  LocalServer rebuild alone repairs them.
- (d) The executor's feed-first rule (C3b, D10), for pre-fix mixed packages
  once C3 has given them a capsfilter, and for any other document whose fed
  appsrc isn't first.
- (e) The legacy builder, for Pipeline_Configuration workflows (C4).
- (f) The later Portal deploy, after which (c) and (d) are no-ops for new
  packages.

Repackaging existing workflows becomes optional. It only matters if the owner
wants old packages' stored documents to match the device render byte for
byte.

## Design Decisions

**D1 — A capsfilter after `jpegdec`, not PNG staging.**

| Option | Decision |
|---|---|
| (a) Native-output capsfilter after `jpegdec` | **Chosen**: one element in the existing chain, no file I/O, no new dependency. `emexifextract` and `videoflip` stay, so EXIF orientation keeps working. It fixes the negotiation at its source and works on 1.16, 1.20 and 1.24 (Root Cause). |
| (b) Extend JP6's PNG staging to x86 and arm64 CPU | Rejected. Each frame gets a Pillow decode plus a PNG encode. A `.dda_decoded.png` lands in the user's source folder, and folder-source-image-consumption already had to add cleanup for those. It drops `emexifextract`, `jpegparse` and `videoflip`, adds `python:pillow`, and needs arch gating in two executors. It also reshapes the catalog chain, so the baseline deltas grow. And it avoids `jpegdec` instead of fixing the negotiation. |
| (c) Build amd64 on Ubuntu 20.04 | Rejected by the owner's OS policy. |
| (d) Patch or upgrade GStreamer in the images | Rejected. The images use distro packages and the owner keeps stock 22.04. A newer GStreamer is no shortcut either: 1.24.2 shows no direct-conversion path, but RGB still has to come from `videoconvert` there, because a direct RGB request after `jpegdec` doesn't negotiate. |
| (e) Another decoder (`avdec_mjpeg`, `nvv4l2decoder`, `decodebin`) | Rejected. libav isn't in every image, the pixel path would differ, `nvv4l2decoder` mis-decodes these JPEGs (d3ef268), and `decodebin` would plug `jpegdec` anyway. |
| (f) `video/x-raw,format=I420` | Rejected by evidence. It breaks grayscale and RGB-colourspace JPEGs on every version, which is exactly the owner's concern. |

**D2 — The caps string.**

- **Separator.** Structures are separated by `;`, not given as a `{…}` list.
  `compiler._resolve_value` runs `str.format_map` over every
  `args_template` string, so braces would be read as a placeholder. The `;`
  form has no braces. `rendering.render_value` quotes it automatically,
  because `;` is in `_UNSAFE_VALUE_PATTERN`. The rendered token is
  `caps="…"`, the exact token verified with `gst-launch`. The legacy builder
  quotes it the same way.
- **Order.** I420 comes first, and this is load-bearing: `jpegdec` on 1.20.x
  decides `format_convert` from the first structure the peer offers that it
  supports. A unit test pins the order.
  - Y42B and Y444 aren't in `jpegdec`'s src template on any shipped version,
    so they are inert today. They let a future `jpegdec` that emits
    subsampling-native 4:2:2 or 4:4:4 still negotiate, as the owner asked.
  - GRAY8 covers grayscale JPEGs.
  - RGB, listed last, covers RGB-colourspace JPEGs. Their RGB is native,
    never the direct path.
- **Quality.** 4:2:2 and 4:4:4 JPEGs lose chroma resolution through I420.
  1.16.3 always did the same (1.0.50, JP5), so this is no regression against
  the working baseline. Pixels follow the 1.0.50 path (native YUV, then
  `videoconvert`), not libjpeg-turbo's direct conversion.

**D3 — Arch scope: `x86_64`, `x86_64_nvidia`, `arm64_cpu`.**

- `x86_64`'s OS follows the build host (20.04 or 22.04), and the cloud
  compiler can't know which. The capsfilter is harmless on 1.16.3: verified
  in stock 20.04 and in a 20.04 amd64 LocalServer image.
- `x86_64_nvidia` is 22.04 (`nvidia/cuda:12.6.3-cudnn-devel-ubuntu22.04`).
- `build-custom.sh` (lines ~78-86) forces `arm64_cpu` to 22.04, and its
  image fails unfixed on grayscale JPEGs.
- Excluded:
  - `arm64_jp5`: pinned to 20.04 / 1.16.3, where the direct path doesn't
    exist.
  - `arm64_jp6`: uses `pngdec`.
  - `arm64_jp7`: on 1.24.2 `jpegdec` emits only native formats, and no
    direct-conversion path was observed (thor1's check 3 PASS and probes,
    plus the matrix).
- Excluding those three keeps their chains and their segment order
  byte-identical. The new code still ships to Jetsons, because
  `pipeline_executor.py` and `pipeline_builder.py` are in every image: C3
  and C3b evaluate their arch gate on every run, and C4's lazy imports and
  `device_arch()` run on every JP5 and JP7 legacy file-source build. So:
  - no JP5, JP6 or JP7 release rebuild is needed for this bug, because the
    gated code leaves their launch strings byte-identical (unit goldens,
    task 4's renders);
  - the steering's "test on every arch the change touches" is met by one
    owner verification build on thor1 (task 8b, D11).
- Rejected alternative: apply it to every non-JP6 chain. That would change
  the JP5 and JP7 chains, and so force their full re-verification, for no
  benefit. On 1.24.2 a direct RGB request after `jpegdec` is
  `not-negotiated` for YCbCr JPEGs with or without the capsfilter, and no
  chain makes one. If a JP7 image ever moves to a GStreamer whose `jpegdec`
  takes the direct path behind `videoconvert` (task 4.1's U chain on that
  image shows it), adding `ARCH_ARM64_JP7` to the tuple is a one-line
  change plus a thor1 check. That is backlogged.

**D4 — Normalize pre-fix packages on the device.**

The executor gains `_constrain_jpegdec_output(document, arch)`, which works
on the per-run document.
- Alternative (i), repackage only, is rejected because of the ordering trap
  in Consequence 2.
- Alternative (ii), rewriting any `jpegdec ! videoconvert`, is rejected as
  too broad: it would also rewrite hand-written and custom chains.
- **Chosen:** match only the exact catalog sequence on a constrained arch.
  The executor already corrects compile output at run time with this
  pattern (`_ensure_terminal_sink`, `_ensure_writable_inference_branch`,
  `roi_crop.insert_crop_after_node`).
- Trade-off: for a pre-fix package, the device render differs from the
  stored document by one element per JPEG chain, and a mixed Frame_Feed
  document also differs in segment order (D10). The sandbox never renders
  device-arch documents, so the "cloud and edge agree" rule in
  `rendering.py` still holds for what it covers.

**D5 — One constant, one owner.** `JPEGDEC_NATIVE_CAPS` and
`JPEGDEC_NATIVE_CAPS_ARCHS` live in `workflow_core/catalog/nodes.py`, which
the mirror guards. The compiler imports the tuple from `..catalog.nodes`.
The executor and the legacy builder import both from
`workflow_engine.vendor.workflow_core.catalog.nodes`. No other literal copy
of the caps string or the arch set may exist.
- The executor imports them at module level, so its test asserts identity
  (`is`) with the vendored constants. The compiler's module attribute is
  checked the same way in the Portal test.
- The legacy builder imports them lazily inside `_add_file_image_source`,
  so its test asserts the rendered token built from the imported constant.

**D6 — Re-sync by per-file copy, not `re_vendor.sh`.**

- The vendor `README.md` calls `re_vendor.sh` the only path. But
  `test_vendored_catalog_mirror.py` says a wholesale re-vendor would also
  copy the Portal-only `catalog/platforms.py`, which the vendor tree omits
  on purpose, together with the Portal `catalog/__init__.py` that imports
  it. Running the script would change the device tree beyond this fix.
- So the fix copies exactly two Portal files over their vendored
  counterparts: `catalog/nodes.py` (the remedy the mirror test prints) and
  `compiler/compiler.py`. The two `compiler.py` copies are byte-identical
  today (`diff -rq` lists only the two `catalog` differences).
- The mirror test `test_vendored_catalog_mirror.py` covers more than the
  vendor README's "Current scope" paragraph says (that paragraph is stale):
  - `catalog/nodes.py` and `catalog/models.py` (`MIRRORED_FILENAMES`);
  - `anomaly_invocation.py` (`MIRRORED_PACKAGE_FILENAMES`);
  - `stream_url.py`, `analytics/__init__.py`, `analytics/scene.py`,
    `validator/checks.py` and `validator/__init__.py`
    (`MIRRORED_SHARED_MODULE_RELPATHS`, paths relative to `workflow_core/`).

  It does not cover the compiler. The fix appends `"compiler/compiler.py"`
  to `MIRRORED_SHARED_MODULE_RELPATHS`, plus one bullet in the comment above
  the tuple. That bullet says the device never imports the compiler, but
  the lab harness and the backend compile tests do, so a drifted vendored
  copy would build packages the Portal never would. The existing
  parametrized test then checks the two copies byte for byte and already
  prints the per-file `cp` remedy. No other line of the mirror test
  changes, and no separate sha256 test is added (review 3, finding 7).
  Task 3.2 also runs the README's `diff -r`, which must list only the two
  pre-existing differences.
- Pre-existing failure, neither caused nor fixed here:
  `test_catalog_content.py::TestCatalogMirrorEquality::test_portal_and_vendor_catalog_init_are_byte_identical`
  fails today because of that deliberate `__init__.py` difference.
  Reconciling the README, that test and the omission is backlogged.

**D7 — Recorded outputs change only through their documented paths.**

- **`catalog_baseline.json`.** Follow the vendor README's "Scoped refresh"
  (5 steps), replacing only `folder_source` and `csi_camera_source`, with
  one correction to step 4: re-dump with `ensure_ascii=False` and write
  UTF-8 bytes.
  - Why: the committed baseline holds 15 literal em dashes (U+2014) in six
    other descriptors, `bedrock_inference` and `custom_python_source` among
    them. It round-trips byte for byte only with `ensure_ascii=False`. The
    README's form (`ensure_ascii` at its default) rewrites them as `\u2014`
    escapes, and those extra deltas trip step 5's stop rule (review 3,
    finding 1; re-measured in design iteration 4).
  - The exact snippet is in tasks.md 3.6. Its first assertion checks that
    the unmodified baseline round-trips, before anything changes.
  - The vendor README is not edited: task 3.2 allows exactly two modified
    files under `vendor/`. Its step 4 is backlogged.
  - Expected delta: one capsfilter element in each of the two descriptors'
    `x86_64`, `x86_64_nvidia` and `arm64_cpu` mappings. That is 6 blocks,
    36 inserted lines and no deletion (measured on a scratch export with C1
    applied, design iteration 4).
  - Append one maintenance-log paragraph to the comment block above
    `_LLM_PRESERVED_PARAMETERS` in `test_bug_catalog_preservation.py`, which
    is the existing log.
  - The baseline has two consumers, and both must pass after the refresh:
    `test_bug_catalog_preservation.py` (two descriptor tests) and
    `test_catalog_stream_and_analytics.py::TestCatalogAppendIsAdditive::test_every_pre_existing_descriptor_content_is_byte_identical`.
- **`golden_zero_trigger_compilation.json`.** Regenerate with
  `_write_golden()`, but not with the command its module docstring gives.
  - Why: `PYTHONPATH=python` puts the layer's bundled third-party packages
    ahead of the host's site-packages. The bundled `rpds` is an x86_64
    cp311 extension, so on the aarch64 host the import chain jsonschema →
    `referencing` → `rpds` fails with `ModuleNotFoundError: No module named
    'rpds.rpds'` (reproduced in design iteration 4). The suite itself works
    because `tests/conftest.py` appends the layer path instead.
  - Task 3.7's command does the same: it appends `python` to `sys.path`
    after putting the layer root first.
  - The docstring is not edited, because a recorded-output test is never
    edited (last bullet of this decision). Its correction is backlogged.
  - Expected delta: 21 inserted lines, one 7-line capsfilter block in each
    of the `x86_64`, `x86_64_nvidia` and `arm64_cpu` `folder_capture`
    documents (measured, design iteration 4). The `arm64_jp5`, `arm64_jp6`
    and `arm64_jp7` documents and `icam_inference_mqtt` don't change. C1b
    changes nothing in it: the golden holds no appsrc (checked: 0
    occurrences).
  - After both refreshes, the two baseline consumers and the two
    zero-trigger files pass: 49 passed (design iteration 4).
- **`test/backend-test/csi_nvargus_optional/goldens/backend_csi_consumers_sha256.json`.**
  Rebaseline the entries for `pipeline_builder.py`, `pipeline_executor.py`
  and the vendored `catalog/nodes.py`, with `sha256sum` of the final files,
  as commits dfdc116, bf8f74a, 89d1655 and 76fbdd5 did. Its `nodes.py`
  entry has been stale since 6bda4de, a pre-existing failure, and the notes
  record that. The golden has no compiler entry.
- **Expected failures before the refresh.** Measured in design iteration 3
  with C1, C2 and C1b applied to a scratch export: the workflow_core suite
  fails 8 tests. One is pre-existing (the `__init__.py` mirror test). Two
  are exact-chain expectations task 3.5 updates
  (`test_mappings_read_the_csi_capture_file`,
  `test_chain_elements_tagged_with_node_id`). Five are recorded-output
  tests that only tasks 3.6 and 3.7 resolve: the three baseline-consumer
  tests above,
  `test_property_zero_trigger_preservation.py::test_legacy_workflow_compilation_matches_committed_golden`
  and
  `test_property_no_new_trigger_preservation.py::test_zero_trigger_golden_comparison_unchanged`.
  C1b alone adds no failure (1022 passed, 1 failed, 1 skipped, as the
  baseline).
- **Any delta outside these** means stop and investigate. Never hand-edit
  the baseline JSON, the golden or a recorded-output test, and never loosen
  a test. Existing exact-chain expectations are updated only to the new
  exact chain (list under Changes Required).

**D8 — `sim` chain and sandbox: out of scope, backlogged.** It is the same
class, on the Ubuntu 22.04 Fargate sandbox. But it is a separate deployable
(the Portal plus the sandbox image), nobody reported a failure, and the fix
would change the `sim` mappings of 7 descriptors, sandbox code and the `sim`
compile tests. The sandbox compiles with `simulation=True`, which C1b skips.
A follow-up spec can reuse `JPEGDEC_NATIVE_CAPS`.

**D9 — JP6 PNG staging stays.** It works, and retiring it needs Orin
verification. The finding (Root Cause) goes into the backlog. The
`_jp6_png_staged_chain` comment and the JP7 mapping comment in `nodes.py`
stay as they are, to keep the diff minimal. The design records that the
libdlr attribution is very likely wrong.

**D10 — Keep the fed appsrc's caps first: at compile time and on the
device.**

Both Frame_Feed consumers take the FIRST `caps=` clause of the launch
string, up to the next `!` (`re.search(r"caps=([^!]+)", …)`):
- `GstPipelineManager.create_buffer` (`gst_pipeline.py` lines 94-101), for
  every non-bridged Frame_Feed run;
- `python_bridge._fed_frame_caps` (line 1781), for bridged runs, but only
  when the frame has no explicit `format`. Stream frames always carry
  `format` "RGB" and Produced_Frames carry the producer's format, so in
  bridged runs only Aravis grabs read the launch string.

Today a JPEG file chain renders no `caps=`, and the fed appsrc's own clause
(`appsrc name=appsrc caps=…`) wins. After the fix the file chain renders
`capsfilter caps="…"`. The validator accepts a document holding a file
source and a frame-feed source, and the compiler emits source segments in
graph node order, so the file chain can come first. The appsrc then gets
the quoted five-structure string, `Gst.Caps.from_string` returns None, and
the push aborts the process (bugfix 1.5, measured on 1.16.3, 1.20.3 and
1.24.2). A triggered registration aborts the backend on every trigger. A
continuous stream registration aborts it at the first tick after every
start: `ContinuousRunnerManager` starts a runner for every registered
continuous registration, and `restart: always` restarts the backend. The
operator pause persists across restarts, but only if the API is reached
between them, so treat it as a crash loop until the registration is
removed.

| Option | Decision |
|---|---|
| (a) Anchor both lookups to the fed appsrc's own `caps=` and fall back to today's rule | Backlogged. It changes `gst_pipeline.py` and `python_bridge.py`, which run on every arch, so JP5, JP6 and JP7 device code changes and needs MIC-730, Orin and thor1 re-verification. It also protects nothing on LocalServers that predate it. |
| (b) The executor moves the fed appsrc's segment to index 0 before rendering, on constrained arches (C3b) | **Chosen**, for packages compiled before the fix: C3 gives their file chains a capsfilter in graph order. |
| (c) The compiler emits the frame-feed source's segment first, on constrained arches (C1b) | **Chosen**, for every package the fixed Portal compiles. Older LocalServers render segments in document order, so the fed `caps=` comes first there too. Measured with the images' own pre-fix code (Consequence 4). |
| (d) A Portal deploy-time floor (raise `WORKFLOW_MIN_LOCAL_SERVER_VERSIONS` for x86_64 and arm64_cpu, or add a mixed-document floor) | Rejected. It needs the version of builds that don't exist yet, blocks deploys instead of making the package safe, and a plain floor would block every workflow on older stations. |
| (e) Only the procedural station gate of iteration 2 | Rejected. Any ComputeStack deploy from the trunk after task 11 publishes the layer, whatever task 10 says, and amd64 1.0.50 stations would crash-loop on continuous mixed registrations (review 2, finding 2). |
| (f) Do nothing | Rejected. A pre-fix mixed package would abort the backend on the fixed LocalServer as soon as C3 inserts the capsfilter. |

- **Why both.** (c) alone leaves pre-fix packages exposed on the fixed
  LocalServer, where C3 creates the hazard. (b) alone leaves older
  LocalServers exposed to new packages. Together, no package from either
  Portal reaches the hazard on either kind of LocalServer.
- **Why (c) runs inside `compile()`.** The packager's `build_binding_points`
  and the device's `camera_binding._substitute_slots` address segments by
  index (`slot["segment"]`). The packager derives the slots from the dict
  `compile()` returns, so a reorder inside `compile()` keeps them right,
  and a reorder applied later would not. Measured: an `icam_source` plus
  Aravis graph compiles for x86_64 to `[s1 appsrc, s0 v4l2src]`, so the icam
  slot is derived as segment 1.
- **Names.** Neither rule renames anything. Segment names (`s<N>`) aren't
  rendered, and tee and funnel names (`t<N>`, `f<N>`) stay as compiled.
- **Tee and funnel safety.** A source segment has no `from`, so no tee
  branch is ever moved ahead of its tee. A `linkTo` funnel reference from
  the moved segment becomes a forward reference. The compiler already emits
  those for fan-in documents, and `Gst.parse_launch` accepted one on
  1.16.3, 1.20.3 and 1.24.2 (iteration 2).
- **Gating.** Both rules run only on `JPEGDEC_NATIVE_CAPS_ARCHS`, and (c)
  also skips `simulation=True` compiles (the sandbox path). So JP5, JP6, JP7
  and `sim` documents stay byte-identical. `gst_pipeline.py`,
  `python_bridge.py` and `rendering.py` don't change.
- **Why not gate on "an earlier segment renders `caps=`".** On a
  constrained arch every catalog file chain now carries the capsfilter, so
  for realistic mixed documents the two gates select the same documents.
  The simpler rule also needs no copy of the consumers' regex.
- **Shipping together.** C1 never ships without C1b, and C3 never ships
  without C3b. All four land in the single fix commit. Guards: task 1's
  compile test fails if C1b is missing; task 1's and task 2's matrix rows
  fail if C3 lands without C3b.
- **Side effect on constrained arches.** Both rules also fix the
  pre-existing first-`caps=` shapes there: a bridge appsink (bugfix 1.6), a
  bedrock feeder's I420 capture chain, model_inference's RGB capsfilter or
  a `format_convert` capsfilter rendered ahead of the fed appsrc. On JP5,
  JP6 and JP7 those stay as they are (D12).

**D11 — The owner's mixed-flow gate (R1 to R5): how it is proven.**

The gate is bugfix 3.11 plus AC 11 to 15. Every validator-accepted mixed
shape is reachable from the Portal, so each dimension of R1 is a matrix axis.

| R1 dimension | Values | Where it is covered |
|---|---|---|
| File source | `folder_source`; `csi_camera_source` (same file chain on the five jpegdec arches) | Compile tests (AC 11), unit matrix subset, offline compile and render |
| Feed type | Aravis 1 byte/pixel (GRAY8); Aravis 3 bytes/pixel (RGB-sized, the static image camera); stream (RTSP, and RTMP at compile level); `custom_python_source` | Unit and offline matrices; Dell 3c |
| Segment order | file first; feed first | Unit and offline matrices; Dell P and F packages |
| Package shape | pre-fix (no capsfilter, graph order); fixed (capsfilter; feed first on constrained arches) | Unit and offline matrices; Dell 3a/3b/3c P and F |
| Run | non-bridged (`create_buffer`); bridged (a pass-through `custom_python` on the file branch, `run_bridged_pipeline` and `_fed_frame_caps`) | Unit and offline matrices; Dell 3c-aravis-bridged |
| Target | x86_64, x86_64_nvidia, arm64_cpu (must work); arm64_jp5, arm64_jp6, arm64_jp7 (byte-identical) | Unit matrix (all six); offline images JP5, JP6, arm64 CPU, JP7 |

**What "works" means, per layer.**
- Unit (host, no GStreamer; task 1 bug rows, task 2 preservation rows):
  the recorded launch string starts with the fed appsrc (constrained);
  its first `caps=` clause is one whitespace-free token equal to
  `_frame_caps(frame)`; `_fed_frame_caps(launch, frame)` equals
  `_frame_caps(frame)` plus `,width=W,height=H`; each file chain renders
  exactly one capsfilter; the execution row is `completed`. Jetson rows
  equal their unfixed-tree goldens byte for byte.
- Offline (task 4, R2): each row runs in its own process in a LocalServer
  image, with the production `Gst.parse_launch` and consumers, through
  task 4's row command (`--init`, unbuffered output). Pass: EOS, exactly
  one buffer at each branch's sink pad (counted with a pad probe), exit 0,
  and the fed appsrc's caps equal the frame caps. Exit 134 is SIGABRT
  under `--init`, a fail (review 7, finding 3).
- Older LocalServers (task 4, R5): fixed-tree packages run through the
  arm64 CPU and JP5 images' own baked executor code (`PYTHONPATH=/`),
  same pass rule. The local amd64 image predates the workflow engine, so
  it can't run a package at all (review 7, finding 2).
- Dell (task 8, R3): every 3c run completes, with the proof of both frames
  (task 8), inside a 30-minute soak with StartedAt and RestartCount
  unchanged.
- thor1 (task 8b, R4): check 3's launch string equals 1.0.56's after
  bugfix 2.11's normalization (only the `location=` and
  `buffer-message-id=` values are masked), no INFO lines from C3/C3b, one
  mixed flow completes, 30-minute soak.

**Bug rows and preservation rows.** On the unfixed tree, a constrained row
fails exactly when its production consumer reads a clause other than the
fed appsrc's: the fixed shape file-first (all feeds, non-bridged; Aravis
when bridged), and the pre-fix shape file-first bridged with an Aravis feed
(1.6). Those are task 1's rows and must FAIL before the fix. Every other
constrained row works today and is task 2's: it must PASS before and after,
including after C3 has inserted the capsfilter (the "C3 never without
C3b" guard). The "launch starts with the fed appsrc" assertion is a fix
property, so it lives only in task 1. The "exactly one capsfilter per file
chain" assertion of AC 12 is split by shape: every constrained pre-fix-shape
row counts it in task 1(i3), where it fails before the fix (0) and passes
after (C3 inserts one), and every constrained fixed-shape row counts it in
task 2, where it is 1 before and after (C3 must not insert a second).

**R4.** thor1 is recommended for the Jetson step: a same-day 1.0.56 check 3
baseline exists (`thor1-rerun/after/checks/check3-custom-python/pipeline-string.txt`
in the lab evidence), JP7 runs the jpegdec chain and the legacy non-JP6
branch where C4 runs, and the re-run harness is ready. MIC-730 (JP5) is the
alternative if the owner weighs the JP5 fleet higher. Orin (JP6) would
exercise less: C4 never runs there, and C3/C3b only evaluate the gate.

**R5.** Met by C1b (D10), which lands in the same commit as C1, so every
layer the Portal can publish carries both. Task 10 still runs and records
the station inventory (counts and versions only). If C1b were ever absent
from the deploy tree or failed its checks, R5's station rule would apply
instead, and any failing, offline or unknown station would block the
deploy. The "owner accepts in writing" path is not available under R5
unless the owner re-enables it in writing.

**D12 — Q1: pre-existing first-`caps=` shapes on JP5, JP6 and JP7.**

**Reachable from the Portal.** The validator accepts a file source plus one
frame-feed source, and the compiler emits source segments in graph node
order. A file branch renders its own `caps=` ahead of the fed appsrc when it
holds any of these:
- a bridge appsink (`BRIDGE_APPSINK_CAPS`, RGB), added at run time by
  `python_bridge.rewrite_document` for a `custom_python` node. The appsink
  ends its segment, so the clause runs on into the next segment's text;
- a bedrock feeder's capture chain (`compiler._capture_chain`, `capsfilter
  caps=video/x-raw,format=I420`);
- model_inference's `capsfilter caps=video/x-raw,format=RGB`;
- `format_convert`'s `capsfilter caps=video/x-raw,format={format}`.

Design iteration 3 compiled three of these graphs (file source first, then
an Aravis or Python-source feed) with the vendored compiler for arm64_jp5,
arm64_jp6 and arm64_jp7. The first rendered `caps=` was
`video/x-raw,format=RGB` (model_inference), `video/x-raw,format=BGR`
(`format_convert` BGR) and `video/x-raw,format=RGB appsrc name=py_out_m
is-live=true format=time block=true` (the bridge). The same graphs for
x86_64 rendered the fed appsrc's `video/x-raw,format=GRAY8` first, under
C1b.

**What happens today (measured, design iteration 3).** Each row ran in its
own process in the images labelled `flask-app:latest` (JP5, 1.16.3),
`flask-app:1.0.46` (JP6, 1.20.3) and `flask-app:arm64JP7-1.0.45` (JP7,
1.24.2). These are redacted labels; verification-notes, "Design iteration
4", gives their local image IDs. The runs used the repo's
`create_buffer`, or the bridged fed-source steps of `run_bridged_pipeline`
(`_fed_frame_caps`, `Gst.Caps.from_string`, strict
`reconcile_to_caps_stride`, then the push). A 64x48 frame was pushed. All
three images gave the same result:

| First `caps=` (source) | Fed frame | Outcome on JP5, JP6 and JP7 |
|---|---|---|
| RGB (model_inference, `format_convert` RGB) | Aravis GRAY8, non-bridged | `create_buffer` raises `PipelineExecutionException` ("Frame buffer cannot be reconciled…"): the run fails, contained |
| I420 (bedrock capture chain) | Aravis GRAY8, non-bridged | Caps accepted; a 3072-byte buffer is pushed under I420 caps that need 4608. EOS into `fakesink`. With a real consumer the frame is wrong-size (task 4 measures it with `jpegenc`) |
| BGR (`format_convert` BGR) | Produced_Frame RGB, non-bridged | EOS; channels silently swapped |
| RGB | Produced_Frame BGR, non-bridged | EOS; channels silently swapped |
| Bridge appsink, run-on clause | Aravis GRAY8, bridged | `from_string` → None; the strict reconcile raises: contained failure |
| Bridge appsink, run-on clause | Aravis 3 bytes/pixel, bridged | `from_string` → None; the size check passes; the push **aborts the process** (SIGABRT, exit 134) |

So the review's expectation that no shape aborts was wrong for the bridged
shape with a 3-byte/pixel camera. The static image camera serves packed
RGB through the Aravis feed path, so a bridged folder-first workflow bound
to it would abort a Jetson backend today. The same shape aborts on x86 today
too; C1b and C3b fix it there.

**Recommendation.** Keep Jetson coverage out of this fix, as an explicit
owner decision (task 12):
- none of these shapes is caused or made worse by this fix, and the
  Jetson launch strings stay byte-identical (bugfix 3.1, 3.2);
- covering Jetsons means MIC-730, Orin and thor1 verification, which this
  bug doesn't otherwise need.

But because one shape aborts the backend, open a follow-up spec soon rather
than leaving it in the general backlog. In that spec, prefer widening the
two feed-first rules (C1b and C3b) to arm64_jp5, arm64_jp6 and arm64_jp7
over option (a):
- it changes only the order of mixed documents and leaves the consumers as
  they are;
- it protects older Jetson LocalServers for new packages, as C1b does here;
- it is verified on each Jetson.

Option (a) is not implemented without owner approval.

## Changes Required

### C1 — Portal catalog (source of truth)

File: `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/catalog/nodes.py`.
In the "Shared element chains" section, add the two constants above
`_jpeg_file_chain` and give the helper an `arch` argument:

```python
#: Caps that pin jpegdec to the formats it emits natively (no libjpeg-turbo
#: direct colour conversion): I420 for every YCbCr JPEG, GRAY8 for grayscale,
#: RGB for RGB-colourspace JPEGs; Y42B/Y444 admit a future jpegdec that emits
#: subsampling-native YUV. I420 MUST stay first: GStreamer 1.20 jpegdec
#: decides its direct RGB output from the first downstream format it
#: supports, and that path fails on 1.20.x ("Improper call to JPEG library
#: in state 205").
#: ';'-separated structures, no braces (args templates go through format_map).
JPEGDEC_NATIVE_CAPS = (
    "video/x-raw,format=I420;"
    "video/x-raw,format=Y42B;"
    "video/x-raw,format=Y444;"
    "video/x-raw,format=GRAY8;"
    "video/x-raw,format=RGB"
)

#: Architectures whose LocalServer image ships GStreamer 1.20.x (Ubuntu
#: 22.04) and decodes file JPEGs with jpegdec. JP5 (1.16.3), JP6 (pngdec) and
#: JP7 (1.24.2) never take the direct path and keep the unconstrained chain.
#: The compiler's feed-first order uses the same set (design D10).
JPEGDEC_NATIVE_CAPS_ARCHS = (ARCH_X86_64, ARCH_X86_64_NVIDIA, ARCH_ARM64_CPU)


def _jpeg_file_chain(location_template, arch):
    chain = [
        _element("filesrc", blocksize=-1, location=location_template),
        _element("emexifextract"),
        _element("jpegparse"),
        _element("jpegdec", **{"idct-method": 2}),
    ]
    if arch in JPEGDEC_NATIVE_CAPS_ARCHS:
        chain.append(_element("capsfilter", caps=JPEGDEC_NATIVE_CAPS))
    chain.append(_element("videoconvert"))
    chain.append(_element("videoflip", method="automatic"))
    return chain
```

- Update the comment above `_jpeg_file_chain` so it describes the optional
  capsfilter.
- Each of the 10 call sites passes its own mapping's arch, for example
  `_jpeg_file_chain("{location}", ARCH_X86_64)`. There are 5 in
  `CSI_CAMERA_SOURCE` and 5 in `FOLDER_SOURCE`, so the arch is stated twice
  on each line and a reviewer can check them at a glance.
- Don't change `plugin_dependencies`, `_jp6_png_staged_chain`,
  `_dataset_fed_sim_source`, any other descriptor, or `models.py`.

### C1b — Portal compiler: frame-feed segment first (D10, option c)

File: `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/compiler/compiler.py`.

- Imports: `from ..catalog.nodes import JPEGDEC_NATIVE_CAPS_ARCHS,
  SOURCE_KIND_TO_SOURCE_TYPE` (extending the existing line) and
  `from ..validator import FRAME_FEED_SOURCE_TYPES, SEVERITY_ERROR,
  validate` (extending the existing line; `validator/__init__.py` already
  exports the set).
- In `compile()`, right after `segments = _build_segments(...)`:

```python
    # Frame-feed segment first on the jpegdec-constrained arches (design
    # D10, option c). Before the packager derives bindingPoints slots,
    # which address segments by index.
    if not simulation:
        segments = _feed_segment_first(segments, nodes_by_id, target_arch)
```

- Add, next to `_build_segments`:

```python
def _feed_segment_first(
    segments: List[dict],
    nodes_by_id: Dict[str, Node],
    target_arch: str,
) -> List[dict]:
    """The segments with the frame-feed source's appsrc-headed segment moved
    to index 0 on the arches in JPEGDEC_NATIVE_CAPS_ARCHS.

    GstPipelineManager.create_buffer and python_bridge._fed_frame_caps read
    the launch string's FIRST caps= clause as the fed appsrc's caps, and on
    these arches a JPEG file chain renders a capsfilter caps= of its own.
    Emitting the feed segment first keeps the fed caps first on every
    LocalServer, including those that predate the executor's own rule.
    Segment, tee and funnel names are unchanged; a source segment has no
    "from", so no tee branch moves ahead of its tee. The single-feed rule
    (validator V7) allows one such segment. Other arches, and documents
    whose feed segment is already first or absent, are returned unchanged
    (the same list object). The scan stops at the first frame-feed segment,
    as the executor's rule does."""
    if target_arch not in JPEGDEC_NATIVE_CAPS_ARCHS:
        return segments
    for index, segment in enumerate(segments):
        elements = segment["elements"]
        head = elements[0] if elements else None
        node = nodes_by_id.get(head["nodeId"]) if head else None
        if (segment["from"] is None and node is not None
                and head["factory"] == "appsrc"
                and node.type in FRAME_FEED_SOURCE_TYPES):
            if index == 0:
                return segments
            return [segment] + segments[:index] + segments[index + 1:]
    return segments
```

- The loop stops at the first frame-feed segment: it returns the input
  list unchanged when that segment is already at index 0, and otherwise
  moves it to the front. Iteration 3's loop skipped index 0 and kept
  scanning, so a second frame-feed segment (unreachable under V7) would have
  moved ahead of the first, unlike C3b (review 3, finding 6).

- `segments` and their elements are the compiler's own dicts (built by
  `_build_segments` and `_resolve_chain`), so direct key access is safe;
  synthetic elements carry `nodeId` None and never match.
- `nodes_by_id` is built after `expand_unified_inputs`, so a `unified_input`
  set to a stream or camera source is matched by its expanded type.
- Nothing else in `compile()` changes. In simulation the frame-feed nodes
  compile to dataset stubs (`multifilesrc`), so the gate is belt and braces.

### C2 — Vendored mirror (two files)

Copy each Portal file over its vendored counterpart, then `cmp` each pair:
- `…/workflow_core/catalog/nodes.py` →
  `src/backend/workflow_engine/vendor/workflow_core/catalog/nodes.py`;
- `…/workflow_core/compiler/compiler.py` →
  `src/backend/workflow_engine/vendor/workflow_core/compiler/compiler.py`.

No other vendored file changes (D6). The README's `diff -r` must list only
`catalog/__init__.py` and `catalog/platforms.py`, as today.

### C3 — Executor normalization

File: `src/backend/workflow_engine/pipeline_executor.py`.

- Add a module-level import: `from workflow_engine.vendor.workflow_core.catalog.nodes import JPEGDEC_NATIVE_CAPS, JPEGDEC_NATIVE_CAPS_ARCHS`.
- Add a `@staticmethod` next to `_ensure_terminal_sink`:

```python
    @staticmethod
    def _constrain_jpegdec_output(document, arch) -> int:
        """Insert capsfilter caps=JPEGDEC_NATIVE_CAPS between jpegdec and
        videoconvert in every catalog JPEG sequence (jpegparse, jpegdec,
        videoconvert) of a document compiled before the fix, on the arches
        in JPEGDEC_NATIVE_CAPS_ARCHS. Returns the number inserted. Never
        raises on JSON-shaped input; anything else is left untouched."""
        if not isinstance(document, dict) or arch not in JPEGDEC_NATIVE_CAPS_ARCHS:
            return 0
        segments = document.get("segments")
        if not isinstance(segments, list):
            return 0
        inserted = 0
        for segment in segments:
            elements = segment.get("elements") if isinstance(segment, dict) else None
            if not isinstance(elements, list):
                continue
            index = 1
            while index < len(elements) - 1:
                before, current, after = elements[index - 1:index + 2]
                if (_factory(before) == "jpegparse" and _factory(current) == "jpegdec"
                        and _factory(after) == "videoconvert"):
                    elements.insert(index + 1, {
                        "factory": "capsfilter",
                        "nodeId": current.get("nodeId"),
                        "args": {"caps": JPEGDEC_NATIVE_CAPS},
                    })
                    inserted += 1
                    index += 2
                else:
                    index += 1
        if inserted:
            logger.info("Pinned jpegdec to its native output formats in %d JPEG "
                        "file chain(s) compiled before the fix (arch %s)",
                        inserted, arch)
        return inserted
```

- `_factory(x)` is a module-private helper. It returns `x.get("factory")`
  for a dict and `None` for anything else.
- Call site: in `execute()`, insert two lines immediately before
  `self._ensure_terminal_sink(document)` (line ~1918):
  `self._constrain_jpegdec_output(document, registration.arch)`, then
  `self._feed_segment_first(document, registration.arch)` (C3b).
  - That point comes after every rewrite except the two terminal fix-ups
    (`_ensure_terminal_sink`, `_ensure_writable_inference_branch`), which
    keep their order after it. Camera-binding slot substitution, the
    appsrc rewrite and the bridge rewrite have all run.
  - It comes before rendering and the name map. The map is built from the
    final document, so attribution stays correct.
  - Both calls sit outside the pipeline-run `try` (which starts at ~1983),
    so an exception takes the outer handler (see Error Handling).

### C3b — Executor feed-segment rule (D10, option b)

Same file. Add a `@staticmethod` next to `_constrain_jpegdec_output`:

```python
    @staticmethod
    def _feed_segment_first(document, arch) -> bool:
        """Move the segment headed by the fed appsrc to index 0.

        GstPipelineManager.create_buffer and python_bridge._fed_frame_caps
        read the launch string's FIRST caps= clause as the fed appsrc's
        caps. On the arches in JPEGDEC_NATIVE_CAPS_ARCHS a JPEG file chain
        renders a capsfilter caps= of its own, so a file source rendered
        first would hand the appsrc that capsfilter's quoted caps, which
        Gst.Caps.from_string rejects. A tee branch (a segment with "from")
        never moves; the forward funnel reference the move can create
        (... ! f0. ahead of funnel name=f0) parses on GStreamer 1.16 to
        1.24. The other segments keep their order. Returns True when it
        moved a segment. Never raises on JSON-shaped input."""
        if not isinstance(document, dict) or arch not in JPEGDEC_NATIVE_CAPS_ARCHS:
            return False
        segments = document.get("segments")
        if not isinstance(segments, list):
            return False
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict) or segment.get("from"):
                continue
            elements = segment.get("elements")
            if not isinstance(elements, list) or not elements:
                continue
            head = elements[0]
            args = head.get("args") if isinstance(head, dict) else None
            if (_factory(head) == "appsrc" and isinstance(args, dict)
                    and args.get("name") == "appsrc"):
                if index == 0:
                    return False
                segments.insert(0, segments.pop(index))
                logger.info("Moved the Frame_Feed segment of node %s ahead of "
                            "%d segment(s) so its appsrc caps come first "
                            "(arch %s)", head.get("nodeId"), index, arch)
                return True
        return False
```

- "Fed appsrc" means exactly what `_point_appsrc_at_frame_feed` produces:
  factory `appsrc`, renamed `appsrc`, with its own `caps` arg. The
  single-Frame_Feed contract allows one per run. Bridge appsrcs are named
  `py_out_<node>`, and `rewrite_document` keeps the fed appsrc at
  `elements[0]` of its first part, so bridge parts never match.
- Nothing after it puts a `caps=` ahead of the appsrc.
  `_ensure_terminal_sink` appends at segment ends, and
  `_ensure_writable_inference_branch` edits only `from` segments.
- The name map and `declared_factories` are built from the reordered
  document, so auto-names such as `videoconvert<N>` follow GStreamer's
  creation order.

### C4 — Legacy builder

File: `src/backend/gstreamer/pipeline_builder.py`, in
`_add_file_image_source`, non-JP6 branch. Put this right after the `jpegdec`
`add_plugin` and before `videoconvert`. The imports are lazy, like the
existing `workflow_engine.csi_capture` import in this class:

```python
        # GStreamer 1.20 jpegdec decodes straight to an RGB-family format when
        # the first format downstream offers is one (an RGB request, or an
        # unconstrained videoconvert), and that libjpeg-turbo path fails
        # ("Improper call to JPEG library in state 205"). Pin it to its native
        # formats on the Ubuntu 22.04 targets; videoconvert converts.
        from workflow_engine.environment import device_arch
        from workflow_engine.vendor.workflow_core.catalog.nodes import (
            JPEGDEC_NATIVE_CAPS, JPEGDEC_NATIVE_CAPS_ARCHS)
        if device_arch() in JPEGDEC_NATIVE_CAPS_ARCHS:
            self.pipeline_config.add_plugin(PluginDefinition(
                "capsfilter", [PluginArg("caps", f'"{JPEGDEC_NATIVE_CAPS}"')]))
```

- The rendered token is `capsfilter caps="video/x-raw,format=I420;…;video/x-raw,format=RGB"`,
  the same token the workflow engine renders.
- `device_arch()` returns `x86_64` on both amd64 variants and `arm64_cpu`
  on the generic arm64 image. JP5, JP6 and JP7 are detected from the
  component path.
- `get_input_file_from_pipeline` reads only the first `!` segment, so it is
  unaffected.
- Every caller of `_add_file_image_source` inherits the capsfilter on a
  constrained arch, harmlessly: the image-source capture and preview path
  (`build()` without a workflow appends `jpegenc ! filesink`), and the
  live-view `GStreamerBackend` for `NvidiaCSI` (`utils/streaming/backends.py`),
  which is Jetson-only in practice.
- A Pipeline_Configuration has one image source, so the D10 hazard can't
  arise on the legacy path.

### C5 — Tests

**New, Portal:**
`edge-cv-portal/backend/layers/workflow_core/tests/test_jpegdec_native_caps.py`.
- The constant's shape: five structures in the order I420, Y42B, Y444,
  GRAY8, RGB; no `{` or `}`; `_resolve_value` returns it unchanged. The
  arch tuple is exactly the three constrained ids, and
  `compiler.compiler.JPEGDEC_NATIVE_CAPS_ARCHS is` the catalog's tuple.
- The chain content of each `folder_source` and `csi_camera_source` mapping
  per arch. Compiling `folder_source → capture` yields the capsfilter at
  element index 4, carrying the source `nodeId`.
- Preservation of the unconstrained mappings against a dedicated frozen
  golden, `layers/workflow_core/tests/goldens/jpegdec_native_caps/unconstrained_mappings.json`:
  descriptor → arch → `dataclasses.asdict(mapping)` for `folder_source` and
  `csi_camera_source` on `arm64_jp5`, `arm64_jp6`, `arm64_jp7` and `sim`.
  Captured once from the UNFIXED tree in task 2 with a capture-or-assert
  helper (the sibling `_cmake_preservation_support.capture_or_assert_text`
  pattern), serialized with `json.dumps(..., indent=2, sort_keys=True)`
  plus a newline. Task 3.6 never touches it, so it stays an independent
  oracle; the comparison with `catalog_baseline.json` stays as a second
  guard.
- C1b, compile-side order (P9):
  - mixed graphs, file source first in graph order: {`folder_source`,
    `csi_camera_source`} × {`aravis_camera_source`, `custom_python_source`,
    `rtsp_camera_source`, `rtmp_stream_source`}, each source feeding its own
    `capture`, plus a fan-in variant (both sources into one `capture`, so a
    funnel `f0` follows). For x86_64, x86_64_nvidia and arm64_cpu: the
    feed segment is at index 0; removing it leaves the other segments in
    their original relative order; every segment `name`, every `from` and
    `linkTo` and every tee/funnel `name` arg equals the unreordered
    compile's (built by calling `compile()` with `_feed_segment_first`
    patched to identity);
  - the same graphs for arm64_jp5, arm64_jp6, arm64_jp7, and with
    `simulation=True` for x86_64, equal a second frozen golden,
    `goldens/jpegdec_native_caps/unconstrained_mixed_compiles.json`
    (`CompiledPipelineDocument.to_dict()` per graph and arch), captured
    from the unfixed tree in task 2;
  - a feed-first graph, and a single-source graph, compile identically
    with and without the post-pass;
  - Hypothesis over `compiler._feed_segment_first` itself: generated
    segment lists (appsrc-headed segments of each frame-feed type at a
    random index, file-chain segments, `from` branches, funnel-headed
    segments, and non-feed appsrc heads such as a `digital_input` node's)
    with a matching `nodes_by_id`, across every arch id. Some lists hold a
    second frame-feed segment (unreachable under V7) to pin the
    stop-at-first rule. On a constrained arch, the first frame-feed segment
    ends at index 0, and every other segment keeps its relative order and
    identity; when that segment is already at index 0, or there is none,
    the input list object itself is returned. On any other arch the input
    list object is returned.

**New, Portal function test (container):**
`edge-cv-portal/backend/tests/test_feed_segment_first_packaging.py`. It
compiles an `icam_source → capture` plus `aravis_camera_source → capture`
graph (icam first) with `compile_workflow` for x86_64 and arm64_jp5, then
calls `build_binding_points` on `compiled.to_dict()`. The icam point's slot
must address the `v4l2src` element (`_rendered_slot_value` returns the
node's `device`). That assertion is order-agnostic, so it is a task 2
preservation case and passes before and after the fix. A second case,
added in task 1, also expects the slot at segment 1 on x86_64 (after C1b)
and at segment 0 on arm64_jp5. The file needs the shared layer, so it runs
in `dda-portal-test:py311` (tasks.md Overview).

**New, backend:** `test/backend-test/workflow_engine/test_jpegdec_native_caps.py`.
- Hypothesis properties of `_constrain_jpegdec_output` (P1 to P4) and of
  `_feed_segment_first` (P7), under Correctness Properties.
- A vendored compile plus `rendering.render_launch_string` check of the
  exact rendered substring.
- `execute()` single-source cases: a pre-fix x86_64 document renders the
  capsfilter once, and an `arm64_jp5` document renders byte-identically.
- Identity (`is`) of the executor's imported constants with the vendored
  ones (D5). The compiler copies are guarded by the mirror test (below),
  not by a test in this file.

**Updated, backend guard:** `test/backend-test/workflow_engine/test_vendored_catalog_mirror.py`
(D6). Append `"compiler/compiler.py"` to `MIRRORED_SHARED_MODULE_RELPATHS`
and add one bullet to the comment block above the tuple, giving the reason
in D6. The new parametrized case passes before the fix (the copies are
byte-identical at 48841a0) and after C2, so task 2 adds it on the unfixed
tree. No other line of the file changes.

**New, backend, mixed-flow matrix (D11, P8):**
`test/backend-test/workflow_engine/test_jpegdec_mixed_feed_matrix.py`.
- Fixtures reused by import, never edited: `make_aravis_document`,
  `make_frame`, `FakePipelineManager` and `FakeCameraManager`
  (`test_workflow_aravis_executor.py`); `make_stream_document`,
  `FakeStreamManager` and its `make_frame` (`test_workflow_stream_executor.py`);
  `make_source_document`, `write_handler` and its handler strings
  (`test_workflow_python_source_executor.py`); `make_session_factory` and
  `write_artifact_set` (`workflow_engine_test_utils.py`).
- A local `_seed_run(session_factory, artifact_path, arch)` helper seeds the
  registration with the row's arch. `test_workflow_aravis_executor.seed_run`
  always seeds `DEVICE_ARCH` (x86_64) and is not edited.
- A row builds one document: a file segment for node `f0` (pre-fix:
  `filesrc ! emexifextract ! jpegparse ! jpegdec ! videoconvert ! videoflip
  ! fakesink`; fixed: the same plus the capsfilter after `jpegdec`; JP6: the
  `pngdec` chain, with `pipeline_executor._stage_decoded_png` patched to
  return its argument plus `.dda_decoded.png`), and the feed fixture's
  segment and binding point, in the row's order. Bridged rows put an
  `emlpython` element for node `py1` (pass-through handler written with
  `write_handler`) before the file segment's sink.
- Feeds: Aravis with `make_frame(4, 2, 1)` and `make_frame(4, 2, 3)`
  (`frame_grabber`); stream with `FakeStreamManager(frame=…)` and
  `processing_mode` on_trigger (`stream_ingest_manager`); Python source
  with a real producer subprocess returning a 2x2 RGB frame.
- Runs: non-bridged rows record `FakePipelineManager.run_pipeline`; bridged
  rows pass `bridged_pipeline_runner=` a recording function, as
  `TestBridgedCoexistence` does.
- Assertions per D11 (bug rows in task 1, preservation rows and Jetson
  goldens in task 2). Jetson goldens live under
  `test/backend-test/workflow_engine/goldens/jpegdec_native_caps/mixed/`,
  one launch string per row, captured from the unfixed tree.
- A `csi_camera_source` subset (non-bridged, Aravis 1 byte/pixel, both
  orders and shapes, x86_64 plus the Jetson goldens) patches
  `_prepare_csi_captures` to a no-op, because it writes the CSI host
  service config.

`_fed_frame_caps` is the production lookup and imports without `gi`.
`create_buffer` and `run_bridged_pipeline` need `gi`, so task 4 runs them in
the LocalServer images.

**Updated legacy builder file:** `test/backend-test/gstreamer/test_pipeline_builder.py`.
- New cases patch `workflow_engine.environment.device_arch`: `x86_64` and
  `arm64_cpu` render the capsfilter, `arm64_jp5` renders today's string,
  and the JP6 PNG path is unchanged.
- Each new case (task 1(h) and task 2's legacy goldens) first asserts that
  `inspect.getsourcefile(type(self.pipeline_builder))` equals
  `os.path.join(os.getcwd(), "src", "backend", "gstreamer", "pipeline_builder.py")`,
  with the message "the legacy cases must test the tree's builder". So a
  run that picked up an image's baked builder fails loudly (review 6,
  finding 1). It runs under the same freeze as the rest of tasks 1 and 2.
- The legacy goldens live in
  `test/backend-test/gstreamer/goldens/jpegdec_native_caps/`, one text
  file per case. The helper resolves that directory from
  `os.path.dirname(os.path.abspath(__file__))`, never from the working
  directory (review 7, finding 1).
  - The container mounts the tree read-only, and the file can't run on
    the host (`gi`). So task 2 captures them once, on the unfixed tree,
    with an extra read-write mount of only
    `test/backend-test/gstreamer/goldens/`.
  - The container runs as root, so a second container then `chown`s that
    directory back to the host user, and a `find` checks that no
    root-owned entry is left.
  - Every later run is read-only and only asserts.
- The file imports `gi` through `aravis_functions`, so it runs only inside
  the `flask-app` container. That is the steering command, run by image ID
  with the tree mounted at `/w/repo` (Testing Strategy, "Container
  unit"). The build's gate body never runs this file, so the assertion
  can't fail a component build.

**Updated expectations** (intended change only; nothing is deleted or
loosened):
- `test_catalog_content.py`, `test_mappings_read_the_csi_capture_file`:
  `x86_64`, `x86_64_nvidia` and `arm64_cpu` expect the 7-element chain.
  `arm64_jp5` and `arm64_jp7` (newly asserted) expect today's 6-element
  chain.
- `test_compiler_compile.py`, `test_chain_elements_tagged_with_node_id`:
  `capsfilter` follows `jpegdec`.
- `test_pipeline_builder.py`, `test_pipeline_definition_with_folder_input`:
  pin `device_arch` to `x86_64`, and the expected string gains the
  capsfilter.
- The five recorded-output tests of D7 are resolved only by tasks 3.6 and
  3.7. Any other failing test means stop.
- C3, C3b and C1b change no existing expectation (measured for C1b; for C3
  and C3b by the review's scratch run). The backend test utils'
  `DEVICE_ARCH` is `x86_64`, a constrained arch, so a reordered launch
  string in any other existing test means stop and investigate.

### C6 — Recorded outputs

These follow D7:
- the `catalog_baseline.json` scoped refresh plus the maintenance-log
  paragraph;
- `_write_golden()` for the zero-trigger golden;
- three sha256 entries in `backend_csi_consumers_sha256.json`.

Each one is reviewed by diff, and the expected deltas are as in D7.

### Not changed

- JP5, JP6 and JP7 chains and segment order; the `sim` mappings, simulation
  compiles and the test sandbox.
- `rendering.py`, `discovery.py`, `camera_binding.py`, `workflow_packaging.py`
  and `build-custom.sh`.
- `gst_pipeline.py` (`create_buffer`) and `python_bridge.py`
  (`_fed_frame_caps`, `run_bridged_pipeline`) (D10, option a backlogged).
- `test_workflow_aravis_executor.py` and the other reused fixture modules.
  (`test_vendored_catalog_mirror.py` gains one tuple entry and one comment
  bullet, C5.)
- Dockerfiles, recipes and `requirements.txt`.
- `README.md`, `README_main.md` and every security baseline.

## Error Handling

| Operation | Failure conditions | Recoverable? | Caller receives | Logging |
|---|---|---|---|---|
| Compile with the new element (Portal Lambda, vendored compiler) | None new: the element is static and holds no placeholder | n/a | Same as today | None new |
| C1b `_feed_segment_first` in `compile()` | None on the compiler's own segment dicts. An unexpected exception (a bug) is NOT caught | The packaging request fails; nothing is uploaded, because the compile loop runs before any artifact is written | The packager's top-level handler: HTTP 500 `INTERNAL_ERROR`, "Internal server error" (`workflow_packaging.handler`) | `logger.error("Handler error: …", exc_info=True)` (existing) |
| C3 `_constrain_jpegdec_output` | None for JSON-shaped input; non-dict or non-list parts are skipped. An unexpected exception (a bug) is NOT caught here | The run is lost; the engine continues | The call sits outside the pipeline-run `try`, so the outer handler (`pipeline_executor.py` ~2376-2383) takes it: execution row `failed`, `error` = `Workflow executor failed unexpectedly; see logs` (not the exception text), `failingNodeId` unset, no node statuses. Directory-resolved folder frames stay in place (no `failed/` relocation on this path), so the next run picks the same image | Inserted ≥ 1: one INFO line per run. Inserted 0: nothing. On an exception: `logger.exception("Workflow execution %s failed unexpectedly")` with the traceback, inside the run-log capture window |
| C3b `_feed_segment_first` | None for JSON-shaped input; non-dict parts are skipped. An unexpected exception is NOT caught here | Same as C3 | Same outer-handler result as C3 | Moved: one INFO line per run. Not moved: nothing. On an exception: as C3 |
| Fed appsrc caps on a LocalServer without a feed-first rule (bugfix 1.5). With C1b, no package the fixed Portal compiles reaches it; only a hand-built or third-party package with a file chain carrying a capsfilter ahead of a frame feed could | `Gst.Caps.from_string` returns None for the quoted capsfilter caps, and the frame push aborts the process | Not recoverable in-process: SIGABRT, and Docker restarts the backend (`restart: always`) | A triggered registration aborts the backend on every trigger, and its row stays `running` (startup reconciliation, `run_retention.reconcile_interrupted`, covers only continuous runs). A non-bridged mixed continuous registration aborts the backend at every startup while its stream is up (a crash loop) until the registration is removed; the operator pause persists only if set between restarts | GStreamer CRITICAL, then `Bail out!` on stderr |
| Pre-existing first-`caps=` shapes on JP5/JP6/JP7 (D12) | As measured in D12: contained `PipelineExecutionException`, wrong-size or channel-swapped frames, or SIGABRT for the bridged shape with a 3-byte/pixel Aravis frame | Unchanged by this fix | Unchanged | Unchanged |
| Legacy builder capsfilter | `device_arch()` cannot fail. A lazy-import failure is impossible in a shipped image, because the vendored package ships with the backend; if it did happen, it propagates | Fatal for that pipeline build | The existing pipeline-build error path | Existing ERROR |
| GStreamer negotiation at run time | `not-negotiated`, only if a future `jpegdec` emits a native format outside the five. Impossible on 1.16 to 1.24 | Yes: extend the constant | Bus ERROR attributed to the source node through the name map. The run fails. Folder frames move to `failed/` (existing legacy and engine logic) | ERROR from `GstPipelineManager` (existing) |
| Undecodable JPEGs (CMYK/YCCK, 12-bit, arithmetic-coded, corrupt) | Unchanged | Unchanged | Unchanged | Unchanged |
| C2 copies, baseline refresh, golden regeneration, sha256 rebaseline | A failed copy; a type-id set mismatch; a delta outside the expected descriptors or documents | Implementer stops; nothing is committed | n/a | Recorded in verification-notes.md |
| Offline matrix (tasks 1 and 4) | (a) A fixed row fails for any image, version or JPEG kind: the design is refuted. (b) An R2 image (JP5, JP6, arm64 CPU, JP7) can't be resolved: its label matches no local image or more than one image ID, or the image reports another OS or GStreamer than its row. (c) A container or network error. (d) A stock image of 4.1 isn't local | (a) Refuted: stop and re-design, no tweak-and-retry. (b) STOP and ask the owner; the R2 rows are never recorded as "not run", because R2 is a hard owner gate. (c) Retry once. If an R2 row still can't run, STOP and ask the owner; any other row is recorded as not run, and its acceptance criterion stays open. (d) Never pulled: a native stock row is recorded as not run and the owner is asked (AC 1 stays open); the emulated row is recorded as not run | n/a | verification-notes.md, with each image's label and image ID only |
| Gate-body run (tasks 2 and 5: the derived copy, against a writable copy of the tree) | (a) A command fails in the first block or in the stream camera components gate. Both are expected in the JP5 image, for image-content reasons: the first block runs verbatim at `/repo`, so its tests import a mix of the tree's `utils` and the image's baked `app`, `gstreamer` and `model` (review 6, finding 1), and no local image has `/stream_ingest` or PyAV. (b) A command fails inside one of the six audit gates. (c) The pip install (derived line 3) fails. (d) A part-1 check prints a `STOP` line: `$G` already exists, the derived copy's line 2 isn't `set +e` or `bash -n` fails, or the tree copy's file count differs from `git ls-files` | (a) Recorded as pre-existing when task 2's run shows the same trap lines and `FAILED`/`ERROR` ids. A trap line or id that task 2 didn't show: stop and investigate before task 6; nothing is pushed. (b) Never expected, in either run: stop. In task 2 it is a STOP for the owner, not a baseline. (c) Infrastructure: fix the network and re-run; that log is not judged. (d) The container doesn't run. Clean up with part 3 (for an existing `$G`, only once no gate container is running), then start again at part 1a | n/a | The recorded grep goes to verification-notes "Task 2" and "Task 5": the `Running …` lines, the `GATE-CMD-FAILED rc=… line=…` trap lines, pytest's `FAILED`/`ERROR` ids and one summary line per pytest call, plus the three part-1 verdict lines |
| Python code run in a LocalServer image (tasks 1, 2, 3.5, 4, 5 and 7: the legacy builder tests and every row) | (a) The run loaded a module from the wrong place. Under a `/repo` mount, pytest's package setup for the repo root's `__init__.py` puts `/` first on `sys.path`. Separately, Python puts the working directory first for `python3 -c`, `-m` or stdin runs, and the image's working directory is `/`. 1(h)'s `inspect.getsourcefile` assertion fails, or a row's provenance line doesn't start with the prefix task 4's Provenance rule requires for it: `/w/repo/src/backend/`, the second mount's prefix for unfixed-tree code, or `/workflow_engine/` and `/gstreamer/` for own-code rows (review 7, NIT 5). (b) The row didn't run with task 4's row command: it exits 133, or its provenance lines are missing from the log, because Python ran as the container's PID 1 or with buffered output (review 7, finding 3). Under emulation, an abort in such a row leaves the container running | Yes. It is a harness error, not a result: fix the mount, the working directory or the command and re-run. A container left running is removed with `docker rm -f` (its ID from `docker ps`). That run's outcome is never judged, and a golden is never captured from it | The failing assertion, with the baked path in its message, or the row's provenance line and exit code | Recorded in verification-notes next to the re-run |
| Built-image offline check (task 7) | (a) After the load and the restore, the label `flask-app:latest` doesn't resolve to the JP5 image ID recorded before the load. (b) The loaded image reports another OS or GStreamer than its variant. (c) A fixed row fails in it | (a) and (b): STOP and ask the owner. (c) The build doesn't go to the Dell; stop and investigate | n/a | verification-notes, with the label `flask-app:<component version>` and the image ID only |

No new code swallows its own exceptions. Swallowing a normalizer bug would
hide it behind the misleading `jpegdec` error this fix removes, swallowing
a reorder bug would bring back the process abort of 1.5, and swallowing a
compile bug would ship a package in the wrong order. Property tests (P4,
P7, P9) prove the three functions total over their inputs. No test expects
the exception text, because the outer handler records the fixed string
above.

## Validation Rules

No user-facing input changes: no new API parameter, node parameter or
config key. The inputs the new code reads:

| Input | Required | Type and limits | Rule | On failure |
|---|---|---|---|---|
| Compiler segments (C1b) | Yes | The list `_build_segments` returns: dicts with `name`, `from`, `linkTo`, `elements`; elements carry `nodeId` (None for synthetic) and `factory` | Find the first segment whose `from` is None, whose head is `appsrc`, and whose head node's type is in `FRAME_FEED_SOURCE_TYPES`. At index 0: return the input list. Otherwise: move that segment to index 0 | No match: the input list is returned unchanged |
| `target_arch`, `simulation` (C1b) | Yes | str; bool | Runs only when `simulation` is False and the arch is in `JPEGDEC_NATIVE_CAPS_ARCHS` | Otherwise a no-op |
| Per-run compiled document (C3, C3b) | Yes; discovery already parsed and validated it | JSON object | `segments` is an optional list. Each segment is a dict with an optional `elements` list. Each element is a dict with a `factory` string. Only `factory`, `args.name` and `from` are read, and `nodeId` is copied | Non-conforming parts are skipped, never repaired, and never raise |
| `registration.arch` | Yes | str: the manifest `targetArch`, which discovery checks against `device_arch` | Membership in `JPEGDEC_NATIVE_CAPS_ARCHS` | Any other value (`"unknown"`, `None`): no-op |
| The fed appsrc (C3b) | Optional: present only on Frame_Feed runs | `elements[0]` of a segment with no `from`: a dict with `factory == "appsrc"` and an `args` dict whose `name == "appsrc"` (set by `_point_appsrc_at_frame_feed`) | The first match wins; the single-Frame_Feed contract allows one | No match, or already at index 0: no-op, returns False |
| `device_arch()` (legacy) | n/a | str | Membership in the same tuple | Other values: today's unconstrained chain |
| `JPEGDEC_NATIVE_CAPS` | Compile-time constant | str | Five structures with I420 first, no `{` or `}`, and containing `;` so it always renders quoted | A unit test fails the suite |
| JPEG file bytes | Run time | Any | `jpegdec`'s own checks | Existing decode-failure paths |

## Invariant Ownership

| Invariant | Enforcing layer | Why there | Guard |
|---|---|---|---|
| New packages for constrained arches carry the capsfilter | Portal catalog `_jpeg_file_chain` | The Portal compiles every package | Catalog tests, baseline, zero-trigger golden |
| New packages for constrained arches render the frame-feed segment first | Portal compiler `_feed_segment_first` (C1b) | Only the compile step can order a package before its `bindingPoints` exist, and only the package reaches older LocalServers | P9 tests; task 4's older-LocalServer runs; Dell 3c F packages |
| Pre-fix packages run constrained on 22.04 devices | Device executor `_constrain_jpegdec_output` | Only the device sees old packages at run time | Backend unit and property tests; Dell check 3a |
| On constrained arches, the fed appsrc's `caps=` is the launch string's first `caps=` | C1b for new packages; C3b for pre-fix ones | Both consumers read the first `caps=`; the device alone knows which appsrc a run feeds | P7, P8, the mixed matrix, task 4, Dell 3c |
| Legacy file chains are constrained | `GstPipelineBuilder` | It builds the string at run time | Legacy builder tests (container, tree at `/w/repo`; each case asserts it tests the tree's builder) |
| The Portal and vendored `nodes.py` and `compiler.py` are byte-identical | C2 per-file copies | The vendored tree is generated, never authored | `test_vendored_catalog_mirror.py` (`nodes.py` through `MIRRORED_FILENAMES`, `compiler/compiler.py` through the new `MIRRORED_SHARED_MODULE_RELPATHS` entry); README `diff -r` |
| One caps string and one arch set | Constants in `nodes.py` | Four layers consume them | Identity tests |
| JP5, JP6, JP7 and `sim` are unchanged | The arch tuple (a single gate) plus C1b's `simulation` check | Scope is decided in one place | Baseline, frozen goldens, Jetson mixed goldens, legacy JP5 test, P3, task 8b |
| I420 stays first | The constant | `jpegdec` decides from the first structure | Order unit test; offline matrix |
| C1 never ships without C1b, and C3 never without C3b | The single fix commit | Either half alone creates the abort for some package on some LocalServer | Task 1's compile and matrix bug rows; task 2's post-C3 preservation rows; task 6 commits all four together |
| No package the fixed Portal compiles aborts an older LocalServer | C1b, which lands in every layer the trunk can publish | Older LocalServers can't be changed after the fact | Task 4's older-LocalServer runs; task 10's recorded inventory |

## Edge Cases

- **JPEG kinds.**
  - RGB-colourspace JPEGs pass through as native RGB.
  - Grayscale JPEGs stay GRAY8, and `videoconvert` expands them to RGB.
  - 4:2:2, 4:4:4, 4:4:0, 4:1:1, progressive and odd-size JPEGs come out as
    I420, as on 1.16.3.
  - CMYK/YCCK, 12-bit and arithmetic-coded JPEGs fail exactly as before.
- **EXIF orientation is preserved.** Tag events pass through `capsfilter`,
  and an orientation-6 JPEG still ends at 480x640.
- **Tee'd chains** (a bedrock capture branch into `jpegenc`) already picked
  I420, and they are unchanged in effect.
- **Documents that already carry the capsfilter** (compiled after the Portal
  deploy) no longer match the sequence, so C3 is a no-op, and their feed
  segment is already first, so C3b is a no-op too. Two JPEG sources in one
  document get one capsfilter each.
- **`unified_input`** expands through the `folder_source` mapping (or a
  stream or camera mapping) before compilation, so C1 and C1b treat it as
  the expanded type.
- **Auto-names.** The inserted capsfilter shifts later `capsfilter<N>`
  auto-names by one, and either reorder shifts `videoconvert<N>`-style
  auto-names. The name map is built after both rewrites, and no code
  references auto-names. `_ensure_writable_inference_branch` scans only
  tee-branch segments, and a source chain is never in one.
- **x86_64 images built on 20.04** decode with the same formats as today.
- **amd64 NVIDIA devices** report `device_arch() == "x86_64"`, and discovery
  requires `targetArch == device_arch`, so only `x86_64` packages register
  there (pre-existing). Both ids are constrained, so the outcome doesn't
  depend on which one a package names.
- **Mixed documents, every combination (D10, D11).** A JPEG file source plus
  a frame-feed source:

| Package | LocalServer | Result |
|---|---|---|
| Pre-fix, file-first | Fixed, constrained arch | C3 inserts the capsfilter, C3b moves the feed segment first: fed caps first |
| Pre-fix, feed-first | Fixed, constrained arch | C3 inserts; C3b no-op |
| Fixed (feed-first by C1b) | Fixed, constrained arch | C3 and C3b no-ops |
| Fixed (feed-first by C1b) | Older, x86 or arm64 CPU | Fed caps first (measured with the images' own code) |
| Pre-fix, any order | Older | Today's behavior: no capsfilter, the original 1.20.3 decode bug where it applies |
| Any | JP5, JP6, JP7 | Unchanged byte for byte; D12's pre-existing shapes stay as they are |

- **Bridged mixed documents.** On constrained arches the fed segment is
  first, and `rewrite_document` keeps the fed appsrc at `elements[0]` of its
  first part, so the first `caps=` is the fed appsrc's own, with or without a
  bridge on either branch. Stream and Python-source frames use their own
  `format` in bridged runs anyway.
- **Two frame-feed sources in one document** are rejected by the validator
  (V7) and again before the pipeline is built ("2 frame-feed source binding
  points"), so neither rule has to choose between them.
- **Feed-first plus slot-bearing nodes.** An `icam_source` (or any node
  with `bindingPoints` slots) in a mixed document moves down by one
  segment. Its slot is derived after C1b, and the device substitutes slots
  before C3b runs, so both stay correct.
- **Continuous stream registrations** run C3 and C3b on every run: the
  INFO lines appear once per run, inside the `continuous_run` logging
  context that keeps INFO out of the component log.
- **Dev runs on a Jetson** without `LOCAL_SERVER_COMPONENT_DECOMPRESSED_PATH`
  report `arm64_cpu`, so the legacy chain is constrained there too. That is
  harmless on every version.
- **Cost:** one capsfilter, and the RGB conversion moves from libjpeg-turbo
  to `videoconvert`. That is negligible for single-frame triggered runs. The
  reorders are O(segments) list operations.

## Correctness Properties

- **P1, Fix.** For a constrained arch, every catalog sequence becomes
  `jpegparse, jpegdec, capsfilter{caps=JPEGDEC_NATIVE_CAPS, nodeId=jpegdec's},
  videoconvert`. The return value equals the number of sequences.
- **P2, Idempotence.** A second call changes nothing and returns 0.
- **P3, Preservation.** For an arch outside the set, or a document with no
  sequence, the document deep-equals its pre-call copy and the call returns
  0. Removing the inserted elements always yields the original document.
- **P4, Totality.** For any JSON-shaped `document` (nested dicts, lists,
  strings, numbers, booleans, `None`) and any `arch`, the call never raises.
  Explicit examples: `{"segments": true}`, `{"segments": 1.5}`,
  `{"segments": 5}`, `{"segments": "s"}`, and segments or elements that are
  numbers, strings or `None`. (A design-phase fuzz of the C3 and C3b code
  over 20,007 such documents and 7 arch values raised nothing; the review
  repeated it over 20,000 documents and 9 arch values.)
- **P5, Catalog.** For every arch in `ARCHITECTURES`, each `folder_source`
  and `csi_camera_source` chain is today's chain plus the capsfilter at
  index 4 if and only if the arch is constrained. JP6 keeps the PNG chain,
  and `sim` keeps the dataset stub. The unconstrained mappings equal the
  frozen task 2 golden `unconstrained_mappings.json`, which task 3.6 never
  regenerates.
- **P6, Runtime (offline).** For each of GStreamer 1.16.3, 1.20.3 and
  1.24.2 and each of the 12 JPEG kinds, the fixed chain exits 0 with the
  RGB tail (X) and with the unconstrained `videoconvert ! fakesink` tail
  (X0), and `jpegdec`'s src format is I420, GRAY8 or RGB, as the kind
  dictates.
- **P7, Feed-first (device).** For a constrained arch and a document whose
  fed appsrc heads segment i (a segment with no `from`): after the call it
  heads segment 0, every other segment keeps its relative order, the
  segments are the same objects, and the call returns `i > 0`. A second
  call returns False and changes nothing. For any other arch, or a document
  with no fed appsrc, the document deep-equals its pre-call copy and the
  call returns False. It never raises on JSON-shaped input (as P4).
- **P8, Fed caps, the owner's gate (3.10, 3.11).** For every matrix row of
  D11 on a constrained arch, the launch string `execute()` hands the runner
  starts with the fed appsrc, its first `caps=` clause is one
  whitespace-free token equal to `WorkflowExecutor._frame_caps(frame)`, and
  `python_bridge._fed_frame_caps(launch, frame)` equals `_frame_caps(frame)`
  plus `,width=W,height=H`. For every row on arm64_jp5, arm64_jp6 and
  arm64_jp7, the launch string equals the unfixed tree's byte for byte.
  Offline (task 4), every row reaches EOS with one buffer at each branch's
  sink and exits 0 in the JP5, JP6, arm64 CPU and JP7 images. The one
  exception is D12's pre-existing bridge-appsink shape on JP5, JP6 and JP7
  (bridged, file first, Aravis frame): it fails with 48841a0's code today,
  so the fixed code must give it the same launch string and the same
  outcome, it is never counted as a pass, and it goes to task 12.
- **P9, Feed-first (compile).** For a constrained arch with
  `simulation=False`, `compile()` of a mixed graph puts the frame-feed
  segment at index 0, and its output equals the unreordered compile's
  segments with that one segment moved to the front. Names, `from`,
  `linkTo` and element args are unchanged. For any other arch, or with
  `simulation=True`, the output equals the unfixed tree's. The packager's
  `bindingPoints` slots address the same elements as before the reorder.
- **P10, Older LocalServers (3.12).** A package compiled by the fixed tree
  for a constrained arch, rendered and fed by an older LocalServer's own
  executor code, hands the fed appsrc `_frame_caps(frame)` plus the
  dimensions and reaches EOS. Measured in design iteration 3 in the arm64
  CPU and JP5 images; task 4 repeats it in the arm64 CPU and JP5 images.
  The local amd64 image predates the workflow engine (no
  `/workflow_engine`), so it can't run a workflow package and is out of
  scope (review 7, finding 2). Both images' frame-feed consumers
  (`create_buffer`, `_point_appsrc_at_frame_feed`, `_frame_caps`,
  `_fed_frame_caps`, `render_launch_string`) are byte-identical to
  48841a0's, so P10 covers LocalServers built from that code. Older builds
  can carry older consumers (the JP6 1.0.46 image does), but each
  generation checked reads the launch string's first `caps=` and gives the
  renamed appsrc its own caps, which is all C1b relies on. 1.0.50's own
  code isn't local and isn't checked.

## Testing Strategy

- **Unit, no GStreamer (host pytest):**
  - the constants, catalog chains (P5), the compiled document, and the
    vendored compile plus render;
  - C1b (P9): mixed compiles per arch, the frozen unconstrained-mixed
    golden, and the Hypothesis property of `_feed_segment_first`;
  - C3 and C3b (P1 to P4, P7) with Hypothesis;
  - the D11 mixed-flow matrix through `execute()` (P8): bug rows (task 1),
    preservation rows and Jetson goldens (task 2).
- **Portal function tests, in the `dda-portal-test:py311` container:** the
  new slot test, plus the existing packaging and binding-point files that
  compile frame-feed documents (tasks.md Overview).
- **Container unit:** the legacy builder tests in the JP5 image (label
  `flask-app:latest`, run by the image ID the Image rule resolves, never by
  name), which has `gi`, with the tree mounted at `/w/repo` (review 6,
  finding 1).
  - Why not `/repo`. The repo root holds a tracked `__init__.py`. So
    pytest collects the mount as a package, and while the first test sets
    up, `Package.setup` puts its parent directory first on `sys.path`.
    For `/repo` that is `/`, where every LocalServer image bakes its
    backend. From then on, any backend package not yet imported resolves
    to the image's copy.
  - `test_pipeline_builder.py` imports `GstPipelineBuilder` in `setUp`.
    So, run on its own under `/repo`, it tests the image's
    `/gstreamer/pipeline_builder.py`. After C4 lands, C4's own cases
    would fail there although C4 is correct. Under `/w/repo` the inserted
    directory is `/w`, which holds only the mount.
  - Each new legacy case asserts `inspect.getsourcefile(type(self.pipeline_builder))`
    against the tree's path, so a run against a baked copy fails loudly.
  - A related effect has a different cause. Python itself puts the
    working directory first on `sys.path` for `python3 -c`, `-m` and
    stdin runs. So from the image's default working directory `/`, those
    runs import baked modules ahead of `PYTHONPATH`. Task 4's Provenance
    rule covers that (design iteration 7).
  - These runs mount the tree read-only. The one exception is task 2's
    legacy golden capture, which adds a read-write mount of only the
    legacy goldens directory and then hands it back to the host user (C5;
    review 7, finding 1).

  In the same image, against a writable throwaway
  copy of the tree under test, the full security preservation suite and
  the rest of `build-custom.sh`'s in-image gate body (task 5), so a
  component build can't fail at its gate on something the local runs
  skipped.
  - The body runs as a derived copy: the verbatim extraction with line 1's
    `set -e` turned into `set +e`, plus an ERR trap that logs each failing
    command (review 4, finding 1). In the JP5 image the verbatim body stops
    at its first block's pytest (body line 8), so the stream gate, the
    preservation suite and the six audit gates would never run. Iteration 5
    said it stopped at the stream camera components gate, which comes later
    (review 5, finding 1).
  - It runs against a copy of the tree, never the worktree (review 5,
    finding 1, which corrects review 3's "repo mounted read-only"). The
    backend conftest's session fixture copies files into
    `test/backend-test/utils/tmp/`, so under a read-only mount every test
    of every call that loads the conftest errors at setup. Review 5 counted
    162, including all 99 exploration and negative-fixture tests of the six
    audit gates. A read-write mount of the worktree would let
    `LocalServerBaseTestCase.setUp` rewrite a tracked JSON file and leave
    root-owned caches. The copy holds the tracked and untracked-not-ignored
    files (`git ls-files … | tar`) and is mounted read-write, as the build
    mounts its own checkout. A container removes the copy afterwards.
  - Two blocks fail in the JP5 image for image-content reasons this fix
    doesn't touch.
    - The first block: 57 failed, 6 passed at 48841a0. The body runs
      verbatim at `/repo`, as the build runs it, so its five test files
      import a mix of modules by the mechanism above: the tree's `utils`,
      and the image's baked `app`, `gstreamer` and `model`.
    - The stream camera components gate: no `/stream_ingest`, no PyAV.

    The build runs both in the freshly built image, which bakes the
    commit it tests. Task 2 records their ids, and the six audit gates
    must pass in both runs (Error Handling row). The audit gates' suites
    import no backend module lazily, so the `sys.path` switch doesn't
    affect them (review 6, V6).
  - The body has no `||`, `&&` or `exit` outside comments, so every command
    runs. The first block has no `Running …` echo, so it spans everything
    before the first `Running …` line (found with a stubbed run in design
    iteration 5). The recorded grep keeps pytest's summary line for each of
    the body's 9 pytest calls (review 5, finding 2).
- **Offline integration, in throwaway `--rm` containers (task 4):**
  - Image names are redacted labels (repository basename and tag, without
    the registry prefix some local copies carry). Each one is resolved to
    exactly one local image ID, run by that ID with `--pull never`, and
    recorded as label plus image ID. A missing, ambiguous or mismatched R2
    image is a STOP for the owner (tasks.md task 4, Image rule; Error
    Handling).
  - Rows that run the tree's Python code in a LocalServer image follow
    tasks.md task 4's Provenance rule (design iteration 7). The tree is
    mounted at `/w/repo`, each row's script runs by file path from
    `/work`, and each row prints where its executor, bridge and pipeline
    modules came from. A row that loaded a baked module is a harness
    error, and its outcome is never judged.
    - Measured in the JP5 image with `PYTHONPATH=/repo/src/backend`:
      `python3.11 -c` from the default working directory `/` imported
      `/workflow_engine/pipeline_executor.py`, the image's copy.
    - A script run by file path, or `-c` with `-w /work`, imported the
      tree's.
    - Rows that are meant to run the image's own code (task 4.5, task 7)
      check for the baked paths instead.
    - Every row, own-code rows included, runs with task 4's row command:
      `--init` and `-e PYTHONUNBUFFERED=1`, with the provenance lines
      printed before the pipeline is built (review 7, finding 3). With
      Python as the container's PID 1, an abort exited 133 and lost the
      provenance lines, and under amd64 emulation the container hung.
      With the row command it exits 134 and keeps them, natively and
      emulated. Design iteration 8 re-measured all of this except the
      emulated hang.
    - An expected abort (task 1's bridged counterexample, 4.4's
      counterexample, 4.5's file-first rows) is exit 134. Exit 133 or a
      missing provenance line is a harness error.
  - Stock `ubuntu:20.04`, `ubuntu:22.04` and `ubuntu:24.04` with
    `gstreamer1.0-tools`, `-plugins-base`, `-plugins-good` and
    `libjpeg-turbo-progs`: the 12-kind matrix (P6), plus the U0/X0 pair
    with the unconstrained `videoconvert ! fakesink` tail. The native rows
    run by name with `--pull never`. The emulated amd64 22.04 row runs by
    the local amd64 image's ID: the plain name `ubuntu:22.04` is the arm64
    image there (review 4, finding 2). No stock tag is ever pulled or
    retagged.
  - The LocalServer images with the real `emexifextract`: JP5
    `flask-app:latest` (1.16.3), arm64 CPU `flask-app:arm64-1.1.0`
    (1.20.3), JP7 `flask-app:arm64JP7-1.0.45` (1.24.2), amd64
    `flask-app-amd64:extracted` (1.16.3, emulated) where present. The
    amd64 image serves this `gst-launch-1.0` check only: it predates the
    workflow engine.
  - The rendered check 3 string in the arm64 CPU image.
  - The D11 matrix in the JP5, JP6 (`flask-app:1.0.46`, Ubuntu 22.04.3 /
    GStreamer 1.20.3, python3.10), arm64 CPU and JP7 images, with the fixed
    tree's rewrites and the production consumers (P8).
  - Older LocalServers (P10): fixed-tree packages through the arm64 CPU
    and JP5 images' own code at `PYTHONPATH=/`, plus the file-first
    counterexample. The local amd64 image is out of scope there, because
    it has no `/workflow_engine` (review 7, finding 2).
  - The D12 outcome table, extended with a `jpegenc` consumer for the I420
    row.
  - Built images (task 7): the amd64, amd64Nvidia, arm64 and arm64JP7
    verification builds are checked offline on the implementation host
    (review 4, finding 3).
    - The image is loaded from the component artifact's `flask-app.tar`,
      which carries the tag `flask-app:latest`. So every local tag the tar
      applies is recorded first and restored right after the load, and the
      JP5 label must resolve to its own image again before anything runs.
    - The loaded image is recorded as `flask-app:<component version>` plus
      its image ID, and run by that ID.
    - The artifact comes from the published component version: its URI is
      read from the version's recipe (`get-component`) and the zip is
      copied with `aws s3 cp`, both reads. Only the component name and
      version are recorded, never the URI or the ARN (review 5, finding 3).
  - Practicalities: snap Docker can't see host `/tmp`, so keep scripts under
    `$HOME`. `gst-launch-1.0` escapes spaces inside one argv token, so pass
    the pipeline as separate tokens. The images' code imports need
    `COMPONENT_WORK_PATH` set. The JP6 image's `gi` is for python3.10 only.
    Each Docker CLI call took about 25 s on the implementation host in
    design iteration 5, so allow generous timeouts.
- **On-device (owner-gated, tasks 7 to 11):**
  - Dell check 3a: the run-2 pre-fix package, re-uploaded unchanged (proves
    C3).
  - Dell check 3b: a package compiled from the pushed branch (proves C1).
    Both pass by one numeric rule on `check3.py`'s inversion proof: output
    size equals the EXIF-oriented source size; Pearson r(output,
    255 − source) ≥ 0.99 and r(output, source) ≤ −0.99; mean
    |output − (255 − source)| ≤ 3. Exact equality can't hold through a JPEG
    decode, a re-encode and I420 chroma: thor1 measured r = 0.9996 and a
    mean of 1.12.
  - Dell check 3c, the mixed matrix (R3): `custom_python_source`, an RTSP
    stream from a throwaway local MediaMTX (`1.9.3-ffmpeg`, run by its
    recorded digest; no fallback tag without the owner; RTSP, RTP and RTCP
    on 127.0.0.1 and the publisher on `-rtsp_transport tcp`; checked with
    `ss` on each socket's local address, requiring exactly the three
    listeners, each once, and nothing else, review 6, finding 3 and
    review 7, NIT 4), on_trigger and
    continuous, and the Aravis Fake camera or static image camera
    (non-bridged and bridged). Each comes from a P package (unfixed tree:
    file-first, no capsfilter; proves C3 plus C3b) and an F package (fixed
    tree: feed-first plus capsfilter; proves C1 plus C1b).
  - A temporary legacy Folder-source model workflow on a scratch image
    source with a copied image, run once and deleted. Owner workflows,
    owner image sources and `/aws_dda/cookies` are never edited or moved.
  - A 30-minute soak that includes every 3c workflow.
  - thor1 (task 8b, R4): a verification build of the branch, check 3
    launch-string equality with 1.0.56 under bugfix 2.11's normalization,
    one mixed flow, a soak, then restore.

## Rollout and Deployment

1. Push the feature branch (task 6). No merge to `remediation`,
   `integration/all-specs` or `main`.
2. The owner rebuilds amd64 on the dedicated 22.04 server and amd64Nvidia,
   one build at a time per steering. arm64 CPU is rebuilt when it next
   ships. For R4, the owner also runs one arm64JP7 verification build of the
   branch for thor1 (task 8b). It is not a release.
3. Dell verification follows (task 8). amd64 is cleared for station rollout
   only after it passes. The Dell is getting two V100 GPUs. Task 8 runs with
   it still deployed as an x86_64 CPU station; switching it to amd64Nvidia
   afterwards, for an on-device task 9, is a pending owner decision.
4. thor1 non-regression follows (task 8b), before the trunk merge.
5. A later, separate run merges into the trunk after hardware verification
   (task 11). From then on, any ComputeStack deploy from the trunk publishes
   C1 and C1b together, which is safe for older LocalServers (P10).
6. Then the Portal ComputeStack deploy republishes the WorkflowCoreLayer
   (task 10). It can ride with security-scan-remediation-high's owner-gated
   section 8, because no station precondition applies (R5 is met by C1b).
   Task 10 first records the station inventory. Until the deploy, C3 and
   C3b cover packages compiled by the old Portal.
7. Repackaging is optional.
8. The Q1 follow-up for JP5, JP6 and JP7 is an owner decision (task 12).

## Rollback

- **Code:** revert the single fix commit. The code, tests and recorded
  outputs revert together, so every suite stays consistent.
- **Devices:** redeploy the previous LocalServer. The Dell's known-good
  build is 1.0.50. Packages compiled by the fixed Portal stay safe there:
  their mixed documents are feed-first (P10), and their single-source
  documents carry a capsfilter that is harmless on 1.16 and 1.24 and fixes
  1.20.
- **Portal:** redeploy the previous layer. New packages are then pre-fix
  again, and C3 and C3b cover them on fixed LocalServers.
- **The one unsafe combination.** A document whose file chain carries the
  capsfilter but whose feed segment is not first, on a LocalServer without
  C3b. A triggered registration of that shape aborts the backend on every
  trigger, and a non-bridged mixed continuous one aborts it at every
  startup (a crash loop) until the registration is removed. Only C1 without
  C1b, or a hand-built package, can produce it. The single commit rules out
  the first.

## Responses to the Design Reviews

**Iteration 1 (CHANGES_REQUESTED, 1 HIGH, 6 MEDIUM, 8 NIT).** All 15
findings were addressed in iteration 2; review 2 confirmed it (V14). In
short: the mixed-document abort (D10, C3b), totality guards and fuzzing
(P4), the outer-handler outcome (Error Handling), the frozen unconstrained
golden (C5), the `--repo` harness for check 3b, the numeric check 3 rule,
the pristine-worktree baselines, the extra legacy callers, the README prose
golden, the D5 identity tests, the call-site order, the NVIDIA arch
mechanism, the check 3a re-upload, the temporary legacy workflow and the
build-source confirmation.

**Iteration 2 (CHANGES_REQUESTED, 2 HIGH, 6 MEDIUM, 3 NIT), answered in
iteration 3.** Each finding was checked against the code and, where it
could be, measured in scratch exports and the local LocalServer images
(verification-notes, "Design iteration 3"). All 11 are addressed. None is
backlogged or ignored.

| # | Severity | Response |
|---|---|---|
| 1 | HIGH | Addressed. bugfix.md quotes the owner and lists R1 to R5 and Q1, adds 3.11 (worded as the review proposed) and AC 11 to 15, and puts the 3c matrix and 8b in the completion criterion. D11 maps every R1 dimension onto the unit matrix (`test_jpegdec_mixed_feed_matrix.py`: four feeds including a 3-byte/pixel Aravis frame, both orders, both shapes, non-bridged and bridged via a recording `bridged_pipeline_runner`, all six arches, a `csi_camera_source` subset), the offline matrix (JP5, JP6 `flask-app:1.0.46` confirmed as Ubuntu 22.04.3 / 1.20.3, arm64 CPU, JP7; `create_buffer` and `run_bridged_pipeline`; pad-probe buffer counts; pre-fix documents through C3), and the Dell. Jetson rows are frozen as unfixed-tree goldens. |
| 2 | HIGH | Addressed with option A. C1b puts the frame-feed segment first inside `compile()`, in both compiler copies, on constrained arches and only outside simulation, before `bindingPoints` exist. Measured: no workflow_core or Portal packaging test changes (1022/1/1 unchanged; 250 passed); mixed x86_64 compiles become feed-first and Jetson compiles stay byte-identical; fixed-tree packages run to EOS through the arm64 CPU and JP5 images' own older executor code for Aravis, stream and Python-source frames, and the file-first counterexample aborts. C3b stays for pre-fix packages. Error Handling and Rollback now state the crash loop for continuous registrations. |
| 3 | MEDIUM | Addressed. The "owner accepts in writing" path is removed everywhere (D11 notes it is unavailable under R5). Task 10 gains the station inventory procedure (list-core-devices per Portal account and region, the installed `aws.edgeml.dda.LocalServer.*` version per device, a cross-check against the latest effective deployment, offline or unknown counted, counts only in committed files). With C1b it is recorded; if C1b were absent or failed, R5's station rule would block. |
| 4 | MEDIUM | Addressed. Task 8 check 3c becomes a matrix: Python source, an RTSP stream from a throwaway localhost MediaMTX (on_trigger and continuous), and the Aravis Fake camera or static image camera (non-bridged and bridged with a logging pass-through handler), each from a P and an F package. The proof of both frames is the review's three-part rule. Every 3c workflow joins the 30-minute soak. |
| 5 | MEDIUM | Addressed. Owner task 8b (thor1, an arm64JP7 build of the branch) with the review's checks, before task 11; MIC-730 as the alternative. D3 keeps "no Jetson release rebuild" with the gated-code justification. |
| 6 | MEDIUM | Addressed. D12 gives the reachability evidence (all four caps sources, three compiled for the Jetson arches), a measured outcome table on JP5, JP6 and JP7, and a recommendation. One correction to the review: the bridged shape with a 3-byte/pixel Aravis frame aborts the process on all three Jetson images. The recommendation stays "keep Jetsons out of this fix" (owner decision, task 12), but asks for a prompt follow-up that widens C1b and C3b to the Jetson arches. Option (a) is not implemented. |
| 7 | MEDIUM | Addressed. The backend command gains `--continue-on-collection-errors`, the `test_mqtt_publish_retain.py` collection error (`awsiot`) is recorded as pre-existing, and `static_image_camera/`, `stream_ingest/`, `output_bindings_fixes/`, `api-endpoints/`, `utils/` and `resources/` join tasks 2 and 5. The review's scratch counts are quoted as a non-authoritative reference. |
| 8 | MEDIUM | Addressed. Task 3.5 lists the five recorded-output tests as expected failures until 3.6 and 3.7, resolved only by the scoped refresh and `_write_golden()`. The full-suite run moved to 3.9, after 3.8. D7 names `test_catalog_stream_and_analytics.py` as the baseline's second consumer. All eight failures were re-measured with C1, C2 and C1b applied. |
| 9 | NIT | Addressed. The matrix file uses a local `_seed_run(…, arch)`; `test_workflow_aravis_executor.py` is not edited. |
| 10 | NIT | Addressed. bugfix's scan and D10 say `_fed_frame_caps` reads the launch string only for frames without `format`, so in bridged runs only Aravis grabs are exposed. D12 adds the run-on clause the review flagged as U1. |
| 11 | NIT | Addressed. Task 8 check 3a bumps only the recipe's `ComponentVersion` (for example 1.0.1) if Greengrass refuses to re-create 1.0.0, keeping the zip and `compiled_pipeline.json` byte-identical. |

**Iteration 3 (CHANGES_REQUESTED, 0 HIGH, 3 MEDIUM, 11 NIT), answered in
iteration 4.** The chosen approach (C1, C1b, C2, C3, C3b, C4, gated on
x86_64, x86_64_nvidia and arm64_cpu) is unchanged. All 14 findings are
addressed; none is backlogged or ignored. Two documented fixes were
corrected after checking them (findings 3 and 10). The evidence is in
verification-notes, "Design iteration 4".

| # | Severity | Response |
|---|---|---|
| 1 | MEDIUM | Addressed. Task 3.6 step 4 is replaced by the review's snippet (`ensure_ascii=False`, UTF-8, a round-trip assertion first). Re-measured on a scratch export with C1: the README form doesn't round-trip (15 literal U+2014 characters in six other descriptors); the snippet gives 36 insertions, 0 deletions, 6 capsfilter blocks. D7 says why. The vendor README is not edited; its step 4 is backlogged. |
| 2 | MEDIUM | Addressed. Task 3.7 uses the review's command (layer root first, `python` appended, as `conftest.py` does). Re-measured: the docstring's `PYTHONPATH=python` form fails with `ModuleNotFoundError: No module named 'rpds.rpds'`; the new command gives 21 insertions, one block in each of the three constrained `folder_capture` documents, and the four consumer files give 49 passed. The docstring is backlogged, not edited. |
| 3 | MEDIUM | Addressed, with a corrected resolver. The labels are declared redacted. Task 4's Image rule resolves each one by tag, runs it with `--pull never`, and records only the label and the image ID. A missing R2 image is a STOP for the owner (task 4, Error Handling, AC 13); tasks 1 and 7 use the same rule. Two corrections to the review's snippet: (a) its `grep … \| head -1` can pick one of several matches silently. On this host the stock label `ubuntu:22.04` matches two local images of different architectures, so the rule requires exactly one image ID and stops otherwise. (b) The container runs by image ID, so no registry URI reaches a command line or a log. The rule also checks the OS and GStreamer each image reports, because a local build can retag `flask-app:latest`. All four R2 images resolve today. Iteration 5 extends the rule to the stock images and the built images (review 4, findings 2 and 3, below). |
| 4 | NIT | Addressed. C(X), its ¬C(X) sentence and 1.1 (bugfix), the Overview, the Glossary, D2 and the C4 comment now say "the first `jpegdec`-supported format the downstream offers is RGB-family", including an unconstrained `videoconvert ! fakesink` (xBGR), citing the Dell's diag-narrowing cases 3 and 10. Task 4.1 adds U0/X0. |
| 5 | NIT | Addressed. Root Cause, D1(d), D3 and the backlog now say that 1.24.2 `jpegdec` emits only native formats and that a direct RGB request is `not-negotiated` for YCbCr JPEGs; no direct-conversion path was observed there. The backlog trigger is re-keyed to "a JP7 image whose `jpegdec` takes the direct path behind `videoconvert`". |
| 6 | NIT | Addressed. C1b stops at the first frame-feed segment and returns the input list when it is already at index 0 (the review's loop; checked on fake segment lists). The C5 property and the Validation Rules row are restated to match. |
| 7 | NIT | Addressed with the preferred option. `compiler/compiler.py` joins `MIRRORED_SHARED_MODULE_RELPATHS` (task 2). The separate sha256 test is dropped, the mirror test moves from "Not changed" to task 5's scope list, and D6's scope sentence lists what the guard really covers. The README's stale "Current scope" paragraph is backlogged. |
| 8 | NIT | Addressed. Task 1(i3) counts the caps string once per file source in every constrained pre-fix-shape row (expected FAIL, 0), and task 2 counts it in every constrained fixed-shape row (1 before and after). |
| 9 | NIT | Addressed. Task 4.4's counterexample compiles for arm64_cpu with `compiler._feed_segment_first` patched to identity, applies the rewrites except C3b, and expects exit 134 in its own process. |
| 10 | NIT | Addressed, with the scope corrected. Task 5 makes the full preservation suite mandatory and runs `build-custom.sh`'s whole in-image gate body in the `flask-app` container with the repo mounted read-only. Task 2 records the same run as its baseline. The body has more than the review listed: the backend unit tests, the stream camera components gate, and six audit gates, with their exploration and negative-fixture suites. It installs its test packages with pip, so that container needs network. The extraction was checked: 141 lines, `bash -n` clean. Iteration 5 corrected how the body runs (review 4, finding 1, below). |
| 11 | NIT | Addressed. Bugfix 2.11 and task 8b use the review's normalization. Checked against 1.0.56's `pipeline-string.txt`: the `location=` and `buffer-message-id=` values are its only run-specific parts. Task 8b keeps the 1.0.56 run's node ids so the bridge element names match. |
| 12 | NIT | Addressed. `test_stream_free_packaging_preservation.py` is in the named Portal list (Overview and task 5). |
| 13 | NIT | Addressed. The build-host link is marked inferred. The Dell canary's sampling was measured offline from its byte-identical repo copy (`test-normal-1.jpg`, sha256 `9fabe604…`): baseline 576x768, 4:4:4, EXIF orientation 1. Task 8 check 3a names its source: a `cp` of `/aws_dda/cookies/test-normal-1.jpg`, sha-checked against `source-sha256.txt`. |
| 14 | NIT | Addressed. Task 8 pulls `bluenviron/mediamtx:1.9.3-ffmpeg`, records its digest and runs it by digest. If the tag can't be pulled, it stops and asks the owner for a replacement. |

Also in iteration 4, outside the findings: the owner's personal name and
the Fake camera's registry id are removed from committed text, the
redaction rule now names personal names and internal scanner or tool names,
and tasks 8 and 9 carry the lab Dell's GPU upgrade as pending owner
decisions.

**Iteration 4 (CHANGES_REQUESTED, 0 HIGH, 1 MEDIUM, 2 NIT), answered in
iteration 5.** The chosen approach (C1, C1b, C2, C3, C3b, C4, gated on
x86_64, x86_64_nvidia and arm64_cpu) is unchanged, and so are the owner
gates R1 to R5 and Q1. Review 4 re-ran iteration 3's three MEDIUM
procedures and found them working. It confirmed 13 of the 14 iteration-3
findings as resolved; finding 10's procedure didn't work as written. All
three new findings are addressed; none is backlogged or ignored. One
documented fix was corrected after checking it: finding 1's pass rule. The
evidence is in verification-notes, "Design iteration 5".

| # | Severity | Response |
|---|---|---|
| 1 | MEDIUM | Addressed, with the pass rule corrected. Task 5 item 5 keeps the verbatim extraction and its checks, then runs a derived copy (line 1's `set -e` turned into `set +e`, plus an ERR trap that logs `GATE-CMD-FAILED rc=… line=…`) in the JP5 image by its ID. Task 2 step 2 runs the same copy as the baseline. **Correction:** the body's first block (the pip install and the five backend unit test files) has no `Running …` echo. Under the review's rule ("between its `Running` line and the next"), a failure there belongs to no gate. A stubbed run of the real derived body showed exactly that: python resolved to a stub that fails chosen commands, and the trap line came before the first `Running` line. The rule now judges that block by the trap lines before the first `Running` line. The same run checked the rest: the body has no `\|\|`, `&&` or `exit` outside comments; the copy is 142 lines with `set +e` on line 2; every gate ran to the end; `line=` names a multi-line command's first line; and the exit status is 0 despite the failures, so only the log is judged. The verbatim body, under the same stub, stopped at its first failing command. The recorded grep also takes pytest's `FAILED`/`ERROR` summary ids. AC 9, Testing Strategy, Error Handling and the verification-notes plan say it is a derived copy, and why. |
| 2 | NIT | Addressed. The native stock rows run by name with `--pull never`. The emulated 22.04 row runs by the local amd64 image's ID with `--platform linux/amd64 --pull never`, with network on for `apt-get`. No stock tag is pulled or retagged, and each stock container records `uname -m`. A missing emulated image is recorded as not run. A missing native image isn't pulled: it is recorded and the owner is asked, and AC 1 stays open. Re-checked: `ubuntu:22.04` matches `b8e6b596a324` (amd64) and `e7a996328f5c` (arm64), and the plain name is the arm64 one. The iteration-4 evidence sentence is corrected in place. |
| 3 | NIT | Addressed with the review's second option: this host, not the build host. The plan doesn't assume access to the owner's dedicated server, and a build host's next build retags `flask-app`. Task 7 reads the tags the artifact's `flask-app.tar` applies (`RepoTags` in its `manifest.json`; today `flask-app:latest`) and records their IDs. It then loads the tar and restores those tags at once, before anything runs, and re-checks the JP5 label by the Image rule. The loaded image is recorded as `flask-app:<component version>` plus its ID and run by ID. A label that doesn't come back, or a built image reporting the wrong OS or GStreamer, is a STOP for the owner. The Overview and task 5 run the JP5 image by `"$IMG"`, never by name. A built image now runs the 4.4 rows of its own arch; iteration 4 said "arm64 CPU rows" for every build. |

**Iteration 3's 14 findings, as of iteration 5.** Findings 1, 2, 4 to 9
and 11 to 14 stand as iteration 4 applied them, and review 4 confirmed
each one; it re-ran 1, 2, 3, 6, 7, 11, 12 and 13. Finding 3 stands,
extended to the stock and built images by review 4's findings 2 and 3.
Finding 10 keeps its scope: the whole gate body, mandatory, in the JP5
container, recorded under "Task 5". Its procedure is corrected by review
4's finding 1.

Also in iteration 5, outside the findings:
- The Dell canary's sampling was measured again from its repo copy. That
  copy was confirmed byte-identical to `/aws_dda/cookies/test-normal-1.jpg`
  on 2026-10-09 (sha256 `9fabe604…`). It is SOF0, 8-bit, 576x768, with
  three components sampled 1x1, Pillow `get_sampling` 0 and EXIF
  orientation 1. The spec states this as measured.
- Tasks 8 and 9 keep the lab Dell's GPU upgrade as pending owner
  decisions, in the recommended order:
  - task 8 runs first, with the Dell deployed as an x86_64 CPU station on
    the amd64 LocalServer, and must not re-run station quick setup;
  - after that, the Dell can optionally switch to amd64Nvidia for an
    on-device task 9;
  - R580 is the last driver branch with Volta support, and the
    amd64Nvidia image is CUDA 12.6 based;
  - sm_70 support in that image is task 9's open item, not investigated
    here.

**Iteration 5 (CHANGES_REQUESTED, 0 HIGH, 1 MEDIUM, 2 NIT), answered in
iteration 6.** The chosen approach (C1, C1b, C2, C3, C3b, C4, gated on
x86_64, x86_64_nvidia and arm64_cpu) is unchanged, and so are the owner
gates R1 to R5 and Q1. Review 5 re-ran iteration 3's three MEDIUM
procedures and found them working, and it confirmed review 4's findings 2
and 3 as resolved. All three new findings are addressed with the review's
fixes; none is backlogged or ignored. The evidence is in
verification-notes, "Design iteration 6".

| # | Severity | Response |
|---|---|---|
| 1 | MEDIUM | Addressed. This is the second correction of review 3's finding 10 fix, whose documented form ("with the repo mounted read-only") was wrong. Task 5 item 5 now copies the tree under test (`git ls-files -z --cached --others --exclude-standard \| tar …`) into its scratch directory, runs the derived copy against that copy read-write, and removes the copy with a container afterwards, because the run leaves root-owned files. Task 2 step 2 does the same from the pristine worktree. One pass rule now reads the same in task 5 item 5, AC 9 and the Error Handling row: the first block and the stream gate are expected to fail in the JP5 image for image-content reasons; the six audit gates must pass in both runs; the work stops on a failing audit gate, or on a first-block or stream-gate trap line or test id that task 2 didn't show. The rationale is corrected: in the JP5 image the verbatim body stops at the first block (body line 8, derived line 9), not at the stream gate. The first block joins Notes "Pre-existing". Checked in this iteration: the copy line on the worktree, the conftest fixture and the `setUp` write the review cites, and the build's own read-write mount of its checkout. The in-image counts (review 5's V1 and V2) were not re-run here. |
| 2 | NIT | Addressed. The grep also keeps `^=+ .* in [0-9.]+s`, one summary line per pytest call (9 in the body). Task 2 step 3 and task 5 say that the grep output is the only copy of the counts. |
| 3 | NIT | Addressed with the review's text, plus `--expected-bucket-owner` on the copy. Task 7 step 2 reads the artifact URI from the published version's recipe (`get-component … \| base64 -d`, then `Manifests[].Artifacts[].URI`), copies the zip with `aws s3 cp` (both are reads), and records only the component name and version. |

**Review 3's 14 findings, re-checked in iteration 6** (iteration 7's
re-check follows at the end of this section). Each was read against the
current text. Review 5 re-ran findings 1, 2 and 3; this iteration re-ran
3's resolver and 13's measurement.

| # | Severity | Status | Where |
|---|---|---|---|
| 1 | MEDIUM | Applied as documented (iteration 4). Review 5's re-run: the round-trip holds, and with C1 the diff is 36 insertions, 0 deletions, 6 blocks | Task 3.6; D7; Notes backlog |
| 2 | MEDIUM | Applied as documented. Review 5's re-run: no `ModuleNotFoundError`, and with C1 the diff is 21 insertions | Task 3.7; D7; Notes backlog |
| 3 | MEDIUM | Applied, with the resolver corrected in iteration 4: exactly one image ID instead of `head -1`, and the container runs by ID. Each label is resolved by tag, run with `--pull never` and recorded as label plus image ID only; a missing R2 image is a STOP for the owner, never "not run". Re-resolved in this iteration: the four R2 labels give the same IDs as in iterations 4 and 5, so no stop was needed | Tasks 1, 4 (Image rule), 4.4 to 4.6, 7; Testing Strategy; D12; Error Handling (b); AC 13 |
| 4 | NIT | Applied as documented | bugfix C(X), ¬C(X), 1.1; Overview; Glossary; task 4.1 (U0, X0) |
| 5 | NIT | Applied as documented | Root Cause; D1(d); D3; Notes backlog |
| 6 | NIT | Applied as documented | C1b; C5; Validation Rules; task 1(c3) |
| 7 | NIT | Applied, with the preferred option | D6; C5; Not changed; tasks 2 and 5; bugfix 2.5, AC 7 |
| 8 | NIT | Applied as documented | Tasks 1(i3) and 2; D11 |
| 9 | NIT | Applied as documented | Tasks 4.4 and 4.5 |
| 10 | NIT | Scope applied as documented: the whole body, mandatory, in the JP5 container, recorded under "Task 5". Its documented procedure was wrong twice over: under `set -e` the body stops at its first failing block (review 4), and a read-only mount errors every conftest-loading test at setup (review 5). Corrected in iterations 5 and 6 | Task 5 item 5; task 2 step 2; AC 9; Testing Strategy; Error Handling; Notes |
| 11 | NIT | Applied as documented | bugfix 2.11; task 8b |
| 12 | NIT | Applied as documented | Tasks Overview; task 5 item 4 |
| 13 | NIT | Applied. The build-host link is marked inferred. The Dell canary's sampling is measured, not inferred, from its byte-identical repo copy (sha256 `9fabe604…a743`), and measured again in this iteration: SOF0, 8-bit, 576x768, every component sampled 1x1, so 4:4:4, Pillow `get_sampling` 0, EXIF orientation 1. Check 3a names its source | bugfix Introduction, 1.3; Root Cause; task 8 3a; verification-notes |
| 14 | NIT | Applied as documented. In addition, iteration 6 sets MediaMTX's RTP and RTCP addresses to 127.0.0.1 and checks every listener with `ss` before use, because `rtspAddress` alone left the "localhost only" note untrue under MediaMTX's documented defaults (not checked offline) | Task 8 (3c-stream); bugfix Assumptions; Testing Strategy |

Also in iteration 6, outside the findings: the lab Dell's GPU-upgrade
notes in tasks 8 and 9 were re-checked against the code.
`detect_arch.sh` reports `x86_64_nvidia` on x86 when `nvidia-smi` runs,
`/proc/driver/nvidia/version` exists, or `lspci` lists an NVIDIA VGA or 3D
controller. `src/host_scripts/get_nvidia_libs_versions.sh` picks `tegra`
only on an aarch64 Jetson with CUDA present. `Dockerfile.x86_64_nvidia`
starts from `nvidia/cuda:12.6.3-cudnn-devel-ubuntu22.04`. Whether that
image still supports sm_70 stays task 9's open item, not investigated.

**Iteration 6 (CHANGES_REQUESTED, 0 HIGH, 1 MEDIUM, 2 NIT), answered in
iteration 7.**
- The chosen approach is unchanged: C1, C1b, C2, C3, C3b and C4, gated on
  x86_64, x86_64_nvidia and arm64_cpu. So are the owner gates R1 to R5 and
  Q1.
- Review 6 re-ran iteration 3's three MEDIUM procedures verbatim and found
  them working. It confirmed review 5's three findings as resolved.
- All three new findings are addressed with the review's fixes, and each
  is extended where a run showed a gap. None is backlogged or ignored.
- The evidence is in verification-notes, "Design iteration 7".

| # | Severity | Response |
|---|---|---|
| 1 | MEDIUM | Addressed with the review's three-part fix, and extended. (1) The Overview's legacy-builder command mounts the tree at `/w/repo`. Task 1(h), task 2's legacy goldens and step 2's `gstreamer/` run, task 3.5, task 5 item 3, AC 6 and Testing Strategy "Container unit" use it. Tasks 2 and 5 also run `test_pipeline_builder.py` on its own, the run that showed the problem. (2) Each new legacy case asserts `inspect.getsourcefile(type(self.pipeline_builder))` against the tree's path (C5). (3) Task 5 item 5 and verification-notes replace "not traced" with the mechanism, and describe the first block as importing a mix. Notes gains the mount rule, with the verbatim gate body as the one exception. Re-measured in the JP5 image (`f2e221c3e2d1`, test packages supplied offline): under `/repo` the single-file run gave 12 passed, but with `gstreamer.pipeline_builder` loaded from `/gstreamer/pipeline_builder.py`. That is sha256 `8f0f7113…`, 20107 bytes; the tree's file is `bf7221c7…`, 20504 bytes. The `sys.path` spy caught `insert(0, '/')` coming from `_pytest/python.py:668` through `pathlib.py:583`. Under `/w/repo` the run gave 12 passed from the tree's file. The guard failed under `/repo` with the baked path and passed under `/w/repo`. **Extension:** the same problem reaches task 4's script rows, which the review didn't cover. Every LocalServer image's working directory is `/`. From there, `python3.11 -c` imported `/workflow_engine/pipeline_executor.py` and `/gstreamer/gst_pipeline.py` ahead of `PYTHONPATH=/repo/src/backend`. A script run by path, or `-c` with `-w /work`, imported the tree's. Task 4 gains a Provenance rule: the tree at `/w/repo`, scripts run by path from `/work`, and each row records and checks its module paths. A mismatch is a harness error (new Error Handling row). |
| 2 | NIT | Addressed with the review's lines. Task 5 item 5's block is split into parts. Part 1a checks for a leftover `$G` before any `mkdir`. Part 1b prints `derived copy OK` and `tree copy OK: <n> files`, or a STOP line. Part 2 runs the container only after the three OK lines, and part 3 is the cleanup. One addition: the review's STOP message says to remove a leftover `$G` with the block's container `rm`. The text now says to do that only once no gate container is running, because tasks 2 and 5 use the same path and the leftover may belong to a run still in progress. Run in iteration 7 on the worktree: parts 1a, 1b and 3 printed the three OK lines (4562 files) and left nothing. A leftover `$G`, a broken line 2 and one extra file in the copy each printed their STOP line. |
| 3 | NIT | Addressed with the review's rule, and extended. Task 8 3c-stream step 2 judges only the Local Address column (field 5 of `ss -H`). It requires exactly tcp 127.0.0.1:8554, udp 127.0.0.1:8000 and udp 127.0.0.1:8001, and says the Peer column is not judged. **Extensions** (review 6's U2): the grep also takes the container's `ffmpeg` process, and step 1's publisher gets `-rtsp_transport tcp`. Over RTSP's UDP default, ffmpeg could open its own RTP sockets on every interface, and `grep mediamtx` wouldn't show them. Over TCP it needs no UDP sockets, and any ffmpeg UDP socket now fails the rule. (Review 7, NIT 4: that held only for sockets off 127.0.0.1. Iteration 8 counts every socket.) Checked on synthetic lines in this host's `ss -H` format: the expected three pass. A wildcard RTP listener, a wildcard ffmpeg UDP socket, an IPv6 wildcard listener, a missing UDP listener and an empty file each give STOP. The MediaMTX and ffmpeg defaults themselves weren't checked offline: the image isn't local, and nothing may be pulled. |

**Review 3's 14 findings, re-checked in iteration 7.** Each was read
against the current text. The three MEDIUM procedures and finding 13's
measurement were re-run (verification-notes, "Design iteration 7").
Two documented fixes were corrected in earlier iterations, because they
didn't work as written:
- finding 3's resolver, in iteration 4;
- finding 10's procedure, in iterations 5 and 6.

The other twelve stand as documented.

| # | Severity | Status in iteration 7 | Where |
|---|---|---|---|
| 1 | MEDIUM | Applied as documented. Re-run verbatim (extracted from task 3.6) on a pristine 48841a0 export: exit 0, the round-trip assertion holds, and the baseline is byte-identical afterwards (15 literal em dashes). The C1-applied diff (36 insertions, 0 deletions, 6 blocks) is reviews 4 to 6's measurement, not re-run here | Task 3.6; D7; Notes backlog |
| 2 | MEDIUM | Applied as documented. Re-run verbatim on the same export with the golden deleted first: exit 0, no `ModuleNotFoundError`, and the golden it re-created is byte-identical (0 `appsrc`). The C1-applied diff (21 insertions) is reviews 4 to 6's | Task 3.7; D7; Notes backlog |
| 3 | MEDIUM | Applied, with the resolver corrected in iteration 4: exactly one image ID instead of `head -1`, and the container runs by ID. Labels are redacted, resolved by tag, run with `--pull never` and recorded as label plus image ID. A missing R2 image is a STOP for the owner, never "not run". Re-run on 2026-10-12: each R2 label resolves to exactly one ID (`f2e221c3e2d1`, `1fa46843533f`, `6d5ce25b9e2d`, `b365ed521490`). Probed by ID with `--network none`, each reports its row's OS and GStreamer. No STOP was needed | Tasks 1, 4 (Image rule), 4.4 to 4.6, 7; Testing Strategy; D12; Error Handling (b); AC 13 |
| 4 | NIT | Applied as documented | bugfix C(X), ¬C(X), 1.1; Overview; Glossary; task 4.1 (U0, X0) |
| 5 | NIT | Applied as documented | Root Cause; D1(d); D3; Notes backlog |
| 6 | NIT | Applied as documented | C1b; C5; Validation Rules; task 1(c3) |
| 7 | NIT | Applied, with the preferred option | D6; C5; Not changed; tasks 2 and 5; bugfix 2.5, AC 7 |
| 8 | NIT | Applied as documented | Tasks 1(i3) and 2; D11 |
| 9 | NIT | Applied as documented | Tasks 4.4 and 4.5 |
| 10 | NIT | Scope applied as documented: the whole body, mandatory, in the JP5 container, recorded under "Task 5". Its documented procedure ("with the repo mounted read-only") was wrong, and was corrected in iteration 5 (the derived copy, review 4) and iteration 6 (a writable copy of the tree, review 5). Iteration 7 adds the printed verdicts (review 6, NIT 2), and the body still runs verbatim at `/repo` | Task 5 item 5; task 2 step 2; AC 9; Testing Strategy; Error Handling; Notes |
| 11 | NIT | Applied as documented | bugfix 2.11; task 8b |
| 12 | NIT | Applied as documented | Tasks Overview; task 5 item 4 |
| 13 | NIT | Applied. The build-host link is marked inferred. The Dell canary's sampling is measured, not inferred. Re-measured on 2026-10-12 from the byte-identical repo copy (503050 bytes, sha256 `9fabe604…a743`, confirmed for the Dell's source file on 2026-10-09): SOF0 baseline, 8-bit, 576x768, components 1x1, so 4:4:4. Pillow 11.3.0 `get_sampling` gives 0, and the EXIF orientation is 1. Check 3a names its source | bugfix Introduction, 1.3; Root Cause; task 8 3a; verification-notes |
| 14 | NIT | Applied as documented. Iteration 6 added the RTP and RTCP addresses on 127.0.0.1. Iteration 7 adds the local-address `ss` rule and the TCP publisher (review 6, NIT 3) | Task 8 (3c-stream); bugfix Assumptions; Testing Strategy |

Also in iteration 7, outside the findings: the lab Dell's GPU-upgrade
notes were re-read against the code.
- `detect_arch.sh` lines 88 to 100 and 119 to 123: `nvidia-smi`,
  `/proc/driver/nvidia/version`, or an `lspci` NVIDIA VGA or 3D
  controller gives `x86_64_nvidia`.
- `get_nvidia_libs_versions.sh` lines 188 to 210: `tegra` only on
  aarch64.
- `recipe-amd64.yaml` line 108 runs that script.
- `Dockerfile.x86_64_nvidia` line 21 is the CUDA 12.6.3 base.

Task 8's recommended order now lists task 9's decision inputs: R580, CUDA
12.6, and sm_70 support UNVERIFIED. sm_70 support stays task 9's open
item, not investigated.

**Iteration 7 (CHANGES_REQUESTED, 0 HIGH, 3 MEDIUM, 3 NIT), answered in
iteration 8.**
- The chosen approach is unchanged: C1, C1b, C2, C3, C3b and C4, gated on
  x86_64, x86_64_nvidia and arm64_cpu. So are the owner gates R1 to R5 and
  Q1.
- Review 7 re-ran iteration 3's three MEDIUM procedures verbatim and found
  them working. It confirmed review 6's three findings as resolved.
- All six new findings are addressed. None is backlogged or ignored.
- Runs in iteration 8 showed gaps in two documented fixes, and both are
  corrected: finding 1's reason for the host `mkdir`, and NIT 4's count
  rule. NIT 6's container mount is narrowed.
- The evidence is in verification-notes, "Design iteration 8".

| # | Severity | Response |
|---|---|---|
| 1 | MEDIUM | Addressed with the review's fix, and the rationale corrected. Task 2 names the legacy goldens' location, `test/backend-test/gstreamer/goldens/jpegdec_native_caps/`, resolved from `os.path.dirname(os.path.abspath(__file__))`. It captures them once on the unfixed tree: the Overview command plus a read-write mount of only `test/backend-test/gstreamer/goldens/`, then a container `chown` back to the host user and a `find` that must show no root-owned entry. Every later run (task 2's second run, 3.5, task 5) uses the plain read-only command. The location joins task 5's sha256 list and scope check, C5, Testing Strategy and verification-notes "Task 2". **Correction:** the review's comment says the host `mkdir -p` is needed because the mount point must exist inside the read-only mount. Measured in iteration 8, the run works without it: Docker creates the missing source directory itself, as root, and that directory is also the mount point. The `mkdir` stays, so that the directory belongs to the host user from the start, and the `chown` covers what the run creates inside it. Measured with a stand-in helper in the JP5 image: the capture run wrote its golden as uid 0, a write outside the goldens directory failed with `Read-only file system`, the `chown` left no entry the host user didn't own, and the plain read-only run asserted. The real pytest capture wasn't run, because it needs the network for `pip`. |
| 2 | MEDIUM | Addressed with the review's fix. 4.5 runs in the arm64 CPU and JP5 images. The local amd64 image is out of scope there, and verification-notes "Task 4" records why. Iteration 8 re-checked the evidence: its `/` holds `app.py`, `gstreamer`, `model` and `utils` but no `workflow_engine`, and no `pipeline_executor.py`, `python_bridge.py` or `rendering.py` exists in it. AC 14, P10, D11 and Testing Strategy now say "the arm64 CPU and JP5 images", and why those images stand in for older amd64 LocalServers such as 1.0.50. The amd64 image stays in 4.2, and the Provenance rule says it can't run rows. Task 10's precondition names the two images. |
| 3 | MEDIUM | Addressed with the review's fix, applied to every row that runs Python in a LocalServer image. The Image rule's row command adds `--init` and `-e PYTHONUNBUFFERED=1`. The Provenance rule requires it for every row, requires the provenance lines to be printed before the pipeline is built, and judges an expected abort as exit 134 under `--init`. Exit 133, or missing provenance lines, is a harness error (Error Handling). Task 1's counterexample, 4.4, 4.5 and task 7's built-image rows use it, with `--platform linux/amd64` for amd64 and amd64Nvidia. 4.1's emulated row and 4.2 also get `--init`. Re-measured in iteration 8 with a stand-in row that prints a provenance line, then aborts GStreamer the way a fed appsrc does. In JP5 with Python as PID 1 (iteration 7's shape), it exited 133 and lost the line. In JP5 and arm64 CPU with the row command, it exited 134 and kept it. In amd64 (emulated), `os.abort()` with the row command exited 134. The PID-1 shape under emulation wasn't re-run, because review 7 measured that it hangs. |
| 4 | NIT | Addressed, with the count rule corrected. Iteration 8 ran the iteration-7 block, review 7's block and a new block on 11 synthetic cases. Review 7's block still printed `localhost only` for RTSP twice with RTCP missing, and for RTP twice with RTSP missing: its total is three, and its combined count of expected sockets is three. The new block requires three lines in total and each expected socket exactly once. The expected three pass, and the other ten cases STOP. Bugfix Assumptions say "`0.0.0.0:*` (or `[::]:*`)". The iteration-6 response's "any ffmpeg UDP socket now fails the rule" is annotated in place. |
| 5 | NIT | Addressed with the review's wording. The Error Handling row names the three prefixes that task 4's Provenance rule requires: the tree's, the second mount's for unfixed-tree code, and the baked ones for own-code rows. It also gains finding 3's exit 133 and missing provenance lines. |
| 6 | NIT | Addressed, with the mount narrowed. Task 4's Workspace bullet empties the workspace with a container that mounts only the workspace (`find /g -mindepth 1 -delete`), then runs `rmdir` and prints a check. The review's form mounts all of `$HOME/dda-tmp` read-write into a root container. The narrower mount keeps that container away from the other scratch directories there. Task 1 uses task 4's workspace rule, and task 7 step 8 uses this removal. Measured on a scratch workspace that a container had written to: the host user's `rm -rf` failed with `Permission denied`, and the container removal plus `rmdir` left nothing. |

**Review 3's 14 findings, re-checked in iteration 8.** Each was read
against the current text. The three MEDIUM procedures and finding 13's
measurement were re-run (verification-notes, "Design iteration 8"). As
before, two documented fixes stand corrected from earlier iterations,
because they didn't work as written:
- finding 3's resolver, in iteration 4;
- finding 10's procedure, in iterations 5 and 6.

The other twelve stand as documented. Iteration 8 changes none of the
fourteen. It adds to finding 3's scope: the row command, and the
narrowed scope of 4.5.

| # | Severity | Status in iteration 8 | Where |
|---|---|---|---|
| 1 | MEDIUM | Applied as documented. Re-run verbatim (extracted from task 3.6) on a pristine 48841a0 export: exit 0, the round-trip assertion holds, and the baseline is byte-identical afterwards (15 literal em dashes, no `\u2014` escape). The C1-applied diff (36 insertions, 0 deletions, 6 blocks) is review 7's re-run | Task 3.6; D7; Notes backlog |
| 2 | MEDIUM | Applied as documented. Re-run verbatim on the same export, with the golden deleted first: exit 0, no `ModuleNotFoundError`, and the re-created golden is byte-identical (0 `appsrc`). The C1-applied diff (21 insertions) is review 7's | Task 3.7; D7; Notes backlog |
| 3 | MEDIUM | Applied, with the resolver corrected in iteration 4: exactly one image ID instead of `head -1`, and the container runs by ID. Labels are redacted, resolved by tag, run with `--pull never` and recorded as label plus image ID. A missing R2 image is a STOP for the owner, never "not run". Re-run on 2026-10-14: each R2 label resolves to exactly one ID (`f2e221c3e2d1`, `1fa46843533f`, `6d5ce25b9e2d`, `b365ed521490`). Probed by ID with `--network none`, each reports its row's OS and GStreamer. No STOP was needed | Tasks 1, 4 (Image rule, row command), 4.4 to 4.6, 7; Testing Strategy; D12; Error Handling (b); AC 13 |
| 4 | NIT | Applied as documented | bugfix C(X), ¬C(X), 1.1; Overview; Glossary; task 4.1 (U0, X0) |
| 5 | NIT | Applied as documented | Root Cause; D1(d); D3; Notes backlog |
| 6 | NIT | Applied as documented | C1b; C5; Validation Rules; task 1(c3) |
| 7 | NIT | Applied, with the preferred option | D6; C5; Not changed; tasks 2 and 5; bugfix 2.5, AC 7 |
| 8 | NIT | Applied as documented | Tasks 1(i3) and 2; D11 |
| 9 | NIT | Applied as documented. The counterexample now runs with task 4's row command, so its expected exit 134 holds (review 7, finding 3) | Tasks 4.4 and 4.5 |
| 10 | NIT | Scope applied as documented: the whole body, mandatory, in the JP5 container, recorded under "Task 5". Its documented procedure ("with the repo mounted read-only") was wrong, and was corrected in iteration 5 (the derived copy, review 4) and iteration 6 (a writable copy of the tree, review 5). Iteration 7 added the printed verdicts (review 6, NIT 2). Unchanged in iteration 8 | Task 5 item 5; task 2 step 2; AC 9; Testing Strategy; Error Handling; Notes |
| 11 | NIT | Applied as documented | bugfix 2.11; task 8b |
| 12 | NIT | Applied as documented | Tasks Overview; task 5 item 4 |
| 13 | NIT | Applied. The build-host link is marked inferred. The Dell canary's sampling is measured, not inferred. Re-measured on 2026-10-14 from the byte-identical repo copy (503050 bytes, sha256 `9fabe604…a743`, confirmed for the Dell's source file on 2026-10-09): SOF0 baseline, 8-bit, 576x768, all three components sampled 1x1, so 4:4:4. Pillow 11.3.0 `get_sampling` gives 0, and the EXIF orientation is 1. Check 3a names its source | bugfix Introduction, 1.3; Root Cause; task 8 3a; verification-notes |
| 14 | NIT | Applied as documented. MediaMTX runs by its recorded digest, with a STOP before any other tag. Iteration 8 tightens its `ss` rule (review 7, NIT 4) | Task 8 (3c-stream); bugfix Assumptions; Testing Strategy |

Also in iteration 8, outside the findings: the lab Dell's GPU-upgrade
notes in tasks 8 and 9 are unchanged. They give the recommended order
(task 8 first, on the amd64 LocalServer, without re-running station quick
setup), R580 as the last driver branch with Volta support, and the CUDA
12.6 base of the amd64Nvidia image. sm_70 support stays task 9's open
item, not investigated.
