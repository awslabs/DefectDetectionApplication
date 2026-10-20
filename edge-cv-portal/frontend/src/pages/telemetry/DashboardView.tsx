/**
 * One dashboard, `/workflow-dashboards/:dashboardId`
 * (workflow-telemetry-dashboards design §8, §9, §11.3, §11.4; tasks.md 5.5).
 *
 * Composes the tiles of the Dashboard's template: the `SeriesPanel` with the
 * `TemplateRenderer` (one series request per refresh), the Now and Health
 * panels fed by ONE `now` poll (`telemetryNowPollSeconds`), the `ClipGallery`
 * when the template lists the `clips` tile, the `LiveTile` with a device
 * selector when it lists `live`, and the Events list refreshing every 15 s.
 * "Telemetry is off" when the settings say so; `registry_enforcement_required`
 * renders as a notice inside the tile it concerns (clip playback, live).
 */
import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Container,
  FormField,
  Header,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService, isTelemetryApiError } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { getErrorMessage } from '../../utils/errorHandling';
import { canManageDashboards, canManageTelemetry } from '../../utils/telemetryPermissions';
import { useDashboardNow } from '../../hooks/useDashboardNow';
import ConfirmationModal from '../../components/ConfirmationModal';
import SeriesPanel from '../../components/telemetry/SeriesPanel';
import NowPanel from '../../components/telemetry/NowPanel';
import HealthPanel from '../../components/telemetry/HealthPanel';
import ClipGallery from '../../components/telemetry/ClipGallery';
import LiveTile from '../../components/telemetry/LiveTile';
import EventsList from '../../components/telemetry/EventsList';
import { TELEMETRY_OFF, newestSummaries } from '../../components/telemetry/nowData';
import type { Dashboard, DeviceShadowResult, TelemetrySchemasResponse } from './types';

export const UNKNOWN_TEMPLATE = 'The dashboard names a template this Portal does not serve';

