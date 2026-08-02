/**
 * Node_Designer API client (custom-node-designer).
 *
 * Thin client over the Node_Designer backend routes (plugin_records.py,
 * plugin_builds.py) following the portal's request conventions in
 * services/api.ts: bearer token from localStorage, the global loading
 * bus, and the structured error envelope {error: {code, message,
 * details}} surfaced as ApiError so views can act on error codes (e.g.
 * INVALID_DECLARATION identifying the failing input, Requirement 1.7).
 */
import { getConfig } from '../../config';
import { ApiError } from '../../services/api';
import { beginRequest, endRequest } from '../../services/loadingBus';
import type { GstPropertiesResponse } from './scan';
import type {
  AdjustRevisionResponse,
  GenerationTurnState,
  NodeTypeDetail,
  NodeTypeSummary,
  NodeTypeRegistrationDeclaration,
  ImportPluginRequest,
  ImportPluginResponse,
  ModuleListingResponse,
  ModulePluginsResponse,
  PluginBuildsView,
  PluginRecordSummary,
  PluginVersionDetail,
  ScaffoldDeclaration,
  ScaffoldFiles,
  SelectImportPluginsResponse,
  SimulationResultsDocument,
  SimulationRunSummary,
  StartSimulationRequest,
  TestDatasetSummary,
} from './types';

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem('idToken');
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  beginRequest();
  try {
    const response = await fetch(`${getConfig().apiUrl}${endpoint}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      if (response.status === 401) {
        localStorage.removeItem('idToken');
        if (window.location.pathname !== '/login') {
          window.location.href = '/login';
        }
      }
      const error = await response.json().catch(() => ({ error: 'Request failed' }));
      if (error.error && typeof error.error === 'object') {
        throw new ApiError(
          error.error.message || `HTTP ${response.status}`,
          response.status,
          error.error.code,
          error.error.details
        );
      }
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    return response.json();
  } finally {
    endRequest();
  }
}

export const nodeDesignerApi = {
  /** GET /plugins?usecase_id=... — Plugin_Record list for the library view. */
  listPlugins(usecaseId: string): Promise<{ plugins: PluginRecordSummary[]; count: number }> {
    return request(`/plugins?usecase_id=${encodeURIComponent(usecaseId)}`);
  },

  /** GET /plugins/{id} — latest version detail plus version history. */
  getPlugin(pluginId: string): Promise<{
    plugin: PluginVersionDetail;
    versions: PluginRecordSummary[];
  }> {
    return request(`/plugins/${encodeURIComponent(pluginId)}`);
  },

  /** GET /plugins/{id}/versions/{v} — one version's full detail. */
  getVersion(pluginId: string, version: number): Promise<{ plugin: PluginVersionDetail }> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}`
    );
  },

  /**
   * DELETE /plugins/{id} — delete every version of a Plugin_Record
   * (bad or duplicate imports) plus best-effort cleanup of its source
   * snapshot and built artifacts. Refused with 409 RECORD_IN_USE when
   * any version was promoted beyond dev.
   */
  deletePlugin(
    pluginId: string
  ): Promise<{ deleted: boolean; plugin_id: string; versions: number[] }> {
    return request(`/plugins/${encodeURIComponent(pluginId)}`, {
      method: 'DELETE',
    });
  },

  /**
   * POST /plugins with a scaffold declaration: renders the
   * Plugin_Scaffold server-side and creates the Plugin_Record (dev,
   * review pending). An invalid declaration returns 400
   * INVALID_DECLARATION with details.field identifying the failing
   * input and creates no record (Requirement 1.7).
   */
  createScaffoldPlugin(data: {
    usecase_id: string;
    name: string;
    description?: string;
    declaration: ScaffoldDeclaration;
  }): Promise<{ plugin: PluginVersionDetail; files: ScaffoldFiles }> {
    return request('/plugins', {
      method: 'POST',
      body: JSON.stringify({ ...data, kind: 'scaffold' }),
    });
  },

  /** GET .../source — list source files, or fetch one file's content. */
  getVersionSource(
    pluginId: string,
    version: number,
    file?: string
  ): Promise<{ files?: Array<{ file: string; size: number }>; file?: string; content?: string }> {
    const suffix = file ? `?file=${encodeURIComponent(file)}` : '';
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/source${suffix}`
    );
  },

  /**
   * PUT .../source — persist the complete (original or edited) scaffold
   * file map ahead of a build (Requirement 1.6). Non-buildable source
   * is rejected with 422 SCAFFOLD_INVALID listing every defect.
   */
  putVersionSource(
    pluginId: string,
    version: number,
    files: ScaffoldFiles
  ): Promise<{ files: string[]; count: number }> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/source`,
      { method: 'PUT', body: JSON.stringify({ files }) }
    );
  },

  /**
   * POST .../build — submit the version's source to the
   * Plugin_Build_Service for the selected Target_Architectures (1.6).
   */
  startBuilds(
    pluginId: string,
    version: number,
    architectures: string[]
  ): Promise<PluginBuildsView> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/build`,
      { method: 'POST', body: JSON.stringify({ architectures }) }
    );
  },

  /** GET .../builds — per-arch build status with log excerpts (3.5). */
  getBuilds(pluginId: string, version: number): Promise<PluginBuildsView> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/builds`
    );
  },

  /**
   * GET .../gst-properties — the version's stored Introspection_Report
   * as per-element Parameter_Suggestions for the wizard's parameter
   * scan (gst-parameter-prepopulation Requirement 1.5), or a
   * machine-readable unavailability reason — `no_x86_64_build`,
   * `not_captured`, `introspection_failed` — as a normal 200, never an
   * error (1.6, 7.4).
   */
  getGstProperties(pluginId: string, version: number): Promise<GstPropertiesResponse> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/gst-properties`
    );
  },

  // ---------------------------------------------------- Node_Generator

  /**
   * POST /plugins/generate — start a scaffold-generation chat session
   * with a natural-language prompt and the Custom_Node_Type declaration
   * (Requirements 2.1, 2.2). Returns 202 with the session id and
   * turn_status 'pending' immediately (the Bedrock turn runs
   * asynchronously); poll getGenerationTurn until the turn settles.
   * Validation failures (400 INVALID_DECLARATION etc.) still reject
   * synchronously with the structured error envelope.
   */
  startGeneration(data: {
    usecase_id: string;
    prompt: string;
    declaration: ScaffoldDeclaration;
  }): Promise<GenerationTurnState> {
    return request('/plugins/generate', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  /**
   * GET /plugins/generate/{session} — poll the current generation turn.
   * A completed turn carries the generated files and assistant
   * commentary; a failed turn carries turn_error (422
   * GENERATED_SCAFFOLD_INVALID with details.defects, 504
   * GENERATION_TIMEOUT, 502 BEDROCK_*), so the caller preserves the
   * prompt for retry (2.6, 2.7).
   */
  getGenerationTurn(sessionId: string): Promise<GenerationTurnState> {
    return request(`/plugins/generate/${encodeURIComponent(sessionId)}`);
  },

  /**
   * POST /plugins/generate/{session}/message with a follow-up prompt:
   * the Node_Generator modifies the current generated source rather
   * than regenerating from scratch (Requirement 2.4). Same async 202 +
   * poll flow as startGeneration. Failures preserve the session (and
   * the prompt client-side) for retry (2.6, 2.7).
   */
  continueGeneration(sessionId: string, prompt: string): Promise<GenerationTurnState> {
    return request(
      `/plugins/generate/${encodeURIComponent(sessionId)}/message`,
      { method: 'POST', body: JSON.stringify({ prompt }) }
    );
  },

  /**
   * POST /plugins/generate/{session}/message with accept: true —
   * accept the session's current generated source into a Plugin_Record
   * (kind 'generated', dev, review pending) with the generation prompt
   * recorded as provenance (Requirement 2.5).
   */
  acceptGeneration(
    sessionId: string,
    data: { name?: string; description?: string } = {}
  ): Promise<{ plugin: PluginVersionDetail }> {
    return request(
      `/plugins/generate/${encodeURIComponent(sessionId)}/message`,
      { method: 'POST', body: JSON.stringify({ accept: true, ...data }) }
    );
  },

  // -------------------------------------------------- Plugin_Importer

  /**
   * GET /plugin-modules — the Module_Listing index parsed server-side
   * into {name, description, repoUrl, classification} entries
   * (Requirement 6.1, cached ≤ 24 h per 6.4). Fetch/parse failure
   * returns the distinct MODULE_LISTING_UNAVAILABLE error code so the
   * import view surfaces the error and falls back to manual repository
   * URL entry (6.3).
   */
  listPluginModules(): Promise<ModuleListingResponse> {
    return request('/plugin-modules');
  },

  /**
   * GET /plugin-modules?module=<name> — the individual plugins of one
   * official module (same endpoint, query parameter — no extra route),
   * so the import view offers a plugin selection before the import.
   * Failure answers the same MODULE_LISTING_UNAVAILABLE code; the UI
   * treats it as non-blocking and imports the full plugin set.
   */
  listModulePlugins(moduleName: string): Promise<ModulePluginsResponse> {
    return request(`/plugin-modules?module=${encodeURIComponent(moduleName)}`);
  },

  /**
   * POST /plugins/import — import a GStreamer plugin from a public
   * repository (or a Module_Listing selection's published repoUrl, 6.2)
   * at an optional revision (Requirements 4.1, 4.3, 5.1).
   *
   * Asynchronous: answers 202 with the Plugin_Record created in
   * import_status 'fetching' (the repository clone runs in CodeBuild
   * past the API Gateway 29 s integration cap). Poll getVersion until
   * import_status settles: 'failed' carries import_finding (an
   * unreachable repository / missing revision, 4.4, or an unbuildable
   * tree, 4.5), 'pending_selection' carries plugins_found, and
   * 'imported' has builds started for the selected
   * Target_Architectures.
   */
  importPlugin(data: ImportPluginRequest): Promise<ImportPluginResponse> {
    return request('/plugins/import', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  /**
   * POST /plugins/{id}/versions/{v}/select-plugins — complete a
   * plugin-set import awaiting selection (import status
   * pending_selection): records the chosen subset of the enumerated
   * plugins on the Plugin_Record and submits builds for the requested
   * Target_Architectures. Rejected with 400 INVALID_PLUGIN_SELECTION
   * when the selection is empty or not a subset of plugins_found.
   */
  selectImportPlugins(
    pluginId: string,
    version: number,
    selectedPlugins: string[]
  ): Promise<SelectImportPluginsResponse> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/select-plugins`,
      { method: 'POST', body: JSON.stringify({ selected_plugins: selectedPlugins }) }
    );
  },

  /**
   * POST /plugins/{id}/versions/{v}/adjust-revision — apply a
   * per-platform source revision override to a settled imported record
   * (import_status 'imported'): the backend fetches the adjusted
   * revision's tree into the record's `fetches` map (reusing an
   * already-fetched tree recording the same revision), maps
   * `arch_revisions[architecture]` on fetch success, and re-runs the
   * affected platform's build. Answers 202 with the updated plugin
   * detail and builds view; rejected with 409
   * REVISION_ADJUSTMENT_NOT_AVAILABLE for non-imports or unsettled
   * imports and 400 INVALID_ARCHITECTURE / INVALID_REVISION on bad
   * input.
   */
  adjustRevision(
    pluginId: string,
    version: number,
    architecture: string,
    revision: string
  ): Promise<AdjustRevisionResponse> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/adjust-revision`,
      { method: 'POST', body: JSON.stringify({ architecture, revision }) }
    );
  },

  // --------------------------------------------------- Plugin_Simulator

  /**
   * GET /test-datasets?usecase_id=... — Test_Datasets of the plugin's
   * Use_Case for the simulator's dataset picker (Requirement 7.1).
   */
  listTestDatasets(
    usecaseId: string
  ): Promise<{ datasets: TestDatasetSummary[]; count: number }> {
    return request(`/test-datasets?usecase_id=${encodeURIComponent(usecaseId)}`);
  },

  /**
   * POST /plugins/{id}/versions/{v}/simulate — start a Plugin_Simulator
   * run against a Test_Dataset or uploaded sample frames with the
   * declared parameter values (Requirement 7.1); a re-run with changed
   * values is another POST (7.4). Refused with 409
   * SIMULATION_REQUIRES_X86_64_BUILD when the version has no successful
   * x86_64 Plugin_Artifact (7.5).
   */
  startSimulation(
    pluginId: string,
    version: number,
    body: StartSimulationRequest
  ): Promise<{ simulation_run: SimulationRunSummary }> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/simulate`,
      { method: 'POST', body: JSON.stringify(body) }
    );
  },

  /**
   * GET /simulations/{runId} — run status plus the results document
   * flushed so far: per-frame input/output refs and emitted metadata
   * (7.3), with partial results for failed/timed-out runs (7.6, 7.7).
   */
  getSimulation(runId: string): Promise<{
    simulation_run: SimulationRunSummary;
    results: SimulationResultsDocument | null;
  }> {
    return request(`/simulations/${encodeURIComponent(runId)}`);
  },
  // ------------------------------------ registration + review (task 12.5)

  /**
   * POST /custom-node-types — register a Custom_Node_Type for a built
   * Plugin_Record version (Requirement 8.1). Invalid declarations are
   * rejected with 400 INVALID_DECLARATION / UNBUILT_ARCHITECTURE and
   * details.field identifying the offending input (8.5).
   */
  registerNodeType(data: {
    plugin_id: string;
    plugin_version?: number;
    declaration: NodeTypeRegistrationDeclaration;
    usecase_ids?: string[];
  }): Promise<{ nodeType: NodeTypeDetail }> {
    return request('/custom-node-types', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  /**
   * GET /custom-node-types?plugin_id=... — the latest version of every
   * Custom_Node_Type backed by the plugin. Drives the registration
   * wizard's update mode: a plugin already backing a node type is
   * updated (a new version) instead of registered twice.
   */
  listNodeTypes(pluginId: string): Promise<{ nodeTypes: NodeTypeSummary[]; count: number }> {
    return request(`/custom-node-types?plugin_id=${encodeURIComponent(pluginId)}`);
  },

  /** GET /custom-node-types/{id} — latest version detail + history (14.1). */
  getNodeType(nodeTypeId: string): Promise<{
    nodeType: NodeTypeDetail;
    versions: NodeTypeSummary[];
  }> {
    return request(`/custom-node-types/${encodeURIComponent(nodeTypeId)}`);
  },

  /**
   * PUT /custom-node-types/{id} — update the registered node type: a
   * declaration update creates a new retained version (14.1) pinning
   * the (possibly updated) backing Plugin_Record version.
   */
  updateNodeType(
    nodeTypeId: string,
    data: {
      declaration?: NodeTypeRegistrationDeclaration;
      plugin_version?: number;
      usecase_ids?: string[];
    }
  ): Promise<{ nodeType: NodeTypeDetail }> {
    return request(`/custom-node-types/${encodeURIComponent(nodeTypeId)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    });
  },

  /**
   * GET /plugins?review=pending — Plugin_Record versions awaiting a
   * security review decision (the PortalAdmin review queue, 10.2).
   */
  listPendingReviews(): Promise<{ plugins: PluginRecordSummary[]; count: number }> {
    return request('/plugins?review=pending');
  },

  /**
   * POST /plugins/{id}/versions/{v}/promote — dev->test (requires at
   * least one successful build, 9.4/9.5) and test->prod (requires an
   * approved security review, 9.9/9.10). 409 rejections identify the
   * missing gate.
   */
  promoteVersion(
    pluginId: string,
    version: number
  ): Promise<{ plugin: PluginVersionDetail }> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/promote`,
      { method: 'POST', body: JSON.stringify({}) }
    );
  },

  /**
   * POST /plugins/{id}/versions/{v}/demote — prod->test / test->dev;
   * always succeeds and only changes the state (9.12). Deployed
   * Workflow_Components keep running; the demoted state's gates apply
   * to subsequent packaging/deployment requests.
   */
  demoteVersion(
    pluginId: string,
    version: number
  ): Promise<{ plugin: PluginVersionDetail }> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/demote`,
      { method: 'POST', body: JSON.stringify({}) }
    );
  },

  /**
   * POST /plugins/{id}/versions/{v}/review — approve or reject a
   * pending security review (PortalAdmin only, Requirements 10.2, 10.3).
   */
  reviewVersion(
    pluginId: string,
    version: number,
    decision: 'approved' | 'rejected',
    notes?: string
  ): Promise<{ plugin: PluginVersionDetail }> {
    return request(
      `/plugins/${encodeURIComponent(pluginId)}/versions/${version}/review`,
      { method: 'POST', body: JSON.stringify({ decision, ...(notes ? { notes } : {}) }) }
    );
  },
};

export default nodeDesignerApi;
