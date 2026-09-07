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
  FileUpload,
  FormField,
  Header,
  Input,
  KeyValuePairs,
  Modal,
  Select,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
  Textarea,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import type { UserRole } from '../types';
import type { JsonValue } from '../pages/workflows/types';
import {
  cameraDisplayName,
  CameraConflictEvent,
  CameraSourceEntry,
  DeviceCameraConflictsResponse,
  DeviceCamerasResponse,
  StaticImagePinStatusResponse,
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
 * Roles holding the device-mutation permission (`manage_devices`) that
 * gates the Camera_Registry mutation routes and the static-image pin
 * mutations server-side (cloud-static-camera-provisioning Req 8.1) —
 * the backend RBAC matrix's Operator-and-above set.
 */
export const DEVICE_MUTATION_ROLES: readonly UserRole[] = [
  'Operator',
  'UseCaseAdmin',
  'PortalAdmin',
];

/**
 * True when the role may pin, replace, or remove a device's static
 * image from the Portal (mirrors the server-side manage_devices gate;
 * the backend enforces it regardless).
 */
export function canManageDeviceCameras(role: UserRole | undefined | null): boolean {
  return role !== undefined && role !== null && DEVICE_MUTATION_ROLES.includes(role);
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
// Static image camera panel (cloud-static-camera-provisioning task 9.1)
// ---------------------------------------------------------------------------

/** Poll interval for the status route while the latest request is pending. */
const PIN_STATUS_POLL_MS = 10000;

const FILE_UPLOAD_I18N = {
  uploadButtonText: () => 'Choose image',
  dropzoneText: () => 'Drop an image to upload',
  removeFileAriaLabel: (index: number) => `Remove file ${index + 1}`,
  errorIconAriaLabel: 'Error',
};

interface StaticImagePanelProps {
  deviceId: string;
  usecaseId: string;
  /** Whether the user holds the device-mutation permission (Req 8.1). */
  canMutate: boolean;
}

/**
 * The "Static image camera" provisioning panel: shows the latest
 * Pin_Request's Sync_Status, the device-reported pinned state, the
 * applied image metadata, the failure reason, and a connectivity hint
 * while pending (Reqs 1.7, 1.10, 4.4-4.7); offers upload-and-pin,
 * replace, and remove actions gated on the mutation permission
 * (Reqs 1.5, 7.2, 8.1); polls the status route while pending.
 */
function StaticImagePanel({ deviceId, usecaseId, canMutate }: StaticImagePanelProps) {
  const [status, setStatus] = useState<StaticImagePinStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [pinning, setPinning] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeConfirmVisible, setRemoveConfirmVisible] = useState(false);

  const loadStatus = useCallback(async () => {
    if (!deviceId || !usecaseId) return;
    try {
      setLoadError(null);
      const response = await apiService.getStaticImagePinStatus(deviceId, usecaseId);
      setStatus(response);
    } catch (err: any) {
      setLoadError(err.message || 'Failed to load the static image camera status');
    } finally {
      setLoading(false);
    }
  }, [deviceId, usecaseId]);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  // Poll the status route while the latest Pin_Request is pending.
  const pending = status?.latest?.status === 'pending';
  useEffect(() => {
    if (!pending) return undefined;
    const interval = setInterval(() => {
      loadStatus();
    }, PIN_STATUS_POLL_MS);
    return () => clearInterval(interval);
  }, [pending, loadStatus]);

  const handlePin = async () => {
    const file = files[0];
    if (!file) {
      setActionError('Choose an image file to pin');
      return;
    }
    try {
      setPinning(true);
      setActionError(null);
      // Upload path (design Decision 2): presigned PUT to a staging key,
      // then the pin submit validates and copies the staged object.
      const upload = await apiService.getStaticImageUploadUrl(deviceId, usecaseId);
      const put = await fetch(upload.uploadUrl, { method: 'PUT', body: file });
      if (!put.ok) {
        throw new Error(`Image upload failed (HTTP ${put.status})`);
      }
      await apiService.pinStaticImage(deviceId, usecaseId, {
        stagingKey: upload.stagingKey,
        fileName: file.name,
      });
      setFiles([]);
      await loadStatus();
    } catch (err: any) {
      setActionError(err.message || 'Failed to pin the image');
    } finally {
      setPinning(false);
    }
  };

  const confirmRemove = async () => {
    try {
      setRemoving(true);
      setActionError(null);
      await apiService.removeStaticImagePin(deviceId, usecaseId);
      setRemoveConfirmVisible(false);
      await loadStatus();
    } catch (err: any) {
      setActionError(err.message || 'Failed to remove the pinned image');
      setRemoveConfirmVisible(false);
    } finally {
      setRemoving(false);
    }
  };

  const latest = status?.latest ?? null;
  const deviceReported = status?.deviceReported ?? null;
  const metadata = latest?.deviceMetadata ?? status?.deviceMetadata ?? null;
  const pinButtonLabel = deviceReported?.present ? 'Replace image' : 'Pin image';

  const statusIndicator = () => {
    if (latest === null) return null;
    const opLabel = latest.op === 'remove' ? 'Removal' : 'Pin';
    switch (latest.status) {
      case 'pending':
        return <StatusIndicator type="pending">{`${opLabel} pending`}</StatusIndicator>;
      case 'applied':
        return <StatusIndicator type="success">{`${opLabel} applied`}</StatusIndicator>;
      case 'failed':
        return <StatusIndicator type="error">{`${opLabel} failed`}</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{latest.status || 'Unknown'}</StatusIndicator>;
    }
  };

  return (
    <Container
      data-testid="static-image-panel"
      header={
        <Header
          variant="h2"
          description="Pin a still image from the Portal; the device serves it as
            the static-image-camera source while an image is pinned."
        >
          Static image camera
        </Header>
      }
    >
      {loading ? (
        <Box textAlign="center" padding="m">
          <Spinner />
        </Box>
      ) : loadError ? (
        <SpaceBetween size="s">
          <Alert type="error">{loadError}</Alert>
          <Button onClick={() => loadStatus()}>Retry</Button>
        </SpaceBetween>
      ) : (
        <SpaceBetween size="m">
          {actionError && (
            <Alert type="error" dismissible onDismiss={() => setActionError(null)}>
              {actionError}
            </Alert>
          )}

          {/* No cloud Pin_Request exists (Reqs 1.10, 4.7). */}
          {status?.noPinRequest && (
            <Box color="text-body-secondary" data-testid="static-image-no-request">
              No cloud pin request exists for this device. Upload an image
              to pin it to the device&apos;s static image camera.
            </Box>
          )}

          {/* Latest Pin_Request Sync_Status (Req 4.4). */}
          {latest !== null && (
            <SpaceBetween size="xxs">
              <SpaceBetween direction="horizontal" size="xs">
                <span data-testid="static-image-status">{statusIndicator()}</span>
                <Box variant="small" color="text-body-secondary">
                  {`Request ${latest.pinRequestId} · created ${formatEpochMs(latest.createdAt)}`}
                </Box>
              </SpaceBetween>
              {latest.status === 'failed' && latest.failureReason && (
                <Box
                  variant="small"
                  color="text-status-error"
                  data-testid="static-image-failure-reason"
                >
                  {latest.failureReason}
                </Box>
              )}
            </SpaceBetween>
          )}

          {/* Connectivity hint while the request is pending (Req 4.5). */}
          {pending && status?.connectivity && (
            <Alert
              type={status.connectivity === 'disconnected' ? 'warning' : 'info'}
              data-testid="static-image-connectivity-hint"
            >
              {status.connectivity === 'disconnected'
                ? 'The device is currently disconnected. The request stays pending and applies when the device reconnects.'
                : 'The device is connected. The pending request should apply shortly.'}
            </Alert>
          )}

          {/* Device-reported pinned state — the current state even when it
              disagrees with the recorded outcome (Reqs 4.6, 4.8). */}
          {deviceReported !== null && (
            <Box data-testid="static-image-device-reported">
              {deviceReported.present ? (
                <StatusIndicator type="success">
                  Device reports a pinned image
                </StatusIndicator>
              ) : (
                <StatusIndicator type="stopped">
                  {`Device reports the static camera absent${
                    deviceReported.absentSince
                      ? ` since ${formatEpochMs(deviceReported.absentSince)}`
                      : ''
                  }`}
                </StatusIndicator>
              )}
            </Box>
          )}

          {/* Applied image metadata (Req 1.7). */}
          {metadata && (
            <div data-testid="static-image-metadata">
              <KeyValuePairs
                columns={4}
                items={[
                  { label: 'Width', value: metadata.width != null ? `${metadata.width} px` : '-' },
                  { label: 'Height', value: metadata.height != null ? `${metadata.height} px` : '-' },
                  { label: 'Format', value: metadata.format || '-' },
                  { label: 'File name', value: metadata.fileName || '-' },
                ]}
              />
            </div>
          )}

          {/* Pin / replace / remove, gated on the mutation permission. */}
          {canMutate && (
            <SpaceBetween size="xs">
              <FormField
                label={pinButtonLabel === 'Replace image' ? 'Replace the pinned image' : 'Pin an image'}
                description="JPEG, PNG, or BMP, at most 50 MB"
              >
                <FileUpload
                  value={files}
                  onChange={({ detail }) => setFiles(detail.value)}
                  accept="image/jpeg,image/png,image/bmp"
                  constraintText="JPEG, PNG, or BMP"
                  i18nStrings={FILE_UPLOAD_I18N}
                />
              </FormField>
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="primary"
                  onClick={handlePin}
                  loading={pinning}
                  disabled={files.length === 0}
                  data-testid="static-image-pin-button"
                >
                  {pinButtonLabel}
                </Button>
                <Button
                  onClick={() => setRemoveConfirmVisible(true)}
                  loading={removing}
                  data-testid="static-image-remove-button"
                >
                  Remove pinned image
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          )}
        </SpaceBetween>
      )}

      {/* Removal confirmation (Req 7.2). */}
      <Modal
        visible={removeConfirmVisible}
        onDismiss={() => setRemoveConfirmVisible(false)}
        header="Remove pinned image"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setRemoveConfirmVisible(false)}
                disabled={removing}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={confirmRemove}
                loading={removing}
                data-testid="static-image-remove-confirm"
              >
                Remove
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          Remove the device&apos;s pinned static image? The removal is
          delivered through the sync channel; the static image camera
          disappears from the device&apos;s camera inventory once applied.
        </Box>
      </Modal>
    </Container>
  );
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
  const { user } = useAuth();
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

      {/* Static image camera provisioning (cloud-static-camera-
          provisioning task 9.1) */}
      <StaticImagePanel
        deviceId={deviceId}
        usecaseId={usecaseId}
        canMutate={canManageDeviceCameras(user?.role)}
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
