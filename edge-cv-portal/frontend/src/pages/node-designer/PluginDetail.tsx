/**
 * Plugin_Record detail (custom-node-designer, Requirements 3.5, 10.2).
 *
 * One record's latest version: lifecycle and classification badges,
 * per-arch build status with the failing build's log excerpt, version
 * history, provenance, and source file inspection.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Multiselect,
  Select,
  SelectProps,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { useNavigate, useParams } from 'react-router-dom';
import ConfirmationModal from '../../components/ConfirmationModal';
import { nodeDesignerApi } from './api';
import {
  ARCHITECTURE_LABELS,
  DEEPSTREAM_ARCHITECTURES,
  DEVICE_ARCHITECTURES,
  PluginBuildsView,
  PluginRecordSummary,
  PluginVersionDetail,
} from './types';
import { BuildStatusIndicator, ClassificationBadge, LifecycleBadge, logExcerpt } from './badges';
import {
  adjustRevisionError,
  archRevisionLabel,
  canAdjustRevision,
  importedPluginsSummary,
  platformWarningMessage,
} from './importFlow';
import RegistrationPrompt from './RegistrationPrompt';

/** Poll builds every 10 s while any requested build is still running. */
const BUILD_POLL_MS = 10_000;

export default function PluginDetail() {
  const navigate = useNavigate();
  const { pluginId } = useParams<{ pluginId: string }>();
  const [plugin, setPlugin] = useState<PluginVersionDetail | null>(null);
  const [versions, setVersions] = useState<PluginRecordSummary[]>([]);
  const [builds, setBuilds] = useState<PluginBuildsView | null>(null);
  const [sourceFiles, setSourceFiles] = useState<Array<{ file: string; size: number }>>([]);
  const [selectedFile, setSelectedFile] = useState<SelectProps.Option | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Architectures with an in-flight retry request (per-arch buttons and
  // the retry-all action share this so double submission is blocked).
  const [retrying, setRetrying] = useState<string[]>([]);
  const [retryError, setRetryError] = useState<string | null>(null);
  // Manual build submission (the Build header action): a generated or
  // scaffold record accepted without a build round has no artifacts yet,
  // so the page offers an explicit build trigger with an architecture
  // selection (defaulted from the declaration / last build round).
  const [buildPanelOpen, setBuildPanelOpen] = useState(false);
  const [buildArchs, setBuildArchs] = useState<string[]>([]);
  const [buildSubmitting, setBuildSubmitting] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);
  // Post-import revision adjustment (incompatible platforms carrying a
  // suggestedRevision): which architecture's inline input is open, its
  // editable value, the in-flight flag, and per-arch errors surfaced
  // on the affected platform's entry only.
  const [adjustingArch, setAdjustingArch] = useState<string | null>(null);
  const [adjustValue, setAdjustValue] = useState('');
  const [adjustSubmitting, setAdjustSubmitting] = useState(false);
  const [adjustErrors, setAdjustErrors] = useState<Record<string, string>>({});
  // Record deletion (bad/duplicate imports): confirmation modal state.
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // Lifecycle transitions (dev -> test -> prod and back): in-flight
  // flag plus the 409 gate rejection (missing build / missing review).
  const [transitioning, setTransitioning] = useState(false);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!pluginId) return;
    setError(null);
    try {
      const response = await nodeDesignerApi.getPlugin(pluginId);
      setPlugin(response.plugin);
      setVersions(response.versions || []);
      const [buildsView, source] = await Promise.all([
        nodeDesignerApi.getBuilds(pluginId, response.plugin.version).catch(() => null),
        nodeDesignerApi
          .getVersionSource(pluginId, response.plugin.version)
          .catch(() => null),
      ]);
      setBuilds(buildsView);
      setSourceFiles(source?.files || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load the plugin record');
    } finally {
      setLoading(false);
    }
  }, [pluginId]);

  useEffect(() => {
    load();
  }, [load]);

  // Keep build status fresh while builds are in flight (3.5).
  useEffect(() => {
    if (!pluginId || !plugin || !builds || builds.settled) {
      return;
    }
    const timer = setInterval(async () => {
      try {
        setBuilds(await nodeDesignerApi.getBuilds(pluginId, plugin.version));
      } catch {
        // transient poll failure: keep the last known status
      }
    }, BUILD_POLL_MS);
    return () => clearInterval(timer);
  }, [pluginId, plugin, builds]);

  const showFile = async (option: SelectProps.Option) => {
    setSelectedFile(option);
    setFileContent(null);
    if (!pluginId || !plugin || !option.value) return;
    try {
      const response = await nodeDesignerApi.getVersionSource(
        pluginId,
        plugin.version,
        option.value
      );
      setFileContent(response.content ?? '');
    } catch (err: any) {
      setFileContent(`Failed to load file: ${err.message}`);
    }
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (error || !plugin) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{error || 'Plugin record not found'}</Alert>
        <Button onClick={() => navigate('/node-designer')}>Back to Node Designer</Button>
      </SpaceBetween>
    );
  }

  const classification = (plugin.provenance?.classification as string) || null;
  const buildEntries = builds?.builds || plugin.artifacts || {};
  const failedArchs = Object.keys(buildEntries)
    .filter((arch) => (buildEntries[arch] || {}).buildStatus === 'failed')
    .sort();

  // Re-submit failed architectures to the Plugin_Build_Service (the
  // build endpoint re-StartBuilds any architecture list; per-arch
  // status flips back to building and the poll picks up the outcome).
  const retryBuilds = async (architectures: string[]) => {
    if (!pluginId || !plugin || architectures.length === 0) return;
    setRetrying(architectures);
    setRetryError(null);
    try {
      const view = await nodeDesignerApi.startBuilds(
        pluginId,
        plugin.version,
        architectures
      );
      setBuilds(view);
    } catch (err: any) {
      setRetryError(err?.message || 'The build retry could not be started');
    } finally {
      setRetrying([]);
    }
  };

  // Architectures the Build panel may target: DeepStream-flagged
  // records are restricted to the JetPack builds (the backend enforces
  // the same rule, Requirement 5.1).
  const buildableArchitectures: string[] = plugin.deepstream
    ? [...DEEPSTREAM_ARCHITECTURES]
    : [...DEVICE_ARCHITECTURES];

  // Default architecture selection for a new build round: the last
  // round's requested architectures when one exists, else the
  // Target_Architectures of the recorded scaffold declaration (create
  // wizard and generate-and-accept records both carry it in
  // provenance.scaffoldDeclaration), else x86_64.
  const defaultBuildArchitectures = (): string[] => {
    const requested = (builds?.requested_architectures || []).filter((arch) =>
      buildableArchitectures.includes(arch)
    );
    if (requested.length > 0) return requested;
    const raw = plugin.provenance?.scaffoldDeclaration;
    if (typeof raw === 'string') {
      try {
        const declared = JSON.parse(raw)?.architectures;
        if (Array.isArray(declared)) {
          const valid = declared.filter(
            (arch): arch is string =>
              typeof arch === 'string' && buildableArchitectures.includes(arch)
          );
          if (valid.length > 0) return valid;
        }
      } catch {
        // unparseable provenance: fall through to the default
      }
    }
    return buildableArchitectures.includes('x86_64') ? ['x86_64'] : [];
  };

  const openBuildPanel = () => {
    setBuildArchs(defaultBuildArchitectures());
    setBuildError(null);
    setBuildPanelOpen(true);
  };

  // Submit a build round for the selected Target_Architectures via the
  // existing build endpoint; the response's builds view is unsettled,
  // so the status poll resumes automatically.
  const startBuild = async () => {
    if (!pluginId || !plugin || buildArchs.length === 0) return;
    setBuildSubmitting(true);
    setBuildError(null);
    try {
      const view = await nodeDesignerApi.startBuilds(
        pluginId,
        plugin.version,
        buildArchs
      );
      setBuilds(view);
      setBuildPanelOpen(false);
    } catch (err: any) {
      setBuildError(err?.message || 'The build could not be started');
    } finally {
      setBuildSubmitting(false);
    }
  };

  const clearAdjustError = (arch: string) =>
    setAdjustErrors(({ [arch]: _dropped, ...rest }) => rest);

  // Open the inline adjust-revision input for one architecture,
  // pre-filled with the recorded suggestedRevision (editable).
  const openAdjust = (arch: string, suggested: string) => {
    setAdjustingArch(arch);
    setAdjustValue(suggested);
    clearAdjustError(arch);
  };

  // Apply the per-platform revision adjustment: POST .../adjust-revision
  // fetches (or reuses) the adjusted revision's tree and re-runs the
  // platform's build. The response carries the refreshed record and
  // builds view; the build poll resumes because the view is no longer
  // settled. Errors surface on the affected platform's entry only.
  const applyAdjustment = async (arch: string) => {
    if (!pluginId || !plugin) return;
    const validation = adjustRevisionError(adjustValue);
    if (validation) {
      setAdjustErrors((prev) => ({ ...prev, [arch]: validation }));
      return;
    }
    setAdjustSubmitting(true);
    clearAdjustError(arch);
    try {
      const response = await nodeDesignerApi.adjustRevision(
        pluginId,
        plugin.version,
        arch,
        adjustValue.trim()
      );
      setPlugin(response.plugin);
      setBuilds(response.builds);
      setAdjustingArch(null);
    } catch (err: any) {
      setAdjustErrors((prev) => ({
        ...prev,
        [arch]: err?.message || 'The revision adjustment could not be applied',
      }));
    } finally {
      setAdjustSubmitting(false);
    }
  };
  // Which plugins an import covers ('rtsp (1 of 74 found)' for a
  // partial selection, 'All 74 plugins' otherwise); null for
  // non-imports and unsettled fetches.
  const importedPlugins = importedPluginsSummary(
    plugin.selected_plugins,
    plugin.plugins_found?.length
  );

  // Promote (dev -> test -> prod) or demote (prod -> test -> dev) the
  // version. Gate rejections (missing successful build for dev -> test,
  // missing approved security review for test -> prod) come back as 409
  // and surface in the lifecycle alert.
  const changeLifecycle = async (direction: 'promote' | 'demote') => {
    if (!pluginId || !plugin) return;
    setTransitioning(true);
    setLifecycleError(null);
    try {
      const response =
        direction === 'promote'
          ? await nodeDesignerApi.promoteVersion(pluginId, plugin.version)
          : await nodeDesignerApi.demoteVersion(pluginId, plugin.version);
      setPlugin(response.plugin);
    } catch (err: any) {
      setLifecycleError(err?.message || 'The lifecycle transition failed');
    } finally {
      setTransitioning(false);
    }
  };

  // Delete the record (every version plus its source snapshot and
  // built artifacts). Failures (e.g. 409 RECORD_IN_USE for versions
  // promoted beyond dev) surface as an error alert on the page.
  const confirmDelete = async () => {
    if (!pluginId) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await nodeDesignerApi.deletePlugin(pluginId);
      setShowDeleteModal(false);
      navigate('/node-designer');
    } catch (err: any) {
      setShowDeleteModal(false);
      setDeleteError(err?.message || 'The plugin record could not be deleted');
    } finally {
      setDeleting(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={plugin.description || undefined}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" ariaLabel="Refresh" onClick={load} />
            <Button onClick={() => navigate('/node-designer')}>Back to library</Button>
            {(plugin.lifecycle_state === 'test' ||
              plugin.lifecycle_state === 'prod') && (
              <Button
                loading={transitioning}
                onClick={() => changeLifecycle('demote')}
              >
                {plugin.lifecycle_state === 'prod'
                  ? 'Demote to test'
                  : 'Demote to dev'}
              </Button>
            )}
            {(plugin.lifecycle_state === 'dev' ||
              plugin.lifecycle_state === 'test') && (
              <Button
                variant="primary"
                loading={transitioning}
                onClick={() => changeLifecycle('promote')}
              >
                {plugin.lifecycle_state === 'dev'
                  ? 'Promote to test'
                  : 'Promote to prod'}
              </Button>
            )}
            <Button
              ariaLabel={`Delete ${plugin.name}`}
              onClick={() => setShowDeleteModal(true)}
            >
              Delete
            </Button>
          </SpaceBetween>
        }
      >
        {plugin.name}
      </Header>

      {deleteError && (
        <Alert
          type="error"
          header="Delete failed"
          dismissible
          onDismiss={() => setDeleteError(null)}
        >
          {deleteError}
        </Alert>
      )}

      {lifecycleError && (
        <Alert
          type="error"
          header="Lifecycle transition rejected"
          dismissible
          onDismiss={() => setLifecycleError(null)}
        >
          {lifecycleError}
        </Alert>
      )}

      <ConfirmationModal
        visible={showDeleteModal}
        title={`Delete ${plugin.name}`}
        message={
          `Delete ${plugin.name}? This removes the record, its source ` +
          'snapshot, and built artifacts. This cannot be undone.'
        }
        confirmButtonText="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setShowDeleteModal(false)}
      />

      {/* Asynchronous import status: the repository fetch is still
          running (refresh to update), or it failed with a finding. */}
      {plugin.import_status === 'fetching' && (
        <Alert type="info" header="Import in progress">
          <StatusIndicator type="in-progress">
            Cloning repository… refresh to update.
          </StatusIndicator>
        </Alert>
      )}
      {plugin.import_status === 'failed' && plugin.import_finding && (
        <Alert type="error" header="Import failed">
          {plugin.import_finding}
        </Alert>
      )}

      <RegistrationPrompt
        pluginId={plugin.plugin_id}
        version={plugin.version}
        artifacts={buildEntries}
      />

      <Container header={<Header variant="h2">Overview</Header>}>
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Version</Box>
            <div>v{plugin.version}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Lifecycle state</Box>
            <LifecycleBadge state={plugin.lifecycle_state} />
          </div>
          <div>
            <Box variant="awsui-key-label">Security review</Box>
            <div>{plugin.review?.decision || 'pending'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Classification</Box>
            <ClassificationBadge classification={classification} />
          </div>
          <div>
            <Box variant="awsui-key-label">Origin</Box>
            <div>{plugin.kind}</div>
          </div>
          {importedPlugins && (
            <div>
              <Box variant="awsui-key-label">Imported plugins</Box>
              <div>{importedPlugins}</div>
            </div>
          )}
          <div>
            <Box variant="awsui-key-label">Created by</Box>
            <div>{plugin.created_by}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Created</Box>
            <div>{plugin.created_at ? new Date(plugin.created_at).toLocaleString() : '—'}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Updated</Box>
            <div>{plugin.updated_at ? new Date(plugin.updated_at).toLocaleString() : '—'}</div>
          </div>
        </ColumnLayout>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {failedArchs.length > 1 && (
                  <Button
                    disabled={retrying.length > 0}
                    onClick={() => retryBuilds(failedArchs)}
                  >
                    Retry failed builds
                  </Button>
                )}
                <Button
                  variant="primary"
                  disabled={buildPanelOpen || retrying.length > 0}
                  onClick={openBuildPanel}
                >
                  Build
                </Button>
              </SpaceBetween>
            }
          >
            Builds
          </Header>
        }
      >
        <SpaceBetween size="m">
        {buildPanelOpen && (
          <SpaceBetween size="s">
            {buildError && (
              <Alert type="error" dismissible onDismiss={() => setBuildError(null)}>
                {buildError}
              </Alert>
            )}
            <FormField
              label="Target architectures"
              description="One build is submitted per selected Target_Architecture."
            >
              <Multiselect
                selectedOptions={buildArchs.map((arch) => ({
                  label:
                    ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ??
                    arch,
                  value: arch,
                }))}
                options={buildableArchitectures.map((arch) => ({
                  label:
                    ARCHITECTURE_LABELS[arch as keyof typeof ARCHITECTURE_LABELS] ??
                    arch,
                  value: arch,
                }))}
                onChange={({ detail }) =>
                  setBuildArchs(
                    detail.selectedOptions
                      .map((option) => option.value)
                      .filter((value): value is string => Boolean(value))
                  )
                }
                placeholder="Select target architectures"
                disabled={buildSubmitting}
              />
            </FormField>
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="primary"
                loading={buildSubmitting}
                disabled={buildArchs.length === 0}
                onClick={startBuild}
              >
                Start build
              </Button>
              <Button
                disabled={buildSubmitting}
                onClick={() => setBuildPanelOpen(false)}
              >
                Cancel
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        )}
        {retryError && (
          <Alert type="error" dismissible onDismiss={() => setRetryError(null)}>
            {retryError}
          </Alert>
        )}
        {Object.keys(buildEntries).length === 0 ? (
          <Box color="text-status-inactive">No builds submitted for this version yet.</Box>
        ) : (
          <SpaceBetween size="m">
            {Object.keys(buildEntries)
              .sort()
              .map((arch) => {
                const entry = buildEntries[arch] || {};
                const excerpt = logExcerpt(entry.logTail);
                // Advisory platform requirements check recorded at
                // import time: warn (never block) when the source's
                // GStreamer requirement exceeds what this platform's
                // build image ships, with the working revision to
                // import instead when one is known.
                const compat = plugin.platform_compatibility?.[arch];
                // Multi-revision imports pin architectures to their
                // own source revision (arch_revisions -> fetches):
                // show which revision this architecture builds from.
                const archRevision = archRevisionLabel(plugin, arch);
                return (
                  <SpaceBetween size="xs" key={arch}>
                    <SpaceBetween direction="horizontal" size="xs">
                      <BuildStatusIndicator
                        arch={arch}
                        status={entry.buildStatus}
                        logTail={entry.logTail}
                      />
                      {archRevision && (
                        <Box
                          variant="span"
                          color="text-body-secondary"
                          fontSize="body-s"
                        >
                          revision {archRevision}
                        </Box>
                      )}
                      {entry.buildStatus === 'failed' && (
                        <Button
                          variant="inline-link"
                          loading={retrying.includes(arch)}
                          disabled={retrying.length > 0 && !retrying.includes(arch)}
                          onClick={() => retryBuilds([arch])}
                        >
                          Retry build
                        </Button>
                      )}
                    </SpaceBetween>
                    {compat && compat.compatible === false && (
                      <Box padding={{ left: 'l' }}>
                        <SpaceBetween size="xs">
                          <StatusIndicator type="warning">
                            {platformWarningMessage(arch, compat)}
                          </StatusIndicator>
                          {canAdjustRevision(plugin, arch) &&
                            (adjustingArch === arch ? (
                              <SpaceBetween direction="horizontal" size="xs">
                                <Input
                                  value={adjustValue}
                                  onChange={({ detail }) =>
                                    setAdjustValue(detail.value)
                                  }
                                  ariaLabel={`Revision for ${arch}`}
                                  disabled={adjustSubmitting}
                                />
                                <Button
                                  variant="primary"
                                  loading={adjustSubmitting}
                                  disabled={retrying.length > 0}
                                  onClick={() => applyAdjustment(arch)}
                                >
                                  Apply
                                </Button>
                                <Button
                                  disabled={adjustSubmitting}
                                  onClick={() => setAdjustingArch(null)}
                                >
                                  Cancel
                                </Button>
                              </SpaceBetween>
                            ) : (
                              <Button
                                variant="inline-link"
                                disabled={
                                  retrying.length > 0 || adjustSubmitting
                                }
                                onClick={() =>
                                  openAdjust(
                                    arch,
                                    compat.suggestedRevision || ''
                                  )
                                }
                              >
                                Adjust revision for this platform
                              </Button>
                            ))}
                          {adjustErrors[arch] && (
                            <Alert
                              type="error"
                              dismissible
                              onDismiss={() => clearAdjustError(arch)}
                            >
                              {adjustErrors[arch]}
                            </Alert>
                          )}
                        </SpaceBetween>
                      </Box>
                    )}
                    {entry.buildStatus === 'failed' && excerpt && (
                      <Box padding={{ left: 'l' }}>
                        <pre
                          style={{
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            margin: 0,
                            fontSize: '12px',
                            background: '#f2f3f3',
                            padding: '8px',
                            borderRadius: '4px',
                            maxHeight: '200px',
                            overflow: 'auto',
                          }}
                        >
                          {excerpt}
                        </pre>
                      </Box>
                    )}
                  </SpaceBetween>
                );
              })}
          </SpaceBetween>
        )}
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header variant="h2" counter={`(${sourceFiles.length})`}>
            Source
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Select
            placeholder="Select a source file to inspect"
            selectedOption={selectedFile}
            options={sourceFiles.map((f) => ({ label: f.file, value: f.file }))}
            onChange={({ detail }) => showFile(detail.selectedOption)}
            empty="No source files"
          />
          {selectedFile && (
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontSize: '12px',
                background: '#f2f3f3',
                padding: '12px',
                borderRadius: '4px',
                maxHeight: '400px',
                overflow: 'auto',
              }}
            >
              {fileContent === null ? 'Loading…' : fileContent}
            </pre>
          )}
        </SpaceBetween>
      </Container>

      <Table<PluginRecordSummary>
        items={versions}
        trackBy={(item) => `${item.plugin_id}:${item.version}`}
        columnDefinitions={[
          { id: 'version', header: 'Version', cell: (item) => `v${item.version}` },
          {
            id: 'lifecycle',
            header: 'Lifecycle',
            cell: (item) => <LifecycleBadge state={item.lifecycle_state} />,
          },
          { id: 'review', header: 'Security review', cell: (item) => item.review_decision },
          {
            id: 'updated',
            header: 'Updated',
            cell: (item) =>
              item.updated_at ? new Date(item.updated_at).toLocaleString() : '—',
          },
        ]}
        header={<Header variant="h2">Version history</Header>}
        empty={<Box textAlign="center">No versions</Box>}
      />
    </SpaceBetween>
  );
}
