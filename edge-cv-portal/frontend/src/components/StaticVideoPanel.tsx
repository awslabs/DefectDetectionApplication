/**
 * Static video camera panel (static-camera-video-loop task 9.3,
 * Requirements 8.2-8.4 and 9.1-9.6).
 *
 * The video counterpart of the Cameras tab's static image panel, for the
 * device's second virtual camera, `static-video-camera`. It shows:
 * - the latest submission's asynchronous validation: validating, or why
 *   the Portal rejected the video (the decode takes up to a minute, so the
 *   pin route only accepts the submission);
 * - the latest Video_Pin_Request's Sync_Status and failure reason, with a
 *   connectivity hint while it is pending;
 * - the device-reported state and the applied video's metadata.
 *
 * It uploads a video with a 100 MB pre-check and a progress bar and offers
 * replace and remove, gated on the device-mutation permission. The status
 * route is polled while a validation or a request is outstanding.
 *
 * Labels and test ids are distinct from the image panel's, so the two
 * panels sit side by side on the tab without interfering.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  FileUpload,
  FormField,
  Header,
  KeyValuePairs,
  Modal,
  ProgressBar,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { putFileWithProgress } from '../utils/detectorConversion';
import {
  MAX_PIN_VIDEO_BYTES,
  type StaticVideoPinMetadata,
  type StaticVideoPinStatusResponse,
  type StaticVideoValidation,
} from '../pages/workflows/cameraReference';

// ---------------------------------------------------------------------------
// Pure helpers (exported for the component tests)
// ---------------------------------------------------------------------------

/** Poll interval while the latest submission is being validated. */
export const VIDEO_VALIDATION_POLL_MS = 3000;

/** Poll interval while the latest Video_Pin_Request is pending. */
export const VIDEO_PIN_STATUS_POLL_MS = 10000;

/** The file picker's filter: the Supported_Video_Containers (Req 9.2). */
export const VIDEO_FILE_ACCEPT = [
  'video/mp4',
  'video/x-m4v',
  'video/quicktime',
  'video/x-msvideo',
  'video/x-matroska',
  'video/webm',
  '.mp4',
  '.m4v',
  '.mov',
  '.avi',
  '.mkv',
  '.webm',
].join(',');

const VALIDATION_OUTSTANDING_STATUSES = ['validating', 'rejected', 'expired'];

/** Human timestamp for epoch-milliseconds values; '-' when absent. */
export function formatVideoTimestamp(value?: number | null): string {
  if (value === null || value === undefined) return '-';
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms <= 0) return '-';
  return new Date(ms).toLocaleString();
}

