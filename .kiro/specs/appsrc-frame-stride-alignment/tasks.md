# Implementation Plan

## Overview

This plan fixes ONE device-side defect with TWO halves, using the exploratory bugfix workflow —
reproduce first, capture existing behavior, apply the minimal fix, then validate.

- **The defect (tasks 1, 2, 3)**: every site that wraps raw frame bytes for an `appsrc` declares caps
  whose row stride the buffer does not satisfy. GStreamer rounds a packed `video/x-raw` row up to a
  4-byte multiple, so `RGB` 810x1080 needs `2432 * 1080 = 2626560` bytes and the store's tightly
  packed frame supplies `2624400`. Preview returns HTTP 200 with a BLACK image; capture returns HTTP
  200 with a grey cast (mean `61.02/61.05/61.04` vs source `117.34/115.61/118.34`, correlation
  `-0.088`).
- **The silence (same tasks)**: `run_pipeline`'s `on_message` appends `Gst.MessageType.WARNING` to
  `acceptable_messages` only when `status_sink is not None`, and every Pipeline_Configuration caller
  passes `None`, so the `videoconvert` warning is dropped and the endpoint answers 200 over the blank
  file. Fixing only the padding would leave the next mismatch just as invisible.

**Fix direction — two candidates were tested locally, one is chosen.** Both were run in
`flask-app:latest` (GStreamer 1.20.3) on a tight `RGB` 810x108 buffer through
`appsrc ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink`:

| candidate | result | verdict |
|---|---|---|
| unfixed (tight buffer) | WARNING + normal EOS, JPEG mean `0.00/0.00/0.00` | the defect |
| **pad rows to `GST_ROUND_UP_4(width * bpp)`** | clean EOS, mean `127.50/127.69/127.16` vs source `127.49/127.68/127.16`, max error 1.0, corr 1.000 | **CHOSEN** |
| attach `GstVideoMeta` with the true stride (`GstVideo.buffer_add_video_meta_full`) | clean EOS, identical pixel result, copy-free | rejected |

The video-meta route is copy-free and idiomatic, and it worked — but it only works for elements that
honor `GstVideoMeta`. These pipelines run `emltriton` (this repo's own inference plugin, see
`src/backend/gstreamer/pipeline_builder.py` line 197) alongside `videocrop`, `jpegenc`, `bayer2rgb`
and the Nvidia converters; a plugin that maps the buffer and trusts the caps stride would silently
misread padded-vs-tight, and any element that copies the buffer without propagating the meta loses
the stride entirely. Padding produces exactly the byte layout the caps already promise, so no
downstream element has to understand anything new. The cost is one full-frame copy per fed frame
(2.6 MB for `bus.jpg`) on a path that runs once per preview/capture/run — and ZERO copies for
aligned widths, where the helper returns the identical object (Requirement 2.2).

**Where the fix goes.** One shared helper, applied at the wrapping sites — nothing else changes:

| site | file:line | mode |
|---|---|---|
| classic preview/capture/Frame_Feed | `src/backend/gstreamer/gst_pipeline.py` line 76, `GstPipelineManager.create_buffer` | reconcile + **strict** (raise on irreconcilable) |
| bridged-run fed `appsrc` | `src/backend/workflow_engine/python_bridge.py` line 1897, caps from `_fed_frame_caps` line 1671 | reconcile + **strict** |
| bridge handler output | `src/backend/workflow_engine/python_bridge.py` line 1844 | reconcile only, **never raises** |

The third site is already consistent by construction — `_invoke_process_frame` (lines 589-598) writes
rows back at the input's own stride (`stride = len(frame) // height`) "so the appsrc caps stay valid",
so it echoes whatever stride arrived. It gets the reconciliation because a raw `handle()`-contract
node can return a tight buffer against padded negotiated caps, but NOT the raise: it is mid-stream,
and a new mid-stream exception would fail runs that work today (Requirement 2.9).

**Loudness by up-front validation, not by promoting bus warnings.** `create_buffer` and the fed
`appsrc` raise when a buffer's size is neither the tight nor the padded size, naming the caps, the
dimensions, the bytes received and the bytes expected. Promoting `WARNING` to fatal was weighed and
rejected: `acceptable_messages` for `status_sink=None` is load-bearing for existing flows that emit
benign warnings (the local reproduction shows `videoconvert` posting a generic warning message rather
than a specific one), a bus warning cannot be attributed to a specific frame, and it can only report
— never reconcile. Up-front validation is deterministic and needs no bus change (Requirements 2.5,
3.6).

**Scope, reasoned rather than assumed.**

