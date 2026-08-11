/**
 * Import-flow helpers (custom-node-designer, task 12.3).
 *
 * Pure logic behind ImportView, kept out of the component so the
 * classification display, acknowledgment, and architecture-restriction
 * rules (Requirements 5.1, 15.1, 15.2, 15.3, 15.7) are unit-testable.
 *
 * The classification derivation mirrors the Python source of truth
 * `workflow_core.catalog.classification.classify_plugin_set`
 * (edge-cv-portal/backend/layers/workflow_core/): the frontend needs it
 * to display the Plugin_Set_Classification on the import confirmation
 * view for manual repository URLs before the import proceeds (15.2) —
 * Module_Listing entries already carry their classification from
 * GET /plugin-modules.
 */
import { ApiError } from '../../services/api';
import {
  ARCHITECTURE_LABELS,
  Classification,
  DEEPSTREAM_ARCHITECTURES,
  DEVICE_ARCHITECTURES,
  DeviceArchitecture,
  EnumeratedPlugin,
  ModulePluginEntry,
  PlatformCompatibilityEntry,
  PluginVersionDetail,
} from './types';

// ---------------------------------------------------------- explanations

/**
 * Fixed plain-language explanation for each Plugin_Set_Classification
 * value, presented verbatim alongside the classification (Requirement
 * 15.3). Keep in sync with `workflow_core.catalog.classification
 * .EXPLANATIONS`.
 */
export const CLASSIFICATION_EXPLANATIONS: Record<Classification, string> = {
  good:
    'good indicates a well-maintained, well-tested, properly licensed ' +
    'plugin set',
  bad:
    'bad indicates a plugin set lacking upstream review, testing, or ' +
    'active maintenance',
  ugly:
    'ugly indicates a plugin set of good quality that carries licensing ' +
    'or distribution concerns',
  unclassified:
    'unclassified indicates a plugin outside the official GStreamer ' +
    'plugin sets that warrants the highest caution',
};

/**
 * Imports classified bad, ugly, or unclassified require the user to
 * acknowledge the displayed classification explanation before the
 * import proceeds (Requirement 15.7). Only good imports proceed
 * without acknowledgment.
 */
export function requiresAcknowledgment(
  classification: Classification | null | undefined
): boolean {
  return classification !== 'good';
}

// -------------------------------------------- architecture restriction

/**
 * The Target_Architectures selectable for an import: DeepStream-flagged
 * imports are restricted to the Jetson JetPack builds (Requirement
 * 5.1); everything else may target all six architectures.
 */
export function selectableArchitectures(
  deepstream: boolean
): readonly DeviceArchitecture[] {
  return deepstream ? DEEPSTREAM_ARCHITECTURES : DEVICE_ARCHITECTURES;
}

/**
 * Prune an architecture selection to the selectable set — flipping the
 * DeepStream toggle on drops any non-Jetson architectures already
 * selected (Requirement 5.1).
 */
export function restrictArchitectureSelection(
  selected: string[],
  deepstream: boolean
): string[] {
  const allowed = new Set<string>(selectableArchitectures(deepstream));
  return selected.filter((arch) => allowed.has(arch));
}

// -------------------------------------- classification derivation port
//
// TypeScript port of workflow_core.catalog.classification: the official
// plugin-set module names and their known freedesktop.org repository
// locations map to good/bad/ugly; everything else — including arbitrary
// public repositories — is never guessed into an official set and
// classifies as unclassified (Requirements 15.3, 15.4).

const OFFICIAL_SET_MODULES: Record<string, Classification> = {
  'gst-plugins-good': 'good',
  'gst-plugins-bad': 'bad',
  'gst-plugins-ugly': 'ugly',
};

const GITLAB_HOST = 'gitlab.freedesktop.org';
const LEGACY_GIT_HOSTS = new Set(['cgit.freedesktop.org', 'anongit.freedesktop.org']);
const RELEASE_HOST = 'gstreamer.freedesktop.org';
const URL_SCHEMES = new Set(['http', 'https', 'git']);