/** "12.5 s" under a minute, "2 min 5.0 s" above; '-' when unknown. */
export function formatVideoDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return '-';
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return '-';
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${(seconds - minutes * 60).toFixed(1)} s`;
}

/** "29.97 fps" (at most two decimals); '-' when unknown. */
export function formatVideoFps(fps?: number | null): string {
  if (fps === null || fps === undefined) return '-';
  const value = Number(fps);
  if (!Number.isFinite(value) || value <= 0) return '-';
  return `${Number(value.toFixed(2))} fps`;
}

/** Bytes as megabytes (MiB, like the 100 MB limit), one decimal. */
export function formatMegabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * The 100 MB pre-check (Req 9.3): the message naming the limit for a file
 * over it, or null. An oversize file is never uploaded.
 */
export function videoOversizeMessage(
  sizeBytes: number,
  limitBytes: number = MAX_PIN_VIDEO_BYTES
): string | null {
  if (sizeBytes <= limitBytes) return null;
  return (
    `The video is ${formatMegabytes(sizeBytes)}; videos can be at most ` +
    `${Math.round(limitBytes / (1024 * 1024))} MB.`
  );
}

/**
 * The Video_Metadata rows (Req 9.5): duration, frame rate, frame count,
 * codec, dimensions, container format, and file name, plus the file size
 * once the device reports it.
 */
export function videoMetadataItems(
  metadata: StaticVideoPinMetadata,
  fileName?: string | null
): { label: string; value: string }[] {
  const items = [
    { label: 'Duration', value: formatVideoDuration(metadata.durationMs) },
    { label: 'Frame rate', value: formatVideoFps(metadata.fps) },
    {
      label: 'Frame count',
      value: metadata.frameCount != null ? String(metadata.frameCount) : '-',
    },
    { label: 'Codec', value: metadata.codec || '-' },
    {
      label: 'Dimensions',
      value:
        metadata.width != null && metadata.height != null
          ? `${metadata.width} × ${metadata.height} px`
          : '-',
    },
    { label: 'Container', value: metadata.format || '-' },
    { label: 'File name', value: metadata.fileName || fileName || '-' },
  ];
  if (metadata.fileSizeBytes != null) {
    items.push({ label: 'File size', value: formatMegabytes(Number(metadata.fileSizeBytes)) });
  }
  return items;
}

/**
 * The validation to surface: one still validating, or rejected or expired,
 * and newer than the latest Video_Pin_Request (an older rejection, or one
 * a later removal replaced, is history). Accepted validations are shown
 * through the request they created.
 */
export function outstandingValidation(
  status: StaticVideoPinStatusResponse | null | undefined
): StaticVideoValidation | null {
  const validation = status?.validation;
  if (!validation || !VALIDATION_OUTSTANDING_STATUSES.includes(String(validation.status))) {
    return null;
  }
  const latestCreatedAt = status?.latest?.createdAt;
  if (
    latestCreatedAt !== null &&
    latestCreatedAt !== undefined &&
    Number(validation.createdAt ?? 0) <= Number(latestCreatedAt)
  ) {
    return null;
  }
  return validation;
}

/**
 * How often to poll the status route: fast while a validation runs,
 * slower while a request is pending, not at all otherwise.
 */
export function videoStatusPollInterval(
  status: StaticVideoPinStatusResponse | null | undefined
): number | null {
  if (outstandingValidation(status)?.status === 'validating') {
    return VIDEO_VALIDATION_POLL_MS;
  }
  if (status?.latest?.status === 'pending') return VIDEO_PIN_STATUS_POLL_MS;
  return null;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const FILE_UPLOAD_I18N = {
  uploadButtonText: () => 'Choose video',
  dropzoneText: () => 'Drop a video to upload',
  removeFileAriaLabel: (index: number) => `Remove video file ${index + 1}`,
  errorIconAriaLabel: 'Error',
};

export interface StaticVideoPanelProps {
  deviceId: string;
  usecaseId: string;
  /** Whether the user holds the device-mutation permission. */
  canMutate: boolean;
  /**
   * Whether this panel is the arrival target of the node panel's
   * "Pin a test video…" shortcut: it then carries an arrival flag so the
   * pin controls read as the next action (Req 9.7).
   */
  focused?: boolean;
}

type UploadPhase = 'idle' | 'uploading' | 'submitting';

export default function StaticVideoPanel({
  deviceId,
  usecaseId,
  canMutate,
  focused = false,
}: StaticVideoPanelProps) {
  const [status, setStatus] = useState<StaticVideoPinStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [phase, setPhase] = useState<UploadPhase>('idle');
  const [uploadProgress, setUploadProgress] = useState(0);
  const [removing, setRemoving] = useState(false);
  const [removeConfirmVisible, setRemoveConfirmVisible] = useState(false);

  const loadStatus = useCallback(async () => {
    if (!deviceId || !usecaseId) return;
    try {
      setLoadError(null);
      const response = await apiService.getStaticVideoPinStatus(deviceId, usecaseId);
      setStatus(response);
    } catch (err: any) {
      setLoadError(err?.message || 'Failed to load the static video camera status');
    } finally {
      setLoading(false);
    }
  }, [deviceId, usecaseId]);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  const pollMs = videoStatusPollInterval(status);
  useEffect(() => {
    if (pollMs === null) return undefined;
    const interval = setInterval(() => {
      loadStatus();
    }, pollMs);
    return () => clearInterval(interval);
  }, [pollMs, loadStatus]);

  const selected = files[0];
  const oversize = selected ? videoOversizeMessage(selected.size) : null;
  const busy = phase !== 'idle';

  const handlePin = async () => {
    if (!selected) {
      setActionError('Choose a video file to pin');
      return;
    }
    const tooLarge = videoOversizeMessage(selected.size);
    if (tooLarge) {
      // Never start an upload the Portal would reject (Req 9.3).
      setActionError(tooLarge);
      return;
    }
    try {
      setActionError(null);
      setUploadProgress(0);
      setPhase('uploading');
      // Presigned PUT to a staging key (shared with images), with progress
      // (Req 9.4); then the pin submit starts the Portal's validation.
      const upload = await apiService.getStaticVideoUploadUrl(deviceId, usecaseId);
      await putFileWithProgress(upload.uploadUrl, selected, setUploadProgress);
      setPhase('submitting');
      await apiService.pinStaticVideo(deviceId, usecaseId, {
        stagingKey: upload.stagingKey,
        fileName: selected.name,
      });
      setFiles([]);
      await loadStatus();
    } catch (err: any) {
      setActionError(err?.message || 'Failed to pin the video');
    } finally {
      setPhase('idle');
    }
  };

  const confirmRemove = async () => {
    try {
      setRemoving(true);
      setActionError(null);
      await apiService.removeStaticVideoPin(deviceId, usecaseId);
      setRemoveConfirmVisible(false);
      await loadStatus();
    } catch (err: any) {
      setActionError(err?.message || 'Failed to remove the pinned video');
      setRemoveConfirmVisible(false);
    } finally {
      setRemoving(false);
    }
  };

  const latest = status?.latest ?? null;
  const pending = latest?.status === 'pending';
  const validation = outstandingValidation(status);
  const deviceReported = status?.deviceReported ?? null;
  const appliedMetadata = latest?.deviceMetadata ?? status?.deviceMetadata ?? null;
  const validatedMetadata =
    !appliedMetadata && pending && latest?.op === 'pin'
      ? latest?.validatedMetadata ?? null
      : null;
  const pinButtonLabel = deviceReported?.present ? 'Replace video' : 'Pin video';

  const requestIndicator = () => {
    if (latest === null) return null;
    const opLabel = latest.op === 'remove' ? 'Video removal' : 'Video pin';
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
      data-testid="static-video-panel"
      header={
        <Header
          variant="h2"
          description="Pin a video from the Portal; the device plays it in a loop, in
            real time, as the static-video-camera source while a video is pinned."
        >
          Static video camera
        </Header>
      }
    >
      {focused && (
        <Box margin={{ bottom: 'm' }}>
          <Alert
            type="info"
            data-testid="static-video-focus-flag"
            header="Pin a test video here"
          >
            The workflow node&apos;s camera picker sent you here. Upload a
            video below to pin it to this device&apos;s static-video-camera
            source; it plays in a loop, and the node binds to it like any
            other camera.
          </Alert>
        </Box>
      )}

      {loading ? (
        <Box textAlign="center" padding="m">
          <Spinner />
        </Box>
      ) : loadError ? (
        <SpaceBetween size="s">
          <Alert type="error" data-testid="static-video-load-error">
            {loadError}
          </Alert>
          <Button onClick={() => loadStatus()} data-testid="static-video-retry">
            Retry loading the video camera status
          </Button>
        </SpaceBetween>
      ) : (
        <SpaceBetween size="m">
          {actionError && (
            <Alert
              type="error"
              dismissible
              onDismiss={() => setActionError(null)}
              data-testid="static-video-action-error"
            >
              {actionError}
            </Alert>
          )}

          {status?.noPinRequest && validation === null && (
            <Box color="text-body-secondary" data-testid="static-video-no-request">
              No cloud video pin request exists for this device. Upload a
              video to pin it to the device&apos;s static video camera.
            </Box>
          )}

          {/* The latest submission's asynchronous validation (Reqs 8.2-8.4). */}
          {validation !== null && (
            <SpaceBetween size="xxs">
              <span data-testid="static-video-validation">
                {validation.status === 'validating' ? (
                  <StatusIndicator type="loading">
                    {`Validating ${validation.fileName || 'the video'}…`}
                  </StatusIndicator>
                ) : (
                  <StatusIndicator type="error">
                    {`Video rejected${validation.fileName ? `: ${validation.fileName}` : ''}`}
                  </StatusIndicator>
                )}
              </span>
              {validation.status === 'validating' ? (
                <Box variant="small" color="text-body-secondary">
                  The Portal decodes the video&apos;s first and last frames
                  before sending it to the device; this can take up to a
                  minute.
                </Box>
              ) : (
                validation.error && (
                  <Box
                    variant="small"
                    color="text-status-error"
                    data-testid="static-video-validation-error"
                  >
                    {validation.error}
                  </Box>
                )
              )}
            </SpaceBetween>
          )}

          {/* Latest Video_Pin_Request Sync_Status. */}
          {latest !== null && (
            <SpaceBetween size="xxs">
              <SpaceBetween direction="horizontal" size="xs">
                <span data-testid="static-video-status">{requestIndicator()}</span>
                <Box variant="small" color="text-body-secondary">
                  {`Request ${latest.pinRequestId} · created ${formatVideoTimestamp(latest.createdAt)}`}
                </Box>
              </SpaceBetween>
              {latest.status === 'failed' && latest.failureReason && (
                <Box
                  variant="small"
                  color="text-status-error"
                  data-testid="static-video-failure-reason"
                >
                  {latest.failureReason}
                </Box>
              )}
            </SpaceBetween>
          )}

          {pending && status?.connectivity && (
            <Alert
              type={status.connectivity === 'disconnected' ? 'warning' : 'info'}
              data-testid="static-video-connectivity-hint"
            >
              {status.connectivity === 'disconnected'
                ? 'The device is currently disconnected. The video request stays pending and applies when the device reconnects.'
                : 'The device is connected. The pending video request should apply shortly.'}
            </Alert>
          )}

          {deviceReported !== null && (
            <Box data-testid="static-video-device-reported">
              {deviceReported.present ? (
                <StatusIndicator type="success">Device reports a pinned video</StatusIndicator>
              ) : (
                <StatusIndicator type="stopped">
                  {`Device reports the static video camera absent${
                    deviceReported.absentSince
                      ? ` since ${formatVideoTimestamp(deviceReported.absentSince)}`
                      : ''
                  }`}
                </StatusIndicator>
              )}
            </Box>
          )}

          {/* Applied video metadata (Req 9.5). */}
          {appliedMetadata && (
            <div data-testid="static-video-metadata">
              <KeyValuePairs columns={4} items={videoMetadataItems(appliedMetadata)} />
            </div>
          )}

          {/* While the device has not applied it yet: what the Portal validated. */}
          {validatedMetadata && (
            <SpaceBetween size="xxs">
              <Box variant="small" color="text-body-secondary">
                Validated by the Portal; the device reports its own details
                once it applies the video.
              </Box>
              <div data-testid="static-video-validated-metadata">
                <KeyValuePairs columns={4} items={videoMetadataItems(validatedMetadata)} />
              </div>
            </SpaceBetween>
          )}

          {canMutate && (
            <SpaceBetween size="xs">
              <FormField
                label={pinButtonLabel === 'Replace video' ? 'Replace the pinned video' : 'Pin a video'}
                description="MP4, M4V, MOV, AVI, MKV, or WebM, at most 100 MB. The video plays in a loop at its own frame rate."
                errorText={oversize ?? undefined}
              >
                <FileUpload
                  value={files}
                  onChange={({ detail }) => {
                    setActionError(null);
                    setFiles(detail.value);
                  }}
                  accept={VIDEO_FILE_ACCEPT}
                  constraintText="MP4, M4V, MOV, AVI, MKV, or WebM, at most 100 MB"
                  i18nStrings={FILE_UPLOAD_I18N}
                />
              </FormField>
              {phase === 'uploading' && (
                <div data-testid="static-video-upload-progress">
                  <ProgressBar
                    value={uploadProgress}
                    label={`Uploading ${selected?.name ?? 'the video'}`}
                    description={selected ? formatMegabytes(selected.size) : undefined}
                  />
                </div>
              )}
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="primary"
                  onClick={handlePin}
                  loading={busy}
                  disabled={files.length === 0 || oversize !== null}
                  data-testid="static-video-pin-button"
                >
                  {pinButtonLabel}
                </Button>
                <Button
                  onClick={() => setRemoveConfirmVisible(true)}
                  loading={removing}
                  disabled={busy}
                  data-testid="static-video-remove-button"
                >
                  Remove pinned video
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          )}
        </SpaceBetween>
      )}

      <Modal
        visible={removeConfirmVisible}
        onDismiss={() => setRemoveConfirmVisible(false)}
        header="Remove pinned video"
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
                data-testid="static-video-remove-confirm"
              >
                Remove
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          Remove the device&apos;s pinned video? The removal is delivered
          through the sync channel; the static video camera disappears from
          the device&apos;s camera inventory once applied. The static image
          camera is not affected.
        </Box>
      </Modal>
    </Container>
  );
}