- `video/x-bayer` is UNTOUCHED. GStreamer does stride Bayer at `GST_ROUND_UP_4(width)` —
  `bayer2rgb`'s declared unit size was observed as `82416` for 813x101 and `876960` for 810x1080 — but
  `video/x-bayer` is not a `GstVideoFormat`, so the `gst_video_frame_map_id` check behind THIS defect
  never applies to it, and a mismatch there was observed as a FATAL ERROR in a
  `videoconvert ! fakesink` chain and as a pass-through in the `bayer2rgb ! ... ! jpegenc ! filesink`
  chain the device actually runs. Loud-or-visible, never silently black. Physical Basler cameras
  (`28183exv`/`g6zrsox3` and `o70qz7ci`/`563tiauk` on `jetson-thor1`; all 7 configurations on the JP6
  Orin) depend on that path, so it stays byte-identical (Requirement 3.4).
- `RGBA`/`BGRA` are 4 bytes per pixel -> every row aligned -> no-op (verified: `RGBA` 810x1080 stride
  3240 = tight). `GRAY8` is aligned only when `width % 4 == 0` (verified: `GRAY8` 810 -> stride 812).
  `RGB`/`BGR` are the exposed cases.
- NOT touched: the store's packed-RGB contract, `default_camera_configurations.json`, and
  `db_backfill.py` — all three belong to `static-camera-pixel-format-and-detection-results`, are
  deployed, and are verified (Requirement 3.9).

**Deployed-workflow exposure is UNVERIFIED ON DEVICE.** `_point_appsrc_at_frame_feed`
(`pipeline_executor.py` line 2773) and `_frame_caps` (line 2821) emit the same
`video/x-raw,format=RGB` and route through the same `run_pipeline`/`create_buffer`, so the Frame_Feed
path should share the exposure — but confirming it needs a deployed workflow bound to the static
camera, which was not available during the measurement session. Task 1 ESTABLISHES it in the
container instead of asserting it (bugfix.md 1.12).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code. Task 1 (Property 1: Bug Condition) must FAIL; task 2 (Property 2: Preservation) must PASS. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Implementation (depends on 1 and 2): the shared stride helper, applied at the three wrapping sites, then re-run 1 and 2."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint: the device suite green in the flask-app container, suites as separate processes, no component build (depends on 3)."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Build hand-off to the user plus the post-build on-device verification checklist (depends on 4). BUILDS NOTHING, DEPLOYS NOTHING."
    }
  ]
}
```

- Tasks 1 and 2 must both be complete BEFORE task 3. Sub-tasks 3.3 / 3.4 depend on 3.1 / 3.2.
- **Nothing in this plan builds or deploys a component.** Builds take ~100 minutes, corrupt each other
  if run concurrently, and are the user's to drive.

## Container test command (use this exact form)

The device suite uses **hypothesis**, not fast-check. Run ONE suite path per invocation:

```
docker run --rm -v "$(pwd)":/w/dda -w /w/dda \
  -e PYTHONPATH=/w/dda/src/backend:/w/dda/test/backend-test:/w/dda/test/backend-test/utils/streaming \
  -e LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64 \
  flask-app:latest bash -lc 'PY=$(command -v python3.10 || command -v python3.11); \
    $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; \
    $PY -m pytest <one suite path> -q -p no:cacheprovider'
