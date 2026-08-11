/**
 * TypeScript types for the Node_Designer frontend (custom-node-designer).
 *
 * Wire shapes of the Plugin_Record API (plugin_records.py,
 * plugin_builds.py) and the constants a Custom_Node_Type declaration is
 * validated against. Keep the constants in sync with the Python source
 * of truth in `workflow_core.catalog.models`
 * (edge-cv-portal/backend/layers/workflow_core/).
 */

// --------------------------------------------------------------------------
// Target architectures (workflow_core.catalog.models.DEVICE_ARCHITECTURES)
// --------------------------------------------------------------------------

/** Buildable device Target_Architectures, including x86_64_nvidia. */
export const DEVICE_ARCHITECTURES = [
  'x86_64',
  'x86_64_nvidia',
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
  'arm64_jp7',
] as const;

export type DeviceArchitecture = (typeof DEVICE_ARCHITECTURES)[number];

/** Architectures with a DeepStream runtime (Requirement 5.1). */
export const DEEPSTREAM_ARCHITECTURES = [
  'arm64_jp4',
  'arm64_jp5',
  'arm64_jp6',
] as const;

/** Human-readable Target_Architecture labels. */
export const ARCHITECTURE_LABELS: Record<DeviceArchitecture, string> = {
  x86_64: 'x86_64',
  x86_64_nvidia: 'x86_64 (NVIDIA GPU)',
  arm64_jp4: 'arm64 JetPack 4',
  arm64_jp5: 'arm64 JetPack 5',
  arm64_jp6: 'arm64 JetPack 6',
  arm64_jp7: 'arm64 JetPack 7',
};

// --------------------------------------------------------------------------
// Declaration constants (workflow_core.catalog.models)
// --------------------------------------------------------------------------

export const PORT_TYPES = ['VideoFrames', 'InferenceMeta', 'EventSignal'] as const;
export type PortType = (typeof PORT_TYPES)[number];

export const CATEGORIES = [
  'input',
  'preprocessing',
  'inference',
  'post_processing',
  'output',
] as const;
export type NodeCategory = (typeof CATEGORIES)[number];

export const PARAMETER_TYPES = [
  'string',
  'int',
  'float',
  'bool',
  'enum',
  'code',
  'model_ref',
] as const;
export type ParameterType = (typeof PARAMETER_TYPES)[number];

// --------------------------------------------------------------------------
// Custom_Node_Type declaration wire shape (design "Data Models")
// --------------------------------------------------------------------------

export interface PortDeclaration {
  name: string;
  portType: string;
}

export interface ParameterDeclaration {
  name: string;
  paramType: string;
  required: boolean;
  default?: string | number | boolean | null;
  constraints?: Record<string, unknown>;
  description: string;
  examples: Array<string | number | boolean>;
}

/**
 * The declaration the create wizard sends with POST /plugins
 * (kind 'scaffold'): the node-catalog wire shape plus the selected
 * Target_Architectures the scaffold renders build configurations for.
 */
export interface ScaffoldDeclaration {
  typeId: string;
  displayName: string;
  description?: string;
  category: string;
  inputs: PortDeclaration[];
  outputs: PortDeclaration[];
  parameters: ParameterDeclaration[];
  mappings: unknown[];
  architectures: string[];
}

// --------------------------------------------------------------------------
// Plugin_Record wire shapes (plugin_records.py)
// --------------------------------------------------------------------------

export type LifecycleState = 'dev' | 'test' | 'prod';
export type ReviewDecision = 'pending' | 'approved' | 'rejected';
export type BuildStatus = 'queued' | 'building' | 'succeeded' | 'failed';
export type Classification = 'good' | 'bad' | 'ugly' | 'unclassified';
export type RecordKind = 'scaffold' | 'generated' | 'imported';