/** URL path split into segments with any `.git` suffix stripped. */
function pathSegments(pathname: string): string[] {
  const segments: string[] = [];
  for (let raw of pathname.split('/')) {
    if (!raw) {
      continue;
    }
    if (raw.endsWith('.git')) {
      raw = raw.slice(0, -'.git'.length);
    }
    segments.push(raw);
  }
  return segments;
}

/** Classification for a repository URL, unclassified unless the URL is
 * a known freedesktop.org location of an official plugin set. */
function classifyRepoUrl(repoUrl: string): Classification {
  let url: URL;
  try {
    url = new URL(repoUrl.trim());
  } catch {
    return 'unclassified';
  }

  const scheme = url.protocol.replace(/:$/, '').toLowerCase();
  if (!URL_SCHEMES.has(scheme)) {
    return 'unclassified';
  }

  const host = (url.hostname || '').toLowerCase();
  const segments = pathSegments(url.pathname);

  if (host === GITLAB_HOST) {
    // https://gitlab.freedesktop.org/gstreamer/gst-plugins-good[.git]
    // and monorepo subproject paths such as
    // .../gstreamer/gstreamer/-/tree/main/subprojects/gst-plugins-good
    if (segments.length > 0 && segments[0] === 'gstreamer') {
      for (const segment of segments.slice(1)) {
        if (segment in OFFICIAL_SET_MODULES) {
          return OFFICIAL_SET_MODULES[segment];
        }
      }
    }
    return 'unclassified';
  }

  if (LEGACY_GIT_HOSTS.has(host)) {
    // https://cgit.freedesktop.org/gstreamer/gst-plugins-good/
    // git://anongit.freedesktop.org/gstreamer/gst-plugins-good
    if (
      segments.length >= 2 &&
      segments[0] === 'gstreamer' &&
      segments[1] in OFFICIAL_SET_MODULES
    ) {
      return OFFICIAL_SET_MODULES[segments[1]];
    }
    return 'unclassified';
  }

  if (host === RELEASE_HOST) {
    // https://gstreamer.freedesktop.org/src/gst-plugins-good/...
    for (const segment of segments) {
      if (segment in OFFICIAL_SET_MODULES) {
        return OFFICIAL_SET_MODULES[segment];
      }
    }
    return 'unclassified';
  }

  return 'unclassified';
}

/**
 * The Plugin_Set_Classification for a module: good/bad/ugly exactly
 * when the module name is one of the official plugin-set module names
 * or the repository URL is a known freedesktop.org location of an
 * official set; unclassified otherwise (Requirement 15.4). Pure and
 * deterministic — no network access, no guessing.
 */
export function classifyPluginSet(
  moduleName: string | null | undefined,
  repoUrl: string | null | undefined
): Classification {
  if (moduleName) {
    const name = moduleName.trim();
    if (name in OFFICIAL_SET_MODULES) {
      return OFFICIAL_SET_MODULES[name];
    }
  }

  if (repoUrl && repoUrl.trim()) {
    return classifyRepoUrl(repoUrl);
  }

  return 'unclassified';
}

// ------------------------------------------- asynchronous fetch flow
//
// POST /plugins/import answers 202 with the Plugin_Record in
// import_status 'fetching': the repository clone runs in CodeBuild
// outside the API Gateway 29 s integration cap. ImportView polls
// GET /plugins/{id}/versions/{v} every IMPORT_POLL_INTERVAL_MS until
// the status leaves 'fetching', then acts on the settled record. The
// transition decision is a pure function (importPollDecision) so it is
// unit-testable.

/** Poll the imported record every 3 s while the fetch runs. */
export const IMPORT_POLL_INTERVAL_MS = 3_000;

/**
 * Give up polling after ~12 minutes: the fetch CodeBuild project times
 * out at 10 minutes, so a record still 'fetching' past this bound will
 * not settle by itself.
 */
export const IMPORT_POLL_TIMEOUT_MS = 12 * 60 * 1000;

/** What ImportView does next with a polled (or just-imported) record. */
export type ImportPollDecision =
  | { kind: 'wait' }
  | { kind: 'timeout' }
  | { kind: 'failed'; finding: string }
  | { kind: 'select'; found: EnumeratedPlugin[] }
  | { kind: 'done' };

