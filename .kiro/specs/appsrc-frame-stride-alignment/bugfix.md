# Bugfix Requirements Document

## Introduction

**A frame whose row size is not a multiple of 4 bytes is handed to `appsrc` with caps that imply a
larger, row-padded buffer, and the pipeline answers HTTP 200 over a black or scrambled image.**

Found during post-deploy verification of `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.66** on a JP6
AGX Orin (thing `ryanorinagxdevkithomelabjp622`), built from commit `318d021`. A static-camera
preview returns **HTTP 200 with an all-black image** and a capture returns **HTTP 200 with a
grey-cast image** whenever the pinned image's width makes `width * 3` a non-multiple of 4. Three of
the five shipped test assets in `test-assets/images` trip it.

Two halves, both required:

1. **The defect.** Every site that wraps raw frame bytes for an `appsrc` calls
   `Gst.Buffer.new_wrapped(data)` and declares caps derived from the frame's dimensions, without
   reconciling the two. GStreamer rounds a packed `video/x-raw` row stride up to a 4-byte multiple,
   so for `RGB` 810x1080 it expects `2432 * 1080 = 2626560` bytes and receives the store's tightly
   packed `810 * 3 * 1080 = 2624400`. `gst_video_frame_map_id` refuses the map and `videoconvert`
   drops the frame.
2. **The silence.** `run_pipeline`'s `on_message` treats `acceptable_messages` as
   `[ERROR, EOS, TAG]` and appends `WARNING` **only when `status_sink is not None`**. Every
   Pipeline_Configuration caller — preview, capture, classic workflow — passes `status_sink=None`,
   so the `videoconvert` warning is dropped, the pipeline reaches EOS, `run_pipeline` returns
   normally, and the endpoint answers 200 over whatever `filesink` wrote. An HTTP-200 check cannot
   tell a correct preview from a black one, which is exactly why this survived the previous
   verification pass.

**Severity framing, stated honestly.** This is not a revert of
`static-camera-pixel-format-and-detection-results`. Before that spec's RGB fix, an unaligned width
produced a Bayer-mangled but VISIBLE image; `video/x-bayer` is not a `GstVideoFormat`, so the
`gst_video_frame_map_id` size check never applied to it. That spec's fix is correct in what it
changed — for aligned widths the pixels are byte-accurate on device (max error 0.01/255) — but for
unaligned widths it converted one visible-wrong into a differently-wrong. This spec finishes that
job by making the wrapped buffer agree with the caps that were, correctly, made truthful.

**Verified locally as well as on hardware.** Every number below was independently reproduced in
`flask-app:latest` (GStreamer 1.20.3, `gi` / `Gst` / `GstVideo` / `Aravis` all importable), which
means the exploration and preservation properties can exercise REAL GStreamer rather than only
launch-string shapes. `GstVideo.VideoInfo.new_from_caps` returns exactly the device's numbers
(`RGB` 810x1080 -> stride 2432, size 2626560; `RGB` 773x512 -> stride 2320, size 1187840), a tight
buffer through `appsrc ! videoconvert ! jpegenc ! filesink` produced a WARNING, a normal EOS, and a
JPEG with mean R/G/B `0.00/0.00/0.00`, and the same buffer padded to the expected stride produced a
clean EOS and mean `127.50/127.69/127.16` against a source mean of `127.49/127.68/127.16`
(max error 1.0 at `quality=100`, correlation 1.000).

## Bug Analysis

### Current Behavior (Defect)

Measured on the JP6 AGX Orin (`LocalServer.arm64JP6` 1.0.66, commit `318d021`) and confirmed against
the shipped source and a local GStreamer 1.20.3 reproduction.

1.1 WHEN `GstPipelineManager.create_buffer` wraps a frame (`src/backend/gstreamer/gst_pipeline.py`
line 76, `return source, Gst.Buffer.new_wrapped(data)`) THEN the system declares caps built from the
launch string's FIRST `caps=` clause — `re.search(r'caps=([^!]+)')`, line 69 — with
`,width={wd} , height={ht}` appended, and pushes the buffer WITHOUT reconciling its byte count
against the row stride those caps imply; nothing in the function inspects `len(data)` at all

1.2 WHEN the declared caps are packed `video/x-raw` and `width * bytes_per_pixel` is not a multiple
of 4 THEN GStreamer rounds the row stride up to a 4-byte multiple and expects `stride * height`
bytes, so the tightly packed frame is short: `RGB` 810x1080 -> stride 2432, expected 2626560,
received 2624400; `RGB` 773x512 -> stride 2320, expected 1187840, received 1187328 (locally
confirmed with `GstVideo.VideoInfo.new_from_caps`, and matching the device's `gst-debug.log`
verbatim: `ERROR default video-frame.c:181:gst_video_frame_map_id: invalid buffer size 2624400 <
2626560` followed by `WARN videofilter gstvideofilter.c:296:gst_video_filter_transform:<videoconvert3>
warning: invalid video buffer received`)

1.3 WHEN a preview runs on such a frame THEN the system returns **HTTP 200 with an all-black image**
— measured preview mean R/G/B `0.00/0.00/0.00` with `bus.jpg` (810x1080) pinned, reproduced twice,
and predicted-then-confirmed for `eagle.jpg` (773x512): the expected log line
`invalid buffer size 1187328 < 1187840` was written down before the run and printed exactly

1.4 WHEN a capture runs on such a frame THEN the system returns **HTTP 200 with a grey-cast image**
— measured mean `R=61.02 G=61.05 B=61.04` against a source mean of `R=117.34 G=115.61 B=118.34`,
per-pixel correlation `-0.088`; `R ≈ G ≈ B` with no correlation to the source is the
grey-cast/scrambled signature, not a dim-but-correct image

1.5 WHEN the frame's row size IS a multiple of 4 THEN the same code path is byte-accurate, which is
what makes the defect width-dependent and easy to miss — measured matrix (pinned image -> preview):

| pinned | WxH | width*3 | gst expects | 4-byte aligned | result |
|---|---|---|---|---|---|
| bus.jpg | 810x1080 | 2430 | 2626560 | NO | BLACK, mean 0.00/0.00/0.00 |
| eagle.jpg | 773x512 | 2319 | 1187840 | NO | BLACK, predicted then confirmed |
| dog.jpg | 768x576 | 2304 | 1327104 | yes | correct, max error 0.01/255 |
| zidane.jpg | 1280x720 | 3840 | 2764800 | yes | correct, max error 0.01/255 |

1.6 WHEN the shipped test assets are used as inputs THEN THREE OF FIVE trip the defect —
`bus.jpg` 810x1080, `eagle.jpg` 773x512, and `horses.jpg` 773x512 are unaligned; only `dog.jpg`
768x576 and `zidane.jpg` 1280x720 are aligned — so the failure is the common case for this feature's
own test material, not an exotic edge

1.7 WHEN the mismatch occurs THEN the system NEVER SURFACES IT: `run_pipeline`'s `on_message`
(`src/backend/gstreamer/gst_pipeline.py` lines 113-121) sets
`acceptable_messages = [Gst.MessageType.ERROR, Gst.MessageType.EOS, Gst.MessageType.TAG]` and
appends `Gst.MessageType.WARNING` only `if status_sink is not None`; every Pipeline_Configuration
caller (preview, capture, classic workflow) passes `status_sink=None`, so the `videoconvert` WARNING
is dropped by the `message.type not in acceptable_messages` early return, the pipeline still reaches
EOS, `run_pipeline` returns its normal dict, and the endpoint answers 200 over the blank file —
locally reproduced end to end: a tight `RGB` 810x108 buffer through
`appsrc ! videoconvert ! jpegenc idct-method=2 quality=100 ! filesink` posted WARNING then EOS and
wrote a JPEG whose mean is `0.00/0.00/0.00`, while the same bytes padded to stride 2432 posted EOS
alone and wrote a JPEG matching the source to within JPEG quantization

1.8 WHEN a Custom_Python bridged run feeds a produced frame THEN the same unreconciled wrap happens
at the second site — `src/backend/workflow_engine/python_bridge.py` line 1897,
`fed_buffer = Gst.Buffer.new_wrapped(frame_data["data"])`, with caps from `_fed_frame_caps`
(line 1671) which renders `video/x-raw,format={format},width={width},height={height}` — so a
produced frame of unaligned width has exactly the same exposure

1.9 WHEN a bridge handler's output buffer is pushed (`python_bridge.py` line 1844,
`out_buffer = Gst.Buffer.new_wrapped(out_bytes)`) THEN the system is ALREADY consistent by
construction and is NOT the defect: the caps applied to that `appsrc` are the appsink's negotiated
caps (line 1819, `src_element.set_property("caps", caps)`), and the runner's `_invoke_process_frame`
(lines 589-598) writes pixel rows back into a copy of the input at the input's own stride
(`stride = len(frame) // height`) precisely so "byte length and row padding are preserved so the
appsrc caps stay valid", so it echoes whatever stride arrived — correct if the input was correct,
and inheriting the defect only from an upstream tight buffer or from a raw `handle()`-contract node
that returns a differently sized buffer

