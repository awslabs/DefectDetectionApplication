import { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Modal,
  SpaceBetween,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { getErrorMessage } from '../utils/errorHandling';

/** Outcome of a removal run, reported to the page that opened the dialog. */
export interface RemoveDevicesResult {
  /** Devices that were removed. */
  removed: string[];
  /** Devices that could not be removed, with the reason. */
  failed: { deviceId: string; message: string }[];
  /** Clean-up warnings for removed devices, prefixed with the device id. */
  warnings: string[];
  /** Whether the AWS IoT things were deleted too. */
  deleteThing: boolean;
}

/** What a page shows after devices were removed. */
export interface RemovalNotice {
  message: string;
  warnings: string[];
}

/** The notice for a removal run, or null when nothing was removed. */
export function removalNotice(result: RemoveDevicesResult): RemovalNotice | null {
  if (result.removed.length === 0) return null;
  const names = result.removed.join(', ');
  const single = result.removed.length === 1;
  const message = result.deleteThing
    ? `Removed ${names} from DDA and deleted ${single ? 'its AWS IoT thing' : 'their AWS IoT things'}.`
    : `Removed ${names} from DDA.`;
  return { message, warnings: result.warnings };
}

interface RemoveDevicesModalProps {
  /** Devices (thing names) to remove. */
  deviceIds: string[];
  usecaseId: string;
  onDismiss: () => void;
  /** Called once every device has been attempted. */
  onComplete: (result: RemoveDevicesResult) => void;
}

/**
 * Confirms and runs the removal of one or more devices
 * (DELETE /devices/{id}). By default a device is only removed from DDA; the
 * checkbox also deletes its AWS IoT thing and certificates.
 */
export default function RemoveDevicesModal({
  deviceIds,
  usecaseId,
  onDismiss,
  onComplete,
}: RemoveDevicesModalProps) {
  const [deleteThing, setDeleteThing] = useState(false);
  // 1-based index of the device being removed; null while not removing.
  const [progress, setProgress] = useState<number | null>(null);
  const removing = progress !== null;
  const single = deviceIds.length === 1;

  const handleRemove = async () => {
    const result: RemoveDevicesResult = {
      removed: [],
      failed: [],
      warnings: [],
      deleteThing,
    };
    // One at a time: each removal makes several AWS IoT calls.
    for (let index = 0; index < deviceIds.length; index += 1) {
      const deviceId = deviceIds[index];
      setProgress(index + 1);
      try {
        const response = await apiService.deleteDevice(deviceId, usecaseId, {
          deleteThing,
        });
        result.removed.push(deviceId);
        result.warnings.push(
          ...(response.warnings ?? []).map((warning) => `${deviceId}: ${warning}`)
        );
      } catch (err) {
        result.failed.push({
          deviceId,
          message: getErrorMessage(err, 'Failed to remove the device'),
        });
      }
    }
    setProgress(null);
    onComplete(result);
  };

  return (
    <Modal
      visible
      onDismiss={() => (removing ? undefined : onDismiss())}
      header={single ? 'Remove device' : `Remove ${deviceIds.length} devices`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={removing}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleRemove}
              loading={removing}
              data-testid="remove-devices-confirm"
            >
              Remove
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {single ? (
          <Box variant="p">
            Remove <b>{deviceIds[0]}</b> from DDA?
          </Box>
        ) : (
          <SpaceBetween size="xs">
            <Box variant="p">Remove these devices from DDA?</Box>
            <ul>
              {deviceIds.map((deviceId) => (
                <li key={deviceId}>{deviceId}</li>
              ))}
            </ul>
          </SpaceBetween>
        )}
        <Box variant="p">
          This deletes the Greengrass core device record and the portal's records
          for {single ? 'the device' : 'each device'}: its settings, camera
          registry and device registration.
        </Box>
        <Checkbox
          checked={deleteThing}
          onChange={({ detail }) => setDeleteThing(detail.checked)}
          disabled={removing}
          description="Use this for a decommissioned device, or one you will set up again under the same name."
        >
          Also delete the AWS IoT thing and its certificates
        </Checkbox>
        {deleteThing ? (
          <Alert type="warning">
            This can&apos;t be undone. The device&apos;s certificates are deactivated
            and deleted, so it can&apos;t connect to AWS IoT until it is set up again
            with Add Device.
          </Alert>
        ) : (
          <Alert type="info">
            The device itself is not changed. If it is still running, it keeps its
            components and its AWS IoT connection, but it no longer appears in DDA.
          </Alert>
        )}
        {removing && !single && (
          <Box variant="small" color="text-body-secondary">
            Removing {progress} of {deviceIds.length}…
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
}