/**
 * Decide the next step from the record's import status:
 * - 'fetching': keep waiting, or give up past IMPORT_POLL_TIMEOUT_MS;
 * - 'failed': show the recorded import finding;
 * - 'pending_selection': open the plugin selection dialog over the
 *   enumerated plugins_found;
 * - 'imported' (and anything else): the import is complete — navigate
 *   to the plugin detail page.
 */
export function importPollDecision(
  plugin: Pick<
    PluginVersionDetail,
    'import_status' | 'import_finding' | 'plugins_found'
  >,
  elapsedMs: number
): ImportPollDecision {
  switch (plugin.import_status) {
    case 'fetching':
      return elapsedMs >= IMPORT_POLL_TIMEOUT_MS
        ? { kind: 'timeout' }
        : { kind: 'wait' };
    case 'failed':
      return {
        kind: 'failed',
        finding:
          plugin.import_finding ||
          'The repository could not be imported',
      };
    case 'pending_selection':
      return { kind: 'select', found: plugin.plugins_found || [] };
    default:
      return { kind: 'done' };
  }
}

// ------------------------------------------ external documentation
//
// Help links shown in the import view (Cloudscape Link external): the
// official GStreamer documentation index, the per-plugin docs pages,
// and the plugin-set split-up explanation. URL building is pure so it
// is unit-testable.

/** The official GStreamer documentation index. */
export const GSTREAMER_DOCS_URL =
  'https://gstreamer.freedesktop.org/documentation/';

/** "Learn more about GStreamer plugin sets" (good/bad/ugly split-up). */
export const GSTREAMER_PLUGIN_SETS_DOCS_URL =
  'https://gstreamer.freedesktop.org/documentation/additional/splitup.html';

/**
 * The official per-plugin documentation page:
 * https://gstreamer.freedesktop.org/documentation/<plugin>/index.html
 */
export function pluginDocsUrl(pluginName: string): string {
  return `${GSTREAMER_DOCS_URL}${encodeURIComponent(pluginName.trim())}/index.html`;
}

// ---------------------------------------------- plugin-set selection
//
// Plugin-set repositories (gst-plugins-good/bad/ugly style meson
// monorepos) enumerate their individual plugin targets at import; the
// record lands in pending_selection and the user picks the subset to
// import in a selection dialog before builds are submitted.
// Single-plugin repositories skip the selection step. The dialog state
// logic lives here so it is unit-testable.

/**
 * The enumerated plugins matching the selection dialog's filter box:
 * case-insensitive substring match on the plugin name, its source
 * path, or its description. An empty or whitespace-only filter matches
 * everything.
 */
export function filterPluginEntries(
  entries: EnumeratedPlugin[],
  filter: string
): EnumeratedPlugin[] {
  const needle = filter.trim().toLowerCase();
  if (!needle) {
    return entries;
  }
  return entries.filter(
    (entry) =>
      entry.name.toLowerCase().includes(needle) ||
      entry.path.toLowerCase().includes(needle) ||
      (entry.description || '').toLowerCase().includes(needle)
  );
}

/**
 * The checkbox description line for one enumerated plugin in the
 * selection dialog: the plugin's description with its source path as
 * secondary detail ("description — path"), whichever alone when only
 * one is known, undefined when neither is.
 */
export function pluginEntryDescription(
  entry: EnumeratedPlugin
): string | undefined {
  const description = (entry.description || '').trim();
  if (description && entry.path) {
    return `${description} — ${entry.path}`;
  }
  return description || entry.path || undefined;
}

/** Toggle one plugin's membership in the selection. */
export function togglePluginSelection(selected: string[], name: string): string[] {
  return selected.includes(name)
    ? selected.filter((n) => n !== name)
    : [...selected, name];
}

/**
 * Add every visible (filtered) plugin to the selection ("select all"),
 * preserving plugins already selected but currently filtered out.
 */
export function addAllToSelection(
  selected: string[],
  visible: EnumeratedPlugin[]
): string[] {
  const merged = new Set(selected);
  visible.forEach((entry) => merged.add(entry.name));
  return Array.from(merged);
}

