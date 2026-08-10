# Custom Node Plugin Runtime Fixes

## Summary

A designer-created custom node ("resize image", plugin
`7878501d-389d-4db5-832c-6ee2429fecb9`, node type `custom.resize_image`)
deployed cleanly but failed at run time on the device, in a cascade of
seven distinct defects spanning the scaffold's generated C skeleton, the
Plugin_Component packaging, the workflow compiler, and the LocalServer
frame feed. Each defect was first live-fixed and verified on hardware,
then fixed at the source so every future custom node inherits the
corrections.

An eighth defect surfaced later — not at run time but at packaging time:
re-packaging a workflow after publishing a new plugin version fails the
Portal "Package workflow for deployment" dialog with a Requirement 10.4
checksum-verification error, because the build promoted every version's
artifact to a single unversioned Plugin_Library key that later builds
overwrote. It is documented and fixed here alongside the run-time
cascade because it shares the same plugin (`custom.resize_image`) and
Plugin_Library build/packaging path; its fix is still in progress (see
Tests and Deployment notes).

## Verification environment

- Device: ryan-orin-nano (Jetson Orin Nano, JetPack 6 / arm64_jp6)
- LocalServer: v1.0.56 (hot-patched in the backend container for
  verification; source fixes in this spec)
- Workflows: `0c7fe31a-a20d-4cf5-a932-67e8b2c4ff68:3` and
  `f81a4c66-39ab-4068-a8b4-77509446e8c8:9`
- Green end-to-end executions with real scene imagery (VLM described the
  actual bench): `1fc5761e...`, `627bdc68...`, `a5a7e484...`,
  `a4ac1c35...`

## Defects and fixes

### 1. Plugin filename / loader-symbol mismatch (plugin silently rejected)

GStreamer derives the loader symbol `gst_plugin_<ident>_get_desc` from
the library FILENAME (dash to underscore, strip `libgst`/`lib`/`gst`
prefix, cut at the first dot). The build promoted the artifact as
`resize-image.so` (sanitized display name) while the scaffold's
`GST_PLUGIN_DEFINE` used the element name token (`customresizeimage`
from the typeId), so the loader looked for
`gst_plugin_resize_image_get_desc`, found nothing, and silently rejected
the library — the run failed with "the run's workflow plugin directories
register no elements".

Fix (`workflow_core/scaffold.py`): new `plugin_ident_for(declaration)`
mirrors the exact filename-to-symbol chain
(`plugin_builds.sanitize_plugin_name` of the displayName, then
GStreamer's `extract_symname`); the C template's `GST_PLUGIN_DEFINE` and
`PACKAGE` now use `${plugin_ident}`, and the meson library target is
`gst${plugin_ident}` so the locally built `libgst<ident>.so` and the
promoted `{plugin}.so` both resolve the same symbol. The element factory
name (what elementChain references) is unchanged.

### 2. Frame_Processing_Hook never shipped

The scaffold's C skeleton imports `frame_processing_hook.py` at run
time, but the Plugin_Component packaging only shipped the `.so` and
`plugin-manifest.json` — the hook stayed behind in plugin-sources. The
element loaded but failed every frame.

Fix (`functions/plugin_components.py`): packaging loads the version's
`plugin/frame_processing_hook.py` from its plugin-sources prefix when
present and ships it beside the `.so` on every architecture (payload,
recipe Artifacts, Install script). Versions without a hook (prebuilt
imports) package exactly as before.

### 3. Hook import path not set up

The C skeleton called `PyImport_ImportModule("frame_processing_hook")`
with no `sys.path` entry for the plugin's install directory, so the
import failed in any host process.

Fix (scaffold C template): the element self-locates its own shared
library via `dladdr` and inserts that directory into `sys.path`
(idempotent via `PySequence_Contains`) before the import.

### 4. appsink/appsrc bridge defects (hangs and a full LocalServer deadlock)

Four defects in the generated bridge:

- **No new-preroll handling**: the FIRST buffer arrives as the appsink
  preroll sample; only `new-preroll` fires for it. Without a handler,
  downstream never prerolls and the pipeline hangs in PREROLLING until
  the run watchdog.
- **No EOS forwarding**: bounded single-frame runs never terminated.
- **No output caps**: the appsrc emitted no caps, so downstream
  misinterpreted the buffer bytes.
- **GIL held across `gst_app_src_push_buffer`**: the push chains into
  the rest of the pipeline and can synchronize with threads that need
  the GIL — in the embedding LocalServer this deadlocked the whole
  process once (recovered via docker restart). Related: after
  `Py_InitializeEx` the initializing thread must `PyEval_SaveThread()`
  or the streaming thread deadlocks in `PyGILState_Ensure`.

Fix (scaffold C template): shared `process_sample` used by both
`new-sample` and `new-preroll` handlers; `eos` forwarded to the appsrc;
input caps copied to the appsrc (`sync_out_caps`, with a marked hook
point for geometry-changing nodes); GIL released before the push;
`we_initialized` + `PyEval_SaveThread()` discipline; appsrc
`format=GST_FORMAT_TIME`. The negotiated frame geometry
(`frame_width`/`frame_height`/`frame_format`) is injected into the
hook's `params` so hooks never guess the incoming layout (documented in
the hook template and README).