1.10 WHEN the codebase already needs to tolerate row padding THEN it already does, on the READ side
only: `dda_frames.to_array` in `python_bridge.HELPERS_SOURCE` (lines 272-306) computes
`stride = len(frame_bytes) // height` and slices `[:, :row_bytes]`, documented as "tolerating row
padding in the frame bytes" — the precedent for reconciling bytes with stride exists; no wrapping
site applies it

1.11 WHEN the producing side is examined THEN it is CORRECT and is not the fault:
`StaticImageStore._decode_frame_locked` (`src/backend/utils/static_image_camera.py` lines 301-322)
returns `{"data": rgb.tobytes(), "width", "height", "pixel_format": "RGB"}` with
`len(data) == 3 * width * height` (documented at line ~233); a tightly packed frame is a reasonable,
documented contract that the previous spec's preservation tests pin and that
`pipeline_executor._frame_caps` depends on for the `"RGB"` tag — the mismatch is created at the
WRAPPING site, by declaring caps whose stride the buffer does not satisfy

1.12 WHEN a deployed workflow feeds the same frame through the Frame_Feed THEN the exposure SHOULD be
identical, because `pipeline_executor._point_appsrc_at_frame_feed`
(`src/backend/workflow_engine/pipeline_executor.py` line 2773) renames the planned feed's element to
`appsrc` and sets `args["caps"]` from `_frame_caps` (line 2821) — `video/x-raw,format=RGB` for a
static-camera frame — and the run then goes through the SAME `run_pipeline` / `create_buffer` pair.
**This is established by reading code and is UNVERIFIED ON DEVICE**: confirming it needs a deployed
workflow bound to the static camera, which was not available during the measurement session. Task 1
establishes it in the container rather than asserting it here