/**
 * Validate the selection before it is submitted, mirroring the
 * backend's validate_plugin_selection: non-empty and a subset of the
 * enumerated plugins. Returns the error to display, or null when the
 * selection is submittable.
 */
export function pluginSelectionError(
  selected: string[],
  found: EnumeratedPlugin[]
): string | null {
  if (selected.length === 0) {
    return 'Select at least one plugin to import';
  }
  const foundNames = new Set(found.map((entry) => entry.name));
  const unknown = selected.filter((name) => !foundNames.has(name));
  if (unknown.length > 0) {
    return `Unknown plugins: ${unknown.join(', ')}`;
  }
  return null;
}

// ------------------------------------- import-time module selection
//
// When an official module is chosen, its individual plugins load from
// GET /plugin-modules?module=<name> and the form offers a selection
// (default: none selected — the user opts in explicitly) before the
// import. The chosen subset serializes to the import request's
// selected_plugins; a full selection serializes to nothing (absent =
// whole module, today's behavior). Loading the list is a non-blocking
// enhancement: on failure the import proceeds with the full set.

/** The plugin names of a module plugin list, in listing order. */
export function allPluginNames(plugins: ModulePluginEntry[]): string[] {
  return plugins.map((plugin) => plugin.name);
}

/**
 * The explicit-selection gate for the module import path: a loaded
 * (non-empty) plugin list requires at least one selected plugin before
 * the import proceeds. True exactly when the source is the module
 * listing, plugins are available, and nothing is selected yet. An
 * unavailable or empty plugin list never blocks (whole-module
 * fallback), and manual repository URL imports are never gated.
 */
export function moduleSelectionIncomplete(
  source: 'module' | 'manual',
  availableNames: string[],
  selectedNames: string[]
): boolean {
  return (
    source === 'module' &&
    availableNames.length > 0 &&
    selectedNames.length === 0
  );
}

/**
 * Normalize a module plugin selection against the available list:
 * unknown names drop, order follows the listing. Pure companion of the
 * checkbox list state.
 */
export function normalizeModuleSelection(
  selected: string[],
  available: string[]
): string[] {
  const chosen = new Set(selected);
  return available.filter((name) => chosen.has(name));
}

/**
 * The selected_plugins value for POST /plugins/import: undefined when
 * no plugin list is available, the selection is empty, or it covers
 * every available plugin — all of which mean "import the whole module"
 * (absent keeps today's behavior exactly); otherwise the normalized
 * partial selection.
 */
export function selectedPluginsParam(
  selected: string[],
  available: string[]
): string[] | undefined {
  if (available.length === 0) {
    return undefined;
  }
  const normalized = normalizeModuleSelection(selected, available);
  if (normalized.length === 0 || normalized.length === available.length) {
    return undefined;
  }
  return normalized;
}

/** Maximum plugin names spelled out in the selection summary. */
const SELECTION_SUMMARY_MAX_NAMES = 8;

/**
 * The selection summary shown on the confirm step: 'All plugins' for a
 * full (or unavailable/empty) selection, otherwise
 * 'N of M plugins: name, name, ...' with long lists truncated.
 */
export function moduleSelectionSummary(
  selected: string[],
  available: string[]
): string {
  const normalized = normalizeModuleSelection(selected, available);
  if (
    available.length === 0 ||
    normalized.length === 0 ||
    normalized.length === available.length
  ) {
    return 'All plugins';
  }
  const shown = normalized.slice(0, SELECTION_SUMMARY_MAX_NAMES).join(', ');
  const more =
    normalized.length > SELECTION_SUMMARY_MAX_NAMES
      ? `, +${normalized.length - SELECTION_SUMMARY_MAX_NAMES} more`
      : '';
  return `${normalized.length} of ${available.length} plugins: ${shown}${more}`;
}

// -------------------------------------- imported-selection display
//
// Importing a subset of a plugin set records selected_plugins on the
// Plugin_Record, but the record used to look like the whole library
// was imported. These pure helpers derive the display strings the
// detail page and the library list show for that selection.

