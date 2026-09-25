/**
 * Import views (custom-node-designer, task 12.3).
 *
 * Repository URL form (with optional revision) and the Module_Listing
 * select populated from GET /plugin-modules with classification risk
 * badges beside module names (Requirements 5.1, 6.1, 15.1); import
 * confirmation view displaying the classification and its
 * plain-language explanation before proceeding, with required
 * acknowledgment for bad/ugly/unclassified imports (15.2, 15.3, 15.7);
 * DeepStream toggle restricting selectable architectures to arm64
 * JetPack 5/6 (5.1); listing failure surfaces the error and falls
 * back to manual URL entry (6.3).
 *
 * The import is asynchronous: POST /plugins/import answers 202 with
 * the Plugin_Record in import_status 'fetching' (the repository clone
 * runs in CodeBuild past the API Gateway 29 s cap). A progress state
 * ("Cloning repository…") shows while the record is polled every 3 s
 * (importFlow.importPollDecision) until the status settles: 'failed'
 * shows the recorded finding, 'pending_selection' opens the plugin
 * selection dialog, 'imported' navigates to the plugin detail page.
 *
 * Plugin-set imports (gst-plugins-good/bad/ugly style repositories
 * enumerating more than one plugin) land in pending_selection: a
 * selection dialog lists the enumerated plugins with checkboxes,
 * select-all/none, and a filter box, and the import completes only for
 * the selected subset (builds are submitted on confirmation).
 * Single-plugin repositories skip the selection step.
 *
 * External documentation links (Cloudscape Link external): the
 * GStreamer documentation index beside the chosen module, per-plugin
 * docs pages on every plugin name in the selection lists, and the
 * plugin-set split-up explanation in the classification container.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  ColumnLayout,
  Container,
  ExpandableSection,
  FormField,
  Header,
  Input,
  Link,
  Modal,
  Multiselect,
  MultiselectProps,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  Tiles,
  Toggle,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import { apiService } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { UseCase } from '../../types';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  Classification,
  DeviceArchitecture,
  EnumeratedPlugin,
  GitConnection,
  ModulePluginEntry,
  PluginModuleEntry,
  PluginVersionDetail,
  SyncFailureCategory,
} from './types';
import { ClassificationBadge } from './badges';
import { isValidSourcePath } from './sourcePath';
import {
  addAllToSelection,
  allPluginNames,
  archRevisionEntries,
  archRevisionsParam,
  BRANCH_ERROR_TEXT,
  CLASSIFICATION_EXPLANATIONS,
  classifyPluginSet,
  connectionNotVerifiedText,
  connectionSourceParams,
  filterPluginEntries,
  GIT_CONNECTIONS_ROUTE,
  GSTREAMER_DOCS_URL,
  GSTREAMER_PLUGIN_SETS_DOCS_URL,
  IMPORT_POLL_INTERVAL_MS,
  importFailureGuidance,
  importPollDecision,
  incompatiblePlatformWarnings,
  isModuleListingUnavailable,
  isValidBranchName,
  PlatformWarning,
  moduleSelectionIncomplete,
  moduleSelectionSummary,
  pluginDocsUrl,
  pluginSelectionError,
  requiresAcknowledgment,
  restrictArchitectureSelection,
  selectableArchitectures,
  selectedPluginsParam,
  shallowParam,
  SUBDIR_ERROR_TEXT,
  togglePluginSelection,
  verifiedConnectionOptions,
} from './importFlow';

/**
 * Where the plugin source comes from: the official Module_Listing, a
 * public repository URL, or a verified Git_Connection of the use case
 * (private-repo-plugin-import 4.1; the two public kinds are unchanged).
 */
type ImportSource = 'module' | 'manual' | 'connection';
type ImportStep = 'form' | 'confirm';