/** List-view summary of one Plugin_Record version (record_summary). */
export interface PluginRecordSummary {
  plugin_id: string;
  version: number;
  usecase_id: string;
  name: string;
  kind: RecordKind;
  deepstream: boolean;
  lifecycle_state: LifecycleState;
  review_decision: ReviewDecision;
  /** Import outcome of imported records ('fetching' while the async
   * repository fetch runs); null/absent for other origins. */
  import_status?: ImportStatus | null;
  classification?: Classification | null;
  build_status: Record<string, BuildStatus | null>;
  updated_at: number;
  /** The recorded plugin selection of an imported record (absent when
   * no selection was recorded or for other origins), so the library
   * list shows which plugins the import covers. */
  selected_plugins?: string[];
  /** How many individual plugins the import enumerated
   * (len(plugins_found)); absent before the fetch settles and for
   * other origins. */
  plugins_found_count?: number;
}

/** Per-arch Plugin_Artifact entry on a Plugin_Record version. */
export interface PluginArtifactEntry {
  s3Key?: string;
  checksum?: string;
  signature?: string;
  buildStatus?: BuildStatus | null;
  logTail?: string;
  prebuilt?: boolean;
}

/** Full Plugin_Record version view (version_detail). */
export interface PluginVersionDetail {
  plugin_id: string;
  version: number;
  usecase_id: string;
  name: string;
  description: string;
  kind: RecordKind;
  deepstream: boolean;
  provenance: Record<string, unknown>;
  lifecycle_state: LifecycleState;
  review: { decision: ReviewDecision; reviewer?: string | null; reviewedAt?: number | null };
  artifacts: Record<string, PluginArtifactEntry>;
  component: Record<string, unknown>;
  source_s3_prefix: string;
  created_by: string;
  created_at: number;
  updated_at: number;
  /**
   * Import fields, present on imported records only: the UI polls
   * GET /plugins/{id}/versions/{v} while the asynchronous repository
   * fetch runs (import_status 'fetching') and acts on the outcome
   * (failed with import_finding, pending_selection with plugins_found,
   * or imported).
   */
  import_status?: ImportStatus;
  import_finding?: string;
  plugins_found?: EnumeratedPlugin[];
  selected_plugins?: string[];
  /**
   * Advisory per-platform requirements check recorded when the import
   * fetch settled ({arch: entry} over the requested architectures):
   * whether the source's minimum GStreamer version requirement is
   * satisfied by the GStreamer version each platform's build image
   * ships. Never blocks builds — incompatible architectures still
   * queue; the UI warns instead.
   */
  platform_compatibility?: Record<string, PlatformCompatibilityEntry>;
  /**
   * Multi-revision import (per-architecture revisions): each requested
   * architecture mapped to the revision slug of the fetch its builds
   * read from ({arch: slug} into `fetches`). Absent for single-revision
   * imports and other origins.
   */
  arch_revisions?: Record<string, string>;
  /**
   * Per-revision fetch map of a multi-revision import: each distinct
   * effective revision fetched once to its own source prefix
   * ({slug: entry}). Absent for single-revision imports.
   */
  fetches?: Record<string, ImportFetchEntry>;
}

/** Per-revision fetch status of a multi-revision import. */
export type ImportFetchStatus = 'fetching' | 'succeeded' | 'failed';

/**
 * One distinct revision's fetch of a multi-revision import
 * (plugin_importer `fetches` map): the revision ('default' for the
 * repository default branch), the rev-{slug}/ source prefix its tree
 * synced to, the CodeBuild fetch id, and the fetch status. The record's
 * import_status leaves 'fetching' only when every fetch settles.
 */
export interface ImportFetchEntry {
  revision: string;
  source_prefix: string;
  fetch_build_id?: string;
  status: ImportFetchStatus;
}

/**
 * One platform's entry of the advisory requirements check
 * (plugin_importer.platform_compatibility). Keep in sync with the
 * Python source of truth.
 */
