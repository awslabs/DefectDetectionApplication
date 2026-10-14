/**
 * Plugin library list (custom-node-designer, Requirements 1.1, 3.5, 15.1).
 *
 * Plugin_Records of the selected Use_Case with lifecycle badges,
 * per-arch build status, and classification risk badges. Each record
 * links to its detail view (build log excerpts, provenance, source).
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Header,
  Link,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { useNavigate } from 'react-router-dom';
import ConfirmationModal from '../../components/ConfirmationModal';
import { apiService } from '../../services/api';
import { useUsecase } from '../../contexts/UsecaseContext';
import { UseCase } from '../../types';
import { nodeDesignerApi } from './api';
import { PluginRecordSummary } from './types';
import { BuildStatusIndicator, ClassificationBadge, LifecycleBadge } from './badges';
import { importedPluginsLabel } from './importFlow';

export default function PluginLibrary() {
  const navigate = useNavigate();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] = useState<SelectProps.Option | null>(null);
  const [plugins, setPlugins] = useState<PluginRecordSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Record deletion (bad/duplicate imports): the row awaiting
  // confirmation in the modal, and the in-flight request flag.
  const [deleteTarget, setDeleteTarget] = useState<PluginRecordSummary | null>(null);
  const [deleting, setDeleting] = useState(false);

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

  const loadPlugins = useCallback(async (usecaseId: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await nodeDesignerApi.listPlugins(usecaseId);
      setPlugins(response.plugins || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load plugin records');
      setPlugins([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedUseCase?.value) {
      loadPlugins(selectedUseCase.value);
    } else {
      setPlugins([]);
    }
  }, [selectedUseCase, loadPlugins]);

  // Delete the confirmed record (every version plus its source
  // snapshot and built artifacts) and refresh the list. Failures
  // (e.g. 409 RECORD_IN_USE for versions promoted beyond dev) surface
  // in the page's error alert.
  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setError(null);
    try {
      await nodeDesignerApi.deletePlugin(deleteTarget.plugin_id);
      setDeleteTarget(null);
      if (selectedUseCase?.value) {
        await loadPlugins(selectedUseCase.value);
      }
    } catch (err: any) {
      setDeleteTarget(null);
      setError(err?.message || 'The plugin record could not be deleted');
    } finally {
      setDeleting(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Create, build, and manage custom node plugins for the Workflow Builder palette."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
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
            <Button
              iconName="refresh"
              ariaLabel="Refresh plugin records"
              onClick={() => selectedUseCase?.value && loadPlugins(selectedUseCase.value)}
            />
            <Button onClick={() => navigate('/node-designer/generate')}>
              Generate with AI
            </Button>
            <Button onClick={() => navigate('/node-designer/import')}>
              Import plugin
            </Button>
            <Button variant="primary" onClick={() => navigate('/node-designer/create')}>
              Create custom node
            </Button>
          </SpaceBetween>
        }
      >
        Node Designer
      </Header>

      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Table<PluginRecordSummary>
        items={plugins}
        loading={loading}
        loadingText="Loading plugin records"
        trackBy={(item) => `${item.plugin_id}:${item.version}`}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: (item) => {
              // Partial import selections show compactly under the
              // name ('rtsp' or '3 plugins') so a single-plugin import
              // of a plugin set is not mistaken for the whole library.
              const selectionLabel = importedPluginsLabel(
                item.selected_plugins,
                item.plugins_found_count
              );
              return (
                <>
                  <Link
                    onFollow={(event) => {
                      event.preventDefault();
                      navigate(`/node-designer/plugins/${item.plugin_id}`);
                    }}
                    href={`/node-designer/plugins/${item.plugin_id}`}
                  >
                    {item.name}
                  </Link>
                  {selectionLabel && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      {selectionLabel}
                    </Box>
                  )}
                </>
              );
            },
          },
          { id: 'version', header: 'Version', cell: (item) => `v${item.version}` },
          { id: 'kind', header: 'Origin', cell: (item) => item.kind },
          {
            id: 'lifecycle',
            header: 'Lifecycle',
            cell: (item) => <LifecycleBadge state={item.lifecycle_state} />,
          },
          {
            id: 'classification',
            header: 'Classification',
            cell: (item) => <ClassificationBadge classification={item.classification} />,
          },
          {
            id: 'builds',
            header: 'Builds',
            cell: (item) => {
              const archs = Object.keys(item.build_status || {}).sort();
              if (archs.length === 0) {
                // Asynchronously imported records without builds yet:
                // surface the import status instead of "not built".
                if (item.import_status === 'fetching') {
                  return (
                    <StatusIndicator type="in-progress">
                      fetching source
                    </StatusIndicator>
                  );
                }
                if (item.import_status === 'failed') {
                  return (
                    <StatusIndicator type="error">import failed</StatusIndicator>
                  );
                }
                return <Box color="text-status-inactive">not built</Box>;
              }
              return (
                <SpaceBetween size="xxs">
                  {archs.map((arch) => (
                    <BuildStatusIndicator
                      key={arch}
                      arch={arch}
                      status={item.build_status[arch]}
                    />
                  ))}
                </SpaceBetween>
              );
            },
          },
          { id: 'review', header: 'Security review', cell: (item) => item.review_decision },
          {
            id: 'updated',
            header: 'Updated',
            cell: (item) =>
              item.updated_at ? new Date(item.updated_at).toLocaleString() : '—',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item) => (
              <Button
                variant="inline-link"
                ariaLabel={`Delete ${item.name}`}
                onClick={() => setDeleteTarget(item)}
              >
                Delete
              </Button>
            ),
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No plugin records</b>
            <Box variant="p" color="inherit">
              Create a custom node to scaffold your first plugin.
            </Box>
            <Button onClick={() => navigate('/node-designer/create')}>
              Create custom node
            </Button>
          </Box>
        }
        header={<Header counter={`(${plugins.length})`}>Plugin records</Header>}
      />

      <ConfirmationModal
        visible={!!deleteTarget}
        title={`Delete ${deleteTarget?.name || ''}`}
        message={
          `Delete ${deleteTarget?.name || ''}? This removes the record, ` +
          'its source snapshot, and built artifacts. This cannot be undone.'
        }
        confirmButtonText="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </SpaceBetween>
  );
}