1.13 WHEN the previous RGB fix is compared against this one THEN the history is: with the pre-fix
Bayer chain the same unaligned frame produced a VISIBLE, mangled image, because `video/x-bayer` is
not a `GstVideoFormat` and `gst_video_frame_map_id`'s size check never applies to it — locally
confirmed, a tight `RGB`-bytes-as-`bggr` buffer through
`bayer2rgb ! capsfilter caps=video/x-raw,format=RGBA ! videoconvert ! jpegenc ! filesink` reached
EOS and wrote a full-size image, while the same buffer through a `videoconvert ! fakesink`
termination was rejected outright with a fatal ERROR (`bayer2rgb0: size 82113 is not a multiple of
unit size 82416`). So the pre-fix behavior was chain-dependent and visibly wrong; the post-fix
behavior for unaligned widths is invisibly wrong. Both are defects; only the second returns black

### Expected Behavior (Correct)

2.1 WHEN a frame is wrapped for an `appsrc` whose declared caps are packed `video/x-raw` in a known
format and `width * bytes_per_pixel` is NOT a multiple of 4 THEN the system SHALL reconcile the
buffer with the caps before pushing it, by padding each row to `GST_ROUND_UP_4(width *
bytes_per_pixel)`, so the wrapped buffer's size equals `expectedStride * height` and
`gst_video_frame_map_id` succeeds

2.2 WHEN `width * bytes_per_pixel` IS already a multiple of 4 THEN the reconciliation SHALL be a
no-op that returns the IDENTICAL object with no copy, so every currently working case stays
byte-identical and no allocation is added to the hot path