```

Three deliberate details, all measured — carry them verbatim:

- **Mount at `/w/dda`, NOT `/repo`.** The repo root has a tracked `__init__.py`, so pytest's prepend
  import mode resolves `pkg_root` to the mount point and prepends its PARENT; at `/repo` that parent
  is `/`, where `flask-app:latest` bakes a stale July backend copy, and the tests silently import the
  image's modules.
- **FLIPPED interpreter order (`python3.10 || python3.11`).** The image is JP6-layout: `python3` is
  3.10.12 and the deps (incl. pydantic) live under 3.10. The documented `python3.11 || python3.10`
  shim picks a dep-less 3.11 and conftest dies on `ModuleNotFoundError: No module named 'pydantic'`.
- **Run suites as SEPARATE processes.** `static_image_camera` leaves `utils.server_setup` a stub in
  `sys.modules`, poisoning a later `from app import app`; and
  `camera_sync/test_property_reconnect_catch_up.py::test_reconnect_publishes_complete_current_state`
  does not terminate when it follows `utils` in one process. Both reproduced at HEAD.

`gi`, `Gst`, `GstVideo` and `Aravis` ARE importable in `flask-app:latest` (GStreamer 1.20.3,
confirmed), so both suites below exercise REAL GStreamer rather than launch-string shapes only. Set
`COMPONENT_WORK_PATH` to a tmp dir in the tests — `run_pipeline` writes its `GST_DEBUG_FILE` there.
Root conftest hypothesis profiles: `fast` = 25 examples, `HYPOTHESIS_PROFILE=ci` = 100.

## Tasks

- [x] 1. Write bug condition exploration test for appsrc frame stride alignment
  - **Property 1: Bug Condition** - Wrapped buffer size disagrees with the stride its declared caps imply, and the mismatch is silent
  - **CRITICAL**: These tests MUST FAIL on unfixed code - failure confirms the defect exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples showing a tightly packed unaligned-width frame is wrapped short and rendered black without any caller-visible signal
  - **Conventions first**: READ `test/backend-test/static_image_camera/test_workflow_feed.py` (its executor harness and the `"appsrc name=appsrc caps=video/x-raw,format=RGB "` assertion that must keep passing, plus the cross-directory `sys.path` shim it uses at lines 49-56), `test/backend-test/static_image_camera/static_image_strategies.py` (`image_specs`, `render_image_bytes`, `expected_frame`), `test/backend-test/static_image_camera/test_property_static_camera_pixel_format.py` (the previous spec's harness style and its first-`caps=` regex helper), and `test/backend-test/gstreamer/test_gst_pipeline_executor.py` BEFORE writing anything
  - New files: `test/backend-test/gstreamer/test_property_appsrc_frame_stride.py` and, for the generators, `test/backend-test/gstreamer/stride_strategies.py` (reuse `render_image_bytes` / `expected_frame` from `static_image_strategies` through the same `sys.path` shim rather than duplicating them)
  - **Generator must straddle the boundary — a generator that only produced aligned widths would pass vacuously on unfixed code and is a TEST DEFECT (Requirement 2.11)**: generate widths and heights such that BOTH `width * 3 % 4 == 0` and `width * 3 % 4 != 0` occur, and assert that fact about the generated sample itself (a meta-assertion over a collected set, or `hypothesis.event` + explicit boundary examples). Include the four measured device widths as explicit examples: 810 and 773 (unaligned), 768 and 1280 (aligned)
  - Assert at the wrapping site, through the REAL `GstPipelineManager.create_buffer`: for every generated frame, `buffer.get_size()` equals the size the declared caps imply (`GST_ROUND_UP_4(width * bpp) * height`). Today this fails for every unaligned width — `RGB` 810x1080 wraps 2624400 where the caps demand 2626560
  - Assert row fidelity for the reconcilable case: each row's first `width * bpp` bytes of the wrapped buffer equal the corresponding source row, so padding is appended per row and no pixel is shifted
  - Assert the caps derivation is unchanged while the buffer is fixed: the first-`caps=` clause the launch string yields (same regex as `create_buffer`, `caps=([^!]+)`) is still `video/x-raw,format=RGB`, and the frame dict handed in is unmodified — the padding is applied to the BUFFER, never to `frame_data['data']`
  - Assert end-to-end against REAL GStreamer, comparing PIXELS not status: drive `run_pipeline` with a launch string ending `videoconvert ! jpegenc idct-method=2 quality=100 ! filesink location=<tmp>`, decode the written JPEG, and assert per-channel mean and per-pixel correlation against the source frame (correlation ≈ 1.0, mean within JPEG quantization). On unfixed code an unaligned width yields mean `0.00/0.00/0.00` and correlation ≈ 0 — the local reproduction measured exactly that
  - Assert the SILENCE half (bugfix.md 1.7): with `status_sink=None`, a buffer/caps mismatch must NOT let `run_pipeline` return normally over a blank file. Today `acceptable_messages` is `[ERROR, EOS, TAG]`, the `videoconvert` WARNING is dropped, and `run_pipeline` returns its normal dict — assert the post-fix behavior (a raised, deterministic error from the wrapping site naming the caps, the dimensions, `len(data)` and the expected size) so this half of the defect is covered by the property, not just described
  - Assert the irreconcilable case raises: a buffer whose size is neither the tight nor the padded size (e.g. truncated by one row) fails deterministically BEFORE the pipeline goes to PLAYING
  - Assert the stride rule is GStreamer's, not ours (Requirement 2.10): for every format the fix claims to know (`RGB`, `BGR`, `RGBA`, `BGRA`, `GRAY8`) and a generated range of widths/heights, the helper's expected stride and size equal `GstVideo.VideoInfo.new_from_caps(...).stride[0]` and `.size`. This anchor passes both before and after the fix and is what keeps the format table honest
  - Cover the second wrapping site (bugfix.md 1.8): a produced frame of unaligned width fed through `python_bridge`'s fed `appsrc` with caps from `_fed_frame_caps` is wrapped to the caps-implied size. Drive `_fed_frame_caps` directly plus the wrap, rather than standing up a subprocess bridge, if a full bridged run is too heavy for a property test
  - **ESTABLISH, do not assert, the deployed-workflow exposure (bugfix.md 1.12, UNVERIFIED ON DEVICE)**: in the container, drive `pipeline_executor._frame_caps` for a static-camera frame and `_point_appsrc_at_frame_feed` over a compiled document, then wrap that frame through `create_buffer` with the resulting caps, and RECORD whether the Frame_Feed path reproduces the same short buffer. Write the finding into the test file as a comment and into the task's completion note. If it does NOT reproduce, say so plainly and narrow bugfix.md 1.12 rather than leaving an unsupported claim
  - Pin the measured device evidence as concrete cases: `invalid buffer size 2624400 < 2626560` for 810x1080 and `1187328 < 1187840` for 773x512 (both verbatim from the JP6 Orin's `gst-debug.log`), the preview mean `0.00/0.00/0.00` for `bus.jpg`, and the capture mean `61.02/61.05/61.04` vs source `117.34/115.61/118.34` with correlation `-0.088`
  - Run with the container command above, suite path `test/backend-test/gstreamer/test_property_appsrc_frame_stride.py`, in its own process
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the defect exists)
  - Document counterexamples found (expected: wrapped size 2624400 vs required 2626560 for `RGB` 810x1080; rendered mean `0.00/0.00/0.00`; no exception and no bus signal reaching the caller; the irreconcilable case also silent)
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.12, 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.10, 2.11_

- [x] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Aligned widths byte-identical and copy-free, Bayer untouched, bus handling and the store contract unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record the actual outputs, then assert those recorded outputs
  - New file: `test/backend-test/gstreamer/test_property_appsrc_frame_stride_preservation.py`
  - Observe on UNFIXED code and record: for every ALIGNED generated case (`width * bpp % 4 == 0`, including 768x576 and 1280x720), `create_buffer`'s wrapped size, the extracted buffer bytes, and the rendered JPEG's per-channel mean and correlation through the real `videoconvert ! jpegenc ! filesink` chain. Assert unchanged after the fix, and assert the no-copy guarantee at the helper level — the helper returns the IDENTICAL object (`result is data`), not an equal copy (Requirements 2.2, 3.1)
  - Observe on UNFIXED code and record: `video/x-bayer` behavior end to end — the wrapped buffer bytes AND the bus messages for the `bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! jpegenc ! filesink` chain, for aligned and unaligned widths, at 1 byte per pixel. Assert byte-identical buffers and identical message sequences after the fix. **This is the guard that the physical Basler path is provably untouched** (Requirement 3.4); note in the file what was found about Bayer striding (`bayer2rgb` unit size `GST_ROUND_UP_4(width) * height`, observed 82416 for 813x101 and 876960 for 810x1080) so a future reader does not re-derive it
  - Observe on UNFIXED code and record: the pass-through cases that must never raise — caps that are not `video/x-raw`, a `video/x-raw` format outside the helper's table, caps missing `width` or `height`, and a non-positive height. Assert identical buffers and no exception (Requirement 3.10)
  - Observe on UNFIXED code and record: `RGBA`/`BGRA` no-ops (4 bytes per pixel, always aligned) and `GRAY8` at `width % 4 == 0` versus `width % 4 != 0`. Assert the aligned ones are byte-identical and the unaligned `GRAY8` gains exactly the padding `GstVideo.VideoInfo` reports (Requirement 3.5)
  - Observe on UNFIXED code and record: `run_pipeline`'s bus behavior on the `status_sink=None` path — the ERROR capture and raise-after-`loop.run()` shape, the EOS/TAG handling, `parse_msg` tag values, the `PIPELINE_TIMEOUT_SEC` watchdog — and, with a `status_sink` supplied, that WARNING and STATE_CHANGED still reach the sink and a sink exception is still swallowed. Assert unchanged; `acceptable_messages` for `status_sink=None` stays `[ERROR, EOS, TAG]` (Requirement 3.6)
  - Observe on UNFIXED code and record: `create_buffer`'s unchanged contract — the first-`caps=` regex result, the `,width={wd} , height={ht}` append, `block=True`, `format=Gst.Format.TIME`, and the `(source, buffer)` return shape (Requirement 3.7)
  - Observe on UNFIXED code and record: `StaticImageStore.get_frame()` for arbitrary pinned images — `pixel_format == "RGB"`, `len(data) == 3 * width * height` (TIGHT, unpadded), EXIF-transposed dimensions, byte-identical across repeated grabs — and `pipeline_executor._frame_caps` for an `"RGB"`-tagged frame, a `bayer:bggr` frame, and an untagged frame. Assert unchanged (Requirements 3.2, 3.3)
  - Observe on UNFIXED code and record: `dda_frames.to_array` for padded and tight inputs (including its too-short error message), `to_bytes`' no-padding output, and `_invoke_process_frame`'s stride-preserving write-back for both padded and tight inputs. Assert unchanged (Requirement 3.8)
  - Assert the previous spec's already-deployed surface is untouched: `default_camera_configurations.json` resolves the static camera to `capsfilter caps=video/x-raw,format=RGB ! videoconvert`, `db_backfill`'s static-camera migration still exists and is still idempotent, and neither file is modified by this spec (Requirement 3.9)
  - Note that the existing suites are themselves preservation coverage and must keep passing untouched: `test/backend-test/static_image_camera/*` (especially `test_workflow_feed.py`, whose 6x4 pinned image is itself an UNALIGNED case — `6 * 3 = 18` — and whose `args == (expected_frame(...),)` assertion proves the frame dict handed to `run_pipeline` stays tight-packed), the previous spec's `test_property_static_camera_pixel_format*.py`, `test/backend-test/gstreamer/*`, and the Custom_Python bridge suites (Requirement 3.13)
  - Run with the container command above, suite path `test/backend-test/gstreamer/test_property_appsrc_frame_stride_preservation.py`, in its own process
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 1.9, 1.10, 1.11, 1.13, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13_

- [x] 3. Fix for the unreconciled appsrc frame stride

  - [x] 3.1 Add the shared stride reconciliation helper
    - New module `src/backend/gstreamer/frame_stride.py` (pure Python, NO `gi` import, so it stays cheap to import and testable outside GStreamer)
    - `BYTES_PER_PIXEL = {"RGB": 3, "BGR": 3, "RGBA": 4, "BGRA": 4, "GRAY8": 1}` — the formats the code paths actually declare. Any other format is UNKNOWN and passes through untouched (Requirement 3.10). Task 1 cross-checks every key against `GstVideo.VideoInfo`, so an error in this table fails a test rather than a device preview
    - `expected_stride(frame_format, width)` returns `GST_ROUND_UP_4(width * BYTES_PER_PIXEL[frame_format])` — `((row_bytes + 3) // 4) * 4`; `expected_size(frame_format, width, height)` returns `expected_stride * height`
    - `reconcile_to_caps_stride(data, caps_string, width=None, height=None, strict=True)` is the single entry point. It parses the media type and `format=` out of `caps_string` (tolerating the trailing whitespace and ` , ` spacing `create_buffer` produces), falls back to `width=`/`height=` in the caps when they are not passed, and then:
      - media type is not `video/x-raw`, format unknown or absent, width/height missing or non-positive -> **return `data` unchanged, never raise**
      - `len(data) == expected_size` -> **return the IDENTICAL object** (`return data`), no copy, no allocation — this is the no-op that keeps every aligned case byte-identical (Requirements 2.2, 3.1)
      - `len(data) == tight_size` and `tight_size != expected_size` -> return a new `bytes` with each row's `row_bytes` copied at `expected_stride` and the pad bytes zeroed
      - anything else -> if `strict`, raise `PipelineExecutionException` (the exception `gst_pipeline` already raises, from `exceptions.api.gst_pipeline_exception`) naming the caps string, `width`x`height`, `len(data)`, and `expected_size`; if not `strict`, return `data` unchanged (Requirements 2.4, 2.9)
    - `video/x-bayer` falls into the first branch by construction — it is not `video/x-raw` — so the Bayer path is untouched without a special case. Say so in the docstring, with the reason (`video/x-bayer` is not a `GstVideoFormat`, so `gst_video_frame_map_id`'s check never applies), so nobody "fixes" it later by adding Bayer to the table (Requirement 3.4)
    - Do NOT change `create_buffer`'s caps derivation, the store's frame contract, `_frame_caps`, `_point_appsrc_at_frame_feed`, any launch string, or `run_pipeline`'s bus handling
    - _Bug_Condition: isBugCondition(X) - isPackedRawVideo(X.declaredCaps) AND LENGTH(X.data) <> expectedSize(X)_
    - _Expected_Behavior: Property 1 in bugfix.md - SIZE(wrapFrame'(X)) = expectedSize(X), rows preserved, irreconcilable input raises_
    - _Preservation: Property 2 in bugfix.md - identical object for aligned input, Bayer and unknown caps pass through, bus handling unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.10, 3.4, 3.5, 3.7, 3.10_

  - [x] 3.2 Apply the helper at the three wrapping sites
    - `src/backend/gstreamer/gst_pipeline.py` `create_buffer` (line 76): build the caps string exactly as today, then wrap `reconcile_to_caps_stride(data, caps_string, wd, ht, strict=True)` instead of `data`. Everything else in the function — the regex, the caps append, `block`, `format`, the return shape — stays as is (Requirement 3.7). This single call covers preview, capture, the classic workflow path, AND the deployed-workflow Frame_Feed, since all four reach `create_buffer`
    - `src/backend/workflow_engine/python_bridge.py` fed `appsrc` (line 1897): reconcile `frame_data["data"]` against the caps `_fed_frame_caps` just produced, `strict=True`. Compute the caps string once and reuse it for both `set_property("caps", ...)` and the reconciliation so the two cannot disagree
    - `src/backend/workflow_engine/python_bridge.py` bridge output (line 1844): reconcile `out_bytes` against the NEGOTIATED caps already extracted in the pump (`frame_format`, `width`, `height` from lines 1811-1817), `strict=False`. No new failure mode mid-stream (Requirement 2.9); when `_invoke_process_frame` did its job this is a no-op returning the same object
    - Keep the frame dicts immutable: the padding applies to the bytes handed to `Gst.Buffer.new_wrapped`, NEVER to `frame_data['data']` — `test_workflow_feed.py`'s `args == (expected_frame(...),)` assertion depends on the frame dict staying the store's tight frame (Requirement 3.3)
    - Do NOT touch `run_pipeline`'s `acceptable_messages`, `_notify_sink`, the watchdog, or `parse_msg`; the loudness comes from 3.1's strict validation (Requirements 2.5, 3.6)
    - Do NOT touch `_invoke_process_frame`, `dda_frames.to_array`/`to_bytes`, the store, `default_camera_configurations.json`, or `db_backfill.py` (Requirements 3.2, 3.8, 3.9)
    - _Bug_Condition: isBugCondition(X) at all three wrapping sites (bugfix.md 1.1, 1.8, 1.9)_
    - _Expected_Behavior: Property 1 in bugfix.md, plus Requirement 2.9's non-strict mode for the mid-stream site_
    - _Preservation: Property 2 in bugfix.md - the frame dict, the caps strings, the launch strings and the bus behavior all unchanged_
    - _Requirements: 2.1, 2.3, 2.6, 2.7, 2.8, 2.9, 3.2, 3.3, 3.6, 3.7, 3.8, 3.9, 3.11_

  - [x] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Wrapped buffer size agrees with the stride its declared caps imply
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass, the fix is confirmed
    - Run `test/backend-test/gstreamer/test_property_appsrc_frame_stride.py` with the container command above
    - **EXPECTED OUTCOME**: Tests PASS (confirms the defect is fixed) — in particular the rendered-pixel assertions for `RGB` 810x1080 and 773x512 now match the source instead of returning mean `0.00/0.00/0.00`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.10, 2.11_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Aligned widths byte-identical and copy-free, Bayer untouched, bus handling and the store contract unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run `test/backend-test/gstreamer/test_property_appsrc_frame_stride_preservation.py` with the container command above
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Also re-run the neighbours that document the untouched surface, each in its OWN process: `test/backend-test/static_image_camera/test_workflow_feed.py`, `test/backend-test/static_image_camera/test_property_static_camera_pixel_format.py`, `test/backend-test/static_image_camera/test_property_static_camera_pixel_format_preservation.py`, `test/backend-test/gstreamer/`
    - Confirm the zero-diff constraints hold: `git diff --stat` shows changes only in `src/backend/gstreamer/frame_stride.py` (new), `src/backend/gstreamer/gst_pipeline.py`, `src/backend/workflow_engine/python_bridge.py`, and the two new test files

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the device backend suite in the flask-app container with the command above, **one suite path per invocation** (see the process-split note: `static_image_camera` stubs `utils.server_setup`, and `camera_sync/test_property_reconnect_catch_up.py::test_reconnect_publishes_complete_current_state` hangs when it follows `utils` in one process)
  - `LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64` is what clears the `libtritonserver.so` import failures; it is environmental, not a code fix
  - **Establish the pre-existing failure set rather than trusting it**: `git archive HEAD` into a scratch directory, run the same suites there, and diff the FAILED id lists. **Never `git stash`** — it would disturb the untracked test files this spec adds. Expect **16** known ids to fail in BOTH trees and to be ignored: 4 streaming e2e, `restart-component`, `stop-component` (x2), `test_get_station_logo_returns_logo`, `test_get_image_source_by_id`, `test_connect_camera_endpoint`, captured-images invalid-path (x2), streams-api (x2), workflows-api load-input-image (x2). Anything else must be green
  - Run the security preservation guards BEFORE the hand-off, since a stale baseline fails the build gate AFTER the ~1h compile:
    `python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`
  - None of the three source files this spec changes is preservation-tracked (the tracked set is `src/docker-compose.yaml`, the backend / frontend / edgemlsdk Dockerfiles, `src/backend/requirements.txt`, the recipe variants, and `station_install/setup_station.sh`), so no rebaseline is expected. If a guard fails it is almost certainly an unbaselined `edge-cv-portal/infrastructure/cdk.out` from a portal deploy — move it aside per `.kiro/steering/builds.md` rather than editing baselines
  - Ensure all tests pass; ask the user if questions arise
  - _Requirements: all_

- [x] 5. Build hand-off and post-build on-device verification
  - **THIS TASK BUILDS NOTHING AND DEPLOYS NOTHING.** A component build takes ~100 minutes, corrupts other builds if run concurrently, and is the user's to drive. This task hands off, then verifies what the user's build produced
  - **Components and versions this supersedes**, both built from commit `318d021`: `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.66** on the JP6 AGX Orin (thing `ryanorinagxdevkithomelabjp622`) and `aws.edgeml.dda.LocalServer.arm64JP7` **1.0.25** on `jetson-thor1` -> next patch each (`bash build-custom.sh aws.edgeml.dda.LocalServer.arm64JP6 NEXT_PATCH`, or `TARGETS="6" ./run_jp_builds.sh`; same for `7`), then a deployment revision to reach each device
  - **Pre-flight gates from `.kiro/steering/builds.md`, in order**:
    - (a) `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` must BOTH return nothing. If either returns a process, wait — never start a second build
    - (b) Move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`) so the cdk.out drift guard does not fail the security gate after the ~1h compile
    - (c) Do NOT run a portal deploy (`deploy-portal.sh` / `deploy-infrastructure.sh` / `deploy-frontend.sh`) while a build runs — it regenerates `cdk.out` mid-build and fails the gate. Sequence: portal deploy fully finishes -> move `cdk.out` aside -> start the build
    - (d) Confirm the security preservation guards are green BEFORE starting (task 4 runs them) — never assume it
    - (e) ONE target at a time. `gdk-config.json` holds a single component; swap it per target and restore it when done. Capture output to `.gdk_build_jp6.log` / `.gdk_build_jp7.log`
  - **Post-build on-device verification — the JP6 AGX Orin** (SSH: `SSHPASS=lookout sshpass -e ssh -p 9995 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null aws@ryan.120v.ac`; Image_Source `6ochjlqf`, provisioned with the RGB chain; test assets at `/home/aws/github/DefectDetectionApplication/test-assets/images` — `bus.jpg` 810x1080, `dog.jpg` 768x576, `eagle.jpg` 773x512, `horses.jpg` 773x512, `zidane.jpg` 1280x720, three of which are unaligned):
    - (a) Confirm the deployed `arm64JP6` version is the new patch and the backend container comes up healthy and STAYS healthy (no restart loop)
    - (b) **Pin an UNALIGNED-width image** — `bus.jpg` (810x1080) or `eagle.jpg` (773x512) — then preview it and **measure per-region mean RGB against the source as ground truth**. Divide the image into regions (e.g. a 3x3 grid), compute per-channel means for both source and preview, and require them to agree within JPEG quantization. **An HTTP 200 check proves nothing — it is exactly what hid this bug.** The pre-fix numbers to beat: preview mean `0.00/0.00/0.00`
    - (c) Capture the same unaligned image and measure the same way, plus per-pixel correlation against the source (require ≈ 1.0). The pre-fix numbers: mean `61.02/61.05/61.04` vs source `117.34/115.61/118.34`, correlation `-0.088`
    - (d) Confirm the component's `gst-debug.log` carries NO `invalid buffer size` line and NO `invalid video buffer received` warning for those runs
    - (e) **Aligned-width regression**: pin `zidane.jpg` (1280x720, currently pinned on this device) and confirm preview and capture still match the source to the pre-fix accuracy (max error 0.01/255) — the fix must be a no-op here
    - (f) Repeat (b) with the second unaligned width so both `810` and `773` are covered on hardware, as they were during reproduction
    - (g) If a workflow bound to the static camera can be deployed, run it and confirm the model sees unmangled pixels — this is the Frame_Feed exposure that bugfix.md 1.12 marks UNVERIFIED ON DEVICE. Report the outcome either way
  - **Post-build on-device verification — `jetson-thor1`** (Image_Source `920nufdy`, provisioned with the RGB chain; `/aws_dda/yolotest` for the folder-backed workflow):
    - (h) Confirm the deployed `arm64JP7` version is the new patch and the backend container is healthy
    - (i) **Physical Basler preview, to prove the Bayer path is untouched**: preview a physical camera source (`28183exv` / `g6zrsox3`, or `o70qz7ci` / `563tiauk`) and confirm it still provisions and executes its BGGR Bayer chain and still renders correctly. Confirm the log shows no new validation error from the wrapping site
    - (j) Repeat (b), (c) and (e) on this device for one unaligned and one aligned width, so both JetPack lines are covered
    - (k) Regression spot-check on a folder-backed workflow (`pagb7vj8` "yolotest", model `model-yolo-test-jetson-xavier-jp7`): a manual run still returns its detection result, still persists, and still counts in the summary. **A folder-backed workflow CONSUMES its oldest source image on a successful run**, so restore `/aws_dda/yolotest` afterwards if the images are needed again
  - **STOP AND ASK THE USER BEFORE ANY STEP THAT MUTATES LIVE DEVICE STATE.** Both devices are real Jetsons in use. Re-pinning or replacing a pinned image, creating or deleting Image_Sources, deploying a workflow, and running folder-backed workflows (which consume source images) all change live state. Get explicit confirmation first; the read-only checks — (a), (d), (h), and reading the current pinned image and its metadata — are the default
  - _Requirements: 2.1, 2.4, 2.6, 2.7, 2.8, 3.1, 3.2, 3.4, 3.11, 3.12_

## Notes

- **Test-first ordering is mandatory.** Task 1 must FAIL and task 2 must PASS on UNFIXED code before
  any of task 3 is written. Do not create `src/backend/gstreamer/frame_stride.py` or touch
  `gst_pipeline.py` / `python_bridge.py` until 1 and 2 are written, run, and documented.
- **Property references**: Property 1 (Bug Condition / Fix Checking) validates Requirements 2.1, 2.2,
  2.3, 2.4, 2.6, 2.7, 2.8, 2.10, 2.11; Property 2 (Preservation) validates 3.1 through 3.13.
  Requirement 2.5 (loudness by up-front validation, not by promoting bus warnings) is enforced by
  3.1's strict mode plus Property 2's unchanged-`acceptable_messages` assertion; Requirement 2.9
  (mid-stream site never raises) by 3.2's `strict=False` at the bridge output.
- **Confirmed root cause (code read, device-measured, and locally reproduced)**: `create_buffer`
  (`src/backend/gstreamer/gst_pipeline.py` line 76) wraps `frame_data['data']` with no reference to
  the stride its own caps imply; `run_pipeline`'s `on_message` (lines 113-121) drops WARNING unless a
  `status_sink` was supplied, and every Pipeline_Configuration caller supplies `None`. On device:
  `ERROR default video-frame.c:181:gst_video_frame_map_id: invalid buffer size 2624400 < 2626560`
  then `WARN videofilter gstvideofilter.c:296:...<videoconvert3> warning: invalid video buffer
  received`, HTTP 200, black image. Locally in `flask-app:latest` (GStreamer 1.20.3):
  `GstVideo.VideoInfo.new_from_caps` reports stride 2432 / size 2626560 for `RGB` 810x1080 and stride
  2320 / size 1187840 for `RGB` 773x512 — the device's numbers exactly — and the tight-vs-padded
  buffer pair through `appsrc ! videoconvert ! jpegenc ! filesink` produced mean `0.00/0.00/0.00`
  (WARNING + EOS) versus mean matching the source (clean EOS, max error 1.0, corr 1.000).
- **Fix-direction decision, with the rejected alternative recorded**: padding to
  `GST_ROUND_UP_4(width * bpp)` was chosen over attaching a `GstVideoMeta` with the true stride. Both
  were run locally and both produced correct pixels; the meta route is copy-free but depends on every
  element in the chain honoring the meta, and these chains include this repo's own `emltriton`
  plugin plus `videocrop`, `jpegenc`, `bayer2rgb` and the Nvidia converters. Padding produces the
  exact layout the caps already promise, so nothing downstream needs to change. If the copy ever
  shows up in a profile, the meta route is the documented next step — and it should be re-tested
  against `emltriton` specifically.
- **Loudness decision**: up-front validation in the wrapping helper, not promoting bus WARNINGs to
  fatal. A bus warning arrives asynchronously, cannot be attributed to a frame, and cannot reconcile
  anything; and `acceptable_messages` on the `status_sink=None` path is load-bearing for existing
  flows that emit benign warnings. The strict check is deterministic, fires before PLAYING, and names
  the caps, the dimensions, the bytes received and the bytes expected.
- **Bayer scoping, answered from evidence rather than assumption**: GStreamer DOES stride
  `video/x-bayer` at `GST_ROUND_UP_4(width)` — `bayer2rgb`'s declared unit size was observed as
  `GST_ROUND_UP_4(width) * height` (82416 for 813x101, 876960 for 810x1080) — so a tight unaligned
  Bayer buffer is also technically inconsistent with its caps. It is nevertheless left ALONE:
  `video/x-bayer` is not a `GstVideoFormat`, so the `gst_video_frame_map_id` check behind this defect
  never applies; the mismatch was observed as a FATAL ERROR in a `videoconvert ! fakesink` chain and
  as a pass-through in the `bayer2rgb ! ... ! jpegenc ! filesink` chain the device actually runs, i.e.
  loud or visible, never silently black; physical frames arrive from Aravis at the camera's own
  declared width with no unaligned width in evidence; and nine physical camera configurations across
  two devices depend on that path. Extending the fix into Bayer on inference would trade a measured
  defect for an unmeasured risk.
- **Deliberately out of scope**: changing the store's tightly packed RGB contract (the previous spec's,
  deployed and verified, and depended on by `_frame_caps`); making `create_buffer` derive caps from the
  frame's `pixel_format` tag instead of the launch string (a separate, larger change affecting every
  physical camera's hot path); adding `video/x-bayer` to the helper's table; changing
  `acceptable_messages`, `default_camera_configurations.json`, or `db_backfill.py`; and standing up a
  frontend test framework — no frontend file is touched.
- **Verified NOT defects** (preservation clauses, not fixes): the store's tight packed-RGB output is a
  reasonable documented contract and stays; `_invoke_process_frame`'s stride-preserving write-back and
  `dda_frames.to_array`'s padding tolerance are already correct and are the precedent this fix follows;
  `_frame_caps`' `"RGB"` tag handling is correct; the `default`/`default` BGGR chain remains correct for
  unknown physical vendors.
- **Risks**: (1) A full-frame copy is added for unaligned widths — bounded to one copy per fed frame on
  a path that runs once per preview/capture/run, and provably zero for aligned widths because the helper
  returns the identical object (task 2 asserts identity, not equality). (2) The strict validation
  introduces a new failure mode where there was silence; it fires only when a buffer matches neither the
  tight nor the padded size, which no working path produces today, and task 2 pins every pass-through
  case that must NOT raise. (3) The format table could drift from GStreamer's rules — task 1
  cross-checks every entry against `GstVideo.VideoInfo`, so drift fails a test. (4) The Frame_Feed
  exposure is code-read, not device-measured; task 1 establishes it in the container and task 5(g)
  checks it on hardware if a workflow can be deployed, and bugfix.md 1.12 says so plainly rather than
  overclaiming.
