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
  Header,
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
  PluginBuildsView,
  PluginRecordSummary,
  PluginVersionDetail,
} from './types';
import { BuildStatusIndicator, ClassificationBadge, LifecycleBadge, logExcerpt } from './badges';
import {
  archRevisionLabel,
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
              failedArchs.length > 1 ? (
                <Button
                  disabled={retrying.length > 0}
                  onClick={() => retryBuilds(failedArchs)}
                >
                  Retry failed builds
                </Button>
              ) : undefined
            }
          >
            Builds
          </Header>
        }
      >
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
                        <StatusIndicator type="warning">
                          {platformWarningMessage(arch, compat)}
                        </StatusIndicator>
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