2.3 WHEN the reconciliation is applied THEN it SHALL live in ONE shared helper used by every fed-frame
wrapping site — `GstPipelineManager.create_buffer` (the classic preview/capture/Frame_Feed path,
proven broken on device) and `python_bridge`'s fed `appsrc` (1.8) — so the two cannot drift

2.4 WHEN a buffer cannot be reconciled with its declared caps at a fed-frame wrapping site — its size
is neither the tight size nor the padded size, so no row layout explains it — THEN the system SHALL
FAIL LOUDLY AND DETERMINISTICALLY before the pipeline starts, raising an error that names the
declared caps, the dimensions, the byte count received, and the byte count expected

2.5 WHEN loudness is implemented THEN it SHALL come from that up-front validation at the wrapping
site and SHALL NOT come from promoting GStreamer bus WARNINGs to fatal: the up-front check is
deterministic, needs no bus-handling change, and cannot mistake a benign warning for a failure (see
3.6 for the behavior that must not change)

2.6 WHEN a preview runs on a pinned image of ANY width THEN the system SHALL return an image whose
per-region mean RGB matches the source within JPEG quantization — specifically, `bus.jpg` 810x1080
SHALL no longer return mean `0.00/0.00/0.00` — and a correct result SHALL be established by
comparing pixels against the source, never by an HTTP 200

2.7 WHEN a capture runs on a pinned image of ANY width THEN the system SHALL return the store's
pixels: per-pixel correlation against the source SHALL be ≈ 1.0, replacing the measured `-0.088`,
and the `R ≈ G ≈ B` grey cast SHALL be gone

2.8 WHEN a deployed workflow runs through the Frame_Feed on an unaligned-width frame THEN the model
SHALL see the same pixels the store decoded, and the fix SHALL cover that path because it shares
`create_buffer` (1.12)

2.9 WHEN a bridge handler's output buffer is pushed (1.9) THEN the system SHALL reconcile it against
the NEGOTIATED caps with the same no-op-when-aligned helper, but SHALL NOT raise on an
irreconcilable size there: that site is mid-stream, its input already arrived with GStreamer's own
padding, and a new mid-stream exception would fail runs that work today

2.10 WHEN the stride rule is implemented THEN it SHALL be verified against GStreamer itself rather
than trusted from memory: a test SHALL cross-check the helper's expected stride and size against
`GstVideo.VideoInfo.new_from_caps` for every format the helper claims to know, in the container
where `gi` is available, so a format table error fails a test instead of a device preview

2.11 WHEN the fix is delivered THEN a property test SHALL generate pinned-image widths and heights
that straddle the `width * 3 % 4 != 0` boundary and assert the wrapped buffer size always equals what
the declared caps imply, with the aligned widths as the preservation half — a generator that produced
only aligned widths would pass vacuously on unfixed code and SHALL be treated as a test defect

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a frame's row size is already 4-byte aligned THEN the system SHALL CONTINUE TO produce
byte-identical results with no added copy — the helper SHALL return the identical buffer object —
and the aligned assets SHALL CONTINUE TO render correctly (`dog.jpg` 768x576, `zidane.jpg` 1280x720,
the latter currently pinned on the JP6 Orin, both at max error 0.01/255)

3.2 WHEN the Static_Image_Camera serves a frame THEN the store SHALL CONTINUE TO emit TIGHTLY PACKED
RGB — `{'data', 'width', 'height', 'pixel_format': 'RGB'}` with `len(data) == 3 * width * height`,
EXIF-transposed, deterministic, `(inode, mtime_ns, size)`-cached — with `_decode_frame_locked`,
`get_frame`, the store lock, the atomic replace, and `StaticImageUnavailableError` unchanged; the
padding happens at the WRAPPING site, never in the store, and `pipeline_executor._frame_caps` depends
on the `"RGB"` tag

3.3 WHEN the Frame_Feed plans and renders caps THEN `_point_appsrc_at_frame_feed` and `_frame_caps`
SHALL CONTINUE TO emit exactly today's strings, and the frame dict handed to `run_pipeline`
SHALL CONTINUE TO be the store's tight frame byte-for-byte — the existing assertions in
`test/backend-test/static_image_camera/test_workflow_feed.py`,
`"appsrc name=appsrc caps=video/x-raw,format=RGB "` and
`args == (expected_frame(pinned_store.image_bytes_for_test),)`, SHALL keep passing untouched