/**
 * The "Imported plugins" overview field of the plugin detail page:
 * - partial selection: 'rtsp (1 of 74 found)' — names capped like the
 *   import confirmation summary;
 * - full selection or none recorded while the enumeration exists:
 *   'All 74 plugins';
 * - single-plugin repositories (one plugin enumerated, none selected)
 *   and non-imports (no enumeration at all): null — nothing to show.
 */
export function importedPluginsSummary(
  selected: string[] | null | undefined,
  foundCount: number | null | undefined
): string | null {
  const names = selected || [];
  const found = foundCount ?? 0;
  if (names.length > 0 && (found === 0 || names.length < found)) {
    const shown = names.slice(0, SELECTION_SUMMARY_MAX_NAMES).join(', ');
    const more =
      names.length > SELECTION_SUMMARY_MAX_NAMES
        ? `, +${names.length - SELECTION_SUMMARY_MAX_NAMES} more`
        : '';
    const counts = found > 0 ? ` (${names.length} of ${found} found)` : '';
    return `${shown}${more}${counts}`;
  }
  if (found > 1) {
    return `All ${found} plugins`;
  }
  return null;
}

/**
 * The compact selection label the library list shows under an imported
 * record's name: the plugin itself for a single-plugin selection
 * ('rtsp'), 'N plugins' for a larger partial selection, and null when
 * nothing partial is recorded (full selections and non-imports stay
 * unmarked to keep the list quiet).
 */
export function importedPluginsLabel(
  selected: string[] | null | undefined,
  foundCount: number | null | undefined
): string | null {
  const names = selected || [];
  if (names.length === 0) {
    return null;
  }
  if (foundCount != null && names.length >= foundCount) {
    return null; // the whole enumeration was selected
  }
  return names.length === 1 ? names[0] : `${names.length} plugins`;
}

// -------------------------------------- platform compatibility display
//
// The import fetch records an advisory per-platform requirements check
// on the Plugin_Record (platform_compatibility: does the source's
// minimum GStreamer version work on each requested platform's build
// image?). These pure helpers derive the warning lines the detail page
// (under each incompatible architecture's build row) and the import
// view (summary Alert after the fetch settles) display. Advisory only:
// builds still queue for incompatible architectures.

/** One incompatible platform's warning line. */
export interface PlatformWarning {
  arch: string;
  message: string;
}

/**
 * The warning line for one incompatible platform entry: the recorded
 * reason (falling back to a locally built one when absent) plus, when
 * an upstream release branch is suggested, "Import revision <X> for
 * this platform instead."
 */
export function platformWarningMessage(
  arch: string,
  entry: PlatformCompatibilityEntry
): string {
  const label = ARCHITECTURE_LABELS[arch as DeviceArchitecture] || arch;
  const reason =
    entry.reason ||
    (entry.requiredVersion && entry.platformVersion
      ? `The source requires GStreamer >= ${entry.requiredVersion}; ` +
        `${label} provides ${entry.platformVersion}`
      : `This source may not be compatible with ${label}`);
  const suggestion = entry.suggestedRevision
    ? ` Import revision ${entry.suggestedRevision} for this platform instead.`
    : '';
  return `${reason}.${suggestion}`;
}

/**
 * The warning lines for every incompatible platform recorded on the
 * detail's platform_compatibility map, in architecture order. Empty
 * when the map is absent (older records, non-imports, unsettled
 * fetches) or every requested platform is compatible.
 */
export function incompatiblePlatformWarnings(
  detail: Pick<PluginVersionDetail, 'platform_compatibility'>
): PlatformWarning[] {
  const map = detail.platform_compatibility || {};
  return Object.keys(map)
    .sort()
    .filter((arch) => map[arch] && map[arch].compatible === false)
    .map((arch) => ({
      arch,
      message: platformWarningMessage(arch, map[arch]),
    }));
}

// ------------------------------------ post-import revision adjustment
//
// An incompatible platform entry carrying a suggestedRevision can be
// adjusted after the import settles: POST .../adjust-revision fetches
// (or reuses) the adjusted revision's tree and re-runs the platform's
// build. These pure helpers gate the detail page's inline action and
// validate its input, mirroring the backend's adjust_revision checks.