export default function ImportView() {
  const navigate = useNavigate();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);

  // --- import source: Module_Listing select or manual repository URL
  const [source, setSource] = useState<ImportSource>('module');
  const [modules, setModules] = useState<PluginModuleEntry[]>([]);
  const [modulesLoading, setModulesLoading] = useState(false);
  // Listing failure message (6.3): surfaced and manual entry forced.
  const [listingError, setListingError] = useState<string | null>(null);
  const [selectedModule, setSelectedModule] = useState<SelectProps.Option | null>(null);

  // --- Git_Connection source (private-repo-plugin-import 4.2-4.4):
  // the use case's connections (only verified ones are offered), the
  // chosen one, the optional subdirectory and branch, and the shallow
  // clone option (4.6, both source kinds; sent only when checked).
  const [connections, setConnections] = useState<GitConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [selectedConnection, setSelectedConnection] = useState<SelectProps.Option | null>(null);
  const [subdir, setSubdir] = useState('');
  const [branch, setBranch] = useState('');
  const [shallow, setShallow] = useState(false);

  // --- form fields
  const [repoUrl, setRepoUrl] = useState('');
  const [revision, setRevision] = useState('');
  const [deepstream, setDeepstream] = useState(false);
  const [architectures, setArchitectures] = useState<readonly MultiselectProps.Option[]>([]);
  // Optional per-architecture revision overrides (raw input values,
  // keyed by arch): each selected architecture may pin its own source
  // revision (e.g. gst-plugins-good branch '1.16' for arm64_jp5);
  // blank inputs follow the top-level Revision field. Only non-empty
  // overrides are sent (archRevisionsParam).
  const [archRevisions, setArchRevisions] = useState<Record<string, string>>({});

  // --- confirmation (15.2, 15.7)
  const [step, setStep] = useState<ImportStep>('form');
  const [acknowledged, setAcknowledged] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  // The recorded finding of a failed import, with the fetch
  // Failure_Category when the import went through a Git_Connection so
  // the guidance can say what to fix (3.1-3.3).
  const [importFinding, setImportFinding] = useState<{
    finding: string;
    pluginId: string;
    category?: SyncFailureCategory;
  } | null>(null);

  // --- plugin-set selection dialog: a pending_selection import lists
  // the enumerated plugins so the user picks the subset to import;
  // the import completes only for the selected plugins.
  const [selectionPrompt, setSelectionPrompt] = useState<{
    pluginId: string;
    version: number;
    found: EnumeratedPlugin[];
  } | null>(null);
  const [selectedPlugins, setSelectedPlugins] = useState<string[]>([]);
  const [pluginFilter, setPluginFilter] = useState('');
  const [selecting, setSelecting] = useState(false);
  const [selectionError, setSelectionError] = useState<string | null>(null);

  // --- asynchronous fetch progress: set after the 202 'fetching'
  // response; the record is polled until the import status settles.
  const [fetchProgress, setFetchProgress] = useState<{
    pluginId: string;
    version: number;
    startedAt: number;
  } | null>(null);

  // --- advisory platform compatibility notice: when the fetch settles
  // (pending_selection or imported) and the record's
  // platform_compatibility map marks any requested architecture
  // incompatible, a dismissible warning summarizes the incompatible
  // platforms and the revisions that would work. Never blocks the
  // import — builds queue regardless. `settled` marks a completed
  // import (navigation to the record deferred behind the notice).
  const [compatNotice, setCompatNotice] = useState<{
    pluginId: string;
    warnings: PlatformWarning[];
    settled: boolean;
  } | null>(null);

  // Load use cases on mount; restore the context selection or default
  // to the first use case (same pattern as the other list pages).
  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);

        const saved = selectedUsecaseId
          ? useCaseList.find((uc) => uc.usecase_id === selectedUsecaseId)
          : undefined;
        const chosen = saved || useCaseList[0];
        if (chosen) {
          setSelectedUseCase({ label: chosen.name, value: chosen.usecase_id });
          setSelectedUsecaseId(chosen.usecase_id);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
      }
    };
    loadUseCases();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the Module_Listing on mount (6.1). Failure surfaces the error
  // and falls back to manual repository URL entry (6.3).
  const loadModules = useCallback(async () => {
    setModulesLoading(true);
    setListingError(null);
    try {
      const response = await nodeDesignerApi.listPluginModules();
      setModules(response.modules || []);
    } catch (err: any) {
      const message = isModuleListingUnavailable(err)
        ? err.message
        : err?.message || 'Failed to load the GStreamer module listing';
      setListingError(message);
      setModules([]);
      setSource('manual');
    } finally {
      setModulesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadModules();
  }, [loadModules]);

  const moduleByName = useMemo(() => {
    const map = new Map<string, PluginModuleEntry>();
    modules.forEach((m) => map.set(m.name, m));
    return map;
  }, [modules]);

  const chosenModule = selectedModule?.value
    ? moduleByName.get(selectedModule.value)
    : undefined;

  // --- module plugin selection (import enhancement): a chosen official
  // module's individual plugins load from GET /plugin-modules?module=
  // and the user opts in to the subset to import (default: none
  // selected). Loading is non-blocking: on failure a warning shows and
  // the import proceeds with the full plugin set.
  const [modulePlugins, setModulePlugins] = useState<ModulePluginEntry[] | null>(null);
  const [modulePluginsLoading, setModulePluginsLoading] = useState(false);
  const [modulePluginsWarning, setModulePluginsWarning] = useState<string | null>(null);
  const [selectedModulePlugins, setSelectedModulePlugins] = useState<string[]>([]);

  const chosenModuleName = chosenModule?.name;
  useEffect(() => {
    setModulePlugins(null);
    setSelectedModulePlugins([]);
    setModulePluginsWarning(null);
    if (!chosenModuleName) {
      return;
    }
    let cancelled = false;
    setModulePluginsLoading(true);
    (async () => {
      try {
        const response = await nodeDesignerApi.listModulePlugins(chosenModuleName);
        if (cancelled) {
          return;
        }
        const plugins = response.plugins || [];
        setModulePlugins(plugins);
        // Default: none selected — the user opts in explicitly.
        setSelectedModulePlugins([]);
      } catch (err: any) {
        if (!cancelled) {
          // Non-blocking: selection is an enhancement, never a blocker.
          setModulePluginsWarning(
            isModuleListingUnavailable(err)
              ? err.message
              : `The plugin list for ${chosenModuleName} could not be loaded`
          );
        }
      } finally {
        if (!cancelled) {
          setModulePluginsLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chosenModuleName]);

  const modulePluginNames = modulePlugins ? allPluginNames(modulePlugins) : [];

  // --- Git connections of the use case, loaded when the connection
  // source is chosen (and reloaded when the use case changes). Only
  // verified connections are offered (4.2); the list never blocks the
  // other sources.
  const usecaseIdForConnections = source === 'connection' ? selectedUseCase?.value : undefined;
  useEffect(() => {
    setSelectedConnection(null);
    if (!usecaseIdForConnections) {
      return;
    }
    let cancelled = false;
    setConnectionsLoading(true);
    setConnectionsError(null);
    (async () => {
      try {
        const response = await nodeDesignerApi.listGitConnections(usecaseIdForConnections);
        if (!cancelled) {
          setConnections(response.connections || []);
        }
      } catch (err: any) {
        if (!cancelled) {
          setConnections([]);
          setConnectionsError(err?.message || 'Git connections could not be loaded');
        }
      } finally {
        if (!cancelled) {
          setConnectionsLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [usecaseIdForConnections]);

  const connectionOptions = verifiedConnectionOptions(connections);
  const chosenConnection = selectedConnection?.value
    ? connections.find((c) => c.connection_id === selectedConnection.value)
    : undefined;
  // Subdirectory and branch follow the backend rules exactly
  // (normalize_source_path, validate_branch_name); empty = whole tree /
  // the connection's default branch.
  const subdirError = subdir.trim() && !isValidSourcePath(subdir) ? SUBDIR_ERROR_TEXT : null;
  const branchError = !isValidBranchName(branch) ? BRANCH_ERROR_TEXT : null;

  // The effective import source repository URL: a selected module feeds
  // its published repository location into the import path (6.2); a
  // Git_Connection contributes its stored URL (shown for confirmation,
  // never sent - the backend clones the connection's URL).
  const effectiveRepoUrl =
    source === 'module'
      ? chosenModule?.repoUrl || ''
      : source === 'connection'
        ? chosenConnection?.repo_url || ''
        : repoUrl.trim();
  const effectiveModuleName = source === 'module' ? chosenModule?.name : undefined;

  // Classification shown before the import proceeds (15.2): the
  // Module_Listing entry carries it; manual URLs derive it locally.
  const classification: Classification =
    source === 'module' && chosenModule
      ? chosenModule.classification
      : classifyPluginSet(effectiveModuleName ?? null, effectiveRepoUrl || null);

  const archOptions: MultiselectProps.Option[] = selectableArchitectures(deepstream).map(
    (arch) => ({ label: ARCHITECTURE_LABELS[arch as DeviceArchitecture], value: arch })
  );

  // DeepStream toggle restricts the selectable Target_Architectures to
  // arm64 JetPack 5/6, pruning any other selection (5.1).
  const onDeepstreamChange = (checked: boolean) => {
    setDeepstream(checked);
    const kept = restrictArchitectureSelection(
      architectures.map((o) => o.value as string),
      checked
    );
    setArchitectures(architectures.filter((o) => kept.includes(o.value as string)));
  };

  const selectedArchValues = architectures.map((o) => o.value as string);
  // The per-arch revision overrides the import sends: only non-empty
  // values for currently selected architectures (undefined = one
  // revision everywhere, today's behavior exactly).
  const archRevisionOverrides = archRevisionsParam(archRevisions, selectedArchValues);

  // A loaded plugin list requires at least one selected plugin; an
  // unavailable or empty list never blocks the import.
  const selectionIncomplete = moduleSelectionIncomplete(
    source,
    modulePluginNames,
    selectedModulePlugins
  );

  // A connection import needs a chosen (verified) connection and valid
  // optional subdirectory / branch inputs; the public kinds need a URL.
  const sourceComplete =
    source === 'connection'
      ? !!chosenConnection && !subdirError && !branchError
      : !!effectiveRepoUrl;

  const formComplete =
    !!selectedUseCase?.value &&
    sourceComplete &&
    architectures.length > 0 &&
    !selectionIncomplete;

  // Act on a settled import record (from the initial response or a
  // poll): 'failed' shows the recorded finding (4.5),
  // 'pending_selection' opens the plugin selection dialog, 'imported'
  // navigates to the plugin detail page.
  const settleImport = useCallback(
    (plugin: PluginVersionDetail, startedAt: number): boolean => {
      const decision = importPollDecision(plugin, Date.now() - startedAt);
      if (decision.kind === 'wait') {
        return false;
      }
      setFetchProgress(null);
      setImporting(false);
      if (decision.kind === 'timeout') {
        setImportError(
          'The repository fetch did not finish in time. Open the plugin ' +
            'record later to check its import status, or retry the import.'
        );
      } else if (decision.kind === 'failed') {
        setImportFinding({
          finding: decision.finding,
          pluginId: plugin.plugin_id,
          ...(decision.category ? { category: decision.category } : {}),
        });
      } else if (decision.kind === 'select') {
        // Plugin set: open the selection dialog over the enumerated
        // plugins; builds are submitted once the subset is confirmed.
        // Any per-platform incompatibility recorded on the settled
        // record surfaces as an advisory warning alongside the dialog.
        const warnings = incompatiblePlatformWarnings(plugin);
        if (warnings.length > 0) {
          setCompatNotice({
            pluginId: plugin.plugin_id,
            warnings,
            settled: false,
          });
        }
        setSelectedPlugins([]);
        setPluginFilter('');
        setSelectionError(null);
        setSelectionPrompt({
          pluginId: plugin.plugin_id,
          version: plugin.version,
          found: plugin.plugins_found || [],
        });
      } else {
        // Imported: surface any recorded platform incompatibility
        // before leaving the import view (the warning offers the
        // plugin record as its action; dismissing it navigates too) —
        // otherwise go straight to the record as before.
        const warnings = incompatiblePlatformWarnings(plugin);
        if (warnings.length > 0) {
          setCompatNotice({
            pluginId: plugin.plugin_id,
            warnings,
            settled: true,
          });
        } else {
          navigate(`/node-designer/plugins/${plugin.plugin_id}`);
        }
      }
      return true;
    },
    [navigate]
  );

  // Poll the record every 3 s while the repository fetch runs (the
  // fetch project times out at 10 minutes; the poll gives up at ~12).
  useEffect(() => {
    if (!fetchProgress) {
      return;
    }
    let cancelled = false;
    const timer = setInterval(async () => {
      let plugin: PluginVersionDetail;
      try {
        ({ plugin } = await nodeDesignerApi.getVersion(
          fetchProgress.pluginId,
          fetchProgress.version
        ));
      } catch {
        return; // transient poll failure: keep polling
      }
      if (!cancelled) {
        settleImport(plugin, fetchProgress.startedAt);
      }
    }, IMPORT_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [fetchProgress, settleImport]);

  const startImport = async () => {
    if (!selectedUseCase?.value || !sourceComplete) {
      return;
    }
    setImporting(true);
    setImportError(null);
    setImportFinding(null);
    // Partial module plugin selection only; a full (or unavailable)
    // selection sends nothing = whole module, today's behavior.
    const selectedParam =
      source === 'module'
        ? selectedPluginsParam(selectedModulePlugins, modulePluginNames)
        : undefined;
    try {
      const response = await nodeDesignerApi.importPlugin({
        usecase_id: selectedUseCase.value,
        // Exactly one source (1.2): the connection id in place of the
        // URL for a Git_Connection import; the token never leaves the
        // backend. Public kinds send exactly what they always have.
        ...(source === 'connection' && chosenConnection
          ? connectionSourceParams(chosenConnection.connection_id, subdir, branch)
          : { repo_url: effectiveRepoUrl }),
        ...(revision.trim() ? { revision: revision.trim() } : {}),
        architectures: selectedArchValues,
        ...(archRevisionOverrides ? { arch_revisions: archRevisionOverrides } : {}),
        deepstream,
        ...(effectiveModuleName ? { module_name: effectiveModuleName } : {}),
        ...(selectedParam ? { selected_plugins: selectedParam } : {}),
        ...shallowParam(shallow),
      });
      const startedAt = Date.now();
      if (!settleImport(response.plugin, startedAt)) {
        // 202 'fetching': show the progress state and poll the record
        // until the import status settles (importing stays true).
        setFetchProgress({
          pluginId: response.plugin.plugin_id,
          version: response.plugin.version,
          startedAt,
        });
      }
    } catch (err: any) {
      // A not-verified connection is rejected with its current status
      // (3.5); the import never re-verifies as a side effect.
      setImportError(
        err?.code === 'CONNECTION_NOT_VERIFIED'
          ? connectionNotVerifiedText(chosenConnection?.name, err?.details?.status)
          : err?.message || 'The import request failed'
      );
      setImporting(false);
    }
  };

  // Confirm the plugin selection: record the chosen subset and submit
  // builds; the import completes only for the selected plugins.
  const confirmSelection = async () => {
    if (!selectionPrompt) {
      return;
    }
    setSelecting(true);
    setSelectionError(null);
    try {
      await nodeDesignerApi.selectImportPlugins(
        selectionPrompt.pluginId,
        selectionPrompt.version,
        selectedPlugins
      );
      navigate(`/node-designer/plugins/${selectionPrompt.pluginId}`);
    } catch (err: any) {
      setSelectionError(err?.message || 'Recording the plugin selection failed');
    } finally {
      setSelecting(false);
    }
  };

  const visiblePlugins = selectionPrompt
    ? filterPluginEntries(selectionPrompt.found, pluginFilter)
    : [];

  // ------------------------------------------------- confirmation view

  if (step === 'confirm') {
    const needsAck = requiresAcknowledgment(classification);
    return (
      <SpaceBetween size="l">
        {selectionPrompt && (
          <Modal
            visible
            onDismiss={() => setSelectionPrompt(null)}
            header="Select plugins to import"
            footer={
              <Box float="right">
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => setSelectionPrompt(null)} disabled={selecting}>
                    Cancel
                  </Button>
                  <Button
                    variant="primary"
                    loading={selecting}
                    disabled={
                      pluginSelectionError(selectedPlugins, selectionPrompt.found) !== null
                    }
                    onClick={confirmSelection}
                  >
                    Import selected plugins
                  </Button>
                </SpaceBetween>
              </Box>
            }
          >
            <SpaceBetween size="m">
              <Box>
                This repository is a plugin set containing{' '}
                {selectionPrompt.found.length} plugins. Choose which plugins to
                import; only the selected plugins are built.
              </Box>
              {selectionError && (
                <Alert type="error" dismissible onDismiss={() => setSelectionError(null)}>
                  {selectionError}
                </Alert>
              )}
              <Input
                type="search"
                value={pluginFilter}
                onChange={({ detail }) => setPluginFilter(detail.value)}
                placeholder="Find plugins"
                ariaLabel="Find plugins"
              />
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  onClick={() =>
                    setSelectedPlugins(addAllToSelection(selectedPlugins, visiblePlugins))
                  }
                >
                  Select all
                </Button>
                <Button onClick={() => setSelectedPlugins([])}>Select none</Button>
                <Box color="text-body-secondary">
                  {selectedPlugins.length} of {selectionPrompt.found.length} selected
                </Box>
              </SpaceBetween>
              <div style={{ maxHeight: 320, overflowY: 'auto' }}>
                <SpaceBetween size="xxs">
                  {visiblePlugins.map((entry) => (
                    <Checkbox
                      key={`${entry.path}/${entry.name}`}
                      checked={selectedPlugins.includes(entry.name)}
                      onChange={() =>
                        setSelectedPlugins(togglePluginSelection(selectedPlugins, entry.name))
                      }
                      description={entry.path || undefined}
                    >
                      {/* The plugin name links to its official docs page;
                          the brief description sits inline to the right so
                          the list scans quickly. */}
                      <Link external href={pluginDocsUrl(entry.name)}>
                        {entry.name}
                      </Link>
                      {entry.description && (
                        <Box
                          variant="span"
                          color="text-body-secondary"
                          fontSize="body-s"
                        >
                          {' — '}
                          {entry.description}
                        </Box>
                      )}
                    </Checkbox>
                  ))}
                </SpaceBetween>
              </div>
            </SpaceBetween>
          </Modal>
        )}
        <Header
          variant="h1"
          description="Review the plugin's upstream classification before the import proceeds."
        >
          Confirm plugin import
        </Header>

        {importError && (
          <Alert type="error" dismissible onDismiss={() => setImportError(null)}>
            {importError}
          </Alert>
        )}

        {fetchProgress && (
          <Alert type="info" header="Import in progress">
            <SpaceBetween direction="horizontal" size="xs">
              <Spinner />
              <span>
                Cloning repository… large repositories can take several
                minutes.
              </span>
            </SpaceBetween>
          </Alert>
        )}

        {importFinding && (
          <Alert
            type="error"
            header={importFailureGuidance(importFinding.category).header}
            action={
              <Button
                onClick={() => navigate(`/node-designer/plugins/${importFinding.pluginId}`)}
              >
                View plugin record
              </Button>
            }
          >
            <SpaceBetween size="xs">
              {/* Category guidance for a Git_Connection fetch failure
                  (3.2, 3.3): what to fix and, for a rejected token,
                  where to re-verify - never a re-verification here. */}
              {importFailureGuidance(importFinding.category).guidance && (
                <div>
                  {importFailureGuidance(importFinding.category).guidance}
                  {importFailureGuidance(importFinding.category).linkGitConnections && (
                    <>
                      {' '}
                      <Link
                        href={GIT_CONNECTIONS_ROUTE}
                        onFollow={(event) => {
                          event.preventDefault();
                          navigate(GIT_CONNECTIONS_ROUTE);
                        }}
                      >
                        Open Git connections
                      </Link>
                    </>
                  )}
                </div>
              )}
              <div>{importFinding.finding}</div>
            </SpaceBetween>
          </Alert>
        )}

        {/* Advisory per-platform compatibility warning: the fetched
            source's GStreamer requirement exceeds what some requested
            platforms provide. Builds still queue — the user may know
            better — but the why and the working revision are spelled
            out so failed builds aren't blindly retried. */}
        {compatNotice && (
          <Alert
            type="warning"
            header="Some target platforms may not be compatible"
            dismissible
            onDismiss={() => {
              const notice = compatNotice;
              setCompatNotice(null);
              if (notice.settled) {
                navigate(`/node-designer/plugins/${notice.pluginId}`);
              }
            }}
            action={
              compatNotice.settled ? (
                <Button
                  onClick={() =>
                    navigate(`/node-designer/plugins/${compatNotice.pluginId}`)
                  }
                >
                  View plugin record
                </Button>
              ) : undefined
            }
          >
            <SpaceBetween size="xs">
              {compatNotice.warnings.map((warning) => (
                <div key={warning.arch}>{warning.message}</div>
              ))}
            </SpaceBetween>
          </Alert>
        )}

        <Container header={<Header variant="h2">Import details</Header>}>
          <ColumnLayout columns={2} variant="text-grid">
            {source === 'connection' && chosenConnection && (
              <div>
                <Box variant="awsui-key-label">Git connection</Box>
                <div>{chosenConnection.name}</div>
              </div>
            )}
            <div>
              <Box variant="awsui-key-label">Repository URL</Box>
              <div>{effectiveRepoUrl}</div>
            </div>
            {source === 'connection' && chosenConnection && (
              <div>
                <Box variant="awsui-key-label">Branch</Box>
                <div>{branch.trim() || chosenConnection.default_branch}</div>
              </div>
            )}
            {source === 'connection' && (
              <div>
                <Box variant="awsui-key-label">Subdirectory</Box>
                <div>{subdir.trim() || 'whole repository'}</div>
              </div>
            )}
            <div>
              <Box variant="awsui-key-label">Revision</Box>
              <div>
                {revision.trim() ||
                  (source === 'connection' ? 'branch head' : 'default branch')}
              </div>
            </div>
            {shallow && (
              <div>
                <Box variant="awsui-key-label">Clone depth</Box>
                <div>shallow (depth 1)</div>
              </div>
            )}
            {archRevisionOverrides && (
              <div>
                <Box variant="awsui-key-label">Per-architecture revisions</Box>
                {/* Every selected architecture with its effective
                    revision (override or the top-level revision). */}
                {archRevisionEntries(archRevisions, selectedArchValues, revision).map(
                  (entry) => (
                    <div key={entry.arch}>
                      {ARCHITECTURE_LABELS[entry.arch as DeviceArchitecture] || entry.arch}
                      {': '}
                      {entry.revision}
                    </div>
                  )
                )}
              </div>
            )}
            {effectiveModuleName && (
              <div>
                <Box variant="awsui-key-label">Module</Box>
                <div>{effectiveModuleName}</div>
              </div>
            )}
            {effectiveModuleName && (
              <div>
                <Box variant="awsui-key-label">Plugins</Box>
                {/* 'All plugins' or 'N of M plugins: names...' */}
                <div>
                  {moduleSelectionSummary(selectedModulePlugins, modulePluginNames)}
                </div>
              </div>
            )}
            <div>
              <Box variant="awsui-key-label">Target architectures</Box>
              <div>{architectures.map((o) => o.label).join(', ')}</div>
            </div>
            <div>
              <Box variant="awsui-key-label">DeepStream plugin</Box>
              <div>{deepstream ? 'yes' : 'no'}</div>
            </div>
          </ColumnLayout>
        </Container>

        <Container header={<Header variant="h2">Upstream classification</Header>}>
          <SpaceBetween size="m">
            <SpaceBetween direction="horizontal" size="xs">
              <ClassificationBadge classification={classification} />
            </SpaceBetween>
            {/* Plain-language explanation, verbatim per Requirement 15.3 */}
            <Box>{CLASSIFICATION_EXPLANATIONS[classification]}</Box>
            <Link external href={GSTREAMER_PLUGIN_SETS_DOCS_URL}>
              Learn more about GStreamer plugin sets
            </Link>
            {needsAck && (
              <Checkbox
                checked={acknowledged}
                onChange={({ detail }) => setAcknowledged(detail.checked)}
              >
                I acknowledge the classification explanation above and want to
                import this plugin anyway.
              </Checkbox>
            )}
          </SpaceBetween>
        </Container>

        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={() => setStep('form')} disabled={importing}>
            Back
          </Button>
          <Button
            variant="primary"
            loading={importing}
            // Required acknowledgment for bad/ugly/unclassified (15.7)
            disabled={needsAck && !acknowledged}
            onClick={startImport}
          >
            Import plugin
          </Button>
        </SpaceBetween>
      </SpaceBetween>
    );
  }

  // -------------------------------------------------------- form view

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Import a GStreamer plugin from the official module listing, a public repository URL, or a private repository through one of this use case's Git connections."
      >
        Import plugin
      </Header>

      {listingError && (
        <Alert
          type="error"
          header="Module listing unavailable"
          action={<Button onClick={loadModules}>Retry</Button>}
        >
          {listingError} — enter a repository URL manually below to import a
          plugin.
        </Alert>
      )}

      <Container header={<Header variant="h2">Import source</Header>}>
        <SpaceBetween size="m">
          <FormField label="Use case">
            <Select
              placeholder="Select use case"
              selectedOption={selectedUseCase}
              options={useCases.map((uc) => ({ label: uc.name, value: uc.usecase_id }))}
              onChange={({ detail }) => {
                setSelectedUseCase(detail.selectedOption);
                if (detail.selectedOption.value) {
                  setSelectedUsecaseId(detail.selectedOption.value);
                }
              }}
            />
          </FormField>

          <Tiles
            value={source}
            onChange={({ detail }) => setSource(detail.value as ImportSource)}
            items={[
              {
                value: 'module',
                label: 'Official GStreamer module',
                description:
                  'Pick a well-known module from the official GStreamer module listing.',
                disabled: !!listingError,
              },
              {
                value: 'manual',
                label: 'Repository URL',
                description: 'Import from a public source repository URL.',
              },
              {
                value: 'connection',
                label: 'Git connection',
                description:
                  'Import from a private repository through a verified Git connection of this use case.',
              },
            ]}
          />

          {source === 'connection' && (
            <>
              {connectionsError && (
                <Alert type="error" header="Git connections unavailable">
                  {connectionsError}
                </Alert>
              )}
              <FormField
                label="Git connection"
                description="Only verified connections of the selected use case can be imported through; each shows its repository URL and default branch. The connection's token stays in the portal and is never shown here."
              >
                <Select
                  placeholder="Select a verified Git connection"
                  statusType={connectionsLoading ? 'loading' : 'finished'}
                  loadingText="Loading Git connections"
                  selectedOption={selectedConnection}
                  options={connectionOptions}
                  onChange={({ detail }) => {
                    setSelectedConnection(detail.selectedOption);
                    // Pre-fill the branch with the connection's default (4.4).
                    const picked = connections.find(
                      (c) => c.connection_id === detail.selectedOption.value
                    );
                    setBranch(picked?.default_branch || '');
                  }}
                  empty={
                    connectionsLoading ? (
                      'Loading Git connections'
                    ) : (
                      <span>
                        No verified Git connections for this use case.{' '}
                        <Link
                          href={GIT_CONNECTIONS_ROUTE}
                          onFollow={(event) => {
                            event.preventDefault();
                            navigate(GIT_CONNECTIONS_ROUTE);
                          }}
                        >
                          Create or verify one on the Git connections page
                        </Link>
                        .
                      </span>
                    )
                  }
                  ariaLabel="Git connection"
                />
              </FormField>
              {!connectionsLoading && !connectionsError && connectionOptions.length === 0 && (
                <Alert type="info" header="No verified Git connections">
                  This use case has no verified Git connection yet.{' '}
                  <Link
                    href={GIT_CONNECTIONS_ROUTE}
                    onFollow={(event) => {
                      event.preventDefault();
                      navigate(GIT_CONNECTIONS_ROUTE);
                    }}
                  >
                    Open Git connections
                  </Link>{' '}
                  to add one and verify its token, then return here to import.
                </Alert>
              )}
              <FormField
                label={
                  <span>
                    Subdirectory <i>- optional</i>
                  </span>
                }
                description="Repository subdirectory holding the plugin source; leave empty to import the whole repository."
                errorText={subdirError}
              >
                <Input
                  value={subdir}
                  onChange={({ detail }) => setSubdir(detail.value)}
                  placeholder="plugins/my-element"
                  ariaLabel="Subdirectory"
                />
              </FormField>
              <FormField
                label={
                  <span>
                    Branch <i>- optional</i>
                  </span>
                }
                description="Branch to clone; pre-filled with the connection's default branch. The imported version is linked to this branch for later push and pull."
                errorText={branchError}
              >
                <Input
                  value={branch}
                  onChange={({ detail }) => setBranch(detail.value)}
                  placeholder={chosenConnection?.default_branch || 'main'}
                  ariaLabel="Branch"
                />
              </FormField>
            </>
          )}

          {source === 'manual' && (
            <FormField
              label="Repository URL"
              description="Public http, https, or git repository containing the plugin source."
            >
              <Input
                value={repoUrl}
                onChange={({ detail }) => setRepoUrl(detail.value)}
                placeholder="https://example.com/my-gst-plugin.git"
              />
            </FormField>
          )}

          {source === 'module' && (
            <>
              <FormField
                label="Module"
                description="Modules from the official GStreamer module listing, with each module's upstream classification shown beside its name."
              >
                <SpaceBetween size="xs">
                  <Select
                    placeholder="Select a GStreamer module"
                    statusType={modulesLoading ? 'loading' : 'finished'}
                    loadingText="Loading module listing"
                    filteringType="auto"
                    selectedOption={selectedModule}
                    // Classification risk indicator beside each module
                    // name in the list (15.1).
                    options={modules.map((m) => ({
                      label: m.name,
                      value: m.name,
                      labelTag: m.classification,
                      description: m.description,
                    }))}
                    onChange={({ detail }) => setSelectedModule(detail.selectedOption)}
                  />
                  {chosenModule && (
                    <SpaceBetween direction="horizontal" size="xs">
                      <ClassificationBadge classification={chosenModule.classification} />
                      <Box color="text-body-secondary" fontSize="body-s">
                        {chosenModule.repoUrl}
                      </Box>
                      <Link external href={GSTREAMER_DOCS_URL}>
                        GStreamer documentation
                      </Link>
                    </SpaceBetween>
                  )}
                </SpaceBetween>
              </FormField>
              {/* Plugin selection for the chosen module (default: none
                  selected — the user opts in explicitly). An unavailable
                  plugin list never blocks the import: the warning shows
                  and the full set imports. */}
              {chosenModule && modulePluginsWarning && (
                <Alert type="warning" header="Plugin list unavailable">
                  {modulePluginsWarning} — the import will include the
                  module's full plugin set.
                </Alert>
              )}
              {chosenModule && modulePluginsLoading && (
                <Box color="text-body-secondary">Loading plugin list…</Box>
              )}
              {chosenModule && modulePlugins && modulePlugins.length > 0 && (
                <FormField
                  label="Plugins to import"
                  description="Choose which of the module's plugins to import and build. No plugins are selected by default — select individual plugins or use Select all to import the whole module."
                  errorText={
                    selectedModulePlugins.length === 0
                      ? 'Select at least one plugin to import'
                      : undefined
                  }
                >
                  <SpaceBetween size="xs">
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button
                        onClick={() =>
                          setSelectedModulePlugins(allPluginNames(modulePlugins))
                        }
                      >
                        Select all
                      </Button>
                      <Button onClick={() => setSelectedModulePlugins([])}>
                        Clear
                      </Button>
                      <Box color="text-body-secondary">
                        {selectedModulePlugins.length} of {modulePlugins.length}{' '}
                        selected
                      </Box>
                    </SpaceBetween>
                    <div style={{ maxHeight: 260, overflowY: 'auto' }}>
                      <SpaceBetween size="xxs">
                        {modulePlugins.map((plugin) => (
                          <Checkbox
                            key={plugin.name}
                            checked={selectedModulePlugins.includes(plugin.name)}
                            onChange={() =>
                              setSelectedModulePlugins(
                                togglePluginSelection(
                                  selectedModulePlugins,
                                  plugin.name
                                )
                              )
                            }
                          >
                            {/* The plugin name links to its official docs
                                page; the brief description sits inline to
                                the right so the list scans quickly. */}
                            <Link external href={pluginDocsUrl(plugin.name)}>
                              {plugin.name}
                            </Link>
                            {plugin.description && (
                              <Box
                                variant="span"
                                color="text-body-secondary"
                                fontSize="body-s"
                              >
                                {' — '}
                                {plugin.description}
                              </Box>
                            )}
                          </Checkbox>
                        ))}
                      </SpaceBetween>
                    </div>
                  </SpaceBetween>
                </FormField>
              )}
            </>
          )}

          <FormField
            label={
              <span>
                Revision <i>- optional</i>
              </span>
            }
            description={
              source === 'connection'
                ? 'Tag or commit to check out after cloning the branch; the branch head is imported when omitted. The link stays on the branch.'
                : 'Branch, tag, or commit to import; the repository default branch is used when omitted.'
            }
          >
            <Input
              value={revision}
              onChange={({ detail }) => setRevision(detail.value)}
              placeholder="main"
            />
          </FormField>

          {/* Optional per-architecture revision overrides: platform
              generations can need different source branches (e.g.
              gst-plugins-good main for the GStreamer 1.20+ platforms,
              '1.16' for arm64 CPU and arm64 JetPack 5).
              Blank inputs follow the Revision field above; only
              non-empty overrides are sent. */}
          <ExpandableSection
            headerText="Per-architecture revisions"
            headerDescription="Optionally import a different branch, tag, or commit per target architecture."
          >
            {selectedArchValues.length === 0 ? (
              <Box color="text-body-secondary">
                Select target architectures below to override their revisions.
              </Box>
            ) : (
              <SpaceBetween size="s">
                {selectedArchValues.map((arch) => (
                  <FormField
                    key={arch}
                    label={ARCHITECTURE_LABELS[arch as DeviceArchitecture] || arch}
                  >
                    <Input
                      value={archRevisions[arch] || ''}
                      onChange={({ detail }) =>
                        setArchRevisions((current) => ({
                          ...current,
                          [arch]: detail.value,
                        }))
                      }
                      placeholder={revision.trim() || 'default branch'}
                      ariaLabel={`Revision for ${
                        ARCHITECTURE_LABELS[arch as DeviceArchitecture] || arch
                      }`}
                    />
                  </FormField>
                ))}
              </SpaceBetween>
            )}
          </ExpandableSection>

          {/* Shallow clone (private-repo-plugin-import 4.6): offered for
              every source kind, unchecked by default, sent only when
              checked so unchecked requests are unchanged. */}
          <Checkbox
            checked={shallow}
            onChange={({ detail }) => setShallow(detail.checked)}
            description="Clone at depth 1: faster for large repositories. A revision that is a bare commit hash may not be reachable from a shallow clone."
          >
            Shallow clone
          </Checkbox>
        </SpaceBetween>
      </Container>

      <Container header={<Header variant="h2">Build targets</Header>}>
        <SpaceBetween size="m">
          <Toggle checked={deepstream} onChange={({ detail }) => onDeepstreamChange(detail.checked)}>
            NVIDIA DeepStream plugin (restricts targets to arm64 JetPack 5/6)
          </Toggle>
          <FormField
            label="Target architectures"
            description={
              deepstream
                ? 'DeepStream plugins target Jetson devices: arm64 JetPack 5 and 6.'
                : 'Architectures the plugin is built for.'
            }
          >
            <Multiselect
              placeholder="Select target architectures"
              selectedOptions={architectures}
              options={archOptions}
              onChange={({ detail }) => setArchitectures(detail.selectedOptions)}
            />
          </FormField>
        </SpaceBetween>
      </Container>

      <SpaceBetween direction="horizontal" size="xs">
        <Button onClick={() => navigate('/node-designer')}>Cancel</Button>
        <Button
          variant="primary"
          disabled={!formComplete}
          onClick={() => {
            setAcknowledged(false);
            setImportError(null);
            setImportFinding(null);
            setStep('confirm');
          }}
        >
          Review import
        </Button>
      </SpaceBetween>
    </SpaceBetween>
  );
}
