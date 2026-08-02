/**
 * Device detail Cameras tab (camera-registry-sync task 8.1).
 *
 * Lists the device's Camera_Registry entries with name, type, parameters,
 * capability metadata, origin, sync status (with failure reason), and
 * last-reported timestamp (Req 1.3); stale and absent badges with their
 * timestamps (Reqs 4.1, 4.4); a device-disconnected indicator (Req 4.2);
 * an explicit "never synced" state (Req 1.6); the conflict event list with
 * a re-apply action (Reqs 6.3, 6.4); create/edit/delete forms for
 * portal-managed sources — discovery-managed sources are read-only
 * (Req 5.6) — and a refresh-now button hitting the refresh route.
 *
 * The small formatting helpers are exported pure functions so the
 * component tests (task 8.2) can target them directly.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
  Textarea,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import type { JsonValue } from '../pages/workflows/types';
import {
  cameraDisplayName,
  CameraConflictEvent,
  CameraSourceEntry,
  DeviceCameraConflictsResponse,
  DeviceCamerasResponse,
} from '../pages/workflows/cameraReference';

// ---------------------------------------------------------------------------
// Pure helpers (exported for the task 8.2 component tests)
// ---------------------------------------------------------------------------

/** Human timestamp for epoch-milliseconds values; '-' when absent. */
export function formatEpochMs(value?: number | null): string {
  if (value === null || value === undefined) return '-';
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms <= 0) return '-';
  return new Date(ms).toLocaleString();
}

/** Compact one-line rendering of a params/capabilities style record. */
export function summarizeRecord(record?: Record<string, JsonValue> | null): string {
  if (!record || Object.keys(record).length === 0) return '-';
  return Object.entries(record)
    .map(([key, value]) =>
      `${key}: ${typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}`)
    .join(', ');
}

/**
 * Compact rendering of capability metadata: format names with their top
 * resolutions when the record follows the `{formats: [...]}` shape,
 * generic record rendering otherwise.
 */
export function summarizeCapabilities(
  capabilities?: Record<string, JsonValue> | null
): string {
  if (!capabilities || Object.keys(capabilities).length === 0) return '-';
  const formats = capabilities.formats;
  if (Array.isArray(formats) && formats.length > 0) {
    const parts = formats.map((format) => {
      if (typeof format !== 'object' || format === null || Array.isArray(format)) {
        return String(format);
      }
      const record = format as Record<string, JsonValue>;
      const name = record.pixelFormat ?? record.pixel_format ?? '?';
      const resolutions = record.resolutions;
      if (Array.isArray(resolutions) && resolutions.length > 0) {
        const rendered = resolutions
          .slice(0, 3)
          .map((r) => (Array.isArray(r) ? r.join('x') : String(r)))
          .join(', ');
        const suffix = resolutions.length > 3 ? ', …' : '';
        return `${String(name)} (${rendered}${suffix})`;
      }
      return String(name);
    });
    const truncated = capabilities.capabilitiesTruncated === true ? ' (truncated)' : '';
    return parts.join('; ') + truncated;
  }
  return summarizeRecord(capabilities);
}

/** Discovery-managed sources are read-only in the Portal (Req 5.6). */
export function isDiscoveryManaged(camera: CameraSourceEntry): boolean {
  return camera.origin === 'edge-discovered';
}

/**
 * Whether the reported device status counts as disconnected for the
 * inventory's disconnected indicator (Req 4.2). The status comes from
 * the existing device-status lookup (Greengrass core-device health).
 */
export function isDeviceDisconnected(status?: string | null): boolean {
  if (!status) return false;
  const normalized = status.toUpperCase();
  return ['DISCONNECTED', 'OFFLINE', 'UNHEALTHY'].includes(normalized);
}

/**
 * Parse the params form input: empty text yields an empty record; any
 * non-object or invalid JSON yields an error instead of a record.
 */