/**
 * True exactly when the adjust-revision action applies to one
 * architecture of a record: the detail is an imported record whose
 * import settled (import_status 'imported') and the architecture's
 * platform_compatibility entry is incompatible with a non-null
 * suggestedRevision. Mirrors the backend gate so the UI never offers
 * an action the endpoint would reject.
 */
export function canAdjustRevision(
  detail: Pick<
    PluginVersionDetail,
    'kind' | 'import_status' | 'platform_compatibility'
  >,
  arch: string
): boolean {
  if (detail.kind !== 'imported' || detail.import_status !== 'imported') {
    return false;
  }
  const entry = detail.platform_compatibility?.[arch];
  return (
    !!entry && entry.compatible === false && entry.suggestedRevision != null
  );
}

/**
 * Validate the adjust-revision input before it is submitted, mirroring
 * the backend's INVALID_REVISION check: null when the trimmed value is
 * non-empty, the error to display otherwise.
 */
export function adjustRevisionError(value: string): string | null {
  return value.trim() ? null : 'Enter a revision to import for this platform';
}

// ------------------------------------- per-architecture revisions
//
// Importing one plugin set for every platform can need DIFFERENT
// source revisions per platform generation (gst-plugins-good: main for
// the GStreamer 1.20+ platforms, branch '1.16' for arm64_jp5, '1.14'
// for arm64_jp4). ImportView offers an optional per-architecture
// revision input under the top-level Revision field; only non-empty
// overrides are sent as the import's arch_revisions map. Records of a
// multi-revision import carry arch_revisions ({arch: slug}) plus the
// per-revision fetches map, from which the detail page derives each
// architecture's revision label. All pure so they are unit-testable.

/**
 * The arch_revisions value for POST /plugins/import: the non-empty
 * (trimmed) overrides of the currently selected architectures, or
 * undefined when none remain — absent means one revision everywhere,
 * today's behavior exactly. Overrides of architectures no longer
 * selected are dropped.
 */
export function archRevisionsParam(
  overrides: Record<string, string>,
  selectedArchs: string[]
): Record<string, string> | undefined {
  const result: Record<string, string> = {};
  for (const arch of selectedArchs) {
    const value = (overrides[arch] || '').trim();
    if (value) {
      result[arch] = value;
    }
  }
  return Object.keys(result).length > 0 ? result : undefined;
}

/**
 * The confirm-step rows for the per-architecture revisions: one
 * {arch, revision} entry per selected architecture in selection order,
 * unoverridden architectures resolving to the top-level revision (or
 * 'default branch' when none was given).
 */
export function archRevisionEntries(
  overrides: Record<string, string>,
  selectedArchs: string[],
  revision: string
): Array<{ arch: string; revision: string }> {
  const fallback = revision.trim() || 'default branch';
  return selectedArchs.map((arch) => ({
    arch,
    revision: (overrides[arch] || '').trim() || fallback,
  }));
}

/**
 * The source revision one architecture's builds read from, for records
 * of a multi-revision import: arch_revisions[arch] names the slug of
 * the fetches entry whose tree the arch builds ('default' rendering as
 * 'default branch'). Null when the record has no per-arch revisions
 * (single-revision imports, other origins) or the arch is unmapped.
 */
export function archRevisionLabel(
  detail: Pick<PluginVersionDetail, 'arch_revisions' | 'fetches'>,
  arch: string
): string | null {
  const slug = detail.arch_revisions?.[arch];
  if (!slug) {
    return null;
  }
  const revision = detail.fetches?.[slug]?.revision || slug;
  return revision === 'default' ? 'default branch' : revision;
}

// ------------------------------------------------ listing failure (6.3)

/**
 * True when a listing failure carries the distinct
 * MODULE_LISTING_UNAVAILABLE code the backend returns on fetch/parse
 * failure — the UI surfaces the error and falls back to manual
 * repository URL entry as the alternative import path (Requirement 6.3).
 */
export function isModuleListingUnavailable(err: unknown): boolean {
  return err instanceof ApiError && err.code === 'MODULE_LISTING_UNAVAILABLE';
}
