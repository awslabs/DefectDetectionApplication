/**
 * The dashboards list, `/workflow-dashboards` (workflow-telemetry-dashboards
 * design §11.2, §11.4; tasks.md 5.5).
 *
 * Per use case (a selector fed by the saved selection): the Dashboards table
 * with name, workflow, template, devices and author; "Create dashboard" and
 * "Delete" for the roles holding `dashboards:manage`; "Telemetry settings"
 * for `telemetry:manage`. The server decides every action; the role
 * predicates only hide controls that would answer 403.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  Link,
  Select,
  SelectProps,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { useUsecase } from '../../contexts/UsecaseContext';
import { getErrorMessage } from '../../utils/errorHandling';
import { canManageDashboards, canManageTelemetry } from '../../utils/telemetryPermissions';
import ConfirmationModal from '../../components/ConfirmationModal';
import { formatTime } from '../../components/telemetry/format';
import type { Dashboard, TelemetrySchemasResponse } from './types';

export const NO_DASHBOARDS = 'No dashboards for this use case yet';

export default function WorkflowDashboards() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { user } = useAuth();
  const { selectedUsecaseId, setSelectedUsecaseId } = useUsecase();
  const canManage = canManageDashboards(user?.role);
  const canSettings = canManageTelemetry(user?.role);

  const [useCaseOptions, setUseCaseOptions] = useState<SelectProps.Option[]>([]);
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [schemas, setSchemas] = useState<TelemetrySchemasResponse | null>(null);
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Dashboard | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const usecaseId = searchParams.get('usecase_id') ?? selectedUsecaseId ?? null;

  // The use-case options; without a selection, continue at the saved or first use case.
  useEffect(() => {
    let cancelled = false;
    apiService
      .listUseCases()
      .then(({ usecases }) => {
        if (cancelled) return;
        const options = usecases.map((uc) => ({ label: uc.name, value: uc.usecase_id }));
        setUseCaseOptions(options);
        if (!usecaseId) {
          const chosen = options.find((o) => o.value === selectedUsecaseId) ?? options[0];
          if (chosen?.value) {
            setSelectedUsecaseId(chosen.value);
            setSearchParams({ usecase_id: chosen.value }, { replace: true });
          }
        }
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, 'Failed to list use cases'));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    apiService
      .getTelemetrySchemas()
      .then(setSchemas)
      .catch(() => undefined);
  }, []);

  const loadDashboards = useCallback(() => {
    if (!usecaseId) return;
    setLoading(true);
    setError(null);
    apiService
      .listDashboards(usecaseId)
      .then((answer) => setDashboards(answer.dashboards ?? []))
      .catch((err) => setError(getErrorMessage(err, 'Failed to list dashboards')))
      .finally(() => setLoading(false));
  }, [usecaseId]);

  useEffect(() => {
    loadDashboards();
  }, [loadDashboards]);

  // Workflow names are decoration: a failure leaves the ids.
  useEffect(() => {
    if (!usecaseId) return;
    let cancelled = false;
    apiService
      .listWorkflows(usecaseId)
      .then(({ workflows }) => {
        if (cancelled) return;
        const names: Record<string, string> = {};
        for (const wf of workflows ?? []) names[wf.workflow_id] = wf.name;
        setWorkflowNames(names);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [usecaseId]);

  const chooseUseCase = (value: string) => {
    setSelectedUsecaseId(value);
    setSearchParams({ usecase_id: value });
  };

  const handleDelete = async () => {
    if (!deleting) return;
    try {
      setDeleteBusy(true);
      setError(null);
      await apiService.deleteDashboard(deleting.dashboard_id);
      setNotice(`Dashboard "${deleting.name}" deleted.`);
      setDeleting(null);
      loadDashboards();
    } catch (err) {
      const remedy = isTelemetryApiError(err) && err.remedy ? ` ${err.remedy}` : '';
      setError(`${getErrorMessage(err, 'Failed to delete the dashboard')}${remedy}`);
      setDeleting(null);
    } finally {
      setDeleteBusy(false);
    }
  };

  const selectedUseCase = useMemo(
    () => useCaseOptions.find((o) => o.value === usecaseId) ?? (usecaseId ? { label: usecaseId, value: usecaseId } : null),
    [useCaseOptions, usecaseId]
  );

  const templateLabel = (id: string) => schemas?.templates?.[id]?.label ?? id;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description="Charts, Now and Health panels, clips and live frames of a deployed workflow, fed by the devices' periodic summaries."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select
                  selectedOption={selectedUseCase}
                  onChange={({ detail }) => {
                    if (detail.selectedOption.value) chooseUseCase(detail.selectedOption.value);
                  }}
                  options={useCaseOptions}
                  placeholder="Select a use case"
                  selectedAriaLabel="Selected"
                  data-testid="dashboards-usecase-select"
                />
                <Button iconName="refresh" onClick={loadDashboards} disabled={!usecaseId || loading} ariaLabel="Refresh">
                  Refresh
                </Button>
                {canSettings && (
                  <Button
                    onClick={() => usecaseId && navigate(`/telemetry-settings/${encodeURIComponent(usecaseId)}`)}
                    disabled={!usecaseId}
                    data-testid="dashboards-settings-button"
                  >
                    Telemetry settings
                  </Button>
                )}
                {canManage && (
                  <Button
                    variant="primary"
                    onClick={() => navigate(`/workflow-dashboards/create${usecaseId ? `?usecase_id=${encodeURIComponent(usecaseId)}` : ''}`)}
                    data-testid="dashboards-create-button"
                  >
                    Create dashboard
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            Workflow dashboards
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          {notice && (
            <Alert type="success" dismissible onDismiss={() => setNotice(null)} data-testid="dashboards-notice">
              {notice}
            </Alert>
          )}
          {!usecaseId ? (
            <Box textAlign="center" color="text-body-secondary" padding="l">
              Select a use case to list its dashboards
            </Box>
          ) : (
            <Table
              items={dashboards}
              loading={loading}
              loadingText="Loading dashboards"
              trackBy="dashboard_id"
              variant="embedded"
              data-testid="dashboards-table"
              columnDefinitions={[
                {
                  id: 'name',
                  header: 'Name',
                  cell: (item) => (
                    <Link
                      href={`/workflow-dashboards/${encodeURIComponent(item.dashboard_id)}`}
                      onFollow={(e) => {
                        e.preventDefault();
                        navigate(`/workflow-dashboards/${encodeURIComponent(item.dashboard_id)}`);
                      }}
                    >
                      {item.name}
                    </Link>
                  ),
                },
                { id: 'workflow', header: 'Workflow', cell: (item) => workflowNames[item.workflow_id] ?? item.workflow_id },
                { id: 'template', header: 'Template', cell: (item) => `${templateLabel(item.template_id)} v${item.template_version}` },
                {
                  id: 'devices',
                  header: 'Devices',
                  cell: (item) => `${item.device_ids.length}: ${item.device_ids.join(', ')}`,
                },
                { id: 'created', header: 'Created', cell: (item) => `${formatTime(item.createdAt)} by ${item.actor}` },
                ...(canManage
                  ? [
                      {
                        id: 'actions',
                        header: 'Actions',
                        cell: (item: Dashboard) => (
                          <Button onClick={() => setDeleting(item)} data-testid={`dashboard-delete-${item.dashboard_id}`}>
                            Delete
                          </Button>
                        ),
                      },
                    ]
                  : []),
              ]}
              empty={
                <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="dashboards-empty">
                  {NO_DASHBOARDS}
                </Box>
              }
            />
          )}
        </SpaceBetween>
      </Container>

      <ConfirmationModal
        visible={deleting !== null}
        title="Delete dashboard"
        message={deleting ? `Delete the dashboard "${deleting.name}"? Its devices keep publishing telemetry; only the dashboard record is removed.` : ''}
        confirmButtonText="Delete"
        variant="danger"
        loading={deleteBusy}
        onConfirm={handleDelete}
        onCancel={() => setDeleting(null)}
      />
    </SpaceBetween>
  );
}