### 5. Compiler dangling branch (unfed segment, watchdog timeout)

A GStreamer node wired downstream of an opaque executor-level node
(capture behind bedrock/LLM inference in `f81a4c66...:9`) gained no
stream feeder — the forward stream terminates at the opaque node's
capture sink — and was emitted as a root segment with `from=None` and no
tee. The unfed branch starved the pipeline until the 120s watchdog.

Fix (`workflow_core/compiler/compiler.py`): after stream adjacency is
computed, any GStreamer node with incoming graph connections but no
stream feeder is re-attached to its nearest upstream GStreamer feeder(s)
looking through executor-level nodes (including opaque ones); the feeder
then fans out via tee + queue branches to both the capture sink and the
re-attached branch — exactly the manual rewiring verified on device.
Genuinely unconnected source nodes remain roots.

### 6. Bayer frames mislabeled GRAY8 (image corruption)

The camera is a Basler Bayer BGGR sensor; Bayer is 1 byte/pixel, so the
workflow frame feed's bytes-per-pixel guess labeled the frames GRAY8 and
captures were horizontal-stripe garbage.

Fix (`src/backend/utils/camera_manager.py`,
`src/backend/workflow_engine/pipeline_executor.py`): frames are tagged
with the PFNC-derived pixel format (`gst_pixel_format` map:
`bayer:bggr`, `GRAY8`, `RGB`, `RGBA`, `BGRA`, ...) carried through
`encode_frame`/`decode_frame`; `_frame_caps` honors the tag
(`video/x-bayer,format=...`), and `_point_appsrc_at_frame_feed` injects
`bayer2rgb` after the appsrc for bayer caps.

### 7. Image_Source configuration contract (device-local camera settings)

Contract: a workflow's camera grab MUST respect the Aravis camera
settings configured on the device's local Image_Source for that camera —
not the feed configuration planned into the compiled document. The
planned configuration is only a fallback when no local Image_Source
exists for the camera.

Fix (`pipeline_executor.py`): `_default_camera_config_resolver(session,
camera_id)` resolves the local ImageSource
(`image_source_dao.list_image_source_ids_by_camera` +
`get_image_source`, converted via
`convert_sqlalchemy_object_to_dict`); `_prepare_aravis_frame_feed`
prefers it over the planned feed config. The resolver is injectable for
tests (`camera_config_resolver` ctor arg).

### 8. Plugin_Library promotion key overwrite invalidates older versions' checksums (packaging rejected, Req 10.4)

The Portal "Package workflow for deployment" dialog rejects packaging
with "Package failed — Custom plugin artifact 'resize-image.so' for
architecture 'arm64_jp6' (Custom_Node_Type 'custom.resize_image', plugin
`7878501d-389d-4db5-832c-6ee2429fecb9` v1) failed checksum verification
against the Plugin_Record (Requirement 10.4)". The per-arch plugin build
promoted every built artifact to a single UNVERSIONED Plugin_Library key
— `workflow-plugins/custom/{usecase}/{arch}/{plugin}.so`
(`plugin_builds.library_so_key`) — so every rebuild of every version of
the plugin overwrote the same object. A Plugin_Record version records
its SHA-256 against the bytes at that key at build time; as soon as ANY
later version of the same plugin built and overwrote the key, the
earlier version's recorded checksum no longer matched the bytes now at
its `s3Key`. The Component_Packager's Requirement 10.4 verification
(`workflow_packaging.verify_custom_plugin_artifact`: recompute the
Plugin_Artifact SHA-256 at the recorded `s3Key` and KMS-verify the
recorded signature) then rejected packaging of every workflow pinned to
the older Custom_Node_Type version. Observed concretely: building
resize-image v2 broke packaging of workflows pinned to resize-image v1.

Fix (`functions/plugin_builds.py`): a new
`versioned_library_so_key(usecase_id, arch, plugin_id, version,
plugin_name)` returns an IMMUTABLE per-version key
`workflow-plugins/custom/{usecase}/{arch}/{plugin_id}/{version}/{plugin}.so`.
`record_promoted_artifact(...)` (successful-build path) now streams the
`.so` from the unversioned promotion key the build image wrote,
RE-HOMES the bytes to the versioned key, records SHA-256 + KMS
signature, writes the detached signature beside the versioned copy, and
returns `s3Key` = the versioned key; the x86_64 `gstIntrospection`
report is still read from the promoted (unversioned) key where the build
image uploads it. `store_signed_artifact(...)` (prebuilt upload path)
also stores at the versioned key. Both functions gained `plugin_id` and
`version` parameters, and their call sites in `handle_build_result` and
`start_builds` were updated. Because the recorded artifact now lives at
a version-scoped key, a later version's rebuild can never overwrite an
earlier version's recorded bytes, so each version stays verifiable
against its recorded checksum forever. The fix is currently uncommitted;
it protects FUTURE builds only (see Deployment notes for the data-repair
dimension).