export interface PlatformCompatibilityEntry {
  compatible: boolean;
  /** GStreamer version the platform's build image ships (e.g. '1.16');
   * null when the platform is unknown. */
  platformVersion?: string | null;
  /** Minimum GStreamer version the source requires; null when no
   * requirement could be determined. */
  requiredVersion?: string | null;
  /** Plain-language explanation, set on incompatible entries (e.g.
   * "The source requires GStreamer >= 1.24; arm64 JetPack 5 provides
   * 1.16"); null otherwise. */
  reason?: string | null;
  /** Upstream release branch matching the platform's GStreamer minor
   * (official GStreamer modules only, e.g. '1.16' for arm64_jp5);
   * null for non-official repositories and compatible platforms. */
  suggestedRevision?: string | null;
}

/** Per-arch build status view (plugin_builds.py builds_view, 3.5). */
export interface PluginBuildsView {
  plugin_id: string;
  version: number;
  requested_architectures: string[];
  builds: Record<string, PluginArtifactEntry>;
  settled: boolean;
  component_packaging_triggered: boolean;
}

/** Scaffold source file map {relative/path: content}. */
export type ScaffoldFiles = Record<string, string>;

// --------------------------------------------------------------------------
// Node_Generator wire shapes (node_generator.py, task 12.2)
// --------------------------------------------------------------------------

/**
 * Generation-turn state of the asynchronous start/poll flow. Bedrock
 * generation takes 45-50 s (past the API Gateway 29 s cap), so
 * POST /plugins/generate and follow-up prompts on
 * POST /plugins/generate/{session}/message return 202 with
 * turn_status 'pending', and GET /plugins/generate/{session} is polled
 * until the turn settles (Requirement 2.2).
 */
export type GenerationTurnStatus = 'pending' | 'running' | 'completed' | 'failed';

/** Error of a failed generation turn (the error-envelope contents). */
export interface GenerationTurnError {
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
  http_status?: number;
}

/**
 * One generation-turn state: the 202 body of the start routes and the
 * body of the poll route. A completed turn carries the complete
 * generated Plugin_Scaffold file map plus the assistant commentary; a
 * failed turn carries turn_error (2.6, 2.7).
 */
export interface GenerationTurnState {
  session_id: string;
  usecase_id: string;
  turn_status: GenerationTurnStatus;
  turn_error?: GenerationTurnError | null;
  files?: ScaffoldFiles;
  assistant_text?: string;
  model_id?: string;
}

// --------------------------------------------------------------------------
// Plugin_Importer / Module_Listing wire shapes (plugin_importer.py, task 12.3)
// --------------------------------------------------------------------------

/** One Module_Listing entry from GET /plugin-modules (Requirement 6.1). */
export interface PluginModuleEntry {
  name: string;
  description: string;
  repoUrl: string;
  classification: Classification;
}

/** Response of GET /plugin-modules (cached for at most 24 h, 6.4). */
export interface ModuleListingResponse {
  modules: PluginModuleEntry[];
  fetchedAt: number;
  cached: boolean;
}

/**
 * One individual plugin of an official module, from
 * GET /plugin-modules?module=<name> (each subdirectory of the module's
 * gst/, ext/, sys/ trees in the gstreamer monorepo is one plugin).
 */
export interface ModulePluginEntry {
  name: string;
  description?: string;
}

/** Response of GET /plugin-modules?module=<name> (cached ≤ 24 h per
 * module; failure answers MODULE_LISTING_UNAVAILABLE and the import
 * proceeds with the full plugin set). */
export interface ModulePluginsResponse {
  module: string;
  plugins: ModulePluginEntry[];
  fetchedAt: number;
  cached: boolean;
}