3.4 WHEN a frame declares `video/x-bayer` caps THEN the system SHALL CONTINUE TO behave EXACTLY as
today — no padding, no validation, byte-identical buffers, identical bus messages. Physical Basler
cameras depend on this path (`28183exv`/`g6zrsox3` and `o70qz7ci`/`563tiauk` on `jetson-thor1`; all
7 configurations on the JP6 Orin). What was found: GStreamer strides `video/x-bayer` at
`GST_ROUND_UP_4(width)` too — `bayer2rgb` declares a unit size of `GST_ROUND_UP_4(width) * height`
(observed 82416 for 813x101 and 876960 for 810x1080) — but `video/x-bayer` is NOT a `GstVideoFormat`,
so the `gst_video_frame_map_id` check that produces THIS defect never applies to it, and a mismatch
surfaces as a chain-dependent `GstBaseTransform` unit-size complaint that was observed as a FATAL
ERROR in one chain and as a pass-through in another (1.13). Physical frames arrive from Aravis at the
camera's own declared width, no unaligned physical width is in evidence, and the failure mode there
is loud rather than silent — so the conservative choice is to leave the Bayer path provably untouched
rather than to extend a fix into it on inference

3.5 WHEN a frame declares an inherently aligned format THEN the helper SHALL CONTINUE TO be a no-op
by construction: `RGBA`/`BGRA` are 4 bytes per pixel so every row is aligned (verified:
`RGBA` 810x1080 -> stride 3240 = tight; `BGRA` 773x512 -> stride 3092 = tight), and `GRAY8` is
aligned exactly when `width % 4 == 0` (verified: `GRAY8` 810 -> stride 812, `GRAY8` 812 -> stride
812 = tight). `RGB`/`BGR` are the exposed cases