## Tests

- `edge-cv-portal/backend/layers/workflow_core/tests/test_scaffold.py`:
  `TestPluginIdent`, `TestRuntimeBridgeHardening` (defects 1, 3, 4).
- `edge-cv-portal/backend/tests/test_plugin_components.py`: hook
  packaging + no-hook back-compat (defect 2).
- `edge-cv-portal/backend/layers/workflow_core/tests/test_compiler_bedrock.py`:
  dangling-branch re-attachment + unconnected-root preservation
  (defect 5).
- `test/backend-test/workflow_engine/test_workflow_aravis_executor.py`:
  pixel-format tagging / bayer2rgb injection and
  `test_local_image_source_configuration_drives_the_grab`
  (defects 6, 7).
- Vendored `workflow_core` copies under
  `src/backend/workflow_engine/vendor/` synced;
  `test_vendored_catalog_mirror.py` green.
- `edge-cv-portal/backend/tests/test_plugin_builds.py` and
  `test_property_sign_verify.py`: versioned Plugin_Library key on both
  the promoted-build and prebuilt-upload paths (defect 8). Remaining
  work before this fix can ship:
  - `test_property_sign_verify.py` (~line 118) still calls
    `store_signed_artifact(usecase_id, arch, plugin_name, data)` with the
    OLD 4-arg signature (now raises `TypeError`); update it to pass
    `plugin_id`/`version`.
  - `test_plugin_builds.py` — the `library_key` helper (~line 154)
    returns the unversioned key and the success and prebuilt paths assert
    `entry["s3Key"] == library_key(...)` (~lines 294 and 486); these must
    expect the versioned key
    (`workflow-plugins/custom/{usecase}/{arch}/{plugin_id}/{version}/{plugin}.so`).
  - Add a regression/property test that pins the core invariant:
    building a LATER version of a plugin MUST NOT invalidate an EARLIER
    version's recorded artifact/checksum — each version's recorded
    `s3Key` bytes stay verifiable against its recorded checksum after any
    number of later-version rebuilds. `test_workflow_packaging_custom_plugins.py`
    exercises the consuming `verify_custom_plugin_artifact` (Req 10.4)
    end of the same invariant.

## Related hardening (same investigation, committed earlier)

- Node-designer wizard registered a factory name that could never match
  the scaffold's element (`resize_image` vs `customresizeimage`) — fixed
  in `registration.ts` (commit 9f8d9b8).
- `MissingPipelineElementError` preflight in the pipeline executor so a
  factory/element mismatch fails with an actionable message instead of a
  bare gst_parse_error (commit 9f8d9b8).
- Unattributable pipeline failures no longer finalize all nodes green
  (NodeStatusCollector.finalize failure_detail, commit 9f8d9b8).

## Deployment notes

The live-patched device artifacts (renamed .so, hand-copied hook,
patched compiled_pipeline.json documents) are superseded by these source
fixes on the next plugin rebuild + component publish + workflow
recompile. The resize node's parameters in existing deployed documents
were hand-set (4608x3288, ch=4, target 1152x822 + post-resize
videoconvert) and will not survive repackaging; re-deploy the workflow
after rebuilding the plugin.

Defect 8 has a data-repair dimension beyond the code fix: the versioned
key protects FUTURE builds only. Existing Plugin_Record versions (e.g.
resize-image v1) still point their `s3Key` at the overwritten
unversioned key, so even after the backend deploys they remain broken —
their recorded checksum will not match the bytes at that key — until the
plugin version is REBUILT, which re-promotes and re-records the artifact
at a versioned key with a matching checksum. Deployment sequence to
actually clear the user's "Package failed" (Req 10.4) error:

1. Finish and commit the `plugin_builds.py` fix — update the two broken
   tests (`test_property_sign_verify.py` `store_signed_artifact` call and
   `test_plugin_builds.py` `library_key`/`s3Key` assertions) and add the
   versioned-key invariant regression test.
2. Deploy the backend Lambda functions.
3. Rebuild the `resize_image` plugin (v1 and any affected versions) so
   artifacts are re-homed/re-recorded at versioned keys — this rebuild
   also picks up the committed defect-1 meson/symbol fix — then
   republish the Plugin_Component.
4. Repackage and redeploy the workflow.