/** Request body of POST /plugins/import (Requirements 4.1, 5.1, 6.2). */
export interface ImportPluginRequest {
  usecase_id: string;
  repo_url: string;
  revision?: string;
  architectures: string[];
  name?: string;
  description?: string;
  deepstream?: boolean;
  module_name?: string;
  /**
   * Optional import-time plugin selection (module imports only): the
   * chosen subset of the module's plugin list. Absent or empty imports
   * the whole module; when present the backend records it on the
   * Plugin_Record provenance (selectedPlugins), skips the
   * pending-selection step, and submits builds immediately.
   */
  selected_plugins?: string[];
  /**
   * Optional per-architecture revision overrides ({arch: revision}, a
   * subset of `architectures`): each arch's effective source revision
   * is its override or the top-level `revision` (default branch when
   * neither is given). Distinct effective revisions fetch once each;
   * absent keeps today's single-revision behavior exactly. Motivating
   * scenario: gst-plugins-good needs main for the GStreamer 1.20+
   * platforms but branch '1.16' for arm64_jp5 and '1.14' for arm64_jp4.
   */
  arch_revisions?: Record<string, string>;
}

/**
 * Import outcome recorded on the Plugin_Record. A fresh import starts
 * in 'fetching' while the asynchronous CodeBuild fetch clones the
 * repository (POST /plugins/import answers 202 and the UI polls
 * GET /plugins/{id}/versions/{v} until the status settles). A
 * plugin-set import with more than one enumerated plugin waits in
 * pending_selection until the user picks the subset to import via
 * POST /plugins/{id}/versions/{v}/select-plugins.
 */
export type ImportStatus = 'fetching' | 'imported' | 'failed' | 'pending_selection';

/**
 * One individual plugin target enumerated in the imported source tree:
 * a plugin directory under gst/, ext/, or sys/ for the plugin-set
 * layout (gst-plugins-good style), or the single entry (path '') of a
 * single-plugin repository.
 */
export interface EnumeratedPlugin {
  name: string;
  path: string;
  /** What the plugin does, joined by the backend from the repository's
   * docs/gst_plugins_cache.json metadata; absent when unknown. */
  description?: string;
}

/**
 * Response of POST /plugins/import (202): the Plugin_Record created in
 * import_status 'fetching' while the repository fetch runs
 * asynchronously (Requirements 4.2, 4.5). Poll getVersion until the
 * status leaves 'fetching', then act on the settled record.
 */
export interface ImportPluginResponse {
  plugin: PluginVersionDetail;
  import: {
    status: ImportStatus;
    /** CodeBuild id of the asynchronous repository fetch
     * (single-revision imports). */
    buildId?: string;
    /** CodeBuild fetch ids per revision slug (multi-revision
     * imports). */
    fetchBuildIds?: Record<string, string>;
  };
}

/**
 * Outcome of POST /plugins/{id}/versions/{v}/select-plugins: the chosen
 * subset is recorded on the Plugin_Record and builds are submitted for
 * the previously requested Target_Architectures.
 */
export interface SelectImportPluginsResponse {
  plugin: PluginVersionDetail;
  import: {
    status: ImportStatus;
    selected_plugins: string[];
    submitted_architectures: string[];
  };
}

/**
 * Outcome of POST /plugins/{id}/versions/{v}/adjust-revision (202):
 * the updated Plugin_Record detail (carrying the new/updated `fetches`
 * entry and, on the reuse path, the arch's `arch_revisions` mapping)
 * plus the refreshed builds view — the adjusted architecture is queued
 * again, so the view is no longer settled and the detail page's poll
 * resumes.
 */
export interface AdjustRevisionResponse {
  plugin: PluginVersionDetail;
  builds: PluginBuildsView;
}

// --------------------------------------------------------------------------
// Plugin_Simulator wire shapes (plugin_simulator.py, task 12.4)
// --------------------------------------------------------------------------

/** SimulationRuns item status (plugin_simulator.py). */
export type SimulationStatus = 'pending' | 'running' | 'completed' | 'failed';

/** Failure recorded on a failed run; `timeout` marks the 5-minute limit (7.7). */
export interface SimulationFailure {
  message: string;
  timeout?: boolean;
}

/** Public shape of one SimulationRuns record (run_summary). */
export interface SimulationRunSummary {
  run_id: string;
  plugin_id: string;
  version: number;
  usecase_id: string;
  dataset:
    | { kind: 'dataset'; dataset_id: string }
    | { kind: 'uploaded'; frame_count: number };
  parameters: Record<string, string | number | boolean | null>;
  element_factory: string;
  status: SimulationStatus;
  results_s3_key: string | null;
  failure: SimulationFailure | null;
  started_at: number | null;
  finished_at: number | null;
  created_by: string;
}

