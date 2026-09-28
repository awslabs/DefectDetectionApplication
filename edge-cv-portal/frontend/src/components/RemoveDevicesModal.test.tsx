/**
 * RemoveDevicesModal: confirms and runs DELETE /devices/{id} for one or more
 * devices. By default a device is only removed from DDA; the checkbox also
 * deletes its AWS IoT thing and certificates.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import RemoveDevicesModal, { removalNotice } from './RemoveDevicesModal';

const { deleteDevice } = vi.hoisted(() => ({ deleteDevice: vi.fn() }));

vi.mock('../services/api', () => ({ apiService: { deleteDevice } }));

function removed(deviceId: string, warnings: string[] = []) {
  return {
    deleted: true,
    device_id: deviceId,
    usecase_id: 'uc-1',
    delete_thing: false,
    thing_deleted: false,
    certificates_deactivated: [],
    certificates_deleted: [],
    shadows_deleted: [],
    warnings,
  };
}

function renderModal(deviceIds: string[]) {
  const onComplete = vi.fn();
  const onDismiss = vi.fn();
  render(
    <RemoveDevicesModal
      deviceIds={deviceIds}
      usecaseId="uc-1"
      onDismiss={onDismiss}
      onComplete={onComplete}
    />
  );
  return { onComplete, onDismiss };
}

async function confirm() {
  await act(async () => {
    fireEvent.click(screen.getByTestId('remove-devices-confirm'));
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('RemoveDevicesModal', () => {
  it('removes a device from DDA only by default', async () => {
    deleteDevice.mockResolvedValue(removed('station-a'));
    const { onComplete } = renderModal(['station-a']);

    expect(screen.getByText('Remove device')).toBeInTheDocument();
    expect(screen.getByText('station-a')).toBeInTheDocument();
    expect(screen.getByText(/The device itself is not changed/)).toBeInTheDocument();

    await confirm();

    expect(deleteDevice).toHaveBeenCalledTimes(1);
    expect(deleteDevice).toHaveBeenCalledWith('station-a', 'uc-1', {
      deleteThing: false,
    });
    expect(onComplete).toHaveBeenCalledWith({
      removed: ['station-a'],
      failed: [],
      warnings: [],
      deleteThing: false,
    });
  });

  it('also deletes the AWS IoT thing when the checkbox is ticked', async () => {
    deleteDevice.mockResolvedValue({ ...removed('station-a'), delete_thing: true });
    const { onComplete } = renderModal(['station-a']);

    fireEvent.click(
      screen.getByRole('checkbox', {
        name: /Also delete the AWS IoT thing and its certificates/,
      })
    );
    expect(screen.getByText(/can't be undone/)).toBeInTheDocument();
    expect(screen.queryByText(/The device itself is not changed/)).not.toBeInTheDocument();

    await confirm();

    expect(deleteDevice).toHaveBeenCalledWith('station-a', 'uc-1', {
      deleteThing: true,
    });
    expect(onComplete.mock.calls[0][0].deleteThing).toBe(true);
  });

  it('removes several devices one at a time and reports failures and warnings', async () => {
    let finishFirst: (value: unknown) => void = () => {};
    deleteDevice
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishFirst = resolve;
          })
      )
      .mockRejectedValueOnce(new Error('Failed to delete the Greengrass core device'))
      .mockResolvedValueOnce(removed('c'));
    const { onComplete } = renderModal(['a', 'b', 'c']);

    expect(screen.getByText('Remove 3 devices')).toBeInTheDocument();
    for (const id of ['a', 'b', 'c']) {
      expect(screen.getByText(id)).toBeInTheDocument();
    }

    await confirm();

    // The next device waits for the one in flight.
    expect(deleteDevice).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Removing 1 of 3…')).toBeInTheDocument();

    await act(async () => {
      finishFirst(removed('a', ['Certificate cert-1 is also attached to b-twin']));
    });

    expect(deleteDevice.mock.calls.map((call) => call[0])).toEqual(['a', 'b', 'c']);
    expect(onComplete).toHaveBeenCalledWith({
      removed: ['a', 'c'],
      failed: [{ deviceId: 'b', message: 'Failed to delete the Greengrass core device' }],
      warnings: ['a: Certificate cert-1 is also attached to b-twin'],
      deleteThing: false,
    });
  });

  it('cancels without removing anything', () => {
    const { onComplete, onDismiss } = renderModal(['station-a']);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(deleteDevice).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
  });
});

describe('removalNotice', () => {
  const base = { failed: [], warnings: [], deleteThing: false };

  it('is null when nothing was removed', () => {
    expect(
      removalNotice({ ...base, removed: [], failed: [{ deviceId: 'a', message: 'x' }] })
    ).toBeNull();
  });

  it('names the removed devices and says whether their things were deleted', () => {
    expect(removalNotice({ ...base, removed: ['a'] })).toEqual({
      message: 'Removed a from DDA.',
      warnings: [],
    });
    expect(
      removalNotice({ ...base, removed: ['a'], deleteThing: true, warnings: ['w'] })
    ).toEqual({
      message: 'Removed a from DDA and deleted its AWS IoT thing.',
      warnings: ['w'],
    });
    expect(removalNotice({ ...base, removed: ['a', 'b'], deleteThing: true })?.message).toBe(
      'Removed a, b from DDA and deleted their AWS IoT things.'
    );
  });
});