export default function DashboardView() {
  const { dashboardId = '' } = useParams<{ dashboardId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useAuth();
  const canManage = canManageDashboards(user?.role);
  const canSettings = canManageTelemetry(user?.role);

  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [schemas, setSchemas] = useState<TelemetrySchemasResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [liveDevice, setLiveDevice] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const created = (location.state as { created?: boolean; devices?: DeviceShadowResult[] } | null) ?? null;
  const [createdNotice, setCreatedNotice] = useState<boolean>(!!created?.created);

  useEffect(() => {
    if (!dashboardId) return;
    let cancelled = false;
    setDashboard(null);
    setError(null);
    Promise.all([apiService.getDashboard(dashboardId), apiService.getTelemetrySchemas()])
      .then(([record, schemasAnswer]) => {
        if (cancelled) return;
        setDashboard(record);
        setSchemas(schemasAnswer);
        setLiveDevice(record.device_ids[0] ?? null);
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, 'The dashboard could not be read'));
      });
    return () => {
      cancelled = true;
    };
  }, [dashboardId]);

  const pollSeconds = schemas?.telemetryNowPollSeconds ?? schemas?.cadence?.now ?? 5;
  // ONE `now` poll per page for both panels (design §9.1, §15.1).
  const nowState = useDashboardNow({ dashboardId: dashboard ? dashboard.dashboard_id : null, pollSeconds });
  const template = dashboard && schemas ? schemas.templates[dashboard.template_id] : undefined;
  const newest = useMemo(() => newestSummaries(nowState.now), [nowState.now]);

  const handleDelete = async () => {
    if (!dashboard) return;
    try {
      setDeleteBusy(true);
      await apiService.deleteDashboard(dashboard.dashboard_id);
      navigate(`/workflow-dashboards?usecase_id=${encodeURIComponent(dashboard.usecase_id)}`);
    } catch (err) {
      const remedy = isTelemetryApiError(err) && err.remedy ? ` ${err.remedy}` : '';
      setError(`${getErrorMessage(err, 'The dashboard could not be deleted')}${remedy}`);
      setDeleting(false);
    } finally {
      setDeleteBusy(false);
    }
  };

  const deviceOptions: SelectProps.Option[] = useMemo(
    () => (dashboard?.device_ids ?? []).map((id) => ({ label: id, value: id })),
    [dashboard]
  );

  if (error && !dashboard) {
    return (
      <Container header={<Header variant="h1">Workflow dashboard</Header>}>
        <Alert type="error" data-testid="dashboard-error">
          {error}
        </Alert>
        <Box padding={{ top: 's' }}>
          <Button onClick={() => navigate('/workflow-dashboards')}>Dashboards</Button>
        </Box>
      </Container>
    );
  }

  if (!dashboard || !schemas) {
    return (
      <Container header={<Header variant="h1">Workflow dashboard</Header>}>
        <StatusIndicator type="loading">Reading the dashboard…</StatusIndicator>
      </Container>
    );
  }

  const telemetryOff = nowState.now ? !nowState.now.enabled : false;
  const tiles = new Set(template?.tiles ?? []);

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description={`${template?.label ?? dashboard.template_id} v${dashboard.template_version} · workflow ${dashboard.workflow_id} · ${dashboard.device_ids.length} device(s): ${dashboard.device_ids.join(', ')}`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => navigate(`/workflow-dashboards?usecase_id=${encodeURIComponent(dashboard.usecase_id)}`)}>
                  Dashboards
                </Button>
                {canSettings && (
                  <Button
                    onClick={() => navigate(`/telemetry-settings/${encodeURIComponent(dashboard.usecase_id)}`)}
                    data-testid="dashboard-settings-button"
                  >
                    Telemetry settings
                  </Button>
                )}
                {canManage && (
                  <Button onClick={() => setDeleting(true)} data-testid="dashboard-delete-button">
                    Delete
                  </Button>
                )}
              </SpaceBetween>
            }
          >
            {dashboard.name}
          </Header>
        }
      >
        <SpaceBetween size="s">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          {createdNotice && (
            <Alert type="success" dismissible onDismiss={() => setCreatedNotice(false)} data-testid="dashboard-created-notice">
              Dashboard created.
              {created?.devices?.length
                ? ` Telemetry shadows: ${created.devices.map((d) => `${d.device_id} ${d.shadow}${d.error ? ` (${d.error})` : ''}`).join(', ')}.`
                : ''}
            </Alert>
          )}
          {telemetryOff && (
            <Alert
              type="warning"
              data-testid="dashboard-telemetry-off"
              action={
                canSettings ? (
                  <Button onClick={() => navigate(`/telemetry-settings/${encodeURIComponent(dashboard.usecase_id)}`)}>Telemetry settings</Button>
                ) : undefined
              }
            >
              {TELEMETRY_OFF}. The devices publish nothing until a UseCaseAdmin enables it.
            </Alert>
          )}
          {!template && (
            <Alert type="error" data-testid="dashboard-unknown-template">
              {UNKNOWN_TEMPLATE}: {dashboard.template_id}
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      {template && (
        <SeriesPanel
          dashboard={dashboard}
          template={template}
          catalog={schemas.catalog}
          modelStateOrder={schemas.registry.modelStateOrder}
          cadence={schemas.cadence}
          newestSummaries={newest}
        />
      )}

      {tiles.has('now') && (
        <NowPanel dashboardId={dashboard.dashboard_id} pollSeconds={pollSeconds} now={nowState.now} loading={nowState.loading} error={nowState.error} />
      )}
      {tiles.has('health') && (
        <HealthPanel
          dashboardId={dashboard.dashboard_id}
          pollSeconds={pollSeconds}
          role={user?.role}
          now={nowState.now}
          loading={nowState.loading}
          error={nowState.error}
        />
      )}
      {tiles.has('clips') && (
        <ClipGallery dashboardId={dashboard.dashboard_id} deviceIds={dashboard.device_ids} refreshSeconds={schemas.cadence?.clips} />
      )}
      {tiles.has('live') && (
        <SpaceBetween size="xs">
          <FormField label="Live device">
            <Select
              selectedOption={deviceOptions.find((o) => o.value === liveDevice) ?? null}
              onChange={({ detail }) => setLiveDevice(detail.selectedOption.value ?? null)}
              options={deviceOptions}
              placeholder="Select a device"
              selectedAriaLabel="Selected"
              data-testid="dashboard-live-device"
            />
          </FormField>
          <LiveTile dashboardId={dashboard.dashboard_id} deviceId={liveDevice} cadence={schemas.cadence} />
        </SpaceBetween>
      )}
      <EventsList dashboardId={dashboard.dashboard_id} deviceIds={dashboard.device_ids} refreshSeconds={schemas.cadence?.events} />

      <ConfirmationModal
        visible={deleting}
        title="Delete dashboard"
        message={`Delete the dashboard "${dashboard.name}"? Its devices keep publishing telemetry; only the dashboard record is removed.`}
        confirmButtonText="Delete"
        variant="danger"
        loading={deleteBusy}
        onConfirm={handleDelete}
        onCancel={() => setDeleting(false)}
      />
    </SpaceBetween>
  );
}