/**
 * One per-frame result record the simulate harness flushes (7.3):
 * input/output frame references under the run's S3 prefix plus the
 * metadata the plugin emitted for that frame. A dropped frame keeps
 * its inputRef with a null outputRef.
 */
export interface SimulationFrameRecord {
  frameIndex: number;
  inputRef: string | null;
  outputRef: string | null;
  metadata: Record<string, unknown>;
}

/**
 * The results document of GET /simulations/{runId}: flushed
 * incrementally, so failed and timed-out runs carry the partial
 * frames produced before termination (7.6, 7.7).
 */
export interface SimulationResultsDocument {
  element?: string;
  parameters?: Record<string, unknown>;
  status?: string;
  frameCount?: number | null;
  frames?: SimulationFrameRecord[];
  error?: { code?: string; message?: string; errorOutput?: string } | null;
}

/** Test_Dataset summary for the Use_Case-scoped picker (7.1). */
export interface TestDatasetSummary {
  dataset_id: string;
  usecase_id: string;
  name: string;
  description?: string;
  total_bytes?: number;
  file_count?: number;
  format?: string;
  created_at?: number;
}

/** One uploaded sample frame of the simulate start request (7.1). */
export interface SampleFrameUpload {
  name: string;
  content_base64: string;
}

/**
 * Body of POST /plugins/{id}/versions/{v}/simulate: exactly one input
 * source (dataset_id or sample_frames) plus the declared parameter
 * values for this run; a re-run with changed values is another POST
 * with new `parameters` (7.4).
 */
export interface StartSimulationRequest {
  dataset_id?: string;
  sample_frames?: SampleFrameUpload[];
  parameters?: Record<string, string | number | boolean>;
  element_factory?: string;
}

// --------------------------------------------------------------------------
// Custom_Node_Type registration wire shapes (custom_node_types.py, task 12.5)
// --------------------------------------------------------------------------

/** One element of a mapping's elementChain ({factory, argsTemplate}). */
export interface ElementChainEntry {
  factory: string;
  /** GObject property -> value template (e.g. "{radius}"). */
  argsTemplate?: Record<string, string>;
}

/** Element/property mapping for one built Target_Architecture (8.1). */
export interface MappingDeclaration {
  arch: string;
  elementChain: ElementChainEntry[];
  executorBinding?: string;
  /** Injected server-side as custom:{usecase_id}/{plugin_name} (8.6). */
  pluginDependencies?: string[];
}

/**
 * The declaration POST /custom-node-types validates through
 * descriptor_from_declaration: ports from PORT_TYPES, parameters with
 * descriptions and examples, element/property mapping per built arch,
 * and the hardware-dependence flag (Requirements 8.1, 8.5).
 */
export interface NodeTypeRegistrationDeclaration {
  typeId: string;
  displayName: string;
  description?: string;
  category: string;
  inputs: PortDeclaration[];
  outputs: PortDeclaration[];
  parameters: ParameterDeclaration[];
  mappings: MappingDeclaration[];
  hardwareDependent: boolean;
}

/** Version-history summary of one Custom_Node_Type version (node_type_summary). */
export interface NodeTypeSummary {
  node_type_id: string;
  version: number;
  usecase_id: string;
  plugin_id: string | null;
  plugin_version: number | null;
  display_name: string | null;
  category: string | null;
  deprecated: boolean;
  updated_at: number | null;
}

/** Full Custom_Node_Type version view (node_type_detail). */
export interface NodeTypeDetail {
  node_type_id: string;
  version: number;
  usecase_id: string;
  usecase_ids: string[];
  plugin_id: string | null;
  plugin_version: number | null;
  declaration: Record<string, unknown>;
  deprecated: boolean;
  created_by: string | null;
  created_at: number | null;
  updated_at: number | null;
}