3.6 WHEN the GStreamer bus is handled THEN `run_pipeline` SHALL CONTINUE TO behave exactly as today
for the `status_sink=None` path — `acceptable_messages` stays `[ERROR, EOS, TAG]`, benign WARNINGs
stay ignored, the ERROR capture / `loop.quit()` / raise-after-`loop.run()` shape stays, the
`PIPELINE_TIMEOUT_SEC` watchdog stays, and TAG parsing via `parse_msg` stays — and the
deployed-workflow observability path (`status_sink is not None` adding WARNING and STATE_CHANGED,
`_notify_sink`'s contained failure) SHALL CONTINUE TO behave exactly as today. Several existing flows
emit benign warnings, so promoting WARNING to fatal would fail working runs; the loudness in 2.4
comes from up-front validation instead

3.7 WHEN `create_buffer` derives caps and configures the source THEN it SHALL CONTINUE TO use the
same first-`caps=` regex, the same `,width={wd} , height={ht}` append, the same `block=True` and
`format=Gst.Format.TIME` properties, the same `Aravis.enable_interface("Fake")` call, and the same
`(source, buffer)` return signature — the fix adds a reconciliation step to the buffer it returns and
changes nothing else about the function's contract

3.8 WHEN a Custom_Python node processes a frame THEN `_invoke_process_frame`'s stride-preserving
write-back (`stride = len(frame) // height`, rows copied into a copy of the input) and
`dda_frames.to_array`'s row-padding tolerance SHALL CONTINUE TO behave exactly as today, including
the `to_array` too-short error message and `to_bytes`' no-padding output

3.9 WHEN the previous spec's already-deployed, already-verified changes are considered THEN the system
SHALL CONTINUE TO leave them untouched: `src/backend/utils/config/default_camera_configurations.json`
(the `AWS-DDA` RGB entry), `src/backend/dao/sqlite_db/db_backfill.py` (the static-camera pipeline
migration), and the store's packed-RGB contract SHALL NOT be modified by this spec, and the RGB
conversion chain SHALL CONTINUE TO resolve to `capsfilter caps=video/x-raw,format=RGB ! videoconvert`

3.10 WHEN the declared caps are not packed `video/x-raw`, name a format the helper does not know,
omit width or height, or carry a non-positive height THEN the system SHALL CONTINUE TO wrap and push
exactly as today — pass-through, no padding, no new exception — so no currently working pipeline can
be failed by an unrecognized caps string

3.11 WHEN preview, capture, and classic-workflow launch strings are built THEN
`GstPipelineBuilder` and `_add_camera_image_source` SHALL CONTINUE TO produce character-for-character
identical strings; this fix touches no launch string and no `capsfilter`

3.12 WHEN the fix is delivered THEN the component SHALL CONTINUE TO build and run on exactly today's
dependencies, schema, and configuration — no third-party dependency added, no database schema change,
no configuration file change, `logging.conf` / the recipes / the Dockerfiles untouched — and it SHALL
CONTINUE TO work on the GStreamer already shipped in the JP6 and JP7 component images (1.20.3 in
`flask-app:latest`, verified)

3.13 WHEN the existing device suites run THEN `test/backend-test/static_image_camera/*` (including
`test_workflow_feed.py` and the previous spec's `test_property_static_camera_pixel_format*.py`),
`test/backend-test/gstreamer/*`, and the Custom_Python bridge suites SHALL CONTINUE TO pass untouched,
apart from the 16 known pre-existing environmental failures

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type WrappedFrame { declaredCaps, data, width, height }
  OUTPUT: boolean

  // A frame is wrapped for an appsrc whose declared caps are packed
  // video/x-raw in a known format, and the byte count does NOT match the
  // buffer size those caps imply. Today no wrapping site checks this.
  RETURN isPackedRawVideo(X.declaredCaps)
         AND X.width > 0 AND X.height > 0
         AND LENGTH(X.data) <> expectedSize(X)
END FUNCTION

FUNCTION isPackedRawVideo(caps)
  INPUT: caps of type CapsString
  OUTPUT: boolean

  // video/x-bayer is deliberately NOT packed raw video here: it is not a
  // GstVideoFormat, the map-time size check never applies to it, and the
  // physical Basler path must stay byte-identical (Requirement 3.4).
  RETURN mediaType(caps) = 'video/x-raw'
         AND format(caps) IN KEYS(BYTES_PER_PIXEL)
END FUNCTION

FUNCTION bytesPerPixel(format)
  OUTPUT: integer

  // Cross-checked against GstVideo.VideoInfo for every key (Req 2.10).
  BYTES_PER_PIXEL := { 'RGB': 3, 'BGR': 3, 'RGBA': 4, 'BGRA': 4, 'GRAY8': 1 }
  RETURN BYTES_PER_PIXEL[format]
END FUNCTION

FUNCTION expectedStride(X)
  OUTPUT: integer

  // GST_ROUND_UP_4 of the tight row size: what GstVideoInfo reports and
  // what gst_video_frame_map_id requires.
  rowBytes := X.width * bytesPerPixel(format(X.declaredCaps))
  RETURN ((rowBytes + 3) DIV 4) * 4
END FUNCTION

FUNCTION expectedSize(X)
  OUTPUT: integer
  RETURN expectedStride(X) * X.height
END FUNCTION

FUNCTION tightSize(X)
  OUTPUT: integer
  RETURN X.width * bytesPerPixel(format(X.declaredCaps)) * X.height
END FUNCTION

FUNCTION isAligned(X)
  OUTPUT: boolean
  RETURN expectedStride(X) = X.width * bytesPerPixel(format(X.declaredCaps))
END FUNCTION
```

Live counterexamples satisfying `isBugCondition`, all measured:

```
X1 = { 'video/x-raw,format=RGB,width=810,height=1080',  2624400 bytes }
     expectedSize = 2626560   -> preview BLACK, mean 0.00/0.00/0.00
X2 = { 'video/x-raw,format=RGB,width=773,height=512',   1187328 bytes }
     expectedSize = 1187840   -> preview BLACK (predicted, then confirmed)
X3 = { 'video/x-raw,format=RGB,width=810,height=1080',  2624400 bytes }  (capture)
     capture mean 61.02/61.05/61.04 vs source 117.34/115.61/118.34, corr -0.088
```

### Property 1: Fix Checking (buffer reconciled with declared caps)

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 2.10, 2.11**

```pascal
// F  = the wrapping sites as they exist today
// F' = the wrapping sites after the fix
FOR ALL X WHERE isBugCondition(X) DO
  buffer := wrapFrame'(X)                      // F'

  // The size the declared caps imply, always.
  ASSERT SIZE(buffer) = expectedSize(X)

  // Reconcilable input: rows are preserved, padding is appended per row.
  IF LENGTH(X.data) = tightSize(X) THEN
    rowBytes := X.width * bytesPerPixel(format(X.declaredCaps))
    FOR row := 0 TO X.height - 1 DO
      ASSERT BYTES(buffer, row * expectedStride(X), rowBytes)
             = BYTES(X.data, row * rowBytes, rowBytes)
    END FOR
    // ... and the pipeline consumes it: no invalid-video-buffer warning,
    // and the rendered pixels match the source (Requirements 2.6, 2.7).
    ASSERT NOT warned(runPipeline'(X), 'invalid video buffer')
    ASSERT meanRGB(rendered(X)) ≈ meanRGB(source(X))
    ASSERT correlation(rendered(X), source(X)) ≈ 1.0
  ELSE
    // Irreconcilable: loud and deterministic, before PLAYING (Req 2.4).
    ASSERT raises(wrapFrame', X)
    ASSERT errorText(wrapFrame', X) MENTIONS X.declaredCaps
       AND MENTIONS LENGTH(X.data) AND MENTIONS expectedSize(X)
  END IF
END FOR

// The stride rule is GStreamer's, not ours (Requirement 2.10).
FOR ALL format IN KEYS(BYTES_PER_PIXEL), FOR ALL width, height > 0 DO
  info := GstVideoInfo('video/x-raw,format={format},width,height')
  ASSERT expectedStride({format, width, height}) = info.stride[0]
  ASSERT expectedSize({format, width, height})   = info.size
END FOR
```

### Property 2: Preservation Checking (everything not in the bug condition)

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.13**

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  // Byte-identical AND copy-free: the same object, not an equal one.
  ASSERT wrapFrame'(X) IS wrapFrame(X)
  ASSERT SIZE(wrapFrame'(X)) = LENGTH(X.data)
END FOR

// The aligned case is the preservation half of the same generator (Req 2.11).
FOR ALL X WHERE isPackedRawVideo(X.declaredCaps) AND isAligned(X)
              AND LENGTH(X.data) = tightSize(X) DO
  ASSERT wrapFrame'(X) IS wrapFrame(X)
  ASSERT meanRGB(rendered'(X)) = meanRGB(rendered(X))
END FOR

// video/x-bayer: untouched, including its bus messages (Req 3.4).
FOR ALL X WHERE mediaType(X.declaredCaps) = 'video/x-bayer' DO
  ASSERT wrapFrame'(X) IS wrapFrame(X)
  ASSERT busMessages(runPipeline'(X)) = busMessages(runPipeline(X))
END FOR

// Unknown format, missing dims, non-video caps: pass-through, no raise (3.10).
FOR ALL X WHERE NOT isPackedRawVideo(X.declaredCaps)
              OR X.width IS NULL OR X.height IS NULL OR X.height <= 0 DO
  ASSERT wrapFrame'(X) IS wrapFrame(X)
  ASSERT NOT raises(wrapFrame', X)
END FOR

// Bus handling on the status_sink=None path is unchanged (Req 3.6).
FOR ALL P WHERE statusSink(P) = NULL DO
  ASSERT acceptableMessages'(P) = [ERROR, EOS, TAG]
  ASSERT outcome'(P) = outcome(P)
END FOR

// The producing side and the caps planners are unchanged (Req 3.2, 3.3).
FOR ALL pinnedImage DO
  ASSERT storeFrame'(pinnedImage) = storeFrame(pinnedImage)
  ASSERT LENGTH(storeFrame'(pinnedImage).data)
         = 3 * width(pinnedImage) * height(pinnedImage)
  ASSERT frameCaps'(storeFrame'(pinnedImage)) = frameCaps(storeFrame(pinnedImage))
END FOR

// The bridge's read and write helpers are unchanged (Req 3.8).
FOR ALL frameBytes, width, height, format DO
  ASSERT toArray'(frameBytes, width, height, format)
       = toArray(frameBytes, width, height, format)
  ASSERT invokeProcessFrame'(...) = invokeProcessFrame(...)
END FOR
```