export function parseParamsInput(
  text: string
): { params?: Record<string, JsonValue>; error?: string } {
  const trimmed = text.trim();
  if (trimmed === '') return { params: {} };
  try {
    const parsed = JSON.parse(trimmed);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return { error: 'Parameters must be a JSON object' };
    }
    return { params: parsed as Record<string, JsonValue> };
  } catch {
    return { error: 'Parameters must be valid JSON' };
  }
}

/** One-line summary of a conflict event's recorded version (Req 6.3). */
export function summarizeConflictVersion(
  version?: Record<string, JsonValue> | null
): string {
  if (!version || Object.keys(version).length === 0) return '-';
  const parts: string[] = [];
  if (version.op !== undefined) parts.push(`op: ${String(version.op)}`);
  if (version.name !== undefined) parts.push(`name: ${String(version.name)}`);
  if (version.type !== undefined) parts.push(`type: ${String(version.type)}`);
  const params = version.params;
  if (params && typeof params === 'object' && !Array.isArray(params)) {
    parts.push(summarizeRecord(params as Record<string, JsonValue>));
  }
  return parts.length > 0 ? parts.join(', ') : summarizeRecord(version);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const CAMERA_TYPE_OPTIONS = [
  { label: 'Camera (V4L2)', value: 'Camera' },
  { label: 'NVIDIA CSI', value: 'NvidiaCSI' },
  { label: 'RTSP', value: 'RTSP' },
  { label: 'Folder', value: 'Folder' },
  { label: 'ICam', value: 'ICam' },
];

interface CameraFormState {
  mode: 'create' | 'edit';
  cameraSourceId?: string;
  name: string;
  type: string;
  paramsText: string;
}

interface DeviceCamerasTabProps {
  deviceId: string;
  usecaseId: string;
}

export default function DeviceCamerasTab({ deviceId, usecaseId }: DeviceCamerasTabProps) {
  const [camerasResponse, setCamerasResponse] = useState<DeviceCamerasResponse | null>(null);
  const [conflictsResponse, setConflictsResponse] =
    useState<DeviceCameraConflictsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [selectedCameras, setSelectedCameras] = useState<CameraSourceEntry[]>([]);
  const [form, setForm] = useState<CameraFormState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<CameraSourceEntry | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [reapplyingConflictId, setReapplyingConflictId] = useState<string | null>(null);

  const loadAll = useCallback(async (showLoading = true) => {
    if (!deviceId || !usecaseId) return;
    try {
      if (showLoading) setLoading(true);
      setLoadError(null);
      const [cameras, conflicts] = await Promise.all([
        apiService.getDeviceCameras(deviceId, usecaseId),
        apiService.getDeviceCameraConflicts(deviceId, usecaseId),
      ]);
      setCamerasResponse(cameras);
      setConflictsResponse(conflicts);
      setSelectedCameras([]);
    } catch (err: any) {
      setLoadError(err.message || 'Failed to load camera registry');
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [deviceId, usecaseId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const handleRefreshNow = async () => {
    try {
      setRefreshing(true);
      setActionError(null);
      // The refresh route pulls the device shadow through the same
      // reducer as the ingest path and returns the refreshed inventory.
      const cameras = await apiService.refreshDeviceCameras(deviceId, usecaseId);
      setCamerasResponse(cameras);
      setSelectedCameras([]);
      const conflicts = await apiService.getDeviceCameraConflicts(deviceId, usecaseId);
      setConflictsResponse(conflicts);
    } catch (err: any) {
      setActionError(err.message || 'Failed to refresh from the device');
    } finally {
      setRefreshing(false);
    }
  };

  const openCreateForm = () => {
    setFormError(null);
    setForm({ mode: 'create', name: '', type: 'Camera', paramsText: '{\n  "devicePath": "/dev/video0"\n}' });
  };

  const openEditForm = (camera: CameraSourceEntry) => {
    setFormError(null);
    setForm({
      mode: 'edit',
      cameraSourceId: camera.camera_source_id,
      name: camera.name ?? '',
      type: camera.type ?? 'Camera',
      paramsText: JSON.stringify(camera.params ?? {}, null, 2),
    });
  };

  const submitForm = async () => {
    if (!form) return;
    if (!form.name.trim()) {
      setFormError('Name is required');
      return;
    }
    const parsed = parseParamsInput(form.paramsText);
    if (parsed.error) {
      setFormError(parsed.error);
      return;
    }
    try {
      setSaving(true);
      setFormError(null);
      const body = { name: form.name.trim(), type: form.type, params: parsed.params };
      if (form.mode === 'create') {
        await apiService.createDeviceCamera(deviceId, usecaseId, body);
      } else {
        await apiService.updateDeviceCamera(deviceId, form.cameraSourceId!, usecaseId, body);
      }
      setForm(null);
      await loadAll(false);
    } catch (err: any) {
      setFormError(err.message || 'Failed to save the camera source');
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    try {
      setDeleting(true);
      setActionError(null);
      await apiService.deleteDeviceCamera(deviceId, deleteTarget.camera_source_id, usecaseId);
      setDeleteTarget(null);
      await loadAll(false);
    } catch (err: any) {
      setActionError(err.message || 'Failed to delete the camera source');
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const handleReapply = async (conflict: CameraConflictEvent) => {
    try {
      setReapplyingConflictId(conflict.conflict_id);
      setActionError(null);
      await apiService.reapplyCameraConflict(deviceId, conflict.conflict_id, usecaseId);
      await loadAll(false);
    } catch (err: any) {
      setActionError(err.message || 'Failed to re-apply the portal version');
    } finally {
      setReapplyingConflictId(null);
    }
  };

  const getSyncStatusIndicator = (camera: CameraSourceEntry) => {
    switch (camera.sync_status) {
      case 'synced':
        return <StatusIndicator type="success">Synced</StatusIndicator>;
      case 'pending':
        return <StatusIndicator type="pending">Pending</StatusIndicator>;
      case 'failed':
        return (
          <SpaceBetween size="xxs">
            <StatusIndicator type="error">Failed</StatusIndicator>
            {camera.failure_reason && (
              <Box variant="small" color="text-status-error">
                {camera.failure_reason}
              </Box>
            )}
          </SpaceBetween>
        );
      default:
        return <StatusIndicator type="info">{camera.sync_status || 'Unknown'}</StatusIndicator>;
    }
  };

  if (loading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box variant="p" color="text-body-secondary" margin={{ top: 's' }}>
            Loading camera registry...
          </Box>
        </Box>
      </Container>
    );
  }

  if (loadError) {
    return (
      <SpaceBetween size="l">
        <Alert type="error">{loadError}</Alert>
        <Button onClick={() => loadAll()}>Retry</Button>
      </SpaceBetween>
    );
  }

  const neverSynced = camerasResponse?.state === 'never-synced';
  const disconnected = isDeviceDisconnected(camerasResponse?.device_status);
  const cameras = camerasResponse?.cameras ?? [];
  const conflicts = conflictsResponse?.conflicts ?? [];
  const selected = selectedCameras[0];
  const selectedIsDiscoveryManaged = selected !== undefined && isDiscoveryManaged(selected);

  return (
    <SpaceBetween size="l">
      {actionError && (
        <Alert type="error" dismissible onDismiss={() => setActionError(null)}>
          {actionError}
        </Alert>
      )}

      {/* Device disconnected indicator alongside the inventory (Req 4.2) */}
      {disconnected && (
        <Alert type="warning" data-testid="device-disconnected-indicator">
          This device is currently reported as disconnected. The camera
          inventory below reflects the last synchronized state.
        </Alert>
      )}

      {/* Explicit never-synced state, never a bare empty list (Req 1.6) */}
      {neverSynced && (
        <Alert type="info" data-testid="never-synced-state" header="Never synced">
          This device has never completed a camera registry
          synchronization. Its camera inventory is not yet known to the
          Portal; sources created here are queued as pending changes.
        </Alert>
      )}

      <Table
        data-testid="device-cameras-table"
        resizableColumns
        wrapLines
        selectionType="single"
        selectedItems={selectedCameras}
        onSelectionChange={({ detail }) =>
          setSelectedCameras(detail.selectedItems as CameraSourceEntry[])
        }
        trackBy="camera_source_id"
        header={
          <Header
            variant="h2"
            counter={`(${cameras.length})`}
            description={
              <SpaceBetween direction="horizontal" size="xs">
                <span>
                  {`Last report: ${formatEpochMs(camerasResponse?.last_report_at)}`}
                </span>
                <span>
                  {`Staleness threshold: ${camerasResponse?.staleness_threshold_hours ?? 24}h`}
                </span>
                {disconnected && (
                  <StatusIndicator type="error">Device disconnected</StatusIndicator>
                )}
                {neverSynced && (
                  <StatusIndicator type="pending">Never synced</StatusIndicator>
                )}
              </SpaceBetween>
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  iconName="refresh"
                  onClick={handleRefreshNow}
                  loading={refreshing}
                  data-testid="refresh-now-button"
                >
                  Refresh now
                </Button>
                <Button
                  onClick={() => selected && openEditForm(selected)}
                  disabled={!selected || selectedIsDiscoveryManaged}
                  data-testid="edit-camera-button"
                >
                  Edit
                </Button>
                <Button
                  onClick={() => selected && setDeleteTarget(selected)}
                  disabled={!selected || selectedIsDiscoveryManaged}
                  data-testid="delete-camera-button"
                >
                  Delete
                </Button>
                <Button variant="primary" onClick={openCreateForm} data-testid="create-camera-button">
                  Create camera source
                </Button>
              </SpaceBetween>
            }
          >
            Cameras
          </Header>
        }
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: (item: CameraSourceEntry) => (
              <SpaceBetween direction="horizontal" size="xs">
                <span>{cameraDisplayName(item)}</span>
                {item.absent && (
                  <Badge color="red">
                    {`Absent${item.absent_since ? ` since ${formatEpochMs(item.absent_since)}` : ''}`}
                  </Badge>
                )}
              </SpaceBetween>
            ),
            sortingField: 'name',
          },
          {
            id: 'type',
            header: 'Type',
            cell: (item: CameraSourceEntry) => item.type || '-',
          },
          {
            id: 'params',
            header: 'Parameters',
            cell: (item: CameraSourceEntry) => summarizeRecord(item.params),
          },
          {
            id: 'capabilities',
            header: 'Capabilities',
            cell: (item: CameraSourceEntry) => summarizeCapabilities(item.capabilities),
          },
          {
            id: 'origin',
            header: 'Origin',
            cell: (item: CameraSourceEntry) =>
              isDiscoveryManaged(item) ? (
                <Badge color="grey">Discovery-managed</Badge>
              ) : item.origin === 'portal-created' ? (
                <Badge color="green">Portal-created</Badge>
              ) : (
                <Badge color="blue">{item.origin || 'Unknown'}</Badge>
              ),
          },
          {
            id: 'syncStatus',
            header: 'Sync status',
            cell: (item: CameraSourceEntry) => getSyncStatusIndicator(item),
          },
          {
            id: 'lastReported',
            header: 'Last reported',
            cell: (item: CameraSourceEntry) => (
              <SpaceBetween direction="horizontal" size="xs">
                <span>{formatEpochMs(item.last_reported_at)}</span>
                {item.stale && <Badge color="severity-medium">Stale</Badge>}
              </SpaceBetween>
            ),
          },
        ]}
        items={cameras}
        empty={
          <Box textAlign="center" color="inherit" padding="l">
            {neverSynced
              ? 'Never synced — no camera inventory has been reported by this device yet'
              : 'No camera sources registered for this device'}
          </Box>
        }
      />

      {/* Conflict events (Reqs 6.3, 6.4) */}
      <Table
        data-testid="camera-conflicts-table"
        resizableColumns
        wrapLines
        header={
          <Header variant="h2" counter={`(${conflicts.length})`}>
            Sync conflicts
          </Header>
        }
        columnDefinitions={[
          {
            id: 'camera',
            header: 'Camera source',
            cell: (item: CameraConflictEvent) => item.camera_source_id || '-',
          },
          {
            id: 'resolution',
            header: 'Resolution',
            cell: (item: CameraConflictEvent) =>
              item.resolution === 'edge-retained' ? (
                <Badge color="blue">Edge retained</Badge>
              ) : item.resolution === 'deletion-retained' ? (
                <Badge color="grey">Deletion retained</Badge>
              ) : (
                item.resolution || '-'
              ),
          },
          {
            id: 'edgeVersion',
            header: 'Edge version (kept)',
            cell: (item: CameraConflictEvent) => summarizeConflictVersion(item.edge_version),
          },
          {
            id: 'portalVersion',
            header: 'Portal version (overridden)',
            cell: (item: CameraConflictEvent) => summarizeConflictVersion(item.portal_version),
          },
          {
            id: 'createdAt',
            header: 'Occurred',
            cell: (item: CameraConflictEvent) => formatEpochMs(item.created_at),
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item: CameraConflictEvent) =>
              item.reapplied_as ? (
                <StatusIndicator type="success">Re-applied</StatusIndicator>
              ) : (
                <Button
                  variant="inline-link"
                  onClick={() => handleReapply(item)}
                  loading={reapplyingConflictId === item.conflict_id}
                  disabled={reapplyingConflictId !== null}
                >
                  Re-apply portal version
                </Button>
              ),
          },
        ]}
        items={conflicts}
        empty={
          <Box textAlign="center" color="inherit" padding="l">
            No sync conflicts recorded for this device
          </Box>
        }
      />

      {/* Create/edit form for portal-managed sources (Req 5.1 UI) */}
      <Modal
        visible={form !== null}
        onDismiss={() => setForm(null)}
        header={form?.mode === 'create' ? 'Create camera source' : `Edit ${form?.cameraSourceId ?? ''}`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setForm(null)} disabled={saving}>
                Cancel
              </Button>
              <Button variant="primary" onClick={submitForm} loading={saving} data-testid="camera-form-submit">
                {form?.mode === 'create' ? 'Create' : 'Save'}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {form && (
          <SpaceBetween size="m">
            {formError && <Alert type="error">{formError}</Alert>}
            <Alert type="info">
              The change is delivered to the device over the sync channel
              and stays pending until the device applies it.
            </Alert>
            <FormField label="Name">
              <Input
                value={form.name}
                onChange={({ detail }) => setForm({ ...form, name: detail.value })}
                placeholder="e.g. Line 1 inspection cam"
                data-testid="camera-form-name"
              />
            </FormField>
            <FormField label="Type">
              <Select
                selectedOption={
                  CAMERA_TYPE_OPTIONS.find((o) => o.value === form.type) ?? {
                    label: form.type,
                    value: form.type,
                  }
                }
                onChange={({ detail }) =>
                  setForm({ ...form, type: detail.selectedOption.value || form.type })
                }
                options={CAMERA_TYPE_OPTIONS}
              />
            </FormField>
            <FormField
              label="Parameters"
              description='Type-specific parameters as a JSON object, e.g. {"devicePath": "/dev/video0"}'
            >
              <Textarea
                value={form.paramsText}
                onChange={({ detail }) => setForm({ ...form, paramsText: detail.value })}
                rows={6}
                data-testid="camera-form-params"
              />
            </FormField>
          </SpaceBetween>
        )}
      </Modal>

      {/* Delete confirmation (pending delete via the sync channel) */}
      <Modal
        visible={deleteTarget !== null}
        onDismiss={() => setDeleteTarget(null)}
        header="Delete camera source"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)} disabled={deleting}>
                Cancel
              </Button>
              <Button variant="primary" onClick={confirmDelete} loading={deleting} data-testid="camera-delete-confirm">
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {deleteTarget && (
          <Box>
            {`Delete camera source "${cameraDisplayName(deleteTarget)}"? The deletion is
            delivered to the device as a pending change and takes effect when the
            device applies it.`}
          </Box>
        )}
      </Modal>
    </SpaceBetween>
  );
}
